"""第三阶段 C2 前端闭环流程测试：直接运行 python test_plan_frontend_flow.py。

覆盖（真实 HTTP + 测试专用 SQLite）：
- 前端资源 smoke：plan.html 引用 plan-confirmation.js、关键函数/状态机存在
- 确认保存链路：POST /plan/confirmation -> /confirm -> POST /plan -> GET /plan 恢复
- 重复保存 -> active_plan_exists；伪造凭证 -> 拒绝；调包计划 -> mismatch
- Feedback 闭环：POST /feedback -> GET /feedback
- 真实模型端到端：POST /plan/draft（Agent 生成草案、不写库）-> 确认 -> 保存 -> GET /plan
- 安全验收：草案生成后 plans 不增加；无确认不保存

隔离：子进程通过 FEEDBACK_DB_PATH 使用测试库，绝不碰真实数据；
结束时校验真实 feedback.db / plans / knowledge / .rag_cache 未被改动。
"""

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import embedding_service
import requests


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_frontend_flow.db"
SERVER_LOG = PROJECT_DIR / "_test_frontend_flow_server.log"
STATIC_DIR = PROJECT_DIR / "static"

KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"

TODAY = "2026-10-06"  # 测试约定的"今天"（服务端保存/查询都用它）


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


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
        except Exception:
            time.sleep(0.4)
    return False


def fixture_plan_rows():
    connection = sqlite3.connect(FIXTURE_DB)

    try:
        return connection.execute(
            "SELECT id, plan_date, task, difficulty, status FROM plans ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def remove_fixture_files():
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(str(FIXTURE_DB) + suffix).unlink()
        except (FileNotFoundError, PermissionError):
            time.sleep(0.1)
            try:
                Path(str(FIXTURE_DB) + suffix).unlink()
            except (FileNotFoundError, PermissionError):
                pass


def knowledge_snapshot():
    return sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )


def real_plans_rows():
    if not REAL_DB.exists():
        return None

    try:
        with sqlite3.connect(str(REAL_DB)) as conn:
            return conn.execute(
                "SELECT id, plan_date, task, difficulty, status FROM plans ORDER BY id"
            ).fetchall()
    except sqlite3.OperationalError:
        return None


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
REAL_PLANS_BEFORE = real_plans_rows()
REAL_CACHE_EXISTED = REAL_CACHE.exists()
KNOWLEDGE_SNAPSHOT = knowledge_snapshot()

try:
    # ==========================
    # 1. 前端资源 smoke（不启动浏览器）
    # ==========================

    plan_html = (STATIC_DIR / "plan.html").read_text(encoding="utf-8")
    confirmation_js = (STATIC_DIR / "plan-confirmation.js").read_text(encoding="utf-8")

    # 剥离注释后检查实际代码（注释里可以解释安全边界，代码里不得出现）
    import re as _re

    confirmation_js_code = _re.sub(r"/\*.*?\*/", "", confirmation_js, flags=_re.S)
    confirmation_js_code = _re.sub(r"^\s*//.*$", "", confirmation_js_code, flags=_re.M)
    confirmation_js_code = _re.sub(r"^\s*/\*.*$", "", confirmation_js_code, flags=_re.M)

    detail_html = (STATIC_DIR / "detail.html").read_text(encoding="utf-8")
    study_html = (STATIC_DIR / "study.html").read_text(encoding="utf-8")
    feedback_html = (STATIC_DIR / "feedback.html").read_text(encoding="utf-8")

    check("plan.html 引用 plan-confirmation.js", "/app/plan-confirmation.js" in plan_html)
    check("plan.html 不再调用 /next-plan", "/next-plan" not in plan_html)
    check("plan.html 页面初始化使用服务端优先", "initPlanPage()" in plan_html)
    check("plan-confirmation.js 有确认状态机", "confirming" in confirmation_js and "draft_ready" in confirmation_js)
    check("确认流程调用服务端三步接口",
          '"/plan/confirmation"' in confirmation_js
          and '"/plan/confirmation/confirm"' in confirmation_js
          and '"/plan"' in confirmation_js)
    check("前端不接触 Agent 工具协议",
          "dispatcher" not in confirmation_js_code.lower()
          and "tool_schema" not in confirmation_js_code.lower()
          and "confirmed_by_user" not in confirmation_js_code)
    check("confirmation_id 不进 URL",
          "confirmation_id=" not in confirmation_js_code.replace("confirmation_id:", "")
          and "confirmation_id=" not in plan_html)
    check("使用本地日期而非 UTC",
          "getLocalDateString" in confirmation_js and "toISOString" not in confirmation_js)
    check("保存成功后以服务端计划同步 current_plan",
          "current_plan" in confirmation_js and "saved.plan" in confirmation_js.replace(" ", ""))
    check("detail/study/feedback 仍读取 current_plan",
          all('sessionStorage.getItem("current_plan")' in text for text in (detail_html, study_html, feedback_html)))
    check("detail 子任务完成状态仍使用 completed_subtask_indices",
          'completed_subtask_indices' in detail_html and 'completed_subtask_indices' in feedback_html)

    # ==========================
    # 2. 真实 HTTP 流程（测试库）
    # ==========================

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

        # 2.1 初始：今天没有计划（刷新恢复路径的"无计划"分支）
        response = requests.get(f"{base_url}/plan?date={TODAY}", timeout=10)
        check("GET /plan 无计划返回 exists=false",
              response.status_code == 200 and response.json().get("exists") is False)

        rows_before = fixture_plan_rows()
        check("初始 plans 为空", rows_before == [])

        # 2.2 安全验收：没有确认 -> 不能保存
        denied = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": TODAY, "confirmed_by_user": False},
            timeout=10,
        )
        check("未确认保存 -> 400 confirmation_required",
              denied.status_code == 400
              and denied.json()["error_type"] == "confirmation_required")
        check("未确认保存后 plans 不增加", fixture_plan_rows() == [])

        # 2.3 前端确认保存链路（与 plan-confirmation.js 相同的三步）
        created = requests.post(
            f"{base_url}/plan/confirmation",
            json={"plan": make_plan(), "plan_date": TODAY},
            timeout=10,
        )
        check("POST /plan/confirmation 成功", created.status_code == 200)
        confirmation_id = created.json()["confirmation_id"]

        confirmed = requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": confirmation_id},
            timeout=10,
        )
        check("POST /plan/confirmation/confirm 成功", confirmed.status_code == 200)

        saved = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(), "plan_date": TODAY, "confirmation_id": confirmation_id},
            timeout=10,
        )
        check("POST /plan（confirmation_id）保存成功",
              saved.status_code == 200 and saved.json().get("saved") is True,
              f"body={saved.text[:200]}")
        plan_id = saved.json()["plan_id"]

        rows = fixture_plan_rows()
        check("保存后 plans 恰好一条", len(rows) == 1 and rows[0][1] == TODAY)

        # 2.4 刷新恢复：GET /plan 取回服务端真实计划
        restored = requests.get(f"{base_url}/plan?date={TODAY}", timeout=10)
        check("刷新后 GET /plan 恢复计划",
              restored.status_code == 200
              and restored.json()["exists"] is True
              and restored.json()["plan"]["task"] == "定积分基础巩固")
        check("恢复的计划带 plan_id（服务端为准）",
              restored.json()["plan"]["plan_id"] == plan_id)

        # 2.5 重复保存 -> active_plan_exists（按钮重复点击兜底）
        duplicate_created = requests.post(
            f"{base_url}/plan/confirmation",
            json={"plan": make_plan(task="第二份计划"), "plan_date": TODAY},
            timeout=10,
        ).json()["confirmation_id"]
        requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": duplicate_created},
            timeout=10,
        )
        duplicate = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(task="第二份计划"), "plan_date": TODAY,
                  "confirmation_id": duplicate_created},
            timeout=10,
        )
        check("同日重复保存 -> 409 active_plan_exists",
              duplicate.status_code == 409
              and duplicate.json()["error_type"] == "active_plan_exists")
        check("重复保存没有产生第二条 active plan", len(fixture_plan_rows()) == 1)

        # 2.6 伪造 confirmation_id -> 拒绝
        fake = requests.post(
            f"{base_url}/plan",
            json={"plan": make_plan(task="伪造凭证计划"), "plan_date": "2026-10-07",
                  "confirmation_id": "fake-id-123"},
            timeout=10,
        )
        check("伪造 confirmation_id -> 404 confirmation_not_found",
              fake.status_code == 404 and fake.json()["error_type"] == "confirmation_not_found")
        check("伪造凭证后 plans 仍一条", len(fixture_plan_rows()) == 1)

        # 2.7 调包计划 -> mismatch
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
            json={"plan": make_plan(task="被调包的计划"), "plan_date": "2026-10-07",
                  "confirmation_id": other},
            timeout=10,
        )
        check("调包计划 -> 409 confirmation_mismatch",
              mismatch.status_code == 409
              and mismatch.json()["error_type"] == "confirmation_mismatch")
        check("调包后 plans 仍一条", len(fixture_plan_rows()) == 1)

        # 2.8 Feedback 闭环保持正常
        feedback = requests.post(
            f"{base_url}/feedback",
            json={
                "task": "定积分基础巩固",
                "estimated_minutes": 40,
                "actual_minutes": 35,
                "status": "completed",
                "reason": None,
                "completed_subtasks": 2,
                "total_subtasks": 2,
                "completed_questions": 4,
                "total_questions": 4,
            },
            timeout=10,
        )
        check("POST /feedback 保存成功", feedback.status_code == 200)
        listed = requests.get(f"{base_url}/feedback", timeout=10).json()["feedback"]

        # 注意：main.py 启动时 migrate_json_to_db() 会把 feedback_backup.json 的
        # 历史反馈自动播种进空库（既有项目行为），所以条数 = 备份条数 + 1 条新反馈
        new_records = [row for row in listed if row["task"] == "定积分基础巩固"
                       and row["actual_minutes"] == 35]
        check("GET /feedback 能读到新提交的反馈",
              len(new_records) == 1
              and new_records[0]["completed_subtasks"] == 2
              and new_records[0]["total_questions"] == 4,
              f"total={len(listed)} new={new_records}")

        # ==========================
        # 3. 真实模型端到端：POST /plan/draft（Agent 生成草案，不保存）
        # ==========================

        try:
            embedding_service.get_embedding("前置检查")
        except embedding_service.EmbeddingError as error:
            raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

        info("真实模型草案生成", "开始（可能需要 1-3 分钟）")
        draft = requests.post(f"{base_url}/plan/draft", timeout=420)

        check("POST /plan/draft 返回 200", draft.status_code == 200,
              f"body={draft.text[:200]}")
        draft_data = draft.json()
        check("草案结构完整（answer + plan 同时存在）",
              draft_data.get("ok") is True
              and draft_data.get("type") == "plan_draft"
              and isinstance(draft_data.get("answer"), str)
              and draft_data["answer"].strip()
              and isinstance(draft_data.get("plan"), dict))
        check("草案包含全部 7 个字段",
              set(draft_data["plan"]) == {"task", "estimated_minutes", "difficulty",
                                          "question_count", "reason", "completion_criteria", "subtasks"})
        check("草案阶段没有写 plans（未确认不落库）", len(fixture_plan_rows()) == 1)

        info("真实草案", f"task={draft_data['plan']['task']} "
                     f"answer={draft_data['answer'][:80].replace(chr(10), ' ')}")

        # 用 Agent 真实草案走确认 -> 保存 -> 恢复（产品闭环）
        agent_plan = draft_data["plan"]
        draft_created = requests.post(
            f"{base_url}/plan/confirmation",
            json={"plan": agent_plan, "plan_date": "2026-10-07"},
            timeout=10,
        )
        check("Agent 草案可以创建确认", draft_created.status_code == 200,
              f"body={draft_created.text[:200]}")

        agent_confirmation_id = draft_created.json()["confirmation_id"]
        requests.post(
            f"{base_url}/plan/confirmation/confirm",
            json={"confirmation_id": agent_confirmation_id},
            timeout=10,
        )

        agent_saved = requests.post(
            f"{base_url}/plan",
            json={"plan": agent_plan, "plan_date": "2026-10-07",
                  "confirmation_id": agent_confirmation_id},
            timeout=10,
        )
        check("Agent 草案经用户确认后保存成功",
              agent_saved.status_code == 200 and agent_saved.json().get("saved") is True,
              f"body={agent_saved.text[:200]}")

        restored_agent = requests.get(f"{base_url}/plan?date=2026-10-07", timeout=10)
        check("保存后的 Agent 计划可通过 GET /plan 恢复",
              restored_agent.status_code == 200
              and restored_agent.json()["exists"] is True
              and restored_agent.json()["plan"]["task"] == agent_plan["task"])
        check("闭环后 plans 共两条（今天 + Agent 明天）", len(fixture_plan_rows()) == 2)

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

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
    check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
    check("真实 plans 表未被修改", real_plans_rows() == REAL_PLANS_BEFORE)
    check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)
    check("knowledge/ 未被修改", knowledge_snapshot() == KNOWLEDGE_SNAPSHOT)

print("\n全部用例通过")
