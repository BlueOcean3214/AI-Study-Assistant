"""毕业设计实验脚手架测试：直接运行 python test_graduation_experiments.py。

覆盖第一阶段交付（docs/GRADUATION-EXPERIMENT-DESIGN.md 的 S1–S7 与 A/B/C/D 四组）：
    T1  config.json 与七个样本结构完整，模拟数据标识为真
    T2  任务名经 memory_service.normalize_subject() 得到的 subject_key 与 Gold Standard 一致
    T3  建库隔离：写库后 DATABASE_PATH 落在临时目录，真实 feedback.db 字节与 mtime 不变
    T4  七个样本各自建独立库，记忆条数与 Gold Standard 一致
    T5  check_memory 对七个样本逐项核验全部通过（20 条记忆，逐条 8 个字段）
    T6  七个样本各自构建 A/B/C/D 四组 Prompt：槽位矩阵、去记忆等价、SHA256 可复现
    T7  四组 Prompt 里出现的记忆注入集合 == 真实 active 记忆检索结果
    T8  全程不调用对话模型与 Embedding（换成抛错桩后仍然全部通过）
    T9  核验能识别「Gold Standard 之外多一条记忆」
    T10 核验能识别「多出一条 pending 证据」
    T11 核验能识别「少一条 Gold Standard 记忆」与「重建失败」
    T12 seed_fixture --keep 对已存在的库只复用、不重复写入；对不存在的库照常构建
    T13 逐样本切换时 database.DATABASE_PATH 始终指向当前样本库；复用 S1 不动 S2.db
    T14 --keep 遇到结构不完整的库必须报错，不得静默写入
    T15 A/C 的历史反馈为空数组、统计摘要为空 dict；B/D 的摘要等于真实反馈计算结果
    T16 --keep 拒绝缺少 memory_apply_cursor 表的库
    T17 --keep 拒绝没有游标记录的库
    T18 --keep 拒绝游标落后于反馈高水位的库
    T19 --keep 拒绝被删掉一条 memory_evidence 的库
    T20 --keep 拒绝多出一条 memory_evidence 的库
    T21 --keep 拒绝记忆键错误但总数相同的库
    T22 --keep 拒绝 pending 缓冲不为零的库
    T23 修复后 S1–S7 的正常库仍能被 --keep 复用（不重写）

隔离：FEEDBACK_DB_PATH 在 import database 之前指向临时目录；脚手架自身还有
DATABASE_PATH 落在工作目录内的断言，本测试额外核对真实库指纹。
本测试不需要 Ollama 或任何 API Key；每个测试自己构建所需的库，可单独调用。
"""

import copy
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
GRADUATION_DIR = PROJECT_DIR / "experiments" / "graduation"
REAL_DB = PROJECT_DIR / "feedback.db"

# 必须在任何模块 import database 之前设置，避免导入期读到真实库
os.environ["FEEDBACK_DB_PATH"] = str(
    Path(tempfile.gettempdir()) / "graduation_experiments" / "_test_placeholder.db"
)

if str(GRADUATION_DIR) not in sys.path:
    sys.path.insert(0, str(GRADUATION_DIR))

import build_prompts  # noqa: E402
import check_memory  # noqa: E402
import seed_fixture  # noqa: E402

REQUIRED_SAMPLE_KEYS = (
    "sample_id",
    "scenario",
    "feedbacks",
    "current_user_request",
    "gold_standard",
    "simulation",
)


def _real_db_fingerprint():
    if not REAL_DB.exists():
        return None
    raw = REAL_DB.read_bytes()
    stat = REAL_DB.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _config():
    return seed_fixture.load_config()


def _db_counts(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "feedbacks",
                "plans",
                "learning_memories",
                "memory_evidence",
                "memory_pending_evidence",
            )
        }
    finally:
        conn.close()


def _memory_block(user_content):
    """取出 Prompt 里的长期记忆区块原文（用于「去掉记忆后应逐字相同」的核验）。"""

    start = user_content.index(build_prompts.MEMORY_HEADER)
    end = user_content.index(build_prompts.MEMORY_USAGE_NOTE, start)

    return user_content[start:end].rstrip("\n")


def _memory_entries(user_content):
    """从 Prompt 文本里取回长期记忆 JSON（即模型实际看到的那一段）。"""

    start = user_content.index(build_prompts.MEMORY_HEADER)
    body = user_content[start:]
    marker = "）：\n"
    json_start = body.index(marker) + len(marker)
    json_end = body.index("\n\n" + build_prompts.MEMORY_USAGE_NOTE, json_start)

    return json.loads(body[json_start:json_end])


# ---------------------------------------------------------------------------
# T1 样本结构
# ---------------------------------------------------------------------------


def test_config_and_samples():
    config = _config()
    assert config["groups"] == ["A", "B", "B0", "C", "D"], config["groups"]
    assert config["memory_limit"] == 5, config["memory_limit"]
    assert config["knowledge_results"] == [], config["knowledge_results"]
    assert config["knowledge_search_failed"] is False

    ids = seed_fixture.sample_ids(config)
    assert ids == config["samples"], ids
    assert len(ids) == 7, ids

    for sample_id in ids:
        sample = seed_fixture.load_sample(sample_id, config)
        assert sample["sample_id"] == sample_id
        for key in REQUIRED_SAMPLE_KEYS:
            assert key in sample, f"{sample_id} 缺少 {key}"
        assert sample["simulation"]["is_simulated"] is True, (
            f"{sample_id} 未标记为模拟数据"
        )
        gold = sample["gold_standard"]
        assert gold["simulation"] is True, f"{sample_id} gold_standard 未标记为模拟数据"
        assert len(sample["feedbacks"]) >= 1, sample_id
        assert len(gold["memories"]) >= 1, sample_id
        assert all(f.get("task") for f in sample["feedbacks"]), sample_id


# ---------------------------------------------------------------------------
# T2 任务名口径
# ---------------------------------------------------------------------------


def test_subject_keys_follow_normalize_subject():
    config = _config()
    memory_service = seed_fixture.project_modules(config)[1]

    for sample_id in seed_fixture.sample_ids(config):
        sample = seed_fixture.load_sample(sample_id, config)
        tasks = [f["task"] for f in sample["feedbacks"]]
        if sample.get("plan"):
            tasks.append(sample["plan"]["task"])

        normalized = {}
        for task in tasks:
            key, label = memory_service.normalize_subject(task)
            assert key and label, f"{sample_id} 归一化结果为空：{task}"
            normalized[key] = label

        for gold in sample["gold_standard"]["memories"]:
            subject_key = gold["subject_key"]
            if gold["memory_type"] == "time_pattern":
                assert subject_key in ("easy", "medium", "hard"), subject_key
                continue
            assert subject_key in normalized, (
                f"{sample_id} 的 {gold['memory_type']} subject_key={subject_key} "
                f"与 normalize_subject 结果不一致：{sorted(normalized)}"
            )


# ---------------------------------------------------------------------------
# T3 隔离
# ---------------------------------------------------------------------------


def test_seed_keeps_real_database_untouched():
    config = _config()
    work_dir = seed_fixture.workspace_dir(config).resolve()
    before = _real_db_fingerprint()

    seeded = seed_fixture.seed_sample("S7", config)
    db_path = Path(seeded["db_path"]).resolve()

    assert db_path.is_file(), db_path
    assert work_dir in db_path.parents, f"{db_path} 不在 {work_dir} 下"
    assert seeded["rebuild"]["ok"] is True, seeded["rebuild"]

    database = seed_fixture.project_modules(config)[0]
    assert Path(database.DATABASE_PATH).resolve().parent == db_path.parent, (
        f"DATABASE_PATH 未被隔离：{database.DATABASE_PATH}"
    )

    assert _db_counts(db_path)["feedbacks"] == len(
        seed_fixture.load_sample("S7", config)["feedbacks"]
    )

    assert _real_db_fingerprint() == before, "真实 feedback.db 被改动了"


# ---------------------------------------------------------------------------
# T4 七个独立库
# ---------------------------------------------------------------------------


def test_each_sample_builds_its_own_database():
    config = _config()
    paths = {}

    for sample_id in seed_fixture.sample_ids(config):
        sample = seed_fixture.load_sample(sample_id, config)
        seeded = seed_fixture.seed_sample(sample_id, config)

        assert seeded["rebuild"]["ok"] is True, (sample_id, seeded["rebuild"])

        memories = seeded["memories"]["memories"]
        assert len(memories) == len(sample["gold_standard"]["memories"]), (
            sample_id,
            [m["memory_type"] for m in memories],
        )
        assert seeded["memories"]["pendings"] == 0, sample_id
        paths[sample_id] = Path(seeded["db_path"]).resolve()

    assert len(set(paths.values())) == 7, paths


# ---------------------------------------------------------------------------
# T5 记忆层核验
# ---------------------------------------------------------------------------


def test_memory_matches_gold_standard():
    config = _config()
    total = 0
    failures = []
    covered = []

    for sample_id in seed_fixture.sample_ids(config):
        seed_fixture.seed_sample(sample_id, config)
        result = check_memory.check_sample(sample_id, config, reuse=True)
        total += len(result["rows"])

        if result["failure_count"]:
            failures.append((sample_id, result["failure_count"], result["reasons"]))

        # 每条 Gold Standard 记忆都要逐条比到 8 个字段，而不是只数条数
        for row in result["rows"]:
            assert len(row["checks"]) == 8, (sample_id, row["checks"])
            assert all(check["ok"] for check in row["checks"]), (sample_id, row["checks"])
            covered.append(row["key"])

        sample = seed_fixture.load_sample(sample_id, config)
        expected = {
            (item["memory_type"], item["subject_key"])
            for item in sample["gold_standard"]["memories"]
        }
        assert set(covered[-len(expected):]) == expected, sample_id

    assert not failures, failures
    assert total == 20, total
    assert len(set(covered)) == 20, covered


# ---------------------------------------------------------------------------
# T6 七个样本的 A/B/C/D Prompt
# ---------------------------------------------------------------------------


def test_prompts_build_for_all_samples():
    config = _config()
    api = build_prompts.project_api(config)
    seen = {}

    for sample_id in seed_fixture.sample_ids(config):
        seed_fixture.seed_sample(sample_id, config)
        build = build_prompts.build_sample_prompts(sample_id, config, reuse=True)

        assert sorted(build["prompts"]) == ["A", "B", "B0", "C", "D"], sample_id
        assert not [check for check in build["checks"] if not check[1]], (
            sample_id,
            [check for check in build["checks"] if not check[1]],
        )

        contents = build["contents"]
        inputs = build["inputs"]

        # 槽位矩阵：A 全无；B 有反馈+短期上下文；C 有记忆；D 三者都有
        assert inputs["memory_count"] >= 1, (sample_id, inputs)
        assert inputs["feedback_count"] >= 1, (sample_id, inputs)

        # A/C 用空反馈（真实系统在无反馈时的形状），B/D 带真实反馈 JSON
        for group in ("A", "C"):
            assert "历史反馈：\n[]" in contents[group], (sample_id, group)
        for group in ("B", "D"):
            assert "历史反馈：\n[\n" in contents[group], (sample_id, group)

        assert build_prompts.SHORT_TERM_HEADER not in contents["A"], sample_id
        assert build_prompts.MEMORY_HEADER not in contents["A"], sample_id
        assert build_prompts.SHORT_TERM_HEADER in contents["B"], sample_id
        assert build_prompts.MEMORY_HEADER not in contents["B"], sample_id
        assert build_prompts.SHORT_TERM_HEADER not in contents["C"], sample_id
        assert build_prompts.MEMORY_HEADER in contents["C"], sample_id
        assert build_prompts.SHORT_TERM_HEADER in contents["D"], sample_id
        assert build_prompts.MEMORY_HEADER in contents["D"], sample_id

        # B0：只有原始反馈行；无统计摘要、无难度硬规则、无短期上下文、无记忆
        assert "历史反馈：\n[\n" in contents["B0"], sample_id
        assert '"total_count"' not in contents["B0"], sample_id
        assert "最近一次反馈是 partial 且原因是任务难度太高" not in contents["B0"], sample_id
        assert build_prompts.SHORT_TERM_HEADER not in contents["B0"], sample_id
        assert build_prompts.MEMORY_HEADER not in contents["B0"], sample_id

        # C 去掉记忆 == A；D 去掉记忆 == B（逐字）
        block = _memory_block(contents["C"])
        assert build_prompts.strip_memory(contents["C"], block) == contents["A"], sample_id
        assert build_prompts.strip_memory(contents["D"], block) == contents["B"], sample_id

        # 每组 SHA256 必须与内容一致，且第二次构建完全相同
        again = build_prompts.build_sample_prompts(sample_id, config, reuse=True)

        for group in ("A", "B", "B0", "C", "D"):
            prompt = build["prompts"][group]
            recomputed = build_prompts.prompt_sha256(prompt["system"], prompt["user"])
            assert prompt["chars"] == len(prompt["user"]), (sample_id, group)
            assert prompt["prompt_sha256"] == recomputed, (sample_id, group)
            assert again["prompts"][group]["prompt_sha256"] == recomputed, (
                sample_id,
                group,
            )
            assert "用户请求：" in prompt["user"], (sample_id, group)

        seen[sample_id] = build["prompts"]["A"]["prompt_sha256"]

    assert len(set(seen.values())) == 7, seen


# ---------------------------------------------------------------------------
# T7 注入集合 == 真实 active 检索结果
# ---------------------------------------------------------------------------


def test_memory_injection_matches_active_retrieval():
    config = _config()
    api = build_prompts.project_api(config)
    moment = seed_fixture.ref_datetime(config)

    for sample_id in seed_fixture.sample_ids(config):
        seeded = seed_fixture.seed_sample(sample_id, config)
        db_path = Path(seeded["db_path"])

        with build_prompts.fixed_clock(api["database"], moment):
            retrieved = api["agent_tools"].get_learning_memory(
                limit=config["memory_limit"]
            )

        assert retrieved["ok"] is True, (sample_id, retrieved)
        expected = {(item["type"], item["subject"]) for item in retrieved["memories"]}

        build = build_prompts.build_sample_prompts(sample_id, config, reuse=True)
        entries = _memory_entries(build["contents"]["C"])
        actual = {(item["type"], item["subject"]) for item in entries}

        assert actual == expected, (sample_id, actual, expected)
        assert len(entries) == build["inputs"]["memory_count"], sample_id
        assert build["inputs"]["memory_count"] == len(expected), sample_id

        # 注入的只能是 active 记忆，不出现 weakened/resolved 文案
        for item in retrieved["memories"]:
            assert item["type"] in ("difficulty", "task_performance", "task_topic", "time_pattern"), item

        injected_from_db = build_prompts.injected_labels(db_path, config["memory_limit"])
        assert actual == injected_from_db, (sample_id, actual, injected_from_db)


# ---------------------------------------------------------------------------
# T8 无模型调用
# ---------------------------------------------------------------------------


def test_no_model_or_embedding_call():
    config = _config()
    calls = []

    def _boom(name):
        def _stub(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"脚手架不应该调用 {name}")

        return _stub

    import ai_service
    import embedding_service

    saved = {
        "call_ollama": ai_service.call_ollama,
        "get_embedding": embedding_service.get_embedding,
        "get_embeddings": embedding_service.get_embeddings,
    }

    try:
        ai_service.call_ollama = _boom("ai_service.call_ollama")
        embedding_service.get_embedding = _boom("embedding_service.get_embedding")
        embedding_service.get_embeddings = _boom("embedding_service.get_embeddings")

        for sample_id in seed_fixture.sample_ids(config):
            seed_fixture.seed_sample(sample_id, config)
            build = build_prompts.build_sample_prompts(sample_id, config, reuse=True)
            assert not [check for check in build["checks"] if not check[1]], sample_id
            assert build["inputs"]["knowledge_count"] == 0, sample_id
            assert build["inputs"]["knowledge_search_failed"] is False, sample_id

        assert not calls, calls
    finally:
        ai_service.call_ollama = saved["call_ollama"]
        embedding_service.get_embedding = saved["get_embedding"]
        embedding_service.get_embeddings = saved["get_embeddings"]


# ---------------------------------------------------------------------------
# T9 / T10 / T11 核验必须会失败（变异副本，不改数据库）
# ---------------------------------------------------------------------------


def _verify_variant(sample_id, mutate):
    config = _config()
    seeded = seed_fixture.seed_sample(sample_id, config)
    sample = seed_fixture.load_sample(sample_id, config)
    dump = copy.deepcopy(seeded["memories"])
    rebuild = {"ok": True}

    mutate(dump, sample)

    return check_memory.verify_sample(
        sample, dump, Path(seeded["db_path"]), config, rebuild
    )


def test_check_detects_unexpected_memory():
    def mutate(dump, sample):
        dump["memories"].append(
            {
                "memory_type": "task_topic",
                "subject_key": "gold-standard-extra",
                "subject_label": "Gold Standard 之外的任务",
                "content": "人为插入的多余记忆",
                "status": "active",
                "strength": 0.5,
                "value": {},
                "evidence_relations": {"supports": 1, "contradicts": 0},
            }
        )

    result = _verify_variant("S7", mutate)

    assert result["failure_count"] > 0, result
    assert result["unexpected"], result
    assert any("多出 Gold Standard 之外的记忆" in reason for reason in result["reasons"]), (
        result["reasons"]
    )


def test_check_detects_pending_evidence():
    def mutate(dump, sample):
        dump["pendings"] = 1

    result = _verify_variant("S7", mutate)

    assert result["failure_count"] > 0, result
    assert any("待定证据" in reason for reason in result["reasons"]), result["reasons"]


def test_check_detects_missing_memory_and_failed_rebuild():
    def drop_one(dump, sample):
        dump["memories"] = dump["memories"][:-1]

    result = _verify_variant("S5", drop_one)
    assert result["failure_count"] > 0, result
    assert any("缺少记忆" in reason for reason in result["reasons"]), result["reasons"]

    config = _config()
    seeded = seed_fixture.seed_sample("S7", config)
    sample = seed_fixture.load_sample("S7", config)
    failed = check_memory.verify_sample(
        sample,
        copy.deepcopy(seeded["memories"]),
        Path(seeded["db_path"]),
        config,
        {"ok": False},
    )
    assert failed["failure_count"] > 0, failed
    assert any("重建未成功" in reason for reason in failed["reasons"]), failed["reasons"]


# ---------------------------------------------------------------------------
# T12 / T13 / T14 seed_fixture --keep 语义
# ---------------------------------------------------------------------------


def test_keep_reuses_existing_database_without_writing():
    config = _config()

    first = seed_fixture.seed_sample("S1", config)
    before = _db_counts(Path(first["db_path"]))

    second = seed_fixture.seed_sample("S1", config, keep=True)
    third = seed_fixture.seed_sample("S1", config, keep=True)

    assert second["reused"] is True and third["reused"] is True, (second, third)
    assert Path(second["db_path"]) == Path(first["db_path"])

    after = _db_counts(Path(first["db_path"]))
    assert before == after, (before, after)

    # 库不存在时 --keep 应照常创建
    db_path = Path(first["db_path"])
    gc.collect()  # 产品里 `with sqlite3.connect(...)` 不关闭连接，Windows 上先回收句柄
    db_path.unlink()
    rebuilt = seed_fixture.seed_sample("S1", config, keep=True)

    assert rebuilt["reused"] is False, rebuilt
    assert rebuilt["rebuild"]["ok"] is True, rebuilt
    assert _db_counts(db_path) == before, (before, _db_counts(db_path))


def test_database_path_switch_between_samples():
    config = _config()
    database = seed_fixture.project_modules(config)[0]

    s1 = seed_fixture.seed_sample("S1", config)
    s2 = seed_fixture.seed_sample("S2", config)

    assert Path(database.DATABASE_PATH) == Path(s2["db_path"]), database.DATABASE_PATH

    s2_before = _db_counts(Path(s2["db_path"]))
    s2_bytes_before = Path(s2["db_path"]).read_bytes()

    reused = seed_fixture.seed_sample("S1", config, keep=True)

    assert reused["reused"] is True
    assert Path(database.DATABASE_PATH) == Path(s1["db_path"]), database.DATABASE_PATH
    assert _db_counts(Path(s2["db_path"])) == s2_before, "复用 S1 时改动了 S2.db"
    assert Path(s2["db_path"]).read_bytes() == s2_bytes_before, "复用 S1 时改动了 S2.db 字节"


def test_keep_rejects_incomplete_database():
    config = _config()
    work_dir = seed_fixture.workspace_dir(config)

    broken = work_dir / "_broken_for_test.db"
    if broken.exists():
        broken.unlink()

    conn = sqlite3.connect(str(broken))
    try:
        conn.execute("CREATE TABLE feedbacks (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    try:
        seed_fixture.inspect_sample_db(broken, seed_fixture.load_sample("S1", config))
    except RuntimeError as exc:
        assert "结构不完整" in str(exc), exc
    else:
        raise AssertionError("不完整的库必须报错，不得静默通过")
    finally:
        shutil.rmtree(broken, ignore_errors=True) if broken.is_dir() else broken.unlink()


# ---------------------------------------------------------------------------
# T15 A/C 的空反馈摘要必须是 {}，B/D 的摘要必须来自真实反馈
# ---------------------------------------------------------------------------


def _prompt_feedback_and_summary(user_content):
    """从渲染好的 Prompt 里取出「历史反馈」与「统计结果」两个 JSON。"""

    feedback_marker = "历史反馈：\n"
    summary_marker = "统计结果：\n"
    stop_marker = "\n\n只能根据数据判断"

    start = user_content.index(feedback_marker) + len(feedback_marker)
    summary_at = user_content.index(summary_marker, start)
    end = user_content.index(stop_marker, summary_at)

    feedback = json.loads(user_content[start:summary_at])
    summary = json.loads(user_content[summary_at + len(summary_marker): end])

    return feedback, summary


def test_empty_and_real_feedback_summaries():
    config = _config()
    days = int(config["recent_feedback_days"])
    api = build_prompts.project_api(config)
    calculate_summary = api["feedback_service"].calculate_summary

    expected_empty = {"days": days, "count": 0, "summary": {}, "feedback": []}

    for sample_id in seed_fixture.sample_ids(config):
        build = build_prompts.build_sample_prompts(sample_id, config)

        # 条件输入本身就是空 dict，而不是渲染时才把统计藏起来
        assert build["inputs"]["empty_feedback"] == expected_empty, (
            sample_id,
            build["inputs"]["empty_feedback"],
        )

        contents = build["contents"]

        for group in ("A", "C"):
            feedback, summary = _prompt_feedback_and_summary(contents[group])

            assert feedback == [], (sample_id, group, feedback)
            assert summary == {}, (sample_id, group, summary)

        # C 与 A 的反馈及摘要完全一致
        assert _prompt_feedback_and_summary(contents["C"]) == _prompt_feedback_and_summary(
            contents["A"]
        ), sample_id

        for group in ("B", "D"):
            feedback, summary = _prompt_feedback_and_summary(contents[group])

            assert feedback, (sample_id, group, "B/D 必须带真实反馈")

            # 条件输入里的摘要就是真实反馈的计算结果，且与 Prompt 文本一致
            assert build["inputs"]["feedback_summary"] == summary, (sample_id, group)

            expected = calculate_summary(feedback)

            for field, value in expected.items():
                assert summary.get(field) == value, (sample_id, group, field, value)

            # 产品在「有反馈」分支才补的两个字段必须来自真实第一条反馈
            assert summary["latest_status"] == feedback[0]["status"], (sample_id, group)
            assert summary["latest_reason"] == feedback[0].get("reason"), (
                sample_id,
                group,
            )

            extra = sorted(
                set(summary) - set(expected) - {"latest_status", "latest_reason"}
            )
            assert not extra, (sample_id, group, extra)

        # 去掉记忆后 C ≡ A、D ≡ B（逐字）
        block = _memory_block(contents["C"])

        assert build_prompts.strip_memory(contents["C"], block) == contents["A"], sample_id
        assert build_prompts.strip_memory(contents["D"], block) == contents["B"], sample_id


# ---------------------------------------------------------------------------
# T16–T22 --keep 必须拒绝七种残缺库
# ---------------------------------------------------------------------------


def _sql(db_path, *statements):
    conn = sqlite3.connect(str(db_path))
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _expect_keep_failure(mutate, expected_substring, sample_id="S1"):
    """把样本库改坏 → --keep 必须报错 → 还原字节 → 必须又能正常复用。"""

    config = _config()
    seeded = seed_fixture.seed_sample(sample_id, config)
    db_path = Path(seeded["db_path"])
    healthy = db_path.read_bytes()

    gc.collect()  # 产品里的连接不显式关闭，Windows 上先回收句柄再改写

    try:
        mutate(db_path)
        gc.collect()

        try:
            seed_fixture.seed_sample(sample_id, config, keep=True)
        except RuntimeError as exc:
            assert expected_substring in str(exc), (expected_substring, str(exc))
        else:
            raise AssertionError(
                f"残缺库必须被 --keep 拒绝（期望包含「{expected_substring}」）"
            )
    finally:
        gc.collect()
        db_path.write_bytes(healthy)
        gc.collect()

    # 还原后必须仍可复用：证明上面的失败确实来自被改坏的库，而不是路径问题
    restored = seed_fixture.seed_sample(sample_id, config, keep=True)

    assert restored["reused"] is True and restored["rebuild"] is None, restored
    assert restored["counts"]["memory_apply_cursor"] == 1, restored["counts"]


def test_keep_rejects_missing_cursor_table():
    def drop_cursor(db_path):
        _sql(db_path, "DROP TABLE memory_apply_cursor")

    _expect_keep_failure(drop_cursor, "缺少表")


def test_keep_rejects_missing_cursor_row():
    def drop_row(db_path):
        _sql(db_path, "DELETE FROM memory_apply_cursor")

    _expect_keep_failure(drop_row, "游标表记录数")


def test_keep_rejects_cursor_behind_high_watermark():
    def stale_cursor(db_path):
        _sql(db_path, "UPDATE memory_apply_cursor SET last_applied_feedback_id = 0")

    _expect_keep_failure(stale_cursor, "反馈高水位")


def test_keep_rejects_deleted_evidence():
    def drop_evidence(db_path):
        _sql(
            db_path,
            "DELETE FROM memory_evidence WHERE id = "
            "(SELECT MIN(id) FROM memory_evidence)",
        )

    _expect_keep_failure(drop_evidence, "supports/contradicts")


def test_keep_rejects_extra_evidence():
    # S5 的证据矩阵有空闲 (memory_id, feedback_id) 组合（唯一索引不许重复），
    # 其余样本矩阵已满，只能在这里插入「引用全部合法但多出来」的一条证据。
    def add_evidence(db_path):
        _sql(
            db_path,
            "INSERT INTO memory_evidence "
            "(memory_id, feedback_id, plan_id, episode, relation, weight, created_at) "
            "SELECT m.id, f.id, NULL, 1, 'supports', 1.0, '2026-11-12T21:00:00' "
            "FROM learning_memories m CROSS JOIN feedbacks f "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM memory_evidence e "
            "WHERE e.memory_id = m.id AND e.feedback_id = f.id) "
            "LIMIT 1",
        )

    _expect_keep_failure(add_evidence, "supports/contradicts", sample_id="S5")


def test_keep_rejects_wrong_memory_key_with_same_count():
    def rename_key(db_path):
        _sql(
            db_path,
            "UPDATE learning_memories SET subject_key = '__wrong_subject_key__' "
            "WHERE id = (SELECT MIN(id) FROM learning_memories)",
        )

    _expect_keep_failure(rename_key, "缺少 Gold Standard 记忆")


def test_keep_rejects_pending_evidence():
    def add_pending(db_path):
        _sql(
            db_path,
            "INSERT INTO memory_pending_evidence "
            "(memory_id, feedback_id, plan_id, episode, created_at, recorded_at) "
            "SELECT MIN(id), 9999, NULL, 1, '2026-11-12T21:00:00', "
            "'2026-11-12T21:00:00' FROM learning_memories",
        )

    _expect_keep_failure(add_pending, "pending 证据")


# ---------------------------------------------------------------------------
# T23 修复后 S1–S7 的正常库仍能被 --keep 复用
# ---------------------------------------------------------------------------


def test_keep_reuses_all_seven_healthy_databases():
    config = _config()

    for sample_id in seed_fixture.sample_ids(config):
        built = seed_fixture.seed_sample(sample_id, config)
        db_path = Path(built["db_path"])
        before = _db_counts(db_path)

        again = seed_fixture.seed_sample(sample_id, config, keep=True)

        assert again["reused"] is True, (sample_id, again)
        assert again["rebuild"] is None, (sample_id, again)
        assert again["counts"]["memory_apply_cursor"] == 1, (sample_id, again["counts"])
        assert _db_counts(db_path) == before, (sample_id, before, _db_counts(db_path))


# ---------------------------------------------------------------------------
# T24 证据引用完整性：孤立引用与非法 relation 都必须被拒绝
# ---------------------------------------------------------------------------


def test_keep_rejects_orphan_memory_id_evidence():
    def add_orphan_memory(db_path):
        _sql(
            db_path,
            "INSERT INTO memory_evidence "
            "(memory_id, feedback_id, plan_id, episode, relation, weight, created_at) "
            "SELECT 999999, MIN(id), NULL, 1, 'supports', 1.0, '2026-11-12T21:00:00' "
            "FROM feedbacks",
        )

    _expect_keep_failure(add_orphan_memory, "learning_memories 中不存在")


def test_keep_rejects_orphan_feedback_id_evidence():
    def add_orphan_feedback(db_path):
        _sql(
            db_path,
            "INSERT INTO memory_evidence "
            "(memory_id, feedback_id, plan_id, episode, relation, weight, created_at) "
            "SELECT MIN(id), 999999, NULL, 1, 'supports', 1.0, '2026-11-12T21:00:00' "
            "FROM learning_memories",
        )

    _expect_keep_failure(add_orphan_feedback, "feedbacks 中不存在")


def test_keep_rejects_orphan_plan_id_on_existing_evidence():
    def point_to_missing_plan(db_path):
        _sql(
            db_path,
            "UPDATE memory_evidence SET plan_id = 999999 "
            "WHERE id = (SELECT MIN(id) FROM memory_evidence)",
        )

    _expect_keep_failure(point_to_missing_plan, "plans 中不存在")


def test_keep_rejects_neutral_relation_evidence():
    def add_neutral(db_path):
        _sql(
            db_path,
            "INSERT INTO memory_evidence "
            "(memory_id, feedback_id, plan_id, episode, relation, weight, created_at) "
            "SELECT m.id, f.id, NULL, 1, 'neutral', 1.0, '2026-11-12T21:00:00' "
            "FROM learning_memories m CROSS JOIN feedbacks f "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM memory_evidence e "
            "WHERE e.memory_id = m.id AND e.feedback_id = f.id) "
            "LIMIT 1",
        )

    _expect_keep_failure(add_neutral, "非法证据关系 neutral×1", sample_id="S5")


def test_keep_rejects_unknown_relation_evidence():
    def add_unsupported(db_path):
        _sql(
            db_path,
            "INSERT INTO memory_evidence "
            "(memory_id, feedback_id, plan_id, episode, relation, weight, created_at) "
            "SELECT m.id, f.id, NULL, 1, 'unsupported', 1.0, '2026-11-12T21:00:00' "
            "FROM learning_memories m CROSS JOIN feedbacks f "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM memory_evidence e "
            "WHERE e.memory_id = m.id AND e.feedback_id = f.id) "
            "LIMIT 1",
        )

    _expect_keep_failure(add_unsupported, "非法证据关系 unsupported×1", sample_id="S5")


# ---------------------------------------------------------------------------
# T25 所有 --reuse 入口都必须先做同一套完整性检查
# ---------------------------------------------------------------------------


def _check_reuse_entry(config):
    return check_memory.check_sample("S1", config, reuse=True)


def _build_reuse_entry(config):
    return build_prompts.build_sample_prompts("S1", config, reuse=True)


def _expect_reuse_failure(entry, mutate, expected_substring):
    """把 S1 的库改坏 → 复用入口必须报错 → 还原字节 → 入口必须又能正常运行。

    两个入口（check_memory --reuse 与 build_prompts --reuse）都走同一个
    inspect_sample_db()，这里验证它们确实在读取数据之前就 fail-closed。
    """

    config = _config()
    seeded = seed_fixture.seed_sample("S1", config)
    db_path = Path(seeded["db_path"])
    healthy = db_path.read_bytes()

    gc.collect()

    try:
        mutate(db_path)
        gc.collect()

        try:
            entry(config)
        except RuntimeError as exc:
            assert expected_substring in str(exc), (expected_substring, str(exc))
        else:
            raise AssertionError(
                f"残缺库必须被复用入口拒绝（期望包含「{expected_substring}」）"
            )
    finally:
        gc.collect()
        db_path.write_bytes(healthy)
        gc.collect()

    # 还原后入口必须正常运行：证明失败来自库缺陷，不是路径或状态问题
    return entry(config)


def _drop_cursor_table(db_path):
    _sql(db_path, "DROP TABLE memory_apply_cursor")


def _drop_first_evidence(db_path):
    _sql(
        db_path,
        "DELETE FROM memory_evidence WHERE id = "
        "(SELECT MIN(id) FROM memory_evidence)",
    )


def test_check_reuse_rejects_missing_cursor_table():
    result = _expect_reuse_failure(_check_reuse_entry, _drop_cursor_table, "缺少表")

    assert result["failure_count"] == 0, result


def test_check_reuse_rejects_deleted_evidence():
    result = _expect_reuse_failure(
        _check_reuse_entry, _drop_first_evidence, "supports/contradicts"
    )

    assert result["failure_count"] == 0, result


def test_build_reuse_rejects_missing_cursor_table():
    result = _expect_reuse_failure(_build_reuse_entry, _drop_cursor_table, "缺少表")

    assert [c for c in result["checks"] if not c[1]] == [], result["checks"]


def test_build_reuse_rejects_deleted_evidence():
    result = _expect_reuse_failure(
        _build_reuse_entry, _drop_first_evidence, "supports/contradicts"
    )

    assert [c for c in result["checks"] if not c[1]] == [], result["checks"]


def test_reuse_entries_accept_all_healthy_databases():
    config = _config()

    for sample_id in seed_fixture.sample_ids(config):
        seed_fixture.seed_sample(sample_id, config)

        checked = check_memory.check_sample(sample_id, config, reuse=True)
        assert checked["failure_count"] == 0, (sample_id, checked["reasons"])

        built = build_prompts.build_sample_prompts(sample_id, config, reuse=True)
        failed = [c for c in built["checks"] if not c[1]]
        assert failed == [], (sample_id, failed)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _run_all():
    before = _real_db_fingerprint()

    tests = [
        test_config_and_samples,
        test_subject_keys_follow_normalize_subject,
        test_seed_keeps_real_database_untouched,
        test_each_sample_builds_its_own_database,
        test_memory_matches_gold_standard,
        test_prompts_build_for_all_samples,
        test_memory_injection_matches_active_retrieval,
        test_no_model_or_embedding_call,
        test_check_detects_unexpected_memory,
        test_check_detects_pending_evidence,
        test_check_detects_missing_memory_and_failed_rebuild,
        test_keep_reuses_existing_database_without_writing,
        test_database_path_switch_between_samples,
        test_keep_rejects_incomplete_database,
        test_empty_and_real_feedback_summaries,
        test_keep_rejects_missing_cursor_table,
        test_keep_rejects_missing_cursor_row,
        test_keep_rejects_cursor_behind_high_watermark,
        test_keep_rejects_deleted_evidence,
        test_keep_rejects_extra_evidence,
        test_keep_rejects_wrong_memory_key_with_same_count,
        test_keep_rejects_pending_evidence,
        test_keep_reuses_all_seven_healthy_databases,
        test_keep_rejects_orphan_memory_id_evidence,
        test_keep_rejects_orphan_feedback_id_evidence,
        test_keep_rejects_orphan_plan_id_on_existing_evidence,
        test_keep_rejects_neutral_relation_evidence,
        test_keep_rejects_unknown_relation_evidence,
        test_check_reuse_rejects_missing_cursor_table,
        test_check_reuse_rejects_deleted_evidence,
        test_build_reuse_rejects_missing_cursor_table,
        test_build_reuse_rejects_deleted_evidence,
        test_reuse_entries_accept_all_healthy_databases,
    ]

    for test in tests:
        print(f"\n=== {test.__name__} ===")
        test()

    assert _real_db_fingerprint() == before, "真实 feedback.db 在测试过程中被改动"

    print(f"\nALL {len(tests)} TESTS COMPLETED")


if __name__ == "__main__":
    _run_all()
