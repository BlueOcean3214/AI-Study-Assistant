"""Agent Plan Draft：让只读 Agent 生成"经过程序验证"的结构化学习计划草案。

本阶段目标：
    Agent 读取真实反馈与知识库后，能产出一个通过程序校验的 plan 草案，
    但草案只返回给用户/程序查看，**绝不保存**。

流程（复用现有设施，不重构 Agent Loop）：
    1. 研究阶段：复用 agent_loop.run_agent（白名单仍是 get_recent_feedback /
       search_knowledge 两个只读工具），收集真实 Tool Result
    2. 起草阶段：程序把真实 Tool Result 组装进 Prompt，LLM 输出
       {"answer": 自然语言, "plan": 结构化计划} JSON
    3. 校验阶段：plan_service.normalize_plan（结构白名单）
       -> agent_tools.validate_plan_for_save（复用 ai_service.validate_plan，
          difficult_previous_task 由程序从真实反馈推导，Agent 不可指定）
       -> 来源 grounding 检查（没有真实 Tool Result 不得声称对应来源）
    4. 修正：校验/grounding 失败时把具体原因返回给 LLM 重新生成，最多 2 次
    5. 仍失败 -> plan_error（绝不把未通过校验的 plan 当正常结果返回）

安全边界（本阶段硬约束）：
    - 不新增 Agent Tool：工具面保持 get_recent_feedback / search_knowledge
    - 不调用 plan_service.save_plan，不写 plans / feedbacks / knowledge
    - 起草阶段不给 LLM 任何工具（tools=None），不存在写入口
    - validate_plan_for_save 只读校验；plan_draft 不等于已保存
"""

import json
import time

import ai_service
import agent_loop
import agent_tools
import plan_service


# 草案最多修正次数（首次生成 + 最多 2 次修正 = 最多 3 次起草调用）
MAX_CORRECTIONS = 2

# 起草阶段的独立墙钟预算（研究阶段沿用 run_agent 自己的预算）。
# 草案需要一次性生成 answer + 完整 plan JSON，输出较长，
# 本地小模型（CPU 推理）生成速度有限，预算按实测留足余量。
DRAFT_MAX_RUN_SECONDS = 240

# 结果类型
TYPE_PLAN_DRAFT = "plan_draft"
TYPE_PLAN_ERROR = "plan_error"

# 稳定错误类型
ERROR_VALIDATION = "validation_error"
ERROR_HISTORY_UNAVAILABLE = "history_unavailable"
ERROR_LLM = "llm_error"
ERROR_RUN_TIMEOUT = "run_timeout"

# 稳定 stopped_reason
STOP_PLAN_DRAFT_READY = "plan_draft_ready"
STOP_PLAN_RESEARCH_FAILED = "plan_research_failed"
STOP_PLAN_VALIDATION_FAILED = "plan_validation_failed"
STOP_PLAN_RUN_TIMEOUT = "plan_run_timeout"
STOP_PLAN_HISTORY_UNAVAILABLE = "plan_history_unavailable"
STOP_PLAN_LLM_ERROR = "plan_llm_error"

# 来源诚实性：这些措辞等于"答案来自知识库/资料"
# （agent_eval.py 从这里导入，保持评测口径一致）
KNOWLEDGE_CLAIM_MARKERS = (
    "根据我的知识库",
    "根据知识库",
    "根据我的资料",
    "根据资料",
    "知识库中显示",
    "知识库显示",
    "知识库中查到",
    "检索结果显示",
)

# 这些措辞等于"答案来自真实学习反馈"
FEEDBACK_CLAIM_MARKERS = (
    "根据你最近的反馈",
    "根据你的反馈",
    "根据最近反馈",
    "根据反馈",
    "根据历史反馈",
    "根据你的历史反馈",
    "根据学习反馈",
    "根据你的学习记录",
    "反馈数据显示",
)

# 最后一次模型原始输出在错误结果里最多保留的字符数
MAX_LAST_RAW_CHARS = 2000

DRAFT_SYSTEM_PROMPT = """你是 AI Study Assistant 的学习计划起草模块。

任务：依据程序在用户消息中提供的真实数据（最近学习反馈、知识库检索结果），起草一份结构化学习计划草案。草案只交给程序校验，当前阶段不会保存，也不会写入任何数据。

输出要求：
- 只输出一个 JSON 对象，不要 Markdown、不要解释文字。
- 外层结构：{"answer": "给用户阅读的自然语言说明", "plan": {计划对象}}。
- plan 必须包含用户消息中要求的全部字段，并严格遵守其中的数值规则。

数值一致性（最常见的校验失败点，输出前务必逐条自查）：
1. 所有 minutes / question_count / estimated_minutes 必须是非负整数。
2. 子任务的 minutes 之和必须不超过 estimated_minutes。
3. 子任务的 question_count 之和必须等于顶层 question_count。
4. completion_criteria 必须用数字明确写出与 question_count 一致的题数（例如"完成4道指定题目"）；question_count 为 0 时不要写任何题数。

来源完整性（硬规则）：
1. 只能依据用户消息中程序提供的真实数据。
2. 没有真实知识库检索结果时，answer 与 plan.reason 不得声称依据知识库或资料。
3. 知识库检索失败时，必须如实说明检索不可用，不得假装检索成功。
4. 没有真实反馈数据时，不得声称"根据你最近的反馈"。
"""


def _log(verbose, tag, message):
    if verbose:
        print(f"{tag}\n{message}\n")


def _claims_source(text, markers):
    return isinstance(text, str) and any(marker in text for marker in markers)


def check_grounding(texts, steps):
    """来源完整性检查：texts 里不得声称没有真实依据的来源。

    steps 是研究阶段的真实 Tool Call 记录。规则：
    - 声称依据知识库 -> 必须存在成功的 search_knowledge 且检索到内容
    - 声称依据学习反馈 -> 必须存在成功的 get_recent_feedback

    返回违规说明列表（空列表表示通过）。
    """

    feedback_ok = any(
        step.get("tool") == "get_recent_feedback" and step.get("ok") is True
        for step in steps
    )
    search_ok = any(
        step.get("tool") == "search_knowledge"
        and step.get("ok") is True
        and isinstance(step.get("result"), dict)
        and step["result"].get("count", 0) > 0
        for step in steps
    )

    violations = []

    for text in texts:
        if not isinstance(text, str):
            continue

        if not search_ok and _claims_source(text, KNOWLEDGE_CLAIM_MARKERS):
            violation = "声称依据知识库，但本次没有成功的知识库检索结果"

            if violation not in violations:
                violations.append(violation)

        if not feedback_ok and _claims_source(text, FEEDBACK_CLAIM_MARKERS):
            violation = "声称依据学习反馈，但本次没有成功读取学习反馈"

            if violation not in violations:
                violations.append(violation)

    return violations


def _extract_json(text):
    """从模型输出里提取 JSON 对象（容忍 Markdown 围栏与前后杂文）。"""

    value = (text or "").strip()

    if value.startswith("```"):
        first_newline = value.find("\n")

        if first_newline != -1:
            value = value[first_newline + 1:]

        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]

        value = value.strip()

    try:
        return json.loads(value)
    except ValueError:
        pass

    start = value.find("{")
    end = value.rfind("}")

    if start != -1 and end > start:
        try:
            return json.loads(value[start:end + 1])
        except ValueError:
            return None

    return None


def _collect_tool_data(steps):
    """从研究阶段真实 steps 中收集起草依据。

    返回 (feedback_result, knowledge_results, search_failed)：
    - feedback_result：最后一次成功的 get_recent_feedback 完整结果（None 表示没有）
    - knowledge_results：所有成功 search_knowledge 返回的内容条目（合并）
    - search_failed：是否出现过失败的 search_knowledge
    """

    feedback_result = None
    knowledge_results = []
    search_failed = False

    for step in steps:
        if step.get("tool") == "get_recent_feedback" and step.get("ok") is True:
            feedback_result = step.get("result")
        elif step.get("tool") == "search_knowledge":
            result = step.get("result")

            # search_knowledge 的失败以结构化结果返回（dispatch 层 ok=True，
            # result.ok=False），必须区分"检索成功"与"检索失败"
            if (
                step.get("ok") is True
                and isinstance(result, dict)
                and result.get("ok") is not False
            ):
                knowledge_results.extend(result.get("results") or [])
            else:
                search_failed = True

    return feedback_result, knowledge_results, search_failed


def _knowledge_block(knowledge_results, search_failed):
    if knowledge_results:
        return (
            "真实知识库检索结果（来自 search_knowledge 的真实结果，可作为计划依据）：\n"
            + json.dumps(knowledge_results, ensure_ascii=False, indent=2)
        )

    if search_failed:
        return (
            "真实知识库检索状态：本次知识库检索失败，没有可用结果。\n"
            "answer 与 plan.reason 不得声称依据知识库；如需提及，必须如实说明检索不可用。"
        )

    return (
        "真实知识库检索状态：本次没有知识库检索结果。\n"
        "answer 与 plan.reason 不得声称依据知识库或资料。"
    )


def _build_draft_user_content(
    user_message, feedback_result, knowledge_results, search_failed, difficult
):
    """起草阶段的用户消息：真实数据 + 复用现有计划规则 + 输出契约。

    计划字段与数值规则复用 ai_service.build_plan_prompt（与 /next-plan 同一份规则文本），
    不新建第二套 plan 规则；真正的校验由 normalize_plan + validate_plan 负责。
    """

    feedback_list = (feedback_result or {}).get("feedback") or []
    summary = (feedback_result or {}).get("summary") or {}

    base = ai_service.build_plan_prompt(feedback_list, summary, difficult, None)
    knowledge = _knowledge_block(knowledge_results, search_failed)

    wrapper = (
        "输出契约：只返回一个 JSON 对象，外层包含 answer 与 plan 两个字段：\n"
        '{"answer": "给用户阅读的自然语言说明", "plan": {上面要求的计划 JSON}}\n'
        "answer 与 plan.reason 只能依据上面提供的真实数据。"
    )

    return f"用户请求：{user_message}\n\n{base}\n{knowledge}\n\n{wrapper}"


def _correction_message(reason):
    return (
        f"你刚才的输出未通过程序校验，原因：{reason}\n"
        "请重新输出完整的 JSON（外层包含 answer 与 plan 两个字段），修正上述问题。"
        "不要只回复解释，也不要输出 Markdown。"
    )


def _plan_error(
    error_type,
    message,
    stopped_reason,
    corrections_used,
    steps,
    last_raw,
    tool_calls=0,
    loop_turns=0,
):
    return {
        "ok": False,
        "type": TYPE_PLAN_ERROR,
        "error_type": error_type,
        "message": message,
        "corrections_used": corrections_used,
        "steps": steps or [],
        "tool_calls": tool_calls,
        "loop_turns": loop_turns,
        "last_draft_raw": (last_raw or "")[:MAX_LAST_RAW_CHARS],
        "stopped_reason": stopped_reason,
    }


def _research_error(research, steps):
    """研究阶段失败时，把 run_agent 的稳定停止原因映射成 plan_error。"""

    stopped = research.get("stopped_reason")
    tool_calls = research.get("tool_calls", 0)
    loop_turns = research.get("loop_turns", 0)

    if stopped == agent_loop.STOP_RUN_TIMEOUT:
        return _plan_error(
            ERROR_RUN_TIMEOUT,
            research.get("final_answer") or agent_loop.timeout_user_message(),
            STOP_PLAN_RUN_TIMEOUT,
            0,
            steps,
            None,
            tool_calls,
            loop_turns,
        )

    if stopped == agent_loop.STOP_LLM_ERROR:
        return _plan_error(
            ERROR_LLM,
            f"研究阶段 LLM 调用失败：{research.get('error')}",
            STOP_PLAN_RESEARCH_FAILED,
            0,
            steps,
            None,
            tool_calls,
            loop_turns,
        )

    return _plan_error(
        stopped or "unknown",
        f"研究阶段未能完成：{research.get('error') or stopped}",
        STOP_PLAN_RESEARCH_FAILED,
        0,
        steps,
        None,
        tool_calls,
        loop_turns,
    )


def _draft_timeout_error(draft_max_run_seconds, corrections_used, steps, last_raw):
    message = f"计划起草超时（超过 {draft_max_run_seconds} 秒），已停止。"

    _log(True, "[Draft]", message)

    return _plan_error(
        ERROR_RUN_TIMEOUT,
        message,
        STOP_PLAN_RUN_TIMEOUT,
        corrections_used,
        steps,
        last_raw,
    )


def run_plan_draft(
    user_message,
    max_corrections=MAX_CORRECTIONS,
    research_max_run_seconds=agent_loop.MAX_RUN_SECONDS,
    draft_max_run_seconds=DRAFT_MAX_RUN_SECONDS,
    clock=None,
    verbose=True,
):
    """跑一次"研究 -> 起草 -> 校验 -> 有限修正"的计划草案流程。

    参数：
    - max_corrections：校验失败后的最多修正次数（默认 2）
    - research_max_run_seconds：研究阶段（run_agent）的墙钟预算
    - draft_max_run_seconds：起草阶段的独立墙钟预算
    - clock：可注入的单调时钟（研究阶段与起草阶段共用）

    返回（成功）：
    {
      "ok": True,
      "type": "plan_draft",
      "answer": "给用户阅读的自然语言说明",
      "plan": {...已归一化、通过校验的 7 字段计划...},
      "difficult_previous_task": bool,   # 程序推导，非 Agent 声明
      "corrections_used": 0..max_corrections,
      "tool_calls": ..., "loop_turns": ..., "steps": [...],  # 研究阶段真实轨迹
      "research_final_answer": "...",
      "stopped_reason": "plan_draft_ready",
    }

    返回（失败）：
    {
      "ok": False,
      "type": "plan_error",
      "error_type": "validation_error" | "history_unavailable" | "llm_error"
                    | "run_timeout" | <研究阶段停止原因>,
      "message": "...",
      "corrections_used": ...,
      "steps": [...],
      "last_draft_raw": "...",       # 调试用：最后一次模型原始输出（截断）
      "stopped_reason": "plan_*",
    }

    绝不返回未通过校验的 plan；绝不写任何数据。
    """

    clock = clock if clock is not None else time.monotonic

    research_message = (
        f"{user_message}\n\n"
        "（系统任务：用户需要一份学习计划草案。请先调用 get_recent_feedback 获取最近学习反馈；"
        "如需学习方法参考，再调用 search_knowledge 查询知识库；"
        "然后用几句话总结你获得的关键事实。）"
    )

    _log(verbose, "[PlanDraft]", f"User:\n{user_message}")

    # 1) 研究阶段：复用现有只读 Agent Loop（工具面、预算、超时全部继承）
    research = agent_loop.run_agent(
        research_message,
        max_run_seconds=research_max_run_seconds,
        clock=clock,
        verbose=verbose,
    )

    steps = research.get("steps") or []

    if not research.get("ok"):
        _log(verbose, "[PlanDraft]", f"研究阶段失败：{research.get('stopped_reason')}")
        return _research_error(research, steps)

    # 2) 困难状态由程序依据真实反馈推导（fail-closed：读不到就拒绝起草）
    difficult, history_error = plan_service.derive_difficult_previous_task()

    if history_error:
        return _plan_error(
            ERROR_HISTORY_UNAVAILABLE,
            f"无法读取最近的学习反馈，暂不能起草计划：{history_error}",
            STOP_PLAN_HISTORY_UNAVAILABLE,
            0,
            steps,
            None,
            research.get("tool_calls", 0),
            research.get("loop_turns", 0),
        )

    # 3) 起草依据只来自研究阶段的真实 Tool Result
    feedback_result, knowledge_results, search_failed = _collect_tool_data(steps)

    deadline = agent_loop._Deadline(draft_max_run_seconds, clock)

    messages = [
        {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_draft_user_content(
                user_message, feedback_result, knowledge_results, search_failed, difficult
            ),
        },
    ]

    corrections_used = 0
    last_reason = None
    last_raw = None

    # 4) 起草 + 校验 + 有限修正（首次生成 + 最多 max_corrections 次修正）
    for attempt in range(max_corrections + 1):
        _log(
            verbose,
            "[Draft]",
            f"起草尝试 {attempt + 1}/{max_corrections + 1}",
        )

        value, timed_out = agent_loop._call_with_deadline(
            lambda: agent_loop.chat_with_tools(messages, None, deadline),
            deadline,
        )

        # 线程被截断，或调用虽完成但墙钟预算已耗尽（例如单次调用内部推进了时钟）
        if timed_out or deadline.expired():
            return _draft_timeout_error(
                draft_max_run_seconds, corrections_used, steps, last_raw
            )

        message, error = value

        if error == agent_loop.AGENT_TIMEOUT:
            return _draft_timeout_error(
                draft_max_run_seconds, corrections_used, steps, last_raw
            )

        if error:
            return _plan_error(
                ERROR_LLM,
                f"起草阶段 LLM 调用失败：{error}",
                STOP_PLAN_LLM_ERROR,
                corrections_used,
                steps,
                last_raw,
                research.get("tool_calls", 0),
                research.get("loop_turns", 0),
            )

        content = (message or {}).get("content") or ""
        last_raw = content

        reason = None

        if message.get("tool_calls"):
            reason = "起草阶段不允许调用工具"
        else:
            parsed = _extract_json(content)

            if not isinstance(parsed, dict):
                reason = "返回内容不是合法 JSON 或为空"
            else:
                answer = parsed.get("answer")
                plan_raw = parsed.get("plan")

                if not isinstance(answer, str) or not answer.strip():
                    reason = "缺少非空的 answer 字段"
                elif not isinstance(plan_raw, dict):
                    reason = "缺少 plan 对象"
                else:
                    # 结构白名单：复用 plan_service.normalize_plan，不新建第二套字段定义
                    normalized, structure_error = plan_service.normalize_plan(plan_raw)

                    if structure_error:
                        reason = structure_error
                    else:
                        # 规则校验：复用 validate_plan_for_save，
                        # difficult_previous_task 由程序内部推导，Agent 不可指定
                        validation = agent_tools.validate_plan_for_save(normalized)

                        if not validation["valid"]:
                            reason = validation["reason"]
                        else:
                            violations = check_grounding(
                                [answer, normalized.get("reason")], steps
                            )

                            if violations:
                                reason = "；".join(violations)

        if reason is None:
            # 走到这里说明结构、规则、来源全部通过
            _log(verbose, "[Draft]", f"Final Answer:\n{answer.strip()}")

            return {
                "ok": True,
                "type": TYPE_PLAN_DRAFT,
                "answer": answer.strip(),
                "plan": normalized,
                "difficult_previous_task": difficult,
                "corrections_used": corrections_used,
                "tool_calls": research.get("tool_calls"),
                "loop_turns": research.get("loop_turns"),
                "steps": steps,
                "research_final_answer": research.get("final_answer"),
                "stopped_reason": STOP_PLAN_DRAFT_READY,
            }

        last_reason = reason
        _log(verbose, "[Draft]", f"校验失败：{reason}")

        if attempt < max_corrections:
            corrections_used += 1
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": _correction_message(reason)})

    # 5) 修正次数用尽仍未通过：稳定错误，绝不返回未校验的 plan
    return _plan_error(
        ERROR_VALIDATION,
        f"计划草案经过 {corrections_used} 次修正仍未通过校验：{last_reason}",
        STOP_PLAN_VALIDATION_FAILED,
        corrections_used,
        steps,
        last_raw,
        research.get("tool_calls", 0),
        research.get("loop_turns", 0),
    )
