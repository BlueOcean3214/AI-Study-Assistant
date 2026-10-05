"""plan_service 测试：直接运行 python test_plan_service.py。

隔离：把 database.DATABASE_PATH 指向测试专用 SQLite；
结束时校验真实 feedback.db / knowledge/ / .rag_cache 未被改动。
"""

import gc
import inspect
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import database
import plan_service


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_plan_service.db"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def make_plan(**overrides):
    """合法计划：easy / 45 分钟 / 4 题 / 3 个子任务。"""

    plan = {
        "task": "定积分基础巩固",
        "estimated_minutes": 45,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "最近反馈显示难度偏高，先巩固基础",
        "completion_criteria": "完成4道定积分基础题并订正",
        "subtasks": [
            {"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习牛顿-莱布尼茨公式"},
            {"title": "基础练习", "minutes": 20, "question_count": 2, "description": "完成2道基础题"},
            {"title": "错题整理", "minutes": 15, "question_count": 2, "description": "整理2道错题"},
        ],
    }
    plan.update(overrides)
    return plan


def plan_row_count(plan_date=None):
    conn = sqlite3.connect(FIXTURE_DB)
    if plan_date is None:
        count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    else:
        count = conn.execute("SELECT COUNT(*) FROM plans WHERE plan_date = ?", (plan_date,)).fetchone()[0]
    conn.close()
    return count


def seed_feedback(status, reason, created_at="2026-10-05T09:00:00"):
    database.insert_feedback(
        {
            "task": "示例任务",
            "estimated_minutes": 60,
            "actual_minutes": 40,
            "status": status,
            "reason": reason,
            "completed_subtasks": 1,
            "total_subtasks": 3,
            "completed_questions": 2,
            "total_questions": 4,
        },
        created_at,
    )


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
KNOWLEDGE_SNAPSHOT = sorted(
    (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
    for path in KNOWLEDGE_DIR.rglob("*.txt")
)
REAL_CACHE_EXISTED = REAL_CACHE.exists()

if FIXTURE_DB.exists():
    FIXTURE_DB.unlink()

database.DATABASE_PATH = FIXTURE_DB
database.init_db()

try:
    # ==========================
    # 1~3 表结构
    # ==========================
    connection = sqlite3.connect(FIXTURE_DB)

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("plans 表已创建", "plans" in tables, f"tables={sorted(tables)}")
    check("feedbacks 表仍然存在", "feedbacks" in tables)

    columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(plans)")}
    expected_columns = {
        "id", "plan_date", "task", "estimated_minutes", "difficulty", "question_count",
        "reason", "completion_criteria", "subtasks_json", "status", "created_at",
    }
    check("plans 字段齐全", set(columns) == expected_columns, f"columns={sorted(columns)}")
    check("status 默认 active", "active" in connection.execute(
        "SELECT dflt_value FROM pragma_table_info('plans') WHERE name='status'"
    ).fetchone()[0])

    index_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_plans_active_date'"
    ).fetchone()
    check("active partial unique index 存在", index_row is not None)
    check(
        "index 只约束 status='active'",
        index_row is not None and "WHERE status = 'active'" in index_row[0],
        f"sql={index_row[0] if index_row else None}",
    )
    connection.close()

    # ==========================
    # 4. 合法计划 + confirmed=True -> 成功
    # ==========================
    result = plan_service.save_plan(make_plan(), "2026-10-05", True)
    check("合法计划保存成功", result["saved"] is True, f"result={result}")
    check("返回 plan_id", isinstance(result["plan_id"], int) and result["plan_id"] > 0)
    check("返回 plan_date", result["plan_date"] == "2026-10-05")
    check("返回结构不含 SQLite 行字段", "subtasks_json" not in result["plan"] and "id" not in result["plan"])
    check("数据库新增 1 行", plan_row_count() == 1)
    check("confirmed_by_user 并未写入计划内容", "confirmed_by_user" not in result["plan"])

    # ==========================
    # 5. confirmed=false -> 失败且不写库
    # ==========================
    before = plan_row_count()
    for bad_confirmation in (False, None, 0, "true", 1):
        result = plan_service.save_plan(make_plan(), "2026-10-06", bad_confirmation)
        check(
            f"confirmed_by_user={bad_confirmation!r} 被拒绝",
            result["saved"] is False and result["error_type"] == "confirmation_required",
            f"result={result}",
        )
    check("未确认时数据库没有新增行", plan_row_count() == before)

    # ==========================
    # 6~10 非法计划
    # ==========================
    invalid_cases = [
        ("非法 difficulty", {"difficulty": "extreme"}),
        ("超长时间", {"estimated_minutes": 90}),
        (
            "subtasks 只有 1 个",
            {"subtasks": [{"title": "A", "minutes": 10, "question_count": 4, "description": "d"}]},
        ),
        (
            "subtasks 有 5 个",
            {"subtasks": [{"title": f"T{i}", "minutes": 5, "question_count": 0, "description": "d"} for i in range(5)]},
        ),
        ("子任务题数不一致", {"question_count": 6, "completion_criteria": "完成6道题"}),
        ("completion_criteria 题数不一致", {"completion_criteria": "完成9道题"}),
    ]
    for name, overrides in invalid_cases:
        result = plan_service.save_plan(make_plan(**overrides), "2026-10-07", True)
        check(
            f"{name} 被拒绝",
            result["saved"] is False and result["error_type"] == "validation_error",
            f"result={result}",
        )

    before = plan_row_count()
    result = plan_service.save_plan(make_plan(extra_field="x"), "2026-10-08", True)
    check("结构白名单拒绝多余字段", result["error_type"] == "validation_error", f"result={result}")

    result = plan_service.save_plan("不是字典", "2026-10-08", True)
    check("非对象 plan 被拒绝", result["error_type"] == "validation_error")

    partial_plan = make_plan()
    partial_plan.pop("task")
    result = plan_service.save_plan(partial_plan, "2026-10-08", True)
    check("缺少字段被拒绝", result["error_type"] == "validation_error")
    check("全部非法计划都没有写库", plan_row_count() == before)

    # ==========================
    # 11. difficult_previous_task 正确收紧
    # ==========================
    database.delete_all_feedback()
    seed_feedback("partial", "任务难度太高")

    result = plan_service.save_plan(make_plan(difficulty="medium"), "2026-10-09", True)
    check(
        "困难状态下 medium 被拒绝",
        result["saved"] is False and "easy" in result["message"],
        f"result={result}",
    )

    result = plan_service.save_plan(
        make_plan(
            question_count=8,
            completion_criteria="完成8道基础题并订正",
            subtasks=[
                {"title": "A", "minutes": 10, "question_count": 0, "description": "d"},
                {"title": "B", "minutes": 15, "question_count": 4, "description": "d"},
                {"title": "C", "minutes": 15, "question_count": 4, "description": "d"},
            ],
        ),
        "2026-10-09",
        True,
    )
    check(
        "困难状态下题量超 6 被拒绝",
        result["saved"] is False and "6" in result["message"],
        f"result={result}",
    )

    result = plan_service.save_plan(make_plan(), "2026-10-09", True)
    check("困难状态下 easy/45/4 仍可通过", result["saved"] is True, f"result={result}")

    # ==========================
    # 12. 无法确定困难条件时 fail-closed
    # ==========================
    original_get_feedback_list = plan_service.get_feedback_list

    def broken_history():
        raise sqlite3.OperationalError("模拟读取反馈失败")

    plan_service.get_feedback_list = broken_history
    try:
        before = plan_row_count()
        result = plan_service.save_plan(make_plan(), "2026-10-10", True)
    finally:
        plan_service.get_feedback_list = original_get_feedback_list

    check(
        "读不到历史时 fail-closed",
        result["saved"] is False and result["error_type"] == "history_unavailable",
        f"result={result}",
    )
    check("fail-closed 时没有写库", plan_row_count() == before)

    # ==========================
    # 13. 调用方无法绕过 difficult_previous_task
    # ==========================
    parameters = set(inspect.signature(plan_service.save_plan).parameters)
    check(
        "save_plan 只有 plan/plan_date/confirmed_by_user",
        parameters == {"plan", "plan_date", "confirmed_by_user"},
        f"parameters={sorted(parameters)}",
    )
    check(
        "没有 difficult_previous_task 入口",
        not any("difficult" in name for name in parameters),
    )
    check(
        "没有 validated 入口",
        not any("valid" in name for name in parameters),
    )

    # ==========================
    # 14~15. 当天唯一 / 不同日期可保存
    # ==========================
    database.delete_all_feedback()
    database.delete_all_feedback()

    result = plan_service.save_plan(make_plan(), "2026-10-11", True)
    check("首次保存成功", result["saved"] is True)
    before = plan_row_count()

    result = plan_service.save_plan(make_plan(task="重复计划"), "2026-10-11", True)
    check(
        "同一天第二个 active plan 被拒绝",
        result["saved"] is False and result["error_type"] == "active_plan_exists",
        f"result={result}",
    )
    check("被拒绝时没有新增/覆盖行", plan_row_count() == before)
    check(
        "原计划没有被覆盖",
        plan_service.get_active_plan("2026-10-11")["plan"]["task"] == "定积分基础巩固",
    )

    result = plan_service.save_plan(make_plan(task="第二天计划"), "2026-10-12", True)
    check("不同日期可以保存", result["saved"] is True, f"result={result}")
    check("两个日期各 1 行", plan_row_count() == before + 1)

    # ==========================
    # 16. 保存后能恢复完整 subtasks
    # ==========================
    restored = plan_service.get_active_plan("2026-10-11")
    check("有计划时 exists=True", restored["exists"] is True)
    check(
        "subtasks 完整恢复",
        restored["plan"]["subtasks"] == make_plan()["subtasks"],
        f"subtasks={restored['plan']['subtasks']}",
    )
    check(
        "返回结构不含 SQLite 行字段",
        "subtasks_json" not in restored["plan"],
    )

    # ==========================
    # 17. 模拟 INSERT 异常 -> 没有半条记录
    # ==========================
    bad_plan = {
        "task": None,  # 违反 NOT NULL
        "estimated_minutes": 45,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "r",
        "completion_criteria": "c",
        "subtasks_json": "[]",
    }
    before = plan_row_count()
    try:
        database.insert_plan("2026-10-13", bad_plan, datetime.now().isoformat())
        check("INSERT 异常被抛出", False)
    except sqlite3.IntegrityError:
        check("INSERT 异常被抛出", True)
    except database.ActivePlanExistsError:
        check("INSERT 异常被正确分类（不应是 ActivePlanExistsError）", False)
    check("异常后没有半条记录", plan_row_count() == before)

    # ==========================
    # 18. UNIQUE 冲突 -> 不产生额外记录
    # ==========================
    connection = sqlite3.connect(FIXTURE_DB)
    connection.execute(
        """
        INSERT INTO plans
        (plan_date, task, estimated_minutes, difficulty, question_count,
         reason, completion_criteria, subtasks_json, status, created_at)
        VALUES ('2026-10-14', '直接写入', 30, 'easy', 2, 'r', 'c', '[]', 'active', '2026-10-14T08:00:00')
        """
    )
    connection.commit()
    before = plan_row_count()

    try:
        connection.execute(
            """
            INSERT INTO plans
            (plan_date, task, estimated_minutes, difficulty, question_count,
             reason, completion_criteria, subtasks_json, status, created_at)
            VALUES ('2026-10-14', '第二条 active', 30, 'easy', 2, 'r', 'c', '[]', 'active', '2026-10-14T09:00:00')
            """
        )
        connection.commit()
        check("partial index 拦截重复 active", False)
    except sqlite3.IntegrityError:
        connection.rollback()
        check("partial index 拦截重复 active", True)

    check("冲突后没有额外记录", plan_row_count() == before)
    connection.close()

    # ==========================
    # 19~21. get_active_plan
    # ==========================
    missing = plan_service.get_active_plan("2030-01-01")
    check(
        "无计划返回稳定空结构",
        missing == {"exists": False, "plan": None},
        f"result={missing}",
    )
    check("有计划返回 exists=True", plan_service.get_active_plan("2026-10-12")["exists"] is True)

    for bad_date in ("2026/10/05", "abc", "2026-99-99", "2026-1-5", "", None, 20261005):
        result = plan_service.get_active_plan(bad_date)
        check(
            f"非法日期被拒绝: {bad_date!r}",
            result["exists"] is False and result["error_type"] == "invalid_date",
            f"result={result}",
        )
        saved = plan_service.save_plan(make_plan(), bad_date, True)
        check(
            f"保存时非法日期被拒绝: {bad_date!r}",
            saved["saved"] is False and saved["error_type"] == "invalid_date",
            f"result={saved}",
        )

    # 数据库错误 -> 稳定的 database_error
    original_insert_plan = plan_service.insert_plan

    def broken_insert(*args, **kwargs):
        raise sqlite3.OperationalError("模拟写入失败")

    plan_service.insert_plan = broken_insert
    try:
        result = plan_service.save_plan(make_plan(), "2026-10-15", True)
    finally:
        plan_service.insert_plan = original_insert_plan

    check(
        "写入失败转成 database_error",
        result["saved"] is False and result["error_type"] == "database_error",
        f"result={result}",
    )
finally:
    database.DATABASE_PATH = PROJECT_DIR / "feedback.db"

    for _ in range(20):
        gc.collect()

        if not FIXTURE_DB.exists():
            break

        try:
            FIXTURE_DB.unlink()
        except PermissionError:
            time.sleep(0.2)

check("测试夹具已清理", not FIXTURE_DB.exists())
check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
check(
    "knowledge/ 未被修改",
    sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    ) == KNOWLEDGE_SNAPSHOT,
)
check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)

print("\n全部用例通过")
