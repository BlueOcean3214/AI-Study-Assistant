"""毕业设计实验脚手架 · 第一阶段：样本库构建。

把 `samples/S*.json` 里的模拟学习历史写进**独立的临时 SQLite 文件**，再调用项目真实的
`memory_service.rebuild_all()` 重建长期记忆与证据。

硬性约束（与 docs/GRADUATION-EXPERIMENT-DESIGN.md §3.2、§5.6 一致）：

1. 只写 `%TEMP%/graduation_experiments/<ref_date>/` 下的 `.db`；导入项目模块**之前**先把
   `FEEDBACK_DB_PATH` 指向该目录里的占位文件，导入后再断言 `database.DATABASE_PATH`
   仍在这个目录内，否则直接抛 RuntimeError（fail-closed，不"尽力而为"）。
2. `ref_date` 固定在 `config.json`，所有 `created_at` 都由它加减偏移量得到，不受本机时钟影响。
3. 不调用任何模型 / Embedding API，不写项目根目录的 `feedback.db`。

命令行：

    python experiments/graduation/seed_fixture.py            # 建 7 个库并重建
    python experiments/graduation/seed_fixture.py --sample S3
    python experiments/graduation/seed_fixture.py --keep     # 复用已存在的库（只复用、不再写入）
"""

import argparse
import gc
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DEFAULT_CONFIG_PATH = HERE / "config.json"
SAMPLES_DIR = HERE / "samples"

WORKSPACE_ROOT_NAME = "graduation_experiments"
PLACEHOLDER_DB_NAME = "_placeholder_not_a_database.db"


# ---------------------------------------------------------------------------
# 配置与样本
# ---------------------------------------------------------------------------


def load_config(config_path=None):
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    for key in ("ref_date", "samples", "groups", "memory_limit"):
        if key not in config:
            raise ValueError(f"config.json 缺少必填字段：{key}")

    return config


def ref_datetime(config=None):
    """把 config.ref_date 解析成 datetime（本地无时区，与数据库 created_at 同格式）。"""

    config = config or load_config()
    return datetime.fromisoformat(config["ref_date"])


def workspace_dir(config=None):
    """本次运行的工作目录：%TEMP%/graduation_experiments/<20261112T210000>/"""

    config = config or load_config()
    slug = ref_datetime(config).strftime("%Y%m%dT%H%M%S")
    path = Path(tempfile.gettempdir()) / WORKSPACE_ROOT_NAME / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_sample(sample_id, config=None):
    config = config or load_config()
    path = SAMPLES_DIR / f"{sample_id}.json"

    if not path.is_file():
        raise FileNotFoundError(f"缺少样本文件：{path}")

    with path.open("r", encoding="utf-8") as handle:
        sample = json.load(handle)

    if sample.get("sample_id") != sample_id:
        raise ValueError(f"{path.name} 的 sample_id 与文件名不一致")

    for key in ("scenario", "feedbacks", "current_user_request", "gold_standard"):
        if key not in sample:
            raise ValueError(f"{sample_id} 缺少必填字段：{key}")

    simulation = sample.get("simulation")
    if not isinstance(simulation, dict) or simulation.get("is_simulated") is not True:
        raise ValueError(f"{sample_id} 必须显式标注 simulation.is_simulated=true")

    if sample["gold_standard"].get("simulation") is not True:
        raise ValueError(f"{sample_id} 的 gold_standard 必须显式标注 simulation=true")

    return sample


def sample_ids(config=None):
    return list((config or load_config())["samples"])


# ---------------------------------------------------------------------------
# 项目模块导入（必须在导入前完成隔离）
# ---------------------------------------------------------------------------


def _prepare_isolation(work_dir):
    os.environ["FEEDBACK_DB_PATH"] = str(work_dir / PLACEHOLDER_DB_NAME)

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def _import_project(work_dir):
    _prepare_isolation(work_dir)

    import database  # noqa: E402
    import memory_service  # noqa: E402

    resolved = Path(database.DATABASE_PATH).resolve()
    if work_dir.resolve() not in resolved.parents:
        raise RuntimeError(
            "数据库路径未被隔离，拒绝继续："
            f"database.DATABASE_PATH={resolved} 不在 {work_dir.resolve()} 之内"
        )

    return database, memory_service


_isolation_state = {"work_dir": None, "database": None, "memory_service": None}


def project_modules(config=None):
    """按当前 ref_date 的工作目录返回 (database, memory_service)，并保证隔离。"""

    work_dir = workspace_dir(config)

    if _isolation_state["work_dir"] != work_dir:
        database, memory_service = _import_project(work_dir)
        _isolation_state.update(
            work_dir=work_dir, database=database, memory_service=memory_service
        )

    return _isolation_state["database"], _isolation_state["memory_service"]


# ---------------------------------------------------------------------------
# 建库
# ---------------------------------------------------------------------------


def _plan_payload(plan):
    subtasks = plan.get("subtasks") or []

    return {
        "task": plan["task"],
        "estimated_minutes": plan["estimated_minutes"],
        "difficulty": plan["difficulty"],
        "question_count": plan["question_count"],
        "reason": plan.get("reason") or "按历史安排",
        "completion_criteria": plan.get("completion_criteria") or "完成练习",
        "subtasks_json": json.dumps(
            [
                {"title": item, "minutes": max(1, plan["estimated_minutes"] // max(1, len(subtasks))), "done": False}
                for item in subtasks
            ],
            ensure_ascii=False,
        ),
    }


REQUIRED_TABLES = (
    "feedbacks",
    "plans",
    "learning_memories",
    "memory_evidence",
    "memory_events",
    "memory_pending_evidence",
    "memory_apply_cursor",
)

CURSOR_COLUMNS = ("id", "last_applied_feedback_id", "updated_at")

# 产品只往正式证据表写这两种关系；neutral 留在 pending 缓冲区，不能混进 memory_evidence
EVIDENCE_RELATIONS = ("supports", "contradicts")


def _evidence_anomalies(conn):
    """逐行检查 memory_evidence 的引用完整性与 relation 合法性（只读，不改库）。

    memory_evidence 建表时没有声明实际外键，SQLite 不会替脚手架兜底：
    指向不存在记忆/反馈/计划的证据行、以及非法 relation，都只能在这里查出来。
    返回异常 id 列表与非法 relation 计数，由 inspect_sample_db 汇总成一条报错。
    """

    def ids(sql):
        return [row[0] for row in conn.execute(sql).fetchall()]

    placeholders = ", ".join("?" for _ in EVIDENCE_RELATIONS)

    return {
        "orphan_memory_ids": ids(
            "SELECT e.memory_id FROM memory_evidence e "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM learning_memories m WHERE m.id = e.memory_id)"
        ),
        "orphan_feedback_ids": ids(
            "SELECT e.feedback_id FROM memory_evidence e "
            "WHERE NOT EXISTS (SELECT 1 FROM feedbacks f WHERE f.id = e.feedback_id)"
        ),
        "orphan_plan_ids": ids(
            "SELECT e.plan_id FROM memory_evidence e "
            "WHERE e.plan_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM plans p WHERE p.id = e.plan_id)"
        ),
        "bad_relations": [
            (relation, count)
            for relation, count in conn.execute(
                f"SELECT relation, COUNT(*) FROM memory_evidence "
                f"WHERE relation NOT IN ({placeholders}) "
                "GROUP BY relation ORDER BY relation",
                EVIDENCE_RELATIONS,
            ).fetchall()
        ],
    }


def _db_state(db_path):
    """读出现有库的结构、行数与关键内容；缺表或不是 SQLite 时抛 RuntimeError（fail-closed）。"""

    db_path = Path(db_path)

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"{db_path.name} 不是可用的样本库：{exc}") from exc

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = [name for name in REQUIRED_TABLES if name not in tables]

        if missing:
            raise RuntimeError(
                f"数据库结构不完整：{db_path.name} 缺少表 {missing}"
            )

        return {
            "counts": {
                name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in REQUIRED_TABLES
            },
            "cursor_columns": tuple(
                row[1]
                for row in conn.execute("PRAGMA table_info(memory_apply_cursor)")
            ),
            "cursor_rows": conn.execute(
                "SELECT id, last_applied_feedback_id, updated_at FROM memory_apply_cursor"
            ).fetchall(),
            "max_feedback_id": conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM feedbacks"
            ).fetchone()[0],
            "memories": conn.execute(
                "SELECT memory_type, subject_key FROM learning_memories"
            ).fetchall(),
            "evidence": conn.execute(
                "SELECT m.memory_type, m.subject_key, e.relation, COUNT(*) "
                "FROM memory_evidence e "
                "JOIN learning_memories m ON m.id = e.memory_id "
                "GROUP BY m.memory_type, m.subject_key, e.relation"
            ).fetchall(),
            "evidence_anomalies": _evidence_anomalies(conn),
        }
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"{db_path.name} 不是可用的样本库：{exc}") from exc
    finally:
        conn.close()


def _evidence_totals(rows):
    """把 (type, key, relation, count) 聚成 {(type, key): {supports, contradicts}}。

    只累加两种正式关系；其它 relation 已被 _evidence_anomalies 判为非法，
    不会走到这里被静默忽略。
    """

    totals = {}

    for memory_type, subject_key, relation, count in rows:
        bucket = totals.setdefault(
            (memory_type, subject_key), {"supports": 0, "contradicts": 0}
        )

        if relation in bucket:
            bucket[relation] += count

    return totals


def inspect_sample_db(db_path, sample):
    """校验已有库是不是该样本的**完整**库；不完整直接报错，绝不静默写入别的库。

    逐项比对 Gold Standard，而不是只看总行数：
    记忆集合（少一条 / 多一条 / 重复一条）、每条记忆的 supports 与 contradicts 证据行数、
    证据行的引用完整性（memory_id / feedback_id / plan_id 必须指向真实记录，relation 只允许
    supports 与 contradicts）、pending 缓冲区、游标表记录与反馈高水位。
    任何一项不符都判定为不可复用。全程只读，不修改库。
    """

    db_path = Path(db_path)

    if not db_path.is_file():
        raise FileNotFoundError(f"样本库不存在：{db_path}")

    if db_path.stat().st_size == 0:
        raise RuntimeError(f"样本库为空文件：{db_path}")

    state = _db_state(db_path)
    counts = state["counts"]
    gold = sample["gold_standard"]["memories"]

    problems = []

    # 1) 基础行数
    expected_feedbacks = len(sample["feedbacks"])
    expected_plans = 1 if sample.get("plan") else 0

    if counts["feedbacks"] != expected_feedbacks:
        problems.append(f"反馈数 {counts['feedbacks']}，样本要求 {expected_feedbacks}")

    if counts["plans"] != expected_plans:
        problems.append(f"计划数 {counts['plans']}，样本要求 {expected_plans}")

    # 2) 记忆集合：按 (memory_type, subject_key) 逐个比对，总数相同但键不同也要报错
    expected_keys = [(item["memory_type"], item["subject_key"]) for item in gold]
    actual_keys = [(row[0], row[1]) for row in state["memories"]]

    if len(actual_keys) != len(set(actual_keys)):
        problems.append("记忆表存在重复的 (memory_type, subject_key) 记录")

    missing_keys = [key for key in expected_keys if key not in set(actual_keys)]
    extra_keys = [key for key in actual_keys if key not in set(expected_keys)]

    if missing_keys:
        problems.append(f"缺少 Gold Standard 记忆 {missing_keys}")

    if extra_keys:
        problems.append(f"多出 Gold Standard 之外的记忆 {extra_keys}")

    # 3) 证据引用完整性：孤立的 memory_id / feedback_id / plan_id 与非法 relation 都不允许出现
    anomalies = state["evidence_anomalies"]

    if anomalies["orphan_memory_ids"]:
        problems.append(
            f"孤立证据 {len(anomalies['orphan_memory_ids'])} 条，memory_id "
            f"{sorted(set(anomalies['orphan_memory_ids']))} 在 learning_memories 中不存在"
        )

    if anomalies["orphan_feedback_ids"]:
        problems.append(
            f"孤立证据 {len(anomalies['orphan_feedback_ids'])} 条，feedback_id "
            f"{sorted(set(anomalies['orphan_feedback_ids']))} 在 feedbacks 中不存在"
        )

    if anomalies["orphan_plan_ids"]:
        problems.append(
            f"孤立证据 {len(anomalies['orphan_plan_ids'])} 条，plan_id "
            f"{sorted(set(anomalies['orphan_plan_ids']))} 在 plans 中不存在"
        )

    if anomalies["bad_relations"]:
        detail = "、".join(
            f"{relation}×{count}" for relation, count in anomalies["bad_relations"]
        )
        problems.append(
            f"非法证据关系 {detail}，只允许 {'/'.join(EVIDENCE_RELATIONS)}"
        )

    # 4) 证据：每条记忆的 supports / contradicts 必须与 Gold Standard 逐条一致
    totals = _evidence_totals(state["evidence"])

    for item in gold:
        key = (item["memory_type"], item["subject_key"])
        got = totals.get(key, {"supports": 0, "contradicts": 0})
        want = {
            "supports": int(item["supports"]),
            "contradicts": int(item["contradicts"]),
        }

        if got != want:
            problems.append(
                f"记忆 {key} 证据 supports/contradicts={got}，"
                f"Gold Standard 要求 {want}"
            )

    # 5) pending 缓冲区必须为空
    if counts["memory_pending_evidence"] != 0:
        problems.append(
            f"pending 证据 {counts['memory_pending_evidence']} 条，应为 0"
        )

    # 6) 游标表：结构完整、有且仅有 id=1 的记录、游标值与反馈高水位一致
    missing_columns = [
        name for name in CURSOR_COLUMNS if name not in state["cursor_columns"]
    ]

    if missing_columns:
        problems.append(f"游标表 memory_apply_cursor 缺少列 {missing_columns}")

    cursor_rows = state["cursor_rows"]

    if len(cursor_rows) != 1:
        problems.append(
            f"游标表记录数 {len(cursor_rows)}，应当且只能有 1 条（id=1）"
        )
    else:
        cursor_id, applied, updated_at = cursor_rows[0]

        if cursor_id != 1:
            problems.append(f"游标主键 id={cursor_id}，必须是 1")

        if not (updated_at or "").strip():
            problems.append("游标 updated_at 为空")

        if applied != state["max_feedback_id"]:
            problems.append(
                f"游标 last_applied_feedback_id={applied}，"
                f"反馈高水位={state['max_feedback_id']}"
            )

    if problems:
        raise RuntimeError(
            f"{Path(db_path).name} 已有但结构不完整（{'；'.join(problems)}）；"
            "请删除该库后重跑，或去掉 --keep"
        )

    return counts


def seed_sample(sample_id, config=None, rebuild=True, keep=False):
    """为单个样本建库并（默认）重建记忆，返回构建结果字典。

    --keep 的语义是「只复用、不写入」：库已存在且完整时直接返回，不插入计划/反馈，
    也不重复重建；库不存在时照常创建。无论哪条分支，动手前都先把
    `database.DATABASE_PATH` 指向本样本的库，避免读到上一次留下的别的样本库。
    """

    config = config or load_config()
    sample = load_sample(sample_id, config)
    database, memory_service = project_modules(config)
    ref = ref_datetime(config)

    work_dir = workspace_dir(config).resolve()
    db_path = work_dir / f"{sample_id}.db"

    if db_path.resolve().parent != work_dir:
        raise RuntimeError(f"样本库路径越界，拒绝继续：{db_path}")

    # 任何数据库操作之前先绑定路径（keep 分支同样需要）
    database.DATABASE_PATH = db_path

    if keep and db_path.exists():
        counts = inspect_sample_db(db_path, sample)
        return {
            "sample_id": sample_id,
            "db_path": str(db_path),
            "feedback_ids": [],
            "plan_id": None,
            "rebuild": None,
            "reused": True,
            "counts": counts,
            "memories": dump_memories(db_path),
        }

    if db_path.exists():
        # 产品里的 `with sqlite3.connect(...)` 只提交事务、不关闭连接；
        # 同一进程重复建库前先让 CPython 回收这些句柄，否则 Windows 上 unlink 会报 WinError 32。
        gc.collect()
        db_path.unlink()

    database.init_db()

    plan_id = None
    plan = sample.get("plan")

    if plan:
        plan_date = (ref + timedelta(days=int(plan["offset_days"]))).date().isoformat()
        plan_id = database.insert_plan(
            plan_date,
            _plan_payload(plan),
            (ref + timedelta(days=int(plan["offset_days"]))).isoformat(),
            status=plan.get("status") or "completed",
        )

    feedback_ids = []

    for row in sample["feedbacks"]:
        feedback_ids.append(
            database.insert_feedback(
                {
                    "task": row["task"],
                    "estimated_minutes": row["estimated_minutes"],
                    "actual_minutes": row["actual_minutes"],
                    "status": row["status"],
                    "reason": row.get("reason"),
                    "completed_subtasks": row.get("completed_subtasks", 1),
                    "total_subtasks": row.get("total_subtasks", 2),
                    "completed_questions": row["completed_questions"],
                    "total_questions": row["total_questions"],
                    "plan_id": plan_id,
                },
                (ref + timedelta(days=int(row["offset_days"]))).isoformat(),
            )
        )

    rebuild_result = None

    if rebuild:
        # rebuild_all 只接受 ISO 字符串；传 datetime 会抛
        # ValueError("时间戳必须是非空字符串")（memory_service._parse_ts）
        rebuild_result = memory_service.rebuild_all(now=ref.isoformat())

    return {
        "sample_id": sample_id,
        "db_path": str(db_path),
        "feedback_ids": feedback_ids,
        "plan_id": plan_id,
        "rebuild": rebuild_result,
        "reused": False,
        "memories": dump_memories(db_path),
    }


def dump_memories(db_path):
    """按库内真实行导出记忆（含证据关系计数），不做任何加工。"""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        memories = []

        for row in conn.execute(
            "SELECT * FROM learning_memories ORDER BY memory_type, subject_key"
        ).fetchall():
            item = dict(row)
            relations = {
                rel["relation"]: rel["c"]
                for rel in conn.execute(
                    "SELECT relation, COUNT(*) AS c FROM memory_evidence "
                    "WHERE memory_id = ? GROUP BY relation",
                    (item["id"],),
                ).fetchall()
            }
            item["value"] = json.loads(item["value_json"])
            item["evidence_relations"] = relations
            memories.append(item)

        pendings = conn.execute(
            "SELECT COUNT(*) FROM memory_pending_evidence"
        ).fetchone()[0]

        events = [
            dict(row)
            for row in conn.execute(
                "SELECT memory_id, event_type, created_at FROM memory_events ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()

    return {"memories": memories, "pendings": pendings, "events": events}


def seed_all(config=None, keep=False):
    config = config or load_config()
    return {
        sample_id: seed_sample(sample_id, config, keep=keep)
        for sample_id in sample_ids(config)
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建毕业设计实验的 7 个样本库")
    parser.add_argument("--sample", action="append", help="只构建指定样本，可重复")
    parser.add_argument("--keep", action="store_true", help="保留已存在的库")
    parser.add_argument("--config", help="config.json 路径")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    targets = args.sample or sample_ids(config)

    for sample_id in targets:
        result = seed_sample(sample_id, config, keep=args.keep)
        rebuild = result["rebuild"] or {}

        if result.get("reused"):
            counts = result["counts"]
            print(
                f"{sample_id}: db={result['db_path']} reused=True "
                f"feedbacks={counts['feedbacks']} memories={counts['learning_memories']} "
                f"evidences={counts['memory_evidence']}"
            )
            continue

        print(
            f"{sample_id}: db={result['db_path']} ok={rebuild.get('ok')} "
            f"memories={rebuild.get('memories')} evidences={rebuild.get('evidences')} "
            f"pendings={rebuild.get('pendings')}"
        )

        if not rebuild.get("ok"):
            return 1

    print("工作目录：" + str(workspace_dir(config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
