"""学习记忆服务：主题归一化与内容渲染（第二阶段 Task 2）。

本阶段只实现两个纯函数：
- normalize_subject(task)：把任务名映射成稳定的 subject_key / subject_label
- render_content(...)：由结构化取值渲染记忆文本

为什么把渲染放在程序里而不是让大模型写：记忆内容一旦由模型生成，就无法
从 value_json 复算、也无法作为论文里的可复核结论。模板只吃 value_json 里的
数值，同一份证据永远渲染出同一句话。
"""

import json
import unicodedata

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


def _clean_text(value):
    """折叠空白 + NFKC 规范化；非字符串一律当空。"""

    if value is None:
        return ""

    text = unicodedata.normalize("NFKC", str(value))
    return " ".join(text.split())


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

    key = key.lower()[:SUBJECT_KEY_MAX_CHARS].strip()
    label = label[:SUBJECT_LABEL_MAX_CHARS]

    if not key:
        return None, None

    return key, label


def _require_number(value, name, integer=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值，收到 {type(value).__name__}")

    if integer and not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数，收到 {type(value).__name__}")

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


def render_content(memory_type, value, subject_label, supports=0):
    """由结构化取值渲染记忆文本（纯函数，不含任何模型调用）。

    value 必须是 dict（value_json 的原样解析结果）；字段缺失或类型不符直接
    抛 ValueError，避免把脏数据渲染成一句看似正常的结论。
    supports 是当前轮已计入的支持条数，仅用于决定是否加「初步观察：」前缀。
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

    if supports < STABLE[memory_type]:
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
