# S1 文档解析与来源定位 · 测试报告

> 生成时间：2026-11-18｜分支：`rag/document-parsing-s1`（基线 `c0c425b`，即当时 `origin/main`）
> 本版为 v1.3（第二轮独立审查修复后重跑）。上一版 v1.2 记录 119 项断言；本版 `test_document_parser.py` 扩到 145 项。2026-11-19 第三轮审查后仅修正 `document_id` 语义表述，代码与测试未改，各项数字不变。
> 报告原则：只写实际跑过的结果。**跳过的、没验证的、基线本来失败的，都按原样标出，不计入通过。**

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| 解释器 | `F:\somethingProject\dsbot\.venv\Scripts\python.exe`（Python 3.14.5） |
| 运行目录 | worktree `C:\Users\BOSS\AppData\Local\Temp\dsbot-rag-s1`（本分支检出，无独立 .venv，复用主仓库依赖） |
| 控制台编码 | 运行前设 `PYTHONIOENCODING=utf-8`（Windows GBK 控制台会把中文/emoji 打印成 `UnicodeEncodeError`） |
| 相关依赖实测版本 | pypdf 7.1.0、python-docx 1.2.0、lxml 6.1.3、reportlab 5.1.0（reportlab 仅测试造夹具用） |
| 数据隔离 | 所有新增测试在 `tempfile` 临时目录内造合成文档，`finally` 里 `shutil.rmtree` 并断言已清理；不读写正式实验 run 文件、不写 `.rag_cache`、不调用真实 LLM/Embedding API |
| 正式数据状态 | 主仓库 `F:\somethingProject\dsbot\feedback.db` SHA256 = `058368B60D72D649A11B82B4D35474069899C42FAF4759D0168F75C1EADBD725`，114688 字节，**与进入本任务前一致**；`experiments/graduation/runs/` 未触碰 |
| 测试执行方式 | 仓库没有 pytest/统一 runner，本分支根目录 **46 个** `test_*.py` 逐个 `python -X utf8 test_xxx.py` 运行，按退出码判定 |

---

## 2. 本轮的测试

### 2.1 `test_document_parser.py`（S1 主测试，**145 项断言**）

单文件手写 runner：`check(name, condition, detail)` 记 pass/fail、`skip(name, reason)` 记跳过、`expect_parse_error(name, expected_code, func)` 断言错误码；结束时按 fail 数 `SystemExit(1)`。

夹具全部现场合成：

- `build_pdf(pages)`：reportlab + `UnicodeCIDFont("STSong-Light")` 生成多页中文 PDF，每页正文 + 页脚 `- 1 -` 形式的印刷页码。
- `build_blank_pdf()`：两页空白，用来制造「无文本层」。
- `build_docx()`：Heading 1「第一章 导数」+ 正文 + Heading 2「1.1 定义」+ 2×2 表格。
- `reencrypt_pdf(raw, password=None)`：用 pypdf 把合成 PDF 重写成加密副本（空口令 / 打开密码两种）。

覆盖矩阵：

| 小节 | 验证点 |
|---|---|
| TXT | 解析内容、`document_hash`、`document_id`（路径摘要为 16 位）、`chunk_id` 两次解析完全一致；改内容后 `document_hash/document_id/chunk_id` 全变；改 `max_chars` 后 `chunk_id` 变；重复段落仍生成唯一 `chunk_id`；TXT 不虚构页码与标题路径；TXT 整篇一个 `kind="text"` 块 |
| 文档身份 | `甲/` 与 `乙/` 同名同内容 → `document_id` 不同；`"甲\\讲义.txt"`、`"./甲//讲义.txt"`、`"甲/讲义.txt"` 三种写法归一；`a#b%23#c.txt` 等文件名下 `chunk_id.split("#")` 恒 3 段且首段等于 `document_id` |
| PDF | 物理页码 `[1,2]`、印刷页码 `[1,2]`；跨页片段 `locator` 含 `-`、`source_spans` 覆盖两页；单页多段 `char_start > 0` 且 `page_text[char_start:char_end] == block.text`；两次解析结果一致 |
| **3.1 版本与换代** | `PARSER_VERSION == "s1.2"` 且 `CHUNKER_VERSION == "s1.2"`；每条记录里 `parser_version/chunker_version` 都是 `s1.2`；同文件两次解析 `document_id` 与 `chunk_id` 列表完全相同；用 `("s1.1","s1.1")` 作为版本输入重新算 `chunk_id` 得到**不同**值（升版即换代）；`document_id` 拆三段、中段长度 16 且只含 `0-9a-f` |
| **3.2 加密 PDF** | 普通 PDF 仍 `status="ok"` 且有块；空口令（仅 owner 口令）加密副本可正常解析出块、`pages_total` 与原文相同；带打开密码的副本抛 `ParseError(code="password_required")`；`status_for_code("password_required") == "password_required"`（不落成 `corrupted_pdf`） |
| DOCX | `page_number` 恒 `None`；`paragraph_index` 齐全；`heading_path` 为 2 级；`chunk_id` 用 `para` 定位；**表格块**（`kind="table"`）至少 1 个，`char_start=0`、`char_end=len(text)`、`text[char_start:char_end]==text`，且 `chunk.source_spans` 里 `kind=="table"` 的 span 的 `block_index` 落在表格块下标集合内、偏移与对应块完全相等 |
| 混合型 PDF | `pages_total=3 / pages_with_text=2 / missing_pages=[2] / status="partial"`；`scan_knowledge` 里 `warnings` 恰一条 `{warning_code:"missing_pages", page_numbers:[2]}`、`issues` 为空、仍产出切片；`inspect_document` 同样返回 `partial` + 缺页 |
| 错误码 | `unsupported_type` / `empty_file` / `decode_failed` / `corrupted_pdf` / `no_text_layer` / `corrupted_docx` 六个码逐一命中，`to_issue()` 结构正确 |
| 落盘与统计 | `save_document` 三种格式成功且磁盘字节与输入一致；坏 PDF 抛 `DocumentError` 且临时目录文件列表前后不变（**坏文件不落盘**）；`.png` 被拒且文案含三后缀；`chunk_file` 三种格式切片数与直接切片一致；`load_chunk_records` 覆盖三格式、15 字段齐全且可 JSON 序列化；`load_chunks` 默认键集恰为 `{content, source}`；`load_chunk_records_with_states` 记录与 `load_chunk_records` 完全一致、状态表覆盖三份文档且全 `ok`、页码完整性字段正确；`inspect_document` 返回切片数与两版本、缺文件抛 `DocumentError`；`scan_knowledge` 3 文档、无 issue、`chunk_count` 与切片数一致 |
| `scan_knowledge` 状态 | 好文件 status 全 `ok`、`bytes` 与磁盘一致、两版本已写入；坏 PDF `failed`、扫描件 `no_text_layer`；失败文档 `chunk_count=0` 且 `document_id=None`、带正确 `error_code`、同时进 `issues`；扫描件不产出切片 |
| 检索层契约 | monkeypatch `rag_service.load_chunks` / `load_chunks_with_states` / `_vector_ranked` / `_log_retrieval_metrics`（避免真实 Embedding 与写 `rag_metrics.jsonl`）：默认 `retrieve_chunks` 每条键集恰为 `{content, source, score}` 且**不含** `document_status`；`with_metadata=True` 才带 `chunk_id`/页码/`heading_path`/`source_spans`；完整文档富记录 `document_status == "ok"`、混合型 PDF 富记录 `document_status == "partial"`；跨目录同名同内容 `chunk_id` 集合 `isdisjoint`、重复扫描稳定；hybrid 富记录两份来源都保留且 `chunk_id` 无重复；`finally` 恢复所有被 patch 的函数 |

结果：**145 项全 PASS，0 失败，0 跳过，exit=0**（`test_document_parser.py`）。上一版 119 项全部保留（`document_id` 公式一条按 16 位新规则更新），新增 26 项来自 3.1 版本换代、3.2 加密 PDF、表格块偏移、`load_chunk_records_with_states` / `inspect_document`、`document_status`。

### 2.2 第一轮审查（P1/P2）的回归测试（保留未删）

| 审查要求 | 测试断言 |
|---|---|
| P1-1/3 路径命名空间 | `甲/讲义.txt` 与 `乙/讲义.txt` 同名同内容 → `document_id` 不同、`document_hash` 相同 |
| P1-2 同路径稳定 | 三种写法 → 同一 `document_id` |
| P1-5 `#` 分隔符安全 | 文件名含 `#`/`%` 时 `document_id` 不含 `#`；`chunk_id` 恒 3 段 |
| P1-4/检索合并 | 跨目录 `chunk_id` 互不相同；hybrid 返回两份来源 |
| P2-1/2 PDF 偏移 | `page_text[char_start:char_end] == block.text`；相邻块不重叠 |
| P2-2 坐标系自洽 | 跨页 span 用 `block_index` 回 blocks 后与所在页文本一致 |
| P2 分段函数 | `_page_pieces("第一段\n\n  第二段  ") == [("第一段",0,3),("第二段",7,10)]` |
| P2-3 DOCX 偏移口径 | 表格块 `char_start=0`、`char_end=len(text)`（**本版修正：上一版筛 `kind="table_row"` 导致断言空跑，现按实现的 `kind="table"` 先断言存在再校验**） |
| 混合型 PDF | `partial` + `missing_pages=[2]`，块页码集合 `{1,3}` |

### 2.3 第二轮审查（P1-a/P1-b/P2-a）的新增回归

| 审查要求 | 测试断言（所在文件） |
|---|---|
| P1-a 版本升级生效 | 两常量均为 `s1.2`；记录字段同步；同版本同文件 `chunk_id` 可复现；用 `s1.1` 作哈希输入得到不同 `chunk_id`（`test_document_parser.py` 3.1） |
| P1-a 路径摘要加长 | `document_id` 中段 16 位十六进制；TXT 用例按 `PATH_DIGEST_CHARS` 重算期望值（`test_document_parser.py` 第 1、3.1 节） |
| P1-b 上传透出 partial | 完整 PDF → 200 + `status=ok` + `pages_total=2/pages_with_text=2/missing_pages=[]/warnings=[]`；混合型 PDF → 200 + `status=partial` + message 含「解析不完整」+ `missing_pages=[2]` + `warnings` 一条 + 仍落盘有切片；全扫描 PDF → 400 + `status=failed` + `error_code=no_text_layer` + 未落盘（`test_upload.py`） |
| P1-b 上游契约 | `test_agent_tools.py` 的 `/upload` 结构检查改为：原四键仍在（`source`/`size_bytes`/`chunk_count`），并新增 `status`/`pages_total`/`missing_pages`/`warnings` 与失败态 `status="failed"` |
| P1-b 检索可识别 partial | 富记录带 `document_status`；默认路径键集不变（`test_document_parser.py` 第 6 节） |
| P2-a 加密 PDF 顺序 | 普通 / 空口令 / 打开密码三种 PDF 的隔离用例（`test_document_parser.py` 3.2）；`password_required` 独立状态码 |

### 2.4 修改 `test_upload.py`

1. 被拒类型用例：「目前只支持 .txt 文件」→「目前只支持 .txt / .pdf / .docx 文件」，被拒后缀从 `pdf/docx/png/jpg` 改为 `md/xlsx/png/jpg`（pdf/docx 现在是合法上传类型）。属**有意的接口契约变更**，已在设计文档 §8 与 PR 说明中标出。
2. 新增三个 PDF 上传 API 级用例（完整 / 混合型 / 全扫描），上传后立即从 `knowledge/` 删除并纳入 `CREATED_FILES` 清理清单。本轮该文件 **47 项 PASS，exit=0**。

### 2.5 未改动但承担回归职责的既有测试

`test_document_service.py`（TXT 兼容 + 上传）、`test_rag_service.py`（检索默认结构）、`test_rag_hybrid.py`（Hybrid 融合与三字段契约）、`test_agent_tools.py`（`search_knowledge` 三字段 + `/upload` 结构）、`test_memory_grounding.py`（`_sanitize_chunk` 三字段 + 真实 db 字节不变）、`test_context_builder.py`（API `sources` 仍为原始检索结果）、`test_vector_cache.py`（不污染真实 `.rag_cache/`）。

---

## 3. 全量回归结果（本分支 46 个 `test_*.py`）

| 结果 | 数量 | 说明 |
|---|---|---|
| exit=0 | **45** | 含 `test_document_parser.py`（145 项）、`test_upload.py`（47 项）与改过的 `test_agent_tools.py` |
| exit=1 | **1** | `test_feedback_plan_link.py`（16 用例：15 过 1 失败，与基线同因） |
| 合计 | **46** | 无超时、无崩溃、无残留进程 |

文件数说明：本分支基于 `c0c425b`，该点尚不包含 `test_graduation_checks.py` 与 `test_graduation_blind_pack.py`（两者是后续盲评工具链分支新增的，只存在于 main 的更新提交里），因此本分支的全量回归基数是 **46** 个文件，不是 main 当前的 47 个。

### 3.1 唯一失败的定性（已证明与本分支无关）

- 失败用例：`test_get_feedback_since_returns_plan_id_and_keeps_clamping -> AssertionError:`
- 验证方法：另开一个基线 worktree，直接检出 `c0c425b`（本分支的起点，未含任何 S1 改动）运行同一文件。
- 基线结果：**同样 15 过 1 失败，exit=1**，同一用例、同一报错。
- 结论：该失败在基线已存在，S1 未引入也未修复；验证用 worktree 已 `git worktree remove --force` 清理。
- 处置：本 PR 不修（超出 S1 范围、且会动 feedback 计划关联逻辑），留待独立任务。

### 3.2 未验证 / 跳过项（不计入通过）

| 项 | 状态 | 原因 |
|---|---|---|
| `test_real_api_smoke.py` | `[SKIP]`，exit=0 | 缺 `LLM_API_KEY` / `EMBEDDING_API_KEY`，按 `real_backend` 约定跳过 |
| 另外 9 个含真实 API/Embedding 章节的测试（`test_agent_loop`、`test_agent_plan_draft`、`test_embedding`、`test_openai`、`test_plan_frontend_flow`、`test_rag_service` 真实章节、`test_save_plan_tool`、`test_upload` 真实章节、`test_document_service` 语义检索章节） | 对应章节按 `real_backend._skip` 跳过 | 同上，本次不联网、不调真实模型（逐个日志确认过） |
| `test_document_service.py` 中「保存文件后的语义检索」 | SKIP | Embedding 不可用：`使用 openai_compatible Embedding 时必须设置 EMBEDDING_API_KEY` |
| PDF 真实教材/论文语料 | 未验证 | 只用 reportlab 合成 PDF；多栏排版、无空行页面的解析质量未在真实语料上评估 |
| 大规模知识库性能 | 未验证 | 现有知识库很小，未做容量/耗时基准 |

---

## 4. 验收表

### 4.1 已实现（代码存在，行为已按设计文档冻结）

| # | 要求 | 实现位置 |
|---|---|---|
| 1 | 统一 15 字段 chunk 数据契约 | `document_chunker.CHUNK_FIELDS` / `Chunk.to_record()` |
| 2 | 文件级 `document_hash` + `document_id`（安全文件名 + 16 位路径摘要 + 内容哈希前 8 位；不含解析器/切片器版本，版本只进入 `chunk_id` 的 `h`） | `document_parser.document_id_for` / `ParsedDocument` |
| 3 | 可复现、非下标式的 `chunk_id` | `document_chunker.make_chunk_id`（含 `occurrence`） |
| 4 | PDF 物理页码 + 印刷页码分离 | `_parse_pdf` / `_printed_page_number` |
| 5 | PDF 跨页 chunk 记录完整来源范围 | `_span_of` + `source_spans` |
| 6 | DOCX 按标题/段落/表格定位、不虚构页码 | `_parse_docx` / `_docx_heading_level` |
| 7 | 扫描件识别为「无文本层」而非静默空内容 | `ParseError(code="no_text_layer")` |
| 8 | 明确错误码 + 状态牌，不静默丢弃 | `STATUS_BY_CODE` / `scan_knowledge` |
| 9 | 加密 PDF 先判 `is_encrypted` 再按 `decrypt("")` 成败，失败报 `password_required` | `_parse_pdf` |
| 10 | TXT 切片逐字节兼容 | `txt_splitter=split_into_chunks` |
| 11 | 富元数据显式开启，默认结构不变 | `with_metadata` / `load_chunk_records` |
| 12 | 文档级状态（`ok`/`partial`/失败）同时供上传与检索使用 | `inspect_document` / `load_chunk_records_with_states` / `retrieve_chunks` 的 `document_status` |
| 13 | 上传支持三后缀、先解析校验后落盘、如实报告 partial | `main.py` `/upload` + `document_service.save_document` |
| 14 | 依赖声明 | `requirements.txt` 新增 `pypdf`、`python-docx` |

### 4.2 已测试通过（有断言支撑，见 §2）

- `chunk_id` 复现性、内容/配置/版本变化导致的 id 换代（含 `s1.1`→`s1.2` 换代）。
- `document_id` 含 16 位路径摘要：不同目录同名同内容得到不同 ID（16 位即 64 bit 截断哈希，碰撞概率极低，但不是数学保证），同一路径不同写法归一，`#` 分隔符安全编码。
- 版本常量只影响 `chunk_id`：`PARSER_VERSION` / `CHUNKER_VERSION` 进入 `h` 的哈希输入，本身不进入 `document_id`。`s1.1`→`s1.2` 时 `document_id` 确实变了，但原因是对路径摘要从 8 位加长到 16 位，不是版本号参与计算。
- PDF 单页多段真实字符偏移、跨页各页独立 span、DOCX 块内偏移口径与表格块 span 对齐。
- 混合型 PDF 的 `partial` 状态与 `missing_pages`/`warnings` 记录（扫描层、`inspect_document`、`/upload`、检索 `document_status` 四处一致）。
- 加密 PDF 三态：普通 / 空口令可解析 / 打开密码报 `password_required`。
- PDF 物理/印刷页码、DOCX 标题层级与表格块、TXT 兼容与 char 区间。
- 六个错误码 + `scan_knowledge` 状态与统计字段（含失败文件可见性）。
- 落盘幂等、坏文件不落盘、后缀白名单。
- 默认返回结构三字段/两字段不被富元数据污染（`retrieve_chunks`、`load_chunks`、`load_documents`）。
- 全量回归 46 个文件：45 个 exit=0，唯一失败已证基线既有。

### 4.3 未验证（明确不做假设）

- 真实 PDF/DOCX 教材语料的解析质量（只有合成夹具）。
- 多栏 PDF 的阅读顺序、以及「按行切分」在真实教材上的段落合并质量（设计文档 §10 已知限制）。
- 混合型 PDF 缺失页的 OCR 补全（本阶段明确不做，只记录状态）。
- 带打开密码 PDF 的自动解密（明确不支持，返回 `password_required`）。
- 大知识库下的解析与检索性能。
- 任何真实模型/Embedding 参与的端到端问答（本任务禁止联网调模型）。
- `agent_tools.search_knowledge` 的定位透出（本阶段未改，见设计文档 §11）。

---

## 5. 变更文件清单

### 5.1 新增

| 文件 | 行数量级 | 说明 |
|---|---|---|
| `document_parser.py` | ~660 | 三格式解析、`ParseError`、错误码与状态映射、加密 PDF 判断、`document_id` |
| `document_chunker.py` | ~300 | 15 字段 `Chunk`、`chunk_id` 算法、切片规则 |
| `test_document_parser.py` | ~930 | S1 主测试（145 项断言） |
| `docs/RAG-DOCUMENT-PARSING-DESIGN.md` | — | 数据契约与来源定位设计说明（v1.3） |
| `docs/RAG-DOCUMENT-PARSING-S1-TEST-REPORT.md` | — | 本报告 |

### 5.2 修改

| 文件 | 改动 |
|---|---|
| `document_service.py` | 整份重构：三后缀白名单、`_iter_parsed` 统一解析、`load_chunk_records`、`load_chunk_records_with_states`、`inspect_document`、`chunk_file`、`scan_knowledge` 新结构、`save_document` 支持 bytes 且先校验后落盘、`DocumentError` 带 `code`；TXT 旧函数原样保留 |
| `rag_service.py` | `with_metadata` 开关、`_chunk_key` 去重键、`_result_entry`、`load_chunk_records` / `load_chunks_with_states` 转发、富结果 `document_status`；默认输出键集不变 |
| `main.py` | `/upload` 放开三后缀、按 `inspect_document` 返回文档级状态与页数、partial 不报完整成功、失败返回 `status=failed` + `error_code` |
| `test_upload.py` | 被拒类型用例同步新文案与新后缀集合；新增完整 / 混合型 / 全扫描 PDF 三个 API 级用例 |
| `test_agent_tools.py` | `/upload` 结构检查改为兼容旧四键并断言新增文档级状态字段 |
| `requirements.txt` | 新增 `pypdf>=5.0,<8.0`、`python-docx>=1.1,<2.0` |

### 5.3 明确未纳入提交

- worktree 内的测试产物（`_test_*.db`、`_test_cache_upload/`、`_test_upload_server.log`、回归日志）均未跟踪，不提交
- 任何正式实验 run 文件、`feedback.db`、`.rag_cache/`、`rag_metrics.jsonl` 的真实数据

---

## 6. 复现步骤

```powershell
$env:PYTHONIOENCODING = "utf-8"
$py = "F:\somethingProject\dsbot\.venv\Scripts\python.exe"
cd C:\Users\BOSS\AppData\Local\Temp\dsbot-rag-s1

# S1 主测试（145 项）
& $py -X utf8 test_document_parser.py

# 受影响的既有测试
& $py -X utf8 test_document_service.py
& $py -X utf8 test_rag_service.py
& $py -X utf8 test_rag_hybrid.py
& $py -X utf8 test_upload.py
& $py -X utf8 test_agent_tools.py
& $py -X utf8 test_vector_cache.py
& $py -X utf8 test_llamaindex_rag.py

# 全量回归（逐个跑，按退出码统计；本分支 46 个文件）
Get-ChildItem test_*.py | ForEach-Object { & $py -X utf8 $_.Name; "exit=$LASTEXITCODE $($_.Name)" }
```
