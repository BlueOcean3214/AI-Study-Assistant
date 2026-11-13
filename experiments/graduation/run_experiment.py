"""毕业设计实验执行层：A/B/B0/C/D 五组 × 样本 × 重复次数的离线可复现运行器。

设计边界（见 docs/GRADUATION-EXPERIMENT-DESIGN.md）：

- 输入固定：反馈、短期上下文、长期记忆由 build_prompts.group_inputs() 决定并逐字节
  与 build_prompts 基线一致；知识库全组为空、不调用 Embedding、不做向量检索。
- 生成链路复用产品：agent_plan_draft.run_plan_draft_from_inputs()（内部仍是
  _run_draft_loop：JSON 解析、normalize_plan、validate_plan_for_save、
  check_grounding、MAX_CORRECTIONS=2），不新建第二套生成/校验流程。
- 数据库隔离：模型调用前把 FEEDBACK_DB_PATH 指向运行目录内的临时库并断言
  database.DATABASE_PATH 就在运行目录里、且绝不等于真实 feedback.db。
- 用量记录：只用 llm_client.set_usage_recorder 装进程内 recorder，不落库
  （不为采样参数新增数据库字段/表）。
- 结果只追加：results.jsonl 逐行 append，Prompt 文本按样本/组只写一次，
  已存在且内容不同时拒绝覆盖；同一 run-dir 里同一实验身份（experiment_id +
  样本 + 组 + 重复序号）已存在时，在任何模型请求之前拒绝运行。
- 样本调度按 (样本, 组) 任务对：B0 默认只跑 S5、S7，其他组跑全部样本；
  显式 --sample 的覆盖语义逐条记进结果（sample_selection / b0_sample_override）。

本模块不发起任何真实请求；是否真的调用模型由 CLI 使用者的环境配置决定。
"""

import argparse
import importlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_prompts  # noqa: E402
import seed_fixture  # noqa: E402

REAL_DB = (seed_fixture.PROJECT_ROOT / "feedback.db").resolve()
ISOLATION_DB_NAME = "_experiment_isolation.db"
RESULTS_NAME = "results.jsonl"

# B0 是探索组：设计 §1.3 只在 S5、S7 上各跑 5 次；显式 --sample 可覆盖。
B0_DEFAULT_SAMPLES = ("S5", "S7")

DEFAULT_SAMPLING = {"temperature": 0.7, "top_p": 1.0, "max_tokens": 2048}


# ---------------------------------------------------------------------------
# 数据库隔离
# ---------------------------------------------------------------------------


def assert_isolated(run_dir, db_path):
    """断言 db_path 位于 run_dir 内且不是真实 feedback.db，返回解析后的路径。"""

    run_dir = Path(run_dir).resolve()
    resolved = Path(db_path).resolve()

    if resolved == REAL_DB:
        raise RuntimeError(f"拒绝在真实数据库上运行实验：{resolved}")

    if run_dir not in resolved.parents:
        raise RuntimeError(
            f"数据库路径未被隔离，拒绝继续：{resolved} 不在 {run_dir} 之内"
        )

    return resolved


def isolate_database(run_dir):
    """在任何模型调用之前完成隔离导入，返回 (database, memory_service)。

    必须在导入 database 之前设置 FEEDBACK_DB_PATH；导入后再做运行时断言，
    不依赖文档约定。调用方随后应把结果登记到 seed_fixture（见 main）。
    """

    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    db_path = run_dir / ISOLATION_DB_NAME
    os.environ["FEEDBACK_DB_PATH"] = str(db_path)

    if str(seed_fixture.PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(seed_fixture.PROJECT_ROOT))

    database = importlib.import_module("database")
    memory_service = importlib.import_module("memory_service")

    assert_isolated(run_dir, database.DATABASE_PATH)

    return database, memory_service


# ---------------------------------------------------------------------------
# 用量记录（进程内，不落库）
# ---------------------------------------------------------------------------


class UsageRecorder:
    """llm_client 的 recorder：逐条保留每次请求的实际 provider/model/采样/token。"""

    def __init__(self):
        self.requests = []

    def __call__(self, record):
        self.requests.append(
            {
                "provider": record.get("provider"),
                "model": record.get("model"),
                "temperature": record.get("temperature"),
                "top_p": record.get("top_p"),
                "max_tokens": record.get("max_tokens"),
                "prompt_tokens": record.get("prompt_tokens"),
                "completion_tokens": record.get("completion_tokens"),
                "total_tokens": record.get("total_tokens"),
                "usage_available": bool(record.get("usage_available")),
                "elapsed_seconds": record.get("elapsed_seconds"),
                "ok": bool(record.get("ok")),
                "error_type": record.get("error_type"),
            }
        )

    def since(self, index):
        return self.requests[index:]


def token_usage(requests):
    """汇总 token 用量；任一请求缺失计数就整体标记 unavailable，绝不做估算。"""

    if not requests:
        return {
            "available": False,
            "status": "unavailable",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }

    complete = [
        item
        for item in requests
        if item.get("usage_available")
        and isinstance(item.get("prompt_tokens"), int)
        and isinstance(item.get("completion_tokens"), int)
    ]

    if len(complete) != len(requests):
        return {
            "available": False,
            "status": "unavailable",
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }

    prompt_tokens = sum(item["prompt_tokens"] for item in complete)
    completion_tokens = sum(item["completion_tokens"] for item in complete)

    return {
        "available": True,
        "status": "available",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# ---------------------------------------------------------------------------
# 追加写入
# ---------------------------------------------------------------------------


def append_jsonl(path, record):
    """追加一行 JSONL；永不截断已有结果。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(record, ensure_ascii=False, sort_keys=True)

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    return path


def write_prompt_file(run_dir, sample_id, group, system, user):
    """把该样本/组的 Prompt 落盘一次，返回相对运行目录的引用。

    已存在且内容不同即拒绝覆盖：Prompt 是实验输入的一部分，不能被静默改写。
    """

    run_dir = Path(run_dir).resolve()
    path = run_dir / "prompts" / str(sample_id) / f"{group}.txt"
    text = (
        "===== SYSTEM =====\n"
        + system
        + "\n\n===== USER =====\n"
        + user
        + "\n"
    )

    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"已存在的 Prompt 文件与本次构建不一致，拒绝覆盖：{path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    return str(path.relative_to(run_dir)).replace("\\", "/")


# ---------------------------------------------------------------------------
# 输入 / evidence
# ---------------------------------------------------------------------------


def evidence_steps(group, inputs):
    """合成 check_grounding 用的 evidence（本实验不调用任何工具）。

    - 注入反馈的组：{"tool": "get_recent_feedback", "ok": True, "result": feedback_result}
    - 带长期记忆的组：{"tool": "get_learning_memory", "ok": True, "result": memory_tool}
    - 知识库一律不合成：本实验没有知识库结果，也不调用 Embedding。
    """

    slots = build_prompts.GROUP_SLOTS[group]
    steps = []

    if slots["feedback"]:
        feedback_result = build_prompts.group_inputs(group, inputs)["feedback_result"]
        steps.append(
            {
                "type": "tool_call",
                "tool": "get_recent_feedback",
                "arguments": {"days": int(inputs["feedback_result"].get("days") or 0)},
                "ok": True,
                "error_type": None,
                "result": feedback_result,
            }
        )

    if slots["memory"]:
        steps.append(
            {
                "type": "tool_call",
                "tool": "get_learning_memory",
                "arguments": {},
                "ok": True,
                "error_type": None,
                "result": inputs["memory_tool"],
            }
        )

    return steps


def resolve_plan(args, config):
    """把 CLI 展开成 (样本, 组) 任务对，B0 的默认样本范围独立于其他组。

    - 不给 --sample：A/B/C/D × 全部样本，B0 × 仅 B0_DEFAULT_SAMPLES（S5、S7）；
    - --group B0：只有 B0 × S5、S7；
    - --group A --group B0：A × 全部样本、B0 × S5、S7；
    - 显式 --sample：样本范围由用户决定，所有选中组都在这些样本上跑；
      该覆盖逐条记进结果元数据（sample_selection / b0_sample_override）。

    任务对而不是「一个全局样本列表」：否则混合组时 B0 会被扩张到全部样本。
    """

    groups = list(args.group or build_prompts.GROUP_SLOTS)
    explicit = bool(args.sample)
    samples = list(args.sample) if explicit else seed_fixture.sample_ids(config)

    plan = []

    for sample_id in samples:
        for group in groups:
            if not explicit and group == "B0" and sample_id not in B0_DEFAULT_SAMPLES:
                continue

            plan.append(
                {
                    "sample_id": sample_id,
                    "group": group,
                    "sample_selection": "explicit" if explicit else "default",
                    "b0_sample_override": bool(
                        explicit and group == "B0" and sample_id not in B0_DEFAULT_SAMPLES
                    ),
                }
            )

    return plan


# ---------------------------------------------------------------------------
# 单次运行
# ---------------------------------------------------------------------------


def _default_draft_runner(api, **kwargs):
    return api["agent_plan_draft"].run_plan_draft_from_inputs(**kwargs)


def run_one(
    *,
    experiment_id,
    sample_id,
    group,
    repeat_index,
    inputs,
    api,
    run_dir,
    recorder,
    sampling=None,
    sample_selection="default",
    b0_sample_override=False,
    draft_runner=None,
    now=None,
):
    """跑一个 (样本, 组, 重复) 并返回一条待追加的结果记录（不写文件）。"""

    run_dir = Path(run_dir).resolve()
    sampling = dict(sampling or DEFAULT_SAMPLING)

    resolved_db = Path(api["database"].DATABASE_PATH).resolve()

    if resolved_db == REAL_DB:
        raise RuntimeError(f"拒绝在真实数据库上运行实验：{resolved_db}")

    slots = build_prompts.group_inputs(group, inputs)
    content = build_prompts.build_group_content(group, inputs, api)
    system = api["agent_plan_draft"].DRAFT_SYSTEM_PROMPT
    prompt_ref = write_prompt_file(run_dir, sample_id, group, system, content)

    steps = evidence_steps(group, inputs)
    raw_sink = []
    before = len(recorder.requests)
    started = time.perf_counter()
    result = None
    failure = None

    runner = draft_runner or _default_draft_runner

    try:
        result = runner(
            api=api,
            user_message=inputs["user_message"],
            feedback_result=slots["feedback_result"],
            knowledge_results=inputs["knowledge_results"],
            search_failed=inputs["search_failed"],
            difficult=slots["difficult"],
            short_term_block=slots["short_term_block"],
            memory_result=slots["memory_result"],
            steps=steps,
            sampling=sampling,
            verbose=False,
            raw_sink=raw_sink,
        )
    except Exception as error:  # noqa: BLE001 - 单次失败必须留痕，不能中断整批
        failure = error
        result = {
            "ok": False,
            "error_type": "runner_exception",
            "message": f"{type(error).__name__}: {error}",
            "corrections_used": 0,
        }

    elapsed = round(time.perf_counter() - started, 3)
    requests = recorder.since(before)
    last = requests[-1] if requests else {}

    success = bool(result.get("ok"))

    record = {
        "experiment_id": experiment_id,
        "sample_id": sample_id,
        "group": group,
        "repeat_index": repeat_index,
        "provider": last.get("provider"),
        "model": last.get("model"),
        "temperature": last.get("temperature", sampling.get("temperature")),
        "top_p": last.get("top_p", sampling.get("top_p")),
        "max_tokens": last.get("max_tokens", sampling.get("max_tokens")),
        "prompt_sha256": build_prompts.prompt_sha256(system, content),
        "prompt_ref": prompt_ref,
        "prompt_chars": len(content),
        "request_count": len(requests),
        "requests": requests,
        "corrections_used": result.get("corrections_used", 0),
        # raw_output：最后一次起草尝试（保留旧语义，含 result 的兜底字段）；
        # raw_outputs：本次 run 的全部起草尝试，按发生顺序，未起草时为 []。
        "raw_output": raw_sink[-1] if raw_sink else result.get("last_draft_raw"),
        "raw_outputs": list(raw_sink),
        "sample_selection": sample_selection,
        "b0_sample_override": bool(b0_sample_override),
        "final_output": (
            {"answer": result.get("answer"), "plan": result.get("plan")}
            if success
            else None
        ),
        "token_usage": token_usage(requests),
        "elapsed_seconds": elapsed,
        "generated_at": (now or datetime.now(timezone.utc).astimezone()).isoformat(
            timespec="seconds"
        ),
        "success": success,
        "error_type": None if success else (result.get("error_type") or "unknown"),
        "error_message": None
        if success
        else (result.get("message") or (str(failure) if failure else None)),
        "stopped_reason": result.get("stopped_reason"),
    }

    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"必须是整数：{value!r}")

    if number < 1:
        raise argparse.ArgumentTypeError("--repeats 必须 >= 1")

    if number > 50:
        raise argparse.ArgumentTypeError("--repeats 不能超过 50")

    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="毕业设计记忆消融实验执行器（A/B/B0/C/D）"
    )
    parser.add_argument("--sample", action="append", help="只跑指定样本，可重复")
    parser.add_argument(
        "--group",
        action="append",
        choices=tuple(build_prompts.GROUP_SLOTS),
        help="只跑指定组，可重复；默认全部",
    )
    parser.add_argument(
        "--repeats",
        type=positive_int,
        default=1,
        help="每组重复次数（默认 1；正式实验 5）",
    )
    parser.add_argument("--run-dir", help="结果目录；默认 runs/<ref>-<UTC 时间戳>")
    parser.add_argument(
        "--reuse", action="store_true", help="复用已建好的样本库（先做完整性校验）"
    )
    parser.add_argument("--config", help="config.json 路径")

    return parser.parse_args(argv)


def default_run_dir(config):
    ref = seed_fixture.ref_datetime(config)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return HERE / "runs" / f"{ref.strftime('%Y%m%dT%H%M%S')}-{stamp}"


def make_experiment_id(config, run_dir):
    ref = seed_fixture.ref_datetime(config)
    name = config.get("experiment") or "graduation-experiment"

    return f"{name}@{ref.strftime('%Y%m%dT%H%M%S')}@{Path(run_dir).name}"


# ---------------------------------------------------------------------------
# 重复实验身份保护
# ---------------------------------------------------------------------------


class IdentityConflictError(RuntimeError):
    """已有结果无法解析，或本次计划与已有实验身份冲突。"""


def identity_key(experiment_id, sample_id, group, repeat_index):
    return (str(experiment_id), str(sample_id), str(group), int(repeat_index))


def read_existing_identities(results_path):
    """读取已有 results.jsonl 的 (experiment_id, sample_id, group, repeat_index)。"""

    path = Path(results_path)

    if not path.is_file():
        return set()

    keys = set()

    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        text = line.strip()

        if not text:
            continue

        try:
            row = json.loads(text)
        except json.JSONDecodeError as error:
            raise IdentityConflictError(
                f"{path} 第 {line_number} 行不是合法 JSON，拒绝在该目录继续追加：{error}"
            ) from error

        missing = [
            field
            for field in ("experiment_id", "sample_id", "group", "repeat_index")
            if row.get(field) is None
        ]

        if missing:
            raise IdentityConflictError(
                f"{path} 第 {line_number} 行缺少实验身份字段 {missing}，无法判断是否重复"
            )

        keys.add(
            identity_key(
                row["experiment_id"],
                row["sample_id"],
                row["group"],
                row["repeat_index"],
            )
        )

    return keys


def find_identity_conflicts(plan, experiment_id, results_path, repeats):
    """返回与已有结果冲突的 (experiment_id, sample_id, group, repeat_index)。"""

    existing = read_existing_identities(results_path)
    conflicts = []

    for task in plan:
        for repeat_index in range(repeats):
            key = identity_key(
                experiment_id, task["sample_id"], task["group"], repeat_index
            )

            if key in existing:
                conflicts.append(key)

    return conflicts


def describe_plan(plan):
    """按组汇总计划里的样本，用于运行前打印。"""

    grouped = {}

    for task in plan:
        grouped.setdefault(task["group"], []).append(task["sample_id"])

    return "  ".join(
        f"{group}={','.join(samples)}" for group, samples in grouped.items()
    )


def main(argv=None):
    args = parse_args(argv)
    config = seed_fixture.load_config(args.config)

    plan = resolve_plan(args, config)

    if not plan:
        print("没有可运行的任务（B0 只在 S5、S7 上有默认样本，请显式指定 --sample）")
        return 2

    run_dir = (
        Path(args.run_dir).expanduser().resolve()
        if args.run_dir
        else default_run_dir(config)
    )
    results_path = run_dir / RESULTS_NAME
    experiment_id = make_experiment_id(config, run_dir)

    # 2) 重复实验身份保护：任何模型请求、任何文件写入之前先拒绝
    try:
        conflicts = find_identity_conflicts(
            plan, experiment_id, results_path, args.repeats
        )
    except IdentityConflictError as error:
        print(f"拒绝运行：{error}", file=sys.stderr)
        return 4

    if conflicts:
        print(
            "拒绝运行：本次计划与已有结果身份重复 "
            f"{len(conflicts)} 条（experiment_id={experiment_id}）。",
            file=sys.stderr,
        )

        for key in conflicts[:5]:
            print(f"  sample={key[1]} group={key[2]} repeat={key[3]}", file=sys.stderr)

        if len(conflicts) > 5:
            print(f"  … 另有 {len(conflicts) - 5} 条", file=sys.stderr)

        print(
            "同一 run-dir 里不允许重复记录同一实验身份；请改用新的 --run-dir。",
            file=sys.stderr,
        )

        return 4

    run_dir.mkdir(parents=True, exist_ok=True)

    sampling = dict(config.get("sampling") or DEFAULT_SAMPLING)

    # 3) 模型调用之前完成数据库隔离，并把模块登记给 seed_fixture（避免二次导入）
    database, memory_service = isolate_database(run_dir)
    seed_fixture.register_project_modules(
        seed_fixture.workspace_dir(config),
        database,
        memory_service,
        isolation_root=run_dir,
    )

    llm_client = importlib.import_module("llm_client")
    recorder = UsageRecorder()
    llm_client.set_usage_recorder(recorder)

    print(f"experiment_id = {experiment_id}")
    print(f"run_dir       = {run_dir}")
    print(f"plan          = {describe_plan(plan)}  repeats={args.repeats}")
    print(f"sampling      = {sampling}")

    order = []
    tasks_by_sample = {}

    for task in plan:
        if task["sample_id"] not in tasks_by_sample:
            tasks_by_sample[task["sample_id"]] = []
            order.append(task["sample_id"])

        tasks_by_sample[task["sample_id"]].append(task)

    failures = 0
    total = 0

    for sample_id in order:
        api = build_prompts.project_api(config)
        sample = seed_fixture.load_sample(sample_id, config)
        db_path, api = build_prompts.activate_sample(
            sample_id, config, reuse=args.reuse, sample=sample
        )
        moment = seed_fixture.ref_datetime(config)
        inputs = build_prompts.collect_inputs(sample, config, api, moment)

        for task in tasks_by_sample[sample_id]:
            for repeat_index in range(args.repeats):
                record = run_one(
                    experiment_id=experiment_id,
                    sample_id=sample_id,
                    group=task["group"],
                    repeat_index=repeat_index,
                    inputs=inputs,
                    api=api,
                    run_dir=run_dir,
                    recorder=recorder,
                    sampling=sampling,
                    sample_selection=task["sample_selection"],
                    b0_sample_override=task["b0_sample_override"],
                )
                append_jsonl(results_path, record)

                total += 1
                failures += 0 if record["success"] else 1

                print(
                    f"  {sample_id}/{task['group']}/#{repeat_index}: "
                    f"success={record['success']} "
                    f"requests={record['request_count']} "
                    f"corrections={record['corrections_used']} "
                    f"chars={record['prompt_chars']} "
                    f"db={db_path.name}"
                )

                if not record["success"]:
                    print(f"    error: {record['error_type']} - {record['error_message']}")

    print(f"results = {results_path}")
    print(f"total={total} failures={failures}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
