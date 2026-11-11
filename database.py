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
SCHEMA_VERSION = 7


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


def _migration_v6_feedback_plan_link(conn):
    """v6：feedbacks 增加可空 plan_id，并建索引（历史数据保持 NULL）。"""

    # 旧反馈没有可靠的计划来源，一律留 NULL：回填等于伪造关联
    columns = {row[1] for row in conn.execute("PRAGMA table_info(feedbacks)")}

    if "plan_id" not in columns:
        conn.execute("ALTER TABLE feedbacks ADD COLUMN plan_id INTEGER")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedbacks_plan_id ON feedbacks(plan_id)"
    )


def _read_feedback_high_water(conn):
    """feedbacks 的已用高水位：max(sqlite_sequence.seq, MAX(id))。

    为什么不能只看 MAX(id)：旧版 delete_all_feedback() 只 DELETE 不清序列，
    表被清空后 MAX(id) 回到 0，而下一个反馈会拿到「旧序列 + 1」。只按 MAX(id)
    初始化游标会把这段历史 id 区间当成待处理空洞。sqlite_sequence 表只在存在
    AUTOINCREMENT 表时才被创建，因此先查存在性。
    """

    seq = 0

    has_sequence = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone()

    if has_sequence is not None:
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'feedbacks'"
        ).fetchone()
        if row is not None and row[0] is not None:
            seq = int(row[0])

    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM feedbacks").fetchone()
    max_id = int(row[0] or 0) if row is not None else 0

    return max(seq, max_id)


def _migration_v7_learning_memory(conn):
    """v7：学习记忆五张表（memories / evidence / events / pending / cursor）。

    - 只建结构，不给历史反馈回填任何记忆（无可靠依据，回填等于伪造）
    - 游标行必须真实写入：列默认 0 只对「插入时的缺省」生效，只建表不插行
      会让读取方拿到 NULL；初值取迁移时刻的已用高水位，保证升级不会把老库
      的全部历史反馈当成待处理
    - 五张表都用 IF NOT EXISTS + 唯一索引，可重复执行
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            subject_label TEXT NOT NULL,
            content TEXT NOT NULL,
            value_json TEXT NOT NULL,
            strength REAL NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            contradict_count INTEGER NOT NULL DEFAULT 0,
            episode INTEGER NOT NULL DEFAULT 1,
            episode_start_feedback_id INTEGER,
            episode_started_at TEXT,
            pending_episode INTEGER,
            pending_start_feedback_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            first_seen_at TEXT NOT NULL,
            last_supported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_type_subject "
        "ON learning_memories(memory_type, subject_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_status_updated "
        "ON learning_memories(status, updated_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            feedback_id INTEGER NOT NULL,
            plan_id INTEGER,
            episode INTEGER NOT NULL DEFAULT 1,
            relation TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL
        )
        """
    )

    # 一条反馈对同一条记忆只有一行；写入后 relation / episode 不再改（证据不可变）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_memory_feedback "
        "ON memory_evidence(memory_id, feedback_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_memory_episode "
        "ON memory_evidence(memory_id, episode, relation)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_evidence_feedback "
        "ON memory_evidence(feedback_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_memory "
        "ON memory_events(memory_id, created_at)"
    )

    # 缓冲区不存 relation：方向未定时先入库，达门槛时再按已固定的 direction 判定
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_pending_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            feedback_id INTEGER NOT NULL,
            plan_id INTEGER,
            episode INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_memory_feedback "
        "ON memory_pending_evidence(memory_id, feedback_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_memory_episode "
        "ON memory_pending_evidence(memory_id, episode)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_apply_cursor (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_applied_feedback_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )

    migration_as_of = datetime.now().isoformat()

    conn.execute(
        """
        INSERT INTO memory_apply_cursor (id, last_applied_feedback_id, updated_at)
        SELECT 1,
               MAX(COALESCE(
                       (SELECT seq FROM sqlite_sequence WHERE name = 'feedbacks'), 0
                   ),
                   COALESCE((SELECT MAX(id) FROM feedbacks), 0)
               ),
               ?
        WHERE NOT EXISTS (SELECT 1 FROM memory_apply_cursor WHERE id = 1)
        """,
        (migration_as_of,),
    )


# 迁移按版本号升序执行；每个迁移都必须可重复执行（IF NOT EXISTS / 存在性检查）
MIGRATIONS = (
    (1, _migration_v1_feedbacks),
    (2, _migration_v2_feedback_progress_columns),
    (3, _migration_v3_plans),
    (4, _migration_v4_query_indexes),
    (5, _migration_v5_model_usage),
    (6, _migration_v6_feedback_plan_link),
    (7, _migration_v7_learning_memory),
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
                   total_questions, created_at, plan_id
            FROM feedbacks ORDER BY id
            """
        ).fetchall()
    return [
        {
            "id": row[0], "task": row[1], "estimated_minutes": row[2],
            "actual_minutes": row[3], "status": row[4], "reason": row[5],
            "completed_subtasks": row[6], "total_subtasks": row[7],
            "completed_questions": row[8], "total_questions": row[9],
            "created_at": row[10], "plan_id": row[11],
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
                   total_questions, created_at, plan_id
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
            "created_at": row[10], "plan_id": row[11],
        }
        for row in rows
    ]


def insert_feedback(feedback_data, created_at):
    """写入一条学习反馈，返回新行的 feedbacks.id。

    plan_id 用 .get() 读取：既有调用方（测试、agent_eval）传来的 dict 没有这个键，
    必须保持“不带计划 ID 也能写”的旧行为。
    返回值是第二阶段新增的（记忆更新要按 id 追溯来源），旧调用方忽略即可。
    """

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions,
             total_questions, created_at, plan_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback_data["task"], feedback_data["estimated_minutes"],
                feedback_data["actual_minutes"], feedback_data["status"],
                feedback_data["reason"], feedback_data["completed_subtasks"],
                feedback_data["total_subtasks"], feedback_data["completed_questions"],
                feedback_data["total_questions"], created_at,
                feedback_data.get("plan_id"),
            ),
        )
        return cursor.lastrowid


def delete_all_feedback():
    """清空反馈与由反馈派生的记忆数据（同一事务）。

    顺序固定为子表 -> 父表：memory_events / memory_evidence /
    memory_pending_evidence / learning_memories / feedbacks。
    - 清空就是清空：不保留归档记忆，也不写 deleted 事件（写了也会被同事务删掉）
    - 游标不重置为 0：DELETE 不会回退 sqlite_sequence，若把游标写回 0，之后新反馈
      拿到「旧序列 + 1」，中间那段已删 id 会被当成待处理历史；因此写回清空前的
      已用高水位，与新反馈的 id 正好衔接
    """

    with sqlite3.connect(DATABASE_PATH) as conn:
        high_water = _read_feedback_high_water(conn)
        cleared_at = datetime.now().isoformat()

        conn.execute("DELETE FROM memory_events")
        conn.execute("DELETE FROM memory_evidence")
        conn.execute("DELETE FROM memory_pending_evidence")
        conn.execute("DELETE FROM learning_memories")
        conn.execute("DELETE FROM feedbacks")

        conn.execute(
            """
            INSERT INTO memory_apply_cursor (id, last_applied_feedback_id, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_applied_feedback_id = excluded.last_applied_feedback_id,
                updated_at = excluded.updated_at
            """,
            (high_water, cleared_at),
        )


# ==========================
# 学习记忆：SQL 层（业务规则在 memory_service，本层只做读写）
# ==========================

# update_memory_state 允许改的列：白名单，避免把任意字段名拼进 SQL
MEMORY_STATE_FIELDS = (
    "content",
    "value_json",
    "strength",
    "evidence_count",
    "contradict_count",
    "episode",
    "episode_start_feedback_id",
    "episode_started_at",
    "pending_episode",
    "pending_start_feedback_id",
    "status",
    "last_supported_at",
    "updated_at",
)


def _require_int(value, name, minimum=None):
    """严格整数校验：bool 不算整数，字符串不做隐式转换。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {type(value).__name__}")

    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}，收到 {value}")

    return value


def _require_text(value, name, allow_empty=False):
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串，收到 {type(value).__name__}")

    if not allow_empty and not value.strip():
        raise ValueError(f"{name} 不能为空")

    return value


# ---------- 处理游标 ----------


def read_cursor():
    """读取最后一条已处理反馈的 id（没有游标行时返回 0，不写库）。"""

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT last_applied_feedback_id FROM memory_apply_cursor WHERE id = 1"
        ).fetchone()

    if row is None or row[0] is None:
        return 0

    return int(row[0])


def write_cursor(feedback_id, updated_at):
    """写入游标（两参数，updated_at 必填非空）。

    updated_at 是 NOT NULL 列，且不参与语义投影比较；调用方必须传入已求值的时刻
    （例如 apply_pending_updates 在事务开始前算好的 effective_now），本层不做
    None 兜底，避免把缺失的时间悄悄写成当前时刻。
    """

    _require_int(feedback_id, "feedback_id", minimum=0)
    _require_text(updated_at, "updated_at")

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            INSERT INTO memory_apply_cursor (id, last_applied_feedback_id, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_applied_feedback_id = excluded.last_applied_feedback_id,
                updated_at = excluded.updated_at
            """,
            (feedback_id, updated_at),
        )


# ---------- 记忆主体 ----------


def _memory_row_to_dict(row):
    return {
        "id": row[0],
        "memory_type": row[1],
        "subject_key": row[2],
        "subject_label": row[3],
        "content": row[4],
        "value_json": row[5],
        "strength": row[6],
        "evidence_count": row[7],
        "contradict_count": row[8],
        "episode": row[9],
        "episode_start_feedback_id": row[10],
        "episode_started_at": row[11],
        "pending_episode": row[12],
        "pending_start_feedback_id": row[13],
        "status": row[14],
        "first_seen_at": row[15],
        "last_supported_at": row[16],
        "updated_at": row[17],
    }


MEMORY_COLUMNS = (
    "id, memory_type, subject_key, subject_label, content, value_json, strength, "
    "evidence_count, contradict_count, episode, episode_start_feedback_id, "
    "episode_started_at, pending_episode, pending_start_feedback_id, status, "
    "first_seen_at, last_supported_at, updated_at"
)


def list_memories(memory_type=None, subject_key=None, status=None, limit=10):
    """按类型 / 主题键 / 状态过滤，按 strength 降序返回。"""

    limit = _require_int(limit, "limit", minimum=1)
    limit = min(limit, 100)

    sql = f"SELECT {MEMORY_COLUMNS} FROM learning_memories WHERE 1 = 1"
    params = []

    if memory_type is not None:
        sql += " AND memory_type = ?"
        params.append(memory_type)

    if subject_key is not None:
        sql += " AND subject_key = ?"
        params.append(subject_key)

    if status is not None:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY strength DESC, id ASC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [_memory_row_to_dict(row) for row in rows]


def get_memory_by_key(memory_type, subject_key):
    _require_text(memory_type, "memory_type")
    _require_text(subject_key, "subject_key")

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            f"SELECT {MEMORY_COLUMNS} FROM learning_memories "
            "WHERE memory_type = ? AND subject_key = ?",
            (memory_type, subject_key),
        ).fetchone()

    return _memory_row_to_dict(row) if row is not None else None


def insert_memory(memory, first_seen_at):
    """插入记忆；唯一索引冲突时不抛错，返回 inserted=False。

    调用方必须核对 inserted：把「已存在」当成「刚创建」会让事件与计数错乱。
    """

    _require_text(memory["memory_type"], "memory_type")
    _require_text(memory["subject_key"], "subject_key")
    _require_text(memory["content"], "content")
    _require_text(memory["value_json"], "value_json")
    _require_text(first_seen_at, "first_seen_at")

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO learning_memories
            (memory_type, subject_key, subject_label, content, value_json, strength,
             evidence_count, contradict_count, episode, episode_start_feedback_id,
             episode_started_at, pending_episode, pending_start_feedback_id, status,
             first_seen_at, last_supported_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory["memory_type"], memory["subject_key"],
                memory["subject_label"], memory["content"], memory["value_json"],
                memory.get("strength", 0), memory.get("evidence_count", 0),
                memory.get("contradict_count", 0), memory.get("episode", 1),
                memory.get("episode_start_feedback_id"),
                memory.get("episode_started_at"),
                memory.get("pending_episode"),
                memory.get("pending_start_feedback_id"),
                memory.get("status", "active"),
                first_seen_at,
                memory.get("last_supported_at", first_seen_at),
                memory.get("updated_at", first_seen_at),
            ),
        )

        inserted = cursor.rowcount == 1
        return {"inserted": inserted, "row_id": cursor.lastrowid if inserted else None}


def update_memory_state(memory_id, fields):
    """更新状态必填列（白名单）。返回 True 表示确实更新到一行。"""

    _require_int(memory_id, "memory_id", minimum=1)

    if not fields:
        raise ValueError("fields 不能为空")

    unknown = [name for name in fields if name not in MEMORY_STATE_FIELDS]
    if unknown:
        raise ValueError(f"不允许更新的字段：{unknown}")

    assignments = ", ".join(f"{name} = ?" for name in fields)
    params = list(fields.values()) + [memory_id]

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            f"UPDATE learning_memories SET {assignments} WHERE id = ?", params
        )
        return cursor.rowcount == 1


# ---------- 证据 ----------


def _evidence_row_to_dict(row):
    return {
        "id": row[0],
        "memory_id": row[1],
        "feedback_id": row[2],
        "plan_id": row[3],
        "episode": row[4],
        "relation": row[5],
        "weight": row[6],
        "created_at": row[7],
    }


EVIDENCE_COLUMNS = (
    "id, memory_id, feedback_id, plan_id, episode, relation, weight, created_at"
)


def insert_evidence(memory_id, feedback_id, plan_id, episode, relation, created_at,
                    weight=1.0):
    """插入一条证据；冲突时不覆盖（证据不可变）。

    返回 {"inserted": bool, "row_id": int|None, "row": dict}：
    调用方拿到 inserted=False 时必须读回已有行核对 relation / episode / plan_id，
    不一致说明数据被改过，应报 data_inconsistency 而不是静默继续。
    """

    _require_int(memory_id, "memory_id", minimum=1)
    _require_int(feedback_id, "feedback_id", minimum=1)
    _require_int(episode, "episode", minimum=1)
    _require_text(relation, "relation")
    _require_text(created_at, "created_at")

    if plan_id is not None:
        _require_int(plan_id, "plan_id", minimum=1)

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO memory_evidence
            (memory_id, feedback_id, plan_id, episode, relation, weight, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, feedback_id, plan_id, episode, relation, weight, created_at),
        )

        row = conn.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM memory_evidence "
            "WHERE memory_id = ? AND feedback_id = ?",
            (memory_id, feedback_id),
        ).fetchone()

    return {
        "inserted": cursor.rowcount == 1,
        "row_id": cursor.lastrowid if cursor.rowcount == 1 else None,
        "row": _evidence_row_to_dict(row) if row is not None else None,
    }


def get_evidence(memory_id, feedback_id):
    _require_int(memory_id, "memory_id", minimum=1)
    _require_int(feedback_id, "feedback_id", minimum=1)

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            f"SELECT {EVIDENCE_COLUMNS} FROM memory_evidence "
            "WHERE memory_id = ? AND feedback_id = ?",
            (memory_id, feedback_id),
        ).fetchone()

    return _evidence_row_to_dict(row) if row is not None else None


def list_evidence(memory_id, episode=None, relation=None, limit=None):
    """按 feedback_id 升序返回证据行（回放顺序固定）。"""

    _require_int(memory_id, "memory_id", minimum=1)

    sql = f"SELECT {EVIDENCE_COLUMNS} FROM memory_evidence WHERE memory_id = ?"
    params = [memory_id]

    if episode is not None:
        _require_int(episode, "episode", minimum=1)
        sql += " AND episode = ?"
        params.append(episode)

    if relation is not None:
        _require_text(relation, "relation")
        sql += " AND relation = ?"
        params.append(relation)

    sql += " ORDER BY feedback_id ASC"

    if limit is not None:
        limit = _require_int(limit, "limit", minimum=1)
        sql += " LIMIT ?"
        params.append(limit)

    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [_evidence_row_to_dict(row) for row in rows]


def max_evidence_feedback_id(memory_id, episode=None):
    """该记忆（可限定轮次）已计入的最大 feedback id；无证据返回 0。"""

    _require_int(memory_id, "memory_id", minimum=1)

    sql = "SELECT COALESCE(MAX(feedback_id), 0) FROM memory_evidence WHERE memory_id = ?"
    params = [memory_id]

    if episode is not None:
        _require_int(episode, "episode", minimum=1)
        sql += " AND episode = ?"
        params.append(episode)

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(sql, params).fetchone()

    return int(row[0] or 0)


def count_evidence(memory_id, episode=None, relation=None):
    _require_int(memory_id, "memory_id", minimum=1)

    sql = "SELECT COUNT(*) FROM memory_evidence WHERE memory_id = ?"
    params = [memory_id]

    if episode is not None:
        _require_int(episode, "episode", minimum=1)
        sql += " AND episode = ?"
        params.append(episode)

    if relation is not None:
        _require_text(relation, "relation")
        sql += " AND relation = ?"
        params.append(relation)

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(sql, params).fetchone()

    return int(row[0] or 0)


# ---------- 事件 ----------


def insert_memory_event(memory_id, event_type, detail_json, created_at):
    _require_int(memory_id, "memory_id", minimum=1)
    _require_text(event_type, "event_type")
    _require_text(detail_json, "detail_json")
    _require_text(created_at, "created_at")

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_events (memory_id, event_type, detail_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (memory_id, event_type, detail_json, created_at),
        )
        return cursor.lastrowid


def delete_all_memories():
    """清空记忆三表（rebuild 用）；不动游标。"""

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("DELETE FROM memory_events")
        conn.execute("DELETE FROM memory_evidence")
        conn.execute("DELETE FROM learning_memories")


# ---------- pending 缓冲区 ----------


def _pending_row_to_dict(row):
    return {
        "id": row[0],
        "memory_id": row[1],
        "feedback_id": row[2],
        "plan_id": row[3],
        "episode": row[4],
        "created_at": row[5],
        "recorded_at": row[6],
    }


def insert_pending(memory_id, feedback_id, plan_id, episode, created_at, recorded_at):
    """写入待判定证据；同一 (memory_id, feedback_id) 只允许一行。"""

    _require_int(memory_id, "memory_id", minimum=1)
    _require_int(feedback_id, "feedback_id", minimum=1)
    _require_int(episode, "episode", minimum=1)
    _require_text(created_at, "created_at")
    _require_text(recorded_at, "recorded_at")

    if plan_id is not None:
        _require_int(plan_id, "plan_id", minimum=1)

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO memory_pending_evidence
            (memory_id, feedback_id, plan_id, episode, created_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, feedback_id, plan_id, episode, created_at, recorded_at),
        )

        inserted = cursor.rowcount == 1
        return {"inserted": inserted, "row_id": cursor.lastrowid if inserted else None}


def list_pending(memory_id=None):
    """按 feedback_id 升序返回缓冲区行（全局或单条记忆）。"""

    sql = (
        "SELECT id, memory_id, feedback_id, plan_id, episode, created_at, recorded_at "
        "FROM memory_pending_evidence"
    )
    params = []

    if memory_id is not None:
        _require_int(memory_id, "memory_id", minimum=1)
        sql += " WHERE memory_id = ?"
        params.append(memory_id)

    sql += " ORDER BY feedback_id ASC"

    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [_pending_row_to_dict(row) for row in rows]


def delete_pending(pending_ids):
    """按缓冲区主键删除；返回实际删除行数。"""

    if not pending_ids:
        return 0

    ids = [_require_int(value, "pending_id", minimum=1) for value in pending_ids]
    placeholders = ", ".join("?" for _ in ids)

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            f"DELETE FROM memory_pending_evidence WHERE id IN ({placeholders})", ids
        )
        return cursor.rowcount


def delete_expired_pending(cutoff_iso):
    """删除 created_at 早于 cutoff 的缓冲区行（窗口过期）；返回删除行数。"""

    _require_text(cutoff_iso, "cutoff_iso")

    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM memory_pending_evidence WHERE created_at < ?", (cutoff_iso,)
        )
        return cursor.rowcount


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


def get_plan_by_id(plan_id):
    """按主键读取计划（不限状态）；非法 ID 或不存在返回 None。

    反馈提交时需要验证 plan_id 真实存在，这里只做 IO，不做业务判断。
    注意：严格只接受正整数，bool 不算（Python 里 True == 1）。
    """

    if isinstance(plan_id, bool) or not isinstance(plan_id, int) or plan_id < 1:
        return None

    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, plan_date, task, estimated_minutes, difficulty, question_count,
                   reason, completion_criteria, subtasks_json, status, created_at
            FROM plans
            WHERE id = ?
            LIMIT 1
            """,
            (plan_id,),
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
