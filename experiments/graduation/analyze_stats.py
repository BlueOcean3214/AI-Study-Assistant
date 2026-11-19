#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""毕业设计实验脚本 · 统计分析（盲评一致性 / 派生最终分 / 格内聚合 / 假设检验）。

口径来源（只读，不得改写）：
`docs/GRADUATION-EXPERIMENT-DESIGN.md` v4.5 的 §5.4.6、§5.4.7、§5.5、§8 与附录 B。

本脚本只做 §5.5「0.1 评分与聚合的固定顺序」那一条流水线，且顺序不可跳步：

    原始评分（长表，两人各一行）
      → 一致性（裁决之前算，二次加权 Cohen's κ / 0/1 用 Cohen's κ）
      → 裁决（只对 1–5 分相差 >1、0/1 相反；写独立列，不回写原始分）
      → 派生最终分（未触发裁决取两人算术均值；触发取裁决值；未裁决保留 pending）
      → 格内聚合（(样本, 条件) 的 3 份输出 → 1 个格值）
      → n=7 样本级配对检验

硬约束（实现层强制）：

- **只读输入**：所有输入路径必须命令行显式给出，没有默认值，不猜、不自动发现真实数据。
  运行前后对各输入文件做 SHA-256 比对，不一致立即失败。
- **不覆盖上游、不覆盖输出**：只写 `--out-dir`；任何目标文件已存在即失败退出，不提供 --force。
- **fail-closed**：表头/类型/取值/配对完整性/84-6-23 边界/映射唯一性任一不满足，给出具体
  原因（文件、行号、字段、原值）后停止相关分析，不猜测、不补零、不跳过。
- **不适用与缺失分开**：`N/A` = 该格按 §5.4.7 不适用；空 = 未评（pending，不进分母）。
  两者都不当 0，都不进均值/比例。
- **算不出来就写原因码**：`m=0` / `sd=0` / `no-variation` / `pe=1` / `all-pending` /
  `log-domain-violation` / `missing-input:*`。不得把「算不出来」写成「没有差异」。

本脚本不实现、也不暗示以下内容：LLM 辅助评分、Cliff's δ、Pratt 变体、连续性校正、
Friedman 之后的事后两两扫描、把 35 或 140 当样本量的检验。

命令（全部输入必须显式给出，示例路径均为占位）：

    python experiments/graduation/analyze_stats.py \
        --formal-raw experiments/graduation/blind_ratings/ratings_formal_raw.csv \
        --calibration-raw experiments/graduation/blind_ratings/ratings_calibration_raw.csv \
        --supplement-raw experiments/graduation/blind_ratings/ratings_supplement_raw.csv \
        --adjudicated experiments/graduation/blind_ratings/ratings_adjudicated.csv \
        --mapping experiments/graduation/blind_private/mapping.csv \
        --results experiments/graduation/runs/main-20261113T061051Z/results.jsonl \
        --out-dir /tmp/stats-out

退出码：0 成功；2 输入校验失败或输出已存在；1 未预期异常。
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量：字段与口径（照抄 §5.4.6 / §5.4.7 / §7.2）
# ---------------------------------------------------------------------------

FORMAL_RAW_COLUMNS = (
    "blind_id", "rater_id", "phase",
    "P1", "P2", "P3", "P4", "S2", "S3_flag", "S4_verdict", "notes",
)
SUPPLEMENT_RAW_COLUMNS = ("blind_id", "rater_id", "phase", "E1", "E7", "notes")
ADJUDICATED_COLUMNS = (
    "blind_id", "item", "rater_a_value", "rater_b_value",
    "final_value", "adjudicator", "reason", "decided_utc",
)
MAPPING_COLUMNS = (
    "blind_id", "phase", "batch", "source_run_dir", "source_file",
    "source_row_number", "experiment_id", "sample_id", "group", "condition",
    "repeat_index", "round_index", "source_prompt_sha256",
    "source_result_row_sha256", "source_results_file_sha256",
    "generated_utc", "generator_version",
)

LIKERT_ITEMS = ("P1", "P2", "P3", "P4", "S2")
BINARY_ITEMS = ("S3_flag",)
VERDICT_ITEMS = ("S4_verdict",)
SUPPLEMENT_ITEMS = ("E1", "E7")
ALL_ITEMS = LIKERT_ITEMS + BINARY_ITEMS + VERDICT_ITEMS + SUPPLEMENT_ITEMS

LIKERT_MIN, LIKERT_MAX = 1, 5
VERDICT_LABELS = ("full", "partial", "wrong")
SAMPLES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
CONDITIONS = ("A", "B", "C", "D")
CELL_OUTPUTS = 3          # 每格实际纳入盲评的份数（§5.4.2 / §5.4.7）
REPEATS_PER_CELL = 5      # §5.5 提到的 5 次重复；盲评只抽 3 份，故 5 与 3 的差异见 README 歧义节

# A3：评分者身份只允许 rater_a / rater_b（两份评分材料固定发给这两位）
RATER_IDS = ("rater_a", "rater_b")

# A4：mapping 的 phase / batch / 样本 / 条件边界。
# 口径来源：§5.4.4/§5.4.5/§5.4.7 的批次表 + make_blind_pack.py 的 build_items()/mapping_rows()。
PHASES = ("formal", "calibration", "supplementary")
BATCH_MAIN = "main"
BATCH_SUBJ = "suppl-subj"
BATCH_S7EMPTY = "suppl-s7empty"
BATCH_B0 = "b0-legacy"
BATCHES = (BATCH_MAIN, BATCH_SUBJ, BATCH_S7EMPTY, BATCH_B0)
S7EMPTY_SAMPLE = "S7-empty"          # 生成器把 S7-empty 作为 sample_id 直接写入 mapping
SUBJ_CONDITIONS = ("C-subj", "D-subj")
B0_CONDITION = "B0"
B0_SAMPLES = ("S5", "S7")
# 每批次允许出现的 (sample_id, condition) 组合；None 表示「S1..S7 × A..D 全组合」
BATCH_SAMPLE_CONDITIONS = {
    BATCH_MAIN: None,
    BATCH_S7EMPTY: frozenset({(S7EMPTY_SAMPLE, "D")}),
    BATCH_SUBJ: frozenset({("S5", c) for c in SUBJ_CONDITIONS}),
    BATCH_B0: frozenset({(s, B0_CONDITION) for s in B0_SAMPLES}),
}
# 补充批次在 mapping 里的份数（§5.4.4：3 + 10 + 10 = 23）
BATCH_COUNTS = {BATCH_S7EMPTY: 3, BATCH_B0: 10, BATCH_SUBJ: 10}
# 补充批次里每个 (sample, group) 的份数（B0：S5 5 份 + S7 5 份；subj：两个条件各 5 份）
BATCH_CELL_COUNTS = {
    BATCH_S7EMPTY: {(S7EMPTY_SAMPLE, "D"): 3},
    BATCH_B0: {("S5", B0_CONDITION): 5, ("S7", B0_CONDITION): 5},
    BATCH_SUBJ: {("S5", "C-subj"): 5, ("S5", "D-subj"): 5},
}

FORMAL_COUNT = 84
CALIBRATION_COUNT = 6
SUPPLEMENT_COUNT = 23

# §5.4.7 逐批次矩阵里的份数边界
EXPECTED_APPLICABLE = {
    "P1": 84, "P2": 84, "P4": 84, "S2": 84,
    "P3": 72,            # S7 的 12 份不评
    "S3_flag": 24,       # 仅 S3、S6
    "S4_verdict": 12,    # 仅 S6
}

# blind_id 只允许 32 位小写十六进制（make_blind_pack 的默认生成形态）或严格 UUIDv4；
# 与 check_ratings.py 保持同一口径，避免同一份评分表一个工具放行、另一个工具拒绝。
BLIND_ID_RE = re.compile(
    r"^(?:[0-9a-f]{32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 三档效应量（§5.5 第 3 条）
RRB_THRESHOLDS = ((0.5, "大"), (0.3, "中"), (0.1, "小"))
DZ_THRESHOLDS = ((0.8, "大"), (0.5, "中"), (0.2, "小"))

# 主家族：预登记 3 个对比（§5.5 第 2、4 条 + 附录 B）
PRIMARY_CONTRASTS = (
    {
        "id": "H1_D_minus_B_P3",
        "hypothesis": "H1",
        "metric": "P3",
        "positive": "D",
        "negative": "B",
        "direction": "D>B",
        "label": "D−B on P3",
    },
    {
        "id": "H2a_D_minus_B_P1",
        "hypothesis": "H2a",
        "metric": "P1",
        "positive": "D",
        "negative": "B",
        "direction": "D>B",
        "label": "D−B on P1",
    },
    {
        "id": "H2b_C_minus_A_P1",
        "hypothesis": "H2b",
        "metric": "P1",
        "positive": "C",
        "negative": "A",
        "direction": "C>A",
        "label": "C−A on P1",
    },
)

# H5 家族：Y3（取对数后）、Y5，D vs A，单独 Holm，不与主家族混算（§5.5 第 4 条）
H5_CONTRASTS = (
    {"id": "H5_Y3_log", "hypothesis": "H5", "metric": "Y3_log", "positive": "D",
     "negative": "A", "direction": "D>A", "label": "Y3(log) D−A"},
    {"id": "H5_Y5", "hypothesis": "H5", "metric": "Y5_prompt_tokens", "positive": "D",
     "negative": "A", "direction": "D>A", "label": "Y5 D−A"},
)

DESIGN_AMBIGUITY_REPEATS = (
    "设计歧义（不修口径，只报告）：§5.5 写 0/1 判定「5 次中 ≥3 次为 1」折成样本级 0/1，"
    "但盲评每格只抽 3 份（§5.4.2/§5.4.7）。本脚本不把 3 改成 5、也不改门槛："
    "格内比例按实际纳入的 3 份计算；样本级 0/1 折叠需要 5 份才成立，本轮数据不足，"
    "因此只输出描述性比例，不据「≥3/5」下结论。"
)


class StatsError(RuntimeError):
    """输入不合法、被改动或输出已存在：终止并返回非零。"""

    def __init__(self, code, detail, problems=None):
        super().__init__(f"[{code}] {detail}")
        self.code = code
        self.detail = detail
        self.problems = list(problems or [])


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj):
    """规范 JSON（与 make_blind_pack.row_sha256 同口径：键排序、无空格、UTF-8 原样）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_sha256(row):
    return sha256_bytes(canonical_json(row).encode("utf-8"))


def mean(values):
    return statistics.fmean(values) if values else None


def sample_sd(values):
    """样本标准差（ddof=1）。少于 2 个值时不可计算，返回 None。"""
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def median(values):
    return statistics.median(values) if values else None


def round_or_none(value, digits=6):
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(value, digits)


def fmt(value, digits=6):
    """CSV 单元格格式化：None → 空；bool/浮点按位取整；其余原样。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else str(value)
    return str(value)


# ---------------------------------------------------------------------------
# 读取与表头校验
# ---------------------------------------------------------------------------


def _read_text(path, label):
    path = Path(path)
    if not path.is_file():
        raise StatsError("missing-input", f"{label} 不存在：{path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StatsError(
            "encoding", f"{label} 不是 UTF-8：{path}（{exc}）"
        ) from exc


def read_table(path, columns, label, allow_empty=False):
    """读 CSV 长表：表头必须与 columns 完全一致（顺序无关，不允许缺失/多余列）。

    `allow_empty=True` 用于裁决表：零条裁决是合法状态（两人从未触发阈值）。
    """
    text = _read_text(path, label)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise StatsError("empty-file", f"{label} 是空文件：{path}") from None
    header = [cell.strip() for cell in header]
    missing = [c for c in columns if c not in header]
    extra = [c for c in header if c not in columns]
    duplicated = sorted({c for c in header if header.count(c) > 1})
    if missing or extra or duplicated or len(header) != len(columns):
        raise StatsError(
            "bad-header",
            f"{label} 表头不符（{path}）：缺 {missing}，多 {extra}，重复 {duplicated}；"
            f"要求 {list(columns)}，实际 {header}",
        )
    index = {name: header.index(name) for name in columns}
    rows = []
    for lineno, cells in enumerate(reader, start=2):
        if not cells or all(not c.strip() for c in cells):
            continue  # 跳过纯空行，但保留行号
        if len(cells) != len(header):
            raise StatsError(
                "bad-row",
                f"{label} 第 {lineno} 行列数 {len(cells)} ≠ 表头 {len(header)}：{cells}",
            )
        row = {name: cells[index[name]].strip() for name in columns}
        row["_line"] = lineno
        rows.append(row)
    if not rows and not allow_empty:
        raise StatsError("empty-table", f"{label} 只有表头没有数据：{path}")
    return rows


# ---------------------------------------------------------------------------
# 取值解析：score / na / pending
# ---------------------------------------------------------------------------

NA_TOKEN = "N/A"
MISSING_TOKEN = ""


def _blank_error(where):
    """A2：原始评分表里的空白必须 fail-closed。

    设计文档 §5.4.6 允许「N/A 或留空」，但留空在统计侧会被静默排除出格内分母，
    等于用一个看不见的理由缩小样本量。本脚本属于目录录入/统计层，按 PR #22
    同一套工具层口径收紧：不适用必须显式写 N/A，留空一律报错。
    """
    return StatsError(
        "blank-value",
        f"{where}：原始评分表不得留空；该样本不适用请写 N/A，"
        f"未评不会静默计入分母（工具层收紧，见 STATISTICS-README.md）",
    )


def parse_likert(raw, where, allow_blank=False):
    """1–5 分：返回 (kind, value)；kind ∈ score/na/pending。"""
    text = (raw or "").strip()
    if text == "":
        if not allow_blank:
            raise _blank_error(where)
        return ("pending", None)
    if text.upper() == NA_TOKEN:
        return ("na", None)
    try:
        value = int(text)
    except ValueError:
        raise StatsError(
            "bad-value", f"{where}：1–5 分只接受 1..5、N/A 或空，实际 {text!r}"
        ) from None
    if raw != str(value):
        raise StatsError("bad-value", f"{where}：1–5 分必须是整数写法，实际 {raw!r}")
    if not (LIKERT_MIN <= value <= LIKERT_MAX):
        raise StatsError("out-of-range", f"{where}：1–5 分越界 {value}")
    return ("score", value)


def parse_binary(raw, where, allow_blank=False):
    text = (raw or "").strip()
    if text == "":
        if not allow_blank:
            raise _blank_error(where)
        return ("pending", None)
    if text.upper() == NA_TOKEN:
        return ("na", None)
    if text in ("0", "1"):
        return ("score", int(text))
    raise StatsError(
        "bad-value", f"{where}：0/1 判定只接受 0、1、N/A 或空，实际 {text!r}"
    )


def parse_verdict(raw, where, allow_blank=False):
    text = (raw or "").strip()
    if text == "":
        if not allow_blank:
            raise _blank_error(where)
        return ("pending", None)
    if text.upper() == NA_TOKEN:
        return ("na", None)
    if text in VERDICT_LABELS:
        return ("score", text)
    raise StatsError(
        "bad-value", f"{where}：S4_verdict 只接受 full/partial/wrong、N/A 或空，实际 {text!r}"
    )


def parse_item(item, raw, where, allow_blank=False):
    if item in LIKERT_ITEMS:
        return parse_likert(raw, where, allow_blank=allow_blank)
    if item in SUPPLEMENT_ITEMS or item in BINARY_ITEMS:
        return parse_binary(raw, where, allow_blank=allow_blank)
    if item in VERDICT_ITEMS:
        return parse_verdict(raw, where, allow_blank=allow_blank)
    raise StatsError("unknown-item", f"{where}：未知评分项 {item!r}")


# ---------------------------------------------------------------------------
# 映射表（blind_id → 源记录身份）
# ---------------------------------------------------------------------------


def _check_mapping_structure(rows, problems):
    """A4：按批次校验 phase/batch/sample/condition 对应关系与重复编号。

    只实现有冻结设计（§5.4.4/§5.4.5/§5.4.7）或生成器代码
    （make_blind_pack.py 的 build_items()/load_*_rows()）依据的规则。
    """
    # 1) phase 与 batch 的对应关系：正式与校准只能是 main 批次；
    #    补充批次只能是三个 legacy/补充批次之一。
    for row in rows:
        where = f"mapping.csv:{row['_line']}"
        phase = row["phase"]
        batch = row["batch"]
        if phase in ("formal", "calibration") and batch != BATCH_MAIN:
            problems.append(
                f"{where}：phase={phase} 的 batch 必须是 {BATCH_MAIN}，实际 {batch!r}"
            )
        if phase == "supplementary" and batch == BATCH_MAIN:
            problems.append(
                f"{where}：phase=supplementary 不应使用 {BATCH_MAIN} 批次"
            )

    # 2) 批次与 (sample_id, condition) 的对应关系。
    for row in rows:
        where = f"mapping.csv:{row['_line']}"
        allowed = BATCH_SAMPLE_CONDITIONS.get(row["batch"])
        if allowed is None:
            continue
        pair = (row["sample_id"], row["condition"])
        if pair not in allowed:
            problems.append(
                f"{where}：批次 {row['batch']} 不允许 (sample_id, condition)={pair}；"
                f"允许 {sorted(allowed)}"
            )

    # 3) 主批次（正式 + 校准）的 sample/condition 只能是 S1..S7 × A..D。
    for row in rows:
        if row["batch"] != BATCH_MAIN:
            continue
        where = f"mapping.csv:{row['_line']}"
        if row["sample_id"] not in SAMPLES:
            problems.append(f"{where}：main 批次 sample_id 非法 {row['sample_id']!r}")
        if row["condition"] not in CONDITIONS:
            problems.append(f"{where}：main 批次 condition 非法 {row['condition']!r}")

    # 4) repeat_index 合法范围 0..4（生成器 REPEATS=[0..4]）。
    for row in rows:
        if not (0 <= row["repeat_index"] < REPEATS_PER_CELL):
            problems.append(
                f"mapping.csv:{row['_line']}：repeat_index 越界 {row['repeat_index']}"
                f"（应在 0..{REPEATS_PER_CELL - 1}）"
            )

    # 5) round_index：B0 legacy 允许为空，其它批次必须是 0..4。
    for row in rows:
        where = f"mapping.csv:{row['_line']}"
        raw = row.get("_round_index_raw", "")
        if row["batch"] == BATCH_B0:
            if raw not in ("", None) and not (0 <= row["round_index"] < REPEATS_PER_CELL):
                problems.append(f"{where}：round_index 越界 {row['round_index']}")
            continue
        if raw in ("", None):
            problems.append(
                f"{where}：批次 {row['batch']} 的 round_index 不得为空"
                f"（只有 {BATCH_B0} 允许为空）"
            )
        elif not (0 <= row["round_index"] < REPEATS_PER_CELL):
            problems.append(f"{where}：round_index 越界 {row['round_index']}")

    # 6) 正式 84：28 格每格 3 份，且三份的 repeat_index 必须互不相同
    #    （不得用重复 repeat_index 凑满每格）。
    formal_rows = [r for r in rows if r["phase"] == "formal"]
    cell_repeats = {}
    for row in formal_rows:
        cell_repeats.setdefault((row["sample_id"], row["condition"]), []).append(
            row["repeat_index"]
        )
    for sample in SAMPLES:
        for condition in CONDITIONS:
            repeats = cell_repeats.get((sample, condition), [])
            if len(repeats) != CELL_OUTPUTS:
                problems.append(
                    f"mapping.csv：正式格 ({sample},{condition}) 应为 {CELL_OUTPUTS} 份，"
                    f"实际 {len(repeats)} 份"
                )
                continue
            if len(set(repeats)) != len(repeats):
                problems.append(
                    f"mapping.csv：正式格 ({sample},{condition}) 的 repeat_index 重复 "
                    f"{sorted(repeats)}；三份必须来自不同 repeat"
                )
    extra_formal = set(cell_repeats) - {(s, c) for s in SAMPLES for c in CONDITIONS}
    if extra_formal:
        problems.append(f"mapping.csv：正式出现 28 格之外的格子 {sorted(extra_formal)[:3]}")

    # 7) 校准 6：每格至多 1 份、每样本至多 1 份（§5.4.3），不得与正式完全同格同 repeat。
    calib_rows = [r for r in rows if r["phase"] == "calibration"]
    calib_cells = [(r["sample_id"], r["condition"]) for r in calib_rows]
    if len(set(calib_cells)) != len(calib_cells):
        problems.append("mapping.csv：校准 6 份必须覆盖 6 个不同格子（每格至多 1 份）")
    calib_samples = [r["sample_id"] for r in calib_rows]
    if len(set(calib_samples)) != len(calib_samples):
        problems.append("mapping.csv：校准 6 份必须覆盖 6 个不同样本（每样本至多 1 份）")

    # 7b) 校准不得与正式完全同格同 repeat：只比行的来源记录不足以发现这件事
    #     （两份行可以来自不同 run、不同 blind_id，但落到同一个
    #     （sample_id, condition, repeat_index）格点上）。这里直接比三元组。
    formal_slots = {}
    for row in formal_rows:
        key = (row["sample_id"], row["condition"], row["repeat_index"])
        formal_slots.setdefault(key, row)
    for row in calib_rows:
        key = (row["sample_id"], row["condition"], row["repeat_index"])
        twin = formal_slots.get(key)
        if twin is not None:
            problems.append(
                f"mapping.csv:{row['_line']}：校准行与正式行 mapping.csv:{twin['_line']} "
                f"同格同 repeat（{key[0]},{key[1]},repeat={key[2]}）；"
                f"校准不得复用正式的同一输出"
            )

    # 8) 补充 23：批次份数与每批次格子份数。
    suppl_rows = [r for r in rows if r["phase"] == "supplementary"]
    batch_counts = {}
    for row in suppl_rows:
        batch_counts[row["batch"]] = batch_counts.get(row["batch"], 0) + 1
    for batch, want in BATCH_COUNTS.items():
        got = batch_counts.get(batch, 0)
        if got != want:
            problems.append(
                f"mapping.csv：补充批次 {batch} 应为 {want} 份，实际 {got}"
            )
    cell_counts = {}
    for row in suppl_rows:
        key = (row["batch"], row["sample_id"], row["condition"])
        cell_counts[key] = cell_counts.get(key, 0) + 1
    for batch, spec in BATCH_CELL_COUNTS.items():
        for (sample, condition), want in spec.items():
            got = cell_counts.get((batch, sample, condition), 0)
            if got != want:
                problems.append(
                    f"mapping.csv：补充批次 {batch} 格 ({sample},{condition}) 应为 {want} 份，"
                    f"实际 {got}"
                )


def load_mapping(path):
    rows = read_table(path, MAPPING_COLUMNS, "mapping.csv")
    problems = []
    by_id = {}
    for row in rows:
        where = f"mapping.csv:{row['_line']}"
        blind_id = row["blind_id"]
        if not BLIND_ID_RE.match(blind_id):
            problems.append(f"{where}：blind_id 形态非法 {blind_id!r}")
        if blind_id in by_id:
            problems.append(f"{where}：blind_id 重复 {blind_id}")
        row["source_row_number"] = _int_field(row["source_row_number"], f"{where} source_row_number")
        row["repeat_index"] = _int_field(row["repeat_index"], f"{where} repeat_index")
        # round_index：B0 legacy 允许为空；空串必须保留下来不被 int() 吃掉。
        row["_round_index_raw"] = (row.get("round_index") or "").strip()
        if row["_round_index_raw"] == "":
            row["round_index"] = -1
        else:
            row["round_index"] = _int_field(row["round_index"], f"{where} round_index")
        if row["phase"] not in PHASES:
            problems.append(f"{where}：phase 非法 {row['phase']!r}")
        if row["batch"] not in BATCHES:
            problems.append(f"{where}：batch 非法 {row['batch']!r}")
        if not SHA256_RE.match(row["source_result_row_sha256"]):
            problems.append(f"{where}：source_result_row_sha256 不是 64 位小写十六进制")
        if not SHA256_RE.match(row["source_prompt_sha256"]):
            problems.append(f"{where}：source_prompt_sha256 不是 64 位小写十六进制")
        if not SHA256_RE.match(row["source_results_file_sha256"]):
            problems.append(f"{where}：source_results_file_sha256 不是 64 位小写十六进制")
        by_id[blind_id] = row
    if problems:
        raise StatsError("bad-mapping", f"mapping.csv 有 {len(problems)} 处问题", problems)

    counts = {
        "formal": sum(1 for r in rows if r["phase"] == "formal"),
        "calibration": sum(1 for r in rows if r["phase"] == "calibration"),
        "supplementary": sum(1 for r in rows if r["phase"] == "supplementary"),
    }
    expected = {"formal": FORMAL_COUNT, "calibration": CALIBRATION_COUNT,
                "supplementary": SUPPLEMENT_COUNT}
    for phase, want in expected.items():
        if counts[phase] != want:
            problems.append(f"mapping.csv：phase={phase} 应为 {want} 份，实际 {counts[phase]}")
    if len(rows) != sum(expected.values()):
        problems.append(f"mapping.csv：总行数应为 113，实际 {len(rows)}")

    # 84 与 6 的交集必须为空（§5.4.5 一致性自检）。
    formal_keys = {_source_key(r) for r in rows if r["phase"] == "formal"}
    calib_keys = {_source_key(r) for r in rows if r["phase"] == "calibration"}
    overlap = formal_keys & calib_keys
    if overlap:
        problems.append(f"mapping.csv：正式与校准存在交集 {sorted(overlap)[:3]}")

    seen = {}
    for row in rows:
        key = _source_key(row)
        if key in seen:
            problems.append(
                f"mapping.csv：源记录 (run_dir,row,row_sha256) 重复 {key} → "
                f"{seen[key]} 与 {row['blind_id']}"
            )
        seen[key] = row["blind_id"]

    _check_mapping_structure(rows, problems)

    if problems:
        raise StatsError("bad-mapping", f"mapping.csv 有 {len(problems)} 处问题", problems)
    return by_id


def _source_key(row):
    return (row["source_run_dir"], row["source_row_number"], row["source_result_row_sha256"])


def _int_field(raw, where):
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise StatsError("bad-value", f"{where}：必须是整数，实际 {raw!r}") from None


# ---------------------------------------------------------------------------
# 原始评分表校验
# ---------------------------------------------------------------------------


def load_raw_ratings(path, mapping, phase):
    """读正式/校准长表并按 §5.4.6/§5.4.7 校验。"""
    label = {"formal": "ratings_formal_raw.csv",
             "calibration": "ratings_calibration_raw.csv"}[phase]
    rows = read_table(path, FORMAL_RAW_COLUMNS, label)
    problems = []
    by_blind = {}
    for row in rows:
        where = f"{label}:{row['_line']}"
        blind_id = row["blind_id"]
        if blind_id not in mapping:
            problems.append(f"{where}：blind_id 不在 mapping.csv 中 {blind_id!r}")
            continue
        if mapping[blind_id]["phase"] != phase:
            problems.append(
                f"{where}：blind_id {blind_id} 在 mapping 里是 "
                f"{mapping[blind_id]['phase']}，不应出现在 {phase} 表"
            )
        if row["phase"] != phase:
            problems.append(f"{where}：phase 应为 {phase}，实际 {row['phase']!r}")
        if row["rater_id"] not in RATER_IDS:
            problems.append(
                f"{where}：rater_id 必须是 {RATER_IDS} 之一，实际 {row['rater_id']!r}"
                f"（不允许任意两个不同 ID 冒充两位评分者）"
            )
        by_blind.setdefault(blind_id, []).append(row)

    expected_ids = {bid for bid, m in mapping.items() if m["phase"] == phase}
    want = FORMAL_COUNT if phase == "formal" else CALIBRATION_COUNT
    if len(by_blind) != want:
        problems.append(f"{label}：应覆盖 {want} 个 blind_id，实际 {len(by_blind)}")
    missing = sorted(expected_ids - set(by_blind))
    if missing:
        problems.append(f"{label}：缺 blind_id {missing[:5]}（共 {len(missing)} 个）")
    extra = sorted(set(by_blind) - expected_ids)
    if extra:
        problems.append(f"{label}：多出 blind_id {extra[:5]}（共 {len(extra)} 个）")

    for blind_id, group in by_blind.items():
        where = f"{label} blind_id={blind_id}"
        if len(group) != 2:
            problems.append(f"{where}：应恰有 2 位评分者各一行，实际 {len(group)} 行")
        raters = [g["rater_id"] for g in group]
        if len(set(raters)) != len(raters):
            problems.append(f"{where}：rater_id 重复 {raters}")
        # A3：必须严格是 rater_a 与 rater_b 各一行，不能缺也不能换成别的 ID。
        if set(raters) != set(RATER_IDS):
            problems.append(
                f"{where}：评分者必须是 {list(RATER_IDS)} 各一行，实际 {sorted(set(raters))}"
            )
        item_kinds = {}
        sample = mapping[blind_id]["sample_id"]
        for row in group:
            for item in LIKERT_ITEMS + BINARY_ITEMS + VERDICT_ITEMS:
                applicable = _item_applicable(item, sample)
                kind, value = parse_item(
                    item, row[item], f"{where} rater={row['rater_id']} {item}"
                )
                if not applicable and kind != "na":
                    problems.append(
                        f"{where} rater={row['rater_id']} {item}：该样本不适用，"
                        f"必须写 N/A，实际 {row[item]!r}"
                    )
                if applicable and kind == "na":
                    problems.append(
                        f"{where} rater={row['rater_id']} {item}：该样本适用，"
                        f"不得写 N/A（请给 1–5 分，未评不会静默计入分母），"
                        f"实际 {row[item]!r}"
                    )
                item_kinds.setdefault(item, []).append(kind)
                row["_parsed_" + item] = (kind, value)
        for item, kinds in item_kinds.items():
            # 同一 blind_id 两人适用性必须一致（与 check_ratings.py --strict 同口径）。
            if len(set(kinds)) > 1 and "pending" not in kinds:
                problems.append(
                    f"{where} {item}：两人适用性不一致 {kinds}（N/A vs 有分）"
                )

    if problems:
        raise StatsError(
            f"bad-{phase}-raw", f"{label} 有 {len(problems)} 处问题", problems
        )

    # 适用性份数边界（§5.4.7 的 84/72/24/12）；份数按 blind_id 计，不按评分者行计。
    # 「适用」= 该份该项没被标成 N/A（有分或尚未评的 pending 都算适用；
    # 适用项误写 N/A 已在上面的逐行检查里报错）。
    if phase == "formal":
        for item, expected in EXPECTED_APPLICABLE.items():
            got = sum(
                1 for group in by_blind.values()
                if any(row["_parsed_" + item][0] in ("score", "pending")
                       for row in group)
            )
            if got != expected:
                problems.append(
                    f"{label}：{item} 适用份数应为 {expected}，实际 {got}"
                )
        if problems:
            raise StatsError("bad-applicability", f"{label} 有 {len(problems)} 处问题", problems)
    return by_blind


def load_supplement_ratings(path, mapping):
    label = "ratings_supplement_raw.csv"
    rows = read_table(path, SUPPLEMENT_RAW_COLUMNS, label)
    problems = []
    by_blind = {}
    for row in rows:
        where = f"{label}:{row['_line']}"
        blind_id = row["blind_id"]
        if blind_id not in mapping:
            problems.append(f"{where}：blind_id 不在 mapping.csv 中 {blind_id!r}")
            continue
        meta = mapping[blind_id]
        if meta["phase"] != "supplementary":
            problems.append(f"{where}：blind_id {blind_id} 不是补充批次")
        if row["phase"] != "supplementary":
            problems.append(f"{where}：phase 应为 supplementary，实际 {row['phase']!r}")
        if row["rater_id"] not in RATER_IDS:
            problems.append(
                f"{where}：rater_id 必须是 {RATER_IDS} 之一，实际 {row['rater_id']!r}"
                f"（不允许任意两个不同 ID 冒充两位评分者）"
            )
        for item in SUPPLEMENT_ITEMS:
            applicable = _supplement_item_applicable(item, meta)
            kind, value = parse_item(
                item, row[item], f"{where} rater={row['rater_id']} {item}"
            )
            if not applicable and kind != "na":
                problems.append(
                    f"{where} rater={row['rater_id']} {item}：该批次不适用（必须 N/A），"
                    f"实际 {row[item]!r}"
                )
            if applicable and kind == "na":
                problems.append(
                    f"{where} rater={row['rater_id']} {item}：该批次适用，"
                    f"不得写 N/A（请给 0/1，未评不会静默计入分母），实际 {row[item]!r}"
                )
            row["_parsed_" + item] = (kind, value)
        by_blind.setdefault(blind_id, []).append(row)

    expected_ids = {bid for bid, m in mapping.items() if m["phase"] == "supplementary"}
    if len(by_blind) != SUPPLEMENT_COUNT:
        problems.append(f"{label}：应覆盖 {SUPPLEMENT_COUNT} 个 blind_id，实际 {len(by_blind)}")
    missing = sorted(expected_ids - set(by_blind))
    if missing:
        problems.append(f"{label}：缺 blind_id {missing[:5]}（共 {len(missing)} 个）")
    extra = sorted(set(by_blind) - expected_ids)
    if extra:
        problems.append(f"{label}：多出 blind_id {extra[:5]}（共 {len(extra)} 个）")
    for blind_id, group in by_blind.items():
        if len(group) != 2:
            problems.append(
                f"{label} blind_id={blind_id}：应恰有 2 位评分者各一行，实际 {len(group)} 行"
            )
        raters = [g["rater_id"] for g in group]
        if len(set(raters)) != len(raters):
            problems.append(f"{label} blind_id={blind_id}：rater_id 重复 {raters}")
        for item in SUPPLEMENT_ITEMS:
            kinds = [g["_parsed_" + item][0] for g in group]
            if len(set(kinds)) > 1 and "pending" not in kinds:
                problems.append(
                    f"{label} blind_id={blind_id} {item}：两人适用性不一致 {kinds}"
                )
    if problems:
        raise StatsError("bad-supplement-raw", f"{label} 有 {len(problems)} 处问题", problems)

    # 补充批次适用份数（按 blind_id 计）：E1 23 份；E7 15 份（B0-S5 5 + C/D-subj 10）。
    # 「适用」= 该份该项没被标成 N/A；尚未评的 pending 仍属适用。
    e1 = sum(1 for g in by_blind.values()
             if any(r["_parsed_E1"][0] in ("score", "pending") for r in g))
    e7 = sum(1 for g in by_blind.values()
             if any(r["_parsed_E7"][0] in ("score", "pending") for r in g))
    if e1 != SUPPLEMENT_COUNT:
        problems.append(f"{label}：E1 适用份数应为 {SUPPLEMENT_COUNT}，实际 {e1}")
    if e7 != 15:
        problems.append(f"{label}：E7 适用份数应为 15，实际 {e7}")
    if problems:
        raise StatsError("bad-applicability", f"{label} 有 {len(problems)} 处问题", problems)
    return by_blind


def _item_applicable(item, sample):
    """§5.4.7 主表逐项：按样本决定该格该不该有分。"""
    if item in ("P1", "P2", "P4", "S2"):
        return True
    if item == "P3":
        return sample != "S7"
    if item == "S3_flag":
        return sample in ("S3", "S6")
    if item == "S4_verdict":
        return sample == "S6"
    raise StatsError("unknown-item", f"未知评分项 {item!r}")


def _supplement_item_applicable(item, meta):
    """§5.4.4/§5.4.7：E1 全适用；E7 只在有主题迁移的批次适用。"""
    if item == "E1":
        return True
    if item == "E7":
        if meta["batch"] == "suppl-subj":
            return True
        return meta["batch"] == "b0-legacy" and meta["sample_id"] == "S5"
    raise StatsError("unknown-item", f"未知补充评分项 {item!r}")


# ---------------------------------------------------------------------------
# 一致性（裁决之前；§5.5 第 0.1 条第 2 步）
# ---------------------------------------------------------------------------


def cohens_kappa(pairs, labels):
    """未加权 Cohen's κ（0/1 与三分 S4_verdict 用；三分按名义变量处理）。"""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "po": None, "pe": None, "kappa": None,
                "status": "not_applicable", "reason_code": "n=0"}
    categories = list(labels)
    po = sum(1 for a, b in pairs if a == b) / n
    marg_a = {c: sum(1 for a, _ in pairs if a == c) / n for c in categories}
    marg_b = {c: sum(1 for _, b in pairs if b == c) / n for c in categories}
    pe = sum(marg_a[c] * marg_b[c] for c in categories)
    if abs(1.0 - pe) < 1e-12:
        return {"n": n, "po": po, "pe": pe, "kappa": None,
                "status": "not_applicable", "reason_code": "pe=1",
                "note": "期望一致率 = 1，κ 无定义（两人都用同一个单一类别）"}
    kappa = (po - pe) / (1.0 - pe)
    degenerate = (len({a for a, _ in pairs}) == 1) or (len({b for _, b in pairs}) == 1)
    return {
        "n": n, "po": po, "pe": pe, "kappa": kappa,
        "status": "ok",
        "reason_code": "",
        "note": "退化：某一评分者只用一个类别，κ 对边际分布敏感" if degenerate else "",
    }


def quadratic_weighted_kappa(pairs, k=LIKERT_MAX):
    """二次加权 Cohen's κ（1–5 有序分；权重 w=(i−j)²/(K−1)²）。"""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "po": None, "pe": None, "kappa": None,
                "status": "not_applicable", "reason_code": "n=0"}
    denom = (k - 1) ** 2
    obs = {}
    for a, b in pairs:
        obs[(a, b)] = obs.get((a, b), 0) + 1
    marg_a = {c: sum(1 for a, _ in pairs if a == c) / n for c in range(1, k + 1)}
    marg_b = {c: sum(1 for _, b in pairs if b == c) / n for c in range(1, k + 1)}
    weighted_disagree = 0.0
    for (a, b), count in obs.items():
        weighted_disagree += (count / n) * ((a - b) ** 2) / denom
    expected_disagree = 0.0
    for a in range(1, k + 1):
        for b in range(1, k + 1):
            expected_disagree += marg_a[a] * marg_b[b] * ((a - b) ** 2) / denom
    po = 1.0 - weighted_disagree
    pe = 1.0 - expected_disagree
    if abs(1.0 - pe) < 1e-12:
        return {"n": n, "po": po, "pe": pe, "kappa": None,
                "status": "not_applicable", "reason_code": "pe=1"}
    return {
        "n": n, "po": po, "pe": pe, "kappa": (po - pe) / (1.0 - pe),
        "status": "ok", "reason_code": "", "note": "",
        "raw_agreement": sum(1 for a, b in pairs if a == b) / n,
    }


def agreement_rows(formal_by_blind, supplement_by_blind):
    """按项算一致性；正式/校准分开，补充批次单独成表（不计入主 κ）。"""
    rows = []

    def pairs_for(by_blind, item, allowed_kinds=("score",)):
        pairs, n_na, n_pending = [], 0, 0
        for blind_id in sorted(by_blind):
            group = by_blind[blind_id]
            parsed = [g["_parsed_" + item] for g in group]
            kinds = [p[0] for p in parsed]
            if all(k == "na" for k in kinds):
                n_na += 2
                continue
            if any(k == "na" for k in kinds):
                n_na += sum(1 for k in kinds if k == "na")
            scored = [p[1] for p in parsed if p[0] in allowed_kinds]
            n_pending += sum(1 for k in kinds if k == "pending")
            if len(scored) == 2:
                pairs.append((scored[0], scored[1]))
        return pairs, n_na, n_pending

    for item in LIKERT_ITEMS:
        pairs, n_na, n_pending = pairs_for(formal_by_blind, item)
        res = quadratic_weighted_kappa(pairs)
        rows.append({
            "table": "formal", "item": item, "scale": "1-5",
            "n_pairs": res["n"], "n_na_rows": n_na, "n_missing_rows": n_pending,
            "raw_agreement": res.get("raw_agreement"),
            "po_weighted": res["po"], "pe_weighted": res["pe"],
            "kappa": res["kappa"], "method": "quadratic_weighted_cohen_kappa",
            "status": res["status"], "reason_code": res["reason_code"],
            "note": res.get("note", ""),
        })
    for item in BINARY_ITEMS:
        pairs, n_na, n_pending = pairs_for(formal_by_blind, item)
        res = cohens_kappa(pairs, (0, 1))
        rows.append({
            "table": "formal", "item": item, "scale": "0/1",
            "n_pairs": res["n"], "n_na_rows": n_na, "n_missing_rows": n_pending,
            "raw_agreement": res["po"],
            "po_weighted": res["po"], "pe_weighted": res["pe"],
            "kappa": res["kappa"], "method": "cohen_kappa",
            "status": res["status"], "reason_code": res["reason_code"],
            "note": res.get("note", ""),
        })
    for item in VERDICT_ITEMS:
        pairs, n_na, n_pending = pairs_for(formal_by_blind, item)
        res = cohens_kappa(pairs, VERDICT_LABELS)
        rows.append({
            "table": "formal", "item": item, "scale": "3-category",
            "n_pairs": res["n"], "n_na_rows": n_na, "n_missing_rows": n_pending,
            "raw_agreement": res["po"],
            "po_weighted": res["po"], "pe_weighted": res["pe"],
            "kappa": res["kappa"], "method": "cohen_kappa_nominal_3cat",
            "status": res["status"], "reason_code": res["reason_code"],
            "note": "S4_verdict 是无序三分类，用未加权 κ；不得按 1–5 有序分加权",
        })
    for item in SUPPLEMENT_ITEMS:
        pairs, n_na, n_pending = pairs_for(supplement_by_blind, item)
        res = cohens_kappa(pairs, (0, 1))
        rows.append({
            "table": "supplementary", "item": item, "scale": "0/1",
            "n_pairs": res["n"], "n_na_rows": n_na, "n_missing_rows": n_pending,
            "raw_agreement": res["po"],
            "po_weighted": res["po"], "pe_weighted": res["pe"],
            "kappa": res["kappa"], "method": "cohen_kappa",
            "status": res["status"], "reason_code": res["reason_code"],
            "note": "描述性、样本量小；不计入主 κ",
        })
    return rows


# ---------------------------------------------------------------------------
# 裁决与最终分派生（§5.5 第 0.2 条）
# ---------------------------------------------------------------------------


def load_adjudications(path, raw_rows_by_blind):
    """读裁决表并校验：只对触发阈值的项；原值必须与原始表一致；不得重复。"""
    rows = read_table(path, ADJUDICATED_COLUMNS, "ratings_adjudicated.csv", allow_empty=True)
    problems = []
    table = {}
    for row in rows:
        where = f"ratings_adjudicated.csv:{row['_line']}"
        blind_id = row["blind_id"]
        item = row["item"]
        if blind_id not in raw_rows_by_blind:
            problems.append(f"{where}：blind_id 不在原始分表中 {blind_id!r}")
            continue
        if item not in ALL_ITEMS:
            problems.append(f"{where}：未知评分项 {item!r}")
            continue
        key = (blind_id, item)
        if key in table:
            problems.append(f"{where}：同一 (blind_id,item) 有两条裁决 {key}")
        group = raw_rows_by_blind[blind_id]
        parsed = [g.get("_parsed_" + item) for g in group]
        if any(p is None for p in parsed) or len(parsed) != 2:
            problems.append(f"{where}：该 blind_id 的 {item} 原始分缺失或不是 2 行")
            continue
        scored = [p[1] for p in parsed if p[0] == "score"]
        if len(scored) != 2:
            problems.append(f"{where}：该 (blind_id,item) 没有两人的有效分，不应有裁决")
            continue
        # A3：按评分者身份取值，不能只看排序后的两个值。
        by_rater = {}
        for g in group:
            parsed_one = g.get("_parsed_" + item)
            if parsed_one and parsed_one[0] == "score":
                by_rater[g["rater_id"]] = parsed_one[1]
        if set(by_rater) != set(RATER_IDS):
            problems.append(
                f"{where}：该 (blind_id,item) 的有效分不在 {list(RATER_IDS)} 上，"
                f"实际 {sorted(by_rater)}"
            )
            continue
        if item in VERDICT_ITEMS:
            triggered = by_rater["rater_a"] != by_rater["rater_b"]
            valid_final = VERDICT_LABELS
        elif item in LIKERT_ITEMS:
            triggered = abs(by_rater["rater_a"] - by_rater["rater_b"]) > 1
            valid_final = tuple(str(v) for v in range(1, 6))
        else:
            triggered = by_rater["rater_a"] != by_rater["rater_b"]
            valid_final = ("0", "1")
        if not triggered:
            problems.append(
                f"{where}：该项未触发裁决阈值"
                f"（rater_a={by_rater['rater_a']}, rater_b={by_rater['rater_b']}），"
                f"按 §5.5 不应写入裁决表"
            )
        # A3：rater_a_value 必须等于原始表 rater_a 的值，rater_b_value 同理；
        # 交换两者（哪怕排序后相同）也要报错，否则身份被抹平。
        if row["rater_a_value"] != str(by_rater["rater_a"]) or \
                row["rater_b_value"] != str(by_rater["rater_b"]):
            problems.append(
                f"{where}：rater_a_value/rater_b_value="
                f"{row['rater_a_value']!r}/{row['rater_b_value']!r} 与原始分"
                f"（rater_a={by_rater['rater_a']}, rater_b={by_rater['rater_b']}）"
                f"不按身份对应"
            )
        if row["final_value"] not in valid_final:
            problems.append(
                f"{where}：final_value={row['final_value']!r} 不在 {valid_final}"
            )
        if not row["adjudicator"].strip() or not row["reason"].strip():
            problems.append(f"{where}：adjudicator 与 reason 必须填写")
        table[key] = row
    if problems:
        raise StatsError("bad-adjudication",
                         f"ratings_adjudicated.csv 有 {len(problems)} 处问题", problems)
    return table


def derive_final(by_blind, adjudications):
    """按 §5.5 第 0.2 条派生最终分；返回 (final_by_blind, detail_rows)。"""
    final_by_blind = {}
    detail_rows = []
    for blind_id in sorted(by_blind):
        group = by_blind[blind_id]
        final = {}
        for item in LIKERT_ITEMS + BINARY_ITEMS + VERDICT_ITEMS:
            parsed = [g["_parsed_" + item] for g in group]
            kinds = [p[0] for p in parsed]
            values = [p[1] for p in parsed if p[0] == "score"]
            if all(k == "na" for k in kinds):
                final[item] = "N/A"
                status = "na"
            elif "pending" in kinds:
                # A2：原始表空白会被 parse_* 直接拒绝，pending 只可能在派生阶段
                # 由「两人合法评分分歧且无有效裁决」产生，绝不允许从这里冒出来，
                # 否则就是把「漏填」和「未裁决分歧」混成同一个状态。
                raise StatsError(
                    "internal-pending",
                    f"blind_id={blind_id} {item}：原始分不应出现 pending（{kinds}）；"
                    f"原始表空白必须 fail-closed，pending 只在派生阶段产生",
                )
            elif len(values) != 2:
                raise StatsError(
                    "bad-pairing",
                    f"blind_id={blind_id} {item}：两人适用性不一致 {kinds}",
                )
            elif item in VERDICT_ITEMS:
                if values[0] == values[1]:
                    final[item] = values[0]
                    status = "derived_agreement"
                elif (blind_id, item) in adjudications:
                    final[item] = adjudications[(blind_id, item)]["final_value"]
                    status = "derived_adjudicated"
                else:
                    final[item] = "pending"
                    status = "pending_unadjudicated"
            elif item in LIKERT_ITEMS:
                if abs(values[0] - values[1]) <= 1:
                    final[item] = (values[0] + values[1]) / 2.0
                    status = "derived_mean"
                elif (blind_id, item) in adjudications:
                    final[item] = float(adjudications[(blind_id, item)]["final_value"])
                    status = "derived_adjudicated"
                else:
                    final[item] = "pending"
                    status = "pending_unadjudicated"
            else:  # 0/1
                if values[0] == values[1]:
                    final[item] = values[0]
                    status = "derived_agreement"
                elif (blind_id, item) in adjudications:
                    final[item] = int(adjudications[(blind_id, item)]["final_value"])
                    status = "derived_adjudicated"
                else:
                    final[item] = "pending"
                    status = "pending_unadjudicated"
            detail_rows.append({
                "blind_id": blind_id, "item": item,
                "rater_values": "/".join(str(v) for v in values) if values else "",
                "kinds": "/".join(kinds),
                "final_value": final[item], "status": status,
                "adjudicated": "1" if (blind_id, item) in adjudications else "0",
            })
        final_by_blind[blind_id] = final
    return final_by_blind, detail_rows


def derive_supplement_final(by_blind, adjudications):
    final_by_blind = {}
    detail_rows = []
    for blind_id in sorted(by_blind):
        group = by_blind[blind_id]
        final = {}
        for item in SUPPLEMENT_ITEMS:
            parsed = [g["_parsed_" + item] for g in group]
            kinds = [p[0] for p in parsed]
            values = [p[1] for p in parsed if p[0] == "score"]
            if all(k == "na" for k in kinds):
                final[item] = "N/A"
                status = "na"
            elif "pending" in kinds:
                # A2：原始表空白会被 parse_* 直接拒绝，pending 只可能在派生阶段
                # 由「两人合法评分分歧且无有效裁决」产生，绝不允许从这里冒出来，
                # 否则就是把「漏填」和「未裁决分歧」混成同一个状态。
                raise StatsError(
                    "internal-pending",
                    f"blind_id={blind_id} {item}：原始分不应出现 pending（{kinds}）；"
                    f"原始表空白必须 fail-closed，pending 只在派生阶段产生",
                )
            elif len(values) != 2:
                raise StatsError(
                    "bad-pairing", f"blind_id={blind_id} {item} 适用性不一致 {kinds}"
                )
            elif values[0] == values[1]:
                final[item] = values[0]
                status = "derived_agreement"
            elif (blind_id, item) in adjudications:
                final[item] = int(adjudications[(blind_id, item)]["final_value"])
                status = "derived_adjudicated"
            else:
                final[item] = "pending"
                status = "pending_unadjudicated"
            detail_rows.append({
                "blind_id": blind_id, "item": item,
                "rater_values": "/".join(str(v) for v in values) if values else "",
                "kinds": "/".join(kinds),
                "final_value": final[item], "status": status,
                "adjudicated": "1" if (blind_id, item) in adjudications else "0",
            })
        final_by_blind[blind_id] = final
    return final_by_blind, detail_rows


# ---------------------------------------------------------------------------
# 格内聚合（§5.5 第 0.1 条第 5 步）
# ---------------------------------------------------------------------------


def aggregate_cells(final_by_blind, mapping, phase="formal"):
    """(样本, 条件) → 格值；连续分取均值，0/1 取比例；pending 不进分母。"""
    cells = {}
    for blind_id, final in final_by_blind.items():
        meta = mapping[blind_id]
        if meta["phase"] != phase:
            raise StatsError(
                "scope-violation",
                f"blind_id={blind_id} 的 phase 是 {meta['phase']}，不应进入 {phase} 的格内聚合",
            )
        key = (meta["sample_id"], meta["condition"])
        cells.setdefault(key, []).append((blind_id, final))

    rows = []
    values = {}
    for sample in SAMPLES:
        for condition in CONDITIONS:
            group = cells.get((sample, condition), [])
            if len(group) != CELL_OUTPUTS:
                raise StatsError(
                    "bad-cell",
                    f"格 ({sample},{condition}) 应有 {CELL_OUTPUTS} 份，实际 {len(group)} 份",
                )
            for item in LIKERT_ITEMS + BINARY_ITEMS + VERDICT_ITEMS:
                entries = [(bid, f[item]) for bid, f in group]
                n_pending = sum(1 for _, v in entries if v == "pending")
                n_na = sum(1 for _, v in entries if v == "N/A")
                effective = [v for _, v in entries
                             if v not in ("pending", "N/A")]
                status = "ok"
                note = ""
                value = None
                cell_sd = None
                if n_na == len(entries):
                    status = "na"
                    note = "该格按 §5.4.7 不适用"
                elif not effective:
                    status = "unavailable"
                    reason = "all-pending"
                    note = f"{n_pending} 份未裁决，分母为 0"
                elif item in LIKERT_ITEMS:
                    value = mean(effective)
                    cell_sd = sample_sd(effective)
                    if n_pending:
                        note = f"{len(entries)} 份中 {n_pending} 份 pending，按 {len(effective)} 份聚合"
                elif item in VERDICT_ITEMS:
                    counts = {label: sum(1 for v in effective if v == label)
                              for label in VERDICT_LABELS}
                    top = max(counts.values())
                    winners = [label for label, c in counts.items() if c == top]
                    value = winners[0] if len(winners) == 1 else "tie"
                    note = "三分类取多数；并列记 tie（不参与比例）" + (
                        f"；{n_pending} 份 pending" if n_pending else "")
                else:  # 0/1
                    value = sum(effective) / len(effective)
                    if n_pending:
                        note = f"{len(entries)} 份中 {n_pending} 份 pending，按 {len(effective)} 份聚合"
                values.setdefault(item, {})[(sample, condition)] = value
                rows.append({
                    "sample_id": sample, "condition": condition, "metric": item,
                    "n_total": len(entries), "n_effective": len(effective),
                    "n_pending": n_pending, "n_na": n_na,
                    "value": value, "cell_sd": cell_sd,
                    "status": status, "note": note, "phase": phase,
                })
    return rows, values


# ---------------------------------------------------------------------------
# 描述统计与 bootstrap（§5.5 第 1 条）
# ---------------------------------------------------------------------------


def _derive_seed(seed, *parts):
    material = "|".join([str(seed)] + [str(p) for p in parts]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def bootstrap_mean_ci(values, reps=10000, seed=0, tag=()):
    """按样本重采样的 95% 百分位 CI（固定种子 → 可复现）。"""
    if not values:
        return {"ci_low": None, "ci_high": None, "reps": 0, "reason_code": "n=0"}
    if len(values) < 2:
        return {"ci_low": None, "ci_high": None, "reps": 0, "reason_code": "n<2"}
    rng = random.Random(_derive_seed(seed, *tag))
    n = len(values)
    means = []
    for _ in range(reps):
        resample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    return {
        "ci_low": _percentile(means, 0.025),
        "ci_high": _percentile(means, 0.975),
        "reps": reps,
        "reason_code": "",
    }


def _percentile(sorted_values, q):
    """线性插值百分位（与 numpy 默认 linear 同口径）。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return sorted_values[low]
    weight = pos - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def descriptive_from_cells(cell_rows, seed, reps):
    """用 cell_aggregates 的明细重算描述统计（含格内 SD）。"""
    index = {}
    for row in cell_rows:
        index[(row["sample_id"], row["condition"], row["metric"])] = row
    rows = []
    for condition in CONDITIONS:
        for item in LIKERT_ITEMS:
            values, within = [], []
            n_pending_cells = 0
            for sample in SAMPLES:
                cell = index.get((sample, condition, item))
                if cell is None:
                    continue
                if isinstance(cell["value"], (int, float)):
                    values.append(cell["value"])
                if cell["status"] == "unavailable":
                    n_pending_cells += 1
                if cell["cell_sd"] is not None:
                    within.append(cell["cell_sd"])
            if not values:
                rows.append({
                    "condition": condition, "metric": item, "n_samples": 0,
                    "mean": None, "sd": None, "median": None,
                    "mean_within_cell_sd": None, "ci_low": None, "ci_high": None,
                    "bootstrap_reps": 0, "bootstrap_seed": seed,
                    "method": "bootstrap_percentile_sample_resampling",
                    "status": "unavailable", "reason_code": "no-sample-value", "note": "",
                })
                continue
            ci = bootstrap_mean_ci(values, reps=reps, seed=seed,
                                   tag=(condition, item, "mean"))
            rows.append({
                "condition": condition, "metric": item, "n_samples": len(values),
                "mean": mean(values), "sd": sample_sd(values), "median": median(values),
                "mean_within_cell_sd": mean(within) if within else None,
                "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
                "bootstrap_reps": ci["reps"], "bootstrap_seed": seed,
                "method": "bootstrap_percentile_sample_resampling",
                "status": "ok" if not n_pending_cells else "partial",
                "reason_code": "" if not n_pending_cells else "some-cells-pending",
                "note": ("n=7 个模拟样本；格内 SD 为生成噪声，非独立样本"
                         + (f"；{n_pending_cells} 个格因 pending 无值" if n_pending_cells else "")),
            })
    return rows


# ---------------------------------------------------------------------------
# 精确 Wilcoxon 符号秩（§5.5 第 2、3 条）
# ---------------------------------------------------------------------------


def average_ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def wilcoxon_exact(diffs):
    """精确符号秩检验：均值秩、零差剔除、符号完全枚举、双侧、无连续性校正。"""
    n_total = len(diffs)
    nonzero = [d for d in diffs if d != 0]
    m = len(nonzero)
    out = {
        "n_total": n_total, "m_nonzero": m,
        "mean_diff": mean(list(diffs)) if diffs else None,
        "median_diff": median(list(diffs)) if diffs else None,
        "w_plus": None, "w_minus": None, "p_raw": None, "p_min_achievable": None,
        "r_rb": None, "dz": None, "dz_reason": "", "status": "ok", "reason_code": "",
        "note": "",
    }
    # dz：用全部 7 个差值（含零差）
    if n_total >= 2:
        sd_all = sample_sd(list(diffs))
        mu = mean(list(diffs))
        if sd_all in (None, 0):
            out["dz"] = None
            out["dz_reason"] = "sd=0"
        elif sd_all is None:
            out["dz_reason"] = "n<2"
        else:
            out["dz"] = mu / sd_all
    else:
        out["dz_reason"] = "n<2"

    if m == 0:
        out["status"] = "not_applicable"
        out["reason_code"] = "m=0"
        out["note"] = "7 个差值全为 0：无非零配对，Wilcoxon 与 r_rb 均不适用"
        return out

    ranks = average_ranks([abs(d) for d in nonzero])
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero) if d < 0)
    total = w_plus + w_minus
    counts = {}
    for mask in range(1 << m):
        t = 0.0
        for i in range(m):
            if mask >> i & 1:
                t += ranks[i]
        key = round(t, 9)
        counts[key] = counts.get(key, 0) + 1
    lower = sum(c for t, c in counts.items() if t <= w_plus + 1e-9)
    upper = sum(c for t, c in counts.items() if t >= w_plus - 1e-9)
    p_raw = min(1.0, 2.0 * min(lower, upper) / (2 ** m))
    out.update({
        "w_plus": w_plus, "w_minus": w_minus, "p_raw": p_raw,
        "p_min_achievable": 2.0 / (2 ** m),
        "r_rb": (w_plus - w_minus) / total if total else None,
        "status": "ok", "reason_code": "",
        "note": f"m={m} 个非零差值；精确分布 2^{m} 种符号组合；平均秩",
    })
    return out


def effect_label(value, thresholds):
    if value is None:
        return ""
    for cut, label in thresholds:
        if abs(value) >= cut:
            return label
    return "可忽略"


def holm_adjust(pvalues, family, labels=None, alpha=0.05):
    """Holm 逐步校正：p_adj_i = max_{j<=i}(p_j * (k - j + 1))，单调不减。

    `labels` 与 pvalues 等长；返回行里带 contrast 标签，便于逐条对照。
    """
    k = len(pvalues)
    if labels is None:
        labels = [str(i) for i in range(k)]
    order = sorted(range(k), key=lambda i: (pvalues[i], i))
    rows = [None] * k
    running = 0.0
    for step, idx in enumerate(order, start=1):
        factor = k - step + 1
        value = min(1.0, pvalues[idx] * factor)
        running = max(running, value)
        rows[idx] = {
            "family": family, "contrast": labels[idx], "p_raw": pvalues[idx],
            "rank": step, "factor": factor, "p_holm": running,
            "alpha": alpha, "rejected": running <= alpha, "family_size": k,
        }
    return rows


# ---------------------------------------------------------------------------
# Friedman / 二分类检验 / 卡方分布（纯标准库）
# ---------------------------------------------------------------------------


def _gamma_series(a, x):
    """正则化下不完全 gamma P(a,x) 的级数展开。"""
    gln = math.lgamma(a)
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(10000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - gln)


def _gamma_continued_fraction(a, x):
    """正则化上不完全 gamma Q(a,x) 的连分式（Lentz）。"""
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b if b != 0 else 1.0 / tiny
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gamma_q(a, x):
    """正则化上不完全 gamma Q(a,x)。"""
    if x <= 0:
        return 1.0
    if x < a + 1.0:
        return 1.0 - _gamma_series(a, x)
    return _gamma_continued_fraction(a, x)


def chi2_sf(statistic, df):
    """卡方分布上尾概率（p 值）。"""
    if df <= 0:
        raise StatsError("bad-df", f"卡方自由度必须为正，实际 {df}")
    if statistic <= 0:
        return 1.0
    return gamma_q(df / 2.0, statistic / 2.0)


def friedman_test(block_values):
    """Friedman：n 个区组 × k 处理（本实验 k=4）；无变异时写不适用。"""
    n = len(block_values)
    if n == 0:
        return {"status": "not_applicable", "reason_code": "n=0", "q": None,
                "df": None, "p": None, "n_blocks": 0, "k": 0}
    k = len(block_values[0])
    if k < 3:
        raise StatsError("bad-friedman", f"处理数 k={k} < 3，无法做 Friedman")
    all_same = all(len(set(block)) == 1 for block in block_values)
    ranks = [average_ranks(block) for block in block_values]
    rank_sums = [sum(r[j] for r in ranks) for j in range(k)]
    q = 12.0 / (n * k * (k + 1)) * sum(r * r for r in rank_sums) - 3.0 * n * (k + 1)
    if all_same:
        return {"status": "not_applicable", "reason_code": "no-variation",
                "q": 0.0, "df": k - 1, "p": None, "n_blocks": n, "k": k,
                "note": "每个区组内四条件值全相等，Q=0，Friedman 不适用"}
    return {"status": "ok", "reason_code": "", "q": q, "df": k - 1,
            "p": chi2_sf(q, k - 1), "n_blocks": n, "k": k,
            "note": "卡方近似（df=3）；仅用于定位维度差异，不替代预登记对比"}


def cochrans_q(matrix):
    """Cochran's Q：n 个区组 × k 个 0/1 条件（弱证据、描述性）。"""
    n = len(matrix)
    if n == 0:
        return {"status": "not_applicable", "reason_code": "n=0", "q": None, "p": None}
    k = len(matrix[0])
    col_sums = [sum(row[j] for row in matrix) for j in range(k)]
    total = sum(col_sums)
    row_sums = [sum(row) for row in matrix]
    denom = k * total - sum(r * r for r in row_sums)
    if denom == 0:
        return {"status": "not_applicable", "reason_code": "no-variation",
                "q": None, "p": None, "df": k - 1}
    q = (k - 1) * (k * sum(c * c for c in col_sums) - total * total) / denom
    return {"status": "ok", "reason_code": "", "q": q, "df": k - 1,
            "p": chi2_sf(q, k - 1)}


def binomial_two_sided_p(successes, trials):
    if trials <= 0:
        return None
    probs = [math.comb(trials, i) * (0.5 ** trials) for i in range(trials + 1)]
    lower = sum(probs[:successes + 1])
    upper = sum(probs[successes:])
    return min(1.0, 2.0 * min(lower, upper))


def mcnemar_exact(pairs):
    """精确 McNemar：pairs = [(before, after)]；返回 (b, c, p)。"""
    b = sum(1 for x, y in pairs if x == 1 and y == 0)
    c = sum(1 for x, y in pairs if x == 0 and y == 1)
    return b, c, binomial_two_sided_p(min(b, c), b + c)


# ---------------------------------------------------------------------------
# 实验结果（Y1–Y5）读取：H5 用 Y2/Y3/Y5
# ---------------------------------------------------------------------------


def load_results(paths, mapping):
    """读 results.jsonl，按 mapping 的 source_result_row_sha256 建索引。"""
    index = {}
    file_hashes = {}
    for path in paths:
        path = Path(path)
        text = _read_text(path, "results.jsonl")
        file_hashes[str(path)] = sha256_bytes(text.encode("utf-8"))
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StatsError(
                    "bad-results", f"{path}:{lineno} JSON 解析失败：{exc}"
                ) from exc
            if not isinstance(row, dict):
                raise StatsError("bad-results", f"{path}:{lineno} 不是 JSON 对象")
            row_hash = row_sha256(row)
            index[row_hash] = {"row": row, "file": str(path), "line": lineno}
    # mapping 引用的每一行都必须能在 results 里找到（fail-closed）
    problems = []
    for blind_id, meta in mapping.items():
        key = meta["source_result_row_sha256"]
        if key not in index:
            problems.append(
                f"mapping.csv blind_id={blind_id}：source_result_row_sha256={key} "
                f"在给定的 results.jsonl 中找不到（文件 {meta['source_file']}）"
            )
            continue
        hit = index[key]
        row = hit["row"]
        for field in ("sample_id", "group"):
            want = meta["sample_id" if field == "sample_id" else "group"]
            if row.get(field) != want:
                problems.append(
                    f"mapping.csv blind_id={blind_id}：results 行 {field}="
                    f"{row.get(field)!r} 与 mapping 的 {want!r} 不一致"
                )
        if meta["source_prompt_sha256"] and row.get("prompt_sha256") != meta["source_prompt_sha256"]:
            problems.append(
                f"mapping.csv blind_id={blind_id}：prompt_sha256 与 mapping 记录不一致"
            )
        if hit["file"] != meta["source_file"] and meta["source_file"] not in hit["file"]:
            pass  # 路径可来自不同根，不按字符串强比；哈希一致即可
    if problems:
        raise StatsError("results-mapping-mismatch",
                         f"mapping 与 results 有 {len(problems)} 处不一致", problems)
    return index, file_hashes


def cost_metrics(index, mapping):
    """Y2/Y3/Y5：按 (样本, 条件) 聚合 3 份实际纳入的记录。"""
    per_cell = {}
    for blind_id, meta in mapping.items():
        if meta["phase"] != "formal":
            continue
        hit = index.get(meta["source_result_row_sha256"])
        if hit is None:
            continue
        row = hit["row"]
        cell = (meta["sample_id"], meta["condition"])
        per_cell.setdefault(cell, []).append(row)
    cell_rows = []
    for (sample, condition), rows in sorted(per_cell.items()):
        calls = [r.get("request_count") for r in rows
                 if isinstance(r.get("request_count"), int)]
        latency = [r.get("elapsed_seconds") for r in rows
                   if isinstance(r.get("elapsed_seconds"), (int, float))]
        tokens = []
        for r in rows:
            value = _prompt_tokens(r)
            if value:
                tokens.append(value)
        cell_rows.append({
            "sample_id": sample, "condition": condition, "n_records": len(rows),
            "y2_calls_mean": mean(calls), "y2_calls_max": max(calls) if calls else None,
            "y3_latency_mean": mean(latency), "y3_latency_median": median(latency),
            "y5_prompt_tokens_mean": mean(tokens),
        })
    return cell_rows


def _prompt_tokens(row):
    requests = row.get("requests")
    if isinstance(requests, str):
        try:
            requests = json.loads(requests)
        except json.JSONDecodeError:
            requests = None
    if isinstance(requests, list) and requests and isinstance(requests[0], dict):
        value = requests[0].get("prompt_tokens")
        if isinstance(value, int) and value > 0:
            return value
    usage = row.get("token_usage")
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            usage = None
    if isinstance(usage, dict):
        value = usage.get("prompt_tokens")
        if isinstance(value, int) and value > 0:
            return value
    return None


# ---------------------------------------------------------------------------
# 假设检验与结论档位
# ---------------------------------------------------------------------------


def paired_values(cell_values, contrast, cell_na=None):
    """取配对差（正条件 − 负条件）。

    两种「没有值」必须分开，否则会把设计允许的缺口当成数据错误：

    - 该格按 §5.4.7 **不适用**（三份都是 N/A，例如 P3 对 S7）→ 该样本合法退出这个
      对比，只减少 m，不报错。设计文档 §5.5 本身就写 H1 在 P3 上 m=6、最低双侧
      p=0.0313；至于它 Holm 后是多少，取决于该 p 在三个对比里的排名
      （标准 rank-dependent Holm，乘数不是固定 3），见第 6 节说明。
    - 该格**该有值但没有**（pending / 未裁决，分母为 0）→ 属于数据不完整，返回
      missing，由调用方 fail-closed，不补零、不生成 p。
    """
    cell_na = cell_na or set()
    metric = contrast["metric"]
    diffs = []
    missing = []
    n_na_excluded = 0
    for sample in SAMPLES:
        a = cell_values.get(metric, {}).get((sample, contrast["positive"]))
        b = cell_values.get(metric, {}).get((sample, contrast["negative"]))
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            diffs.append(a - b)
            continue
        if (metric, sample, contrast["positive"]) in cell_na and \
                (metric, sample, contrast["negative"]) in cell_na:
            n_na_excluded += 1
            continue
        missing.append(sample)
    return diffs, missing, n_na_excluded


def evaluate_contrast(contrast, cell_values, cell_na=None, alpha=0.05):
    diffs, missing, n_na_excluded = paired_values(cell_values, contrast, cell_na)
    if missing:
        # 缺格不补零：直接标记不可计算，不生成 p 值
        return {
            "hypothesis": contrast["hypothesis"], "contrast": contrast["id"],
            "metric": contrast["metric"], "label": contrast["label"],
            "direction": contrast["direction"], "n_blocks": len(SAMPLES),
            "missing_samples": ",".join(missing),
            "n_na_excluded": n_na_excluded,
            "m_nonzero": None, "w_plus": None, "w_minus": None,
            "mean_diff": None, "median_diff": None, "p_raw": None,
            "p_min_achievable": None, "p_holm": None, "r_rb": None,
            "r_rb_magnitude": "", "dz": None, "dz_magnitude": "",
            "dz_reason": "", "status": "unavailable",
            "reason_code": "missing-cell-value",
            "note": f"缺格值样本 {missing}；不补零、不生成 p",
        }
    res = wilcoxon_exact(diffs)
    na_note = (f"{n_na_excluded} 个样本该格按 §5.4.7 不适用（N/A），未计入配对"
               if n_na_excluded else "")
    note = "；".join(x for x in (res["note"], na_note) if x)
    return {
        "hypothesis": contrast["hypothesis"], "contrast": contrast["id"],
        "metric": contrast["metric"], "label": contrast["label"],
        "direction": contrast["direction"], "n_blocks": len(diffs),
        "missing_samples": "",
        "n_na_excluded": n_na_excluded,
        "m_nonzero": res["m_nonzero"], "w_plus": res["w_plus"],
        "w_minus": res["w_minus"],
        "mean_diff": res["mean_diff"], "median_diff": res["median_diff"],
        "p_raw": res["p_raw"], "p_min_achievable": res["p_min_achievable"],
        "r_rb": res["r_rb"], "r_rb_magnitude": effect_label(res["r_rb"], RRB_THRESHOLDS),
        "dz": res["dz"], "dz_magnitude": effect_label(res["dz"], DZ_THRESHOLDS),
        "dz_reason": res["dz_reason"],
        "status": res["status"], "reason_code": res["reason_code"],
        "note": note,
    }


def conclude(row, p_holm, alpha=0.05):
    """附录 B 的判定：p<0.05（Holm）+ |r_rb|≥0.5 + dz≥0.8 + m=7 + 均值差 ≥0.5。

    m 门槛按**实际参与配对的样本数**判：设计文档 §5.5 已明确 P3 对 S7 不适用，
    H1 的 m 上限就是 6，此时判据里「m=7」自动不成立，但结论仍照实写清楚
    是「未达 m=7」，而不是把不适用当缺失。
    """
    if row["status"] != "ok":
        return "不适用" if row["reason_code"] == "m=0" else "不可计算"
    n_paired = row.get("n_blocks") or 0
    if row["m_nonzero"] <= 5:
        return "不可判定（m≤5，原始 p 最低 ≥0.0625）"
    if p_holm is None:
        return "不可计算"
    if p_holm > alpha:
        return "未观察到差异（Holm 校正后 p>{:.2f}）".format(alpha)
    sign_ok = (row["r_rb"] or 0) > 0
    if not sign_ok:
        return "反向结果（统计可区分但方向与假设相反）"
    criteria = {
        "mean_diff>=0.5": (row["mean_diff"] or 0) >= 0.5,
        "r_rb>=0.5": abs(row["r_rb"] or 0) >= 0.5,
        "dz>=0.8": (row["dz"] or 0) >= 0.8,
        "m=7": row["m_nonzero"] == n_paired == 7,
    }
    if all(criteria.values()):
        return "支持（推断性，n=7）"
    failed = [k for k, v in criteria.items() if not v]
    return "达到统计门槛但未满足效应量条件：" + ", ".join(failed)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _write_text_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_csv_atomic(path, columns, rows):
    lines = [",".join(_csv_cell(c) for c in columns)]
    for row in rows:
        lines.append(",".join(_csv_cell(fmt(row.get(c))) for c in columns))
    _write_text_atomic(path, "\n".join(lines) + "\n")


def _csv_cell(value):
    text = "" if value is None else str(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def ensure_outputs_absent(paths):
    existing = [str(p) for p in paths if Path(p).exists()]
    if existing:
        raise StatsError(
            "output-exists",
            "以下输出已存在，本脚本不覆盖任何文件：" + "；".join(existing),
            existing,
        )


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

STATUS_LEGEND = (
    "状态口径：`支持`/`反向结果` 属**推断性**（n=7 配对，仅限 7 个模拟样本）；"
    "`未观察到差异` 不等于「没有差异」或「等价」；`不适用`/`不可计算` 一律附原因码，"
    "不得读作 0 或无差异；二分类与案例级内容一律标**描述性**。"
)


def render_report(ctx):
    lines = []
    add = lines.append
    add("# 统计分析报告（自动生成）")
    add("")
    add("> 本报告由 `experiments/graduation/analyze_stats.py` 生成，口径见 "
        "`docs/GRADUATION-EXPERIMENT-DESIGN.md` v4.5 §5.4.6 / §5.4.7 / §5.5 / §8 与附录 B。")
    add("")
    add("## 0. 运行与环境")
    add("")
    add(f"- 生成时间：{ctx['generated_utc']}")
    add(f"- 解释器：{ctx['python']}")
    add(f"- 随机种子（bootstrap）：{ctx['seed']}；重采样次数：{ctx['bootstrap_reps']}")
    add(f"- 输入文件（只读，SHA-256 见 `input_hashes.csv`）：{len(ctx['input_hashes'])} 个")
    add(f"- {STATUS_LEGEND}")
    add("")
    add("输入清单：")
    add("")
    for name, digest in sorted(ctx["input_hashes"].items()):
        add(f"- `{name}` — `{digest[:16]}…`")
    add("")

    add("## 1. 数据校验")
    add("")
    add(f"- 映射表：正式 {ctx['counts']['formal']} / 校准 {ctx['counts']['calibration']} / "
        f"补充 {ctx['counts']['supplementary']}（合计 {ctx['counts']['total']}），"
        f"正式与校准交集为空")
    add(f"- 原始评分：正式 {ctx['raw_rows']['formal']} 行、校准 "
        f"{ctx['raw_rows']['calibration']} 行、补充 {ctx['raw_rows']['supplementary']} 行")
    add(f"- 适用份数（正式）：P1/P2/P4/S2 = 84、P3 = 72、S3_flag = 24、S4_verdict = 12")
    add(f"- 补充批次适用份数：E1 = 23、E7 = 15")
    add(f"- 每格 {CELL_OUTPUTS} 份 × 7 样本 × 4 条件 = {FORMAL_COUNT} 份；"
        f"分析单位为样本（n=7），不是 35 或 140")
    add("")

    add("## 2. 一致性（裁决之前，推断性前置）")
    add("")
    add("| 表 | 项 | 量表 | 有效配对 | N/A 行 | 缺失行 | 原始一致率 | 加权 Po | 加权 Pe | κ | 方法 | 状态 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in ctx["agreement"]:
        status = row["status"] + (f"/{row['reason_code']}" if row["reason_code"] else "")
        cells = [
            row["table"], row["item"], row["scale"], str(row["n_pairs"]),
            str(row["n_na_rows"]), str(row["n_missing_rows"]),
            fmt(row["raw_agreement"], 4), fmt(row["po_weighted"], 4),
            fmt(row["pe_weighted"], 4), fmt(row["kappa"], 4),
            row["method"], status,
        ]
        add("| " + " | ".join(cells) + " |")
    add("")
    add("注：1–5 分用二次加权 κ（w=(i−j)²/16）；S3_flag/E1/E7 用未加权 κ；"
        "S4_verdict 是无序三分类，用未加权 κ 单独报告，不得按有序分加权。"
        "补充批次 κ 只作描述、不计入主 κ。")
    add("")
    add("## 3. 最终分派生")
    add("")
    add(f"- 两人相差 ≤1 分的连续项取算术均值；相差 >1 分取裁决值；未裁决保留 `pending`")
    add(f"- 0/1 一致直接采用，不一致取裁决值；S4_verdict 一致采用、不一致取裁决值")
    add(f"- 派生明细见 `ratings_final_detail.csv` / `ratings_supplement_final_detail.csv`")
    add(f"- pending 与 N/A 均不进格内均值/比例；各格实际参与份数见 `cell_aggregates.csv`")
    add("")

    add("## 4. 描述统计（描述性，n=7）")
    add("")
    add("| 条件 | 指标 | n | 均值 | SD | 中位数 | 格内 SD 均值 | 95% Bootstrap CI |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in ctx["descriptive"]:
        if row["status"] == "unavailable":
            add(f"| {row['condition']} | {row['metric']} | 0 | 不可计算（{row['reason_code']}） | | | | |")
            continue
        ci = (f"[{fmt(row['ci_low'], 4)}, {fmt(row['ci_high'], 4)}]"
              if row["ci_low"] is not None else "不可计算")
        add(f"| {row['condition']} | {row['metric']} | {row['n_samples']} | "
            f"{fmt(row['mean'], 4)} | {fmt(row['sd'], 4)} | {fmt(row['median'], 4)} | "
            f"{fmt(row['mean_within_cell_sd'], 4)} | {ci} |")
    add("")
    add("Bootstrap：按样本重采样（10000 次，固定种子），百分位法 95% CI；"
        "每格 3 份重复只是生成噪声，不是独立样本。")
    add("")

    add("## 5. 主假设检验（推断性）")
    add("")
    add("| 假设 | 对比 | m | W⁺ | W⁻ | 平均差 | 原始 p | 最小可达 p | Holm p | "
        "r_rb | dz | 结论 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in ctx["primary"]:
        cells = [
            row["hypothesis"], row["label"], str(row["m_nonzero"]),
            fmt(row["w_plus"], 4), fmt(row["w_minus"], 4),
            fmt(row["mean_diff"], 4), fmt(row["p_raw"], 4),
            fmt(row["p_min_achievable"], 4), fmt(row["p_holm"], 4),
            fmt(row["r_rb"], 4), fmt(row["dz"], 4), row["conclusion"],
        ]
        add("| " + " | ".join(cells) + " |")
    add("")
    add("口径：精确符号秩（2^m 符号完全枚举、平均秩、零差剔除、不做连续性校正）；"
        "`m` 是非零差值个数，与 `dz` 的口径不同——`dz` 用该对比实际存在的配对差"
        "（H1 因 S7 的 P3 不适用只有 6 个配对），表注必须分别写。")
    add("")
    add("## 6. Holm 校正")
    add("")
    add("| 家族 | 对比 | 原始 p | 排名 | 因子 | Holm p | 拒绝 H0 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for row in ctx["holm"]:
        add(f"| {row['family']} | {row['contrast']} | {fmt(row['p_raw'], 4)} | "
            f"{row['rank']} | {row['factor']} | {fmt(row['p_holm'], 4)} | "
            f"{'是' if row['rejected'] else '否'} |")
    add("")
    add("主家族 3 个对比统一校正；H5 家族（Y3/Y5）单独成族，不与主家族混算。")
    add("")
    add("**Holm 是 rank-dependent 的标准逐步法**：第 i 小的 p 乘以 (k−i+1)，"
        "乘数取决于该 p 在家族里的排名，而不是固定乘以 3。因此"
        "「m=6 时最小原始 p=0.0313 必然被 ×3 得 0.0939」只在它排最后一名时成立；"
        "若 H1 排第一或第二，乘数可能是 3 或 2，Holm p 可能是 0.09375 或 0.0625——"
        "这跟「H1 不可能显著」是两件事，必须分开写：")
    add("")
    add("- **H1 的 Holm p 能否低于 0.05**：取决于三个对比的 p 排序与 running maximum，"
        "不能一句话下结论。H1 排第一时 ×3 = 0.09375 >0.05；排第二时 ×2 = 0.0625 >0.05；"
        "排第三（×1）时其自身 p 是 0.03125，但校正值是 running max——"
        "若前两名的最小原始 p 为 0.015625（×3 = 0.046875），running max 会把 H1 抬到 "
        "0.046875，此时 H1 的 Holm p 确实低于 0.05。"
        "所以只能说「在特定排序下 H1 的 Holm p 可以低于 0.05」，"
        "不能说「H1 的 Holm p 必然够不着 0.05」；本次运行的实际排序与取值见上表。")
    add("- **H1 是否满足附录 B 的完整支持判据**：不满足，且是结构性的——"
        "附录 B 要求 m=7（7 个非零配对）且 Δ≥0.5、|r_rb|≥0.5、dz≥0.8、Holm p<0.05 同时成立，"
        "而 P3 对 S7 按 §5.4.7 不适用，H1 最多只有 6 个有效配对，m=7 永远无法达成。")
    add("- **H1 的 dz 分母**：实际只基于 6 个可用 P3 配对（不是 7 个）；"
        "`dz` 列用全部参与配对的差计算，样本数见 `n_blocks`。")
    add("")
    add("冻结设计文档 §5.5 对「m=6 时 Holm 如何解释」存在数学口径冲突"
        "（文档给出的 0.0939 只对应「该 p 排最后」的情形），"
        "本脚本不改设计文档、不改 Holm 算法，只按标准 rank-dependent 逐步法计算并如实标注；"
        "此处需研究负责人确认最终解释口径。")
    add("")

    add("## 7. Friedman（描述性定位）")
    add("")
    add("| 指标 | 区组 | 处理 | Q | df | p | 方法 | 状态 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in ctx["friedman"]:
        add(f"| {row['metric']} | {row['n_blocks']} | {row['k']} | {fmt(row['q'], 4)} | "
            f"{row['df']} | {fmt(row['p'], 4)} | {row['method']} | "
            f"{row['status']}{('/' + row['reason_code']) if row['reason_code'] else ''} |")
    add("")
    add("Friedman 只用于定位「哪些维度存在系统差异」，不替代三个预登记对比，"
        "也不在它之后做未预登记的事后两两扫描。")
    add("")

    add("## 8. 二分类比例（只描述，不做推断）")
    add("")
    add(f"- {DESIGN_AMBIGUITY_REPEATS}")
    add("")
    add("| 指标 | 范围 | 批次/条件 | 适用单位数 | 有值单位数 | 平均比例 | 说明 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for row in ctx["binary_rates"]:
        add(f"| {row['metric']} | {row['scope']} | {row['group']} | {row['n_units']} | "
            f"{row['n_scored']} | {fmt(row['mean_proportion'], 4)} | {row['note']} |")
    add("")
    if ctx["binary_tests"]:
        add("| 指标 | 检验 | 统计量 | p | 状态 | 说明 |")
        add("| --- | --- | --- | --- | --- | --- |")
        for row in ctx["binary_tests"]:
            add(f"| {row['metric']} | {row['test']} | {fmt(row['statistic'], 4)} | "
                f"{fmt(row['p'], 4)} | {row['status']} | {row['note']} |")
        add("")
    add("二分类结果一律标**弱证据/描述性**，本脚本不产出任何二分类推断 p 值。"
        "样本级 0/1 折叠按 §5.5 需要「同格 5 次重复中 ≥3 次为 1」，"
        "而盲评每格只抽 3 份；3 份如何折叠未被冻结文档定义，"
        "任何自定义折半门槛都属于新增判据，因此 Cochran's Q 与 McNemar 均未执行。")
    add("")

    add("## 9. H5（Y2/Y3/Y5，弱推断 + 描述）")
    add("")
    if not ctx["h5"]:
        add(f"- 未执行：{ctx['h5_reason'] or '未提供 results.jsonl'}")
    else:
        add("Y2（调用次数）分布：")
        add("")
        add("| 样本 | A | B | C | D |")
        add("| --- | --- | --- | --- | --- |")
        for row in ctx["y2_table"]:
            add(f"| {row['sample_id']} | {fmt(row['A'], 2)} | {fmt(row['B'], 2)} | "
                f"{fmt(row['C'], 2)} | {fmt(row['D'], 2)} |")
        add("")
        add("Y3/Y5 配对对比（D−A，H5 家族单独 Holm）：")
        add("")
        add("| 对比 | m | 平均差 | 原始 p | 最小可达 p | Holm p | r_rb | dz | 95% CI | 状态 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in ctx["h5"]:
            ci = (f"[{fmt(row['ci_low'], 4)}, {fmt(row['ci_high'], 4)}]"
                  if row["ci_low"] is not None else "不可计算")
            add(f"| {row['label']} | {row['m_nonzero']} | {fmt(row['mean_diff'], 4)} | "
                f"{fmt(row['p_raw'], 4)} | {fmt(row['p_min_achievable'], 4)} | "
                f"{fmt(row['p_holm'], 4)} | {fmt(row['r_rb'], 4)} | "
                f"{fmt(row['dz'], 4)} | {ci} | "
                f"{row['status']}{('/' + row['reason_code']) if row['reason_code'] else ''} |")
        add("")
        add("Y3 取对数后计算 dz 与 Wilcoxon；任一时间 ≤0 即 fail-closed（不加常数）。"
            "Y5 用提示 token 原始值。Y2 按设计报分布（1 次 + 修正 0–2 次），不做检验。")
    add("")

    add("## 10. 案例级与描述性结论")
    add("")
    add("H3（S5、S7）、H4（S3、S6）、H-risk 只报 flag 次数/比例与原文摘录，"
        "**不报 p 值、不写「显著」**：方向在重复内一致写「案例支持」，"
        "不稳定写「未观察到」。本脚本不生成原文摘录（需人工阅读 `results.jsonl`），"
        "只提供 `cell_aggregates.csv` 的逐样本比值作为定位索引。")
    add("")
    add("## 11. 未观察到差异 / 反向结果 / 不适用 / 不可计算 汇总")
    add("")
    buckets = {"支持": [], "反向结果": [], "未观察到差异": [], "不适用": [], "不可计算": [],
               "达到统计门槛但未满足效应量条件": [], "不可判定（m≤5": []}
    for row in ctx["primary"]:
        for key in buckets:
            if row["conclusion"].startswith(key):
                buckets[key].append(row["label"])
                break
    for key, items in buckets.items():
        if items:
            add(f"- {key}：{'；'.join(items)}")
        else:
            add(f"- {key}：无")
    add("")
    add("## 12. 复核指引")
    add("")
    add("1. 先读 `inter_rater_agreement.csv`：κ 在裁决前算好，任何裁决都不应抬高它；")
    add("2. 再读 `ratings_final_detail.csv`：每个最终分都能追到两人原值与裁决依据；")
    add("3. 再读 `cell_aggregates.csv`：确认每格分母（3 或更少）与 pending 数；")
    add("4. 最后读 `primary_hypothesis_tests.csv` 与 `holm_adjusted_results.csv`："
        "原始 p、最小可达 p、Holm p、m、r_rb、dz 必须同时出现，缺一个都不完整。")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_ctx(args, out_dir):
    mapping = load_mapping(args.mapping)
    formal_raw = load_raw_ratings(args.formal_raw, mapping, "formal")
    calibration_raw = load_raw_ratings(args.calibration_raw, mapping, "calibration")
    supplement_raw = load_supplement_ratings(args.supplement_raw, mapping)

    # 一致性只看正式 84；校准 6 不进主 κ（§5.4.7/§5.5）
    agreement = agreement_rows(formal_raw, supplement_raw)

    # 裁决：正式 + 校准 + 补充（裁决表按 blind_id 混存，逐项校验）
    all_raw = {}
    for group in (formal_raw, calibration_raw):
        all_raw.update(group)
    all_raw.update(supplement_raw)
    adjudications = load_adjudications(args.adjudicated, all_raw)

    # 只用正式 84 的裁决进主派生；校准/补充单独派生
    formal_adj = {k: v for k, v in adjudications.items() if k[0] in formal_raw}
    calib_adj = {k: v for k, v in adjudications.items() if k[0] in calibration_raw}
    supp_adj = {k: v for k, v in adjudications.items() if k[0] in supplement_raw}

    final_formal, detail_formal = derive_final(formal_raw, formal_adj)
    final_calib, detail_calib = derive_final(calibration_raw, calib_adj)
    final_supp, detail_supp = derive_supplement_final(supplement_raw, supp_adj)

    cell_rows, cell_values = aggregate_cells(final_formal, mapping, "formal")
    # 「按不适用」的格：三份都是 N/A（status == "na"）。与 pending 造成的无值严格区分。
    cell_na = {(row["metric"], row["sample_id"], row["condition"])
               for row in cell_rows if row["status"] == "na"}
    descriptive = descriptive_from_cells(cell_rows, args.seed, args.bootstrap)

    primary = []
    for contrast in PRIMARY_CONTRASTS:
        primary.append(evaluate_contrast(contrast, cell_values, cell_na))
    pvalues = [row["p_raw"] if row["p_raw"] is not None else 1.0 for row in primary]
    labels = [c["id"] for c in PRIMARY_CONTRASTS]
    holm = holm_adjust(pvalues, "primary_H1_H2a_H2b", labels=labels)
    for row in primary:
        idx = labels.index(row["contrast"])
        row["p_holm"] = holm[idx]["p_holm"]
        row["conclusion"] = conclude(row, row["p_holm"])

    # Friedman：P1–P4 各自做一次（区组 = 样本 × 4 条件），只定位维度差异。
    # 某样本在该维度上四格都按 §5.4.7 不适用（例如 P3 的 S7）时整块跳过，
    # 因为设计本身就允许它没有值；若只是零星缺格（pending）则 fail-closed。
    friedman = []
    for metric in ("P1", "P2", "P3", "P4"):
        metric_blocks = []
        n_na_blocks = 0
        broken = None
        for sample in SAMPLES:
            row = [cell_values.get(metric, {}).get((sample, condition))
                   for condition in CONDITIONS]
            if all(not isinstance(v, (int, float)) for v in row):
                if all((metric, sample, c) in cell_na for c in CONDITIONS):
                    n_na_blocks += 1
                    continue
                broken = sample
                break
            if any(not isinstance(v, (int, float)) for v in row):
                broken = sample
                break
            metric_blocks.append(row)
        if broken is not None:
            friedman.append({
                "metric": metric, "n_blocks": 0, "k": len(CONDITIONS), "q": None,
                "df": len(CONDITIONS) - 1, "p": None, "method": "friedman_chi2_df3",
                "status": "unavailable", "reason_code": "missing-cell-value",
                "note": f"样本 {broken} 有四条件缺格；不补零、不执行",
            })
            continue
        res = friedman_test(metric_blocks)
        note = res.get("note", "")
        if n_na_blocks:
            extra = f"{n_na_blocks} 个样本该维度不适用（§5.4.7），未计入区组"
            note = "；".join(x for x in (note, extra) if x)
        friedman.append({
            "metric": metric, "n_blocks": res["n_blocks"], "k": res["k"],
            "q": res["q"], "df": res["df"], "p": res["p"],
            "method": "friedman_chi2_df3", "status": res["status"],
            "reason_code": res["reason_code"], "note": note,
        })

    # 二分类比例（A1：只做描述性，不做任何推断检验）
    #
    # 为什么不做 Cochran's Q / McNemar：§5.5 要求「同格 5 次重复中 ≥3 次为 1」
    # 才能把格子折叠成样本级 0/1，而盲评每格只抽 3 份 —— 冻结文档没说清 3 份
    # 该怎么折。用「3 份比例 ≥0.5 就记 1」是我们自己发明的门槛，不是预登记判据，
    # 会直接制造出设计里没有的 p 值。因此这里只报描述性比例，推断一律
    # not_executed 并写明原因；也不新增任何未预登记的二分类比较。
    binary_rates = []
    binary_tests = []
    for item in BINARY_ITEMS:
        for condition in CONDITIONS:
            cells = []
            for sample in SAMPLES:
                value = cell_values.get(item, {}).get((sample, condition))
                if isinstance(value, (int, float)):
                    cells.append(value)
            binary_rates.append({
                "metric": item, "scope": "formal", "group": condition,
                "n_units": len(cells), "n_scored": len(cells),
                "mean_proportion": mean(cells) if cells else None,
                "note": "每格 3 份的格内比例，再对可算样本取均值；"
                        "仅 S3/S6 适用（§5.4.7），n_units 是能算出比例的格数",
            })
    for item in SUPPLEMENT_ITEMS:
        for batch in (BATCH_S7EMPTY, BATCH_B0, BATCH_SUBJ):
            ids = [bid for bid, meta in mapping.items()
                   if meta["phase"] == "supplementary" and meta["batch"] == batch]
            applicable = [bid for bid in ids if _supplement_item_applicable(item, mapping[bid])]
            scored = [final_supp[bid][item] for bid in applicable
                      if isinstance(final_supp[bid][item], (int, float))]
            binary_rates.append({
                "metric": item, "scope": "supplementary", "group": batch,
                "n_units": len(applicable), "n_scored": len(scored),
                "mean_proportion": mean(scored) if scored else None,
                "note": "补充批次描述性比例（份级 0/1，不含推断）；"
                        f"分母为该批次 {item} 适用份数，pending 不计入分子",
            })

    binary_tests.append({
        "metric": "S3_flag", "test": "none", "statistic": None, "p": None,
        "status": "not_executed",
        "note": "设计歧义：§5.5 要求同格 5 次重复中 ≥3 次为 1 才折成样本级 0/1，"
                "但盲评每格只抽 3 份；3 份如何折叠未被冻结文档定义，"
                "不能用任何自定义门槛恢复预登记的样本级判定。"
                "Cochran's Q 与 McNemar 均不执行，只报描述性比例。",
    })
    for item in SUPPLEMENT_ITEMS:
        binary_tests.append({
            "metric": item, "test": "none", "statistic": None, "p": None,
            "status": "not_executed",
            "note": "补充批次 E1/E7 只做描述性比例；冻结文档未预登记任何"
                    "补充批次的二分类推断比较，故不执行 Cochran's Q / McNemar。",
        })

    # H5：Y2/Y3/Y5（需 results.jsonl）
    h5, h5_reason, y2_table = [], "", []
    input_hashes = {}
    if not args.results:
        h5_reason = "未提供 --results（Y2/Y3/Y5 依赖 results.jsonl）"
    else:
        index, file_hashes = load_results(args.results, mapping)
        for path, digest in file_hashes.items():
            input_hashes[path] = digest
        cost_rows = cost_metrics(index, mapping)
        y2_table = _y2_table(cost_rows)
        h5, h5_holm = _h5_rows(cost_rows, args.seed, args.bootstrap)
        if h5_holm:
            holm = holm + h5_holm
        if not h5:
            h5_reason = "results.jsonl 中缺少 A/D 的成本字段"

    return {
        "mapping": mapping,
        "agreement": agreement,
        "final_formal": final_formal,
        "final_calib": final_calib,
        "final_supp": final_supp,
        "detail_formal": detail_formal,
        "detail_calib": detail_calib,
        "detail_supp": detail_supp,
        "cell_rows": cell_rows,
        "cell_values": cell_values,
        "descriptive": descriptive,
        "primary": primary,
        "holm": holm,
        "friedman": friedman,
        "binary_rates": binary_rates,
        "binary_tests": binary_tests,
        "h5": h5,
        "h5_reason": h5_reason,
        "y2_table": y2_table,
        "results_hashes": input_hashes,
    }


def _y2_table(cost_rows):
    table = {}
    for row in cost_rows:
        table.setdefault(row["sample_id"], {})[row["condition"]] = row["y2_calls_mean"]
    return [{"sample_id": s, **{c: table.get(s, {}).get(c) for c in CONDITIONS}}
            for s in SAMPLES]


def _h5_rows(cost_rows, seed, reps):
    by_cell = {}
    for row in cost_rows:
        by_cell[(row["sample_id"], row["condition"])] = row

    def series(field, log=False):
        values = {}
        for sample in SAMPLES:
            for condition in CONDITIONS:
                cell = by_cell.get((sample, condition))
                value = cell.get(field) if cell else None
                if not isinstance(value, (int, float)):
                    value = None
                values[(sample, condition)] = value
        return values

    out = []

    # Y3：对数变换（任一 ≤0 即 fail-closed，不加常数）
    y3 = series("y3_latency_mean")
    bad = [(s, c) for (s, c), v in y3.items() if v is not None and v <= 0]
    y3_diffs, y3_ok = [], not bad
    if y3_ok:
        for sample in SAMPLES:
            d = y3.get((sample, "D"))
            a = y3.get((sample, "A"))
            if d is None or a is None:
                y3_ok = False
                break
            y3_diffs.append(math.log(d) - math.log(a))
    if not y3_ok:
        out.append({
            "label": "Y3(log) D−A", "metric": "Y3_log", "m_nonzero": None,
            "mean_diff": None, "p_raw": None, "p_min_achievable": None,
            "p_holm": None, "r_rb": None, "dz": None, "ci_low": None, "ci_high": None,
            "status": "not_executed", "reason_code": "log-domain-violation",
            "note": "Y3 存在 ≤0 的延迟或缺失格值；对数变换定义域不满足，fail-closed，不加常数",
        })
    else:
        res = wilcoxon_exact(y3_diffs)
        ci = bootstrap_mean_ci(y3_diffs, reps=reps, seed=seed, tag=("H5", "Y3_log"))
        out.append({
            "label": "Y3(log) D−A", "metric": "Y3_log",
            "m_nonzero": res["m_nonzero"], "mean_diff": res["mean_diff"],
            "p_raw": res["p_raw"], "p_min_achievable": res["p_min_achievable"],
            "p_holm": None, "r_rb": res["r_rb"], "dz": res["dz"],
            "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
            "status": res["status"], "reason_code": res["reason_code"],
            "note": "对数差值的配对检验；dz 为对数尺度",
        })

    # Y5：提示 token 原始值
    y5 = series("y5_prompt_tokens_mean")
    y5_diffs, y5_ok = [], True
    for sample in SAMPLES:
        d = y5.get((sample, "D"))
        a = y5.get((sample, "A"))
        if d is None or a is None:
            y5_ok = False
            break
        y5_diffs.append(d - a)
    if not y5_ok:
        out.append({
            "label": "Y5 D−A", "metric": "Y5_prompt_tokens", "m_nonzero": None,
            "mean_diff": None, "p_raw": None, "p_min_achievable": None,
            "p_holm": None, "r_rb": None, "dz": None,
            "ci_low": None, "ci_high": None,
            "status": "not_executed", "reason_code": "missing-cell-value",
            "note": "Y5 提示 token 有缺格，未执行",
        })
    else:
        res = wilcoxon_exact(y5_diffs)
        ci = bootstrap_mean_ci(y5_diffs, reps=reps, seed=seed, tag=("H5", "Y5"))
        out.append({
            "label": "Y5 D−A", "metric": "Y5_prompt_tokens",
            "m_nonzero": res["m_nonzero"], "mean_diff": res["mean_diff"],
            "p_raw": res["p_raw"], "p_min_achievable": res["p_min_achievable"],
            "p_holm": None, "r_rb": res["r_rb"], "dz": res["dz"],
            "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
            "status": res["status"], "reason_code": res["reason_code"],
            "note": "原始 token 值；H5 家族单独 Holm",
        })

    # H5 家族单独 Holm（只对可执行的对比）
    active = [(i, r) for i, r in enumerate(out)
              if isinstance(r["p_raw"], (int, float))]
    holm_rows = []
    if active:
        adjusted = holm_adjust([r["p_raw"] for _, r in active], "H5_Y3_Y5",
                               labels=[r["label"] for _, r in active])
        for (slot, row), adj in zip(active, adjusted):
            row["p_holm"] = adj["p_holm"]
        holm_rows = adjusted
    return out, holm_rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="毕业设计盲评统计分析（只读输入、显式路径、fail-closed）",
    )
    parser.add_argument("--formal-raw", required=True, help="ratings_formal_raw.csv")
    parser.add_argument("--calibration-raw", required=True, help="ratings_calibration_raw.csv")
    parser.add_argument("--supplement-raw", required=True, help="ratings_supplement_raw.csv")
    parser.add_argument("--adjudicated", required=True, help="ratings_adjudicated.csv")
    parser.add_argument("--mapping", required=True, help="blind_private/mapping.csv")
    parser.add_argument("--results", action="append", default=None,
                        help="results.jsonl（可重复；提供才执行 Y2/Y3/Y5）")
    parser.add_argument("--out-dir", required=True, help="输出目录（必须不存在或为空）")
    parser.add_argument("--seed", type=int, default=2026111301,
                        help="bootstrap 固定种子（默认与 blind_config.json 一致）")
    parser.add_argument("--bootstrap", type=int, default=10000, help="重采样次数")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    # A5：先检查输出目录状态，失败要在做任何统计之前。
    if out_dir.exists():
        if not out_dir.is_dir():
            raise StatsError("bad-out-dir", f"--out-dir 已存在且不是目录：{out_dir}")
        if any(out_dir.iterdir()):
            raise StatsError("output-exists", f"--out-dir 已存在且非空：{out_dir}")

    input_paths = [args.formal_raw, args.calibration_raw, args.supplement_raw,
                   args.adjudicated, args.mapping] + list(args.results or [])
    missing = [str(p) for p in input_paths if not Path(p).is_file()]
    if missing:
        raise StatsError(
            "missing-input", "以下输入文件不存在：" + "、".join(missing), missing
        )
    before = {str(p): sha256_file(p) for p in input_paths}

    ctx = build_ctx(args, out_dir)

    # 输出清单与「禁止覆盖」
    outputs = {
        "inter_rater_agreement.csv": (("table", "item", "scale", "n_pairs", "n_na_rows",
                                      "n_missing_rows", "raw_agreement", "po_weighted",
                                      "pe_weighted", "kappa", "method", "status",
                                      "reason_code", "note"), ctx["agreement"]),
        "ratings_final.csv": (("blind_id", "phase", "P1", "P2", "P3", "P4", "S2",
                               "S3_flag", "S4_verdict"),
                              _final_rows(ctx["final_formal"], "formal")),
        "ratings_final_detail.csv": (("blind_id", "item", "rater_values", "kinds",
                                      "final_value", "status", "adjudicated"),
                                     ctx["detail_formal"]),
        "ratings_calibration_final.csv": (("blind_id", "phase", "P1", "P2", "P3", "P4",
                                           "S2", "S3_flag", "S4_verdict"),
                                          _final_rows(ctx["final_calib"], "calibration")),
        "ratings_supplement_final.csv": (("blind_id", "E1", "E7"),
                                         _final_supp_rows(ctx["final_supp"])),
        "ratings_supplement_final_detail.csv": (("blind_id", "item", "rater_values",
                                                 "kinds", "final_value", "status",
                                                 "adjudicated"), ctx["detail_supp"]),
        "cell_aggregates.csv": (("sample_id", "condition", "metric", "n_total",
                                 "n_effective", "n_pending", "n_na", "value",
                                 "cell_sd", "status", "note"), ctx["cell_rows"]),
        "descriptive_statistics.csv": (("condition", "metric", "n_samples", "mean", "sd",
                                        "median", "mean_within_cell_sd", "ci_low",
                                        "ci_high", "bootstrap_reps", "bootstrap_seed",
                                        "method", "status", "reason_code", "note"),
                                       ctx["descriptive"]),
        "primary_hypothesis_tests.csv": (("hypothesis", "contrast", "metric", "label",
                                          "direction", "n_blocks", "missing_samples",
                                          "n_na_excluded", "m_nonzero", "w_plus",
                                          "w_minus", "mean_diff",
                                          "median_diff", "p_raw", "p_min_achievable",
                                          "p_holm", "r_rb", "r_rb_magnitude", "dz",
                                          "dz_magnitude", "dz_reason", "status",
                                          "reason_code", "conclusion", "note"),
                                         ctx["primary"]),
        "holm_adjusted_results.csv": (("family", "contrast", "p_raw", "rank", "factor",
                                       "p_holm", "alpha", "rejected", "family_size"),
                                      _holm_rows(ctx["holm"])),
        "friedman_results.csv": (("metric", "n_blocks", "k", "q", "df", "p", "method",
                                  "status", "reason_code", "note"), ctx["friedman"]),
        "binary_rates.csv": (("metric", "scope", "group", "n_units", "n_scored",
                              "mean_proportion", "note"),
                             ctx["binary_rates"]),
        "binary_tests.csv": (("metric", "test", "statistic", "p", "status", "note"),
                             ctx["binary_tests"]),
        "h5_results.csv": (("label", "metric", "m_nonzero", "mean_diff", "p_raw",
                            "p_min_achievable", "p_holm", "r_rb", "dz", "ci_low",
                            "ci_high", "status", "reason_code", "note"), ctx["h5"]),
        "y2_calls_distribution.csv": (("sample_id", "A", "B", "C", "D"), ctx["y2_table"]),
        "input_hashes.csv": (("path", "sha256"), [
            {"path": p, "sha256": d} for p, d in sorted(before.items())
        ]),
    }
    targets = {name: out_dir / name for name in outputs}
    ensure_outputs_absent(list(targets.values()))

    # 报告（先算好文本，再统一写，保证中途失败不留下半套产物）
    from datetime import datetime, timezone
    ctx["generated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx["python"] = sys.version.split()[0]
    ctx["seed"] = args.seed
    ctx["bootstrap_reps"] = args.bootstrap
    ctx["input_hashes"] = before
    ctx["counts"] = {
        "formal": sum(1 for m in ctx["mapping"].values() if m["phase"] == "formal"),
        "calibration": sum(1 for m in ctx["mapping"].values() if m["phase"] == "calibration"),
        "supplementary": sum(1 for m in ctx["mapping"].values() if m["phase"] == "supplementary"),
        "total": len(ctx["mapping"]),
    }
    ctx["raw_rows"] = {
        "formal": FORMAL_COUNT * 2,
        "calibration": CALIBRATION_COUNT * 2,
        "supplementary": SUPPLEMENT_COUNT * 2,
    }
    report = render_report(ctx)

    # A5：事务式发布（目录级原子发布）。
    # 1) 全部输出（CSV + 报告）先写进 out_dir 旁的独立 staging 目录；
    # 2) 写完复核所有输入 SHA-256（输入在统计期间被改动就整体失败）；
    # 3) staging 内容自检通过后，用一次同文件系统的目录级 os.replace 发布整组结果；
    # 4) 发布后重新清点文件清单并重算哈希，任何异常都不在正式目录留下半套统计文件。
    output_names = sorted(list(outputs) + ["analysis_report.md"])
    staging = out_dir.parent / (out_dir.name + ".staging")
    if staging.exists() or staging.is_symlink():
        raise StatsError(
            "staging-exists", f"staging 目录已存在，拒绝复用/覆盖：{staging}"
        )
    staged_hashes = {}
    try:
        staging.mkdir(parents=True)
        for name, (columns, rows) in outputs.items():
            write_csv_atomic(staging / name, columns, rows)
        _write_text_atomic(staging / "analysis_report.md", report)
        staged_hashes = {name: sha256_file(staging / name) for name in output_names}

        # 输入哈希复核：必须在发布之前完成。
        after = {str(p): sha256_file(p) for p in input_paths}
        changed = [p for p in after if after[p] != before[p]]
        if changed:
            raise StatsError("input-mutated", f"运行期间输入被改动：{changed}")

        # staging 自检：数量与内容都要对上，避免发布半套结果。
        staged = sorted(p.name for p in staging.iterdir())
        if staged != output_names:
            raise StatsError(
                "staging-mismatch",
                f"staging 内容与预期不符：预期 {output_names}，实际 {staged}",
            )

        # 发布前目标语义：不存在才发布；已存在且为空的目录明确清掉后再发布（不静默覆盖），
        # 非空目录、普通文件或符号链接一律拒绝。
        if out_dir.is_symlink():
            raise StatsError(
                "output-exists", f"输出目录是符号链接，拒绝写入：{out_dir}"
            )
        if out_dir.exists():
            if not out_dir.is_dir():
                raise StatsError(
                    "output-exists", f"输出路径已存在且不是目录，拒绝覆盖：{out_dir}"
                )
            leftovers = sorted(p.name for p in out_dir.iterdir())
            if leftovers:
                raise StatsError(
                    "output-exists",
                    f"输出目录已存在且非空，拒绝覆盖：{out_dir}（内含 {leftovers}）",
                )
            out_dir.rmdir()

        # 同文件系统内的一次目录级 rename：要么整组就位，要么一个文件都不出现。
        os.replace(str(staging), str(out_dir))
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    # 发布后复核：清单与哈希都要与 staging 一致，否则明确报错，不假装完整。
    published = sorted(p.name for p in out_dir.iterdir())
    if published != output_names:
        raise StatsError(
            "publish-incomplete",
            f"发布后目录内容与预期不符：预期 {output_names}，实际 {published}",
        )
    mismatched = [
        name for name in output_names
        if sha256_file(out_dir / name) != staged_hashes[name]
    ]
    if mismatched:
        raise StatsError(
            "publish-incomplete", f"发布后文件哈希与 staging 不一致：{mismatched}"
        )

    print("OK 输出目录：" + str(out_dir))
    for name in output_names:
        print("  - " + name)
    return 0


def _final_rows(final_by_blind, phase):
    rows = []
    for blind_id in sorted(final_by_blind):
        final = final_by_blind[blind_id]
        row = {"blind_id": blind_id, "phase": phase}
        for item in LIKERT_ITEMS + BINARY_ITEMS + VERDICT_ITEMS:
            row[item] = final[item]
        rows.append(row)
    return rows


def _final_supp_rows(final_by_blind):
    rows = []
    for blind_id in sorted(final_by_blind):
        final = final_by_blind[blind_id]
        rows.append({"blind_id": blind_id, "E1": final["E1"], "E7": final["E7"]})
    return rows


def _holm_rows(rows):
    """Holm 明细行：contrast 已是可读标签（主家族用对比 id，H5 家族用 label）。"""
    out = []
    for row in rows:
        item = dict(row)
        out.append(item)
    return out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StatsError as exc:
        print(f"FAIL [{exc.code}] {exc.detail}", file=sys.stderr)
        for problem in exc.problems[:40]:
            print(f"  - {problem}", file=sys.stderr)
        if len(exc.problems) > 40:
            print(f"  … 另有 {len(exc.problems) - 40} 条", file=sys.stderr)
        sys.exit(2)
