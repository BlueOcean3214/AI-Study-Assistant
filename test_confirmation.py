"""Confirmation Service 测试：直接运行 python test_confirmation.py。

覆盖：
- 服务层：签发 / 初始状态 / 唯一性 / 过期 / 确认 / 重复确认 / 绑定匹配 / 伪造 id / 消费
- API 层：POST /plan/confirmation + /plan/confirmation/confirm + POST /plan(confirmation_id)
  （子进程启动真实 uvicorn，与 test_plan_api 同一模式）

隔离：API 测试通过 FEEDBACK_DB_PATH 使用测试专用 SQLite，绝不碰真实 feedback.db。
"""

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import confirmation_service
import plan_service


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_confirmation.db"
SERVER_LOG = PROJECT_DIR / "_test_confirmation_server.log"

REAL_DB = PROJECT_DIR / "feedback.db"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def make_plan(**overrides):
    plan = {
        "task": "定积分基础巩固",
        "estimated_minutes": 40,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "根据最近反馈，先巩固基础。",
        "completion_criteria": "完成4道基础题并订正后结束。",
        "subtasks": [
            {"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习定积分基本公式"},
            {"title": "基础练习", "minutes": 30, "question_count": 4, "description": "完成指定基础题目"},
        ],
    }
    plan.update(overrides)
    return plan


def normalized(plan):
    value, error = plan_service.normalize_plan(plan)
    if error:
        raise AssertionError(f"测试计划非法：{error}")
    return value


def fixture_plan_count():
    connection = sqlite3.connect(FIXTURE_DB)

    try:
        return connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    finally:
        connection.close()


def pick_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def remove_fixture_files():
    """删除测试库及其附属 journal/wal 文件（孤立 journal 会让新库损坏）。"""

    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(str(FIXTURE_DB) + suffix)
        try:
            candidate.unlink()
        except (FileNotFoundError, PermissionError):
            time.sleep(0.1)
            try:
                candidate.unlink()
            except (FileNotFoundError, PermissionError):
                pass


def wait_until_ready(base_url, timeout=40):
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{base_url}/", timeout=2).status_code == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None

confirmation_service.reset_confirmations()

try:
    # ==========================
    # 1. 服务层：签发与初始状态
    # ==========================

    plan = normalized(make_plan())
    created = confirmation_service.create_pending_confirmation(plan, "2026-10-06")

    check("创建 pending confirmation 成功", created["ok"] is True)
    check("返回 confirmation_id", isinstance(created["confirmation_id"], str) and len(created["confirmation_id"]) >= 16)
    check("初始状态未确认", created["confirmed"] is False and created["status"] == "pending")
    check("凭证与 plan/plan_date 绑定",
          created["plan"] == plan and created["plan_date"] == "2026-10-06")
    check("有效期晚于创建时间", created["expires_at"] > created["created_at"])

    stored = confirmation_service.get_confirmation(created["confirmation_id"])
    check("可以读取凭证", stored is not None and stored["confirmed"] is False)
    check("读取的是副本（外部无法改动内部状态）",
          stored is not created and stored["plan"] is not plan)

    # 2. confirmation_id 唯一
    ids = {created["confirmation_id"]}
    for _ in range(50):
        another = confirmation_service.create_pending_confirmation(plan, "2026-10-06")
        ids.add(another["confirmation_id"])

    check("confirmation_id 全局唯一", len(ids) == 51, f"n={len(ids)}")

    # 3. 伪造 id / 空 id
    fake = confirmation_service.validate_for_save("deadbeef" * 4, plan, "2026-10-06")
    check("伪造 confirmation_id 被拒绝",
          fake["ok"] is False and fake["error_type"] == "confirmation_not_found")
    empty = confirmation_service.validate_for_save("", plan, "2026-10-06")
    check("空 confirmation_id 被拒绝",
          empty["ok"] is False and empty["error_type"] == "confirmation_required")

    # 4. 未确认不能保存
    pending = confirmation_service.validate_for_save(
        created["confirmation_id"], plan, "2026-10-06"
    )
    check("pending 凭证不能保存",
          pending["ok"] is False and pending["error_type"] == "confirmation_required")

    # 5. 确认与重复确认
    confirmed = confirmation_service.confirm_confirmation(created["confirmation_id"])
    check("用户确认后状态为 confirmed",
          confirmed["ok"] is True and confirmed["status"] == "confirmed")
    check("存储状态已更新",
          confirmation_service.get_confirmation(created["confirmation_id"])["confirmed"] is True)

    repeat = confirmation_service.confirm_confirmation(created["confirmation_id"])
    check("重复确认返回稳定错误（不改变状态）",
          repeat["ok"] is False
          and repeat["error_type"] == "confirmation_already_confirmed"
          and confirmation_service.get_confirmation(created["confirmation_id"])["confirmed"] is True)

    # 6. 确认后可保存，但绑定必须完全匹配
    ok = confirmation_service.validate_for_save(created["confirmation_id"], plan, "2026-10-06")
    check("确认后的凭证可以通过保存校验", ok["ok"] is True, f"{ok}")

    changed_plan = normalized(make_plan(task="被替换的任务"))
    mismatch = confirmation_service.validate_for_save(
        created["confirmation_id"], changed_plan, "2026-10-06"
    )
    check("plan 内容不匹配被拒绝",
          mismatch["ok"] is False and mismatch["error_type"] == "confirmation_mismatch")

    date_mismatch = confirmation_service.validate_for_save(
        created["confirmation_id"], plan, "2026-10-07"
    )
    check("plan_date 不匹配被拒绝",
          date_mismatch["ok"] is False and date_mismatch["error_type"] == "confirmation_mismatch")

    numeric_change = normalized(make_plan(estimated_minutes=45))
    numeric_mismatch = confirmation_service.validate_for_save(
        created["confirmation_id"], numeric_change, "2026-10-06"
    )
    check("数值被篡改同样被拒绝",
          numeric_mismatch["ok"] is False and numeric_mismatch["error_type"] == "confirmation_mismatch")

    # 字段顺序不同不影响绑定（canonical hash）
    reordered = {key: plan[key] for key in reversed(list(plan))}
    same = confirmation_service.validate_for_save(
        created["confirmation_id"], reordered, "2026-10-06"
    )
    check("字段顺序不同不影响绑定匹配", same["ok"] is True, f"{same}")

    # 7. 过期：不能确认、不能保存
    expired = confirmation_service.create_pending_confirmation(
        plan, "2026-10-06", ttl_seconds=0, now=1000.0
    )
    check("可以创建立即过期的凭证（测试用）", expired["ok"] is True)

    expired_confirm = confirmation_service.confirm_confirmation(
        expired["confirmation_id"], now=1001.0
    )
    check("过期凭证不能确认",
          expired_confirm["ok"] is False and expired_confirm["error_type"] == "confirmation_expired")

    expired_save = confirmation_service.validate_for_save(
        expired["confirmation_id"], plan, "2026-10-06", now=1001.0
    )
    check("过期凭证不能保存",
          expired_save["ok"] is False and expired_save["error_type"] == "confirmation_expired")

    confirmed_then_expired = confirmation_service.create_pending_confirmation(
        plan, "2026-10-06", ttl_seconds=10, now=1000.0
    )
    confirmation_service.confirm_confirmation(confirmed_then_expired["confirmation_id"], now=1005.0)
    late_save = confirmation_service.validate_for_save(
        confirmed_then_expired["confirmation_id"], plan, "2026-10-06", now=2000.0
    )
    check("已确认但过期的凭证不能保存",
          late_save["ok"] is False and late_save["error_type"] == "confirmation_expired")

    # 8. 消费（一次性）：保存成功后不能再次写库
    consumable = confirmation_service.create_pending_confirmation(plan, "2026-10-06")
    confirmation_service.confirm_confirmation(consumable["confirmation_id"])
    check("消费前校验通过",
          confirmation_service.validate_for_save(consumable["confirmation_id"], plan, "2026-10-06")["ok"] is True)

    confirmation_service.mark_consumed(consumable["confirmation_id"])
    consumed = confirmation_service.validate_for_save(
        consumable["confirmation_id"], plan, "2026-10-06"
    )
    check("已消费的凭证不能再次保存",
          consumed["ok"] is False and consumed["error_type"] == "confirmation_already_used")
    check("mark_consumed 幂等", confirmation_service.mark_consumed(consumable["confirmation_id"]) is True)

    # 9. 签发参数守门
    bad_plan = confirmation_service.create_pending_confirmation("不是字典", "2026-10-06")
    check("plan 非对象拒绝签发", bad_plan["ok"] is False)
    bad_date = confirmation_service.create_pending_confirmation(plan, "2026-10-06T00:00:00")
    check("plan_date 超长拒绝签发", bad_date["ok"] is False)
    bad_ttl = confirmation_service.create_pending_confirmation(plan, "2026-10-06", ttl_seconds=-1)
    check("负 TTL 拒绝签发", bad_ttl["ok"] is False)
    unknown_confirm = confirmation_service.confirm_confirmation("不存在的id")
    check("确认不存在的凭证 -> not_found",
          unknown_confirm["ok"] is False and unknown_confirm["error_type"] == "confirmation_not_found")

    # ==========================
    # 10. API 层：确认流程端到端（真实 HTTP + 测试库）
    # ==========================

    import requests

    remove_fixture_files()

    port = pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = dict(os.environ, FEEDBACK_DB_PATH=str(FIXTURE_DB))

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_DIR),
        env=environment,
        stdout=open(SERVER_LOG, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    try:
        check("测试服务器启动", wait_until_ready(base_url), f"log={SERVER_LOG.name}")

        # 创建 pending
        response = requests.post(
            f"{base_url}/plan/confirmation",
            json={"plan": make_plan(), "plan_date": "2026-10-06"},
            timeout=10,
        )
        check("POST /plan/confirmation 返回 200", response.status_code == 200, f"body={response.text[:200]}")
        confirmation_id = response.json()["confirmation_id"]
        check("API 签发的凭证为 pending", response.json()["status"] == "pending")

        # 未确认直接保存 -> 拒绝
        response = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": "2026-10-06", "confirmation_id": confirmation_id},
            timeout=10,
        )
        check("未确认凭证保存 -> 400 confirmation_required",
              response.status_code == 400
              and response.json()["error_type"] == "confirmation_required")

        # 确认
        response = requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": confirmation_id},
            timeout=10,
        )
        check("POST /plan/confirmation/confirm 返回 200", response.status_code == 200)

        repeat = requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": confirmation_id},
            timeout=10,
        )
        check("重复确认 -> 409 confirmation_already_confirmed",
              repeat.status_code == 409
              and repeat.json()["error_type"] == "confirmation_already_confirmed")

        # 伪造 id 确认 -> 404
        fake_confirm = requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": "deadbeefdeadbeef"},
            timeout=10,
        )
        check("伪造 id 确认 -> 404 confirmation_not_found",
              fake_confirm.status_code == 404
              and fake_confirm.json()["error_type"] == "confirmation_not_found")

        # 确认后保存成功
        response = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": "2026-10-06", "confirmation_id": confirmation_id},
            timeout=10,
        )
        check("确认后 POST /plan(confirmation_id) 返回 200",
              response.status_code == 200 and response.json()["saved"] is True,
              f"body={response.text[:200]}")
        check("保存成功返回 plan_id", isinstance(response.json().get("plan_id"), int))

        check("写库恰好一条 plan", fixture_plan_count() == 1)

        # 凭证一次性：再次保存 -> 409 already_used
        response = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": "2026-10-06", "confirmation_id": confirmation_id},
            timeout=10,
        )
        check("同一凭证再次保存 -> 409 confirmation_already_used",
              response.status_code == 409
              and response.json()["error_type"] == "confirmation_already_used")

        # 换一份计划使用同一凭证 -> 409 mismatch（绑定不可篡改）
        # 注意：凭证必须在服务器进程内创建（API 创建），跨进程内存不共享
        other = requests.post(
            f"{base_url}/plan/confirmation",
            json={"plan": make_plan(), "plan_date": "2026-10-07"},
            timeout=10,
        ).json()["confirmation_id"]
        requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": other},
            timeout=10,
        )
        mismatch = requests.post(
            f"{base_url}/plan",
            json={
                "plan": make_plan(task="被调包的计划"),
                "plan_date": "2026-10-07",
                "confirmation_id": other,
            },
            timeout=10,
        )
        check("调包计划 -> 409 confirmation_mismatch",
              mismatch.status_code == 409
              and mismatch.json()["error_type"] == "confirmation_mismatch")

        check("调包被拒后 plans 仍只有一条", fixture_plan_count() == 1)

        # 旧 API 兼容：confirmed_by_user=True 直连保存仍然可用
        legacy = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(task="旧接口直连计划"), "plan_date": "2026-10-08",
                  "confirmed_by_user": True},
            timeout=10,
        )
        check("旧 API（confirmed_by_user=True）兼容可用",
              legacy.status_code == 200 and legacy.json()["saved"] is True,
              f"body={legacy.text[:200]}")

        # 旧 API 未确认仍然拒绝
        legacy_denied = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": "2026-10-09", "confirmed_by_user": False},
            timeout=10,
        )
        check("旧 API 未确认 -> 400 confirmation_required",
              legacy_denied.status_code == 400
              and legacy_denied.json()["error_type"] == "confirmation_required")

        # 非法计划不能进入确认流程
        bad_create = requests.post(
            f"{base_url}/plan/confirmation",
            json={"plan": make_plan(extra_field="x"), "plan_date": "2026-10-06"},
            timeout=10,
        )
        check("非法计划拒绝创建确认", bad_create.status_code == 400)

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # 等待进程退出后清理测试库（Windows 上文件可能被短暂占用）
    for _ in range(50):
        if not FIXTURE_DB.exists():
            break
        try:
            FIXTURE_DB.unlink()
        except PermissionError:
            time.sleep(0.2)

    remove_fixture_files()

    check("测试库已清理", not FIXTURE_DB.exists())

finally:
    confirmation_service.reset_confirmations()

check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)

print("\n全部用例通过")
