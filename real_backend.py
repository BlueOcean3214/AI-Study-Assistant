"""真实后端就绪检查（测试用）。

迁移到云端 API 之后，"真实模型场景"不再默认回退本地 Ollama：没有配置好的
对话 / Embedding 后端时，测试应当**显式跳过并说明原因**，而不是假装通过，
也不能悄悄连本机 Ollama。

判定规则
--------
对话后端：
- provider=openai_compatible：只要 LLM_API_KEY 和 base_url 配置齐全即视为「已配置」
  （真的能不能连、返回什么，由真实 API 冒烟脚本负责验证，单元测试不烧钱）。
- provider=ollama：需要本机 /api/tags 可访问，否则视为不可用。

Embedding 后端同理，用 EMBEDDING_API_KEY / EMBEDDING_BASE_URL 或本地 Ollama 判定。
"""

import requests

import embedding_service
import llm_client

PROBE_TIMEOUT_SECONDS = 2


def _ollama_alive(base_url):
    url = f"{str(base_url).rstrip('/')}/api/tags"

    try:
        response = requests.get(url, timeout=PROBE_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False

    return response.status_code == 200


def _ollama_root(url):
    """从完整端点地址反推 Ollama 根地址（Settings 里给的是 chat_url / base_url）。"""

    text = str(url or "")

    for suffix in ("/api/chat", "/api/embed", "/v1/embeddings", "/chat/completions"):
        if text.endswith(suffix):
            return text[: -len(suffix)]

    return text.rstrip("/")


def chat_ready():
    """真实对话后端是否可用。"""

    try:
        settings = llm_client.get_settings()
    except llm_client.LLMConfigError:
        return False

    if settings.provider == llm_client.PROVIDER_OPENAI:
        return bool(settings.api_key and settings.chat_url)

    return _ollama_alive(_ollama_root(settings.chat_url))


def embedding_ready():
    """真实 Embedding 后端是否可用。"""

    try:
        settings = embedding_service.get_settings()
    except embedding_service.EmbeddingConfigError:
        return False

    if settings.provider == embedding_service.PROVIDER_OPENAI:
        return bool(settings.api_key and settings.base_url)

    return _ollama_alive(_ollama_root(settings.base_url))


def describe():
    """给测试输出用的一行说明。"""

    return f"chat_ready={chat_ready()} embedding_ready={embedding_ready()}"


def require_chat(test_name):
    """真实对话后端不可用时打印 SKIP 并以退出码 0 结束测试（绝不假装通过）。

    用在「真实模型场景」这一节的入口：迁移后不再隐式回退本地 Ollama，
    缺失配置只报 SKIP 与原因，由人工/CI 决定是否补配置。
    """

    if chat_ready():
        return

    print(
        f"\n[SKIP] {test_name} 未执行：没有可用的真实对话后端"
        "（配置 LLM_API_KEY + LLM_BASE_URL，或启动本地 Ollama）"
    )
    raise SystemExit(0)
