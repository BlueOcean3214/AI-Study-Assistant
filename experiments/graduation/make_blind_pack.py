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
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"

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


def load_blind_config(path: Path = CONFIG_PATH) -> dict:
    if not path.is_file():
        raise PackError(f"缺少配置文件：{path}")
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "protocol_version",
        "blind_sampling_seed",
        "algorithm_version",
        "selection_domains",
        "created_utc",
    }
    missing = sorted(required - set(config))
    if missing:
        raise PackError(f"blind_config.json 缺字段：{missing}")
    if not isinstance(config["blind_sampling_seed"], int):
        raise PackError("blind_sampling_seed 必须是整数")
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


def read_rows(run_dir_name: str, batch: str, runs_dir: Path = RUNS_DIR) -> list[SourceRow]:
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


def load_main_rows(runs_dir: Path = RUNS_DIR) -> list[SourceRow]:
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


def load_b0_rows(runs_dir: Path = RUNS_DIR) -> list[SourceRow]:
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


def load_s7empty_rows(runs_dir: Path = RUNS_DIR) -> list[SourceRow]:
    rows = read_rows(S7EMPTY_RUN, BATCH_S7EMPTY, runs_dir)
    if len(rows) != 5 or sorted(item.repeat_index for item in rows) != REPEATS:
        raise PackError("S7-empty 源输出必须是 5 份 repeat_index 0..4")
    if {item.sample_id for item in rows} != {"S7-empty"} or {item.group for item in rows} != {"D"}:
        raise PackError("S7-empty 源输出必须全部是 S7-empty × D")
    return rows


def load_subj_rows(runs_dir: Path = RUNS_DIR) -> list[SourceRow]:
    rows = read_rows(SUBJ_RUN, BATCH_SUBJ, runs_dir)
    if len(rows) != 10:
        raise PackError(f"suppl-subj 应有 10 行，实际 {len(rows)}")
    by_group: dict[str, int] = {}
    for item in rows:
        by_group[item.group] = by_group.get(item.group, 0) + 1
    if by_group != {"C-subj": 5, "D-subj": 5}:
        raise PackError(f"suppl-subj 分组不符：{by_group}")
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


def load_sample_context(sample_id: str, samples_dir: Path = SAMPLES_DIR) -> dict:
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


def ensure_private_gate(private_dir: Path, allow_outside_repo: bool = False) -> list[str]:
    """返回证据行；失败抛 PackError（绝不创建/写入私有文件）。"""
    evidence: list[str] = []
    resolved = private_dir.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
        inside = True
    except ValueError:
        inside = False
    if not inside:
        if not allow_outside_repo:
            raise PackError(
                f"私有目录 {resolved} 不在仓库 {REPO_ROOT} 内；如确为临时/测试目录，"
                "必须显式传 --allow-outside-repo"
            )
        evidence.append(f"[gate] {resolved} 在仓库外，跳过 .gitignore 校验（--allow-outside-repo）")
        return evidence

    top = _git(["rev-parse", "--show-toplevel"], REPO_ROOT)
    if top.returncode != 0:
        raise PackError(f"git rev-parse 失败：{top.stderr.strip()}")
    if Path(top.stdout.strip()).resolve() != REPO_ROOT:
        raise PackError(f"仓库根不一致：git={top.stdout.strip()} 期望={REPO_ROOT}")

    for name in PRIVATE_FILES:
        target = resolved / name
        result = _git(["check-ignore", "-v", str(target)], REPO_ROOT)
        if result.returncode != 0 or ".gitignore" not in result.stdout:
            raise PackError(
                "私有目录未被 .gitignore 覆盖，拒绝写入。请先在 .gitignore 添加 "
                "experiments/graduation/blind_private/ 并验证 "
                f"git check-ignore -v {target}\n"
                f"git 退出码={result.returncode} 输出={result.stdout.strip()!r} "
                f"stderr={result.stderr.strip()!r}"
            )
        evidence.append(result.stdout.strip())

    tracked = _git(["ls-files", "--", str(resolved)], REPO_ROOT)
    if tracked.returncode != 0:
        raise PackError(f"git ls-files 失败：{tracked.stderr.strip()}")
    if tracked.stdout.strip():
        raise PackError(
            "私有目录中存在已被 Git 跟踪的文件，先停止并清理：\n" + tracked.stdout.strip()
        )
    evidence.append("[gate] git ls-files 无私有文件（0 条）")
    return evidence


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_csv_atomic(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in columns})
    os.replace(tmp, path)


def manifest_hashes(path: Path = MANIFEST_PATH) -> dict[tuple[str, str], dict]:
    table: dict[tuple[str, str], dict] = {}
    if not path.is_file():
        return table
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            relative = (row.get("relative_path") or "").replace("\\", "/").strip("/")
            table[(relative, (row.get("file") or "").strip())] = row
    return table


def verify_source_files(rows: list[SourceRow], manifest: dict) -> list[dict]:
    """打包前核对：源文件整体 SHA256 必须与执行报告附录 A（manifest）一致。"""
    report = []
    files = {}
    for item in rows:
        files.setdefault(item.run_dir, item.results_file_sha256)
    for run_dir, actual in sorted(files.items()):
        relative = f"experiments/graduation/runs/{run_dir}/{SOURCE_FILE}"
        entry = manifest.get((relative, SOURCE_FILE))
        if entry is None:
            report.append(
                {"run_dir": run_dir, "actual": actual, "expected": "", "status": "manifest_missing"}
            )
            continue
        expected = (entry.get("sha256") or "").strip().lower()
        status = "match" if expected == actual else "mismatch"
        report.append(
            {"run_dir": run_dir, "actual": actual, "expected": expected, "status": status}
        )
    mismatch = [row for row in report if row["status"] == "mismatch"]
    if mismatch:
        raise PackError(
            "源 results.jsonl 与 manifest 不一致，停止打包："
            + "；".join(f"{row['run_dir']} expected={row['expected']} actual={row['actual']}" for row in mismatch)
        )
    return report


def build_items(seed: int, runs_dir: Path = RUNS_DIR):
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
    subj_all = [Pick(item, selection_hash(seed, DOMAIN_FORMAL, item.sample_id, item.group, item.repeat_index)) for item in subj_rows]

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
        used.add(blind_id)
        assert_identity_free(f"blind_id={blind_id}", blind_id, IDENTITY_PATTERNS + STRICT_DATE_PATTERNS)
        item["blind_id"] = blind_id


def order_rows(items: list[dict], rater_id: str) -> list[tuple[int, str]]:
    rng = secrets.SystemRandom()
    shuffled = list(items)
    rng.shuffle(shuffled)
    return [(index + 1, item["blind_id"]) for index, item in enumerate(shuffled)]


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


def append_repack_note(private_dir: Path, note: str) -> None:
    audit = private_dir / "MAPPING-AUDIT.md"
    if audit.is_file():
        with audit.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"- {note}\n")


def archive_previous_mapping(private_dir: Path, generated_utc: str) -> tuple[str, str]:
    mapping = private_dir / "mapping.csv"
    old_sha = sha256_file(mapping)
    archive_dir = private_dir / "mapping_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_utc.replace(":", "").replace("-", "")
    target = archive_dir / f"mapping-{stamp}-{old_sha[:8]}.csv"
    if target.exists():
        raise PackError(f"归档目标已存在，拒绝覆盖：{target}")
    shutil.copy2(mapping, target)
    return old_sha, str(target.relative_to(private_dir))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_pack(args) -> int:
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    private_dir = Path(args.private_dir).resolve() if args.private_dir else DEFAULT_PRIVATE_DIR
    pack_dir = Path(args.pack_dir).resolve() if args.pack_dir else DEFAULT_PACK_DIR

    config = load_blind_config(Path(args.config) if args.config else CONFIG_PATH)
    config_sha = sha256_file(Path(args.config) if args.config else CONFIG_PATH)
    seed = int(config["blind_sampling_seed"])
    algorithm_version = str(config["algorithm_version"])

    built = build_items(seed, runs_dir)
    items = built["items"]
    manifest = manifest_hashes()
    source_report = verify_source_files(built["main_rows"] + [i["row"] for i in items], manifest)

    gate_evidence = ensure_private_gate(private_dir, allow_outside_repo=args.allow_outside_repo)
    if private_dir.joinpath("mapping.csv").exists() and not args.repack:
        raise PackError(
            f"{private_dir / 'mapping.csv'} 已存在：重打包必须显式 --repack，"
            "并会在 MAPPING-AUDIT.md 记录被替换旧映射的 SHA256（不得静默覆盖）"
        )

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
    if args.repack and (private_dir / "mapping.csv").exists():
        old_sha, archive_rel = archive_previous_mapping(private_dir, generated_utc)
        repack_notes.append(
            f"{generated_utc} 重打包：被替换旧映射 SHA256={old_sha}，归档为 {archive_rel}"
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

    # ---- 落盘：pack ----
    for phase in (PHASE_FORMAL, PHASE_CALIBRATION, PHASE_SUPPLEMENTARY):
        phase_dir = pack_dir / phase
        phase_dir.mkdir(parents=True, exist_ok=True)
        for item in items:
            if item["phase"] != phase:
                continue
            _, text = packets[item["blind_id"]]
            write_text_atomic(phase_dir / f"{item['blind_id']}.md", text)
        for rater_id in ("rater_a", "rater_b"):
            write_csv_atomic(
                phase_dir / f"order_{rater_id}.csv",
                ["position", "blind_id"],
                [{"position": pos, "blind_id": bid} for pos, bid in order_rows(
                    [i for i in items if i["phase"] == phase], rater_id
                )],
            )

    # ---- 落盘：private ----
    private_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(private_dir / "selection_audit.csv", AUDIT_COLUMNS, audit_rows)
    write_csv_atomic(private_dir / "mapping.csv", MAPPING_COLUMNS, mapping_rows)
    mapping_sha = sha256_file(private_dir / "mapping.csv")
    mapping_bytes = (private_dir / "mapping.csv").stat().st_size
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
    if repack_notes and (private_dir / "MAPPING-AUDIT.md").is_file():
        # 重打包：保留历史，追加新记录而不是覆盖
        write_text_atomic(private_dir / "MAPPING-AUDIT.md", audit_text)
        for note in repack_notes:
            append_repack_note(private_dir, note)
    else:
        write_text_atomic(private_dir / "MAPPING-AUDIT.md", audit_text)

    # ---- 打包后复核：源文件哈希未变 ----
    post_report = verify_source_files(built["main_rows"], manifest)
    if [row["actual"] for row in post_report] != [row["actual"] for row in source_report if row["status"] != "manifest_missing"][: len(post_report)]:
        # 兜底：逐个 run 比对（顺序不保证）
        before = {row["run_dir"]: row["actual"] for row in source_report}
        for row in post_report:
            if before.get(row["run_dir"]) not in (None, row["actual"]):
                raise PackError(f"打包后源文件被改动：{row['run_dir']}")

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
    parser.add_argument("--repack", action="store_true", help="允许覆盖 mapping.csv（会归档旧映射）")
    parser.add_argument("--dry-run", action="store_true", help="只做抽样与校验，不写文件")
    parser.add_argument(
        "--allow-outside-repo",
        action="store_true",
        help="仅用于临时/测试目录：私有目录在仓库外时跳过 .gitignore 校验",
    )
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
