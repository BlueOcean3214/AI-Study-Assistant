"""文档解析：TXT / PDF / DOCX → 统一的 ParsedDocument（文本块 + 来源定位）。

职责边界：
    document_parser  只负责“把文件变成带定位信息的文本块”，不切片、不落盘、不检索
    document_chunker 负责把文本块切成 Chunk（14 字段，含 chunk_id）
    document_service 负责文件读写、目录扫描、兼容旧 API

设计要点（详见 docs/RAG-DOCUMENT-PARSING-DESIGN.md）：
    - document_hash 按原始字节算 SHA-256，编码无关
    - PDF 用物理页码（1 起），印刷页码单独识别，识别不到就是 None
    - DOCX 不虚构页码，用正文顺序号 + 标题栈定位
    - TXT 保持 utf-8-sig -> gbk 的解码顺序，切片规则完全交给 document_service 的旧实现
    - 解析失败一律抛 ParseError(code=...)，绝不返回空内容冒充成功
"""

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

# 解析器版本：解析行为（分段、页码识别、标题识别）一改就要升版
PARSER_VERSION = "s1.1"

# 单文件大小上限：超过就不解析（避免一个巨型 PDF 把进程拖死）
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

SUPPORTED_SUFFIXES = (".txt", ".pdf", ".docx")

# TXT 解码顺序，与旧实现保持一致
_ENCODINGS = ("utf-8-sig", "gbk")

# 印刷页码：形如 “- 12 -”“—12—”“第 12 页”
_PRINTED_PAGE_PATTERNS = (
    re.compile(r"^\s*[-—–]\s*(\d{1,4})\s*[-—–]\s*$"),
    re.compile(r"^\s*第\s*(\d{1,4})\s*页\s*$"),
)

# 编号型标题：“1.3 条件概率”“2、矩阵”
_NUMBERED_HEADING = re.compile(r"^\s*\d+(?:\.\d+){0,3}[\s、.．]\s*\S")

# 章节型标题：“第三章 …”“第 2 节 …”
_CHAPTER_HEADING = re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百]+\s*[章节篇部]\s*\S*")

_BLANK_LINE_PATTERN = re.compile(r"\n\s*\n")

# 错误码 -> 文档状态。未列出的错误码统一记为 failed。
STATUS_BY_CODE = {
    "unsupported_type": "skipped_unsupported",
    "empty_file": "empty",
    "no_text_layer": "no_text_layer",
    "oversize": "skipped_oversize",
}


def status_for_code(code):
    """把错误码映射成文档状态，供 scan_knowledge 的清单使用。"""

    return STATUS_BY_CODE.get(code, "failed")


class ParseError(RuntimeError):
    """文档解析失败。code 是稳定的错误码，message 是给人看的原因。"""

    def __init__(self, code, message, source_name=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.source_name = source_name

    def to_issue(self):
        return {
            "source_name": self.source_name,
            "error_code": self.code,
            "error_message": self.message,
        }


@dataclass(frozen=True)
class TextBlock:
    """解析出的一个文本块：一段正文、一行表格或一个标题。

    page_number        物理页码（PDF，1 起）；TXT/DOCX 为 None
    printed_page_number 印刷页码（PDF 页脚/页眉识别），识别不到为 None
    paragraph_index    文档内段落/表格顺序号（DOCX 与段落共用计数）
    heading_path       该块所属的标题链（DOCX 由标题样式层级推导）
    char_start/char_end 该块在“所属页文本”或“表格拼接文本”中的字符范围
    """

    text: str
    kind: str = "text"  # text / heading / table
    page_number: int = None
    printed_page_number: int = None
    paragraph_index: int = None
    heading_path: tuple = ()
    char_start: int = None
    char_end: int = None


@dataclass(frozen=True)
class ParsedDocument:
    """一份文档的解析结果。source_name 是知识库内相对路径。"""

    source_name: str
    file_type: str
    document_hash: str
    document_id: str
    blocks: tuple
    parser_version: str = PARSER_VERSION

    def text(self):
        """把全部块拼成一整段文本（块之间用空行分隔）。"""

        return "\n\n".join(block.text for block in self.blocks if block.text)


def document_id_for(source_name, document_hash):
    """文档标识：文件主名 + 内容哈希前缀。

    源文件内容一变，document_hash 变，document_id 随之换代，
    这样“同一路径的两个版本”不会被当成同一份文档。
    """

    stem = Path(source_name).stem or "document"
    return f"{stem}-{document_hash[:8]}"


def compute_document_hash(raw):
    """按原始字节算 SHA-256（与编码、换行符无关）。"""

    return hashlib.sha256(raw).hexdigest()


def detect_file_type(source_name):
    """由后缀判断文件类型；不支持的后缀抛 unsupported_type。"""

    suffix = Path(source_name).suffix.lower()

    if suffix not in SUPPORTED_SUFFIXES:
        raise ParseError(
            "unsupported_type",
            f"不支持的文档格式：{suffix or '(无后缀)'}，仅支持 .txt / .pdf / .docx",
            source_name,
        )

    return suffix.lstrip(".")


def _decode_text(raw, source_name):
    """TXT 解码：先 utf-8-sig 再 gbk，都失败抛 decode_failed。"""

    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ParseError(
        "decode_failed",
        "无法识别文件编码，请把 txt 另存为 UTF-8 后重新上传",
        source_name,
    )


def _normalize_text(text):
    """统一换行符，去掉首尾空白。"""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _printed_page_number(page_text):
    """从页首/页尾几行里找印刷页码；找不到返回 None。"""

    lines = [line.strip() for line in page_text.splitlines() if line.strip()]

    for line in lines[:2] + lines[-2:]:
        for pattern in _PRINTED_PAGE_PATTERNS:
            match = pattern.match(line)

            if match:
                try:
                    return int(match.group(1))
                except (TypeError, ValueError):
                    return None

    return None


def _looks_like_heading(line):
    """判断一行是否像标题（PDF/TXT 没有样式信息，只能用启发式）。"""

    text = line.strip()

    if not text or len(text) > 30:
        return False

    if text.endswith("。"):
        return False

    if _NUMBERED_HEADING.match(text) or _CHAPTER_HEADING.match(text):
        return True

    return text.endswith(("：", ":"))


def _split_page_paragraphs(page_text):
    """页内分段：优先按空行分；没有空行时按非空行分。"""

    page_text = page_text.strip()

    if not page_text:
        return []

    paragraphs = [piece.strip() for piece in _BLANK_LINE_PATTERN.split(page_text) if piece.strip()]

    if paragraphs:
        return paragraphs

    return [line.strip() for line in page_text.splitlines() if line.strip()]


def _parse_txt(raw, source_name, doc_hash):
    """TXT：整篇作为一个文本块，切片交给旧算法（保证逐字节兼容）。"""

    if not raw.strip():
        raise ParseError("empty_file", "文件内容为空", source_name)

    text = _normalize_text(_decode_text(raw, source_name))

    if not text:
        raise ParseError("empty_file", "文件内容为空", source_name)

    block = TextBlock(text=text, kind="text", char_start=0, char_end=len(text))

    return ParsedDocument(
        source_name=source_name,
        file_type="txt",
        document_hash=doc_hash,
        document_id=document_id_for(source_name, doc_hash),
        blocks=(block,),
    )


def _import_pypdf(source_name):
    """惰性导入 pypdf：缺失时报明确的 parser_unavailable，而不是 ImportError。"""

    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - 依赖缺失时才会走到
        raise ParseError(
            "parser_unavailable",
            "缺少 pypdf，无法解析 PDF（pip install pypdf）",
            source_name,
        ) from error

    return PdfReader


def _parse_pdf(raw, source_name, doc_hash):
    """PDF：逐页取文本，物理页码 1 起，印刷页码单独识别。"""

    if not raw.strip():
        raise ParseError("empty_file", "文件内容为空", source_name)

    PdfReader = _import_pypdf(source_name)

    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = list(reader.pages)
    except ParseError:
        raise
    except Exception as error:  # noqa: BLE001 - pypdf 的异常类型很杂
        raise ParseError("corrupted_pdf", f"PDF 无法读取：{error}", source_name) from error

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as error:  # noqa: BLE001
            raise ParseError("corrupted_pdf", f"PDF 已加密且无法解密：{error}", source_name) from error

    blocks = []
    heading = None
    paragraph_index = 0
    extracted_any = False

    for page_number, page in enumerate(pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as error:  # noqa: BLE001
            raise ParseError(
                "corrupted_pdf",
                f"PDF 第 {page_number} 页文本提取失败：{error}",
                source_name,
            ) from error

        page_text = _normalize_text(page_text)

        if not page_text:
            continue

        extracted_any = True
        printed = _printed_page_number(page_text)

        for piece in _split_page_paragraphs(page_text):
            kind = "text"
            heading_path = (heading,) if heading else ()

            if _looks_like_heading(piece):
                heading = piece
                kind = "heading"
                heading_path = (piece,)

            blocks.append(
                TextBlock(
                    text=piece,
                    kind=kind,
                    page_number=page_number,
                    printed_page_number=printed,
                    paragraph_index=paragraph_index,
                    heading_path=heading_path,
                    char_start=0,
                    char_end=len(piece),
                )
            )
            paragraph_index += 1

    if not extracted_any:
        raise ParseError(
            "no_text_layer",
            "PDF 没有可提取的文本层（可能是扫描件），本阶段不做 OCR",
            source_name,
        )

    return ParsedDocument(
        source_name=source_name,
        file_type="pdf",
        document_hash=doc_hash,
        document_id=document_id_for(source_name, doc_hash),
        blocks=tuple(blocks),
    )


def _import_docx(source_name):
    """惰性导入 python-docx。"""

    try:
        import docx
    except ImportError as error:  # pragma: no cover
        raise ParseError(
            "parser_unavailable",
            "缺少 python-docx，无法解析 DOCX（pip install python-docx）",
            source_name,
        ) from error

    return docx


def _docx_heading_level(paragraph):
    """识别标题级别：优先看样式名 Heading N，其次看 style_id。

    返回 1~9，普通段落返回 0。Word 里中文标题的样式名仍是 "Heading 1"。
    """

    try:
        style = paragraph.style
    except Exception:  # noqa: BLE001 - 样式缺失不影响正文
        return 0

    if style is None:
        return 0

    name = (getattr(style, "name", "") or "").strip().lower()
    style_id = (getattr(style, "style_id", "") or "").strip().lower()

    for candidate in (name, style_id):
        if not candidate:
            continue

        if candidate in ("title",):
            return 1

        match = re.match(r"^heading\s*([1-9])$", candidate)

        if match:
            return int(match.group(1))

    return 0


def _docx_table_blocks(table, paragraph_index):
    """把表格的每一行提取成一个块：单元格文本用 " | " 连接。"""

    blocks = []

    for row in table.rows:
        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
        text = " | ".join(cell for cell in cells).strip()

        if not text or text.replace("|", "").strip() == "":
            continue

        blocks.append(
            TextBlock(
                text=text,
                kind="table",
                paragraph_index=paragraph_index,
                char_start=0,
                char_end=len(text),
            )
        )

    return blocks


def _parse_docx(raw, source_name, doc_hash):
    """DOCX：按 body 顺序遍历段落与表格，用标题样式维护标题栈。"""

    if not raw.strip():
        raise ParseError("empty_file", "文件内容为空", source_name)

    docx = _import_docx(source_name)

    try:
        document = docx.Document(io.BytesIO(raw))
        items = list(document.iter_inner_content())
    except ParseError:
        raise
    except Exception as error:  # noqa: BLE001 - BadZipFile / PackageNotFoundError 等
        raise ParseError("corrupted_docx", f"DOCX 无法读取：{error}", source_name) from error

    blocks = []
    heading_stack = []
    paragraph_index = 0
    produced_text = False

    for item in items:
        # python-docx 的 Paragraph 有 .text，Table 有 .rows
        if hasattr(item, "rows"):
            for block in _docx_table_blocks(item, paragraph_index):
                blocks.append(
                    TextBlock(
                        text=block.text,
                        kind=block.kind,
                        paragraph_index=block.paragraph_index,
                        heading_path=tuple(heading_stack),
                        char_start=block.char_start,
                        char_end=block.char_end,
                    )
                )
                produced_text = True

            paragraph_index += 1
            continue

        text = (getattr(item, "text", "") or "").strip()
        level = _docx_heading_level(item)

        if level:
            del heading_stack[level - 1:]
            heading_stack.append(text)
            blocks.append(
                TextBlock(
                    text=text,
                    kind="heading",
                    paragraph_index=paragraph_index,
                    heading_path=tuple(heading_stack),
                    char_start=0,
                    char_end=len(text),
                )
            )
            produced_text = produced_text or bool(text)

        elif text:
            blocks.append(
                TextBlock(
                    text=text,
                    kind="text",
                    paragraph_index=paragraph_index,
                    heading_path=tuple(heading_stack),
                    char_start=0,
                    char_end=len(text),
                )
            )
            produced_text = True

        paragraph_index += 1

    if not produced_text:
        raise ParseError("empty_file", "DOCX 没有可提取的正文内容", source_name)

    return ParsedDocument(
        source_name=source_name,
        file_type="docx",
        document_hash=doc_hash,
        document_id=document_id_for(source_name, doc_hash),
        blocks=tuple(blocks),
    )


def parse_bytes(raw, source_name):
    """解析一段文件内容（bytes），source_name 决定格式与相对路径。"""

    if not isinstance(raw, (bytes, bytearray)):
        raise ParseError("unsupported_type", "raw 必须是 bytes", source_name)

    raw = bytes(raw)

    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ParseError(
            "oversize",
            f"文件超过 {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB，未解析",
            source_name,
        )

    file_type = detect_file_type(source_name)
    doc_hash = compute_document_hash(raw)

    if file_type == "txt":
        return _parse_txt(raw, source_name, doc_hash)

    if file_type == "pdf":
        return _parse_pdf(raw, source_name, doc_hash)

    return _parse_docx(raw, source_name, doc_hash)


def parse_file(path, source_name=None):
    """解析磁盘上的一个文件。source_name 默认取文件名。"""

    path = Path(path)
    source_name = source_name or path.name

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ParseError("read_failed", f"读取文件失败：{path}", source_name) from error

    return parse_bytes(raw, source_name)
