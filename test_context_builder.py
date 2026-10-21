"""上下文组装测试（P1-A）：直接运行 python test_context_builder.py。

全确定性（无模型 / 无网络 / 无 sqlite 写入），覆盖：
1. 无重复内容时保持原有输出语义（顺序 + 来源标签）
2. 相同 (source, content) 去重，保留首次出现
3. 相同内容、不同 source 时仍保留独立来源
4. 去重后保持原有检索顺序
5. 上下文未超过预算（统计含来源标签与分隔符）
6. 超出预算时按既定策略处理（顺序贪心整块跳过 + info 统计）
7. 来源信息不会被截断或丢失
8. 边界：空检索结果、单个分块超过预算
9. /ask 与 /ask/stream 上下文组装一致
"""

import asyncio
import json
import os

import context_builder
import llm_client
import main

from context_builder import BLOCK_SEPARATOR, build_context, dedupe_chunks


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def block_text(source, content):
    return f"[来源: {source}]\n\n{content}"


# ==========================
# 1. 无重复内容：输出语义保持
# ==========================

chunks = [
    {"content": "定积分定义", "source": "math.txt"},
    {"content": "学习步骤", "source": "guide.txt"},
    {"content": "常见错误", "source": "math.txt"},
]

text, info = build_context(chunks, max_chars=0)

check("无重复时全部块保留", info["blocks_in"] == 3 and info["blocks_used"] == 3)
check("无重复时去重计数为 0", info["blocks_deduped"] == 0)
check("每块带来源标签且内容完整",
      all(block_text(c["source"], c["content"]) in text for c in chunks))
check("无重复时块顺序与检索顺序一致",
      text.index("定积分定义") < text.index("学习步骤") < text.index("常见错误"))
check("块之间用统一分隔符且不混排来源",
      text.count(BLOCK_SEPARATOR) == 2
      and text == BLOCK_SEPARATOR.join(block_text(c["source"], c["content"]) for c in chunks))

# ==========================
# 2. 相同 (source, content) 去重
# ==========================

dup_chunks = [
    {"content": "重复内容", "source": "a.txt"},
    {"content": "重复内容", "source": "a.txt"},
    {"content": "重复内容", "source": "a.txt"},
    {"content": "另一段", "source": "a.txt"},
]

unique, removed = dedupe_chunks(dup_chunks)

check("相同 (source, content) 只保留一份",
      len(unique) == 2 and removed == 2
      and [c["content"] for c in unique] == ["重复内容", "另一段"])

text, info = build_context(dup_chunks, max_chars=0)

check("build_context 去重计数正确",
      info["blocks_in"] == 4 and info["blocks_used"] == 2 and info["blocks_deduped"] == 2)
check("重复内容在上下文中只出现一次",
      text.count("重复内容") == 1 and text.count("[来源: a.txt]") == 2)

# ==========================
# 3. 相同内容、不同 source：独立保留
# ==========================

same_content = [
    {"content": "共享定义", "source": "a.txt"},
    {"content": "共享定义", "source": "b.txt"},
    {"content": "共享定义", "source": "a.txt"},
]

text, info = build_context(same_content, max_chars=0)

check("同内容异来源不合并（两个来源都保留）",
      info["blocks_used"] == 2 and info["blocks_deduped"] == 1
      and "[来源: a.txt]" in text and "[来源: b.txt]" in text)
check("同内容异来源各保留独立来源标记",
      text.count("共享定义") == 2 and text.count("[来源: a.txt]") == 1)

# ==========================
# 4. 去重后保持原有顺序
# ==========================

mixed = [
    {"content": "第一", "source": "s1"},
    {"content": "第二", "source": "s2"},
    {"content": "第一", "source": "s1"},
    {"content": "第三", "source": "s3"},
    {"content": "第二", "source": "s2"},
]

text, info = build_context(mixed, max_chars=0)

check("去重后仍按首次出现顺序",
      info["blocks_used"] == 3
      and text.index("第一") < text.index("第二") < text.index("第三")
      and text == BLOCK_SEPARATOR.join([
          block_text("s1", "第一"), block_text("s2", "第二"), block_text("s3", "第三"),
      ]))

# ==========================
# 5. 预算统计与未超预算
# ==========================

three = [
    {"content": "甲" * 50, "source": "a.txt"},
    {"content": "乙" * 50, "source": "b.txt"},
]

text, info = build_context(three, max_chars=0)

expected_len = len(block_text("a.txt", "甲" * 50)) + len(BLOCK_SEPARATOR) + len(block_text("b.txt", "乙" * 50))

check("预算统计 = 最终文本长度（含来源标签与分隔符）",
      info["chars"] == len(text) == expected_len)

text, info = build_context(three, max_chars=expected_len)

check("恰好等于预算时全部保留",
      info["blocks_used"] == 2 and info["blocks_dropped_budget"] == 0 and info["chars"] <= info["budget"])

text, info = build_context(three, max_chars=expected_len - 1)

check("超预算 1 字符时按策略丢块且 chars <= budget",
      info["blocks_dropped_budget"] >= 1 and info["chars"] <= info["budget"])

# ==========================
# 6. 超预算策略：顺序贪心整块跳过
# ==========================

seq = [
    {"content": "A" * 20, "source": "1.txt"},
    {"content": "B" * 20, "source": "2.txt"},
    {"content": "C" * 20, "source": "3.txt"},
    {"content": "D" * 20, "source": "4.txt"},
]

one_block = len(block_text("1.txt", "A" * 20))

text, info = build_context(seq, max_chars=one_block + 10)

check("贪心策略：装不下的块及其后全部跳过",
      info["blocks_used"] == 1 and info["blocks_dropped_budget"] == 3
      and "[来源: 1.txt]" in text and "[来源: 2.txt]" not in text)

check("跳过后文本不超过预算", info["chars"] <= info["budget"])

text, info = build_context(seq, max_chars=one_block * 2 + len(BLOCK_SEPARATOR))

check("贪心策略保持检索优先顺序（前两块保留）",
      info["blocks_used"] == 2 and info["blocks_dropped_budget"] == 2
      and text.index("1.txt") < text.index("2.txt"))

# ==========================
# 7. 来源信息不被截断或丢失
# ==========================

text, info = build_context(seq, max_chars=one_block + 10)

for part in text.split(BLOCK_SEPARATOR):
    if not part:
        continue
    source_label = part.split("\n\n", 1)[0]
    check("保留块的来源标签完整（含收尾 ]）",
          source_label.startswith("[来源: ") and source_label.endswith("]"))

check("被跳过整块不产生孤立来源标签",
      text.count("[来源: ") == info["blocks_used"] and text.count("]") >= info["blocks_used"])

# ==========================
# 8. 边界：空检索结果 / 单块超预算 / 配置
# ==========================

text, info = build_context([], max_chars=0)

check("空检索结果产出空上下文且计数归零",
      text == "" and info["blocks_in"] == 0 and info["blocks_used"] == 0
      and info["blocks_deduped"] == 0 and info["chars"] == 0)

huge = [{"content": "超长" * 100, "source": "big.txt"}, {"content": "小块", "source": "small.txt"}]

text, info = build_context(huge, max_chars=50)

check("单块超预算整块跳过（不截断、不丢来源）",
      info["blocks_dropped_oversize"] == 1 and "big.txt" not in text and "超长" not in text)

check("超大块跳过不影响后续小块",
      info["blocks_used"] == 1 and "[来源: small.txt]" in text)

exact = [{"content": "x" * 10, "source": "e.txt"}]
exact_block_len = len(block_text("e.txt", "x" * 10))

text, info = build_context(exact, max_chars=exact_block_len)
check("单块长度恰好等于预算时保留",
      info["blocks_used"] == 1 and info["blocks_dropped_oversize"] == 0)

text, info = build_context(exact, max_chars=exact_block_len - 1)
check("单块长度超预算 1 字符时跳过",
      info["blocks_dropped_oversize"] == 1 and text == "")

check("默认预算常量存在且为正",
      context_builder.DEFAULT_CONTEXT_MAX_CHARS > 0)

backup = os.environ.get("CONTEXT_MAX_CHARS")
try:
    os.environ.pop("CONTEXT_MAX_CHARS", None)
    check("未配置时用默认预算",
          context_builder.context_max_chars() == context_builder.DEFAULT_CONTEXT_MAX_CHARS)

    os.environ["CONTEXT_MAX_CHARS"] = "0"
    check("CONTEXT_MAX_CHARS=0 表示不限制", context_builder.context_max_chars() == 0)

    os.environ["CONTEXT_MAX_CHARS"] = "1234"
    check("CONTEXT_MAX_CHARS 可配置", context_builder.context_max_chars() == 1234)

    os.environ["CONTEXT_MAX_CHARS"] = "abc"
    try:
        context_builder.context_max_chars()
        check("非法 CONTEXT_MAX_CHARS 抛 ContextError", False)
    except context_builder.ContextError:
        check("非法 CONTEXT_MAX_CHARS 抛 ContextError", True)

    os.environ["CONTEXT_MAX_CHARS"] = "800"
    text, info = build_context(seq)
    check("build_context 默认读取环境预算",
          info["budget"] == 800 and info["chars"] <= 800)
finally:
    if backup is None:
        os.environ.pop("CONTEXT_MAX_CHARS", None)
    else:
        os.environ["CONTEXT_MAX_CHARS"] = backup

# ==========================
# 9. /ask 与 /ask/stream 上下文一致
# ==========================

ask_chunks = [
    {"content": "定积分定义", "source": "math.txt", "score": 0.9},
    {"content": "定积分定义", "source": "math.txt", "score": 0.8},
    {"content": "换元法", "source": "math.txt", "score": 0.7},
    {"content": "定积分定义", "source": "notes.txt", "score": 0.6},
]

captured = {}


def fake_call_ollama(prompt):
    captured["ask_prompt"] = prompt
    return "答案", None


def fake_chat_stream(messages):
    captured["stream_prompt"] = messages[0]["content"]
    yield {"type": "delta", "text": "答"}
    yield {"type": "done", "usage": {"available": False}, "model": "test", "elapsed_seconds": 0.1}


async def collect_body(response):
    parts = []
    async for chunk in response.body_iterator:
        parts.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(parts)


original_retrieve_for_ask = main.retrieve_for_ask
original_call_ollama = main.call_ollama
original_chat_stream = llm_client.chat_stream

main.retrieve_for_ask = lambda question: list(ask_chunks)
main.call_ollama = fake_call_ollama
llm_client.chat_stream = fake_chat_stream

try:
    result = main.ask("定积分怎么学？")
    stream_body = asyncio.run(collect_body(main.ask_stream("定积分怎么学？")))
finally:
    main.retrieve_for_ask = original_retrieve_for_ask
    main.call_ollama = original_call_ollama
    llm_client.chat_stream = original_chat_stream

check("普通问答与流式问答的 Prompt 完全一致",
      captured["ask_prompt"] == captured["stream_prompt"])

context_part = captured["ask_prompt"]

check("两条路径共用去重后的上下文（重复块只进一次）",
      context_part.count("定积分定义") == 2  # math.txt 与 notes.txt 各一次
      and context_part.count("[来源: math.txt]") == 2)  # 定积分定义 + 换元法

check("两条路径来源可追溯（math.txt / notes.txt 均带标签）",
      "[来源: math.txt]" in context_part and "[来源: notes.txt]" in context_part)

check("上下文块顺序与检索顺序一致",
      context_part.index("[来源: math.txt]\n\n定积分定义")
      < context_part.index("[来源: math.txt]\n\n换元法")
      < context_part.index("[来源: notes.txt]\n\n定积分定义"))

check("API 响应 sources 仍为原始检索结果（含重复与排序）",
      result["sources"] == ask_chunks and result["answer"] == "答案" and result["error"] is None)

frames = [json.loads(line[6:]) for line in stream_body.split("\n") if line.startswith("data: ")]
events = [line[7:].strip() for line in stream_body.split("\n") if line.startswith("event: ")]

check("SSE sources 帧仍为原始检索结果",
      events[0] == "sources" and frames[0]["sources"] == ask_chunks)

# 小预算下两条路径依然一致（超限策略同样共用）
original_retrieve_for_ask = main.retrieve_for_ask
main.retrieve_for_ask = lambda question: list(ask_chunks)
main.call_ollama = fake_call_ollama
llm_client.chat_stream = fake_chat_stream

backup = os.environ.get("CONTEXT_MAX_CHARS")
os.environ["CONTEXT_MAX_CHARS"] = "60"

try:
    main.ask("定积分怎么学？")
    asyncio.run(collect_body(main.ask_stream("定积分怎么学？")))
finally:
    main.retrieve_for_ask = original_retrieve_for_ask
    main.call_ollama = original_call_ollama
    llm_client.chat_stream = original_chat_stream
    if backup is None:
        os.environ.pop("CONTEXT_MAX_CHARS", None)
    else:
        os.environ["CONTEXT_MAX_CHARS"] = backup

check("超预算场景下两条路径 Prompt 仍完全一致",
      captured["ask_prompt"] == captured["stream_prompt"])
check("超预算场景下来源标签不残缺（每个出现的标签都闭合）",
      captured["ask_prompt"].count("[来源: ") == captured["ask_prompt"].count("]\n\n"))

print("ALL TESTS COMPLETED")
