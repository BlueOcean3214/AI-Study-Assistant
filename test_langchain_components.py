"""LangChain 组件接入测试：直接运行 python test_langchain_components.py。

全 Mock ChatModel（脚本化回放），不发起真实请求。

覆盖：
- ChatPromptTemplate 构建（文案与 prompt_registry 一致、变量注入）
- Chat Model 业务调用（analyze_feedback_langchain 契约：(answer, error)）
- PydanticOutput 结构化解析（合法草案 / 非法 JSON / Schema 不符 / Markdown 围栏）
- 解析合法 ≠ 业务合法（业务校验仍需 validate_plan_for_save）
- /experiment/langchain/analyze 路由开关与错误结构
"""

import contextlib
import json

import langchain_service
import main


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


VALID_DRAFT = {
    "answer": "这是计划说明。",
    "plan": {
        "task": "定积分基础巩固",
        "estimated_minutes": 45,
        "difficulty": "easy",
        "question_count": 4,
        "reason": "根据反馈先巩固基础。",
        "completion_criteria": "完成4道基础题并订正后结束。",
        "subtasks": [
            {"title": "复习公式", "minutes": 15, "question_count": 0, "description": "复习基本公式"},
            {"title": "基础练习", "minutes": 30, "question_count": 4, "description": "完成指定基础题"},
        ],
    },
}


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeChatModel:
    def __init__(self, content):
        self.content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return FakeResponse(self.content)


# 1. ChatPromptTemplate 构建
messages = langchain_service.build_analyze_prompt([{"task": "x"}], {"count": 1})
text = "".join(getattr(m, "content", "") for m in messages)
check("ChatPromptTemplate 注入真实数据", "task" in text and "count" in text)
check("ChatPromptTemplate 保留输出格式约束", "### 1. 已知事实" in text)

# 2. 业务调用契约
model = FakeChatModel("### 1. 已知事实\n- 任务 x")
answer, error = langchain_service.analyze_feedback_langchain([{"task": "x"}], {"count": 1}, model=model)
check("analyze_feedback_langchain 返回 (answer, error)",
      error is None and answer.startswith("### 1. 已知事实"))
check("模型收到格式化消息", model.seen is not None)

model2 = FakeChatModel("")
answer, error = langchain_service.analyze_feedback_langchain([], {}, model=model2)
check("空回答返回稳定错误", answer is None and error is not None)

# 3. Pydantic 结构化解析
parsed, error = langchain_service.parse_plan_draft(json.dumps(VALID_DRAFT, ensure_ascii=False))
check("合法草案解析成功", error is None and parsed.plan.task == "定积分基础巩固")

fenced = "```json\n" + json.dumps(VALID_DRAFT, ensure_ascii=False) + "\n```"
parsed, error = langchain_service.parse_plan_draft(fenced)
check("Markdown 围栏草案可解析", error is None and parsed is not None)

parsed, error = langchain_service.parse_plan_draft("{不是合法 JSON")
check("非法 JSON 报清晰错误", parsed is None and "JSON" in error)

broken = dict(VALID_DRAFT)
broken["plan"] = dict(VALID_DRAFT["plan"], subtasks=[{"title": "只有1个"}])
parsed, error = langchain_service.parse_plan_draft(json.dumps(broken, ensure_ascii=False))
check("Schema 不符报清晰错误", parsed is None and "Schema" in error, f"error={error}")

# 解析合法 ≠ 业务合法：业务规则（题数不一致）仍由 validate_plan_for_save 拒绝
lied = json.loads(json.dumps(VALID_DRAFT))
lied["plan"]["question_count"] = 5
parsed, error = langchain_service.parse_plan_draft(json.dumps(lied, ensure_ascii=False))

import agent_tools

validation = agent_tools.validate_plan_for_save(
    {k: v for k, v in parsed.plan.model_dump().items()}
)
check("解析通过但业务校验仍拒绝（题数不一致）",
      parsed is not None and validation["valid"] is False,
      f"validation={validation}")

# 4. 路由开关
with contextlib.ExitStack():
    import os

    os.environ.pop("LANGCHAIN_EXPERIMENT_ENABLED", None)
    response = main.langchain_analyze(main.LangChainAnalyzeRequest(feedback_list=[], summary={}))
    check("实验关闭时 403", getattr(response, "status_code", None) == 403)

    os.environ["LANGCHAIN_EXPERIMENT_ENABLED"] = "1"

    original = langchain_service.analyze_feedback_langchain
    langchain_service.analyze_feedback_langchain = lambda fb, sm: ("分析结果", None)

    try:
        result = main.langchain_analyze(main.LangChainAnalyzeRequest(feedback_list=[], summary={}))
    finally:
        langchain_service.analyze_feedback_langchain = original
        os.environ.pop("LANGCHAIN_EXPERIMENT_ENABLED", None)

    check("实验开启时路由放行", result["ok"] is True and result["engine"] == "langchain")

print("ALL TESTS COMPLETED")
