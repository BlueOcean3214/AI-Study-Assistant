#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盲评包生成器（docs/GRADUATION-EXPERIMENT-DESIGN.md v4.5 §5.4）。

职责边界
--------
只读 `experiments/graduation/runs/*/results.jsonl`，派生三类产物：

1. `blind_pack/`（可分发给评分者）：`formal/`(84) + `calibration/`(6) +
   `supplementary/`(23)，每份材料一个 `<blind_id>.md`，文件名与正文只含 `blind_id`；
   另含每位评分者独立的顺序文件 `order_rater_a.csv` / `order_rater_b.csv`。
2. `blind_private/`（禁止分发、禁止提交）：`mapping.csv`、`selection_audit.csv`、
   `MAPPING-AUDIT.md`。写入前必须先通过 `.gitignore` 前置校验（fail-closed）。
3. 终端输出：抽样汇总、源文件哈希核对、去身份扫描结果。**不打印任何 blind_id↔身份对应**。

关键协议（逐条对齐 v4.5，不得先写脚本再回头改协议）
--------------------------------------------------
* 正式 84：28 格（7 样本 × A/B/C/D）× 每格 5 个 repeat_index(0..4)，排序键
  `SHA-256(f"{seed}|blind-main-selection|{sample_id}|{group}|{repeat_index}")`
  十六进制升序取前 3；同键按 repeat_index 升序。
* 校准 6：先定 84，再在剩余 56 份中按 `blind-calibration-selection` 全局升序取，
  每格至多 1、每样本至多 1，取满 6；与 84 交集必须为空。
* 补充 23：S7-empty×D 5 取 3（`blind-s7empty-selection`）+ B0 10（全量）
  + C/D-subj-S5 10（全量）。B0-S5、C/D-subj 评 E1+E7；B0-S7、S7-empty-D 只评 E1。
* `blind_id`：只由 `secrets.token_hex(16)`（os.urandom CSPRNG）生成，32 位小写十六进制；
  禁止任何确定性派生，不含身份片段，不复用。
* 去身份自检：blind_id 与文件名用“全量日期”口径扫描；正文用“身份承载日期”口径
  （run 目录日期 / 生成时间戳），因为模型输出里可能出现记忆数据自带的 2026-11-11
  这类非身份日期（v4.5 §5.4.1 的扫描目的是防止反推 run/批次，不是删改模型原文）。
  命中任何身份模式即停止打包。
* 重打包：默认拒绝覆盖已有 `mapping.csv`；`--repack` 时先把旧映射归档到
  `blind_private/mapping_archive/`，并在 `MAPPING-AUDIT.md` 追加“被替换旧映射 SHA256”。

本脚本不评分、不调用任何模型 / Embedding API、不修改 runs/、plan.json、
results.jsonl、feedback.db、seed、Gold、Prompt 与设计文档。
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_GRADUATION_DIR_FOR_IMPORT = Path(__file__).resolve().parent
if str(_GRADUATION_DIR_FOR_IMPORT) not in sys.path:
    # 允许从仓库根 import 本模块（测试就是这样导入的），manifest_rules 是同级文件
    sys.path.insert(0, str(_GRADUATION_DIR_FOR_IMPORT))

import manifest_rules  # noqa: E402  （清单解析规则必须与 check_auto.py 共用）

ManifestError = manifest_rules.ManifestError

VERSION = "1.1.0"

# v4.5 冻结值：seed 与算法版本一旦被改写，所有已产出的 selection_audit 都会失效，
# 因此脚本自己再硬编码一份，配置被随手改动时直接拒绝（不静默接受）。
FROZEN_SEED = 2026111301
FROZEN_ALGORITHM_VERSION = "sha256-domain-separated-selection-v1"

GRADUATION_DIR = Path(__file__).resolve().parent
REPO_ROOT = GRADUATION_DIR.parents[1]
RUNS_DIR = GRADUATION_DIR / "runs"
DEFAULT_PACK_DIR = GRADUATION_DIR / "blind_pack"
DEFAULT_PRIVATE_DIR = GRADUATION_DIR / "blind_private"
CONFIG_PATH = GRADUATION_DIR / "blind_config.json"
MANIFEST_PATH = REPO_ROOT / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"
SAMPLES_DIR = GRADUATION_DIR / "samples"

# §5.4.2 / §5.4.3 / §5.4.4 冻结的 domain 串（与 blind_config.json 必须一致）
DOMAIN_FORMAL = "blind-main-selection"
DOMAIN_CALIBRATION = "blind-calibration-selection"
DOMAIN_S7EMPTY = "blind-s7empty-selection"

FORMAL_SAMPLES = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
FORMAL_GROUPS = ["A", "B", "C", "D"]
REPEATS = [0, 1, 2, 3, 4]
PER_CELL = 3
CALIBRATION_COUNT = 6
S7EMPTY_TAKE = 3

MAIN_RUN = "main-20261113T061051Z"
B0_RUNS = [
    "20261112T210000-20261113T011751Z",
    "20261112T210000-20261113T012031Z",
    "b0-remainder-20261113T013105Z",
]
S7EMPTY_RUN = "suppl-s7empty-20261113T062822Z"
SUBJ_RUN = "suppl-subj-20261113T062711Z"

SOURCE_FILE = "results.jsonl"
BATCH_MAIN = "main"
BATCH_SUBJ = "suppl-subj"
BATCH_S7EMPTY = "suppl-s7empty"
BATCH_B0 = "b0-legacy"

PHASE_FORMAL = "formal"
PHASE_CALIBRATION = "calibration"
PHASE_SUPPLEMENTARY = "supplementary"

MAPPING_COLUMNS = [
    "blind_id",
    "phase",
    "batch",
    "source_run_dir",
    "source_file",
    "source_row_number",
    "experiment_id",
    "sample_id",
    "group",
    "condition",
    "repeat_index",
    "round_index",
    "source_prompt_sha256",
    "source_result_row_sha256",
    "source_results_file_sha256",
    "generated_utc",
    "generator_version",
]
AUDIT_COLUMNS = [
    "sample_id",
    "group",
    "repeat_index",
    "plan_seq",
    "source_prompt_sha256",
    "source_result_row_sha256",
    "selection_hash",
]
PRIVATE_FILES = ["mapping.csv", "selection_audit.csv", "MAPPING-AUDIT.md"]
PHASE_COUNTS = {PHASE_FORMAL: 84, PHASE_CALIBRATION: 6, PHASE_SUPPLEMENTARY: 23}
TOTAL_BLIND_IDS = sum(PHASE_COUNTS.values())
ORDER_COLUMNS = ["position", "blind_id"]
ORDER_FILES = ("order_rater_a.csv", "order_rater_b.csv")
ARCHIVE_MANIFEST_NAME = "ARCHIVE-MANIFEST.md"
# 历史归档放在私有目录内的这个子目录：它不代表「有未归档的活动旧产物」。
ARCHIVE_DIRNAME = "archive"
BLIND_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class PackError(RuntimeError):
    """打包前置条件 / 自检失败（fail-closed，绝不在错误状态下继续写文件）。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj) -> str:
    """规范 JSON：键排序、无多余空白；作为行哈希的稳定输入。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_sha256(row: dict) -> str:
    return sha256_text(canonical_json(row))


def selection_hash(seed: int, domain: str, sample_id: str, group: str, repeat_index: int) -> str:
    key = f"{seed}|{domain}|{sample_id}|{group}|{int(repeat_index)}"
    return sha256_text(key)


# --------------------------------------------------------------------------
# 配置与源数据
# --------------------------------------------------------------------------

IDENTITY_KEY_NAMES = {
    "sample_id",
    "group",
    "condition",
    "repeat_index",
    "round_index",
    "run_id",
    "experiment_id",
    "blind_id",
    "mapping",
    "selected",
    "selection",
}


def load_blind_config(path: Path | None = None) -> dict:
    path = Path(path) if path else CONFIG_PATH
    if not path.is_file():
        raise PackError(f"缺少配置文件：{path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(config, dict):
        raise PackError(f"{path} 顶层必须是 JSON 对象")
    required = {
        "protocol_version",
        "blind_sampling_seed",
        "algorithm_version",
        "selection_domains",
        "created_utc",
        "frozen",
    }
    missing = sorted(required - set(config))
    if missing:
        raise PackError(f"blind_config.json 缺字段：{missing}")
    seed = config["blind_sampling_seed"]
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise PackError("blind_sampling_seed 必须是整数")
    # 冻结校验：改写 seed / 算法版本会让历史 selection_audit 全部对不上，必须硬拒绝
    if seed != FROZEN_SEED:
        raise PackError(
            f"blind_sampling_seed 已被冻结为 {FROZEN_SEED}，实际 {seed}；"
            "修改 seed 会使所有已产出的 selection_audit.csv 失效"
        )
    algorithm_version = config["algorithm_version"]
    if algorithm_version != FROZEN_ALGORITHM_VERSION:
        raise PackError(
            f"algorithm_version 已被冻结为 {FROZEN_ALGORITHM_VERSION}，实际 {algorithm_version!r}"
        )
    if config["frozen"] is not True:
        raise PackError("blind_config.json 的 frozen 必须为 true（抽样参数已冻结）")
    domains = config["selection_domains"]
    expected = {
        "formal": DOMAIN_FORMAL,
        "calibration": DOMAIN_CALIBRATION,
        "supplementary_s7empty": DOMAIN_S7EMPTY,
    }
    if domains != expected:
        raise PackError(
            f"selection_domains 与 v4.5 §5.4 不一致：{domains!r} != {expected!r}"
        )
    if str(config["protocol_version"]) not in {"v4.5", "4.5"}:
        raise PackError(f"协议版本必须是 v4.5，实际 {config['protocol_version']!r}")
    _reject_identity_payload(config)
    return config


def _reject_identity_payload(node, trail="config") -> None:
    """配置文件不得含身份、blind_id 或抽样结果（§5.4.2）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in IDENTITY_KEY_NAMES:
                raise PackError(f"{trail}.{key} 不得出现在 blind_config.json 中")
            _reject_identity_payload(value, f"{trail}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _reject_identity_payload(value, f"{trail}[{index}]")
    elif isinstance(node, str):
        if re.search(r"\bS[1-7]\b", node) or re.search(r"\d{8}T\d{6}Z", node):
            raise PackError(f"{trail} 的值疑似含样本身份：{node!r}")


@dataclass
class SourceRow:
    run_dir: str
    batch: str
    line_no: int
    row: dict
    row_sha256: str
    results_file_sha256: str

    @property
    def sample_id(self) -> str:
        return str(self.row["sample_id"])

    @property
    def group(self) -> str:
        return str(self.row["group"])

    @property
    def repeat_index(self) -> int:
        return int(self.row["repeat_index"])

    @property
    def plan_seq(self):
        value = self.row.get("plan_seq")
        return "" if value is None else int(value)

    @property
    def round_index(self):
        value = self.row.get("round_index")
        return "" if value is None else int(value)

    @property
    def experiment_id(self) -> str:
        return str(self.row.get("experiment_id") or "")

    @property
    def prompt_sha256(self) -> str:
        return str(self.row.get("prompt_sha256") or "")

    @property
    def condition(self) -> str:
        # 本实验里 condition 就是 group（A/B/C/D/C-subj/D-subj/B0），逐条照抄
        return self.group

    @property
    def identity_tuple(self):
        return (self.run_dir, self.line_no, self.row_sha256)


def _as_dict(value, what: str):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:  # pragma: no cover - 防御
            raise PackError(f"{what} 不是合法 JSON：{exc}") from exc
        if isinstance(parsed, dict):
            return parsed
    raise PackError(f"{what} 必须是 JSON 对象，实际 {type(value).__name__}")


def read_rows(run_dir_name: str, batch: str, runs_dir: Path | None = None) -> list[SourceRow]:
    runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
    path = runs_dir / run_dir_name / SOURCE_FILE
    if not path.is_file():
        raise PackError(f"缺少源文件：{path}")
    file_sha = sha256_file(path)
    rows: list[SourceRow] = []
    seen = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PackError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
            if not isinstance(row, dict):
                raise PackError(f"{path}:{line_no} 行必须是 JSON 对象")
            _validate_row_shape(row, f"{path}:{line_no}")
            key = (row["sample_id"], row["group"], int(row["repeat_index"]))
            if key in seen:
                raise PackError(
                    f"{run_dir_name} 内身份重复：{key} 同时出现在第 {seen[key]} 行与第 {line_no} 行"
                )
            seen[key] = line_no
            rows.append(
                SourceRow(
                    run_dir=run_dir_name,
                    batch=batch,
                    line_no=line_no,
                    row=row,
                    row_sha256=row_sha256(row),
                    results_file_sha256=file_sha,
                )
            )
    if not rows:
        raise PackError(f"{path} 没有任何记录")
    return rows


def _validate_row_shape(row: dict, where: str) -> None:
    for field in ("sample_id", "group"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise PackError(f"{where} 缺合法 {field}")
    repeat = row.get("repeat_index")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 0:
        raise PackError(f"{where} repeat_index 必须是非负整数，实际 {repeat!r}")
    if not isinstance(row.get("success"), bool):
        raise PackError(f"{where} success 必须是布尔值")
    prompt_sha = row.get("prompt_sha256")
    if not isinstance(prompt_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha):
        raise PackError(f"{where} prompt_sha256 必须是 64 位小写十六进制")
    if not isinstance(row.get("experiment_id"), str) or not row["experiment_id"].strip():
        raise PackError(f"{where} 缺 experiment_id")
    _as_dict(row.get("final_output"), f"{where} final_output")


def load_main_rows(runs_dir: Path | None = None) -> list[SourceRow]:
    rows = read_rows(MAIN_RUN, BATCH_MAIN, runs_dir)
    cells: dict[tuple, list[int]] = {}
    for item in rows:
        cells.setdefault((item.sample_id, item.group), []).append(item.repeat_index)
    expected_cells = {(s, g) for s in FORMAL_SAMPLES for g in FORMAL_GROUPS}
    if set(cells) != expected_cells:
        missing = sorted(expected_cells - set(cells))
        extra = sorted(set(cells) - expected_cells)
        raise PackError(f"主实验格子不符：缺 {missing}，多 {extra}")
    for cell, repeats in sorted(cells.items()):
        if sorted(repeats) != REPEATS:
            raise PackError(f"主实验格子 {cell} 的 repeat_index 必须是 0..4，实际 {sorted(repeats)}")
    if len(rows) != len(expected_cells) * len(REPEATS):
        raise PackError(f"主实验应有 140 行，实际 {len(rows)}")
    return rows


def load_b0_rows(runs_dir: Path | None = None) -> list[SourceRow]:
    runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
    rows: list[SourceRow] = []
    for run_dir in B0_RUNS:
        rows.extend(read_rows(run_dir, BATCH_B0, runs_dir))
    samples = sorted({item.sample_id for item in rows})
    if samples != ["S5", "S7"]:
        raise PackError(f"B0 只允许 S5/S7，实际 {samples}")
    for sample in samples:
        count = sum(1 for item in rows if item.sample_id == sample)
        if count != 5:
            raise PackError(f"B0-{sample} 应有 5 份，实际 {count}")
    if len(rows) != 10:
        raise PackError(f"B0 合计应为 10 份，实际 {len(rows)}")
    return rows


def load_s7empty_rows(runs_dir: Path | None = None) -> list[SourceRow]:
    runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
    rows = read_rows(S7EMPTY_RUN, BATCH_S7EMPTY, runs_dir)
    if len(rows) != 5 or sorted(item.repeat_index for item in rows) != REPEATS:
        raise PackError("S7-empty 源输出必须是 5 份 repeat_index 0..4")
    if {item.sample_id for item in rows} != {"S7-empty"} or {item.group for item in rows} != {"D"}:
        raise PackError("S7-empty 源输出必须全部是 S7-empty × D")
    return rows


def load_subj_rows(runs_dir: Path | None = None) -> list[SourceRow]:
    rows = read_rows(SUBJ_RUN, BATCH_SUBJ, runs_dir)
    if len(rows) != 10:
        raise PackError(f"suppl-subj 应有 10 行，实际 {len(rows)}")
    samples = {item.sample_id for item in rows}
    if samples != {"S5"}:
        raise PackError(f"suppl-subj 的 sample_id 必须全部是 S5，实际 {sorted(samples)}")
    by_group: dict[str, list[int]] = {}
    for item in rows:
        by_group.setdefault(item.group, []).append(item.repeat_index)
    if set(by_group) != {"C-subj", "D-subj"}:
        raise PackError(f"suppl-subj 分组不符：{sorted(by_group)}")
    for group, repeats in sorted(by_group.items()):
        if sorted(repeats) != REPEATS:
            raise PackError(f"suppl-subj 的 {group} repeat_index 必须恰好覆盖 0..4，实际 {sorted(repeats)}")
    return rows


# --------------------------------------------------------------------------
# 抽样（§5.4.2 / §5.4.3 / §5.4.4）
# --------------------------------------------------------------------------


@dataclass
class Pick:
    row: SourceRow
    hash: str


def select_formal(rows: list[SourceRow], seed: int, domain: str = DOMAIN_FORMAL) -> list[Pick]:
    cells: dict[tuple, list[SourceRow]] = {}
    for item in rows:
        cells.setdefault((item.sample_id, item.group), []).append(item)
    picks: list[Pick] = []
    for cell in sorted(cells):
        candidates = cells[cell]
        scored = [
            Pick(item, selection_hash(seed, domain, item.sample_id, item.group, item.repeat_index))
            for item in candidates
        ]
        scored.sort(key=lambda pick: (pick.hash, pick.row.repeat_index))
        if len(scored) < PER_CELL:
            raise PackError(f"格子 {cell} 候选不足 {PER_CELL} 份")
        picks.extend(scored[:PER_CELL])
    if len(picks) != 84:
        raise PackError(f"正式批次应为 84 份，实际 {len(picks)}")
    return picks


def select_calibration(
    rows: list[SourceRow], seed: int, used: set, domain: str = DOMAIN_CALIBRATION
) -> list[Pick]:
    scored = [
        Pick(item, selection_hash(seed, domain, item.sample_id, item.group, item.repeat_index))
        for item in rows
        if item.identity_tuple not in used
    ]
    scored.sort(key=lambda pick: (pick.hash, pick.row.sample_id, pick.row.group, pick.row.repeat_index))
    chosen: list[Pick] = []
    cells: set[tuple] = set()
    samples: set[str] = set()
    for pick in scored:
        cell = (pick.row.sample_id, pick.row.group)
        if cell in cells or pick.row.sample_id in samples:
            continue
        chosen.append(pick)
        cells.add(cell)
        samples.add(pick.row.sample_id)
        if len(chosen) == CALIBRATION_COUNT:
            break
    if len(chosen) != CALIBRATION_COUNT:
        raise PackError(f"校准份不足：只取到 {len(chosen)} 份")
    return chosen


def select_s7empty(rows: list[SourceRow], seed: int, domain: str = DOMAIN_S7EMPTY) -> list[Pick]:
    scored = [
        Pick(item, selection_hash(seed, domain, item.sample_id, item.group, item.repeat_index))
        for item in rows
    ]
    scored.sort(key=lambda pick: (pick.hash, pick.row.repeat_index))
    chosen = scored[:S7EMPTY_TAKE]
    if len(chosen) != S7EMPTY_TAKE:
        raise PackError("S7-empty 抽样份数不足")
    return chosen


# --------------------------------------------------------------------------
# 样本上下文（只读 samples/*.json，不读 prompt 文件以免带入 run 指纹）
# --------------------------------------------------------------------------


def load_sample_context(sample_id: str, samples_dir: Path | None = None) -> dict:
    samples_dir = Path(samples_dir) if samples_dir else SAMPLES_DIR
    path = samples_dir / f"{sample_id}.json"
    if not path.is_file():
        raise PackError(f"缺少样本文件：{path}")
    sample = json.loads(path.read_text(encoding="utf-8"))
    if sample.get("sample_id") != sample_id:
        raise PackError(f"{path} 的 sample_id 与文件名不一致")
    return sample


def feedback_summary(sample: dict) -> list[str]:
    """把样本反馈压成不含日期、不含样本号的匿名背景（评分者需要知道历史状态）。"""
    lines = []
    feedbacks = sample.get("feedbacks") or []
    if not feedbacks:
        lines.append("- 该学习者暂无历史反馈记录（零历史）。")
        return lines
    for index, feedback in enumerate(feedbacks, start=1):
        status = {"completed": "完成", "partial": "部分完成", "not_completed": "未完成"}.get(
            str(feedback.get("status")), str(feedback.get("status"))
        )
        reason = feedback.get("reason")
        extra = f"，原因：{reason}" if reason else ""
        lines.append(f"- 历史反馈 {index}：{status}{extra}")
    return lines


# --------------------------------------------------------------------------
# 材料与去身份扫描
# --------------------------------------------------------------------------

# 身份模式：来源是 §5.4.1 列的 sample / group / repeat / run 片段
IDENTITY_PATTERNS = [
    ("sample_id", re.compile(r"(?<![A-Za-z0-9_])S[1-7](?![0-9A-Za-z_-])")),
    ("s7empty", re.compile(r"S7-empty")),
    ("group_b0", re.compile(r"(?<![A-Za-z0-9_])B0(?![A-Za-z0-9_])")),
    ("group_subj", re.compile(r"(?:C|D)-subj")),
    ("repeat", re.compile(r"(?<![A-Za-z0-9_])r[0-4](?![0-9A-Za-z_])")),
    ("run_dir", re.compile(r"(?:main|suppl-subj|suppl-s7empty|b0-remainder)-\d{8}T\d{6}Z")),
    ("bare_group", re.compile(r"(?<![A-Za-z0-9_])[ABCD](?![A-Za-z0-9_])")),
]
# 只用于 blind_id / 文件名：任何“像真日期”的子串都算命中（ID 是纯十六进制，理论不可能命中；
# 月份/日必须合法，避免 20261879 这类随机十六进制误报）
STRICT_DATE_PATTERNS = [
    (
        "date",
        re.compile(
            r"(?<!\d)20\d{2}[-/]?(?:0[1-9]|1[0-2])[-/]?(?:0[1-9]|[12]\d|3[01])(?!\d)"
        ),
    ),
    (
        "date_compact",
        re.compile(r"(?<!\d)20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"),
    ),
]
# 正文额外口径：只拦“能反推 run/批次”的日期与时间戳（§5.4.1 的扫描目的）
RUN_DATE_PATTERNS = [
    ("run_date", re.compile(r"(?<!\d)2026[-/]?11[-/]?13(?!\d)")),
    ("run_stamp", re.compile(r"20261113T\d{6}Z?")),
    ("run_clock", re.compile(r"\b\d{2}:\d{2}:\d{2}\+08:00\b")),
]


def scan_text(text: str, patterns) -> list[tuple[str, str]]:
    hits = []
    for name, pattern in patterns:
        for match in pattern.finditer(text):
            hits.append((name, match.group(0)))
    return hits


def assert_identity_free(where: str, text: str, patterns=IDENTITY_PATTERNS) -> None:
    hits = scan_text(text, patterns)
    if hits:
        detail = "，".join(f"{name}={value!r}" for name, value in hits[:5])
        raise PackError(f"去身份扫描失败：{where} 命中 {detail}")


def render_packet(item: dict, context: dict) -> str:
    row = item["row"]
    final_output = _as_dict(row.row["final_output"], "final_output")
    answer = final_output.get("answer")
    plan = final_output.get("plan")
    lines = [
        f"# 盲评材料 {item['blind_id']}",
        "",
        "> 本材料已去身份：只保留用户请求、匿名化的历史状态、模型回答与计划。"
        "评分项与分数请写在评分表中，不要在本文件里写任何身份信息。",
        "",
        "## 用户请求",
        "",
        str(context.get("current_user_request") or "（样本未提供）").strip(),
        "",
        "## 学习者历史（程序统计，匿名化）",
        "",
        *feedback_summary(context),
        "",
        "## 模型回答",
        "",
        (answer if isinstance(answer, str) and answer.strip() else "（该次生成未产出可读回答）").strip(),
        "",
        "## 学习计划（JSON）",
        "",
        "```json",
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True)
        if plan is not None
        else "null",
        "```",
        "",
    ]
    return "\n".join(lines)


def scan_packet_body(item: dict, text: str) -> list[tuple[str, str]]:
    """正文扫描：身份模式 + run 日期/时间戳；返回命中（调用方决定是否中止）。"""
    return scan_text(text, IDENTITY_PATTERNS + RUN_DATE_PATTERNS)


# --------------------------------------------------------------------------
# .gitignore 前置校验（fail-closed）
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# 私有目录的忽略保护必须覆盖「整个目录」，而不是只覆盖当下写到的几个文件名：
# 将来新增的归档、清单、临时子目录一旦落在这个目录里，同样不能被提交。
# 这些探针路径（相对 private_dir，多数文件此刻并不存在）就是用来证明
# 「目录级规则生效」而不是「恰好三个文件名被点名忽略」。
PRIVATE_IGNORE_PROBES = (
    "mapping.csv",
    "selection_audit.csv",
    "MAPPING-AUDIT.md",
    "archive/ARCHIVE-MANIFEST.md",
    "archive/pack-test/private/mapping.csv",
    "archive/pack-test/blind_pack/formal/example.md",
    ".staging-test/ARCHIVE-MANIFEST.md",
    # 未来才会出现的文件名：只有目录级规则能覆盖它
    ".gate-probe/未来新文件.md",
)


def _check_ignore(repo_root: Path, target: Path) -> subprocess.CompletedProcess:
    """git check-ignore -v --no-index；老版本 git 不认 --no-index 时退回普通调用。"""

    result = _git(["check-ignore", "-v", "--no-index", str(target)], repo_root)
    if result.returncode != 0 and "unknown option" in result.stderr:
        result = _git(["check-ignore", "-v", str(target)], repo_root)
    return result


def ensure_private_gate(private_dir: Path, repo_root: Path | None = None) -> list[str]:
    """返回证据行；失败抛 PackError（绝不创建/写入私有文件）。

    仓库外路径一律拒绝：不存在任何“临时目录免检”的正式入口。
    测试通过把 `REPO_ROOT`（或 repo_root 参数）指向临时 Git 仓库来验证同一套逻辑。
    """

    evidence: list[str] = []
    repo_root = Path(repo_root).resolve() if repo_root else REPO_ROOT
    resolved = Path(private_dir).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        raise PackError(
            f"私有目录 {resolved} 不在仓库 {repo_root} 内；私有映射只能写在仓库内已被 "
            ".gitignore 忽略的目录，拒绝写入（不存在绕过开关）"
        ) from None

    top = _git(["rev-parse", "--show-toplevel"], repo_root)
    if top.returncode != 0:
        raise PackError(f"git rev-parse 失败（{repo_root}）：{top.stderr.strip()}")
    if Path(top.stdout.strip()).resolve() != repo_root:
        raise PackError(f"仓库根不一致：git={top.stdout.strip()} 期望={repo_root}")
    evidence.append(f"[gate] git rev-parse --show-toplevel = {top.stdout.strip()}")

    ignore_file = repo_root / ".gitignore"
    if not ignore_file.is_file():
        raise PackError(f"仓库缺少 .gitignore（{ignore_file}），无法证明私有目录被忽略")

    # 1) 目录本身必须被一条「目录规则」（以 / 结尾）忽略：这才是覆盖未来文件的前提。
    #    探针用一个此刻不存在的深层文件名，命中的规则就应该是那条目录规则。
    future_probe = resolved / PRIVATE_IGNORE_PROBES[-1]
    future_result = _check_ignore(repo_root, future_probe)
    # git check-ignore -v 的一行是「来源:行号:规则<TAB>路径」；
    # 规则必须从第一列取，第二列是路径（本任务里恰好也是不以 / 结尾的文件名）。
    future_pattern = ""
    if future_result.returncode == 0 and future_result.stdout.strip():
        source_line = future_result.stdout.strip().split("\t", 1)[0]
        parts = source_line.split(":", 2)
        if len(parts) == 3:
            future_pattern = parts[2]
    if not future_pattern.endswith("/"):
        raise PackError(
            "私有目录没有被目录级 .gitignore 规则覆盖（只忽略了若干固定文件名不够），拒绝写入。"
            "请把 experiments/graduation/blind_private/ 整目录加入 .gitignore 并验证 "
            f"git check-ignore -v {future_probe}\n"
            f"git 退出码={future_result.returncode} 输出={future_result.stdout.strip()!r} "
            f"stderr={future_result.stderr.strip()!r}"
        )
    evidence.append(f"[gate] 目录级忽略规则：{future_result.stdout.strip()}")

    # 2) 代表性路径（含 archive/ 深层文件与此刻不存在的未来文件名）必须逐条被忽略。
    for name in PRIVATE_IGNORE_PROBES:
        target = resolved / name
        result = _check_ignore(repo_root, target)
        if result.returncode != 0 or ".gitignore" not in result.stdout:
            raise PackError(
                "私有目录内的路径未被 .gitignore 覆盖，拒绝写入（忽略规则必须覆盖整个目录，"
                f"不能只点名若干文件）：{name}\n"
                f"git 退出码={result.returncode} 输出={result.stdout.strip()!r} "
                f"stderr={result.stderr.strip()!r}"
            )
        evidence.append(result.stdout.strip())

    # 3) 目录里不能藏着已被跟踪的文件（在 .gitignore 生效前就被 add 过的那些）
    tracked = _git(["ls-files", "--", str(resolved)], repo_root)
    if tracked.returncode != 0:
        raise PackError(f"git ls-files 失败：{tracked.stderr.strip()}")
    if tracked.stdout.strip():
        raise PackError(
            "私有目录中存在已被 Git 跟踪的文件，先停止并清理：\n" + tracked.stdout.strip()
        )
    evidence.append("[gate] git ls-files 无私有文件（0 条）")
    return evidence


def ensure_path_layout(pack_dir: Path, private_dir: Path) -> None:
    """盲评包目录与私有目录必须互不包含：私有映射绝不能落在评分者可见的目录里。"""

    pack_dir = Path(pack_dir).resolve()
    private_dir = Path(private_dir).resolve()
    if pack_dir == private_dir:
        raise PackError(f"pack_dir 与 private_dir 不能是同一目录：{pack_dir}")
    for outer, inner, label in (
        (pack_dir, private_dir, "private_dir 位于 pack_dir 内（会被分发给评分者）"),
        (private_dir, pack_dir, "pack_dir 位于 private_dir 内（评分材料会被当成私有文件）"),
    ):
        try:
            inner.relative_to(outer)
        except ValueError:
            continue
        raise PackError(f"路径冲突：{label}：{outer} / {inner}")


def ensure_not_within_runs(target: Path, runs_dir: Path, label: str) -> None:
    """目录不得与实验 run 目录互相包含。

    正式实验结果（runs/<run>/results.jsonl）是只读证据：包目录若落在 runs/ 内，
    --repack 归档旧包时会把 run 目录当成旧包内容搬进归档；私有目录若落在 runs/ 内，
    mapping.csv 会写进被 .gitignore 的目录里，泄露与丢失都无人察觉。
    """

    target_real = Path(target).resolve()
    runs_real = Path(runs_dir).resolve()
    if target_real == runs_real:
        raise PackError(f"{label} 不能就是实验 run 目录：{runs_real}")
    for outer, inner, reason in (
        (runs_real, target_real, f"{label} 位于实验 run 目录内"),
        (target_real, runs_real, f"实验 run 目录位于{label}内"),
    ):
        try:
            inner.relative_to(outer)
        except ValueError:
            continue
        raise PackError(f"路径冲突：{reason}：{outer} / {inner}")


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def render_csv_text(columns: list[str], rows: list[dict]) -> str:
    """把 CSV 渲染成字符串（行尾统一 \n），便于先算哈希/字节数再落盘。"""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in columns})
    return buffer.getvalue()


def write_csv_atomic(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(render_csv_text(columns, rows))
    os.replace(tmp, path)


SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def normalize_manifest_path(value: str) -> str:
    """把 manifest 的 relative_path 规范化成唯一形式，作为条目存在性的主键。

    只 `strip("/")` 不够：`a\\b`、`a//b`、`./a/b`、`a/./b` 指向的是同一个文件，
    规范化不彻底就能用另一种写法绕过「同一路径重复出现」的检查，条目缺失也就重新可辩称。

    规则本体在 `manifest_rules.normalize_relative_path()`：核查器 `check_auto.py`
    使用同一份实现，否则同一份清单会得到两种「可信」结论。
    """

    try:
        return manifest_rules.normalize_relative_path(value)
    except ManifestError as exc:
        raise PackError(str(exc)) from exc


def manifest_hashes(path: Path | None = None) -> dict[str, dict]:
    """读取执行报告附录 A 的清单 → {规范化 relative_path: row}。

    键只能是 relative_path 本身。早先以 `(relative_path, file)` 为键，`file` 列被清空或
    写错时同一路径就查不到条目，B0 记录会被误判成「manifest 未收录」而走 legacy 放行；
    现在路径存在即必须按条目校验，`file` 列的问题由 `manifest_rules.load_manifest()`
    在读取阶段就判失败（`file` 空、`file` 与路径不符、空路径但同行有内容、规范后重复）。

    解析与失败规则全部由 `manifest_rules` 承担，与核查器共用同一份实现。
    """

    path = Path(path) if path else MANIFEST_PATH
    try:
        return manifest_rules.load_manifest(path)
    except ManifestError as exc:
        raise PackError(str(exc)) from exc


def _manifest_int(entry: dict, field: str, allow_zero: bool) -> tuple[int | None, str]:
    """取清单里的整数列，返回 (值, 错误描述)。空、非数字、越界都算错误。"""

    text = str(entry.get(field) or "").strip()
    if not text:
        return None, f"{field} 为空"
    if not text.isdigit():
        return None, f"{field} 不是合法非负整数：{text!r}"
    value = int(text)
    if value == 0 and not allow_zero:
        return None, f"{field} 必须为正整数：{text!r}"
    return value, ""


# B0 是基线提交之前的 legacy 结果，manifest 允许没有条目；主实验与新补充批次不允许缺。
LEGACY_MANIFEST_OPTIONAL = frozenset(B0_RUNS)


def source_run_dirs() -> list[str]:
    return [MAIN_RUN, *B0_RUNS, S7EMPTY_RUN, SUBJ_RUN]


def snapshot_sources(run_dirs=None, runs_dir: Path | None = None) -> dict[str, dict]:
    """从真实磁盘重算每个 run 的 SHA256 / 字节数 / 记录条数（不使用任何缓存值）。"""

    runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
    snapshot: dict[str, dict] = {}
    for run_dir in run_dirs or source_run_dirs():
        path = runs_dir / run_dir / SOURCE_FILE
        if not path.is_file():
            raise PackError(f"缺少源文件：{path}")
        records = 0
        with path.open("rb") as handle:
            for line in handle:
                if line.strip():
                    records += 1
        snapshot[run_dir] = {
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "records": records,
        }
    return snapshot


def _describe_mismatch(row: dict) -> str:
    """把不一致字段逐个写清楚：只报「不一致」而不说哪一项，等于让人猜。"""

    parts = [row["run_dir"], f"status={row['status']}"]
    if "sha256" in row["status"]:
        parts.append(f"expected_sha256={row['expected']}")
        parts.append(f"actual_sha256={row['actual']}")
    if "size_bytes" in row["status"]:
        parts.append(f"expected_size_bytes={row['expected_size_bytes']}")
        parts.append(f"actual_size_bytes={row['size_bytes']}")
    if "records" in row["status"]:
        parts.append(f"expected_records={row['expected_records']}")
        parts.append(f"actual_records={row['records']}")
    if "file" in row["status"]:
        parts.append(f"manifest_file={row.get('manifest_file', '')!r}")
        parts.append(f"expected_file={SOURCE_FILE!r}")
    return " ".join(parts)


def verify_snapshot(snapshot: dict[str, dict], manifest: dict) -> list[dict]:
    """打包前核对：源文件哈希/大小/条数必须与执行报告附录 A（manifest）一致。"""

    report: list[dict] = []
    for run_dir, actual in sorted(snapshot.items()):
        relative = normalize_manifest_path(f"experiments/graduation/runs/{run_dir}/{SOURCE_FILE}")
        entry = manifest.get(relative)
        row = {
            "run_dir": run_dir,
            "expected": "",
            "actual": actual["sha256"],
            "size_bytes": actual["size_bytes"],
            "records": actual["records"],
            "expected_size_bytes": "",
            "expected_records": "",
            "manifest_file": "",
            "status": "",
        }
        if entry is None:
            row["status"] = (
                "legacy_no_manifest" if run_dir in LEGACY_MANIFEST_OPTIONAL else "manifest_missing"
            )
            report.append(row)
            continue
        # 条目存在就说明该路径被 manifest 收录过，此时 `file` 列空掉或写错意味着清单本身不可信，
        # 必须判失败——不能因为按 (路径, file) 查不到就把 B0 记录当成「条目缺失」放行。
        # `manifest_hashes()` 已在读取阶段拦下这类清单；这里再判一次，是因为本函数也可能
        # 直接收到手工构造的条目表（测试与将来的调用方），留一层独立防线。
        file_problem, _file_detail = manifest_rules.entry_file_problem(relative, entry, SOURCE_FILE)
        row["manifest_file"] = str(entry.get(manifest_rules.FILE_COLUMN) or "").strip()
        row["expected"] = (entry.get("sha256") or "").strip().lower()
        problems = []
        if file_problem:
            problems.append(file_problem)
        # 有清单条目就必须逐字段完整核对：空字段不是「没写就先放过」，
        # 而是「无法证明源文件没被改过」，一样要停下来。
        if not row["expected"]:
            problems.append("sha256_empty")
        elif not SHA256_HEX.match(row["expected"]):
            problems.append("sha256_invalid")
        elif row["expected"] != actual["sha256"]:
            problems.append("sha256")

        expected_size, error = _manifest_int(entry, "size_bytes", allow_zero=True)
        row["expected_size_bytes"] = (entry.get("size_bytes") or "").strip()
        if error:
            problems.append("size_bytes_" + ("empty" if not row["expected_size_bytes"] else "invalid"))
        elif expected_size != actual["size_bytes"]:
            problems.append("size_bytes")

        expected_records, error = _manifest_int(entry, "records", allow_zero=False)
        row["expected_records"] = (entry.get("records") or "").strip()
        if error:
            problems.append("records_" + ("empty" if not row["expected_records"] else "invalid"))
        elif expected_records != actual["records"]:
            problems.append("records")

        row["status"] = "match" if not problems else "mismatch:" + ",".join(problems)
        report.append(row)

    mismatched = [row for row in report if row["status"].startswith("mismatch")]
    if mismatched:
        raise PackError(
            "源 results.jsonl 与 manifest 不一致，停止打包："
            + "；".join(_describe_mismatch(row) for row in mismatched)
        )
    missing = [row for row in report if row["status"] == "manifest_missing"]
    if missing:
        raise PackError(
            "主实验/补充实验源文件在 manifest 中缺失（只有 B0 legacy 允许例外）："
            + "、".join(row["run_dir"] for row in missing)
        )
    return report


def verify_sources_unchanged(before: dict[str, dict], after: dict[str, dict]) -> None:
    """打包前后逐 run 比对真实哈希；任一变化即失败，绝不报告打包成功。"""

    if set(before) != set(after):
        raise PackError(f"打包前后源文件集合不一致：before={sorted(before)} after={sorted(after)}")
    for run_dir in sorted(before):
        if before[run_dir] != after[run_dir]:
            raise PackError(
                f"打包期间源文件被改动：{run_dir} before={before[run_dir]} after={after[run_dir]}"
            )


def build_items(seed: int, runs_dir: Path | None = None):
    runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
    main_rows = load_main_rows(runs_dir)
    b0_rows = load_b0_rows(runs_dir)
    s7_rows = load_s7empty_rows(runs_dir)
    subj_rows = load_subj_rows(runs_dir)

    formal = select_formal(main_rows, seed)
    used = {pick.row.identity_tuple for pick in formal}
    remaining = [item for item in main_rows if item.identity_tuple not in used]
    if len(remaining) != 56:
        raise PackError(f"正式批次之外应剩 56 份，实际 {len(remaining)}")
    calibration = select_calibration(remaining, seed, used)
    used_cal = {pick.row.identity_tuple for pick in calibration}
    if used & used_cal:
        raise PackError("校准 6 份与正式 84 份存在交集，必须重抽")

    s7_picked = select_s7empty(s7_rows, seed)
    # C/D-subj 整批全取、不做抽样（同 B0）：selection_hash 留空。
    # 这里曾经套用 DOMAIN_FORMAL 计算一份「正式批次抽样键」，但该批从未按抽签入选，
    # mapping/selection_audit 里写出一个 formal 域的哈希会被读成「这是主实验抽样结果」。
    subj_all = [Pick(item, "") for item in subj_rows]

    def make(phase, batch, picks, evaluate_e7):
        return [
            {
                "phase": phase,
                "batch": batch,
                "row": pick.row,
                "selection_hash": pick.hash,
                "evaluate_e7": evaluate_e7,
            }
            for pick in picks
        ]

    items = []
    items.extend(make(PHASE_FORMAL, BATCH_MAIN, formal, False))
    items.extend(make(PHASE_CALIBRATION, BATCH_MAIN, calibration, False))
    items.extend(make(PHASE_SUPPLEMENTARY, BATCH_S7EMPTY, s7_picked, False))
    items.extend(make(PHASE_SUPPLEMENTARY, BATCH_B0, [Pick(r, "") for r in b0_rows], False))
    items.extend(make(PHASE_SUPPLEMENTARY, BATCH_SUBJ, subj_all, True))
    for item in items:
        row = item["row"]
        if item["batch"] == BATCH_B0:
            item["evaluate_e7"] = row.sample_id == "S5"
        elif item["batch"] == BATCH_S7EMPTY:
            item["evaluate_e7"] = False

    counts = {
        "formal": sum(1 for i in items if i["phase"] == PHASE_FORMAL),
        "calibration": sum(1 for i in items if i["phase"] == PHASE_CALIBRATION),
        "supplementary": sum(1 for i in items if i["phase"] == PHASE_SUPPLEMENTARY),
    }
    if counts != {"formal": 84, "calibration": 6, "supplementary": 23}:
        raise PackError(f"phase 计数必须为 84/6/23，实际 {counts}")
    return {
        "items": items,
        "main_rows": main_rows,
        "formal": formal,
        "calibration": calibration,
        "counts": counts,
    }


def assign_ids(items: list[dict]) -> None:
    used: set[str] = set()
    for item in items:
        attempts = 0
        while True:
            blind_id = secrets.token_hex(16)
            attempts += 1
            if attempts > 10000:  # pragma: no cover - 防御性上限，CSPRNG 正常不会触发
                raise PackError("blind_id 生成连续被拒超过 10000 次，怀疑随机源异常")
            if blind_id in used:
                continue
            if scan_text(blind_id, IDENTITY_PATTERNS + STRICT_DATE_PATTERNS):
                # 纯十六进制偶然拼出日期形态时直接丢弃重抽，绝不改写 ID
                continue
            break
        used.add(blind_id)
        assert_identity_free(f"blind_id={blind_id}", blind_id, IDENTITY_PATTERNS + STRICT_DATE_PATTERNS)
        item["blind_id"] = blind_id


def order_rows(items: list[dict], rng=None) -> list[tuple[int, str]]:
    rng = rng if rng is not None else secrets.SystemRandom()
    shuffled = list(items)
    rng.shuffle(shuffled)
    return [(index + 1, item["blind_id"]) for index, item in enumerate(shuffled)]


def build_rater_orders(items: list[dict], rng=None) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """两位评分者的材料顺序必须独立随机且**必定不同**。

    两次独立 shuffle 在数学上可能恰好一致（n! 分之一），这里显式排除：
    重试有限次后仍未不同，就确定性地旋转一位作为兜底，保证 sequence 不相等。
    """

    rng = rng if rng is not None else secrets.SystemRandom()
    first = order_rows(items, rng)
    if len(first) < 2:
        return first, first
    first_seq = [blind_id for _, blind_id in first]
    for _ in range(64):
        second = order_rows(items, rng)
        if [blind_id for _, blind_id in second] != first_seq:
            return first, second
    rotated = first_seq[1:] + first_seq[:1]
    return first, [(index + 1, blind_id) for index, blind_id in enumerate(rotated)]


def render_audit(
    *,
    generated_utc: str,
    config_sha: str,
    seed: int,
    algorithm_version: str,
    counts: dict,
    source_report: list[dict],
    gate_evidence: list[str],
    mapping_sha: str,
    mapping_bytes: int,
    mapping_rows: int,
    body_warnings: list[str],
    pack_root: Path,
    private_dir: Path,
    repack_notes: list[str],
) -> str:
    lines = [
        "# MAPPING-AUDIT（私有留档，禁止分发 / 禁止提交 Git）",
        "",
        f"- 生成时间 (UTC)：{generated_utc}",
        f"- 生成器版本：{VERSION}",
        f"- 协议版本：v4.5（docs/GRADUATION-EXPERIMENT-DESIGN.md §5.4）",
        f"- blind_sampling_seed：{seed}",
        f"- algorithm_version：{algorithm_version}",
        f"- blind_config.json SHA256：{config_sha}",
        f"- 盲评包目录：{pack_root}",
        f"- 私有目录：{private_dir}",
        "",
        "## 1. 份数",
        "",
        f"- formal：{counts['formal']}（期望 84）",
        f"- calibration：{counts['calibration']}（期望 6）",
        f"- supplementary：{counts['supplementary']}（期望 23）",
        "",
        "## 2. mapping.csv 自身审计",
        "",
        f"- SHA256：{mapping_sha}",
        f"- 字节数：{mapping_bytes}",
        f"- 行数（不含表头）：{mapping_rows}",
        "",
        "## 3. 源 results.jsonl 哈希核对（打包前 = 打包后 = 打包时）",
        "",
        "| run_dir | manifest | 实际 SHA256 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    for row in source_report:
        expected = row["expected"] or "（manifest 未收录）"
        lines.append(f"| {row['run_dir']} | {expected} | {row['actual']} | {row['status']} |")
    lines += [
        "",
        "## 4. .gitignore 前置校验证据",
        "",
        "```",
        *gate_evidence,
        "```",
        "",
        "## 5. 去身份扫描",
        "",
        "- blind_id：32 位小写十六进制（secrets.token_hex，CSPRNG），全表唯一。",
        "- 文件名：仅 `<blind_id>.md` 与 `order_rater_{a,b}.csv`。",
        "- 正文：身份模式（sample/group/repeat/run_dir）0 命中；run 日期与生成时间戳 0 命中。",
    ]
    if body_warnings:
        lines.append(
            "- 非身份日期说明：以下材料的模型原文含记忆数据自带日期（非 run 日期，不用于反推批次），"
            "按 §5.4.1 的扫描目的记为告警而非阻断："
        )
        for warning in body_warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("- 非身份日期：0 处。")
    lines += [
        "",
        "## 6. 一致性自检",
        "",
        "- 每个 blind_id 恰好对应一行：通过。",
        "- phase 计数 84 / 6 / 23：通过。",
        "- 同一 (source_run_dir, source_row_number, source_result_row_sha256) 不重复：通过。",
        "- 正式 84 与校准 6 交集为空：通过。",
        "- B0 为 legacy 结果：10 份全量入包，只读复制，未补字段、未重跑；"
        "legacy 行没有 round_index，`round_index` 列留空（N/A），不是 0。",
        "",
        "## 7. 说明",
        "",
        "- `selection_audit.csv` 只含正式 84（§5.4.2 的 7 列），不含 blind_id；校准 6 与补充 23 的选择可"
        "由 mapping.csv 与本文件的算法参数复算。",
        "- 行哈希 `source_result_row_sha256` 的计算口径：行的规范 JSON（键排序、分隔符 `,`/`:`、"
        "`ensure_ascii=False`）的 UTF-8 SHA256。",
        "- 本文件不进入评审包、不进入压缩包、不提交 Git。",
        "",
    ]
    if repack_notes:
        lines += ["## 8. 重打包记录", ""]
        lines += [f"- {note}" for note in repack_notes]
        lines.append("")
    return "\n".join(lines)


def _read_csv_table(path: Path, columns: list[str], what: str) -> list[dict]:
    """按既定列读一张旧 CSV；缺失、空表、表头不符都直接拒绝。"""

    if not path.is_file():
        raise PackError(f"步骤 1/缺少{what}：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = [(name or "").strip() for name in (reader.fieldnames or [])]
        if header != columns:
            raise PackError(
                f"步骤 1/{what}表头不符：期望 {columns}，实际 {header}"
            )
        rows = [dict(row) for row in reader]
    if not rows:
        raise PackError(f"步骤 1/{what}没有任何记录：{path}")
    return rows


def _require_count(actual: int, expected: int, what: str) -> None:
    if actual != expected:
        raise PackError(f"步骤 1/{what}数量应为 {expected}，实际 {actual}")


def verify_previous_outputs(pack_dir: Path, private_dir: Path) -> dict:
    """重打包前置检查：旧私有文件与旧盲审包必须成套、结构符合协议、数量可核对。

    任何一项不满足都在创建 staging / 移动文件之前抛 PackError；这里只读，不改动旧数据。
    """

    pack_dir = Path(pack_dir)
    private_dir = Path(private_dir)
    if not pack_dir.is_dir():
        raise PackError(f"步骤 1/旧盲审包不存在，没有可归档的版本：{pack_dir}")

    for name in PRIVATE_FILES:
        path = private_dir / name
        if not path.is_file():
            raise PackError(f"步骤 1/旧私有文件缺失：{path}")
        if path.stat().st_size == 0:
            raise PackError(f"步骤 1/旧私有文件为空：{path}")

    # ---- 旧 mapping.csv：113 个唯一 blind_id，phase 计数 84/6/23 ----
    mapping_rows = _read_csv_table(
        private_dir / "mapping.csv", MAPPING_COLUMNS, "旧 mapping.csv"
    )
    _require_count(len(mapping_rows), TOTAL_BLIND_IDS, "旧 mapping.csv 行")
    phase_ids: dict[str, set[str]] = {phase: set() for phase in PHASE_COUNTS}
    seen_ids: set[str] = set()
    for line_no, row in enumerate(mapping_rows, start=2):
        blind_id = (row.get("blind_id") or "").strip()
        phase = (row.get("phase") or "").strip()
        if not BLIND_ID_RE.match(blind_id):
            raise PackError(
                f"步骤 1/旧 mapping.csv 第 {line_no} 行 blind_id 非法：{blind_id!r}"
            )
        if blind_id in seen_ids:
            raise PackError(
                f"步骤 1/旧 mapping.csv 第 {line_no} 行 blind_id 重复：{blind_id}"
            )
        seen_ids.add(blind_id)
        if phase not in phase_ids:
            raise PackError(
                f"步骤 1/旧 mapping.csv 第 {line_no} 行 phase 非法：{phase!r}"
            )
        phase_ids[phase].add(blind_id)
    for phase, expected in PHASE_COUNTS.items():
        _require_count(len(phase_ids[phase]), expected, f"旧 mapping.csv 的 {phase}")

    # ---- 旧 selection_audit.csv：只含正式 84，键唯一 ----
    audit_rows = _read_csv_table(
        private_dir / "selection_audit.csv", AUDIT_COLUMNS, "旧 selection_audit.csv"
    )
    _require_count(len(audit_rows), PHASE_COUNTS[PHASE_FORMAL], "旧 selection_audit.csv 行")
    audit_keys = {
        (
            (row.get("sample_id") or "").strip(),
            (row.get("group") or "").strip(),
            (row.get("repeat_index") or "").strip(),
        )
        for row in audit_rows
    }
    if len(audit_keys) != len(audit_rows):
        raise PackError("步骤 1/旧 selection_audit.csv 存在重复的 (sample_id, group, repeat_index)")

    # ---- 旧盲审包：三个 phase 目录、材料数、评分者顺序文件齐全且与 mapping 对齐 ----
    top_entries = sorted(entry.name for entry in pack_dir.iterdir())
    unexpected_top = sorted(set(top_entries) - set(PHASE_COUNTS))
    if unexpected_top:
        raise PackError(
            f"步骤 1/旧盲审包存在协议外条目：{unexpected_top}；"
            "无法确认它们该不该归档，拒绝继续"
        )
    packed_ids: set[str] = set()
    phase_summary: dict[str, dict] = {}
    for phase, expected in PHASE_COUNTS.items():
        phase_dir = pack_dir / phase
        if not phase_dir.is_dir():
            raise PackError(f"步骤 1/旧盲审包缺少 phase 目录：{phase_dir}")
        files = {entry.name: entry for entry in phase_dir.iterdir()}
        if any(entry.is_dir() for entry in files.values()):
            raise PackError(f"步骤 1/旧盲审包 {phase} 目录不应包含子目录：{phase_dir}")
        markdown = {name for name in files if name.endswith(".md")}
        _require_count(len(markdown), expected, f"旧盲审包 {phase} 的 .md 材料")
        unexpected = sorted(set(files) - markdown - set(ORDER_FILES))
        if unexpected:
            raise PackError(
                f"步骤 1/旧盲审包 {phase} 存在协议外文件：{unexpected}"
            )
        expected_ids = phase_ids[phase]
        if markdown != {f"{blind_id}.md" for blind_id in expected_ids}:
            missing = sorted(expected_ids - {name[: -len('.md')] for name in markdown})
            extra = sorted({name[: -len('.md')] for name in markdown} - expected_ids)[:5]
            raise PackError(
                f"步骤 1/旧盲审包 {phase} 材料与旧 mapping.csv 不一致："
                f"缺少 {missing[:5]}；多出 {extra}"
            )
        if packed_ids & expected_ids:
            raise PackError(f"步骤 1/旧盲审包 {phase} 与其它 phase 的 blind_id 有交集")
        packed_ids |= expected_ids

        for order_name in ORDER_FILES:
            order_path = files.get(order_name)
            if order_path is None or not order_path.is_file():
                raise PackError(f"步骤 1/旧盲审包 {phase} 缺少评分者顺序文件：{order_name}")
            order_rows = _read_csv_table(
                order_path, ORDER_COLUMNS, f"旧盲审包 {phase}/{order_name}"
            )
            _require_count(len(order_rows), expected, f"旧盲审包 {phase}/{order_name} 行")
            positions = []
            order_ids = []
            for line_no, row in enumerate(order_rows, start=2):
                text = (row.get("position") or "").strip()
                if not text.isdigit():
                    raise PackError(
                        f"步骤 1/旧盲审包 {phase}/{order_name} 第 {line_no} 行 position 非法：{text!r}"
                    )
                positions.append(int(text))
                order_ids.append((row.get("blind_id") or "").strip())
            if sorted(positions) != list(range(1, expected + 1)):
                raise PackError(
                    f"步骤 1/旧盲审包 {phase}/{order_name} 的 position 必须是 1..{expected}"
                )
            if len(set(order_ids)) != expected or set(order_ids) != expected_ids:
                raise PackError(
                    f"步骤 1/旧盲审包 {phase}/{order_name} 的 blind_id 与本 phase 材料不一一对应"
                )

        phase_summary[phase] = {"materials": len(markdown), "orders": len(ORDER_FILES)}

    if packed_ids != seen_ids:
        raise PackError("步骤 1/旧盲审包材料集合与旧 mapping.csv 的 blind_id 集合不一致")

    return {
        "mapping_rows": len(mapping_rows),
        "selection_audit_rows": len(audit_rows),
        "blind_ids": len(seen_ids),
        "phases": phase_summary,
        "expected_files": len(PRIVATE_FILES) + TOTAL_BLIND_IDS + len(ORDER_FILES) * len(PHASE_COUNTS),
    }


def _archive_manifest_entries(path: Path) -> list[tuple[str, str]]:
    """读回 ARCHIVE-MANIFEST.md 的表格，返回 (相对路径, SHA256) 列表。"""

    if not path.is_file():
        raise PackError(f"缺少归档清单：{path}")
    entries: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text.startswith("|"):
            continue
        if set(text) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if len(cells) != 2 or cells[0] == "归档路径":
            continue
        relative, digest = cells[0], cells[1].lower()
        if not relative or not SHA256_HEX.match(digest):
            raise PackError(f"归档清单记录非法：{line!r}")
        entries.append((relative, digest))
    return entries


def _actual_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in root.rglob("*")
        if path.is_file()
    }


def _verify_entries_against(root: Path, entries: list[tuple[str, str]], step: str) -> None:
    """核对目录里实际的文件集合与每个文件的 SHA256 是否与清单条目一致。"""

    recorded: dict[str, str] = {}
    for relative, digest in entries:
        if relative in recorded:
            raise PackError(f"{step}/归档清单存在重复路径：{relative}")
        recorded[relative] = digest
    actual = _actual_files(root)
    actual.pop(ARCHIVE_MANIFEST_NAME, None)
    missing = sorted(set(recorded) - set(actual))
    extra = sorted(set(actual) - set(recorded))
    if missing or extra:
        raise PackError(f"{step}/归档文件与清单不一致：缺少 {missing[:5]}；多出 {extra[:5]}")
    bad = sorted(relative for relative, digest in recorded.items() if actual[relative] != digest)
    if bad:
        raise PackError(f"{step}/归档文件 SHA256 与清单不一致：{bad[:5]}")


def _verify_archive_tree(root: Path, step: str = "归档复核") -> int:
    """核对归档目录：清单条数、路径集合与每个文件的 SHA256 都必须与磁盘一致。"""

    entries = _archive_manifest_entries(root / ARCHIVE_MANIFEST_NAME)
    if not entries:
        raise PackError(f"{step}/归档清单没有任何条目：{root / ARCHIVE_MANIFEST_NAME}")
    _verify_entries_against(root, entries, step)
    return len(entries)


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_like(path: Path) -> bool:
    """路径本身是否是符号链接 / junction（含悬空链接）。不跟随最后一级。

    Windows 上目录 junction 的 `lstat().st_mode` 不一定带 S_ISLNK，必须同时看
    `st_file_attributes` 的 reparse point 位；只看 is_symlink() 会漏掉 junction，
    归档目录就能被指到 Git 忽略范围之外，私有材料（mapping.csv 等）随之写出保护目录。
    悬空链接也必须算：`lstat` 成功而 `exists()` 为 False，复制时会把材料写到它指向的位置。
    """

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PackError(f"无法确认路径类型（拒绝继续）：{path}（{exc}）") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0) or 0
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _verified_dir_inside(path: Path, parent_real: Path, label: str) -> Path:
    """要求 path 是已存在、非链接、真实路径落在 parent_real 内的目录，返回真实路径。"""

    if _is_link_like(path):
        raise PackError(
            f"{label} {path} 是符号链接/junction（或悬空链接），拒绝使用："
            "真实写入位置可能落到 Git 忽略的私有目录之外；本次未写入或删除任何文件"
        )
    try:
        real = path.resolve(strict=True)
    except OSError as exc:
        raise PackError(f"{label} 无法解析真实路径：{path}（{exc}）") from exc
    if not real.is_dir():
        raise PackError(f"{label} 不是目录：{real}")
    if real == parent_real:
        raise PackError(f"{label} 的真实路径与父目录相同，拒绝使用：{real}")
    try:
        real.relative_to(parent_real)
    except ValueError as exc:
        raise PackError(
            f"{label} 的真实路径 {real} 不在 {parent_real} 内，拒绝写入"
            "（私有材料只能落在盲评私有目录里）"
        ) from exc
    return real


def ensure_archive_root(private_dir: Path) -> tuple[Path, Path]:
    """在读写任何旧私有文件之前，确认归档目录真实落在私有目录内。

    返回 (私有目录真实路径, 归档目录真实路径)。归档目录不存在就建一个普通目录，
    但建之前也要先看它是不是悬空链接——否则 `mkdir(exist_ok=True)` 会顺着链接建到别处。
    """

    private_dir = Path(private_dir)
    if _is_link_like(private_dir):
        raise PackError(
            f"私有目录本身是符号链接/junction（或悬空链接），拒绝归档：{private_dir}"
        )
    try:
        private_real = private_dir.resolve(strict=True)
    except OSError as exc:
        raise PackError(f"私有目录无法解析真实路径：{private_dir}（{exc}）") from exc
    if not private_real.is_dir():
        raise PackError(f"私有目录不是目录：{private_real}")
    archive_root = private_dir / ARCHIVE_DIRNAME
    if _is_link_like(archive_root):
        raise PackError(
            f"归档目录 {archive_root} 是符号链接/junction（或悬空链接），拒绝归档："
            "私有材料可能被写到 Git 忽略的私有目录之外；本次未写入或删除任何文件"
        )
    if not archive_root.exists():
        try:
            archive_root.mkdir()
        except OSError as exc:
            raise PackError(f"创建归档目录失败：{archive_root}（{exc}）") from exc
    archive_real = _verified_dir_inside(archive_root, private_real, "归档目录")
    return private_real, archive_real


def _sane_child_path(path: Path, parent_real: Path, label: str) -> Path:
    """归档目标 / staging 必须是已校验目录下的普通直接子路径，不能是链接。"""

    path = Path(path)
    if _is_link_like(path):
        raise PackError(
            f"{label} {path} 是符号链接/junction（或悬空链接），拒绝写入："
            "本次未写入或删除任何文件"
        )
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise PackError(f"{label} 无法解析真实路径：{path}（{exc}）") from exc
    if resolved.parent != parent_real:
        raise PackError(
            f"{label} 的真实路径 {resolved} 不在已校验的归档目录 {parent_real} 下，拒绝写入"
        )
    return resolved


def _require_sane_pack_dir(pack_dir: Path) -> None:
    """活动包目录不能是普通文件，也不能是（悬空的）符号链接。

    以前只在 `pack_dir.is_dir()` 为真时才枚举旧产物：把它换成一个普通文件或悬空链接，
    旧产物检查与「必须成套替换」都会被跳过，新文件却照样写进去。这里要在写入任何
    私有文件、删除任何目录之前就拒绝。
    """

    pack_dir = Path(pack_dir)
    if _is_link_like(pack_dir):
        raise PackError(
            f"活动包目录 {pack_dir} 是符号链接/junction（或悬空链接），拒绝写入："
            "本次未写入或删除任何文件；请人工确认后改用普通目录或不存在的路径重跑"
        )
    if pack_dir.exists() and not pack_dir.is_dir():
        raise PackError(
            f"活动包目录 {pack_dir} 已存在但不是目录（普通文件等），拒绝写入："
            "本次未写入或删除任何文件；请人工确认或换 --pack-dir"
        )


def archive_previous_outputs(
    pack_dir: Path, private_dir: Path, generated_utc: str
) -> tuple[str, str, int]:
    """重打包前把旧的一整套产物归档：旧 blind_pack/ + mapping + selection_audit + MAPPING-AUDIT。

    旧材料（含旧 blind_id）必须成套留档，之后再清空活动包目录，避免新旧 blind_id 混在
    同一个可分发目录里。归档目录带逐文件 SHA256 清单，不能静默覆盖。
    旧产物先整体校验，再复制到 staging 并逐文件重算 SHA256，清单复核通过才发布；
    只有确认归档完整之后才清理旧活动包。
    """

    pack_dir = Path(pack_dir)
    private_dir = Path(private_dir)
    # 步骤 0：真实路径校验放在最前面——任何读取/复制旧私有文件之前就要确认活动包目录
    # 与归档目录不是符号链接/junction，且归档目录真实落点仍在私有目录内。
    _require_sane_pack_dir(pack_dir)
    private_real, archive_real = ensure_archive_root(private_dir)
    # 步骤 1：先校验旧产物完整性；这里只读，任何失败都不会移动或删除任何旧文件
    evidence = verify_previous_outputs(pack_dir, private_dir)
    old_mapping = private_dir / "mapping.csv"

    stamp = generated_utc.replace(":", "").replace("-", "")
    target = _sane_child_path(archive_real / f"pack-{stamp}", archive_real, "归档目标")
    if target.exists() or target.is_symlink():
        raise PackError(f"步骤 2/归档目标已存在，拒绝覆盖：{target}")
    # 先写到 staging，全部校验通过后再原子改名为正式的 pack-<stamp>/：
    # 复制中途失败只会留下可清理的 staging，不会出现“半个归档”占住归档名。
    staging = _sane_child_path(
        archive_real / f".pack-{stamp}.staging", archive_real, "归档 staging"
    )
    if staging.is_symlink():
        raise PackError(f"步骤 2/{staging} 是符号链接，拒绝写入，请人工确认后清理")
    if staging.exists():
        if not staging.is_dir():
            raise PackError(f"步骤 2/归档 staging {staging} 已存在且不是目录，请人工确认后清理")
        shutil.rmtree(staging)
    staging.mkdir()
    staging_real = staging.resolve(strict=True)
    if staging_real.parent != archive_real:
        raise PackError(
            f"步骤 2/staging {staging_real} 的真实父目录不是已校验的归档目录 "
            f"{archive_real}，拒绝写入"
        )
    old_mapping_sha = sha256_file(old_mapping)

    entries: list[tuple[str, str]] = []
    try:
        # 步骤 3：复制旧私有文件与旧盲审包（旧文件保持原样，不移动、不删除）
        # 清单里的哈希必须先算源文件：拿复制后的文件自己算哈希，复制坏了一样对得上，
        # 步骤 4 就成了自证。先记源哈希，再由步骤 4 拿 staging 逐条比对。
        for name in PRIVATE_FILES:
            source = private_dir / name
            destination = staging / "private" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            entries.append((f"private/{name}", sha256_file(source)))
            shutil.copy2(source, destination)

        for path in sorted(pack_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(pack_dir).as_posix()
            destination = staging / "blind_pack" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            entries.append((f"blind_pack/{relative}", sha256_file(path)))
            shutil.copy2(path, destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise PackError(
            f"步骤 3/复制旧产物到 staging 失败，已放弃本次归档并保留旧私有文件与活动包原状：{exc}"
        ) from exc

    # 步骤 4：复制后的文件必须逐条重算 SHA256，并与应有数量一致
    if len(entries) != evidence["expected_files"]:
        shutil.rmtree(staging, ignore_errors=True)
        raise PackError(
            f"步骤 4/staging 文件数 {len(entries)} 与校验得到的应有数量 "
            f"{evidence['expected_files']} 不一致，拒绝发布归档"
        )
    try:
        _verify_entries_against(staging, entries, "步骤 4")
    except PackError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    phase_counts = " / ".join(
        f"{phase}={evidence['phases'][phase]['materials']}" for phase in PHASE_COUNTS
    )
    lines = [
        "# ARCHIVE-MANIFEST（私有留档，禁止分发 / 禁止提交 Git）",
        "",
        f"- 归档时间 (UTC)：{generated_utc}",
        f"- 生成器版本：{VERSION}",
        f"- 被替换旧 mapping.csv SHA256：{old_mapping_sha}",
        f"- 旧包阶段计数：{phase_counts}",
        f"- 归档文件数：{len(entries)}",
        "",
        "| 归档路径 | SHA256 |",
        "| --- | --- |",
    ]
    lines += [f"| {relative} | {digest} |" for relative, digest in entries]
    (staging / ARCHIVE_MANIFEST_NAME).write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )

    # 步骤 5：清单落盘后再读回，条目数与 SHA256 全部复核一致才发布归档
    try:
        archived_count = _verify_archive_tree(staging, "步骤 5")
    except PackError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if archived_count != len(entries):
        shutil.rmtree(staging, ignore_errors=True)
        raise PackError(
            f"步骤 5/归档清单条目数 {len(entries)} 与读回核对数 {archived_count} 不一致，拒绝发布归档"
        )
    try:
        os.replace(staging, target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise PackError(
            f"步骤 6/发布归档目录失败，活动包与私有文件保持原状：{exc}"
        ) from exc

    # 步骤 6：发布后再核对一次归档目录，确认完整才允许清理旧活动包
    try:
        published = _verify_archive_tree(target, "步骤 6/发布后复核")
    except PackError as exc:
        raise PackError(
            f"{exc}；归档目录 {target.relative_to(private_real)} 已保留，"
            "活动包未被清理，请人工核对"
        ) from exc
    if published != len(entries):
        raise PackError(
            f"步骤 6/发布后归档文件数 {published} 与清单 {len(entries)} 不一致；"
            f"归档目录 {target.relative_to(private_real)} 已保留，活动包未被清理"
        )

    try:
        shutil.rmtree(pack_dir)
    except OSError as exc:
        raise PackError(
            f"步骤 7/旧产物已完整归档到 {target.relative_to(private_real)}，但清空活动包目录失败：{exc}；"
            "请不要删除该归档，手动清理活动包后重试"
        ) from exc
    return str(target.relative_to(private_real)), old_mapping_sha, len(entries)


def stale_output_paths(pack_dir: Path, private_dir: Path) -> list[Path]:
    """列出仍留在活动位置上的旧运行产物（历史归档目录不算）。

    三个私有文件任一存在，或 `pack_dir` 里还有任何条目，都说明上一次运行留下了一整套
    可分发材料；`private_dir/archive/` 是 `--repack` 的正常留档结果，不能算作活动旧产物，
    否则合法的历史归档会让后续打包永远无法开始。
    """

    pack_dir = Path(pack_dir)
    private_dir = Path(private_dir)
    # 先判类型：普通文件/悬空链接不是「空目录」，不能当成没有旧产物直接放行。
    _require_sane_pack_dir(pack_dir)
    found: list[Path] = []
    for name in PRIVATE_FILES:
        path = private_dir / name
        if path.exists() or path.is_symlink():
            found.append(path)
    if pack_dir.is_dir():
        found.extend(sorted(pack_dir.iterdir()))
    return found


def ensure_no_stale_outputs(pack_dir: Path, private_dir: Path, repack: bool) -> list[Path]:
    """无 `--repack` 时，任何旧产物都必须在写入之前拒绝。

    早先只检查 `mapping.csv`：mapping 被删、旧包与旧审计文件还在时，脚本会先覆盖私有文件，
    直到发布阶段才因为活动包目录非空而失败——旧包配上新 mapping，正是最坏的混合状态。
    """

    stale = stale_output_paths(pack_dir, private_dir)
    if not stale or repack:
        return stale
    shown = "、".join(str(path) for path in stale[:10])
    more = f"（另有 {len(stale) - 10} 项）" if len(stale) > 10 else ""
    raise PackError(
        f"发现未归档的旧运行产物：{shown}{more}。拒绝覆盖，本次未写入或删除任何文件："
        "重打包必须显式 --repack（旧产物会先完整校验再成套归档），或先人工清理"
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _resolve_workdir(raw: Path, label: str) -> Path:
    """先判类型、再解析真实路径。

    顺序不能反：`Path.resolve()` 会把悬空的符号链接/junction 直接解析成它指向的（不存在的）
    目标，链接身份随之消失，后续的 `lstat` 检查就再也看不到它是链接了——那样「先建个悬空
    链接占住目录名」就能绕过全部写入保护。
    """

    if _is_link_like(raw):
        raise PackError(
            f"{label} {raw} 是符号链接/junction（或悬空链接），拒绝使用："
            "本次未写入或删除任何文件；请人工确认后改用普通目录或不存在的路径重跑"
        )
    return raw.resolve()


class DryRunWriteBlocked(PackError):
    """dry-run 期间出现了写入/删除/移动动作（内部错误，必须显式失败而不是静默生效）。"""


@contextlib.contextmanager
def dry_run_guard():
    """dry-run 的硬保护：任何写入、删除、移动、改名都在落盘那一刻被拦下。

    只靠「提前 return」不足以保证零副作用——只要将来有人在 `if args.dry_run:` 之前插入
    一个 mkdir / shutil.rmtree / os.replace / open("w")，dry-run 就又会静默改磁盘。
    这里把最底层的写入入口替换掉，越权即抛 DryRunWriteBlocked，让缺陷在测试里立刻暴露。
    """

    saved = {
        "os_replace": os.replace,
        "os_remove": os.remove,
        "os_unlink": os.unlink,
        "os_rmdir": os.rmdir,
        "os_mkdir": os.mkdir,
        "os_rename": os.rename,
        "path_mkdir": Path.mkdir,
        "path_unlink": Path.unlink,
        "path_rmdir": Path.rmdir,
        "path_write_text": Path.write_text,
        "path_write_bytes": Path.write_bytes,
        "path_touch": Path.touch,
        "shutil_rmtree": shutil.rmtree,
        "shutil_copy2": shutil.copy2,
        "shutil_move": shutil.move,
        "builtin_open": builtins.open,
        "io_open": io.open,
    }

    def _blocked(what):
        def _raise(*_args, **_kwargs):
            raise DryRunWriteBlocked(
                f"dry-run 期间发生写盘动作（{what}）：dry-run 必须零副作用，"
                "本次已中止，未修改任何文件"
            )

        return _raise

    def _guard_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            raise DryRunWriteBlocked(
                f"dry-run 期间以写模式打开文件（open mode={mode!r}, path={file!r}）："
                "dry-run 必须零副作用，本次已中止，未修改任何文件"
            )
        return saved["builtin_open"](file, mode, *args, **kwargs)

    os.replace = _blocked("os.replace")
    os.remove = _blocked("os.remove")
    os.unlink = _blocked("os.unlink")
    os.rmdir = _blocked("os.rmdir")
    os.mkdir = _blocked("os.mkdir")
    os.rename = _blocked("os.rename")
    Path.mkdir = _blocked("Path.mkdir")
    Path.unlink = _blocked("Path.unlink")
    Path.rmdir = _blocked("Path.rmdir")
    Path.write_text = _blocked("Path.write_text")
    Path.write_bytes = _blocked("Path.write_bytes")
    Path.touch = _blocked("Path.touch")
    shutil.rmtree = _blocked("shutil.rmtree")
    shutil.copy2 = _blocked("shutil.copy2")
    shutil.move = _blocked("shutil.move")
    builtins.open = _guard_open
    io.open = _guard_open
    try:
        yield
    finally:
        os.replace = saved["os_replace"]
        os.remove = saved["os_remove"]
        os.unlink = saved["os_unlink"]
        os.rmdir = saved["os_rmdir"]
        os.mkdir = saved["os_mkdir"]
        os.rename = saved["os_rename"]
        Path.mkdir = saved["path_mkdir"]
        Path.unlink = saved["path_unlink"]
        Path.rmdir = saved["path_rmdir"]
        Path.write_text = saved["path_write_text"]
        Path.write_bytes = saved["path_write_bytes"]
        Path.touch = saved["path_touch"]
        shutil.rmtree = saved["shutil_rmtree"]
        shutil.copy2 = saved["shutil_copy2"]
        shutil.move = saved["shutil_move"]
        builtins.open = saved["builtin_open"]
        io.open = saved["io_open"]


def run_pack(args) -> int:
    # 冲突组合必须在任何数据读取、目录创建、归档、删除之前就拒绝。
    # 历史缺陷：归档调用（archive_previous_outputs）排在 `if args.dry_run:` 之前，
    # 而归档的最后一步是 shutil.rmtree(活动包目录)，于是 `--dry-run --repack`
    # 会把旧包删掉——dry-run 的承诺是「不写文件」，这条组合根本不该被接受。
    if args.dry_run and args.repack:
        raise PackError(
            "拒绝执行：--dry-run 与 --repack 不能同时使用。--dry-run 的承诺是只抽样与校验、"
            "不写不删任何文件；--repack 会先归档再删除活动包目录。请二选一：只验抽样用 "
            "--dry-run，真正重打包去掉 --dry-run。本次未写入或删除任何文件"
        )
    if args.dry_run:
        with dry_run_guard():
            return _run_pack(args)
    return _run_pack(args)


def _run_pack(args) -> int:
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    private_dir = _resolve_workdir(
        Path(args.private_dir) if args.private_dir else DEFAULT_PRIVATE_DIR, "私有目录"
    )
    pack_dir = _resolve_workdir(
        Path(args.pack_dir) if args.pack_dir else DEFAULT_PACK_DIR, "活动包目录"
    )

    config = load_blind_config(Path(args.config) if args.config else CONFIG_PATH)
    config_sha = sha256_file(Path(args.config) if args.config else CONFIG_PATH)
    seed = int(config["blind_sampling_seed"])
    algorithm_version = str(config["algorithm_version"])

    # 打包前：从磁盘重算全部源文件哈希/大小/条数，并与 manifest 核对（B0 允许 legacy 缺条目）
    snapshot_before = snapshot_sources(runs_dir=runs_dir)
    manifest = manifest_hashes()
    source_report = verify_snapshot(snapshot_before, manifest)

    ensure_path_layout(pack_dir, private_dir)
    # 包目录与实验 run 目录必须互不包含：否则 --repack 归档旧包时会把正式 results.jsonl
    # 一起搬走，或把可分发材料写进被 .gitignore 的 runs/ 里而无人发现。放在任何写盘之前判。
    ensure_not_within_runs(pack_dir, runs_dir, "活动包目录")
    ensure_not_within_runs(private_dir, runs_dir, "私有目录")
    gate_evidence = ensure_private_gate(private_dir)
    # 统一前置检查：三个私有文件与活动包目录都必须在写入任何文件之前判掉，
    # 不能只查 mapping.csv（mapping 被删而旧包还在时，旧包会被新私有文件配上）。
    stale_before = ensure_no_stale_outputs(pack_dir, private_dir, bool(args.repack))

    built = build_items(seed, runs_dir)
    items = built["items"]
    assign_ids(items)

    # 材料正文：先渲染 + 扫描，全部通过才落盘（避免半成品包）
    packets = {}
    warnings = []
    for item in items:
        context = load_sample_context(item["row"].sample_id)
        text = render_packet(item, context)
        assert_identity_free(f"文件名 {item['blind_id']}.md", item["blind_id"] + ".md", IDENTITY_PATTERNS + STRICT_DATE_PATTERNS)
        hits = scan_packet_body(item, text)
        if hits:
            raise PackError(
                f"去身份扫描失败：材料 {item['blind_id']} 命中 "
                + "，".join(f"{name}={value!r}" for name, value in hits[:5])
            )
        benign = scan_text(text, STRICT_DATE_PATTERNS)
        if benign:
            warnings.append(f"{item['blind_id']}：{len(benign)} 处数据日期（{benign[0][1]} 等）")
        packets[item["blind_id"]] = (item, text)

    generated_utc = utc_now()
    repack_notes = []
    # 重打包时只要磁盘上还留着任何旧产物（旧私有文件或旧包，不含 archive/ 历史归档），
    # 就必须先过归档完整性校验。不能以 mapping.csv 是否存在为前提：mapping 被删而旧包还在，
    # 正是必须拒绝（而不是静默覆盖）的情况。
    if args.repack and stale_before:
        archive_rel, old_sha, archived_count = archive_previous_outputs(
            pack_dir, private_dir, generated_utc
        )
        repack_notes.append(
            f"{generated_utc} 重打包：被替换旧映射 SHA256={old_sha}，"
            f"旧包 + 旧私有文件共 {archived_count} 个文件成套归档为 {archive_rel}"
        )

    mapping_rows = []
    for item in items:
        row = item["row"]
        mapping_rows.append(
            {
                "blind_id": item["blind_id"],
                "phase": item["phase"],
                "batch": item["batch"],
                "source_run_dir": row.run_dir,
                "source_file": SOURCE_FILE,
                "source_row_number": row.line_no,
                "experiment_id": row.experiment_id,
                "sample_id": row.sample_id,
                "group": row.group,
                "condition": row.condition,
                "repeat_index": row.repeat_index,
                "round_index": row.round_index,
                "source_prompt_sha256": row.prompt_sha256,
                "source_result_row_sha256": row.row_sha256,
                "source_results_file_sha256": row.results_file_sha256,
                "generated_utc": generated_utc,
                "generator_version": VERSION,
            }
        )

    audit_rows = [
        {
            "sample_id": pick.row.sample_id,
            "group": pick.row.group,
            "repeat_index": pick.row.repeat_index,
            "plan_seq": pick.row.plan_seq,
            "source_prompt_sha256": pick.row.prompt_sha256,
            "source_result_row_sha256": pick.row.row_sha256,
            "selection_hash": pick.hash,
        }
        for pick in built["formal"]
    ]
    audit_rows.sort(key=lambda row: (row["sample_id"], row["group"], row["repeat_index"]))

    _self_check(mapping_rows, built)

    if args.dry_run:
        print(f"[dry-run] formal={built['counts']['formal']} calibration={built['counts']['calibration']} "
              f"supplementary={built['counts']['supplementary']}")
        print(f"[dry-run] config_sha256={config_sha} seed={seed} algorithm={algorithm_version}")
        for row in source_report:
            print(f"[dry-run] source {row['run_dir']} {row['status']} {row['actual']}")
        for line in gate_evidence:
            print(f"[dry-run] {line}")
        print(f"[dry-run] 扫描通过；非身份日期告警 {len(warnings)} 处；未写任何文件")
        return 0

    # ---- 打包后复核（在写任何文件之前）：重新从磁盘计算全部源文件哈希，逐 run 与打包前比对 ----
    snapshot_after = snapshot_sources(runs_dir=runs_dir)
    verify_sources_unchanged(snapshot_before, snapshot_after)

    # ---- 落盘：先把全部内容在内存里生成，pack 写进 staging 目录，最后才一次性发布 ----
    phase_plans = []
    for phase in (PHASE_FORMAL, PHASE_CALIBRATION, PHASE_SUPPLEMENTARY):
        phase_items = [item for item in items if item["phase"] == phase]
        order_a, order_b = build_rater_orders(phase_items)
        phase_plans.append((phase, phase_items, order_a, order_b))

    mapping_text = render_csv_text(MAPPING_COLUMNS, mapping_rows)
    mapping_sha = sha256_text(mapping_text)
    mapping_bytes = len(mapping_text.encode("utf-8"))
    selection_audit_text = render_csv_text(AUDIT_COLUMNS, audit_rows)
    audit_text = render_audit(
        generated_utc=generated_utc,
        config_sha=config_sha,
        seed=seed,
        algorithm_version=algorithm_version,
        counts=built["counts"],
        source_report=source_report,
        gate_evidence=gate_evidence,
        mapping_sha=mapping_sha,
        mapping_bytes=mapping_bytes,
        mapping_rows=len(mapping_rows),
        body_warnings=warnings,
        pack_root=pack_dir,
        private_dir=private_dir,
        repack_notes=repack_notes,
    )

    stamp = generated_utc.replace(":", "").replace("-", "")
    staging_dir = pack_dir.parent / f".{pack_dir.name}.staging-{stamp}"
    if _is_link_like(staging_dir):
        raise PackError(f"发布暂存路径是符号链接/联接，拒绝在此落地：{staging_dir}")
    if staging_dir.exists():
        if not staging_dir.is_dir():
            # 普通文件（或残留的错误产物）不能用 rmtree，直接报结构化错误而不是 traceback
            raise PackError(f"发布暂存路径已存在且不是目录：{staging_dir}")
        shutil.rmtree(staging_dir)
    private_written = False
    try:
        for phase, phase_items, order_a, order_b in phase_plans:
            phase_dir = staging_dir / phase
            phase_dir.mkdir(parents=True, exist_ok=True)
            for item in phase_items:
                _, text = packets[item["blind_id"]]
                write_text_atomic(phase_dir / f"{item['blind_id']}.md", text)
            for rater_id, order in (("rater_a", order_a), ("rater_b", order_b)):
                write_csv_atomic(
                    phase_dir / f"order_{rater_id}.csv",
                    ["position", "blind_id"],
                    [{"position": pos, "blind_id": bid} for pos, bid in order],
                )

        # 私有文件先落盘（评分材料离开 mapping 就无法回溯，绝不能先发布材料）
        private_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(private_dir / "selection_audit.csv", selection_audit_text)
        write_text_atomic(private_dir / "mapping.csv", mapping_text)
        write_text_atomic(private_dir / "MAPPING-AUDIT.md", audit_text)
        private_written = True

        if pack_dir.exists():
            if any(pack_dir.iterdir()):
                raise PackError(f"活动包目录非空且未归档：{pack_dir}；拒绝把新包与旧包混在一起")
            pack_dir.rmdir()
        os.replace(staging_dir, pack_dir)
    except (OSError, PackError) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        detail = (
            f"私有文件已写入 {private_dir}（mapping.csv SHA256={mapping_sha}），"
            "下次重打包必须显式 --repack 才会归档并替换"
            if private_written
            else "未写入任何私有文件"
        )
        raise PackError(f"写入盲评包失败：{exc}；{detail}") from exc

    print("blind pack 生成完成（无评分、无模型调用、未修改 runs/ 与 feedback.db）")
    print(f"  pack_dir      = {pack_dir}")
    print(f"  private_dir   = {private_dir}")
    print(f"  formal        = {built['counts']['formal']}")
    print(f"  calibration   = {built['counts']['calibration']}")
    print(f"  supplementary = {built['counts']['supplementary']}")
    print(f"  seed          = {seed}  algorithm_version = {algorithm_version}")
    print(f"  config_sha256 = {config_sha}")
    for row in source_report:
        print(f"  source {row['run_dir']}: {row['status']} {row['actual']}")
    for line in gate_evidence:
        print(f"  {line}")
    print(f"  mapping.csv sha256 = {mapping_sha} bytes={mapping_bytes} rows={len(mapping_rows)}")
    print(f"  非身份日期告警 = {len(warnings)} 处")
    return 0


def _self_check(mapping_rows: list[dict], built: dict) -> None:
    ids = [row["blind_id"] for row in mapping_rows]
    if len(ids) != len(set(ids)):
        raise PackError("blind_id 不唯一")
    counts = {}
    for row in mapping_rows:
        counts[row["phase"]] = counts.get(row["phase"], 0) + 1
    if counts != {"formal": 84, "calibration": 6, "supplementary": 23}:
        raise PackError(f"phase 计数不符：{counts}")
    identities = [
        (row["source_run_dir"], row["source_row_number"], row["source_result_row_sha256"])
        for row in mapping_rows
    ]
    if len(identities) != len(set(identities)):
        raise PackError("同一源记录对应了多个 blind_id")
    formal = {
        (row["source_run_dir"], row["source_row_number"], row["source_result_row_sha256"])
        for row in mapping_rows
        if row["phase"] == "formal"
    }
    calibration = {
        (row["source_run_dir"], row["source_row_number"], row["source_result_row_sha256"])
        for row in mapping_rows
        if row["phase"] == "calibration"
    }
    if formal & calibration:
        raise PackError("正式 84 与校准 6 存在交集")
    for row in mapping_rows:
        if not re.fullmatch(r"[0-9a-f]{32}", row["blind_id"]):
            raise PackError(f"blind_id 形式非法：{row['blind_id']!r}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成毕业设计盲评包（v4.5 §5.4）")
    parser.add_argument("--config", default=None, help="blind_config.json 路径")
    parser.add_argument("--runs-dir", default=None, help="runs/ 目录（默认 experiments/graduation/runs）")
    parser.add_argument("--pack-dir", default=None, help="盲评包输出目录")
    parser.add_argument("--private-dir", default=None, help="私有映射目录")
    parser.add_argument("--repack", action="store_true",
                        help="允许覆盖 mapping.csv（会先把旧产物成套归档再替换；不能与 --dry-run 同用）")
    parser.add_argument("--dry-run", action="store_true", help="只做抽样与校验，不写文件")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run_pack(args)
    except PackError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
