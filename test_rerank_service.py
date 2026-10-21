"""Rerank 服务测试：直接运行 python test_rerank_service.py。

全 Mock（桩 requests.post），不发起真实请求。

覆盖：
- 开关与配置校验（独立 RERANK_API_KEY）
- 成功重排：按 relevance_score 排序、保留 source / 原始 score / rerank_score（量纲不混用）
- 失败处理：回退未重排结果并明确标注（RERANK_FALLBACK=1）/ 直接报错（=0）
- 超时 / HTTP 错误 / 响应格式异常的稳定错误类型
- main.retrieve_for_ask 集成（启用时重排、失败回退原始顺序）
"""

import contextlib
import os

import rerank_service
import main


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


CANDIDATES = [
    {"content": "片段A", "source": "a.txt", "score": 0.9},
    {"content": "片段B", "source": "b.txt", "score": 0.7},
    {"content": "片段C", "source": "c.txt", "score": 0.6},
]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"status={self.status_code}")

    def json(self):
        return self.payload


@contextlib.contextmanager
def env(**values):
    keys = ["RAG_RERANK_ENABLED", "RERANK_API_KEY", "RERANK_BASE_URL", "RERANK_MODEL",
            "RERANK_TOP_N", "RERANK_TIMEOUT", "RERANK_FALLBACK", "RERANK_PROVIDER"]
    saved = {k: os.environ.get(k) for k in keys}

    for k in keys:
        os.environ.pop(k, None)
    for k, v in values.items():
        os.environ[k] = v

    try:
        yield
    finally:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@contextlib.contextmanager
def fake_post(handler):
    original = rerank_service.requests.post
    calls = []

    def wrapper(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return handler(url, json)

    rerank_service.requests.post = wrapper

    try:
        yield calls
    finally:
        rerank_service.requests.post = original


def rerank_payload(scores):
    return {"output": {"results": [
        {"index": i, "relevance_score": s} for i, s in enumerate(scores)
    ]}}


# 1. 开关与配置
check("默认关闭重排", rerank_service.is_enabled() is False)

with env(RAG_RERANK_ENABLED="1"):
    check("RAG_RERANK_ENABLED=1 开启重排", rerank_service.is_enabled() is True)

with env(RAG_RERANK_ENABLED="1"):
    try:
        rerank_service.get_settings()
        check("缺 RERANK_API_KEY 报配置错误", False)
    except rerank_service.RerankError as error:
        check("缺 RERANK_API_KEY 报配置错误", "RERANK_API_KEY" in str(error))

# 2. 成功重排
def ok_handler(url, payload):
    return FakeResponse(rerank_payload([0.2, 0.9, 0.5]))

with env(RAG_RERANK_ENABLED="1", RERANK_API_KEY="sk-test", RERANK_TOP_N="2"), \
        fake_post(ok_handler) as calls:
    outcome = rerank_service.rerank("查询", CANDIDATES)

check("重排成功并按 relevance_score 排序",
      outcome["ok"] is True and outcome["rerank_applied"] is True
      and [r["source"] for r in outcome["results"]] == ["b.txt", "c.txt"],
      f"results={[(r['source'], r['rerank_score']) for r in outcome['results']]}")
check("保留原始 score 与 rerank_score（不混用量纲）",
      outcome["results"][0]["score"] == 0.7 and outcome["results"][0]["rerank_score"] == 0.9)
check("请求发往独立 Rerank 端点",
      calls[0]["url"].endswith("/api/v1/services/rerank/text-rerank/text-rerank")
      and calls[0]["json"]["model"] == "qwen3-rerank"
      and calls[0]["json"]["documents"] == ["片段A", "片段B", "片段C"])
check("RERANK_TOP_N 生效", len(outcome["results"]) == 2)

# 3. 失败：默认回退（标注 rerank_applied=False）
def timeout_handler(url, payload):
    import requests
    raise requests.exceptions.Timeout("超时")

with env(RAG_RERANK_ENABLED="1", RERANK_API_KEY="sk-test"), fake_post(timeout_handler):
    outcome = rerank_service.rerank("查询", CANDIDATES)

check("重排超时默认回退未重排结果并明确标注",
      outcome["ok"] is True and outcome["rerank_applied"] is False
      and [r["source"] for r in outcome["results"]] == ["a.txt", "b.txt", "c.txt"]
      and "超时" in outcome["reason"])

# 4. 失败：RERANK_FALLBACK=0 直接报错（不静默假装成功）
with env(RAG_RERANK_ENABLED="1", RERANK_API_KEY="sk-test", RERANK_FALLBACK="0"), \
        fake_post(timeout_handler):
    outcome = rerank_service.rerank("查询", CANDIDATES)

check("禁止回退时返回稳定失败",
      outcome["ok"] is False and outcome["error_type"] == "timeout")

# 5. HTTP / 响应格式异常
def http_handler(url, payload):
    return FakeResponse({}, status_code=500)

with env(RAG_RERANK_ENABLED="1", RERANK_API_KEY="sk-test", RERANK_FALLBACK="0"), \
        fake_post(http_handler):
    outcome = rerank_service.rerank("查询", CANDIDATES)
    check("HTTP 错误稳定类型",
          outcome["ok"] is False and outcome["error_type"] == "rerank_http_error")

def bad_shape(url, payload):
    return FakeResponse({"unexpected": True})

with env(RAG_RERANK_ENABLED="1", RERANK_API_KEY="sk-test", RERANK_FALLBACK="0"), \
        fake_post(bad_shape):
    outcome = rerank_service.rerank("查询", CANDIDATES)
    check("响应格式异常稳定类型",
          outcome["ok"] is False and outcome["error_type"] == "rerank_response_error")

# 6. main.retrieve_for_ask 集成
original_custom = main.retrieve_chunks

with env(RAG_RERANK_ENABLED="1", RERANK_API_KEY="sk-test", RERANK_TOP_N="3"), \
        fake_post(ok_handler):
    main.retrieve_chunks = lambda q: list(CANDIDATES)
    result = main.retrieve_for_ask("查询")
    check("启用时 /ask 检索结果经过重排",
          [r["source"] for r in result] == ["b.txt", "c.txt", "a.txt"])

with env():
    main.retrieve_chunks = lambda q: list(CANDIDATES)
    result = main.retrieve_for_ask("查询")
    check("默认关闭时 /ask 保持原始检索顺序",
          [r["source"] for r in result] == ["a.txt", "b.txt", "c.txt"])

main.retrieve_chunks = original_custom

print("ALL TESTS COMPLETED")
