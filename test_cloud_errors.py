"""云端错误分类与重试策略测试：直接运行 python test_cloud_errors.py。

覆盖：429/5xx/连接失败可重试、认证/参数/超时不重试、重试上限、指数退避、
RETRYABLE 集合稳定、分类映射正确。
"""

import os

import cloud_errors


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


# 1. 状态码分类
check("429 -> rate_limit", cloud_errors.classify_status(429)[0] == "rate_limit")
check("401 -> auth_error", cloud_errors.classify_status(401)[0] == "auth_error")
check("403 -> auth_error", cloud_errors.classify_status(403)[0] == "auth_error")
check("400 -> invalid_request", cloud_errors.classify_status(400)[0] == "invalid_request")
check("422 -> invalid_request", cloud_errors.classify_status(422)[0] == "invalid_request")
check("500 -> server_error", cloud_errors.classify_status(500)[0] == "server_error")
check("503 -> server_error", cloud_errors.classify_status(503)[0] == "server_error")
check("418 -> llm_error（未分类）", cloud_errors.classify_status(418)[0] == "llm_error")
check("无状态码 -> llm_error", cloud_errors.classify_status(None)[0] == "llm_error")

# 2. 可重试集合稳定（写操作相关的错误类型绝不进重试集合）
check("可重试集合为 connection/rate_limit/server_error",
      cloud_errors.RETRYABLE_ERROR_TYPES == {"connection", "rate_limit", "server_error"})

# 3. 退避增长且有上限
backoffs = [cloud_errors.retry_after_seconds(i) for i in range(8)]
check("退避指数增长", backoffs[:3] == [0.5, 1.0, 2.0], f"backoffs={backoffs}")
check("退避有上限", max(backoffs) <= cloud_errors.MAX_BACKOFF_SECONDS)

# 4. 重试行为：可重试错误重试到上限后失败；不可重试错误立即失败
sleeps = []


class Boom(Exception):
    def __init__(self, kind):
        super().__init__(f"boom-{kind}")
        self.kind = kind


def make_request(fail_times, kind):
    state = {"calls": 0}

    def request():
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise Boom(kind)
        return "ok"

    return request, state


def classify_by_kind(error):
    return error.kind


policy = cloud_errors.RetryPolicy("CLOUD_MAX_RETRIES", sleep=sleeps.append)

request, state = make_request(2, "rate_limit")
result, failure = policy.run(request, classify_by_kind)
check("429 重试 2 次后成功", result == "ok" and state["calls"] == 3 and len(sleeps) == 2)

sleeps.clear()
request, state = make_request(5, "server_error")
result, failure = policy.run(request, classify_by_kind)
check("5xx 超过重试上限后失败",
      result is None and failure[0] == "server_error" and state["calls"] == 3,
      f"calls={state['calls']} failure={failure}")

sleeps.clear()
request, state = make_request(3, "auth_error")
result, failure = policy.run(request, classify_by_kind)
check("认证错误不重试", result is None and failure[0] == "auth_error" and state["calls"] == 1)

sleeps.clear()
request, state = make_request(3, "timeout")
result, failure = policy.run(request, classify_by_kind)
check("超时不重试", result is None and failure[0] == "timeout" and state["calls"] == 1)

sleeps.clear()
request, state = make_request(3, "invalid_request")
result, failure = policy.run(request, classify_by_kind)
check("参数错误不重试", result is None and failure[0] == "invalid_request" and state["calls"] == 1)

sleeps.clear()
request, state = make_request(3, None)
result, failure = policy.run(request, classify_by_kind)
check("未分类异常不重试", result is None and state["calls"] == 1)

# 5. 重试次数可配置（0 = 禁用重试）
os.environ["CLOUD_MAX_RETRIES"] = "0"
try:
    strict = cloud_errors.RetryPolicy("CLOUD_MAX_RETRIES", sleep=sleeps.append)
    request, state = make_request(1, "connection")
    result, failure = strict.run(request, classify_by_kind)
    check("CLOUD_MAX_RETRIES=0 禁用重试", result is None and state["calls"] == 1)
finally:
    os.environ.pop("CLOUD_MAX_RETRIES", None)

check("失败信息带异常类名", failure is not None and "Boom" in failure[1], f"failure={failure}")

# 6. 错误信息脱敏：上游异常回显的密钥不得出现在对外错误文本里
sk_text = "认证失败：Incorrect API key provided: sk-abcdef1234567890"
check("sk- 形态密钥被脱敏",
      "sk-abcdef1234567890" not in cloud_errors.redact_secrets(sk_text)
      and cloud_errors.REDACTED in cloud_errors.redact_secrets(sk_text))

bearer_text = "请求头 Authorization: Bearer qwen-token-abcdef123456 被拒绝"
check("Bearer 令牌被脱敏",
      "qwen-token-abcdef123456" not in cloud_errors.redact_secrets(bearer_text)
      and "Bearer" in cloud_errors.redact_secrets(bearer_text))

os.environ["LLM_API_KEY"] = "notask-shaped-secret-value"
try:
    echoed = "服务返回：invalid key notask-shaped-secret-value"
    check("已配置真实 Key（非 sk- 形态）被脱敏",
          "notask-shaped-secret-value" not in cloud_errors.redact_secrets(echoed))
finally:
    os.environ.pop("LLM_API_KEY", None)

plain = "boom-auth_error（重试 2 次）"
check("无密钥文本保持原样", cloud_errors.redact_secrets(plain) == plain)
check("None 输入返回空串", cloud_errors.redact_secrets(None) == "")

# RetryPolicy 失败信息整体经过脱敏
os.environ["LLM_API_KEY"] = "sk-live-secret-000111"
try:
    class EchoError(Exception):
        pass

    def echo_request():
        raise EchoError("provider echoed sk-live-secret-000111")

    policy = cloud_errors.RetryPolicy("CLOUD_MAX_RETRIES", sleep=lambda s: None)
    result, failure = policy.run(echo_request, lambda e: "auth_error")

    check("RetryPolicy 失败信息不含密钥",
          failure is not None and "sk-live-secret-000111" not in failure[1]
          and cloud_errors.REDACTED in failure[1]
          and "EchoError" in failure[1],
          f"failure={failure}")
finally:
    os.environ.pop("LLM_API_KEY", None)

print("ALL TESTS COMPLETED")
