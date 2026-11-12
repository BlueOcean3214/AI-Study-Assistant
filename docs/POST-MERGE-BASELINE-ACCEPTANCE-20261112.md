# PR #7 合并后本地基线验收报告

- 验收时间：2026-11-12 13:14–13:15（Asia/Shanghai），本机实跑
- 验收基线：`53a18b3985ed9e964ca197e22b167fbb7225d878`（PR #7 merge commit，= 当时 origin/main）
- 被测代码 head：`296712c84ddc8fe70129909ee034b8c17208c7cb`（PR #7 第二轮复审通过的开发 head）
- 执行方式：`.\.venv\Scripts\python.exe test_xxx.py`，每个脚本独立进程、独立 exit code
- 结论：**本地基线验收通过（无 P0/P1），可以进入真实 API 配置阶段**

> 口径声明：本报告的 758 个 PASS 全部来自 Stub / Mock 打桩与确定性链路，**不代表真实云端 API 已可用**。
> 真实对话与 Embedding 调用、Embedding 实际维度、`knowledge/` 向量重建与真实语义召回均未执行，
> `test_real_api_smoke.py` 本次未运行。仓库没有 GitHub Actions，本报告不是 CI 结果。

## 1. Git 状态

| 项目 | 实际结果 |
|---|---|
| 当前分支 | `main` |
| 本地 HEAD | `53a18b3985ed9e964ca197e22b167fbb7225d878` |
| origin/main | `53a18b3985ed9e964ca197e22b167fbb7225d878` |
| 本地与远端差异 | `main..origin/main` = 0，`origin/main..main` = 0 |
| PR #7 合并提交是否进入 main | 是（`git merge-base --is-ancestor 53a18b3 main` 通过） |
| 已跟踪文件改动 | 无（`git diff` 为空） |
| 未跟踪遗留文件 | 5 个 `_test_*.db` 与 2 个 `sweep_*.log`，验收过程中未删除、未提交 |

同步方式为 `git fetch origin` + 本地 main 已等于 `origin/main`，未做 cherry-pick、未做 reset、未强推。

## 2. 测试结果

| 测试文件 | PASS | FAIL | SKIP | Exit Code |
|---|---:|---:|---:|---:|
| test_embedding_cloud.py | 40 | 0 | 0 | 0 |
| test_vector_cache.py | 50 | 0 | 0 | 0 |
| test_rag_service.py | 47 | 0 | 1 | 0 |
| test_feedback_memory_trigger.py | 157 | 0 | 0 | 0 |
| test_learning_memory_tool.py | 87 | 0 | 0 | 0 |
| test_memory_context.py | 105 | 0 | 0 | 0 |
| test_memory_grounding.py | 40 | 0 | 0 | 0 |
| test_learning_memory_state.py | 216 | 0 | 0 | 0 |
| test_langgraph_plan.py | 16 | 0 | 0 | 0 |
| **合计** | **758** | **0** | **1** | 全部 0 |

判定口径：以 exit code 为准，并只统计行首 `PASS:` / `FAIL:` / `[SKIP]`。不用「包含 FAIL 字样」粗筛——那会误命中
`test_memory_failure_does_not_hurt_feedback`、`fail-closed`、`failed_count` 这类用例名与字段名（粗筛会误报 6/2/1/1/2 条）。

唯一的 SKIP 原文：

```
[SKIP] 语义相关查询可召回（需真实 Embedding 后端，见 test_real_api_smoke.py）
```

该用例必须真实 Embedding 才能验证，计入 SKIP，不计入通过。

## 3. 关键证据（摘自实跑输出）

- Embedding 适配：`PASS: 缓存身份含 provider/model/dimensions identity=openai_compatible:text-embedding-v4@1024`、
  `PASS: 缓存身份随模型/维度变化`、`PASS: 维度切换后缓存身份不匹配`、
  `PASS: 测试全程未发起真实网络请求（requests.post 为炸弹）`。
- 向量缓存：`INFO: 1200 chunk 首次=120 批/1200 条，用时 0.36s；重启后=0 批，缓存恢复 26.0ms，缓存文件 6.7MB`、
  `PASS: 没有污染真实 .rag_cache/ exists=False`。
- 学习记忆状态机：`结果：39/39 个测试函数通过`。
- RAG 降级：`PASS: 降级时 /ask 仍返回答案`、`PASS: 降级时 sources 为空`、`PASS: 降级时 prompt 直接使用问题`。

## 4. 模型配置快速检查

| 检查项 | 结果 | 依据 |
|---|---|---|
| `LLM_PROVIDER` 默认值 | `openai_compatible` | 实跑打印；`llm_client.py:118`、`llm_client.py:171` |
| `EMBEDDING_PROVIDER` 默认值 | `openai_compatible` | 实跑打印；`embedding_service.py:144` |
| 对话与 Embedding 可配不同服务商 | 可以 | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 与 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` 完全独立 |
| API 失败是否回退本地 Ollama | 不回退 | `llm_client.py:1033-1040`、`embedding_service.py:458-461` 按 provider 单路分派，失败直接返回错误 |
| 缺 Key 行为 | 明确报错 | 实跑：`LLMConfigError`（对话）、`EmbeddingConfigError`（Embedding），不落本地 |
| Key 是否进日志 | 不进 | `env_loader.py` 只打印异常类型；`real_backend.py:17` 只输出 provider/model/base_url 与「Key 是否存在」 |
| 维度校验 | 强校验 | `embedding_service.py:292-299` 返回向量长度与 `EMBEDDING_DIMENSIONS` 不一致即抛错 |
| `.env` 是否被忽略 | 是 | `git check-ignore -v .env` → `.gitignore:6:.env`；`git ls-files` 中只有 `.env.example` |

## 5. 数据保护

| 项目 | 结果 |
|---|---|
| `feedback.db` | sha256 `4E8F2C986D96AC672E1CCED7F698622B983CDCC175C61B081D535016ED129251`，mtime 2026-11-12 11:56:32（早于本轮测试），未被测试触碰 |
| `.rag_cache/` | 不存在（测试用临时目录并在结束时清理） |
| `knowledge/` | 2 个 txt 原样未动 |
| 测试数据库 | 写库用例只使用 `_test_*.db` 独立库 |

## 6. 遗留事项（本轮未执行，需配置真实服务商后补做）

1. 真实对话 API 尚未测试（调用次数 0）。
2. 真实 Embedding API 尚未测试（调用次数 0）。
3. Embedding 实际维度尚未确认；当前 `1024` 只是配置默认值。
4. `knowledge/` 正式向量重建尚未进行。
5. `MIN_SCORE=0.5` 仍为 bge-m3 历史标定值，未按新模型重标定。
6. 真实 RAG 语义召回尚未验收。

## 7. 非阻断问题

- P3（文档残留）：`test_rag_service.py:3` 仍写着「运行测试需要本机 Ollama 已启动并已 pull bge-m3」，
  与同文件第 14 行「Embedding 用 stub 打桩，不依赖本地 Ollama，也不调用云端 API」矛盾。
  不影响测试结果，属一行注释修正，本次未改动以保持验收范围最小。

## 8. 复现命令

```powershell
$env:PYTHONIOENCODING="utf-8"
[Console]::OutputEncoding=[Text.Encoding]::UTF8

.\.venv\Scripts\python.exe test_embedding_cloud.py
.\.venv\Scripts\python.exe test_vector_cache.py
.\.venv\Scripts\python.exe test_rag_service.py
.\.venv\Scripts\python.exe test_feedback_memory_trigger.py
.\.venv\Scripts\python.exe test_learning_memory_tool.py
.\.venv\Scripts\python.exe test_memory_context.py
.\.venv\Scripts\python.exe test_memory_grounding.py
.\.venv\Scripts\python.exe test_learning_memory_state.py
.\.venv\Scripts\python.exe test_langgraph_plan.py
```

真实 API 冒烟测试 `test_real_api_smoke.py` 需在配置好服务商 Key、Base URL、模型与维度后单独执行，
且必须满足 `provider == openai_compatible` 才被视为本次云端 API 验收的一部分。
