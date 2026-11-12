"""Task 0 契约测试：/feedback 的 status / reason 服务端枚举。

必须走真实 HTTP（不是直接调函数），因为合约是在 Pydantic 请求体校验里生效的：
- 取值不在枚举内 -> HTTP 422（FastAPI 默认 detail 结构，没有 error_type）
- 取值合法但缺 reason（partial / not_completed）-> HTTP 400 reason_required
- completed 允许不带 reason
"""

import atexit
import gc
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

os.environ.setdefault(
    "FEEDBACK_DB_PATH",
    str(PROJECT_DIR / "_test_feedback_enum_unit.db"),
)
# 确定性测试：不配置模型后端（不依赖本地 Ollama，也不调用云端 API 产生费用）

import requests  # noqa: E402

HTTP_DB = PROJECT_DIR / "_test_feedback_enum_http.db"
SERVER_LOG = PROJECT_DIR / "_test_feedback_enum_server.log"
REAL_DB = PROJECT_DIR / "feedback.db"

_real_db_bytes_before = REAL_DB.read_bytes() if REAL_DB.exists() else None

_server = {"process": None, "base_url": None, "log": None}


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


def _pick_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_ready(base_url, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _server["process"] and _server["process"].poll() is not None:
            return False
        try:
            if requests.get(f"{base_url}/", timeout=2).status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - 服务还没起来
            time.sleep(0.4)
    return False


def ensure_server():
    if _server["base_url"]:
        return _server["base_url"]

    remove_file(HTTP_DB)

    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ, FEEDBACK_DB_PATH=str(HTTP_DB))
    log_handle = open(SERVER_LOG, "w", encoding="utf-8")

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_DIR),
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    _server.update(process=process, base_url=base_url, log=log_handle)

    if not _wait_until_ready(base_url):
        raise AssertionError(f"FAILED: 测试服务器未能启动，日志见 {SERVER_LOG.name}")

    return base_url


def shutdown_server():
    process = _server.get("process")
    log_handle = _server.get("log")

    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    if log_handle is not None and not log_handle.closed:
        log_handle.close()

    _server.update(process=None, base_url=None, log=None)
    remove_file(SERVER_LOG)


atexit.register(shutdown_server)


def feedback_count():
    ensure_server()  # 确保夹具库已被服务端建好（含 migrations）
    with sqlite3.connect(HTTP_DB) as conn:
        return conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]


def base_payload(**overrides):
    payload = {
        "task": "条件概率练习",
        "estimated_minutes": 30,
        "actual_minutes": 20,
        "status": "partial",
        "reason": "任务难度太高",
    }
    payload.update(overrides)
    return payload


def post(payload):
    base_url = ensure_server()
    return requests.post(f"{base_url}/feedback", json=payload, timeout=10)


def assert_rejected_422(method, label):
    before = feedback_count()
    response = method()

    assert response.status_code == 422, f"{label} 应 422，实际 {response.status_code}"
    body = response.json()
    assert "detail" in body, f"{label} 422 应带 detail，实际 {body}"
    assert "error_type" not in body, f"{label} 422 不应伪造 error_type，实际 {body}"
    assert feedback_count() == before, f"{label} 被拒绝时不得写库"

    print(f"PASS: {label} -> 422（detail 存在、无 error_type、未写库）")


def test_invalid_status_is_rejected():
    assert_rejected_422(lambda: post(base_payload(status="done")), "V1 非法 status done")
    assert_rejected_422(
        lambda: post(base_payload(status="COMPLETED", reason=None)), "V2 大写 status COMPLETED"
    )
    assert_rejected_422(
        lambda: post(base_payload(status="")), "V2 空 status"
    )


def test_invalid_reason_is_rejected():
    assert_rejected_422(
        lambda: post(base_payload(reason="时间不够")), "V3 非枚举 reason 时间不够"
    )
    assert_rejected_422(
        lambda: post(base_payload(reason="任务太难")), "V3 非枚举 reason 任务太难"
    )


def test_completed_with_invalid_reason_is_rejected():
    """status=completed 可以不带 reason，但带了一个非枚举值仍应被 422 拒绝。

    completed 只是放宽「必须给 reason」，不放宽 reason 本身的取值域；否则
    「完成任务但标记原因为任务太难」这种自相矛盾的记录会写进库。
    """

    assert_rejected_422(
        lambda: post(base_payload(status="completed", reason="时间不够")),
        "V9 completed + 非枚举 reason 时间不够",
    )
    assert_rejected_422(
        lambda: post(base_payload(status="completed", reason="任务太难")),
        "V9 completed + 非枚举 reason 任务太难",
    )


def test_reason_required_for_partial():
    before = feedback_count()
    response = post(base_payload(status="partial", reason=None))

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "reason_required"
    assert feedback_count() == before, "缺 reason 被拒绝时不得写库"

    print("PASS: V4 partial 缺 reason -> 400 reason_required，且未写库")


def test_reason_required_for_not_completed():
    before = feedback_count()
    response = post(base_payload(status="not_completed", reason=None))

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "reason_required"
    assert feedback_count() == before

    print("PASS: V5 not_completed 缺 reason -> 400 reason_required，且未写库")


def test_completed_without_reason_is_accepted():
    before = feedback_count()
    response = post(base_payload(status="completed", reason=None))

    assert response.status_code == 200, response.text
    assert response.json()["feedback"]["reason"] is None
    assert feedback_count() == before + 1

    print("PASS: V6 completed 不带 reason 仍 200")


def test_valid_payload_is_accepted():
    before = feedback_count()
    response = post(base_payload())

    assert response.status_code == 200, response.text
    body = response.json()["feedback"]
    assert body["status"] == "partial" and body["reason"] == "任务难度太高"
    assert feedback_count() == before + 1

    print("PASS: V7 合法 partial + 合法 reason -> 200")


def test_plan_id_check_runs_before_reason_check():
    """依赖顺序：plan_id 非法时先报 invalid_plan_id（不是 reason_required）。"""

    response = post(base_payload(status="partial", reason=None, plan_id=999999))

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "invalid_plan_id"

    print("PASS: V8 plan_id 校验先于 reason_required")


def _run_all():
    tests = [
        test_invalid_status_is_rejected,
        test_invalid_reason_is_rejected,
        test_completed_with_invalid_reason_is_rejected,
        test_reason_required_for_partial,
        test_reason_required_for_not_completed,
        test_completed_without_reason_is_accepted,
        test_valid_payload_is_accepted,
        test_plan_id_check_runs_before_reason_check,
    ]

    failures = 0

    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f"FAILED: {test.__name__}: {error}")
        except Exception as error:  # noqa: BLE001
            failures += 1
            print(f"ERROR: {test.__name__}: {type(error).__name__}: {error}")

    shutdown_server()
    remove_file(HTTP_DB)
    remove_file(PROJECT_DIR / "_test_feedback_enum_unit.db")

    after = REAL_DB.read_bytes() if REAL_DB.exists() else None
    if after != _real_db_bytes_before:
        failures += 1
        print("FAILED: 真实 feedback.db 被改动")

    print(f"\n结果：{len(tests) - failures}/{len(tests)} 个测试函数通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
