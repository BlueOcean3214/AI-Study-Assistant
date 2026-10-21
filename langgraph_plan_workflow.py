"""LangGraph 计划草案工作流（实验路径，与原生 agent_plan_draft.run_plan_draft 对照）。

图结构（State / Node / Conditional Edge）：

    START -> research（只读研究：复用 agent_loop.run_agent）
          -> prepare（程序推导困难状态 + 收集真实 Tool Result + 构造起草消息）
          -> draft（起草：复用 DRAFT_SYSTEM_PROMPT 与统一模型层）
          -> validate（程序校验：normalize_plan + validate_plan_for_save + check_grounding）
          -> [条件边] 通过 -> finish_ok
                       修正预算未尽 -> correct（追加修正消息）-> draft
                       预算尽 -> finish_error

安全边界（与原生路径一致）：
- 整条图零写操作：研究阶段只读工具面，起草不给工具
- 校验全部由程序执行（Pydantic/结构白名单 + 业务规则 + 来源依据），模型声明无效
- 有限修正（MAX_CORRECTIONS）+ 墙钟超时保护（研究/起草双预算）
- 绝不返回未通过校验的 plan；结果结构与 run_plan_draft 保持一致（多一个 engine 字段）

引擎选择：PLAN_DRAFT_ENGINE=langgraph 时 main./plan/draft 走本模块（默认 native）。
"""

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

import agent_loop
import agent_plan_draft
import agent_tools
import plan_service


ENGINE_NAME = "langgraph"


class DraftState(TypedDict, total=False):
    user_message: str
    max_corrections: int
    research: dict
    steps: list
    difficult: bool
    history_error: Any
    feedback_result: Any
    knowledge_results: Any
    search_failed: bool
    messages: list
    corrections_used: int
    last_reason: str | None
    last_raw: str | None
    result: dict


# ==========================
# 节点（复用既有服务，不重复实现业务规则）
# ==========================


def node_research(state: DraftState) -> DraftState:
    """只读研究节点：复用 agent_loop.run_agent（工具白名单/预算/超时全部继承）。"""

    research_message = (
        f"{state['user_message']}\n\n"
        "（系统任务：用户需要一份学习计划草案。请先调用 get_recent_feedback 获取最近学习反馈；"
        "如需学习方法参考，再调用 search_knowledge 查询知识库；"
        "然后用几句话总结你获得的关键事实。）"
    )

    research = agent_loop.run_agent(
        research_message,
        max_run_seconds=agent_loop.MAX_RUN_SECONDS,
        verbose=False,
    )

    return {"research": research, "steps": research.get("steps") or []}


def node_prepare(state: DraftState) -> DraftState:
    """准备节点：程序推导困难状态（fail-closed），收集真实 Tool Result，构造起草消息。"""

    research = state["research"]
    steps = state["steps"]

    if not research.get("ok"):
        return {}

    difficult, history_error = plan_service.derive_difficult_previous_task()

    if history_error:
        return {"history_error": history_error}

    feedback_result, knowledge_results, search_failed = agent_plan_draft._collect_tool_data(steps)

    messages = [
        {"role": "system", "content": agent_plan_draft.DRAFT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": agent_plan_draft._build_draft_user_content(
                state["user_message"], feedback_result, knowledge_results, search_failed, difficult
            ),
        },
    ]

    return {
        "difficult": difficult,
        "feedback_result": feedback_result,
        "knowledge_results": knowledge_results,
        "search_failed": search_failed,
        "messages": messages,
        "corrections_used": state.get("corrections_used", 0),
        "last_reason": None,
        "last_raw": None,
    }


def node_draft(state: DraftState) -> DraftState:
    """起草节点：调用统一模型层（无工具），只记录原始输出，校验交给 validate 节点。"""

    deadline = agent_loop._Deadline(agent_plan_draft.DRAFT_MAX_RUN_SECONDS, time.monotonic)

    value, timed_out = agent_loop._call_with_deadline(
        lambda: agent_loop.chat_with_tools(state["messages"], None, deadline),
        deadline,
    )

    if timed_out or deadline.expired():
        return {"last_raw": None, "last_reason": "draft_timeout"}

    message, error = value

    if error == agent_loop.AGENT_TIMEOUT:
        return {"last_raw": None, "last_reason": "draft_timeout"}

    if error:
        return {"last_raw": None, "last_reason": f"llm_error: {error}"}

    content = (message or {}).get("content") or ""

    if message.get("tool_calls"):
        return {"last_raw": content, "last_reason": "起草阶段不允许调用工具"}

    return {"last_raw": content, "last_reason": None}


def node_validate(state: DraftState) -> DraftState:
    """校验节点：结构白名单 + 业务规则 + 来源依据，全部程序执行。"""

    if state.get("last_reason"):
        return {}

    content = state.get("last_raw") or ""
    parsed = agent_plan_draft._extract_json(content)

    if not isinstance(parsed, dict):
        return {"last_reason": "返回内容不是合法 JSON 或为空"}

    answer = parsed.get("answer")
    plan_raw = parsed.get("plan")

    if not isinstance(answer, str) or not answer.strip():
        return {"last_reason": "缺少非空的 answer 字段"}
    if not isinstance(plan_raw, dict):
        return {"last_reason": "缺少 plan 对象"}

    normalized, structure_error = plan_service.normalize_plan(plan_raw)

    if structure_error:
        return {"last_reason": structure_error}

    validation = agent_tools.validate_plan_for_save(normalized)

    if not validation["valid"]:
        return {"last_reason": validation["reason"]}

    violations = agent_plan_draft.check_grounding(
        [answer, normalized.get("reason")], state["steps"]
    )

    if violations:
        return {"last_reason": "；".join(violations)}

    return {"result": {
        "ok": True,
        "type": agent_plan_draft.TYPE_PLAN_DRAFT,
        "answer": answer.strip(),
        "plan": normalized,
        "difficult_previous_task": state.get("difficult"),
        "corrections_used": state.get("corrections_used", 0),
        "tool_calls": state["research"].get("tool_calls"),
        "loop_turns": state["research"].get("loop_turns"),
        "steps": state["steps"],
        "research_final_answer": state["research"].get("final_answer"),
        "stopped_reason": agent_plan_draft.STOP_PLAN_DRAFT_READY,
        "engine": ENGINE_NAME,
    }}


def node_correct(state: DraftState) -> DraftState:
    """修正节点：把失败原因作为修正消息追加进对话（有限次数）。"""

    messages = list(state["messages"])
    messages.append({"role": "assistant", "content": state.get("last_raw") or ""})
    messages.append({"role": "user", "content": agent_plan_draft._correction_message(state["last_reason"])})

    return {
        "messages": messages,
        "corrections_used": state.get("corrections_used", 0) + 1,
    }


def node_finish_error(state: DraftState) -> DraftState:
    """结束节点（失败）：复用原生路径的稳定错误结构，绝不返回未校验的 plan。"""

    research = state.get("research") or {}
    reason = state.get("last_reason")

    if reason == "draft_timeout":
        result = agent_plan_draft._draft_timeout_error(
            agent_plan_draft.DRAFT_MAX_RUN_SECONDS,
            state.get("corrections_used", 0),
            state["steps"],
            state.get("last_raw"),
        )
    elif reason and reason.startswith("llm_error"):
        result = agent_plan_draft._plan_error(
            agent_plan_draft.ERROR_LLM,
            f"起草阶段 LLM 调用失败：{reason}",
            agent_plan_draft.STOP_PLAN_LLM_ERROR,
            state.get("corrections_used", 0),
            state["steps"],
            state.get("last_raw"),
            research.get("tool_calls", 0),
            research.get("loop_turns", 0),
        )
    else:
        result = agent_plan_draft._plan_error(
            agent_plan_draft.ERROR_VALIDATION,
            f"计划草案经过 {state.get('corrections_used', 0)} 次修正仍未通过校验：{reason}",
            agent_plan_draft.STOP_PLAN_VALIDATION_FAILED,
            state.get("corrections_used", 0),
            state["steps"],
            state.get("last_raw"),
            research.get("tool_calls", 0),
            research.get("loop_turns", 0),
        )

    result["engine"] = ENGINE_NAME

    return {"result": result}


# ==========================
# 条件边
# ==========================


def route_after_prepare(state: DraftState) -> str:
    if state.get("history_error"):
        return "finish_error"
    if not (state.get("research") or {}).get("ok"):
        return "finish_error"
    return "draft"


def route_after_draft(state: DraftState) -> str:
    if state.get("last_reason") in {"draft_timeout"} or (state.get("last_reason") or "").startswith("llm_error"):
        return "finish_error"
    return "validate"


def route_after_validate(state: DraftState) -> str:
    if state.get("result"):
        return "finish_ok"
    if state.get("corrections_used", 0) < state.get("max_corrections", agent_plan_draft.MAX_CORRECTIONS):
        return "correct"
    return "finish_error"


def node_finish_ok(state: DraftState) -> DraftState:
    return {"result": state["result"]}


def node_finish_prepare_error(state: DraftState) -> DraftState:
    """研究/历史失败的结束路径（复用原生错误结构）。"""

    research = state.get("research") or {}
    steps = state.get("steps") or []

    if state.get("history_error"):
        result = agent_plan_draft._plan_error(
            agent_plan_draft.ERROR_HISTORY_UNAVAILABLE,
            f"无法读取最近的学习反馈，暂不能起草计划：{state['history_error']}",
            agent_plan_draft.STOP_PLAN_HISTORY_UNAVAILABLE,
            0, steps, None,
            research.get("tool_calls", 0), research.get("loop_turns", 0),
        )
    else:
        result = agent_plan_draft._research_error(research, steps)

    result["engine"] = ENGINE_NAME

    return {"result": result}


# ==========================
# 图构建与运行入口
# ==========================


def build_graph():
    """构建 LangGraph 状态图（每次调用新建，无共享状态）。"""

    graph = StateGraph(DraftState)

    graph.add_node("research", node_research)
    graph.add_node("prepare", node_prepare)
    graph.add_node("draft", node_draft)
    graph.add_node("validate", node_validate)
    graph.add_node("correct", node_correct)
    graph.add_node("finish_ok", node_finish_ok)
    graph.add_node("finish_error", node_finish_error)
    graph.add_node("finish_prepare_error", node_finish_prepare_error)

    graph.add_edge(START, "research")
    graph.add_edge("research", "prepare")
    graph.add_conditional_edges("prepare", route_after_prepare, {
        "draft": "draft",
        "finish_error": "finish_prepare_error",
    })
    graph.add_conditional_edges("draft", route_after_draft, {
        "validate": "validate",
        "finish_error": "finish_error",
    })
    graph.add_conditional_edges("validate", route_after_validate, {
        "finish_ok": "finish_ok",
        "correct": "correct",
        "finish_error": "finish_error",
    })
    graph.add_edge("correct", "draft")
    graph.add_edge("finish_ok", END)
    graph.add_edge("finish_error", END)
    graph.add_edge("finish_prepare_error", END)

    return graph.compile()


def run_plan_draft_graph(user_message, max_corrections=agent_plan_draft.MAX_CORRECTIONS):
    """LangGraph 版计划草案入口：结果结构与 run_plan_draft 一致（多 engine 字段）。

    绝不写任何数据；校验与安全约束与原生路径完全一致。
    """

    if not isinstance(user_message, str) or not user_message.strip():
        return {
            "ok": False,
            "type": agent_plan_draft.TYPE_PLAN_ERROR,
            "error_type": agent_plan_draft.ERROR_LLM,
            "message": "user_message 必须是非空字符串",
            "corrections_used": 0,
            "steps": [],
            "stopped_reason": agent_plan_draft.STOP_PLAN_LLM_ERROR,
            "engine": ENGINE_NAME,
        }

    app = build_graph()

    state = app.invoke({
        "user_message": user_message.strip(),
        "max_corrections": max_corrections,
        "corrections_used": 0,
    })

    result = state.get("result") or {
        "ok": False,
        "type": agent_plan_draft.TYPE_PLAN_ERROR,
        "error_type": agent_plan_draft.ERROR_LLM,
        "message": "图执行未产生结果",
        "corrections_used": state.get("corrections_used", 0),
        "steps": state.get("steps") or [],
        "stopped_reason": agent_plan_draft.STOP_PLAN_LLM_ERROR,
        "engine": ENGINE_NAME,
    }

    return result


def main():
    """脚本入口：python langgraph_plan_workflow.py "你的需求"。"""

    import json
    import sys

    question = " ".join(sys.argv[1:]).strip() or "根据我最近的学习情况，制定明天的学习计划草案。"

    result = run_plan_draft_graph(question)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
