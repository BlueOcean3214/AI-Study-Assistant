import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

import env_loader

# 启动时自动读取根目录 .env（FEEDBACK_DB_PATH 等配置可能来自 .env）
env_loader.load_env()


# 数据库路径：默认项目根目录下的 feedback.db。
# 可用环境变量 FEEDBACK_DB_PATH 覆盖（测试隔离 / 部署配置用，与 RAG_CACHE_DIR 同一风格）。
DATABASE_PATH = Path(
    os.environ.get("FEEDBACK_DB_PATH")
    or (Path(__file__).resolve().parent / "feedback.db")
)
BACKUP_PATH = Path(__file__).resolve().parent / "feedback_backup.json"

# plans.status 允许值
PLAN_STATUS_ACTIVE = "active"


class ActivePlanExistsError(RuntimeError):
    """同一天已经存在 active plan。"""


# ==========================
# 版本化迁移（PRAGMA user_version）
# ==========================

# 当前 schema 版本：每次结构变更追加一个迁移函数，禁止修改已有迁移
SCHEMA_VERSION = 5


def _migration_v1_feedbacks(conn):
    """v1：feedbacks 主表。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            estimated_minutes INTEGER NOT NULL,
            actual_minutes INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            completed_subtasks INTEGER NOT NULL DEFAULT 0,
            total_subtasks INTEGER NOT NULL DEFAULT 0,
            completed_questions INTEGER NOT NULL DEFAULT 0,
            total_questions INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )


def _migration_v2_feedback_progress_columns(conn):
    """v2：给旧版 feedbacks 补进度字段（只加列，不删改既有数据）。"""

    columns = conn.execute("PRAGMA table_info(feedbacks)").fetchall()
    column_names = {column[1] for column in columns}
    new_columns = {
        "completed_subtasks": "INTEGER NOT NULL DEFAULT 0",
        "total_subtasks": "INTEGER NOT NULL DEFAULT 0",
        "completed_questions": "INTEGER NOT NULL DEFAULT 0",
        "total_questions": "INTEGER NOT NULL DEFAULT 0",
    }
    for column_name, column_definition in new_columns.items():
        if column_name not in column_names:
            conn.execute(
                f"ALTER TABLE feedbacks ADD COLUMN {column_name} {column_definition}"
            )


def _migration_v3_plans(conn):
    """v3：plans 表 + 当天 active 唯一的部分唯一索引。"""

    # 学习计划（与 feedbacks 暂时是两个独立实体，不互相引用）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_date TEXT NOT NULL,
            task TEXT NOT NULL,
            estimated_minutes INTEGER NOT NULL,
            difficulty TEXT NOT NULL,
            question_count INTEGER NOT NULL,
            reason TEXT,
            completion_criteria TEXT,
            subtasks_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )

    # 同一天最多一条 active plan：用 partial unique index，而不是 UNIQUE(plan_date)，
    # 这样将来同一天可以同时保留 completed / cancelled 的历史计划。
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_active_date
        ON plans(plan_date) WHERE status = 'active'
        """
    )


def _migration_v4_query_indexes(conn):
    """v4：按实际查询补索引（最近反馈时间排序 / 计划日期+状态）。"""

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedbacks_created_at ON feedbacks(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plans_date_status ON plans(plan_date, status)"
    )


def _migration_v5_model_usage(conn):
    """v5：模型用量记录表（token 用量/耗时/状态；不存 Prompt、回答、密钥）。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            usage_available INTEGER NOT NULL DEFAULT 0,
            elapsed_seconds REAL,
            ok INTEGER NOT NULL,
            error_type TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_usage_created_at ON model_usage(created_at)"
    )


# 迁移按版本号升序执行；每个迁移都必须可重复执行（IF NOT EXISTS / 存在性检查）
MIGRATIONS = (
    (1, _migration_v1_feedbacks),
    (2, _migration_v2_feedback_progress_columns),
    (3, _migration_v3_plans),
    (4, _migration_v4_query_indexes),
    (5, _migration_v5_model_usage),
)


def init_db():
    """初始化 / 升级数据库（唯一启动入口，可重复执行）。

    - 用 PRAGMA user_version 记录 schema 版本；高版本库拒绝启动（禁止降级写入）
    - 迁移全部是幂等的（IF NOT EXISTS / 存在性检查）：每次都顺序执行，
      因此重复调用不重复加列/索引，个别表被误删后还能自动补回
    - 旧库（含只有 feedbacks 的库、user_version=0 的库）升级不删任何数据
    - 迁移失败抛 RuntimeError（信息里带路径与版本号），启动方能清晰报错
    """

    conn = sqlite3.connect(DATABASE_PATH)

    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}，"
                "请升级程序，禁止降级写入"
            )

        for target_version, migration in MIGRATIONS:
            migration(conn)

        if version != SCHEMA_VERSION:
            # PRAGMA 不支持参数绑定；SCHEMA_VERSION 是本模块内的整数常量
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

        conn.commit()
    except sqlite3.Error as error:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.rollback()
        raise RuntimeError(
            f"数据库迁移失败（DATABASE_PATH={DATABASE_PATH}，user_version={current}）：{error}"
        ) from error
    finally:
        conn.close()


def migrate_json_to_db():
    if not BACKUP_PATH.exists():
        return

    conn = sqlite3.connect(DATABASE_PATH)
    count = conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
    if count == 0:
        try:
            with BACKUP_PATH.open("r", encoding="utf-8") as file:
                feedback_list = json.load(file)
            for item in feedback_list:
                conn.execute(
                    """
                    INSERT INTO feedbacks
                    (task, estimated_minutes, actual_minutes, status, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["task"], item["estimated_minutes"],
                        item["actual_minutes"], item["status"],
                        item.get("reason"), item["created_at"],
                    ),
                )
            conn.commit()
        except (OSError, json.JSONDecodeError, KeyError, TypeError, sqlite3.Error):
            # Keep the database available if an old optional backup is malformed.
            conn.rollback()
    conn.close()


def get_feedback_list():
    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, task, estimated_minutes, actual_minutes, status, reason,
                   completed_subtasks, total_subtasks, completed_questions,
                   total_questions, created_at
            FROM feedbacks ORDER BY id
            """
        ).fetchall()
    return [
        {
            "id": row[0], "task": row[1], "estimated_minutes": row[2],
            "actual_minutes": row[3], "status": row[4], "reason": row[5],
            "completed_subtasks": row[6], "total_subtasks": row[7],
            "completed_questions": row[8], "total_questions": row[9],
            "created_at": row[10],
        }
        for row in rows
    ]


def _clamp_int(value, default, minimum, maximum):
    """把参数钳制到 [minimum, maximum]；非法值（非整数/布尔）回退默认值。"""

    if isinstance(value, bool) or not isinstance(value, int):
        return default

    return max(minimum, min(value, maximum))


def _normalize_days(value):
    """days 归一化：非整数/布尔、或小于 1 都视为非法，回退 7；大于 30 钳制到 30。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 7

    return min(value, 30)


def get_feedback_since(days=7, limit=20):
    """返回最近 days 天内的反馈，按 created_at 倒序，最多 limit 条。

    - days 默认 7，范围 1~30：非整数/布尔或小于 1 回退 7，大于 30 钳制到 30。
      调用方即使已经校验过，这里仍然再兜底一次。
    - limit 默认 20，钳制到 1~20：数据库层不返回无限历史。
    - created_at 是 datetime.now().isoformat() 写入的“本地无时区”字符串，
      同格式下字典序等价于时间序，所以这里直接做字符串比较（本阶段不重构时间系统）。
    - 返回结构与 get_feedback_list() 一致（含 id）；摘要由上层负责，数据库层不做摘要。
    - 不改动 get_feedback_list()，现有 API 继续依赖它。
    """

    days = _normalize_days(days)
    limit = _clamp_int(limit, 20, 1, 20)

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, task, estimated_minutes, actual_minutes, status, reason,
                   completed_subtasks, total_subtasks, completed_questions,
                   total_questions, created_at
            FROM feedbacks
            WHERE created_at >= ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

    return [
        {
            "id": row[0], "task": row[1], "estimated_minutes": row[2],
            "actual_minutes": row[3], "status": row[4], "reason": row[5],
            "completed_subtasks": row[6], "total_subtasks": row[7],
            "completed_questions": row[8], "total_questions": row[9],
            "created_at": row[10],
        }
        for row in rows
    ]


def insert_feedback(feedback_data, created_at):
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions,
             total_questions, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_data["task"], feedback_data["estimated_minutes"],
                feedback_data["actual_minutes"], feedback_data["status"],
                feedback_data["reason"], feedback_data["completed_subtasks"],
                feedback_data["total_subtasks"], feedback_data["completed_questions"],
                feedback_data["total_questions"], created_at,
            ),
        )


def delete_all_feedback():
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("DELETE FROM feedbacks")


# ==========================
# plans：学习计划持久化
# ==========================


def _plan_row_to_dict(row):
    """plans 行 -> 业务字典（subtasks_json 原样返回，由上层解析）。"""

    return {
        "id": row[0],
        "plan_date": row[1],
        "task": row[2],
        "estimated_minutes": row[3],
        "difficulty": row[4],
        "question_count": row[5],
        "reason": row[6],
        "completion_criteria": row[7],
        "subtasks_json": row[8],
        "status": row[9],
        "created_at": row[10],
    }


def get_active_plan_row(plan_date):
    """读取指定日期的 active plan，返回 dict 或 None。"""

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, plan_date, task, estimated_minutes, difficulty, question_count,
                   reason, completion_criteria, subtasks_json, status, created_at
            FROM plans
            WHERE plan_date = ? AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (plan_date, PLAN_STATUS_ACTIVE),
        ).fetchone()

    return _plan_row_to_dict(row) if row else None


def insert_plan(plan_date, plan, created_at, status=PLAN_STATUS_ACTIVE):
    """在同一个写事务里检查“当天 active plan 唯一”并插入，返回新的 plan id。

    plan 是已归一化的计划字典，必须包含：
        task / estimated_minutes / difficulty / question_count /
        reason / completion_criteria / subtasks_json

    - 显式 BEGIN IMMEDIATE：写事务立即取锁，避免并发下两个请求都通过检查
    - 事务内先 SELECT 检查，再 INSERT；partial unique index 是最后一道防线
    - 冲突抛 ActivePlanExistsError；其他 sqlite3 错误原样抛出（由上层转 database_error）
    """

    conn = sqlite3.connect(DATABASE_PATH)
    conn.isolation_level = None  # 手动管理事务

    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT id FROM plans WHERE plan_date = ? AND status = ?",
            (plan_date, PLAN_STATUS_ACTIVE),
        ).fetchone()

        if existing:
            conn.rollback()
            raise ActivePlanExistsError(f"{plan_date} 已经存在 active plan")

        cursor = conn.execute(
            """
            INSERT INTO plans
            (plan_date, task, estimated_minutes, difficulty, question_count,
             reason, completion_criteria, subtasks_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_date, plan["task"], plan["estimated_minutes"], plan["difficulty"],
                plan["question_count"], plan["reason"], plan["completion_criteria"],
                plan["subtasks_json"], status, created_at,
            ),
        )

        conn.commit()

        return cursor.lastrowid
    except ActivePlanExistsError:
        raise
    except sqlite3.IntegrityError as error:
        conn.rollback()

        # 只有唯一索引冲突才是“当天已有 active plan”，其他约束错误照实抛出
        if "UNIQUE" in str(error).upper():
            raise ActivePlanExistsError(f"{plan_date} 已经存在 active plan") from error

        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
