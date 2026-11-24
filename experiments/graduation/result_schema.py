#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生产者记录 → 冻结设计 §7.1 视图的标准化层（2026-11-23）。

## 为什么需要这一层

实验生产器 run_experiment.py 写下的记录，与冻结设计
docs/GRADUATION-EXPERIMENT-DESIGN.md v4.5 §7.1 的记录示例不是同一套字段结构：

* 预检 preflight_experiment_data.py 按 §7.1 的 29 个字段逐条要求；
* 打包器 make_blind_pack.py 按生产器字段读（SourceRow.condition = group）；
* 统计器 analyze_stats.py 把两种口径混用（condition 按 §7.1 的名字去取）。

三者对同一份 results.jsonl 因此给出互不一致的判断。本模块只做一件事：
把生产器实际写下的字段，逐项映射到 §7.1 的字段，并且用状态标明证据强度。

## 三种状态

    direct        生产器直接写了该字段；含仅改名、类型与语义都不变的别名
    derived       生产器没写，但可由其他字段无损、确定地导出；导不出时
                  必须报「不可导出」，不许填默认值
    unrecoverable 生产器从未记录，也无法从已有字段确定地恢复

状态为 unrecoverable、或派生失败时，调用方（预检 / 统计）必须 fail-closed：
把字段列成缺口并让判断停下来，不得降级成普通 WARNING 或补默认值。

## 边界

只读转换：不改生产器、不改冻结设计、不写任何文件；不做网络与数据库访问。
每个 derived 规则都对应注释里的代码位置，改动其中一侧必须同步另一侧，
并有 test_graduation_contract.py 里的契约测试兜底。
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 冻结设计 §7.1 的字段清单（顺序照抄，29 项）
# 来源：docs/GRADUATION-EXPERIMENT-DESIGN.md:1262-1302
# ---------------------------------------------------------------------------

DESIGN_FIELDS = (
    "run_id",
    "experiment_id",
    "sample_id",
    "condition",
    "group",
    "repeat_index",
    "round_index",
    "plan_seq",
    "seed",
    "scheme",
    "algorithm_version",
    "plan_sha256",
    "batch",
    "model",
    "base_sha",
    "prompt_sha256",
    "messages_path",
    "knowledge_result_ids",
    "memory_injected",
    "short_term_injected",
    "difficult_flag",
    "output_raw",
    "answer",
    "plan",
    "validation",
    "usage",
    "latency_ms",
    "error",
    "data_kind",
)

STATE_DIRECT = "direct"
STATE_DERIVED = "derived"
STATE_UNRECOVERABLE = "unrecoverable"

# §7.1 的 plan 对象字段（与 plan_service.PLAN_FIELDS 同一组 7 个字段）
DESIGN_PLAN_FIELDS = (
    "task",
    "estimated_minutes",
    "difficulty",
    "question_count",
    "reason",
    "completion_criteria",
    "subtasks",
)

DESIGN_DATA_KIND = "simulated_learner_fixture"

# write_prompt_file() 写下的固定格式头（run_experiment.py:197-220）
PROMPT_SYSTEM_HEADER = "===== SYSTEM ====="
PROMPT_USER_HEADER = "===== USER ====="

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 契约表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldContract:
    """一个 §7.1 字段的映射依据。

    name    §7.1 字段名
    state   direct / derived / unrecoverable
    source  生产器一侧的来源（字段名，或派生规则的输入）
    rule    转换规则；不可恢复时写清为什么恢复不出来
    basis   证据位置（冻结设计行号 / 生产器行号）
    """

    name: str
    state: str
    source: str
    rule: str
    basis: str


_DESIGN = "设计 §7.1（docs/GRADUATION-EXPERIMENT-DESIGN.md:1262-1302）"
_PRODUCER = "run_experiment.py:643-688"

FIELD_CONTRACT = {
    "run_id": FieldContract(
        "run_id", STATE_UNRECOVERABLE, "—",
        "生产器不记录 run_id；按 §7.1 模板的形态（如 20261120T140012Z-S1-D-r3）"
        "现编一个等于伪造一个不存在的标识",
        _PRODUCER,
    ),
    "experiment_id": FieldContract(
        "experiment_id", STATE_DIRECT, "experiment_id",
        "同名同义同类型（非空字符串）", _PRODUCER,
    ),
    "sample_id": FieldContract(
        "sample_id", STATE_DIRECT, "sample_id", "同名同义（非空字符串）", _PRODUCER,
    ),
    "condition": FieldContract(
        "condition", STATE_DERIVED, "group",
        "condition 就是 group：打包器 make_blind_pack.py:323-326 的 SourceRow.condition "
        "直接 return self.group，mapping.csv 的 condition 列也由此而来；"
        "记录若自带 condition（设计原生数据），必须与 group 相等，否则视为归属冲突",
        _DESIGN + "；make_blind_pack.py:323-326",
    ),
    "group": FieldContract(
        "group", STATE_DIRECT, "group", "同名同义（非空字符串）", _PRODUCER,
    ),
    "repeat_index": FieldContract(
        "repeat_index", STATE_DIRECT, "repeat_index", "同名同义（int）", _PRODUCER,
    ),
    "round_index": FieldContract(
        "round_index", STATE_DIRECT, "round_index",
        "同名同义（int）；v4.2 起 round_index == repeat_index，两者都在且不等时"
        "视为记录自相矛盾", _PRODUCER + "；设计 §7.1 注（:1299-1302）",
    ),
    "plan_seq": FieldContract(
        "plan_seq", STATE_DIRECT, "plan_seq", "同名同义（int，仅作顺序审计）",
        _PRODUCER,
    ),
    "seed": FieldContract(
        "seed", STATE_DIRECT, "seed", "同名同义（int）", _PRODUCER,
    ),
    "scheme": FieldContract(
        "scheme", STATE_DIRECT, "scheme", "同名同义（非空字符串）", _PRODUCER,
    ),
    "algorithm_version": FieldContract(
        "algorithm_version", STATE_DIRECT, "algorithm_version", "同名同义（非空字符串）",
        _PRODUCER,
    ),
    "plan_sha256": FieldContract(
        "plan_sha256", STATE_DIRECT, "plan_sha256",
        "同名同义（64 位小写十六进制）；legacy 记录允许为 null，由预检按"
        "「无 plan.json 只报 WARNING」的既有规则处理",
        _PRODUCER,
    ),
    "batch": FieldContract(
        "batch", STATE_DERIVED, "run 目录名 / 记录自身声明",
        "生产器记录不含 batch 字段，批次由 run 目录名按 BATCH_PREFIXES 推断"
        "（preflight_experiment_data.py:74-80）；记录一旦显式声明 batch，就必须是非空"
        "字符串，并与目录推断值一致 —— 类型非法或与目录冲突都按序报 FAIL，不当成"
        "「无从推断」的 UNKNOWN",
        _DESIGN + "；preflight_experiment_data.py:74-80",
    ),
    "model": FieldContract(
        "model", STATE_DERIVED, "provider + model + temperature + top_p",
        "§7.1 的 model 是对象 {provider, name, temperature, top_p}；生产器把同一组"
        "信息平铺成四个字段，name 取自字符串字段 model，逐一搬进对象即可，无信息损失",
        _DESIGN + "；" + _PRODUCER,
    ),
    "base_sha": FieldContract(
        "base_sha", STATE_UNRECOVERABLE, "—",
        "生产器不记录生成实验时所在工作树的 git 提交；事后再取 HEAD 得到的是"
        "「现在的」提交，不是「当时的」提交，不能当作 base_sha",
        _PRODUCER,
    ),
    "prompt_sha256": FieldContract(
        "prompt_sha256", STATE_DIRECT, "prompt_sha256", "同名同义（64 位小写十六进制）",
        _PRODUCER,
    ),
    "messages_path": FieldContract(
        "messages_path", STATE_DERIVED, "prompt_ref + prompt_sha256",
        "生产器把同一份 system/user 消息写成 run_dir/prompts/<sample>/<group>.txt，"
        "扩展名与 §7.1 示例的 .json 不同但内容一一对应；本模块解析该 .txt 重建 "
        "[system, user] 两条消息，再用 build_prompts.prompt_sha256 的同一算法"
        "（对 messages 数组取 SHA-256）核对记录里的 prompt_sha256，核对通过才算派生成立",
        _DESIGN + "；run_experiment.py:197-220 与 build_prompts.py:485-493",
    ),
    "knowledge_result_ids": FieldContract(
        "knowledge_result_ids", STATE_UNRECOVERABLE, "—",
        "生产器只把检索结果渲染进 prompt 文本，不记录条目 id",
        _PRODUCER,
    ),
    "memory_injected": FieldContract(
        "memory_injected", STATE_UNRECOVERABLE, "—",
        "生产器不记录结构化记忆注入（memory_type/subject_key/strength/status）；"
        "prompt.txt 里只有渲染后的自然语言，反推不出结构化字段",
        _PRODUCER,
    ),
    "short_term_injected": FieldContract(
        "short_term_injected", STATE_UNRECOVERABLE, "—",
        "生产器不记录短期记忆的注入开关与条目", _PRODUCER,
    ),
    "difficult_flag": FieldContract(
        "difficult_flag", STATE_UNRECOVERABLE, "—",
        "生产器不记录 difficult 布尔值；它只体现在 prompt 文本的渲染结果里，"
        "而文本渲染无法区分「确实难」与「恰好写到类似句子」", _PRODUCER,
    ),
    "output_raw": FieldContract(
        "output_raw", STATE_DIRECT, "raw_output",
        "字段改名，语义相同：都是最后一次模型起草的原始输出文本", _PRODUCER,
    ),
    "answer": FieldContract(
        "answer", STATE_DERIVED, "final_output.answer",
        "生产器把答案放进 final_output 对象；成功记录里 answer 非空"
        "（agent_plan_draft._run_draft_loop 只在校验通过后才返回 answer.strip()）",
        _PRODUCER + "；agent_plan_draft.py:679-695",
    ),
    "plan": FieldContract(
        "plan", STATE_DERIVED, "final_output.plan",
        "同 answer：final_output.plan 已过 plan_service.normalize_plan 的 7 字段白名单"
        "与 validate_plan_for_save 规则校验",
        _PRODUCER + "；agent_plan_draft.py:656-695",
    ),
    "validation": FieldContract(
        "validation", STATE_DERIVED, "success + corrections_used",
        "§7.1 的 validation 是 {json_ok, plan_valid, grounding_ok, corrections_used}；"
        "起草链只有在 JSON 解析、plan 结构、规则校验（validate_plan_for_save）、"
        "来源校验（check_grounding）全部通过后才返回 ok=True，因此成功记录蕴含"
        "前三项为 true，corrections_used 由生产器直接记录",
        _PRODUCER + "；agent_plan_draft.py:590-715",
    ),
    "usage": FieldContract(
        "usage", STATE_DERIVED, "token_usage",
        "§7.1 的 usage 是 {prompt_tokens, completion_tokens}；生产器的 token_usage "
        "在 available=True 时给出同一组计数（同名同义），不可用时计数全为 null，"
        "此时不得补 0 或估算",
        _PRODUCER + "；run_experiment.py:135-173",
    ),
    "latency_ms": FieldContract(
        "latency_ms", STATE_DERIVED, "elapsed_seconds",
        "elapsed_seconds 是 round(墙钟秒, 3)，毫秒数 = round(elapsed_seconds*1000)，"
        "得到整数毫秒，无精度损失（3 位小数即 1ms 粒度）",
        _PRODUCER + "；run_experiment.py:637",
    ),
    "error": FieldContract(
        "error", STATE_DERIVED, "success + error_type + error_message",
        "§7.1 模板只描述成功记录且要求 error 为 null；生产器 success=True 时 "
        "error_type/error_message 均为空。失败记录在 §7.1 里没有对应表示，"
        "必须作为冲突上报，不能塞进 error 字符串冒充可统计记录",
        _PRODUCER + "；" + _DESIGN,
    ),
    "data_kind": FieldContract(
        "data_kind", STATE_UNRECOVERABLE, "—",
        "生产器不记录数据来源标记；写入常量 simulated_learner_fixture 等于用"
        "「代码大概只会跑模拟数据」代替数据证据，属于编造",
        _PRODUCER + "；" + _DESIGN,
    ),
}

DIRECT_FIELDS = tuple(n for n, c in FIELD_CONTRACT.items() if c.state == STATE_DIRECT)
DERIVED_FIELDS = tuple(n for n, c in FIELD_CONTRACT.items() if c.state == STATE_DERIVED)
UNRECOVERABLE_FIELDS = tuple(
    n for n, c in FIELD_CONTRACT.items() if c.state == STATE_UNRECOVERABLE
)


def contract_rows():
    """给报告/文档用的契约表行：(字段, 状态, 生产器来源, 规则摘要)。"""

    return [
        (item.name, item.state, item.source, item.rule)
        for item in FIELD_CONTRACT.values()
    ]


def unrecoverable_reasons():
    """不可恢复字段 → 原因（用于预检报告一次列清）。"""

    return {name: FIELD_CONTRACT[name].rule for name in UNRECOVERABLE_FIELDS}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def is_design_native(record):
    """记录是否已经是 §7.1 的原生结构（29 个字段全在）。

    设计原生的记录（历史/合成/将来按 §7.1 产出的数据）走严格路径，
    不需要标准化；生产器记录走标准化路径。
    """

    return isinstance(record, dict) and all(name in record for name in DESIGN_FIELDS)


# ---------------------------------------------------------------------------
# condition：唯一带跨工具语义的派生字段
# ---------------------------------------------------------------------------

CONDITION_OK = "ok"
CONDITION_MISSING_GROUP = "missing-group"
CONDITION_CONFLICT = "conflict"


@dataclass(frozen=True)
class ConditionResult:
    code: str
    value: object = None
    detail: str = ""

    @property
    def ok(self):
        return self.code == CONDITION_OK


def derive_condition(record):
    """按真实语义导出 §7.1 的 condition —— 结论：condition 就是 group。

    依据两条，不靠字段名相似：
    1. 打包器 make_blind_pack.py:323-326 的 SourceRow.condition 直接返回
       self.group，mapping.csv 的 condition 列因此恒等于源记录的 group；
    2. 冻结设计 §7.1 注（:1299-1302）说明 condition 是旧字段、保留作显示，
       调度与去重以 group 为准。

    记录若自带 condition（设计原生数据），必须与 group 相等：两个值都是
    「这一行属于哪个实验条件」，把任一处改成另一个合法值都必须被看见，
    因此不一致时报冲突，而不是二选一。
    """

    if not isinstance(record, dict):
        return ConditionResult(CONDITION_MISSING_GROUP, None, "记录不是 JSON 对象")
    group = _text(record.get("group"))
    if group is None:
        return ConditionResult(
            CONDITION_MISSING_GROUP, None, "缺非空的 group 字段，无法确定 condition"
        )
    if "condition" in record:
        stated = _text(record.get("condition"))
        if stated is None:
            return ConditionResult(
                CONDITION_CONFLICT, None, "记录自带 condition 但不是非空字符串"
            )
        if stated != group:
            return ConditionResult(
                CONDITION_CONFLICT, None,
                "记录自带 condition=" + repr(stated) + " 与 group=" + repr(group)
                + " 不一致（condition 必须等于 group）",
            )
    return ConditionResult(CONDITION_OK, group, "")


# ---------------------------------------------------------------------------
# prompt 文本 → messages（messages_path 的派生依据）
# ---------------------------------------------------------------------------


def parse_prompt_text(text):
    """解析 write_prompt_file 写下的 .txt，返回 (system, user)；不合格式返回 None。

    格式（run_experiment.py:197-220）：

        ===== SYSTEM =====
        <system>
        <空行>
        ===== USER =====
        <user>
        <结尾换行>

    正文里若出现同样的分隔行，解析会提前截断 —— 那时重建出的 messages 哈希
    与记录里的 prompt_sha256 不等，调用方会 fail-closed，因此不会误判为通过。
    """

    if not isinstance(text, str):
        return None
    lines = text.split("\n")
    if not lines or lines[0] != PROMPT_SYSTEM_HEADER:
        return None
    if PROMPT_USER_HEADER not in lines:
        return None
    split_at = lines.index(PROMPT_USER_HEADER)
    if split_at < 2 or lines[split_at - 1] != "":
        return None
    if lines[-1] != "":
        return None
    system = "\n".join(lines[1:split_at - 1])
    user = "\n".join(lines[split_at + 1:-1])
    if not system or not user:
        return None
    return system, user


def messages_sha256(system, user):
    """与 build_prompts.prompt_sha256 逐字节一致（build_prompts.py:485-493）。"""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_prompt_messages(run_dir, prompt_ref):
    """读 run_dir/prompt_ref 并解析出 (system, user)。

    返回 (messages, reason)：messages 为 None 时 reason 说明原因。
    只接受 run_dir 内的相对路径，拒绝绝对路径与越界路径段。
    """

    ref = _text(prompt_ref)
    if ref is None:
        return None, "prompt_ref 不是非空字符串"
    cleaned = ref.replace(chr(92), "/")
    if cleaned.startswith("/") or ":" in cleaned.split("/")[0]:
        return None, "prompt_ref=" + repr(ref) + " 是绝对路径，拒绝读取"
    parts = [part for part in cleaned.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        return None, "prompt_ref=" + repr(ref) + " 含越界路径段"
    target = run_dir.joinpath(*parts)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        return None, "读不到 prompt 文件 " + ref + "：" + str(error)
    parsed = parse_prompt_text(text)
    if parsed is None:
        return None, "prompt 文件 " + ref + " 不符合生产器的固定格式"
    return parsed, ""


# ---------------------------------------------------------------------------
# batch：目录推断值 + 记录自身声明（缺失 / 冲突 / 非法 / 可用 四态分开）
# ---------------------------------------------------------------------------

BATCH_OK = "ok"
BATCH_FROM_DIRECTORY = "directory"
BATCH_MISSING = "missing"
BATCH_INVALID = "invalid"
BATCH_CONFLICT = "conflict"


@dataclass(frozen=True)
class BatchResult:
    """一条记录的批次派生结果。

    code    ok / directory / missing / invalid / conflict
    value   可用批次名；不可用时为 None（不得用目录值掩盖记录的冲突声明）
    detail  不可用时说明原因
    declared 记录里原本声明的原样值（缺失时为 None）
    """

    code: str
    value: object = None
    detail: str = ""
    declared: object = None

    @property
    def ok(self):
        return self.code in (BATCH_OK, BATCH_FROM_DIRECTORY)


def is_valid_batch_value(value):
    """§7.1 的 batch 只能是批次名字符串：list/dict/int/bool/空串都算非法值。"""

    return isinstance(value, str) and bool(value.strip())


def derive_batch(record, directory_batch=None):
    """批次：记录自身声明与目录推断值一起判，四种情形不混为一谈。

    1. 记录没给 batch：有目录推断值就采用它（directory）；都没有就是 missing ——
       生产器当前不记录 batch，所以「没给」不是契约缺口，由调用方报 UNKNOWN；
    2. 记录给了但不是非空字符串：invalid（list/dict/int/bool/空串一律拒绝）；
    3. 记录给了合法值但与目录推断值不同：conflict（两个值都合法，必须被看见，
       不能用目录值把它覆盖掉）；
    4. 记录给了合法值且与目录一致（或目录不可判定时）：ok。
    """

    base = record if isinstance(record, dict) else {}
    has_declared = "batch" in base
    declared = base.get("batch") if has_declared else None
    if not has_declared or declared is None:
        if directory_batch is not None:
            return BatchResult(BATCH_FROM_DIRECTORY, directory_batch,
                               "记录未声明 batch，采用目录名推断值", None)
        return BatchResult(BATCH_MISSING, None,
                           "记录未声明 batch，目录名也无法判定批次", None)
    if not is_valid_batch_value(declared):
        return BatchResult(
            BATCH_INVALID, None,
            "batch=" + repr(declared) + " 不是非空字符串（批次数值/列表/对象都不接受）",
            declared,
        )
    if directory_batch is not None and declared != directory_batch:
        return BatchResult(
            BATCH_CONFLICT, None,
            "记录声明 batch=" + repr(declared) + " 与目录推断 "
            + repr(directory_batch) + " 冲突",
            declared,
        )
    return BatchResult(BATCH_OK, declared, "", declared)


# ---------------------------------------------------------------------------

# 标准化
# ---------------------------------------------------------------------------


@dataclass
class FieldResult:
    """单个 §7.1 字段的标准化结果。"""

    name: str
    state: str
    ok: bool
    value: object = None
    reason: str = ""
    basis: str = ""


@dataclass
class Normalized:
    """一份生产器记录的标准化结果（只读视图 + 逐字段证据）。"""

    record: dict
    results: dict = field(default_factory=dict)

    # --- 视图 ---
    def view(self):
        """只含取到值的设计字段（缺失/冲突字段不出现）。"""

        return {
            name: self.results[name].value
            for name in DESIGN_FIELDS
            if name in self.results
            and self.results[name].ok
            and self.results[name].value is not None
        }

    def value(self, name, default=None):
        item = self.results.get(name)
        if item is None or not item.ok:
            return default
        return item.value

    # --- 证据 ---
    def unrecoverable(self):
        """生产器从未记录、要求调用方 fail-closed 的字段（设计顺序）。"""

        return [
            name for name in DESIGN_FIELDS
            if self.results.get(name) is not None
            and self.results[name].state == STATE_UNRECOVERABLE
        ]

    def failures(self):
        """状态是 direct/derived、但这一行没能取到值的字段 → 原因。"""

        out = {}
        for name in DESIGN_FIELDS:
            item = self.results.get(name)
            if item is not None and not item.ok and item.state != STATE_UNRECOVERABLE:
                out[name] = item.reason
        return out

    def null_valued(self):
        """取到值、且设计值本身就是 null 的字段（成功记录的 error；legacy 的 plan_sha256）。

        §7.1 的成功记录要求 error 为 null，view() 按「value 是 None 就不出现」处理，
        所以这类字段不能算缺口 —— 否则每条成功记录都会多报一条假缺口。
        """

        return [
            name for name in DESIGN_FIELDS
            if self.results.get(name) is not None
            and self.results[name].ok
            and self.results[name].value is None
        ]

    def missing(self):
        """§7.1 要求有值、但这一行没取到的字段（不可恢复 + 派生失败），设计顺序。

        恒等于 unrecoverable() ∪ failures()：设计值本来就是 null 的字段
        （见 null_valued()）不计入缺口。
        """

        failures = self.failures()
        return [
            name for name in DESIGN_FIELDS
            if name in failures
            or (self.results.get(name) is not None
                and self.results[name].state == STATE_UNRECOVERABLE)
        ]

    def is_complete(self):
        return not self.unrecoverable() and not self.failures()


def _result(name, ok, value=None, reason="", state=None):
    contract = FIELD_CONTRACT[name]
    return FieldResult(name, state or contract.state, ok, value, reason, contract.basis)


def _derive_model(record):
    provider = _text(record.get("provider"))
    name = _text(record.get("model"))
    temperature = record.get("temperature")
    top_p = record.get("top_p")
    if provider is None:
        return None, "缺非空的 provider，无法还原 model.provider"
    if name is None:
        return None, "缺非空的 model 字符串，无法还原 model.name"
    if not _is_number(temperature):
        return None, "temperature=" + repr(temperature) + " 不是有限数值"
    if not _is_number(top_p):
        return None, "top_p=" + repr(top_p) + " 不是有限数值"
    return {
        "provider": provider,
        "name": name,
        "temperature": temperature,
        "top_p": top_p,
    }, ""


def _derive_validation(record):
    success = record.get("success")
    corrections = record.get("corrections_used")
    if success is not True:
        return None, (
            "该记录不是成功记录（success=" + repr(success) + "）：§7.1 的 validation "
            "只描述成功记录，失败记录没有对应表示"
        )
    if not _is_int(corrections) or corrections < 0:
        return None, "corrections_used=" + repr(corrections) + " 不是非负整数"
    return {
        "json_ok": True,
        "plan_valid": True,
        "grounding_ok": True,
        "corrections_used": corrections,
    }, ""


def _derive_usage(record):
    usage = record.get("token_usage")
    if not isinstance(usage, dict):
        return None, "缺 token_usage 对象"
    if usage.get("available") is not True:
        return None, (
            "token_usage.available 不为 true（存在没有计量数据的请求）："
            "§7.1 的 usage 要求两个整数，此处不得补 0 或估算"
        )
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if not _is_int(prompt_tokens) or not _is_int(completion_tokens):
        return None, (
            "token_usage 计数不是 int（prompt=" + repr(prompt_tokens)
            + "、completion=" + repr(completion_tokens) + "）"
        )
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}, ""


def _derive_latency(record):
    elapsed = record.get("elapsed_seconds")
    if not _is_number(elapsed) or elapsed < 0:
        return None, "elapsed_seconds=" + repr(elapsed) + " 不是非负有限数值"
    return int(round(elapsed * 1000)), ""


def _derive_error(record):
    if record.get("success") is not True:
        return None, "失败记录：§7.1 要求 error 为 null，失败结果没有对应表示"
    if record.get("error_type") or record.get("error_message"):
        return None, (
            "success=true 但同时带 error_type=" + repr(record.get("error_type"))
            + "、error_message=" + repr(record.get("error_message"))
            + "，记录自相矛盾"
        )
    return None, ""


def _derive_answer(record):
    final = record.get("final_output")
    if not isinstance(final, dict):
        return None, "缺 final_output 对象（失败记录）"
    answer = _text(final.get("answer"))
    if answer is None:
        return None, "final_output.answer 不是非空字符串"
    return answer, ""


def _derive_plan(record):
    final = record.get("final_output")
    if not isinstance(final, dict):
        return None, "缺 final_output 对象（失败记录）"
    plan = final.get("plan")
    if not isinstance(plan, dict):
        return None, "final_output.plan 不是 JSON 对象"
    missing = [name for name in DESIGN_PLAN_FIELDS if name not in plan]
    if missing:
        return None, "final_output.plan 缺字段：" + repr(missing)
    unknown = sorted(set(plan) - set(DESIGN_PLAN_FIELDS))
    if unknown:
        return None, "final_output.plan 含白名单外字段：" + repr(unknown)
    return {name: plan[name] for name in DESIGN_PLAN_FIELDS}, ""


def normalize_record(record, batch=None, run_dir=None):
    """把一条生产器记录标准化成 §7.1 视图，并逐字段记录证据。

    batch    由调用方按 run 目录名推断；推断不出时传 None → batch 不进视图
    run_dir  给出时校验 messages_path 指向的 prompt 文件并核对 prompt_sha256
    """

    normalized = Normalized(record if isinstance(record, dict) else {})
    row = normalized.record

    # --- direct：同名同义 ---
    for name in ("experiment_id", "sample_id", "group", "scheme",
                 "algorithm_version"):
        value = _text(row.get(name))
        normalized.results[name] = _result(
            name, value is not None, value,
            "" if value is not None else name + " 不是非空字符串",
        )

    for name in ("repeat_index", "plan_seq", "seed"):
        value = row.get(name)
        ok = _is_int(value)
        normalized.results[name] = _result(
            name, ok, value if ok else None,
            "" if ok else name + "=" + repr(value) + " 不是 int（bool 不算）",
        )

    round_index = row.get("round_index")
    round_ok = _is_int(round_index)
    round_reason = "" if round_ok else "round_index=" + repr(round_index) + " 不是 int"
    repeat_index = row.get("repeat_index")
    if round_ok and _is_int(repeat_index) and round_index != repeat_index:
        round_ok = False
        round_reason = (
            "round_index=" + repr(round_index) + " 与 repeat_index="
            + repr(repeat_index) + " 不一致（v4.2 起两者必须相等）"
        )
    normalized.results["round_index"] = _result(
        "round_index", round_ok, round_index if round_ok else None, round_reason
    )

    plan_sha = row.get("plan_sha256")
    if plan_sha is None:
        normalized.results["plan_sha256"] = _result(
            "plan_sha256", True, None,
            "legacy 记录允许缺 plan_sha256（预检按既有 WARNING 规则处理）",
        )
    else:
        ok = isinstance(plan_sha, str) and bool(
            _SHA256_RE.match(plan_sha.strip().lower())
        )
        normalized.results["plan_sha256"] = _result(
            "plan_sha256", ok, plan_sha.strip().lower() if ok else None,
            "" if ok else "plan_sha256=" + repr(plan_sha) + " 不是 64 位十六进制",
        )

    prompt_sha = row.get("prompt_sha256")
    if isinstance(prompt_sha, str) and _SHA256_RE.match(prompt_sha.strip().lower()):
        normalized.results["prompt_sha256"] = _result(
            "prompt_sha256", True, prompt_sha.strip().lower()
        )
    else:
        normalized.results["prompt_sha256"] = _result(
            "prompt_sha256", False, None,
            "prompt_sha256=" + repr(prompt_sha) + " 不是 64 位十六进制",
        )

    raw_output = row.get("raw_output")
    normalized.results["output_raw"] = _result(
        "output_raw", isinstance(raw_output, str), raw_output,
        "" if isinstance(raw_output, str) else "raw_output 不是字符串",
    )

    # --- condition：见 derive_condition 的语义依据 ---
    condition = derive_condition(row)
    normalized.results["condition"] = _result(
        "condition", condition.ok, condition.value,
        "" if condition.ok else condition.detail,
    )

    # --- batch：目录推断值 + 记录自身声明（类型与一致性都要核）---
    # 只信目录会漏掉「记录显式声明了另一个批次」，所以这里不直接写目录值，
    # 而是走 derive_batch：记录声明非法或与目录冲突都记 ok=False（调用方据此 FAIL）。
    batch_result = derive_batch(row, directory_batch=batch)
    normalized.results["batch"] = _result(
        "batch", batch_result.ok,
        batch_result.value if batch_result.ok else None,
        "" if batch_result.ok else batch_result.detail,
    )

    # --- 其余派生字段 ---
    for name, handler in (
        ("model", _derive_model),
        ("validation", _derive_validation),
        ("usage", _derive_usage),
        ("latency_ms", _derive_latency),
        ("answer", _derive_answer),
        ("plan", _derive_plan),
    ):
        value, reason = handler(row)
        normalized.results[name] = _result(name, value is not None, value, reason)

    # error：成功记录必须是 null；失败记录无法表达
    error_value, error_reason = _derive_error(row)
    if row.get("success") is True and not error_reason:
        normalized.results["error"] = _result("error", True, None)
    elif row.get("success") is True:
        normalized.results["error"] = _result("error", False, None, error_reason)
    else:
        normalized.results["error"] = _result(
            "error", False, None,
            "失败记录：§7.1 要求 error 为 null，失败结果没有对应表示",
        )

    # messages_path：prompt_ref 指向的文件 + 重建 messages 后核对哈希
    prompt_ref = _text(row.get("prompt_ref"))
    if prompt_ref is None:
        normalized.results["messages_path"] = _result(
            "messages_path", False, None, "缺非空的 prompt_ref"
        )
    elif run_dir is None:
        normalized.results["messages_path"] = _result(
            "messages_path", False, None,
            "未提供 run_dir，无法核对 prompt 文件与 prompt_sha256",
        )
    else:
        messages, reason = read_prompt_messages(run_dir, prompt_ref)
        if messages is None:
            normalized.results["messages_path"] = _result(
                "messages_path", False, None, reason
            )
        else:
            rebuilt = messages_sha256(*messages)
            recorded = normalized.value("prompt_sha256")
            if recorded is None:
                normalized.results["messages_path"] = _result(
                    "messages_path", False, None,
                    "记录缺可核对的 prompt_sha256，无法证明 prompt 文件与记录同源",
                )
            elif rebuilt != recorded:
                normalized.results["messages_path"] = _result(
                    "messages_path", False, None,
                    "由 " + prompt_ref + " 重建的 messages 哈希 " + rebuilt
                    + " 与记录的 prompt_sha256 " + recorded + " 不一致",
                )
            else:
                normalized.results["messages_path"] = _result(
                    "messages_path", True, prompt_ref
                )

    # --- 不可恢复：如实记录，绝不代填 ---
    for name in UNRECOVERABLE_FIELDS:
        normalized.results[name] = _result(
            name, False, None, FIELD_CONTRACT[name].rule, STATE_UNRECOVERABLE
        )

    return normalized
