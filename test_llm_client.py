"""llm_client 测试：直接运行 python test_llm_client.py。

覆盖（全部 Mock，不发起任何真实网络/付费请求）：
- 默认 Ollama 路径兼容（OLLAMA_URL / OLLAMA_MODEL 旧配置）
- OpenAI 兼容 API：普通文本 / 单工具调用 / 多工具调用 / 流式内容与参数累积
- 工具调用 ID 对应关系（assistant tool_calls <-> tool 结果消息）
- 无效 JSON 参数
- 缺少 API Key / 缺少 Base URL / 非法 Provider（清晰报错、不静默切换）
- 连接失败 / 超时 / HTTP 错误 / deadline 中途超时
- Token usage 存在与缺失（缺失时 available=False，不估算）
- 用量记录钩子：调用、字段、异常不影响请求
- 安全：任何错误信息都不包含 API Key
- 真实 requests.post 被替换为“炸弹”，确保测试不会发出真实请求
"""

import contextlib
import json
import os
import time

import httpx2
import openai
import requests

import agent_loop
import ai_service
import llm_client


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def info(name, detail):
    print(f"INFO: {name} {detail}")


ENV_KEYS = [
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "OLLAMA_URL", "OLLAMA_MODEL",
]

SECRET_KEY = "sk-test-secret-key-123456"


@contextlib.contextmanager
def clean_env(**values):
    """沙箱环境变量：先清空全部模型配置变量，再应用指定值，退出后恢复。"""

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

        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value

        llm_client.reset_settings()


class FakeResponse:
    """模拟流式 HTTP 响应（NDJSON / SSE 行迭代）。"""

    def __init__(self, lines, status_code=200):
        self.lines = list(lines)
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"status={self.status_code}", response=self
            )

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


class Obj:
    """属性访问的小对象（模拟 openai SDK 的 chunk / delta 结构）。"""

    def __init__(self, **values):
        self.__dict__.update(values)


def tool_delta(index, identifier=None, name=None, arguments=None):
    return Obj(index=index, id=identifier, function=Obj(name=name, arguments=arguments))


def delta_chunk(content=None, tool_calls=None):
    return Obj(
        choices=[Obj(index=0, delta=Obj(content=content, tool_calls=tool_calls))],
        usage=None,
    )


def usage_chunk(**counts):
    return Obj(choices=[], usage=Obj(**counts))


class FakeStream:
    """模拟 openai SDK 的流式迭代；可编程在第 N 个分块后抛异常。"""

    def __init__(self, chunks, raise_after=None):
        self.chunks = list(chunks)
        self.raise_after = raise_after
        self.closed = False

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.raise_after is not None and index >= self.raise_after:
                raise self.raise_after

            yield chunk

    def close(self):
        self.closed = True


class FakeOpenAIClient:
    """模拟 openai SDK 客户端：chat.completions.create(**payload) -> FakeStream 或抛 SDK 异常。"""

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
    """替换 llm_client 的 openai 客户端工厂；handler(payload) -> FakeStream 或 raise。"""

    box = {}
    original = llm_client._make_openai_client

    def factory(settings, read_timeout):
        client = FakeOpenAIClient(handler)
        client.settings = settings
        client.read_timeout = read_timeout
        box["client"] = client

        return client

    llm_client._make_openai_client = factory

    try:
        yield box
    finally:
        llm_client._make_openai_client = original


def make_timeout_error():
    return openai.APITimeoutError(request=httpx2.Request("POST", "http://test"))


def make_conn_error():
    return openai.APIConnectionError(request=httpx2.Request("POST", "http://test"))


def make_status_error(status):
    request = httpx2.Request("POST", "http://test")

    return openai.APIStatusError(
        f"status={status}", response=httpx2.Response(status, request=request), body=None
    )


def make_bad_request_error():
    request = httpx2.Request("POST", "http://test")

    return openai.BadRequestError(
        "bad request", response=httpx2.Response(400, request=request), body=None
    )


@contextlib.contextmanager
def fake_http(handler):
    """替换 llm_client 的 HTTP 边界；handler(url, headers, payload, stream, timeout)。"""

    calls = []
    original = llm_client._http_post

    def recording(url, headers, payload, stream, timeout):
        calls.append({
            "url": url, "headers": headers, "payload": payload,
            "stream": stream, "timeout": timeout,
        })

        return handler(url, headers, payload, stream, timeout)

    llm_client._http_post = recording

    try:
        yield calls
    finally:
        llm_client._http_post = original


def real_network_bomb(*args, **kwargs):
    raise AssertionError("测试不允许发起真实网络请求")


class FakeDeadline:
    """可编程 deadline：第 N 次检查后过期。"""

    def __init__(self, expire_after_checks=None):
        self.expire_after_checks = expire_after_checks
        self.checks = 0

    def expired(self):
        self.checks += 1

        if self.expire_after_checks is None:
            return False

        return self.checks > self.expire_after_checks

    def remaining(self):
        return 30.0 if not (self.expire_after_checks is not None and self.checks > self.expire_after_checks) else 0.0


def ollama_text_lines(*contents, usage=True):
    lines = []

    for content in contents:
        lines.append(json.dumps({
            "message": {"role": "assistant", "content": content},
            "done": False,
        }))

    final = {"message": {"role": "assistant", "content": ""}, "done": True}

    if usage:
        final["prompt_eval_count"] = 12
        final["eval_count"] = 34

    lines.append(json.dumps(final))

    return lines


ORIGINAL_REQUESTS_POST = llm_client.requests.post
llm_client.requests.post = real_network_bomb

try:
    # ==========================
    # 1. 配置层
    # ==========================

    with clean_env(LLM_API_KEY=SECRET_KEY):
        settings = llm_client.get_settings()
        check("默认 Provider 是云端 openai_compatible", settings.provider == "openai_compatible")
        check("默认模型回落 qwen-plus", settings.model == "qwen-plus", f"model={settings.model}")
        check("默认 chat 地址是百炼兼容端点",
              settings.chat_url.startswith("https://dashscope.aliyuncs.com/compatible-mode/v1"),
              f"url={settings.chat_url}")

    with clean_env(LLM_PROVIDER="ollama"):
        settings = llm_client.get_settings()
        check("显式 ollama 回退可用", settings.provider == "ollama")
        check("ollama 默认模型回落 qwen3.5:4b", settings.model == "qwen3.5:4b", f"model={settings.model}")
        check("ollama 默认 chat 地址是本地",
              settings.chat_url == "http://localhost:11434/api/chat", f"url={settings.chat_url}")

    with clean_env(LLM_PROVIDER="ollama", OLLAMA_URL="http://legacy-host:11434/api/chat",
                   OLLAMA_MODEL="legacy-model"):
        settings = llm_client.get_settings()
        check("OLLAMA_URL 兼容", settings.chat_url == "http://legacy-host:11434/api/chat")
        check("OLLAMA_MODEL 兼容", settings.model == "legacy-model")

    with clean_env(LLM_PROVIDER="ollama", OLLAMA_MODEL="legacy-model", LLM_MODEL="cloud-model"):
        settings = llm_client.get_settings()
        check("ollama 只认 OLLAMA_MODEL（LLM_MODEL 云端模型名不污染本地）",
              settings.model == "legacy-model", f"model={settings.model}")
        check("LLM_MODEL 只服务云端 Provider",
              llm_client.default_model("openai_compatible") == "cloud-model")

    with clean_env(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="https://api.example.com/v1",
                   LLM_API_KEY=SECRET_KEY, LLM_MODEL="gpt-test"):
        settings = llm_client.get_settings()
        check("openai_compatible chat 地址拼接 /chat/completions",
              settings.chat_url == "https://api.example.com/v1/chat/completions")
        check("API Key 进入 settings", settings.api_key == SECRET_KEY)

    with clean_env(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="https://api.example.com/v1/chat/completions",
                   LLM_API_KEY=SECRET_KEY):
        settings = llm_client.get_settings()
        check("Base URL 已含 /chat/completions 时不重复拼接",
              settings.chat_url == "https://api.example.com/v1/chat/completions")

    # 配置错误：清晰报错、不静默切换
    with clean_env(LLM_PROVIDER="openai_compatible", LLM_API_KEY=SECRET_KEY):
        settings = llm_client.get_settings()
        check("LLM_BASE_URL 缺省时默认百炼兼容端点",
              settings.chat_url == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
              f"url={settings.chat_url}")

    with clean_env(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="https://api.example.com/v1"):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("缺 LLM_API_KEY 报配置错误",
              result is None and error is not None and "LLM_API_KEY" in error, f"error={error}")

    with clean_env(LLM_PROVIDER="bedrock"):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("非法 Provider 报配置错误",
              result is None and error is not None and "LLM_PROVIDER" in error, f"error={error}")

    # ==========================
    # 2. Ollama 路径（默认本地模式）
    # ==========================

    def ollama_ok(url, headers, payload, stream, timeout):
        return FakeResponse(ollama_text_lines("你好", "，世界"))

    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_ok) as calls:
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])

        check("Ollama 文本回答解析", error is None and result["message"]["content"] == "你好，世界")
        check("Ollama 结果记录 provider/model", result["provider"] == "ollama" and result["model"] == "qwen3.5:4b")
        check("Ollama 请求带 think=False", calls[0]["payload"].get("think") is False)
        check("Ollama 请求 stream=True", calls[0]["payload"].get("stream") is True)

        usage = result["usage"]
        check("Ollama usage 存在时 available=True",
              usage["available"] is True and usage["prompt_tokens"] == 12
              and usage["completion_tokens"] == 34 and usage["total_tokens"] == 46)

    def ollama_no_usage(url, headers, payload, stream, timeout):
        return FakeResponse(ollama_text_lines("回答", usage=False))

    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_no_usage):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        usage = result["usage"]
        check("Ollama usage 缺失时 available=False 且字段为 None",
              usage["available"] is False and usage["prompt_tokens"] is None
              and usage["completion_tokens"] is None and usage["total_tokens"] is None)

    def ollama_single_tool(url, headers, payload, stream, timeout):
        lines = [
            json.dumps({
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_a",
                        "function": {"name": "search_knowledge", "arguments": {"query": "定积分"}},
                    }],
                },
                "done": False,
            }),
            json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
        ]

        return FakeResponse(lines)

    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_single_tool):
        result, error = llm_client.chat([{"role": "user", "content": "查一下"}], tools=[{"type": "function"}])
        calls = result["message"]["tool_calls"]

        check("Ollama 单工具调用解析",
              error is None and len(calls) == 1
              and calls[0]["id"] == "call_a"
              and calls[0]["function"]["name"] == "search_knowledge"
              and calls[0]["function"]["arguments"] == {"query": "定积分"})

    def ollama_multi_tool(url, headers, payload, stream, timeout):
        lines = [
            json.dumps({
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "get_recent_feedback", "arguments": {"days": 7}}},
                        # 同 id 的重复分块必须被去重
                        {"id": "call_1", "function": {"name": "get_recent_feedback", "arguments": {"days": 7}}},
                        {"id": "call_2", "function": {"name": "search_knowledge", "arguments": {"query": "英语"}}},
                    ],
                },
                "done": False,
            }),
            json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
        ]

        return FakeResponse(lines)

    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_multi_tool):
        result, error = llm_client.chat([{"role": "user", "content": "查"}], tools=[{"type": "function"}])
        calls = result["message"]["tool_calls"]

        check("Ollama 多工具调用按顺序保留",
              [c["function"]["name"] for c in calls] == ["get_recent_feedback", "search_knowledge"],
              f"names={[c['function']['name'] for c in calls]}")
        check("Ollama 同 id 分块去重", len(calls) == 2)

    def ollama_bad_args(url, headers, payload, stream, timeout):
        lines = [
            json.dumps({
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_x",
                        "function": {"name": "search_knowledge", "arguments": "{不是合法 JSON"},
                    }],
                },
                "done": False,
            }),
            json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
        ]

        return FakeResponse(lines)

    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_bad_args):
        result, error = llm_client.chat([{"role": "user", "content": "查"}], tools=[{"type": "function"}])
        calls = result["message"]["tool_calls"]

        check("Ollama 无效 JSON 参数不崩溃并归一化为空对象",
              error is None and calls[0]["function"]["arguments"] == {})

    # ==========================
    # 3. OpenAI 兼容路径
    # ==========================

    openai_env = {
        "LLM_PROVIDER": "openai_compatible",
        "LLM_BASE_URL": "https://api.example.com/v1",
        "LLM_API_KEY": SECRET_KEY,
        "LLM_MODEL": "gpt-test",
    }

    def openai_ok(payload):
        return FakeStream([delta_chunk("你好"), delta_chunk("，世界"), delta_chunk(None)])

    with clean_env(**openai_env), fake_openai(openai_ok) as box:
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        calls = box["client"].calls

        check("OpenAI 文本回答拼接", error is None and result["message"]["content"] == "你好，世界")
        check("OpenAI 客户端使用配置的 API Key", box["client"].settings.api_key == SECRET_KEY)
        check("OpenAI 请求带 stream_options 以获取 usage",
              calls[0].get("stream_options") == {"include_usage": True})
        check("OpenAI 请求为流式且透传消息", calls[0].get("stream") is True
              and calls[0]["messages"] == [{"role": "user", "content": "hi"}])
        usage = result["usage"]
        check("OpenAI usage 缺失时 available=False", usage["available"] is False and usage["prompt_tokens"] is None)

    def openai_usage_ok(payload):
        return FakeStream([
            delta_chunk("ok"),
            usage_chunk(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ])

    with clean_env(**openai_env), fake_openai(openai_usage_ok):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        usage = result["usage"]
        check("OpenAI usage 存在时记录真实数值",
              usage["available"] is True and usage["prompt_tokens"] == 10
              and usage["completion_tokens"] == 5 and usage["total_tokens"] == 15)

    def openai_single_tool(payload):
        return FakeStream([
            delta_chunk(None, tool_calls=[
                tool_delta(0, "call_o1", "search_knowledge", "{\"query\": \"定积分\"}"),
            ]),
            delta_chunk(None),
        ])

    with clean_env(**openai_env), fake_openai(openai_single_tool):
        result, error = llm_client.chat([{"role": "user", "content": "查"}], tools=[{"type": "function"}])
        calls = result["message"]["tool_calls"]

        check("OpenAI 单工具调用解析（arguments JSON 字符串转对象）",
              error is None and len(calls) == 1
              and calls[0]["id"] == "call_o1"
              and calls[0]["function"]["name"] == "search_knowledge"
              and calls[0]["function"]["arguments"] == {"query": "定积分"})

    def openai_stream_accumulate(payload):
        """流式：内容分块 + 工具 name/arguments/id 分块到达 + 双工具。"""

        return FakeStream([
            delta_chunk("前"),
            delta_chunk(None, tool_calls=[
                tool_delta(0, "call_s1", "get_recent_", "{\"da"),
            ]),
            delta_chunk(None, tool_calls=[
                tool_delta(0, None, "feedback", "ys\": 7}"),
                tool_delta(1, "call_s2", "search_knowledge", "{\"query\": \"英语\"}"),
            ]),
            delta_chunk("后"),
            delta_chunk(None),
        ])

    with clean_env(**openai_env), fake_openai(openai_stream_accumulate):
        result, error = llm_client.chat([{"role": "user", "content": "查"}], tools=[{"type": "function"}])
        message = result["message"]
        calls = message["tool_calls"]

        check("流式内容分块拼接", message["content"] == "前后", f"content={message['content']}")
        check("流式工具参数跨分块累积",
              calls[0]["function"]["arguments"] == {"days": 7},
              f"args={calls[0]['function']['arguments']}")
        check("流式工具 name 跨分块累积",
              calls[0]["function"]["name"] == "get_recent_feedback")
        check("多工具按 index 顺序保留",
              [c["function"]["name"] for c in calls] == ["get_recent_feedback", "search_knowledge"],
              f"names={[c['function']['name'] for c in calls]}")
        check("工具调用 ID 保留", calls[0]["id"] == "call_s1" and calls[1]["id"] == "call_s2")

    def openai_bad_args(payload):
        return FakeStream([
            delta_chunk(None, tool_calls=[
                tool_delta(0, "call_bad", "search_knowledge", "not-json{{"),
            ]),
            delta_chunk(None),
        ])

    with clean_env(**openai_env), fake_openai(openai_bad_args):
        result, error = llm_client.chat([{"role": "user", "content": "查"}], tools=[{"type": "function"}])
        calls = result["message"]["tool_calls"]

        check("OpenAI 无效 JSON 参数归一化为空对象",
              error is None and calls[0]["function"]["arguments"] == {})

    # 不认识 stream_options 的实现报 400 -> 去掉该参数后重试一次
    retry_state = {"calls": 0}

    def openai_stream_options_retry(payload):
        retry_state["calls"] += 1

        if retry_state["calls"] == 1:
            raise make_bad_request_error()

        return FakeStream([delta_chunk("ok")])

    with clean_env(**openai_env), fake_openai(openai_stream_options_retry) as box:
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        calls = box["client"].calls

        check("不认识 stream_options 的实现自动重试一次",
              error is None and len(calls) == 2 and result["message"]["content"] == "ok")
        check("重试请求不再带 stream_options", "stream_options" not in calls[1])

    # 客户端 base_url 拼接正确（含 /chat/completions 后缀自动剥离）
    with clean_env(LLM_PROVIDER="openai_compatible",
                   LLM_BASE_URL="https://api.example.com/v1/chat/completions",
                   LLM_API_KEY=SECRET_KEY, LLM_MODEL="gpt-test"):
        settings = llm_client.get_settings()
        real_client = llm_client._make_openai_client(settings, 30)

        check("SDK base_url 剥离 /chat/completions 后缀",
              str(real_client.base_url).rstrip("/") == "https://api.example.com/v1",
              f"base_url={real_client.base_url}")

    # ==========================
    # 4. 工具调用 ID 对应关系（历史消息 -> Provider 格式）
    # ==========================

    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "查"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_h1", "function": {"name": "get_recent_feedback", "arguments": {"days": 7}}},
                        {"id": "call_h2", "function": {"name": "search_knowledge", "arguments": {"query": "英语"}}}]},
        {"role": "tool", "content": "{\"a\": 1}"},
        {"role": "tool", "content": "{\"b\": 2}"},
        {"role": "assistant", "content": "最终回答"},
    ]

    converted = llm_client._to_openai_messages(history)
    tool_messages = [m for m in converted if m["role"] == "tool"]

    check("tool 消息按顺序关联 assistant 工具 ID",
          [m["tool_call_id"] for m in tool_messages] == ["call_h1", "call_h2"])
    assistant_with_calls = [m for m in converted if m["role"] == "assistant" and m.get("tool_calls")][0]
    check("assistant 工具调用转为 OpenAI 格式",
          assistant_with_calls["tool_calls"][0]["type"] == "function"
          and assistant_with_calls["tool_calls"][0]["function"]["name"] == "get_recent_feedback"
          and json.loads(assistant_with_calls["tool_calls"][0]["function"]["arguments"]) == {"days": 7})

    # 历史里 assistant 没有 id / tool 消息自带 id 的兼容
    odd_history = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "search_knowledge", "arguments": {"query": "x"}}}]},
        {"role": "tool", "content": "r1"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_z", "function": {"name": "search_knowledge", "arguments": {"query": "y"}}}]},
        {"role": "tool", "tool_call_id": "call_z", "content": "r2"},
    ]
    converted_odd = llm_client._to_openai_messages(odd_history)
    odd_tools = [m for m in converted_odd if m["role"] == "tool"]

    check("assistant 缺 id 时补发合成 id 并配对",
          odd_tools[0]["tool_call_id"] == "call_1" and odd_tools[0]["tool_call_id"]
          == converted_odd[0]["tool_calls"][0]["id"])
    check("tool 消息自带 tool_call_id 时原样保留", odd_tools[1]["tool_call_id"] == "call_z")

    # ==========================
    # 5. 超时 / 连接 / HTTP 错误
    # ==========================

    def timeout_handler(url, headers, payload, stream, timeout):
        raise requests.exceptions.Timeout("模拟超时")

    with clean_env(LLM_PROVIDER="ollama"), fake_http(timeout_handler):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("连接超时返回稳定错误", result is None and error == "Ollama 请求超时", f"error={error}")

    with clean_env(**openai_env), fake_openai(lambda payload: (_ for _ in ()).throw(make_timeout_error())):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("OpenAI 超时返回稳定错误", result is None and "超时" in error, f"error={error}")

    def conn_handler(url, headers, payload, stream, timeout):
        raise requests.exceptions.ConnectionError("模拟连接失败")

    with clean_env(LLM_PROVIDER="ollama"), fake_http(conn_handler):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("连接失败返回稳定错误（有限重试后仍失败）",
              result is None and "ConnectionError" in error and "尝试" in error, f"error={error}")

    def http500_handler(url, headers, payload, stream, timeout):
        return FakeResponse([], status_code=500)

    with clean_env(LLM_PROVIDER="ollama"), fake_http(http500_handler):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("Ollama HTTP 500 返回稳定错误（分类为服务端错误）",
              result is None and "500" in error, f"error={error}")

    with clean_env(**openai_env), fake_openai(lambda payload: (_ for _ in ()).throw(make_status_error(500))):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])
        check("OpenAI HTTP 500 返回稳定错误（分类为服务端错误）",
              result is None and "500" in error and "尝试" in error, f"error={error}")

    # deadline：调用前已过期 / 流式中途过期
    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_ok):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}], deadline=FakeDeadline(0))
        check("deadline 调用前过期直接返回 agent_timeout", result is None and error == "agent_timeout")

    mid_stream_deadline = FakeDeadline(expire_after_checks=1)
    stream_response_box = {}

    def streaming_handler(url, headers, payload, stream, timeout):
        response = FakeResponse(ollama_text_lines("a", "b", "c", "d", "e", "f"))
        stream_response_box["response"] = response

        return response

    with clean_env(LLM_PROVIDER="ollama"), fake_http(streaming_handler):
        result, error = llm_client.chat(
            [{"role": "user", "content": "hi"}], deadline=mid_stream_deadline
        )
        check("流式中途超过 deadline 返回 agent_timeout", result is None and error == "agent_timeout")
        check("超时后连接被关闭", stream_response_box["response"].closed is True)

    # read timeout 不超过 deadline 剩余预算
    class CountdownDeadline:
        def __init__(self, remaining):
            self.remaining_value = remaining

        def expired(self):
            return self.remaining_value <= 0

        def remaining(self):
            return self.remaining_value

    with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_ok) as calls:
        llm_client.chat([{"role": "user", "content": "hi"}], deadline=CountdownDeadline(5))
        check("单次 read timeout 不超过 deadline 剩余时间",
              calls[0]["timeout"][1] <= 5, f"timeout={calls[0]['timeout']}")

    # ==========================
    # 6. 用量记录钩子
    # ==========================

    recorded = []
    llm_client.set_usage_recorder(recorded.append)

    try:
        with clean_env(**openai_env), fake_openai(openai_usage_ok):
            result, error = llm_client.chat([{"role": "user", "content": "hi"}])

        check("用量记录钩子被调用", len(recorded) == 1)
        record = recorded[0]
        check("用量记录字段完整",
              record["provider"] == "openai_compatible" and record["model"] == "gpt-test"
              and record["prompt_tokens"] == 10 and record["completion_tokens"] == 5
              and record["total_tokens"] == 15 and record["usage_available"] is True
              and record["ok"] is True and record["elapsed_seconds"] >= 0)

        with clean_env(LLM_PROVIDER="ollama"), fake_http(timeout_handler):
            llm_client.chat([{"role": "user", "content": "hi"}])

        check("失败请求也记录用量（ok=False）",
              len(recorded) == 2 and recorded[1]["ok"] is False and recorded[1]["error_type"] == "timeout"
              and recorded[1]["usage_available"] is False)

        def broken_recorder(record):
            raise RuntimeError("记录器坏了")

        llm_client.set_usage_recorder(broken_recorder)

        with clean_env(LLM_PROVIDER="ollama"), fake_http(ollama_ok):
            result, error = llm_client.chat([{"role": "user", "content": "hi"}])

        check("用量记录失败不影响模型请求", error is None and result["message"]["content"] == "你好，世界")
    finally:
        llm_client.set_usage_recorder(None)

    # ==========================
    # 7. 安全：错误信息不泄漏 API Key
    # ==========================

    with clean_env(**openai_env), fake_openai(lambda payload: (_ for _ in ()).throw(make_conn_error())):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])

        check("连接错误信息不包含 API Key", error is not None and SECRET_KEY not in error)

    with clean_env(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="https://api.example.com/v1"):
        result, error = llm_client.chat([{"role": "user", "content": "hi"}])

        check("配置错误信息不包含任何密钥内容", error is not None and "sk-" not in error)

    # ==========================
    # 8. 上层接线兼容（agent_loop / ai_service 接口契约不变）
    # ==========================

    captured = {}

    def fake_llm_chat(messages, tools=None, deadline=None, read_timeout=None):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["deadline"] = deadline
        captured["read_timeout"] = read_timeout

        return {
            "message": {"role": "assistant", "content": "回答"},
            "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                      "available": False},
            "provider": "ollama",
            "model": "qwen3.5:4b",
            "elapsed_seconds": 0.01,
        }, None

    original_chat = llm_client.chat
    llm_client.chat = fake_llm_chat

    try:
        message, error = agent_loop.chat_with_tools(
            [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]
        )
        check("agent_loop.chat_with_tools 契约不变（返回 assistant 消息）",
              error is None and message == {"role": "assistant", "content": "回答"})
        check("tools 参数透传给统一层", captured["tools"] == [{"type": "function"}])

        content, error = ai_service.call_ollama("你好")
        check("ai_service.call_ollama 契约不变（返回 (content, error)）",
              error is None and content == "回答")
        check("call_ollama 使用 60 秒 read timeout", captured["read_timeout"] == 60)

        def fake_llm_timeout(messages, tools=None, deadline=None, read_timeout=None):
            return None, llm_client.TIMEOUT_ERROR

        llm_client.chat = fake_llm_timeout
        message, error = agent_loop.chat_with_tools([{"role": "user", "content": "hi"}])
        check("超时错误与 AGENT_TIMEOUT 标识一致", message is None and error == agent_loop.AGENT_TIMEOUT)
    finally:
        llm_client.chat = original_chat

    check("测试全程未发起真实网络请求（requests.post 为炸弹）", True)

finally:
    llm_client.requests.post = ORIGINAL_REQUESTS_POST

print("ALL TESTS COMPLETED")
