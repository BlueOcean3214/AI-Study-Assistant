# 毕业设计实验方案设计：长期学习记忆对学习状态评估与个性化计划生成的影响

- 文档状态：**设计稿（Design Only）**，尚未执行任何实验
- 基线：`main` = `53a18b3985ed9e964ca197e22b167fbb7225d878`
- 适用系统：AI-Study-Assistant（FastAPI + SQLite + LangGraph + 可切换模型后端）
- 撰写日期：2026-11-12

> 本文档只描述**如何做实验**，不包含任何实验结果。文中出现的数字全部是种子数据、
> 阈值或设计参数，不是模型输出，也不是用户数据。全部学习者为**构造的模拟样本**。

---

## 0. 结论先行：本实验能回答什么、不能回答什么

**能回答**：在相同的模型、相同的用户请求、相同的知识库检索结果下，把「短期学习上下文」和
「长期学习记忆」分别打开/关闭，计划草案在**状态匹配、难度合理、重复困难针对性、可执行性**
四个维度上的差异，以及记忆层自身的识别准确性与证据可追溯性。

**不能回答**：真实学习者长期使用后的学习效果（成绩提升、坚持率）。本实验的"计划完成情况"
只能是**模拟推演**，不能写成真实用户数据。

---

## 1. 研究问题与实验假设

### 1.1 研究问题

- **RQ1（记忆层）**：系统从历史反馈中生成的长期记忆，是否准确、可追溯、在证据不足时保持克制？
- **RQ2（状态评估层）**：注入记忆后，系统对"当前学习状态"的判断是否更准确，尤其是
  反复困难、逐步改善、历史与当前冲突、主题切换这四类情形？
- **RQ3（计划层）**：记忆是否让计划更贴合学习者状态（难度、时长、针对性、可执行）？
- **RQ4（系统层）**：这些收益的代价是什么（模型调用次数、生成延迟、运行错误率、提示长度）？

### 1.2 实验假设与判定规则

| 编号 | 假设 | 主要对比 | 支持判据（预先登记） |
| --- | --- | --- | --- |
| H1 | 长期记忆提升计划的"重复困难针对性" | D vs B | 7 个样本的配对均值差 ≥ 0.5（5 分制），Wilcoxon 符号秩 p < 0.05（Holm 校正），Cliff's δ ≥ 0.33 |
| H2 | 长期记忆提升"状态匹配度" | D vs B、C vs A | 同上；两个对比至少一个成立 |
| H3 | 只有长期记忆、没有短期上下文时（C），在新用户与主题切换样本上更容易误用历史 | C vs D（S5、S7） | C 组"过度断言/记忆误用"发生率高于 D 组，且差异在样本级可复现 |
| H4 | D 相对 B 的收益在"冲突"与"改善"样本上最明显 | D vs B（S3、S6） | 这两个样本的差值方向一致且大于其余样本中位数 |
| H5 | 记忆注入不增加模型调用次数，但增加延迟与提示长度 | 四组 | 调用次数差异不显著；延迟与提示 token 中位数 D > B ≈ C > A |
| H-risk | 记忆注入会诱发"把历史说成当前"的过度断言 | 四组 | 用"无证据主张率 / 过度断言率"衡量，D 若升高则作为负向结果如实报告 |

判定采用"**支持 / 未观察到 / 反向**"三档，不做"显著即证明"的表述。样本量小，
结论一律限定为"在本实验的 7 个模拟样本与单一模型下"。

### 1.3 与现有实现的边界（必须先说清楚）

现有系统**没有**一个独立的"学习状态评估模型"。状态判断实际来自三处程序规则：

1. `plan_service.derive_difficult_previous_task()`（`plan_service.py:111`）：最新一条反馈是
   `partial` 且原因是"任务难度太高"时，`ai_service.build_plan_prompt()` 追加硬规则
   `difficulty=easy / question_count<=6 / estimated_minutes<=45`。
2. `memory_context.collect_short_term()` 的统计摘要（近 7 天完成率、困难主题、最近 3 条反馈）。
3. 长期记忆自身的 `status / strength / episode / evidence_count` 字段（`memory_service.py:1084`
   的状态机与 §5.5 强度公式）。

因此本实验把"学习状态评估"操作化为 **①+②+③**，而不是假想一个不存在的评估模块。
由此带来一个**无法完全解耦**的限制：B 组已含 ①②，D 组 = B + ③。为了单独估计 ①② 的作用，
另设探索组 **B0**（只有原始反馈行，无统计摘要、无 difficulty 硬规则）。B0 不参与主假设检验。

---

## 2. 四组对照实验设计

### 2.1 复用的代码接缝（不新造提示词）

四组共用同一个组装函数 `agent_plan_draft._build_draft_user_content()`
（`agent_plan_draft.py:385`），只改关键字参数。该函数保证：

- `short_term_block` 为 `None` 时**不改变原消息任何字节**（`agent_plan_draft.py:399-400`）；
- `memory_result` 为空时不出现"真实长期学习记忆检索结果"区块（`:401-402`、`:354-358`）；
- 计划字段规则复用 `ai_service.build_plan_prompt()`（`ai_service.py:159`），不出现第二套规则。

长期记忆区块由 `_memory_block()`（`agent_plan_draft.py:354`）渲染，内容只来自
`agent_tools.get_learning_memory()`（`agent_tools.py:222`）→ `memory_service.retrieve()`
（`memory_service.py:2031`）的真实结果。

### 2.2 条件矩阵

| 槽位 | A 无记忆 | B 短期上下文 | B0 仅原始反馈（探索） | C 长期记忆 | D 完整方案 |
| --- | --- | --- | --- | --- | --- |
| `user_message` | 相同 | 相同 | 相同 | 相同 | 相同 |
| `feedback_result["feedback"]` | 空 | 近 7 天（≤20 条） | 近 7 天（≤20 条） | 空 | 近 7 天（≤20 条） |
| `feedback_result["summary"]` | `{}` | 统计摘要 | `{}` | `{}` | 统计摘要 |
| `difficult`（难度硬规则） | `False` | 由最新反馈计算 | `False` | `False` | 由最新反馈计算 |
| `short_term_block` | `None` | `render_short_term(collect_short_term(REF_DATE))` | `None` | `None` | 同 B |
| `memory_result` | `None` | `None` | `None` | `get_learning_memory(limit=5)` | `get_learning_memory(limit=5)` |
| `knowledge_results` | 固定 | 固定 | 固定 | 固定 | 固定 |
| `search_failed` | `False` | `False` | `False` | `False` | `False` |

说明：

- **知识库槽不是实验变量**。每个样本预检索一次（同一查询、同一 top-k），四组复用**同一份**
  结果；若该样本在知识库中无命中，则四组统一为空结果并记录"本次无知识库结果"。
- **`memory_result` 采用"全类型、无主题过滤、limit=5"**，即
  `agent_tools.get_learning_memory(limit=5)`，与生产中最常见的调用方式一致。这样 C/D 会同时
  看到多个主题的记忆，才能测出"会不会把 A 主题的困难套到 B 主题"（S5）。
- **与生产的偏差（必须写进局限）**：生产链路由 Agent 自行决定是否调用记忆 Tool、传什么参数
  （`agent_plan_draft.py` 研究阶段），实验固定为"总是注入 top-5"，对 D 是**乐观设置**，
  会高估记忆的收益。为此设敏感性变体 `C-subj / D-subj`：按当前主题 `subject` 过滤后再取，
  仅作补充分析，不替换主结果。
- A 组走 `_build_draft_user_content()` 的"无短期、无记忆"分支（`agent_plan_draft.py:441`），
  与生产"空历史新用户"的提示逐字节一致；这一点用单元断言固定。

### 2.3 组间可比性

- 同一 `user_message`、同一历史种子、同一模型与版本、同一 `temperature`（建议 `0.7`，
  记录 `top_p` 实际值）、同一 `max_tokens`、同一系统提示 `plan_draft_system`。
- 生成顺序**按样本分块随机化**（同一趟实验内打乱条件顺序，用固定随机种子记录）。
- 每次生成记录 `prompt_sha256`（对最终 `messages` 列表做哈希），并断言：同一 (样本, 条件)
  的重跑哈希一致；不同条件之间只在设计槽位上有差异（人工抽查 1 个样本的 diff）。
- 温度不为 0 时，重跑哈希相同但输出会不同，这是重复实验要测的部分。

---

## 3. 模拟样本设计

### 3.1 统一的种子格式

参考基准日 `REF_DATE = 2026-11-12T21:00:00`，所有历史用"相对天数"描述，负数表示过去。
反馈写入 `feedbacks` 表，字段与 `database.py:43` 一致：
`task / estimated_minutes / actual_minutes / status / reason / completed_subtasks /
total_subtasks / completed_questions / total_questions / created_at / plan_id`。

- `status ∈ {completed, partial, not_completed}`，`reason ∈ {任务时间太长, 任务难度太高,
  临时有事情, 今天状态不好, 其他}`（`memory_service.py:104` 附近枚举）。
- 主题由 `normalize_subject(task)`（`memory_service.py:170`）确定：剥离尾部"练习/复习/
  练习册/练习题/习题"、折叠空白、ASCII 转小写。**同一主题的多条反馈必须使用同一任务名**，
  否则会被归一化成不同 `subject_key`。
- 除 S4 外**不写 `plan_id`**：`time_pattern` 候选要求 `plan_id` 指向难度合法且
  `estimated_minutes > 0` 的计划行（`memory_service.py:528`），不建计划行就不会产生
  `time_pattern` 记忆。这是有意的夹具设计（历史上"反馈没有关联计划"很常见），
  同时把 `time_pattern` 的检验集中到 S4。

### 3.2 七个样本（+1 个空历史变体）

每个样本给出：历史反馈、当前学习情况、预期记忆、预期状态判断、预期计划方向、评测要点。
预期记忆的强度是按 §5.5 公式估算的**设计预期区间**，实际值以夹具跑出的为准并写入记录；
若实际值与区间不符，先当作"实现或夹具问题"排查，不得直接改预期迁就结果。

---

#### S1 反复出现同一种学习困难

**目标**：检验系统能否把 5 次同因失败识别为"稳定困难"，并在计划里真正降难度。

**历史反馈**（任务名统一为 `一元二次方程练习` → `subject_key = 一元二次方程`）

| 天数 | estimated | actual | status | reason | 题数 |
| --- | --- | --- | --- | --- | --- |
| -24 | 45 | 70 | not_completed | 任务难度太高 | 2/10 |
| -19 | 45 | 65 | not_completed | 任务难度太高 | 3/10 |
| -14 | 40 | 60 | partial | 任务难度太高 | 6/10 |
| -9 | 40 | 58 | partial | 任务难度太高 | 5/10 |
| -4 | 30 | 55 | not_completed | 任务难度太高 | 4/8 |

**当前学习情况**：`user_message` = "明天大概有 60 分钟，想继续练一元二次方程，帮我安排一下。"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| difficulty | 一元二次方程 | 5 次因难度太高未完成（30 天内），无"初步观察"前缀 | active | ≈0.90 |
| task_performance | 一元二次方程 | 5 次完成率 0 | active | 0.70–0.80 |
| task_topic | 一元二次方程 | 5 次出现，带"初步观察：" | active | 0.70–0.80 |

**预期状态判断**：存在**稳定、未解决**的难度型困难；最近一条反馈是 `not_completed`，
因此 `difficult` 硬规则**不会**触发（该规则只认 `partial`），难度约束只能由记忆提供——
这正是本样本的关键。

**预期计划方向**：`difficulty=easy`、`estimated_minutes<=45`、`question_count<=6`、
拆成 2 个子任务、先补"配方法/判别式"再做题、给出可核对的完成标准。

**评测陷阱**：A/B 组很可能给出 8–10 题、60 分钟的计划；若 D 组仍如此，记为"重复困难未针对"。

---

#### S2 同一知识点多次失败，但归因是时间而非难度

**目标**：检验系统能否区分"难度问题"与"时间预估问题"，不生成错误的难度记忆。

**历史反馈**（任务名统一 `英语定语从句练习` → `subject_key = 英语定语从句`）

| 天数 | estimated | actual | status | reason | 题数 |
| --- | --- | --- | --- | --- | --- |
| -21 | 40 | 75 | not_completed | 任务时间太长 | 6/10 |
| -17 | 40 | 70 | not_completed | 任务时间太长 | 5/10 |
| -13 | 40 | 68 | not_completed | 任务时间太长 | 6/10 |
| -9 | 35 | 62 | partial | 任务时间太长 | 5/10 |
| -5 | 30 | 45 | completed | — | 8/8 |
| -2 | 35 | 58 | partial | 任务时间太长 | 5/8 |

**当前学习情况**：`user_message` = "明天有 90 分钟，想把定语从句再练一轮。"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| **difficulty** | — | **不应生成**（原因是时间，不是难度） | — | — |
| task_performance | 英语定语从句 | 6 次完成率约 0.17 | active（-2 的支撑把 weakened 拉回 active，需实测确认） | 0.50–0.65 |
| task_topic | 英语定语从句 | 6 次出现，无"初步观察"前缀 | active | ≈0.90 |

**预期状态判断**：完成率低的主因是**单次任务过长**（平均实际 ≈ 预估的 1.75 倍），
不是难度；`difficult=False`（最新反馈是 `partial` + 时间原因）。

**预期计划方向**：单次 25–35 分钟、拆 2 段、总量控制在 90 分钟以内、题量降到 6 题左右、
保留 `difficulty=medium`。

**评测陷阱**：若系统生成 difficulty 记忆或把计划压到 `easy`，记为"困难归因错误"。

---

#### S3 近期学习表现逐渐改善

**目标**：检验系统会不会把"正在改善"误判成"持续困难"。

**历史反馈**（任务名统一 `Python列表推导式练习` → `subject_key = python列表推导式`）

| 天数 | estimated | actual | status | reason | 题数 |
| --- | --- | --- | --- | --- | --- |
| -26 | 40 | 65 | not_completed | 任务难度太高 | 4/8 |
| -22 | 40 | 60 | partial | 任务难度太高 | 5/8 |
| -18 | 40 | 55 | partial | 任务难度太高 | 6/8 |
| -13 | 35 | 40 | completed | — | 8/8 |
| -8 | 35 | 32 | completed | — | 8/8 |
| -3 | 35 | 28 | completed | — | 8/8 |

**当前学习情况**：`user_message` = "明天 45 分钟，想继续练列表推导式，可以加点难度吗？"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| difficulty | python列表推导式 | 3 次因难度太高未完成，带"初步观察：" | weakened | 0.05–0.20 |
| task_performance | python列表推导式 | 6 次完成率 0.5 | weakened | 0.15–0.30 |
| task_topic | python列表推导式 | 6 次出现，无前缀 | active | ≈0.90 |

**预期状态判断**：**改善中**。记忆内容是"历史 0.5 完成率"，且 `contradict_count=3`；
最近 3 次全部完成、用时低于预估。正确表述是"历史上有困难，最近已连续完成"。

**预期计划方向**：`difficulty=medium`（可小幅提升），35–45 分钟，6–8 题，明确指出"最近三次
已完成，本次适度加难"。

**评测陷阱**：把 `task_performance` 的 0.5 直接说成"你现在完成率只有一半"= 过度断言（E1）。

---

#### S4 学习时间分布存在规律

**目标**：检验 `time_pattern` 记忆的生成与使用（需要 `plan_id` 关联计划行）。

**计划行**（5 条，`difficulty=medium`、`estimated_minutes=30`、`status=completed`、
`plan_date` 与反馈同日、`subtasks_json` 给 2 个子任务）

| 计划日 | 任务名 | 反馈 status | actual |
| --- | --- | --- | --- |
| -20 | 英语阅读练习 | partial（任务时间太长） | 62 |
| -15 | 英语阅读练习 | partial（任务时间太长） | 68 |
| -10 | 英语阅读练习 | completed | 71 |
| -6 | 英语阅读练习 | partial（任务时间太长） | 65 |
| -2 | 英语阅读练习 | completed | 66 |

**当前学习情况**：`user_message` = "明天晚上有 1 小时，帮我排英语阅读。"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| time_pattern | medium | "中等 任务近期 5 次实际用时约为预估的 2.2 倍"，带"初步观察：" | active | 0.50–0.65 |
| task_performance | 英语阅读 | 5 次完成率 0.4 | weakened | 0.25–0.35 |
| task_topic | 英语阅读 | 5 次出现，带"初步观察：" | active | 0.50–0.65 |

**预期状态判断**：时间预估系统性偏乐观（中位数比值 ≈2.2），不是难度问题。

**预期计划方向**：`estimated_minutes` 落在 60–70（或题量减半），`difficulty=medium`，
在 `reason` 里说明"按历史实际用时 2.2 倍调整"。

**评测陷阱**：计划仍写 30 分钟 = 忽略时间分布记忆；把原因归为"你基础差" = 错误归因。

---

#### S5 学习主题发生变化

**目标**：检验系统会不会把旧主题的困难迁移到新主题。

**历史反馈**

| 天数 | 任务名 | estimated | actual | status | reason |
| --- | --- | --- | --- | --- | --- |
| -26 | 线性代数练习 | 45 | 70 | not_completed | 任务难度太高 |
| -22 | 线性代数练习 | 45 | 68 | partial | 任务难度太高 |
| -19 | 线性代数练习 | 40 | 66 | not_completed | 任务难度太高 |
| -15 | 概率论练习 | 40 | 45 | completed | — |
| -12 | 概率论练习 | 40 | 42 | completed | — |
| -8 | 概率论练习 | 40 | 60 | not_completed | 临时有事情 |
| -4 | 概率论练习 | 40 | 38 | completed | — |
| -1 | 概率论练习 | 35 | 36 | completed | — |

**当前学习情况**：`user_message` = "这周想主攻概率论，明天 50 分钟，安排一次练习。"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| difficulty | 线性代数 | 3 次因难度太高未完成，带"初步观察：" | active | 0.40–0.55 |
| task_performance | 线性代数 | 3 次完成率 0 | active | 0.25–0.35 |
| task_topic | 线性代数 | 3 次出现，带"初步观察：" | active | 0.40–0.55 |
| task_performance | 概率论 | 5 次完成率 0.8 | active | 0.40–0.55 |
| task_topic | 概率论 | 5 次出现，带"初步观察：" | active | 0.50–0.65 |
| **difficulty** | **概率论** | **不应生成** | — | — |

**预期状态判断**：当前主题是**概率论**，表现良好（完成率 0.8）；线性代数的困难是**历史、
另一个主题**，不得作为当前难度的依据。全类型 top-5 注入很可能把线性代数记忆也带进来，
这正是考点。

**预期计划方向**：概率论、`difficulty=medium`（不因线代记忆降到 easy）、45–50 分钟、
6–8 题、可以提一句"线代的历史困难与本次无关"。

**评测陷阱**：把计划压成 `easy`、或在 `reason` 里写"你数学基础薄弱" = 跨主题误用（E1）。

---

#### S6 历史记忆与当前反馈冲突

**目标**：检验"记忆 vs 最新反馈"冲突时，系统是否以当前为准、是否降低断言强度。

**历史反馈**（任务名统一 `高等数学极限练习` → `subject_key = 高等数学极限`）

| 天数 | estimated | actual | status | reason | 题数 |
| --- | --- | --- | --- | --- | --- |
| -27 | 45 | 75 | not_completed | 任务难度太高 | 3/10 |
| -24 | 45 | 72 | not_completed | 任务难度太高 | 4/10 |
| -21 | 40 | 68 | partial | 任务难度太高 | 5/10 |
| -18 | 40 | 65 | partial | 任务难度太高 | 5/10 |
| -15 | 40 | 62 | partial | 任务难度太高 | 6/10 |
| -1 | 40 | 20 | completed | — | 8/8 |

**当前学习情况**：`user_message` = "明天 40 分钟，极限这块继续练，上次做得挺顺。"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| difficulty | 高等数学极限 | 5 次因难度太高未完成 | **weakened** | 0.35–0.50 |
| task_performance | 高等数学极限 | 6 次完成率约 0.17 | weakened | 0.35–0.50 |
| task_topic | 高等数学极限 | 6 次出现，无前缀 | active | ≈0.90 |

**预期状态判断**：历史困难已被最近一次成功**部分反驳**，状态应为 `weakened`；
`contradict_count=1`；`difficult=False`（最新反馈是 completed）。

**预期计划方向**：`difficulty=medium`（不必强制 easy），约 40 分钟、6 题，
明确写"历史 5 次未完成，但最近一次已完成且用时 20 分钟，本次按中等难度试探"。

**评测陷阱**：仍按 easy + 30 分钟安排（过度保守，E4）或直接说"你一直卡在极限"
（把历史说成当前，E1）。

---

#### S7 缺乏足够历史证据的新用户

**目标**：检验"证据不足"时系统是否避免错误记忆主张。

**历史反馈**（任务名统一 `数据结构练习` → `subject_key = 数据结构`，只有 2 条）

| 天数 | estimated | actual | status | reason | 题数 |
| --- | --- | --- | --- | --- | --- |
| -6 | 30 | 50 | not_completed | 任务难度太高 | 3/8 |
| -2 | 30 | 48 | partial | 任务难度太高 | 4/8 |

**当前学习情况**：`user_message` = "刚开始准备数据结构，明天 30 分钟，先带我入门。"

**预期记忆**

| 类型 | subject_key | 预期内容要点 | 预期状态 | 预期 strength |
| --- | --- | --- | --- | --- |
| difficulty | 数据结构 | 2 次未完成，**必须**带"初步观察：" | active | 0.35–0.50 |

`task_topic` 需要 3 次出现，因此**不应生成**。

**预期状态判断**：只有 2 条证据，只能算"初步观察"，不能当作稳定困难。

**预期计划方向**：`difficulty=easy`、25–30 分钟、4 题、先摸底；说明"样本还少，先试试水"。

**S7-empty 变体**：同一句 `user_message`，**0 条历史**。预期：记忆 0 条；
D 组答案**不得**出现"根据你的学习记忆/你之前总是"之类断言（无证据主张率 = 0 为达标）。

---

### 3.3 样本清单与覆盖矩阵

| 样本 | 场景 | 关键考点 | 主要对比 |
| --- | --- | --- | --- |
| S1 | 反复同一种困难 | 稳定困难识别、降难度 | D vs B |
| S2 | 多次失败但归因时间 | 难度/时间区分、不生成错误记忆 | D vs B、A vs C |
| S3 | 逐步改善 | 历史与当前区分、不过度断言 | D vs B |
| S4 | 时间分布规律 | `time_pattern` 生成与使用 | C vs A、D vs B |
| S5 | 主题切换 | 跨主题不迁移 | C vs D |
| S6 | 记忆与当前冲突 | 冲突处理、降权 | D vs B |
| S7 / S7-empty | 新用户、证据不足 | 不编造、标注"初步观察" | C vs D |

### 3.4 数据标记要求

所有样本文件、夹具库、结果文件必须带：

```json
{"data_kind": "simulated_learner_fixture", "is_real_user_data": false,
 "designed_by": "...", "design_date": "2026-11-12"}
```

报告与图表标题统一加"（模拟样本）"。禁止把样本称为"用户数据"或"真实学习者"。

---

## 4. 评价指标与计算方式

评分维度分两类：**自动指标**（脚本可判定，可复现）与**人工指标**（盲评 1–5 分）。
人工指标不得由 LLM 单独给出最终分（见 §5.4）。

### 4.1 记忆层

设样本 `s` 的期望记忆集合 `G_s`（按 `(memory_type, subject_key)` 唯一），系统在
`REF_DATE` 重建后的 `active/weakened` 记忆集合 `P_s`。

| 指标 | 定义 | 计算 | 类型 |
| --- | --- | --- | --- |
| M1 记忆识别准确性 | 类型内 P/R/F1 | 对每个类型 `t`：`P_t=|P∩G|/|P|`、`R_t=|P∩G|/|G|`、`F1=2PR/(P+R)`；再对 4 类取宏平均；`task_topic` 单独报告（它最容易，混在一起会掩盖 difficulty 的错误） | 自动 |
| M2 证据来源可追溯性 | 每条记忆的证据是否都真实存在 | ①合法率 = 所有 `evidence_feedback_ids` 都属于该样本种子反馈的比例（目标 100%）；②证据集合 Jaccard = `|E_pred∩E_gold|/|E_pred∪E_gold|` 的均值；③`contradict_count` 与期望完全一致的比例 | 自动 |
| M3 无证据时避免错误主张 | 空历史/弱证据下不编造 | S7-empty：`|P|=0` 为 1/0；S7：`content` 必须以"初步观察："开头且 `strength<=0.6`；D 组答案中"根据你的学习记忆/你之前总是/你一直"等断言出现次数 | 自动 + 人工复核 |
| M4 记忆状态正确性 | `status`、`strength` 落在预期区间 | 每样本每记忆逐项核对，报告一致条数/总条数；不一致必须给出原因分类（实现问题 / 夹具预期错误） | 自动 + 人工确认 |

> M1 的 `subject_key` 必须用 `normalize_subject()` 的实际输出比较，不能用人工写的主题名。

### 4.2 学习状态评估层

| 指标 | 定义 | 计算 | 类型 |
| --- | --- | --- | --- |
| S1 困难识别准确性 | 是否识别出"存在/不存在难度型困难" | 以样本 gold（S1/S6/S7 有，S2/S3/S4/S5 无）为标签，系统侧取两个信号：`difficulty` 记忆是否存在、答案是否把原因归为难度；两信号分别算 accuracy，并报告不一致的样本 | 自动（记忆）+ 人工（答案归因） |
| S2 学习表现判断合理性 | 对近期完成率、趋势、用时倍数的描述是否与事实一致 | 人工 1–5 分；另做"数值一致性"自动检查：答案中若出现完成率/倍数，须在事实值 ±10% 内（未提及则 N/A） | 人工 + 自动 |
| S3 当前状态与历史状态区分 | 是否把历史说成当前 | 对 S3、S6 每份输出判 0/1（0=清楚区分，1=混用）；另用关键词筛查（"一直/总是/始终/仍然" + 记忆主题）作辅助，最终以人工为准 | 人工（双人） |
| S4 冲突处理 | 冲突样本是否以最新反馈为准 | S6 专项：计划难度、时长、`reason` 三点逐项判定，报告"完全正确/部分/错误" | 人工 |

### 4.3 学习计划层（人工盲评 1–5 分）

| 指标 | 1 分 | 3 分 | 5 分 |
| --- | --- | --- | --- |
| P1 状态匹配度 | 与样本状态明显矛盾（如改善样本仍按失败处理） | 大致合理但遗漏关键趋势 | 准确反映当前状态与趋势，并说明依据 |
| P2 任务难度合理性 | 难度/时长/题量与 gold 方向相反 | 方向对但某一项越界 | 三项全部落在 gold 区间且理由充分 |
| P3 重复困难针对性 | 完全没提历史困难 | 提到但措施泛泛 | 针对具体卡点给出可操作步骤 |
| P4 计划可执行性 | 子任务时间/题数不自洽 | 自洽但标准不可核对 | 自洽、标准可核对、单步时长达标 |

P3 在 S7/S7-empty 上记 N/A，不计入该样本均值。
打分者只看到：样本 gold 事实卡（含历史反馈表与预期状态）+ 被打乱顺序的输出；
看不到条件标签、看不到预期计划文本。

### 4.4 系统层

| 指标 | 定义 | 计算 | 类型 |
| --- | --- | --- | --- |
| Y1 计划完成情况 | **模拟**完成率 | 主指标用"结构可保存率"=`validate_plan_for_save()`（`agent_tools.py:438`）通过数/总数；辅助指标用确定性学习者模拟器：难度 ≤ 该样本能力等级 且 时长 ≤ 可用时长 记为完成。**必须标注"模拟推演"** | 自动 |
| Y2 模型调用次数 | 每次输出消耗的对话调用数 | 起草 1 次 + 修正 0–2 次（`agent_plan_draft.MAX_CORRECTIONS=2`）；报告均值/中位数/最大值 | 自动 |
| Y3 生成延迟 | 端到端墙钟时间 | 从发出请求到拿到合法 JSON，排除夹具构建与人工评分；报告 median / P95 | 自动 |
| Y4 运行错误率 | 失败输出占比 | (模型错误 + 超时 + 两次修正后仍非法 JSON + 校验不过)/总数 | 自动 |
| Y5 提示规模 | 提示长度与 token | `len(messages)` 与 usage 中的 prompt tokens；报告中位数 | 自动 |

### 4.5 指标与假设的对应

H1→P3；H2→P1（+S2）；H3→S3/S4（S5、S7 专项）；H4→S3 与 S6 的 P1/P3；H5→Y2/Y3/Y5；
H-risk→S3（当前/历史区分）、S7（无证据主张）、M3。

---

## 5. 实验控制方法

### 5.1 固定项

| 项目 | 固定方式 |
| --- | --- |
| 代码版本 | 记录 `git rev-parse HEAD`；实验期间不改业务代码（发现 bug 记 issue，不在实验中途改） |
| 模型 | 单一模型；记录 provider / model 名 / 版本 / `temperature` / `top_p` / `max_tokens` |
| 系统提示 | 同一份 `plan_draft_system`（`prompt_registry`） |
| 输出契约 | 同一份 `answer + plan` JSON 契约（`agent_plan_draft.py:412-416`） |
| 知识库 | 每样本预检索一次，四组复用同一结果（见 §2.2） |
| 夹具库 | 临时 SQLite（如 `_exp_fixture.db`），**绝不写真实 `feedback.db`**（参考 `agent_eval.py` 的隔离做法） |
| 温度与重复 | `temperature=0.7`，每 (样本, 条件) 生成 5 次 |

### 5.2 唯一变量

只有 §2.2 矩阵中的槽位变化；`user_message`、知识结果、系统提示、模型参数全部不变。

### 5.3 随机性与重复

- 生成：每 (样本, 条件) 重复 **5 次**，共 7×4×5 = **140** 份（S7-empty 若纳入则 +20）。
- 生成顺序：按样本分块，块内四条件顺序随机（固定随机种子，记录在 `config.json`）。
- 人工盲评：从每 (样本, 条件) 的 5 份中随机抽 **3** 份，共 7×4×3 = **84** 份，
  两位评分者独立评；抽签种子固定。

### 5.4 人工盲评与 LLM 辅助

- 盲法：输出文件按 `blind_id` 命名（如 `S3-7f2a`），评分表不含条件列；评分完成后才由脚本回填映射。
- 两位评分者（作者 + 一名同学），先各自独立评 6 份校准样本，讨论统一尺度后再正式评。
- 一致性：1–5 分用二次加权 Cohen's κ，目标 κ ≥ 0.6；0/1 判定用 Cohen's κ。
  分歧 > 1 分或 0/1 相反时，由第三人（或双方讨论）裁决，裁决结果单独记录。
- LLM 作为辅助评分者：同一 rubric、`temperature=0`、评 3 次取多数；只报告其与人工均分的
  Spearman 相关与 κ，**不作为主结论依据**，也不参与假设检验。

### 5.5 统计方法

- 描述统计：每条件每指标的均值、标准差、中位数、95% bootstrap 置信区间（按样本重采样 10000 次）。
- 主检验：以"每样本均值"为单位（n=7），四条件用 Friedman 检验；显著后做
  Wilcoxon 符号秩两两比较（Holm 校正）。效应量用 Cliff's δ（阈值 0.147/0.33/0.474 对应小/中/大）。
- 二分类指标：Cochran's Q + McNemar（精确检验）。
- **功效说明**：n=7 的配对设计只能发现较大效应；报告效应量与置信区间，不把"p>0.05"写成
  "没有差异"。全部结论标注为探索性。

### 5.6 公平性检查（实验后必做）

1. 同一 (样本, 条件) 的 5 份 `prompt_sha256` 必须一致；
2. 抽查 S1 的四组 prompt diff，确认差异只在 §2.2 允许的槽位；
3. 确认四组的 `knowledge_results` 字节一致；
4. 确认真实 `feedback.db`、`knowledge/`、`.rag_cache/` 的 mtime/哈希未变；
5. 确认结果文件不含 API key、`.env` 内容或完整系统提示之外的隐私字段。

---

## 6. 实验操作流程

1. **冻结基线**：`git fetch`，记录 `main` SHA；从 `main` 拉实验分支；不改业务代码。
2. **建夹具**：按 §3 写 `samples/*.json`，由 `seed_fixture.py` 在临时库建表、插入计划与反馈，
   写入 `fixtures/<sample>.db` 与 `fixtures/<sample>.json`（记录插入后的行 id 与哈希）。
3. **物化记忆**：对每个夹具跑一次"重建"（`memory_service` 的 rebuild 入口，`as_of=REF_DATE`），
   导出 `memories.json`，与 §3 的预期逐条比对，产出 `memory_check.md`（一致/不一致+原因）。
   不一致时先修夹具或报实现问题，**不许改预期来迁就**。
4. **构建条件**：对每个 (样本, 条件) 调 `_build_draft_user_content()` 生成 `messages`，
   计算 `prompt_sha256`，存 `prompts/<sample>/<condition>.json`（含完整 messages，便于复核）。
5. **生成**：按随机顺序跑 140 次，落 `runs/<run_id>.jsonl`（一行一份输出，含原始文本、
   解析后的 `answer/plan`、usage、latency、错误类型）。
6. **自动判定**：跑 M1–M4、Y1–Y5 与 `check_grounding`，输出 `auto_metrics.csv` 与失败清单。
7. **盲评**：生成 84 份盲评包 → 两位评分者独立打分 → 记录 `ratings_raw.csv`（含评分者 id、
   盲 id、四项分数、0/1 判定、备注）→ 计算一致性并裁决。
8. **分析**：生成描述统计表、箱线图、假设检验结果、错误分类统计、个案摘录（S3/S6/S7）。
9. **归档**：`experiments/graduation/` 下提交配置、样本、prompt 哈希、聚合指标、评分表、
   分析脚本与报告；原始输出若过大可只提交 JSONL 压缩包并在报告里给出校验和。

预计规模：140 次生成（每次 1–3 次模型调用）、约 1–2 小时机器时间；人工评分约 84×4 项。

---

## 7. 数据记录模板

### 7.1 生成记录（JSONL，一行一条）

```json
{
  "run_id": "20261120T140012Z-S1-D-r3",
  "sample_id": "S1",
  "condition": "D",
  "repeat": 3,
  "model": {"provider": "openai_compatible", "name": "<model>", "temperature": 0.7, "top_p": 1.0},
  "base_sha": "<git sha>",
  "prompt_sha256": "…",
  "messages_path": "prompts/S1/D.json",
  "knowledge_result_ids": ["…"],
  "memory_injected": [{"memory_type": "difficulty", "subject_key": "一元二次方程", "strength": 0.9, "status": "active"}],
  "short_term_injected": true,
  "difficult_flag": false,
  "output_raw": "…",
  "answer": "…",
  "plan": {"task": "…", "estimated_minutes": 40, "difficulty": "easy", "question_count": 5,
            "reason": "…", "completion_criteria": "…", "subtasks": []},
  "validation": {"json_ok": true, "plan_valid": true, "grounding_ok": true, "corrections_used": 0},
  "usage": {"prompt_tokens": 0, "completion_tokens": 0},
  "latency_ms": 0,
  "error": null,
  "data_kind": "simulated_learner_fixture"
}
```

### 7.2 盲评记录（CSV）

`blind_id, rater_id, P1, P2, P3, P4, S2, S3_flag, S4_verdict, notes`

其中 `P3` 可为空（N/A），`S3_flag ∈ {0,1}`，`S4_verdict ∈ {full, partial, wrong}`。

### 7.3 记忆核对表（Markdown/CSV）

`sample_id, memory_type, subject_key, expected_status, actual_status, expected_strength_range,
actual_strength, evidence_ok, contradicts_ok, verdict, note`

---

## 8. 结果分析方法

1. **主表**：四条件 × (P1–P4, S1–S3, Y1–Y5) 的均值/中位数/CI，附样本量。
2. **分样本表**：7 行 × 4 条件，保留每个样本的原始差值，避免只看总平均掩盖反向样本。
3. **假设检验表**：假设、对比、统计量、p（Holm）、效应量、结论档位（支持/未观察到/反向）。
4. **记忆质量表**：M1–M4 按类型拆分，列出每条不一致的记忆与原因。
5. **错误分类**：对全部输出按以下编码统计（可多标签）：
   - E1 过度断言（把历史/弱记忆说成当前事实）
   - E2 难度不匹配（方向与 gold 相反或越界）
   - E3 时间安排不合理（忽略用时倍数/可用时长）
   - E4 冲突未处理（无视最新反馈）
   - E5 无证据编造（新用户样本声称有历史）
   - E6 结构不合法（JSON/字段/子任务不自洽）
   - E7 主题漂移（把旧主题困难套到新主题）
6. **个案分析**：S3、S6、S7 各摘 2–3 段原文，展示"过度断言/正确降权"的具体措辞。
7. **代价分析**：Y2–Y5 与质量的权衡，回答"多花的延迟换来了什么"。
8. **负结果处理**：若 D 相对 B 无提升甚至更差，如实报告，并区分是"记忆没被用上"、
   "记忆内容错"还是"模型过度解读记忆"（用 E1/E4 比例与 prompt 检查佐证）。

---

## 9. 实验局限性

1. **样本是构造的**：7 个样本覆盖了目标场景，但不代表真实学习者分布；样本由本文档作者设计，
   存在"按实现能力出题"的偏向。
2. **单一模型、单次基线**：结论不能外推到其他模型或未来版本。
3. **样本量小**：n=7 的配对检验功效有限，只能发现较大效应；结论为探索性。
4. **D 组是乐观设置**：生产由 Agent 自行决定是否检索记忆，实验固定注入 top-5，
   会高估记忆收益；`C-subj/D-subj` 敏感性分析只能部分缓解。
5. **状态评估无法完全解耦**：B 已含程序统计与难度硬规则，H2 的"记忆贡献"含混；
   B0 只能事后补充。
6. **记忆规则是固定阈值**（`REQUIRED`/`STABLE`/30 天窗口/0.6 衰减），
   换阈值结论可能变化；实验不调参，也不为提升指标改阈值。
7. **`time_pattern` 覆盖有限**：只有 S4 有 `plan_id`，其余样本不产生该记忆。
8. **人工评分主观性**：两位评分者、84 份输出，κ 可能低于 0.6；低一致性本身要报告。
9. **LLM 辅助评分有偏**：只作辅助，但仍可能影响讨论；不参与检验。
10. **"计划完成情况"是模拟**：Y1 的完成率来自确定性模拟器，不是真实学习者行为。
11. **一次性横截面**：不测长期使用后的记忆演化（多轮衰减、resolved 后的复发）。
12. **语言与场景限定**：中文、中学—大学基础学科，未覆盖多模态或团队学习。
13. **知识库槽虽固定，但覆盖不均**：某些样本可能无知识命中，会削弱"计划依据"的丰富度。

---

## 10. 交付物与"不做"清单

**本实验需要新增的文件（全部属于实验脚手架，不改产品行为）**

```
experiments/graduation/
  README.md                  # 如何复现
  config.json                # 模型、温度、种子、阈值、REF_DATE
  samples/S1..S7.json        # 样本与 gold
  seed_fixture.py            # 建临时库、插计划/反馈
  build_prompts.py           # 调 _build_draft_user_content 生成 messages + 哈希
  run_generation.py          # 调模型、记录 JSONL
  check_memory.py            # M1–M4
  check_auto.py              # Y1–Y5 + check_grounding
  make_blind_pack.py         # 盲评包
  analyze.py                 # 统计与图表
  report/EXPERIMENT-REPORT.md
```

**明确不做**

- 不修改 `memory_service.py` / `memory_context.py` / `agent_plan_draft.py` /
  `langgraph_plan_workflow.py` 的业务逻辑；不为实验加开关、加字段、加表。
- 不修改 `docs/STAGE2-LEARNING-MEMORY-DESIGN.md` v16 基线。
- 不写真实 `feedback.db`，不碰 `knowledge/` 与 `.rag_cache/`。
- 不把模拟结果写成真实用户实验；不编造任何指标数值。

---

## 附录 A：现有能力 → 实验用途对照

| 能力 | 位置 | 实验用途 |
| --- | --- | --- |
| 记忆候选/阈值/窗口 | `memory_service.py:30-59` | 决定样本需要几条证据才能触发记忆 |
| 记忆模板 | `memory_service.py:76` | 预期 `content` 文案与"初步观察："前缀 |
| 强度公式 | `docs/STAGE2-LEARNING-MEMORY-DESIGN.md:925-960` | 预期 strength 区间 |
| 状态机 | `memory_service.py:1084` | 预期 active/weakened/resolved |
| 记忆重建 | `memory_service.py` rebuild 入口 | 夹具 → 记忆物化 |
| 检索 | `memory_service.py:2031` + `agent_tools.py:222` | C/D 的记忆注入 |
| 短期上下文 | `memory_context.py` collect/render | B/D 的短期块 |
| 提示组装 | `agent_plan_draft.py:385` | 四组的唯一差异点 |
| 计划规则 | `ai_service.py:159` | 四组共用的字段规则 |
| 计划归一/校验 | `plan_service.py` + `agent_tools.py:438` | Y1 与 E6 |
| 接地检查 | `agent_plan_draft.py:140` | E5 的自动信号 |
| 只读评测范式 | `agent_eval.py` | 夹具隔离、真实库不动、结果归档 |

## 附录 B：假设检验的"最小可判定"汇总

| 假设 | 指标 | 对比 | 最小可判定条件 |
| --- | --- | --- | --- |
| H1 | P3 | D vs B | Δ均值 ≥0.5 且 p<0.05（Holm）且 δ≥0.33 |
| H2 | P1 / S2 | D vs B 或 C vs A | 同上，至少一个对比 |
| H3 | S3_flag 率、E7 | C vs D（S5/S7） | C 的比率更高且在 5 次重复中方向一致 |
| H4 | P1/P3 差值 | D vs B（S3/S6） | 两样本差值同向且大于其余样本中位数 |
| H5 | Y2/Y3/Y5 | 四组 | 调用次数无显著差异；延迟与 prompt tokens 中位数排序 D>B≈C>A |
| H-risk | E1 率、S3_flag | 四组 | D 的比率不高于 B，或升高则如实报告为负向 |
