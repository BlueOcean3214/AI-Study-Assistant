# S1 文档解析与来源定位 · 测试报告

> 生成时间：2026-11-18｜分支：`rag/document-parsing-s1`（基线 `c0c425b`，即当时 `origin/main`）
> 本版为 v1.2（第一轮独立审查 P1/P2 修复后重跑）。上一版 v1.1 记录 81 项断言。
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
| 测试执行方式 | 仓库没有 pytest/统一 runner，47 个根目录 `test_*.py` 逐个 `python -X utf8 test_xxx.py` 运行，按退出码判定 |

---

## 2. 本轮新增 / 修改的测试

### 2.1 新增 `test_document_parser.py`（S1 主测试，119 项断言）

单文件手写 runner：`check(name, condition, detail)` 记 pass/fail、`skip(name, reason)` 记跳过、`expect_parse_error(name, expected_code, func)` 断言错误码；结束时按 fail 数 `SystemExit(1)`。

夹具全部现场合成：

- `build_pdf(pages)`：reportlab + `UnicodeCIDFont("STSong-Light")` 生成多页中文 PDF，每页正文 + 页脚 `- 1 -` 形式的印刷页码。
- `build_blank_pdf()`：两页空白，用来制造「无文本层」。
- `build_docx()`：Heading 1「第一章 导数」+ 正文 + Heading 2「1.1 定义」+ 2×2 表格。

覆盖矩阵：

| 小节 | 验证点 |
|---|---|
| TXT | 解析内容、`document_hash`、`document_id`、`chunk_id` 两次解析完全一致；改内容后 `document_hash/document_id/chunk_id` 全变；改 `max_chars` 后 `chunk_id` 变；重复段落仍生成唯一 `chunk_id`；TXT 不虚构页码与标题路径；TXT 整篇一个 `kind="text"` 块 |
| PDF | 物理页码 `[1,2]`、印刷页码 `[1,2]`；跨页片段 `locator` 含 `-`、`source_spans` 覆盖两页；正文包含两页文字；两次解析结果一致 |
| DOCX | `page_number` 恒 `None`；`paragraph_index` 齐全；`heading_path` 为 2 级；`chunk_id` 用 `para` 定位；表格行文本含 `" \| "` |
| 错误码 | `unsupported_type` / `empty_file` / `decode_failed` / `corrupted_pdf` / `no_text_layer` / `corrupted_docx` 六个码逐一命中，`to_issue()` 结构正确 |
| 落盘与统计 | `save_document` 三种格式成功且磁盘字节与输入一致；坏 PDF 抛 `DocumentError` 且临时目录文件列表前后不变（**坏文件不落盘**）；`.png` 被拒且文案含三后缀；`chunk_file` 三种格式切片数与直接切片一致；`load_chunk_records` 覆盖三格式、15 字段齐全且可 JSON 序列化；`load_chunks` 默认键集恰为 `{content, source}`；`scan_knowledge` 3 文档、无 issue、`chunk_count` 与切片数一致 |
| `scan_knowledge` 状态 | 三份好文件 status 均为 `ok`、`bytes` 与磁盘一致、解析器/切片器版本已写入；坏 PDF status=`failed`、扫描件 status=`no_text_layer`；失败文档 `chunk_count=0` 且 `document_id=None`；失败文档带正确 `error_code`；失败文件同时出现在 `issues`；扫描件不产出任何切片 |
| 检索层契约 | monkeypatch `rag_service.load_chunks` / `_vector_ranked` / `_log_retrieval_metrics`（避免真实 Embedding 与写 `rag_metrics.jsonl`）：默认 `retrieve_chunks` 每条键集恰为 `{content, source, score}`；只有 `with_metadata=True` 才带 `chunk_id` / `page_number` / `heading_path` / `source_spans`；`finally` 恢复被 patch 的函数 |

结果演变：首轮 70/71（唯一失败是我把「TXT 会产出 `kind="heading"` 块」写进了断言，实现本就不识别 TXT 标题，属测试写错，已改成正确断言）→ v1.1 终版 81 项全 PASS，0 失败，0 跳过，exit=0。
→ v1.2（P1/P2 修复后）扩到 **119 项，全 PASS，0 失败，0 跳过，exit=0**，原有 81 项断言除 `document_id` 公式一条按新规则更新外全部保留。

### 2.2 第一轮审查（P1/P2）修复新增的回归测试

覆盖点按审查意见逐条对应：

| 审查要求 | 测试断言 |
|---|---|
| P1-1/3 路径命名空间 | `甲/讲义.txt` 与 `乙/讲义.txt` 同名同内容 → `document_id` 不同、`document_hash` 相同 |
| P1-2 同路径稳定 | `"甲\\讲义.txt"`、`"./甲//讲义.txt"`、`"甲/讲义.txt"` 三种写法 → 同一 `document_id` |
| P1-5 `#` 分隔符安全 | 文件名 `a#b%23#c.txt` → `document_id` 不含 `#`；每个 `chunk_id.split("#")` 恰好 3 段且首段等于 `document_id` |
| P1-4/检索合并 | 两个目录同名同内容 PDF 落盘后 `chunk_id` 集合 `isdisjoint`；重复扫描 `chunk_id` 稳定；hybrid 模式下 `retrieve_chunks` 返回两份来源、结果条数等于记录数、`chunk_id` 无重复 |
| P2-1/2 PDF 单页多段偏移 | 单页三段 PDF：`blocks[1:]` 的 `char_start > 0`，`page_text[char_start:char_end] == block.text`，相邻块 `char_end <= 下一块 char_start` |
| P2-2 偏移坐标系自洽 | 跨页片段的每条 span 用 `block_index` 回 blocks 后，与该 span 所在页的 `extract_text()` 归一化文本切片一致，且 span 页码与块页码一致 |
| P2 分段函数 | `_page_pieces("第一段\n\n  第二段  ")` == `[("第一段",0,3),("第二段",7,10)]` |
| P2-3 DOCX 偏移口径 | 所有 DOCX 块 `char_start == 0` 且 `char_end == len(text)`；`table_row` 块同样自洽 |
| 混合型 PDF | `pages_total=3 / pages_with_text=2 / missing_pages=[2] / status="partial"`，块页码集合 `{1,3}`；对照完整 PDF 为 `ok` 且 `missing_pages=[]` |
| 混合型 PDF 落盘 | `scan_knowledge` 里 `status="partial"`、`warnings` 恰一条 `{warning_code:"missing_pages", page_numbers:[2]}`、`issues` 为空、`chunk_count >= 1` |

测试隔离：全部在 `tempfile.mkdtemp(prefix="s1-parser-test-")` 内造合成 PDF/TXT/DOCX，`finally` 中 `shutil.rmtree` 并断言目录已消失；检索用例 monkeypatch `rag_service.load_chunks/_vector_scored/_bm25_ranked/_log_retrieval_metrics`，`finally` 恢复函数与 `RAG_RETRIEVAL_MODE`，不触真实 Embedding、不写 `.rag_cache/`。

### 2.3 修改 `test_upload.py`

被拒类型的用例：「目前只支持 .txt 文件」→「目前只支持 .txt / .pdf / .docx 文件」，被拒后缀从 `pdf/docx/png/jpg` 改为 `md/xlsx/png/jpg`（pdf/docx 现在是合法上传类型）。这是**有意的接口契约变更**，已在设计文档 §8 与 PR 说明中标出。

### 2.4 未改动但承担回归职责的既有测试

`test_document_service.py`（TXT 兼容 + 上传）、`test_rag_service.py`（检索默认结构）、`test_rag_hybrid.py`（Hybrid 融合与三字段契约）、`test_agent_tools.py`（`search_knowledge` 三字段）、`test_memory_grounding.py`（`_sanitize_chunk` 三字段 + 真实 db 字节不变）、`test_context_builder.py`（API `sources` 仍为原始检索结果）、`test_vector_cache.py`（不污染真实 `.rag_cache/`）。

---

## 3. 全量回归结果（本分支 46 个 `test_*.py`）

v1.2 复跑（P1/P2 修复后）结果：

| 结果 | 数量 | 说明 |
|---|---|---|
| exit=0 | **45** | 含本轮新增的 `test_document_parser.py`（119 项断言）与被改动的 `test_upload.py` |
| exit=1 | **1** | `test_feedback_plan_link.py`（16 用例：15 过 1 失败，与基线同因） |
| 合计 | 46 | 无超时、无崩溃、无残留进程 |

文件数说明：本分支基于 `c0c425b`，该点尚不包含 `test_graduation_checks.py` 与 `test_graduation_blind_pack.py`（两者是后续盲评工具链分支新增的，只存在于 main 的更新提交里），因此本分支的全量回归基数是 **46** 个文件，不是 main 当前的 47 个。

### 3.1 唯一失败的定性（已证明与本分支无关）

- 失败用例：`test_get_feedback_since_returns_plan_id_and_keeps_clamping -> AssertionError:`
- 验证方法：另开一个基线 worktree，直接检出 `c0c425b`（本分支的起点，未含任何 S1 改动）运行同一文件。
- 基线结果：**同样 15 过 1 失败，exit=1**，同一用例、同一报错。
- 结论：该失败在基线已存在，S1 未引入也未修复；验证用 worktree 已 `git worktree remove --force` 清理，`git worktree list` 仅剩主仓库与既有功能 worktree。
- 处置：本 PR 不修（超出 S1 范围、且会动 feedback 计划关联逻辑），留待独立任务。

### 3.2 未验证 / 跳过项（不计入通过）

| 项 | 状态 | 原因 |
|---|---|---|
| `test_real_api_smoke.py` | `[SKIP]`，exit=0 | 缺 `LLM_API_KEY` / `EMBEDDING_API_KEY`，按 `real_backend` 约定跳过 |
| 另外 9 个含真实 API/Embedding 章节的测试（`test_agent_loop`、`test_agent_plan_draft`、`test_embedding`、`test_openai`、`test_plan_frontend_flow`、`test_rag_service` 真实章节、`test_save_plan_tool`、`test_upload` 真实章节、`test_document_service` 语义检索章节） | 对应章节按 `real_backend._skip` 跳过 | 同上，本次不联网、不调真实模型（v1.2 复跑日志逐个确认过） |
| `test_document_service.py` 中「保存文件后的语义检索」 | SKIP | Embedding 不可用：`使用 openai_compatible Embedding 时必须设置 EMBEDDING_API_KEY` |
| PDF 真实教材/论文语料 | 未验证 | 只用 reportlab 合成 PDF；多栏排版、无空行页面的解析质量未在真实语料上评估 |
| 大规模知识库性能 | 未验证 | 现有知识库很小，未做容量/耗时基准 |

---

## 4. 验收表

### 4.1 已实现（代码存在，行为已按设计文档冻结）

| # | 要求 | 实现位置 |
|---|---|---|
| 1 | 统一 15 字段 chunk 数据契约 | `document_chunker.CHUNK_FIELDS` / `Chunk.to_record()` |
| 2 | 文件级 `document_hash` + 版本化 `document_id` | `document_parser.document_id_for` / `ParsedDocument` |
| 3 | 可复现、非下标式的 `chunk_id` | `document_chunker.make_chunk_id`（含 `occurrence`） |
| 4 | PDF 物理页码 + 印刷页码分离 | `_parse_pdf` / `_printed_page_number` |
| 5 | PDF 跨页 chunk 记录完整来源范围 | `_span_of` + `source_spans`（跨页时多 span） |
| 6 | DOCX 按标题/段落/表格定位、不虚构页码 | `_parse_docx` / `_docx_heading_level` |
| 7 | 扫描件识别为「无文本层」而非静默空内容 | `ParseError(code="no_text_layer")` |
| 8 | 明确错误码 + 状态牌，不静默丢弃 | `STATUS_BY_CODE` / `scan_knowledge` |
| 9 | TXT 切片逐字节兼容 | `txt_splitter=split_into_chunks` |
| 10 | 富元数据显式开启，默认结构不变 | `with_metadata` / `load_chunk_records` |
| 11 | 上传支持三后缀并通过解析校验后落盘 | `main.py` `/upload` + `document_service.save_document` |
| 12 | 依赖声明 | `requirements.txt` 新增 `pypdf`、`python-docx` |

### 4.2 已测试通过（有断言支撑，见 §2）

- chunk_id 复现性、内容/配置/版本变化导致的 id 换代。
- document_id 含路径命名空间：不同目录同名同内容不碰撞，同一路径不同写法归一，`#` 分隔符安全编码。
- PDF 单页多段的真实字符偏移、跨页各页独立 span、DOCX 块内偏移口径。
- 混合型 PDF 的 `partial` 状态与 `missing_pages`/`warnings` 记录（不静默当成功）。
- PDF 物理/印刷页码、跨页 span、DOCX 标题层级与表格行、TXT 兼容与 char 区间。
- 六个错误码 + `scan_knowledge` 状态与统计字段（含失败文件可见性）。
- 落盘幂等、坏文件不落盘、后缀白名单。
- 默认返回结构三字段/两字段不被富元数据污染（`retrieve_chunks`、`load_chunks`、`load_documents`）。
- 全量回归 46/47 exit=0（唯一失败已证基线既有）。

### 4.3 未验证（明确不做假设）

- 真实 PDF/DOCX 教材语料的解析质量（只有合成夹具）。
- 多栏 PDF 的阅读顺序、以及「按行切分」在真实教材上的段落合并质量（已知限制，设计文档 §10）。
- 混合型 PDF 缺失页的 OCR 补全（本阶段明确不做，只记录状态）。
- 大知识库下的解析与检索性能。
- 任何真实模型/Embedding 参与的端到端问答（本任务禁止联网调模型）。
- `agent_tools.search_knowledge` 的定位透出（本阶段未改，见设计文档 §11）。

---

## 5. 变更文件清单

### 5.1 新增

| 文件 | 行数量级 | 说明 |
|---|---|---|
| `document_parser.py` | ~524 | 三格式解析、`ParseError`、错误码与状态映射 |
| `document_chunker.py` | ~278 | 15 字段 `Chunk`、`chunk_id` 算法、切片规则 |
| `test_document_parser.py` | ~713 | S1 主测试（119 项断言） |
| `docs/RAG-DOCUMENT-PARSING-DESIGN.md` | — | 数据契约与来源定位设计说明（v1.1） |
| `docs/RAG-DOCUMENT-PARSING-S1-TEST-REPORT.md` | — | 本报告 |

### 5.2 修改

| 文件 | 改动 |
|---|---|
| `document_service.py` | 整份重构：三后缀白名单、`_iter_parsed` 统一解析、`load_chunk_records`、`chunk_file`、`scan_knowledge` 新结构、`save_document` 支持 bytes 且先校验后落盘；TXT 旧函数原样保留 |
| `rag_service.py` | `with_metadata` 开关、`_chunk_key` 去重键、`_result_entry`、`load_chunk_records` 转发；默认输出键集不变 |
| `main.py` | `/upload` 放开三后缀、chunk_count 改用 `chunk_file` 统一口径 |
| `test_upload.py` | 被拒类型用例同步新文案与新后缀集合 |
| `requirements.txt` | 新增 `pypdf>=5.0,<8.0`、`python-docx>=1.1,<2.0` |

### 5.3 明确未纳入提交

- `_test_feedback_memory_unit.db`（worktree 内测试产物，未跟踪，不提交）
- 任何正式实验 run 文件、`feedback.db`、`.rag_cache/`、`rag_metrics.jsonl` 的真实数据

---

## 6. 复现步骤

```powershell
$env:PYTHONIOENCODING = "utf-8"
$py = "F:\somethingProject\dsbot\.venv\Scripts\python.exe"
cd C:\Users\BOSS\AppData\Local\Temp\dsbot-rag-s1

# S1 主测试
& $py -X utf8 test_document_parser.py

# 受影响的既有测试
& $py -X utf8 test_document_service.py
& $py -X utf8 test_rag_service.py
& $py -X utf8 test_rag_hybrid.py
& $py -X utf8 test_upload.py

# S1 专项（119 项）与受影响的 RAG 测试
& $py -X utf8 test_document_parser.py
& $py -X utf8 test_document_service.py
& $py -X utf8 test_rag_service.py
& $py -X utf8 test_rag_hybrid.py
& $py -X utf8 test_upload.py
& $py -X utf8 test_vector_cache.py
& $py -X utf8 test_llamaindex_rag.py

# 全量回归（逐个跑，按退出码统计；本分支 46 个文件）
Get-ChildItem test_*.py | ForEach-Object { & $py -X utf8 $_.Name; "exit=$LASTEXITCODE $($_.Name)" }
```
