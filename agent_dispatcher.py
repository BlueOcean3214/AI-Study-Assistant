"""Tool Dispatcher：把 LLM 产生的 tool name + arguments 派发到白名单工具。

只做三件事：
    白名单查找 -> 参数守门 -> 执行并返回结构化结果

原则：
- 显式白名单：不使用 getattr / eval / globals / __import__ 这类动态执行方式
- Schema 负责告诉模型怎么调用，Dispatcher 负责真正拦截非法调用
- 白名单之外的名字一律拒绝（例如 insert_feedback、delete_all_feedback）
- save_plan 是唯一写入口，且没有任何"用户已确认"布尔参数——
  写权限由服务端确认状态决定，模型参数决定不了（见 confirmation_service）
- 不破坏 Tool 自身返回结构：成功时 result 原样透传
"""

from agent_schema import ARGUMENT_RULES, TOOL_NAMES
from agent_tools import get_recent_feedback, save_plan, search_knowledge


# 显式白名单：Agent 产生的 tool name 必须在这里
TOOLS = {
    "get_recent_feedback": get_recent_feedback,
    "search_knowledge": search_knowledge,
    "save_plan": save_plan,
}


ERROR_UNKNOWN_TOOL = "unknown_tool"
ERROR_INVALID_ARGUMENTS = "invalid_arguments"
ERROR_TOOL_FAILED = "tool_failed"


def _failed(tool_name, message, error_type):
    """统一的失败结构（与成功结构字段完全一致）。"""

    return {
        "ok": False,
        "tool": tool_name,
        "result": None,
        "error": message,
        "error_type": error_type,
    }


def _validate_arguments(tool_name, arguments):
    """守门：返回 (清理后的参数, 错误信息)。

    - arguments 必须是对象
    - 不允许出现规则之外的参数
    - 类型、必填、范围（含长度）都必须满足；不满足就直接拒绝，而不是悄悄钳制
    """

    rules = ARGUMENT_RULES[tool_name]

    if arguments is None:
        arguments = {}

    if not isinstance(arguments, dict):
        return None, "arguments 必须是对象"

    unknown = sorted(set(arguments) - set(rules))
    if unknown:
        return None, (
            f"不支持的参数：{', '.join(unknown)}；"
            f"允许的参数：{', '.join(sorted(rules))}"
        )

    cleaned = {}

    for name, rule in rules.items():
        if name not in arguments:
            if rule["required"]:
                return None, f"缺少必填参数：{name}"
            continue

        value = arguments[name]

        if rule["type"] == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f"{name} 必须是整数"

            if not rule["min"] <= value <= rule["max"]:
                return None, f"{name} 必须在 {rule['min']}-{rule['max']} 之间"

        elif rule["type"] == "string":
            if not isinstance(value, str):
                return None, f"{name} 必须是字符串"

            value = value.strip()

            if len(value) < rule["min_length"]:
                return None, f"{name} 不能为空"

            if len(value) > rule["max_length"]:
                return None, f"{name} 过长（最多 {rule['max_length']} 个字符）"

        elif rule["type"] == "object":
            if not isinstance(value, dict):
                return None, f"{name} 必须是对象"

        cleaned[name] = value

    return cleaned, None


def dispatch(tool_call):
    """执行一次工具调用。

    tool_call: {"name": "search_knowledge", "arguments": {"query": "...", "top_k": 3}}
    返回:      {"ok", "tool", "result", "error", "error_type}
    """

    if not isinstance(tool_call, dict):
        return _failed(None, "tool_call 必须是对象", ERROR_INVALID_ARGUMENTS)

    tool_name = tool_call.get("name")
    arguments = tool_call.get("arguments", {})

    if not isinstance(tool_name, str) or not tool_name.strip():
        return _failed(tool_name, "tool name 不能为空", ERROR_INVALID_ARGUMENTS)

    tool_name = tool_name.strip()

    if tool_name not in TOOLS:
        return _failed(
            tool_name,
            f"未注册的工具：{tool_name}；可用工具：{', '.join(TOOL_NAMES)}",
            ERROR_UNKNOWN_TOOL,
        )

    cleaned, error = _validate_arguments(tool_name, arguments)

    if error:
        return _failed(tool_name, error, ERROR_INVALID_ARGUMENTS)

    try:
        result = TOOLS[tool_name](**cleaned)
    except Exception as error:  # noqa: BLE001 - Tool 异常不能炸掉 Agent Loop
        return _failed(tool_name, f"{type(error).__name__}: {error}", ERROR_TOOL_FAILED)

    return {
        "ok": True,
        "tool": tool_name,
        "result": result,
        "error": None,
        "error_type": None,
    }
