"""Agent Plan Draft 测试：直接运行 python test_agent_plan_draft.py。

分两部分：
- 确定性部分：替换 agent_loop.chat_with_tools 为脚本化 LLM，验证
  结构 / Validation / Grounding / Safety 四类契约（不需要聊天模型）
- 真实部分：用真实 qwen3.5:4b 跑 2 个草案场景，验证端到端闭环

隔离：反馈数据库与向量缓存都指向临时夹具；
结束时校验真实 feedback.db / plans 表 / knowledge/ / .rag_cache 未被改动。

本阶段硬约束（测试重点）：
- 草案流程只读：不写 plans / feedback / knowledge（save_plan 无凭证时也会被拒绝）
- 草案必须通过 normalize_plan + validate_plan 才能返回
- difficult_previous_task 由程序推导，Agent 不可指定
- 没有真实 Tool Result 时不得声称"根据知识库/反馈"
- 最多修正 2 次，仍失败返回稳定 plan_error
"""

import contextlib
import copy
import gc
import json
import shutil
import sqlite3
import time
from pathlib import Path

import agent_dispatcher
import agent_loop
import agent_plan_draft
import agent_schema
import agent_tools
import database
import embedding_service
import plan_service
import rag_service
import vector_cache


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_draft.db"
CACHE_FIXTURE = PROJECT_DIR / "_test_cache_draft"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"

MAX_CORRECTIONS = agent_plan_draft.MAX_CORRECTIONS


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


def tool_call(name, arguments, call_id="call_1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
    }


def multi_tool_call(*calls):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"call_{index}", "function": {"name": name, "arguments": arguments}}
            for index, (name, arguments) in enumerate(calls)
        ],
    }


def final(answer):
    return {"role": "assistant", "content": answer}


class ScriptedLLM:
    """按阶段分发的脚本化 LLM。

    - 消息首条等于 DRAFT_SYSTEM_PROMPT 视为起草阶段，否则视为研究阶段
    - 记录每阶段的消息快照、调用次数与 tools 参数
    - 桶条目可以是 message（返回 (message, None)）或 (message, error) 元组
    - 桶用尽后返回 fallback（默认一句非空回答）
    - on_call(messages, is_draft) 钩子可推进假时钟
    """

    def __init__(self, research=None, draft=None, fallback=None, on_call=None):
        self.research = list(research or [])
        self.draft = list(draft or [])
        self.fallback = fallback
        self.on_call = on_call
        self.research_calls = 0
        self.draft_calls = 0
        self.research_tools = []
        self.draft_tools = []
        self.research_seen = []
        self.draft_seen = []

    def __call__(self, messages, tools=None, deadline=None):
        is_draft = bool(messages) and messages[0].get("content") == agent_plan_draft.DRAFT_SYSTEM_PROMPT

        if self.on_call is not None:
            self.on_call(messages, is_draft)

        if is_draft:
            self.draft_calls += 1
            self.draft_tools.append(tools)
            self.draft_seen.append(copy.deepcopy(messages))
            bucket, index = self.draft, self.draft_calls
        else:
            self.research_calls += 1
            self.research_tools.append(tools)
            self.research_seen.append(copy.deepcopy(messages))
            bucket, index = self.research, self.research_calls

        if index <= len(bucket):
            item = bucket[index - 1]
        else:
            item = self.fallback if self.fallback is not None else final("（脚本已用尽）")

        if isinstance(item, tuple):
            return item

        return item, None


@contextlib.contextmanager
def stub_llm(fake):
    original = agent_loop.chat_with_tools
    agent_loop.chat_with_tools = fake
    try:
        yield
    finally:
        agent_loop.chat_with_tools = original


@contextlib.contextmanager
def stub_search_knowledge(fake_result):
    """把 Dispatcher 里的 search_knowledge 换成固定结果（保持派发路径真实）。"""

    original = agent_dispatcher.TOOLS["search_knowledge"]
    agent_dispatcher.TOOLS["search_knowledge"] = lambda **kwargs: fake_result
    try:
        yield
    finally:
        agent_dispatcher.TOOLS["search_knowledge"] = original


@contextlib.contextmanager
def broken_embedding():
    original_module = embedding_service.get_embedding
    original_rag = rag_service.get_embedding
    original_rag_many = rag_service.get_embeddings

    def failing_embedding(text):
        raise embedding_service.EmbeddingError("模拟 embedding 服务不可用")

    def failing_embeddings(texts):
        raise embedding_service.EmbeddingError("模拟 embedding 服务不可用")

    embedding_service.get_embedding = failing_embedding
    rag_service.get_embedding = failing_embedding
    rag_service.get_embeddings = failing_embeddings
    try:
        yield
    finally:
        embedding_service.get_embedding = original_module
        rag_service.get_embedding = original_rag
        rag_service.get_embeddings = original_rag_many


def seed_difficult_latest():
    """最近一条反馈是 partial + 任务难度太高 -> difficult_previous_task=True。"""

    database.delete_all_feedback()
    database.insert_feedback(
        {
            "task": "定积分基础题练习",
            "estimated_minutes": 45,
            "actual_minutes": 45,
            "status": "completed",
            "reason": None,
            "completed_subtasks": 3,
            "total_subtasks": 3,
            "completed_questions": 4,
            "total_questions": 4,
        },
        "2026-10-04T20:00:00",
    )
    database.insert_feedback(
        {
            "task": "定积分综合题专项练习",
            "estimated_minutes": 60,
            "actual_minutes": 40,
            "status": "partial",
            "reason": "任务难度太高",
            "completed_subtasks": 1,
            "total_subtasks": 3,
            "completed_questions": 2,
            "total_questions": 4,
        },
        "2026-10-05T09:00:00",
    )


def seed_easy_latest():
    """最近一条反馈正常完成 -> difficult_previous_task=False。"""

    database.delete_all_feedback()
    database.insert_feedback(
        {
            "task": "定积分基础题练习",
            "estimated_minutes": 30,
            "actual_minutes": 28,
            "status": "completed",
            "reason": None,
            "completed_subtasks": 2,
            "total_subtasks": 2,
            "completed_questions": 3,
            "total_questions": 3,
        },
        "2026-10-05T09:00:00",
    )


def valid_plan(difficulty="easy", estimated_minutes=45, question_count=4, **overrides):
    """一张默认合法的草案计划（满足困难收紧：easy / 45 分钟 / 4 题）。"""

    plan = {
        "task": "定积分基础巩固练习",
        "estimated_minutes": estimated_minutes,
        "difficulty": difficulty,
        "question_count": question_count,
        "reason": "根据反馈，最近一次任务难度较高，这次先巩固基础。",
        "completion_criteria": f"完成{question_count}道基础题并订正后结束。",
        "subtasks": [
            {"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习定积分基本公式与几何意义"},
            {"title": "基础练习", "minutes": 15, "question_count": question_count - question_count // 2, "description": "完成指定基础题目"},
            {"title": "错题整理", "minutes": estimated_minutes - 25, "question_count": question_count // 2, "description": "订正本次练习中的错题"},
        ],
    }
    plan.update(overrides)
    return plan


def draft_json(answer, plan):
    return {
        "role": "assistant",
        "content": json.dumps({"answer": answer, "plan": plan}, ensure_ascii=False),
    }


def run_draft(user_message, llm, **kwargs):
    with stub_llm(llm):
        return agent_plan_draft.run_plan_draft(user_message, verbose=False, **kwargs)


def draft_user_content(llm, call_index=0):
    """第 call_index 次起草调用看到的用户消息内容。"""

    return llm.draft_seen[call_index][1]["content"]


def correction_message(llm, call_index):
    """第 call_index 次起草失败后追加的修正消息内容。"""

    return llm.draft_seen[call_index + 1][-1]["content"]


def fixture_plans_rows():
    with sqlite3.connect(str(FIXTURE_DB)) as conn:
        return conn.execute(
            "SELECT id, plan_date, task, difficulty, status FROM plans ORDER BY id"
        ).fetchall()


def real_plans_rows():
    """真实库 plans 快照（真实库可能还没有 plans 表，退让为 None）。"""

    if not REAL_DB.exists():
        return None

    try:
        with sqlite3.connect(str(REAL_DB)) as conn:
            return conn.execute(
                "SELECT id, plan_date, task, difficulty, status FROM plans ORDER BY id"
            ).fetchall()
    except sqlite3.OperationalError:
        return None


def knowledge_snapshot():
    return sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )


# 研究阶段的固定脚本：读反馈 -> 总结（反馈工具成功，因此 reason 可声称依据反馈）
RESEARCH_WITH_FEEDBACK = [
    tool_call("get_recent_feedback", {"days": 7}),
    final("反馈要点：最近一次任务是部分完成，原因是任务难度太高。"),
]

DEFAULT_ANSWER = "根据你最近的反馈，明天建议降低任务难度，先巩固定积分基础。"

FAKE_SEARCH_RESULT = {
    "ok": True,
    "query": "定积分 学习方法",
    "count": 1,
    "results": [
        {"source": "knowledge/定积分学习方法.md", "score": 0.92, "content": "先复习定义与几何意义，再做基础题巩固定积分。"}
    ],
    "error": None,
    "error_type": None,
}


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
REAL_PLANS_BEFORE = real_plans_rows()
REAL_CACHE_EXISTED = REAL_CACHE.exists()
KNOWLEDGE_SNAPSHOT = knowledge_snapshot()

if FIXTURE_DB.exists():
    FIXTURE_DB.unlink()
shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)

database.DATABASE_PATH = FIXTURE_DB
vector_cache.CACHE_DIR = CACHE_FIXTURE
database.init_db()
seed_difficult_latest()

try:
    # ==========================
    # 1. 结构：合法草案的两层输出
    # ==========================

    llm = ScriptedLLM(research=RESEARCH_WITH_FEEDBACK, draft=[draft_json(DEFAULT_ANSWER, valid_plan())])
    result = run_draft("帮我根据最近的学习情况，制定明天的学习计划草案。", llm)

    check("合法草案生成成功", result["ok"] is True and result["type"] == "plan_draft", f"result={result.get('type')}")
    check("stopped_reason 稳定", result["stopped_reason"] == "plan_draft_ready")
    check("自然语言 answer 与 structured plan 同时存在",
          isinstance(result["answer"], str) and result["answer"].strip()
          and isinstance(result["plan"], dict))
    check("plan 字段完整（恰好 7 个）", set(result["plan"]) == set(plan_service.PLAN_FIELDS),
          f"fields={sorted(result['plan'])}")
    check("plan 不是已保存记录", "plan_id" not in result["plan"] and "plan_date" not in result["plan"])
    check("subtasks 数量在 2-4 之间", 2 <= len(result["plan"]["subtasks"]) <= 4)
    check(
        "subtasks 结构正确",
        all(set(sub) == {"title", "minutes", "question_count", "description"} for sub in result["plan"]["subtasks"]),
    )
    check("首次即通过，无修正", result["corrections_used"] == 0 and llm.draft_calls == 1)
    check("研究阶段轨迹被带回", result["tool_calls"] == 1 and result["loop_turns"] == 2)
    check("程序推导的困难状态为 True", result["difficult_previous_task"] is True)
    validation = agent_tools.validate_plan_for_save(result["plan"])
    check("草案通过 validate_plan_for_save", validation["valid"] is True, f"reason={validation['reason']}")
    check("起草阶段没有给模型任何工具", all(tools is None for tools in llm.draft_tools))
    check("研究阶段工具面来自 Schema", all(tools is not None for tools in llm.research_tools))

    # 1.2 Markdown 围栏 JSON 也能解析
    fenced = {
        "role": "assistant",
        "content": "```json\n" + json.dumps({"answer": DEFAULT_ANSWER, "plan": valid_plan()}, ensure_ascii=False) + "\n```",
    }
    llm = ScriptedLLM(research=RESEARCH_WITH_FEEDBACK, draft=[fenced])
    result = run_draft("制定学习计划草案", llm)

    check("Markdown 围栏 JSON 可解析", result["ok"] is True and result["corrections_used"] == 0)

    # ==========================
    # 2. Validation：非法草案必须被程序拒绝并有限修正
    # ==========================

    # 2.1 非法 difficulty
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, valid_plan(difficulty="extreme")), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("非法 difficulty 被拒绝并修正", result["ok"] is True and result["corrections_used"] == 1)
    check("difficulty 错误原因返回给模型", "difficulty 只能是" in correction_message(llm, 0))
    check("修正以多轮消息进行", len(llm.draft_seen[1]) == 4, f"messages={len(llm.draft_seen[1])}")

    # 2.2 超时（estimated_minutes > 60）
    too_long = valid_plan()
    too_long["estimated_minutes"] = 90
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, too_long), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("超时被拒绝并修正", result["ok"] is True and result["corrections_used"] == 1)
    check("超时原因返回给模型", "不能超过60分钟" in correction_message(llm, 0))

    # 2.3 question_count 与 subtasks 不一致
    mismatched = valid_plan()
    mismatched["question_count"] = 5
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, mismatched), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("question_count 不一致被拒绝并修正", result["ok"] is True and result["corrections_used"] == 1)
    check("题数不一致原因返回给模型", "子任务题目数量之和与总题目数量不一致" in correction_message(llm, 0))

    # 2.4 subtasks 分钟总和超过 estimated_minutes
    over_minutes = valid_plan()
    over_minutes["subtasks"][2]["minutes"] = 60
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, over_minutes), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("子任务分钟总和超限被拒绝并修正", result["ok"] is True and result["corrections_used"] == 1)
    check("分钟超限原因返回给模型", "子任务时间总和超过" in correction_message(llm, 0))

    # 2.5 completion_criteria 与 question_count 不一致
    bad_criteria = valid_plan()
    bad_criteria["completion_criteria"] = "完成3道基础题并订正后结束。"
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, bad_criteria), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("completion_criteria 不一致被拒绝并修正", result["ok"] is True and result["corrections_used"] == 1)
    check("完成标准原因返回给模型", "completion_criteria 中的题目数量" in correction_message(llm, 0))

    # 2.6 非法字段（模型试图夹带 confirmed_by_user）被结构白名单拒绝
    smuggled = valid_plan()
    smuggled["confirmed_by_user"] = True
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, smuggled), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("非法字段被结构白名单拒绝", result["ok"] is True and result["corrections_used"] == 1)
    check("非法字段原因返回给模型", "confirmed_by_user" in correction_message(llm, 0))
    check("confirmed_by_user 没有进入最终草案", "confirmed_by_user" not in result["plan"])

    # 2.7 difficult_previous_task 由程序收紧（Agent 不可自称难度不高）
    medium_plan = valid_plan(difficulty="medium")
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, medium_plan), draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("困难收紧：medium 被拒绝并修正", result["ok"] is True and result["corrections_used"] == 1)
    check("收紧原因返回给模型", "difficulty 必须为 easy" in correction_message(llm, 0))
    check("收紧规则来自程序推导并写进起草 Prompt",
          "最近一次反馈是 partial" in draft_user_content(llm) and "difficulty = easy" in draft_user_content(llm))
    check("草案遵守收紧规则", result["plan"]["difficulty"] == "easy"
          and result["plan"]["question_count"] <= 6 and result["plan"]["estimated_minutes"] <= 45)

    # 2.8 没有困难历史时 medium 合法
    seed_easy_latest()
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, valid_plan(difficulty="medium"))],
    )
    result = run_draft("制定学习计划草案", llm)

    check("无困难历史时 medium 合法", result["ok"] is True and result["corrections_used"] == 0)
    check("程序推导的困难状态为 False", result["difficult_previous_task"] is False)
    check("无困难历史时 Prompt 不含收紧规则", "最近一次反馈是 partial" not in draft_user_content(llm))
    seed_difficult_latest()

    # 2.9 最多修正 2 次；仍失败 -> 稳定 plan_error
    always_bad = draft_json(DEFAULT_ANSWER, valid_plan(difficulty="extreme"))
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[always_bad, always_bad, always_bad],
        fallback=always_bad,
    )
    result = run_draft("制定学习计划草案", llm)

    check("最多修正 2 次（共 3 次起草调用）", llm.draft_calls == 3, f"calls={llm.draft_calls}")
    check("corrections_used 记录为 2", result["corrections_used"] == 2)
    check("2 次后仍失败 -> plan_error",
          result["ok"] is False and result["type"] == "plan_error"
          and result["error_type"] == "validation_error"
          and result["stopped_reason"] == "plan_validation_failed")
    check("失败的 plan 绝不作为正常结果返回", "plan" not in result and "answer" not in result)
    check("错误信息包含最后一次校验原因", "difficulty 只能是" in result["message"])
    check("错误结果保留研究阶段轨迹", result["tool_calls"] == 1 and result["steps"][0]["tool"] == "get_recent_feedback")

    # ==========================
    # 3. Grounding：来源完整性
    # ==========================

    # 3.1 明确要求知识库时真实调用 search_knowledge，草案使用真实 Tool Result
    with stub_search_knowledge(FAKE_SEARCH_RESULT):
        llm = ScriptedLLM(
            research=[
                tool_call("get_recent_feedback", {"days": 7}),
                tool_call("search_knowledge", {"query": "定积分 学习方法"}),
                final("已获取反馈与知识库内容。"),
            ],
            draft=[draft_json(DEFAULT_ANSWER, valid_plan())],
        )
        result = run_draft("结合知识库和我最近的反馈，帮我制定明天的学习计划草案。", llm)

    check("明确要求知识库时真实调用了 search_knowledge",
          result["ok"] is True
          and [step["tool"] for step in result["steps"]][1] == "search_knowledge"
          and result["steps"][1]["ok"] is True)
    prompt = draft_user_content(llm)
    check("起草 Prompt 包含真实知识库检索结果",
          "先复习定义与几何意义" in prompt and "真实知识库检索结果" in prompt)
    expected_knowledge_json = json.dumps(
        [FAKE_SEARCH_RESULT["results"][0]], ensure_ascii=False, indent=2
    )
    check("知识库结果原样进入 Prompt", expected_knowledge_json in prompt)

    # 3.2 草案依据真实反馈 Tool Result
    check("起草 Prompt 包含真实反馈数据", "定积分综合题专项练习" in prompt)
    check("起草 Prompt 来自真实 Tool Result 而非模型记忆", "统计结果" in prompt and "too_difficult_count" in prompt)

    # 3.3 没有 Tool Result 时不得声称"根据知识库"
    llm = ScriptedLLM(
        research=[final("没有查询任何数据。")],
        draft=[
            draft_json("根据知识库，建议先巩固定积分定义。", valid_plan(reason="根据知识库，先巩固基础。")),
            draft_json("没有可用的历史数据，建议从基础内容开始巩固。", valid_plan(reason="暂无可用的历史反馈，建议从基础内容开始。")),
        ],
    )
    result = run_draft("随便帮我制定个学习计划草案。", llm)

    check("无 Tool Result 却声称知识库 -> 修正",
          result["ok"] is True and result["corrections_used"] == 1)
    check("来源不诚实原因返回给模型", "知识库" in correction_message(llm, 0) and "没有成功的知识库检索结果" in correction_message(llm, 0))
    check("修正后的草案不再声称来源", "知识库" not in result["answer"] and "知识库" not in result["plan"]["reason"])

    # 3.4 search_knowledge 失败不能伪造知识依据
    with broken_embedding():
        llm = ScriptedLLM(
            research=[
                tool_call("get_recent_feedback", {"days": 7}),
                tool_call("search_knowledge", {"query": "定积分 学习方法"}),
                final("反馈已获取；知识库检索失败。"),
            ],
            draft=[
                draft_json("根据知识库，建议先巩固定积分定义。", valid_plan()),
                draft_json("知识库检索当前不可用；根据反馈，建议先巩固基础。", valid_plan()),
            ],
        )
        result = run_draft("结合知识库和我最近的反馈，帮我制定明天的学习计划草案。", llm)

    check("检索失败被如实记录",
          result["steps"][1]["tool"] == "search_knowledge"
          and result["steps"][1]["result"]["ok"] is False
          and result["steps"][1]["result"]["error_type"] == "embedding_service_unavailable")
    check("检索失败时声称知识库 -> 修正", result["ok"] is True and result["corrections_used"] == 1)
    check("起草 Prompt 明确告知检索失败", "知识库检索失败" in draft_user_content(llm))
    check("修正后仍可依据真实反馈起草", "根据反馈" in result["plan"]["reason"])

    # 3.5 始终伪造知识依据 -> 稳定错误
    dishonest = draft_json("根据知识库，建议先巩固定积分定义。", valid_plan())
    llm = ScriptedLLM(
        research=[final("没有查询任何数据。")],
        draft=[dishonest, dishonest, dishonest],
        fallback=dishonest,
    )
    result = run_draft("随便帮我制定个学习计划草案。", llm)

    check("始终伪造来源 -> plan_error",
          result["ok"] is False and result["error_type"] == "validation_error"
          and result["corrections_used"] == 2)

    # ==========================
    # 4. Safety：草案流程只读（第三阶段 C1 后 save_plan 已注册但仍受确认保护）
    # ==========================

    check("save_plan 已注册但工具面仅三个",
          set(agent_dispatcher.TOOLS) == {"get_recent_feedback", "search_knowledge", "save_plan"})
    check("save_plan 的 Tool 面没有 confirmed_by_user",
          "confirmed_by_user" not in json.dumps(agent_schema.TOOL_SCHEMAS, ensure_ascii=False))
    check("破坏性工具仍然不存在",
          "insert_feedback" not in agent_dispatcher.TOOLS
          and "delete_all_feedback" not in agent_dispatcher.TOOLS)

    # 4.1 模型不带凭证调用 save_plan -> 被参数守门拒绝，草案流程不受影响
    seed_difficult_latest()
    rows_before = database.get_feedback_list()
    llm = ScriptedLLM(
        research=[
            tool_call("save_plan", {"plan": valid_plan()}),
            tool_call("get_recent_feedback", {"days": 7}),
            final("反馈要点：最近一次任务难度太高。"),
        ],
        draft=[draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("帮我把计划保存下来。", llm)

    check("未带凭证的 save_plan 被参数守门拒绝",
          result["steps"][0]["tool"] == "save_plan"
          and result["steps"][0]["ok"] is False
          and result["steps"][0]["error_type"] == "invalid_arguments",
          f"step={result['steps'][0]}")
    check("save_plan 拒绝后草案仍完成", result["ok"] is True and result["type"] == "plan_draft")
    check("save_plan 尝试没有写入反馈", database.get_feedback_list() == rows_before)
    check("save_plan 尝试没有写入 plans", fixture_plans_rows() == [])

    # 4.2 起草阶段发出 tool_call -> 被程序拒绝并修正
    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[
            tool_call("save_plan", {"plan": valid_plan()}),
            draft_json(DEFAULT_ANSWER, valid_plan()),
        ],
    )
    result = run_draft("制定学习计划草案", llm)

    check("起草阶段不允许调用工具", result["ok"] is True and result["corrections_used"] == 1
          and "起草阶段不允许调用工具" in correction_message(llm, 0))

    # 4.3 连续 10 个 Plan Draft 也不写 plans / feedback / knowledge
    seed_difficult_latest()
    feedback_before = database.get_feedback_list()
    plans_before = fixture_plans_rows()
    knowledge_before = knowledge_snapshot()

    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK * 10,
        draft=[draft_json(DEFAULT_ANSWER, valid_plan())] * 10,
    )
    all_ok = True

    for index in range(10):
        draft_result = run_draft(f"制定第 {index + 1} 份学习计划草案", llm)

        if not (draft_result["ok"] is True and draft_result["type"] == "plan_draft"):
            all_ok = False
            break

    check("连续 10 个 Plan Draft 全部生成", all_ok, f"last={draft_result.get('type')}")
    check("10 次草案没有写 plans", fixture_plans_rows() == plans_before)
    check("10 次草案没有写 feedback", database.get_feedback_list() == feedback_before)
    check("10 次草案没有修改 knowledge", knowledge_snapshot() == knowledge_before)

    # 4.4 Tool Call 上限继续生效
    two_tools = multi_tool_call(
        ("get_recent_feedback", {"days": 7}),
        ("search_knowledge", {"query": "定积分"}),
    )
    llm = ScriptedLLM(
        research=[two_tools] * 3,
        fallback=final("预算用尽前的总结。"),
        draft=[draft_json(DEFAULT_ANSWER, valid_plan())],
    )
    result = run_draft("制定学习计划草案", llm)

    check("研究阶段工具次数被上限截断",
          result["tool_calls"] == agent_loop.MAX_TOOL_CALLS
          and len(result["steps"]) == agent_loop.MAX_TOOL_CALLS)
    check("预算内草案仍生成", result["ok"] is True and result["type"] == "plan_draft")

    # 4.5 Run timeout 继续生效（确定性假时钟）
    clock = FakeClock()

    def advance_research(messages, is_draft):
        if not is_draft:
            clock.advance(200)

    llm = ScriptedLLM(
        research=[tool_call("get_recent_feedback", {"days": 7})],
        draft=[draft_json(DEFAULT_ANSWER, valid_plan())],
        on_call=advance_research,
    )
    result = run_draft("制定学习计划草案", llm, clock=clock)

    check("研究阶段超时 -> plan_error run_timeout",
          result["ok"] is False and result["error_type"] == "run_timeout"
          and result["stopped_reason"] == "plan_run_timeout")

    clock = FakeClock()

    def advance_draft(messages, is_draft):
        if is_draft:
            clock.advance(200)

    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[draft_json(DEFAULT_ANSWER, valid_plan())],
        on_call=advance_draft,
    )
    result = run_draft("制定学习计划草案", llm, clock=clock, draft_max_run_seconds=90)

    check("起草阶段超时 -> plan_error run_timeout",
          result["ok"] is False and result["error_type"] == "run_timeout"
          and result["stopped_reason"] == "plan_run_timeout")

    # 4.6 LLM 错误 -> 稳定 plan_error
    llm = ScriptedLLM(research=[(None, "无法连接 Ollama（ConnectionError）")])
    result = run_draft("制定学习计划草案", llm)

    check("研究阶段 LLM 错误 -> plan_error",
          result["ok"] is False and result["error_type"] == "llm_error")

    llm = ScriptedLLM(
        research=RESEARCH_WITH_FEEDBACK,
        draft=[(None, "无法连接 Ollama（ConnectionError）")],
    )
    result = run_draft("制定学习计划草案", llm)

    check("起草阶段 LLM 错误 -> plan_error",
          result["ok"] is False and result["error_type"] == "llm_error"
          and result["stopped_reason"] == "plan_llm_error")

    # 4.7 研究阶段空响应 -> 稳定 plan_error
    llm = ScriptedLLM(research=[{"role": "assistant", "content": "   "}])
    result = run_draft("制定学习计划草案", llm)

    check("研究阶段空响应 -> plan_error",
          result["ok"] is False and result["error_type"] == "empty_response")

    # 4.8 读不到反馈时 fail-closed（不允许在状态未知时起草）
    original_path = database.DATABASE_PATH
    database.DATABASE_PATH = PROJECT_DIR / "_no_such_dir_draft" / "missing.db"
    try:
        llm = ScriptedLLM(research=[final("没有查询任何数据。")])
        result = run_draft("制定学习计划草案", llm)
    finally:
        database.DATABASE_PATH = FIXTURE_DB

    check("读不到反馈 -> plan_error history_unavailable",
          result["ok"] is False and result["error_type"] == "history_unavailable"
          and result["stopped_reason"] == "plan_history_unavailable")
    check("fail-closed 时没有调用起草 LLM", llm.draft_calls == 0)

    # ==========================
    # 5. 真实模型场景（端到端）
    # ==========================

    try:
        embedding_service.get_embedding("前置检查")
    except embedding_service.EmbeddingError as error:
        raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

    seed_difficult_latest()

    feedback_before = database.get_feedback_list()
    plans_before = fixture_plans_rows()

    real_result = agent_plan_draft.run_plan_draft(
        "帮我根据最近的学习情况，制定明天的学习计划草案。", verbose=False
    )

    check("真实草案返回结构稳定",
          real_result.get("type") in ("plan_draft", "plan_error"),
          f"type={real_result.get('type')} error={real_result.get('error_type')}")
    check("真实草案成功生成", real_result["ok"] is True and real_result["type"] == "plan_draft",
          f"error_type={real_result.get('error_type')} message={real_result.get('message')} "
          f"raw={str(real_result.get('last_draft_raw'))[:200]}")
    check("真实草案通过 validate_plan_for_save",
          agent_tools.validate_plan_for_save(real_result["plan"])["valid"] is True)
    check("真实草案遵守困难收紧",
          real_result["plan"]["difficulty"] == "easy"
          and real_result["plan"]["estimated_minutes"] <= 45
          and real_result["plan"]["question_count"] <= 6,
          f"plan={real_result['plan']}")
    check("真实草案修正次数不超过 2", real_result["corrections_used"] <= MAX_CORRECTIONS)
    check("真实草案只用了白名单工具",
          all(step["tool"] in agent_dispatcher.TOOLS for step in real_result["steps"]),
          f"tools={[step['tool'] for step in real_result['steps']]}")
    check("真实草案没有 save_plan",
          "save_plan" not in [step["tool"] for step in real_result["steps"]])
    check("真实草案后来源诚实",
          agent_plan_draft.check_grounding(
              [real_result["answer"], real_result["plan"]["reason"]], real_result["steps"]
          ) == [])

    info("真实草案 answer", str(real_result.get("answer"))[:120].replace("\n", " "))
    info("真实草案 plan", json.dumps(real_result.get("plan"), ensure_ascii=False)[:200])
    info("真实草案工具轨迹", f"tools={[step['tool'] for step in real_result['steps']]} "
                        f"corrections={real_result.get('corrections_used')}")

    # 真实场景 2：明确要求结合知识库
    real_result_2 = agent_plan_draft.run_plan_draft(
        "结合知识库和我最近的反馈，帮我制定明天的学习计划草案。", verbose=False
    )

    tools_used = [step["tool"] for step in real_result_2.get("steps", [])]
    check("真实草案 2 返回结构稳定", real_result_2.get("type") in ("plan_draft", "plan_error"),
          f"type={real_result_2.get('type')}")
    check("真实草案 2 成功生成", real_result_2["ok"] is True and real_result_2["type"] == "plan_draft",
          f"error_type={real_result_2.get('error_type')} message={real_result_2.get('message')} "
          f"raw={str(real_result_2.get('last_draft_raw'))[:200]}")
    check("真实草案 2 调用了 search_knowledge", "search_knowledge" in tools_used, f"tools={tools_used}")
    check("真实草案 2 通过 validate_plan_for_save",
          agent_tools.validate_plan_for_save(real_result_2["plan"])["valid"] is True)
    check("真实草案 2 来源诚实",
          agent_plan_draft.check_grounding(
              [real_result_2["answer"], real_result_2["plan"]["reason"]], real_result_2["steps"]
          ) == [])

    check("真实草案不写 feedback", database.get_feedback_list() == feedback_before)
    check("真实草案不写 plans", fixture_plans_rows() == plans_before)
finally:
    database.DATABASE_PATH = PROJECT_DIR / "feedback.db"
    vector_cache.CACHE_DIR = PROJECT_DIR / ".rag_cache"
    rag_service.reset_cache()

    for _ in range(20):
        gc.collect()

        if not FIXTURE_DB.exists():
            break

        try:
            FIXTURE_DB.unlink()
        except PermissionError:
            time.sleep(0.2)

    shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)

check("测试夹具已清理", not FIXTURE_DB.exists() and not CACHE_FIXTURE.exists())
check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
check("真实 plans 表未被修改", real_plans_rows() == REAL_PLANS_BEFORE)
check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)
check("knowledge/ 未被修改", knowledge_snapshot() == KNOWLEDGE_SNAPSHOT)

print("\n全部用例通过")
