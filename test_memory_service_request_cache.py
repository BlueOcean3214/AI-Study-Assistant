"""memory_service 请求级缓存（apply_feedback 内 cache）的查询计数与结果一致性测试。

两类断言，缺一不可：

1. 查询计数：一次 apply_feedback 里同一个窗口 SQL / 同一个 plan 只回表一次，
   无论请求走到普通更新、pending 消费、只有 neutral、resolved/archived 晋升还是
   time_pattern 的 plan 校验。
2. 业务结果一致性：关掉缓存（把三个读取侧缓存入口全部强制 cache=None）重跑
   同一串反馈，最终落库状态与 info 计数必须逐行相同——缓存只能省查询，不能
   改判定。三个入口是 _window_rows / _plan_difficulties_for /
   _plan_difficulty_for_row，只关前两个不算无缓存对照。

测试全程只用自己的 tempfile 子目录，不碰仓库 feedback.db / .rag_cache。
"""

import contextlib
import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

import database
import memory_service

ORIGINAL_DATABASE_PATH = database.DATABASE_PATH

CHECKS = {"passed": 0, "failed": 0}


def check(name, condition, detail=""):
    if condition:
        CHECKS["passed"] += 1
        print(f"  [OK] {name}")
    else:
        CHECKS["failed"] += 1
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))
    return bool(condition)


# ---------- 环境 ----------


def _temp_dir():
    return tempfile.mkdtemp(prefix="dsbot_reqcache_")


def _activate(db_dir):
    database.DATABASE_PATH = os.path.join(db_dir, "memory.db")
    database.init_db()
    return database.DATABASE_PATH


def _cleanup(db_dir):
    database.DATABASE_PATH = ORIGINAL_DATABASE_PATH
    gc.collect()
    shutil.rmtree(db_dir, ignore_errors=True)


def _insert_plan(plan_date, difficulty, estimated_minutes, task="专项练习"):
    return database.insert_plan(
        plan_date,
        {
            "task": task,
            "estimated_minutes": estimated_minutes,
            "difficulty": difficulty,
            "question_count": 5,
            "reason": "测试计划",
            "completion_criteria": "做完",
            "subtasks_json": "[]",
        },
        f"{plan_date}T08:00:00",
    )


def _insert_feedback(created_at, task="专项练习", status="completed", reason=None,
                     estimated_minutes=20, actual_minutes=30, plan_id=None):
    return database.insert_feedback(
        {
            "task": task,
            "estimated_minutes": estimated_minutes,
            "actual_minutes": actual_minutes,
            "status": status,
            "reason": reason,
            "completed_subtasks": 2 if status == "completed" else 0,
            "total_subtasks": 2,
            "completed_questions": 5 if status == "completed" else 0,
            "total_questions": 5,
            "plan_id": plan_id,
        },
        created_at,
    )


def _insert_memory(memory_type, subject_key, subject_label, value, status="active",
                   episode=1, pending_episode=None, episode_start_feedback_id=None):
    result = database.insert_memory(
        {
            "memory_type": memory_type,
            "subject_key": subject_key,
            "subject_label": subject_label,
            "content": "测试用记忆内容",
            "value_json": json.dumps(value, ensure_ascii=False),
            "strength": 0.6,
            "evidence_count": 3,
            "contradict_count": 0,
            "episode": episode,
            "episode_start_feedback_id": episode_start_feedback_id,
            "status": status,
            "pending_episode": pending_episode,
            "last_supported_at": "2026-11-01T10:00:00",
            "updated_at": "2026-11-01T10:00:00",
        },
        first_seen_at="2026-11-01T10:00:00",
    )
    assert result["inserted"], "测试记忆插入失败"
    return database.get_memory(result["row_id"])


def _event_types(memory_id):
    with sqlite3.connect(database.DATABASE_PATH) as conn:
        rows = conn.execute(
            "SELECT event_type FROM memory_events WHERE memory_id = ? ORDER BY id",
            (memory_id,),
        ).fetchall()
    return [row[0] for row in rows]


def _apply(feedback_id):
    trigger = database.get_feedback(feedback_id)
    return memory_service.apply_feedback(trigger)


# ---------- 查询计数 ----------


class QuerySpy:
    """记录窗口 SQL 与 plan 查询，并记下 get_plan_by_id 的调用方。"""

    def __init__(self):
        self.window = []
        self.plan = []
        self._originals = {}

    def install(self):
        self._originals = {
            "between": database.list_feedbacks_between,
            "with_plan": database.list_feedbacks_with_plan,
            "plan": database.get_plan_by_id,
        }
        spy = self

        def between(start, end, max_id=None, conn=None):
            spy.window.append(("between", start, end, max_id))
            return spy._originals["between"](start, end, max_id=max_id, conn=conn)

        def with_plan(start, end, max_id, difficulties=None, conn=None):
            spy.window.append(
                ("with_plan", start, end, max_id, tuple(difficulties or ()))
            )
            return spy._originals["with_plan"](
                start, end, max_id, difficulties=difficulties, conn=conn
            )

        def plan(plan_id, conn=None):
            spy.plan.append((plan_id, sys._getframe(1).f_code.co_name))
            return spy._originals["plan"](plan_id, conn=conn)

        database.list_feedbacks_between = between
        database.list_feedbacks_with_plan = with_plan
        database.get_plan_by_id = plan
        return self

    def restore(self):
        database.list_feedbacks_between = self._originals["between"]
        database.list_feedbacks_with_plan = self._originals["with_plan"]
        database.get_plan_by_id = self._originals["plan"]

    def cache_plan_calls(self):
        callers = ("_plan_difficulty_for_row", "_plan_difficulties_for")
        return [plan_id for plan_id, caller in self.plan if caller in callers]


def _duplicates(items):
    seen = {}
    for item in items:
        seen[item] = seen.get(item, 0) + 1
    return {key: value for key, value in seen.items() if value > 1}


# ---------- 场景 ----------


def test_window_query_shared_across_memory_types():
    db_dir = _temp_dir()
    try:
        _activate(db_dir)
        plan_id = _insert_plan("2026-11-17", "easy", 20)
        feedback_id = _insert_feedback("2026-11-17T10:00:00", plan_id=plan_id)

        spy = QuerySpy().install()
        try:
            _apply(feedback_id)
        finally:
            spy.restore()

        check("新记忆路径至少扫一次窗口", len(spy.window) >= 1, str(spy.window))
        check("新记忆路径同一窗口只扫一次", not _duplicates(spy.window), str(spy.window))
    finally:
        _cleanup(db_dir)


def test_active_memory_update_uses_shared_cache():
    db_dir = _temp_dir()
    try:
        _activate(db_dir)
        key, label = memory_service.normalize_subject("专项练习")
        memory = _insert_memory(
            "task_performance",
            key,
            label,
            {"samples": 3, "rate": 1.0, "direction": "high"},
        )
        # 先补 3 条已计入的历史样本：记忆的 value_json 与证据必须同源，
        # 否则重算 samples 时回原始反馈会凭空少 3 条。
        history_ids = [
            _insert_feedback(f"2026-11-1{4 + index}T10:00:00") for index in range(3)
        ]
        for index, feedback_id in enumerate(history_ids):
            database.insert_evidence(
                memory["id"], feedback_id, None, memory["episode"], "supports",
                f"2026-11-1{4 + index}T10:00:00",
            )
        feedback_id = _insert_feedback("2026-11-17T10:00:00")

        spy = QuerySpy().install()
        try:
            result = _apply(feedback_id)
        finally:
            spy.restore()

        check("普通更新：同一窗口只扫一次", not _duplicates(spy.window), str(spy.window))
        check("普通更新：info 计数正常", result["info"]["memories_updated"] >= 1,
              str(result["info"]))
        updated = database.get_memory(memory["id"])
        value = json.loads(updated["value_json"])
        check("普通更新：samples 重算为 4", value.get("samples") == 4, str(value))
        check("普通更新：触发反馈写入证据",
              database.get_evidence(memory["id"], feedback_id) is not None,
              "no evidence row")
    finally:
        _cleanup(db_dir)


def test_pending_drain_with_evidence_uses_shared_cache():
    db_dir = _temp_dir()
    try:
        _activate(db_dir)
        key, label = memory_service.normalize_subject("专项练习")
        memory = _insert_memory(
            "difficulty",
            key,
            label,
            {"failed_count": 2, "window_days": 30, "direction": "supports"},
        )
        # pending 只能靠 insert_pending 造：两条历史 completed 对 difficulty 是反证
        history_ids = [
            _insert_feedback("2026-11-15T10:00:00"),
            _insert_feedback("2026-11-16T10:00:00"),
        ]
        for feedback_id in history_ids:
            database.insert_pending(
                memory["id"], feedback_id, None, memory["episode"],
                "2026-11-16T12:00:00", "2026-11-16T12:00:00",
            )

        trigger_id = _insert_feedback("2026-11-17T10:00:00", status="partial",
                                      reason="任务难度太高")

        spy = QuerySpy().install()
        try:
            result = _apply(trigger_id)
        finally:
            spy.restore()

        check("pending 消费：同一窗口只扫一次", not _duplicates(spy.window), str(spy.window))
        check("pending 消费：两条 pending 已消费", database.count_pending(memory["id"]) == 0,
              str(database.count_pending(memory["id"])))
        contradicts = database.list_evidence(memory["id"], relation="contradicts")
        supports = database.list_evidence(memory["id"], relation="supports")
        check("pending 消费：两条 pending 写成反证证据", len(contradicts) == 2, str(contradicts))
        check("pending 消费：触发反馈写成支持证据", len(supports) == 1, str(supports))
        check("pending 消费：info 记录消费数", result["info"]["evidence_written"] >= 2,
              str(result["info"]))
    finally:
        _cleanup(db_dir)


def test_pending_only_neutral_recomputes_with_shared_cache():
    db_dir = _temp_dir()
    try:
        _activate(db_dir)
        key, label = memory_service.normalize_subject("专项练习")
        memory = _insert_memory(
            "difficulty",
            key,
            label,
            {"failed_count": 2, "window_days": 30, "direction": "supports"},
        )
        neutral_ids = [
            _insert_feedback("2026-11-15T10:00:00", status="partial", reason="临时有事情"),
            _insert_feedback("2026-11-16T10:00:00", status="partial", reason="今天状态不好"),
        ]
        for feedback_id in neutral_ids:
            database.insert_pending(
                memory["id"], feedback_id, None, memory["episode"],
                "2026-11-16T12:00:00", "2026-11-16T12:00:00",
            )

        trigger_id = _insert_feedback("2026-11-17T10:00:00", status="partial",
                                      reason="临时有事情")

        spy = QuerySpy().install()
        try:
            result = _apply(trigger_id)
        finally:
            spy.restore()

        check("只有 neutral：同一窗口只扫一次", not _duplicates(spy.window), str(spy.window))
        check("只有 neutral：pending 已消费", database.count_pending(memory["id"]) == 0,
              str(database.count_pending(memory["id"])))
        check("只有 neutral：不写正式证据", len(database.list_evidence(memory["id"])) == 0,
              str(database.list_evidence(memory["id"])))
        # 2 条来自 drain（缓冲区消费），1 条来自当前轮对触发器本身的判定
        check("只有 neutral：neutral_dropped 计 3 条",
              result["info"]["neutral_dropped"] == 3, str(result["info"]))
        check("只有 neutral：记忆保持 active",
              database.get_memory(memory["id"])["status"] == "active",
              str(database.get_memory(memory["id"])["status"]))
    finally:
        _cleanup(db_dir)


def _seed_time_pattern_memory(status="active", pending_episode=None, episode=1,
                              direction="slow"):
    return _insert_memory(
        "time_pattern",
        "easy",
        "简单",
        {"difficulty": "easy", "samples": 4, "median_ratio": 1.8, "direction": direction},
        status=status,
        episode=episode,
        pending_episode=pending_episode,
    )


def _seed_easy_plans():
    return [
        _insert_plan("2026-11-14", "easy", 20),
        _insert_plan("2026-11-15", "easy", 20),
        _insert_plan("2026-11-16", "easy", 20),
    ]


def test_time_pattern_plan_lookup_once_per_plan():
    db_dir = _temp_dir()
    try:
        _activate(db_dir)
        plan_ids = _seed_easy_plans()
        memory = _seed_time_pattern_memory()
        pending_ids = [
            _insert_feedback(f"2026-11-1{4 + index}T10:00:00", plan_id=plan_id,
                             estimated_minutes=20, actual_minutes=40)
            for index, plan_id in enumerate(plan_ids)
        ]
        for index, feedback_id in enumerate(pending_ids):
            database.insert_pending(
                memory["id"], feedback_id, plan_ids[index], memory["episode"],
                "2026-11-16T12:00:00", "2026-11-16T12:00:00",
            )

        trigger_id = _insert_feedback(
            "2026-11-17T10:00:00", plan_id=plan_ids[2], estimated_minutes=20,
            actual_minutes=40,
        )

        spy = QuerySpy().install()
        try:
            _apply(trigger_id)
        finally:
            spy.restore()

        cache_plans = spy.cache_plan_calls()
        check("time_pattern：cache 内的 plan 查询非空", bool(cache_plans), str(spy.plan))
        check("time_pattern：同一个 plan 只回表一次", not _duplicates(cache_plans), str(spy.plan))
        check("time_pattern：窗口只扫一次", not _duplicates(spy.window), str(spy.window))
        updated = database.get_memory(memory["id"])
        check("time_pattern：记忆仍在 active/weakened",
              updated["status"] in ("active", "weakened"), str(updated["status"]))
        check("time_pattern：pending 已全部消费",
              database.count_pending(memory["id"]) == 0,
              str(database.count_pending(memory["id"])))
    finally:
        _cleanup(db_dir)


def _disable_request_cache():
    """把所有读取侧缓存入口强制改成 cache=None；返回 (restore, stats)。

    memory_service 只有三个函数真正读写请求级缓存：
      - _window_rows(893)              窗口 SQL 的结果
      - _plan_difficulties_for(1199)   plan 难度批量补齐
      - _plan_difficulty_for_row(528)  单行 plan 校验（_episode_candidates 与
                                       _plan_difficulties_for 内部都走它）
    _candidate_rows / _episode_candidates / _episode_value / _drain_pending /
    _try_promote / apply_feedback 只是把同一个 cache 往下传，所以三个入口
    全部置空就等于整条链路无缓存。

    stats["caches_seen"] 记录拿到非 None cache 对象的次数，stats["observed_caches"]
    保存这些对象本身，跑完必须全部为空字典——即没有任何一次读写落进请求级
    缓存；stats["plan_row_calls"] 记录单行 plan 校验实际被调用次数，用来证明
    无缓存对照不是空跑。包装函数保留原名，QuerySpy 才能按调用方名字认出
    plan 查询。
    """

    stats = {"caches_seen": 0, "plan_row_calls": 0, "observed_caches": []}
    originals = {
        "_window_rows": memory_service._window_rows,
        "_plan_difficulties_for": memory_service._plan_difficulties_for,
        "_plan_difficulty_for_row": memory_service._plan_difficulty_for_row,
    }

    def note_cache(cache):
        if cache is not None:
            stats["caches_seen"] += 1
            if not any(cache is seen for seen in stats["observed_caches"]):
                stats["observed_caches"].append(cache)

    def _window_rows(start, end, max_id, conn, cache=None, with_plan_difficulty=None):
        note_cache(cache)
        return originals["_window_rows"](
            start, end, max_id, conn, None, with_plan_difficulty
        )

    def _plan_difficulties_for(rows, conn, subject_key=None, cache=None):
        note_cache(cache)
        return originals["_plan_difficulties_for"](rows, conn, subject_key, None)

    def _plan_difficulty_for_row(row, subject_key, conn, cache=None):
        note_cache(cache)
        stats["plan_row_calls"] += 1
        return originals["_plan_difficulty_for_row"](row, subject_key, conn, None)

    memory_service._window_rows = _window_rows
    memory_service._plan_difficulties_for = _plan_difficulties_for
    memory_service._plan_difficulty_for_row = _plan_difficulty_for_row

    def restore():
        memory_service._window_rows = originals["_window_rows"]
        memory_service._plan_difficulties_for = originals["_plan_difficulties_for"]
        memory_service._plan_difficulty_for_row = originals["_plan_difficulty_for_row"]

    return restore, stats


@contextlib.contextmanager
def _cache_disabled():
    """无缓存对照的上下文：进入时关掉三个入口，退出时在 finally 里恢复。"""

    restore, stats = _disable_request_cache()
    try:
        yield stats
    finally:
        restore()


def _scenario_active_update():
    """active 记忆重算：task_performance 记忆 + 3 条历史证据 + 触发反馈。"""

    key, label = memory_service.normalize_subject("专项练习")
    memory = _insert_memory(
        "task_performance", key, label,
        {"samples": 3, "rate": 1.0, "direction": "high"},
    )
    for index in range(3):
        feedback_id = _insert_feedback(f"2026-11-1{4 + index}T10:00:00")
        database.insert_evidence(
            memory["id"], feedback_id, None, memory["episode"], "supports",
            f"2026-11-1{4 + index}T10:00:00",
        )
    return _insert_feedback("2026-11-17T10:00:00")


def _scenario_pending_drain():
    """pending 消费：difficulty 记忆 + 2 条 pending + partial 触发。"""

    key, label = memory_service.normalize_subject("专项练习")
    memory = _insert_memory(
        "difficulty", key, label,
        {"failed_count": 2, "window_days": 30, "direction": "supports"},
    )
    for created_at in ("2026-11-15T10:00:00", "2026-11-16T10:00:00"):
        feedback_id = _insert_feedback(created_at)
        database.insert_pending(
            memory["id"], feedback_id, None, memory["episode"],
            "2026-11-16T12:00:00", "2026-11-16T12:00:00",
        )
    return _insert_feedback("2026-11-17T10:00:00", status="partial", reason="任务难度太高")


def _scenario_time_pattern_promotion():
    """resolved → active 晋升：time_pattern 记忆 + 3 条带 plan 的 pending + 触发。"""

    plan_ids = _seed_easy_plans()
    memory = _seed_time_pattern_memory(status="resolved", pending_episode=2)
    for index, plan_id in enumerate(plan_ids):
        feedback_id = _insert_feedback(
            f"2026-11-1{4 + index}T10:00:00", plan_id=plan_id,
            estimated_minutes=20, actual_minutes=40,
        )
        database.insert_pending(
            memory["id"], feedback_id, plan_id, 2,
            "2026-11-16T12:00:00", "2026-11-16T12:00:00",
        )
    return _insert_feedback(
        "2026-11-17T10:00:00", plan_id=plan_ids[2], estimated_minutes=20,
        actual_minutes=40,
    )


SNAPSHOT_TABLES = (
    "feedbacks",
    "plans",
    "learning_memories",
    "memory_evidence",
    "memory_events",
    "memory_pending_evidence",
)


def test_promotion_path_shares_cache():
    db_dir = _temp_dir()
    try:
        _activate(db_dir)
        plan_ids = _seed_easy_plans()
        memory = _seed_time_pattern_memory(status="resolved", pending_episode=2)
        pending_ids = [
            _insert_feedback(f"2026-11-1{4 + index}T10:00:00", plan_id=plan_id,
                             estimated_minutes=20, actual_minutes=40)
            for index, plan_id in enumerate(plan_ids)
        ]
        for index, feedback_id in enumerate(pending_ids):
            database.insert_pending(
                memory["id"], feedback_id, plan_ids[index], 2,
                "2026-11-16T12:00:00", "2026-11-16T12:00:00",
            )
        trigger_id = _insert_feedback(
            "2026-11-17T10:00:00", plan_id=plan_ids[2], estimated_minutes=20,
            actual_minutes=40,
        )

        spy = QuerySpy().install()
        try:
            result = _apply(trigger_id)
        finally:
            spy.restore()

        updated = database.get_memory(memory["id"])
        check("晋升：resolved 回到 active", updated["status"] == "active", str(updated["status"]))
        check("晋升：episode 推进到 2", updated["episode"] == 2, str(updated["episode"]))
        check("晋升：缓冲区已清空", database.count_pending(memory["id"], episode=2) == 0,
              str(database.count_pending(memory["id"], episode=2)))
        check("晋升：发生 reopened 事件",
              any(event.startswith("reopened") for event in _event_types(memory["id"])),
              str(_event_types(memory["id"])))
        check("晋升：info 记录 memories_updated", result["info"]["memories_updated"] >= 1,
              str(result["info"]))
        check("晋升：同一窗口只扫一次", not _duplicates(spy.window), str(spy.window))
        check("晋升：同一个 plan 只回表一次",
              not _duplicates(spy.cache_plan_calls()), str(spy.plan))
    finally:
        _cleanup(db_dir)


def _snapshot(db_path):
    snapshot = {}
    with sqlite3.connect(db_path) as conn:
        for table in SNAPSHOT_TABLES:
            snapshot[table] = conn.execute(
                f"SELECT * FROM {table} ORDER BY id"
            ).fetchall()
    return snapshot


def _run_scenario(builder, db_dir, disable_cache):
    """在全新临时库里跑一个场景，返回 info、全库快照与查询计数。"""

    _activate(db_dir)
    trigger_id = builder()

    spy = QuerySpy().install()
    try:
        if disable_cache:
            with _cache_disabled() as stats:
                result = _apply(trigger_id)
        else:
            stats = {"caches_seen": 0, "plan_row_calls": 0, "observed_caches": []}
            result = _apply(trigger_id)
    finally:
        spy.restore()

    return {
        "info": result["info"],
        "snapshot": _snapshot(database.DATABASE_PATH),
        "window_calls": len(spy.window),
        "plan_calls": spy.cache_plan_calls(),
        "stats": stats,
    }


def _check_scenario_matches(label, cached, plain, expect_plan_lookups=False):
    """缓存版与无缓存版逐项比较：info + 六张表逐行 + 查询次数方向。"""

    check(f"{label}：无缓存版的请求级 cache 始终为空",
          all(len(cache) == 0 for cache in plain["stats"]["observed_caches"]),
          f"观察到 {plain['stats']['caches_seen']} 次 cache 传递，非空条目="
          f"{[len(cache) for cache in plain['stats']['observed_caches']]}")
    check(f"{label}：apply_feedback 返回的 info 完全相同",
          cached["info"] == plain["info"],
          f"{cached['info']} != {plain['info']}")
    for table in SNAPSHOT_TABLES:
        check(f"{label}：{table} 逐行相同",
              cached["snapshot"][table] == plain["snapshot"][table],
              f"{len(cached['snapshot'][table])} 行 vs "
              f"{len(plain['snapshot'][table])} 行")
    check(f"{label}：缓存版窗口查询次数不多于无缓存版",
          cached["window_calls"] <= plain["window_calls"],
          f"{cached['window_calls']} vs {plain['window_calls']}")
    if expect_plan_lookups:
        check(f"{label}：无缓存对照确实走到了单行 plan 校验",
              plain["stats"]["plan_row_calls"] > 0, str(plain["stats"]))
        check(f"{label}：缓存版 plan 回表次数少于无缓存版",
              len(cached["plan_calls"]) < len(plain["plan_calls"]),
              f"{len(cached['plan_calls'])} vs {len(plain['plan_calls'])}")


def test_cached_and_uncached_runs_match():
    """同一串反馈跑两遍：一遍带请求级缓存，一遍完全关掉，结果必须一致。

    三个场景各起两个全新临时库：active 重算、pending 消费、resolved 晋升；
    晋升场景还要求无缓存版真的调用过单行 plan 校验、缓存版真的少回表，否则
    「cache 始终为空」只是个空断言。
    """

    scenarios = (
        ("active 更新", _scenario_active_update, False),
        ("pending 消费", _scenario_pending_drain, False),
        ("resolved 晋升", _scenario_time_pattern_promotion, True),
    )
    for label, builder, expect_plan_lookups in scenarios:
        cached_dir = _temp_dir()
        plain_dir = _temp_dir()
        try:
            cached = _run_scenario(builder, cached_dir, disable_cache=False)
            plain = _run_scenario(builder, plain_dir, disable_cache=True)
            _check_scenario_matches(label, cached, plain, expect_plan_lookups)
        finally:
            _cleanup(cached_dir)
            _cleanup(plain_dir)


def _run_all():
    tests = [
        test_window_query_shared_across_memory_types,
        test_active_memory_update_uses_shared_cache,
        test_pending_drain_with_evidence_uses_shared_cache,
        test_pending_only_neutral_recomputes_with_shared_cache,
        test_time_pattern_plan_lookup_once_per_plan,
        test_promotion_path_shares_cache,
        test_cached_and_uncached_runs_match,
    ]

    for test in tests:
        print(f"[{test.__name__}]")
        try:
            test()
        except Exception:  # noqa: BLE001  失败也要跑完其余用例
            CHECKS["failed"] += 1
            print("  [FAIL] 用例抛异常")
            traceback.print_exc()

    print(f"\n合计：{CHECKS['passed']} 通过，{CHECKS['failed']} 失败")
    return 0 if CHECKS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
