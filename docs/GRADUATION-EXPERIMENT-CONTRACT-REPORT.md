# DSBot 记忆实验 · 生产者—消费者字段契约报告（PR #26 第十一轮）

- 分支：`feat/graduation-preflight-integrity`
- 基线提交：`1911daf49dddaa029fedfb2cb3024392a047512a`（第十轮交付，第十一轮在其上修复「plan.json 已声明但清单 batch 缺失/为空时仍建立计划索引并产生下游 PASS」这一复审阻断）
- 冻结口径：`docs/GRADUATION-EXPERIMENT-DESIGN.md` v4.5（本报告未修改该文件）
- 取证范围：只读源码与合成数据。未读取或修改真实 `runs/`、`feedback.db`、`blind_private/`，未生成盲评包，未执行真实 `--repack`。
- 运行环境：本机 PowerShell + `F:\somethingProject\dsbot\.venv\Scripts\python.exe`，非 CI。

## 结论

1. 当前生产器 `run_experiment.py` 从未按 §7.1 产出过记录（首个版本 0471275 起即如此，不是后来回归）。三个消费者里，打包器按生产器 schema 写，预检与统计器按 §7.1 写，这才是"契约不一致"的根因。
2. 29 个 §7.1 字段中，12 个直接可用、10 个可确定性派生、7 个不可恢复。新增标准化层 `experiments/graduation/result_schema.py` 承担这层转换，逐字段标注状态与依据（不允许"名字像就复制"）。
3. 统计器的 `condition` 比较已改为"由源记录 `group` 派生"，与打包器 `SourceRow.condition = self.group` 同口径；mapping 与源记录任一侧被篡改都会报 `results-mapping-mismatch`。
4. 预检对生产器记录走标准化路径：可派生字段照常核验，7 个不可恢复字段逐字段 FAIL（不降级为 WARNING），设计原生记录仍走原严格路径（回归用例证明未放宽）。
5. 第三轮复审指出的阻断问题已修：预检不再因「这一行凑不齐 29 字段」就整行跳过。**执行证据**（身份四元组、批次归属、是否属于 plan.json、plan_sha256 是否同源）与 **§7.1 契约完整性**（29 字段是否齐备）现在是两个独立维度：缺口照报 FAIL，执行证据照常核验，报告里分开计数（第五节）。
6. 新增生产者—消费者契约测试 42 个用例、343 项断言（第十轮扩到 62 个用例 / 451 项断言，第十一轮扩到 67 个用例 / 487 项断言）：真实跑 `run_one()` → 真实 `append_jsonl()` 落盘 → 依次过预检、打包器、统计器。7 套测试全绿（详见第十四、十五节）。
7. 第四轮复审的两处遗留已在第五轮修掉：**批次不可判定**不再冒充契约缺口（只报 UNKNOWN，其余字段失败照常保留）；**结果行与计划文件的逐字段同源审计**补齐 `plan_seq`/`seed`/`scheme`/`algorithm_version`，其中 `plan_seq` 必须按实验身份定位到计划任务再比 —— 只查「是不是整数 / 在不在范围内 / 连不连续」抓不到那类篡改。B0 legacy 的 WARNING 现在由真实 `b0-*` 目录走完整流程验证（第六节）。
8. 第六轮复审的两处问题已修（第十二节）：**批次判定拆成四态**，只有「目录名与记录都没有批次信息」才报 UNKNOWN，同一文件内声明冲突与 `batch` 值非法（list/dict/int/bool/空串）各自独立 FAIL，不再被压成「无从推断」；**计划身份唯一性**改为逐任务展开核对，`set`/`dict` 不再吃掉重复身份，重复身份一定 FAIL 并点名各自 `plan_seq`，计划结构缺失时直接 FAIL 且不生成看似完整的身份索引。
9. 第七轮复审的两处问题已修（第七节）：**清单成为批次归属的唯一可信来源**，目录名只在能与清单一致时确认批次，记录声明只能交叉验证；未识别目录不再能靠记录统一写 `batch="main"` 冒充正式批次，清单缺 batch / batch 为空 / 没有 batch 列 / 目录与清单矛盾 / plan.json 与 results.jsonl 清单批次不一致全部独立 FAIL。**plan.json 增加严格 JSON 类型核验**：顶层 `schema_version`/`seed`/`round_count`、每个任务的 `plan_seq`/`round_index`/`repeat_index` 必须是 int 且不能是 bool，`group` 必须是非空字符串；`1.0 == 1`、`True == 1` 这类等值比较不再能逃过预检。契约测试 389 项、七套合计 1093 项本机全绿。
10. 第八轮复审的问题已修（第八节）：`check_plan()` 检测到 plan.json 类型/结构/哈希错误后仍把 plan 返回，`run_preflight()` 随即把这份无效计划交给 `check_results()`，后者照常建立计划身份索引，5 条完整结果就能拿到「计划完成度 PASS」——计划侧的 FAIL 与结果侧的 PASS 同时出现。现在计划核验改为 `PlanCheck` 三态（missing / valid / invalid）并贯穿整个调用链：invalid 计划不建索引、不做成员关系与同源核验、不给计划完成度结论，但仍保留计划区域原始 FAIL；结果侧不再把它误当 legacy 而报 `plan_sha256` WARNING。契约测试 404 项、七套合计 1108 项本机全绿。
11. 第九轮复审的问题已修（第九节）：`check_manifest()` 把 SHA-256/大小/记录数不一致的文件放进 `ManifestIndex.failed`，但 `run_preflight()` 只用它压掉「清单覆盖 PASS」，随后仍把这些文件交给 `check_plan()`、`check_results()`、`by_batch`、覆盖与 165 份总量。于是一个哈希对不上的 results.jsonl 仍可能拿到主批次覆盖 PASS、计划完成度 PASS 与总计 PASS。现在文件信任成为下游结论的前置条件：`failed` 中的 plan.json 进入第四态 `untrusted`，不建索引、不做计划核验；`failed` 中或未纳入清单的正式 results.jsonl 仍解析诊断，但返回空身份、不产生完成度结论、不计入 `by_batch`/覆盖/总量；B0 legacy 的清单外例外保持不变。契约测试 427 项、七套合计 1131 项本机全绿。
12. 第十轮复审的两处阻断已修（第十节）：**B0 legacy 不再绕过已声明文件的完整性失败**——`legacy` 只豁免「未纳入清单」，已声明且进入 `failed` 的 B0 plan/results 仍按不可信处理，只有未声明时才走 legacy；**plan/results 清单批次冲突不再只报一条 FAIL**——新增 `plan-results-conflict` 批次态，冲突时 plan 直接进入 `untrusted`、不建身份索引，results 以 `trusted=False` 只做诊断，覆盖与 165 份总量均不产生正向结论。契约测试 451 项、七套合计 1155 项本机全绿。
13. 第十一轮复审的阻断已修（第十一节）：第十轮只处理了「两侧清单 batch 都合法但不同」。**plan.json 已声明、哈希/大小/records 都正确，但清单 batch 为空或清单没有 batch 列时**，`plan_trusted` 仍为 True，计划照样进 `check_plan()` 建身份索引，结果侧照样拿计划完成度 PASS、main 覆盖 PASS 与总量 PASS；results 一侧 batch 为空时同样会留下完成度 PASS。现在 plan/results 任一侧的清单 batch 不可用都会形成显式的 `batch_pairing_problem`：plan 进入第四态 `untrusted`（不退化成 missing/legacy），results `trusted=False`，覆盖与总量一律跳过；明确清单错误仍是 FAIL，不降级成 UNKNOWN/WARNING。B0「未纳入清单」的 legacy 例外保持不变。契约测试 487 项、七套合计 1191 项本机全绿。

本轮没有让预检报告"变绿"：真实数据的预检结论仍是 FAIL，因为生产器确实没有记录那 7 个字段。本轮的产出是"能把这件事准确查出来、并且证明查得对"。

## 一、字段契约对照表（29 个 §7.1 字段）

状态含义：direct = 生产器直接提供；derived = 可从其他字段无损、确定地派生（含校验）；unrecoverable = 生产器没有提供，无法可靠恢复。

| # | §7.1 字段 | 状态 | 生产器来源 | 转换规则 / 不可恢复原因 |
| --- | --- | --- | --- | --- |
| 1 | run_id | unrecoverable | — | 生产器不记录 run_id；按 §7.1 模板的形态（如 20261120T140012Z-S1-D-r3）现编一个等于伪造一个不存在的标识 |
| 2 | experiment_id | direct | experiment_id | 同名同义同类型（非空字符串） |
| 3 | sample_id | direct | sample_id | 同名同义（非空字符串） |
| 4 | condition | derived | group | condition 就是 group：打包器 `make_blind_pack.py:323-326` 的 `SourceRow.condition` 直接 `return self.group`；记录若自带 condition（设计原生数据）必须与 group 相等，否则视为归属冲突 |
| 5 | group | direct | group | 同名同义（非空字符串） |
| 6 | repeat_index | direct | repeat_index | 同名同义（int） |
| 7 | round_index | direct | round_index | 同名同义（int）；v4.2 起 round_index == repeat_index，两者都在且不等视为记录自相矛盾 |
| 8 | plan_seq | direct | plan_seq | 同名同义（int，仅作顺序审计，不进身份） |
| 9 | seed | direct | seed | 同名同义（int） |
| 10 | scheme | direct | scheme | 同名同义（非空字符串） |
| 11 | algorithm_version | direct | algorithm_version | 同名同义（非空字符串） |
| 12 | plan_sha256 | direct | plan_sha256 | 同名同义（64 位小写十六进制）；legacy 允许 null，按"无 plan.json 只报 WARNING"的既有规则处理 |
| 13 | batch | derived | run 目录名 | 记录无批次字段；由目录名按 `BATCH_PREFIXES` 推断（`preflight_experiment_data.py:74-80`、`:533-573`），推断不出就不填（调用方报 UNKNOWN） |
| 14 | model | derived | provider + model + temperature + top_p | §7.1 的 model 是对象 `{provider, name, temperature, top_p}`；生产器把同一组信息平铺成四个字段，name 取字符串字段 model，逐一搬进对象，无信息损失；任一为 None 则不派生 |
| 15 | base_sha | unrecoverable | — | 生产器不记录生成实验时所在工作树的 git 提交；事后取 HEAD 得到的是"现在的"提交，不是"当时的"提交 |
| 16 | prompt_sha256 | direct | prompt_sha256 | 同名同义（64 位小写十六进制） |
| 17 | messages_path | derived | prompt_ref + prompt_sha256 | 生产器把同一份 system/user 消息写成 `run_dir/prompts/<sample>/<group>.txt`；本模块解析该 .txt 重建 `[system, user]`，再用 `build_prompts.prompt_sha256` 同一算法核对记录里的 prompt_sha256，核对通过才算派生成立 |
| 18 | knowledge_result_ids | unrecoverable | — | 生产器只把检索结果渲染进 prompt 文本，不记录条目 id |
| 19 | memory_injected | unrecoverable | — | 不记录结构化记忆注入（memory_type/subject_key/strength/status）；prompt.txt 里只有渲染后的自然语言，反推不出结构化字段 |
| 20 | short_term_injected | unrecoverable | — | 不记录短期记忆的注入开关与条目 |
| 21 | difficult_flag | unrecoverable | — | 不记录 difficult 布尔值；它只体现在 prompt 文本渲染里，文本无法区分"确实难"与"恰好写到类似句子" |
| 22 | output_raw | direct | raw_output | 字段改名，语义相同：最后一次模型起草的原始输出文本 |
| 23 | answer | derived | final_output.answer | 成功记录的 answer 非空（`agent_plan_draft._run_draft_loop` 只在校验通过后 `return answer.strip()`） |
| 24 | plan | derived | final_output.plan | 已过 `plan_service.normalize_plan` 的 7 字段白名单与 `validate_plan_for_save` 规则校验；7 个设计键必须齐备 |
| 25 | validation | derived | success + corrections_used | 起草链只有在 JSON 解析、plan 结构、规则校验、`check_grounding` 来源校验全部通过后才 `return {"ok": True, ...}`，故成功记录蕴含 json_ok/plan_valid/grounding_ok 为 true；corrections_used 生产器直接记录 |
| 26 | usage | derived | token_usage | `token_usage.available=True` 时给出同名同义的两个计数；不可用时计数全 null，不得补 0 或估算 |
| 27 | latency_ms | derived | elapsed_seconds | elapsed_seconds 是 `round(墙钟秒, 3)`，毫秒 = `round(elapsed_seconds*1000)`，3 位小数即 1ms 粒度，无精度损失 |
| 28 | error | derived | success + error_type + error_message | §7.1 模板只描述成功记录且要求 error 为 null；失败记录在 §7.1 没有对应表示，必须作为冲突上报，不能塞进 error 字符串冒充可统计记录 |
| 29 | data_kind | unrecoverable | — | 不记录数据来源标记；写常量 simulated_learner_fixture 等于用"代码大概只会跑模拟数据"代替数据证据 |

统计：direct 12、derived 10、unrecoverable 7，合计 29（`result_schema.contract_rows()` 与 `DIRECT_FIELDS/DERIVED_FIELDS/UNRECOVERABLE_FIELDS` 由同一张表派生，测试断言三者不重不漏）。

## 二、确定性映射方案（可执行的规则，不是"名字相似就复制"）

标准化入口：`result_schema.normalize_record(record, batch=None, run_dir=None)` → `Normalized`，逐字段给出 `FieldResult(name, state, ok, value, reason, basis)`。

| 派生字段 | 判定条件（任一不满足即 ok=False 并给出原因） |
| --- | --- |
| condition | group 是非空字符串；若记录自带 condition 则必须与之相等（`derive_condition` 返回 ok / missing-group / conflict） |
| batch | 调用方给出推断值才填；否则字段本身不参与判定（由调用方报 UNKNOWN） |
| model | provider、model 均为非空字符串，temperature、top_p 为有限数值 |
| messages_path | run_dir 与 prompt_ref 均可用；拒绝绝对路径与 .. 越界；解析 .txt 成两条消息后哈希必须等于 prompt_sha256 |
| answer / plan | final_output 是对象且 answer 非空字符串 / plan 为含 7 个设计键的对象 |
| validation | `success is True` 且 corrections_used 为非负 int |
| usage | `token_usage.available is True` 且两个计数均为 int |
| latency_ms | elapsed_seconds 为非负有限数值 |
| error | `success is True` 且 error_type/error_message 皆空 → 值为 None（§7.1 要求的 null）；失败记录记冲突 |

视图语义（`Normalized`）：

- `view()`：只含取到值的设计字段（null 不出现，符合"§7.1 里 error 就该是 null"）。
- `null_valued()`：取到值且设计值就是 None 的字段（成功记录的 error、legacy 的 plan_sha256）——这类字段不算缺口，否则每条成功记录都会多报一条假缺口（本轮修掉的一个真缺陷）。
- `missing()`：恒等于 `unrecoverable() ∪ failures()`；`is_complete()` = 既无不可恢复、也无派生失败。
- `failures()`：状态为 direct/derived 但这一行取不到值 → 原因（不静默）。

## 三、不可恢复字段及其影响（7 个，一律 FAIL，不得填默认值）

| 字段 | 影响 |
| --- | --- |
| run_id | 无法给每条记录一个可追溯的运行标识；§7.1 的"一次运行唯一标识"不可核验 |
| base_sha | 无法证明数据由哪个代码版本产生 → §5.3 的"结果行与 plan.json 同源"只能核到 plan 层，核不到代码层 |
| knowledge_result_ids | 无法复核该条记录引用了哪些知识条目 → 检索一致性不可审计 |
| memory_injected | 无法审计实际注入了哪些记忆（类型/主题/强度/状态）→ 记忆组间差异只能靠 prompt 文本证据 |
| short_term_injected | 同上，短期记忆开关不可审计 |
| difficult_flag | 难例标记不可审计 → 各组的困难样本是否同分布无法核对 |
| data_kind | 数据来源标记缺失 → 无法在数据层区分真实 LLM 与 fixture（只能靠目录约定） |

对统计量的实际影响（已核对，避免夸大）：盲评映射与 Y2/Y3/Y5 成本指标只依赖 experiment_id/sample_id/group/condition/repeat_index/round_index/prompt_sha256/request_count/elapsed_seconds/token_usage，这些全部可得。所以缺失的 7 个字段损害的是可复现性与记忆注入审计，不是统计公式本身的正确性——但按冻结设计，这份数据仍不能被判为"符合 §7.1"。

## 四、condition 错误映射的修复

- 修前：`analyze_stats._compare_mapping_identity()` 把 condition 当普通字符串字段，直接比较 `row.get("condition")` 与 mapping 的 condition。生产器记录没有 condition，`row.get` 恒为 None，于是真实 mapping 必然 113 处 mismatch。
- 修后（`analyze_stats.py:1692-1706`）：`MAPPING_IDENTITY_FIELDS` 去掉 condition（`analyze_stats.py:1624-1634`），改由 `result_schema.derive_condition(row)` 判定：
  - mapping condition 为空 → 报"condition 为空"；
  - derive_condition 非 ok → 报"无法确认 condition 归属（原因）"；
  - 派生值与 mapping 不等 → 报"condition=...（由 group 派生）与 mapping 的 ... 不一致"。
  - 错误码仍是 results-mapping-mismatch，不新增未文档化的码。
- 反证用例（全部通过）：
  - `test_stats_condition_tamper`：只改 mapping.condition → mismatch，且同文件同行的正确 mapping 通过（证明不是哈希在报错）。
  - `test_stats_source_group_tamper`：改源行 group 并重算文件哈希与行哈希（绕过哈希校验）→ 仍被身份比对抓到，且同时报 condition 归属被改。
  - `test_stats_source_condition_conflict`：源行自带 condition≠group → "无法确认 condition 归属"。
  - `test_stats_group_missing` / `test_stats_mapping_condition_empty`：缺 group、mapping condition 为空。

## 五、部分核验：执行证据 vs §7.1 契约完整性（第四轮修复）

### 5.1 修的是什么缺陷

第三轮的预检对生产器记录先做标准化，然后 `if not normalized.is_complete(): continue`。生产器记录必然缺 7 个不可恢复字段，于是**每一条正常记录都被整行跳过**，连锁后果是：

1. 身份不进入集合比较 → `plan_ids - set(identities)` 把计划任务全报成缺结果；
2. 生成任务总数据此计算会偏成 0（批次数与覆盖统计一起失真）；
3. 有 plan.json 时，结果行 `plan_sha256` 与计划文件的同源核对被跳过；
4. 身份重复、批次归属、是否属于计划等检查都没有跑。

结果是「真实跑完的数据」与「根本没跑」在报告里长得一样——这比缺字段本身更危险：它会让预检给出一个**看起来完整、其实什么都没核**的结论。

### 5.2 修复后的判定规则

| 维度 | 证明什么 | 数据来源 | 缺口时的行为 |
| --- | --- | --- | --- |
| 执行证据 | 这一行是否真的跑过：身份四元组可识别、批次归属正确、属于 plan.json、plan_sha256 与同目录 plan.json 同源 | 12 个直供字段 + 10 个可派生字段 | **照常核验**，不因 7 个缺口而跳过 |
| §7.1 契约完整性 | 记录是否具备冻结设计要求的全部 29 个字段 | 29 字段是否齐备 | 不可恢复字段逐字段 FAIL（按字段聚合，不逐行刷屏），并单独计数 |

实现要点（`preflight_experiment_data.check_results`）：

- 保留 `failures()` 逐字段 FAIL、把 `missing()` 里剩下的记成`gap_fields`（按字段聚合）；
- **不再 `continue`**：改用 `row = normalized.view()` 继续走原有的类型/批次/身份/重复/计划成员/同源校验；
- 字段齐全性检查只在设计原生行上做（`if not partial`），生产器行的「取不到」不重复报一次「值不对」；
- `null_ok`（设计值就是 null 的字段，如成功记录的 `error`、legacy 的 `plan_sha256`）不当作缺口，也不当作类型错误；
- 不补默认值、不把缺口降级成 WARNING、不为「部分标准化成功」产生任何 §7.1 合规 PASS；
- **plan.json 存在时，`plan_sha256` 为 null 与不一致都各自产生独立 FAIL**（「记录未提供」与「不同源」是两回事）；只有没有 plan.json 的 B0 legacy 才走既有的 WARNING 例外。

### 5.3 报告里分开显示的四组数字

每个结果文件一条 notes 记执行证据、一条记契约完整性，再加一条计划覆盖：

- 执行证据：非空结果行 N 行、身份可识别 M 行（唯一身份 K 个）；
- §7.1 完整性：完整记录 X 行、存在契约缺口 Y 行（缺口字段 Z 个）；
- 计划覆盖：计划任务 N 个、身份命中（已执行）M 个、未命中 K 个（命中即 PASS，未命中即 FAIL）；
- 汇总行仍是 PASS/WARNING/UNKNOWN/FAIL 四类计数——**整个文件仍因缺口 FAIL**，但不会再声称`计划任务全部缺失`。

### 5.4 合成数据演示（本轮实跑输出）

用真实 `run_one()` 产出记录 + 合成 plan.json，三种情形（同一份记录、同一份计划，只改 plan_sha256）：

| 情形 | 身份列表 | FAIL 合计 | 其中契约缺口 | 其他 | 计划完成度 |
| --- | --- | --- | --- | --- | --- |
| A 正常记录 + 同源 plan.json | `[('exp-contract', 'S2', 'D', 0)]` | 7 | 7 | 0 | PASS（1 个计划身份全部有结果） |
| B plan_sha256 = null | 同上 | 8 | 7 | 1（记录未提供，无法核对同源） | PASS |
| C plan_sha256 被篡改 | 同上 | 8 | 7 | 1（与 plan.json 不同源） | PASS |

A 的 notes 原文（节选）：

```text
…results.jsonl：执行证据 —— 非空结果行 1 行，身份可识别 1 行（唯一身份 1 个）
…results.jsonl：§7.1 完整性 —— 完整记录 0 行，存在契约缺口 1 行（缺口字段 7 个）；缺口不影响上面这一行的身份与计划归属核验，但该文件整体仍因缺口 FAIL
…results.jsonl：计划任务 1 个，身份命中（已执行）1 个，未命中 0 个
```

这张表正好回答复审的两个担心：7 个缺口确实还在 FAIL（第 4 列恒为 7），而真实跑过的行也确实被认出来了（身份列表非空、计划完成度 PASS、同源被单独判定）。

## 六、批次边界与计划同源审计（第五轮修复）

### 6.1 批次不可判定不是契约缺口

第四轮把「执行证据」与「契约完整性」分开后，`batch` 落进了一个尴尬位置：它既是可派生字段（目录名或记录提供），又会在目录名判不出批次时被标准化层记成「派生失败」。`check_results()` 随后按 `failures()` 提前报了一条 `… · batch` 的 FAIL —— 而实际上生产器记录里**根本没有** `batch` 字段，目录名也不含批次前缀时，谁都无法给出这个值。这等于把「无从推断」当成「记录缺字段」，属于假 FAIL。

修复（`preflight_experiment_data.check_results`）：

- `batch_undecidable = expected_batch is None`；此时 `undecidable = {"batch"}`，逐字段 FAIL 用 `set(failures) - undecidable` 减去它；
- `normalized.missing()` 的聚合循环保持 `if name in failures: continue`，所以 `batch` 不会混进「缺口字段」列表；
- 关键点：**不是**把未知批次当成某个已知批次，也不是静默吞掉 —— 批次判定仍由 `run_preflight()` 统一报 `UNKNOWN（覆盖｜目录名｜可判定的批次｜无法判定）`，并且 notes 里不会出现「批次 X：N 条记录」这类假归属；
- 目录名能判定批次时（`expected_batch != None`），批次照旧逐条比对，覆盖检查照旧执行；
- 其他字段的失败一条都不抑制 —— 测试里 7 个不可恢复字段仍逐字段 FAIL。

新增全流程用例 `test_flow_unknown_batch`：临时 `runs/` 下建一个不匹配任何 `BATCH_PREFIXES` 的目录，用真实 `run_one()` 产出记录（记录里没有 batch），经 `run_preflight()` 完整跑一遍，断言 UNKNOWN 存在、无 `· batch` FAIL、7 个缺口 FAIL 仍在、没有把未知批次记成已知批次。

### 6.2 plan_seq 必须按身份比，不能只查形态

冻结设计 §5.3 里 `plan_seq` 是计划中的**全局连续序号**，用于顺序审计；`seed`/`scheme`/`algorithm_version`/`plan_sha256` 用于核对结果行与计划文件是否同源。第四轮只核了 `plan_sha256`，其余四个字段只在 `check_plan()` 里核计划自身 —— 结果行的这四个值其实没人看。

修复：`check_results()` 在拿到 plan 后先建索引

```text
plan_task_by_identity[(plan.experiment_id, block.sample_id, task.group, task.repeat_index)] = task
```

然后按每条结果行的身份取任务：

| 字段 | 预期值来源 | 不一致时的判定 |
| --- | --- | --- |
| `plan_seq` | 该身份对应任务的 `plan_seq`（不是「是不是整数」「在不在 0..N−1」「连不连续」） | FAIL `… · plan_seq`，理由「结果行的执行序号与计划任务不一致，执行顺序无法审计」 |
| `seed` | `plan["seed"]` | FAIL `… · seed`，理由「不同源」 |
| `scheme` | `plan["scheme"]` | FAIL `… · scheme`，理由「不同源」 |
| `algorithm_version` | `plan["algorithm_version"]` | FAIL `… · algorithm_version`，理由「不同源」 |
| `plan_sha256` | `plan["plan_sha256"]` | 沿用第四轮规则：null 与不一致各自独立 FAIL |

两条边界：记录**没给**这个字段（值为 None）时不重复报「不一致」——缺口已经由 §7.1 完整性那一路 FAIL 了；plan.json 不存在时整套同源审计不执行（legacy 只走 WARNING）。

### 6.3 负向测试：四种篡改各一条，外加对照组

每种篡改只改一个字段，其余字段（含 `plan_sha256`）保持正确，且都走 `run_preflight()` 全流程（计划 5 个任务全部真实跑完，计划完成度是 PASS，篡改只落在一行上）：

| 用例 | 篡改 | 期望 |
| --- | --- | --- |
| `test_flow_plan_audit_control` | 无 | 四个字段 0 FAIL、plan_sha256 0 FAIL、计划完成度 PASS、7 条缺口仍在 |
| `test_flow_plan_seq_tamper` | `plan_seq: 0 → 2`（整数、且在 0..4 范围内） | 1 条 `· plan_seq` FAIL，写明实际 2 / 预期 0 |
| `test_flow_seed_tamper` | `seed: 20261113 → 20261114` | 1 条 `· seed` FAIL |
| `test_flow_scheme_tamper` | `scheme → other_scheme_v9` | 1 条 `· scheme` FAIL |
| `test_flow_algorithm_version_tamper` | `algorithm_version → sha256-block-order-v2` | 1 条 `· algorithm_version` FAIL |

`plan_seq` 那条特意用「合法整数、合法范围」的值：如果实现只检查类型/范围/连续性，这个篡改会漏过去。

### 6.4 B0 legacy 改用真实 `b0-*` 目录

第四轮的 `test_partial_record_legacy_still_warning` 用的是 `case-plan-legacy/run`，目录名根本不触发 `is_b0_legacy()`，所以「legacy 例外」其实没被验证。第五轮换成真实目录名 `b0-remainder-20261113T013105Z`，并走完整流程（`test_flow_b0_legacy`）：

- 目录名按 `b0-` 规则识别为 legacy，`expected_batch_for()` 返回 `b0-legacy`；
- 没有 plan.json → 「计划」区域只有 1 条 WARNING，不是 FAIL；
- B0 文件未纳入清单 → 「清单覆盖」只有 WARNING（清单里另放一个无关说明文件，保证清单非空），不做清单哈希核对；
- `plan_sha256` 缺失只按 legacy 规则报 1 条 WARNING，没有 FAIL；
- 7 个契约缺口仍保留 FAIL，行仍计入执行证据。

## 七、可信批次来源与 plan.json 严格类型（第七轮修复）

### 7.1 清单必须是批次归属的可信来源

第七轮复审指出的第一个问题：`check_manifest()` 只返回 `declared_paths` / `failed_paths`，把清单里的 `batch` 列读丢；`run_preflight()` 于是直接拿目录名或记录自述决定批次。后果是：一个不匹配任何 `BATCH_PREFIXES` 的目录，只要文件里的记录统一写 `batch="main"`，就会被当成正式 main 批次计入覆盖统计——清单明明写着别的批次也拦不住。

修复后 `check_manifest()` 返回 `ManifestIndex(declared, failed, batches, batch_problems)`：

- `batches`：规范化相对路径 → 清单 `batch`（只收非空字符串）；
- `batch_problems`：该文件在清单里有条目但取不到可信 batch 的原因（`清单没有 batch 列` / `清单条目的 batch 为空`）；
- `declared` / `failed` 语义不变（清单覆盖与哈希/记录数核对仍按原规则）。

`decide_batch()` 改成七态，优先级是「清单是唯一可信来源，目录名用于确认，记录只做交叉验证」：

| 状态 | 触发条件 | 采用的批次 | 预检行为 |
| --- | --- | --- | --- |
| `directory` | 目录名可识别，且清单 batch 与之一致 | 目录名 | 逐行核对记录声明；覆盖核对照常 |
| `manifest` | 目录名不可识别，清单给出合法 batch | 清单值 | 逐行核对记录声明；覆盖核对照常 |
| `manifest-conflict` | 目录名可识别，但清单 batch 不同 | **None（不采用任何一个）** | 1 条文件级 FAIL「目录名与清单的批次一致」，不做覆盖核对，不写「批次 X：N 条记录」 |
| `manifest-missing` | 目录名可识别，清单没有该文件或没有 batch，且不是 B0 legacy | 目录名 | 1 条文件级 FAIL「清单给出该文件的 batch」；目录名推断不能补上缺失的清单证据 |
| `manifest-invalid` | 清单条目存在，但 batch 为空或清单没有 batch 列 | None | 1 条文件级 FAIL，实际值写清「没有 batch 列」或「为空」；不得回退到记录自述 |
| `untrusted-records` | 目录名不可识别、清单也没有批次，只有记录自述（合法或非法） | None | 1 条文件级 FAIL 并点名行号；记录自述不能把文件归入任何正式批次 |
| `missing` | 目录、清单、记录都没有批次信息 | None | 只报 UNKNOWN，不猜批次 |

两条关键语义：

- **目录不可识别不再等于失败**：清单给出合法 batch 时按清单归类（`test_flow_manifest_batch_fallback`），这是 `default_run_dir()` 的 `<ref时间>-<UTC时间戳>` 目录能继续工作的前提；``main-*`` / `suppl-*` / `b0-*` 前缀不是强制要求。
- **矛盾时不采纳任何一方**：`manifest-conflict` 的 `value` 是 `None`，因此既不会把记录错误计入 main，也不会写一条看似正常的覆盖结论；文件级 FAIL 已经点明两个来源。

记录声明仍在 `check_results()` 逐行核对：与可信批次不符 → 该行 FAIL，expected 文本按来源写成「batch=X（由清单给出）」「batch=X（由目录推断（清单缺 batch））」或「batch=X（由目录推断）」。同一文件内多个声明不再单独报「整体矛盾」——有可信来源时逐行冲突已经足够，无可信来源时由 `untrusted-records` 的文件级 FAIL 覆盖。

同一 run-dir 的 `plan.json` 与 `results.jsonl` 必须给出同一个清单批次：只声明了其中一个、或两者清单批次不同，都在 `plan.json · batch` 上报 FAIL（expected 写 results 的清单批次与「同一 run-dir 必须一致」）。

B0 legacy 例外保留：`b0-*` 目录没有清单条目时仍按 `directory` 处理，清单覆盖只报 WARNING，不因缺清单 batch 额外 FAIL。

### 7.2 plan.json 严格类型核验

第二个问题：`check_plan()` 只做普通数值比较，而 Python 里 `1.0 == 1`、`True == 1` 都为真，所以把 `plan_seq` 写成 `1.0` 或 `true`、把 `seed` 写成 `20261113.0`，都能通过计划同源与 `plan_sha256` 核对。

修复：`plan_schedule.plan_type_problems(plan)` 返回全部类型问题（不抛异常），在生成身份索引、身份唯一性 PASS、计划完成度 PASS **之前**由 `check_plan()` 判定：

| 位置 | 字段 | 要求 |
| --- | --- | --- |
| 顶层 | `schema_version` / `seed` / `round_count` | int，且 `not isinstance(value, bool)` |
| 顶层 | `sample_order` | 列表，每项非空字符串 |
| 顶层 | `blocks` | 列表，每项是对象 |
| 每个 block | `sample_id` | 字段存在时必须是非空字符串；缺失留给「计划结构」FAIL（不误报成类型错误） |
| 每个 block | `tasks` | 列表，每项是对象 |
| 每个 task | `plan_seq` / `round_index` / `repeat_index` | int，且不是 bool |
| 每个 task | `group` | 非空字符串 |

有问题时：恰好 1 条 FAIL，target 为 `… · 计划类型`，actual 列出前 5 条问题，reason 写明「等值比较不能证明类型」，并**中止**序号连续性、身份唯一性与身份集合核验（不再给出可能误导的 PASS）；`plan_sha256` 与清单哈希已由前面的核验独立判定，不因类型错误改变结论。

生产侧 `validate_plan()` 复用同一份 `plan_type_problems()`（`_check_plan_scalar_types` / `_check_plan_types` 只是取首条并抛 `PlanIntegrityError`），错误文案与原来一致；冻结计划矩阵与随机化顺序未改动。

### 7.3 第七轮新增全流程用例（12 个场景 / 46 项断言）

全部通过真实 `run_preflight()`，使用 tempfile + 合成记录/计划/清单：

| 用例 | 构造 | 期望 |
| --- | --- | --- |
| `test_flow_manifest_batch_fallback` | 未识别目录 + 清单 batch=main + 生产器记录无 batch | 批次按清单归入 main；无 `· batch` FAIL；覆盖核对执行并因 S1 不属于 main FAIL；无 UNKNOWN；7 个缺口仍在 |
| `test_flow_batch_unknown` | 未识别目录 + results.jsonl 不在清单 + 记录无 batch | 只报 1 条 UNKNOWN；无 `· batch` FAIL；「清单覆盖」点名该文件未纳入 |
| `test_flow_manifest_vs_directory_conflict` | 目录 `main-*` + 清单 `suppl-subj` | 1 条「批次」文件级 FAIL，actual 同时点名两个批次；不出现「批次 main」的覆盖结论 |
| `test_flow_manifest_batch_empty` | 清单 batch 为空串 | 1 条文件级 FAIL，actual 含「空」；不回退到记录自述 |
| `test_flow_manifest_without_batch_column` | 清单没有 `batch` 列 | 1 条文件级 FAIL，actual 含「没有 batch 列」；不报「批次 main」 |
| `test_flow_plan_manifest_batch_mismatch` | plan 清单 suppl-subj、results 清单 main | 恰好 1 条 `plan.json · batch` FAIL，expected 含 main，actual 含 suppl-subj |
| `test_flow_plan_type_float_plan_seq` | `tasks[0].plan_seq: 1 → 1.0`，重算 `plan_sha256` | 恰好 1 条「计划类型」FAIL，actual 含 `plan_seq` 与「类型不正确」；无清单/plan_sha256 FAIL；无身份唯一性 PASS |
| `test_flow_plan_type_bool_plan_seq` | `plan_seq → true` | 同上 |
| `test_flow_plan_type_float_round_index` | `tasks[0].round_index: 2 → 2.0` | 同上（字段名 `round_index`） |
| `test_flow_plan_type_bool_round_index` | `round_index → true` | 同上 |
| `test_flow_plan_type_float_seed` | 顶层 `seed: 20261113 → 20261113.0` | 同上（字段名 `seed`） |
| `test_flow_plan_type_float_round_count` | 顶层 `round_count: 5 → 5.0` | 同上（字段名 `round_count`） |

每个类型负向用例都重新计算 `plan_sha256` 并写入清单，因此清单哈希不会提前拦截；断言「无 `plan_sha256` FAIL」证明预检是自己发现了类型错误。`test_flow_plan_identity_control` 仍断言正常整数计划没有「计划类型」FAIL。

### 7.4 生产侧身份唯一性后置断言：本轮未加

任务书把 `build_schedule()` 的身份唯一性后置断言标为「可考虑」。本轮选择**不加**，理由：

- `build_schedule()` 现有组别去重与任务生成逻辑、`validate_plan()` 的完整校验、生产器发起模型请求前的计划验证都保持不变；冻结矩阵与随机化顺序不受影响；
- 消费侧 `check_plan()` 已对重复身份 fail-closed（第六轮），预检会在数据进入统计前拦下；
- 在生产侧加断言需要构造一个能通过既有生成逻辑的重复身份夹具（或打桩），测试成本与改动面都超过本轮范围，容易为了断言而重构生产路径。

如果负责人要求生产侧也 fail-closed，应作为独立变更提案（含对既有生成器测试夹具的影响评估），不与本轮消费侧修复混在一起。

## 八、计划失败状态必须贯穿结果侧（第八轮修复）

### 8.1 修的是什么缺陷

第八轮复审发现：`check_plan()` 对 `plan_type_problems()`、结构缺失、哈希不自洽只报 FAIL，随后仍然 `return plan`。`run_preflight()` 把这份已被判定为非法的计划原样传给 `check_results()`，后者内部的 `plan_identity_map()` 不再检查类型，照常建立身份索引。于是一份 5 任务计划配上 5 条合法结果，会同时得到「计划类型 FAIL」和「计划任务 5 个，身份命中 5 个」「计划完成度 PASS」——计划侧说它非法，结果侧却按它算完成度。

这不是文案问题：完成度 PASS 会进入覆盖统计与盲评前的合格判定，等于让类型错误的计划绕过消费侧防线。

### 8.2 四态 `PlanCheck` 与结果侧闸门

`check_plan()` 返回 `PlanCheck(state, plan=None, detail="")`；`untrusted` 由第九轮的清单闸门在 `run_preflight()` 中赋值，`check_plan()` 自身仍只产出前三种：

| 状态 | 判定条件 | 结果侧行为 |
| --- | --- | --- |
| `missing` | plan.json 不存在 | 保留 legacy 路径：缺 `plan_sha256` 只报 WARNING；不建索引、不做完成度 |
| `valid` | 哈希自洽、类型合法、结构完整、plan_seq 连续、repeat_index 一致、身份唯一、身份集合符合冻结设计 | 建身份索引；核对成员关系与同源字段；给出计划完成度 PASS/FAIL |
| `invalid` | 上述任一项失败（哈希不自洽 / 类型非法 / 结构不全 / plan_seq 断裂 / repeat_index 不一致 / 重复身份 / 身份集合不符） | 保留「计划」区域原始 FAIL 与 `detail`；不建索引；不做成员关系、同源字段、计划完成度核验；结果侧写一条说明；**不**走 legacy WARNING |
| `untrusted` | 文件存在，但 SHA-256/大小/记录数与清单不一致，或正式 plan.json 未纳入清单（第九轮新增） | 保留「清单」/「清单覆盖」区域的原始 FAIL；不再调用 `check_plan()`；不建索引；不做成员关系、同源字段、计划完成度核验；**不**走 legacy WARNING |

`run_preflight()` 把 `PlanCheck` 连同 `plan_check` 关键字参数传给 `check_results()`。后者只在 `valid` 时调用 `plan_identity_map()`；行内的计划成员关系、`plan_seq`/`seed`/`scheme`/`algorithm_version` 同源比对全部以 `plan_usable` 为前置条件。legacy WARNING 的触发条件从「plan is None」改为「plan 状态是 missing」，因此无效计划不会被误判成「这个目录没有 plan.json」。

无效计划的结果侧说明固定写为：`plan.json 校验失败（{detail}），结果侧未建立计划身份索引，未做计划成员关系、同源字段与计划完成度核验`。其他与计划无关的字段检查（29 字段缺口、批次、身份四元组、重复行等）继续执行，不会被这条闸门整体跳过；但任何依赖无效计划索引的正向结论都不会出现。

`check_results()` 的 `plan_check` 是仅关键字参数，不传时保持旧行为（plan 非 None 即按 valid），避免影响既有的直接调用与测试。

### 8.3 第八轮新增全流程用例（2 个场景 / 14 项断言）

| 用例 | 构造 | 期望 |
| --- | --- | --- |
| `test_flow_invalid_plan_full_plan_seq` | 5 条完整结果 + `tasks[1].plan_seq = 1.0`，重算 `plan_sha256` | 1 条「计划类型」FAIL；无「计划完成度」PASS；无「身份唯一性」PASS；notes 无「身份命中」；无 legacy `plan_sha256` WARNING；notes 写明未建索引 |
| `test_flow_invalid_plan_full_seed` | 5 条完整结果 + 顶层 `seed = float(seed)`，重算 `plan_sha256` | 同上 |

对照组 `test_flow_plan_identity_control` 追加一条断言：正常整数计划 + 5 条完整结果必须拿到「计划完成度 PASS（全部有结果）」，证明闸门没有把合法计划一起挡掉。两个负向用例都用完整 5 条结果，因此任何「身份命中」或「计划完成度 PASS」都只能来自被错误放行的无效计划，不能归因于结果缺失。

## 九、清单完整性失败必须阻断下游正向结论（第九轮修复）

### 9.1 修的是什么缺陷

`check_manifest()` 会把 SHA-256、文件大小、记录数校验失败的路径放进 `ManifestIndex.failed`。但 `run_preflight()` 只在「文件级清单覆盖」检查时使用这个集合：文件在 `failed` 中就不补「清单覆盖 PASS」。之后主流程仍继续调用 `check_plan()`、`check_results()`，把结果身份累加进 `by_batch`，再做批次覆盖与 165 份总量汇总。

于是出现一种自相矛盾的报告：清单区域已经 FAIL「SHA-256 与清单不符」，数据区域却仍可能给出「主批次 140 覆盖 PASS」「计划完成度 PASS」「生成任务总数 165 PASS」。行内字段再完整、身份再齐全，也不能证明这份文件就是清单登记的那一份——哈希核验是不可替代的文件级证据。

### 9.2 文件信任闸门

修复后，`文件是否存在`、`文件内容是否通过清单核验`、`文件内容本身是否合法` 是三件独立的事。`run_preflight()` 在进入计划/结果核验前先算两个信任位：

```python
plan_trusted = plan_resolved not in failed and (
    plan_resolved in declared or legacy)
results_trusted = results_resolved not in failed and (
    results_resolved in declared or legacy)
```

| 文件状态 | 信任位 | 下游行为 |
| --- | --- | --- |
| 不存在 | — | plan：`missing` + legacy WARNING；results：目录 FAIL（有计划无结果） |
| 存在、在清单中、未进 `failed` | 可信 | 正常核验 |
| 存在、在清单中、进 `failed` | 不可信 | 保留清单 FAIL；计划进入 `untrusted`，结果只做诊断 |
| 存在、正式文件但不在清单中 | 不可信 | 保留「清单覆盖」FAIL；计划进入 `untrusted`，结果只做诊断 |
| B0 legacy 目录、未纳入清单 | 可信（设计例外） | 继续按原 legacy 规则，不因缺少清单条目被误伤 |
| B0 legacy 目录、已纳入清单 | 按清单核验 | 通过则正常可信；进入 `failed` 则与正式文件一样不可信，legacy 不能豁免哈希/大小/记录数失败 |

计划侧：不可信时不再调用 `check_plan()`，直接构造 `PlanCheck(PLAN_STATE_UNTRUSTED, None, reason)`。清单区域已经报过 FAIL，这里不重复报，只写一条 notes「未通过可信清单完整性核验，未进行计划内容核验」。因此不会出现「计划类型 PASS」「身份唯一性 PASS」「身份集合 PASS」，也不会被误判成 `missing` 而报 legacy `plan_sha256` WARNING。

结果侧：`check_results(..., trusted=False)` 仍逐行解析、仍报告字段/身份/批次诊断（便于定位问题），但：

- 不返回身份（返回空列表），调用方无法把它累加进 `by_batch`；
- 不产生「计划完成度」PASS/FAIL；
- 不产生「身份命中」结论；
- 覆盖核对拿到空身份集合，主批次会得到 FAIL（缺 140），而不是 PASS；
- 165 份总量只累加可信身份，通常得到 FAIL（总数不符），不会得到 PASS；
- 文件级 notes 明确写「已解析 N 行仅用于诊断；身份不计入 by_batch、覆盖与 165 份总量，不产生计划完成度结论」。

`check_results()` 的 `trusted` 是仅关键字参数，默认 `True`，直接调用与既有测试路径保持旧行为。

### 9.3 第九轮新增全流程用例（5 个场景 / 23 项断言）

全部走真实 `run_preflight()`；主实验夹具用 7 样本 × A/B/C/D × 5 轮 = 140 条真实 `run_one()` 记录 + 同源 plan.json + 清单，确保对照组能拿到真实的覆盖与完成度 PASS。

| 用例 | 构造 | 期望 |
| --- | --- | --- |
| `test_flow_untrusted_results_tamper` | 140 条完整主实验记录，改第 0 行 `answer`，不刷新清单 | 清单 SHA/大小 FAIL；无主批次覆盖 PASS；无计划完成度 PASS；无总量 PASS；notes 写明身份不计入汇总 |
| `test_flow_untrusted_plan_tamper` | 140 条完整记录，改 plan 的 `created_at` 并重算 `plan_sha256`，不刷新清单 | 清单 SHA/大小 FAIL；无身份唯一性 PASS；无「身份命中」；无计划完成度 PASS；不误报 legacy WARNING；notes 写明未做计划内容核验 |
| `test_flow_trusted_control` | 内容与清单完全一致的 140 条主实验 | 140 行齐全；计划完成度 PASS；主批次 140 覆盖 PASS；无清单闸门诊断 |
| `test_flow_untrusted_results_not_in_manifest` | 清单只登记 plan.json，results.jsonl 不登记（目录名仍为 `main-*`） | 清单覆盖 FAIL；无主批次覆盖 PASS；无计划完成度 PASS；无总量 PASS；notes 写明未纳入可信清单 |
| `test_flow_legacy_not_untrusted` | `b0-*` 目录 + 无 plan.json + 清单只放无关说明文件 | 不被判为清单不可信；身份仍计入 `b0-legacy`；清单覆盖只 WARNING 不 FAIL |

## 十、B0 清单信任边界与 plan/results 批次冲突闸门（第十轮修复）

### 10.1 修的是什么缺陷

第九轮把「清单完整性失败」接进了计划/结果信任位，但留下两个洞。

**其一：B0 legacy 用逻辑或短路了清单失败。**

```python
plan_trusted = legacy or (plan_resolved in declared and plan_resolved not in failed)
results_trusted = legacy or (results_resolved in declared and results_resolved not in failed)
```

`legacy=True` 时右半边根本不求值。B0 文件一旦纳入清单，即使 SHA-256、大小或记录数已经对不上，仍会被当作可信文件：results 照样进 `by_batch`、拿批次覆盖 PASS，plan 照样进 `check_plan()`、建身份索引。冻结设计的 B0 例外只覆盖「未纳入清单的 legacy 文件」，不能覆盖清单已经明确报告的完整性失败。

**其二：plan/results 清单批次冲突只报一条 FAIL。**

`run_preflight()` 已经发现同一 run-dir 的 plan.json 与 results.jsonl 清单 batch 不一致，并写明「目录属于哪个批次无法唯一确定」，但冲突没有传给后续调用链。之后仍可能：plan 被判可信、建立身份索引、产生计划完成度 PASS；results 按自己的 batch 继续产生覆盖 PASS；某些组合下再产生总量 PASS。一条 FAIL 与一串 PASS 同时出现，等于没有闸门。

### 10.2 两条闸门

**B0 信任边界。** 信任位改成「先排除 failed，再看声明或 legacy」：

```python
plan_trusted = plan_resolved not in failed and (
    plan_resolved in declared or legacy)
results_trusted = results_resolved not in failed and (
    results_resolved in declared or legacy)
```

| B0 文件状态 | 信任位 | 下游行为 |
| --- | --- | --- |
| 未纳入清单 | 可信（设计例外） | 按原 legacy 规则，不报清单覆盖 FAIL |
| 已纳入清单且校验通过 | 可信 | 与正式文件相同，正常核验 |
| 已纳入清单且进入 `failed` | 不可信 | 保留清单 FAIL；plan 进入 `untrusted`，results 只做诊断 |

不要求所有 B0 文件必须纳入清单；冻结设计的 B0 规则未改。

**plan/results 批次冲突。** 新增批次态 `BATCH_STATE_PLAN_RESULTS_CONFLICT = "plan-results-conflict"`。检测条件与动作：

1. plan.json 存在，且已纳入清单；清单 batch 本身合法；results 有清单 batch，且两者不同；
2. 报一条 `FAIL｜清单｜{label}/plan.json · batch`，expected 写 `batch={results 的清单 batch}（与 results.jsonl 的清单批次一致）`，actual 同时点名两个 batch；
3. 把 `BatchDecision` 覆盖为 `plan-results-conflict`（value 为 `None`），不采用 results 的 batch；
4. plan 不再进入 `check_plan()`，直接 `PlanCheck(PLAN_STATE_UNTRUSTED, None, detail)`，不建身份索引；
5. results 以 `trusted=False`、`untrusted_reason=detail` 进入 `check_results()`：仍解析行、报字段诊断，但返回空身份，不做成员关系/同源/完成度；
6. 跳过 `by_batch` 与 `check_coverage()`，并写 notes「批次归属冲突，未做覆盖核对，身份不计入 165 份总量」；
7. 冲突是 FAIL，不降级为 UNKNOWN 或 WARNING。

冲突只切断该 run-dir；其他目录照常核验。清单批次一致的正常目录不受影响。

### 10.3 第十轮新增全流程用例（4 个场景 / 24 项断言）

全部走真实 `run_preflight()`，合成数据写入临时目录。B0 夹具是完整 10 条（S5/S7 × B0 × 5 轮）+ 同源 10 任务 plan.json；冲突夹具是完整 140 条主实验记录 + 同源 140 任务 plan.json。

| 用例 | 构造 | 期望 |
| --- | --- | --- |
| `test_flow_b0_declared_results_tamper` | B0 的 plan/results 都登记进清单（各 10 条），只改第 0 行 `final_output.answer`，不刷新清单；随后恢复原文作为同夹具对照 | 清单报 results.jsonl SHA/大小 FAIL；无 B0 批次覆盖 PASS；无计划完成度 PASS；不误报成清单外 legacy WARNING；notes 写明 results 未通过核验；恢复后对照拿到 legacy WARNING 与完成度 PASS |
| `test_flow_b0_declared_plan_tamper` | B0 的 plan/results 都登记，改 plan 的 `created_at` 并重算 `plan_sha256`，不刷新清单 | 清单报 plan.json SHA/大小 FAIL；无身份唯一性 PASS；无「身份命中」；无计划完成度 PASS；notes 写明 plan 未通过核验且未做内容核验 |
| `test_flow_plan_results_batch_conflict_full` | 140 条完整主实验 + 140 任务 plan；清单把 plan 的 batch 写 `suppl-subj`、results 写 `main`，两份文件哈希/大小/records 都正确 | 恰一条 `plan.json · batch` FAIL，expected 点名 main、actual 点名 suppl-subj；无主批次覆盖 PASS；无完成度 PASS；无总量 PASS；无身份唯一性/身份集合 PASS；notes 无「身份命中」；无 UNKNOWN；notes 写明 plan-results-conflict、计划不进入索引、结果不计入 165 |
| `test_flow_plan_results_batch_conflict_control` | 同一 140 条夹具，清单 plan/results 都为 `main` | 计划完成度 PASS；main 批次 140 覆盖 PASS；无 plan-results-conflict 说明（证明闸门没有误伤正常目录） |

第七轮已有的单行 `test_flow_plan_manifest_batch_mismatch` 继续保留：它仍要求恰一条 `plan.json · batch` FAIL、expected 含「与 results.jsonl 的清单批次一致」、actual 点名两个 batch。新增的完整 140 条用例补上了「冲突不得产生下游 PASS」这一半。

## 十一、清单 batch 缺失/为空时的批次配对闸门（第十一轮修复）

### 11.1 修的是什么缺陷

第十轮的 `plan-results-conflict` 只覆盖「两侧清单 batch 都合法、但互不相同」。另两种更基础的情况仍然漏过去了。

**plan.json 已纳入清单，但清单 batch 为空，或整张清单没有 batch 列。** `check_manifest()` 会写 `batch_problems`，`run_preflight()` 也报一条 FAIL；但信任位只看声明与 `failed`：

```python
plan_trusted = plan_resolved not in failed and (plan_resolved in declared or legacy)
```

`declared` 为 True、`failed` 为空 → `plan_trusted=True`。于是 SHA-256、大小、records 都正确（文件本来也确实没被改）的 plan.json 仍进 `check_plan()`：建立身份索引、给出「N 个任务身份唯一」PASS；results 一侧的清单 batch 合法，`check_results()` 照常做计划成员关系与同源核验，拿到计划完成度 PASS，并把 140 个身份交给 `by_batch`，再拿到「批次 main」覆盖 PASS 与可信总量 PASS。一条清单 FAIL 与整串正向 PASS 同时出现。

**results.jsonl 已纳入清单，但清单 batch 为空。** 同样地，`results_trusted` 仍为 True，完成度 PASS 仍会出现——虽然 `decide_batch()` 已经把这个文件判成 `manifest-invalid`、`expected_batch=None`，覆盖核对被跳过，但「plan 与 results 属于同一批次」这个前提并没有被显式切断。

一句话：清单对 batch 的错误只被当成「文件级提示」，没有被当成「这份文件属于哪个批次」的证据缺口。

### 11.2 闸门：把配对前提变成显式状态

新增两个配对问题变量，并把它们与第十轮的冲突合并成一个 `batch_pairing_problem`：

| 变量 | 触发条件 | 说明 |
| --- | --- | --- |
| `plan_results_batch_conflict` | 两侧清单 batch 合法但不同（第十轮） | `plan.json 清单 batch='suppl-subj'，results.jsonl 清单 batch='main'` |
| `plan_batch_missing_problem` | plan 已声明，且 `plan_manifest_problem` 非空 | `plan.json 的清单 batch 不可用（清单条目的 batch 为空 / 清单没有 batch 列）` |
| `results_batch_missing_problem` | results 已声明，且 `manifest_problem` 非空 | `results.jsonl 的清单 batch 不可用（…）` |

`plan_manifest_problem` 由现有 `check_manifest()` 提供（清单条目 batch 为空 / 清单没有 batch 列），不是本轮新造的判定。plan 一侧仍保留原有那条 `FAIL｜清单｜{label}/plan.json · batch`，不因为后面切断了核验就删掉证据。

下游动作：

1. **计划**：任一侧配对问题存在时，plan 直接 `PlanCheck(PLAN_STATE_UNTRUSTED, None, problem)`，不调用 `check_plan()`，不建身份索引。这里刻意复用第九轮的第四态 `untrusted`，而不是返回 `None`：`None` 在调用链里表示「没有 plan.json」的 legacy 目录，会把已声明文件误报成清单外例外。
2. **结果**：`check_results(..., trusted=(results_trusted and batch_pairing_problem is None), untrusted_reason=batch_pairing_problem)`。仍逐行解析、仍报字段/身份/批次诊断，但返回空身份，不做成员关系、同源与完成度。
3. **覆盖与总量**：`batch_pairing_problem` 非空时既不 `by_batch.extend`，也不调用 `check_coverage()`；notes 写明「批次归属不可确认（原因），未做覆盖核对，结果身份不计入 165 份总量」。因此 main 覆盖 PASS、可信生成总量 PASS 都不会出现。
4. **不新增批次态、不降级**：清单 batch 缺失/为空本身仍是明确 FAIL（`decide_batch()` 的 `manifest-missing` / `manifest-invalid`），不引入 UNKNOWN 或 WARNING 来「表示不确定」。第十轮的 `plan-results-conflict` 批次态保持原样，只用于冲突。
5. **范围**：闸门只作用于当前 run-dir。其它清单 batch 合法且一致的目录不受影响（11.3 的正常对照）。

### 11.3 第十一轮新增全流程用例（5 个场景 / 36 项断言）

全部调用真实 `run_preflight()`，数据在 `tempfile` 临时目录里合成。主实验夹具沿用第十轮：`run_experiment.run_one()` 真实产出 140 条记录（7 样本 × A/B/C/D × 5 轮）+ 同源 140 任务 `plan.json`；B0 夹具是完整 10 条（S5/S7 × B0 × 5 轮）。每个场景只改清单的 batch 字段。

| 用例 | 构造 | 期望 |
| --- | --- | --- |
| `test_flow_plan_manifest_batch_empty_blocks_pairing`（场景 A） | 两份文件都入清单，SHA/大小/records 全对；plan 的 batch 写空串，results 写 `main` | 恰一条 `plan.json · batch` FAIL 且写明「空」；无身份唯一性/身份集合/计划完成度/main 覆盖/总量 PASS；无 UNKNOWN、无 batch WARNING；notes 写明「未进行计划内容核验」「plan.json 的清单 batch 不可用」「批次归属不可确认」 |
| `test_flow_manifest_without_batch_column_blocks_pairing`（场景 B） | 同上，但清单整表不含 `batch` 列 | 同上，FAIL 写明「没有 batch 列」；结果侧同样不产生覆盖/总量/完成度 |
| `test_flow_results_manifest_batch_empty_blocks_pairing` | plan 的 batch=`main`，results 的 batch 写空串 | 恰一条 `批次` 区域 FAIL（`decide_batch()` 的 manifest-invalid）；无计划完成度/main 覆盖/总量 PASS；notes 写明「results.jsonl 的清单 batch 不可用」「未做覆盖核对」 |
| `test_flow_plan_batch_pairing_control`（场景 C） | 两侧 batch 都是 `main` | 身份唯一性 PASS；批次 main 的「140 个身份」覆盖 PASS；计划完成度 PASS；notes 不含「批次归属不可确认」 |
| `test_flow_b0_legacy_not_declared_after_pairing_gate`（场景 D） | 完整 10 条 B0 + 清单只登记一个 `docs/` 占位文件（B0 两文件都不声明） | 不含「未通过可信清单完整性核验」「批次归属不可确认」；notes 仍有「批次 b0-legacy：10 条记录」；恰一条 b0-legacy WARNING；无清单覆盖 FAIL；完成度 PASS——证明 B0 的清单外例外没有被本轮收窄 |

场景 C 的「身份集合」说明：`run_preflight()` 不向 `check_plan()` 传 `expected_identities`（计划侧集合核对属于 `check_coverage()` 的职责），所以计划区域本来就不会单独出「身份集合」PASS。测试里改为断言「计划身份集合没有被判 FAIL」+「批次 main 的 140 身份集合覆盖 PASS」，避免造一条工具根本不产生的 PASS。

先红后绿：把 `preflight_experiment_data.py` 临时回退到 `1911daf`（只回退这一个文件、测试保留），新增用例有 11 条断言失败（无闸门时身份唯一性/完成度/main 覆盖/总量 PASS 都还在）；恢复修复后 487 项全过。

### 11.4 与 B0 冻结规则的关系

未纳入清单的 B0 继续走 legacy：不报清单覆盖 FAIL、不因「没在清单里」被判不可信（场景 D）。B0 文件一旦被声明，就必须和正式文件一样有合法 batch；缺 batch 时同样被本轮闸门切断——这与第十轮「legacy 只豁免未声明」的口径一致，不修改 B0 冻结定义。

## 十二、第六轮的两处修复（历史记录：批次四态 + 计划身份唯一性）

> 说明：本节记录第六轮当时的批次四态设计与计划身份唯一性修复。其中批次部分的设计（含 `records` 态）已在第七轮被第七节的「清单为可信来源」取代，读批次口径请直接看第七节；计划身份唯一性部分第七轮保留不变。

### 11.1 批次判定必须分四态（历史设计，已被第七节取代）



第六轮复审指出的第一个问题：批次的三种"没有值"被混成了一件事。`_record_batches()` 只把**非空字符串**算作声明，所以 `batch: ["main"]`、`batch: 3`、`batch: true`、`batch: "  "` 全都被丢掉；随后「一个值 → 采用，零个或多个值 → None」的规则又把**声明冲突**压成 None。`check_results()` 只看记录 `batch` 非空且与目录一致，`normalize_record()` 更是直接用目录推断值构造视图、根本不看原始行的声明。结果是：同一文件里两行分别声明 `main` 与 `suppl-subj` 时，预检只报一条 UNKNOWN、覆盖核对被静默跳过；`batch` 写成 list 时同样是 UNKNOWN，而不是「记录写错了」。

修复（`result_schema.derive_batch` + `preflight_experiment_data.decide_batch`）：

| 状态 | 触发条件 | 预检行为 |
| --- | --- | --- |
| `directory` | 目录名带已知批次前缀 | 值取目录名；逐行比对记录声明；覆盖核对照常执行 |
| `records` | 目录判不出，且文件内**所有**记录一致声明同一个非空字符串 | 值取该声明；逐行比对；覆盖核对照常执行 |
| `missing` | 目录判不出，且没有任何记录写 `batch` | 只报 1 条 UNKNOWN（"目录名与记录都没有给出批次"），不做覆盖核对 |
| `invalid` | 有记录的 `batch` 不是非空字符串（list/dict/int/bool/空串/纯空白） | 每个非法值一行 FAIL（点名行号与 `repr`），不报 UNKNOWN，不做覆盖核对 |
| `conflict` | 同一文件出现 ≥2 个**不同**的非空字符串 | 1 条文件级 FAIL（点名全部行号与值），不报 UNKNOWN，不做覆盖核对 |

两条容易搞反的边界：

- **目录值优先**。目录能判出批次时，记录声明与目录不一致 → 该行独立 FAIL（`记录批次与所属目录/文件不一致`），不会被当成"记录没说话"而落到 `missing`；
- **不重复报 ≠ 不报**。`failed_fields` 里会减掉 `batch`，是因为行级检查已经逐行核过它；若行级没抓到（非法值、与目录冲突），文件级 FAIL 仍会补齐。测试里对每种情形都断言了「恰好 N 条」，既没有漏报也没有重复报。

`normalize_record()` 相应改为调 `derive_batch(row, directory_batch=batch)`：`results["batch"]` 的 `ok`/`value`/`detail` 现在来自四态判定，不再无条件信任目录值。

### 11.2 计划身份唯一性：`set` 与 `dict` 都会吃掉重复

第二个问题出在计划侧。`plan_identities()` 用 **set 推导** `(experiment_id, sample_id, group, repeat_index)`，两个任务写成同一身份时集合自然折叠成一个；`check_results()` 的 `plan_task_by_identity` 用 **dict 覆盖**，后者悄悄盖掉前者。于是 plan.json 里出现重复身份时：身份集合看起来完整、计划完成度可能 PASS、结果行只要匹配其中任意一条也能通过 —— 可执行顺序（`plan_seq`）已经无法审计了，却查不出来。

修复（`plan_schedule` 新增三个函数，`plan_tasks()`/`plan_identities()` 未改动）：

| 函数 | 语义 |
| --- | --- |
| `plan_identity_entries(plan)` | 逐任务展开成 `entries`，每条带 `identity`/`plan_seq`/`repeat_index`/`round_index`/`where`（`blocks[i].tasks[j]`）；缺 `experiment_id`/`blocks`/`sample_id`/`group`/`repeat_index` 时记一条结构问题 |
| `plan_identity_duplicates(entries)` | 只返回出现 ≥2 次的身份及其全部条目（首次出现顺序） |
| `plan_identity_map(plan)` | `identity → [entries]`；结构问题非空时返回 `(None, problems)`，**不生成**看似完整的索引 |

`check_plan()` 现在先看结构，再逐身份查重：

- 结构不完整 → 一条 `… · 计划结构` FAIL，列出前 5 处问题（如 `blocks[0] 缺少 sample_id`），并**中止**序号与唯一性核验（没有可信索引时不再往下比）；
- 每个重复身份 → 一条 `… · 身份唯一性` FAIL，actual 写「重复 N 次：[四元组]；plan_seq=0、1」，reason 写「同一身份对应多个计划任务（blocks[i].tasks[j]…），结果行匹配其中任意一条都不能证明计划合法，执行顺序不可审计」；
- 无重复 → 一条 PASS「N 个任务身份唯一」。

结果侧同步收紧：`plan_ids` 改由 `set(plan_map)` 得到（结构坏时为 None），不再直接调 `plan_identities()`；重复身份**不建** `plan_task_by_identity` 索引，所以不会出现「结果行恰好匹配重复计划中的一条」就被洗成合法的情况。

### 11.3 负向用例（第 7.7 节，八个场景 / 35 项断言，第六轮记录）

> 以下为第六轮当时的用例与断言；第七轮已改写其中依赖 `records` 态的用例（`test_flow_batch_records_consistent` 改为清单 suppl-subj 一致，`test_flow_batch_conflict` 改为清单 main + 第 2 行 suppl-subj），当前行为以 7.3 为准。

全部走 `run_preflight()` 全流程：真实 `run_one()` 产出记录 → 逐行改写/删除 `batch`（或改写计划任务）→ 写清单 → 预检。

| 用例 | 构造 | 期望 |
| --- | --- | --- |
| `test_flow_batch_records_consistent` | 目录判不出批次，两行一致声明 `suppl-subj` | 无 `· batch` FAIL、notes 写「批次 suppl-subj：2 条记录」、无 UNKNOWN、7 个缺口 FAIL 仍在（说明 `records` 态确实被采用，不是被忽略） |
| `test_flow_batch_conflict` | 第 1 行 `main`、第 2 行 `suppl-subj` | 恰好 1 条 FAIL，点名两个值与「第 1 行/第 2 行」；无 UNKNOWN；7 个缺口仍在 |
| `test_flow_batch_invalid` | 五行分别写 `["main"]`、`{"batch":"main"}`、`3`、`true`、`"  "` | 5 条 FAIL，均指向「非空字符串」；无 UNKNOWN；7 个缺口仍在 |
| `test_flow_batch_directory_conflict` | 目录 `main-*`，第 2 行声明 `suppl-subj` | 恰好 1 条 FAIL，expected 为「batch=main（由目录推断）」，target 以 `:2 · batch` 结尾 |
| `test_flow_batch_directory_derived` | 目录 `main-*`，两行都不写 `batch` | 无 `· batch` FAIL、notes 写「批次 main」（目录推断仍有效） |
| `test_flow_plan_duplicate_identity` | `tasks[1]` 的 `repeat_index`/`round_index` 改成与 `tasks[0]` 相同（重算 `plan_sha256`），只跑 1 行 | 恰好 1 条「身份唯一性」FAIL，写「重复 2 次」「plan_seq=0、1」「执行顺序不可审计」；无 PASS；notes 提示重复身份未建索引 —— **一行匹配也照样 FAIL** |
| `test_flow_plan_structure_broken` | 删掉 `blocks[0]["sample_id"]` | 恰好 1 条「计划结构」FAIL 并点名 `sample_id`；notes 记结构不完整；无 PASS |
| `test_flow_plan_identity_control` | 正常五轮计划 | 恰好 1 条 PASS「5 个任务身份唯一」，无重复 FAIL、无结构 FAIL |

### 11.4 B0 legacy 的合成数据改用 S5/S7 与 B0 组

原来的 B0 用例用 `S2 × D` 造"看起来像 legacy"的记录，身份与目录规则的证据混在一起（`b0-*` 目录只说批次，不说样本）。第六轮改成两行真实记录 `S7 × B0 × repeat 0` 与 `S5 × B0 × repeat 1`，断言「身份可识别 2 行」「批次 b0-legacy：2 条记录」「记录身份是 S5×B0」，其余 legacy 结论（无 plan.json 只 WARNING、清单缺失只 WARNING、`plan_sha256` 恰 1 条 WARNING、7 个缺口仍 FAIL）保持不变。
## 十三、代码修改清单

| 文件 | 变更 | 要点 |
| --- | --- | --- |
| `experiments/graduation/result_schema.py` | 新增，第六轮后 848 行（+88 / −10） | §7.1 标准化层：契约表、derive_condition、parse_prompt_text/messages_sha256/read_prompt_messages、normalize_record/Normalized。stdlib only，不 import 产品代码。第六轮新增 `BatchResult` 与 `derive_batch()`：`batch` 分 `OK`/`FROM_DIRECTORY`/`MISSING`/`INVALID`/`CONFLICT` 四类，`normalize_record` 不再无条件信任目录值 |
| `experiments/graduation/preflight_experiment_data.py` | 第三轮 +92 / −7，第四轮 +74 / −18，第五轮 +69 / −4，第六轮 +207 / −70，第七轮 +210 / −75，第八轮 +77 / −16，第九轮 +64 / −19，第十轮 +57 / −19，第十一轮 +43 / −9（现 1428 行） | 引入标准化层（`:55`）；`CONTRACT_BASIS`；`check_results` 生产器分支：标准化 → 派生失败逐行 FAIL → 缺口按字段聚合 FAIL → **不再整行跳过**（第四轮删掉 `continue`，改走 `normalized.view()`）→ 视图参与原有类型/批次/身份/重复/计划成员/同源校验；`null_ok`（设计值就是 null 字段不算缺口）；`failed_fields` 避免与按字段缺口重复报；`plan_sha256` 在 plan.json 存在时 null 与不一致各自 FAIL（`:523-541`）；`result_rows`/`identified_rows`/`derived_rows`/`gap_rows`/`gap_fields` 分开计数并写进 notes（`:556-587`）；第五轮：`expected_batch is None` 时把 `batch` 从逐字段 FAIL 集合里减掉（只留 UNKNOWN，既不猜批次也不抑制其他字段）；新增 `plan_task_by_identity` 索引 + `plan_seq`/`seed`/`scheme`/`algorithm_version` 按身份逐字段同源审计；`_warn_legacy_plan()` 只给无 plan.json 的 legacy 用；第六轮：删除 `_record_batches()`，新增 `BatchDecision`/`decide_batch()`/`_scan_batch_declarations()`（记录声明分 missing/conflict/invalid/可用四态，非法值不再被丢掉），行级改用 `raw_row` 逐行核 `batch`（非空字符串 + 与目录/文件一致），文件级冲突单独 FAIL，`failed_fields` 统一减 `batch` 避免重复；`plan_ids` 改由 `plan_identity_map()` 得到（结构坏时为 None）；`check_plan()` 增加计划结构 FAIL、计划身份唯一性 FAIL 与「N 个任务身份唯一」PASS；第七轮：`check_manifest()` 返回 `ManifestIndex`（`batches`/`batch_problems`），`decide_batch()` 改七态（directory/manifest/manifest-conflict/manifest-missing/manifest-invalid/untrusted-records/missing），冲突时不采用任何来源，记录声明只做逐行交叉验证，`check_plan()` 开头调用 `plan_type_problems()` 对类型错误 fail-fast；第八轮：`check_plan()` 改返回 `PlanCheck` 三态（missing/valid/invalid），`check_results()` 接收 `plan_check` 关键字参数，只有 valid 才建索引、做成员关系与完成度，invalid 保留原始 FAIL 且不误报 legacy；第九轮（第十轮已修正 B0 短路）：新增 `PLAN_STATE_UNTRUSTED`，`run_preflight()` 先按 `legacy or (declared and not failed)` 判定 plan/results 信任位，不可信 plan 不再调用 `check_plan()`，不可信 results 以 `trusted=False` 进入 `check_results()`（仍解析诊断，但返回空身份、不做完成度），B0 legacy 保持清单外例外；第十轮：信任位改为 `not in failed and (declared or legacy)`，legacy 只豁免未声明、不豁免已声明的完整性失败；新增 `plan-results-conflict` 批次态，冲突时 plan 直接 `untrusted`、results `trusted=False`，跳过 `by_batch` 与覆盖；第十一轮：新增 `plan_batch_missing_problem`/`results_batch_missing_problem` 并合并为 `batch_pairing_problem`，plan 的清单 batch 不可用时走 `PlanCheck(untrusted)` 且不建身份索引，`check_results()` 的 `trusted` 追加配对条件，覆盖与总量一律跳过 |
| `experiments/graduation/plan_schedule.py` | 第六轮 +83 / −0，第七轮 +106 / −33（现 700 行） | 新增 `plan_identity_entries()`/`plan_identity_duplicates()`/`plan_identity_map()`，把计划任务逐条展开并带 `where`；`plan_tasks()`/`plan_identities()` 未改动。第七轮新增 `_int_type_problem()`/`_str_type_problem()` 与公开的 `plan_type_problems(plan)`（顶层与 task 的 int 不得是 bool，`group` 非空字符串，`sample_id` 缺失不算类型错误），生产侧 `_check_plan_scalar_types`/`_check_plan_types` 改为复用同一份判定 |
| `experiments/graduation/analyze_stats.py` | +31 / −1 | 引入 result_schema（`:64`）；`MAPPING_IDENTITY_FIELDS` 去掉 condition 并加注释（`:1624-1634`）；`_compare_mapping_identity` 增加 condition 归属判定（`:1692-1706`） |
| `test_graduation_stats.py` | +5 / −2 | 合成源行夹具的 group 改为与 condition 相等（真实记录 group 就是 A/B/C/D/C-subj/D-subj/B0）。原夹具 group="G"+... 与打包器 condition=group 冲突，是 STATS EXIT 2 的真因 |
| `test_graduation_contract.py` | 第三轮新增，第四轮 +220 行，第五轮 +260 行，第六轮 +257 / −3，第七轮 +258 / −34，第八轮 +83，第九轮 +228 / −4，第十轮 +179，第十一轮 +218（现 2567 行） | 生产者—消费者契约测试，67 个用例 / 487 项断言；第四轮新增第 7.5 节七个「生产器记录 × plan.json 联合核验」场景；第五轮新增第 7.6 节七个走 `run_preflight()` 全流程的场景（批次不可判定、四种字段篡改、对照组、真实 `b0-*` legacy）；第六轮新增第 7.7 节八个场景（批次四态五条 + 计划唯一性三条，含正常对照组），B0 legacy 用例改用 S5/S7 与 B0 组；第七轮改写 7.6 的批次用例（清单回退、文件不在清单），新增 7.7 四个清单批次场景与 7.8 六个 plan.json 类型负向场景；第八轮新增 7.9 两个「无效计划 + 完整 5 条结果」全流程场景，并给 7.7 对照组补一条完成度 PASS 断言；第九轮新增 7.10 五个「清单完整性闸门」全流程场景（140 条真实主实验记录 × 4 组、B0 legacy 例外），并给 `_produce()` 增加 `plan_seq`/`round_index` 覆盖参数以构造 140 任务计划；第十轮新增 7.10 的 4 个场景（B0 已声明 results 篡改、B0 已声明 plan 篡改、140 条完整数据的 plan/results 清单批次冲突、同夹具正常对照），新增 `_full_b0_case()` 完整 10 条 B0 夹具；第十一轮新增 7.11 五个清单 batch 配对全流程场景（plan batch 为空、清单无 batch 列、results batch 为空、140 条正常对照、B0 legacy 对照） |

未改动：`run_experiment.py`（生产器）、`make_blind_pack.py`（打包器）、`docs/GRADUATION-EXPERIMENT-DESIGN.md`（冻结规范）。

## 十四、生产者—消费者契约测试（`test_graduation_contract.py`）

链路（全部合成数据、临时目录，不触真实数据库/runs/盲评目录）：

1. 打桩 `api["database"].DATABASE_PATH`（临时路径，`run_one` 自带的"不得等于真实 DB"检查仍在）+ `api["agent_plan_draft"]` 替身；fake draft_runner 在起草期间向 `UsageRecorder` 写一条用量（真实客户端也是在这时写；`run_one` 用 `recorder.since(before)` 只统计起草期间的请求）。
2. 真实调用 `run_experiment.run_one()`，真实 `append_jsonl()` 落盘。
3. 过 `result_schema.normalize_record()`，逐字段核对。
4. 过 `preflight_experiment_data.check_results()`。
5. 过 `make_blind_pack.read_rows()`（`_validate_row_shape` + `SourceRow.condition`）。
6. 过 `analyze_stats.load_results()` 的 mapping 核对。

用例（67 个）：

- 契约表：test_contract_table（29 字段、顺序、12/10/7 分类不重不漏、每字段都有依据/来源/规则）
- 生产器记录形状：test_producer_record_shape（不含 17 个 §7.1 专属字段、含 final_output/token_usage/raw_output/prompt_ref/elapsed_seconds/success/corrections_used、非设计原生行、落盘后同一份记录）
- 标准化：test_normalize_success（视图 21 字段 = 29 − 7 − 1；condition/batch/output_raw/answer/plan/model/validation/usage/latency_ms/messages_path 逐个核对；由 .txt 重建的 messages 哈希等于 prompt_sha256；D 组 prompt 里确有反馈与记忆痕迹）
- 不可恢复：test_unrecoverable（7 个、与契约表一致、不含可派生字段、is_complete()==False、missing()==unrecoverable()、每项都有原因）
- 派生边界：test_derivation_boundaries（condition 冲突/缺 group、缺 final_output、失败记录不派生 validation/error、usage 不可用不补 0、缺 elapsed_seconds、Prompt 文件缺失、round_index≠repeat_index、1.234 → 1234ms 精确）
- 落盘：test_append_jsonl（追加不覆盖、键有序、中文不转义）
- 预检：test_preflight_producer_rows（FAIL 恰好 7 条、点名 7 个字段、可派生字段 0 FAIL、理由等于契约表原因、写明行数行号、notes 记录标准化数量、没有伪造 PASS）、test_preflight_design_native_unchanged（补齐 7 字段 + error=null 的 29 字段原生行 0 FAIL）、test_preflight_missing_prompt、test_preflight_usage_unavailable、test_preflight_legacy_plan_sha（不产生 FAIL，只有 1 条 WARNING 且写明需负责人确认）、test_preflight_failed_record
- 预检 × plan.json 联合核验（第四轮，七个场景、共 30 项断言）：
  - test_partial_record_with_plan：同源计划下 7 条缺口 FAIL + 身份进入集合比较 + 计划完成度 PASS + notes 分写执行证据/完整性；
  - test_partial_record_plan_sha_null：plan.json 存在而记录 plan_sha256 为 null → 额外 FAIL（7+1=8 条），且不走 legacy WARNING；
  - test_partial_record_plan_sha_tampered：篡改 plan_sha256 → 「与同目录 plan.json 不同源」单独 FAIL（7+1=8 条），不被 7 个缺口遮住；
  - test_partial_record_not_in_plan：记录不在计划中 → 身份 FAIL + 计划完成度 FAIL（7+1+1=9 条），缺失身份被点名；
  - test_partial_record_duplicate：同一身份两条真实记录 → 重复 FAIL（7+1=8 条），身份只计 1 个，两条都被计数；
  - test_partial_record_batch_misattributed：把 S2 记录放进 suppl-subj 批次 → 生产器行照常带目录推断批次、不产生假 FAIL，但覆盖检查报 FAIL，与 7 条缺口互不屏蔽；
  - test_partial_record_legacy_still_warning：无 plan.json 的 B0 legacy → 只有 1 条 WARNING，仍只 7 条缺口 FAIL，不做计划完成度判定，行仍计入执行证据。
- 预检全流程（第五轮，第 7.6 节，七个场景 / 43 项断言）：
  - test_flow_manifest_batch_fallback（第七轮改写自 test_flow_unknown_batch）：不匹配任何 `BATCH_PREFIXES` 的目录 + 清单 batch=main + 记录不带 batch → 按清单归入 main，无 `· batch` FAIL，覆盖核对执行并 FAIL（S1 不属于 main），无 UNKNOWN，7 个缺口 FAIL 仍在；
  - test_flow_batch_unknown（第七轮新增）：目录不可识别、results.jsonl 不在清单、记录不带 batch → 1 条 UNKNOWN，无 `· batch` FAIL，清单覆盖 FAIL；
  - test_flow_plan_audit_control：计划 5 个任务全部真实跑完 + 同源 plan.json → 四个字段 0 FAIL、plan_sha256 0 FAIL、计划完成度 PASS、7 条缺口仍在；
  - test_flow_plan_seq_tamper：只把第 0 行 `plan_seq` 改成 2（合法整数、仍在 0..4 内）→ 1 条 `… · plan_seq` FAIL，写明实际 2 / 预期 0，理由「执行顺序无法审计」；
  - test_flow_seed_tamper：只改 `seed` → 1 条 `… · seed` FAIL「不同源」；
  - test_flow_scheme_tamper：只改 `scheme` → 1 条 `… · scheme` FAIL「不同源」；
  - test_flow_algorithm_version_tamper：只改 `algorithm_version` → 1 条 `… · algorithm_version` FAIL「不同源」；四种篡改下 `plan_sha256` 都不跟着误报，其余 4 行照常通过；
  - test_flow_b0_legacy：真实目录 `b0-remainder-20261113T013105Z`、无 plan.json、清单里只放无关说明文件 → 目录按 `b0-` 规则识别为 legacy，清单覆盖只 WARNING、计划缺失只 WARNING、`plan_sha256` 恰 1 条 WARNING 且无 FAIL，7 个缺口仍 FAIL，行仍计入执行证据（第六轮把合成数据改成两行 `S7 × B0`、`S5 × B0` 真实记录，断言「身份可识别 2 行」）。
- 批次四态与计划唯一性（第六轮，第 7.7 节，八个场景 / 35 项断言）：
  - test_flow_batch_records_consistent：目录判不出批次、两行一致声明 `suppl-subj` → 无 `· batch` FAIL、notes「批次 suppl-subj：2 条记录」、无 UNKNOWN、7 条缺口仍是 FAIL；
  - test_flow_batch_conflict：两行分别声明 `main` 与 `suppl-subj` → 恰好 1 条文件级 FAIL 并点名两个值与两个行号，**不再**降级成 UNKNOWN，7 条缺口仍在；
  - test_flow_batch_invalid：五行分别写 `["main"]` / `{"batch":"main"}` / `3` / `true` / `"  "` → 5 条 FAIL 全部要求「非空字符串」，无 UNKNOWN；
  - test_flow_batch_directory_conflict：目录 `main-*`，第 2 行声明 `suppl-subj` → 恰好 1 条 FAIL 且 target 以 `:2 · batch` 结尾，expected 为「batch=main（由目录推断）」；
  - test_flow_batch_directory_derived：目录 `main-*`、两行都不写 batch → 无 batch FAIL，notes 写「批次 main」（正常目录推断对照组）；
  - test_flow_plan_duplicate_identity：把 `tasks[1]` 的身份改成与 `tasks[0]` 相同并重算 `plan_sha256`，只跑 1 行 → 恰好 1 条「身份唯一性」FAIL（「重复 2 次」「plan_seq=0、1」「执行顺序不可审计」），无 PASS，notes 说明重复身份未建索引；
  - test_flow_plan_structure_broken：删掉 `blocks[0]["sample_id"]` → 恰好 1 条「计划结构」FAIL 点名 sample_id，notes 记结构不完整，无 PASS；
  - test_flow_plan_identity_control：正常五轮计划 → 恰好 1 条 PASS「5 个任务身份唯一」，无重复 FAIL、无结构 FAIL、无「计划类型」FAIL。
- 第七轮新增（第 7.6 / 7.7 / 7.8 节，12 个场景 / 46 项断言）：清单与目录矛盾、清单 batch 为空、清单没有 batch 列、plan.json 与 results.jsonl 清单批次不一致各 1 条场景；plan.json 的 `plan_seq`/`round_index`/`seed`/`round_count` 六个 float/bool 负向场景。具体构造与断言见第 7.3 节。
- 第八轮新增（第 7.9 节，2 个场景 / 14 项断言）：无效计划（`plan_seq=1.0` / `seed` 等值 float）+ 完整 5 条结果，验证计划类型 FAIL 保留、无计划完成度 PASS、无身份唯一性 PASS、无「身份命中」、无 legacy WARNING；对照组追加正常计划 5 条结果的完成度 PASS。具体构造与断言见第 8.3 节。
- 第九轮新增（第 7.10 节，5 个场景 / 23 项断言）：140 条真实主实验记录分别验证 results 哈希失败、plan 哈希失败、可信对照、results 未纳入清单、B0 legacy 例外。前两者不得产生覆盖/完成度/总量 PASS，可信对照必须保留完成度与主批次覆盖 PASS，未纳入清单的 results 不得靠目录前缀进入汇总，B0 legacy 不因清单外被新闸门误伤。具体构造与断言见第 9.3 节。
- 第十轮新增（第 7.10 节，4 个场景 / 24 项断言）：完整 10 条 B0 数据分别验证「results 已声明后改 final_output.answer」「plan 已声明后改 created_at 并重算 plan_sha256」都不得产生 B0 覆盖/完成度 PASS，且不误报 legacy；B0 数据恢复原文后必须拿到 legacy WARNING 与完成度 PASS 作为同夹具对照；完整 140 条主实验数据把 plan 的清单 batch 写成 suppl-subj、results 写成 main，验证恰一条冲突 FAIL、无计划索引/完成度/覆盖/总量/身份 PASS、无 UNKNOWN；同一 140 条夹具在 batch 一致时保留完成度与覆盖 PASS。具体构造与断言见第 10.3 节。
- 第十一轮新增（第 7.11 节，5 个场景 / 36 项断言）：完整 140 条主实验数据分别验证 plan 清单 batch 为空、清单没有 batch 列、results 清单 batch 为空三种构造下，都不产生身份唯一性/身份集合/计划完成度/主批次覆盖/可信总量 PASS，notes 写明批次归属不可确认；同夹具在两侧 batch 一致时保留计划身份唯一性、完成度与主批次 140 覆盖 PASS；B0 两文件未纳入清单时仍按 legacy 处理（WARNING + 完成度 PASS），不被新闸门误伤。具体构造与断言见第 11.3 节。
- 打包器：test_packer（读得到、SourceRow.condition == group、行哈希与统计器一致、文件哈希 = 文件字节 SHA-256）
- 统计器：test_stats_roundtrip（1 mapping ↔ 1 行、命中第 1 行、内容一致、文件哈希记录、cost_metrics 可用）+ test_stats_condition_tamper、test_stats_source_group_tamper、test_stats_source_condition_conflict、test_stats_group_missing、test_stats_mapping_condition_empty
- 三消费者口径一致：test_consumers_agree（身份四元组、condition 归属、prompt 哈希三处判定同源）

结果：`0 FAILED / 487 PASSED`，退出码 0。

## 十五、测试结果（本机，非 CI）

| 测试 | 结果 |
| --- | --- |
| `test_graduation_preflight.py` | 18/18 PASS，退出码 0 |
| `test_graduation_stats.py` | 368 PASSED / 0 FAILED，退出码 0 |
| `test_graduation_blind_pack.py` | 46/46 PASS，退出码 0 |
| `test_graduation_rating_materials.py` | 59 通过 / 0 失败，退出码 0 |
| `test_graduation_toolchain_e2e.py` | 8/8 PASS，退出码 0 |
| `test_graduation_experiment_runner.py` | 205 PASSED / 0 FAILED，退出码 0 |
| `test_graduation_contract.py`（第三轮新增，第四～十一轮扩充） | 487 PASSED / 0 FAILED，退出码 0 |

合计 1191 项断言全部通过（第三轮 936 + 第四轮新增 33 项 + 第五轮新增 43 项：第 7.6 节七个全流程场景 + 第六轮新增 35 项：第 7.7 节八个场景 + 第七轮新增 46 项：清单批次场景与 plan.json 类型负向 + 第八轮新增 15 项：第 7.9 节两个无效计划场景与对照组完成度断言 + 第九轮新增 23 项：第 7.10 节五个清单完整性场景 + 第十轮新增 24 项：第 10.3 节四个 B0/批次冲突场景 + 第十一轮新增 36 项：第 11.3 节五个清单 batch 配对场景）。以上全部为本机运行，非 CI。本轮按要求未访问真实 `runs/`、`feedback.db`、`blind_private/`；测试只使用 `tempfile` 临时目录，结束后自动清理。

复现：`cd F:\somethingProject\dsbot-preflight`；`$env:PYTHONIOENCODING="utf-8"`；`& "F:\somethingProject\dsbot\.venv\Scripts\python.exe" test_graduation_contract.py`。

第五～十一节的合成演示与全流程用例用的是临时目录里的真实 `run_one()` 记录 + 合成 plan.json / 清单（直接调用 `run_preflight()`，不写任何真实目录），演示结束后脚本与目录一并删除；本轮未对真实 `runs/`、`blind_private/` 或 `feedback.db` 做任何读取或写入。

## 十六、仍需负责人决策的事项

1. 7 个不可恢复字段怎么办（这是本轮暴露的实质问题，不能靠工具绕开）：
   - A. 接受"现有数据不满足 §7.1"的事实，把预检对这批数据的结论固定为 FAIL，并在论文里如实说明；下一版生产器补齐字段。
   - B. 修订冻结设计（§7.1 降级/改写），把字段要求改成"可获得的子集"。
   - C. 重新生成数据（165 个任务），生产器先补齐字段再跑。
   三项互斥，取舍影响论文口径与工作量，需负责人定。
2. 若选 A / 做下一版生产器：需要确认补齐方式——run_id 用何种形态（是否允许由 generated_at + sample + group + round 合成）、base_sha 是否取运行开始时的工作树 SHA、knowledge_result_ids/memory_injected/short_term_injected/difficult_flag 是否要新增到 run_one() 的记录里（会改动生产器，本轮未做）。
3. B0 legacy 的 plan_sha256：无 plan.json 时预检只报 WARNING、需负责人确认。是否仍按"WARNING 可接受"处理？
4. 批次口径（第七轮已收敛，剩一个确认点）：清单是唯一可信来源；目录名可识别时必须与清单一致，否则 `manifest-conflict` FAIL 且不采用任何一批；目录名不可识别但清单有合法 batch 时按清单归类（不强制 `main-*` / `suppl-*` / `b0-*` 前缀）；清单缺 batch / batch 为空 / 没有 batch 列都是文件级 FAIL；记录自述永远只做交叉验证。需负责人确认的是：是否进一步要求正式数据**必须**落进带批次前缀的目录（即「只有清单、目录名不规范」也不合格），从而把 `manifest` 态提升为 FAIL。
5. 冒烟批次是否纳入正式清单：internal-api-smoke-*（round_count=1）是否计入 165 个任务的完成度统计。
6. 计划身份唯一性的防线位置：目前只在**消费侧**（`preflight` 的 `check_plan()`）把重复身份判成 FAIL；第七轮任务书把 `plan_schedule.build_schedule()` 的身份唯一性后置断言标为「可考虑」，本轮未加（理由见 7.4）。是否要让生产侧也 fail-closed（生成时就拒绝重复身份），需要负责人确认——那会改动计划生成器的行为与既有测试夹具，应作为独立变更评审。
7. validation 的派生是否可接受：本轮依据"起草链只在四项校验全过后才返回 ok=True"把 validation 确定性派生为全 true + corrections_used。如果评审要求"validation 必须由生产器逐项记录"（即不认可派生），那它要并入第 1 条的不可恢复清单。
8. 第九轮把「正式 plan.json 未纳入清单」也按 `untrusted` 处理（任务书只点名 `ManifestIndex.failed`）。这是更严格的 fail-closed 口径：正式目录的 plan.json 与 results.jsonl 都必须在清单中且哈希一致。若评审认为计划文件可以先于清单登记、允许暂时不在清单中，需要明确这个例外及其证据边界。
9. 第十轮新增 `plan-results-conflict` 批次态：plan/results 的清单 batch 矛盾时，整个 run-dir 的计划索引、完成度、覆盖与总量全部切断。若评审希望保留「按 results 的 batch 继续统计、只把 plan 标为不可信」的部分结论，需要明确哪一类结论仍可信及其论文口径；当前实现选择整体 fail-closed。
10. 第十轮把 B0 的 legacy 例外收窄为「只豁免未纳入清单」：已纳入清单的 B0 文件必须通过 SHA-256/大小/记录数核验，进入 `failed` 即不可信。若评审认为 B0 legacy 应当无条件豁免清单完整性（即使已登记），需要给出设计依据；当前实现以「清单已声明就必须自洽」为准。
11. 第十一轮把「plan/results 任一侧清单 batch 缺失/为空」并入批次配对闸门，与第十轮的「两侧冲突」同口径处理：一旦配对前提不可确认，覆盖、完成度与 165 总量全部跳过，只在 notes 说明「不计入」，不额外产生一条点名「165 份」的 FAIL。若评审要求这种情况下必须出现显式总量 FAIL（而不是静默跳过），需要确认 target/expected 口径；当前实现选择与 `plan-results-conflict` 一致，避免同一缺陷两种报告形态。
