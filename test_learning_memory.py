"""第二阶段 Batch A 测试：V7 迁移、记忆 SQL 层、归一化与渲染。

覆盖设计文档 §8.2 的 M / K（游标部分）/ D / X / N / R 组，以及 Task 0 的服务端
枚举契约（V 组，走真实 HTTP）。所有夹具都用独立 _test_*.db，结束时删除并断言
真实 feedback.db 字节不变。
"""

import gc
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# 兜底：任何情况下都不让被测模块默认指向真实库
os.environ.setdefault(
    "FEEDBACK_DB_PATH",
    str(PROJECT_DIR / "_test_learning_memory_env.db"),
)
os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")
os.environ.setdefault("EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("OLLAMA_MODEL", "qwen3.5:4b")

import database  # noqa: E402  （必须在环境变量之后导入）
import memory_service  # noqa: E402

DB = PROJECT_DIR / "_test_learning_memory.db"
LEGACY_DB = PROJECT_DIR / "_test_learning_memory_legacy.db"
HIGH_WATER_DB = PROJECT_DIR / "_test_learning_memory_highwater.db"
REAL_DB = PROJECT_DIR / "feedback.db"

_real_db_bytes_before = REAL_DB.read_bytes() if REAL_DB.exists() else None


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def remove_file(path):
    for _ in range(20):
        if not path.exists():
            return
        gc.collect()
        try:
            path.unlink()
        except PermissionError:  # Windows 句柄释放延迟
            time.sleep(0.1)
    raise AssertionError(f"夹具文件无法删除（仍被占用）：{path}")


def use(path):
    """把 database 指向夹具库（不重新导入模块）。"""

    remove_file(path)
    database.DATABASE_PATH = path
    return path


def connect(path=None):
    return sqlite3.connect(path or database.DATABASE_PATH)


def table_names(path=None):
    with connect(path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}


def column_names(table, path=None):
    with connect(path) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def index_names(table, path=None):
    with connect(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def insert_feedback_row(task="条件概率练习", status="partial", reason="任务难度太高",
                        minutes=30, actual=50, created_at="2026-11-01T10:00:00",
                        plan_id=None):
    """直接写 feedbacks（绕过 API），返回 feedbacks.id。"""

    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO feedbacks
            (task, estimated_minutes, actual_minutes, status, reason,
             completed_subtasks, total_subtasks, completed_questions,
             total_questions, created_at, plan_id)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)
            """,
            (task, minutes, actual, status, reason, created_at, plan_id),
        )
        return cursor.lastrowid


def make_memory(**overrides):
    memory = {
        "memory_type": "difficulty",
        "subject_key": "条件概率",
        "subject_label": "条件概率练习",
        "content": "「条件概率练习」近期 2 次因难度太高未完成（30 天内）",
        "value_json": '{"failed_count":2,"reason":"任务难度太高","window_days":30}',
        "strength": 0.4,
        "evidence_count": 2,
        "contradict_count": 0,
        "episode": 1,
        "episode_start_feedback_id": 1,
        "episode_started_at": "2026-11-01T10:00:00",
        "status": "active",
    }
    memory.update(overrides)
    return memory


# ==========================
# M 组：V7 迁移
# ==========================


def test_v7_creates_five_tables():
    use(DB)
    database.init_db()

    names = table_names()
    for table in (
        "learning_memories",
        "memory_evidence",
        "memory_events",
        "memory_pending_evidence",
        "memory_apply_cursor",
    ):
        check(f"M1 新库存在 {table}", table in names, f"tables={sorted(names)}")

    check("M1 新库 user_version = 7", database.SCHEMA_VERSION == 7)


def test_v7_columns_and_indexes():
    names = column_names("learning_memories")
    for column in (
        "memory_type", "subject_key", "subject_label", "content", "value_json",
        "strength", "evidence_count", "contradict_count", "episode",
        "episode_start_feedback_id", "episode_started_at", "pending_episode",
        "pending_start_feedback_id", "status", "first_seen_at",
        "last_supported_at", "updated_at",
    ):
        check(f"M2 learning_memories 有列 {column}", column in names, f"columns={names}")

    memory_indexes = index_names("learning_memories")
    check("M2 唯一索引 idx_memories_type_subject",
          "idx_memories_type_subject" in memory_indexes, f"indexes={memory_indexes}")

    evidence_indexes = index_names("memory_evidence")
    check("M2 唯一索引 idx_evidence_memory_feedback",
          "idx_evidence_memory_feedback" in evidence_indexes, f"indexes={evidence_indexes}")
    check("M2 evidence 按 feedback 反查索引",
          "idx_evidence_feedback" in evidence_indexes)

    pending_columns = column_names("memory_pending_evidence")
    check("M2 pending 不存 relation（方向未定先入库）",
          "relation" not in pending_columns, f"columns={pending_columns}")
    check("M2 唯一索引 idx_pending_memory_feedback",
          "idx_pending_memory_feedback" in index_names("memory_pending_evidence"))


def test_empty_database_cursor_is_zero():
    use(DB)
    database.init_db()

    check("M3 空库迁移后游标 = 0", database.read_cursor() == 0,
          f"cursor={database.read_cursor()}")


def test_legacy_database_uses_history_high_water():
    use(LEGACY_DB)
    database.init_db()

    ids = [insert_feedback_row(task=f"历史任务{index}") for index in range(1, 4)]

    # 模拟 V6 老库：删掉 V7 五张表、版本回退到 6
    with connect() as conn:
        for table in (
            "memory_apply_cursor", "memory_pending_evidence", "memory_events",
            "memory_evidence", "learning_memories",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version = 6")

    database.init_db()

    check("M4 老库升级后五表重建", "learning_memories" in table_names())
    check("M4 老库升级后游标 = 历史最大 id（不回填历史）",
          database.read_cursor() == max(ids), f"cursor={database.read_cursor()}, ids={ids}")
    check("M4 老库升级后记忆表为空（无伪造记忆）",
          len(database.list_memories(limit=100)) == 0)

    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
    check("M4 老库升级不删反馈数据", count == 3, f"count={count}")


def test_repeat_migration_is_idempotent():
    cursor_before = database.read_cursor()
    database.init_db()
    database.init_db()

    check("M5 重复迁移后版本不变", True)
    check("M5 重复迁移不改游标", database.read_cursor() == cursor_before,
          f"{cursor_before} -> {database.read_cursor()}")


def test_newer_database_is_rejected():
    use(DB)
    database.init_db()

    with connect() as conn:
        conn.execute(f"PRAGMA user_version = {database.SCHEMA_VERSION + 1}")

    try:
        database.init_db()
    except RuntimeError as error:
        check("M6 高版本库拒绝启动", "高于程序支持" in str(error), str(error)[:60])
    else:
        raise AssertionError("FAILED: M6 高版本库应抛 RuntimeError")

    with connect() as conn:
        conn.execute(f"PRAGMA user_version = {database.SCHEMA_VERSION}")


def test_cleared_legacy_db_uses_sequence_high_water():
    """M7：升级前已清空但 sqlite_sequence 非零，游标必须取序列而不是 0。"""

    use(HIGH_WATER_DB)
    database.init_db()

    for index in range(1, 4):
        insert_feedback_row(task=f"已删历史{index}")

    # 只 DELETE 不清序列（旧版 delete_all_feedback 的行为）
    with connect() as conn:
        conn.execute("DELETE FROM feedbacks")
        for table in (
            "memory_apply_cursor", "memory_pending_evidence", "memory_events",
            "memory_evidence", "learning_memories",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version = 6")

    database.init_db()

    check("M7 清空但序列非零 -> 游标 = 序列高水位",
          database.read_cursor() == 3, f"cursor={database.read_cursor()}")

    new_id = insert_feedback_row(task="清空后的新反馈", created_at="2026-11-20T10:00:00")
    check("M7 清空后新反馈 id 从高水位衔接", new_id == 4, f"id={new_id}")


# ==========================
# K 组：游标读写（K1–K6）
# ==========================


def test_cursor_read_write_contract():
    use(DB)
    database.init_db()

    check("K1 无游标行时按 0 返回", database.read_cursor() == 0)

    database.write_cursor(7, "2026-11-11T00:00:00")
    check("K2 写入后能读回", database.read_cursor() == 7, f"cursor={database.read_cursor()}")

    for label, args in (
        ("K3 updated_at 为 None", (1, None)),
        ("K3 updated_at 为空串", (1, "  ")),
        ("K4 feedback_id 为字符串", ("5", "2026-11-11T00:00:00")),
        ("K4 feedback_id 为布尔", (True, "2026-11-11T00:00:00")),
        ("K5 feedback_id 为负数", (-1, "2026-11-11T00:00:00")),
    ):
        try:
            database.write_cursor(*args)
        except ValueError:
            print(f"PASS: {label} 被拒绝")
        else:
            raise AssertionError(f"FAILED: {label} 应抛 ValueError")

    with connect() as conn:
        row = conn.execute(
            "SELECT last_applied_feedback_id, updated_at FROM memory_apply_cursor WHERE id = 1"
        ).fetchone()
    check("K6 非法写入不改变游标", row[0] == 7 and row[1] == "2026-11-11T00:00:00", f"row={row}")


# ==========================
# D 组：记忆 / 证据 / 事件 / pending CRUD
# ==========================


def test_memory_crud():
    use(DB)
    database.init_db()

    created = database.insert_memory(make_memory(), "2026-11-01T10:00:00")
    check("D1 插入记忆", created["inserted"] is True and created["row_id"] >= 1,
          f"result={created}")

    memory = database.get_memory_by_key("difficulty", "条件概率")
    check("D1 按 (type, key) 读回", memory is not None and memory["subject_label"] == "条件概率练习")

    duplicate = database.insert_memory(make_memory(content="另一句话"), "2026-11-02T10:00:00")
    check("D2 重复插入不新增（唯一索引）", duplicate["inserted"] is False, f"result={duplicate}")

    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM learning_memories").fetchone()[0]
    check("D2 重复插入后仍只有一行", count == 1, f"count={count}")

    updated = database.update_memory_state(
        memory["id"],
        {"status": "weakened", "evidence_count": 3, "updated_at": "2026-11-03T10:00:00"},
    )
    check("D3 白名单字段可更新", updated is True)
    memory = database.get_memory_by_key("difficulty", "条件概率")
    check("D3 更新落库", memory["status"] == "weakened" and memory["evidence_count"] == 3)

    try:
        database.update_memory_state(memory["id"], {"first_seen_at": "2020-01-01T00:00:00"})
    except ValueError:
        print("PASS: D4 非白名单字段被拒绝")
    else:
        raise AssertionError("FAILED: D4 非白名单字段应被拒绝")


def test_evidence_crud():
    memory = database.get_memory_by_key("difficulty", "条件概率")

    first = database.insert_evidence(
        memory["id"], 11, None, 1, "supports", "2026-11-01T10:00:00"
    )
    check("D5 插入证据", first["inserted"] is True and first["row"]["relation"] == "supports",
          f"result={first}")

    second = database.insert_evidence(
        memory["id"], 11, None, 1, "contradicts", "2026-11-01T10:00:00"
    )
    check("D6 冲突不覆盖既有证据（证据不可变）",
          second["inserted"] is False and second["row"]["relation"] == "supports",
          f"result={second}")

    database.insert_evidence(memory["id"], 13, None, 1, "contradicts", "2026-11-02T10:00:00")
    database.insert_evidence(memory["id"], 12, None, 1, "supports", "2026-11-03T10:00:00")

    check("D7 count_evidence 总数", database.count_evidence(memory["id"]) == 3)
    check("D7 count_evidence 按关系", database.count_evidence(memory["id"], relation="supports") == 2)
    check("D7 max_evidence_feedback_id", database.max_evidence_feedback_id(memory["id"]) == 13)

    ordered = [row["feedback_id"] for row in database.list_evidence(memory["id"])]
    check("D8 证据按 feedback_id 升序", ordered == [11, 12, 13], f"ordered={ordered}")

    check("D8 get_evidence 单条", database.get_evidence(memory["id"], 12)["relation"] == "supports")
    check("D8 不存在的证据返回 None", database.get_evidence(memory["id"], 99) is None)


def test_event_and_pending_crud():
    memory = database.get_memory_by_key("difficulty", "条件概率")

    event_id = database.insert_memory_event(
        memory["id"], "created", '{"as_of":"2026-11-01T10:00:00","episode":1}',
        "2026-11-01T10:00:00",
    )
    check("D12 写入事件", event_id >= 1, f"event_id={event_id}")

    pending = database.insert_pending(
        memory["id"], 21, None, 1, "2026-11-04T10:00:00", "2026-11-04T10:00:01"
    )
    check("D9 写入 pending", pending["inserted"] is True, f"result={pending}")

    again = database.insert_pending(
        memory["id"], 21, None, 1, "2026-11-04T10:00:00", "2026-11-04T10:00:02"
    )
    check("D11 同反馈重复写入 pending 不新增", again["inserted"] is False, f"result={again}")

    rows = database.list_pending(memory["id"])
    check("D9 list_pending 返回一行", len(rows) == 1 and rows[0]["feedback_id"] == 21,
          f"rows={rows}")

    database.insert_pending(memory["id"], 22, None, 1, "2026-10-01T10:00:00", "2026-10-01T10:00:01")
    deleted = database.delete_expired_pending("2026-10-15T00:00:00")
    check("D10 过期 pending 被删除", deleted == 1, f"deleted={deleted}")

    remaining = database.list_pending(memory["id"])
    remaining_ids = [row["id"] for row in remaining]
    check("D10 未过期的保留", len(remaining) == 1, f"remaining={remaining}")

    removed = database.delete_pending(remaining_ids)
    check("D9 delete_pending 按主键删除", removed == 1 and not database.list_pending(memory["id"]))


# ==========================
# X 组：删除反馈的级联清理
# ==========================


def test_delete_feedback_cascades_and_keeps_high_water():
    use(DB)
    database.init_db()

    ids = [
        insert_feedback_row(task=f"级联测试{index}", created_at=f"2026-11-0{index}T10:00:00")
        for index in range(1, 4)
    ]

    memory = database.insert_memory(make_memory(subject_key="级联测试1"), "2026-11-01T10:00:00")
    database.insert_evidence(memory["row_id"], ids[0], None, 1, "supports", "2026-11-01T10:00:00")
    database.insert_memory_event(memory["row_id"], "created", "{}", "2026-11-01T10:00:00")
    database.insert_pending(memory["row_id"], ids[1], None, 1, "2026-11-02T10:00:00",
                            "2026-11-02T10:00:00")

    database.delete_all_feedback()

    with connect() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "feedbacks", "learning_memories", "memory_evidence",
                "memory_events", "memory_pending_evidence",
            )
        }

    check("X1 五张表全部清空", all(value == 0 for value in counts.values()), f"counts={counts}")
    check("X2 无悬空证据（证据表为空）", counts["memory_evidence"] == 0)

    with connect() as conn:
        deleted_events = conn.execute(
            "SELECT COUNT(*) FROM memory_events WHERE event_type = 'deleted'"
        ).fetchone()[0]
    check("X3 不写 deleted 事件", deleted_events == 0)

    check("X5 游标写回清空前高水位", database.read_cursor() == 3,
          f"cursor={database.read_cursor()}")

    database.delete_all_feedback()
    check("X4 重复删除幂等且游标不回退", database.read_cursor() == 3,
          f"cursor={database.read_cursor()}")

    new_id = insert_feedback_row(task="删除后的新反馈", created_at="2026-11-20T10:00:00")
    check("X5 新反馈 id 与高水位衔接（不卡死）", new_id == 4, f"id={new_id}")


def test_delete_all_memories_keeps_feedback_and_cursor():
    use(DB)
    database.init_db()

    insert_feedback_row(task="保留测试")
    database.write_cursor(1, "2026-11-10T00:00:00")
    memory = database.insert_memory(make_memory(subject_key="保留测试"), "2026-11-01T10:00:00")
    database.insert_evidence(memory["row_id"], 1, None, 1, "supports", "2026-11-01T10:00:00")

    database.delete_all_memories()

    with connect() as conn:
        feedbacks = conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
        evidence = conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0]

    check("D13 delete_all_memories 只清记忆三表",
          feedbacks == 1 and evidence == 0, f"feedbacks={feedbacks}, evidence={evidence}")
    check("D13 不动游标", database.read_cursor() == 1)


# ==========================
# N 组：主题归一化
# ==========================


def test_normalize_subject_basics():
    key, label = memory_service.normalize_subject("条件概率")
    check("N1 基本归一化", (key, label) == ("条件概率", "条件概率"), f"{key} / {label}")

    key, _ = memory_service.normalize_subject("  条件概率   练习 ")
    check("N2 折叠空白 + 剥离后缀", key == "条件概率", f"key={key}")

    key, _ = memory_service.normalize_subject("Ｃｏｎｄｉｔｉｏｎａｌ  Probability")
    check("N3 全半角统一 + 英文小写",
          key == "conditional probability", f"key={key}")

    key, _ = memory_service.normalize_subject("条件概率练习册")
    check("N3 剥离「练习册」后缀", key == "条件概率", f"key={key}")

    key, _ = memory_service.normalize_subject("复习")
    check("N5 只有后缀时保留原文", key == "复习", f"key={key}")

    key, label = memory_service.normalize_subject("   ")
    check("N5 空任务返回 (None, None)", key is None and label is None)

    key, _ = memory_service.normalize_subject("积" * 200)
    check("N6 超长 key 截断到 120", len(key) == 120, f"len={len(key)}")

    _, label = memory_service.normalize_subject("题" * 200)
    check("N6 label 截断到 60", len(label) == 60, f"len={len(label)}")


def test_normalize_subject_collapses_variants():
    variants = ["条件概率练习", " 条件概率 ", "条件概率复习", "条件概率练习题", "条件概率"]
    keys = {memory_service.normalize_subject(value)[0] for value in variants}
    check("N7 同一主题的不同写法落到同一 key", keys == {"条件概率"}, f"keys={keys}")


# ==========================
# R 组：内容渲染
# ==========================


def test_render_content_templates():
    content = memory_service.render_content(
        "difficulty",
        {"failed_count": 2, "reason": "任务难度太高", "window_days": 30},
        "条件概率练习",
        supports=4,
    )
    check("R1 difficulty 模板", content == "「条件概率练习」近期 2 次因难度太高未完成（30 天内）",
          content)

    content = memory_service.render_content(
        "task_performance",
        {"samples": 4, "completed": 1, "completion_rate": 0.25, "direction": "low"},
        "条件概率",
        supports=6,
    )
    check("R2 task_performance 用百分比", content == "「条件概率」近期 4 次任务完成率 25%", content)

    content = memory_service.render_content(
        "time_pattern",
        {"difficulty": "hard", "samples": 5, "median_ratio": 1.6},
        "困难",
        supports=8,
    )
    check("R3 time_pattern 用难度中文与倍数",
          content == "困难 任务近期 5 次实际用时约为预估的 1.6 倍", content)

    content = memory_service.render_content(
        "task_topic",
        {"appearances": 3, "last_status": "partial"},
        "条件概率",
        supports=6,
    )
    check("R4 task_topic 模板", content == "「条件概率」近期反馈中 3 次出现", content)

    tentative = memory_service.render_content(
        "difficulty", {"failed_count": 2, "window_days": 30}, "条件概率", supports=2
    )
    check("R5 未达稳定门槛加「初步观察：」",
          tentative.startswith(memory_service.TENTATIVE_PREFIX), tentative)

    stable = memory_service.render_content(
        "difficulty", {"failed_count": 4, "window_days": 30}, "条件概率", supports=4
    )
    check("R6 达到稳定门槛不加前缀",
          not stable.startswith(memory_service.TENTATIVE_PREFIX), stable)

    for label, args in (
        ("R7 未知类型", ("preference", {"x": 1}, "x", 1)),
        ("R8 缺字段", ("task_performance", {"samples": 3}, "x", 1)),
        ("R8 非 dict", ("difficulty", ["failed_count"], "x", 1)),
        ("R8 未知难度值", ("time_pattern", {"difficulty": "insane", "samples": 4,
                                            "median_ratio": 1.6}, "x", 1)),
    ):
        try:
            memory_service.render_content(*args)
        except ValueError:
            print(f"PASS: {label} 被拒绝")
        else:
            raise AssertionError(f"FAILED: {label} 应抛 ValueError")


def test_value_json_roundtrip_is_deterministic():
    value = {"window_days": 30, "failed_count": 2, "reason": "任务难度太高"}
    encoded = memory_service.encode_value(value)
    check("R9 序列化键排序且保留中文",
          encoded == '{"failed_count":2,"reason":"任务难度太高","window_days":30}', encoded)
    check("R9 反序列化还原", memory_service.decode_value(encoded) == value)

    try:
        memory_service.decode_value("{not json}")
    except ValueError:
        print("PASS: R9 损坏的 value_json 抛 ValueError")
    else:
        raise AssertionError("FAILED: R9 损坏的 value_json 应抛 ValueError")


# ==========================
# 运行
# ==========================


def _run_all():
    tests = [
        test_v7_creates_five_tables,
        test_v7_columns_and_indexes,
        test_empty_database_cursor_is_zero,
        test_legacy_database_uses_history_high_water,
        test_repeat_migration_is_idempotent,
        test_newer_database_is_rejected,
        test_cleared_legacy_db_uses_sequence_high_water,
        test_cursor_read_write_contract,
        test_memory_crud,
        test_evidence_crud,
        test_event_and_pending_crud,
        test_delete_feedback_cascades_and_keeps_high_water,
        test_delete_all_memories_keeps_feedback_and_cursor,
        test_normalize_subject_basics,
        test_normalize_subject_collapses_variants,
        test_render_content_templates,
        test_value_json_roundtrip_is_deterministic,
    ]

    failures = 0

    for test in tests:
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f"FAILED: {test.__name__}: {error}")
        except Exception as error:  # noqa: BLE001 - 打印真实异常类型，便于定位
            failures += 1
            print(f"ERROR: {test.__name__}: {type(error).__name__}: {error}")

    for path in (DB, LEGACY_DB, HIGH_WATER_DB,
                 PROJECT_DIR / "_test_learning_memory_env.db"):
        remove_file(path)

    after = REAL_DB.read_bytes() if REAL_DB.exists() else None
    if after != _real_db_bytes_before:
        failures += 1
        print("FAILED: 真实 feedback.db 被改动")

    print(f"\n结果：{len(tests) - failures}/{len(tests)} 个测试函数通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
