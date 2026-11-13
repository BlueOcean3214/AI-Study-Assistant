"""五轮随机区组调度（plan.json）+ 断点续跑的离线测试。

覆盖：
- A. 计划算法：同参数可复现、换 seed 变序、7 样本 × 5 轮的形状、
     身份唯一 / plan_seq 连续、sample_order 可复现、轮内顺序不是固定 ABCD、kind 分类。
- B. 计划文件：原子写 + 读回校准、canonical_json 稳定 key 顺序且不转义非 ASCII、
     篡改后拒绝、原子写失败不留残缺文件、幂等重写。
- C. 执行器：默认 140 任务按 plan.json 顺序执行、结果行带计划审计字段、
     每样本每轮四组各一次、预检只覆盖待执行组且发生在生成之前。
- D. 续跑：中断后只补未完成身份、全部完成后再跑不发请求、
     失败不自动重试、legacy 目录 / 被篡改计划 / config 变化 / 计划外身份 /
     重复成功 / 非法 JSON / plan_sha256 不符 一律 fail-closed 且 0 请求。
- E. 补充实验与 legacy B0：C-subj/D-subj、S7-empty 只进补充计划、B0 在建立
  run-dir 之前就被拒绝、默认计划与结果都没有 B0。

全部离线：monkeypatch 掉 isolate_database / 样本激活 / 输入收集 / 模型调用，
并把 llm_client 的唯一 HTTP 边界替换成"一被调用就抛错"的守卫，
因此任何一个用例都不可能有真实对话模型或 Embedding 请求。
不读写真实 feedback.db，不修改 experiments/graduation/runs 下任何已有文件。
"""

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
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

# 真实 HTTP 边界守卫：任何调用都记下来并直接失败。
_HTTP_CALLS = []
_ORIGINAL_HTTP = None


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


# ---------------------------------------------------------------------------
# 全局守卫与通用工具
# ---------------------------------------------------------------------------


def install_http_guard():
    global _ORIGINAL_HTTP
    _ORIGINAL_HTTP = llm_client._http_post

    def guard(*args, **kwargs):
        _HTTP_CALLS.append((args, kwargs))
        raise AssertionError("离线测试禁止真实 HTTP / 模型请求")

    llm_client._http_post = guard


def restore_http_guard():
    if _ORIGINAL_HTTP is not None:
        llm_client._http_post = _ORIGINAL_HTTP


def tree_snapshot(root):
    """已有 runs 目录的 (相对路径, 大小, mtime_ns) 快照，用来证明没被改动。"""

    root = Path(root)

    if not root.is_dir():
        return {}

    snapshot = {}

    for path in sorted(root.rglob("*")):
        stat = path.stat()

        snapshot[str(path.relative_to(root))] = (path.is_dir(), stat.st_size, stat.st_mtime_ns)

    return snapshot


def read_rows(path):
    path = Path(path)

    if not path.is_file():
        return []

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")


def identity_of(row):
    return (
        str(row["experiment_id"]),
        str(row["sample_id"]),
        str(row["group"]),
        int(row["repeat_index"]),
    )


def identity_of_call(call):
    return (
        str(call["experiment_id"]),
        str(call["sample_id"]),
        str(call["group"]),
        int(call["repeat_index"]),
    )


# ---------------------------------------------------------------------------
# Fake：模型调用 / 数据库隔离 / 样本激活
# ---------------------------------------------------------------------------


class FakeRunner:
    """替代 run_experiment.run_one：不发请求，回填 main 传入的全部计划字段。"""

    def __init__(self):
        self.calls = []
        self.events = []
        self.interrupt_at = None
        self.fail_at = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.events.append(("run", kwargs["sample_id"]))
        index = len(self.calls)

        if self.interrupt_at is not None and index == self.interrupt_at:
            raise KeyboardInterrupt(f"mock interrupt at run_one call {index}")

        success = True
        error_type = None
        error_message = None

        if self.fail_at is not None and index == self.fail_at:
            success = False
            error_type = "mock_error"
            error_message = "mock failure"

        return {
            "experiment_id": kwargs["experiment_id"],
            "sample_id": kwargs["sample_id"],
            "group": kwargs["group"],
            "repeat_index": kwargs["repeat_index"],
            "plan_seq": kwargs.get("plan_seq"),
            "round_index": kwargs.get("round_index"),
            "seed": kwargs.get("seed"),
            "scheme": kwargs.get("scheme"),
            "algorithm_version": kwargs.get("algorithm_version"),
            "plan_sha256": kwargs.get("plan_sha256"),
            "sample_selection": kwargs.get("sample_selection", "default"),
            "b0_sample_override": bool(kwargs.get("b0_sample_override", False)),
            "request_count": 0,
            "corrections_used": 0,
            "prompt_chars": 0,
            "raw_output": None,
            "raw_outputs": [],
            "success": success,
            "error_type": error_type,
            "error_message": error_message,
        }


class Fakes:
    """一次性安装 main 需要的外部边界 fake；退出时（含异常）全部恢复。"""

    def __init__(self, runner, config=None):
        self.runner = runner
        self.config = config
        self.isolate_calls = []
        self.preflight_calls = []
        self._saved = []

    def _isolate(self, path):
        self.isolate_calls.append(Path(path))
        return (object(), object())

    def _preflight(self, sample_id, groups, inputs, api, db_path, config, subject_count=None):
        self.preflight_calls.append((sample_id, list(groups)))
        self.runner.events.append(("preflight", sample_id))
        return []

    def _patch(self, target, name, value):
        self._saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def __enter__(self):
        self._patch(run_experiment, "isolate_database", self._isolate)
        self._patch(seed_fixture, "register_project_modules", lambda *a, **k: None)
        self._patch(build_prompts, "project_api", lambda config=None: {})
        self._patch(
            build_prompts,
            "activate_sample",
            lambda sample_id, config=None, reuse=False, sample=None: (
                Path(tempfile.gettempdir()) / "fake_isolation.db",
                {},
            ),
        )
        self._patch(build_prompts, "collect_inputs", lambda sample, config, api, moment: {})
        self._patch(run_experiment, "preflight_sample", self._preflight)
        self._patch(run_experiment, "run_one", self.runner)

        if self.config is not None:
            self._patch(seed_fixture, "load_config", lambda path=None: self.config)

        return self

    def __exit__(self, exc_type, exc, tb):
        while self._saved:
            target, name, original = self._saved.pop()
            setattr(target, name, original)

        return False


def call_main(argv, runner=None, config=None):
    """调用 main 并收集 stdout/stderr；返回 (code, out, err, fakes)。"""

    runner = runner or FakeRunner()

    with Fakes(runner, config) as fakes:
        out = io.StringIO()
        err = io.StringIO()

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_experiment.main(list(argv))

    return code, out.getvalue(), err.getvalue(), fakes


# ---------------------------------------------------------------------------
# A. 计划算法
# ---------------------------------------------------------------------------


def schedule_params(config, config_hash, seed=None):
    samples = seed_fixture.sample_ids(config)

    return {
        "experiment_id": "graduation-memory-ablation@20261112T210000@test-run",
        "sample_groups": {sample_id: ["A", "B", "C", "D"] for sample_id in samples},
        "rounds": 5,
        "seed": plan_schedule.DEFAULT_SEED if seed is None else seed,
        "scheme": plan_schedule.DEFAULT_SCHEME,
        "algorithm_version": plan_schedule.DEFAULT_ALGORITHM_VERSION,
        "config_hash": config_hash,
        "created_at": "2026-11-13T00:00:00+08:00",
    }


def test_plan_algorithm(config, config_hash):
    params = schedule_params(config, config_hash)
    plan = plan_schedule.build_schedule(**params)

    # ① 同 seed 同 sample_groups 两次构建完全一致（含 dict 插入顺序不同）
    reordered = dict(reversed(list(params["sample_groups"].items())))
    reordered = {key: list(reversed(value)) for key, value in reordered.items()}
    plan_reordered = plan_schedule.build_schedule(**dict(params, sample_groups=reordered))

    check(
        "① 同 seed 两次构建 canonical_json 完全相同",
        plan_schedule.canonical_json(plan) == plan_schedule.canonical_json(plan_reordered),
    )
    check(
        "① 同 seed 两次构建 plan_sha256 相同",
        plan["plan_sha256"] == plan_reordered["plan_sha256"] == plan_schedule.plan_digest(plan),
        (plan["plan_sha256"][:12], plan_reordered["plan_sha256"][:12]),
    )

    # ② 换 seed 后样本区块顺序或轮内顺序至少一个不同
    plan_seed_1 = plan_schedule.build_schedule(**dict(params, seed=1))
    plan_seed_2 = plan_schedule.build_schedule(**dict(params, seed=2))

    def round_orders(target):
        return [
            [task["group"] for task in block["tasks"] if task["round_index"] == round_index]
            for block in target["blocks"]
            for round_index in range(target["round_count"])
        ]

    check(
        "② 换 seed 后 sample_order 或轮内顺序至少一个不同",
        plan_seed_1["sample_order"] != plan_seed_2["sample_order"]
        or round_orders(plan_seed_1) != round_orders(plan_seed_2),
        (plan_seed_1["sample_order"], plan_seed_2["sample_order"]),
    )

    # ③ 默认 7 样本 × 5 轮的形状
    tasks = plan_schedule.plan_tasks(plan)
    check("③ 默认计划 7 个样本区块", len(plan["blocks"]) == 7, len(plan["blocks"]))
    check("③ 默认计划共 140 个任务", len(tasks) == 140, len(tasks))
    check(
        "③ 每个样本 20 个任务（4 组 × 5 轮）",
        all(len(block["tasks"]) == 20 for block in plan["blocks"]),
        [len(block["tasks"]) for block in plan["blocks"]],
    )
    check(
        "③ 每样本每轮 A/B/C/D 各恰好一次",
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
        "③ repeat_index == round_index 且 round_index ∈ 0..4",
        all(task["repeat_index"] == task["round_index"] for task in tasks)
        and all(task["round_index"] in range(5) for task in tasks),
        sorted({(task["round_index"], task["repeat_index"]) for task in tasks}),
    )

    # ④ 四元组身份全局唯一、plan_seq 连续 0..139
    identities = plan_schedule.plan_identities(plan)
    check("④ 四元组身份全局唯一（140 个）", len(identities) == 140, len(identities))
    check(
        "④ plan_seq 连续 0..139 且按执行顺序",
        [task["plan_seq"] for task in tasks] == list(range(140)),
        [task["plan_seq"] for task in tasks][:5],
    )

    # ⑤ sample_order 可复现、不同 seed 有差异
    samples = seed_fixture.sample_ids(config)
    order_a = plan_schedule.order_samples(
        samples, plan_schedule.DEFAULT_SEED, plan_schedule.DEFAULT_ALGORITHM_VERSION
    )
    order_b = plan_schedule.order_samples(
        samples, plan_schedule.DEFAULT_SEED, plan_schedule.DEFAULT_ALGORITHM_VERSION
    )
    order_other = plan_schedule.order_samples(
        samples, 1, plan_schedule.DEFAULT_ALGORITHM_VERSION
    )

    check("⑤ 同 seed 的 sample_order 可复现", order_a == order_b == plan["sample_order"], order_a)
    check("⑤ 不同 seed 的 sample_order 有差异", order_other != order_a, (order_a, order_other))
    check("⑤ sample_order 是全部样本的排列", sorted(order_a) == sorted(samples), order_a)

    # ⑥ 轮内顺序不是固定 ABCD，但每轮仍是四个组
    orders = round_orders(plan)
    check(
        "⑥ 至少一个（样本, 轮次）顺序不是 A/B/C/D",
        any(order != ["A", "B", "C", "D"] for order in orders),
        orders[:3],
    )
    check(
        "⑥ 非 ABCD 顺序的轮次仍是四个不同组",
        all(sorted(order) == ["A", "B", "C", "D"] for order in orders),
    )

    # ⑦ kind 分类
    check(
        "⑦ 主实验 A/B/C/D 全部为 main",
        all(
            plan_schedule.task_kind(sample_id, group) == "main"
            for sample_id in ("S1", "S4", "S6")
            for group in ("A", "B", "C", "D")
        ),
    )
    check(
        "⑦ C-subj / D-subj 为 supplementary",
        plan_schedule.task_kind("S5", "C-subj") == "supplementary"
        and plan_schedule.task_kind("S5", "D-subj") == "supplementary",
    )
    check("⑦ B0 为 b0", plan_schedule.task_kind("S1", "B0") == "b0")
    check(
        "⑦ S7-empty 为 supplementary",
        plan_schedule.task_kind("S7-empty", "D") == "supplementary",
    )
    check(
        "⑦ 默认计划所有任务 kind=main",
        all(task["kind"] == "main" for task in tasks),
        sorted({task["kind"] for task in tasks}),
    )


# ---------------------------------------------------------------------------
# B. 计划文件
# ---------------------------------------------------------------------------


def test_plan_file(root, config, config_hash):
    params = schedule_params(config, config_hash)
    plan = plan_schedule.build_schedule(**params)
    directory = Path(root) / "plan_files"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / plan_schedule.PLAN_NAME

    # ⑧ 原子写 + 读回校准
    returned = plan_schedule.write_plan_atomic(path, plan)
    loaded = plan_schedule.load_plan(path)

    check("⑧ write_plan_atomic 返回目标路径", Path(returned) == path, returned)
    check("⑧ load_plan 成功且 plan_sha256 与计划一致", loaded["plan_sha256"] == plan_schedule.plan_digest(plan) == plan["plan_sha256"])
    check(
        "⑧ 读回的计划内容等价",
        plan_schedule.canonical_json(loaded) == plan_schedule.canonical_json(plan),
    )

    # ⑨ canonical_json 稳定 key 顺序、不转义非 ASCII
    first = plan_schedule.canonical_json({"b": 1, "a": 2, "note": "中文记忆"})
    second = plan_schedule.canonical_json({"note": "中文记忆", "a": 2, "b": 1})

    check("⑨ 同一 dict 不同插入顺序得到同一字符串", first == second, (first, second))
    check("⑨ canonical_json 不转义非 ASCII", "中文记忆" in first and "\\u4e2d" not in first, first)

    # ⑩ 篡改任意字段后拒绝
    tampered = copy.deepcopy(plan)
    task = tampered["blocks"][0]["tasks"][0]
    task["group"] = "D" if task["group"] != "D" else "A"

    raised = None

    try:
        plan_schedule.validate_plan(
            tampered,
            experiment_id=params["experiment_id"],
            config_hash=params["config_hash"],
            seed=params["seed"],
            scheme=params["scheme"],
            algorithm_version=params["algorithm_version"],
            rounds=params["rounds"],
            sample_groups=params["sample_groups"],
        )
    except plan_schedule.PlanIntegrityError as error:
        raised = error

    check("⑩ 改掉任务 group 后 validate_plan 抛 PlanIntegrityError", raised is not None, "未抛错")

    tampered_path = directory / "tampered.json"
    tampered_path.write_text(
        json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    raised_load = None

    try:
        plan_schedule.load_plan(tampered_path)
    except plan_schedule.PlanIntegrityError as error:
        raised_load = error

    check("⑩ 篡改后的文件 load_plan 校验失败", raised_load is not None, "未抛错")

    # 重算摘要后仍然被语义校验拒绝（防止只改摘要绕过）
    redigested = copy.deepcopy(tampered)
    redigested["plan_sha256"] = plan_schedule.plan_digest(redigested)
    raised_redigest = None

    try:
        plan_schedule.validate_plan(
            redigested,
            experiment_id=params["experiment_id"],
            config_hash=params["config_hash"],
            seed=params["seed"],
            scheme=params["scheme"],
            algorithm_version=params["algorithm_version"],
            rounds=params["rounds"],
            sample_groups=params["sample_groups"],
        )
    except plan_schedule.PlanIntegrityError as error:
        raised_redigest = error

    check(
        "⑩ 重算 plan_sha256 后语义校验仍拒绝（每轮组集合不符）",
        raised_redigest is not None,
        "未抛错",
    )

    seed_tampered = copy.deepcopy(plan)
    seed_tampered["seed"] = int(plan["seed"]) + 1
    raised_seed = None

    try:
        plan_schedule.load_plan(
            _write_temp_plan(directory, "seed_tampered.json", seed_tampered)
        )
    except plan_schedule.PlanIntegrityError as error:
        raised_seed = error

    check("⑩ 改 seed 后 load_plan 拒绝", raised_seed is not None, "未抛错")

    # ⑪ 原子写失败：不改目标、不留 tmp
    fail_dir = Path(root) / "plan_write_failure"
    fail_dir.mkdir(parents=True, exist_ok=True)
    fail_path = fail_dir / plan_schedule.PLAN_NAME
    original_os = plan_schedule.os
    failure = None

    class FailingReplaceOs:
        def __getattr__(self, name):
            return getattr(original_os, name)

        def replace(self, *args, **kwargs):
            raise OSError("mock os.replace failure")

    plan_schedule.os = FailingReplaceOs()

    try:
        plan_schedule.write_plan_atomic(fail_path, plan)
    except OSError as error:
        failure = error
    finally:
        plan_schedule.os = original_os

    leftovers = [entry.name for entry in fail_dir.iterdir() if entry.name.endswith(".tmp")]

    check("⑪ os.replace 失败时 write_plan_atomic 抛 OSError", isinstance(failure, OSError), failure)
    check("⑪ 原子写失败后目标文件不存在", not fail_path.exists())
    check("⑪ 原子写失败后目录里没有残留 tmp 文件", leftovers == [], leftovers)

    # ⑫ 幂等：已存在正确计划时再次写入 / 校验仍通过；换 seed 校验失败
    plan_schedule.write_plan_atomic(path, plan)
    again = plan_schedule.load_plan(path)
    validated = None

    try:
        validated = plan_schedule.validate_plan(
            again,
            experiment_id=params["experiment_id"],
            config_hash=params["config_hash"],
            seed=params["seed"],
            scheme=params["scheme"],
            algorithm_version=params["algorithm_version"],
            rounds=params["rounds"],
            sample_groups=params["sample_groups"],
        )
    except plan_schedule.PlanIntegrityError as error:  # pragma: no cover - 失败即测试失败
        validated = error

    check("⑫ 幂等重写后 plan_sha256 不变", again["plan_sha256"] == plan["plan_sha256"])
    check(
        "⑫ 同参数 validate_plan 通过",
        validated == again and validated["plan_sha256"] == plan["plan_sha256"],
        validated if not isinstance(validated, dict) else "ok",
    )

    other_seed_plan = plan_schedule.build_schedule(**dict(params, seed=int(params["seed"]) + 1))
    rejected = None

    try:
        plan_schedule.validate_plan(
            other_seed_plan,
            experiment_id=params["experiment_id"],
            config_hash=params["config_hash"],
            seed=params["seed"],
            scheme=params["scheme"],
            algorithm_version=params["algorithm_version"],
            rounds=params["rounds"],
            sample_groups=params["sample_groups"],
        )
    except plan_schedule.PlanIntegrityError as error:
        rejected = error

    check("⑫ 不同 seed 的计划用原参数校验失败", rejected is not None, "未抛错")


def _write_temp_plan(directory, name, plan):
    path = Path(directory) / name
    path.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    return path


# ---------------------------------------------------------------------------
# C. 执行器默认运行
# ---------------------------------------------------------------------------


def test_default_run(root):
    run_dir = Path(root) / "default_run"
    runner = FakeRunner()
    code, out, err, fakes = call_main(["--run-dir", str(run_dir)], runner)
    plan = plan_schedule.load_plan(run_dir / plan_schedule.PLAN_NAME)
    ordered = [
        (block["sample_id"], task) for block in plan["blocks"] for task in block["tasks"]
    ]
    dispatched = [
        (call["sample_id"], call["group"], call["repeat_index"], call["plan_seq"], call["round_index"])
        for call in runner.calls
    ]
    expected = [
        (sample_id, task["group"], task["repeat_index"], task["plan_seq"], task["round_index"])
        for sample_id, task in ordered
    ]

    # ⑬ 默认运行：exit 0、140 行、顺序 == plan.json、140 次 run_one、0 请求
    check("⑬ 默认运行 exit code 0", code == 0, (code, err[-400:]))
    check("⑬ 默认运行执行 140 次 run_one", len(runner.calls) == 140, len(runner.calls))
    check("⑬ 执行顺序与 plan.json 完全一致", dispatched == expected, dispatched[:5])
    check(
        "⑬ 执行顺序的 plan_seq 严格递增",
        [item[3] for item in dispatched] == sorted(item[3] for item in dispatched)
        and len({item[3] for item in dispatched}) == 140,
    )

    rows = read_rows(run_dir / run_experiment.RESULTS_NAME)

    check("⑬ 默认运行写入 140 行结果", len(rows) == 140, len(rows))
    check(
        "⑬ 结果顺序与计划顺序一致",
        [row["plan_seq"] for row in rows] == [task["plan_seq"] for _, task in ordered],
        [row["plan_seq"] for row in rows][:6],
    )
    check(
        "⑬ 全部结果 request_count=0（0 次真实 llm 调用）",
        all(row["request_count"] == 0 for row in rows),
        sorted({row["request_count"] for row in rows}),
    )

    # ⑭ 结果行带计划审计字段且与计划一致
    field_ok = all(
        row["plan_seq"] == task["plan_seq"]
        and row["round_index"] == task["round_index"]
        and row["seed"] == plan["seed"]
        and row["scheme"] == plan["scheme"]
        and row["algorithm_version"] == plan["algorithm_version"]
        and row["plan_sha256"] == plan["plan_sha256"]
        for row, (_, task) in zip(rows, ordered)
    )

    check("⑭ 结果行含 plan_seq/round_index/seed/scheme/algorithm_version/plan_sha256", field_ok)
    check(
        "⑭ 结果行身份与计划四元组一致",
        [(row["sample_id"], row["group"], row["repeat_index"]) for row in rows]
        == [(sample_id, task["group"], task["repeat_index"]) for sample_id, task in ordered],
    )

    # ⑮ 每样本 5 轮、repeat_index 0..4、每轮四组各一次
    by_sample = {}

    for row in rows:
        by_sample.setdefault(row["sample_id"], []).append(row)

    check(
        "⑮ 每个样本 5 轮 repeat_index 恰为 0..4",
        all(sorted({row["repeat_index"] for row in sample_rows}) == [0, 1, 2, 3, 4] for sample_rows in by_sample.values()),
        {sample_id: sorted({row["repeat_index"] for row in sample_rows}) for sample_id, sample_rows in by_sample.items()},
    )
    check(
        "⑮ 每个样本每轮四组各一次",
        all(
            sorted(
                row["group"]
                for row in sample_rows
                if row["round_index"] == round_index
            )
            == ["A", "B", "C", "D"]
            for sample_rows in by_sample.values()
            for round_index in range(5)
        ),
    )

    # ⑯ 预检只针对待执行组、且在该样本第一次 run_one 之前
    expected_preflight = [
        (block["sample_id"], sorted({task["group"] for task in block["tasks"]}))
        for block in plan["blocks"]
    ]
    check(
        "⑯ 每个样本预检一次且只覆盖待执行组",
        [(sample_id, sorted(groups)) for sample_id, groups in fakes.preflight_calls]
        == expected_preflight,
        fakes.preflight_calls,
    )

    first_run = {}

    for index, (kind, sample_id) in enumerate(runner.events):
        if kind == "run":
            first_run.setdefault(sample_id, index)

    check(
        "⑯ 预检发生在该样本第一次 run_one 之前",
        all(
            runner.events.index(("preflight", sample_id)) < index
            for sample_id, index in first_run.items()
        ),
        runner.events[:6],
    )

    # ㉚ 默认计划与结果都不含 B0
    check(
        "㉚ 默认计划不含 B0 任务",
        all(task["group"] != "B0" for _, task in ordered),
        sorted({task["group"] for _, task in ordered}),
    )
    check("㉚ 默认结果不含 B0 行", all(row["group"] != "B0" for row in rows))

    return run_dir


# ---------------------------------------------------------------------------
# D. 断点续跑
# ---------------------------------------------------------------------------


def test_resume_after_interrupt(root):
    run_dir = Path(root) / "resume_interrupt"
    results_path = run_dir / run_experiment.RESULTS_NAME

    # ⑰ 第 4 次 run_one 抛 KeyboardInterrupt → 只留前 3 行；再跑只补 17 次
    interrupted_runner = FakeRunner()
    interrupted_runner.interrupt_at = 4
    interrupted = False

    try:
        call_main(["--sample", "S1", "--run-dir", str(run_dir)], interrupted_runner)
    except KeyboardInterrupt:
        interrupted = True

    plan = plan_schedule.load_plan(run_dir / plan_schedule.PLAN_NAME)
    rows_after_interrupt = read_rows(results_path)
    ordered = [
        (block["sample_id"], task) for block in plan["blocks"] for task in block["tasks"]
    ]

    check("⑰ 中断确实抛出 KeyboardInterrupt", interrupted)
    check("⑰ 中断后 results.jsonl 只有前 3 行", len(rows_after_interrupt) == 3, len(rows_after_interrupt))
    check("⑰ 中断后 plan.json 仍在", (run_dir / plan_schedule.PLAN_NAME).is_file())
    check("⑰ 单样本计划共 20 个任务", len(ordered) == 20, len(ordered))

    completed_before = {identity_of(row) for row in rows_after_interrupt}
    resumed_runner = FakeRunner()
    code, out, err, fakes = call_main(["--sample", "S1", "--run-dir", str(run_dir)], resumed_runner)
    rows_after_resume = read_rows(results_path)

    check("⑰ 续跑 exit code 0", code == 0, (code, err[-300:]))
    check("⑰ 续跑只新增 17 次 run_one", len(resumed_runner.calls) == 17, len(resumed_runner.calls))
    check(
        "⑰ 已完成的 3 个身份没有被重跑",
        completed_before.isdisjoint({identity_of_call(call) for call in resumed_runner.calls}),
        sorted(completed_before),
    )
    check("⑰ 续跑后共 20 行结果", len(rows_after_resume) == 20, len(rows_after_resume))
    check(
        "⑰ 续跑结果身份无重复且与计划一致",
        len({identity_of(row) for row in rows_after_resume}) == 20
        and {identity_of(row) for row in rows_after_resume}
        == {
            (plan["experiment_id"], sample_id, task["group"], task["repeat_index"])
            for sample_id, task in ordered
        },
    )

    # ⑱ 全部完成后再跑：0 次 run_one / isolate / 结果行数不变
    idle_runner = FakeRunner()
    code2, out2, err2, fakes2 = call_main(["--sample", "S1", "--run-dir", str(run_dir)], idle_runner)

    check("⑱ 全部完成后再次运行 exit code 0", code2 == 0, (code2, err2[-300:]))
    check("⑱ 全部完成后不再调用 run_one", idle_runner.calls == [], len(idle_runner.calls))
    check("⑱ 全部完成后不再调用 isolate_database", fakes2.isolate_calls == [], fakes2.isolate_calls)
    check("⑱ 结果行数不变（20）", len(read_rows(results_path)) == 20)
    check(
        "⑱ 输出提示已完成 / 已有成功记录",
        ("已完成" in out2) or ("已有成功记录" in out2),
        out2[-300:],
    )

    if "已完成" not in out2:
        info(
            "⑱ 文案",
            "main 完成分支实际输出「全部 N 个任务都已有成功记录，本次不发起任何模型请求。」，"
            "未出现字面「已完成」；语义等价，按「已有成功记录」判通过。",
        )


def test_failure_not_retried(root):
    run_dir = Path(root) / "failure_no_retry"
    results_path = run_dir / run_experiment.RESULTS_NAME

    # ⑲ 失败不自动重试
    failing_runner = FakeRunner()
    failing_runner.fail_at = 1
    code, out, err, fakes = call_main(
        ["--sample", "S1", "--group", "A", "--run-dir", str(run_dir)], failing_runner
    )
    rows = read_rows(results_path)

    check("⑲ 失败运行 exit code 1", code == 1, (code, out[-300:]))
    check("⑲ 失败行已保留", len(rows) == 1 and rows[0]["success"] is False, rows)
    check(
        "⑲ 失败行记录 error_type=mock_error",
        rows and rows[0]["error_type"] == "mock_error",
        rows[0]["error_type"] if rows else None,
    )
    check("⑲ stderr 说明不自动重试", "不自动重试" in err, err[-300:])

    retry_runner = FakeRunner()
    code2, out2, err2, fakes2 = call_main(
        ["--sample", "S1", "--group", "A", "--run-dir", str(run_dir)], retry_runner
    )

    check("⑲ 再次运行同一 run-dir 仍 exit code 1", code2 == 1, (code2, out2[-300:]))
    check("⑲ 再次运行不自动重试（0 次 run_one）", retry_runner.calls == [], len(retry_runner.calls))
    check("⑲ 提前拒绝，0 次 isolate_database", fakes2.isolate_calls == [], fakes2.isolate_calls)
    check(
        "⑲ stderr 提到失败身份与不自动重试",
        "sample=S1" in err2 and "group=A" in err2 and "repeat=0" in err2 and "不自动重试" in err2,
        err2[-400:],
    )


def test_legacy_directory(root):
    # ⑳ 只有 results.jsonl、没有 plan.json 的 legacy 目录
    run_dir = Path(root) / "legacy_dir"
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / run_experiment.RESULTS_NAME
    legacy_rows = [
        {
            "experiment_id": "graduation-memory-ablation@20261112T210000@legacy_dir",
            "sample_id": "S1",
            "group": "A",
            "repeat_index": 0,
            "success": True,
            "plan_sha256": None,
        }
    ]
    write_rows(results_path, legacy_rows)
    before = results_path.read_bytes()

    runner = FakeRunner()
    code, out, err, fakes = call_main(["--sample", "S1", "--run-dir", str(run_dir)], runner)

    check("⑳ legacy 目录 exit code 4", code == 4, (code, out[-300:], err[-300:]))
    check("⑳ legacy 目录不执行任何 run_one", runner.calls == [], len(runner.calls))
    check("⑳ legacy 目录 results.jsonl 字节不变", results_path.read_bytes() == before)
    check(
        "⑳ stderr 提到 plan.json / legacy",
        "plan.json" in err and "legacy" in err,
        err[-400:],
    )
    check("⑳ legacy 拒绝时 0 次 isolate_database", fakes.isolate_calls == [], fakes.isolate_calls)


def test_tampered_plan(root, base_run):
    # ㉑ 计划被篡改（seed / 任务 group）→ exit 4、0 请求
    seed_dir = shutil.copytree(base_run, Path(root) / "tamper_seed" / base_run.name)
    seed_plan_path = seed_dir / plan_schedule.PLAN_NAME
    plan = json.loads(seed_plan_path.read_text(encoding="utf-8"))
    plan["seed"] = int(plan["seed"]) + 1
    seed_plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    seed_results = (seed_dir / run_experiment.RESULTS_NAME).read_bytes()

    runner = FakeRunner()
    code, out, err, fakes = call_main(
        ["--sample", "S1", "--group", "A", "--run-dir", str(seed_dir)], runner
    )

    check("㉑ 篡改 seed 后 exit code 4", code == 4, (code, out[-300:], err[-300:]))
    check("㉑ 篡改 seed 后 0 次 run_one", runner.calls == [], len(runner.calls))
    check("㉑ 篡改 seed 后 0 次 isolate_database", fakes.isolate_calls == [], fakes.isolate_calls)
    check("㉑ results.jsonl 未被改写", (seed_dir / run_experiment.RESULTS_NAME).read_bytes() == seed_results)
    check("㉑ stderr 指出 plan.json 校验失败", "plan.json" in err and "plan_sha256" in err, err[-300:])

    group_dir = shutil.copytree(base_run, Path(root) / "tamper_group" / base_run.name)
    group_plan_path = group_dir / plan_schedule.PLAN_NAME
    plan = json.loads(group_plan_path.read_text(encoding="utf-8"))
    task = plan["blocks"][0]["tasks"][0]
    task["group"] = "D" if task["group"] != "D" else "A"
    group_plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    runner2 = FakeRunner()
    code2, out2, err2, fakes2 = call_main(
        ["--sample", "S1", "--group", "A", "--run-dir", str(group_dir)], runner2
    )

    check("㉑ 篡改任务 group 后 exit code 4", code2 == 4, (code2, err2[-300:]))
    check("㉑ 篡改任务 group 后 0 次 run_one", runner2.calls == [], len(runner2.calls))


def test_config_change(root, config, base_run):
    # ㉒ randomization.seed 变化 → 计划与当前 config 不一致 → exit 4
    run_dir = shutil.copytree(base_run, Path(root) / "config_seed" / base_run.name)
    changed = copy.deepcopy(config)
    changed["randomization"]["seed"] = int(config["randomization"]["seed"]) + 1
    results_before = (run_dir / run_experiment.RESULTS_NAME).read_bytes()

    runner = FakeRunner()
    code, out, err, fakes = call_main(
        ["--sample", "S1", "--group", "A", "--run-dir", str(run_dir)],
        runner,
        config=changed,
    )

    check("㉒ config.seed 变化后 exit code 4", code == 4, (code, out[-300:], err[-300:]))
    check("㉒ config.seed 变化后 0 次 run_one", runner.calls == [], len(runner.calls))
    check("㉒ config.seed 变化后 0 次 isolate_database", fakes.isolate_calls == [], fakes.isolate_calls)
    check("㉒ results.jsonl 未被改写", (run_dir / run_experiment.RESULTS_NAME).read_bytes() == results_before)
    check("㉒ stderr 指出与当前运行不一致", "不一致" in err, err[-300:])


def test_foreign_and_broken_results(root, base_run):
    # 从该 run-dir 的 plan.json 取 plan_sha256 / experiment_id 来伪造结果行
    for case_name, mutate, needle in (
        ("foreign_identity", "foreign", "计划外身份"),
        ("duplicate_success", "duplicate", "重复记录了同一成功身份"),
        ("invalid_json", "invalid", "不是合法 JSON"),
        ("digest_mismatch", "digest", "plan_sha256"),
    ):
        run_dir = shutil.copytree(base_run, Path(root) / case_name / base_run.name)
        results_path = run_dir / run_experiment.RESULTS_NAME
        plan = plan_schedule.load_plan(run_dir / plan_schedule.PLAN_NAME)
        rows = read_rows(results_path)
        first = copy.deepcopy(rows[0])
        first["plan_sha256"] = plan["plan_sha256"]

        if mutate == "foreign":
            first["sample_id"] = "S9"
            first["experiment_id"] = plan["experiment_id"]
            with results_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n")
        elif mutate == "duplicate":
            with results_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n")
        elif mutate == "invalid":
            with results_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("{this is not json\n")
        else:
            first["plan_sha256"] = "0" * 64
            with results_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n")

        before = results_path.read_bytes()
        runner = FakeRunner()
        code, out, err, fakes = call_main(
            ["--sample", "S1", "--group", "A", "--run-dir", str(run_dir)], runner
        )

        check(f"{needle} → exit code 4", code == 4, (code, out[-200:], err[-300:]))
        check(f"{needle} → 0 次 run_one", runner.calls == [], len(runner.calls))
        check(f"{needle} → 0 次 isolate_database", fakes.isolate_calls == [], fakes.isolate_calls)
        check(f"{needle} → results.jsonl 字节不变", results_path.read_bytes() == before)
        check(f"{needle} → stderr 命中关键提示", needle in err, err[-400:])


# ---------------------------------------------------------------------------
# E. 补充实验与 legacy B0
# ---------------------------------------------------------------------------


def test_supplementary_runs(root):
    # ㉗ C-subj / D-subj：10 任务、全部 supplementary、每轮两组各一次
    run_dir = Path(root) / "supplementary_two_groups"
    runner = FakeRunner()
    code, out, err, fakes = call_main(
        [
            "--sample",
            "S5",
            "--group",
            "C-subj",
            "--group",
            "D-subj",
            "--run-dir",
            str(run_dir),
        ],
        runner,
    )
    plan = plan_schedule.load_plan(run_dir / plan_schedule.PLAN_NAME)
    tasks = plan_schedule.plan_tasks(plan)

    check("㉗ C-subj/D-subj 运行 exit code 0", code == 0, (code, err[-300:]))
    check("㉗ 计划共 10 个任务（2 组 × 5 轮）", len(tasks) == 10, len(tasks))
    check("㉗ 全部任务 kind=supplementary", all(task["kind"] == "supplementary" for task in tasks))
    check(
        "㉗ 每轮 C-subj / D-subj 各一次",
        all(
            sorted(task["group"] for task in tasks if task["round_index"] == round_index)
            == ["C-subj", "D-subj"]
            for round_index in range(5)
        ),
        [task["group"] for task in tasks],
    )
    check("㉗ 结果 10 行", len(read_rows(run_dir / run_experiment.RESULTS_NAME)) == 10)
    check(
        "㉗ 预检只覆盖 C-subj/D-subj",
        [sorted(groups) for _, groups in fakes.preflight_calls] == [["C-subj", "D-subj"]],
        fakes.preflight_calls,
    )

    # ㉘ S7-empty + D：5 任务、round_index == repeat_index ∈ 0..4
    empty_dir = Path(root) / "empty_sample_d"
    empty_runner = FakeRunner()
    code2, out2, err2, fakes2 = call_main(
        ["--sample", "S7-empty", "--group", "D", "--run-dir", str(empty_dir)],
        empty_runner,
    )
    empty_plan = plan_schedule.load_plan(empty_dir / plan_schedule.PLAN_NAME)
    empty_tasks = plan_schedule.plan_tasks(empty_plan)

    check("㉘ S7-empty/D 运行 exit code 0", code2 == 0, (code2, err2[-300:]))
    check("㉘ S7-empty/D 计划 5 个任务", len(empty_tasks) == 5, len(empty_tasks))
    check(
        "㉘ round_index == repeat_index ∈ 0..4",
        sorted(task["round_index"] for task in empty_tasks) == [0, 1, 2, 3, 4]
        and all(task["repeat_index"] == task["round_index"] for task in empty_tasks),
        [(task["round_index"], task["repeat_index"]) for task in empty_tasks],
    )
    check(
        "㉘ S7-empty 任务 kind=supplementary",
        all(task["kind"] == "supplementary" for task in empty_tasks),
    )
    check("㉘ S7-empty/D 结果 5 行", len(read_rows(empty_dir / run_experiment.RESULTS_NAME)) == 5)


def test_b0_rejected_before_run_dir(root):
    # ㉙ 只传 --group B0（以及 S1+B0）在建立 run-dir / 任何请求之前 exit 2
    for label, argv, dir_name in (
        ("只传 --group B0", ["--group", "B0"], "b0_only"),
        ("--sample S1 --group B0", ["--sample", "S1", "--group", "B0"], "b0_s1"),
    ):
        run_dir = Path(root) / dir_name
        runner = FakeRunner()
        code, out, err, fakes = call_main(argv + ["--run-dir", str(run_dir)], runner)

        check(f"㉙ {label} → exit code 2", code == 2, (code, out[-200:], err[-300:]))
        check(f"㉙ {label} → run_dir 未被建立", not run_dir.exists(), str(run_dir))
        check(f"㉙ {label} → 0 次 run_one", runner.calls == [], len(runner.calls))
        check(f"㉙ {label} → 0 次 isolate_database", fakes.isolate_calls == [], fakes.isolate_calls)
        check(
            f"㉙ {label} → stderr 说明 B0 是 legacy 组",
            "B0" in err and "legacy" in err,
            err[-300:],
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def make_base_run(root):
    """建立一个合法的小 run-dir（S1 单组 A，5 个任务），供复制后做篡改类用例。"""

    base = Path(root) / "base" / "S1A"
    runner = FakeRunner()
    code, out, err, fakes = call_main(
        ["--sample", "S1", "--group", "A", "--run-dir", str(base)], runner
    )

    return base, code, len(read_rows(base / run_experiment.RESULTS_NAME))


def _run_all():
    global PASSED

    root = Path(tempfile.mkdtemp(prefix="graduation_randomized_block_")).resolve()
    runs_dir = GRADUATION_DIR / "runs"
    runs_before = tree_snapshot(runs_dir)
    config = seed_fixture.load_config()
    config_hash = plan_schedule.config_sha256(config)
    usage_sentinel = getattr(llm_client, "_usage_recorder", None)

    print(f"temp root = {root}")

    install_http_guard()

    try:
        test_plan_algorithm(config, config_hash)
        test_plan_file(root, config, config_hash)
        test_default_run(root)

        base_run, base_code, base_rows = make_base_run(root)

        check("准备：用作复制基础的小 run-dir 正常完成", base_code == 0 and base_rows == 5, (base_code, base_rows))

        test_resume_after_interrupt(root)
        test_failure_not_retried(root)
        test_legacy_directory(root)
        test_tampered_plan(root, base_run)
        test_config_change(root, config, base_run)
        test_foreign_and_broken_results(root, base_run)
        test_supplementary_runs(root)
        test_b0_rejected_before_run_dir(root)
    finally:
        restore_http_guard()
        llm_client._usage_recorder = usage_sentinel

    check("全局：全程 0 次真实 HTTP / 模型请求", _HTTP_CALLS == [], len(_HTTP_CALLS))
    check("全局：runs 目录里的已有文件未被改动", tree_snapshot(runs_dir) == runs_before)

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
