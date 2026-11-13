"""毕业设计实验执行层（run_experiment.py）的离线测试。

覆盖：
- CLI 参数与 --repeats 边界、B0 默认样本
- B0 五个槽位 / A/B/C/D 槽位不受影响（含与 build_prompts 基线 prompt_sha256 对齐）
- 采样参数进入 openai / ollama 的真实请求体（HTTP 与 SDK 边界全部打桩）
- 起草失败与异常都记录错误、修正次数不超过 2
- JSONL 只追加不覆盖、Prompt 文件拒绝覆盖、sha256 正确
- token 缺失记 unavailable、数据库隔离断言、真实 feedback.db 前后指纹不变

全部离线：不发起任何真实对话模型 / Embedding 请求，不读写真实 feedback.db。
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRADUATION_DIR = HERE / "experiments" / "graduation"

for path in (str(HERE), str(GRADUATION_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import llm_client  # noqa: E402
import seed_fixture  # noqa: E402
import build_prompts  # noqa: E402
import plan_schedule  # noqa: E402
import run_experiment  # noqa: E402

PASSED = 0
FAILED = []
INFO = []


def check(name, condition, detail=""):
    global PASSED

    if condition:
        PASSED += 1
        print(f"PASS  {name}")
        return True

    FAILED.append(f"{name}: {detail}")
    print(f"FAIL  {name}  {detail}")
    return False


def info(name, detail):
    INFO.append(f"{name}: {detail}")
    print(f"INFO  {name}  {detail}")


def real_db_fingerprint():
    path = Path(run_experiment.REAL_DB)

    if not path.is_file():
        return {"exists": False}

    stat = path.stat()

    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
    }


@contextlib.contextmanager
def fake_http(handler):
    """替换 llm_client 的唯一 HTTP 边界。"""

    calls = []
    original = llm_client._http_post

    def recording(url, headers, payload, stream, timeout):
        calls.append(payload)
        return handler(url, headers, payload, stream, timeout)

    llm_client._http_post = recording

    try:
        yield calls
    finally:
        llm_client._http_post = original


class FakeResponse:
    def __init__(self, lines):
        self._lines = list(lines)

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        return None


class FakeCompletions:
    def __init__(self, captured):
        self.captured = captured

    def create(self, **payload):
        self.captured.append(payload)

        return iter(
            [
                types.SimpleNamespace(
                    usage=None,
                    choices=[
                        types.SimpleNamespace(
                            delta=types.SimpleNamespace(content="ok", tool_calls=None)
                        )
                    ],
                ),
                types.SimpleNamespace(
                    usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=5),
                    choices=[],
                ),
            ]
        )


def fake_openai_client(captured):
    client = types.SimpleNamespace()
    client.chat = types.SimpleNamespace(completions=FakeCompletions(captured))

    return client


def ollama_settings():
    return llm_client.Settings(
        provider=llm_client.PROVIDER_OLLAMA,
        model="mock-ollama",
        chat_url="http://mock.invalid/api/chat",
    )


def openai_settings():
    return llm_client.Settings(
        provider=llm_client.PROVIDER_OPENAI,
        model="mock-openai",
        chat_url="http://mock.invalid/v1",
        api_key="test-only-not-a-real-key",
    )


MESSAGES = [{"role": "user", "content": "hi"}]
SAMPLING = {"temperature": 0.7, "top_p": 1.0, "max_tokens": 2048}

# ---------------------------------------------------------------------------
# 冻结的旧版 Prompt 哈希（来源提交 df4b93806d7520181a9b5771fa0d09802fa9a791）
#
# 导出方式：`git worktree add --detach <dir> df4b938`，在该基线的独立 worktree 里用
# 它自己的 experiments/graduation/build_prompts.py 跑 build_sample_prompts(sample, config,
# reuse=False)，只取 A/B/C/D（B0 由本 PR 新增，不属于旧版基线）。导出脚本与当前分支
# 完全隔离，因此这些常量是真正的旧版基线，不是「当前代码和自己比」。
# ---------------------------------------------------------------------------

FROZEN_BASELINE_PROMPT_SHA256 = {
    "S1": {
        "A": "1cba51d1399648b3ba66621897b7a79396668e544499f622690f1a93b008e656",
        "B": "bd1e0323575e04392aaf3f1496ffd83873bb8c2e1d9e951ec6a2e9613a6cc5da",
        "C": "56dd4573db5ba4b07fed872e81b23865267e953ed52b74ead32b951b7e702da7",
        "D": "cafc40c63a3edd16454fc6f97db721e16477f6ae0dfa64af559d1c775a4d223a",
    },
    "S2": {
        "A": "6fab10f7cba0b0e93540151c90aa049bd967aaeffead38cb4fb408649f824070",
        "B": "79b613aabe3c9af0375e70a805ce8417ca4f1cd779fdfe113926bd6370606718",
        "C": "271c273071b9e7dfa59da5202aa926406001ee487613cc7671f5b4744188d9f4",
        "D": "2791b9239ccf9f8f81125e51acc11c290dcb61ce2f3937d4fd57087617bbac14",
    },
    "S3": {
        "A": "c52fadd41f75a5ff2a55388607fe204b474352f8cda729a715a0defb51e3da13",
        "B": "292d22d3355c31e12ceab0793120913aadeaa49fc9d77b892fcbe708f3bba7c6",
        "C": "d6ce98dfa17e123bc43b4695b12715c4f3f5d9e4d53f59de6373c7c30953b94e",
        "D": "9847630fd371ee3edb44a30c74250823f07bf2b80e0769213ed85442aa863f91",
    },
    "S4": {
        "A": "0c01afd08fd58527d265aba4b9964359220a985acae843497b00ad688149e2a9",
        "B": "7e733e32d4cad56047d775331945423d21ab3d20b52db388b55afff3891fe618",
        "C": "c96aa71ab1c085edc31f0e86ec9481e9c51ad8ed83240edf20b2a7d4f08ac450",
        "D": "e36f01b51e9e39e417d4e973019c027fc3d3dd9853b824e81ddfb77b2de1c07b",
    },
    "S5": {
        "A": "864c1bce13d764952e5c7538526bf467bf5d7f6ecb8a2c50acfda668aeaea059",
        "B": "4876f3c570703297ca5fc0977e2541bbd007b30b4a40e48c60274fcec6bb1759",
        "C": "467a59a89335cf1374ea515445497c5c9a818b0187e18f0e867b698737dcf7bc",
        "D": "ab8d8b7750effeed830db6c0816ea9d31195b7dc02591e48cf3aa8021d29d550",
    },
    "S6": {
        "A": "60ca716930fd80b7cd4edecc8d86d8fb595d0e25c33bbed04f0b8729673369ac",
        "B": "d93b593ab992a5f94a61685fca254f7e1b653b808d5d60e284d28de58dceba01",
        "C": "38faace385fa47c638444c7483130c6fdabf6de63c5cabcf8f9fb020e5529d62",
        "D": "b031a726b9648e7af4b15e52cc5c15aacd3df91a51cf62cb479426f28ab334ac",
    },
    "S7": {
        "A": "9ddf093dab8640368d8cb0dab745cbc5420fcf8db58164748fd55c5285aa4c3d",
        "B": "c47ae633b8223b2ff8da765bd837de3287135212a9d3dd5f80dcc222fce2cc5d",
        "C": "901d67901abbbdf1bff17e7eb46b93d0dccc9bf41330de47d683b0a11eff4103",
        "D": "aa55f5caef21e71cf296dab6bd1a2d27f8a2a762fb73722992b1b11adb883803",
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_defaults():
    args = run_experiment.parse_args([])
    config = seed_fixture.load_config()
    settings = run_experiment.randomization_settings(config)

    check("CLI 默认 repeats 为 None（轮次取 config）", args.repeats is None, args.repeats)
    check(
        "config.randomization.round_count 默认 5",
        settings["round_count"] == 5,
        settings,
    )
    check("CLI 默认 sample/group 为 None", args.sample is None and args.group is None)
    check("CLI 默认不 reuse", args.reuse is False)


def test_cli_repeats_bounds():
    for good in ("1", "5", "50"):
        args = run_experiment.parse_args(["--repeats", good])

        check(f"CLI --repeats {good} 接受", args.repeats == int(good), args.repeats)

    for bad in ("0", "51", "-1", "abc", "1.5"):
        stderr = io.StringIO()

        try:
            with contextlib.redirect_stderr(stderr):
                run_experiment.parse_args(["--repeats", bad])
        except SystemExit as error:
            check(f"CLI --repeats {bad} 拒绝", int(error.code) == 2, error.code)
        else:
            check(f"CLI --repeats {bad} 拒绝", False, "没有抛出 SystemExit")


def test_cli_group_choices():
    args = run_experiment.parse_args(["--group", "B0", "--group", "A"])

    check("CLI --group B0 可选", args.group == ["B0", "A"], args.group)

    stderr = io.StringIO()

    try:
        with contextlib.redirect_stderr(stderr):
            run_experiment.parse_args(["--group", "X"])
    except SystemExit as error:
        check("CLI 未知组拒绝", int(error.code) == 2, error.code)
    else:
        check("CLI 未知组拒绝", False, "没有抛出 SystemExit")


def test_positive_int_direct():
    check("positive_int(1)=1", run_experiment.positive_int("1") == 1)
    check("positive_int(50)=50", run_experiment.positive_int("50") == 50)

    import argparse

    for bad in ("0", "51", "x"):
        try:
            run_experiment.positive_int(bad)
        except argparse.ArgumentTypeError:
            check(f"positive_int({bad}) 拒绝", True)
        else:
            check(f"positive_int({bad}) 拒绝", False)


def plan_pairs(plan):
    return [(task["sample_id"], task["group"]) for task in plan]


def default_group_order():
    """默认矩阵的组顺序：A/B/C/D 逐样本，B0 排在最后（与 resolve_plan 两阶段一致）。

    补充组 C-subj/D-subj 与 S7-empty 不属于默认矩阵，只有显式 --group/--sample
    才进入计划，因此这里不取自 GROUP_SLOTS 的全集。
    """

    non_b0 = [group for group in build_prompts.DEFAULT_GROUPS if group != "B0"]
    return non_b0 + ["B0"]


def test_plan_matrix():
    """B0 的默认样本范围必须独立于其他组（任务对，不是一个全局样本列表）。"""

    config = seed_fixture.load_config()
    samples = seed_fixture.sample_ids(config)
    order = default_group_order()

    # 1) 默认全部组：A/B/C/D × 全部样本，B0 只有 S5、S7
    plan = run_experiment.resolve_plan(run_experiment.parse_args([]), config)
    pairs = plan_pairs(plan)
    expected = [
        (sample_id, group) for sample_id in samples for group in order if group != "B0"
    ] + [
        (sample_id, "B0")
        for sample_id in samples
        if sample_id in run_experiment.B0_DEFAULT_SAMPLES
    ]

    check("默认计划：任务对完全符合设计", pairs == expected, pairs)
    check("默认计划：B0 只在 S5/S7", [p for p in pairs if p[1] == "B0"] == [("S5", "B0"), ("S7", "B0")], pairs)
    check("默认计划：A/B/C/D 每个样本各一次", all(pair in pairs for pair in [(s, g) for s in samples for g in ("A", "B", "C", "D")]))
    check("默认计划：30 个任务对（7×4 + 2）", len(pairs) == 30, len(pairs))
    check(
        "默认计划：全部标记 default 且未覆盖 B0 样本",
        all(t["sample_selection"] == "default" and not t["b0_sample_override"] for t in plan),
        plan[:1],
    )

    # 2) 只给 --group B0
    b0_only = plan_pairs(run_experiment.resolve_plan(run_experiment.parse_args(["--group", "B0"]), config))
    check("--group B0 默认只跑 S5/S7", b0_only == [("S5", "B0"), ("S7", "B0")], b0_only)

    # 3) 混合组：A 全样本，B0 仍然只有 S5/S7
    mixed = plan_pairs(
        run_experiment.resolve_plan(run_experiment.parse_args(["--group", "A", "--group", "B0"]), config)
    )
    check("A+B0：B0 不被扩张到全部样本", [p for p in mixed if p[1] == "B0"] == [("S5", "B0"), ("S7", "B0")], mixed)
    check("A+B0：A 跑全部样本", [p for p in mixed if p[1] == "A"] == [(s, "A") for s in samples], mixed)
    check("A+B0：共 9 个任务对", len(mixed) == 9, len(mixed))

    # 4) 显式 --sample 保留覆盖语义，并把覆盖记进元数据
    explicit = run_experiment.resolve_plan(
        run_experiment.parse_args(["--sample", "S5", "--group", "B0"]), config
    )
    check("显式 S5/B0 只有一个任务对", plan_pairs(explicit) == [("S5", "B0")], explicit)
    check(
        "显式命中 B0 默认样本：selection=explicit、override=False",
        explicit[0]["sample_selection"] == "explicit" and not explicit[0]["b0_sample_override"],
        explicit,
    )

    override = run_experiment.resolve_plan(
        run_experiment.parse_args(["--sample", "S1", "--group", "B0"]), config
    )
    check("显式 S1/B0 按覆盖语义执行", plan_pairs(override) == [("S1", "B0")], override)
    check(
        "显式超出 B0 默认样本：override=True 留痕",
        override[0]["sample_selection"] == "explicit" and override[0]["b0_sample_override"],
        override,
    )

    all_groups_explicit = run_experiment.resolve_plan(
        run_experiment.parse_args(["--sample", "S1"]), config
    )
    check(
        "显式 --sample 时五组都跑该样本（含 B0 覆盖标记）",
        plan_pairs(all_groups_explicit) == [("S1", group) for group in order]
        and [task["group"] for task in all_groups_explicit if task["b0_sample_override"]]
        == ["B0"],
        all_groups_explicit,
    )


def test_main_dispatch_respects_plan(run_dir):
    """真实 main 循环按任务对分发：默认全部组时 B0 只能出现在 S5/S7。"""

    config = seed_fixture.load_config()
    samples = seed_fixture.sample_ids(config)
    target = Path(run_dir) / "dispatch_default"
    dispatched = []
    preflighted = []
    events = []

    originals = {
        "isolate": run_experiment.isolate_database,
        "register": seed_fixture.register_project_modules,
        "activate": build_prompts.activate_sample,
        "collect": build_prompts.collect_inputs,
        "project_api": build_prompts.project_api,
        "run_one": run_experiment.run_one,
        "preflight": run_experiment.preflight_sample,
    }

    def fake_preflight(sample_id, groups, inputs, api, db_path, config, subject_count=None):
        preflighted.append((sample_id, list(groups)))
        events.append(("preflight", sample_id))
        return []

    def fake_run_one(**kwargs):
        dispatched.append((kwargs["sample_id"], kwargs["group"], kwargs["repeat_index"]))
        events.append(("run", kwargs["sample_id"]))
        return {
            "experiment_id": kwargs["experiment_id"],
            "sample_id": kwargs["sample_id"],
            "group": kwargs["group"],
            "repeat_index": kwargs["repeat_index"],
            "sample_selection": kwargs["sample_selection"],
            "b0_sample_override": kwargs["b0_sample_override"],
            "plan_seq": kwargs.get("plan_seq"),
            "round_index": kwargs.get("round_index"),
            "seed": kwargs.get("seed"),
            "scheme": kwargs.get("scheme"),
            "algorithm_version": kwargs.get("algorithm_version"),
            "plan_sha256": kwargs.get("plan_sha256"),
            "raw_output": None,
            "raw_outputs": [],
            "success": True,
            "request_count": 0,
            "corrections_used": 0,
            "prompt_chars": 0,
            "error_type": None,
            "error_message": None,
        }

    run_experiment.isolate_database = lambda path: (object(), object())
    seed_fixture.register_project_modules = lambda *args, **kwargs: None
    build_prompts.project_api = lambda config=None: {}
    build_prompts.activate_sample = lambda sample_id, config, reuse=False, sample=None: (
        Path(f"{sample_id}.db"),
        {},
    )
    build_prompts.collect_inputs = lambda sample, config, api, moment: {}
    run_experiment.preflight_sample = fake_preflight
    run_experiment.run_one = fake_run_one

    stdout = io.StringIO()

    try:
        with contextlib.redirect_stdout(stdout):
            code = run_experiment.main(["--run-dir", str(target)])
    finally:
        run_experiment.isolate_database = originals["isolate"]
        seed_fixture.register_project_modules = originals["register"]
        build_prompts.activate_sample = originals["activate"]
        build_prompts.collect_inputs = originals["collect"]
        build_prompts.project_api = originals["project_api"]
        run_experiment.run_one = originals["run_one"]
        run_experiment.preflight_sample = originals["preflight"]

    # 执行顺序完全由 plan.json 决定：sample 区块 → 轮次 → 组（plan_seq 升序）。
    plan = plan_schedule.load_plan(target / plan_schedule.PLAN_NAME)
    expected = [
        (block["sample_id"], task["group"], task["repeat_index"])
        for block in plan["blocks"]
        for task in block["tasks"]
    ]

    check("默认运行 exit code 0", code == 0, code)
    check("执行顺序 == plan.json 的 plan_seq 顺序", dispatched == expected, dispatched[:5])
    check("默认计划共 140 个任务（7 样本 × 20）", len(expected) == 140, len(expected))
    check(
        "每个样本恰好 20 个任务",
        all(len(block["tasks"]) == 20 for block in plan["blocks"]),
        [len(block["tasks"]) for block in plan["blocks"]],
    )
    check(
        "每个样本每轮 A/B/C/D 各一次",
        all(
            sorted(
                task["group"]
                for task in block["tasks"]
                if task["round_index"] == round_index
            )
            == ["A", "B", "C", "D"]
            for block in plan["blocks"]
            for round_index in range(5)
        ),
    )
    check(
        "repeat_index == round_index",
        all(task["repeat_index"] == task["round_index"] for block in plan["blocks"] for task in block["tasks"]),
    )
    check(
        "B0 不进入五轮计划（legacy 不重跑）",
        all(task["group"] != "B0" for block in plan["blocks"] for task in block["tasks"]),
        [block["sample_id"] for block in plan["blocks"]],
    )
    check(
        "同一计划里每个样本的任务连续执行完再进下一个样本",
        [sample_id for sample_id, _, _ in dispatched]
        == [block["sample_id"] for block in plan["blocks"] for _ in block["tasks"]],
    )

    expected_preflight = [
        (block["sample_id"], sorted({task["group"] for task in block["tasks"]}))
        for block in plan["blocks"]
    ]

    check(
        "每个样本在模型调用前都做了预检（只含待执行组）",
        [(sample_id, sorted(groups)) for sample_id, groups in preflighted]
        == expected_preflight,
        preflighted,
    )

    first_run = {}

    for index, (kind, sample_id) in enumerate(events):
        if kind == "run":
            first_run.setdefault(sample_id, index)

    check(
        "预检发生在该样本第一次生成之前",
        all(
            events.index(("preflight", sample_id)) < index
            for sample_id, index in first_run.items()
        ),
        events,
    )

    lines = (target / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]

    check("默认运行写入 140 条结果", len(lines) == 140, len(lines))
    expected_seq = [task["plan_seq"] for block in plan["blocks"] for task in block["tasks"]]

    check(
        "结果 plan_seq 与计划一致",
        [row.get("plan_seq") for row in rows] == expected_seq,
        ([row.get("plan_seq") for row in rows][:6], expected_seq[:6]),
    )
    check(
        "结果文件里没有 B0 记录",
        all(row["group"] != "B0" for row in rows),
        sorted({row["group"] for row in rows}),
    )
    check(
        "默认运行结果是 default 选择",
        all(row["sample_selection"] == "default" for row in rows),
    )


# ---------------------------------------------------------------------------
# 采样参数
# ---------------------------------------------------------------------------


def test_normalize_sampling():
    normalized = llm_client.normalize_sampling(SAMPLING, llm_client.PROVIDER_OPENAI)

    check(
        "采样参数规范化",
        normalized == {"temperature": 0.7, "top_p": 1.0, "max_tokens": 2048},
        normalized,
    )
    check("sampling=None 保持 None", llm_client.normalize_sampling(None, "ollama") is None)

    for bad in (
        {"foo": 1},
        {"temperature": 3},
        {"temperature": "0.7"},
        {"top_p": 1.5},
        {"max_tokens": 0},
        {"max_tokens": True},
    ):
        try:
            llm_client.normalize_sampling(bad, llm_client.PROVIDER_OPENAI)
        except llm_client.LLMConfigError:
            check(f"非法采样参数拒绝 {bad}", True)
        else:
            check(f"非法采样参数拒绝 {bad}", False)


def test_sampling_in_openai_payload():
    captured = []
    original = llm_client._make_openai_client
    llm_client._make_openai_client = lambda settings, read_timeout: fake_openai_client(captured)

    try:
        result, error, error_type = llm_client._chat_openai(
            openai_settings(), MESSAGES, None, None, 30, sampling=SAMPLING
        )
    finally:
        llm_client._make_openai_client = original

    check("openai 调用成功", error is None and result is not None, error)
    check("openai 请求体含采样参数", bool(captured), captured)

    if captured:
        payload = captured[0]

        check("openai temperature=0.7", payload.get("temperature") == 0.7, payload)
        check("openai top_p=1.0", payload.get("top_p") == 1.0, payload)
        check("openai max_tokens=2048", payload.get("max_tokens") == 2048, payload)
        check("openai 不写 options", "options" not in payload, payload)

    captured.clear()
    llm_client._make_openai_client = lambda settings, read_timeout: fake_openai_client(captured)

    try:
        llm_client._chat_openai(openai_settings(), MESSAGES, None, None, 30, sampling=None)
    finally:
        llm_client._make_openai_client = original

    if captured:
        payload = captured[0]

        check(
            "sampling=None 时 openai 请求体不变",
            not {"temperature", "top_p", "max_tokens", "options"} & set(payload),
            sorted(payload),
        )


def test_sampling_in_ollama_payload():
    lines = [
        json.dumps({"message": {"content": "ok"}, "done": False}),
        json.dumps(
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 5}
        ),
    ]

    with fake_http(lambda url, headers, payload, stream, timeout: FakeResponse(lines)) as calls:
        result, error, error_type = llm_client._chat_ollama(
            ollama_settings(), MESSAGES, None, None, 30, sampling=SAMPLING
        )

    check("ollama 调用成功", error is None and result is not None, error)
    check("ollama 请求体含 options", bool(calls) and "options" in calls[0], calls)

    if calls:
        options = calls[0].get("options") or {}

        check("ollama temperature=0.7", options.get("temperature") == 0.7, options)
        check("ollama top_p=1.0", options.get("top_p") == 1.0, options)
        check("ollama max_tokens→num_predict", options.get("num_predict") == 2048, options)
        check("ollama 不把 max_tokens 放顶层", "max_tokens" not in calls[0], sorted(calls[0]))

    with fake_http(lambda url, headers, payload, stream, timeout: FakeResponse(lines)) as calls2:
        llm_client._chat_ollama(ollama_settings(), MESSAGES, None, None, 30, sampling=None)

    check("sampling=None 时 ollama 请求体不变", bool(calls2) and "options" not in calls2[0])


def test_usage_recorder_gets_sampling():
    recorder = run_experiment.UsageRecorder()
    previous = llm_client._USAGE_RECORDER
    llm_client.set_usage_recorder(recorder)

    try:
        llm_client._finish(
            {"role": "assistant", "content": "x"},
            llm_client._usage_from_counts(10, 20),
            openai_settings(),
            time.monotonic(),
            sampling=llm_client.normalize_sampling(SAMPLING, llm_client.PROVIDER_OPENAI),
        )
    finally:
        llm_client.set_usage_recorder(previous)

    check("recorder 收到一次记录", len(recorder.requests) == 1, len(recorder.requests))

    if recorder.requests:
        record = recorder.requests[0]

        check("记录含 temperature", record["temperature"] == 0.7, record)
        check("记录含 top_p", record["top_p"] == 1.0, record)
        check("记录含 max_tokens", record["max_tokens"] == 2048, record)
        check("记录 token 可用", record["usage_available"] is True, record)
        check("token_usage 汇总", run_experiment.token_usage(recorder.requests)["total_tokens"] == 30)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_token_usage_unavailable():
    empty = run_experiment.token_usage([])

    check(
        "无请求时 token 记 unavailable",
        empty == {
            "available": False,
            "status": "unavailable",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        },
        empty,
    )

    partial = run_experiment.token_usage(
        [
            {"usage_available": True, "prompt_tokens": 3, "completion_tokens": 4},
            {"usage_available": False, "prompt_tokens": None, "completion_tokens": None},
        ]
    )

    check("任一请求缺 token 即 unavailable", partial["available"] is False, partial)
    check("缺 token 时不做估算", partial["total_tokens"] is None, partial)


def test_append_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "results.jsonl"

        run_experiment.append_jsonl(path, {"a": 1, "index": 0})
        run_experiment.append_jsonl(path, {"a": 2, "index": 1})

        lines = path.read_text(encoding="utf-8").splitlines()

        check("JSONL 两行", len(lines) == 2, len(lines))
        check("JSONL 保留第一条", json.loads(lines[0]) == {"a": 1, "index": 0}, lines[0])
        check("JSONL 追加不覆盖", json.loads(lines[1])["index"] == 1, lines[1])


def test_write_prompt_file():
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)

        ref = run_experiment.write_prompt_file(run_dir, "S1", "A", "SYS", "USER")
        again = run_experiment.write_prompt_file(run_dir, "S1", "A", "SYS", "USER")

        check("Prompt 引用为相对路径", ref == "prompts/S1/A.txt", ref)
        check("幂等重写不报错", again == ref, again)

        text = (run_dir / ref).read_text(encoding="utf-8")

        check("Prompt 文件含 system", "SYS" in text and "===== SYSTEM =====" in text, text[:40])

        try:
            run_experiment.write_prompt_file(run_dir, "S1", "A", "SYS", "DIFFERENT")
        except RuntimeError as error:
            check("Prompt 内容不同时拒绝覆盖", "拒绝覆盖" in str(error), error)
        else:
            check("Prompt 内容不同时拒绝覆盖", False, "没有抛出 RuntimeError")


def test_prompt_sha256_manual():
    system = "系统提示"
    user = "用户内容"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    expected = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    check("prompt_sha256 与手工计算一致", build_prompts.prompt_sha256(system, user) == expected)


def test_slots():
    inputs = {
        "feedback_result": {
            "days": 7,
            "count": 2,
            "summary": {"total_count": 2},
            "feedback": [{"id": 1}, {"id": 2}],
        },
        "raw_feedback_result": {
            "days": 7,
            "count": 2,
            "summary": {},
            "feedback": [{"id": 1}, {"id": 2}],
        },
        "empty_feedback": {"days": 7, "count": 0, "summary": {}, "feedback": []},
        "difficult": True,
        "short_term_block": "近期情况",
        "memory_result": {"count": 1, "memories": [{"content": "m"}]},
        "memory_tool": {"ok": True, "count": 1, "memories": [{"content": "m"}]},
        # 补充组：主题过滤后的记忆 Tool 结果（与注入 Prompt 的是同一次调用）
        "subject_memory_result": {"count": 2, "memories": [{"content": "s"}]},
        "subject_memory_tool": {"ok": True, "count": 2, "memories": [{"content": "s"}]},
        "subject_filter": "概率论",
        "memory_limit": 5,
    }

    b0 = build_prompts.group_inputs("B0", inputs)

    check("B0 有原始反馈行", b0["feedback_result"]["feedback"] == [{"id": 1}, {"id": 2}], b0)
    check("B0 summary 为空 dict", b0["feedback_result"]["summary"] == {}, b0)
    check("B0 difficult=False", b0["difficult"] is False, b0)
    check("B0 无 short_term", b0["short_term_block"] is None, b0)
    check("B0 无 memory", b0["memory_result"] is None, b0)

    a = build_prompts.group_inputs("A", inputs)
    b = build_prompts.group_inputs("B", inputs)
    c = build_prompts.group_inputs("C", inputs)
    d = build_prompts.group_inputs("D", inputs)

    check("A 空反馈", a["feedback_result"] == inputs["empty_feedback"], a)
    check("A 无 memory/短期", a["memory_result"] is None and a["short_term_block"] is None, a)
    check("B 真反馈且无 memory", b["feedback_result"] == inputs["feedback_result"] and b["memory_result"] is None, b)
    check("B difficult 取推导值", b["difficult"] is True, b)
    check("C 空反馈 + memory", c["feedback_result"] == inputs["empty_feedback"] and c["memory_result"] is not None, c)
    check("D 真反馈 + memory", d["feedback_result"] == inputs["feedback_result"] and d["memory_result"] is not None, d)

    steps = {group: run_experiment.evidence_steps(group, inputs) for group in build_prompts.GROUP_SLOTS}

    def tools(group):
        return [step["tool"] for step in steps[group]]

    check("A 没有任何 evidence（基线组不注入反馈/记忆）", tools("A") == [], tools("A"))
    check("B evidence 只有反馈", tools("B") == ["get_recent_feedback"], tools("B"))
    check("B0 evidence 只有反馈", tools("B0") == ["get_recent_feedback"], tools("B0"))
    check("C evidence 只有记忆", tools("C") == ["get_learning_memory"], tools("C"))
    check("D evidence 反馈+记忆", tools("D") == ["get_recent_feedback", "get_learning_memory"], tools("D"))
    check(
        "C-subj evidence 只用主题记忆且 arguments 记录 subject/limit",
        tools("C-subj") == ["get_learning_memory"]
        and steps["C-subj"][0]["arguments"] == {"subject": "概率论", "limit": 5}
        and steps["C-subj"][0]["result"] is inputs["subject_memory_tool"],
        steps["C-subj"],
    )
    check(
        "D-subj evidence 是反馈+主题记忆",
        tools("D-subj") == ["get_recent_feedback", "get_learning_memory"]
        and steps["D-subj"][1]["arguments"] == {"subject": "概率论", "limit": 5}
        and steps["D-subj"][1]["result"] is inputs["subject_memory_tool"],
        steps["D-subj"],
    )
    check(
        "C 主组 evidence 仍用全主题记忆且 arguments 为空",
        steps["C"][0]["result"] is inputs["memory_tool"] and steps["C"][0]["arguments"] == {},
        steps["C"],
    )

    all_tools = {step["tool"] for group in steps for step in steps[group]}

    check("知识库永不合成 evidence", "search_knowledge" not in all_tools, all_tools)
    check(
        "B0 evidence 用的是原始反馈（summary 为空）",
        steps["B0"][0]["result"]["summary"] == {},
        steps["B0"][0]["result"],
    )


def test_isolation_assertions(run_dir):
    try:
        run_experiment.assert_isolated(run_dir, run_experiment.REAL_DB)
    except RuntimeError as error:
        check("拒绝真实数据库", "真实数据库" in str(error), error)
    else:
        check("拒绝真实数据库", False)

    try:
        run_experiment.assert_isolated(run_dir, Path(run_dir).parent / "outside.db")
    except RuntimeError as error:
        check("拒绝运行目录之外的库", "拒绝继续" in str(error), error)
    else:
        check("拒绝运行目录之外的库", False)

    inside = run_experiment.assert_isolated(run_dir, Path(run_dir) / "inside.db")

    check("运行目录内的库通过", inside.name == "inside.db", inside)


def test_draft_loop_corrections():
    import agent_plan_draft

    calls = []
    original = agent_plan_draft.agent_loop.chat_with_tools

    def fake_chat_with_tools(messages, tools=None, deadline=None, sampling=None):
        calls.append({"sampling": sampling, "messages": list(messages)})
        return {"role": "assistant", "content": "这不是 JSON"}, None

    agent_plan_draft.agent_loop.chat_with_tools = fake_chat_with_tools

    try:
        result = agent_plan_draft.run_plan_draft_from_inputs(
            "给我一份计划",
            steps=[],
            sampling=SAMPLING,
            verbose=False,
            raw_sink=[],
        )
    finally:
        agent_plan_draft.agent_loop.chat_with_tools = original

    check("起草失败返回 ok=False", result["ok"] is False, result.get("error_type"))
    check("起草失败 error_type=validation_error", result["error_type"] == "validation_error", result)
    check("修正次数不超过 2", result["corrections_used"] == 2, result["corrections_used"])
    check("最多调用 3 次（首轮 + 2 次修正）", len(calls) == 3, len(calls))
    check("采样参数透传到起草调用", all(call["sampling"] == SAMPLING for call in calls), calls[:1])
    check("实验中 tool_calls=0", result["tool_calls"] == 0, result)
    check("实验中 loop_turns=0", result["loop_turns"] == 0, result)
    check(
        "失败的实验记录不带研究结论字段",
        "research_final_answer" not in result,
        sorted(result),
    )


# ---------------------------------------------------------------------------
# 端到端（离线 mock 起草）
# ---------------------------------------------------------------------------


def test_run_one_end_to_end(run_dir, config, api, inputs, baseline):
    calls = []

    def fake_runner(api=None, raw_sink=None, **kwargs):
        calls.append(kwargs)

        if raw_sink is not None:
            raw_sink.append('{"answer": "mock", "plan": {"title": "mock"}}')

        return {
            "ok": True,
            "answer": "mock",
            "plan": {"title": "mock"},
            "corrections_used": 0,
            "stopped_reason": "plan_draft_ready",
        }

    recorder = run_experiment.UsageRecorder()
    records = {}

    for group in build_prompts.CORE_GROUPS:
        records[group] = run_experiment.run_one(
            experiment_id="exp-test",
            sample_id="S2",
            group=group,
            repeat_index=0,
            inputs=inputs,
            api=api,
            run_dir=run_dir,
            recorder=recorder,
            sampling=SAMPLING,
            draft_runner=fake_runner,
        )

    check("五个组都产出一条记录", len(records) == 5, sorted(records))

    for group in build_prompts.CORE_GROUPS:
        record = records[group]
        expected = baseline["prompts"][group]["prompt_sha256"]

        check(f"{group} prompt_sha256 与基线一致", record["prompt_sha256"] == expected, record)
        check(f"{group} 记录 success", record["success"] is True, record.get("error_type"))
        check(f"{group} 无真实请求 request_count=0", record["request_count"] == 0, record)
        check(f"{group} token 记 unavailable", record["token_usage"]["available"] is False, record)
        check(f"{group} raw_output 保留", record["raw_output"] == '{"answer": "mock", "plan": {"title": "mock"}}', record)
        check(f"{group} final_output 含 plan", record["final_output"]["plan"] == {"title": "mock"}, record)
        check(f"{group} 采样参数落记录", record["temperature"] == 0.7 and record["max_tokens"] == 2048, record)
        check(f"{group} prompt 文件存在", (run_dir / record["prompt_ref"]).is_file(), record["prompt_ref"])

    check("A/B/C/D 记录未受 B0 影响（同组仅一次调用）", len(calls) == 5, len(calls))
    check(
        "B0 起草时无 memory_result",
        [call for call in calls if call["memory_result"] is None] and len(
            [call for call in calls if call["short_term_block"] is None]
        ) >= 2,
    )

    # 追加两轮后不覆盖
    for record in records.values():
        run_experiment.append_jsonl(run_dir / "results.jsonl", record)

    for record in records.values():
        run_experiment.append_jsonl(run_dir / "results.jsonl", record)

    lines = (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()

    check("JSONL 追加两轮共 10 行", len(lines) == 10, len(lines))
    check(
        "JSONL 首轮内容保留",
        json.loads(lines[0])["experiment_id"] == "exp-test"
        and json.loads(lines[5])["experiment_id"] == "exp-test",
        lines[0][:40],
    )


def test_run_one_failure_and_exception(run_dir, api, inputs):
    def failing_runner(api=None, raw_sink=None, **kwargs):
        return {
            "ok": False,
            "error_type": "llm_error",
            "message": "模型炸了",
            "corrections_used": 1,
        }

    recorder = run_experiment.UsageRecorder()
    record = run_experiment.run_one(
        experiment_id="exp-test",
        sample_id="S2",
        group="A",
        repeat_index=0,
        inputs=inputs,
        api=api,
        run_dir=run_dir,
        recorder=recorder,
        sampling=SAMPLING,
        draft_runner=failing_runner,
    )

    check("失败结果 success=False", record["success"] is False, record)
    check("失败结果保留 error_type", record["error_type"] == "llm_error", record)
    check("失败结果保留 error_message", record["error_message"] == "模型炸了", record)
    check("失败结果 final_output 为空", record["final_output"] is None, record)
    check("失败结果保留修正次数", record["corrections_used"] == 1, record)

    def exploding_runner(api=None, raw_sink=None, **kwargs):
        raise RuntimeError("mock boom")

    record = run_experiment.run_one(
        experiment_id="exp-test",
        sample_id="S2",
        group="A",
        repeat_index=1,
        inputs=inputs,
        api=api,
        run_dir=run_dir,
        recorder=recorder,
        sampling=SAMPLING,
        draft_runner=exploding_runner,
    )

    check("异常转为 runner_exception", record["error_type"] == "runner_exception", record)
    check("异常信息保留", "mock boom" in (record["error_message"] or ""), record)


def test_no_research_phase():
    """实验入口不执行研究阶段：run_agent 被换成抛错也不会被调用。"""

    import agent_plan_draft

    called = []
    original_run_agent = agent_plan_draft.agent_loop.run_agent
    original_chat = agent_plan_draft.agent_loop.chat_with_tools

    def fake_run_agent(*args, **kwargs):
        called.append("run_agent")
        raise AssertionError("实验入口不应进入研究阶段")

    def fake_chat(messages, tools=None, deadline=None, sampling=None):
        return {"role": "assistant", "content": "这不是 JSON"}, None

    agent_plan_draft.agent_loop.run_agent = fake_run_agent
    agent_plan_draft.agent_loop.chat_with_tools = fake_chat

    try:
        result = agent_plan_draft.run_plan_draft_from_inputs(
            "给我一份计划",
            steps=[],
            sampling=SAMPLING,
            verbose=False,
        )
    finally:
        agent_plan_draft.agent_loop.run_agent = original_run_agent
        agent_plan_draft.agent_loop.chat_with_tools = original_chat

    check("实验入口不调用 run_agent（不执行研究阶段）", called == [], called)
    check(
        "实验入口只走起草校验链路（validation_error）",
        result["ok"] is False and result["error_type"] == "validation_error",
        result.get("error_type"),
    )


def test_main_aborts_before_model_calls(run_dir):
    """隔离失败必须立即中止，且一次模型请求都不发、不写结果。"""

    original_isolate = run_experiment.isolate_database
    original_chat = llm_client.chat
    chat_calls = []

    def broken_isolate(*args, **kwargs):
        raise RuntimeError("mock isolation failure")

    def fake_chat(*args, **kwargs):
        chat_calls.append(1)
        return None, "不应被调用"

    run_experiment.isolate_database = broken_isolate
    llm_client.chat = fake_chat

    target = Path(run_dir) / "isolation_failure"
    raised = None

    try:
        try:
            run_experiment.main(
                ["--sample", "S2", "--group", "A", "--run-dir", str(target)]
            )
        except RuntimeError as error:
            raised = error
    finally:
        run_experiment.isolate_database = original_isolate
        llm_client.chat = original_chat

    check(
        "隔离失败时主流程抛错中止",
        raised is not None and "mock isolation failure" in str(raised),
        raised,
    )
    check("隔离失败时模型请求为 0", chat_calls == [], len(chat_calls))
    check(
        "隔离失败时不写 results.jsonl",
        not (target / "results.jsonl").exists(),
        str(target),
    )


def test_raw_outputs_chain(run_dir, api, inputs):
    """首轮 + 2 次修正的全部原始输出都要留痕，且顺序稳定。"""

    import agent_plan_draft

    calls = []
    original_chat = agent_plan_draft.agent_loop.chat_with_tools

    def fake_chat_with_tools(messages, tools=None, deadline=None, sampling=None):
        calls.append(1)
        return {"role": "assistant", "content": f"尝试{len(calls)} 不是 JSON"}, None

    agent_plan_draft.agent_loop.chat_with_tools = fake_chat_with_tools

    try:
        record = run_experiment.run_one(
            experiment_id="exp-raw",
            sample_id="S2",
            group="A",
            repeat_index=0,
            inputs=inputs,
            api=api,
            run_dir=run_dir,
            recorder=run_experiment.UsageRecorder(),
            sampling=SAMPLING,
        )
    finally:
        agent_plan_draft.agent_loop.chat_with_tools = original_chat

    expected = [f"尝试{index} 不是 JSON" for index in (1, 2, 3)]

    check("完整起草链：3 次调用", len(calls) == 3, len(calls))
    check("raw_outputs 保留全部 3 条尝试", record.get("raw_outputs") == expected, record.get("raw_outputs"))
    check("raw_outputs 顺序与调用顺序一致", record["raw_outputs"][0] == expected[0] and record["raw_outputs"][-1] == expected[-1])
    check("raw_output 仍代表最后一次尝试", record.get("raw_output") == record["raw_outputs"][-1], record.get("raw_output"))
    check("起草失败 success=False", record["success"] is False, record.get("error_type"))

    def empty_runner(api=None, raw_sink=None, **kwargs):
        return {"ok": False, "error_type": "llm_error", "message": "没有起草", "corrections_used": 0}

    record = run_experiment.run_one(
        experiment_id="exp-raw",
        sample_id="S2",
        group="A",
        repeat_index=1,
        inputs=inputs,
        api=api,
        run_dir=run_dir,
        recorder=run_experiment.UsageRecorder(),
        sampling=SAMPLING,
        draft_runner=empty_runner,
    )

    check("未起草时 raw_outputs 是稳定空列表", record.get("raw_outputs") == [], record.get("raw_outputs"))
    check("未起草时 raw_output 为 None", record.get("raw_output") is None, record.get("raw_output"))


def test_duplicate_identity_guard(run_dir, config):
    """同一 run-dir 里重复记录同一实验身份必须被拒绝，且拒绝发生在任何模型请求之前。"""

    target = Path(run_dir) / "dup_guard"
    target.mkdir(parents=True, exist_ok=True)
    results_path = target / "results.jsonl"
    experiment_id = run_experiment.make_experiment_id(config, target)

    existing = {
        "experiment_id": experiment_id,
        "sample_id": "S5",
        "group": "A",
        "repeat_index": 0,
        "success": True,
        "raw_output": None,
        "raw_outputs": [],
    }
    results_path.write_text(json.dumps(existing, ensure_ascii=False) + "\n", encoding="utf-8")
    before_bytes = results_path.read_bytes()

    original = {
        "isolate": run_experiment.isolate_database,
        "run_one": run_experiment.run_one,
        "chat": llm_client.chat,
        "register": seed_fixture.register_project_modules,
        "project_api": build_prompts.project_api,
        "activate": build_prompts.activate_sample,
        "collect": build_prompts.collect_inputs,
        "load_sample": seed_fixture.load_sample,
        "preflight": run_experiment.preflight_sample,
    }

    isolate_calls = []
    run_calls = []
    chat_calls = []

    def fake_isolate(path):
        isolate_calls.append(path)
        return object(), object()

    def fake_run_one(**kwargs):
        run_calls.append(kwargs)
        return {
            "experiment_id": kwargs["experiment_id"],
            "sample_id": kwargs["sample_id"],
            "group": kwargs["group"],
            "repeat_index": kwargs["repeat_index"],
            "plan_seq": kwargs.get("plan_seq"),
            "round_index": kwargs.get("round_index"),
            "plan_sha256": kwargs.get("plan_sha256"),
            "success": True,
            "request_count": 0,
            "corrections_used": 0,
            "prompt_chars": 0,
            "error_type": None,
            "error_message": None,
        }

    run_experiment.isolate_database = fake_isolate
    run_experiment.run_one = fake_run_one
    llm_client.chat = lambda *args, **kwargs: chat_calls.append(1)
    seed_fixture.register_project_modules = lambda *args, **kwargs: None
    build_prompts.project_api = lambda config=None: {}
    build_prompts.activate_sample = lambda sample_id, config, reuse=False, sample=None: (Path(f"{sample_id}.db"), {})
    build_prompts.collect_inputs = lambda sample, config, api, moment: {}
    seed_fixture.load_sample = lambda sample_id, config: {}
    # 本测试只验证重复身份保护；输入预检由 test_graduation_supplementary.py 覆盖
    run_experiment.preflight_sample = lambda *args, **kwargs: []

    stderr = io.StringIO()
    stdout = io.StringIO()

    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = run_experiment.main(
                ["--sample", "S5", "--group", "A", "--run-dir", str(target)]
            )

        check("重复身份：exit code 4", code == 4, code)
        check(
            "重复身份：0 次数据库隔离、0 次模型请求",
            isolate_calls == [] and run_calls == [] and chat_calls == [],
            (len(isolate_calls), len(run_calls), len(chat_calls)),
        )
        check("legacy：不覆盖、不追加已有结果", results_path.read_bytes() == before_bytes)
        check("legacy：错误信息建议换 --run-dir", "--run-dir" in stderr.getvalue(), stderr.getvalue())
        check(
            "legacy：提示说明缺 plan.json",
            "plan.json" in stderr.getvalue() and "legacy" in stderr.getvalue(),
            stderr.getvalue(),
        )

        # 续跑：已完成任务跳过，不重复追加、不再发请求
        resume_dir = Path(run_dir) / "resume_skip"
        resume_args = [
            "--sample", "S1", "--group", "A", "--repeats", "1",
            "--run-dir", str(resume_dir),
        ]
        run_calls.clear()
        isolate_calls.clear()
        chat_calls.clear()

        with contextlib.redirect_stdout(io.StringIO()):
            first = run_experiment.main(resume_args)

        resume_results = resume_dir / "results.jsonl"

        check("续跑：首跑 exit 0", first == 0, first)
        check("续跑：首跑写入 1 条结果", len(resume_results.read_text(encoding="utf-8").splitlines()) == 1)
        check("续跑：首跑生成 plan.json", (resume_dir / plan_schedule.PLAN_NAME).is_file())

        run_calls.clear()
        isolate_calls.clear()
        chat_calls.clear()

        with contextlib.redirect_stdout(io.StringIO()):
            second = run_experiment.main(resume_args)

        check(
            "续跑：已完成任务全部跳过（exit 0、0 请求）",
            second == 0 and run_calls == [] and isolate_calls == [] and chat_calls == [],
            (second, len(run_calls), len(isolate_calls), len(chat_calls)),
        )
        check(
            "续跑：不重复追加结果",
            len(resume_results.read_text(encoding="utf-8").splitlines()) == 1,
        )

        # 计划被篡改（seed 变）→ fail-closed，0 请求
        plan_path = resume_dir / plan_schedule.PLAN_NAME
        tampered = json.loads(plan_path.read_text(encoding="utf-8"))
        tampered["seed"] = int(tampered["seed"]) + 1
        plan_path.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        run_calls.clear()
        isolate_calls.clear()
        chat_calls.clear()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = run_experiment.main(resume_args)

        check("计划被篡改：exit code 4", code == 4, code)
        check(
            "计划被篡改：0 请求、0 隔离",
            run_calls == [] and isolate_calls == [] and chat_calls == [],
            (len(run_calls), len(isolate_calls), len(chat_calls)),
        )

        # 结果文件损坏时 fail-closed，同样不发请求
        bad_dir = Path(run_dir) / "dup_guard_bad"
        bad_dir.mkdir(parents=True, exist_ok=True)
        bad_results = bad_dir / "results.jsonl"
        bad_results.write_text("{这不是 JSON\n", encoding="utf-8")
        bad_bytes = bad_results.read_bytes()

        run_calls.clear()
        isolate_calls.clear()
        chat_calls.clear()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            code = run_experiment.main(
                ["--sample", "S5", "--group", "A", "--run-dir", str(bad_dir)]
            )

        check("结果文件损坏时拒绝运行（exit 4）", code == 4, code)
        check(
            "结果文件损坏时 0 请求、文件不变",
            run_calls == [] and isolate_calls == [] and chat_calls == [] and bad_results.read_bytes() == bad_bytes,
        )
    finally:
        run_experiment.isolate_database = original["isolate"]
        run_experiment.run_one = original["run_one"]
        llm_client.chat = original["chat"]
        seed_fixture.register_project_modules = original["register"]
        build_prompts.project_api = original["project_api"]
        build_prompts.activate_sample = original["activate"]
        build_prompts.collect_inputs = original["collect"]
        seed_fixture.load_sample = original["load_sample"]
        run_experiment.preflight_sample = original["preflight"]


def test_frozen_abcd_prompt_hashes(config):
    """当前 A/B/C/D Prompt 必须与基线 df4b938 的 28 个哈希逐一相同。"""

    samples = seed_fixture.sample_ids(config)
    mismatches = []
    compared = 0

    check("冻结基线覆盖 S1–S7", sorted(FROZEN_BASELINE_PROMPT_SHA256) == sorted(samples), sorted(FROZEN_BASELINE_PROMPT_SHA256.keys()))

    for sample_id in samples:
        built = build_prompts.build_sample_prompts(sample_id, config, reuse=False)

        for group in ("A", "B", "C", "D"):
            compared += 1
            expected = FROZEN_BASELINE_PROMPT_SHA256[sample_id][group]
            actual = built["prompts"][group]["prompt_sha256"]

            if actual != expected:
                mismatches.append(f"{sample_id}/{group}: {actual} != {expected}")

    check("A/B/C/D 共比较 28 个哈希", compared == 28, compared)
    check("28 个旧版 Prompt 哈希全部一致", mismatches == [], mismatches)

    frozen_groups = {group for entry in FROZEN_BASELINE_PROMPT_SHA256.values() for group in entry}

    check("冻结基线不含 B0（B0 不属于旧版）", frozen_groups == {"A", "B", "C", "D"}, sorted(frozen_groups))


# ---------------------------------------------------------------------------
# 运行入口
# ---------------------------------------------------------------------------


def _run_all():
    before = real_db_fingerprint()
    info("真实 feedback.db 运行前", json.dumps(before, ensure_ascii=False))

    config = seed_fixture.load_config()

    with tempfile.TemporaryDirectory(prefix="graduation_runner_test_") as tmp:
        run_dir = Path(tmp).resolve() / "run"

        # 关键顺序：任何项目模块导入之前先隔离数据库（与 CLI 完全一致）
        database, memory_service = run_experiment.isolate_database(run_dir)

        check(
            "隔离后的 DATABASE_PATH 在运行目录内",
            str(run_dir) in str(Path(database.DATABASE_PATH).resolve()),
            database.DATABASE_PATH,
        )
        check(
            "隔离后的 DATABASE_PATH 不是真实库",
            Path(database.DATABASE_PATH).resolve() != run_experiment.REAL_DB,
            database.DATABASE_PATH,
        )
        check(
            "FEEDBACK_DB_PATH 指向运行目录",
            Path(os.environ["FEEDBACK_DB_PATH"]).resolve().parent == run_dir,
            os.environ["FEEDBACK_DB_PATH"],
        )

        seed_fixture.register_project_modules(
            seed_fixture.workspace_dir(config),
            database,
            memory_service,
            isolation_root=run_dir,
        )

        test_isolation_assertions(run_dir)

        test_cli_defaults()
        test_cli_repeats_bounds()
        test_cli_group_choices()
        test_positive_int_direct()
        test_plan_matrix()
        test_main_dispatch_respects_plan(run_dir)
        test_normalize_sampling()
        test_sampling_in_openai_payload()
        test_sampling_in_ollama_payload()
        test_usage_recorder_gets_sampling()
        test_token_usage_unavailable()
        test_append_jsonl()
        test_write_prompt_file()
        test_prompt_sha256_manual()
        test_slots()
        test_draft_loop_corrections()
        test_no_research_phase()
        test_main_aborts_before_model_calls(run_dir)

        baseline = build_prompts.build_sample_prompts("S2", config, reuse=False)

        check("基线含五个组", sorted(baseline["prompts"]) == ["A", "B", "B0", "C", "D"], sorted(baseline["prompts"]))

        db_path, api = build_prompts.activate_sample("S2", config, reuse=True)
        inputs = build_prompts.collect_inputs(
            seed_fixture.load_sample("S2", config), config, api, seed_fixture.ref_datetime(config)
        )

        test_run_one_end_to_end(run_dir, config, api, inputs, baseline)
        test_run_one_failure_and_exception(run_dir, api, inputs)
        test_raw_outputs_chain(run_dir, api, inputs)
        test_duplicate_identity_guard(run_dir, config)
        test_frozen_abcd_prompt_hashes(config)

    after = real_db_fingerprint()

    check("真实 feedback.db 指纹不变", after == before, f"{before} != {after}")

    print()

    if INFO:
        for line in INFO:
            print(f"INFO  {line}")

    print(f"\n{len(FAILED)} FAILED / {PASSED} PASSED")

    if FAILED:
        for line in FAILED:
            print(f"  - {line}")

        return 1

    print(f"\nALL {PASSED} TESTS COMPLETED")

    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
