# 记忆 Gold Standard 实跑核对报告（第三轮校准）

- 报告日期：2026-11-12
- 对应设计文档：[`docs/GRADUATION-EXPERIMENT-DESIGN.md`](GRADUATION-EXPERIMENT-DESIGN.md) v3 的 §3.2
- 参考基准日：`REF_DATE = 2026-11-12T21:00:00`
- 代码基线：`main` = `53a18b3985ed9e964ca197e22b167fbb7225d878`（本轮未改任何业务代码）
- 性质：**本机实跑核对记录**（不是 CI 结果，也不是模型生成结果）；只调用 `memory_service` 的
  rebuild/retrieve，不产生任何计划文本。

---

## 1. 目的

设计文档 §3.2 原本给的是"按公式估算的预期强度**区间**"。区间无法验收：只要实现落在区间里就算
通过，既掩盖实现偏差，也无法在写论文时给出确定值。本轮的目的一是两个：

1. 用**真实 `memory_service` + 独立临时 SQLite 库**把 7 个样本的记忆完整重建一遍，逐条核对
   `status / strength / supports / contradicts / unit / 是否达 REQUIRED / 是否达 STABLE / 有无
   "初步观察："前缀 / evidence_count / contradict_count`；
2. 把 §3.2 的区间替换成**实测精确值**，并把"哪些记忆真的会被注入"也写进标准（见 §5）。

---

## 2. 复现方法（隔离，不碰真实库）

- 每个样本一个独立 SQLite 文件：`%TEMP%\gold_check_20261112\S1.db … S7.db`（一次性脚本
  `%TEMP%\gold_check.py`，**未入库**，因为它是校验工具而非产品代码）。
- 每个库的流程：设置 `database.DATABASE_PATH` → `database.init_db()` → 按 §3 的种子表
  `insert_plan()` / `insert_feedback()` → `memory_service.rebuild_all(now=REF_DATE)`（ISO 字符串）。
- 关键实现细节：`rebuild_all(now=...)` 必须传**字符串**，传 `datetime` 会直接抛
  `ValueError("时间戳必须是非空字符串")`（`memory_service.py:424` 的 `_parse_ts`）。
- 运行命令（PowerShell，UTF-8 输出）：

  ```powershell
  $env:PYTHONIOENCODING='utf-8'
  F:\somethingProject\dsbot\.venv\Scripts\python.exe %TEMP%\gold_check.py
  ```

  退出码 0；七个样本全部 `ok=true`，`pendings` 全为 0，`events` 与预期状态迁移一致。
- 真实库未受影响的证据（同一时点核查）：`F:\somethingProject\dsbot\feedback.db` 仍是
  `106496` 字节、mtime `2026-11-12 11:56:32`（早于本次运行），`feedbacks`/`plans` 均为 **0 行**。

---

## 3. 逐条核对（20 条记忆，全部通过）

列含义：`supports`=`evidence_count`；`contradicts`=`contradict_count`；`unit` 为该类型的稳定单位
（difficulty→supports；task_performance/time_pattern→`samples`；task_topic→`appearances`）；
`REQ/STABLE` 为该类型阈值（`REQUIRED={difficulty 2, task_performance 3, time_pattern 4, task_topic 3}`、
`STABLE={4, 6, 8, 6}`）；`前缀` 指 `content` 是否以"初步观察："开头；`注入` 指 `status="active"`
（只有 active 会被 `get_learning_memory()` 注入）。

| # | 样本 | 类型 | subject_key | status | strength（实测） | supports | contradicts | unit | 达 REQ | 达 STABLE | 前缀 | 注入 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | S1 | difficulty | 一元二次方程 | active | 0.9 | 5 | 0 | 5 | 是 | 是 | 无 | 是 |
| 2 | S1 | task_performance | 一元二次方程 | active | 0.6 | 5 | 0 | 5 | 是 | 否 | **有** | 是 |
| 3 | S1 | task_topic | 一元二次方程 | active | 0.6 | 5 | 0 | 5 | 是 | 否 | **有** | 是 |
| 4 | S2 | task_performance | 英语定语从句 | active | 0.6 | 5 | 1 | 6 | 是 | 是 | 无 | 是 |
| 5 | S2 | task_topic | 英语定语从句 | active | 0.9 | 6 | 0 | 6 | 是 | 是 | 无 | 是 |
| 6 | S3 | difficulty | python列表推导式 | **resolved** | 0.114551 | 3 | 3 | 3 | 是 | 否 | 有 | **否** |
| 7 | S3 | task_performance | python列表推导式 | **resolved** | 0.152735 | 3 | 3 | 6 | 是 | 是 | 无 | **否** |
| 8 | S3 | task_topic | python列表推导式 | active | 0.9 | 6 | 0 | 6 | 是 | 是 | 无 | 是 |
| 9 | S4 | time_pattern | medium | active | 0.6 | 5 | 0 | 5 | 是 | 否 | 有 | 是 |
| 10 | S4 | task_performance | 英语阅读 | **weakened** | 0.273517 | 3 | 2 | 5 | 是 | 否 | 有 | **否** |
| 11 | S4 | task_topic | 英语阅读 | active | 0.6 | 5 | 0 | 5 | 是 | 否 | 有 | 是 |
| 12 | S5 | difficulty | 线性代数 | active | 0.6 | 3 | 0 | 3 | 是 | 否 | 有 | 是 |
| 13 | S5 | task_performance | 线性代数 | active | 0.5 | 3 | 0 | 3 | 是 | 否 | 有 | 是 |
| 14 | S5 | task_topic | 线性代数 | active | 0.5 | 3 | 0 | 3 | 是 | 否 | 有 | 是 |
| 15 | S5 | task_performance | 概率论 | active | 0.5 | 4 | 1 | 5 | 是 | 否 | 有 | 是 |
| 16 | S5 | task_topic | 概率论 | active | 0.6 | 5 | 0 | 5 | 是 | 否 | 有 | 是 |
| 17 | S6 | difficulty | 高等数学极限 | **weakened** | 0.434181 | 5 | 1 | 5 | 是 | 是 | 无 | **否** |
| 18 | S6 | task_performance | 高等数学极限 | **weakened** | 0.434181 | 5 | 1 | 6 | 是 | 是 | 无 | **否** |
| 19 | S6 | task_topic | 高等数学极限 | active | 0.9 | 6 | 0 | 6 | 是 | 是 | 无 | 是 |
| 20 | S7 | difficulty | 数据结构 | active | 0.5 | 2 | 0 | 2 | 是 | 否 | 有 | 是 |

合计：**20 条记忆**，其中 **15 条注入**、**5 条不注入**（S3 两条 `resolved`、S4 一条 `weakened`、
S6 两条 `weakened`）。

**不应生成的记忆，实测都不存在**：S2 无 `difficulty`（原因是时间）；S5 无 `difficulty 概率论`；
S7 无 `task_topic`（appearances 2 < 3）、无 `task_performance`（samples 2 < REQUIRED 3）。

---

## 4. 强度手算复现（公式 → 实测值逐条对上）

`compute_strength`（`memory_service.py:724`）：

```
base    = min(1.0, unit / STABLE[type])
recency = 0.5 ** (age_days / 30)          # age = 最后写入证据的 as_of − last_supported_at
penalty = 0.6 ** contradict_count
raw     = max(0.05, min(0.99, base * recency * penalty))
cap     = 0.6 if unit < STABLE[type] else 0.9
strength= round(min(raw, cap), 6)
```

| # | 样本/类型 | 代入 | 结果 | 实测 |
| --- | --- | --- | --- | --- |
| 1 | S1 difficulty | base=5/4→1.0，age 0→1.0，pen 1 | min(0.99,1.0)=0.99，cap 0.9 | 0.9 ✓ |
| 2 | S1 task_performance | base=5/6=0.8333，cap 0.6 | 0.8333→0.6 | 0.6 ✓ |
| 3 | S1 task_topic | base=5/6=0.8333，cap 0.6 | 0.8333→0.6 | 0.6 ✓ |
| 4 | S2 task_performance | base=6/6=1.0，pen=0.6¹=0.6，age 0 | 0.6 | 0.6 ✓ |
| 5 | S2 task_topic | base=6/6=1.0，pen 1 | 0.99→cap 0.9 | 0.9 ✓ |
| 6 | S3 difficulty | base=3/4=0.75，pen=0.6³=0.216，age 15→0.7071 | 0.75×0.216×0.7071=0.114551 | 0.114551 ✓ |
| 7 | S3 task_performance | base=6/6=1.0，pen 0.216，age 15→0.7071 | 0.152735 | 0.152735 ✓ |
| 8 | S3 task_topic | base=1.0，pen 1，age 0 | 0.99→cap 0.9 | 0.9 ✓ |
| 9 | S4 time_pattern | base=5/8=0.625，pen 1，age 0 | 0.625→cap 0.6 | 0.6 ✓ |
| 10 | S4 task_performance | base=5/6=0.8333，pen=0.6²=0.36，age 4→0.5^(4/30)=0.9117 | 0.273517 | 0.273517 ✓ |
| 11 | S4 task_topic | base=5/6=0.8333，cap 0.6 | 0.6 | 0.6 ✓ |
| 12 | S5 difficulty 线代 | base=3/4=0.75，cap 0.6 | 0.6 | 0.6 ✓ |
| 13 | S5 task_performance 线代 | base=3/6=0.5，age 0，pen 1 | 0.5 | 0.5 ✓ |
| 14 | S5 task_topic 线代 | base=3/6=0.5 | 0.5 | 0.5 ✓ |
| 15 | S5 task_performance 概率论 | base=5/6=0.8333，pen=0.6¹=0.6 | 0.5 | 0.5 ✓ |
| 16 | S5 task_topic 概率论 | base=5/6=0.8333，cap 0.6 | 0.6 | 0.6 ✓ |
| 17 | S6 difficulty | base=5/4→1.0，pen 0.6，age 14→0.5^(14/30)=0.7236 | 0.434181 | 0.434181 ✓ |
| 18 | S6 task_performance | base=6/6=1.0，pen 0.6，age 14→0.7236 | 0.434181 | 0.434181 ✓ |
| 19 | S6 task_topic | base=1.0，pen 1，age 0 | 0.9 | 0.9 ✓ |
| 20 | S7 difficulty | base=2/4=0.5，age 0，pen 1 | 0.5（未触及 0.6 封顶） | 0.5 ✓ |

**结论：20/20 与公式手算完全一致**，不存在"实测值无法解释"的项。

关于 `age_days`：strength 只在**写入证据**时按该条反馈的 `as_of` 重算，`_materialize_stale` 不会
重算，所以 `age` 恒等于"最后一条证据 − 最后一次 supports"的天数。这也是 S3 的 `resolved` 强度会
停在 `0.114551/0.152735` 而不随日期继续衰减的原因（10-30 首次 weakened，11-09 resolved，此后无新
证据）。论文里必须这样写，否则"为什么 resolved 的强度不继续掉"会被追问。

---

## 5. 设计层面的关键修正（已同步进设计文档）

1. **`task_performance`/`task_topic` 在 5 次证据时不能超过 0.6，且都带"初步观察："前缀**
   （S1 原稿写 0.70–0.80 是错的）。原因是 `STABLE` 分别为 6 和 6，`_stable_unit < STABLE` 时既封顶
   0.6、`render_content()` 也加前缀（`memory_service.py:240`、`:255`）。
2. **S2 的 `task_performance` 是 0.6 而不是 0.5–0.65 区间**：`samples=6` 恰好达 `STABLE`，不加前缀，
   但 1 次 contradicts（-5 的 completed）把强度乘 0.6。
3. **S3 是 `resolved`，不是 `weakened`**（0.114551 / 0.152735）；0.05–0.20 / 0.15–0.30 的旧区间是把
   状态看错了。
4. **S5 线性代数 3 条全部 ≤0.6**：`task_performance`=0.5、`task_topic`=0.5、`difficulty`=0.6（封顶），
   不是 0.40–0.55 一个区间能覆盖的。
5. **只有 `status="active"` 会被注入**：`agent_tools.get_learning_memory()` 内部固定
   `retrieve(status="active")`。所以 S3 实际只注入 1 条（`task_topic`）、S4 注入 2 条、S6 注入 1 条。
   这不是实验设置，而是 v16 的既有行为。直接影响：
   - S3、S6 上"D vs B"的差异预期很小，**不能**再把"冲突/改善"当作记忆的优势场景（H4 已据此改写）；
   - "模型看到被削弱的记忆后会不会降低断言强度"在生成层**测不到**，只能由 M4 在记忆层验证
     （已写进 §9 局限性第 16 条）；
   - 报告必须把"记忆生成集 `P_s`"与"注入集 `D_s`"分开列（已写进 §4.1）。

---

## 6. 与设计文档的同步点

| 文档位置 | 改动 |
| --- | --- |
| 文档头 | 版本 v2 → v3，注明本轮按实跑校准 Gold Standard |
| §1.2 | H4 改写为"降级正确 + 未注入 + D 不变差"；H5 补 `dz` 口径；新增"注入范围决定哪些假设可检测"说明 |
| §2.2 | 新增"只注入 `status="active"`"条目，写明各样本注入条数与"生成集/注入集要分开" |
| §2.4 | 补齐每位评分者口径：84+23=107 份/人、214 份次、408 个 1–5 评分点/人、`S3_flag` 24 份、`S4_verdict` 12 份 |
| §3.2 | 7 张预期记忆表全部改为实测精确值 + 新增"注入"列；S3/S4/S6 的状态判断与评测陷阱按注入集改写 |
| §3.3 | S3、S6 的"主要对比"补 M4 |
| §4.1 | 区分 `P_s`（全部非 archived）与 `D_s`（active 注入集）；M4 增加 `injected` 与"是否被正确排除" |
| §4.2 | S1 标签按"当前状态"重述；S4 冲突处理拆成记忆层/生成层 |
| §4.5 | H4 的指标映射同步改写 |
| §5.3 / §6 | 每位评分者与评分点数量口径同步；§6 增加 `injected.json` 与"注入集必须与记录一致"的硬检查 |
| §5.5 | 新增零差值/`m` 口径表（m=6 原始 p=0.0313 可 <0.05，但三个主比较的 Holm 家族到 m=7 才可能达到校正后 0.05）；`r_rb` 只用非零差值、`dz` 用全部 7 个配对；`dz` 覆盖 Y5 |
| §7.3 | 记忆核对表扩为 15 列（含 supports/contradicts/unit/required/stable/has_prefix/injected） |
| §8.6 | S3/S6 先报 M4 结论再报差值，禁止把"注入集不含困难记忆"包装成"记忆无效" |
| §9 | 新增第 16 条：只有 active 记忆会被注入，及其对可检测性的限制 |
| 附录 B | H1 改为需 m=7（Holm 校正后门槛）、H2 补"m=6 原始 p 可 <0.05 但校正后不达标"；H4 改写；H5 补 `r_rb`/`dz` 与对数口径 |

---

## 7. 结论

- **记忆 Gold Standard：全部通过**（20/20 条记忆的 status、strength、supports、contradicts、unit、
  阈值达标、前缀、注入状态全部与实现一致，且每条强度都能用公式手算复现）。
- **不应生成的 4 类记忆**（S2 difficulty、S5 概率论 difficulty、S7 task_topic、S7 task_performance）
  实测均未生成。
- **本轮不含模型生成**：没有任何计划文本被产出或评分，M1–M4 与 H1–H5 的"结果"仍未开始采集；
  本报告只固定了验收基准（gold），不是实验结果。
- **局限**：仅本机 Python 环境实跑，不是 CI；临时脚本与 7 个 `.db` 留在 `%TEMP%` 未入库；
  实跑依赖的 `REF_DATE` 一旦变化，`age_days` 与 S3/S4/S6 的强度都会变，**必须**用同一基准日复跑。
