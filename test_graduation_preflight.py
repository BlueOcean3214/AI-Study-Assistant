#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`preflight_experiment_data.py` 的合成数据测试（任务 B/C，2026-11-23）。

原则：只用 tempfile 临时目录 + 合成记录 + 合成清单，绝不读写正式实验数据
（`experiments/graduation/runs`、`feedback.db`、`blind_private/`、`blind_pack/`）。
每个用例结束（即使断言失败）都删掉自己的临时仓库。

覆盖：正常通过、清单缺失、文件缺失、哈希被改、JSONL 损坏、重复身份、覆盖不符、
计划被篡改、清单大小不符、legacy B0 缺 plan_sha256、只读无副作用、失败不产出统计。
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRADUATION = HERE / "experiments" / "graduation"
if str(GRADUATION) not in sys.path:
    sys.path.insert(0, str(GRADUATION))

import plan_schedule as ps          # noqa: E402
import preflight_experiment_data as pf   # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(prefix="grad_preflight_"))
CREATED = "2026-11-13T06:10:51+00:00"
CONFIG_SHA = "c" * 64
MAIN_SAMPLES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------


def _sha_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def _sha(path):
    return _sha_bytes(Path(path).read_bytes())


def _result_row(*, experiment_id, plan_sha, batch, sample_id, group,
                repeat_index, plan_seq, kind="main"):
    return {
        "run_id": f"{experiment_id}-{sample_id}-{group}-r{repeat_index}",
        "experiment_id": experiment_id,
        "sample_id": sample_id,
        "condition": group,
        "group": group,
        "repeat_index": repeat_index,
        "round_index": repeat_index,
        "plan_seq": plan_seq,
        "seed": 20261113,
        "scheme": ps.DEFAULT_SCHEME,
        "algorithm_version": ps.DEFAULT_ALGORITHM_VERSION,
        "plan_sha256": plan_sha,
        "batch": batch,
        "model": {"provider": "internal", "name": "test-model",
                  "temperature": 0.2, "top_p": 0.95},
        "base_sha": "b" * 40,
        "prompt_sha256": _sha_bytes(
            f"{sample_id}|{group}|{repeat_index}".encode("utf-8")),
        "messages_path": f"messages/{sample_id}-{group}-r{repeat_index}.json",
        "knowledge_result_ids": [],
        "memory_injected": [],
        "short_term_injected": group != "A",
        "difficult_flag": False,
        "output_raw": "{}",
        "answer": f"合成答案 {sample_id}/{group}/r{repeat_index}",
        "plan": {"title": "合成计划"},
        "validation": {"json_ok": True, "plan_valid": True,
                       "grounding_ok": True, "corrections_used": 0},
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "latency_ms": 100,
        "error": None,
        "data_kind": pf.DESIGN_DATA_KIND,
    }


def _write_jsonl(path, rows):
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def _write_plan(path, plan):
    Path(path).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="\n")


def _build_plan(experiment_id, sample_groups, *, rounds=5):
    return ps.build_schedule(
        experiment_id=experiment_id,
        sample_groups=sample_groups,
        rounds=rounds,
        seed=ps.DEFAULT_SEED,
        scheme=ps.DEFAULT_SCHEME,
        algorithm_version=ps.DEFAULT_ALGORITHM_VERSION,
        config_hash=CONFIG_SHA,
        created_at=CREATED,
    )


def _rows_for_plan(plan, *, batch, experiment_id):
    rows = []
    for block in plan["blocks"]:
        sample_id = block["sample_id"]
        for task in block["tasks"]:
            rows.append(_result_row(
                experiment_id=experiment_id,
                plan_sha=plan["plan_sha256"],
                batch=batch,
                sample_id=sample_id,
                group=task["group"],
                repeat_index=task["repeat_index"],
                plan_seq=task["plan_seq"],
                kind=task["kind"],
            ))
    return rows


def _legacy_b0_rows():
    rows = []
    plan_seq = 0
    for sample_id in ("S5", "S7"):
        for repeat in range(5):
            rows.append(_result_row(
                experiment_id="exp-b0-legacy",
                plan_sha="",          # legacy：v4.2 之前没有 plan.json
                batch="b0-legacy",
                sample_id=sample_id,
                group="B0",
                repeat_index=repeat,
                plan_seq=plan_seq,
            ))
            plan_seq += 1
    return rows


def build_dataset(root, *, include_smoke=False, include_b0=True):
    """在 root 下造一份合成仓库（runs/ + 清单），返回 (runs_dir, manifest_path)。"""

    root = Path(root)
    runs = root / "experiments" / "graduation" / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    specs = (
        ("main-20261113T061051Z", "main", "exp-main",
         {sample: ["A", "B", "C", "D"] for sample in MAIN_SAMPLES}, 5),
        ("suppl-subj-20261113T062711Z", "suppl-subj", "exp-suppl-subj",
         {"S5": ["C-subj", "D-subj"]}, 5),
        ("suppl-s7empty-20261113T062822Z", "suppl-s7empty", "exp-suppl-s7empty",
         {"S7-empty": ["D"]}, 5),
    )
    if include_smoke:
        specs = specs + (("internal-api-smoke-20261113T061019Z", "smoke",
                          "exp-smoke", {"S5": ["A"]}, 1),)

    entries = []
    for dirname, batch, experiment_id, sample_groups, rounds in specs:
        directory = runs / dirname
        directory.mkdir(parents=True, exist_ok=True)
        plan = _build_plan(experiment_id, sample_groups, rounds=rounds)
        plan_path = directory / "plan.json"
        _write_plan(plan_path, plan)
        rows = _rows_for_plan(plan, batch=batch, experiment_id=experiment_id)
        results_path = directory / "results.jsonl"
        _write_jsonl(results_path, rows)
        for path, records in ((plan_path, len(rows)), (results_path, len(rows))):
            entries.append((batch, path, records))

    if include_b0:
        directory = runs / "b0-remainder-20261113T013105Z"
        directory.mkdir(parents=True, exist_ok=True)
        rows = _legacy_b0_rows()
        results_path = directory / "results.jsonl"
        _write_jsonl(results_path, rows)
        # B0 legacy 不在冻结清单里（设计 §1.3 / §2.4），只做记录，不写 manifest。

    manifest = root / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"
    write_manifest(root, runs, entries, manifest=manifest)
    return runs, manifest


def write_manifest(root, runs, entries, *, manifest):
    """按磁盘当前内容重写清单（用于隔离某一项检查）。"""

    root = Path(root)
    manifest = Path(manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["batch", "batch_desc", "relative_path", "file",
                         "size_bytes", "sha256", "records"])
        for batch, path, records in entries:
            path = Path(path)
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            writer.writerow([batch, f"合成数据（{batch}）", relative, path.name,
                             path.stat().st_size, _sha(path), records])
    return manifest


def manifest_entries(root, runs):
    """把 runs 下（除 B0）所有 plan.json/results.jsonl 收进清单条目。"""

    entries = []
    for directory in sorted(Path(runs).iterdir()):
        if not directory.is_dir() or directory.name.startswith("b0-"):
            continue
        batch = "smoke" if directory.name.startswith("internal-api-smoke-") else (
            "main" if directory.name.startswith("main-") else (
                "suppl-subj" if directory.name.startswith("suppl-subj-")
                else "suppl-s7empty"))
        for name in ("plan.json", "results.jsonl"):
            path = directory / name
            if not path.is_file():
                continue
            records = (len(json.loads(path.read_text(encoding="utf-8")).get("blocks", []))
                       if name == "plan.json" else None)
            if name == "plan.json":
                plan = json.loads(path.read_text(encoding="utf-8"))
                records = len(ps.plan_tasks(plan))
            else:
                records = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                              if line.strip())
            entries.append((batch, path, records))
    return entries


def refresh_manifest(root, runs):
    return write_manifest(root, runs, manifest_entries(root, runs),
                          manifest=Path(root) / "docs" /
                          "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv")


# ---------------------------------------------------------------------------
# 运行助手
# ---------------------------------------------------------------------------


def run_preflight(repo_root, runs_dir, manifest, *, extra=()):
    """走真实 CLI：捕获 stdout，返回 (退出码, 报告文本)。"""

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = pf.main([
            "--repo-root", str(repo_root),
            "--runs-dir", str(runs_dir),
            "--manifest", str(manifest),
            *extra,
        ])
    return code, buffer.getvalue()


def assert_contains(report, *needles):
    for needle in needles:
        assert needle in report, f"报告里缺少 {needle!r}：\n{report}"


def assert_fail(report, area_keyword):
    for line in report.splitlines():
        if line.startswith("[FAIL]") and area_keyword in line:
            return
    raise AssertionError(f"报告里没有 {area_keyword!r} 的 FAIL：\n{report}")


def assert_no_fail(report):
    assert "[FAIL]" not in report, f"不应出现 FAIL：\n{report}"


def fingerprint_tree(root):
    """runs 目录下所有文件的 SHA-256 + 大小，用于「运行前后完全一致」。"""

    root = Path(root)
    print_snapshot = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            print_snapshot[str(path.relative_to(root))] = (
                path.stat().st_size, _sha(path))
    return print_snapshot


@contextlib.contextmanager
def _case(name):
    root = Path(tempfile.mkdtemp(prefix=f"grad_preflight_{name}_", dir=TMP_ROOT))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        assert not root.exists(), f"临时目录未清理：{root}"


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_clean_dataset_passes():
    """正常合成仓库：无 FAIL，生成任务总数 165，smoke 不计入 165。"""

    with _case("clean") as root:
        runs, manifest = build_dataset(root, include_smoke=True, include_b0=True)
        before = fingerprint_tree(runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 0, f"退出码应为 0，实际 {code}：\n{report}"
        assert_no_fail(report)
        assert_contains(report, "生成任务总数", "165", "PASS")
        assert "[FAIL] 汇总" not in report
        # smoke 单列，不计入 §2.4 的 165
        assert "smoke=1" in report, f"smoke 应单独列出：\n{report}"
        assert_contains(report, "只读自检")
        assert fingerprint_tree(runs) == before, "预检改动了实验数据"


def test_missing_results_file_fails():
    with _case("missing") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "suppl-s7empty-20261113T062822Z" / "results.jsonl"
        target.unlink()
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert_fail(report, "suppl-s7empty-20261113T062822Z/results.jsonl")


def test_manifest_missing_fails():
    with _case("nomanifest") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        manifest.unlink()
        code, report = run_preflight(root, runs, manifest)
        assert code == 1
        assert_fail(report, "清单")


def test_tampered_results_hash_fails():
    with _case("hash") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(" \n")
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert_contains(report, "文件 SHA-256 与清单不符")


def test_corrupt_jsonl_fails():
    with _case("corrupt") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        lines = target.read_text(encoding="utf-8").splitlines()
        lines[3] = "{这不是 JSON"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        # 重新写清单，把 JSONL 损坏与「清单哈希不符」隔离开
        refresh_manifest(root, runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert_contains(report, "合法 JSON")


def test_duplicate_identity_fails():
    with _case("duplicate") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        lines = target.read_text(encoding="utf-8").splitlines()
        lines.append(lines[0])
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        refresh_manifest(root, runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1
        assert_contains(report, "四元组唯一")


def test_coverage_gap_fails():
    with _case("coverage") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        lines = target.read_text(encoding="utf-8").splitlines()
        dropped = lines.pop(7)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        refresh_manifest(root, runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert_contains(report, "批次 main")
        assert "缺 1" in report, f"应报缺 1 个身份（被删的行：{dropped[:60]}）：\n{report}"


def test_tampered_plan_fails():
    with _case("plan") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        plan_path = runs / "main-20261113T061051Z" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["seed"] = 20260101
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8", newline="\n")
        refresh_manifest(root, runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1
        assert_contains(report, "plan_sha256")


def test_manifest_size_mismatch_fails():
    with _case("size") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        rows = list(csv.DictReader(manifest.open(encoding="utf-8", newline="")))
        for row in rows:
            if row["relative_path"].endswith("main-20261113T061051Z/results.jsonl"):
                row["size_bytes"] = str(int(row["size_bytes"]) + 1)
        with manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1
        assert_contains(report, "文件大小与清单不符")


def test_legacy_b0_missing_plan_sha_is_warning():
    """B0 legacy 目录没有 plan.json、记录缺 plan_sha256：只报 WARNING，不算 FAIL。"""

    with _case("legacy") as root:
        runs, manifest = build_dataset(root, include_b0=True)
        code, report = run_preflight(root, runs, manifest)
        assert code == 0, f"退出码应为 0，实际 {code}：\n{report}"
        assert_no_fail(report)
        assert_contains(report, "b0-legacy", "WARNING")
        assert "10 条记录" in report or "b0-legacy=10" in report


def test_runtime_error_does_not_emit_statistics():
    """预检失败时只写报告，不产出任何统计文件。"""

    with _case("noside") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        (runs / "main-20261113T061051Z" / "results.jsonl").unlink()
        json_path = root / "报告.json"
        code, report = run_preflight(root, runs, manifest, extra=("--json", str(json_path)))
        assert code == 1
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["counts"]["fail"] >= 1
        produced = [path.name for path in Path(root).rglob("*")
                    if path.is_file() and path.suffix in (".csv", ".md")]
        assert produced == ["GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"] or \
            set(produced) == {"GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"}, produced


def test_readonly_mode_leaves_inputs_untouched():
    """只读：runs 下文件哈希、大小、数量在运行前后完全一致。"""

    with _case("readonly") as root:
        runs, manifest = build_dataset(root, include_smoke=False, include_b0=True)
        before = fingerprint_tree(runs)
        before_manifest = _sha(manifest)
        code, _ = run_preflight(root, runs, manifest)
        assert code == 0
        assert fingerprint_tree(runs) == before
        assert _sha(manifest) == before_manifest


def test_preflight_records_feed_analyze_stats():
    """跨工具契约：预检接受的 results.jsonl 能被 analyze_stats 的哈希核验接受；
    之后改动文件，analyze_stats 必须 fail-closed（results-file-hash-mismatch）。"""

    import analyze_stats as A  # 延迟导入：只有这个用例需要统计工具的核验逻辑

    with _case("cross") as root:
        runs, manifest = build_dataset(root, include_b0=True)
        code, report = run_preflight(root, runs, manifest)
        assert code == 0, f"合成数据应先通过预检：{report}"

        results_path = runs / "main-20261113T061051Z" / "results.jsonl"
        first = json.loads(results_path.read_text(encoding="utf-8").splitlines()[0])
        blind_id = "a" * 32
        # analyze_stats 现在逐项比对 mapping 与源记录的身份字段，
        # 因此这里的 meta 必须与第一条记录完全一致（含行号 1）。
        meta = {
            "blind_id": blind_id,
            "phase": "formal",
            "batch": "main",
            "source_run_dir": results_path.parent.name,
            "source_file": "results.jsonl",
            "source_results_file_sha256": _sha(results_path),
            "source_result_row_sha256": A.row_sha256(first),
            "source_prompt_sha256": first["prompt_sha256"],
            "prompt_sha256": first["prompt_sha256"],
            "experiment_id": first["experiment_id"],
            "sample_id": first["sample_id"],
            "group": first["group"],
            "condition": first["condition"],
            "repeat_index": str(first["repeat_index"]),
            "round_index": str(first["round_index"]),
            "source_row_number": "1",
        }
        index, file_hashes = A.load_results([results_path], {blind_id: meta})
        assert file_hashes == {str(results_path): _sha(results_path)}
        assert len(index) == 1

        with results_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(" ")
        try:
            A.load_results([results_path], {blind_id: meta})
        except A.StatsError as error:
            assert error.code == "results-file-hash-mismatch", error.code
        else:
            raise AssertionError("改动结果文件后 analyze_stats 必须拒绝")

def test_manifest_missing_results_entry_fails():
    """清单只列了 plan.json、漏列 results.jsonl：必须按文件报 FAIL。"""

    with _case("manifest_gap") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        entries = [entry for entry in manifest_entries(root, runs)
                   if Path(entry[1]) != target]
        write_manifest(root, runs, entries, manifest=manifest)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert_fail(report, "main-20261113T061051Z/results.jsonl")
        assert_contains(report, "未纳入")
        # 同目录的 plan.json 仍应判 PASS：文件级判定，不是目录级
        assert_contains(report, "main-20261113T061051Z/plan.json")


def test_nonfirst_batch_tamper_fails():
    """只改非首行的 batch：不能被「第一条记录推断批次」蒙混过去。"""

    with _case("batch") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        lines = target.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        assert first["batch"] == "main", first["batch"]
        second = json.loads(lines[1])
        second["batch"] = "suppl-subj"
        lines[1] = json.dumps(second, ensure_ascii=False, sort_keys=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        refresh_manifest(root, runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert_fail(report, "batch")
        assert_contains(report, "suppl-subj")


def test_malformed_field_types_report_fail():
    """batch/sample_id/repeat_index 被写成 list/dict/null：结构化 FAIL，不崩。"""

    with _case("types") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        target = runs / "main-20261113T061051Z" / "results.jsonl"
        lines = target.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["batch"] = ["main"]
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
        second = json.loads(lines[1])
        second["sample_id"] = {"x": 1}
        lines[1] = json.dumps(second, ensure_ascii=False, sort_keys=True)
        third = json.loads(lines[2])
        third["repeat_index"] = None
        lines[2] = json.dumps(third, ensure_ascii=False, sort_keys=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        refresh_manifest(root, runs)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1, f"退出码应为 1，实际 {code}：\n{report}"
        assert "Traceback" not in report, report
        assert_fail(report, "batch")
        assert_fail(report, "sample_id")
        assert_fail(report, "repeat_index")


def test_manifest_mismatch_never_reports_pass():
    """同一文件出现大小不符时，不能再有该文件的 PASS 行。"""

    with _case("nopass") as root:
        runs, manifest = build_dataset(root, include_b0=False)
        rows = list(csv.DictReader(manifest.open(encoding="utf-8", newline="")))
        for row in rows:
            if row["relative_path"].endswith("main-20261113T061051Z/results.jsonl"):
                row["size_bytes"] = str(int(row["size_bytes"]) + 1)
        with manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        code, report = run_preflight(root, runs, manifest)
        assert code == 1
        assert_fail(report, "main-20261113T061051Z/results.jsonl")
        for line in report.splitlines():
            if "main-20261113T061051Z/results.jsonl" in line:
                assert not line.startswith("[PASS]"), line


def test_fingerprint_covers_manifest_and_legacy():
    """指纹范围含清单自身与 B0 legacy 的 results.jsonl。"""

    with _case("fingerprint") as root:
        runs, manifest = build_dataset(root, include_b0=True)
        paths = {str(path) for path in pf.collect_fingerprint_paths(runs, manifest)}
        assert str(manifest.resolve()) in paths, paths
        b0 = runs / "b0-remainder-20261113T013105Z" / "results.jsonl"
        assert str(b0.resolve()) in paths, paths
        code, report = run_preflight(root, runs, manifest)
        assert code == 0, report
        assert_contains(report, "只读自检")


TESTS = (
    test_clean_dataset_passes,
    test_missing_results_file_fails,
    test_manifest_missing_fails,
    test_tampered_results_hash_fails,
    test_corrupt_jsonl_fails,
    test_duplicate_identity_fails,
    test_coverage_gap_fails,
    test_tampered_plan_fails,
    test_manifest_size_mismatch_fails,
    test_legacy_b0_missing_plan_sha_is_warning,
    test_runtime_error_does_not_emit_statistics,
    test_readonly_mode_leaves_inputs_untouched,
    test_preflight_records_feed_analyze_stats,
    test_manifest_missing_results_entry_fails,
    test_nonfirst_batch_tamper_fails,
    test_malformed_field_types_report_fail,
    test_manifest_mismatch_never_reports_pass,
    test_fingerprint_covers_manifest_and_legacy,
)


def main():
    failures = 0
    for index, test in enumerate(TESTS, start=1):
        name = test.__name__
        try:
            test()
            print(f"[{index}/{len(TESTS)}] PASS {name}")
        except Exception:                        # noqa: BLE001 测试助手逐个报告
            failures += 1
            print(f"[{index}/{len(TESTS)}] FAIL {name}")
            traceback.print_exc()
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print(f"结果：{len(TESTS) - failures}/{len(TESTS)} PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
