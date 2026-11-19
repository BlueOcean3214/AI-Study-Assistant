"""S1 文档解析 / 切片 / 来源定位测试：直接运行 python test_document_parser.py。

全程离线、合成数据：PDF 用 reportlab 现造，DOCX 用 python-docx 现造，
知识库一律落在临时目录里，结束后删除；不碰真实 knowledge/、feedback.db、
.rag_cache/ 与正式实验数据，也不调任何模型或 Embedding API。

覆盖：
1. TXT：解析结果、document_id / document_hash、chunk_id 可复现
2. 文档身份：路径命名空间防碰撞、"#" 分隔符安全编码
3. PDF：物理页码、印刷页码、跨页片段来源范围、整页文本真实字符偏移
3.1 版本与换代：解析器/切片器均为 s1.2，同文件可复现，升版即换 chunk_id，路径摘要 16 位
3.2 加密 PDF：普通 PDF 仍 ok，空密码（owner-only）可解析，需要打开密码报 password_required
4. 混合型 PDF：部分页没有文本层时记 partial 并列出缺页，不静默成功
5. DOCX：标题路径、段落序号、表格块来源范围、不虚构页码、块内偏移口径
6. 解析失败：不支持的格式、空文件、编码错误、坏 PDF、无文本层、坏 DOCX
7. 上传落盘：PDF / DOCX 走 bytes 校验，坏文件不写盘，chunk_file 统计切片数
8. 检索层元数据：默认返回仍是 {content, source, score}，with_metadata=True 才透出；
   Hybrid 富记录按 chunk_id 去重时，不同目录的同名同内容文件都保留；
   富记录的 document_status 会标出 partial 来源
"""

import hashlib
import io
import os
import shutil
import tempfile
from pathlib import Path

import document_chunker
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


def extract_page_texts(raw):
    """按 pypdf 默认抽取方式取每页规范化文本，用来核对 source_spans 偏移。"""

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))

    return [
        (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        for page in reader.pages
    ]


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
        "TXT document_id 含规范化路径摘要与哈希前缀",
        parsed.document_id
        == f"notes-{hashlib.sha256(b'notes.txt').hexdigest()[:document_parser.PATH_DIGEST_CHARS]}-{parsed.document_hash[:8]}",
        parsed.document_id,
    )
    check("TXT document_id 不含 # 分隔符", "#" not in parsed.document_id, parsed.document_id)
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

    # ------------------------------------------- 1.1 文档身份：路径命名空间

    same_bytes = "同名同内容的文档。\n".encode("utf-8")
    id_dir_a = document_parser.parse_bytes(same_bytes, "甲/讲义.txt").document_id
    id_dir_b = document_parser.parse_bytes(same_bytes, "乙/讲义.txt").document_id
    check("不同目录同名同内容 document_id 不同", id_dir_a != id_dir_b,
          f"{id_dir_a} / {id_dir_b}")
    check("不同目录同名同内容 document_hash 相同",
          document_parser.parse_bytes(same_bytes, "甲/讲义.txt").document_hash
          == document_parser.parse_bytes(same_bytes, "乙/讲义.txt").document_hash)

    id_dir_a2 = document_parser.parse_bytes(same_bytes, "甲\\讲义.txt").document_id
    id_dir_a3 = document_parser.parse_bytes(same_bytes, "./甲//讲义.txt").document_id
    check("同一路径的不同写法规范化后 document_id 稳定",
          id_dir_a == id_dir_a2 == id_dir_a3, f"{id_dir_a} / {id_dir_a2} / {id_dir_a3}")

    weird_name = "a#b%23#c.txt"
    weird_parsed = document_parser.parse_bytes(b"sharp in name.\n", weird_name)
    weird_chunks = document_parser_chunks(weird_parsed)
    check("文件名含 # 时 document_id 仍然安全（不含 #）",
          "#" not in weird_parsed.document_id, weird_parsed.document_id)
    check("文件名含 # 时 chunk_id 仍恰好三段",
          all(len(chunk.chunk_id.split("#")) == 3 for chunk in weird_chunks),
          [chunk.chunk_id for chunk in weird_chunks])
    check("文件名含 # 时 chunk_id 前缀就是 document_id",
          all(chunk.chunk_id.split("#")[0] == weird_parsed.document_id
              for chunk in weird_chunks))

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

    # ---------------------------------------- 2.1 PDF 真实字符偏移（P2）

    pdf_page_texts = extract_page_texts(pdf_raw)
    check("PDF 页文本按页取到两页", len(pdf_page_texts) == 2, len(pdf_page_texts))

    if cross_page:
        spans = cross_page[0].source_spans
        span_ok = True
        detail = []
        for span in spans:
            if span["page_number"] is None:
                continue
            page_index = span["page_number"] - 1
            block = pdf_parsed.blocks[span["block_index"]]
            raw_slice = pdf_page_texts[page_index][span["char_start"]:span["char_end"]]
            detail.append((span["page_number"], span["char_start"], span["char_end"], raw_slice))
            span_ok = span_ok and raw_slice == block.text
        check("跨页片段每个 span 用该页页文本能取回对应块", span_ok, detail)
        check("跨页片段各 span 页码与块页码一致",
              all(pdf_parsed.blocks[span["block_index"]].page_number == span["page_number"]
                  for span in spans if span["page_number"] is not None))

    # 单页多段：后续段落的 char_start 必须有真实偏移
    multi_raw = build_pdf([("第一段：导数的定义。\n第二段：极限的定义。\n第三段：连续的定义。", "- 1 -")])
    multi_parsed = document_parser.parse_bytes(multi_raw, "多段.pdf")
    multi_page_text = extract_page_texts(multi_raw)[0]
    multi_blocks = [block for block in multi_parsed.blocks if block.page_number == 1]
    check("PDF 单页被切成多个块", len(multi_blocks) >= 3,
          [block.text for block in multi_blocks])
    check("PDF 第二段及以后 char_start 不再为 0",
          all(block.char_start > 0 for block in multi_blocks[1:]),
          [block.char_start for block in multi_blocks])
    check("PDF 每个块都能用整页文本切片取回原文",
          all(multi_page_text[block.char_start:block.char_end] == block.text
              for block in multi_blocks),
          [(block.char_start, block.char_end, block.text) for block in multi_blocks])
    check("PDF 块偏移不重叠且递增",
          all(multi_blocks[index].char_end <= multi_blocks[index + 1].char_start
              for index in range(len(multi_blocks) - 1)),
          [(block.char_start, block.char_end) for block in multi_blocks])

    # 空行分段时同样保留真实偏移（含前导空白）
    blank_pieces = document_parser._page_pieces("第一段\n\n  第二段  ")
    check("空行分段保留真实字符偏移", blank_pieces == [("第一段", 0, 3), ("第二段", 7, 10)],
          blank_pieces)
    check("空行分段的偏移与页文本切片一致",
          all("第一段\n\n  第二段  ".strip()[start:end] == text
              for text, start, end in blank_pieces))

    # ---------------------------------------- 2.2 混合型 PDF 记部分解析

    mixed_raw = build_pdf([
        ("第一页有文本。", "- 1 -"),
        ("", ""),
        ("第三页有文本。", "- 3 -"),
    ])
    mixed_parsed = document_parser.parse_bytes(mixed_raw, "混合.pdf")
    check("混合型 PDF 总页数与有文本页数正确",
          (mixed_parsed.pages_total, mixed_parsed.pages_with_text) == (3, 2),
          (mixed_parsed.pages_total, mixed_parsed.pages_with_text))
    check("混合型 PDF 列出缺文本页", list(mixed_parsed.missing_pages) == [2],
          mixed_parsed.missing_pages)
    check("混合型 PDF 标记为 partial", mixed_parsed.status == document_parser.STATUS_PARTIAL,
          mixed_parsed.status)
    check("混合型 PDF 仍产出有文本页的块",
          {block.page_number for block in mixed_parsed.blocks} == {1, 3},
          sorted({block.page_number for block in mixed_parsed.blocks}))
    check("完整 PDF 的 status 仍是 ok", pdf_parsed.status == document_parser.STATUS_OK,
          pdf_parsed.status)
    check("完整 PDF 没有缺页", list(pdf_parsed.missing_pages) == [],
          pdf_parsed.missing_pages)

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
    check("DOCX 偏移以块自身文本为基准（段落级 / 表格行级）",
          all(block.char_start == 0 and block.char_end == len(block.text)
              for block in docx_parsed.blocks),
          [(block.char_start, block.char_end, len(block.text)) for block in docx_parsed.blocks])
    table_blocks = [block for block in docx_parsed.blocks if block.kind == "table"]
    check("DOCX 至少解析出一个表格块（kind=table）", len(table_blocks) >= 1,
          [(block.kind, block.text[:20]) for block in docx_parsed.blocks])
    check("DOCX 表格块的偏移覆盖含分隔符的行文本",
          len(table_blocks) >= 1
          and all(block.char_start == 0
                  and block.char_end == len(block.text)
                  and block.text[block.char_start:block.char_end] == block.text
                  for block in table_blocks),
          [(block.char_start, block.char_end, len(block.text)) for block in table_blocks])
    check("DOCX 表格块文本确实是表格行（含分隔符）",
          len(table_blocks) >= 1 and all(" | " in block.text for block in table_blocks),
          [block.text for block in table_blocks[:1]])

    table_block_indexes = {index for index, block in enumerate(docx_parsed.blocks)
                           if block.kind == "table"}
    table_spans = [span for chunk in docx_chunks for span in chunk.source_spans
                   if span.get("kind") == "table"]
    check("DOCX 片段 source_spans 指向真实存在的表格块",
          len(table_spans) >= 1
          and all(span["block_index"] in table_block_indexes for span in table_spans),
          [(span["block_index"], span.get("kind")) for span in table_spans])
    check("DOCX 表格 span 的块内坐标与表格块一致",
          len(table_spans) >= 1
          and all(
              span["char_start"] == docx_parsed.blocks[span["block_index"]].char_start
              and span["char_end"] == docx_parsed.blocks[span["block_index"]].char_end
              for span in table_spans
          ),
          [(span["block_index"], span["char_start"], span["char_end"]) for span in table_spans])
    check("DOCX 块坐标只服务块内定位，不出现在跨块合并里",
          all(span["char_start"] >= 0 and span["char_end"] >= span["char_start"]
              for chunk in docx_chunks for span in chunk.source_spans))

    # ------------------------------------------- 3.1 版本号、可复现性与 ID 换代

    check("解析器版本为 s1.2", document_parser.PARSER_VERSION == "s1.2",
          document_parser.PARSER_VERSION)
    check("切片器版本为 s1.2", document_chunker.CHUNKER_VERSION == "s1.2",
          document_chunker.CHUNKER_VERSION)

    version_records = document_parser_chunks(document_parser.parse_bytes(pdf_raw, "ch1.pdf"))
    check("每个切片记录都带 s1.2 两个版本字段",
          len(version_records) >= 1
          and all(chunk.parser_version == "s1.2"
                  and chunk.chunker_version == "s1.2" for chunk in version_records),
          [(chunk.parser_version, chunk.chunker_version) for chunk in version_records[:1]])

    # 同一份文件、同一版本，重复解析必须得到同一批 ID
    repeat_parsed = document_parser.parse_bytes(pdf_raw, "ch1.pdf")
    repeat_records = document_parser_chunks(repeat_parsed)
    first_records = document_parser_chunks(document_parser.parse_bytes(pdf_raw, "ch1.pdf"))
    check("同文件同版本 document_id 可复现",
          repeat_parsed.document_id == document_parser.parse_bytes(
              pdf_raw, "ch1.pdf").document_id,
          repeat_parsed.document_id)
    check("同文件同版本 chunk_id 可复现",
          [chunk.chunk_id for chunk in repeat_records]
          == [chunk.chunk_id for chunk in first_records])

    # 升级版本必须让同一内容换一批 chunk_id，否则旧索引会和新解析混在一起
    sample_chunk = repeat_records[0]
    same_version_id = document_chunker.make_chunk_id(
        sample_chunk.document_id,
        "p1",
        0,
        sample_chunk.content_hash,
        sample_chunk.document_hash,
        "s1.2",
        "s1.2",
        document_service.CHUNK_MAX_CHARS,
    )
    old_version_id = document_chunker.make_chunk_id(
        sample_chunk.document_id,
        "p1",
        0,
        sample_chunk.content_hash,
        sample_chunk.document_hash,
        "s1.1",
        "s1.1",
        document_service.CHUNK_MAX_CHARS,
    )
    check("版本号是 chunk_id 的哈希输入（升版即换代）",
          same_version_id != old_version_id,
          (same_version_id, old_version_id))

    # 路径摘要从 8 位加到 16 位：document_id 形如 名字-摘要文档哈希
    doc_id_parts = repeat_parsed.document_id.rsplit("-", 2)
    check("document_id 由 名称/路径摘要/内容哈希 三段组成", len(doc_id_parts) == 3,
          repeat_parsed.document_id)
    check("document_id 的路径摘要为 16 位十六进制",
          len(doc_id_parts) == 3
          and len(doc_id_parts[1]) == 16
          and all(char in "0123456789abcdef" for char in doc_id_parts[1]),
          doc_id_parts[1] if len(doc_id_parts) == 3 else repeat_parsed.document_id)

    # ------------------------------------------- 3.2 加密 PDF：先判加密再取页面

    def reencrypt_pdf(raw, password=None):
        """把明文 PDF 重写成加密 PDF（password=None 表示只加 owner 密码）。"""

        from pypdf import PdfReader, PdfWriter

        source_reader = PdfReader(io.BytesIO(raw))
        writer = PdfWriter()

        for page in source_reader.pages:
            writer.add_page(page)

        if password is None:
            writer.encrypt("")
        else:
            writer.encrypt(password)

        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    check("普通（未加密）PDF 仍解析为 ok",
          pdf_parsed.status == "ok" and len(pdf_parsed.blocks) >= 1,
          (pdf_parsed.status, len(pdf_parsed.blocks)))

    empty_password_raw = reencrypt_pdf(pdf_raw)
    check("空密码加密 PDF 确实带加密标记",
          empty_password_raw != pdf_raw, len(empty_password_raw))
    empty_password_parsed = document_parser.parse_bytes(empty_password_raw, "只读密码.pdf")
    check("空密码（owner-only）加密 PDF 能正常解析",
          empty_password_parsed.status == "ok"
          and len(empty_password_parsed.blocks) >= 1
          and empty_password_parsed.pages_total == pdf_parsed.pages_total,
          (empty_password_parsed.status,
           empty_password_parsed.pages_total,
           len(empty_password_parsed.blocks)))

    locked_raw = reencrypt_pdf(pdf_raw, "secret")
    expect_parse_error("需要打开密码的 PDF 报 password_required", "password_required",
                       lambda: document_parser.parse_bytes(locked_raw, "锁定.pdf"))
    check("password_required 的状态映射不是 corrupted_pdf",
          document_parser.status_for_code("password_required") == "password_required",
          document_parser.status_for_code("password_required"))

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

    state_records, state_map = document_service.load_chunk_records_with_states(knowledge)
    check("load_chunk_records_with_states 记录与 load_chunk_records 完全一致",
          state_records == records)
    check("状态表覆盖全部文档且状态为 ok",
          set(state_map) == {"notes.txt", "讲义.pdf", "ch1.docx"}
          and all(state["status"] == "ok" for state in state_map.values()),
          {key: value["status"] for key, value in state_map.items()})
    check("状态表给出页码完整性信息",
          state_map["讲义.pdf"]["pages_total"] == 2
          and state_map["讲义.pdf"]["pages_with_text"] == 2
          and state_map["讲义.pdf"]["missing_pages"] == [],
          state_map["讲义.pdf"])

    pdf_info = document_service.inspect_document("讲义.pdf", knowledge)
    check("inspect_document 返回完整状态与切片数",
          pdf_info["status"] == "ok"
          and pdf_info["warnings"] == []
          and pdf_info["pages_total"] == 2
          and pdf_info["chunk_count"] == pdf_count
          and pdf_info["parser_version"] == "s1.2"
          and pdf_info["chunker_version"] == "s1.2",
          {key: pdf_info[key] for key in
           ("status", "pages_total", "chunk_count", "parser_version", "chunker_version")})

    missing_file_error = None
    try:
        document_service.inspect_document("不存在.pdf", knowledge)
    except document_service.DocumentError as error:
        missing_file_error = error
    check("inspect_document 对不存在的文件抛 DocumentError",
          isinstance(missing_file_error, document_service.DocumentError),
          missing_file_error)

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

    # 混合型 PDF 在扫描清单里必须是 partial + warning，而不是笼统的 ok
    mixed_dir = workdir / "knowledge_mixed"
    mixed_dir.mkdir()
    (mixed_dir / "混合.pdf").write_bytes(mixed_raw)

    mixed_scan = document_service.scan_knowledge(mixed_dir)
    mixed_entry = mixed_scan["documents"][0]
    check("scan_knowledge 不把混合型 PDF 报成 ok",
          mixed_entry["status"] == "partial", mixed_entry["status"])
    check("scan_knowledge 记录混合型 PDF 的缺页",
          list(mixed_entry["missing_pages"]) == [2]
          and (mixed_entry["pages_total"], mixed_entry["pages_with_text"]) == (3, 2),
          (mixed_entry["pages_total"], mixed_entry["pages_with_text"],
           mixed_entry["missing_pages"]))
    check("scan_knowledge 为缺文本页给出 warning",
          len(mixed_scan["warnings"]) == 1
          and mixed_scan["warnings"][0]["source_name"] == "混合.pdf"
          and mixed_scan["warnings"][0]["warning_code"] == "missing_pages"
          and list(mixed_scan["warnings"][0]["page_numbers"]) == [2],
          mixed_scan["warnings"])
    check("partial 文档不算解析失败（不进 issues）",
          mixed_scan["issues"] == [], mixed_scan["issues"])
    check("partial 文档仍产出切片",
          mixed_scan["chunk_count"] >= 1 and mixed_entry["chunk_count"] >= 1,
          (mixed_scan["chunk_count"], mixed_entry["chunk_count"]))

    mixed_info = document_service.inspect_document("混合.pdf", mixed_dir)
    check("inspect_document 把混合型 PDF 标为 partial 并列缺页",
          mixed_info["status"] == "partial"
          and mixed_info["missing_pages"] == [2]
          and (mixed_info["pages_total"], mixed_info["pages_with_text"]) == (3, 2)
          and len(mixed_info["warnings"]) == 1
          and mixed_info["chunk_count"] >= 1,
          {key: mixed_info[key] for key in
           ("status", "pages_total", "pages_with_text", "missing_pages", "chunk_count")})

    # ------------------------------------------------------------ 6. 检索层元数据

    original_load_chunks = rag_service.load_chunks
    original_load_with_states = rag_service.load_chunks_with_states
    original_vector_ranked = rag_service._vector_ranked
    original_log_metrics = rag_service._log_retrieval_metrics

    try:
        # 不碰真实 Embedding：直接给出向量排名
        rag_service.load_chunks = lambda *args, **kwargs: (
            document_service.load_chunk_records(knowledge)
            if kwargs.get("with_metadata")
            else document_service.load_chunks(knowledge)
        )
        # 富元数据路径会顺带取文档级状态，这里同样指到临时知识库
        rag_service.load_chunks_with_states = lambda *args, **kwargs: (
            document_service.load_chunk_records_with_states(knowledge)
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
        check("完整文档的富记录标为 ok",
              all(result.get("document_status") == "ok" for result in rich),
              [result.get("document_status") for result in rich])

        # partial 文档：来源能定位到“这份文档有页面没进索引”
        rag_service.load_chunks = lambda *args, **kwargs: (
            document_service.load_chunk_records(mixed_dir)
            if kwargs.get("with_metadata")
            else document_service.load_chunks(mixed_dir)
        )
        rag_service.load_chunks_with_states = lambda *args, **kwargs: (
            document_service.load_chunk_records_with_states(mixed_dir)
        )

        partial_rich = rag_service.retrieve_chunks("第二页", top_k=5, with_metadata=True)
        check("混合型 PDF 的富记录标为 partial",
              len(partial_rich) >= 1
              and all(result.get("document_status") == "partial"
                      for result in partial_rich),
              [result.get("document_status") for result in partial_rich])
        check("默认检索结果不带 document_status（结构保持旧样）",
              all("document_status" not in result
                  for result in rag_service.retrieve_chunks("第二页", top_k=5)))
    finally:
        rag_service.load_chunks = original_load_chunks
        rag_service.load_chunks_with_states = original_load_with_states
        rag_service._vector_ranked = original_vector_ranked
        rag_service._log_retrieval_metrics = original_log_metrics

    # ------------------------------- 6.1 跨目录同名同内容：来源不被合并（P1）

    collision_dir = workdir / "knowledge_collision"
    (collision_dir / "甲").mkdir(parents=True)
    (collision_dir / "乙").mkdir()
    (collision_dir / "甲" / "讲义.pdf").write_bytes(pdf_raw)
    (collision_dir / "乙" / "讲义.pdf").write_bytes(pdf_raw)

    collision_records = document_service.load_chunk_records(collision_dir)
    collision_docs = {}
    for record in collision_records:
        collision_docs.setdefault(record["source"], set()).add(record["document_id"])

    check("跨目录两份同名同内容 PDF 都被扫描到",
          set(collision_docs) == {"甲/讲义.pdf", "乙/讲义.pdf"}, sorted(collision_docs))
    check("跨目录同名同内容 document_id 不同",
          len({next(iter(ids)) for ids in collision_docs.values()}) == 2, collision_docs)
    check("跨目录两份文档内容哈希相同（旧实现会碰撞）",
          len({record["document_hash"] for record in collision_records}) == 1,
          {record["document_hash"] for record in collision_records})

    ids_a = {record["chunk_id"] for record in collision_records
             if record["source"] == "甲/讲义.pdf"}
    ids_b = {record["chunk_id"] for record in collision_records
             if record["source"] == "乙/讲义.pdf"}
    check("跨目录同名同内容的 chunk_id 互不相同", ids_a.isdisjoint(ids_b),
          sorted(ids_a & ids_b))
    check("跨目录同一路径重复解析 chunk_id 稳定",
          {record["chunk_id"] for record in document_service.load_chunk_records(collision_dir)}
          == {record["chunk_id"] for record in collision_records})

    original_load_chunks = rag_service.load_chunks
    original_load_with_states = rag_service.load_chunks_with_states
    original_vector_scored = rag_service._vector_scored
    original_bm25_ranked = rag_service._bm25_ranked
    original_log_metrics = rag_service._log_retrieval_metrics
    original_mode = os.environ.get("RAG_RETRIEVAL_MODE")

    try:
        rag_service.load_chunks = lambda *args, **kwargs: (
            document_service.load_chunk_records(collision_dir)
            if kwargs.get("with_metadata")
            else document_service.load_chunks(collision_dir)
        )
        rag_service.load_chunks_with_states = lambda *args, **kwargs: (
            document_service.load_chunk_records_with_states(collision_dir)
        )
        rag_service._vector_scored = lambda query, chunks: [(chunk, 0.9) for chunk in chunks]
        rag_service._bm25_ranked = lambda query, chunks, limit: [
            (chunk, 1.0) for chunk in chunks[:limit]
        ]
        rag_service._log_retrieval_metrics = lambda *args, **kwargs: None
        os.environ["RAG_RETRIEVAL_MODE"] = "hybrid"

        hybrid = rag_service.retrieve_chunks("定义", top_k=10, with_metadata=True)
        hybrid_sources = {result["source"] for result in hybrid}
        hybrid_ids = [result["chunk_id"] for result in hybrid]

        check("Hybrid 富记录不把两个来源合并成一条",
              hybrid_sources == {"甲/讲义.pdf", "乙/讲义.pdf"}, sorted(hybrid_sources))
        check("Hybrid 结果份数等于两文档全部切片且 chunk_id 不重复",
              len(hybrid) == len(collision_records) and len(hybrid_ids) == len(set(hybrid_ids)),
              f"{len(hybrid)} vs {len(collision_records)}")
        check("Hybrid 每条结果都带各自 document_id",
              len({result["document_id"] for result in hybrid}) == 2,
              [result["document_id"] for result in hybrid])
    finally:
        rag_service.load_chunks = original_load_chunks
        rag_service.load_chunks_with_states = original_load_with_states
        rag_service._vector_scored = original_vector_scored
        rag_service._bm25_ranked = original_bm25_ranked
        rag_service._log_retrieval_metrics = original_log_metrics
        if original_mode is None:
            os.environ.pop("RAG_RETRIEVAL_MODE", None)
        else:
            os.environ["RAG_RETRIEVAL_MODE"] = original_mode

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
