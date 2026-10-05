"""Agent Tool Schema：给 LLM 看的“工具说明书”。

只描述工具，不包含执行逻辑：
    执行在 agent_tools.py，派发在 agent_dispatcher.py。

当前开放的工具：
    get_recent_feedback          只读：学习反馈
    search_knowledge             只读：知识库检索
    save_plan                    写入：保存"用户已确认"的计划（凭证由服务端校验）

save_plan 的 Schema 里只有 plan / plan_date / confirmation_id 三个参数，
刻意不出现 confirmed_by_user：模型输出 true 不构成用户确认，
confirmation_id 只是"凭证引用"，服务端负责判断它是否真实存在且已确认。

validate_plan_for_save 仍不作为 LLM Tool：它是内部确定性校验能力，
开放它只会浪费一次 Tool Call。

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
    {
        "type": "function",
        "function": {
            "name": "save_plan",
            "description": (
                "保存一份已经由用户确认的学习计划。必须提供系统签发且用户已确认的 "
                "confirmation_id；没有有效确认时保存会被拒绝，此时应告知用户先在界面上确认计划"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "description": (
                            "要保存的计划对象，字段与规则和计划草案一致"
                            "（task、estimated_minutes、difficulty、question_count、"
                            "reason、completion_criteria、subtasks）"
                        ),
                    },
                    "plan_date": {
                        "type": "string",
                        "description": "计划日期，YYYY-MM-DD 格式",
                    },
                    "confirmation_id": {
                        "type": "string",
                        "description": (
                            "用户确认后由系统签发的确认凭证；自己编造的 id 无效"
                        ),
                    },
                },
                "required": ["plan", "plan_date", "confirmation_id"],
            },
        },
    },
]


# 开放给 LLM 的工具名（白名单的唯一来源）
TOOL_NAMES = ("get_recent_feedback", "search_knowledge", "save_plan")


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
    "save_plan": {
        "plan": {
            "type": "object",
            "required": True,
        },
        "plan_date": {
            "type": "string",
            "min_length": 1,
            "max_length": 10,
            "required": True,
        },
        "confirmation_id": {
            "type": "string",
            "min_length": 1,
            "max_length": 128,
            "required": True,
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
