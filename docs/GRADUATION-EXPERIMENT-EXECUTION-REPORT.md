# 毕业设计正式实验执行报告

- 报告性质：**实验执行归档**（只记录 2026-11-13 实际发生了什么），不是设计文档，也不替代分析报告。
- 设计基线：`docs/GRADUATION-EXPERIMENT-DESIGN.md`（冻结，本报告未修改其中任何口径）。
- 数据来源：本机真实运行记录（`experiments/graduation/runs/` 下的 `plan.json` / `results.jsonl` / `prompts/`），全部由执行器 `experiments/graduation/run_experiment.py` 生成。
- 生成方式：本报告的每个数字都由脚本从上述 JSON 文件重新读取计算，未手工推算、未使用估算值。

> 口径提示：本仓库没有配置 CI（GitHub Actions），因此本报告不包含任何"CI 通过"的表述；所有"通过/成功"均指**本机实跑**结果。

---

## 1. 实验基线与版本

| 项 | 值 |
| --- | --- |
| 仓库 | `BlueOcean3214/AI-Study-Assistant` |
| 分支 | `main` |
| 基线 commit | `e5f97e9fd9d721af24ed5d7a6c047ff305fd3fcd`（PR #11 合并提交，即实验开始时的 main 最新提交） |
| 实验配置 | `experiments/graduation/config.json` |
| 配置文件 SHA256 | `b33bd1ac4ca16ffd7a2ad9c8d256b4842454f3c6d5a5268a04df50255d05c256`（plan.json 的 `config_sha256` 字段） |
| 随机化方案 | `five_round_randomized_block_v1` |
| 算法版本 | `sha256-block-order-v1` |
| 区组单位 | `sample`（每个样本一个区块） |
| 轮数 | `round_count = 5`（`repeat_index == round_index`，取值 0–4） |
| 随机种子 | `seed = 20261113`（开跑前人工确认并冻结，执行期间未改动） |
| 固定参考时间 | `ref_date = 2026-11-12T21:00:00`（历史时间戳 = ref_date + offset_days，不受本机时钟影响） |
| 知识检索 | `knowledge_results = []`、`knowledge_search_failed = false`（本次不把 RAG 作为实验变量，未调用 Embedding） |
| 记忆检索 | `memory_limit = 5` |
| 近期反馈 | `recent_feedback_days = 7`、`recent_feedback_limit = 20` |
| 采样参数 | `temperature = 0.7`、`top_p = 1.0`、`max_tokens = 2048`（A/B/C/D 完全一致，逐条写入结果） |

执行顺序的唯一依据是各 run-dir 的 `plan.json`（含 `plan_sha256`、`sample_order`、每轮组顺序、`plan_seq`），执行器不重算顺序。

---

## 2. 模型服务配置

| 项 | 值 |
| --- | --- |
| Provider | `openai_compatible`（OpenAI 兼容网关） |
| Model | `santuo-workbuddy` |
| 认证方式 | 运行进程内注入 `LLM_*` 环境变量（来自本机已配置的密钥存储） |
| 端点 | 内网网关（主机/端口不写入文档） |
| 密钥 | 不写入文档、不落盘、不打印；本报告与仓库中不存在任何密钥或端点明文 |

每条结果记录都带有 `provider` / `model` / `temperature` / `top_p` / `max_tokens`，可逐条复核；本次 155 份新生成记录中这些字段完全一致。

---

## 3. API 冒烟记录

- run-dir：`experiments/graduation/runs/internal-api-smoke-20261113T061019Z`（UTC 目录名；本地时间 2026-11-13 14:10）
- 规模：S1 × A × 1 份（单次生成，**不计入 165 份实验矩阵**）
- `plan_sha256`：`4afefab801fc7b0595b7cdbaa66f39dba0049a924b6c4c84aa73a8c552bf6255`
- 结果：1 条记录，`success = true`，`stopped_reason = plan_draft_ready`
- 请求 1 次、修正 0 次
- tokens：prompt 773 / completion 537 / total 1310（`usage_available = true`）
- 耗时：4.884 s
- `prompt_sha256 = 1cba51d1399648b3ba66621897b7a79396668e544499f622690f1a93b008e656`，与冻结的 S1/A 哈希一致
- 该冒烟结果独立存档，**不得覆盖、不得重复运行**；后续主实验与补充实验未重跑该格。

---

## 4. 主实验 140 份执行情况

- run-dir：`experiments/graduation/runs/main-20261113T061051Z`
- 计划：7 个样本区块 × 5 轮 × A/B/C/D 各一次 = 140 个任务
- `sample_order`：`S3, S7, S2, S6, S1, S4, S5`
- 每轮内 A/B/C/D 各出现一次；每个 (样本, 条件) 恰好 5 条记录，`repeat_index` 分别 0–4
- `plan_sha256`：`d17fb761819736e95f73ce20ef074f00757566aaa8950358a775f5d07fa7eea3`

执行结果：

| 指标 | 值 |
| --- | --- |
| 记录数 | 140 |
| success | 140 |
| failed | 0 |
| 重复实验身份 | 0 |
| 唯一 (sample, group, repeat) 组合 | 140 / 140 |
| 不同 `prompt_sha256` 数量 | 28（= 7 样本 × 4 条件，每格 5 次完全一致） |
| 实际请求次数 | 150（= 140 次起草 + 10 次修正） |
| 修正次数 | 合计 10，单任务最多 2（未超过 `MAX_CORRECTIONS = 2`） |
| tokens | prompt 181,093 / completion 149,978 / total 331,071（140/140 条 `usage_available = true`） |
| 延迟 | mean 6.846 s / median 6.258 s / max 15.394 s / 累计 958.4 s |
| 执行时间 | 2026-11-13 14:11:00 → 14:26:52（+08:00），约 15 分 52 秒 |

审计核对（脚本逐条比对 `results.jsonl` 与 `plan.json`）：

- `experiment_id` / `sample_id` / `group` / `repeat_index` / `plan_seq` / `round_index` / `seed` / `scheme` / `algorithm_version` / `plan_sha256` 与计划 **0 处不一致**；
- 结果行按 `plan_seq` 覆盖 0–139 全部序号，无缺号、无重复；
- 28 个 Prompt 哈希与 `build_prompts.py` 冻结值一致。

---

## 5. 补充实验：C-subj / D-subj（S5，10 份）

- run-dir：`experiments/graduation/runs/suppl-subj-20261113T062711Z`
- 计划：S5 × {C-subj, D-subj} × 5 轮 = 10 个任务；`round_index` / `repeat_index` 0–4 各一次
- `plan_sha256`：`a95e5e37068c7b6427a8f88ad7ed07e4dda9e14fa614a5a3bc1c1b0f4f71e243`

| 指标 | 值 |
| --- | --- |
| 记录数 | 10（C-subj 5 + D-subj 5） |
| success / failed | 10 / 0 |
| 请求 / 修正 | 10 / 0 |
| tokens | prompt 13,245 / completion 10,468 / total 23,713 |
| 延迟 | mean 6.663 s / median 6.545 s / max 9.606 s / 累计 66.6 s |
| 执行时间 | 2026-11-13 14:27:18 → 14:28:18（+08:00） |
| `prompt_sha256` | C-subj `fbd3a1c2689b…`、D-subj `dc0e5df63633…`（各自组内 5 次完全一致） |

说明：C-subj 只注入「当前主题记忆」（无反馈、无短期上下文），D-subj 注入「主题记忆 + 近期反馈 + 短期上下文」；两组共用 S5 的固定输入，经由 `normalize_subject()` 归一化为「概率论」后检索记忆。

---

## 6. 补充实验：S7-empty × D（5 份）

- run-dir：`experiments/graduation/runs/suppl-s7empty-20261113T062822Z`
- 计划：S7-empty × D × 5 轮 = 5 个任务
- `plan_sha256`：`a51d8a7ec418f71adfe50b8b9b6fc43ff28a23f5b411e38a62b28c8840eb3ffd`

| 指标 | 值 |
| --- | --- |
| 记录数 | 5 |
| success / failed | 5 / 0 |
| 请求 / 修正 | 5 / 0 |
| tokens | prompt 4,765 / completion 3,357 / total 8,122 |
| 延迟 | mean 4.959 s / median 4.786 s / max 5.448 s / 累计 24.8 s |
| 执行时间 | 2026-11-13 14:28:28 → 14:28:47（+08:00） |
| `prompt_sha256` | `fe80087dff85…`（5 次完全一致） |

用途：验证「零记忆、零反馈、零历史计划」时生成器不会凭空编造历史（M3 / E1 方向）。该组的空记忆预检口径是**成功且为空**（`memory_tool.ok = true` 且 `count = 0`），与 C-subj/D-subj 的「记忆不得为空」相反，两者不可混用。

---

## 7. B0 历史 10 份的保留说明（legacy，只读）

B0 在 v4.2 起是 legacy 探索组，不进入五轮随机区组计划，本次**未修改、未重跑、未补字段**。三个历史 run-dir：

| run-dir | 记录数 | `results.jsonl` 大小 | SHA256 |
| --- | --- | --- | --- |
| `runs/20261112T210000-20261113T011751Z` | 1 | 5,518 B | `bd75cca7617c84a79b4eecfcd3fb1f5417df4d6892b0dd1217b57d4f3f23a8f2` |
| `runs/20261112T210000-20261113T012031Z` | 1 | 6,908 B | `9175c0914b7e9b6c0a40c920dd81051151a6baa2d1518d5f382cc9688246174d` |
| `runs/b0-remainder-20261113T013105Z` | 8 | 50,280 B | `248b5742e2e834d0b5b782085f60ba2cb96ff2296e661052855dcec84e02a437` |

来源映射见 `experiments/graduation/runs/B0-REPEAT-MAP.md`。执行器在计划阶段即拒绝显式 `--group B0`（提示 legacy），本次未触发该路径。

### B0 历史用量核对（只读）

三条记录均含可靠的 `request_count` 与 `token_usage`（`available = true`、`prompt_tokens` / `completion_tokens` 为整数），因此可以完整计算：

| 项 | 值 |
| --- | --- |
| 记录数 | 10 |
| 实际请求数 | 10（每条 1 次，无修正） |
| prompt tokens | 9,720 |
| completion tokens | 8,626 |
| total tokens | 18,346 |

不存在字段缺失或不可读的情况，故未使用任何估算值填补。

---

## 8. 请求数、修正、token 与延迟汇总

### 8.1 本次新生成（不含 B0 历史）

| 批次 | 份数 | 请求 | 修正 | prompt tokens | completion tokens | total tokens |
| --- | --- | --- | --- | --- | --- | --- |
| API 冒烟 | 1 | 1 | 0 | 773 | 537 | 1,310 |
| 主实验 A/B/C/D | 140 | 150 | 10 | 181,093 | 149,978 | 331,071 |
| 补充 C-subj/D-subj | 10 | 10 | 0 | 13,245 | 10,468 | 23,713 |
| 补充 S7-empty×D | 5 | 5 | 0 | 4,765 | 3,357 | 8,122 |
| **合计** | **156** | **166** | **10** | **199,876** | **164,340** | **364,216** |

- **155 份** = 本次新生成的实验数据（不含冒烟）；**1 份** 冒烟独立存档；合计生成 156 份、166 次请求、364,216 tokens。
- token 用量全部来自服务端返回的 `usage`，本次 156 条记录全部 `usage_available = true`（156/156），未做任何 token 估算。

### 8.2 并入 B0 历史后的总量

| 项 | 值 |
| --- | --- |
| 实验矩阵总份数 | 165（主实验 140 + C/D-subj 10 + S7-empty×D 5 + B0 legacy 10） |
| 请求总数 | 176（166 + 10） |
| tokens 总数 | 382,562（364,216 + 18,346） |

> 说明：B0 的 10 次请求发生在 2026-11-12 至 11-13 的 legacy 运行中，不属于本次执行；单列是为了让"矩阵总量"与"本次实际消耗"两个口径都能被核对，不把历史用量算成本次消耗。

### 8.3 延迟

| 批次 | mean | median | max | 累计 |
| --- | --- | --- | --- | --- |
| 冒烟 | 4.884 s | 4.884 s | 4.884 s | 4.9 s |
| 主实验 | 6.846 s | 6.258 s | 15.394 s | 958.4 s |
| C/D-subj | 6.663 s | 6.545 s | 9.606 s | 66.6 s |
| S7-empty×D | 4.959 s | 4.786 s | 5.448 s | 24.8 s |

延迟是「单次 HTTP 请求」的客户端计时，包含内网网络往返，不等于模型纯推理时间。

---

## 9. Prompt SHA256 与 plan SHA256

`prompt_sha256` 是对最终 `messages` 列表的哈希（不是对 `prompts/*.txt` 文件字节的哈希）。主实验 28 个格子的冻结值如下（每格 5 次完全一致）：

| 样本 | A | B | C | D |
| --- | --- | --- | --- | --- |
| S1 | `1cba51d1…08e656` | `bd1e0323…6cc5da` | `56dd4573…702da7` | `cafc40c6…4d223a` |
| S2 | `6fab10f7…824070` | `79b613aa…0606718` | `271c2730…88d9f4` | `2791b923…7bbac14` |
| S3 | `c52fadd4…e3da13` | `292d22d3…bba7c6` | `d6ce98df…53b94e` | `9847630f…863f91` |
| S4 | `0c01afd0…149e2a9` | `7e733e32…1fe618` | `c96aa71a…08ac450` | `e36f01b5…de1c07b` |
| S5 | `864c1bce…aea059` | `4876f3c5…bb1759` | `467a59a8…dcf7bc` | `ab8d8b77…d29d550` |
| S6 | `60ca7169…73369ac` | `d93b593a…dceba01` | `38faace3…529d62` | `b031a726…ab334ac` |
| S7 | `9ddf093d…5aa4c3d` | `c47ae633…e2cc5d` | `901d6790…ff4103` | `aa55f5ca…883803` |

完整 64 位哈希、以及 `prompt_chars`（如 S5：A 1090 / B 2316 / C 2651 / D 3877 字符）见各 run-dir 的 `results.jsonl` 与 `prompts/` 目录。

补充批次：C-subj `fbd3a1c2689b…`、D-subj `dc0e5df63633…`、S7-empty×D `fe80087dff85…`、冒烟 S1/A `1cba51d1…`。全部 32 个（28 + 3 + 1）哈希与 `build_prompts.py` 的冻结输出一致，执行期间 Prompt 模板未改动。

`plan_sha256`（对规范化计划内容哈希，非文件字节哈希）：

| 批次 | `plan_sha256` |
| --- | --- |
| 冒烟 | `4afefab801fc7b0595b7cdbaa66f39dba0049a924b6c4c84aa73a8c552bf6255` |
| 主实验 | `d17fb761819736e95f73ce20ef074f00757566aaa8950358a775f5d07fa7eea3` |
| C/D-subj | `a95e5e37068c7b6427a8f88ad7ed07e4dda9e14fa614a5a3bc1c1b0f4f71e243` |
| S7-empty×D | `a51d8a7ec418f71adfe50b8b9b6fc43ff28a23f5b411e38a62b28c8840eb3ffd` |

四个批次的 `experiment_id` 互不相同（后缀即 run-dir 名），因此即使样本/条件同名也不会在身份上混淆。

---

## 10. 数据库隔离与历史数据安全检查

### 10.1 隔离机制（代码级）

- `run_experiment.py:78 isolate_database()` 在**导入** `database` / `memory_service` 之前设置 `os.environ["FEEDBACK_DB_PATH"] = <run-dir>/<sample>.db`，因此进程内所有写入落在 run-dir 的临时库。
- `run_experiment.py:61 assert_isolated()` 拒绝真实 `feedback.db`、拒绝 run-dir 之外的路径；计划/结果校验失败时在任何数据库操作之前退出。
- 调用顺序：`load_plan` / `validate_plan` → `write_plan_atomic` → `resume_state` → `isolate_database` → `run_one`。
- 本次 155 份新生成记录全部在独立 run-dir 的临时库中完成；未对真实库执行任何写操作。

### 10.2 运行前后核对

| 检查项 | 结果 |
| --- | --- |
| 真实 `feedback.db` SHA256 | `4E8F2C986D96AC672E1CCED7F698622B983CDCC175C61B081D535016ED129251`（运行前后一致） |
| 大小 / mtime | 106,496 B / 2026-11-12 11:56:32（未变化） |
| 历史 runs 文件 | 8 个文件（3 个 B0 run-dir 的 plan/results/prompts + `B0-REPEAT-MAP.md`）的路径/大小/mtime/SHA256 前后 `Compare-Object` 无差异 |
| 未跟踪文件 | 运行前后均为 8 项（`.dsh/`、5 个 `_test_*.db`、2 个 sweep 日志），未新增、未删除 |
| Embedding 调用 | `EMBEDDING_API_KEY` / `DSH_SECRET_EMBEDDING_API_KEY` / `RERANK_API_KEY` 均不存在，全程未触发 Embedding 或向量检索 |
| Git 工作区 | 实验期间 `main` 停在 `e5f97e9`，无提交、无 `reset`、无 `clean`、无 stash |
| 原始数据是否入 Git | 否。`.gitignore` 已忽略 `experiments/graduation/runs/`；本报告只归档哈希与统计量，不提交原始 `results.jsonl` |

---

## 11. 实验限制与已知缺口

1. **服务端模型版本信息缺失**：网关只返回 `model = santuo-workbuddy` 别名，未返回模型版本/快照 ID。若服务商在批次之间更换底层权重，本报告无法从数据中分辨——只能记录"使用同一别名、采样参数一致"。
2. **n = 7 个模拟样本**：7 个样本是人工构造的模拟学习者场景，不是真实用户；结论只能作为方法学验证，不能外推为真实学习效果。5 次重复是生成噪声估计，不是独立样本。
3. **不含 RAG**：所有条件 `knowledge_results = []`，因此本实验不能说明知识检索的作用。
4. **单一路径**：固定在 native 起草链路上，未覆盖生产环境的多轮研究、知识检索失败退化、langgraph 路径差异。
5. **延迟含网络**：内网往返计入 `elapsed_seconds`，不是纯推理耗时；跨批次比较延迟需谨慎。
6. **自动指标尚未计算**：`check_auto.py`（Y1–Y5、`check_grounding`）在设计中列为交付物，但仓库中不存在，因此本次只完成了"生成"，**尚未产出任何自动指标数值**；在补齐脚本前不得对外声称已完成指标评估。
7. **无 CI**：仓库未配置 CI，本报告所有验证均为本机实跑。
8. **B0 与主实验不同源**：B0 为 legacy 运行（2026-11-12 至 11-13），与本次批次共享同一 Prompt 哈希与采样参数，但不进入五轮计划、不做身份层面的同源校验（详见 `B0-REPEAT-MAP.md`）。

---

## 12. 执行结论

1. 正式实验按设计规模完成：**主实验 140 份、补充 C/D-subj 10 份、S7-empty×D 5 份，共 155 份新数据，0 失败、0 重复身份、0 审计不一致**；加冒烟 1 份为 156 份生成记录。
2. 实验矩阵总量 **165 份**（含 B0 legacy 10 份）；本次实际 **166 次请求、364,216 tokens**；并入 B0 历史用量后为 **176 次请求、382,562 tokens**。
3. 28 个主实验格子的 `prompt_sha256` 与冻结值一致，`plan_sha256`、`config_sha256` 可复核；执行顺序完全由 `plan.json` 决定。
4. 真实 `feedback.db` 与历史 runs 前后完全一致，未发生任何越界写入；原始实验数据未进入 Git。
5. 当前状态是"**数据已产出、指标未分析**"：盲评与分析脚本（`make_blind_pack.py`、`check_auto.py`、`analyze_stats.py`、`analyze.py`）在仓库中均不存在，下一步需先补脚本再进入盲评与统计（见附录 B）。
6. 本报告不修改 `docs/GRADUATION-EXPERIMENT-DESIGN.md`，也不修改任何 `plan.json` / `results.jsonl` / B0 文件。

---

## 附录 A：实验文件校验清单

机器可读版本：`docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv`（8 行）。下表为同一份数据：

| 批次 | 文件 | 大小 (B) | SHA256 | 记录数 |
| --- | --- | --- | --- | --- |
| 冒烟 | `runs/internal-api-smoke-20261113T061019Z/plan.json` | 851 | `10248c9f2b2f6b8982d0da9d2e8c2fa59775eeea4d6614af6dfe20ccdea2d06e` | 1 |
| 冒烟 | `runs/internal-api-smoke-20261113T061019Z/results.jsonl` | 6,617 | `7502e4681389f8736e12573fb45f7388b8f4e8924e18af9aa04833d194643b14` | 1 |
| 主实验 | `runs/main-20261113T061051Z/plan.json` | 33,686 | `877e955e9f2db8a7e7ceba8d2aaff5f8a6f8296b59d7c530d06e2b51013ec53d` | 140 |
| 主实验 | `runs/main-20261113T061051Z/results.jsonl` | 982,080 | `bbab693f72b459206035e06b26fee99fab86584d98627f74a77d9cc6df1f6383` | 140 |
| C/D-subj | `runs/suppl-subj-20261113T062711Z/plan.json` | 3,080 | `01f23fbc88efedf0d0317215815a23946ebe67cdde448fc4384ba60049b36e36` | 10 |
| C/D-subj | `runs/suppl-subj-20261113T062711Z/results.jsonl` | 65,100 | `5f718df8850f8a1cd1e5ddc3fe89d55aa0a0d7f10cb800c51cb92e76a889eac3` | 10 |
| S7-empty×D | `runs/suppl-s7empty-20261113T062822Z/plan.json` | 1,835 | `161c476630b65ce159735309abae99da536412e0664bd4027370de9068f38d2f` | 5 |
| S7-empty×D | `runs/suppl-s7empty-20261113T062822Z/results.jsonl` | 34,020 | `f0577f4f4bacaec04f97bf12a7d3f27f14f0bd5272a81a5cdd3828313dfcb2df` | 5 |

- `records` 对 `results.jsonl` 是行数，对 `plan.json` 是 `blocks[*].tasks` 的任务总数。
- 路径前缀统一为 `experiments/graduation/`，表中省略。
- 配套 `prompts/` 目录：主实验 28 个文件（7 样本 × 4 条件）、C/D-subj 2 个、S7-empty 1 个、冒烟 1 个，共 32 个；这些 Prompt 文件的字节哈希与 `prompt_sha256`（messages 哈希）不是同一概念，复核时以 `results.jsonl` 中的 `prompt_sha256` 为准。

---

## 附录 B：盲评准备一致性检查（只检查，不启动评分）

### B.1 脚本存在性

| 设计文档要求的文件 | 仓库实际状态 |
| --- | --- |
| `make_blind_pack.py` | **不存在**（`git ls-files` 与工作区均无） |
| `analyze_stats.py` | **不存在** |
| `analyze.py` | **不存在** |
| `check_auto.py` | **不存在** |
| `report/EXPERIMENT-REPORT.md` | **不存在** |
| 评分 CSV 模板（`blind_id, rater_id, P1…`） | **不存在**（只在设计 §7.2 定义 schema，没有模板文件） |

结论：**无法进行"脚本口径与设计是否一致"的核对，因为脚本尚未创建**。目前只有 `config.json`、`plan_schedule.py`、`build_prompts.py`、`seed_fixture.py`、`run_experiment.py`、`check_memory.py`、`samples/` 七项已入库。以下各项为**设计口径 vs 现有数据**的可支撑性核对。

### B.2 数据能否支撑设计口径

| 设计要求 | 现有数据 | 结论 |
| --- | --- | --- |
| 主实验盲评 84 份 = 28 格 × 每格从 5 次中抽 3 | 主实验 28 格，每格恰好 5 条 success 记录 | ✅ 可支撑 |
| 两位评审独立评价 | 设计 §5.4/§5.5 已规定独立评分、先算 κ 再裁决 | ✅ 口径明确，缺模板 |
| 补充 23 份 flag 复核 = S7-empty 3 + B0 10 + 敏感性 10 | S7-empty 5 条、B0 10 条、C/D-subj 10 条 | ✅ 可支撑（S7-empty 需抽 3） |
| 主实验与补充盲评数据分开 | 设计要求 `ratings.csv` 与 `ratings_supplement.csv` 分文件 | ✅ 口径明确，缺脚本 |
| 盲评 ID 不泄露 sample/group/condition/repeat | 设计 §5.4 示例 `blind_id` 写成 `S3-7f2a` | ❌ **冲突**：示例中的 `S3` 就是样本号，直接泄露 sample；需改为不含任何真实身份前缀的随机 ID |
| 盲评包可通过独立映射文件追溯 | 设计只写"评分完成后由脚本回填映射" | ⚠️ 不完整：未规定映射文件名、字段（至少 `blind_id → experiment_id/sample_id/group/repeat_index`）与存放位置（不得与评分表同包分发） |
| 评分结果不得反向修改原始输出 | 设计隐含只追加，但无明文禁止 | ⚠️ 建议在脚本层强制只读（打开 `results.jsonl` 用只读模式，输出单独目录） |

### B.3 盲评阶段仍需解决的问题（按优先级）

1. **补 `make_blind_pack.py`**：实现 84 份抽样（28 格 × 3）、补充 23 份（S7-empty 抽 3、B0 全 10、C/D-subj 全 10）、生成不含身份信息的 `blind_id`、输出 `mapping.csv`（与盲评包分离）。
2. **确定并冻结盲抽取样种子**：设计只说"抽签种子固定"，没有给值；需在脚本与文档中写死一个种子并记录。
3. **确定 S7-empty 5 取 3 的规则**（随机 or 固定轮次），并写入文档。
4. **修正 `blind_id` 命名示例**：去掉 `S3-` 这类样本前缀，改为纯随机串；这一条是与 §5.4「评分表不含条件列」并列的硬要求。
5. **补评分 CSV 模板**：主表 `blind_id, rater_id, P1, P2, P3, P4, S2, S3_flag, S4_verdict, notes`；补充表 `blind_id, rater_id, E1, E7, S3_flag, notes`。需按样本过滤：P3 仅 S1–S6（72 份）、`S3_flag` 仅 S3/S6（24 份）、`S4_verdict` 仅 S6（12 份）。
6. **补 `check_auto.py`（Y1–Y5 + grounding）**：这是进入统计前的必需前置，当前 155 份只有原始输出，没有任何自动指标。
7. **补 `analyze_stats.py` / `analyze.py`**：按 §5.5 的固定顺序实现（原始分 → κ → 裁决 → 输出级最终分 → 格内聚合 → n=7 检验），并实现"未决项不参与聚合"的处理。
8. **LLM 辅助评分**（可选）：设计要求 `temperature=0`、评 3 次取多数、只报相关性与 κ，不作为主结论；需单独脚本，且不得与人工分混算。

> 本附录只做核对与缺口登记，**未生成盲评包、未模拟评分、未启动任何统计检验、未修改研究假设**。
