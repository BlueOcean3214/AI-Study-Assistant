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
T11 check_auto 的 Prompt 归档：parse_prompt_archive / parse_memory_payload / verify_prompt_hash /
    grounding_for_row 的 PASS / FAIL / UNVERIFIABLE 分支，调用真实 agent_plan_draft.check_grounding。
"""

import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
GRADUATION_DIR = PROJECT_DIR / "experiments" / "graduation"
sys.path.insert(0, str(GRADUATION_DIR))

TMP_ROOT = Path(tempfile.mkdtemp(prefix="grad_checks_"))
os.environ["FEEDBACK_DB_PATH"] = str(TMP_ROOT / "graduation_experiments" / "_test_placeholder.db")

sys.path.insert(0, str(PROJECT_DIR))

import check_memory as cm  # noqa: E402
import check_auto as ca  # noqa: E402
import seed_fixture  # noqa: E402
import build_prompts as bp  # noqa: E402

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
        result = {
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
                   "score": None, "detail": {"reason": "非 S7 样本不适用"}},
            "m4": {
                "rows": [], "consistent": 1, "total": 1, "ratio": 1.0,
                "by_field": {name: {"matched": 1, "total": 1} for name in
                             ("status", "strength", "injected")},
                "inconsistent": [], "diagnostic_counts": {},
            },
            "tool_check": {"attempted": False, "ok": False, "error": "未调用"},
            "rows": [],
        }
        # 与 check_sample() 的产物保持同一形状：summary 只接受带计数字段的结果
        result["gold_failure_count"] = 0
        result["metric_checks"] = cm.metric_status_checks(result)
        result["metric_failure_count"] = sum(
            1 for entry in result["metric_checks"]
            if entry["status"] == cm.STATUS_FAIL and entry["hard"]
        )
        result["failure_count"] = result["gold_failure_count"] + result["metric_failure_count"]
        return result

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


def test_status_accounting():
    """T10：M1–M4 的状态必须真的进 failure_count 与退出码，不能只写在文字里。"""

    def base_item(sample_id="S1"):
        return {
            "sample_id": sample_id,
            "rows": [
                {"key": ("difficulty", "概率论"), "memory_type": "difficulty",
                 "subject_key": "概率论"},
            ],
            "m1": {
                "types": {}, "macro_f1": 1.0, "macro_f1_partial": 1.0,
                "macro_defined": 4, "macro_n": 4, "task_topic": {},
                "normalization_mismatches": [], "normalization_checked": 1,
            },
            "m2": {
                "legit_rate": 1.0, "legal_ids": 1, "evidence_ids_total": 1,
                "seed_feedback_count": 1, "illegal": [], "jaccard": None,
                "jaccard_reason": "无 E_gold", "contradict_ratio": 1.0,
                "contradict_matches": 1, "contradict_total": 1,
                "contradict_mismatches": [], "missing_memories": 0,
            },
            "m3": {"kind": "none", "applicable": False, "ok": None, "n": 0,
                   "rows": [], "score": None, "detail": {"reason": "非 S7 样本不适用"}},
            "m4": {
                "rows": [], "consistent": 1, "total": 1, "ratio": 1.0,
                "by_field": {name: {"matched": 1, "total": 1} for name in
                             ("status", "strength", "injected")},
                "inconsistent": [], "diagnostic_counts": {},
            },
            "tool_check": {
                "attempted": True, "ok": True, "matches_injected": True,
                "returned_count": 1, "returned_labels": ["difficulty/概率论"],
                "expected_count": 1, "evidence_ids_total": 1,
                "evidence_legit_rate": 1.0,
            },
        }

    def finish(item, gold=0):
        item["gold_failure_count"] = gold
        item["metric_checks"] = cm.metric_status_checks(item)
        item["metric_failure_count"] = sum(
            1 for entry in item["metric_checks"]
            if entry["status"] == cm.STATUS_FAIL and entry["hard"]
        )
        item["failure_count"] = gold + item["metric_failure_count"]
        return item

    # ① 全 PASS：状态 PASS、退出码 0
    ok_item = finish(base_item())
    summary = cm.summarize_results([ok_item], scan={"ok": True, "hits": [], "rows_checked": 3})
    check("T10 全部 PASS → status=PASS/exit 0",
          (summary["status"], summary["exit_code"]) == (cm.STATUS_PASS, cm.EXIT_PASS), summary)

    # ② 注入状态不一致 → M4 硬失败必须进 metric_failure_count 与退出码
    bad = base_item("S4")
    bad["m4"]["inconsistent"] = [{
        "memory_type": "difficulty", "subject_key": "概率论",
        "expected_injected": False, "actual_injected": True,
        "checks": [{"field": "injected", "ok": False}],
    }]
    bad_item = finish(bad)
    injected_checks = [c for c in bad_item["metric_checks"] if c["metric"] == "注入状态与实际注入"]
    check("T10 注入不一致 → M4 记为硬失败",
          injected_checks and injected_checks[0]["status"] == cm.STATUS_FAIL
          and injected_checks[0]["hard"], injected_checks)
    check("T10 硬失败进入 metric_failure_count 与 failure_count",
          bad_item["metric_failure_count"] >= 1
          and bad_item["failure_count"] == bad_item["gold_failure_count"] + bad_item["metric_failure_count"],
          (bad_item["metric_failure_count"], bad_item["failure_count"]))
    summary_bad = cm.summarize_results([bad_item], scan={"ok": True, "hits": [], "rows_checked": 3})
    check("T10 硬失败 → status=FAIL/exit 1",
          (summary_bad["status"], summary_bad["exit_code"]) == (cm.STATUS_FAIL, cm.EXIT_FAIL),
          summary_bad)

    # ③ --no-tool：M4 的 Tool 核对是 INCOMPLETE，整套不能报 PASS
    no_tool = base_item("S5")
    no_tool["tool_check"] = {
        "attempted": False, "ok": False,
        "error": "本次运行显式跳过真实 Tool 调用（--no-tool）",
    }
    no_tool_item = finish(no_tool)
    tool_checks = [c for c in no_tool_item["metric_checks"] if c["metric"] == "真实 get_learning_memory() 核对"]
    check("T10 --no-tool 时 M4 Tool 核对记 INCOMPLETE 而不是 PASS",
          tool_checks and tool_checks[0]["status"] == cm.STATUS_INCOMPLETE,
          tool_checks)
    check("T10 INCOMPLETE 不计硬失败，但样本状态不是 PASS",
          no_tool_item["metric_failure_count"] == 0
          and cm._sample_status(no_tool_item) == cm.STATUS_INCOMPLETE,
          cm._sample_status(no_tool_item))
    summary_no_tool = cm.summarize_results([no_tool_item],
                                           scan={"ok": True, "hits": [], "rows_checked": 3})
    check("T10 --no-tool → status=INCOMPLETE/exit 2（不冒充全绿）",
          (summary_no_tool["status"], summary_no_tool["exit_code"])
          == (cm.STATUS_INCOMPLETE, cm.EXIT_INCOMPLETE)
          and summary_no_tool["incomplete_count"] >= 1,
          summary_no_tool)

    # ④ 未扫描 runs/：同样是 INCOMPLETE，不是 PASS
    summary_no_scan = cm.summarize_results([ok_item], scan=None)
    check("T10 未扫描断言词 → INCOMPLETE/exit 2",
          (summary_no_scan["status"], summary_no_scan["exit_code"])
          == (cm.STATUS_INCOMPLETE, cm.EXIT_INCOMPLETE)
          and any(c["status"] == cm.STATUS_INCOMPLETE for c in summary_no_scan["global_checks"]),
          summary_no_scan["status"])

    # ⑤ 断言词命中：MANUAL_REVIEW，不升级成 FAIL 也不降级成 PASS
    summary_manual = cm.summarize_results(
        [ok_item], scan={"ok": True, "hits": [{"line": 1}], "rows_checked": 3}
    )
    check("T10 断言词命中 → MANUAL_REVIEW/exit 3",
          (summary_manual["status"], summary_manual["exit_code"])
          == (cm.STATUS_MANUAL, cm.EXIT_MANUAL), summary_manual["status"])

    # ⑥ 形状不对的结果必须显式报错，不能用 0 兜底当成没有失败
    broken = dict(base_item("S6"))
    for name in ("gold_failure_count", "metric_failure_count", "failure_count"):
        broken.pop(name, None)
    try:
        cm.summarize_results([broken], scan={"ok": True, "hits": [], "rows_checked": 3})
    except ValueError as error:
        check("T10 缺计数字段 → ValueError 指出样本",
              "S6" in str(error) and "failure_count" in str(error), str(error))
    else:
        check("T10 缺计数字段 → ValueError 指出样本", False, "没有报错")


# ---------------------------------------------------------------------------
# T11 check_auto 的 Prompt 归档 / 哈希 / 记忆注入核验
# ---------------------------------------------------------------------------

def _write_prompt(run_dir, sample_id, group, system, user):
    path = Path(run_dir) / "prompts" / str(sample_id) / f"{group}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ca.PROMPT_SYSTEM_PREFIX + system + ca.PROMPT_USER_SEP + user,
        encoding="utf-8",
    )
    return path


def _samples(*sample_ids):
    return {sid: {"sample_id": sid, "feedbacks": []} for sid in sample_ids}


def test_grounding():
    """T11：归档 Prompt 必须能安全拆回 (system, user)、SHA-256 能按同一算法重算一致、
    记忆区块里必须是真实记忆对象；任何一条做不到都不能记 PASS。"""

    import agent_plan_draft as apd

    run_dir = TMP_ROOT / "grounding-run"
    system = "你是学习规划助手。"
    # 与真实归档一致：记忆区块里是完整的公开投影（8 个字段），不是简写版
    memory = [{
        "type": "difficulty",
        "subject": "概率论",
        "content": "条件概率掌握薄弱",
        "strength": 0.6,
        "evidence_count": 2,
        "episode": "ep-1",
        "evidence_feedback_ids": [1, 2],
        "updated_at": "2026-11-01T00:00:00Z",
    }]
    user = (
        "请为学习者制定计划。\n\n"
        + bp.MEMORY_HEADER + "\n"
        + json.dumps(memory, ensure_ascii=False)
    )
    _write_prompt(run_dir, "S1", "C", system, user)
    good = _row(
        sample_id="S1",
        group="C",
        prompt_sha256=bp.prompt_sha256(system, user),
        final_output={
            "answer": "建议先复习条件概率。",
            "plan": {"reason": "按历史情况", "steps": []},
        },
    )
    cache = _samples("S1", "S2", "S3", "S4", "S9")

    status, detail = ca.grounding_for_row(
        good, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 哈希一致 + 记忆可解析 → PASS",
          status == "PASS" and "重算一致" in detail and "记忆 1 条" in detail,
          (status, detail))

    claimed = json.loads(json.dumps(good, ensure_ascii=False))
    claimed["final_output"]["answer"] = "根据学习记忆，你在概率论上比较薄弱。"
    status, detail = ca.grounding_for_row(
        claimed, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 声称记忆来源且有真实注入 → PASS 且标注主张一致",
          status == "PASS" and "来源主张与注入一致" in detail, (status, detail))

    tampered = json.loads(json.dumps(good, ensure_ascii=False))
    tampered["prompt_sha256"] = "0" * 64
    status, detail = ca.grounding_for_row(
        tampered, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 Prompt 被改写（哈希不符）→ UNVERIFIABLE，不冒充 PASS",
          status == "UNVERIFIABLE" and "不一致" in detail, (status, detail))

    empty_user = "请为学习者制定计划。\n\n" + bp.MEMORY_HEADER + "\n[]"
    _write_prompt(run_dir, "S2", "C", system, empty_user)
    empty_row = _row(
        sample_id="S2",
        group="C",
        prompt_sha256=bp.prompt_sha256(system, empty_user),
        final_output={"answer": "好的", "plan": {"reason": "按当前情况", "steps": []}},
    )
    status, detail = ca.grounding_for_row(
        empty_row, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 记忆区块为空数组 → FAIL（取不到真实记忆内容）",
          status == "FAIL" and "取不到真实记忆内容" in detail, (status, detail))

    _write_prompt(run_dir, "S3", "A", system, user)
    a_row = _row(
        sample_id="S3",
        group="A",
        prompt_sha256=bp.prompt_sha256(system, user),
        final_output={"answer": "好的", "plan": {"reason": "按当前情况", "steps": []}},
    )
    status, detail = ca.grounding_for_row(
        a_row, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 A 组 Prompt 出现记忆区块 → FAIL（该组不应注入长期记忆）",
          status == "FAIL" and "不应注入长期记忆" in detail, (status, detail))

    broken_path = Path(run_dir) / "prompts" / "S4" / "C.txt"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text("系统提示\n\n===== USER =====\n用户请求", encoding="utf-8")
    broken_row = _row(sample_id="S4", group="C")
    status, detail = ca.grounding_for_row(
        broken_row, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 缺 `===== SYSTEM =====` 头 → UNVERIFIABLE",
          status == "UNVERIFIABLE" and "格式无法解析" in detail, (status, detail))

    no_hash = json.loads(json.dumps(good, ensure_ascii=False))
    no_hash.pop("prompt_sha256")
    status, detail = ca.grounding_for_row(
        no_hash, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 归档行缺 prompt_sha256 → UNVERIFIABLE",
          status == "UNVERIFIABLE" and "没有 prompt_sha256" in detail, (status, detail))

    missing_row = _row(sample_id="S9", group="C")
    status, detail = ca.grounding_for_row(
        missing_row, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 缺 Prompt 文件 → UNVERIFIABLE",
          status == "UNVERIFIABLE" and "缺少 Prompt 文件" in detail, (status, detail))

    kb = json.loads(json.dumps(good, ensure_ascii=False))
    kb["final_output"]["answer"] = "根据知识库，条件概率需要复习。"
    status, detail = ca.grounding_for_row(
        kb, run_dir, cache, apd.check_grounding, apd.MEMORY_CLAIM_MARKERS
    )
    check("T11 真实 check_grounding 的违例原样进 FAIL",
          status == "FAIL" and "声称依据知识库" in detail, (status, detail))

    archive = ca.PROMPT_SYSTEM_PREFIX + "系统" + ca.PROMPT_USER_SEP + "用户"
    check("T11 parse_prompt_archive 安全拆回 (system, user)",
          ca.parse_prompt_archive(archive) == ("系统", "用户"),
          ca.parse_prompt_archive(archive))

    try:
        ca.parse_prompt_archive(archive + ca.PROMPT_USER_SEP + "重复")
    except ValueError as error:
        check("T11 USER 分隔符出现两次 → ValueError（不做截断式切分）",
              "2 次" in str(error), str(error))
    else:
        check("T11 USER 分隔符出现两次 → ValueError（不做截断式切分）", False, "没有报错")

    for label, bad, expected in (
        ("SYSTEM 为空", ca.PROMPT_SYSTEM_PREFIX + ca.PROMPT_USER_SEP + "用户", "SYSTEM 内容为空"),
        ("USER 为空", ca.PROMPT_SYSTEM_PREFIX + "系统" + ca.PROMPT_USER_SEP, "USER 内容为空"),
    ):
        try:
            ca.parse_prompt_archive(bad)
        except ValueError as error:
            check(f"T11 {label} → ValueError", expected in str(error), str(error))
        else:
            check(f"T11 {label} → ValueError", False, "没有报错")

    archive_row = dict(good, prompt_sha256=bp.prompt_sha256("系统", "用户"))
    actual, error = ca.verify_prompt_hash(archive, archive_row)
    check("T11 verify_prompt_hash 返回与归档一致的重算值",
          error is None and actual == archive_row["prompt_sha256"], (actual, error))

    check("T11 无记忆区块时 parse_memory_payload 返回 (False, [], None)",
          ca.parse_memory_payload("纯用户请求") == (False, [], None))

    present, payload, error = ca.parse_memory_payload(
        "用户请求\n" + bp.MEMORY_HEADER + "\n" + json.dumps(memory, ensure_ascii=False)
    )
    check("T11 parse_memory_payload 还原真实记忆对象",
          present and error is None and payload == memory, (present, payload, error))

    partial = dict(memory[0], content="  ")
    payload_cases = (
        ("没有 JSON 数组", "用户请求\n" + bp.MEMORY_HEADER + "\n没有数组", "找不到 JSON 数组"),
        ("JSON 损坏", "用户请求\n" + bp.MEMORY_HEADER + '\n[{"content": ]', "JSON 解析失败"),
        ("空数组", "用户请求\n" + bp.MEMORY_HEADER + "\n[]", "不是非空数组"),
        ("非对象条目", "用户请求\n" + bp.MEMORY_HEADER + '\n["x"]', "不是 JSON 对象"),
        ("content 为空",
         "用户请求\n" + bp.MEMORY_HEADER + "\n" + json.dumps([partial], ensure_ascii=False),
         "的 content 为空"),
    )
    for label, text, expected in payload_cases:
        present, got, error = ca.parse_memory_payload(text)
        check(f"T11 记忆区块{label} → 显式报错而不是空 payload",
              present and got == [] and expected in (error or ""), (present, got, error))

    analysis, failures = ca.analyze_row(
        "grounding-run", run_dir, 1, good, None,
        seed_fixture.load_config(None), cache, apd.check_grounding,
        apd.MEMORY_CLAIM_MARKERS,
    )
    check("T11 analyze_row 把 grounding PASS 计入分析且不进失败清单",
          analysis["grounding"] == "PASS"
          and not any(item["metric"] == "grounding" for item in failures),
          (analysis["grounding"], failures))


# ---------------------------------------------------------------------------
# T12 check_auto：manifest 不可信必须在算指标前终止
# ---------------------------------------------------------------------------

def _manifest_entry(run_dir, run_name):
    results = run_dir / "results.jsonl"
    return {
        "relative_path": f"experiments/graduation/runs/{run_name}/results.jsonl",
        "sha256": hashlib.sha256(results.read_bytes()).hexdigest(),
        "size_bytes": str(results.stat().st_size),
        "records": "2",
    }


@contextlib.contextmanager
def _patched_manifest(path):
    saved = ca.MANIFEST_PATH
    ca.MANIFEST_PATH = path
    try:
        yield
    finally:
        ca.MANIFEST_PATH = saved


def test_auto_manifest_failclosed():
    runs_dir = TMP_ROOT / "manifest-runs"
    run_name = "manifest-run"
    run_dir = runs_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [_row(repeat_index=0), _row(repeat_index=1, sample_id="S2")]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    before = hashlib.sha256((run_dir / "results.jsonl").read_bytes()).hexdigest()
    config = seed_fixture.load_config(None)
    columns = ["relative_path", "sha256", "size_bytes", "records"]
    manifest = TMP_ROOT / "manifest.csv"

    # 指标计算必须完全没被调用：manifest 不可信时不能先算再说
    saved_analyze = ca.analyze_row

    def forbidden(*args, **kwargs):
        raise AssertionError("manifest 不可信时不得开始计算指标")

    ca.analyze_row = forbidden
    try:
        cases = []

        good = _manifest_entry(run_dir, run_name)
        ca.write_csv(manifest, columns, [good])
        with _patched_manifest(manifest):
            try:
                ca.collect_scope("main", [run_name], runs_dir, None, config, {}, None, ())
                good_status = "no-error"
            except AssertionError:
                # manifest 校验放行后才会去算指标，被这个桩拦住 → 说明这一步校验真的过了
                good_status = "reached-metrics"
            except ca.CheckError as error:
                good_status = str(error)
        assert good_status == "reached-metrics", good_status

        empty_sha = dict(good, sha256="")
        ca.write_csv(manifest, columns, [empty_sha])

        invalid_size = dict(good, size_bytes="")
        # 另一个 path 用来放这一份，避免和后一条互相覆盖
        size_manifest = TMP_ROOT / "manifest-bad-size.csv"
        ca.write_csv(size_manifest, columns, [invalid_size])

        dup_manifest = TMP_ROOT / "manifest-dup.csv"
        ca.write_csv(dup_manifest, columns, [good, dict(good)])

        empty_manifest = TMP_ROOT / "manifest-empty.csv"
        ca.write_csv(empty_manifest, columns, [])

        missing = TMP_ROOT / "no-such-manifest.csv"
        cases = [
            ("空 sha256", manifest, "sha256 为空"),
            ("空 size_bytes", size_manifest, "size_bytes 为空"),
            ("重复条目", dup_manifest, "重复条目"),
            ("缺条目", empty_manifest, "manifest 未收录"),
            ("manifest 缺失", missing, "清单文件缺失"),
        ]

        for label, manifest_path, fragment in cases:
            raised = None
            try:
                # patch 必须包住这次调用本身：lambda 事后执行会读回真实 MANIFEST_PATH
                with _patched_manifest(manifest_path):
                    ca.collect_scope(
                        "main", [run_name], runs_dir, None, config, {}, None, ())
            except ca.CheckError as error:
                raised = str(error)
            except AssertionError:
                raised = "指标被计算了（manifest 未 fail-closed）"
            check(f"T12 {label} → 在算指标前抛 CheckError", raised is not None, raised)
            if raised is not None:
                check(f"T12 {label} 错误信息含 {fragment}", fragment in raised, raised)
                check(f"T12 {label} 明确写了 fail-closed",
                      "fail-closed" in raised or "manifest" in raised, raised)

        # 这条错误必须走 main() 的真实 except 分支并给出退出码 2
        def raising_run(args):
            raise ca.CheckError("manifest 校验未通过，已在计算指标前终止（fail-closed）")

        saved_run = ca.run
        ca.run = raising_run
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ca.main(["--runs-dir", str(runs_dir)])
        finally:
            ca.run = saved_run
        check("T12 CheckError 经 main() 返回退出码 2", code == 2, code)
        check("T12 main() 把失败原因写到 stderr", "FAILED" in err.getvalue(), err.getvalue())
    finally:
        ca.analyze_row = saved_analyze

    after = hashlib.sha256((run_dir / "results.jsonl").read_bytes()).hexdigest()
    check("T12 核对过程只读，results.jsonl 字节未变", before == after, (before, after))


# ---------------------------------------------------------------------------
# T13 记忆公开投影：字段白名单与取值校验
# ---------------------------------------------------------------------------

def _memory_item(**overrides):
    item = {
        "type": "difficulty",
        "subject": "概率论",
        "content": "在条件概率上反复出错",
        "strength": 0.6,
        "evidence_count": 2,
        "episode": "ep-1",
        "evidence_feedback_ids": [1, 2],
        "updated_at": "2026-11-01T00:00:00Z",
    }
    item.update(overrides)
    return item


def test_memory_payload_item_validation():
    def payload(item):
        return (
            bp.MEMORY_HEADER
            + "\n"
            + json.dumps([item], ensure_ascii=False)
            + "\n"
            + ca.MEMORY_SUFFIX_MARKER
            + "\n答案"
        )

    present, items, error = ca.parse_memory_payload(payload(_memory_item()))
    check("T13 合法投影被接受", present is True and error is None and len(items) == 1,
          (present, items, error))

    cases = [
        ("多余字段", _memory_item(owner="me"), "含公开投影之外的字段"),
        ("type 非法", _memory_item(type="unknown"), "的 type 非法"),
        ("subject 为空", _memory_item(subject=""), "的 subject 为空"),
        ("content 为空", _memory_item(content=""), "的 content 为空"),
        ("strength 非数字", _memory_item(strength="0.6"), "的 strength 不是数字"),
        ("strength 越界", _memory_item(strength=2), "的 strength 超出 0–1 范围"),
        ("evidence_count 为负", _memory_item(evidence_count=-1), "的 evidence_count 不是非负整数"),
        ("evidence_feedback_ids 不是数组", _memory_item(evidence_feedback_ids="1,2"),
         "的 evidence_feedback_ids 不是数组"),
        ("证据 id 超过 5 个", _memory_item(evidence_feedback_ids=[1, 2, 3, 4, 5, 6]), "超过 5 个"),
        ("证据 id 非法", _memory_item(evidence_feedback_ids=[1, "x"]), "含非法 id"),
        ("updated_at 类型非法", _memory_item(updated_at=20261101), "的 updated_at 类型非法"),
    ]
    for label, item, fragment in cases:
        present, items, error = ca.parse_memory_payload(payload(item))
        check(f"T13 {label} 被拒绝", present is True and error is not None and items == [],
              (label, items, error))
        check(f"T13 {label} 错误信息含「{fragment}」",
              error is not None and fragment in error, error)

    # 无记忆区块时不冒充「校验通过」
    check("T13 无记忆区块 → present=False 且无 payload",
          ca.parse_memory_payload("纯用户请求") == (False, [], None))


# ---------------------------------------------------------------------------
# T14 check_memory：报告状态必须统一，不能只在文字里改
# ---------------------------------------------------------------------------

def _status_item(sample_id, checks, *, failure_count=0, gold=0, metric_failures=0):
    return {
        "sample_id": sample_id,
        "rows": [{"key": "k1"}],
        "injected": [("difficulty", "概率论")],
        "unexpected": [],
        "gold_failure_count": gold,
        "metric_failure_count": metric_failures,
        "failure_count": failure_count,
        "metric_checks": checks,
    }


def _check_row(section, metric, status, reason="", hard=False):
    return {"section": section, "metric": metric, "status": status,
            "reason": reason, "hard": hard}


def test_overview_status_unified():
    passing = _status_item("S1", [
        _check_row("M1", "类型 P/R/F1", "PASS"),
        _check_row("M4", cm.TOOL_CHECK_METRIC, "PASS"),
    ])
    incomplete = _status_item("S2", [_check_row("M4", cm.TOOL_CHECK_METRIC, cm.STATUS_INCOMPLETE, "未执行")])
    manual = _status_item("S3", [_check_row("M3", "断言词扫描", cm.STATUS_MANUAL, "命中")])
    failed = _status_item("S4", [_check_row("M4", "注入一致性", cm.STATUS_FAIL, "不一致", hard=True)],
                          failure_count=1, metric_failures=1)
    not_applicable = _status_item("S5-empty", [_check_row("M1", "类型 P/R/F1", cm.STATUS_NA)])

    check("T14 PASS 项总览写「通过」", cm._sample_status(passing) == cm.STATUS_PASS, passing)
    check("T14 检查未完成的样本总览不能写通过", cm._sample_status(incomplete) == cm.STATUS_INCOMPLETE,
          incomplete)
    check("T14 待人工复核的样本总览写「待人工复核」", cm._sample_status(manual) == cm.STATUS_MANUAL, manual)
    check("T14 硬失败样本总览写「失败」", cm._sample_status(failed) == cm.STATUS_FAIL, failed)
    check("T14 N/A 样本仍是 PASS（不适用 ≠ 未完成）",
          cm._sample_status(not_applicable) == cm.STATUS_PASS, not_applicable)

    table = cm.overview_table([passing, incomplete, manual, failed, not_applicable])
    text = "\n".join(table)
    check("T14 总览表把 5 种状态分别写出来",
          "通过" in text and "检查未完成" in text and "待人工复核" in text and "失败" in text, text)
    check("T14 总览表不再直接用「硬失败为 0 就通过」",
          "| S2 | 1 | 1 | 0 | 0 | 0 | 检查未完成 |" in text, text)
    check("T14 总览表 header 保留原有列",
          table[0].startswith("| 样本 | 记忆条数 | 注入条数 | 多余记忆 | Gold 失败项 | 检查硬失败 | 结果 |"),
          table[0])

    unknown = None
    try:
        cm._status_label("WHATEVER")
    except ValueError as error:
        unknown = str(error)
    check("T14 未知状态直接报错，不静默当通过", unknown is not None, unknown)

    # M4 明细表的 Tool 列必须取自同一份状态表
    no_tool = _status_item("S6", [_check_row("M4", "注入一致性", cm.STATUS_PASS)])
    label_missing = cm.tool_verdict_label(no_tool)
    check("T14 缺 Tool 核对记录时 M4 明细写「检查未完成」", "检查未完成" in label_missing, label_missing)

    tool_failed = _status_item("S7", [_check_row("M4", cm.TOOL_CHECK_METRIC, "FAIL", "记忆条数不符", True)])
    label_failed = cm.tool_verdict_label(tool_failed)
    check("T14 Tool 核对失败时 M4 明细写失败并带原因",
          label_failed.startswith("失败") and "记忆条数不符" in label_failed, label_failed)

    tool_ok = _status_item("S8", [_check_row("M4", cm.TOOL_CHECK_METRIC, cm.STATUS_PASS)])
    check("T14 Tool 核对通过时 M4 明细只写「通过」",
          cm.tool_verdict_label(tool_ok) == "通过", cm.tool_verdict_label(tool_ok))


def _force_rmtree(target):
    """Windows 上临时目录里的只读文件会让 rmtree 静默失败，先解除只读再重试。"""

    def onexc(func, path, exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=onexc)
    else:
        shutil.rmtree(target, onerror=lambda func, path, exc_info: onexc(func, path, exc_info[1]))


def main():
    try:
        test_m1()
        test_m2()
        test_m3()
        test_m4()
        test_pooled_and_csv()
        test_scan()
        test_read_rows()
        test_auto_analysis()
        test_status_accounting()
        test_grounding()
        test_auto_manifest_failclosed()
        test_memory_payload_item_validation()
        test_overview_status_unified()

        print()
        print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
        for name, detail in FAILED:
            print(f"  FAILED {name}: {detail}")
        return 0 if not FAILED else 1
    finally:
        _force_rmtree(TMP_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
