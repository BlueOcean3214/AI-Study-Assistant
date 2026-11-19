"""盲评评分材料（阶段 C）的合成数据回归测试。

只使用 `tempfile` 临时目录里的合成评分表，不读写 `experiments/graduation/runs/`、
根目录 `feedback.db`、`.rag_cache`，也不接触 `blind_private/` 或任何真实盲评材料。

覆盖：
- 三张模板的合法性与 `--allow-empty` 语义；
- 表头漂移、身份列、notes 身份泄露、blind_id/rater_id/phase 非法、重复 (blind_id, rater_id)；
- 1–5 分维度出现 0、必填维度缺失、S4_verdict 非法；
- `--strict` 的份数、行数、两位评分者齐全、以及 §5.4.7 不适用矩阵计数；
- 文件缺失时的失败与退出码。

运行：`.venv\\Scripts\\python.exe test_graduation_rating_materials.py`
"""

from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import traceback
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments" / "graduation"))

import check_ratings as cr  # noqa: E402 - 校验器在 experiments/graduation/ 下，需先补 sys.path

TMP_ROOT = Path(tempfile.mkdtemp(prefix="rating_materials_test_"))
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(name)
        print(f"PASS {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"FAIL {name} :: {detail}")


def _write(directory: Path, phase: str, header: list[str], rows: list[dict[str, str]]) -> Path:
    filename, _ = cr.PHASE_FILES[phase]
    path = directory / filename
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in header})
    return path


def _blind_id(index: int) -> str:
    return f"{index:032x}"


def _formal_row(blind_id: str, rater: str, *, phase: str = "formal", p3: str = "3", s3: str = "N/A", s4: str = "N/A", notes: str = "") -> dict[str, str]:
    return {
        "blind_id": blind_id,
        "rater_id": rater,
        "phase": phase,
        "P1": "4",
        "P2": "3",
        "P3": p3,
        "P4": "5",
        "S2": "4",
        "S3_flag": s3,
        "S4_verdict": s4,
        "notes": notes,
    }


def _supplement_row(blind_id: str, rater: str, *, e1: str = "0", e7: str = "N/A", notes: str = "") -> dict[str, str]:
    return {
        "blind_id": blind_id,
        "rater_id": rater,
        "phase": "supplementary",
        "E1": e1,
        "E7": e7,
        "notes": notes,
    }


def _formal_rows() -> list[dict[str, str]]:
    """84 份；前 12 份 P3 记 N/A（24 行），12–35 判 S3_flag（48 行），12–23 判 S4_verdict（24 行）。"""
    rows: list[dict[str, str]] = []
    for index in range(84):
        p3 = "N/A" if index < 12 else "3"
        s3 = "0" if 12 <= index < 36 else "N/A"
        s4 = "full" if 12 <= index < 24 else "N/A"
        for rater in ("rater_a", "rater_b"):
            rows.append(_formal_row(_blind_id(index), rater, p3=p3, s3=s3, s4=s4))
    return rows


def _calibration_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index in range(100, 106):
        for rater in ("rater_a", "rater_b"):
            rows.append(_formal_row(_blind_id(index), rater, phase="calibration", p3="4"))
    return rows


def _supplement_rows() -> list[dict[str, str]]:
    """23 份；前 15 份判 E7（30 行），其余记 N/A。"""
    rows: list[dict[str, str]] = []
    for index in range(200, 223):
        e7 = "1" if index < 215 else "N/A"
        for position, rater in enumerate(("rater_a", "rater_b")):
            rows.append(_supplement_row(_blind_id(index), rater, e1="0" if position == 0 else "1", e7=e7))
    return rows


def _valid_directory() -> Path:
    directory = TMP_ROOT / "valid"
    _write(directory, "formal", cr.FORMAL_HEADER, _formal_rows())
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    return directory


def _errors_after(mutation) -> list[str]:
    """基于合法数据做一次变异，返回 strict=False 的校验错误。"""
    directory = TMP_ROOT / f"case_{len(PASSED) + len(FAILED)}"
    formal = _formal_rows()
    calibration = _calibration_rows()
    supplement = _supplement_rows()
    mutation(directory, formal, calibration, supplement)
    _write(directory, "formal", cr.FORMAL_HEADER, formal)
    _write(directory, "calibration", cr.FORMAL_HEADER, calibration)
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, supplement)
    return cr.collect_errors(directory, strict=False, allow_empty=False)


def _expect(errors: list[str], needle: str) -> str:
    for error in errors:
        if needle in error:
            return ""
    return f"未出现期望错误 {needle!r}；实际 {errors[:4]}"


def test_empty_templates() -> None:
    directory = TMP_ROOT / "empty"
    for phase, (filename, header) in cr.PHASE_FILES.items():
        _write(directory, phase, header, [])

    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("空模板不带 --allow-empty 时被判失败", len(errors) == 3, f"实际 {len(errors)} 条错误：{errors}")
    check(
        "空模板错误提示要求 --allow-empty",
        all("没有任何评分行" in error for error in errors),
        f"{errors}",
    )
    check("空模板带 --allow-empty 时全部通过", cr.collect_errors(directory, strict=True, allow_empty=True) == [], "")
    check("空模板 + --allow-empty 的退出码为 0", cr.main(["--dir", str(directory), "--allow-empty"]) == 0, "")


def test_valid_strict() -> None:
    directory = _valid_directory()
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("合法合成数据在 --strict 下零错误", errors == [], f"{errors}")
    check("合法合成数据退出码为 0", cr.main(["--dir", str(directory), "--strict"]) == 0, "")


def test_header_and_identity_columns() -> None:
    directory = TMP_ROOT / "missing_column"
    formal = _formal_rows()
    header = [column for column in cr.FORMAL_HEADER if column != "S2"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "ratings_formal_raw.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in formal:
            writer.writerow({key: row.get(key, "") for key in header})
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=False, allow_empty=False)
    check("表头缺列被判定为与设计文档不一致", any("表头与设计文档" in error for error in errors), f"{errors[:3]}")

    directory = TMP_ROOT / "identity_column"
    formal = _formal_rows()
    for row in formal:
        row["sample_id"] = "S1"
    path = directory / "ratings_formal_raw.csv"
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=cr.FORMAL_HEADER + ["sample_id"])
        writer.writeheader()
        for row in formal:
            writer.writerow({key: row.get(key, "") for key in cr.FORMAL_HEADER + ["sample_id"]})
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=False, allow_empty=False)
    check("表头出现身份列 sample_id 被拒绝", any("含身份列" in error for error in errors), f"{errors}")


def test_notes_identity_leak() -> None:
    def leak_sample(directory, formal, calibration, supplement):
        formal[0]["notes"] = "这份输出对应 S3 组，明显更好"

    check("notes 出现样本编号被拒绝", _expect(_errors_after(leak_sample), "notes 含可反推来源的信息") == "")

    def leak_repeat(directory, formal, calibration, supplement):
        formal[1]["notes"] = "第 2 轮重复的结果"

    check("notes 出现轮次信息被拒绝", _expect(_errors_after(leak_repeat), "notes 含可反推来源的信息") == "")

    def leak_run(directory, formal, calibration, supplement):
        formal[2]["notes"] = "来自 main-20261113T061051Z 的样本"

    check("notes 出现 run 目录名被拒绝", _expect(_errors_after(leak_run), "notes 含可反推来源的信息") == "")

    def clean_notes(directory, formal, calibration, supplement):
        formal[0]["notes"] = "计划可执行，难度与 gold 一致"

    check("普通评语不会被误判为身份泄露", _errors_after(clean_notes) == [], "")


def test_invalid_ids_and_phase() -> None:
    def bad_blind_id(directory, formal, calibration, supplement):
        formal[0]["blind_id"] = "NOT-A-BLIND-ID"

    check("非法 blind_id 被拒绝", _expect(_errors_after(bad_blind_id), "不是 32 位小写十六进制或小写 UUIDv4") == "")

    def uppercase_blind_id(directory, formal, calibration, supplement):
        formal[0]["blind_id"] = "abcdef0123456789abcdef0123456789".upper()

    check("大写 blind_id 被拒绝", _expect(_errors_after(uppercase_blind_id), "不是 32 位小写十六进制或小写 UUIDv4") == "")

    def bad_rater(directory, formal, calibration, supplement):
        formal[1]["rater_id"] = "rater_c"

    check("非法 rater_id 被拒绝", _expect(_errors_after(bad_rater), "只能是 rater_a / rater_b") == "")

    def bad_phase(directory, formal, calibration, supplement):
        formal[2]["phase"] = "supplementary"

    check("phase 与文件不符被拒绝", _expect(_errors_after(bad_phase), "应为 'formal'") == "")

    def empty_blind_id(directory, formal, calibration, supplement):
        formal[3]["blind_id"] = ""

    check("空 blind_id 被拒绝", _expect(_errors_after(empty_blind_id), "blind_id 为空") == "")


def test_duplicate_rows() -> None:
    def duplicate(directory, formal, calibration, supplement):
        formal.append(deepcopy(formal[0]))

    check(
        "重复 (blind_id, rater_id) 被拒绝并指出首次行号",
        _expect(_errors_after(duplicate), "重复的 (blind_id, rater_id)，首次出现在第 2 行") == "",
    )


def test_value_rules() -> None:
    def zero_score(directory, formal, calibration, supplement):
        formal[0]["P1"] = "0"

    check("1–5 分维度写 0 被拒绝", _expect(_errors_after(zero_score), "不得写 0") == "")

    def missing_required(directory, formal, calibration, supplement):
        formal[0]["S2"] = ""

    check("P1/P2/P4/S2 缺失被拒绝", _expect(_errors_after(missing_required), "缺失：P1/P2/P4/S2 必须给 1–5 分") == "")

    def p3_na_allowed(directory, formal, calibration, supplement):
        formal[0]["P3"] = "N/A"

    check("P3 记 N/A 是合法的", _errors_after(p3_na_allowed) == [], "")

    def bad_s4(directory, formal, calibration, supplement):
        formal[12]["S4_verdict"] = "mostly"

    check("S4_verdict 非法取值被拒绝", _expect(_errors_after(bad_s4), "S4_verdict='mostly'") == "")

    def bad_binary(directory, formal, calibration, supplement):
        formal[12]["S3_flag"] = "2"

    check("S3_flag 只能是 0/1", _expect(_errors_after(bad_binary), "S3_flag='2'") == "")

    def supplement_e1_missing(directory, formal, calibration, supplement):
        supplement[0]["E1"] = ""

    check("补充批次缺 E1 被拒绝", _expect(_errors_after(supplement_e1_missing), "E1 缺失") == "")

    def supplement_e7_na(directory, formal, calibration, supplement):
        supplement[0]["E7"] = "N/A"

    check("补充批次 E7 记 N/A 是合法的", _errors_after(supplement_e7_na) == [], "")


def test_blank_is_not_na() -> None:
    """空白不等于 N/A：允许不适用的列留空必须报错，必填列写 N/A 必须报错。"""

    def p3_blank(directory, formal, calibration, supplement):
        formal[0]["P3"] = ""

    errors = _errors_after(p3_blank)
    check("P3 留空被拒绝并提示写 N/A", _expect(errors, "P3 为空：空白不等于 N/A") == "", f"{errors}")

    def s3_blank(directory, formal, calibration, supplement):
        formal[12]["S3_flag"] = ""

    errors = _errors_after(s3_blank)
    check("S3_flag 留空被拒绝并提示写 N/A", _expect(errors, "S3_flag 为空：空白不等于 N/A") == "", f"{errors}")

    def s4_blank(directory, formal, calibration, supplement):
        formal[12]["S4_verdict"] = ""

    errors = _errors_after(s4_blank)
    check("S4_verdict 留空被拒绝并提示写 N/A", _expect(errors, "S4_verdict 为空：空白不等于 N/A") == "", f"{errors}")

    def e7_blank(directory, formal, calibration, supplement):
        supplement[0]["E7"] = ""

    errors = _errors_after(e7_blank)
    check("补充批次 E7 留空被拒绝并提示写 N/A", _expect(errors, "E7 为空：空白不等于 N/A") == "", f"{errors}")

    def p1_na(directory, formal, calibration, supplement):
        formal[0]["P1"] = "N/A"

    errors = _errors_after(p1_na)
    check("必填维度写 N/A 被拒绝", _expect(errors, "P1=N/A：P1/P2/P4/S2 必填") == "", f"{errors}")

    def supplement_e1_na(directory, formal, calibration, supplement):
        supplement[0]["E1"] = "N/A"

    errors = _errors_after(supplement_e1_na)
    check("补充批次 E1 写 N/A 被拒绝", _expect(errors, "E1=N/A：补充批次每份都要判 E1") == "", f"{errors}")

    def optional_na_tolerated(directory, formal, calibration, supplement):
        formal[0]["P3"] = "N/A"
        formal[12]["S3_flag"] = "N/A"
        formal[12]["S4_verdict"] = "N/A"
        supplement[0]["E7"] = "N/A"

    check("显式 N/A 在不适用维度仍然通过", _errors_after(optional_na_tolerated) == [], "")

    # strict 下空白不得被当成「不适用」计数：把一行合法 N/A 改成空白，N/A 行数应变成 23。
    directory = TMP_ROOT / "blank_not_counted_as_na"
    formal = _formal_rows()
    target = next(row for row in formal if row.get("P3") == "N/A")
    target["P3"] = ""
    _write(directory, "formal", cr.FORMAL_HEADER, formal)
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("strict 下留空不计入 P3 的 N/A 行数", _expect(errors, "P3 记 N/A 的行数 23") == "", f"{errors}")
    check("strict 下留空本身仍作为缺失报错", _expect(errors, "P3 为空：空白不等于 N/A") == "", f"{errors}")


def test_applicability_consistency() -> None:
    """同一 blind_id 的两位评分者在可选列上必须同为 N/A 或同为已评分（只比适用性，不比分数）。"""

    def errors_with(mutation, *, strict: bool = True) -> list[str]:
        directory = TMP_ROOT / f"applicability_{len(PASSED) + len(FAILED)}"
        formal = _formal_rows()
        calibration = _calibration_rows()
        supplement = _supplement_rows()
        mutation(formal, calibration, supplement)
        _write(directory, "formal", cr.FORMAL_HEADER, formal)
        _write(directory, "calibration", cr.FORMAL_HEADER, calibration)
        _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, supplement)
        return cr.collect_errors(directory, strict=strict, allow_empty=False)

    def pick(rows: list[dict[str, str]], index: int, rater: str) -> dict[str, str]:
        blind_id = _blind_id(index)
        for item in rows:
            if item["blind_id"] == blind_id and item["rater_id"] == rater:
                return item
        raise AssertionError(f"合成数据缺少 blind_id={blind_id} / {rater} 的行")

    def set_value(rows: list[dict[str, str]], index: int, rater: str, column: str, value: str):
        pick(rows, index, rater)[column] = value

    # 1. 合法数据在 strict 下不应出现适用性错误（避免误报）。
    baseline = errors_with(lambda *_: None)
    check("合法数据在 strict 下仍全部通过", baseline == [], f"实际 {baseline[:4]}")

    # 2. 一方 N/A、另一方有分 → 必须拦下，且错误里给出两位评分者的原值。
    mismatch = errors_with(lambda formal, *_: set_value(formal, 12, "rater_b", "P3", "N/A"))
    check("一方 P3 写 N/A、另一方给分被判失败", _expect(mismatch, "P3 适用性不一致") == "", f"{mismatch[:4]}")
    check(
        "适用性不一致的错误带出双方原值",
        _expect(mismatch, "rater_a='3' / rater_b='N/A'") == "",
        f"{mismatch[:4]}",
    )

    # 3. 两人分数不同（3 vs 4）只属评分分歧，不属适用性冲突 → 通过。
    different_scores = errors_with(
        lambda formal, *_: (set_value(formal, 12, "rater_a", "P3", "3"), set_value(formal, 12, "rater_b", "P3", "4"))
    )
    check("两人 P3 分数不同（3 vs 4）不被判为适用性不一致", different_scores == [], f"{different_scores[:4]}")

    # 4. 两人都写 N/A → 通过。
    both_na = errors_with(
        lambda formal, *_: (set_value(formal, 0, "rater_a", "P3", "N/A"), set_value(formal, 0, "rater_b", "P3", "N/A"))
    )
    check("两人都写 N/A 通过", both_na == [], f"{both_na[:4]}")

    # 5. 二分类 0 vs 1 → 允许（交后续一致性分析），不算适用性冲突。
    binary_split = errors_with(lambda formal, *_: set_value(formal, 12, "rater_b", "S3_flag", "1"))
    check("S3_flag 两人 0 vs 1 允许（不算适用性冲突）", binary_split == [], f"{binary_split[:4]}")

    # 6. 空白仍按缺失报错，且不重复报成适用性不一致。
    blank = errors_with(lambda formal, *_: set_value(formal, 12, "rater_b", "P3", ""))
    check("空白仍报缺失", _expect(blank, "空白不等于 N/A") == "", f"{blank[:4]}")
    check("空白不重复报成适用性不一致", _expect(blank, "适用性不一致") != "", f"{blank[:4]}")

    # 7. 配对缺失（只剩一位评分者）走评分者缺失，不误报适用性。
    def drop_rater_b(formal, *_) -> None:
        blind_id = _blind_id(12)
        formal[:] = [item for item in formal if not (item["blind_id"] == blind_id and item["rater_id"] == "rater_b")]

    missing_pair = errors_with(drop_rater_b)
    check("配对缺失仍报评分者不齐", _expect(missing_pair, "应为 rater_a + rater_b") == "", f"{missing_pair[:4]}")
    check("配对缺失不额外报适用性不一致", _expect(missing_pair, "适用性不一致") != "", f"{missing_pair[:4]}")

    # 8. 补充批次只查 E7：一方 N/A、另一方有分被拒；两人同为 N/A 通过。
    supplement_mismatch = errors_with(lambda _formal, _calibration, supplement: set_value(supplement, 200, "rater_b", "E7", "N/A"))
    check("补充批次 E7 一方 N/A 另一方给分被判失败", _expect(supplement_mismatch, "E7 适用性不一致") == "", f"{supplement_mismatch[:4]}")
    supplement_both_na = errors_with(
        lambda _formal, _calibration, supplement: (
            set_value(supplement, 215, "rater_a", "E7", "N/A"),
            set_value(supplement, 215, "rater_b", "E7", "N/A"),
        )
    )
    check("补充批次两人 E7 都写 N/A 通过", supplement_both_na == [], f"{supplement_both_na[:4]}")


def test_strict_counts() -> None:
    def drop_one_blind_id(directory, formal, calibration, supplement):
        target = formal[-1]["blind_id"]
        formal[:] = [row for row in formal if row["blind_id"] != target]

    errors = _errors_after(drop_one_blind_id)
    check("strict 关闭时不核对份数（先确认变异本身合法）", errors == [], f"{errors}")

    directory = TMP_ROOT / "count_missing_id"
    formal = _formal_rows()
    target = formal[-1]["blind_id"]
    formal = [row for row in formal if row["blind_id"] != target]
    _write(directory, "formal", cr.FORMAL_HEADER, formal)
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("strict 检出 blind_id 份数不足", _expect(errors, "blind_id 份数 83 与冻结口径 84 不一致") == "")
    check("strict 检出评分行数不足", any("评分行数 166" in error for error in errors), f"{errors}")

    directory = TMP_ROOT / "count_missing_rater"
    formal = _formal_rows()
    formal = [row for row in formal if not (row["blind_id"] == _blind_id(0) and row["rater_id"] == "rater_b")]
    _write(directory, "formal", cr.FORMAL_HEADER, formal)
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("strict 检出某份缺一位评分者", any("评分者为 ['rater_a']" in error for error in errors), f"{errors}")

    directory = TMP_ROOT / "count_p3_na"
    formal = _formal_rows()
    for row in formal:
        if row["blind_id"] == _blind_id(20):
            row["P3"] = "N/A"
    _write(directory, "formal", cr.FORMAL_HEADER, formal)
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("strict 检出 P3 记 N/A 的行数偏离冻结口径", _expect(errors, "P3 记 N/A 的行数 26 与冻结口径 24 不一致") == "")

    directory = TMP_ROOT / "count_s3"
    formal = _formal_rows()
    for row in formal:
        if row["blind_id"] == _blind_id(40):
            row["S3_flag"] = "1"
    _write(directory, "formal", cr.FORMAL_HEADER, formal)
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, _supplement_rows())
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("strict 检出 S3_flag 已判行数偏离冻结口径", _expect(errors, "S3_flag 已判行数 50 与冻结口径 48 不一致") == "")

    directory = TMP_ROOT / "count_e7"
    supplement = _supplement_rows()
    for row in supplement:
        if row["blind_id"] == _blind_id(220):
            row["E7"] = "1"
    _write(directory, "formal", cr.FORMAL_HEADER, _formal_rows())
    _write(directory, "calibration", cr.FORMAL_HEADER, _calibration_rows())
    _write(directory, "supplementary", cr.SUPPLEMENT_HEADER, supplement)
    errors = cr.collect_errors(directory, strict=True, allow_empty=False)
    check("strict 检出 E7 已判行数偏离冻结口径", _expect(errors, "E7 已判行数 32 与冻结口径 30 不一致") == "")


def test_missing_files_and_failure_code() -> None:
    directory = TMP_ROOT / "only_formal"
    _write(directory, "formal", cr.FORMAL_HEADER, _formal_rows())
    errors = cr.collect_errors(directory, strict=False, allow_empty=True)
    check("缺文件时报错而不是静默通过", len(errors) == 2, f"{errors}")
    check("缺文件的退出码为 1", cr.main(["--dir", str(directory), "--allow-empty"]) == 1, "")

    missing_dir = TMP_ROOT / "no_such_dir"
    check("目录不存在时报错", cr.collect_errors(missing_dir, strict=False, allow_empty=True) != [], "")


def test_header_text_matches_design() -> None:
    check(
        "正式表头与 v4.5 §7.2 逐字一致",
        cr.FORMAL_HEADER
        == ["blind_id", "rater_id", "phase", "P1", "P2", "P3", "P4", "S2", "S3_flag", "S4_verdict", "notes"],
        f"{cr.FORMAL_HEADER}",
    )
    check(
        "补充表头与 v4.5 §7.2 逐字一致",
        cr.SUPPLEMENT_HEADER == ["blind_id", "rater_id", "phase", "E1", "E7", "notes"],
        f"{cr.SUPPLEMENT_HEADER}",
    )
    check(
        "不适用矩阵计数冻结为 84/24/48/24 与 23/30",
        cr.STRICT_COUNTS["formal"] == {"blind_ids": 84, "rows": 168, "P3_na_rows": 24, "S3_flag_scored_rows": 48, "S4_verdict_scored_rows": 24}
        and cr.STRICT_COUNTS["supplementary"] == {"blind_ids": 23, "rows": 46, "E7_scored_rows": 30},
        f"{cr.STRICT_COUNTS}",
    )


def main() -> int:
    try:
        for test in (
            test_empty_templates,
            test_valid_strict,
            test_header_and_identity_columns,
            test_notes_identity_leak,
            test_invalid_ids_and_phase,
            test_duplicate_rows,
            test_value_rules,
            test_blank_is_not_na,
            test_applicability_consistency,
            test_strict_counts,
            test_missing_files_and_failure_code,
            test_header_text_matches_design,
        ):
            try:
                test()
            except Exception:  # noqa: BLE001 - 单条用例异常也要继续跑完并计入失败
                FAILED.append(f"{test.__name__} :: {traceback.format_exc()}")
                print(f"FAIL {test.__name__} :: 抛出异常\n{traceback.format_exc()}")
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  FAIL {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
