"""毕业设计实验脚手架 · 第一阶段：Gold Standard 核验与记忆层自动指标。

读回 `seed_fixture.py` 建好的库，把**实际生成的记忆**与 `samples/S*.json` 里的
`gold_standard` 逐条比对，并输出 Markdown 报告。

比对维度（与 docs/GRADUATION-EXPERIMENT-DESIGN.md §4.1、§7.3 一致）：

    status / strength / supports / contradicts / unit / 前缀 / injected / content

另外检查：记忆集合与 Gold Standard 完全一致（多一条也算失败）、注入集合一致、
「不应生成的记忆」确实不存在、待定证据为 0、重建本身成功（ok=True）。

在此之上按 v4.5 §4.1 计算记忆层自动指标 M1–M4（主实验与补充批次分开输出）：

    M1 记忆识别准确性     按 memory_type 的 P/R/F1、4 类 Macro-F1，task_topic 单报
    M2 证据来源可追溯性   evidence_feedback_ids 合法率、Jaccard、contradict_count 一致率
    M3 无证据时避免错误主张 S7-empty 的 |P|=0、S7 的「初步观察：」前缀与 strength≤0.6、
                          D 组答案断言词计数（关键词只作提示，原文一并保留）
    M4 记忆状态正确性     全部非 archived 记忆逐项核对 status/strength/injected，
                          并优先在临时库调用真实 `agent_tools.get_learning_memory()`

不适用一律写 N/A（原因写清楚），绝不用 SKIP 或 0 冒充通过。

命令行：

    python experiments/graduation/check_memory.py                # 重建后核验（含 S7-empty 补充样本）
    python experiments/graduation/check_memory.py --reuse        # 复用已有库
    python experiments/graduation/check_memory.py --report out/x.md --metrics out/m.csv
    python experiments/graduation/check_memory.py --no-tool      # 不调真实 Tool，M4 的 Tool 核对记 N/A
"""

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import seed_fixture  # noqa: E402

STRENGTH_TOLERANCE = 1e-6

VERSION = "1.1.0"

# 与 memory_service.MEMORY_TYPES 保持同序；四类宏平均按这四类算
MEMORY_TYPES = ("difficulty", "task_performance", "time_pattern", "task_topic")
TASK_TOPIC = "task_topic"
S7_SAMPLE = "S7"
EMPTY_SAMPLE = "S7-empty"
TENTATIVE_PREFIX = "初步观察："
S7_MAX_STRENGTH = 0.6

# §4.1 M3：D 组答案里的「断言式记忆引用」关键词。只作提示，命中原文一并保留给人工复核。
ASSERTION_KEYWORDS = (
    "根据你的学习记忆",
    "根据你的记忆",
    "你之前总是",
    "你一直",
    "你总是",
    "你的学习记忆显示",
)
# 扫描哪些 run/sample/group：只扫无证据样本的 D 组答案
SCAN_SAMPLE_IDS = (S7_SAMPLE, EMPTY_SAMPLE)
SCAN_GROUPS = ("D",)


# ---------------------------------------------------------------------------
# 从库里取"真实发生了什么"
# ---------------------------------------------------------------------------


def injected_keys(db_path, limit):
    """复刻注入口径：status='active'，按 strength DESC, id ASC 取前 limit 条。"""

    conn = sqlite3.connect(str(db_path))

    try:
        rows = conn.execute(
            "SELECT memory_type, subject_key FROM learning_memories "
            "WHERE status = 'active' ORDER BY strength DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return {(row[0], row[1]) for row in rows}


def unit_of(memory, memory_service):
    supports = memory["evidence_relations"].get("supports", 0)
    return memory_service._stable_unit(
        memory["memory_type"], memory["value"], supports
    )


# ---------------------------------------------------------------------------
# 比对
# ---------------------------------------------------------------------------


def _check(label, expected, actual, ok):
    return {"field": label, "expected": expected, "actual": actual, "ok": bool(ok)}


def compare_memory(expected, memory, injected, memory_service):
    checks = []
    prefix = memory_service.TENTATIVE_PREFIX

    actual_unit = unit_of(memory, memory_service)
    actual_supports = memory["evidence_relations"].get("supports", 0)
    actual_contradicts = memory["evidence_relations"].get("contradicts", 0)
    has_prefix = memory["content"].startswith(prefix)

    checks.append(_check("status", expected["status"], memory["status"],
                         expected["status"] == memory["status"]))
    checks.append(_check("strength", expected["strength"], memory["strength"],
                         abs(expected["strength"] - memory["strength"]) <= STRENGTH_TOLERANCE))
    checks.append(_check("supports", expected["supports"], actual_supports,
                         expected["supports"] == actual_supports))
    checks.append(_check("contradicts", expected["contradicts"], actual_contradicts,
                         expected["contradicts"] == actual_contradicts))
    checks.append(_check("unit", expected["unit"], actual_unit,
                         expected["unit"] == actual_unit))
    checks.append(_check("has_prefix", expected["has_prefix"], has_prefix,
                         expected["has_prefix"] == has_prefix))
    checks.append(_check("injected", expected["injected"], injected,
                         expected["injected"] == injected))
    checks.append(_check("content", expected["content"], memory["content"],
                         expected["content"] == memory["content"]))

    return checks


def check_sample(sample_id, config, reuse=False, tool_check=True):
    """构建/复用样本库后核验；任何一条不一致（含多余记忆、pending、重建失败）都计入失败。"""

    sample = seed_fixture.load_sample(sample_id, config)

    if reuse:
        db_path = seed_fixture.workspace_dir(config) / f"{sample_id}.db"

        if not db_path.is_file():
            raise FileNotFoundError(f"{sample_id} 还没有库，请先运行 seed_fixture.py")

        # 复用前必须先验完整性：文件存在不等于「已成功重建」，
        # 缺表或引用残缺的库不能绕过检查直接进入 Gold Standard 核验。
        seed_fixture.inspect_sample_db(db_path, sample)

        dump = seed_fixture.dump_memories(db_path)
        rebuild = {"ok": True, "reused": True, "verified": True}
    else:
        seeded = seed_fixture.seed_sample(sample_id, config)
        db_path = Path(seeded["db_path"])
        dump = seeded["memories"]
        rebuild = seeded["rebuild"] or {}

    result = verify_sample(sample, dump, db_path, config, rebuild)

    if tool_check:
        result["tool_check"] = tool_memory_check(db_path, config, result["injected"])
    else:
        result["tool_check"] = {
            "attempted": False,
            "ok": False,
            "error": "本次运行显式跳过真实 Tool 调用（--no-tool）",
        }

    return result


def verify_sample(sample, dump, db_path, config, rebuild):
    """对一份已读出的 dump 做核验（数据库只用于计算注入集合）。

    把读库与核验拆开，测试才能在不改数据库的前提下注入「多一条记忆 / 多一条 pending」
    这类变异副本，验证核验本身真的会失败。
    """

    sample_id = sample["sample_id"]

    _, memory_service = seed_fixture.project_modules(config)

    limit = int(config["memory_limit"])
    injected = injected_keys(db_path, limit)

    memories = dump["memories"]
    by_key = {(memory["memory_type"], memory["subject_key"]): memory for memory in memories}

    rows = []
    failures = 0
    reasons = []

    # 0）重建本身没成功时，后面读到的只是部分结果，整份核验不得算通过
    if rebuild.get("ok") is not True:
        failures += 1
        reasons.append(f"记忆重建未成功：{rebuild!r}")

    # 1）同一 (type, subject_key) 出现多行，说明重建留下了重复记忆
    duplicates = len(memories) - len(by_key)
    if duplicates:
        failures += duplicates
        reasons.append(f"同一 (memory_type, subject_key) 出现重复记忆 {duplicates} 行")

    # 2）逐条比对 Gold Standard
    expected_items = sample["gold_standard"]["memories"]

    for expected in expected_items:
        key = (expected["memory_type"], expected["subject_key"])
        memory = by_key.get(key)

        if memory is None:
            rows.append({
                "key": key,
                "expected": expected,
                "memory": None,
                "checks": [_check("exists", True, False, False)],
            })
            failures += 1
            reasons.append(f"缺少记忆：{key[0]}/{key[1]}")
            continue

        checks = compare_memory(expected, memory, key in injected, memory_service)
        failed = [check for check in checks if not check["ok"]]
        failures += len(failed)

        for check in failed:
            reasons.append(
                f"{key[0]}/{key[1]} 的 {check['field']} 期望 {check['expected']!r} "
                f"实得 {check['actual']!r}"
            )

        rows.append({"key": key, "expected": expected, "memory": memory, "checks": checks})

    # 3）记忆集合必须与 Gold Standard 完全一致：多一条也算失败
    expected_keys = {(item["memory_type"], item["subject_key"]) for item in expected_items}
    unexpected = sorted(key for key in by_key if key not in expected_keys)

    for key in unexpected:
        memory = by_key[key]
        failures += 1
        reasons.append(
            f"多出 Gold Standard 之外的记忆：{key[0]}/{key[1]}"
            f"（content={memory['content']!r}, strength={memory['strength']}）"
        )

    # 4）注入集合必须与 Gold Standard 的预期注入集合一致
    expected_injected = {
        (item["memory_type"], item["subject_key"]) for item in expected_items if item["injected"]
    }
    injected_only_actual = sorted(injected - expected_injected)
    injected_only_expected = sorted(expected_injected - injected)

    for key in injected_only_actual:
        failures += 1
        reasons.append(f"实际注入了 Gold Standard 未预期注入的记忆：{key[0]}/{key[1]}")

    for key in injected_only_expected:
        failures += 1
        reasons.append(f"Gold Standard 预期注入但实际未注入：{key[0]}/{key[1]}")

    # 5）不应生成的记忆
    missing_checked = []

    for item in sample["gold_standard"].get("must_not_exist", []):
        mtype = item["memory_type"]
        mkey = item["subject_key"]

        if mkey == "*":
            found = [key for key in by_key if key[0] == mtype]
        else:
            found = [key for key in by_key if key == (mtype, mkey)]

        ok = not found

        if not ok:
            failures += 1
            reasons.append(f"生成了不应存在的记忆：{mtype}/{mkey} → 实际 {found}")

        missing_checked.append({"item": item, "found": found, "ok": ok})

    # 6）待定证据必须清空
    if dump["pendings"] != 0:
        failures += 1
        reasons.append(f"存在未处理的待定证据 {dump['pendings']} 条")

    # 7）Gold Standard 为空记忆集合的样本：实际记忆集合必须严格为空。
    # 不能只判断"记录数为 0"——键集合、注入集合、pending 缓冲区与事件表都要为空，
    # 任何残留都说明空样本被历史数据污染了。
    empty_check = None

    if not expected_items:
        detail = {
            "memories": len(memories),
            "distinct_keys": len(by_key),
            "injected": len(injected),
            "pendings": dump["pendings"],
            "events": len(dump.get("events") or []),
        }
        empty_ok = (
            detail["memories"] == 0
            and detail["distinct_keys"] == 0
            and detail["injected"] == 0
            and detail["pendings"] == 0
            and detail["events"] == 0
        )
        empty_check = {"ok": empty_ok, "detail": detail}

        if not empty_ok:
            failures += 1
            reasons.append(
                "空 Gold Standard 样本的记忆集合非空："
                + "、".join(f"{name}={value}" for name, value in detail.items())
                + (
                    "（事件表残留表明空样本曾被历史数据污染）"
                    if detail["events"] != 0
                    else ""
                )
            )

    return {
        "sample_id": sample_id,
        "db_path": str(db_path),
        "rebuild": rebuild,
        "rows": rows,
        "unexpected": unexpected,
        "unexpected_detail": [
            {
                "key": key,
                "status": by_key[key]["status"],
                "strength": by_key[key]["strength"],
                "content": by_key[key]["content"],
            }
            for key in unexpected
        ],
        "must_not_exist": missing_checked,
        "injected": sorted(injected),
        "pendings": dump["pendings"],
        "empty_check": empty_check,
        "failure_count": failures,
        "reasons": reasons,
        "m1": memory_type_scores(sample, dump["memories"], memory_service),
        "m2": evidence_audit(
            sample, db_path, dump["memories"], seed_feedback_ids(db_path)
        ),
        "m3": s7_checks(sample, dump["memories"]),
        "m4": state_audit(sample, dump["memories"], injected),
    }


# ---------------------------------------------------------------------------
# M1–M4：记忆层自动指标（v4.5 §4.1）
# ---------------------------------------------------------------------------


def _ratio(numerator, denominator):
    """分母为 0 时返回 None（调用方写 N/A，不拿 0 顶替）。"""

    if not denominator:
        return None
    return numerator / denominator


def _f1(precision, recall):
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _scope_of(sample_id, config):
    return "main" if sample_id in config["samples"] else "supplement"


def active_memories(memories):
    """P_s 口径：全部非 archived 记忆（weakened / resolved 也算在 P_s 里）。"""

    return [memory for memory in memories if memory.get("status", "active") != "archived"]


def seed_feedback_ids(db_path):
    conn = sqlite3.connect(str(db_path))

    try:
        rows = conn.execute("SELECT id FROM feedbacks ORDER BY id ASC").fetchall()
    finally:
        conn.close()

    return {row[0] for row in rows}


def _normalized_subject(label, memory_service):
    """用真实 normalize_subject() 的输出比较 subject_key（契约要求）。"""

    if memory_service is None or not hasattr(memory_service, "normalize_subject"):
        return None

    value = memory_service.normalize_subject(label)
    return value[0] if isinstance(value, tuple) else value


def memory_type_scores(sample, memories, memory_service=None):
    """M1：按 memory_type 的 precision / recall / F1 与 4 类 Macro-F1。

    P_s 用全部非 archived 记忆；task_topic 在宏平均之外单独再报一次。
    subject_key 一律与 `normalize_subject()` 的实际输出比较。
    """

    gold = sample["gold_standard"]["memories"]
    predicted = active_memories(memories)
    predicted_keys = {(item["memory_type"], item["subject_key"]) for item in predicted}
    gold_keys = {(item["memory_type"], item["subject_key"]) for item in gold}

    per_type = {}
    for name in MEMORY_TYPES:
        pred_set = {key for key in predicted_keys if key[0] == name}
        gold_set = {key for key in gold_keys if key[0] == name}
        true_positive = len(pred_set & gold_set)
        precision = _ratio(true_positive, len(pred_set))
        recall = _ratio(true_positive, len(gold_set))
        per_type[name] = {
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
            "predicted": len(pred_set),
            "expected": len(gold_set),
            "true_positive": true_positive,
            "false_positive": len(pred_set - gold_set),
            "false_negative": len(gold_set - pred_set),
        }

    defined = [per_type[name]["f1"] for name in MEMORY_TYPES if per_type[name]["f1"] is not None]
    macro = sum(defined) / len(defined) if len(defined) == len(MEMORY_TYPES) else None
    macro_partial = sum(defined) / len(defined) if defined else None

    normalization_mismatches = []
    normalization_checked = 0
    for item in gold:
        # normalize_subject() 只用于 task_topic：其它类型的 subject_key 是固定枚举
        if item["memory_type"] != TASK_TOPIC:
            continue
        label = item.get("subject_label") or item.get("subject_key") or ""
        normalized = _normalized_subject(label, memory_service)
        if normalized is not None:
            normalization_checked += 1
            if normalized != item["subject_key"]:
                normalization_mismatches.append(
                    {
                        "source": "gold",
                        "subject_label": item["subject_label"],
                        "recorded": item["subject_key"],
                        "normalized": normalized,
                    }
                )
    for memory in predicted:
        if memory["memory_type"] != TASK_TOPIC:
            continue
        label = memory.get("subject_label") or memory.get("subject_key") or ""
        normalized = _normalized_subject(label, memory_service)
        if normalized is not None:
            normalization_checked += 1
            if normalized != memory["subject_key"]:
                normalization_mismatches.append(
                    {
                        "source": "live",
                        "subject_label": memory["subject_label"],
                        "recorded": memory["subject_key"],
                        "normalized": normalized,
                    }
                )

    return {
        "types": per_type,
        "macro_f1": macro,
        "macro_f1_partial": macro_partial,
        "macro_defined": len(defined),
        "macro_n": len(MEMORY_TYPES),
        "task_topic": per_type[TASK_TOPIC],
        "normalization_mismatches": normalization_mismatches,
        "normalization_checked": memory_service is not None,
    }


def evidence_audit(sample, db_path, memories, seed_ids):
    """M2：证据来源可追溯性。

    ① 所有 evidence_feedback_ids（当前 episode 的 supports 证据）是否都属于该样本的种子反馈；
    ② 与 Gold 证据集合的 Jaccard —— 样本没有记录 E_gold，按 N/A 报并写明原因；
    ③ contradict_count 与 Gold 期望完全一致的比例。
    """

    conn = sqlite3.connect(str(db_path))

    try:
        rows = conn.execute(
            "SELECT m.memory_type, m.subject_key, e.episode, e.feedback_id, e.relation "
            "FROM memory_evidence e JOIN learning_memories m ON m.id = e.memory_id"
        ).fetchall()
    finally:
        conn.close()

    live_by_key = {(item["memory_type"], item["subject_key"]): item for item in memories}
    support_ids = set()
    illegal = []
    contradict_counts = {}
    for memory_type, subject_key, episode, feedback_id, relation in rows:
        key = (memory_type, subject_key)
        if relation == "contradicts":
            contradict_counts[key] = contradict_counts.get(key, 0) + 1
        if relation != "supports":
            continue
        live = live_by_key.get(key)
        # 只统计当前 episode 的 supports（与 retrieve() 同口径）
        if live is not None and live.get("episode") != episode:
            continue
        support_ids.add(feedback_id)
        if feedback_id not in seed_ids:
            illegal.append(
                {"memory_type": memory_type, "subject_key": subject_key, "feedback_id": feedback_id}
            )

    gold = {
        (item["memory_type"], item["subject_key"]): item
        for item in sample["gold_standard"]["memories"]
    }
    contradict_mismatches = []
    present_gold = 0
    for key, expected in sorted(gold.items()):
        live = live_by_key.get(key)
        if live is None:
            continue
        present_gold += 1
        expected_count = expected.get("contradicts", 0)
        actual_count = live.get("evidence_relations", {}).get(
            "contradicts", contradict_counts.get(key, 0)
        )
        if expected_count != actual_count:
            contradict_mismatches.append(
                {
                    "memory_type": key[0],
                    "subject_key": key[1],
                    "expected": expected_count,
                    "actual": actual_count,
                }
            )

    illegal_ids = {item["feedback_id"] for item in illegal}
    legal_ids = len(support_ids - illegal_ids)
    return {
        "evidence_ids_total": len(support_ids),
        "legal_ids": legal_ids,
        "legit_rate": _ratio(legal_ids, len(support_ids)),
        "illegal": illegal,
        "seed_feedback_count": len(seed_ids),
        "jaccard": None,
        "jaccard_reason": "样本 Gold Standard 未记录 E_gold（无 evidence 字段），无法计算 E_pred 与 E_gold 的 Jaccard",
        "contradict_ratio": _ratio(present_gold - len(contradict_mismatches), present_gold),
        "contradict_matches": present_gold - len(contradict_mismatches),
        "contradict_total": present_gold,
        "contradict_mismatches": contradict_mismatches,
        "missing_memories": len(gold) - present_gold,
    }




def pooled_scores(results, config):
    """M1 的批次汇总：把同一 scope 各样本的 TP/FP/FN 相加后重算 P/R/F1。

    汇总单位是 (sample_id, memory_type, subject_key)，不同样本里的同名记忆不互相覆盖。
    """

    pooled = {}

    for item in results:
        scope = _scope_of(item["sample_id"], config)
        bucket = pooled.setdefault(
            scope,
            {
                f"{name}": {"tp": 0, "predicted": 0, "expected": 0} for name in MEMORY_TYPES
            },
        )
        for name in MEMORY_TYPES:
            entry = item["m1"]["types"][name]
            bucket[name]["tp"] += entry["true_positive"]
            bucket[name]["predicted"] += entry["predicted"]
            bucket[name]["expected"] += entry["expected"]

    result = {}
    for scope, bucket in pooled.items():
        types = {}
        for name in MEMORY_TYPES:
            counts = bucket[name]
            precision = _ratio(counts["tp"], counts["predicted"])
            recall = _ratio(counts["tp"], counts["expected"])
            types[name] = {
                "precision": precision,
                "recall": recall,
                "f1": _f1(precision, recall),
                "predicted": counts["predicted"],
                "expected": counts["expected"],
                "true_positive": counts["tp"],
                "false_positive": counts["predicted"] - counts["tp"],
                "false_negative": counts["expected"] - counts["tp"],
            }
        defined = [types[name]["f1"] for name in MEMORY_TYPES if types[name]["f1"] is not None]
        result[scope] = {
            "types": types,
            "macro_f1": sum(defined) / len(defined) if len(defined) == len(MEMORY_TYPES) else None,
            "macro_f1_partial": sum(defined) / len(defined) if defined else None,
            "macro_defined": len(defined),
            "task_topic": types[TASK_TOPIC],
            "sample_count": sum(
                1 for item in results if _scope_of(item["sample_id"], config) == scope
            ),
        }

    return result


def s7_checks(sample, memories):
    """M3：无证据样本的「不编造」检查。

    - S7-empty：|P|=0 记 1，否则 0；
    - S7：每条记忆的 content 必须以「初步观察：」开头且 strength ≤ 0.6。
    """

    sample_id = sample["sample_id"]
    live = active_memories(memories)

    if sample_id == EMPTY_SAMPLE:
        ok = len(memories) == 0
        return {
            "kind": "empty",
            "applicable": True,
            "ok": ok,
            "n": 0,
            "rows": [],
            "score": 1 if ok else 0,
            "detail": {
                "all_rows": len(memories),
                "active": len(live),
                "distinct_keys": len(
                    {(item["memory_type"], item["subject_key"]) for item in memories}
                ),
            },
        }

    if sample_id == S7_SAMPLE:
        rows = []
        for memory in live:
            prefix_ok = str(memory.get("content", "")).startswith(TENTATIVE_PREFIX)
            strength_ok = (
                float(memory.get("strength", 0.0)) <= S7_MAX_STRENGTH + STRENGTH_TOLERANCE
            )
            rows.append(
                {
                    "memory_type": memory["memory_type"],
                    "subject_key": memory["subject_key"],
                    "strength": memory["strength"],
                    "prefix_ok": prefix_ok,
                    "strength_ok": strength_ok,
                    "ok": prefix_ok and strength_ok,
                    "content": memory["content"],
                }
            )
        all_ok = bool(rows) and all(row["ok"] for row in rows)
        return {
            "kind": "s7",
            "applicable": True,
            "ok": all_ok,
            "n": len(rows),
            "rows": rows,
            "score": 1 if all_ok else 0,
            "detail": {"reason": None if rows else "S7 没有任何非 archived 记忆可检"},
        }

    return {
        "kind": "none",
        "applicable": False,
        "ok": None,
        "n": 0,
        "rows": [],
        "score": None,
        "detail": {"reason": "该样本不在 M3 检查范围"},
    }


def state_audit(sample, memories, injected):
    """M4：全部非 archived 记忆逐项核对 status / strength / injected。

    不一致只做**诊断分类**（缺失 / 被归档 / 多余 / 字段不符）；「实现问题 vs 夹具预期错误」
    需要人工定因，报告里保留这一列但不自动下结论。
    """

    gold = {
        (item["memory_type"], item["subject_key"]): item
        for item in sample["gold_standard"]["memories"]
    }
    live = {
        (item["memory_type"], item["subject_key"]): item
        for item in active_memories(memories)
    }
    archived = {
        (item["memory_type"], item["subject_key"])
        for item in memories
        if item.get("status") == "archived"
    }
    injected = set(injected)

    rows = []
    for key in sorted(set(gold) | set(live)):
        expected = gold.get(key)
        actual = live.get(key)
        checks = []
        cause = None

        if expected is not None and actual is not None:
            strength_ok = (
                abs(float(actual.get("strength", 0.0)) - float(expected["strength"]))
                <= STRENGTH_TOLERANCE
            )
            checks = [
                _check("status", expected["status"], actual.get("status"),
                       expected["status"] == actual.get("status")),
                _check("strength", expected["strength"], actual.get("strength"), strength_ok),
                _check(
                    "injected",
                    bool(expected.get("injected", False)),
                    key in injected,
                    bool(expected.get("injected", False)) == (key in injected),
                ),
            ]
        elif expected is not None:
            cause = "记忆被归档" if key in archived else "记忆缺失"
            actual_status = "archived" if key in archived else None
            checks = [
                {"field": "status", "expected": expected["status"], "actual": actual_status, "ok": False},
                {"field": "strength", "expected": expected["strength"], "actual": None, "ok": False},
                {
                    "field": "injected",
                    "expected": bool(expected.get("injected", False)),
                    "actual": False,
                    "ok": not bool(expected.get("injected", False)),
                },
            ]
        else:
            cause = "多余记忆（Gold 未期望）"
            checks = [
                {"field": "status", "expected": None, "actual": actual["status"], "ok": False},
                {"field": "strength", "expected": None, "actual": actual["strength"], "ok": False},
                {"field": "injected", "expected": None, "actual": key in injected, "ok": False},
            ]

        rows.append(
            {
                "memory_type": key[0],
                "subject_key": key[1],
                "expected_status": expected["status"] if expected else None,
                "actual_status": actual.get("status") if actual else ("archived" if key in archived else None),
                "expected_strength": expected["strength"] if expected else None,
                "actual_strength": actual.get("strength") if actual else None,
                "expected_injected": bool(expected.get("injected", False)) if expected else None,
                "actual_injected": key in injected,
                "checks": checks,
                "ok": all(check["ok"] for check in checks),
                "cause": cause,
                "owner": None if cause is None else "待人工判定（实现问题 / 夹具预期错误）",
            }
        )

    by_field = {}
    for field in ("status", "strength", "injected"):
        matched = sum(
            1 for row in rows for check in row["checks"] if check["field"] == field and check["ok"]
        )
        by_field[field] = {"matched": matched, "total": len(rows)}

    consistent = sum(1 for row in rows if row["ok"])
    return {
        "rows": rows,
        "consistent": consistent,
        "total": len(rows),
        "ratio": _ratio(consistent, len(rows)),
        "by_field": by_field,
        "inconsistent": [row for row in rows if not row["ok"]],
        "diagnostic_counts": _count_causes(rows),
    }


def _count_causes(rows):
    counts = {}
    for row in rows:
        if row["ok"]:
            continue
        label = row["cause"] or "、".join(
            check["field"] for check in row["checks"] if not check["ok"]
        )
        counts[label] = counts.get(label, 0) + 1
    return counts


def tool_memory_check(db_path, config, expected_injected):
    """M4 的 Tool 侧核对：在已隔离的临时库上真实调用 get_learning_memory()。

    `seed_fixture.project_modules()` 必须先建好隔离（database.DATABASE_PATH 在 workspace 内），
    再 import agent_tools；否则会先绑到真实 feedback.db。
    """

    try:
        seed_fixture.project_modules(config)
    except Exception as error:  # pragma: no cover - 防御：隔离失败就不再往下调
        return {
            "attempted": False,
            "ok": False,
            "error": f"项目模块隔离失败，未调用真实 Tool：{error}",
        }

    project_root = str(seed_fixture.PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        import database  # noqa: E402
        import agent_tools  # noqa: E402

        configured_limit = int(config["memory_limit"])
        limit = min(configured_limit, int(agent_tools.MAX_MEMORY_LIMIT))
        database.DATABASE_PATH = Path(db_path)
        result = agent_tools.get_learning_memory(limit=limit)
    except Exception as error:
        return {
            "attempted": True,
            "ok": False,
            "error": f"真实 Tool 调用异常：{type(error).__name__}: {error}",
        }

    if not result.get("ok"):
        return {
            "attempted": True,
            "ok": False,
            "error": result.get("error") or repr(result),
            "limit": limit,
        }

    returned_keys = {
        (item.get("type"), item.get("subject")) for item in result["memories"]
    }
    returned_labels = [
        f"{item.get('type')}/{item.get('subject')}" for item in result["memories"]
    ]
    # Tool 只暴露 subject_label（不暴露 subject_key），所以与库里的 label 集合比较；
    # limit 被夹到 Tool 上限时，按同一上限重算期望集合。
    conn = sqlite3.connect(str(db_path))

    try:
        expected = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT memory_type, subject_label FROM learning_memories "
                "WHERE status = 'active' ORDER BY strength DESC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        }
    finally:
        conn.close()

    evidence_ids = [
        feedback_id
        for item in result["memories"]
        for feedback_id in item.get("evidence_feedback_ids", [])
    ]
    seed_ids = seed_feedback_ids(db_path)
    legitimate = sum(1 for feedback_id in evidence_ids if feedback_id in seed_ids)
    return {
        "attempted": True,
        "ok": True,
        "limit": limit,
        "limit_clamped": limit != configured_limit,
        "returned_count": len(returned_keys),
        "expected_count": len(expected),
        "matches_injected": returned_keys == expected,
        "returned_keys": sorted(returned_keys),
        "returned_labels": returned_labels,
        "evidence_ids_total": len(evidence_ids),
        "evidence_legit_rate": _ratio(legitimate, len(evidence_ids)),
        "error": None,
    }


def _sha256_file(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def scan_assertion_keywords(runs_dir, sample_ids=SCAN_SAMPLE_IDS, groups=SCAN_GROUPS):
    """M3 自动提示：扫 runs/ 里无证据样本 D 组答案的断言式关键词。

    只读 results.jsonl，不写、不改；命中原文一并保留，自动计数只作提示，最终以人工复核为准。
    """

    runs_dir = Path(runs_dir)

    if not runs_dir.is_dir():
        return {
            "ok": False,
            "reason": f"runs 目录不存在：{runs_dir}",
            "rows_checked": 0,
            "hits": [],
            "keyword_totals": {},
            "runs_scanned": [],
        }

    hits = []
    keyword_totals = {}
    rows_checked = 0
    runs_scanned = []
    sources = []

    for run_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
        results_path = run_dir / "results.jsonl"

        if not results_path.is_file():
            continue

        before = _sha256_file(results_path)
        runs_scanned.append(run_dir.name)
        sources.append(str(results_path))

        with results_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()

                if not line:
                    continue

                row = json.loads(line)

                if row.get("sample_id") not in sample_ids or row.get("group") not in groups:
                    continue

                rows_checked += 1
                answer = (row.get("final_output") or {}).get("answer") or ""

                for keyword in ASSERTION_KEYWORDS:
                    count = answer.count(keyword)

                    if not count:
                        continue

                    index = answer.find(keyword)
                    snippet = " ".join(answer[max(0, index - 40) : index + 60].split())
                    hits.append(
                        {
                            "run": run_dir.name,
                            "line_number": line_number,
                            "sample_id": row.get("sample_id"),
                            "group": row.get("group"),
                            "repeat_index": row.get("repeat_index"),
                            "keyword": keyword,
                            "count": count,
                            "snippet": snippet,
                        }
                    )
                    keyword_totals[keyword] = keyword_totals.get(keyword, 0) + count

        after = _sha256_file(results_path)

        if before != after:
            return {
                "ok": False,
                "reason": f"扫描期间 {results_path} 的 SHA-256 发生变化",
                "rows_checked": rows_checked,
                "hits": hits,
                "keyword_totals": keyword_totals,
                "runs_scanned": runs_scanned,
                "source_sha256": before,
                "source_files": sources,
            }

    return {
        "ok": True,
        "reason": None,
        "rows_checked": rows_checked,
        "hits": hits,
        "keyword_totals": keyword_totals,
        "runs_scanned": runs_scanned,
        "source_files": sources,
    }


def metric_rows(results, config, scan=None):
    """把每样本 M1–M4 摊平成 CSV 长表（主实验 / 补充批次分开）。"""

    rows = []

    def add(scope, sample_id, metric, value, n=None, denominator=None, unit="", note=""):
        rows.append(
            {
                "scope": scope,
                "sample_id": sample_id,
                "metric": metric,
                "value": "" if value is None else value,
                "n": "" if n is None else n,
                "denominator": "" if denominator is None else denominator,
                "unit": unit,
                "note": note,
            }
        )

    for item in results:
        sample_id = item["sample_id"]
        scope = _scope_of(sample_id, config)
        m1 = item["m1"]

        for name in MEMORY_TYPES:
            entry = m1["types"][name]
            add(
                scope, sample_id, f"M1.precision.{name}", entry["precision"],
                entry["predicted"], None, "ratio",
                f"tp={entry['true_positive']} fp={entry['false_positive']}",
            )
            add(
                scope, sample_id, f"M1.recall.{name}", entry["recall"],
                entry["true_positive"], entry["expected"], "ratio",
                f"fn={entry['false_negative']}",
            )
            add(
                scope, sample_id, f"M1.f1.{name}", entry["f1"],
                None, None, "ratio",
                f"P={entry['precision']} R={entry['recall']}",
            )

        add(
            scope, sample_id, "M1.macro_f1", m1["macro_f1"],
            m1["macro_defined"], m1["macro_n"], "ratio",
            "4 类齐全才算 Macro-F1；缺失类型见 macro_f1_partial",
        )
        add(
            scope, sample_id, "M1.macro_f1_partial", m1["macro_f1_partial"],
            m1["macro_defined"], m1["macro_n"], "ratio",
            "仅对已定义类型求平均，不等同 4 类 Macro-F1",
        )
        add(
            scope, sample_id, "M1.task_topic_f1", m1["task_topic"]["f1"],
            None, None, "ratio", "task_topic 单报",
        )
        add(
            scope, sample_id, "M1.subject_key_normalization",
            "PASS" if not m1["normalization_mismatches"] else "FAIL",
            len(m1["normalization_mismatches"]), None, "verdict",
            "；".join(
                f"{entry['source']}:{entry['subject_label']}→{entry['normalized']}（记录 {entry['recorded']}）"
                for entry in m1["normalization_mismatches"]
            ) or "全部与 normalize_subject() 输出一致",
        )

        m2 = item["m2"]
        add(
            scope, sample_id, "M2.evidence_legit_rate", m2["legit_rate"],
            m2["legal_ids"], m2["evidence_ids_total"], "ratio",
            f"种子反馈 {m2['seed_feedback_count']} 条；非法 {len(m2['illegal'])} 条",
        )
        add(
            scope, sample_id, "M2.evidence_jaccard", None, None, None, "ratio",
            m2["jaccard_reason"],
        )
        add(
            scope, sample_id, "M2.contradict_agreement", m2["contradict_ratio"],
            m2["contradict_matches"], m2["contradict_total"], "ratio",
            f"不一致 {len(m2['contradict_mismatches'])} 条；缺失记忆 {m2['missing_memories']} 条不计入分母",
        )

        m3 = item["m3"]
        if m3["kind"] == "empty":
            add(
                scope, sample_id, "M3.s7empty_zero_memories", m3["score"],
                m3["detail"]["active"], 0, "verdict",
                f"全部行 {m3['detail']['all_rows']}；|P|=0 记 1",
            )
        elif m3["kind"] == "s7":
            add(
                scope, sample_id, "M3.s7_prefix_ok",
                int(all(row["prefix_ok"] for row in m3["rows"])) if m3["rows"] else None,
                sum(1 for row in m3["rows"] if row["prefix_ok"]), m3["n"], "verdict",
                "content 必须以「初步观察：」开头",
            )
            add(
                scope, sample_id, "M3.s7_strength_ok",
                int(all(row["strength_ok"] for row in m3["rows"])) if m3["rows"] else None,
                sum(1 for row in m3["rows"] if row["strength_ok"]), m3["n"], "verdict",
                "strength 必须 ≤ 0.6",
            )

        m4 = item["m4"]
        add(
            scope, sample_id, "M4.consistency_rate", m4["ratio"],
            m4["consistent"], m4["total"], "ratio",
            "；".join(f"{name}×{count}" for name, count in sorted(m4["diagnostic_counts"].items())) or "全部一致",
        )
        for field, entry in m4["by_field"].items():
            add(
                scope, sample_id, f"M4.{field}_agreement", _ratio(entry["matched"], entry["total"]),
                entry["matched"], entry["total"], "ratio", "",
            )

        tool = item.get("tool_check")
        if tool is None:
            add(scope, sample_id, "M4.tool_injected_match", "N/A", None, None, "verdict", "本次运行未做 Tool 侧核对")
        elif not tool.get("attempted"):
            add(scope, sample_id, "M4.tool_injected_match", "N/A", None, None, "verdict", tool.get("error") or "Tool 未调用")
        elif not tool.get("ok"):
            add(scope, sample_id, "M4.tool_injected_match", "N/A", None, None, "verdict", tool.get("error") or "Tool 调用失败")
        else:
            add(
                scope, sample_id, "M4.tool_injected_match",
                "PASS" if tool["matches_injected"] else "FAIL",
                tool["returned_count"], tool["expected_count"], "verdict",
                "get_learning_memory(limit=%d) 返回集合 vs 注入集合%s"
                % (tool["limit"], "（limit 已夹到 Tool 上限）" if tool.get("limit_clamped") else ""),
            )
            add(
                scope, sample_id, "M4.tool_evidence_legit_rate", tool["evidence_legit_rate"],
                None, tool["evidence_ids_total"], "ratio",
                "真实 Tool 返回的 evidence_feedback_ids 合法率",
            )

    pooled = pooled_scores(results, config)
    for scope, entry in sorted(pooled.items()):
        for name in MEMORY_TYPES:
            type_entry = entry["types"][name]
            add(
                scope, "ALL", f"M1.precision.{name}", type_entry["precision"],
                type_entry["predicted"], None, "ratio",
                f"tp={type_entry['true_positive']} fp={type_entry['false_positive']}；批次汇总 {entry['sample_count']} 样本",
            )
            add(
                scope, "ALL", f"M1.recall.{name}", type_entry["recall"],
                type_entry["true_positive"], type_entry["expected"], "ratio",
                f"fn={type_entry['false_negative']}；批次汇总 {entry['sample_count']} 样本",
            )
            add(
                scope, "ALL", f"M1.f1.{name}", type_entry["f1"],
                None, None, "ratio",
                f"P={type_entry['precision']} R={type_entry['recall']}；批次汇总 {entry['sample_count']} 样本",
            )
        add(
            scope, "ALL", "M1.macro_f1", entry["macro_f1"],
            entry["macro_defined"], 4, "ratio",
            f"批次汇总 {entry['sample_count']} 样本；4 类齐全才算 Macro-F1",
        )
        add(
            scope, "ALL", "M1.macro_f1_partial", entry["macro_f1_partial"],
            entry["macro_defined"], 4, "ratio",
            "仅对已定义类型求平均，不等同 4 类 Macro-F1",
        )
        add(
            scope, "ALL", "M1.task_topic_f1", entry["task_topic"]["f1"],
            None, None, "ratio", "task_topic 单报（批次汇总）",
        )

    if scan is not None and scan.get("ok"):
        for sample_id in sorted({hit["sample_id"] for hit in scan["hits"]}):
            scope = _scope_of(sample_id, config)
            for keyword in ASSERTION_KEYWORDS:
                count = sum(
                    hit["count"]
                    for hit in scan["hits"]
                    if hit["sample_id"] == sample_id and hit["keyword"] == keyword
                )
                add(
                    scope, sample_id, f"M3.assertion_keyword.{keyword}", count,
                    count, None, "count",
                    "关键词只作提示，最终以人工复核原文为准",
                )

    return rows


def write_metrics_csv(rows, path):
    fieldnames = ["scope", "sample_id", "metric", "value", "n", "denominator", "unit", "note"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def render_report(results, config, scan=None):
    lines = []
    lines.append("# 记忆 Gold Standard 核验报告")
    lines.append("")
    lines.append(f"- 参照时间 REF_DATE：`{config['ref_date']}`")
    lines.append("- 数据来源：`experiments/graduation/samples/S*.json`（模拟数据，非真实用户）")
    lines.append("- 比对实现：`experiments/graduation/check_memory.py`，直接读回 SQLite 行，不重新渲染文案")
    lines.append(
        f"- 版本：check_memory v{VERSION}；M1–M4 口径对应 v4.5 §4.1，主实验与补充批次分开标注"
    )
    lines.append("- 不适用一律写 N/A（附原因），不用 SKIP 或 0 冒充通过")
    lines.append("")

    total_memories = sum(len(item["rows"]) for item in results)
    total_failures = sum(item["failure_count"] for item in results)

    lines.append("## 总览")
    lines.append("")
    lines.append("| 样本 | 记忆条数 | 注入条数 | 多余记忆 | 核验失败项 | 结果 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in results:
        verdict = "通过" if item["failure_count"] == 0 else "失败"
        lines.append(
            f"| {item['sample_id']} | {len(item['rows'])} | {len(item['injected'])} | "
            f"{len(item['unexpected'])} | {item['failure_count']} | {verdict} |"
        )
    lines.append("")
    lines.append(
        f"合计 {total_memories} 条记忆，失败项 {total_failures} 项，"
        f"多余记忆 {sum(len(item['unexpected']) for item in results)} 条；"
        f"结论：{'全部通过' if total_failures == 0 else '存在不一致'}"
    )
    lines.append("")

    for item in results:
        lines.append(f"## {item['sample_id']}")
        lines.append("")
        lines.append(f"- 数据库：`{item['db_path']}`")
        lines.append(f"- 待定证据（pendings）：{item['pendings']}")
        lines.append(f"- 核验失败项：{item['failure_count']}")

        if item.get("empty_check") is not None:
            check = item["empty_check"]
            detail = check["detail"]
            lines.append(
                "- 空记忆集合严格性："
                + ("通过" if check["ok"] else "失败")
                + "（"
                + "、".join(f"{name}={value}" for name, value in detail.items())
                + "）"
            )

        lines.append("")

        if item["failure_count"] == 0:
            lines.append("**失败原因**：无，记忆集合与 Gold Standard 完全一致。")
        else:
            lines.append("**失败原因**")
            lines.append("")
            for reason in item["reasons"]:
                lines.append(f"- {reason}")
        lines.append("")

        lines.append("| 类型 | subject_key | 状态 | strength | sup | con | unit | 前缀 | 注入 | 结论 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

        for row in item["rows"]:
            expected = row["expected"]
            memory = row["memory"]

            if memory is None:
                lines.append(
                    f"| {expected['memory_type']} | {expected['subject_key']} | 缺失 | "
                    "— | — | — | — | — | — | 失败：未生成 |"
                )
                continue

            failed = [check for check in row["checks"] if not check["ok"]]
            verdict = "通过" if not failed else "失败：" + "，".join(
                f"{check['field']} 期望 {check['expected']!r} 实得 {check['actual']!r}"
                for check in failed
            )

            lines.append(
                f"| {memory['memory_type']} | {memory['subject_key']} | {memory['status']} | "
                f"{memory['strength']} | {memory['evidence_relations'].get('supports', 0)} | "
                f"{memory['evidence_relations'].get('contradicts', 0)} | "
                f"{unit_of(memory, seed_fixture.project_modules(config)[1])} | "
                f"{'有' if memory['content'].startswith(seed_fixture.project_modules(config)[1].TENTATIVE_PREFIX) else '无'} | "
                f"{'是' if (memory['memory_type'], memory['subject_key']) in set(item['injected']) else '否'} | "
                f"{verdict} |"
            )

        lines.append("")

        if item["unexpected"]:
            lines.append("**多余记忆（Gold Standard 之外，每条计 1 项失败）**")
            lines.append("")
            lines.append("| 类型 | subject_key | 状态 | strength | content |")
            lines.append("| --- | --- | --- | --- | --- |")
            for entry in item["unexpected_detail"]:
                mtype, mkey = entry["key"]
                lines.append(
                    f"| {mtype} | {mkey} | {entry['status']} | {entry['strength']} | "
                    f"{entry['content']} |"
                )
            lines.append("")
        else:
            lines.append("**多余记忆**：无。")
            lines.append("")

        lines.append("**不应生成的记忆**")
        lines.append("")
        lines.append("| 类型 | subject_key | 理由 | 结果 |")
        lines.append("| --- | --- | --- | --- |")
        for entry in item["must_not_exist"]:
            item_def = entry["item"]
            verdict = "确实未生成" if entry["ok"] else f"错误生成：{entry['found']}"
            lines.append(
                f"| {item_def['memory_type']} | {item_def['subject_key']} | "
                f"{item_def['why']} | {verdict} |"
            )
        lines.append("")

    lines.append("## M1 记忆识别准确性（P/R/F1，按 memory_type）")
    lines.append("")
    lines.append("| 范围 | 样本 | 类型 | P | R | F1 | P 条数 | G 条数 | TP |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    def fmt(value):
        return "N/A" if value is None else f"{value:.4f}"

    for item in results:
        scope = "主实验" if _scope_of(item["sample_id"], config) == "main" else "补充"
        for name in MEMORY_TYPES:
            entry = item["m1"]["types"][name]
            lines.append(
                f"| {scope} | {item['sample_id']} | {name} | {fmt(entry['precision'])} | "
                f"{fmt(entry['recall'])} | {fmt(entry['f1'])} | {entry['predicted']} | "
                f"{entry['expected']} | {entry['true_positive']} |"
            )
        macro = item["m1"]
        lines.append(
            f"| {scope} | {item['sample_id']} | **4 类 Macro-F1** | — | — | "
            f"{fmt(macro['macro_f1'])} | — | — | — |"
        )
        lines.append(
            f"| {scope} | {item['sample_id']} | Macro-F1（已定义类型，{macro['macro_defined']}/4） | — | — | "
            f"{fmt(macro['macro_f1_partial'])} | — | — | — |"
        )
        lines.append(
            f"| {scope} | {item['sample_id']} | task_topic（单报） | — | — | "
            f"{fmt(macro['task_topic']['f1'])} | — | — | — |"
        )

    pooled = pooled_scores(results, config)
    for scope, entry in sorted(pooled.items()):
        scope_label = "主实验汇总" if scope == "main" else "补充汇总"
        for name in MEMORY_TYPES:
            type_entry = entry["types"][name]
            lines.append(
                f"| {scope_label}（{entry['sample_count']} 样本） | ALL | {name} | "
                f"{fmt(type_entry['precision'])} | {fmt(type_entry['recall'])} | "
                f"{fmt(type_entry['f1'])} | {type_entry['predicted']} | {type_entry['expected']} | "
                f"{type_entry['true_positive']} |"
            )
        lines.append(
            f"| {scope_label}（{entry['sample_count']} 样本） | ALL | **4 类 Macro-F1** | — | — | "
            f"{fmt(entry['macro_f1'])} | — | — | — |"
        )
        lines.append(
            f"| {scope_label}（{entry['sample_count']} 样本） | ALL | Macro-F1（已定义类型，"
            f"{entry['macro_defined']}/4） | — | — | {fmt(entry['macro_f1_partial'])} | — | — | — |"
        )

    lines.append("")

    normalization_failures = [
        (item["sample_id"], entry)
        for item in results
        for entry in item["m1"]["normalization_mismatches"]
    ]
    lines.append(
        "- subject_key 归一化核对："
        + (
            "全部通过（与 `normalize_subject()` 实际输出一致）"
            if not normalization_failures
            else "存在不一致："
            + "；".join(
                f"{sample_id} {entry['source']} {entry['subject_label']}→{entry['normalized']}"
                f"（记录 {entry['recorded']}）"
                for sample_id, entry in normalization_failures
            )
        )
    )
    lines.append("")

    lines.append("## M2 证据来源可追溯性")
    lines.append("")
    lines.append("| 范围 | 样本 | 证据合法率 | 合法/总数 | contradict 一致率 | 一致/总数 | 证据 Jaccard |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in results:
        scope = "主实验" if _scope_of(item["sample_id"], config) == "main" else "补充"
        m2 = item["m2"]
        lines.append(
            f"| {scope} | {item['sample_id']} | {fmt(m2['legit_rate'])} | "
            f"{m2['legal_ids']}/{m2['evidence_ids_total']} | {fmt(m2['contradict_ratio'])} | "
            f"{m2['contradict_matches']}/{m2['contradict_total']} | N/A |"
        )
    lines.append("")
    lines.append(
        "- Jaccard 全表 N/A：样本 Gold Standard 没有 `evidence` 字段（E_gold 未记录），"
        "无法计算 |E_pred∩E_gold|/|E_pred∪E_gold|；这不是 0 分，也不能用其它集合代替。"
    )
    illegal = [
        (item["sample_id"], entry) for item in results for entry in item["m2"]["illegal"]
    ]
    lines.append(
        "- 证据合法率："
        + (
            "全部 evidence_feedback_ids 都属于该样本种子反馈（100%）"
            if not illegal
            else "存在非法反馈 ID："
            + "；".join(
                f"{sample_id} {entry['memory_type']}/{entry['subject_key']}→feedback {entry['feedback_id']}"
                for sample_id, entry in illegal
            )
        )
    )
    lines.append("")

    lines.append("## M3 无证据时避免错误主张")
    lines.append("")
    lines.append("| 范围 | 样本 | 检查 | 结果 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for item in results:
        m3 = item["m3"]
        scope = "主实验" if _scope_of(item["sample_id"], config) == "main" else "补充"

        if m3["kind"] == "empty":
            lines.append(
                f"| {scope} | {item['sample_id']} | |P|=0 | {'通过（1）' if m3['ok'] else '失败（0）'} | "
                f"全部记忆行 {m3['detail']['all_rows']} 条，active {m3['detail']['active']} 条 |"
            )
        elif m3["kind"] == "s7":
            for row in m3["rows"]:
                lines.append(
                    f"| {scope} | {item['sample_id']} | {row['memory_type']}/{row['subject_key']} | "
                    f"{'通过' if row['ok'] else '失败'} | 前缀 {'OK' if row['prefix_ok'] else '缺失'}；"
                    f"strength {row['strength']}（{'≤0.6' if row['strength_ok'] else '>0.6'}） |"
                )
            if not m3["rows"]:
                lines.append(
                    f"| {scope} | {item['sample_id']} | 前缀/strength | N/A | 没有非 archived 记忆可检 |"
                )
    lines.append("")

    if scan is None:
        lines.append(
            "- D 组断言词扫描：本次未执行（--no-assertion-scan），结果 N/A，不冒充通过。"
        )
    elif not scan.get("ok"):
        lines.append(f"- D 组断言词扫描：N/A（{scan.get('reason')}）")
    else:
        lines.append(
            f"- D 组断言词扫描：只读扫描 {len(scan['runs_scanned'])} 个 run、"
            f"{scan['rows_checked']} 条 S7/S7-empty 的 D 组答案；自动命中仅作提示，需人工复核原文。"
        )
        lines.append("")
        lines.append("| 样本 | 关键词 | 命中次数 | 首次命中原文片段 |")
        lines.append("| --- | --- | --- | --- |")
        for hit in scan["hits"]:
            lines.append(
                f"| {hit['sample_id']} | {hit['keyword']} | {hit['count']} | {hit['snippet']} |"
            )
        if not scan["hits"]:
            lines.append("| — | — | 0 | 未命中任何关键词（仍需人工复核，不能只靠关键词） |")
    lines.append("")

    lines.append("## M4 记忆状态正确性")
    lines.append("")
    lines.append("| 范围 | 样本 | 一致/总条数 | 一致率 | 不一致分类 | Tool 注入集合 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in results:
        scope = "主实验" if _scope_of(item["sample_id"], config) == "main" else "补充"
        m4 = item["m4"]
        tool = item.get("tool_check") or {}
        if tool.get("attempted") and tool.get("ok"):
            tool_verdict = "PASS" if tool["matches_injected"] else "FAIL"
        elif tool:
            tool_verdict = "N/A（" + str(tool.get("error")) + "）"
        else:
            tool_verdict = "N/A（未调用）"
        causes = "；".join(
            f"{name}×{count}" for name, count in sorted(m4["diagnostic_counts"].items())
        ) or "无"
        lines.append(
            f"| {scope} | {item['sample_id']} | {m4['consistent']}/{m4['total']} | "
            f"{fmt(m4['ratio'])} | {causes} | {tool_verdict} |"
        )
    lines.append("")

    inconsistent = [
        (item["sample_id"], row) for item in results for row in item["m4"]["inconsistent"]
    ]
    if inconsistent:
        lines.append("**逐条不一致（诊断分类，定因待人工）**")
        lines.append("")
        lines.append("| 样本 | 类型 | subject_key | 期望 status/strength/injected | 实际 | 诊断 | 定因 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for sample_id, row in inconsistent:
            lines.append(
                f"| {sample_id} | {row['memory_type']} | {row['subject_key']} | "
                f"{row['expected_status']}/{row['expected_strength']}/{row['expected_injected']} | "
                f"{row['actual_status']}/{row['actual_strength']}/{row['actual_injected']} | "
                f"{row['cause'] or '字段不符'} | {row['owner']} |"
            )
    else:
        lines.append("- 逐条不一致：无，全部非 archived 记忆的 status / strength / injected 与 Gold 一致。")
    lines.append("")

    lines.append("## 说明")
    lines.append("")
    lines.append("- 本报告只验证「模拟历史 → 记忆层」这一段，不含任何模型生成结果。")
    lines.append("- `injected` 按 `WHEN status='active' ORDER BY strength DESC, id ASC LIMIT 5` 计算，"
                 "与 `agent_tools.get_learning_memory()` 的取数口径一致。")
    lines.append("- 改动 `ref_date` 或样本反馈后必须重跑，旧报告不能沿用。")
    lines.append("")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="核验记忆 Gold Standard 并计算 M1–M4")
    parser.add_argument("--sample", action="append", help="只核验指定样本，可重复")
    parser.add_argument("--reuse", action="store_true", help="复用已建好的库，不重建")
    parser.add_argument("--report", help="报告输出路径")
    parser.add_argument("--metrics", help="M1–M4 长表 CSV 输出路径")
    parser.add_argument("--config", help="config.json 路径")
    parser.add_argument(
        "--runs-dir",
        default=str(HERE / "runs"),
        help="扫描 D 组断言词的 runs 目录（只读，默认 experiments/graduation/runs）",
    )
    parser.add_argument(
        "--no-tool", action="store_true", help="不调用真实 get_learning_memory()，Tool 侧核对记 N/A"
    )
    parser.add_argument(
        "--no-assertion-scan", action="store_true", help="不扫描 runs/，断言词结果记 N/A"
    )
    args = parser.parse_args(argv)

    config = seed_fixture.load_config(args.config)
    targets = list(args.sample or seed_fixture.sample_ids(config))

    # 默认把 S7-empty（补充批次、只在 results 里出现）也纳入核验；显式 --sample 时尊重用户选择
    if not args.sample and EMPTY_SAMPLE not in targets:
        if (seed_fixture.SAMPLES_DIR / f"{EMPTY_SAMPLE}.json").is_file():
            targets.append(EMPTY_SAMPLE)

    results = [
        check_sample(sample_id, config, reuse=args.reuse, tool_check=not args.no_tool)
        for sample_id in targets
    ]

    scan = None
    if not args.no_assertion_scan:
        scan = scan_assertion_keywords(args.runs_dir)

    report = render_report(results, config, scan)
    report_path = Path(args.report) if args.report else (
        seed_fixture.workspace_dir(config) / "memory_check.md"
    )
    report_path.write_text(report, encoding="utf-8")

    metrics_path = Path(args.metrics) if args.metrics else (
        seed_fixture.workspace_dir(config) / "memory_metrics.csv"
    )
    metric_items = metric_rows(results, config, scan)
    write_metrics_csv(metric_items, metrics_path)

    for item in results:
        print(
            f"{item['sample_id']}: memories={len(item['rows'])} "
            f"injected={len(item['injected'])} failures={item['failure_count']} "
            f"M4={item['m4']['consistent']}/{item['m4']['total']}"
        )

    print("报告：" + str(report_path))
    print("指标：" + str(metrics_path))

    return 0 if all(item["failure_count"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
