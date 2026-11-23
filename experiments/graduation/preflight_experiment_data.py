#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""preflight_experiment_data.py — 正式实验数据的只读完整性预检。

用途（见 docs/GRADUATION-EXPERIMENT-DESIGN.md §2.4 / §5.1 / §5.3 / §7.1）：

    .venv\\Scripts\\python.exe experiments\\graduation\\preflight_experiment_data.py
    .venv\\Scripts\\python.exe experiments\\graduation\\preflight_experiment_data.py \\
        --runs-dir experiments/graduation/runs \\
        --manifest docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv

检查内容：
- 清单（manifest）声明的文件是否存在、大小/SHA-256/记录数是否与磁盘一致；
- plan.json 的 plan_sha256 是否自洽，冻结参数（seed/scheme/algorithm_version/
  block_unit/round_count）是否与设计 §5.1 一致；
- results.jsonl 是否可解析、字段是否齐全、身份是否重复、行是否属于计划、
  plan_sha256 是否与同目录计划同源；
- 覆盖是否与冻结设计一致：主实验 7 样本 × A/B/C/D × 5 轮 = 140 份，补充
  C/D-subj（S5）10 份、S7-empty × D 5 份，生成任务合计 165 份；B0 的 10 份是
  legacy 探索组，只做记录、不作为覆盖要求。

只读约定：本工具只打开文件进行读取，不写、不改、不生成任何实验数据；唯一的写操作
是把报告写到显式给出的 `--json` 路径（可选）。运行结束会重算输入文件哈希，若期间
被改动则报 FAIL。

报告分级：
- PASS：实际值与冻结设计/清单一致；
- WARNING：设计未规定或无法从现有证据判定的地方（例如未纳入清单的 legacy 目录），
  需要负责人确认；
- FAIL：与冻结设计/清单冲突，或数据本身损坏，这些情况不得进入正式统计。
- UNKNOWN：证据不足（文件缺失导致无法继续核对该项）。

退出码：0 = 无 FAIL；1 = 存在 FAIL；2 = 参数/环境错误。
"""

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import manifest_rules  # noqa: E402
import plan_schedule  # noqa: E402

# --- 冻结口径（docs/GRADUATION-EXPERIMENT-DESIGN.md） -----------------------
DESIGN_SEED = 20261113                     # §5.1 冻结 seed
DESIGN_SCHEME = "five_round_randomized_block_v1"      # §5.1
DESIGN_ALGORITHM_VERSION = "sha256-block-order-v1"    # §5.1
DESIGN_BLOCK_UNIT = "sample"               # §5.1
DESIGN_ROUND_COUNT = 5                     # §5.1 / §5.3
DESIGN_MAIN_SAMPLES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")   # §2.4
DESIGN_MAIN_GROUPS = ("A", "B", "C", "D")                          # §2.4
DESIGN_MAIN_RECORDS = 140                  # §2.4 28 格 × 5 轮
DESIGN_SUPPLEMENT_SUBJ = {"sample_id": "S5", "groups": ("C-subj", "D-subj"),
                          "batches": ("suppl-subj",), "records_per_cell": 5}
DESIGN_SUPPLEMENT_S7EMPTY = {"sample_id": "S7-empty", "groups": ("D",),
                             "batches": ("suppl-s7empty",),
                             "records_per_cell": 5}
DESIGN_GENERATED_TOTAL = 165               # §2.4 140 + 25
DESIGN_BLIND_FORMAL = 84                   # §2.4 / §7.2
DESIGN_BLIND_SUPPLEMENT = 23               # §2.4（25 份源输出里实际纳入 23）
DESIGN_DATA_KIND = "simulated_learner_fixture"        # §7.1
B0_BATCH = "b0-legacy"
B0_LEGACY_SAMPLES = ("S5", "S7")           # §1.3 / §2.4，B0 只跑这两格
B0_LEGACY_RECORDS = 10
RESULTS_FILE_NAME = "results.jsonl"
TOTAL_BATCHES = ("main", "suppl-subj", "suppl-s7empty", B0_BATCH)

# §7.1 生成记录字段（全部必填；值为 None 也算存在）
RESULTS_REQUIRED_FIELDS = (
    "run_id", "experiment_id", "sample_id", "condition", "group",
    "repeat_index", "round_index", "plan_seq", "seed", "scheme",
    "algorithm_version", "plan_sha256", "batch", "model", "base_sha",
    "prompt_sha256", "messages_path", "knowledge_result_ids",
    "memory_injected", "short_term_injected", "difficult_flag", "output_raw",
    "answer", "plan", "validation", "usage", "latency_ms", "error",
    "data_kind",
)
RESULTS_INT_FIELDS = ("repeat_index", "round_index", "plan_seq", "seed", "latency_ms")
RESULTS_STR_FIELDS = ("run_id", "experiment_id", "sample_id", "group", "batch",
                      "scheme", "algorithm_version", "base_sha",
                      "prompt_sha256", "messages_path", "data_kind")

LEVELS = ("FAIL", "WARNING", "UNKNOWN", "PASS")


@dataclass
class Finding:
    level: str
    area: str
    target: str
    expected: str
    actual: str
    reason: str
    basis: str

    def render(self) -> str:
        return (f"[{self.level}] {self.area}｜{self.target}\n"
                f"    依据：{self.basis}\n"
                f"    预期：{self.expected}\n"
                f"    实际：{self.actual}\n"
                f"    原因：{self.reason}")


@dataclass
class Report:
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def add(self, level, area, target, expected, actual, reason, basis):
        self.findings.append(Finding(level, area, target, expected, actual,
                                     reason, basis))

    def count(self, level):
        return sum(1 for item in self.findings if item.level == level)

    def has_fail(self):
        return self.count("FAIL") > 0

    def render(self, header):
        lines = [header, ""]
        lines.extend(self.notes)
        if self.notes:
            lines.append("")
        for level in LEVELS:
            items = [item for item in self.findings if item.level == level]
            if not items:
                continue
            lines.append(f"## {level}（{len(items)} 项）")
            for item in items:
                lines.append(item.render())
            lines.append("")
        lines.append("## 汇总")
        lines.append(f"PASS {self.count('PASS')} / WARNING {self.count('WARNING')} / "
                     f"UNKNOWN {self.count('UNKNOWN')} / FAIL {self.count('FAIL')}")
        lines.append("结论：" + ("存在 FAIL，数据不得进入正式统计。" if self.has_fail()
                                 else "无 FAIL；WARNING/UNKNOWN 项需负责人确认。"))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def check_manifest(report, manifest_path, repo_root):
    """清单声明的文件是否存在，大小/哈希/记录数是否与磁盘一致。返回已核对的路径集合。"""

    area = "清单"
    basis = "docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv（可信清单）"
    verified = set()
    if not manifest_path.is_file():
        report.add("FAIL", area, str(manifest_path), "清单文件存在",
                   "不存在", "缺少可信清单，无法核对文件完整性", basis)
        return verified
    try:
        entries = manifest_rules.load_manifest(manifest_path)
    except manifest_rules.ManifestError as error:
        report.add("FAIL", area, str(manifest_path), "清单结构与列定义合法",
                   f"解析失败：{error}", "清单不可信，后续核对全部失效", basis)
        return verified

    if not entries:
        report.add("FAIL", area, str(manifest_path), "至少有一条清单条目",
                   "0 条", "空清单无法作为可信来源", basis)
        return verified

    for relative, entry in sorted(entries.items()):
        path = (repo_root / relative).resolve()
        verified.add(path)
        target = relative
        if not path.is_file():
            report.add("FAIL", area, target, "文件存在", str(path),
                       "清单声明的文件缺失", basis)
            continue
        actual_size = path.stat().st_size
        declared_size = entry.get("size_bytes")
        if str(actual_size) != str(declared_size).strip():
            report.add("FAIL", area, target, f"size_bytes={declared_size}",
                       f"size_bytes={actual_size}", "文件大小与清单不符", basis)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        declared = str(entry.get("sha256", "")).strip().lower()
        if digest != declared:
            report.add("FAIL", area, target, f"sha256={declared}",
                       f"sha256={digest}", "文件 SHA-256 与清单不符", basis)
        declared_records = str(entry.get("records", "")).strip()
        if path.name == plan_schedule.PLAN_NAME:
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
                actual_records = str(len(plan_schedule.plan_tasks(plan)))
            except (OSError, ValueError, KeyError, TypeError) as error:
                report.add("FAIL", area, target, "可解析的 plan.json",
                           f"解析失败：{error}", "计划文件损坏", basis)
                continue
        else:
            actual_records = str(_count_jsonl(path))
        if declared_records and declared_records != actual_records:
            report.add("FAIL", area, target, f"records={declared_records}",
                       f"records={actual_records}", "记录数与清单不符", basis)
        report.add("PASS", area, target, "存在、大小/哈希/记录数与清单一致",
                   "一致", "清单条目可核对", basis)
    return verified


def _count_jsonl(path):
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            count += 1
    return count


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


def check_plan(report, plan_path, expected_identities, label, *, strict=True):
    """plan.json 完整性 + 冻结参数 + 任务身份集合。返回 plan 或 None。

    strict=False 用于设计 §2.4 之外、不受五轮冻结参数约束的目录（例如冒烟批次）：
    参数偏离冻结值只报 WARNING（需负责人确认），不直接判 FAIL。
    """

    area = "计划"
    basis = "设计 §5.1（冻结参数）/ §5.3（plan.json 是执行顺序唯一依据）"
    if not plan_path.is_file():
        report.add("FAIL", area, label, "plan.json 存在", "不存在",
                   "缺少计划文件，无法核对执行顺序与身份", basis)
        return None
    try:
        plan = plan_schedule.load_plan(plan_path)
    except plan_schedule.PlanIntegrityError as error:
        report.add("FAIL", area, label, "plan_sha256 自洽", str(error),
                   "计划被改动或损坏", basis)
        return None

    deviations = []
    for field_name, expected in (
        ("seed", DESIGN_SEED), ("scheme", DESIGN_SCHEME),
        ("algorithm_version", DESIGN_ALGORITHM_VERSION),
        ("block_unit", DESIGN_BLOCK_UNIT), ("round_count", DESIGN_ROUND_COUNT),
    ):
        actual = plan.get(field_name)
        if actual != expected:
            deviations.append(f"{field_name}={actual!r}（预期 {expected!r}）")
    if deviations and strict:
        report.add("FAIL", area, f"{label} · 冻结参数", "§5.1 冻结参数",
                   "；".join(deviations), "冻结参数被改动", basis)
    elif deviations:
        report.add("WARNING", area, f"{label} · 冻结参数", "§5.1 冻结参数",
                   "；".join(deviations),
                   "该批次不在设计 §2.4 的 165 份内（如冒烟批次），其轮数/参数需负责人确认",
                   basis)

    tasks = plan_schedule.plan_tasks(plan)
    sequences = [task.get("plan_seq") for task in tasks]
    if sequences != list(range(len(tasks))):
        report.add("FAIL", area, f"{label} · plan_seq", "0..N-1 连续",
                   f"不连续/重复（N={len(tasks)}）", "计划序号断裂", basis)
    bad_round = [task for task in tasks
                 if task.get("repeat_index") != task.get("round_index")]
    if bad_round:
        report.add("FAIL", area, f"{label} · repeat_index",
                   "repeat_index == round_index", f"{len(bad_round)} 个任务不等",
                   "与设计 §5.3 冲突", basis)

    identities = plan_schedule.plan_identities(plan)
    if expected_identities is not None:
        missing = sorted(expected_identities - identities)
        extra = sorted(identities - expected_identities)
        if missing or extra:
            report.add("FAIL", area, f"{label} · 身份集合",
                       f"{len(expected_identities)} 个身份",
                       f"缺 {len(missing)}、多 {len(extra)}",
                       f"缺：{missing[:5]}；多：{extra[:5]}", basis)
        else:
            report.add("PASS", area, f"{label} · 身份集合",
                       f"{len(expected_identities)} 个身份", "完全一致",
                       "计划覆盖符合冻结设计", basis)
    return plan


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------


def check_results(report, results_path, plan, expected_batches):
    """逐行核对 results.jsonl 的字段、身份、重复与计划同源关系。返回身份列表。"""

    area = "结果记录"
    basis = "设计 §7.1（记录字段）/ §5.3（身份四元组）/ §2.4（覆盖）"
    label = str(results_path)
    identities = []
    seen = {}
    plan_ids = plan_schedule.plan_identities(plan) if plan else None
    plan_sha = plan.get("plan_sha256") if plan else None

    try:
        text = results_path.read_text(encoding="utf-8")
    except OSError as error:
        report.add("FAIL", area, label, "可读取", str(error), "文件不可读", basis)
        return identities

    batch_values = set()
    legacy_plan_warned = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            report.add("FAIL", area, f"{label}:{lineno}", "非空行", "空行",
                       "结果文件出现空行，可能被截断或手工编辑", basis)
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            report.add("FAIL", area, f"{label}:{lineno}", "合法 JSON", str(error),
                       "损坏行无法解析", basis)
            continue
        if not isinstance(row, dict):
            report.add("FAIL", area, f"{label}:{lineno}", "JSON 对象",
                       type(row).__name__, "记录格式错误", basis)
            continue

        missing = [name for name in RESULTS_REQUIRED_FIELDS if name not in row]
        if missing:
            report.add("FAIL", area, f"{label}:{lineno}", "§7.1 全部字段",
                       f"缺 {missing}", "字段缺失，无法进入统计", basis)
            continue
        for name in RESULTS_INT_FIELDS:
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                report.add("FAIL", area, f"{label}:{lineno} · {name}",
                           "int（不能是 bool）", repr(value), "字段类型错误", basis)
        for name in RESULTS_STR_FIELDS:
            value = row.get(name)
            if not isinstance(value, str) or not value.strip():
                report.add("FAIL", area, f"{label}:{lineno} · {name}",
                           "非空字符串", repr(value), "字段类型/取值错误", basis)

        batch_values.add(row.get("batch"))
        if row.get("data_kind") != DESIGN_DATA_KIND:
            report.add("FAIL", area, f"{label}:{lineno} · data_kind",
                       DESIGN_DATA_KIND, repr(row.get("data_kind")),
                       "数据来源标记与设计不符", basis)
        if row.get("error") is not None:
            report.add("FAIL", area, f"{label}:{lineno} · error", "null",
                       repr(row.get("error"))[:120],
                       "存在失败记录，不能当作完成数据统计", basis)

        identity = (row.get("experiment_id"), row.get("sample_id"),
                    row.get("group"), row.get("repeat_index"))
        if identity in seen:
            report.add("FAIL", area, f"{label}:{lineno} · 身份",
                       "四元组唯一",
                       f"与第 {seen[identity]} 行重复：{identity}",
                       "同一实验身份出现多条记录", basis)
        else:
            seen[identity] = lineno
            identities.append(identity)

        plan_sha_value = row.get("plan_sha256")
        if plan is not None:
            if plan_sha_value != plan_sha:
                report.add("FAIL", area, f"{label}:{lineno} · plan_sha256",
                           plan_sha, repr(plan_sha_value),
                           "结果行与同目录 plan.json 不同源", basis)
            if plan_ids is not None and identity not in plan_ids:
                report.add("FAIL", area, f"{label}:{lineno} · 身份",
                           "属于 plan.json 的任务", repr(identity),
                           "结果行不在计划中", basis)
        elif not _is_sha256(plan_sha_value) and not legacy_plan_warned:
            legacy_plan_warned = True
            report.add("WARNING", area, f"{label} · plan_sha256",
                       "64 位十六进制（legacy 记录可能缺失）", repr(plan_sha_value),
                       "该目录没有 plan.json（设计 §5.3 的 legacy 例外），记录缺 plan_sha256 时"
                       "无法核对同源，需负责人确认", basis)

    if plan_ids is not None:
        missing_ids = sorted(plan_ids - set(identities))
        if missing_ids:
            report.add("FAIL", area, f"{label} · 计划完成度",
                       f"{len(plan_ids)} 个计划身份", f"缺 {len(missing_ids)}",
                       f"未完成：{missing_ids[:5]}", basis)
        else:
            report.add("PASS", area, f"{label} · 计划完成度",
                       f"{len(plan_ids)} 个计划身份", "全部有结果",
                       "计划任务全部完成", basis)

    if expected_batches is not None and batch_values:
        unexpected = sorted(str(item) for item in batch_values if item not in expected_batches)
        if unexpected:
            report.add("FAIL", area, f"{label} · batch", str(sorted(expected_batches)),
                       str(unexpected), "批次与目录职责不符", basis)
    return identities


# ---------------------------------------------------------------------------
# 覆盖
# ---------------------------------------------------------------------------


def expected_main_identities(experiment_id=None):
    """主实验 28 格 × 5 轮 = 140 个身份的「相对身份」集合（不含 experiment_id）。"""

    return {(sample, group, repeat)
            for sample in DESIGN_MAIN_SAMPLES
            for group in DESIGN_MAIN_GROUPS
            for repeat in range(DESIGN_ROUND_COUNT)}


def relative_identities(identities):
    return {(row[1], row[2], row[3]) for row in identities if isinstance(row[3], int)}


def check_coverage(report, batch, identities, experiment_ids):
    """按设计 §2.4 核对每个批次的样本×条件×重复覆盖。"""

    area = "覆盖"
    basis = "设计 §2.4（主实验 140 / 补充 25 / 生成任务 165）"
    relative = relative_identities(identities)
    if len(experiment_ids) > 1:
        report.add("FAIL", area, f"批次 {batch} · experiment_id",
                   "1 个", f"{len(experiment_ids)} 个：{sorted(experiment_ids)}",
                   "同一批次混入不同实验身份", basis)

    if batch == "main":
        expected = expected_main_identities()
        _report_set(report, area, f"批次 main", expected, relative, basis,
                    "主实验 7 样本 × A/B/C/D × 5 轮")
    elif batch == "suppl-subj":
        expected = {(DESIGN_SUPPLEMENT_SUBJ["sample_id"], group, repeat)
                    for group in DESIGN_SUPPLEMENT_SUBJ["groups"]
                    for repeat in range(DESIGN_ROUND_COUNT)}
        _report_set(report, area, "批次 suppl-subj", expected, relative, basis,
                    "S5 × C-subj/D-subj × 5 轮")
    elif batch == "suppl-s7empty":
        expected = {(DESIGN_SUPPLEMENT_S7EMPTY["sample_id"], group, repeat)
                    for group in DESIGN_SUPPLEMENT_S7EMPTY["groups"]
                    for repeat in range(DESIGN_ROUND_COUNT)}
        _report_set(report, area, "批次 suppl-s7empty", expected, relative, basis,
                    "S7-empty × D × 5 轮")
    elif batch == B0_BATCH:
        expected = {(sample, B0_BATCH, repeat)
                    for sample in B0_LEGACY_SAMPLES
                    for repeat in range(DESIGN_ROUND_COUNT)}
        if relative and relative != expected:
            report.add("WARNING", area, "批次 b0-legacy",
                       "S5/S7 × B0 × 5 = 10（legacy 探索组）",
                       f"{len(relative)} 个身份",
                       "B0 是 legacy 探索组，设计未把它纳入五轮主计划；差异需负责人确认",
                       basis)
        elif relative:
            report.add("PASS", area, "批次 b0-legacy", "10 个 legacy 身份",
                       "一致", "legacy 探索组覆盖完整", basis)
    else:
        report.add("UNKNOWN", area, f"批次 {batch}", "设计 §2.4 规定的批次",
                   repr(batch), "设计未规定该批次的期望覆盖，需负责人确认", basis)


def _report_set(report, area, label, expected, actual, basis, desc):
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        report.add("FAIL", area, label, f"{len(expected)} 个身份（{desc}）",
                   f"{len(actual)} 个：缺 {len(missing)}、多 {len(extra)}",
                   f"缺：{missing[:5]}；多：{extra[:5]}", basis)
    else:
        report.add("PASS", area, label, f"{len(expected)} 个身份（{desc}）",
                   "完全覆盖", "覆盖符合冻结设计", basis)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_preflight(runs_dir, manifest_path, repo_root):
    report = Report()
    report.notes.append(f"runs-dir: {runs_dir}")
    report.notes.append(f"manifest: {manifest_path}")
    report.notes.append(f"repo-root: {repo_root}")
    report.notes.append("本工具只读：不写实验数据、不生成盲评包、不调用模型/Embedding API。")

    verified = check_manifest(report, manifest_path, repo_root)

    if not runs_dir.is_dir():
        report.add("FAIL", "目录", str(runs_dir), "runs 目录存在", "不存在",
                   "无法核对实验数据", "设计 §5.3 运行目录约定")
        return report

    run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    if not run_dirs:
        report.add("FAIL", "目录", str(runs_dir), "至少一个运行目录", "0 个",
                   "没有可核对的实验数据", "设计 §5.3")
        return report

    fingerprint_before = {path: _fingerprint(path) for path in verified
                          if path.is_file()}
    by_batch = {}
    for directory in run_dirs:
        plan_path = directory / plan_schedule.PLAN_NAME
        results_path = directory / RESULTS_FILE_NAME
        label = directory.name
        if directory.resolve() in {path.parent for path in verified}:
            report.add("PASS", "目录", label, "已纳入清单", "已纳入",
                       "该目录的文件完整性由清单核对", "docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv")
        else:
            report.add("WARNING", "目录", label, "已纳入清单", "未纳入",
                       "未纳入清单的目录不做哈希核对；若是 B0 legacy 则按设计保留，"
                       "其他情况需负责人确认", "设计 §2.4（B0 legacy）")

        if not results_path.is_file():
            if plan_path.is_file():
                report.add("FAIL", "目录", label, "results.jsonl 存在", "缺失",
                           "有计划但没有结果文件", "设计 §5.3")
            continue

        batch = _infer_batch(directory, results_path)
        design_batch = batch in TOTAL_BATCHES
        plan = None
        if plan_path.is_file():
            plan = check_plan(report, plan_path, None, label, strict=design_batch)
        else:
            report.add("WARNING", "计划", label, "plan.json 存在", "缺失",
                       "无计划的目录按 legacy 处理；若需要续跑会被拒绝",
                       "设计 §5.3")

        identities = check_results(report, results_path, plan,
                                   expected_batches=None)
        experiment_ids = {row[0] for row in identities}
        if batch is None:
            report.add("UNKNOWN", "覆盖", label, "可判定的批次", "无法判定",
                       "无法从目录名或记录判断批次，需负责人确认", "设计 §2.4")
        else:
            by_batch.setdefault(batch, []).extend(identities)
            check_coverage(report, batch, identities, experiment_ids)

    total = sum(len(by_batch.get(batch, [])) for batch in TOTAL_BATCHES)
    if by_batch:
        excluded = {batch: len(items) for batch, items in by_batch.items()
                    if batch not in TOTAL_BATCHES}
        if excluded:
            report.notes.append("未计入 §2.4 的 165 份的批次：" + "、".join(
                f"{batch}={count}" for batch, count in sorted(excluded.items())))
        expected_total = DESIGN_MAIN_RECORDS + 25
        if total == expected_total:
            report.add("PASS", "汇总", "生成任务总数", f"{expected_total}",
                       f"{total}", "与设计 §2.4 一致", "设计 §2.4")
        else:
            report.add("FAIL", "汇总", "生成任务总数", f"{expected_total}",
                       f"{total}", "生成任务数与冻结设计不一致", "设计 §2.4")
        for batch, items in sorted(by_batch.items()):
            report.notes.append(f"批次 {batch}：{len(items)} 条记录")

    changed = [str(path) for path, digest in fingerprint_before.items()
               if path.is_file() and _fingerprint(path) != digest]
    if changed:
        report.add("FAIL", "只读自检", "运行期间输入文件未被改动", "无改动",
                   f"{len(changed)} 个文件变化：{changed[:5]}",
                   "预检过程中输入被改动，结果不可信", "只读约定")
    else:
        report.add("PASS", "只读自检", "运行期间输入文件未被改动", "无改动",
                   "无改动", "输入哈希前后一致", "只读约定")
    return report


def _is_sha256(value):
    """64 位十六进制字符串（大小写不敏感）才算一个 SHA-256 值。"""

    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _infer_batch(directory, results_path):
    """批次判定：优先读记录里的 batch 字段，其次按目录名前缀。"""

    try:
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and isinstance(row.get("batch"), str):
                return row["batch"]
    except (OSError, ValueError):
        pass
    name = directory.name
    for prefix, batch in (("main-", "main"), ("suppl-subj-", "suppl-subj"),
                          ("suppl-s7empty-", "suppl-s7empty"),
                          ("b0-", B0_BATCH), ("internal-api-smoke-", "smoke")):
        if name.startswith(prefix):
            return batch
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="正式实验数据只读完整性预检（不写实验数据）")
    parser.add_argument("--runs-dir", type=Path,
                        default=REPO_ROOT / "experiments" / "graduation" / "runs")
    parser.add_argument("--manifest", type=Path,
                        default=REPO_ROOT / "docs" /
                        "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", type=Path, default=None,
                        help="可选：把机器可读结果写到该路径（会覆盖同名文件）")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总行")
    args = parser.parse_args(argv)

    report = run_preflight(args.runs_dir, args.manifest, args.repo_root)
    header = "实验数据只读预检报告"
    text = report.render(header)
    if not args.quiet:
        print(text)
    else:
        print("PASS %d / WARNING %d / UNKNOWN %d / FAIL %d" % (
            report.count("PASS"), report.count("WARNING"),
            report.count("UNKNOWN"), report.count("FAIL")))

    if args.json is not None:
        payload = {
            "runs_dir": str(args.runs_dir),
            "manifest": str(args.manifest),
            "repo_root": str(args.repo_root),
            "counts": {level.lower(): report.count(level) for level in LEVELS},
            "findings": [
                {"level": item.level, "area": item.area, "target": item.target,
                 "expected": item.expected, "actual": item.actual,
                 "reason": item.reason, "basis": item.basis}
                for item in report.findings
            ],
            "notes": report.notes,
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return 1 if report.has_fail() else 0


if __name__ == "__main__":
    sys.exit(main())
