#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_graduation_stats.py — analyze_stats.py 的合成数据单元测试。

所有输入都在 tempfile 临时目录里现造：不读正式实验数据、不生成正式评分表、
不联网、不需要 pytest。运行方式：

    .venv\\Scripts\\python.exe test_graduation_stats.py

覆盖点：二次加权 κ / 未加权 κ / 原始一致率、精确 Wilcoxon（平均秩、零差剔除、
全零、非零常数）、r_rb、dz、Holm（排序、逐步校正、并列最小 p、单调性、
m=7/6/5 的最小可达 p）、Bootstrap 可复现、Friedman、格内聚合、样本不混、
校准与补充排除、未裁决排除、pending 分母、mapping 缺失与重复、输入哈希不变、
输出禁覆盖、以及各类异常 fail-closed。
"""

import contextlib
import csv
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
GRADUATION_DIR = PROJECT_DIR / "experiments" / "graduation"
sys.path.insert(0, str(GRADUATION_DIR))

import analyze_stats as A  # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(prefix="grad_stats_"))
os.environ["FEEDBACK_DB_PATH"] = str(TMP_ROOT / "unused_feedback.db")

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("PASS " + name)
    else:
        FAILED.append((name, detail))
        print("FAIL " + name + (" :: " + str(detail) if detail else ""))


def close(actual, expected, tol=1e-9):
    if actual is None:
        return False
    try:
        return abs(float(actual) - float(expected)) <= tol
    except (TypeError, ValueError):
        return False


def expect_error(name, code, fn):
    """期望抛出 StatsError 且错误码一致。"""
    try:
        fn()
    except A.StatsError as exc:
        check(name, exc.code == code,
              "期望 %s，实际 %s：%s" % (code, exc.code, exc.detail))
        return exc
    except Exception as exc:  # noqa: BLE001
        check(name, False, "期望 StatsError(%s)，实际 %s：%s" % (
            code, type(exc).__name__, exc))
        return None
    check(name, False, "期望 StatsError(%s)，但没有抛错" % code)
    return None


# ---------------------------------------------------------------------------
# 合成数据：mapping.csv / 三张原始评分表 / 裁决表 / results.jsonl
# ---------------------------------------------------------------------------


def _sha(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _bid(seq):
    return _sha("blind", seq)[:32]


def _formal_spec():
    spec = []
    for sample in A.SAMPLES:
        for condition in A.CONDITIONS:
            for repeat in range(A.CELL_OUTPUTS):
                spec.append({"sample": sample, "condition": condition,
                             "repeat": repeat, "phase": "formal",
                             "batch": "main", "run_dir": "runs/formal"})
    return spec


def _calibration_spec():
    return [{"sample": sample, "condition": "A", "repeat": 0,
             "phase": "calibration", "batch": "main",
             "run_dir": "runs/calibration"}
            for sample in ("S1", "S2", "S3", "S4", "S5", "S6")]


def _supplement_spec():
    """§5.4.4 的 23 份：S7-empty×D 3 + B0-S5 5 + B0-S7 5 + C/D-subj 10。"""
    spec = []
    for repeat in range(3):
        spec.append({"sample": "S7", "condition": "D", "repeat": repeat,
                     "phase": "supplementary", "batch": "suppl-s7empty",
                     "run_dir": "runs/supp-s7empty"})
    for repeat, condition in enumerate(("A", "B", "C", "D", "A")):
        spec.append({"sample": "S5", "condition": condition, "repeat": repeat,
                     "phase": "supplementary", "batch": "b0-legacy",
                     "run_dir": "runs/b0"})
    for repeat, condition in enumerate(("A", "B", "C", "D", "A")):
        spec.append({"sample": "S7", "condition": condition, "repeat": repeat,
                     "phase": "supplementary", "batch": "b0-legacy",
                     "run_dir": "runs/b0"})
    subj = [("S1", "C"), ("S1", "D"), ("S2", "C"), ("S2", "D"), ("S3", "C"),
            ("S3", "D"), ("S4", "C"), ("S4", "D"), ("S1", "D"), ("S2", "C")]
    for repeat, (sample, condition) in enumerate(subj):
        spec.append({"sample": sample, "condition": condition, "repeat": repeat,
                     "phase": "supplementary", "batch": "suppl-subj",
                     "run_dir": "runs/suppl-subj"})
    return spec


def _default_item(sample, item):
    if item in A.LIKERT_ITEMS:
        if item == "P3" and sample == "S7":
            return "N/A"       # §5.4.7：S7 不评 P3
        return "3"
    if item == "S3_flag":
        return "1" if sample in ("S3", "S6") else "N/A"
    if item == "S4_verdict":
        return "full" if sample == "S6" else "N/A"
    raise AssertionError("未知评分项 %r" % (item,))


def _results_row(sample, condition, repeat, phase, zero_latency):
    bias = {"formal": 0, "calibration": 100, "supplementary": 1000}[phase]
    base = 10 if condition == "A" else (14 if condition == "D" else 12)
    latency = 0.0 if (zero_latency and condition == "A") else float(
        base + repeat + bias)
    tokens = (1000 if condition == "A" else (1200 if condition == "D" else 1100))
    return {
        "sample_id": sample,
        "group": "G" + sample[1:],
        "condition": condition,
        "repeat_index": repeat,
        "round_index": 1,
        "prompt_sha256": _sha("prompt", phase, sample, condition, repeat),
        "request_count": 3 + bias,
        "elapsed_seconds": latency,
        "token_usage": {"prompt_tokens": tokens + repeat + bias},
        "success": True,
    }


def _write_csv(path, columns, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in columns})
    Path(path).write_text(buffer.getvalue(), encoding="utf-8")


def make_fixture(root, overrides=None, rater_overrides=None, pending=None,
                 with_results=False, zero_latency=False, adjudications=None,
                 include_supplement=True):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    overrides = dict(overrides or {})
    rater_overrides = dict(rater_overrides or {})
    pending = set(pending or ())
    adjudications = list(adjudications or [])

    spec = _formal_spec() + _calibration_spec()
    if include_supplement:
        spec += _supplement_spec()

    mapping_rows, results_rows = [], []
    for seq, item in enumerate(spec, start=1):
        sample = item["sample"]
        condition = item["condition"]
        repeat = item["repeat"]
        prompt = _sha("prompt", item["phase"], sample, condition, repeat)
        if with_results:
            result_row = _results_row(sample, condition, repeat, item["phase"],
                                      zero_latency)
            results_rows.append(result_row)
            row_sha = A.row_sha256(result_row)
        else:
            row_sha = _sha("row", seq)
        mapping_rows.append({
            "blind_id": _bid(seq),
            "phase": item["phase"],
            "batch": item["batch"],
            "source_run_dir": item["run_dir"],
            "source_file": "results.jsonl",
            "source_row_number": str(seq),
            "experiment_id": "exp-" + sample,
            "sample_id": sample,
            "group": "G" + sample[1:],
            "condition": condition,
            "repeat_index": str(repeat),
            "round_index": "1",
            "source_prompt_sha256": prompt,
            "source_result_row_sha256": row_sha,
            "source_results_file_sha256": _sha("file", item["run_dir"]),
            "generated_utc": "2026-11-19T00:00:00Z",
            "generator_version": "test-1",
        })

    def resolve(sample, condition, repeat, item, rater):
        key = (sample, condition, repeat, item, rater)
        if key in pending:
            return ""
        if key in rater_overrides:
            return rater_overrides[key]
        if (sample, condition, item) in overrides:
            return overrides[(sample, condition, item)]
        return _default_item(sample, item)

    formal_rows, calibration_rows, supplement_rows = [], [], []
    for meta in mapping_rows:
        phase = meta["phase"]
        sample = meta["sample_id"]
        condition = meta["condition"]
        repeat = int(meta["repeat_index"])
        for rater in ("R1", "R2"):
            if phase == "supplementary":
                row = {"blind_id": meta["blind_id"], "rater_id": rater,
                       "phase": phase, "notes": ""}
                for item in A.SUPPLEMENT_ITEMS:
                    key = (sample, condition, repeat, item, rater)
                    if key in pending:
                        row[item] = ""
                    elif key in rater_overrides:
                        row[item] = rater_overrides[key]
                    elif (sample, condition, item) in overrides:
                        row[item] = overrides[(sample, condition, item)]
                    elif item == "E1":
                        row[item] = "1"
                    else:
                        applicable = (meta["batch"] == "suppl-subj" or
                                      (meta["batch"] == "b0-legacy" and sample == "S5"))
                        row[item] = "1" if applicable else "N/A"
                supplement_rows.append(row)
            else:
                row = {"blind_id": meta["blind_id"], "rater_id": rater,
                       "phase": phase, "notes": ""}
                for item in (A.LIKERT_ITEMS + A.BINARY_ITEMS + A.VERDICT_ITEMS):
                    row[item] = resolve(sample, condition, repeat, item, rater)
                if phase == "formal":
                    formal_rows.append(row)
                else:
                    calibration_rows.append(row)

    paths = {
        "mapping": root / "mapping.csv",
        "formal_raw": root / "ratings_formal_raw.csv",
        "calibration_raw": root / "ratings_calibration_raw.csv",
        "supplement_raw": root / "ratings_supplement_raw.csv",
        "adjudicated": root / "ratings_adjudicated.csv",
        "results": root / "results.jsonl",
    }
    _write_csv(paths["mapping"], A.MAPPING_COLUMNS, mapping_rows)
    _write_csv(paths["formal_raw"], A.FORMAL_RAW_COLUMNS, formal_rows)
    _write_csv(paths["calibration_raw"], A.FORMAL_RAW_COLUMNS, calibration_rows)
    _write_csv(paths["supplement_raw"], A.SUPPLEMENT_RAW_COLUMNS, supplement_rows)
    _write_csv(paths["adjudicated"], A.ADJUDICATED_COLUMNS, adjudications)
    if with_results:
        paths["results"].write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                      for row in results_rows) + "\n",
            encoding="utf-8",
        )
    return {"root": root, "paths": paths, "mapping_rows": mapping_rows,
            "spec": spec}


def blind_of(fixture, phase, sample, condition, repeat):
    for meta in fixture["mapping_rows"]:
        if (meta["phase"] == phase and meta["sample_id"] == sample and
                meta["condition"] == condition and
                int(meta["repeat_index"]) == repeat):
            return meta["blind_id"]
    raise AssertionError("找不到 blind_id：%s %s %s %s" %
                         (phase, sample, condition, repeat))


def run_main(fixture, out_dir, extra=(), bootstrap="500"):
    argv = [
        "--formal-raw", str(fixture["paths"]["formal_raw"]),
        "--calibration-raw", str(fixture["paths"]["calibration_raw"]),
        "--supplement-raw", str(fixture["paths"]["supplement_raw"]),
        "--adjudicated", str(fixture["paths"]["adjudicated"]),
        "--mapping", str(fixture["paths"]["mapping"]),
        "--out-dir", str(out_dir),
        "--bootstrap", str(bootstrap),
    ] + list(extra)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = A.main(argv)
    return code, buffer.getvalue()


def read_csv(path):
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def mutate_csv(path, mutate):
    rows = read_csv(path)
    mutate(rows)
    header = list(rows[0].keys()) if rows else []
    _write_csv(path, header, rows)


# ---------------------------------------------------------------------------
# E2E 夹具的评分设计（所有断言据此手算）
#   P1（H2a D−B）：D = 3 + delta，B = 3，delta = [1,2,3,1,2,3,1]
#   P1（H2b C−A）：C = A = 3 → 全零差 → m=0
#   P3（H1  D−B）：D = 5，B = 3（S7 按 §5.4.7 为 N/A）→ 6 个差，均为 +2
# ---------------------------------------------------------------------------

P1_DELTA = {"S1": 1, "S2": 2, "S3": 3, "S4": 1, "S5": 2, "S6": 3, "S7": 1}
P1_BASE = 2


def e2e_overrides():
    overrides = {}
    for sample, delta in P1_DELTA.items():
        overrides[(sample, "D", "P1")] = str(P1_BASE + delta)
        overrides[(sample, "B", "P1")] = str(P1_BASE)
    for sample in A.SAMPLES:
        if sample == "S7":
            continue
        overrides[(sample, "D", "P3")] = "5"
        overrides[(sample, "B", "P3")] = "3"
    return overrides


# ===========================================================================
# 1. 纯统计函数
# ===========================================================================


def test_average_ranks():
    check("average_ranks 基本", A.average_ranks([1, 2, 2, 3]) == [1.0, 2.5, 2.5, 4.0],
          A.average_ranks([1, 2, 2, 3]))
    check("average_ranks 全并列", A.average_ranks([5, 5, 5]) == [2.0, 2.0, 2.0],
          A.average_ranks([5, 5, 5]))
    check("average_ranks 单值", A.average_ranks([7]) == [1.0])


def test_parse_values():
    check("parse_likert 整数", A.parse_likert("3", "t") == ("score", 3))
    check("parse_likert 空 = pending", A.parse_likert("", "t") == ("pending", None))
    check("parse_likert N/A", A.parse_likert("N/A", "t") == ("na", None))
    check("parse_likert n/a 小写", A.parse_likert("n/a", "t") == ("na", None))
    expect_error("parse_likert 拒绝 05", "bad-value",
                 lambda: A.parse_likert("05", "t"))
    expect_error("parse_likert 拒绝带空格 3", "bad-value",
                 lambda: A.parse_likert(" 3 ", "t"))
    expect_error("parse_likert 拒绝 6", "out-of-range",
                 lambda: A.parse_likert("6", "t"))
    expect_error("parse_likert 拒绝文本", "bad-value",
                 lambda: A.parse_likert("good", "t"))
    check("parse_binary 0/1", A.parse_binary("0", "t") == ("score", 0) and
          A.parse_binary("1", "t") == ("score", 1))
    check("parse_verdict", A.parse_verdict("partial", "t") == ("score", "partial"))
    expect_error("parse_verdict 拒绝未知标签", "bad-value",
                 lambda: A.parse_verdict("maybe", "t"))


def test_read_table():
    root = TMP_ROOT / "read_table"
    root.mkdir(parents=True, exist_ok=True)
    good = root / "good.csv"
    good.write_text("a,b\n1,2\n", encoding="utf-8")
    rows = A.read_table(good, ("a", "b"), "good")
    check("read_table 读表", rows[0]["a"] == "1" and rows[0]["_line"] == 2, rows)

    bad = root / "bad.csv"
    bad.write_text("a,c\n1,2\n", encoding="utf-8")
    expect_error("read_table 表头不符", "bad-header",
                 lambda: A.read_table(bad, ("a", "b"), "bad"))

    empty = root / "empty.csv"
    empty.write_text("a,b\n", encoding="utf-8")
    expect_error("read_table 只有表头", "empty-table",
                 lambda: A.read_table(empty, ("a", "b"), "empty"))
    check("read_table allow_empty",
          A.read_table(empty, ("a", "b"), "empty", allow_empty=True) == [])

    blank = root / "blank.csv"
    blank.write_text("", encoding="utf-8")
    expect_error("read_table 空文件", "empty-file",
                 lambda: A.read_table(blank, ("a", "b"), "blank"))

    ragged = root / "ragged.csv"
    ragged.write_text("a,b\n1,2\n3\n", encoding="utf-8")
    expect_error("read_table 列数不符", "bad-row",
                 lambda: A.read_table(ragged, ("a", "b"), "ragged"))

    expect_error("read_table 缺文件", "missing-input",
                 lambda: A.read_table(root / "nope.csv", ("a",), "nope"))


def test_cohens_kappa():
    perfect = A.cohens_kappa([(1, 1), (2, 2)], (1, 2))
    check("κ 完全一致 = 1", close(perfect["kappa"], 1.0), perfect)
    check("κ 完全一致 po=1", close(perfect["po"], 1.0), perfect)

    # 手算：pairs [(1,1),(1,2)] → po=0.5；marg_a={1:1.0}；marg_b={1:.5,2:.5}
    # pe = 1.0*0.5 + 0.0*0.5 = 0.5 → κ = (0.5-0.5)/(1-0.5) = 0
    hand = A.cohens_kappa([(1, 1), (1, 2)], (1, 2))
    check("κ 手算 pe=0.5", close(hand["pe"], 0.5), hand)
    check("κ 手算 κ=0", close(hand["kappa"], 0.0), hand)

    zero = A.cohens_kappa([], (0, 1))
    check("κ n=0 不适用", zero["status"] == "not_applicable" and
          zero["reason_code"] == "n=0", zero)

    flat = A.cohens_kappa([(1, 1), (1, 1)], (1, 2))
    check("κ pe=1 不适用", flat["status"] == "not_applicable" and
          flat["reason_code"] == "pe=1", flat)


def test_weighted_kappa():
    # 手算：pairs [(1,1),(2,3)]，k=5，denom=16
    #   weighted_disagree = 0.5*0 + 0.5*(1/16) = 1/32 → po = 31/32
    #   expected_disagree = 0.25*(0 + 4/16 + 1/16 + 1/16) = 0.25*0.375
    #   pe = 1 - 0.09375 = 0.90625
    #   kappa = (0.96875 - 0.90625) / (1 - 0.90625) = 0.6666...
    res = A.quadratic_weighted_kappa([(1, 1), (2, 3)])
    check("加权 κ po 手算", close(res["po"], 0.96875), res)
    check("加权 κ pe 手算", close(res["pe"], 0.90625), res)
    check("加权 κ κ 手算 2/3", close(res["kappa"], 2.0 / 3.0), res)
    check("加权 κ 原始一致率手算", close(res["raw_agreement"], 0.5), res)

    perfect = A.quadratic_weighted_kappa([(1, 1), (5, 5), (3, 3)])
    check("加权 κ 完全一致", close(perfect["kappa"], 1.0) and
          close(perfect["raw_agreement"], 1.0), perfect)

    flat = A.quadratic_weighted_kappa([(2, 2), (2, 2)])
    check("加权 κ pe=1 不适用", flat["status"] == "not_applicable" and
          flat["reason_code"] == "pe=1", flat)


def reference_p(diffs):
    """用 DP（子集和分布）独立复算精确双侧 p，对照枚举实现。"""
    nonzero = [d for d in diffs if d != 0]
    m = len(nonzero)
    if m == 0:
        return None
    ranks = A.average_ranks([abs(d) for d in nonzero])
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    dist = {0.0: 1.0}
    for rank in ranks:
        nxt = dict(dist)
        for total, count in dist.items():
            key = round(total + rank, 9)
            nxt[key] = nxt.get(key, 0) + count
        dist = nxt
    denominator = sum(dist.values())
    lower = sum(c for t, c in dist.items() if t <= w_plus + 1e-9)
    upper = sum(c for t, c in dist.items() if t >= w_plus - 1e-9)
    return min(1.0, 2.0 * min(lower, upper) / denominator)


def test_wilcoxon_min_p():
    seven = A.wilcoxon_exact([1.0] * 7)
    check("m=7 最小双侧 p=0.015625", close(seven["p_raw"], 0.015625), seven)
    check("m=7 p_min_achievable", close(seven["p_min_achievable"], 0.015625), seven)
    check("m=7 r_rb=1", close(seven["r_rb"], 1.0), seven)
    check("m=7 W+ 为 28", close(seven["w_plus"], 28.0), seven)
    check("m=7 差值全为 1 → dz 因 sd=0 不可计算",
          seven["dz"] is None and seven["dz_reason"] == "sd=0", seven)

    six = A.wilcoxon_exact([2.0] * 6)
    check("m=6 最小双侧 p=0.03125", close(six["p_raw"], 0.03125), six)
    check("m=6 p_min_achievable", close(six["p_min_achievable"], 0.03125), six)
    check("m=6 常量差 dz 不可计算", six["dz"] is None and
          six["dz_reason"] == "sd=0", six)
    check("常量差不得写成「无差异」",
          "无差异" not in (six["note"] or "") and six["status"] == "ok", six)

    five = A.wilcoxon_exact([1.0, 2.0, 3.0, 4.0, 5.0])
    check("m=5 最小双侧 p=0.0625", close(five["p_raw"], 0.0625), five)

    four = A.wilcoxon_exact([1.0, 2.0, 3.0, 4.0])
    check("m=4 最小双侧 p≥0.125", five["p_raw"] is not None and
          four["p_raw"] >= 0.125 - 1e-12, four)


def test_wilcoxon_ties_and_zeros():
    tied = [1.0, 2.0, 2.0, 3.0]
    res = A.wilcoxon_exact(tied)
    # 平均秩：|1|=1, |2|=2.5, |2|=2.5, |3|=4 → 全为正 → W+ = 1+2.5+2.5+4 = 10
    check("并列用平均秩 W+ 手算 = 10", close(res["w_plus"], 10.0), res)
    check("并列秩 r_rb=1", close(res["r_rb"], 1.0), res)
    check("并列 p 与 DP 复算一致", close(res["p_raw"], reference_p(tied)), res)

    zeroed = [1.0, 2.0, 0.0, 0.0, 0.0, 3.0, 4.0]
    res2 = A.wilcoxon_exact(zeroed)
    check("零差剔除后 m=4", res2["m_nonzero"] == 4, res2)
    check("m 只数非零差 n_total=7", res2["n_total"] == 7, res2)
    check("零差保留在 dz 里（用全部 7 个）", close(
        res2["dz"], A.mean(zeroed) / A.sample_sd(zeroed)), res2)
    check("零差案例 p 与 DP 一致", close(res2["p_raw"], reference_p(zeroed)), res2)

    mixed = [1.0, 2.0, 3.0, -4.0, -5.0, -6.0, -7.0]
    res3 = A.wilcoxon_exact(mixed)
    check("r_rb 手算 = (6-22)/28", close(res3["r_rb"], (6.0 - 22.0) / 28.0), res3)
    check("负差主导 W- 手算 22", close(res3["w_minus"], 22.0), res3)
    check("混合符号 p 与 DP 一致", close(res3["p_raw"], reference_p(mixed)), res3)
    check("dz 符号与均值同号", res3["dz"] < 0, res3)


def test_wilcoxon_all_zero():
    res = A.wilcoxon_exact([0.0] * 7)
    check("全零 → 不适用", res["status"] == "not_applicable", res)
    check("全零 reason=m=0", res["reason_code"] == "m=0", res)
    check("全零不生成 p", res["p_raw"] is None, res)
    check("全零 r_rb 不适用", res["r_rb"] is None, res)
    check("全零 dz 不可计算（sd=0）", res["dz"] is None and
          res["dz_reason"] == "sd=0", res)


def test_effect_label():
    check("r_rb 档位 大", A.effect_label(0.6, A.RRB_THRESHOLDS) == "大")
    check("r_rb 档位 中", A.effect_label(0.35, A.RRB_THRESHOLDS) == "中")
    check("r_rb 档位 小", A.effect_label(0.15, A.RRB_THRESHOLDS) == "小")
    check("r_rb 档位 可忽略", A.effect_label(0.05, A.RRB_THRESHOLDS) == "可忽略")
    check("档位 None → 空", A.effect_label(None, A.RRB_THRESHOLDS) == "")


def test_holm():
    basic = A.holm_adjust([0.01, 0.02, 0.04], "fam")
    check("Holm 逐步校正 0.03/0.04/0.04",
          [close(r["p_holm"], v) for r, v in zip(basic, (0.03, 0.04, 0.04))] ==
          [True, True, True], [(r["p_raw"], r["p_holm"]) for r in basic])
    check("Holm factor = 3,2,1", [r["factor"] for r in basic] == [3, 2, 1])
    check("Holm rank 按 p 升序", [r["rank"] for r in basic] == [1, 2, 3])
    check("Holm 按原始位置返回", close(basic[2]["p_raw"], 0.04))

    monotone = A.holm_adjust([0.02, 0.02, 0.5], "fam")
    check("Holm 并列最小 p 两个都取 0.06",
          close(monotone[0]["p_holm"], 0.06) and close(monotone[1]["p_holm"], 0.06),
          [(r["p_raw"], r["p_holm"]) for r in monotone])
    check("Holm 单调不减",
          all(monotone[i]["p_holm"] <= monotone[i + 1]["p_holm"] + 1e-12
              for i in range(len(monotone) - 1)))
    check("Holm 并列最小 p 在 α=0.05 下都不显著",
          not any(r["rejected"] for r in monotone))

    tied_all = A.holm_adjust([0.015625] * 3, "fam")
    check("三对比同为最小 p → Holm 后 0.046875 全显著",
          all(close(r["p_holm"], 0.046875) and r["rejected"] for r in tied_all),
          [(r["p_holm"], r["rejected"]) for r in tied_all])

    m6 = A.holm_adjust([0.03125] * 3, "fam")
    check("m=6 三对比 Holm 后 0.0939 不显著",
          all(close(r["p_holm"], 0.09375) and not r["rejected"] for r in m6),
          [(r["p_holm"], r["rejected"]) for r in m6])

    labelled = A.holm_adjust([0.2, 0.1], "fam", labels=["乙", "甲"])
    check("Holm labels 跟随原索引", labelled[0]["contrast"] == "乙" and
          labelled[1]["contrast"] == "甲")
    check("Holm family/family_size 记录", labelled[0]["family"] == "fam" and
          labelled[0]["family_size"] == 2)


def test_bootstrap():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    first = A.bootstrap_mean_ci(values, reps=2000, seed=7, tag=("P1", "A"))
    second = A.bootstrap_mean_ci(values, reps=2000, seed=7, tag=("P1", "A"))
    check("Bootstrap 固定种子可复现",
          close(first["ci_low"], second["ci_low"]) and
          close(first["ci_high"], second["ci_high"]),
          (first, second))
    check("Bootstrap 记录 reps", first["reps"] == 2000)
    check("Bootstrap CI 含样本均值", first["ci_low"] <= 4.0 <= first["ci_high"],
          first)
    check("Bootstrap n<2 不计算",
          A.bootstrap_mean_ci([3.0], reps=100, seed=1)["reason_code"] == "n<2")
    check("Bootstrap n=0 不计算",
          A.bootstrap_mean_ci([], reps=100, seed=1)["reason_code"] == "n=0")

    # 种子推导：同 seed+tag 必同；任一变化必不同（这是可复现性的根）
    check("_derive_seed 同参同值",
          A._derive_seed(7, "P1", "A") == A._derive_seed(7, "P1", "A"))
    check("_derive_seed 换种子变值",
          A._derive_seed(7, "P1", "A") != A._derive_seed(8, "P1", "A"))
    check("_derive_seed 换 tag 变值",
          A._derive_seed(7, "P1", "A") != A._derive_seed(7, "P1", "D"))

    # 独立复算：CI 必须与「用 _derive_seed 播种、同一重采样算法」的结果逐位一致
    manual = []
    rng = random.Random(A._derive_seed(7, "P1", "A"))
    for _ in range(2000):
        sample = [values[rng.randrange(len(values))] for _ in values]
        manual.append(sum(sample) / len(sample))
    manual.sort()
    check("Bootstrap CI 与独立复算一致（低端）",
          close(first["ci_low"], A._percentile(manual, 0.025)), first)
    check("Bootstrap CI 与独立复算一致（高端）",
          close(first["ci_high"], A._percentile(manual, 0.975)), first)


def test_chi2_and_friedman():
    check("chi2_sf(0,3)=1", close(A.chi2_sf(0.0, 3), 1.0))
    check("chi2_sf(7.815,3)≈0.05", close(A.chi2_sf(7.8147, 3), 0.05, 5e-4),
          A.chi2_sf(7.8147, 3))

    # 手算 Friedman：三区组、四处理，列秩和 6/9/8/7 → Q=1.0，df=3
    blocks = [[1, 2, 3, 4], [2, 3, 4, 1], [3, 4, 1, 2]]
    res = A.friedman_test(blocks)
    check("Friedman Q 手算 = 1.0", close(res["q"], 1.0), res)
    check("Friedman df=3", res["df"] == 3, res)
    check("Friedman p 手算 ≈0.8013", close(res["p"], 0.8013, 1e-3), res)
    check("Friedman n_blocks=3", res["n_blocks"] == 3, res)

    flat = A.friedman_test([[2, 2, 2, 2], [3, 3, 3, 3]])
    check("Friedman 无变异 → 不适用", flat["status"] == "not_applicable" and
          flat["reason_code"] == "no-variation", flat)
    empty = A.friedman_test([])
    check("Friedman n=0", empty["status"] == "not_applicable" and
          empty["reason_code"] == "n=0", empty)
    expect_error("Friedman 处理数 <3 报错", "bad-friedman",
                 lambda: A.friedman_test([[1, 2]]))

    flat_q = A.cochrans_q([[1, 1, 1, 1], [0, 0, 0, 0]])
    check("Cochran Q 无变异", flat_q["status"] == "not_applicable" and
          flat_q["reason_code"] == "no-variation", flat_q)


# ===========================================================================
# 2. 端到端：合成全套输入 → main() → 断言手算结果
# ===========================================================================


def test_e2e():
    fixture = make_fixture(TMP_ROOT / "e2e", overrides=e2e_overrides())
    out_dir = TMP_ROOT / "e2e_out"
    code, stdout = run_main(fixture, out_dir)
    check("E2E main 返回 0", code == 0, stdout[-400:])

    expected = {
        "inter_rater_agreement.csv": 9,
        "ratings_final.csv": A.FORMAL_COUNT,
        "ratings_final_detail.csv": A.FORMAL_COUNT * 7,
        "ratings_calibration_final.csv": A.CALIBRATION_COUNT,
        "ratings_supplement_final.csv": A.SUPPLEMENT_COUNT,
        "ratings_supplement_final_detail.csv": A.SUPPLEMENT_COUNT * 2,
        "cell_aggregates.csv": 28 * 7,
        "descriptive_statistics.csv": 4 * 5,
        "primary_hypothesis_tests.csv": 3,
        "holm_adjusted_results.csv": 3,
        "friedman_results.csv": 4,
        "binary_rates.csv": 4,
        "binary_tests.csv": 1,
        "h5_results.csv": 0,
        "y2_calls_distribution.csv": 0,
        "input_hashes.csv": 5,
    }
    for name, count in expected.items():
        path = out_dir / name
        exists = path.is_file()
        got = len(read_csv(path)) if exists else -1
        check("E2E 输出 %s 行数 %d" % (name, count), exists and got == count,
              "存在=%s 行数=%s" % (exists, got))
    check("E2E analysis_report.md 存在", (out_dir / "analysis_report.md").is_file())

    primary = {row["contrast"]: row for row in
               read_csv(out_dir / "primary_hypothesis_tests.csv")}
    h1 = primary["H1_D_minus_B_P3"]
    check("H1 n_blocks=6（S7 的 P3 按 §5.4.7 不适用）",
          int(h1["n_blocks"]) == 6, h1)
    check("H1 n_na_excluded=1", int(h1["n_na_excluded"]) == 1, h1)
    check("H1 m=6", int(h1["m_nonzero"]) == 6, h1)
    check("H1 W+=21", close(float(h1["w_plus"]), 21.0), h1)
    check("H1 原始 p=0.03125", close(float(h1["p_raw"]), 0.03125), h1)
    check("H1 平均差=2", close(float(h1["mean_diff"]), 2.0), h1)
    check("H1 r_rb=1", close(float(h1["r_rb"]), 1.0), h1)
    check("H1 dz 不可计算（sd=0）", h1["dz"] == "" and h1["dz_reason"] == "sd=0", h1)
    check("H1 Holm 后 p=0.0625", close(float(h1["p_holm"]), 0.0625), h1)
    check("H1 结论=未观察到差异",
          h1["conclusion"].startswith("未观察到差异"), h1["conclusion"])

    h2a = primary["H2a_D_minus_B_P1"]
    check("H2a n_blocks=7", int(h2a["n_blocks"]) == 7, h2a)
    check("H2a m=7", int(h2a["m_nonzero"]) == 7, h2a)
    check("H2a 原始 p=0.015625", close(float(h2a["p_raw"]), 0.015625), h2a)
    check("H2a 平均差=13/7", close(float(h2a["mean_diff"]), 13.0 / 7.0, 1e-6), h2a)
    check("H2a dz 手算 2.064", close(float(h2a["dz"]), 2.064, 1e-3), h2a)
    check("H2a Holm 后 p=0.046875", close(float(h2a["p_holm"]), 0.046875), h2a)
    check("H2a 结论=支持（推断性，n=7）",
          h2a["conclusion"] == "支持（推断性，n=7）", h2a["conclusion"])

    h2b = primary["H2b_C_minus_A_P1"]
    check("H2b 全零差 → 不适用", h2b["status"] == "not_applicable" and
          h2b["reason_code"] == "m=0", h2b)
    check("H2b 不生成 p", h2b["p_raw"] == "", h2b)
    check("H2b 结论=不适用", h2b["conclusion"] == "不适用", h2b["conclusion"])

    holm = read_csv(out_dir / "holm_adjusted_results.csv")
    check("Holm 主家族 3 行", len(holm) == 3 and
          all(row["family"] == "primary_H1_H2a_H2b" for row in holm))
    got_holm = {row["contrast"]: float(row["p_holm"]) for row in holm}
    check("Holm 与主假设表一致",
          close(got_holm["H2a_D_minus_B_P1"], 0.046875) and
          close(got_holm["H1_D_minus_B_P3"], 0.0625) and
          close(got_holm["H2b_C_minus_A_P1"], 1.0), got_holm)

    friedman = {row["metric"]: row for row in
                read_csv(out_dir / "friedman_results.csv")}
    check("Friedman P3 跳过 S7 → n_blocks=6",
          int(friedman["P3"]["n_blocks"]) == 6, friedman["P3"])
    check("Friedman P3 note 说明 1 个样本不适用",
          "不适用" in friedman["P3"]["note"], friedman["P3"]["note"])
    check("Friedman P1 n_blocks=7", int(friedman["P1"]["n_blocks"]) == 7,
          friedman["P1"])
    check("Friedman 不替代预登记对比（status=ok）",
          friedman["P1"]["status"] == "ok", friedman["P1"])

    desc = {(row["condition"], row["metric"]): row for row in
            read_csv(out_dir / "descriptive_statistics.csv")}
    check("描述统计 P1/A 均值=3", close(float(desc[("A", "P1")]["mean"]), 3.0),
          desc[("A", "P1")])
    check("描述统计 P1/D 均值=27/7",
          close(float(desc[("D", "P1")]["mean"]), 27.0 / 7.0, 1e-6),
          desc[("D", "P1")])
    check("描述统计记录 bootstrap_reps=500",
          int(desc[("A", "P1")]["bootstrap_reps"]) == 500, desc[("A", "P1")])
    check("描述统计给出 CI", desc[("A", "P1")]["ci_low"] != "" and
          desc[("A", "P1")]["ci_high"] != "")
    check("描述统计 95% CI 覆盖均值",
          float(desc[("D", "P1")]["ci_low"]) <= float(desc[("D", "P1")]["mean"]) <=
          float(desc[("D", "P1")]["ci_high"]), desc[("D", "P1")])

    agreement = {row["item"]: row for row in
                 read_csv(out_dir / "inter_rater_agreement.csv")}
    check("一致性 P1 κ=1（两人完全一致）",
          close(float(agreement["P1"]["kappa"]), 1.0), agreement["P1"])
    check("一致性 P1 用二次加权 κ",
          agreement["P1"]["method"] == "quadratic_weighted_cohen_kappa",
          agreement["P1"])
    check("一致性 S4_verdict 用未加权三分类 κ",
          agreement["S4_verdict"]["method"] == "cohen_kappa_nominal_3cat",
          agreement["S4_verdict"])
    check("补充 E1/E7 标为 supplementary",
          agreement["E1"]["table"] == "supplementary", agreement["E1"])
    check("一致性 9 行", len(agreement) == 9)

    report = (out_dir / "analysis_report.md").read_text(encoding="utf-8")
    for token in ("n=7", "未观察到差异", "推断性", "描述性",
                  "二次加权", "Holm", "bootstrap", "不适用"):
        check("报告包含关键词 %s" % token, token in report)
    check("报告写明 5 次 vs 3 份的设计歧义",
          "5 次" in report and "3 份" in report)
    check("报告写明 H1 的 m=6 与 0.0939",
          "0.0939" in report or "0.0313" in report)

    check("stdout 打印输出目录", "OK 输出目录" in stdout, stdout[-200:])
    return fixture, out_dir


def test_samples_not_mixed(fixture, out_dir):
    mapping = {row["blind_id"]: row for row in
               read_csv(fixture["paths"]["mapping"])}
    final = read_csv(out_dir / "ratings_final.csv")
    check("ratings_final 84 行", len(final) == A.FORMAL_COUNT)
    check("ratings_final 只有正式批次",
          all(row["phase"] == "formal" for row in final))
    check("ratings_final 不含 sample_id/group（评分表不得泄露）",
          "sample_id" not in final[0] and "group" not in final[0], list(final[0]))

    cells = {}
    for row in final:
        meta = mapping[row["blind_id"]]
        cells.setdefault((meta["sample_id"], meta["condition"]), []).append(row)
    check("28 个格", len(cells) == 28)
    check("每格恰 3 份", all(len(group) == 3 for group in cells.values()),
          {k: len(v) for k, v in cells.items() if len(v) != 3})

    aggregates = read_csv(out_dir / "cell_aggregates.csv")
    index = {(row["sample_id"], row["condition"], row["metric"]): row
             for row in aggregates}
    ok = True
    detail = ""
    for (sample, condition), group in cells.items():
        values = [float(row["P1"]) for row in group]
        cell = index[(sample, condition, "P1")]
        if not close(float(cell["value"]), A.mean(values)):
            ok = False
            detail = (sample, condition, values, cell["value"])
            break
        if int(cell["n_total"]) != 3:
            ok = False
            detail = ("n_total", sample, condition, cell["n_total"])
            break
    check("格值 = 3 份最终分均值（不跨样本混算）", ok, detail)
    check("格内 SD 记录", index[("S1", "A", "P1")]["cell_sd"] != "")


def test_calibration_and_supplement_excluded(fixture, out_dir):
    calib = read_csv(out_dir / "ratings_calibration_final.csv")
    formal = read_csv(out_dir / "ratings_final.csv")
    check("校准 6 行", len(calib) == A.CALIBRATION_COUNT)
    check("校准与正式 blind_id 不重叠",
          not ({row["blind_id"] for row in calib} &
               {row["blind_id"] for row in formal}))
    supp = read_csv(out_dir / "ratings_supplement_final.csv")
    check("补充 23 行", len(supp) == A.SUPPLEMENT_COUNT)
    check("补充只含 E1/E7", set(supp[0].keys()) == {"blind_id", "E1", "E7"},
          list(supp[0].keys()))
    mapping = {row["blind_id"]: row for row in
               read_csv(fixture["paths"]["mapping"])}
    e7 = sum(1 for row in supp if row["E7"] != "N/A")
    check("E7 适用 15 份", e7 == 15, e7)
    e1 = sum(1 for row in supp if row["E1"] != "N/A")
    check("E1 适用 23 份", e1 == 23, e1)
    s7empty = sum(1 for row in supp
                  if mapping[row["blind_id"]]["batch"] == "suppl-s7empty")
    check("S7-empty 批次 3 份", s7empty == 3, s7empty)
    b0 = sum(1 for row in supp
             if mapping[row["blind_id"]]["batch"] == "b0-legacy")
    check("b0-legacy 批次 10 份", b0 == 10, b0)
    subj = sum(1 for row in supp
               if mapping[row["blind_id"]]["batch"] == "suppl-subj")
    check("suppl-subj 批次 10 份", subj == 10, subj)

    # 校准份不得进正式格内聚合
    mapping_full = A.load_mapping(fixture["paths"]["mapping"])
    calib_raw = A.load_raw_ratings(fixture["paths"]["calibration_raw"],
                                   mapping_full, "calibration")
    final_calib, _ = A.derive_final(calib_raw, {})
    expect_error("校准份进正式聚合 → scope-violation", "scope-violation",
                 lambda: A.aggregate_cells(final_calib, mapping_full, "formal"))


def test_overwrite_protection(fixture, out_dir):
    expect_error("输出目录非空 → output-exists", "output-exists",
                 lambda: run_main(fixture, out_dir))
    fresh = TMP_ROOT / "e2e_out_fresh"
    fresh.mkdir(parents=True, exist_ok=True)
    (fresh / "ratings_final.csv").write_text("占位\n", encoding="utf-8")
    expect_error("目标文件已存在 → output-exists", "output-exists",
                 lambda: run_main(fixture, fresh))
    check("禁覆盖时没有写坏占位文件",
          (fresh / "ratings_final.csv").read_text(encoding="utf-8") == "占位\n")


def test_input_hashes_unchanged(fixture, out_dir):
    before = {Path(path).name: A.sha256_file(path)
              for name, path in fixture["paths"].items() if name != "results"}
    records = {Path(row["path"]).name: row["sha256"]
               for row in read_csv(out_dir / "input_hashes.csv")}
    ok = all(records.get(name) == digest for name, digest in before.items())
    check("input_hashes.csv 记录与输入一致", ok,
          {name: (records.get(name), digest) for name, digest in before.items()})
    after = {Path(path).name: A.sha256_file(path)
             for name, path in fixture["paths"].items() if name != "results"}
    check("运行后输入未被改动", before == after)


def test_pending_denominator():
    overrides = e2e_overrides()
    pending = {("S1", "A", 0, "P1", "R2")}
    fixture = make_fixture(TMP_ROOT / "pending_part", overrides=overrides,
                           pending=pending)
    out_dir = TMP_ROOT / "pending_part_out"
    code, _ = run_main(fixture, out_dir)
    check("单份 pending 仍可跑完", code == 0)
    aggregates = {(row["sample_id"], row["condition"], row["metric"]): row
                  for row in read_csv(out_dir / "cell_aggregates.csv")}
    cell = aggregates[("S1", "A", "P1")]
    check("pending 不进分母：n_effective=2", int(cell["n_effective"]) == 2, cell)
    check("pending 计数：n_pending=1", int(cell["n_pending"]) == 1, cell)
    check("pending 时 note 说明分母", "pending" in cell["note"], cell["note"])
    detail = {(row["blind_id"], row["item"]): row for row in
              read_csv(out_dir / "ratings_final_detail.csv")}
    bid = blind_of(fixture, "formal", "S1", "A", 0)
    check("pending 最终分保留 pending 标记",
          detail[(bid, "P1")]["status"] == "pending" and
          detail[(bid, "P1")]["final_value"] == "pending", detail[(bid, "P1")])


def test_pending_blocks_hypothesis():
    overrides = e2e_overrides()
    pending = {("S5", "B", repeat, "P1", rater)
               for repeat in range(3) for rater in ("R1", "R2")}
    fixture = make_fixture(TMP_ROOT / "pending_all", overrides=overrides,
                           pending=pending)
    out_dir = TMP_ROOT / "pending_all_out"
    code, _ = run_main(fixture, out_dir)
    check("整格 pending 仍可跑完（不中止）", code == 0)
    aggregates = {(row["sample_id"], row["condition"], row["metric"]): row
                  for row in read_csv(out_dir / "cell_aggregates.csv")}
    cell = aggregates[("S5", "B", "P1")]
    check("整格 pending → unavailable", cell["status"] == "unavailable", cell)
    check("整格 pending n_effective=0", int(cell["n_effective"]) == 0, cell)
    primary = {row["contrast"]: row for row in
               read_csv(out_dir / "primary_hypothesis_tests.csv")}
    h2a = primary["H2a_D_minus_B_P1"]
    check("缺格不补零：H2a 标 unavailable",
          h2a["status"] == "unavailable" and
          h2a["reason_code"] == "missing-cell-value", h2a)
    check("缺格不生成 p", h2a["p_raw"] == "", h2a)
    check("缺格点名具体样本", "S5" in h2a["missing_samples"], h2a)
    check("缺格结论=不可计算", h2a["conclusion"] == "不可计算", h2a["conclusion"])


def test_adjudication_flow():
    overrides = e2e_overrides()
    split = {("S2", "B", repeat, "P1", "R1"): "2"
             for repeat in range(3)}
    split.update({("S2", "B", repeat, "P1", "R2"): "4"
                  for repeat in range(3)})

    fixture = make_fixture(TMP_ROOT / "adj_missing", overrides=overrides,
                           rater_overrides=split)
    out_dir = TMP_ROOT / "adj_missing_out"
    code, _ = run_main(fixture, out_dir)
    check("未裁决仍可跑完", code == 0)
    detail = {(row["blind_id"], row["item"]): row for row in
              read_csv(out_dir / "ratings_final_detail.csv")}
    bid = blind_of(fixture, "formal", "S2", "B", 0)
    row = detail[(bid, "P1")]
    check("差>1 未裁决 → pending_unadjudicated",
          row["status"] == "pending_unadjudicated" and
          row["final_value"] == "pending", row)
    primary = {row["contrast"]: row for row in
               read_csv(out_dir / "primary_hypothesis_tests.csv")}
    check("未裁决不参与聚合 → H2a unavailable",
          primary["H2a_D_minus_B_P1"]["status"] == "unavailable",
          primary["H2a_D_minus_B_P1"])

    adjudications = []
    for repeat in range(3):
        adjudications.append({
            "blind_id": blind_of(fixture, "formal", "S2", "B", repeat),
            "item": "P1", "rater_a_value": "2", "rater_b_value": "4",
            "final_value": "3", "adjudicator": "导师",
            "reason": "两人差距 2 分，按细则取 3",
            "decided_utc": "2026-11-19T01:00:00Z",
        })
    fixture2 = make_fixture(TMP_ROOT / "adj_done", overrides=overrides,
                            rater_overrides=split, adjudications=adjudications)
    out_dir2 = TMP_ROOT / "adj_done_out"
    code2, _ = run_main(fixture2, out_dir2)
    check("有裁决可跑完", code2 == 0)
    detail2 = {(row["blind_id"], row["item"]): row for row in
               read_csv(out_dir2 / "ratings_final_detail.csv")}
    row2 = detail2[(bid, "P1")]
    check("裁决值进入最终分",
          row2["status"] == "derived_adjudicated" and
          close(float(row2["final_value"]), 3.0) and row2["adjudicated"] == "1",
          row2)
    primary2 = {row["contrast"]: row for row in
                read_csv(out_dir2 / "primary_hypothesis_tests.csv")}
    check("有裁决后 H2a 恢复可计算",
          primary2["H2a_D_minus_B_P1"]["status"] == "ok",
          primary2["H2a_D_minus_B_P1"])

    bad = [{
        "blind_id": blind_of(fixture, "formal", "S1", "A", 0),
        "item": "P1", "rater_a_value": "3", "rater_b_value": "3",
        "final_value": "3", "adjudicator": "导师", "reason": "无",
        "decided_utc": "2026-11-19T01:00:00Z",
    }]
    fixture3 = make_fixture(TMP_ROOT / "adj_bad", overrides=overrides,
                            adjudications=bad)
    expect_error("未触发阈值却写裁决 → bad-adjudication", "bad-adjudication",
                 lambda: run_main(fixture3, TMP_ROOT / "adj_bad_out"))

    bad2 = [dict(bad[0], blind_id=blind_of(fixture, "formal", "S2", "B", 0),
                 rater_a_value="2", rater_b_value="4", final_value="9")]
    fixture4 = make_fixture(TMP_ROOT / "adj_bad2", overrides=overrides,
                            rater_overrides=split, adjudications=bad2)
    expect_error("裁决 final_value 越界 → bad-adjudication", "bad-adjudication",
                 lambda: run_main(fixture4, TMP_ROOT / "adj_bad2_out"))


def test_mapping_fail_closed():
    fixture = make_fixture(TMP_ROOT / "map_ok", overrides=e2e_overrides())
    good = A.load_mapping(fixture["paths"]["mapping"])
    check("mapping 载入 113 份", len(good) == 113, len(good))

    def one(root_name, mutate):
        root = TMP_ROOT / root_name
        if root.exists():
            shutil.rmtree(root)
        built = make_fixture(root, overrides=e2e_overrides())
        mutate_csv(built["paths"]["mapping"], mutate)
        return built

    def set_phase(rows):
        rows[0]["phase"] = "bogus"

    expect_error("mapping phase 非法 → bad-mapping", "bad-mapping",
                 lambda: A.load_mapping(one("map_bad_phase", set_phase)["paths"]["mapping"]))

    def dup_blind(rows):
        rows[1]["blind_id"] = rows[0]["blind_id"]

    expect_error("mapping blind_id 重复 → bad-mapping", "bad-mapping",
                 lambda: A.load_mapping(one("map_dup", dup_blind)["paths"]["mapping"]))

    def bad_sha(rows):
        rows[2]["source_result_row_sha256"] = "xyz"

    expect_error("mapping 哈希格式非法 → bad-mapping", "bad-mapping",
                 lambda: A.load_mapping(one("map_sha", bad_sha)["paths"]["mapping"]))

    def drop_row(rows):
        rows.pop()

    expect_error("mapping 份数不足 → bad-mapping", "bad-mapping",
                 lambda: A.load_mapping(one("map_short", drop_row)["paths"]["mapping"]))

    def dup_source(rows):
        rows[1]["source_run_dir"] = rows[0]["source_run_dir"]
        rows[1]["source_row_number"] = rows[0]["source_row_number"]
        rows[1]["source_result_row_sha256"] = rows[0]["source_result_row_sha256"]

    expect_error("同一源记录两个 blind_id → bad-mapping", "bad-mapping",
                 lambda: A.load_mapping(one("map_dup_src", dup_source)["paths"]["mapping"]))

    def swap_to_calibration(rows):
        formal = [r for r in rows if r["phase"] == "formal"]
        for row in formal[:1]:
            row["phase"] = "calibration"

    expect_error("正式/校准份数不符 → bad-mapping", "bad-mapping",
                 lambda: A.load_mapping(one("map_calib", swap_to_calibration)["paths"]["mapping"]))


def test_raw_ratings_fail_closed():
    def one(root_name, mutate):
        root = TMP_ROOT / root_name
        if root.exists():
            shutil.rmtree(root)
        built = make_fixture(root, overrides=e2e_overrides())
        mutate(built)
        return built

    def run(built):
        mapping = A.load_mapping(built["paths"]["mapping"])
        return lambda: A.load_raw_ratings(built["paths"]["formal_raw"], mapping,
                                          "formal")

    def score_p3_s7(built):
        mapping = {row["blind_id"]: row for row in
                   read_csv(built["paths"]["mapping"])}
        def mutate(rows):
            for row in rows:
                meta = mapping.get(row["blind_id"])
                if meta and meta["sample_id"] == "S7":
                    row["P3"] = "4"
        mutate_csv(built["paths"]["formal_raw"], mutate)

    expect_error("S7 给 P3 打分 → 不适用必须 N/A", "bad-formal-raw",
                 run(one("raw_p3", score_p3_s7)))

    def na_on_p1(built):
        def mutate(rows):
            rows[0]["P1"] = "N/A"
        mutate_csv(built["paths"]["formal_raw"], mutate)

    expect_error("适用项写 N/A → bad-formal-raw", "bad-formal-raw",
                 run(one("raw_na", na_on_p1)))

    def mismatch_pair(built):
        def mutate(rows):
            rows[0]["S2"] = "N/A"
        mutate_csv(built["paths"]["formal_raw"], mutate)

    expect_error("同 blind_id 两人适用性不一致 → bad-formal-raw", "bad-formal-raw",
                 run(one("raw_pair", mismatch_pair)))

    def out_of_range(built):
        def mutate(rows):
            rows[0]["P2"] = "9"
        mutate_csv(built["paths"]["formal_raw"], mutate)

    expect_error("1–5 越界 → out-of-range", "out-of-range",
                 run(one("raw_range", out_of_range)))

    def drop_two_rows(built):
        def mutate(rows):
            del rows[0:2]
        mutate_csv(built["paths"]["formal_raw"], mutate)

    expect_error("缺 blind_id → bad-formal-raw", "bad-formal-raw",
                 run(one("raw_missing", drop_two_rows)))


def test_applicability_counts():
    """P3 适用 72 份、S3_flag 24 份、S4_verdict 12 份（每份 2 行）。"""
    fixture = make_fixture(TMP_ROOT / "appl", overrides=e2e_overrides())
    mapping = A.load_mapping(fixture["paths"]["mapping"])
    formal = A.load_raw_ratings(fixture["paths"]["formal_raw"], mapping, "formal")
    counts = {}
    for item in A.EXPECTED_APPLICABLE:
        counts[item] = sum(
            1 for group in formal.values()
            if any(row["_parsed_" + item][0] == "score" for row in group))
    check("适用份数 P1/P2/P4/S2 各 84",
          all(counts[i] == 84 for i in ("P1", "P2", "P4", "S2")), counts)
    check("适用份数 P3=72（S7 不评）", counts["P3"] == 72, counts)
    check("适用份数 S3_flag=24", counts["S3_flag"] == 24, counts)
    check("适用份数 S4_verdict=12", counts["S4_verdict"] == 12, counts)


def test_h5_with_results():
    fixture = make_fixture(TMP_ROOT / "h5_ok", overrides=e2e_overrides(),
                           with_results=True)
    out_dir = TMP_ROOT / "h5_ok_out"
    code, _ = run_main(fixture, out_dir,
                       extra=("--results", str(fixture["paths"]["results"])))
    check("有 results 时可跑完", code == 0)
    h5 = {row["label"]: row for row in read_csv(out_dir / "h5_results.csv")}
    check("H5 两行（Y3/Y5）", len(h5) == 2, list(h5))
    check("H5 Y3 用对数变换", "log" in h5["Y3(log) D−A"]["metric"], h5)
    check("H5 Y3 m=7", int(h5["Y3(log) D−A"]["m_nonzero"]) == 7, h5["Y3(log) D−A"])
    check("H5 Y3 p=0.015625",
          close(float(h5["Y3(log) D−A"]["p_raw"]), 0.015625), h5["Y3(log) D−A"])
    check("H5 Y5 m=7", int(h5["Y5 D−A"]["m_nonzero"]) == 7, h5["Y5 D−A"])
    check("H5 家族 Holm 后 0.03125",
          close(float(h5["Y5 D−A"]["p_holm"]), 0.03125), h5["Y5 D−A"])
    check("H5 给 bootstrap CI", h5["Y5 D−A"]["ci_low"] != "" and
          h5["Y5 D−A"]["ci_high"] != "", h5["Y5 D−A"])
    check("Y2 分布 7 行",
          len(read_csv(out_dir / "y2_calls_distribution.csv")) == 7)
    holm = read_csv(out_dir / "holm_adjusted_results.csv")
    check("H5 家族单独成族（3+2=5 行）", len(holm) == 5 and
          sum(1 for row in holm if row["family"] == "H5_Y3_Y5") == 2)
    check("input_hashes 多一条 results",
          len(read_csv(out_dir / "input_hashes.csv")) == 6)

    # 对数定义域不满足 → fail-closed，不加常数
    fixture2 = make_fixture(TMP_ROOT / "h5_zero", overrides=e2e_overrides(),
                            with_results=True, zero_latency=True)
    out_dir2 = TMP_ROOT / "h5_zero_out"
    code2, _ = run_main(fixture2, out_dir2,
                        extra=("--results", str(fixture2["paths"]["results"])))
    check("延迟为 0 时仍可跑完（不崩）", code2 == 0)
    h5b = {row["label"]: row for row in read_csv(out_dir2 / "h5_results.csv")}
    y3 = h5b["Y3(log) D−A"]
    check("Y3 定义域不满足 → not_executed",
          y3["status"] == "not_executed" and
          y3["reason_code"] == "log-domain-violation", y3)
    check("Y3 fail-closed 不加常数、不给 p", y3["p_raw"] == "", y3)
    check("Y3 失败不影响 Y5", h5b["Y5 D−A"]["status"] == "ok", h5b["Y5 D−A"])


def test_results_mismatch():
    fixture = make_fixture(TMP_ROOT / "res_bad", overrides=e2e_overrides(),
                           with_results=True)
    rows = [json.loads(line) for line in
            fixture["paths"]["results"].read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows[0]["prompt_sha256"] = "0" * 64
    fixture["paths"]["results"].write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                  for row in rows) + "\n", encoding="utf-8")
    expect_error("results 与 mapping 的 prompt 哈希不一致 → fail-closed",
                 "results-mapping-mismatch",
                 lambda: run_main(fixture, TMP_ROOT / "res_bad_out",
                                  extra=("--results", str(fixture["paths"]["results"]))))

    fixture2 = make_fixture(TMP_ROOT / "res_missing", overrides=e2e_overrides(),
                            with_results=True)
    lines = fixture2["paths"]["results"].read_text(encoding="utf-8").splitlines()
    fixture2["paths"]["results"].write_text("\n".join(lines[1:]) + "\n",
                                            encoding="utf-8")
    expect_error("results 缺行 → fail-closed", "results-mapping-mismatch",
                 lambda: run_main(fixture2, TMP_ROOT / "res_missing_out",
                                  extra=("--results", str(fixture2["paths"]["results"]))))

    broken = TMP_ROOT / "res_broken" / "results.jsonl"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json}\n", encoding="utf-8")
    expect_error("results 非法 JSON → bad-results", "bad-results",
                 lambda: A.load_results([broken], {}))


def test_missing_inputs():
    fixture = make_fixture(TMP_ROOT / "miss", overrides=e2e_overrides())
    fixture["paths"]["mapping"].unlink()
    expect_error("缺 mapping 文件 → missing-input", "missing-input",
                 lambda: run_main(fixture, TMP_ROOT / "miss_out"))


# ===========================================================================
# main
# ===========================================================================


def main():
    print("临时目录：" + str(TMP_ROOT))
    try:
        test_average_ranks()
        test_parse_values()
        test_read_table()
        test_cohens_kappa()
        test_weighted_kappa()
        test_wilcoxon_min_p()
        test_wilcoxon_ties_and_zeros()
        test_wilcoxon_all_zero()
        test_effect_label()
        test_holm()
        test_bootstrap()
        test_chi2_and_friedman()

        fixture, out_dir = test_e2e()
        test_samples_not_mixed(fixture, out_dir)
        test_calibration_and_supplement_excluded(fixture, out_dir)
        test_overwrite_protection(fixture, out_dir)
        test_input_hashes_unchanged(fixture, out_dir)
        test_applicability_counts()

        test_pending_denominator()
        test_pending_blocks_hypothesis()
        test_adjudication_flow()
        test_mapping_fail_closed()
        test_raw_ratings_fail_closed()
        test_h5_with_results()
        test_results_mismatch()
        test_missing_inputs()
    finally:
        print("")
        print("PASSED %d" % len(PASSED))
        print("FAILED %d" % len(FAILED))
        for name, detail in FAILED:
            print("  - %s :: %s" % (name, detail))
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        print("已清理临时目录：" + str(TMP_ROOT))
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        sys.exit(2)
