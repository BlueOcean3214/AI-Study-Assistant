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

import requests

from ai_service import OLLAMA_MODEL, OLLAMA_URL

from agent_dispatcher import dispatch
from agent_schema import describe_tools, get_tool_schemas


MAX_TOOL_CALLS = 4
MAX_LOOP_TURNS = 3

# 整个 Agent Run 的墙钟预算（从进入 Loop 开始计时）
MAX_RUN_SECONDS = 120

# 单次 HTTP 调用的连接/读取间隔超时。
# 注意：requests 的 read timeout 是“两次读取之间”的超时，服务端持续滴流时
# 总耗时不会被它限制，所以它不能当成 Run 的总预算——真正的总预算由 _Deadline 负责。
OLLAMA_CONNECT_TIMEOUT = 10
OLLAMA_READ_TIMEOUT = 30

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
    """系统 Prompt：角色、目标、可用工具（来自 Schema）与行为规则。"""

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
9. 除 save_plan 外，当前阶段禁止修改任何用户数据。
10. save_plan 只能用于保存用户已经明确确认的计划：confirmation_id 必须来自"用户确认后由系统签发"的真实凭证；你自己生成的 confirmation_id 或 confirmed 类参数不构成用户确认。
11. 没有有效 confirmation_id 时不要调用 save_plan；如果保存被拒绝（例如 confirmation_required），如实告诉用户需要先确认计划，不要编造凭证重试。
12. 对于通用概念问题（例如“什么是 Embedding”），如果用户只要求简短解释、没有要求依据知识库，而你自己就能回答，就直接回答，不要调用 search_knowledge。
13. 但如果用户明确要求“根据知识库”“结合我的资料”“在知识库中查找”，或者问题明显依赖本项目的专有资料（例如知识库里的具体学习步骤），则仍然必须调用 search_knowledge。
14. 不要为了调用工具而调用工具：先判断这个问题需不需要外部信息，再决定是否调用。
15. 来源完整性：用户明确要求“根据知识库”“根据我的资料”“知识库里说什么”时，必须真实调用 search_knowledge 取得资料；在没有 Tool Result 的情况下，不得声称答案来自知识库、资料或检索结果。
16. 如果 search_knowledge 调用失败，必须明确告诉用户知识库检索当前不可用；不得把自己的模型知识说成知识库内容。
"""


SYSTEM_PROMPT = build_system_prompt()


def chat_with_tools(messages, tools=None, deadline=None):
    """本模块唯一的 LLM 边界，返回 (assistant_message, error)。

    - 使用流式读取（Ollama NDJSON）：可以在每个数据块之间检查绝对 deadline，
      一旦超过 Run 预算就立即关闭连接，而不是无限等待滴流响应。
    - deadline 为 None 时不限时（测试用桩替换整个函数）。
    - 超时返回 (None, AGENT_TIMEOUT)，调用方据此产生稳定的超时错误结构。
    """

    if deadline is not None and deadline.expired():
        return None, AGENT_TIMEOUT

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "think": False,
    }

    if tools:
        payload["tools"] = tools

    read_timeout = OLLAMA_READ_TIMEOUT

    if deadline is not None:
        # 单次静默等待也不会超过剩余预算
        read_timeout = max(1, min(OLLAMA_READ_TIMEOUT, deadline.remaining()))

    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            stream=True,
            timeout=(OLLAMA_CONNECT_TIMEOUT, read_timeout),
        )
    except requests.exceptions.Timeout:
        return None, "Ollama 请求超时"
    except requests.exceptions.RequestException as error:
        return None, f"无法连接 Ollama（{type(error).__name__}）"

    try:
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        response.close()
        return None, f"Ollama HTTP 错误（{type(error).__name__}）"

    content_parts = []
    tool_calls = []

    try:
        for line in response.iter_lines(decode_unicode=True):
            # 每个数据块之间检查绝对 deadline：滴流响应也无法突破总预算
            if deadline is not None and deadline.expired():
                return None, AGENT_TIMEOUT

            if not line:
                continue

            try:
                chunk = json.loads(line)
            except ValueError:
                continue

            message = chunk.get("message") or {}

            if message.get("content"):
                content_parts.append(message["content"])

            # 流式响应可能把多个 tool_call 分散在不同数据块里，必须累积而不是覆盖
            for item in message.get("tool_calls") or []:
                identifier = item.get("id")

                if identifier is not None and any(
                    existing.get("id") == identifier for existing in tool_calls
                ):
                    continue

                tool_calls.append(item)

            if chunk.get("done"):
                break
    except requests.exceptions.Timeout:
        return None, "Ollama 请求超时"
    except requests.exceptions.RequestException as error:
        return None, f"Ollama 连接中断（{type(error).__name__}）"
    finally:
        response.close()

    assistant_message = {
        "role": "assistant",
        "content": "".join(content_parts),
    }

    if tool_calls:
        assistant_message["tool_calls"] = tool_calls

    return assistant_message, None


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
    deadline = _Deadline(max_run_seconds, clock)

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
