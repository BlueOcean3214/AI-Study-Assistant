"""文档导入服务：保存上传的 txt、读取内容、切片，供检索层使用。

职责划分：
    save_document(filename, content)   保存到 knowledge/，返回 source（相对路径）
    read_text(path)                    读取 txt（自动兼容 UTF-8 / UTF-8 BOM / GBK）
    split_into_chunks(text)            文本切片，每片最多 400 字（300~500 区间）
    chunk_text(text, source)           切片并给每片带上 source
    load_documents() / load_chunks()   递归读取整个知识库

本模块只使用标准库，不依赖 FastAPI，之后实现上传接口时可以直接调用：
    source = document_service.save_document(file.filename, await file.read())
"""

import re
from pathlib import Path


# 用脚本所在目录定位知识库，避免依赖启动时的工作目录
KNOWLEDGE_PATH = Path(__file__).resolve().parent / "knowledge"

# 每个 chunk 允许的最大字数（300~500 之间，这里取 400）
CHUNK_MAX_CHARS = 400

# 标题行最大长度：以冒号结尾且不超过这个长度，就认为它是标题（开启新 chunk）
HEADING_MAX_CHARS = 30

# 只接受 txt
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
    """校验并返回安全的文件名（只允许形如 xxx.txt）。

    会拒绝路径分隔符和 .. ，避免上传时把文件写到 knowledge 目录之外。
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

    if not name.lower().endswith(ALLOWED_SUFFIX):
        raise DocumentError("只支持 .txt 文件")

    if len(name) > FILENAME_MAX_CHARS:
        raise DocumentError(f"文件名过长（最多 {FILENAME_MAX_CHARS} 个字符）")

    if not name[: -len(ALLOWED_SUFFIX)].strip():
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


def load_documents(directory=None):
    """递归读取目录下所有 txt，返回 [{"content", "source"}]。

    source 是相对知识库根目录的路径（统一用正斜杠），例如 "math/定积分.txt"。
    """

    root = Path(directory) if directory is not None else KNOWLEDGE_PATH

    documents = []

    if not root.is_dir():
        return documents

    for file in sorted(root.rglob("*.txt")):
        try:
            content = _normalize(read_text(file))
        except DocumentError as error:
            # 单个文件坏了不影响整个知识库
            print("跳过无法读取的知识文件:", error)
            continue

        if content:
            documents.append(
                {
                    "content": content,
                    "source": file.relative_to(root).as_posix(),
                }
            )

    return documents


def load_chunks(directory=None):
    """递归读取并切片，返回 [{"content", "source"}]。"""

    chunks = []

    for document in load_documents(directory):
        chunks.extend(chunk_text(document["content"], document["source"]))

    return chunks


def save_document(filename, content, directory=None, overwrite=False):
    """把上传的 txt 保存进知识库，返回保存后的 source（相对路径）。

    content 可以是 str，也可以是上传接口读到的 bytes（自动识别 UTF-8 / GBK）。
    保存时会统一换行符为 \n 并去掉首尾空白。
    同名文件内容不同时默认拒绝覆盖，避免误删已有知识；确实要覆盖时传 overwrite=True。
    内容完全相同的重复上传会直接当作成功（幂等）。
    """

    root = Path(directory) if directory is not None else KNOWLEDGE_PATH

    source = sanitize_filename(filename)

    if isinstance(content, (bytes, bytearray)):
        text = _decode(bytes(content))
    elif isinstance(content, str):
        text = content
    else:
        raise DocumentError("content 必须是 str 或 bytes")

    text = _normalize(text)

    if not text:
        raise DocumentError("文件内容为空，未保存")

    target = root / source

    if target.exists() and not overwrite:
        if _normalize(read_text(target)) == text:
            return source
        raise DocumentError(f"{source} 已存在且内容不同；如需覆盖请传 overwrite=True")

    try:
        root.mkdir(parents=True, exist_ok=True)
        # 按字节写入，避免 Windows 把 \n 翻译成 \r\n
        target.write_bytes((text + "\n").encode("utf-8"))
    except OSError as error:
        raise DocumentError(f"保存文件失败：{source}") from error

    return source
