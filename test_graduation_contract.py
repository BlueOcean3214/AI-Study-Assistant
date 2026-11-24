"""毕业设计实验：生产者—消费者字段契约测试（PR #26 第二轮复审要求）。

与既有两套测试的分工：

- test_graduation_preflight.py 用「设计原生行」夹具（29 个 §7.1 字段齐全），验证的是
  预检工具本身的判定逻辑；
- 本文件反过来：用**真实的生成端** run_experiment.run_one() 产出记录，经**真实的**
  append_jsonl() 落盘，再分别交给预检、打包器、统计器三个消费者，验证的是
  「执行器实际产生的数据能不能按冻结设计被核验和统计」，而不是「夹具符合 schema」。

所以这里不手写 §7.1 行：每一行的来源都是 run_one()，fixture 只替换 api 与 draft_runner
（模型调用），run_one() 的其余路径（写 Prompt 文件、算 prompt_sha256、汇总 token_usage、
拼记录）全部真实执行。

约束（与复审要求一致）：全程离线，不访问真实 feedback.db、不读真实 runs/、不生成盲评包、
不碰 blind_private/、不改冻结文档。所有文件都落在 tempfile 临时目录里。
"""

import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
GRADUATION_DIR = HERE / "experiments" / "graduation"

for _path in (str(HERE), str(GRADUATION_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import analyze_stats  # noqa: E402
import build_prompts  # noqa: E402
import make_blind_pack  # noqa: E402
import plan_schedule  # noqa: E402
import preflight_experiment_data  # noqa: E402
import result_schema  # noqa: E402
import run_experiment  # noqa: E402

PASSED = []
FAILED = []
INFO = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}")
        if detail:
            print(f"      {detail}")
    return bool(condition)


def info(name, detail=""):
    INFO.append(f"{name}：{detail}" if detail else name)
    print(f"INFO  {name}" + (f"：{detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 生产器夹具：只打桩 api 与 draft_runner，其余代码路径真实执行
# ---------------------------------------------------------------------------

EXPERIMENT_ID = "exp-contract"
SAMPLE_ID = "S2"
GROUP = "D"
SEED = 20261113
SCHEME = "five_round_randomized_block_v1"
ALGORITHM_VERSION = "sha256-block-order-v1"
PLAN_SHA = "ab" * 32  # 64 位十六进制：调度器从 plan.json 传入的值
DRAFT_SYSTEM_PROMPT = "SYSTEM：契约测试用系统提示词（不代表真实实验内容）"

ANSWER = "先复习条件概率，再做 3 道练习。"
PLAN = {
    "task": "两周复习概率论前三章",
    "estimated_minutes": 120,
    "difficulty": "medium",
    "question_count": 3,
    "reason": "反馈显示条件概率薄弱",
    "completion_criteria": ["能独立完成一页练习"],
    "subtasks": [{"title": "复习条件概率", "minutes": 40}],
}
RAW_OUTPUT = json.dumps({"answer": ANSWER, "plan": PLAN}, ensure_ascii=False, sort_keys=True)

# §7.1 里生产器从未写出的字段：契约测试用来反证「不是靠字段名相似就复制」
DESIGN_ONLY_FIELDS = (
    "run_id", "condition", "batch", "base_sha", "messages_path",
    "knowledge_result_ids", "memory_injected", "short_term_injected",
    "difficult_flag", "output_raw", "answer", "plan", "validation",
    "usage", "latency_ms", "error", "data_kind",
)


class _StubPlanDraft:
    """api["agent_plan_draft"] 的最小替身：只提供 run_one 真正用到的两个成员。

    刻意与产品函数同签名、同位置参数，这样 build_prompts.build_group_content() 的调用
    形式也必须是真的；替身内容只要求「组间可区分 + 能原样写进 Prompt 文件」。
    """

    DRAFT_SYSTEM_PROMPT = DRAFT_SYSTEM_PROMPT
    # 由 _produce 注入：起草期间模拟请求用量（run_one 只认起草期间新增的 recorder 记录）
    recorder = None

    def _build_draft_user_content(self, user_message, feedback_result, knowledge_results,
                                  search_failed, difficult, short_term_block, memory_result):
        return (
            "USER_MESSAGE=" + str(user_message) + "\n"
            "FEEDBACK_COUNT=" + str(len((feedback_result or {}).get("feedback") or [])) + "\n"
            "DIFFICULT=" + repr(difficult) + "\n"
            "SHORT_TERM=" + repr(short_term_block) + "\n"
            "MEMORY_COUNT=" + str(len((memory_result or {}).get("memories") or [])) + "\n"
            "KNOWLEDGE_COUNT=" + str(len(knowledge_results or [])) + "\n"
            "SEARCH_FAILED=" + repr(search_failed) + "\n"
        )


def _make_api(root):
    """数据库隔离替身：DATABASE_PATH 指向临时目录（≠ 真实 feedback.db）。"""

    return {
        "database": SimpleNamespace(DATABASE_PATH=str(Path(root) / "_contract_isolation.db")),
        "agent_plan_draft": _StubPlanDraft(),
    }


def _make_inputs():
    """run_one 需要的槽位输入：与 build_prompts.group_inputs / evidence_steps 的键一致。"""

    return {
        "user_message": "我想在两周内复习完概率论前三章",
        "feedback_result": {
            "days": 7,
            "count": 2,
            "summary": {"total_count": 2},
            "feedback": [{"id": 1}, {"id": 2}],
        },
        "raw_feedback_result": {
            "days": 7,
            "count": 2,
            "summary": {},
            "feedback": [{"id": 1}, {"id": 2}],
        },
        "empty_feedback": {"days": 7, "count": 0, "summary": {}, "feedback": []},
        "difficult": True,
        "short_term_block": "近期情况",
        "memory_result": {"count": 1, "memories": [{"content": "m"}]},
        "memory_tool": {"ok": True, "count": 1, "memories": [{"content": "m"}]},
        "subject_memory_result": {"count": 2, "memories": [{"content": "s"}]},
        "subject_memory_tool": {"ok": True, "count": 2, "memories": [{"content": "s"}]},
        "subject_filter": "概率论",
        "memory_limit": 5,
        "knowledge_results": [],
        "search_failed": False,
    }


def _fake_runner(*, ok=True, raw=RAW_OUTPUT, corrections=1, error_type="llm_error",
                 message="mock 失败：模型返回的内容不是 JSON"):
    def runner(api=None, raw_sink=None, **kwargs):
        # 起草期间模拟一次真实模型调用：真实 llm 客户端也是在这时写 recorder 的。
        # （run_one 用 recorder.since(before) 只把起草期间新增的请求算进本行，
        #   所以在 run_one 之前预先塞进去的用量不会进入记录。）
        recorder = getattr((api or {}).get("agent_plan_draft"), "recorder", None)
        if recorder is not None:
            _seed_usage(recorder)
        # run_one 以全 kwargs 调用；这里只关心成败与 raw_sink（记录里 raw_output 的来源）
        if raw_sink is not None and raw is not None:
            raw_sink.append(raw)
        if not ok:
            return {
                "ok": False,
                "error_type": error_type,
                "message": message,
                "corrections_used": corrections,
            }
        return {
            "ok": True,
            "answer": ANSWER,
            "plan": PLAN,
            "corrections_used": corrections,
            "stopped_reason": "plan_draft_ready",
        }

    return runner


def _seed_usage(recorder, *, prompt_tokens=321, completion_tokens=123):
    """模拟一次真实请求的用量记录：让 token_usage 变成 available。"""

    recorder({
        "provider": "deepseek",
        "model": "deepseek-chat",
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 1024,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "usage_available": True,
        "elapsed_seconds": 0.5,
        "ok": True,
        "error_type": None,
    })


def _produce(run_dir, api, *, group=GROUP, repeat_index=0, ok=True, usage=True,
             experiment_id=EXPERIMENT_ID, sample_id=SAMPLE_ID,
             plan_sha256=PLAN_SHA, plan_seq=None, round_index=None):
    """真实跑一次 run_one() 并真实 append_jsonl() 落盘，返回 (record, 源行号)。

    `plan_seq`/`round_index` 默认等于 `repeat_index`（单格 5 轮夹具的旧行为）；
    全主实验 140 任务夹具需要按计划里的全局序号单独传入，否则同一样本不同组
    会拿到相同的 plan_seq。
    """

    recorder = run_experiment.UsageRecorder()
    if usage:
        api["agent_plan_draft"].recorder = recorder

    record = run_experiment.run_one(
        experiment_id=experiment_id,
        sample_id=sample_id,
        group=group,
        repeat_index=repeat_index,
        inputs=_make_inputs(),
        api=api,
        run_dir=run_dir,
        recorder=recorder,
        sampling={"temperature": 0.7, "top_p": 0.9, "max_tokens": 1024},
        plan_seq=repeat_index if plan_seq is None else plan_seq,
        round_index=repeat_index if round_index is None else round_index,
        seed=SEED,
        scheme=SCHEME,
        algorithm_version=ALGORITHM_VERSION,
        plan_sha256=plan_sha256,
        draft_runner=_fake_runner(ok=ok),
    )

    path = Path(run_dir) / run_experiment.RESULTS_NAME
    run_experiment.append_jsonl(path, record)

    return record, path


def _rows_of(path):
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _rewrite(path, row):
    Path(path).write_text(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8", newline="\n")
    return row


def _mapping_meta(results_path, row, *, line=1, condition=None, group=None,
                  blind_id="B0001", phase="formal", file_digest=None, row_digest=None):
    """按 mapping.csv 的语义造一行 meta：文件哈希与行哈希默认取真实值。

    负向用例允许替换文件/行哈希，用来证明「只核哈希」不够、身份比对真的在工作。
    """

    path = Path(results_path)
    data = path.read_bytes()
    meta = {
        "blind_id": blind_id,
        "phase": phase,
        "source_run_dir": path.parent.name,
        "source_file": path.name,
        "source_row_number": str(line),
        "experiment_id": row["experiment_id"],
        "sample_id": row["sample_id"],
        "group": row.get("group") if group is None else group,
        "condition": row.get("group") if condition is None else condition,
        "repeat_index": str(row["repeat_index"]),
        "round_index": "" if row.get("round_index") is None else str(row["round_index"]),
        "source_prompt_sha256": row["prompt_sha256"],
        "source_result_row_sha256": row_digest or analyze_stats.row_sha256(row),
        "source_results_file_sha256": file_digest or hashlib.sha256(data).hexdigest(),
    }
    return meta


def _stats_problems(results_path, mapping):
    """调 load_results；成功返回空特征，失败返回 (code, problems, detail)。"""

    try:
        analyze_stats.load_results([results_path], mapping)
    except analyze_stats.StatsError as error:
        return error.code, list(error.problems), error.detail
    return "", [], ""


def _preflight(results_path, *, batch="main", plan=None):
    report = preflight_experiment_data.Report()
    preflight_experiment_data.check_results(report, Path(results_path), plan,
                                            expected_batch=batch)
    return report


def _check(results_path, *, batch="main", plan=None):
    """同 _preflight，但返回 (报告, 身份列表)：身份列表证明行没被整体跳过。"""
    report = preflight_experiment_data.Report()
    identities = preflight_experiment_data.check_results(
        report, Path(results_path), plan, expected_batch=batch)
    return report, identities


def _fails(report):
    return [item for item in report.findings if item.level == "FAIL"]


def _fail_targets(report):
    return "\n".join(item.target for item in _fails(report))


# ---------------------------------------------------------------------------
# 1. 契约表本身
# ---------------------------------------------------------------------------


def test_contract_table():
    rows = result_schema.contract_rows()

    check("契约表覆盖 29 个 §7.1 字段", len(rows) == 29, len(rows))
    check("契约表字段顺序与设计 §7.1 一致",
          [name for name, _state, _source, _rule in rows]
          == list(result_schema.DESIGN_FIELDS))
    check("直供 12 个", len(result_schema.DIRECT_FIELDS) == 12,
          sorted(result_schema.DIRECT_FIELDS))
    check("可派生 10 个", len(result_schema.DERIVED_FIELDS) == 10,
          sorted(result_schema.DERIVED_FIELDS))
    check("不可恢复 7 个", len(result_schema.UNRECOVERABLE_FIELDS) == 7,
          sorted(result_schema.UNRECOVERABLE_FIELDS))

    states = set()
    for name, state, source, rule in rows:
        item = result_schema.FIELD_CONTRACT[name]
        states.add(state)
        check(f"契约字段 {name} 有可追溯依据", bool(item.basis.strip()), item.basis[:120])
        check(f"契约字段 {name} 写明生产器来源", bool(source.strip()), source[:120])
        check(f"契约字段 {name} 写明转换规则", bool(rule.strip()), rule[:120])

    check("状态取值只有三种", states == {result_schema.STATE_DIRECT,
                                          result_schema.STATE_DERIVED,
                                          result_schema.STATE_UNRECOVERABLE}, states)
    check("三类互不吃重、合起来正好覆盖全部字段",
          sorted(list(result_schema.DIRECT_FIELDS) + list(result_schema.DERIVED_FIELDS)
                 + list(result_schema.UNRECOVERABLE_FIELDS))
          == sorted(result_schema.DESIGN_FIELDS))


# ---------------------------------------------------------------------------
# 2. 生产器记录到底写了什么
# ---------------------------------------------------------------------------


def test_producer_record_shape(run_dir, api):
    record, path = _produce(run_dir, api)

    for name in DESIGN_ONLY_FIELDS:
        check(f"生产器记录不含 §7.1 专属字段 {name}", name not in record, sorted(record))

    for name in ("final_output", "token_usage", "raw_output", "prompt_ref",
                 "elapsed_seconds", "success", "corrections_used"):
        check(f"生产器记录含真实字段 {name}", name in record)

    check("生产器记录不是设计原生行",
          result_schema.is_design_native(record) is False)
    check("落盘后仍是同一份记录",
          _rows_of(path)[0] == json.loads(json.dumps(record, ensure_ascii=False)))

    return record, path


# ---------------------------------------------------------------------------
# 3. 标准化：可派生字段逐个核对
# ---------------------------------------------------------------------------


def test_normalize_success(record, path):
    normalized = result_schema.normalize_record(
        record, batch="main", run_dir=Path(path).parent)
    view = normalized.view()

    # 29 − 7 个不可恢复 − 1 个设计值就是 null 的 error（view() 约定：null 不出现）
    check("视图字段数 = 29 − 7 不可恢复 − 1（error 设计上为 null）",
          len(view) == 21, sorted(view))
    check("派生没有失败", normalized.failures() == {}, normalized.failures())

    check("condition 由 group 派生", view.get("condition") == GROUP, view.get("condition"))
    check("batch 由目录推断传入", view.get("batch") == "main", view.get("batch"))
    check("output_raw 照抄 raw_output", view.get("output_raw") == RAW_OUTPUT)
    check("answer 由 final_output.answer 派生", view.get("answer") == ANSWER)
    check("plan 由 final_output.plan 派生", view.get("plan") == PLAN)

    model = view.get("model") or {}
    check("model 对象由 provider/model/temperature/top_p 派生",
          model == {"provider": "deepseek", "name": "deepseek-chat",
                    "temperature": 0.7, "top_p": 0.9}, model)

    validation = view.get("validation") or {}
    check("validation 由 success + corrections_used 派生",
          validation == {"json_ok": True, "plan_valid": True,
                         "grounding_ok": True, "corrections_used": 1}, validation)

    check("usage 由 token_usage 派生（不补 0、不估算）",
          view.get("usage") == {"prompt_tokens": 321, "completion_tokens": 123},
          view.get("usage"))
    check("latency_ms 由 elapsed_seconds 换算且无浮点漂移",
          view.get("latency_ms") == int(round(record["elapsed_seconds"] * 1000)),
          (record["elapsed_seconds"], view.get("latency_ms")))
    check("误差不超过 1ms 的整数",
          isinstance(view.get("latency_ms"), int)
          and abs(view["latency_ms"] - record["elapsed_seconds"] * 1000) < 1,
          view.get("latency_ms"))
    error = normalized.results.get("error")
    check("成功记录的 error 值是 null（§7.1 要求 null）",
          error is not None and error.ok is True and error.value is None, error)
    check("null 值按 view() 约定不进入视图，调用方用 .get() 视作 null",
          "error" not in view, sorted(view))

    messages_path = view.get("messages_path")
    check("messages_path 由 prompt_ref 派生", messages_path == record["prompt_ref"],
          messages_path)
    prompt_file = Path(path).parent / str(messages_path)
    check("messages_path 指向真实 Prompt 文件", prompt_file.is_file(), messages_path)

    system, user = result_schema.parse_prompt_text(prompt_file.read_text(encoding="utf-8"))
    check("Prompt 文件能解析出 system/user", system == DRAFT_SYSTEM_PROMPT and bool(user),
          (system, user[:20]))
    check("由 Prompt 文件重建的 messages 哈希等于记录里的 prompt_sha256",
          result_schema.messages_sha256(system, user) == record["prompt_sha256"])
    check("组间差异体现在 Prompt 文本里（D 组注入反馈与记忆）",
          "FEEDBACK_COUNT=2" in user and "MEMORY_COUNT=1" in user, user)

    return normalized


# ---------------------------------------------------------------------------
# 4. 不可恢复字段：如实记录、不许默认值换 PASS
# ---------------------------------------------------------------------------


def test_unrecoverable(normalized):
    missing = normalized.unrecoverable()

    check("不可恢复字段正好是 7 个", len(missing) == 7, missing)
    check("不可恢复集合与契约表一致",
          sorted(missing) == sorted(result_schema.UNRECOVERABLE_FIELDS), missing)
    check("缺字段里没有可派生字段",
          not (set(missing) & set(result_schema.DERIVED_FIELDS)), missing)
    check("标准化结果不完整（不能据此报 PASS）", normalized.is_complete() is False)
    check("missing() 与 unrecoverable() 一致",
          normalized.missing() == [name for name in result_schema.DESIGN_FIELDS
                                   if name in missing], normalized.missing())

    reasons = result_schema.unrecoverable_reasons()
    check("每个不可恢复字段都有原因说明",
          all(reasons.get(name) for name in missing), reasons)


# ---------------------------------------------------------------------------
# 5. 派生规则的边界：语义不一致时必须失败，不能「名字像就复制」
# ---------------------------------------------------------------------------


def test_derivation_boundaries(record, path):
    run_dir = Path(path).parent

    def derive(mutate, *, run_dir_override=None):
        row = json.loads(json.dumps(record, ensure_ascii=False))
        mutate(row)
        target = run_dir if run_dir_override is None else run_dir_override
        return result_schema.normalize_record(row, batch="main", run_dir=target)

    # condition：源记录自带 condition 且与 group 冲突 → 不可采信
    conflict = derive(lambda row: row.__setitem__("condition", "A"))
    check("源记录 condition 与 group 冲突 → 不派生 condition",
          "condition" not in conflict.view()
          and result_schema.derive_condition(
              dict(record, condition="A")).code == result_schema.CONDITION_CONFLICT)
    check("冲突原因写明 group", "group" in conflict.failures().get("condition", ""),
          conflict.failures().get("condition"))

    # condition：缺 group → 无法归属
    no_group = derive(lambda row: row.pop("group"))
    check("缺 group 时 condition 不可派生",
          result_schema.derive_condition({**record, "group": None}).code
          == result_schema.CONDITION_MISSING_GROUP)
    check("缺 group 记为失败", "condition" in no_group.failures())

    # answer/plan：没有 final_output → 不可派生（不是空字符串）
    no_final = derive(lambda row: row.__setitem__("final_output", None))
    check("缺 final_output 时 answer 不可派生", "answer" in no_final.failures())
    check("缺 final_output 时 plan 不可派生", "plan" in no_final.failures())

    # validation：失败记录没有 validation 语义
    failed = derive(lambda row: row.__setitem__("success", False))
    check("失败记录不派生 validation", "validation" in failed.failures())
    check("失败记录不派生 error", "error" in failed.failures())

    # usage：不可用就不导出（绝不补 0）
    no_usage = derive(lambda row: row.__setitem__(
        "token_usage", {"available": False, "status": "unavailable",
                        "prompt_tokens": None, "completion_tokens": None,
                        "total_tokens": None}))
    check("token_usage 不可用时 usage 不导出", "usage" in no_usage.failures())

    # latency：分母缺失不许瞎猜
    no_latency = derive(lambda row: row.__setitem__("elapsed_seconds", None))
    check("缺 elapsed_seconds 时 latency_ms 不可派生",
          "latency_ms" in no_latency.failures())

    # messages_path：Prompt 文件缺失 → 不可派生
    missing_prompt = derive(lambda row: None, run_dir_override=run_dir / "no-such-run")
    check("Prompt 文件缺失时 messages_path 不可派生",
          "messages_path" in missing_prompt.failures(),
          missing_prompt.failures().get("messages_path"))

    # round_index 必须等于 repeat_index（v4.2 起）
    bad_round = derive(lambda row: row.__setitem__("round_index", 99))
    check("round_index ≠ repeat_index 记为失败", "round_index" in bad_round.failures())

    # 精确换算：1234 ms 不能变成 1233 或 1235
    exact = result_schema.normalize_record(
        {**record, "elapsed_seconds": 1.234}, batch="main", run_dir=run_dir)
    check("elapsed_seconds=1.234 精确换算为 1234 ms",
          exact.view().get("latency_ms") == 1234, exact.view().get("latency_ms"))


# ---------------------------------------------------------------------------
# 6. append_jsonl：落盘序列化不改变语义
# ---------------------------------------------------------------------------


def test_append_jsonl(run_dir, api):
    target = Path(run_dir) / "append.jsonl"
    first = {"b": 1, "a": [1, 2]}
    second = {"b": 2, "a": []}

    run_experiment.append_jsonl(target, first)
    run_experiment.append_jsonl(target, second)

    lines = target.read_text(encoding="utf-8").splitlines()
    check("追加两次得到两行", len(lines) == 2, lines)
    check("键按字典序序列化（与统计器的规范 JSON 同序）",
          lines[0].startswith('{"a"'), lines[0][:20])
    check("先写的记录没有被覆盖", json.loads(lines[0]) == first)
    check("中文不转义（ensure_ascii=False）",
          "概率论" in json.dumps({"t": "概率论"}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 7. 消费者一：预检
# ---------------------------------------------------------------------------


def test_preflight_producer_rows(path):
    report = _preflight(path)
    targets = _fail_targets(report)

    gaps = [name for name in _fails(report)]
    check("生产器记录在预检里被判 FAIL（不能靠缺字段混过去）", report.has_fail() is True)
    check("FAIL 条数 = 7 个不可恢复字段",
          len(gaps) == 7, [(item.target, item.area) for item in gaps])

    for name in result_schema.UNRECOVERABLE_FIELDS:
        check(f"缺口 FAIL 点名 {name}", name in targets, targets[:400])

    for name in result_schema.DERIVED_FIELDS:
        check(f"可派生字段 {name} 没有 FAIL", name not in targets)

    reasons = result_schema.unrecoverable_reasons()
    gap_items = [item for item in _fails(report)
                 if any(item.target.endswith(" · " + name)
                        for name in result_schema.UNRECOVERABLE_FIELDS)]
    check("缺口 FAIL 的理由就是契约表里的不可恢复原因（不降级为 WARNING）",
          len(gap_items) == 7
          and all(item.level == "FAIL"
                  and item.reason == reasons[item.target.rsplit(" · ", 1)[1]]
                  for item in gap_items),
          [(item.target, item.level, item.reason[:40]) for item in gap_items])
    check("缺口 FAIL 写明涉及行数与行号",
          all("行缺失：" in item.actual and "[1]" in item.actual
              for item in gap_items),
          [item.actual for item in gap_items][:2])
    check("报告 notes 记录标准化数量",
          any("标准化" in note for note in report.notes), report.notes)

    pass_lines = [item for item in report.findings
                  if item.level == "PASS"
                  and (str(Path(path).name) in item.target
                       or any(name in item.target
                              for name in result_schema.DESIGN_FIELDS))]
    check("没有为「标准化成功」伪造 PASS", pass_lines == [],
          [item.target for item in pass_lines])

    return report


def test_preflight_design_native_unchanged(record, path):
    """回归保护：29 字段齐全的设计原生行仍走原来的严格路径，且不因新分支放宽。"""

    normalized = result_schema.normalize_record(
        record, batch="main", run_dir=Path(path).parent)
    native = dict(normalized.view())
    native.update({
        "run_id": "20261120T140012Z-S2-D-r0",
        "base_sha": "cd" * 32,
        "knowledge_result_ids": [],
        "memory_injected": [{"memory_type": "long_term", "subject_key": "概率论",
                             "strength": 1, "status": "active"}],
        "short_term_injected": True,
        "difficult_flag": True,
        "data_kind": result_schema.DESIGN_DATA_KIND,
        # 成功记录的 error 设计值就是 null：原生行必须显式写 null 才凑齐 29 个字段
        "error": None,
    })
    check("补齐 7 个字段后即为设计原生行",
          result_schema.is_design_native(native) is True, sorted(native))

    native_path = Path(path).parent.parent / "native" / "results.jsonl"
    native_path.parent.mkdir(parents=True, exist_ok=True)
    native_path.write_text(json.dumps(native, ensure_ascii=False) + "\n",
                           encoding="utf-8", newline="\n")

    report = _preflight(native_path)
    check("设计原生行 0 FAIL（原严格路径未被放宽）", report.has_fail() is False,
          [item.render() for item in _fails(report)])


def test_preflight_missing_prompt(run_dir, api):
    record, path = _produce(run_dir, api)
    prompt_file = Path(run_dir) / record["prompt_ref"]
    prompt_file.unlink()

    report = _preflight(path)
    check("Prompt 文件被删 → 预检 FAIL", report.has_fail() is True)
    check("FAIL 点名 messages_path", "messages_path" in _fail_targets(report),
          _fail_targets(report)[:300])


def test_preflight_usage_unavailable(run_dir, api):
    record, path = _produce(run_dir, api, usage=False)

    check("无请求时 token_usage 标记 unavailable",
          record["token_usage"]["available"] is False)
    report = _preflight(path)
    check("usage 不可用 → 预检 FAIL", report.has_fail() is True)
    check("FAIL 点名 usage", "usage" in _fail_targets(report), _fail_targets(report)[:300])


def test_preflight_legacy_plan_sha(run_dir, api):
    """legacy 目录（没有 plan.json）：缺 plan_sha256 只报 WARNING，不得算缺口 FAIL。

    设计 §5.3 的 legacy 例外针对的就是 B0 那批历史数据：那里根本没有 plan.json，
    所以「没有 plan_sha256」是已知限制，不是数据缺陷。
    """

    record, path = _produce(run_dir, api, plan_sha256=None)
    check("生产器记录没有 plan_sha256", record["plan_sha256"] is None)

    normalized = result_schema.normalize_record(
        record, batch="main", run_dir=Path(path).parent)
    check("plan_sha256=null 属于设计上的 null（进 null_valued，不算缺口）",
          "plan_sha256" in normalized.null_valued(), normalized.null_valued())
    check("plan_sha256=null 不进入视图", "plan_sha256" not in normalized.view())
    check("null 值不影响完整性判定", normalized.is_complete() is False)  # 仍缺 7 个不可恢复

    report = _preflight(path)
    targets = _fail_targets(report)
    check("legacy 缺 plan_sha256 不产生 FAIL", "plan_sha256" not in targets,
          targets[:300])
    warnings = [item for item in report.findings
                if item.level == "WARNING" and "plan_sha256" in item.target]
    check("legacy 缺 plan_sha256 报一条 WARNING", len(warnings) == 1,
          [item.target for item in report.findings if item.level == "WARNING"])
    check("WARNING 说明需要负责人确认",
          warnings and "负责人确认" in warnings[0].reason,
          warnings[0].reason if warnings else "")


def test_preflight_failed_record(run_dir, api):
    record, path = _produce(run_dir, api, ok=False)

    check("失败记录 success=False", record["success"] is False)
    check("失败记录 final_output 为空", record["final_output"] is None)

    report = _preflight(path)
    targets = _fail_targets(report)

    for name in ("answer", "plan", "validation", "error"):
        check(f"失败记录在预检里点名 {name}", name in targets)

    check("失败记录不会被当成完整行静默跳过",
          any("失败记录" in str(item.actual) for item in _fails(report)),
          [str(item.actual)[:60] for item in _fails(report)])


# ---------------------------------------------------------------------------
# 7.5 生产器记录 × plan.json 联合核验（第四轮复审）
#
# 第三轮的预检对生产器记录 normalize 后只要 is_complete() 为假就 continue，
# 于是「有 7 个不可恢复字段」被当成「这一行等于不存在」：身份不进集合比较、
# 计划全部报缺失、plan_sha256 同源核对也不做。下面七个场景把两个维度分开钉住：
# 契约缺口照报 FAIL，执行证据照常核验，互不屏蔽、互不伪造 PASS。
# ---------------------------------------------------------------------------


def _plan_for(*, experiment_id=EXPERIMENT_ID, sample_id=SAMPLE_ID, group=GROUP,
              rounds=1):
    """与 _produce 落盘记录同源的 plan.json（身份四元组一一对应）。"""

    return plan_schedule.build_schedule(
        experiment_id=experiment_id,
        sample_groups={sample_id: [group]},
        rounds=rounds,
        seed=SEED,
        scheme=SCHEME,
        algorithm_version=ALGORITHM_VERSION,
        config_hash="cd" * 32,
        created_at="2026-11-20T14:00:00+08:00",
    )


def _gap_fails(report):
    """只挑出「§7.1 缺口」这一类 FAIL（target 结尾是不可恢复字段名）。"""

    return [item for item in _fails(report)
            if item.target.rsplit(" · ", 1)[-1] in result_schema.UNRECOVERABLE_FIELDS]


def test_partial_record_with_plan(run_dir, api):
    """场景 1：正常生产器记录 + 同源 plan.json。"""

    plan = _plan_for()
    record, path = _produce(run_dir, api, plan_sha256=plan["plan_sha256"])
    report, identities = _check(path, plan=plan)
    notes = "\n".join(report.notes)
    targets = _fail_targets(report)

    check("场景1：7 个不可恢复字段仍然逐字段 FAIL",
          len(_gap_fails(report)) == 7,
          [item.target for item in _fails(report)])
    check("场景1：整条记录没有被跳过（身份进入集合比较）",
          identities == [(EXPERIMENT_ID, SAMPLE_ID, GROUP, 0)], identities)
    check("场景1：计划完成度不误报缺失",
          not any(item.level == "FAIL" and "计划完成度" in item.target
                  for item in report.findings)
          and any(item.level == "PASS" and "计划完成度" in item.target
                  for item in report.findings),
          [item.render() for item in report.findings if "计划完成度" in item.target])
    check("场景1：同源时 plan_sha256 不产生 FAIL",
          "plan_sha256" not in targets, targets[:300])
    check("场景1：报告分开写「执行证据」与「§7.1 完整性」",
          "执行证据" in notes and "§7.1 完整性" in notes, notes)
    check("场景1：执行证据给出结果行数与身份可识别行数",
          "非空结果行 1 行" in notes and "身份可识别 1 行" in notes, notes)
    check("场景1：执行证据给出计划覆盖（1 个计划任务、未命中 0）",
          "计划任务 1 个" in notes and "未命中 0 个" in notes, notes)
    check("场景1：§7.1 完整性单独计数（缺口 1 行、完整 0 行）",
          "完整记录 0 行" in notes and "存在契约缺口 1 行" in notes, notes)
    check("场景1：没有为部分标准化伪造 §7.1 合规 PASS",
          [item for item in report.findings
           if item.level == "PASS"
           and item.target.rsplit(" · ", 1)[-1] in result_schema.DESIGN_FIELDS] == [],
          [item.target for item in report.findings if item.level == "PASS"])


def test_partial_record_plan_sha_null(run_dir, api):
    """场景 2：plan.json 存在、记录 plan_sha256 为 null —— 必须额外 FAIL。"""

    plan = _plan_for()
    record, path = _produce(run_dir, api, plan_sha256=None)
    report, identities = _check(path, plan=plan)
    sha_fails = [item for item in _fails(report)
                 if item.target.endswith(" · plan_sha256")]

    check("场景2：plan_sha256=null 额外产生 FAIL", len(sha_fails) == 1,
          [item.target for item in _fails(report)])
    check("场景2：FAIL 写明是「null（记录未提供）」",
          any("null（记录未提供）" in item.actual for item in sha_fails),
          [item.actual for item in sha_fails])
    check("场景2：缺口 FAIL 与同源 FAIL 同时存在（7 + 1 = 8 条）",
          len(_fails(report)) == 8 and len(_gap_fails(report)) == 7,
          len(_fails(report)))
    check("场景2：有 plan.json 时不走 legacy WARNING",
          not any(item.level == "WARNING" and "plan_sha256" in item.target
                  for item in report.findings),
          [item.target for item in report.findings if item.level == "WARNING"])
    check("场景2：身份仍被识别",
          identities == [(EXPERIMENT_ID, SAMPLE_ID, GROUP, 0)], identities)


def test_partial_record_plan_sha_tampered(run_dir, api):
    """场景 3：plan_sha256 被篡改 —— 不能被 7 个缺口遮住。"""

    plan = _plan_for()
    record, path = _produce(run_dir, api, plan_sha256="ee" * 32)
    report, identities = _check(path, plan=plan)
    sha_fails = [item for item in _fails(report)
                 if item.target.endswith(" · plan_sha256")]

    check("场景3：篡改的 plan_sha256 被识别（不同源）",
          len(sha_fails) == 1
          and sha_fails[0].reason == "结果行与同目录 plan.json 不同源",
          [(item.reason, item.actual) for item in sha_fails])
    check("场景3：契约缺口 7 条 + 同源 1 条 = 8 条",
          len(_fails(report)) == 8 and len(_gap_fails(report)) == 7,
          len(_fails(report)))
    check("场景3：篡改不影响身份识别",
          identities == [(EXPERIMENT_ID, SAMPLE_ID, GROUP, 0)], identities)


def test_partial_record_not_in_plan(run_dir, api):
    """场景 4：结果行不属于 plan.json —— 有 7 个缺口也必须发现。"""

    plan = _plan_for(sample_id="S3")
    record, path = _produce(run_dir, api, plan_sha256=plan["plan_sha256"])
    report, identities = _check(path, plan=plan)
    targets = _fail_targets(report)

    check("场景4：发现结果行不在计划中",
          any(item.reason == "结果行不在计划中" for item in _fails(report)),
          [(item.target, item.reason) for item in _fails(report)])
    check("场景4：计划完成度同时报缺失（不因缺口而放过）",
          any(item.level == "FAIL" and "计划完成度" in item.target
                  for item in report.findings),
          [item.render() for item in report.findings if "计划完成度" in item.target])
    check("场景4：缺口 7 条 + 身份 1 条 + 完成度 1 条 = 9 条",
          len(_fails(report)) == 9 and len(_gap_fails(report)) == 7,
          [item.target for item in _fails(report)])
    check("场景4：缺失的计划身份被点名",
          any("S3" in item.reason for item in _fails(report)), targets[:300])


def test_partial_record_duplicate(run_dir, api):
    """场景 5：重复身份 —— 缺口行也必须继续查重。"""

    plan = _plan_for()
    _produce(run_dir, api, plan_sha256=plan["plan_sha256"])
    record, path = _produce(run_dir, api, plan_sha256=plan["plan_sha256"])
    report, identities = _check(path, plan=plan)

    check("场景5：发现重复身份",
          any(item.reason == "同一实验身份出现多条记录" for item in _fails(report)),
          [(item.target, item.actual) for item in _fails(report)])
    check("场景5：重复的同一身份只计 1 个", len(identities) == 1, identities)
    check("场景5：缺口 7 条 + 重复 1 条 = 8 条（缺口按字段聚合不刷屏）",
          len(_fails(report)) == 8 and len(_gap_fails(report)) == 7,
          [item.target for item in _fails(report)])
    check("场景5：两条真实执行记录被计数",
          any("非空结果行 2 行" in note for note in report.notes), report.notes)


def test_partial_record_batch_misattributed(run_dir, api):
    """场景 6：批次归属错误 —— 生产器行的 batch 由目录推断，归属由覆盖检查判定。"""

    plan = _plan_for()
    record, path = _produce(run_dir, api, plan_sha256=plan["plan_sha256"])
    report, identities = _check(path, batch="suppl-subj", plan=plan)
    check("场景6：生产器行照常带上目录推断的批次",
          identities == [(EXPERIMENT_ID, SAMPLE_ID, GROUP, 0)], identities)
    check("场景6：批次本身不产生假 FAIL",
          not any("· batch" in item.target for item in _fails(report)),
          [item.target for item in _fails(report)])

    coverage = preflight_experiment_data.Report()
    preflight_experiment_data.check_coverage(coverage, "suppl-subj", identities,
                                             {EXPERIMENT_ID})
    check("场景6：S2 记录放进 suppl-subj 目录 → 覆盖检查报 FAIL",
          any(item.level == "FAIL" for item in coverage.findings),
          [item.render() for item in coverage.findings])
    check("场景6：覆盖 FAIL 与 7 条缺口 FAIL 互不屏蔽",
          len(_gap_fails(report)) == 7 and len(_fails(report)) == 7,
          [item.target for item in _fails(report)])


def test_partial_record_legacy_still_warning(run_dir, api):
    """场景 7：无 plan.json 的 B0 legacy —— 仍只报 WARNING，不算缺口 FAIL。"""

    record, path = _produce(run_dir, api, plan_sha256=None)
    report, identities = _check(path, plan=None)
    warnings = [item for item in report.findings
                if item.level == "WARNING" and "plan_sha256" in item.target]

    check("场景7：legacy 缺 plan_sha256 只有 WARNING", len(warnings) == 1,
          [item.target for item in report.findings if item.level == "WARNING"])
    check("场景7：不因缺 plan_sha256 多出 FAIL（仍只有 7 条缺口）",
          len(_fails(report)) == 7 and len(_gap_fails(report)) == 7,
          [item.target for item in _fails(report)])
    check("场景7：没有 plan.json 时不做计划完成度判定",
          not any("计划完成度" in item.target for item in report.findings),
          [item.target for item in report.findings])
    check("场景7：legacy 行仍被计入执行证据",
          identities == [(EXPERIMENT_ID, SAMPLE_ID, GROUP, 0)]
          and any("身份可识别 1 行" in note for note in report.notes), report.notes)

# ---------------------------------------------------------------------------
# 7.6 全流程预检：批次不可判定、计划逐字段同源、B0 legacy 目录规则（第五轮复审）
#
# 7.5 的七个场景都只调 check_results()，所以「目录名判不出批次」与「B0 目录名规则」
# 这两条路径没被真正走过：前者对应任务一（batch 不能因为无从推断就冒充契约缺口），
# 后者对应任务四（legacy WARNING 必须真的由 b0-* 目录触发）。
# 这里全部改走 run_preflight()：跑真实 CLI 主流程，输入是真实 run_one() 记录。
# ---------------------------------------------------------------------------

PLAN_FIELD_AUDIT = ("plan_seq", "seed", "scheme", "algorithm_version")


def _repo(root, dirname):
    """临时仓库骨架：返回 (repo_root, runs_dir, run_dir)。"""

    repo_root = Path(root) / "repo"
    runs_dir = repo_root / "experiments" / "graduation" / "runs"
    run_dir = runs_dir / dirname
    run_dir.mkdir(parents=True)
    return repo_root, runs_dir, run_dir


def _write_manifest(repo_root, entries):
    """按磁盘当前内容写清单；entries = [(batch, path, records)]。"""

    repo_root = Path(repo_root)
    manifest = repo_root / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["batch", "batch_desc", "relative_path", "file",
                         "size_bytes", "sha256", "records"])
        for batch, path, records in entries:
            path = Path(path)
            relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
            writer.writerow([batch, "合成数据（" + str(batch) + "）", relative, path.name,
                             path.stat().st_size,
                             hashlib.sha256(path.read_bytes()).hexdigest(), records])
    return manifest


def _write_manifest_without_batch(repo_root, entries):
    """清单没有 batch 列：entries = [(path, records)]。

    清单结构仍合法（relative_path/file 在），但 batch 信息整体缺失 ——
    正式文件必须 FAIL，不能因为「读不到」就回退到记录自述。
    """

    repo_root = Path(repo_root)
    manifest = repo_root / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["batch_desc", "relative_path", "file",
                         "size_bytes", "sha256", "records"])
        for path, records in entries:
            path = Path(path)
            relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
            writer.writerow(["合成数据（无 batch 列）", relative, path.name,
                             path.stat().st_size,
                             hashlib.sha256(path.read_bytes()).hexdigest(), records])
    return manifest


def _write_plan(run_dir, plan):
    path = Path(run_dir) / plan_schedule.PLAN_NAME
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def _tamper_row(path, field, value, *, index=0):
    """只改一行的某一個字段，其余字段（含 plan_sha256）保持原样。"""

    rows = _rows_of(path)
    rows[index][field] = value
    Path(path).write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows), encoding="utf-8", newline="\n")
    return rows[index]


def _planned_case(root, *, tamper=None):
    """一个计划齐备的运行目录：plan.json + 真实生产器记录 + 清单。"""

    repo_root, runs_dir, run_dir = _repo(root, "main-20261113T061051Z")
    plan = _plan_for(rounds=5)          # §5.1 冻结轮数，strict 校验才不误报
    api = _make_api(repo_root)
    # 计划里的 5 个任务全部跑完：计划完成度才是 PASS，篡改用例才有基准
    for repeat_index in range(5):
        _produce(run_dir, api, repeat_index=repeat_index,
                 plan_sha256=plan["plan_sha256"])
    _write_plan(run_dir, plan)
    results_path = run_dir / run_experiment.RESULTS_NAME
    if tamper is not None:
        _tamper_row(results_path, *tamper)
    manifest = _write_manifest(repo_root, (
        ("main", Path(run_dir) / plan_schedule.PLAN_NAME,
         len(plan_schedule.plan_tasks(plan))),
        ("main", results_path, len(_rows_of(results_path))),
    ))
    return repo_root, runs_dir, manifest, plan


def _flow(repo_root, runs_dir, manifest):
    return preflight_experiment_data.run_preflight(runs_dir, manifest, repo_root)


def _field_fails(report, field):
    return [item for item in _fails(report) if item.target.endswith(" · " + field)]


def _findings(report, level, needle):
    return [item for item in report.findings
            if item.level == level and needle in item.target]


def test_flow_manifest_batch_fallback(root):
    """任务一：未识别目录 + 清单 batch=main + 记录不带 batch —— 按清单归入 main。"""

    report = _batch_case(root, "run-no-batch-marker", [_MISSING_BATCH])
    notes = "\n".join(report.notes)

    check("7.6-清单归类：记录不带 batch 时按清单归入 main",
          "批次 main" in notes, report.notes)
    check("7.6-清单归类：不报 batch FAIL",
          _field_fails(report, "batch") == [],
          [item.target for item in _fails(report)])
    check("7.6-清单归类：覆盖按 main 核对（S1 记录不属于主实验 → FAIL）",
          any(item.level == "FAIL" and item.area == "覆盖"
              for item in report.findings),
          [item.render() for item in report.findings if item.area == "覆盖"])
    check("7.6-清单归类：不再报 UNKNOWN（批次已由清单确定）",
          not [item for item in report.findings
               if item.level == "UNKNOWN" and item.area == "覆盖"],
          [item.render() for item in report.findings
           if item.level == "UNKNOWN"])
    check("7.6-清单归类：7 个契约缺口仍逐字段 FAIL",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])


def test_flow_batch_unknown(root):
    """任务一：清单没收录该文件、目录不可判定、记录不带 batch —— 只能 UNKNOWN。"""

    repo_root, runs_dir, run_dir = _repo(root, "run-unknown-batch")
    _record, path = _produce(run_dir, _make_api(repo_root), plan_sha256=None)
    other = repo_root / "docs" / "note.txt"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("占位文件\n", encoding="utf-8")
    manifest = _write_manifest(repo_root, (("main", other, 1),))
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)

    check("7.6-未知批次：无清单批次且目录不可判定时报 UNKNOWN（覆盖｜批次无法判定）",
          any(item.level == "UNKNOWN" and item.area == "覆盖"
              and item.actual == "无法判定" for item in report.findings),
          [item.render() for item in report.findings
           if item.level == "UNKNOWN"])
    check("7.6-未知批次：没有针对 batch 的 FAIL（无从推断 ≠ 契约缺口）",
          not any(item.target.endswith(" · batch") for item in _fails(report)),
          [item.target for item in _fails(report)])
    check("7.6-未知批次：未把未知批次静默记成某个已知批次",
          not any(note.startswith("批次 ") for note in report.notes), report.notes)
    check("7.6-未知批次：结果文件不在清单中仍单独 FAIL（清单覆盖）",
          any(item.level == "FAIL" and item.area == "清单覆盖"
              and item.target.endswith("results.jsonl") for item in report.findings),
          [item.render() for item in report.findings
           if item.area == "清单覆盖"])


def test_flow_plan_audit_control(root):
    """任务三（对照组）：字段与计划一致时，同源审计不产生任何 FAIL。"""

    repo_root, runs_dir, manifest, plan = _planned_case(root)
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    task = plan_schedule.plan_tasks(plan)[0]

    check("7.6-对照组：plan_seq / seed / scheme / algorithm_version 均无 FAIL",
          all(not _field_fails(report, name) for name in PLAN_FIELD_AUDIT),
          [item.target for item in _fails(report)])
    check("7.6-对照组：plan_sha256 同源不产生 FAIL",
          not _field_fails(report, "plan_sha256"),
          [item.target for item in _fails(report)])
    check("7.6-对照组：计划完成度是 PASS（5 个任务全部命中）",
          _findings(report, "PASS", "计划完成度") != []
          and not _findings(report, "FAIL", "计划完成度"),
          [item.render() for item in report.findings if "计划完成度" in item.target])
    check("7.6-对照组：报告写明做了计划同源逐字段核对",
          "计划同源核对" in notes and all(name in notes for name in PLAN_FIELD_AUDIT),
          notes)
    check("7.6-对照组：7 条契约缺口不因同源通过而消失",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])
    check("7.6-对照组：计划任务的 plan_seq 就是 0（篡改用例的基准）",
          task["plan_seq"] == 0, task)


def _flow_plan_tamper(root, *, label, field, value, reason_needle):
    """任务三：单个字段被改成合法值，其余字段（含 plan_sha256）保持正确。"""

    repo_root, runs_dir, manifest, plan = _planned_case(root, tamper=(field, value))
    report = _flow(repo_root, runs_dir, manifest)
    task = plan_schedule.plan_tasks(plan)[0]
    hits = _field_fails(report, field)

    check("7.6-" + label + "：被识别（同源审计独立 FAIL）",
          len(hits) == 1, [item.target for item in _fails(report)])
    check("7.6-" + label + "：FAIL 落在 " + field + " 字段上",
          hits and hits[0].target.endswith(" · " + field)
          and reason_needle in hits[0].reason,
          [(item.target, item.reason) for item in hits])
    expected_value = task["plan_seq"] if field == "plan_seq" else plan[field]
    check("7.6-" + label + "：FAIL 写明实际值与计划预期值",
          hits and hits[0].actual == repr(value)
          and str(expected_value) in hits[0].expected,
          [(item.actual, item.expected) for item in hits])
    check("7.6-" + label + "：plan_sha256 正确，不跟着误报",
          not _field_fails(report, "plan_sha256"),
          [item.target for item in _fails(report)])
    check("7.6-" + label + "：其他身份字段正确，5 行仍被识别",
          "身份可识别 5 行" in "\n".join(report.notes), report.notes)
    check("7.6-" + label + "：7 条契约缺口仍然逐字段 FAIL",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])


def test_flow_plan_seq_tamper(root):
    """只改 plan_seq：类型合法、且在 0..N-1 范围内 —— 只有按身份比对才抓得住。"""

    _flow_plan_tamper(root, label="plan_seq", field="plan_seq", value=2,
                      reason_needle="执行顺序无法审计")


def test_flow_seed_tamper(root):
    _flow_plan_tamper(root, label="seed", field="seed", value=SEED + 1,
                      reason_needle="不同源")


def test_flow_scheme_tamper(root):
    _flow_plan_tamper(root, label="scheme", field="scheme", value="other_scheme_v9",
                      reason_needle="不同源")


def test_flow_algorithm_version_tamper(root):
    _flow_plan_tamper(root, label="algorithm_version", field="algorithm_version",
                      value="sha256-block-order-v2", reason_needle="不同源")


def test_flow_b0_legacy(root):
    """任务四：真实 b0-* 目录 + 无 plan.json —— legacy 例外只报 WARNING。"""

    repo_root, runs_dir, run_dir = _repo(root, "b0-remainder-20261113T013105Z")
    # 批次由 b0-* 目录名识别，身份则必须是 B0 组、样本来自 S5/S7（设计 §2.4 的
    # legacy 探索组）：目录规则与记录身份是两条独立证据，用 S2×D 就测不出后者。
    api = _make_api(repo_root)
    _produce(run_dir, api, plan_sha256=None, sample_id="S7", group="B0",
             repeat_index=0)
    record, path = _produce(run_dir, api, plan_sha256=None, sample_id="S5",
                            group="B0", repeat_index=1)
    # 清单必须非空（空清单本身就是 FAIL），但绝不能覆盖 B0 文件本身：
    # 只放一个与本次核验无关的说明文件，用来证明「未纳入清单」对 legacy 是 WARNING。
    note = repo_root / "docs" / "synthetic-note.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("合成清单占位文件（不属于任何运行目录）\n", encoding="utf-8")
    manifest = _write_manifest(repo_root, (("docs", note, None),))
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)

    check("7.6-B0：目录名按 b0- 规则识别为 legacy",
          preflight_experiment_data.is_b0_legacy(run_dir)
          and preflight_experiment_data.expected_batch_for(run_dir, path) == "b0-legacy",
          [preflight_experiment_data.expected_batch_for(run_dir, path)])
    check("7.6-B0：B0 文件未纳入清单只报 WARNING，不报 FAIL",
          _findings(report, "WARNING", "results.jsonl") != []
          and not _findings(report, "FAIL", "清单覆盖")
          and not any(item.area == "清单覆盖" and item.level == "FAIL"
                      for item in report.findings),
          [item.render() for item in report.findings if item.area == "清单覆盖"])
    check("7.6-B0：无 plan.json 只报 WARNING（缺计划不是 FAIL）",
          [item for item in report.findings
           if item.area == "计划" and item.level == "WARNING"] != []
          and not [item for item in report.findings
                   if item.area == "计划" and item.level == "FAIL"],
          [item.render() for item in report.findings if item.area == "计划"])
    check("7.6-B0：plan_sha256 缺失仅按 legacy 规则报 WARNING",
          len([item for item in report.findings
               if item.level == "WARNING" and "plan_sha256" in item.target]) == 1
          and not _field_fails(report, "plan_sha256"),
          [item.render() for item in report.findings if "plan_sha256" in item.target])
    check("7.6-B0：7 个契约缺口仍保留 FAIL",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])
    check("7.6-B0：legacy 目录没有 manifest 记录数可核（不误判清单 FAIL）",
          not any(item.area == "清单" and item.level == "FAIL"
                  for item in report.findings),
          [item.render() for item in report.findings if item.area == "清单"])
    check("7.6-B0：legacy 行仍被计入执行证据（2 行都是 B0 身份）",
          "身份可识别 2 行" in notes and "批次 b0-legacy：2 条记录" in notes,
          notes)
    check("7.6-B0：记录身份是 S5/S7 × B0，不是主实验 S2×D",
          record.get("sample_id") == "S5" and record.get("group") == "B0",
          [record.get("sample_id"), record.get("group")])


# ---------------------------------------------------------------------------
# 7.7 批次四态与计划身份唯一性（第六轮复审）
#
# 7.6 只用「一条记录 + 没有 batch 字段」覆盖了「批次不可判定」，另外三态（记录一致
# 声明、记录互相矛盾、记录声明非法值）以及「记录声明与目录冲突」都没被走过；计划侧
# 也只比了身份集合，而 plan_identities() 用 set 会把重复身份折叠掉，重复身份从未被测。
# 下面五条批次路径 + 四条计划路径把它们钉住。
# ---------------------------------------------------------------------------

_MISSING_BATCH = object()


def _batch_case(root, dirname, markers, *, plan_sha256=None,
                manifest_batch="main", manifest_without_batch_column=False):
    """一个运行目录：按 markers 逐行写 batch，然后跑完整的 run_preflight()。

    markers[i] 对应第 i 行；_MISSING_BATCH 表示该行完全不带 batch 字段。
    manifest_batch 决定清单给这个文件什么批次；manifest_without_batch_column
    用于造「整张清单没有 batch 列」的场景。
    """

    repo_root, runs_dir, run_dir = _repo(root, dirname)
    api = _make_api(repo_root)
    path = None
    for repeat_index in range(len(markers)):
        _record, path = _produce(run_dir, api, repeat_index=repeat_index,
                                 plan_sha256=plan_sha256)
    rows = _rows_of(path)
    check("7.7-批次场景（" + dirname + "）：合成行数与声明数一致",
          len(rows) == len(markers), [len(rows), len(markers)])
    for row, marker in zip(rows, markers):
        if marker is _MISSING_BATCH:
            row.pop("batch", None)
        else:
            row["batch"] = marker
    Path(path).write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows), encoding="utf-8", newline="\n")
    if manifest_without_batch_column:
        manifest = _write_manifest_without_batch(
            repo_root, ((path, len(rows)),))
    else:
        manifest = _write_manifest(
            repo_root, ((manifest_batch, path, len(rows)),))
    return _flow(repo_root, runs_dir, manifest)


def test_flow_batch_records_consistent(root):
    """未识别目录 + 清单与记录一致声明 suppl-subj —— 归入补充批次，不报 FAIL。"""

    report = _batch_case(root, "run-batch-consistent",
                         ["suppl-subj", "suppl-subj"],
                         manifest_batch="suppl-subj")
    notes = "\n".join(report.notes)

    check("7.7-记录一致声明：不报 batch FAIL",
          _field_fails(report, "batch") == [],
          [item.target for item in _fails(report)])
    check("7.7-记录一致声明：批次被采用并计入覆盖（notes 出现「批次 suppl-subj」）",
          "批次 suppl-subj" in notes, report.notes)
    check("7.7-记录一致声明：不报 UNKNOWN（值已确定，不是证据不足）",
          not [item for item in report.findings
               if item.level == "UNKNOWN" and item.area == "覆盖"],
          [item.render() for item in report.findings
           if item.level == "UNKNOWN"])
    check("7.7-记录一致声明：7 个契约缺口仍逐字段 FAIL",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])


def test_flow_batch_conflict(root):
    """清单给 main，第二行记录却声明 suppl-subj —— 只在第 2 行报冲突。"""

    report = _batch_case(root, "run-batch-conflict", ["main", "suppl-subj"])
    fails = _field_fails(report, "batch")

    check("7.7-记录冲突：只有与清单不符的那一行 FAIL", len(fails) == 1,
          [(item.target, item.actual) for item in fails])
    check("7.7-记录冲突：FAIL 写明清单批次 main 与实际值 suppl-subj",
          bool(fails) and fails[0].expected == "batch=main（由清单给出）"
          and fails[0].actual == "'suppl-subj'",
          [(item.expected, item.actual) for item in fails])
    check("7.7-记录冲突：指向第 2 行",
          bool(fails) and fails[0].target.endswith(":2 · batch"),
          [item.target for item in fails])
    check("7.7-记录冲突：报的是 FAIL 而不是 UNKNOWN",
          bool(fails) and not [item for item in report.findings
                               if item.level == "UNKNOWN" and item.area == "覆盖"],
          [item.render() for item in report.findings
           if item.level == "UNKNOWN"])
    check("7.7-记录冲突：7 个契约缺口仍逐字段 FAIL",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])


def test_flow_batch_invalid(root):
    """batch 是 list/dict/int/bool/空串 —— 逐行结构化 FAIL，不当成「没声明」。"""

    markers = [["main"], {"batch": "main"}, 3, True, "  "]
    report = _batch_case(root, "run-batch-invalid", markers)
    fails = _field_fails(report, "batch")

    check("7.7-非法值：list/dict/int/bool/空串逐行 FAIL（5 条）",
          len(fails) == 5, [(item.target, item.actual) for item in fails])
    check("7.7-非法值：每条都按「非空字符串」类型错误报",
          bool(fails) and all("非空字符串" in item.expected for item in fails),
          [(item.expected,) for item in fails])
    check("7.7-非法值：不降级成 UNKNOWN",
          bool(fails) and not [item for item in report.findings
                               if item.level == "UNKNOWN" and item.area == "覆盖"],
          [item.render() for item in report.findings
           if item.level == "UNKNOWN"])
    check("7.7-非法值：7 个契约缺口仍逐字段 FAIL",
          len(_gap_fails(report)) == 7, [item.target for item in _fails(report)])


def test_flow_batch_directory_conflict(root):
    """main-* 目录里一行声明 suppl-subj：目录可信，只在该行报冲突。"""

    report = _batch_case(root, "main-20261113T061051Z",
                         ["main", "suppl-subj"])
    fails = _field_fails(report, "batch")

    check("7.7-目录冲突：只在声明与目录不符的那一行 FAIL", len(fails) == 1,
          [(item.target, item.actual) for item in fails])
    check("7.7-目录冲突：FAIL 写明目录推断值",
          bool(fails) and fails[0].expected == "batch=main（由目录推断）",
          [(item.expected,) for item in fails])
    check("7.7-目录冲突：指向第 2 行",
          bool(fails) and fails[0].target.endswith(":2 · batch"),
          [(item.target,) for item in fails])


def test_flow_batch_directory_derived(root):
    """对照组：main-* 目录、记录不带 batch —— 沿用目录值，不报 batch FAIL。"""

    report = _batch_case(root, "main-20261113T061051Z",
                         [_MISSING_BATCH, _MISSING_BATCH])
    notes = "\n".join(report.notes)

    check("7.7-目录推断：记录不带 batch 也不报 FAIL",
          _field_fails(report, "batch") == [],
          [item.target for item in _fails(report)])
    check("7.7-目录推断：批次按目录计入 main 覆盖",
          "批次 main" in notes, report.notes)


def test_flow_manifest_vs_directory_conflict(root):
    """main-* 目录 + 清单写 suppl-subj —— 目录与清单矛盾必须 FAIL。"""

    report = _batch_case(root, "main-20261113T061051Z",
                         [_MISSING_BATCH, "main"],
                         manifest_batch="suppl-subj")
    batches = [item for item in report.findings
               if item.level == "FAIL" and item.area == "批次"]

    check("7.7-清单冲突：目录与清单矛盾报文件级 FAIL（恰好 1 条）",
          len(batches) == 1, [item.render() for item in batches])
    check("7.7-清单冲突：FAIL 同时点名目录批次 main 与清单批次 suppl-subj",
          bool(batches) and "main" in batches[0].actual
          and "suppl-subj" in batches[0].actual,
          [(item.actual,) for item in batches])
    check("7.7-清单冲突：不静默采用其中一个批次（不报「批次 main」的覆盖结论）",
          "批次 main" not in "\n".join(report.notes), report.notes)
    check("7.7-清单冲突：记录声明不用于覆盖清单结果（不存在只按记录归类的路径）",
          any(item.target.endswith(" · batch") for item in _fails(report)),
          [item.target for item in _fails(report)])


def test_flow_manifest_batch_empty(root):
    """清单条目 batch 为空串 —— 正式文件必须 FAIL，不能回退到记录自述。"""

    report = _batch_case(root, "main-20261113T061051Z", ["main"],
                         manifest_batch="")
    batches = [item for item in report.findings
               if item.level == "FAIL" and item.area == "批次"]

    check("7.7-清单空 batch：文件级 FAIL 恰好 1 条", len(batches) == 1,
          [item.render() for item in batches])
    check("7.7-清单空 batch：FAIL 说清清单条目 batch 为空",
          bool(batches) and "空" in batches[0].actual,
          [(item.actual,) for item in batches])
    check("7.7-清单空 batch：不按记录自述把文件归入 main",
          "批次 main" not in "\n".join(report.notes), report.notes)


def test_flow_manifest_without_batch_column(root):
    """整张清单没有 batch 列 —— 正式文件必须 FAIL，不能猜批次。"""

    report = _batch_case(root, "main-20261113T061051Z", ["main"],
                         manifest_without_batch_column=True)
    batches = [item for item in report.findings
               if item.level == "FAIL" and item.area == "批次"]

    check("7.7-清单缺列：文件级 FAIL 恰好 1 条", len(batches) == 1,
          [item.render() for item in batches])
    check("7.7-清单缺列：FAIL 说清清单没有 batch 列",
          bool(batches) and "没有 batch 列" in batches[0].actual,
          [(item.actual,) for item in batches])
    check("7.7-清单缺列：不回退到记录自述（不报「批次 main」）",
          "批次 main" not in "\n".join(report.notes), report.notes)


def _plan_case(root, dirname, mutate, *, rows=1,
               manifest_plan_batch="main", manifest_results_batch="main"):
    """计划被 mutate() 改动后重算 plan_sha256，再跑一次完整预检。"""

    repo_root, runs_dir, run_dir = _repo(root, dirname)
    plan = _plan_for(rounds=5)
    mutate(plan)
    plan["plan_sha256"] = plan_schedule.plan_digest(plan)
    _write_plan(run_dir, plan)
    api = _make_api(repo_root)
    path = None
    for repeat_index in range(rows):
        _record, path = _produce(run_dir, api, repeat_index=repeat_index,
                                 plan_sha256=plan["plan_sha256"])
    manifest = _write_manifest(repo_root, (
        (manifest_plan_batch, Path(run_dir) / plan_schedule.PLAN_NAME,
         len(plan_schedule.plan_tasks(plan))),
        (manifest_results_batch, path, len(_rows_of(path))),
    ))
    return _flow(repo_root, runs_dir, manifest)


def _identity_fails(report):
    return [item for item in _fails(report)
            if item.target.endswith(" · 身份唯一性")]


def test_flow_plan_duplicate_identity(root):
    """同一身份两个 plan_seq：即使只有一条结果行匹配，也必须 FAIL。"""

    def mutate(plan):
        tasks = plan_schedule.plan_tasks(plan)
        tasks[1]["repeat_index"] = tasks[0]["repeat_index"]
        tasks[1]["round_index"] = tasks[0]["round_index"]

    report = _plan_case(root, "main-20261113T061051Z", mutate, rows=1)
    fails = _identity_fails(report)

    check("7.7-重复身份：一条结果行匹配也照样 FAIL（1 条）",
          len(fails) == 1, [item.target for item in _fails(report)])
    check("7.7-重复身份：FAIL 点名重复次数与两个 plan_seq",
          bool(fails) and "重复 2 次" in fails[0].actual
          and "plan_seq=0、1" in fails[0].actual,
          [(item.actual,) for item in fails])
    check("7.7-重复身份：FAIL 说清「匹配任意一条都不能证明计划合法」",
          bool(fails) and "执行顺序不可审计" in fails[0].reason,
          [(item.reason,) for item in fails])
    check("7.7-重复身份：不给「身份唯一」PASS",
          not [item for item in report.findings
               if item.level == "PASS" and "身份唯一性" in item.target],
          [item.render() for item in report.findings
           if item.level == "PASS" and "身份唯一性" in item.target])
    check("7.7-重复身份：结果侧不为重复身份建立索引（notes 说明）",
          "重复身份" in "\n".join(report.notes), report.notes)


def test_flow_plan_structure_broken(root):
    """blocks 缺 sample_id：结构不完整时报 FAIL，不崩溃、不伪装成完整索引。"""

    def mutate(plan):
        plan["blocks"][0].pop("sample_id")

    report = _plan_case(root, "main-20261113T061051Z", mutate, rows=1)
    structure = [item for item in _fails(report)
                 if item.target.endswith(" · 计划结构")]

    check("7.7-结构缺失：报「计划结构」FAIL（不抛异常）", len(structure) == 1,
          [item.target for item in _fails(report)])
    check("7.7-结构缺失：FAIL 点明缺的是 sample_id",
          bool(structure) and "sample_id" in structure[0].actual,
          [(item.actual,) for item in structure])
    check("7.7-结构缺失：结果侧不建立身份索引并说明",
          "计划结构不完整" in "\n".join(report.notes), report.notes)
    check("7.7-结构缺失：不给出「身份唯一」PASS",
          not [item for item in report.findings
               if item.level == "PASS" and "身份唯一性" in item.target],
          [item.render() for item in report.findings
           if item.level == "PASS" and "身份唯一性" in item.target])


def test_flow_plan_identity_control(root):
    """对照组：正常计划拿到「身份唯一」PASS，新增检查不误报。"""

    repo_root, runs_dir, manifest, _plan = _planned_case(root)
    report = _flow(repo_root, runs_dir, manifest)
    passes = [item for item in report.findings
              if item.level == "PASS" and item.target.endswith(" · 身份唯一性")]

    check("7.7-对照组：正常计划拿到「身份唯一」PASS", len(passes) == 1,
          [item.render() for item in report.findings
           if "身份唯一性" in item.target])
    check("7.7-对照组：没有重复身份 FAIL", _identity_fails(report) == [],
          [item.target for item in _fails(report)])
    check("7.7-对照组：也没有「计划结构」FAIL",
          not [item for item in _fails(report)
               if item.target.endswith(" · 计划结构")],
          [item.target for item in _fails(report)])
    check("7.7-对照组：正常整数计划没有「计划类型」FAIL",
          not [item for item in _fails(report)
               if item.target.endswith(" · 计划类型")],
          [item.target for item in _fails(report)])
    completion = [item for item in report.findings
                  if item.level == "PASS" and item.target.endswith(" · 计划完成度")]
    check("7.7-对照组：5 条结果拿满「计划完成度」PASS",
          len(completion) == 1 and completion[0].actual == "全部有结果",
          [item.render() for item in report.findings
           if "计划完成度" in item.target])


def test_flow_plan_manifest_batch_mismatch(root):
    """plan.json 与 results.jsonl 的清单批次不同 —— 同一 run-dir 必须一致。"""

    report = _plan_case(root, "main-20261113T061051Z", lambda plan: None, rows=1,
                        manifest_plan_batch="suppl-subj",
                        manifest_results_batch="main")
    hits = [item for item in _fails(report)
            if item.target.endswith("plan.json · batch")]

    check("7.7-计划批次不一致：报且只报一条 FAIL", len(hits) == 1,
          [item.render() for item in _fails(report)])
    check("7.7-计划批次不一致：FAIL 写明应与 results.jsonl 的清单批次一致",
          bool(hits) and "与 results.jsonl 的清单批次一致" in hits[0].expected,
          [(item.expected,) for item in hits])
    check("7.7-计划批次不一致：实际值点名 suppl-subj，预期点名 main",
          bool(hits) and "suppl-subj" in hits[0].actual
          and "main" in hits[0].expected,
          [(item.expected, item.actual) for item in hits])


def _plan_type_case(root, dirname, mutate, *, needle):
    """任务二：合成计划出现等值 float/bool —— 预检自身必须识别为类型错误。"""

    report = _plan_case(root, dirname, mutate, rows=1)
    fails = [item for item in _fails(report)
             if item.target.endswith(" · 计划类型")]
    label = "7.8-计划类型（" + needle + "）"

    check(label + "：恰好一条类型 FAIL", len(fails) == 1,
          [item.render() for item in _fails(report)])
    check(label + "：FAIL 点名 " + needle + " 且说明类型不正确",
          bool(fails) and needle in fails[0].actual
          and "类型不正确" in fails[0].actual,
          [(item.actual,) for item in fails])
    check(label + "：不是靠清单哈希或结果行哈希被提前拦截",
          not [item for item in _fails(report)
               if item.area == "清单"
               or item.target.endswith(" · plan_sha256")],
          [item.render() for item in _fails(report)
           if item.area == "清单"
           or item.target.endswith(" · plan_sha256")])
    check(label + "：不再给出身份唯一性 PASS",
          not [item for item in report.findings
               if item.level == "PASS" and "身份唯一性" in item.target],
          [item.render() for item in report.findings
           if item.level == "PASS" and "身份唯一性" in item.target])


def test_flow_plan_type_float_plan_seq(root):
    def mutate(plan):
        plan_schedule.plan_tasks(plan)[1]["plan_seq"] = 1.0

    _plan_type_case(root, "main-20261113T061051Z", mutate, needle="plan_seq")


def test_flow_plan_type_bool_plan_seq(root):
    def mutate(plan):
        plan_schedule.plan_tasks(plan)[1]["plan_seq"] = True

    _plan_type_case(root, "main-20261113T061051Z", mutate, needle="plan_seq")


def test_flow_plan_type_float_round_index(root):
    def mutate(plan):
        plan_schedule.plan_tasks(plan)[1]["round_index"] = 2.0

    _plan_type_case(root, "main-20261113T061051Z", mutate, needle="round_index")


def test_flow_plan_type_bool_round_index(root):
    def mutate(plan):
        plan_schedule.plan_tasks(plan)[1]["round_index"] = True

    _plan_type_case(root, "main-20261113T061051Z", mutate, needle="round_index")


def test_flow_plan_type_float_seed(root):
    def mutate(plan):
        plan["seed"] = float(plan["seed"])

    _plan_type_case(root, "main-20261113T061051Z", mutate, needle="seed")


def test_flow_plan_type_float_round_count(root):
    def mutate(plan):
        plan["round_count"] = float(plan["round_count"])

    _plan_type_case(root, "main-20261113T061051Z", mutate, needle="round_count")


def _invalid_plan_full_case(root, dirname, mutate, *, needle):
    """第八轮：无效计划 + 完整 5 条结果，计划失败状态必须贯穿结果侧。

    计划类型不合法时，结果侧不得建立身份索引，也不得把 plan=None 误当 legacy。
    这里让 5 条结果全部合法（只是计划本身非法），因此任何「计划完成度 PASS」
    「身份命中」或 legacy WARNING 都只能来自被错误放行的无效计划。
    """

    repo_root, runs_dir, run_dir = _repo(root, dirname)
    plan = _plan_for(rounds=5)
    mutate(plan)
    plan["plan_sha256"] = plan_schedule.plan_digest(plan)
    _write_plan(run_dir, plan)
    api = _make_api(repo_root)
    path = None
    for repeat_index in range(5):
        _record, path = _produce(run_dir, api, repeat_index=repeat_index,
                                 plan_sha256=plan["plan_sha256"])
    manifest = _write_manifest(repo_root, (
        ("main", Path(run_dir) / plan_schedule.PLAN_NAME,
         len(plan_schedule.plan_tasks(plan))),
        ("main", path, len(_rows_of(path))),
    ))
    report = _flow(repo_root, runs_dir, manifest)
    label = "7.9-无效计划（" + needle + "）"
    notes = "\n".join(report.notes)
    fails = [item for item in _fails(report)
             if item.target.endswith(" · 计划类型")]

    check(label + "：5 条完整结果都写入了 results.jsonl",
          len(_rows_of(path)) == 5, len(_rows_of(path)))
    check(label + "：恰好一条计划类型 FAIL", len(fails) == 1,
          [item.render() for item in _fails(report)])
    check(label + "：没有任何「计划完成度」PASS",
          not [item for item in report.findings
               if item.level == "PASS" and item.target.endswith(" · 计划完成度")],
          [item.render() for item in report.findings
           if "计划完成度" in item.target])
    check(label + "：没有任何「身份唯一性」PASS",
          not [item for item in report.findings
               if item.level == "PASS" and "身份唯一性" in item.target],
          [item.render() for item in report.findings
           if item.level == "PASS" and "身份唯一性" in item.target])
    check(label + "：没有依赖无效计划索引的「身份命中」结论",
          "身份命中" not in notes, report.notes)
    check(label + "：不把无效计划当 legacy（没有 legacy plan_sha256 WARNING）",
          not [item for item in report.findings
               if item.level == "WARNING" and "plan_sha256" in item.target],
          [item.render() for item in report.findings
           if item.level == "WARNING" and "plan_sha256" in item.target])
    check(label + "：结果侧说明未建立计划身份索引",
          "plan.json 校验失败" in notes and "未建立计划身份索引" in notes,
          report.notes)


def test_flow_invalid_plan_full_plan_seq(root):
    def mutate(plan):
        plan_schedule.plan_tasks(plan)[1]["plan_seq"] = 1.0

    _invalid_plan_full_case(root, "main-20261113T061051Z", mutate,
                            needle="plan_seq")


def test_flow_invalid_plan_full_seed(root):
    def mutate(plan):
        plan["seed"] = float(plan["seed"])

    _invalid_plan_full_case(root, "main-20261113T061051Z", mutate, needle="seed")


# ---------------------------------------------------------------------------
# 7.10 清单完整性失败必须阻断下游正向结论（第九轮复审）
#
# 7.6–7.9 证明了「文件存在/计划自洽」就能拿计划完成度、覆盖与总量 PASS，但没有
# 证明「文件是否通过可信清单核验」是这些结论的前提。下面用全主实验 140 条真实
# 生产器记录，分别破坏 results/plan 的清单哈希、把 results 移出清单，并保留
# B0 legacy 例外，全部走真实 run_preflight()。
# ---------------------------------------------------------------------------

FULL_MAIN_SAMPLES = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
FULL_MAIN_GROUPS = ("A", "B", "C", "D")


def _write_rows(path, rows):
    Path(path).write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                for row in rows), encoding="utf-8", newline="\n")
    return Path(path)


def _full_main_case(root):
    """7 样本 × A/B/C/D × 5 轮 = 140 条真实生产器记录 + 同源 plan + 清单。"""

    repo_root, runs_dir, run_dir = _repo(root, "main-20261113T061051Z")
    plan = plan_schedule.build_schedule(
        experiment_id=EXPERIMENT_ID,
        sample_groups={sample: list(FULL_MAIN_GROUPS)
                       for sample in FULL_MAIN_SAMPLES},
        rounds=5,
        seed=SEED,
        scheme=SCHEME,
        algorithm_version=ALGORITHM_VERSION,
        config_hash="cd" * 32,
        created_at="2026-11-20T14:00:00+08:00",
    )
    plan_path = _write_plan(run_dir, plan)
    api = _make_api(repo_root)
    path = None
    for block in plan["blocks"]:
        sample_id = block["sample_id"]
        for task in block["tasks"]:
            _record, path = _produce(
                run_dir, api,
                sample_id=sample_id,
                group=task["group"],
                repeat_index=task["repeat_index"],
                round_index=task["round_index"],
                plan_seq=task["plan_seq"],
                plan_sha256=plan["plan_sha256"],
            )
    manifest = _write_manifest(repo_root, (
        ("main", plan_path, len(plan_schedule.plan_tasks(plan))),
        ("main", path, len(_rows_of(path))),
    ))
    return repo_root, runs_dir, run_dir, plan, plan_path, path, manifest


def _full_b0_case(root):
    """B0 legacy 完整 10 条（S5/S7 × B0 × 5 轮）+ 同源 plan + 清单。"""

    repo_root, runs_dir, run_dir = _repo(root, "b0-remainder-20261113T013105Z")
    plan = plan_schedule.build_schedule(
        experiment_id=EXPERIMENT_ID,
        sample_groups={"S5": ["B0"], "S7": ["B0"]},
        rounds=5,
        seed=SEED,
        scheme=SCHEME,
        algorithm_version=ALGORITHM_VERSION,
        config_hash="cd" * 32,
        created_at="2026-11-20T14:00:00+08:00",
    )
    plan_path = _write_plan(run_dir, plan)
    api = _make_api(repo_root)
    path = None
    for block in plan["blocks"]:
        for task in block["tasks"]:
            _record, path = _produce(
                run_dir, api,
                sample_id=block["sample_id"],
                group=task["group"],
                repeat_index=task["repeat_index"],
                round_index=task["round_index"],
                plan_seq=task["plan_seq"],
                plan_sha256=plan["plan_sha256"],
            )
    manifest = _write_manifest(repo_root, (
        ("b0-legacy", plan_path, len(plan_schedule.plan_tasks(plan))),
        ("b0-legacy", path, len(_rows_of(path))),
    ))
    return repo_root, runs_dir, run_dir, plan, plan_path, path, manifest


def _completion_passes(report):
    return [item for item in report.findings
            if item.level == "PASS" and item.target.endswith(" · 计划完成度")]


def _total_passes(report):
    return [item for item in report.findings
            if item.level == "PASS" and item.target == "生成任务总数"]


def test_flow_untrusted_results_tamper(root):
    """场景 1：results 内容被改、清单未刷新 —— 覆盖/完成度/总量都不得 PASS。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_main_case(root)
    rows = _rows_of(path)
    rows[0]["answer"] = "被改写的答案：清单没有刷新"
    _write_rows(path, rows)
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-results-未刷新清单"

    manifest_fails = [item for item in _fails(report)
                      if item.area == "清单" and item.target.endswith("results.jsonl")]
    check(label + "：清单报 results.jsonl 的 SHA/大小 FAIL",
          manifest_fails != []
          and any("SHA-256" in item.reason for item in manifest_fails),
          [item.render() for item in manifest_fails])
    check(label + "：不得产生主批次覆盖 PASS",
          _findings(report, "PASS", "批次 main") == [],
          [item.render() for item in _findings(report, "PASS", "批次 main")])
    check(label + "：不得产生计划完成度 PASS",
          _completion_passes(report) == [],
          [item.render() for item in _completion_passes(report)])
    check(label + "：不得产生可信总量 PASS",
          _total_passes(report) == [],
          [item.render() for item in _total_passes(report)])
    check(label + "：说明结果文件未通过清单核验、身份不计入汇总",
          "results.jsonl 未通过可信清单完整性核验" in notes
          and "身份不计入 by_batch" in notes
          and "不产生计划完成度结论" in notes, report.notes)


def test_flow_untrusted_plan_tamper(root):
    """场景 2：plan 合法字段被改并重算内部哈希、清单未刷新 —— 计划不可用。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_main_case(root)
    plan["created_at"] = "2026-11-21T14:00:00+08:00"
    plan["plan_sha256"] = plan_schedule.plan_digest(plan)
    _write_plan(run_dir, plan)
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-plan-未刷新清单"

    manifest_fails = [item for item in _fails(report)
                      if item.area == "清单" and item.target.endswith("plan.json")]
    check(label + "：清单报 plan.json 的 SHA/大小 FAIL",
          manifest_fails != []
          and any("SHA-256" in item.reason for item in manifest_fails),
          [item.render() for item in manifest_fails])
    check(label + "：不得把计划标成有效（无身份唯一性 PASS）",
          _findings(report, "PASS", "身份唯一性") == [],
          [item.render() for item in _findings(report, "PASS", "身份唯一性")])
    check(label + "：不得建立计划身份索引（无身份命中结论）",
          "身份命中" not in notes, report.notes)
    check(label + "：不得产生计划完成度 PASS",
          _completion_passes(report) == [],
          [item.render() for item in _completion_passes(report)])
    check(label + "：说明计划未通过清单核验且未做内容核验",
          "plan.json：未通过可信清单完整性核验" in notes
          and "未进行计划内容核验" in notes
          and "结果侧未建立计划身份索引" in notes, report.notes)
    check(label + "：不误报成 legacy（无 plan_sha256 WARNING）",
          not [item for item in report.findings
               if item.level == "WARNING" and "plan_sha256" in item.target],
          [item.render() for item in report.findings
           if item.level == "WARNING" and "plan_sha256" in item.target])


def test_flow_trusted_control(root):
    """场景 3：内容与清单完全一致 —— 正常完成度与主批次覆盖 PASS 保留。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_main_case(root)
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-可信对照"

    check(label + "：140 条结果全部写入",
          len(_rows_of(path)) == 140, len(_rows_of(path)))
    check(label + "：计划完成度 PASS",
          len(_completion_passes(report)) == 1,
          [item.render() for item in _completion_passes(report)])
    check(label + "：主批次 140 覆盖 PASS",
          len(_findings(report, "PASS", "批次 main")) == 1,
          [item.render() for item in _findings(report, "PASS", "批次 main")])
    check(label + "：没有触发清单完整性闸门的诊断说明",
          "未通过可信清单完整性核验" not in notes, report.notes)


def test_flow_untrusted_results_not_in_manifest(root):
    """场景 5：results 未纳入清单 —— 不能靠目录前缀或记录自述进入可信汇总。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_main_case(root)
    # 只保留 plan 的清单条目：results 完全不在清单里，但目录名仍是 main-*。
    manifest = _write_manifest(repo_root, (
        ("main", plan_path, len(plan_schedule.plan_tasks(plan))),
    ))
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-results-未纳入清单"

    check(label + "：清单覆盖报 results.jsonl FAIL",
          _findings(report, "FAIL", "results.jsonl") != []
          and any(item.area == "清单覆盖" for item in _findings(report, "FAIL", "results.jsonl")),
          [item.render() for item in _findings(report, "FAIL", "results.jsonl")])
    check(label + "：不得产生主批次覆盖 PASS",
          _findings(report, "PASS", "批次 main") == [],
          [item.render() for item in _findings(report, "PASS", "批次 main")])
    check(label + "：不得产生计划完成度 PASS",
          _completion_passes(report) == [],
          [item.render() for item in _completion_passes(report)])
    check(label + "：不得产生可信总量 PASS",
          _total_passes(report) == [],
          [item.render() for item in _total_passes(report)])
    check(label + "：说明结果文件未纳入可信清单",
          "results.jsonl 未通过可信清单完整性核验" in notes, report.notes)


def test_flow_legacy_not_untrusted(root):
    """场景 4：B0 legacy 未纳入清单是设计例外，不能被新闸门判为不可信。"""

    repo_root, runs_dir, run_dir = _repo(root, "b0-remainder-20261113T013105Z")
    api = _make_api(repo_root)
    _produce(run_dir, api, plan_sha256=None, sample_id="S7", group="B0",
             repeat_index=0)
    note = repo_root / "docs" / "synthetic-note.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("合成清单占位文件（不属于任何运行目录）\n", encoding="utf-8")
    manifest = _write_manifest(repo_root, (("docs", note, None),))
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-B0-legacy"

    check(label + "：legacy 文件未被判为清单不可信",
          "未通过可信清单完整性核验" not in notes, report.notes)
    check(label + "：legacy 身份仍计入 b0-legacy 批次",
          "批次 b0-legacy：1 条记录" in notes, report.notes)
    check(label + "：legacy 未纳入清单仍只报 WARNING、不报清单覆盖 FAIL",
          not any(item.area == "清单覆盖" and item.level == "FAIL"
                  for item in report.findings),
          [item.render() for item in report.findings if item.area == "清单覆盖"])


def test_flow_b0_declared_results_tamper(root):
    """B0 results 已入清单但内容被改 —— legacy 不能绕过完整性失败。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_b0_case(root)
    rows = _rows_of(path)
    original_answer = rows[0]["final_output"]["answer"]
    rows[0]["final_output"]["answer"] = "B0 被改写的答案：清单没有刷新"
    _write_rows(path, rows)
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-B0-已声明-results-未刷新"

    manifest_fails = [item for item in _fails(report)
                      if item.area == "清单" and item.target.endswith("results.jsonl")]
    check(label + "：清单报 B0 results.jsonl 的 SHA/大小 FAIL",
          manifest_fails != []
          and any("SHA-256" in item.reason for item in manifest_fails),
          [item.render() for item in manifest_fails])
    check(label + "：不得产生 B0 覆盖 PASS",
          _findings(report, "PASS", "批次 b0-legacy") == [],
          [item.render() for item in _findings(report, "PASS", "批次 b0-legacy")])
    check(label + "：不得产生计划完成度 PASS",
          _completion_passes(report) == [],
          [item.render() for item in _completion_passes(report)])
    check(label + "：不误报成未声明的 legacy（无 B0 legacy WARNING）",
          not [item for item in report.findings
               if item.level == "WARNING" and "B0 legacy" in item.reason],
          [item.render() for item in report.findings
           if item.level == "WARNING" and "B0 legacy" in item.reason])
    check(label + "：说明 B0 结果文件未通过清单核验",
          "results.jsonl 未通过可信清单完整性核验" in notes, report.notes)

    # 同一份完整 B0 数据恢复原文后必须能拿到覆盖与完成度 PASS，
    # 证明上面的「无 PASS」来自信任闸门，而不是夹具本身缺字段。
    rows[0]["final_output"]["answer"] = original_answer
    _write_rows(path, rows)
    control = _flow(repo_root, runs_dir, manifest)
    check(label + "：完整 B0 正常对照拿到 10 条覆盖结论（legacy 设计差异为 WARNING）",
          len(_findings(control, "WARNING", "批次 b0-legacy")) == 1
          and _findings(control, "PASS", "批次 b0-legacy") == [],
          [item.render() for item in control.findings
           if "批次 b0-legacy" in item.target])
    check(label + "：完整 B0 正常对照拿到计划完成度 PASS",
          len(_completion_passes(control)) == 1,
          [item.render() for item in _completion_passes(control)])


def test_flow_b0_declared_plan_tamper(root):
    """B0 plan 已入清单但内容被改 —— 不能进入有效计划索引。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_b0_case(root)
    plan["created_at"] = "2026-11-21T14:00:00+08:00"
    plan["plan_sha256"] = plan_schedule.plan_digest(plan)
    _write_plan(run_dir, plan)
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-B0-已声明-plan-未刷新"

    manifest_fails = [item for item in _fails(report)
                      if item.area == "清单" and item.target.endswith("plan.json")]
    check(label + "：清单报 B0 plan.json 的 SHA/大小 FAIL",
          manifest_fails != []
          and any("SHA-256" in item.reason for item in manifest_fails),
          [item.render() for item in manifest_fails])
    check(label + "：计划不进入有效索引（无身份唯一性 PASS）",
          _findings(report, "PASS", "身份唯一性") == [],
          [item.render() for item in _findings(report, "PASS", "身份唯一性")])
    check(label + "：不得建立计划身份索引（无身份命中结论）",
          "身份命中" not in notes, report.notes)
    check(label + "：不得产生计划完成度 PASS",
          _completion_passes(report) == [],
          [item.render() for item in _completion_passes(report)])
    check(label + "：说明 B0 计划未通过清单核验且未做内容核验",
          "plan.json：未通过可信清单完整性核验" in notes
          and "未进行计划内容核验" in notes, report.notes)


def test_flow_plan_results_batch_conflict_full(root):
    """完整 main 计划/结果，plan 与 results 清单 batch 冲突 —— 全链路切断。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, _manifest = _full_main_case(root)
    manifest = _write_manifest(repo_root, (
        ("suppl-subj", plan_path, len(plan_schedule.plan_tasks(plan))),
        ("main", path, len(_rows_of(path))),
    ))
    report = _flow(repo_root, runs_dir, manifest)
    notes = "\n".join(report.notes)
    label = "7.10-plan-results-清单批次冲突"

    conflicts = [item for item in _fails(report)
                 if item.target.endswith("plan.json · batch")]
    check(label + "：报且只报一条计划批次冲突 FAIL", len(conflicts) == 1,
          [item.render() for item in _fails(report)])
    check(label + "：FAIL 写明应与 results.jsonl 的清单批次一致",
          bool(conflicts)
          and "与 results.jsonl 的清单批次一致" in conflicts[0].expected,
          [(item.expected,) for item in conflicts])
    check(label + "：实际值点名 suppl-subj，预期点名 main",
          bool(conflicts) and "suppl-subj" in conflicts[0].actual
          and "main" in conflicts[0].expected,
          [(item.expected, item.actual) for item in conflicts])
    check(label + "：不得产生该目录的批次覆盖 PASS",
          _findings(report, "PASS", "批次 main") == [],
          [item.render() for item in _findings(report, "PASS", "批次 main")])
    check(label + "：不得产生计划完成度 PASS",
          _completion_passes(report) == [],
          [item.render() for item in _completion_passes(report)])
    check(label + "：不得产生可信总量 PASS",
          _total_passes(report) == [],
          [item.render() for item in _total_passes(report)])
    check(label + "：不得产生身份关系正向结论",
          _findings(report, "PASS", "身份唯一性") == []
          and _findings(report, "PASS", "身份集合") == []
          and "身份命中" not in notes, report.notes)
    check(label + "：不把冲突降级为 UNKNOWN",
          [item for item in report.findings if item.level == "UNKNOWN"] == [],
          [item.render() for item in report.findings if item.level == "UNKNOWN"])
    check(label + "：说明批次归属冲突、计划和结果均不进入汇总",
          "plan-results-conflict" in notes
          and "计划不进入身份索引" in notes
          and "身份不计入 165 份总量" in notes, report.notes)


def test_flow_plan_results_batch_conflict_control(root):
    """plan/results 清单 batch 一致的完整 main 对照仍保留正向结论。"""

    repo_root, runs_dir, run_dir, plan, plan_path, path, manifest = _full_main_case(root)
    report = _flow(repo_root, runs_dir, manifest)
    label = "7.10-批次冲突-正常对照"

    check(label + "：计划完成度 PASS",
          len(_completion_passes(report)) == 1,
          [item.render() for item in _completion_passes(report)])
    check(label + "：main 批次 140 覆盖 PASS",
          len(_findings(report, "PASS", "批次 main")) == 1,
          [item.render() for item in _findings(report, "PASS", "批次 main")])
    check(label + "：没有批次冲突说明",
          "plan-results-conflict" not in "\n".join(report.notes), report.notes)


# ---------------------------------------------------------------------------
# 8. 消费者二：打包器
# ---------------------------------------------------------------------------


def test_packer(path):
    rows = make_blind_pack.read_rows(Path(path).parent.name, "main",
                                     runs_dir=Path(path).parent.parent)
    check("打包器读得到生产器行", len(rows) == 1, len(rows))

    source = rows[0]
    check("打包器接受生产器行（_validate_row_shape 通过）", source.row == _rows_of(path)[0])
    check("SourceRow.condition 取源记录 group",
          source.condition == source.group == GROUP, (source.condition, source.group))
    check("SourceRow 行哈希与统计器一致",
          source.row_sha256 == analyze_stats.row_sha256(source.row))
    check("SourceRow 文件哈希 = 文件字节 SHA-256",
          source.results_file_sha256
          == hashlib.sha256(Path(path).read_bytes()).hexdigest())
    check("身份三元组可用", (source.sample_id, source.group, source.repeat_index)
          == (SAMPLE_ID, GROUP, 0), source.identity_tuple)


# ---------------------------------------------------------------------------
# 9. 消费者三：统计器的 mapping 核对
# ---------------------------------------------------------------------------


def test_stats_roundtrip(record, path):
    mapping = {"B0001": _mapping_meta(path, record)}
    index, file_hashes = analyze_stats.load_results([path], mapping)

    check("一条 mapping 对应一条源记录", len(index) == 1, index.keys())
    hit = index[mapping["B0001"]["source_result_row_sha256"]]
    check("命中行就是源文件第 1 行", hit["line"] == 1, hit["line"])
    check("命中行内容与源记录一致", hit["row"] == record)
    check("文件哈希被记录", list(file_hashes.values())
          == [mapping["B0001"]["source_results_file_sha256"]])

    # 成本指标只依赖生产器字段，不依赖 §7.1 缺失字段
    metrics = analyze_stats.cost_metrics(index, mapping)
    check("cost_metrics 能吃到这一行（Y2/Y3/Y5 不依赖缺失字段）",
          metrics is not None, type(metrics).__name__)

    return mapping


def test_stats_condition_tamper(record, path, mapping):
    """mapping.condition 被改：文件一个字节没动，必须靠身份比对发现。"""

    bad = {"B0001": _mapping_meta(path, record, condition="A")}
    code, problems, _ = _stats_problems(path, bad)
    check("篡改 mapping.condition → results-mapping-mismatch",
          code == "results-mapping-mismatch", code)
    check("问题里写明 condition 由 group 派生",
          any("condition" in item and "group" in item for item in problems), problems)

    # 对照组：同一份文件、同一行，condition 正确时通过 → 证明不是哈希在报错
    code_ok, problems_ok, _ = _stats_problems(path, mapping)
    check("对照组（condition 正确）通过", code_ok == "", (code_ok, problems_ok))


def test_stats_source_group_tamper(record, path):
    """源行 group 被改：连文件哈希与行哈希一起重算，只剩身份比对能发现。"""

    row = json.loads(json.dumps(record, ensure_ascii=False))
    row["group"] = "A"
    _rewrite(path, row)

    mapping = {"B0001": _mapping_meta(path, row, group=GROUP, condition=GROUP)}
    code, problems, _ = _stats_problems(path, mapping)
    check("篡改源行 group → results-mapping-mismatch",
          code == "results-mapping-mismatch", code)
    check("问题里点名 group 不一致",
          any("group=" in item and "不一致" in item for item in problems), problems)
    check("同时发现 condition 归属被改",
          any("condition" in item for item in problems), problems)


def test_stats_source_condition_conflict(record, path):
    """源行自带 condition 且与 group 冲突：不能因为「字段有了」就放行。"""

    row = json.loads(json.dumps(record, ensure_ascii=False))
    row["condition"] = "A"  # group 仍是 D
    _rewrite(path, row)

    mapping = {"B0001": _mapping_meta(path, row)}
    code, problems, _ = _stats_problems(path, mapping)
    check("源行 condition 与 group 冲突 → results-mapping-mismatch",
          code == "results-mapping-mismatch", code)
    check("问题写明无法确认 condition 归属",
          any("无法确认 condition 归属" in item for item in problems), problems)


def test_stats_group_missing(record, path):
    row = json.loads(json.dumps(record, ensure_ascii=False))
    row.pop("group")
    _rewrite(path, row)

    # 篡改发生在打包之后：mapping 里仍是打包时的真实 group（D）
    mapping = {"B0001": _mapping_meta(path, row, group=GROUP, condition=GROUP)}
    code, problems, _ = _stats_problems(path, mapping)
    check("源行缺 group → results-mapping-mismatch",
          code == "results-mapping-mismatch", code)
    check("问题点名 group 为 None", any("group=None" in item for item in problems),
          problems)
    check("同时报 condition 无法确认",
          any("无法确认 condition 归属" in item for item in problems), problems)


def test_stats_mapping_condition_empty(record, path):
    bad = {"B0001": _mapping_meta(path, record, condition="")}
    code, problems, _ = _stats_problems(path, bad)
    check("mapping.condition 为空 → mismatch", code == "results-mapping-mismatch", code)
    check("问题写明 condition 为空", any("condition 为空" in item for item in problems),
          problems)


# ---------------------------------------------------------------------------
# 10. 三个消费者的口径必须一致
# ---------------------------------------------------------------------------


def test_consumers_agree(record, path, mapping):
    rows = make_blind_pack.read_rows(Path(path).parent.name, "main",
                                     runs_dir=Path(path).parent.parent)
    packer_condition = rows[0].condition
    stats_condition = mapping["B0001"]["condition"]
    derived = result_schema.derive_condition(_rows_of(path)[0])

    check("打包器 / 统计器 / 标准化层对 condition 的判断一致",
          packer_condition == stats_condition == derived.value == GROUP,
          (packer_condition, stats_condition, derived.value))

    preflight_identity = (record["experiment_id"], record["sample_id"],
                          record["group"], record["repeat_index"])
    stats_identity = (mapping["B0001"]["experiment_id"], mapping["B0001"]["sample_id"],
                      mapping["B0001"]["group"], int(mapping["B0001"]["repeat_index"]))
    check("预检与统计器的实验身份四元组一致",
          preflight_identity == stats_identity, (preflight_identity, stats_identity))


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


def _run_all():
    with tempfile.TemporaryDirectory(prefix="grad_contract_") as tmp:
        root = Path(tmp).resolve()

        api = _make_api(root)

        # 2 + 3 + 4：生产器 → 标准化
        record, path = test_producer_record_shape(root / "case-main" / "run", api)
        normalized = test_normalize_success(record, path)
        test_unrecoverable(normalized)

        # 5：派生边界
        test_derivation_boundaries(record, path)

        # 6：append_jsonl
        test_append_jsonl(root / "case-append", api)

        # 7：预检
        test_contract_table()
        test_preflight_producer_rows(path)
        test_preflight_design_native_unchanged(record, path)
        test_preflight_missing_prompt(root / "case-noprompt" / "run", api)
        test_preflight_usage_unavailable(root / "case-nousage" / "run", api)
        test_preflight_legacy_plan_sha(root / "case-legacy" / "run", api)
        test_preflight_failed_record(root / "case-failed" / "run", api)

        # 7.5：生产器记录 × plan.json 联合核验
        for name, runner in (
            ("case-plan-ok", test_partial_record_with_plan),
            ("case-plan-null", test_partial_record_plan_sha_null),
            ("case-plan-tamper", test_partial_record_plan_sha_tampered),
            ("case-plan-notin", test_partial_record_not_in_plan),
            ("case-plan-dup", test_partial_record_duplicate),
            ("case-plan-batch", test_partial_record_batch_misattributed),
            ("case-plan-legacy", test_partial_record_legacy_still_warning),
        ):
            runner(root / name / "run", _make_api(root / name))

        # 7.6：全流程预检（批次不可判定 / 计划逐字段同源 / B0 legacy 目录规则）
        for name, runner in (
            ("case-flow-unknown", test_flow_manifest_batch_fallback),
            ("case-flow-batch-unknown", test_flow_batch_unknown),
            ("case-flow-plan-ok", test_flow_plan_audit_control),
            ("case-flow-plan-seq", test_flow_plan_seq_tamper),
            ("case-flow-seed", test_flow_seed_tamper),
            ("case-flow-scheme", test_flow_scheme_tamper),
            ("case-flow-algorithm", test_flow_algorithm_version_tamper),
            ("case-flow-b0", test_flow_b0_legacy),
        ):
            runner(root / name)

        # 7.7：批次四态 / 计划身份唯一性（第六轮复审）
        for name, runner in (
            ("case-batch-consistent", test_flow_batch_records_consistent),
            ("case-batch-conflict", test_flow_batch_conflict),
            ("case-batch-invalid", test_flow_batch_invalid),
            ("case-batch-dir-conflict", test_flow_batch_directory_conflict),
            ("case-batch-dir-derived", test_flow_batch_directory_derived),
            ("case-batch-manifest-vs-dir", test_flow_manifest_vs_directory_conflict),
            ("case-batch-manifest-empty", test_flow_manifest_batch_empty),
            ("case-batch-manifest-no-column", test_flow_manifest_without_batch_column),
            ("case-plan-batch-mismatch", test_flow_plan_manifest_batch_mismatch),
            ("case-plan-dup-identity", test_flow_plan_duplicate_identity),
            ("case-plan-structure", test_flow_plan_structure_broken),
            ("case-plan-unique-control", test_flow_plan_identity_control),
        ):
            runner(root / name)

        # 7.8：plan.json 严格类型核验（第七轮任务二）
        for name, runner in (
            ("case-type-float-plan-seq", test_flow_plan_type_float_plan_seq),
            ("case-type-bool-plan-seq", test_flow_plan_type_bool_plan_seq),
            ("case-type-float-round-index", test_flow_plan_type_float_round_index),
            ("case-type-bool-round-index", test_flow_plan_type_bool_round_index),
            ("case-type-float-seed", test_flow_plan_type_float_seed),
            ("case-type-float-round-count", test_flow_plan_type_float_round_count),
        ):
            runner(root / name)

        # 7.9：无效计划 + 完整 5 条结果，验证失败状态贯穿结果侧（第八轮任务）
        for name, runner in (
            ("case-invalid-plan-seq", test_flow_invalid_plan_full_plan_seq),
            ("case-invalid-plan-seed", test_flow_invalid_plan_full_seed),
        ):
            runner(root / name)

        # 7.10：清单完整性失败必须阻断下游正向结论（第九轮任务）
        for name, runner in (
            ("case-untrusted-results", test_flow_untrusted_results_tamper),
            ("case-untrusted-plan", test_flow_untrusted_plan_tamper),
            ("case-trusted-control", test_flow_trusted_control),
            ("case-results-not-in-manifest", test_flow_untrusted_results_not_in_manifest),
            ("case-legacy-not-untrusted", test_flow_legacy_not_untrusted),
            ("case-b0-declared-results-tamper", test_flow_b0_declared_results_tamper),
            ("case-b0-declared-plan-tamper", test_flow_b0_declared_plan_tamper),
            ("case-batch-conflict-full", test_flow_plan_results_batch_conflict_full),
            ("case-batch-conflict-control", test_flow_plan_results_batch_conflict_control),
        ):
            runner(root / name)

        # 8：打包器
        test_packer(path)

        # 9：统计器（每条负向用例都用独立目录，避免互相污染）
        mapping = test_stats_roundtrip(record, path)
        test_stats_condition_tamper(record, path, mapping)

        for name, runner in (
            ("case-group-tamper", test_stats_source_group_tamper),
            ("case-condition-conflict", test_stats_source_condition_conflict),
            ("case-group-missing", test_stats_group_missing),
            ("case-condition-empty", test_stats_mapping_condition_empty),
        ):
            case_dir = root / name / "run"
            case_api = _make_api(root / name)
            case_record, case_path = _produce(case_dir, case_api)
            runner(case_record, case_path)

        # 10：口径一致
        test_consumers_agree(record, path, mapping)

    print()

    if INFO:
        for line in INFO:
            print(f"INFO  {line}")

    print(f"\n{len(FAILED)} FAILED / {len(PASSED)} PASSED")

    if FAILED:
        for line in FAILED:
            print(f"  - {line}")

        return 1

    print(f"\nALL {len(PASSED)} TESTS COMPLETED")

    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
