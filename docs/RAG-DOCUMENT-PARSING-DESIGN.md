# S1 文档解析与来源定位设计说明

> 版本：v1.3（2026-11-18，第二轮独立审查修复后修订：解析器/切片器升到 `s1.2`、`document_id` 路径摘要 8→16 位、加密 PDF 判断顺序与 `password_required`、`/upload` 透出文档级状态、富元数据检索透出 `document_status`；2026-11-19 第三轮审查后仅修正 `document_id` 语义表述——不含版本号、截断摘要不声称数学唯一，算法与常量未改）｜分支：`rag/document-parsing-s1`（基线 `origin/main` = `c0c425b`）
> 范围：TXT / 可提取文本的 PDF / DOCX 的解析、切片、chunk_id 与来源定位。不含 OCR、不含 Rerank、不改长期记忆逻辑。
> 本文档先落数据契约，再描述实现；所有描述均对应当前分支已提交的代码，未实现的内容集中在 §11「后续工作」，不写成本阶段已具备。

---

## 1. 目标与非目标

### 1.1 目标

1. 同一套解析/切片管线处理 TXT、PDF、DOCX，产出统一 `Chunk` 记录（15 个字段，见 §2）。
2. 每个 chunk 能回答三个问题：**来自哪份文件的哪个版本**（`document_id` / `document_hash`）、**来自哪一页/哪一段**（`page_number` / `paragraph_index` / `heading_path`）、**原始字符范围是什么**（`source_spans`）。
3. `chunk_id` 可复现、不使用数组下标、源文件变化后自动换代。
4. PDF 无文本层（扫描件）被**明确识别并标记**（`ParseError(code="no_text_layer")`、`scan_knowledge` 状态 `no_text_layer`），不静默产出空内容。
5. 解析失败有明确错误码与状态记录（`ParseError.code` + `scan_knowledge.documents[].status/error_code/error_message`），不静默丢弃。
6. 原有 TXT 切片结果**逐字节不变**，向量缓存、BM25 索引、Hybrid 行为不受影响。

### 1.2 非目标（本阶段不做）

- OCR / 扫描版 PDF 的文本识别（只做识别与标记）。
- 表格结构还原为结构化数据（只提取单元格文本，用 `" | "` 保留行列位置）。
- 跨文档实体对齐、Chunk 语义去重、Rerank。
- 修改 `agent_tools.search_knowledge()` 的返回结构（见 §11）。
- 修改 `docs/GRADUATION-EXPERIMENT-DESIGN.md`（v4.5）与任何正式实验数据。

---

## 2. 数据契约（Chunk 记录，15 字段）

字段常量定义在 `document_chunker.CHUNK_FIELDS`，`Chunk.to_record()` 负责转成 JSON 可序列化 dict。

| # | 字段 | 类型 | 含义 | 生成规则 |
|---|---|---|---|---|
| 1 | `document_id` | str | 文档稳定标识（安全文件名 + 16 位路径摘要 + 内容哈希前缀；**不含解析器/切片器版本**） | `f"{安全文件名}-{路径摘要}-{document_hash[:8]}"`（`document_parser.document_id_for`）；路径摘要 = `sha256(规范化相对路径)[:16]`（`PATH_DIGEST_CHARS = 16`，s1.2 起由 8 位放宽到 16 位）；内容或相对路径任一变化即变。`PARSER_VERSION` / `CHUNKER_VERSION` 不参与 `document_id`，只进入 `chunk_id` 的哈希输入（见 §3、§7） |
| 2 | `document_hash` | str | 源文件字节 SHA-256（64 位小写十六进制） | `hashlib.sha256(raw_bytes).hexdigest()`，按原始字节算，与编码无关 |
| 3 | `source_name` | str | 知识库内相对路径（正斜杠） | 如 `probability/条件概率.pdf` |
| 4 | `file_type` | str | `txt` / `pdf` / `docx` | 由后缀小写决定 |
| 5 | `chunk_id` | str | 切片永久标识 | 见 §3 |
| 6 | `chunk_index` | int | 文档内**顺序**（0 起） | 仅用于排序/展示，**不参与身份** |
| 7 | `content` | str | 切片正文 | TXT 沿用旧切片算法；PDF/DOCX 见 §4 |
| 8 | `content_hash` | str | 正文 SHA-256（64 位） | `compute_content_hash()`：换行统一 `\n`、去每行行尾空白、整体 strip 后取 SHA-256 |
| 9 | `page_number` | int \| null | **物理页码**（1 起） | PDF 取 PDF 页序号；TXT/DOCX 为 `null`（不虚构页码） |
| 10 | `printed_page_number` | int \| null | **印刷页码** | PDF 从页首/页尾两行识别「- 12 -」「第 12 页」，识别不到为 `null`；TXT/DOCX 恒为 `null` |
| 11 | `heading_path` | list[str] | 该片首块的标题链 | DOCX 由标题样式层级推导（可多级）；PDF 为启发式识别的最近一个标题（0 或 1 项）；TXT 恒为空列表 |
| 12 | `paragraph_index` | int \| null | 该片首块的文档内序号（0 起） | DOCX/PDF 为解析序；TXT 为切片序 |
| 13 | `source_spans` | list[dict] | 来源范围（跨页时多条） | 每条 `{block_index, kind, page_number, paragraph_index, char_start, char_end, heading_path}`，按块顺序排列 |
| 14 | `parser_version` | str | 解析器版本 | 当前 `"s1.2"`；解析行为变更必须升版 |
| 15 | `chunker_version` | str | 切片器版本 | 当前 `"s1.2"`；切片规则变更必须升版 |

补充约束：

- **`document_id` 带路径命名空间，但不含版本**：只靠「文件名 + 内容哈希」会让两个不同目录下的同名同内容文件落到同一 ID，进而让 `chunk_id` 也相同、让 `rag_service` 的按 `chunk_id` 去重把两个来源合并掉。因此 ID 里必须有规范化相对路径（`normalize_source_name`：反斜杠→`/`、`posixpath.normpath`、首尾空白与 `./` 归一）。同一相对路径 + 同一内容 → ID 稳定，与 `PARSER_VERSION` / `CHUNKER_VERSION` 无关；解析或切片规则升级只改 `chunk_id` 的 `h`，不会改 `document_id`。
- **截断摘要提供的是极低碰撞概率，不是数学唯一**：路径摘要是 16 位十六进制（64 bit）截断哈希，`chunk_id` 里的 `h` 是 10 位十六进制（40 bit）截断哈希。不同相对路径得到同一摘要是极小概率事件（约 2⁻⁶⁴ 量级），不能声称「绝不碰撞」；同一文档内 `chunk_id` 首先由 `locator` 区分，只有 `locator` 相同（重复内容）的片才依赖 `h` 与 `occurrence`，此处的截断碰撞概率约 2⁻⁴⁰，实际可忽略但不为零。跨文档溯源应以 `source_name` 为准，`document_id` 是稳定标识而非防伪凭据。
- **ID 分隔符安全编码**：`chunk_id = document_id#locator#h` 用 `#` 做分隔符，而文件名允许 `#`/`%`。`document_chunker._encode_id_component()` 对 `document_id` 与 `locator` 先 `%`→`%25`、`#`→`%23`，保证 `chunk_id.split("#")` 恒为 3 段、前缀恒等于未编码的 `document_id`。
- **印刷页码不写进 `page_number`**：两者含义不同（PDF 物理页序 vs 书页上印的号），混用会破坏跨版本对照。
- `source_spans` 在实现中**每片至少一条**（片由块组成，块必有定位），因此不存在「空 span 的降级路径」，也不需要额外的降级标记字段。
- **字符偏移坐标系按文件类型固定**（`char_start`/`char_end` 只在该坐标系内可解释）：PDF = 当前页 `extract_text()` 后经 `_normalize_text` 的整页文本（`\r\n`/`\r`→`\n`、首尾 strip）；DOCX = 块自身文本内偏移（段落级 / 表格行级，恒为 `0..len(block.text)`，不跨块解释）；TXT = 整篇规范化文本。
- 所有字段值为 JSON 可序列化类型；`to_record()` 把 `heading_path` / `source_spans` 转成 list。
- `load_chunk_records()` / `rag_service.retrieve_chunks(with_metadata=True)` 的记录在此基础上**额外带一个历史键 `source`**（值等于 `source_name`），方便旧消费方只改字段不改读法。

---

## 3. chunk_id 生成算法

```
occurrence = 该 (locator, content_hash) 在本文档内第几次出现（按布局顺序，从 0 计）
locator    = 结构化定位键（见 §4）
h          = sha256(f"{CHUNKER_VERSION}|{PARSER_VERSION}|{max_chars}|{document_hash}|{locator}|{occurrence}|{content_hash}")[:10]
chunk_id   = f"{document_id}#{locator}#{h}"
```

示例：`条件概率-3f2a91bc4d5e6f70-9d1e00aa#p12#7c1d90e3a4`（`条件概率`=安全文件名，`3f2a91bc4d5e6f70`=16 位路径摘要，`9d1e00aa`=`document_hash[:8]`）

`h` 是 10 位十六进制（40 bit）截断摘要，`document_id` 里的路径摘要是 16 位（64 bit）；两者用于区分而不是用于防伪，只能表述为「碰撞概率极低」，不能表述为数学上绝对唯一。版本号（`PARSER_VERSION` / `CHUNKER_VERSION`）只出现在 `h` 的哈希输入里，不出现在 `document_id` 中。

性质与理由：

| 要求 | 满足方式 |
|---|---|
| 相同源文件 + 相同解析/切片版本 + 相同配置下可复现 | 输入全部是确定性量：文件字节哈希、两个版本号、`max_chars`、定位键、出现序号、正文哈希 |
| 不用数组下标做永久 id | `chunk_index` 不参与；重复内容靠 `occurrence`（由布局顺序推出，不是数组位置）区分 |
| 源文件修改可识别 | `document_hash` 同时进入 `document_id` 与 `h`，文件一改，全部 chunk_id 换代 |
| 解析/切片规则改动可追溯 | `parser_version` / `chunker_version` / `max_chars` 进入 `h`，规则升级即换代 |
| 同一文档内唯一 | `locator + occurrence` 唯一；`occurrence` 让重复段落也能区分 |
| 人工可读、可定位 | `document_id` 带文件名，`locator` 带页码/段落号 |

**显式取舍**：`content_hash` 参与计算，因此「id 相同 ⇒ 内容相同」成立；代价是正文编辑后 id 换代——正符合来源追溯需求（旧引用指向旧版本，不会被新内容冒充）。

---

## 4. 各格式的定位规则

### 4.1 PDF

- 解析器：`pypdf`（惰性导入 `PdfReader`，缺失时报 `parser_unavailable`）。
- 物理页码：`enumerate(reader.pages, start=1)`，**一律以 PDF 页对象顺序为准**。
- 印刷页码：`_printed_page_number(page_text)` 只在页首两行、页尾两行里匹配 `^\s*[-—–]\s*(\d{1,4})\s*[-—–]\s*$` 与 `^\s*第\s*(\d{1,4})\s*页\s*$`；识别不到为 `None`，不影响解析。
- 页内分段（`_page_pieces`）：优先按空行（`\n\s*\n`）切段；**pypdf 默认抽取会丢掉空行**，整页只剩一段时退化为按行切分（每个非空行一个块）。两种分支都保留段落在**整页规范化文本**中的真实起止位置，不再一律从 0 起算。
- 字符偏移（`char_start`/`char_end`）：相对**该页 `_normalize_text(extract_text())` 后的整页文本**。校验方式：`page_text[char_start:char_end] == block.text`（有前导空白的段落，偏移指向 strip 后的正文起点）。跨页片段的每个 span 各自归属自己那一页，坐标系互不混用。
- 标题启发式（`_looks_like_heading`）：文本 ≤ 30 字、不以「。」结尾，且命中编号型（`1.3 条件概率`）、章节型（`第三章 …`）或以「：/：」结尾，则视为标题；标题块 `kind="heading"`，后续块 `heading_path` 只记最近这一个标题。
- 跨页：片由块贪心合并而成（§5），若相邻页的块合进同一片，`source_spans` 就有 ≥ 2 条且 `page_number` 不同，`locator` 用 `p{a}-{b}`。
- 无文本层：全部页 `extract_text()` 去空白后皆空 → `ParseError(code="no_text_layer")`，**不产出任何 chunk**。
- 加密 PDF：**先判 `reader.is_encrypted`，再按 `reader.decrypt("")` 的返回值定成败，解密成功后才读取页对象**。返回假值（`NOT_DECRYPTED`）或抛异常 → `ParseError(code="password_required")`，错误信息明确“需要打开密码”，不伪装成 `corrupted_pdf`；owner 口令加密（只限制权限、不限制阅读）用空口令即可解开，正常解析。

### 4.2 DOCX

- 解析器：`python-docx`（惰性导入），`document.iter_inner_content()` 按 body 顺序遍历段落与表格。
- `page_number`：**恒为 `None`**。DOCX 没有稳定页概念（分页由渲染器决定），虚构页码会破坏可追溯性。
- `paragraph_index`：body 顺序号，段落与表格**共用同一计数序列**；空段落不产出块但**仍占一个序号**（保证定位不因空行漂移）。
- `heading_path`：`_docx_heading_level()` 先看样式名、再匹配 `style_id`，接受 `Title`（视为 1 级）与 `heading 1`~`heading 9`；命中时 `del heading_stack[level-1:]` 后压入标题文本，普通段落继承当前栈。
- 表格：每行一个块，`cell.text` 去换行后以 `" | "` 连接，`kind="table"`，`heading_path` 继承当前标题栈；整行的 `paragraph_index` 指向该表在 body 中的位置。
- 字符偏移：`char_start=0`、`char_end=len(block.text)`——**块内坐标**（普通段落是段落级、表格行是行级），不相对整篇文档、也不相对整页。这样切片拼装后每块的 span 仍能在「该块自身文本」内自洽；需要还原正文时按 `block_index` 取 `parsed.blocks[i].text` 切片即可。
- 损坏文件（`BadZipFile` / `PackageNotFoundError` / XML 错误）→ `corrupted_docx`；正文全空 → `empty_file`。

### 4.3 TXT（兼容路径，逐字节不变）

- 解析：整篇作为一个块（`kind="text"`），解析器**不做**标题识别与分段——结构由切片器负责。
- 解码顺序保持 `utf-8-sig` → `gbk`；换行统一 `\n`；首尾 strip；都失败 → `decode_failed`。
- 切片：`document_service.load_chunk_records()` / `chunk_file()` 对 TXT 传入旧函数 `split_into_chunks` 作为 `txt_splitter`，内容与 S1 之前**逐字节一致**。
- 定位：按顺序用 `whole.find(text, cursor)` 回填 `char_start/char_end`（相对整篇文本），每次把游标推进到片尾；极端情况下 `find` 失败则退化为「从当前游标起算」的保守区间，**不跳过该片、不丢内容**。`paragraph_index` 记为切片序，`page_number=None`，`heading_path=[]`。
- 空文件（0 字节或仅空白）→ `empty_file`。

---

## 5. 切片规则（PDF / DOCX）

`_group_blocks()`：按块顺序贪心合并，规则只有三条：

1. 标题块强制开新片（标题跟着自己的正文走）；
2. 下一块会超过 `max_chars`（`CHUNK_MAX_CHARS = 400`）就换片；
3. 单个块本身超过预算时整块独占一片（**不切碎正文**，保留定位完整性）。

片内正文用 `"\n"` 连接各块再 strip。TXT 完全走旧切片函数，见 §4.3。

---

## 6. 错误码与文档状态

`ParseError(code, message, source_name)`，另有 `to_issue()` 返回 `{source_name, error_code, error_message}`。

| code | 触发条件 | `scan_knowledge` 里的 status |
|---|---|---|
| `unsupported_type` | 后缀不在 `.txt/.pdf/.docx` | `skipped_unsupported` |
| `empty_file` | 0 字节 / 仅空白 / 无可提正文 | `empty` |
| `no_text_layer` | PDF 全部页无文本层 | `no_text_layer` |
| `oversize` | 超过 `MAX_DOCUMENT_BYTES`（20 MB） | `skipped_oversize` |
| `corrupted_pdf` | pypdf 读取/页面提取抛异常 | `failed` |
| `password_required` | PDF 已加密且空口令解不开（需要打开密码） | `password_required` |
| `corrupted_docx` | python-docx 读取抛异常 | `failed` |
| `decode_failed` | TXT 编码无法识别 | `failed` |
| `parser_unavailable` | 缺 pypdf / python-docx | `failed` |
| `read_failed` | 读盘失败（OSError） | `failed` |

映射表实现在 `document_parser.STATUS_BY_CODE` + `status_for_code()`，未列出的码统一 `failed`。

此外还有一个**非错误**状态：`partial`（`STATUS_PARTIAL`）。当 PDF **部分页面有文本、部分页面没有**（混合型，常见于正文+扫描插图页）时，解析继续产出有文本页的 chunk，但 `ParsedDocument.status="partial"`、`pages_total/pages_with_text/missing_pages` 记下缺失页，**不得无条件报告完整解析成功**。本阶段不做 OCR，也不把缺失页当失败。

`scan_knowledge(directory)` 返回：

```python
{
  "documents": [
    {"source": ..., "source_name": ..., "file_type": ..., "status": "ok" | "partial" | "empty" | "no_text_layer"
     | "failed" | "skipped_unsupported" | "skipped_oversize",
     "document_id": str | None, "document_hash": str | None, "chunk_count": int,
     "bytes": int | None, "error_code": None | str, "error_message": None | str,
     "pages_total": int | None, "pages_with_text": int | None, "missing_pages": [int, ...],
     "parser_version": "s1.2", "chunker_version": "s1.2"}
  ],
  "warnings": [{"source_name": ..., "warning_code": "missing_pages", "page_numbers": [int, ...], "message": str}],
  "issues": [{"source_name": ..., "file_type": ..., "error_code": ..., "error_message": ...}],
  "chunk_count": int
}
```

规则：**`documents` 覆盖扫描到的每个文件（含失败的）**，失败文件 `document_id/document_hash` 为 `None`、`chunk_count=0`；`issues` 只装失败文件，供调用方单独报警；`warnings` 装「解析成功但不完整」的情况（当前只有 `missing_pages`），`partial` 文档**同时**出现在 `documents` 与 `warnings`，且不进 `issues`（它不是解析失败）。任何被跳过的文件都两者可见，禁止静默 continue（`load_documents/load_chunks/load_chunk_records` 仍会 `print` 一行日志后继续处理其它文件）。

### 6.1 `/upload` 的返回契约（s1.2 起）

上传成功后调用 `document_service.inspect_document(source)` 取文档级状态，**不把 partial 报成完整成功**：

```python
# 成功（含 partial）
{"message": "文档上传成功" | "文档已上传，但解析不完整：第 N 页没有可提取的文本层（本阶段不做 OCR），只有有文本的页面进入了索引",
 "source": str, "size_bytes": int, "chunk_count": int,
 "status": "ok" | "partial",
 "pages_total": int | None, "pages_with_text": int | None,
 "missing_pages": [int, ...], "warnings": [{...}]}
# 失败（400）
{"message": str, "status": "failed", "error_code": str | None}
```

混合型 PDF 仍会保存并可被检索，但 `status="partial"` 且 `message` 明确说明“解析不完整”，调用方不能把它当完整解析；全扫描 PDF 走解析失败分支（`error_code="no_text_layer"`），不落盘、不进索引。`document_service.inspect_document(source, directory=None)` 也可单独查询：`{status, pages_total, pages_with_text, missing_pages, warnings, chunk_count, document_id, parser_version, chunker_version}`。

文档级状态**不写进 15 字段 `Chunk` 契约**（那是切片级身份），而是由 `load_chunk_records_with_states()` / `rag_service.load_chunks_with_states()` 与记录一起返回 `{source: state}`；需要来源定位的调用方据此就能区分 partial 与 ok。

---

## 7. 版本与可复现

- `PARSER_VERSION = "s1.2"`（`document_parser`）、`CHUNKER_VERSION = "s1.2"`（`document_chunker`），均作为模块常量导出并写入每条记录。
- 两者任一升级 → 所有 `chunk_id` 换代（`document_id` 不变：它只由规范化相对路径与文件内容决定）；升级必须重跑评测并记录新 manifest。
- 版本历史：
  - `s1.1`：第一轮审查后（`document_id` 加路径命名空间、`chunk_id` 分隔符编码、PDF 真实字符偏移、混合型 PDF `partial`）。
  - `s1.2`：第二轮审查后（`document_id` 路径摘要 8→16 位、加密 PDF 先判 `is_encrypted` 再按 `decrypt("")` 返回值定成败并新增 `password_required`）。**路径摘要变长会改 `document_id`，`parser_version` 与 `chunker_version` 又都进入 `chunk_id` 的哈希输入，因此同一文件在 s1.2 下 chunk_id 整批换代**——这是刻意的，用于区分旧索引。
- 说明：`document_id` 公式在前两轮审查中各有修正（加入路径命名空间、摘要加长）。本分支尚未合并、未产出任何正式产物，因此没有历史 chunk_id 需要迁移。
- 解析结果不做跨进程缓存：每次调用重新解析（当前知识库很小），保证「文件即真相」；向量缓存仍按 `content_hash` 复用，不受 chunk_id 变化影响。

---

## 8. 与现有代码的兼容策略（关键决定）

现有冻结测试对数据结构有**精确断言**：

- `test_document_service.py:171`：`load_documents()` 每条 `set(doc) == {"content","source"}`
- `test_document_service.py:176`：`load_chunks()` 每条 `set(chunk) == {"content","source"}`
- `test_document_service.py:241`：`retrieve_chunks()` 每条 `set(chunk) == {"content","source","score"}`
- `test_memory_grounding.py:292`：`_sanitize_chunk()` 结果必须是 `{"source","score","content"}`
- `test_agent_tools.py:291`：`search_knowledge()` 的 `results` 每条仅三个字段

因此**富元数据一律显式开启**，默认关闭：

| API | 默认 | 开启方式 |
|---|---|---|
| `document_service.load_chunks(directory=None)` | 旧结构 `{content, source}` | `load_chunks(directory, with_metadata=True)` 或 `load_chunk_records(directory)` 返回 15 字段记录（外加 `source`） |
| `rag_service.load_chunks(directory=None)` | 旧结构（转发到 `document_service.load_chunks`，不传 `with_metadata`） | `rag_service.load_chunks(directory, with_metadata=True)` / `rag_service.load_chunk_records(directory)` |
| `rag_service.retrieve_chunks(query, top_k, with_metadata=False)` | 旧结构 `{content, source, score}` | `with_metadata=True` 时追加 `chunk_id/document_id/document_hash/file_type/page_number/printed_page_number/heading_path/paragraph_index/source_spans/parser_version/chunker_version`，并额外带文档级 `document_status`（`ok` / `partial`；查不到状态时为 `None`） |
| `rag_service._retrieve_vector` / `_retrieve_hybrid` | 同上 | 同上（`with_metadata` 透传） |
| `document_service.load_chunk_records_with_states(directory)` / `rag_service.load_chunks_with_states(directory)` | — | 返回 `(records, states)`，`states = {source: {status, pages_total, pages_with_text, missing_pages}}`；一次扫描不重复解析 |

理由：这些精确断言是既有消费方的契约（前端、评测脚本、Prompt 组装都按三字段读），改默认值等于静默破坏；显式开关让新消费方拿到定位信息，同时不制造回归。

Hybrid 去重键同步升级：`_chunk_key()` 有 `chunk_id` 时用 `("chunk_id", id)`，否则退回旧的 `("content", source, content)`——默认路径行为与升级前完全一致。

> 唯一改动的既有测试：`test_upload.py`（1）被拒类型的用例（原断言「目前只支持 .txt 文件」，现文案「目前只支持 .txt / .pdf / .docx 文件」，被拒类型改为 md/xlsx/png/jpg）；（2）新增三个 PDF 上传的 API 级用例（完整 PDF = `ok`、混合型 PDF = `partial` 且 message 明确“解析不完整”、全扫描 PDF = 400 + `no_text_layer`）。属于**有意的契约变更与新增覆盖**，PR 说明里单独标出。

---

## 9. 依赖

| 依赖 | 用途 | 位置 |
|---|---|---|
| `pypdf`（实测 7.1.0） | PDF 文本提取 | `requirements.txt` |
| `python-docx`（实测 1.2.0，带入 `lxml`） | DOCX 段落/表格 | `requirements.txt` |
| `reportlab`（实测 5.1.0） | **仅测试**用于生成多页中文 PDF 夹具 | 测试脚本内惰性导入，不进生产依赖 |

`pypdf` / `python-docx` 缺失时惰性导入 + 明确报错（`parser_unavailable`），TXT 路径不受影响。

---

## 10. 已知限制与风险

| 项 | 现状 | 处理 |
|---|---|---|
| PDF 无空行的页 | pypdf 默认抽取丢空行 → 退回按行切分，一行一块 | 定位粒度到行；`char_start/char_end` 仍是整页文本内的真实偏移 |
| 混合型 PDF（部分页无文本） | 有文本页正常出 chunk，`status="partial"` + `missing_pages` 记录缺失页 | 缺页**不被静默忽略**；本阶段不做 OCR，调用方需看 `warnings` |
| PDF 偏移坐标系 | 相对「当前页 `_normalize_text(extract_text())`」，不是整个 PDF 的全局偏移 | 跨页片段的每个 span 各自归属本页；还原正文时先按 `page_number` 取页文本 |
| DOCX 偏移坐标系 | 块内坐标（段落级 / 表格行级，恒 `0..len(block.text)`） | 不跨块解释；需要原文时用 `block_index` 取 `parsed.blocks[i].text` |
| PDF 多栏排版 | 只保证「文本不丢」，阅读顺序由 pypdf 决定 | 同上，不在本阶段解决 |
| PDF 标题识别 | 启发式（≤30 字、编号/章节/冒号），可能误判 | `heading_path` 只是辅助定位，不参与身份计算 |
| DOCX 合并单元格 | 单元格按 `cell.text` 取，合并单元格会重复文本 | 依靠 `chunk_id` 的 `occurrence` 与 span 定位可追溯 |
| chunk_id 随正文变 | 刻意性质 | manifest 保留旧版本记录用于对照 |
| TXT `find` 失败 | 退化为游标起算的保守区间 | 不跳过、不丢内容；测试覆盖正常路径 |
| DOCX 页码 | 恒为 `None` | 不虚构；需要页码时以 PDF 为准 |
| 带打开密码的 PDF | 无法解析（`password_required`），不落盘、不进索引 | 需先解除加密；本阶段不做密码输入 |

---

## 11. 后续工作（本阶段明确未做）

1. `agent_tools.search_knowledge(..., with_location=True)` 的定位透出：会改到 Agent 工具的输出契约，S1 不动，单独排期。
2. 扫描版 PDF 的 OCR。
3. Chunk 级语义去重、Rerank。
4. 解析结果持久化（manifest）与增量重建。

---

## 12. 测试

测试矩阵、变更文件清单与验收表见 `docs/RAG-DOCUMENT-PARSING-S1-TEST-REPORT.md`。
