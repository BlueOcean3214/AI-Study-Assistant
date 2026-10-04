"""rag_service 回归测试：直接运行 python test_rag_service.py 即可。

覆盖此前的线上问题：GET /ask?question=定积分怎么学 时 RAG 长度为 0。
"""

import ai_service
import main
import rag_service


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


# 1. 核心回归：中文长句（无空格）必须能召回知识库
context = rag_service.retrieve_context("定积分怎么学")
check("中文无空格查询可召回", len(context) > 0, f"len={len(context)}")
check("召回内容包含关键词", "定积分" in context)

# 2. 兼容原有行为：带空格、纯关键词、英文混排
for query in ("定积分 怎么学", "定积分", "定积分 换元积分法"):
    ctx = rag_service.retrieve_context(query)
    check(f"查询可用: {query!r}", len(ctx) > 0, f"len={len(ctx)}")

# 3. 知识库外的问题不应被误召回（单字“一/部”命中曾被误判为相关）
for query in ("今天天气怎么样", "英语单词怎么背", "推荐一部电影", "怎么"):
    ctx = rag_service.retrieve_context(query)
    check(f"无关问题不误召回: {query!r}", ctx == "", f"len={len(ctx)}")

# 3b. 兜底召回：关键词片段全不命中但字符重合度高时仍可用（如错别字）
ctx = rag_service.retrieve_context("定学")
check("字符重合度兜底可召回", len(ctx) > 0, f"len={len(ctx)}")

# 4. 空查询与非法输入
for query in ("", "   ", None):
    check(f"空查询返回空串: {query!r}", rag_service.retrieve_context(query) == "")

# 5. 文档加载：按文件名排序且非空
documents = rag_service.load_documents()
check("知识库可加载", len(documents) > 0, f"docs={len(documents)}")

# 5b. v1.2 新增契约：retrieve_chunks 返回 content / source / score
chunks = rag_service.retrieve_chunks("定积分怎么学")
check("retrieve_chunks 返回 Top-K", 0 < len(chunks) <= rag_service.TOP_K, f"n={len(chunks)}")
check(
    "返回字段为 content/source/score",
    all(set(chunk) == {"content", "source", "score"} for chunk in chunks),
)
check("source 为文件名", all(chunk["source"] == "math.txt" for chunk in chunks))
check("content 非空", all(chunk["content"].strip() for chunk in chunks))
check(
    "score 降序排列",
    [chunk["score"] for chunk in chunks]
    == sorted((chunk["score"] for chunk in chunks), reverse=True),
)
check("retrieve_chunks 空查询返回空列表", rag_service.retrieve_chunks("") == [])
check(
    "retrieve_context 仍只返回 content 拼接",
    rag_service.retrieve_context("定积分怎么学")
    == "\n".join(chunk["content"] for chunk in chunks),
)
check("format_context 可拼出来源标记", "来源: math.txt" in rag_service.format_context(chunks))

# 6. 端到端：/ask 的 context 必须真正进入发给大模型的 prompt
captured = {}


def fake_call_ollama(prompt):
    captured["prompt"] = prompt
    return "stub answer", None


original_call_ollama = ai_service.call_ollama
ai_service.call_ollama = fake_call_ollama
try:
    response = main.ask("定积分怎么学")
finally:
    ai_service.call_ollama = original_call_ollama

check("接口无错误", response["error"] is None, f"error={response['error']}")
check("资料已注入 prompt", "定积分" in captured.get("prompt", ""))
check("prompt 包含检索资料段", "请参考下面资料回答问题" in captured.get("prompt", ""))
check("接口返回模型内容", response["answer"] == "stub answer")

print("\n全部用例通过")
