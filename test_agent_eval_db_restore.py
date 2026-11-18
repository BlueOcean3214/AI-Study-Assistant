"""isolated_environment 的路径恢复回归。

覆盖两个已修缺陷：

1. 2026-11-17：finally 里把 database.DATABASE_PATH / vector_cache.CACHE_DIR
   写死成仓库默认路径，FEEDBACK_DB_PATH / RAG_CACHE_DIR 被覆盖时隔离库退出后
   悄悄换成真实开发库。现在必须恢复「进入前的值」。
2. 2026-11-18：路径切换、init_db、seed_database 原本在 try 之外，初始化或播种
   抛异常时根本走不到 finally，全局路径永久停在夹具库上。现在它们全部在 try 内，
   正常退出 / 用例异常 / 初始化异常 / 播种异常 四种路径都必须恢复。

夹具路径（agent_eval.FIXTURE_DB / FIXTURE_CACHE）在本测试里映射到自己的
tempfile 子目录，绝不读写、更不删除仓库里既有的 _eval_fixture.db / _eval_cache。

只用 tempfile 临时目录里的合成数据；无论断言是否失败都会清理临时目录。
"""

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import agent_eval
import database
import rag_service
import vector_cache


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def repo_fixture_fingerprint():
    """仓库固定夹具文件的指纹；文件不存在时返回 None。"""

    path = Path(agent_eval.PROJECT_DIR) / "_eval_fixture.db"
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def main():
    root = Path(tempfile.mkdtemp(prefix="dsbot_agent_eval_restore_"))
    fixture_db = root / "fixture" / "eval_fixture.db"
    fixture_cache = root / "fixture" / "eval_cache"
    sandbox_db = root / "sandbox" / "isolated.db"
    sandbox_cache = root / "sandbox" / "isolated_cache"
    fixture_db.parent.mkdir(parents=True, exist_ok=True)
    sandbox_db.parent.mkdir(parents=True, exist_ok=True)

    original_db = database.DATABASE_PATH
    original_cache = vector_cache.CACHE_DIR
    original_fixture_db = agent_eval.FIXTURE_DB
    original_fixture_cache = agent_eval.FIXTURE_CACHE
    original_init_db = database.init_db
    original_seed_database = agent_eval.seed_database
    original_reset_cache = rag_service.reset_cache

    repo_before = repo_fixture_fingerprint()
    reset_calls = []

    def tracking_reset_cache():
        # 记录调用瞬间的全局路径：用来断言"先恢复路径、再清缓存"。
        reset_calls.append((Path(database.DATABASE_PATH), Path(vector_cache.CACHE_DIR)))
        return original_reset_cache()

    try:
        # 夹具映射到本测试自己的 tempfile 子目录。
        agent_eval.FIXTURE_DB = fixture_db
        agent_eval.FIXTURE_CACHE = fixture_cache
        rag_service.reset_cache = tracking_reset_cache

        # 模拟 FEEDBACK_DB_PATH / RAG_CACHE_DIR 生效后的进程状态。
        database.DATABASE_PATH = sandbox_db
        vector_cache.CACHE_DIR = sandbox_cache

        # ---- 1) 正常退出 ----
        reset_calls.clear()
        with agent_eval.isolated_environment({"seed": "difficult_history"}):
            check(
                "隔离期间指向夹具库（tempfile 子目录）",
                Path(database.DATABASE_PATH) == fixture_db,
                f"database.DATABASE_PATH={database.DATABASE_PATH}",
            )
            check(
                "隔离期间指向夹具缓存（tempfile 子目录）",
                Path(vector_cache.CACHE_DIR) == fixture_cache,
                f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
            )
            check("隔离期间夹具库已初始化", Path(database.DATABASE_PATH).exists())
            rows = database.get_feedback_list()
            check(
                "隔离期间播种生效",
                len(rows) == len(agent_eval.SEED_ROWS["difficult_history"]),
                f"rows={len(rows)}",
            )

        check(
            "正常退出：恢复进入前的隔离库（不是仓库 feedback.db）",
            Path(database.DATABASE_PATH) == sandbox_db,
            f"database.DATABASE_PATH={database.DATABASE_PATH}",
        )
        check(
            "正常退出：恢复进入前的缓存目录",
            Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
        )
        check(
            "正常退出：reset_cache 在路径恢复之后调用",
            reset_calls == [(sandbox_db, sandbox_cache)],
            f"reset_calls={reset_calls}",
        )

        # ---- 2) yield 抛异常 ----
        reset_calls.clear()
        try:
            with agent_eval.isolated_environment({"seed": None}):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        check(
            "用例异常：恢复进入前的隔离库",
            Path(database.DATABASE_PATH) == sandbox_db,
            f"database.DATABASE_PATH={database.DATABASE_PATH}",
        )
        check(
            "用例异常：恢复进入前的缓存目录",
            Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
        )
        check(
            "用例异常：reset_cache 在路径恢复之后调用",
            reset_calls == [(sandbox_db, sandbox_cache)],
            f"reset_calls={reset_calls}",
        )

        # ---- 3) init_db 抛异常 ----
        seed_calls = []

        def failing_init_db():
            raise RuntimeError("init_db boom")

        def recording_seed(seed_name):
            seed_calls.append(seed_name)
            return original_seed_database(seed_name)

        database.init_db = failing_init_db
        agent_eval.seed_database = recording_seed
        reset_calls.clear()
        entered_body = False
        try:
            with agent_eval.isolated_environment({"seed": "difficult_history"}):
                entered_body = True
        except RuntimeError as error:
            check("初始化异常：异常向外传播", "init_db boom" in str(error), f"{error}")
        finally:
            database.init_db = original_init_db

        check("初始化异常：没有进入用例体", entered_body is False)
        check("初始化异常：没有播种", seed_calls == [], f"seed_calls={seed_calls}")
        check(
            "初始化异常：恢复进入前的隔离库",
            Path(database.DATABASE_PATH) == sandbox_db,
            f"database.DATABASE_PATH={database.DATABASE_PATH}",
        )
        check(
            "初始化异常：恢复进入前的缓存目录",
            Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
        )
        check(
            "初始化异常：reset_cache 在路径恢复之后调用",
            reset_calls == [(sandbox_db, sandbox_cache)],
            f"reset_calls={reset_calls}",
        )

        # ---- 4) seed_database 抛异常 ----
        seed_calls.clear()

        def failing_seed(seed_name):
            seed_calls.append(seed_name)
            raise RuntimeError("seed boom")

        agent_eval.seed_database = failing_seed
        reset_calls.clear()
        entered_body = False
        try:
            with agent_eval.isolated_environment({"seed": "difficult_history"}):
                entered_body = True
        except RuntimeError as error:
            check("播种异常：异常向外传播", "seed boom" in str(error), f"{error}")
        finally:
            agent_eval.seed_database = original_seed_database

        check("播种异常：没有进入用例体", entered_body is False)
        check("播种异常：种子名传到位", seed_calls == ["difficult_history"], f"seed_calls={seed_calls}")
        check(
            "播种异常：恢复进入前的隔离库",
            Path(database.DATABASE_PATH) == sandbox_db,
            f"database.DATABASE_PATH={database.DATABASE_PATH}",
        )
        check(
            "播种异常：恢复进入前的缓存目录",
            Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
        )
        check(
            "播种异常：reset_cache 在路径恢复之后调用",
            reset_calls == [(sandbox_db, sandbox_cache)],
            f"reset_calls={reset_calls}",
        )

        # ---- 5) isolate=False ----
        def forbidden(*_args, **_kwargs):
            raise AssertionError("isolate=False 不得切换路径或初始化/播种夹具库")

        database.init_db = forbidden
        agent_eval.seed_database = forbidden
        reset_calls.clear()
        try:
            with agent_eval.isolated_environment({"seed": "difficult_history"}, isolate=False):
                inside_db = Path(database.DATABASE_PATH)
                inside_cache = Path(vector_cache.CACHE_DIR)
        finally:
            database.init_db = original_init_db
            agent_eval.seed_database = original_seed_database

        check(
            "isolate=False 不切换数据库路径",
            inside_db == sandbox_db and Path(database.DATABASE_PATH) == sandbox_db,
            f"inside={inside_db}",
        )
        check(
            "isolate=False 不切换缓存路径",
            inside_cache == sandbox_cache and Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"inside={inside_cache}",
        )
        check("isolate=False 不清空 rag 缓存", reset_calls == [], f"reset_calls={reset_calls}")

        # ---- 6) 仓库固定夹具文件没被碰 ----
        check(
            "仓库固定夹具 _eval_fixture.db 未被创建/删除/修改",
            repo_fixture_fingerprint() == repo_before,
            f"before={repo_before} after={repo_fixture_fingerprint()}",
        )

        print("PASS: isolated_environment 四条异常/正常路径均恢复进入前的数据库与缓存路径")
    finally:
        database.DATABASE_PATH = original_db
        vector_cache.CACHE_DIR = original_cache
        database.init_db = original_init_db
        agent_eval.FIXTURE_DB = original_fixture_db
        agent_eval.FIXTURE_CACHE = original_fixture_cache
        agent_eval.seed_database = original_seed_database
        rag_service.reset_cache = original_reset_cache
        # 只清本测试自己的临时目录；不调用 clean_fixtures()（那会删仓库夹具）。
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(str(error))
        sys.exit(1)
