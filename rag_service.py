"""Mini-RAG v1.2：把知识库切片，再按关键词做 Top-K 检索，并返回来源信息。

流程：
knowledge/*.txt
-> load_documents()            读取整篇文档
-> split_into_chunks()         切片（空行 / 标题 / 最大长度）
-> load_chunks()               得到 [{"content": 片段, "source": 文件名}]
-> retrieve_chunks()           逐个 chunk 打分，取 Top-K
                               返回 [{"content", "source", "score"}]
-> retrieve_context()          兼容旧代码，只把 content 拼成字符串
"""

import re
from pathlib import Path


# 用脚本所在目录定位知识库，避免依赖启动时的工作目录
KNOWLEDGE_PATH = Path(__file__).resolve().parent / "knowledge"

# 最终返回得分最高的前几个 chunk
TOP_K = 3

# 每个 chunk 允许的最大字数（300~500 之间，这里取 400）
CHUNK_MAX_CHARS = 400

# 标题行最大长度：以冒号结尾且不超过这个长度，就认为它是标题（开启新 chunk）
HEADING_MAX_CHARS = 30

# 关键词片段全部落空时，用字符重合度兜底召回的最低比例
MIN_CHAR_OVERLAP = 0.5

# 兜底召回要求查询至少有这么多个可检索字符，避免单个字乱召回
MIN_FALLBACK_QUERY_CHARS = 2

_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+")
_CJK_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_SEARCHABLE_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_BLANK_LINE_PATTERN = re.compile(r"\n\s*\n")
_SENTENCE_END_PATTERN = re.compile(r"[。！？；!?;]")

# 中文没有空格，query.split() 会把“定积分怎么学”当成一个整体，无法与知识库里的
# “定积分”匹配，所以改成 n-gram 切分，并让越长的片段权重越高。
# 这里刻意不用单个汉字：像“推荐一部电影”会因为“一/部”单字命中而被误召回。
_NGRAM_WEIGHTS = ((2, 3), (3, 5))

# 疑问词等常见虚词不作为检索关键词，避免无关提问被误召回
_STOPWORDS = {"的", "了", "吗", "呢", "怎么", "如何", "什么", "怎样", "咋"}


def load_documents():
    """读取 knowledge 目录下的所有 txt 文档，返回 [{"text", "source"}]。"""

    documents = []

    if not KNOWLEDGE_PATH.is_dir():
        return documents

    for file in sorted(KNOWLEDGE_PATH.glob("*.txt")):
        content = file.read_text(encoding="utf-8").strip()

        if content:
            documents.append({"text": content, "source": file.name})

    return documents


def _split_long_paragraph(paragraph, max_chars):
    """段落本身超过 max_chars 时，尽量在句子结尾处切开。"""

    pieces = []

    while len(paragraph) > max_chars:
        window = paragraph[: max_chars + 1]

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
    """把一篇文档切成若干 chunk，规则尽量简单：

    1. 先按空行分段；
    2. 段落本身太长时，按句子结尾再切；
    3. 遇到“标题行”就开一个新 chunk，否则向后合并，直到接近 max_chars。
    """

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


def load_chunks():
    """把所有文档切片，返回 [{"content": 片段正文, "source": 文件名}]。"""

    chunks = []

    for document in load_documents():
        for chunk_text in split_into_chunks(document["text"]):
            chunks.append({"content": chunk_text, "source": document["source"]})

    return chunks


def _query_keywords(query):
    """把查询切成带权重的关键词：英文/数字按词，中文按 2~3 字片段。"""

    keywords = {}

    for word in _WORD_PATTERN.findall(query):
        keywords[word.lower()] = max(keywords.get(word.lower(), 0), 5)

    for run in _CJK_RUN_PATTERN.findall(query):
        for size, weight in _NGRAM_WEIGHTS:
            for start in range(len(run) - size + 1):
                token = run[start : start + size]

                if token in _STOPWORDS:
                    continue

                keywords[token] = max(keywords.get(token, 0), weight)

    return keywords


def _char_overlap(query, document):
    """查询与文本的字符重合比例，用于关键词全部落空时的兜底召回。"""

    query_chars = set(_SEARCHABLE_CHAR_PATTERN.findall(query))

    if not query_chars:
        return 0.0

    document_chars = set(_SEARCHABLE_CHAR_PATTERN.findall(document))

    return len(query_chars & document_chars) / len(query_chars)


def score_chunk(chunk_text, keywords):
    """给一个 chunk 打分：命中的关键词越多、片段越长，score 越高。"""

    return sum(weight for token, weight in keywords.items() if token in chunk_text)


def retrieve_chunks(query, top_k=TOP_K):
    """打分并返回得分最高的前 top_k 个片段。

    返回格式：
    [{"content": "知识片段内容", "source": "math.txt", "score": 10}]
    """

    if not isinstance(query, str) or not query.strip():
        return []

    query = query.strip()

    keywords = _query_keywords(query)

    chunks = load_chunks()

    results = []

    for chunk in chunks:
        score = score_chunk(chunk["content"], keywords)

        if score > 0:
            results.append(
                {
                    "content": chunk["content"],
                    "source": chunk["source"],
                    "score": score,
                }
            )

    if not results:
        # 兜底：一个关键词片段都没命中时（例如有错别字），用字符重合度找最接近的
        # chunk，此时 score 是字符重合比例
        query_chars = set(_SEARCHABLE_CHAR_PATTERN.findall(query))

        if len(query_chars) >= MIN_FALLBACK_QUERY_CHARS:
            for chunk in chunks:
                overlap = _char_overlap(query, chunk["content"])

                if overlap >= MIN_CHAR_OVERLAP:
                    results.append(
                        {
                            "content": chunk["content"],
                            "source": chunk["source"],
                            "score": round(overlap, 2),
                        }
                    )

    results.sort(key=lambda item: item["score"], reverse=True)

    return results[:top_k]


def format_context(chunks):
    """可选辅助函数：把片段拼成带来源标记的文本，方便展示引用来源。"""

    blocks = [
        f"[来源: {chunk['source']}]\n\n{chunk['content']}" for chunk in chunks
    ]

    return "\n\n---\n\n".join(blocks)


def retrieve_context(query, top_k=TOP_K):
    """
    根据关键词寻找相关知识

    为了兼容旧代码，只返回拼好的字符串；
    需要来源信息时改用 retrieve_chunks()。
    """

    chunks = retrieve_chunks(query, top_k=top_k)

    context = "\n".join(chunk["content"] for chunk in chunks)

    print("RAG长度:", len(context))
    print("检索结果数量:", len(chunks))
    print("Top scores:", [chunk["score"] for chunk in chunks])

    return context
