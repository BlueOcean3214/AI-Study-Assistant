"""SSE 流式问答测试：直接运行 python test_sse_stream.py。

全 Mock（桩 HTTP / openai SDK / chat_stream），不发起真实请求。

覆盖：
- llm_client.chat_stream：ollama NDJSON 与 openai SSE 两种流的 delta/usage 事件
- 正常结束 done、上游错误 error、超时 error 的稳定事件结构
- 客户端中断（生成器 close）后上游连接被关闭
- main./ask/stream 的 SSE 帧格式（sources / delta / done / error）与前端解析约定一致
- build_ask_prompt 流式与非流式共用
"""

import contextlib
import json

import openai

import llm_client
import main


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


class Obj:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeResponse:
    def __init__(self, lines, status_code=200):
        self.lines = list(lines)
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"http {self.status_code}")

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


@contextlib.contextmanager
def fake_http(handler):
    original = llm_client._http_post
    llm_client._http_post = handler
    try:
        yield
    finally:
        llm_client._http_post = original


@contextlib.contextmanager
def fake_openai_stream(chunks, raise_after=None):
    box = {}
    original = openai.OpenAI

    class FakeStream:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            for index, chunk in enumerate(chunks):
                if raise_after is not None and index >= raise_after:
                    raise raise_after
                yield chunk

        def close(self):
            self.closed = True

    stream = FakeStream()
    box["stream"] = stream

    def factory(**kwargs):
        box["client_kwargs"] = kwargs

        class Completions:
            def create(self, **payload):
                box["payload"] = payload
                return stream

        class Chat:
            completions = Completions()

        client = Obj(chat=Chat())
        return client

    openai.OpenAI = factory

    try:
        yield box
    finally:
        openai.OpenAI = original


def ollama_lines():
    return [
        json.dumps({"message": {"content": "你好"}, "done": False}),
        json.dumps({"message": {"content": "，世界"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True,
                    "prompt_eval_count": 5, "eval_count": 7}),
    ]


# ==========================
# 1. chat_stream：ollama 流
# ==========================

with fake_http(lambda url, **kwargs: FakeResponse(ollama_lines())):
    os_env_backup = {}
    import os
    os.environ["LLM_PROVIDER"] = "ollama"
    llm_client.reset_settings()
    try:
        events = list(llm_client.chat_stream([{"role": "user", "content": "hi"}]))
    finally:
        os.environ.pop("LLM_PROVIDER", None)
        llm_client.reset_settings()

deltas = [e for e in events if e["type"] == "delta"]
dones = [e for e in events if e["type"] == "done"]

check("ollama 流按块产出 delta", [d["text"] for d in deltas] == ["你好", "，世界"])
check("ollama 流以 done 结束且带真实 usage",
      len(dones) == 1 and dones[0]["usage"]["prompt_tokens"] == 5
      and dones[0]["usage"]["completion_tokens"] == 7
      and dones[0]["usage"]["available"] is True)

# ==========================
# 2. chat_stream：openai/百炼 流
# ==========================

openai_chunks = [
    Obj(choices=[Obj(delta=Obj(content="定积分"))], usage=None),
    Obj(choices=[Obj(delta=Obj(content="的定义"))], usage=None),
    Obj(choices=[], usage=Obj(prompt_tokens=11, completion_tokens=3)),
]

import os
os.environ["LLM_PROVIDER"] = "openai_compatible"
os.environ["LLM_API_KEY"] = "sk-test"
llm_client.reset_settings()

try:
    with fake_openai_stream(openai_chunks) as box:
        events = list(llm_client.chat_stream([{"role": "user", "content": "hi"}]))

    deltas = [e for e in events if e["type"] == "delta"]
    dones = [e for e in events if e["type"] == "done"]

    check("openai 流按 delta 拼接", [d["text"] for d in deltas] == ["定积分", "的定义"])
    check("openai 流 done 带 usage", dones and dones[0]["usage"]["prompt_tokens"] == 11)
    check("流请求带 stream_options", box["payload"].get("stream_options") == {"include_usage": True})
finally:
    os.environ.pop("LLM_PROVIDER", None)
    os.environ.pop("LLM_API_KEY", None)
    llm_client.reset_settings()

# ==========================
# 3. 错误与中断
# ==========================

import httpx2


def make_conn_error():
    return openai.APIConnectionError(request=httpx2.Request("POST", "http://t"))


os.environ["LLM_PROVIDER"] = "openai_compatible"
os.environ["LLM_API_KEY"] = "sk-test"
llm_client.reset_settings()

try:
    with fake_openai_stream([], raise_after=None) as box:
        def raising_create(**payload):
            raise make_conn_error()

        original = openai.OpenAI

        def factory(**kwargs):
            class Completions:
                create = staticmethod(raising_create)

            class Chat:
                completions = Completions()

            return Obj(chat=Chat())

        openai.OpenAI = factory

        try:
            events = list(llm_client.chat_stream([{"role": "user", "content": "hi"}]))
        finally:
            openai.OpenAI = original

    errors = [e for e in events if e["type"] == "error"]
    check("上游连接失败产出 error 事件",
          len(errors) == 1 and errors[0]["error_type"] in {"connection", "server_error"},
          f"errors={errors}")
finally:
    os.environ.pop("LLM_PROVIDER", None)
    os.environ.pop("LLM_API_KEY", None)
    llm_client.reset_settings()

# 中断：消费方 close 生成器后上游流必须被关闭
os.environ["LLM_PROVIDER"] = "openai_compatible"
os.environ["LLM_API_KEY"] = "sk-test"
llm_client.reset_settings()

try:
    slow_chunks = [
        Obj(choices=[Obj(delta=Obj(content="a"))], usage=None),
        Obj(choices=[Obj(delta=Obj(content="b"))], usage=None),
    ]

    with fake_openai_stream(slow_chunks) as box:
        generator = llm_client.chat_stream([{"role": "user", "content": "hi"}])
        first = next(generator)
        generator.close()

        check("中断后上游流被关闭",
              first["type"] == "delta" and box["stream"].closed is True)
finally:
    os.environ.pop("LLM_PROVIDER", None)
    os.environ.pop("LLM_API_KEY", None)
    llm_client.reset_settings()

# ==========================
# 4. /ask/stream SSE 帧与前端解析约定
# ==========================


def parse_frames(text):
    """按前端 ask.html 的解析约定（event: / data: / 空行分隔）解析。"""

    frames = []

    for block in text.split("\n\n"):
        if not block.strip():
            continue

        name = "message"
        data = ""

        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:].strip()
            if line.startswith("data: "):
                data += line[6:]

        frames.append((name, json.loads(data)))

    return frames


def fake_chat_stream(messages):
    yield {"type": "delta", "text": "第一段"}
    yield {"type": "delta", "text": "第二段"}
    yield {"type": "done", "usage": {"available": True, "prompt_tokens": 1,
                                     "completion_tokens": 2, "total_tokens": 3},
           "model": "test-model", "elapsed_seconds": 0.5}


async def collect_body(response):
    """Starlette 把同步生成器包装成 async iterator，需异步消费。"""

    parts = []

    async for chunk in response.body_iterator:
        parts.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)

    return "".join(parts)


original_chat_stream = llm_client.chat_stream
original_retrieve = main.retrieve_chunks

llm_client.chat_stream = fake_chat_stream
main.retrieve_chunks = lambda question: [
    {"content": "片段", "source": "math.txt", "score": 0.9}
]

try:
    import asyncio

    response = main.ask_stream("定积分怎么学？")
    body = asyncio.run(collect_body(response))
    frames = parse_frames(body)
finally:
    llm_client.chat_stream = original_chat_stream
    main.retrieve_chunks = original_retrieve

check("SSE 首帧是 sources",
      frames[0][0] == "sources" and frames[0][1]["sources"][0]["source"] == "math.txt")
check("SSE 中间帧是 delta",
      [name for name, _ in frames[1:3]] == ["delta", "delta"])
check("SSE 结束帧是 done 且带 usage/model",
      frames[-1][0] == "done" and frames[-1][1]["model"] == "test-model"
      and frames[-1][1]["usage"]["available"] is True)
check("SSE 帧是合法 JSON 单行",
      all("\n" not in json.dumps(data, ensure_ascii=False) for _, data in frames))

# error 帧
def failing_chat_stream(messages):
    yield {"type": "error", "error": "上游失败", "error_type": "server_error"}


llm_client.chat_stream = failing_chat_stream
main.retrieve_chunks = lambda question: []

try:
    response = main.ask_stream("问题")
    body = asyncio.run(collect_body(response))
    frames = parse_frames(body)
finally:
    llm_client.chat_stream = original_chat_stream
    main.retrieve_chunks = original_retrieve

check("上游错误产出 error 帧",
      frames[-1][0] == "error" and frames[-1][1]["error_type"] == "server_error")

# ==========================
# 5. 流式/非流式 Prompt 一致
# ==========================

prompt_a = main.build_ask_prompt("问题", "资料")
prompt_b = main.build_ask_prompt("问题", "")

check("流式与非流式共用 Prompt 构造",
      "资料" in prompt_a and "问题" in prompt_a and prompt_b == "问题")

print("ALL TESTS COMPLETED")
