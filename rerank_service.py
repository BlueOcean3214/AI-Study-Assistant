"""第二阶段重排序（Rerank）：候选文档重排（阿里云百炼 text-rerank 适配）。

流程（深度 RAG）：
    初始召回（向量 / 混合）→ 候选集 → Rerank 重排 → Top-N → 组上下文 → LLM 回答

配置（独立 Provider，不假设与 LLM/Embedding 同 Base URL）：
- RAG_RERANK_ENABLED   1 开启重排（默认 0）
- RERANK_PROVIDER      openai_compatible（阿里云百炼 DashScope rerank 端点）
- RERANK_BASE_URL      默认 https://dashscope.aliyuncs.com（以控制台/地域为准）
- RERANK_API_KEY       必填（独立密钥；不读取 LLM_API_KEY/EMBEDDING_API_KEY）
- RERANK_MODEL         默认 qwen3-rerank（示例；实际可用型号以控制台为准）
- RERANK_TOP_N         重排后保留条数（默认 3）
- RERANK_TIMEOUT       超时秒数（默认 20）
- RERANK_FALLBACK      1（默认）重排失败时回退未重排结果并明确标注；0 直接报错

约束：
- Rerank 不替代 Embedding；原始检索分数（向量余弦 / RRF 融合分）与 rerank 分数
  是不同量纲，结果中分字段保留（score / rerank_score），绝不混用
- 失败绝不静默假装成功：日志与返回值明确标注 rerank_applied=False 与失败原因
- **真实百炼 rerank API 尚未用真实 Key 实测**（请求格式按公开文档实现，见 README）
"""

import json
import os
import time

import env_loader
import requests

env_loader.load_env()


DEFAULT_RERANK_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_RERANK_MODEL = "qwen3-rerank"
DEFAULT_RERANK_TOP_N = 3
DEFAULT_RERANK_TIMEOUT = 20


class RerankError(RuntimeError):
    """重排调用失败（启用且不允许回退时抛出）。"""


def _env(name, default=None):
    value = (os.getenv(name) or "").strip()

    return value or default


def is_enabled():
    return (_env("RAG_RERANK_ENABLED") or "0").lower() in {"1", "true", "on", "yes"}


def fallback_allowed():
    return (_env("RERANK_FALLBACK") or "1").lower() not in {"0", "false", "off", "no"}


def get_settings():
    api_key = _env("RERANK_API_KEY")

    if not api_key:
        raise RerankError("启用 Rerank 必须设置 RERANK_API_KEY（独立密钥，不要写进代码）")

    return {
        "provider": (_env("RERANK_PROVIDER") or "openai_compatible").lower(),
        "base_url": (_env("RERANK_BASE_URL") or DEFAULT_RERANK_BASE_URL).rstrip("/"),
        "api_key": api_key,
        "model": _env("RERANK_MODEL") or DEFAULT_RERANK_MODEL,
        "top_n": max(1, min(int(_env("RERANK_TOP_N") or DEFAULT_RERANK_TOP_N), 20)),
        "timeout": max(1, int(_env("RERANK_TIMEOUT") or DEFAULT_RERANK_TIMEOUT)),
    }


def _call_bailian_rerank(query, documents, settings):
    """百炼 DashScope text-rerank 端点（非 OpenAI 格式，按公开文档实现）。

    POST {base}/api/v1/services/rerank/text-rerank/text-rerank
    请求：{"model", "query", "documents": [...], "top_n", "return_documents": false}
    响应：{"output": {"results": [{"index", "relevance_score"}]}}
    """

    url = settings["base_url"] + "/api/v1/services/rerank/text-rerank/text-rerank"

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings["model"],
            "query": query,
            "documents": documents,
            "top_n": settings["top_n"],
            "return_documents": False,
        },
        timeout=settings["timeout"],
    )
    response.raise_for_status()
    payload = response.json()

    results = (payload.get("output") or {}).get("results")

    if not isinstance(results, list):
        raise RerankError("rerank 响应格式异常（缺少 output.results）")

    return results


def rerank(query, candidates, top_n=None):
    """对候选片段重排。

    candidates: [{"content", "source", "score", ...}]（原始检索结果，score 为原始检索分）
    返回：
      {"ok": True, "rerank_applied": True, "results": [...]}   成功（含 rerank_score）
      {"ok": True, "rerank_applied": False, "results": 原始候选, "reason": ...}  回退
      {"ok": False, "error_type": ..., "error": ...}            不允许回退时的失败
    """

    if not isinstance(query, str) or not query.strip() or not candidates:
        return {"ok": True, "rerank_applied": False, "results": list(candidates or []),
                "reason": "无候选或查询为空"}

    try:
        settings = get_settings()
    except RerankError as error:
        if fallback_allowed():
            print(f"[rerank] 配置不可用，回退未重排结果：{error}")
            return {"ok": True, "rerank_applied": False, "results": list(candidates),
                    "reason": str(error)}
        return {"ok": False, "error_type": "rerank_config_error", "error": str(error)}

    documents = [item.get("content") or "" for item in candidates]
    started = time.perf_counter()

    try:
        raw_results = _call_bailian_rerank(query, documents, settings)
    except requests.exceptions.Timeout:
        failure = ("timeout", f"rerank 请求超时（{settings['timeout']} 秒）")
    except requests.exceptions.HTTPError as error:
        status = error.response.status_code if error.response is not None else "?"
        failure = ("rerank_http_error", f"rerank 返回 HTTP 错误（status={status}）")
    except requests.exceptions.RequestException as error:
        failure = ("rerank_connection_error", f"无法连接 rerank 接口（{type(error).__name__}）")
    except RerankError as error:
        failure = ("rerank_response_error", str(error))
    else:
        failure = None

    if failure is not None:
        error_type, message = failure
        print(f"[rerank] 重排失败（{error_type}）：{message}")

        if fallback_allowed():
            return {"ok": True, "rerank_applied": False, "results": list(candidates),
                    "reason": message}
        return {"ok": False, "error_type": error_type, "error": message}

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    # 按 relevance_score 降序重排；保留原始 source / 原始 score / rerank_score
    reranked = []

    for item in raw_results:
        index = item.get("index")

        if not isinstance(index, int) or not (0 <= index < len(candidates)):
            continue

        original = dict(candidates[index])
        original["rerank_score"] = item.get("relevance_score")
        reranked.append(original)

    reranked.sort(key=lambda item: (item.get("rerank_score") is not None, item.get("rerank_score")), reverse=True)
    reranked = reranked[: settings["top_n"]]

    print(f"[rerank] 重排完成 model={settings['model']} candidates={len(candidates)} "
          f"kept={len(reranked)} {elapsed_ms}ms")

    return {"ok": True, "rerank_applied": True, "results": reranked,
            "model": settings["model"], "elapsed_ms": elapsed_ms}
