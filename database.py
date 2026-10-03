import json
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
