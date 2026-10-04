"""Embedding 批处理 / 缓存 / 错误处理测试：直接运行 python test_embedding_batching.py。

需要 Ollama + bge-m3（第 2、3、8 节会真实调用 embedding）；
第 6、7 节用假向量，不联网，只为验证分批与失败处理。

对应任务要求：
1. 10 个 chunk 正常 embedding
2. 100 个 chunk 不会作为单个超大请求发送
3. 100+ chunk 被拆成多个 batch
4. 第二次检索命中缓存，不重复 embedding
5. 新增 chunk 只请求新 chunk
6. 现有 RAG 测试继续通过（见 test_rag_service.py）
7. 现有上传测试继续通过（见 test_upload.py）
8. 1000+ chunk 压测
"""

import contextlib
import time

import embedding_service
import rag_service


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def make_chunks(count, prefix="chunk"):
    """造 count 条互不相同的 chunk。"""

    return [
        {"content": f"{prefix}-{index} 的专属内容，编号 {index}", "source": "synthetic.txt"}
        for index in range(count)
    ]


@contextlib.contextmanager
def patched(**attributes):
    originals = {name: getattr(rag_service, name) for name in attributes}
    for name, value in attributes.items():
        setattr(rag_service, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(rag_service, name, value)


def make_spy(inner):
    """包一层，记录每次请求里发了多少条文本。"""

    calls = []

    def spy(texts):
        calls.append(list(texts))
        return inner(texts)

    return spy, calls


def fake_vector(text, dim=4096):
    """假向量：位置敏感，相同文本得到完全相同的向量。

    dim 取大一些，避免不同文本凑出平行向量导致余弦并列 1.0（dim=64 时出现过）。
    """

    vector = [0.0] * dim
    for position, char in enumerate(text):
        vector[(ord(char) * 31 + position) % dim] += 1.0
    return vector


def fake_embeddings(texts):
    return [fake_vector(text) for text in texts]


def fake_embedding(text):
    """query 必须走同一套假向量，否则 query 与 chunk 维度不一致、相似度无意义。"""

    return fake_vector(text)


# 0. 前置检查
try:
    embedding_service.get_embedding("前置检查")
except embedding_service.EmbeddingError as error:
    raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

# 1. 分批工具本身
check(
    "batch size 是明确的小批量",
    1 <= embedding_service.EMBEDDING_BATCH_SIZE <= 64,
    f"EMBEDDING_BATCH_SIZE={embedding_service.EMBEDDING_BATCH_SIZE}",
)
sizes = [len(batch) for batch in embedding_service.split_into_batches(list(range(100)))]
check("100 条切成多批", sizes == [32, 32, 32, 4], f"sizes={sizes}")
check(
    "单条 batch_size 生效",
    [len(batch) for batch in embedding_service.split_into_batches([1, 2, 3], batch_size=1)] == [1, 1, 1],
)
check("空输入不产生批次", list(embedding_service.split_into_batches([])) == [])
try:
    list(embedding_service.split_into_batches([1], batch_size=0))
    check("batch_size 非法时报错", False)
except ValueError:
    check("batch_size 非法时报错", True)

# 2. 10 个 chunk：真实 embedding 正常，且只发一次请求
rag_service._EMBEDDING_CACHE.clear()
ten_chunks = make_chunks(10)
ten_query = ten_chunks[3]["content"]
spy, calls = make_spy(rag_service.get_embeddings)
with patched(load_chunks=lambda: ten_chunks, get_embeddings=spy):
    results = rag_service.retrieve_chunks(ten_query)

check("10 个 chunk 能正常检索", bool(results), f"n={len(results)}")
check("10 个 chunk 只发一次请求", len(calls) == 1, f"calls={len(calls)}")
check("返回结构不变", all(set(item) == {"content", "source", "score"} for item in results))
check("Top-K 不超过 3", len(results) <= rag_service.TOP_K, f"n={len(results)}")
check("相似度最高的就是查询对应的 chunk", results[0]["content"] == ten_query, f"score={results[0]['score']}")

# 3. 100 个 chunk：必须拆成多批，不能一次发出去
rag_service._EMBEDDING_CACHE.clear()
hundred_chunks = make_chunks(100)
spy_batch, batch_calls = make_spy(rag_service.get_embeddings)
with patched(load_chunks=lambda: hundred_chunks, get_embeddings=spy_batch):
    batch_results = rag_service.retrieve_chunks(hundred_chunks[7]["content"])

check("100 个 chunk 分批请求", len(batch_calls) == 4, f"calls={len(batch_calls)}")
check(
    "没有一次性发送 100 条",
    max(len(batch) for batch in batch_calls) <= embedding_service.EMBEDDING_BATCH_SIZE,
    f"max_batch={max(len(batch) for batch in batch_calls)}",
)
check(
    "每批都不超过 batch size",
    [len(batch) for batch in batch_calls] == [32, 32, 32, 4],
    f"sizes={[len(batch) for batch in batch_calls]}",
)
check("分批后检索结果不变", bool(batch_results) and batch_results[0]["content"] == hundred_chunks[7]["content"])

# 4. 缓存命中：第二次检索不再请求 embedding
spy_warm, warm_calls = make_spy(rag_service.get_embeddings)
with patched(load_chunks=lambda: hundred_chunks, get_embeddings=spy_warm):
    rag_service.retrieve_chunks(hundred_chunks[7]["content"])

check("第二次检索命中缓存", len(warm_calls) == 0, f"calls={len(warm_calls)}")
check("缓存条数等于 chunk 数", len(rag_service._EMBEDDING_CACHE) >= 100, f"cached={len(rag_service._EMBEDDING_CACHE)}")

# 4b. 内容相同但 source 不同：复用同一个向量，只请求一次
rag_service._EMBEDDING_CACHE.clear()
same_content = [
    {"content": "重复内容 abcdefg", "source": "a.txt"},
    {"content": "重复内容 abcdefg", "source": "b.txt"},
]
spy_dup, dup_calls = make_spy(rag_service.get_embeddings)
with patched(load_chunks=lambda: same_content, get_embeddings=spy_dup):
    rag_service.retrieve_chunks("重复内容 abcdefg")

check("相同正文只请求一次", sum(len(batch) for batch in dup_calls) == 1, f"total={sum(len(b) for b in dup_calls)}")

# 5. 新增一个 chunk：只请求新的那条
# 先预热原有 100 条（假向量，不联网），再模拟“知识库新增文件”
rag_service._EMBEDDING_CACHE.clear()
with patched(
    load_chunks=lambda: hundred_chunks,
    get_embeddings=fake_embeddings,
    get_embedding=fake_embedding,
):
    rag_service.retrieve_chunks(hundred_chunks[0]["content"])

added_chunks = hundred_chunks + [{"content": "全新加入的知识点 xyz", "source": "new.txt"}]
spy_new, new_calls = make_spy(rag_service.get_embeddings)
with patched(
    load_chunks=lambda: added_chunks,
    get_embeddings=spy_new,
    get_embedding=fake_embedding,
):
    rag_service.retrieve_chunks(added_chunks[-1]["content"])

check("新增 chunk 只请求 1 条", sum(len(batch) for batch in new_calls) == 1, f"total={sum(len(b) for b in new_calls)}")
check("新增 chunk 立刻可检索", rag_service._EMBEDDING_CACHE.get("全新加入的知识点 xyz") is not None)

# 6. 某一批失败：报错带批次信息、保留已成功的缓存、不写伪向量、可重试
rag_service._EMBEDDING_CACHE.clear()
fail_chunks = make_chunks(100)
attempts = {"count": 0}


def flaky_embeddings(texts):
    attempts["count"] += 1
    if attempts["count"] == 2:
        raise embedding_service.EmbeddingError("模拟第 2 批失败")
    return [fake_vector(text) for text in texts]


with patched(
    load_chunks=lambda: fail_chunks,
    get_embeddings=flaky_embeddings,
    get_embedding=fake_embedding,
):
    failed_result = rag_service.retrieve_chunks("随便问一个问题")

check("失败时检索返回空（不把半成品当完整知识库）", failed_result == [], f"n={len(failed_result)}")
check("失败前的批次已保留在缓存", len(rag_service._EMBEDDING_CACHE) == 32, f"cached={len(rag_service._EMBEDDING_CACHE)}")
check(
    "失败的批次内容没有写入伪向量",
    not any(fail_chunks[32]["content"] == text for text in rag_service._EMBEDDING_CACHE),
)

# 直接调用底层函数，检查异常信息
rag_service._EMBEDDING_CACHE.clear()
attempts["count"] = 0
try:
    with patched(
        load_chunks=lambda: fail_chunks,
        get_embeddings=flaky_embeddings,
        get_embedding=fake_embedding,
    ):
        rag_service._chunk_vectors(fail_chunks)
    check("失败批次抛出 EmbeddingError", False)
except embedding_service.EmbeddingError as error:
    check("失败批次抛出 EmbeddingError", True)
    check("错误信息指出是哪一批", "第 2/" in str(error), f"msg={str(error)[:60]}")
    check("错误信息带本批条数", "本批 32 条" in str(error), f"msg={str(error)[:60]}")

check("成功后缓存了第 1 批", len(rag_service._EMBEDDING_CACHE) == 32, f"cached={len(rag_service._EMBEDDING_CACHE)}")

# 重试：只补没成功的部分
spy_retry, retry_calls = make_spy(fake_embeddings)
with patched(
    load_chunks=lambda: fail_chunks,
    get_embeddings=spy_retry,
    get_embedding=fake_embedding,
):
    retry_result = rag_service.retrieve_chunks(fail_chunks[70]["content"])

retried_texts = [text for batch in retry_calls for text in batch]
check("重试只处理未成功的 chunk", len(retried_texts) == 68, f"retried={len(retried_texts)}")
check("重试不再请求已缓存的 chunk", fail_chunks[0]["content"] not in retried_texts)
check("重试后检索恢复", bool(retry_result) and retry_result[0]["content"] == fail_chunks[70]["content"],
      f"top={retry_result[0]['content'] if retry_result else None}")

# 7. 1200 chunk 压测（假向量，不联网）
rag_service._EMBEDDING_CACHE.clear()
big_chunks = make_chunks(1200, prefix="big")
spy_big, big_calls = make_spy(fake_embeddings)
big_query = big_chunks[777]["content"]
with patched(
    load_chunks=lambda: big_chunks,
    get_embeddings=spy_big,
    get_embedding=fake_embedding,
):
    big_results = rag_service.retrieve_chunks(big_query)

expected_batches = -(-1200 // embedding_service.EMBEDDING_BATCH_SIZE)
check("1200 chunk 按批处理", len(big_calls) == expected_batches, f"calls={len(big_calls)} expected={expected_batches}")
check(
    "没有任何一次请求包含全部 1200 条",
    all(len(batch) < 1200 for batch in big_calls),
    f"max_batch={max(len(b) for b in big_calls)}",
)
check(
    "每一批都不超过 batch size",
    max(len(batch) for batch in big_calls) <= embedding_service.EMBEDDING_BATCH_SIZE,
    f"max_batch={max(len(b) for b in big_calls)}",
)
check("压测后 Top-K 结构不变", all(set(item) == {"content", "source", "score"} for item in big_results))
check("压测后 Top-K 仍正确", len(big_results) <= 3 and big_results[0]["content"] == big_query,
      f"top={big_results[0]['content'] if big_results else None}")

spy_big2, big_calls2 = make_spy(fake_embeddings)
with patched(
    load_chunks=lambda: big_chunks,
    get_embeddings=spy_big2,
    get_embedding=fake_embedding,
):
    rag_service.retrieve_chunks(big_query)
check("压测第二次检索全部命中缓存", len(big_calls2) == 0, f"calls={len(big_calls2)}")

# 8. 真实 embedding：512 chunk（正是之前会失败的规模）
rag_service._EMBEDDING_CACHE.clear()
real_chunks = make_chunks(512, prefix="real")
spy_real, real_calls = make_spy(rag_service.get_embeddings)
started = time.time()
with patched(load_chunks=lambda: real_chunks, get_embeddings=spy_real):
    real_results = rag_service.retrieve_chunks(real_chunks[300]["content"])
elapsed = time.time() - started

check("512 chunk 真实 embedding 成功", bool(real_results), f"n={len(real_results)}")
check(
    "512 chunk 拆成 16 批",
    len(real_calls) == -(-512 // embedding_service.EMBEDDING_BATCH_SIZE),
    f"calls={len(real_calls)}",
)
check(
    "512 chunk 每批不超过 batch size",
    max(len(batch) for batch in real_calls) <= embedding_service.EMBEDDING_BATCH_SIZE,
    f"max_batch={max(len(batch) for batch in real_calls)}",
)
check("512 chunk 检索结果正确", real_results[0]["content"] == real_chunks[300]["content"],
      f"score={real_results[0]['score']}")
print(f"INFO: 512 条真实 embedding 用时 {elapsed:.1f} 秒（批大小 {embedding_service.EMBEDDING_BATCH_SIZE}）")

# 9. retrieve_context 仍然返回字符串
rag_service._EMBEDDING_CACHE.clear()
with patched(load_chunks=lambda: ten_chunks):
    context = rag_service.retrieve_context(ten_chunks[0]["content"])
check("retrieve_context 仍返回字符串", isinstance(context, str) and len(context) > 0, f"len={len(context)}")

print("\n全部用例通过")
