"""Plan 业务层：计划校验、困难状态推导、人工确认、当天 active 唯一、事务写入。

设计原则：
- main.py 不直接堆 SQL，所有计划业务规则集中在这里
- 校验复用 ai_service.validate_plan()，不复制第二套规则
- difficult_previous_task 由服务端根据最近真实反馈推导，调用方无法覆盖
- 读不到真实历史时 fail-closed：宁可拒绝保存，也不自动放宽
- 写库必须有服务端认可的确认（两条路径，见下）
- 同一天最多一条 active plan，由 database.insert_plan 在同一事务内保证

两条保存路径：
- save_plan(plan, plan_date, confirmed_by_user)：
  旧 API（POST /plan 直连）兼容路径。confirmed_by_user 只是旧接口的
  兼容参数，代表"用户本人通过 HTTP 直接提交"，Agent 路径不走这里。
- save_plan_with_confirmation(plan, plan_date, confirmation_id)：
  新路径（save_plan Tool / 确认 API）。必须持有 confirmation_service
  签发且已被用户确认、未过期、与 plan+plan_date 绑定匹配的凭证；
  保存成功后凭证被消费（一次性）。Agent 无法凭参数伪造确认。
"""

import json
from datetime import datetime

import ai_service
import confirmation_service
from database import (
    ActivePlanExistsError,
    get_active_plan_row,
    get_feedback_list,
    insert_plan,
)


PLAN_DATE_FORMAT = "%Y-%m-%d"

# 计划字段白名单（与 ai_service.validate_plan 的 REQUIRED_PLAN_FIELDS 一致）
PLAN_FIELDS = (
    "task",
    "estimated_minutes",
    "difficulty",
    "question_count",
    "reason",
    "completion_criteria",
    "subtasks",
)

# 稳定的错误类型
ERROR_INVALID_DATE = "invalid_date"
ERROR_VALIDATION = "validation_error"
ERROR_CONFIRMATION_REQUIRED = "confirmation_required"
ERROR_ACTIVE_PLAN_EXISTS = "active_plan_exists"
ERROR_HISTORY_UNAVAILABLE = "history_unavailable"
ERROR_DATABASE = "database_error"

# 确认凭证相关错误（由 confirmation_service 产生，这里转成 _failure 结构）
CONFIRMATION_ERRORS = (
    confirmation_service.ERROR_REQUIRED,
    confirmation_service.ERROR_NOT_FOUND,
    confirmation_service.ERROR_EXPIRED,
    confirmation_service.ERROR_MISMATCH,
    confirmation_service.ERROR_ALREADY_CONFIRMED,
    confirmation_service.ERROR_ALREADY_USED,
)


def today_string():
    """服务器本地日期（沿用项目现有的本地无时区时间口径）。"""

    return datetime.now().strftime(PLAN_DATE_FORMAT)


def normalize_plan_date(plan_date):
    """严格校验 YYYY-MM-DD，返回 (规范化日期, 错误信息)。不静默修正。"""

    if not isinstance(plan_date, str):
        return None, "plan_date 必须是 YYYY-MM-DD 字符串"

    value = plan_date.strip()

    try:
        parsed = datetime.strptime(value, PLAN_DATE_FORMAT)
    except ValueError:
        return None, f"plan_date 格式必须是 YYYY-MM-DD，收到：{plan_date!r}"

    # strptime 接受 2026-1-5 这类写法，这里要求原样回写以拒绝非零填充
    if parsed.strftime(PLAN_DATE_FORMAT) != value:
        return None, f"plan_date 格式必须是 YYYY-MM-DD，收到：{plan_date!r}"

    return value, None


def normalize_plan(plan):
    """结构白名单：只接受约定的 7 个字段，返回 (规范化计划, 错误信息)。"""

    if not isinstance(plan, dict):
        return None, "plan 必须是 JSON 对象"

    unknown = sorted(set(plan) - set(PLAN_FIELDS))

    if unknown:
        return None, "plan 包含不支持的字段：" + ", ".join(unknown)

    missing = [field for field in PLAN_FIELDS if field not in plan]

    if missing:
        return None, "plan 缺少字段：" + ", ".join(missing)

    return {field: plan[field] for field in PLAN_FIELDS}, None


def derive_difficult_previous_task():
    """从最近一条真实反馈推导“上一个任务难度过高”。

    规则与 ai_service.generate_next_plan 完全一致：
        latest.status == "partial" 且 latest.reason == "任务难度太高"

    返回 (是否困难, 错误信息)。读不到历史时返回错误，调用方必须 fail-closed。
    """

    try:
        feedback_list = get_feedback_list()
    except Exception as error:  # noqa: BLE001 - 交给调用方拒绝保存
        return False, f"{type(error).__name__}: {error}"

    if not feedback_list:
        return False, None

    latest = feedback_list[-1]

    difficult = (
        latest.get("status") == "partial"
        and latest.get("reason") == "任务难度太高"
    )

    return difficult, None


def _failure(error_type, message):
    return {
        "saved": False,
        "error_type": error_type,
        "message": message,
    }


def plan_payload(plan):
    """返回给调用方的计划结构（不含任何 SQLite 行字段）。"""

    return {
        "task": plan["task"],
        "estimated_minutes": plan["estimated_minutes"],
        "difficulty": plan["difficulty"],
        "question_count": plan["question_count"],
        "reason": plan["reason"],
        "completion_criteria": plan["completion_criteria"],
        "subtasks": plan["subtasks"],
    }


def _save_checked_plan(normalized, plan_date):
    """规范化之后的统一校验与写入（两条保存路径共用，顺序不可调整）。

    执行顺序：
        validate_plan（基础规则）-> 程序推导 difficult_previous_task
        -> 用真实困难状态再次校验（收紧规则）-> 日期
        -> 事务写入（事务内检查当天 active plan）

    调用方必须先完成 normalize_plan 和各自的确认检查。
    """

    # 1) 先用统一规则校验一次（无论调用方是否声称已经 validated）
    first_pass = ai_service.validate_plan(normalized, False)

    if not first_pass["valid"]:
        return _failure(ERROR_VALIDATION, first_pass["reason"])

    # 2) 困难状态由服务端根据真实反馈推导（Agent 不可指定）
    difficult, history_error = derive_difficult_previous_task()

    if history_error:
        return _failure(
            ERROR_HISTORY_UNAVAILABLE,
            f"无法读取最近反馈，已拒绝保存：{history_error}",
        )

    # 3) 用真实困难状态再次校验（收紧规则）
    second_pass = ai_service.validate_plan(normalized, difficult)

    if not second_pass["valid"]:
        return _failure(ERROR_VALIDATION, second_pass["reason"])

    # 4) 日期
    normalized_date, date_error = normalize_plan_date(plan_date)

    if date_error:
        return _failure(ERROR_INVALID_DATE, date_error)

    # 5) 事务写入（当天 active 唯一由 database 层在事务内保证）
    created_at = datetime.now().isoformat()

    try:
        plan_id = insert_plan(
            normalized_date,
            {
                "task": normalized["task"],
                "estimated_minutes": normalized["estimated_minutes"],
                "difficulty": normalized["difficulty"],
                "question_count": normalized["question_count"],
                "reason": normalized["reason"],
                "completion_criteria": normalized["completion_criteria"],
                "subtasks_json": json.dumps(normalized["subtasks"], ensure_ascii=False),
            },
            created_at,
        )
    except ActivePlanExistsError:
        return _failure(ERROR_ACTIVE_PLAN_EXISTS, "当天已经存在有效计划")
    except Exception as error:  # noqa: BLE001 - 统一转成稳定的 database_error
        return _failure(ERROR_DATABASE, f"保存计划失败：{type(error).__name__}")

    return {
        "saved": True,
        "plan_id": plan_id,
        "plan_date": normalized_date,
        "plan": plan_payload(normalized),
    }


def save_plan(plan, plan_date, confirmed_by_user):
    """保存当天的 active 学习计划（旧 API 兼容路径，POST /plan 使用）。

    confirmed_by_user 只是旧接口的兼容参数：它代表"用户本人通过 HTTP
    直接提交"这一事实，Agent 工具路径不经过本函数，也没有任何参数可以
    注入这个标记（见 save_plan_with_confirmation）。

    执行顺序（每一步都不能跳过）：
        结构白名单 -> confirmed_by_user -> validate_plan
        -> 程序推导 difficult_previous_task -> 用真实困难状态再次校验
        -> 日期 -> 事务写入（事务内检查当天 active plan）

    返回：
        成功 {"saved": True, "plan_id", "plan_date", "plan"}
        失败 {"saved": False, "error_type", "message"}
    """

    # 1) 结构白名单
    normalized, structure_error = normalize_plan(plan)

    if structure_error:
        return _failure(ERROR_VALIDATION, structure_error)

    # 2) 人工确认：必须是严格的 True（旧 API 兼容语义）
    if confirmed_by_user is not True:
        return _failure(ERROR_CONFIRMATION_REQUIRED, "用户确认后才能保存计划")

    # 3) 统一校验与写入
    return _save_checked_plan(normalized, plan_date)


def save_plan_with_confirmation(plan, plan_date, confirmation_id):
    """保存持有有效确认凭证的学习计划（save_plan Tool / 确认 API 路径）。

    确认检查完全交给 confirmation_service（服务端签发的状态），
    本函数不接受任何"用户已确认"布尔值——模型输出的 true 不构成确认。

    执行顺序：
        结构白名单 -> 凭证校验（存在/未过期/已确认/绑定匹配）
        -> 统一校验与写入 -> 成功后消费凭证（一次性）

    返回：
        成功 {"saved": True, "plan_id", "plan_date", "plan"}
        失败 {"saved": False, "error_type", "message"}
    """

    # 1) 结构白名单（先规范化，绑定哈希才稳定）
    normalized, structure_error = normalize_plan(plan)

    if structure_error:
        return _failure(ERROR_VALIDATION, structure_error)

    # 2) 凭证校验：服务端查真实确认状态 + plan/plan_date 绑定匹配
    check = confirmation_service.validate_for_save(confirmation_id, normalized, plan_date)

    if not check["ok"]:
        return _failure(check["error_type"], check["message"])

    # 3) 统一校验与写入
    result = _save_checked_plan(normalized, plan_date)

    # 4) 只有真正写库成功才消费凭证；校验失败保留凭证（用户可修正后重试）
    if result.get("saved"):
        confirmation_service.mark_consumed(check["confirmation_id"])

    return result


def get_active_plan(plan_date):
    """获取指定日期的 active plan。

    返回：
        存在   {"exists": True, "plan": {...}}
        不存在 {"exists": False, "plan": None}
        出错   {"exists": False, "plan": None, "error_type", "message"}
    """

    normalized_date, date_error = normalize_plan_date(plan_date)

    if date_error:
        return {
            "exists": False,
            "plan": None,
            "error_type": ERROR_INVALID_DATE,
            "message": date_error,
        }

    try:
        row = get_active_plan_row(normalized_date)
    except Exception as error:  # noqa: BLE001
        return {
            "exists": False,
            "plan": None,
            "error_type": ERROR_DATABASE,
            "message": f"读取计划失败：{type(error).__name__}",
        }

    if row is None:
        return {"exists": False, "plan": None}

    try:
        subtasks = json.loads(row["subtasks_json"] or "[]")
    except ValueError:
        subtasks = []

    return {
        "exists": True,
        "plan": {
            "plan_id": row["id"],
            "plan_date": row["plan_date"],
            "task": row["task"],
            "estimated_minutes": row["estimated_minutes"],
            "difficulty": row["difficulty"],
            "question_count": row["question_count"],
            "reason": row["reason"],
            "completion_criteria": row["completion_criteria"],
            "subtasks": subtasks,
            "status": row["status"],
            "created_at": row["created_at"],
        },
    }
