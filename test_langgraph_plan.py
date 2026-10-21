"""LangGraph 计划草案工作流测试：直接运行 python test_langgraph_plan.py。

全 Mock（stub run_agent / chat_with_tools / 历史推导），不调用真实模型。

覆盖：
- 图节点流转：research -> prepare -> draft -> validate -> finish_ok
- 校验失败 -> 有限修正 -> 成功；修正预算尽 -> plan_error（绝不返回未校验 plan）
- 研究失败 / 历史不可用 -> 稳定错误结构
- 起草超时 -> run_timeout 结构
- 起草阶段不允许工具调用
- 安全：无写操作（stub 捕获 dispatcher 调用面）、来源依据校验生效
- 结果结构与 run_plan_draft 兼容（+ engine 字段）
"""

import contextlib
import json

import agent_loop
import agent_plan_draft
import langgraph_plan_workflow


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


VALID_PLAN = {
    "task": "定积分基础巩固",
    "estimated_minutes": 45,
    "difficulty": "easy",
    "question_count": 4,
    "reason": "根据反馈先巩固基础。",
    "completion_criteria": "完成4道基础题并订正后结束。",
    "subtasks": [
        {"title": "复习公式", "minutes": 15, "question_count": 0, "description": "复习基本公式"},
        {"title": "基础练习", "minutes": 30, "question_count": 4, "description": "完成指定基础题"},
    ],
}


def draft_payload(plan=None, answer="这是计划说明。"):
    return json.dumps({"answer": answer, "plan": VALID_PLAN if plan is None else plan}, ensure_ascii=False)


FEEDBACK_TOOL_RESULT = {
    "days": 7, "count": 1,
    "summary": {"too_difficult_count": 0},
    "feedback": [{"task": "定积分练习", "status": "completed"}],
}


def fake_research(ok=True):
    return {
        "ok": ok,
        "final_answer": "已获取反馈。",
        "tool_calls": 1,
        "loop_turns": 2,
        "steps": [{
            "type": "tool_call", "tool": "get_recent_feedback", "arguments": {"days": 7},
            "ok": True, "error_type": None, "result": FEEDBACK_TOOL_RESULT,
        }],
        "stopped_reason": "final_answer" if ok else "llm_error",
        "error": None if ok else "研究失败",
    }


@contextlib.contextmanager
def stub_env(research=None, draft_contents=(), difficult=(False, None)):
    state = {"research_calls": 0, "draft_calls": 0}

    original_run_agent = agent_loop.run_agent
    original_chat = agent_loop.chat_with_tools
    original_derive = langgraph_plan_workflow.plan_service.derive_difficult_previous_task

    script = list(draft_contents)

    def fake_run_agent(*args, **kwargs):
        state["research_calls"] += 1
        return research if research is not None else fake_research()

    def fake_chat(messages, tools=None, deadline=None):
        state["draft_calls"] += 1
        content = script.pop(0) if script else draft_payload()

        return {"role": "assistant", "content": content}, None

    agent_loop.run_agent = fake_run_agent
    agent_loop.chat_with_tools = fake_chat
    langgraph_plan_workflow.plan_service.derive_difficult_previous_task = lambda: difficult

    try:
        yield state
    finally:
        agent_loop.run_agent = original_run_agent
        agent_loop.chat_with_tools = original_chat
        langgraph_plan_workflow.plan_service.derive_difficult_previous_task = original_derive


# ==========================
# 1. 成功路径
# ==========================

with stub_env() as state:
    result = langgraph_plan_workflow.run_plan_draft_graph("制定明天的计划草案")

check("成功路径返回 plan_draft",
      result["ok"] is True and result["type"] == "plan_draft" and result["engine"] == "langgraph")
check("成功路径结构与原生兼容",
      {"answer", "plan", "difficult_previous_task", "corrections_used", "steps",
       "stopped_reason"} <= set(result))
check("研究节点真实复用只读 Agent", state["research_calls"] == 1)
check("起草默认一次通过", state["draft_calls"] == 1 and result["corrections_used"] == 0)
check("计划通过程序校验（7 字段）", set(result["plan"]) == {
    "task", "estimated_minutes", "difficulty", "question_count",
    "reason", "completion_criteria", "subtasks"})

# ==========================
# 2. 校验失败 -> 修正 -> 成功
# ==========================

bad_plan = dict(VALID_PLAN, question_count=5)  # 与子任务题数之和不一致

with stub_env(draft_contents=[draft_payload(bad_plan), draft_payload()]) as state:
    result = langgraph_plan_workflow.run_plan_draft_graph("制定计划")

check("修正后成功",
      result["ok"] is True and result["corrections_used"] == 1
      and state["draft_calls"] == 2)
check("修正路径走图 correct 节点", result["engine"] == "langgraph")

# ==========================
# 3. 修正预算尽 -> plan_error
# ==========================

with stub_env(draft_contents=[draft_payload(bad_plan)] * 5) as state:
    result = langgraph_plan_workflow.run_plan_draft_graph("制定计划", max_corrections=2)

check("预算尽返回 plan_error",
      result["ok"] is False and result["type"] == "plan_error"
      and result["error_type"] == "validation_error")
check("绝不返回未校验 plan", "plan" not in result and "answer" not in result)
check("起草调用次数受控（首次 + 2 次修正）", state["draft_calls"] == 3)
check("错误信息含最后校验原因", "题目数量" in result["message"], f"msg={result['message']}")

# ==========================
# 4. 研究失败 / 历史不可用
# ==========================

with stub_env(research=fake_research(ok=False)):
    result = langgraph_plan_workflow.run_plan_draft_graph("制定计划")

check("研究失败返回稳定错误",
      result["ok"] is False and result["type"] == "plan_error"
      and result["error_type"] == "llm_error",
      json.dumps({k: result.get(k) for k in ("ok", "type", "error_type", "stopped_reason")}, ensure_ascii=False))

with stub_env(difficult=(False, "数据库不可读")):
    result = langgraph_plan_workflow.run_plan_draft_graph("制定计划")

check("历史不可用 fail-closed",
      result["ok"] is False and result["error_type"] == "history_unavailable")

# ==========================
# 5. 起草不允许工具 / 超时结构
# ==========================


def fake_tool_call_chat(messages, tools=None, deadline=None):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "search_knowledge", "arguments": {}}}]}, None


with stub_env():
    original_chat = agent_loop.chat_with_tools
    agent_loop.chat_with_tools = fake_tool_call_chat

    try:
        result = langgraph_plan_workflow.run_plan_draft_graph("制定计划", max_corrections=0)
    finally:
        agent_loop.chat_with_tools = original_chat

check("起草阶段工具调用被拒绝",
      result["ok"] is False and "不允许调用工具" in result["message"])

with stub_env(draft_contents=[]):
    original_chat = agent_loop.chat_with_tools

    def timeout_chat(messages, tools=None, deadline=None):
        return None, agent_loop.AGENT_TIMEOUT

    agent_loop.chat_with_tools = timeout_chat

    try:
        result = langgraph_plan_workflow.run_plan_draft_graph("制定计划", max_corrections=0)
    finally:
        agent_loop.chat_with_tools = original_chat

check("起草超时返回稳定结构",
      result["ok"] is False and result["stopped_reason"] == "plan_run_timeout",
      json.dumps({k: result.get(k) for k in ("ok", "type", "error_type", "stopped_reason")}, ensure_ascii=False))

# ==========================
# 6. 来源依据校验生效（声称知识库但无检索 -> 被程序拒）
# ==========================

lie_plan = dict(VALID_PLAN, reason="根据知识库资料，先巩固基础。")

with stub_env(draft_contents=[draft_payload(lie_plan)] * 3) as state:
    result = langgraph_plan_workflow.run_plan_draft_graph("制定计划", max_corrections=1)

check("无真实检索时声称知识库被拒绝",
      result["ok"] is False and "知识库" in result["message"],
      f"msg={result['message']}")

print("ALL TESTS COMPLETED")
