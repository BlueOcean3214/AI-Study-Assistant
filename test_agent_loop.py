"""Agent Loop 测试：直接运行 python test_agent_loop.py。

分两部分：
- 确定性部分：替换 agent_loop.chat_with_tools 为脚本化 LLM，验证 Loop 机制
  （动态决策、预算、错误处理、消息结构、日志）
- 真实部分：用真实 qwen3.5:4b 跑 5 个场景，验证端到端闭环

隔离：反馈数据库与向量缓存都指向临时夹具；
结束时校验真实 feedback.db / knowledge/ / .rag_cache 未被改动。
"""

import contextlib
import copy
import gc
import io
import json
import shutil
import time
from pathlib import Path

import agent_dispatcher
import agent_loop
import agent_schema
import database
import embedding_service
import rag_service
import vector_cache


PROJECT_DIR = Path(__file__).resolve().parent
FIXTURE_DB = PROJECT_DIR / "_test_loop.db"
CACHE_FIXTURE = PROJECT_DIR / "_test_cache_loop"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_CACHE = PROJECT_DIR / ".rag_cache"


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


def tool_call(name, arguments, call_id="call_1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
    }


def multi_tool_call(*calls):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"call_{index}", "function": {"name": name, "arguments": arguments}}
            for index, (name, arguments) in enumerate(calls)
        ],
    }


def final(answer):
    return {"role": "assistant", "content": answer}


def scripted(responses, fallback=None):
    """返回 (fake_llm, seen_messages)。每次调用前记录一份消息历史快照。

    responses 用尽后返回 fallback：默认给一句非空回答（用于验证收尾调用成功），
    传 {"role": "assistant", "content": ""} 可以验证收尾也拿不到答案的情况。
    """

    seen = []

    def fake(messages, tools=None):
        seen.append(copy.deepcopy(messages))
        index = len(seen) - 1

        if index < len(responses):
            return responses[index], None

        return fallback if fallback is not None else final("（脚本已用尽）"), None

    return fake, seen


@contextlib.contextmanager
def stub_llm(fake):
    original = agent_loop.chat_with_tools
    agent_loop.chat_with_tools = fake
    try:
        yield
    finally:
        agent_loop.chat_with_tools = original


@contextlib.contextmanager
def broken_embedding():
    original_module = embedding_service.get_embedding
    original_rag = rag_service.get_embedding
    original_rag_many = rag_service.get_embeddings

    def failing_embedding(text):
        raise embedding_service.EmbeddingError("模拟 embedding 服务不可用")

    def failing_embeddings(texts):
        raise embedding_service.EmbeddingError("模拟 embedding 服务不可用")

    embedding_service.get_embedding = failing_embedding
    rag_service.get_embedding = failing_embedding
    rag_service.get_embeddings = failing_embeddings
    try:
        yield
    finally:
        embedding_service.get_embedding = original_module
        rag_service.get_embedding = original_rag
        rag_service.get_embeddings = original_rag_many


def seed_feedback():
    database.delete_all_feedback()
    database.insert_feedback(
        {
            "task": "定积分综合题专项练习",
            "estimated_minutes": 60,
            "actual_minutes": 40,
            "status": "partial",
            "reason": "任务难度太高",
            "completed_subtasks": 1,
            "total_subtasks": 3,
            "completed_questions": 2,
            "total_questions": 4,
        },
        "2026-10-04T20:00:00",
    )
    database.insert_feedback(
        {
            "task": "定积分基础题练习",
            "estimated_minutes": 45,
            "actual_minutes": 45,
            "status": "completed",
            "reason": None,
            "completed_subtasks": 3,
            "total_subtasks": 3,
            "completed_questions": 4,
            "total_questions": 4,
        },
        "2026-10-05T09:00:00",
    )


def tool_messages(result):
    return [message for message in result["messages"] if message.get("role") == "tool"]


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
seed_feedback()

try:
    # ==========================
    # 1. 确定性 Loop 机制
    # ==========================

    # 1.1 不需要 Tool 时直接回答
    fake, seen = scripted([final("Embedding 是把文本映射成向量的技术。")])
    with stub_llm(fake):
        result = agent_loop.run_agent("什么是 Embedding？", verbose=False)

    check("无需工具时直接回答", result["ok"] is True and result["stopped_reason"] == "final_answer", f"reason={result['stopped_reason']}")
    check("没有调用任何工具", result["tool_calls"] == 0 and result["steps"] == [])
    check("只用了 1 轮", result["loop_turns"] == 1, f"turns={result['loop_turns']}")
    check("回答内容正确", result["final_answer"] == "Embedding 是把文本映射成向量的技术。")
    check("消息历史只有 system/user/assistant", [m["role"] for m in result["messages"]] == ["system", "user", "assistant"])
    check("把工具清单传给了模型", bool(seen[0][0]["content"]) and "get_recent_feedback" in seen[0][0]["content"])

    # 1.2 能调用 get_recent_feedback
    fake, seen = scripted([
        tool_call("get_recent_feedback", {"days": 7}),
        final("最近 7 天有 2 条反馈。"),
    ])
    with stub_llm(fake):
        result = agent_loop.run_agent("看看我最近 7 天的学习情况。", verbose=False)

    check("调用了 get_recent_feedback", result["steps"][0]["tool"] == "get_recent_feedback", f"steps={[s['tool'] for s in result['steps']]}")
    check("工具调用计数正确", result["tool_calls"] == 1 and result["loop_turns"] == 2)
    check("最终回答来自第二轮", result["final_answer"] == "最近 7 天有 2 条反馈。")

    # 1.3 能调用 search_knowledge
    fake, seen = scripted([
        tool_call("search_knowledge", {"query": "定积分怎么学", "top_k": 3}),
        final("知识库建议：先理解定义。"),
    ])
    with stub_llm(fake):
        result = agent_loop.run_agent("知识库里定积分怎么学？", verbose=False)

    check("调用了 search_knowledge", result["steps"][0]["tool"] == "search_knowledge")
    check("检索结果进入 steps", result["steps"][0]["result"]["count"] >= 0)

    # 1.4 第二个 Tool 由第一个 Tool Result 动态决定
    def dynamic_llm(messages, tools=None):
        tool_results = [m for m in messages if m.get("role") == "tool"]

        if not tool_results:
            return tool_call("get_recent_feedback", {"days": 7}), None

        # 拿到两条信息（历史 + 知识库）之后就应该给出最终回答
        if len(tool_results) >= 2:
            return final("结合你的历史反馈和知识库建议，先巩固定义再练基础题。"), None

        first_result = json.loads(tool_results[0]["content"])

        if first_result["summary"]["too_difficult_count"] > 0:
            return tool_call("search_knowledge", {"query": "定积分学习方法", "top_k": 3}), None

        return final("你的历史反馈里没有难度过高的任务。"), None

    with stub_llm(dynamic_llm):
        result = agent_loop.run_agent("我最近定积分学不完，该怎么学？", verbose=False)

    check(
        "动态决定第二个工具",
        [step["tool"] for step in result["steps"]] == ["get_recent_feedback", "search_knowledge"],
        f"steps={[step['tool'] for step in result['steps']]}",
    )
    check("完整多轮 Run 收尾正常", result["stopped_reason"] == "final_answer" and result["tool_calls"] == 2 and result["loop_turns"] == 3)

    # 反证：如果第一个 Tool Result 显示“没有难度过高”，就不该查知识库
    database.delete_all_feedback()
    database.insert_feedback(
        {
            "task": "普通练习",
            "estimated_minutes": 30,
            "actual_minutes": 30,
            "status": "completed",
            "reason": None,
            "completed_subtasks": 0,
            "total_subtasks": 0,
            "completed_questions": 0,
            "total_questions": 0,
        },
        "2026-10-05T09:00:00",
    )
    try:
        with stub_llm(dynamic_llm):
            counter_example = agent_loop.run_agent("我最近定积分学不完，该怎么学？", verbose=False)
    finally:
        seed_feedback()

    check(
        "结果不同则决策不同（证明不是固定工作流）",
        [step["tool"] for step in counter_example["steps"]] == ["get_recent_feedback"],
        f"steps={[step['tool'] for step in counter_example['steps']]}",
    )

    # 1.5 Tool Result 正确进入下一轮 LLM
    fake, seen = scripted([
        tool_call("get_recent_feedback", {"days": 7}),
        final("收到。"),
    ])
    with stub_llm(fake):
        agent_loop.run_agent("看看最近情况", verbose=False)

    second_turn_messages = seen[1]
    tool_message = [m for m in second_turn_messages if m.get("role") == "tool"]
    check("第二轮能看到 tool 结果", len(tool_message) == 1)
    tool_payload = json.loads(tool_message[0]["content"])
    check("tool 结果是结构化数据", tool_payload["days"] == 7 and tool_payload["count"] == 2, f"payload_keys={sorted(tool_payload)}")

    assistant_with_call = [m for m in second_turn_messages if m.get("role") == "assistant" and m.get("tool_calls")]
    check("assistant tool_call 也进了历史", len(assistant_with_call) == 1)
    check(
        "历史角色顺序正确",
        [m["role"] for m in second_turn_messages] == ["system", "user", "assistant", "tool"],
        f"roles={[m['role'] for m in second_turn_messages]}",
    )

    # 1.6 MAX_TOOL_CALLS 生效（工具次数被限制；预算耗尽后允许一次无工具收尾）
    two_tools = multi_tool_call(
        ("get_recent_feedback", {"days": 7}),
        ("search_knowledge", {"query": "定积分"}),
    )

    # 3 轮都请求工具：前两轮用满 4 次预算，第 3 轮的请求被拒绝 -> 触发收尾
    fake, seen = scripted([two_tools] * 3)
    with stub_llm(fake):
        result = agent_loop.run_agent("随便问", verbose=False)

    check("MAX_TOOL_CALLS 上限生效", result["tool_calls"] == agent_loop.MAX_TOOL_CALLS, f"tool_calls={result['tool_calls']}")
    check("超出预算的工具没有被执行", [s["tool"] for s in result["steps"]] == ["get_recent_feedback", "search_knowledge"] * 2)
    check("预算耗尽后使用无工具收尾", result["wrap_up_used"] is True and result["ok"] is True)
    check("收尾调用没有再发工具请求", result["stopped_reason"] == "final_answer")

    # 收尾也拿不到答案时，才以预算超限结束
    fake, seen = scripted([two_tools] * 3, fallback={"role": "assistant", "content": ""})
    with stub_llm(fake):
        result = agent_loop.run_agent("随便问", verbose=False)

    check("收尾无答案时以预算超限结束", result["stopped_reason"] == "tool_budget_exceeded", f"reason={result['stopped_reason']}")
    check("预算超限时 ok=False", result["ok"] is False and result["final_answer"] is None)

    # 1.7 MAX_LOOP_TURNS 生效
    one_tool = tool_call("get_recent_feedback", {"days": 7})

    fake, seen = scripted([one_tool] * 3)
    with stub_llm(fake):
        result = agent_loop.run_agent("随便问", verbose=False)

    check("MAX_LOOP_TURNS 上限生效", result["loop_turns"] == agent_loop.MAX_LOOP_TURNS, f"turns={result['loop_turns']}")
    check("轮次用尽前每次都执行了工具", result["tool_calls"] == agent_loop.MAX_LOOP_TURNS)
    check("轮次耗尽后同样有收尾回答", result["wrap_up_used"] is True and result["ok"] is True)

    fake, seen = scripted([one_tool] * 3, fallback={"role": "assistant", "content": ""})
    with stub_llm(fake):
        result = agent_loop.run_agent("随便问", verbose=False)

    check("收尾无答案时以轮次超限结束", result["stopped_reason"] == "turn_budget_exceeded", f"reason={result['stopped_reason']}")

    # 1.8 工具返回 ok=false：错误必须进入下一轮，模型据此如实回答
    fake, seen = scripted([
        tool_call("search_knowledge", {"query": "定积分"}),
        final("知识检索服务当前不可用，我无法查询知识库。"),
    ])
    with broken_embedding(), stub_llm(fake):
        result = agent_loop.run_agent("知识库里定积分怎么说？", verbose=False)

    failed_payload = json.loads(tool_messages(result)[0]["content"])
    check(
        "工具错误进入历史",
        failed_payload["ok"] is False
        and failed_payload["error_type"] == "embedding_service_unavailable",
        f"payload={failed_payload}",
    )
    check("模型据此说明服务不可用", "不可用" in result["final_answer"])

    # 1.9 工具抛异常不会让 Loop 崩溃
    original_tools = dict(agent_dispatcher.TOOLS)

    def exploding(**kwargs):
        raise RuntimeError("模拟工具崩溃")

    agent_dispatcher.TOOLS["get_recent_feedback"] = exploding
    try:
        fake, seen = scripted([
            tool_call("get_recent_feedback", {"days": 7}),
            final("工具出错了，我无法获取历史数据。"),
        ])
        with stub_llm(fake):
            result = agent_loop.run_agent("看看最近情况", verbose=False)
    finally:
        agent_dispatcher.TOOLS.clear()
        agent_dispatcher.TOOLS.update(original_tools)

    check("工具异常被捕获", result["steps"][0]["ok"] is False and result["steps"][0]["error_type"] == "tool_failed")
    check("异常后仍然完成回答", result["stopped_reason"] == "final_answer" and "工具出错" in result["final_answer"])

    # 1.10 未注册 Tool 被拒绝，且不产生任何写操作
    rows_before = database.get_feedback_list()
    fake, seen = scripted([
        tool_call("save_plan", {"plan": {"task": "x"}}),
        final("我不能保存学习计划。"),
    ])
    with stub_llm(fake):
        result = agent_loop.run_agent("帮我保存计划", verbose=False)

    check(
        "未注册工具被拒绝",
        result["steps"][0]["ok"] is False and result["steps"][0]["error_type"] == "unknown_tool",
        f"error_type={result['steps'][0]['error_type']}",
    )
    check("拒绝信息进入历史", "未注册的工具" in tool_messages(result)[0]["content"])
    check("未注册工具没有造成写操作", database.get_feedback_list() == rows_before)

    # 1.11 LLM 调用失败
    def failing_llm(messages, tools=None):
        return None, "无法连接 Ollama（ConnectionError）"

    with stub_llm(failing_llm):
        result = agent_loop.run_agent("随便问", verbose=False)

    check("LLM 失败时停止", result["stopped_reason"] == "llm_error" and result["ok"] is False)
    check("LLM 失败时给出错误信息", "Ollama" in result["error"])

    # 1.12 模型空响应
    fake, seen = scripted([{"role": "assistant", "content": "   "}])
    with stub_llm(fake):
        result = agent_loop.run_agent("随便问", verbose=False)

    check("空响应被识别", result["stopped_reason"] == "empty_response" and result["ok"] is False)

    # 1.13 开发模式日志可观测
    fake, seen = scripted([
        tool_call("get_recent_feedback", {"days": 7}),
        final("最终回答"),
    ])
    buffer = io.StringIO()
    with stub_llm(fake), contextlib.redirect_stdout(buffer):
        agent_loop.run_agent("看看最近情况", verbose=True)

    log = buffer.getvalue()
    for marker in ("[Agent]", "User:", "Decision:", "[Tool]", "Arguments:", "Result:", "Final Answer:"):
        check(f"日志包含 {marker}", marker in log)

    # 1.14 Tool 面与 Schema 一致（不会调用未注册工具）
    check(
        "Dispatcher 白名单 = Schema 工具",
        set(agent_dispatcher.TOOLS) == set(agent_schema.TOOL_NAMES),
        f"dispatcher={sorted(agent_dispatcher.TOOLS)}",
    )
    check("save_plan 不在任何工具面", "save_plan" not in agent_dispatcher.TOOLS and "save_plan" not in agent_schema.TOOL_NAMES)

    # 1.15 只读：脚本化整段跑完后数据库与知识库没变
    seed_feedback()
    rows_before = database.get_feedback_list()
    knowledge_before = sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    )
    fake, seen = scripted([
        tool_call("get_recent_feedback", {"days": 7}),
        tool_call("search_knowledge", {"query": "定积分"}),
        final("总结"),
    ])
    with stub_llm(fake):
        agent_loop.run_agent("综合看看", verbose=False)

    check("Loop 不写数据库", database.get_feedback_list() == rows_before)
    check(
        "Loop 不修改知识库",
        sorted(
            (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
            for path in KNOWLEDGE_DIR.rglob("*.txt")
        ) == knowledge_before,
    )

    # ==========================
    # 2. 真实模型场景（端到端）
    # ==========================

    try:
        embedding_service.get_embedding("前置检查")
    except embedding_service.EmbeddingError as error:
        raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

    seed_feedback()

    def run_real(user_message):
        result = agent_loop.run_agent(user_message, verbose=False)

        check(
            "真实运行没有 LLM 错误",
            result["stopped_reason"] != "llm_error",
            f"error={result['error']}",
        )
        check("真实运行有最终回答", bool(result["final_answer"]), f"reason={result['stopped_reason']}")
        check(
            "真实运行只用了白名单工具",
            all(step["tool"] in agent_dispatcher.TOOLS for step in result["steps"]),
            f"tools={[step['tool'] for step in result['steps']]}",
        )

        info("真实运行轨迹", f"用户={user_message!r} tools={[step['tool'] for step in result['steps']]} "
                           f"turns={result['loop_turns']} reason={result['stopped_reason']}")

        return result

    # 场景 A：只需要历史反馈
    scenario_a = run_real("看看我最近 7 天的学习情况。")
    check(
        "场景 A 调用了 get_recent_feedback",
        "get_recent_feedback" in [step["tool"] for step in scenario_a["steps"]],
        f"tools={[step['tool'] for step in scenario_a['steps']]}",
    )
    check(
        "场景 A 没有调用 search_knowledge",
        "search_knowledge" not in [step["tool"] for step in scenario_a["steps"]],
        f"tools={[step['tool'] for step in scenario_a['steps']]}",
    )

    # 场景 B（原始表述）：模型自己决定要不要查知识库
    scenario_b = run_real("我最近定积分总学不完，你分析一下原因，并告诉我接下来应该怎么学。")
    info("场景 B 模型自选工具", f"tools={[step['tool'] for step in scenario_b['steps']]}")

    # 场景 B2（明确要求结合知识库）：必须出现双工具链
    scenario_b2 = run_real("先看看我最近 7 天的学习情况，再结合知识库告诉我定积分接下来应该怎么学。")
    tools_b2 = [step["tool"] for step in scenario_b2["steps"]]
    check(
        "场景 B2 出现双工具链",
        tools_b2[:2] == ["get_recent_feedback", "search_knowledge"],
        f"tools={tools_b2}",
    )
    check("场景 B2 两条 Tool Result 都进入历史", len(tool_messages(scenario_b2)) == 2)

    # 场景 C：不需要工具的问题（模型可能仍选择检索，这里只断言运行结构）
    scenario_c = run_real("什么是 Embedding？请用一句话回答。")
    info("场景 C 是否调用工具", f"tools={[step['tool'] for step in scenario_c['steps']]}")

    # 场景 D：检索服务不可用，Agent 必须如实说明
    with broken_embedding():
        scenario_d = agent_loop.run_agent(
            "先看看我最近 7 天的学习情况，再结合知识库告诉我定积分接下来应该怎么学。",
            verbose=False,
        )

    check("场景 D 有最终回答", bool(scenario_d["final_answer"]), f"reason={scenario_d['stopped_reason']}")

    search_steps = [step for step in scenario_d["steps"] if step["tool"] == "search_knowledge"]
    check("场景 D 触发了知识检索", len(search_steps) == 1, f"tools={[step['tool'] for step in scenario_d['steps']]}")
    check(
        "场景 D 检索失败信息进入历史",
        bool(search_steps)
        and search_steps[0]["result"]["ok"] is False
        and search_steps[0]["result"]["error_type"] == "embedding_service_unavailable",
        f"result={search_steps[0]['result'] if search_steps else None}",
    )
    failed_tool_payloads = [json.loads(m["content"]) for m in tool_messages(scenario_d)]
    check(
        "场景 D 历史里的工具结果含失败标记",
        any(payload.get("error_type") == "embedding_service_unavailable" for payload in failed_tool_payloads),
    )
    check(
        "场景 D 如实告知服务不可用",
        any(keyword in scenario_d["final_answer"] for keyword in ("不可用", "无法", "失败", "服务")),
        f"answer={scenario_d['final_answer'][:80]}",
    )
    info("场景 D 回答", scenario_d["final_answer"][:120].replace("\n", " "))
finally:
    database.DATABASE_PATH = PROJECT_DIR / "feedback.db"
    vector_cache.CACHE_DIR = PROJECT_DIR / ".rag_cache"
    rag_service.reset_cache()

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
check("真实 feedback.db 未被修改", (REAL_DB.read_bytes() if REAL_DB.exists() else None) == REAL_DB_BYTES)
check("真实 .rag_cache 状态未变", REAL_CACHE.exists() == REAL_CACHE_EXISTED)
check(
    "knowledge/ 未被修改",
    sorted(
        (path.relative_to(KNOWLEDGE_DIR).as_posix(), path.stat().st_size)
        for path in KNOWLEDGE_DIR.rglob("*.txt")
    ) == KNOWLEDGE_SNAPSHOT,
)

print("\n全部用例通过")
