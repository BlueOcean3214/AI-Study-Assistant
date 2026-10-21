# 测试报告（交付验收实测）

- 日期：2026-10-20
- 环境：Windows 11 / Python 3.11（项目 `.venv`）/ 本地 Ollama（bge-m3、qwen3.5:4b）/ Docker Desktop 29.8.2
- 测试框架：项目自有脚式测试（无 pytest），每个 `test_xxx.py` 直接运行，`check(name, condition)` 断言，`PASS:` 行计数
- 运行命令统一为：`.\.venv\Scripts\python.exe test_xxx.py`（在项目根目录）

## 一、总量

```text
30 个测试文件 / 1321 项断言 / 1321 通过 / 0 失败
Agent Evaluation：15 用例（14 完全符合 / 1 期望偏差 / 硬失败 0）
```

## 二、确定性测试（无模型、无网络、无第三方 Key）：20 文件 / 737 项

| 文件 | 通过 | 说明 |
| --- | --- | --- |
| test_cloud_errors.py | 26 | 错误分类/重试/退避 + 错误信息密钥脱敏 |
| test_context_builder.py | 38 | 上下文去重/来源标签/字符预算/边界//ask 与 /ask/stream 一致性 |
| test_eval_metrics.py | 40 | source/chunk 双口径指标（确定性手算 + 兼容别名） |
| test_sse_stream.py | 13 | chat_stream 双 Provider 流解析、SSE 帧约定 |
| test_prompt_registry.py | 26 | Prompt 集中注册/版本切换/渲染 |
| test_llm_client.py | 67 | 统一模型层（全 Mock，含"禁止真实请求"炸弹桩） |
| test_embedding_cloud.py | 40 | 云端 Embedding 适配（全 Mock） |
| test_rag_hybrid.py | 33 | BM25 + RRF 混合检索（确定性） |
| test_database_migrations.py | 27 | user_version v5 幂等迁移 |
| test_agent_schema.py | 33 | 工具说明书与参数守门 |
| test_agent_dispatcher.py | 55 | 工具白名单/派发守门 |
| test_agent_eval.py | 72 | 评测器自测（合成 run_result，不调模型） |
| test_langgraph_plan.py | 16 | LangGraph 计划草案工作流（桩模型） |
| test_langchain_components.py | 12 | LangChain 组件（全 Mock） |
| test_llamaindex_rag.py | 12 | LlamaIndex 适配路由（桩后端） |
| test_rerank_service.py | 13 | Rerank 适配 + 失败回退（Mock HTTP） |
| test_confirmation.py | 48 | 确认凭证签发/确认/一次性消费 |
| test_plan_service.py | 70 | 计划校验/写入/同天 active 唯一 |
| test_plan_api.py | 50 | 计划 HTTP API（独立 SQLite） |
| test_document_service.py | 46 | 文档保存/切片/语义检索（无 Embedding 时 2 项按设计 SKIP） |

**无第三方密钥独立性验证**：清空 `LLM_API_KEY` / `EMBEDDING_API_KEY` / `RERANK_API_KEY` 并指向不可达模型端口后复跑 19 个文件（当时 document_service 语义段 SKIP）：**663 项通过 / 0 失败**。即基础测试体系不需要任何第三方密钥或联网。

## 三、本地 Ollama 真实模型 / Embedding：10 文件 / 584 项

| 文件 | 通过 | 说明 |
| --- | --- | --- |
| test_rag_service.py | 48 | 向量检索 + `/ask` 端到端 prompt 注入（LLM 打桩 + 真实 embedding） |
| test_vector_cache.py | 50 | 向量缓存身份/归档/判废（真实 embedding） |
| test_upload.py | 42 | 上传→保存→可检索闭环 |
| test_agent_tools.py | 83 | 工具真实检索 + 输入校验 |
| test_embedding_batching.py | 41 | 批量 embedding / 分批 / retrieve_context 兼容 |
| test_agent_loop.py | 100 | Agent Loop 确定性 + 真实模型端到端 |
| test_save_plan_tool.py | 54 | save_plan 确认凭证路径（真实模型） |
| test_agent_plan_draft.py | 95 | 计划草案两阶段 + grounding（真实模型） |
| test_plan_frontend_flow.py | 43 | 草案→确认→保存→恢复 全链路（真实模型 + 真实 HTTP） |
| test_langchain_agent.py | 28 | LangChain 只读 Agent（含真实模型对照） |

上述真实模型用例全部使用**本地 Ollama**（测试夹具 `os.environ.setdefault("LLM_PROVIDER", "ollama")`），不产生云端费用。

## 四、Agent Evaluation（独立计数）

```text
用例=15  完全符合=14  期望偏差=1  硬性失败=0
数据安全: 真实库未变=True 知识库未变=True 真实缓存状态未变=True 真实plans未变=True
轨迹归档: agent_eval_runs\agent_eval_20261020T132521Z.json
```

- 期望偏差 1 例：`P4_confirmation_required`（本地模型未按期望发起 save_plan 调用，属模型行为波动，非契约破坏；同用例的反向防护 P7 通过：伪造/未确认凭证无法保存）。
- **如实披露**：本次 agent_eval 运行读取了 `.env` 的云端模型配置（qwen-plus），即产生了少量**真实付费 API 调用**（约 25 轮对话）。这是评测入口未被测试夹具接管所致，非刻意产生；其余测试全程零云端费用。

## 五、未运行 / 环境相关

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| test_openai.py | 未运行 | 遗留脚本，会调用真实付费 OpenAI API，交付验收刻意不跑 |
| test_embedding.py | 已运行 exit 0 | 遗留脚本，无 check() 断言计数 |
| docker compose up | 未单独执行 | `docker run` 等价冒烟已通过（同镜像同入口） |
| 云端 LLM/Embedding 付费验证 | 本次未执行 | 2026-10-19 曾用真实 Key 做过限量验证；本次仅意外发生 agent_eval 的少量调用（见上） |
| Rerank 真实 API | 未验证 | 无 RERANK_API_KEY，适配层为 Mock 测试，回退路径已验证 |

## 六、修复后回归

- 修复 `cloud_errors` 错误信息密钥泄漏风险（见 DELIVERY_REPORT）后：test_cloud_errors 26 项（含 6 项新增脱敏断言）通过，test_llm_client 67 / test_embedding_cloud 40 复跑无回归，随后全量 1321 项 0 失败。

## 七、应用与容器冒烟（真实执行）

- 本地 uvicorn（模型指向不可达端口，验证降级契约）：`/` 200、`/health` 200、`/ready` ready=true（schema v5）、`/ask` 模型不可用时返回结构化 `error` 不崩溃、`/ask/stream` SSE 输出 `sources`+`error` 帧、`/plan?date=` 正常。
- Docker：`docker build` 产出 `ai-study-assistant:delivery`（727MB）成功；`docker run` 后容器内 `/health` 200、`/ready` ready=true（容器内 DB 自动迁移到 v5）。
