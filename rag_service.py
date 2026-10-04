"""Mini-RAG v2.1（Embedding RAG + 向量持久化）：切片后用 bge-m3 向量检索。

流程：
knowledge/**/*.txt
-> load_documents()            递归读取所有 txt（rglob），source 为相对 knowledge 的路径
-> split_into_chunks()         切片（空行 / 标题 / 最大长度）
-> load_chunks()               得到 [{"content": 片段, "source": 相对路径}]
-> retrieve_chunks()           按正文 sha256 查向量缓存，缺失的分批取 embedding，
                               算 cosine 相似度，返回 [{"content", "source", "score"}] 的 Top-K
-> retrieve_context()          兼容旧代码，只把 content 拼成字符串

向量缓存：
- 内存：_EMBEDDING_CACHE（正文 sha256 -> 向量）
- 磁盘：.rag_cache/meta.json + .rag_cache/embeddings.jsonl（见 vector_cache.py）
- 第一次检索时惰性加载；每批 embedding 成功后立即追加落盘，重启不必重算

文档的保存 / 读取 / 切片都在 document_service 里，本模块只负责检索，
下面这几个同名函数为了保持向后兼容而保留。
"""

import requests

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
import vector_cache
from embedding_service import EmbeddingError, get_embedding, get_embeddings


# 最终返回相似度最高的前几个 chunk
TOP_K = 3

# 相似度低于这个值的 chunk 不返回，避免无关问题也把资料塞进 Prompt。
# bge-m3 的相似度整体偏高：实测无关问题 <= 0.49，相关问题 >= 0.52，所以取 0.5。
# 如果想无条件返回 Top-3，把它改成 0.0 即可。
MIN_SCORE = 0.5

# 取模型 digest 用（判断模型是否被就地更新过）
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"

# 进程内缓存：chunk 正文的 sha256 -> embedding 向量（array("f")）
# 键与磁盘缓存（vector_cache）完全一致，方便重启后原样恢复。
_EMBEDDING_CACHE = {}

# 缓存加载状态
_CACHE_LOADED = False
_CACHE_INVALID_REASON = None
_CACHE_DIM = None
_META_WRITTEN = False
_MODEL_DIGEST = None
_MODEL_DIGEST_FETCHED = False


def _fetch_model_digest():
    """从 Ollama 取当前模型指纹；取不到就返回 None（不因此让检索失败）。"""

    try:
        response = requests.get(OLLAMA_TAGS_URL, timeout=5)
        response.raise_for_status()

        for model in response.json().get("models", []):
            name = model.get("name", "")

            if name == embedding_service.EMBEDDING_MODEL or name.startswith(
                embedding_service.EMBEDDING_MODEL + ":"
            ):
                return model.get("digest")
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return None

    return None


def current_model_digest():
    """当前模型指纹（每个进程只取一次）。"""

    global _MODEL_DIGEST, _MODEL_DIGEST_FETCHED

    if not _MODEL_DIGEST_FETCHED:
        _MODEL_DIGEST_FETCHED = True
        _MODEL_DIGEST = _fetch_model_digest()

    return _MODEL_DIGEST


def ensure_cache_loaded():
    """第一次检索时从磁盘加载向量缓存（惰性，只加载一次）。

    任何形式的缓存问题（缺失、损坏、模型/维度不匹配）都只会让缓存为空，
    不会让 /ask 失败。
    """

    global _CACHE_LOADED, _CACHE_INVALID_REASON, _CACHE_DIM

    if _CACHE_LOADED:
        return

    _CACHE_LOADED = True

    meta = vector_cache.read_meta()

    if meta is None:
        # meta 缺失/损坏：如果向量文件里还有内容，说明来路不明，稍后归档重写
        vectors_file = vector_cache.vectors_path()

        if vectors_file.exists() and vectors_file.stat().st_size > 0:
            _CACHE_INVALID_REASON = "meta.json 缺失或损坏，但 embeddings.jsonl 存在"

        return

    usable, reason = vector_cache.check_meta(
        meta,
        model=embedding_service.EMBEDDING_MODEL,
        model_digest=current_model_digest(),
    )

    if not usable:
        _CACHE_INVALID_REASON = reason
        print(f"向量缓存不可用（{reason}），将重新生成")
        return

    vectors = vector_cache.read_vectors()
    expected_dim = meta.get("dim")
    bad_dim = [key for key, vector in vectors.items() if len(vector) != expected_dim]

    if bad_dim:
        _CACHE_INVALID_REASON = (
            f"缓存里有 {len(bad_dim)} 条向量的长度不等于 meta.dim={expected_dim}"
        )
        print(f"向量缓存不可用（{_CACHE_INVALID_REASON}），将重新生成")
        return

    _EMBEDDING_CACHE.update(vectors)
    _CACHE_DIM = expected_dim

    print(
        f"向量缓存已加载：{len(vectors)} 条"
        f"（模型 {meta.get('model')}，维度 {expected_dim}）"
    )


def _validate_cache_dim(live_dim):
    """实时向量维度与磁盘缓存不一致时，整块缓存作废。"""

    global _CACHE_INVALID_REASON, _CACHE_DIM

    if _CACHE_DIM is None or _CACHE_DIM == live_dim:
        return

    _CACHE_INVALID_REASON = f"实时向量维度 {live_dim} 与缓存维度 {_CACHE_DIM} 不一致"
    print(f"向量缓存不可用（{_CACHE_INVALID_REASON}），将重新生成")

    _EMBEDDING_CACHE.clear()
    _CACHE_DIM = None


def reset_cache(reload=False):
    """清空内存缓存并复位加载状态（模拟程序重启）；reload=True 时立即重新加载。"""

    global _CACHE_LOADED, _CACHE_INVALID_REASON, _CACHE_DIM, _META_WRITTEN

    _EMBEDDING_CACHE.clear()
    _CACHE_LOADED = False
    _CACHE_INVALID_REASON = None
    _CACHE_DIM = None
    _META_WRITTEN = False

    if reload:
        ensure_cache_loaded()


def _prepare_cache_for_write(dim):
    """首次写盘前的准备：归档作废的旧缓存、必要时补写 meta.json。

    顺便记住当前缓存维度，供实时维度校验使用。
    """

    global _CACHE_INVALID_REASON, _META_WRITTEN, _CACHE_DIM

    _CACHE_DIM = dim

    if _CACHE_INVALID_REASON:
        vector_cache.archive_cache(_CACHE_INVALID_REASON)
        _CACHE_INVALID_REASON = None
        _META_WRITTEN = False

    if _META_WRITTEN:
        return

    if vector_cache.read_meta() is not None:
        _META_WRITTEN = True
        return

    _META_WRITTEN = vector_cache.write_meta(
        vector_cache.build_meta(
            model=embedding_service.EMBEDDING_MODEL,
            dim=dim,
            model_digest=current_model_digest(),
            chunk_max_chars=CHUNK_MAX_CHARS,
        )
    )


def _persist_vectors(fresh_vectors):
    """把新向量追加到磁盘缓存（每个批次成功后立即调用）。"""

    if not fresh_vectors:
        return

    _prepare_cache_for_write(dim=len(next(iter(fresh_vectors.values()))))
    vector_cache.append_vectors(fresh_vectors)


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

    - 以正文 sha256 为键，命中直接复用（相同正文跨文件只算一次）
    - 每批成功后立刻写内存 + 追加到 .rag_cache/embeddings.jsonl
    - 失败的批次带批次编号抛出 EmbeddingError，不写入任何伪向量
    """

    ensure_cache_loaded()

    chunk_hashes = []
    content_by_hash = {}

    for chunk in chunks:
        content_hash = vector_cache.chunk_hash(chunk["content"])
        chunk_hashes.append(content_hash)
        content_by_hash.setdefault(content_hash, chunk["content"])

    missing = list(
        dict.fromkeys(
            content_hash
            for content_hash in chunk_hashes
            if content_hash not in _EMBEDDING_CACHE
        )
    )

    if missing:
        batches = list(embedding_service.split_into_batches(missing))
        saved = 0

        for index, batch in enumerate(batches, start=1):
            batch_texts = [content_by_hash[content_hash] for content_hash in batch]

            try:
                vectors = get_embeddings(batch_texts)
            except EmbeddingError as error:
                raise EmbeddingError(
                    f"第 {index}/{len(batches)} 批 embedding 失败"
                    f"（本批 {len(batch)} 条，已成功缓存 {saved} 条，"
                    f"共 {len(missing)} 条待处理）：{error}"
                ) from error

            fresh_vectors = {}

            for content_hash, vector in zip(batch, vectors):
                stored = vector_cache.to_float32(vector)
                _EMBEDDING_CACHE[content_hash] = stored
                fresh_vectors[content_hash] = stored

            # 不等整批处理完，每个批次成功就落盘
            _persist_vectors(fresh_vectors)

            saved += len(batch)

    return [_EMBEDDING_CACHE[content_hash] for content_hash in chunk_hashes]


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
        ensure_cache_loaded()
        _validate_cache_dim(len(query_vector))
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
