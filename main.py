from datetime import datetime
from pathlib import Path
from typing import Any

import json

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

import ai_service
import agent_plan_draft
import confirmation_service
import context_builder
import document_service
import plan_service

from rag_service import retrieve_chunks

from ai_service import (
    analyze_feedback,
    call_ollama,
    generate_next_plan,
)

from database import (
    delete_all_feedback,
    get_feedback_list,
    init_db,
    insert_feedback,
    migrate_json_to_db,
)

from feedback_service import calculate_summary

app = FastAPI()

app.mount(
    "/app",
    StaticFiles(
        directory=str(Path(__file__).resolve().parent / "static"),
        html=True
    ),
    name="static",
)

init_db()
migrate_json_to_db()

# 注册模型用量记录（model_usage 表）：记录失败绝不影响任何请求
import model_usage
model_usage.install()


class Feedback(BaseModel):
    task: str = Field(min_length=1)

    estimated_minutes: int = Field(ge=0)

    actual_minutes: int = Field(ge=0)

    status: str

    reason: str | None = None

    completed_subtasks: int = Field(
        default=0,
        ge=0
    )

    total_subtasks: int = Field(
        default=0,
        ge=0
    )

    completed_questions: int = Field(
        default=0,
        ge=0
    )

    total_questions: int = Field(
        default=0,
        ge=0
    )

    @model_validator(mode="after")
    def check_progress_counts(self):

        if self.completed_subtasks > self.total_subtasks:
            raise ValueError(
                "completed_subtasks 不能大于 total_subtasks"
            )

        if self.completed_questions > self.total_questions:
            raise ValueError(
                "completed_questions 不能大于 total_questions"
            )

        return self


@app.get("/")
def home():
    return {
        "message": "AI Study Assistant"
    }


# ==========================
# 健康检查 / 就绪检查（轻量：不调用任何付费模型）
# ==========================


@app.get("/health")
def health():
    """存活检查：进程可响应即 200，附配置摘要（不含任何密钥）。"""

    import embedding_service
    import llm_client

    def describe(settings_getter):
        try:
            settings = settings_getter()
            return {"configured": True, "provider": settings.provider, "model": settings.model}
        except Exception as error:  # noqa: BLE001 - 健康检查必须永远返回 200 结构
            return {"configured": False, "error": str(error)}

    return {
        "status": "ok",
        "llm": describe(llm_client.get_settings),
        "embedding": describe(embedding_service.get_settings),
    }


@app.get("/ready")
def ready():
    """就绪检查：数据库可读 + 配置完整即就绪；不调用付费模型、不产生费用。"""

    import sqlite3

    import database as database_module
    import embedding_service
    import llm_client

    checks = {}

    try:
        conn = sqlite3.connect(database_module.DATABASE_PATH)
        try:
            conn.execute("SELECT 1").fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        checks["database"] = {"ready": True, "schema_version": version}
    except Exception as error:  # noqa: BLE001
        checks["database"] = {"ready": False, "error": f"{type(error).__name__}: {error}"}

    for name, getter in (("llm", llm_client.get_settings), ("embedding", embedding_service.get_settings)):
        try:
            settings = getter()
            checks[name] = {"ready": True, "provider": settings.provider}
        except Exception as error:  # noqa: BLE001
            checks[name] = {"ready": False, "error": f"{type(error).__name__}: {error}"}

    is_ready = all(item.get("ready") for item in checks.values())

    body = {"ready": is_ready, "checks": checks}

    if is_ready:
        return body

    return JSONResponse(status_code=503, content=body)


# ==========================
# Mini RAG 问答接口
# ==========================

def retrieve_for_ask(question):
    """问答检索入口：RAG_BACKEND=llamaindex 走 LlamaIndex 实验路径，默认自研主路径。

    RAG_RERANK_ENABLED=1 时对候选做第二阶段重排（失败按 RERANK_FALLBACK 回退并标注）。
    """

    import llamaindex_rag
    import rerank_service

    if llamaindex_rag.is_enabled():
        chunks = llamaindex_rag.retrieve_chunks(question)
    else:
        chunks = retrieve_chunks(question)

    if rerank_service.is_enabled() and chunks:
        outcome = rerank_service.rerank(question, chunks)
        chunks = outcome.get("results") or chunks

    return chunks


@app.get("/ask")
def ask(question):
    # 1. 检索知识库
    chunks = retrieve_for_ask(question)

    # 2. 组装上下文（P1-A：(source, content) 去重 + 字符预算 + 来源标签；
    #    与 /ask/stream 共用 context_builder，两条路径上下文完全一致）
    context, _context_info = context_builder.build_context(chunks)

    # 3. 构造 Prompt
    prompt = build_ask_prompt(question, context)

    # 4. 调用大模型

    content, error = call_ollama(prompt)

    # 5. 返回答案 + 来源

    return {
        "answer": content,
        "sources": chunks,
        "error": error
    }


def build_ask_prompt(question, context):
    """问答 Prompt（流式与非流式共用，保证两条路径行为一致）。

    文案集中在 prompt_registry（任务 ask_rag / ask_plain，支持版本切换）。
    """

    import prompt_registry

    if context:
        return prompt_registry.render("ask_rag", question=question, context=context)

    return prompt_registry.render("ask_plain", question=question)


def sse_event(name, payload):
    """SSE 事件帧（data 为 JSON 单行）。"""

    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/ask/stream")
def ask_stream(question):
    """流式问答（SSE）：先发 sources，再逐段发 delta，最后 done / error。

    客户端断开时上游生成器被关闭，模型流式连接随之释放。
    """

    import llm_client

    chunks = retrieve_for_ask(question)
    context, _context_info = context_builder.build_context(chunks)
    prompt = build_ask_prompt(question, context)

    def event_stream():
        yield sse_event("sources", {"sources": chunks})

        for event in llm_client.chat_stream([{"role": "user", "content": prompt}]):
            if event["type"] == "delta":
                yield sse_event("delta", {"text": event["text"]})
            elif event["type"] == "done":
                yield sse_event("done", {
                    "usage": event.get("usage"),
                    "model": event.get("model"),
                    "elapsed_seconds": event.get("elapsed_seconds"),
                })
            else:
                yield sse_event("error", {
                    "error": event.get("error"),
                    "error_type": event.get("error_type"),
                })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==========================
# Mini RAG 文档导入接口
# ==========================

# 单个上传文件最大 2 MB
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def upload_error(message):
    return JSONResponse(
        status_code=400,
        content={
            "message": message
        }
    )


@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    # 1. 只取文件名本身，防止客户端传入路径或 ../ 写到 knowledge 之外

    filename = Path(file.filename).name if file.filename else ""

    if not filename:
        return upload_error("文件名不能为空")

    if Path(filename).suffix.lower() != ".txt":
        return upload_error("目前只支持 .txt 文件")

    # 2. 读取 bytes，并限制大小

    contents = await file.read()

    if len(contents) > MAX_UPLOAD_BYTES:
        return upload_error("文件过大，当前最大支持 2 MB")

    # 3. 保存进知识库（编码识别、文件名校验、切片都在 document_service 里）

    try:

        source = document_service.save_document(
            filename,
            contents
        )

    except document_service.DocumentError as error:

        return upload_error(str(error))

    # 4. 复用 document_service 统计这篇文档切成了多少 chunk

    try:

        text = document_service.read_text(
            document_service.KNOWLEDGE_PATH / source
        )

        chunk_count = len(
            document_service.chunk_text(text, source)
        )

    except document_service.DocumentError:

        chunk_count = 0

    # 5. 返回结果；向量由 rag_service 在下次检索时按需生成

    return {
        "message": "文档上传成功",
        "source": source,
        "size_bytes": len(contents),
        "chunk_count": chunk_count
    }


@app.post("/feedback")
def feedback(data: Feedback):
    created_at = datetime.now().isoformat()

    insert_feedback(
        data.model_dump(),
        created_at
    )

    return {
        "message": "反馈保存成功",
        "feedback": {
            **data.model_dump(),
            "created_at": created_at
        }
    }


@app.get("/feedback")
def get_feedback():
    return {
        "feedback": get_feedback_list()
    }


@app.get("/analyze")
def analyze():
    feedback_list = get_feedback_list()

    if not feedback_list:
        return {
            "message": "还没有学习反馈数据"
        }

    summary = calculate_summary(feedback_list)

    analysis, error = analyze_feedback(
        feedback_list,
        summary
    )

    if error:
        return {
            "message": error
        }

    return {
        "analysis": analysis
    }


@app.get("/next-plan")
def next_plan():
    feedback_list = get_feedback_list()

    if not feedback_list:
        return {
            "message": "还没有学习反馈数据"
        }

    summary = calculate_summary(
        feedback_list
    )

    return generate_next_plan(
        feedback_list,
        summary
    )


@app.delete("/feedback")
def delete_feedback():
    delete_all_feedback()

    return {
        "message": "所有反馈数据已清空"
    }


# ==========================
# 学习计划持久化接口（第三阶段 A）
# 只增加能力，不改动任何已有 API
# ==========================

# 业务错误 -> HTTP 状态码（避免所有错误都返回 500）
PLAN_ERROR_STATUS = {
    plan_service.ERROR_INVALID_DATE: 400,
    plan_service.ERROR_VALIDATION: 400,
    plan_service.ERROR_CONFIRMATION_REQUIRED: 400,
    plan_service.ERROR_ACTIVE_PLAN_EXISTS: 409,
    plan_service.ERROR_HISTORY_UNAVAILABLE: 503,
    plan_service.ERROR_DATABASE: 500,
    # 确认凭证相关（第三阶段 C1）
    confirmation_service.ERROR_NOT_FOUND: 404,
    confirmation_service.ERROR_EXPIRED: 410,
    confirmation_service.ERROR_MISMATCH: 409,
    confirmation_service.ERROR_ALREADY_CONFIRMED: 409,
    confirmation_service.ERROR_ALREADY_USED: 409,
}


class PlanRequest(BaseModel):
    # 故意不做字段级强校验：计划内容统一交给 plan_service / validate_plan 判定，
    # 保证错误类型稳定（而不是 FastAPI 的 422 结构）。
    plan: Any = None

    plan_date: Any = None

    # 旧 API 兼容参数：只代表"用户本人通过 HTTP 直接提交"。
    # Agent 工具路径不经过这里，也没有任何模型参数可以注入这个标记。
    confirmed_by_user: Any = False

    # 新路径：携带服务端签发且已确认的凭证（第三阶段 C1）。
    # 二选一：提供 confirmation_id 时走确认凭证校验，否则走旧 confirmed_by_user。
    confirmation_id: Any = None


class ConfirmationRequest(BaseModel):
    plan: Any = None

    plan_date: Any = None


class ConfirmRequest(BaseModel):
    confirmation_id: Any = None


def plan_error_response(result):
    status_code = PLAN_ERROR_STATUS.get(
        result.get("error_type"),
        400
    )

    return JSONResponse(
        status_code=status_code,
        content=result
    )


@app.get("/plan")
def get_plan(date: str | None = None):
    # 不传 date 时默认查询今天（本地日期）
    target_date = date if date is not None else plan_service.today_string()

    result = plan_service.get_active_plan(target_date)

    if result.get("error_type"):
        return plan_error_response(result)

    return result


@app.post("/plan")
def create_plan(data: PlanRequest):
    # 二选一（核心要求：写库前必须存在服务端认可的确认）：
    # - confirmation_id：服务端签发且用户已确认的凭证（新路径）
    # - confirmed_by_user：旧 API 兼容参数，仅代表用户本人直连提交
    if data.confirmation_id:

        result = plan_service.save_plan_with_confirmation(
            data.plan,
            data.plan_date,
            data.confirmation_id
        )

    else:

        result = plan_service.save_plan(
            data.plan,
            data.plan_date,
            data.confirmed_by_user
        )

    if not result.get("saved"):
        return plan_error_response(result)

    return result


# ==========================
# 计划确认接口（第三阶段 C1）
# 流程：创建 pending -> 用户确认 -> 凭证可用于保存
# Agent 只能引用凭证，不能签发、确认或篡改绑定
# ==========================


@app.post("/plan/draft")
def plan_draft():
    """让只读 Agent 依据真实反馈/知识库生成今日计划草案（第三阶段 C2）。

    - 复用 agent_plan_draft.run_plan_draft：草案只返回给前端展示，**不保存**
    - 保存必须由用户在前端点击确认，走 /plan/confirmation -> /confirm -> /plan
    - 草案失败（plan_error）统一 503，不向浏览器泄漏内部细节
    """
    # PLAN_DRAFT_ENGINE=langgraph 时走 LangGraph 工作流（实验路径，默认 native）
    import os

    if (os.getenv("PLAN_DRAFT_ENGINE") or "native").strip().lower() == "langgraph":
        import langgraph_plan_workflow

        result = langgraph_plan_workflow.run_plan_draft_graph(
            "帮我根据最近的学习情况，制定今天的学习计划草案。",
        )
    else:
        result = agent_plan_draft.run_plan_draft(
            "帮我根据最近的学习情况，制定今天的学习计划草案。",
            verbose=False,
        )

    if result.get("type") == agent_plan_draft.TYPE_PLAN_DRAFT:
        return {
            "ok": True,
            "type": result["type"],
            "answer": result["answer"],
            "plan": result["plan"],
            "corrections_used": result.get("corrections_used"),
        }

    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "type": result.get("type"),
            "error_type": result.get("error_type"),
            "message": "暂时无法生成计划草案，请稍后重试",
        },
    )


@app.post("/plan/confirmation")
def create_plan_confirmation(data: ConfirmationRequest):
    """为一份计划创建待确认凭证（返回 confirmation_id 给前端展示确认按钮）。"""

    # 结构白名单 + 基础规则校验：不能确认一份注定无法保存的计划
    normalized, structure_error = plan_service.normalize_plan(data.plan)

    if structure_error:
        return plan_error_response({
            "saved": False,
            "error_type": plan_service.ERROR_VALIDATION,
            "message": structure_error,
        })

    first_pass = ai_service.validate_plan(normalized, False)

    if not first_pass["valid"]:
        return plan_error_response({
            "saved": False,
            "error_type": plan_service.ERROR_VALIDATION,
            "message": first_pass["reason"],
        })

    normalized_date, date_error = plan_service.normalize_plan_date(data.plan_date)

    if date_error:
        return plan_error_response({
            "saved": False,
            "error_type": plan_service.ERROR_INVALID_DATE,
            "message": date_error,
        })

    result = confirmation_service.create_pending_confirmation(normalized, normalized_date)

    if not result.get("ok"):
        return plan_error_response({"saved": False, **result})

    return result


@app.post("/plan/confirmation/confirm")
def confirm_plan(data: ConfirmRequest):
    """用户确认：把 pending 凭证标记为 confirmed（重复确认返回稳定 409）。"""

    result = confirmation_service.confirm_confirmation(data.confirmation_id)

    if not result.get("ok"):
        return plan_error_response({"saved": False, **result})

    return result


# ==========================
# LangChain 只读 Agent 实验路由（默认关闭）
# LANGCHAIN_EXPERIMENT_ENABLED=1 才启用；只读，不提供任何写操作
# ==========================


class LangChainAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@app.post("/experiment/langchain/ask")
def langchain_ask(data: LangChainAskRequest):
    import langchain_agent

    if not langchain_agent.is_experiment_enabled():
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "LangChain 实验入口未启用（设置 LANGCHAIN_EXPERIMENT_ENABLED=1 后重启）",
                "error_type": "experiment_disabled",
            },
        )

    result = langchain_agent.run_langchain_agent(data.question)

    return result


class LangChainAnalyzeRequest(BaseModel):
    feedback_list: list = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


@app.post("/experiment/langchain/analyze")
def langchain_analyze(data: LangChainAnalyzeRequest):
    """LangChain 组件版反馈分析（ChatPromptTemplate + ChatOpenAI；只读，默认关闭）。"""

    import langchain_service

    if not langchain_agent_enabled():
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "LangChain 实验入口未启用（设置 LANGCHAIN_EXPERIMENT_ENABLED=1 后重启）",
                "error_type": "experiment_disabled",
            },
        )

    answer, error = langchain_service.analyze_feedback_langchain(data.feedback_list, data.summary)

    if error:
        return JSONResponse(status_code=502, content={"ok": False, "error": error})

    return {"ok": True, "answer": answer, "engine": "langchain"}


def langchain_agent_enabled():
    import langchain_agent

    return langchain_agent.is_experiment_enabled()
