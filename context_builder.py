"""检索结果 → LLM 上下文组装（P1-A：精确去重 + 字符预算）。

调用链位置：检索（rag_service / llamaindex_rag）→ **本模块** → build_ask_prompt → LLM。
`/ask` 与 `/ask/stream` 共用，保证两条路径上下文完全一致。

规则（全部可测试、无隐式行为）：
1. 精确去重：键为 (source, content)，保留首次出现的块与原有检索排序；
   相同内容不同 source 各自保留（不合并不同来源）；不同分块（内容不同）绝不误删。
2. 来源可追溯：每块独立成段，带 `[来源: {source}]` 标记（与 rag_service.format_context 风格一致），
   不把多来源压缩成无法追溯的文本。
3. 字符预算：CONTEXT_MAX_CHARS（环境变量，默认 6000；设 0 表示不限制）。
   预算统计对象 = **最终进入 Prompt 的完整上下文文本**，包含来源标签与分隔符。
   超限策略：按检索顺序贪心装入**完整块**；一旦某块装入后超出预算，该块及其后所有块跳过
   （保持顺序、不切块、不丢已装入块的来源标记）；单块自身超过预算 → 跳过并记 oversized。
   跳过情况全部记入返回 info（调用方可日志提示），绝不静默截断。
4. 不引入 Tokenizer 依赖：按字符计数（中文语境下与 prompt 长度一致）。
"""

import logging
import os


DEFAULT_CONTEXT_MAX_CHARS = 6000

BLOCK_SEPARATOR = "\n\n---\n\n"

logger = logging.getLogger("ai_study.context")


class ContextError(ValueError):
    """上下文输入非法。"""


def context_max_chars():
    """字符预算（CONTEXT_MAX_CHARS，默认 6000；0/负数 = 不限制）。"""

    raw = (os.getenv("CONTEXT_MAX_CHARS") or "").strip()

    if not raw:
        return DEFAULT_CONTEXT_MAX_CHARS

    try:
        value = int(raw)
    except ValueError as error:
        raise ContextError(f"CONTEXT_MAX_CHARS 必须是整数：{raw!r}") from error

    return value if value > 0 else 0


def _block_text(chunk):
    source = chunk.get("source") or "unknown"

    return f"[来源: {source}]\n\n{chunk.get('content') or ''}"


def dedupe_chunks(chunks):
    """按 (source, content) 精确去重，保留首次出现与原顺序。

    返回 (去重后列表, 去除条数)。相同内容不同 source 保留为不同块。
    """

    if chunks is None:
        raise ContextError("chunks 不能为 None")

    seen = set()
    unique = []
    removed = 0

    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ContextError("chunk 必须是字典")

        key = (chunk.get("source"), chunk.get("content"))

        if key in seen:
            removed += 1
            continue

        seen.add(key)
        unique.append(chunk)

    return unique, removed


def build_context(chunks, max_chars=None):
    """组装 LLM 上下文，返回 (context_text, info)。

    - max_chars=None 时读取 CONTEXT_MAX_CHARS 配置
    - context_text 为空字符串表示无可入库内容（调用方走无资料路径）
    - info 记录去重/预算/超限的全部事实，供日志与测试断言（不进 API 响应）
    """

    if max_chars is None:
        max_chars = context_max_chars()

    unique, deduped = dedupe_chunks(chunks)

    info = {
        "blocks_in": len(chunks),
        "blocks_used": 0,
        "blocks_deduped": deduped,
        "blocks_dropped_budget": 0,
        "blocks_dropped_oversize": 0,
        "chars": 0,
        "budget": max_chars,
    }

    blocks = []
    total = 0

    for index, chunk in enumerate(unique):
        block = _block_text(chunk)

        if max_chars and len(block) > max_chars:
            # 单块超预算：整块跳过（不截断、不丢来源标记），记录事实
            info["blocks_dropped_oversize"] += 1
            continue

        # 装入后的最终文本长度 = 已装入文本 + 分隔符 + 本块
        sep_len = len(BLOCK_SEPARATOR) if blocks else 0

        if max_chars and total + sep_len + len(block) > max_chars:
            # 顺序贪心：本块装不下，其后所有块也不再尝试（保持排序与完整块）
            info["blocks_dropped_budget"] += len(unique) - index
            break

        blocks.append(block)
        info["blocks_used"] += 1
        total += sep_len + len(block)

    text = BLOCK_SEPARATOR.join(blocks)
    info["chars"] = len(text)

    if info["blocks_dropped_budget"] or info["blocks_dropped_oversize"]:
        # 超限跳过不静默：记 warning（含跳过数量与预算），info 同时返回给调用方
        logger.warning(
            "上下文超出字符预算，跳过整块（budget=%s, chars=%s, dropped_budget=%s, dropped_oversize=%s）",
            info["budget"], info["chars"], info["blocks_dropped_budget"], info["blocks_dropped_oversize"],
        )

    return text, info
