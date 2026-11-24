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
- **文件级**清单覆盖：每个运行目录的 plan.json 与 results.jsonl 要分别确认已纳入清单，
  不能用「目录里有一个文件在清单里」代表整个目录可信；B0 legacy 按设计例外单独处理；
- **文件级信任**是下游结论的前提：清单哈希/大小/记录数不一致或正式文件未纳入清单时，
  该文件不得产生计划完成度、批次覆盖或 165 份总量的 PASS（结果仍解析用于诊断）；
- plan.json 的 plan_sha256 是否自洽，冻结参数（seed/scheme/algorithm_version/
  block_unit/round_count）是否与设计 §5.1 一致；
- results.jsonl 是否可解析、字段是否齐全、身份是否重复、行是否属于计划、
  plan_sha256 是否与同目录计划同源；batch 归属以清单（relative_path→batch）为唯一
  可信来源，目录名只做一致性校验（B0 legacy 例外），记录声明只做交叉验证：
  清单缺失/非法、清单与目录冲突、记录与可信批次不符一律 FAIL，三者都没有才 UNKNOWN；
- 字段类型畸形（list/dict/null 等）只形成结构化 FAIL，不再让集合/身份比较抛异常；
- 覆盖是否与冻结设计一致：主实验 7 样本 × A/B/C/D × 5 轮 = 140 份，补充
  C/D-subj（S5）10 份、S7-empty × D 5 份，B0 legacy 10 份只记录；
- 运行前后对 **manifest 自身 + 所有实际读取的 plan.json/results.jsonl** 重算哈希。

只读约定：本工具只打开文件进行读取，不写、不改、不生成任何实验数据；唯一的写操作
是把报告写到显式给出的 `--json` 路径（可选）。

报告分级：
- PASS：实际值与冻结设计/清单一致；
- WARNING：设计未规定或无法从现有证据判定的地方（例如未纳入清单的 B0 legacy），
  需要负责人确认；
- FAIL：与冻结设计/清单冲突，或数据本身损坏，这些情况不得进入正式统计；
- UNKNOWN：证据不足（无法判定批次等）。

退出码：0 = 无 FAIL；1 = 存在 FAIL；2 = 参数错误。
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import manifest_rules  # noqa: E402
import plan_schedule  # noqa: E402
import result_schema  # noqa: E402

# --- 冻结口径（docs/GRADUATION-EXPERIMENT-DESIGN.md） -----------------------
DESIGN_SEED = 20261113                     # §5.1 冻结 seed
DESIGN_SCHEME = "five_round_randomized_block_v1"      # §5.1
DESIGN_ALGORITHM_VERSION = "sha256-block-order-v1"    # §5.1
DESIGN_BLOCK_UNIT = "sample"               # §5.1
DESIGN_ROUND_COUNT = 5                     # §5.1 / §5.3
DESIGN_MAIN_SAMPLES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")   # §2.4
DESIGN_MAIN_GROUPS = ("A", "B", "C", "D")                          # §2.4
DESIGN_MAIN_RECORDS = 140                  # §2.4 28 格 × 5 轮
DESIGN_SUPPLEMENT_RECORDS = 25             # §2.4 补充源输出 25 份
DESIGN_GENERATED_TOTAL = 165               # §2.4 140 + 25
DESIGN_DATA_KIND = "simulated_learner_fixture"        # §7.1
B0_BATCH = "b0-legacy"
B0_LEGACY_SAMPLES = ("S5", "S7")           # §1.3 / §2.4，B0 只跑这两格
B0_LEGACY_RECORDS = 10
RESULTS_FILE_NAME = "results.jsonl"
TOTAL_BATCHES = ("main", "suppl-subj", "suppl-s7empty", B0_BATCH)
# 目录名前缀 → 批次（可信规则；不依赖记录内容，避免第一条记录决定整批归属）
BATCH_PREFIXES = (
    ("internal-api-smoke-", "smoke"),
    ("suppl-s7empty-", "suppl-s7empty"),
    ("suppl-subj-", "suppl-subj"),
    ("main-", "main"),
    ("b0-", B0_BATCH),
)
SUPPLEMENT_S7EMPTY = {"sample_id": "S7-empty", "groups": ("D",),
                      "batches": ("suppl-s7empty",), "records_per_cell": 5}
SUPPLEMENT_SUBJ = {"sample_id": "S5", "groups": ("C-subj", "D-subj"),
                   "batches": ("suppl-subj",), "records_per_cell": 5}

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

# 生产器（run_experiment.py）记录与 §7.1 的差异只能通过 result_schema 的确定性规则
# 转换成设计视图；转换不出来的字段一律 FAIL，不降级为 WARNING。
CONTRACT_BASIS = ("设计 §7.1（冻结记录结构）/ result_schema.FIELD_CONTRACT"
                 "（生产器 run_experiment.py 字段取证）")

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


@dataclass(frozen=True)
class ManifestIndex:
    """清单核对结果：文件覆盖集合 + 每个文件的清单批次。

    batches 是规范化相对路径（解析后的绝对路径）→ batch，是批次归属的唯一可信来源；
    batch_problems 记录「清单里有这个文件、但没有合法 batch」的情况（该列缺失或为空），
    调用方据此对正式文件报 FAIL，而不是回退到记录自述。
    """

    declared: set
    failed: set
    batches: dict
    batch_problems: dict


def check_manifest(report, manifest_path, repo_root):
    """清单声明的文件是否存在，大小/哈希/记录数是否与磁盘一致。

    返回 ManifestIndex（覆盖集合 + 绝对路径→清单 batch）。**同一文件只要有一项不一致，
    就不再补 PASS**：报告的 PASS/FAIL 必须互斥，否则「哈希对不上」和「清单条目可核对」
    会同时出现。
    """

    area = "清单"
    basis = "docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv（可信清单）"
    declared_paths = set()
    failed_paths = set()
    batches = {}
    batch_problems = {}
    if not manifest_path.is_file():
        report.add("FAIL", area, str(manifest_path), "清单文件存在",
                   "不存在", "缺少可信清单，无法核对文件完整性", basis)
        return ManifestIndex(declared_paths, failed_paths, batches, batch_problems)
    try:
        entries = manifest_rules.load_manifest(manifest_path)
    except manifest_rules.ManifestError as error:
        report.add("FAIL", area, str(manifest_path), "清单结构与列定义合法",
                   f"解析失败：{error}", "清单不可信，后续核对全部失效", basis)
        return ManifestIndex(declared_paths, failed_paths, batches, batch_problems)

    if not entries:
        report.add("FAIL", area, str(manifest_path), "至少有一条清单条目",
                   "0 条", "空清单无法作为可信来源", basis)
        return ManifestIndex(declared_paths, failed_paths, batches, batch_problems)

    for relative, entry in sorted(entries.items()):
        path = (repo_root / relative).resolve()
        declared_paths.add(path)
        raw_batch = entry.get("batch")
        batch_text = "" if raw_batch is None else str(raw_batch).strip()
        if batch_text:
            batches[path] = batch_text
        elif raw_batch is None:
            batch_problems[path] = "清单没有 batch 列"
        else:
            batch_problems[path] = "清单条目的 batch 为空"
        target = relative
        if not path.is_file():
            report.add("FAIL", area, target, "文件存在", str(path),
                       "清单声明的文件缺失", basis)
            failed_paths.add(path)
            continue
        entry_ok = True
        actual_size = path.stat().st_size
        declared_size = entry.get("size_bytes")
        if str(actual_size) != str(declared_size).strip():
            report.add("FAIL", area, target, f"size_bytes={declared_size}",
                       f"size_bytes={actual_size}", "文件大小与清单不符", basis)
            entry_ok = False
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        declared = str(entry.get("sha256", "")).strip().lower()
        if digest != declared:
            report.add("FAIL", area, target, f"sha256={declared}",
                       f"sha256={digest}", "文件 SHA-256 与清单不符", basis)
            entry_ok = False
        declared_records = str(entry.get("records", "")).strip()
        if path.name == plan_schedule.PLAN_NAME:
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
                actual_records = str(len(plan_schedule.plan_tasks(plan)))
            except (OSError, ValueError, KeyError, TypeError) as error:
                report.add("FAIL", area, target, "可解析的 plan.json",
                           f"解析失败：{error}", "计划文件损坏", basis)
                failed_paths.add(path)
                continue
        else:
            actual_records = str(_count_jsonl(path))
        if declared_records and declared_records != actual_records:
            report.add("FAIL", area, target, f"records={declared_records}",
                       f"records={actual_records}", "记录数与清单不符", basis)
            entry_ok = False
        if entry_ok:
            report.add("PASS", area, target, "存在、大小/哈希/记录数与清单一致",
                       "一致", "清单条目可核对", basis)
        else:
            failed_paths.add(path)
    return ManifestIndex(declared_paths, failed_paths, batches, batch_problems)


def _count_jsonl(path):
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            count += 1
    return count


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


PLAN_STATE_MISSING = "missing"
PLAN_STATE_VALID = "valid"
PLAN_STATE_INVALID = "invalid"
PLAN_STATE_UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class PlanCheck:
    """plan.json 的四态核验结果：missing / valid / invalid / untrusted。

    `invalid` 时 `plan` 可能是已载入的计划（类型/结构/身份问题），也可能是 None
    （文件损坏、plan_sha256 不自洽）。`untrusted` 表示文件存在，但没有通过可信清单的
    完整性核验（SHA-256/大小/记录数不一致，或正式文件未纳入清单）：清单 FAIL 已经
    由 check_manifest()/清单覆盖检查给出，本状态只负责阻止计划内容进入下游。

    结果侧只能使用 `valid` 的计划：无效或不可信计划不得建立身份索引、不得做计划
    成员关系或完成度核验，也不能被当作「没有 plan.json」的 legacy 目录而给出
    legacy WARNING。
    """

    state: str
    plan: dict | None = None
    detail: str = ""

    @property
    def valid(self):
        return self.state == PLAN_STATE_VALID


def check_plan(report, plan_path, expected_identities, label, *, strict=True):
    """plan.json 完整性 + 冻结参数 + 任务身份集合。返回 PlanCheck 三态结果。

    `strict=False` 用于设计 §2.4 之外、不受五轮冻结参数约束的目录（例如冒烟批次）：
    参数偏离冻结值只报 WARNING（需负责人确认），不直接判 FAIL。
    """

    area = "计划"
    basis = "设计 §5.1（冻结参数）/ §5.3（plan.json 是执行顺序唯一依据）"
    if not plan_path.is_file():
        report.add("FAIL", area, label, "plan.json 存在", "不存在",
                   "缺少计划文件，无法核对执行顺序与身份", basis)
        return PlanCheck(PLAN_STATE_MISSING, None, "plan.json 不存在")
    try:
        plan = plan_schedule.load_plan(plan_path)
    except plan_schedule.PlanIntegrityError as error:
        report.add("FAIL", area, label, "plan_sha256 自洽", str(error),
                   "计划被改动或损坏", basis)
        return PlanCheck(PLAN_STATE_INVALID, None,
                         f"plan_sha256 不自洽：{error}")

    # --- 严格类型：必须早于任何等值比较/身份索引/唯一性 PASS ---
    # Python 里 1.0 == 1、True == 1：改写成等值浮点或布尔再重算 plan_sha256，
    # 所有 == 比较都会放行。类型不合法时直接终止计划侧的后续核验，
    # 不生成身份索引，也不给出「身份唯一」「身份集合」的 PASS。
    type_problems = plan_schedule.plan_type_problems(plan)
    if type_problems:
        shown = "；".join(type_problems[:5])
        if len(type_problems) > 5:
            shown += f"（共 {len(type_problems)} 处）"
        report.add("FAIL", area, f"{label} · 计划类型",
                   "schema_version/seed/round_count 为整数（非 bool）；"
                   "每个任务的 plan_seq/round_index/repeat_index 为整数（非 bool）、"
                   "group 为非空字符串",
                   shown,
                   "plan.json 字段类型不合法：等值比较不能证明类型正确，"
                   "序号/身份唯一性/身份集合核验中止", basis)
        return PlanCheck(PLAN_STATE_INVALID, plan, "计划类型不合法")

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

    invalid_reasons = []
    if deviations and strict:
        invalid_reasons.append("冻结参数偏离")

    # --- 结构完整性：先把「每个任务的身份」展开，结构不全就不再往下给结论 ---
    # plan_tasks()/plan_identities() 直接索引 plan["blocks"]，结构缺失会抛异常，
    # 集合推导还会把重复身份折叠成一个 —— 两者都不能用来做唯一性判定。
    entries, structure_problems = plan_schedule.plan_identity_entries(plan)
    if structure_problems:
        shown = "；".join(structure_problems[:5])
        if len(structure_problems) > 5:
            shown += f"（共 {len(structure_problems)} 处）"
        report.add("FAIL", area, f"{label} · 计划结构",
                   "blocks/tasks 与身份字段（sample_id/group/repeat_index）齐备",
                   shown,
                   "计划结构不完整，无法为每个任务建立身份；序号与身份唯一性核验中止，不生成看似完整的索引", basis)
        return PlanCheck(PLAN_STATE_INVALID, plan, "计划结构不完整")

    sequences = [entry["plan_seq"] for entry in entries]
    if sequences != list(range(len(entries))):
        report.add("FAIL", area, f"{label} · plan_seq", "0..N-1 连续",
                   f"不连续/重复（N={len(entries)}）", "计划序号断裂", basis)
        invalid_reasons.append("plan_seq 不连续")
    bad_round = [entry for entry in entries
                 if entry["repeat_index"] != entry["round_index"]]
    if bad_round:
        report.add("FAIL", area, f"{label} · repeat_index",
                   "repeat_index == round_index", f"{len(bad_round)} 个任务不等",
                   "与设计 §5.3 冲突", basis)
        invalid_reasons.append("repeat_index 与 round_index 不一致")

    # --- 身份唯一性：同一身份出现在两个 plan_seq 上，结果行就无法映射到唯一任务 ---
    duplicates = plan_schedule.plan_identity_duplicates(entries)
    for identity, items in duplicates:
        seqs = "、".join(repr(item["plan_seq"]) for item in items)
        locations = "、".join(item["where"] for item in items)
        report.add("FAIL", area, f"{label} · 身份唯一性",
                   "每个实验身份（experiment_id, sample_id, group, repeat_index）只出现一次",
                   f"重复 {len(items)} 次：{list(identity)}；plan_seq={seqs}",
                   f"同一身份对应多个计划任务（{locations}），结果行匹配其中任意一条都不能证明计划合法，执行顺序不可审计", basis)
    if duplicates:
        invalid_reasons.append(f"{len(duplicates)} 个重复身份")
    if not duplicates:
        report.add("PASS", area, f"{label} · 身份唯一性",
                   f"{len(entries)} 个任务身份唯一", "无重复",
                   "计划可逐身份审计", basis)

    identities = {entry["identity"] for entry in entries}
    if expected_identities is not None:
        missing = sorted(expected_identities - identities)
        extra = sorted(identities - expected_identities)
        if missing or extra:
            report.add("FAIL", area, f"{label} · 身份集合",
                       f"{len(expected_identities)} 个身份",
                       f"缺 {len(missing)}、多 {len(extra)}",
                       f"缺：{missing[:5]}；多：{extra[:5]}", basis)
            invalid_reasons.append("身份集合与冻结设计不一致")
        else:
            report.add("PASS", area, f"{label} · 身份集合",
                       f"{len(expected_identities)} 个身份", "完全一致",
                       "计划覆盖符合冻结设计", basis)
    if invalid_reasons:
        return PlanCheck(PLAN_STATE_INVALID, plan, "；".join(invalid_reasons))
    return PlanCheck(PLAN_STATE_VALID, plan, "计划核验通过")


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def check_results(report, results_path, plan, expected_batch=None,
                  batch_decision=None, *, plan_check=None, trusted=True):
    """逐行核对 results.jsonl 的字段、身份、批次、重复与计划同源关系。

    返回**字段类型合法且文件可信**的身份列表（畸形记录只形成 FAIL，不进入集合
    比较，否则 list/dict 值会让 set/dict 抛 TypeError，预检就崩了）。`trusted=False`
    表示文件没有通过可信清单的完整性核验：仍然解析并报告字段/身份/批次诊断，
    但不返回身份、不产生计划完成度结论 —— 行内容再正确也不能替代清单哈希核验。

    两种记录来源：

    - **设计原生行**（29 个 §7.1 字段齐全）：直接按字段校验，行为与改造前一致；
    - **生产器行**（run_experiment.py 写出的记录）：先经 result_schema 做确定性
      标准化，得到 §7.1 视图后再做同一套校验；转换不出来的字段按字段 FAIL，
      不可恢复字段（run_id/base_sha/...）同样 FAIL，不降级为 WARNING。

    对生产器行，「执行证据」与「§7.1 契约完整性」是两个独立维度：

    - 执行证据（这一行是否真的跑过）由身份四元组、批次归属、是否属于
      plan.json、以及结果行与计划任务是否同源来证明；只要这些字段能导出来就照常
      核对，与 7 个不可恢复字段缺不缺无关。同源核对按身份定位计划任务后逐字段比对
      plan_seq / seed / scheme / algorithm_version / plan_sha256（设计 §5.3）。
    - 契约完整性由 29 个字段是否齐备来判定，缺口逐字段 FAIL（按字段聚合，
      不逐行刷屏），并单独计数。

    因此结果行即使 `is_complete()` 为假也**不会**被整体跳过：缺口照报，
    身份照进集合比较与计划覆盖统计。反过来也不允许用「部分成功」换任何
    §7.1 合规 PASS。

    批次（batch）的可信来源只有清单与目录名（见 decide_batch()），记录声明只做交叉验证：

    - directory / manifest：批次已由目录名或清单确认，记录声明别的批次逐行 FAIL；
    - manifest-conflict：目录名与清单不一致 —— 文件级一条 FAIL，归属不可确定；
    - manifest-missing / manifest-invalid：正式文件缺清单 batch —— 文件级 FAIL，
      不得回退到记录自述；
    - untrusted-records：只有记录自述 —— 文件级 FAIL，不能据此归入任何正式批次；
    - missing：目录、清单、记录都没有批次 —— 不猜批次，由 run_preflight 报 UNKNOWN；
    - 同一文件内声明了多个批次 —— 每一行只要与可信批次不符就逐行 FAIL（有可信
      来源时不需要再判「整体矛盾」，归属已经确定）；非法值逐行一条 FAIL。

    `batch_decision` 为 None 时（直接调用的旧路径）按 expected_batch 退化成
    directory / missing 两态，行为与第五轮一致。

    `plan_check` 是 check_plan()/清单闸门给出的四态结果。只有 `valid` 计划参与
    身份索引、结果行成员关系、同源字段与计划完成度核验；`invalid` 与 `untrusted`
    计划保留文件级 FAIL，结果侧只继续做与计划无关的字段核验，并写一条「未做计划
    核验」的说明 —— 既不能建立索引，也不能被误当作没有 plan.json 的 legacy 而报
    WARNING。直接调用（不传 plan_check）时保持旧行为：plan 非 None 即按 valid 处理。

    `trusted=False` 时（正式结果文件在清单中哈希/大小/记录数不一致，或未纳入清单），
    结果行仍逐条解析并报告字段、身份、批次诊断，但不返回身份、不做计划完成度
    结论；调用方不得把这些身份计入 by_batch、覆盖或 165 份总量。
    """

    if batch_decision is None:
        batch_decision = BatchDecision(
            BATCH_STATE_DIRECTORY if expected_batch is not None
            else BATCH_STATE_MISSING,
            expected_batch,
            "" if expected_batch is not None else "调用方未给出批次，目录名也不可判定",
        )

    if plan_check is None:
        plan_state = PLAN_STATE_VALID if plan is not None else PLAN_STATE_MISSING
        plan_detail = ""
    else:
        plan_state = plan_check.state
        plan_detail = plan_check.detail
        plan = plan_check.plan
    plan_usable = plan_state == PLAN_STATE_VALID
    plan_missing = plan_state == PLAN_STATE_MISSING
    plan_untrusted = plan_state == PLAN_STATE_UNTRUSTED

    area = "结果记录"
    basis = "设计 §7.1（记录字段）/ §5.3（身份四元组）/ §2.4（覆盖与批次）"
    label = str(results_path)
    identities = []
    seen = {}
    plan_map = None
    plan_ids = None
    plan_sha = plan.get("plan_sha256") if plan else None
    # 计划任务索引：身份四元组 → 计划任务条目。设计 §5.3 要求 plan_seq 是计划的全局
    # 连续序号，seed/scheme/algorithm_version/plan_sha256 用来证明结果行与计划同源；这些
    # 比对必须按身份找到「那一条任务」再比，不能只看 plan_seq 是不是 int、在不在范围里。
    #
    # 同一身份出现多次时不能用 dict 覆盖掉一条：这里保留整个列表，重复身份既不建索引，
    # 也不挑其中任意一条来比对 —— 唯一性由 check_plan() 逐条 FAIL，结果侧不重复报；
    # 更不允许「结果行恰好匹配其中一条」把重复计划洗成合法。
    plan_task_by_identity = {}
    plan_identity_duplicates = {}
    plan_structure_problems = []
    if plan_usable:
        plan_map, plan_structure_problems = plan_schedule.plan_identity_map(plan)
        for plan_key, plan_entries in (plan_map or {}).items():
            if len(plan_entries) == 1:
                plan_task_by_identity[plan_key] = plan_entries[0]
            else:
                plan_identity_duplicates[plan_key] = plan_entries
        if plan_map is not None:
            plan_ids = set(plan_map)
    plan_seed = plan.get("seed") if plan is not None else None
    plan_scheme = plan.get("scheme") if plan is not None else None
    plan_algorithm_version = plan.get("algorithm_version") if plan is not None else None
    legacy_plan_warned = False
    invalid_rows = 0
    producer_rows = 0
    derived_rows = 0
    gap_rows = 0
    gap_fields = {}
    null_ok = set()
    failed_fields = set()
    result_rows = 0
    identified_rows = 0
    declared_seen = []   # 记录自己声明的合法 batch：(行号, 值)
    mismatches = []      # 与可信批次不符的记录声明：(行号, 值)

    def _warn_legacy_plan(value):
        """legacy 目录没有 plan.json：缺 plan_sha256 只报一次 WARNING（设计 §5.3 例外）。"""

        nonlocal legacy_plan_warned
        if legacy_plan_warned or _is_sha256(value):
            return
        legacy_plan_warned = True
        report.add("WARNING", area, f"{label} · plan_sha256",
                   "64 位十六进制（legacy 记录可能缺失）", repr(value),
                   "该目录没有 plan.json（设计 §5.3 的 legacy 例外），记录缺 plan_sha256 时"
                   "无法核对同源，需负责人确认", basis)

    try:
        text = results_path.read_text(encoding="utf-8")
    except OSError as error:
        report.add("FAIL", area, label, "可读取", str(error), "文件不可读", basis)
        return identities

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            report.add("FAIL", area, f"{label}:{lineno}", "非空行", "空行",
                       "结果文件出现空行，可能被截断或手工编辑", basis)
            continue
        result_rows += 1
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

        raw_row = row  # 原始记录：batch 这类字段必须按记录本来的样子核对
        null_ok = set()  # 只有生产器行会出现「设计值就是 null」的字段
        failed_fields = set()
        partial = False  # 生产器行：只有 §7.1 视图的一部分，核验按「可得即核」

        # --- 生产器记录：先按 §7.1 视图做确定性标准化 ---
        # run_experiment.py 从未写出 §7.1 原生结构（见 result_schema 契约表），
        # 所以不能拿字段名直接比对：能确定性导出的字段导出，导不出的字段 FAIL。
        # 设计原生记录（历史/合成/将来按 §7.1 产出的数据）仍走下面原有的严格路径。
        #
        # 契约缺口（7 个不可恢复字段）**不等于**执行证据缺失：缺口只让「§7.1 完整性」
        # 这一维度 FAIL；其余字段能证明的身份、批次归属、计划成员关系、同源哈希仍要
        # 逐行核对。否则一批真实跑完的数据会被整体误报成「计划任务全部缺失」。
        if not result_schema.is_design_native(row):
            producer_rows += 1
            partial = True
            normalized = result_schema.normalize_record(
                row, batch=batch_decision.value, run_dir=results_path.parent)
            failures = normalized.failures()
            # batch 的状态由 decide_batch() 判定，行级只核对记录「自己声明的」值：
            # - missing：记录没声明、目录也判不出 —— 不是契约缺口，由 run_preflight
            #   报 UNKNOWN，normalize 的 batch 失败在这里减掉；
            # - manifest-invalid / untrusted-records：可信来源缺失或记录自述非法 ——
            #   由函数末尾报结构化 FAIL，这里的通用失败减掉，避免同一件事报两遍；
            # - manifest-conflict：目录名与清单矛盾 —— 文件级一条 FAIL，归属不确定；
            # - directory / manifest：值已确定，记录若声明了别的批次逐行报冲突。
            # 行级已单独核对「记录自己声明的 batch」（非空字符串 / 与目录一致 /
            # 非法值逐行 FAIL），normalize 的通用 batch 失败不再重复报一次。
            undecidable = {"batch"}
            failed_fields = set(failures) - undecidable
            for name in sorted(failed_fields):
                report.add("FAIL", area, f"{label}:{lineno} · {name}",
                           "可确定性导出", failures[name],
                           "生产器记录该字段无法按冻结设计导出", CONTRACT_BASIS)
            for name in normalized.missing():
                if name in failures:
                    continue
                gap_fields.setdefault(name, []).append(lineno)
            if normalized.is_complete():
                derived_rows += 1
            else:
                gap_rows += 1
                # 即使这一行有契约缺口，legacy 缺 plan_sha256 的已知限制仍要告知
                if plan_missing:
                    _warn_legacy_plan(normalized.value("plan_sha256"))
            row = normalized.view()
            # 设计值本身就是 null 的字段（成功记录的 error、legacy 的 plan_sha256）
            # 不在视图里是正常的：交给下面的 error / plan_sha256 判定，不当作缺字段。
            null_ok = set(normalized.null_valued())

        # --- 字段齐全性：设计原生行要求 29 个字段全在 ---
        # 生产器行的缺口已按字段聚合上报（见函数末尾），此处不逐行重复刷屏，
        # 也不把「取不到」当成「值不对」再报一次。
        if not partial:
            missing = [name for name in RESULTS_REQUIRED_FIELDS
                       if name not in row and name not in null_ok]
            if missing:
                report.add("FAIL", area, f"{label}:{lineno}", "§7.1 全部字段",
                           f"缺 {missing}", "字段缺失，无法进入统计", basis)
                invalid_rows += 1
                continue
        for name in RESULTS_INT_FIELDS:
            if name in null_ok or name not in row:
                continue
            value = row.get(name)
            if not _is_int(value):
                report.add("FAIL", area, f"{label}:{lineno} · {name}",
                           "int（不能是 bool）", repr(value), "字段类型错误", basis)
        for name in RESULTS_STR_FIELDS:
            if name in null_ok or name not in row:
                continue
            value = row.get(name)
            if not isinstance(value, str) or not value.strip():
                report.add("FAIL", area, f"{label}:{lineno} · {name}",
                           "非空字符串", repr(value), "字段类型/取值错误", basis)

        # --- batch：逐条核对记录「自己声明的」值（不是标准化视图里的目录值）---
        # 可信批次只能来自清单或目录名；记录声明用于交叉验证，不得覆盖清单。
        has_declared_batch = "batch" in raw_row
        declared_batch = raw_row.get("batch") if has_declared_batch else None
        if has_declared_batch and not result_schema.is_valid_batch_value(declared_batch):
            report.add("FAIL", area, f"{label}:{lineno} · batch",
                       "非空字符串（或完全省略该字段）", repr(declared_batch),
                       "batch 值非法：批次只能是批次名字符串，list/dict/int/bool/空串"
                       "都不接受", basis)
        elif has_declared_batch:
            declared_seen.append((lineno, declared_batch))
            if (batch_decision.value is not None
                    and declared_batch != batch_decision.value
                    and batch_decision.state != BATCH_STATE_MANIFEST_CONFLICT):
                mismatches.append((lineno, declared_batch))

        # data_kind 在生产器记录里是不可恢复字段：缺口已按字段 FAIL（见函数末尾），
        # 这里既不补默认值，也不因为「跳过」而产生任何 §7.1 合规 PASS。
        if not partial and row.get("data_kind") != DESIGN_DATA_KIND:
            report.add("FAIL", area, f"{label}:{lineno} · data_kind",
                       DESIGN_DATA_KIND, repr(row.get("data_kind")),
                       "数据来源标记与设计不符", basis)
        if row.get("error") is not None:
            report.add("FAIL", area, f"{label}:{lineno} · error", "null",
                       repr(row.get("error"))[:120],
                       "存在失败记录，不能当作完成数据统计", basis)

        # --- 身份：只有字段类型合法才进入集合比较 ---
        experiment_id = row.get("experiment_id")
        sample_id = row.get("sample_id")
        group = row.get("group")
        repeat_index = row.get("repeat_index")
        identity_ok = (isinstance(experiment_id, str) and experiment_id.strip()
                       and isinstance(sample_id, str) and sample_id.strip()
                       and isinstance(group, str) and group.strip()
                       and _is_int(repeat_index))
        if identity_ok:
            identified_rows += 1
            identity = (experiment_id, sample_id, group, repeat_index)
            if identity in seen:
                report.add("FAIL", area, f"{label}:{lineno} · 身份",
                           "四元组唯一",
                           f"与第 {seen[identity]} 行重复：{identity}",
                           "同一实验身份出现多条记录", basis)
            else:
                seen[identity] = lineno
                identities.append(identity)
        else:
            invalid_rows += 1

        plan_sha_value = row.get("plan_sha256")
        if plan_usable:
            if plan_sha_value is None and "plan_sha256" not in failed_fields:
                # 只有「记录根本没给」才走这条：设计 §7.1 要求每条结果行带 plan_sha256，
                # 有 plan.json 时可据此核对同源；null 不是可以忽略的缺省值。
                report.add("FAIL", area, f"{label}:{lineno} · plan_sha256",
                           plan_sha, "null（记录未提供）",
                           "plan.json 存在时结果行必须带 plan_sha256，否则无法核对同源",
                           basis)
            elif plan_sha_value is not None and plan_sha_value != plan_sha:
                report.add("FAIL", area, f"{label}:{lineno} · plan_sha256",
                           plan_sha, repr(plan_sha_value),
                           "结果行与同目录 plan.json 不同源", basis)
            if identity_ok and plan_ids is not None and identity not in plan_ids:
                report.add("FAIL", area, f"{label}:{lineno} · 身份",
                           "属于 plan.json 的任务", repr(identity),
                           "结果行不在计划中", basis)
            # --- 计划同源：按身份定位计划任务后逐字段比对 ---
            # 只比「记录真的给了值」的字段：取不到的字段已按 §7.1 缺口 FAIL，不重复报。
            # 每个字段独立 FAIL，7 个不可恢复字段的缺口不能吞掉其中任何一条。
            task = plan_task_by_identity.get(identity) if identity_ok else None
            if task is not None:
                expected_seq = task.get("plan_seq")
                actual_seq = row.get("plan_seq")
                if _is_int(actual_seq) and actual_seq != expected_seq:
                    report.add("FAIL", area, f"{label}:{lineno} · plan_seq",
                               f"plan.json 中该身份的 plan_seq={expected_seq!r}"
                               f"（计划全局连续序号）", repr(actual_seq),
                               "结果行的执行序号与计划任务不一致，执行顺序无法审计", basis)
                for name, expected_value in (
                    ("seed", plan_seed),
                    ("scheme", plan_scheme),
                    ("algorithm_version", plan_algorithm_version),
                ):
                    actual_value = row.get(name)
                    if actual_value is None:
                        continue
                    if actual_value != expected_value:
                        report.add("FAIL", area, f"{label}:{lineno} · {name}",
                                   repr(expected_value), repr(actual_value),
                                   f"结果行的 {name} 与同目录 plan.json 不一致（不同源）",
                                   basis)
        elif plan_missing:
            _warn_legacy_plan(plan_sha_value)

    if batch_decision.state == BATCH_STATE_UNTRUSTED_RECORDS:
        report.add("FAIL", area, f"{label} · batch",
                   "批次来自清单（relative_path→batch）或目录名",
                   batch_decision.detail,
                   "记录自述不能决定正式批次：未识别目录里的记录统一写 main，"
                   "就能把其他批次冒充成主实验", BATCH_BASIS)
    elif batch_decision.state == BATCH_STATE_MANIFEST_CONFLICT:
        report.add("FAIL", area, f"{label} · batch",
                   "目录名与清单的批次一致",
                   batch_decision.detail,
                   "同一文件的批次有两个互相矛盾的可信来源，归属不可确定", BATCH_BASIS)
    elif mismatches and batch_decision.value is not None:
        if batch_decision.state == BATCH_STATE_MANIFEST:
            source = "由清单给出"
        elif batch_decision.state == BATCH_STATE_MANIFEST_MISSING:
            source = "由目录推断（清单缺 batch）"
        else:
            source = "由目录推断"
        for lineno, value in mismatches:
            report.add("FAIL", area, f"{label}:{lineno} · batch",
                       f"batch={batch_decision.value}（{source}）",
                       repr(value),
                       "记录批次与清单/目录给出的可信批次不一致", basis)

    # --- 契约缺口：按字段聚合，各一条 FAIL（不可恢复字段不得降级为 WARNING）---
    unrecoverable = result_schema.unrecoverable_reasons()
    for name in RESULTS_REQUIRED_FIELDS:
        lines = gap_fields.get(name)
        if not lines:
            continue
        report.add("FAIL", area, f"{label} · {name}",
                   "§7.1 必填字段（生产器记录须可核验）",
                   f"{len(lines)} 行缺失：第 {lines[:5]} 行",
                   unrecoverable.get(
                       name, "生产器未记录该字段，且无法从现有字段确定性恢复"),
                   CONTRACT_BASIS)

    if producer_rows:
        report.notes.append(
            f"{label}：执行证据 —— 非空结果行 {result_rows} 行，身份可识别 "
            f"{identified_rows} 行（唯一身份 {len(identities)} 个）")
        report.notes.append(
            f"{label}：§7.1 完整性 —— 完整记录 {derived_rows} 行，存在契约缺口 "
            f"{gap_rows} 行（缺口字段 {len(gap_fields)} 个）；缺口不影响上面这一行的"
            f"身份与计划归属核验，但该文件整体仍因缺口 FAIL")
        report.notes.append(
            f"{label}：{producer_rows} 行来自 run_experiment.py（生产器 schema），"
            f"经 result_schema 标准化为 §7.1 视图；{derived_rows} 行完整导出，"
            f"{gap_rows} 行存在无法恢复的字段")
        if plan_usable:
            report.notes.append(
                f"{label}：计划同源核对 —— 按身份定位计划任务后逐字段比对 plan_seq / "
                f"seed / scheme / algorithm_version / plan_sha256；只有记录真的给出了值"
                f"才比较，取不到的字段仍按 §7.1 缺口 FAIL")
        report.notes.append(
            f"{label}：标准化覆盖 {len(result_schema.DIRECT_FIELDS)} 个直供字段、"
            f"{len(result_schema.DERIVED_FIELDS)} 个可确定性派生字段；"
            f"不可恢复字段 {len(result_schema.UNRECOVERABLE_FIELDS)} 个，"
            f"逐字段见上（缺 {sorted(gap_fields)}）")
    if plan_state == PLAN_STATE_INVALID:
        reason = plan_detail or "原因见「计划」区域"
        report.notes.append(
            f"{label}：plan.json 校验失败（{reason}），结果侧未建立计划身份索引，"
            f"未做计划成员关系、同源字段与计划完成度核验")
    elif plan_untrusted:
        reason = plan_detail or "原因见「清单」区域"
        report.notes.append(
            f"{label}：plan.json 未通过可信清单完整性核验（{reason}），"
            f"结果侧未建立计划身份索引，未做计划成员关系、同源字段与计划完成度核验")
    if plan_structure_problems:
        report.notes.append(
            f"{label}：计划结构不完整（{len(plan_structure_problems)} 处），未建立身份索引；"
            f"原因见「计划」区域的 FAIL")
    if plan_identity_duplicates:
        report.notes.append(
            f"{label}：计划中有 {len(plan_identity_duplicates)} 个重复身份，未为这些身份建立"
            f"索引（不取其中任意一条任务比对）；唯一性已在「计划」区域逐条 FAIL")

    if invalid_rows:
        report.notes.append(f"{label}：{invalid_rows} 行字段/身份畸形，已跳过集合比较")
    if plan_ids is not None and trusted:
        missing_ids = sorted(plan_ids - set(identities))
        report.notes.append(
            f"{label}：计划任务 {len(plan_ids)} 个，身份命中（已执行）"
            f"{len(plan_ids) - len(missing_ids)} 个，未命中 {len(missing_ids)} 个")
        if missing_ids:
            report.add("FAIL", area, f"{label} · 计划完成度",
                       f"{len(plan_ids)} 个计划身份", f"缺 {len(missing_ids)}",
                       f"未完成：{missing_ids[:5]}", basis)
        else:
            report.add("PASS", area, f"{label} · 计划完成度",
                       f"{len(plan_ids)} 个计划身份", "全部有结果",
                       "计划任务全部完成", basis)
    if not trusted:
        report.notes.append(
            f"{label}：results.jsonl 未通过可信清单完整性核验（SHA-256/大小/记录数"
            f"不一致，或正式文件未纳入清单），已解析 {result_rows} 行仅用于诊断；"
            f"身份不计入 by_batch、覆盖与 165 份总量，不产生计划完成度结论")
        return []
    return identities


# ---------------------------------------------------------------------------
# 覆盖
# ---------------------------------------------------------------------------


def expected_main_identities():
    """主实验 28 格 × 5 轮 = 140 个「相对身份」（不含 experiment_id）。"""

    return {(sample, group, repeat)
            for sample in DESIGN_MAIN_SAMPLES
            for group in DESIGN_MAIN_GROUPS
            for repeat in range(DESIGN_ROUND_COUNT)}


def relative_identities(identities):
    return {(row[1], row[2], row[3]) for row in identities if _is_int(row[3])}


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
        _report_set(report, area, "批次 main", expected_main_identities(), relative,
                    basis, "主实验 7 样本 × A/B/C/D × 5 轮")
    elif batch == "suppl-subj":
        expected = {(SUPPLEMENT_SUBJ["sample_id"], group, repeat)
                    for group in SUPPLEMENT_SUBJ["groups"]
                    for repeat in range(DESIGN_ROUND_COUNT)}
        _report_set(report, area, "批次 suppl-subj", expected, relative, basis,
                    "S5 × C-subj/D-subj × 5 轮")
    elif batch == "suppl-s7empty":
        expected = {(SUPPLEMENT_S7EMPTY["sample_id"], group, repeat)
                    for group in SUPPLEMENT_S7EMPTY["groups"]
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
# 批次与指纹
# ---------------------------------------------------------------------------


def _batch_from_directory(name):
    """目录名 → 批次；无法判定返回 None（不读记录内容）。"""

    for prefix, batch in BATCH_PREFIXES:
        if name.startswith(prefix):
            return batch
    return None


@dataclass(frozen=True)
class BatchDecision:
    """一个结果文件的**可信批次**判定。

    value 只在 directory / manifest 两态有值。其余状态一律 None：
    manifest-conflict 虽然能按目录名给出一个值，但归属有矛盾，
    调用方必须同时报 FAIL，不能让 value 冒充「已确认」。

    state 的含义：
    - directory：目录名前缀给出批次（与清单一致；B0 legacy 允许不在清单）；
    - manifest：目录名不可判定，清单给出批次；
    - manifest-conflict：目录名与清单批次不一致；
    - manifest-missing：目录名可判定，但清单没有给出该文件的 batch；
    - manifest-invalid：清单里有这个文件，但 batch 列缺失或为空；
    - untrusted-records：只有记录自述，不能作为正式批次来源；
    - missing：目录、清单、记录都没有批次信息。
    """

    state: str
    value: object = None
    detail: str = ""


BATCH_STATE_DIRECTORY = "directory"
BATCH_STATE_MANIFEST = "manifest"
BATCH_STATE_MANIFEST_CONFLICT = "manifest-conflict"
BATCH_STATE_MANIFEST_MISSING = "manifest-missing"
BATCH_STATE_MANIFEST_INVALID = "manifest-invalid"
BATCH_STATE_UNTRUSTED_RECORDS = "untrusted-records"
BATCH_STATE_MISSING = "missing"

BATCH_BASIS = "设计 §2.4（批次与覆盖）/ 附录 A（清单是 batch 的可信来源）"


def decide_batch(directory, results_path, *, manifest_batch=None,
                 manifest_problem=None, legacy=False):
    """判定结果文件的可信批次：清单 > 目录名 > B0 legacy 目录名。

    清单是文件批次归属的唯一可信来源：未识别目录里的记录统一写 batch="main"，
    就能把别的批次冒充成正式主实验，所以记录自述永远只做交叉验证，不能覆盖清单。
    目录名能识别批次时要求与清单一致；清单没给出 batch 的正式文件直接 FAIL，
    不能因为目录名恰好能推断就把缺失的清单证据补上。
    """

    directory_batch = _batch_from_directory(directory.name)
    if directory_batch is not None:
        if manifest_batch is not None and manifest_batch != directory_batch:
            # 两个可信来源矛盾：不采用任何一个（采用目录值会把该目录的记录
            # 错误计入覆盖统计），归属保持不可确定，由调用方报文件级 FAIL。
            return BatchDecision(
                BATCH_STATE_MANIFEST_CONFLICT, None,
                "目录名推断 " + repr(directory_batch)
                + "，清单声明 " + repr(manifest_batch))
        if manifest_batch is None and manifest_problem is not None:
            return BatchDecision(BATCH_STATE_MANIFEST_INVALID, None,
                                 manifest_problem)
        if manifest_batch is None and not legacy:
            return BatchDecision(
                BATCH_STATE_MANIFEST_MISSING, directory_batch,
                "目录名可推断批次，但清单没有给出该文件的 batch")
        return BatchDecision(BATCH_STATE_DIRECTORY, directory_batch, "")

    if manifest_problem is not None:
        return BatchDecision(BATCH_STATE_MANIFEST_INVALID, None, manifest_problem)

    if manifest_batch is not None:
        return BatchDecision(BATCH_STATE_MANIFEST, manifest_batch,
                             "清单给出 batch=" + repr(manifest_batch))

    invalids, declared = _scan_batch_declarations(results_path)
    if invalids or declared:
        detail = "、".join(
            "第 " + str(lineno) + " 行 batch=" + repr(value)
            for lineno, value in invalids + declared)
        return BatchDecision(
            BATCH_STATE_UNTRUSTED_RECORDS, None,
            "目录名与清单都没有给出批次；记录自述（" + detail + "）只能交叉验证")
    return BatchDecision(BATCH_STATE_MISSING, None,
                         "目录名、清单与记录都没有给出批次")


def _scan_batch_declarations(results_path):
    """逐行扫描记录里的 batch 声明；返回 (invalids, declared)，都带行号。

    非法值不能像「只收集字符串」那样被悄悄丢掉：那会把「记录声明了 list」变成
    「记录没声明」，从而错误地退化成 UNKNOWN。解析失败的行由 check_results 报错，
    这里跳过即可。
    """

    invalids = []
    declared = []
    try:
        text = results_path.read_text(encoding="utf-8")
    except OSError:
        return invalids, declared
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or "batch" not in row:
            continue
        value = row.get("batch")
        if result_schema.is_valid_batch_value(value):
            declared.append((lineno, value))
        else:
            invalids.append((lineno, value))
    return invalids, declared


def expected_batch_for(directory, results_path):
    """预期批次名（兼容旧调用）；可信来源优先级见 decide_batch()。"""

    return decide_batch(directory, results_path).value


def is_b0_legacy(directory):
    """B0 legacy 目录（设计 §1.3 / §2.4：不进五轮计划，也不在冻结清单里）。"""

    return directory.name.startswith("b0-") or _batch_from_directory(directory.name) == B0_BATCH


def collect_fingerprint_paths(runs_dir, manifest_path):
    """运行前后要重算指纹的文件：manifest 自身 + 所有实际读取的 plan/results。

    清单只覆盖一部分文件（B0 legacy 就不在清单里），因此只对 `verified` 做指纹
    覆盖不到被读取的 legacy 文件；这里按「实际会读什么」收集，和清单无关。
    """

    paths = set()
    manifest = Path(manifest_path)
    if manifest.is_file():
        paths.add(manifest.resolve())
    base = Path(runs_dir)
    if base.is_dir():
        for directory in sorted(path for path in base.iterdir() if path.is_dir()):
            for name in (plan_schedule.PLAN_NAME, RESULTS_FILE_NAME):
                candidate = directory / name
                if candidate.is_file():
                    paths.add(candidate.resolve())
    return paths


def _is_sha256(value):
    """64 位十六进制字符串（大小写不敏感）才算一个 SHA-256 值。"""

    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_preflight(runs_dir, manifest_path, repo_root):
    report = Report()
    report.notes.append(f"runs-dir: {runs_dir}")
    report.notes.append(f"manifest: {manifest_path}")
    report.notes.append(f"repo-root: {repo_root}")
    report.notes.append("本工具只读：不写实验数据、不生成盲评包、不调用模型/Embedding API。")

    manifest_index = check_manifest(report, manifest_path, repo_root)
    declared, failed = manifest_index.declared, manifest_index.failed
    manifest_basis = "docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv（可信清单）"

    if not runs_dir.is_dir():
        report.add("FAIL", "目录", str(runs_dir), "runs 目录存在", "不存在",
                   "无法核对实验数据", "设计 §5.3 运行目录约定")
        return report

    run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    if not run_dirs:
        report.add("FAIL", "目录", str(runs_dir), "至少一个运行目录", "0 个",
                   "没有可核对的实验数据", "设计 §5.3")
        return report

    fingerprint_before = {path: _fingerprint(path)
                          for path in collect_fingerprint_paths(runs_dir, manifest_path)
                          if path.is_file()}

    by_batch = {}
    for directory in run_dirs:
        legacy = is_b0_legacy(directory)
        plan_path = directory / plan_schedule.PLAN_NAME
        results_path = directory / RESULTS_FILE_NAME
        label = directory.name

        # --- 文件级清单覆盖：plan.json 与 results.jsonl 分别确认 ---
        results_resolved = results_path.resolve()
        plan_resolved = plan_path.resolve()
        for path in (plan_path, results_path):
            if not path.is_file():
                continue
            target = f"{label}/{path.name}"
            resolved = path.resolve()
            if resolved in failed:
                # 清单条目核对已在「清单」区域报 FAIL；此处不再补 PASS，
                # 也不重复报「未纳入」，避免同一文件同时出现 PASS 与 FAIL。
                continue
            if resolved in declared:
                report.add("PASS", "清单覆盖", target, "已纳入清单", "已纳入",
                           "该文件的大小/哈希/记录数由清单核对", manifest_basis)
            elif legacy:
                report.add("WARNING", "清单覆盖", target,
                           "已纳入清单（B0 legacy 例外）", "未纳入",
                           "B0 legacy 按设计保留在清单之外，未做清单哈希核对，需负责人确认",
                           manifest_basis)
            else:
                report.add("FAIL", "清单覆盖", target, "已纳入清单", "未纳入",
                           "该文件不会被清单的大小/哈希/记录数核对；"
                           "目录里其他文件在清单里不能替代文件级核对", manifest_basis)

        if not results_path.is_file():
            if plan_path.is_file():
                report.add("FAIL", "目录", label, "results.jsonl 存在", "缺失",
                           "有计划但没有结果文件", "设计 §5.3")
            continue

        # --- 可信批次：清单 > 目录名 > B0 legacy 目录名 ---
        # 清单批次按规范化相对路径索引；同一 run-dir 的 plan.json 与 results.jsonl
        # 必须给出同一个清单批次，否则这个目录属于哪个批次无法唯一确定。
        #
        # 文件级信任同样以清单为准：正式文件只有在「已声明且未进 failed」时才可信。
        # B0 legacy 按冻结设计保留例外（它本来就不在清单里），不能因为缺少清单条目
        # 或清单核验失败被新闸门误伤。
        plan_trusted = legacy or (plan_resolved in declared and plan_resolved not in failed)
        results_trusted = legacy or (results_resolved in declared
                                     and results_resolved not in failed)
        manifest_batch = manifest_index.batches.get(results_resolved)
        manifest_problem = manifest_index.batch_problems.get(results_resolved)
        plan_manifest_batch = manifest_index.batches.get(plan_resolved)
        plan_manifest_problem = manifest_index.batch_problems.get(plan_resolved)

        if plan_path.is_file() and not legacy and plan_resolved in manifest_index.declared:
            if plan_manifest_problem is not None:
                report.add("FAIL", "清单", f"{label}/plan.json · batch",
                           "清单 batch 非空字符串", plan_manifest_problem,
                           "清单没有给出 plan.json 的可信批次", manifest_basis)
            elif (manifest_batch is not None
                  and plan_manifest_batch != manifest_batch):
                report.add("FAIL", "清单", f"{label}/plan.json · batch",
                           f"batch={manifest_batch}（与 results.jsonl 的清单批次一致）",
                           repr(plan_manifest_batch),
                           "同一 run-dir 的 plan.json 与 results.jsonl 清单批次不一致，"
                           "目录属于哪个批次无法唯一确定", manifest_basis)

        batch_decision = decide_batch(
            directory, results_path, manifest_batch=manifest_batch,
            manifest_problem=manifest_problem, legacy=legacy)
        expected_batch = batch_decision.value
        design_batch = expected_batch in TOTAL_BATCHES

        if batch_decision.state == BATCH_STATE_MANIFEST_CONFLICT:
            report.add("FAIL", "批次", label, "目录名与清单的批次一致",
                       batch_decision.detail,
                       "同一文件的批次有两个互相矛盾的可信来源，归属不可确定",
                       BATCH_BASIS)
        elif batch_decision.state == BATCH_STATE_MANIFEST_MISSING:
            report.add("FAIL", "批次", label, "清单给出该文件的 batch",
                       batch_decision.detail,
                       "正式 results.jsonl 必须有合法的清单 batch；目录名推断不能补上"
                       "缺失的清单证据", BATCH_BASIS)
        elif batch_decision.state == BATCH_STATE_MANIFEST_INVALID:
            report.add("FAIL", "批次", label, "清单给出该文件的 batch",
                       batch_decision.detail,
                       "清单里有这个文件但没有合法 batch，正式批次无法确认；"
                       "不得回退到记录自述", BATCH_BASIS)
        if plan_path.is_file() and not plan_trusted:
            # 清单已经为这个文件报了 FAIL（哈希/大小/记录数不一致）或清单覆盖 FAIL
            # （正式文件未纳入清单）；这里不再重复报一次，只把状态切断：计划内容
            # 不进入身份索引、成员关系、同源字段与完成度核验。不能退化成 missing，
            # 否则会误报 legacy WARNING。
            reason = ("SHA-256/大小/记录数与清单不一致" if plan_resolved in failed
                      else "正式文件未纳入可信清单")
            plan_check = PlanCheck(PLAN_STATE_UNTRUSTED, None, reason)
            report.notes.append(
                f"{label}/plan.json：未通过可信清单完整性核验（{reason}），"
                f"未进行计划内容核验")
        elif plan_path.is_file():
            plan_check = check_plan(report, plan_path, None, label,
                                    strict=design_batch)
        else:
            report.add("WARNING", "计划", label, "plan.json 存在", "缺失",
                       "无计划的目录按 legacy 处理；若需要续跑会被拒绝",
                       "设计 §5.3")
            plan_check = PlanCheck(PLAN_STATE_MISSING, None, "plan.json 不存在")

        # 计划有效性与文件信任贯穿到结果侧：invalid/untrusted 计划不得建立身份索引，
        # 也不得因 plan=None 被当作 legacy；missing 才走 legacy 路径。results 的
        # 清单信任独立判定：行内容正确也不能替代清单哈希核验。
        identities = check_results(report, results_path, plan_check.plan,
                                   expected_batch=expected_batch,
                                   batch_decision=batch_decision,
                                   plan_check=plan_check,
                                   trusted=results_trusted)
        experiment_ids = {row[0] for row in identities}
        if expected_batch is not None:
            by_batch.setdefault(expected_batch, []).extend(identities)
            check_coverage(report, expected_batch, identities, experiment_ids)
        elif batch_decision.state == BATCH_STATE_MISSING:
            # 只有「目录名与记录都没有批次信息」才是证据不足；冲突与非法值已经在
            # check_results 里报了结构化 FAIL，不能再降级成 UNKNOWN 把矛盾藏起来。
            report.add("UNKNOWN", "覆盖", label, "可判定的批次", "无法判定",
                       "目录名与记录都没有给出批次，需负责人确认", "设计 §2.4")
        else:
            report.notes.append(
                f"{label}：batch 判定为 {batch_decision.state}（{batch_decision.detail}），"
                f"批次归属不可确定，未做覆盖核对；原因见「结果记录」区域的 FAIL")

    total = sum(len(by_batch.get(batch, [])) for batch in TOTAL_BATCHES)
    if by_batch:
        excluded = {batch: len(items) for batch, items in by_batch.items()
                    if batch not in TOTAL_BATCHES}
        if excluded:
            report.notes.append("未计入 §2.4 的 165 份的批次：" + "、".join(
                f"{batch}={count}" for batch, count in sorted(excluded.items())))
        expected_total = DESIGN_MAIN_RECORDS + DESIGN_SUPPLEMENT_RECORDS
        if total == expected_total:
            report.add("PASS", "汇总", "生成任务总数", f"{expected_total}",
                       f"{total}", "与设计 §2.4 一致", "设计 §2.4")
        else:
            report.add("FAIL", "汇总", "生成任务总数", f"{expected_total}",
                       f"{total}", "生成任务数与冻结设计不一致", "设计 §2.4")
        for batch, items in sorted(by_batch.items()):
            report.notes.append(f"批次 {batch}：{len(items)} 条记录")

    changed = [str(path) for path, digest in fingerprint_before.items()
               if not path.is_file() or _fingerprint(path) != digest]
    if changed:
        report.add("FAIL", "只读自检", "运行期间输入文件未被改动", "无改动",
                   f"{len(changed)} 个文件变化：{changed[:5]}",
                   "预检过程中输入被改动，结果不可信", "只读约定")
    else:
        report.add("PASS", "只读自检",
                   f"运行期间输入文件未被改动（{len(fingerprint_before)} 个：manifest + "
                   f"plan.json/results.jsonl，含不在清单里的 legacy 文件）",
                   "无改动", "无改动", "输入哈希前后一致", "只读约定")
    return report


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

    try:
        report = run_preflight(args.runs_dir, args.manifest, args.repo_root)
    except Exception as error:                 # noqa: BLE001 预检不得以 traceback 结束
        print("实验数据只读预检报告\n")
        print(f"[FAIL] 预检异常｜{type(error).__name__}: {error}")
        print("结论：预检未能完成，按存在 FAIL 处理，数据不得进入正式统计。")
        return 1

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
