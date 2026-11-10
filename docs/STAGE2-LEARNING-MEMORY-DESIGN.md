# 第二阶段：学习记忆管理 —— 方案设计（提案，未编码）

- 基线：`main` @ `fb22365`（第一阶段已合并）
- 代码阅读范围：`database.py`、`main.py`、`feedback_service.py`、`agent_tools.py`、`agent_loop.py`、`agent_schema.py`、`agent_dispatcher.py`、`agent_plan_draft.py`、`context_builder.py`、`langgraph_plan_workflow.py`、`rag_service.py`、`plan_service.py`、`prompt_registry.py`、`static/feedback.html`，以及 `test_agent_tools.py` / `test_agent_loop.py` / `test_database_migrations.py` / `test_feedback_plan_link.py`
- 本文件只提交设计，不含任何代码改动
- 标注约定：**【已实现】** 现有代码已有；**【新增】** 本方案要加；**【待定】** 需要你或审查方拍板

---

## 1. 现有记忆能力审查报告

### 1.1 结论先说

**当前系统没有任何长期记忆能力。** 全仓 `grep`「记忆 / memory」只命中两处注释与一个测试断言，没有表、没有服务、没有 Tool。所谓「上下文」目前只有两种，都不构成记忆：

| 现有机制 | 代码位置 | 本质 | 是否算记忆 |
| --- | --- | --- | --- |
| 最近反馈读取 | `agent_tools.get_recent_feedback()` `agent_tools.py:147` → `database.get_feedback_since()` `database.py:279` | 每次按 `created_at >= cutoff` 现查最近的原始行，最多 20 条、窗口 1–30 天 | 否，是**每次重算的原始数据视图** |
| 确定性统计 | `feedback_service.calculate_summary()` `feedback_service.py:12` | 对这次查出来的列表做计数与均值 | 否，是**瞬时聚合**，不落库 |
| 知识库检索 | `rag_service.retrieve_chunks()` `rag_service.py:545`，语料在 `knowledge/**/*.txt` | 教材/资料语料，`TOP_K=3`、`MIN_SCORE=0.5`，bge-m3 向量 + BM25 混合 | 否，是**公共知识**，不是用户数据 |
| 上下文组装 | `context_builder.build_context()` `context_builder.py:85` | 对检索块做 `(source, content)` 去重 + 6000 字符预算 + 来源标签 | 否，是无状态的装配器 |
| 计划持久化 | `plans` 表 + `plan_service` | 用户确认后的单日计划 | 不是记忆，是业务实体 |

### 1.2 逐项核实（用户点名的 7 个问题）

1. **`feedbacks.plan_id` 已落地**【已实现】`database.py:145` `_migration_v6_feedback_plan_link()` 加列 + `idx_feedbacks_plan_id`；`main.py:378-398` 在写库前校验「计划存在」与「任务名折叠空白后完全相等」；`get_feedback_list()` / `get_feedback_since()` 均已返回 `plan_id`（`database.py:245`、`database.py:301`）。
2. **`feedbacks` 真实结构**【已实现】12 列：`id, task, estimated_minutes, actual_minutes, status, reason, completed_subtasks, total_subtasks, completed_questions, total_questions, created_at, plan_id`。注意两个硬约束：
   - `status` 只有 `completed / partial / not_completed` 三个取值（`static/feedback.html:239/247/255`）；
   - `reason` 是**固定枚举**：`任务时间太长 / 任务难度太高 / 临时有事情 / 今天状态不好 / 其他`（`static/feedback.html:269-297`），只在 partial / not_completed 时必填。
   - `created_at` 是 `datetime.now().isoformat()` 的**本地无时区**字符串（`main.py:400`），`database.py:285-286` 已明确「同格式下字典序等价于时间序」。
3. **Agent 每次执行如何构建上下文**【已实现】`agent_loop.run_agent()` `agent_loop.py:257`：system prompt 来自 `prompt_registry.render("agent_system")`（`agent_loop.py:104-120`，现有 16 条规则）+ 用户消息；**不注入任何历史数据**，历史只能靠模型自己调 Tool 去取。预算：`MAX_TOOL_CALLS=4`、`MAX_LOOP_TURNS=3`、`MAX_RUN_SECONDS=120`（`agent_loop.py:24-28`）。
4. **近期反馈如何读取与传递** 模型调 `get_recent_feedback(days)` → `get_feedback_since` → `_sanitize_feedback()`（`agent_tools.py:83`，字段白名单 + 任务名截 60 字 / 原因截 40 字，**丢弃 `id` 与 `plan_id`**）→ 结果以 JSON 塞进 `role=tool` 消息。`agent_plan_draft._collect_tool_data()`（`agent_plan_draft.py:181`）再从 `steps` 里回收真实 Tool Result 供起草使用。
5. **Tool Dispatcher 如何限制调用**【已实现】`agent_dispatcher.TOOLS` 是 3 个函数的显式字典（`agent_dispatcher.py:20-24`），无 `getattr/eval`；`_validate_arguments()`（`:44`）按 `agent_schema.ARGUMENT_RULES`（`agent_schema.py:117`）拦「未知参数 / 缺必填 / 类型 / 范围」，**不钳制、直接拒绝**；`dispatch()` 捕获所有异常转为 `tool_failed`（`:135-138`）。写能力只有 `save_plan`，且权限来自 `confirmation_service` 的服务端凭证，模型参数无法伪造（`agent_tools.py:312`）。
6. **RAG 与用户学习记忆的边界**【已实现，且必须保持】`rag_service` 的语料目录是 `KNOWLEDGE_PATH`，当前只有 `knowledge/math.txt`（480 B）和 `knowledge/条件概率.txt`（136 B），是**教材类公共知识**；`search_knowledge` 的返回值只有 `source / score / content`（`agent_tools.py:100-112`）。用户个人数据从未进入该索引，也没有任何写入口能改它（`/upload` 是人工上传 `.txt`）。
7. **当前可复用的记忆能力** 只有三样**模式**可以借，没有一件能直接复用：
   - `feedback_service.calculate_summary` 的「先算客观指标、再交给 LLM 解释」口径；
   - `agent_plan_draft.check_grounding()`（`agent_plan_draft.py:105`）的「声称来源必须有真实 Tool Result」检查；
   - `context_builder` 的「去重 + 字符预算 + 来源标签」组装法。

### 1.3 因此第二阶段要补的三块空白

- **没有事实沉淀层**：现在每次都要把 20 条原始反馈重新读一遍、重新算一遍；跨 30 天以上的规律（例如「条件概率反复失败」）在窗口滑走后彻底消失。
- **没有来源可追溯的结论**：`calculate_summary` 只给聚合数，无法回答「这条结论是哪几条反馈支撑的」。
- **没有更新/失效机制**：用户后来攻克了难点，系统仍然只知道「最近 7 天里有一条难度太高」。

---

## 2. 短期记忆与长期记忆设计

### 2.1 总原则

1. **短期记忆不落库。** 用户明确要求「不要为了短期记忆而无意义地重复存储数据库已有数据」。短期记忆 = 一次 Agent Run 内**程序即时组装**的只读视图，用完即弃。
2. **长期记忆必落库、必带证据、必可失效。** 每条长期记忆 = 一个**结构化结论**（不是散文）+ N 条证据行（`feedback_id` / `plan_id`）。
3. **写入权在程序，不在模型。** 记忆文本由**模板 + 结构化取值**渲染；LLM 永远没有写记忆的 Tool。这是「区分程序计算结果 / 大模型生成内容」在记忆层的具体落实。

### 2.2 短期记忆（一次 Run 内的组装）

**【新增】** `memory_context.py`，纯函数、无副作用、不 import `main`（与 `feedback_service.py` 同风格），对外两个入口：

| 函数 | 输入 | 输出 | 数据来源（全部只读现查） |
| --- | --- | --- | --- |
| `build_short_term_context(plan_date=None)` | 日期 | `{"text": str, "facts": dict, "info": dict}` | `plan_service.get_active_plan()`（当前计划）、`get_feedback_since(7, 20)`（最近反馈）、`calculate_summary()`（进度）、按 `reason` 计数得到的「最近困难」 |
| `render_for_prompt(short_term, max_chars=None)` | 上述结果 | ≤1200 字符的定长文本块 | 无（纯渲染） |

组装内容与**不重复存储**的落实：

- **当前学习计划**：只取 `plan_id / task / estimated_minutes / difficulty / question_count / completed_subtasks 总量`，从 `plans` 现查，不复制进记忆表。
- **最近学习反馈**：复用 `get_feedback_since`，只取**最近 3 条**的 `task / status / reason / created_at`（比 `get_recent_feedback` 的 20 条更省 token），并明确标注「共 N 条，此处仅列最近 3 条」。
- **当前学习进度**：直接调 `calculate_summary`，不新增计算逻辑。
- **最近遇到的困难**：对最近 7 天 `reason` 做 `Counter`，输出 `任务难度太高 ×2、任务时间太长 ×1` 这种**可核对**的形态。
- 字符预算：默认 1200 字符，超限按「计划 → 困难 → 最近反馈」优先级整段丢弃（不截断句子），`info` 记录丢弃项，与 `context_builder` 的「不静默截断」保持一致。

接入点（**不改 `run_agent` 签名**）：`agent_plan_draft.py` 里研究阶段的 user message 与 `langgraph_plan_workflow.node_research()` 的 research message 前，拼上 `render_for_prompt(...)`。理由：`run_agent` 被 6 个测试文件直接调用，改签名会波及既有用例；而「把短期上下文写进 user message」在语义上等价且零风险。

> **【待定 1】** 是否允许 `/ask` 也注入短期上下文？我的建议：**不注入**。`/ask` 是知识问答，注入学习历史会让「根据知识库回答」变成「根据我的历史回答」，与 `prompt_registry.ask_rag` 的口径冲突。

### 2.3 长期记忆（跨会话沉淀）

**五种记忆类型**（`memory_type` 枚举，程序常量，不接受模型自定义）：

| 类型 | 键（`subject_key`） | 结构化取值 `value_json` | 产生条件（全部程序判定） |
| --- | --- | --- | --- |
| `difficulty` | 归一化任务名 | `{"reason":"任务难度太高","failed_count":2,"window_days":30}` | 同一 subject 在 30 天内 **≥2 条** `reason=任务难度太高` 且 `status in (partial, not_completed)` |
| `task_performance` | 归一化任务名 | `{"samples":4,"completed":1,"completion_rate":0.25}` | 同一 subject **≥3 条**任意反馈，且完成率 ≤0.5 或 ≥0.8（中间区间不产生结论） |
| `time_pattern` | 任务类型（`difficulty` 字段值：easy/medium/hard） | `{"samples":5,"median_ratio":1.6}` | **≥4 条**同难度反馈，`actual/estimated` 中位数 ≥1.5（普遍超时）或 ≤0.4（普遍提前） |
| `knowledge_point` | 归一化任务名 | `{"appearances":3,"last_status":"partial"}` | 同一 subject **≥3 次**出现，作为「最近在学什么」的索引 |
| `preference` | 偏好键 | `{"value":"...","source":"user_stated"}` | **仅当用户在界面上明确勾选**；当前无此 UI → **本阶段不产生该类型**（见 §11 风险） |

**明确不做的事**：

- 不从任务名里「用 LLM 抽取知识点」——任务名是自由文本（如「条件概率练习」），自动抽知识点等于让模型编造分类。`subject_key` 只用**确定性归一化**：`NFKC → 去空白 → 去尾部「练习/复习/练习册」后缀 → 小写（英文）**，归一化规则写成纯函数并单测。
- 不把单条反馈提升为长期结论（`evidence_count >= 2` 是硬门槛，`task_performance` 要 3、`time_pattern` 要 4）。
- 不做「心理状态」类记忆（对应 `prompt_registry.analyze_feedback` v1 的既有禁令）。

**记忆文本的生成方式**【新增】：`content` 由**模板**渲染，例如

- `difficulty` → `「{subject}」相关任务 {failed_count} 次因难度太高未完成（最近 {last_seen}）`
- `task_performance` → `「{subject}」最近 {samples} 次任务完成率 {rate:.0%}`
- `time_pattern` → `{difficulty_label} 任务最近 {samples} 次实际用时是预估的 {ratio:.1f} 倍`

模板集中在 `memory_service.TEMPLATES`，与 `prompt_registry` 同风格（集中、可版本化），并且**取值为程序计算**，所以同一份证据永远渲染出同一句话（可重复、可测试）。

### 2.4 「难度判断」的边界（与第三阶段的分工）

本阶段**只记事实**：`failed_count / completion_rate / median_ratio / appearances` 全是可复算的计数与比例。
**不产出**「知识点掌握程度」「学习状态」「是否适合加难度」这类判断——那是第三阶段状态评估的职责。记忆表里不允许出现 `level / mastery / score` 之类字段。

---

## 3. 数据库 ER 结构与迁移方案

### 3.1 新增两张表（V7）

**【新增】** 迁移 `_migration_v7_learning_memory(conn)`，`SCHEMA_VERSION` 6 → 7，追加进 `MIGRATIONS` 元组末尾，**不改 V1–V6 任何一行**（与第一阶段同一纪律）。

```sql
CREATE TABLE IF NOT EXISTS learning_memories (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type        TEXT    NOT NULL,           -- difficulty / task_performance / time_pattern / knowledge_point / preference
    subject_key        TEXT    NOT NULL,           -- 归一化后的稳定键（任务名 / 难度值）
    subject_label      TEXT    NOT NULL,           -- 展示用原文（首个证据的任务名原文，截 60 字）
    content            TEXT    NOT NULL,           -- 模板渲染出的一句话结论（程序生成）
    value_json         TEXT    NOT NULL,           -- 结构化取值，便于复算与评测
    confidence         REAL    NOT NULL DEFAULT 0, -- 0~1，程序公式（见 §5.4）
    evidence_count     INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'active',  -- active / weakened / resolved / archived
    first_seen_at      TEXT    NOT NULL,
    last_supported_at  TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_type_subject
    ON learning_memories(memory_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_memories_status_updated
    ON learning_memories(status, updated_at);

CREATE TABLE IF NOT EXISTS memory_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   INTEGER NOT NULL,
    feedback_id INTEGER NOT NULL,
    plan_id     INTEGER,               -- 可空：反馈本身可能没有关联计划
    relation    TEXT    NOT NULL,      -- supports / contradicts / resolves
    weight      REAL    NOT NULL DEFAULT 1.0,
    created_at  TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_memory_feedback
    ON memory_evidence(memory_id, feedback_id, relation);
CREATE INDEX IF NOT EXISTS idx_evidence_feedback ON memory_evidence(feedback_id);
CREATE INDEX IF NOT EXISTS idx_evidence_memory ON memory_evidence(memory_id);
```

### 3.2 字段设计说明

| 字段 | 为什么需要 |
| --- | --- |
| `subject_key` 唯一 | 去重的物理保证：同一类型同一主题**永远只有一行**，靠 DB 唯一索引兜底，不靠服务层自觉 |
| `subject_label` | `subject_key` 是归一化形态（可能小写、去后缀），展示要给原文；单独存避免每次回查 feedbacks |
| `value_json` | 让「结论」可复算：评测要断言 `completion_rate=0.25` 这种值，而不是比对一句中文 |
| `confidence` | 让「证据强弱」可排序、可衰减；同时也是「证据不足」的量化表达 |
| `status` 四态 | 覆盖 §5 的全部更新语义（当前有效 / 被削弱 / 已克服 / 已归档） |
| `memory_evidence.relation` | 冲突处理必须能区分「支持」与「反证」，否则无法解释「为什么这条被削弱」 |
| `plan_id` 冗余 | 用户要求「每条记忆能追到 `feedbacks.id` / `feedbacks.plan_id` / `plans.id`」；反馈表已有 `plan_id`，但记忆直接带上可以一次 JOIN 到位，不必依赖反馈侧的关联是否完整 |

**不加外键**：与 V6 同一决定（SQLite 外键需 `PRAGMA foreign_keys=ON`，现有全仓连接都没开）。改为**服务层 + 测试**保证引用完整性：删除反馈的既有入口是 `DELETE /feedback`（`main.py:468`，`delete_all_feedback()` 清空全表）——见 §3.4 的兼容处理。

### 3.3 索引与规模

单用户 MVP：`learning_memories` 预计 **几十到几百行**（受 `subject_key` 去重约束），`memory_evidence` 是反馈条数的 1~3 倍。检索全部走 `(memory_type, subject_key)` 与 `(status, updated_at)` 两个索引，**不需要向量库、不需要分页深翻**。

### 3.4 历史数据兼容

- **旧库升级**：V7 只 `CREATE TABLE IF NOT EXISTS`，不动既有表；老库（`user_version=5`、6）升级后两张新表为空，`/feedback` 历史行自然没有记忆——**不回填**。理由同 V6：没有可靠的判定依据，回填等于伪造。
- **`DELETE /feedback` 清空后**：`memory_evidence` 会指向不存在的 `feedback_id`。处理方案【新增】：`delete_all_feedback()` 之后调用 `memory_service.rebuild_or_clear()`，把 `status='active'` 的记忆全部转为 `archived` 并写一条 `memory_events`（见 §3.5）——**不静默保留不可追溯的记忆**。
- **降级保护**：V7 库用旧代码打开仍走既有 `RuntimeError("数据库版本 7 高于程序支持的 6…")`（`database.py:185-189`），无需改动。

### 3.5 是否要第三张表（`memory_events`）

**【待定 2】** 我倾向**加**，但只存最小审计信息（`memory_id / event_type / detail_json / created_at`，`event_type ∈ created / strengthened / weakened / resolved / archived`）。

- 加的理由：论文要论证「记忆可追溯、可解释」，需要能回答「这条记忆什么时候被谁推翻的」；且冲突处理必须有历史，否则只剩一个当前态，无法展示「曾经认为难 → 后来攻克」。
- 不加的理由：+1 张表 +1 段 CRUD，违反「不要为技术复杂度而复杂」。
- 折中建议：**本阶段加表，但只写不读**（唯一读者是测试与将来的评测），写入点集中在 `memory_service._log_event()` 一处，成本约 30 行。

---

## 4. 记忆服务模块与接口设计

### 4.1 分层（与现有习惯一致：服务层做业务，`main.py` 只做 IO 与错误码）

```
main.py (/feedback, GET /memory)          ← HTTP 边界：参数校验、错误码
        ↓
memory_service.py                          ← 业务规则：生成 / 更新 / 去重 / 失效 / 检索
        ↓
database.py (V7 表 + memories 的 CRUD)     ← 只做 SQL，不做判断
        ↑
memory_context.py                          ← 短期记忆组装（纯函数，只读 database/plan_service）
```

### 4.2 `memory_service.py` 对外接口（草案）

| 函数 | 签名 | 说明 |
| --- | --- | --- |
| 归一化 | `normalize_subject(task) -> (key, label)` | 纯函数；`NFKC → 去空白 → 去后缀 → 小写`；空串返回 `(None, None)` |
| 主入口 | `update_from_feedback(feedback_id) -> {"ok": bool, "memory_ids": [...], "error_type": str\|None}` | 读一条反馈 → 对 4 类规则各评估一次 → upsert 记忆 + 追加证据；幂等（同一 `feedback_id` 重复调用不重复计数，靠 `idx_evidence_memory_feedback` 唯一索引） |
| 批量重建 | `rebuild_all(limit=500) -> {"ok": bool, "memories": int}` | 从 `get_feedback_list()` 全量重算（用于迁移后校准、`DELETE /feedback` 后清理、测试） |
| 检索 | `retrieve(memory_type=None, subject=None, status="active", limit=10) -> {"ok": bool, "count": int, "memories": [...], "error": str\|None}` | 结构化过滤 + 关键词；`limit` 硬上限 10；每条回传 `evidence_feedback_ids`（最多 5 个） |
| 失效 | `archive_stale(days=60) -> int` | 把 `last_supported_at` 早于 N 天且 `status='active'` 的转为 `archived`；**只由显式调用触发，不在读路径里做** |
| 渲染 | `render_content(memory_type, value, label) -> str` | 模板渲染，纯函数，可单测 |

错误语义沿用项目习惯：稳定 `error_type` 字符串（`invalid_argument / db_error / not_found`），不抛裸异常给上层。

### 4.3 `database.py` 新增函数（只做 SQL）

`list_memories(memory_type=None, subject_key=None, status=None, limit=10)`、`get_memory_by_key(memory_type, subject_key)`、`insert_memory(...)`、`update_memory_state(id, ...)`、`insert_evidence(...)`、`list_evidence(memory_id, limit=5)`、`count_evidence_by_relation(memory_id)`、`insert_memory_event(...)`。

全部参数化，全部返回 `dict`（与 `_plan_row_to_dict` 同风格），不返回 `sqlite3.Row`。

---

## 5. 记忆更新与去重策略

### 5.1 去重（用户点名的第 4 种情况）

- **物理层**：`UNIQUE(memory_type, subject_key)`。同一主题同一类型只允许一行。
- **证据层**：`UNIQUE(memory_id, feedback_id, relation)`。同一条反馈不会给同一条记忆重复加权（`update_from_feedback` 幂等的前提）。
- **语义层**：`normalize_subject` 让「条件概率练习」「条件概率」「条件概率 复习」落到同一个 `subject_key`。这是本阶段**唯一**允许的「模糊匹配」，且有确定性单测固定行为；不引入编辑距离/向量相似度。

### 5.2 四种状态迁移（对应「用户后来已克服」「相互矛盾」「已经过时」）

| 触发 | 迁移 | 说明 |
| --- | --- | --- |
| 新证据支持当前结论 | `active → active` | `evidence_count+1`、`last_supported_at` 刷新、`confidence` 按 §5.4 重算 |
| 出现反证但反证不足 | `active → weakened` | 例如 `difficulty` 记忆遇到 1 条 `completed`：`confidence × 0.6`，`relation='contradicts'` 落证据，**内容不变但标记可信度下降** |
| 反证足够推翻（≥3 条连续 `completed`，且都晚于最后一次 `difficulty` 证据） | `difficulty → resolved` | 内容改写成「「{subject}」此前 N 次因难度太高未完成，最近 3 次已完成」，旧证据全部保留（`relation='supports'` 不动，新增 `resolves`） |
| `last_supported_at` 超过 60 天无新证据 | `→ archived` | 只在 `archive_stale()` 显式调用时发生；`archived` 不出现在默认检索结果里 |

**注意**：`weakened → resolved` 允许；`resolved → active` **不允许自动回退**（要重新犯同样错误 ≥2 次才会新建一条 `active`——但那会撞唯一索引）。折中：`resolved` 的记忆再遇到 2 条同类失败时，把 `status` 改回 `active` 并把 `evidence_count` 归零重算，同时写 `memory_events(event_type='reopened')`。这是**唯一**的自动回退路径，且必须有事件记录。

### 5.3 「证据不足」的显式表达

- 单条反馈 → 不产生任何记忆（`update_from_feedback` 直接返回 `{"ok": True, "memory_ids": []}`）。
- 达到门槛但样本少于「稳定门槛」（例如 `task_performance` 只有 3 条）→ `confidence` 封顶 0.6，并在 `content` 前缀加「初步观察：」（模板判断，不由模型决定）。
- 检索结果为空时，Tool 返回 `count=0` + `reason="insufficient_evidence"`，让模型能说「目前记忆里还没有足够依据」，而不是编造。

### 5.4 `confidence` 公式（必须先讲清楚，否则会被质疑主观）

```
base = min(1.0, evidence_count / required_evidence)        # required: difficulty=2, performance=3, time=4, kp=3
recency = 0.5 ** (days_since_last_support / 30)             # 30 天半衰期
penalty = 0.6 ** contradict_count                           # 每条反证打 6 折
confidence = round(clamp(base * recency * penalty, 0.05, 0.99), 2)
```

- 每个乘子都有单一职责，可以单独构造用例（改时间、加反证、加证据）。
- 阈值（0.5 半衰期天数、0.6 惩罚、上下限）写成模块常量，评测时可对比调参。
- **不做的**：不把 `confidence` 解释成「用户掌握概率」——它只是「这条结论有多少当前证据」的度量，文档与 Prompt 里都必须这样描述。

### 5.5 冲突的最终裁决权

任何自动裁决都可能错，所以：

- 冲突不删除旧证据，`memory_events` 留痕；
- 记忆只用于**提供依据**，不直接改计划：计划调整仍要走 `plan_service` 的规则与用户确认（第一阶段已固化的 `confirmation_id` 机制）。

---

## 6. Agent Tool 接入方案

### 6.1 只加一个只读 Tool

**【新增】** `get_learning_memory(memory_type=None, subject=None, limit=5)`

- Schema 落 `agent_schema.TOOL_SCHEMAS` / `TOOL_NAMES` / `ARGUMENT_RULES` 三处（现有结构要求三处同步，测试已有 `test_agent_schema.py` 覆盖一致性）；
- `memory_type` 必须属于 5 个枚举值之一，否则直接 `invalid_arguments`（不静默忽略）；
- `subject` 为 1–60 字符字符串，走 `normalize_subject` 后精确匹配，不做模糊检索；
- `limit` 1–5（**注意比 `search_knowledge` 的 5 更保守**：记忆是个人结论，一次只给 5 条）；
- 返回值固定结构：`{"ok", "count", "memories": [{"type","subject","content","confidence","evidence_count","evidence_feedback_ids","updated_at"}], "reason", "error", "error_type"}`；
- 不返回 `memory_id`、不返回 `value_json` 原始结构（避免模型过度解读内部字段）、不返回其他用户的任何东西（本阶段单用户，仍保留字段白名单习惯）。

### 6.2 Agent 什么时候检索记忆

- **不强制**：写进 `agent_system` prompt 的新规则（建议加为第 17 条）——「涉及用户以往学习情况、反复出现的困难、学习偏好时，先调用 `get_learning_memory`；只做通用概念解释时不要调用」。
- **短期上下文自动注入**（§2.2），不占 Tool Call 预算。
- 预算不变：`MAX_TOOL_CALLS=4` 仍是硬上限，新增 Tool 会分走预算，**不提高上限**（提高会让「工具预算超限」的既有测试与行为全部改变）。

### 6.3 反馈提交后什么时候更新记忆

**【新增】** `POST /feedback` 成功写库之后、返回响应之前，**同步**调用 `memory_service.update_from_feedback(feedback_id)`：

- 需要 `insert_feedback` 返回 `lastrowid`（当前返回 `None`，要改成返回 id —— 这是本阶段唯一对既有函数签名的改动，向后兼容）；
- **失败不回滚反馈**：反馈是用户数据，记忆是可重建的派生数据。响应里附 `"memory": {"ok": false, "error_type": "..."}`，前端不展示，日志告警；
- 不引入后台线程/队列（单用户 MVP，一次更新是 3~6 条 SQL，耗时毫秒级）。

> **【待定 3】** 是否允许「记忆更新失败时让 `/feedback` 返回 500」？我的建议：**不允许**。理由同上：用户提交的反馈必须成功落库。

### 6.4 防止 Agent 伪造学习记忆（四道闸）

1. **没有写 Tool**：`TOOL_NAMES` 里不存在 `save_memory / add_memory / update_memory`；Dispatcher 白名单之外一律 `unknown_tool`（`agent_dispatcher.py:123-128`）。
2. **记忆文本程序生成**：`content` 只能由 `render_content()` 从 `value_json` 渲染；没有任何路径接收模型给的字符串写库。
3. **来源诚实性检查**：复用 `check_grounding()` 模式，新增 `MEMORY_CLAIM_MARKERS`（「根据我的记忆」「我记得你之前」「你的长期记忆显示」…），若文本声称依据记忆但 `steps` 里没有成功的 `get_learning_memory`，判违规并要求重写（接入点在 `agent_plan_draft` 的校验阶段，与现有 knowledge/feedback 检查并列）。
4. **Tool Result 可核对**：返回的每条记忆都带 `evidence_count` 与 `evidence_feedback_ids`，模型若在回答里夸大（例如把 2 次说成 5 次），属于可被评测集捕获的错误类型（§8 用例 E7）。

### 6.5 如何限制一次读取的记忆量

三层硬约束：`limit ≤ 5`（dispatcher 拒绝越界）、单条 `content ≤ 200` 字符（服务层截断）、整个 Tool Result ≤ 2000 字符（超出按 confidence 从低到高丢弃，并在 `info` 记录 `dropped_by_budget`）。这条预算写进测试，防止将来放宽时无感退化。

---

## 7. 记忆安全边界

| 边界 | 具体约束 | 落地方式 |
| --- | --- | --- |
| 模型不能写 | 无写 Tool；`content` 由模板生成 | Dispatcher 白名单 + 代码评审（测试断言 `TOOL_NAMES` 精确集合） |
| 模型不能改结论 | 只有 `update_from_feedback` 能触发重算，输入是数据库里的反馈行 | 服务层无「接受外部结论」的参数 |
| 不能把推测当事实 | 规则只接受 `status / reason`（都是用户在界面上选的枚举）与数值列；不含任何自由文本推断 | `update_from_feedback` 只读这 6 个字段 |
| 不能把 RAG 当个人记忆 | 记忆检索只查 `learning_memories`，`search_knowledge` 只查 `knowledge/**`；两个 Tool 的返回结构、`source` 语义不同 | 分表 + 分 Tool + 提示词规则 |
| 不解释心理状态 | 记忆类型里没有 `mood / motivation / mental_state`；`reason` 的文案只记录用户所选原因，不改写成「因为焦虑」 | 枚举白名单测试（未知 reason 不产生记忆） |
| 个人数据不外泄 | Tool 只返回聚合结论与反馈 id，不返回任务原文以外的自由文本 | 字段白名单 + 长度上限 |
| 可删除 | `DELETE /feedback` 后旧记忆转 `archived`；另提供 `memory_service.rebuild_all()` 让用户可「重算」 | 迁移脚本与 API 各留一个出口 |

**本阶段明确不做**：跨用户记忆、记忆分享、云端同步、记忆加密存储（单用户本地 SQLite，与现有 `feedback.db` 同一安全级别）。

---

## 8. 自动化测试与评测设计

### 8.1 测试技术基线（沿用第一阶段，不引入 pytest）

自写脚本：`def test_xxx(): assert ...` + `__main__` 运行器；`check()` 风格与 `PASS:` 输出；夹具一律 `_test_*.db` 独立库，结束时删除，并断言真实 `feedback.db` 字节不变；`FEEDBACK_DB_PATH` 环境变量切换（`database.py:15`）。

### 8.2 用例矩阵（`test_learning_memory.py`，预计 40~45 个用例）

| 组 | 编号 | 断言内容 | 覆盖要求 |
| --- | --- | --- | --- |
| 迁移 | M1–M5 | V7 建表/索引存在；旧库（V5、V6）升级后两表为空；重复 `init_db()` 幂等；V7 库被旧代码拒绝的等价断言（`user_version > SCHEMA_VERSION` 抛 `RuntimeError`） | 数据库设计 |
| 归一化 | N1–N6 | 中英文、全半角、首尾空白、「练习/复习」后缀、空串、超长截断；同一主题不同写法落同一个 key | 去重前提 |
| 存储正确性 | S1–S6 | 2 条同类难度失败 → 1 条 `difficulty` + 2 条证据；单条 → 0 条；3 条完成率 1/3 → 1 条 `task_performance` 且 `completion_rate=0.33`；`value_json` 可 JSON 解析 | 需求 1 |
| 检索准确性 | R1–R8 | 按 type / subject / status 过滤；`limit` 越界拒绝；只返回 `active`；关键词命中；**不相关主题不返回**（查「线性代数」不得命中「条件概率」）；`evidence_feedback_ids` 正确 | 需求 2、8 |
| 去重 | D1–D4 | 同一 subject 提交 5 次 → 仍 1 行、`evidence_count=5`；同一 `feedback_id` 重复调用 `update_from_feedback` → 不重复加权；不同 `memory_type` 同 subject → 2 行 | 需求 3 |
| 更新 | U1–U6 | 反证 → `weakened`；≥3 连续完成 → `resolved` 且内容改写；`resolved` 后再 2 次失败 → `reopened`；`confidence` 随证据增加而升、随时间衰减而降 | 需求 4、5 |
| 冲突与不足 | C1–C5 | 交替证据 → `weakened` 且 `contradict_count` 正确；证据不足 → 0 条记忆 + `reason="insufficient_evidence"`；未知 `reason` / 未知 `status` 不产生记忆 | 需求 5、7 |
| 来源追溯 | E1–E5 | 每条 `active` 记忆至少 1 条证据；证据的 `feedback_id` 必须在 `feedbacks` 里真实存在（SQL JOIN 断言）；`plan_id` 与反馈一致；删除全部反馈后记忆转 `archived`；`memory_events` 有对应记录 | 需求 6 |
| Tool 层 | T1–T6 | `get_learning_memory` 在 `TOOL_NAMES` / `TOOL_SCHEMAS` / `ARGUMENT_RULES` 三处一致；非法 `memory_type` 被拒；超长 `subject` 被拒；`limit=99` 被拒；返回结构含 `evidence_count`；总字符 ≤2000 | Agent 接入 |
| 安全 | X1–X5 | 不存在写记忆 Tool（白名单精确集合）；模型的自然语言无法进入 `learning_memories.content`（构造 `update_from_feedback` 只读 DB 行的断言）；`check_grounding` 对「根据我的记忆」但无 Tool Result 判违规；`search_knowledge` 结果不含记忆字段；记忆 Tool 结果不含 `value_json` | 安全边界 |
| 回归 | G1–G4 | `test_feedback_plan_link.py`、`test_database_migrations.py`、`test_agent_tools.py`、`test_agent_schema.py` 仍全绿（`/feedback` 响应新增 `memory` 字段不破坏既有断言） | 不回归 |

### 8.3 可量化验收指标

| 指标 | 目标 | 怎么测 |
| --- | --- | --- |
| 记忆生成准确率 | ≥95% | 构造 20 组「反馈序列 → 期望记忆集合」黄金用例，逐组比对类型/键/`value_json` 关键字段 |
| 记忆检索 P@3 / R@3 | P ≥0.9、R ≥0.85 | 20 条人工标注查询（含 5 条「应无结果」），`retrieve()` 结果与标注比对 |
| 去重有效率 | 100% | D 组用例（重复提交、重复反馈） |
| 来源完整性 | 100% | E 组 SQL JOIN 断言 |
| 证据不足不生成 | 100% | C 组（单条、门槛差 1 条、未知枚举三种边界） |
| 误检索率 | ≤5% | 10 条「不相关查询」，统计错误命中 |
| 记忆上下文开销 | ≤2000 字符 / ≤5 条每次 | T 组字符数断言 |
| 端到端延迟增量 | `/feedback` P95 增量 ≤50 ms | 本地 100 次提交计时（不含 LLM，纯 SQL） |

**不做的评测**：不让 LLM 参与打分（`eval_metrics.py` 那套是给 Agent 回答用的，记忆正确性必须是确定性的断言，否则论文里无法复核）。

---

## 9. 预计修改文件清单

| 文件 | 类型 | 改动 |
| --- | --- | --- |
| `database.py` | 改 | `SCHEMA_VERSION` 7；追加 `_migration_v7_learning_memory`、`MIGRATIONS` 末项；新增 §4.3 的 8 个函数；`insert_feedback()` 返回 `lastrowid` |
| `memory_service.py` | **新增** | §4.2 的 6 个接口 + 模板 + 规则常量（约 350–450 行） |
| `memory_context.py` | **新增** | 短期记忆组装与渲染（约 120 行） |
| `agent_tools.py` | 改 | 新增 `get_learning_memory()`（约 60 行）；常量 `MAX_MEMORY_ITEMS=5`、`MAX_MEMORY_CONTENT_CHARS=200`、`MAX_MEMORY_RESULT_CHARS=2000` |
| `agent_schema.py` | 改 | `TOOL_SCHEMAS` 追加一条、`TOOL_NAMES` 加名字、`ARGUMENT_RULES` 加规则 |
| `agent_dispatcher.py` | 改 | `TOOLS` 加一项映射（1 行） |
| `agent_plan_draft.py` | 改 | 研究阶段 user message 注入短期上下文；校验阶段加 `MEMORY_CLAIM_MARKERS` 检查（约 25 行） |
| `main.py` | 改 | `/feedback` 触发记忆更新 + 响应加 `memory` 字段；新增 `GET /memory`（只读，供前端与评测） |
| `prompt_registry.py` | 改 | `agent_system` 加第 17 条规则（**新增 v2 版本 + 默认切到 v2**，v1 保留原样以便对照） |
| `test_learning_memory.py` | **新增** | §8.2 全部用例 |
| `test_database_migrations.py` | 改 | 追加 V7 相关断言（新库两表、旧库升级、幂等） |
| `docs/STAGE2-LEARNING-MEMORY.md` | **新增** | 实现后的交付说明（对齐 `STAGE1-FEEDBACK-PLAN-LINK.md` 的写法） |

**明确不改**：`agent_loop.py`（预算/超时逻辑一行不动）、`langgraph_plan_workflow.py`（本阶段不重构 LangGraph，只在其 research 消息里拼短期上下文——若你希望连这处也不碰，可退化为「仅原生路径注入」）、`rag_service.py` / `embedding_service.py` / `vector_cache.py`（不碰知识库与向量设施）、`plans` 表结构、`confirmation_service.py`。

---

## 10. 开发任务拆分与优先级

| 序 | 任务 | 优先级 | 依赖 | 预估 | 验收 |
| --- | --- | --- | --- | --- | --- |
| 1 | V7 迁移 + 8 个 DB 函数 + 迁移测试（M1–M5） | P0 | — | 0.5 天 | `test_database_migrations.py` 全绿且新增断言通过 |
| 2 | `normalize_subject` + `render_content` + N 组测试 | P0 | 1 | 0.3 天 | 纯函数单测全绿 |
| 3 | `update_from_feedback`（含 4 类规则、confidence 公式）+ S/D/U/C/E 组测试 | P0 | 1,2 | 1.5 天 | 40 个用例中 S/D/U/C/E 全绿 |
| 4 | `retrieve()` + R 组测试 + P@3/R@3 标注集 | P0 | 3 | 0.5 天 | 指标达标 |
| 5 | `memory_context.py` + 注入 `agent_plan_draft` | P1 | 3 | 0.4 天 | 注入文本 ≤1200 字符；既有 `test_agent_plan_draft.py` 不回归 |
| 6 | Tool 接入（schema/dispatcher/tools 三处）+ T/X 组测试 | P1 | 4 | 0.5 天 | `test_agent_schema.py`、`test_agent_dispatcher.py` 全绿 |
| 7 | `/feedback` 触发 + `GET /memory` + G 组回归 | P1 | 3 | 0.3 天 | 全量回归；`/feedback` 延迟增量达标 |
| 8 | `prompt_registry.agent_system` v2 + grounding 扩展 | P2 | 6 | 0.3 天 | 违规用例能被判出 |
| 9 | `docs/STAGE2-LEARNING-MEMORY.md` + 交付说明 | P2 | 全部 | 0.3 天 | 文档含真实测试输出 |

合计约 **4.6 人日**。P0 = 不做就没有记忆能力；P1 = 接进 Agent 与 API；P2 = 提示词与文档收尾。

建议**一个 PR 分两次推送**：P0（任务 1–4）先审一次（数据模型与规则最容易返工），P1/P2 再补；这样审查粒度与第一阶段一致。

---

## 11. 技术风险与替代方案

| 风险 | 影响 | 缓解 / 替代 |
| --- | --- | --- |
| **知识点只能来自任务名** | `subject_key` 归并质量取决于计划里的任务名写法（「条件概率练习」vs「做条件概率题」会被当成两个主题） | ①归一化只做确定性变换，宁可少合并不错合并；②`subject_label` 保留原文，让用户/评测能看出是否该合并；③后续可选「合并同义主题」的人工确认 UI（不在本阶段） |
| **confidence 公式主观** | 审稿人可能质疑权重拍脑袋 | ①公式与常量全部写在文档与代码里，可复算；②`sensitivity` 小实验：改半衰期 30/60/90 天，看检索命中率是否稳定（放在评测里，不写进产品） |
| **自动裁决冲突会出错** | 用户攻克后被误判为 `weakened`，或反之 | ①保留全部证据与事件，不删除；②记忆只作为「依据」提供给模型，不直接驱动计划；③`resolved` 后允许 `reopened`，双向可恢复 |
| **`/feedback` 同步更新记忆** | 写库路径变长，反馈保存可能变慢或部分失败 | 更新失败不回滚反馈；延迟指标纳入验收（≤50 ms）；必要时降级为「下次读记忆时按需重建」 |
| **新增 Tool 挤占 Tool 预算** | `MAX_TOOL_CALLS=4` 下，模型可能为了查记忆而少查知识库 | ①`agent_system` 明确「只做通用概念解释时不要调用」；②评测集里加「该不该调记忆」的判断用例；③不提高预算上限 |
| **记忆与状态评估边界模糊** | 第三阶段容易把「掌握程度」塞进记忆表 | 本阶段表结构**不含**任何 level/mastery/score 字段；第三阶段若需要，新建 `learning_states` 表，不扩 `learning_memories` |
| **复用 RAG 做记忆检索的诱惑** | 引入向量库/新索引，复杂度与部署成本上升 | 明确拒绝：记忆规模 ≤数百行，SQL 足够；如后续要做语义检索实验，复用现有 `embedding_service` + 独立缓存命名空间，作为**实验路径**（`RAG_RETRIEVAL_MODE` 式开关），不默认启用 |
| **短期上下文注入改变既有行为** | `test_agent_plan_draft.py` 里对 prompt 内容有断言（如「统计结果」必须出现） | 注入位置放在研究阶段 user message 前，不动 `DRAFT_SYSTEM_PROMPT`；注入内容为空时**不改变**任何字节，保证既有用例二进制等价 |
| **时区/时间口径** | 现有全部是本地无时区字符串，跨时区或夏令时会乱 | 沿用现状（`database.py:285` 已声明不重构时间系统）；衰减计算统一用 `datetime.now()` 差值，不使用 UTC 混算 |

### 需要你拍板的 3 件事

1. **【待定 1】** `/ask` 是否也注入短期上下文？（建议：不注入）
2. **【待定 2】** 是否加 `memory_events` 审计表？（建议：加，只写不读）
3. **【待定 3】** 记忆更新失败时 `/feedback` 是否要报错？（建议：不报错，附 `memory.ok=false`）

---

## 附：与「禁止事项」的对照自查

- 未修改任何代码（本文件是唯一新增物）✔
- 未涉及学习状态评估（无 mastery / level / 状态机推断）✔
- 未重构 LangGraph（只在 research 消息里拼一段文本，且标注为可退化项）✔
- 未更换数据库（SQLite + 现有迁移机制）✔
- 未重写 Agent Loop（`agent_loop.py` 零改动）✔
- 未引入不必要的复杂技术（无向量库、无消息队列、无 ORM、无后台任务框架）✔
- 未把模型推测当事实（记忆文本 100% 模板化，写入只看枚举与数值列）✔
- 未把 RAG 教材知识当个人记忆（分表、分 Tool、分来源语义）✔
