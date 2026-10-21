"""LlamaIndex RAG 适配路径测试：直接运行 python test_llamaindex_rag.py。

Embedding 全 Mock（确定性向量），LlamaIndex 索引/检索是真实本地计算（零 API 费用）。

覆盖：
- RAG_BACKEND 开关（默认自研主路径）
- 复用 document_service 知识库切片构建索引；检索返回 content/source/score
- 索引持久化与复用（重置内存后从磁盘加载，文档向量不重复计算）
- 模型身份变化后重建；索引目录与 .rag_cache 隔离
- 空知识库报清晰错误；查询参数非法返回空
- main.retrieve_for_ask 按配置分流
"""

import contextlib
import os
import shutil
from pathlib import Path

import embedding_service
import llamaindex_rag
import main


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


PROJECT_DIR = Path(__file__).resolve().parent
STORE_FIXTURE = PROJECT_DIR / "_test_llamaindex_store"


def fake_embedding(text):
    """确定性 4 维向量：按文本语义给轴向量。"""

    vector = [0.0, 0.0, 0.0, 1.0]

    if "定积分" in text:
        vector = [1.0, 0.0, 0.0, 0.0]
    elif "条件概率" in text:
        vector = [0.0, 1.0, 0.0, 0.0]
    elif "公式" in text:
        vector = [0.0, 0.0, 1.0, 0.0]

    return vector


def fake_embeddings(texts):
    return [fake_embedding(text) for text in texts]


@contextlib.contextmanager
def mock_embedding():
    original_one = embedding_service.get_embedding
    original_many = embedding_service.get_embeddings
    embedding_service.get_embedding = fake_embedding
    embedding_service.get_embeddings = fake_embeddings

    try:
        yield
    finally:
        embedding_service.get_embedding = original_one
        embedding_service.get_embeddings = original_many


# ==========================
# 1. 开关与配置
# ==========================

os.environ.pop("RAG_BACKEND", None)
check("默认关闭 LlamaIndex 路径（自研主路径）", llamaindex_rag.is_enabled() is False)

os.environ["RAG_BACKEND"] = "llamaindex"
check("RAG_BACKEND=llamaindex 启用", llamaindex_rag.is_enabled() is True)
os.environ.pop("RAG_BACKEND", None)

os.environ["LLAMAINDEX_STORE_DIR"] = str(STORE_FIXTURE)
shutil.rmtree(STORE_FIXTURE, ignore_errors=True)
llamaindex_rag.reset_index()

try:
    # ==========================
    # 2. 构建与检索（真实 LlamaIndex 本地计算）
    # ==========================

    embed_calls = {"docs": 0}
    original_many = embedding_service.get_embeddings

    def counting_embeddings(texts):
        embed_calls["docs"] += 1
        return fake_embeddings(texts)

    with mock_embedding():
        embedding_service.get_embeddings = counting_embeddings

        results = llamaindex_rag.retrieve_chunks("定积分怎么学", top_k=3)

    check("LlamaIndex 检索返回结果",
          len(results) > 0 and all(set(item) == {"content", "source", "score"} for item in results),
          f"results={[(i['source'], i['score']) for i in results]}")
    check("命中来源是真实知识库文件",
          any(item["source"] == "math.txt" for item in results),
          f"sources={[i['source'] for i in results]}")
    check("score 为向量相似度数值",
          all(isinstance(item["score"], float) for item in results))
    check("索引目录独立于 .rag_cache",
          llamaindex_rag.store_dir() == STORE_FIXTURE and (STORE_FIXTURE / "identity.json").exists())

    # ==========================
    # 3. 持久化复用（重置内存后从磁盘加载，不重复文档 embedding）
    # ==========================

    llamaindex_rag.reset_index()
    build_calls_before = embed_calls["docs"]

    with mock_embedding():
        embedding_service.get_embeddings = counting_embeddings
        results2 = llamaindex_rag.retrieve_chunks("定积分怎么学", top_k=3)

    check("索引从磁盘复用（不再重复文档向量计算）",
          len(results2) > 0 and embed_calls["docs"] == build_calls_before,
          f"calls_before={build_calls_before} after={embed_calls['docs']}")

    # ==========================
    # 4. 身份变化触发重建
    # ==========================

    identity_before = llamaindex_rag._read_identity()

    original_cache_identity = embedding_service.cache_identity
    embedding_service.cache_identity = lambda: "openai_compatible:other-model@4"

    try:
        llamaindex_rag.reset_index()

        with mock_embedding():
            results3 = llamaindex_rag.retrieve_chunks("定积分怎么学", top_k=2)

        identity_after = llamaindex_rag._read_identity()
        check("身份变化后重建索引",
              identity_after["cache_identity"] == "openai_compatible:other-model@4"
              and identity_after != identity_before and len(results3) > 0)
    finally:
        embedding_service.cache_identity = original_cache_identity

    # ==========================
    # 5. 查询分流与错误处理
    # ==========================

    check("空查询返回空列表", llamaindex_rag.retrieve_chunks("  ") == [])

    original_load = llamaindex_rag.rag_service.load_chunks
    llamaindex_rag.rag_service.load_chunks = lambda: []

    try:
        llamaindex_rag.reset_index()
        embedding_service.cache_identity = original_cache_identity

        with mock_embedding():
            try:
                llamaindex_rag.retrieve_chunks("查询", top_k=1)
                check("空知识库报清晰错误", False)
            except llamaindex_rag.LlamaIndexRagError as error:
                check("空知识库报清晰错误", "知识库为空" in str(error))
    finally:
        llamaindex_rag.rag_service.load_chunks = original_load

    # main.retrieve_for_ask 按配置分流（stub 两个后端验证路由）
    original_custom = main.retrieve_chunks
    original_llama = llamaindex_rag.retrieve_chunks
    main.retrieve_chunks = lambda q: [{"content": "c", "source": "s", "score": 0.9}]
    llamaindex_rag.retrieve_chunks = lambda q, top_k=3: [{"content": "l", "source": "s", "score": 0.8}]

    try:
        os.environ.pop("RAG_BACKEND", None)
        check("默认分流到自研主路径",
              main.retrieve_for_ask("q")[0]["content"] == "c")

        os.environ["RAG_BACKEND"] = "llamaindex"
        check("RAG_BACKEND=llamaindex 分流到 LlamaIndex",
              main.retrieve_for_ask("q")[0]["content"] == "l")
    finally:
        os.environ.pop("RAG_BACKEND", None)
        main.retrieve_chunks = original_custom
        llamaindex_rag.retrieve_chunks = original_llama

finally:
    os.environ.pop("LLAMAINDEX_STORE_DIR", None)
    llamaindex_rag.reset_index()
    shutil.rmtree(STORE_FIXTURE, ignore_errors=True)

print("ALL TESTS COMPLETED")
