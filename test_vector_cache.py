"""向量缓存持久化测试：直接运行 python test_vector_cache.py。

所有用例都在临时缓存目录里跑（patch vector_cache.CACHE_DIR），
不会污染真实的 .rag_cache/。

对应任务要求：
1~3 hash 稳定/相同/不同   4 编码 round-trip      5 meta 读写
6 落盘                     7 重启恢复             8 已存在不重复请求
9 只补新增                10 内容修改失效         11 改名复用
12 模型不一致作废         13 维度不一致作废       14 坏行跳过
15 末行截断不崩           16 compact 压实         17+ 1000 chunk 规模验证
"""

import array
import contextlib
import shutil
import time
from pathlib import Path

import document_service
import embedding_service
import rag_service
import vector_cache

PROJECT_DIR = Path(__file__).resolve().parent
CACHE_FIXTURE = PROJECT_DIR / "_test_cache_fixture"
KB_FIXTURE = PROJECT_DIR / "_test_cache_kb"
REAL_CACHE_DIR = PROJECT_DIR / ".rag_cache"

REAL_CACHE_EXISTED_BEFORE = REAL_CACHE_DIR.exists()


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


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
    calls = []

    def spy(texts):
        calls.append(list(texts))
        return inner(texts)

    return spy, calls


def doc_text(count):
    """生成 count 段文本：每段首行以"："结尾，因此每段切成一个 chunk。"""

    return "\n\n".join(
        f"知识点 {index} 的标题：\n这是第 {index} 条互不相同的内容。" for index in range(count)
    )


def fake_vector(text, dim=1024):
    vector = [0.0] * dim
    for position, char in enumerate(text):
        vector[(ord(char) * 31 + position) % dim] += 1.0
    return vector


def fake_embeddings(texts):
    return [fake_vector(text) for text in texts]


def fake_embedding(text):
    return fake_vector(text)


def reset_environment():
    """把缓存目录和知识库都指到临时夹具，并清空内存状态。"""

    shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)
    shutil.rmtree(KB_FIXTURE, ignore_errors=True)
    CACHE_FIXTURE.mkdir(parents=True)
    KB_FIXTURE.mkdir(parents=True)

    vector_cache.CACHE_DIR = CACHE_FIXTURE
    rag_service.KNOWLEDGE_PATH = KB_FIXTURE
    rag_service.reset_cache()


reset_environment()

try:
    # 0. 前置检查
    try:
        embedding_service.get_embedding("前置检查")
    except embedding_service.EmbeddingError as error:
        raise SystemExit(f"embedding 服务不可用，请先启动 Ollama 并 ollama pull bge-m3：{error}")

    # 1~3. hash 稳定性 / 相同 / 不同
    first_hash = vector_cache.chunk_hash("定积分怎么学")
    check("相同正文 hash 稳定", first_hash == vector_cache.chunk_hash("定积分怎么学"))
    check("使用完整 sha256（64 位十六进制）", len(first_hash) == 64 and all(c in "0123456789abcdef" for c in first_hash))
    check("不同正文 hash 不同", first_hash != vector_cache.chunk_hash("定积分怎么学 "))
    check("空正文也能算 hash", len(vector_cache.chunk_hash("")) == 64)

    # 归一化一致性：同一内容用 LF / CRLF 落盘，切片后 hash 必须一致
    lf_dir = CACHE_FIXTURE / "lf"
    crlf_dir = CACHE_FIXTURE / "crlf"
    lf_dir.mkdir()
    crlf_dir.mkdir()
    text = "标题一：\n内容一。\n\n标题二：\n内容二。"
    (lf_dir / "a.txt").write_bytes(text.replace("\n", "\n").encode("utf-8"))
    (crlf_dir / "a.txt").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    lf_hashes = sorted(
        vector_cache.chunk_hash(chunk["content"])
        for chunk in document_service.load_chunks(lf_dir)
    )
    crlf_hashes = sorted(
        vector_cache.chunk_hash(chunk["content"])
        for chunk in document_service.load_chunks(crlf_dir)
    )
    check("换行符差异不产生重复向量", lf_hashes == crlf_hashes, f"n={len(lf_hashes)}")

    # 4. 向量编码 round-trip
    real_vector = embedding_service.get_embedding("定积分怎么学")
    encoded = vector_cache.encode_vector(real_vector)
    decoded = vector_cache.decode_vector(encoded)
    check("round-trip 维度一致", len(decoded) == len(real_vector), f"dim={len(decoded)}")
    check(
        "round-trip 余弦没有明显变化",
        rag_service._cosine_similarity(real_vector, decoded) > 0.999999,
        f"cos={rag_service._cosine_similarity(real_vector, decoded):.9f}",
    )
    check(
        "round-trip 数值基本一致",
        max(abs(a - b) for a, b in zip(real_vector, decoded)) < 1e-5,
    )
    check("解码结果是 float32 数组", isinstance(decoded, array.array))

    # 5. meta 正常保存和读取
    meta = vector_cache.build_meta(
        model=embedding_service.EMBEDDING_MODEL, dim=1024, model_digest="test-digest", chunk_max_chars=400
    )
    check("meta 写入成功", vector_cache.write_meta(meta) is True)
    loaded_meta = vector_cache.read_meta()
    check(
        "meta 读取一致",
        loaded_meta["model"] == embedding_service.EMBEDDING_MODEL
        and loaded_meta["dim"] == 1024
        and loaded_meta["format"] == vector_cache.FORMAT
        and loaded_meta["vector_encoding"] == "base64-float32",
        f"meta={loaded_meta['model']}/{loaded_meta['dim']}",
    )
    check(
        "meta 原子写入没有残留临时文件",
        not (CACHE_FIXTURE / "meta.json.tmp").exists(),
    )

    # 6. 第一次 embedding 后落盘
    reset_environment()
    (KB_FIXTURE / "a.txt").write_text(doc_text(10), encoding="utf-8")

    spy_first, first_calls = make_spy(rag_service.get_embeddings)
    with patched(get_embeddings=spy_first):
        first_result = rag_service.retrieve_chunks("知识点 3 的内容")

    check("首次检索有结果", bool(first_result), f"n={len(first_result)}")
    check("首次检索请求 1 批（10 条 <= 32）", len(first_calls) == 1, f"calls={len(first_calls)}")
    check("向量已落盘", vector_cache.vectors_path().exists())
    line_count = len(vector_cache.vectors_path().read_text(encoding="utf-8").strip().splitlines())
    check("jsonl 行数等于 chunk 数", line_count == 10, f"lines={line_count}")
    disk_meta = vector_cache.read_meta()
    check(
        "meta 记录了模型和维度",
        disk_meta["model"] == embedding_service.EMBEDDING_MODEL and disk_meta["dim"] == 1024,
        f"{disk_meta['model']}/{disk_meta['dim']}",
    )
    check("meta 记录了 chunk_max_chars", disk_meta["chunk_max_chars"] == 400)

    # 7. 模拟程序重启：清空内存后重新加载磁盘缓存
    before_restart = {key: list(value) for key, value in rag_service._EMBEDDING_CACHE.items()}
    rag_service.reset_cache()  # 只清内存，不加载
    check("重启后内存缓存为空", rag_service._EMBEDDING_CACHE == {})

    spy_after, after_calls = make_spy(rag_service.get_embeddings)
    with patched(get_embeddings=spy_after):
        started = time.time()
        rag_service.ensure_cache_loaded()
        recovery_seconds = time.time() - started
        second_result = rag_service.retrieve_chunks("知识点 3 的内容")

    check("重启后不再请求 embedding", len(after_calls) == 0, f"calls={len(after_calls)}")
    check("重启后检索结果不变", bool(second_result) and second_result[0]["content"] == first_result[0]["content"])
    check(
        "重启后向量与重启前完全一致",
        set(before_restart) == set(rag_service._EMBEDDING_CACHE)
        and all(before_restart[key] == list(rag_service._EMBEDDING_CACHE[key]) for key in before_restart),
        f"restored={len(rag_service._EMBEDDING_CACHE)} recovery={recovery_seconds * 1000:.1f}ms",
    )

    # 8. 已存在的 chunk 不重复调用 embedding（不重启也一样）
    spy_repeat, repeat_calls = make_spy(rag_service.get_embeddings)
    with patched(get_embeddings=spy_repeat):
        rag_service.retrieve_chunks("知识点 8 的内容")
    check("热缓存检索 0 请求", len(repeat_calls) == 0, f"calls={len(repeat_calls)}")

    # 9. 新增 chunk 只请求新增部分
    (KB_FIXTURE / "new.txt").write_text("新增知识点的标题：\n这是全新加入的内容 xyz。", encoding="utf-8")
    spy_new, new_calls = make_spy(rag_service.get_embeddings)
    with patched(get_embeddings=spy_new):
        rag_service.retrieve_chunks("全新加入的内容 xyz")
    check(
        "新增 chunk 只请求 1 条",
        sum(len(batch) for batch in new_calls) == 1,
        f"total={sum(len(b) for b in new_calls)}",
    )

    # 10. 文件内容修改：旧 hash 不命中，重新 embedding
    reset_environment()
    (KB_FIXTURE / "a.txt").write_text("原始标题：\n这是原始内容。", encoding="utf-8")
    with patched(get_embeddings=fake_embeddings, get_embedding=fake_embedding):
        rag_service.retrieve_chunks("原始内容")
    old_hash = vector_cache.chunk_hash("原始标题：\n这是原始内容。")
    check("修改前的 hash 已在缓存里", old_hash in rag_service._EMBEDDING_CACHE)

    (KB_FIXTURE / "a.txt").write_text("修改后的标题：\n这是修改后的内容。", encoding="utf-8")
    new_hash = vector_cache.chunk_hash("修改后的标题：\n这是修改后的内容。")
    spy_modify, modify_calls = make_spy(fake_embeddings)
    with patched(get_embeddings=spy_modify, get_embedding=fake_embedding):
        rag_service.retrieve_chunks("修改后的内容")

    requested = [text for batch in modify_calls for text in batch]
    check("内容修改后重新 embedding", requested == ["修改后的标题：\n这是修改后的内容。"], f"requested={requested}")
    check("新 hash 已缓存", new_hash in rag_service._EMBEDDING_CACHE)
    check("旧 hash 暂时保留（不自动重写缓存）", old_hash in rag_service._EMBEDDING_CACHE)

    # 11. 文件改名但内容不变：复用原向量
    (KB_FIXTURE / "a.txt").rename(KB_FIXTURE / "renamed.txt")
    rag_service.reset_cache()
    spy_rename, rename_calls = make_spy(fake_embeddings)
    with patched(get_embeddings=spy_rename, get_embedding=fake_embedding):
        renamed_result = rag_service.retrieve_chunks("修改后的标题：\n这是修改后的内容。")
    check("改名后复用向量", len(rename_calls) == 0, f"calls={len(rename_calls)}")
    check("改名后检索结果正常", bool(renamed_result) and renamed_result[0]["source"] == "renamed.txt")

    # 12. 模型名称不一致：旧缓存不能使用
    reset_environment()
    (KB_FIXTURE / "a.txt").write_text("模型测试标题：\n模型测试内容。", encoding="utf-8")
    with patched(get_embeddings=fake_embeddings, get_embedding=fake_embedding):
        rag_service.retrieve_chunks("模型测试内容")

    stale_meta = vector_cache.read_meta()
    stale_meta["model"] = "some-other-model"
    vector_cache.write_meta(stale_meta)
    rag_service.reset_cache()
    rag_service.ensure_cache_loaded()
    check("模型不一致时旧缓存不加载", rag_service._EMBEDDING_CACHE == {}, f"cached={len(rag_service._EMBEDDING_CACHE)}")

    spy_model, model_calls = make_spy(fake_embeddings)
    with patched(get_embeddings=spy_model, get_embedding=fake_embedding):
        rag_service.retrieve_chunks("模型测试内容")
    check("模型不一致时重新 embedding", len(model_calls) == 1, f"calls={len(model_calls)}")
    check(
        "重新生成后 meta 已更新为新模型",
        vector_cache.read_meta()["model"] == embedding_service.EMBEDDING_MODEL,
    )
    check(
        "旧缓存已归档备份",
        list(CACHE_FIXTURE.glob("embeddings.jsonl.*.bak")) != [],
        f"bak={[p.name for p in CACHE_FIXTURE.glob('*.bak')]}",
    )

    # 13. 维度不一致：旧缓存不能使用
    # 13a: meta.dim 与向量长度不符
    reset_environment()
    (KB_FIXTURE / "a.txt").write_text("维度测试标题：\n维度测试内容。", encoding="utf-8")
    with patched(get_embeddings=fake_embeddings, get_embedding=fake_embedding):
        rag_service.retrieve_chunks("维度测试内容")

    wrong_meta = vector_cache.read_meta()
    wrong_meta["dim"] = 512
    vector_cache.write_meta(wrong_meta)
    rag_service.reset_cache()
    rag_service.ensure_cache_loaded()
    check("meta.dim 与向量长度不符时不加载", rag_service._EMBEDDING_CACHE == {})

    # 13b: 实时维度与缓存维度不符 -> 整块作废
    reset_environment()
    (KB_FIXTURE / "a.txt").write_text("维度测试标题：\n维度测试内容。", encoding="utf-8")
    with patched(get_embeddings=fake_embeddings, get_embedding=fake_embedding):
        rag_service.retrieve_chunks("维度测试内容")
    check("缓存已加载且维度为 1024", rag_service._CACHE_DIM == 1024, f"dim={rag_service._CACHE_DIM}")
    rag_service._validate_cache_dim(512)
    check("实时维度不一致时缓存作废", rag_service._EMBEDDING_CACHE == {} and rag_service._CACHE_DIM is None)

    # 14 / 15. 坏行与末行截断
    broken_path = vector_cache.vectors_path()
    good_one = vector_cache.chunk_hash("好记录一")
    good_two = vector_cache.chunk_hash("好记录二")
    with broken_path.open("w", encoding="utf-8") as handle:
        handle.write('{"h":"%s","v":"%s"}\n' % (good_one, vector_cache.encode_vector(fake_vector("好记录一"))))
        handle.write("这一行不是 JSON\n")
        handle.write('{"h":"%s","v":"%s"}\n' % (good_two, vector_cache.encode_vector(fake_vector("好记录二"))))
        handle.write('{"h":"截断记录","v":"AAAA')  # 末行截断
    loaded_vectors = vector_cache.read_vectors()
    check(
        "坏行被跳过、正常记录仍能加载",
        set(loaded_vectors) == {good_one, good_two},
        f"n={len(loaded_vectors)}",
    )

    rag_service.reset_cache()
    rag_service.ensure_cache_loaded()
    check("末行截断不影响整体加载", len(rag_service._EMBEDDING_CACHE) == 2, f"cached={len(rag_service._EMBEDDING_CACHE)}")

    # 16. compact_cache 显式压实
    keep_hash = good_one
    kept, removed = vector_cache.compact_cache([keep_hash])
    check("压实保留引用的 hash", kept == 1 and removed == 3, f"kept={kept} removed={removed}")
    check(
        "压实后文件只剩 1 行",
        len(vector_cache.vectors_path().read_text(encoding="utf-8").strip().splitlines()) == 1,
    )
    check("压实后读取只剩 1 条", set(vector_cache.read_vectors()) == {keep_hash})

    # 17. 1000+ 不同正文的规模验证（假向量，不联网，验证持久化机制）
    reset_environment()
    big_chunks = [
        {"content": f"big-{index} 的标题：\n这是第 {index} 条互不相同的正文。", "source": "big.txt"}
        for index in range(1200)
    ]

    spy_big1, big_calls1 = make_spy(fake_embeddings)
    with patched(load_chunks=lambda: big_chunks, get_embeddings=spy_big1, get_embedding=fake_embedding):
        started = time.time()
        big_first = rag_service.retrieve_chunks(big_chunks[777]["content"])
        first_seconds = time.time() - started

    first_request_count = len(big_calls1)
    first_text_count = sum(len(batch) for batch in big_calls1)
    cache_size_mb = vector_cache.vectors_path().stat().st_size / 1e6

    check("1000+ chunk 首次检索有结果", bool(big_first), f"n={len(big_first)}")
    check("首次按批请求（<=32 一批）", max(len(batch) for batch in big_calls1) <= 32, f"batches={first_request_count}")

    # 模拟程序重启
    rag_service.reset_cache()
    check("重启后内存缓存清空", rag_service._EMBEDDING_CACHE == {})

    started = time.time()
    rag_service.ensure_cache_loaded()
    recovery_seconds = time.time() - started

    spy_big2, big_calls2 = make_spy(fake_embeddings)
    with patched(load_chunks=lambda: big_chunks, get_embeddings=spy_big2, get_embedding=fake_embedding):
        big_second = rag_service.retrieve_chunks(big_chunks[777]["content"])

    check("重启后恢复 1200 条向量", len(rag_service._EMBEDDING_CACHE) == 1200, f"cached={len(rag_service._EMBEDDING_CACHE)}")
    check("重启后第二次检索 0 请求", len(big_calls2) == 0, f"calls={len(big_calls2)}")
    check("重启后检索结果一致", big_second[0]["content"] == big_first[0]["content"])

    print(
        f"INFO: 1200 chunk 首次={first_request_count} 批/{first_text_count} 条，用时 {first_seconds:.2f}s；"
        f"重启后={len(big_calls2)} 批，缓存恢复 {recovery_seconds * 1000:.1f}ms，缓存文件 {cache_size_mb:.1f}MB"
    )
finally:
    rag_service.reset_cache()
    shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)
    shutil.rmtree(KB_FIXTURE, ignore_errors=True)

check("测试夹具已清理", not CACHE_FIXTURE.exists() and not KB_FIXTURE.exists())
check(
    "没有污染真实 .rag_cache/",
    REAL_CACHE_DIR.exists() == REAL_CACHE_EXISTED_BEFORE,
    f"exists={REAL_CACHE_DIR.exists()}",
)

print("\n全部用例通过")
