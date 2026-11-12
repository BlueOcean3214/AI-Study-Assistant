"""第一阶段验收测试：学习反馈与学习计划的关联（feedbacks.plan_id）。

直接运行：python test_feedback_plan_link.py

覆盖：
- V5 库升级到 V6：旧反馈不丢、plan_id 为 NULL、索引存在、重复初始化幂等
- 全新库初始化、高版本库拒绝启动（降级保护仍有效）
- 反馈读写：带/不带 plan_id、get_feedback_since 仍返回 plan_id、旧调用方兼容
- get_plan_by_id 边界（不存在 / 0 / -1 / 字符串 / 布尔）
- POST /feedback：有效关联、无效 ID、任务不匹配、缺省兼容、严格类型（"12" / true / 0）
- 同一计划多条反馈、旧数据仍可读
- 前端静态检查：feedback.html 读取 plan_id，且未破坏计时/子任务逻辑
- 真实 feedback.db 不被改动

测试风格：def test_xxx() + assert，文件底部有 __main__ 运行器；
既能 `python test_feedback_plan_link.py` 直接跑，也能被将来的 pytest 收集。
本阶段不引入 pytest 依赖。
"""

import atexit
import gc
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# 兜底：任何情况下都不让被测模块默认指向真实库
os.environ.setdefault(
    "FEEDBACK_DB_PATH",
    str(PROJECT_DIR / "_test_feedback_link_unit.db"),
)
# 与既有测试保持一致：确定性用例不配置模型后端（不依赖本机 .env 与服务商）

import database  # noqa: E402  （必须在环境变量之后导入）
import requests  # noqa: E402

MIGRATE_DB = PROJECT_DIR / "_test_feedback_link_migrate.db"
UNIT_DB = PROJECT_DIR / "_test_feedback_link_unit.db"
HTTP_DB = PROJECT_DIR / "_test_feedback_link_http.db"
SERVER_LOG = PROJECT_DIR / "_test_feedback_link_server.log"
STATIC_DIR = PROJECT_DIR / "static"
REAL_DB = PROJECT_DIR / "feedback.db"

TODAY = "2026-11-10"

_real_db_bytes_before = REAL_DB.read_bytes() if REAL_DB.exists() else None


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def remove_file(path):
    """删夹具（Windows 上 sqlite 句柄释放有延迟，先 gc 再重试）。"""

    for _ in range(20):
        if not path.exists():
            return
        gc.collect()
        try:
            path.unlink()
        except PermissionError:
            time.sleep(0.1)

    raise AssertionError(f"夹具文件无法删除（仍被占用）：{path}")


@contextmanager
def fixture_database(path):
    """把 database 模块指向夹具库，结束后恢复原路径。"""

    original = database.DATABASE_PATH
    database.DATABASE_PATH = path
    try:
        yield
    finally:
        database.DATABASE_PATH = original


def connect(path):
    return sqlite3.connect(path)


def column_names(path, table):
    conn = connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def index_names(path, table):
    conn = connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA index_list({table})")]
    finally:
        conn.close()


def feedback_rows(path):
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT id, task, status, plan_id FROM feedbacks ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def build_v5_database(path):
    """按 v1..v5 造一个 user_version=5 的旧库，含 2 条旧反馈 + 1 条计划。"""

    remove_file(path)

    conn = connect(path)
    try:
        for _version, migration in database.MIGRATIONS[:5]:
            migration(conn)

        conn.execute("PRAGMA user_version = 5")
        conn.executemany(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions,
             total_questions, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("旧反馈A", 30, 30, "completed", "无", 1, 1, 2, 2, "2026-11-01T10:00:00"),
                ("旧反馈B", 45, 20, "partial", "时间不够", 0, 2, 1, 4, "2026-11-02T10:00:00"),
            ],
        )
        conn.execute(
            """
            INSERT INTO plans
            (plan_date, task, estimated_minutes, difficulty, question_count,
             reason, completion_criteria, subtasks_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-11-01", "旧计划", 40, "easy", 4, "旧理由", "完成", "[]", "active",
             "2026-11-01T09:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


# ==========================
# 一、数据库迁移
# ==========================


def test_v5_to_v6_migration_keeps_data():
    build_v5_database(MIGRATE_DB)

    assert column_names(MIGRATE_DB, "feedbacks").count("plan_id") == 0
    assert "idx_feedbacks_plan_id" not in index_names(MIGRATE_DB, "feedbacks")

    with fixture_database(MIGRATE_DB):
        database.init_db()

    conn = connect(MIGRATE_DB)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    # 第二阶段起 schema 已到 V7：这里只断言「升级到程序当前支持的版本」
    assert version == database.SCHEMA_VERSION, f"user_version 应为 {database.SCHEMA_VERSION}，实际 {version}"
    assert column_names(MIGRATE_DB, "feedbacks").count("plan_id") == 1
    assert "idx_feedbacks_plan_id" in index_names(MIGRATE_DB, "feedbacks")

    rows = feedback_rows(MIGRATE_DB)
    assert [row[1] for row in rows] == ["旧反馈A", "旧反馈B"], "旧反馈不应丢失"
    assert all(row[3] is None for row in rows), "历史反馈 plan_id 必须为 NULL"

    conn = connect(MIGRATE_DB)
    try:
        plan_count = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    finally:
        conn.close()
    assert plan_count == 1, "旧计划不应丢失"

    print("PASS: V5 老库升级到当前版本，旧数据保留，plan_id 为 NULL，索引已建立")


def test_v6_migration_is_idempotent():
    # 自建 V5 夹具：不依赖其他测试留下的库，单独跑也必须成立
    build_v5_database(MIGRATE_DB)

    with fixture_database(MIGRATE_DB):
        for _ in range(3):
            database.init_db()

    assert column_names(MIGRATE_DB, "feedbacks").count("plan_id") == 1, "重复初始化不应重复加列"
    assert index_names(MIGRATE_DB, "feedbacks").count("idx_feedbacks_plan_id") == 1
    rows = feedback_rows(MIGRATE_DB)
    assert [row[1] for row in rows] == ["旧反馈A", "旧反馈B"], f"重复初始化不应改动数据：{rows}"

    print("PASS: 重复 init_db 3 次，列/索引/数据均不变")


def test_fresh_database_is_current_version():
    remove_file(UNIT_DB)

    with fixture_database(UNIT_DB):
        database.init_db()

    conn = connect(UNIT_DB)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert version == database.SCHEMA_VERSION
    assert "plan_id" in column_names(UNIT_DB, "feedbacks")
    assert "idx_feedbacks_plan_id" in index_names(UNIT_DB, "feedbacks")

    print(f"PASS: 全新库初始化为 v{database.SCHEMA_VERSION}，含 plan_id 与索引")


def test_newer_database_is_rejected():
    build_v5_database(MIGRATE_DB)

    conn = connect(MIGRATE_DB)
    try:
        conn.execute(f"PRAGMA user_version = {database.SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with fixture_database(MIGRATE_DB):
        try:
            database.init_db()
        except RuntimeError as error:
            assert "高于程序支持" in str(error)
        else:
            raise AssertionError("FAILED: 高版本库应拒绝启动")

    print("PASS: 高于 SCHEMA_VERSION 的库仍被拒绝（降级保护有效）")


# ==========================
# 二、数据库读写
# ==========================


def _feedback(task="关联测试任务", plan_id=None):
    data = {
        "task": task,
        "estimated_minutes": 40,
        "actual_minutes": 30,
        "status": "completed",
        "reason": "其他",
        "completed_subtasks": 1,
        "total_subtasks": 2,
        "completed_questions": 2,
        "total_questions": 4,
    }
    if plan_id is not None:
        data["plan_id"] = plan_id
    return data


def test_insert_and_read_feedback_with_plan_id():
    remove_file(UNIT_DB)

    with fixture_database(UNIT_DB):
        database.init_db()
        database.insert_feedback(_feedback(plan_id=7), "2026-11-10T12:00:00")
        rows = database.get_feedback_list()

    assert len(rows) == 1, f"应只有 1 条反馈，实际 {rows}"
    assert rows[0]["plan_id"] == 7, f"plan_id 应为 7，实际 {rows[0]}"
    assert rows[0]["task"] == "关联测试任务"
    assert "created_at" in rows[0], "原有字段必须保留"

    print("PASS: 带 plan_id 写入后可读出，plan_id 正确")


def test_insert_feedback_without_plan_id_still_works():
    remove_file(UNIT_DB)

    with fixture_database(UNIT_DB):
        database.init_db()
        # 模拟既有调用方（测试 / agent_eval）：dict 里根本没有 plan_id 键
        database.insert_feedback(_feedback(), "2026-11-10T12:05:00")
        rows = database.get_feedback_list()

    assert len(rows) == 1, f"应只有 1 条反馈，实际 {rows}"
    assert rows[0]["plan_id"] is None, f"plan_id 应为 None，实际 {rows[0]}"

    print("PASS: 不带 plan_id 的旧调用方式仍可写入，值为 NULL")


def test_get_feedback_since_returns_plan_id_and_keeps_clamping():
    remove_file(UNIT_DB)

    with fixture_database(UNIT_DB):
        database.init_db()
        database.insert_feedback(_feedback(plan_id=3), "2026-11-10T12:10:00")

        rows = database.get_feedback_since(days=7, limit=20)
        clamped = database.get_feedback_since(days=0, limit=999)

    assert len(rows) == 1 and rows[0]["plan_id"] == 3
    assert len(clamped) == 1, "非法 days 回退 7 后仍应查到今天的记录"

    print("PASS: get_feedback_since 返回 plan_id，days/limit 钳制行为不变")


def test_get_plan_by_id_edges():
    remove_file(UNIT_DB)

    with fixture_database(UNIT_DB):
        database.init_db()
        plan_id = database.insert_plan(
            "2026-11-10",
            {
                "task": "边界测试计划",
                "estimated_minutes": 40,
                "difficulty": "easy",
                "question_count": 4,
                "reason": "测试",
                "completion_criteria": "完成",
                "subtasks_json": "[]",
            },
            "2026-11-10T08:00:00",
        )

        found = database.get_plan_by_id(plan_id)

        assert found is not None and found["task"] == "边界测试计划"
        assert found["id"] == plan_id
        for invalid in (0, -1, 999999, "1", True, None, 1.5):
            assert database.get_plan_by_id(invalid) is None, f"非法输入 {invalid!r} 应返回 None"

    print("PASS: get_plan_by_id 正常命中，非法/不存在的 ID 一律返回 None")


# ==========================
# 三、HTTP 接口
# ==========================

_server = {"process": None, "base_url": None, "log": None}
_plan_cache = {}


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
    """启动一次测试服务（夹具库），模块结束时关闭。"""

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


def make_plan(**overrides):
    plan = {
        "task": "定积分基础巩固",
        "estimated_minutes": 40,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "根据最近反馈，先巩固基础。",
        "completion_criteria": "完成4道基础题并订正后结束。",
        "subtasks": [
            {"title": "复习公式", "minutes": 10, "question_count": 0,
             "description": "复习定积分基本公式"},
            {"title": "基础练习", "minutes": 30, "question_count": 4,
             "description": "完成指定基础题目"},
        ],
    }
    plan.update(overrides)
    return plan


def http_feedback_count(base_url):
    response = requests.get(f"{base_url}/feedback", timeout=10)
    assert response.status_code == 200
    return len(response.json()["feedback"])


def create_real_plan(base_url, plan_date=TODAY):
    """走前端同样的三步：创建凭证 -> 确认 -> 保存，拿到真实 plan_id。

    同一天只能有一条 active plan（数据库约束），所以按日期缓存复用。
    """

    if plan_date in _plan_cache:
        return _plan_cache[plan_date]

    created = requests.post(
        f"{base_url}/plan/confirmation",
        json={"plan": make_plan(), "plan_date": plan_date},
        timeout=10,
    )
    assert created.status_code == 200, created.text
    confirmation_id = created.json()["confirmation_id"]

    confirmed = requests.post(
        f"{base_url}/plan/confirmation/confirm",
        json={"confirmation_id": confirmation_id},
        timeout=10,
    )
    assert confirmed.status_code == 200, confirmed.text

    saved = requests.post(
        f"{base_url}/plan",
        json={"plan": make_plan(), "plan_date": plan_date,
              "confirmation_id": confirmation_id},
        timeout=10,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body.get("saved") is True, body

    _plan_cache[plan_date] = (body["plan_id"], body["plan"]["task"])
    return _plan_cache[plan_date]


def test_http_valid_plan_link():
    base_url = ensure_server()
    plan_id, task = create_real_plan(base_url)

    payload = {
        "task": task,
        "estimated_minutes": 40,
        "actual_minutes": 35,
        "status": "completed",
        "reason": "其他",
        "completed_subtasks": 2,
        "total_subtasks": 2,
        "completed_questions": 4,
        "total_questions": 4,
        "plan_id": plan_id,
    }

    response = requests.post(f"{base_url}/feedback", json=payload, timeout=10)
    assert response.status_code == 200, response.text
    assert response.json()["feedback"]["plan_id"] == plan_id

    created_at = response.json()["feedback"]["created_at"]
    listed = requests.get(f"{base_url}/feedback", timeout=10).json()["feedback"]
    matched = [
        row for row in listed
        if row["plan_id"] == plan_id and row["created_at"] == created_at
    ]
    assert len(matched) == 1, "GET /feedback 应能读回刚写入的关联记录"

    print(f"PASS: 有效 plan_id={plan_id} 关联保存并可在 GET /feedback 读回")


def test_http_invalid_plan_id_rejected():
    base_url = ensure_server()
    before = http_feedback_count(base_url)

    payload = {
        "task": "定积分基础巩固",
        "estimated_minutes": 40,
        "actual_minutes": 30,
        "status": "completed",
        "plan_id": 999999,
    }
    response = requests.post(f"{base_url}/feedback", json=payload, timeout=10)

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "invalid_plan_id"
    assert http_feedback_count(base_url) == before, "被拒绝的请求不得写库"

    print("PASS: 不存在的 plan_id -> 400 invalid_plan_id，且未写库")


def test_http_task_mismatch_rejected():
    base_url = ensure_server()
    plan_id, _task = create_real_plan(base_url)
    before = http_feedback_count(base_url)

    payload = {
        "task": "完全不同的任务",
        "estimated_minutes": 40,
        "actual_minutes": 30,
        "status": "completed",
        "plan_id": plan_id,
    }
    response = requests.post(f"{base_url}/feedback", json=payload, timeout=10)

    assert response.status_code == 400, response.text
    assert response.json()["error_type"] == "plan_task_mismatch"
    assert http_feedback_count(base_url) == before, "任务不匹配时不得写库"

    print("PASS: 任务名不匹配 -> 400 plan_task_mismatch，且未写库")


def test_http_missing_plan_id_is_compatible():
    base_url = ensure_server()
    before = http_feedback_count(base_url)

    payload = {
        "task": "旧客户端反馈",
        "estimated_minutes": 30,
        "actual_minutes": 20,
        "status": "partial",
        # 第二阶段 Task 0 起 reason 是服务端枚举：这里必须给五值之一
        "reason": "任务时间太长",
    }
    response = requests.post(f"{base_url}/feedback", json=payload, timeout=10)

    assert response.status_code == 200, response.text
    assert response.json()["feedback"]["plan_id"] is None
    assert http_feedback_count(base_url) == before + 1

    print("PASS: 不带 plan_id 的旧客户端请求仍 200，plan_id 为 null")


def test_http_plan_id_type_is_strict():
    base_url = ensure_server()

    base_payload = {
        "task": "定积分基础巩固",
        "estimated_minutes": 40,
        "actual_minutes": 30,
        "status": "completed",
    }

    for label, value in (("字符串 \"12\"", "12"), ("布尔 true", True), ("零 0", 0)):
        response = requests.post(
            f"{base_url}/feedback",
            json={**base_payload, "plan_id": value},
            timeout=10,
        )
        assert response.status_code == 422, f"{label} 应被拒绝，实际 {response.status_code}"

    print("PASS: plan_id 严格模式生效：\"12\" / true / 0 均为 422")


def count_plan_feedback(base_url, plan_id):
    """数出某个计划当前的反馈条数（断言增量而不是绝对值，避免依赖测试执行顺序）。"""

    listed = requests.get(f"{base_url}/feedback", timeout=10).json()["feedback"]
    return sum(1 for row in listed if row["plan_id"] == plan_id)


def test_http_multiple_feedback_for_same_plan():
    base_url = ensure_server()
    plan_id, task = create_real_plan(base_url)
    before = count_plan_feedback(base_url, plan_id)

    for _ in range(2):
        response = requests.post(
            f"{base_url}/feedback",
            json={
                "task": task,
                "estimated_minutes": 40,
                "actual_minutes": 20,
                "status": "partial",
                "reason": "任务时间太长",
                "plan_id": plan_id,
            },
            timeout=10,
        )
        assert response.status_code == 200, response.text

    after = count_plan_feedback(base_url, plan_id)
    assert after == before + 2, f"同一计划应允许重复提交：before={before}, after={after}"

    print("PASS: 同一计划允许重复提交反馈（不设唯一约束）")


def test_http_old_rows_still_readable():
    base_url = ensure_server()

    conn = connect(HTTP_DB)
    try:
        conn.execute(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions,
             total_questions, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("历史反馈", 30, 30, "completed", "无", 0, 0, 0, 0, "2026-10-01T10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    listed = requests.get(f"{base_url}/feedback", timeout=10).json()["feedback"]
    historical = [row for row in listed if row["task"] == "历史反馈"]

    assert len(historical) == 1 and historical[0]["plan_id"] is None

    print("PASS: 历史反馈（plan_id 为 NULL）经 GET /feedback 仍可读")


# ==========================
# 四、前端静态检查
# ==========================


def test_frontend_reads_plan_id_without_breaking_timing():
    html = (STATIC_DIR / "feedback.html").read_text(encoding="utf-8")

    assert 'sessionStorage.getItem("current_plan")' in html, "仍须读取 current_plan"
    assert "plan.plan_id" in html, "必须从计划对象读取 plan_id"
    assert "feedback.plan_id" in html, "必须把 plan_id 放进提交体"

    # 不得破坏原有计时 / 子任务统计逻辑
    assert "actual_study_seconds" in html
    assert "completed_subtask_indices" in html
    assert "completed_subtask_plan_key" in html

    # 没有 plan_id 时不得发送该字段（保持旧请求体一致）
    assert "if (planId !== null)" in html

    print("PASS: feedback.html 读取并提交 plan_id，计时/子任务逻辑未被改动")


# ==========================
# __main__ 运行器（同时兼容 pytest 收集）
# ==========================


def _run_all():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = []
    for test in tests:
        try:
            test()
        except Exception as error:  # noqa: BLE001 - 汇总所有失败
            failures.append((test.__name__, error))
            print(f"FAIL: {test.__name__} -> {type(error).__name__}: {error}")

    shutdown_server()

    # 清理夹具库，避免在仓库里留下 *_test_*.db
    for path in (MIGRATE_DB, UNIT_DB, HTTP_DB):
        try:
            remove_file(path)
        except AssertionError as error:
            print(f"INFO: 夹具未能删除（不影响结果）：{error}")

    real_db_bytes_after = REAL_DB.read_bytes() if REAL_DB.exists() else None
    check("真实 feedback.db 未被测试改动", real_db_bytes_after == _real_db_bytes_before)

    print()
    print(f"用例 {len(tests)} 个，通过 {len(tests) - len(failures)} 个，失败 {len(failures)} 个")

    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
