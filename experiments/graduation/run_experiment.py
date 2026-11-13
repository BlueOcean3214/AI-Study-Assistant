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
  已存在且内容不同时拒绝覆盖。
- 调度按五轮随机区组计划：每个 run-dir 先由 plan_schedule.build_schedule()
  生成 plan.json（原子写），执行只读计划、不重算顺序；每个样本跑完 5 轮再进
  下一个样本，每轮 A/B/C/D 各一次、轮内顺序确定性随机，repeat_index == round_index。
- 断点续跑：已有成功记录的任务跳过；同一身份已有失败记录时不自动重试，
  停止并报告等人工处理；无记录的任务按 plan_seq 继续。
- 样本调度按 (样本, 组) 任务对：B0 默认只跑 S5、S7，但 B0 是 legacy 探索组，
  已有 10 份结果，不进入五轮计划、不重跑；显式 --sample 的覆盖语义逐条记进
  结果（sample_selection / b0_sample_override）。

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
import plan_schedule  # noqa: E402
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
        if slots.get("memory_scope") == "subject":
            # 补充组：evidence 必须与真正注入 Prompt 的是同一次主题过滤调用，
            # 并把 subject / limit 原样记进 arguments，便于事后复算。
            tool_result = inputs["subject_memory_tool"]
            arguments = {
                "subject": inputs["subject_filter"],
                "limit": int(inputs["memory_limit"]),
            }
        else:
            tool_result = inputs["memory_tool"]
            arguments = {}

        steps.append(
            {
                "type": "tool_call",
                "tool": "get_learning_memory",
                "arguments": arguments,
                "ok": bool(tool_result.get("ok")),
                "error_type": tool_result.get("error_type"),
                "result": tool_result,
            }
        )

    return steps


class PreflightError(RuntimeError):
    """输入预检失败；必须在任何模型请求之前抛出（fail-closed）。"""


def preflight_sample(
    sample_id,
    groups,
    inputs,
    api,
    db_path,
    config,
    subject_count=None,
):
    """当前样本所有待执行组在第一次模型调用之前的输入预检（fail-closed）。

    只对补充组加硬性规则，不改 A/B/C/D 的既有语义：

    - C-subj / D-subj：主题过滤值非空、主题记忆 Tool 成功、条数自洽且非空、
      每条记忆都属于当前主题、与库内 active 行数一致、Prompt 真的带记忆区块、
      evidence 用的是同一次调用与同一组参数；
    - S7-empty 的 D：记忆 Tool 成功但严格 0 条、Prompt 不得出现记忆区块、
      evidence 记录这次成功的空结果。

    任何一项失败抛 PreflightError；调用方必须据此停止生成，
    不得把无记忆结果记作 C-subj/D-subj。
    """

    groups = list(groups)
    checks = []
    problems = []

    def record(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

        if not ok:
            problems.append(f"{name}（{detail}）")

    def memory_step(group):
        steps = [
            step
            for step in evidence_steps(group, inputs)
            if step["tool"] == "get_learning_memory"
        ]

        return steps[0] if len(steps) == 1 else None

    contents = {
        group: build_prompts.build_group_content(group, inputs, api) for group in groups
    }

    memory_service = api.get("memory_service")

    def normalized_subject_of(memory):
        """记忆里存的是原始任务文本；用产品真实 normalize_subject() 归一再比对。"""

        subject_text = memory.get("subject") if isinstance(memory, dict) else None

        if memory_service is None:
            return subject_text

        key, _label = memory_service.normalize_subject(subject_text or "")

        return key

    for group in groups:
        slots = build_prompts.GROUP_SLOTS[group]
        content = contents[group]
        has_block = build_prompts.MEMORY_HEADER in content
        step = memory_step(group)

        if slots.get("memory_scope") == "subject":
            tool = inputs.get("subject_memory_tool")
            result = inputs.get("subject_memory_result") or {}
            subject_key = inputs.get("subject_filter")
            memories = list(result.get("memories") or [])
            count = result.get("count")
            expected = (
                subject_count(subject_key)
                if subject_count is not None
                else build_prompts.active_subject_count(db_path, subject_key)
            )

            record(
                f"{group}·主题过滤值非空",
                bool(subject_key),
                f"subject={subject_key!r}",
            )
            record(
                f"{group}·主题记忆 Tool 成功",
                isinstance(tool, dict) and tool.get("ok") is True,
                f"tool={type(tool).__name__} ok={tool.get('ok') if isinstance(tool, dict) else None}",
            )
            record(
                f"{group}·Tool 条数自洽",
                isinstance(tool, dict)
                and tool.get("count") == len(tool.get("memories") or []),
                f"count={tool.get('count') if isinstance(tool, dict) else None} "
                f"memories={len(tool.get('memories') or []) if isinstance(tool, dict) else 0}",
            )
            record(
                f"{group}·记忆结果与 Tool 一致",
                isinstance(tool, dict)
                and tool.get("count") == count
                and count == len(memories),
                f"tool={tool.get('count') if isinstance(tool, dict) else None} "
                f"result={count} memories={len(memories)}",
            )
            record(
                f"{group}·主题记忆非空",
                isinstance(count, int) and count > 0 and bool(memories),
                f"count={count}",
            )
            record(
                f"{group}·每条记忆都属于当前主题",
                bool(memories)
                and subject_key is not None
                and all(
                    normalized_subject_of(item) == subject_key for item in memories
                ),
                f"subjects={sorted({str(normalized_subject_of(item)) for item in memories})}",
            )
            record(
                f"{group}·记忆条数与库内 active 一致",
                isinstance(count, int) and count == expected and count > 0,
                f"tool={count} db={expected}",
            )
            record(
                f"{group}·Prompt 含主题记忆区块",
                has_block and content.count(build_prompts.MEMORY_HEADER) == 1,
                f"blocks={content.count(build_prompts.MEMORY_HEADER)}",
            )
            record(
                f"{group}·evidence 用同一次主题 Tool",
                step is not None and step.get("result") is tool and step.get("ok") is True,
                f"step={'有' if step is not None else '缺失'}",
            )
            record(
                f"{group}·evidence 参数一致",
                step is not None
                and step.get("arguments")
                == {"subject": subject_key, "limit": int(inputs["memory_limit"])},
                f"arguments={step.get('arguments') if step is not None else None}",
            )

        elif sample_id == build_prompts.EMPTY_SAMPLE_ID:
            # S7-empty：记忆槽位启用，但可注入记忆必须严格为 0（不是 Tool 失败）。
            tool = inputs.get("memory_tool")
            count = tool.get("count") if isinstance(tool, dict) else None
            memories = list(tool.get("memories") or []) if isinstance(tool, dict) else None

            record(
                f"{group}·空样本记忆 Tool 成功",
                isinstance(tool, dict) and tool.get("ok") is True,
                f"tool={type(tool).__name__} ok={tool.get('ok') if isinstance(tool, dict) else None}",
            )
            record(
                f"{group}·空样本记忆条数为 0",
                count == 0,
                f"count={count}",
            )
            record(
                f"{group}·空样本 memories 为空",
                memories == [],
                f"memories={memories}",
            )
            record(
                f"{group}·空样本 Prompt 无记忆区块",
                not has_block,
                f"blocks={content.count(build_prompts.MEMORY_HEADER)}",
            )
            record(
                f"{group}·空样本 evidence 记录成功空结果",
                step is not None
                and step.get("ok") is True
                and step.get("result") is tool
                and (step.get("result") or {}).get("count") == 0,
                f"step={'有' if step is not None else '缺失'}",
            )

    if problems:
        raise PreflightError(
            f"{sample_id} 输入预检失败，未发起任何模型请求：\n  - "
            + "\n  - ".join(problems)
        )

    return checks


class PlanError(RuntimeError):
    """非法的 (样本, 组) 组合；必须在创建目录与调用模型之前拒绝。"""


def resolve_plan(args, config):
    """把 CLI 展开成 (样本, 组) 任务对，B0 的默认样本范围独立于其他组。

    - 不给 --sample：A/B/B0/C/D × 全部样本，B0 × 仅 B0_DEFAULT_SAMPLES（S5、S7）；
      补充组 C-subj/D-subj 与 S7-empty 默认绝不进入计划。
    - --group B0：只有 B0 × S5、S7；
    - --group A --group B0：A × 全部样本、B0 × S5、S7；
    - --group C-subj --group D-subj：只允许 S5（--sample S5 或默认样本里的 S5）；
    - --sample S7-empty --group D：只允许 D，且必须显式同时给出样本与组；
    - 显式 --sample：样本范围由用户决定，所有选中组都在这些样本上跑；
      该覆盖逐条记进结果元数据（sample_selection / b0_sample_override）。

    任务对而不是「一个全局样本列表」：否则混合组时 B0 会被扩张到全部样本。
    """

    groups = list(args.group or build_prompts.DEFAULT_GROUPS)
    explicit = bool(args.sample)
    samples = list(args.sample) if explicit else seed_fixture.sample_ids(config)

    for sample_id in samples:
        try:
            seed_fixture.load_sample(sample_id, config)
        except FileNotFoundError as error:
            raise PlanError(f"样本文件不存在：{sample_id}") from error

    plan = []

    def add(sample_id, group):
        if group not in build_prompts.GROUP_SLOTS:
            raise PlanError(f"未知的组：{group}")

        if group in build_prompts.SUBJECT_GROUPS:
            allowed = build_prompts.SUBJECT_GROUP_SAMPLES[group]
            if sample_id not in allowed:
                raise PlanError(
                    f"{group} 只允许在 {'/'.join(allowed)} 上执行，收到 {sample_id}"
                )

        if sample_id == build_prompts.EMPTY_SAMPLE_ID:
            if group not in build_prompts.EMPTY_SAMPLE_GROUPS:
                raise PlanError(
                    f"{sample_id} 只允许 {'/'.join(build_prompts.EMPTY_SAMPLE_GROUPS)} 组，"
                    f"收到 {group}；请使用 --sample {sample_id} "
                    f"--group {' --group '.join(build_prompts.EMPTY_SAMPLE_GROUPS)}"
                )

            if not explicit:
                # 默认矩阵永远不包含 S7-empty，避免它混进主实验
                return

        if not explicit and group == "B0" and sample_id not in B0_DEFAULT_SAMPLES:
            return

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

    # 当前默认顺序：A/B/C/D 逐样本排完，再把 B0 排在最后
    # （DEFAULT_GROUPS = A/B/C/D/B0，即新版默认顺序）。
    # 注意这与 main 基线 2d9b3be 的 GROUP_SLOTS（A/B/B0/C/D）不同：
    # 旧版把 B0 夹在 B 与 C 之间，所以结果文件里的任务对顺序已经变化，
    # 旧结果只能按 (sample, group, repeat) 身份逐条对照，不能依赖行序。
    for sample_id in samples:
        for group in groups:
            if group != "B0":
                add(sample_id, group)

    for sample_id in samples:
        for group in groups:
            if group == "B0":
                add(sample_id, group)

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
    plan_seq=None,
    round_index=None,
    seed=None,
    scheme=None,
    algorithm_version=None,
    plan_sha256=None,
):
    """跑一个 (样本, 组, 重复) 并返回一条待追加的结果记录（不写文件）。

    plan_seq / round_index / seed / scheme / algorithm_version / plan_sha256 由
    调度器从 plan.json 传入，只作顺序与计划审计；实验身份仍是
    (experiment_id, sample_id, group, repeat_index)。
    """

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
        # 计划审计字段：plan_seq 只用于顺序核对，不进入实验身份。
        "plan_seq": plan_seq,
        "round_index": repeat_index if round_index is None else round_index,
        "seed": seed,
        "scheme": scheme,
        "algorithm_version": algorithm_version,
        "plan_sha256": plan_sha256,
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
        default=None,
        help="显式覆盖轮次数（默认取 config.randomization.round_count=5）",
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
    """已有结果无法解析，或与当前计划不一致（计划外身份、重复成功等）。"""


def identity_key(experiment_id, sample_id, group, repeat_index):
    """实验身份仍是四元组；plan_seq / round_index 只作顺序审计。"""

    return (str(experiment_id), str(sample_id), str(group), int(repeat_index))


def scheduled_tasks(plan):
    """按 plan_seq 顺序展开 [(sample_id, task)]，执行器只读这份顺序。"""

    return [
        (block["sample_id"], task)
        for block in plan["blocks"]
        for task in block["tasks"]
    ]


def task_key(plan, sample_id, task):
    return identity_key(
        plan["experiment_id"], sample_id, task["group"], task["repeat_index"]
    )


def read_result_rows(results_path):
    """读取已有 results.jsonl，返回 [(行号, row)]；任何非法行都 fail-closed。"""

    path = Path(results_path)

    if not path.is_file():
        return []

    rows = []

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

        if not isinstance(row, dict):
            raise IdentityConflictError(f"{path} 第 {line_number} 行不是 JSON 对象")

        missing = [
            field
            for field in ("experiment_id", "sample_id", "group", "repeat_index")
            if row.get(field) is None
        ]

        if missing:
            raise IdentityConflictError(
                f"{path} 第 {line_number} 行缺少实验身份字段 {missing}，无法判断是否重复"
            )

        if not isinstance(row.get("success"), bool):
            raise IdentityConflictError(
                f"{path} 第 {line_number} 行缺少布尔 success 字段，无法判断是否已完成"
            )

        rows.append((line_number, row))

    return rows


def resume_state(plan, results_path):
    """按 plan.json 校验已有结果，返回 (successes, failures)。

    - 计划外身份、plan_sha256 不一致、同一身份重复成功、同一身份既有成功又有
      失败：一律抛 IdentityConflictError（调用方在任何模型请求之前拒绝）。
    - 失败记录原样保留在 failures，不自动重试。
    """

    planned = {
        task_key(plan, sample_id, task) for sample_id, task in scheduled_tasks(plan)
    }
    digest = plan["plan_sha256"]
    successes = set()
    failures = {}

    for line_number, row in read_result_rows(results_path):
        key = identity_key(
            row["experiment_id"], row["sample_id"], row["group"], row["repeat_index"]
        )

        if key not in planned:
            raise IdentityConflictError(
                f"{results_path} 第 {line_number} 行是计划外身份 "
                f"sample={row['sample_id']} group={row['group']} "
                f"repeat={row['repeat_index']} experiment_id={row['experiment_id']}；"
                "拒绝在同一 run-dir 里混入别的实验或别的计划。"
            )

        if row.get("plan_sha256") != digest:
            raise IdentityConflictError(
                f"{results_path} 第 {line_number} 行的 plan_sha256 与当前 plan.json 不一致，"
                "拒绝续跑（计划被替换或结果来自旧计划）。"
            )

        if row["success"]:
            if key in successes:
                raise IdentityConflictError(
                    f"{results_path} 第 {line_number} 行重复记录了同一成功身份 "
                    f"sample={row['sample_id']} group={row['group']} "
                    f"repeat={row['repeat_index']}。"
                )

            successes.add(key)
        else:
            if key in successes:
                raise IdentityConflictError(
                    f"{results_path} 第 {line_number} 行：同一身份既有成功又有失败记录，"
                    "无法判断应否重跑。"
                )

            failures[key] = row

    return successes, failures


class RandomizationError(RuntimeError):
    """config.json 的 randomization 块缺失或非法（fail-closed）。"""


def randomization_settings(config):
    """读取 config.randomization；缺字段或非法值一律拒绝，不猜默认值。"""

    block = config.get("randomization")

    if not isinstance(block, dict):
        raise RandomizationError(
            "config.json 缺少 randomization 块：五轮随机区组计划需要 "
            "seed / scheme / algorithm_version / block_unit / round_count"
        )

    missing = [
        field
        for field in ("seed", "scheme", "algorithm_version", "block_unit", "round_count")
        if field not in block
    ]

    if missing:
        raise RandomizationError(f"config.json 的 randomization 块缺少字段：{missing}")

    try:
        seed = int(block["seed"])
        round_count = int(block["round_count"])
    except (TypeError, ValueError) as error:
        raise RandomizationError(
            f"randomization.seed / round_count 必须是整数：{block!r}"
        ) from error

    if round_count < 1:
        raise RandomizationError(
            f"randomization.round_count 必须 >= 1，收到 {round_count!r}"
        )

    if block["block_unit"] != plan_schedule.BLOCK_UNIT:
        raise RandomizationError(
            f"randomization.block_unit 必须是 {plan_schedule.BLOCK_UNIT!r}，"
            f"收到 {block['block_unit']!r}"
        )

    return {
        "seed": seed,
        "scheme": str(block["scheme"]),
        "algorithm_version": str(block["algorithm_version"]),
        "round_count": round_count,
    }


def build_plan_schedule(base_plan, settings, rounds, experiment_id, config_hash):
    """把 resolve_plan 的任务对展开成五轮随机区组计划。

    B0 是 legacy 探索组（已有 10 份结果在 experiments/graduation/runs/），
    不进入五轮计划、不重跑；显式 --group B0 只跑 B0 时直接拒绝。
    """

    sample_groups = {}
    task_meta = {}
    skipped_b0 = []

    for task in base_plan:
        if task["group"] == plan_schedule.B0_GROUP:
            skipped_b0.append(task["sample_id"])
            continue

        sample_groups.setdefault(task["sample_id"], []).append(task["group"])
        task_meta[(task["sample_id"], task["group"])] = {
            "sample_selection": task["sample_selection"],
            "b0_sample_override": task["b0_sample_override"],
        }

    if not sample_groups:
        raise RandomizationError(
            "拒绝运行：B0 是 legacy 探索组，已有 10 份结果，不进入五轮随机区组计划，"
            "也不会被重跑；请显式指定 A/B/C/D（或 C-subj/D-subj、S7-empty）组合。"
        )

    schedule = plan_schedule.build_schedule(
        experiment_id=experiment_id,
        sample_groups=sample_groups,
        rounds=rounds,
        seed=settings["seed"],
        scheme=settings["scheme"],
        algorithm_version=settings["algorithm_version"],
        config_hash=config_hash,
        task_meta=task_meta,
    )

    return schedule, skipped_b0


def describe_schedule(plan):
    """按样本区块汇总计划里的组，用于运行前打印。"""

    parts = []

    for block in plan["blocks"]:
        groups = []

        for task in block["tasks"]:
            if task["group"] not in groups:
                groups.append(task["group"])

        parts.append(f"{block['sample_id']}={','.join(groups)}")

    return "  ".join(parts)


def main(argv=None):
    args = parse_args(argv)
    config = seed_fixture.load_config(args.config)

    try:
        base_plan = resolve_plan(args, config)
    except PlanError as error:
        print(f"拒绝运行：{error}", file=sys.stderr)
        return 2

    if not base_plan:
        print("没有可运行的任务（B0 只在 S5、S7 上有默认样本，请显式指定 --sample）")
        return 2

    try:
        settings = randomization_settings(config)
    except RandomizationError as error:
        print(f"拒绝运行：{error}", file=sys.stderr)
        return 2

    rounds = settings["round_count"] if args.repeats is None else args.repeats

    run_dir = (
        Path(args.run_dir).expanduser().resolve()
        if args.run_dir
        else default_run_dir(config)
    )
    results_path = run_dir / RESULTS_NAME
    plan_path = run_dir / plan_schedule.PLAN_NAME
    experiment_id = make_experiment_id(config, run_dir)
    config_hash = plan_schedule.config_sha256(config)

    try:
        schedule, skipped_b0 = build_plan_schedule(
            base_plan, settings, rounds, experiment_id, config_hash
        )
    except RandomizationError as error:
        print(f"拒绝运行：{error}", file=sys.stderr)
        return 2

    # 计划先于数据库隔离与任何模型请求：存在则校验，缺失则原子写一份。
    # 有结果却没有 plan.json 的目录是 legacy（v4.1 及更早），拒绝续跑。
    try:
        if plan_path.is_file():
            plan = plan_schedule.load_plan(plan_path)
            plan_schedule.validate_plan(
                plan,
                experiment_id=experiment_id,
                config_hash=config_hash,
                seed=settings["seed"],
                scheme=settings["scheme"],
                algorithm_version=settings["algorithm_version"],
                rounds=rounds,
                sample_groups={
                    block["sample_id"]: sorted(
                        {task["group"] for task in block["tasks"]}
                    )
                    for block in schedule["blocks"]
                },
            )

            # 计划是唯一顺序依据，但它必须与"同 seed 的确定性推导"逐项一致：
            # 这样即使有人重写了 plan.json 并重算了 plan_sha256，也骗不过去。
            def _task_signature(block_list):
                return [
                    (
                        block["sample_id"],
                        task["plan_seq"],
                        task["round_index"],
                        task["repeat_index"],
                        task["group"],
                        task["kind"],
                        task["sample_selection"],
                        task["b0_sample_override"],
                    )
                    for block in block_list
                    for task in block["tasks"]
                ]

            if _task_signature(plan["blocks"]) != _task_signature(schedule["blocks"]):
                raise plan_schedule.PlanIntegrityError(
                    f"{plan_path} 的任务顺序与同 seed/算法版本推导出的计划不一致，"
                    "拒绝续跑（不重洗、不覆盖）。"
                )
        else:
            if results_path.is_file() and results_path.stat().st_size > 0:
                raise plan_schedule.PlanIntegrityError(
                    f"{run_dir} 已有 results.jsonl 但没有 plan.json（legacy 目录），"
                    "拒绝续跑；请换新的 --run-dir，或按 docs 的 legacy 说明人工归档。"
                )

            plan_schedule.write_plan_atomic(plan_path, schedule)
            plan = schedule
    except (plan_schedule.PlanIntegrityError, OSError) as error:
        print(f"拒绝运行：{error}", file=sys.stderr)
        print("本次没有发起任何模型请求。", file=sys.stderr)
        return 4

    try:
        successes, failures_by_key = resume_state(plan, results_path)
    except IdentityConflictError as error:
        print(f"拒绝运行：{error}", file=sys.stderr)
        print("本次没有发起任何模型请求。", file=sys.stderr)
        return 4

    all_tasks = scheduled_tasks(plan)
    pending = [
        (sample_id, task)
        for sample_id, task in all_tasks
        if task_key(plan, sample_id, task) not in successes
    ]
    blocked = [
        (sample_id, task)
        for sample_id, task in pending
        if task_key(plan, sample_id, task) in failures_by_key
    ]

    if blocked:
        print(
            f"拒绝运行：{len(blocked)} 个任务已有失败记录，不自动重试，"
            "请人工检查后决定是否换新的 --run-dir。",
            file=sys.stderr,
        )

        for sample_id, task in blocked[:5]:
            row = failures_by_key[task_key(plan, sample_id, task)]
            print(
                f"  sample={sample_id} group={task['group']} "
                f"repeat={task['repeat_index']} error={row.get('error_type')}",
                file=sys.stderr,
            )

        if len(blocked) > 5:
            print(f"  … 另有 {len(blocked) - 5} 条", file=sys.stderr)

        return 1

    if not pending:
        print(f"experiment_id = {experiment_id}")
        print(f"run_dir       = {run_dir}")
        print(f"plan          = {plan_path.name} sha256={plan['plan_sha256'][:12]}…")
        print(
            f"全部 {len(all_tasks)} 个任务都已有成功记录，本次不发起任何模型请求。"
        )
        return 0

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
    print(
        f"plan          = {describe_schedule(plan)}  "
        f"scheme={plan['scheme']} seed={plan['seed']} "
        f"rounds={plan['round_count']} algorithm={plan['algorithm_version']}"
    )
    print(
        f"resume        = 已完成 {len(successes)} / {len(all_tasks)}，"
        f"本次待执行 {len(pending)}"
    )
    print(f"sampling      = {sampling}")

    if skipped_b0:
        print(
            f"B0 legacy     = 跳过 {len(skipped_b0)} 个 B0 任务对"
            "（既有 10 份结果保持不动，不重跑）"
        )

    # 4) 按计划顺序执行：sample 区块 → 轮次 → 组；只读 plan.json，不重算顺序。
    pending_keys = {
        task_key(plan, sample_id, task) for sample_id, task in pending
    }
    executed = 0
    failed = 0

    for block in plan["blocks"]:
        sample_id = block["sample_id"]
        sample_tasks = [
            task
            for task in block["tasks"]
            if task_key(plan, sample_id, task) in pending_keys
        ]

        if not sample_tasks:
            print(f"  {sample_id}: 全部 {len(block['tasks'])} 个任务已完成，跳过")
            continue

        api = build_prompts.project_api(config)
        sample = seed_fixture.load_sample(sample_id, config)
        db_path, api = build_prompts.activate_sample(
            sample_id, config, reuse=args.reuse, sample=sample
        )
        moment = seed_fixture.ref_datetime(config)
        inputs = build_prompts.collect_inputs(sample, config, api, moment)

        # 运行前预检：只覆盖本样本真正待执行的组，且发生在该样本第一次
        # 模型调用之前；全部成功的样本根本不会走到这里。
        groups = []

        for task in sample_tasks:
            if task["group"] not in groups:
                groups.append(task["group"])

        try:
            preflight_sample(
                sample_id,
                groups,
                inputs,
                api,
                Path(db_path),
                config,
            )
        except PreflightError as error:
            print(f"拒绝运行：{error}", file=sys.stderr)
            print(
                "已按 fail-closed 停止本次运行；该样本没有发起任何模型请求。",
                file=sys.stderr,
            )
            return 3

        for task in sample_tasks:
            record = run_one(
                experiment_id=experiment_id,
                sample_id=sample_id,
                group=task["group"],
                repeat_index=task["repeat_index"],
                inputs=inputs,
                api=api,
                run_dir=run_dir,
                recorder=recorder,
                sampling=sampling,
                sample_selection=task["sample_selection"],
                b0_sample_override=task["b0_sample_override"],
                plan_seq=task["plan_seq"],
                round_index=task["round_index"],
                seed=plan["seed"],
                scheme=plan["scheme"],
                algorithm_version=plan["algorithm_version"],
                plan_sha256=plan["plan_sha256"],
            )
            append_jsonl(results_path, record)

            executed += 1

            print(
                f"  #{task['plan_seq']:03d} {sample_id}/{task['group']}/"
                f"round{task['round_index']}: "
                f"success={record['success']} "
                f"requests={record['request_count']} "
                f"corrections={record['corrections_used']} "
                f"chars={record['prompt_chars']} "
                f"db={db_path.name}"
            )

            if not record["success"]:
                failed += 1
                print(
                    f"    error: {record['error_type']} - {record['error_message']}"
                )
                print(
                    "失败记录已保留（不自动重试）；请人工检查后用新的 --run-dir "
                    "重跑，或修复环境后继续。",
                    file=sys.stderr,
                )
                return 1

    print(f"results = {results_path}")
    print(f"total={executed} failures={failed} skipped={len(all_tasks) - executed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
