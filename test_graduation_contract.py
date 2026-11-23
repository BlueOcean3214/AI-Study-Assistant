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
             plan_sha256=PLAN_SHA):
    """真实跑一次 run_one() 并真实 append_jsonl() 落盘，返回 (record, 源行号)。"""

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
        plan_seq=repeat_index,
        round_index=repeat_index,
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
