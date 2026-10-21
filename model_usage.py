"""模型用量记录（model_usage 表）。

设计约束：
- 只记录 Provider / 模型 / token 用量 / 耗时 / 成败 / 稳定错误类型 / 时间
- 不记录完整 Prompt、回答内容、API Key 或任何隐私数据
- 记录失败绝不影响模型请求（全部异常吞掉，返回 False）
- Provider 未返回 usage 时写 NULL（usage_available=0），不做任何估算
- 启用策略：环境变量 MODEL_USAGE_ENABLED（默认 1 启用；0 关闭）
"""

import os
import sqlite3
from datetime import datetime

import database
import llm_client


def is_enabled():
    """用量记录开关（MODEL_USAGE_ENABLED，默认启用）。"""

    value = (os.getenv("MODEL_USAGE_ENABLED") or "1").strip().lower()

    return value not in {"0", "false", "off", "no"}


def record_usage(record):
    """写一条用量记录；任何失败都返回 False，绝不抛异常。"""

    if not is_enabled():
        return False

    conn = sqlite3.connect(database.DATABASE_PATH)

    try:
        conn.execute(
            """
            INSERT INTO model_usage
            (provider, model, prompt_tokens, completion_tokens, total_tokens,
             usage_available, elapsed_seconds, ok, error_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("provider"),
                record.get("model"),
                record.get("prompt_tokens"),
                record.get("completion_tokens"),
                record.get("total_tokens"),
                1 if record.get("usage_available") else 0,
                record.get("elapsed_seconds"),
                1 if record.get("ok") else 0,
                record.get("error_type"),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 - 用量记录绝不能成为模型请求的强依赖
        return False
    finally:
        conn.close()


def install():
    """把用量记录注册为 llm_client 的用量钩子（调用一次即可）。"""

    llm_client.set_usage_recorder(record_usage)


def recent_usage(limit=20):
    """最近 limit 条用量记录（诊断用；不包含 Prompt / 回答）。"""

    limit = max(1, min(int(limit), 100))

    conn = sqlite3.connect(database.DATABASE_PATH)

    try:
        rows = conn.execute(
            """
            SELECT provider, model, prompt_tokens, completion_tokens, total_tokens,
                   usage_available, elapsed_seconds, ok, error_type, created_at
            FROM model_usage
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "provider": row[0], "model": row[1],
            "prompt_tokens": row[2], "completion_tokens": row[3], "total_tokens": row[4],
            "usage_available": bool(row[5]), "elapsed_seconds": row[6],
            "ok": bool(row[7]), "error_type": row[8], "created_at": row[9],
        }
        for row in rows
    ]
