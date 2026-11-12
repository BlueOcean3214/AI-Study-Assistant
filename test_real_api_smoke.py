r"""真实 API 冒烟测试（任务书 B 组）：真实云端对话 + 真实 Embedding。

原则
----
- 这套用例**必须**打到真实云端服务商，且两个后端都必须是 `openai_compatible`：
  本机 Ollama 不计入本次 API 化验收，遇到 provider=ollama 一律 SKIP 并说明原因。
- 没有配置后端时打印 [SKIP] 并以退出码 0 退出，绝不静默通过，也不用 Mock 冒充真实 API。
- 与 A 组（Stub/Mock 确定性测试）分开跑：确定性用例验证逻辑，本文件验证真实通路。
- 控制请求量：对话 4 次、Embedding 3 次，全部是短请求。
- 输出只含 provider / model / base_url，绝不打印 API Key。

运行示例（PowerShell）：

    $env:LLM_PROVIDER="openai_compatible"
    $env:LLM_BASE_URL="https://<服务商>/v1"
    $env:LLM_MODEL="<对话模型名>"
    $env:LLM_API_KEY="<Key>"            # 只从环境变量读，不写进代码/日志
    $env:EMBEDDING_PROVIDER="openai_compatible"
    $env:EMBEDDING_BASE_URL="https://<服务商>/v1"
    $env:EMBEDDING_MODEL="<Embedding 模型名>"
    $env:EMBEDDING_API_KEY="<Key>"
    .\.venv\Scripts\python.exe test_real_api_smoke.py

退出码：0 = 全通过或显式 SKIP；1 = 真实 API 调用出现失败（必须人工看输出）。
"""

import json
import math
import os

import embedding_service
import llm_client
import rag_service
import real_backend

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"PASS: {name} {detail}")
    else:
        FAILED.append(name)
        print(f"FAIL: {name} {detail}")


def info(name, detail=""):
    print(f"INFO: {name} {detail}")


def cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))

    return dot / norm if norm else 0.0


def main():
    # 严格云端口径：provider 必须是 openai_compatible，本地 Ollama 一律 SKIP。
    real_backend.require_cloud("真实 API 冒烟测试")

    chat_settings = llm_client.get_settings()
    embedding_settings = embedding_service.get_settings()

    info("对话后端", chat_settings.describe())
    info("Embedding 后端", embedding_settings.describe())

    # ---------- 1. 配置 / Provider 口径 / 密钥不外泄 ----------

    check(
        "对话 Provider 是云端 openai_compatible",
        chat_settings.provider == llm_client.PROVIDER_OPENAI,
        f"provider={chat_settings.provider}",
    )
    check(
        "Embedding Provider 是云端 openai_compatible",
        embedding_settings.provider == embedding_service.PROVIDER_OPENAI,
        f"provider={embedding_settings.provider}",
    )

    chat_url = str(chat_settings.chat_url or "")
    embedding_url = str(embedding_settings.base_url or "")

    check(
        "对话 base_url 不是本机地址",
        "localhost" not in chat_url and "127.0.0.1" not in chat_url,
        f"url={chat_url}",
    )
    check(
        "Embedding base_url 不是本机地址",
        "localhost" not in embedding_url and "127.0.0.1" not in embedding_url,
        f"url={embedding_url}",
    )

    check("对话配置含模型名", bool(chat_settings.model))
    check(
        "对话配置描述不含 Key",
        bool(chat_settings.api_key)
        and chat_settings.api_key not in chat_settings.describe(),
    )
    check(
        "Embedding 配置描述不含 Key",
        bool(embedding_settings.api_key)
        and embedding_settings.api_key not in embedding_settings.describe(),
    )

    # ---------- 2. 普通对话 ----------

    result, error = llm_client.chat([
        {"role": "user", "content": "只回复两个字：收到"},
    ])

    check("普通对话返回结果", result is not None, f"error={error}")
    answer = (result or {}).get("message", {}).get("content") if result else None
    check("普通对话文本非空", bool(answer and str(answer).strip()), f"answer={str(answer)[:60]}")
    check("普通对话带 usage", bool((result or {}).get("usage")))

    # ---------- 3. JSON 输出 ----------

    result, error = llm_client.chat([
        {"role": "system", "content": "你只输出 JSON，不要任何解释文字。"},
        {"role": "user", "content": '输出 {"ok": true, "items": [1, 2, 3]} 这个 JSON 本身。'},
    ])

    check("JSON 输出返回结果", result is not None, f"error={error}")
    raw = (result or {}).get("message", {}).get("content") or ""
    parsed = None

    try:
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start:end + 1]) if start >= 0 and end > start else None
    except json.JSONDecodeError:
        parsed = None

    check(
        "JSON 输出可解析",
        isinstance(parsed, dict) and parsed.get("ok") is True and parsed.get("items") == [1, 2, 3],
        f"raw={raw[:120]}",
    )

    # ---------- 4. Tool Calling ----------

    tools = [{
        "type": "function",
        "function": {
            "name": "get_learning_minutes",
            "description": "查询某个知识点今天已经学习的分钟数",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string", "description": "知识点名称"}},
                "required": ["topic"],
            },
        },
    }]

    messages = [{
        "role": "user",
        "content": "帮我查一下『定积分』今天已经学了多少分钟，请直接调用工具。",
    }]

    result, error = llm_client.chat(messages, tools=tools)

    check("Tool Calling 返回结果", result is not None, f"error={error}")
    assistant = (result or {}).get("message") or {}
    tool_calls = assistant.get("tool_calls") or []

    check("Tool Calling 产生工具调用", bool(tool_calls), f"message={str(assistant)[:160]}")

    if tool_calls:
        call = tool_calls[0]
        function = call.get("function") or {}

        check("工具名正确", function.get("name") == "get_learning_minutes",
              f"name={function.get('name')}")
        check(
            "工具参数已解析为对象且含 topic",
            isinstance(function.get("arguments"), dict)
            and "topic" in function["arguments"],
            f"arguments={function.get('arguments')}",
        )

        followup = messages + [
            {"role": "assistant", "content": assistant.get("content") or "",
             "tool_calls": tool_calls},
            {"role": "tool", "tool_call_id": call.get("id"),
             "content": json.dumps({"minutes": 25}, ensure_ascii=False)},
        ]

        result2, error2 = llm_client.chat(followup, tools=tools)
        check("工具结果回填后仍能返回文本", result2 is not None, f"error={error2}")

    # ---------- 5. Embedding 单条 / 批量 / 维度 ----------

    vector = embedding_service.get_embedding("定积分是微积分的重要内容")

    check("单条 Embedding 维度正确",
          len(vector) == embedding_settings.dimensions,
          f"len={len(vector)} expect={embedding_settings.dimensions}")
    check("单条 Embedding 非全零", any(abs(value) > 0 for value in vector))

    batch = embedding_service.get_embeddings(["定积分", "条件概率", "二叉树"])
    check("批量 Embedding 数量一致", len(batch) == 3, f"n={len(batch)}")
    check("批量 Embedding 维度一致",
          all(len(item) == embedding_settings.dimensions for item in batch))

    repeat = embedding_service.get_embedding("定积分是微积分的重要内容")
    check("同一文本两次 Embedding 一致", abs(cosine(vector, repeat) - 1.0) < 0.01,
          f"cos={cosine(vector, repeat):.6f}")

    # ---------- 6. 文档与查询向量同模型 ----------

    identity = embedding_service.cache_identity()
    check("缓存身份包含真实模型名", embedding_settings.model in identity, f"identity={identity}")
    check("缓存身份包含维度", str(embedding_settings.dimensions) in identity, f"identity={identity}")

    # ---------- 7. 真实 RAG 检索（文档与查询都走同一 Embedding） ----------

    chunks = rag_service.retrieve_chunks("定积分怎么学")

    check("真实 RAG 检索有结果", bool(chunks), f"n={len(chunks)}")
    check("真实 RAG 结果含关键词",
          any("定积分" in chunk["content"] for chunk in chunks),
          f"sources={[chunk['source'] for chunk in chunks]}")
    check("真实 RAG 分数不低于 MIN_SCORE",
          all(chunk["score"] >= rag_service.MIN_SCORE for chunk in chunks),
          f"scores={[chunk['score'] for chunk in chunks]}")
    info("真实 RAG 分数", str([round(chunk["score"], 4) for chunk in chunks]))

    # 语义召回：查询与语料没有相同字词，只能靠真实 Embedding 的语义能力
    # （打桩环境下这条用例无法成立，所以从 test_rag_service.py 迁到这里）
    semantic = rag_service.retrieve_chunks("数学怎么学")
    check("语义相关查询可召回（真实 Embedding）", bool(semantic), f"n={len(semantic)}")

    unrelated = rag_service.retrieve_chunks("红烧肉怎么做")
    check("无关问题不误召回", unrelated == [],
          f"sources={[chunk['source'] for chunk in unrelated]}")

    # ---------- 8. 失败必须显式报错，不静默回退 ----------

    saved_key = os.environ.get("LLM_API_KEY")
    saved_provider = os.environ.get("LLM_PROVIDER")

    try:
        os.environ["LLM_PROVIDER"] = llm_client.PROVIDER_OPENAI
        os.environ["LLM_API_KEY"] = ""
        llm_client.reset_settings()

        _, error = llm_client.chat([{"role": "user", "content": "hi"}])
        detail = str(error)

        check("缺少 Key 时 chat 显式报错", bool(error), f"error={detail[:120]}")
        check("错误信息提示配置问题", "配置" in detail or "API_KEY" in detail, f"error={detail[:120]}")
        check("错误信息不含任何 Key", not (saved_key and saved_key in detail))
    finally:
        if saved_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = saved_key

        if saved_provider is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = saved_provider

        llm_client.reset_settings()

    print(f"\n真实 API 冒烟：PASS {len(PASSED)} / FAIL {len(FAILED)}")

    if FAILED:
        print("失败用例：" + "；".join(FAILED))
        return 1

    print("全部真实 API 用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
