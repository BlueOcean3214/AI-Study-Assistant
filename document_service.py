"""文档导入服务：保存上传的文档、读取内容、切片，供检索层使用。

职责划分（S1 起）：
    save_document(filename, content)   保存到 knowledge/，返回 source（相对路径）
    read_text(path)                    读取 txt（自动兼容 UTF-8 / UTF-8 BOM / GBK）
    split_into_chunks(text)            TXT 文本切片，每片最多 400 字（300~500 区间）
    chunk_text(text, source)           切片并给每片带上 source
    load_documents()                   递归读取整个知识库（txt/pdf/docx）
    load_chunks()                      递归读取并切片，默认仍返回 [{"content","source"}]
    load_chunk_records()               递归读取并切片，返回带 15 个元数据字段的记录
    scan_knowledge()                   扫描知识库，返回文档清单 + 结构化问题清单

兼容策略：
    默认返回值与 S1 之前逐键一致（{"content","source"}），富元数据必须显式开启
    （load_chunks(with_metadata=True) / load_chunk_records()），不改变既有调用方契约。

解析失败处理：
    单个文件解析失败不会中断整个知识库扫描，但也不会被静默丢弃——
    scan_knowledge() 会给出 {"source_name","error_code","error_message"} 形式的问题清单。
"""

import re
from pathlib import Path

from document_chunker import CHUNKER_VERSION, chunk_document
from document_parser import (
    PARSER_VERSION,
    ParseError,
    SUPPORTED_SUFFIXES,
    parse_bytes,
    parse_file,
    status_for_code,
)


# 用脚本所在目录定位知识库，避免依赖启动时的工作目录
KNOWLEDGE_PATH = Path(__file__).resolve().parent / "knowledge"

# 每个 chunk 允许的最大字数（300~500 之间，这里取 400）
CHUNK_MAX_CHARS = 400

# 标题行最大长度：以冒号结尾且不超过这个长度，就认为它是标题（开启新 chunk）
HEADING_MAX_CHARS = 30

# 允许的文档后缀（S1 起支持 TXT / PDF / DOCX）
ALLOWED_SUFFIXES = SUPPORTED_SUFFIXES

# 历史引用：仍指向 txt 后缀，供旧代码判断“纯文本”
ALLOWED_SUFFIX = ".txt"

# 文件名里不允许出现的字符（含 Windows 保留字符和空字符）
_INVALID_FILENAME_CHARS = set('<>:"/\\|?*\x00')

# 文件名最大长度
FILENAME_MAX_CHARS = 100

_ENCODINGS = ("utf-8-sig", "gbk")

_BLANK_LINE_PATTERN = re.compile(r"\n\s*\n")
_SENTENCE_END_PATTERN = re.compile(r"[。！？；!?;]")


class DocumentError(RuntimeError):
    """文档保存或读取失败时抛出的异常。"""


def _decode(data):
    """把 bytes 解码成文本，依次尝试 UTF-8（含 BOM）和 GBK。"""

    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise DocumentError("无法识别文件编码，请把 txt 另存为 UTF-8 后重新上传")


def _normalize(text):
    """统一换行符为 \\n 并去掉首尾空白。

    Windows 上 write_text 会把 \\n 翻译成 \\r\\n，这里统一处理，
    保证“保存的内容”和“读回来的内容”能直接比较。
    """

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def read_text(path):
    """读取单个 txt 文件的全部内容。"""

    try:
        return _decode(Path(path).read_bytes())
    except OSError as error:
        raise DocumentError(f"读取文件失败：{path}") from error


def sanitize_filename(filename):
    """校验并返回安全的文件名（允许 .txt / .pdf / .docx）。

    会拒绝路径分隔符和 .. ，避免上传时把文件写到 knowledge 目录之外。
    后缀大小写不敏感，返回值保留原始大小写。
    """

    if not isinstance(filename, str) or not filename.strip():
        raise DocumentError("文件名不能为空")

    name = filename.strip()

    if "/" in name or "\\" in name or Path(name).name != name:
        raise DocumentError("文件名不能包含路径分隔符")

    if name.startswith("."):
        raise DocumentError("文件名不能以点开头")

    if set(name) & _INVALID_FILENAME_CHARS:
        raise DocumentError('文件名包含非法字符 <>:"/\\|?*')

    suffix = Path(name).suffix.lower()

    if suffix not in ALLOWED_SUFFIXES:
        raise DocumentError("目前只支持 .txt / .pdf / .docx 文件")

    if len(name) > FILENAME_MAX_CHARS:
        raise DocumentError(f"文件名过长（最多 {FILENAME_MAX_CHARS} 个字符）")

    if not name[: -len(suffix)].strip():
        raise DocumentError("文件名缺少名称部分")

    return name


def _split_long_paragraph(paragraph, max_chars):
    """段落本身超过 max_chars 时，尽量在句子结尾处切开。"""

    pieces = []

    while len(paragraph) > max_chars:
        window = paragraph[:max_chars]

        cut = 0
        for match in _SENTENCE_END_PATTERN.finditer(window):
            cut = match.end()

        if cut == 0:
            # 整段没有句号之类的断点，只能按固定长度硬切
            cut = max_chars

        pieces.append(paragraph[:cut].strip())
        paragraph = paragraph[cut:].strip()

    if paragraph:
        pieces.append(paragraph)

    return pieces


def _starts_with_heading(paragraph):
    """判断一段是否以标题开头，例如“学习定积分第一步：”。"""

    first_line = paragraph.splitlines()[0].strip()

    return len(first_line) <= HEADING_MAX_CHARS and first_line.endswith(("：", ":"))


def split_into_chunks(text, max_chars=CHUNK_MAX_CHARS):
    """把文本切成若干 chunk，规则尽量简单：

    1. 先按空行分段；
    2. 段落本身太长时，按句子结尾再切；
    3. 遇到“标题行”就开一个新 chunk，否则向后合并，直到接近 max_chars。

    每个 chunk 的字数都不会超过 max_chars（默认 400，落在 300~500 区间）。
    """

    if not isinstance(text, str):
        raise DocumentError("text 必须是字符串")

    paragraphs = []

    for paragraph in _BLANK_LINE_PATTERN.split(text):
        paragraph = paragraph.strip()

        if paragraph:
            paragraphs.extend(_split_long_paragraph(paragraph, max_chars))

    chunks = []
    current = ""

    for paragraph in paragraphs:
        too_long = len(current) + len(paragraph) + 1 > max_chars

        if current and (_starts_with_heading(paragraph) or too_long):
            chunks.append(current)
            current = paragraph
        elif current:
            current = f"{current}\n{paragraph}"
        else:
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def chunk_text(text, source, max_chars=CHUNK_MAX_CHARS):
    """切片并给每片带上 source，返回 [{"content": ..., "source": ...}]。"""

    return [
        {"content": chunk, "source": source}
        for chunk in split_into_chunks(text, max_chars)
    ]


def _iter_parsed(directory):
    """逐个文件解析，产出 (parsed, issue)。单个文件失败只记 issue，不中断。"""

    root = Path(directory) if directory is not None else KNOWLEDGE_PATH

    if not root.is_dir():
        return

    files = [
        file
        for file in sorted(root.rglob("*"))
        if file.is_file() and file.suffix.lower() in ALLOWED_SUFFIXES
    ]

    for file in files:
        source = file.relative_to(root).as_posix()

        try:
            parsed = parse_file(file, source_name=source)
        except ParseError as error:
            # 解析失败必须有明确错误和状态，不能静默丢弃
            print(f"跳过无法解析的知识文件：{source}（{error.code}: {error.message}）")
            yield None, dict(error.to_issue(), file_type=file.suffix.lower().lstrip("."))
            continue
        except OSError as error:
            print(f"跳过无法读取的知识文件：{source}（{error}）")
            yield None, {
                "source_name": source,
                "file_type": file.suffix.lower().lstrip("."),
                "error_code": "read_failed",
                "error_message": str(error),
            }
            continue

        yield parsed, None


def load_documents(directory=None):
    """递归读取目录下所有支持的文档，返回 [{"content", "source"}]。

    source 是相对知识库根目录的路径（统一用正斜杠），例如 "math/定积分.txt"。
    PDF / DOCX 的 content 是解析出的全文（块之间空行分隔）。
    """

    documents = []

    for parsed, _issue in _iter_parsed(directory):
        if parsed is None:
            continue

        content = parsed.text()

        if content:
            documents.append({"content": content, "source": parsed.source_name})

    return documents


def load_chunk_records(directory=None):
    """递归读取并切片，返回每个切片的完整记录（含 chunk_id 与来源定位）。

    记录字段见 document_chunker.CHUNK_FIELDS，另外附带 "source"，
    方便检索层继续按既有的 source 键组装上下文。
    """

    records = []

    for parsed, _issue in _iter_parsed(directory):
        if parsed is None:
            continue

        txt_splitter = split_into_chunks if parsed.file_type == "txt" else None

        for chunk in chunk_document(parsed, CHUNK_MAX_CHARS, txt_splitter):
            record = chunk.to_record()
            # source 是历史键名，保留它让检索层无需区分新旧字段
            record["source"] = chunk.source_name
            records.append(record)

    return records


def load_chunks(directory=None, with_metadata=False):
    """递归读取并切片，默认返回 [{"content", "source"}]（与 S1 之前一致）。

    with_metadata=True 时返回带完整元数据的记录（等同 load_chunk_records()）。
    """

    records = load_chunk_records(directory)

    if with_metadata:
        return records

    return [{"content": record["content"], "source": record["source"]} for record in records]


def chunk_file(source, directory=None):
    """解析知识库里的单个文件并切片，返回 [Chunk]。

    source 是相对知识库根目录的路径；文件不存在则抛 DocumentError。
    上传接口用它在保存后统计切片数，不必重扫整个知识库。
    """

    root = Path(directory) if directory is not None else KNOWLEDGE_PATH
    file = root / source

    if not file.is_file():
        raise DocumentError(f"文件不存在：{source}")

    try:
        parsed = parse_file(file, source_name=source)
    except ParseError as error:
        raise DocumentError(f"{source} 解析失败（{error.code}）：{error.message}") from error

    txt_splitter = split_into_chunks if parsed.file_type == "txt" else None

    return chunk_document(parsed, CHUNK_MAX_CHARS, txt_splitter)


def scan_knowledge(directory=None):
    """扫描知识库，返回文档清单、切片数与结构化问题清单。

    返回 {"documents": [...], "issues": [...], "chunk_count": int}：
        documents 覆盖扫描到的每个文件（含解析失败的），每条含
            source / source_name / file_type / status / document_id / document_hash /
            chunk_count / bytes / error_code / error_message / parser_version / chunker_version
        issues 只保留解析失败的文件（source_name / error_code / error_message / file_type），
            方便调用方单独报警；两者不重复叙事，status 一律以此为准。
    """

    root = Path(directory) if directory is not None else KNOWLEDGE_PATH
    documents = []
    issues = []
    chunk_count = 0

    for parsed, issue in _iter_parsed(root):
        if issue is not None:
            source = issue.get("source_name")
            file = root / source if source else None
            issues.append(issue)
            documents.append(
                {
                    "source": source,
                    "source_name": source,
                    "file_type": issue.get("file_type"),
                    "status": status_for_code(issue.get("error_code")),
                    "document_id": None,
                    "document_hash": None,
                    "chunk_count": 0,
                    "bytes": file.stat().st_size if file is not None and file.is_file() else None,
                    "error_code": issue.get("error_code"),
                    "error_message": issue.get("error_message"),
                    "parser_version": PARSER_VERSION,
                    "chunker_version": CHUNKER_VERSION,
                }
            )
            continue

        txt_splitter = split_into_chunks if parsed.file_type == "txt" else None
        chunks = chunk_document(parsed, CHUNK_MAX_CHARS, txt_splitter)
        file = root / parsed.source_name
        chunk_count += len(chunks)
        documents.append(
            {
                "source": parsed.source_name,
                "source_name": parsed.source_name,
                "file_type": parsed.file_type,
                "status": "ok",
                "document_id": parsed.document_id,
                "document_hash": parsed.document_hash,
                "chunk_count": len(chunks),
                "bytes": file.stat().st_size if file.is_file() else None,
                "error_code": None,
                "error_message": None,
                "parser_version": parsed.parser_version,
                "chunker_version": CHUNKER_VERSION,
            }
        )

    return {"documents": documents, "issues": issues, "chunk_count": chunk_count}


def save_document(filename, content, directory=None, overwrite=False):
    """把上传的文档保存进知识库，返回保存后的 source（相对路径）。

    txt：content 可以是 str，也可以是 bytes（自动识别 UTF-8 / GBK），
         保存时统一换行符为 \\n 并去掉首尾空白。
    pdf/docx：content 必须是原始 bytes，保存时会先解析一次做校验，
         解析失败直接抛 DocumentError，不会把坏文件写进知识库。
    同名文件内容不同时默认拒绝覆盖，避免误删已有知识；确实要覆盖时传 overwrite=True。
    内容完全相同的重复上传会直接当作成功（幂等）。
    """

    root = Path(directory) if directory is not None else KNOWLEDGE_PATH

    source = sanitize_filename(filename)
    suffix = Path(source).suffix.lower()

    if suffix == ".txt":
        if isinstance(content, (bytes, bytearray)):
            text = _decode(bytes(content))
        elif isinstance(content, str):
            text = content
        else:
            raise DocumentError("content 必须是 str 或 bytes")

        text = _normalize(text)

        if not text:
            raise DocumentError("文件内容为空，未保存")

        payload = (text + "\n").encode("utf-8")
    else:
        if isinstance(content, str):
            raise DocumentError(f"{suffix} 文件必须以 bytes 上传")
        if not isinstance(content, (bytes, bytearray)):
            raise DocumentError("content 必须是 str 或 bytes")

        payload = bytes(content)

        if not payload:
            raise DocumentError("文件内容为空，未保存")

        # 先解析校验，坏文件不落盘
        try:
            parse_bytes(payload, source)
        except ParseError as error:
            raise DocumentError(f"{source} 解析失败（{error.code}）：{error.message}") from error

    target = root / source

    if target.exists() and not overwrite:
        try:
            existing = target.read_bytes()
        except OSError as error:
            raise DocumentError(f"读取已有文件失败：{source}") from error

        if existing == payload:
            return source

        raise DocumentError(f"{source} 已存在且内容不同；如需覆盖请传 overwrite=True")

    try:
        root.mkdir(parents=True, exist_ok=True)
        # 按字节写入，避免 Windows 把 \n 翻译成 \r\n
        target.write_bytes(payload)
    except OSError as error:
        raise DocumentError(f"保存文件失败：{source}") from error

    return source
