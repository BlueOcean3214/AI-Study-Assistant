"""Agent Tool 层测试：直接运行 python test_agent_tools.py。

不导入 main.py —— 导入 main 会执行 init_db() / migrate_json_to_db()，产生数据库副作用，
这正是不把 calculate_summary 留在 main.py 的原因。

测试隔离：
- 反馈数据库 -> 临时 sqlite 夹具（patch database.DATABASE_PATH）
- 向量缓存   -> 临时目录（patch vector_cache.CACHE_DIR）
- 结束时会校验真实 feedback.db / knowledge/ / .rag_cache 完全没有被改动

覆盖：get_recent_feedback（11 项）、search_knowledge（12 项）、validate_plan_for_save（7 项）
以及架构约束（Tool 面为三个只读函数 + 受确认保护的 save_plan、不得暴露禁用函数、main 只做 import）。
"""

import gc
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import agent_tools
import database
import embedding_service
import rag_service
import vector_cache
from feedback_service import calculate_summary


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_agent_tools.db"
CACHE_FIXTURE = PROJECT_DIR / "_test_cache_agent_tools"

KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def now_minus(**kwargs):
    return (datetime.now() - timedelta(**kwargs)).isoformat()


def make_row(
    task="任务",
    status="completed",
    reason=None,
    estimated_minutes=60,
    actual_minutes=40,
    completed_subtasks=0,
    total_subtasks=0,
    completed_questions=0,
    total_questions=0,
):
    return {
        "task": task,
        "estimated_minutes": estimated_minutes,
        "actual_minutes": actual_minutes,
        "status": status,
        "reason": reason,
        "completed_subtasks": completed_subtasks,
        "total_subtasks": total_subtasks,
        "completed_questions": completed_questions,
        "total_questions": total_questions,
    }


def seed(rows):
    """清空夹具库并写入 (row, created_at) 列表。"""

    database.delete_all_feedback()

    for row, created_at in rows:
        database.insert_feedback(row, created_at)


def make_plan(**overrides):
    plan = {
        "task": "定积分基础巩固",
        "estimated_minutes": 45,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "最近反馈显示难度偏高，先巩固基础",
        "completion_criteria": "完成4道定积分基础题并订正",
        "subtasks": [
            {"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习牛顿-莱布尼茨公式"},
            {"title": "基础练习", "minutes": 20, "question_count": 2, "description": "完成2道基础题"},
            {"title": "错题整理", "minutes": 15, "question_count": 2, "description": "整理2道错题"},
        ],
    }
    plan.update(overrides)
    return plan


# ---- 快照：用于证明测试没有碰真实数据 ----
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

try:
    # ==========================
    # 1. get_recent_feedback
    # ==========================

    # 1.1 默认 7 天
    seed([
        (make_row(task="今天"), now_minus()),
        (make_row(task="三天前"), now_minus(days=3)),
        (make_row(task="十天前"), now_minus(days=10)),
    ])
    result = agent_tools.get_recent_feedback()
    check("默认 days=7", result["days"] == 7, f"days={result['days']}")
    check("默认只返回 7 天内数据", result["count"] == 2, f"count={result['count']}")

    # 1.2 days=1
    seed([
        (make_row(task="今天"), now_minus()),
        (make_row(task="两天前"), now_minus(days=2)),
    ])
    result = agent_tools.get_recent_feedback(days=1)
    check("days=1 生效", result["days"] == 1 and result["count"] == 1, f"count={result['count']}")

    # 1.3 days=30
    seed([
        (make_row(task="今天"), now_minus()),
        (make_row(task="二十天前"), now_minus(days=20)),
    ])
    result = agent_tools.get_recent_feedback(days=30)
    check("days=30 生效", result["days"] == 30 and result["count"] == 2, f"count={result['count']}")

    # 1.4 days>30 被钳制到 30
    seed([
        (make_row(task="今天"), now_minus()),
        (make_row(task="三十五天前"), now_minus(days=35)),
    ])
    result = agent_tools.get_recent_feedback(days=40)
    check(
        "days=40 被钳制到 30",
        result["days"] == 30 and result["count"] == 1,
        f"days={result['days']} count={result['count']}",
    )

    # 1.5 非法 days 回退 7
    seed([
        (make_row(task="今天"), now_minus()),
        (make_row(task="十天前"), now_minus(days=10)),
    ])
    for bad_days in ("7", None, True, 3.5, [], -5, 0):
        result = agent_tools.get_recent_feedback(days=bad_days)
        check(
            f"非法 days 回退 7: {bad_days!r}",
            result["days"] == 7 and result["count"] == 1,
            f"days={result['days']} count={result['count']}",
        )

    # 1.6 最多返回 20 条（最近 20 条）
    seed([
        (make_row(task=f"任务-{index}"), now_minus(minutes=index))
        for index in range(25)
    ])
    result = agent_tools.get_recent_feedback(days=7)
    tasks = [item["task"] for item in result["feedback"]]
    check("最多返回 20 条", result["count"] == 20, f"count={result['count']}")
    check("返回的是最近 20 条", "任务-0" in tasks and "任务-24" not in tasks)

    # 1.7 按 created_at 倒序（故意乱序插入）
    seed([
        (make_row(task="中间"), now_minus(hours=2)),
        (make_row(task="最早"), now_minus(hours=5)),
        (make_row(task="最新"), now_minus(hours=1)),
    ])
    result = agent_tools.get_recent_feedback()
    created = [item["created_at"] for item in result["feedback"]]
    check("按 created_at 倒序", created == sorted(created, reverse=True), f"created={created}")
    check("最新的一条排在最前", result["feedback"][0]["task"] == "最新")
    check("latest_status 取最新一条", result["summary"]["latest_status"] == "completed")

    # 1.8 空数据正常返回
    seed([])
    result = agent_tools.get_recent_feedback()
    check("空数据 count=0", result["count"] == 0 and result["feedback"] == [])
    check(
        "空数据 summary 全 0",
        result["summary"] == {
            "total_count": 0,
            "completed_count": 0,
            "partial_count": 0,
            "not_completed_count": 0,
            "too_difficult_count": 0,
            "average_estimated_minutes": 0,
            "average_actual_minutes": 0,
            "latest_status": None,
            "latest_reason": None,
        },
        f"summary={result['summary']}",
    )

    # 1.9 不包含数据库 id
    seed([(make_row(task="任务A"), now_minus())])
    result = agent_tools.get_recent_feedback()
    item = result["feedback"][0]
    check("不返回 id", "id" not in item)
    check(
        "单条反馈字段固定",
        set(item) == {
            "task", "status", "reason", "estimated_minutes", "actual_minutes",
            "completed_subtasks", "total_subtasks", "completed_questions",
            "total_questions", "created_at",
        },
        f"keys={sorted(item)}",
    )

    # 1.10 summary 正确
    seed([
        (make_row(task="未完成", status="not_completed", estimated_minutes=30, actual_minutes=0),
         now_minus(hours=3)),
        (make_row(task="已完成", status="completed", estimated_minutes=45, actual_minutes=35),
         now_minus(hours=2)),
        (make_row(task="难度过高", status="partial", reason="任务难度太高",
                  estimated_minutes=60, actual_minutes=40), now_minus(hours=1)),
    ])
    result = agent_tools.get_recent_feedback()
    check(
        "summary 计数与均值正确",
        result["summary"] == {
            "total_count": 3,
            "completed_count": 1,
            "partial_count": 1,
            "not_completed_count": 1,
            "too_difficult_count": 1,
            "average_estimated_minutes": 45.0,
            "average_actual_minutes": 25.0,
            "latest_status": "partial",
            "latest_reason": "任务难度太高",
        },
        f"summary={result['summary']}",
    )

    # 1.11 不修改数据库
    before_rows = database.get_feedback_list()
    agent_tools.get_recent_feedback(days=30)
    agent_tools.get_recent_feedback(days=1)
    after_rows = database.get_feedback_list()
    check("读取反馈不修改数据库", before_rows == after_rows, f"rows={len(after_rows)}")

    # 1.12 超长 task/reason 被截断
    seed([(make_row(task="长" * 200, reason="难" * 200, status="partial"), now_minus())])
    item = agent_tools.get_recent_feedback()["feedback"][0]
    check("task 长度受限", len(item["task"]) == agent_tools.MAX_TASK_CHARS, f"len={len(item['task'])}")
    check("reason 长度受限", len(item["reason"]) == agent_tools.MAX_REASON_CHARS, f"len={len(item['reason'])}")

    # ==========================
    # 2. search_knowledge
    # ==========================

    try:
        embedding_service.get_embedding("前置检查")
    except embedding_service.EmbeddingError as error:
        raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

    # 2.1 正常 query
    result = agent_tools.search_knowledge("定积分怎么学")
    check("正常检索 ok=True", result["ok"] is True, f"error={result['error']}")
    check("正常检索有结果", result["count"] > 0, f"count={result['count']}")
    check(
        "结果字段只有 content/source/score",
        all(set(item) == {"source", "score", "content"} for item in result["results"]),
    )
    check(
        "结果字段类型正确",
        all(
            isinstance(item["content"], str) and item["content"]
            and isinstance(item["source"], str) and item["source"]
            and isinstance(item["score"], float)
            for item in result["results"]
        ),
    )

    # 2.2 空 query
    for empty_query in ("", "   "):
        result = agent_tools.search_knowledge(empty_query)
        check(
            f"空 query 被拒绝: {empty_query!r}",
            result["ok"] is False and result["error_type"] == agent_tools.ERROR_INVALID_ARGUMENT,
            f"error_type={result['error_type']}",
        )
    result = agent_tools.search_knowledge(None)
    check("非字符串 query 被拒绝", result["ok"] is False)

    # 2.3 超长 query
    result = agent_tools.search_knowledge("定" * (agent_tools.MAX_QUERY_CHARS + 1))
    check(
        "超长 query 被拒绝",
        result["ok"] is False and result["error_type"] == agent_tools.ERROR_INVALID_ARGUMENT,
        f"error={result['error']}",
    )

    # 2.4 top_k 默认 3
    default_result = agent_tools.search_knowledge("定积分怎么学")
    explicit_three = agent_tools.search_knowledge("定积分怎么学", top_k=3)
    check("top_k 默认 3", default_result["results"] == explicit_three["results"])
    check("默认不超过 3 条", default_result["count"] <= 3, f"count={default_result['count']}")

    # 2.5 top_k=1
    result = agent_tools.search_knowledge("定积分怎么学", top_k=1)
    check("top_k=1 生效", 0 < result["count"] <= 1, f"count={result['count']}")

    # 2.6 top_k=5（且前 3 条与 top_k=3 一致）
    five = agent_tools.search_knowledge("定积分怎么学", top_k=5)
    check("top_k=5 生效", five["count"] <= 5, f"count={five['count']}")
    check(
        "top_k=5 的前几条与 top_k=3 一致",
        five["results"][:len(explicit_three["results"])] == explicit_three["results"],
    )

    # 2.7 top_k>5 被钳制
    clamped = agent_tools.search_knowledge("定积分怎么学", top_k=99)
    check("top_k=99 被钳制到 5", clamped["results"] == five["results"], f"count={clamped['count']}")

    # 2.8 0 / 负数 / 非整数被拒绝
    for bad_top_k in (0, -3, "3", None, True, 2.5):
        result = agent_tools.search_knowledge("定积分怎么学", top_k=bad_top_k)
        check(
            f"非法 top_k 被拒绝: {bad_top_k!r}",
            result["ok"] is False and result["error_type"] == agent_tools.ERROR_INVALID_ARGUMENT,
            f"ok={result['ok']} error_type={result['error_type']}",
        )

    # 2.9 正常无结果（知识库里没有相关内容）
    empty_hit = agent_tools.search_knowledge("今天天气怎么样")
    check(
        "无相关内容: ok=True 且 count=0",
        empty_hit["ok"] is True and empty_hit["count"] == 0
        and empty_hit["results"] == [] and empty_hit["error"] is None,
        f"ok={empty_hit['ok']} count={empty_hit['count']}",
    )

    # 2.10 / 2.11 模拟 embedding 服务异常
    original_module_embedding = embedding_service.get_embedding
    original_rag_embedding = rag_service.get_embedding
    original_rag_embeddings = rag_service.get_embeddings

    def failing_embedding(text):
        raise embedding_service.EmbeddingError("模拟 embedding 服务不可用")

    def failing_embeddings(texts):
        raise embedding_service.EmbeddingError("模拟 embedding 服务不可用")

    embedding_service.get_embedding = failing_embedding
    rag_service.get_embedding = failing_embedding
    rag_service.get_embeddings = failing_embeddings
    try:
        broken = agent_tools.search_knowledge("定积分怎么学")
    finally:
        embedding_service.get_embedding = original_module_embedding
        rag_service.get_embedding = original_rag_embedding
        rag_service.get_embeddings = original_rag_embeddings

    check("服务异常: ok=False", broken["ok"] is False, f"ok={broken['ok']}")
    check(
        "服务异常: error_type 明确",
        broken["error_type"] == agent_tools.ERROR_EMBEDDING_UNAVAILABLE,
        f"error_type={broken['error_type']}",
    )
    check("服务异常: count=0 且 results 为空", broken["count"] == 0 and broken["results"] == [])
    check(
        "服务异常不会被伪装成无结果",
        empty_hit["ok"] is True and broken["ok"] is False and empty_hit["error_type"] != broken["error_type"],
    )

    # 2.12 返回结构稳定
    expected_keys = {"ok", "query", "count", "results", "error", "error_type"}
    check(
        "成功路径结构稳定",
        set(agent_tools.search_knowledge("定积分怎么学")) == expected_keys,
    )
    check("无结果路径结构稳定", set(empty_hit) == expected_keys)
    check("异常路径结构稳定", set(broken) == expected_keys)
    check(
        "参数错误路径结构稳定",
        set(agent_tools.search_knowledge("")) == expected_keys,
    )

    # ==========================
    # 3. validate_plan_for_save
    # ==========================

    # 3.1 合法 plan（数据库为空 -> 不触发难度收紧）
    seed([])
    check(
        "合法计划通过",
        agent_tools.validate_plan_for_save(make_plan()) == {"valid": True, "reason": None},
    )

    # 3.2 非法 difficulty
    result = agent_tools.validate_plan_for_save(make_plan(difficulty="extreme"))
    check("非法 difficulty 被拒绝", result["valid"] is False and "difficulty" in result["reason"], f"reason={result['reason']}")

    # 3.3 超长时间
    result = agent_tools.validate_plan_for_save(
        make_plan(estimated_minutes=90, subtasks=[
            {"title": "A", "minutes": 10, "question_count": 0, "description": "d"},
            {"title": "B", "minutes": 20, "question_count": 4, "description": "d"},
        ])
    )
    check("超过 60 分钟被拒绝", result["valid"] is False and "60" in result["reason"], f"reason={result['reason']}")

    # 3.4 subtask 数量错误
    for bad_subtasks in (
        [{"title": "A", "minutes": 10, "question_count": 4, "description": "d"}],
        [{"title": f"T{index}", "minutes": 5, "question_count": 0, "description": "d"} for index in range(5)],
    ):
        result = agent_tools.validate_plan_for_save(make_plan(subtasks=bad_subtasks))
        check(
            f"subtask 数量错误被拒绝: {len(bad_subtasks)} 个",
            result["valid"] is False and "subtask" in result["reason"],
            f"reason={result['reason']}",
        )

    # 3.5 question_count 与子任务不一致
    result = agent_tools.validate_plan_for_save(
        make_plan(question_count=6, completion_criteria="完成6道基础题并订正")
    )
    check(
        "question_count 不一致被拒绝",
        result["valid"] is False and "题目数量" in result["reason"],
        f"reason={result['reason']}",
    )

    # 3.6 难度过高场景（由程序从最近反馈推导，Agent 不能自己指定）
    seed([(make_row(task="难度过高", status="partial", reason="任务难度太高"), now_minus())])
    result = agent_tools.validate_plan_for_save(make_plan(difficulty="medium"))
    check(
        "难度过高时 medium 被拒绝",
        result["valid"] is False and "easy" in result["reason"],
        f"reason={result['reason']}",
    )
    check("难度过高时 easy 计划仍可通过", agent_tools.validate_plan_for_save(make_plan())["valid"] is True)
    result = agent_tools.validate_plan_for_save(
        make_plan(
            question_count=8,
            completion_criteria="完成8道基础题并订正",
            subtasks=[
                {"title": "A", "minutes": 10, "question_count": 0, "description": "d"},
                {"title": "B", "minutes": 15, "question_count": 4, "description": "d"},
                {"title": "C", "minutes": 15, "question_count": 4, "description": "d"},
            ],
        )
    )
    check(
        "难度过高时题量超 6 被拒绝",
        result["valid"] is False and "6" in result["reason"],
        f"reason={result['reason']}",
    )

    # 3.7 无副作用（读数据库但不改数据、不写文件）
    db_rows_before = database.get_feedback_list()
    knowledge_before = sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )
    first = agent_tools.validate_plan_for_save(make_plan())
    second = agent_tools.validate_plan_for_save(make_plan())
    db_rows_after = database.get_feedback_list()
    knowledge_after = sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )
    check("校验不修改数据库", db_rows_before == db_rows_after)
    check("校验不修改知识库文件", knowledge_before == knowledge_after)
    check("校验是确定性的", first == second)

    # ==========================
    # 4. 架构约束
    # ==========================

    main_source = (PROJECT_DIR / "main.py").read_text(encoding="utf-8")
    my_source = (PROJECT_DIR / "feedback_service.py").read_text(encoding="utf-8")

    check("main.py 改为 import calculate_summary", "from feedback_service import calculate_summary" in main_source)
    check("main.py 不再定义 calculate_summary", "def calculate_summary(" not in main_source)
    check("feedback_service 定义 calculate_summary", "def calculate_summary(" in my_source)

    # 只看真正的 import 语句行，避免被文档字符串里的字样误导
    import_lines = [
        line.strip()
        for line in my_source.splitlines()
        if re.match(r"\s*(?:import|from)\s", line)
    ]
    check(
        "feedback_service 不 import main",
        not any(re.match(r"(?:import|from)\s+main\b", line) for line in import_lines),
        f"imports={import_lines}",
    )
    check(
        "feedback_service 不依赖数据库/FastAPI/模型",
        all(
            token not in line
            for line in import_lines
            for token in ("sqlite3", "fastapi", "requests", "database", "ai_service", "rag_service")
        ),
        f"imports={import_lines}",
    )

    forbidden = {
        "delete_all_feedback", "insert_feedback", "init_db", "migrate_json_to_db",
        "call_ollama", "generate_next_plan", "analyze_feedback",
        "get_embedding", "get_embeddings", "chunk_hash", "read_vectors", "append_vectors",
        "compact_cache", "archive_cache", "save_document", "load_chunks", "load_documents",
        "retrieve_chunks", "validate_plan",
    }
    exposed = forbidden & set(vars(agent_tools))
    check("agent_tools 没有暴露禁用函数", exposed == set(), f"exposed={sorted(exposed)}")

    public_tools = {
        name
        for name, value in vars(agent_tools).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == agent_tools.__name__
    }
    check(
        "Tool 面为三个只读函数 + 受确认保护的 save_plan",
        public_tools == {
            "get_recent_feedback", "search_knowledge", "validate_plan_for_save", "save_plan",
        },
        f"public={sorted(public_tools)}",
    )

    # 既有 API 端点与返回结构未改动（本阶段只做了 calculate_summary 搬家）
    endpoints = (
        '@app.get("/")',
        '@app.get("/ask")',
        '@app.post("/upload")',
        '@app.post("/feedback")',
        '@app.get("/feedback")',
        '@app.get("/analyze")',
        '@app.get("/next-plan")',
        '@app.delete("/feedback")',
    )
    missing_endpoints = [endpoint for endpoint in endpoints if endpoint not in main_source]
    check("既有 API 端点齐全", missing_endpoints == [], f"missing={missing_endpoints}")
    check(
        "/ask 返回结构未改动",
        '"answer": content' in main_source
        and '"sources": chunks' in main_source
        and '"error": error' in main_source,
    )
    check(
        "POST /feedback 返回结构未改动",
        '"message": "反馈保存成功"' in main_source and '"feedback": {' in main_source,
    )
    check(
        "/upload 返回结构未改动",
        '"message": "文档上传成功"' in main_source
        and '"size_bytes": len(contents)' in main_source
        and '"chunk_count": chunk_count' in main_source,
    )

    # calculate_summary 口径（移动前后必须一致）
    check(
        "空列表摘要全 0",
        calculate_summary([]) == {
            "total_count": 0,
            "completed_count": 0,
            "partial_count": 0,
            "not_completed_count": 0,
            "too_difficult_count": 0,
            "average_estimated_minutes": 0,
            "average_actual_minutes": 0,
        },
    )
    sample_rows = [
        make_row(status="completed", estimated_minutes=60, actual_minutes=40),
        make_row(status="partial", reason="任务难度太高", estimated_minutes=45, actual_minutes=35),
        make_row(status="not_completed", estimated_minutes=30, actual_minutes=0),
    ]
    check(
        "统计口径与移动前一致",
        calculate_summary(sample_rows) == {
            "total_count": 3,
            "completed_count": 1,
            "partial_count": 1,
            "not_completed_count": 1,
            "too_difficult_count": 1,
            "average_estimated_minutes": 45.0,
            "average_actual_minutes": 25.0,
        },
    )
finally:
    database.DATABASE_PATH = PROJECT_DIR / "feedback.db"
    vector_cache.CACHE_DIR = PROJECT_DIR / ".rag_cache"
    rag_service.reset_cache()

    # sqlite 连接是既有代码里靠 GC 释放的（with sqlite3.connect 不会自动 close），
    # 所以这里先回收再删，避免 Windows 上文件仍被占用。
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
check(
    "真实 feedback.db 未被修改",
    (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES,
)
check(
    "knowledge/ 未被修改",
    sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    ) == KNOWLEDGE_SNAPSHOT,
)
check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)

print("\n全部用例通过")
