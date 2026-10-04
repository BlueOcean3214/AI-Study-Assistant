"""Embedding 服务：调用本地 Ollama 的 bge-m3 模型把文本转成向量。

只使用 requests，不引入新的第三方依赖。

接口说明：
    get_embedding("定积分")        -> [0.01, -0.23, ...]   单条文本（1024 维）
    get_embeddings(["第一段", "第二段"])  -> [[...], [...]] 一次请求，文本不宜过多
    split_into_batches(很多文本)     -> 按 EMBEDDING_BATCH_SIZE 切批

注意：一次请求里的文本越多越慢，实测 512 条就会失败、5243 条会超时，
所以批量生成向量时请配合 split_into_batches() 使用小批量。
"""

import requests


OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
EMBEDDING_MODEL = "bge-m3"
EMBEDDING_TIMEOUT = 60

# 单次请求最多发送多少条文本。
# 实测：64 条约 8.6 秒可成功，512 条会失败，几千条会撞上 60 秒超时。
EMBEDDING_BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """调用 embedding 接口失败时抛出的异常。"""


def split_into_batches(texts, batch_size=EMBEDDING_BATCH_SIZE):
    """把文本按 batch_size 切成小批（生成器），避免一次请求发送过多文本。"""

    if batch_size < 1:
        raise ValueError("batch_size 必须大于 0")

    texts = list(texts)

    for start in range(0, len(texts), batch_size):
        yield texts[start : start + batch_size]


def get_embeddings(texts):
    """把一批文本转成向量，返回 [[float, ...], ...]（一次 HTTP 请求）。

    文本条数请控制在 EMBEDDING_BATCH_SIZE 左右，或改用 split_into_batches() 分批。
    """

    if isinstance(texts, str):
        texts = [texts]

    texts = list(texts)

    if not texts:
        return []

    try:
        response = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": EMBEDDING_MODEL, "input": texts},
            timeout=EMBEDDING_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data["embeddings"]
    except requests.exceptions.Timeout as error:
        raise EmbeddingError(
            f"Ollama embedding 请求超时（{EMBEDDING_TIMEOUT} 秒，本批 {len(texts)} 条），"
            "可以调小 EMBEDDING_BATCH_SIZE 后重试"
        ) from error
    except requests.exceptions.HTTPError as error:
        raise EmbeddingError(
            f"Ollama embedding 返回 HTTP 错误（本批 {len(texts)} 条）：{error}"
        ) from error
    except requests.exceptions.RequestException as error:
        raise EmbeddingError(
            "无法连接 Ollama embedding 接口，"
            f"请确认 Ollama 已启动，并且已执行 ollama pull {EMBEDDING_MODEL}"
            f"（{type(error).__name__}）"
        ) from error
    except (ValueError, KeyError, TypeError) as error:
        raise EmbeddingError("Ollama embedding 返回内容格式异常") from error

    if len(embeddings) != len(texts) or any(not vector for vector in embeddings):
        raise EmbeddingError("Ollama embedding 返回的向量数量与输入不一致")

    return embeddings


def get_embedding(text):
    """把单条文本转成向量，返回 [float, ...]。"""

    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("embedding 的输入必须是非空字符串")

    return get_embeddings([text])[0]

