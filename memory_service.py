"""学习记忆服务：主题归一化、内容渲染、记忆状态机与全量重建（第二阶段 Task 2/3/4）。

Batch A（Task 2）只实现两个纯函数：
- normalize_subject(task)：把任务名映射成稳定的 subject_key / subject_label
- render_content(...)：由结构化取值渲染记忆文本

Batch B（Task 3/4）在本模块追加状态机，严格对齐 v16 设计文档 §5：
- `apply_feedback(trigger, as_of)`（内部单步）→ `apply_pending_updates()`（在线唯一入口）
- `rebuild_all()`：同一套算法的全量回放版本
- `retrieve()` / `archive_stale()`：检索与派生式 stale 入口

为什么把渲染放在程序里而不是让大模型写：记忆内容一旦由模型生成，就无法
从 value_json 复算、也无法作为论文里的可复核结论。模板只吃 value_json 里的
数值，同一份证据永远渲染出同一句话。

时间口径（v16 §5.2/§5.3.2）：`as_of` **永远是触发反馈的 created_at**，不是
`now()`；只有 `apply_pending_updates()`/`rebuild_all()` 收尾的批量 stale 判定
和游标 updated_at 用 `effective_now`。因此在线与重建能逐条复现同一批结论。
"""

import json
import sqlite3
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone

import database

# 记忆类型（顺序即文档顺序，检索与测试都按它遍历）
MEMORY_TYPES = (
    "difficulty",
    "task_performance",
    "time_pattern",
    "task_topic",
)

# 各类型的关系取值：neutral 表示方向未定（不写证据、不计分子分母）
RELATIONS = ("supports", "contradicts", "neutral")

# 生成门槛 / 稳定门槛，单位随类型不同：
# - difficulty 数 supports 条数
# - task_performance / time_pattern 数样本条数（含反证）
# - task_topic 数出现次数
REQUIRED = {
    "difficulty": 2,
    "task_performance": 3,
    "time_pattern": 4,
    "task_topic": 3,
}

STABLE = {
    "difficulty": 4,
    "task_performance": 6,
    "time_pattern": 8,
    "task_topic": 6,
}

# 记忆窗口（天）：所有规则只看窗口内的反馈
MEMORY_WINDOW_DAYS = 30

# 主题文本长度上限
SUBJECT_KEY_MAX_CHARS = 120
SUBJECT_LABEL_MAX_CHARS = 60

# 归一化时剥离的尾部后缀（顺序无关，逐个尝试）
SUBJECT_SUFFIXES = ("练习册", "练习题", "练习", "复习", "习题")

# 难度值 -> 可读中文（time_pattern 的 subject_label 用）
DIFFICULTY_LABELS = {
    "easy": "简单",
    "medium": "中等",
    "hard": "困难",
}

# 模板：字段名与 value_json 的键严格一致
TEMPLATES = {
    "difficulty": "「{subject}」近期 {failed_count} 次因难度太高未完成（{window_days} 天内）",
    "task_performance": "「{subject}」近期 {samples} 次任务完成率 {rate}",
    "time_pattern": "{difficulty_label} 任务近期 {samples} 次实际用时约为预估的 {ratio} 倍",
    "task_topic": "「{subject}」近期反馈中 {appearances} 次出现",
}

# 证据不足稳定门槛时的前缀
TENTATIVE_PREFIX = "初步观察："

# ==========================
# Task 3/4：状态机常量（v16 §5）
# ==========================

# 反馈枚举（服务端契约 Task 0）：读取侧 fail-closed，脏值既不计分子也不计分母
VALID_STATUSES = ("completed", "partial", "not_completed")

VALID_REASONS = (
    "任务时间太长",
    "任务难度太高",
    "临时有事情",
    "今天状态不好",
    "其他",
)

# 关系判定用到的两个具体原因值
REASON_TOO_HARD = "任务难度太高"

# 记忆状态（v16 §3.2）：active / weakened / resolved / archived
MEMORY_STATUSES = ("active", "weakened", "resolved", "archived")

# stale 门槛（天）：只看最近一次 supports 证据的时间，不看反证（§5.3.4）
STALE_DAYS = 60

# 未来样本偏移上限（天）：超过 as_of + 30d 的候选判为脏数据（§5.3.2 第 10 条）
FUTURE_SKEW_MAX_DAYS = MEMORY_WINDOW_DAYS

# rebuild 的安全上限：超过就拒绝，不写任何数据（§4.2）
REBUILD_MAX_FEEDBACKS = 5000

# 检索条数硬上限（§4.2 retrieve）
RETRIEVE_MAX_LIMIT = 10

# retrieve 回传的证据 feedback_id 条数上限（§4.2）
RETRIEVE_EVIDENCE_LIMIT = 5

# 事件类型（v16 §3.5）：八种，没有 deleted
EVENT_CREATED = "created"
EVENT_STRENGTHENED = "strengthened"
EVENT_WEAKENED = "weakened"
EVENT_RESOLVED = "resolved"
EVENT_ARCHIVED = "archived"
EVENT_REOPENED = "reopened"
EVENT_REOPENED_FROM_ARCHIVED = "reopened_from_archived"
EVENT_PENDING_DRAINED = "pending_drained"

# 稳定错误类型（§4.2 错误语义）
ERROR_INVALID_ARGUMENT = "invalid_argument"
ERROR_NOT_FOUND = "not_found"
ERROR_DB = "db_error"
ERROR_REBUILD_TOO_LARGE = "rebuild_too_large"
ERROR_DATA_INCONSISTENCY = "data_inconsistency"


class MemoryError(Exception):
    """带稳定 error_type 的业务异常；调用方按 error_type 决定 HTTP 语义。"""

    def __init__(self, error_type, message):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _clean_text(value):
    """折叠空白 + NFKC 规范化；非字符串一律当空。"""

    if value is None:
        return ""

    text = unicodedata.normalize("NFKC", str(value))
    return " ".join(text.split())


def _ascii_lower(text):
    """只把 A-Z 转小写，其他字符原样保留（v16 归一化规则）。

    不能用 str.lower()：它按 Unicode 大小写映射改写非 ASCII 字母（如 `Ä`、`Σ`），
    会让「Ärger」和「ärger」被当成同一主题，也会把拉丁/希腊/西里尔字母的原始
    写法悄悄改掉。主题 key 只承诺 ASCII 大小写不敏感。
    """

    return "".join(chr(ord(char) + 32) if "A" <= char <= "Z" else char for char in text)


def normalize_subject(task):
    """任务名 -> (subject_key, subject_label)；空任务返回 (None, None)。

    归一化是确定性的、可单测的字符串处理，不做编辑距离、不做向量相似度：
    - NFKC：全角/半角、兼容字符统一
    - 折叠空白：连续空格/换行/制表符变一个空格
    - 剥离尾部「练习/复习/练习册/练习题/习题」
    - ASCII 字母转小写（中文不受影响）
    - key 截 120 字符、label 截 60 字符
    """

    label = _clean_text(task)

    if not label:
        return None, None

    key = label
    stripped = True
    while stripped:
        stripped = False
        for suffix in SUBJECT_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix):
                key = key[: -len(suffix)].rstrip()
                stripped = True
                break

    if not key:
        key = label

    key = _ascii_lower(key)[:SUBJECT_KEY_MAX_CHARS].strip()
    label = label[:SUBJECT_LABEL_MAX_CHARS]

    if not key:
        return None, None

    return key, label


def _require_number(value, name, integer=True, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值，收到 {type(value).__name__}")

    if integer and not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {type(value).__name__}")

    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}，收到 {value}")

    return value


def _format_rate(rate):
    _require_number(rate, "completion_rate", integer=False)
    return "{:.0%}".format(rate)


def _format_ratio(ratio):
    _require_number(ratio, "median_ratio", integer=False)
    return "{:.1f}".format(ratio)


def _pick(value, name):
    """取 value_json 的必填字段；缺失时抛 ValueError（不返回 None 蒙混）。"""

    if name not in value:
        raise ValueError(f"value 缺少字段：{name}")

    return value[name]


def _stable_unit(memory_type, fields, supports):
    """稳定门槛的计入单位（v16 §5.5）：同一记忆只有一套门槛口径。

    difficulty 数 supports，task_performance / time_pattern 数有效样本 samples，
    task_topic 数出现次数。单位直接从模板已校验的字段里取，避免「门槛用 A、
    文案用 B」两套口径打架。
    """

    if memory_type == "difficulty":
        return supports
    if memory_type in ("task_performance", "time_pattern"):
        return fields["samples"]
    return fields["appearances"]


def render_content(memory_type, value, subject_label, supports=0):
    """由结构化取值渲染记忆文本（纯函数，不含任何模型调用）。

    value 必须是 dict（value_json 的原样解析结果）；字段缺失或类型不符直接
    抛 ValueError，避免把脏数据渲染成一句看似正常的结论。
    supports 是当前轮已计入的支持条数，只有 difficulty 用它判稳定门槛；
    其余类型按 v16 §5.5 的计入单位（samples / appearances）判。
    """

    if memory_type not in TEMPLATES:
        raise ValueError(f"未知记忆类型：{memory_type}")

    if not isinstance(value, dict):
        raise ValueError("value 必须是 dict")

    _require_number(supports, "supports")
    supports = int(supports)

    if not isinstance(subject_label, str):
        raise ValueError("subject_label 必须是字符串")

    if memory_type == "difficulty":
        fields = {
            "subject": subject_label,
            "failed_count": _require_number(_pick(value, "failed_count"), "failed_count"),
            "window_days": _require_number(
                value.get("window_days", MEMORY_WINDOW_DAYS), "window_days"
            ),
        }
    elif memory_type == "task_performance":
        fields = {
            "subject": subject_label,
            "samples": _require_number(_pick(value, "samples"), "samples"),
            "rate": _format_rate(_pick(value, "completion_rate")),
        }
    elif memory_type == "time_pattern":
        difficulty = _pick(value, "difficulty")
        if difficulty not in DIFFICULTY_LABELS:
            raise ValueError(f"未知难度值：{difficulty}")
        fields = {
            "difficulty_label": DIFFICULTY_LABELS[difficulty],
            "samples": _require_number(_pick(value, "samples"), "samples"),
            "ratio": _format_ratio(_pick(value, "median_ratio")),
        }
    else:
        fields = {
            "subject": subject_label,
            "appearances": _require_number(_pick(value, "appearances"), "appearances"),
        }

    content = TEMPLATES[memory_type].format(**fields)

    if _stable_unit(memory_type, fields, supports) < STABLE[memory_type]:
        content = TENTATIVE_PREFIX + content

    return content


def encode_value(value):
    """value_json 的确定性序列化：键排序、无多余空格、保留中文。"""

    if not isinstance(value, dict):
        raise ValueError("value 必须是 dict")

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_value(raw):
    """value_json -> dict；损坏的 JSON 抛 ValueError（不返回空 dict 蒙混）。"""

    if not isinstance(raw, str):
        raise ValueError("value_json 必须是字符串")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"value_json 不是合法 JSON：{error}") from error

    if not isinstance(parsed, dict):
        raise ValueError("value_json 必须是 JSON 对象")

    return parsed


# ==========================
# Task 3：时间、候选与纯函数评估（v16 §5.2）
# ==========================


def _parse_ts(value):
    """把库里的时间戳字符串解析成 naive datetime。

    库里统一存 `datetime.now().isoformat()` 的本地无时区字符串；这里也接受带
    空格的写法（"2026-01-05 10:00:00"）。带时区的输入按「墙钟时间」处理并
    丢掉 tzinfo，避免 aware / naive 混算直接抛 TypeError。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间戳必须是非空字符串")

    text = value.strip()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"非法时间戳：{value}") from error

    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)

    return parsed


def _window_start(as_of):
    """as_of 对应的 30 天窗口下界（返回 datetime）。"""

    return _parse_ts(as_of) - timedelta(days=MEMORY_WINDOW_DAYS)


def _future_limit(as_of):
    """as_of 允许的未来偏移上限（as_of + 30d）。"""

    return _parse_ts(as_of) + timedelta(days=FUTURE_SKEW_MAX_DAYS)


def _in_window(created_at, as_of):
    """`as_of − 30d ≤ created_at ≤ as_of`（解析后比较，不依赖字符串精度）。"""

    moment = _parse_ts(created_at)
    return _window_start(as_of) <= moment <= _parse_ts(as_of)


def _days_between(earlier, later):
    """later − earlier 的天数（浮点，可负）。"""

    delta = _parse_ts(later) - _parse_ts(earlier)
    return delta.total_seconds() / 86400.0


def _is_valid_status(status):
    return isinstance(status, str) and status in VALID_STATUSES


def _is_valid_reason(status, reason):
    """按服务端枚举校验一行反馈；completed 仍要求 reason（若给）合法。"""

    if not _is_valid_status(status):
        return False

    if reason is None:
        return status == "completed"

    return isinstance(reason, str) and reason in VALID_REASONS


def _is_stale(memory, as_of, threshold_days=None):
    """派生式 stale（§5.3.4）：距最近一次 supports 超过 60 天。

    只看 supports（`last_supported_at` 只被 supports 刷新），反证不续命；
    负 age（时间戳乱序）意味着这条记忆比当前反馈更新，不 stale。
    """

    last_supported_at = memory.get("last_supported_at")

    if not last_supported_at:
        return False

    threshold = STALE_DAYS if threshold_days is None else threshold_days

    return _days_between(last_supported_at, as_of) > threshold


def subject_key_for(memory_type, trigger, conn=None):
    """按类型给出 (key, label)：time_pattern 的键是 plans.difficulty。

    time_pattern 必须能追到有效计划：`plan_id` 为空、计划缺失、难度非法或
    `estimated_minutes <= 0` 时整类跳过（返回 (None, None)），绝不猜难度。
    """

    if memory_type == "time_pattern":
        plan_id = trigger.get("plan_id")

        if not plan_id:
            return (None, None)

        plan = database.get_plan_by_id(plan_id, conn=conn)

        if not plan:
            return (None, None)

        difficulty = plan.get("difficulty")

        if difficulty not in DIFFICULTY_LABELS:
            return (None, None)

        estimated = plan.get("estimated_minutes")

        if not isinstance(estimated, int) or isinstance(estimated, bool) or estimated <= 0:
            return (None, None)

        return (difficulty, DIFFICULTY_LABELS[difficulty])

    return normalize_subject(trigger.get("task"))


def _ratio_for(row, plan_difficulty=None):
    """实际/预估的比值；预估为 0 或非法时返回 None（该样本不参与比例）。"""

    estimated = row.get("estimated_minutes")
    actual = row.get("actual_minutes")

    for number in (estimated, actual):
        if isinstance(number, bool) or not isinstance(number, int):
            return None

    if estimated <= 0 or actual < 0:
        return None

    return actual / float(estimated)


def _relation_for(memory_type, direction, row, plan_difficulty=None):
    """按已固定的 direction 判定单行反馈的 relation（§5.2 映射表）。

    direction 为 None 表示方向未定：调用方只能写 pending，不能猜 relation。
    """

    status = row.get("status")
    reason = row.get("reason")

    if memory_type == "difficulty":
        if status in ("partial", "not_completed"):
            return "supports" if reason == REASON_TOO_HARD else "neutral"
        if status == "completed":
            return "contradicts"
        return None

    if memory_type == "task_performance":
        if direction not in ("low", "high"):
            return None
        completed = status == "completed"
        if direction == "low":
            return "supports" if not completed else "contradicts"
        return "supports" if completed else "contradicts"

    if memory_type == "time_pattern":
        ratio = _ratio_for(row, plan_difficulty)

        if ratio is None:
            return None

        if direction not in ("slow", "fast"):
            return None

        if direction == "slow":
            if ratio >= 1.5:
                return "supports"
            if ratio <= 0.4:
                return "contradicts"
            return "neutral"

        if ratio <= 0.4:
            return "supports"
        if ratio >= 1.5:
            return "contradicts"
        return "neutral"

    if memory_type == "task_topic":
        return "supports"

    return None


def _plan_difficulty_for_row(row, subject_key, conn, cache=None):
    """time_pattern 候选的 plan 有效性校验（§5.2），返回合法 difficulty 或 None。

    四件事必须同时成立，缺一即视为无效候选（不参与 direction/samples/median/
    REQUIRED/STABLE，也不写正式证据）：plan 行存在、difficulty ∈ easy/medium/hard、
    difficulty 等于当前记忆的 subject_key、plans.estimated_minutes > 0。
    任何时候都重新回表核对，不信任反馈自带的 estimated_minutes。
    """

    plan_id = row.get("plan_id")

    if plan_id is None:
        return None

    if cache is not None and plan_id in cache:
        plan = cache[plan_id]
    else:
        plan = database.get_plan_by_id(plan_id, conn=conn)

        if cache is not None:
            cache[plan_id] = plan

    if plan is None:
        return None

    difficulty = plan.get("difficulty")

    if difficulty not in DIFFICULTY_LABELS:
        return None

    if difficulty != subject_key:
        return None

    estimated = plan.get("estimated_minutes")

    if isinstance(estimated, bool) or not isinstance(estimated, int) or estimated <= 0:
        return None

    return difficulty


def _valid_candidates(memory_type, rows, plan_difficulties=None):
    """过滤掉枚举非法、以及 time_pattern 里 plan 无效或比例不可算的行。

    返回 (有效行, 非法枚举行数, 无效计划行数)。两类非法行既不计分子也不计分母，
    分别计 info.invalid_enum 与 info.invalid_plan。time_pattern 的每一行都必须在
    plan_difficulties 里（调用方已按 §5.2 校验过 plan 存在、难度匹配、预估 > 0）。
    """

    valid = []
    invalid = 0
    invalid_plan = 0

    for row in rows:
        if not _is_valid_reason(row.get("status"), row.get("reason")):
            invalid += 1
            continue

        if memory_type == "time_pattern":
            difficulty = None if plan_difficulties is None else plan_difficulties.get(row.get("id"))
            if difficulty is None:
                invalid_plan += 1
                continue
            if _ratio_for(row, difficulty) is None:
                invalid += 1
                continue

        valid.append(row)

    return (valid, invalid, invalid_plan)


def evaluate(memory_type, candidates, as_of, plan_difficulties=None):
    """纯函数：由候选集算出本轮结论（§5.2 计算 / §5.5 计入单位）。

    返回 dict（**永远返回，不用 None 表达样本不足**，否则非法候选计数会丢）：
      - `unit`：本次的门槛计数（difficulty 数 supports，其余数有效样本）
      - `sufficient`：`unit >= REQUIRED[memory_type]`
      - `invalid`：被剔除的非法枚举行数（计 `info.invalid_enum`）
      - `direction`：None 表示方向不可定（只能保持 pending）
      - `value`：写进 value_json 的结论字段（不含 direction，由调用方补）
      - `relations`：{feedback_id: relation}，neutral 也在内
    """

    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"未知记忆类型：{memory_type}")

    rows, invalid, invalid_plan = _valid_candidates(memory_type, candidates, plan_difficulties)
    required = REQUIRED[memory_type]

    if memory_type == "difficulty":
        supports = [r for r in rows if _relation_for(memory_type, None, r) == "supports"]
        unit = len(supports)
        relations = {}
        for row in rows:
            relations[row["id"]] = _relation_for(memory_type, None, row)
        return {
            "unit": unit,
            "sufficient": unit >= required,
            "invalid": invalid,
            "invalid_plan": invalid_plan,
            "direction": "failure",
            "value": {
                "failed_count": unit,
                "reason": REASON_TOO_HARD,
                "window_days": MEMORY_WINDOW_DAYS,
            },
            "relations": relations,
        }

    if memory_type == "task_performance":
        samples = len(rows)
        completed = len([r for r in rows if r.get("status") == "completed"])
        rate = (completed / float(samples)) if samples else 0.0

        if samples and rate <= 0.5:
            direction = "low"
        elif samples and rate >= 0.8:
            direction = "high"
        else:
            direction = None

        relations = {}
        for row in rows:
            relations[row["id"]] = _relation_for(memory_type, direction, row)
        return {
            "unit": samples,
            "sufficient": samples >= required,
            "invalid": invalid,
            "invalid_plan": invalid_plan,
            "direction": direction,
            "value": {
                "completion_rate": round(rate, 6),
                "samples": samples,
            },
            "relations": relations,
        }

    if memory_type == "time_pattern":
        ratios = []

        for row in rows:
            difficulty = None if plan_difficulties is None else plan_difficulties.get(row.get("id"))
            ratio = _ratio_for(row, difficulty)
            if ratio is not None:
                ratios.append(ratio)

        samples = len(ratios)
        median_ratio = statistics.median(ratios) if ratios else None

        if median_ratio is not None and median_ratio >= 1.5:
            direction = "slow"
        elif median_ratio is not None and median_ratio <= 0.4:
            direction = "fast"
        else:
            direction = None

        relations = {}
        for row in rows:
            difficulty = None if plan_difficulties is None else plan_difficulties.get(row.get("id"))
            relations[row["id"]] = _relation_for(memory_type, direction, row, difficulty)

        difficulty_value = None
        for row in rows:
            difficulty_value = None if plan_difficulties is None else plan_difficulties.get(row.get("id"))
            if difficulty_value:
                break

        return {
            "unit": samples,
            "sufficient": samples >= required,
            "invalid": invalid,
            "invalid_plan": invalid_plan,
            "direction": direction,
            "value": {
                "difficulty": difficulty_value,
                "median_ratio": None if median_ratio is None else round(median_ratio, 6),
                "samples": samples,
            },
            "relations": relations,
        }

    # task_topic：出现即支持，没有反证
    unit = len(rows)
    relations = {row["id"]: "supports" for row in rows}
    return {
        "unit": unit,
        "sufficient": unit >= required,
        "invalid": invalid,
        "invalid_plan": invalid_plan,
        "direction": "appearance",
        "value": {"appearances": unit},
        "relations": relations,
    }


def compute_strength(memory_type, unit, supports, contradicts, as_of, last_supported_at):
    """启发式证据强度分数（§5.5）：不是概率，未做统计校准。

    base 与封顶必须用同一个 unit；age_days 钳到 0，绝不让更早的时间戳
    反而抬高 strength。
    """

    if not last_supported_at:
        return 0.0

    age_days = max(0.0, _days_between(last_supported_at, as_of))
    base = min(1.0, unit / float(STABLE[memory_type]))
    recency = 0.5 ** (age_days / float(MEMORY_WINDOW_DAYS))
    penalty = 0.6 ** contradicts
    raw = base * recency * penalty
    raw = max(0.05, min(0.99, raw))
    cap = 0.6 if unit < STABLE[memory_type] else 0.9

    return round(min(raw, cap), 6)


# ==========================
# Task 3：证据、缓冲区与状态机（v16 §5.3）
# ==========================


def _new_info():
    """info 计数器：诊断用，不参与任何判定（除测试断言外）。"""

    return {
        "already_evidenced": 0,
        "invalid_enum": 0,
        "invalid_plan": 0,
        "neutral_dropped": 0,
        "direction_undetermined": 0,
        "insufficient_evidence": 0,
        "pending_expired": 0,
        "pending_future_id": 0,
        "pending_future_skipped": 0,
        "pending_out_of_range_dropped": 0,
        "pending_orphan_dropped": 0,
        "pending_episode_closed": 0,
        "future_out_of_range": 0,
        "evidence_written": 0,
        "events_written": 0,
        "memories_created": 0,
        "memories_updated": 0,
        "memory_ids": [],
    }


def _note_memory_id(info, memory_id):
    if memory_id not in info["memory_ids"]:
        info["memory_ids"].append(memory_id)


def _episode_counts(memory_id, episode, conn):
    """当前轮计数一律由正式证据 COUNT 重算（§5.2 规则 5），并取 supports 的最新时间。"""

    supports = database.count_evidence(memory_id, episode=episode, relation="supports", conn=conn)
    contradicts = database.count_evidence(
        memory_id, episode=episode, relation="contradicts", conn=conn
    )
    supports_rows = database.list_evidence(
        memory_id, episode=episode, relation="supports", conn=conn
    )
    last_supported_at = None

    for row in supports_rows:
        if last_supported_at is None or row["created_at"] > last_supported_at:
            last_supported_at = row["created_at"]

    return {"supports": supports, "contradicts": contradicts, "last_supported_at": last_supported_at}


def _write_evidence(memory_id, feedback_row, episode, relation, conn):
    """写一行正式证据，并对 INSERT OR IGNORE 的冲突做读回核对（§5.1 第 2 条）。

    返回 (evidence_row_id, inserted)。核对不一致抛 data_inconsistency：
    绝不覆盖既有行，也不把冲突当成成功。
    """

    result = database.insert_evidence(
        memory_id,
        feedback_row["id"],
        feedback_row.get("plan_id"),
        episode,
        relation,
        feedback_row["created_at"],
        weight=1.0,
        conn=conn,
    )

    if result["inserted"]:
        return (result["row_id"], True)

    existing = database.get_evidence(memory_id, feedback_row["id"], conn=conn)

    if not existing:
        raise MemoryError(
            ERROR_DATA_INCONSISTENCY,
            f"证据写入被忽略却读不到既有行：memory_id={memory_id} feedback_id={feedback_row['id']}",
        )

    if (
        existing["relation"] != relation
        or existing["episode"] != episode
        or existing["plan_id"] != feedback_row.get("plan_id")
    ):
        raise MemoryError(
            ERROR_DATA_INCONSISTENCY,
            "既有证据与本次判定不一致，需 rebuild_all 重建："
            f"memory_id={memory_id} feedback_id={feedback_row['id']} "
            f"既有=({existing['relation']},{existing['episode']},{existing['plan_id']}) "
            f"本次=({relation},{episode},{feedback_row.get('plan_id')})",
        )

    return (existing["id"], False)


def _write_event(memory_id, event_type, detail, created_at, conn, info=None):
    """写一条事件；detail 必须是纯业务投影（键排序、无随机值，§5.3.8）。"""

    detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    database.insert_memory_event(memory_id, event_type, detail_json, created_at, conn=conn)

    if info is not None:
        info["events_written"] += 1


def _event_detail(as_of, episode, evidence_ids, **extra):
    """构造 detail_json 的字典：evidence_ids 升序去重，可选键不适用就省略。"""

    detail = {
        "as_of": as_of,
        "episode": episode,
        "evidence_ids": sorted({int(mid) for mid in evidence_ids}),
    }

    for key, value in extra.items():
        if value is not None:
            detail[key] = value

    return detail


def _conclusion_payload(memory_type, value, subject_label, supports, contradicts, as_of, last_supported_at):
    """由 value + 计数算出 content / strength / value_json（同一套公式，无分支漂移）。"""

    value = dict(value)
    unit = _stable_unit(memory_type, value, supports)
    strength = compute_strength(
        memory_type, unit, supports, contradicts, as_of, last_supported_at
    )
    content = render_content(memory_type, value, subject_label, supports)

    return {
        "content": content,
        "value_json": encode_value(value),
        "strength": strength,
    }


def _candidate_rows(memory_type, trigger, as_of, key, lower_bound, conn):
    """取本轮候选：窗口内、id ≤ 触发反馈、id > 轮次下界（§5.2 候选规则）。

    返回 (rows, plan_difficulties)。SQL 只做粗筛（时间窗向两侧各放宽 1 天、
    id 上界），窗口边界与主题归一化在 Python 里精确判定，避免字符串精度差异。
    """

    start = (_parse_ts(as_of) - timedelta(days=MEMORY_WINDOW_DAYS + 1)).isoformat()
    end = (_parse_ts(as_of) + timedelta(days=1)).isoformat()

    if memory_type == "time_pattern":
        rows = database.list_feedbacks_with_plan(
            start, end, trigger["id"], difficulties=[key], conn=conn
        )
        plan_difficulties = {row["id"]: row.get("difficulty") for row in rows}
    else:
        rows = database.list_feedbacks_between(start, end, max_id=trigger["id"], conn=conn)
        plan_difficulties = None

    selected = []

    for row in rows:
        if not _in_window(row["created_at"], as_of):
            continue

        if lower_bound is not None and row["id"] <= lower_bound:
            continue

        if memory_type == "time_pattern":
            if row.get("difficulty") != key:
                continue
            if _ratio_for(row) is None:
                continue
        elif normalize_subject(row.get("task"))[0] != key:
            continue

        selected.append(row)

    return (selected, plan_difficulties)


def _append_pending(memory, trigger, as_of, conn, info):
    """把一条候选写进缓冲区（§5.3.2）：先查正式证据，越界未来直接丢弃。"""

    existing = database.get_evidence(memory["id"], trigger["id"], conn=conn)

    if existing:
        info["already_evidenced"] += 1
        return False

    if not _is_valid_reason(trigger.get("status"), trigger.get("reason")):
        info["invalid_enum"] += 1
        return False

    if _parse_ts(trigger["created_at"]) > _future_limit(as_of):
        info["future_out_of_range"] += 1
        return False

    episode = memory.get("pending_episode")

    if episode is None:
        episode = memory["episode"] + 1

    result = database.insert_pending(
        memory["id"],
        trigger["id"],
        trigger.get("plan_id"),
        episode,
        trigger["created_at"],
        as_of,
        conn=conn,
    )

    if not result["inserted"]:
        # 唯一索引冲突：同一反馈已在缓冲区，属幂等重复（不再计数），核对后返回
        rows = database.list_pending(memory["id"], episode=episode, conn=conn)
        for row in rows:
            if row["feedback_id"] == trigger["id"]:
                return False

        raise MemoryError(
            ERROR_DATA_INCONSISTENCY,
            f"pending 写入冲突却读不到行：memory_id={memory['id']} feedback_id={trigger['id']}",
        )

    return True


def _pending_window(memory, trigger, as_of, episode, conn, info):
    """共用窗口过滤器（§5.3.2 第 5 条）：五类分流，顺序固定。

    返回有效行列表（含 feedback 原始字段），已按 feedback_id 升序。
    过期行与越界未来行在这里就地删除并计数；未来行、上界外行原样保留。
    """

    pending_rows = database.list_pending(memory["id"], episode=episode, conn=conn)

    if not pending_rows:
        return []

    feedbacks = database.list_feedbacks_by_ids(
        [row["feedback_id"] for row in pending_rows], conn=conn
    )
    by_id = {row["id"]: row for row in feedbacks}

    window_start = _window_start(as_of)
    as_of_dt = _parse_ts(as_of)
    future_limit = _future_limit(as_of)

    expired_ids = []
    out_of_range_ids = []
    orphan_ids = []
    future_id_rows = []
    future_rows = []
    valid = []

    for row in pending_rows:
        feedback = by_id.get(row["feedback_id"])

        if feedback is None:
            orphan_ids.append(row["feedback_id"])
            continue

        created = _parse_ts(row["created_at"])

        if created < window_start:
            expired_ids.append(row["feedback_id"])
            continue

        if created > future_limit:
            out_of_range_ids.append(row["feedback_id"])
            continue

        if row["feedback_id"] > trigger["id"]:
            future_id_rows.append(row["feedback_id"])
            continue

        if created > as_of_dt:
            future_rows.append(row["feedback_id"])
            continue

        valid.append(feedback)

    for ids, counter in (
        (expired_ids, "pending_expired"),
        (out_of_range_ids, "pending_out_of_range_dropped"),
        (orphan_ids, "pending_orphan_dropped"),
    ):
        if ids:
            database.delete_pending(
                memory["id"], episode=episode, feedback_ids=ids, conn=conn
            )
            info[counter] += len(ids)

    if future_id_rows:
        info["pending_future_id"] += len(future_id_rows)

    if future_rows:
        info["pending_future_skipped"] += len(future_rows)

    valid.sort(key=lambda row: row["id"])

    return valid


def _close_episode_pending(memory, episode, conn, info):
    """关轮清理（§5.3.3.1 第 10 条）：进入 resolved/archived 时删掉旧轮残留。"""

    removed = database.delete_pending(memory["id"], episode=episode, conn=conn)

    if removed:
        info["pending_episode_closed"] += removed

    return removed


def _fix_pending_boundary(memory, conn):
    """进入 resolved/archived 时固定新轮边界；已存在则原样保留（不重置）。"""

    if memory.get("pending_episode") is not None:
        return {
            "pending_episode": memory["pending_episode"],
            "pending_start_feedback_id": memory.get("pending_start_feedback_id"),
        }

    return {
        "pending_episode": memory["episode"] + 1,
        "pending_start_feedback_id": database.max_evidence_feedback_id(memory["id"], conn=conn),
    }


def _apply_status_transition(memory, as_of, conn, info, evidence_ids, wrote_supports, wrote_contradicts):
    """一次性状态机（§5.3.3.1 第 7 步）：调用前计数与 strength 必须已刷新。

    返回 (new_status, event_type or None, extra_fields)。顺序严格：
    stale → resolved → active → weakened → 不变。
    """

    memory_type = memory["memory_type"]
    supports = info["_supports"]
    contradicts = info["_contradicts"]
    last_supported_at = info["_last_supported_at"]
    strength = info["_strength"]

    if _is_stale({"last_supported_at": last_supported_at}, as_of):
        return ("archived", EVENT_ARCHIVED, {"reason": "stale_as_of"})

    if memory["status"] == "weakened" and contradicts >= 3:
        all_later = True
        evidences = database.list_evidence(
            memory["id"], episode=memory["episode"], relation="contradicts", conn=conn
        )
        for row in evidences:
            if last_supported_at and _parse_ts(row["created_at"]) <= _parse_ts(last_supported_at):
                all_later = False
                break
        if all_later:
            return ("resolved", EVENT_RESOLVED, {"reason": "rule_triggered"})

    if memory["status"] == "weakened" and wrote_supports and strength >= 0.5:
        return ("active", EVENT_STRENGTHENED, {})

    if memory["status"] == "active" and wrote_contradicts:
        return ("weakened", EVENT_WEAKENED, {})

    return (memory["status"], None, {})


def _prepare_conclusion(memory, as_of, conn, info, value, episode=None):
    """重算计数与结论字段，暂存进 info，不落库（状态机要先看到刷新后的值）。"""

    memory_type = memory["memory_type"]
    episode = memory["episode"] if episode is None else episode
    counts = _episode_counts(memory["id"], episode, conn)
    supports = counts["supports"]
    contradicts = counts["contradicts"]
    last_supported_at = counts["last_supported_at"] or memory.get("last_supported_at") or as_of

    payload = _conclusion_payload(
        memory_type, value, memory["subject_label"], supports, contradicts, as_of, last_supported_at
    )

    info["_supports"] = supports
    info["_contradicts"] = contradicts
    info["_last_supported_at"] = last_supported_at
    info["_strength"] = payload["strength"]
    info["_fields"] = {
        "content": payload["content"],
        "value_json": payload["value_json"],
        "strength": payload["strength"],
        "evidence_count": supports,
        "contradict_count": contradicts,
        "episode": episode,
        "last_supported_at": last_supported_at,
        "updated_at": as_of,
    }

    return info["_fields"]


def _commit_conclusion(memory, conn, info, status=None, extra_fields=None):
    """把 _prepare_conclusion 暂存的字段一次性写回（单条 UPDATE）。"""

    fields = dict(info["_fields"])

    if status is not None:
        fields["status"] = status

    if extra_fields:
        fields.update(extra_fields)

    database.update_memory_state(memory["id"], fields, conn=conn)
    info["memories_updated"] += 1
    _note_memory_id(info, memory["id"])

    return database.get_memory(memory["id"], conn=conn)


def _plan_difficulties_for(rows, conn, subject_key=None):
    """给带 plan_id 的反馈补齐 plans.difficulty（time_pattern 的关系判定要用）。

    subject_key 非 None 时按 §5.2 做完整有效性校验（plan 存在、难度合法且等于
    subject_key、estimated_minutes > 0），无效行不进返回字典——调用方的
    evaluate 会把它们计成 invalid_plan，既不参与统计也不写正式证据。
    """

    cache = {}
    result = {}

    for row in rows:
        if subject_key is None and "difficulty" in row:
            result[row["id"]] = row.get("difficulty")
            continue

        plan_id = row.get("plan_id")

        if plan_id is None:
            continue

        if subject_key is not None:
            difficulty = _plan_difficulty_for_row(row, subject_key, conn, cache)
            if difficulty is not None:
                result[row["id"]] = difficulty
            continue

        if plan_id not in cache:
            plan = database.get_plan_by_id(plan_id, conn=conn)
            cache[plan_id] = None if plan is None else plan.get("difficulty")

        result[row["id"]] = cache[plan_id]

    return result


def _episode_candidates(memory, as_of, end_feedback_id, conn, info=None):
    """从原始反馈重算当前轮的有效候选（含 neutral），供更新后的结论重算。

    v16 §5.2：窗口 [as_of-30d, as_of]、id <= end_feedback_id、id > 上一轮高水位。
    轮次下界取「episode < 当前轮的最大 evidence feedback_id」，不是本轮的
    `episode_start_feedback_id`——后者是当前轮第一条正式证据，用它当下界会把
    这一条本身和 id 更小的 neutral 一起排除，第二轮起 samples 会凭空变小
    （PR #4 第三轮 P1）。`episode_start_feedback_id` 只用于展示与诊断。
    第 1 轮没有更早的轮次，因此没有下界（首轮覆盖窗口内全部候选）。
    neutral 没有正式证据行，只能从原始 feedbacks 重算。
    """

    memory_type = memory["memory_type"]
    episode = memory["episode"]
    lower_bound = None

    if episode > 1:
        previous = database.max_evidence_feedback_id_before_episode(
            memory["id"], episode, conn=conn
        )
        # 没有更早轮的证据（不该发生）时退化成无下界，避免把本轮全部候选筛空
        lower_bound = previous if previous > 0 else None

    as_of_dt = _parse_ts(as_of)
    start = (as_of_dt - timedelta(days=MEMORY_WINDOW_DAYS + 1)).isoformat()
    end = (as_of_dt + timedelta(days=1)).isoformat()

    if end_feedback_id is None:
        end_feedback_id = database.max_feedback_id(conn=conn)

    rows = database.list_feedbacks_between(start, end, max_id=end_feedback_id, conn=conn)
    selected = []

    for row in rows:
        if not _in_window(row["created_at"], as_of):
            continue
        if lower_bound is not None and row["id"] <= lower_bound:
            continue

        if memory_type == "time_pattern":
            if _plan_difficulty_for_row(row, memory["subject_key"], conn) is None:
                # plan 被删/难度非法或不匹配/预估 <= 0：这条不是本轮有效样本，
                # 诊断计数要如实记下，不能静默丢掉（PR #4 第三轮 P2-1）。
                if info is not None:
                    info["invalid_plan"] += 1
                continue
            if _ratio_for(row) is None:
                continue
        elif normalize_subject(row.get("task"))[0] != memory["subject_key"]:
            continue

        selected.append(row)

    return selected


def _episode_value(memory, as_of, conn, direction, episode=None, end_feedback_id=None, info=None):
    """用当前轮的原始候选重算 value（计数/比率全部重算，禁止 +1）。

    v16 §5.2/§5.5：time_pattern 的 samples 是有效样本总数，neutral 也算，但
    neutral 不写正式证据。所以不能拿 memory_evidence 行数当 samples——那会让
    历史 neutral 样本在第二次更新时消失（PR #4 P1-3）。这里一律回原始
    feedbacks 按轮次边界重算，rebuild 走同一函数于是天然可复现。
    """

    memory_type = memory["memory_type"]
    episode = memory["episode"] if episode is None else episode
    probe = dict(memory)
    probe["episode"] = episode

    rows = _episode_candidates(probe, as_of, end_feedback_id, conn, info)
    plan_difficulties = None

    if memory_type == "time_pattern":
        plan_difficulties = _plan_difficulties_for(rows, conn, memory["subject_key"])

    conclusion = evaluate(memory_type, rows, as_of, plan_difficulties)
    value = dict(conclusion["value"])

    if memory_type == "time_pattern":
        if value.get("difficulty") is None:
            value["difficulty"] = memory["subject_key"]
        if value.get("samples") in (0, None):
            # 当前轮的 plan 全部失效（被删/改难度/预估非法）：没有有效样本可
            # 支撑新结论，保留上一次结论而不是把内容改写成「0 次」。
            stored = decode_value(memory["value_json"])
            value["samples"] = stored.get("samples", 0)
            value["median_ratio"] = stored.get("median_ratio")

    value["direction"] = direction

    return value


def _write_candidate_evidence(memory, rows, relations, episode, conn, info, neutral_scope=None):
    """按 relations 写一行行正式证据，返回 (evidence_feedback_ids, wrote_supports, wrote_contradicts, neutral)。

    `neutral_scope` 限定「本批可以计入 neutral_dropped 的 feedback_id 集合」：neutral 不写
    `memory_evidence`（§5.2），窗口里历史 neutral 的 id 不会推进证据高水位，下一批重扫窗口
    时它们会再次出现。若每批都按候选行数累计，同一条 neutral 会被重复计数（v16 §3.5
    `neutral_dropped` 的口径是「本批被判定为 neutral 并丢弃的候选行数」，按反馈计一次）。
    `None` 表示本批全部候选都可以计（只有首次建行时成立：该记忆此前从未判定过这些行）。
    """

    evidence_ids = []
    wrote_supports = False
    wrote_contradicts = False
    neutral = 0

    for row in rows:
        relation = relations.get(row["id"])

        if relation is None:
            continue

        if relation == "neutral":
            if neutral_scope is None or row["id"] in neutral_scope:
                neutral += 1
            continue

        _, inserted = _write_evidence(memory["id"], row, episode, relation, conn)

        if inserted:
            evidence_ids.append(row["id"])
            info["evidence_written"] += 1

        if relation == "supports":
            wrote_supports = True
        else:
            wrote_contradicts = True

    return (evidence_ids, wrote_supports, wrote_contradicts, neutral)


def _finalize(memory, as_of, conn, info, value, evidence_ids, wrote_supports, wrote_contradicts, drained=None):
    """写结论 + 一次性状态机 + 事件（pending_drained 固定在前，一批最多 2 条）。"""

    from_status = memory["status"]
    _prepare_conclusion(memory, as_of, conn, info, value)
    status, event_type, extra_detail = _apply_status_transition(
        memory, as_of, conn, info, evidence_ids, wrote_supports, wrote_contradicts
    )

    extra_fields = {}

    if status != from_status and status in ("resolved", "archived"):
        extra_fields = _fix_pending_boundary(memory, conn)
        _close_episode_pending(memory, memory["episode"], conn, info)

    _commit_conclusion(memory, conn, info, status=status, extra_fields=extra_fields)

    if drained is not None:
        detail = _event_detail(
            as_of,
            memory["episode"],
            drained["evidence_ids"],
            consumed=drained["consumed"],
            neutral_dropped=drained["neutral_dropped"],
            from_status=from_status if status != from_status else None,
            to_status=status if status != from_status else None,
        )
        _write_event(memory["id"], EVENT_PENDING_DRAINED, detail, as_of, conn, info)

    if event_type is not None and status != from_status:
        detail = _event_detail(
            as_of,
            memory["episode"],
            evidence_ids,
            from_status=from_status,
            to_status=status,
            **extra_detail,
        )
        _write_event(memory["id"], event_type, detail, as_of, conn, info)

    return status


def _materialize_stale(memory, as_of, conn, info, threshold_days=None):
    """stale 是派生状态（§5.3.4/§5.3.5）：以 as_of 判定，成立即归档。

    候选是 active/weakened/resolved：v16 §5.3.5 明确 resolved --(>60 天无支撑)-->
    archived。archived 本身不在候选里，重复调用因此天然幂等（不写第二条事件）。
    固定 pending_* 只在未曾存在时才写；resolved 已有的新轮边界必须原样保留，
    只关轮清理旧轮 pending，新轮缓冲区一行都不能删（PR #4 P1-1）。
    """

    if memory is None or memory["status"] not in ("active", "weakened", "resolved"):
        return False

    if not _is_stale(memory, as_of, threshold_days):
        return False

    extra_fields = _fix_pending_boundary(memory, conn)
    _close_episode_pending(memory, memory["episode"], conn, info)
    extra_fields["status"] = "archived"
    extra_fields["updated_at"] = as_of
    database.update_memory_state(memory["id"], extra_fields, conn=conn)
    info["memories_updated"] += 1
    _note_memory_id(info, memory["id"])

    detail = _event_detail(
        as_of,
        memory["episode"],
        [],
        from_status=memory["status"],
        to_status="archived",
        reason="stale_as_of",
    )
    _write_event(memory["id"], EVENT_ARCHIVED, detail, as_of, conn, info)

    return True


def _materialize_all_stale(as_of, conn, info, threshold_days=None):
    """对全部 active/weakened 记忆跑一次 stale（批量入口与 rebuild 收尾共用）。

    先取 id 再逐条读最新行：归档会改变 status，按 offset 翻页会漏行；只拿 id
    也保证每条只按当前状态判一次（漏掉一条就等于在线与 rebuild 的结果都不完整）。
    """

    archived = 0

    for memory_id in database.list_stale_candidate_ids(conn=conn):
        memory = database.get_memory(memory_id, conn=conn)

        if memory is None:
            continue

        if _materialize_stale(memory, as_of, conn, info, threshold_days):
            archived += 1

    return archived


def _apply_current_episode(memory, trigger, as_of, memory_type, key, subject_label, conn, info):
    """当前轮路径：没有记忆时建行，已有 active/weakened 记忆时追加本轮证据。"""

    if memory is None:
        rows, plan_difficulties = _candidate_rows(memory_type, trigger, as_of, key, None, conn)
        conclusion = evaluate(memory_type, rows, as_of, plan_difficulties)

        if conclusion["invalid"]:
            info["invalid_enum"] += conclusion["invalid"]

        if conclusion["invalid_plan"]:
            info["invalid_plan"] += conclusion["invalid_plan"]

        if not conclusion["sufficient"]:
            info["insufficient_evidence"] += 1
            return None

        if conclusion["direction"] is None:
            info["direction_undetermined"] += 1
            return None

        value = dict(conclusion["value"])
        value["direction"] = conclusion["direction"]
        relations = conclusion["relations"]
        supports_rows = [row for row in rows if relations.get(row["id"]) == "supports"]
        contradicts_rows = [row for row in rows if relations.get(row["id"]) == "contradicts"]
        formal_rows = supports_rows + contradicts_rows
        last_supported_at = None

        for row in supports_rows:
            if last_supported_at is None or row["created_at"] > last_supported_at:
                last_supported_at = row["created_at"]

        payload = _conclusion_payload(
            memory_type,
            value,
            subject_label,
            len(supports_rows),
            len(contradicts_rows),
            as_of,
            last_supported_at or as_of,
        )

        result = database.insert_memory(
            {
                "memory_type": memory_type,
                "subject_key": key,
                "subject_label": subject_label,
                "content": payload["content"],
                "value_json": payload["value_json"],
                "strength": payload["strength"],
                "evidence_count": len(supports_rows),
                "contradict_count": len(contradicts_rows),
                "episode": 1,
                # §3.2：当前轮第一条「正式证据」的 feedback id；neutral 没有证据行
                # （PR #4 P2-1），所以必须从 supports/contradicts 里取最小 id。
                "episode_start_feedback_id": min(row["id"] for row in formal_rows),
                "episode_started_at": as_of,
                "status": "active",
                "last_supported_at": last_supported_at or as_of,
                "updated_at": as_of,
            },
            first_seen_at=as_of,
            conn=conn,
        )

        if not result["inserted"]:
            # 并发下别人刚建过：读回来走已有记忆路径
            existing = database.get_memory_by_key(memory_type, key, conn=conn)
            return _apply_current_episode(
                existing, trigger, as_of, memory_type, key, subject_label, conn, info
            )

        memory = database.get_memory(result["row_id"], conn=conn)
        evidence_ids, wrote_supports, wrote_contradicts, neutral = _write_candidate_evidence(
            memory, rows, relations, 1, conn, info
        )

        if neutral:
            info["neutral_dropped"] += neutral

        info["memories_created"] += 1
        _note_memory_id(info, memory["id"])

        detail = _event_detail(as_of, 1, evidence_ids)
        _write_event(memory["id"], EVENT_CREATED, detail, as_of, conn, info)

        return memory["id"]

    lower_bound = memory.get("pending_start_feedback_id")

    if lower_bound is None:
        lower_bound = database.max_evidence_feedback_id(memory["id"], conn=conn)

    rows, plan_difficulties = _candidate_rows(memory_type, trigger, as_of, key, lower_bound, conn)
    stored = decode_value(memory["value_json"])
    direction = stored.get("direction")
    relations = {}

    for row in rows:
        if database.get_evidence(memory["id"], row["id"], conn=conn):
            continue
        relations[row["id"]] = _relation_for(
            memory_type,
            direction,
            row,
            None if plan_difficulties is None else plan_difficulties.get(row["id"]),
        )

    if not relations:
        return None

    evidence_ids, wrote_supports, wrote_contradicts, neutral = _write_candidate_evidence(
        memory,
        rows,
        relations,
        memory["episode"],
        conn,
        info,
        # 已有记忆的后续批次里，本批真正新出现的候选只有触发器这一条；高水位以上的
        # 历史 neutral 在各自批次已经计过，重扫窗口时不能再计一次（P2 重复计数）。
        neutral_scope={trigger["id"]},
    )

    if neutral:
        info["neutral_dropped"] += neutral

    if not evidence_ids:
        if neutral:
            # neutral 不写正式证据，但 time_pattern 的 samples/median 与 STABLE 的
            # 计入单位都包含 neutral（§5.2/§5.5）。只来 neutral 时样本口径已经变了，
            # 必须重算结论，否则 samples 会永远停在旧值（PR #4 P1-3）；结论变了但
            # 状态没变，按 §5.3.7 不写事件。
            value = _episode_value(
                memory, as_of, conn, direction, end_feedback_id=trigger["id"], info=info
            )
            _prepare_conclusion(memory, as_of, conn, info, value)
            _commit_conclusion(memory, conn, info)
        return None

    value = _episode_value(
        memory, as_of, conn, direction, end_feedback_id=trigger["id"], info=info
    )
    _finalize(memory, as_of, conn, info, value, evidence_ids, wrote_supports, wrote_contradicts)

    return memory["id"]


def _drain_pending(memory, trigger, as_of, conn, info):
    """active/weakened 下消费遗留 pending（§5.3.3.1）。

    用当前轮已固定的 direction 判 relation，不重算方向、不重评门槛。
    """

    if memory is None or memory["status"] not in ("active", "weakened"):
        return 0

    valid = _pending_window(memory, trigger, as_of, memory["episode"], conn, info)

    if not valid:
        return 0

    stored = decode_value(memory["value_json"])
    direction = stored.get("direction")
    plan_difficulties = {}

    if memory["memory_type"] == "time_pattern":
        plan_difficulties = _plan_difficulties_for(valid, conn, memory["subject_key"])

    evidence_ids = []
    consumed_ids = []
    neutral = 0
    wrote_supports = False
    wrote_contradicts = False

    for row in valid:
        if memory["memory_type"] == "time_pattern" and row["id"] not in plan_difficulties:
            # plan 已删除/难度失效/预估非法：这条 pending 不能计入，但也不能删
            # 掉——它没被消费，等 plan 合法或 rebuild 时再处理（PR #4 P1-2）。
            # 诊断计数不在这里加：_episode_candidates() 扫原始反馈时统一记一次，
            # 这里再加会和它重复计同一行（PR #4 第三轮 P2-1）。
            continue

        relation = _relation_for(
            memory["memory_type"], direction, row, plan_difficulties.get(row["id"])
        )

        if relation is None:
            continue

        if relation == "neutral":
            neutral += 1
            consumed_ids.append(row["id"])
            continue

        _, inserted = _write_evidence(memory["id"], row, memory["episode"], relation, conn)

        if inserted:
            evidence_ids.append(row["id"])
            info["evidence_written"] += 1

        consumed_ids.append(row["id"])

        if relation == "supports":
            wrote_supports = True
        else:
            wrote_contradicts = True

    if consumed_ids:
        database.delete_pending(
            memory["id"], episode=memory["episode"], feedback_ids=consumed_ids, conn=conn
        )

    if neutral:
        info["neutral_dropped"] += neutral

    if not evidence_ids:
        if neutral:
            # 与 _apply_current_episode 一致：neutral 已从缓冲区消费，但它是
            # samples/median 的一部分（§5.2/§5.5），样本口径变了就要重算结论，
            # 只是不写事件（§5.3.7：本批没写证据就不写 pending_drained）。
            value = _episode_value(
                memory, as_of, conn, direction, end_feedback_id=trigger["id"], info=info
            )
            _prepare_conclusion(memory, as_of, conn, info, value)
            _commit_conclusion(memory, conn, info)
        return 0

    value = _episode_value(
        memory, as_of, conn, direction, end_feedback_id=trigger["id"], info=info
    )
    _finalize(
        memory,
        as_of,
        conn,
        info,
        value,
        evidence_ids,
        wrote_supports,
        wrote_contradicts,
        drained={
            "evidence_ids": evidence_ids,
            "consumed": len(evidence_ids),
            "neutral_dropped": neutral,
        },
    )

    return len(evidence_ids)


def _try_promote(memory, trigger, as_of, conn, info):
    """单事务内的原子晋升（§5.3.3 八步）：pending → 新轮正式证据。"""

    if memory is None or memory["status"] not in ("resolved", "archived"):
        return False

    episode = memory.get("pending_episode")

    if episode is None:
        episode = memory["episode"] + 1

    valid = _pending_window(memory, trigger, as_of, episode, conn, info)
    candidates = {row["id"]: row for row in valid}

    if _in_window(trigger["created_at"], as_of):
        candidates.setdefault(trigger["id"], dict(trigger))

    rows = [candidates[key] for key in sorted(candidates)]

    if not rows:
        info["insufficient_evidence"] += 1
        return False

    plan_difficulties = _plan_difficulties_for(
        rows, conn, memory["subject_key"] if memory["memory_type"] == "time_pattern" else None
    )
    conclusion = evaluate(memory["memory_type"], rows, as_of, plan_difficulties)

    if conclusion["invalid"]:
        info["invalid_enum"] += conclusion["invalid"]

    if conclusion["invalid_plan"]:
        info["invalid_plan"] += conclusion["invalid_plan"]

    if not conclusion["sufficient"]:
        info["insufficient_evidence"] += 1
        return False

    if conclusion["direction"] is None:
        info["direction_undetermined"] += 1
        return False

    value = dict(conclusion["value"])
    value["direction"] = conclusion["direction"]
    relations = conclusion["relations"]
    neutral_ids = [row["id"] for row in rows if relations.get(row["id"]) == "neutral"]
    evidence_ids, wrote_supports, wrote_contradicts, _ = _write_candidate_evidence(
        memory, rows, relations, episode, conn, info, neutral_scope=set()
    )

    if not evidence_ids:
        # 本批没有正式证据 → 不晋升、缓冲区原样保留；neutral 行下一批还会被重新判定，
        # 此刻计数就会变成同一行重复累计（P2），所以这里不计数。
        return False

    consumed = [row["id"] for row in rows if relations.get(row["id"]) is not None]
    database.delete_pending(memory["id"], episode=episode, feedback_ids=consumed, conn=conn)

    if neutral_ids:
        # neutral_dropped 的口径（v16 §3.5）是「本批被判定为 neutral 并丢弃的候选行数」：
        # 只有随缓冲区一起被删除时才真正丢弃，因此计数放在删除之后，按行计一次。
        info["neutral_dropped"] += len(neutral_ids)

    counts = _episode_counts(memory["id"], episode, conn)
    last_supported_at = counts["last_supported_at"] or as_of
    evidence_rows = database.list_evidence(memory["id"], episode=episode, conn=conn)
    episode_start_feedback_id = min(row["feedback_id"] for row in evidence_rows)

    # 晋升值与后续在线更新必须同源：都用 _episode_value 从原始反馈重算，
    # 否则下一轮更新时 samples/median 会跳变（PR #4 P1-3）。
    probe = dict(memory)
    probe["episode"] = episode
    probe["episode_start_feedback_id"] = episode_start_feedback_id
    value = _episode_value(
        probe, as_of, conn, value["direction"], episode=episode, end_feedback_id=trigger["id"]
    )

    payload = _conclusion_payload(
        memory["memory_type"],
        value,
        memory["subject_label"],
        counts["supports"],
        counts["contradicts"],
        as_of,
        last_supported_at,
    )

    database.update_memory_state(
        memory["id"],
        {
            "content": payload["content"],
            "value_json": payload["value_json"],
            "strength": payload["strength"],
            "evidence_count": counts["supports"],
            "contradict_count": counts["contradicts"],
            "episode": episode,
            "episode_start_feedback_id": episode_start_feedback_id,
            "episode_started_at": as_of,
            "pending_episode": None,
            "pending_start_feedback_id": None,
            "status": "active",
            "last_supported_at": last_supported_at,
            "updated_at": as_of,
        },
        conn=conn,
    )
    info["memories_updated"] += 1
    _note_memory_id(info, memory["id"])

    event_type = EVENT_REOPENED_FROM_ARCHIVED if memory["status"] == "archived" else EVENT_REOPENED
    detail = _event_detail(
        as_of,
        episode,
        evidence_ids,
        from_status=memory["status"],
        to_status="active",
    )
    _write_event(memory["id"], event_type, detail, as_of, conn, info)

    refreshed = database.get_memory(memory["id"], conn=conn)
    _drain_pending(refreshed, trigger, as_of, conn, info)

    return True


def apply_feedback(trigger, as_of=None, conn=None):
    """处理一条反馈的算法体（§5.3.6）——只被在线入口与 rebuild_all 调用。"""

    if not isinstance(trigger, dict) or "id" not in trigger or "created_at" not in trigger:
        raise MemoryError(ERROR_INVALID_ARGUMENT, "trigger 必须是带 id/created_at 的反馈字典")

    as_of = as_of or trigger["created_at"]
    info = _new_info()

    for memory_type in MEMORY_TYPES:
        key, subject_label = subject_key_for(memory_type, trigger, conn=conn)

        if key is None:
            continue

        memory = database.get_memory_by_key(memory_type, key, conn=conn)

        if memory is not None:
            _drain_pending(memory, trigger, as_of, conn, info)
            memory = database.get_memory_by_key(memory_type, key, conn=conn)
            _materialize_stale(memory, as_of, conn, info)
            memory = database.get_memory_by_key(memory_type, key, conn=conn)

        if memory is not None and memory["status"] in ("resolved", "archived"):
            if database.get_evidence(memory["id"], trigger["id"], conn=conn):
                info["already_evidenced"] += 1
                continue
            if _append_pending(memory, trigger, as_of, conn, info):
                _try_promote(memory, trigger, as_of, conn, info)
        else:
            _apply_current_episode(
                memory, trigger, as_of, memory_type, key, subject_label, conn, info
            )

    return {"ok": True, "info": info}


def update_from_feedback(feedback_id):
    """兼容包装：内部走在线入口与同一游标，不绕过批量循环（§5.3.6）。"""

    result = apply_pending_updates(final_feedback_id=feedback_id)

    if not result.get("ok"):
        return {
            "ok": False,
            "memory_ids": [],
            "created": 0,
            "updated": 0,
            "error_type": result.get("error_type", ERROR_DB),
        }

    info = result.get("info", {})

    return {
        "ok": True,
        "memory_ids": list(info.get("memory_ids", [])),
        "created": info.get("memories_created", 0),
        "updated": info.get("memories_updated", 0),
        "error_type": None,
    }


def apply_pending_updates(final_feedback_id=None, now=None):
    """在线唯一入口（§5.3.3.2）：BEGIN IMMEDIATE → 读游行 → 升序逐条 → 逐条推进游标。"""

    effective_now = now or _utcnow()
    info = _new_info()
    info["id_gap_skipped"] = 0
    info["id_gap_detail"] = []
    processed = 0

    try:
        with database.transaction() as conn:
            cursor = database.read_cursor(conn=conn)
            rows = database.list_feedbacks_after(cursor, final_feedback_id, conn=conn)

            if not rows:
                return {"ok": True, "processed": 0, "info": info}

            previous = cursor

            for row in rows:
                gap = row["id"] - previous - 1

                if gap > 0:
                    info["id_gap_skipped"] += gap
                    info["id_gap_detail"].append(
                        {"after": previous, "before": row["id"], "gap": gap}
                    )

                result = apply_feedback(row, as_of=row["created_at"], conn=conn)
                _merge_info(info, result["info"])
                database.write_cursor(row["id"], effective_now, conn=conn)
                previous = row["id"]
                processed += 1

            _materialize_all_stale(effective_now, conn, info)

        return {"ok": True, "processed": processed, "info": info}
    except MemoryError as error:
        return {
            "ok": False,
            "processed": 0,
            "info": info,
            "error_type": error.error_type,
            "message": str(error),
        }
    except sqlite3.Error as error:
        return {
            "ok": False,
            "processed": 0,
            "info": info,
            "error_type": ERROR_DB,
            "message": str(error),
        }


def rebuild_all(now=None):
    """派生数据全量重建（Task 4）：单事务清四表 → ORDER BY id 回放 → 收尾 stale → 抬游标。"""

    effective_now = now or _utcnow()
    info = _new_info()

    try:
        with database.transaction() as conn:
            # 条数检查必须和清表/回放同事务（PR #4 P2-3）：放在事务外的话，
            # 检查与 BEGIN IMMEDIATE 之间还能插进新反馈，5000 条上限就失效了。
            total = database.count_feedbacks(conn=conn)

            if total > REBUILD_MAX_FEEDBACKS:
                # 用异常退出事务：transaction() 会 ROLLBACK，超限路径一定不改
                # 任何派生数据或游标。error_type 由下面的 except 分支统一返回。
                raise MemoryError(
                    ERROR_REBUILD_TOO_LARGE,
                    f"feedbacks 共 {total} 条，超过上限 {REBUILD_MAX_FEEDBACKS}",
                )

            database.delete_all_memories(conn=conn)
            rows = database.list_feedbacks_after(0, None, conn=conn)

            for row in rows:
                result = apply_feedback(row, as_of=row["created_at"], conn=conn)
                _merge_info(info, result["info"])

            _materialize_all_stale(effective_now, conn, info)

            cursor = database.read_cursor(conn=conn)
            high_water = database.max_feedback_id(conn=conn) or 0
            database.write_cursor(max(cursor, high_water), effective_now, conn=conn)

            counts = database.count_derived_rows(conn=conn)

        return {
            "ok": True,
            "memories": counts["memories"],
            "evidences": counts["evidences"],
            "pendings": counts["pendings"],
            "events": counts["events"],
            "processed": len(rows),
            "cursor": max(cursor, high_water),
            "error_type": None,
        }
    except MemoryError as error:
        return {
            "ok": False,
            "memories": 0,
            "evidences": 0,
            "pendings": 0,
            "events": 0,
            "error_type": error.error_type,
            "message": str(error),
        }
    except sqlite3.Error as error:
        return {
            "ok": False,
            "memories": 0,
            "evidences": 0,
            "pendings": 0,
            "events": 0,
            "error_type": ERROR_DB,
            "message": str(error),
        }


def retrieve(memory_type=None, subject=None, status="active", limit=10):
    """结构化检索（Task 4）：只返回正式结论，不含 pending。

    limit 硬上限 10；每条回传当前轮 supports 的 feedback_id（最多 5 个，升序）。
    参数非法时**返回错误字典而不是抛异常**（稳定 error_type，不抛裸异常）。
    """

    if memory_type is not None and memory_type not in MEMORY_TYPES:
        return {
            "ok": False,
            "count": 0,
            "memories": [],
            "error_type": ERROR_INVALID_ARGUMENT,
            "message": f"未知记忆类型：{memory_type}",
        }

    if status is not None and status not in MEMORY_STATUSES:
        return {
            "ok": False,
            "count": 0,
            "memories": [],
            "error_type": ERROR_INVALID_ARGUMENT,
            "message": f"未知记忆状态：{status}",
        }

    try:
        limit = _require_number(limit, "limit", minimum=1)
    except ValueError as error:
        # §4.2：参数非法返回稳定 error_type，不向调用方抛裸 ValueError（PR #4 P2-2）
        return {
            "ok": False,
            "count": 0,
            "memories": [],
            "error_type": ERROR_INVALID_ARGUMENT,
            "message": str(error),
        }

    if limit > RETRIEVE_MAX_LIMIT:
        limit = RETRIEVE_MAX_LIMIT

    subject_key = None

    if subject is not None:
        subject_key, _label = normalize_subject(subject)

        if subject_key is None:
            return {"ok": True, "count": 0, "memories": [], "error_type": None}

    rows = database.list_memories(memory_type, subject_key, status, limit)
    result = []

    for memory in rows:
        evidence_rows = database.list_evidence(
            memory["id"], episode=memory["episode"], relation="supports"
        )
        feedback_ids = sorted(row["feedback_id"] for row in evidence_rows)
        result.append(
            {
                "memory_id": memory["id"],
                "memory_type": memory["memory_type"],
                "subject_key": memory["subject_key"],
                "subject_label": memory["subject_label"],
                "content": memory["content"],
                "strength": memory["strength"],
                "evidence_count": memory["evidence_count"],
                "contradict_count": memory["contradict_count"],
                "episode": memory["episode"],
                "status": memory["status"],
                "updated_at": memory["updated_at"],
                "evidence_feedback_ids": feedback_ids[-RETRIEVE_EVIDENCE_LIMIT:],
            }
        )

    return {"ok": True, "count": len(result), "memories": result, "error_type": None}


def archive_stale(days=STALE_DAYS, now=None):
    """批量归档入口（§5.3.4）：as_of = now()，对全部记忆跑同一个 _materialize_stale。"""

    days = _require_number(days, "days", minimum=1)
    as_of = now or _utcnow()
    info = _new_info()

    try:
        with database.transaction() as conn:
            archived = _materialize_all_stale(as_of, conn, info, threshold_days=days)
    except MemoryError as error:
        return {"ok": False, "archived": 0, "error_type": error.error_type, "info": info}
    except sqlite3.Error:
        return {"ok": False, "archived": 0, "error_type": ERROR_DB, "info": info}

    return {"ok": True, "archived": archived, "error_type": None, "info": info}


def semantic_projection(conn=None):
    """语义字段投影（§5.3.8）：只取可复现列，按业务序排序后返回。

    memory_id 统一换成 (memory_type, subject_key) 业务键；自增 id 与
    memory_pending_evidence.recorded_at 不参与比较。
    """

    memories = []
    evidence = []
    pendings = []
    events = []

    with database._db(conn) as db_conn:
        for row in db_conn.execute(
            """
            SELECT memory_type, subject_key, subject_label, content, value_json, strength,
                   evidence_count, contradict_count, episode, episode_start_feedback_id,
                   pending_episode, pending_start_feedback_id, status, first_seen_at,
                   last_supported_at, episode_started_at, updated_at
            FROM learning_memories
            """
        ).fetchall():
            memories.append(tuple(row))

        key_by_id = {}

        for row in db_conn.execute(
            "SELECT id, memory_type, subject_key FROM learning_memories"
        ).fetchall():
            key_by_id[row[0]] = (row[1], row[2])

        for row in db_conn.execute(
            """
            SELECT memory_id, feedback_id, plan_id, episode, relation, weight, created_at
            FROM memory_evidence
            """
        ).fetchall():
            evidence.append((key_by_id.get(row[0]),) + tuple(row[1:]))

        for row in db_conn.execute(
            """
            SELECT memory_id, feedback_id, plan_id, episode, created_at
            FROM memory_pending_evidence
            """
        ).fetchall():
            pendings.append((key_by_id.get(row[0]),) + tuple(row[1:]))

        for row in db_conn.execute(
            "SELECT memory_id, event_type, detail_json, created_at FROM memory_events"
        ).fetchall():
            events.append((key_by_id.get(row[0]),) + tuple(row[1:]))

        cursor_row = db_conn.execute(
            "SELECT last_applied_feedback_id FROM memory_apply_cursor WHERE id = 1"
        ).fetchone()

    return {
        "memories": sorted(memories),
        "evidence": sorted(evidence),
        "pendings": sorted(pendings),
        "events": sorted(events),
        "cursor": None if cursor_row is None else cursor_row[0],
    }


def _merge_info(target, source):
    """把单步 apply_feedback 的 info 合并进批次 info（计数相加，memory_ids 去重）。"""

    for key, value in source.items():
        if key == "memory_ids":
            for memory_id in value:
                _note_memory_id(target, memory_id)
        elif isinstance(value, int) and key in target:
            target[key] += value

    return target


def _utcnow():
    """批次内唯一时间来源：非空字符串（cursor.updated_at NOT NULL 的要求）。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()



