"""测试夹具：确定性 Embedding 打桩（不联网、不依赖本地 Ollama / bge-m3，也不需要 API Key）。

用途
----
让「只需要一条可用的 Embedding 通路」的测试（记忆检索、RAG 缓存、批量、降级等）
在没有模型服务的机器上也能跑出确定性结果；迁移到云端 API 之后，单元测试不应该
再强制要求本机启动 Ollama。

能力边界（很重要）
------------------
这里的向量是**词面**的字符 n-gram 哈希向量（非负、L2 归一化、默认 1024 维），
只保证：同文本稳定、不同文本可区分、字面重叠越多余弦越大。
它**不具备语义能力**——「数学怎么学」这类与语料没有共同字词、只能靠语义召回的用例
不能在打桩环境下断言，必须放到真实 Embedding 后端（API 冒烟测试）里跑。

用法
----
    import embedding_service
    import rag_service
    import stub_embedding

    stub_embedding.install()   # 装上确定性向量，进程内一直生效
    ...
    stub_embedding.uninstall()  # 可选，恢复真实实现

注意：install() 会替换 embedding_service / rag_service 里的 get_embedding、
get_embeddings，并让 rag_service.current_model_digest 返回 None（云端/打桩都没有权重
digest，避免测试为了取 digest 去连 localhost:11434）。
"""

import hashlib
import math
import re

DIMENSIONS = 1024

# 主题维：为测试用小语料准备的一小组「同义/同主题关键词」。
# 词面向量本身没有语义能力，但测试需要对「相关的问题能召回」做断言，
# 这里用一张显式、可审查的主题词表模拟这一点（只覆盖测试夹具用的知识点）。
# 真实语义能力仍然必须在真实 Embedding 后端上验证，见 test_real_api_smoke.py。
TOPIC_DIM_BASE = 1000
# 主题权重远大于词面计数，让「命中同一主题」的余弦稳定落在 MIN_SCORE(0.5) 之上，
# 而完全无关文本（既无共同主题也无共同字词）接近 0——这是打桩环境下唯一可自洽的标定方式。
TOPIC_WEIGHT = 12.0

TOPICS = (
    ("integral", ("定积分", "积分", "面积", "莱布尼茨", "牛顿", "微分", "导数", "上限", "下限", "曲边", "图形")),
    ("study", ("怎么学", "如何学", "学习方法", "学习方法论", "数学怎么", "学习")),
    ("tree", ("二叉树", "树节点", "遍历", "子树", "叶子节点")),
    ("probability", ("条件概率", "概率", "贝叶斯", "事件", "独立")),
)

LAST_TOPIC_DIM = TOPIC_DIM_BASE + len(TOPICS) - 1

assert LAST_TOPIC_DIM < DIMENSIONS, "主题维必须落在向量维度内"

_ORIGINALS = {}
_INSTALLED = False
_CALLS = {"count": 0}


def _tokenize(text):
    """中文按单字 + 相邻 bigram，英文数字按词切；空白丢弃。"""

    chars = [char for char in text if not char.isspace()]
    tokens = list(chars)

    for index in range(len(chars) - 1):
        tokens.append(chars[index] + chars[index + 1])

    tokens.extend(re.findall(r"[a-z0-9]+", text.lower()))

    return tokens


def get_embedding(text):
    """确定性词面向量（非负、L2 归一化）。空文本按真实实现的行为抛 EmbeddingError。"""

    import embedding_service

    if text is None or not str(text).strip():
        raise embedding_service.EmbeddingError("embedding 输入文本为空")

    _CALLS["count"] += 1

    vector = [0.0] * DIMENSIONS

    for token in _tokenize(str(text)):
        # 主题维之外的维度留给词面哈希
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vector[int.from_bytes(digest[:4], "big") % TOPIC_DIM_BASE] += 1.0

    lowered = str(text).lower()

    for offset, (_topic, keywords) in enumerate(TOPICS):
        if any(keyword in lowered for keyword in keywords):
            vector[TOPIC_DIM_BASE + offset] += TOPIC_WEIGHT

    norm = math.sqrt(sum(value * value for value in vector))

    if norm == 0.0:
        # 全是空白/标点：给一个固定的单位向量，保证返回结构合法
        vector[0] = 1.0
        return vector

    return [value / norm for value in vector]


def get_embeddings(texts):
    """批量版（顺序与输入一致；空列表返回空列表）。"""

    texts = list(texts)

    if not texts:
        return []

    return [get_embedding(text) for text in texts]


def call_count():
    """累计调用次数（需要断言调用次数的测试可用）。"""

    return _CALLS["count"]


def reset_call_count():
    _CALLS["count"] = 0


def install():
    """替换真实实现；重复调用是幂等的。"""

    global _INSTALLED

    if _INSTALLED:
        return

    import embedding_service
    import rag_service

    for module in (embedding_service, rag_service):
        key = (module.__name__, "get_embedding")
        _ORIGINALS.setdefault(key, getattr(module, "get_embedding", None))
        module.get_embedding = get_embedding

        key = (module.__name__, "get_embeddings")
        _ORIGINALS.setdefault(key, getattr(module, "get_embeddings", None))
        module.get_embeddings = get_embeddings

    _ORIGINALS.setdefault(
        (rag_service.__name__, "current_model_digest"),
        getattr(rag_service, "current_model_digest", None),
    )
    rag_service.current_model_digest = lambda: None

    _INSTALLED = True


def uninstall():
    """恢复 install() 之前的实现。"""

    global _INSTALLED

    if not _INSTALLED:
        return

    import embedding_service
    import rag_service

    for (module_name, attribute), original in _ORIGINALS.items():
        module = embedding_service if module_name == "embedding_service" else rag_service

        if original is None:
            continue

        setattr(module, attribute, original)

    _ORIGINALS.clear()
    _INSTALLED = False
