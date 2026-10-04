from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

import document_service

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



def calculate_summary(feedback_list):
    total_count = len(feedback_list)

    if total_count == 0:
        return {
            "total_count": 0,
            "completed_count": 0,
            "partial_count": 0,
            "not_completed_count": 0,
            "too_difficult_count": 0,
            "average_estimated_minutes": 0,
            "average_actual_minutes": 0,
        }


    return {
        "total_count": total_count,

        "completed_count": sum(
            item["status"] == "completed"
            for item in feedback_list
        ),

        "partial_count": sum(
            item["status"] == "partial"
            for item in feedback_list
        ),

        "not_completed_count": sum(
            item["status"] == "not_completed"
            for item in feedback_list
        ),

        "too_difficult_count": sum(
            item.get("reason") == "任务难度太高"
            for item in feedback_list
        ),

        "average_estimated_minutes": round(
            sum(
                item["estimated_minutes"]
                for item in feedback_list
            ) / total_count,
            1
        ),

        "average_actual_minutes": round(
            sum(
                item["actual_minutes"]
                for item in feedback_list
            ) / total_count,
            1
        ),
    }




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
# Mini RAG 问答接口
# ==========================

@app.get("/ask")
def ask(question):

    # 1. 检索知识库
    chunks = retrieve_chunks(question)


    # 2. 拼接上下文
    context = "\n".join(
        item["content"]
        for item in chunks
    )


    # 3. 构造 Prompt

    if context:

        prompt = f"""
你是一个学习助手。

请参考下面资料回答问题：

{context}


用户问题：

{question}


要求：

1. 优先根据资料回答。
2. 如果资料没有相关内容，可以使用自己的知识补充。
3. 不要编造资料不存在的信息。
"""

    else:

        prompt = question



    # 4. 调用大模型

    content, error = call_ollama(prompt)



    # 5. 返回答案 + 来源

    return {
        "answer": content,
        "sources": chunks,
        "error": error
    }





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