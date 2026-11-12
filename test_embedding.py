"""Embedding 通路的契约测试：直接运行 python test_embedding.py。

背景
----
原实现是一个直接 POST `http://localhost:11434/api/embeddings`（bge-m3）的裸脚本。
迁移到云端 API 之后，本地 Ollama 不再是项目运行依赖，测试也不应该强制本机起模型。
本脚本拆成两节：

A. 打桩契约节（默认执行，无网络、无 API Key、零费用）
   用 `stub_embedding.py` 的确定性词面向量校验 Embedding 通路的**结构契约**：
   维度、数值合法性、L2 归一化、同文本稳定、不同文本可区分、批量保序、
   空文本报错、以及「同主题词相似度高于无关文本」的夹具标定。

B. 真实后端节（有可用 Embedding 后端才执行，否则打印 [SKIP] 并退出码 0）
   真实调用 `embedding_service.get_embedding`，校验：返回维度与
   EMBEDDING_DIMENSIONS 一致、同文本两次结果一致、相关文本相似度高于无关文本，
   并打印 cache_identity() 证明文档向量与查询向量共用同一模型身份。

注意：A 节的向量没有语义能力，它证明的是「通路与缓存契约」，不是「语义召回质量」；
语义质量只由 B 节（真实 API）负责，缺后端时 B 节会明确跳过，绝不假装通过。
"""

import math

import embedding_service
import real_backend
import stub_embedding


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


# ==========================
# A. 打桩契约节（无网络）
# ==========================

print("=" * 60)
print("A. 打桩契约节（stub_embedding，不联网、不需要 API Key）")
print("=" * 60)

stub_embedding.install()
stub_embedding.reset_call_count()

try:
    text1 = "定积分怎么学习"
    text2 = "积分这一章应该如何复习"
    text3 = "今天晚上吃什么"

    v1 = embedding_service.get_embedding(text1)
    v2 = embedding_service.get_embedding(text2)
    v3 = embedding_service.get_embedding(text3)

    check(
        "向量维度等于约定维度",
        len(v1) == stub_embedding.DIMENSIONS,
        f"len={len(v1)} expect={stub_embedding.DIMENSIONS}",
    )
    check(
        "向量是有效数值数组",
        all(isinstance(value, float) and math.isfinite(value) for value in v1),
        f"head={[round(value, 4) for value in v1[:5]]}",
    )
    check(
        "向量已 L2 归一化",
        abs(math.sqrt(sum(value * value for value in v1)) - 1.0) < 1e-9,
        f"norm={math.sqrt(sum(value * value for value in v1)):.12f}",
    )

    v1_again = embedding_service.get_embedding(text1)
    check("同一文本两次调用结果一致", v1 == v1_again)
    check("不同文本向量可区分", v1 != v3)

    batch = embedding_service.get_embeddings([text1, text2, text3])
    check("批量返回条数与输入一致", len(batch) == 3, f"n={len(batch)}")
    check(
        "批量结果保序且与单条一致",
        batch == [v1, v2, v3],
        f"calls={stub_embedding.call_count()}",
    )
    check("空列表返回空列表", embedding_service.get_embeddings([]) == [])

    empty_error = None

    try:
        embedding_service.get_embedding("   ")
    except embedding_service.EmbeddingError as error:
        empty_error = error

    check("空文本抛 EmbeddingError", empty_error is not None, f"error={empty_error}")

    similarity_related = cosine_similarity(v1, v2)
    similarity_unrelated = cosine_similarity(v1, v3)

    info("定积分 vs 积分复习", f"cos={similarity_related:.4f}（打桩词面 + 主题维）")
    info("定积分 vs 吃什么", f"cos={similarity_unrelated:.4f}（打桩词面 + 主题维）")

    check(
        "夹具标定：相关文本相似度高于无关文本",
        similarity_related > similarity_unrelated,
        f"related={similarity_related:.4f} unrelated={similarity_unrelated:.4f}",
    )
finally:
    stub_embedding.uninstall()


# ==========================
# B. 真实后端节（可选）
# ==========================

print()
print("=" * 60)
print("B. 真实 Embedding 后端（只认云端 openai_compatible，本地 Ollama 不算）")
print("=" * 60)

if not real_backend.cloud_embedding_ready():
    status = real_backend.cloud_embedding_status()
    print(
        f"[SKIP] 真实 Embedding 后端未执行：provider={status['provider']} "
        f"model={status['model']} url={status['base_url']} "
        f"api_key={status['api_key']} 原因={status['reason']}"
    )
    print(
        "[SKIP] 配置 EMBEDDING_PROVIDER=openai_compatible + EMBEDDING_API_KEY + "
        "EMBEDDING_BASE_URL + EMBEDDING_MODEL 后重跑；本地 Ollama 不计入本次 API 验收。"
    )
else:
    settings = embedding_service.get_settings()
    identity = embedding_service.cache_identity()

    info("Embedding 后端", f"provider={settings.provider} model={settings.model} url={settings.base_url}")
    info("缓存身份 cache_identity()", identity)

    check(
        "真实后端 Provider 是云端 openai_compatible",
        settings.provider == embedding_service.PROVIDER_OPENAI,
        f"provider={settings.provider}",
    )
    check(
        "真实后端 base_url 不是本机地址",
        "localhost" not in str(settings.base_url) and "127.0.0.1" not in str(settings.base_url),
        f"url={settings.base_url}",
    )

    expected_identity = f"{settings.provider}:{settings.model}@{settings.dimensions or 'default'}"

    check(
        "缓存身份为 provider:model@dim（文档与查询向量同一身份）",
        identity == expected_identity,
        f"identity={identity}",
    )
    check(
        "缓存身份在多次调用间稳定",
        identity == embedding_service.cache_identity(),
        f"identity={identity}",
    )

    real_v1 = embedding_service.get_embedding("定积分怎么学习")
    real_v2 = embedding_service.get_embedding("积分这一章应该如何复习")
    real_v3 = embedding_service.get_embedding("今天晚上吃什么")
    real_v1_again = embedding_service.get_embedding("定积分怎么学习")

    info("真实向量维度", f"len={len(real_v1)}")
    info("真实向量前 5 位", str([round(value, 4) for value in real_v1[:5]]))

    check("真实向量为有效数值数组", all(math.isfinite(value) for value in real_v1))

    if settings.dimensions is not None:
        check(
            "真实向量维度与 EMBEDDING_DIMENSIONS 一致",
            len(real_v1) == settings.dimensions,
            f"len={len(real_v1)} expect={settings.dimensions}",
        )
    else:
        info("维度校验", "EMBEDDING_DIMENSIONS=0（用模型默认维度），跳过维度一致性断言")

    check(
        "真实后端：同文本两次结果一致（可安全缓存）",
        real_v1 == real_v1_again,
        "",
    )

    real_related = cosine_similarity(real_v1, real_v2)
    real_unrelated = cosine_similarity(real_v1, real_v3)

    info("真实：定积分 vs 积分复习", f"cos={real_related:.4f}")
    info("真实：定积分 vs 吃什么", f"cos={real_unrelated:.4f}")

    check(
        "真实后端：相关文本相似度高于无关文本（语义能力）",
        real_related > real_unrelated,
        f"related={real_related:.4f} unrelated={real_unrelated:.4f}",
    )

print()
print("全部 Embedding 契约测试通过")
