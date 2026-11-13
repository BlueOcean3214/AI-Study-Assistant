"""毕业设计实验脚本 · 自动指标核对：Y1–Y5 与 grounding。

只读 `experiments/graduation/runs/*/results.jsonl` 与同目录 `prompts/`，按
docs/GRADUATION-EXPERIMENT-DESIGN.md v4.5 §4.4 计算 Y1–Y5，并用
`agent_plan_draft.check_grounding()` 复核来源完整性。

设计要点（与 v4.5 一致，且不夸大测到的内容）：

- **只读**：不写 runs/、不写 plan.json/results.jsonl、不写 feedback.db、不生成盲评包、
  不调用任何模型或 Embedding API。运行前后对每个 results.jsonl 做 SHA-256 比对，
  不一致直接失败。
- **主实验与补充实验分开输出**：主实验写 auto_metrics.csv，补充批次写
  auto_metrics_supplement.csv；B0 legacy 10 份归入补充批次（§5.4.4 把 B0 计入补充 23 份）。
- **不适用就写 N/A**：Y1 的"模拟完成率"在正式样本未提供能力等级/可用时长时记 N/A，
  不编造能力值；Y5 的 `len(messages)` 在归档未保存 messages 数组时记 N/A，
  不把"Prompt 文件里能确认的 SYSTEM+USER 两条"冒充成运行时的消息条数。
- **不把 SKIP/UNVERIFIABLE 记成 PASS**：grounding 在归档信息不足时明确标注
  `UNVERIFIABLE`，并写清是哪一条证据链缺失。

Y1 主指标用真实的 `agent_tools.validate_plan_for_save()`（`agent_tools.py:438`）。
该函数只读一次"最近一条学习反馈"、不调模型，但需要可读的库；因此本脚本在
`%TEMP%/graduation_experiments/<ref>/` 下用 `seed_fixture.seed_sample()` 重建
临时夹具库后调用，绝不碰真实 `feedback.db`。

命令：

    python experiments/graduation/check_auto.py                    # 默认输出到临时目录
    python experiments/graduation/check_auto.py --out-dir experiments/graduation/auto
    python experiments/graduation/check_auto.py --no-fixtures      # 不建夹具库，Y1 记 N/A
"""

import argparse
import csv
import hashlib
import json
import statistics
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_prompts  # noqa: E402
import seed_fixture  # noqa: E402

RESULTS_NAME = "results.jsonl"
MAIN_RUN = "main-20261113T061051Z"
SMOKE_RUN = "internal-api-smoke-20261113T061019Z"
SUPPLEMENT_RUNS = (
    "suppl-subj-20261113T062711Z",
    "suppl-s7empty-20261113T062822Z",
    "20261112T210000-20261113T011751Z",
    "20261112T210000-20261113T012031Z",
    "b0-remainder-20261113T013105Z",
)
MANIFEST_PATH = HERE.parents[1] / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"

# 合法取值（v4.5 §4.2、§5.1）
MAIN_GROUPS = ("A", "B", "C", "D", "B0")
SAMPLE_IDS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
SUPPLEMENT_SAMPLES = {"suppl-subj-20261113T062711Z": "S5"}

MAX_CORRECTIONS = 2

# 模拟推演（§4.4 辅助指标）：难度 ≤ 能力 且 时长 ≤ 可用时长 记完成。
# 只使用样本里**显式提供**的能力/时长字段；正式 S1–S7 没有这两个字段，
# 因此正式实验该指标一律 N/A，而不是按脚本自定的规则硬算。
ABILITY_RANK = {"easy": 1, "medium": 2, "hard": 3}


class CheckError(RuntimeError):
    """输入不合法或数据被改动：终止并返回非零。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def median(values):
    return statistics.median(values) if values else None


def mean(values):
    return statistics.fmean(values) if values else None


def p95(values):
    """最近秩法 P95（不做插值，避免与论文口径不一致）。"""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(0.95 * len(ordered) + 0.5)) - 1)
    rank = min(rank, len(ordered) - 1)
    return ordered[rank]


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# 归档行读取与字段校验
# ---------------------------------------------------------------------------


def read_rows(run_dir):
    """读一个 run 目录的 results.jsonl，并做字段/类型校验。

    返回 (rows, problems)。problems 里每项是一条 (row_number, field, detail)。
    行号从 1 开始，便于人工回查。
    """

    path = Path(run_dir) / RESULTS_NAME
    if not path.is_file():
        raise CheckError(f"缺少结果文件：{path}")

    rows = []
    problems = []

    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                problems.append((number, "json", str(error)))
                continue
            if not isinstance(row, dict):
                problems.append((number, "row", "不是 JSON 对象"))
                continue
            rows.append((number, row))

    for number, row in rows:
        def bad(field, detail):
            problems.append((number, field, detail))

        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip():
            bad("sample_id", "缺失或不是非空字符串")

        group = row.get("group")
        if group not in build_prompts.GROUP_SLOTS:
            bad("group", f"非法取值：{group!r}")

        if not row.get("experiment_id") or not isinstance(row.get("experiment_id"), str):
            bad("experiment_id", "缺失或不是非空字符串（身份字段必须保留）")

        repeat = row.get("repeat_index")
        if not is_int(repeat) or not 0 <= repeat <= 4:
            bad("repeat_index", f"必须是 0–4 的整数，实际 {repeat!r}")

        if not isinstance(row.get("success"), bool):
            bad("success", "必须是布尔值")

        if row.get("error_type") is not None and not isinstance(row.get("error_type"), str):
            bad("error_type", "必须是字符串或 null")

        request_count = row.get("request_count")
        if not is_int(request_count) or request_count < 1:
            bad("request_count", f"必须是 ≥1 的整数，实际 {request_count!r}")

        corrections = row.get("corrections_used")
        if not is_int(corrections) or not 0 <= corrections <= MAX_CORRECTIONS:
            bad("corrections_used", f"必须是 0–{MAX_CORRECTIONS} 的整数，实际 {corrections!r}")

        elapsed = row.get("elapsed_seconds")
        if not is_number(elapsed) or elapsed <= 0:
            bad("elapsed_seconds", f"必须是正数，实际 {elapsed!r}")

        final_output = row.get("final_output")
        if not isinstance(final_output, dict):
            bad("final_output", "必须是 JSON 对象（含 answer/plan）")
        elif not isinstance(final_output.get("answer"), str):
            bad("final_output.answer", "必须是字符串")

        # legacy 行没有 plan_seq / round_index：允许缺失，但存在时必须是整数
        for field in ("plan_seq", "round_index"):
            if field in row and not is_int(row[field]):
                bad(field, f"存在但不是整数：{row[field]!r}")

        if "prompt_sha256" in row:
            value = row["prompt_sha256"]
            if not isinstance(value, str) or len(value) != 64:
                bad("prompt_sha256", "必须是 64 位十六进制字符串")

    return rows, problems


def manifest_hashes():
    """docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv → {relative_path: (sha256, records)}。"""

    hashes = {}
    if not MANIFEST_PATH.is_file():
        return hashes
    with MANIFEST_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            relative = (row.get("relative_path") or "").strip().replace("\\", "/")
            if relative:
                hashes[relative] = (row.get("sha256"), row.get("records"))
    return hashes


def verify_manifest(run_name, run_dir, hashes, rows_count):
    """返回 (状态, 细节)。状态：match / missing / mismatch / no_manifest。"""

    key = f"experiments/graduation/runs/{run_name}/{RESULTS_NAME}"
    if key not in hashes:
        return "manifest_missing", f"manifest 未收录 {key}（legacy 目录不在清单内）"

    expected_sha, expected_records = hashes[key]
    actual_sha = sha256_file(Path(run_dir) / RESULTS_NAME)
    if actual_sha != expected_sha:
        return "mismatch", f"SHA-256 不一致：manifest={expected_sha} 实际={actual_sha}"

    if expected_records is not None and str(expected_records).strip() != str(rows_count):
        return "mismatch", f"记录数不一致：manifest={expected_records} 实际={rows_count}"

    return "match", f"SHA-256 与 manifest 一致（{actual_sha[:16]}…）"


# ---------------------------------------------------------------------------
# grounding：从归档的 Prompt 重建 evidence
# ---------------------------------------------------------------------------


def prompt_text(run_dir, sample_id, group):
    path = Path(run_dir) / "prompts" / str(sample_id) / f"{group}.txt"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def memory_block_present(text):
    return isinstance(text, str) and build_prompts.MEMORY_HEADER in text


def reconstruct_steps(group, feedback_count, memory_count):
    """按 run_experiment.evidence_steps() 的同一规则重建证据链。

    本实验不调用真实工具，证据由程序合成；这里只重建"当时注入了什么"。
    """

    slots = build_prompts.GROUP_SLOTS[group]
    steps = []

    if slots["feedback"]:
        steps.append(
            {
                "type": "tool_call",
                "tool": "get_recent_feedback",
                "arguments": {"days": 7},
                "ok": True,
                "error_type": None,
                "result": {"ok": True, "count": feedback_count},
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
                "result": {"ok": True, "count": memory_count},
            }
        )

    return steps


def grounding_for_row(row, run_dir, sample_cache, checker, memory_markers):
    """返回 (status, detail)。status ∈ PASS / FAIL / UNVERIFIABLE。"""

    sample_id = row.get("sample_id")
    group = row.get("group")
    answer = (row.get("final_output") or {}).get("answer") or ""
    plan = (row.get("final_output") or {}).get("plan") or {}
    reason = plan.get("reason") if isinstance(plan, dict) else None

    if sample_id not in sample_cache:
        try:
            sample_cache[sample_id] = seed_fixture.load_sample(sample_id)
        except Exception as error:  # noqa: BLE001 - 缺样本就无法判断，显式标注
            return "UNVERIFIABLE", f"读不到样本 {sample_id}：{type(error).__name__}: {error}"

    sample = sample_cache[sample_id]
    text = prompt_text(run_dir, sample_id, group)

    if text is None:
        return "UNVERIFIABLE", "缺少 Prompt 文件，无法确认当时注入的 evidence"

    feedback_count = len(sample.get("feedbacks") or [])
    memory_count = 1 if memory_block_present(text) else 0
    steps = reconstruct_steps(group, feedback_count, memory_count)

    # memory_result 只有在 Prompt 里确有记忆区块时才存在；这与运行时的
    # agent_plan_draft._collect_tool_data() 一致（没有有效记忆时为 None）。
    memory_result = None
    if memory_count:
        memory_result = {"count": 1, "memories": [{"content": "（归档 Prompt 中确有记忆区块）"}]}

    texts = [answer]
    if isinstance(reason, str):
        texts.append(reason)

    violations = checker(texts, steps, memory_result=memory_result)

    if violations:
        return "FAIL", "；".join(violations)

    # 归档没有保存 steps / 最终 draft 消息，无法证明"同一份 memory_result 拼进了
    # 最终 draft"；只要模型没有声称记忆来源，这不影响判定。
    claimed_memory = any(marker in answer for marker in memory_markers)
    if memory_count and not claimed_memory:
        return "PASS", "来源主张与注入一致（记忆区块存在；模型未声称记忆来源）"

    return "PASS", "来源主张与注入一致"


# ---------------------------------------------------------------------------
# Y1：结构可保存率（真实调用 validate_plan_for_save）
# ---------------------------------------------------------------------------


def derive_difficult(sample):
    """与 agent_tools._derive_difficult_previous_task() 同一规则，用于交叉核对。"""

    feedbacks = sample.get("feedbacks") or []
    if not feedbacks:
        return False
    latest = feedbacks[-1]
    return latest.get("status") == "partial" and latest.get("reason") == "任务难度太高"


def y1_for_plan(plan, apis, sample_id, config):
    """在临时夹具库上调用真实 validate_plan_for_save()。

    返回 (status, detail)。status ∈ VALID / INVALID / UNAVAILABLE。
    """

    if apis is None:
        return "UNAVAILABLE", "未启用夹具库（--no-fixtures），无法调用 validate_plan_for_save()"

    api = apis
    try:
        if api.get("_sample") != sample_id:
            seed_fixture.seed_sample(sample_id, config, rebuild=True)
            api["_sample"] = sample_id
        result = api["agent_tools"].validate_plan_for_save(plan)
    except Exception as error:  # noqa: BLE001 - 夹具不可用就明确标注，不冒充通过
        api["_sample"] = None
        return "UNAVAILABLE", f"夹具库调用失败：{type(error).__name__}: {error}"

    if result.get("valid"):
        return "VALID", "validate_plan_for_save 通过"
    return "INVALID", str(result.get("reason"))


def build_apis(config):
    """一次性准备夹具与真实校验函数；失败时返回 (None, 原因)。"""

    try:
        apis = build_prompts.project_api(config)
        apis["_sample"] = None
        # 交叉核对用：纯结构校验器
        import ai_service  # noqa: E402 - 只在夹具隔离后导入

        apis["_ai_service"] = ai_service
        return apis, None
    except Exception as error:  # noqa: BLE001
        return None, f"{type(error).__name__}: {error}"


def simulate_completion(plan, sample):
    """§4.4 确定性学习者模拟器（模拟推演）。

    只有样本显式给出 ability_level 与 available_minutes 时才计算；
    否则返回 None，由调用方记 N/A。
    """

    ability = sample.get("ability_level")
    available = sample.get("available_minutes")
    if ability not in ABILITY_RANK or not is_number(available):
        return None
    if not isinstance(plan, dict):
        return False
    difficulty = plan.get("difficulty")
    minutes = plan.get("estimated_minutes")
    if difficulty not in ABILITY_RANK or not is_number(minutes):
        return False
    return ABILITY_RANK[difficulty] <= ABILITY_RANK[ability] and minutes <= available


# ---------------------------------------------------------------------------
# 指标汇总
# ---------------------------------------------------------------------------


def metric_rows_for_scope(scope, analyses):
    """把逐行分析结果汇成长表行：table,metric,group,unit,value,n,denominator,note。"""

    rows = []

    def add(metric, value, n, denominator="", group="all", unit="", note=""):
        rows.append(
            {
                "table": scope,
                "metric": metric,
                "group": group,
                "unit": unit,
                "value": "" if value is None else value,
                "n": n,
                "denominator": denominator,
                "note": note,
            }
        )

    total = len(analyses)

    # Y1 结构可保存率
    y1_valid = [a for a in analyses if a["y1"] == "VALID"]
    y1_invalid = [a for a in analyses if a["y1"] == "INVALID"]
    y1_unavailable = [a for a in analyses if a["y1"] == "UNAVAILABLE"]
    if total and not y1_unavailable:
        add("Y1_structural_saveable_rate", round(len(y1_valid) / total, 4), total, total,
            unit="ratio", note="validate_plan_for_save() 通过数/总数")
    elif y1_unavailable:
        add("Y1_structural_saveable_rate", "N/A", total, total, unit="ratio",
            note=f"{len(y1_unavailable)} 行夹具不可用，无法调用 validate_plan_for_save()；"
                 f"已通过 {len(y1_valid)}、未通过 {len(y1_invalid)}")

    # Y1 辅助：模拟完成率（仅当样本显式提供能力/时长）
    sim_values = [a["simulated"] for a in analyses if a["simulated"] is not None]
    if sim_values:
        add("Y1_simulated_completion_rate", round(sum(1 for v in sim_values if v) / len(sim_values), 4),
            len(sim_values), len(sim_values), unit="ratio",
            note="模拟推演：难度≤能力且时长≤可用时长记完成（非真实用户行为）")
    else:
        add("Y1_simulated_completion_rate", "N/A", 0, total, unit="ratio",
            note="样本未提供 ability_level / available_minutes，按 §4.4 口径不可计算")

    # Y2 调用次数
    calls = [a["request_count"] for a in analyses]
    if calls:
        add("Y2_calls_mean", round(mean(calls), 4), len(calls), unit="calls")
        add("Y2_calls_median", median(calls), len(calls), unit="calls")
        add("Y2_calls_max", max(calls), len(calls), unit="calls",
            note=f"起草 1 次 + 修正 0–{MAX_CORRECTIONS} 次")

    # Y3 延迟
    latencies = [a["elapsed_seconds"] for a in analyses]
    if latencies:
        add("Y3_latency_median_s", round(median(latencies), 3), len(latencies), unit="seconds")
        add("Y3_latency_p95_s", round(p95(latencies), 3), len(latencies), unit="seconds")

    # Y4 错误率：模型错误 + 超时 + 非法 JSON + 校验不过
    failed = [a for a in analyses if a["failed"]]
    add("Y4_error_rate", round(len(failed) / total, 4) if total else "N/A", total, total,
        unit="ratio",
        note="失败 = success=False 或 error_type 非空 或 plan 不可解析 或 validate_plan_for_save 未通过"
             + ("（Y1 不可用的行不计入校验失败）" if y1_unavailable else ""))

    # Y5 提示规模
    prompt_tokens = [a["prompt_tokens"] for a in analyses if a["prompt_tokens"] is not None]
    if prompt_tokens:
        add("Y5_prompt_tokens_median", median(prompt_tokens), len(prompt_tokens), unit="tokens",
            note="usage.prompt_tokens（首次请求）")
    else:
        add("Y5_prompt_tokens_median", "N/A", 0, total, unit="tokens", note="requests 中没有 prompt_tokens")
    add("Y5_messages_len_median", "N/A", 0, total, unit="messages",
        note="归档未保存 messages 数组；Prompt 文件只能确认 SYSTEM+USER 两条基础消息，"
             "不把推断值冒充为运行时消息条数")

    # grounding：分母只算可判定的行（PASS+FAIL），UNVERIFIABLE 单独计数、不进分母
    grounding_pass = [a for a in analyses if a["grounding"] == "PASS"]
    grounding_fail = [a for a in analyses if a["grounding"] == "FAIL"]
    grounding_unver = [a for a in analyses if a["grounding"] == "UNVERIFIABLE"]
    verifiable = len(grounding_pass) + len(grounding_fail)
    add("grounding_pass_rate",
        round(len(grounding_pass) / verifiable, 4) if verifiable else "N/A",
        verifiable, verifiable, unit="ratio",
        note=f"PASS {len(grounding_pass)} / FAIL {len(grounding_fail)} / "
             f"UNVERIFIABLE {len(grounding_unver)}（UNVERIFIABLE 不计入 PASS 也不进分母；"
             f"总行数 {len(analyses)}）")

    # 分组明细（只给 grounding 与 Y1，便于定位差异）
    for group in sorted({a["group"] for a in analyses}):
        subset = [a for a in analyses if a["group"] == group]
        group_pass = sum(1 for a in subset if a["grounding"] == "PASS")
        group_fail = sum(1 for a in subset if a["grounding"] == "FAIL")
        group_unver = sum(1 for a in subset if a["grounding"] == "UNVERIFIABLE")
        group_verifiable = group_pass + group_fail
        group_valid = sum(1 for a in subset if a["y1"] == "VALID")
        group_invalid = sum(1 for a in subset if a["y1"] == "INVALID")
        value = round(group_valid / len(subset), 4) if group_invalid or group_valid else "N/A"
        add("Y1_structural_saveable_rate", value, len(subset), len(subset), group=group,
            unit="ratio", note="按组拆分")
        add("grounding_pass_rate",
            round(group_pass / group_verifiable, 4) if group_verifiable else "N/A",
            group_verifiable, group_verifiable, group=group, unit="ratio",
            note=f"按组拆分；UNVERIFIABLE {group_unver} 不计入 PASS 也不进分母")

    return rows


def analyze_row(run_name, run_dir, number, row, apis, config, sample_cache, checker,
                memory_markers):
    """单行分析，返回 dict 与失败明细列表。"""

    failures = []
    sample_id = row.get("sample_id")
    group = row.get("group")
    final_output = row.get("final_output") if isinstance(row.get("final_output"), dict) else {}
    plan = final_output.get("plan")

    sample = sample_cache.get(sample_id)
    if sample is None:
        try:
            sample = seed_fixture.load_sample(sample_id)
            sample_cache[sample_id] = sample
        except Exception:  # noqa: BLE001 - 缺样本时样本相关指标显式标注
            sample = {}

    # Y1
    if isinstance(plan, dict):
        y1, y1_detail = y1_for_plan(plan, apis, sample_id, config)
    else:
        y1, y1_detail = "INVALID", "final_output.plan 缺失或不是对象"

    if y1 == "INVALID":
        failures.append({
            "scope": "",
            "run_dir": run_name,
            "row_number": number,
            "experiment_id": row.get("experiment_id") or "",
            "sample_id": sample_id or "",
            "group": group or "",
            "repeat_index": row.get("repeat_index"),
            "metric": "Y1",
            "detail": y1_detail,
        })

    # grounding
    grounding, grounding_detail = grounding_for_row(
        row, run_dir, sample_cache, checker, memory_markers
    )
    if grounding != "PASS":
        failures.append({
            "scope": "",
            "run_dir": run_name,
            "row_number": number,
            "experiment_id": row.get("experiment_id") or "",
            "sample_id": sample_id or "",
            "group": group or "",
            "repeat_index": row.get("repeat_index"),
            "metric": "grounding",
            "detail": f"{grounding}: {grounding_detail}",
        })

    # Y5 prompt tokens：取首次请求（归档是 list；兼容旧格式的 JSON 字符串）
    prompt_tokens = None
    requests = row.get("requests")
    if isinstance(requests, str):
        try:
            requests = json.loads(requests)
        except json.JSONDecodeError:
            requests = None
    if isinstance(requests, list) and requests and isinstance(requests[0], dict):
        value = requests[0].get("prompt_tokens")
        if is_int(value) and value > 0:
            prompt_tokens = value
    if prompt_tokens is None:
        usage = row.get("token_usage")
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except json.JSONDecodeError:
                usage = None
        if isinstance(usage, dict):
            value = usage.get("prompt_tokens")
            if is_int(value) and value > 0:
                prompt_tokens = value

    simulated = simulate_completion(plan if isinstance(plan, dict) else None, sample)

    failed = (
        row.get("success") is not True
        or row.get("error_type") is not None
        or not isinstance(plan, dict)
        or y1 == "INVALID"
    )

    analysis = {
        "group": group,
        "y1": y1,
        "grounding": grounding,
        "request_count": row.get("request_count"),
        "elapsed_seconds": row.get("elapsed_seconds"),
        "prompt_tokens": prompt_tokens,
        "simulated": simulated,
        "failed": failed,
    }

    return analysis, failures


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


METRIC_COLUMNS = ("table", "metric", "group", "unit", "value", "n", "denominator", "note")
FAILURE_COLUMNS = (
    "scope", "run_dir", "row_number", "experiment_id",
    "sample_id", "group", "repeat_index", "metric", "detail",
)


def write_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def render_report(path, payload):
    lines = []
    lines.append("# 自动指标核对报告（Y1–Y5 + grounding）")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{payload['generated_utc']}")
    lines.append(f"- 配置：`{payload['config_path']}`")
    lines.append(f"- 脚本：`experiments/graduation/check_auto.py`（v{payload['version']}）")
    lines.append("- 数据来源：`experiments/graduation/runs/*/results.jsonl` 与同目录 `prompts/`")
    lines.append("")
    lines.append("## 1. 只读与数据保护")
    lines.append("")
    lines.append("| run | 行数 | manifest 校验 | results.jsonl SHA-256（前后一致） |")
    lines.append("| --- | --- | --- | --- |")
    for item in payload["sources"]:
        lines.append(
            f"| {item['run']} | {item['rows']} | {item['manifest']} | "
            f"`{item['sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("运行前后逐文件比对 SHA-256 一致；未写入 runs/、plan.json、results.jsonl、feedback.db。")
    lines.append("")
    lines.append("## 2. 主实验（140 次生成）")
    lines.append("")
    lines.append(payload["main_table"])
    lines.append("")
    lines.append("## 3. 补充批次（单独成表，不并入主表）")
    lines.append("")
    lines.append(payload["supplement_table"])
    lines.append("")
    lines.append("## 4. 已知数据缺口（不以 PASS 代替）")
    lines.append("")
    for note in payload["gaps"]:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## 5. 失败清单")
    lines.append("")
    lines.append(f"共 {payload['failure_count']} 条，详见 `{payload['failures_name']}`。")
    lines.append("")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def md_table(rows):
    """把长表渲染成 Markdown（只保留核心列）。"""

    if not rows:
        return "（无数据）"
    lines = ["| 指标 | 组 | 值 | n | 说明 |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        note = str(row["note"]).replace("|", "\\|")
        lines.append(
            f"| {row['metric']} | {row['group']} | {row['value']} | "
            f"{row['n']} | {note} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def collect_scope(scope, run_names, runs_dir, apis, config, sample_cache, checker,
                  memory_markers):
    analyses = []
    failures = []
    sources = []

    hashes = manifest_hashes()

    for run_name in run_names:
        run_dir = Path(runs_dir) / run_name
        rows, problems = read_rows(run_dir)
        for number, field, detail in problems:
            failures.append({
                "scope": scope,
                "run_dir": run_name,
                "row_number": number,
                "experiment_id": "",
                "sample_id": "",
                "group": "",
                "repeat_index": "",
                "metric": "schema",
                "detail": f"{field}: {detail}",
            })

        status, detail = verify_manifest(run_name, run_dir, hashes, len(rows))
        sources.append({
            "run": run_name,
            "rows": len(rows),
            "manifest": f"{status}（{detail}）",
            "sha256": sha256_file(Path(run_dir) / RESULTS_NAME),
        })

        for number, row in rows:
            analysis, row_failures = analyze_row(
                run_name, run_dir, number, row, apis, config, sample_cache, checker,
                memory_markers,
            )
            analysis["scope"] = scope
            analyses.append(analysis)
            for failure in row_failures:
                failure["scope"] = scope
            failures.extend(row_failures)

    return analyses, failures, sources


def run(args):
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    runs_dir = Path(args.runs_dir).resolve()

    if not runs_dir.is_dir():
        raise CheckError(f"runs 目录不存在：{runs_dir}")

    sample_cache = {}

    # 顺序很重要：夹具隔离（project_modules）必须先于 database/agent_tools 的导入，
    # 否则 database.DATABASE_PATH 会先绑定真实 feedback.db。
    apis = None
    apis_error = None
    checker = None
    memory_markers = ()
    checker_error = None

    if args.no_fixtures:
        apis_error = "用户指定 --no-fixtures"
        checker_error = "用户指定 --no-fixtures，未导入检查器"
    else:
        apis, apis_error = build_apis(config)
        if apis is not None:
            checker = apis["agent_plan_draft"].check_grounding
            memory_markers = apis["agent_plan_draft"].MEMORY_CLAIM_MARKERS
            checker_error = None
        else:
            checker_error = apis_error

    if checker is None:
        # 没有夹具时仍可尝试导入 check_grounding（纯函数、不读库），用于补充批次之外的
        # 来源复核；但 Y1 会明确记 N/A，绝不用 SKIP 冒充 PASS。
        try:
            project_root = str(seed_fixture.PROJECT_ROOT)
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            import agent_plan_draft  # noqa: E402

            checker = agent_plan_draft.check_grounding
            memory_markers = agent_plan_draft.MEMORY_CLAIM_MARKERS
            checker_error = None
        except Exception as error:  # noqa: BLE001
            checker_error = f"{type(error).__name__}: {error}"

    if checker is None:
        # 没有检查器就无法计算 grounding；明确失败，不用 SKIP 冒充 PASS
        def checker(texts, steps, memory_result=None):  # noqa: ANN001
            raise CheckError(f"无法导入 agent_plan_draft.check_grounding：{checker_error}")

    before = {
        name: sha256_file(Path(runs_dir) / name / RESULTS_NAME)
        for name in [MAIN_RUN, *SUPPLEMENT_RUNS]
        if (Path(runs_dir) / name / RESULTS_NAME).is_file()
    }

    main_analyses, main_failures, main_sources = collect_scope(
        "main", [MAIN_RUN], runs_dir, apis, config, sample_cache, checker, memory_markers
    )
    supp_analyses, supp_failures, supp_sources = collect_scope(
        "supplement", list(SUPPLEMENT_RUNS), runs_dir, apis, config, sample_cache,
        checker, memory_markers,
    )

    smoke_sources = []
    if args.include_smoke:
        smoke_analyses, smoke_failures, smoke_sources = collect_scope(
            "smoke", [SMOKE_RUN], runs_dir, apis, config, sample_cache, checker,
            memory_markers,
        )
        supp_analyses += smoke_analyses
        supp_failures += smoke_failures

    after = {
        name: sha256_file(Path(runs_dir) / name / RESULTS_NAME)
        for name in before
    }
    if before != after:
        raise CheckError("results.jsonl 在核对过程中发生变化，疑似破坏只读约定，已终止")

    main_rows = metric_rows_for_scope("main", main_analyses)
    supp_rows = metric_rows_for_scope("supplement", supp_analyses)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else (
        Path(tempfile.gettempdir()) / "graduation_auto_metrics"
    )
    main_csv = write_csv(out_dir / "auto_metrics.csv", METRIC_COLUMNS, main_rows)
    supp_csv = write_csv(out_dir / "auto_metrics_supplement.csv", METRIC_COLUMNS, supp_rows)
    failures = main_failures + supp_failures
    failures_csv = write_csv(out_dir / "auto_failures.csv", FAILURE_COLUMNS, failures)

    gaps = [
        "Y1 辅助指标（模拟完成率）：S1–S7 样本没有 ability_level / available_minutes 字段，"
        "按 §4.4 口径记 N/A；已实现 simulate_completion() 并在测试里用合成样本验证，"
        "但不用它推演正式数据。",
        "Y5 len(messages)：归档 results.jsonl 没有 messages 数组，记 N/A；"
        "prompt tokens 来自 requests[0].prompt_tokens，是真实记录。",
        "grounding：归档没有保存 steps 与最终 draft 消息，本脚本按 Prompt 中实际注入的"
        "反馈/记忆区块重建 evidence；若模型未声称记忆来源，则结论不受该限制影响，"
        "已逐行记 UNVERIFIABLE 的行不与 PASS 混算。",
        "B0 legacy 两个早期 run 合计 10 行，manifest 未收录其 results.jsonl，"
        "SHA-256 以现场计算值为准（详见第 1 节）。",
    ]
    if apis is None:
        gaps.append(f"Y1 主指标：夹具库不可用（{apis_error}），记 N/A，未冒充通过。")

    payload = {
        "generated_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_path": str(config_path),
        "version": VERSION,
        "sources": main_sources + supp_sources + smoke_sources,
        "main_table": md_table(main_rows),
        "supplement_table": md_table(supp_rows),
        "gaps": gaps,
        "failure_count": len(failures),
        "failures_name": failures_csv.name,
    }
    report = render_report(out_dir / "auto_metrics_report.md", payload)

    print(f"主实验：{len(main_analyses)} 行 → {main_csv}")
    print(f"补充批次：{len(supp_analyses)} 行 → {supp_csv}")
    print(f"失败清单：{len(failures)} 条 → {failures_csv}")
    print(f"报告：{report}")
    print("只读校验：results.jsonl SHA-256 前后一致")

    if apis_error:
        print(f"注意：Y1 夹具不可用（{apis_error}），已记 N/A")

    if args.strict and failures:
        return 1
    return 0


VERSION = "1.0.0"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="自动指标核对：Y1–Y5 与 grounding")
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--runs-dir", default=str(HERE / "runs"))
    parser.add_argument("--out-dir", default=None,
                        help="输出目录，默认 %%TEMP%%/graduation_auto_metrics")
    parser.add_argument("--no-fixtures", action="store_true",
                        help="不重建临时夹具库，Y1 主指标记 N/A")
    parser.add_argument("--include-smoke", action="store_true",
                        help="把 internal-api-smoke 计入补充表（默认排除）")
    parser.add_argument("--strict", action="store_true",
                        help="有任何失败明细时返回非零")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except CheckError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
