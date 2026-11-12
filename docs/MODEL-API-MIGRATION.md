# 模型全面 API 化：对话与 Embedding 配置、调用架构与迁移说明

本文说明本项目如何把**对话模型**与 **Embedding 模型**全部切到云端 API（OpenAI 兼容协议），
以及切换后本地 Ollama / bge-m3 为何不再是运行依赖、旧向量缓存如何处理、失败如何暴露。

> 结论先行：代码里的默认配置本来就是云端 `openai_compatible`，本轮工作是把
> **测试与文档**一起迁移过来，让「不装 Ollama」成为可运行、可验收的状态；
> 业务代码没有新增 provider，也没有加入任何「API 失败时回退本地模型」的逻辑。

---

## 1. 对话 API 怎么配

对话模型统一由 `llm_client.py` 读取配置，AI 问答、Agent Tool Calling、Native 计划起草、
LangGraph 计划工作流全部走同一个入口，因此**只配一处**即可。

| 环境变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | 否 | `openai_compatible`（默认）或 `ollama` | `openai_compatible` |
| `LLM_BASE_URL` | 是（云端） | 服务商 OpenAI 兼容地址，例如 `https://<服务商>/v1` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_API_KEY` | 是（云端） | 服务商 Key，**只从环境变量读，绝不写进代码/README/日志** | 无 |
| `LLM_MODEL` | 否 | 对话模型名，以服务商控制台实际可用为准 | 见 `llm_client.DEFAULT_MODEL` |

配置读取点：`llm_client.py:163` `get_settings()`。

- 云端模式下缺 `LLM_API_KEY` 会抛 `LLMConfigError`，错误文案明确提示「不要把密钥写进代码」，
  不会静默降级到本地模型。
- `LLM_BASE_URL` 只服务 `openai_compatible`，避免 `.env` 里的云端地址污染本地 Ollama 配置。
- 若确实要用本地 Ollama：`LLM_PROVIDER=ollama` + `LLM_MODEL=qwen3.5:4b`（需先 `ollama serve`）。

## 2. Embedding API 怎么配

Embedding 统一由 `embedding_service.py` 读取配置，文档入库与查询检索用的是同一份配置、
同一个模型，这是「文档向量与查询向量必须同模型」的实现保证。

| 环境变量 | 必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `EMBEDDING_PROVIDER` | 否 | `openai_compatible`（默认）或 `ollama` | `openai_compatible` |
| `EMBEDDING_BASE_URL` | 是（云端） | OpenAI 兼容地址 | 见 `embedding_service.DEFAULT_BASE_URL` |
| `EMBEDDING_API_KEY` | 是（云端） | 服务商 Key | 无 |
| `EMBEDDING_MODEL` | 否 | Embedding 模型名 | `text-embedding-v4` |
| `EMBEDDING_DIMENSIONS` | 否 | 向量维度，必须与服务商返回一致 | `1024` |

配置读取点：`embedding_service.py:135` `get_settings()`。

- 维度以 `EMBEDDING_DIMENSIONS` 为准（默认 1024），响应维度不符会报错，不会截断/补齐。
- 批量大小：云端 `EMBEDDING_BATCH_SIZE` 默认 10，Ollama 默认 32（见 `DEFAULT_BATCH_SIZES`）。

## 3. 服务商与模型名从哪里读

| 用途 | 读取函数 | 备注 |
| --- | --- | --- |
| 对话 | `llm_client.get_settings()` | 返回 `provider/model/chat_url/api_key/timeouts`，`describe()` 打日志时不含 Key |
| Embedding | `embedding_service.get_settings()` | 返回 `provider/model/base_url/dimensions/api_key` |
| 向量缓存身份 | `embedding_service.cache_identity()` | `"{provider}:{model}@{dimensions}"`，用于判定缓存是否可用 |
| 服务状态 | `GET /health`（`main.py:160/187`） | 输出对话与 Embedding 的 provider/model/维度 |

## 4. 环境变量一览

```text
# 对话（Agent Tool Calling / Native / LangGraph 共用）
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://<服务商>/v1
LLM_MODEL=<对话模型名>
LLM_API_KEY=<只放环境变量>

# Embedding（文档入库与查询共用）
EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_BASE_URL=https://<服务商>/v1
EMBEDDING_MODEL=<Embedding 模型名>
EMBEDDING_DIMENSIONS=1024
EMBEDDING_API_KEY=<只放环境变量>
```

启动前自检（不打印 Key）：

```powershell
.\.venv\Scripts\python.exe -c "import llm_client, embedding_service; print(llm_client.get_settings().describe()); print(embedding_service.get_settings().describe())"
```

## 5. 调用架构

```text
用户消息
  -> ai_service.py:17            /ask、流式问答
  -> llm_client.chat / chat_stream
       - provider=openai_compatible -> _stream_openai / chat via OpenAI 兼容 HTTP
       - provider=ollama            -> _stream_ollama
       - 单路分派：provider 选定后不会跨 provider 回退

Agent 工具调用
  -> agent_loop.py:136           llm_client.chat(tools=...)
  -> agent_plan_draft.py:661     Native 计划起草（chat_with_tools）
  -> langgraph_plan_workflow.py:149  LangGraph 计划工作流（同一入口）

知识库检索（RAG）
  -> rag_service.py:41 from embedding_service import get_embedding, get_embeddings
       文档切片 -> embedding_service.get_embeddings（批量）
       查询    -> embedding_service.get_embedding（单条，同模型）
  -> rag_service.py:154/237   向量缓存（vector_cache）与混合检索
```

关键性质：

1. **同一套对话配置**服务 Tool Calling / Native / LangGraph——它们调用的是同一个
   `llm_client.chat`，不存在各自一套模型设置。
2. **文档与查询同一 Embedding 模型**——两处都调用 `embedding_service`，且缓存身份里带模型名与维度。
3. **没有跨 provider 回退**——`llm_client.py:792/1033` 只按 `settings.provider` 分派；
   Embedding 同理（`embedding_service.py:457`）。API 失败时返回稳定错误类型，由上层决定报错或降级。

## 6. Embedding 维度

- 默认 `EMBEDDING_DIMENSIONS=1024`（`embedding_service.py:72` `DEFAULT_DIMENSIONS`）。
- 维度写入向量缓存元数据；与当前配置不一致的缓存会被判废重建，不会被复用。
- 换服务商/换模型/改维度都必须重建缓存（见下节）。

## 7. 旧向量缓存（bge-m3）怎么迁移

`vector_cache` 的元数据里保存 `model = embedding_service.cache_identity()`，
即 `"{provider}:{model}@{dimensions}"`，例如：

```text
ollama:bge-m3@1024          # 旧：本地 bge-m3
openai_compatible:text-embedding-v4@1024   # 新：云端 API
```

因此：

- 切到云端后，旧的 `ollama:bge-m3@1024` 缓存**自动判废**，不会与新区向量混用。
- 首次检索会按新模型重新生成向量并写回缓存（耗时与文档量成正比）。
- 想强制全量重建：停服后删除 `.rag_cache/` 目录即可（不涉及数据库）。
- 注意 `rag_service.py:67` 的 `OLLAMA_TAGS_URL` 只在 `provider=ollama` 时才访问
  （`rag_service.py:102` `current_model_digest()` 取权重 digest 判断模型是否被就地更新）；
  云端模式返回 `None`，**不会**因为探测 Ollama 而失败。

## 8. 失败处理

| 场景 | 行为 |
| --- | --- |
| 缺 `LLM_API_KEY` / `EMBEDDING_API_KEY` | 配置阶段抛 `LLMConfigError` / `EmbeddingConfigError`，`/health` 与启动自检可见 |
| 对话 API 超时/401/5xx/限流 | `llm_client.chat` 返回 `(None, error)`，`error_type` 由 `cloud_errors.py` 分类；上层报错或降级 |
| Embedding API 失败 | 抛 `EmbeddingError`；`rag_service.retrieve_chunks` 记录日志并返回空列表（`/ask` 仍回答，只是没有资料） |
| 工具调用参数不合法 | 由 `agent_dispatcher` / 工具层返回结构化失败，不写库 |
| 任何失败 | **不会**静默回退到本地 Ollama 或 bge-m3 |

## 9. 怎么跑测试

A 组：确定性测试（Stub/Mock，不需要模型服务，也不需要 API Key）

```powershell
.\.venv\Scripts\python.exe test_embedding_cloud.py     # 假 HTTP：配置/请求组装/解析/维度/超时/禁止回退
.\.venv\Scripts\python.exe test_embedding.py           # Embedding 契约（打桩 A 节；真实后端 B 节无后端则 SKIP）
.\.venv\Scripts\python.exe test_openai.py              # 对话契约（假流式分块 A 节；真实 JSON 输出 B 节无后端则 SKIP）
.\.venv\Scripts\python.exe test_vector_cache.py        # 缓存失效与重建（Embedding 打桩）
.\.venv\Scripts\python.exe test_embedding_batching.py  # 批量与降级（Embedding 打桩）
.\.venv\Scripts\python.exe test_rag_service.py         # RAG 契约与降级（Embedding 打桩）
.\.venv\Scripts\python.exe test_upload.py              # 上传即检索（子进程跑 stub_app，测试进程自装 Embedding 打桩）
.\.venv\Scripts\python.exe test_agent_loop.py          # Agent 闭环（LLM 打桩；真实模型节显式 SKIP）
.\.venv\Scripts\python.exe test_agent_plan_draft.py     # 计划起草（同上）
.\.venv\Scripts\python.exe test_save_plan_tool.py       # save_plan 确认链路（同上）
.\.venv\Scripts\python.exe test_plan_frontend_flow.py   # 前端流程（同上）
```

Stage 2 记忆链路的确定性回归（同样不需要模型服务）：

```powershell
.\.venv\Scripts\python.exe test_memory_context.py
.\.venv\Scripts\python.exe test_memory_grounding.py
.\.venv\Scripts\python.exe test_learning_memory_tool.py
.\.venv\Scripts\python.exe test_feedback_memory_trigger.py
.\.venv\Scripts\python.exe test_langgraph_plan.py
```

- `stub_embedding.py` 是测试夹具：确定性词面 + 主题词表向量，**不具备语义能力**，
  只用于验证通路/缓存/降级；语义召回质量必须在真实后端上测。
- 真实模型小节需要可用的对话后端，没有时打印 `[SKIP] ... 未执行：没有可用的真实对话后端` 并以退出码 0 结束，
  不会假装通过，也不会偷偷连本机 Ollama（判定见 `real_backend.py`）。

B 组：真实 API 冒烟（会消耗少量额度）

```powershell
.\.venv\Scripts\python.exe test_real_api_smoke.py
```

覆盖：普通对话、JSON 输出、Tool Calling（含工具结果回填）、Embedding 单条/批量/维度一致性、
文档与查询同模型、真实 RAG 检索、语义召回、无关问题不误召回、缺 Key 时显式报错且不回退本地。
没有配置后端时打印 `[SKIP]` 并以退出码 0 退出——这不是通过，配置好后端必须重跑。

## 10. 如何启动

```powershell
# 1) 设置环境变量（示例，见第 4 节；Key 只放环境变量）
$env:LLM_PROVIDER="openai_compatible"; $env:LLM_BASE_URL="https://<服务商>/v1"
$env:LLM_MODEL="<对话模型名>"; $env:LLM_API_KEY="<Key>"
$env:EMBEDDING_PROVIDER="openai_compatible"; $env:EMBEDDING_BASE_URL="https://<服务商>/v1"
$env:EMBEDDING_MODEL="<Embedding 模型名>"; $env:EMBEDDING_DIMENSIONS="1024"
$env:EMBEDDING_API_KEY="<Key>"

# 2) 自检
.\.venv\Scripts\python.exe -c "import main; main.health(); "   # 或启动后 GET /health

# 3) 启动
.\.venv\Scripts\uvicorn.exe main:app --host 127.0.0.1 --port 8000
```

首次启动后第一次知识库检索会重建向量缓存（`.rag_cache/`），属正常现象。
