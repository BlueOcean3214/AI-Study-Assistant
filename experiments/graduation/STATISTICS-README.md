# 统计分析与假设检验（experiments/graduation/analyze_stats.py）

本目录的统计脚本把盲评原始分（`ratings_formal_raw.csv` / `ratings_calibration_raw.csv` /
`ratings_supplement_raw.csv`）、裁决表（`ratings_adjudicated.csv`）与实验产出
（`results.jsonl`）按 `docs/GRADUATION-EXPERIMENT-DESIGN.md` §5.4–§5.5、§8 与附录 B
的口径，派生出最终分、格内聚合、描述统计与预登记假设检验结果。

- 脚本：`experiments/graduation/analyze_stats.py`
- 测试：`test_graduation_stats.py`（仓库根，与其它 `test_graduation_*.py` 并列）
- 设计依据：`docs/GRADUATION-EXPERIMENT-DESIGN.md`
  - §5.4.6 统一长表与派生规则（:1022–1040）
  - §5.4.7 适用矩阵（:1042–1067）
  - §5.5 统计方法（:1080–1198）
  - §8 结果分析方法（:1339–1364）
  - 附录 B 假设与判据（:1469–1478）

## 1. 设计原则：只读、显式、可复核

1. **所有输入路径必须命令行显式给出**，脚本不内置任何默认路径，也不会去猜
   `blind_private/` 或某个 run 目录的位置；不读真实数据也能跑（测试全部使用临时目录
   里的合成数据）。
2. **输入只读**：运行前记录每个输入的 SHA-256，运行后再算一遍，不一致就以
   `input-mutated` 失败退出。所有哈希同时写进 `input_hashes.csv`。
3. **输出禁止覆盖**：`--out-dir` 若已存在且非空，或任一目标文件已存在，直接
   `output-exists` 失败；绝不覆盖上游原始表，也绝不覆盖自己的历史输出。
4. **fail-closed**：表头、类型、取值、配对完整性、84/6/23 份数边界、适用性矩阵任何
   一处不符，立即打印具体原因（文件、行号、字段、实际值）并以退出码 2 停止，不产出
   半份结果、不静默跳过、不补 0。
5. **纯标准库**：只用 `statistics` / `random` / `json` / `csv` / `hashlib` / `math`，
   不依赖 numpy/scipy（当前 `.venv` 无 scipy），随机过程全部由固定种子派生。

## 2. 用法

```powershell
$env:PYTHONIOENCODING='utf-8'
F:\somethingProject\dsbot\.venv\Scripts\python.exe `
  experiments\graduation\analyze_stats.py `
  --formal-raw     <ratings_formal_raw.csv> `
  --calibration-raw <ratings_calibration_raw.csv> `
  --supplement-raw <ratings_supplement_raw.csv> `
  --adjudicated    <ratings_adjudicated.csv> `
  --mapping        <blind_private\mapping.csv> `
  --results        <results.jsonl> [--results <另一个 results.jsonl> ...] `
  --out-dir        <新的空目录> `
  [--seed 2026111301] [--bootstrap 10000]
```

- `--results` 可重复、可省略。省略时 H5（Y2/Y3/Y5）不执行，报告里写明原因是
  「未提供 --results」，其余分析照常。
- `--seed` 只影响 bootstrap 抽样与报告里的记录值；`--bootstrap` 是重采样次数
  （默认 10000，测试里用 500 加速）。
- 成功时打印 `OK 输出目录：<dir>` 并列出文件名，退出码 0。

## 3. 输出文件

| 文件 | 内容 | 性质 |
| --- | --- | --- |
| `inter_rater_agreement.csv` | 裁决**前**的评分者一致性（9 行：P1–P4/S2 加权 κ、S3_flag、S4_verdict、E1、E7） | 描述性 |
| `ratings_final.csv` | 正式 84 份的派生最终分 | 派生数据 |
| `ratings_final_detail.csv` | 逐项派生明细（两人原值、种类、最终值、状态、是否裁决） | 派生数据（审计） |
| `ratings_calibration_final.csv` | 校准 6 份的派生最终分 | 派生数据 |
| `ratings_supplement_final.csv` | 补充 23 份的 E1/E7 最终值 | 派生数据 |
| `ratings_supplement_final_detail.csv` | 补充批次的逐项派生明细 | 派生数据（审计） |
| `cell_aggregates.csv` | 28 格 × 6 项的格内聚合（每格 3 份先聚合） | 派生数据 |
| `descriptive_statistics.csv` | 条件 × 指标的描述统计（均值/SD/中位数/格内 SD/95% Bootstrap CI） | 描述性 |
| `primary_hypothesis_tests.csv` | H1/H2a/H2b 三主对比的精确 Wilcoxon 结果与结论档位 | **推断性** |
| `holm_adjusted_results.csv` | Holm 校正明细（主家族 3 个 + H5 家族 2 个） | **推断性** |
| `friedman_results.csv` | P1–P4 四条件的 Friedman 定位检验 | **推断性（定位用）** |
| `binary_rates.csv` | S3_flag 各条件的格内比例 | 描述性 |
| `binary_tests.csv` | 二分类的 Cochran's Q（条件不足时不执行并给出原因） | 描述性（弱证据） |
| `h5_results.csv` | H5 的 Y3（对数）/Y5 Wilcoxon 与效应量、Bootstrap CI | **推断性** |
| `y2_calls_distribution.csv` | Y2 的每样本四条件请求次数分布 | 描述性 |
| `input_hashes.csv` | 本次运行用到的全部输入路径与 SHA-256 | 审计 |
| `analysis_report.md` | 人读报告（第 0–12 节，含汇总桶与复核指引） | 报告 |

报告第 11 节把每个假设归入互斥的桶：**支持 / 反向结果 / 未观察到差异 / 不适用 /
不可计算 / 达到统计门槛但未满足效应量条件 / 不可判定（m≤5）**。桶名即结论用词，
不允许替换成「显著 / 不显著」以外的模糊表述。

## 4. 分析单位与格内聚合（这是最容易出错的地方）

- **分析单位是样本（n=7），不是 28 格、不是 35 次、更不是 140 行评分。**
  82 或 84 份只是抽取次数，属于「噪声」，不能当作独立观测扩大样本量（§5.5、§8）。
- 每格（样本 × 条件）有 **3 份**输出（设计文档写「5 次重复」，盲评包每格只抽 3 份
  ——见第 8 节歧义说明）。3 份先按项聚合：1–5 分取均值（同时记录格内 SD），
  0/1 取比例，S4_verdict 取多数（并列记 `tie`），然后才做样本级的配对比较。
- 因此 28 格 × 3 份 = 84 份，聚合后得到 7 个样本 × 4 个条件的 28 个格值。

## 5. 分析顺序（顺序本身就是方法的一部分）

1. **保留原始分**：原始表只读，永不改写。
2. **先算一致性**：在裁决之前，用原始分算评分者一致性；校准 6 份与补充 23 份不进主 κ。
3. **再裁决**：只对触发阈值的项（1–5 分两人差 >1；0/1 或 S4_verdict 两人不同）使用
   裁决表的 `final_value`；裁决表里出现「未触发阈值」或「没有两人有效分」的行会直接
   报错。
4. **派生最终分**：未触发阈值取两人算术均值（S4_verdict 与 0/1 需完全相同才对，
   不同即触发裁决），触发且有裁决取裁决值，**触发但未裁决则保留 `pending` 且不进任何
   聚合分母**。
5. **格内聚合** → **n=7 检验**。

## 6. 一致性统计口径

| 指标 | 系数 | 备注 |
| --- | --- | --- |
| P1–P4、S2（1–5） | 二次加权 Cohen's κ，w=(i−j)²/16 | 返回 `raw_agreement`（完全相同比例）与 `po_weighted`/`pe_weighted` |
| S3_flag（0/1） | 未加权 Cohen's κ | `raw_agreement` 即 `po` |
| S4_verdict（三分类） | 未加权 Cohen's κ | 无序类别，**不得**按 1–5 有序加权；理由写在行内 `note` |
| E1/E7（补充） | 未加权 Cohen's κ | 描述性、样本量小，**不计入主 κ** |

报告同时给出有效评分份数、原始一致率、κ、不适用数量（`n_na_rows`）与缺失数量
（`n_missing_rows`）。两人都写 N/A 的项整份跳过；只有其中一人写 N/A 时，该份不进配对，
并在原始表校验阶段就已按「两人适用性不一致」报错（与 `check_ratings.py --strict` 同口径）。

退化情形一律给原因代码，不填 0：`n=0`（没有可用配对）、`pe=1`（期望一致率为 1，κ 无定义）。

## 7. 主假设与检验（精确 Wilcoxon 符号秩）

预登记的三道主对比（§5.5）：

| 假设 | 对比 | 指标 | 方向 |
| --- | --- | --- | --- |
| H1 | D − B | P3 | D > B |
| H2a | D − B | P1 | D > B |
| H2b | C − A | P1 | C > A |

检验实现细节：

- **精确分布**：不用正态近似，直接枚举 2^m 种符号组合（m≤7，最多 128 种），
  因此 p 值是精确的、可逐位复算的。
- **平均秩**处理并列；**剔除零差**（m 只数非零差）；**不做连续性校正**。
- 输出 `W+`、`W−`、`m`、原始双侧 p、**最小可达 p**、Holm 校正后 p。
- 效应量：
  - `r_rb = (W+ − W−) / (W+ + W−)`，只用来非零差的秩和（档位 0.1/0.3/0.5）。
  - `dz = mean(d) / sd(d)`，用**全部** 7 个差（含 0），档位 0.2/0.5/0.8。
  - 不采用 Cliff's δ。

**多重比较**：主家族 3 个对比统一做 Holm（step-down，`factor = k − step + 1`，
并保证单调不减）；H5 的 Y3/Y5 另成一个 2 个对比的小家族单独 Holm。两个家族的行都写进
`holm_adjusted_results.csv`，用 `family` 列区分。

**最小可达 p 的硬边界**（设计文档已写、测试已核对）：

| m | 最小双侧 p | 三对比 Holm 后最低 |
| --- | --- | --- |
| 7 | 2/2⁷ = 0.0156 | 0.0469 |
| 6 | 2/2⁶ = 0.0313 | 0.0939（**不显著**） |
| 5 | 0.0625 | ≥0.1875 |
| ≤4 | ≥0.125 | — |

> H1 用的是 P3，而 S7 的 P3 按 §5.4.7 本来就**不适用**（N/A），所以 H1 的 m 上限是
> **6**，其最小可达 p 是 0.0313；经 3 个对比的 Holm 校正后最低只能到 0.0939，
> **在 α=0.05 下 H1 结构上不可能显著**。这是设计层面的既成约束，不是实现缺陷；
> 报告里会照实写出，不会把 3 份或 5 次改成别的数来凑显著性。

**结论档位**（`conclude()`）：`status != ok` → 「不适用」（`m=0`）或「不可计算」；
`m ≤ 5` → 「不可判定（m≤5，原始 p 最低 ≥0.0625）」；Holm p > 0.05 → 「未观察到差异」；
`r_rb ≤ 0` → 「反向结果」；四项判据（平均差 ≥0.5、|r_rb| ≥0.5、dz ≥0.8、m=7）
全过 → 「支持（推断性，n=7）」；否则「达到统计门槛但未满足效应量条件：<未过项>」。

### 三种退化情形（必须给原因代码，不得填 0、不得写「无差异」）

| 情形 | Wilcoxon | r_rb | dz | 报告怎么写 |
| --- | --- | --- | --- | --- |
| 7 个差**全为 0** | 不适用（`m=0`），不生成 p | 不适用 | 不可计算（`sd=0`） | 「不适用」+ 原因码，绝不写「无差异」 |
| 7 个差同为**非零常数** | 照常执行，p = 0.0156 | ±1 | **不可计算**（`sd=0`） | 说明「差异恒定，dz 因 sd=0 不可计算」，**不得**写成「无差异」 |
| 部分差为 0 | 剔除零差后按 m 计算 | 只用非零差 | 用全部 7 个差 | 正常报告，并注明剔除了几个零差 |

## 8. 已知设计歧义（必须在论文与报告里如实说明）

`§5.5 :1085–1086` 写的是「同一格 5 次重复中 ≥3 次为 1 即判定 S3_flag=1」，
但盲评包**每格只抽 3 份**（§5.4.4、§5.4.7）。3 份里无法取「≥3/5」。

处理方式：**不改口径、不改门槛、不把 3 说成 5**。代码与报告都原样引用设计文档原文，
`analysis_report.md` 第 8 节会打印 `DESIGN_AMBIGUITY_REPEATS` 常量（含设计原文与
实际抽样数的对照），并说明：数据不足时只输出描述性比例（`binary_rates.csv`），
Cochran's Q / 精确 McNemar 只在拿到足够条件值时执行，否则以 `not_executed` +
具体原因落盘，绝不硬凑一个 p。

## 9. Friedman 与二分类

- **Friedman**：7 区组 × 4 处理（P1–P4），只用于「四条件是否存在整体差异」的定位，
  **不替代**三道预登记对比，也**不做**未预登记的事后两两扫描。
  只有每个区组内四值完全相等（Q=0）才写「不适用（无变异）」；零散缺格时不补 0，
  直接 `missing-cell-value` 失败态。
- **二分类**（S3_flag / E1 / E7）：报各条件的格内比例；需要比较时用 Cochran's Q
  或精确 McNemar，并明确标注为弱证据、描述性。

## 10. H5（成本类，依赖 results.jsonl）

- **Y2**：每样本四条件的 `request_count` 分布（描述性表格）。
- **Y3**：`elapsed_seconds` 的格内均值，取自然对数后做 Wilcoxon（D−A），
  报 `r_rb`、`dz` 与 Bootstrap CI。任一样本均值为 0 或负数时
  **fail-closed**（`log-domain-violation`），说明「对数定义域不满足」，
  **不加常数**（加常数会伪造出可比的对数值）。
- **Y5**：首次请求的 `prompt_tokens`（优先 `requests[0].prompt_tokens`，
  退回 `token_usage.prompt_tokens`），原始值做 Wilcoxon，报同样的效应量与 CI。

## 11. Bootstrap CI

- 按**样本**重采样（不是按格、不是按评分行），默认 10000 次，`--seed` 固定。
- 每次重采样的随机种子由 `SHA-256(seed | tag)` 派生的 64 位整数确定，所以
  **同一输入 + 同一 seed 必得同一 CI**，且每个条件的 CI 相互独立、可单独复算。
- 采用百分位法（线性插值取 2.5%/97.5% 分位）。n<2 时不计算并给 `n<2`。

## 12. 案例级结果（H3/H4/H-risk）

只报描述性统计与逐案例的判定，**不报 p 值、不写「显著」**，在报告里单列、与推断性
结果物理隔开（§8 要求「分栏标推断性与描述性」）。

## 13. 测试

```powershell
$env:PYTHONIOENCODING='utf-8'
F:\somethingProject\dsbot\.venv\Scripts\python.exe test_graduation_stats.py
```

- 只用 `tempfile.mkdtemp()` 临时目录 + 完全合成的数据；不读真实 run、不读
  `blind_private/`、不生成正式盲评包；结束时（即使失败）清理临时目录。
- 覆盖：平均秩、1–5/0–1/三分类解析（含拒绝 `05`、`" 3 "`、越界、未知标签）、
  六种读表错误、未加权 κ 与二次加权 κ 的**手算**对照、用独立 DP 复算精确双侧 p 对照
  枚举实现、m=7/6/5/4 的最小可达 p、并列平均秩、零差剔除、全零与非零常数两种退化、
  `r_rb`/`dz` 手算、Holm 逐步校正与并列最小 p 与单调性、Bootstrap 可复现与独立复算、
  χ² 尾概率、Friedman 手算 Q/p、端到端 16 个输出 CSV 的行数与关键数值、
  样本不混、校准与补充排除、未裁决排除、pending 分母、mapping/原始表/results 的
  fail-closed、输入哈希不变、输出禁覆盖、缺文件。
- 断言既有「精确手算小案例」（期望值写在注释里、可人工复核），也有 fail-closed
  的负向用例；不是「只断言不抛异常」。

## 14. 数据安全边界

- 脚本不写任何输入文件，不移动、不删除、不改名。
- 输出只写 `--out-dir` 下的新文件；目录非空即拒绝。
- 报告与 CSV 里只出现 `blind_id`、`sample_id`、条件、指标与数值，
  **不含**任何评分者身份信息、源 `run_id`/`row_number` 之外的私有路径细节，
  可直接放进论文附录。
- 真跑之前请先确认 `experiments/graduation/blind_private/` 已在 `.gitignore` 中
  （`git check-ignore -v` 实测），避免私有数据误入版本库。
