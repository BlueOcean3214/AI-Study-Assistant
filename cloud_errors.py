"""云端 API 错误分类与有限重试策略（llm_client / embedding_service 共用）。

错误分类（稳定 error_type，日志与用量记录共用）：
- timeout          请求超时（不自动重试：流式/长请求重试代价高，交给调用方预算）
- connection       连接失败（可重试：无副作用请求）
- rate_limit       429（可重试，尊重 Retry-After，有限次数）
- server_error     5xx（可重试，有限次数）
- auth_error       401/403 认证失败（不重试：重试只会重复计费失败）
- invalid_request  400/422 参数错误（不重试：请求本身不合法）
- llm_error        其他未分类错误

重试约束：
- 只用于无副作用的计算型请求（chat / embeddings），写操作一律不在此层重试
- 有限次数（默认 2，可用环境变量调）+ 指数退避，绝不无限重试
- 错误信息绝不包含 API Key / 完整 Prompt
"""

import os
import re
import time


RETRYABLE_ERROR_TYPES = {"connection", "rate_limit", "server_error"}

DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 8.0

# 疑似密钥形态：sk- 开头的令牌、Bearer 令牌
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{6,}", re.IGNORECASE),
)

# 已配置的真实密钥：即使不符合 sk- 形态也不允许出现在错误信息里
_SECRET_ENV_NAMES = ("LLM_API_KEY", "EMBEDDING_API_KEY", "RERANK_API_KEY")

REDACTED = "***REDACTED***"


def redact_secrets(text):
    """把错误文本中的疑似密钥（sk- 令牌 / Bearer 令牌 / 已配置的真实 Key）替换为掩码。

    云端 API 的异常文本可能回显请求里的密钥（认证失败时尤其常见），
    所有对外返回的错误信息都要经过本函数，避免凭据泄漏到响应或日志。
    """

    if text is None:
        return ""

    result = str(text)

    for name in _SECRET_ENV_NAMES:
        value = (os.getenv(name) or "").strip()

        if len(value) >= 6 and value in result:
            result = result.replace(value, REDACTED)

    result = _SECRET_PATTERNS[0].sub(REDACTED, result)
    result = _SECRET_PATTERNS[1].sub(lambda match: match.group(1) + REDACTED, result)

    return result


def max_retries(env_name, default=DEFAULT_MAX_RETRIES):
    value = (os.getenv(env_name) or "").strip()

    if not value:
        return default

    try:
        number = int(value)
    except ValueError:
        return default

    return max(0, min(number, 5))


def classify_status(status_code):
    """HTTP 状态码 -> (error_type, 描述后缀)。"""

    if status_code == 429:
        return "rate_limit", "请求频率超限（429）"
    if status_code in (401, 403):
        return "auth_error", f"认证/授权失败（status={status_code}），请检查 API Key"
    if status_code in (400, 422):
        return "invalid_request", f"请求参数错误（status={status_code}）"
    if isinstance(status_code, int) and status_code >= 500:
        return "server_error", f"服务端错误（status={status_code}）"
    return "llm_error", f"HTTP 错误（status={status_code}）"


def retry_after_seconds(attempt, base=DEFAULT_BACKOFF_SECONDS):
    """指数退避（带上限）；attempt 从 0 开始。"""

    return min(base * (2 ** attempt), MAX_BACKOFF_SECONDS)


class RetryPolicy:
    """有限重试执行器：只对可重试错误类型重试。

    usage:
        policy = RetryPolicy("LLM_MAX_RETRIES")
        result = policy.run(lambda: do_request(), classify=fn)
    其中 classify(error) -> error_type 或 None（None 表示成功路径外的异常不重试）。
    """

    def __init__(self, env_name="CLOUD_MAX_RETRIES", sleep=time.sleep):
        self.limit = max_retries(env_name)
        self.sleep = sleep

    def run(self, request, classify):
        """执行请求；返回 (result, None) 或 (None, (error_type, error_message, attempts))。

        request() 抛异常时用 classify(error) 判断是否重试。
        """

        attempts = 0

        while True:
            try:
                return request(), None
            except Exception as error:  # noqa: BLE001 - 由 classify 决定处理方式
                error_type = classify(error)
                attempts += 1

                if error_type not in RETRYABLE_ERROR_TYPES or attempts > self.limit:
                    # 异常文本可能回显请求参数（含密钥），统一脱敏后再对外
                    message = redact_secrets(f"{type(error).__name__}: {error}")

                    return None, (error_type, message, attempts)

                self.sleep(retry_after_seconds(attempts - 1))
