"""毕业设计实验脚本 · 自动指标核对：Y1–Y5 与 grounding。

只读 `experiments/graduation/runs/*/results.jsonl` 与同目录 `prompts/`，按
docs/GRADUATION-EXPERIMENT-DESIGN.md v4.5 §4.4 计算 Y1–Y5，并用
`agent_plan_draft.check_grounding()` 复核来源完整性。

设计要点（与 v4.5 一致，且不夸大测到的内容）：

- **只读**：不写 runs/、不写 plan.json/results.jsonl、不写 feedback.db、不生成盲评包、
  不调用任何模型或 Embedding API。运行前后对每个 results.jsonl 做 SHA-256 比对，
  不一致直接失败。
- **主实验与补充实验分开输出**：主实验写 auto_metrics.csv，补充批次写
  auto_metrics_supplement.csv；B0 legacy 10 份归入补充批次（§5.4.4 把 B0 计入补充 23 份）。
- **不适用就写 N/A**：Y1 的"模拟完成率"在正式样本未提供能力等级/可用时长时记 N/A，
  不编造能力值；Y5 的 `len(messages)` 在归档未保存 messages 数组时记 N/A，
  不把"Prompt 文件里能确认的 SYSTEM+USER 两条"冒充成运行时的消息条数。
- **不把 SKIP/UNVERIFIABLE 记成 PASS**：grounding 在归档信息不足时明确标注
  `UNVERIFIABLE`，并写清是哪一条证据链缺失。

Y1 主指标用真实的 `agent_tools.validate_plan_for_save()`（`agent_tools.py:438`）。
该函数只读一次"最近一条学习反馈"、不调模型，但需要可读的库；因此本脚本在
`%TEMP%/graduation_experiments/<ref>/` 下用 `seed_fixture.seed_sample()` 重建
临时夹具库后调用，绝不碰真实 `feedback.db`。

命令：

    python experiments/graduation/check_auto.py                    # 默认输出到临时目录
    python experiments/graduation/check_auto.py --out-dir experiments/graduation/auto
    python experiments/graduation/check_auto.py --no-fixtures      # 不建夹具库，Y1 记 N/A
"""

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import build_prompts  # noqa: E402
import manifest_rules  # noqa: E402
import seed_fixture  # noqa: E402

RESULTS_NAME = "results.jsonl"
MAIN_RUN = "main-20261113T061051Z"
SMOKE_RUN = "internal-api-smoke-20261113T061019Z"
SUPPLEMENT_RUNS = (
    "suppl-subj-20261113T062711Z",
    "suppl-s7empty-20261113T062822Z",
    "20261112T210000-20261113T011751Z",
    "20261112T210000-20261113T012031Z",
    "b0-remainder-20261113T013105Z",
)
MANIFEST_PATH = HERE.parents[1] / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"

# 唯一允许「manifest 完全没有条目」的 run：B0 是基线提交 df4b938 之前的 legacy 结果。
# 白名单之外一律 fail-closed；B0 若已有条目，其字段仍必须完整合法（见 verify_manifest）。
LEGACY_NO_MANIFEST_RUNS = frozenset({
    "20261112T210000-20261113T011751Z",
    "20261112T210000-20261113T012031Z",
    "b0-remainder-20261113T013105Z",
})

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# 合法取值（v4.5 §4.2、§5.1）
MAIN_GROUPS = ("A", "B", "C", "D", "B0")
SAMPLE_IDS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
SUPPLEMENT_SAMPLES = {"suppl-subj-20261113T062711Z": "S5"}

MAX_CORRECTIONS = 2

# 模拟推演（§4.4 辅助指标）：难度 ≤ 能力 且 时长 ≤ 可用时长 记完成。
# 只使用样本里**显式提供**的能力/时长字段；正式 S1–S7 没有这两个字段，
# 因此正式实验该指标一律 N/A，而不是按脚本自定的规则硬算。
ABILITY_RANK = {"easy": 1, "medium": 2, "hard": 3}


class CheckError(RuntimeError):
    """输入不合法或数据被改动：终止并返回非零。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def median(values):
    return statistics.median(values) if values else None


def mean(values):
    return statistics.fmean(values) if values else None


def p95(values):
    """最近秩法 P95（不做插值，避免与论文口径不一致）。"""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(0.95 * len(ordered) + 0.5)) - 1)
    rank = min(rank, len(ordered) - 1)
    return ordered[rank]


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def load_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# 归档行读取与字段校验
# ---------------------------------------------------------------------------


def read_rows(run_dir):
    """读一个 run 目录的 results.jsonl，并做字段/类型校验。

    返回 (rows, problems)。problems 里每项是一条 (row_number, field, detail)。
    行号从 1 开始，便于人工回查。
    """

    path = Path(run_dir) / RESULTS_NAME
    if not path.is_file():
        raise CheckError(f"缺少结果文件：{path}")

    rows = []
    problems = []

    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                problems.append((number, "json", str(error)))
                continue
            if not isinstance(row, dict):
                problems.append((number, "row", "不是 JSON 对象"))
                continue
            rows.append((number, row))

    for number, row in rows:
        def bad(field, detail):
            problems.append((number, field, detail))

        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip():
            bad("sample_id", "缺失或不是非空字符串")

        group = row.get("group")
        if group not in build_prompts.GROUP_SLOTS:
            bad("group", f"非法取值：{group!r}")

        if not row.get("experiment_id") or not isinstance(row.get("experiment_id"), str):
            bad("experiment_id", "缺失或不是非空字符串（身份字段必须保留）")

        repeat = row.get("repeat_index")
        if not is_int(repeat) or not 0 <= repeat <= 4:
            bad("repeat_index", f"必须是 0–4 的整数，实际 {repeat!r}")

        if not isinstance(row.get("success"), bool):
            bad("success", "必须是布尔值")

        if row.get("error_type") is not None and not isinstance(row.get("error_type"), str):
            bad("error_type", "必须是字符串或 null")

        request_count = row.get("request_count")
        if not is_int(request_count) or request_count < 1:
            bad("request_count", f"必须是 ≥1 的整数，实际 {request_count!r}")

        corrections = row.get("corrections_used")
        if not is_int(corrections) or not 0 <= corrections <= MAX_CORRECTIONS:
            bad("corrections_used", f"必须是 0–{MAX_CORRECTIONS} 的整数，实际 {corrections!r}")

        elapsed = row.get("elapsed_seconds")
        if not is_number(elapsed) or elapsed <= 0:
            bad("elapsed_seconds", f"必须是正数，实际 {elapsed!r}")

        final_output = row.get("final_output")
        if not isinstance(final_output, dict):
            bad("final_output", "必须是 JSON 对象（含 answer/plan）")
        elif not isinstance(final_output.get("answer"), str):
            bad("final_output.answer", "必须是字符串")

        # legacy 行没有 plan_seq / round_index：允许缺失，但存在时必须是整数
        for field in ("plan_seq", "round_index"):
            if field in row and not is_int(row[field]):
                bad(field, f"存在但不是整数：{row[field]!r}")

        if "prompt_sha256" in row:
            value = row["prompt_sha256"]
            if not isinstance(value, str) or len(value) != 64:
                bad("prompt_sha256", "必须是 64 位十六进制字符串")

    return rows, problems


def manifest_hashes(path=None):
    """docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv → {relative_path: row}。

    解析规则与 make_blind_pack.py 共用 manifest_rules，避免两处口径不一致：
    路径规范化、file 列必须指向该行真正描述的文件、同一路径重复、空路径行，
    全部由同一套规则判定；任何一条不成立都直接抛 CheckError，不允许「静默跳过」。
    """

    path = Path(path) if path else MANIFEST_PATH
    if not path.is_file():
        return {}
    try:
        return manifest_rules.load_manifest(path)
    except manifest_rules.ManifestError as exc:
        raise CheckError(str(exc)) from exc


def _manifest_number(entry, field):
    """取 manifest 里的整数列，返回 (value, error)。空/非纯数字都算错误。"""

    text = str(entry.get(field) or "").strip()
    if not text:
        return None, f"manifest 的 {field} 为空"
    if not text.isdigit():
        return None, f"manifest 的 {field} 不是合法非负整数：{text!r}"
    return int(text), None


def verify_manifest(run_name, run_dir, hashes, rows_count, manifest_path=None):
    """返回 (状态, 细节)。状态：match / mismatch / manifest_missing / legacy_no_manifest / manifest_absent。

    只有 match 与 legacy_no_manifest 可信；其余状态调用方必须终止，不能只记一行日志。
    """

    path = Path(manifest_path) if manifest_path else MANIFEST_PATH
    if not path.is_file():
        return "manifest_absent", f"manifest 清单文件缺失：{path}（不能只凭现场计算值放行）"

    key = f"experiments/graduation/runs/{run_name}/{RESULTS_NAME}"
    entry = hashes.get(key)
    if entry is None:
        if run_name in LEGACY_NO_MANIFEST_RUNS:
            return "legacy_no_manifest", f"B0 legacy run 允许缺条目（manifest 未收录 {key}）"
        return "manifest_missing", f"manifest 未收录 {key}（该 run 不属于 B0 legacy 白名单）"

    results_path = Path(run_dir) / RESULTS_NAME
    actual_sha = sha256_file(results_path)
    actual_size = results_path.stat().st_size
    problems = []

    # file 列必须描述这一条路径真正指向的文件；空着或写成 plan.json 都说明这行不可信，
    # 不能只靠 sha256/size 通过就放行。
    file_problem, file_detail = manifest_rules.entry_file_problem(key, entry, RESULTS_NAME)
    if file_problem:
        problems.append(file_detail)

    expected_sha = str(entry.get("sha256") or "").strip().lower()
    if not expected_sha:
        problems.append("sha256 为空")
    elif not SHA256_HEX.match(expected_sha):
        problems.append(f"sha256 不是 64 位十六进制：{expected_sha!r}")
    elif actual_sha != expected_sha:
        problems.append(f"SHA-256 不一致：manifest={expected_sha} 实际={actual_sha}")

    expected_size, error = _manifest_number(entry, "size_bytes")
    if error:
        problems.append(error)
    elif expected_size != actual_size:
        problems.append(f"大小不一致：manifest={expected_size} 实际={actual_size}")

    expected_records, error = _manifest_number(entry, "records")
    if error:
        problems.append(error)
    elif expected_records != rows_count:
        problems.append(f"记录数不一致：manifest={expected_records} 实际={rows_count}")

    if problems:
        return "mismatch", "；".join(problems)

    return "match", (
        f"SHA-256/大小/记录数与 manifest 一致（{actual_sha[:16]}…，"
        f"{actual_size} 字节，{rows_count} 条）"
    )


# ---------------------------------------------------------------------------
# grounding：从归档的 Prompt 重建 evidence
# ---------------------------------------------------------------------------


PROMPT_SYSTEM_PREFIX = "===== SYSTEM =====\n"
PROMPT_USER_SEP = "\n\n===== USER =====\n"


def prompt_text(run_dir, sample_id, group):
    path = Path(run_dir) / "prompts" / str(sample_id) / f"{group}.txt"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def memory_block_present(text):
    return isinstance(text, str) and build_prompts.MEMORY_HEADER in text


def parse_prompt_archive(text):
    """把归档 Prompt 拆回 (system, user)。

    分隔符只在「文档级」出现一次：先确认 SYSTEM 前缀，再按唯一的 USER 分隔符切分。
    任何一处不满足就抛 ValueError —— 宁可判 UNVERIFIABLE，也不用字符串切割截断内容。
    """

    if not isinstance(text, str) or not text.startswith(PROMPT_SYSTEM_PREFIX):
        raise ValueError("归档 Prompt 缺少 `===== SYSTEM =====` 头部")

    body = text[len(PROMPT_SYSTEM_PREFIX):]
    occurrences = body.count(PROMPT_USER_SEP)

    if occurrences != 1:
        raise ValueError(f"归档 Prompt 的 `===== USER =====` 分隔符出现 {occurrences} 次（应为 1 次），无法安全切分")

    system, _, user = body.partition(PROMPT_USER_SEP)

    if not system.strip():
        raise ValueError("归档 Prompt 的 SYSTEM 内容为空")

    if not user.strip():
        raise ValueError("归档 Prompt 的 USER 内容为空")

    return system, user


# 记忆条目字段白名单：与 agent_plan_draft.MEMORY_RESULT_FIELDS / agent_tools._sanitize_memory()
# 的公开投影逐字对应。这里刻意不 import agent_tools（它会导入 database 并绑定 DATABASE_PATH，
# 破坏 check_auto 的只读隔离），改用镜像常量，并在测试里断言两边完全一致，防止静默漂移。
MEMORY_PAYLOAD_FIELDS = (
    "type",
    "subject",
    "content",
    "strength",
    "evidence_count",
    "episode",
    "evidence_feedback_ids",
    "updated_at",
)

# 与 agent_tools.MEMORY_TYPES 一致
MEMORY_PAYLOAD_TYPES = ("difficulty", "task_performance", "time_pattern", "task_topic")

# _memory_block() 之后固定跟随的 Prompt 片段（记忆区块是 parts 的倒数第二段）
MEMORY_SUFFIX_MARKER = "输出契约："


def _parse_memory_timestamp(value):
    """按 memory_service._parse_ts() 的同一规则解析记忆时间戳。

    真实记忆的 updated_at 一律来自 `datetime.now().isoformat()`（本地无时区字符串），
    这里额外接受 `Z` 与带偏移的写法（按墙钟时间丢 tzinfo），与记忆服务的解析口径一致；
    空串、`not-a-date`、`20261108` 这类"看起来像日期"的值一律解析失败。
    只读镜像，不 import memory_service（它会绑定 DATABASE_PATH，破坏 check_auto 的只读隔离）。

    注意：Python 3.11+ 的 `fromisoformat()` 会接受纯日期基本格式（`20261108`、`2026-11-08`），
    而本项目的真实时间戳必然带 `T` 或空格加时分。这里先用形状校验把这些值挡掉，
    否则"看起来像日期"的伪造值会被当成合法时间。
    """

    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", text) is None:
        return None

    # 与模块内其他时间用法一致：局部导入，不在顶层扩大 import 面。
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)

    return parsed


def _memory_int(value, minimum):
    """非负/下界整数（排除 bool）：True 在 Python 里是 int，必须单独排掉。"""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum:
        return None
    return value


def _validate_memory_item(index, item):
    """逐条严格校验一条记忆，返回错误描述或 None。

    8 个字段必须全部出现。`_sanitize_memory()` 对每条记忆都无条件产出这 8 个键
    （来源是 learning_memories 的 NOT NULL 列），所以「字段缺失」只可能是 Prompt 被改过；
    「值允许为 None」与「字段本身缺失」是两件事，后者绝不能当成「这一项不用查」放过去。
    """

    where = f"第 {index} 条记忆"
    if not isinstance(item, dict):
        return f"{where}不是 JSON 对象"

    unknown = sorted(set(item) - set(MEMORY_PAYLOAD_FIELDS))
    if unknown:
        return f"{where}含公开投影之外的字段：{unknown}"

    missing = [field for field in MEMORY_PAYLOAD_FIELDS if field not in item]
    if missing:
        return f"{where}缺少公开投影字段：{missing}（8 个字段必须全部存在，键缺失即判失败）"

    memory_type = item.get("type")
    if not isinstance(memory_type, str) or memory_type not in MEMORY_PAYLOAD_TYPES:
        return f"{where}的 type 非法：{memory_type!r}（应为 {'/'.join(MEMORY_PAYLOAD_TYPES)}）"

    subject = item.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        return f"{where}的 subject 为空或不是字符串：{subject!r}"

    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return f"{where}的 content 为空或不是字符串：{content!r}"

    strength = item.get("strength")
    if isinstance(strength, bool) or not isinstance(strength, (int, float)):
        return f"{where}的 strength 不是数字：{strength!r}"
    if not 0.0 <= float(strength) <= 1.0:
        return f"{where}的 strength 超出 0–1 范围：{strength!r}"

    count = item.get("evidence_count")
    if _memory_int(count, 0) is None:
        return f"{where}的 evidence_count 不是非负整数：{count!r}"

    episode = item.get("episode")
    if _memory_int(episode, 1) is None:
        return f"{where}的 episode 不是正整数：{episode!r}"

    ids = item.get("evidence_feedback_ids")
    if not isinstance(ids, list):
        return f"{where}的 evidence_feedback_ids 不是数组：{ids!r}"
    if len(ids) > 5:
        return f"{where}的 evidence_feedback_ids 超过 5 个（真实 Tool 每条最多 5 个）：{len(ids)}"
    for value in ids:
        if _memory_int(value, 1) is None:
            return f"{where}的 evidence_feedback_ids 含非法 id：{value!r}"

    updated_at = item.get("updated_at")
    if _parse_memory_timestamp(updated_at) is None:
        return f"{where}的 updated_at 不是可解析的 ISO 8601 时间：{updated_at!r}"

    return None


def parse_memory_payload(user_text):
    """从 USER 内容里解析记忆区块的真实记忆列表。

    返回 (header_present, payload, error)。payload 是 Prompt 里实际注入的记忆对象列表；
    header_present 为 True 但 payload 为空/解析失败时 error 非空 —— 这正是
    「Prompt 声称有记忆来源，却没有真实记忆内容」的情况。

    每一条记忆都必须逐项合法：只要求"至少一条有效"会让夹带的伪造条目混进证据链。
    格式问题一律返回 error（由调用方判 FAIL），绝不降级成"记忆为空"后继续 PASS。
    """

    if not isinstance(user_text, str) or build_prompts.MEMORY_HEADER not in user_text:
        return False, [], None

    occurrences = user_text.count(build_prompts.MEMORY_HEADER)
    if occurrences != 1:
        return True, [], (
            f"记忆区块头部出现 {occurrences} 次（应为 1 次），存在多份互相冲突的记忆数据"
        )

    header_index = user_text.index(build_prompts.MEMORY_HEADER)
    tail = user_text[header_index + len(build_prompts.MEMORY_HEADER):]
    array_index = tail.find("[")

    if array_index < 0:
        return True, [], "记忆区块后找不到 JSON 数组"

    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(tail[array_index:])
    except json.JSONDecodeError as error:
        return True, [], f"记忆区块的 JSON 解析失败：{error}"

    remainder = tail[array_index + end:]
    if remainder.count("[") > 0:
        return True, [], "记忆区块的 JSON 数组之后还有第二份 JSON 数据，无法判定哪一份才是注入内容"
    allowed_prefixes = (MEMORY_SUFFIX_MARKER, "answer 与 plan.reason 不得把上面的长期学习记忆")
    stripped = remainder.strip()
    if stripped and not stripped.startswith(allowed_prefixes):
        return True, [], f"记忆区块的 JSON 之后出现无法解释的残留内容：{stripped[:60]!r}"

    if not isinstance(payload, list) or not payload:
        return True, [], "记忆区块的 JSON 不是非空数组"

    for index, item in enumerate(payload, start=1):
        problem = _validate_memory_item(index, item)
        if problem:
            return True, [], problem

    return True, payload, None


def verify_prompt_hash(text, row):
    """用 build_prompts.prompt_sha256() 的同一算法重算并比对归档行里的 prompt_sha256。"""

    expected = row.get("prompt_sha256")

    if not isinstance(expected, str) or not expected:
        return None, "归档行没有 prompt_sha256 字段，无法确认 Prompt 未被改写"

    system, user = parse_prompt_archive(text)
    actual = build_prompts.prompt_sha256(system, user)

    if actual != expected:
        return None, (
            f"Prompt 重算 SHA-256 与归档行不一致：重算 {actual[:16]}… / 归档 {expected[:16]}…"
        )

    return actual, None


def reconstruct_steps(group, feedback_count, memory_count):
    """按 run_experiment.evidence_steps() 的同一规则重建证据链。

    本实验不调用真实工具，证据由程序合成；这里只重建"当时注入了什么"。
    """

    slots = build_prompts.GROUP_SLOTS[group]
    steps = []

    if slots["feedback"]:
        steps.append(
            {
                "type": "tool_call",
                "tool": "get_recent_feedback",
                "arguments": {"days": 7},
                "ok": True,
                "error_type": None,
                "result": {"ok": True, "count": feedback_count},
            }
        )

    if slots["memory"]:
        steps.append(
            {
                "type": "tool_call",
                "tool": "get_learning_memory",
                "arguments": {},
                "ok": True,
                "error_type": None,
                "result": {"ok": True, "count": memory_count},
            }
        )

    return steps


def expected_memory_state(sample):
    """从设计冻结的样本夹具推断「实验时预期注入的记忆」。

    可信归档是 `samples/<sample>.json` 的 `gold_standard.memories[]`：每一项带
    `injected`，由同一份夹具经 build_prompts 的注入规则算出，已与全部归档 Prompt 逐条核对一致
    （S7-empty 为 `memories: []`）。这里只读归档，绝不用实时数据库状态冒充实验时的预期。

    返回 (state, count)：
      "required"     —— 预期至少一条记忆（缺记忆区块即 FAIL）
      "empty"        —— 经归档确认的零记忆（缺区块不算 FAIL，其余检查照旧）
      "unverifiable" —— 夹具缺失或结构不足以判断（不得默认放行）
    """

    gold = sample.get("gold_standard") if isinstance(sample, dict) else None
    if not isinstance(gold, dict):
        return "unverifiable", 0

    memories = gold.get("memories")
    if not isinstance(memories, list):
        return "unverifiable", 0

    count = 0
    for item in memories:
        if not isinstance(item, dict) or not isinstance(item.get("injected"), bool):
            return "unverifiable", 0
        if item["injected"]:
            count += 1

    return ("required", count) if count else ("empty", 0)


def grounding_for_row(row, run_dir, sample_cache, checker, memory_markers):
    """返回 (status, detail)。status ∈ PASS / FAIL / UNVERIFIABLE。

    证据链必须先自证：归档 Prompt 能安全解析、SHA-256 能按 build_prompts.prompt_sha256()
    重算一致、记忆区块里是真的记忆内容。任一步做不到就是 UNVERIFIABLE，不冒充 PASS；
    Prompt 自带记忆来源却没有可解析的真实记忆时是 FAIL。
    """

    sample_id = row.get("sample_id")
    group = row.get("group")
    answer = (row.get("final_output") or {}).get("answer") or ""
    plan = (row.get("final_output") or {}).get("plan") or {}
    reason = plan.get("reason") if isinstance(plan, dict) else None

    if sample_id not in sample_cache:
        try:
            sample_cache[sample_id] = seed_fixture.load_sample(sample_id)
        except Exception as error:  # noqa: BLE001 - 缺样本就无法判断，显式标注
            return "UNVERIFIABLE", f"读不到样本 {sample_id}：{type(error).__name__}: {error}"

    sample = sample_cache[sample_id]
    text = prompt_text(run_dir, sample_id, group)

    if text is None:
        return "UNVERIFIABLE", "缺少 Prompt 文件，无法确认当时注入的 evidence"

    try:
        _, user_text = parse_prompt_archive(text)
        verified_sha, hash_error = verify_prompt_hash(text, row)
    except ValueError as error:
        return "UNVERIFIABLE", f"归档 Prompt 格式无法解析：{error}"

    if hash_error:
        return "UNVERIFIABLE", hash_error

    header_present, payload, payload_error = parse_memory_payload(user_text)
    slots = build_prompts.GROUP_SLOTS[group]

    if header_present and payload_error:
        return "FAIL", f"Prompt 有记忆区块但取不到真实记忆内容：{payload_error}"

    if header_present and not slots["memory"]:
        return "FAIL", f"该组（{group}）不应注入长期记忆，但 Prompt 出现记忆区块"

    # 预期状态只能来自样本归档（gold_standard.memories[].injected），不能从组名硬编码、
    # 也不能看当前数据库：S7-empty 这类零记忆样本缺区块是正确结果，其他 C/D 缺区块才是 FAIL。
    memory_state, expected_count = ("unverifiable", 0)
    if slots["memory"]:
        memory_state, expected_count = expected_memory_state(sample)

        if memory_state == "unverifiable":
            return "UNVERIFIABLE", (
                f"样本 {sample_id} 的归档缺少 gold_standard.memories[].injected，"
                "无法确认实验时是否应注入记忆"
            )

        if not header_present and memory_state == "required":
            return "FAIL", (
                f"该组（{group}）按样本归档应注入长期记忆（预期 {expected_count} 条），"
                "但 Prompt 没有记忆区块"
            )

        if header_present and memory_state == "empty":
            return "FAIL", (
                f"样本 {sample_id} 的归档确认预期零记忆，但 Prompt 出现了记忆区块"
            )

    feedback_count = len(sample.get("feedbacks") or [])
    memory_count = len(payload) if header_present else 0
    steps = reconstruct_steps(group, feedback_count, memory_count)

    # 这里传入的是从归档 Prompt 里解析出来的真实记忆对象，
    # 与 agent_plan_draft._memory_block() 收到的同一份数据同构。
    memory_result = None
    if memory_count:
        memory_result = {"count": memory_count, "memories": payload}

    texts = [answer]
    if isinstance(reason, str):
        texts.append(reason)

    violations = checker(texts, steps, memory_result=memory_result)

    if violations:
        return "FAIL", "；".join(violations)

    claimed_memory = any(marker in answer for marker in memory_markers)
    detail = (
        f"Prompt SHA-256 重算一致（{verified_sha[:16]}…），记忆 {memory_count} 条；"
        "来源主张与注入一致"
    )
    if memory_count and not claimed_memory:
        detail = (
            f"Prompt SHA-256 重算一致（{verified_sha[:16]}…），注入记忆 {memory_count} 条；"
            "模型未声称记忆来源"
        )

    return "PASS", detail


# ---------------------------------------------------------------------------
# Y1：结构可保存率（真实调用 validate_plan_for_save）
# ---------------------------------------------------------------------------


def derive_difficult(sample):
    """与 agent_tools._derive_difficult_previous_task() 同一规则，用于交叉核对。"""

    feedbacks = sample.get("feedbacks") or []
    if not feedbacks:
        return False
    latest = feedbacks[-1]
    return latest.get("status") == "partial" and latest.get("reason") == "任务难度太高"


def y1_for_plan(plan, apis, sample_id, config):
    """在临时夹具库上调用真实 validate_plan_for_save()。

    返回 (status, detail)。status ∈ VALID / INVALID / UNAVAILABLE。
    """

    if apis is None:
        return "UNAVAILABLE", "未启用夹具库（--no-fixtures），无法调用 validate_plan_for_save()"

    api = apis
    try:
        if api.get("_sample") != sample_id:
            seed_fixture.seed_sample(sample_id, config, rebuild=True)
            api["_sample"] = sample_id
        result = api["agent_tools"].validate_plan_for_save(plan)
    except Exception as error:  # noqa: BLE001 - 夹具不可用就明确标注，不冒充通过
        api["_sample"] = None
        return "UNAVAILABLE", f"夹具库调用失败：{type(error).__name__}: {error}"

    if result.get("valid"):
        return "VALID", "validate_plan_for_save 通过"
    return "INVALID", str(result.get("reason"))


def build_apis(config):
    """一次性准备夹具与真实校验函数；失败时返回 (None, 原因)。"""

    try:
        apis = build_prompts.project_api(config)
        apis["_sample"] = None
        # 交叉核对用：纯结构校验器
        import ai_service  # noqa: E402 - 只在夹具隔离后导入

        apis["_ai_service"] = ai_service
        return apis, None
    except Exception as error:  # noqa: BLE001
        return None, f"{type(error).__name__}: {error}"


def simulate_completion(plan, sample):
    """§4.4 确定性学习者模拟器（模拟推演）。

    只有样本显式给出 ability_level 与 available_minutes 时才计算；
    否则返回 None，由调用方记 N/A。
    """

    ability = sample.get("ability_level")
    available = sample.get("available_minutes")
    if ability not in ABILITY_RANK or not is_number(available):
        return None
    if not isinstance(plan, dict):
        return False
    difficulty = plan.get("difficulty")
    minutes = plan.get("estimated_minutes")
    if difficulty not in ABILITY_RANK or not is_number(minutes):
        return False
    return ABILITY_RANK[difficulty] <= ABILITY_RANK[ability] and minutes <= available


# ---------------------------------------------------------------------------
# 指标汇总
# ---------------------------------------------------------------------------


def metric_rows_for_scope(scope, analyses):
    """把逐行分析结果汇成长表行：table,metric,group,unit,value,n,denominator,note。"""

    rows = []

    def add(metric, value, n, denominator="", group="all", unit="", note=""):
        rows.append(
            {
                "table": scope,
                "metric": metric,
                "group": group,
                "unit": unit,
                "value": "" if value is None else value,
                "n": n,
                "denominator": denominator,
                "note": note,
            }
        )

    total = len(analyses)

    # Y1 结构可保存率
    y1_valid = [a for a in analyses if a["y1"] == "VALID"]
    y1_invalid = [a for a in analyses if a["y1"] == "INVALID"]
    y1_unavailable = [a for a in analyses if a["y1"] == "UNAVAILABLE"]
    if total and not y1_unavailable:
        add("Y1_structural_saveable_rate", round(len(y1_valid) / total, 4), total, total,
            unit="ratio", note="validate_plan_for_save() 通过数/总数")
    elif y1_unavailable:
        add("Y1_structural_saveable_rate", "N/A", total, total, unit="ratio",
            note=f"{len(y1_unavailable)} 行夹具不可用，无法调用 validate_plan_for_save()；"
                 f"已通过 {len(y1_valid)}、未通过 {len(y1_invalid)}")

    # Y1 辅助：模拟完成率（仅当样本显式提供能力/时长）
    sim_values = [a["simulated"] for a in analyses if a["simulated"] is not None]
    if sim_values:
        add("Y1_simulated_completion_rate", round(sum(1 for v in sim_values if v) / len(sim_values), 4),
            len(sim_values), len(sim_values), unit="ratio",
            note="模拟推演：难度≤能力且时长≤可用时长记完成（非真实用户行为）")
    else:
        add("Y1_simulated_completion_rate", "N/A", 0, total, unit="ratio",
            note="样本未提供 ability_level / available_minutes，按 §4.4 口径不可计算")

    # Y2 调用次数
    calls = [a["request_count"] for a in analyses]
    if calls:
        add("Y2_calls_mean", round(mean(calls), 4), len(calls), unit="calls")
        add("Y2_calls_median", median(calls), len(calls), unit="calls")
        add("Y2_calls_max", max(calls), len(calls), unit="calls",
            note=f"起草 1 次 + 修正 0–{MAX_CORRECTIONS} 次")

    # Y3 延迟
    latencies = [a["elapsed_seconds"] for a in analyses]
    if latencies:
        add("Y3_latency_median_s", round(median(latencies), 3), len(latencies), unit="seconds")
        add("Y3_latency_p95_s", round(p95(latencies), 3), len(latencies), unit="seconds")

    # Y4 错误率：模型错误 + 超时 + 非法 JSON + 校验不过
    failed = [a for a in analyses if a["failed"]]
    add("Y4_error_rate", round(len(failed) / total, 4) if total else "N/A", total, total,
        unit="ratio",
        note="失败 = success=False 或 error_type 非空 或 plan 不可解析 或 validate_plan_for_save 未通过"
             + ("（Y1 不可用的行不计入校验失败）" if y1_unavailable else ""))

    # Y5 提示规模
    prompt_tokens = [a["prompt_tokens"] for a in analyses if a["prompt_tokens"] is not None]
    if prompt_tokens:
        add("Y5_prompt_tokens_median", median(prompt_tokens), len(prompt_tokens), unit="tokens",
            note="usage.prompt_tokens（首次请求）")
    else:
        add("Y5_prompt_tokens_median", "N/A", 0, total, unit="tokens", note="requests 中没有 prompt_tokens")
    add("Y5_messages_len_median", "N/A", 0, total, unit="messages",
        note="归档未保存 messages 数组；Prompt 文件只能确认 SYSTEM+USER 两条基础消息，"
             "不把推断值冒充为运行时消息条数")

    # grounding：分母只算可判定的行（PASS+FAIL），UNVERIFIABLE 单独计数、不进分母
    grounding_pass = [a for a in analyses if a["grounding"] == "PASS"]
    grounding_fail = [a for a in analyses if a["grounding"] == "FAIL"]
    grounding_unver = [a for a in analyses if a["grounding"] == "UNVERIFIABLE"]
    verifiable = len(grounding_pass) + len(grounding_fail)
    add("grounding_pass_rate",
        round(len(grounding_pass) / verifiable, 4) if verifiable else "N/A",
        verifiable, verifiable, unit="ratio",
        note=f"PASS {len(grounding_pass)} / FAIL {len(grounding_fail)} / "
             f"UNVERIFIABLE {len(grounding_unver)}（UNVERIFIABLE 不计入 PASS 也不进分母；"
             f"总行数 {len(analyses)}）")

    # 分组明细（只给 grounding 与 Y1，便于定位差异）
    for group in sorted({a["group"] for a in analyses}):
        subset = [a for a in analyses if a["group"] == group]
        group_pass = sum(1 for a in subset if a["grounding"] == "PASS")
        group_fail = sum(1 for a in subset if a["grounding"] == "FAIL")
        group_unver = sum(1 for a in subset if a["grounding"] == "UNVERIFIABLE")
        group_verifiable = group_pass + group_fail
        group_valid = sum(1 for a in subset if a["y1"] == "VALID")
        group_invalid = sum(1 for a in subset if a["y1"] == "INVALID")
        value = round(group_valid / len(subset), 4) if group_invalid or group_valid else "N/A"
        add("Y1_structural_saveable_rate", value, len(subset), len(subset), group=group,
            unit="ratio", note="按组拆分")
        add("grounding_pass_rate",
            round(group_pass / group_verifiable, 4) if group_verifiable else "N/A",
            group_verifiable, group_verifiable, group=group, unit="ratio",
            note=f"按组拆分；UNVERIFIABLE {group_unver} 不计入 PASS 也不进分母")

    return rows


def analyze_row(run_name, run_dir, number, row, apis, config, sample_cache, checker,
                memory_markers):
    """单行分析，返回 dict 与失败明细列表。"""

    failures = []
    sample_id = row.get("sample_id")
    group = row.get("group")
    final_output = row.get("final_output") if isinstance(row.get("final_output"), dict) else {}
    plan = final_output.get("plan")

    sample = sample_cache.get(sample_id)
    if sample is None:
        try:
            sample = seed_fixture.load_sample(sample_id)
            sample_cache[sample_id] = sample
        except Exception:  # noqa: BLE001 - 缺样本时样本相关指标显式标注
            sample = {}

    # Y1
    if isinstance(plan, dict):
        y1, y1_detail = y1_for_plan(plan, apis, sample_id, config)
    else:
        y1, y1_detail = "INVALID", "final_output.plan 缺失或不是对象"

    if y1 == "INVALID":
        failures.append({
            "scope": "",
            "run_dir": run_name,
            "row_number": number,
            "experiment_id": row.get("experiment_id") or "",
            "sample_id": sample_id or "",
            "group": group or "",
            "repeat_index": row.get("repeat_index"),
            "metric": "Y1",
            "detail": y1_detail,
        })

    # grounding
    grounding, grounding_detail = grounding_for_row(
        row, run_dir, sample_cache, checker, memory_markers
    )
    if grounding != "PASS":
        failures.append({
            "scope": "",
            "run_dir": run_name,
            "row_number": number,
            "experiment_id": row.get("experiment_id") or "",
            "sample_id": sample_id or "",
            "group": group or "",
            "repeat_index": row.get("repeat_index"),
            "metric": "grounding",
            "detail": f"{grounding}: {grounding_detail}",
        })

    # Y5 prompt tokens：取首次请求（归档是 list；兼容旧格式的 JSON 字符串）
    prompt_tokens = None
    requests = row.get("requests")
    if isinstance(requests, str):
        try:
            requests = json.loads(requests)
        except json.JSONDecodeError:
            requests = None
    if isinstance(requests, list) and requests and isinstance(requests[0], dict):
        value = requests[0].get("prompt_tokens")
        if is_int(value) and value > 0:
            prompt_tokens = value
    if prompt_tokens is None:
        usage = row.get("token_usage")
        if isinstance(usage, str):
            try:
                usage = json.loads(usage)
            except json.JSONDecodeError:
                usage = None
        if isinstance(usage, dict):
            value = usage.get("prompt_tokens")
            if is_int(value) and value > 0:
                prompt_tokens = value

    simulated = simulate_completion(plan if isinstance(plan, dict) else None, sample)

    failed = (
        row.get("success") is not True
        or row.get("error_type") is not None
        or not isinstance(plan, dict)
        or y1 == "INVALID"
    )

    analysis = {
        "group": group,
        "y1": y1,
        "grounding": grounding,
        "request_count": row.get("request_count"),
        "elapsed_seconds": row.get("elapsed_seconds"),
        "prompt_tokens": prompt_tokens,
        "simulated": simulated,
        "failed": failed,
    }

    return analysis, failures


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


METRIC_COLUMNS = ("table", "metric", "group", "unit", "value", "n", "denominator", "note")
FAILURE_COLUMNS = (
    "scope", "run_dir", "row_number", "experiment_id",
    "sample_id", "group", "repeat_index", "metric", "detail",
)


def write_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def render_report(path, payload):
    lines = []
    lines.append("# 自动指标核对报告（Y1–Y5 + grounding）")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{payload['generated_utc']}")
    lines.append(f"- 配置：`{payload['config_path']}`")
    lines.append(f"- 脚本：`experiments/graduation/check_auto.py`（v{payload['version']}）")
    lines.append("- 数据来源：`experiments/graduation/runs/*/results.jsonl` 与同目录 `prompts/`")
    lines.append("")
    lines.append("## 1. 只读与数据保护")
    lines.append("")
    lines.append("| run | 行数 | manifest 校验 | results.jsonl SHA-256（前后一致） |")
    lines.append("| --- | --- | --- | --- |")
    for item in payload["sources"]:
        lines.append(
            f"| {item['run']} | {item['rows']} | {item['manifest']} | "
            f"`{item['sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("运行前后逐文件比对 SHA-256 一致；未写入 runs/、plan.json、results.jsonl、feedback.db。")
    lines.append("")
    lines.append("## 2. 主实验（140 次生成）")
    lines.append("")
    lines.append(payload["main_table"])
    lines.append("")
    lines.append("## 3. 补充批次（单独成表，不并入主表）")
    lines.append("")
    lines.append(payload["supplement_table"])
    lines.append("")
    lines.append("## 4. 已知数据缺口（不以 PASS 代替）")
    lines.append("")
    for note in payload["gaps"]:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## 5. 失败清单")
    lines.append("")
    lines.append(f"共 {payload['failure_count']} 条，详见 `{payload['failures_name']}`。")
    lines.append("")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def md_table(rows):
    """把长表渲染成 Markdown（只保留核心列）。"""

    if not rows:
        return "（无数据）"
    lines = ["| 指标 | 组 | 值 | n | 说明 |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        note = str(row["note"]).replace("|", "\\|")
        lines.append(
            f"| {row['metric']} | {row['group']} | {row['value']} | "
            f"{row['n']} | {note} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def collect_scope(scope, run_names, runs_dir, apis, config, sample_cache, checker,
                  memory_markers):
    analyses = []
    failures = []
    sources = []

    hashes = manifest_hashes()

    # 第一遍：读行 + 核对 manifest。任何一条 run 的 manifest 不可信就整批终止，
    # 绝不"先把指标算出来再说" —— 不可信的源文件不配得到 PASS 结论。
    prepared = []
    untrusted = []
    for run_name in run_names:
        run_dir = Path(runs_dir) / run_name
        rows, problems = read_rows(run_dir)
        status, detail = verify_manifest(run_name, run_dir, hashes, len(rows))
        sources.append({
            "run": run_name,
            "rows": len(rows),
            "manifest": f"{status}（{detail}）",
            "sha256": sha256_file(Path(run_dir) / RESULTS_NAME),
        })
        if status not in ("match", "legacy_no_manifest"):
            untrusted.append(f"{run_name}: {status}（{detail}）")
        prepared.append((run_name, run_dir, rows, problems))

    if untrusted:
        raise CheckError(
            "manifest 校验未通过，已在计算指标前终止（fail-closed，--strict 与否都一样）："
            + "；".join(untrusted)
        )

    # 第二遍：manifest 与源文件可信之后才计算指标与 grounding
    for run_name, run_dir, rows, problems in prepared:
        for number, field, detail in problems:
            failures.append({
                "scope": scope,
                "run_dir": run_name,
                "row_number": number,
                "experiment_id": "",
                "sample_id": "",
                "group": "",
                "repeat_index": "",
                "metric": "schema",
                "detail": f"{field}: {detail}",
            })

        for number, row in rows:
            analysis, row_failures = analyze_row(
                run_name, run_dir, number, row, apis, config, sample_cache, checker,
                memory_markers,
            )
            analysis["scope"] = scope
            analyses.append(analysis)
            for failure in row_failures:
                failure["scope"] = scope
            failures.extend(row_failures)

    return analyses, failures, sources


def run(args):
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    runs_dir = Path(args.runs_dir).resolve()

    if not runs_dir.is_dir():
        raise CheckError(f"runs 目录不存在：{runs_dir}")

    sample_cache = {}

    # 顺序很重要：夹具隔离（project_modules）必须先于 database/agent_tools 的导入，
    # 否则 database.DATABASE_PATH 会先绑定真实 feedback.db。
    apis = None
    apis_error = None
    checker = None
    memory_markers = ()
    checker_error = None

    if args.no_fixtures:
        apis_error = "用户指定 --no-fixtures"
        checker_error = "用户指定 --no-fixtures，未导入检查器"
    else:
        apis, apis_error = build_apis(config)
        if apis is not None:
            checker = apis["agent_plan_draft"].check_grounding
            memory_markers = apis["agent_plan_draft"].MEMORY_CLAIM_MARKERS
            checker_error = None
        else:
            checker_error = apis_error

    if checker is None:
        # 没有夹具时仍可尝试导入 check_grounding（纯函数、不读库），用于补充批次之外的
        # 来源复核；但 Y1 会明确记 N/A，绝不用 SKIP 冒充 PASS。
        try:
            project_root = str(seed_fixture.PROJECT_ROOT)
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            import agent_plan_draft  # noqa: E402

            checker = agent_plan_draft.check_grounding
            memory_markers = agent_plan_draft.MEMORY_CLAIM_MARKERS
            checker_error = None
        except Exception as error:  # noqa: BLE001
            checker_error = f"{type(error).__name__}: {error}"

    if checker is None:
        # 没有检查器就无法计算 grounding；明确失败，不用 SKIP 冒充 PASS
        def checker(texts, steps, memory_result=None):  # noqa: ANN001
            raise CheckError(f"无法导入 agent_plan_draft.check_grounding：{checker_error}")

    before = {
        name: sha256_file(Path(runs_dir) / name / RESULTS_NAME)
        for name in [MAIN_RUN, *SUPPLEMENT_RUNS]
        if (Path(runs_dir) / name / RESULTS_NAME).is_file()
    }

    main_analyses, main_failures, main_sources = collect_scope(
        "main", [MAIN_RUN], runs_dir, apis, config, sample_cache, checker, memory_markers
    )
    supp_analyses, supp_failures, supp_sources = collect_scope(
        "supplement", list(SUPPLEMENT_RUNS), runs_dir, apis, config, sample_cache,
        checker, memory_markers,
    )

    smoke_sources = []
    if args.include_smoke:
        smoke_analyses, smoke_failures, smoke_sources = collect_scope(
            "smoke", [SMOKE_RUN], runs_dir, apis, config, sample_cache, checker,
            memory_markers,
        )
        supp_analyses += smoke_analyses
        supp_failures += smoke_failures

    after = {
        name: sha256_file(Path(runs_dir) / name / RESULTS_NAME)
        for name in before
    }
    if before != after:
        raise CheckError("results.jsonl 在核对过程中发生变化，疑似破坏只读约定，已终止")

    main_rows = metric_rows_for_scope("main", main_analyses)
    supp_rows = metric_rows_for_scope("supplement", supp_analyses)

    out_dir = Path(args.out_dir).resolve() if args.out_dir else (
        Path(tempfile.gettempdir()) / "graduation_auto_metrics"
    )
    main_csv = write_csv(out_dir / "auto_metrics.csv", METRIC_COLUMNS, main_rows)
    supp_csv = write_csv(out_dir / "auto_metrics_supplement.csv", METRIC_COLUMNS, supp_rows)
    failures = main_failures + supp_failures
    failures_csv = write_csv(out_dir / "auto_failures.csv", FAILURE_COLUMNS, failures)

    gaps = [
        "Y1 辅助指标（模拟完成率）：S1–S7 样本没有 ability_level / available_minutes 字段，"
        "按 §4.4 口径记 N/A；已实现 simulate_completion() 并在测试里用合成样本验证，"
        "但不用它推演正式数据。",
        "Y5 len(messages)：归档 results.jsonl 没有 messages 数组，记 N/A；"
        "prompt tokens 来自 requests[0].prompt_tokens，是真实记录。",
        "grounding：归档没有保存 steps 与最终 draft 消息，本脚本按 Prompt 中实际注入的"
        "反馈/记忆区块重建 evidence；若模型未声称记忆来源，则结论不受该限制影响，"
        "已逐行记 UNVERIFIABLE 的行不与 PASS 混算。",
        "B0 legacy 两个早期 run 合计 10 行，manifest 未收录其 results.jsonl，"
        "SHA-256 以现场计算值为准（详见第 1 节）。",
    ]
    if apis is None:
        gaps.append(f"Y1 主指标：夹具库不可用（{apis_error}），记 N/A，未冒充通过。")

    payload = {
        "generated_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_path": str(config_path),
        "version": VERSION,
        "sources": main_sources + supp_sources + smoke_sources,
        "main_table": md_table(main_rows),
        "supplement_table": md_table(supp_rows),
        "gaps": gaps,
        "failure_count": len(failures),
        "failures_name": failures_csv.name,
    }
    report = render_report(out_dir / "auto_metrics_report.md", payload)

    print(f"主实验：{len(main_analyses)} 行 → {main_csv}")
    print(f"补充批次：{len(supp_analyses)} 行 → {supp_csv}")
    print(f"失败清单：{len(failures)} 条 → {failures_csv}")
    print(f"报告：{report}")
    print("只读校验：results.jsonl SHA-256 前后一致")

    if apis_error:
        print(f"注意：Y1 夹具不可用（{apis_error}），已记 N/A")

    if args.strict and failures:
        return 1
    return 0


VERSION = "1.0.0"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="自动指标核对：Y1–Y5 与 grounding")
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--runs-dir", default=str(HERE / "runs"))
    parser.add_argument("--out-dir", default=None,
                        help="输出目录，默认 %%TEMP%%/graduation_auto_metrics")
    parser.add_argument("--no-fixtures", action="store_true",
                        help="不重建临时夹具库，Y1 主指标记 N/A")
    parser.add_argument("--include-smoke", action="store_true",
                        help="把 internal-api-smoke 计入补充表（默认排除）")
    parser.add_argument("--strict", action="store_true",
                        help="有任何失败明细时返回非零")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except CheckError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
