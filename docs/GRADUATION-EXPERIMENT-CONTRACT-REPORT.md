# DSBot 记忆实验 · 生产者—消费者字段契约报告（PR #26 第三轮）

- 分支：`feat/graduation-preflight-integrity`
- 基线提交：`64f619ab2c7dad07ce731994316bcdb3f66b9a71`
- 冻结口径：`docs/GRADUATION-EXPERIMENT-DESIGN.md` v4.5（本报告未修改该文件）
- 取证范围：只读源码与合成数据。未读取或修改真实 `runs/`、`feedback.db`、`blind_private/`，未生成盲评包，未执行真实 `--repack`。
- 运行环境：本机 PowerShell + `F:\somethingProject\dsbot\.venv\Scripts\python.exe`，非 CI。

## 结论

1. 当前生产器 `run_experiment.py` 从未按 §7.1 产出过记录（首个版本 0471275 起即如此，不是后来回归）。三个消费者里，打包器按生产器 schema 写，预检与统计器按 §7.1 写，这才是"契约不一致"的根因。
2. 29 个 §7.1 字段中，12 个直接可用、10 个可确定性派生、7 个不可恢复。新增标准化层 `experiments/graduation/result_schema.py` 承担这层转换，逐字段标注状态与依据（不允许"名字像就复制"）。
3. 统计器的 `condition` 比较已改为"由源记录 `group` 派生"，与打包器 `SourceRow.condition = self.group` 同口径；mapping 与源记录任一侧被篡改都会报 `results-mapping-mismatch`。
4. 预检对生产器记录走标准化路径：可派生字段照常核验，7 个不可恢复字段逐字段 FAIL（不降级为 WARNING），设计原生记录仍走原严格路径（回归用例证明未放宽）。
5. 新增生产者—消费者契约测试 20 个用例、232 项断言：真实跑 `run_one()` → 真实 `append_jsonl()` 落盘 → 依次过预检、打包器、统计器。7 套测试全绿（详见第六、七节）。

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

## 五、代码修改清单

| 文件 | 变更 | 要点 |
| --- | --- | --- |
| `experiments/graduation/result_schema.py` | 新增（825 行） | §7.1 标准化层：契约表、derive_condition、parse_prompt_text/messages_sha256/read_prompt_messages、normalize_record/Normalized。stdlib only，不 import 产品代码 |
| `experiments/graduation/preflight_experiment_data.py` | +92 / −7 | 引入标准化层（`:55`）；`CONTRACT_BASIS`（`:104`）；`check_results` 新增生产器分支（`:399-419`）：标准化 → 派生失败逐行 FAIL → 缺口按字段聚合 FAIL（`:501-512`）→ 视图参与原有类型/批次/身份/plan 校验；`null_ok`（设计值就是 null 的字段不算缺口）；`_warn_legacy_plan()`（`:359-372`）把 legacy 缺 plan_sha256 的 WARNING 提前到跳过之前，保证被跳过的行也仍被告知 |
| `experiments/graduation/analyze_stats.py` | +31 / −1 | 引入 result_schema（`:64`）；`MAPPING_IDENTITY_FIELDS` 去掉 condition 并加注释（`:1624-1634`）；`_compare_mapping_identity` 增加 condition 归属判定（`:1692-1706`） |
| `test_graduation_stats.py` | +5 / −2 | 合成源行夹具的 group 改为与 condition 相等（真实记录 group 就是 A/B/C/D/C-subj/D-subj/B0）。原夹具 group="G"+... 与打包器 condition=group 冲突，是 STATS EXIT 2 的真因 |
| `test_graduation_contract.py` | 新增（905 行） | 生产者—消费者契约测试，20 个用例 / 232 项断言 |

未改动：`run_experiment.py`（生产器）、`make_blind_pack.py`（打包器）、`docs/GRADUATION-EXPERIMENT-DESIGN.md`（冻结规范）。

## 六、生产者—消费者契约测试（`test_graduation_contract.py`）

链路（全部合成数据、临时目录，不触真实数据库/runs/盲评目录）：

1. 打桩 `api["database"].DATABASE_PATH`（临时路径，`run_one` 自带的"不得等于真实 DB"检查仍在）+ `api["agent_plan_draft"]` 替身；fake draft_runner 在起草期间向 `UsageRecorder` 写一条用量（真实客户端也是在这时写；`run_one` 用 `recorder.since(before)` 只统计起草期间的请求）。
2. 真实调用 `run_experiment.run_one()`，真实 `append_jsonl()` 落盘。
3. 过 `result_schema.normalize_record()`，逐字段核对。
4. 过 `preflight_experiment_data.check_results()`。
5. 过 `make_blind_pack.read_rows()`（`_validate_row_shape` + `SourceRow.condition`）。
6. 过 `analyze_stats.load_results()` 的 mapping 核对。

用例（20 个）：

- 契约表：test_contract_table（29 字段、顺序、12/10/7 分类不重不漏、每字段都有依据/来源/规则）
- 生产器记录形状：test_producer_record_shape（不含 17 个 §7.1 专属字段、含 final_output/token_usage/raw_output/prompt_ref/elapsed_seconds/success/corrections_used、非设计原生行、落盘后同一份记录）
- 标准化：test_normalize_success（视图 21 字段 = 29 − 7 − 1；condition/batch/output_raw/answer/plan/model/validation/usage/latency_ms/messages_path 逐个核对；由 .txt 重建的 messages 哈希等于 prompt_sha256；D 组 prompt 里确有反馈与记忆痕迹）
- 不可恢复：test_unrecoverable（7 个、与契约表一致、不含可派生字段、is_complete()==False、missing()==unrecoverable()、每项都有原因）
- 派生边界：test_derivation_boundaries（condition 冲突/缺 group、缺 final_output、失败记录不派生 validation/error、usage 不可用不补 0、缺 elapsed_seconds、Prompt 文件缺失、round_index≠repeat_index、1.234 → 1234ms 精确）
- 落盘：test_append_jsonl（追加不覆盖、键有序、中文不转义）
- 预检：test_preflight_producer_rows（FAIL 恰好 7 条、点名 7 个字段、可派生字段 0 FAIL、理由等于契约表原因、写明行数行号、notes 记录标准化数量、没有伪造 PASS）、test_preflight_design_native_unchanged（补齐 7 字段 + error=null 的 29 字段原生行 0 FAIL）、test_preflight_missing_prompt、test_preflight_usage_unavailable、test_preflight_legacy_plan_sha（不产生 FAIL，只有 1 条 WARNING 且写明需负责人确认）、test_preflight_failed_record
- 打包器：test_packer（读得到、SourceRow.condition == group、行哈希与统计器一致、文件哈希 = 文件字节 SHA-256）
- 统计器：test_stats_roundtrip（1 mapping ↔ 1 行、命中第 1 行、内容一致、文件哈希记录、cost_metrics 可用）+ test_stats_condition_tamper、test_stats_source_group_tamper、test_stats_source_condition_conflict、test_stats_group_missing、test_stats_mapping_condition_empty
- 三消费者口径一致：test_consumers_agree（身份四元组、condition 归属、prompt 哈希三处判定同源）

结果：`0 FAILED / 232 PASSED`，退出码 0。

## 七、原有测试结果（本机，非 CI）

| 测试 | 结果 |
| --- | --- |
| `test_graduation_preflight.py` | 18/18 PASS，退出码 0 |
| `test_graduation_stats.py` | 368 PASSED / 0 FAILED，退出码 0 |
| `test_graduation_blind_pack.py` | 46/46 PASS，退出码 0 |
| `test_graduation_rating_materials.py` | 59 通过 / 0 失败，退出码 0 |
| `test_graduation_toolchain_e2e.py` | 8/8 PASS，退出码 0 |
| `test_graduation_experiment_runner.py` | 205 PASSED / 0 FAILED，退出码 0 |
| `test_graduation_contract.py`（新增） | 232 PASSED / 0 FAILED，退出码 0 |

合计 936 项断言全部通过。运行前后 `feedback.db` 指纹不变：058368B60D72D649A11B82B4D35474069899C42FAF4759D0168F75C1EADBD725（114688 字节）；`%TEMP%` 无新增 grad_* 残留。

复现：`cd F:\somethingProject\dsbot-preflight`；`$env:PYTHONIOENCODING="utf-8"`；`& "F:\somethingProject\dsbot\.venv\Scripts\python.exe" test_graduation_contract.py`。

## 八、仍需负责人决策的事项

1. 7 个不可恢复字段怎么办（这是本轮暴露的实质问题，不能靠工具绕开）：
   - A. 接受"现有数据不满足 §7.1"的事实，把预检对这批数据的结论固定为 FAIL，并在论文里如实说明；下一版生产器补齐字段。
   - B. 修订冻结设计（§7.1 降级/改写），把字段要求改成"可获得的子集"。
   - C. 重新生成数据（165 个任务），生产器先补齐字段再跑。
   三项互斥，取舍影响论文口径与工作量，需负责人定。
2. 若选 A / 做下一版生产器：需要确认补齐方式——run_id 用何种形态（是否允许由 generated_at + sample + group + round 合成）、base_sha 是否取运行开始时的工作树 SHA、knowledge_result_ids/memory_injected/short_term_injected/difficult_flag 是否要新增到 run_one() 的记录里（会改动生产器，本轮未做）。
3. B0 legacy 的 plan_sha256：无 plan.json 时预检只报 WARNING、需负责人确认。是否仍按"WARNING 可接受"处理？
4. 冒烟批次是否纳入正式清单：internal-api-smoke-*（round_count=1）是否计入 165 个任务的完成度统计。
5. validation 的派生是否可接受：本轮依据"起草链只在四项校验全过后才返回 ok=True"把 validation 确定性派生为全 true + corrections_used。如果评审要求"validation 必须由生产器逐项记录"（即不认可派生），那它要并入第 1 条的不可恢复清单。
