"""document_service 测试：直接运行 python test_document_service.py。

不需要 Ollama；只有最后一节（保存后能否被语义检索到）在 embedding
不可用时会打印 SKIP。

覆盖：文件名安全校验、编码兼容（UTF-8 / BOM / GBK）、切片规则与长度上限、
保存/覆盖/幂等、递归读取、以及 rag_service 的向后兼容。
"""

import shutil
from pathlib import Path

import document_service
import rag_service
import vector_cache

# 测试隔离：向量缓存写到临时目录，避免污染真实 .rag_cache/
CACHE_FIXTURE = Path(__file__).resolve().parent / "_test_cache_document"
shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)
vector_cache.CACHE_DIR = CACHE_FIXTURE


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def expect_error(name, func):
    try:
        func()
    except document_service.DocumentError as error:
        check(name, True, f"-> {error}")
    else:
        check(name, False, "没有抛出 DocumentError")


fixture = Path(__file__).resolve().parent / "_test_document_tmp"
if fixture.exists():
    shutil.rmtree(fixture)

try:
    # 1. 文件名安全校验
    check(
        "正常文件名通过",
        document_service.sanitize_filename("定积分.txt") == "定积分.txt",
    )
    check(
        "去掉首尾空格且后缀大小写不敏感",
        document_service.sanitize_filename("  notes.TXT  ") == "notes.TXT",
    )
    for bad_name in (
        "a/b.txt",
        "..\\evil.txt",
        "../evil.txt",
        "notes.md",
        ".hidden.txt",
        "a:b.txt",
        "",
        "   ",
        None,
        ".txt",
        "x" * 200 + ".txt",
    ):
        expect_error(f"拒绝非法文件名 {bad_name!r}", lambda n=bad_name: document_service.sanitize_filename(n))

    # 2. 编码兼容
    documents_dir = fixture / "docs"
    documents_dir.mkdir(parents=True)

    (documents_dir / "utf8.txt").write_bytes("定积分 UTF-8".encode("utf-8"))
    (documents_dir / "bom.txt").write_bytes("定积分 BOM".encode("utf-8-sig"))
    (documents_dir / "gbk.txt").write_bytes("定积分 GBK".encode("gbk"))
    (documents_dir / "broken.txt").write_bytes(b"\xff\xfe\xff\xfe")

    check("读取 UTF-8", document_service.read_text(documents_dir / "utf8.txt") == "定积分 UTF-8")
    check("读取 UTF-8 BOM", document_service.read_text(documents_dir / "bom.txt") == "定积分 BOM")
    check("读取 GBK", document_service.read_text(documents_dir / "gbk.txt") == "定积分 GBK")
    expect_error("损坏文件读取失败", lambda: document_service.read_text(documents_dir / "broken.txt"))

    loaded_sources = sorted(document["source"] for document in document_service.load_documents(fixture))
    check(
        "坏文件被跳过、好文件仍可读",
        loaded_sources == ["docs/bom.txt", "docs/gbk.txt", "docs/utf8.txt"],
        f"sources={loaded_sources}",
    )

    # 3. 切片规则
    text = "标题一：\n内容一。\n\n标题二：\n内容二。"
    heading_chunks = document_service.split_into_chunks(text)
    check("标题开启新 chunk", heading_chunks == ["标题一：\n内容一。", "标题二：\n内容二。"],
          f"chunks={heading_chunks}")

    long_text = "定积分是微积分的重要内容，用于计算面积。" * 60
    long_chunks = document_service.split_into_chunks(long_text)
    check("长文本被切成多片", len(long_chunks) >= 2, f"n={len(long_chunks)}")
    check(
        "每片不超过 CHUNK_MAX_CHARS",
        all(len(chunk) <= document_service.CHUNK_MAX_CHARS for chunk in long_chunks),
        f"max={max(len(c) for c in long_chunks)} limit={document_service.CHUNK_MAX_CHARS}",
    )

    chunked = document_service.chunk_text(long_text, "定积分.txt")
    check(
        "chunk_text 返回 content/source",
        all(set(chunk) == {"content", "source"} for chunk in chunked)
        and all(chunk["source"] == "定积分.txt" for chunk in chunked),
        f"n={len(chunked)}",
    )
    expect_error("非字符串切片报错", lambda: document_service.split_into_chunks(123))

    # 4. 保存文档
    math_source = document_service.save_document(
        "线性代数.txt", "矩阵乘法需要行乘以列。\n\n特征值用于判断矩阵是否可对角化。", directory=fixture
    )
    check("保存返回 source", math_source == "线性代数.txt", f"source={math_source}")
    check("文件已写入知识库", (fixture / "线性代数.txt").exists())
    check(
        "写入内容可读回",
        "矩阵乘法" in document_service.read_text(fixture / "线性代数.txt"),
    )

    check(
        "重复上传相同内容幂等",
        document_service.save_document("线性代数.txt", "矩阵乘法需要行乘以列。\n\n特征值用于判断矩阵是否可对角化。", directory=fixture)
        == "线性代数.txt",
    )
    expect_error(
        "内容不同时拒绝覆盖",
        lambda: document_service.save_document("线性代数.txt", "完全不同的内容。", directory=fixture),
    )
    check(
        "overwrite=True 可以覆盖",
        document_service.save_document("线性代数.txt", "覆盖后的内容。", directory=fixture, overwrite=True)
        == "线性代数.txt"
        and "覆盖后" in document_service.read_text(fixture / "线性代数.txt"),
    )

    check(
        "bytes 输入（UTF-8）",
        document_service.save_document("cs.txt", "二叉树是常用的数据结构。".encode("utf-8"), directory=fixture)
        == "cs.txt",
    )
    check(
        "bytes 输入（GBK）转存为 UTF-8",
        document_service.save_document("物理.txt", "牛顿第二定律 F=ma。".encode("gbk"), directory=fixture)
        == "物理.txt"
        and "牛顿第二定律" in document_service.read_text(fixture / "物理.txt"),
    )
    expect_error("空内容不保存", lambda: document_service.save_document("empty.txt", "   ", directory=fixture))
    expect_error(
        "非法文件名不写盘",
        lambda: document_service.save_document("../escape.txt", "x", directory=fixture),
    )
    check("目录穿越未生效", not (fixture.parent / "escape.txt").exists())

    try:
        document_service.save_document("bad.txt", 12345, directory=fixture)
        check("content 类型校验", False)
    except document_service.DocumentError:
        check("content 类型校验", True)

    # 5. load_documents / load_chunks 结构
    saved_documents = document_service.load_documents(fixture)
    check(
        "load_documents 结构为 content/source",
        all(set(document) == {"content", "source"} for document in saved_documents),
    )
    saved_chunks = document_service.load_chunks(fixture)
    check(
        "load_chunks 保留 source",
        all(set(chunk) == {"content", "source"} for chunk in saved_chunks)
        and {"线性代数.txt", "cs.txt", "物理.txt", "docs/utf8.txt", "docs/bom.txt", "docs/gbk.txt"}
        <= {chunk["source"] for chunk in saved_chunks},
        f"sources={sorted({chunk['source'] for chunk in saved_chunks})}",
    )

    # 6. rag_service 向后兼容 + 保存后即可检索
    original_knowledge_path = rag_service.KNOWLEDGE_PATH
    rag_service.KNOWLEDGE_PATH = fixture
    try:
        compatible_documents = rag_service.load_documents()
        compatible_chunks = rag_service.load_chunks()
        check(
            "rag_service.load_documents 仍可用",
            len(compatible_documents) == len(saved_documents),
            f"docs={len(compatible_documents)}",
        )
        check(
            "rag_service.load_chunks 仍可用",
            len(compatible_chunks) == len(saved_chunks),
            f"chunks={len(compatible_chunks)}",
        )
        check(
            "rag_service.split_into_chunks 仍可用",
            rag_service.split_into_chunks("标题：\n内容。") == ["标题：\n内容。"],
        )
        check(
            "新保存的文件进入 rag_service 知识库",
            any(chunk["source"] == "线性代数.txt" for chunk in compatible_chunks),
        )

        # 用一个只有一份新文档的干净目录做检索集成（模拟“上传后立即可检索”）
        retrieval_root = fixture / "kb"
        retrieval_root.mkdir(parents=True)
        document_service.save_document(
            "矩阵乘法.txt",
            "矩阵乘法需要行乘以列。\n\n特征值用于判断矩阵是否可对角化。",
            directory=retrieval_root,
        )
        rag_service.KNOWLEDGE_PATH = retrieval_root

        check(
            "上传后的文件就在检索范围内",
            [chunk["source"] for chunk in rag_service.load_chunks()] == ["矩阵乘法.txt"],
        )

        try:
            import embedding_service

            embedding_service.get_embedding("探测")
            embedding_ready = True
        except Exception as error:  # noqa: BLE001 - 只为区分“跳过”与“失败”
            embedding_ready = False
            print(f"SKIP: 保存文件后的语义检索（embedding 不可用：{error}）")

        if embedding_ready:
            retrieved = rag_service.retrieve_chunks("矩阵乘法怎么算")
            check(
                "保存的文件可被语义检索命中",
                bool(retrieved) and retrieved[0]["source"] == "矩阵乘法.txt",
                f"top={retrieved[0]['source'] if retrieved else None} "
                f"score={retrieved[0]['score'] if retrieved else None}",
            )
            check(
                "检索结果结构仍为 content/source/score",
                all(set(chunk) == {"content", "source", "score"} for chunk in retrieved),
            )
    finally:
        rag_service.KNOWLEDGE_PATH = original_knowledge_path
finally:
    shutil.rmtree(fixture, ignore_errors=True)

check("测试夹具已清理", not fixture.exists())

shutil.rmtree(CACHE_FIXTURE, ignore_errors=True)
check("测试缓存目录已清理", not CACHE_FIXTURE.exists())

print("\n全部用例通过")
