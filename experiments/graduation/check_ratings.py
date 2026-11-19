"""校验毕业设计盲评评分表（实验设计 v4.5 §5.4.6 / §7.2）。

只读校验，不修改任何评分文件，也不访问 `blind_private/`：

- 三张原始长表的表头必须与设计文档逐字一致；
- 不允许出现身份列（`sample_id` / `group` / `repeat_index` / `run_id` 等）；
- `blind_id` 只允许 32 位小写十六进制或小写 UUIDv4；
- `rater_id` 只允许 `rater_a` / `rater_b`，`phase` 必须与文件对应；
- 每个 `(blind_id, rater_id)` 只能出现一次；
- 1–5 分列只允许 1/2/3/4/5 或 `N/A`，**不允许 0**；0/1 列只允许 0/1 或 `N/A`；
- `S4_verdict` 只允许 `full` / `partial` / `wrong` 或 `N/A`；
- `notes` 不得出现可反推来源的身份信息；
- `--strict` 额外核对份数、两位评分者齐全、以及不适用矩阵的计数。

用法：

    python experiments/graduation/check_ratings.py
    python experiments/graduation/check_ratings.py --dir experiments/graduation/blind_ratings --strict
    python experiments/graduation/check_ratings.py --allow-empty   # 只校验空模板表头

退出码：0 = 全部通过；1 = 存在失败项；2 = 无法读取评分文件。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DIR = HERE / "blind_ratings"

FORMAL_HEADER = [
    "blind_id",
    "rater_id",
    "phase",
    "P1",
    "P2",
    "P3",
    "P4",
    "S2",
    "S3_flag",
    "S4_verdict",
    "notes",
]
SUPPLEMENT_HEADER = ["blind_id", "rater_id", "phase", "E1", "E7", "notes"]

IDENTITY_COLUMNS = {
    "sample_id",
    "sample",
    "group",
    "condition",
    "arm",
    "repeat_index",
    "repeat",
    "run_id",
    "experiment_id",
    "plan_seq",
    "round_index",
    "db_path",
    "model",
    "file",
    "path",
}

RATER_IDS = {"rater_a", "rater_b"}
FIVE_SCALE_COLUMNS = ("P1", "P2", "P3", "P4", "S2")
BINARY_COLUMNS = ("S3_flag", "E1", "E7")
S4_VALUES = {"full", "partial", "wrong"}
NA_VALUES = {"", "N/A"}

BLIND_ID_RE = re.compile(
    r"^(?:[0-9a-f]{32}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
IDENTITY_LEAK_PATTERNS = (
    (re.compile(r"\bS[1-7]\b"), "样本编号"),
    (re.compile(r"\b(?:sample_id|repeat_index|run_id|experiment_id|plan_seq)\b"), "身份字段名"),
    (re.compile(r"group[\s_-]*[ABCD]\b", re.IGNORECASE), "组别"),
    (re.compile(r"[ABCD][\s_-]*组"), "组别"),
    (re.compile(r"(?:条件标签|第\s*\d\s*轮|repeat\s*\d)", re.IGNORECASE), "条件或轮次"),
    (re.compile(r"main-2026\d{4}T\d{6}Z|suppl-(?:subj|s7empty)|b0-remainder"), "run 目录名"),
)

PHASE_FILES = {
    "formal": ("ratings_formal_raw.csv", FORMAL_HEADER),
    "calibration": ("ratings_calibration_raw.csv", FORMAL_HEADER),
    "supplementary": ("ratings_supplement_raw.csv", SUPPLEMENT_HEADER),
}

# v4.5 §5.4.7 冻结口径：12 份 S7 材料的 P3 记 N/A；S3_flag 只判 24 份；S4_verdict 只判 12 份；
# 补充 23 份都判 E1、其中 15 份判 E7。行数 = 材料数 × 2 位评分者。
STRICT_COUNTS = {
    "formal": {
        "blind_ids": 84,
        "rows": 168,
        "P3_na_rows": 24,
        "S3_flag_scored_rows": 48,
        "S4_verdict_scored_rows": 24,
    },
    "calibration": {"blind_ids": 6, "rows": 12},
    "supplementary": {"blind_ids": 23, "rows": 46, "E7_scored_rows": 30},
}


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到评分文件：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]
    return header, rows


def _identity_leak(notes: str) -> str:
    for pattern, label in IDENTITY_LEAK_PATTERNS:
        hit = pattern.search(notes)
        if hit:
            return f"notes 含可反推来源的信息（{label}：{hit.group(0)!r}）"
    return ""


def _check_value(errors: list[str], where: str, column: str, value: str, allowed: set[str]) -> None:
    if value in NA_VALUES:
        return
    if value in allowed:
        return
    if value == "0" and column in FIVE_SCALE_COLUMNS:
        errors.append(f"{where} {column}=0：1–5 分维度不得写 0（不适用请写 N/A）")
        return
    errors.append(f"{where} {column}={value!r} 不是合法取值（允许 {sorted(allowed)} 或 N/A）")


def check_file(directory: Path, phase: str, *, strict: bool, allow_empty: bool) -> list[str]:
    filename, expected_header = PHASE_FILES[phase]
    path = directory / filename
    errors: list[str] = []
    try:
        header, rows = _read_csv(path)
    except FileNotFoundError as error:
        return [str(error)]

    if header != expected_header:
        errors.append(f"{filename} 表头与设计文档 v4.5 §7.2 不一致：实际 {header} / 期望 {expected_header}")
    leaked_columns = [name for name in header if name.lower() in IDENTITY_COLUMNS]
    if leaked_columns:
        errors.append(f"{filename} 含身份列 {leaked_columns}：评分表不得出现样本身份")

    if not rows and not allow_empty:
        errors.append(f"{filename} 没有任何评分行（空模板只能用 --allow-empty 校验）")

    seen: dict[tuple[str, str], int] = {}
    for line_no, row in enumerate(rows, start=2):
        where = f"{filename}:{line_no}"
        blind_id = row.get("blind_id", "")
        rater_id = row.get("rater_id", "")
        if not blind_id:
            errors.append(f"{where} blind_id 为空")
        elif not BLIND_ID_RE.match(blind_id):
            errors.append(f"{where} blind_id={blind_id!r} 不是 32 位小写十六进制或小写 UUIDv4")
        if rater_id not in RATER_IDS:
            errors.append(f"{where} rater_id={rater_id!r} 只能是 rater_a / rater_b")
        if row.get("phase", "") != phase:
            errors.append(f"{where} phase={row.get('phase')!r} 应为 {phase!r}")
        key = (blind_id, rater_id)
        if blind_id and rater_id:
            if key in seen:
                errors.append(f"{where} 重复的 (blind_id, rater_id)，首次出现在第 {seen[key]} 行")
            else:
                seen[key] = line_no

        if phase in ("formal", "calibration"):
            for column in FIVE_SCALE_COLUMNS:
                value = row.get(column, "")
                if column != "P3" and value in NA_VALUES:
                    errors.append(f"{where} {column} 缺失：P1/P2/P4/S2 必须给 1–5 分")
                    continue
                _check_value(errors, where, column, value, {"1", "2", "3", "4", "5"})
            _check_value(errors, where, "S3_flag", row.get("S3_flag", ""), {"0", "1"})
            _check_value(errors, where, "S4_verdict", row.get("S4_verdict", ""), S4_VALUES)
            leaked = _identity_leak(row.get("notes", ""))
            if leaked:
                errors.append(f"{where} {leaked}")
        else:
            for column in ("E1", "E7"):
                value = row.get(column, "")
                if column == "E1" and value in NA_VALUES:
                    errors.append(f"{where} E1 缺失：补充批次每份都要判 E1")
                    continue
                _check_value(errors, where, column, value, {"0", "1"})
            leaked = _identity_leak(row.get("notes", ""))
            if leaked:
                errors.append(f"{where} {leaked}")

    if strict and rows:
        expected = STRICT_COUNTS[phase]
        blind_ids = {row.get("blind_id", "") for row in rows if row.get("blind_id")}
        if len(blind_ids) != expected["blind_ids"]:
            errors.append(
                f"{filename} blind_id 份数 {len(blind_ids)} 与冻结口径 {expected['blind_ids']} 不一致"
            )
        if len(rows) != expected["rows"]:
            errors.append(f"{filename} 评分行数 {len(rows)} 与冻结口径 {expected['rows']} 不一致（每份两人各一行）")
        for blind_id in sorted(blind_ids):
            raters = {row.get("rater_id", "") for row in rows if row.get("blind_id") == blind_id}
            if raters != RATER_IDS:
                errors.append(f"{filename} blind_id={blind_id} 的评分者为 {sorted(raters)}，应为 rater_a + rater_b")
        if phase in ("formal", "calibration"):
            if "P3_na_rows" in expected:
                na_rows = sum(1 for row in rows if row.get("P3", "") in NA_VALUES)
                if na_rows != expected["P3_na_rows"]:
                    errors.append(f"{filename} P3 记 N/A 的行数 {na_rows} 与冻结口径 {expected['P3_na_rows']} 不一致（S7 的 12 份 × 2 人）")
            if "S3_flag_scored_rows" in expected:
                scored = sum(1 for row in rows if row.get("S3_flag", "") not in NA_VALUES)
                if scored != expected["S3_flag_scored_rows"]:
                    errors.append(f"{filename} S3_flag 已判行数 {scored} 与冻结口径 {expected['S3_flag_scored_rows']} 不一致（24 份 × 2 人）")
            if "S4_verdict_scored_rows" in expected:
                scored = sum(1 for row in rows if row.get("S4_verdict", "") not in NA_VALUES)
                if scored != expected["S4_verdict_scored_rows"]:
                    errors.append(f"{filename} S4_verdict 已判行数 {scored} 与冻结口径 {expected['S4_verdict_scored_rows']} 不一致（12 份 × 2 人）")
        if phase == "supplementary":
            scored = sum(1 for row in rows if row.get("E7", "") not in NA_VALUES)
            if scored != expected["E7_scored_rows"]:
                errors.append(f"{filename} E7 已判行数 {scored} 与冻结口径 {expected['E7_scored_rows']} 不一致（15 份 × 2 人）")

    return errors


def collect_errors(directory: Path, *, strict: bool, allow_empty: bool) -> list[str]:
    if not directory.is_dir():
        return [f"评分目录不存在：{directory}"]
    errors: list[str] = []
    for phase in PHASE_FILES:
        errors.extend(check_file(directory, phase, strict=strict, allow_empty=allow_empty))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验盲评评分表（v4.5 §5.4.6 / §7.2）")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="评分表目录（默认 experiments/graduation/blind_ratings）")
    parser.add_argument("--strict", action="store_true", help="额外核对份数、两位评分者齐全与不适用矩阵计数")
    parser.add_argument("--allow-empty", action="store_true", help="允许只有表头的空模板（用于交付前自检）")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    errors = collect_errors(directory, strict=args.strict, allow_empty=args.allow_empty)
    for phase, (filename, _) in PHASE_FILES.items():
        status = "FAIL" if any(filename in error for error in errors) else "PASS"
        print(f"[{status}] {phase:<13} {directory / filename}")
    if errors:
        print(f"\n发现 {len(errors)} 个问题：")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"\n全部通过（strict={args.strict}, allow_empty={args.allow_empty}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
