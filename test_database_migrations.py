"""数据库迁移 / 索引 / 用量记录测试：直接运行 python test_database_migrations.py。

覆盖：
- 新数据库首次初始化（user_version / 表 / 索引）
- 从已有旧库（user_version=0、缺进度列）迁移且数据不丢
- feedback_backup.json 旧数据迁移兼容（幂等）
- 迁移重复执行不重复加列/索引
- 已有计划与反馈迁移后仍可读
- 当天 active 计划唯一索引 + insert_plan 事务（BEGIN IMMEDIATE）并发冲突
- model_usage 写入 / usage 缺失 NULL / 记录失败不影响主业务 / 不存 Prompt
- 数据库版本高于程序支持时清晰报错
- 参数化 SQL（注入串不会被执行）
- 真实 feedback.db 与 feedback_backup.json 不被改动

注意（Windows）：sqlite 连接必须显式 close，否则文件被占用无法删除夹具。
"""

import gc
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import database
import model_usage


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_migrate.db"
FIXTURE_BACKUP = PROJECT_DIR / "_test_backup.json"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_BACKUP = PROJECT_DIR / "feedback_backup.json"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def run_sql(sql, params=(), db_path=None):
    """执行 SQL 并显式关闭连接；返回全部行。"""

    conn = sqlite3.connect(db_path or FIXTURE_DB)

    try:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


def scalar(sql, params=(), db_path=None):
    rows = run_sql(sql, params, db_path)

    return rows[0][0] if rows else None


def plan_dict(task="迁移测试计划"):
    return {
        "task": task,
        "estimated_minutes": 45,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "迁移测试",
        "completion_criteria": "完成4道题",
        "subtasks_json": "[]",
    }


def cleanup_fixture():
    """尽力而为的夹具清理：Windows 上 sqlite 句柄释放有延迟，重试后仍删不掉就跳过
    （残留的 _test_ 夹具会在下次运行开头被再次清理）。"""

    for _ in range(5):
        gc.collect()

        remaining = []

        for path in (FIXTURE_DB, Path(str(FIXTURE_DB) + "-journal"),
                     Path(str(FIXTURE_DB) + "-wal"), Path(str(FIXTURE_DB) + "-shm"),
                     FIXTURE_BACKUP):
            if path.exists():
                try:
                    path.unlink()
                except PermissionError:
                    remaining.append(path)

        if not remaining:
            return

        time.sleep(0.2)


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
REAL_BACKUP_BYTES = REAL_BACKUP.read_bytes() if REAL_BACKUP.exists() else None

cleanup_fixture()
database.DATABASE_PATH = FIXTURE_DB
database.BACKUP_PATH = FIXTURE_BACKUP

try:
    # ==========================
    # 1. 新数据库首次初始化
    # ==========================

    database.init_db()

    version = scalar("PRAGMA user_version")
    tables = {row[0] for row in run_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {row[0] for row in run_sql("SELECT name FROM sqlite_master WHERE type='index'")}

    check("新库 user_version = SCHEMA_VERSION",
          version == database.SCHEMA_VERSION, f"version={version}")
    check("新库包含全部业务表",
          {"feedbacks", "plans", "model_usage"} <= tables, f"tables={sorted(tables)}")
    check("查询索引已创建",
          {"idx_feedbacks_created_at", "idx_plans_date_status",
           "idx_model_usage_created_at", "idx_plans_active_date"} <= indexes,
          f"indexes={sorted(indexes)}")

    # ==========================
    # 2. 迁移重复执行
    # ==========================

    database.init_db()
    database.init_db()

    version_again = scalar("PRAGMA user_version")
    index_count = scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='index'")
    feedback_columns = [row[1] for row in run_sql("PRAGMA table_info(feedbacks)")]

    check("重复执行后 user_version 不变", version_again == database.SCHEMA_VERSION)
    check("重复执行不重复建索引", index_count == len(indexes), f"count={index_count}")
    check("重复执行不重复加列", len(feedback_columns) == len(set(feedback_columns)))

    # ==========================
    # 3. 从旧库迁移（user_version=0、缺进度列）且数据不丢
    # ==========================

    cleanup_fixture()

    run_sql(
        """
        CREATE TABLE feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            estimated_minutes INTEGER NOT NULL,
            actual_minutes INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    run_sql(
        "INSERT INTO feedbacks (task, estimated_minutes, actual_minutes, status, reason, created_at)"
        " VALUES ('旧反馈', 30, 25, 'completed', NULL, '2026-10-01T09:00:00')"
    )

    database.init_db()

    version_old = scalar("PRAGMA user_version")
    columns = {row[1] for row in run_sql("PRAGMA table_info(feedbacks)")}
    rows = run_sql("SELECT task, completed_subtasks, total_subtasks FROM feedbacks")

    check("旧库升级到当前版本", version_old == database.SCHEMA_VERSION, f"version={version_old}")
    check("旧库补上进度列",
          {"completed_subtasks", "total_subtasks",
           "completed_questions", "total_questions"} <= columns)
    check("旧库存量反馈仍可读", rows == [("旧反馈", 0, 0)], f"rows={rows}")

    # ==========================
    # 4. feedback_backup.json 兼容迁移（migrate_json_to_db 只在空表时导入）
    # ==========================

    cleanup_fixture()
    database.init_db()

    FIXTURE_BACKUP.write_text(
        json.dumps([
            {"task": "备份反馈", "estimated_minutes": 20, "actual_minutes": 20,
             "status": "completed", "reason": None, "created_at": "2026-10-02T09:00:00"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    database.migrate_json_to_db()
    backup_rows = run_sql("SELECT task FROM feedbacks WHERE task = '备份反馈'")
    check("JSON 备份迁移进库", backup_rows == [("备份反馈",)])

    database.migrate_json_to_db()
    duplicate_count = scalar("SELECT COUNT(*) FROM feedbacks WHERE task = '备份反馈'")
    check("JSON 备份迁移不重复导入", duplicate_count == 1)

    # ==========================
    # 5. 已有计划 / 反馈在重跑 init_db 后仍可读
    # ==========================

    plan_id = database.insert_plan("2026-10-10", plan_dict(), "2026-10-10T08:00:00")
    database.init_db()

    plan_row = run_sql("SELECT task FROM plans WHERE id = ?", (plan_id,))
    feedback_count = scalar("SELECT COUNT(*) FROM feedbacks")

    check("重跑 init_db 后计划仍可读", plan_row == [("迁移测试计划",)])
    check("重跑 init_db 后反馈仍可读", feedback_count == 1, f"count={feedback_count}")

    # ==========================
    # 6. 唯一索引与事务
    # ==========================

    try:
        database.insert_plan("2026-10-10", plan_dict("同天第二份"), "2026-10-10T09:00:00")
        check("同天 active 计划唯一约束生效", False)
    except database.ActivePlanExistsError:
        check("同天 active 计划唯一约束生效", True)

    other_day_id = database.insert_plan("2026-10-11", plan_dict("另一天"), "2026-10-11T08:00:00")
    check("不同天可以各有 active 计划", isinstance(other_day_id, int))

    barrier = threading.Barrier(2)
    outcomes = []
    outcomes_lock = threading.Lock()

    def concurrent_insert(tag):
        barrier.wait()
        try:
            database.insert_plan("2026-10-12", plan_dict(f"并发-{tag}"), "2026-10-12T08:00:00")
            result = "saved"
        except database.ActivePlanExistsError:
            result = "conflict"
        except Exception as error:  # noqa: BLE001
            result = f"other:{type(error).__name__}"

        with outcomes_lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=concurrent_insert, args=(index,))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    check("并发插入恰好一次成功",
          sorted(outcomes) == ["conflict", "saved"], f"outcomes={outcomes}")

    # ==========================
    # 7. model_usage
    # ==========================

    written = model_usage.record_usage({
        "provider": "ollama",
        "model": "qwen3.5:4b",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "usage_available": True,
        "elapsed_seconds": 1.23,
        "ok": True,
        "error_type": None,
    })
    check("用量记录写入成功", written is True)

    written_missing = model_usage.record_usage({
        "provider": "openai_compatible",
        "model": "gpt-test",
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "usage_available": False,
        "elapsed_seconds": 2.0,
        "ok": False,
        "error_type": "timeout",
    })
    check("usage 缺失时记录 NULL 而不是估算", written_missing is True)

    records = model_usage.recent_usage(10)
    check("用量记录可读且字段正确",
          len(records) == 2
          and records[1]["prompt_tokens"] == 10 and records[1]["usage_available"] is True
          and records[0]["prompt_tokens"] is None and records[0]["usage_available"] is False
          and records[0]["error_type"] == "timeout",
          f"records={records}")

    usage_columns = {row[1] for row in run_sql("PRAGMA table_info(model_usage)")}
    check("用量表不含 Prompt/回答/密钥字段",
          not ({"prompt", "messages", "content", "answer", "api_key"} & usage_columns),
          f"columns={sorted(usage_columns)}")

    # 记录失败不影响主业务：表被删后 record_usage 只返回 False
    run_sql("DROP TABLE model_usage")
    silent = model_usage.record_usage({
        "provider": "ollama", "model": "x", "ok": True, "usage_available": False,
    })
    check("记录失败返回 False 且不抛异常", silent is False)

    # 表被误删后 init_db 的幂等迁移自动补回
    database.init_db()
    recovered = scalar(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_usage'"
    )
    check("init_db 自动补回被误删的表", recovered == 1)

    # 开关：MODEL_USAGE_ENABLED=0 时不写库
    os.environ["MODEL_USAGE_ENABLED"] = "0"
    try:
        disabled = model_usage.record_usage({"provider": "ollama", "model": "x", "ok": True})
        check("MODEL_USAGE_ENABLED=0 时不写用量", disabled is False)
    finally:
        os.environ.pop("MODEL_USAGE_ENABLED", None)

    # ==========================
    # 8. 版本过高清晰报错 / 参数化 SQL
    # ==========================

    run_sql(f"PRAGMA user_version = {database.SCHEMA_VERSION + 1}")

    try:
        database.init_db()
        check("版本过高时报清晰错误", False)
    except RuntimeError as error:
        check("版本过高时报清晰错误",
              "高于程序支持" in str(error) and str(database.SCHEMA_VERSION) in str(error))

    run_sql(f"PRAGMA user_version = {database.SCHEMA_VERSION}")

    injection = "Robert'); DROP TABLE feedbacks;--"
    database.insert_feedback(
        {
            "task": injection, "estimated_minutes": 1, "actual_minutes": 1,
            "status": "completed", "reason": None,
            "completed_subtasks": 0, "total_subtasks": 0,
            "completed_questions": 0, "total_questions": 0,
        },
        "2026-10-13T09:00:00",
    )

    tables_after = {row[0] for row in run_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    injected = run_sql("SELECT task FROM feedbacks WHERE task = ?", (injection,))

    check("注入串被参数化存储，不被当作 SQL 执行",
          "feedbacks" in tables_after and injected == [(injection,)])

finally:
    database.DATABASE_PATH = PROJECT_DIR / "feedback.db"
    database.BACKUP_PATH = PROJECT_DIR / "feedback_backup.json"
    cleanup_fixture()

    now_db = REAL_DB.read_bytes() if REAL_DB.exists() else None
    now_backup = REAL_BACKUP.read_bytes() if REAL_BACKUP.exists() else None

    check("真实 feedback.db 未被改动", now_db == REAL_DB_BYTES)
    check("真实 feedback_backup.json 未被改动", now_backup == REAL_BACKUP_BYTES)

print("ALL TESTS COMPLETED")
