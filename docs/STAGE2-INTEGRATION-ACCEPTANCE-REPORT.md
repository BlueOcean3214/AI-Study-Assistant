# Stage 2 主线集成验收报告（模型 API 化迁移后）

- 验收时间：2026-11-12（本机实跑，时区 +08:00）
- 分支：`feat/api-model-provider`
- 本次报告中的每一个数字都来自本次实际执行；没有沿用历史报告的数字，也没有把计划执行写成已执行。

---

## 1. Git 基线

| 项目 | 实际值 |
|---|---|
| 当前分支 | `feat/api-model-provider` |
| 分支 HEAD | `0016c7b5d61597b76dc1fcc91485b00524adf40c` |
| 远端 main | `0016c7b5d61597b76dc1fcc91485b00524adf40c` |
| 与 main 的 merge-base | `0016c7b5d61597b76dc1fcc91485b00524adf40c` |
| PR #6 合并提交是否存在 | 存在；`0016c7b…` 是 `2026-11-12 10:42:59 +0800` 的 `Merge PR #6: feat(memory): Stage 2 Batch C Agent 接入与上下文集成` |
| 本次验收所用代码 | 分支 HEAD + 本分支工作区改动（迁移改动尚未提交，见第 7 节） |

说明：分支从 PR #6 合并后的 main 拉出，验收期间未修改 main、未执行 `git reset --hard` / `git clean`、未丢弃任何用户文件。

## 2. API 迁移结果

| 项目 | 迁移前 | 迁移后 | 验证结果 |
|---|---|---|---|
| 对话模型 | 本地 Ollama（`/api/chat`） | 云端 OpenAI 兼容 API（`LLM_PROVIDER=openai_compatible` 为默认） | 代码层已切换：默认 Provider 为 `openai_compatible`，缺 `LLM_API_KEY` 直接抛 `LLMConfigError`，不回退本地。真实 API 调用因缺配置**未执行**（见 7. P2-1） |
| Native Agent | 本地 Ollama | 同一套对话配置（`llm_client`） | `test_agent_plan_draft.py` 75 PASS：起草前的研究与起草调用都经 `llm_client`，打桩验证 |
| LangGraph | 本地 Ollama | 同一套对话配置（`llm_client`） | `test_langgraph_plan.py` 16 PASS + `test_langchain_agent.py` 28 PASS：两条链路共用同一 chat 入口 |
| 文档 Embedding | 本地 bge-m3 | 云端 Embedding API（`EMBEDDING_PROVIDER=openai_compatible` 为默认） | 代码层已切换；打桩验证通路；真实 Embedding **未执行** |
| 查询 Embedding | 本地 bge-m3 | 同一 Embedding 配置、同一模型 | `test_embedding.py` / `test_rag_service.py` 验证「文档与查询同一 `cache_identity()`」，不允许混模型 |
| RAG 检索向量 | 旧 bge-m3 向量 | 新 API 模型向量（缓存身份 `provider:model@dim`） | `test_vector_cache.py` 50 PASS：模型/维度变更即缓存失效并重建；正式 `.rag_cache/` 当前为空，**不存在旧向量混用** |

迁移过程中的关键代码事实（已逐条核对源码）：

- `llm_client.py:163` 附近：Provider 默认 `openai_compatible`；`chat()` / `chat_stream()` 只按 `settings.provider` 单路分派（`llm_client.py:792`、`llm_client.py:1033`），**没有跨 Provider 的自动回退分支**。
- `embedding_service.py:143` 附近：Embedding Provider 默认云端，`DEFAULT_DIMENSIONS = 1024`，`cache_identity() = provider:model@dim`；缺 `EMBEDDING_API_KEY` 抛 `EmbeddingConfigError`。
- 失败处理：对话/Embedding 失败都返回稳定 `error_type`（`cloud_errors.py` 分类），不做本地兜底。

## 3. Embedding 迁移

| 项目 | 实际值 |
|---|---|
| 新 Embedding 模型 | 当前已明确要求对话模型和 Embedding 模型均使用云端 API，**具体服务商、模型名称、Base URL 和 Embedding 维度需以实际配置为准**。代码内置默认值 `text-embedding-v4` / `dashscope.aliyuncs.com/compatible-mode/v1` 仅为示例，不代表用户已确认的实际配置 |
| 期望维度 | `EMBEDDING_DIMENSIONS` 默认 `1024`；运行时对每条返回向量强制校验维度，不一致即 `EmbeddingError` |
| 原始文档数量 | 2 篇（`knowledge/math.txt` 480B、`knowledge/条件概率.txt` 136B） |
| 文本块数量 | 7 个（`rag_service.load_chunks()`，`CHUNK_MAX_CHARS=400`，本次实测） |
| 实际用新模型生成的向量数量 | **0**（真实 Embedding API 未配置，未执行） |
| 缓存是否重建 | 正式缓存未生成：`.rag_cache/` 目录当前不存在。缓存重建/失效逻辑由打桩后端验证通过 |
| 旧向量混用风险 | 无。缓存身份含 provider + model + dim，任何一项变化都会让旧条目失效；且当前没有任何旧 bge-m3 向量残留 |

补充：`test_vector_cache.py`（50 PASS）覆盖「换模型后旧缓存不命中、按新身份重建、维度不一致拒绝写入」；`test_rag_service.py`（47 PASS，1 SKIP）覆盖「文档向量与查询向量必须同一身份」。

## 4. 测试结果

执行方式：`python test_xxx.py`（仓库无 pytest，也未配置 GitHub Actions / CI checks，因此**本报告全部为本机实跑结果，没有任何 CI 结果**）。

全量跑法：遍历仓库全部 41 个 `test_*.py`，逐个执行并记录退出码。汇总：**脚本总数 41，exit=0 的 41，exit!=0 的 0**。

| 测试文件 | 结果 | PASS | FAIL | SKIP | 退出码 | 后端类型 | 说明 |
|---|---|---:|---:|---:|---|---|---|
| test_agent_dispatcher.py | PASS | 56 | 0 | 0 | 0 | Stub | 工具白名单/参数规则 |
| test_agent_eval.py | PASS | 72 | 0 | 0 | 0 | Stub | Agent 评测 |
| test_agent_loop.py | PASS | 75 | 0 | 1 | 0 | Stub | 真实模型节 SKIP（无可用的真实对话后端） |
| test_agent_plan_draft.py | PASS | 75 | 0 | 1 | 0 | Stub | 同上 |
| test_agent_schema.py | PASS | 37 | 0 | 0 | 0 | Stub | Schema 与工具面一致 |
| test_agent_tools.py | PASS | 83 | 0 | 0 | 0 | Stub | 四个只读工具 + save_plan |
| test_cloud_errors.py | PASS | 26 | 0 | 0 | 0 | 假 HTTP | 错误分类 |
| test_confirmation.py | PASS | 48 | 0 | 0 | 0 | Stub | 计划确认 |
| test_context_builder.py | PASS | 38 | 0 | 0 | 0 | Stub | 上下文预算 |
| test_database_migrations.py | PASS | 31 | 0 | 0 | 0 | 独立临时库 | 迁移幂等 |
| test_document_service.py | PASS | 44 | 0 | 1 | 0 | Stub | SKIP：embedding 不可用时的语义检索小节 |
| test_embedding.py | PASS | 10 | 0 | 2 | 0 | 打桩 + 闸门 | B 节真实 Embedding SKIP |
| test_embedding_batching.py | PASS | 41 | 0 | 0 | 0 | 打桩 | 批量/降级 |
| test_embedding_cloud.py | PASS | 40 | 0 | 0 | 0 | 假 HTTP | 配置/组装/解析/维度/超时/禁止回退 |
| test_eval_metrics.py | PASS | 40 | 0 | 0 | 0 | 确定性 | 无模型、无网络 |
| test_feedback_enum_contract.py | PASS | 12 | 0 | 0 | 0 | 独立临时库 | 反馈枚举契约 |
| test_feedback_memory_trigger.py | PASS | 157 | 0 | 0 | 0 | 独立临时库 + 打桩 | **反馈→记忆触发主证据** |
| test_feedback_plan_link.py | PASS | 17 | 0 | 0 | 0 | 独立临时库 | plan_id 关联 |
| test_langchain_agent.py | PASS | 28 | 0 | 0 | 0 | 打桩 | LangChain 适配 |
| test_langchain_components.py | PASS | 12 | 0 | 0 | 0 | 打桩 | 组件层 |
| test_langgraph_plan.py | PASS | 16 | 0 | 0 | 0 | 打桩 | **LangGraph 主链路** |
| test_learning_memory.py | PASS | 126 | 0 | 0 | 0 | 独立临时库 | v16 规则 |
| test_learning_memory_state.py | PASS | 216 | 0 | 0 | 0 | 独立临时库 | **状态机/游标/rebuild 主证据** |
| test_learning_memory_tool.py | PASS | 87 | 0 | 0 | 0 | 独立临时库 | get_learning_memory 只读 |
| test_llamaindex_rag.py | PASS | 12 | 0 | 0 | 0 | 打桩 | LlamaIndex 适配 |
| test_llm_client.py | PASS | 67 | 0 | 0 | 0 | 假 HTTP | 对话请求组装/解析/错误 |
| test_memory_context.py | PASS | 105 | 0 | 0 | 0 | 打桩 | **Native/LangGraph 草案含记忆主证据** |
| test_memory_grounding.py | PASS | 40 | 0 | 0 | 0 | 打桩 | **grounding 主证据** |
| test_openai.py | PASS | 10 | 0 | 1 | 0 | 假流式 + 闸门 | A 节假流式分块；B 节真实 JSON SKIP |
| test_plan_api.py | PASS | 50 | 0 | 0 | 0 | Stub | 计划 API |
| test_plan_frontend_flow.py | PASS | 34 | 0 | 1 | 0 | 子进程服务 | 前端流程；1 项外部依赖 SKIP |
| test_plan_service.py | PASS | 70 | 0 | 0 | 0 | 独立临时库 | 保存/确认 |
| test_prompt_registry.py | PASS | 26 | 0 | 0 | 0 | 确定性 | Prompt 版本 |
| test_rag_hybrid.py | PASS | 33 | 0 | 0 | 0 | 打桩 | 混合检索 |
| test_rag_service.py | PASS | 47 | 0 | 1 | 0 | 打桩 | SKIP：语义召回需真实 Embedding |
| test_real_api_smoke.py | **SKIP** | 0 | 0 | 1 | 0 | **真实 API** | **未执行**：没有可用的真实对话/Embedding 后端 |
| test_rerank_service.py | PASS | 13 | 0 | 0 | 0 | 确定性 | 重排 |
| test_save_plan_tool.py | PASS | 41 | 0 | 1 | 0 | 打桩 | 真实模型端到端节 SKIP |
| test_sse_stream.py | PASS | 13 | 0 | 0 | 0 | 假 HTTP | SSE 分块 |
| test_upload.py | PASS | 34 | 0 | 1 | 0 | 子进程 `stub_app` | 上传即检索；/ask 等模型接口 SKIP |
| test_vector_cache.py | PASS | 50 | 0 | 0 | 0 | 打桩 | 缓存身份失效/重建 |

SKIP 逐条原因（全部是因为没有真实模型后端，不是断言被放宽）：

1. `test_agent_loop.py`、`test_agent_plan_draft.py`、`test_save_plan_tool.py`、`test_openai.py`：`[SKIP] 真实模型场景/JSON 输出验收 未执行：没有可用的真实对话后端`。
2. `test_embedding.py`：`[SKIP] 真实 Embedding 后端未执行`（`real_backend.embedding_ready() == False`）。
3. `test_rag_service.py`：`[SKIP] 语义相关查询可召回（需真实 Embedding 后端，见 test_real_api_smoke.py）`。
4. `test_document_service.py`：`SKIP: 保存文件后的语义检索（embedding 不可用）`。
5. `test_plan_frontend_flow.py`：1 项依赖真实模型的用例跳过（其余 34 项断言照常执行）。
6. `test_upload.py`：`[SKIP] /ask 与依赖模型的接口（/next-plan、/analyze）未执行`；上传、检索、路径安全等 34 项照常断言。
7. `test_real_api_smoke.py`：整份文件 SKIP，`这不是通过；配置好后端后必须重跑本文件`。

**Mock / 打桩与真实 API 严格区分**：上表中「Stub / 打桩 / 假 HTTP / 确定性」都不是真实 API 调用。真实 API 部分本次为 **0 项执行、全部 SKIP**。

## 5. 业务验收

链路：用户学习反馈 → 原始反馈保存 → 长期记忆更新 → 短期上下文收集 → Agent 记忆检索 → 计划生成（Native / LangGraph）→ Grounding 检查。

**第一步 反馈写入与 plan_id 关联**（`test_feedback_memory_trigger.py`，独立临时库）：

```
PASS: 两条反馈都落库 [(1, '极限练习', 'partial'), (2, '极限练习', 'partial')]
PASS: 游标推进到 2 (2,)
PASS: 只产生一条 difficulty 记忆 [(1, 'difficulty', '极限', 2)]
PASS: 证据计 2 条 [(1, 'difficulty', '极限', 2)]
```

`plan_id` 关联由 `test_feedback_plan_link.py`（17 PASS）覆盖：有效 plan_id 正确关联，无效 plan_id 拒绝关联，历史无 plan_id 的反馈不被错误回填。

**第二步 长期记忆形成（门槛 / 证据 / 状态机）**：

```
PASS: 首次单条未达门槛：ok 且没有创建记忆 {'ok': True, 'created': [], 'updated': []}
PASS: 第二条触发记忆更新成功 {'ok': True, 'created': [], 'updated': [{'type': 'difficulty', 'subject': '极限练习'}]}
PASS: created/updated 只含 type+subject（无内部 id）
PASS: 产生一条记忆 [(1, 'difficulty', '极限', '极限', '初步观察：「极限」近期 2 次因难度太高未完成（30 天内）', '{"direction":"failure","failed_count":2,"reason":"任务难度太高","window_days":30}', 2, 1)]
```

`test_learning_memory.py`（126 PASS）/`test_learning_memory_state.py`（216 PASS）覆盖 v16 的阈值、去重、冲突、`strength` 语义（启发式强度，非概率）、`memory_apply_cursor` 推进。

**第三步 长期记忆检索（只读 + 白名单）**：

```
PASS: 工具名集合精确为四个只读工具 + save_plan ['get_learning_memory', 'get_recent_feedback', 'save_plan', 'search_knowledge']
PASS: 记忆 Tool 只读：参数里没有 content/记忆写入字段 ['limit', 'memory_type', 'subject']
PASS: 有效记忆能被 _collect_tool_data 提取 {'count': 1, 'memories': [{'type': 'difficulty', 'subject': '定积分换元', 'content': '定积分换元连续 3 次出错', 'strength': 0.8, 'evidence_count': 3}]}
```

`test_learning_memory_tool.py`（87 PASS）覆盖 limit 上限、字段白名单、不泄漏 `memory_id`/`value_json`、空结果与失败处理。

**第四步 短期上下文预算与不可信数据标记**：

```
PASS: P2 默认预算 1200
PASS: P2 默认渲染不超预算 535
PASS: P2 预算 400：不超预算 391
PASS: P2 预算 400：丢弃顺序是 DROP_ORDER 前缀 ['今日计划', '最近困难']
```

**第五步 最终模型收到的消息确实包含记忆（两条链路都查）**：

```
PASS: P6 Native：prompt 含真实记忆区块标题
PASS: P6 Native：prompt 含真实记忆内容
PASS: P6 LangGraph：prepare prompt 含真实记忆区块
PASS: P6 LangGraph：prompt 含真实记忆内容
PASS: P6 查询失败：不出现记忆区块
PASS: P6 查询为空：不出现记忆区块
```

**第六步 Grounding（没有真实依据不许声称依据记忆）**：

```
PASS: 无记忆查询却声称掌握记忆 -> 判违规 ['声称依据学习记忆，但本次没有成功读取学习记忆']
PASS: 查过记忆但没传入最终消息仍判违规 ['声称依据学习记忆，但本次记忆检索结果没有传入最终生成计划的消息']
PASS: 有真实记忆且传入最终消息时不判违规 []
PASS: P6 空查询不放行记忆主张：草案被拒 plan_error
```

**记忆更新失败不影响反馈成功**（事务边界）：

```
PASS: 记忆更新异常时反馈仍返回 200 {"message":"反馈保存成功",... "memory":{"ok":false,"error_type":"memory_update_failed"}}
PASS: 反馈没有被回滚 [(1, '极限练习', 'partial')]
PASS: 记忆事务失败后没有残留记忆 []
```

**重建与删除安全**（`test_learning_memory_state.py`，独立临时库）：

```
PASS: rebuild 成功 {'ok': True, 'memories': 6, 'evidences': 26, 'pendings': 4, 'events': 11, 'processed': 10, 'cursor': 10}
PASS: 在线与重建语义投影相等 {}
PASS: rebuild 后游标不回退 (10, '2026-01-10T00:00:00Z')
PASS: 重建后继续在线 = 纯在线 {}
PASS: 空表 rebuild 后派生表全空 {'learning_memories': 0, 'memory_evidence': 0, 'memory_pending_evidence': 0, 'memory_events': 0}
PASS: 超上限：error_type = rebuild_too_large
```

结论：**在确定性后端（LLM/Embedding 打桩）下，反馈 → 记忆更新 → 检索 → 上下文 → 两条计划链路 → grounding 全链路跑通**；真实模型那一环本次没有执行（见第 7 节 P2-1），因此不能声称「真实模型下跑通」。

## 6. 数据安全

| 检查项 | 结果 |
|---|---|
| 是否使用独立数据库 | 是。所有写库/删除/重建用例都用 `_test_*.db` 临时库（`FEEDBACK_DB_PATH` 指向临时文件或 `database.DATABASE_PATH` 重定向），跑完删除 |
| 是否执行过删除测试 | 是，仅在临时库上（`DELETE /feedback` 清理五张记忆表、rebuild、超上限拒绝） |
| 是否清理真实数据库 | **没有**。未对 `feedback.db` 执行任何写、删、重建、迁移 |
| 真实 feedback.db 是否保持不变 | 是。验收前 `SHA256=6A41F4AAFFEEAB78ACC9E8EA0221B2D8C2691C900603F315E10025700A0FCDC2`（106496 B），全部测试跑完后再取哈希**完全相同**，mtime 仍为 `2026-11-12T11:29:50` |
| 测试内自检 | `test_feedback_memory_trigger.py` / `test_memory_context.py` / `test_memory_grounding.py` 都有 `test_real_database_untouched`，本次实跑均输出 `PASS: 真实 feedback.db 字节未变` |
| 密钥安全 | 本次全程未输入、未读取、未打印任何 API Key；两处配置缺失都以明确异常报出（见下），日志无密钥 |
| 数据隔离问题 | 未发现 |

缺失配置时的显式报错（这就是「不静默回退本地」的实测证据）：

```
EMB -> EmbeddingConfigError 使用 openai_compatible Embedding 时必须设置 EMBEDDING_API_KEY（密钥只放环境变量，不要写进代码）
LLM -> LLMConfigError 使用 openai_compatible 时必须设置 LLM_API_KEY（不要把密钥写进代码）
GET / -> 200 {'message': 'AI Study Assistant'}
GET /ask -> 200 {'answer': '', 'sources': [], 'error': '模型配置错误：使用 openai_compatible 时必须设置 LLM_API_KEY（不要把密钥写进代码）'}
```

即：应用能正常启动，`/` 正常；需要模型的接口返回结构化错误，不会偷偷连本机 Ollama。

## 7. 未解决问题

**P0：无。**

**P1：无（复审指出的「ollama 可冒充云端」问题已修复，见 7.1）。**

### 7.1 复审 P1 修复证据（真实 API 测试严格限定云端）

`real_backend.py` 新增只认云端的就绪检查，`provider != "openai_compatible"` 一律不算 ready：

| 函数 | 判定依据 |
|---|---|
| `cloud_chat_status()` / `cloud_chat_ready()` | `llm_client.get_settings().provider == "openai_compatible"` 且 model/base_url/api_key 齐备 |
| `cloud_embedding_status()` / `cloud_embedding_ready()` | `embedding_service.get_settings().provider == "openai_compatible"` 且字段齐备 |
| `local_backend_status()` | 旧 Ollama 探测，**纯诊断**，不参与任何验收断言 |

实测反证（把环境变量显式设成本地后端，测试必须 SKIP 而不是通过）：

```
# LLM_PROVIDER=ollama  EMBEDDING_PROVIDER=ollama
INFO: 对话 provider=ollama model=qwen3.5:4b base_url=http://localhost:11434/api/chat api_key=缺失
INFO: Embedding provider=ollama model=bge-m3 base_url=http://localhost:11434 api_key=缺失
[SKIP] 真实 API 冒烟测试 未执行：对话：provider=ollama 是本地后端（Ollama），不属于本次云端 API 验收；
       Embedding：provider=ollama 是本地后端（Ollama），不属于本次云端 API 验收
       这不是通过：本地 Ollama 不算真实 API，配置好云端后端后必须重跑。
[exit code: 0]

# LLM_PROVIDER=ollama  EMBEDDING_PROVIDER=openai_compatible（无 Key）
[SKIP] 真实 API 冒烟测试 未执行：对话：provider=ollama 是本地后端（Ollama），不属于本次云端 API 验收；
       Embedding：使用 openai_compatible Embedding 时必须设置 EMBEDDING_API_KEY
[exit code: 0]
```

三处真实后端测试也各自加了显式断言：`test_real_api_smoke.py`（对话与 Embedding 双方 provider 必须是 `openai_compatible`、base_url 非本机地址、描述不含 Key）、`test_embedding.py` B 节、`test_openai.py` B 节（后两者先调 `cloud_*_ready()`，SKIP 文案直接写出实际 provider/model/url）。

**P2-1（阻断真实 API 验收）真实 API 冒烟未执行。**
- 复现：`python test_real_api_smoke.py` → `[SKIP] … 这不是通过；配置好云端后端后必须重跑本文件`；`real_backend.cloud_chat_ready()==False`、`cloud_embedding_ready()==False`。
- 原因：当前已明确要求对话模型和 Embedding 模型均使用云端 API，但本机 `.env` 不存在，9 个配置变量全部缺失（`LLM_PROVIDER`/`LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`/`EMBEDDING_PROVIDER`/`EMBEDDING_BASE_URL`/`EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS`/`EMBEDDING_API_KEY`），**具体服务商、模型名称、Base URL 和 Embedding 维度需以实际配置为准**。
- 影响：Task B（真实 API）与第 4 节中 SKIP 项无法消除；不影响确定性测试结论。

**P2-2 新 Embedding 模型的实际维度未经真实调用确认。**
- 现状：仅按 `EMBEDDING_DIMENSIONS` 默认 1024 做运行时校验；任务要求「实际调用确认维度」，这一条待真实 API 后补测。
- 复现：`python test_embedding.py` → B 节 SKIP。

**P3-1（本轮已修复）测试文件中的本地模型夹具已清理。**
- 上一版：14 个测试文件保留 `os.environ.setdefault("EMBEDDING_PROVIDER","ollama")` / `EMBEDDING_MODEL=bge-m3` / `OLLAMA_MODEL`。
- 本轮：21 个测试文件里的 ollama/bge-m3 默认夹具全部删除（`test_agent_eval`、`test_agent_plan_draft`、`test_agent_tools`、`test_agent_loop`、`test_document_service`、`test_embedding_batching`、`test_feedback_enum_contract`、`test_feedback_memory_trigger`、`test_feedback_plan_link`、`test_langchain_agent`、`test_learning_memory`、`test_memory_context`、`test_learning_memory_tool`、`test_memory_grounding`、`test_plan_frontend_flow`、`test_rag_service`、`test_save_plan_tool`、`test_vector_cache` 等），保留的只有显式 `clean_env(LLM_PROVIDER="ollama")`/显式 ollama Provider 用例——它们在验证 ollama 适配器契约，属**独立命名的本地后端测试**，在 `test_llm_client.py`、`test_embedding_cloud.py` 内单独统计，不混入云端 API 验收。
- 断言未削弱：删除的只是环境默认值，没有删掉任何 `PASS` 断言；清理后全部用例在无 Key、Ollama 未启动的环境下仍然 exit=0（走 Stub/Mock）。

**P3-2（本轮已修复）提示语不再指向 Ollama。**
- `agent_eval.py:808`、`eval_rag_metrics.py:24`/`:484`/`:587` 全部改为「云端 Embedding API 配置 / 可用的 Embedding API，见 docs/MODEL-API-MIGRATION.md」。

**P3-3 `MIN_SCORE=0.5` 的标定依据仍是 bge-m3 实测（阈值未改，注释已更新）。**
- 位置：`rag_service.py:51-55`，注释现明确写「尚未在云端 Embedding 模型上重新标定，真实 API 可用后必须用同一批样本重测」。
- 为什么本轮不改数值：没有真实 Embedding API 就无法重测分布，凭猜测改阈值等于伪造标定结论。

**P3-4 `knowledge/` 的 2 篇文档尚未在真实 Embedding 模型上重建**（`.rag_cache/` 为空属预期，等 Key 到位后按迁移文档第 7 节安全重建流程执行）。

## 8. 最终结论

**有条件通过。**

依据：

1. 对话模型、Agent Tool Calling、Native、LangGraph 已统一走 `llm_client` 的云端 Provider，Embedding 走统一 `embedding_service`，文档向量与查询向量共用同一 `cache_identity()`；代码层不存在跨 Provider 的静默回退分支（`llm_client.py:792`、`llm_client.py:1033`、`embedding_service.py:143`）。
2. 缺少 API 配置时行为正确：显式抛 `LLMConfigError` / `EmbeddingConfigError`，`/ask` 返回结构化错误，不连本地 Ollama（第 6 节实测输出）。
3. Stage 2 主线在确定性后端下全链路跑通：反馈 → 记忆更新 → 检索 → 上下文 → Native/LangGraph 计划生成 → grounding（第 5 节实测证据）。
4. 全量回归 41/41 脚本 exit=0，无 FAIL；真实数据库字节未变。
5. 未达到「通过」的唯一原因是**真实 API 冒烟一项都没执行**（P2-1/P2-2），按任务要求不得用 Mock 结果冒充真实 API 结果。

后续必须补做（按优先级）：

1. 提供对话与 Embedding 的服务商 base URL、模型名与 Key（走安全卡片），然后重跑 `test_real_api_smoke.py`、`test_embedding.py`、`test_openai.py` 的 B 节，并把真实维度写回本报告第 3 节。
2. Key 到位后按 `docs/MODEL-API-MIGRATION.md` 第 7 节在临时位置重建 `knowledge/` 向量（2 篇文档 / 7 个块），确认检索结果后切换正式缓存。
3. 清理 P3-1/P3-2 的旧 Ollama 夹具与提示语（单独一个文档/清理 commit，不与迁移混在一起）。
