"""LangChain 只读 Agent（实验路径，与原生 agent_loop 对照）。

定位：
- 原生 agent_loop 仍是默认、经过完整测试的主路径，本模块不替换它
- 本模块用 LangChain 1.x 的 create_agent（langgraph 后端）实现一个独立的
  只读 Agent，用于对照实验：同样的只读问题分别跑原生 / LangChain 两版

安全边界（只读，写操作为零）：
- 工具面只有 get_recent_feedback / search_knowledge 两个只读工具
- 明确不注册 save_plan，也不暴露任何数据库写入 / 任意代码执行 / SQL 执行入口
- 工具内部经 agent_dispatcher 的固定名称白名单调用，参数同样走 Dispatcher 校验
- 模型无法通过本路径保存学习计划（保存仍走原生确认凭证安全链路）
- 预算：工具调用上限 + 模型调用上限 + 墙钟超时（工具执行前检查 deadline）

启用条件：
- LANGCHAIN_EXPERIMENT_ENABLED=1 时才开放 API 实验路由（默认关闭，不影响现有路由）
- run_langchain_agent() 供脚本 / 测试直接调用

模型：
- 优先使用已配置的 OpenAI 兼容 API（LLM_PROVIDER=openai_compatible）
- 本地 Ollama 模式经 Ollama 的 OpenAI 兼容端点 /v1 接入（复用同一 ChatOpenAI 适配）
"""

import json
import os
import time
from dataclasses import dataclass, field

import llm_client


# 只读工具白名单（与 agent_dispatcher 的只读工具一一对应）
READONLY_TOOLS = ("get_recent_feedback", "search_knowledge")

# 预算上限（与原生 Agent 的默认预算同量级）
DEFAULT_MAX_TOOL_CALLS = 4
DEFAULT_MAX_MODEL_CALLS = 5
DEFAULT_MAX_SECONDS = 120


def is_experiment_enabled():
    """实验开关：LANGCHAIN_EXPERIMENT_ENABLED=1 才启用（默认关闭）。"""

    value = (os.getenv("LANGCHAIN_EXPERIMENT_ENABLED") or "0").strip().lower()

    return value in {"1", "true", "on", "yes"}


class LangChainAgentError(RuntimeError):
    """LangChain 实验路径的稳定错误。"""


# ==========================
# 运行上下文（预算 / 步骤 / deadline）
# ==========================


@dataclass
class RunContext:
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_seconds: float = DEFAULT_MAX_SECONDS
    started: float = field(default_factory=time.perf_counter)
    tool_calls: int = 0
    steps: list = field(default_factory=list)

    def expired(self):
        return time.perf_counter() - self.started >= self.max_seconds

    def remaining(self):
        return max(0.0, self.max_seconds - (time.perf_counter() - self.started))


def _dispatch_fixed(name, arguments):
    """固定名称约束的 Dispatcher 调用：不在白名单内的工具名直接拒绝。"""

    if name not in READONLY_TOOLS:
        return {
            "ok": False,
            "error": f"工具 {name} 不在 LangChain 只读白名单内",
            "error_type": "tool_not_allowed",
        }

    import agent_dispatcher

    return agent_dispatcher.dispatch({"name": name, "arguments": arguments})


def _run_tool(context, name, arguments):
    """工具执行统一入口：预算 / deadline / 步骤记录 / 异常兜底。

    context 通过工具闭包显式绑定（langgraph 在线程池里执行同步工具，
    threading.local 不跨线程，闭包绑定既线程安全又支持并发运行互不干扰）。
    """

    if context is not None:
        if context.expired():
            context.steps.append({
                "type": "tool_call",
                "tool": name,
                "arguments": arguments,
                "ok": False,
                "error_type": llm_client.TIMEOUT_ERROR,
            })

            return {"ok": False, "error": "Agent 运行超时，工具未执行",
                    "error_type": llm_client.TIMEOUT_ERROR}

        if context.tool_calls >= context.max_tool_calls:
            context.steps.append({
                "type": "tool_call",
                "tool": name,
                "arguments": arguments,
                "ok": False,
                "error_type": "tool_budget_exceeded",
            })

            return {"ok": False, "error": f"超过最大工具调用次数 {context.max_tool_calls}",
                    "error_type": "tool_budget_exceeded"}

        context.tool_calls += 1

    try:
        outcome = _dispatch_fixed(name, arguments)
    except Exception as error:  # noqa: BLE001 - 工具边界不允许把异常泄漏给 Agent
        outcome = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
            "error_type": "unexpected_error",
        }

    if context is not None:
        context.steps.append({
            "type": "tool_call",
            "tool": name,
            "arguments": arguments,
            "ok": outcome.get("ok", False),
            "error_type": outcome.get("error_type"),
        })

    return outcome


# ==========================
# LangChain 工具（只读两个）
# ==========================


def _build_tools(context=None):
    """构建 LangChain 工具列表（每次调用新建，全部只读；context 绑定预算与步骤）。"""

    from langchain_core.tools import tool

    @tool("get_recent_feedback")
    def get_recent_feedback_tool(days: int = 7) -> str:
        """获取用户最近的学习反馈和统计信息（只读）。days 范围 1-30，默认 7。"""

        outcome = _run_tool(context, "get_recent_feedback", {"days": days})

        return json.dumps(outcome, ensure_ascii=False)

    @tool("search_knowledge")
    def search_knowledge_tool(query: str, top_k: int = 3) -> str:
        """在学习知识库中搜索相关知识片段（只读）。query 为检索问题，top_k 范围 1-5。"""

        outcome = _run_tool(context, "search_knowledge", {"query": query, "top_k": top_k})

        return json.dumps(outcome, ensure_ascii=False)

    return [get_recent_feedback_tool, search_knowledge_tool]


def build_system_prompt():
    """只读 Agent 的系统 Prompt：明确没有写权限。

    文案集中在 prompt_registry（任务 langchain_readonly_system）。
    """

    import prompt_registry

    return prompt_registry.render("langchain_readonly_system")


# ==========================
# 模型构建（复用 llm_client 配置）
# ==========================


def _ollama_openai_base(chat_url):
    """Ollama 的 OpenAI 兼容端点：把 /api/chat 地址换算成 /v1。"""

    if chat_url.endswith("/api/chat"):
        return chat_url[: -len("/api/chat")] + "/v1"

    return chat_url.rstrip("/") + "/v1"


def build_chat_model(settings=None, timeout=None):
    """按 llm_client 配置构建 LangChain ChatModel（ChatOpenAI 适配两种 Provider）。

    timeout 用整轮运行预算，而不是 llm 的分块 read timeout——
    ChatOpenAI 的 timeout 是整个请求的上限，CPU 长生成时用 read timeout 会误杀请求。
    """

    from langchain_openai import ChatOpenAI

    settings = settings or llm_client.get_settings()
    timeout = timeout or max(DEFAULT_MAX_SECONDS, settings.read_timeout)

    if settings.provider == llm_client.PROVIDER_OPENAI:
        base_url = settings.chat_url

        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]

        return ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    # 本地 Ollama：使用其 OpenAI 兼容端点 /v1（tool calling 由 Ollama 转发给模型）
    return ChatOpenAI(
        model=settings.model,
        api_key="ollama",
        base_url=_ollama_openai_base(settings.chat_url),
        timeout=timeout,
        max_retries=0,
    )


# ==========================
# 运行入口
# ==========================


def run_langchain_agent(
    question,
    model=None,
    max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
    max_model_calls=DEFAULT_MAX_MODEL_CALLS,
    max_seconds=DEFAULT_MAX_SECONDS,
):
    """跑一次 LangChain 只读 Agent，返回结构化结果（与 run_agent 可对照的形状）。

    {
      "ok": bool,
      "final_answer": str | None,
      "stopped_reason": "final_answer" | "run_timeout" | "llm_error" | "empty_response",
      "error": str | None,
      "tool_calls": int,
      "steps": [{"type": "tool_call", "tool", "arguments", "ok", "error_type"}],
      "elapsed_seconds": float,
      "framework": "langchain",
    }
    """

    if not isinstance(question, str) or not question.strip():
        return {
            "ok": False,
            "final_answer": None,
            "stopped_reason": "empty_response",
            "error": "question 必须是非空字符串",
            "tool_calls": 0,
            "steps": [],
            "elapsed_seconds": 0.0,
            "framework": "langchain",
        }

    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

    started = time.perf_counter()
    context = RunContext(
        max_tool_calls=max_tool_calls,
        max_seconds=max_seconds,
        started=started,
    )

    try:
        chat_model = model if model is not None else build_chat_model(timeout=max_seconds)

        agent = create_agent(
            chat_model,
            tools=_build_tools(context),
            system_prompt=build_system_prompt(),
            middleware=[
                ToolCallLimitMiddleware(run_limit=max_tool_calls),
                ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end"),
            ],
        )

        result = agent.invoke(
            {"messages": [{"role": "user", "content": question.strip()}]}
        )
    except Exception as error:  # noqa: BLE001 - 实验路径必须返回稳定错误结构
        elapsed = round(time.perf_counter() - started, 3)

        if context.expired():
            return {
                "ok": False,
                "final_answer": None,
                "stopped_reason": "run_timeout",
                "error": f"Agent run exceeded {max_seconds} seconds",
                "tool_calls": context.tool_calls,
                "steps": context.steps,
                "elapsed_seconds": elapsed,
                "framework": "langchain",
            }

        return {
            "ok": False,
            "final_answer": None,
            "stopped_reason": "llm_error",
            "error": f"{type(error).__name__}: {error}",
            "tool_calls": context.tool_calls,
            "steps": context.steps,
            "elapsed_seconds": elapsed,
            "framework": "langchain",
        }

    elapsed = round(time.perf_counter() - started, 3)

    if context.expired():
        return {
            "ok": False,
            "final_answer": None,
            "stopped_reason": "run_timeout",
            "error": f"Agent run exceeded {max_seconds} seconds",
            "tool_calls": context.tool_calls,
            "steps": context.steps,
            "elapsed_seconds": elapsed,
            "framework": "langchain",
        }

    messages = result.get("messages") or []
    final_answer = None

    for message in reversed(messages):
        content = getattr(message, "content", None)

        if getattr(message, "type", "") == "ai" and isinstance(content, str) and content.strip():
            final_answer = content.strip()
            break

    if not final_answer:
        return {
            "ok": False,
            "final_answer": None,
            "stopped_reason": "empty_response",
            "error": "模型返回内容为空",
            "tool_calls": context.tool_calls,
            "steps": context.steps,
            "elapsed_seconds": elapsed,
            "framework": "langchain",
        }

    return {
        "ok": True,
        "final_answer": final_answer,
        "stopped_reason": "final_answer",
        "error": None,
        "tool_calls": context.tool_calls,
        "steps": context.steps,
        "elapsed_seconds": elapsed,
        "framework": "langchain",
    }


def main():
    """脚本入口：python langchain_agent.py "你的问题"。"""

    import sys

    question = " ".join(sys.argv[1:]).strip()

    if not question:
        print("用法: python langchain_agent.py \"你的问题\"")
        raise SystemExit(2)

    result = run_langchain_agent(question)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
