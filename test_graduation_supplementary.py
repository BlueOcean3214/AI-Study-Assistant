"""毕业设计补充实验测试：S5×C-subj / S5×D-subj 与 S7-empty×D。

覆盖（全部离线，不调用任何对话模型或 Embedding）：
    T1  主题过滤由 memory_service.normalize_subject() 决定，S5 唯一命中「概率论」
    T2  多主题请求必须拒绝（SubjectResolutionError），不得字符串截取或猜
    T3  C-subj/D-subj 只在 S5 上构建；S7-empty 只在 D 上构建
    T4  S5 补充组组装完成，C-subj/D-subj 的记忆区块只含概率论、不含线性代数
    T5  补充组不改动 S5 的 A/B/C/D 与 S7 的 B0 冻结 Prompt 哈希
    T6  evidence_steps 对主题组记录 arguments={subject, limit}，且 result 与注入一致
    T7  S7-empty 零反馈/零计划重建后记忆集合严格为空（不只是条数为 0）
    T8  空记忆核验能识别被污染的记忆集合
    T9  S7-empty 的 D 组 Prompt 没有记忆区块、反馈条数为 0
    T10 非法组合在创建目录与模型请求之前被拒绝
    T11 默认计划不含补充组与 S7-empty
    T12 S5 上 --group C-subj --group D-subj --repeats 5 恰好 10 个任务
    T13 全程不触碰真实 feedback.db（SHA256/大小/mtime 前后一致）

隔离：FEEDBACK_DB_PATH 在 import database 之前指向临时目录；本测试不需要任何 API Key。
"""

import copy
import hashlib
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
import run_experiment  # noqa: E402
import seed_fixture  # noqa: E402

# 冻结基线（来源提交 df4b938，取值见 test_graduation_experiment_runner.py
# 的 FROZEN_BASELINE_PROMPT_SHA256）；补充组不得改动这些哈希。
FROZEN_S5 = {
    "A": "864c1bce13d764952e5c7538526bf467bf5d7f6ecb8a2c50acfda668aeaea059",
    "B": "4876f3c570703297ca5fc0977e2541bbd007b30b4a40e48c60274fcec6bb1759",
    "C": "467a59a89335cf1374ea515445497c5c9a818b0187e18f0e867b698737dcf7bc",
    "D": "ab8d8b7750effeed830db6c0816ea9d31195b7dc02591e48cf3aa8021d29d550",
}
FROZEN_S7_B0 = "687932c8df552eadba18ef0376949d01b205ad3d98e9c96a1efe2b16979161a0"


def _config():
    return seed_fixture.load_config()


def _real_db_fingerprint():
    stat = REAL_DB.stat()
    digest = hashlib.sha256(REAL_DB.read_bytes()).hexdigest().upper()

    return (digest, stat.st_size, int(stat.st_mtime))


def _plan(argv, config):
    return run_experiment.resolve_plan(run_experiment.parse_args(list(argv)), config)


def _failed_checks(build):
    return [check for check in build["checks"] if not check[1]]


# ---------------------------------------------------------------------------
# 主题过滤
# ---------------------------------------------------------------------------


def test_subject_filter_follows_normalize_subject():
    config = _config()
    sample = seed_fixture.load_sample("S5", config)
    api = build_prompts.project_api(config)
    service = api["memory_service"]

    assert service.normalize_subject("概率论练习") == ("概率论", "概率论练习")
    assert build_prompts.SUBJECT_FILTER_SAMPLES["S5"] == "概率论"

    request = service._clean_text(sample["current_user_request"])
    keys = {service.normalize_subject(feedback["task"])[0] for feedback in sample["feedbacks"]}

    assert keys == {"概率论", "线性代数"}, keys
    assert {key for key in keys if key in request} == {"概率论"}, (keys, request)

    resolved = build_prompts.resolve_request_subject(
        sample, sample["current_user_request"], api
    )

    assert resolved == "概率论", resolved


def test_ambiguous_request_is_rejected():
    config = _config()
    api = build_prompts.project_api(config)

    ambiguous = {
        "feedbacks": [{"task": "概率论练习"}, {"task": "线性代数练习"}],
    }

    try:
        build_prompts.resolve_request_subject(
            ambiguous, "概率论和线性代数都想练", api
        )
    except build_prompts.SubjectResolutionError as error:
        assert "概率论" in str(error) or "多个" in str(error), error
    else:
        raise AssertionError("多主题请求必须被拒绝，不能用字符串截取猜一个")

    missing = {"feedbacks": [{"task": "概率论练习"}]}

    try:
        build_prompts.resolve_request_subject(missing, "明天想练英语听力", api)
    except build_prompts.SubjectResolutionError:
        pass
    else:
        raise AssertionError("请求里没有主题时必须被拒绝")


def test_groups_are_restricted_per_sample():
    assert build_prompts.groups_for_sample("S5", include_supplementary=True) == list(
        build_prompts.CORE_GROUPS
    ) + list(build_prompts.SUBJECT_GROUPS)

    for sample_id in ("S1", "S2", "S3", "S4", "S6", "S7"):
        groups = build_prompts.groups_for_sample(sample_id, include_supplementary=True)
        assert groups == list(build_prompts.CORE_GROUPS), (sample_id, groups)

    assert build_prompts.groups_for_sample("S7-empty") == ["D"]
    assert build_prompts.groups_for_sample("S7-empty", include_supplementary=True) == ["D"]

    for sample_id, group in (("S1", "C-subj"), ("S7", "D-subj"), ("S7-empty", "A")):
        try:
            build_prompts.validate_groups(sample_id, [group])
        except ValueError:
            pass
        else:
            raise AssertionError(f"{sample_id}/{group} 必须被拒绝")


# ---------------------------------------------------------------------------
# 补充组 Prompt 组装
# ---------------------------------------------------------------------------


def test_s5_supplementary_prompts():
    config = _config()
    build = build_prompts.build_sample_prompts(
        "S5", config, include_supplementary=True
    )

    assert build["groups"] == [
        "A", "B", "B0", "C", "D", "C-subj", "D-subj",
    ], build["groups"]
    assert _failed_checks(build) == [], _failed_checks(build)

    assert build["inputs"]["subject_filter"] == "概率论"
    assert build["inputs"]["subject_memory_count"] > 0, build["inputs"]

    for group in ("C-subj", "D-subj"):
        content = build["contents"][group]
        assert build_prompts.MEMORY_HEADER in content, group
        assert content.count(build_prompts.MEMORY_HEADER) == 1, group
        assert "线性代数" not in content, group
        assert "概率论" in content, group

    # 全主题组保持原样：仍然包含两个主题
    assert "线性代数" in build["contents"]["C"]
    assert "线性代数" in build["contents"]["D"]


def test_frozen_hashes_unchanged():
    config = _config()

    s5 = build_prompts.build_sample_prompts("S5", config, include_supplementary=True)

    for group, expected in FROZEN_S5.items():
        actual = s5["prompts"][group]["prompt_sha256"]
        assert actual == expected, (group, actual, expected)

    s7 = build_prompts.build_sample_prompts("S7", config, groups=["B0"])

    assert s7["prompts"]["B0"]["prompt_sha256"] == FROZEN_S7_B0
    assert _failed_checks(s7) == [], _failed_checks(s7)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def _fake_inputs():
    subject_tool = {
        "ok": True,
        "count": 2,
        "memories": [
            {"type": "task_topic", "subject": "概率论"},
            {"type": "task_performance", "subject": "概率论"},
        ],
        "reason": None,
        "info": {"chars": 120},
    }
    all_tool = {
        "ok": True,
        "count": 5,
        "memories": [{"type": "task_topic", "subject": "线性代数"}],
        "reason": None,
        "info": {"chars": 300},
    }

    return {
        "feedback_result": {"count": 3, "days": 7, "summary": {}, "status_counts": {}},
        "raw_feedback_result": {"count": 3, "days": 7, "summary": {}},
        "empty_feedback": {"count": 0, "days": 7, "summary": {}},
        "difficult": False,
        "short_term_block": "【学习者近期情况（程序统计，非模型总结）】…",
        "memory_result": {"ok": True, "count": 1, "memories": [{"type": "task_topic"}]},
        "memory_tool": all_tool,
        "subject_memory_result": {
            "ok": True,
            "count": 2,
            "memories": subject_tool["memories"],
        },
        "subject_memory_tool": subject_tool,
        "subject_filter": "概率论",
        "memory_limit": 5,
    }


def test_evidence_steps_use_subject_tool():
    inputs = _fake_inputs()
    subject_tool = inputs["subject_memory_tool"]

    steps = run_experiment.evidence_steps("C-subj", inputs)

    assert [step["tool"] for step in steps] == ["get_learning_memory"], steps
    assert steps[0]["arguments"] == {"subject": "概率论", "limit": 5}, steps[0]
    assert steps[0]["result"] is subject_tool
    assert steps[0]["ok"] is True

    d_steps = run_experiment.evidence_steps("D-subj", inputs)

    assert [step["tool"] for step in d_steps] == [
        "get_recent_feedback",
        "get_learning_memory",
    ], d_steps
    assert d_steps[1]["arguments"] == {"subject": "概率论", "limit": 5}, d_steps[1]
    assert d_steps[1]["result"] is subject_tool
    assert d_steps[0]["result"] == inputs["feedback_result"]

    c_steps = run_experiment.evidence_steps("C", inputs)

    assert c_steps[0]["arguments"] == {}, c_steps[0]
    assert c_steps[0]["result"] is inputs["memory_tool"]

    # 主题组注入的就是 subject_memory_result；主组仍是全主题 memory_result
    assert build_prompts.group_inputs("C-subj", inputs)["memory_result"] is inputs[
        "subject_memory_result"
    ]
    assert build_prompts.group_inputs("C", inputs)["memory_result"] is inputs[
        "memory_result"
    ]


# ---------------------------------------------------------------------------
# S7-empty
# ---------------------------------------------------------------------------


def test_empty_sample_memory_strictly_empty():
    config = _config()
    sample = seed_fixture.load_sample("S7-empty", config)

    assert sample["feedbacks"] == []
    assert sample["plan"] is None
    assert sample["gold_standard"]["memories"] == []
    assert sample["current_user_request"] == seed_fixture.load_sample("S7", config)[
        "current_user_request"
    ]

    seeded = seed_fixture.seed_sample("S7-empty", config)

    assert seeded["feedback_ids"] == []
    assert seeded["plan_id"] is None
    assert seeded["rebuild"]["ok"] is True, seeded["rebuild"]
    assert seeded["rebuild"]["memories"] == 0, seeded["rebuild"]
    assert seeded["memories"]["memories"] == []
    assert seeded["memories"]["pendings"] == 0

    db_path = Path(seeded["db_path"])

    connection = sqlite3.connect(str(db_path))
    try:
        for table in (
            "learning_memories",
            "memory_evidence",
            "memory_events",
            "memory_pending_evidence",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, (table, count)
    finally:
        connection.close()

    checked = check_memory.check_sample("S7-empty", config, reuse=True)

    assert checked["failure_count"] == 0, checked["reasons"]
    assert checked["empty_check"]["ok"] is True, checked["empty_check"]
    assert checked["empty_check"]["detail"]["memories"] == 0
    assert checked["empty_check"]["detail"]["distinct_keys"] == 0
    assert checked["empty_check"]["detail"]["injected"] == 0
    assert checked["empty_check"]["detail"]["pendings"] == 0


def test_empty_check_detects_polluted_memory_set():
    config = _config()
    sample = seed_fixture.load_sample("S7-empty", config)
    seeded = seed_fixture.seed_sample("S7-empty", config)
    db_path = Path(seeded["db_path"])
    dump = seed_fixture.dump_memories(db_path)

    polluted = copy.deepcopy(dump)
    polluted["memories"] = [
        {
            "memory_type": "task_topic",
            "subject_key": "数据结构",
            "status": "active",
            "strength": 0.5,
            "content": "伪造记忆",
            "evidence_relations": {"supports": 0, "contradicts": 0},
            "updated_at": "2026-11-12T00:00:00",
        }
    ]

    result = check_memory.verify_sample(sample, polluted, db_path, config, {"ok": True})

    assert result["empty_check"]["ok"] is False, result["empty_check"]
    assert result["empty_check"]["detail"]["memories"] == 1
    assert result["failure_count"] >= 1


def test_empty_sample_prompt_has_no_memory_block():
    config = _config()
    seed_fixture.seed_sample("S7-empty", config)

    build = build_prompts.build_sample_prompts("S7-empty", config, groups=["D"])

    assert build["groups"] == ["D"], build["groups"]
    assert _failed_checks(build) == [], _failed_checks(build)

    content = build["contents"]["D"]

    assert build_prompts.MEMORY_HEADER not in content
    assert "数据结构" in content
    assert build["inputs"]["feedback_count"] == 0, build["inputs"]
    assert build["inputs"]["memory_count"] == 0, build["inputs"]
    assert build["inputs"]["empty_feedback"]["count"] == 0, build["inputs"]

    # 记忆 Tool 成功返回 0 条（槽位启用 ≠ 有内容可注入）
    slots = build_prompts.GROUP_SLOTS["D"]
    assert slots["memory"] is True


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def test_invalid_combinations_are_rejected():
    config = _config()

    for argv in (
        ["--group", "C-subj"],
        ["--group", "D-subj"],
        ["--sample", "S1", "--group", "C-subj"],
        ["--sample", "S7", "--group", "D-subj"],
        ["--sample", "S7-empty", "--group", "A"],
        ["--sample", "S7-empty", "--group", "C"],
        ["--sample", "S7-empty", "--group", "C-subj"],
    ):
        try:
            _plan(argv, config)
        except run_experiment.PlanError:
            continue
        else:
            raise AssertionError(f"{argv} 必须被拒绝")

    for argv in (["--group", "S7-empty"], ["--group", "C-subj "]):
        try:
            run_experiment.parse_args(argv)
        except SystemExit as exit_error:
            code = exit_error.code
            assert code == 2, (argv, code)
        else:
            raise AssertionError(f"{argv} 必须被 argparse 拒绝")


def test_default_plan_has_no_supplementary_groups():
    config = _config()
    plan = _plan([], config)

    assert plan, plan
    assert {task["group"] for task in plan} <= set(build_prompts.CORE_GROUPS)
    assert not any(task["group"] in build_prompts.SUBJECT_GROUPS for task in plan)
    assert not any(task["sample_id"] == "S7-empty" for task in plan)
    assert all(task["sample_selection"] == "default" for task in plan)
    assert [task for task in plan if task["group"] == "B0"] == [
        task for task in plan if task["group"] == "B0" and task["sample_id"] in ("S5", "S7")
    ]


def test_supplementary_plan_counts():
    config = _config()
    args = run_experiment.parse_args(
        [
            "--sample", "S5",
            "--group", "C-subj",
            "--group", "D-subj",
            "--repeats", "5",
        ]
    )
    plan = run_experiment.resolve_plan(args, config)

    assert args.repeats == 5
    assert len(plan) == 2, plan
    assert {task["group"] for task in plan} == {"C-subj", "D-subj"}
    assert all(task["sample_id"] == "S5" for task in plan)
    assert len(plan) * args.repeats == 10

    explicit = _plan(["--sample", "S5", "--group", "C-subj"], config)

    assert len(explicit) == 1 and explicit[0]["sample_selection"] == "explicit"


def test_main_rejects_before_creating_run_dir():
    target = (
        Path(tempfile.gettempdir())
        / "graduation_experiments"
        / "_supplementary_rejected_run"
    )

    if target.exists():
        shutil.rmtree(target)

    code = run_experiment.main(
        ["--sample", "S7-empty", "--group", "A", "--run-dir", str(target)]
    )

    assert code == 2, code
    assert not target.exists(), "拒绝必须发生在创建目录与模型请求之前"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _run_all():
    before = _real_db_fingerprint()

    tests = [
        test_subject_filter_follows_normalize_subject,
        test_ambiguous_request_is_rejected,
        test_groups_are_restricted_per_sample,
        test_s5_supplementary_prompts,
        test_frozen_hashes_unchanged,
        test_evidence_steps_use_subject_tool,
        test_empty_sample_memory_strictly_empty,
        test_empty_check_detects_polluted_memory_set,
        test_empty_sample_prompt_has_no_memory_block,
        test_invalid_combinations_are_rejected,
        test_default_plan_has_no_supplementary_groups,
        test_supplementary_plan_counts,
        test_main_rejects_before_creating_run_dir,
    ]

    for test in tests:
        print(f"\n=== {test.__name__} ===")
        test()

    after = _real_db_fingerprint()

    assert after == before, "真实 feedback.db 在测试过程中被改动"

    print("\n真实 feedback.db：" + f"sha256={before[0]} size={before[1]} mtime={before[2]}")
    print(f"\nALL {len(tests)} TESTS COMPLETED")


if __name__ == "__main__":
    _run_all()
