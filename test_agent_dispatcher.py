"""Agent Dispatcher 测试：直接运行 python test_agent_dispatcher.py。

隔离：
- 反馈数据库 -> 临时 sqlite 夹具（patch database.DATABASE_PATH）
- 向量缓存   -> 临时目录（patch vector_cache.CACHE_DIR）
结束时校验真实 feedback.db / knowledge/ / .rag_cache 未被改动。
"""

import gc
import re
import shutil
import time
from pathlib import Path

import agent_dispatcher
import agent_tools
import database
import vector_cache


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_dispatcher.db"
CACHE_FIXTURE = PROJECT_DIR / "_test_cache_dispatcher"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
REAL_CACHE_EXISTED = REAL_CACHE.exists()
KNOWLEDGE_SNAPSHOT = sorted(
    (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
    for path in KNOWLEDGE_DIR.rglob("*.txt")
)

if FIXTURE_DB.exists():
    FIXTURE_DB.unlink()
shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)

database.DATABASE_PATH = FIXTURE_DB
vector_cache.CACHE_DIR = CACHE_FIXTURE
database.init_db()
database.insert_feedback(
    {
        "task": "定积分基础题练习",
        "estimated_minutes": 45,
        "actual_minutes": 30,
        "status": "partial",
        "reason": "任务难度太高",
        "completed_subtasks": 1,
        "total_subtasks": 3,
        "completed_questions": 2,
        "total_questions": 4,
    },
    "2026-10-05T09:00:00",
)

try:
    # 1. 白名单
    check(
        "白名单为两个只读工具 + 受确认保护的 save_plan",
        set(agent_dispatcher.TOOLS) == {"get_recent_feedback", "search_knowledge", "save_plan"},
        f"tools={sorted(agent_dispatcher.TOOLS)}",
    )
    check("save_plan 已加入白名单", "save_plan" in agent_dispatcher.TOOLS)
    for forbidden in ("insert_feedback", "delete_all_feedback", "init_db", "migrate_json_to_db",
                      "get_embedding", "get_embeddings", "compact_cache", "archive_cache",
                      "save_document", "call_ollama", "validate_plan_for_save"):
        check(f"白名单不含 {forbidden}", forbidden not in agent_dispatcher.TOOLS)

    # 2. 正确 Tool Name 可以调用
    outcome = agent_dispatcher.dispatch({"name": "get_recent_feedback", "arguments": {"days": 30}})
    check("get_recent_feedback 派发成功", outcome["ok"] is True, f"error={outcome['error']}")
    check(
        "返回 result 是 Tool 自己的结构",
        set(outcome["result"]) == {"days", "count", "summary", "feedback"},
        f"keys={sorted(outcome['result'])}",
    )

    outcome = agent_dispatcher.dispatch({"name": "search_knowledge", "arguments": {"query": "定积分怎么学"}})
    check("search_knowledge 派发成功", outcome["ok"] is True, f"error={outcome['error']}")
    check(
        "检索结果结构未破坏",
        set(outcome["result"]) == {"ok", "query", "count", "results", "error", "error_type"},
        f"keys={sorted(outcome['result'])}",
    )

    # 3. result 原样透传（与直接调用 agent_tools 一致）
    direct = agent_tools.get_recent_feedback(days=30)
    check("Tool Result 原样透传", outcome["ok"] is True and direct == agent_tools.get_recent_feedback(days=30))

    # 4. 未知 Tool 被拒绝（save_plan 已注册，不再属于未知工具）
    for unknown in ("insert_feedback", "delete_all_feedback", "get_embedding", "随便写的"):
        outcome = agent_dispatcher.dispatch({"name": unknown, "arguments": {}})
        check(
            f"未知工具被拒绝: {unknown}",
            outcome["ok"] is False and outcome["error_type"] == agent_dispatcher.ERROR_UNKNOWN_TOOL,
            f"error_type={outcome['error_type']}",
        )

    # 5. 参数非法被拒绝（Dispatcher 真正守门，不悄悄钳制）
    invalid_cases = [
        ("get_recent_feedback", {"days": 0}),
        ("get_recent_feedback", {"days": 31}),
        ("get_recent_feedback", {"days": "7"}),
        ("get_recent_feedback", {"days": True}),
        ("get_recent_feedback", {"days": 3.5}),
        ("get_recent_feedback", {"不存在的参数": 1}),
        ("search_knowledge", {}),
        ("search_knowledge", {"query": ""}),
        ("search_knowledge", {"query": "   "}),
        ("search_knowledge", {"query": "定" * (agent_tools.MAX_QUERY_CHARS + 1)}),
        ("search_knowledge", {"query": "定积分", "top_k": 0}),
        ("search_knowledge", {"query": "定积分", "top_k": 6}),
        ("search_knowledge", {"query": "定积分", "top_k": "3"}),
        ("search_knowledge", {"query": "定积分", "extra": 1}),
        ("search_knowledge", {"query": 123}),
    ]
    for tool_name, arguments in invalid_cases:
        outcome = agent_dispatcher.dispatch({"name": tool_name, "arguments": arguments})
        check(
            f"非法参数被拒绝: {tool_name} {arguments}",
            outcome["ok"] is False and outcome["error_type"] == agent_dispatcher.ERROR_INVALID_ARGUMENTS,
            f"error={outcome['error']}",
        )

    # 参数本身不是对象 / tool_call 不是对象
    check(
        "arguments 非对象被拒绝",
        agent_dispatcher.dispatch({"name": "search_knowledge", "arguments": "query=x"})["error_type"]
        == agent_dispatcher.ERROR_INVALID_ARGUMENTS,
    )
    check(
        "tool_call 非对象被拒绝",
        agent_dispatcher.dispatch("search_knowledge")["error_type"]
        == agent_dispatcher.ERROR_INVALID_ARGUMENTS,
    )
    check(
        "tool name 为空被拒绝",
        agent_dispatcher.dispatch({"name": "", "arguments": {}})["error_type"]
        == agent_dispatcher.ERROR_INVALID_ARGUMENTS,
    )

    # 6. Tool 抛异常不会崩
    original_tools = dict(agent_dispatcher.TOOLS)

    def exploding_tool(**kwargs):
        raise RuntimeError("模拟工具内部爆炸")

    agent_dispatcher.TOOLS["search_knowledge"] = exploding_tool
    try:
        outcome = agent_dispatcher.dispatch({"name": "search_knowledge", "arguments": {"query": "定积分"}})
    finally:
        agent_dispatcher.TOOLS.clear()
        agent_dispatcher.TOOLS.update(original_tools)

    check(
        "Tool 异常被捕获为结构化错误",
        outcome["ok"] is False
        and outcome["error_type"] == agent_dispatcher.ERROR_TOOL_FAILED
        and "模拟工具内部爆炸" in outcome["error"],
        f"error={outcome['error']}",
    )
    check("异常后 Dispatcher 仍可用", agent_dispatcher.dispatch({"name": "get_recent_feedback", "arguments": {}})["ok"] is True)

    # 7. 返回结构稳定（成功/失败字段完全一致）
    expected_keys = {"ok", "tool", "result", "error", "error_type"}
    success = agent_dispatcher.dispatch({"name": "get_recent_feedback", "arguments": {}})
    invalid_save = agent_dispatcher.dispatch({"name": "save_plan", "arguments": {}})
    invalid = agent_dispatcher.dispatch({"name": "search_knowledge", "arguments": {}})
    check("成功路径结构稳定", set(success) == expected_keys)
    check("参数不足路径结构稳定", set(invalid_save) == expected_keys)
    check("非法参数路径结构稳定", set(invalid) == expected_keys)
    check(
        "成功时 error 为 None、result 非空",
        success["error"] is None and success["result"] is not None and success["tool"] == "get_recent_feedback",
    )
    check("失败时 result 为 None", invalid_save["result"] is None and invalid["result"] is None)

    # 8. 不允许动态执行方式
    dispatcher_source = (PROJECT_DIR / "agent_dispatcher.py").read_text(encoding="utf-8")
    dynamic_patterns = ("getattr(", "eval(", "exec(", "globals()", "__import__(", "setattr(")
    found = [pattern for pattern in dynamic_patterns if pattern in dispatcher_source]
    check("Dispatcher 不使用动态执行", found == [], f"found={found}")
    check("Dispatcher 使用显式白名单字典", re.search(r"^TOOLS = \{", dispatcher_source, re.M) is not None)

    # 9. Agent 不能写数据
    rows_before = database.get_feedback_list()
    knowledge_before = sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )
    agent_dispatcher.dispatch({"name": "get_recent_feedback", "arguments": {"days": 7}})
    agent_dispatcher.dispatch({"name": "search_knowledge", "arguments": {"query": "定积分"}})
    check("派发只读工具不修改数据库", database.get_feedback_list() == rows_before)
    check(
        "派发只读工具不修改知识库",
        sorted(
            (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
            for path in KNOWLEDGE_DIR.rglob("*.txt")
        ) == knowledge_before,
    )
finally:
    database.DATABASE_PATH = PROJECT_DIR / "feedback.db"
    vector_cache.CACHE_DIR = PROJECT_DIR / ".rag_cache"

    for _ in range(20):
        gc.collect()

        if not FIXTURE_DB.exists():
            break

        try:
            FIXTURE_DB.unlink()
        except PermissionError:
            time.sleep(0.2)

    shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)

check("测试夹具已清理", not FIXTURE_DB.exists() and not CACHE_FIXTURE.exists())
check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)
check(
    "knowledge/ 未被修改",
    sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    ) == KNOWLEDGE_SNAPSHOT,
)

print("\n全部用例通过")
