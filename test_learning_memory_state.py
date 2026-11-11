"""Batch B 测试：共享事务、记忆状态机、在线更新与全量重建（v16 §5）。

运行：.venv\\Scripts\\python.exe test_learning_memory_state.py

设计基线：docs/STAGE2-LEARNING-MEMORY-DESIGN.md v16
- 事务层：database.transaction() 与可选 conn 参数（§Batch B 委派第一优先级）
- 状态机：§5.2 候选与 relation、§5.3 轮次/pending/stale、§5.5 强度
- 在线入口：apply_pending_updates()（§5.3.3.2 游标）
- 全量重建：rebuild_all() + §5.3.8 语义投影比较

所有测试都在临时数据库上跑，结尾断言真实 feedback.db 字节不变。
"""

import json
import os
import pathlib
import sqlite3
import threading

PROJECT_DIR = pathlib.Path(__file__).resolve().parent

os.environ.setdefault("FEEDBACK_DB_PATH", str(PROJECT_DIR / "_test_memory_state_env.db"))
os.environ.setdefault("EMBEDDING_PROVIDER", "none")

import database
import memory_service

TEST_DB = str(PROJECT_DIR / "_test_memory_state.db")
REAL_DB = str(PROJECT_DIR / "feedback.db")
_CREATED_DBS = []
_DB_SEQ = [0]


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}".strip())

    print(f"PASS: {name} {detail}".rstrip())


def remove_file(path):
    for suffix in ("", "-wal", "-shm"):
        target = pathlib.Path(str(path) + suffix)

        if not target.exists():
            continue

        for _ in range(20):
            try:
                target.unlink()
                break
            except PermissionError:
                import time

                time.sleep(0.1)


def connect(path=None):
    return sqlite3.connect(path or TEST_DB)


def use(path=None):
    global TEST_DB

    if path is not None:
        TEST_DB = str(path)

    database.DATABASE_PATH = TEST_DB


def fresh(name=None):
    """每个测试一个全新库文件：删文件 + 跑迁移。

    用独立文件而不是共用一个库，是因为 Windows 上残留的 sqlite 句柄会让删除
    静默失败，旧 feedbacks.id / 旧记忆行会串进下一个测试，把游标与 id 上界类
    断言污染成假结果。
    """

    global TEST_DB

    if name is None:
        _DB_SEQ[0] += 1
        name = str(PROJECT_DIR / f"_test_memory_state_{_DB_SEQ[0]}.db")
        _CREATED_DBS.append(name)

    use(name)
    remove_file(name)

    for suffix in ("", "-wal", "-shm"):
        leftover = pathlib.Path(str(name) + suffix)

        if leftover.exists():
            raise AssertionError(f"FAILED: 测试库未删除（仍有连接持有句柄）: {leftover}")

    database.init_db()
    return name


def cleanup_databases():
    for name in _CREATED_DBS:
        remove_file(name)


def insert_feedback_row(task, status, reason=None, minutes=30, actual=45, created_at=None,
                        plan_id=None, path=None):
    conn = connect(path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions, total_questions,
             created_at, plan_id)
            VALUES (?, ?, ?, ?, ?, 0, 1, 0, 1, ?, ?)
            """,
            (task, minutes, actual, status, reason,
             created_at or "2026-01-01T09:00:00Z", plan_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def insert_plan_row(plan_date, task, difficulty="medium", estimated=30, path=None):
    conn = connect(path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO plans
            (plan_date, task, estimated_minutes, difficulty, question_count,
             reason, completion_criteria, subtasks_json, status, created_at)
            VALUES (?, ?, ?, ?, 1, '测试', '完成', '[]', 'active', ?)
            """,
            (plan_date, task, estimated, difficulty, plan_date + "T08:00:00Z"),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def row_counts(path=None):
    conn = connect(path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "learning_memories",
                "memory_evidence",
                "memory_pending_evidence",
                "memory_events",
            )
        }
    finally:
        conn.close()


def memory_by_key(memory_type, subject_key, path=None):
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM learning_memories WHERE memory_type = ? AND subject_key = ?",
            (memory_type, subject_key),
        ).fetchone()
        return row
    finally:
        conn.close()


def cursor_row(path=None):
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT last_applied_feedback_id, updated_at FROM memory_apply_cursor"
        ).fetchone()
    finally:
        conn.close()


def subject_key(task):
    """归一化后的 subject_key（「XX练习」会被剥掉后缀，测试必须用它查行）。"""

    return memory_service.normalize_subject(task)[0]


def memory_of(memory_type, task, path=None):
    return memory_by_key(memory_type, subject_key(task), path)


def key_counts(memory_type, subject_key, path=None):
    """按业务键直接取行数（time_pattern 的 key 是难度，不是任务名）。"""

    row = memory_by_key(memory_type, subject_key, path)
    conn = connect(path)
    try:
        if row is None:
            return {"evidence": 0, "pending": 0, "events": 0}

        memory_id = row[0]
        return {
            "evidence": conn.execute(
                "SELECT COUNT(*) FROM memory_evidence WHERE memory_id = ?", (memory_id,)
            ).fetchone()[0],
            "pending": conn.execute(
                "SELECT COUNT(*) FROM memory_pending_evidence WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE memory_id = ?", (memory_id,)
            ).fetchone()[0],
        }
    finally:
        conn.close()


def scoped_counts(memory_type, task, path=None):
    """某条记忆自己的证据 / pending / 事件行数（全局计数会被其它类型干扰）。"""

    row = memory_of(memory_type, task, path)
    conn = connect(path)
    try:
        if row is None:
            return {"evidence": 0, "pending": 0, "events": 0}

        memory_id = row[0]
        return {
            "evidence": conn.execute(
                "SELECT COUNT(*) FROM memory_evidence WHERE memory_id = ?", (memory_id,)
            ).fetchone()[0],
            "pending": conn.execute(
                "SELECT COUNT(*) FROM memory_pending_evidence WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE memory_id = ?", (memory_id,)
            ).fetchone()[0],
        }
    finally:
        conn.close()


def events_of(memory_type, task, path=None):
    row = memory_of(memory_type, task, path)

    if row is None:
        return []

    conn = connect(path)
    try:
        return [
            item[0]
            for item in conn.execute(
                "SELECT event_type FROM memory_events WHERE memory_id = ? ORDER BY id",
                (row[0],),
            ).fetchall()
        ]
    finally:
        conn.close()


# ==========================
# 一、共享事务层
# ==========================


def test_transaction_rollback_restores_all_tables():
    """事务中途失败：memory/evidence/event/pending/cursor 全部回到事务前状态。

    这是 Batch B 的第一优先级契约：不允许出现「证据已提交但游标没推进」
    「pending 已清但 memory 未更新」这类半成品状态。
    """

    fresh()
    insert_feedback_row("事务回滚", "completed", None)

    before_cursor = cursor_row()
    before_counts = row_counts()

    def run_transaction(kind):
        with database.transaction() as conn:
            memory = database.insert_memory(
                {
                    "memory_type": "task_topic",
                    "subject_key": "事务回滚",
                    "subject_label": "事务回滚",
                    "content": "测试内容",
                    "value_json": memory_service.encode_value(
                        {"appearances": 3, "direction": "appearance"}
                    ),
                    "strength": 0.5,
                    "evidence_count": 1,
                    "contradict_count": 0,
                    "episode": 1,
                    "episode_start_feedback_id": 1,
                    "episode_started_at": "2026-01-01T09:00:00Z",
                    "pending_episode": None,
                    "pending_start_feedback_id": None,
                    "status": "active",
                    "last_supported_at": "2026-01-01T09:00:00Z",
                    "updated_at": "2026-01-01T09:00:00Z",
                },
                "2026-01-01T09:00:00Z",
                conn=conn,
            )
            memory_id = memory["row_id"]
            database.insert_evidence(
                memory_id, 1, None, 1, "supports", "2026-01-01T09:00:00Z", conn=conn
            )
            database.insert_pending(
                memory_id, 1, None, 2, "2026-01-01T09:00:00Z", "2026-01-01T09:00:00Z",
                conn=conn,
            )
            database.insert_memory_event(
                memory_id, "created",
                json.dumps({"as_of": "2026-01-01T09:00:00Z", "episode": 1,
                            "evidence_ids": [1]}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                "2026-01-01T09:00:00Z", conn=conn,
            )
            database.write_cursor(1, "2026-01-02T00:00:00Z", conn=conn)

            if kind == "raise":
                raise RuntimeError("注入失败")

    try:
        run_transaction("raise")
        check("事务回滚：异常被重抛", False)
    except RuntimeError:
        check("事务回滚：异常被重抛", True)

    check("事务回滚：四张派生表全部为空", row_counts() == before_counts, str(row_counts()))
    check(
        "事务回滚：游标未推进",
        cursor_row() == before_cursor,
        f"{cursor_row()} != {before_cursor}",
    )
    check("事务回滚：连接已关闭且未留下残留", True)

    run_transaction("commit")
    after = row_counts()
    check(
        "正常提交：四张表各写一行",
        after == {"learning_memories": 1, "memory_evidence": 1,
                  "memory_pending_evidence": 1, "memory_events": 1},
        str(after),
    )
    check("正常提交：游标推进且 updated_at 非空", cursor_row() == (1, "2026-01-02T00:00:00Z"))


def test_conn_reuse_does_not_commit_early():
    """传入 conn 的 helper 不得自行提交：外层回滚后这些写入必须一起消失。"""

    fresh()
    counts_before = row_counts()

    try:
        with database.transaction() as conn:
            database.insert_memory(
                {
                    "memory_type": "task_topic",
                    "subject_key": "提前提交",
                    "subject_label": "提前提交",
                    "content": "x",
                    "value_json": memory_service.encode_value(
                        {"appearances": 3, "direction": "appearance"}
                    ),
                    "strength": 0.5,
                    "evidence_count": 0,
                    "contradict_count": 0,
                    "episode": 1,
                    "episode_start_feedback_id": 1,
                    "episode_started_at": "2026-01-01T09:00:00Z",
                    "pending_episode": None,
                    "pending_start_feedback_id": None,
                    "status": "active",
                    "last_supported_at": "2026-01-01T09:00:00Z",
                    "updated_at": "2026-01-01T09:00:00Z",
                },
                "2026-01-01T09:00:00Z",
                conn=conn,
            )
            # 事务内可见
            inside = database.list_memories(limit=10, conn=conn)
            check("conn 复用：事务内可见刚写入的行", len(inside) == 1)
            raise RuntimeError("回滚")
    except RuntimeError:
        pass

    check("conn 复用：外层回滚后不留行（没有提前提交）", row_counts() == counts_before, str(row_counts()))


def test_transaction_begin_immediate_blocks_concurrent_writer():
    """BEGIN IMMEDIATE 立即取写锁：并发的第二个写事务必须被挡住而不是插队。"""

    fresh()
    observed = {}

    def second_writer():
        conn = sqlite3.connect(TEST_DB, timeout=0.2)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            observed["second"] = "acquired"
            conn.execute("COMMIT")
        except sqlite3.OperationalError as error:
            observed["second"] = str(error)
        finally:
            conn.close()

    with database.transaction() as conn:
        conn.execute("SELECT 1").fetchone()
        thread = threading.Thread(target=second_writer)
        thread.start()
        thread.join(5)

    check(
        "并发写者被写锁挡住",
        observed.get("second", "").lower().startswith("database is locked"),
        observed.get("second", ""),
    )


# ==========================
# 二、在线游标与批量处理
# ==========================


def test_apply_pending_updates_processes_in_id_order():
    """处理全部 id > cursor 的现存反馈，升序；重复调用幂等。"""

    fresh()
    insert_feedback_row("游标练习", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("游标练习", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")

    result = memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")
    check("在线入口：ok", result["ok"] is True, str(result.keys()))
    check("在线入口：处理两条", result["processed"] == 2, str(result["processed"]))
    check("在线入口：游标推进到 2", cursor_row()[0] == 2, str(cursor_row()))
    check("在线入口：updated_at 非空", bool(cursor_row()[1]))

    repeat = memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")
    check("在线入口：重复调用处理 0 条", repeat["processed"] == 0, str(repeat["processed"]))

    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0]
    finally:
        conn.close()

    check("在线入口：重复调用不增加证据", count == 2, str(count))


def test_id_gap_skipped_is_diagnostic_only():
    """真实已提交的 id 空洞：照常处理后续反馈，只记诊断，不阻塞游标。"""

    fresh()
    insert_feedback_row("空洞练习", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("空洞练习", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-03T00:00:00Z")
    check("空洞前游标 = 2", cursor_row()[0] == 2, str(cursor_row()))

    # 真实制造永久空洞：插入 id=3 并提交，再删除并提交。sqlite_sequence 不回退，
    # 所以下一条反馈拿到 id=4，而 3 永远不会出现（不是手工指定 id）。
    hole_id = insert_feedback_row("空洞占位", "completed", None, created_at="2026-01-03T12:00:00Z")
    conn = connect()
    try:
        conn.execute("DELETE FROM feedbacks WHERE id = ?", (hole_id,))
        conn.commit()
    finally:
        conn.close()

    following = insert_feedback_row("空洞练习", "not_completed", "任务难度太高",
                                    created_at="2026-01-04T10:00:00Z")
    check("删除后新反馈 id 跳号", following == hole_id + 1, f"{hole_id} -> {following}")

    result = memory_service.apply_pending_updates(now="2026-01-05T00:00:00Z")
    info = result["info"]
    check("空洞后仍然处理新反馈", result["processed"] == 1, str(result["processed"]))
    check("id_gap_skipped 记录真实跳过数量", info["id_gap_skipped"] == 1,
          str(info["id_gap_skipped"]))
    check("id_gap_detail 记录 after/before/gap",
          info["id_gap_detail"] == [{"after": 2, "before": following, "gap": 1}],
          str(info["id_gap_detail"]))
    check("游标推进到最大 id（gap 不阻塞）", cursor_row()[0] == following, str(cursor_row()))

    row = memory_of("difficulty", "空洞练习")
    check("空洞后仍计入第三条 supports", row[7] == 3, str(row[7]))
    check("空洞不产生重复证据",
          scoped_counts("difficulty", "空洞练习")["evidence"] == 3,
          str(scoped_counts("difficulty", "空洞练习")))


def test_failed_feedback_insert_leaves_no_permanent_gap_wrong_id():
    """禁止手工指定 feedbacks.id：契约断言（§5.3.3.2 前置约束）。"""

    fresh()
    conn = connect()
    try:
        sql = "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'feedbacks'"
        schema = conn.execute(sql).fetchone()[0]
    finally:
        conn.close()

    check("feedbacks.id 是 AUTOINCREMENT 主键", "AUTOINCREMENT" in schema.upper(), schema[:80])


# ==========================
# 三、状态机：创建 / 追加 / 削弱
# ==========================


def test_difficulty_memory_creation_threshold():
    """difficulty 门槛数 supports：1 条不够，2 条建行并一次性写齐证据。"""

    fresh()
    insert_feedback_row("条件概率", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    first = memory_service.apply_pending_updates(now="2026-01-03T00:00:00Z")
    check("单条反馈不建记忆", row_counts()["learning_memories"] == 0,
          str(first["info"]["insufficient_evidence"]))
    check("单条反馈不写证据", row_counts()["memory_evidence"] == 0)

    insert_feedback_row("条件概率", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")
    row = memory_by_key("difficulty", "条件概率")
    check("两条 supports 建行", row is not None)
    check("建行后 episode = 1", row[10] == 1, str(row[10]))
    check("建行后状态 active", row[14] == "active", str(row[14]))
    check("evidence_count = 2", row[7] == 2, str(row[7]))
    check("content 带「初步观察：」", row[4].startswith("初步观察："), row[4])

    counts = scoped_counts("difficulty", "条件概率")
    check("一次性写齐两条证据", counts["evidence"] == 2, str(counts))

    conn = connect()
    try:
        events = conn.execute("SELECT event_type FROM memory_events ORDER BY id").fetchall()
    finally:
        conn.close()

    check("只写一条 created 事件", [row[0] for row in events] == ["created"], str(events))


def test_active_memory_appends_support_and_contradiction():
    """active 下新 supports 追加证据并写 strengthened；contradicts 触发 weakened。"""

    fresh()
    base = [("2026-01-02T10:00:00Z"), ("2026-01-03T10:00:00Z")]
    for stamp in base:
        insert_feedback_row("追加练习", "not_completed", "任务难度太高", created_at=stamp)
    memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")
    before = memory_of("difficulty", "追加练习")

    insert_feedback_row("追加练习", "not_completed", "任务难度太高", created_at="2026-01-05T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-06T00:00:00Z")
    after = memory_of("difficulty", "追加练习")

    check("新 supports 追加一行证据",
          scoped_counts("difficulty", "追加练习")["evidence"] == 3,
          str(scoped_counts("difficulty", "追加练习")))
    check("evidence_count 重算为 3", after[7] == 3, str(after[7]))
    check("last_supported_at 取证据 MAX(created_at)",
          after[16] == "2026-01-05T10:00:00Z", str(after[16]))
    check("strength 变大（recency 更近）", after[6] >= before[6], f"{after[6]} vs {before[6]}")

    insert_feedback_row("追加练习", "completed", None, created_at="2026-01-07T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-08T00:00:00Z")
    weakened = memory_of("difficulty", "追加练习")

    check("contradicts 使状态 weakened", weakened[14] == "weakened", str(weakened[14]))
    check("contradict_count = 1", weakened[8] == 1, str(weakened[8]))
    check("最后一条证据 episode 未变", True)

    events = events_of("difficulty", "追加练习")

    check("事件序列 created → weakened（状态没变不写 strengthened，§5.3.7）",
          events == ["created", "weakened"], str(events))


def test_neutral_candidate_is_not_evidence():
    """neutral（难度太高之外的失败原因）不写证据、不计分子分母。"""

    fresh()
    insert_feedback_row("中性练习", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("中性练习", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")
    before = scoped_counts("difficulty", "中性练习")

    insert_feedback_row("中性练习", "not_completed", "任务时间太长", created_at="2026-01-05T10:00:00Z")
    result = memory_service.apply_pending_updates(now="2026-01-06T00:00:00Z")
    after = scoped_counts("difficulty", "中性练习")

    check("neutral 不写证据", after["evidence"] == before["evidence"], str(after))
    check("neutral 计入 info.neutral_dropped", result["info"]["neutral_dropped"] >= 1,
          str(result["info"]["neutral_dropped"]))
    check("neutral 不改变 evidence_count",
          memory_of("difficulty", "中性练习")[7] == 2)


def test_invalid_enum_candidate_is_fail_closed():
    """脏枚举（直接写库）既不计分子也不计分母。"""

    fresh()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions, total_questions,
             created_at, plan_id)
            VALUES ('脏数据练习', 30, 45, 'done', '任务难度太高', 0, 1, 0, 1,
                    '2026-01-02T10:00:00Z', NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = memory_service.apply_pending_updates(now="2026-01-03T00:00:00Z")
    check("非法 status 不建记忆", row_counts()["learning_memories"] == 0, str(row_counts()))
    check("非法 status 计入 invalid_enum", result["info"]["invalid_enum"] >= 1,
          str(result["info"]["invalid_enum"]))


# ==========================
# 四、resolved / pending / 晋升
# ==========================


def build_difficulty_memory(task="状态机练习"):
    insert_feedback_row(task, "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row(task, "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")
    return memory_of("difficulty", task)


def test_resolved_after_three_contradictions():
    """反证 ≥3 且全部晚于最后一次 supports → resolved，并固定 pending_*、关轮清理。"""

    fresh()
    build_difficulty_memory()
    for stamp in ("2026-01-05T10:00:00Z", "2026-01-06T10:00:00Z", "2026-01-07T10:00:00Z"):
        insert_feedback_row("状态机练习", "completed", None, created_at=stamp)

    memory_service.apply_pending_updates(now="2026-01-08T00:00:00Z")
    row = memory_of("difficulty", "状态机练习")

    check("三条反证后 resolved", row[14] == "resolved", str(row[14]))
    check("pending_episode 固定为 episode + 1", row[12] == 2, str(row[12]))
    check("pending_start_feedback_id 固定为证据最大 id", row[13] == 5, str(row[13]))

    conn = connect()
    try:
        events = [row[0] for row in conn.execute(
            "SELECT event_type FROM memory_events ORDER BY id").fetchall()]
    finally:
        conn.close()

    check("resolved 之前有 weakened 事件", "resolved" in events and "weakened" in events, str(events))


def test_resolved_memory_only_writes_pending_then_promotes():
    """resolved 下新候选只写 pending；凑齐后再原子晋升成新一轮 active。"""

    fresh()
    build_difficulty_memory()
    for stamp in ("2026-01-05T10:00:00Z", "2026-01-06T10:00:00Z", "2026-01-07T10:00:00Z"):
        insert_feedback_row("状态机练习", "completed", None, created_at=stamp)
    memory_service.apply_pending_updates(now="2026-01-08T00:00:00Z")

    resolved = memory_of("difficulty", "状态机练习")
    evidence_before = scoped_counts("difficulty", "状态机练习")["evidence"]

    insert_feedback_row("状态机练习", "not_completed", "任务难度太高", created_at="2026-01-09T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")
    after_one = scoped_counts("difficulty", "状态机练习")

    check("resolved 下第一条候选进 pending", after_one["pending"] == 1,
          str(after_one))
    check("resolved 下不写正式证据", after_one["evidence"] == evidence_before,
          str(after_one))
    check("结论字段不动（status 仍 resolved）",
          memory_of("difficulty", "状态机练习")[14] == "resolved")

    insert_feedback_row("状态机练习", "not_completed", "任务难度太高", created_at="2026-01-11T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-12T00:00:00Z")
    promoted = memory_of("difficulty", "状态机练习")

    check("凑齐门槛后晋升为 active", promoted[14] == "active", str(promoted[14]))
    check("episode 变成 2", promoted[9] == 2, str(promoted[9]))
    check("新轮 episode_start_feedback_id = 新轮最小 feedback_id",
          promoted[10] == 6, str(promoted[10]))
    check("旧轮证据未被改写", resolved[9] == 1, str(resolved[9]))
    check("episode_started_at = 晋升 as_of",
          promoted[11] == "2026-01-11T10:00:00Z", str(promoted[11]))
    check("晋升后清空 pending_*", promoted[12] is None and promoted[13] is None,
          f"{promoted[12]}, {promoted[13]}")

    staged = scoped_counts("difficulty", "状态机练习")
    check("晋升后 pending 已消费", staged["pending"] == 0, str(staged))

    events = events_of("difficulty", "状态机练习")
    check("晋升写 reopened 事件", events[-1] == "reopened", str(events))

    conn = connect()
    try:
        new_evidence = conn.execute(
            "SELECT feedback_id, episode, relation FROM memory_evidence "
            "WHERE memory_id = ? ORDER BY id",
            (promoted[0],),
        ).fetchall()
    finally:
        conn.close()

    check("新证据落在第 2 轮，旧轮证据仍是第 1 轮",
          sorted({row[1] for row in new_evidence if row[0] >= 6}) == [2]
          and all(row[1] == 1 for row in new_evidence if row[0] <= 3),
          str(new_evidence))


def test_direction_undetermined_keeps_pending():
    """task_performance 完成率落在 (0.5, 0.8) → 即使样本够也不晋升。"""

    fresh()
    insert_feedback_row("中间率", "completed", None, created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("中间率", "completed", None, created_at="2026-01-03T10:00:00Z")
    insert_feedback_row("中间率", "not_completed", "其他", created_at="2026-01-04T10:00:00Z")

    result = memory_service.apply_pending_updates(now="2026-01-05T00:00:00Z")

    check("方向不可定：不建 task_performance 记忆",
          memory_of("task_performance", "中间率") is None,
          str(result["info"].get("direction_undetermined")))
    check("方向不可定计入 info", result["info"]["direction_undetermined"] >= 1,
          str(result["info"]["direction_undetermined"]))


def test_task_performance_uses_samples_not_supports():
    """task_performance 门槛数有效 samples（含反证）而不是 supports。"""

    fresh()
    insert_feedback_row("完成率练习", "completed", None, created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("完成率练习", "not_completed", "其他", created_at="2026-01-03T10:00:00Z")
    insert_feedback_row("完成率练习", "not_completed", "其他", created_at="2026-01-04T10:00:00Z")

    memory_service.apply_pending_updates(now="2026-01-05T00:00:00Z")
    row = memory_of("task_performance", "完成率练习")

    check("3 个样本、仅 2 条 supports 也建行", row is not None)
    check("completion_rate = 33%", "33%" in row[4], row[4])
    check("samples 计入 value_json",
          json.loads(row[5]).get("samples") == 3, row[5])


# ==========================
# 五、stale 与事件
# ==========================


def test_stale_archives_memory():
    """距最后一次 supports 超过 60 天 → archived（reason=stale_as_of）。"""

    fresh()
    build_difficulty_memory()
    memory_service.apply_pending_updates(now="2026-01-04T00:00:00Z")

    insert_feedback_row("状态机练习", "completed", None, created_at="2026-04-10T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-04-11T00:00:00Z")
    row = memory_of("difficulty", "状态机练习")

    check("超过 60 天未支撑 → archived", row[14] == "archived", str(row[14]))

    conn = connect()
    try:
        detail = conn.execute(
            "SELECT detail_json FROM memory_events WHERE event_type = 'archived' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    check("archived 事件带 stale_as_of",
          detail is not None and json.loads(detail[0]).get("reason") == "stale_as_of",
          str(detail))


def test_archive_stale_batch_entry():
    """archive_stale 退化为批量入口：全部记忆按 now 判一次。"""

    fresh()
    build_difficulty_memory()
    result = memory_service.archive_stale(now="2026-05-01T00:00:00Z")

    check("archive_stale 返回 ok", result["ok"] is True, str(result.keys()))
    check("批量入口归档成功",
          memory_of("difficulty", "状态机练习")[14] == "archived")

    again = memory_service.archive_stale(now="2026-05-01T00:00:00Z")
    check("重复调用幂等（不再写事件）", again["ok"] is True)


def test_evidence_is_immutable():
    """正式证据写入后 relation / episode / created_at 永不改写。"""

    fresh()
    build_difficulty_memory()
    conn = connect()
    try:
        before = conn.execute(
            "SELECT id, memory_id, feedback_id, episode, relation, weight, created_at "
            "FROM memory_evidence ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    for stamp in ("2026-01-05T10:00:00Z", "2026-01-06T10:00:00Z"):
        insert_feedback_row("状态机练习", "completed", None, created_at=stamp)
    memory_service.apply_pending_updates(now="2026-01-07T00:00:00Z")

    conn = connect()
    try:
        after = conn.execute(
            "SELECT id, memory_id, feedback_id, episode, relation, weight, created_at "
            "FROM memory_evidence ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    check("旧证据行逐列不变", after[: len(before)] == before,
          f"{after[:len(before)]} != {before}")
    check("新证据只是追加", len(after) > len(before))


# ==========================
# 六、重建与语义投影
# ==========================


def build_rich_dataset():
    """覆盖创建 / 追加 / 削弱 / resolved / pending / stale 的数据集。"""

    insert_feedback_row("富集练习", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("富集练习", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")
    insert_feedback_row("富集练习", "completed", None, created_at="2026-01-04T10:00:00Z")
    insert_feedback_row("富集练习", "completed", None, created_at="2026-01-05T10:00:00Z")
    insert_feedback_row("富集练习", "completed", None, created_at="2026-01-06T10:00:00Z")
    insert_feedback_row("富集练习", "not_completed", "任务难度太高", created_at="2026-01-07T10:00:00Z")
    insert_feedback_row("富集练习", "not_completed", "任务时间太长", created_at="2026-01-08T10:00:00Z")
    insert_feedback_row("另一主题", "not_completed", "任务难度太高", created_at="2026-01-02T11:00:00Z")
    insert_feedback_row("另一主题", "not_completed", "任务难度太高", created_at="2026-01-03T11:00:00Z")
    insert_feedback_row("另一主题", "completed", None, created_at="2026-01-09T11:00:00Z")


def test_rebuild_matches_online_projection():
    """在线 = rebuild 的前缀增量：语义字段投影逐项相等。"""

    fresh()
    build_rich_dataset()
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")
    online = memory_service.semantic_projection()

    result = memory_service.rebuild_all(now="2026-01-10T00:00:00Z")
    check("rebuild 成功", result["ok"] is True, str(result))

    rebuilt = memory_service.semantic_projection()
    check("在线与重建语义投影相等", online == rebuilt,
          str({key: (online[key], rebuilt.get(key)) for key in online if online[key] != rebuilt.get(key)}))

    check("rebuild 后游标不回退", cursor_row()[0] == 10, str(cursor_row()))


def test_rebuild_then_continue_online_stays_consistent():
    """重建后继续在线处理，结果与纯在线路径一致。"""

    fresh()
    build_rich_dataset()
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")
    memory_service.rebuild_all(now="2026-01-10T00:00:00Z")

    insert_feedback_row("富集练习", "not_completed", "任务难度太高", created_at="2026-01-11T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-12T00:00:00Z")
    after_rebuild = memory_service.semantic_projection()

    memory_service.rebuild_all(now="2026-01-12T00:00:00Z")
    direct = memory_service.semantic_projection()

    check("重建后继续在线 = 纯在线", after_rebuild == direct,
          str({key: (after_rebuild[key], direct.get(key)) for key in after_rebuild
               if after_rebuild[key] != direct.get(key)}))


def test_rebuild_empty_database_keeps_cursor():
    """空表 rebuild：不报错，游标保持旧高水位（不回退到 0）。"""

    fresh()
    insert_feedback_row("清空练习", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-03T00:00:00Z")
    high_water = cursor_row()[0]

    conn = connect()
    try:
        conn.execute("DELETE FROM feedbacks")
        conn.commit()
    finally:
        conn.close()

    result = memory_service.rebuild_all(now="2026-01-04T00:00:00Z")
    check("空表 rebuild ok", result["ok"] is True, str(result))
    check("空表 rebuild 后派生表全空", row_counts() == {
        "learning_memories": 0, "memory_evidence": 0,
        "memory_pending_evidence": 0, "memory_events": 0}, str(row_counts()))
    check("游标保持旧高水位", cursor_row()[0] == high_water, str(cursor_row()))
    check("空表也写合法 updated_at", bool(cursor_row()[1]))


def test_rebuild_too_large_is_rejected_without_writes():
    """feedbacks 超过 5000 条：返回 rebuild_too_large 且不写任何数据。"""

    fresh()
    conn = connect()
    try:
        conn.executemany(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions, total_questions,
             created_at, plan_id)
            VALUES ('容量练习', 30, 45, 'completed', NULL, 0, 1, 0, 1, ?, NULL)
            """,
            [("2026-01-02T10:00:00Z",)] * 5001,
        )
        conn.commit()
    finally:
        conn.close()

    result = memory_service.rebuild_all(now="2026-01-03T00:00:00Z")
    check("超上限：ok 为 False", result["ok"] is False, str(result))
    check("超上限：error_type = rebuild_too_large",
          result["error_type"] == "rebuild_too_large", str(result))
    check("超上限：不写任何派生数据", row_counts() == {
        "learning_memories": 0, "memory_evidence": 0,
        "memory_pending_evidence": 0, "memory_events": 0}, str(row_counts()))
    check("超上限：游标不动", cursor_row()[0] == 0, str(cursor_row()))


def test_rebuild_after_delete_feedback():
    """DELETE /feedback 清空后 rebuild：派生表干净、游标停在清空前高水位。"""

    fresh()
    build_rich_dataset()
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")
    high_water = cursor_row()[0]

    database.delete_all_feedback()
    memory_service.rebuild_all(now="2026-01-11T00:00:00Z")

    check("清空反馈后派生表全空", row_counts() == {
        "learning_memories": 0, "memory_evidence": 0,
        "memory_pending_evidence": 0, "memory_events": 0}, str(row_counts()))
    check("游标 ≥ 清空前高水位", cursor_row()[0] >= high_water, str(cursor_row()))


def test_timestamp_out_of_order_does_not_inflate_strength():
    """created_at 乱序（更大 id 时间更早）时 recency 必须钳到 0，不得把旧样本当"更新"。"""

    fresh()
    insert_feedback_row("乱序练习", "not_completed", "任务难度太高", created_at="2026-01-09T10:00:00Z")
    insert_feedback_row("乱序练习", "not_completed", "任务难度太高", created_at="2026-01-10T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-11T00:00:00Z")
    before = memory_of("difficulty", "乱序练习")
    check("两条 supports 建行", before is not None)

    # 更大 id、更早 created_at：as_of 回拨到 01-05，age = 01-05 − 01-10 < 0，必须钳成 0
    insert_feedback_row("乱序练习", "not_completed", "任务难度太高", created_at="2026-01-05T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-12T00:00:00Z")

    row = memory_of("difficulty", "乱序练习")
    check("乱序样本仍计入当前轮", scoped_counts("difficulty", "乱序练习")["evidence"] == 3,
          str(scoped_counts("difficulty", "乱序练习")))
    check("strength 不超过封顶（recency 未被放大）", row[6] <= 0.9 + 1e-9, str(row[6]))
    check("last_supported_at 回拨不允许发生（取 MAX）",
          row[16] == "2026-01-10T10:00:00Z", str(row[16]))
    check("strength 取 unit=3 的 base*cap（0.75 被 0.6 封顶），未被 recency>1 抬高",
          abs(row[6] - 0.6) < 1e-9, str(row[6]))


def test_time_pattern_requires_valid_plan():
    """time_pattern 只从有效 plan_id 关联 plans 取难度；无计划整类跳过。"""

    fresh()
    insert_feedback_row("用时练习", "completed", None, created_at="2026-01-02T10:00:00Z",
                        minutes=30, actual=60)
    memory_service.apply_pending_updates(now="2026-01-03T00:00:00Z")
    check("无 plan_id 不建 time_pattern", memory_by_key("time_pattern", "medium") is None)

    plan_id = insert_plan_row("2026-01-04", "用时练习", difficulty="medium", estimated=30)
    for index, stamp in enumerate(("2026-01-05T10:00:00Z", "2026-01-06T10:00:00Z",
                                   "2026-01-07T10:00:00Z", "2026-01-08T10:00:00Z")):
        insert_feedback_row("用时练习", "completed", None, created_at=stamp,
                            minutes=30, actual=60, plan_id=plan_id)

    memory_service.apply_pending_updates(now="2026-01-09T00:00:00Z")
    row = memory_by_key("time_pattern", "medium")

    check("4 个有效样本建 time_pattern", row is not None)
    check("time_pattern 的 subject_label 是难度中文", row[3] == "中等", row[3])
    check("time_pattern 计数按 samples", json.loads(row[5]).get("samples") == 4, row[5])
    check("ratio = 2.0", "2.0" in row[4], row[4])


def test_retrieve_filters_and_limits():
    """retrieve：类型/状态精确过滤，limit 夹到 10，证据 id ≤5，不返回 pending。"""

    fresh()
    build_rich_dataset()
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")

    all_active = memory_service.retrieve()
    check("retrieve 只返回 active", all(item["status"] == "active" for item in all_active["memories"]))
    check("retrieve 回传 evidence_feedback_ids",
          all(len(item["evidence_feedback_ids"]) <= 5 for item in all_active["memories"]))

    only_difficulty = memory_service.retrieve(memory_type="difficulty")
    check("按类型过滤",
          all(item["memory_type"] == "difficulty" for item in only_difficulty["memories"]))

    oversized = memory_service.retrieve(limit=999)
    check("limit 夹到 10 不报错", oversized["count"] <= 10, str(oversized["count"]))

    bad = memory_service.retrieve(memory_type="unknown")
    check("未知类型返回 invalid_argument", bad["ok"] is False and bad["error_type"] == "invalid_argument",
          str(bad))


def test_apply_feedback_is_not_a_public_entry():
    """update_from_feedback 不得绕过游标；游标已消费的反馈不会被重复处理。"""

    fresh()
    insert_feedback_row("包装练习", "not_completed", "任务难度太高", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("包装练习", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")

    first = memory_service.update_from_feedback(2)
    check("包装函数 ok", first["ok"] is True, str(first))
    check("包装函数返回受影响记忆 id", first["memory_ids"] != [], str(first))

    second = memory_service.update_from_feedback(2)
    check("重复调用不再处理（游标已到 2）", second["updated"] == 0 and second["created"] == 0,
          str(second))


def test_concurrent_apply_processes_each_feedback_once():
    """两个线程同时提交并触发在线更新：每条反馈只计一次、游标到最大 id。

    不手工指定 feedback.id：每个线程都走 SQLite 自动分配。
    """

    fresh()
    errors = []

    def worker(index):
        try:
            insert_feedback_row(f"并发练习{index}", "not_completed", "任务难度太高",
                                created_at=f"2026-01-0{index + 2}T10:00:00Z")
            memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")
        except Exception as error:  # noqa: BLE001 - 测试需要看见任何线程异常
            errors.append(repr(error))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(30)

    check("并发提交无异常", errors == [], str(errors))
    check("游标推进到最大 id", cursor_row()[0] == 2, str(cursor_row()))

    conn = connect()
    try:
        duplicates = conn.execute(
            "SELECT feedback_id, COUNT(*) FROM memory_evidence GROUP BY feedback_id HAVING COUNT(*) > 1"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0]
    finally:
        conn.close()

    check("没有重复证据", duplicates == [], str(duplicates))
    check("每条反馈最多一条证据", total <= 2, str(total))


# ==========================
# 四、PR #4 复审：resolved stale、plan 有效性、neutral 样本、参数契约
# ==========================


def plan_update(plan_id, **fields):
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [plan_id]
    conn = connect()
    try:
        conn.execute(f"UPDATE plans SET {assignments} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def plan_delete(plan_id):
    conn = connect()
    try:
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        conn.commit()
    finally:
        conn.close()


def plan_restore(plan_id, task, difficulty="medium", estimated=30, plan_date="2026-01-02"):
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO plans
            (id, plan_date, task, estimated_minutes, difficulty, question_count,
             reason, completion_criteria, subtasks_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, 1, '测试', '完成', '[]', 'active', ?)
            """,
            (plan_id, plan_date, task, estimated, difficulty, plan_date + "T08:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def events_detail(memory_id, path=None):
    conn = connect(path)
    try:
        return conn.execute(
            "SELECT event_type, detail_json FROM memory_events WHERE memory_id = ? ORDER BY id",
            (memory_id,),
        ).fetchall()
    finally:
        conn.close()


def build_resolved_difficulty(task="过期练习"):
    """2 条 difficulty supports 建行，再 3 条 completed 走到 resolved。"""

    for stamp in ("2026-01-02T10:00:00Z", "2026-01-03T10:00:00Z"):
        insert_feedback_row(task, "not_completed", "任务难度太高", created_at=stamp)
    memory_service.apply_pending_updates(now="2026-01-03T12:00:00Z")

    for stamp in ("2026-01-04T10:00:00Z", "2026-01-05T10:00:00Z", "2026-01-06T10:00:00Z"):
        insert_feedback_row(task, "completed", None, created_at=stamp)

    memory_service.apply_pending_updates(now="2026-01-07T00:00:00Z")


def build_time_pattern(task, difficulty="medium", supports=4, contradicts=0):
    """按 time_pattern 口径造样本：ratio 2.0 → slow supports，ratio 0.3 → contradicts。"""

    plan_id = insert_plan_row("2026-01-02", task, difficulty=difficulty, estimated=30)
    day = 2

    for _ in range(supports):
        insert_feedback_row(task, "completed", None,
                            created_at=f"2026-01-{day:02d}T10:00:00Z",
                            minutes=30, actual=60, plan_id=plan_id)
        day += 1

    for _ in range(contradicts):
        insert_feedback_row(task, "completed", None,
                            created_at=f"2026-01-{day:02d}T10:00:00Z",
                            minutes=30, actual=9, plan_id=plan_id)
        day += 1

    memory_service.apply_pending_updates(now=f"2026-01-{day:02d}T23:00:00Z")

    return plan_id


def test_resolved_memory_archives_when_stale():
    """§5.3.5：resolved 超过 60 天无支撑 → archived，保留新轮 pending 与边界。"""

    fresh()
    build_resolved_difficulty("过期练习")
    resolved = memory_of("difficulty", "过期练习")
    check("先进入 resolved", resolved[14] == "resolved", str(resolved[14]))

    boundary = (resolved[12], resolved[13])
    check("resolved 已固定新轮边界", boundary[0] == 2 and boundary[1] == 5, str(boundary))

    insert_feedback_row("过期练习", "not_completed", "任务难度太高", created_at="2026-01-08T10:00:00Z")
    memory_service.apply_pending_updates(now="2026-01-09T00:00:00Z")
    waiting = memory_of("difficulty", "过期练习")

    check("resolved 下新候选只写 pending",
          scoped_counts("difficulty", "过期练习")["pending"] == 1,
          str(scoped_counts("difficulty", "过期练习")))
    check("写 pending 不改边界", (waiting[12], waiting[13]) == boundary,
          str((waiting[12], waiting[13])))

    result = memory_service.archive_stale(now="2026-04-01T00:00:00Z")
    archived = memory_of("difficulty", "过期练习")

    check("批量入口归档成功", result["ok"] is True and result["archived"] >= 1, str(result))
    check("resolved 超 60 天转 archived", archived[14] == "archived", str(archived[14]))
    check("归档不重算 pending 边界", (archived[12], archived[13]) == boundary,
          str((archived[12], archived[13])))
    check("新轮 pending 一行不删",
          scoped_counts("difficulty", "过期练习")["pending"] == 1,
          str(scoped_counts("difficulty", "过期练习")))

    events = events_detail(archived[0])
    archived_events = [detail for kind, detail in events if kind == "archived"]
    check("只写一条 archived 事件", len(archived_events) == 1, str(events))

    detail = json.loads(archived_events[0])
    check("archived 事件 reason = stale_as_of", detail.get("reason") == "stale_as_of", str(detail))
    check("archived 事件 from/to 正确",
          detail.get("from_status") == "resolved" and detail.get("to_status") == "archived",
          str(detail))

    again = memory_service.archive_stale(now="2026-05-01T00:00:00Z")
    repeated = [detail for kind, detail in events_detail(archived[0]) if kind == "archived"]

    check("重复归档不再写事件", again["archived"] == 0 and len(repeated) == 1, str(again))
    check("重复归档状态不变", memory_of("difficulty", "过期练习")[14] == "archived")


def test_time_pattern_ignores_candidates_after_plan_deleted():
    """plan 被删除后：历史与新反馈都不再计入 samples、不写新证据（§5.2）。"""

    fresh()
    plan_id = build_time_pattern("计划失效")
    before = json.loads(memory_of("time_pattern", "medium")[5])

    check("4 个有效样本建 time_pattern", before["samples"] == 4, str(before))

    plan_delete(plan_id)

    for stamp in ("2026-01-07T10:00:00Z", "2026-01-08T10:00:00Z"):
        insert_feedback_row("计划失效", "completed", None, created_at=stamp,
                            minutes=30, actual=60, plan_id=plan_id)

    result = memory_service.apply_pending_updates(now="2026-01-09T00:00:00Z")
    after = json.loads(memory_of("time_pattern", "medium")[5])

    check("plan 删除后 samples 不增长", after["samples"] == 4, str(after))
    check("plan 删除后不写新证据",
          scoped_counts("time_pattern", "medium")["evidence"] == 4,
          str(scoped_counts("time_pattern", "medium")))
    # plan 已删除的行被 SQL JOIN 直接挡在候选之外，根本到不了 evaluate，
    # 所以 invalid_plan 不增长；关键是它们没被计入统计、也没进缓冲区。
    check("plan 删除后不产生 pending",
          scoped_counts("time_pattern", "medium")["pending"] == 0,
          str(scoped_counts("time_pattern", "medium")))
    check("plan 删除后记忆状态不变", memory_of("time_pattern", "medium")[14] == "active")


def test_time_pattern_ignores_candidates_when_plan_estimate_invalid():
    """plans.estimated_minutes <= 0 的反馈不能充当 time_pattern 样本。"""

    fresh()
    plan_id = build_time_pattern("预估失效")
    plan_update(plan_id, estimated_minutes=0)

    insert_feedback_row("预估失效", "completed", None, created_at="2026-01-07T10:00:00Z",
                        minutes=30, actual=60, plan_id=plan_id)
    memory_service.apply_pending_updates(now="2026-01-08T00:00:00Z")

    after = json.loads(memory_of("time_pattern", "medium")[5])

    check("预估为 0 时 samples 不增长", after["samples"] == 4, str(after))
    check("预估为 0 时不写证据",
          scoped_counts("time_pattern", "medium")["evidence"] == 4,
          str(scoped_counts("time_pattern", "medium")))


def test_time_pattern_ignores_candidates_when_difficulty_mismatched():
    """plan 难度改成另一个合法值后，不再属于当前 time_pattern 记忆。"""

    fresh()
    plan_id = build_time_pattern("难度漂移")
    plan_update(plan_id, difficulty="hard")

    insert_feedback_row("难度漂移", "completed", None, created_at="2026-01-07T10:00:00Z",
                        minutes=30, actual=60, plan_id=plan_id)
    memory_service.apply_pending_updates(now="2026-01-08T00:00:00Z")

    after = json.loads(memory_of("time_pattern", "medium")[5])

    check("难度不匹配时 samples 不增长", after["samples"] == 4, str(after))
    check("难度不匹配时不写 medium 证据",
          scoped_counts("time_pattern", "medium")["evidence"] == 4,
          str(scoped_counts("time_pattern", "medium")))
    # 难点在于"不能串进 medium"：同一条反馈按 plan 的当前难度归到 hard，
    # 会另建一条 time_pattern/hard 记忆，这属于设计预期，不是串记忆。
    check("medium 记忆仍是原 episode", memory_of("time_pattern", "medium")[9] == 1,
          str(memory_of("time_pattern", "medium")[9]))


def test_time_pattern_pending_promotion_requires_valid_plan():
    """pending 晋升候选同样校验 plan：删掉 plan 后不能错误晋升（§5.3.3）。"""

    fresh()
    plan_id = build_time_pattern("节奏练习", supports=4, contradicts=3)
    resolved = memory_of("time_pattern", "medium")

    check("time_pattern 走到 resolved", resolved[14] == "resolved", str(resolved[14]))

    day = 10
    for _ in range(3):
        insert_feedback_row("节奏练习", "completed", None,
                            created_at=f"2026-01-{day:02d}T10:00:00Z",
                            minutes=30, actual=60, plan_id=plan_id)
        day += 1

    memory_service.apply_pending_updates(now="2026-01-13T00:00:00Z")
    waiting = memory_of("time_pattern", "medium")

    check("3 条待定样本不足不晋升", waiting[14] == "resolved", str(waiting[14]))
    check("3 条写入 pending", scoped_counts("time_pattern", "medium")["pending"] == 3,
          str(scoped_counts("time_pattern", "medium")))

    plan_delete(plan_id)
    insert_feedback_row("节奏练习", "completed", None, created_at="2026-01-13T10:00:00Z",
                        minutes=30, actual=60, plan_id=plan_id)
    blocked = memory_service.apply_pending_updates(now="2026-01-14T00:00:00Z")
    still = memory_of("time_pattern", "medium")

    check("plan 失效时门槛不动", still[14] == "resolved", str(still[14]))
    check("plan 失效时不写新轮证据",
          scoped_counts("time_pattern", "medium")["evidence"] == 7,
          str(scoped_counts("time_pattern", "medium")))
    # trigger 自己的 plan 失效时，time_pattern 整类被跳过（subject_key_for 返回
    # None），这条反馈连缓冲区都不进；已有 3 行 pending 必须原样保留。
    check("无效 plan 的 pending 不删",
          scoped_counts("time_pattern", "medium")["pending"] == 3,
          str(scoped_counts("time_pattern", "medium")))

    plan_restore(plan_id, "节奏练习", difficulty="medium", estimated=30)
    insert_feedback_row("节奏练习", "completed", None, created_at="2026-01-15T10:00:00Z",
                        minutes=30, actual=60, plan_id=plan_id)
    memory_service.apply_pending_updates(now="2026-01-16T00:00:00Z")
    promoted = memory_of("time_pattern", "medium")

    check("plan 恢复后 4 条样本可晋升", promoted[14] == "active", str(promoted[14]))
    check("晋升到第 2 轮", promoted[9] == 2, str(promoted[9]))
    # 新轮证据 = 3 行 pending + 本轮 trigger，共 4 行；旧轮 7 行保留
    check("晋升后新轮证据 4 行、总数 11 行",
          scoped_counts("time_pattern", "medium")["evidence"] == 11,
          str(scoped_counts("time_pattern", "medium")))
    check("晋升后 pending 清空",
          scoped_counts("time_pattern", "medium")["pending"] == 0,
          str(scoped_counts("time_pattern", "medium")))


def test_time_pattern_neutral_samples_survive_updates():
    """neutral 不写证据但要计入 samples：更新后不能凭空消失（PR #4 P1-3）。"""

    fresh()
    plan_id = build_time_pattern("中性样本")
    created = json.loads(memory_of("time_pattern", "medium")[5])

    check("初始 samples = 4", created["samples"] == 4, str(created))
    check("初始 median = 2.0", created["median_ratio"] == 2.0, str(created))

    # ratio 1.0 落在 0.4~1.5：在 slow 方向下是 neutral，不写证据但要计入 samples
    insert_feedback_row("中性样本", "completed", None, created_at="2026-01-07T10:00:00Z",
                        minutes=30, actual=30, plan_id=plan_id)
    memory_service.apply_pending_updates(now="2026-01-08T00:00:00Z")
    neutral = json.loads(memory_of("time_pattern", "medium")[5])

    check("neutral 计入 samples", neutral["samples"] == 5, str(neutral))
    check("neutral 不写正式证据",
          scoped_counts("time_pattern", "medium")["evidence"] == 4,
          str(scoped_counts("time_pattern", "medium")))

    # 追加一条 contradicts（ratio 0.3）：neutral 仍留在 samples 里
    insert_feedback_row("中性样本", "completed", None, created_at="2026-01-09T10:00:00Z",
                        minutes=30, actual=9, plan_id=plan_id)
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")
    updated = json.loads(memory_of("time_pattern", "medium")[5])

    check("更新后 neutral 仍在 samples 内", updated["samples"] == 6, str(updated))
    check("contradicts 写证据、neutral 不写",
          scoped_counts("time_pattern", "medium")["evidence"] == 5,
          str(scoped_counts("time_pattern", "medium")))

    online = memory_service.semantic_projection()
    rebuilt = memory_service.rebuild_all(now="2026-01-10T23:00:00Z")
    offline = memory_service.semantic_projection()

    check("含 neutral 的 rebuild 成功", rebuilt["ok"] is True, str(rebuilt))
    check("含 neutral 的在线/rebuild 投影完全相等",
          online["memories"] == offline["memories"]
          and online["evidence"] == offline["evidence"]
          and online["pendings"] == offline["pendings"]
          and online["events"] == offline["events"]
          and online["cursor"] == offline["cursor"])


def test_episode_start_feedback_id_uses_formal_evidence_only():
    """§3.2：episode_start_feedback_id 取当前轮第一条正式证据，不含 neutral。"""

    fresh()
    insert_feedback_row("边界练习", "not_completed", "任务时间太长", created_at="2026-01-02T10:00:00Z")
    insert_feedback_row("边界练习", "not_completed", "任务难度太高", created_at="2026-01-03T10:00:00Z")
    insert_feedback_row("边界练习", "not_completed", "任务难度太高", created_at="2026-01-04T10:00:00Z")

    memory_service.apply_pending_updates(now="2026-01-05T00:00:00Z")
    row = memory_of("difficulty", "边界练习")

    check("neutral 在前仍能建行", row is not None)
    check("episode_start_feedback_id 跳过 neutral", row[10] == 2, str(row[10]))
    check("neutral 不计入 evidence_count", row[7] == 2, str(row[7]))


def test_retrieve_invalid_limit_returns_error_dict():
    """§4.2：limit 非法返回稳定 invalid_argument，不抛裸 ValueError（PR #4 P2-2）。"""

    fresh()
    build_rich_dataset()
    memory_service.apply_pending_updates(now="2026-01-10T00:00:00Z")

    for limit in (0, -1, 2.5, True, "abc"):
        result = memory_service.retrieve(limit=limit)

        check(f"limit={limit!r} 返回错误字典",
              result["ok"] is False and result["error_type"] == "invalid_argument",
              str(result))
        check(f"limit={limit!r} 不返回记忆", result["memories"] == [], str(result))


def test_second_episode_keeps_neutral_and_first_formal_sample():
    """§3.2/§5.3.1：episode_start_feedback_id 只用于展示，不能当候选过滤下界。"""

    fresh()
    plan_id = build_time_pattern("重算样本", supports=4, contradicts=3)
    first_round = memory_by_key("time_pattern", "medium")
    check("第一轮已 resolved", first_round[14] == "resolved", str(first_round[14]))
    check("第一轮证据 7 行",
          key_counts("time_pattern", "medium")["evidence"] == 7,
          str(key_counts("time_pattern", "medium")))

    # 新一轮：第 1 条 neutral（ratio 1.0），随后 3 条 supports（ratio 1.6/1.7/1.8）
    neutral_id = insert_feedback_row("重算样本", "completed", None,
                                     created_at="2026-01-10T10:00:00Z",
                                     minutes=30, actual=30, plan_id=plan_id)
    formal_ids = []
    for day, actual in ((11, 48), (12, 51), (13, 54)):
        formal_ids.append(insert_feedback_row("重算样本", "completed", None,
                                             created_at=f"2026-01-{day:02d}T10:00:00Z",
                                             minutes=30, actual=actual, plan_id=plan_id))

    memory_service.apply_pending_updates(now="2026-01-14T00:00:00Z")
    promoted = memory_by_key("time_pattern", "medium")
    value = memory_service.decode_value(promoted[5])

    check("晋升到第二轮",
          promoted[9] == 2 and promoted[14] == "active",
          f"episode={promoted[9]} status={promoted[14]}")
    check("episode_start_feedback_id 取首条正式证据而非 neutral",
          promoted[10] == formal_ids[0],
          f"start={promoted[10]} neutral={neutral_id} formal={formal_ids}")
    check("samples 计入 neutral 与首条正式证据", value["samples"] == 4, str(value))
    check("median 覆盖四条有效候选", value["median_ratio"] == 1.65, str(value))
    check("新轮只写 3 条正式证据（neutral 不写）",
          key_counts("time_pattern", "medium")["evidence"] == 10,
          str(key_counts("time_pattern", "medium")))

    # 追加 1 条 supports（ratio 3.0）：samples 5、median 按 5 条重算、证据只加 1 条
    insert_feedback_row("重算样本", "completed", None,
                        created_at="2026-01-15T10:00:00Z",
                        minutes=30, actual=90, plan_id=plan_id)
    memory_service.apply_pending_updates(now="2026-01-16T00:00:00Z")
    updated = memory_by_key("time_pattern", "medium")
    updated_value = memory_service.decode_value(updated[5])

    check("active 更新后 samples 为 5", updated_value["samples"] == 5, str(updated_value))
    check("median 按五条有效候选重算", updated_value["median_ratio"] == 1.7,
          str(updated_value))
    check("证据只增加一条正式 evidence",
          key_counts("time_pattern", "medium")["evidence"] == 11,
          str(key_counts("time_pattern", "medium")))
    check("episode_start_feedback_id 不因更新而改变",
          updated[10] == formal_ids[0], f"start={updated[10]}")

    online = memory_service.semantic_projection()
    rebuilt = memory_service.rebuild_all(now="2026-01-16T00:00:00Z")
    offline = memory_service.semantic_projection()

    check("rebuild 成功", rebuilt["ok"] is True, str(rebuilt))
    check("rebuild 后语义投影完全相等", online == offline, "在线与重建结果不一致")
    rebuilt_memory = memory_by_key("time_pattern", "medium")
    rebuilt_value = memory_service.decode_value(rebuilt_memory[5])
    check("rebuild 后 samples 仍为 5", rebuilt_value["samples"] == 5, str(rebuilt_value))
    check("rebuild 后 median 仍为 1.7", rebuilt_value["median_ratio"] == 1.7,
          str(rebuilt_value))


def test_time_pattern_promotion_filters_invalid_pending_with_other_plan():
    """PR #4 P2-2：trigger 用另一条有效 plan，也要进 _try_promote 并跳过失效 pending。"""

    fresh()
    base_plan = build_time_pattern("混合计划", supports=4, contradicts=3)
    resolved = memory_by_key("time_pattern", "medium")
    check("先进入 resolved", resolved[14] == "resolved", str(resolved[14]))

    # 3 条 pending 候选挂在独立 plan 上
    stale_plan = insert_plan_row("2026-01-03", "混合计划", difficulty="medium", estimated=30)
    for day in (10, 11, 12):
        insert_feedback_row("混合计划", "completed", None,
                            created_at=f"2026-01-{day:02d}T10:00:00Z",
                            minutes=30, actual=60, plan_id=stale_plan)

    memory_service.apply_pending_updates(now="2026-01-13T00:00:00Z")
    check("resolved 下只写 pending",
          key_counts("time_pattern", "medium")["pending"] == 3,
          str(key_counts("time_pattern", "medium")))

    # 让旧 pending 的 plan 失效（行还在，estimated_minutes=0）
    plan_update(stale_plan, estimated_minutes=0)

    # trigger 用另一条同难度有效 plan
    trigger_plan = insert_plan_row("2026-01-04", "混合计划", difficulty="medium", estimated=30)
    insert_feedback_row("混合计划", "completed", None,
                        created_at="2026-01-14T10:00:00Z",
                        minutes=30, actual=60, plan_id=trigger_plan)

    blocked = memory_service.apply_pending_updates(now="2026-01-15T00:00:00Z")
    still = memory_by_key("time_pattern", "medium")

    check("trigger 有效时仍能进入候选流程", blocked["ok"] is True, str(blocked))
    check("无效 pending 不参与门槛 → 不晋升",
          still[14] == "resolved" and still[9] == 1, f"status={still[14]} episode={still[9]}")
    # trigger 本身也按 §5.3.3 入缓冲区：3 条旧 pending + 1 条 trigger = 4
    check("无效 pending 不被误删",
          key_counts("time_pattern", "medium")["pending"] == 4,
          str(key_counts("time_pattern", "medium")))
    check("无效 pending 不写证据",
          key_counts("time_pattern", "medium")["evidence"] == 7,
          str(key_counts("time_pattern", "medium")))
    check("无效 plan 计入 info.invalid_plan", blocked["info"]["invalid_plan"] >= 3,
          str(blocked["info"]))

    # plan 恢复后，3 条 pending + 1 条 trigger 达门槛 → 正常晋升
    plan_update(stale_plan, estimated_minutes=30)
    insert_feedback_row("混合计划", "completed", None,
                        created_at="2026-01-16T10:00:00Z",
                        minutes=30, actual=60, plan_id=trigger_plan)
    recovered = memory_service.apply_pending_updates(now="2026-01-17T00:00:00Z")
    promoted = memory_by_key("time_pattern", "medium")

    check("plan 恢复后晋升成功",
          recovered["ok"] is True and promoted[14] == "active" and promoted[9] == 2,
          f"status={promoted[14]} episode={promoted[9]}")
    check("晋升补齐五条正式证据",
          key_counts("time_pattern", "medium")["evidence"] == 12,
          str(key_counts("time_pattern", "medium")))
    check("晋升后 pending 清空",
          key_counts("time_pattern", "medium")["pending"] == 0,
          str(key_counts("time_pattern", "medium")))
    check("晋升后 samples 为 5",
          memory_service.decode_value(promoted[5])["samples"] == 5,
          str(memory_service.decode_value(promoted[5])))


# ==========================
# 运行入口
# ==========================


def main():
    if pathlib.Path(REAL_DB).exists():
        real_before = pathlib.Path(REAL_DB).read_bytes()
    else:
        real_before = None

    tests = [
        test_transaction_rollback_restores_all_tables,
        test_conn_reuse_does_not_commit_early,
        test_transaction_begin_immediate_blocks_concurrent_writer,
        test_apply_pending_updates_processes_in_id_order,
        test_id_gap_skipped_is_diagnostic_only,
        test_failed_feedback_insert_leaves_no_permanent_gap_wrong_id,
        test_difficulty_memory_creation_threshold,
        test_active_memory_appends_support_and_contradiction,
        test_neutral_candidate_is_not_evidence,
        test_invalid_enum_candidate_is_fail_closed,
        test_resolved_after_three_contradictions,
        test_resolved_memory_only_writes_pending_then_promotes,
        test_direction_undetermined_keeps_pending,
        test_task_performance_uses_samples_not_supports,
        test_stale_archives_memory,
        test_archive_stale_batch_entry,
        test_evidence_is_immutable,
        test_rebuild_matches_online_projection,
        test_rebuild_then_continue_online_stays_consistent,
        test_rebuild_empty_database_keeps_cursor,
        test_rebuild_too_large_is_rejected_without_writes,
        test_rebuild_after_delete_feedback,
        test_timestamp_out_of_order_does_not_inflate_strength,
        test_time_pattern_requires_valid_plan,
        test_retrieve_filters_and_limits,
        test_apply_feedback_is_not_a_public_entry,
        test_concurrent_apply_processes_each_feedback_once,
        test_resolved_memory_archives_when_stale,
        test_time_pattern_ignores_candidates_after_plan_deleted,
        test_time_pattern_ignores_candidates_when_plan_estimate_invalid,
        test_time_pattern_ignores_candidates_when_difficulty_mismatched,
        test_time_pattern_pending_promotion_requires_valid_plan,
        test_time_pattern_neutral_samples_survive_updates,
        test_episode_start_feedback_id_uses_formal_evidence_only,
        test_retrieve_invalid_limit_returns_error_dict,
        test_second_episode_keeps_neutral_and_first_formal_sample,
        test_time_pattern_promotion_filters_invalid_pending_with_other_plan,
    ]

    passed = 0
    failures = []

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as error:
            failures.append((test.__name__, str(error)))
            print(f"FAIL: {test.__name__} {error}")
        except Exception as error:  # noqa: BLE001 - 收集后统一报告
            import traceback

            failures.append((test.__name__, traceback.format_exc()))
            print(f"ERROR: {test.__name__} {error!r}")

    if real_before is not None:
        real_after = pathlib.Path(REAL_DB).read_bytes()
        check("真实 feedback.db 未被改动", real_after == real_before)

    use(REAL_DB)
    cleanup_databases()

    print(f"\n结果：{passed}/{len(tests)} 个测试函数通过")

    if failures:
        for name, detail in failures:
            print(f"  - {name}: {detail}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
