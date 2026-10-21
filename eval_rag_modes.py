"""RAG 检索模式对比评测：vector（纯向量基线）vs hybrid（BM25 关键词 + RRF 融合）。

用法：
    python eval_rag_modes.py                      # 两种模式跑完 rag_eval_labels.json 全部已核验用例
    python eval_rag_modes.py --mode hybrid        # 只跑一种模式
    python eval_rag_modes.py --labels rag_eval_labels.json --out rag_mode_eval_results.json

指标定义（只对 verified=true 的用例计算）：
    Recall@K : Top-K 结果中出现任一 expected_source 记 1，否则 0，求平均
    MRR      : 首次命中 expected_source 的排名倒数（按内部召回列表计算），未命中 0，求平均
    P@K      : Top-K 中命中期望来源的结果占比，求平均
    空返回率 : expect_empty=true 的用例中检索结果为空的比例（越高越好）
    延迟     : 单次 retrieve_chunks 的 wall time

诚实性约定：
    - 只输出真实跑出来的数字；数据/服务缺失打印 "MISSING: ..." 并以非零码退出，绝不填充占位数字
    - 样本量小（< 20）时明确标注 small sample
    - 两种模式用同一份标注、同一参数跑，原始结果全部写入 --out JSON，可重复执行复核
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LABELS_DEFAULT = PROJECT_DIR / "rag_eval_labels.json"
OUT_DEFAULT = PROJECT_DIR / "rag_mode_eval_results.json"

SMALL_SAMPLE_THRESHOLD = 20
RANK_LIST_LEN = 5
REPORT_KS = (1, 3, 5)

MODES = ("vector", "hybrid")


def fail(message):
    print(f"MISSING: {message}")
    sys.exit(1)


def load_cases(path):
    label_path = Path(path)

    if not label_path.exists():
        fail(f"标注文件不存在：{label_path}")

    data = json.loads(label_path.read_text(encoding="utf-8"))

    verified = []
    skipped = []

    for case in data.get("cases") or []:
        is_verified = (
            case.get("verified") is True
            and isinstance(case.get("query"), str)
            and case["query"].strip()
            and isinstance(case.get("expected_source"), list)
        )

        if is_verified:
            verified.append(case)
        else:
            skipped.append(case.get("id"))

    return verified, skipped


def hit_rank(sources, expected):
    """首次命中期望来源的排名（1 起）；未命中返回 None。"""

    for position, source in enumerate(sources, start=1):
        if source in expected:
            return position

    return None


def evaluate_mode(mode, cases, rank_len):
    """跑一种模式的全部用例，返回 (case_rows, errors)。"""

    os.environ["RAG_RETRIEVAL_MODE"] = mode

    import rag_service  # 延迟导入：确保 RAG_RETRIEVAL_MODE 已生效（retrieval 每次读环境变量）

    rows = []
    errors = []

    for case in cases:
        query = case["query"]
        expected = case.get("expected_source") or []
        expect_empty = bool(case.get("expect_empty"))

        started = time.perf_counter()

        try:
            results = rag_service.retrieve_chunks(query, top_k=rank_len)
        except Exception as error:  # noqa: BLE001 - 评测必须如实记录失败而不是吞掉
            errors.append({"id": case["id"], "mode": mode, "error": f"{type(error).__name__}: {error}"})
            rows.append({
                "id": case["id"],
                "category": case.get("category"),
                "mode": mode,
                "query": query,
                "error": f"{type(error).__name__}: {error}",
            })
            continue

        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        sources = [item["source"] for item in results]
        rank = hit_rank(sources, expected) if expected else None

        rows.append({
            "id": case["id"],
            "category": case.get("category"),
            "mode": mode,
            "query": query,
            "expect_empty": expect_empty,
            "expected_source": expected,
            "sources": sources,
            "scores": [item["score"] for item in results],
            "score_types": [item.get("score_type", "cosine") for item in results],
            "hit_rank": rank,
            "latency_ms": elapsed_ms,
        })

    return rows, errors


def metrics_for(rows, rank_len):
    """从明细行计算 Recall@K / MRR / P@K / 空返回率 / 延迟。"""

    answered = [row for row in rows if not row.get("expect_empty") and "error" not in row]
    empties = [row for row in rows if row.get("expect_empty") and "error" not in row]

    metrics = {"n_answered": len(answered), "n_expect_empty": len(empties)}

    for k in REPORT_KS:
        if k > rank_len:
            continue

        recalls = []
        precisions = []

        for row in answered:
            top_sources = row["sources"][:k]
            recalls.append(1 if any(source in row["expected_source"] for source in top_sources) else 0)
            precisions.append(
                sum(1 for source in top_sources if source in row["expected_source"]) / k
            )

        metrics[f"recall@{k}"] = round(sum(recalls) / len(recalls), 4) if recalls else None
        metrics[f"precision@{k}"] = round(sum(precisions) / len(precisions), 4) if precisions else None

    rr = []

    for row in answered:
        rank = row.get("hit_rank")
        rr.append(1.0 / rank if rank else 0.0)

    metrics["mrr"] = round(sum(rr) / len(rr), 4) if rr else None

    if empties:
        metrics["empty_accuracy"] = round(
            sum(1 for row in empties if not row["sources"]) / len(empties), 4
        )
    else:
        metrics["empty_accuracy"] = None

    latencies = [row["latency_ms"] for row in rows if "latency_ms" in row]

    if latencies:
        ordered = sorted(latencies)
        p95_index = max(1, int(round(0.95 * len(ordered)))) - 1
        metrics["latency_ms_mean"] = round(statistics.mean(ordered), 2)
        metrics["latency_ms_p50"] = round(statistics.median(ordered), 2)
        metrics["latency_ms_p95"] = round(ordered[p95_index], 2)
    else:
        metrics["latency_ms_mean"] = metrics["latency_ms_p50"] = metrics["latency_ms_p95"] = None

    return metrics


def print_rows(rows):
    for row in rows:
        if "error" in row:
            print(f"  [{row['mode']}] {row['id']} ERROR: {row['error']}")
            continue

        tag = "EMPTY" if row["expect_empty"] else f"rank={row['hit_rank']}"
        status = "ok" if (
            (row["expect_empty"] and not row["sources"])
            or (not row["expect_empty"] and row["hit_rank"] is not None)
        ) else "MISS"

        print(
            f"  [{row['mode']}] {row['id']} {row['category']} {status} {tag} "
            f"latency={row['latency_ms']}ms sources={row['sources']} scores={row['scores']}"
        )


def main():
    parser = argparse.ArgumentParser(description="RAG vector vs hybrid 对比评测")
    parser.add_argument("--labels", default=str(LABELS_DEFAULT))
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    parser.add_argument("--mode", choices=("vector", "hybrid", "both"), default="both")
    parser.add_argument("--rank-len", type=int, default=RANK_LIST_LEN)
    args = parser.parse_args()

    cases, skipped = load_cases(args.labels)

    if not cases:
        fail("没有任何 verified=true 的可用例")

    if skipped:
        print(f"INFO: 跳过未核验用例 {skipped}")

    import embedding_service
    import rag_service

    print(f"embedding 模型: {embedding_service.EMBEDDING_MODEL}")
    print(f"MIN_SCORE={rag_service.MIN_SCORE}  RRF_K={rag_service.RRF_K}  rank_len={args.rank_len}")
    print(f"用例数: {len(cases)}"
          + (f"  [small sample: {len(cases)} < {SMALL_SAMPLE_THRESHOLD}，指标波动大]" if len(cases) < SMALL_SAMPLE_THRESHOLD else ""))
    print()

    modes = MODES if args.mode == "both" else (args.mode,)
    all_rows = {}
    all_errors = []
    all_metrics = {}

    for mode in modes:
        print(f"===== 模式: {mode} =====")
        rows, errors = evaluate_mode(mode, cases, args.rank_len)
        all_rows[mode] = rows
        all_errors.extend(errors)
        all_metrics[mode] = metrics_for(rows, args.rank_len)
        print_rows(rows)
        print()
        print(f"  指标 ({mode}):")
        for key, value in all_metrics[mode].items():
            print(f"    {key} = {value}")
        print()

    if args.mode == "both" and all_metrics.get("vector") and all_metrics.get("hybrid"):
        print("===== 对比观察（仅陈述本次运行数据） =====")
        for key in ("recall@1", "recall@3", "mrr", "empty_accuracy", "latency_ms_p50"):
            v = all_metrics["vector"].get(key)
            h = all_metrics["hybrid"].get(key)
            delta = None
            if isinstance(v, (int, float)) and isinstance(h, (int, float)):
                delta = round(h - v, 4)
            print(f"  {key}: vector={v} hybrid={h} delta={delta}")

        vector_misses = [row["id"] for row in all_rows["vector"]
                         if not row.get("expect_empty") and row.get("hit_rank") is None and "error" not in row]
        hybrid_misses = [row["id"] for row in all_rows["hybrid"]
                         if not row.get("expect_empty") and row.get("hit_rank") is None and "error" not in row]
        print(f"  vector 未命中用例: {vector_misses}")
        print(f"  hybrid 未命中用例: {hybrid_misses}")
        print()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "labels": str(Path(args.labels).name),
        "params": {
            "min_score": rag_service.MIN_SCORE,
            "rrf_k": rag_service.RRF_K,
            "rank_len": args.rank_len,
            "embedding_model": embedding_service.EMBEDDING_MODEL,
        },
        "cases": cases,
        "results": all_rows,
        "metrics": all_metrics,
        "errors": all_errors,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"原始结果已写入: {out_path}")

    if all_errors:
        print(f"WARNING: 有 {len(all_errors)} 条用例执行失败，详见 errors 字段")
        sys.exit(1)


if __name__ == "__main__":
    main()
