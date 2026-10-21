"""Embedding 服务（Provider 适配层）：文本转向量。

支持两种 Provider（环境变量 EMBEDDING_PROVIDER）：
- openai_compatible（默认，阿里云百炼等云端 OpenAI 兼容 /embeddings 接口）
- ollama（本地回退，Ollama /api/embed）

配置（环境变量）：
- EMBEDDING_PROVIDER    openai_compatible（默认）或 ollama
- EMBEDDING_BASE_URL    openai_compatible：API 基地址（默认百炼兼容端点，
                        具体模型/地域/地址以百炼控制台实际可用配置为准）
                        ollama：服务地址（默认 http://localhost:11434）
- EMBEDDING_API_KEY     openai_compatible 必填；ollama 忽略（不写进代码/提交）
- EMBEDDING_MODEL       openai_compatible 默认 text-embedding-v4；ollama 默认 bge-m3
- EMBEDDING_DIMENSIONS  向量维度（默认 1024；设为 0 表示不传 dimensions 参数、
                        采用模型默认维度）。openai_compatible 路径返回向量长度
                        必须与之一致，不一致直接报错，绝不混用
- EMBEDDING_BATCH_SIZE  单次请求最多条数（openai_compatible 默认 10，ollama 默认 32）
- EMBEDDING_TIMEOUT     单次请求超时秒数（默认 60）
- OLLAMA_EMBED_URL      兼容旧配置：ollama 完整 embed 地址（优先级最高）

接口说明（保持不变，业务层无感知）：
    get_embedding("定积分")               -> [0.01, -0.23, ...]   单条文本
    get_embeddings(["第一段", "第二段"])  -> [[...], [...]] 一次请求
    split_into_batches(很多文本)          -> 按当前 batch_size 切批
    cache_identity()                     -> 向量缓存身份串（provider:model@dim），
                                            换模型/Provider/维度后旧缓存自动判废

注意：一次请求里的文本越多越慢，请配合 split_into_batches() 使用小批量。
文档向量与查询向量必须走同一份配置（同一个模型与维度），由本模块统一保证。
"""

import logging
import os
import time
from dataclasses import dataclass

import cloud_errors
import env_loader
import requests

# 启动时自动读取根目录 .env（已存在的环境变量不覆盖；幂等）
env_loader.load_env()

# 关键链路日志：只记 provider/model/条数/耗时，绝不记文本内容/密钥
logger = logging.getLogger("ai_study.embedding")


# 兼容旧配置（完整 embed 地址）；不设置时按 EMBEDDING_BASE_URL 拼接。
# reload_settings() 会把当前生效的完整地址写回本常量，
# 测试可直接 monkeypatch 本常量模拟 embedding 故障（既有约定）。
OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"
LEGACY_OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"

# 阿里云百炼（DashScope）OpenAI 兼容端点。
# 型号 / 地域 / 端点以百炼控制台实际可用配置为准，可用 EMBEDDING_BASE_URL 覆盖。
DEFAULT_OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

PROVIDER_OPENAI = "openai_compatible"
PROVIDER_OLLAMA = "ollama"
VALID_PROVIDERS = {PROVIDER_OPENAI, PROVIDER_OLLAMA}

DEFAULT_MODELS = {
    PROVIDER_OPENAI: "text-embedding-v4",
    PROVIDER_OLLAMA: "bge-m3",
}

DEFAULT_BATCH_SIZES = {
    PROVIDER_OPENAI: 10,
    PROVIDER_OLLAMA: 32,
}

DEFAULT_DIMENSIONS = 1024
DEFAULT_TIMEOUT = 60


class EmbeddingError(RuntimeError):
    """调用 embedding 接口失败时抛出的异常。"""


class EmbeddingConfigError(EmbeddingError):
    """Embedding 配置错误（缺必需配置 / Provider 非法）。"""


@dataclass
class EmbeddingSettings:
    provider: str
    model: str
    base_url: str
    api_key: str | None
    dimensions: int | None
    batch_size: int
    timeout: int

    def describe(self):
        """用于日志/错误的安全描述（不含 API Key）。"""

        return (
            f"provider={self.provider} model={self.model} "
            f"dimensions={self.dimensions} url={self.base_url}"
        )


def _env(name, default=None):
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value or default


def _positive_int(value, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default

    return number if number > 0 else default


_SETTINGS = None


def reset_settings():
    """清空配置缓存并刷新模块常量（测试切换环境变量后使用）。"""

    global _SETTINGS

    _SETTINGS = None
    reload_settings()


def get_settings():
    """解析并校验 Embedding 配置（进程内缓存；reset_settings() 复位）。"""

    global _SETTINGS

    if _SETTINGS is not None:
        return _SETTINGS

    provider = (_env("EMBEDDING_PROVIDER") or PROVIDER_OPENAI).lower()

    if provider not in VALID_PROVIDERS:
        raise EmbeddingConfigError(
            f"EMBEDDING_PROVIDER 取值非法：{provider!r}，"
            "只支持 openai_compatible 或 ollama"
        )

    model = (
        _env("OLLAMA_EMBED_MODEL") or DEFAULT_MODELS[PROVIDER_OLLAMA]
        if provider == PROVIDER_OLLAMA
        else _env("EMBEDDING_MODEL") or DEFAULT_MODELS[PROVIDER_OPENAI]
    )

    if provider == PROVIDER_OPENAI:
        api_key = _env("EMBEDDING_API_KEY")

        if not api_key:
            raise EmbeddingConfigError(
                "使用 openai_compatible Embedding 时必须设置 EMBEDDING_API_KEY"
                "（密钥只放环境变量，不要写进代码）"
            )

        base_url = _env("EMBEDDING_BASE_URL") or DEFAULT_OPENAI_BASE_URL
        base_url = base_url.rstrip("/")

        if base_url.endswith("/embeddings"):
            base_url = base_url[: -len("/embeddings")]
    else:
        api_key = None

        # ollama 只认 OLLAMA_EMBED_URL（完整地址）/ 默认本地；
        # EMBEDDING_BASE_URL 只服务 openai_compatible，避免云端地址污染本地回退
        legacy = _env("OLLAMA_EMBED_URL")

        if legacy:
            base_url = legacy[: -len("/api/embed")] if legacy.endswith("/api/embed") else legacy
        else:
            base_url = "http://localhost:11434"

        base_url = base_url.rstrip("/")

    dimensions_raw = _env("EMBEDDING_DIMENSIONS")

    if dimensions_raw is None:
        dimensions = DEFAULT_DIMENSIONS
    else:
        try:
            parsed = int(dimensions_raw)
        except ValueError as error:
            raise EmbeddingConfigError(
                f"EMBEDDING_DIMENSIONS 必须是整数（0 表示用模型默认维度）：{dimensions_raw!r}"
            ) from error

        dimensions = parsed if parsed > 0 else None

    settings = EmbeddingSettings(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        dimensions=dimensions,
        batch_size=_positive_int(
            _env("EMBEDDING_BATCH_SIZE"), DEFAULT_BATCH_SIZES[provider]
        ),
        timeout=_positive_int(_env("EMBEDDING_TIMEOUT"), DEFAULT_TIMEOUT),
    )

    _SETTINGS = settings

    return settings


# ==========================
# 兼容旧常量（业务/测试按名字引用；reload_settings() 刷新）
# ==========================

EMBEDDING_MODEL = DEFAULT_MODELS[PROVIDER_OPENAI]
EMBEDDING_TIMEOUT = DEFAULT_TIMEOUT
EMBEDDING_BATCH_SIZE = DEFAULT_BATCH_SIZES[PROVIDER_OPENAI]
EMBEDDING_PROVIDER = PROVIDER_OPENAI


def reload_settings():
    """把当前配置快照到模块常量（保持旧名字兼容），返回 settings。

    配置非法时不抛异常（常量回落到安全默认），真正调用时再报清晰错误。
    """

    global EMBEDDING_MODEL, EMBEDDING_TIMEOUT, EMBEDDING_BATCH_SIZE, EMBEDDING_PROVIDER
    global OLLAMA_EMBED_URL

    try:
        settings = get_settings()
    except EmbeddingConfigError:
        return None

    EMBEDDING_MODEL = settings.model
    EMBEDDING_TIMEOUT = settings.timeout
    EMBEDDING_BATCH_SIZE = settings.batch_size
    EMBEDDING_PROVIDER = settings.provider
    OLLAMA_EMBED_URL = settings.base_url + "/api/embed"

    return settings


reload_settings()


def cache_identity():
    """向量缓存身份串：provider:model@dim。

    换 Provider / 模型 / 维度后身份串变化，旧缓存会被判废归档（绝不混用）。
    配置非法时返回带错误标记的串（缓存校验必然不通过，安全优先）。
    """

    try:
        settings = get_settings()
    except EmbeddingConfigError as error:
        return f"invalid-config:{error}"

    return f"{settings.provider}:{settings.model}@{settings.dimensions or 'default'}"


def split_into_batches(texts, batch_size=None):
    """把文本按 batch_size 切成小批（生成器），避免一次请求发送过多文本。"""

    if batch_size is None:
        batch_size = EMBEDDING_BATCH_SIZE

    if batch_size < 1:
        raise ValueError("batch_size 必须大于 0")

    texts = list(texts)

    for start in range(0, len(texts), batch_size):
        yield texts[start : start + batch_size]


def _validate_vectors(vectors, texts, settings):
    """校验返回向量：条数一致、非空、维度与配置一致（绝不静默混用不同维度）。"""

    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise EmbeddingError("embedding 返回的向量数量与输入不一致")

    if settings.dimensions is None:
        return

    expected = settings.dimensions

    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingError(
                f"embedding 返回向量维度 {len(vector)} 与配置 EMBEDDING_DIMENSIONS={expected} 不一致"
                "（不同模型/维度的向量不可混用，请检查 EMBEDDING_MODEL 与 EMBEDDING_DIMENSIONS）"
            )


def _get_embeddings_ollama(texts, settings):
    """Ollama /api/embed 后端（本地回退路径，有限重试）。"""

    policy = cloud_errors.RetryPolicy("EMBEDDING_MAX_RETRIES")

    def request():
        response = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": settings.model, "input": texts},
            timeout=settings.timeout,
        )
        response.raise_for_status()

        return response.json()

    def classify(error):
        if isinstance(error, requests.exceptions.Timeout):
            return "timeout"
        if isinstance(error, requests.exceptions.HTTPError):
            status = error.response.status_code if error.response is not None else None

            return cloud_errors.classify_status(status)[0]
        if isinstance(error, requests.exceptions.RequestException):
            return "connection"
        return None

    data, failure = policy.run(request, classify)

    if failure is not None:
        error_type, message, attempts = failure

        if error_type == "timeout":
            raise EmbeddingError(
                f"Ollama embedding 请求超时（{settings.timeout} 秒，本批 {len(texts)} 条），"
                "可以调小 EMBEDDING_BATCH_SIZE 后重试"
            )
        if error_type == "connection":
            raise EmbeddingError(
                "无法连接 Ollama embedding 接口，"
                f"请确认 Ollama 已启动，并且已执行 ollama pull {settings.model}"
                f"（{message}）"
            )

        raise EmbeddingError(
            f"Ollama embedding 调用失败（{message}，尝试 {attempts} 次）"
        )

    try:
        embeddings = data["embeddings"]
    except (ValueError, KeyError, TypeError) as error:
        raise EmbeddingError("Ollama embedding 返回内容格式异常") from error

    return embeddings


def _import_openai():
    try:
        import openai
    except ImportError as error:
        raise EmbeddingConfigError(
            "使用 openai_compatible Embedding 需要安装 openai 包（pip install openai）"
        ) from error

    return openai


def _get_embeddings_openai(texts, settings):
    """OpenAI 兼容 /embeddings 后端（阿里云百炼等云端，有限重试）。

    - 复用已安装的 openai SDK（不引入额外客户端）
    - 响应按 index 排序后取向量（部分实现不保证顺序）
    - 传 dimensions 参数（配置了才传），返回维度由 _validate_vectors 强校验
    - 错误按 429/5xx/认证/参数/超时分类；429/5xx/连接失败有限重试
    """

    openai = _import_openai()
    policy = cloud_errors.RetryPolicy("EMBEDDING_MAX_RETRIES")

    client = openai.OpenAI(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.timeout,
        max_retries=0,
    )

    payload = {"model": settings.model, "input": texts}

    if settings.dimensions is not None:
        payload["dimensions"] = settings.dimensions

    def request():
        return client.embeddings.create(**payload)

    def classify(error):
        if isinstance(error, openai.APITimeoutError):
            return "timeout"
        if isinstance(error, openai.APIConnectionError):
            return "connection"
        if isinstance(error, openai.APIStatusError):
            return cloud_errors.classify_status(getattr(error, "status_code", None))[0]
        return None

    response, failure = policy.run(request, classify)

    if failure is not None:
        error_type, message, attempts = failure

        if error_type == "timeout":
            raise EmbeddingError(
                f"云端 embedding 请求超时（{settings.timeout} 秒，本批 {len(texts)} 条）"
            )
        if error_type == "connection":
            raise EmbeddingError(
                "无法连接云端 embedding 接口，请检查 EMBEDDING_BASE_URL 与网络"
                f"（{message}）"
            )
        if error_type == "auth_error":
            raise EmbeddingError(
                f"云端 embedding 认证失败，请检查 EMBEDDING_API_KEY（{message}）"
            )
        if error_type == "invalid_request":
            raise EmbeddingError(
                f"云端 embedding 请求参数错误，请检查 EMBEDDING_MODEL/EMBEDDING_DIMENSIONS（{message}）"
            )

        raise EmbeddingError(
            f"云端 embedding 调用失败（{message}，尝试 {attempts} 次）"
        )

    items = sorted(response.data, key=lambda item: item.index)

    return [list(item.embedding) for item in items]


def get_embeddings(texts):
    """把一批文本转成向量，返回 [[float, ...], ...]（一次 HTTP 请求）。

    文档向量与查询向量都走本函数：同一个模型、同一份维度配置。
    文本条数请控制在 EMBEDDING_BATCH_SIZE 左右，或改用 split_into_batches() 分批。
    """

    if isinstance(texts, str):
        texts = [texts]

    texts = list(texts)

    if not texts:
        return []

    try:
        settings = get_settings()
    except EmbeddingConfigError as error:
        raise EmbeddingError(f"Embedding 配置错误：{error}") from error

    started = time.perf_counter()

    if settings.provider == PROVIDER_OLLAMA:
        vectors = _get_embeddings_ollama(texts, settings)
    else:
        vectors = _get_embeddings_openai(texts, settings)

    _validate_vectors(vectors, texts, settings)

    # 真实耗时观测（不记录文本内容）
    elapsed = round(time.perf_counter() - started, 3)
    logger.info(
        "embedding_call provider=%s model=%s count=%d elapsed=%.2fs dim=%s",
        settings.provider, settings.model, len(texts), elapsed, settings.dimensions,
    )

    if elapsed > settings.timeout:
        print(f"embedding 批次耗时偏高：{elapsed}s（{len(texts)} 条，{settings.provider}）")

    return vectors


def get_embedding(text):
    """把单条文本转成向量，返回 [float, ...]。"""

    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("embedding 的输入必须是非空字符串")

    return get_embeddings([text])[0]
