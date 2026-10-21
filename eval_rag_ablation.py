"""深度 RAG 消融评测：同一标注集对比四种检索配置。

配置：
    vector            纯向量（基线）
    hybrid            向量 + BM25 + RRF
    vector+rerank     纯向量 + 第二阶段重排
    hybrid+rerank     混合检索 + 第二阶段重排

指标（口径见 eval_metrics.py：source 级二元标签，不混用）：
    Recall@K（来源覆盖度）/ source_entry_precision@K（来源标签判定的条目精确率）
    MRR / NDCG@K（来源去重二元增益）
    空返回正确性（expect_empty 单列，不计入质量指标）/ 检索延迟
    按 query category 分组报告（防止总体平均掩盖失败场景）

用法：
    # 合成语料（独立于生产 knowledge/，推荐做对照实验）
    python eval_rag_ablation.py --corpus eval_fixtures/rag_eval_corpus.json
    # 生产知识库标注（旧入口，兼容保留）
    python eval_rag_ablation.py --labels rag_eval_labels.json
    # 用确定性 mock 重排验证评测器（不代表真实效果）
    python eval_rag_ablation.py --corpus eval_fixtures/rag_eval_corpus.json --mock-rerank

诚实性：只输出真实运行数字；样本量小标注 small sample；rerank 无真实 Key 时该组合
明确标记 skipped（未验证），绝不虚构效果；Mock 重排结果明确标注 MOCK。
"""

import argparse
import contextlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

import eval_metrics

PROJECT_DIR = Path(__file__).resolve().parent
LABELS_DEFAULT = PROJECT_DIR / "rag_eval_labels.json"
CORPUS_DEFAULT = PROJECT_DIR / "eval_fixtures" / "rag_eval_corpus.json"
OUT_DEFAULT = PROJECT_DIR / "rag_ablation_results.json"

CONFIGS = ["vector", "hybrid", "vector+rerank", "hybrid+rerank"]
SMALL_SAMPLE_THRESHOLD = 20


def load_cases(path):
    """加载标注（生产 labels 或合成 corpus），返回 (cases, documents)。

    - cases 只取 verified=true；expect_empty 用例由 eval_metrics 单独统计
    - documents 非空表示合成语料（评测时替换检索输入，不动生产 knowledge/）
    """

    data = json.loads(Path(path).read_text(encoding="utf-8"))

    cases = [c for c in data.get("cases") or [] if c.get("verified") is True]
    documents = data.get("documents") or []

    return cases, documents


@contextlib.contextmanager
def synthetic_corpus(documents):
    """把检索输入替换为合成语料（只影响评测进程，退出恢复）。

    兼容两种文档结构：
    - chunks: [{"chunk_id", "content"}]   显式分块（chunk 级评测）
    - content: "..."                      整文档（旧结构，仅 source 级评测）
    """

    if not documents:
        yield
        return

    import rag_service

    original = rag_service.load_chunks

    def load_synthetic():
        chunks = []

        for doc in documents:
            if doc.get("chunks"):
                for chunk in doc["chunks"]:
                    chunks.append({
                        "content": chunk["content"],
                        "source": doc["source"],
                        "chunk_id": chunk["chunk_id"],
                    })
            else:
                chunks.append({"content": doc["content"], "source": doc["source"]})

        return chunks

    rag_service.load_chunks = load_synthetic

    try:
        yield
    finally:
        rag_service.load_chunks = original


def chunk_id_map(documents):
    """(source, content) -> chunk_id 映射（chunk 级标签匹配用）。"""

    mapping = {}

    for doc in documents:
        for chunk in doc.get("chunks") or []:
            mapping[(doc["source"], chunk["content"])] = chunk["chunk_id"]

    return mapping


def run_retrieval(config, query, top_k, mock_rerank, id_map=None):
    """按配置执行一次检索（+可选重排），返回 (来源列表, chunk_id 列表)。"""

    import rag_service
    import rerank_service

    use_hybrid = config.startswith("hybrid")
    use_rerank = config.endswith("+rerank")

    previous_mode = os.environ.get("RAG_RETRIEVAL_MODE")
    os.environ["RAG_RETRIEVAL_MODE"] = "hybrid" if use_hybrid else "vector"

    mock_original = None

    if use_rerank:
        os.environ["RAG_RERANK_ENABLED"] = "1"

        if mock_rerank:
            mock_original = rerank_service.rerank

            def mock_rank(query_text, candidates, top_n=None):
                ordered = sorted(
                    candidates,
                    key=lambda item: sum(1 for t in [query_text] if t[:2] in item.get("content", "")),
                    reverse=True,
                )
                return {"ok": True, "rerank_applied": True,
                        "results": ordered[: top_n or len(ordered)]}

            rerank_service.rerank = mock_rank

    try:
        results = rag_service.retrieve_chunks(query, top_k=top_k)

        if use_rerank and results:
            outcome = rerank_service.rerank(query, results, top_n=top_k)
            results = outcome.get("results") or results

        sources = [item["source"] for item in results]

        if id_map is None:
            return sources, None

        # chunk_id 映射：合成语料按 (source, content) 精确映射；映射不到记 None
        chunk_ids = [
            id_map.get((item.get("source"), item.get("content")))
            for item in results
        ]

        return sources, chunk_ids
    finally:
        os.environ.pop("RAG_RERANK_ENABLED", None)

        if previous_mode is None:
            os.environ.pop("RAG_RETRIEVAL_MODE", None)
        else:
            os.environ["RAG_RETRIEVAL_MODE"] = previous_mode

        if mock_original is not None:
            rerank_service.rerank = mock_original


def evaluate_config(config, cases, documents, top_k, mock_rerank):
    """跑一种配置的全部用例，返回 (总体指标, 分组指标, 明细)。"""

    id_map = chunk_id_map(documents) if documents else None
    case_results = []
    rows = []
    latencies = []

    with synthetic_corpus(documents):
        for case in cases:
            started = time.perf_counter()
            sources, chunk_ids = run_retrieval(config, case.get("query", ""), top_k, mock_rerank, id_map)
            latencies.append((time.perf_counter() - started) * 1000)

            result = eval_metrics.evaluate_case(case, sources, top_k, chunk_ids)
            case_results.append(result)

            rows.append({
                "id": case.get("id"),
                "category": case.get("category"),
                "kind": result["kind"],
                "sources": sources,
                "chunk_ids": chunk_ids,
            })

    summary = eval_metrics.summarize(case_results)
    summary["latency_ms_p50"] = round(sorted(latencies)[len(latencies) // 2], 1) if latencies else None

    by_category = eval_metrics.summarize_by_category(cases, case_results)

    return summary, by_category, rows


def main():
    parser = argparse.ArgumentParser(description="深度 RAG 消融评测")
    parser.add_argument("--labels", default=str(LABELS_DEFAULT),
                        help="生产知识库标注（旧入口，兼容保留）")
    parser.add_argument("--corpus", default=None,
                        help="合成评测语料（含 documents + cases）；指定后优先于 --labels")
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--mock-rerank", action="store_true",
                        help="用确定性 mock 重排（验证评测器，不代表真实效果）")
    args = parser.parse_args()

    # --corpus 显式指定优先；否则默认用合成语料（若存在），保持旧 --labels 可用
    if args.corpus:
        source_path = args.corpus
    elif Path(CORPUS_DEFAULT).exists() and args.labels == str(LABELS_DEFAULT):
        source_path = str(CORPUS_DEFAULT)
    else:
        source_path = args.labels

    cases, documents = load_cases(source_path)
    print(f"标注来源: {Path(source_path).name}（合成语料={bool(documents)}）")
    print(f"用例数: {len(cases)}"
          + (f"  [small sample: < {SMALL_SAMPLE_THRESHOLD}，指标波动大]" if len(cases) < SMALL_SAMPLE_THRESHOLD else ""))

    has_rerank_key = bool((os.getenv("RERANK_API_KEY") or "").strip())

    summary = {}
    by_category = {}
    skipped = []

    for config in CONFIGS:
        if config.endswith("+rerank") and not has_rerank_key and not args.mock_rerank:
            skipped.append(config)
            summary[config] = {"skipped": True,
                              "reason": "未配置 RERANK_API_KEY（真实重排未验证，未虚构指标）"}
            print(f"===== {config}: SKIPPED（未配置 RERANK_API_KEY）=====")
            continue

        mode_note = "（MOCK 重排，不代表真实效果）" if args.mock_rerank and config.endswith("+rerank") else ""
        print(f"===== {config} {mode_note} =====")

        metrics, grouped, rows = evaluate_config(config, cases, documents, args.top_k, args.mock_rerank)
        summary[config] = metrics
        by_category[config] = grouped

        for key, value in metrics.items():
            print(f"  {key} = {value}")

        print("  -- 分组指标（source 级 / chunk 级） --")

        for category, values in grouped.items():
            print(
                f"    [{category}] "
                f"source_recall={values['source_recall']} source_mrr={values['source_mrr']} "
                f"source_ndcg={values['source_ndcg']} | "
                f"chunk_recall={values['chunk_recall']} chunk_mrr={values['chunk_mrr']} "
                f"chunk_ndcg={values['chunk_ndcg']} | "
                f"empty={values['empty_accuracy']} "
                f"(answered={values['n_answered']} chunked={values['n_chunk_answered']} "
                f"empty={values['n_expect_empty']})"
            )

        print()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "mock-rerank" if args.mock_rerank else "real",
        "labels_source": Path(source_path).name,
        "synthetic_corpus": bool(documents),
        "top_k": args.top_k,
        "n_cases": len(cases),
        "skipped_configs": skipped,
        "metrics_definition": "双口径：source 级二元标签（source_recall=来源覆盖度、source_entry_precision=按来源标签判定的条目精确率、source_ndcg=来源去重二元增益）；chunk 级二元标签（chunk_* 同套公式，仅带 expected_chunks 的用例）；expect_empty/空查询单列（见 eval_metrics.py）",
        "summary": summary,
        "by_category": by_category,
    }

    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"消融结果已写入: {args.out}")

    if skipped:
        print(f"注意：{len(skipped)} 个组合因缺少真实 Rerank 配置被跳过（未验证，未虚构指标）")


if __name__ == "__main__":
    main()
