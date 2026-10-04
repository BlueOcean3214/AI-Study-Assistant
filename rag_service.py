"""Mini-RAG v2.0（Embedding RAG）：切片后用 bge-m3 向量检索。

流程：
knowledge/**/*.txt
-> load_documents()            递归读取所有 txt（rglob），source 为相对 knowledge 的路径
-> split_into_chunks()         切片（空行 / 标题 / 最大长度）
-> load_chunks()               得到 [{"content": 片段, "source": 相对路径}]
-> retrieve_chunks()           query 与 chunk 各自取 embedding，算 cosine 相似度
                               返回 [{"content", "source", "score"}] 的 Top-K
-> retrieve_context()          兼容旧代码，只把 content 拼成字符串

文档的保存 / 读取 / 切片都在 document_service 里，本模块只负责检索，
下面这几个同名函数为了保持向后兼容而保留。
"""

from document_service import (
    CHUNK_MAX_CHARS,
    KNOWLEDGE_PATH,
    load_chunks as _load_chunks,
    load_documents as _load_documents,
    split_into_chunks as _split_into_chunks,
)

# 用模块名调用 split_into_batches（批大小常量在 embedding_service 里）；
# 同时按名字导入 get_embeddings，保持现有测试可以打桩替换它。
import embedding_service
from embedding_service import EmbeddingError, get_embedding, get_embeddings


# 最终返回相似度最高的前几个 chunk
TOP_K = 3

# 相似度低于这个值的 chunk 不返回，避免无关问题也把资料塞进 Prompt。
# bge-m3 的相似度整体偏高：实测无关问题 <= 0.49，相关问题 >= 0.52，所以取 0.5。
# 如果想无条件返回 Top-3，把它改成 0.0 即可。
MIN_SCORE = 0.5

# 进程内缓存：chunk 正文 -> embedding 向量，避免每次提问都重复调用 Ollama
_EMBEDDING_CACHE = {}


def load_documents():
    """递归读取知识库，返回 [{"content", "source"}]。"""

    return _load_documents(KNOWLEDGE_PATH)


def split_into_chunks(text, max_chars=CHUNK_MAX_CHARS):
    """把文本切片（规则见 document_service.split_into_chunks）。"""

    return _split_into_chunks(text, max_chars)


def load_chunks():
    """读取并切片，返回 [{"content", "source"}]。"""

    return _load_chunks(KNOWLEDGE_PATH)


def _cosine_similarity(vector_a, vector_b):
    """两个向量的余弦相似度，范围大致在 -1 ~ 1 之间。"""

    dot = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = sum(a * a for a in vector_a) ** 0.5
    norm_b = sum(b * b for b in vector_b) ** 0.5

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


def _chunk_vectors(chunks):
    """取每个 chunk 的向量：命中的用缓存，没命中的分成小批请求 Ollama。

    - 正文相同的 chunk 只会请求一次（source 不同也能复用）
    - 每批成功后就写入缓存，所以某一批失败不会丢掉已成功的批次
    - 失败的批次会带上批次编号抛出 EmbeddingError，不会写入任何伪向量
    """

    missing = list(
        dict.fromkeys(
            chunk["content"]
            for chunk in chunks
            if chunk["content"] not in _EMBEDDING_CACHE
        )
    )

    if missing:
        batches = list(embedding_service.split_into_batches(missing))
        saved = 0

        for index, batch in enumerate(batches, start=1):
            try:
                vectors = get_embeddings(batch)
            except EmbeddingError as error:
                raise EmbeddingError(
                    f"第 {index}/{len(batches)} 批 embedding 失败"
                    f"（本批 {len(batch)} 条，已成功缓存 {saved} 条，"
                    f"共 {len(missing)} 条待处理）：{error}"
                ) from error

            for text, vector in zip(batch, vectors):
                _EMBEDDING_CACHE[text] = vector

            saved += len(batch)

    return [_EMBEDDING_CACHE[chunk["content"]] for chunk in chunks]


def retrieve_chunks(query, top_k=TOP_K):
    """用 embedding + 余弦相似度返回最相关的前 top_k 个片段。

    返回格式：
    [{"content": "知识片段内容", "source": "math.txt", "score": 0.8}]
    """

    if not isinstance(query, str) or not query.strip():
        return []

    chunks = load_chunks()

    if not chunks:
        return []

    try:
        query_vector = get_embedding(query.strip())
        vectors = _chunk_vectors(chunks)
    except EmbeddingError as error:
        # embedding 不可用时当作“没有检索到资料”，不影响 /ask 继续回答问题
        print("embedding 调用失败:", error)
        return []

    results = []

    for chunk, vector in zip(chunks, vectors):
        score = round(_cosine_similarity(query_vector, vector), 4)

        if score >= MIN_SCORE:
            results.append(
                {
                    "content": chunk["content"],
                    "source": chunk["source"],
                    "score": score,
                }
            )

    results.sort(key=lambda item: item["score"], reverse=True)

    return results[:top_k]


def format_context(chunks):
    """可选辅助函数：把片段拼成带来源标记的文本，方便展示引用来源。"""

    blocks = [
        f"[来源: {chunk['source']}]\n\n{chunk['content']}" for chunk in chunks
    ]

    return "\n\n---\n\n".join(blocks)


def retrieve_context(query, top_k=TOP_K):
    """
    根据关键词寻找相关知识

    为了兼容旧代码，只返回拼好的字符串；
    需要来源信息时改用 retrieve_chunks()。
    """

    chunks = retrieve_chunks(query, top_k=top_k)

    context = "\n".join(chunk["content"] for chunk in chunks)

    print("RAG长度:", len(context))
    print("检索结果数量:", len(chunks))
    print("Top scores:", [chunk["score"] for chunk in chunks])

    return context
