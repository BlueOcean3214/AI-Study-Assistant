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
    python experiments/graduation/seed_fixture.py --keep     # 保留已存在的库（不删除重建）
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


def seed_sample(sample_id, config=None, rebuild=True, keep=False):
    """为单个样本建库并（默认）重建记忆，返回构建结果字典。"""

    config = config or load_config()
    sample = load_sample(sample_id, config)
    database, memory_service = project_modules(config)
    ref = ref_datetime(config)

    db_path = workspace_dir(config) / f"{sample_id}.db"

    if keep and db_path.exists():
        pass
    else:
        if db_path.exists():
            # 产品里的 `with sqlite3.connect(...)` 只提交事务、不关闭连接；
            # 同一进程重复建库前先让 CPython 回收这些句柄，否则 Windows 上 unlink 会报 WinError 32。
            gc.collect()
            db_path.unlink()
        database.DATABASE_PATH = db_path
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
