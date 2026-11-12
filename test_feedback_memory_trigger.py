"""Task 7：/feedback 触发记忆更新（独立事务）与 GET /memory 契约测试。

覆盖 v16 §6.4 与 §8.2 的 G 组：
- 反馈保存成功后确实触发记忆更新，响应里带 memory 字段
- 记忆更新失败不影响反馈保存成功（HTTP 仍 200，反馈已落库）
- 反馈事务与记忆事务互相独立（记忆失败不产生第二条反馈、游标不被回滚）
- 无重复处理、无重复记忆（同一条反馈只计一次）
- GET /memory：过滤、空结果、参数非法、公开字段边界
- /feedback 100 次本地提交的延迟增量（真实测量，不写死结论）

运行：python test_feedback_memory_trigger.py
"""

import gc
import os
import sqlite3
import statistics
import time
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parent

os.environ.setdefault(
    "FEEDBACK_DB_PATH",
    str(PROJECT_DIR / "_test_feedback_memory_unit.db"),
)
os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")
os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("OLLAMA_MODEL", "qwen3.5:4b")

from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
import memory_service  # noqa: E402

REAL_DB = PROJECT_DIR / "feedback.db"
_REAL_DB_BYTES_BEFORE = REAL_DB.read_bytes() if REAL_DB.exists() else None

_CREATED_DBS = []
_DB_SEQ = [0]


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def remove_file(path):
    for _ in range(20):
        if not path.exists():
            return
        gc.collect()
        try:
            path.unlink()
        except PermissionError:
            time.sleep(0.1)
    raise AssertionError(f"夹具文件无法删除（仍被占用）：{path}")


def fresh():
    _DB_SEQ[0] += 1
    path = PROJECT_DIR / f"_test_feedback_memory_{_DB_SEQ[0]}.db"

    if path.exists():
        remove_file(path)

    database.DATABASE_PATH = path
    database.init_db()
    _CREATED_DBS.append(path)

    return path


def cleanup_databases():
    for path in list(_CREATED_DBS):
        gc.collect()
        if not path.exists():
            continue
        try:
            remove_file(path)
        except AssertionError:
            continue


def client():
    return TestClient(main.app)


def feedback_payload(task="极限练习", status="partial", reason="任务难度太高", minutes=30, actual=20, created_note=None):
    payload = {
        "task": task,
        "estimated_minutes": minutes,
        "actual_minutes": actual,
        "status": status,
        "reason": reason,
        "completed_subtasks": 1,
        "total_subtasks": 2,
        "completed_questions": 1,
        "total_questions": 4,
    }
    if created_note:
        payload["note"] = created_note
    return payload


def feedback_rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT id, task, status FROM feedbacks ORDER BY id").fetchall()


def cursor_row(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT last_applied_feedback_id FROM memory_apply_cursor WHERE id = 1"
        ).fetchone()


def memory_rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT id, memory_type, subject_key, evidence_count FROM learning_memories ORDER BY id"
        ).fetchall()


def test_feedback_success_reports_memory_field():
    path = fresh()
    http = client()

    first = http.post("/feedback", json=feedback_payload())
    check("首次反馈返回 200", first.status_code == 200, first.text)
    first_body = first.json()
    check("响应里有 memory 字段", "memory" in first_body, first_body.keys())
    check("首次单条未达门槛：ok 且没有创建记忆", first_body["memory"]["ok"] is True, first_body["memory"])
    check(
        "未达门槛时 created/updated 为空列表",
        first_body["memory"]["created"] == [] and first_body["memory"]["updated"] == [],
        first_body["memory"],
    )

    second = http.post("/feedback", json=feedback_payload(actual=25))
    check("第二条反馈返回 200", second.status_code == 200, second.text)
    memory_field = second.json()["memory"]

    check("第二条触发记忆更新成功", memory_field.get("ok") is True, memory_field)
    check(
        "difficulty 记忆被更新",
        any(item.get("type") == "difficulty" for item in memory_field.get("updated", [])),
        memory_field.get("updated"),
    )
    check(
        "created/updated 只含 type+subject（无内部 id）",
        all(set(item) == {"type", "subject"} for item in memory_field["updated"]),
        memory_field["updated"],
    )

    check("两条反馈都落库", len(feedback_rows(path)) == 2, feedback_rows(path))
    check("游标推进到 2", cursor_row(path) == (2,), cursor_row(path))
    check("只产生一条 difficulty 记忆", len(memory_rows(path)) == 1, memory_rows(path))
    check("证据计 2 条", memory_rows(path)[0][3] == 2, memory_rows(path))


def test_memory_failure_does_not_hurt_feedback():
    path = fresh()
    http = client()

    with mock.patch.object(
        memory_service, "update_from_feedback", side_effect=RuntimeError("boom")
    ):
        response = http.post("/feedback", json=feedback_payload())

    check("记忆更新异常时反馈仍返回 200", response.status_code == 200, response.text)
    body = response.json()
    check("memory.ok 为 False", body["memory"]["ok"] is False, body["memory"])
    check(
        "memory 带稳定 error_type",
        body["memory"].get("error_type") == "memory_update_failed",
        body["memory"],
    )
    check("反馈没有被回滚", len(feedback_rows(path)) == 1, feedback_rows(path))
    check("记忆事务失败后没有残留记忆", memory_rows(path) == [], memory_rows(path))


def test_memory_failure_reported_error_type_is_passthrough():
    path = fresh()
    http = client()

    with mock.patch.object(
        memory_service,
        "update_from_feedback",
        return_value={"ok": False, "error_type": "db_error"},
    ):
        response = http.post("/feedback", json=feedback_payload())

    check("记忆返回失败时反馈仍 200", response.status_code == 200, response.text)
    check(
        "error_type 原样透传",
        response.json()["memory"]["error_type"] == "db_error",
        response.json()["memory"],
    )
    check("反馈已落库", len(feedback_rows(path)) == 1, feedback_rows(path))


def test_repeated_feedback_is_counted_once():
    path = fresh()
    http = client()

    http.post("/feedback", json=feedback_payload(actual=20))
    second = http.post("/feedback", json=feedback_payload(actual=25))

    check("第二条返回 200", second.status_code == 200, second.text)
    check("证据只有 2 条", memory_rows(path)[0][3] == 2, memory_rows(path))

    # 同一条反馈重复触发记忆更新（模拟重试）不应重复计证据
    with sqlite3.connect(path) as conn:
        last_id = conn.execute("SELECT MAX(id) FROM feedbacks").fetchone()[0]

    memory_service.update_from_feedback(last_id)
    check("重复触发不增加证据", memory_rows(path)[0][3] == 2, memory_rows(path))
    check("游标不回退", cursor_row(path) == (2,), cursor_row(path))


def test_validation_failure_writes_nothing():
    path = fresh()
    http = client()

    # reason 缺省（partial 必须有 reason）：服务端 reason_required 400，
    # 注意空字符串属于枚举非法（422），不用于此用例
    payload = feedback_payload(status="partial")
    payload.pop("reason")

    response = http.post("/feedback", json=payload)

    check("缺 reason 返回 400", response.status_code == 400, response.text)
    check("错误类型稳定", response.json()["error_type"] == "reason_required", response.text)
    check("没有写反馈", feedback_rows(path) == [], feedback_rows(path))
    check("没有写记忆", memory_rows(path) == [], memory_rows(path))


def test_get_memory_returns_active_memory():
    path = fresh()
    http = client()

    http.post("/feedback", json=feedback_payload(actual=20))
    http.post("/feedback", json=feedback_payload(actual=25))

    response = http.get("/memory")
    check("GET /memory 返回 200", response.status_code == 200, response.text)
    body = response.json()

    check("ok 为 True", body["ok"] is True, body)
    check("count 与列表一致", body["count"] == len(body["memories"]), body)
    check("至少返回一条记忆", body["count"] >= 1, body)
    check("error_type 为 None", body["error_type"] is None, body)

    memory = body["memories"][0]
    check(
        "公开字段齐备",
        set(memory) == {
            "type",
            "subject",
            "content",
            "strength",
            "evidence_count",
            "episode",
            "evidence_feedback_ids",
            "updated_at",
        },
        sorted(memory),
    )
    check("不返回 memory_id", "memory_id" not in memory, memory)
    check("不返回 value_json", "value_json" not in memory, memory)
    check("证据 id 不超过 5 个", len(memory["evidence_feedback_ids"]) <= 5, memory)
    check("content 非空", bool(memory["content"]), memory)


def test_get_memory_filters_and_empty_results():
    path = fresh()
    http = client()

    http.post("/feedback", json=feedback_payload(actual=20))
    http.post("/feedback", json=feedback_payload(actual=25))

    only_type = http.get("/memory", params={"memory_type": "difficulty"})
    check("按类型过滤成功", only_type.status_code == 200, only_type.text)
    check(
        "过滤后只剩 difficulty",
        all(item["type"] == "difficulty" for item in only_type.json()["memories"]),
        only_type.json(),
    )

    empty_type = http.get("/memory", params={"memory_type": "task_topic"})
    check("空结果是正常响应", empty_type.status_code == 200, empty_type.text)
    check("空结果 count 为 0", empty_type.json()["count"] == 0, empty_type.json())

    by_subject = http.get("/memory", params={"subject": "极限练习"})
    check("按主题过滤成功", by_subject.json()["count"] >= 1, by_subject.json())

    unknown_subject = http.get("/memory", params={"subject": "不存在的主题"})
    check("未知主题返回空列表", unknown_subject.json()["count"] == 0, unknown_subject.json())

    resolved = http.get("/memory", params={"status": "resolved"})
    check("按状态过滤成功（可能为空）", resolved.status_code == 200, resolved.text)


def test_get_memory_rejects_invalid_arguments():
    path = fresh()
    http = client()

    bad_type = http.get("/memory", params={"memory_type": "preference"})
    check("非法类型返回 400", bad_type.status_code == 400, bad_type.text)
    check(
        "错误类型为 invalid_argument",
        bad_type.json()["error_type"] == "invalid_argument",
        bad_type.json(),
    )

    bad_status = http.get("/memory", params={"status": "unknown"})
    check("非法状态返回 400", bad_status.status_code == 400, bad_status.text)

    zero_limit = http.get("/memory", params={"limit": 0})
    check("limit=0 返回 400", zero_limit.status_code == 400, zero_limit.text)

    huge_limit = http.get("/memory", params={"limit": 99})
    check("limit=99 被夹到上限而不是报错", huge_limit.status_code == 200, huge_limit.text)

    bad_limit_type = http.get("/memory", params={"limit": "abc"})
    check("limit 非整数返回 422", bad_limit_type.status_code == 422, bad_limit_type.text)


def test_get_memory_is_read_only():
    path = fresh()
    http = client()

    http.post("/feedback", json=feedback_payload(actual=20))
    http.post("/feedback", json=feedback_payload(actual=25))

    before = path.read_bytes()
    http.get("/memory")
    http.get("/memory", params={"memory_type": "difficulty"})
    after = path.read_bytes()

    check("GET /memory 不改数据库", before == after, "bytes changed")
    check("没有写接口暴露写操作", not any(
        route.path == "/memory" and "POST" in getattr(route, "methods", set())
        for route in main.app.routes
    ), "POST /memory 不应存在")


def test_feedback_latency_p95():
    path = fresh()
    http = client()

    # 预热：首次请求包含连接与模块级缓存初始化
    http.post("/feedback", json=feedback_payload(actual=20))

    samples = []
    for index in range(100):
        started = time.perf_counter()
        response = http.post("/feedback", json=feedback_payload(actual=20 + (index % 5)))
        samples.append((time.perf_counter() - started) * 1000)
        check(f"第 {index + 1} 次提交返回 200", response.status_code == 200, response.text)

    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    p99 = ordered[-1]

    print(
        f"INFO: /feedback 本地 100 次延迟 p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms"
    )

    check("100 次反馈全部落库", len(feedback_rows(path)) == 101, len(feedback_rows(path)))
    check("延迟测量有有效样本", len(samples) == 100 and p95 > 0, f"p95={p95:.2f}ms")
    # v16 §8.2 的性能目标：本地 /feedback 的 P95 增量 ≤ 50ms
    check("P95 满足 v16 目标（≤50ms）", p95 <= 50.0, f"p95={p95:.2f}ms")


def test_real_database_untouched():
    if _REAL_DB_BYTES_BEFORE is None:
        check("真实 feedback.db 不存在（跳过字节比对）", True)
        return

    after = REAL_DB.read_bytes()
    check("真实 feedback.db 字节未变", after == _REAL_DB_BYTES_BEFORE, "bytes changed")


def _run_all():
    tests = [
        test_feedback_success_reports_memory_field,
        test_memory_failure_does_not_hurt_feedback,
        test_memory_failure_reported_error_type_is_passthrough,
        test_repeated_feedback_is_counted_once,
        test_validation_failure_writes_nothing,
        test_get_memory_returns_active_memory,
        test_get_memory_filters_and_empty_results,
        test_get_memory_rejects_invalid_arguments,
        test_get_memory_is_read_only,
        test_feedback_latency_p95,
        test_real_database_untouched,
    ]

    for test in tests:
        print(f"\n=== {test.__name__} ===")
        test()

    print("\nALL TESTS COMPLETED")


if __name__ == "__main__":
    try:
        _run_all()
    finally:
        cleanup_databases()
