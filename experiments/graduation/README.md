# 毕业设计实验脚手架（第一阶段）

本目录把 `docs/GRADUATION-EXPERIMENT-DESIGN.md` 的 S1–S7 样本固化成可复现的实验输入，
并复现设计文档里的两组核验：**记忆层 Gold Standard（20 条）** 与 **A/B/C/D 四组 draft prompt**。

这一阶段只做三件事：模拟数据、记忆重建、Prompt 构建。
**不调用对话模型、不调用 Embedding、不执行 165 次生成、不写正式产品逻辑、不产出实验结果。**

## 目录结构

```text
experiments/graduation/
    README.md            本文件
    config.json          固定 REF_DATE、样本清单、四组、记忆上限、知识库占位
    samples/
        S1.json … S7.json  结构化样本：场景/历史反馈/计划记录/当前请求/Gold Standard/模拟标识
    seed_fixture.py      建独立临时库 + 写计划与反馈 + 调 memory_service.rebuild_all()
    check_memory.py      读实跑记忆，与 Gold Standard 逐项比对，输出 Markdown 报告
    build_prompts.py     调 agent_plan_draft._build_draft_user_content() 生成 A/B/C/D 四组 Prompt
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

# 3. Prompt 构建 + 组间等价性核验
.\.venv\Scripts\python.exe experiments\graduation\build_prompts.py --reuse
```

`--reuse` 表示复用已建好的库（不重跑 rebuild）；不加则每次重新建库并重建记忆。
三个脚本都支持 `--sample S3` 只跑单个样本，`--config <path>` 换配置文件。

自测：`.\.venv\Scripts\python.exe test_graduation_experiments.py`

## 产物位置

全部写在系统临时目录，不入库：

```text
%TEMP%\graduation_experiments\<yyyyMMddTHHmmss>\
    S1.db … S7.db          每个样本一个独立 SQLite 库
    memory_check.md        记忆层核验报告
    prompts/<S>/A.txt … D.txt   每一组的 system + user 全文
    prompts/<S>/diff.md          A→B、A→C、B→D 的 unified diff
    prompts/<S>/manifest.json    每组 SHA256、字符数、注入记忆清单
    prompts/hashes.json         七个样本 × 四组的 SHA256 汇总
    prompts/report.md           组间等价性核验结果
```

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

### 3. 四组差异只来自设计允许的槽位

| 组 | 近期反馈 | 短期上下文 | 长期记忆 |
|---|---|---|---|
| A | 无 | 无 | 无 |
| B | 有（7 天窗口） | 有 | 无 |
| C | 无 | 无 | 有（active 前 5 条） |
| D | 有 | 有 | 有 |

知识库结果四组共用同一份（当前固定为空列表），`search_failed` 固定 `false`。
`build_prompts` 用 8 类断言把这件事钉死，其中包括：

- `strip_memory(C) == A`、`strip_memory(D) == B`（记忆是唯一增量）
- `base_segment(A) == base_segment(C)`、`base_segment(B) == base_segment(D)`（反馈/难度规则一致）
- 四组的「真实知识库检索状态」段逐字相同
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

单位口径来自 `memory_service._stable_unit()`：difficulty 取 `supports`，
task_performance 与 time_pattern 取 `fields["samples"]`，task_topic 取 `fields["appearances"]`。

## 本阶段不做的事

- B0 组、敏感性分析、统计检验、评分流程：都还没实现
- 任何模型生成、Embedding、知识库检索：一件都没跑
- 仓库没有配置 CI，测试结论只能来自本机实跑
