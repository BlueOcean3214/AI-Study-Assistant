"""文档切片：把 ParsedDocument 切成带稳定标识与来源定位的 Chunk。

chunk_id 可复现性靠三件事：
    1. 标识只由内容与定位推导（document_hash / locator / content_hash / occurrence），
       与数组下标无关——前面插一段内容不会让后面所有 chunk 换 id；
    2. 参与哈希的每一项都显式规范化（换行、空白、版本号、max_chars）；
    3. 同一定位下出现完全相同的内容时，用出现序号区分，重复导入结果不变。

TXT 保持历史行为：切片规则交给调用方传入的旧实现（document_service.split_into_chunks），
这里只按顺序回填 char 偏移与定位，避免改变既有 chunk 内容。
"""

import hashlib
from dataclasses import dataclass

from document_parser import TextBlock

# 切片器版本：切分规则（合并、断点、空白处理）一改就要升版
CHUNKER_VERSION = "s1.1"

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


@dataclass(frozen=True)
class Chunk:
    """一个切片及其全部来源信息。source_spans 记录它覆盖的每个文本块范围。"""

    document_id: str
    document_hash: str
    source_name: str
    file_type: str
    chunk_id: str
    chunk_index: int
    content: str
    content_hash: str
    page_number: int
    printed_page_number: int
    heading_path: tuple
    paragraph_index: int
    source_spans: tuple
    parser_version: str
    chunker_version: str = CHUNKER_VERSION

    def to_record(self):
        """转成可 JSON 序列化的字典（source_spans 转 list）。"""

        record = {field: getattr(self, field) for field in CHUNK_FIELDS}
        record["heading_path"] = list(self.heading_path)
        record["source_spans"] = [dict(span) for span in self.source_spans]
        return record


def compute_content_hash(content):
    """归一化后算 SHA-256：换行与行尾空白不影响哈希。"""

    normalized = "\n".join(line.rstrip() for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()


def make_chunk_id(document_id, locator, occurrence, content_hash, document_hash, parser_version, chunker_version, max_chars):
    """chunk_id = document_id#locator#digest，digest 由全部定位要素哈希推导。"""

    payload = "|".join(
        [
            chunker_version,
            parser_version,
            str(max_chars),
            document_hash,
            locator,
            str(occurrence),
            content_hash,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    return f"{document_id}#{locator}#{digest}"


def _page_locator(first_page, last_page):
    if first_page is None:
        return "p-none"

    if last_page is None or last_page == first_page:
        return f"p{first_page}"

    return f"p{first_page}-{last_page}"


def _docx_locator(first_paragraph, last_paragraph):
    if first_paragraph is None:
        return "para-none"

    if last_paragraph is None or last_paragraph == first_paragraph:
        return f"para{first_paragraph}"

    return f"para{first_paragraph}-{last_paragraph}"


def _span_of(block, block_index):
    return {
        "block_index": block_index,
        "kind": block.kind,
        "page_number": block.page_number,
        "paragraph_index": block.paragraph_index,
        "char_start": block.char_start,
        "char_end": block.char_end,
        "heading_path": list(block.heading_path),
    }


def _finalize(parsed, pieces, max_chars):
    """pieces: [(content, [TextBlock...])] -> [Chunk]，统一算 hash 与 id。"""

    chunks = []
    seen = {}
    # 用对象身份建索引：内容完全相同的两个块用 .index() 会取到前一个
    block_positions = {id(block): position for position, block in enumerate(parsed.blocks)}

    for chunk_index, (content, blocks) in enumerate(pieces):
        content_hash = compute_content_hash(content)
        first = blocks[0]
        pages = [block.page_number for block in blocks if block.page_number is not None]

        if parsed.file_type == "pdf":
            locator = _page_locator(min(pages), max(pages)) if pages else "p-none"
        elif parsed.file_type == "docx":
            paragraphs = [block.paragraph_index for block in blocks if block.paragraph_index is not None]
            locator = _docx_locator(min(paragraphs), max(paragraphs)) if paragraphs else "para-none"
        else:
            locator = "txt"

        key = (locator, content_hash)
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1

        # TXT 的片是按旧切片器切出来的合成块，不来自 parsed.blocks，统一指向 0 号整篇块
        spans = [_span_of(block, block_positions.get(id(block), 0)) for block in blocks]

        chunk_id = make_chunk_id(
            parsed.document_id,
            locator,
            occurrence,
            content_hash,
            parsed.document_hash,
            parsed.parser_version,
            CHUNKER_VERSION,
            max_chars,
        )

        chunks.append(
            Chunk(
                document_id=parsed.document_id,
                document_hash=parsed.document_hash,
                source_name=parsed.source_name,
                file_type=parsed.file_type,
                chunk_id=chunk_id,
                chunk_index=chunk_index,
                content=content,
                content_hash=content_hash,
                page_number=first.page_number,
                printed_page_number=first.printed_page_number,
                heading_path=tuple(first.heading_path),
                paragraph_index=first.paragraph_index,
                source_spans=tuple(spans),
                parser_version=parsed.parser_version,
                chunker_version=CHUNKER_VERSION,
            )
        )

    return chunks


def _group_blocks(parsed, max_chars):
    """把文本块贪心合并到不超过 max_chars 的片里。

    断点规则：
        - 标题行强制开新片（标题应该带着自己的正文）
        - 下一块会超预算就换片
        - 单块本身超预算：整块独占一片（不切碎正文，保留定位完整性）
    """

    pieces = []
    current_blocks = []
    current_len = 0

    def flush():
        if not current_blocks:
            return

        content = "\n".join(block.text for block in current_blocks).strip()
        if content:
            pieces.append((content, list(current_blocks)))
        current_blocks.clear()

    for block in parsed.blocks:
        if not block.text.strip():
            continue

        if block.kind == "heading" and current_blocks:
            flush()
            current_len = 0

        projected = current_len + (1 if current_blocks else 0) + len(block.text)

        if current_blocks and projected > max_chars:
            flush()
            current_len = 0

        current_blocks.append(block)
        current_len += (1 if len(current_blocks) > 1 else 0) + len(block.text)

        if current_len >= max_chars:
            flush()
            current_len = 0

    flush()
    return pieces


def _txt_pieces(parsed, max_chars, txt_splitter):
    """TXT：用旧切片函数产出内容，再按出现顺序回填定位。"""

    block = parsed.blocks[0]
    whole = block.text

    if txt_splitter is not None:
        # 旧实现 split_into_chunks 返回字符串列表
        texts = list(txt_splitter(whole, max_chars))
    else:
        texts = [content for content, _blocks in _group_blocks(parsed, max_chars)]

    pieces = []
    cursor = 0

    for text in texts:
        if not text or not text.strip():
            continue

        index = whole.find(text, cursor)
        start = index if index >= 0 else cursor
        end = start + len(text)
        cursor = end

        piece_block = TextBlock(
            text=text,
            kind="text",
            char_start=start,
            char_end=end,
            paragraph_index=len(pieces),
        )
        pieces.append((text, [piece_block]))

    return pieces


def chunk_document(parsed, max_chars=400, txt_splitter=None):
    """把 ParsedDocument 切成 Chunk 列表。txt_splitter 仅 TXT 需要。"""

    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")

    if parsed.file_type == "txt":
        pieces = _txt_pieces(parsed, max_chars, txt_splitter)
    else:
        pieces = _group_blocks(parsed, max_chars)

    return _finalize(parsed, pieces, max_chars)
