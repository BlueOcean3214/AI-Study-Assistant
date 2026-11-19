"""校验毕业设计盲评评分表（实验设计 v4.5 §5.4.6 / §7.2）。

只读校验，不修改任何评分文件，也不访问 `blind_private/`：

- 三张原始长表的表头必须与设计文档逐字一致；
- 不允许出现身份列（`sample_id` / `group` / `repeat_index` / `run_id` 等）；
- `blind_id` 只允许 32 位小写十六进制或小写 UUIDv4；
- `rater_id` 只允许 `rater_a` / `rater_b`，`phase` 必须与文件对应；
- 每个 `(blind_id, rater_id)` 只能出现一次；
- **空白不等于 `N/A`**：只有显式写 `N/A` 才表示该份材料不适用，空字符串一律按缺失值报错；
- 1–5 分列只允许 1/2/3/4/5，允许不适用的列（P3）可写 `N/A`，**不允许 0**；
- 0/1 列只允许 0/1，允许不适用的列（S3_flag / E7）可写 `N/A`；必填列（E1）不得写 `N/A`；
- `S4_verdict` 只允许 `full` / `partial` / `wrong` 或 `N/A`；
- `notes` 不得出现可反推来源的身份信息；
- `--strict` 额外核对份数、两位评分者齐全、不适用矩阵的计数，以及同一 `blind_id` 两位评分者的
  **适用性状态一致**（只比“是否写了 `N/A`”，不比分数；**不能替代** `blind_private/mapping.csv`
  的逐样本适用矩阵核对，真正的源样本适用性仍须在盲评包私有映射审计阶段验证）。

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
# 只有显式写 `N/A` 才表示「该份材料不适用」；空字符串是缺失值，**绝不等于 N/A**。
NA_TOKEN = "N/A"
MISSING_TOKEN = ""
# §5.4.7 冻结口径里允许记「不适用」的列；未列出的列都是必填。
OPTIONAL_COLUMNS = {
    "formal": frozenset({"P3", "S3_flag", "S4_verdict"}),
    "calibration": frozenset({"P3", "S3_flag", "S4_verdict"}),
    "supplementary": frozenset({"E7"}),
}

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

# `--mapping` 模式下逐项核对的维度；适用性一律调用 analyze_stats 的冻结矩阵函数，不在这里复制规则。
MAPPING_FORMAL_ITEMS = ("P1", "P2", "P3", "P4", "S2", "S3_flag", "S4_verdict")
MAPPING_SUPPLEMENT_ITEMS = ("E1", "E7")


class MappingError(RuntimeError):
    """mapping.csv 本身不可用：与评分表的错误分开报，避免把映射问题误当成评分问题。"""


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


def _check_value(
    errors: list[str],
    where: str,
    column: str,
    value: str,
    allowed: set[str],
    *,
    optional: bool,
    required_missing: str = "",
    required_na: str = "",
) -> None:
    """校验单个维度取值；空白按缺失处理，只有显式的 `N/A` 才算不适用。"""
    if value == MISSING_TOKEN:
        if optional:
            errors.append(
                f"{where} {column} 为空：空白不等于 N/A，请填写 {sorted(allowed)} 或明确写 N/A"
            )
        else:
            errors.append(
                required_missing or f"{where} {column} 缺失：必须填写 {sorted(allowed)}（空白不等于 N/A）"
            )
        return
    if value == NA_TOKEN:
        if optional:
            return
        errors.append(required_na or f"{where} {column}=N/A：该维度必填，不能记不适用")
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

        optional_columns = OPTIONAL_COLUMNS[phase]
        if phase in ("formal", "calibration"):
            for column in FIVE_SCALE_COLUMNS:
                _check_value(
                    errors,
                    where,
                    column,
                    row.get(column, ""),
                    {"1", "2", "3", "4", "5"},
                    optional=column in optional_columns,
                    required_missing=f"{where} {column} 缺失：P1/P2/P4/S2 必须给 1–5 分",
                    required_na=f"{where} {column}=N/A：P1/P2/P4/S2 必填，不能记不适用",
                )
            _check_value(
                errors,
                where,
                "S3_flag",
                row.get("S3_flag", ""),
                {"0", "1"},
                optional="S3_flag" in optional_columns,
            )
            _check_value(
                errors,
                where,
                "S4_verdict",
                row.get("S4_verdict", ""),
                S4_VALUES,
                optional="S4_verdict" in optional_columns,
            )
            leaked = _identity_leak(row.get("notes", ""))
            if leaked:
                errors.append(f"{where} {leaked}")
        else:
            for column in ("E1", "E7"):
                _check_value(
                    errors,
                    where,
                    column,
                    row.get(column, ""),
                    {"0", "1"},
                    optional=column in optional_columns,
                    required_missing=f"{where} {column} 缺失：补充批次每份都要判 {column}",
                    required_na=f"{where} {column}=N/A：补充批次每份都要判 {column}，不能记不适用",
                )
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
        # 适用性状态一致：同一 blind_id 的两位评分者在可选字段上必须「同为 N/A」或「同为已评分」。
        # 只比适用性、不比分数（3 vs 4 允许；0 vs 1 也允许，属于一致性分析要处理的分歧）；
        # 一方 N/A、另一方有分 = 两人对同一份材料是否适用判断冲突，必须拦下。
        # 空白已在逐行校验里报过「空白不等于 N/A」，这里跳过，避免同一问题重复计数。
        by_pair = {(row.get("blind_id", ""), row.get("rater_id", "")): row for row in rows}
        for blind_id in sorted(blind_ids):
            for column in sorted(OPTIONAL_COLUMNS[phase]):
                values = {
                    rater_id: by_pair.get((blind_id, rater_id), {}).get(column, MISSING_TOKEN)
                    for rater_id in sorted(RATER_IDS)
                }
                if MISSING_TOKEN in values.values():
                    continue
                if len({value == NA_TOKEN for value in values.values()}) > 1:
                    errors.append(
                        f"{filename} blind_id={blind_id} 的 {column} 适用性不一致："
                        f"rater_a={values['rater_a']!r} / rater_b={values['rater_b']!r}；"
                        "两位评分者必须同为 N/A 或同为有评分（分数可以不同）"
                    )
        if phase in ("formal", "calibration"):
            if "P3_na_rows" in expected:
                na_rows = sum(1 for row in rows if row.get("P3", "") == NA_TOKEN)
                if na_rows != expected["P3_na_rows"]:
                    errors.append(f"{filename} P3 记 N/A 的行数 {na_rows} 与冻结口径 {expected['P3_na_rows']} 不一致（S7 的 12 份 × 2 人）")
            if "S3_flag_scored_rows" in expected:
                scored = sum(1 for row in rows if row.get("S3_flag", "") not in (MISSING_TOKEN, NA_TOKEN))
                if scored != expected["S3_flag_scored_rows"]:
                    errors.append(f"{filename} S3_flag 已判行数 {scored} 与冻结口径 {expected['S3_flag_scored_rows']} 不一致（24 份 × 2 人）")
            if "S4_verdict_scored_rows" in expected:
                scored = sum(1 for row in rows if row.get("S4_verdict", "") not in (MISSING_TOKEN, NA_TOKEN))
                if scored != expected["S4_verdict_scored_rows"]:
                    errors.append(f"{filename} S4_verdict 已判行数 {scored} 与冻结口径 {expected['S4_verdict_scored_rows']} 不一致（12 份 × 2 人）")
        if phase == "supplementary":
            scored = sum(1 for row in rows if row.get("E7", "") not in (MISSING_TOKEN, NA_TOKEN))
            if scored != expected["E7_scored_rows"]:
                errors.append(f"{filename} E7 已判行数 {scored} 与冻结口径 {expected['E7_scored_rows']} 不一致（15 份 × 2 人）")

    return errors


def _stats_module():
    """延迟导入 analyze_stats：复用 mapping 解析与 §5.4.7 冻结适用矩阵，不复制口径。"""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import analyze_stats  # noqa: PLC0415 —— 只在 --mapping 时加载

    return analyze_stats


def _mapping_by_phase(mapping_path: Path) -> dict[str, dict[str, dict]]:
    """读 mapping.csv 并按 phase 分组；mapping 不合规时统一转成 MappingError。"""
    stats = _stats_module()
    try:
        by_id = stats.load_mapping(mapping_path)
    except stats.StatsError as error:
        detail = "；".join(str(item) for item in error.problems[:5])
        reason = f"{error.detail}{('：' + detail) if detail else ''}"
        raise MappingError(f"mapping.csv 不可用（{error.code}）：{reason}") from error
    grouped: dict[str, dict[str, dict]] = {phase: {} for phase in PHASE_FILES}
    for blind_id, meta in by_id.items():
        grouped[meta["phase"]][blind_id] = meta
    return grouped


def _item_applicable(stats, phase: str, item: str, meta: dict) -> bool:
    if phase == "supplementary":
        return stats._supplement_item_applicable(item, meta)
    return stats._item_applicable(item, meta["sample_id"])


def check_against_mapping(directory: Path, mapping_path: Path, *, allow_empty: bool) -> list[str]:
    """用 mapping.csv 核对「谁该出现」和「该不该有分」。

    `--strict` 只能数行数与 N/A 行数；把 S7 的 P3=N/A 与非 S7 的分数互换后总数不变，
    只有对着 mapping 的样本身份逐项比对才能发现。这里检测三类问题：
    1) 评分表里的 blind_id 不在 mapping 的对应 phase（例如校准材料混进正式表）；
    2) mapping 里的材料缺行或缺某位评分者；
    3) 某一项该不适用却给了分，或该适用却记了 N/A。
    """
    stats = _stats_module()
    grouped = _mapping_by_phase(mapping_path)
    errors: list[str] = []
    owner = {blind_id: phase for phase, entries in grouped.items() for blind_id in entries}
    for phase, (filename, _) in PHASE_FILES.items():
        try:
            _, rows = _read_csv(directory / filename)
        except FileNotFoundError as error:
            errors.append(str(error))
            continue
        expected = grouped[phase]
        seen = {row.get("blind_id", "") for row in rows if row.get("blind_id")}
        for blind_id in sorted(seen - set(expected)):
            errors.append(
                f"{filename} 出现 blind_id={blind_id}，但 mapping.csv 把它归在 "
                f"phase={owner.get(blind_id, '（缺失）')}：评分表与盲评包不是同一套材料"
            )
        if not allow_empty:
            for blind_id in sorted(set(expected) - seen):
                errors.append(
                    f"{filename} 缺 mapping.csv 里的 {phase} 材料 blind_id={blind_id}（每位评分者各一行）"
                )
        by_pair = {(row.get("blind_id", ""), row.get("rater_id", "")): row for row in rows}
        for blind_id, meta in sorted(expected.items()):
            if blind_id not in seen:
                continue
            present = {rater for (sid, rater) in by_pair if sid == blind_id}
            missing = [rater for rater in sorted(RATER_IDS) if rater not in present]
            if missing:
                errors.append(f"{filename} blind_id={blind_id} 缺评分者 {missing}（每位评分者各一行）")
            items = MAPPING_SUPPLEMENT_ITEMS if phase == "supplementary" else MAPPING_FORMAL_ITEMS
            for item in items:
                applicable = _item_applicable(stats, phase, item, meta)
                for rater_id in sorted(RATER_IDS):
                    row = by_pair.get((blind_id, rater_id))
                    if row is None:
                        continue
                    value = row.get(item, "")
                    if value == MISSING_TOKEN:
                        continue  # 空白已由 _check_value 报过，不重复计数
                    where = f"{filename} blind_id={blind_id}/{rater_id} {item}"
                    if applicable and value == NA_TOKEN:
                        errors.append(f"{where}=N/A：mapping.csv 显示该材料适用该维度，不得记不适用")
                    elif not applicable and value != NA_TOKEN:
                        errors.append(
                            f"{where}={value!r}：mapping.csv 显示该材料不适用该维度"
                            f"（sample_id={meta['sample_id']}），必须记 N/A"
                        )
    return errors


def collect_errors(
    directory: Path,
    *,
    strict: bool,
    allow_empty: bool,
    mapping_path: Path | None = None,
) -> list[str]:
    if not directory.is_dir():
        return [f"评分目录不存在：{directory}"]
    errors: list[str] = []
    for phase in PHASE_FILES:
        errors.extend(check_file(directory, phase, strict=strict, allow_empty=allow_empty))
    if mapping_path is not None:
        try:
            errors.extend(check_against_mapping(directory, mapping_path, allow_empty=allow_empty))
        except MappingError as error:
            errors.append(str(error))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验盲评评分表（v4.5 §5.4.6 / §7.2）")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="评分表目录（默认 experiments/graduation/blind_ratings）")
    parser.add_argument("--strict", action="store_true", help="额外核对份数、两位评分者齐全与不适用矩阵计数")
    parser.add_argument("--allow-empty", action="store_true", help="允许只有表头的空模板（用于交付前自检）")
    parser.add_argument(
        "--mapping",
        default=None,
        help="盲评包的私有 mapping.csv：核对 blind_id 归属 phase、评分者齐全与逐项 N/A 是否符合 §5.4.7",
    )
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    mapping_path = Path(args.mapping) if args.mapping else None
    errors = collect_errors(
        directory, strict=args.strict, allow_empty=args.allow_empty, mapping_path=mapping_path
    )
    for phase, (filename, _) in PHASE_FILES.items():
        status = "FAIL" if any(filename in error for error in errors) else "PASS"
        print(f"[{status}] {phase:<13} {directory / filename}")
    if mapping_path is not None:
        failed = any("mapping.csv" in error for error in errors)
        print(f"[{'FAIL' if failed else 'PASS'}] {"mapping":<13} {mapping_path}")
    if errors:
        print(f"\n发现 {len(errors)} 个问题：")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(
        f"\n全部通过（strict={args.strict}, allow_empty={args.allow_empty}, "
        f"mapping={mapping_path if mapping_path else '未提供'}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
