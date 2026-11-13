# 毕业设计实验脚手架

本目录把 `docs/GRADUATION-EXPERIMENT-DESIGN.md` 的 S1–S7 样本固化成可复现的实验输入，
并复现设计文档里的三组核验：**记忆层 Gold Standard（20 条）**、**A/B/B0/C/D 五组 draft prompt**
与 **离线可复现的运行层**（`run_experiment.py`）。

分两层，边界要说清楚：

- **离线层**（`seed_fixture.py` / `check_memory.py` / `build_prompts.py`）：只做模拟数据、
  记忆重建、Prompt 构建，**不调用任何模型**。
- **执行层**（`run_experiment.py`）：把同一批 Prompt 真正发给模型，逐次追加写 `results.jsonl`。
  它**只在用户显式运行时**调用对话模型；**不调用 Embedding、不做知识库检索**（见下"不考察 RAG"）。

## 目录结构

```text
experiments/graduation/
    README.md            本文件
    config.json          固定 REF_DATE、样本清单、五组、记忆上限、采样参数、知识库占位
    samples/
        S1.json … S7.json  结构化样本：场景/历史反馈/计划记录/当前请求/Gold Standard/模拟标识
    seed_fixture.py      建独立临时库 + 写计划与反馈 + 调 memory_service.rebuild_all()
    check_memory.py      读实跑记忆，与 Gold Standard 逐项比对，输出 Markdown 报告
    build_prompts.py     调 agent_plan_draft._build_draft_user_content() 生成 A/B/B0/C/D 五组 Prompt
    run_experiment.py    执行层：隔离库 + 逐次调模型 + 追加写 results.jsonl（只在显式运行时调模型）
```

## 前置条件

- 仓库根目录的 `.venv`（Windows）：`F:\somethingProject\dsbot\.venv\Scripts\python.exe`
- 不需要 Ollama、不需要任何 API Key：整条链路没有任何模型调用点
- 所有脚本都以 **仓库根目录** 为工作目录运行

## 运行方式

```powershell
cd F:\somethingProject\dsbot
$env:PYTHONIOENCODING='utf-8'

# 1. 建库 + 重建记忆（七个样本各一个独立库）
.\.venv\Scripts\python.exe experiments\graduation\seed_fixture.py

# 2. 记忆层核验（20 条 Gold Standard）
.\.venv\Scripts\python.exe experiments\graduation\check_memory.py

# 3. Prompt 构建 + 组间等价性核验（A/B/B0/C/D）
.\.venv\Scripts\python.exe experiments\graduation\build_prompts.py --reuse

# 4. 执行层：真正调模型（需要已配置的对话模型；不加 --sample 会跑全部样本）
.\.venv\Scripts\python.exe experiments\graduation\run_experiment.py --sample S5 --group B0 --repeats 1
```

`--reuse` 表示复用已建好的库（不重跑 rebuild）；不加则每次重新建库并重建记忆。
`--reuse` 与 `--keep` 走同一套完整性校验：`check_memory.py --reuse` 与 `build_prompts.py --reuse`
在读取库之前都先调用 `seed_fixture.inspect_sample_db()`，残缺库直接失败，不会带着缺表的库
继续做 Gold Standard 核验或生成 Prompt。
`seed_fixture.py --keep` 的语义是**只复用、不写入**：库已存在且**完整校验通过**时直接返回，不插入计划/反馈、
也不重复重建；库不存在时照常创建。完整性校验逐项比对 Gold Standard 而不是只数行数——
7 张业务表必须齐全（含 `memory_apply_cursor`）、反馈与计划条数一致、
`learning_memories` 的 `(memory_type, subject_key)` 集合不多不少不重复、
每条记忆的 `supports`/`contradicts` 证据行数逐条相符、
每条证据的 `memory_id`/`feedback_id`/`plan_id` 必须指向真实记录（表上没有实际外键，
所以由脚手架逐行查孤立行）、`relation` 只允许 `supports`/`contradicts`、
`memory_pending_evidence` 为 0、
游标表有且仅有 `id=1` 一行且 `last_applied_feedback_id` 等于反馈高水位。
任何一项不符直接报错（报错写明异常类型与条数），不会静默写到别的库。

三个脚本都支持 `--sample S3` 只跑单个样本，`--config <path>` 换配置文件。

自测（都离线、不调模型）：

- `.\.venv\Scripts\python.exe test_graduation_experiments.py`（33 项，覆盖七个样本的五组构建、
  A/C 空反馈摘要与 B/D 真实摘要、B0 槽位、核验的负向用例、`--keep` 的七种残缺库 + 五种残缺证据
  拒绝路径、两个 `--reuse` 入口的负向用例与正常复用）
- `.\.venv\Scripts\python.exe test_graduation_experiment_runner.py`（执行层：采样参数进入 mock
  payload、失败记录、修正上限、JSONL 追加、prompt_sha256、token 缺失、数据库隔离、CLI 边界）

## 执行层（run_experiment.py）

```powershell
# 只跑 1 份 B0 冒烟（S5，1 次），结果落到指定运行目录
.\.venv\Scripts\python.exe experiments\graduation\run_experiment.py `
    --sample S5 --group B0 --repeats 1 --run-dir %TEMP%\graduation_runs\smoke
```

参数：`--sample`（可重复，默认全部样本）、`--group`（A/B/B0/C/D，可重复，默认全部）、
`--repeats`（1–50，默认 1）、`--run-dir`（默认 `experiments/graduation/runs/<ref>-<UTC>`）、
`--reuse`（复用已建好且通过完整性校验的样本库，否则 `%TEMP%\graduation_experiments\...` 下的库）、
`--config`。

样本调度按 `(样本, 组)` 任务对生成，不是「一个样本列表套所有组」：

| 命令 | A/B/C/D | B0 |
| --- | --- | --- |
| 默认（不给 `--group`） | S1–S7 | 仅 S5、S7 |
| `--group B0` | 不跑 | 仅 S5、S7 |
| `--group A --group B0` | A：S1–S7 | 仅 S5、S7 |
| `--sample S5 --group B0` | 不跑 | S5 |
| `--sample S1 --group B0` | 不跑 | S1（显式覆盖，记录里 `b0_sample_override=true`） |

显式 `--sample` 保留「按你指定的样本执行」的语义，可以超出 B0 的默认范围；这种覆盖会写进每条
结果的 `sample_selection`（`explicit` / `default`）与 `b0_sample_override` 字段，统计时能识别出来。

运行前的三道闸门：

1. **数据库隔离**：先建运行目录，把 `FEEDBACK_DB_PATH` 指向运行目录内的
   `_experiment_isolation.db`，**之后**才 import `database` / `memory_service`，然后断言
   `database.DATABASE_PATH` 既不等于仓库根目录的真实 `feedback.db`、又确实位于本次运行目录内，
   否则 `RuntimeError` 直接退出（fail-closed）。随后用
   `seed_fixture.register_project_modules()` 把已隔离的模块登记给脚手架，避免二次 import 冲突。
2. **用量回收器**：`llm_client.set_usage_recorder()` 装的是本脚本自己的 recorder，只把记录
   追加进内存列表，**不落数据库**（不产生 `model_usage` 行）。
3. **重复实验身份保护**：运行前先读运行目录里已有的 `results.jsonl`，把本次计划生成的
   `(experiment_id, sample_id, group, repeat_index)` 与已有记录逐一比对。只要有一条重复，
   就在建目录、隔离数据库、发任何模型请求之前退出（返回码 `4`），提示改用新的 `--run-dir`；
   已有文件一个字节都不改，也不会追加半截结果。结果文件是坏 JSON 或缺身份字段时同样
   fail-closed（返回码 `4`），不猜测、不跳过。
4. **不覆盖**：结果按 JSONL 追加写运行目录下的 `results.jsonl`（`a` 模式 + `flush` + `fsync`），
   通过身份检查后的新记录只会追加到文件末尾；Prompt 快照
   （`prompts/<样本>/<组>.txt`）若已存在且内容不同则报错拒绝覆盖。

每条记录至少包含：`experiment_id`、`sample_id`、`group`、`repeat_index`、`provider`、`model`、
`temperature`、`top_p`、`max_tokens`（优先取**本次最后一次实际请求**的值，无请求时记录意图值）、
`prompt_sha256`、`prompt_ref`/`prompt_chars`、`raw_output`（最后一次起草尝试的模型原始输出）、
`raw_outputs`（本次全部起草尝试的原始输出列表，按调用顺序；没有起草时是 `[]`）、
`sample_selection`（`explicit` / `default`）、`b0_sample_override`（显式 `--sample` 让 B0 跑出
S5/S7 时为 `true`）、`final_output`
（`{answer, plan}` 或 `null`）、`request_count`、`requests`（逐次请求的用量）、`corrections_used`、
`token_usage`（缺失记为 `{"available": false, "status": "unavailable"}`，不填 0 冒充）、
`elapsed_seconds`、`generated_at`、`success`、`error_type`、`error_message`、`stopped_reason`。

生成走的是产品自己的链路 `agent_plan_draft.run_plan_draft_from_inputs()`——跳过研究阶段
（`tool_calls = 0`、`loop_turns = 0`、`research_final_answer = None`），但仍复用产品的 JSON 解析、
`plan_service.normalize_plan`、`agent_tools.validate_plan_for_save`、`check_grounding` 与
`MAX_CORRECTIONS = 2`。`check_grounding` 需要的证据由合成 evidence steps 提供：注入反馈的组
才有 `get_recent_feedback`，C/D 才有 `get_learning_memory`，知识库一律不合成。

## 产物位置

全部写在系统临时目录，不入库：

```text
%TEMP%\graduation_experiments\<yyyyMMddTHHmmss>\
    S1.db … S7.db          每个样本一个独立 SQLite 库
    memory_check.md        记忆层核验报告
    prompts/<S>/A.txt … D.txt   每一组的 system + user 全文（B0 写成 B0.txt）
    prompts/<S>/diff.md          A→B、A→B0、B→B0、A→C、B→D 的 unified diff
    prompts/<S>/manifest.json    每组 SHA256、字符数、注入记忆清单
    prompts/hashes.json         七个样本 × 五组的 SHA256 汇总
    prompts/report.md           组间等价性核验结果
```

执行层另写自己的运行目录（默认 `experiments/graduation/runs/<ref>-<UTC>`，可 `--run-dir` 指定）：

```text
<run-dir>\
    _experiment_isolation.db  本次运行的隔离库（绝不指向仓库根的真实 feedback.db）
    results.jsonl             逐次生成结果，只追加、不覆盖
    prompts/<S>/<组>.txt      本次实际发送的 system + user 快照
```

`runs/` 已在 `.gitignore` 里，实验结果不进仓库。

## 关键约束（都由脚本强制）

### 1. 真实库不可达

`seed_fixture._prepare_isolation()` 在 import `database` **之前**把 `FEEDBACK_DB_PATH` 指向临时目录里的占位文件，
import 之后再断言 `database.DATABASE_PATH` 落在该临时目录内，否则直接
`RuntimeError("数据库路径未被隔离，拒绝继续：…")`（fail-closed）。
真实 `feedback.db` 不会被创建、写入或读取。

### 2. 时间是固定的

`config.json` 的 `ref_date` 是唯一时间基准，样本里的 `offset_days` 都是相对它的偏移。
记忆重建走 `memory_service.rebuild_all(now=ref_date)`（该参数只接受 ISO 字符串）。
反馈查询的窗口（`get_feedback_since`）用的是 `datetime.now()`，所以 `build_prompts.fixed_clock()`
在调用期间临时替换 `database.datetime`——`agent_tools` 与 `memory_context` 都是
`from database import get_feedback_since`，绑的是同一个函数对象，读的是同一个模块全局，
一次替换对两条路径同时生效；**不改任何产品文件**。

### 3. 组间差异只来自设计允许的槽位

| 组 | 近期反馈 | 统计摘要 / 难度规则 | 短期上下文 | 长期记忆 |
|---|---|---|---|---|
| A | 无 | 无 | 无 | 无 |
| B | 有（7 天窗口） | 有 | 有 | 无 |
| B0 | 有（原始反馈行） | 无（`summary={}`、`difficult=False`） | 无 | 无 |
| C | 无 | 无 | 无 | 有（active 前 5 条） |
| D | 有 | 有 | 有 | 有 |

槽位的唯一定义来源是 `GROUP_SLOTS` 与 `group_inputs()`，`build_prompts.py` 与
`run_experiment.py` 都调它，不存在第二份槽位逻辑。B0 是**探索组**（只在 S5、S7 上生成、
仅作描述性旁证），A/B/C/D 四组的 Prompt 在本版中**逐字节不变**。

**本实验不考察 RAG**：五组 `knowledge_results` 一律为 `[]`、`search_failed` 固定 `false`，
运行期不调用 Embedding、不执行向量检索。知识库当前仅 238 字符，`MIN_SCORE=0.5` 是在本地小模型上
标定的，尚未在云端对话模型上重新校准；把它固定为空是为了不把"这次检索是否命中"混进记忆的主效应。
`build_prompts` 用若干断言把这件事钉死，其中包括：

- `strip_memory(C) == A`、`strip_memory(D) == B`（记忆是唯一增量）
- `base_segment(A) == base_segment(C)`、`base_segment(B) == base_segment(D)`（反馈/难度规则一致）
- 五组的「真实知识库检索状态」段逐字相同（同为空）
- B0 含原始反馈行、但不含统计摘要（无 `"total_count"`）与难度硬规则，也不含短期/记忆区块
- 记忆区块里的条目集合恰好等于库里 `status='active'` 按 strength 倒序的前 N 条
- 记忆区块在 D 里只出现一次

Prompt 一律来自产品函数 `agent_plan_draft._build_draft_user_content()`，
系统提示词来自 `prompt_registry`，不在这里手写任何文案。

### 4. 注入只走 `status="active"`

`agent_tools.get_learning_memory()` 里 `status="active"` 是写死的，
weakened / resolved 记忆永远进不了 Prompt。所以 D 组的注入条数并不等于记忆总数：

| 样本 | 记忆条数 | 实际注入 |
|---|---|---|
| S1 | 3 | 3 |
| S2 | 2 | 2 |
| S3 | 3 | 1 |
| S4 | 3 | 2 |
| S5 | 5 | 5 |
| S6 | 3 | 1 |
| S7 | 1 | 1 |

S3、S6 的冲突/改善类记忆（resolved / weakened）因此不出现在任何 Prompt 里——这是产品现状，
不是脚手架缺陷；实验分析必须按 `memory_injected` 实际注入集合解释 H4。

## 样本与 Gold Standard

`check_memory.py` 对每条记忆逐项核对 **status / strength / supports / contradicts / unit / 前缀 / injected / content**，
并单独核对 `must_not_exist`（不该出现的记忆确实没出现）。当前 7 个样本共 20 条记忆，全部通过。

核验是 **fail-closed** 的，任何一条不一致都计入 `failure_count` 并让脚本以非零码退出：

- 记忆集合必须与 Gold Standard **完全一致**——少一条、多一条都算失败（多余的在报告里单独列表）；
- 注入集合必须与 Gold Standard 的预期注入集合一致（双向差集都查）；
- 同一 `(memory_type, subject_key)` 出现重复行算失败；
- `pendings != 0`（还有未处理证据）算失败；
- 重建本身 `rebuild.ok != True` 算失败，不把部分结果当通过；
- 报告逐样本列「失败原因」，不留"存在不一致"这种模糊结论。

单位口径来自 `memory_service._stable_unit()`：difficulty 取 `supports`，
task_performance 与 time_pattern 取 `fields["samples"]`，task_topic 取 `fields["appearances"]`。

## 本阶段不做的事

- 敏感性分析（C-subj / D-subj）、统计检验、双人评分流程：都还没实现
- 正式 165 份生成、Embedding、知识库检索：一件都没跑（执行层只做过空 Key 的失败路径冒烟）
- 仓库没有配置 CI，测试结论只能来自本机实跑
