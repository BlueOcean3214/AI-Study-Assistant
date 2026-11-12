r"""真实后端就绪检查（测试用）。

验收口径（2026-11-12 复审后收紧）
--------------------------------
本项目的「真实 API」只认**云端** OpenAI 兼容服务商：

- 对话：`llm_client.get_settings().provider == "openai_compatible"`
- Embedding：`embedding_service.get_settings().provider == "openai_compatible"`

`provider=ollama`（本地 Ollama）**不属于**本次 API 化验收。`cloud_chat_ready()` /
`cloud_embedding_ready()` 对本地 Provider 一律返回 False，测试必须明确 SKIP 并说明原因，
绝不能把本机模型跑出的结果当作真实 API 通过。

本地 Ollama 的探测只保留在 `local_backend_status()` 里，纯诊断用，不参与任何验收断言；
若将来需要本地模型的回归测试，应单独命名、单独统计。

输出纪律：只打印 provider / model / base_url 与「Key 是否存在」，绝不打印 Key 的值。
"""

import os

import embedding_service
import llm_client

PROBE_TIMEOUT_SECONDS = 2

CLOUD_HINT_CHAT = (
    "需要 LLM_PROVIDER=openai_compatible、LLM_BASE_URL、LLM_MODEL、LLM_API_KEY"
)
CLOUD_HINT_EMBEDDING = (
    "需要 EMBEDDING_PROVIDER=openai_compatible、EMBEDDING_BASE_URL、"
    "EMBEDDING_MODEL、EMBEDDING_API_KEY（维度用 EMBEDDING_DIMENSIONS 指定）"
)


def _env(name, default=None):
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value or default


def _env_flag(name):
    return "存在" if _env(name) else "缺失"


def cloud_chat_status():
    """云端对话后端状态（不含 Key 值）。"""

    provider = (_env("LLM_PROVIDER") or llm_client.PROVIDER_OPENAI).lower()

    if provider == llm_client.PROVIDER_OLLAMA:
        model = _env("OLLAMA_MODEL") or llm_client.DEFAULT_OLLAMA_MODEL
        base_url = _env("OLLAMA_URL") or llm_client.DEFAULT_OLLAMA_CHAT_URL
    else:
        model = _env("LLM_MODEL") or llm_client.DEFAULT_OPENAI_MODEL
        base_url = _env("LLM_BASE_URL") or llm_client.DEFAULT_OPENAI_BASE_URL

    status = {
        "kind": "chat",
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": _env_flag("LLM_API_KEY"),
        "ready": False,
        "reason": "",
    }

    if provider != llm_client.PROVIDER_OPENAI:
        status["reason"] = (
            f"provider={provider} 是本地后端（Ollama），不属于本次云端 API 验收"
        )

        return status

    try:
        settings = llm_client.get_settings()
    except llm_client.LLMConfigError as error:
        status["reason"] = str(error)

        return status

    status.update(
        provider=settings.provider,
        model=settings.model,
        base_url=settings.chat_url,
        api_key="存在" if getattr(settings, "api_key", None) else "缺失",
    )

    if not (getattr(settings, "api_key", None) and settings.chat_url):
        status["reason"] = "缺少 LLM_API_KEY 或 LLM_BASE_URL"

        return status

    if settings.provider != llm_client.PROVIDER_OPENAI:
        status["reason"] = f"provider={settings.provider} 不是 openai_compatible"

        return status

    status["ready"] = True

    return status


def cloud_embedding_status():
    """云端 Embedding 后端状态（不含 Key 值）。"""

    provider = (
        _env("EMBEDDING_PROVIDER") or embedding_service.PROVIDER_OPENAI
    ).lower()

    if provider == embedding_service.PROVIDER_OLLAMA:
        model = (
            _env("OLLAMA_EMBED_MODEL") or embedding_service.DEFAULT_MODELS[
                embedding_service.PROVIDER_OLLAMA
            ]
        )
        base_url = _env("OLLAMA_EMBED_URL") or "http://localhost:11434"
    else:
        model = (
            _env("EMBEDDING_MODEL")
            or embedding_service.DEFAULT_MODELS[embedding_service.PROVIDER_OPENAI]
        )
        base_url = (
            _env("EMBEDDING_BASE_URL") or embedding_service.DEFAULT_OPENAI_BASE_URL
        )

    status = {
        "kind": "embedding",
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": _env_flag("EMBEDDING_API_KEY"),
        "dimensions": _env("EMBEDDING_DIMENSIONS") or "default",
        "ready": False,
        "reason": "",
    }

    if provider != embedding_service.PROVIDER_OPENAI:
        status["reason"] = (
            f"provider={provider} 是本地后端（Ollama），不属于本次云端 API 验收"
        )

        return status

    try:
        settings = embedding_service.get_settings()
    except embedding_service.EmbeddingConfigError as error:
        status["reason"] = str(error)

        return status

    status.update(
        provider=settings.provider,
        model=settings.model,
        base_url=settings.base_url,
        api_key="存在" if getattr(settings, "api_key", None) else "缺失",
        dimensions=settings.dimensions if settings.dimensions else "default",
    )

    if not (getattr(settings, "api_key", None) and settings.base_url):
        status["reason"] = "缺少 EMBEDDING_API_KEY 或 EMBEDDING_BASE_URL"

        return status

    if settings.provider != embedding_service.PROVIDER_OPENAI:
        status["reason"] = f"provider={settings.provider} 不是 openai_compatible"

        return status

    status["ready"] = True

    return status


def cloud_chat_ready():
    """云端对话后端是否就绪（本地 Ollama 永远返回 False）。"""

    return cloud_chat_status()["ready"]


def cloud_embedding_ready():
    """云端 Embedding 后端是否就绪（本地 Ollama 永远返回 False）。"""

    return cloud_embedding_status()["ready"]


# 兼容旧调用点：真实 API 测试入口一律按云端口径判断。
chat_ready = cloud_chat_ready
embedding_ready = cloud_embedding_ready


def local_backend_status():
    """本地 Ollama 探测（纯诊断，不参与验收）。"""

    import requests

    def alive(base_url):
        url = f"{str(base_url).rstrip('/')}/api/tags"

        try:
            return requests.get(url, timeout=PROBE_TIMEOUT_SECONDS).status_code == 200
        except requests.RequestException:
            return False

    chat = cloud_chat_status()
    embedding = cloud_embedding_status()

    return {
        "chat_provider": chat["provider"],
        "chat_ollama_alive": alive("http://localhost:11434"),
        "embedding_provider": embedding["provider"],
        "embedding_ollama_alive": alive("http://localhost:11434"),
        "note": "本地探测仅用于诊断，不能作为真实 API 验收依据",
    }


def describe():
    """给测试输出用的一行说明（含 provider/model/base_url，不含 Key）。"""

    chat = cloud_chat_status()
    embedding = cloud_embedding_status()

    return (
        f"chat: provider={chat['provider']} model={chat['model']} "
        f"url={chat['base_url']} key={chat['api_key']} ready={chat['ready']}; "
        f"embedding: provider={embedding['provider']} model={embedding['model']} "
        f"url={embedding['base_url']} dim={embedding['dimensions']} "
        f"key={embedding['api_key']} ready={embedding['ready']}"
    )


def _print_status(kind, status):
    print(
        f"INFO: {kind} provider={status['provider']} model={status['model']} "
        f"base_url={status['base_url']} api_key={status['api_key']}"
    )


def _skip(test_name, statuses, hints):
    for kind, status in statuses:
        _print_status(kind, status)

    reasons = "；".join(
        f"{kind}：{status['reason']}" for kind, status in statuses if status["reason"]
    )

    print(f"\n[SKIP] {test_name} 未执行：{reasons}")
    print(f"       所需配置：{'；'.join(hints)}")
    print("       这不是通过：本地 Ollama 不算真实 API，配置好云端后端后必须重跑。")

    raise SystemExit(0)


def require_cloud_chat(test_name):
    """云端对话后端不就绪时打印 SKIP 并以退出码 0 结束测试。"""

    status = cloud_chat_status()

    if status["ready"]:
        _print_status("云端对话后端", status)

        return status

    _skip(test_name, [("对话", status)], [CLOUD_HINT_CHAT])


def require_cloud_embedding(test_name):
    """云端 Embedding 后端不就绪时打印 SKIP 并以退出码 0 结束测试。"""

    status = cloud_embedding_status()

    if status["ready"]:
        _print_status("云端 Embedding 后端", status)

        return status

    _skip(test_name, [("Embedding", status)], [CLOUD_HINT_EMBEDDING])


def require_cloud(test_name):
    """对话与 Embedding 都必须是云端就绪，否则 SKIP。"""

    chat = cloud_chat_status()
    embedding = cloud_embedding_status()

    if chat["ready"] and embedding["ready"]:
        _print_status("云端对话后端", chat)
        _print_status("云端 Embedding 后端", embedding)

        return {"chat": chat, "embedding": embedding}

    _skip(
        test_name,
        [("对话", chat), ("Embedding", embedding)],
        [CLOUD_HINT_CHAT, CLOUD_HINT_EMBEDDING],
    )


# 兼容旧调用点：`require_chat` 现在等同于「云端对话」检查。
require_chat = require_cloud_chat
