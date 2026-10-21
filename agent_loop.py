"""最小只读 Agent Loop：LLM -> Tool Call -> Dispatcher -> Tool Result -> LLM -> Final Answer。

本阶段只验证“LLM 能否根据任务和 Tool Result 动态决定下一步”，不实现：
Planner / 长期记忆 / 多 Agent / 反思 / 自我评估 / 任何写操作。

关键约束：
- 只允许调用 agent_dispatcher 白名单里的工具（两个只读 + 受确认保护的 save_plan）
- Agent 自己的 LLM 调用走本模块的 chat_with_tools()（原生 Ollama tools 参数），
  不调用 ai_service.call_ollama()，也不改动 ai_service 的行为
- 有预算上限：MAX_TOOL_CALLS / MAX_LOOP_TURNS
- 一轮里模型可能一次请求多个工具，按顺序派发，每个都计入预算
"""

import json
import threading
import time

import llm_client

from agent_dispatcher import dispatch
from agent_schema import describe_tools, get_tool_schemas


MAX_TOOL_CALLS = 4
MAX_LOOP_TURNS = 3

# 整个 Agent Run 的墙钟预算（从进入 Loop 开始计时）
MAX_RUN_SECONDS = 120

STOP_FINAL_ANSWER = "final_answer"
STOP_TOOL_BUDGET = "tool_budget_exceeded"
STOP_TURN_BUDGET = "turn_budget_exceeded"
STOP_RUN_TIMEOUT = "run_timeout"
STOP_LLM_ERROR = "llm_error"
STOP_EMPTY_RESPONSE = "empty_response"

# 统一超时标识：既作为 chat_with_tools 的超时信号，也作为结果里的 error_type
AGENT_TIMEOUT = "agent_timeout"


def timeout_user_message(max_run_seconds=MAX_RUN_SECONDS):
    """超时后给用户的确定性说明（不包含任何知识库来源声明）。"""

    return (
        f"本次请求超时（超过 {max_run_seconds} 秒），已停止继续调用工具。"
        "请稍后重试，或把问题拆小一些。"
    )


class _Deadline:
    """一次 Agent Run 的绝对截止时间。

    clock 可注入，测试里用假时钟即可验证超时行为，不需要真的等 120 秒。
    """

    def __init__(self, seconds, clock):
        self.clock = clock
        self.seconds = seconds
        self.expires_at = clock() + seconds

    def remaining(self):
        return self.expires_at - self.clock()

    def expired(self):
        return self.remaining() <= 0


def _call_with_deadline(func, deadline):
    """独立执行边界：最多等待 deadline 剩余时间，返回 (value, timed_out)。

    超时后主 Loop 不再继续等待（后台线程是 daemon，会由自身的 read timeout 收尾）。
    这样即使单次 LLM / 单次 Tool 卡住，Agent Run 的墙钟时间也有硬上限。
    """

    if deadline is None:
        return func(), False

    remaining = deadline.remaining()

    if remaining <= 0:
        return None, True

    box = {}

    def target():
        try:
            box["value"] = func()
        except BaseException as error:  # noqa: BLE001 - 原样带回主线程再处理
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(remaining)

    if thread.is_alive():
        return None, True

    if "error" in box:
        raise box["error"]

    return box.get("value"), False


def build_system_prompt():
    """系统 Prompt：角色、目标、可用工具（来自 Schema）与行为规则。

    文案集中在 prompt_registry（任务 agent_system，支持版本切换）。
    """

    import prompt_registry

    return prompt_registry.render(
        "agent_system",
        tools=describe_tools(),
        max_tool_calls=MAX_TOOL_CALLS,
        max_loop_turns=MAX_LOOP_TURNS,
    )


SYSTEM_PROMPT = build_system_prompt()


def chat_with_tools(messages, tools=None, deadline=None):
    """本模块唯一的 LLM 边界，返回 (assistant_message, error)。

    - 底层统一走 llm_client（Provider 由 LLM_PROVIDER 选择：ollama / openai_compatible），
      业务层不需要知道模型来自哪里。
    - 流式读取期间每个数据块之间都检查绝对 deadline，超过 Run 预算立即关闭连接。
    - deadline 为 None 时不限时（测试用桩替换整个函数）。
    - 超时返回 (None, AGENT_TIMEOUT)，调用方据此产生稳定的超时错误结构。
    """

    if deadline is not None and deadline.expired():
        return None, AGENT_TIMEOUT

    result, error = llm_client.chat(messages, tools=tools, deadline=deadline)

    if error:
        return None, error

    return result["message"], None


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


def _result(ok, final_answer, stopped_reason, error, state, steps, wrap_up_used=False, error_type=None):
    started = state.get("started_perf")

    return {
        "ok": ok,
        "final_answer": final_answer,
        "stopped_reason": stopped_reason,
        "error": error,
        "error_type": error_type,
        "tool_calls": state["tool_calls"],
        "loop_turns": state["loop_turns"],
        "wrap_up_used": wrap_up_used,
        "steps": steps,
        "messages": state["messages"],
        # 端到端耗时（用户请求 → 最终回答/草案），供评测与可观测使用
        "elapsed_seconds": round(time.perf_counter() - started, 3) if started else None,
    }


def _timeout_result(state, steps, verbose, max_run_seconds):
    """Run 墙钟预算耗尽时的稳定错误结构（区别于“知识库没有相关内容”）。"""

    message = f"Agent run exceeded {max_run_seconds} seconds"

    _log(verbose, "[Agent]", f"{message}（已停止继续调用工具）")

    return _result(
        False,
        timeout_user_message(max_run_seconds),
        STOP_RUN_TIMEOUT,
        message,
        state,
        steps,
        error_type=AGENT_TIMEOUT,
    )


def _wrap_up(state, verbose, deadline=None):
    """预算耗尽时的收尾：不带 tools 再问一次，让模型用已有信息给出回答。

    - 保留这个机制：实测模型在工具报错时会反复重试工具，直接返回“预算超限”用户什么都看不到。
    - 收尾调用同样受整个 Run 的 deadline 约束，不会额外启动一个无时限请求。
    """

    if deadline is not None and deadline.expired():
        _log(verbose, "[Agent]", "Run 预算已用尽，跳过收尾调用")

        return None

    _log(verbose, "[Agent]", "预算用尽，改用不带工具的收尾调用")

    llm_result, timed_out = _call_with_deadline(
        lambda: chat_with_tools(state["messages"], None, deadline),
        deadline,
    )

    if timed_out:
        return None

    message, error = llm_result

    if error or not message:
        return None

    content = (message.get("content") or "").strip()

    if not content:
        return None

    state["messages"].append({"role": "assistant", "content": content})

    _log(verbose, "[Agent]", f"Final Answer:\n{content}")

    return content


def run_agent(
    user_message,
    max_tool_calls=MAX_TOOL_CALLS,
    max_loop_turns=MAX_LOOP_TURNS,
    max_run_seconds=MAX_RUN_SECONDS,
    clock=None,
    verbose=True,
):
    """跑一次只读 Agent Loop，返回结构化运行结果。

    参数：
    - max_tool_calls / max_loop_turns：工具次数与轮次上限
    - max_run_seconds：整个 Run 的墙钟预算（从进入 Loop 开始计时），默认 120 秒
    - clock：可注入的单调时钟，测试用假时钟即可验证超时，不必真的等待

    返回：
    {
      "ok": bool,                    # 是否拿到了最终回答
      "final_answer": str | None,
      "stopped_reason": "final_answer" | "tool_budget_exceeded" | "turn_budget_exceeded"
                        | "run_timeout" | "llm_error" | "empty_response",
      "error": str | None,
      "error_type": "agent_timeout" | None,
      "tool_calls": int,             # 实际执行的工具次数（不会超过 max_tool_calls）
      "loop_turns": int,             # 工具决策轮数（不会超过 max_loop_turns）
      "wrap_up_used": bool,          # 是否在预算耗尽后用了“不带工具的收尾调用”
      "steps": [{"type": "tool_call", "tool", "arguments", "ok", "error_type", "result"}],
      "messages": [...],             # 完整消息历史（含 assistant tool_call 与 role=tool）
    }
    """

    clock = clock if clock is not None else time.monotonic
    run_started = time.perf_counter()

    deadline = _Deadline(max_run_seconds, clock)

    state = {
        "user_message": user_message,
        "started_perf": run_started,
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
        # 每一步之前都检查墙钟 deadline，而不是只看轮次
        if deadline.expired():
            return _timeout_result(state, steps, verbose, max_run_seconds)

        state["loop_turns"] += 1

        llm_result, timed_out = _call_with_deadline(
            lambda: chat_with_tools(state["messages"], get_tool_schemas(), deadline),
            deadline,
        )

        if timed_out:
            return _timeout_result(state, steps, verbose, max_run_seconds)

        message, error = llm_result

        if error == AGENT_TIMEOUT:
            return _timeout_result(state, steps, verbose, max_run_seconds)

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

            answer = _wrap_up(state, verbose, deadline)

            if answer:
                return _result(True, answer, STOP_FINAL_ANSWER, None, state, steps, wrap_up_used=True)

            if deadline.expired():
                return _timeout_result(state, steps, verbose, max_run_seconds)

            return _result(False, None, STOP_TOOL_BUDGET, budget_error, state, steps)

        # 把模型的 tool_call 原样放进历史，下一轮模型才能看到自己调用过什么
        state["messages"].append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls"),
        })

        for call in calls:
            # 超时后不再调用任何 Tool
            if deadline.expired():
                return _timeout_result(state, steps, verbose, max_run_seconds)

            _log(verbose, "[Agent]", f"Decision:\n{call['name']}")
            _log(verbose, "[Tool]", f"Arguments:\n{json.dumps(call['arguments'], ensure_ascii=False)}")

            outcome, timed_out = _call_with_deadline(
                lambda call=call: dispatch({"name": call["name"], "arguments": call["arguments"]}),
                deadline,
            )

            if timed_out:
                return _timeout_result(state, steps, verbose, max_run_seconds)

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

    answer = _wrap_up(state, verbose, deadline)

    if answer:
        return _result(True, answer, STOP_FINAL_ANSWER, None, state, steps, wrap_up_used=True)

    if deadline.expired():
        return _timeout_result(state, steps, verbose, max_run_seconds)

    return _result(False, None, STOP_TURN_BUDGET, turn_error, state, steps)
