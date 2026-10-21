"""检索评测指标与标注数据测试（双口径：source 级 + chunk 级）：
直接运行 python test_eval_metrics.py。

纯 Python，零外部依赖、零 API 调用、不依赖 Ollama。

覆盖：
- source 级与 chunk 级口径分离：同源多块不误认多来源；来源命中但分块未命中时两指标体现差异
- 多来源 / 重复结果 / 无相关结果 / 空查询 / 无答案的计算符合文档公式
- NDCG 标准 DCG/IDCG（二元 + 去重，不超 1）；修复前后对比（确定性数据）
- 合成语料完整性（39 条、8 类、chunk_id 全局唯一、expected_chunks 可核验）
- 旧 22 条数据只做来源级评测（不虚构 chunk ID）
- 评测器端到端（确定性检索器，无 Ollama/API）
"""

import json
import math
from pathlib import Path

import eval_metrics
import eval_rag_ablation


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


PROJECT_DIR = Path(__file__).resolve().parent
CORPUS_PATH = PROJECT_DIR / "eval_fixtures" / "rag_eval_corpus.json"

RANKED_SOURCES = ["pycache_errors.txt", "pycache_troubleshoot.txt", "distractor_redis.txt"]
EXPECTED_MULTI = ["pycache_errors.txt", "pycache_troubleshoot.txt"]


def legacy_precision(ranked, expected, k):
    """旧实现（修复前）：只检查 expected_source[0]。"""

    first = expected[0]
    top = ranked[:k]
    return sum(1 for s in top if s == first) / k if top else 0.0


# ==========================
# 1. source 级：多来源与修复前后对比
# ==========================

old_p3 = legacy_precision(RANKED_SOURCES, EXPECTED_MULTI, 3)
new_p3 = eval_metrics.precision_at_k(RANKED_SOURCES, EXPECTED_MULTI, 3)

check("修复前后 source Precision@3 对比（多来源）",
      old_p3 == 1 / 3 and new_p3 == 2 / 3, f"旧={old_p3:.4f} 新={new_p3:.4f}")

check("source_recall 全覆盖 = 1.0",
      eval_metrics.recall_at_k(RANKED_SOURCES, EXPECTED_MULTI, 3) == 1.0)
check("source_recall 半覆盖 = 0.5",
      eval_metrics.recall_at_k(["pycache_errors.txt"], EXPECTED_MULTI, 3) == 0.5)
check("source_recall@K 截断到 K",
      eval_metrics.recall_at_k(["a.txt"], ["a.txt", "b.txt"], 1) == 0.5)
check("source MRR 首次命中排名倒数",
      eval_metrics.reciprocal_rank(["x", "a", "b"], ["a", "b"]) == 0.5)
check("source MRR 无命中 = 0",
      eval_metrics.reciprocal_rank(["x", "y"], ["a"]) == 0.0)

# ==========================
# 2. 口径分离：同源多块 ≠ 多来源；来源命中 ≠ 分块命中
# ==========================

# B3 形状：同一来源两个相关分块都命中
same_source_chunks = ["pycache_config.txt#1", "pycache_config.txt#2", "x.txt"]
case_b3 = {
    "query": "q",
    "expected_source": ["pycache_config.txt"],
    "expected_chunks": ["pycache_config.txt#1", "pycache_config.txt#2"],
}
r = eval_metrics.evaluate_case(case_b3, ["pycache_config.txt", "pycache_config.txt", "x.txt"],
                               3, same_source_chunks)
check("同源多块：source 级只算 1 个来源",
      r["source_recall"] == 1.0 and r["source_entry_precision"] == 2 / 3,
      f"source_recall={r['source_recall']} source_entry_precision={r['source_entry_precision']}")
check("旧键 source_precision 仍为同值别名（兼容）",
      r["source_precision"] == r["source_entry_precision"]
      and eval_metrics.precision_at_k is eval_metrics.source_entry_precision_at_k)
check("同源多块：chunk 级算 2 个相关分块",
      r["chunk_recall"] == 1.0 and r["chunk_precision"] == 2 / 3,
      f"chunk_recall={r['chunk_recall']} chunk_precision={r['chunk_precision']}")

# 来源命中但分块未命中（检索到了正确来源的错误分块）
case_wrong_chunk = {
    "query": "q",
    "expected_source": ["pycache_config.txt"],
    "expected_chunks": ["pycache_config.txt#1"],
}
r2 = eval_metrics.evaluate_case(case_wrong_chunk, ["pycache_config.txt"], 3,
                                ["pycache_config.txt#2"])
check("来源命中但分块未命中：source_recall=1.0 且 chunk_recall=0.0",
      r2["source_recall"] == 1.0 and r2["chunk_recall"] == 0.0 and r2["chunk_rr"] == 0.0,
      f"source_recall={r2['source_recall']} chunk_recall={r2['chunk_recall']}")

# 两指标独立：chunk 命中一半
r3 = eval_metrics.evaluate_case(case_b3, ["pycache_config.txt", "pycache_config.txt"], 3,
                                ["pycache_config.txt#1", "x.txt"])
check("chunk_recall 反映部分分块命中（0.5）",
      r3["chunk_recall"] == 0.5 and r3["source_recall"] == 1.0,
      f"chunk_recall={r3['chunk_recall']}")

# ==========================
# 3. chunk 级公式（多来源/重复/空）
# ==========================

case_multi = {
    "query": "q",
    "expected_source": ["a.txt", "b.txt"],
    "expected_chunks": ["a.txt#1", "b.txt#1", "b.txt#2"],
}
r4 = eval_metrics.evaluate_case(case_multi, ["a.txt", "b.txt", "x.txt"], 3,
                                ["a.txt#1", "b.txt#2", "x.txt#9"])
check("chunk 多分块部分命中（1/3 与 2/3）",
      r4["chunk_recall"] == 2 / 3 and r4["chunk_precision"] == 2 / 3,
      f"chunk_recall={r4['chunk_recall']} chunk_precision={r4['chunk_precision']}")

# 重复结果：同一 chunk 重复占位
dup_ids = ["a.txt#1", "a.txt#1", "x.txt"]
check("重复 chunk 结果 precision 条目级如实计数",
      eval_metrics.chunk_precision_at_k(dup_ids, ["a.txt#1"], 3) == 2 / 3)
check("重复 chunk 结果 recall 去重（覆盖度 1.0）",
      eval_metrics.chunk_recall_at_k(dup_ids, ["a.txt#1"], 3) == 1.0)
check("重复 chunk NDCG 不重复计增益",
      abs(eval_metrics.chunk_ndcg_at_k(["a.txt#1", "a.txt#1"], ["a.txt#1"], 3) - 1.0) < 1e-9)

# 无相关结果（空检索）
r5 = eval_metrics.evaluate_case(case_multi, [], 3, [])
check("chunk 口径空结果全 0（计入平均）",
      r5["chunk_recall"] == 0.0 and r5["chunk_ndcg"] == 0.0
      and r5["source_recall"] == 0.0)

# NDCG 标准公式（手算）
perfect = eval_metrics.chunk_ndcg_at_k(["a.txt#1", "b.txt#1"], ["a.txt#1", "b.txt#1"], 3)
check("chunk NDCG 理想排名 = 1.0", abs(perfect - 1.0) < 1e-9)
late = eval_metrics.chunk_ndcg_at_k(["x", "y", "a.txt#1"], ["a.txt#1", "b.txt#1"], 3)
check("chunk NDCG 排名靠后 < 1", 0 < late < 1, f"ndcg={late:.4f}")

# ==========================
# 4. 空查询 / 无答案 / 聚合
# ==========================

blank = eval_metrics.evaluate_case({"query": "", "expected_source": ["a.txt"]}, [], 3)
check("空查询单列 skipped", blank["kind"] == "blank_query")

empty_hit = eval_metrics.evaluate_case(
    {"query": "q", "expected_source": [], "expect_empty": True}, ["x.txt"], 3)
check("无答案用例误召回 = 错误（不计入质量指标）",
      empty_hit["kind"] == "expect_empty" and empty_hit["empty_correct"] is False)

no_label = eval_metrics.evaluate_case({"query": "q", "expected_source": ["a.txt"]},
                                      ["a.txt"], 3, None)
check("无 chunk 标签时只算来源级（不虚构 chunk 指标）",
      "chunk_recall" not in no_label and no_label["source_recall"] == 1.0)

results = [
    eval_metrics.evaluate_case(case_b3, ["pycache_config.txt", "pycache_config.txt"], 3,
                               ["pycache_config.txt#1", "x.txt"]),
    eval_metrics.evaluate_case({"query": "q", "expected_source": ["a.txt"], "category": "lexical"},
                               ["x.txt"], 3, None),
    eval_metrics.evaluate_case({"query": "q", "expected_source": [], "expect_empty": True}, [], 3),
    eval_metrics.evaluate_case({"query": "", "expected_source": []}, [], 3),
]
summary = eval_metrics.summarize(results)

check("聚合分流计数正确（source/chunk/empty/blank）",
      summary["n_answered"] == 2 and summary["n_chunk_answered"] == 1
      and summary["n_expect_empty"] == 1 and summary["n_blank_query"] == 1)
check("chunk 聚合只含带标签用例",
      summary["chunk_recall"] == 0.5 and summary["source_recall"] == (1.0 + 0.0) / 2,
      f"chunk_recall={summary['chunk_recall']} source_recall={summary['source_recall']}")
check("empty_accuracy 独立统计", summary["empty_accuracy"] == 1.0)

# ==========================
# 5. 合成语料完整性（chunk 结构）
# ==========================

cases, documents = eval_rag_ablation.load_cases(str(CORPUS_PATH))

check("合成语料含文档与用例", len(documents) == 7 and len(cases) == 39,
      f"docs={len(documents)} cases={len(cases)}")

all_chunk_ids = [c["chunk_id"] for d in documents for c in d.get("chunks", [])]
check("chunk_id 全局唯一", len(all_chunk_ids) == len(set(all_chunk_ids)),
      f"n_chunks={len(all_chunk_ids)}")

doc_sources = {d["source"] for d in documents}
chunk_id_set = set(all_chunk_ids)

labeled_chunks = {c_id for c in cases for c_id in c.get("expected_chunks", [])}
check("expected_chunks 全部存在于语料（可核验）",
      labeled_chunks <= chunk_id_set, f"unknown={labeled_chunks - chunk_id_set}")

answered_with_chunks = [c for c in cases
                        if not c.get("expect_empty") and c.get("expected_chunks")]
check("全部 28 条 answered 用例都有 chunk 标签",
      len(answered_with_chunks) == 28, f"n={len(answered_with_chunks)}")

by_cat = {}
for c in cases:
    by_cat.setdefault(c.get("category"), 0)
    by_cat[c.get("category")] += 1
check("覆盖 8 类且重点类别齐全",
      set(by_cat) == {"lexical", "paraphrase", "keyword", "distractor",
                      "cross_chunk", "multi_source", "no_answer", "boundary"})
check("跨分块/多来源/重复分块均有 chunk 标签",
      all(c.get("expected_chunks") for c in cases
          if c.get("category") in {"cross_chunk", "multi_source"})
      and any(len(c.get("expected_chunks", [])) == 2 for c in cases
              if c.get("category") == "multi_source")
      and any(len(c.get("expected_chunks", [])) == 2 and c["id"] == "B2" for c in cases))
check("干扰/无答案用例无 chunk 标签（expect_empty）",
      all(not c.get("expected_chunks") for c in cases
          if c.get("category") in {"distractor", "no_answer"}))

# chunk_id 与 source 一致（ID 前缀 = source）
check("chunk_id 前缀与 source 一致",
      all(c_id.split("#")[0] in doc_sources for c_id in chunk_id_set))

# ==========================
# 6. 旧标注兼容（22 条只做来源级，不虚构 chunk ID）
# ==========================

old_cases, old_documents = eval_rag_ablation.load_cases(
    str(PROJECT_DIR / "rag_eval_labels.json"))
check("旧 22 条可读取且无 chunk 结构",
      len(old_cases) == 22 and old_documents == []
      and all(not c.get("expected_chunks") for c in old_cases))

old_result = eval_metrics.evaluate_case(
    old_cases[0], ["math.txt"], 3, None)
check("旧标注仅产出来源级指标",
      old_result["kind"] == "answered" and "chunk_recall" not in old_result)

# ==========================
# 7. 确定性指标手算 + 端到端（确定性检索器）
# ==========================

fixed_results = [
    eval_metrics.evaluate_case({"query": "q", "expected_source": ["a.txt", "b.txt"]},
                               ["b.txt", "x.txt", "a.txt"], 3, None),
    eval_metrics.evaluate_case({"query": "q", "expected_source": ["c.txt"]},
                               ["x.txt", "y.txt", "z.txt"], 3, None),
]
fixed_summary = eval_metrics.summarize(fixed_results)

expected_ndcg_case1 = (1.0 / math.log2(2) + 1.0 / math.log2(4)) / (1.0 / math.log2(2) + 1.0 / math.log2(3))
check("确定性指标：source_recall 手算一致",
      abs(fixed_summary["source_recall"] - 0.5) < 1e-4)
check("确定性指标：source_ndcg 手算一致",
      abs(fixed_summary["source_ndcg"] - expected_ndcg_case1 / 2) < 1e-4,
      f"got={fixed_summary['source_ndcg']}")

# 修复前后 NDCG 对比（重复来源，确定性）
def legacy_ndcg(ranked, expected, k):
    ranks = [i + 1 for i, s in enumerate(ranked[:k]) if s in expected]
    dcg = sum(1.0 / (r + 1) for r in ranks)
    ideal = sum(1.0 / (i + 1) for i in range(min(k, len(expected))))
    return dcg / ideal if ideal else 0.0

old_dup = legacy_ndcg(["a.txt", "a.txt", "a.txt"], ["a.txt"], 3)
new_dup = eval_metrics.ndcg_at_k(["a.txt", "a.txt", "a.txt"], ["a.txt"], 3)
check("修复前后 NDCG 对比（重复来源）",
      old_dup > 1.0 and abs(new_dup - 1.0) < 1e-9,
      f"旧={old_dup:.4f} 新={new_dup:.4f}")


def deterministic_retriever(query, top_k=3):
    """确定性检索器：按查询字符与分块内容字面重合排序（仅测评测器）。"""

    if not isinstance(query, str) or not query.strip():
        return []

    chunks = []

    for doc in documents:
        for chunk in doc.get("chunks", []):
            chunks.append({"content": chunk["content"], "source": doc["source"],
                           "chunk_id": chunk["chunk_id"]})

    scored = []
    for chunk in chunks:
        overlap = sum(1 for token in query if token in chunk["content"])
        if overlap > 0:
            scored.append((overlap, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)

    return [{"content": c["content"], "source": c["source"], "score": 0.0}
            for _, c in scored[:top_k]]


import rag_service as _rag

_original_retrieve = _rag.retrieve_chunks
_rag.retrieve_chunks = deterministic_retriever

try:
    id_map = eval_rag_ablation.chunk_id_map(documents)
    metrics, grouped, rows = eval_rag_ablation.evaluate_config(
        "vector", cases, documents, 3, mock_rerank=False)
finally:
    _rag.retrieve_chunks = _original_retrieve

check("端到端双口径指标均产出",
      metrics["n_chunk_answered"] == 28 and metrics["source_recall"] is not None
      and metrics["chunk_recall"] is not None,
      f"source_recall={metrics['source_recall']} chunk_recall={metrics['chunk_recall']}")
check("确定性检索器下 source/chunk 指标可分离（允许不完美）",
      0 <= metrics["chunk_recall"] <= 1 and 0 <= metrics["source_recall"] <= 1
      and metrics["n_answered"] == 28 and metrics["n_expect_empty"] == 10
      and metrics["n_blank_query"] == 1)
check("端到端分组覆盖 8 类", set(grouped) == {
    "lexical", "paraphrase", "keyword", "distractor",
    "cross_chunk", "multi_source", "no_answer", "boundary"})

print("ALL TESTS COMPLETED")
