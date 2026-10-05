"""Agent Tool Schema：给 LLM 看的“工具说明书”。

只描述工具，不包含执行逻辑：
    执行在 agent_tools.py，派发在 agent_dispatcher.py。

当前只开放两个只读工具：
    get_recent_feedback
    search_knowledge

validate_plan_for_save 暂不作为 LLM Tool：它是内部确定性校验能力，
本阶段没有 save_plan，开放它只会浪费一次 Tool Call。

Schema 中不出现：embedding / vector / cache / MIN_SCORE / SQLite / 数据库路径 / 内部函数名。
参数范围直接引用 agent_tools 里的常量，避免说明书和真实实现脱节。
"""

from copy import deepcopy

from agent_tools import (
    DEFAULT_FEEDBACK_DAYS,
    DEFAULT_TOP_K,
    MAX_FEEDBACK_DAYS,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_recent_feedback",
            "description": "获取用户最近的学习反馈和统计信息（任务、状态、用时、题目进度）",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": (
                            f"查询最近多少天，范围 1-{MAX_FEEDBACK_DAYS}，"
                            f"默认 {DEFAULT_FEEDBACK_DAYS}"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "在学习知识库中搜索与问题相关的知识片段",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": f"知识库搜索问题，1-{MAX_QUERY_CHARS} 个字符",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": f"最多返回 1-{MAX_TOP_K} 个相关结果，默认 {DEFAULT_TOP_K}",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# 开放给 LLM 的工具名（白名单的唯一来源）
TOOL_NAMES = ("get_recent_feedback", "search_knowledge")


# Dispatcher 用的参数守门规则（与上面的 parameters 描述一一对应，
# 但这里是“真正拦截”的那一份）
ARGUMENT_RULES = {
    "get_recent_feedback": {
        "days": {
            "type": "integer",
            "min": 1,
            "max": MAX_FEEDBACK_DAYS,
            "required": False,
        },
    },
    "search_knowledge": {
        "query": {
            "type": "string",
            "min_length": 1,
            "max_length": MAX_QUERY_CHARS,
            "required": True,
        },
        "top_k": {
            "type": "integer",
            "min": 1,
            "max": MAX_TOP_K,
            "required": False,
        },
    },
}


def get_tool_schemas():
    """返回 Schema 的深拷贝，避免调用方改动模块级状态。"""

    return deepcopy(TOOL_SCHEMAS)


def describe_tools():
    """一行式工具清单，供系统 Prompt 与日志使用。"""

    return "\n".join(
        f"- {schema['function']['name']}: {schema['function']['description']}"
        for schema in TOOL_SCHEMAS
    )
