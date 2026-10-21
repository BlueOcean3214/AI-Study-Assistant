"""检索评测指标（纯函数，零外部依赖，供 eval_rag_ablation / 单元测试共用）。

标签口径（全模块统一，两套标签互不混淆）：
- **source 级**：二元相关性，结果相关 ⇔ result.source ∈ expected_source
  （旧标注只有 source 级；同来源多个分块在 source 级只算 1 个来源）
- **chunk 级**：二元相关性，结果相关 ⇔ 该结果所属 chunk_id ∈ expected_chunks
  （chunk_id 形如 "文件名#序号"，检索结果按 (source, content) 映射回 chunk_id）

指标命名（@K 对应函数的 k 参数；聚合结果键同名）：
- source_recall@K = 命中的期望来源（去重）/ |expected_source|   ——来源覆盖度
- source_entry_precision@K = top-K 中"来源标签相关"的检索条目数 / K
  ——按来源标签判定的检索**条目**精确率：同来源多个分块分别计数，
    不是去重后的"来源精确率"（同来源多块 ≠ 多个来源）
- source_mrr = 1 / 首条来源相关结果的排名
- source_ndcg@K = 二元增益、来源去重的 DCG/IDCG
- chunk_recall@K / chunk_precision@K / chunk_mrr / chunk_ndcg@K：
  同套公式作用于 chunk_id（重复 chunk_id 同样去重计增益）；仅有 chunk 标签的用例计算

兼容：聚合与用例结果中的旧键 source_precision 保留为 source_entry_precision
的别名（同值，deprecated，仅为不破坏既有输出消费方）。

明确规则：
- 多来源/多分块：expected 列表含全部相关项，禁止只标一个
- 空结果（answered 用例）：两套指标全记 0（计入平均）
- 无相关文档（expect_empty）：单独统计 empty_accuracy，不计入任何质量指标
- 空查询：单独统计（skipped），不计入任何指标
- 重复结果：source 级同来源不算多个来源；chunk 级同 chunk_id 不重复计增益；
  precision（条目级）对重复条目如实计数
"""

import math


# ==========================
# 通用核心（对标识列表生效：source 或 chunk_id）
# ==========================


def hit_ranks(ranked_ids, expected_ids):
    """相关条目的 1-based 排名列表（条目级，重复标识每条都记）。"""

    expected = set(expected_ids or [])

    return [i + 1 for i, item in enumerate(ranked_ids) if item in expected]


def coverage_recall_at_k(ranked_ids, expected_ids, k):
    """覆盖度 Recall@K：命中的期望标识（去重）/ 期望标识总数。"""

    expected = set(expected_ids or [])

    if not expected:
        return None

    top = set(ranked_ids[:k])
    covered = expected & top

    return len(covered) / len(expected)


def item_precision_at_k(ranked_ids, expected_ids, k):
    """条目级 Precision@K：top-K 中相关条目数 / K。"""

    if k <= 0:
        return None

    top = ranked_ids[:k]

    if not top:
        return 0.0

    expected = set(expected_ids or [])
    relevant = sum(1 for item in top if item in expected)

    return relevant / k


def reciprocal_rank(ranked_ids, expected_ids):
    """MRR 贡献值：1 / 首个相关条目排名；无相关条目 = 0。"""

    ranks = hit_ranks(ranked_ids, expected_ids)

    return 1.0 / ranks[0] if ranks else 0.0


def dedup_ndcg_at_k(ranked_ids, expected_ids, k):
    """NDCG@K（二元增益 + 标识去重：每个期望标识只计首次出现的增益，保证 ≤1）。"""

    expected = set(expected_ids or [])

    if not expected or k <= 0:
        return None

    gains = []
    seen = set()

    for item in ranked_ids[:k]:
        gain = 0.0

        if item in expected and item not in seen:
            gain = 1.0
            seen.add(item)

        gains.append(gain)

    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal_hits = min(k, len(expected))
    idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))

    return dcg / idcg if idcg > 0 else 0.0


# ==========================
# source 级公开口径
# ==========================


def recall_at_k(ranked_sources, expected_sources, k):
    """source_recall@K（命名见模块文档；保留旧函数名兼容）。"""

    return coverage_recall_at_k(ranked_sources, expected_sources, k)


def source_entry_precision_at_k(ranked_sources, expected_sources, k):
    """source_entry_precision@K：按来源标签判定的检索条目精确率（同来源多条各计）。"""

    return item_precision_at_k(ranked_sources, expected_sources, k)


# deprecated 别名（最小兼容，勿在新代码使用）
precision_at_k = source_entry_precision_at_k


def ndcg_at_k(ranked_sources, expected_sources, k):
    """source_ndcg@K（二元增益、来源去重）。"""

    return dedup_ndcg_at_k(ranked_sources, expected_sources, k)


# ==========================
# chunk 级公开口径
# ==========================


def chunk_recall_at_k(ranked_chunk_ids, expected_chunk_ids, k):
    """chunk_recall@K：命中的期望分块（去重）/ 期望分块总数。"""

    return coverage_recall_at_k(ranked_chunk_ids, expected_chunk_ids, k)


def chunk_precision_at_k(ranked_chunk_ids, expected_chunk_ids, k):
    """chunk_precision@K：top-K 中相关分块条目数 / K。"""

    return item_precision_at_k(ranked_chunk_ids, expected_chunk_ids, k)


def chunk_ndcg_at_k(ranked_chunk_ids, expected_chunk_ids, k):
    """chunk_ndcg@K（二元增益、chunk_id 去重）。"""

    return dedup_ndcg_at_k(ranked_chunk_ids, expected_chunk_ids, k)


# ==========================
# 用例级分流
# ==========================


def evaluate_case(case, ranked_sources, k, ranked_chunk_ids=None):
    """单用例指标包（source 级恒算；chunk 级仅在标签与映射都存在时计算）。"""

    query = case.get("query")
    expected = case.get("expected_source") or []
    expected_chunks = case.get("expected_chunks") or []
    expect_empty = bool(case.get("expect_empty"))

    # 空查询：单独统计，不计入任何指标
    if not (isinstance(query, str) and query.strip()):
        return {"kind": "blank_query"}

    # 无答案用例：只看空返回正确性，不计入检索质量指标
    if expect_empty or not expected:
        return {
            "kind": "expect_empty",
            "empty_correct": len(ranked_sources) == 0,
        }

    result = {
        "kind": "answered",
        "source_recall": recall_at_k(ranked_sources, expected, k),
        "source_entry_precision": source_entry_precision_at_k(ranked_sources, expected, k),
        "source_rr": reciprocal_rank(ranked_sources, expected),
        "source_ndcg": ndcg_at_k(ranked_sources, expected, k),
    }
    # deprecated 别名键（同值，兼容旧输出消费方）
    result["source_precision"] = result["source_entry_precision"]

    # chunk 级：需要 expected_chunks 标签 + 检索结果的 chunk_id 映射
    if expected_chunks and ranked_chunk_ids is not None:
        result["chunk_recall"] = chunk_recall_at_k(ranked_chunk_ids, expected_chunks, k)
        result["chunk_precision"] = chunk_precision_at_k(ranked_chunk_ids, expected_chunks, k)
        result["chunk_rr"] = reciprocal_rank(ranked_chunk_ids, expected_chunks)
        result["chunk_ndcg"] = chunk_ndcg_at_k(ranked_chunk_ids, expected_chunks, k)
        result["n_expected_chunks"] = len(set(expected_chunks))

    return result


def summarize(case_results):
    """聚合：source 组恒算；chunk 组只聚合带 chunk 指标的用例。"""

    answered = [r for r in case_results if r["kind"] == "answered"]
    chunk_answered = [r for r in answered if "chunk_recall" in r]
    empties = [r for r in case_results if r["kind"] == "expect_empty"]
    blanks = [r for r in case_results if r["kind"] == "blank_query"]

    def mean(values):
        return round(sum(values) / len(values), 4) if values else None

    summary = {
        "n_answered": len(answered),
        "n_chunk_answered": len(chunk_answered),
        "n_expect_empty": len(empties),
        "n_blank_query": len(blanks),
        "source_recall": mean([r["source_recall"] for r in answered]),
        "source_entry_precision": mean([r["source_entry_precision"] for r in answered]),
        "source_mrr": mean([r["source_rr"] for r in answered]),
        "source_ndcg": mean([r["source_ndcg"] for r in answered]),
        "chunk_recall": mean([r["chunk_recall"] for r in chunk_answered]),
        "chunk_precision": mean([r["chunk_precision"] for r in chunk_answered]),
        "chunk_mrr": mean([r["chunk_rr"] for r in chunk_answered]),
        "chunk_ndcg": mean([r["chunk_ndcg"] for r in chunk_answered]),
        "empty_accuracy": mean([1.0 if r["empty_correct"] else 0.0 for r in empties]),
    }
    # deprecated 别名键（同值，兼容旧输出消费方）
    summary["source_precision"] = summary["source_entry_precision"]

    return summary


def summarize_by_category(cases, case_results):
    """按 category 分组聚合（防止总体平均掩盖失败场景）。"""

    grouped = {}

    for case, result in zip(cases, case_results):
        category = case.get("category") or "uncategorized"
        grouped.setdefault(category, []).append(result)

    return {category: summarize(results) for category, results in sorted(grouped.items())}
