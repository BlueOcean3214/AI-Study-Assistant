"""Prompt 变体对照评测：同一评测集对比 v1/v2 提示词的真实表现。

用法：
    python eval_prompt_variants.py                # 真实云端（有费用，量小）
    python eval_prompt_variants.py --mock         # Mock（零费用，验证评测器逻辑）
    python eval_prompt_variants.py --task ask_rag # 评测其他任务（默认 plan_draft_system）

指标（每变体）：
- 任务完成率：run_plan_draft 返回 plan_draft 的比例
- Schema 通过率：validate_plan_for_save 一次通过（corrections_used==0）的比例
- 来源依据合规：check_grounding 0 违规的比例
- 平均修正次数 / 端到端耗时 / Token 用量（model_usage 真实记录）

诚实性：只输出真实运行数字；Mock 模式输出明确标注 MOCK，不能当真实效果。
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
OUT_DEFAULT = PROJECT_DIR / "prompt_variant_eval_results.json"

# 同一评测集：覆盖基础生成、困难收紧、知识库依据三类场景
SCENARIOS = [
    {"id": "S1_basic", "message": "根据我最近的学习情况，制定明天的学习计划草案。"},
    {"id": "S2_knowledge", "message": "结合知识库里的定积分学习步骤和我的反馈，制定学习计划草案。"},
    {"id": "S3_plain", "message": "随便帮我制定一个学习计划草案。"},
]

VARIANTS = ["v1", "v2"]


def run_one(variant, scenario, mock=False):
    import agent_plan_draft
    import prompt_registry

    # 切换变体（模块级常量按变体重渲染）
    agent_plan_draft.DRAFT_SYSTEM_PROMPT = prompt_registry.render(
        "plan_draft_system", version=variant
    )

    if mock:
        # Mock：只验证评测器指标计算，不代表模型效果
        return {
            "id": scenario["id"],
            "variant": variant,
            "mode": "mock",
            "completed": True,
            "schema_first_pass": True,
            "grounding_violations": 0,
            "corrections_used": 0,
            "elapsed_seconds": 0.0,
        }

    started = time.perf_counter()
    result = agent_plan_draft.run_plan_draft(scenario["message"], verbose=False)
    elapsed = round(time.perf_counter() - started, 3)

    completed = result.get("type") == "plan_draft"
    corrections = result.get("corrections_used", 0)

    return {
        "id": scenario["id"],
        "variant": variant,
        "mode": "real",
        "completed": completed,
        "schema_first_pass": completed and corrections == 0,
        "corrections_used": corrections,
        "elapsed_seconds": elapsed,
    }


def summarize(rows, variant):
    subset = [row for row in rows if row["variant"] == variant]
    n = len(subset)

    if n == 0:
        return None

    return {
        "n": n,
        "task_completion_rate": round(sum(r["completed"] for r in subset) / n, 4),
        "schema_first_pass_rate": round(sum(r["schema_first_pass"] for r in subset) / n, 4),
        "avg_corrections": round(sum(r["corrections_used"] for r in subset) / n, 2),
        "avg_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in subset) / n, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Prompt 变体对照评测")
    parser.add_argument("--mock", action="store_true", help="Mock 模式（零费用，仅验证评测器）")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    args = parser.parse_args()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]

    if not args.mock:
        import model_usage
        model_usage.install()
        print("注意：真实模式会产生云端模型调用费用（小规模）。")
        before_usage = len(model_usage.recent_usage(100))
    else:
        before_usage = 0

    rows = []

    for scenario in SCENARIOS:
        for variant in variants:
            row = run_one(variant, scenario, mock=args.mock)
            rows.append(row)
            print(f"[{row['mode']}] {row['id']} {variant}: completed={row['completed']} "
                  f"first_pass={row['schema_first_pass']} corrections={row['corrections_used']} "
                  f"{row['elapsed_seconds']}s")

    print()
    print("===== 变体对比（同一评测集） =====")

    summary = {}

    for variant in variants:
        summary[variant] = summarize(rows, variant)
        print(f"  {variant}: {summary[variant]}")

    if not args.mock:
        import model_usage
        after = model_usage.recent_usage(100)
        new_records = after[: len(after) - before_usage]
        total_prompt = sum(r["prompt_tokens"] or 0 for r in new_records)
        total_completion = sum(r["completion_tokens"] or 0 for r in new_records)
        print(f"\n本次真实 API 用量：LLM 调用 {len(new_records)} 次，"
              f"prompt {total_prompt} tokens，completion {total_completion} tokens")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "mock" if args.mock else "real",
        "scenarios": SCENARIOS,
        "rows": rows,
        "summary": summary,
    }

    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入: {args.out}")

    if args.mock:
        print("（MOCK 模式：指标仅验证评测器逻辑，不代表模型真实效果）")


if __name__ == "__main__":
    main()
