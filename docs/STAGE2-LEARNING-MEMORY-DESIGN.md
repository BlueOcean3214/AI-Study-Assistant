# 第二阶段：学习记忆管理 —— 方案设计（v8）

- 基线：`main` @ `fb22365`（第一阶段已合并）
- 代码阅读范围：`database.py`、`main.py`、`feedback_service.py`、`agent_tools.py`、`agent_loop.py`、`agent_schema.py`、`agent_dispatcher.py`、`agent_plan_draft.py`、`context_builder.py`、`langgraph_plan_workflow.py`、`rag_service.py`、`plan_service.py`、`prompt_registry.py`、`static/feedback.html`，以及 `test_agent_tools.py` / `test_agent_loop.py` / `test_database_migrations.py` / `test_feedback_plan_link.py`
- 本文件只提交设计，**不含任何代码改动**
- 标注：**【已实现】** 现有代码已有；**【新增】** 本方案要加；**【契约变更】** 有意改变既有行为

## 0. 修订记录

### v7 → v8（本轮，预审复核 P1/P2）

| # | 审查意见 | 处理 |
| --- | --- | --- |
| P1-1 | `REQUIRED` 已改按 samples，但 `STABLE` 与封顶仍按 `supports`，低完成率时「初步观察」期被拉长到双倍门槛 | §5.5 表：`STABLE` 与 `REQUIRED` **同单位**（task_performance 6 samples、time_pattern 8 samples）；公式引入 `unit`，`base` 与封顶都用 `unit` |
| P1-2 | §5.3.5 写入 `pending_started_at`，DDL 里没有这个字段 | 删掉该赋值，pending 只用 `pending_episode` + `pending_start_feedback_id` |
| P1-3 | §5.3.8「能复现」清单未限定 `now`，与末尾 now pass 自相矛盾 | 改为「**两侧注入同一 `now` 时**能复现：…」，并在"不能复现"里显式点名 now pass 的 `archived` 与事件 |
| P1-4 | neutral 与 direction 统计量的关系没写死，实现可能把 neutral 剔出中位数/完成率 | §5.2 补一句：**neutral 只影响 relation 与两个计数，不影响 direction 的统计量**；完成率分母与中位 ratio 用全部有效候选 |
| P2-1 | `episode_start_feedback_id = MIN(新轮 supports …)` 与 §5.3.1 定义不一致（首条证据可能是 contradicts） | 改为 `MIN(新轮全部 evidence 的 feedback_id)` |
| P2-2 | §5.2 第 5 条留着"或至少保证同事务一致"的退路，与"一律 COUNT"并存 | 删掉退路半句；并补 `last_supported_at` 与计数**同源**（取 supports 证据的 `MAX(created_at)`） |
| P2-3 | 用例数 131 应为 130 | §8.2 改「约 130 例」 |
| P2-4 | §10 括号里写「任务 3 +0.8」，实际 +0.1 | 改为 v7 +0.1、v8 +0（只改口径） |

### v6 → v7（已完成）

| # | 审查意见 | 处理 |
| --- | --- | --- |
| P0-1 | `active/weakened` 阶段增量证据未持久化：只加计数不插行，导致 `evidence_count` 与正式证据行数脱节、`MAX(feedback_id)` 不推进、重复调用无法靠唯一索引识别、`resolved` 时 `pending_start_feedback_id` 漏掉已影响结论的反馈 | §5.2 扩为**八条实现级规则**：relation 按当前 episode 已固定 direction 判定；`INSERT OR IGNORE`；`inserted=False` 读回核对 `relation/episode/plan_id`；不一致报 `data_inconsistency`；计数一律 `COUNT` 重算；同一反馈不重复计；晋升路径同规则；`episode` 必须等于写入轮次。§5.3.5 迁移表把"计数+1"改为"**追加证据行 + COUNT 重算**" |
| P0-2 | 统一伪代码 `get_memory(mtype, subject_key(trigger))` 对 `time_pattern` 不成立（其 key 是 `plans.difficulty`，且需跨 task） | §5.3.6 已加 `subject_key_for(mtype, trigger)` 分派（v6 完成），本轮**补测试**：两个不同 task 同 difficulty 进同一条记忆、无效 `plan_id` 不创建/不更新、其他三类仍按 task 查找；并明确"整类跳过不影响其他三种" |
| P1 | pending 缓冲区过期清理时机未定义，方向长期不可定时过期行长期留存、缓冲表无增长上限 | §5.3.3 新增**第 0 步**：每次 `_try_promote` 先删除 `created_at < as_of − 30d` 的 pending 行、条数计 `info.pending_expired`、只用剩余行算方向与门槛、与晋升同事务；§5.3.2 规则 5 与新增规则 9 写死"上界 = 窗口内候选数"；补 K10 |
| 连带 | 计数口径、测试矩阵、工期需同步 | §5.3.6「重复计数在物理层不可能」改为「服务层拦截 + 唯一索引兜底」；§8.2 扩 S11/D8–D9/E6/U19/B10/K10，用例数 124 → **131**；§10 任务 3 3.5 → 3.6 天，合计 **8.0 人日**；§11 新增"pending 缓冲区无界增长"风险行 |

### v5 → v6（已完成）

| # | 审查意见 | 处理 |
| --- | --- | --- |
| P0-1 | §5.2「增量只加计数不插行」与 `evidence_count` 定义、D1 断言、Tool 的 `evidence_feedback_ids` 直接矛盾；且轮次下界 `MAX(feedback_id)` 会落后，导致旧反馈被重新计入 | §5.2 写死「**一条被计入的反馈 = 一行 evidence**」，增量也插行；**所有计数一律 `COUNT` 重算，禁止 `+1`** |
| P0-2 | `memory_evidence.created_at` 口径未定义，而「contradicts 全部晚于最后一次 supports」依赖它；用 `now()` 则重建不可复现 | `created_at = 该反馈的 created_at`（即 as_of），写入 DDL 注释与 §5.3.3 第 4 步；B 组补「时间戳乱序/同秒」用例 |
| P0-3 | relation 映射有空档：`not_completed + reason=临时有事情`、`0.4<ratio<1.5` 既非 supports 也非 contradicts，实现只能自行猜 | §5.2 映射表新增 **neutral 列**并写死处理：不写证据、不计分子分母、晋升时删除并计 `info.neutral_dropped` |
| P1-1 | `task_performance` 的「≥3 条样本」与「REQUIRED=supports」叠加错位，low 方向实际要 6 条才生成，S3 用例永远不成立 | §5.5 新增**计入单位**行：`difficulty` 数 `supports`，`task_performance`/`time_pattern` 数 **samples**，`task_topic` 数出现次数；S3 改为断言"3 样本/2 supports/rate 0.33 → 生成 low" |
| P1-2 | 「重复计数在物理层不可能」不成立：pending 与 evidence 是两张表，重试旧反馈会先落缓冲区、晋升时撞唯一索引 | §5.3.2 新增第 7 条：写 pending 前查 evidence，已存在则跳过并计 `info.already_evidenced`；K 组断言改为"服务层拦截" |
| P1-3 | B1「逐表相等」与末尾 `_materialize_stale(all, now)` 冲突（运行时时钟） | `rebuild_all(now=None)` 允许注入；B1 用同一 `now` 比较，末尾 pass 单独断言；§5.3.8 明确这是唯一受运行时时钟影响处 |
| P1-4 | 迁移表缺 `resolved → archived`，且未说明既有 pending 缓冲区是否重置 | §5.3.5 补该行：保留既有 `pending_*` 与缓冲区，不重置；§5.3.4 同步「只在不存在时才固定」 |
| P1-5 | §5.3.6 伪代码用 `subject_key(trigger)` 取所有类型的 key，而 `time_pattern` 的 key 是 `plans.difficulty`，照抄会永不命中 | 伪代码改为 `subject_key_for(mtype, trigger)` 分派：`time_pattern` 走 `plan_id→plans.difficulty`，空/非法则整类跳过 |
| P2-1 | M1/M2 仍写「三表」；§5.3.4 第 3、4 条自相矛盾 | 统一"四张表"；改写为「不需要为归档补算事件，只需一次 now pass 让当前 stale 可见」 |
| P2-2 | 用例数与工期算错（各组相加 124 例，文档写 78–88；分项相加 7.9，文档写 7.7） | §8.2 改「约 124 例」；§10 开列分项算式，合计 **7.9 人日** |
| P2-3 | `test_feedback_plan_link.py` 行号写错 574；漏了 `:142` fixture 里的「时间不够」脏值 | 改为 `:575`；新增 `:142` 一行，明确**不修改**、作为 H/K 组脏数据来源 |

### v4 → v5（已完成）

| # | 审查意见 | 处理 |
| --- | --- | --- |
| R-P0-1 | pending 期间 direction 尚未确定，但与 `memory_evidence.relation` 不可变冲突（旧方向不能沿用、新方向不能猜） | 新增**第四张表 `memory_pending_evidence`**（只存 `memory_id/feedback_id/plan_id/episode`，**无 relation 列**）作为原始候选缓冲区；达门槛后**单事务一次性转正**（算方向 → 定 relation → INSERT 正式证据 → 删缓冲区 → 改记忆 → 写事件），绝不先写占位再 UPDATE（§3.1、§5.1、§5.3.2、§5.3.3） |
| R-P0-2 | `rebuild_all()` 未限制回放时的候选范围，可能读到未来反馈、也可能因重建时刻把历史反馈错误排除 | 候选规则唯一化：`as_of = trigger.created_at`、`candidate.id <= trigger.id`、`as_of−30d ≤ created_at ≤ as_of`、轮次下界照旧；在线与重建共用 `apply_feedback(trigger, as_of)`（§5.2、§4.2、§5.3.6）；新增 B6–B9 用例 |
| R-P0-3 | `archive_stale()` 是隐式事件、rebuild 会清空派生数据，导致 B 被放回旧 episode | stale 改为**派生判定** `as_of − last_supported_at > 60d`：处理每条反馈前先 `_materialize_stale(mem, as_of)`；`archive_stale()` 退化为 `as_of = now()` 的批量入口，不再是唯一归档途径；重建靠同一函数在回放中自然重现（§5.3.4） |
| R-P0-4 | §5.2 与 §5.3 存在两套流程，同一条反馈可能被重复计数 | §5.2 只保留「候选规则 + relation 映射」，流程统一为 §5.3.6 的 `apply_feedback` 伪代码；`_append_pending` 与 `_apply_current_episode` 互斥 + 两把唯一索引兜底，物理层防重复 |
| 连带 | 删除语义、事件字段、测试矩阵、工期需同步 | §3.4 改为**五表**同事务清空；`detail_json` 增必填 `as_of`；§8.2 新增 K 组、扩 U 至 U18、扩 B 至 B9、X 至 X6；§10 合计 6.8 → 7.7 人日 |

### v3 → v4（已完成）

| # | 审查意见 | 处理 |
| --- | --- | --- |
| P0-1 | pending episode 的存储与边界未定义：resolved / archived 后未达门槛的新反馈写哪个 episode、候选下界怎么推进；按「当前轮-1」查询会得到 episode 0，滚动窗口又会把旧轮证据卷回来 | §5.3 新增「待激活轮次（pending episode）」：进入 resolved / archived 时立即固定 `pending_episode = episode+1` 与 `pending_start_feedback_id = MAX(全轮 evidence.feedback_id)`；此后每条新反馈**立刻写入 `episode = pending_episode`**；只有 pending 轮 supports 达 `REQUIRED` 才把 `episode` 正式切到 pending 并恢复 `active`；旧轮证据的 episode/relation 永久不变。补 U10–U13 四条用例（含 21/22/23 连续三条的完整时序）。**（v5 修正：这批"立刻写入"的样本改存 `memory_pending_evidence` 缓冲区，见 v4→v5 R-P0-1）** |
| P1-1 | `rebuild_all()` 与 `archive_stale()` 的关系没说明 | §4.2 / §5.3：重建 = 按 id 回放原始反馈 + **回放结束后按当前时间重算 stale**；事件是重建生成的派生轨迹，不承诺复原用户历史上何时手动归档过 |
| P1-2 | `INSERT OR IGNORE` 不能把「忽略冲突」当成功 | §5.1：`insert_evidence()` 返回 `inserted`；为 False 时必须读回既有行核对 `relation/episode/plan_id`，一致才算幂等成功，不一致 → `data_inconsistency` |

### v2 → v3（已完成）

| # | 审查意见 | 处理 |
| --- | --- | --- |
| D1 | `reason` 条件必填是有意的契约收紧；`Literal` 校验发生在路由之前，文档写的校验顺序不成立；FastAPI 422 是 `detail` 结构，不带 `error_type` | §6.1 重写：校验顺序改为「Pydantic 422 → plan_id → reason_required」；明确**接受 FastAPI 默认 422 结构**、不自定义 `error_type`；把该变更标为【契约变更】并列测试 |
| D2 | `relation` 不应在普通重跑时 UPDATE 历史证据；`episode` 也不应被搬动 | §3.2/§5.1 改为**证据行不可变**：`(memory_id, feedback_id)` 一旦写入，`relation`/`episode` 禁止改写；普通 `update_from_feedback` 遇到不一致只报 `data_inconsistency`，不覆盖；`rebuild_all()` 明确为派生数据全量重建（事务内重建三表、按时间回放，事件 id 不承诺稳定） |
| D3 | `DELETE /feedback` 不应保留 `deleted` 事件（会被同事务删掉，或违反"清空就是清空"） | 删除 `deleted` 事件类型；§3.4/§3.5/§5.3/§8.2 统一为「同事务清空、不写任何事件」（v5 起为**五表**） |
| P0-A | `time_pattern` 的 `subject_key` 是 `plans.difficulty`，不能按任务名取候选 | §5.2 改为**按类型分别收集候选**：`difficulty/task_performance/task_topic` 按归一化 task；`time_pattern` 通过有效 `plan_id` JOIN `plans` 按 `difficulty` 跨 task 汇总；补跨 task 测试 |
| P0-B | episode 重开后滚动窗口会把旧证据重新选中并搬进新轮 | 新增 `learning_memories.episode_start_feedback_id` 与 `episode_started_at`；**候选下界 = 上一轮最大证据 id**，当前轮候选 `id > 上一轮 max(evidence.feedback_id)` 且 `id >= episode_start_feedback_id`；旧证据永不移动；`rebuild_all()` 按 id 顺序回放以重现同一批边界（**v4 把下界统一为 `pending_start_feedback_id`，见 §5.3**） |
| P0-C | `archived` 后单条反馈就能复活，绕过门槛 | §5.3 改为：复活**必须在新轮内重新满足 `REQUIRED`**；未达门槛保持 `archived`、不进入检索 |
| P1-D | 历史脏枚举只校验触发反馈，会污染其他统计 | §5.2 第 3 步：**每个候选逐条校验** `status/reason`，非法候选既不计分子也不计分母，计 `info.invalid_enum`；补"合法触发 + 同窗口非法历史行"测试 |
| P1-E | `task_topic` 文案说"计划/反馈中"，但候选只查 feedbacks | 文案收敛为「**近期反馈中** N 次出现」；数据来源只 `feedbacks`，不在本阶段合并 plans |
| P1-F | 短期上下文只进研究阶段消息，进不了真正起草的模型调用 | §2.2 改为**显式参数**传入 `_build_draft_user_content(..., short_term_block=...)`（`agent_plan_draft.py:233`），Native（`:441`）与 LangGraph（`langgraph_plan_workflow.py:97` 的 `node_prepare`）两条路径都传；补"最终 draft prompt 含当前计划"的断言；空上下文时**旧消息字节不变** |
| P1-G | 事件记录不幂等 | §5.3 末尾：事件只在**实际状态迁移或首次创建**时写；重复调用无变化不写；rebuild 重建事件、id 不承诺稳定 |

### v1 → v2（上一轮，已完成）

服务端枚举补齐、`time_pattern` 依赖 `plans.difficulty`、证据回溯补齐、`UNIQUE(memory_id, feedback_id)`、episode 状态机闭环、`DELETE /feedback` 级联、短期上下文接口修正、`task_topic` 改名、`preference` 移除、`strength` 改名与双门槛。

---

## 1. 现有记忆能力审查报告

### 1.1 结论

**当前系统没有任何长期记忆能力。** 全仓 `grep`「记忆 / memory」只命中两处注释与一个测试断言。现有「上下文」都只是每次重算的瞬时视图：

| 现有机制 | 代码位置 | 本质 | 是否算记忆 |
| --- | --- | --- | --- |
| 最近反馈读取 | `agent_tools.get_recent_feedback()` `agent_tools.py:147` → `database.get_feedback_since()` `database.py:279` | 按 `created_at >= cutoff` 现查，最多 20 条、窗口 1–30 天 | 否，原始数据视图 |
| 确定性统计 | `feedback_service.calculate_summary()` `feedback_service.py:12` | 对本次列表做计数/均值 | 否，瞬时聚合 |
| 知识库检索 | `rag_service.retrieve_chunks()` `rag_service.py:545`，语料 `knowledge/**/*.txt` | 教材公共知识，`TOP_K=3`、`MIN_SCORE=0.5` | 否，不是用户数据 |
| 上下文组装 | `context_builder.build_context()` `context_builder.py:85` | 去重 + 6000 字符预算 + 来源标签 | 否，无状态装配 |
| 计划持久化 | `plans` + `plan_service` | 用户确认后的单日计划 | 业务实体，不是记忆 |

### 1.2 逐项核实

1. **`feedbacks.plan_id` 已落地**【已实现】`database.py:145` 加列 + `idx_feedbacks_plan_id`；`main.py:378-398` 写库前校验计划存在与任务名一致；两个读取函数均返回 `plan_id`。
2. **`feedbacks` 真实结构**【已实现】12 列：`id, task, estimated_minutes, actual_minutes, status, reason, completed_subtasks, total_subtasks, completed_questions, total_questions, created_at, plan_id`。
3. **`created_at` 口径**【已实现】`datetime.now().isoformat()`，本地无时区字符串（`main.py:400`）；`database.py:285-286` 声明同格式下字典序等价时间序。**因此本方案的时间比较一律用字符串，但顺序回放与边界一律用自增 `id`**（避免同秒并列）。
4. **Agent 上下文构建**【已实现】`agent_loop.run_agent()` `agent_loop.py:257` 只有 system prompt + 用户消息，**不注入历史**；预算 `MAX_TOOL_CALLS=4`、`MAX_LOOP_TURNS=3`、`MAX_RUN_SECONDS=120`（`:24-28`）。
5. **近期反馈传递**【已实现】`get_recent_feedback(days)` → `_sanitize_feedback()`（`agent_tools.py:83`，字段白名单，**丢弃 `id` 与 `plan_id`**）→ `role=tool` 的 JSON。
6. **Dispatcher 限制**【已实现】`TOOLS` 是 3 个函数的显式字典（`agent_dispatcher.py:20-24`）；`_validate_arguments()`（`:44`）严格拒绝、不钳制；异常转 `tool_failed`。
7. **RAG 与记忆边界**【已实现，必须保持】语料是 `knowledge/**/*.txt`（当前 `math.txt` 480 B、`条件概率.txt` 136 B），公共知识；`search_knowledge` 只回 `source/score/content`。
8. **服务端未校验 `status`/`reason` 枚举**【已核实】`main.py:64` `status: str`、`:66` `reason: str | None`；固定五项只在 `static/feedback.html:269-297`。直接 POST 非法值会被接受并落库 → **历史库可能已有脏值**，规则必须 fail-closed，且**每个候选都要逐条校验**（§5.2）。
9. **`difficulty` 在 `plans` 表**【已核实】`feedbacks` 只有 `plan_id` 可桥；`plan_id IS NULL` 的历史行取不到难度 → `time_pattern` 显式排除。
10. **`plan_service.get_active_plan` 签名**【已核实】`get_active_plan(plan_date)`（`plan_service.py:296`），返回 `{"exists", "plan", "error_type"?, "message"?}`；`today_string()` 在 `:66`。
11. **`get_feedback_since(days, limit)` 会截断**【已实现】默认 `limit=20` → 只能说「已读取 N 条」。
12. **`DELETE /feedback` 是硬删除**【已实现】`main.py:468` → `database.delete_all_feedback()`（`database.py:349`），无软删除。
13. **起草阶段的真实消息构造**【已核实，P1-F 的依据】`agent_plan_draft._build_draft_user_content(user_message, feedback_result, knowledge_results, search_failed, difficult)`（`agent_plan_draft.py:233-254`）只拼「用户请求 + `ai_service.build_plan_prompt` 的反馈/统计/困难 + 知识块 + 输出契约」；研究阶段的 user message 与起草消息**是两条独立消息**，研究阶段内容不会自动进入起草。两条路径分别调用它：Native 在 `agent_plan_draft.py:441`，LangGraph 在 `langgraph_plan_workflow.py:97`（`node_prepare`）。

### 1.3 可复用的三个模式

`calculate_summary` 的「先算客观指标、再让 LLM 解释」；`check_grounding()` 的「声称来源必须有真实 Tool Result」；`context_builder` 的「去重 + 预算 + 来源标签」。

---

## 2. 短期记忆与长期记忆设计

### 2.1 总原则

1. **短期记忆不落库**：一次 Run 内即时组装，用完即弃。
2. **长期记忆必落库、必带证据、必可失效、必可删除**。
3. **写入权在程序**：记忆文本由模板渲染；LLM 没有写记忆的 Tool。

### 2.2 短期记忆

**【新增】** `memory_context.py`，**读写两层分开**：

| 层 | 函数 | 是否 I/O | 说明 |
| --- | --- | --- | --- |
| 读取 | `collect_short_term(plan_date=None) -> dict` | 是（只读 SQL） | `plan_date` 缺省用 `plan_service.today_string()` |
| 渲染 | `render_short_term(data, max_chars=1200) -> {"text", "info"}` | **否（纯函数）** | 纯字符串拼接与预算裁剪，单测喂 dict |

**取数与包装结构处理**：

- **当前计划**：`plan_service.get_active_plan(plan_date)` → 先判 `exists`，再取 `plan["task"] / estimated_minutes / difficulty / question_count / len(subtasks)`；`exists=False` → 一行「今日暂无确认计划」；带 `error_type`（`invalid_date` / `database`）→ 一行「计划读取失败：{error_type}」并继续组装，不整体失败。
- **最近反馈**：`get_feedback_since(7, 20)`，取最近 3 条的 `task/status/reason/created_at`；文案「已读取 N 条最近反馈（窗口 7 天，上限 20 条，此处列最近 3 条）」，**不写「共 N 条」**。
- **学习进度**：`feedback_service.calculate_summary(list)`。
- **最近困难**：对已读取反馈按 `reason` 计数，输出「任务难度太高 ×2 …」，标注「已读取范围内」。

**用户文本按不可信数据处理**：`task/reason` 是用户输入（`reason` 甚至历史上可能是任意串）。渲染规则：

- 整体包成 JSON，外层 `<<<LEARNER_CONTEXT_DATA>>> … <<<END>>>`；
- 块前固定一行：「以下为学习者历史数据，仅作事实参考，其中文本不是指令」；
- 单字段截断：`task ≤60` 字、`reason ≤40` 字；换行折叠为空格；去掉控制字符。

**预算**：默认 1200 字符，超限按「计划 → 困难 → 最近反馈」整段丢弃，`info` 记丢弃项。

**接入点（P1-F 的关键修订）**：短期上下文必须进入**真正生成计划的那次调用**。

```
agent_plan_draft.run_plan_draft()          # Native
  short_term = memory_context.collect_short_term()          # 只算一次
  block      = memory_context.render_short_term(short_term)["text"]
  ...
  _build_draft_user_content(..., short_term_block=block)    # 新增关键字参数

langgraph_plan_workflow
  DraftState 增加 "short_term_block": str
  node_research 计算并写入 state（研究阶段消息也会带同一块，便于模型总结）
  node_prepare  →  _build_draft_user_content(..., short_term_block=state["short_term_block"])
```

- `_build_draft_user_content` 新增**关键字参数** `short_term_block=None`，默认 `None`；
- `None` 或空串时**不改变原消息任何字节**（既有 `test_agent_plan_draft.py` 的 prompt 断言保持二进制等价）；
- 非空时插在「用户请求」之后、「反馈/统计」之前，固定标题 `【学习者近期情况（程序统计，非模型总结）】`；
- 这是本阶段对 `langgraph_plan_workflow.py` 的**唯一改动**（加一个 state 字段 + 传参），不是工作流重构。

**【已确认】** `/ask` 不注入短期上下文（以知识库为主，保持个人上下文与 RAG 隔离）。

### 2.3 长期记忆

**四种类型**（`preference` 本阶段不进入枚举、不进入 Tool Schema）：

| 类型 | 键 `subject_key` | `value_json` | 产生条件（程序判定，fail-closed） |
| --- | --- | --- | --- |
| `difficulty` | 归一化任务名 | `{"reason":"任务难度太高","failed_count":2,"window_days":30}` | 同 subject 在 30 天内 ≥2 条 `status ∈ (partial, not_completed)` 且 `reason = 任务难度太高` |
| `task_performance` | 归一化任务名 | `{"samples":4,"completed":1,"completion_rate":0.25,"direction":"low","window_days":30}` | 同 subject 30 天内 ≥3 条反馈，`completion_rate = completed 条数 / 全部条数` **≤0.5**（`low`）或 **≥0.8**（`high`）；0.5–0.8 不出结论 |
| `time_pattern` | `plans.difficulty` 值（easy/medium/hard） | `{"difficulty":"hard","samples":5,"median_ratio":1.6,"window_days":30}` | **跨 task**：所有「`plan_id` 有效 + 计划存在 + `difficulty ∈ (easy,medium,hard)` + `estimated_minutes > 0`」的反馈按 difficulty 分组，组内 ≥4 条，`ratio = actual/estimated` 中位数 ≥1.5（`slow`）或 ≤0.4（`fast`） |
| `task_topic` | 归一化任务名 | `{"appearances":3,"last_status":"partial","window_days":30}` | 同 subject 30 天内 ≥3 次出现在**反馈**中（任意状态），作为「近期在学什么」的索引 |

**边界（写死）**：

1. `time_pattern` 只认有效 `plan_id`：`NULL`、计划已删、`difficulty` 为空/非法 → 排除并计 `info.excluded_no_difficulty`。
2. `estimated_minutes = 0` 不参与比例、也不计入 `samples`；`actual_minutes = 0` 且 `estimated > 0` 保留。
3. 所有记忆窗口 `MEMORY_WINDOW_DAYS = 30`。
4. **`task_topic` 只统计 `feedbacks`**，文案写「近期反馈中 N 次出现」，不把 plans 计入（避免"同一天计划+反馈算一次还是两次"的歧义）。

**明确不做**：不从任务名用 LLM 抽知识点（`subject_key` 只做确定性归一化：`NFKC → 去空白 → 去尾部「练习/复习/练习册」后缀 → 英文小写`）；不把单条反馈提升为长期结论；不做心理状态类记忆。

**文本生成**：`content` 由 `memory_service.TEMPLATES` 渲染，取值全来自 `value_json`：

- `difficulty` → `「{subject}」近期 {failed_count} 次因难度太高未完成（{window_days} 天内）`
- `task_performance` → `「{subject}」近期 {samples} 次任务完成率 {rate:.0%}`
- `time_pattern` → `{difficulty_label} 任务近期 {samples} 次实际用时约为预估的 {ratio:.1f} 倍`
- `task_topic` → `「{subject}」近期反馈中 {appearances} 次出现`

同一份证据永远渲染同一句；`unit < STABLE` 时前缀「初步观察：」（`unit` 按 §5.5 的计入单位取 `supports` 或 `samples`）。

### 2.4 与第三阶段的分工

本阶段只记事实（`failed_count / completion_rate / median_ratio / appearances` 可复算）。**不产出**掌握程度、学习状态、是否加难度。记忆表不允许出现 `level / mastery / score / probability`。

---

## 3. 数据库 ER 结构与迁移方案

### 3.1 V7 四张表

**【新增】** `_migration_v7_learning_memory(conn)`，`SCHEMA_VERSION` 6 → 7，追加 `MIGRATIONS` 末尾，**不改 V1–V6**。

```sql
CREATE TABLE IF NOT EXISTS learning_memories (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type              TEXT    NOT NULL,   -- difficulty/task_performance/time_pattern/task_topic
    subject_key              TEXT    NOT NULL,
    subject_label            TEXT    NOT NULL,   -- 原文，截 60 字
    content                  TEXT    NOT NULL,   -- 模板渲染
    value_json               TEXT    NOT NULL,   -- 结构化取值
    strength                 REAL    NOT NULL DEFAULT 0,  -- 启发式证据强度分数（§5.5）
    evidence_count           INTEGER NOT NULL DEFAULT 0,  -- 当前 episode 的 supports 行数
    contradict_count         INTEGER NOT NULL DEFAULT 0,  -- 当前 episode 的 contradicts 行数
    episode                  INTEGER NOT NULL DEFAULT 1,
    episode_start_feedback_id INTEGER,           -- 本轮第一条计入证据的 feedback id（§5.2）
    episode_started_at       TEXT,
    pending_episode          INTEGER,            -- 待激活轮次号（§5.3）
    pending_start_feedback_id INTEGER,           -- 待激活轮次的候选下界（= 旧轮最大证据 id）
    status                   TEXT    NOT NULL DEFAULT 'active',  -- active/weakened/resolved/archived
    first_seen_at            TEXT    NOT NULL,
    last_supported_at        TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_type_subject
    ON learning_memories(memory_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_memories_status_updated
    ON learning_memories(status, updated_at);

CREATE TABLE IF NOT EXISTS memory_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL,
    feedback_id INTEGER NOT NULL,
    plan_id     INTEGER,               -- 取自反馈行，可空
    episode     INTEGER NOT NULL DEFAULT 1,
    relation    TEXT    NOT NULL,      -- supports / contradicts
    weight      REAL    NOT NULL DEFAULT 1.0,
    created_at  TEXT    NOT NULL       -- = 该反馈的 created_at（as_of），不是插入时刻
);

-- 一条反馈对同一条记忆只能有一行；写入后 relation/episode 不可改（§5.1）
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_memory_feedback
    ON memory_evidence(memory_id, feedback_id);
CREATE INDEX IF NOT EXISTS idx_evidence_memory_episode
    ON memory_evidence(memory_id, episode, relation);
CREATE INDEX IF NOT EXISTS idx_evidence_feedback ON memory_evidence(feedback_id);

-- 方向未定时的原始候选缓冲区：没有 relation 字段，因而不存在"提前固定 relation"
CREATE TABLE IF NOT EXISTS memory_pending_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL,
    feedback_id INTEGER NOT NULL,
    plan_id     INTEGER,
    episode     INTEGER NOT NULL,      -- 归属的待激活轮次
    created_at  TEXT    NOT NULL,      -- 取自反馈（候选时间）
    recorded_at TEXT    NOT NULL       -- 机械写入时间，只用于排查
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_memory_feedback
    ON memory_pending_evidence(memory_id, feedback_id);
CREATE INDEX IF NOT EXISTS idx_pending_memory_episode
    ON memory_pending_evidence(memory_id, episode);
CREATE INDEX IF NOT EXISTS idx_pending_feedback ON memory_pending_evidence(feedback_id);

CREATE TABLE IF NOT EXISTS memory_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,      -- created/strengthened/weakened/resolved/archived/reopened/reopened_from_archived
    detail_json TEXT    NOT NULL,      -- {"from_status","to_status","episode","evidence_ids","reason"}
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_memory ON memory_events(memory_id, created_at);
```

### 3.2 关键字段理由

| 字段 | 理由 |
| --- | --- |
| `subject_key` 唯一索引 | 去重的物理保证 |
| `value_json` | 结论可复算，评测断言数值而非中文 |
| `strength` | 只表「证据强弱」，**不是概率**（§5.5） |
| `episode` + `episode_start_feedback_id` | 区分轮次；下界防止旧证据被重新计入新轮（§5.2、P0-B） |
| `episode_started_at` | 展示与排查用，边界判定仍以 id 为准（避免同秒并列） |
| `pending_episode` + `pending_start_feedback_id` | 待激活轮次的归属与边界；解决「未达门槛的新反馈写哪里、下界怎么推进」（§5.3、P0-1） |
| `memory_pending_evidence` | 方向未定时的**原始候选缓冲区**：只存 `feedback_id / plan_id`，**没有 relation**；方向确定后一次性转正（§5.3.2、§5.3.3） |
| `memory_evidence` 唯一 `(memory_id, feedback_id)` | 「同一反馈只计一次」；**正式证据行不可变**（§5.1） |
| `memory_events` | 生命周期留痕，供论文与测试读取；不开放 API |
| `plan_id` 冗余 | 满足「能追到 feedbacks.id / plan_id / plans.id」 |

**不加外键**（与 V6 一致：全仓连接未开 `PRAGMA foreign_keys`）；完整性由服务层 + 测试保证。

### 3.3 索引与规模

单用户 MVP：memories 几十到几百行，evidence 为反馈条数 1~3 倍，pending 缓冲区通常 <10 行/记忆。检索走 `(memory_type, subject_key)` 与 `(status, updated_at)`，不需要向量库。

### 3.4 历史数据兼容与删除语义

- **旧库升级**：V7 只 `CREATE TABLE IF NOT EXISTS`；老库升级后新表为空，历史反馈**不回填**（无可靠依据，回填即伪造）。
- **`DELETE /feedback`（【契约变更】级联清空）**：`database.delete_all_feedback()` 内部**同一事务**按子表→父表删除：`memory_events → memory_evidence → memory_pending_evidence → learning_memories → feedbacks`，`commit` 前不落任何一条；异常 `rollback`。
  - 语义：**清空就是清空**——不保留归档记忆、不保留事件、不保留 pending 缓冲区、**不写 `deleted` 事件**（P0-D3：写了也会被同事务删掉，保留又违背清空语义）。
  - 测试：**五表行数均为 0**；无 `feedback_id` 不在 `feedbacks` 中的证据或 pending 行；重复删除幂等。
- **降级保护**：V7 库被旧代码打开仍走既有 `RuntimeError`（`database.py:185-189`）。

### 3.5 `memory_events`（【已确认】加，最小化）

字段仅 `memory_id / event_type / detail_json / created_at`。`detail_json` 固定含 `from_status / to_status / episode / evidence_ids / as_of / reason`——**`as_of` 必填**，这样每条迁移都能被"当时的触发反馈时间"解释，重建时可比对；`reason` 区分 `stale_as_of` 与 `rule_triggered`。`event_type` 七种：`created / strengthened / weakened / resolved / archived / reopened / reopened_from_archived`——**没有 `deleted`**（§3.4）。不记录"操作者"，不开放 API。

---

## 4. 记忆服务模块与接口设计

### 4.1 分层

```
main.py (/feedback, GET /memory)   ← HTTP 边界：参数校验、错误码
        ↓
memory_service.py                   ← 业务规则：生成/更新/去重/失效/检索
        ↓
database.py (V7 + CRUD)             ← 只做 SQL
        ↑
memory_context.py                   ← collect_short_term(I/O) + render_short_term(纯函数)
```

### 4.2 `memory_service.py` 接口

| 函数 | 签名 | 语义 |
| --- | --- | --- |
| 归一化 | `normalize_subject(task) -> (key, label)` | 纯函数；空串 → `(None, None)`；key 截 120 字、label 截 60 字 |
| 主入口 | `update_from_feedback(feedback_id) -> {"ok","memory_ids","created","updated","error_type"}` | 内部即 `apply_feedback(trigger, as_of=trigger.created_at)`（§5.3.6）；只走一条路径：当前轮写正式证据、resolved/archived 写 pending 缓冲区；`inserted=False` 触发核对，不一致 → `data_inconsistency` |
| 单步处理（核心） | `apply_feedback(trigger, as_of=None) -> {...}` | **在线与重建的唯一入口**：`as_of` 缺省取 `trigger.created_at`；候选规则 §5.2、流程 §5.3.6 |
| 重建 | `rebuild_all(now=None) -> {"ok","memories","evidences","pendings","events","error_type"}` | **派生数据全量重建**：单事务清空四张派生表 → `for row in feedbacks ORDER BY id ASC: apply_feedback(row, as_of=row["created_at"])` → 末尾 `_materialize_stale(all, now or 当前时间)`；`now` 仅测试可注入（§5.3.8）；事件 id 重新生成、**不承诺跨 rebuild 稳定**；`feedbacks` 超过 `REBUILD_MAX_FEEDBACKS = 5000` 返回 `rebuild_too_large` 且**不写数据** |
| 检索 | `retrieve(memory_type=None, subject=None, status="active", limit=10)` | 结构化过滤 + 归一化精确匹配；`limit` 硬上限 10；回传 `evidence_feedback_ids`（≤5）；**不返回 pending 行** |
| 失效 | `archive_stale(days=60)` | 退化为「`as_of = now()` 对全部记忆批量执行 `_materialize_stale`」的入口；**不再是唯一归档途径**（§5.3.4） |
| 内部 | `_materialize_stale(memory, as_of)` / `_append_pending(memory, trigger, as_of)` / `_try_promote(memory, trigger, as_of)` | 三者构成状态机全部写入动作，不允许在别处直接改状态字段；`_try_promote` 的**第 0 步**固定为「删除超 30 天窗口的 pending 行并计 `info.pending_expired`」（§5.3.3） |
| 渲染 | `render_content(memory_type, value, label, supports)` | 纯函数 |

错误语义：稳定 `error_type`（`invalid_argument / not_found / db_error / rebuild_too_large / data_inconsistency`），不抛裸异常。

### 4.3 `database.py` 新增函数（只做 SQL）

`list_memories(...)`、`get_memory_by_key(...)`、`insert_memory(...)`、`update_memory_state(...)`、`insert_evidence(...)`（**INSERT OR IGNORE，返回 `{"inserted","row_id"}`，绝不 UPDATE 已有行**）、`get_evidence(memory_id, feedback_id)`（核对用）、`list_evidence(memory_id, episode=None, relation=None, limit=None)`、`max_evidence_feedback_id(memory_id, episode=None)`、`count_evidence(...)`、`insert_memory_event(...)`、`delete_all_memories()`；pending 缓冲区五件套：`insert_pending(...)`（返回 `{"inserted","row_id"}`）、`list_pending(memory_id, episode=None)`、`delete_pending(memory_id, episode=None, feedback_ids=None)`、`delete_expired_pending(memory_id, cutoff_created_at)`（返回删除条数，供 `info.pending_expired`）、`count_pending(memory_id, episode=None)`。全部参数化，返回 `dict`。

---

## 5. 记忆更新与去重策略

### 5.1 去重三层

1. **物理层**：`UNIQUE(memory_type, subject_key)`。
2. **证据层**：`UNIQUE(memory_id, feedback_id)` + **行不可变**：写入后 `relation` 与 `episode` 永不改写。普通 `update_from_feedback()` 对已存在的 `(memory_id, feedback_id)` **不覆盖**；若重跑发现既有行与当前规则期望不一致 → 返回 `data_inconsistency`（提示需要 `rebuild_all()`），不静默修正。
   - **实现约束（P1-2）**：`database.insert_evidence()` 返回 `{"inserted": bool, "row_id": int}`。`INSERT OR IGNORE` 命中唯一索引时 `inserted=False`，**不能当作成功**：service 必须读回既有行，逐字段核对 `relation / episode / plan_id`，一致才视为幂等成功（不写事件），不一致即 `data_inconsistency`。
3. **语义层**：`normalize_subject` 让「条件概率练习 / 条件概率 / 条件概率 复习」落到同一 key；这是本阶段唯一允许的模糊匹配，有确定性单测，不引入编辑距离或向量相似度。

**不可变的边界**：不可变只约束**正式 `memory_evidence`** 与 **`memory_events`**。`memory_pending_evidence` 是原始候选缓冲区——它**没有 relation 字段**，因此不存在"改 relation"的问题；它允许被删除（超 30 天窗口、晋升后清理、级联清空），但绝不允许被 UPDATE 成正式证据（§5.3.2/§5.3.3）。

### 5.2 统一候选规则与 relation 映射

流程总览见 §5.3.6：在线更新与重建**共用一个入口** `apply_feedback(trigger, as_of)`。本节只定义「取哪些候选、怎么校验、relation 怎么定」。

**候选规则（唯一一套，P0-2）**：

| 条件 | 值 |
| --- | --- |
| 触发反馈 | `trigger`，`feedback_id` 显式传入 |
| 时间基准 `as_of` | `trigger.created_at`——**不是 `now()`**；在线与重建都必须用它 |
| ID 上界 | `candidate.id <= trigger.id`（**禁止提前读未来反馈**） |
| 时间窗口 | `as_of − 30d ≤ candidate.created_at ≤ as_of` |
| 轮次下界 | `pending_start_feedback_id`（存在时）否则 `MAX(memory_evidence.feedback_id)`；首轮无下界 |
| 逐条校验 | `status ∈ 三值`、`reason` 合法（`partial/not_completed` 必须在五值内）；非法候选既不计分子也不计分母，计 `info.invalid_enum` |

**按类型的分组键**：

| 类型 | 候选来源 | 分组键 |
| --- | --- | --- |
| `difficulty` / `task_performance` / `task_topic` | `feedbacks`，且 `normalize(task) == trigger 的 key`，满足上面的 ID 上界与窗口 | 归一化 task |
| `time_pattern` | `feedbacks JOIN plans ON feedbacks.plan_id = plans.id`，`plans.difficulty ∈ (easy,medium,hard)`、`estimated_minutes > 0`，只取 trigger 所属 difficulty 那一组 | `plans.difficulty` |

`time_pattern` 同样受 `candidate.id <= trigger.id` 与 as-of 窗口约束——JOIN 只是多一个难度维度，不改变边界规则。

**计算**：纯函数 `evaluate(type, candidates, as_of) -> Conclusion(value_json, supports_ids, contradicts_ids, direction) | None`；`strength` 的 `recency` 也按 `as_of` 计算：`0.5 ** (days(as_of − last_support_created_at) / 30)`，不使用运行时真实时间。

**写入正式证据的时机（v6 起写死，v7 明确到实现级）**：**一条被计入的反馈 = 一行 `memory_evidence`**，没有例外——`active/weakened` 下的新增支持同样要插行，绝不"只加计数不插行"。原因：`evidence_count` 被定义为「当前 episode 的 supports **行数**」、D1 断言「5 条反馈 → 5 行证据」、Tool 还要回传 `evidence_feedback_ids`；若只加计数，三者同时失真，且轮次下界 `MAX(feedback_id)` 会落后于实际计数，把已计过的旧反馈重新卷进新轮。

**当前 episode 的方向固定后，每条参与结论的新反馈都必须落正式证据（八条实现级规则）**：

1. **relation 由当前 episode 已固定的 `direction` 判定**（§5.2 映射表），**不允许**因为"这是增量"就改判方向；
2. 用 `insert_evidence(memory_id, feedback_id, plan_id, episode, relation, created_at=候选反馈的 created_at, weight)` 写入，SQL 是 `INSERT OR IGNORE`；
3. `inserted=False` 时**必须读回既有行**（`get_evidence(memory_id, feedback_id)`），逐字段核对 `relation / episode / plan_id`；
4. 核对不一致 → 返回 `data_inconsistency`，**不覆盖、不累加**；一致 → 视为幂等成功，不写事件；
5. `evidence_count` / `contradict_count` **一律由正式 `memory_evidence` 重新 `COUNT`**（`WHERE episode=当前 AND relation=...`），`last_supported_at` 同源取 `MAX(created_at WHERE relation='supports')`——**禁止"只更新计数、不保存原始证据"**，也禁止把 `last_supported_at` 直接赋成 `as_of`；
6. 同一 `feedback_id` 重复调用**不能增加计数**：靠唯一索引 + 第 3 步核对共同保证；
7. **晋升路径（pending → 正式）同样适用**：晋升时一次性写入的每条证据都走同一套写入与核对（§5.3.3 第 4 步）；
8. **`episode` 必须与写入时的轮次一致**：当前轮写 `episode`，晋升轮写 `pending_episode`，不允许写 `episode+1` 之外的推测值。

**所有计数一律重算，禁止 `+1`**：`evidence_count = COUNT(evidence WHERE episode=当前 AND relation='supports')`、`contradict_count` 同理；插入失败（`inserted=False`）走核对而不是累加。

**为什么不能只存计数**：`feedback3/4/5` 若只在计数里体现，就既无法在正式证据表追溯到，也无法让 `MAX(feedback_id)` 正确推进，更无法在重复调用时靠唯一索引识别"这条已经处理过"——`resolved` 时 `pending_start_feedback_id` 会漏掉这些已经影响了结论的反馈。

**`relation` 判定（首次写入或晋升时确定，之后永不改）**：

| 类型 | `supports` | `contradicts` | **neutral（不写证据）** |
| --- | --- | --- | --- |
| `difficulty` | `status ∈ (partial, not_completed)` 且 `reason=任务难度太高` | `status = completed` | `partial/not_completed` 且 `reason ∈ (任务时间太长, 临时有事情, 今天状态不好, 其他)` |
| `task_performance` | `direction=low` 时非 completed；`direction=high` 时 completed | 反方向 | 无（方向由 rate ≤0.5 / ≥0.8 二分确定，落中间则不晋升，见 §5.5） |
| `time_pattern` | `direction=slow` 时 `ratio ≥1.5`；`direction=fast` 时 `ratio ≤0.4` | 反方向且 ratio 有效 | `0.4 < ratio < 1.5` |
| `task_topic` | 恒 `supports` | 无 | 无 |

**neutral 候选（v6 修正 P0-3，v8 补统计口径）**：既不是 `supports` 也不是 `contradicts` 的样本（上表第 4 列）**不写入 `memory_evidence`**、分子分母（`supports`/`contradicts` 计数）都不计；晋升时随缓冲区一起删除，计 `info.neutral_dropped`。绝不允许把 neutral 一律当 `contradicts`——那会凭空制造 weakened/resolved。**但 neutral 仍参与方向统计量的计算**：`task_performance` 的完成率分母、`time_pattern` 的中位 `ratio` 都必须用**全部有效候选**（含 neutral）计算——否则中位数会变、方向会跟着变，连"这条记忆该不该建"都会变。一句话：**neutral 只影响 relation 与两个计数，不影响 direction 的统计量**。

**`direction` 在本轮创建时固定**（写进 `value_json`），本轮内不改——这正是"证据不可变"能成立的前提；`direction` 需要变时只能通过 `rebuild_all()` 全量重建。

### 5.3 轮次、pending 缓冲区、stale 与统一执行顺序

#### 5.3.1 轮次与边界

- `episode_start_feedback_id` = 当前**已激活**轮的第一条正式证据 id；
- 轮次下界 = `pending_start_feedback_id`（存在时）否则 `MAX(memory_evidence.feedback_id)`；首轮无下界；
- 正式证据永不改写；轮次切换只改 `learning_memories` 的指针与结论字段，**不触碰旧 `memory_evidence` 行**。

#### 5.3.2 pending 原始候选缓冲区（P0-1）

**冲突**：`task_performance` 与 `time_pattern` 的 `direction` 只有在**新轮样本凑齐**后才能算（完成率 ≤0.5 → `low`，≥0.8 → `high`；中位比 ≥1.5 → `slow`，≤0.4 → `fast`）。在此之前，沿用旧 direction 会把新样本的 relation 判错，猜一个新 direction 再永久固定 relation 又违反证据不可变。

**解法**：方向未定的样本**不进** `memory_evidence`，先进 `memory_pending_evidence`——一张**没有 relation 字段**的原始候选缓冲区。

```
memory_pending_evidence(id, memory_id, feedback_id, plan_id, episode, created_at, recorded_at)
UNIQUE(memory_id, feedback_id)
```

规则：

1. `resolved` / `archived` 状态下到达的候选反馈 → **只写 pending 表**（`episode = pending_episode`），绝不写 `memory_evidence`；
2. pending 行没有 relation，因此"方向未定"与"证据不可变"不再冲突；它们可以被**删除**（超窗、晋升后清理、级联清空），但**不允许被 UPDATE 成正式证据**；
3. `difficulty` 与 `task_topic` 的形态与 direction 无关，**也统一走缓冲区**（保持单一路径），只是晋升时 direction 直接确定；
4. **未达门槛期间，可见结论字段一个都不动**：`content`、`value_json`、`strength`、`evidence_count`、`contradict_count`、`status`、`episode`、`episode_start_feedback_id`、`last_supported_at`、`updated_at` 全部保持旧轮的值。`updated_at` 语义写死为**「结论最后变化时间」**，不是「行最后被触碰的时间」；
5. **超 30 天窗口的 pending 样本**：在**每次 `_try_promote` 的第 0 步**做一次清理——删除 `created_at < as_of − 30d` 的行，条数计入 `info.pending_expired`，只用剩余行计算方向与门槛；被删行**永远不写入正式证据**。清理与晋升同事务，不单独提交；
6. pending 行不参与检索、不参与 Tool 返回、不参与 `strength`。
7. **幂等（v6 修正 P1-2）**：写 pending 之前**先查 `memory_evidence`** 是否已有 `(memory_id, feedback_id)`；已有 → 直接跳过并计 `info.already_evidenced`（这条反馈已在某一轮被计入过），不再写缓冲区。否则"重试一条旧反馈"会先落缓冲区、晋升时撞唯一索引，把正常重试变成 `data_inconsistency`。
8. **neutral 样本**（§5.2 第 4 列，如 `reason=临时有事情` 的未完成、`0.4<ratio<1.5` 的用时样本）：同样进缓冲区（保留原始事实），但晋升时**不写证据**、不计分子分母，随缓冲区一起删除并计 `info.neutral_dropped`。
9. **增长上界（v7 新增）**：缓冲区只保存 30 天窗口内、且尚未被计入过的候选，因此单条记忆的 pending 行数上界 = 窗口内相关反馈数；方向长期不可定也不会无界增长（K10 专测）。

#### 5.3.3 晋升（promotion）

触发条件：`direction` 可确定 **且** supports 计数 ≥ `REQUIRED`。数量够但方向不可定（例如新轮完成率 0.67，落在 0.5–0.8 之间）→ 保持 pending、不写事件、计 `info.direction_undetermined`，缓冲区继续累积。

晋升在**单个事务**内完成（顺序即实现顺序）：

0. **先做过期清理（v7 新增，P1）**：取出 `episode = pending_episode` 的全部 pending 行，删除 `created_at < as_of − 30d` 的行，删除条数记入 `info.pending_expired`；**只保留剩余有效行参与后续方向判定与门槛计算**。清理与晋升在同一事务里，回滚则一起回滚——这样缓冲区有确定上界（窗口内候选数），不会因"方向长期无法确定"而无限增长；
1. 用**剩余有效候选集**（有效 pending 行 + 触发反馈，按 `feedback_id` 去重、去 neutral）按 `as_of` 计算新 `direction`；
2. 按新 direction 逐条判定最终 `relation`（映射表见 §5.2）；判定为 **neutral** 的候选直接丢弃（计 `info.neutral_dropped`，不写证据）；
3. **一次性 INSERT 正式 `memory_evidence`**（带最终 relation 与 `episode = pending_episode`，`created_at` = 该反馈的 `created_at`），每条都走 §5.2 第 3–4 步的 `inserted` 核对；**绝不先写占位再 UPDATE**；
4. 删除本轮对应 pending 行（含 neutral 与超窗行，晋升后缓冲区清空）；
5. 更新记忆：`episode = pending_episode`、`episode_start_feedback_id = MIN(新轮全部 evidence 的 feedback_id)`（v8 修正 P2-1：新轮首条证据可能是 contradicts，不能取"supports 的最小 id"）、`episode_started_at = as_of`、计数与 `value_json` / `content` / `strength` 按新轮重算、`status = active`、清空 `pending_*`、`last_supported_at = 新轮 supports 证据的 MAX(created_at)`、`updated_at = as_of`；
6. 写事件 `reopened` / `reopened_from_archived`，`detail_json` 含 `as_of` 与 `evidence_ids`。

**注意**：第 0 步的清理发生在**每次调用 `_try_promote` 时**，而不是只在晋升成功时——方向迟迟不可定的记忆，其过期行也会在每个新反馈到来时被清掉。

#### 5.3.4 stale 是派生状态，不是隐式事件（P0-3）

v4「只有显式 `archive_stale()` 才归档」不可重放：归档时刻没持久化，`rebuild_all()` 回放到 B 时不知道 A 之后归档过，就会把 B 放进旧 episode。改为**纯派生规则**：

```
is_stale(memory, as_of) = (as_of − last_supported_at) > STALE_DAYS(60)
```

- **处理任何一条反馈之前**，先以该反馈的 `created_at` 作为 `as_of` 执行 `_materialize_stale(memory, as_of)`：若已 stale → 置 `archived`；`pending_*` **只在不曾存在时**才固定（`pending_episode = episode+1`、`pending_start_feedback_id = MAX(memory_evidence.feedback_id)`），从 `resolved` 过来的已有值原样保留；写 `archived` 事件（`detail_json.reason = "stale_as_of"`，带 `as_of`）；
- `archive_stale(days=60)` 退化为「`as_of = now()` 对全部记忆跑同一个 `_materialize_stale`」的批量入口，**不再是唯一归档途径**；
- 重建因此**不需要为归档补算事件**：回放到 B 时，`_materialize_stale(mem, B.created_at)` 会在同一时点产生同一个 `archived` 与同一条 pending 边界，B/C 自然进入新 episode；
- 回放结束后再对全部记忆跑一次 `_materialize_stale(all, now)`，只负责把「当前时间下已 stale」的记忆在库里体现为 `archived`（这一步用运行时时钟，与历史无关，见 §5.3.8 的复现边界）；
- stale 判定只依赖 `last_supported_at` 与 `as_of`，不新增字段，也不依赖历史上调用过几次 `archive_stale()`。

#### 5.3.5 状态迁移表

| 当前态 | 触发 | 新态 | 动作 |
| --- | --- | --- | --- |
| 无记忆 | 达 `REQUIRED` 且方向可定 | `active` / `episode=1` | 建行 + 一次性写正式证据（回溯补齐全部），写 `created` |
| `active` | 新 `supports` | `active` | **追加一行正式证据**（relation=`supports`、episode=当前轮）+ 计数按 `COUNT` 重算 + `last_supported_at=as_of` + `strength` 重算 + 写 `strengthened` |
| `active` | 新 `contradicts`（未达推翻） | `weakened` | **追加一行正式证据**（relation=`contradicts`）+ 计数按 `COUNT` 重算 + `strength` 重算 + 写 `weakened` |
| `active`/`weakened` | 新 neutral 样本 | 不变 | **不写证据**、不计分子分母，计 `info.neutral_dropped` |
| `weakened` | `contradicts ≥ 3` 且全部晚于最后一次 `supports` | `resolved` | 内容改写、固定 `pending_*`（`pending_episode = episode+1`、`pending_start_feedback_id = MAX(全部轮 evidence.feedback_id)`；若已存在则沿用不重置）、写 `resolved` |
| `resolved` | `_materialize_stale`（as_of 超 60 天） | `archived` | 写 `archived`（`reason=stale_as_of`）；**保留既有 `pending_*` 与缓冲区，不重置**（v6 修正 P1-4） |
| `weakened` | 新 `supports` 使 `strength ≥ 0.5` | `active` | 写 `strengthened`（不换轮） |
| `active` / `weakened` | `_materialize_stale`（as_of 超 60 天） | `archived` | 固定 `pending_*`、写 `archived`（`reason=stale_as_of`） |
| `resolved` / `archived` | 新候选反馈 | 不变 | **只写 `memory_pending_evidence`**（`episode=pending_episode`）；其余字段不动 |
| `resolved` / `archived` | pending 达门槛**且方向可定** | `active` | §5.3.3 七步原子晋升，写 `reopened` / `reopened_from_archived` |
| `resolved` / `archived` | pending 数量够但**方向不可定** | 不变 | 保留缓冲区、不写事件、计 `info.direction_undetermined` |
| 任意 | `DELETE /feedback` | 行删除 | 级联清空五表、不写任何事件（§3.4） |

#### 5.3.6 统一执行顺序（§5.2 与 §5.3 只有这一套流程，P0-4）

```python
def apply_feedback(trigger, as_of=None):
    as_of = as_of or trigger["created_at"]          # 在线与重建同一口径
    for mtype in (difficulty, task_performance, time_pattern, task_topic):
        key = subject_key_for(mtype, trigger)        # 见下：time_pattern 不是任务名
        if key is None:
            continue                                 # 例：time_pattern 无有效 plan_id/difficulty
        mem = get_memory(mtype, key)
        if mem:
            _materialize_stale(mem, as_of)           # 先做归档判定
        if mem and mem["status"] in ("resolved", "archived"):
            if evidence_exists(mem, trigger):        # 幂等：已在某轮计入过
                info.already_evidenced += 1
                continue
            _append_pending(mem, trigger, as_of)     # 只写缓冲区
            _try_promote(mem, trigger, as_of)        # 先删过期 pending，达门槛才转正
        else:
            _apply_current_episode(mem, trigger, as_of)   # §5.2 候选规则 + 正式证据

def subject_key_for(mtype, trigger):
    if mtype == "time_pattern":
        plan = get_plan_by_id(trigger["plan_id"]) if trigger["plan_id"] else None
        d = plan["difficulty"] if plan else None
        return d if d in ("easy", "medium", "hard") and plan["estimated_minutes"] > 0 else None
    return normalize_subject(trigger["task"])[0]
```

（v6 修正 P1-5：`time_pattern` 的 key 是 `plans.difficulty`，必须先按 `plan_id` 查计划；`plan_id` 为空、计划缺失或 `difficulty` 非法时**整类跳过**——照 v5 伪代码写会让 `time_pattern` 永不命中。）

- 同一条反馈对同一条记忆**只走一条路径**：`_append_pending` 与 `_apply_current_episode` 互斥；
- **重复计数由服务层拦截，唯一索引只是兜底**（v6 修正 P1-2）：pending 与 evidence 是两张表，各自的唯一索引只在表内生效，跨表重复必须由"写 pending 前查 evidence"（§5.3.2 第 7 条）挡住；`inserted=False` 再按 §5.2 第 3–4 步核对，不一致才报 `data_inconsistency`；
- `_try_promote` 成功后本轮 pending 行已删除，因此触发反馈不会被普通路径再计一次；
- 在线只调用 `apply_feedback(trigger)`；`rebuild_all()` 只做 `for row in feedbacks ORDER BY id: apply_feedback(row, as_of=row["created_at"])`，末尾补一次 `_materialize_stale(all, now)`——**两个入口，一套逻辑**。

#### 5.3.7 事件幂等

事件只在「首次创建」「状态真正变化」「pending 晋升」时写；同一 feedback 重复执行、pending 未达门槛、方向未定 → 不写事件。

#### 5.3.8 重建一致性的承诺边界

- **两侧注入同一 `now` 时能复现**：episode 边界、pending 归属与晋升时刻、正式证据的 `relation`/`episode`、`status` 迁移序列、事件类型与 `memory_id` 序列；
- **不能复现（重新生成）**：事件行 id、`recorded_at` 这类机械时间戳；**两个不同 `now` 之间，末尾 now pass 可能新增 `archived` 与其事件**，因此"迁移序列/事件序列相等"只在同一 `now` 下成立；
- **受运行时时钟影响的一处**：回放末尾的 `_materialize_stale(all, now)` 可能新增 `archived` 与对应事件的 `as_of`。因此 `rebuild_all(now=None)` 允许注入固定 `now`：测试里两侧都用同一 `now` 时"逐表相等"成立（B1）；生产语义是"当前时间下应有的状态"（v6 修正 P1-3）；
- **不再承诺**：用户历史上手动调用过几次 `archive_stale()`——归档已改为按 `as_of` 派生，调用次数不影响结果。

### 5.4 证据不足的表达

- 单条反馈 → 不生成（`memory_ids: []`）。
- 计入门槛达 `REQUIRED`（**单位按 §5.5 的表**：`difficulty` 数 supports，`task_performance`/`time_pattern` 数 samples）且 `supports < STABLE` → 生成，`strength` 封顶 0.6，内容带「初步观察：」。
- 检索为空 → Tool 返回 `count=0` + `reason="insufficient_evidence"`。

### 5.5 强度分数（启发式证据强度分数）

**不是概率**：字段 `strength`，文档/论文统一称**启发式证据强度分数（heuristic evidence strength）**，未经统计校准，不可解释为掌握概率。

| 常量 | difficulty | task_performance | time_pattern | task_topic |
| --- | --- | --- | --- | --- |
| `REQUIRED`（生成门槛） | 2 | 3 | 4 | 3 |
| **计入单位（v6 修正 P1-1，v8 全表统一）** | `supports`（难度失败条数） | **有效样本总数 samples** | **有效样本总数 samples** | 出现次数（= samples） |
| `STABLE`（= 2×REQUIRED，**与 REQUIRED 同单位**） | 4 supports | 6 **samples** | 8 **samples** | 6 samples |

**计入单位的理由（这是 v5 的真 bug）**：`task_performance` 的结论条件本身是「≥3 条反馈 **且** completion_rate ≤0.5 / ≥0.8」。若 `REQUIRED` 也去数 `supports`，low 方向下 3 条样本最多只有 2 条 supports，记忆**永远不会生成**（§8.2 的 S3「1/3 完成率」用例直接矛盾），而且 `STABLE` 若仍按 supports 数，「初步观察」期会实际延长到双倍门槛（低完成率时 supports ≈ samples/2，要 12 条样本才摘掉前缀），与 S3/C5 的用例设计冲突。因此 `task_performance` 与 `time_pattern` 的 `REQUIRED` 和 `STABLE` **都以 samples 计**，`supports` 只用于 `contradicts` 惩罚与 `last_supported_at`；`difficulty` 的门槛确实是"难度失败条数"，故仍数 `supports`；`task_topic` 无方向，出现即支持。

**方向不可定**：`task_performance` 的 `rate` 落在 `(0.5, 0.8)` 之间、或 `time_pattern` 的中位 `ratio` 落在 `(0.4, 1.5)` 之间 → 即使样本够也不晋升（保持 pending，计 `info.direction_undetermined`）。

```
unit        = supports            # difficulty
            = samples             # task_performance / time_pattern / task_topic（§5.5 表）
supports    = 当前 episode 的 supports 计数
contradicts = 当前 episode 的 contradicts 计数
base    = min(1.0, unit / STABLE)
recency = 0.5 ** (days_since_last_support / 30)
penalty = 0.6 ** contradicts
raw     = clamp(base * recency * penalty, 0.05, 0.99)
strength = min(raw, 0.6 if unit < STABLE else 0.9)   # 封顶与 base 用同一个 unit
```

- `REQUIRED ≤ unit < STABLE` → cap 0.6（「初步观察」期）；
- `unit ≥ STABLE` → cap 0.9，永不给 1.0；
- **base 与封顶必须用同一个 `unit`**（v8 修正 P1-1）：否则 `task_performance` 低完成率时 supports 只有样本数的一半，「初步观察」期会被动延长到 12 条样本；
- 三个乘子职责单一，可分别构造用例；
- 阈值与公式落常量；评测里做半衰期 30/60/90 敏感性检查（只进论文）。

### 5.6 裁决权

自动裁决可能错：不删旧证据、事件留痕、记忆只作**依据**，计划调整仍走 `plan_service` 与用户确认。

---

## 6. Agent 接入设计

### 6.1 服务端枚举（【契约变更】，P0 前置）

```python
FeedbackStatus = Literal["completed", "partial", "not_completed"]
FeedbackReason = Literal["任务时间太长", "任务难度太高", "临时有事情", "今天状态不好", "其他"]

status: FeedbackStatus
reason: FeedbackReason | None = None
```

**真实校验顺序（按 FastAPI/Pydantic 实际行为，v2 写错已改）**：

| 序 | 位置 | 失败结果 |
| --- | --- | --- |
| 1 | Pydantic 请求体校验（路由函数**执行之前**）| **HTTP 422**，body 是 FastAPI 默认 `{"detail": [...]}` 结构，**没有 `error_type`** |
| 2 | 路由函数体：`plan_id` 存在性 | 400 `invalid_plan_id` |
| 3 | 路由函数体：任务名一致 | 400 `plan_task_mismatch` |
| 4 | 路由函数体：`status ∈ (partial, not_completed)` 且 `reason` 为空 | 400 `reason_required` |

- **接受 FastAPI 默认 422 结构**，不加统一异常处理器、不伪造 `error_type`（本阶段最小改动；若将来要统一错误体，单独任务）。
- `reason` 条件必填是**有意的 API 契约收紧**，不是向后兼容：`completed` 允许空 `reason`，`partial/not_completed` 必须给合法值。
- 历史数据不回填、不修正；脏值由记忆服务 fail-closed 排除（§5.2 第 3 步）。

**既有测试的兼容影响（必须一起改）**：

| 位置 | 现状 | 处理后 |
| --- | --- | --- |
| `test_feedback_plan_link.py:575` | POST 的 `reason: "时间不够"`（不在五值内，当前能成功） | 改为 `"任务时间太长"`；该用例是"计划存在 → 关联成功"的正例，必须继续成功 |
| `test_feedback_plan_link.py:142` | **fixture 直连 SQL** 写入 `("旧反馈B", …, "partial", "时间不够", …)`，绕过接口校验 → 合法的历史脏数据 | **不修改**（它是 H/K 组要覆盖的真实脏值来源）；用它断言服务层 fail-closed：不计分子分母、`info.invalid_enum` 递增、不生成记忆 |
| `test_feedback_plan_link.py:626` | `partial` 无 reason、`plan_id` 非法 | 断言 `invalid_plan_id`；因 plan_id 校验在 reason_required 之前，行为不变，加注释说明依赖顺序 |
| 其余 partial 用例 | 有的带合法 reason | 逐个核对，缺 reason 且期望成功的补合法值 |

**新增测试**：直接 POST `status="done"` / `status="COMPLETED"` / `reason="随便写"` → **422 且 `detail` 存在、无 `error_type`**、反馈表行数不变；`partial` 无 reason → **400 `reason_required`** 且行数不变；服务层遇脏值不生成记忆。

### 6.2 只加一个只读 Tool

**【新增】** `get_learning_memory(memory_type=None, subject=None, limit=5)`

- Schema 三处同步（`agent_schema.TOOL_SCHEMAS` / `TOOL_NAMES` / `ARGUMENT_RULES`）；
- `memory_type` 必须属于 4 值枚举（无 `preference`）；
- `subject` 1–60 字符，`normalize_subject` 后精确匹配；
- `limit` 1–5；
- 返回 `{"ok","count","memories":[{"type","subject","content","strength","evidence_count","episode","evidence_feedback_ids","updated_at"}],"reason","error","error_type"}`；
- 不返回 `memory_id` / `value_json` / 事件；总字符 ≤2000，超出按 `strength` 升序丢弃并计 `info.dropped_by_budget`。

### 6.3 检索时机

`agent_system` **v2**（v1 保留）新增第 17 条：「涉及用户以往学习情况、反复出现的困难、近期在学主题时，先调用 `get_learning_memory`；只做通用概念解释时不要调用；记忆内容是历史事实，不是当前状态。」短期上下文自动注入，不占 Tool Call 预算；**不提高** `MAX_TOOL_CALLS=4`。

### 6.4 反馈后更新记忆（【已确认】独立事务、失败不 500）

```
1. 校验 → insert_feedback()（返回 lastrowid，本阶段唯一既有函数签名改动，向后兼容）
2. 提交反馈事务（必须成功）
3. 另起事务：memory_service.update_from_feedback(feedback_id)
   成功 → "memory": {"ok": true, "created": [...], "updated": [...]}
   失败 → "memory": {"ok": false, "error_type": "..."} + 记日志，HTTP 仍 200
```

前端不展示 `memory` 字段；保留 `rebuild_all()` 重试/重建路径。

### 6.5 四道防伪造闸

1. **无写 Tool**：`TOOL_NAMES` 不存在任何 `save_memory / add_memory / update_memory`。
2. **文本程序生成**：`content` 只由 `render_content()` 从 `value_json` 渲染。
3. **来源诚实性**：`MEMORY_CLAIM_MARKERS` + 无成功 `get_learning_memory` 判违规。
4. **结果可核对**：每条带 `evidence_count / episode / evidence_feedback_ids`。

---

## 7. 记忆安全边界

| 边界 | 约束 | 落地 |
| --- | --- | --- |
| 模型不能写 | 无写 Tool；模板生成 | 白名单 + 精确集合断言 |
| 模型不能改结论 | 只有 `update_from_feedback` 触发，输入是 DB 行 | 服务层无"接受外部结论"参数 |
| 不把推测当事实 | 只读 `status/reason/数值列/plans.difficulty` | 规则签名限定字段；逐条 fail-closed |
| 不把 RAG 当个人记忆 | 分表 + 分 Tool + 分语义 | 检索只查 `learning_memories` |
| 不解释心理状态 | 无 mood/motivation 类型 | 枚举白名单测试 |
| 用户文本不可信 | JSON 包装 + 边界标记 + 声明 | §2.2 渲染约定 + 断言 |
| 可删除 | **五表**同事务清空（memories / evidence / pending / events / feedbacks） | §3.4 + 测试 |
| 不外泄 | Tool 只回聚合结论与反馈 id | 字段白名单 + 长度上限 |

**不做**：跨用户、分享、云同步、加密存储（单用户本地 SQLite，与 `feedback.db` 同级）。

---

## 8. 自动化测试与评测设计

### 8.1 技术基线

自写脚本 `def test_xxx(): assert ...` + `__main__` 运行器；`check()` / `PASS:` 输出；夹具 `_test_*.db`，`FEEDBACK_DB_PATH` 切换（`database.py:15`），结束删除并断言真实 `feedback.db` 字节不变。

### 8.2 用例矩阵（`test_learning_memory.py`，约 130 例）

| 组 | 编号 | 断言要点 |
| --- | --- | --- |
| 迁移 | M1–M6 | V7 **四张表**/索引存在；V5、V6 老库升级后**四张表**为空；重复 `init_db()` 幂等；`user_version > SCHEMA_VERSION` 抛 `RuntimeError`；`episode` 默认 1 |
| 枚举契约 | V1–V8 | 非法 `status`/`reason` → **422 + `detail` + 无 `error_type`** 且行数不变；`partial` 无 reason → 400 `reason_required`；`completed` 无 reason 仍成功；服务层遇脏值不生成 |
| 归一化 | N1–N6 | 中英/全半角/空白/后缀/空串/超长；不同写法同 key |
| 存储与回溯 | S1–S11 | 2 条难度失败 → 1 记忆 + **2 条 evidence**；单条 → 0；**`task_performance` 3 条样本（1 completed）→ 生成 `low`（samples=3 达门槛、supports=2、rate=0.33）**，门槛数样本而非 supports（P1-1）；**5 条反馈 → 5 行 evidence**（一条被计入的反馈一行，P0-1）；`plan_id IS NULL` 不进 `time_pattern`；`estimated=0` 排除；`difficulty` 非法排除；**两个不同 task 同 difficulty 共同进入 `time_pattern` 同一条记忆**；**无效 `plan_id`/计划缺失/`difficulty` 非法 → `time_pattern` 整类跳过，不创建也不更新该记忆，其他三类照常处理**；`value_json` 可解析 |
| 脏历史 | H1–H3 | 合法触发 + 同窗口非法历史行 → 非法行**不计分子分母**；全非法 → 不生成；`info.invalid_enum` 计数正确（P1-D） |
| 检索 | R1–R8 | 类型/主题/状态过滤；`limit` 越界拒绝；只回 `active`；不相关主题不命中；`evidence_feedback_ids` 正确；空结果 `insufficient_evidence` |
| 去重与不可变 | D1–D9 | 同 subject 5 次 → 1 行 / `evidence_count=5` / evidence 5 行；重复 `feedback_id` 不重复计；**重复调用后既有 evidence 的 `relation`/`episode` 字节不变**；人为构造不一致 → `data_inconsistency` 且不覆盖；**`insert_evidence()` 冲突返回 `inserted=false` 时被当作失败处理而非成功**（P1-2）；**记忆建立后继续输入 3 条新反馈 → 正式 evidence 恰好追加 3 行，且每行 `relation`/`episode` 正确、`evidence_count` 与 `COUNT` 一致**（P0-1）；**再重复调用这 3 条中的任意一条 → evidence 行数与两个计数都不变**（P0-1） |
| pending 缓冲区 | K1–K10 | `resolved`/`archived` 后新反馈**只写 `memory_pending_evidence`**（该表无 relation 列）；`UNIQUE(memory_id, feedback_id)` 防重复；pending 期间 `content`/`value_json`/`strength`/`status`/`episode`/`episode_start_feedback_id`/`last_supported_at`/`updated_at` **逐列不变**；晋升后正式 evidence 一次写齐、pending 行清空；超 30 天窗口的 pending 行被删除且**永不进正式证据**；pending 行不出现在 `retrieve` 与 Tool 返回；**重试一条已在 `memory_evidence` 的旧反馈 → 服务层直接跳过（`info.already_evidenced`），不写缓冲区、不报 `data_inconsistency`**（P1-2）；neutral 样本晋升时被丢弃且不计分子分母；一次晋升只写一条事件；**方向长期不可定（完成率持续 0.6）跨过 30 天后新反馈到来 → 旧 pending 行被删除、`info.pending_expired` 等于删除条数、剩余行不参与方向与门槛计算**（v7 P1） |
| 轮次 | U1–U19 | 反证 → `weakened`；+新支持强度 ≥0.5 → `active`；≥3 反证 → `resolved` 且固定 `pending_*`；`resolved` 后新轮达门槛 → `episode=2` + 计数从 0 + **旧证据仍是 `episode=1` 且未被搬动**；**`task_performance` 新轮 2 条 `completed`（REQUIRED=3）→ pending 2 行、正式 evidence 0 行、`status` 仍 `resolved`**；**数量够但方向不可定（完成率 0.67）→ 不晋升、不写事件、缓冲区保留**；第三条促成方向 → 一次性晋升；`archived` 后单条反馈 **不复活**；`archived` 后达门槛 → `episode+1` + 不新建行；`strength` 随证据升/随时间降/随反证降；`weakened` 期间新支持不换轮；**当前轮追加 3 条支持后 `evidence_count == COUNT(evidence)` 且 `episode` 全为当前轮**（P0-1 计数重算） |
| 冲突与不足 | C1–C6 | 交替证据 → `weakened` 且 contradicts 计数正确；单条不生成；`REQUIRED-1` 不生成；`REQUIRED` 生成且 `strength ≤0.6` 且带「初步观察：」；`STABLE` 后 `strength ≤0.9`；未知枚举不生成 |
| 来源追溯 | E1–E6 | `active` 记忆 ≥1 证据；证据 `feedback_id` 真实存在（JOIN）；`plan_id` 与反馈一致；**当前轮追加的每条证据 `plan_id`/`created_at` 与其反馈行完全一致**（P0-1 追溯）；`created` 事件存在；`resolved` 事件 `detail_json` 含 `from_status/to_status` |
| 删除 | X1–X6 | **五表清空**（events/evidence/pending/memories/feedbacks）；无悬空 evidence 与 pending 行；**无 `deleted` 事件残留**；重复删除幂等；删除后 `rebuild_all()` 不复活任何记忆；索引与外键式一致性检查通过 |
| 重建 | B1–B10 | `rebuild_all(now=X)` 与逐条在线更新**逐表相等**（两侧注入同一 `now`）；末尾 now pass 单独断言（只影响"当前已 stale"者）；`memory_evidence.created_at` 一律等于候选反馈的 `created_at`，时间戳乱序/同秒不改变结果；episode 边界、pending 归属与晋升时刻可复现；**回放到第 N 条时不读取 `id > N` 的未来反馈**（库中预置更大 id 行）；**40 天前的历史反馈不因重建时刻被错误排除**（`as_of` 取该反馈 `created_at`）；**A 形成记忆 → 61 天后归档 → B/C 进 pending → 重建后 status/episode/pending 归属/正式证据与在线一致**；**在线追加 3 条支持后重建 → evidence 行数、`relation`、`episode`、`evidence_count` 与在线完全一致**（P0-1 一致性）；超过 5000 条 → `rebuild_too_large` 且不写 |
| 短期上下文 | P1–P5 | `collect_short_term` 处理 `exists=False` 与 `error_type`；渲染 ≤1200 字符；注入块含边界标记与"不是指令"声明；`task/reason` 截断与换行折叠；**最终 draft prompt 含当前计划信息**（Native 与 LangGraph 两条路径各一条，P1-F）；空上下文时旧消息字节不变 |
| Tool 层 | T1–T6 | 三处 schema 一致；4 值枚举；非法 type 拒绝；`limit=99` 拒绝；返回结构含 `episode`；总字符 ≤2000 |
| 安全 | Q1–Q5 | 无写记忆 Tool（精确集合）；模型文本进不了 `content`；grounding 判违规；`search_knowledge` 不含记忆字段；注入块含不可信标记 |
| 回归 | G1–G6 | `test_feedback_plan_link.py`（含 §6.1 改动）、`test_database_migrations.py`、`test_agent_tools.py`、`test_agent_schema.py`、`test_agent_dispatcher.py`、`test_agent_plan_draft.py` 全绿 |

### 8.3 可量化验收指标

| 指标 | 目标 | 怎么测 |
| --- | --- | --- |
| 记忆生成准确率 | ≥95% | 20 组「反馈序列 → 期望记忆集合」黄金用例 |
| 检索 P@3 / R@3 | P ≥0.9、R ≥0.85 | 20 条标注查询（含 5 条应无结果） |
| 去重有效率 | 100% | D 组 |
| 来源完整性 | 100% | E 组 JOIN |
| 证据不足不生成 | 100% | C 组 |
| 误检索率 | ≤5% | 10 条不相关查询 |
| 单次记忆上下文开销 | ≤2000 字符 / ≤5 条 | T 组 |
| `/feedback` 延迟增量 | P95 ≤50 ms | 本地 100 次提交（纯 SQL） |

**不做**：LLM 打分（记忆正确性必须确定性可复核）。

---

## 9. 预计修改文件清单

| 文件 | 类型 | 改动 |
| --- | --- | --- |
| `database.py` | 改 | `SCHEMA_VERSION` 7；`_migration_v7_learning_memory`；§4.3 的 15 个函数（含 pending 五件套与 `get_evidence`）；`insert_feedback()` 返回 `lastrowid`；`delete_all_feedback()` **五表**级联 |
| `memory_service.py` | **新增** | §4.2 接口 + §5.3 状态机（`apply_feedback` / `_materialize_stale` / `_append_pending` / `_try_promote`）+ 模板 + 规则/门槛常量（约 650–780 行） |
| `memory_context.py` | **新增** | `collect_short_term`（I/O）+ `render_short_term`（纯函数）（约 140 行） |
| `agent_tools.py` | 改 | `get_learning_memory()` + 3 个预算常量 |
| `agent_schema.py` | 改 | Schema / `TOOL_NAMES` / `ARGUMENT_RULES` |
| `agent_dispatcher.py` | 改 | `TOOLS` 加一项 |
| `agent_plan_draft.py` | 改 | `_build_draft_user_content` 加关键字参数 `short_term_block`；调用点补参；加 `MEMORY_CLAIM_MARKERS` 检查 |
| `langgraph_plan_workflow.py` | 改（**最小**）| `DraftState` 加 `short_term_block`；`node_research` 计算并写 state；`node_prepare` 传参。不加节点、不改拓扑 |
| `main.py` | 改 | `Feedback` 枚举化 + `reason_required`；`/feedback` 触发记忆更新 + `memory` 字段；新增 `GET /memory` |
| `prompt_registry.py` | 改 | `agent_system` v2（第 17 条），v1 保留 |
| `test_feedback_plan_link.py` | 改 | `reason` 值修正 + 校验顺序注释（§6.1） |
| `test_learning_memory.py` | **新增** | §8.2 全部用例 |
| `test_database_migrations.py` | 改 | V7 断言 + 级联删除断言 |
| `docs/STAGE2-LEARNING-MEMORY.md` | **新增** | 实现后的交付说明 |

**明确不改**：`agent_loop.py`（预算/超时一行不动）、`rag_service.py` / `embedding_service.py` / `vector_cache.py`、`plans` 表结构、`confirmation_service.py`。

---

## 10. 开发任务拆分与优先级

| 序 | 任务 | 优先级 | 依赖 | 预估 | 验收 |
| --- | --- | --- | --- | --- | --- |
| 0 | 服务端枚举（`Literal` + `reason_required` + 改 `test_feedback_plan_link.py`）| **P0** | — | 0.3 天 | V1–V8；既有测试全绿 |
| 1 | V7 迁移（四张表）+ 14 个 DB 函数 + **五表**级联删除 + 迁移/删除测试 | **P0** | 0 | 0.8 天 | M1–M6、X1–X6 |
| 2 | `normalize_subject` + `render_content` + N 组 | **P0** | 1 | 0.3 天 | 纯函数单测 |
| 3 | `apply_feedback` 单一入口（§5.2 候选规则 + §5.3 状态机，含 time_pattern 的 key 分派）+ `evaluate()` 四类规则（含 neutral）+ **pending 缓冲区、过期清理与原子晋升** + `_materialize_stale` + 追加证据写入与核对 + S/H/D/K/U/C/E 组 | **P0** | 1,2 | **3.6 天** | 全绿；重点中途审查 |
| 4 | `retrieve()` + `rebuild_all()`（as-of 回放）+ R/B 组 + 标注集 | **P0** | 3 | **1.0 天** | P@3/R@3 达标；重建与在线逐表相等 |
| 5 | `memory_context` + 两条路径注入（Native + LangGraph）+ P 组 | P1 | 3 | 0.6 天 | draft prompt 含计划；空上下文零字节变化 |
| 6 | Tool 接入三处 + T/Q 组 | P1 | 4 | 0.5 天 | schema/dispatcher 测试全绿 |
| 7 | `/feedback` 触发（独立事务）+ `GET /memory` + G 组 | P1 | 3 | 0.3 天 | 全量回归 + 延迟达标 |
| 8 | `agent_system` v2 + grounding 扩展 | P2 | 6 | 0.3 天 | 违规用例被判出 |
| 9 | `docs/STAGE2-LEARNING-MEMORY.md` | P2 | 全部 | 0.3 天 | 含真实测试输出 |

合计约 **8.0 人日**（v2 5.6 → v3 6.5 → v4 6.8 → v5 7.7 → v6 7.9 → v7 8.0（任务 3 +0.1）→ v8 8.0（只改口径与措辞，不加人日）；分项相加 0.3+0.8+0.3+3.6+1.0+0.6+0.5+0.3+0.3+0.3 = 8.0）。

**交付节奏**：P0（任务 0–4）一个 PR；P1/P2 另一个 PR。任务 3 风险最高，建议单独 push 一次供中途审查。

---

## 11. 技术风险与替代方案

| 风险 | 影响 | 缓解 / 替代 |
| --- | --- | --- |
| 任务名归并质量 | 「条件概率练习」与「做条件概率题」被视为两个主题 | 确定性归一化，宁少合并不错合并；`subject_label` 保原文便于发现；人工合并 UI 留后续 |
| 历史脏枚举 | 旧库任意 `status/reason` | API 严格校验 + 逐候选过滤 + 服务层 fail-closed |
| 强度分数主观 | 审稿人质疑 | 公式常量公开可复算；半衰期敏感性检查 |
| 证据不可变的代价 | 规则改进后旧结论无法就地修正 | 提供 `rebuild_all()` 显式全量重建；普通路径只报 `data_inconsistency`，不静默改写 |
| episode 边界写错 | 旧证据被计入新轮 | 边界用 `feedback.id`（非时间字符串）；U 组专测"旧证据未被搬动" |
| pending 轮实现走样 | 未达门槛期间状态/强度被 pending 证据污染，或候选反复卷回旧轮 | §5.3 固定两个 pending 字段 + 候选下界只认 `pending_start_feedback_id` + U10 三条连续反馈用例 + 断言旧轮逐列不变 |
| pending 缓冲区无界增长 | 方向长期不可定（如完成率长期 0.6）时过期候选一直留在表里 | 每次 `_try_promote` **第 0 步**删除超 30 天窗口的行并计 `info.pending_expired`，与晋升同事务；上界 = 窗口内候选数；K10 专测 |
| 增量证据只存计数 | `feedback3/4/5` 无法追溯、`MAX(feedback_id)` 不推进、重复调用识别失败 | §5.2 八条规则写死"一条被计入的反馈 = 一行证据"、计数一律 `COUNT` 重算、`inserted=False` 必读回核对；D8/D9/U19/B10 专测 |
| 方向未定与证据不可变冲突 | 新轮 direction 尚不可算时，`relation` 无法确定；沿用旧方向会判错，先猜再改又违反不可变 | **原始候选缓冲区 `memory_pending_evidence`**（无 relation 列）+ 方向确定后一次性转正（§5.3.2/§5.3.3）；K 组专测 |
| 重建边界漂移 | 重建用真实当前时间取窗口，会把历史反馈错误排除 / 把未来反馈提前读入 | 统一 `as_of = trigger.created_at` + `candidate.id <= trigger.id` + 窗口 `as_of−30d ≤ created_at ≤ as_of`（§5.2）；B 组三条专门用例 |
| stale 归档不可重放 | 归档时刻没持久化，重建时 pending 归属错乱 | stale 改为 `as_of − last_supported_at > 60d` 的**派生判定**，处理每条反馈前先 materialize（§5.3.4）；`archive_stale()` 只说"批量化"，不再是唯一入口 |
| 自动裁决出错 | 误判 weakened/resolved | 全留痕；可 reopened / 复活；记忆只作依据 |
| `/feedback` 同步更新记忆 | 写路径变长 | 独立事务 + 失败不 500 + 延迟 ≤50 ms；可退化按需重建 |
| 新增 Tool 挤预算 | `MAX_TOOL_CALLS=4` | prompt 明确"通用解释不调记忆"；不提高上限 |
| 与状态评估边界 | 第三阶段把 mastery 塞进记忆表 | 表结构无 `level/mastery/score`；需要时新建 `learning_states` |
| 短期上下文改 prompt | `test_agent_plan_draft.py` 有内容断言 | 空上下文零字节变化；有内容用固定标题与边界标记 |
| 级联删除误伤 | 用户只想删一条反馈却清空 | 当前 `DELETE /feedback` 本就是全清；单条删除需求记入后续（需按 `feedback_id` 精确清理并重算） |
| 事件表随删除清空 | 删除前后无审计 | 有意为之（**清空就是清空**）；若要审计，需改为软删除设计，属产品决策变更 |

### 【已确认】三个待拍板项（复审已答复）

1. **`/ask` 不注入短期上下文** —— 同意。
2. **加 `memory_events`** —— 同意，最小化，补 `reopened`，`detail_json` 记 `from_status/to_status/evidence_ids/reason`，不开放 API。
3. **记忆更新失败不返 500** —— 同意；独立事务 + `memory.ok=false` + 日志 + `rebuild_all()` 重试路径。

---

## 附：与「禁止事项」对照自查

- 未修改任何代码（本 PR 只有 1 个 md 文件）✔
- 未涉及学习状态评估（无 mastery / level / score / 状态推断）✔
- 未重构 LangGraph（`agent_loop.py` 零改动；`langgraph_plan_workflow.py` 仅加一个 state 字段与传参，不改拓扑）✔
- 未更换数据库（SQLite + 现有迁移，V7 只 `CREATE TABLE IF NOT EXISTS`）✔
- 未重写 Agent Loop ✔
- 未引入不必要复杂技术（无向量库 / 队列 / ORM / 后台框架）✔
- 未把模型推测当事实（模板渲染、只读枚举与数值、逐候选 fail-closed）✔
- 未把 RAG 教材知识当个人记忆（分表、分 Tool、分语义）✔
