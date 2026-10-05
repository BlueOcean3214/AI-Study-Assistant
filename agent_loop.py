"""最小只读 Agent Loop：LLM -> Tool Call -> Dispatcher -> Tool Result -> LLM -> Final Answer。

本阶段只验证“LLM 能否根据任务和 Tool Result 动态决定下一步”，不实现：
Planner / 长期记忆 / 多 Agent / 反思 / 自我评估 / 任何写操作。

关键约束：
- 只允许调用 agent_dispatcher 白名单里的两个只读工具
- Agent 自己的 LLM 调用走本模块的 chat_with_tools()（原生 Ollama tools 参数），
  不调用 ai_service.call_ollama()，也不改动 ai_service 的行为
- 有预算上限：MAX_TOOL_CALLS / MAX_LOOP_TURNS
- 一轮里模型可能一次请求多个工具，按顺序派发，每个都计入预算
"""

import json

import requests

from ai_service import OLLAMA_MODEL, OLLAMA_URL

from agent_dispatcher import dispatch
from agent_schema import describe_tools, get_tool_schemas


MAX_TOOL_CALLS = 4
MAX_LOOP_TURNS = 3
OLLAMA_TIMEOUT = 120

STOP_FINAL_ANSWER = "final_answer"
STOP_TOOL_BUDGET = "tool_budget_exceeded"
STOP_TURN_BUDGET = "turn_budget_exceeded"
STOP_LLM_ERROR = "llm_error"
STOP_EMPTY_RESPONSE = "empty_response"


def build_system_prompt():
    """系统 Prompt：角色、目标、可用工具（来自 Schema）与 10 条规则。"""

    return f"""你是 AI Study Assistant Agent。

目标：
根据用户要求，帮助用户分析学习情况、查询知识库，并回答问题。

可用工具：
{describe_tools()}

规则：
1. 不要编造用户历史反馈。
2. 需要真实历史时调用 get_recent_feedback。
3. 需要知识库内容时调用 search_knowledge。
4. Tool Result 是外部真实结果，不得伪造。
5. Tool 出现错误时，不得假装调用成功。
6. 没有足够信息时直接说明缺少信息。
7. 最多调用 {MAX_TOOL_CALLS} 次 Tool。
8. 最多运行 {MAX_LOOP_TURNS} 轮。
9. 当前阶段禁止修改用户数据。
10. 当前阶段不能保存学习计划。
"""


SYSTEM_PROMPT = build_system_prompt()


def chat_with_tools(messages, tools=None):
    """本模块唯一的 LLM 边界，返回 (assistant_message, error)。

    测试通过替换这个函数来验证 Loop 行为，不需要真的调用模型。
    """

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
    }

    if tools:
        payload["tools"] = tools

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        response.raise_for_status()
        message = response.json().get("message")
    except requests.exceptions.RequestException as error:
        return None, f"无法连接 Ollama（{type(error).__name__}）"
    except (ValueError, AttributeError):
        return None, "Ollama 返回内容格式异常"

    if not isinstance(message, dict):
        return None, "Ollama 返回内容格式异常"

    return message, None


def _parse_tool_calls(message):
    """把原生 tool_calls 规整成 [{"id", "name", "arguments"}]。

    Ollama 返回的 arguments 正常情况下是对象；这里对 JSON 字符串也做兼容，
    避免因为模型/版本差异直接崩掉。
    """

    calls = []

    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        name = function.get("name")
        arguments = function.get("arguments")

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}

        if not isinstance(arguments, dict):
            arguments = {}

        calls.append({
            "id": raw.get("id"),
            "name": name,
            "arguments": arguments,
        })

    return calls


def _log(verbose, tag, message):
    if verbose:
        print(f"{tag}\n{message}\n")


def _result(ok, final_answer, stopped_reason, error, state, steps, wrap_up_used=False):
    return {
        "ok": ok,
        "final_answer": final_answer,
        "stopped_reason": stopped_reason,
        "error": error,
        "tool_calls": state["tool_calls"],
        "loop_turns": state["loop_turns"],
        "wrap_up_used": wrap_up_used,
        "steps": steps,
        "messages": state["messages"],
    }


def _wrap_up(state, verbose):
    """预算耗尽时的收尾：不带 tools 再问一次，让模型用已有信息给出回答。

    为什么需要它：实测模型在工具报错（例如知识检索服务不可用）时会反复重试工具，
    3 轮用尽后直接返回“预算超限”会让用户什么都看不到。
    这里只做一次、且禁用工具，因此不会突破 MAX_TOOL_CALLS。
    """

    _log(verbose, "[Agent]", "预算用尽，改用不带工具的收尾调用")

    message, error = chat_with_tools(state["messages"], None)

    if error:
        return None

    content = (message.get("content") or "").strip()

    if not content:
        return None

    state["messages"].append({"role": "assistant", "content": content})

    _log(verbose, "[Agent]", f"Final Answer:\n{content}")

    return content


def run_agent(user_message, max_tool_calls=MAX_TOOL_CALLS, max_loop_turns=MAX_LOOP_TURNS, verbose=True):
    """跑一次只读 Agent Loop，返回结构化运行结果。

    返回：
    {
      "ok": bool,                    # 是否拿到了最终回答
      "final_answer": str | None,
      "stopped_reason": "final_answer" | "tool_budget_exceeded" | "turn_budget_exceeded"
                        | "llm_error" | "empty_response",
      "error": str | None,
      "tool_calls": int,             # 实际执行的工具次数（不会超过 max_tool_calls）
      "loop_turns": int,             # 工具决策轮数（不会超过 max_loop_turns）
      "wrap_up_used": bool,          # 是否在预算耗尽后用了“不带工具的收尾调用”
      "steps": [{"type": "tool_call", "tool", "arguments", "ok", "error_type", "result"}],
      "messages": [...],             # 完整消息历史（含 assistant tool_call 与 role=tool）
    }
    """

    state = {
        "user_message": user_message,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "tool_calls": 0,
        "loop_turns": 0,
    }

    steps = []

    _log(verbose, "[Agent]", f"User:\n{user_message}")

    while state["loop_turns"] < max_loop_turns:
        state["loop_turns"] += 1

        message, error = chat_with_tools(state["messages"], get_tool_schemas())

        if error:
            _log(verbose, "[Agent]", f"LLM 调用失败：{error}")

            return _result(False, None, STOP_LLM_ERROR, error, state, steps)

        calls = _parse_tool_calls(message)

        # 没有工具调用 => 这就是最终回答
        if not calls:
            content = message.get("content") or ""

            if not content.strip():
                _log(verbose, "[Agent]", "模型既没有回答也没有调用工具")

                return _result(False, None, STOP_EMPTY_RESPONSE, "模型返回内容为空", state, steps)

            # 最终回答也要进历史，调用方拿到的 messages 才是完整对话记录
            state["messages"].append({"role": "assistant", "content": content})

            _log(verbose, "[Agent]", f"Final Answer:\n{content}")

            return _result(True, content, STOP_FINAL_ANSWER, None, state, steps)

        # 预算检查：这一轮要调用的工具数量超出剩余预算就直接停，不执行
        if state["tool_calls"] + len(calls) > max_tool_calls:
            budget_error = (
                f"超过最大工具调用次数 {max_tool_calls}"
                f"（本轮请求 {len(calls)} 次，已用 {state['tool_calls']} 次）"
            )

            _log(verbose, "[Agent]", budget_error)

            answer = _wrap_up(state, verbose)

            if answer:
                return _result(True, answer, STOP_FINAL_ANSWER, None, state, steps, wrap_up_used=True)

            return _result(False, None, STOP_TOOL_BUDGET, budget_error, state, steps)

        # 把模型的 tool_call 原样放进历史，下一轮模型才能看到自己调用过什么
        state["messages"].append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls"),
        })

        for call in calls:
            _log(verbose, "[Agent]", f"Decision:\n{call['name']}")
            _log(verbose, "[Tool]", f"Arguments:\n{json.dumps(call['arguments'], ensure_ascii=False)}")

            outcome = dispatch({"name": call["name"], "arguments": call["arguments"]})

            state["tool_calls"] += 1

            _log(verbose, "[Tool]", f"Result:\n{json.dumps(outcome['result'] if outcome['ok'] else outcome, ensure_ascii=False)}")

            steps.append({
                "type": "tool_call",
                "tool": call["name"],
                "arguments": call["arguments"],
                "ok": outcome["ok"],
                "error_type": outcome["error_type"],
                "result": outcome["result"],
            })

            # 成功时原样透传 Tool Result；失败时给出结构化错误，让模型能正确说明原因
            if outcome["ok"]:
                payload = outcome["result"]
            else:
                payload = {
                    "ok": False,
                    "error": outcome["error"],
                    "error_type": outcome["error_type"],
                }

            state["messages"].append({
                "role": "tool",
                "content": json.dumps(payload, ensure_ascii=False),
            })

    turn_error = f"超过最大循环轮次 {max_loop_turns}"

    _log(verbose, "[Agent]", turn_error)

    answer = _wrap_up(state, verbose)

    if answer:
        return _result(True, answer, STOP_FINAL_ANSWER, None, state, steps, wrap_up_used=True)

    return _result(False, None, STOP_TURN_BUDGET, turn_error, state, steps)
