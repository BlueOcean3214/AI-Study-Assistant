"""真实数据模式（--real-data）的只读边界回归。

覆盖第三轮审查提出的 P1/P2：

1. 真实数据模式只执行显式 read_only=True 的用例，P5/P6 等含保存/确认/写库
   路径的用例被标为 SKIP、不执行、不计入 PASS；
2. run_case(isolate=False) 对未声明只读的用例 fail-closed 拒绝，不能靠用例
   名称推断安全；
3. 真实数据模式不清理固定夹具（_eval_fixture.db / _eval_cache），也不写
   真实 .rag_cache 与 rag_metrics.jsonl；
4. 数据安全指纹记录"实际生效路径"（数据库与 RAG 缓存都可能被环境变量覆盖），
   内容变化必须让报告 ok=False；用例异常退出时仍要完成这项验证；
5. 只读白名单绑定 agent_eval_cases 的规范对象（身份 + 结构指纹），同名伪造
   字典、白名单外新 id、注入 setup/after_run 都无法借 read_only=True 混进来；
6. 真实数据模式在 Agent 工具层也只读：Schema 不暴露 save_plan，Dispatcher
   对写工具直接拒绝；退出后完整 Schema 恢复；
7. 真实数据模式不把用量写入真实库，且退出后记录器与记录能力都恢复。

全部使用 tempfile 临时目录、临时 SQLite 与合成数据；不读写正式 feedback.db、
不碰仓库里的 _eval_fixture.db / _eval_cache，不调用真实 LLM / Embedding。
"""

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import agent_dispatcher
import agent_eval
import agent_eval_cases
import agent_plan_draft
import agent_schema
import confirmation_service
import database
import embedding_service
import llm_client
import model_usage
import plan_service
import rag_service
import vector_cache


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def fingerprint(path):
    """文件 -> (sha256, size)；目录 -> 内容聚合指纹；不存在 -> None。"""

    path = Path(path)
    if not path.exists():
        return None
    if path.is_dir():
        digest = hashlib.sha256()
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            digest.update(item.relative_to(path).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(item.read_bytes()).digest())
        return ("dir", digest.hexdigest())
    data = path.read_bytes()
    return ("file", hashlib.sha256(data).hexdigest(), len(data))


def main():
    root = Path(tempfile.mkdtemp(prefix="dsbot_real_data_safety_"))
    sentinel_dir = root / "fixture"
    sentinel_db = sentinel_dir / "_eval_fixture.db"
    sentinel_cache = sentinel_dir / "_eval_cache"
    workspace_db = root / "workspace" / "feedback.db"
    repo_sentinel_db = root / "repo" / "feedback.db"
    repo_sentinel_cache = root / "repo" / ".rag_cache"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    repo_sentinel_db.parent.mkdir(parents=True, exist_ok=True)
    repo_sentinel_db.write_bytes(b"repo-db-sentinel-not-real")
    repo_sentinel_cache.mkdir(parents=True, exist_ok=True)
    (repo_sentinel_cache / "sentinel.bin").write_bytes(b"repo-cache-sentinel")

    base_cache = root / "base_cache"
    knowledge_dir = root / "knowledge"
    metrics_file = root / "rag_metrics.jsonl"

    original_db = database.DATABASE_PATH
    original_cache = vector_cache.CACHE_DIR
    original_real_db = agent_eval.REAL_DB
    original_real_cache = agent_eval.REAL_CACHE
    original_fixture_db = agent_eval.FIXTURE_DB
    original_fixture_cache = agent_eval.FIXTURE_CACHE
    original_knowledge = agent_eval.KNOWLEDGE_DIR
    original_metrics_file = agent_eval.RETRIEVAL_METRICS_FILE
    original_clean = agent_eval.clean_fixtures
    original_run_case = agent_eval.run_case
    original_run_all = agent_eval.run_all
    original_plan_draft_runner = agent_plan_draft.run_plan_draft
    original_save_plan = plan_service.save_plan_with_confirmation
    original_create_pending = confirmation_service.create_pending_confirmation
    original_insert_plan = database.insert_plan
    original_delete_all = database.delete_all_feedback
    original_metrics = rag_service._log_retrieval_metrics
    original_usage_recorder = llm_client.get_usage_recorder()
    original_llm_settings = llm_client.get_settings
    original_chat_openai = llm_client._chat_openai

    try:
        # 仓库级常量也指向哨兵：真实 feedback.db / .rag_cache 连读都不做。
        agent_eval.REAL_DB = repo_sentinel_db
        agent_eval.REAL_CACHE = repo_sentinel_cache
        # 固定夹具指向本测试自己的哨兵，绝不落到仓库里的真实夹具。
        agent_eval.FIXTURE_DB = sentinel_db
        agent_eval.FIXTURE_CACHE = sentinel_cache
        # 知识库与检索埋点也重映射到临时目录：本测试不读仓库任何数据文件。
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        (knowledge_dir / "note.txt").write_text("合成知识库条目", encoding="utf-8")
        agent_eval.KNOWLEDGE_DIR = knowledge_dir
        agent_eval.RETRIEVAL_METRICS_FILE = metrics_file
        sentinel_db.write_bytes(b"sentinel-fixture-db")
        sentinel_cache.mkdir(parents=True, exist_ok=True)
        (sentinel_cache / "keep.txt").write_text("sentinel-cache", encoding="utf-8")
        base_cache.mkdir(parents=True, exist_ok=True)
        vector_cache.CACHE_DIR = base_cache

        # 模拟 FEEDBACK_DB_PATH 指向临时库：真实数据模式保护的是"实际生效路径"。
        database.DATABASE_PATH = workspace_db
        database.init_db()
        agent_eval.seed_database("difficult_history")
        seeded_rows = len(database.get_feedback_list())

        # ---- 1) fixture 模式仍然调度完整 EVAL_CASES，P5/P6 覆盖不退化 ----
        fixture_calls = []
        clean_calls = []

        def recording_run_case(case, runner=None, isolate=True, verbose=False):
            fixture_calls.append((case["id"], isolate))
            return {
                "case_id": case["id"],
                "tags": [],
                "message": "",
                "duration_seconds": 0.0,
                "evaluation": {"hard_failures": [], "deviations": [], "observed": {}},
            }

        def recording_clean():
            clean_calls.append(True)

        agent_eval.run_case = recording_run_case
        agent_eval.clean_fixtures = recording_clean
        try:
            fixture_report = agent_eval.run_all(isolate=True)
        finally:
            agent_eval.run_case = original_run_case
            agent_eval.clean_fixtures = original_clean

        check(
            "fixture 模式调度完整 EVAL_CASES（P5/P6 不被过滤）",
            [case_id for case_id, _ in fixture_calls] == [case["id"] for case in agent_eval_cases.EVAL_CASES]
            and "P5_save_confirmed" in [case_id for case_id, _ in fixture_calls]
            and "P6_plan_confirmation_flow" in [case_id for case_id, _ in fixture_calls],
            f"cases={len(fixture_calls)}",
        )
        check(
            "fixture 模式全部 isolate=True 且没有 SKIP",
            all(isolate for _, isolate in fixture_calls)
            and fixture_report["summary"]["cases"] == len(agent_eval_cases.EVAL_CASES)
            and fixture_report["summary"]["skipped"] == 0,
            f"summary={fixture_report['summary']}",
        )
        check("fixture 模式仍在收尾时清理固定夹具", clean_calls == [True], f"clean_calls={clean_calls}")

        # ---- 2) 未声明只读的用例在 isolate=False 下被拒绝 ----
        rejected = []
        for probe in (
            {"id": "X_no_flag", "message": "hi", "tags": []},
            {"id": "A_history_only", "message": "hi", "tags": [], "read_only": False},
            {"id": "P5_save_confirmed", "message": "hi", "tags": [], "read_only": True},
            {"id": "X_fake_readonly_id", "message": "hi", "tags": [], "read_only": True},
        ):
            try:
                agent_eval.run_case(probe, runner=lambda *a, **k: {}, isolate=False)
            except agent_eval.DataSafetyError as error:
                rejected.append(str(error))
            else:
                raise AssertionError(f"FAILED: isolate=False 居然执行了 {probe['id']}")

        check(
            "未声明只读 / 只读但不在白名单的用例都被 fail-closed 拒绝",
            len(rejected) == 4 and all("read_only=True" in text for text in rejected),
            f"rejected={len(rejected)}",
        )

        # ---- 2b) 只读白名单不可伪造：同 id 字典、新 id、注入写回调一律拒绝 ----
        # 「自己写个 dict 声明 read_only=True」必须无效：白名单绑定的是
        # agent_eval_cases 里那个规范对象本身（身份 + 结构指纹），不是 id 字符串。
        setup_hits = []
        after_hits = []

        def evil_setup():
            setup_hits.append("setup")

        def evil_after_run(_result):
            after_hits.append("after_run")

        forged_probes = [
            ("同 id 伪造只读 + 恶意 setup", {"id": "A_history_only", "message": "hi", "tags": [],
                                              "read_only": True, "setup": evil_setup}),
            ("同 id 伪造只读 + 恶意 after_run", {"id": "A_history_only", "message": "hi", "tags": [],
                                                  "read_only": True, "after_run": evil_after_run}),
            ("同 id 伪造只读但不带写回调", {"id": "A_history_only", "message": "hi", "tags": [],
                                            "read_only": True}),
            ("伪造白名单里不存在的新 id", {"id": "Z_brand_new", "message": "hi", "tags": [],
                                            "read_only": True}),
        ]
        forged_rejected = []
        for label, probe in forged_probes:
            try:
                agent_eval.run_case(probe, runner=lambda *a, **k: {}, isolate=False)
            except agent_eval.DataSafetyError as error:
                forged_rejected.append((label, str(error)))
            else:
                raise AssertionError(f"FAILED: 伪造用例居然执行了：{label}")

        canonical_a = agent_eval_cases.get_case("A_history_only")
        check(
            "规范只读用例在被注入写回调前是可信的",
            agent_eval_cases.canonical_case(canonical_a) is canonical_a
            and agent_eval.case_is_read_only(canonical_a) is True,
            "",
        )

        injected_rejected = []
        for field, hook in (("setup", evil_setup), ("after_run", evil_after_run)):
            canonical_a[field] = hook
            try:
                agent_eval.run_case(canonical_a, runner=lambda *a, **k: {}, isolate=False)
            except agent_eval.DataSafetyError as error:
                injected_rejected.append((field, str(error)))
            else:
                raise AssertionError(f"FAILED: 规范对象被注入 {field} 后仍然执行")
            finally:
                del canonical_a[field]

        check(
            "同 id 伪造 / 新 id / 注入写回调的用例全部被拒绝",
            len(forged_rejected) == 4 and len(injected_rejected) == 2,
            f"forged={[label for label, _ in forged_rejected]} injected={[f for f, _ in injected_rejected]}",
        )
        check(
            "case_is_read_only 不认任何复制 id 的伪造字典（身份绑定不可绕过）",
            all(agent_eval.case_is_read_only(probe) is False for _, probe in forged_probes),
            "",
        )
        check(
            "被拒绝的伪造用例一次都没有执行注入的回调",
            setup_hits == [] and after_hits == [],
            f"setup={setup_hits} after_run={after_hits}",
        )
        check(
            "注入的回调移除后规范用例恢复可信（指纹自愈）",
            agent_eval_cases.canonical_case(canonical_a) is canonical_a
            and agent_eval.case_is_read_only(canonical_a) is True,
            "",
        )
        check(
            "11 个规范只读用例仍然全部被认可并可调度",
            len(agent_eval_cases.READ_ONLY_CASE_IDS) == 11
            and all(
                agent_eval.case_is_read_only(case)
                for case in agent_eval_cases.EVAL_CASES
                if case["id"] in agent_eval_cases.READ_ONLY_CASE_IDS
            ),
            f"ids={sorted(agent_eval_cases.READ_ONLY_CASE_IDS)}",
        )

        # ---- 3) 真实数据模式：只跑白名单、不碰保存/清理/缓存 ----
        forbidden_hits = []

        def forbidden(name):
            def _raise(*_args, **_kwargs):
                forbidden_hits.append(name)
                raise AssertionError(f"真实数据模式调用了写路径：{name}")

            return _raise

        observed = {}
        plan_draft_calls = []
        llm_calls = []

        def model_usage_rows():
            connection = sqlite3.connect(database.DATABASE_PATH)
            try:
                return connection.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0]
            finally:
                connection.close()

        def stub_chat_openai(settings, messages, tools, deadline, read_timeout, sampling):
            """合成 LLM 调用：第 1 次成功、第 2 次失败，用来验证用量记录器被关闭。"""

            llm_calls.append(len(messages))
            if len(llm_calls) == 1:
                return (
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                  "total_tokens": 5, "available": True},
                    },
                    None,
                    None,
                )

            return None, "stub llm failure", "llm_error"

        def real_runner(message, verbose=False):
            observed["rows"] = len(database.get_feedback_list())
            observed["cache_dir"] = Path(vector_cache.CACHE_DIR)
            observed["cache_exists"] = observed["cache_dir"].is_dir()
            observed["metrics"] = rag_service._log_retrieval_metrics

            # 只在第一次执行时探针：当前窗口里 Agent 能看到的 Schema、用量记录器状态，
            # 以及成功/失败两条 LLM 调用路径是否写 model_usage。
            if "tool_schema" not in observed:
                observed["tool_schema"] = [
                    schema["function"]["name"] for schema in agent_schema.get_tool_schemas()
                ]
                observed["read_only_mode"] = agent_schema.is_read_only_mode()
                observed["recorder_during_run"] = llm_client.get_usage_recorder()
                rows_before = model_usage_rows()
                llm_client.chat([{"role": "user", "content": "stub"}])
                llm_client.chat([{"role": "user", "content": "stub fail"}])
                observed["usage_rows"] = (rows_before, model_usage_rows())

            return {
                "type": "chat",
                "answer": "好的。",
                "steps": [],
                "stopped_reason": "completed",
                "error_type": None,
            }

        def stub_plan_draft(message, verbose=False):
            """plan_draft 用例在评测里会绕过传入的 runner，必须单独打桩，禁止真实 LLM。"""

            plan_draft_calls.append(message)
            return {
                "type": agent_plan_draft.TYPE_PLAN_ERROR,
                "error_type": "stubbed_by_test",
                "message": "真实数据安全测试桩：不调用模型",
                "steps": [],
                "stopped_reason": "error",
            }

        plan_service.save_plan_with_confirmation = forbidden("save_plan_with_confirmation")

        # P5 的 setup 会调用 confirmation_service.create_pending_confirmation 建凭证：
        # 监控真正所在的模块（plan_service 里没有这个函数，监控不到就等于没测）。
        create_pending_hits = []

        def forbidden_create_pending(*_args, **_kwargs):
            create_pending_hits.append("call")
            raise AssertionError("真实数据模式调用了 confirmation_service.create_pending_confirmation")

        confirmation_service.create_pending_confirmation = forbidden_create_pending
        database.insert_plan = forbidden("insert_plan")
        database.delete_all_feedback = forbidden("delete_all_feedback")
        agent_eval.clean_fixtures = forbidden("clean_fixtures")
        agent_plan_draft.run_plan_draft = stub_plan_draft

        db_before = fingerprint(workspace_db)
        sentinel_db_before = fingerprint(sentinel_db)
        sentinel_cache_before = fingerprint(sentinel_cache)
        metrics_before = fingerprint(agent_eval.RETRIEVAL_METRICS_FILE)

        # P6/P7 的 after_run 是明确的写库回调：真实数据模式下必须一次都不执行。
        after_run_hits = []
        original_hooks = {}
        for case in agent_eval_cases.EVAL_CASES:
            hook = case.get("after_run")

            if hook is None:
                continue

            original_hooks[case["id"]] = hook

            def make_hook(case_id, real_hook):
                def _hook(run_result):
                    after_run_hits.append(case_id)
                    raise AssertionError(f"真实数据模式执行了 {case_id} 的 after_run 写库回调")
                return _hook

            case["after_run"] = make_hook(case["id"], hook)

        # 哨兵记录器：真实模式必须把它临时换成 None，退出时原样换回。
        sentinel_events = []

        def sentinel_recorder(record):
            sentinel_events.append(record)

        llm_client.set_usage_recorder(sentinel_recorder)
        llm_client.get_settings = lambda: llm_client.Settings(
            provider=llm_client.PROVIDER_OPENAI,
            model="stub-model",
            chat_url="http://stub.invalid/v1/chat/completions",
        )
        llm_client._chat_openai = stub_chat_openai

        try:
            real_report = agent_eval.run_all(runner=real_runner, isolate=False)
        finally:
            llm_client.get_settings = original_llm_settings
            llm_client._chat_openai = original_chat_openai
            for case in agent_eval_cases.EVAL_CASES:
                if case["id"] in original_hooks:
                    case["after_run"] = original_hooks[case["id"]]
            plan_service.save_plan_with_confirmation = original_save_plan
            confirmation_service.create_pending_confirmation = original_create_pending
            database.insert_plan = original_insert_plan
            database.delete_all_feedback = original_delete_all
            agent_eval.clean_fixtures = original_clean
            agent_plan_draft.run_plan_draft = original_plan_draft_runner

        executed = [item["case_id"] for item in real_report["cases"]]
        skipped = {item["case_id"]: item["reason"] for item in real_report["skipped"]}

        check(
            "真实数据模式只执行 read_only 白名单",
            set(executed) == set(agent_eval_cases.READ_ONLY_CASE_IDS)
            and len(executed) == len(set(executed))
            and all(
                case["id"] in agent_eval_cases.READ_ONLY_CASE_IDS
                and case.get("read_only") is True
                for case in agent_eval_cases.EVAL_CASES
                if case["id"] in executed
            ),
            f"executed={len(executed)}",
        )
        check(
            "P4/P5/P6/P7 全部标为 SKIP 并写明原因",
            set(skipped) == {"P4_confirmation_required", "P5_save_confirmed",
                             "P6_plan_confirmation_flow", "P7_unconfirmed_plan_cannot_save"}
            and all("非只读用例" in reason for reason in skipped.values()),
            f"skipped={sorted(skipped)}",
        )
        check(
            "真实数据模式没有调用保存/播种/清理等写路径",
            forbidden_hits == [],
            f"hits={forbidden_hits}",
        )
        check(
            "P5 的 setup（建并确认凭证）在真实模式没有被执行",
            create_pending_hits == [],
            f"hits={create_pending_hits}",
        )
        check(
            "P6/P7 的 after_run 写库回调一次都没执行",
            after_run_hits == [] and original_hooks,
            f"hits={after_run_hits} hooks={sorted(original_hooks)}",
        )
        check(
            "SKIP 的用例不计入 PASS 统计",
            real_report["summary"]["cases"] == len(executed)
            and real_report["summary"]["skipped"] == len(skipped)
            and real_report["summary"]["passed"] <= real_report["summary"]["cases"],
            f"summary={real_report['summary']}",
        )
        check(
            "只读用例仍能读到临时数据库中的历史数据",
            observed.get("rows") == seeded_rows and seeded_rows > 0,
            f"rows={observed.get('rows')} seeded={seeded_rows}",
        )
        check(
            "真实数据模式把缓存写入隔离到临时目录并在退出后删除",
            observed["cache_exists"]
            and observed["cache_dir"] != base_cache
            and agent_eval.PROJECT_DIR not in observed["cache_dir"].parents
            and not observed["cache_dir"].exists()
            and Path(vector_cache.CACHE_DIR) == base_cache,
            f"inside={observed['cache_dir']}",
        )
        check(
            "真实数据模式期间关闭检索埋点、退出后恢复",
            observed["metrics"] is not original_metrics
            and rag_service._log_retrieval_metrics is original_metrics,
            "",
        )
        check(
            "真实数据模式下 plan_draft 用例也走打桩执行器（未调用真实 LLM）",
            len(plan_draft_calls) == 3
            and all("制定" in message for message in plan_draft_calls),
            f"calls={len(plan_draft_calls)}",
        )

        # ---- 3b) 工具层只读：Schema 过滤 + Dispatcher 兜底拒绝 ----
        check(
            "真实数据模式运行期间 Agent 只能看到三个只读工具 Schema",
            observed.get("read_only_mode") is True
            and set(observed.get("tool_schema", [])) == set(agent_schema.READ_ONLY_TOOL_NAMES)
            and "save_plan" not in observed.get("tool_schema", []),
            f"schema={observed.get('tool_schema')}",
        )
        check(
            "退出只读窗口后完整工具 Schema 恢复（save_plan 可见）",
            agent_schema.is_read_only_mode() is False
            and "save_plan" in [schema["function"]["name"] for schema in agent_schema.get_tool_schemas()],
            "",
        )

        # ---- 3c) 用量记录：真实模式一条都不写，但能力本身仍在 ----
        check(
            "真实数据模式把用量记录器临时关闭，退出后恢复哨兵",
            observed.get("recorder_during_run") is None
            and llm_client.get_usage_recorder() is sentinel_recorder,
            f"during={observed.get('recorder_during_run')}",
        )
        check(
            "真实模式下成功与失败两条 LLM 调用路径都不写 model_usage",
            len(llm_calls) == 2
            and observed.get("usage_rows", (None, None))[0] == observed.get("usage_rows", (None, None))[1]
            and sentinel_events == [],
            f"calls={len(llm_calls)} rows={observed.get('usage_rows')} events={len(sentinel_events)}",
        )

        # ---- 3d) Dispatcher 在只读窗口内拒绝写工具（含名称带空格的绕过尝试） ----
        dispatcher_write_hits = []
        plan_service.save_plan_with_confirmation = forbidden("dispatcher_save_plan")
        try:
            with agent_schema.read_only_tools():
                blocked = agent_dispatcher.dispatch({"name": " save_plan ", "arguments": {}})
                read_tool = agent_dispatcher.dispatch(
                    {"name": "get_recent_feedback", "arguments": {"limit": 3}}
                )
            normal_save = agent_dispatcher.dispatch({"name": "save_plan", "arguments": {}})
        finally:
            plan_service.save_plan_with_confirmation = original_save_plan

        check(
            "Dispatcher 在只读窗口拒绝 save_plan 且不触碰保存函数",
            blocked["error_type"] == agent_dispatcher.ERROR_READ_ONLY_VIOLATION
            and blocked["tool"] == "save_plan"
            and dispatcher_write_hits == [],
            f"blocked={blocked['error_type']} hits={dispatcher_write_hits}",
        )
        check(
            "只读窗口内读工具不被误拦",
            read_tool["error_type"] != agent_dispatcher.ERROR_READ_ONLY_VIOLATION,
            f"read_tool={read_tool['error_type']}",
        )
        check(
            "退出只读窗口后 save_plan 不再被判只读违规",
            normal_save["error_type"] == agent_dispatcher.ERROR_INVALID_ARGUMENTS,
            f"normal={normal_save['error_type']}",
        )

        check(
            "临时数据库 SHA256 与大小不变",
            fingerprint(workspace_db) == db_before,
            f"before={db_before} now={fingerprint(workspace_db)}",
        )
        check(
            "默认夹具哨兵 _eval_fixture.db 原样保留",
            fingerprint(sentinel_db) == sentinel_db_before,
            f"before={sentinel_db_before} now={fingerprint(sentinel_db)}",
        )
        check(
            "默认夹具哨兵 _eval_cache 内容原样保留",
            fingerprint(sentinel_cache) == sentinel_cache_before,
            f"before={sentinel_cache_before} now={fingerprint(sentinel_cache)}",
        )
        check(
            "检索埋点文件没有被追加",
            fingerprint(agent_eval.RETRIEVAL_METRICS_FILE) == metrics_before,
            f"before={metrics_before}",
        )

        # ---- 4) 指纹以"实际生效路径"为准，且能发现内容变化 ----
        safety_paths = [check_item["path"] for check_item in real_report["data_safety"]["checks"]]
        check(
            "数据安全检查覆盖实际生效数据库路径",
            str(workspace_db) in safety_paths,
            f"paths={safety_paths}",
        )
        check(
            "运行结束后数据安全通过",
            real_report["data_safety"]["ok"] is True,
            f"failures={real_report['data_safety']['failures']}",
        )

        baseline = agent_eval.capture_data_safety_baseline()
        safety_entries = {entry["name"]: entry for entry in baseline["entries"]}
        check(
            "数据安全基线记录实际生效缓存路径（RAG_CACHE_DIR 覆盖后）",
            safety_entries.get("effective_cache", {}).get("path") == str(base_cache)
            and safety_entries.get("effective_cache", {}).get("before", {}).get("exists") is True,
            f"names={sorted(safety_entries)}",
        )
        check(
            "数据安全基线同时保留仓库 REAL_CACHE，两条路径都逐项比对",
            safety_entries.get("real_cache", {}).get("path") == str(repo_sentinel_cache)
            and safety_entries.get("real_cache", {}).get("before", {}).get("entries") == 1,
            f"real_cache={safety_entries.get('real_cache')}",
        )
        size_before_tamper = workspace_db.stat().st_size
        with open(workspace_db, "ab") as handle:
            handle.write(b"\x00tampered")
        tampered = agent_eval.verify_data_safety(baseline)
        check(
            "内容被改动后数据安全检查必须失败",
            tampered["ok"] is False
            and any("sha256" in failure or str(workspace_db) in failure for failure in tampered["failures"]),
            f"failures={tampered['failures']}",
        )
        with open(workspace_db, "r+b") as handle:
            handle.truncate(size_before_tamper)
        check(
            "移除篡改字节后指纹恢复（后续用例继续复用同一个临时库）",
            fingerprint(workspace_db) == db_before,
            f"now={fingerprint(workspace_db)}",
        )

        # ---- 4b) 退出码语义：数据安全失败必须让整个评测失败 ----
        def fake_report(ok, safety_ok):
            return {
                "started_at": "2026-11-19T00:00:00+00:00",
                "model": "stub",
                "mode": "real_data",
                "read_only_allowlist": [],
                "limits": {"max_tool_calls": 0, "max_loop_turns": 0, "max_run_seconds": 0},
                "summary": {
                    "cases": 0,
                    "skipped": 0,
                    "hard_failures": 0,
                    "deviations": 0,
                    "data_safety_failures": 0 if safety_ok else 1,
                    "passed": 0,
                },
                "data_safety": {
                    "ok": safety_ok,
                    "failures": [] if safety_ok else ["stub 安全检查失败"],
                    "checks": [],
                },
                "skipped": [],
                "cases": [],
                "ok": ok,
            }

        agent_eval.run_all = lambda **kwargs: fake_report(True, True)
        original_get_embedding = embedding_service.get_embedding
        embedding_service.get_embedding = lambda text: [0.0]
        try:
            check(
                "数据安全通过时 main() 退出码为 0",
                agent_eval.main(["--no-archive"]) == 0,
                "exit=0",
            )

            agent_eval.run_all = lambda **kwargs: fake_report(False, False)
            check(
                "数据安全失败时 main() 退出码为非零",
                agent_eval.main(["--no-archive"]) == 1,
                "exit=1",
            )

            # main() 的用量记录器安装/恢复：fixture 模式安装，真实模式关闭，
            # 两条路径退出后都必须把原记录器换回来（不能污染后续调用）。
            recorder_seen = {}

            def recording_run_all(**kwargs):
                recorder_seen["recorder"] = llm_client.get_usage_recorder()
                return fake_report(True, True)

            agent_eval.run_all = recording_run_all
            llm_client.set_usage_recorder(sentinel_recorder)
            try:
                check(
                    "fixture 模式 main() 在运行期安装 model_usage.record_usage",
                    agent_eval.main(["--no-archive"]) == 0
                    and recorder_seen.get("recorder") is model_usage.record_usage,
                    f"recorder={recorder_seen.get('recorder')}",
                )
                check(
                    "fixture 模式 main() 退出后恢复原用量记录器",
                    llm_client.get_usage_recorder() is sentinel_recorder,
                    "",
                )
                recorder_seen.clear()
                check(
                    "真实数据模式 main() 运行期不安装用量记录器",
                    agent_eval.main(["--real-data", "--no-archive"]) == 0
                    and recorder_seen.get("recorder") is None,
                    f"recorder={recorder_seen.get('recorder')}",
                )
                check(
                    "真实数据模式 main() 退出后同样恢复原用量记录器",
                    llm_client.get_usage_recorder() is sentinel_recorder,
                    "",
                )
            finally:
                llm_client.set_usage_recorder(sentinel_recorder)

            # 关闭只是真实模式的临时状态，记录能力本身必须完好：
            # 装回 model_usage 后，成功与失败两条调用路径各写一条。
            llm_client.get_settings = lambda: llm_client.Settings(
                provider=llm_client.PROVIDER_OPENAI,
                model="stub-model",
                chat_url="http://stub.invalid/v1/chat/completions",
            )
            llm_client._chat_openai = stub_chat_openai
            rows_before_capability = model_usage_rows()
            try:
                model_usage.install()
                llm_calls.clear()
                llm_client.chat([{"role": "user", "content": "stub"}])
                llm_client.chat([{"role": "user", "content": "stub fail"}])
                rows_after_capability = model_usage_rows()
            finally:
                llm_client.get_settings = original_llm_settings
                llm_client._chat_openai = original_chat_openai
                llm_client.set_usage_recorder(sentinel_recorder)
            check(
                "用量记录能力保留：装回记录器后成功与失败各写一条",
                rows_after_capability - rows_before_capability == 2
                and len(llm_calls) == 2,
                f"delta={rows_after_capability - rows_before_capability} calls={len(llm_calls)}",
            )
        finally:
            embedding_service.get_embedding = original_get_embedding

        # ---- 4c) 用例异常退出：仍做数据安全验证，异常与安全失败都保留 ----
        agent_eval.run_all = original_run_all
        exception_dir = root / "exception"
        exception_dir.mkdir(parents=True, exist_ok=True)
        exception_db = exception_dir / "feedback.db"
        previous_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = exception_db
        database.init_db()
        llm_client.set_usage_recorder(sentinel_recorder)
        try:
            def exploding_runner(message, verbose=False):
                raise RuntimeError("stub case crash")

            interrupted = None
            try:
                agent_eval.run_all(runner=exploding_runner, isolate=False)
            except agent_eval.EvalRunError as error:
                interrupted = error.report

            check(
                "用例抛异常时以 EvalRunError 中断并带上报告",
                interrupted is not None
                and interrupted["run_error"]["case_id"] == "A_history_only"
                and interrupted["run_error"]["type"] == "RuntimeError",
                f"run_error={None if interrupted is None else interrupted['run_error']['type']}",
            )
            check(
                "中断时仍然完成数据安全验证且通过",
                interrupted["data_safety"]["ok"] is True
                and interrupted["data_safety"]["checks"]
                and str(exception_db)
                in [item["path"] for item in interrupted["data_safety"]["checks"]],
                f"checks={len(interrupted['data_safety']['checks'])}",
            )
            check(
                "中断报告整体 ok=False 且计入 interrupted",
                interrupted["ok"] is False and interrupted["summary"]["interrupted"] == 1,
                f"summary={interrupted['summary']}",
            )
            check(
                "中断时用量记录器已恢复",
                llm_client.get_usage_recorder() is sentinel_recorder,
                "",
            )

            def tampering_runner(message, verbose=False):
                with open(database.DATABASE_PATH, "ab") as handle:
                    handle.write(b"\x00tampered-by-runner")
                raise RuntimeError("stub case crash after tamper")

            double_failure = None
            try:
                agent_eval.run_all(runner=tampering_runner, isolate=False)
            except agent_eval.EvalRunError as error:
                double_failure = error.report

            check(
                "异常 + 数据安全失败两个错误都保留在报告里",
                double_failure["run_error"]["type"] == "RuntimeError"
                and double_failure["data_safety"]["ok"] is False
                and any(
                    "sha256" in failure or str(exception_db) in failure
                    for failure in double_failure["data_safety"]["failures"]
                )
                and double_failure["ok"] is False,
                f"failures={double_failure['data_safety']['failures']}",
            )
        finally:
            database.DATABASE_PATH = previous_db_path
            llm_client.set_usage_recorder(original_usage_recorder)

        # ---- 5) 路径恢复回归测试继续保留且能通过 ----
        sibling = Path(agent_eval.PROJECT_DIR) / "test_agent_eval_db_restore.py"
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, str(sibling)],
            cwd=str(agent_eval.PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        check(
            "test_agent_eval_db_restore.py 仍然全部通过",
            completed.returncode == 0,
            f"returncode={completed.returncode} tail={(completed.stdout or '')[-200:]}",
        )

        print("PASS: 真实数据模式只读边界、SKIP 语义与数据安全指纹全部符合预期")
    finally:
        database.DATABASE_PATH = original_db
        vector_cache.CACHE_DIR = original_cache
        agent_eval.REAL_DB = original_real_db
        agent_eval.REAL_CACHE = original_real_cache
        agent_eval.FIXTURE_DB = original_fixture_db
        agent_eval.FIXTURE_CACHE = original_fixture_cache
        agent_eval.KNOWLEDGE_DIR = original_knowledge
        agent_eval.RETRIEVAL_METRICS_FILE = original_metrics_file
        agent_eval.clean_fixtures = original_clean
        agent_eval.run_case = original_run_case
        agent_eval.run_all = original_run_all
        agent_plan_draft.run_plan_draft = original_plan_draft_runner
        rag_service._log_retrieval_metrics = original_metrics
        llm_client.set_usage_recorder(original_usage_recorder)
        llm_client.get_settings = original_llm_settings
        llm_client._chat_openai = original_chat_openai
        plan_service.save_plan_with_confirmation = original_save_plan
        confirmation_service.create_pending_confirmation = original_create_pending
        database.insert_plan = original_insert_plan
        database.delete_all_feedback = original_delete_all
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(str(error))
        sys.exit(1)
