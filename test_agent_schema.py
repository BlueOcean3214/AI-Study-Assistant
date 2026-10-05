"""Agent Tool Schema 测试：直接运行 python test_agent_schema.py。

Schema 是给 LLM 看的工具说明书，只描述、不执行。
本文件不调用模型、不读写任何业务数据。
"""

import json

import agent_schema
import agent_tools
from agent_dispatcher import TOOLS as DISPATCHER_TOOLS


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


schemas = agent_schema.get_tool_schemas()
by_name = {schema["function"]["name"]: schema for schema in schemas}

# 1. 工具名称正确
check("Schema 数量为 2", len(schemas) == 2, f"n={len(schemas)}")
check(
    "工具名称正确",
    set(by_name) == {"get_recent_feedback", "search_knowledge"},
    f"names={sorted(by_name)}",
)
check("TOOL_NAMES 与 Schema 一致", set(agent_schema.TOOL_NAMES) == set(by_name))
check("Schema 顶层是 function 类型", all(schema["type"] == "function" for schema in schemas))
check(
    "每个工具都有非空 description",
    all(schema["function"]["description"].strip() for schema in schemas),
)

# 2. 参数 schema 正确
feedback_params = by_name["get_recent_feedback"]["function"]["parameters"]
search_params = by_name["search_knowledge"]["function"]["parameters"]

check("get_recent_feedback 参数是 object", feedback_params["type"] == "object")
check(
    "get_recent_feedback 只有 days 参数",
    set(feedback_params["properties"]) == {"days"},
    f"properties={sorted(feedback_params['properties'])}",
)
check("days 声明为 integer", feedback_params["properties"]["days"]["type"] == "integer")
check("days 有说明", "1-30" in feedback_params["properties"]["days"]["description"])

check("search_knowledge 参数是 object", search_params["type"] == "object")
check(
    "search_knowledge 有 query/top_k",
    set(search_params["properties"]) == {"query", "top_k"},
    f"properties={sorted(search_params['properties'])}",
)
check("query 声明为 string", search_params["properties"]["query"]["type"] == "string")
check("top_k 声明为 integer", search_params["properties"]["top_k"]["type"] == "integer")

# 3. required 正确
check("get_recent_feedback 没有必填参数", "required" not in feedback_params or feedback_params["required"] == [])
check("search_knowledge 必填 query", search_params.get("required") == ["query"], f"required={search_params.get('required')}")

# 4. 只有白名单 Tool
check(
    "Schema 工具集合 = Dispatcher 白名单",
    set(by_name) == set(DISPATCHER_TOOLS),
    f"schema={sorted(by_name)} dispatcher={sorted(DISPATCHER_TOOLS)}",
)
check("validate_plan_for_save 未作为 LLM Tool", "validate_plan_for_save" not in by_name)
check("save_plan 未作为 LLM Tool", "save_plan" not in by_name)
check(
    "没有多余工具",
    set(agent_schema.TOOL_NAMES) == {"get_recent_feedback", "search_knowledge"},
)

# 5. Schema 不暴露内部细节
serialized = json.dumps(schemas, ensure_ascii=False).lower()
forbidden_tokens = (
    "embedding", "vector", "cache", "min_score", "sqlite", "feedback.db", ".rag_cache",
    "retrieve_chunks", "get_embedding", "vector_cache", "top_k=3 的默认检索算法",
    "ollama", "bge-m3", "database",
)
leaked = [token for token in forbidden_tokens if token in serialized]
check("Schema 不暴露内部细节", leaked == [], f"leaked={leaked}")

# 6. 范围与 agent_tools 的常量保持一致（防止说明书与实现脱节）
check(
    "days 上限与 agent_tools 一致",
    f"1-{agent_tools.MAX_FEEDBACK_DAYS}" in feedback_params["properties"]["days"]["description"],
    f"MAX_FEEDBACK_DAYS={agent_tools.MAX_FEEDBACK_DAYS}",
)
check(
    "top_k 上限与 agent_tools 一致",
    f"1-{agent_tools.MAX_TOP_K}" in search_params["properties"]["top_k"]["description"],
    f"MAX_TOP_K={agent_tools.MAX_TOP_K}",
)
check(
    "query 长度上限与 agent_tools 一致",
    str(agent_tools.MAX_QUERY_CHARS) in search_params["properties"]["query"]["description"],
    f"MAX_QUERY_CHARS={agent_tools.MAX_QUERY_CHARS}",
)

# 7. 守门规则与 Schema 对齐
check(
    "ARGUMENT_RULES 覆盖全部工具",
    set(agent_schema.ARGUMENT_RULES) == set(by_name),
    f"rules={sorted(agent_schema.ARGUMENT_RULES)}",
)
check(
    "days 守门范围 = 1..MAX_FEEDBACK_DAYS",
    agent_schema.ARGUMENT_RULES["get_recent_feedback"]["days"]["min"] == 1
    and agent_schema.ARGUMENT_RULES["get_recent_feedback"]["days"]["max"] == agent_tools.MAX_FEEDBACK_DAYS,
)
check(
    "top_k 守门范围 = 1..MAX_TOP_K",
    agent_schema.ARGUMENT_RULES["search_knowledge"]["top_k"]["min"] == 1
    and agent_schema.ARGUMENT_RULES["search_knowledge"]["top_k"]["max"] == agent_tools.MAX_TOP_K,
)
check(
    "query 必填且长度受限",
    agent_schema.ARGUMENT_RULES["search_knowledge"]["query"]["required"] is True
    and agent_schema.ARGUMENT_RULES["search_knowledge"]["query"]["max_length"] == agent_tools.MAX_QUERY_CHARS,
)

# 8. 返回的是深拷贝，调用方改不到模块状态
schemas[0]["function"]["name"] = "被改坏了"
check(
    "get_tool_schemas 返回深拷贝",
    agent_schema.TOOL_SCHEMAS[0]["function"]["name"] == "get_recent_feedback",
)

# 9. describe_tools 供系统 Prompt 使用
description = agent_schema.describe_tools()
check(
    "describe_tools 包含两个工具",
    "get_recent_feedback" in description and "search_knowledge" in description,
)

print("\n全部用例通过")
