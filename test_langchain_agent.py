"""LangChain 只读 Agent 测试：直接运行 python test_langchain_agent.py。

全 Mock ChatModel（脚本化回放），不发起任何真实 LLM / 网络请求。

覆盖：
- 工具面只有 get_recent_feedback / search_knowledge（save_plan 等写工具绝不暴露）
- 工具经 agent_dispatcher 固定名称白名单调用（非白名单名称直接拒绝）
- 完整闭环：模型 tool call -> Dispatcher -> Tool Result -> 最终回答
- 多工具调用顺序、工具调用预算、模型调用预算
- 超时（run_timeout）与空回答（empty_response）稳定结构
- 模型异常 -> llm_error 稳定结构
- 实验开关（LANGCHAIN_EXPERIMENT_ENABLED）与 API 路由 403 / 放行
- 输入校验（空问题拒绝）
"""

import contextlib
import json
import os
import time
from pathlib import Path

# 测试夹具：build_chat_model 用例显式回退本地配置（代码默认云端百炼，测试不依赖云端 Key）
os.environ.setdefault("LLM_PROVIDER", "ollama")
os.environ.setdefault("OLLAMA_MODEL", "qwen3.5:4b")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_dispatcher
import langchain_agent
import main


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


class ScriptedChatModel(BaseChatModel):
    """脚本化 ChatModel：按顺序回放 AIMessage（可带 tool_calls）。"""

    queue: list
    delay: float = 0.0

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.delay:
            time.sleep(self.delay)

        message = self.queue.pop(0) if self.queue else AIMessage(content="（脚本已用尽）")

        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_call_message(name, arguments, call_id="call_1"):
    return AIMessage(content="", tool_calls=[{
        "id": call_id,
        "name": name,
        "args": arguments,
    }])


@contextlib.contextmanager
def stub_dispatch(mapping):
    """打桩 agent_dispatcher.dispatch：按工具名返回固定结果并记录调用。"""

    calls = []
    original = agent_dispatcher.dispatch

    def fake(request):
        calls.append({"name": request.get("name"), "arguments": request.get("arguments")})
        name = request.get("name")

        if name not in mapping:
            return {"ok": False, "error": f"未知工具 {name}", "error_type": "unknown_tool",
                    "result": None}

        result = mapping[name]

        if isinstance(result, Exception):
            raise result

        return {"ok": True, "error": None, "error_type": None, "result": result}

    agent_dispatcher.dispatch = fake

    try:
        yield calls
    finally:
        agent_dispatcher.dispatch = original


@contextlib.contextmanager
def env_toggle(name, value):
    old = os.environ.get(name)

    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value

    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


FEEDBACK_RESULT = {"days": 7, "count": 1, "summary": {"too_difficult_count": 0}, "feedback": []}
SEARCH_RESULT = {"ok": True, "query": "定积分", "count": 1,
                 "results": [{"content": "片段", "source": "math.txt", "score": 0.8}],
                 "error": None, "error_type": None}

# ==========================
# 1. 工具面与安全边界
# ==========================

tools = langchain_agent._build_tools()
tool_names = sorted(tool.name for tool in tools)

check("工具面只有两个只读工具",
      tool_names == ["get_recent_feedback", "search_knowledge"], f"tools={tool_names}")
check("白名单不含 save_plan",
      "save_plan" not in langchain_agent.READONLY_TOOLS)
check("白名单不含任何写工具名",
      not any(name in langchain_agent.READONLY_TOOLS
              for name in ("save_plan", "insert_feedback", "delete_all_feedback",
                           "insert_plan", "execute_sql")))

with stub_dispatch({}):
    rejected = langchain_agent._dispatch_fixed("save_plan", {"plan": {}, "plan_date": "2026-10-10",
                                                             "confirmation_id": "x"})

check("绕过白名单直接调 save_plan 被拒绝",
      rejected["ok"] is False and rejected["error_type"] == "tool_not_allowed",
      f"rejected={rejected}")

# ==========================
# 2. 完整闭环（mock LLM + mock Dispatcher）
# ==========================

model = ScriptedChatModel(queue=[
    tool_call_message("get_recent_feedback", {"days": 7}),
    tool_call_message("search_knowledge", {"query": "定积分", "top_k": 3}, call_id="call_2"),
    AIMessage(content="这是最终回答"),
])

with stub_dispatch({"get_recent_feedback": FEEDBACK_RESULT, "search_knowledge": SEARCH_RESULT}) as calls:
    result = langchain_agent.run_langchain_agent("帮我看看学习情况并查下资料", model=model)

check("闭环拿到最终回答",
      result["ok"] is True and result["final_answer"] == "这是最终回答"
      and result["stopped_reason"] == "final_answer")
check("工具按顺序真实派发到 Dispatcher",
      [call["name"] for call in calls] == ["get_recent_feedback", "search_knowledge"],
      f"calls={calls}")
check("工具参数透传", calls[1]["arguments"] == {"query": "定积分", "top_k": 3})
check("步骤记录完整",
      [step["tool"] for step in result["steps"]] == ["get_recent_feedback", "search_knowledge"]
      and all(step["ok"] for step in result["steps"]))
check("结果标记 framework=langchain", result["framework"] == "langchain")
check("耗时已记录", isinstance(result["elapsed_seconds"], float) and result["elapsed_seconds"] >= 0)

# ==========================
# 3. 预算与超时
# ==========================

model = ScriptedChatModel(queue=[
    tool_call_message("get_recent_feedback", {"days": 7}),
    tool_call_message("get_recent_feedback", {"days": 7}, call_id="call_2"),
    AIMessage(content="回答"),
])

with stub_dispatch({"get_recent_feedback": FEEDBACK_RESULT}) as calls:
    budget_result = langchain_agent.run_langchain_agent(
        "连续调用两次", model=model, max_tool_calls=1
    )

check("工具调用预算生效（超出的调用不执行）",
      len(calls) == 1 and budget_result["tool_calls"] <= 1,
      f"calls={len(calls)} tool_calls={budget_result['tool_calls']} steps={budget_result['steps']}")
check("预算拒绝不影响最终回答", budget_result["ok"] is True and budget_result["final_answer"] == "回答")

# 预算拒绝路径（工具层）返回稳定 error_type
exhausted_context = langchain_agent.RunContext(max_tool_calls=0, max_seconds=60)
with stub_dispatch({"get_recent_feedback": FEEDBACK_RESULT}):
    rejected_budget = langchain_agent._run_tool(exhausted_context, "get_recent_feedback", {"days": 7})

check("工具层预算拒绝带稳定 error_type",
      rejected_budget["ok"] is False and rejected_budget["error_type"] == "tool_budget_exceeded"
      and exhausted_context.steps[-1]["error_type"] == "tool_budget_exceeded")

# 模型反复要求调用工具：模型调用预算触发后必须停止（不无限循环）
model = ScriptedChatModel(queue=[
    tool_call_message("get_recent_feedback", {"days": 7}, call_id=f"call_{index}")
    for index in range(10)
])

with stub_dispatch({"get_recent_feedback": FEEDBACK_RESULT}) as calls:
    runaway = langchain_agent.run_langchain_agent(
        "无限调用工具", model=model, max_model_calls=3, max_tool_calls=2
    )

check("模型/工具预算触发后停止（不无限循环）",
      len(calls) <= 2 and runaway["tool_calls"] <= 2
      and runaway["stopped_reason"] in {"empty_response", "final_answer"},
      f"calls={len(calls)} stopped={runaway['stopped_reason']}")

# 超时：模型每次调用延迟超过总预算 -> run_timeout 稳定结构
model = ScriptedChatModel(queue=[AIMessage(content="迟到的回答")], delay=1.0)

timeout_result = langchain_agent.run_langchain_agent("慢问题", model=model, max_seconds=0.3)

check("超过墙钟预算返回 run_timeout",
      timeout_result["ok"] is False and timeout_result["stopped_reason"] == "run_timeout",
      f"result={timeout_result}")

# 超时后工具不再执行
slow_model = ScriptedChatModel(queue=[
    tool_call_message("get_recent_feedback", {"days": 7}),
    AIMessage(content="回答"),
], delay=1.0)

with stub_dispatch({"get_recent_feedback": FEEDBACK_RESULT}) as calls:
    timeout_tool = langchain_agent.run_langchain_agent(
        "超时后工具", model=slow_model, max_seconds=0.3
    )

check("超时后不再调用工具",
      timeout_tool["stopped_reason"] == "run_timeout"
      and len(calls) == 0
      and any(step["error_type"] == "agent_timeout" for step in timeout_tool["steps"]),
      f"calls={len(calls)} steps={timeout_tool['steps']}")

# ==========================
# 4. 错误结构
# ==========================

empty = langchain_agent.run_langchain_agent("   ")

check("空问题返回稳定错误结构",
      empty["ok"] is False and empty["stopped_reason"] == "empty_response" and empty["error"])


class ExplodingChatModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "exploding"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("模拟模型服务爆炸")


exploded = langchain_agent.run_langchain_agent("出错的问题", model=ExplodingChatModel())

check("模型异常返回 llm_error 稳定结构",
      exploded["ok"] is False and exploded["stopped_reason"] == "llm_error"
      and "RuntimeError" in exploded["error"],
      f"result={exploded}")

model = ScriptedChatModel(queue=[AIMessage(content="   ")])

blank = langchain_agent.run_langchain_agent("空回答", model=model)

check("模型空回答返回 empty_response", blank["ok"] is False and blank["stopped_reason"] == "empty_response")

# Dispatcher 异常被工具边界兜住（不让异常击穿 Agent）
model = ScriptedChatModel(queue=[
    tool_call_message("get_recent_feedback", {"days": 7}),
    AIMessage(content="工具坏了之后的回答"),
])

with stub_dispatch({"get_recent_feedback": RuntimeError("数据库炸了")}):
    tool_error = langchain_agent.run_langchain_agent("工具异常", model=model)

check("工具异常不击穿 Agent",
      tool_error["ok"] is True and tool_error["final_answer"] == "工具坏了之后的回答"
      and tool_error["steps"][0]["ok"] is False)

# ==========================
# 5. 实验开关与 API 路由
# ==========================

with env_toggle("LANGCHAIN_EXPERIMENT_ENABLED", None):
    check("默认关闭实验入口", langchain_agent.is_experiment_enabled() is False)

with env_toggle("LANGCHAIN_EXPERIMENT_ENABLED", "1"):
    check("LANGCHAIN_EXPERIMENT_ENABLED=1 启用", langchain_agent.is_experiment_enabled() is True)

    with stub_dispatch({}):
        disabled_model = ScriptedChatModel(queue=[AIMessage(content="回答")])

        def fake_run(question):
            return {"ok": True, "final_answer": f"回答:{question}", "stopped_reason": "final_answer",
                    "error": None, "tool_calls": 0, "steps": [], "elapsed_seconds": 0.01,
                    "framework": "langchain"}

        original_run = langchain_agent.run_langchain_agent
        langchain_agent.run_langchain_agent = fake_run

        try:
            response = main.langchain_ask(main.LangChainAskRequest(question="你好"))
        finally:
            langchain_agent.run_langchain_agent = original_run

        check("启用后实验路由放行",
              isinstance(response, dict) and response["ok"] is True
              and response["final_answer"] == "回答:你好")

with env_toggle("LANGCHAIN_EXPERIMENT_ENABLED", None):
    response = main.langchain_ask(main.LangChainAskRequest(question="你好"))
    check("关闭时实验路由返回 403",
          getattr(response, "status_code", None) == 403
          and json.loads(response.body)["error_type"] == "experiment_disabled")

# 输入校验
try:
    main.LangChainAskRequest(question="")
    check("空问题被输入校验拒绝", False)
except Exception as error:
    check("空问题被输入校验拒绝", type(error).__name__ in {"ValidationError", "ValueError"})

try:
    main.LangChainAskRequest(question="x" * 501)
    check("超长问题被输入校验拒绝", False)
except Exception as error:
    check("超长问题被输入校验拒绝", type(error).__name__ in {"ValidationError", "ValueError"})

# ==========================
# 6. 系统 Prompt 声明只读边界
# ==========================

prompt = langchain_agent.build_system_prompt()

check("系统 Prompt 明确没有写权限",
      "写权限" in prompt and "不能保存学习计划" in prompt)

# ChatModel 超时用整轮运行预算（不能拿 read timeout 当整请求超时）
chat_model = langchain_agent.build_chat_model(timeout=77)

check("ChatModel 超时对齐运行预算",
      getattr(chat_model, "timeout", None) == 77
      or getattr(chat_model, "request_timeout", None) == 77,
      f"timeout={getattr(chat_model, 'timeout', None)}")

print("ALL TESTS COMPLETED")
