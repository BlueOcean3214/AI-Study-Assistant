#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_memory.py（M1–M4）与 check_auto.py 的自写回归测试（无 pytest，直接 python 运行）。

全部使用临时目录与合成数据，不重建正式实验库、不写 runs/、不改正式数据。

覆盖：
T1  M1：按类型的 P/R/F1、缺失类型的 N/A 口径、4 类 Macro-F1 与 task_topic 单报。
T2  M1：task_topic 的 subject_key 必须与 normalize_subject() 实际输出一致。
T3  M2：证据合法率（含当前 episode 过滤）、非法反馈 ID、contradict 一致率、Jaccard=N/A。
T4  M3：S7-empty |P|=0 记 1/0；S7 前缀与 strength≤0.6；非 S7 样本不适用。
T5  M4：weakened/resolved 未注入与已归档记忆的缺失诊断、多余记忆。
T6  M1/M2 汇总：pooled_scores 的批次 P/R/F1 与 metric_rows CSV 长表。
T7  M3 断言词扫描：只读、只扫 S7/S7-empty 的 D 组、命中计数与原文、扫描前后 SHA-256 一致。
T8  check_auto.read_rows：合法行通过；非法字段逐项进 problems。
T9  check_auto.analyze_row / metric_rows_for_scope：无夹具时 Y1=N/A、无 Prompt 时 grounding=UNVERIFIABLE，
    且不把 N/A 写成 PASS；write_csv 可回读。
"""

import csv
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
GRADUATION_DIR = PROJECT_DIR / "experiments" / "graduation"
sys.path.insert(0, str(GRADUATION_DIR))

TMP_ROOT = Path(tempfile.mkdtemp(prefix="grad_checks_"))
os.environ["FEEDBACK_DB_PATH"] = str(TMP_ROOT / "graduation_experiments" / "_test_placeholder.db")

import check_memory as cm  # noqa: E402
import check_auto as ca  # noqa: E402
import seed_fixture  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"PASS {name}")
    else:
        FAILED.append((name, detail))
        print(f"FAIL {name} :: {detail}")


def gold(memory_type, key, label, status="active", strength=1.0, contradicts=0,
         injected=True, supports=1, unit="count", has_prefix=False, content="记录"):
    return {
        "memory_type": memory_type,
        "subject_key": key,
        "subject_label": label,
        "status": status,
        "strength": strength,
        "supports": supports,
        "contradicts": contradicts,
        "unit": unit,
        "has_prefix": has_prefix,
        "injected": injected,
        "content": content,
    }


def live(memory_type, key, label, status="active", strength=1.0, supports=1,
         contradicts=0, episode=1, content="记录"):
    return {
        "id": 0,
        "memory_type": memory_type,
        "subject_key": key,
        "subject_label": label,
        "status": status,
        "strength": strength,
        "content": content,
        "value": {},
        "evidence_relations": {"supports": supports, "contradicts": contradicts},
        "episode": episode,
    }


def sample(sample_id="S1", memories=None, must_not_exist=None):
    return {
        "sample_id": sample_id,
        "gold_standard": {
            "memories": memories or [],
            "must_not_exist": must_not_exist or [],
            "simulation": True,
        },
        "simulation": {"is_simulated": True},
    }


def make_db(path, memories, evidence, feedback_ids):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE feedbacks (id INTEGER PRIMARY KEY);
        CREATE TABLE learning_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            subject_label TEXT,
            status TEXT NOT NULL,
            strength REAL NOT NULL,
            episode INTEGER NOT NULL DEFAULT 1,
            content TEXT
        );
        CREATE TABLE memory_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            feedback_id INTEGER NOT NULL,
            plan_id INTEGER,
            episode INTEGER NOT NULL DEFAULT 1,
            relation TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL
        );
        """
    )
    for feedback_id in feedback_ids:
        conn.execute("INSERT INTO feedbacks (id) VALUES (?)", (feedback_id,))
    id_by_key = {}
    for memory in memories:
        cursor = conn.execute(
            "INSERT INTO learning_memories "
            "(memory_type, subject_key, subject_label, status, strength, episode, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                memory["memory_type"],
                memory["subject_key"],
                memory["subject_label"],
                memory["status"],
                memory["strength"],
                memory.get("episode", 1),
                memory["content"],
            ),
        )
        id_by_key[(memory["memory_type"], memory["subject_key"])] = cursor.lastrowid
    for item in evidence:
        conn.execute(
            "INSERT INTO memory_evidence "
            "(memory_id, feedback_id, episode, relation, weight, created_at) "
            "VALUES (?, ?, ?, ?, 1.0, '2026-11-13T00:00:00')",
            (
                id_by_key[(item["memory_type"], item["subject_key"])],
                item["feedback_id"],
                item.get("episode", 1),
                item["relation"],
            ),
        )
    conn.commit()
    conn.close()
    return id_by_key


class FakeMemoryService:
    @staticmethod
    def normalize_subject(task):
        label = " ".join(str(task).split()).strip()
        key = label
        if key.endswith("复习") and len(key) > 2:
            key = key[: -len("复习")].strip()
        return key, label


# ---------------------------------------------------------------------------
# T1 / T2 M1
# ---------------------------------------------------------------------------

def test_m1():
    golds = [
        gold("difficulty", "medium", "中等"),
        gold("task_performance", "partial", "部分完成"),
        gold("time_pattern", "evening", "晚上"),
        gold("task_topic", "数据结构", "数据结构", has_prefix=True),
    ]
    lives = [
        live("difficulty", "medium", "中等"),
        live("task_performance", "partial", "部分完成"),
        live("time_pattern", "evening", "晚上"),
        live("task_topic", "数据结构", "数据结构", content="初步观察：数据结构"),
    ]
    result = cm.memory_type_scores(sample(memories=golds), lives, FakeMemoryService())
    check("T1 四类全对 Macro-F1=1", result["macro_f1"] == 1.0, result["macro_f1"])
    check("T1 task_topic 单报 F1=1", result["task_topic"]["f1"] == 1.0, result["task_topic"])

    # 去掉 time_pattern 与 task_topic，并多一条 difficulty：time_pattern 有 Gold 无预测
    lives_with_extra = lives[:2] + [live("difficulty", "hard", "困难")]
    result = cm.memory_type_scores(sample(memories=golds), lives_with_extra, FakeMemoryService())
    diff = result["types"]["difficulty"]
    check("T1 多一条 difficulty → P=0.5", diff["precision"] == 0.5, diff)
    check("T1 多一条 difficulty → R=1", diff["recall"] == 1.0, diff)
    check("T1 多一条 difficulty → F1=2/3", abs(diff["f1"] - 2 / 3) < 1e-9, diff)
    check("T1 缺 time_pattern → P 记 None", result["types"]["time_pattern"]["precision"] is None,
          result["types"]["time_pattern"])
    check("T1 缺 time_pattern → R=0", result["types"]["time_pattern"]["recall"] == 0.0,
          result["types"]["time_pattern"])
    check("T1 缺类型时 4 类 Macro-F1=N/A", result["macro_f1"] is None, result["macro_f1"])
    check("T1 缺类型时 Macro-F1(已定义) 可计算", result["macro_f1_partial"] is not None,
          result["macro_f1_partial"])

    # T2 归一化：label 带「复习」后缀，key 是不带后缀的规范形
    golds2 = [gold("task_topic", "数据结构", "数据结构 复习")]
    lives2 = [live("task_topic", "数据结构", "数据结构 复习")]
    result2 = cm.memory_type_scores(sample(memories=golds2), lives2, FakeMemoryService())
    check("T2 Gold/Hi 的 label 经 normalize_subject 与 key 一致",
          result2["normalization_mismatches"] == [] and result2["normalization_checked"] is True,
          (result2["normalization_mismatches"], result2["normalization_checked"]))

    golds3 = [gold("task_topic", "错键", "数据结构 复习")]
    result3 = cm.memory_type_scores(sample(memories=golds3), [], FakeMemoryService())
    check("T2 记录键与归一化输出不一致会被报告",
          len(result3["normalization_mismatches"]) == 1
          and result3["normalization_mismatches"][0]["normalized"] == "数据结构",
          result3["normalization_mismatches"])

    # 非 task_topic 不拿 normalize_subject 硬套（用探针确认没被调用）
    calls = []

    class RecordingService:
        @staticmethod
        def normalize_subject(task):
            calls.append(task)
            return "WRONG", "WRONG"

    golds4 = [gold("difficulty", "medium", "中等")]
    lives4 = [live("difficulty", "medium", "中等")]
    result4 = cm.memory_type_scores(sample(memories=golds4), lives4, RecordingService())
    check("T2 difficulty 不参与 normalize_subject 比较",
          result4["normalization_mismatches"] == [] and calls == [], (result4, calls))


# ---------------------------------------------------------------------------
# T3 M2
# ---------------------------------------------------------------------------

def test_m2():
    db_path = TMP_ROOT / "t3.db"
    memories = [
        live("difficulty", "medium", "中等", supports=2),
        live("task_performance", "partial", "部分完成", contradicts=1),
    ]
    make_db(
        db_path,
        memories,
        [
            {"memory_type": "difficulty", "subject_key": "medium", "feedback_id": 1, "relation": "supports"},
            {"memory_type": "difficulty", "subject_key": "medium", "feedback_id": 2, "relation": "supports"},
            {"memory_type": "difficulty", "subject_key": "medium", "feedback_id": 99, "relation": "supports"},
            # 历史 episode 的非法证据不应计入当前 episode
            {"memory_type": "difficulty", "subject_key": "medium", "feedback_id": 98,
             "relation": "supports", "episode": 2},
            {"memory_type": "task_performance", "subject_key": "partial", "feedback_id": 2,
             "relation": "supports"},
            {"memory_type": "task_performance", "subject_key": "partial", "feedback_id": 3,
             "relation": "contradicts"},
        ],
        feedback_ids=[1, 2, 3],
    )
    golds = [
        gold("difficulty", "medium", "中等", contradicts=0),
        gold("task_performance", "partial", "部分完成", contradicts=1),
    ]
    audit = cm.evidence_audit(sample(memories=golds), db_path, memories, {1, 2, 3})
    check("T3 当前 episode 的 supports 共 3 条（历史 episode 不计）",
          audit["evidence_ids_total"] == 3, audit["evidence_ids_total"])
    check("T3 非法反馈 ID 被识别", len(audit["illegal"]) == 1 and audit["illegal"][0]["feedback_id"] == 99,
          audit["illegal"])
    check("T3 合法率 = 2/3", abs(audit["legit_rate"] - 2 / 3) < 1e-9, audit["legit_rate"])
    check("T3 contradict 一致率 = 1", audit["contradict_ratio"] == 1.0, audit["contradict_ratio"])
    check("T3 Jaccard 无 E_gold 时记 N/A 且带原因",
          audit["jaccard"] is None and "E_gold" in audit["jaccard_reason"], audit["jaccard_reason"])

    golds[1]["contradicts"] = 2
    audit2 = cm.evidence_audit(sample(memories=golds), db_path, memories, {1, 2, 3})
    check("T3 contradict 不一致时一致率 = 0.5", audit2["contradict_ratio"] == 0.5,
          audit2["contradict_ratio"])
    check("T3 不一致明细保留期望/实际",
          audit2["contradict_mismatches"][0]["expected"] == 2
          and audit2["contradict_mismatches"][0]["actual"] == 1,
          audit2["contradict_mismatches"])


# ---------------------------------------------------------------------------
# T4 M3
# ---------------------------------------------------------------------------

def test_m3():
    empty_ok = cm.s7_checks(sample("S7-empty"), [])
    check("T4 S7-empty 无记忆 → 1", empty_ok["score"] == 1 and empty_ok["ok"], empty_ok)
    empty_bad = cm.s7_checks(sample("S7-empty"), [live("difficulty", "medium", "中等")])
    check("T4 S7-empty 有记忆 → 0（不冒充通过）", empty_bad["score"] == 0, empty_bad)

    tentative = live(
        "difficulty", "数据结构", "数据结构", strength=0.5, content="初步观察：数据结构还不稳"
    )
    good = cm.s7_checks(sample("S7"), [tentative])
    check("T4 S7 前缀+strength≤0.6 → 通过", good["ok"] and good["score"] == 1, good)

    no_prefix = live("difficulty", "数据结构", "数据结构", strength=0.5, content="数据结构还不稳")
    bad_prefix = cm.s7_checks(sample("S7"), [no_prefix])
    check("T4 S7 缺「初步观察：」→ 失败", not bad_prefix["ok"] and not bad_prefix["rows"][0]["prefix_ok"],
          bad_prefix)

    too_strong = live(
        "difficulty", "数据结构", "数据结构", strength=0.61, content="初步观察：数据结构还不稳"
    )
    bad_strength = cm.s7_checks(sample("S7"), [too_strong])
    check("T4 S7 strength=0.61 → 失败", not bad_strength["ok"] and not bad_strength["rows"][0]["strength_ok"],
          bad_strength)

    not_applicable = cm.s7_checks(sample("S1"), [tentative])
    check("T4 其它样本 M3 不适用（None，不记 0）",
          not_applicable["applicable"] is False and not_applicable["score"] is None, not_applicable)


# ---------------------------------------------------------------------------
# T5 M4
# ---------------------------------------------------------------------------

def test_m4():
    golds = [
        gold("difficulty", "medium", "中等", status="active", strength=0.8, injected=True),
        gold("task_performance", "partial", "部分完成", status="weakened", strength=0.27,
             injected=False),
        gold("time_pattern", "evening", "晚上", status="resolved", strength=0.11, injected=False),
    ]
    memories = [
        live("difficulty", "medium", "中等", status="active", strength=0.8),
        live("task_performance", "partial", "部分完成", status="weakened", strength=0.27),
        live("time_pattern", "evening", "晚上", status="resolved", strength=0.11),
    ]
    injected = {("difficulty", "medium")}
    audit = cm.state_audit(sample(memories=golds), memories, injected)
    check("T5 weakened/resolved 三项一致", audit["consistent"] == 3 and audit["ratio"] == 1.0, audit)

    # weakened/resolved 不得出现在注入集合
    audit_bad = cm.state_audit(sample(memories=golds), memories,
                               {("difficulty", "medium"), ("time_pattern", "evening")})
    check("T5 resolved 被注入会判失败",
          audit_bad["consistent"] == 2 and audit_bad["inconsistent"][0]["subject_key"] == "evening",
          audit_bad["inconsistent"])

    archived = [dict(item) for item in memories if item["subject_key"] == "medium"]
    archived[0]["status"] = "archived"
    audit_arch = cm.state_audit(sample(memories=golds), archived + memories[1:], set())
    archived_row = [row for row in audit_arch["rows"] if row["subject_key"] == "medium"][0]
    check("T5 已归档记忆报「记忆被归档」而不是静默通过",
          archived_row["cause"] == "记忆被归档" and not archived_row["ok"], archived_row)

    extra = list(memories) + [live("task_topic", "数据结构", "数据结构")]
    audit_extra = cm.state_audit(sample(memories=golds), extra, injected)
    extra_row = [row for row in audit_extra["rows"] if row["subject_key"] == "数据结构"][0]
    check("T5 多余记忆进入不一致并标注诊断",
          extra_row["cause"] == "多余记忆（Gold 未期望）" and audit_extra["consistent"] == 3,
          extra_row)


# ---------------------------------------------------------------------------
# T6 M1/M2 汇总 + CSV
# ---------------------------------------------------------------------------

def test_pooled_and_csv():
    config = {"samples": ["S1", "S4"]}
    db_path = TMP_ROOT / "t6.db"
    make_db(db_path, [], [], feedback_ids=[])

    def item(sample_id, m1):
        return {
            "sample_id": sample_id,
            "m1": m1,
            "m2": {
                "legit_rate": 1.0, "legal_ids": 2, "evidence_ids_total": 2,
                "seed_feedback_count": 2, "illegal": [],
                "jaccard": None, "jaccard_reason": "无 E_gold",
                "contradict_ratio": 1.0, "contradict_matches": 1, "contradict_total": 1,
                "contradict_mismatches": [], "missing_memories": 0,
            },
            "m3": {"kind": "none", "applicable": False, "ok": None, "n": 0, "rows": [],
                   "score": None, "detail": {}},
            "m4": {
                "rows": [], "consistent": 1, "total": 1, "ratio": 1.0,
                "by_field": {name: {"matched": 1, "total": 1} for name in
                             ("status", "strength", "injected")},
                "inconsistent": [], "diagnostic_counts": {},
            },
            "tool_check": {"attempted": False, "ok": False, "error": "未调用"},
        }

    def m1_for(tp, fp, fn):
        types = {}
        for name in cm.MEMORY_TYPES:
            if name == "difficulty":
                predicted, expected, hit = tp + fp, tp + fn, tp
            else:
                predicted, expected, hit = 1, 1, 1
            precision = hit / predicted if predicted else None
            recall = hit / expected if expected else None
            types[name] = {
                "precision": precision, "recall": recall,
                "f1": cm._f1(precision, recall), "predicted": predicted,
                "expected": expected, "true_positive": hit,
                "false_positive": predicted - hit, "false_negative": expected - hit,
            }
        defined = [types[name]["f1"] for name in cm.MEMORY_TYPES if types[name]["f1"] is not None]
        macro = sum(defined) / 4 if len(defined) == 4 else None
        return {
            "types": types, "macro_f1": macro,
            "macro_f1_partial": sum(defined) / len(defined) if defined else None,
            "macro_defined": len(defined), "macro_n": 4,
            "task_topic": types["task_topic"], "normalization_mismatches": [],
            "normalization_checked": 0,
        }

    results = [
        item("S1", m1_for(tp=2, fp=0, fn=0)),
        item("S4", m1_for(tp=1, fp=1, fn=1)),
    ]
    pooled = cm.pooled_scores(results, config)
    diff = pooled["main"]["types"]["difficulty"]
    check("T6 汇总 difficulty TP=3 FP=1 FN=1",
          (diff["true_positive"], diff["false_positive"], diff["false_negative"]) == (3, 1, 1), diff)
    check("T6 汇总 Macro-F1 使用四类齐全口径", pooled["main"]["macro_f1"] is not None,
          pooled["main"]["macro_f1"])

    rows = cm.metric_rows(results, config)
    keys = {(row["sample_id"], row["metric"]) for row in rows}
    check("T6 长表含逐样本与汇总行",
          ("S1", "M1.macro_f1") in keys and ("ALL", "M1.macro_f1") in keys, sorted(keys)[:5])
    check("T6 Jaccard 行写 N/A 而不是 0",
          any(row["sample_id"] == "S1" and row["metric"] == "M2.evidence_jaccard"
              and row["value"] == "" and "E_gold" in row["note"] for row in rows), rows[:2])

    csv_path = TMP_ROOT / "metrics.csv"
    cm.write_metrics_csv(rows, csv_path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        read_back = list(csv.DictReader(handle))
    check("T6 CSV 列与行数可回读",
          list(read_back[0].keys()) == ["scope", "sample_id", "metric", "value", "n",
                                         "denominator", "unit", "note"]
          and len(read_back) == len(rows), (len(read_back), len(rows)))


# ---------------------------------------------------------------------------
# T7 M3 断言词扫描
# ---------------------------------------------------------------------------

def test_scan():
    runs_dir = TMP_ROOT / "runs"
    run_dir = runs_dir / "main-test"
    run_dir.mkdir(parents=True)
    results_path = run_dir / "results.jsonl"
    rows = [
        {"sample_id": "S7", "group": "D", "repeat_index": 0,
         "final_output": {"answer": "嗯。你一直都是这样，你一直不改。"}},
        {"sample_id": "S7", "group": "C", "repeat_index": 0,
         "final_output": {"answer": "你一直（C 组不扫）"}},
        {"sample_id": "S4", "group": "D", "repeat_index": 0,
         "final_output": {"answer": "根据你的学习记忆（S4 不扫）"}},
        {"sample_id": "S7-empty", "group": "D", "repeat_index": 1,
         "final_output": {"answer": "先做一题看看。"}},
    ]
    results_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    before = hashlib.sha256(results_path.read_bytes()).hexdigest()
    scan = cm.scan_assertion_keywords(runs_dir)
    after = hashlib.sha256(results_path.read_bytes()).hexdigest()
    check("T7 只扫 S7/S7-empty 的 D 组（2 行）", scan["ok"] and scan["rows_checked"] == 2, scan)
    check("T7 关键词计数正确（你一直×2）", scan["keyword_totals"].get("你一直") == 2,
          scan["keyword_totals"])
    check("T7 命中保留原文片段",
          scan["hits"] and "你一直以来" not in scan["hits"][0]["snippet"]
          and "你一直" in scan["hits"][0]["snippet"], scan["hits"])
    check("T7 扫描前后 SHA-256 一致（只读）", before == after, (before, after))

    missing = cm.scan_assertion_keywords(TMP_ROOT / "no-such-runs")
    check("T7 runs 不存在时显式失败而不是 0 命中", missing["ok"] is False and missing["reason"],
          missing)


# ---------------------------------------------------------------------------
# T8 check_auto.read_rows
# ---------------------------------------------------------------------------

def _row(**overrides):
    row = {
        "sample_id": "S1",
        "group": "A",
        "experiment_id": "exp-1",
        "repeat_index": 0,
        "success": True,
        "error_type": None,
        "request_count": 1,
        "corrections_used": 0,
        "elapsed_seconds": 1.5,
        "final_output": {"answer": "好", "plan": {"steps": []}},
        "prompt_sha256": "a" * 64,
    }
    row.update(overrides)
    return row


def test_read_rows():
    run_dir = TMP_ROOT / "rows-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        _row(),
        _row(group="Z"),
        _row(repeat_index=7),
        _row(request_count=0),
        _row(final_output={"answer": 1}),
        _row(prompt_sha256="abc"),
        _row(plan_seq="0"),
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n{broken\n",
        encoding="utf-8",
    )
    parsed, problems = ca.read_rows(run_dir)
    fields = {field for _, field, _ in problems}
    check("T8 合法行被读出", len(parsed) == 7 and parsed[0][1]["sample_id"] == "S1", len(parsed))
    check("T8 非法 group / repeat / request_count 全被记录",
          {"group", "repeat_index", "request_count"} <= fields, fields)
    check("T8 非法 answer / prompt_sha256 / plan_seq 全被记录",
          {"final_output.answer", "prompt_sha256", "plan_seq"} <= fields, fields)
    check("T8 坏行按行列号报 json 错误",
          any(field == "json" and number == 8 for number, field, _ in problems), problems)


# ---------------------------------------------------------------------------
# T9 check_auto 分析链路
# ---------------------------------------------------------------------------

def test_auto_analysis():
    config = seed_fixture.load_config(None)
    run_dir = TMP_ROOT / "analysis-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    analysis, failures = ca.analyze_row(
        "analysis-run", run_dir, 1, _row(), None, config, {}, None, ()
    )
    check("T9 无夹具时 Y1 记 UNAVAILABLE（N/A），不冒充通过",
          analysis["y1"] == "UNAVAILABLE", analysis["y1"])
    check("T9 无 Prompt 文件时 grounding=UNVERIFIABLE",
          analysis["grounding"] == "UNVERIFIABLE", analysis["grounding"])
    check("T9 无 Prompt 时 grounding 计入失败清单（不是 PASS）",
          any(item["metric"] == "grounding" for item in failures), failures)
    check("T9 无 requests 时 prompt_tokens 记 None",
          analysis["prompt_tokens"] is None, analysis["prompt_tokens"])

    rows = ca.metric_rows_for_scope("main", [analysis])
    by_metric = {row["metric"]: row for row in rows}
    check("T9 Y1 汇总是 N/A 而不是 0",
          by_metric["Y1_structural_saveable_rate"]["value"] == "N/A",
          by_metric["Y1_structural_saveable_rate"])
    check("T9 grounding 汇总把 UNVERIFIABLE 排除出分母（无可判定行 → N/A）",
          by_metric["grounding_pass_rate"]["value"] == "N/A"
          and "UNVERIFIABLE" in by_metric["grounding_pass_rate"]["note"],
          by_metric["grounding_pass_rate"])
    check("T9 Y2/Y3 仍可计算（1 次 / 1.5s）",
          by_metric["Y2_calls_mean"]["value"] == 1 and by_metric["Y3_latency_median_s"]["value"] == 1.5,
          (by_metric["Y2_calls_mean"], by_metric["Y3_latency_median_s"]))

    csv_path = TMP_ROOT / "auto.csv"
    ca.write_csv(csv_path, ["table", "metric", "value"], rows[:2])
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        read_back = list(csv.DictReader(handle))
    check("T9 check_auto.write_csv 可回读", len(read_back) == 2 and "metric" in read_back[0], read_back)


def main():
    test_m1()
    test_m2()
    test_m3()
    test_m4()
    test_pooled_and_csv()
    test_scan()
    test_read_rows()
    test_auto_analysis()

    print()
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"  FAILED {name}: {detail}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
