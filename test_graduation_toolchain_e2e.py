"""工具链整合测试：盲评包 → 评分表 → 统计分析的契约与端到端数据流。

这里的用例只用合成数据与临时目录：临时 Git 仓库来自 test_graduation_blind_pack 的
Sandbox 构造器，评分表由 mapping.csv 反推生成，统计产出落在临时目录里。
不断言真实 experiments/graduation/runs、feedback.db、blind_private 的任何内容，
只在开始与结束时各取一次指纹，确认它们没有被本测试改动。

为什么需要这组测试：
- 三个工具的常量（表头、mapping 列、blind_id 口径）过去各自维护，改一个不会提醒另两个；
- check_ratings 的 --strict 只能数行数与 N/A 行数，把 S7 的 P3=N/A 与非 S7 的分数
  互换后总数不变，只有对着 mapping.csv 的样本身份逐项比对才会暴露；
- 打包 → 评分 → 统计这条链此前没有一条真正跑通的用例，格式漂移只能等到正式交付才发现。
"""
from __future__ import annotations

import contextlib
import csv
import io
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "experiments" / "graduation"))

import analyze_stats as stats  # noqa: E402
import check_ratings as ratings  # noqa: E402
import make_blind_pack as pack  # noqa: E402
import test_graduation_blind_pack as harness  # noqa: E402


# ---------------------------------------------------------------------------
# 合成评分表：完全按 mapping.csv 的样本身份生成，N/A 位置由冻结矩阵决定
# ---------------------------------------------------------------------------
def _default_value(item: str) -> str:
    if item in stats.LIKERT_ITEMS:
        return "3"
    if item in stats.BINARY_ITEMS:
        return "0"
    if item in stats.VERDICT_ITEMS:
        return "full"
    if item == "E1":
        return "1"
    if item == "E7":
        return "1"
    raise AssertionError(f"未覆盖的评分维度：{item}")


def _write_rows(path: Path, header, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(header), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _read_rows(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def _build_ratings(by_id: dict[str, dict], directory: Path) -> Path:
    """按 mapping 生成三份原始评分表 + 空裁决表。"""
    formal: list[dict] = []
    calibration: list[dict] = []
    supplement: list[dict] = []
    for blind_id, meta in by_id.items():
        phase = meta["phase"]
        sample = meta["sample_id"]
        if phase == "supplementary":
            e7_applicable = stats._supplement_item_applicable("E7", meta)
            for rater in stats.RATER_IDS:
                supplement.append(
                    {
                        "blind_id": blind_id,
                        "rater_id": rater,
                        "phase": phase,
                        "E1": "1",
                        "E7": "1" if e7_applicable else "N/A",
                        "notes": "",
                    }
                )
            continue
        for rater in stats.RATER_IDS:
            row = {"blind_id": blind_id, "rater_id": rater, "phase": phase, "notes": ""}
            for item in stats.LIKERT_ITEMS + stats.BINARY_ITEMS + stats.VERDICT_ITEMS:
                row[item] = _default_value(item) if stats._item_applicable(item, sample) else "N/A"
            (formal if phase == "formal" else calibration).append(row)
    _write_rows(directory / "ratings_formal_raw.csv", stats.FORMAL_RAW_COLUMNS, formal)
    _write_rows(directory / "ratings_calibration_raw.csv", stats.FORMAL_RAW_COLUMNS, calibration)
    _write_rows(directory / "ratings_supplement_raw.csv", stats.SUPPLEMENT_RAW_COLUMNS, supplement)
    _write_rows(directory / "ratings_adjudicated.csv", stats.ADJUDICATED_COLUMNS, [])
    return directory


def _run_ratings(directory: Path, *extra: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ratings.main(["--dir", str(directory), *extra])
    return code, out.getvalue() + err.getvalue()


def _run_stats(directory: Path, mapping: Path, out_dir: Path, *extra: str):
    out, err = io.StringIO(), io.StringIO()
    argv = [
        "--formal-raw", str(directory / "ratings_formal_raw.csv"),
        "--calibration-raw", str(directory / "ratings_calibration_raw.csv"),
        "--supplement-raw", str(directory / "ratings_supplement_raw.csv"),
        "--adjudicated", str(directory / "ratings_adjudicated.csv"),
        "--mapping", str(mapping),
        "--out-dir", str(out_dir),
        "--bootstrap", "200",
        *extra,
    ]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = stats.main(argv)
        except stats.StatsError as exc:
            # 与 __main__ 入口一致：打印 FAIL 码与明细，退出码 2
            print(f"FAIL [{exc.code}] {exc.detail}", file=sys.stderr)
            for problem in exc.problems[:40]:
                print(f"  - {problem}", file=sys.stderr)
            code = 2
    return code, out.getvalue() + err.getvalue()


@contextlib.contextmanager
def _case(name: str):
    """一个用例一个临时仓库；无论断言是否失败都在退出时删掉。"""
    sandbox = harness._make_sandbox(name)
    try:
        yield sandbox
    finally:
        leftovers = harness._force_rmtree(sandbox.root)
        assert not leftovers, f"临时目录未清理干净：{leftovers}"


def _packed(sandbox) -> tuple[Path, dict[str, dict]]:
    output = harness._run_pack(sandbox)
    mapping_path = sandbox.private_dir / "mapping.csv"
    assert mapping_path.is_file(), f"打包未产出 mapping.csv：{output[-400:]}"
    return mapping_path, stats.load_mapping(mapping_path)


def _find_formal_pair(by_id: dict[str, dict]) -> tuple[str, str]:
    """挑一份 S7 正式材料（P3 不适用）和一份 P3 适用的正式材料。"""
    s7 = next(bid for bid, meta in by_id.items() if meta["phase"] == "formal" and meta["sample_id"] == "S7")
    other = next(
        bid
        for bid, meta in by_id.items()
        if meta["phase"] == "formal" and meta["sample_id"] != "S7"
    )
    return s7, other


# ---------------------------------------------------------------------------
# T1 三个工具的契约常量必须一致
# ---------------------------------------------------------------------------
def test_c01_shared_constants_match():
    assert tuple(pack.MAPPING_COLUMNS) == tuple(stats.MAPPING_COLUMNS), (
        "make_blind_pack 与 analyze_stats 的 mapping 列不一致："
        f"{pack.MAPPING_COLUMNS} vs {stats.MAPPING_COLUMNS}"
    )
    formal_header = ratings.PHASE_FILES["formal"][1]
    supplement_header = ratings.PHASE_FILES["supplementary"][1]
    assert list(formal_header) == list(stats.FORMAL_RAW_COLUMNS), (
        f"正式/校准表头不一致：{formal_header} vs {stats.FORMAL_RAW_COLUMNS}"
    )
    assert list(supplement_header) == list(stats.SUPPLEMENT_RAW_COLUMNS), (
        f"补充表头不一致：{supplement_header} vs {stats.SUPPLEMENT_RAW_COLUMNS}"
    )
    for name in ("BATCH_MAIN", "BATCH_SUBJ", "BATCH_S7EMPTY", "BATCH_B0"):
        assert getattr(pack, name) == getattr(stats, name), f"{name} 常量不一致"
    # 裁决表只有 analyze_stats 与模板两处来源（C03 校验模板），打包器不参与，
    # 因此这里只核对 tools 与 mapping 的共享契约，不再对 pack 提要求。


def test_c02_blind_id_regex_same_verdict():
    """同一个 blind_id 不能被一个工具放行、另一个工具拒绝。"""
    samples = [
        "0123456789abcdef0123456789abcdef",  # 32 位 hex，pack 的生成形态
        "123e4567-e89b-42d3-a456-426614174000",  # UUIDv4
        "123e4567-e89b-12d3-a456-426614174000",  # UUIDv1：都不是本项目的形态
        "0123456789ABCDEF0123456789ABCDEF",  # 大写
        "0123456789abcdef0123456789abcde",  # 31 位
        "123e4567e89b42d3a456426614174000",  # 无连字符
    ]
    for blind_id in samples:
        mine = bool(ratings.BLIND_ID_RE.match(blind_id))
        theirs = bool(stats.BLIND_ID_RE.match(blind_id))
        assert mine == theirs, f"{blind_id} 在 check_ratings={mine} 与 analyze_stats={theirs} 结论不同"
    assert ratings.BLIND_ID_RE.match(samples[0]) and ratings.BLIND_ID_RE.match(samples[1])
    assert not ratings.BLIND_ID_RE.match(samples[2]), "UUIDv1 不应通过"


def test_c03_adjudicated_template_matches_schema():
    template = ROOT / "experiments" / "graduation" / "blind_ratings" / "ratings_adjudicated.csv"
    assert template.is_file(), "缺少裁决表模板 blind_ratings/ratings_adjudicated.csv"
    header, rows = _read_rows(template)
    assert list(header) == list(stats.ADJUDICATED_COLUMNS), f"模板列与 ADJUDICATED_COLUMNS 不一致：{header}"
    assert rows == [], "模板只应有表头，不应预填裁决记录"


# ---------------------------------------------------------------------------
# T2 端到端：打包 → 评分表 → 校验 → 统计
# ---------------------------------------------------------------------------
def test_e01_pack_to_stats_end_to_end():
    with _case("e2e_happy") as sandbox:
        mapping_path, by_id = _packed(sandbox)
        assert len(by_id) == 113, f"mapping 应有 113 份材料，实际 {len(by_id)}"
        directory = _build_ratings(by_id, sandbox.root / "ratings")

        code, output = _run_ratings(directory, "--strict", "--mapping", str(mapping_path))
        assert code == 0, f"check_ratings 应通过，实际 exit={code}\n{output[-1500:]}"
        assert "[PASS] mapping" in output, f"缺少 mapping 检查行：{output[-800:]}"

        out_dir = sandbox.root / "stats-out"
        code, output = _run_stats(directory, mapping_path, out_dir)
        assert code == 0, f"analyze_stats 应通过，实际 exit={code}\n{output[-1500:]}"
        produced = sorted(path.name for path in out_dir.glob("*") if path.is_file())
        expected = {
            "analysis_report.md",
            "binary_rates.csv",
            "binary_tests.csv",
            "cell_aggregates.csv",
            "descriptive_statistics.csv",
            "friedman_results.csv",
            "h5_results.csv",
            "holm_adjusted_results.csv",
            "input_hashes.csv",
            "inter_rater_agreement.csv",
            "primary_hypothesis_tests.csv",
            "ratings_calibration_final.csv",
            "ratings_final.csv",
            "ratings_final_detail.csv",
            "ratings_supplement_final.csv",
            "ratings_supplement_final_detail.csv",
            "y2_calls_distribution.csv",
        }
        assert set(produced) == expected, f"统计产出与契约不符，多出={set(produced) - expected}，缺少={expected - set(produced)}"


# ---------------------------------------------------------------------------
# T3 --strict 的盲区：P3 的 N/A 与非 S7 分数互换后总数不变
# ---------------------------------------------------------------------------
def _swap_p3(directory: Path, s7_id: str, other_id: str) -> None:
    path = directory / "ratings_formal_raw.csv"
    header, rows = _read_rows(path)
    for row in rows:
        if row["blind_id"] == s7_id:
            row["P3"] = "3"  # 不应适用的材料被打了分
        elif row["blind_id"] == other_id:
            row["P3"] = "N/A"  # 应适用的材料被记成不适用
    _write_rows(path, header, rows)


def test_e02_strict_misses_swapped_na_but_mapping_catches():
    with _case("e2e_swap_p3") as sandbox:
        mapping_path, by_id = _packed(sandbox)
        directory = _build_ratings(by_id, sandbox.root / "ratings")
        s7_id, other_id = _find_formal_pair(by_id)

        baseline, output = _run_ratings(directory, "--strict")
        assert baseline == 0, f"基线评分表应通过 --strict，实际 exit={baseline}\n{output[-800:]}"

        _swap_p3(directory, s7_id, other_id)

        strict_code, strict_output = _run_ratings(directory, "--strict")
        assert strict_code == 0, (
            "本用例要证明 --strict 单独看不出 P3 互换；若这里失败说明行数口径变了，需重写用例\n"
            f"{strict_output[-1200:]}"
        )

        code, output = _run_ratings(directory, "--strict", "--mapping", str(mapping_path))
        assert code == 1, f"--mapping 应拦住 P3 互换，实际 exit={code}\n{output[-1200:]}"
        assert any(f"blind_id={s7_id}" in line and "P3" in line for line in output.splitlines()), (
            f"S7 材料打了 P3 分未被指出：{output[-1500:]}"
        )
        assert any(f"blind_id={other_id}" in line and "P3" in line for line in output.splitlines()), (
            f"应适用的 P3 被记 N/A 未被指出：{output[-1500:]}"
        )
        assert "[FAIL] mapping" in output, f"mapping 检查行应为 FAIL：{output[-800:]}"

        out_dir = sandbox.root / "stats-out"
        stats_code, stats_output = _run_stats(directory, mapping_path, out_dir)
        assert stats_code != 0, "analyze_stats 不应接受 P3 互换后的评分表"
        assert not out_dir.exists() or not any(out_dir.glob("*.csv")), "失败时不应留下统计产出"
        assert "N/A" in stats_output or "不适用" in stats_output, f"错误信息未说明 N/A 冲突：{stats_output[-1200:]}"


# ---------------------------------------------------------------------------
# T4 校准材料混进正式表
# ---------------------------------------------------------------------------
def test_e03_calibration_rows_in_formal_are_rejected():
    with _case("e2e_calibration_mix") as sandbox:
        mapping_path, by_id = _packed(sandbox)
        directory = _build_ratings(by_id, sandbox.root / "ratings")
        calibration_id = next(bid for bid, meta in by_id.items() if meta["phase"] == "calibration")
        _, formal_rows = _read_rows(directory / "ratings_formal_raw.csv")
        for rater in stats.RATER_IDS:
            extra = {
                "blind_id": calibration_id,
                "rater_id": rater,
                "phase": "formal",
                "notes": "",
            }
            for item in stats.LIKERT_ITEMS + stats.BINARY_ITEMS + stats.VERDICT_ITEMS:
                extra[item] = _default_value(item)
            formal_rows.append(extra)
        _write_rows(directory / "ratings_formal_raw.csv", stats.FORMAL_RAW_COLUMNS, formal_rows)

        code, output = _run_ratings(directory, "--mapping", str(mapping_path))
        assert code == 1, f"校准材料混进正式表应被拦住，实际 exit={code}\n{output[-1200:]}"
        assert calibration_id in output and "calibration" in output, f"错误信息未指出归属冲突：{output[-1500:]}"


# ---------------------------------------------------------------------------
# T5 mapping 本身不可用时单独报错，而不是崩溃
# ---------------------------------------------------------------------------
def test_e04_broken_mapping_reports_mapping_error():
    with _case("e2e_broken_mapping") as sandbox:
        mapping_path, by_id = _packed(sandbox)
        directory = _build_ratings(by_id, sandbox.root / "ratings")
        broken = sandbox.root / "broken-mapping.csv"
        broken.write_text("blind_id,phase\n", encoding="utf-8")

        code, output = _run_ratings(directory, "--mapping", str(broken))
        assert code == 1, f"坏 mapping 应失败，实际 exit={code}\n{output[-1200:]}"
        assert "mapping.csv 不可用" in output, f"未说明 mapping 不可用：{output[-1200:]}"
        assert "[FAIL] mapping" in output, f"mapping 检查行应为 FAIL：{output[-800:]}"


# ---------------------------------------------------------------------------
# T6 dry-run 不落盘
# ---------------------------------------------------------------------------
def test_e05_dry_run_writes_nothing():
    with _case("e2e_dry_run") as sandbox:
        code, out, err = harness._main(sandbox, ("--dry-run",))
        assert code == 0, f"dry-run 应成功，实际 exit={code}\nstdout={out[-800:]}\nstderr={err[-800:]}"
        assert not sandbox.pack_dir.exists(), "dry-run 不得创建盲评包目录"
        assert not sandbox.private_dir.exists(), "dry-run 不得创建私有目录"


TESTS = [
    ("C01", test_c01_shared_constants_match),
    ("C02", test_c02_blind_id_regex_same_verdict),
    ("C03", test_c03_adjudicated_template_matches_schema),
    ("E01", test_e01_pack_to_stats_end_to_end),
    ("E02", test_e02_strict_misses_swapped_na_but_mapping_catches),
    ("E03", test_e03_calibration_rows_in_formal_are_rejected),
    ("E04", test_e04_broken_mapping_reports_mapping_error),
    ("E05", test_e05_dry_run_writes_nothing),
]


def _run_all() -> int:
    real_before = harness._fingerprints()
    passed = 0
    failures: list[str] = []
    try:
        for name, func in TESTS:
            try:
                func()
                passed += 1
                print(f"  {name} PASS")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  {name} FAIL: {type(exc).__name__}: {exc}")
                traceback.print_exc()
        assert harness._fingerprints() == real_before, "真实 runs/feedback.db 在测试期间被改动"
    finally:
        leftovers = harness._force_rmtree(harness.TMP_ROOT)
        if leftovers:
            failures.append(f"清理失败，残留临时目录：{leftovers}")
            print(f"  清理失败：残留 {leftovers}")
    print(f"结果：{passed}/{len(TESTS)} PASS")
    if failures:
        print("失败明细：")
        for item in failures:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
