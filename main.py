from datetime import datetime
from pathlib import Path
from rag_service import retrieve_context
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from ai_service import analyze_feedback, call_ollama, generate_next_plan
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
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static"), html=True),
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
        "completed_count": sum(item["status"] == "completed" for item in feedback_list),
        "partial_count": sum(item["status"] == "partial" for item in feedback_list),
        "not_completed_count": sum(
            item["status"] == "not_completed" for item in feedback_list
        ),
        "too_difficult_count": sum(
            item.get("reason") == "任务难度太高" for item in feedback_list
        ),
        "average_estimated_minutes": round(
            sum(item["estimated_minutes"] for item in feedback_list) / total_count, 1
        ),
        "average_actual_minutes": round(
            sum(item["actual_minutes"] for item in feedback_list) / total_count, 1
        ),
    }


class Feedback(BaseModel):
    task: str = Field(min_length=1)
    estimated_minutes: int = Field(ge=0)
    actual_minutes: int = Field(ge=0)
    status: str
    reason: str | None = None
    # Defaults preserve compatibility with the current feedback page, which
    # does not yet submit these four optional progress counters.
    completed_subtasks: int = Field(default=0, ge=0)
    total_subtasks: int = Field(default=0, ge=0)
    completed_questions: int = Field(default=0, ge=0)
    total_questions: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def check_progress_counts(self):
        if self.completed_subtasks > self.total_subtasks:
            raise ValueError("completed_subtasks 不能大于 total_subtasks")
        if self.completed_questions > self.total_questions:
            raise ValueError("completed_questions 不能大于 total_questions")
        return self


@app.get("/")
def home():
    return {"message": "AI Study Assistant"}


@app.get("/ask")
def ask(question):

    context = retrieve_context(question)

    if context:

        prompt = f"""
你是一个学习助手。

请参考下面资料回答问题：

{context}


用户问题：
{question}

要求：
根据资料回答，如果资料没有相关内容，可以使用自己的知识补充。
"""

    else:

        prompt = question


    content, error = call_ollama(prompt)

    return content, error


@app.post("/feedback")
def feedback(data: Feedback):
    created_at = datetime.now().isoformat()
    insert_feedback(data.model_dump(), created_at)
    return {"message": "反馈保存成功", "feedback": {**data.model_dump(), "created_at": created_at}}


@app.get("/feedback")
def get_feedback():
    return {"feedback": get_feedback_list()}


@app.get("/analyze")
def analyze():
    feedback_list = get_feedback_list()
    if not feedback_list:
        return {"message": "还没有学习反馈数据"}

    summary = calculate_summary(feedback_list)
    analysis, error = analyze_feedback(feedback_list, summary)
    if error:
        return {"message": error}
    return {"analysis": analysis}


@app.get("/next-plan")
def next_plan():
    feedback_list = get_feedback_list()
    if not feedback_list:
        return {"message": "还没有学习反馈数据"}

    summary = calculate_summary(feedback_list)
    return generate_next_plan(feedback_list, summary)


@app.delete("/feedback")
def delete_feedback():
    delete_all_feedback()
    return {"message": "所有反馈数据已清空"}
