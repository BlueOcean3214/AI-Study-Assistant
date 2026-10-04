"""rag_service / embedding_service 回归测试：直接运行 python test_rag_service.py。

注意：现在是 Embedding RAG，运行测试需要本机 Ollama 已启动并已 pull bge-m3。

覆盖：
- 最初的线上问题：GET /ask?question=定积分怎么学 时 RAG 长度为 0
- v1.2/v1.3 契约：retrieve_chunks 返回 content/source/score、多级目录与 source 相对路径
- v2.0 升级：embedding 语义检索、余弦相似度、embedding 缓存
"""

import shutil
from pathlib import Path

import embedding_service
import main
import rag_service


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


# 0. 前置检查：embedding 服务必须可用，否则后面的失败会很误导
try:
    probe_vector = embedding_service.get_embedding("前置检查")
except embedding_service.EmbeddingError as error:
    raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

# 1. embedding_service 契约
check("get_embedding 返回向量", len(probe_vector) == 1024, f"dim={len(probe_vector)}")
check(
    "向量元素为数值",
    all(isinstance(value, (int, float)) for value in probe_vector),
)
check("get_embeddings 空列表返回空", embedding_service.get_embeddings([]) == [])
batch = embedding_service.get_embeddings(["定积分", "二叉树"])
check("批量接口返回数量一致", len(batch) == 2, f"n={len(batch)}")
check(
    "同一文本两次结果一致",
    abs(rag_service._cosine_similarity(probe_vector, embedding_service.get_embedding("前置检查")) - 1.0)
    < 0.001,
)
try:
    embedding_service.get_embedding("   ")
    check("空文本应报错", False)
except embedding_service.EmbeddingError:
    check("空文本正确抛出 EmbeddingError", True)

# 2. 语义检索：中文长句（无空格）必须能召回知识库
context = rag_service.retrieve_context("定积分怎么学")
check("中文无空格查询可召回", len(context) > 0, f"len={len(context)}")
check("召回内容包含关键词", "定积分" in context)

# 2b. 语义能力：这句与知识库没有相同的关键词片段，关键词检索会漏掉，embedding 能召回
semantic_chunks = rag_service.retrieve_chunks("数学怎么学")
check("语义相关查询可召回", len(semantic_chunks) > 0, f"n={len(semantic_chunks)}")

# 3. retrieve_chunks 返回契约
chunks = rag_service.retrieve_chunks("定积分怎么学")
check("retrieve_chunks 返回 Top-K", 0 < len(chunks) <= rag_service.TOP_K, f"n={len(chunks)}")
check(
    "返回字段为 content/source/score",
    all(set(chunk) == {"content", "source", "score"} for chunk in chunks),
)
check("source 为文件名", all(chunk["source"] == "math.txt" for chunk in chunks))
check("content 非空", all(chunk["content"].strip() for chunk in chunks))
check(
    "score 是 0~1 的浮点相似度",
    all(isinstance(chunk["score"], float) and 0 < chunk["score"] <= 1 for chunk in chunks),
    f"scores={[chunk['score'] for chunk in chunks]}",
)
check(
    "score 降序排列",
    [chunk["score"] for chunk in chunks]
    == sorted((chunk["score"] for chunk in chunks), reverse=True),
)
check("retrieve_chunks 空查询返回空列表", rag_service.retrieve_chunks("") == [])
check(
    "低相似度结果被过滤",
    all(chunk["score"] >= rag_service.MIN_SCORE for chunk in chunks),
    f"MIN_SCORE={rag_service.MIN_SCORE}",
)

# 4. 知识库外的问题不应被召回（实测相似度都低于 MIN_SCORE）
for query in ("今天天气怎么样", "推荐一部电影", "吃什么", "如何写简历", "Python 怎么安装"):
    ctx = rag_service.retrieve_context(query)
    check(f"无关问题不误召回: {query!r}", ctx == "", f"len={len(ctx)}")

# 5. 空查询与非法输入
for query in ("", "   ", None):
    check(f"空查询返回空串: {query!r}", rag_service.retrieve_context(query) == "")

# 6. 文档与切片结构
documents = rag_service.load_documents()
check("知识库可加载", len(documents) > 0, f"docs={len(documents)}")
check(
    "document 结构为 content/source",
    all(set(document) == {"content", "source"} for document in documents),
)
all_chunks = rag_service.load_chunks()
check("切片保留 source", all(chunk["source"] for chunk in all_chunks), f"chunks={len(all_chunks)}")

# 7. embedding 缓存：第二次检索不应重复请求 chunk 向量
original_get_embeddings = rag_service.get_embeddings
calls = {"count": 0}


def counting_get_embeddings(texts):
    calls["count"] += 1
    return original_get_embeddings(texts)


rag_service._EMBEDDING_CACHE.clear()
rag_service.get_embeddings = counting_get_embeddings
try:
    rag_service.retrieve_chunks("定积分怎么学")
    after_first = calls["count"]

    rag_service.retrieve_chunks("定积分怎么学")
    after_second = calls["count"]
finally:
    rag_service.get_embeddings = original_get_embeddings

check("冷启动批量请求 chunk 向量", after_first == 1, f"calls={after_first}")
check("重复检索命中缓存", after_second == after_first, f"calls={after_second}")

# 8. 端到端：main.py 的 /ask 会把检索到的资料真正拼进 prompt
# 注意：main.py 用 from ai_service import call_ollama，所以要打桩 main.call_ollama
captured = {}


def fake_call_ollama(prompt):
    captured["prompt"] = prompt
    return "stub answer", None


original_call_ollama = main.call_ollama
main.call_ollama = fake_call_ollama
try:
    response = main.ask("定积分怎么学")
finally:
    main.call_ollama = original_call_ollama

check("接口无错误", response["error"] is None, f"error={response['error']}")
check("资料已注入 prompt", "定积分" in captured.get("prompt", ""))
check("prompt 包含检索资料段", "请参考下面资料回答问题" in captured.get("prompt", ""))
check("接口返回模型内容", response["answer"] == "stub answer")
check(
    "接口 sources 字段完整",
    bool(response["sources"])
    and all(set(chunk) == {"content", "source", "score"} for chunk in response["sources"]),
    f"sources={[chunk['source'] for chunk in response['sources']]}",
)
check("format_context 可拼出来源标记", "来源: math.txt" in rag_service.format_context(chunks))

# 9. 多级目录、source 相对路径（在工作区内造临时知识库，测完删除）
fixture_root = Path(__file__).resolve().parent / "_test_knowledge_tmp"
if fixture_root.exists():
    shutil.rmtree(fixture_root)

(fixture_root / "math").mkdir(parents=True)
(fixture_root / "cs" / "algo").mkdir(parents=True)
(fixture_root / "math" / "定积分.txt").write_text(
    "定积分是微积分的重要内容。\n\n牛顿-莱布尼茨公式。", encoding="utf-8"
)
(fixture_root / "cs" / "algo" / "数据结构.txt").write_text(
    "二叉树是常用的数据结构。\n\n图的遍历方法。", encoding="utf-8"
)
(fixture_root / "README.txt").write_text("知识库说明文件。", encoding="utf-8")

original_knowledge_path = rag_service.KNOWLEDGE_PATH
rag_service.KNOWLEDGE_PATH = fixture_root
try:
    documents = rag_service.load_documents()
    sources = sorted(document["source"] for document in documents)

    check("递归读取多级目录", len(documents) == 3, f"docs={len(documents)}")
    check(
        "source 为相对 knowledge 的路径",
        sources == ["README.txt", "cs/algo/数据结构.txt", "math/定积分.txt"],
        f"sources={sources}",
    )

    sub_chunks = rag_service.retrieve_chunks("定积分怎么学")
    check(
        "子目录文档排在第一位",
        bool(sub_chunks) and sub_chunks[0]["source"] == "math/定积分.txt",
        f"sources={[chunk['source'] for chunk in sub_chunks]}",
    )

    cs_chunks = rag_service.retrieve_chunks("二叉树怎么学")
    check(
        "跨目录检索命中 cs 文件",
        bool(cs_chunks) and cs_chunks[0]["source"] == "cs/algo/数据结构.txt",
        f"sources={[chunk['source'] for chunk in cs_chunks]}",
    )

    fixture_chunks = rag_service.load_chunks()
    check(
        "切片保留 source",
        all(chunk["source"] for chunk in fixture_chunks)
        and {chunk["source"] for chunk in fixture_chunks}
        == {"README.txt", "cs/algo/数据结构.txt", "math/定积分.txt"},
    )
    check(
        "context 仍为 content 拼接",
        rag_service.retrieve_context("定积分怎么学")
        == "\n".join(chunk["content"] for chunk in sub_chunks),
    )
finally:
    rag_service.KNOWLEDGE_PATH = original_knowledge_path
    shutil.rmtree(fixture_root, ignore_errors=True)

check("测试夹具已清理", not fixture_root.exists())

# 10. embedding 不可用时优雅降级：/ask 仍要能回答，只是没有资料
original_embed_url = embedding_service.OLLAMA_EMBED_URL
embedding_service.OLLAMA_EMBED_URL = "http://127.0.0.1:1/api/embed"
captured.clear()
main.call_ollama = fake_call_ollama
try:
    check("embedding 不可用时检索返回空列表", rag_service.retrieve_chunks("定积分怎么学") == [])
    degraded_response = main.ask("定积分怎么学")
finally:
    embedding_service.OLLAMA_EMBED_URL = original_embed_url
    main.call_ollama = original_call_ollama

check("降级时 /ask 仍返回答案", degraded_response["answer"] == "stub answer")
check("降级时 sources 为空", degraded_response["sources"] == [])
check("降级时 prompt 直接使用问题", captured.get("prompt") == "定积分怎么学")

print("\n全部用例通过")
