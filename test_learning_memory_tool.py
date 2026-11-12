"""Task 6：get_learning_memory Tool 的测试（v16 §6.2 / §8.2 T1–T6）。

设计要点（被测契约）：
    1. Schema / TOOL_NAMES / ARGUMENT_RULES / dispatcher.TOOLS 四处必须一致
    2. memory_type 只有四种，preference 之类一律拒绝（不静默扩大查询范围）
    3. limit 只接受 1~5 的整数，99 / 0 / True / "5" 全部拒绝
    4. 返回结构必须带 episode，且不泄露 memory_id / value_json / 事件 / pending
    5. 总返回字符 ≤ 2000，超出按 strength 升序丢弃并记 info.dropped_by_budget
    6. 只读：调用前后数据库字节不变

测试使用独立临时库（每个用例一个文件），并校验真实 feedback.db 未被污染。
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

os.environ.setdefault("FEEDBACK_DB_PATH", str(PROJECT_DIR / "_test_memory_tool_env.db"))
os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")
os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")

import agent_dispatcher  # noqa: E402
import agent_schema  # noqa: E402
import agent_tools  # noqa: E402
import database  # noqa: E402
import memory_service  # noqa: E402


REAL_DB = PROJECT_DIR / "feedback.db"
_CREATED_DBS = []
_DB_SEQ = [0]
TEST_DB = [None]


def check(name, condition, detail=None):
    if condition:
        print(f"PASS: {name}")
        return

    raise AssertionError(f"FAILED: {name}" + (f" | {detail}" if detail else ""))


def remove_file(path):
    path = Path(path)

    if not path.exists():
        return

    for _ in range(20):
        try:
            path.unlink()
            return
        except OSError:
            time.sleep(0.05)

    raise AssertionError(f"FAILED: 无法删除测试库 {path}")


def use(path):
    database.DATABASE_PATH = str(path)
    TEST_DB[0] = Path(path)


def fresh():
    _DB_SEQ[0] += 1
    path = PROJECT_DIR / f"_test_memory_tool_{_DB_SEQ[0]}.db"
    remove_file(path)
    use(path)
    _CREATED_DBS.append(path)
    database.init_db()
    return path


def cleanup_databases():
    for path in _CREATED_DBS:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def connect(path=None):
    return sqlite3.connect(str(path or TEST_DB[0]))


def insert_memory(
    memory_type="difficulty",
    subject_key="条件概率",
    subject_label="条件概率练习",
    content="「条件概率练习」近期 2 次因难度太高未完成（30 天内）",
    strength=0.4,
    evidence_count=2,
    episode=1,
    status="active",
    first_seen_at="2026-11-01T10:00:00",
):
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO learning_memories ("
            " memory_type, subject_key, subject_label, content, value_json, strength,"
            " evidence_count, contradict_count, episode, episode_start_feedback_id,"
            " episode_started_at, pending_episode, pending_start_feedback_id, status,"
            " first_seen_at, last_supported_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                memory_type,
                subject_key,
                subject_label,
                content,
                "{}",
                strength,
                evidence_count,
                0,
                episode,
                1,
                first_seen_at,
                None,
                None,
                status,
                first_seen_at,
                first_seen_at,
                first_seen_at,
            ),
        )
        return cursor.lastrowid


def insert_evidence(memory_id, feedback_id, relation="supports", episode=1):
    with connect() as conn:
        conn.execute(
            "INSERT INTO memory_evidence ("
            " memory_id, feedback_id, plan_id, episode, relation, weight, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (memory_id, feedback_id, None, episode, relation, 1.0, "2026-11-01T10:00:00"),
        )


def memory_types_seen(response):
    return [item["type"] for item in response["memories"]]


# ==========================
# T1：四处定义必须一致
# ==========================


def test_schema_sources_agree():
    fresh()

    schema_names = [schema["function"]["name"] for schema in agent_schema.TOOL_SCHEMAS]

    check("T1 schema 与 TOOL_NAMES 顺序一致", schema_names == list(agent_schema.TOOL_NAMES), schema_names)
    check(
        "T1 ARGUMENT_RULES 覆盖全部工具",
        set(agent_schema.ARGUMENT_RULES) == set(agent_schema.TOOL_NAMES),
        sorted(agent_schema.ARGUMENT_RULES),
    )
    check(
        "T1 dispatcher.TOOLS 覆盖全部工具",
        set(agent_dispatcher.TOOLS) == set(agent_schema.TOOL_NAMES),
        sorted(agent_dispatcher.TOOLS),
    )
    check("T1 工具数量为 4", len(schema_names) == 4, schema_names)

    schema = next(
        item for item in agent_schema.TOOL_SCHEMAS
        if item["function"]["name"] == "get_learning_memory"
    )
    properties = schema["function"]["parameters"]["properties"]

    check(
        "T1 schema 参数与 ARGUMENT_RULES 一致",
        set(properties) == set(agent_schema.ARGUMENT_RULES["get_learning_memory"]),
        sorted(properties),
    )
    check(
        "T1 schema 的 memory_type 枚举与实现一致",
        tuple(properties["memory_type"]["enum"]) == agent_tools.MEMORY_TYPES,
        properties["memory_type"]["enum"],
    )
    check(
        "T1 schema 的 limit 上限与实现一致",
        agent_schema.ARGUMENT_RULES["get_learning_memory"]["limit"]["max"]
        == agent_tools.MAX_MEMORY_LIMIT,
    )
    check(
        "T1 描述里明确 strength 不是概率",
        "不是概率" in schema["function"]["description"],
        schema["function"]["description"],
    )


# ==========================
# T2：四种合法类型
# ==========================


def test_four_memory_types_are_accepted():
    fresh()

    for memory_type in agent_tools.MEMORY_TYPES:
        insert_memory(memory_type=memory_type, subject_key="概率", subject_label="概率练习")

    for memory_type in agent_tools.MEMORY_TYPES:
        response = agent_tools.get_learning_memory(memory_type=memory_type)

        check(f"T2 {memory_type} 返回 ok", response["ok"] is True, response)
        check(f"T2 {memory_type} 命中 1 条", response["count"] == 1, response["count"])
        check(f"T2 {memory_type} 类型正确", memory_types_seen(response) == [memory_type])

    all_types = agent_tools.get_learning_memory()

    check("T2 不传类型返回全部 4 条", all_types["count"] == 4, all_types["count"])
    check(
        "T2 不传类型时四种类型都在",
        sorted(memory_types_seen(all_types)) == sorted(agent_tools.MEMORY_TYPES),
        memory_types_seen(all_types),
    )


# ==========================
# T3：非法类型拒绝
# ==========================


def test_invalid_memory_type_is_rejected():
    fresh()
    insert_memory()

    for value in ("preference", "", "difficulty ", 1, True, [], {}):
        response = agent_tools.get_learning_memory(memory_type=value)

        check(
            f"T3 memory_type={value!r} 被拒绝",
            response["ok"] is False
            and response["error_type"] == agent_tools.ERROR_INVALID_ARGUMENT
            and response["count"] == 0
            and response["memories"] == [],
            response,
        )

    # 拒绝时不能悄悄"返回全部"
    check(
        "T3 拒绝时不会退化成无过滤查询",
        len(agent_tools.get_learning_memory()["memories"]) == 1,
    )


# ==========================
# T4：limit / subject 约束
# ==========================


def test_invalid_limit_is_rejected():
    fresh()
    insert_memory()

    for value in (99, 0, -1, 2.5, True, "5", None, [5]):
        response = agent_tools.get_learning_memory(limit=value)

        check(
            f"T4 limit={value!r} 被拒绝",
            response["ok"] is False and response["error_type"] == "invalid_argument",
            response,
        )

    for value in (1, 3, 5):
        response = agent_tools.get_learning_memory(limit=value)
        check(f"T4 limit={value} 被接受", response["ok"] is True, response)


def test_invalid_subject_is_rejected():
    fresh()

    for value in ("", "   ", "\n\t", 5, [], {"a": 1}):
        response = agent_tools.get_learning_memory(subject=value)

        check(
            f"T4 subject={value!r} 被拒绝",
            response["ok"] is False and response["error_type"] == "invalid_argument",
            response,
        )

    too_long = "概" * (agent_tools.MAX_MEMORY_SUBJECT_CHARS + 1)
    response = agent_tools.get_learning_memory(subject=too_long)

    check("T4 subject 超长被拒绝", response["ok"] is False and response["error_type"] == "invalid_argument")

    exact = "概" * agent_tools.MAX_MEMORY_SUBJECT_CHARS
    check("T4 subject 正好 60 字被接受", agent_tools.get_learning_memory(subject=exact)["ok"] is True)


# ==========================
# T5：返回结构
# ==========================


def test_result_structure_has_episode_and_no_internal_fields():
    fresh()

    memory_id = insert_memory(
        memory_type="task_performance",
        subject_key="线性代数",
        subject_label="线性代数练习",
        content="「线性代数练习」近期 4 次任务完成率 0.75",
        strength=0.62,
        evidence_count=4,
        episode=2,
    )
    for feedback_id in (11, 12, 13, 14, 15, 16):
        insert_evidence(memory_id, feedback_id, episode=2)

    response = agent_tools.get_learning_memory(memory_type="task_performance")
    item = response["memories"][0]

    check("T5 返回 ok 与 count", response["ok"] is True and response["count"] == 1)
    check(
        "T5 字段集合固定",
        set(item) == {
            "type",
            "subject",
            "content",
            "strength",
            "evidence_count",
            "episode",
            "evidence_feedback_ids",
            "updated_at",
        },
        sorted(item),
    )
    check("T5 带 episode", item["episode"] == 2, item["episode"])
    check("T5 type/subject 正确", item["type"] == "task_performance" and item["subject"] == "线性代数练习")
    check("T5 strength 原样透传", item["strength"] == 0.62, item["strength"])
    check("T5 evidence_count 正确", item["evidence_count"] == 4, item["evidence_count"])
    check(
        "T5 证据 id 最多 5 个且升序",
        item["evidence_feedback_ids"] == [12, 13, 14, 15, 16],
        item["evidence_feedback_ids"],
    )
    for forbidden in ("memory_id", "memory_type", "subject_key", "value_json", "status", "pending"):
        check(f"T5 不泄露 {forbidden}", forbidden not in item)

    encoded = json.dumps(response, ensure_ascii=False)
    for forbidden in ("memory_id", "value_json", "pending_episode"):
        check(f"T5 序列化后不含 {forbidden}", forbidden not in encoded)


# ==========================
# T6：字符预算
# ==========================


def test_budget_drops_weakest_and_stays_under_limit():
    fresh()

    for index, strength in enumerate((0.9, 0.8, 0.7, 0.6, 0.5)):
        insert_memory(
            memory_type="task_topic",
            subject_key=f"主题{index}",
            subject_label=f"主题{index}",
            content="「主题」近期 5 次反馈中 5 次出现（长文本占位）" + "细节" * 120,
            strength=strength,
            evidence_count=5,
        )

    response = agent_tools.get_learning_memory(memory_type="task_topic", limit=5)
    encoded = json.dumps(response, ensure_ascii=False)

    check("T6 总字符不超过 2000", len(encoded) <= agent_tools.MAX_MEMORY_RESULT_CHARS, len(encoded))
    check("T6 info.chars 与实际一致", response["info"]["chars"] == len(encoded), response["info"])
    check("T6 丢弃数量被记录", response["info"]["dropped_by_budget"] > 0, response["info"])
    check(
        "T6 保留的是更强的记忆（升序丢弱的）",
        [item["strength"] for item in response["memories"]]
        == sorted((item["strength"] for item in response["memories"]), reverse=True),
        [item["strength"] for item in response["memories"]],
    )
    check(
        "T6 count 与 memories 长度一致",
        response["count"] == len(response["memories"]),
        response["count"],
    )
    check("T6 reason 说明丢弃原因", "丢弃" in (response["reason"] or ""), response["reason"])

    # 单条就超预算时：全部丢弃但仍是合法响应
    fresh()
    insert_memory(
        memory_type="task_topic",
        subject_key="超长",
        subject_label="超长",
        content="超" * 3000,
        strength=0.5,
    )
    huge = agent_tools.get_learning_memory()

    check("T6 单条超预算时被丢弃", huge["ok"] is True and huge["count"] == 0, huge)
    check("T6 丢弃后仍在预算内", huge["info"]["chars"] <= agent_tools.MAX_MEMORY_RESULT_CHARS)


# ==========================
# 过滤 / 空结果 / 只读 / dispatcher
# ==========================


def test_subject_filter_and_empty_result():
    fresh()

    insert_memory(memory_type="difficulty", subject_key="条件概率", subject_label="条件概率练习")
    insert_memory(memory_type="difficulty", subject_key="线性代数", subject_label="线性代数练习")

    hit = agent_tools.get_learning_memory(memory_type="difficulty", subject="条件概率练习")

    check("T7 subject 归一化后精确匹配", hit["count"] == 1, hit["count"])
    check("T7 命中类型正确", memory_types_seen(hit) == ["difficulty"])

    miss = agent_tools.get_learning_memory(subject="完全无关的主题")

    check("T7 空结果是正常响应", miss["ok"] is True and miss["count"] == 0, miss)
    check("T7 空结果 error 为 None", miss["error"] is None and miss["error_type"] is None)
    check("T7 空结果 reason 有说明", bool(miss["reason"]), miss["reason"])

    only_archived = insert_memory(status="archived", subject_key="归档主题")
    check("T7 archived 记忆已插入", only_archived > 0)
    check("T7 只检索 active 记忆", agent_tools.get_learning_memory(subject="归档主题")["count"] == 0)


def test_tool_is_read_only():
    path = fresh()

    insert_memory(memory_type="difficulty", subject_key="只读", subject_label="只读练习")
    before = path.read_bytes()

    for _ in range(3):
        agent_tools.get_learning_memory()
        agent_tools.get_learning_memory(memory_type="difficulty", subject="只读练习", limit=1)

    check("T8 工具只读，数据库字节不变", path.read_bytes() == before)


def test_dispatcher_route():
    fresh()
    insert_memory(memory_type="time_pattern", subject_key="medium", subject_label="中等难度任务")

    ok = agent_dispatcher.dispatch(
        {"name": "get_learning_memory", "arguments": {"memory_type": "time_pattern", "limit": 2}}
    )

    check("T9 dispatcher 成功派发", ok["ok"] is True, ok)
    check("T9 dispatcher 透传工具结果", ok["result"]["count"] == 1, ok["result"])

    rejected = agent_dispatcher.dispatch(
        {"name": "get_learning_memory", "arguments": {"memory_type": "preference"}}
    )

    check(
        "T9 dispatcher 拒绝非法 memory_type",
        rejected["ok"] is False and rejected["error_type"] == "invalid_arguments",
        rejected,
    )

    unknown = agent_dispatcher.dispatch(
        {"name": "get_learning_memory", "arguments": {"subject": 1}}
    )

    check(
        "T9 dispatcher 拒绝非字符串 subject",
        unknown["ok"] is False and unknown["error_type"] == "invalid_arguments",
        unknown,
    )

    extra = agent_dispatcher.dispatch(
        {"name": "get_learning_memory", "arguments": {"status": "active"}}
    )

    check(
        "T9 dispatcher 拒绝额外参数",
        extra["ok"] is False and "不支持的参数" in (extra["error"] or ""),
        extra,
    )


def test_real_database_untouched():
    before = REAL_DB.read_bytes() if REAL_DB.exists() else None

    use(REAL_DB)
    agent_tools.get_learning_memory()

    after = REAL_DB.read_bytes() if REAL_DB.exists() else None

    check("T10 真实 feedback.db 未被修改", before == after)


def main():
    tests = [
        test_schema_sources_agree,
        test_four_memory_types_are_accepted,
        test_invalid_memory_type_is_rejected,
        test_invalid_limit_is_rejected,
        test_invalid_subject_is_rejected,
        test_result_structure_has_episode_and_no_internal_fields,
        test_budget_drops_weakest_and_stays_under_limit,
        test_subject_filter_and_empty_result,
        test_tool_is_read_only,
        test_dispatcher_route,
        test_real_database_untouched,
    ]

    for test in tests:
        print(f"\n=== {test.__name__} ===")
        test()

    cleanup_databases()
    print(f"\nALL {len(tests)} TESTS COMPLETED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
