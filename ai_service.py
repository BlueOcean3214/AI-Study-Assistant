import json
import re

import requests


OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3.5:4b"


def call_ollama(prompt):

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "think": False,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content")
        if not isinstance(content, str):
            return None, "Ollama 返回内容格式异常"
        return content, None
    except requests.exceptions.RequestException:
        return None, "无法连接 Ollama，请确认 Ollama 已启动"
    except (ValueError, AttributeError):
        return None, "Ollama 返回内容格式异常"

def ask_with_rag(question, context):
        if context:
            prompt = f"""
    你是一个学习助手。

    请参考下面资料回答问题：

    {context}


    用户问题：
    {question}

    要求：
    1. 优先依据资料回答。
    2. 如果资料没有相关内容，可以使用自己的知识补充。
    3. 不要编造资料中不存在的信息。
    """
        else:
            prompt = question

        return call_ollama(prompt)

def analyze_feedback(feedback_list, summary):
    prompt = f"""
你是 AI Study Assistant 的学习数据分析助手。

下面是用户真实的历史学习反馈：
{json.dumps(feedback_list, ensure_ascii=False, indent=2)}

下面是 Python 根据历史数据计算出的统计结果：
{json.dumps(summary, ensure_ascii=False, indent=2)}

请严格按照以下原则分析：
1. 只陈述数据中明确存在的事实。
2. “实际时间低于预计时间”只能描述为“实际用时低于预计用时”，不能直接判断学习效率高低。
3. 不要推测用户的心理状态。
4. 不要推测用户为什么提前结束任务。
5. “任务难度太高”只能说明这是用户主动选择的反馈原因。
6. 不要把“部分完成”解释成用户一定遇到了具体困难，除非数据明确说明。
7. 所有建议必须能够与历史数据对应。

请严格按照以下格式回答：
### 1. 已知事实
### 2. 可观察的问题
### 3. 下一次计划建议
"""
    return call_ollama(prompt)


REQUIRED_PLAN_FIELDS = {
    "task", "estimated_minutes", "difficulty", "question_count",
    "reason", "completion_criteria", "subtasks",
}
SUBTASK_FIELDS = {"title", "minutes", "question_count", "description"}
CHINESE_NUMBERS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def parse_chinese_number(value):
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        return (CHINESE_NUMBERS.get(left, 1) * 10) + CHINESE_NUMBERS.get(right, 0)
    return CHINESE_NUMBERS.get(value)


def validate_plan(plan, difficult_previous_task=False):
    def invalid(reason):
        return {"valid": False, "reason": reason}

    if not isinstance(plan, dict):
        return invalid("计划必须是 JSON 对象")
    missing = REQUIRED_PLAN_FIELDS - plan.keys()
    if missing:
        return invalid("缺少顶层字段：" + ", ".join(sorted(missing)))
    for field in ("task", "reason", "completion_criteria"):
        if not isinstance(plan[field], str) or not plan[field].strip():
            return invalid(f"{field} 必须是非空字符串")
    if not isinstance(plan["difficulty"], str) or plan["difficulty"] not in {
        "easy", "medium", "hard"
    }:
        return invalid("difficulty 只能是 easy、medium 或 hard")
    for field in ("estimated_minutes", "question_count"):
        if isinstance(plan[field], bool) or not isinstance(plan[field], int):
            return invalid(f"{field} 必须是整数")
        if plan[field] < 0:
            return invalid(f"{field} 不能小于0")
    if plan["estimated_minutes"] > 60:
        return invalid("estimated_minutes 不能超过60分钟")
    if difficult_previous_task:
        if plan["difficulty"] != "easy":
            return invalid("最近任务难度过高时，difficulty 必须为 easy")
        if plan["question_count"] > 6:
            return invalid("最近任务难度过高时，question_count 不能超过6")
        if plan["estimated_minutes"] > 45:
            return invalid("最近任务难度过高时，estimated_minutes 不能超过45")

    subtasks = plan["subtasks"]
    if not isinstance(subtasks, list):
        return invalid("subtasks 必须是列表")
    if not 2 <= len(subtasks) <= 4:
        return invalid("subtasks 数量必须在2到4个之间")
    for index, subtask in enumerate(subtasks, start=1):
        if not isinstance(subtask, dict):
            return invalid(f"第{index}个 subtask 必须是对象")
        missing_subtask = SUBTASK_FIELDS - subtask.keys()
        if missing_subtask:
            return invalid(f"第{index}个 subtask 缺少字段：" + ", ".join(sorted(missing_subtask)))
        for field in ("title", "description"):
            if not isinstance(subtask[field], str) or not subtask[field].strip():
                return invalid(f"第{index}个 subtask 的 {field} 必须是非空字符串")
        for field in ("minutes", "question_count"):
            value = subtask[field]
            if isinstance(value, bool) or not isinstance(value, int):
                return invalid(f"第{index}个 subtask 的 {field} 必须是整数")
            if value < 0:
                return invalid(f"第{index}个 subtask 的 {field} 不能小于0")

    if sum(item["minutes"] for item in subtasks) > plan["estimated_minutes"]:
        return invalid("子任务时间总和超过 estimated_minutes")
    if sum(item["question_count"] for item in subtasks) != plan["question_count"]:
        return invalid("子任务题目数量之和与总题目数量不一致")

    mentioned_counts = re.findall(
        r"([0-9]+|[零一二两三四五六七八九十]+)\s*(?:道|个)?[^0-9零一二两三四五六七八九十]{0,8}题",
        plan["completion_criteria"],
    )
    parsed_counts = [parse_chinese_number(value) for value in mentioned_counts]
    if any(value != plan["question_count"] for value in parsed_counts):
        return invalid("completion_criteria 中的题目数量与 question_count 不一致")
    if plan["question_count"] > 0 and not parsed_counts:
        return invalid("completion_criteria 必须明确写出与 question_count 一致的题目数量")
    return {"valid": True, "reason": None}


def build_plan_prompt(feedback_list, summary, difficult_previous_task, correction=None):
    dynamic_rules = ""
    if difficult_previous_task:
        dynamic_rules = """
最近一次反馈是 partial 且原因是任务难度太高。必须满足：
- difficulty = easy
- question_count <= 6
- estimated_minutes <= 45
"""
    correction_text = ""
    if correction:
        correction_text = f"""
你之前的计划不符合系统规则，原因：{correction}
请重新生成完整计划并修正问题。不要只回复解释。
"""
    return f"""
你是 AI Study Assistant 的学习规划助手。根据真实历史反馈生成下一次学习任务。
历史反馈：
{json.dumps(feedback_list, ensure_ascii=False, indent=2)}
统计结果：
{json.dumps(summary, ensure_ascii=False, indent=2)}

只能根据数据判断，不要推测心理状态，也不要把实际用时较短解释为效率高。
任务时长不得超过60分钟。difficulty 只能为 easy、medium、hard。
必须有2到4个具体可执行的 subtasks，每个包含 title、minutes、question_count、description。
所有数量和分钟数必须是非负整数；子任务分钟总和不得超过 estimated_minutes；
子任务题数总和必须等于 question_count。completion_criteria 必须明确写出准确题数，
且与 question_count 一致；question_count 为0时不要写需要完成多少道题。
{dynamic_rules}
{correction_text}
只返回合法 JSON，不要 Markdown 或其他文字，格式如下：
{{
  "task": "任务名称",
  "estimated_minutes": 45,
  "difficulty": "easy",
  "question_count": 4,
  "reason": "为什么这样调整",
  "completion_criteria": "完成4道指定题目，并完成所有子任务后即可结束。",
  "subtasks": [
    {{"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习指定基础公式"}},
    {{"title": "基础练习", "minutes": 15, "question_count": 2, "description": "完成两道指定基础题"}},
    {{"title": "错题整理", "minutes": 15, "question_count": 2, "description": "完成两道指定练习并订正"}}
  ]
}}
"""


def generate_next_plan(feedback_list, summary):
    if not feedback_list:
        return {"message": "还没有学习反馈数据"}

    latest_feedback = feedback_list[-1]
    difficult_previous_task = (
        latest_feedback["status"] == "partial"
        and latest_feedback.get("reason") == "任务难度太高"
    )
    correction = None

    # Initial generation plus at most two correction attempts.
    for attempt in range(3):
        prompt = build_plan_prompt(
            feedback_list, summary, difficult_previous_task, correction
        )
        plan_text, error = call_ollama(prompt)
        if error:
            return {"message": error, "next_plan": None}
        try:
            plan = json.loads(plan_text)
        except json.JSONDecodeError:
            correction = "AI返回的内容不是合法 JSON"
            if attempt == 0:
                continue
            if attempt == 1:
                continue
            return {
                "message": "AI生成的任务经过两次修正后仍不符合系统规则",
                "next_plan": None,
                "raw_response": plan_text,
            }

        validation = validate_plan(plan, difficult_previous_task)
        if validation["valid"]:
            return {"next_plan": plan}
        correction = validation["reason"]

    return {
        "message": "AI生成的任务经过两次修正后仍不符合系统规则",
        "next_plan": None,
    }
