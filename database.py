import json
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3


DATABASE_PATH = Path(__file__).resolve().parent / "feedback.db"
BACKUP_PATH = Path(__file__).resolve().parent / "feedback_backup.json"


def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
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

    # Add fields to an older table without removing or changing existing data.
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
    conn.commit()
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
