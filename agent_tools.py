"""Agent Tool 层：把现有项目能力包装成少量、稳定、受控的 Tool 接口。

本阶段只做 Tool Layer 基础建设，不实现 Agent Loop、不实现 Tool Calling、不用 LLM 决定调用。

对外只提供三个函数：
    get_recent_feedback(days=7)        只读：最近学习反馈 + 确定性统计
    search_knowledge(query, top_k=3)   只读：知识库语义检索（内部复用 rag_service）
    validate_plan_for_save(plan)       只读校验：计划保存前的确定性校验入口

设计原则：
- 薄包装：本层只做参数校验/钳制、结果清洗、错误语义统一，不重新实现数据库或检索逻辑
- 不暴露底层：不返回数据库 id、向量、缓存、维度、模型指纹，也不提供任何写入/删除入口
- 不调用 LLM：本层只提供事实，不生成学习内容
- 参数上限由程序固定，Agent 不能越过（例如不能调低 MIN_SCORE、不能决定是否放宽计划规则）

明确不暴露（见架构审计结论）：
    delete_all_feedback / insert_feedback / init_db / migrate_json_to_db
    embedding_service.get_embedding(s) / vector_cache.* / document_service.*
    call_ollama / generate_next_plan / analyze_feedback / rag_service 的私有函数
"""

import embedding_service
import rag_service
import ai_service

from database import get_feedback_list, get_feedback_since
from feedback_service import calculate_summary


# ---- 参数上限（Agent 不可越过） ----

DEFAULT_FEEDBACK_DAYS = 7
MAX_FEEDBACK_DAYS = 30
MAX_FEEDBACK_ITEMS = 20
MAX_TASK_CHARS = 60
MAX_REASON_CHARS = 40

DEFAULT_TOP_K = 3
MAX_TOP_K = 5
MAX_QUERY_CHARS = 200
MAX_RESULT_CHARS = 500

# ---- 错误类型（供未来的 Guardrail 层区分“参数错误”和“服务异常”） ----

ERROR_INVALID_ARGUMENT = "invalid_argument"
ERROR_EMBEDDING_UNAVAILABLE = "embedding_service_unavailable"
ERROR_UNEXPECTED = "unexpected_error"


def _clamp_days(days):
    """days 归一化到 1~30。

    规则（与 database.get_feedback_since 保持一致）：
    - 非整数/布尔 -> 非法，回退默认 7
    - 小于 1（0 或负数）-> 非法（不构成有意义的窗口），回退默认 7
    - 大于 30 -> 钳制到 30（调用方只是要得太多）
    """

    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        return DEFAULT_FEEDBACK_DAYS

    return min(days, MAX_FEEDBACK_DAYS)


def _truncate(value, max_chars):
    """截断过长文本（只处理 str；None 等原样返回）。"""

    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars]

    return value


def _sanitize_feedback(row):
    """清洗单条反馈：去掉数据库 id，限制文本长度。"""

    return {
        "task": _truncate(row.get("task"), MAX_TASK_CHARS),
        "status": row.get("status"),
        "reason": _truncate(row.get("reason"), MAX_REASON_CHARS),
        "estimated_minutes": row.get("estimated_minutes"),
        "actual_minutes": row.get("actual_minutes"),
        "completed_subtasks": row.get("completed_subtasks"),
        "total_subtasks": row.get("total_subtasks"),
        "completed_questions": row.get("completed_questions"),
        "total_questions": row.get("total_questions"),
        "created_at": row.get("created_at"),
    }


def _sanitize_chunk(chunk):
    """清洗单条检索结果：只保留 content / source / score。"""

    content = chunk.get("content")

    if isinstance(content, str) and len(content) > MAX_RESULT_CHARS:
        content = content[:MAX_RESULT_CHARS]

    return {
        "source": chunk.get("source"),
        "score": chunk.get("score"),
        "content": content,
    }


def _search_response(ok, query, results=None, error=None, error_type=None):
    """search_knowledge 的固定返回结构（三条路径字段完全一致）。"""

    results = results if results is not None else []

    return {
        "ok": ok,
        "query": query,
        "count": len(results),
        "results": results,
        "error": error,
        "error_type": error_type,
    }


def _embedding_service_available():
    """最小健康探测：只发一条极短文本。

    用途：rag_service.retrieve_chunks() 会把 EmbeddingError 吞掉并返回 []，
    所以 Tool 层必须能把“知识库里确实没有相关内容”和“embedding 服务不可用”分开。
    探测只在检索结果为空时发生，正常命中时不会增加任何额外请求。
    """

    try:
        embedding_service.get_embedding("健康检查")
        return True, None
    except embedding_service.EmbeddingError as error:
        return False, str(error)
    except Exception as error:  # noqa: BLE001 - 任何异常都视为服务不可用
        return False, f"{type(error).__name__}: {error}"


def get_recent_feedback(days=DEFAULT_FEEDBACK_DAYS):
    """Tool：读取最近 days 天的学习反馈，并附带确定性统计。

    - days 默认 7，最大 30；非法值回退 7（database 层还会再兜底一次）
    - 最多返回 20 条，按 created_at 倒序
    - 不返回数据库 id；task/reason 会做长度限制
    - 空数据正常返回全 0 摘要与空列表，不抛异常
    - 只读：不写数据库、不写文件、不调用 LLM
    """

    days = _clamp_days(days)

    rows = get_feedback_since(days=days, limit=MAX_FEEDBACK_ITEMS)

    summary = calculate_summary(rows)

    # 让 Agent 不必自己找“最近一次”，与 ai_service.generate_next_plan 的口径一致
    summary["latest_status"] = rows[0]["status"] if rows else None
    summary["latest_reason"] = rows[0].get("reason") if rows else None

    return {
        "days": days,
        "count": len(rows),
        "summary": summary,
        "feedback": [_sanitize_feedback(row) for row in rows],
    }


def search_knowledge(query, top_k=DEFAULT_TOP_K):
    """Tool：在知识库里做语义检索，返回 content / source / score。

    - query 必须是 1~200 字符的非空字符串，否则直接拒绝
    - top_k 默认 3，范围 1~5：大于 5 钳制到 5，小于 1 或非整数直接拒绝
    - MIN_SCORE 由程序固定（rag_service 内 0.5），Agent 无法调整
    - 不暴露向量、缓存、维度等实现细节
    - 返回值用 ok 区分三种情况：
        正常/无相关内容 -> ok=True（无结果时 count=0、results=[]）
        参数非法/服务异常 -> ok=False，并给出 error 与 error_type

    注意：本工具会间接让 rag_service 按需生成并缓存向量（缓存写入是既有行为），
    但不会修改知识库文件，也不会调用 LLM。
    """

    if not isinstance(query, str) or not query.strip():
        return _search_response(
            False, "", error="query 必须是非空字符串", error_type=ERROR_INVALID_ARGUMENT
        )

    query = query.strip()

    if len(query) > MAX_QUERY_CHARS:
        return _search_response(
            False,
            query[:MAX_QUERY_CHARS],
            error=f"query 过长（最多 {MAX_QUERY_CHARS} 个字符）",
            error_type=ERROR_INVALID_ARGUMENT,
        )

    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        return _search_response(
            False,
            query,
            error="top_k 必须是大于等于 1 的整数",
            error_type=ERROR_INVALID_ARGUMENT,
        )

    top_k = min(top_k, MAX_TOP_K)

    try:
        chunks = rag_service.retrieve_chunks(query, top_k=top_k)
    except Exception as error:  # noqa: BLE001 - Tool 边界不允许把异常泄漏给 Agent
        return _search_response(
            False,
            query,
            error=f"{type(error).__name__}: {error}",
            error_type=ERROR_UNEXPECTED,
        )

    results = [_sanitize_chunk(chunk) for chunk in chunks]

    if results:
        return _search_response(True, query, results)

    # 空结果：区分“真的没有相关内容”和“embedding 服务不可用”
    available, reason = _embedding_service_available()

    if not available:
        return _search_response(
            False,
            query,
            error=f"embedding service unavailable: {reason}",
            error_type=ERROR_EMBEDDING_UNAVAILABLE,
        )

    return _search_response(True, query, results)


def _derive_difficult_previous_task():
    """按 generate_next_plan 的同一规则，从最近一条反馈推导“上一个任务难度过高”。

    返回 (是否难度过高, 错误信息)。读不到反馈时返回错误，由调用方 fail-closed。
    """

    try:
        feedback_list = get_feedback_list()
    except Exception as error:  # noqa: BLE001 - 读不到就交给调用方判失败
        return False, f"{type(error).__name__}: {error}"

    if not feedback_list:
        return False, None

    latest = feedback_list[-1]

    difficult = (
        latest.get("status") == "partial"
        and latest.get("reason") == "任务难度太高"
    )

    return difficult, None


def validate_plan_for_save(plan):
    """计划保存前的确定性校验（未来 save_plan 复用的安全入口）。

    - 只接收 plan：**不接收 difficult_previous_task**，该条件由程序根据最近一条反馈
      推导（规则与 ai_service.generate_next_plan 一致），Agent 无法自称“难度不高”
      来绕过更严格的限制。
    - 纯校验：不写数据库、不写文件、不调用 LLM（只读一次最近反馈）。
    - 读不到最近反馈时 fail-closed（valid=False），避免在状态未知时放行。
    - 返回 {"valid": bool, "reason": str | None}。
    """

    difficult_previous_task, error = _derive_difficult_previous_task()

    if error:
        return {
            "valid": False,
            "reason": f"无法读取最近的学习反馈，暂不能校验计划：{error}",
        }

    try:
        validation = ai_service.validate_plan(plan, difficult_previous_task)
    except Exception as error:  # noqa: BLE001 - 校验器异常同样 fail-closed
        return {
            "valid": False,
            "reason": f"计划校验异常：{type(error).__name__}: {error}",
        }

    return {
        "valid": bool(validation.get("valid")),
        "reason": validation.get("reason"),
    }
