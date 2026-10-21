"""原生 Agent vs LangChain 只读 Agent 对照实验（D4）。

用法：
    python agent_compare.py                 # 用内置 3 个只读问题分别跑两种实现
    python agent_compare.py "你的问题"      # 单个自定义只读问题

对每个问题分别运行：
- 原生：agent_loop.run_agent（默认主路径）
- LangChain：langchain_agent.run_langchain_agent（只读实验路径）

记录每次运行的：工具调用（名称+参数）、步骤成败、最终回答、耗时、停止原因、错误。
原始结果写入 agent_compare_results.json，可重复执行复核。

诚实性约定：
- 只输出真实运行结果；任何失败/超时都原样记录，不美化
- 两种实现用同一个问题、同一个本地模型，回答差异如实呈现
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
OUT_DEFAULT = PROJECT_DIR / "agent_compare_results.json"

# 内置只读对照问题（覆盖：历史数据工具 / 知识库工具 / 无需工具）
DEFAULT_QUESTIONS = [
    "我最近 7 天的学习情况怎么样？",
    "知识库里关于定积分的学习步骤说了什么？",
    "用一句话解释什么是 Embedding。",
]


def run_native(question):
    import agent_loop

    started = time.perf_counter()
    result = agent_loop.run_agent(question, verbose=False)
    elapsed = round(time.perf_counter() - started, 3)

    return {
        "framework": "native",
        "ok": result["ok"],
        "final_answer": result["final_answer"],
        "stopped_reason": result["stopped_reason"],
        "error": result["error"],
        "tool_calls": result["tool_calls"],
        "steps": [
            {"tool": step["tool"], "arguments": step["arguments"],
             "ok": step["ok"], "error_type": step["error_type"]}
            for step in result["steps"]
        ],
        "elapsed_seconds": elapsed,
    }


def run_langchain(question):
    import langchain_agent

    result = langchain_agent.run_langchain_agent(question)

    return {
        "framework": "langchain",
        "ok": result["ok"],
        "final_answer": result["final_answer"],
        "stopped_reason": result["stopped_reason"],
        "error": result["error"],
        "tool_calls": result["tool_calls"],
        "steps": result["steps"],
        "elapsed_seconds": result["elapsed_seconds"],
    }


def print_run(run):
    status = "ok" if run["ok"] else f"FAIL({run['stopped_reason']})"
    tools = [step.get("tool") for step in run["steps"]]

    print(f"  [{run['framework']}] {status} 耗时={run['elapsed_seconds']}s 工具={tools}")

    if run["error"]:
        print(f"    error: {run['error']}")

    answer = (run["final_answer"] or "").replace("\n", " ")

    if len(answer) > 200:
        answer = answer[:200] + "..."

    print(f"    answer: {answer}")


def main():
    import model_usage
    model_usage.install()

    questions = sys.argv[1:] or DEFAULT_QUESTIONS
    runs = []

    for question in questions:
        print(f"===== 问题: {question} =====")

        for runner in (run_native, run_langchain):
            run = runner(question)
            run["question"] = question
            runs.append(run)
            print_run(run)

        print()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runs": runs,
    }

    OUT_DEFAULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"对照结果已写入: {OUT_DEFAULT}")

    failed = [run for run in runs if not run["ok"]]

    if failed:
        print(f"WARNING: {len(failed)} 次运行未拿到最终回答（已如实记录）")


if __name__ == "__main__":
    main()
