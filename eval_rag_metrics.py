"""RAG 检索质量与延迟指标评测脚本（只读工具，不修改任何项目代码）。

指标定义：
    Recall@3 : 在检索 Top-3 的 source 中命中该查询期望来源（expected_source 任一）记 1 分，
               否则 0 分，对全部已核验用例求平均。
    MRR      : 期望来源在 Top-3 中首次命中的排名倒数（第1名 1.0 / 第2名 0.5 / 第3名 ≈0.33），
               未命中 0 分，求平均。
    P95      : 端到端请求耗时的 95 分位数（nearest-rank 法）。

子命令：
    python eval_rag_metrics.py report
        数据盘点：现有归档/埋点/标注里有什么、缺什么（不做任何评测）

    python eval_rag_metrics.py latency [--latest] [--all]
        从 agent_eval_runs/agent_eval_*.json 的 duration_seconds 计算端到端耗时分布
        （用户请求 → Agent 最终回答/草案，含工具调用与模型时间）

    python eval_rag_metrics.py labels-template
        读取 knowledge/ 真实文件，生成 rag_eval_labels.json 模板。
        模板内所有条目 verified=false：你必须人工核对文件内容后，
        填写 query 与 expected_source 并把 verified 改为 true，脚本才会采用。

    python eval_rag_metrics.py rag --labels rag_eval_labels.json [--top-k 3]
        用 rag_service.retrieve_chunks 真实检索（需要 Ollama + bge-m3），
        实时计算 Recall@3 / MRR / 检索延迟。verified=false 的条目跳过并明确提示。

    python eval_rag_metrics.py rag-log --labels rag_eval_labels.json --file rag_metrics.jsonl
        用 rag_service 的埋点日志（JSONL）离线计算 Recall@3 / MRR / 检索延迟。

诚实性约定：
    - 脚本只输出它真实读到/算出的数据。
    - 数据缺失时打印 "MISSING: ..." 并以非零码退出，绝不填充任何占位数字。
    - 样本量过小时（<20）会明确标注 small sample。
"""

import argparse
import glob
import json
import math
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = PROJECT_DIR / "agent_eval_runs"
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
LABELS_DEFAULT = PROJECT_DIR / "rag_eval_labels.json"
RAG_METRICS_LOG = PROJECT_DIR / "rag_metrics.jsonl"
AGENT_LATENCY_LOG = PROJECT_DIR / "agent_latency.jsonl"

SMALL_SAMPLE_THRESHOLD = 20


# ==========================
# 通用工具
# ==========================


def percentile_nearest_rank(sorted_values, pct):
    """nearest-rank 法分位数：输入必须已升序排序。"""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def describe_distribution(values, unit="s"):
    """输出 n / mean / p50 / p95 / max；样本量不足时标注 small sample。"""
    if not values:
        print("MISSING: 没有任何耗时数据可计算")
        return
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n
    p50 = percentile_nearest_rank(ordered, 50)
    p95 = percentile_nearest_rank(ordered, 95)
    note = (
        f"  [small sample: n={n} < {SMALL_SAMPLE_THRESHOLD}，"
        "P95 波动大，仅作参考，建议埋点后累积更多样本]"
        if n < SMALL_SAMPLE_THRESHOLD
        else f"  [n={n}]"
    )
    print(f"    n    = {n}{note}")
    print(f"    mean = {mean:.3f} {unit}")
    print(f"    p50  = {p50:.3f} {unit}")
    print(f"    p95  = {p95:.3f} {unit}")
    print(f"    max  = {ordered[-1]:.3f} {unit}")
    return p95


def load_labels(path):
    """读取标注文件；返回 (verified_cases, skipped_cases)。

    verified=false 或字段缺失的条目一律跳过——脚本绝不猜测期望来源。
    """
    label_path = Path(path)

    if not label_path.exists():
        print(f"MISSING: 标注文件不存在：{label_path}")
        print("        先运行 python eval_rag_metrics.py labels-template 生成模板，")
        print("        人工核对 knowledge/ 文件内容后填写 query / expected_source，")
        print("        并把对应条目的 verified 改为 true。")
        return [], []

    data = json.loads(label_path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []

    verified = []
    skipped = []

    for case in cases:
        ok = (
            case.get("verified") is True
            and isinstance(case.get("query"), str)
            and case["query"].strip()
            and isinstance(case.get("expected_source"), list)
            and len(case["expected_source"]) > 0
            and all(isinstance(s, str) and s.strip() for s in case["expected_source"])
        )

        if ok:
            verified.append(case)
        else:
            skipped.append(case.get("id", "<未编号>"))

    if skipped:
        print(f"跳过 {len(skipped)} 个未核验条目（verified != true 或字段缺失）：{skipped}")

    return verified, skipped


def source_hit(retrieved_sources, expected_sources):
    """命中判断：检索 source 与期望 source 完全相等，或以期望 source 结尾
    （兼容子目录相对路径）。返回首个命中的 1-based 排名，未命中返回 None。
    """
    for rank, retrieved in enumerate(retrieved_sources, start=1):
        for expected in expected_sources:
            if retrieved == expected or retrieved.endswith("/" + expected):
                return rank
    return None


# ==========================
# report：数据盘点（只读，不调模型）
# ==========================


def cmd_report(args):
    print("=" * 70)
    print("数据盘点：当前项目里有哪些可用于指标计算的真实数据")
    print("=" * 70)

    # 1. Eval 归档（端到端耗时来源）
    archives = sorted(ARCHIVE_DIR.glob("agent_eval_*.json"))
    print(f"\n[1] Eval 归档（端到端耗时 duration_seconds 的来源）")
    if not archives:
        print("MISSING: agent_eval_runs/ 下没有任何 agent_eval_*.json")
    else:
        total_cases = 0
        for archive in archives:
            data = json.loads(archive.read_text(encoding="utf-8"))
            cases = data.get("cases") or []
            durations = [
                c.get("duration_seconds")
                for c in cases
                if isinstance(c.get("duration_seconds"), (int, float))
            ]
            total_cases += len(durations)
            print(
                f"    {archive.name}: {len(durations)} 条耗时"
                f"（范围 {min(durations):.1f}s ~ {max(durations):.1f}s）"
                if durations
                else f"    {archive.name}: 0 条耗时"
            )
        print(
            f"    => 可用：P95 端到端延迟可从归档真实计算"
            f"（共 {len(archives)} 份归档 / {total_cases} 条耗时）。"
            "运行 python eval_rag_metrics.py latency"
        )

    # 2. 检索埋点日志（Recall@3 / MRR 的离线来源）
    print(f"\n[2] 检索埋点日志 rag_metrics.jsonl（Recall@3 / MRR / 检索延迟来源）")
    if RAG_METRICS_LOG.exists():
        lines = [
            line
            for line in RAG_METRICS_LOG.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print(f"    存在：{len(lines)} 条检索记录")
        print("    => 可用：运行 python eval_rag_metrics.py rag-log ...")
    else:
        print(
            "MISSING: rag_metrics.jsonl 不存在。归档 JSON 未记录检索来源明细，\n"
            "        因此 Recall@3 / MRR 目前无法从现有数据计算。\n"
            "        需要先：1) 按 README/说明在 rag_service.retrieve_chunks 加埋点；\n"
            "                2) 生成并核验 rag_eval_labels.json 标注。"
        )

    # 3. Agent 端到端埋点日志
    print(f"\n[3] Agent 端到端埋点日志 agent_latency.jsonl")
    if AGENT_LATENCY_LOG.exists():
        print(f"    存在：{AGENT_LATENCY_LOG.read_text(encoding='utf-8').count(chr(10))} 条记录")
    else:
        print("MISSING: agent_latency.jsonl 不存在（可选：在 agent_loop._result 加埋点）")

    # 4. 标注文件
    print(f"\n[4] 人工标注 rag_eval_labels.json（Recall@3 / MRR 的期望来源）")
    if LABELS_DEFAULT.exists():
        verified, skipped = load_labels(LABELS_DEFAULT)
        print(f"    存在：已核验 {len(verified)} 条 / 跳过 {len(skipped)} 条")
        if len(verified) < 5:
            print(
                f"    WARNING: 已核验条目少于 5 条，Recall@3 / MRR 统计意义有限，"
                f"建议基于 knowledge/ 内容补充到 10 条以上"
            )
        else:
            print("    => 可用：运行 python eval_rag_metrics.py rag --labels ...")
    else:
        print(
            "MISSING: rag_eval_labels.json 不存在。\n"
            "        Recall@3 / MRR 需要每个查询的期望来源（人工标注），\n"
            "        该标注目前不存在，脚本不会猜测。\n"
            "        运行 python eval_rag_metrics.py labels-template 生成模板。"
        )

    # 5. 知识库
    print(f"\n[5] 知识库（标注的依据）")
    knowledge_files = sorted(KNOWLEDGE_DIR.rglob("*.txt"))
    if not knowledge_files:
        print("MISSING: knowledge/ 下没有 txt 文件")
    else:
        for path in knowledge_files:
            relative = path.relative_to(KNOWLEDGE_DIR).as_posix()
            print(f"    {relative}（{path.stat().st_size} 字节）—— 标注时先阅读其内容")

    print("\n结论：缺什么、怎么补，见上方 MISSING 项。")
    return 0


# ==========================
# latency：从归档计算端到端 P95
# ==========================


def collect_archive_durations(archive_paths):
    """从归档 JSON 收集 duration_seconds 与用例分类。"""
    records = []

    for archive in archive_paths:
        data = json.loads(Path(archive).read_text(encoding="utf-8"))
        started_at = data.get("started_at", "?")

        for case in data.get("cases") or []:
            duration = case.get("duration_seconds")
            if not isinstance(duration, (int, float)):
                continue

            observed = (case.get("evaluation") or {}).get("observed") or {}
            case_type = observed.get("type")
            kind = "plan_draft" if case_type == "plan_draft" else "chat"

            records.append(
                {
                    "archive": Path(archive).name,
                    "started_at": started_at,
                    "case_id": case.get("case_id"),
                    "kind": kind,
                    "duration": duration,
                    "hard_failures": len(
                        (case.get("evaluation") or {}).get("hard_failures") or []
                    ),
                }
            )

    return records


def cmd_latency(args):
    archives = sorted(ARCHIVE_DIR.glob("agent_eval_*.json"))

    if not archives:
        print("MISSING: agent_eval_runs/ 下没有任何 agent_eval_*.json，无法计算 P95")
        return 1

    if args.latest:
        archives = archives[-1:]
        print(f"模式：仅使用最新归档 {archives[0].name}")
    else:
        print(f"模式：合并 {len(archives)} 份归档（--latest 可只用最新一份）")

    records = collect_archive_durations(archives)

    if not records:
        print("MISSING: 归档里没有 duration_seconds 数据")
        return 1

    print("\n== 端到端耗时（用户请求 → Agent 最终回答/草案）==")
    print("口径说明：agent_eval.run_case 用 time.time() 包裹整个 runner 调用，")
    print("包含工具调用、模型推理与（草案用例的）修正轮次；不含 FastAPI 序列化开销。\n")

    all_durations = [r["duration"] for r in records]
    print(f"[全部用例]")
    describe_distribution(all_durations)

    for kind in ("chat", "plan_draft"):
        subset = [r["duration"] for r in records if r["kind"] == kind]
        if subset:
            print(f"\n[{kind} 用例]")
            describe_distribution(subset)

    failed = [r for r in records if r["hard_failures"] > 0]
    if failed:
        print(
            f"\nNOTE: 其中 {len(failed)} 条用例带硬性失败（其耗时已包含在上述分布中），"
            f"可用 --ok-only 排除"
        )

    ok_only = [r["duration"] for r in records if r["hard_failures"] == 0]
    if args.ok_only and ok_only and len(ok_only) != len(all_durations):
        print(f"\n[仅无硬失败的用例]")
        describe_distribution(ok_only)

    if args.detail:
        print("\n[逐条明细]")
        for r in records:
            print(
                f"    {r['started_at']}  {r['case_id']:<32} {r['kind']:<10} "
                f"{r['duration']:>7.2f}s  hard_failures={r['hard_failures']}"
            )

    return 0


# ==========================
# labels-template：从真实知识库生成标注模板
# ==========================


def cmd_labels_template(args):
    knowledge_files = sorted(KNOWLEDGE_DIR.rglob("*.txt"))

    if not knowledge_files:
        print("MISSING: knowledge/ 下没有 txt 文件，无法生成标注模板")
        return 1

    cases = []
    for index, path in enumerate(knowledge_files, start=1):
        relative = path.relative_to(KNOWLEDGE_DIR).as_posix()
        content = path.read_text(encoding="utf-8", errors="replace")
        preview = content[:200].replace("\n", " ")

        cases.append(
            {
                "id": f"R{index}",
                "query": "<TODO：写一个真实学习问题，答案应能在该文件中找到>",
                "expected_source": [relative],
                "verified": False,
                "_文件内容预览": preview,
            }
        )

    template = {
        "_说明": (
            "expected_source 必须先人工阅读 knowledge/ 对应文件内容后核对；"
            "把 verified 改为 true 的条目才会参与 Recall@3 / MRR 计算。"
            "未核验条目会被脚本跳过，绝不参与统计。"
        ),
        "cases": cases,
    }

    if LABELS_DEFAULT.exists():
        print(f" refuse: {LABELS_DEFAULT} 已存在，不覆盖（请手工编辑或删除后重试）")
        return 1

    LABELS_DEFAULT.write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已生成标注模板：{LABELS_DEFAULT}")
    print("下一步：")
    print("  1) 打开 knowledge/ 下每个文件，阅读内容")
    print("  2) 为每个文件写 3~5 个真实学习问题（query）")
    print("  3) 确认 expected_source 指向的文件确实能回答该问题")
    print("  4) 把核对完成的条目 verified 改为 true（未核验条目不参与统计）")
    return 0


# ==========================
# rag：真实检索计算 Recall@3 / MRR
# ==========================


def compute_rag_metrics(rows, top_k):
    """输入：[{id, query, expected_source, retrieved_sources, latency_ms}]。
    输出：(recall3, mrr, per_case 明细列表)。命中判定见 source_hit。
    """
    per_case = []
    recall_scores = []
    rr_scores = []

    for row in rows:
        rank = source_hit(row["retrieved_sources"], row["expected_source"])
        recall = 1 if rank is not None else 0
        rr = (1.0 / rank) if rank is not None else 0.0

        recall_scores.append(recall)
        rr_scores.append(rr)
        per_case.append({**row, "rank": rank, "recall": recall, "rr": round(rr, 4)})

    recall3 = sum(recall_scores) / len(recall_scores) if recall_scores else None
    mrr = sum(rr_scores) / len(rr_scores) if rr_scores else None

    return recall3, mrr, per_case


def print_rag_result(recall3, mrr, per_case, latency_note):
    print("\n[逐条结果]")
    for case in per_case:
        rank_text = f"rank={case['rank']}" if case["rank"] else "未命中"
        retrieved = ", ".join(case["retrieved_sources"]) or "<无结果>"
        print(
            f"    {case['id']:<6} {rank_text:<10} recall={case['recall']} "
            f"rr={case['rr']:.4f}  期望={case['expected_source']}  检索=[{retrieved}]"
        )

    print("\n[汇总]")
    n = len(per_case)
    print(f"    已核验用例数 n = {n}")
    if n < SMALL_SAMPLE_THRESHOLD:
        print(
            f"    [small sample: n={n} < {SMALL_SAMPLE_THRESHOLD}，"
            "指标波动大，仅作参考]"
        )
    print(f"    Recall@{3} = {recall3:.4f}")
    print(f"    MRR       = {mrr:.4f}")

    if latency_note:
        print(latency_note)


def cmd_rag(args):
    verified, _ = load_labels(args.labels)

    if not verified:
        print("MISSING: 没有 verified=true 的标注条目，无法计算 Recall@3 / MRR（不猜测）")
        return 1

    # 延迟导入：report/labels-template 模式不需要 Ollama
    try:
        import rag_service
    except Exception as error:
        print(f"MISSING: 无法导入 rag_service：{error}")
        return 1

    rows = []
    embedding_failed = None

    for case in verified:
        query = case["query"].strip()
        started = time.perf_counter()

        try:
            chunks = rag_service.retrieve_chunks(query, top_k=args.top_k)
        except Exception as error:  # EmbeddingError 等一律如实报告，不编造
            embedding_failed = f"查询 {case['id']} 检索失败：{type(error).__name__}: {error}"
            break

        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        rows.append(
            {
                "id": case["id"],
                "query": query,
                "expected_source": case["expected_source"],
                "retrieved_sources": [chunk.get("source") for chunk in chunks],
                "latency_ms": latency_ms,
            }
        )

    if embedding_failed:
        print(f"MISSING: {embedding_failed}")
        print("        检索服务不可用时无法得到真实指标，不输出任何数字。")
        print("        请确认 Ollama 已启动且已 ollama pull bge-m3。")
        return 2

    recall3, mrr, per_case = compute_rag_metrics(rows, args.top_k)

    latencies = [r["latency_ms"] for r in rows]
    latency_note = (
        f"    检索延迟（仅检索环节，非端到端）：mean={sum(latencies)/len(latencies):.1f}ms, "
        f"p95={percentile_nearest_rank(sorted(latencies), 95):.1f}ms"
    )

    print_rag_result(recall3, mrr, per_case, latency_note)
    return 0


# ==========================
# rag-log：从埋点 JSONL 离线计算
# ==========================


def cmd_rag_log(args):
    verified, _ = load_labels(args.labels)

    if not verified:
        print("MISSING: 没有 verified=true 的标注条目，无法计算（不猜测）")
        return 1

    log_path = Path(args.file)

    if not log_path.exists():
        print(f"MISSING: 埋点日志不存在：{log_path}")
        print(
            "        先在 rag_service.retrieve_chunks 中加入埋点（输出 JSONL："
            "query / sources / scores / latency_ms），累积真实检索记录后再计算。"
        )
        return 1

    logged = {}

    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue

        query = record.get("query")
        sources = record.get("sources")

        if isinstance(query, str) and isinstance(sources, list):
            logged.setdefault(query.strip(), sources)  # 首次出现为准

    rows = []
    for case in verified:
        query = case["query"].strip()

        if query not in logged:
            continue

        rows.append(
            {
                "id": case["id"],
                "query": query,
                "expected_source": case["expected_source"],
                "retrieved_sources": logged[query][: args.top_k],
                "latency_ms": None,
            }
        )

    missing = [c["id"] for c in verified if c["query"].strip() not in logged]

    if missing:
        print(f"WARNING: {len(missing)} 条已核验查询在埋点日志中没有检索记录：{missing}")

    if not rows:
        print("MISSING: 标注查询与埋点日志无任何交集，无法计算（不猜测）")
        return 1

    recall3, mrr, per_case = compute_rag_metrics(rows, args.top_k)
    print_rag_result(recall3, mrr, per_case, latency_note=None)
    return 0


# ==========================
# 入口
# ==========================


def main(argv=None):
    parser = argparse.ArgumentParser(description="RAG / 延迟指标评测（只读真实数据，缺数据时报 MISSING）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("report", help="盘点现有数据：有什么、缺什么")

    latency = sub.add_parser("latency", help="从 eval 归档计算端到端耗时分布（P95 等）")
    latency.add_argument("--latest", action="store_true", help="只使用最新一份归档")
    latency.add_argument("--all", action="store_true", help="合并全部归档（默认）")
    latency.add_argument("--ok-only", action="store_true", help="额外输出无硬失败用例的分布")
    latency.add_argument("--detail", action="store_true", help="输出逐条明细")

    sub.add_parser("labels-template", help="根据 knowledge/ 真实文件生成标注模板（verified=false）")

    rag = sub.add_parser("rag", help="真实检索计算 Recall@3 / MRR（需要 Ollama + bge-m3）")
    rag.add_argument("--labels", default=str(LABELS_DEFAULT))
    rag.add_argument("--top-k", type=int, default=3)

    rag_log = sub.add_parser("rag-log", help="从埋点 JSONL 离线计算 Recall@3 / MRR")
    rag_log.add_argument("--labels", default=str(LABELS_DEFAULT))
    rag_log.add_argument("--file", default=str(RAG_METRICS_LOG))
    rag_log.add_argument("--top-k", type=int, default=3)

    args = parser.parse_args(argv)

    if args.command is None:
        return cmd_report(args)

    if args.command == "report":
        return cmd_report(args)
    if args.command == "latency":
        return cmd_latency(args)
    if args.command == "labels-template":
        return cmd_labels_template(args)
    if args.command == "rag":
        return cmd_rag(args)
    if args.command == "rag-log":
        return cmd_rag_log(args)

    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
