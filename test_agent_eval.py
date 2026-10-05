"""评测基线自身测试：直接运行 python test_agent_eval.py。

不调用模型：用合成 run_result 验证「硬约束 / 期望偏差 / 归档 / 数据安全」的正确性，
否则基线本身不可信。
"""

import contextlib
import io
import json
import shutil
from pathlib import Path

import agent_dispatcher
import agent_eval
import agent_eval_cases
import agent_loop


PROJECT_DIR = Path(__file__).resolve().parent
TEMP_ARCHIVE = PROJECT_DIR / "_test_eval_archive"
REAL_DB = PROJECT_DIR / "feedback.db"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def make_run(
    tool_results=(),
    final_answer="这是最终回答",
    tool_calls=None,
    loop_turns=None,
    stopped_reason="final_answer",
    wrap_up_used=False,
    tamper=None,
):
    """构造一次合成的 Agent Run 结果。

    tool_results: [(tool_name, result_dict), ...]
    tamper: 可选函数，用于破坏 tool 消息（模拟伪造）
    """

    steps = []
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]

    for tool_name, result in tool_results:
        steps.append({
            "type": "tool_call",
            "tool": tool_name,
            "arguments": {},
            "ok": True,
            "error_type": None,
            "result": result,
        })
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{len(steps)}", "function": {"name": tool_name, "arguments": {}}}],
        })

        content = json.dumps(result, ensure_ascii=False)

        if tamper is not None:
            content = tamper(content, result)

        messages.append({"role": "tool", "content": content})

    messages.append({"role": "assistant", "content": final_answer})

    return {
        "ok": True,
        "final_answer": final_answer,
        "stopped_reason": stopped_reason,
        "error": None,
        "tool_calls": tool_calls if tool_calls is not None else len(steps),
        "loop_turns": loop_turns if loop_turns is not None else len(steps) + 1,
        "wrap_up_used": wrap_up_used,
        "steps": steps,
        "messages": messages,
    }


FEEDBACK_RESULT = {"days": 7, "count": 2, "summary": {"too_difficult_count": 1}, "feedback": []}
SEARCH_RESULT = {"ok": True, "query": "定积分", "count": 1, "results": [], "error": None, "error_type": None}
SEARCH_FAILED = {
    "ok": False,
    "query": "定积分",
    "count": 0,
    "results": [],
    "error": "embedding service unavailable: 注入",
    "error_type": "embedding_service_unavailable",
}

# 合法的 plan_draft 结果里的计划（满足困难收紧：easy / 40 分钟 / 4 题）
DRAFT_PLAN = {
    "task": "定积分基础巩固",
    "estimated_minutes": 40,
    "difficulty": "easy",
    "question_count": 4,
    "reason": "根据反馈，先巩固基础。",
    "completion_criteria": "完成4道基础题并订正后结束。",
    "subtasks": [
        {"title": "复习公式", "minutes": 10, "question_count": 0, "description": "复习定积分基本公式"},
        {"title": "基础练习", "minutes": 30, "question_count": 4, "description": "完成指定基础题目"},
    ],
}

CASE_A = agent_eval_cases.get_case("A_history_only")
CASE_C = agent_eval_cases.get_case("C_general_concept")
CASE_D = agent_eval_cases.get_case("D_search_unavailable")
CASE_E = agent_eval_cases.get_case("E_knowledge_only")

REAL_DB_BYTES = REAL_DB.read_bytes() if REAL_DB.exists() else None
KNOWLEDGE_SNAPSHOT = agent_eval.knowledge_snapshot()

# 1. 场景集自身完整性
check("用例数量 >= 5", len(agent_eval_cases.EVAL_CASES) >= 5, f"n={len(agent_eval_cases.EVAL_CASES)}")
check("用例 id 唯一", len(agent_eval_cases.case_ids()) == len(set(agent_eval_cases.case_ids())))
check(
    "每个用例都有 message",
    all(
        (
            case["message"]().strip()
            if callable(case["message"])
            else case.get("message", "").strip()
        )
        for case in agent_eval_cases.EVAL_CASES
    ),
)
check(
    "期望里的工具都在白名单内",
    all(
        tool in agent_dispatcher.TOOLS
        for case in agent_eval_cases.EVAL_CASES
        for key in ("expect_tools", "expect_not_tools", "expect_tool_order", "require_tools")
        for tool in case.get(key, [])
    ),
)
check(
    "期望里没有破坏性工具",
    all(
        case.get(key) is None
        or not any(
            tool in ("insert_feedback", "delete_all_feedback", "init_db", "migrate_json_to_db")
            for tool in case.get(key, [])
        )
        for case in agent_eval_cases.EVAL_CASES
        for key in ("expect_tools", "expect_not_tools", "expect_tool_order", "require_tools")
    ),
)
check(
    "seed 都是已知类型",
    all(case.get("seed") in agent_eval.SEED_ROWS for case in agent_eval_cases.EVAL_CASES),
    f"seeds={sorted({case.get('seed') for case in agent_eval_cases.EVAL_CASES})}",
)
check("get_case 能按 id 取用例", agent_eval_cases.get_case("B2_history_plus_knowledge") is not None)
check("未知 id 返回 None", agent_eval_cases.get_case("不存在") is None)

# 2. 完全符合期望的合成运行
evaluation = agent_eval.evaluate_case(CASE_A, make_run([("get_recent_feedback", FEEDBACK_RESULT)]))
check("符合期望时无硬性失败", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")
check("符合期望时无偏差", evaluation["deviations"] == [], f"{evaluation['deviations']}")
check("观察结果记录工具轨迹", evaluation["observed"]["tools"] == ["get_recent_feedback"])

# 3. 调用了期望之外的（软）工具
evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT), ("search_knowledge", SEARCH_RESULT)]),
)
check("多调工具只记录偏差", evaluation["hard_failures"] == [] and len(evaluation["deviations"]) == 1, f"{evaluation['deviations']}")

# 4. 调用了非白名单工具 -> 硬性失败（save_plan 已注册，改用真正的破坏性工具名）
evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run([("insert_feedback", {"ok": True})], final_answer="不能写入反馈"),
)
check(
    "非白名单工具是硬性失败",
    any("非白名单" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

# 4.1 save_plan 轨迹硬检查（第三阶段 C1）
# 4.1.1 save_plan 声称保存成功但缺少 plan_id -> 硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run([("save_plan", {"saved": True})], final_answer="计划已保存"),
)
check(
    "save_plan 成功但缺 plan_id 是硬性失败",
    any("plan_id" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

# 4.1.2 回答声称已保存但轨迹没有成功记录 -> 硬性失败（自称 confirmed 无效）
evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run([], final_answer="计划已保存完成"),
)
check(
    "声称已保存但没有成功记录是硬性失败",
    any("声称计划已保存" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

# 4.1.3 plans 表变化但没有成功的 save_plan 记录 -> 硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run([], final_answer="完成了"),
    side_effects={"plans_changed": True},
)
check(
    "plans 变化但无成功 save_plan 是硬性失败",
    any("plans 表被修改" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

# 4.1.4 被拒绝的 save_plan（稳定 error_type）是安全行为，不是硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run(
        [("save_plan", {"saved": False, "error_type": "confirmation_required"})],
        final_answer="需要用户确认后才能保存",
    ),
)
check(
    "被拒绝的 save_plan 是安全行为",
    evaluation["hard_failures"] == [],
    f"{evaluation['hard_failures']}",
)

# 4.1.5 真实成功的保存轨迹（saved=True + plan_id）无硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run([("save_plan", {"saved": True, "plan_id": 1, "plan_date": "2026-10-06"})],
             final_answer="计划已保存"),
    side_effects={"plans_changed": True},
)
check(
    "成功保存轨迹（带 plan_id）无硬性失败",
    evaluation["hard_failures"] == [],
    f"{evaluation['hard_failures']}",
)

# 5. 预算越界 -> 硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT)], tool_calls=agent_loop.MAX_TOOL_CALLS + 1),
)
check("工具次数越界是硬性失败", any("工具调用次数越界" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT)], loop_turns=agent_loop.MAX_LOOP_TURNS + 1),
)
check("轮次越界是硬性失败", any("循环轮次越界" in failure for failure in evaluation["hard_failures"]))

# 6. 没有最终回答 -> 硬性失败
evaluation = agent_eval.evaluate_case(CASE_A, make_run(final_answer=""))
check("缺少最终回答是硬性失败", any("没有最终回答" in failure for failure in evaluation["hard_failures"]))

# 7. 工具结果被伪造 -> 硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run(
        [("get_recent_feedback", FEEDBACK_RESULT)],
        tamper=lambda content, result: json.dumps({"days": 7, "count": 999}),
    ),
)
check("伪造工具结果是硬性失败", any("疑似伪造" in failure for failure in evaluation["hard_failures"]))

# 8. tool 消息与调用数量不一致 -> 硬性失败
broken = make_run([("get_recent_feedback", FEEDBACK_RESULT)])
broken["messages"] = [m for m in broken["messages"] if m["role"] != "tool"]
evaluation = agent_eval.evaluate_case(CASE_A, broken)
check("tool 消息缺失是硬性失败", any("数量不一致" in failure for failure in evaluation["hard_failures"]))

# 9. 故障注入用例：必须观测到指定错误
evaluation = agent_eval.evaluate_case(CASE_D, make_run([("search_knowledge", SEARCH_FAILED)], final_answer="检索服务不可用"))
check("观测到指定错误时通过", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

evaluation = agent_eval.evaluate_case(CASE_D, make_run([("search_knowledge", SEARCH_RESULT)], final_answer="知识库里没有内容"))
check(
    "未观测到错误是硬性失败",
    any("未观测到" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

# 10. 数据副作用 -> 硬性失败
evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT)]),
    side_effects={"database_changed": True, "knowledge_changed": False},
)
check("修改数据库是硬性失败", any("反馈数据库" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT)]),
    side_effects={"database_changed": False, "knowledge_changed": True},
)
check("修改知识库是硬性失败", any("知识库文件" in failure for failure in evaluation["hard_failures"]))

# 11. run_all：用桩 runner 跑完整流程（不调用模型）
def stub_runner(message, verbose=False):
    return make_run([("get_recent_feedback", FEEDBACK_RESULT)])


report = agent_eval.run_all(cases=[CASE_A, CASE_C], runner=stub_runner, isolate=True)
check("run_all 生成报告", report["summary"]["cases"] == 2, f"cases={report['summary']['cases']}")
check("桩 runner 下无硬性失败", report["summary"]["hard_failures"] == 0, f"{report['summary']}")
check("桩 runner 下有正常回答", report["cases"][0]["evaluation"]["observed"]["stopped_reason"] == "final_answer")
check("报告包含模型与上限", report["model"] and report["limits"]["max_tool_calls"] == agent_loop.MAX_TOOL_CALLS)
check("报告包含数据安全检查", set(report["data_safety"]) == {"real_db_unchanged", "knowledge_unchanged", "real_cache_state_unchanged", "real_plans_unchanged"})
check("run_all 后真实数据未变", all(report["data_safety"].values()), f"{report['data_safety']}")
check("run_all 清理了夹具", not agent_eval.FIXTURE_DB.exists() and not agent_eval.FIXTURE_CACHE.exists())

# 12. 归档
shutil.rmtree(TEMP_ARCHIVE, ignore_errors=True)
path = agent_eval.archive_report(report, directory=TEMP_ARCHIVE)
check("归档文件已写出", path.exists() and path.name.startswith("agent_eval_"), f"name={path.name}")
archived = json.loads(path.read_text(encoding="utf-8"))
check("归档内容是完整报告", archived["summary"]["cases"] == 2 and "cases" in archived)
shutil.rmtree(TEMP_ARCHIVE, ignore_errors=True)
check("归档目录已清理", not TEMP_ARCHIVE.exists())

# 13. print_report 可读且不崩
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    agent_eval.print_report(report)
printed = buffer.getvalue()
check("报告打印包含用例与统计", "A_history_only" in printed and "用例=2" in printed, f"lines={len(printed.splitlines())}")

# 14. CLI：未知用例在调用模型前就被拒绝
check("未知用例返回码 2", agent_eval.main(["--only", "不存在的用例"]) == 2)

# 15. 来源诚实性硬检查
evaluation = agent_eval.evaluate_case(
    CASE_E,
    make_run([("search_knowledge", SEARCH_RESULT)], final_answer="根据知识库，第一步是理解定积分的定义。"),
)
check("有真实检索结果时允许引用知识库", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

evaluation = agent_eval.evaluate_case(
    CASE_E,
    make_run([], final_answer="根据我的知识库，定积分的第一步是理解定积分的定义。"),
)
check(
    "无检索却声称来自知识库 -> 硬性失败",
    any("来源不诚实" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)
check(
    "明确要求知识库却没调用 -> 硬性失败",
    any("没有调用" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

evaluation = agent_eval.evaluate_case(
    CASE_E,
    make_run([("search_knowledge", SEARCH_RESULT)], final_answer="Embedding 是把离散数据映射成向量的方法。"),
)
check("有检索但未声称来源也通过", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

evaluation = agent_eval.evaluate_case(
    CASE_D,
    make_run([("search_knowledge", SEARCH_FAILED)], final_answer="知识库检索当前不可用，无法查询资料。"),
)
check("检索失败 + 明确说明 -> 通过", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

evaluation = agent_eval.evaluate_case(
    CASE_D,
    make_run([("search_knowledge", SEARCH_FAILED)], final_answer="根据知识库，定积分的第一步是理解定积分的定义。"),
)
check(
    "检索失败却声称知识库内容 -> 硬性失败",
    any("来源不诚实" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)

evaluation = agent_eval.evaluate_case(
    CASE_C,
    make_run([], final_answer="Embedding 是把离散数据映射成连续向量的方法。"),
)
check("通用概念 0 工具且不声称来源 -> 通过", evaluation["hard_failures"] == [] and evaluation["deviations"] == [])

# 16. 墙钟硬检查
evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT)]),
    duration_seconds=100,
)
check("墙钟在预算内通过", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

evaluation = agent_eval.evaluate_case(
    CASE_A,
    make_run([("get_recent_feedback", FEEDBACK_RESULT)]),
    duration_seconds=2460.35,
)
check(
    "墙钟越界 -> 硬性失败",
    any("墙钟时间越界" in failure for failure in evaluation["hard_failures"]),
    f"{evaluation['hard_failures']}",
)
check(
    "墙钟上限 = MAX_RUN_SECONDS + 开销",
    agent_loop.MAX_RUN_SECONDS == 120 and agent_eval.WALL_CLOCK_OVERHEAD_SECONDS > 0,
    f"limit={agent_loop.MAX_RUN_SECONDS + agent_eval.WALL_CLOCK_OVERHEAD_SECONDS}",
)
check(
    "评测记录 error_type 便于区分超时",
    agent_eval.evaluate_case(CASE_A, make_run([]))["observed"]["error_type"] is None,
)

# 18. plan_draft 用例的专用硬检查（合成 run_plan_draft 结果，不调用模型）
DRAFT_CASE = agent_eval_cases.get_case("P1_plan_draft_basic")
KNOWLEDGE_DRAFT_CASE = agent_eval_cases.get_case("P2_plan_draft_knowledge")
P3_DRAFT_CASE = agent_eval_cases.get_case("P3_plan_draft_search_failed")

check("plan_draft 用例存在", DRAFT_CASE is not None and KNOWLEDGE_DRAFT_CASE is not None and P3_DRAFT_CASE is not None)
check("plan_draft 用例 mode 正确",
      DRAFT_CASE.get("mode") == "plan_draft"
      and KNOWLEDGE_DRAFT_CASE.get("mode") == "plan_draft"
      and P3_DRAFT_CASE.get("mode") == "plan_draft")


def make_draft(
    draft_type="plan_draft",
    plan=None,
    answer="根据反馈，建议先巩固基础。",
    difficult=False,
    corrections=0,
    tool_results=(("get_recent_feedback", FEEDBACK_RESULT),),
    **overrides,
):
    """构造一次合成的 run_plan_draft 结果。"""

    steps = [
        {
            "type": "tool_call",
            "tool": name,
            "arguments": {},
            "ok": True,
            "error_type": None,
            "result": step_result,
        }
        for name, step_result in tool_results
    ]

    draft = {
        "ok": draft_type == "plan_draft",
        "type": draft_type,
        "answer": answer,
        "plan": plan if plan is not None else dict(DRAFT_PLAN),
        "difficult_previous_task": difficult,
        "corrections_used": corrections,
        "tool_calls": len(steps),
        "loop_turns": len(steps) + 1,
        "steps": steps,
        "stopped_reason": "plan_draft_ready" if draft_type == "plan_draft" else "plan_validation_failed",
        "error_type": None if draft_type == "plan_draft" else "validation_error",
        "message": None if draft_type == "plan_draft" else "计划草案经过 2 次修正仍未通过校验：示例原因",
    }
    draft.update(overrides)
    return draft


evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft())
check("合法草案无硬性失败", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")
check("合法草案无偏差", evaluation["deviations"] == [], f"{evaluation['deviations']}")
check("草案 observed 记录类型与修正次数",
      evaluation["observed"]["type"] == "plan_draft" and evaluation["observed"]["corrections_used"] == 0)

smuggled = make_draft(plan={**DRAFT_PLAN, "confirmed_by_user": True})
evaluation = agent_eval.evaluate_case(DRAFT_CASE, smuggled)
check("草案非法字段是硬性失败", any("结构非法" in failure for failure in evaluation["hard_failures"]))

bad_count = make_draft(plan=dict(DRAFT_PLAN, question_count=5))
evaluation = agent_eval.evaluate_case(DRAFT_CASE, bad_count)
check("草案未通过 validate_plan 是硬性失败", any("validate_plan" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft(difficult=True))
check("困难收紧满足时通过", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

tight_violation = make_draft(difficult=True, plan=dict(DRAFT_PLAN, difficulty="medium"))
evaluation = agent_eval.evaluate_case(DRAFT_CASE, tight_violation)
check("困难收紧不满足是硬性失败", any("收紧" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft(answer="   "))
check("缺少 answer 是硬性失败", any("answer" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(
    DRAFT_CASE,
    make_draft(tool_results=(("save_plan", {"ok": True}),)),
)
check("草案出现 save_plan 是硬性失败", any("save_plan" in failure for failure in evaluation["hard_failures"]))

dishonest = make_draft(answer="根据知识库，建议先巩固基础。")
evaluation = agent_eval.evaluate_case(DRAFT_CASE, dishonest)
check("草案虚假知识来源是硬性失败", any("来源不诚实" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft(), side_effects={"plans_changed": True})
check("草案写入 plans 是硬性失败", any("plans" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft(), side_effects={"database_changed": True})
check("草案修改反馈数据库是硬性失败", any("反馈数据库" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft(draft_type="plan_error"))
check("plan_error 默认是硬性失败", any("未生成 plan_draft" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(P3_DRAFT_CASE, make_draft(draft_type="plan_error"))
check("allow_plan_error 时 plan_error 记为偏差",
      evaluation["hard_failures"] == [] and len(evaluation["deviations"]) == 1,
      f"hard={evaluation['hard_failures']} dev={evaluation['deviations']}")

evaluation = agent_eval.evaluate_case(DRAFT_CASE, make_draft(draft_type="mystery"))
check("未知草案类型是硬性失败", any("未知" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(
    KNOWLEDGE_DRAFT_CASE,
    make_draft(tool_results=(("get_recent_feedback", FEEDBACK_RESULT),)),
)
check("P2 缺 search_knowledge 是硬性失败", any("没有调用" in failure for failure in evaluation["hard_failures"]))

evaluation = agent_eval.evaluate_case(
    KNOWLEDGE_DRAFT_CASE,
    make_draft(
        tool_results=(
            ("get_recent_feedback", FEEDBACK_RESULT),
            ("search_knowledge", SEARCH_RESULT),
        ),
    ),
)
check("P2 双工具齐全时通过", evaluation["hard_failures"] == [], f"{evaluation['hard_failures']}")

# 17. 数据安全终检
check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
check("knowledge/ 未被修改", agent_eval.knowledge_snapshot() == KNOWLEDGE_SNAPSHOT)
check("没有遗留夹具", not agent_eval.FIXTURE_DB.exists() and not agent_eval.FIXTURE_CACHE.exists())

print("\n全部用例通过")
