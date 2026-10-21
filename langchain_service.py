"""LangChain 真实组件接入业务路径（实验）。

职责分工（与原生路径对照）：
- ChatPromptTemplate：反馈分析提示词的模板化构建（文案与 prompt_registry 一致）
- Chat Model（ChatOpenAI）：复用 llm_client 的云端配置（百炼），不重复硬编码密钥/模型/Base URL
- PydanticOutput：计划草案 JSON 的结构化解析（Pydantic Schema + 显式校验，
  不假设模型原生 JSON 模式能保证合法——最终仍以程序校验为准）

业务接入点：
- analyze_feedback_langchain(feedback_list, summary)：/experiment/langchain/analyze 路由使用
- parse_plan_draft(content)：草案结构化解析的 LangChain 对照实现（供评测/工具复用）

安全：只读分析/解析，无任何写操作；输出仍受业务层二次校验约束。
"""

import json

from pydantic import BaseModel, Field

import prompt_registry


class LangChainServiceError(RuntimeError):
    """LangChain 业务路径错误。"""


# 计划草案 Pydantic Schema（字段与 validate_plan 一致；解析合法 ≠ 业务合法，
# 业务规则仍由 validate_plan_for_save 二次校验）
class PlanSubtask(BaseModel):
    title: str = Field(min_length=1)
    minutes: int = Field(ge=0)
    question_count: int = Field(ge=0)
    description: str = Field(min_length=1)


class PlanSchema(BaseModel):
    task: str = Field(min_length=1)
    estimated_minutes: int = Field(ge=0, le=60)
    difficulty: str
    question_count: int = Field(ge=0)
    reason: str = Field(min_length=1)
    completion_criteria: str = Field(min_length=1)
    subtasks: list[PlanSubtask] = Field(min_length=2, max_length=4)


class PlanDraftSchema(BaseModel):
    answer: str = Field(min_length=1)
    plan: PlanSchema


def build_analyze_prompt(feedback_list, summary):
    """ChatPromptTemplate 构建反馈分析消息（文案与 prompt_registry 版本一致）。"""

    from langchain_core.prompts import ChatPromptTemplate

    template = prompt_registry.render("analyze_feedback", feedback_json="{feedback_json}", summary_json="{summary_json}")

    prompt = ChatPromptTemplate.from_template(template)

    return prompt.format_messages(
        feedback_json=json.dumps(feedback_list, ensure_ascii=False, indent=2),
        summary_json=json.dumps(summary, ensure_ascii=False, indent=2),
    )


def _chat_model(timeout=120):
    import langchain_agent

    return langchain_agent.build_chat_model(timeout=timeout)


def analyze_feedback_langchain(feedback_list, summary, model=None, timeout=120):
    """LangChain 版反馈分析：ChatPromptTemplate + ChatOpenAI（复用云端配置）。

    返回 (answer, error)，与 ai_service.analyze_feedback 的契约对齐。
    """

    try:
        messages = build_analyze_prompt(feedback_list, summary)
        chat_model = model if model is not None else _chat_model(timeout=timeout)
        response = chat_model.invoke(messages)
    except Exception as error:  # noqa: BLE001 - 稳定错误结构
        return None, f"LangChain 分析失败（{type(error).__name__}: {error}）"

    content = getattr(response, "content", None)

    if not isinstance(content, str) or not content.strip():
        return None, "LangChain 分析返回内容为空"

    return content, None


def parse_plan_draft(content):
    """草案结构化解析（LangChain PydanticOutput 对照实现）。

    返回 (PlanDraftSchema | None, error)。注意：解析合法只代表结构合法，
    业务规则与来源依据仍必须由 validate_plan_for_save / check_grounding 二次校验。
    """

    from langchain_core.output_parsers import PydanticOutputParser

    if not isinstance(content, str) or not content.strip():
        return None, "草案内容为空"

    text = content.strip()

    # 去掉可能的 Markdown 围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]

    try:
        parsed = json.loads(text)
    except ValueError as error:
        return None, f"草案不是合法 JSON：{error}"

    parser = PydanticOutputParser(pydantic_object=PlanDraftSchema)

    try:
        # PydanticOutputParser 的校验入口（含格式指引；这里只用其校验能力）
        parser.parse(json.dumps(parsed, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - pydantic ValidationError 等
        return None, f"草案不符合 Pydantic Schema：{error}"

    return PlanDraftSchema.model_validate(parsed), None
