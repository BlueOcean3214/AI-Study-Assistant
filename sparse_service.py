"""BM25 关键词检索（稀疏检索）：jieba 中文分词 + rank-bm25。

定位：为混合检索提供与向量检索互补的关键词通道。
- 中文必须先分词再进 BM25（直接按空格切分对中文完全无效）
- 索引按“当前 chunks 签名”惰性重建：文档新增 / 修改 / 删除后自动失效重建
- 只做检索，不改文档、不调用 LLM
- 依赖缺失 / 分词失败 / 索引失败抛 SparseError，由 rag_service 决定降级策略

BM25 分数是词频类分数，与余弦相似度不是同一个量纲，两者只通过 RRF 排名融合，
不做任何数值混合（融合逻辑在 rag_service）。
"""

import hashlib
import re

from rank_bm25 import BM25Okapi

try:
    import jieba
except ImportError:  # pragma: no cover - 环境缺依赖时给出清晰错误
    jieba = None


# BM25Okapi 参数取默认值（k1=1.5, b=0.75）；语料极小，不做调参
BM25_K1 = 1.5
BM25_B = 0.75
BM25_EPSILON = 0.25

# 单字/纯符号 token 对中文检索噪声大于收益，直接丢弃
_MIN_TOKEN_CHARS = 2

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]+|[一-鿿]+")

_STOPWORDS = {
    # 代词 / 量词 / 连词 / 系词等纯功能词：对区分文档没有贡献，只会制造撞词误召回
    "的", "了", "是", "在", "和", "与", "或", "也", "就", "都", "而", "及其",
    "这", "那", "什么", "怎么", "如何", "可以", "应该", "请问", "一下", "哪些",
    "一个", "一种", "这个", "那个", "每个", "每次", "各种", "其他", "其它",
    "任何", "所有", "如果", "因为", "所以", "但是", "而且", "或者", "还是",
    "就是", "没有", "不是", "我们", "你们", "他们", "以及", "其中", "然后",
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "in", "for",
}


class SparseError(Exception):
    """关键词检索不可用（依赖缺失 / 分词失败 / 索引构建失败）。"""


def tokenize(text):
    """中文分词 + 英文数字切词，返回小写 token 列表。"""

    if not isinstance(text, str) or not text.strip():
        return []

    if jieba is None:
        raise SparseError("关键词检索不可用：未安装 jieba 分词库")

    try:
        # cut_all=False：精确模式，避免把“条件概率”切成无意义碎片
        raw_tokens = jieba.lcut(text.lower(), cut_all=False)
    except Exception as error:  # noqa: BLE001 - 分词失败统一转成 SparseError
        raise SparseError(f"关键词检索分词失败：{type(error).__name__}") from error

    tokens = []

    for raw in raw_tokens:
        for match in _TOKEN_PATTERN.findall(raw):
            if len(match) < _MIN_TOKEN_CHARS:
                continue

            if match in _STOPWORDS:
                continue

            tokens.append(match)

    return tokens


def chunks_signature(chunks):
    """chunks 内容签名：(source, content) 变化即变化，用于索引失效判断。"""

    digest = hashlib.sha256()

    for chunk in chunks:
        digest.update(chunk.get("source", "").encode("utf-8"))
        digest.update(b"\x00")
        digest.update(chunk.get("content", "").encode("utf-8"))
        digest.update(b"\x00")

    return digest.hexdigest()


class SparseIndex:
    """一次性构建的 BM25 索引（chunks 签名变化后由 get_index 重建）。"""

    def __init__(self, chunks):
        self.signature = chunks_signature(chunks)
        self.size = len(chunks)

        tokenized = [tokenize(chunk.get("content", "")) for chunk in chunks]

        # BM25Okapi 要求至少一条文档；空语料直接标记为不可用
        if not tokenized or all(not tokens for tokens in tokenized):
            raise SparseError("关键词检索索引构建失败：语料为空或分词结果为空")

        try:
            self.bm25 = BM25Okapi(
                tokenized,
                k1=BM25_K1,
                b=BM25_B,
                epsilon=BM25_EPSILON,
            )
        except Exception as error:  # noqa: BLE001 - 索引失败统一转成 SparseError
            raise SparseError(f"关键词检索索引构建失败：{type(error).__name__}") from error

    def search(self, query, limit):
        """返回 [(chunk_index, bm25_score)]，按分数降序，只含分数 > 0 的条目。"""

        if limit < 1:
            return []

        query_tokens = tokenize(query)

        if not query_tokens:
            return []

        try:
            scores = self.bm25.get_scores(query_tokens)
        except Exception as error:  # noqa: BLE001
            raise SparseError(f"关键词检索失败：{type(error).__name__}") from error

        ranked = [
            (index, float(score))
            for index, score in enumerate(scores)
            if score > 0
        ]

        ranked.sort(key=lambda item: item[1], reverse=True)

        return ranked[:limit]


# 进程内索引缓存：chunks 签名 -> SparseIndex（文档变化后签名不同，自动重建）
_INDEX_CACHE = {}


def get_index(chunks):
    """按当前 chunks 取 BM25 索引（签名一致则复用，否则重建）。"""

    signature = chunks_signature(chunks)

    cached = _INDEX_CACHE.get(signature)

    if cached is not None:
        return cached

    index = SparseIndex(chunks)
    _INDEX_CACHE.clear()
    _INDEX_CACHE[signature] = index

    return index


def reset_index():
    """清空索引缓存（测试 / 知识库大幅变更后使用）。"""

    _INDEX_CACHE.clear()
