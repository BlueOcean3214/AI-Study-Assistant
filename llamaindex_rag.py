"""LlamaIndex RAG 适配路径（独立、可配置的实验检索路径）。

定位：
- 自研 rag_service 仍是默认主路径（稳定、已评测）
- 本模块用 LlamaIndex（llama-index-core）构建真实可运行的向量检索：
  复用 document_service 的知识库读取/切片 与 embedding_service 的云端 Embedding 配置，
  不重复制造 API 调用（Embedding 桥接现有统一配置，文档向量由 LlamaIndex 自己持久化）
- 索引存储在独立目录（默认 .llamaindex_store/），带模型身份标识；
  换 Provider/模型/维度后身份不匹配 → 自动重建（与主缓存 .rag_cache/ 完全隔离）

配置：
- RAG_BACKEND=custom（默认，自研 rag_service）| llamaindex（本路径）
- LLAMAINDEX_STORE_DIR：索引目录（默认项目根 .llamaindex_store/）

接口（与 rag_service.retrieve_chunks 兼容）：
    retrieve_chunks(query, top_k=3) -> [{"content", "source", "score"}]
"""

import json
import os
import time
from pathlib import Path

import env_loader

env_loader.load_env()

import embedding_service
import rag_service


DEFAULT_STORE_DIR = Path(__file__).resolve().parent / ".llamaindex_store"
IDENTITY_FILENAME = "identity.json"


class LlamaIndexRagError(RuntimeError):
    """LlamaIndex 路径不可用（依赖缺失 / 索引构建失败）。"""


def store_dir():
    return Path(os.getenv("LLAMAINDEX_STORE_DIR") or DEFAULT_STORE_DIR)


def is_enabled():
    """RAG_BACKEND=llamaindex 时启用（默认自研主路径）。"""

    return (os.getenv("RAG_BACKEND") or "custom").strip().lower() == "llamaindex"


def _import_llama():
    try:
        from llama_index.core import (
            Document,
            StorageContext,
            VectorStoreIndex,
            load_index_from_storage,
        )
        from llama_index.core.embeddings import BaseEmbedding
    except ImportError as error:
        raise LlamaIndexRagError(
            "LlamaIndex 路径需要 llama-index-core（pip install llama-index-core）"
        ) from error

    return {
        "Document": Document,
        "StorageContext": StorageContext,
        "VectorStoreIndex": VectorStoreIndex,
        "load_index_from_storage": load_index_from_storage,
        "BaseEmbedding": BaseEmbedding,
    }


def _make_embedder(api):
    """把现有 embedding_service 桥接成 LlamaIndex Embedding（同一配置、同一模型/维度）。"""

    BaseEmbedding = api["BaseEmbedding"]

    class ServiceEmbedding(BaseEmbedding):
        def _get_text_embedding(self, text):
            return embedding_service.get_embedding(text)

        def _get_text_embeddings(self, texts):
            return embedding_service.get_embeddings(list(texts))

        def _get_query_embedding(self, query):
            return embedding_service.get_embedding(query)

        # LlamaIndex 的抽象 async 接口：委托同步实现（本项目调用都是同步语义）
        async def _aget_text_embedding(self, text):
            return self._get_text_embedding(text)

        async def _aget_text_embeddings(self, texts):
            return self._get_text_embeddings(texts)

        async def _aget_query_embedding(self, query):
            return self._get_query_embedding(query)

    return ServiceEmbedding()


def _read_identity():
    path = store_dir() / IDENTITY_FILENAME

    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_identity():
    path = store_dir() / IDENTITY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cache_identity": embedding_service.cache_identity()}, ensure_ascii=False),
        encoding="utf-8",
    )


_INDEX_CACHE = {"index": None, "identity": None}


def build_index(force=False):
    """构建/加载 LlamaIndex 索引（身份一致则复用；文档变更时 force=True 重建）。"""

    api = _import_llama()
    identity = embedding_service.cache_identity()

    if not force and _INDEX_CACHE["index"] is not None and _INDEX_CACHE["identity"] == identity:
        return _INDEX_CACHE["index"]

    persist_dir = store_dir()
    saved_identity = _read_identity()

    if (
        not force
        and saved_identity is not None
        and saved_identity.get("cache_identity") == identity
        and (persist_dir / "docstore.json").exists()
    ):
        try:
            storage_context = api["StorageContext"].from_defaults(persist_dir=str(persist_dir))
            index = api["load_index_from_storage"](storage_context, embed_model=_make_embedder(api))
            _INDEX_CACHE.update(index=index, identity=identity)
            return index
        except Exception as error:  # noqa: BLE001 - 加载失败则重建
            print(f"LlamaIndex 索引加载失败，将重建：{type(error).__name__}")

    # 从现有知识库切片构建（复用 document_service 的读取/切片）
    chunks = rag_service.load_chunks()

    if not chunks:
        raise LlamaIndexRagError("知识库为空，无法构建 LlamaIndex 索引")

    documents = [
        api["Document"](text=chunk["content"], metadata={"source": chunk["source"]})
        for chunk in chunks
    ]

    index = api["VectorStoreIndex"].from_documents(documents, embed_model=_make_embedder(api))

    persist_dir.mkdir(parents=True, exist_ok=True)
    index.storage_context.persist(persist_dir=str(persist_dir))
    _write_identity()

    _INDEX_CACHE.update(index=index, identity=identity)

    return index


def reset_index():
    _INDEX_CACHE.update(index=None, identity=None)


def retrieve_chunks(query, top_k=3):
    """LlamaIndex 检索：返回 [{"content", "source", "score"}]（score 为向量相似度）。"""

    started = time.perf_counter()

    if not isinstance(query, str) or not query.strip():
        return []

    try:
        index = build_index()
        retriever = index.as_retriever(similarity_top_k=max(1, min(top_k, 10)))
        nodes = retriever.retrieve(query.strip())
    except LlamaIndexRagError:
        raise
    except Exception as error:  # noqa: BLE001 - 稳定错误结构
        raise LlamaIndexRagError(f"LlamaIndex 检索失败：{type(error).__name__}: {error}") from error

    results = []

    for node_with_score in nodes:
        node = node_with_score.node
        results.append({
            "content": node.get_content(),
            "source": (node.metadata or {}).get("source"),
            "score": round(float(node_with_score.score or 0.0), 4),
        })

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    print(f"[llamaindex] query={query[:30]!r} hits={len(results)} {elapsed_ms}ms")

    return results
