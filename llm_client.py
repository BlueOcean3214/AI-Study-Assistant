"""统一的大模型调用层（Provider 适配层）。

支持两种 Provider：
- ollama（默认）：本地 Ollama /api/chat，兼容既有 OLLAMA_URL / OLLAMA_MODEL 环境变量
- openai_compatible：线上 OpenAI 兼容 API（POST {base_url}/chat/completions）

配置（环境变量）：
- LLM_PROVIDER      "ollama"（默认）或 "openai_compatible"
- LLM_BASE_URL      ollama 默认 http://localhost:11434；openai_compatible 必填
- LLM_API_KEY       openai_compatible 必填；ollama 忽略
- LLM_MODEL         默认回落 OLLAMA_MODEL，再回落 qwen3.5:4b
- OLLAMA_URL / OLLAMA_MODEL：旧配置，继续有效（OLLAMA_URL 为完整 /api/chat 地址）

设计要点：
- 对外只有一种内部标准消息格式（与 agent_loop 既有约定一致）：
    assistant: {"role": "assistant", "content": str,
                "tool_calls": [{"id": str, "function": {"name": str, "arguments": dict}}] | 省略}
    tool:      {"role": "tool", "content": str}（可选 "tool_call_id"）
  各 Provider 适配器负责在“内部标准格式 <-> Provider 线上格式”之间转换：
  arguments 对象/JSON 字符串、流式 tool_call 分块累积、多工具顺序、
  assistant 工具调用与 tool 结果消息的 id 关联。
- 配置错误直接报清晰错误，不静默切换 Provider；错误信息绝不包含 API Key。
- Token usage：Provider 真实返回才记录；缺失时明确标记 available=False，不估算。
- usage 记录通过 set_usage_recorder() 注入（见 model_usage），记录失败不影响请求本身。
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field

import cloud_errors
import env_loader
import requests

# 启动时自动读取根目录 .env（已存在的环境变量不覆盖；幂等）
env_loader.load_env()

# 关键链路日志：只记 provider/model/耗时/状态/tokens，绝不记 Prompt/回答/密钥
logger = logging.getLogger("ai_study.llm")


# 超时标识：与 agent_loop.AGENT_TIMEOUT 取值一致（"agent_timeout"）
TIMEOUT_ERROR = "agent_timeout"

DEFAULT_OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

# 阿里云百炼（DashScope）OpenAI 兼容端点。
# 模型 / 地域 / 端点以百炼控制台实际可用配置为准，可用 LLM_BASE_URL 覆盖。
DEFAULT_OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OPENAI_MODEL = "qwen-plus"

PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai_compatible"
VALID_PROVIDERS = {PROVIDER_OLLAMA, PROVIDER_OPENAI}

CONNECT_TIMEOUT = 10

# 注意：requests 的 read timeout 是“两次读取之间”的超时，服务端持续滴流时
# 总耗时不会被它限制，所以它不能当成一次请求/整个 Run 的总预算——
# 总预算由调用方传入的 deadline 负责（流式分块之间逐块检查）。
READ_TIMEOUT = 30

# 用量记录钩子：fn(record: dict) -> None；默认不记录。
# 用量记录绝不能成为模型请求的强依赖（记录失败必须吞掉）。
_USAGE_RECORDER = None


class LLMConfigError(Exception):
    """模型配置错误（缺必需配置 / Provider 非法）。"""


@dataclass
class Settings:
    provider: str
    model: str
    chat_url: str
    api_key: str | None = None
    connect_timeout: int = CONNECT_TIMEOUT
    read_timeout: int = READ_TIMEOUT
    extra: dict = field(default_factory=dict)

    def describe(self):
        """用于日志/错误的安全描述（不含 API Key）。"""

        return f"provider={self.provider} model={self.model} url={self.chat_url}"


def set_usage_recorder(recorder):
    """注入用量记录函数（None 表示不记录）。"""

    global _USAGE_RECORDER

    _USAGE_RECORDER = recorder


def _env(name, default=None):
    value = os.getenv(name)

    if value is None:
        return default

    value = value.strip()

    return value or default


def default_model(provider=None):
    """当前配置的模型名（按 Provider 分离变量，避免 .env 云端模型名污染本地回退）。

    - openai_compatible（默认）：LLM_MODEL -> qwen-plus（百炼常见型号，以控制台为准）
    - ollama：OLLAMA_MODEL -> qwen3.5:4b（LLM_MODEL 只服务云端 Provider）
    """

    provider = (provider or _env("LLM_PROVIDER") or PROVIDER_OPENAI).lower()

    if provider == PROVIDER_OLLAMA:
        return _env("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL

    return _env("LLM_MODEL") or DEFAULT_OPENAI_MODEL


def ollama_chat_url():
    """Ollama 完整 chat 地址（只认 OLLAMA_URL / 默认本地）。

    LLM_BASE_URL 只服务 openai_compatible Provider，避免 .env 里的云端地址
    污染 ollama 回退配置（两个 Provider 的地址变量互相独立）。
    """

    legacy = _env("OLLAMA_URL")

    if legacy:
        return legacy

    return DEFAULT_OLLAMA_CHAT_URL


def _openai_chat_url():
    base = _env("LLM_BASE_URL") or DEFAULT_OPENAI_BASE_URL

    base = base.rstrip("/")

    if base.endswith("/chat/completions"):
        return base

    return base + "/chat/completions"


def reset_settings():
    """清空设置缓存（测试切换环境变量后使用）。"""

    global _SETTINGS

    _SETTINGS = None


_SETTINGS = None


def get_settings():
    """解析并校验模型配置（进程内缓存；reset_settings() 复位）。"""

    global _SETTINGS

    if _SETTINGS is not None:
        return _SETTINGS

    provider = (_env("LLM_PROVIDER") or PROVIDER_OPENAI).lower()

    if provider not in VALID_PROVIDERS:
        raise LLMConfigError(
            f"LLM_PROVIDER 取值非法：{provider!r}，只支持 ollama 或 openai_compatible"
        )

    model = default_model()

    if provider == PROVIDER_OLLAMA:
        settings = Settings(
            provider=PROVIDER_OLLAMA,
            model=model,
            chat_url=ollama_chat_url(),
        )
    else:
        api_key = _env("LLM_API_KEY")

        if not api_key:
            raise LLMConfigError(
                "使用 openai_compatible 时必须设置 LLM_API_KEY（不要把密钥写进代码）"
            )

        settings = Settings(
            provider=PROVIDER_OPENAI,
            model=model,
            chat_url=_openai_chat_url(),
            api_key=api_key,
        )

    _SETTINGS = settings

    return settings


# ==========================
# 内部标准消息格式 <-> Provider 格式
# ==========================


def _normalize_arguments(arguments):
    """arguments 归一化为 dict（兼容对象 / JSON 字符串 / 非法 JSON）。"""

    if isinstance(arguments, dict):
        return arguments

    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}

        return parsed if isinstance(parsed, dict) else {}

    return {}


def _new_tool_call_id(counter):
    counter["value"] += 1

    return f"call_{counter['value']}"


def _to_openai_messages(messages):
    """内部标准消息历史 -> OpenAI 格式。

    关键点：tool 结果消息必须带 tool_call_id 与 assistant 工具调用关联。
    内部历史里 tool 消息可以不带 id，这里按出现顺序与前一条 assistant
    tool_calls 的 id 一一配对（缺失时补发合成 id，保证请求结构合法）。
    """

    converted = []
    pending_ids = []
    counter = {"value": 0}

    for message in messages:
        role = message.get("role")

        if role == "assistant":
            tool_calls = message.get("tool_calls")
            content = message.get("content") or ""

            if not tool_calls:
                converted.append({"role": "assistant", "content": content})
                continue

            openai_calls = []
            pending_ids = []

            for item in tool_calls:
                function = item.get("function") or {}
                identifier = item.get("id") or _new_tool_call_id(counter)
                arguments = _normalize_arguments(function.get("arguments"))

                pending_ids.append(identifier)
                openai_calls.append({
                    "id": identifier,
                    "type": "function",
                    "function": {
                        "name": function.get("name"),
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                })

            converted.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": openai_calls,
            })

        elif role == "tool":
            identifier = message.get("tool_call_id")

            if identifier is None:
                if pending_ids:
                    identifier = pending_ids.pop(0)
                else:
                    identifier = _new_tool_call_id(counter)

            converted.append({
                "role": "tool",
                "tool_call_id": identifier,
                "content": message.get("content") or "",
            })

        else:
            converted.append({
                "role": role,
                "content": message.get("content") or "",
            })

    return converted


def _to_openai_tools(tools):
    """工具 Schema 原样透传（本项目 Schema 已是 OpenAI tools 格式）。"""

    return tools


def _usage_from_counts(prompt_tokens, completion_tokens):
    """把 Provider 的计数整理为统一 usage 结构。

    任一关键计数缺失都标记 available=False 并置 None，不做任何估算。
    """

    if prompt_tokens is None or completion_tokens is None:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "available": False,
        }

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "available": True,
    }


def _record_usage(record):
    recorder = _USAGE_RECORDER

    if recorder is None:
        return

    try:
        recorder(record)
    except Exception:  # noqa: BLE001 - 用量记录失败绝不影响模型请求
        pass


def _build_result(message, usage, settings, started):
    return {
        "message": message,
        "usage": usage,
        "provider": settings.provider,
        "model": settings.model,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def _finish(message, usage, settings, started, ok=True, error_type=None):
    """生成结果并顺带发出用量记录（不记录 Prompt / 回答内容 / 密钥）。"""

    result = _build_result(message, usage, settings, started)

    _record_usage({
        "provider": settings.provider,
        "model": settings.model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "usage_available": usage.get("available", False),
        "elapsed_seconds": result["elapsed_seconds"],
        "ok": ok,
        "error_type": error_type,
    })

    return result


# ==========================
# Provider 适配器
# ==========================


def _http_post(url, headers, payload, stream, timeout):
    """唯一的 HTTP 边界（测试可整体打桩）。"""

    return requests.post(
        url,
        headers=headers,
        json=payload,
        stream=stream,
        timeout=timeout,
    )


def _check_deadline(deadline):
    return deadline is not None and deadline.expired()


def _chat_ollama(settings, messages, tools, deadline, read_timeout):
    payload = {
        "model": settings.model,
        "messages": messages,
        "stream": True,
        "think": False,
    }

    if tools:
        payload["tools"] = tools

    policy = cloud_errors.RetryPolicy("LLM_MAX_RETRIES")

    def request():
        response = _http_post(
            settings.chat_url,
            headers=None,
            payload=payload,
            stream=True,
            timeout=(settings.connect_timeout, read_timeout),
        )
        response.raise_for_status()

        return response

    def classify(error):
        if isinstance(error, requests.exceptions.Timeout):
            return "timeout"
        if isinstance(error, requests.exceptions.HTTPError):
            status = error.response.status_code if error.response is not None else None

            return cloud_errors.classify_status(status)[0]
        if isinstance(error, requests.exceptions.RequestException):
            return "connection"
        return None

    response, failure = policy.run(request, classify)

    if failure is not None:
        error_type, message, attempts = failure

        if error_type == "timeout":
            return None, "Ollama 请求超时", "timeout"

        return None, f"Ollama 调用失败（{message}，尝试 {attempts} 次）", error_type

    try:
        content_parts = []
        tool_calls = []
        prompt_count = None
        completion_count = None

        for line in response.iter_lines(decode_unicode=True):
            # 每个数据块之间检查绝对 deadline：滴流响应也无法突破总预算
            if _check_deadline(deadline):
                return None, TIMEOUT_ERROR, "timeout"

            if not line:
                continue

            try:
                chunk = json.loads(line)
            except ValueError:
                continue

            message = chunk.get("message") or {}

            if message.get("content"):
                content_parts.append(message["content"])

            # 流式响应可能把 tool_call 分散在不同数据块里，按 id 累积而不是覆盖
            for item in message.get("tool_calls") or []:
                identifier = item.get("id")

                if identifier is not None and any(
                    existing.get("id") == identifier for existing in tool_calls
                ):
                    continue

                tool_calls.append(item)

            if chunk.get("prompt_eval_count") is not None:
                prompt_count = chunk.get("prompt_eval_count")

            if chunk.get("eval_count") is not None:
                completion_count = chunk.get("eval_count")

            if chunk.get("done"):
                break
    except requests.exceptions.Timeout:
        return None, "Ollama 请求超时", "timeout"
    except requests.exceptions.RequestException as error:
        return None, f"Ollama 连接中断（{type(error).__name__}）", "connection"
    finally:
        response.close()

    assistant_message = {
        "role": "assistant",
        "content": "".join(content_parts),
    }

    normalized_calls = []

    for index, item in enumerate(tool_calls):
        function = item.get("function") or {}
        normalized_calls.append({
            "id": item.get("id") or f"call_{index + 1}",
            "function": {
                "name": function.get("name"),
                "arguments": _normalize_arguments(function.get("arguments")),
            },
        })

    if normalized_calls:
        assistant_message["tool_calls"] = normalized_calls

    usage = _usage_from_counts(prompt_count, completion_count)

    return {"message": assistant_message, "usage": usage}, None, None


def _merge_openai_tool_delta(existing, delta):
    """把流式 tool_call 分块合并进累积条目（id/name/arguments 分块到达）。"""

    function_delta = delta.get("function") or {}

    if delta.get("id") and not existing.get("id"):
        existing["id"] = delta["id"]

    # name 理论上在首个分块完整到达，但按“拼接累积”处理分块情况更稳
    if function_delta.get("name"):
        existing["function"]["name"] = (
            existing["function"].get("name") or ""
        ) + function_delta["name"]

    if function_delta.get("arguments"):
        existing["function"]["arguments"] = (
            existing["function"].get("arguments") or ""
        ) + function_delta["arguments"]


def _accumulate_openai_tool_deltas(tool_deltas):
    """按 index 累积流式 tool_call 分块，保持模型给出的顺序。"""

    by_index = {}

    for delta in tool_deltas:
        index = delta.get("index", 0)

        if index not in by_index:
            by_index[index] = {
                "id": None,
                "function": {"name": None, "arguments": ""},
            }

        _merge_openai_tool_delta(by_index[index], delta)

    calls = []

    for index in sorted(by_index):
        item = by_index[index]
        name = item["function"].get("name")

        if not name:
            continue

        calls.append({
            "id": item.get("id") or f"call_{index + 1}",
            "function": {
                "name": name,
                "arguments": _normalize_arguments(item["function"].get("arguments")),
            },
        })

    return calls


def _import_openai():
    """复用环境中已有的 openai SDK（不引入额外客户端）；未安装时给清晰配置错误。"""

    try:
        import openai
    except ImportError as error:
        raise LLMConfigError(
            "使用 openai_compatible 需要安装 openai 包（pip install openai）"
        ) from error

    return openai


def _make_openai_client(settings, read_timeout):
    """构建 OpenAI 兼容客户端（base_url 自动去掉 /chat/completions 后缀）。"""

    openai = _import_openai()

    base_url = settings.chat_url

    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]

    return openai.OpenAI(
        base_url=base_url,
        api_key=settings.api_key,
        timeout=read_timeout,
        max_retries=0,
    )


def _classify_openai_error(openai, error):
    """openai SDK 异常 -> (稳定错误描述, error_type)。

    注意超时是连接错误的子类，先判超时；HTTP 状态码按 429/认证/参数/5xx 细分。
    错误描述绝不包含 API Key。
    """

    if isinstance(error, openai.APITimeoutError):
        return "OpenAI 兼容 API 请求超时", "timeout"

    if isinstance(error, openai.APIConnectionError):
        return f"无法连接 OpenAI 兼容 API（{type(error).__name__}）", "connection"

    if isinstance(error, openai.APIStatusError):
        status = getattr(error, "status_code", None)
        error_type, description = cloud_errors.classify_status(status)

        return f"OpenAI 兼容 API {description}", error_type

    return f"OpenAI 兼容 API 调用失败（{type(error).__name__}）", "llm_error"


def _openai_start_stream(client, openai, payload):
    """发起流式请求（有限重试：429/5xx/连接失败可重试，认证/参数/超时不重试）。

    不认识 stream_options 的实现去掉该参数后重试一次（结构性兼容，不算错误重试）。
    """

    policy = cloud_errors.RetryPolicy("LLM_MAX_RETRIES")

    def request():
        try:
            return client.chat.completions.create(**payload)
        except openai.BadRequestError:
            if "stream_options" not in payload:
                raise

            payload.pop("stream_options", None)

            return client.chat.completions.create(**payload)

    def classify(error):
        if isinstance(error, openai.OpenAIError):
            return _classify_openai_error(openai, error)[1]

        return None

    stream, failure = policy.run(request, classify)

    if failure is not None:
        error_type, message, attempts = failure

        if error_type == "timeout":
            return None, ("OpenAI 兼容 API 请求超时", "timeout")
        if error_type == "auth_error":
            return None, (f"OpenAI 兼容 API 认证失败，请检查 LLM_API_KEY（{message}）", "auth_error")
        if error_type == "invalid_request":
            return None, (f"OpenAI 兼容 API 请求参数错误（{message}）", "invalid_request")

        return None, (f"OpenAI 兼容 API 调用失败（{message}，尝试 {attempts} 次）", error_type)

    return stream, None


def _chat_openai(settings, messages, tools, deadline, read_timeout):
    openai = _import_openai()

    payload = {
        "model": settings.model,
        "messages": _to_openai_messages(messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    if tools:
        payload["tools"] = _to_openai_tools(tools)

    try:
        client = _make_openai_client(settings, read_timeout)
    except LLMConfigError as error:
        return None, f"模型配置错误：{error}", "config_error"

    stream, stream_error = _openai_start_stream(client, openai, payload)

    if stream_error is not None:
        return None, stream_error[0], stream_error[1]

    content_parts = []
    tool_deltas = []
    usage_raw = None

    try:
        for chunk in stream:
            # 每个数据块之间检查绝对 deadline：滴流响应也无法突破总预算
            if _check_deadline(deadline):
                return None, TIMEOUT_ERROR, "timeout"

            chunk_usage = getattr(chunk, "usage", None)

            if chunk_usage is not None:
                usage_raw = chunk_usage

            choices = getattr(chunk, "choices", None) or []

            if not choices:
                continue

            delta = choices[0].delta

            content = getattr(delta, "content", None)

            if content:
                content_parts.append(content)

            # 流式 tool_call 分块：统一转成 dict 再按 index 累积（id/name/arguments 可能分开到达）
            for item in getattr(delta, "tool_calls", None) or []:
                function = getattr(item, "function", None)

                tool_deltas.append({
                    "index": getattr(item, "index", 0),
                    "id": getattr(item, "id", None),
                    "function": {
                        "name": getattr(function, "name", None),
                        "arguments": getattr(function, "arguments", None),
                    },
                })
    except openai.OpenAIError as error:
        return None, *_classify_openai_error(openai, error)
    finally:
        close = getattr(stream, "close", None)

        if callable(close):
            close()

    assistant_message = {
        "role": "assistant",
        "content": "".join(content_parts),
    }

    normalized_calls = _accumulate_openai_tool_deltas(tool_deltas)

    if normalized_calls:
        assistant_message["tool_calls"] = normalized_calls

    if isinstance(usage_raw, dict):
        prompt_tokens = usage_raw.get("prompt_tokens")
        completion_tokens = usage_raw.get("completion_tokens")
    else:
        prompt_tokens = getattr(usage_raw, "prompt_tokens", None)
        completion_tokens = getattr(usage_raw, "completion_tokens", None)

    usage = _usage_from_counts(prompt_tokens, completion_tokens)

    return {"message": assistant_message, "usage": usage}, None, None


# ==========================
# 对外统一入口
# ==========================


def chat_stream(messages, deadline=None, read_timeout=None):
    """流式文本输出（SSE 问答用；不支持工具调用——工具场景请用 chat()）。

    逐块 yield 事件字典：
      {"type": "delta", "text": str}                    文本增量
      {"type": "done", "usage": {...}, "model": str}    正常结束（含真实 token usage）
      {"type": "error", "error": str, "error_type": str} 稳定错误
    流被调用方提前关闭（客户端断开）时：上游连接在 finally 中关闭，不再产生事件。
    """

    try:
        settings = get_settings()
    except LLMConfigError as error:
        yield {"type": "error", "error": f"模型配置错误：{error}", "error_type": "config_error"}
        return

    if _check_deadline(deadline):
        yield {"type": "error", "error": "agent timeout", "error_type": TIMEOUT_ERROR}
        return

    effective_read_timeout = read_timeout or settings.read_timeout

    if deadline is not None:
        effective_read_timeout = max(1, min(effective_read_timeout, deadline.remaining()))

    started = time.perf_counter()

    if settings.provider == PROVIDER_OLLAMA:
        iterator = _stream_ollama(settings, messages, deadline, effective_read_timeout)
    else:
        iterator = _stream_openai(settings, messages, deadline, effective_read_timeout)

    usage = _usage_from_counts(None, None)

    try:
        for event in iterator:
            if event[0] == "delta":
                yield {"type": "delta", "text": event[1]}
            elif event[0] == "usage":
                usage = event[1]
            elif event[0] == "error":
                _record_usage({
                    "provider": settings.provider,
                    "model": settings.model,
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "usage_available": False,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "ok": False,
                    "error_type": event[2],
                })
                logger.warning(
                    "llm_stream provider=%s model=%s ok=False error_type=%s",
                    settings.provider, settings.model, event[2],
                )
                yield {"type": "error", "error": event[1], "error_type": event[2]}
                return
    finally:
        close = getattr(iterator, "close", None)

        if callable(close):
            close()

    elapsed = round(time.perf_counter() - started, 3)
    _record_usage({
        "provider": settings.provider,
        "model": settings.model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "usage_available": usage.get("available", False),
        "elapsed_seconds": elapsed,
        "ok": True,
        "error_type": None,
    })
    logger.info(
        "llm_stream provider=%s model=%s ok=True elapsed=%.2fs prompt_tokens=%s completion_tokens=%s",
        settings.provider, settings.model, elapsed,
        usage.get("prompt_tokens"), usage.get("completion_tokens"),
    )

    yield {"type": "done", "usage": usage, "model": settings.model, "elapsed_seconds": elapsed}


def _stream_ollama(settings, messages, deadline, read_timeout):
    """Ollama NDJSON 流式读取，逐块 yield ("delta", text) / ("usage", ...) / ("error", ...)。"""

    payload = {
        "model": settings.model,
        "messages": messages,
        "stream": True,
        "think": False,
    }

    try:
        response = _http_post(
            settings.chat_url,
            headers=None,
            payload=payload,
            stream=True,
            timeout=(settings.connect_timeout, read_timeout),
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        yield ("error", "Ollama 请求超时", "timeout")
        return
    except requests.exceptions.HTTPError:
        yield ("error", "Ollama HTTP 错误（服务端返回非 2xx）", "server_error")
        return
    except requests.exceptions.RequestException as error:
        yield ("error", f"无法连接 Ollama（{type(error).__name__}）", "connection")
        return

    prompt_count = None
    completion_count = None

    try:
        for line in response.iter_lines(decode_unicode=True):
            if _check_deadline(deadline):
                yield ("error", "agent timeout", TIMEOUT_ERROR)
                return

            if not line:
                continue

            try:
                chunk = json.loads(line)
            except ValueError:
                continue

            message = chunk.get("message") or {}

            if message.get("content"):
                yield ("delta", message["content"])

            if chunk.get("prompt_eval_count") is not None:
                prompt_count = chunk["prompt_eval_count"]
            if chunk.get("eval_count") is not None:
                completion_count = chunk["eval_count"]

            if chunk.get("done"):
                break
    except requests.exceptions.Timeout:
        yield ("error", "Ollama 请求超时", "timeout")
        return
    except requests.exceptions.RequestException as error:
        yield ("error", f"Ollama 连接中断（{type(error).__name__}）", "connection")
        return
    finally:
        response.close()

    yield ("usage", _usage_from_counts(prompt_count, completion_count))


def _stream_openai(settings, messages, deadline, read_timeout):
    """OpenAI 兼容（百炼等）SSE 流式读取，逐块 yield delta/usage/error。"""

    openai = _import_openai()

    client = openai.OpenAI(
        base_url=settings.chat_url[: -len("/chat/completions")]
        if settings.chat_url.endswith("/chat/completions") else settings.chat_url,
        api_key=settings.api_key,
        timeout=read_timeout,
        max_retries=0,
    )

    payload = {
        "model": settings.model,
        "messages": _to_openai_messages(messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    policy = cloud_errors.RetryPolicy("LLM_MAX_RETRIES")

    def request():
        try:
            return client.chat.completions.create(**payload)
        except openai.BadRequestError:
            if "stream_options" not in payload:
                raise
            payload.pop("stream_options", None)

            return client.chat.completions.create(**payload)

    def classify(error):
        if isinstance(error, openai.OpenAIError):
            return _classify_openai_error(openai, error)[1]
        return None

    stream, failure = policy.run(request, classify)

    if failure is not None:
        error_type, message, attempts = failure

        if error_type == "timeout":
            yield ("error", "OpenAI 兼容 API 请求超时", "timeout")
        else:
            yield ("error", f"OpenAI 兼容 API 调用失败（{message}，尝试 {attempts} 次）", error_type)

        return

    usage_raw = None

    try:
        for chunk in stream:
            if _check_deadline(deadline):
                yield ("error", "agent timeout", TIMEOUT_ERROR)
                return

            chunk_usage = getattr(chunk, "usage", None)

            if chunk_usage is not None:
                usage_raw = chunk_usage

            choices = getattr(chunk, "choices", None) or []

            if not choices:
                continue

            content = getattr(choices[0].delta, "content", None)

            if content:
                yield ("delta", content)
    except openai.OpenAIError as error:
        message, error_type = _classify_openai_error(openai, error)
        yield ("error", message, error_type)
        return
    finally:
        close = getattr(stream, "close", None)

        if callable(close):
            close()

    yield ("usage", _usage_from_counts(
        getattr(usage_raw, "prompt_tokens", None),
        getattr(usage_raw, "completion_tokens", None),
    ))


def chat(messages, tools=None, deadline=None, read_timeout=None):
    """统一 Chat Completion 入口，返回 (result, error)。

    - result: {"message": 内部标准 assistant 消息, "usage": {...},
               "provider", "model", "elapsed_seconds"}；失败时为 None
    - error: 稳定错误描述字符串；超时返回 TIMEOUT_ERROR（"agent_timeout"），
      配置错误返回 "模型配置错误：..."。任何错误信息都不包含 API Key。
    - deadline 为 None 时不限总时长（只受 connect/read timeout 约束）。
    """

    try:
        settings = get_settings()
    except LLMConfigError as error:
        return None, f"模型配置错误：{error}"

    if _check_deadline(deadline):
        return None, TIMEOUT_ERROR

    effective_read_timeout = read_timeout or settings.read_timeout

    if deadline is not None:
        # 单次静默等待也不会超过剩余预算
        effective_read_timeout = max(1, min(effective_read_timeout, deadline.remaining()))

    started = time.perf_counter()

    if settings.provider == PROVIDER_OLLAMA:
        result, error, error_type = _chat_ollama(
            settings, messages, tools, deadline, effective_read_timeout
        )
    else:
        result, error, error_type = _chat_openai(
            settings, messages, tools, deadline, effective_read_timeout
        )

    if result is None:
        _record_usage({
            "provider": settings.provider,
            "model": settings.model,
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "usage_available": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "ok": False,
            "error_type": error_type or "llm_error",
        })
        logger.warning(
            "llm_call provider=%s model=%s ok=False error_type=%s",
            settings.provider, settings.model, error_type or "llm_error",
        )

        return None, error

    final_result = _finish(
        result["message"], result["usage"], settings, started
    )
    logger.info(
        "llm_call provider=%s model=%s ok=True elapsed=%.2fs prompt_tokens=%s completion_tokens=%s",
        settings.provider, settings.model,
        final_result["elapsed_seconds"],
        final_result["usage"].get("prompt_tokens"),
        final_result["usage"].get("completion_tokens"),
    )

    return final_result, None
