"""毕业设计实验脚手架测试：直接运行 python test_graduation_experiments.py。

覆盖第一阶段交付（docs/GRADUATION-EXPERIMENT-DESIGN.md 的 S1–S7 与 A/B/C/D 四组）：
    T1 config.json 与七个样本结构完整，模拟数据标识为真
    T2 任务名经 memory_service.normalize_subject() 得到的 subject_key 与 Gold Standard 一致
    T3 建库隔离：写库后 DATABASE_PATH 落在临时目录，真实 feedback.db 字节与 mtime 不变
    T4 七个样本各自建独立库，记忆条数与 Gold Standard 一致
    T5 check_memory 对七个样本逐项核验全部通过（20 条记忆）
    T6 四组 Prompt 构建成功，槽位核验全过，且 SHA256 可复现
    T7 全程不调用对话模型与 Embedding（换成抛错桩后仍然全部通过）

隔离：FEEDBACK_DB_PATH 在 import database 之前指向临时目录；脚手架自身还有
DATABASE_PATH 落在工作目录内的断言，本测试额外核对真实库指纹。
本测试不需要 Ollama 或任何 API Key。
"""

import hashlib
import json
import os
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


# ---------------------------------------------------------------------------
# T1 样本结构
# ---------------------------------------------------------------------------


def test_config_and_samples():
    config = _config()
    assert config["groups"] == ["A", "B", "C", "D"], config["groups"]
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

    with sqlite3.connect(str(db_path)) as conn:
        feedback_count = conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
    assert feedback_count == len(
        seed_fixture.load_sample("S7", config)["feedbacks"]
    ), feedback_count

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

    for sample_id in seed_fixture.sample_ids(config):
        result = check_memory.check_sample(sample_id, config, reuse=True)
        total += len(result["rows"])
        if result["failure_count"]:
            failures.append((sample_id, result["failure_count"]))

    assert not failures, failures
    assert total == 20, total


# ---------------------------------------------------------------------------
# T6 Prompt 构建
# ---------------------------------------------------------------------------


def test_prompts_build_and_slots_are_exact():
    config = _config()
    build = build_prompts.build_sample_prompts("S1", config, reuse=True)

    assert sorted(build["prompts"]) == ["A", "B", "C", "D"]

    failed = [check for check in build["checks"] if not check[1]]
    assert not failed, failed

    contents = build["contents"]
    assert build_prompts.MEMORY_HEADER not in contents["A"]
    assert build_prompts.MEMORY_HEADER not in contents["B"]
    assert build_prompts.MEMORY_HEADER in contents["C"]
    assert build_prompts.MEMORY_HEADER in contents["D"]

    assert build_prompts.SHORT_TERM_HEADER not in contents["A"]
    assert build_prompts.SHORT_TERM_HEADER in contents["B"]

    for group, prompt in build["prompts"].items():
        assert prompt["chars"] == len(prompt["user"]), group
        assert len(prompt["prompt_sha256"]) == 64, group
        assert "用户请求：" in prompt["user"], group

    again = build_prompts.build_sample_prompts("S1", config, reuse=True)
    for group in ("A", "B", "C", "D"):
        assert (
            build["prompts"][group]["prompt_sha256"]
            == again["prompts"][group]["prompt_sha256"]
        ), f"{group} 的 Prompt 不可复现"


# ---------------------------------------------------------------------------
# T7 无模型调用
# ---------------------------------------------------------------------------


def test_no_model_or_embedding_call():
    config = _config()
    api = build_prompts.project_api(config)

    calls = []

    def _boom(name):
        def _stub(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"脚手架不应该调用 {name}")

        return _stub

    import ai_service
    import embedding_service

    saved = {
        "ai_service.call_ollama": ai_service.call_ollama,
        "embedding_service.get_embedding": embedding_service.get_embedding,
        "embedding_service.get_embeddings": embedding_service.get_embeddings,
    }

    try:
        ai_service.call_ollama = _boom("ai_service.call_ollama")
        embedding_service.get_embedding = _boom("embedding_service.get_embedding")
        embedding_service.get_embeddings = _boom("embedding_service.get_embeddings")

        build_prompts.activate_sample("S1", config, reuse=True)
        build = build_prompts.build_sample_prompts("S1", config, reuse=True)

        assert not calls, calls
        assert not [check for check in build["checks"] if not check[1]]
        assert build["inputs"]["knowledge_count"] == 0, build["inputs"]
        assert build["inputs"]["knowledge_search_failed"] is False
    finally:
        ai_service.call_ollama = saved["ai_service.call_ollama"]
        embedding_service.get_embedding = saved["embedding_service.get_embedding"]
        embedding_service.get_embeddings = saved["embedding_service.get_embeddings"]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _run_all():
    seed_fixture.seed_all(_config())

    tests = [
        test_config_and_samples,
        test_subject_keys_follow_normalize_subject,
        test_seed_keeps_real_database_untouched,
        test_each_sample_builds_its_own_database,
        test_memory_matches_gold_standard,
        test_prompts_build_and_slots_are_exact,
        test_no_model_or_embedding_call,
    ]

    for test in tests:
        print(f"\n=== {test.__name__} ===")
        test()

    print(f"\nALL {len(tests)} TESTS COMPLETED")


if __name__ == "__main__":
    _run_all()
