"""save_plan Agent Tool 测试：直接运行 python test_save_plan_tool.py。

覆盖：
- Tool 层：无凭证/伪造凭证/未确认/过期/不匹配 -> 稳定拒绝；合法确认 -> 写库成功
- 规则层：validation 失败不写库；difficult_previous_task 仍由服务端推导；
  当天 active 唯一；凭证一次性
- Dispatcher/Schema：save_plan 已注册；confirmed_by_user 注入被参数守门拒绝；
  破坏性工具仍然不存在
- 真实模型段：用户确认后 Agent 能完成保存；没有凭证时保存被拒绝且 plans 不变

隔离：反馈数据库与向量缓存指向临时夹具；结束校验真实数据未被改动。
"""

import gc
import json
import shutil
import sqlite3
import time
from pathlib import Path

import agent_dispatcher
import agent_loop
import agent_schema
import agent_tools
import confirmation_service
import database
import embedding_service
import plan_service
import rag_service
import vector_cache


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_save_tool.db"
CACHE_FIXTURE = PROJECT_DIR / "_test_cache_save_tool"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"

SAVE_PLAN_DATE = "2026-10-06"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


def make_plan(**overrides):
    plan = {
        "task": "定积分基础巩固",
        "estimated_minutes": 40,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "根据最近反馈，先巩固基础。",
        "completion_criteria": "完成4道基础题并订正后结束。",
        "subtasks": [
            {"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习定积分基本公式"},
            {"title": "基础练习", "minutes": 30, "question_count": 4, "description": "完成指定基础题目"},
        ],
    }
    plan.update(overrides)
    return plan


def normalized(plan):
    value, error = plan_service.normalize_plan(plan)
    if error:
        raise AssertionError(f"测试计划非法：{error}")
    return value


def create_confirmed(plan, plan_date=SAVE_PLAN_DATE):
    """签发并确认一份凭证，返回 confirmation_id。"""

    created = confirmation_service.create_pending_confirmation(normalized(plan), plan_date)
    if not created.get("ok"):
        raise AssertionError(f"测试夹具：创建凭证失败 {created}")

    confirmed = confirmation_service.confirm_confirmation(created["confirmation_id"])
    if not confirmed.get("ok"):
        raise AssertionError(f"测试夹具：确认失败 {confirmed}")

    return created["confirmation_id"]


def create_pending(plan, plan_date=SAVE_PLAN_DATE):
    created = confirmation_service.create_pending_confirmation(normalized(plan), plan_date)
    if not created.get("ok"):
        raise AssertionError(f"测试夹具：创建凭证失败 {created}")
    return created["confirmation_id"]


def seed_easy_history():
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


def seed_difficult_history():
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


def plans_rows():
    with sqlite3.connect(str(FIXTURE_DB)) as conn:
        return conn.execute(
            "SELECT id, plan_date, task, difficulty, status FROM plans ORDER BY id"
        ).fetchall()


def knowledge_snapshot():
    return sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )


def real_plans_rows():
    if not REAL_DB.exists():
        return None

    try:
        with sqlite3.connect(str(REAL_DB)) as conn:
            return conn.execute(
                "SELECT id, plan_date, task, difficulty, status FROM plans ORDER BY id"
            ).fetchall()
    except sqlite3.OperationalError:
        return None


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
seed_easy_history()
confirmation_service.reset_confirmations()

try:
    # ==========================
    # 1. Tool 层：确认状态决定一切
    # ==========================

    plan = normalized(make_plan())

    # 1.1 无 confirmation -> 拒绝
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, "")
    check("无 confirmation -> confirmation_required",
          result["ok"] is False and result["saved"] is False
          and result["error_type"] == "confirmation_required")
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, None)
    check("confirmation_id 为 None -> 拒绝", result["error_type"] == "confirmation_required")
    check("拒绝时不写库", plans_rows() == [])

    # 1.2 伪造 confirmation -> 拒绝
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, "deadbeefdeadbeefdeadbeef")
    check("伪造 confirmation -> confirmation_not_found",
          result["ok"] is False and result["error_type"] == "confirmation_not_found")
    check("伪造凭证不写库", plans_rows() == [])

    # 1.3 未确认（pending）-> 拒绝
    pending_id = create_pending(make_plan())
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, pending_id)
    check("pending 凭证 -> confirmation_required",
          result["ok"] is False and result["error_type"] == "confirmation_required")
    check("未确认不写库", plans_rows() == [])

    # 1.4 过期 -> 拒绝（直接构造已过期的凭证）
    expired_created = confirmation_service.create_pending_confirmation(
        normalized(make_plan()), SAVE_PLAN_DATE, ttl_seconds=0, now=1000.0
    )
    expired_id = expired_created["confirmation_id"]
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, expired_id)
    check("过期凭证 -> confirmation_expired",
          result["ok"] is False and result["error_type"] == "confirmation_expired")

    # 1.5 plan / plan_date 不匹配 -> 拒绝
    mismatch_id = create_confirmed(make_plan())
    result = agent_tools.save_plan(make_plan(task="被替换的任务"), SAVE_PLAN_DATE, mismatch_id)
    check("plan 不匹配 -> confirmation_mismatch",
          result["ok"] is False and result["error_type"] == "confirmation_mismatch")

    result = agent_tools.save_plan(make_plan(), "2026-10-07", mismatch_id)
    check("plan_date 不匹配 -> confirmation_mismatch",
          result["ok"] is False and result["error_type"] == "confirmation_mismatch")
    check("不匹配不写库", plans_rows() == [])

    # 1.6 合法 confirmed 凭证 -> 保存成功
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, mismatch_id)
    check("合法 confirmed 凭证 -> 保存成功",
          result["ok"] is True and result["saved"] is True
          and isinstance(result["plan_id"], int) and result["plan_date"] == SAVE_PLAN_DATE,
          f"result={result}")
    check("写库内容与凭证绑定一致", plans_rows() == [(
        result["plan_id"], SAVE_PLAN_DATE, "定积分基础巩固", "easy", "active"
    )])

    # 1.7 凭证一次性：成功保存后再次使用 -> already_used
    result = agent_tools.save_plan(make_plan(), SAVE_PLAN_DATE, mismatch_id)
    check("成功保存后凭证被消费 -> confirmation_already_used",
          result["ok"] is False and result["error_type"] == "confirmation_already_used")
    check("消费后没有产生第二条 plan", len(plans_rows()) == 1)

    # ==========================
    # 2. 规则层：validation / difficult / active 唯一
    # ==========================

    # 2.1 difficult_previous_task 由服务端推导：确认一份 medium 计划，再让历史变难
    seed_difficult_history()
    medium_id = create_confirmed(make_plan(difficulty="medium"), "2026-10-07")
    result = agent_tools.save_plan(make_plan(difficulty="medium"), "2026-10-07", medium_id)
    check("困难历史下 medium 计划被校验拒绝",
          result["ok"] is False and result["error_type"] == "validation_error",
          f"result={result}")
    check("校验原因来自收紧规则", "difficulty 必须为 easy" in (result.get("error") or ""))
    check("validation 失败不写库", len(plans_rows()) == 1)

    # 2.2 校验失败不消费凭证（用户可修正后用同一凭证保存修正后的计划？——
    #     绑定是 plan+date，修正后的计划需要重新确认。凭证保留但只能用于原计划）
    still_valid = confirmation_service.validate_for_save(
        medium_id, normalized(make_plan(difficulty="medium")), "2026-10-07"
    )
    check("校验失败后凭证未被消费（仍可校验绑定）",
          still_valid["ok"] is True or still_valid["error_type"] == "confirmation_mismatch")

    # 2.3 困难历史下 easy 计划可以保存（收紧规则正确应用）
    easy_id = create_confirmed(make_plan(), "2026-10-07")
    result = agent_tools.save_plan(make_plan(), "2026-10-07", easy_id)
    check("困难历史下 easy 计划保存成功",
          result["ok"] is True and result["saved"] is True, f"result={result}")
    check("困难收紧写入 easy 计划", plans_rows()[-1][3] == "easy")

    # 2.4 当天 active 唯一：同一天再保存 -> active_plan_exists
    another_id = create_confirmed(make_plan(task="同日第二份计划"), "2026-10-07")
    result = agent_tools.save_plan(make_plan(task="同日第二份计划"), "2026-10-07", another_id)
    check("同天重复 active -> active_plan_exists",
          result["ok"] is False and result["error_type"] == "active_plan_exists")
    check("重复保存没有新增 plan", len(plans_rows()) == 2)

    # 2.5 参数类型错误 -> invalid_argument
    result = agent_tools.save_plan("不是字典", SAVE_PLAN_DATE, "x")
    check("plan 非对象 -> invalid_argument",
          result["ok"] is False and result["error_type"] == "invalid_argument")
    result = agent_tools.save_plan(make_plan(), "   ", "x")
    check("plan_date 空白 -> invalid_argument",
          result["ok"] is False and result["error_type"] == "invalid_argument")

    # ==========================
    # 3. Dispatcher / Schema / Agent Security
    # ==========================

    check("save_plan 已加入 Dispatcher", "save_plan" in agent_dispatcher.TOOLS)
    check("save_plan 已加入 Schema", "save_plan" in agent_schema.TOOL_NAMES)
    check("Dispatcher 与 Schema 工具面一致",
          set(agent_dispatcher.TOOLS) == set(agent_schema.TOOL_NAMES))

    schema_names = {item["function"]["name"] for item in agent_schema.TOOL_SCHEMAS}
    check("Schema 工具面为两个只读 + save_plan",
          schema_names == {"get_recent_feedback", "search_knowledge", "save_plan"})

    schema_text = json.dumps(agent_schema.TOOL_SCHEMAS, ensure_ascii=False)
    check("Schema 中不存在 confirmed_by_user", "confirmed_by_user" not in schema_text)
    check("Schema 要求 confirmation_id",
          "confirmation_id" in schema_text and "confirmation_required" in agent_tools.save_plan.__doc__)

    check("破坏性工具仍然不存在",
          "insert_feedback" not in agent_dispatcher.TOOLS
          and "delete_all_feedback" not in agent_dispatcher.TOOLS
          and "insert_feedback" not in agent_schema.TOOL_NAMES)

    # 3.1 Agent 不能注入 confirmed_by_user：参数守门直接拒绝
    outcome = agent_dispatcher.dispatch({
        "name": "save_plan",
        "arguments": {
            "plan": make_plan(),
            "plan_date": SAVE_PLAN_DATE,
            "confirmation_id": "whatever",
            "confirmed_by_user": True,
        },
    })
    check("Agent 注入 confirmed_by_user -> invalid_arguments",
          outcome["ok"] is False and outcome["error_type"] == "invalid_arguments"
          and "confirmed_by_user" in outcome["error"],
          f"error={outcome['error']}")

    # 3.2 Agent 不能绕过 confirmation：缺少 confirmation_id -> 参数守门拒绝
    outcome = agent_dispatcher.dispatch({
        "name": "save_plan",
        "arguments": {"plan": make_plan(), "plan_date": SAVE_PLAN_DATE},
    })
    check("缺少 confirmation_id -> invalid_arguments（缺少必填参数）",
          outcome["ok"] is False and outcome["error_type"] == "invalid_arguments"
          and "confirmation_id" in outcome["error"])

    # 3.3 plan 不是对象 -> 参数守门拒绝
    outcome = agent_dispatcher.dispatch({
        "name": "save_plan",
        "arguments": {"plan": "json字符串", "plan_date": SAVE_PLAN_DATE,
                      "confirmation_id": "whatever"},
    })
    check("plan 非对象 -> invalid_arguments",
          outcome["ok"] is False and outcome["error_type"] == "invalid_arguments")

    # 3.4 Agent 伪造 confirmation_id：通过守门但被服务端拒绝，不写库
    rows_before = plans_rows()
    outcome = agent_dispatcher.dispatch({
        "name": "save_plan",
        "arguments": {"plan": make_plan(), "plan_date": SAVE_PLAN_DATE,
                      "confirmation_id": "agent伪造的id123456"},
    })
    check("伪造凭证经 Dispatcher 到达服务端被拒",
          outcome["ok"] is True  # 工具执行并返回结构化失败
          and outcome["result"]["ok"] is False
          and outcome["result"]["error_type"] == "confirmation_not_found")
    check("伪造凭证没有写库", plans_rows() == rows_before)

    # 3.5 完整 Dispatcher 成功链路：真实凭证 -> saved=True -> plan_id
    chain_id = create_confirmed(make_plan(task="Dispatcher 链路计划"), "2026-10-08")
    outcome = agent_dispatcher.dispatch({
        "name": "save_plan",
        "arguments": {"plan": make_plan(task="Dispatcher 链路计划"),
                      "plan_date": "2026-10-08",
                      "confirmation_id": chain_id},
    })
    check("Dispatcher 端到端保存成功",
          outcome["ok"] is True and outcome["result"]["saved"] is True
          and isinstance(outcome["result"]["plan_id"], int),
          f"result={outcome['result']}")
    check("链路计划写入数据库", plans_rows()[-1][2] == "Dispatcher 链路计划")

    # 3.6 Agent 无法修改凭证与计划的绑定（没有任何修改接口，行为上只能 mismatch）
    bound = create_confirmed(make_plan(task="绑定测试计划"), "2026-10-09")
    tampered = agent_tools.save_plan(
        make_plan(task="绑定测试计划", estimated_minutes=45), "2026-10-09", bound
    )
    check("篡改绑定计划 -> mismatch",
          tampered["ok"] is False and tampered["error_type"] == "confirmation_mismatch")
    check("篡改不写库", len(plans_rows()) == 3)

    # ==========================
    # 4. 真实模型场景（端到端）
    # ==========================

    try:
        embedding_service.get_embedding("前置检查")
    except embedding_service.EmbeddingError as error:
        raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

    seed_easy_history()
    confirmation_service.reset_confirmations()

    # 真实场景 1：用户确认后，Agent 持真实凭证完成保存
    real_plan = normalized(make_plan(task="真实确认保存计划"))
    real_id = create_confirmed(real_plan, "2026-10-09")
    rows_before = plans_rows()

    message = (
        f"我已经在界面上确认了这份学习计划，确认凭证是 {real_id}。"
        "请调用 save_plan 工具，把它原样保存到 2026-10-09。"
        "plan 参数必须与下面的 JSON 完全一致，不要修改任何字段或数值：\n"
        + json.dumps(real_plan, ensure_ascii=False)
    )

    real_result = agent_loop.run_agent(message, verbose=False)

    save_steps = [step for step in real_result.get("steps", []) if step["tool"] == "save_plan"]
    check("真实运行没有 LLM 错误", real_result["stopped_reason"] != "llm_error",
          f"error={real_result['error']}")
    check("真实运行调用了 save_plan", len(save_steps) >= 1,
          f"tools={[step['tool'] for step in real_result['steps']]}")
    saved_results = [step["result"] for step in save_steps
                     if isinstance(step["result"], dict) and step["result"].get("saved") is True]
    check("真实保存成功且带 plan_id",
          bool(saved_results) and isinstance(saved_results[0].get("plan_id"), int),
          f"save_results={save_steps}")
    check("真实保存后 plans 增加一条", len(plans_rows()) == len(rows_before) + 1)
    check("真实保存的日期正确", plans_rows()[-1][1] == "2026-10-09")

    info("真实保存轨迹", f"tools={[step['tool'] for step in real_result['steps']]} "
                     f"answer={str(real_result['final_answer'])[:100]}")

    # 真实场景 2：没有凭证时，保存必须被拒绝且 plans 不变
    rows_before = plans_rows()
    no_conf_result = agent_loop.run_agent(
        "请把这份计划保存到 2026-10-10：\n"
        + json.dumps(make_plan(task="无凭证保存尝试"), ensure_ascii=False)
        + "\n如果保存不了，请告诉我原因。",
        verbose=False,
    )

    no_conf_saves = [
        step for step in no_conf_result.get("steps", [])
        if step["tool"] == "save_plan"
        and isinstance(step.get("result"), dict)
        and step["result"].get("saved") is True
    ]
    check("无凭证时没有保存成功", no_conf_saves == [])
    check("无凭证时 plans 不变", plans_rows() == rows_before)
    check("无凭证场景有最终回答", bool(no_conf_result.get("final_answer")))

    rejected = [
        step["result"].get("error_type")
        for step in no_conf_result.get("steps", [])
        if step["tool"] == "save_plan" and isinstance(step.get("result"), dict)
    ]
    info("无凭证保存尝试结果", f"save_plan 结果 error_type={rejected} "
                          f"answer={str(no_conf_result['final_answer'])[:100]}")

finally:
    confirmation_service.reset_confirmations()
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

    # 附带清理 journal/wal（孤立 journal 会让下次建库异常）
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            Path(str(FIXTURE_DB) + suffix).unlink()
        except FileNotFoundError:
            pass

    shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)

check("测试夹具已清理", not FIXTURE_DB.exists() and not CACHE_FIXTURE.exists())
check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
check("真实 plans 表未被修改", real_plans_rows() == REAL_PLANS_BEFORE)
check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)
check("knowledge/ 未被修改", knowledge_snapshot() == KNOWLEDGE_SNAPSHOT)

print("\n全部用例通过")
