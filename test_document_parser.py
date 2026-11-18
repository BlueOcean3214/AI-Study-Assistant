"""S1 文档解析 / 切片 / 来源定位测试：直接运行 python test_document_parser.py。

全程离线、合成数据：PDF 用 reportlab 现造，DOCX 用 python-docx 现造，
知识库一律落在临时目录里，结束后删除；不碰真实 knowledge/、feedback.db、
.rag_cache/ 与正式实验数据，也不调任何模型或 Embedding API。

覆盖：
1. TXT：解析结果、document_id / document_hash、chunk_id 可复现
2. PDF：物理页码、印刷页码、跨页片段来源范围
3. DOCX：标题路径、段落序号、表格行、不虚构页码
4. 解析失败：不支持的格式、空文件、编码错误、坏 PDF、无文本层、坏 DOCX
5. 上传落盘：PDF / DOCX 走 bytes 校验，坏文件不写盘，chunk_file 统计切片数
6. 检索层元数据：默认返回仍是 {content, source, score}，with_metadata=True 才透出
"""

import shutil
import tempfile
from pathlib import Path

import document_parser
import document_service
import rag_service

CHUNK_FIELDS = (
    "document_id",
    "document_hash",
    "source_name",
    "file_type",
    "chunk_id",
    "chunk_index",
    "content",
    "content_hash",
    "page_number",
    "printed_page_number",
    "heading_path",
    "paragraph_index",
    "source_spans",
    "parser_version",
    "chunker_version",
)

checks = {"pass": 0, "fail": 0, "skip": 0}


def check(name, condition, detail=""):
    if condition:
        checks["pass"] += 1
        print(f"PASS: {name} {detail}")
    else:
        checks["fail"] += 1
        print(f"FAIL: {name} {detail}")


def skip(name, reason):
    checks["skip"] += 1
    print(f"SKIP: {name}（{reason}）")


def expect_parse_error(name, expected_code, func):
    try:
        func()
    except document_parser.ParseError as error:
        check(name, error.code == expected_code, f"code={error.code}")
    except Exception as error:  # noqa: BLE001 - 只用于测试报错信息
        check(name, False, f"抛出了非 ParseError：{type(error).__name__}: {error}")
    else:
        check(name, False, "没有抛出 ParseError")


# ---------------------------------------------------------------- 造测试文件

def build_pdf(pages):
    """pages: [(正文, 页脚)] -> PDF bytes（reportlab 用内置中文字体）。"""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    try:
        pdfmetrics.getFont("STSong-Light")
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    import io

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    for body, footer in pages:
        text = pdf.beginText(50, 780)
        text.setFont("STSong-Light", 11)

        for line in body.split("\n"):
            text.textLine(line)

        if footer:
            text.textLine("")
            text.textLine(footer)

        pdf.drawText(text)
        pdf.showPage()

    pdf.save()

    return buffer.getvalue()


def build_blank_pdf():
    """只有空白页、没有文本层的 PDF（模拟扫描件）。"""

    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.showPage()
    pdf.showPage()
    pdf.save()

    return buffer.getvalue()


def build_docx():
    """标题 + 正文 + 表格的 DOCX bytes。"""

    import io

    from docx import Document

    document = Document()
    document.add_heading("第一章 导数", level=1)
    document.add_paragraph("导数是函数在某一点的变化率。")
    document.add_heading("1.1 定义", level=2)
    document.add_paragraph("若极限存在，则称函数在该点可导。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "公式"
    table.cell(0, 1).text = "含义"
    table.cell(1, 0).text = "f'(x)"
    table.cell(1, 1).text = "变化率"

    buffer = io.BytesIO()
    document.save(buffer)

    return buffer.getvalue()


def chunk_pdf(raw, name="讲义.pdf"):
    parsed = document_parser.parse_bytes(raw, name)
    return parsed, document_parser_chunks(parsed)


def document_parser_chunks(parsed):
    import document_chunker

    return document_chunker.chunk_document(parsed, max_chars=400)


workdir = Path(tempfile.mkdtemp(prefix="s1-parser-test-"))

try:
    # ------------------------------------------------------------ 1. TXT

    txt_raw = "第一章 积分\n\n定积分是面积。\n\n第二章 级数\n\n级数可以求和。\n".encode("utf-8")
    txt_path = workdir / "notes.txt"
    txt_path.write_bytes(txt_raw)

    parsed = document_parser.parse_file(txt_path, source_name="notes.txt")

    check("TXT 识别为 txt", parsed.file_type == "txt", parsed.file_type)
    check("TXT 记录原始 SHA256", len(parsed.document_hash) == 64, parsed.document_hash[:12])
    check(
        "TXT document_id 由文件名与哈希前缀构成",
        parsed.document_id == f"notes-{parsed.document_hash[:8]}",
        parsed.document_id,
    )
    check("TXT 解析版本号写入", parsed.parser_version == document_parser.PARSER_VERSION)

    txt_blocks = [block for block in parsed.blocks if block.text.strip()]
    check("TXT 空行不产生空块", all(block.text.strip() for block in parsed.blocks))
    check("TXT 整篇作为一个文本块（结构交给切片器）", len(parsed.blocks) == 1,
          [block.kind for block in parsed.blocks])

    chunks_first = document_parser_chunks(parsed)
    chunks_second = document_parser_chunks(document_parser.parse_file(txt_path, "notes.txt"))

    check("TXT 至少切出一片", len(chunks_first) >= 1, len(chunks_first))
    check("TXT chunk_index 从 0 连续递增",
          [chunk.chunk_index for chunk in chunks_first] == list(range(len(chunks_first))))
    check("TXT 两次解析 chunk_id 完全一致",
          [chunk.chunk_id for chunk in chunks_first] == [chunk.chunk_id for chunk in chunks_second])
    check("TXT chunk_id 不以数组下标命名", all("#" in chunk.chunk_id for chunk in chunks_first))
    check("TXT 每片都带 content_hash",
          all(len(chunk.content_hash) == 64 for chunk in chunks_first))
    check("TXT 不虚构页码", all(chunk.page_number is None for chunk in chunks_first))
    check("TXT 不虚构标题路径",
          all(list(chunk.heading_path) == [] for chunk in chunks_first))

    changed = document_parser.parse_bytes(
        txt_raw.replace("定积分是面积。".encode("utf-8"), "定积分是极限。".encode("utf-8")),
        "notes.txt",
    )
    check("TXT 内容变化导致 document_hash 变化", changed.document_hash != parsed.document_hash)
    check("TXT 内容变化导致 document_id 变化", changed.document_id != parsed.document_id)
    check(
        "TXT 内容变化导致 chunk_id 变化",
        [chunk.chunk_id for chunk in document_parser_chunks(changed)]
        != [chunk.chunk_id for chunk in chunks_first],
    )

    # 切片参数变化也应当改变 chunk_id（配置参与身份）
    import document_chunker

    wide = document_chunker.chunk_document(parsed, max_chars=2000)
    check("TXT 切片参数不同则 chunk_id 不同",
          [chunk.chunk_id for chunk in wide] != [chunk.chunk_id for chunk in chunks_first])
    check("chunk_id 长度稳定且可读",
          all(len(chunk.chunk_id) < 120 and chunk.chunk_id.startswith(parsed.document_id)
              for chunk in chunks_first),
          chunks_first[0].chunk_id)

    # 同一文件里内容完全相同的两段，chunk_id 仍需唯一
    duplicate_raw = "重复段落\n\n重复段落\n".encode("utf-8")
    duplicate_parsed = document_parser.parse_bytes(duplicate_raw, "dup.txt")
    duplicate_chunks = document_chunker.chunk_document(duplicate_parsed, max_chars=30)
    duplicate_ids = [chunk.chunk_id for chunk in duplicate_chunks]
    check("同定位同内容的多片 chunk_id 仍互不相同",
          len(duplicate_ids) == len(set(duplicate_ids)), duplicate_ids)

    # ------------------------------------------------------------ 2. PDF

    pdf_raw = build_pdf([
        ("第一页正文：极限的定义。", "- 1 -"),
        ("第二页正文：连续与可导。", "- 2 -"),
    ])
    pdf_parsed = document_parser.parse_bytes(pdf_raw, "讲义.pdf")

    check("PDF 识别为 pdf", pdf_parsed.file_type == "pdf", pdf_parsed.file_type)

    pages = sorted({block.page_number for block in pdf_parsed.blocks if block.page_number is not None})
    check("PDF 物理页码从 1 开始且连续", pages == [1, 2], pages)
    printed = sorted({block.printed_page_number for block in pdf_parsed.blocks
                      if block.printed_page_number is not None})
    check("PDF 印刷页码单独保存", printed == [1, 2], printed)
    check("PDF 印刷页码与物理页码是两组字段",
          any(block.printed_page_number is not None for block in pdf_parsed.blocks))

    pdf_chunks = document_parser_chunks(pdf_parsed)
    check("PDF 至少切出一片", len(pdf_chunks) >= 1, len(pdf_chunks))
    cross_page = [chunk for chunk in pdf_chunks if len(
        {span["page_number"] for span in chunk.source_spans if span["page_number"] is not None}
    ) > 1]
    check("跨页片段被识别", len(cross_page) >= 1,
          [chunk.chunk_id.split("#")[-2] for chunk in cross_page])
    if cross_page:
        merged = cross_page[0]
        check("跨页片段 locator 记录页码范围", "-" in merged.chunk_id.split("#")[1],
              merged.chunk_id)
        check("跨页片段保留两页的来源范围",
              len({span["page_number"] for span in merged.source_spans
                   if span["page_number"] is not None}) == 2)
        check("跨页片段正文包含两页文字",
              "极限" in merged.content and "连续" in merged.content)
    else:
        skip("跨页片段来源范围", "该 PDF 未产生跨页片段")

    pdf_chunks_again = document_parser_chunks(document_parser.parse_bytes(pdf_raw, "讲义.pdf"))
    check("PDF 两次解析 chunk_id 一致",
          [chunk.chunk_id for chunk in pdf_chunks] == [chunk.chunk_id for chunk in pdf_chunks_again])

    # ------------------------------------------------------------ 3. DOCX

    docx_raw = build_docx()
    docx_parsed = document_parser.parse_bytes(docx_raw, "ch1.docx")

    check("DOCX 识别为 docx", docx_parsed.file_type == "docx", docx_parsed.file_type)
    check("DOCX 不虚构页码",
          all(block.page_number is None for block in docx_parsed.blocks))
    check("DOCX 记录段落序号",
          all(block.paragraph_index is not None for block in docx_parsed.blocks))

    docx_chunks = document_parser_chunks(docx_parsed)
    check("DOCX 至少切出一片", len(docx_chunks) >= 1, len(docx_chunks))
    heading_chunks = [chunk for chunk in docx_chunks if chunk.heading_path]
    check("DOCX 片段带标题路径", len(heading_chunks) >= 1,
          [list(chunk.heading_path) for chunk in heading_chunks])
    check("DOCX 标题路径记录层级",
          any(len(chunk.heading_path) >= 2 for chunk in heading_chunks),
          [list(chunk.heading_path) for chunk in heading_chunks])
    check("DOCX chunk_id 用段落定位",
          all(chunk.chunk_id.split("#")[1].startswith("para") for chunk in docx_chunks),
          [chunk.chunk_id.split("#")[1] for chunk in docx_chunks])
    table_chunks = [chunk for chunk in docx_chunks if " | " in chunk.content]
    check("DOCX 表格行保留结构", len(table_chunks) >= 1,
          table_chunks[0].content if table_chunks else "无表格片段")

    # ------------------------------------------------------------ 4. 解析失败

    expect_parse_error("拒绝不支持的格式（.md）", "unsupported_type",
                       lambda: document_parser.parse_bytes(b"# x", "notes.md"))
    expect_parse_error("拒绝空文件", "empty_file",
                       lambda: document_parser.parse_bytes(b"", "empty.txt"))
    expect_parse_error("拒绝无法识别的编码", "decode_failed",
                       lambda: document_parser.parse_bytes(b"\xff\xfe\x00\x01abc", "bad.txt"))
    expect_parse_error("拒绝损坏的 PDF", "corrupted_pdf",
                       lambda: document_parser.parse_bytes(b"%PDF-1.4 not really a pdf", "bad.pdf"))
    expect_parse_error("识别无文本层（扫描件）", "no_text_layer",
                       lambda: document_parser.parse_bytes(build_blank_pdf(), "scan.pdf"))
    expect_parse_error("拒绝损坏的 DOCX", "corrupted_docx",
                       lambda: document_parser.parse_bytes(b"PK\x03\x04broken", "bad.docx"))

    try:
        document_parser.parse_bytes(b"# x", "notes.md")
    except document_parser.ParseError as error:
        issue = error.to_issue()
        check("解析失败可转成结构化 issue",
              issue.get("error_code") == "unsupported_type"
              and issue.get("source_name") == "notes.md"
              and bool(issue.get("error_message")),
              issue)
    else:
        check("解析失败可转成结构化 issue", False, "没有抛错")

    # ------------------------------------------------------------ 5. 落盘与统计

    knowledge = workdir / "knowledge"
    knowledge.mkdir()

    txt_saved = document_service.save_document("notes.txt", txt_raw, directory=knowledge)
    check("TXT 保存成功", txt_saved == "notes.txt", txt_saved)

    pdf_saved = document_service.save_document("讲义.pdf", pdf_raw, directory=knowledge)
    check("PDF 保存成功", pdf_saved == "讲义.pdf", pdf_saved)

    docx_saved = document_service.save_document("ch1.docx", docx_raw, directory=knowledge)
    check("DOCX 保存成功", docx_saved == "ch1.docx", docx_saved)

    check("PDF 落盘字节与原始一致",
          (knowledge / "讲义.pdf").read_bytes() == pdf_raw)
    check("DOCX 落盘字节与原始一致",
          (knowledge / "ch1.docx").read_bytes() == docx_raw)

    before = sorted(path.name for path in knowledge.iterdir())
    try:
        document_service.save_document("坏文件.pdf", b"%PDF-1.4 broken", directory=knowledge)
    except document_service.DocumentError as error:
        check("坏 PDF 上传被拒绝", "解析失败" in str(error) or "损坏" in str(error), str(error)[:60])
    else:
        check("坏 PDF 上传被拒绝", False, "没有抛 DocumentError")

    after = sorted(path.name for path in knowledge.iterdir())
    check("坏文件不写盘", before == after, f"{before} vs {after}")

    try:
        document_service.save_document("图片.png", b"\x89PNG", directory=knowledge)
    except document_service.DocumentError as error:
        check("上传接口白名单只放行三种后缀",
              ".txt" in str(error) and ".pdf" in str(error) and ".docx" in str(error),
              str(error))
    else:
        check("上传接口白名单只放行三种后缀", False, "没有抛 DocumentError")

    txt_count = len(document_service.chunk_file("notes.txt", directory=knowledge))
    pdf_count = len(document_service.chunk_file("讲义.pdf", directory=knowledge))
    docx_count = len(document_service.chunk_file("ch1.docx", directory=knowledge))
    check("chunk_file 能统计 TXT 切片数", txt_count >= 1, txt_count)
    check("chunk_file 能统计 PDF 切片数", pdf_count >= 1, pdf_count)
    check("chunk_file 能统计 DOCX 切片数", docx_count >= 1, docx_count)
    check("chunk_file 统计结果与直接切片一致",
          pdf_count == len(pdf_chunks) and docx_count == len(docx_chunks),
          f"pdf {pdf_count}/{len(pdf_chunks)} docx {docx_count}/{len(docx_chunks)}")

    records = document_service.load_chunk_records(knowledge)
    check("load_chunk_records 覆盖三种文件",
          {record["file_type"] for record in records} == {"txt", "pdf", "docx"},
          sorted({record["file_type"] for record in records}))
    check("load_chunk_records 字段齐全",
          all(set(CHUNK_FIELDS).issubset(record) for record in records))
    check("load_chunk_records 保留 source 键",
          all(record["source"] == record["source_name"] for record in records))
    check("load_chunk_records 可 JSON 序列化", isinstance(records[0]["source_spans"], list))

    default_chunks = document_service.load_chunks(knowledge)
    check("load_chunks 默认仍只有 content/source",
          all(set(chunk) == {"content", "source"} for chunk in default_chunks))

    scan = document_service.scan_knowledge(knowledge)
    check("scan_knowledge 列出三份文档", len(scan["documents"]) == 3,
          [item["source"] for item in scan["documents"]])
    check("scan_knowledge 无解析问题", scan["issues"] == [], scan["issues"])
    check("scan_knowledge 总切片数一致",
          scan["chunk_count"] == len(records), f"{scan['chunk_count']} vs {len(records)}")
    check("scan_knowledge 三份文档状态均为 ok",
          {item["status"] for item in scan["documents"]} == {"ok"},
          [item["status"] for item in scan["documents"]])
    check("scan_knowledge 记录字节数且与磁盘一致",
          all(item["bytes"] == (knowledge / item["source"]).stat().st_size
              for item in scan["documents"]),
          [(item["source"], item["bytes"]) for item in scan["documents"]])
    check("scan_knowledge 记录解析/切片版本",
          all(item["parser_version"] == document_parser.PARSER_VERSION
              and item["chunker_version"] == document_chunker.CHUNKER_VERSION
              for item in scan["documents"]))

    # 坏文件与扫描件必须留下状态，而不是消失
    bad_dir = workdir / "knowledge_bad"
    bad_dir.mkdir()
    (bad_dir / "坏.pdf").write_bytes(b"%PDF-1.4 broken")
    (bad_dir / "扫描件.pdf").write_bytes(build_blank_pdf())

    bad_scan = document_service.scan_knowledge(bad_dir)
    statuses = {item["source"]: item["status"] for item in bad_scan["documents"]}
    check("坏 PDF 记录为 failed", statuses.get("坏.pdf") == "failed", statuses)
    check("扫描件记录为 no_text_layer", statuses.get("扫描件.pdf") == "no_text_layer", statuses)
    check("失败文档切片数为 0 且无 document_id",
          all(item["chunk_count"] == 0 and item["document_id"] is None
              for item in bad_scan["documents"]))
    check("失败文档带错误码",
          {item["error_code"] for item in bad_scan["documents"]}
          == {"corrupted_pdf", "no_text_layer"},
          [item["error_code"] for item in bad_scan["documents"]])
    check("失败文档同时进入 issues",
          len(bad_scan["issues"]) == 2
          and {issue["source_name"] for issue in bad_scan["issues"]}
          == {"坏.pdf", "扫描件.pdf"},
          bad_scan["issues"])
    check("扫描件不产出任何切片", bad_scan["chunk_count"] == 0, bad_scan["chunk_count"])

    # ------------------------------------------------------------ 6. 检索层元数据

    original_load_chunks = rag_service.load_chunks
    original_vector_ranked = rag_service._vector_ranked
    original_log_metrics = rag_service._log_retrieval_metrics

    try:
        # 不碰真实 Embedding：直接给出向量排名
        rag_service.load_chunks = lambda *args, **kwargs: (
            document_service.load_chunk_records(knowledge)
            if kwargs.get("with_metadata")
            else document_service.load_chunks(knowledge)
        )
        rag_service._vector_ranked = (
            lambda query, chunks, limit: [(chunk, 0.9 - index * 0.1)
                                          for index, chunk in enumerate(chunks[:limit])]
        )
        rag_service._log_retrieval_metrics = lambda *args, **kwargs: None

        legacy = rag_service.retrieve_chunks("定积分", top_k=2)
        check("检索默认结果仍只有 content/source/score",
              all(set(result) == {"content", "source", "score"} for result in legacy),
              [sorted(result) for result in legacy[:1]])
        check("检索默认结果带分数", all("score" in result for result in legacy))

        rich = rag_service.retrieve_chunks("定积分", top_k=2, with_metadata=True)
        check("检索开启元数据后返回富记录", len(rich) >= 1, len(rich))
        check("检索富记录带 chunk_id",
              all(result.get("chunk_id") for result in rich))
        check("检索富记录保留原有三键",
              all({"content", "source", "score"}.issubset(result) for result in rich))
        check("检索富记录带页码/标题路径/来源范围",
              all("page_number" in result and "heading_path" in result
                  and isinstance(result["source_spans"], list) for result in rich))
        check("检索富记录不泄露内部对象",
              all(isinstance(result["source_spans"], list) for result in rich))
    finally:
        rag_service.load_chunks = original_load_chunks
        rag_service._vector_ranked = original_vector_ranked
        rag_service._log_retrieval_metrics = original_log_metrics

finally:
    shutil.rmtree(workdir, ignore_errors=True)

    check("临时目录已清理", not workdir.exists(), str(workdir))

    total = checks["pass"] + checks["fail"]
    print(
        f"\n合计 {total} 项：通过 {checks['pass']}，失败 {checks['fail']}，"
        f"跳过 {checks['skip']}"
    )

    if checks["fail"]:
        raise SystemExit(1)
