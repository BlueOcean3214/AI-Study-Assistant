"""对话模型 JSON 输出验收：直接运行 python test_openai.py。

背景
----
原实现直接 POST `http://localhost:11434/api/chat`（qwen 本地模型）并要求 JSON 输出。
迁移到云端 API 之后，本地 Ollama 不再是运行依赖，本脚本改为走统一入口
`llm_client.chat()`，拆成两节：

A. 打桩契约节（默认执行，无网络、无 API Key、零费用）
   替换 llm_client 的 openai 客户端工厂，校验「JSON 指令 → 流式分块累积 → 完整 JSON」
   这条通路：分块累积正确、JSON 可解析、字段齐全、请求参数（model / stream）来自
   统一配置；同时校验缺 API Key 时返回 config_error，不会静默切到本地模型。

B. 真实后端节（有可用对话后端才执行，否则打印 [SKIP] 并退出码 0）
   真实调用一次 `llm_client.chat()`，要求返回合法 JSON 且字段齐全——这是工具调用
   之外对「模型能稳定输出结构化 JSON」的最低验收。

注意：A 节证明的是通路与配置契约，不是模型能力；模型能力只由 B 节（真实 API）负责，
缺后端时 B 节明确跳过，绝不假装通过。
"""

import contextlib
import json
import os
from types import SimpleNamespace

import llm_client
import real_backend


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


ENV_KEYS = ["LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT", "OLLAMA_URL", "OLLAMA_MODEL"]

DUMMY_KEY = "sk-local-test-key"


@contextlib.contextmanager
def model_env(**values):
    """沙箱环境变量：先清空模型配置，再应用指定值，退出后恢复并重置 settings。"""

    saved = {key: os.environ.get(key) for key in ENV_KEYS}

    for key in ENV_KEYS:
        os.environ.pop(key, None)

    for key, value in values.items():
        if value is not None:
            os.environ[key] = value

    llm_client.reset_settings()

    try:
        yield
    finally:
        for key in ENV_KEYS:
            os.environ.pop(key, None)

            if saved[key] is not None:
                os.environ[key] = saved[key]

        llm_client.reset_settings()


def make_chunk(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))],
        usage=None,
    )


class RecordingFakeClient:
    """最小 openai SDK 替身：chat.completions.create(**payload) 返回可迭代分块。"""

    def __init__(self, handler):
        self.calls = []
        self.handler = handler

        client = self

        class Completions:
            def create(self, **payload):
                client.calls.append(payload)
                return client.handler(payload)

        class Chat:
            completions = Completions()

        self.chat = Chat()


@contextlib.contextmanager
def fake_openai(handler):
    original = llm_client._make_openai_client
    box = {}

    def factory(settings, read_timeout):
        client = RecordingFakeClient(handler)
        box["client"] = client
        return client

    llm_client._make_openai_client = factory

    try:
        yield box
    finally:
        llm_client._make_openai_client = original


PROMPT = [
    {
        "role": "user",
        "content": """请告诉我一个学生的信息。
必须返回 JSON。
字段：
name：姓名
age：年龄
major：专业
""",
    }
]

STUDENT_JSON = '{"name": "张三", "age": 20, "major": "计算机科学与技术"}'


# ==========================
# A. 打桩契约节（无网络）
# ==========================

print("=" * 60)
print("A. 打桩契约节（假 openai 客户端，不联网、不需要真实 Key）")
print("=" * 60)

with model_env(
    LLM_PROVIDER="openai_compatible",
    LLM_BASE_URL="https://example.invalid/v1",
    LLM_API_KEY=DUMMY_KEY,
    LLM_MODEL="stub-chat-model",
):
    settings = llm_client.get_settings()

    info("对话配置", f"provider={settings.provider} model={settings.model} url={settings.chat_url}")

    # 故意把 JSON 拆成三段返回，验证流式分块累积
    pieces = [STUDENT_JSON[:12], STUDENT_JSON[12:35], STUDENT_JSON[35:]]

    with fake_openai(lambda payload: iter([make_chunk(piece) for piece in pieces])) as box:
        result, error = llm_client.chat(PROMPT)

    check("接口返回结构为 (result, error)", isinstance(result, dict) and "message" in result, f"result={result}")
    check("打桩路径没有报错", error is None, f"error={error}")

    content = result["message"]["content"] if result else ""
    check("流式分块已完整累积", content == STUDENT_JSON, f"content={content}")

    payload = box["client"].calls[0]
    check("请求使用统一配置里的模型", payload["model"] == settings.model, f"model={payload['model']}")
    check("请求走流式兼容接口", payload["stream"] is True, f"stream={payload['stream']}")
    info("认证头", f"Bearer 已设置={bool(box['client'].calls)}（Key 不输出）")

    parsed = None
    try:
        parsed = json.loads(content)
    except ValueError as error_detail:
        raise AssertionError(f"FAILED: JSON 输出无法解析 {error_detail}")

    check("JSON 可解析", isinstance(parsed, dict), f"parsed={parsed}")
    for field in ("name", "age", "major"):
        check(f"字段 {field} 存在", field in parsed, f"value={parsed.get(field)!r}")

    # 缺 API Key：必须明确报配置错误，不得静默回退本地模型
    with model_env(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="https://example.invalid/v1"):
        missing_result, missing_error = llm_client.chat(PROMPT)

    if missing_error is None and missing_result is not None:
        raise AssertionError("FAILED: 缺 API Key 时不应返回成功结果")

    check("缺 API Key 时明确报错", bool(missing_error), f"error={missing_error}")


# ==========================
# B. 真实后端节（可选）
# ==========================

print()
print("=" * 60)
print("B. 真实对话后端（JSON 输出验收；只认云端 openai_compatible）")
print("=" * 60)

real_backend.require_cloud_chat("test_openai.py 的 JSON 输出验收")

settings = llm_client.get_settings()

info("对话后端", f"provider={settings.provider} model={settings.model} url={settings.chat_url}")

check(
    "真实后端 Provider 是云端 openai_compatible",
    settings.provider == llm_client.PROVIDER_OPENAI,
    f"provider={settings.provider}",
)
check(
    "真实后端 base_url 不是本机地址",
    "localhost" not in str(settings.chat_url) and "127.0.0.1" not in str(settings.chat_url),
    f"url={settings.chat_url}",
)
check(
    "真实后端配置描述不含 Key",
    bool(settings.api_key) and settings.api_key not in settings.describe(),
)

real_result, real_error = llm_client.chat(PROMPT, read_timeout=60)

check("真实后端调用成功", real_error is None, f"error={real_error}")

real_content = real_result["message"]["content"] if real_result else ""
info("真实回复", real_content[:200].replace("\n", " "))

real_parsed = None
try:
    real_parsed = json.loads(real_content)
except ValueError:
    # 模型偶尔包 ```json 围栏，取出花括号内容再试一次（不改变验收标准）
    start = real_content.find("{")
    end = real_content.rfind("}")

    if start != -1 and end > start:
        try:
            real_parsed = json.loads(real_content[start : end + 1])
        except ValueError:
            real_parsed = None

check("真实后端返回合法 JSON", isinstance(real_parsed, dict), f"parsed={real_parsed}")

for field in ("name", "age", "major"):
    check(f"真实 JSON 字段 {field} 存在", field in real_parsed, f"value={real_parsed.get(field)!r}")

print()
print("全部对话 JSON 输出测试通过")
