"""Prompt 注册表回归测试：直接运行 python test_prompt_registry.py。

覆盖：
- 任务/版本管理与环境变量版本切换
- 占位符渲染（含字面花括号不受影响）
- 关键任务的契约句（防止修改一个 Prompt 破坏其他任务的安全/输出规则）
- v1/v2 变体可区分（供对照实验）
- 业务模块接线后的 Prompt 契约（agent_loop / main / ai_service / langchain_agent）
"""

import os

import prompt_registry


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


# 1. 任务与版本管理
tasks = prompt_registry.list_tasks()
check("关键 Prompt 任务已注册",
      {"ask_rag", "agent_system", "analyze_feedback", "plan_generate",
       "plan_draft_system", "langchain_readonly_system"} <= set(tasks),
      f"tasks={tasks}")
check("plan_draft_system 有对照变体",
      prompt_registry.list_versions("plan_draft_system") == ["v1", "v2"])
check("ask_rag 有对照变体",
      prompt_registry.list_versions("ask_rag") == ["v1", "v2"])

try:
    prompt_registry.render("不存在的任务")
    check("未知任务报 PromptError", False)
except prompt_registry.PromptError:
    check("未知任务报 PromptError", True)

try:
    prompt_registry.render("ask_rag", version="v9")
    check("未知版本报 PromptError", False)
except prompt_registry.PromptError:
    check("未知版本报 PromptError", True)

# 2. 渲染与占位符
text = prompt_registry.render("ask_rag", question="问题Q", context="资料C")
check("占位符被替换", "问题Q" in text and "资料C" in text)

plan_prompt = prompt_registry.render(
    "plan_generate",
    feedback_json="[FB]", summary_json="[SUM]",
    dynamic_rules="[RULES]", correction_text="[FIX]",
)
check("plan_generate 动态段渲染",
      "[FB]" in plan_prompt and "[SUM]" in plan_prompt
      and "[RULES]" in plan_prompt and "[FIX]" in plan_prompt)
check("plan_generate 保留字面 JSON 花括号",
      '"task": "任务名称"' in plan_prompt and "{{" not in plan_prompt)

# 3. 版本切换（环境变量）
check("默认版本为 v1", prompt_registry.active_version("plan_draft_system") == "v1")

os.environ["PROMPT_VERSION_PLAN_DRAFT_SYSTEM"] = "v2"
try:
    check("环境变量切换默认版本",
          prompt_registry.active_version("plan_draft_system") == "v2")
    rendered = prompt_registry.render("plan_draft_system")
    check("按切换后版本渲染", "不要输出思考过程" in rendered)
finally:
    os.environ.pop("PROMPT_VERSION_PLAN_DRAFT_SYSTEM", None)

check("显式 version 参数优先", "不要输出思考过程" in prompt_registry.render("plan_draft_system", version="v2"))

# 4. 关键契约句（回归保护：修改 Prompt 不得破坏安全/输出规则）
agent_text = prompt_registry.render("agent_system", tools="[TOOLS]", max_tool_calls=4, max_loop_turns=3)
check("agent_system 保留 save_plan 确认凭证规则",
      "confirmation_id" in agent_text and "不构成用户确认" in agent_text)
check("agent_system 保留来源完整性规则", "来源完整性" in agent_text)
check("agent_system 保留工具预算", "最多调用 4 次 Tool" in agent_text and "最多运行 3 轮" in agent_text)
check("agent_system 注入工具清单", "[TOOLS]" in agent_text)

draft_v1 = prompt_registry.render("plan_draft_system", version="v1")
check("plan_draft_system 保留数值一致性清单",
      "子任务的 question_count 之和必须等于顶层 question_count" in draft_v1)
check("plan_draft_system 保留来源完整性硬规则",
      "不得声称依据知识库" in draft_v1 and "不得假装检索成功" in draft_v1)
check("plan_draft_system 保留只输出 JSON 约束",
      "只输出一个 JSON 对象" in draft_v1)

ask_text = prompt_registry.render("ask_rag", question="Q", context="C")
check("ask_rag 保留不编造约束", "不要编造资料不存在的信息" in ask_text)

langchain_text = prompt_registry.render("langchain_readonly_system")
check("langchain_readonly_system 保留只读边界",
      "没有任何写权限" in langchain_text and "不支持保存计划" in langchain_text)

# 5. 业务模块接线（渲染结果与模块出口一致）
import agent_loop
import ai_service
import langchain_agent
import main

check("agent_loop.SYSTEM_PROMPT 来自 registry",
      agent_loop.SYSTEM_PROMPT == prompt_registry.render(
          "agent_system",
          tools=agent_loop.describe_tools(),
          max_tool_calls=agent_loop.MAX_TOOL_CALLS,
          max_loop_turns=agent_loop.MAX_LOOP_TURNS,
      ))
check("main.build_ask_prompt 来自 registry",
      main.build_ask_prompt("Q", "C") == prompt_registry.render("ask_rag", question="Q", context="C")
      and main.build_ask_prompt("Q", "") == prompt_registry.render("ask_plain", question="Q"))
check("langchain_agent 系统提示来自 registry",
      langchain_agent.build_system_prompt() == prompt_registry.render("langchain_readonly_system"))

analyze_prompt = ai_service.build_plan_prompt([{"x": 1}], {"y": 2}, False)
check("ai_service.build_plan_prompt 来自 registry",
      analyze_prompt == prompt_registry.render(
          "plan_generate",
          feedback_json='[\n  {\n    "x": 1\n  }\n]',
          summary_json='{\n  "y": 2\n}',
          dynamic_rules="",
          correction_text="",
      ))

check("v1 与 v2 变体可区分",
      draft_v1 != prompt_registry.render("plan_draft_system", version="v2"))

print("ALL TESTS COMPLETED")
