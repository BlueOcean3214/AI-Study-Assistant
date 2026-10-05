"""GET /plan 与 POST /plan 接口测试：直接运行 python test_plan_api.py。

不需要 httpx：本文件自己启动一个 uvicorn 进程（不带 --reload），再用 requests 访问真实 HTTP。
隔离：子进程通过环境变量 FEEDBACK_DB_PATH 使用测试专用 SQLite，绝不碰真实 feedback.db。
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests

import database


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_plan_api.db"
SERVER_LOG = PROJECT_DIR / "_test_plan_api_server.log"

KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def make_plan(**overrides):
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


def pick_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_ready(base_url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{base_url}/", timeout=2).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            time.sleep(0.4)
    return False


def fixture_row_count():
    connection = sqlite3.connect(FIXTURE_DB)
    count = connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    connection.close()
    return count


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
KNOWLEDGE_SNAPSHOT = sorted(
    (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
    for path in KNOWLEDGE_DIR.rglob("*.txt")
)
REAL_CACHE_EXISTED = REAL_CACHE.exists()

if FIXTURE_DB.exists():
    FIXTURE_DB.unlink()
if SERVER_LOG.exists():
    SERVER_LOG.unlink()

port = pick_free_port()
base_url = f"http://127.0.0.1:{port}"

log_handle = SERVER_LOG.open("w", encoding="utf-8")
process = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
    cwd=str(PROJECT_DIR),
    stdout=log_handle,
    stderr=subprocess.STDOUT,
    env={**os.environ, "FEEDBACK_DB_PATH": str(FIXTURE_DB)},
)
print(f"已启动测试服务：{base_url}（数据库：{FIXTURE_DB.name}）")

try:
    if not wait_until_ready(base_url):
        print(SERVER_LOG.read_text(encoding="utf-8")[-2000:] if SERVER_LOG.exists() else "无日志")
        raise SystemExit("测试服务启动失败")

    check("夹具数据库已建立（不碰真实库）", FIXTURE_DB.exists())

    # ==========================
    # POST /plan
    # ==========================

    # 22. 合法请求成功
    response = requests.post(
        f"{base_url}/plan",
        json={"plan": make_plan(), "plan_date": "2026-10-20", "confirmed_by_user": True},
        timeout=30,
    )
    check("合法计划返回 200", response.status_code == 200, f"status={response.status_code}")
    payload = response.json()
    check("返回 saved=true", payload.get("saved") is True, f"payload={payload}")
    check("返回 plan_id", isinstance(payload.get("plan_id"), int) and payload["plan_id"] > 0)
    check("返回 plan_date", payload.get("plan_date") == "2026-10-20")
    check("返回计划内容", payload["plan"]["task"] == "定积分基础巩固")
    check("返回结构不含 SQLite 行字段", "subtasks_json" not in payload["plan"])
    check("数据库里恰好 1 行", fixture_row_count() == 1)

    # 23. 无确认失败
    for bad_confirmation in (False, None, "true", 1):
        response = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": "2026-10-21", "confirmed_by_user": bad_confirmation},
            timeout=30,
        )
        check(
            f"confirmed_by_user={bad_confirmation!r} 返回 400 confirmation_required",
            response.status_code == 400 and response.json().get("error_type") == "confirmation_required",
            f"status={response.status_code} body={response.text[:80]}",
        )
    check("未确认时没有新增行", fixture_row_count() == 1)

    # 24. 非法计划失败
    invalid_cases = [
        ("difficulty 非法", {"difficulty": "extreme"}),
        ("时间超限", {"estimated_minutes": 90}),
        ("subtasks 数量错误", {"subtasks": [{"title": "A", "minutes": 5, "question_count": 4, "description": "d"}]}),
        ("completion_criteria 不一致", {"completion_criteria": "完成9道题"}),
    ]
    for name, overrides in invalid_cases:
        response = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(**overrides), "plan_date": "2026-10-22", "confirmed_by_user": True},
            timeout=30,
        )
        check(
            f"{name} 返回 400 validation_error",
            response.status_code == 400 and response.json().get("error_type") == "validation_error",
            f"status={response.status_code} body={response.text[:80]}",
        )

    response = requests.post(
        f"{base_url}/plan",
        json={"plan": make_plan(extra="x"), "plan_date": "2026-10-22", "confirmed_by_user": True},
        timeout=30,
    )
    check("多余字段返回 validation_error", response.json().get("error_type") == "validation_error")

    response = requests.post(
        f"{base_url}/plan",
        json={"plan": "不是对象", "plan_date": "2026-10-22", "confirmed_by_user": True},
        timeout=30,
    )
    check("plan 非对象返回 validation_error", response.json().get("error_type") == "validation_error")

    # 日期非法
    for bad_date in ("2026/10/05", "abc", "2026-99-99", "2026-1-5", ""):
        response = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": bad_date, "confirmed_by_user": True},
            timeout=30,
        )
        check(
            f"非法日期 {bad_date!r} 返回 400 invalid_date",
            response.status_code == 400 and response.json().get("error_type") == "invalid_date",
            f"status={response.status_code} body={response.text[:80]}",
        )

    # 25. 重复计划失败
    response = requests.post(
        f"{base_url}/plan",
        json={"plan": make_plan(task="重复计划"), "plan_date": "2026-10-20", "confirmed_by_user": True},
        timeout=30,
    )
    check(
        "同一天重复计划返回 409 active_plan_exists",
        response.status_code == 409 and response.json().get("error_type") == "active_plan_exists",
        f"status={response.status_code} body={response.text[:80]}",
    )
    check("重复请求没有新增行", fixture_row_count() == 1)

    # ==========================
    # GET /plan
    # ==========================

    response = requests.get(f"{base_url}/plan", params={"date": "2026-10-20"}, timeout=10)
    check("GET 有计划返回 200", response.status_code == 200)
    body = response.json()
    check("GET exists=true", body.get("exists") is True)
    check("GET 返回完整 subtasks", body["plan"]["subtasks"] == make_plan()["subtasks"])
    check("GET 不返回 subtasks_json", "subtasks_json" not in body["plan"])

    response = requests.get(f"{base_url}/plan", params={"date": "2030-01-01"}, timeout=10)
    check("GET 无计划返回稳定空结构", response.status_code == 200 and response.json() == {"exists": False, "plan": None},
          f"body={response.text[:80]}")

    response = requests.get(f"{base_url}/plan", params={"date": "2026/10/20"}, timeout=10)
    check(
        "GET 非法日期返回 400 invalid_date",
        response.status_code == 400 and response.json().get("error_type") == "invalid_date",
        f"status={response.status_code} body={response.text[:80]}",
    )

    response = requests.get(f"{base_url}/plan", timeout=10)
    check("GET 不带日期返回 200（默认今天）", response.status_code == 200 and "exists" in response.json(),
          f"body={response.text[:80]}")

    # ==========================
    # 26. DB error 稳定处理
    # ==========================
    connection = sqlite3.connect(FIXTURE_DB)
    connection.execute("DROP TABLE plans")
    connection.commit()
    connection.close()

    response = requests.post(
        f"{base_url}/plan",
        json={"plan": make_plan(), "plan_date": "2026-10-23", "confirmed_by_user": True},
        timeout=30,
    )
    check(
        "表缺失时返回 500 database_error",
        response.status_code == 500 and response.json().get("error_type") == "database_error",
        f"status={response.status_code} body={response.text[:80]}",
    )

    response = requests.get(f"{base_url}/plan", params={"date": "2026-10-20"}, timeout=10)
    check(
        "读表失败同样返回 database_error",
        response.status_code == 500 and response.json().get("error_type") == "database_error",
        f"status={response.status_code} body={response.text[:80]}",
    )

    # 恢复表结构，供后续检查使用
    original_path = database.DATABASE_PATH
    database.DATABASE_PATH = FIXTURE_DB
    try:
        database.init_db()
    finally:
        database.DATABASE_PATH = original_path
    check("测试库表结构可恢复", fixture_row_count() == 0)

    # ==========================
    # 既有 API 未受影响（廉价检查；/ask /next-plan /analyze 由各自测试套件覆盖）
    # ==========================
    check("GET / 行为未变", requests.get(f"{base_url}/", timeout=10).json() == {"message": "AI Study Assistant"})

    feedback_response = requests.get(f"{base_url}/feedback", timeout=10)
    check(
        "GET /feedback 结构未变",
        feedback_response.status_code == 200 and "feedback" in feedback_response.json(),
    )

    openapi = requests.get(f"{base_url}/openapi.json", timeout=10).json()
    check("OpenAPI 暴露 GET/POST /plan", "get" in openapi["paths"]["/plan"] and "post" in openapi["paths"]["/plan"])
    for existing in ("/", "/ask", "/upload", "/feedback", "/analyze", "/next-plan"):
        check(f"端点 {existing} 仍注册", existing in openapi["paths"])
finally:
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        print("测试服务已停止，退出码:", process.returncode)

    log_handle.close()

    for _ in range(20):
        if not SERVER_LOG.exists():
            break
        try:
            SERVER_LOG.unlink()
        except PermissionError:
            time.sleep(0.3)

    if FIXTURE_DB.exists():
        try:
            FIXTURE_DB.unlink()
        except PermissionError:
            time.sleep(0.3)

check("夹具数据库已清理", not FIXTURE_DB.exists())
check("服务日志已清理", not SERVER_LOG.exists())
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
