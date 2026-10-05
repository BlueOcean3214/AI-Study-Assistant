"""Confirmation Service：学习计划保存凭证的签发、确认、校验与消费。

安全模型（第三阶段 C1 的核心）：
- confirmation_id 由 secrets 模块生成（不可预测），Agent 无法凭空造出有效凭证
- 状态单向流转：pending -> confirmed -> consumed
- 绑定：签发时计算 canonical hash(plan + plan_date)，保存时必须原样匹配，
  Agent 换一份计划或换一个日期都会被拒绝（confirmation_mismatch）
- 过期：超过 TTL 的凭证不能确认、不能保存（confirmation_expired）
- 保存成功后凭证被消费，不能再次写库（confirmation_already_used）

实现方式：进程内内存存储（MVP 单进程部署），接口不依赖存储方式；
将来要跨进程/重启保留时可以换成 SQLite 表，函数签名不变。

重要：本模块不校验 plan 的业务规则（那是 plan_service/validate_plan 的职责），
也不 import plan_service（避免循环依赖）。调用方必须传入规范化后的 plan
（plan_service.normalize_plan 的返回值），绑定哈希按传入内容原样计算。
"""

import copy
import hashlib
import json
import secrets
import threading
import time
from datetime import datetime


# 默认有效期：30 分钟内确认有效（用户确认是一个即时动作，不需要太长）
DEFAULT_TTL_SECONDS = 30 * 60

# 稳定错误类型
ERROR_REQUIRED = "confirmation_required"
ERROR_NOT_FOUND = "confirmation_not_found"
ERROR_EXPIRED = "confirmation_expired"
ERROR_MISMATCH = "confirmation_mismatch"
ERROR_ALREADY_CONFIRMED = "confirmation_already_confirmed"
ERROR_ALREADY_USED = "confirmation_already_used"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"

# 确认凭证的输入上限（防止异常大的输入；plan 本身由 normalize_plan 白名单约束）
MAX_PLAN_DATE_CHARS = 10
MAX_CONFIRMATION_ID_CHARS = 128

_confirmations = {}
_lock = threading.Lock()


def canonical_plan_hash(plan, plan_date):
    """计算 plan + plan_date 的规范绑定哈希。

    - json.dumps(sort_keys=True)：字段顺序不影响哈希
    - ensure_ascii=False：中文原样参与哈希
    - 保存时用同一函数重算并比对，Agent 无法在不改变哈希的情况下替换计划
    """

    payload = json.dumps(
        {"plan": plan, "plan_date": plan_date},
        sort_keys=True,
        ensure_ascii=False,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _failure(error_type, message):
    return {"ok": False, "error_type": error_type, "message": message}


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _public_view(record):
    """返回给调用方的凭证视图（不含内部字段，plan/字典均为副本）。"""

    return {
        "confirmation_id": record["confirmation_id"],
        "plan": copy.deepcopy(record["plan"]),
        "plan_date": record["plan_date"],
        "status": STATUS_CONFIRMED if record["confirmed"] else STATUS_PENDING,
        "confirmed": record["confirmed"],
        "consumed": record["consumed"],
        "created_at": _iso(record["created_at"]),
        "expires_at": _iso(record["expires_at"]),
    }


def create_pending_confirmation(plan, plan_date, ttl_seconds=DEFAULT_TTL_SECONDS, now=None):
    """为一份规范化计划创建待确认凭证。

    - plan 必须是 dict（调用方负责先做 normalize_plan 业务白名单）
    - plan_date 必须是 YYYY-MM-DD 字符串（格式由调用方校验，这里做长度兜底）
    - ttl_seconds：有效期，测试可传 0 生成"立即过期"的凭证
    - now：可注入的当前时间戳（测试用）

    返回：
        成功 {"ok": True, "confirmation_id", "plan", "plan_date", "status",
              "created_at", "expires_at", "confirmed": False}
        失败 {"ok": False, "error_type", "message"}
    """

    if not isinstance(plan, dict) or not plan:
        return _failure(ERROR_MISMATCH, "plan 必须是非空对象")

    if not isinstance(plan_date, str) or not plan_date.strip():
        return _failure(ERROR_MISMATCH, "plan_date 必须是非空字符串")

    plan_date = plan_date.strip()

    if len(plan_date) > MAX_PLAN_DATE_CHARS:
        return _failure(ERROR_MISMATCH, f"plan_date 过长（最多 {MAX_PLAN_DATE_CHARS} 个字符）")

    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds < 0:
        return _failure(ERROR_MISMATCH, "ttl_seconds 必须是非负数")

    current = now if now is not None else time.time()
    confirmation_id = secrets.token_hex(16)

    record = {
        "confirmation_id": confirmation_id,
        "plan": copy.deepcopy(plan),
        "plan_date": plan_date,
        "plan_hash": canonical_plan_hash(plan, plan_date),
        "created_at": current,
        "expires_at": current + ttl_seconds,
        "confirmed": False,
        "consumed": False,
    }

    with _lock:
        _confirmations[confirmation_id] = record

    view = _public_view(record)

    return {
        "ok": True,
        "confirmation_id": confirmation_id,
        "plan": view["plan"],
        "plan_date": view["plan_date"],
        "status": view["status"],
        "confirmed": False,
        "created_at": view["created_at"],
        "expires_at": view["expires_at"],
    }


def get_confirmation(confirmation_id):
    """按 id 读取凭证（不存在返回 None）；返回副本，调用方无法改动内部状态。"""

    if not isinstance(confirmation_id, str):
        return None

    with _lock:
        record = _confirmations.get(confirmation_id)

    return _public_view(record) if record else None


def confirm_confirmation(confirmation_id, now=None):
    """用户确认：pending -> confirmed。

    - 不存在的 id -> confirmation_not_found
    - 已过期 -> confirmation_expired（过期不能确认）
    - 重复确认 -> confirmation_already_confirmed（稳定错误，不炸、不改变状态）
    """

    if not isinstance(confirmation_id, str) or not confirmation_id.strip():
        return _failure(ERROR_NOT_FOUND, "confirmation_id 不能为空")

    confirmation_id = confirmation_id.strip()
    current = now if now is not None else time.time()

    with _lock:
        record = _confirmations.get(confirmation_id)

        if record is None:
            return _failure(ERROR_NOT_FOUND, f"确认凭证不存在：{confirmation_id}")

        if current > record["expires_at"]:
            return _failure(ERROR_EXPIRED, "确认凭证已过期，请重新发起计划确认")

        if record["confirmed"]:
            return _failure(ERROR_ALREADY_CONFIRMED, "该计划已经确认过，无需重复确认")

        record["confirmed"] = True

    return {
        "ok": True,
        "confirmation_id": confirmation_id,
        "status": STATUS_CONFIRMED,
        "confirmed": True,
    }


def validate_for_save(confirmation_id, plan, plan_date, now=None):
    """保存前的凭证校验（save_plan Tool / plan_service 的唯一信任入口）。

    校验顺序（都通过才算有效）：
        存在 -> 未过期 -> 未消费 -> 已确认 -> plan+plan_date 绑定匹配

    返回：
        有效 {"ok": True, "confirmation_id"}
        无效 {"ok": False, "error_type", "message"}
    """

    if not isinstance(confirmation_id, str) or not confirmation_id.strip():
        return _failure(ERROR_REQUIRED, "缺少 confirmation_id：保存计划需要用户确认的凭证")

    confirmation_id = confirmation_id.strip()

    if len(confirmation_id) > MAX_CONFIRMATION_ID_CHARS:
        return _failure(ERROR_NOT_FOUND, "confirmation_id 无效")

    current = now if now is not None else time.time()

    with _lock:
        record = _confirmations.get(confirmation_id)

        if record is None:
            return _failure(ERROR_NOT_FOUND, f"确认凭证不存在：{confirmation_id}")

        if current > record["expires_at"]:
            return _failure(ERROR_EXPIRED, "确认凭证已过期，请重新发起计划确认")

        if record["consumed"]:
            return _failure(ERROR_ALREADY_USED, "该确认凭证已用于保存，不能重复使用")

        if not record["confirmed"]:
            return _failure(ERROR_REQUIRED, "用户尚未确认该计划，不能保存")

        if not isinstance(plan, dict):
            return _failure(ERROR_MISMATCH, "plan 必须是对象")

        if canonical_plan_hash(plan, plan_date) != record["plan_hash"]:
            return _failure(
                ERROR_MISMATCH,
                "计划内容与确认凭证不匹配：只能保存用户确认时的那份计划",
            )

    return {"ok": True, "confirmation_id": confirmation_id}


def mark_consumed(confirmation_id):
    """保存成功后消费凭证（一次性使用，防止同一确认写多条 plan）。幂等。"""

    if not isinstance(confirmation_id, str):
        return False

    with _lock:
        record = _confirmations.get(confirmation_id)

        if record is None:
            return False

        record["consumed"] = True

    return True


def confirmation_count():
    """当前存储的凭证数量（测试/观测用）。"""

    with _lock:
        return len(_confirmations)


def reset_confirmations():
    """清空全部凭证（仅测试与评测隔离使用）。"""

    with _lock:
        _confirmations.clear()
