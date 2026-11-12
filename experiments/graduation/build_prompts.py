"""毕业设计实验脚手架 · 第一阶段：A/B/C/D 四组 Prompt 构建。

只拼 Prompt，不调用模型：

- 输入：同一份模拟历史（`samples/S*.json`）+ 同一份知识库结果（`config.json`，固定为空）
- 四组只改设计 §2.2 允许的输入槽位：

      A：空反馈 / 无短期上下文 / 无长期记忆
      B：近 7 天反馈 + 短期上下文
      C：空反馈 + 长期记忆（get_learning_memory，limit=5，只取 active）
      D：近 7 天反馈 + 短期上下文 + 长期记忆

- 输出：每份 Prompt 的系统段与用户段、SHA256、组间 Diff、与"差异只来自允许槽位"的核验结果

时间基准：`config.ref_date` 固定。`database.get_feedback_since()` 内部用 `datetime.now()`
算时间窗，所以本模块在**只读调用期间**把 `database.datetime` 换成固定时钟，
不改任何产品文件；`agent_tools` / `memory_context` 里 `from database import ...` 绑定的
是同一个函数对象，读的仍是 database 模块的全局 `datetime`，因此一次替换对两者都生效。

命令行：

    python experiments/graduation/build_prompts.py            # 重建 7 个库并构建全部 Prompt
    python experiments/graduation/build_prompts.py --reuse    # 复用已建好的库
    python experiments/graduation/build_prompts.py --sample S4
"""

import argparse
import difflib
import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import seed_fixture  # noqa: E402

# 设计 §2.2 的四组输入矩阵。B0 与敏感性分析不在此阶段。
GROUP_SLOTS = {
    "A": {"feedback": False, "short_term": False, "memory": False},
    "B": {"feedback": True, "short_term": True, "memory": False},
    "C": {"feedback": False, "short_term": False, "memory": True},
    "D": {"feedback": True, "short_term": True, "memory": True},
}

MEMORY_HEADER = "【真实长期学习记忆检索结果】"
SHORT_TERM_HEADER = "【学习者近期情况（程序统计，非模型总结）】"
# 与 agent_plan_draft._build_draft_user_content 里的两句相邻字面量逐字一致（渲染后是一行）
MEMORY_USAGE_NOTE = (
    "answer 与 plan.reason 不得把上面的长期学习记忆说成用户当前状态；"
    "引用记忆时只能按它写的历史事实表述。"
)

DIFF_PAIRS = (("A", "B"), ("A", "C"), ("B", "D"))


# ---------------------------------------------------------------------------
# 固定时钟
# ---------------------------------------------------------------------------


@contextmanager
def fixed_clock(database, moment):
    """把 database 模块里的 datetime 换成只返回 moment 的替身（仅覆盖只读调用）。"""

    original = database.datetime

    class _FixedDatetime:
        @staticmethod
        def now(tz=None):
            return moment

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

        @staticmethod
        def strptime(value, fmt):
            return datetime.strptime(value, fmt)

    database.datetime = _FixedDatetime

    try:
        yield moment
    finally:
        database.datetime = original


# ---------------------------------------------------------------------------
# 模块与输入
# ---------------------------------------------------------------------------


def project_api(config=None):
    """返回 (database, memory_service, agent_tools, memory_context, agent_plan_draft, feedback_service)。"""

    database, memory_service = seed_fixture.project_modules(config)

    root = str(seed_fixture.PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    import agent_plan_draft  # noqa: E402
    import agent_tools  # noqa: E402
    import feedback_service  # noqa: E402
    import memory_context  # noqa: E402

    return {
        "database": database,
        "memory_service": memory_service,
        "agent_tools": agent_tools,
        "memory_context": memory_context,
        "agent_plan_draft": agent_plan_draft,
        "feedback_service": feedback_service,
    }


def activate_sample(sample_id, config, reuse=False):
    """建库（或复用）并把 database.DATABASE_PATH 指向该样本的库。"""

    if reuse:
        db_path = seed_fixture.workspace_dir(config) / f"{sample_id}.db"

        if not db_path.is_file():
            raise FileNotFoundError(f"{sample_id} 还没有库，请先运行 seed_fixture.py")
    else:
        seeded = seed_fixture.seed_sample(sample_id, config)

        if not (seeded["rebuild"] or {}).get("ok"):
            raise RuntimeError(f"{sample_id} 记忆重建失败：{seeded['rebuild']}")

        db_path = Path(seeded["db_path"])

    api = project_api(config)
    api["database"].DATABASE_PATH = db_path

    return db_path, api


def empty_feedback_result(days):
    """A/C 组的空反馈对象。

    预登记口径见 docs/GRADUATION-EXPERIMENT-DESIGN.md §2.2：空反馈组的
    `feedback_result["summary"]` 必须是空 dict，而不是 calculate_summary([]) 的全零统计。
    这里刻意不附加 latest_status / latest_reason 之类由产品在"有反馈"分支才补的字段，
    让 A/C 看到的统计槽位就是"没有统计"，与 B/D 的真实摘要形成干净对照。
    """

    return {"days": days, "count": 0, "summary": {}, "feedback": []}


def collect_inputs(sample, config, api, moment):
    """收集四组共用的原始输入（只读，全部在固定时钟下进行）。"""

    agent_tools = api["agent_tools"]
    memory_context = api["memory_context"]
    agent_plan_draft = api["agent_plan_draft"]

    days = int(config["recent_feedback_days"])
    limit = int(config["memory_limit"])
    plan_date = moment.date().isoformat()

    with fixed_clock(api["database"], moment):
        feedback_result = agent_tools.get_recent_feedback(days=days)
        difficult, difficult_error = agent_tools._derive_difficult_previous_task()
        short_term = memory_context.render_short_term(
            memory_context.collect_short_term(plan_date)
        )
        memory_tool = agent_tools.get_learning_memory(limit=limit)

    if difficult_error:
        raise RuntimeError(f"{sample['sample_id']} 无法推导难度标记：{difficult_error}")

    memories = agent_plan_draft._memory_memories(memory_tool)
    memory_result = {"count": len(memories), "memories": memories} if memories else None

    return {
        "user_message": sample["current_user_request"],
        "knowledge_results": list(config.get("knowledge_results") or []),
        "search_failed": bool(config.get("knowledge_search_failed")),
        "feedback_result": feedback_result,
        "empty_feedback": empty_feedback_result(days),
        "difficult": bool(difficult),
        "short_term_block": short_term["text"],
        "short_term_info": short_term["info"],
        "memory_result": memory_result,
        "memory_tool": memory_tool,
        "plan_date": plan_date,
    }


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def build_group_content(group, inputs, api):
    """按组矩阵调用产品函数拼出该组的用户消息（不做任何后处理）。"""

    slots = GROUP_SLOTS[group]
    feedback_result = inputs["feedback_result"] if slots["feedback"] else inputs["empty_feedback"]
    difficult = inputs["difficult"] if slots["feedback"] else False

    return api["agent_plan_draft"]._build_draft_user_content(
        inputs["user_message"],
        feedback_result,
        inputs["knowledge_results"],
        inputs["search_failed"],
        difficult,
        inputs["short_term_block"] if slots["short_term"] else None,
        inputs["memory_result"] if slots["memory"] else None,
    )


def memory_block_text(inputs, api):
    return api["agent_plan_draft"]._memory_block(inputs["memory_result"])


def strip_memory(content, memory_block):
    """去掉长期记忆区块与随之出现的一句使用约束，剩下应与无记忆组逐字相同。"""

    marker = "\n\n" + memory_block

    if marker not in content:
        raise AssertionError("记忆区块未按预期拼接，无法做等价性核验")

    content = content.replace(marker, "", 1)

    if MEMORY_USAGE_NOTE + "\n" not in content:
        raise AssertionError("记忆使用约束缺失，无法做等价性核验")

    return content.replace(MEMORY_USAGE_NOTE + "\n", "", 1)


def base_segment(content):
    """用户请求之后、知识库区块之前的"计划规则 + 反馈 JSON"段。"""

    start = content.index("\n\n") + 2
    return content[start:content.index("真实知识库检索状态")]


def knowledge_segment(content):
    """知识库区块整段（四组必须逐字相同）。"""

    start = content.index("真实知识库检索状态")
    end = content.find("\n\n", start)

    return content[start:] if end == -1 else content[start:end]


def injected_labels(db_path, limit):
    """复刻注入口径，返回本次真正会进 Prompt 的 (memory_type, subject_label)。"""

    conn = sqlite3.connect(str(db_path))

    try:
        rows = conn.execute(
            "SELECT memory_type, COALESCE(subject_label, subject_key) "
            "FROM learning_memories WHERE status = 'active' "
            "ORDER BY strength DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return {(row[0], row[1]) for row in rows}


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_sha256(system, user):
    """整份 Prompt 的 SHA256：按 [system, user] 两条消息的 JSON 计算，避免拼接歧义。"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    return sha256_text(json.dumps(messages, ensure_ascii=False, sort_keys=True))


def check_group_relations(contents, inputs, api, db_path, config):
    """核验"四组差异只来自设计允许的槽位"，返回 [(name, ok, detail)]。"""

    checks = []
    memory = inputs["memory_result"]
    block = memory_block_text(inputs, api) if memory else ""

    for group in GROUP_SLOTS:
        slots = GROUP_SLOTS[group]
        content = contents[group]

        has_memory = MEMORY_HEADER in content
        has_short_term = SHORT_TERM_HEADER in content

        checks.append((
            f"{group}·记忆槽位",
            has_memory == slots["memory"],
            f"期望记忆={slots['memory']}，实际={has_memory}",
        ))
        checks.append((
            f"{group}·短期上下文槽位",
            has_short_term == slots["short_term"],
            f"期望短期={slots['short_term']}，实际={has_short_term}",
        ))

    # C = A + 记忆；D = B + 记忆（逐字等价，不允许出现别的差异）
    if memory:
        checks.append((
            "C 去记忆后与 A 逐字相同",
            strip_memory(contents["C"], block) == contents["A"],
            "C - 记忆区块 == A",
        ))
        checks.append((
            "D 去记忆后与 B 逐字相同",
            strip_memory(contents["D"], block) == contents["B"],
            "D - 记忆区块 == B",
        ))

    # 反馈/难度槽位：A 与 C 相同（都是空反馈），B 与 D 相同（同一份反馈）
    checks.append((
        "A 与 C 的反馈段逐字相同",
        base_segment(contents["A"]) == base_segment(contents["C"]),
        "空反馈组共用同一份计划规则与反馈 JSON",
    ))
    checks.append((
        "B 与 D 的反馈段逐字相同",
        base_segment(contents["B"]) == base_segment(contents["D"]),
        "有反馈组共用同一份近期反馈与难度标记",
    ))

    # 知识库槽位：四组同一份（本阶段固定为空结果）
    knowledge = {knowledge_segment(contents[group]) for group in GROUP_SLOTS}
    checks.append(("四组知识库区块逐字相同", len(knowledge) == 1, "knowledge 段一致"))

    # 注入公平性：进 Prompt 的记忆必须就是 status=active 的前 limit 条
    if memory:
        expected = injected_labels(db_path, int(config["memory_limit"]))
        actual = {(item["type"], item["subject"]) for item in memory["memories"]}

        checks.append((
            "注入记忆与 status='active' 前 N 条一致",
            actual == expected,
            f"期望 {sorted(expected)}，实际 {sorted(actual)}",
        ))

        checks.append((
            "记忆区块只出现一次",
            contents["D"].count(block) == 1,
            f"出现 {contents['D'].count(block)} 次",
        ))

    return checks


def build_sample_prompts(sample_id, config, reuse=False):
    """构建一个样本的四组 Prompt（含核验与 Diff），不写文件。"""

    sample = seed_fixture.load_sample(sample_id, config)
    db_path, api = activate_sample(sample_id, config, reuse=reuse)
    moment = seed_fixture.ref_datetime(config)

    inputs = collect_inputs(sample, config, api, moment)
    system = api["agent_plan_draft"].DRAFT_SYSTEM_PROMPT

    contents = {
        group: build_group_content(group, inputs, api) for group in GROUP_SLOTS
    }

    prompts = {}

    for group, content in contents.items():
        prompts[group] = {
            "system": system,
            "user": content,
            "system_sha256": sha256_text(system),
            "user_sha256": sha256_text(content),
            "prompt_sha256": prompt_sha256(system, content),
            "chars": len(content),
        }

    checks = check_group_relations(contents, inputs, api, db_path, config)

    return {
        "sample_id": sample_id,
        "db_path": str(db_path),
        "plan_date": inputs["plan_date"],
        "inputs": {
            "feedback_count": inputs["feedback_result"]["count"],
            "feedback_latest_status": inputs["feedback_result"]["summary"].get("latest_status"),
            "empty_feedback": inputs["empty_feedback"],
            "feedback_summary": inputs["feedback_result"]["summary"],
            "difficult": inputs["difficult"],
            "short_term_chars": inputs["short_term_info"]["length"],
            "short_term_dropped": inputs["short_term_info"]["dropped"],
            "memory_count": (inputs["memory_result"] or {}).get("count", 0),
            "memory_types": sorted(
                (inputs["memory_result"] or {}).get("memories", [])[0].keys()
            ),
            "knowledge_count": len(inputs["knowledge_results"]),
            "knowledge_search_failed": inputs["search_failed"],
        },
        "prompts": prompts,
        "contents": contents,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def render_diff(sample_id, left, right, contents):
    diff = difflib.unified_diff(
        contents[left].splitlines(),
        contents[right].splitlines(),
        fromfile=f"{sample_id}/{left}",
        tofile=f"{sample_id}/{right}",
        lineterm="",
        n=2,
    )

    return "\n".join(diff)


def write_sample_output(sample_dir, build):
    sample_dir.mkdir(parents=True, exist_ok=True)

    for group, prompt in build["prompts"].items():
        text = (
            "===== SYSTEM =====\n"
            + prompt["system"]
            + "\n\n===== USER =====\n"
            + prompt["user"]
            + "\n"
        )
        (sample_dir / f"{group}.txt").write_text(text, encoding="utf-8")

    diff_lines = []
    diff_lines.append(f"# {build['sample_id']} Prompt Diff")
    diff_lines.append("")

    for left, right in DIFF_PAIRS:
        diff_lines.append(f"## {left} → {right}")
        diff_lines.append("")
        diff = render_diff(build["sample_id"], left, right, build["contents"])
        diff_lines.append("```diff")
        diff_lines.append(diff or "（无差异）")
        diff_lines.append("```")
        diff_lines.append("")

    (sample_dir / "diff.md").write_text("\n".join(diff_lines), encoding="utf-8")

    summary = {
        "sample_id": build["sample_id"],
        "db_path": build["db_path"],
        "plan_date": build["plan_date"],
        "inputs": build["inputs"],
        "prompts": {
            group: {key: value for key, value in prompt.items() if key != "user"}
            for group, prompt in build["prompts"].items()
        },
        "checks": [
            {"name": name, "ok": ok, "detail": detail} for name, ok, detail in build["checks"]
        ],
    }

    (sample_dir / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return summary


def render_report(summaries):
    lines = []
    lines.append("# A/B/C/D 四组 Prompt 构建报告")
    lines.append("")
    lines.append("本报告由 `experiments/graduation/build_prompts.py` 生成；**没有调用任何模型**。")
    lines.append("")
    lines.append("| 样本 | 反馈条数 | 难度标记 | 短期上下文字符 | 记忆条数 | A 字符 | B 字符 | C 字符 | D 字符 | 核验 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    for summary in summaries:
        inputs = summary["inputs"]
        prompts = summary["prompts"]
        failed = [item for item in summary["checks"] if not item["ok"]]
        verdict = "通过" if not failed else f"失败 {len(failed)} 项"

        lines.append(
            f"| {summary['sample_id']} | {inputs['feedback_count']} | "
            f"{'是' if inputs['difficult'] else '否'} | {inputs['short_term_chars']} | "
            f"{inputs['memory_count']} | {prompts['A']['chars']} | {prompts['B']['chars']} | "
            f"{prompts['C']['chars']} | {prompts['D']['chars']} | {verdict} |"
        )

    lines.append("")
    lines.append("## Prompt SHA256")
    lines.append("")
    lines.append("| 样本 | 组 | 字符数 | system_sha256 | user_sha256 | prompt_sha256 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    for summary in summaries:
        for group in GROUP_SLOTS:
            prompt = summary["prompts"][group]
            lines.append(
                f"| {summary['sample_id']} | {group} | {prompt['chars']} | "
                f"`{prompt['system_sha256'][:12]}…` | `{prompt['user_sha256'][:12]}…` | "
                f"`{prompt['prompt_sha256'][:12]}…` |"
            )

    lines.append("")
    lines.append("## 核验明细")
    lines.append("")

    for summary in summaries:
        lines.append(f"### {summary['sample_id']}")
        lines.append("")

        for item in summary["checks"]:
            mark = "通过" if item["ok"] else "失败"
            lines.append(f"- {mark} · {item['name']}（{item['detail']}）")

        lines.append("")

    lines.append("## 说明")
    lines.append("")
    lines.append("- 四组共用同一份用户请求、同一份知识库结果（固定为空）与同一份模拟历史。")
    lines.append("- 组间差异只来自设计 §2.2 允许的三个槽位：近期反馈、短期上下文、长期记忆。")
    lines.append("- `ref_date` 固定；`get_feedback_since()` 的时间窗由固定时钟替身提供，不读本机时钟。")
    lines.append("- 本阶段不调用模型、不接知识库；Prompt 只是待测文本，不是实验结果。")
    lines.append("")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建 A/B/C/D 四组实验 Prompt")
    parser.add_argument("--sample", action="append", help="只构建指定样本，可重复")
    parser.add_argument("--reuse", action="store_true", help="复用已建好的库，不重建")
    parser.add_argument("--config", help="config.json 路径")
    args = parser.parse_args(argv)

    config = seed_fixture.load_config(args.config)
    targets = args.sample or seed_fixture.sample_ids(config)
    out_root = seed_fixture.workspace_dir(config) / "prompts"

    summaries = []
    failures = 0

    for sample_id in targets:
        build = build_sample_prompts(sample_id, config, reuse=args.reuse)
        summary = write_sample_output(out_root / sample_id, build)
        summaries.append(summary)

        bad = [item for item in summary["checks"] if not item["ok"]]
        failures += len(bad)

        print(
            f"{sample_id}: feedback={summary['inputs']['feedback_count']} "
            f"memory={summary['inputs']['memory_count']} "
            f"chars A/B/C/D="
            f"{summary['prompts']['A']['chars']}/{summary['prompts']['B']['chars']}/"
            f"{summary['prompts']['C']['chars']}/{summary['prompts']['D']['chars']} "
            f"checks_failed={len(bad)}"
        )

        for item in bad:
            print(f"  - 失败：{item['name']}（{item['detail']}）")

    (out_root / "report.md").write_text(render_report(summaries), encoding="utf-8")
    (out_root / "hashes.json").write_text(
        json.dumps(
            {
                summary["sample_id"]: {
                    group: summary["prompts"][group]["prompt_sha256"]
                    for group in GROUP_SLOTS
                }
                for summary in summaries
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("输出目录：" + str(out_root))

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
