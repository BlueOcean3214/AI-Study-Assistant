"""毕业设计实验脚手架 · 第一阶段：Gold Standard 核验。

读回 `seed_fixture.py` 建好的库，把**实际生成的记忆**与 `samples/S*.json` 里的
`gold_standard` 逐条比对，并输出 Markdown 报告。

比对维度（与 docs/GRADUATION-EXPERIMENT-DESIGN.md §4.1、§7.3 一致）：

    status / strength / supports / contradicts / unit / 前缀 / injected / content

另外检查：记忆集合与 Gold Standard 完全一致（多一条也算失败）、注入集合一致、
「不应生成的记忆」确实不存在、待定证据为 0、重建本身成功（ok=True）。

命令行：

    python experiments/graduation/check_memory.py                # 重建后核验，写默认报告
    python experiments/graduation/check_memory.py --reuse        # 复用已有库
    python experiments/graduation/check_memory.py --report out/x.md
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import seed_fixture  # noqa: E402

STRENGTH_TOLERANCE = 1e-6


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


def check_sample(sample_id, config, reuse=False):
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

    return verify_sample(sample, dump, db_path, config, rebuild)


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
        "failure_count": failures,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def render_report(results, config):
    lines = []
    lines.append("# 记忆 Gold Standard 核验报告")
    lines.append("")
    lines.append(f"- 参照时间 REF_DATE：`{config['ref_date']}`")
    lines.append("- 数据来源：`experiments/graduation/samples/S*.json`（模拟数据，非真实用户）")
    lines.append("- 比对实现：`experiments/graduation/check_memory.py`，直接读回 SQLite 行，不重新渲染文案")
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

    lines.append("## 说明")
    lines.append("")
    lines.append("- 本报告只验证「模拟历史 → 记忆层」这一段，不含任何模型生成结果。")
    lines.append("- `injected` 按 `WHEN status='active' ORDER BY strength DESC, id ASC LIMIT 5` 计算，"
                 "与 `agent_tools.get_learning_memory()` 的取数口径一致。")
    lines.append("- 改动 `ref_date` 或样本反馈后必须重跑，旧报告不能沿用。")
    lines.append("")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="核验记忆 Gold Standard")
    parser.add_argument("--sample", action="append", help="只核验指定样本，可重复")
    parser.add_argument("--reuse", action="store_true", help="复用已建好的库，不重建")
    parser.add_argument("--report", help="报告输出路径")
    parser.add_argument("--config", help="config.json 路径")
    args = parser.parse_args(argv)

    config = seed_fixture.load_config(args.config)
    targets = args.sample or seed_fixture.sample_ids(config)

    results = [check_sample(sample_id, config, reuse=args.reuse) for sample_id in targets]

    report = render_report(results, config)
    report_path = Path(args.report) if args.report else (
        seed_fixture.workspace_dir(config) / "memory_check.md"
    )
    report_path.write_text(report, encoding="utf-8")

    for item in results:
        print(
            f"{item['sample_id']}: memories={len(item['rows'])} "
            f"injected={len(item['injected'])} failures={item['failure_count']}"
        )

    print("报告：" + str(report_path))

    return 0 if all(item["failure_count"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
