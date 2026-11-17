"""isolated_environment 的路径恢复回归：必须恢复进入前的值，不能硬编码仓库默认路径。

覆盖 2026-11-17 的缺陷：agent_eval.isolated_environment() 在 finally 里把
database.DATABASE_PATH 写死成 PROJECT_DIR/feedback.db、vector_cache.CACHE_DIR
写死成 PROJECT_DIR/.rag_cache，导致 FEEDBACK_DB_PATH / RAG_CACHE_DIR 被覆盖时，
隔离库在退出后悄悄换成真实开发库。

只用 tempfile 临时目录里的合成数据；不论断言是否失败都会清理临时目录。
"""

import shutil
import sys
import tempfile
from pathlib import Path

import agent_eval
import database
import vector_cache


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def main():
    root = Path(tempfile.mkdtemp(prefix="dsbot_agent_eval_restore_"))
    sandbox_db = root / "isolated.db"
    sandbox_cache = root / "isolated_cache"
    sandbox_db.parent.mkdir(parents=True, exist_ok=True)

    original_db = database.DATABASE_PATH
    original_cache = vector_cache.CACHE_DIR

    try:
        # 模拟 FEEDBACK_DB_PATH / RAG_CACHE_DIR 生效后的进程状态：
        # 全局指向临时隔离库，而不是仓库的 feedback.db。
        database.DATABASE_PATH = sandbox_db
        vector_cache.CACHE_DIR = sandbox_cache

        with agent_eval.isolated_environment({"seed": None}):
            check(
                "隔离期间指向夹具库",
                Path(database.DATABASE_PATH) == Path(agent_eval.FIXTURE_DB),
                f"database.DATABASE_PATH={database.DATABASE_PATH}",
            )
            check(
                "隔离期间指向夹具缓存",
                Path(vector_cache.CACHE_DIR) == Path(agent_eval.FIXTURE_CACHE),
                f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
            )

        check(
            "退出后恢复进入前的隔离库（不是真实 feedback.db）",
            Path(database.DATABASE_PATH) == sandbox_db,
            f"database.DATABASE_PATH={database.DATABASE_PATH}",
        )
        check(
            "退出后恢复进入前的向量缓存目录",
            Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
        )
        check(
            "退出后没有指回真实开发库",
            Path(database.DATABASE_PATH) != agent_eval.REAL_DB,
            f"real={agent_eval.REAL_DB}",
        )

        # 异常路径同样要恢复（否则一次失败用例就能污染后面的用例）。
        try:
            with agent_eval.isolated_environment({"seed": None}):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        check(
            "用例抛异常时也恢复进入前的隔离库",
            Path(database.DATABASE_PATH) == sandbox_db,
            f"database.DATABASE_PATH={database.DATABASE_PATH}",
        )
        check(
            "用例抛异常时也恢复进入前的缓存目录",
            Path(vector_cache.CACHE_DIR) == sandbox_cache,
            f"vector_cache.CACHE_DIR={vector_cache.CACHE_DIR}",
        )

        # isolate=False 时不得改动任何全局路径。
        with agent_eval.isolated_environment({"seed": None}, isolate=False):
            inside_db = Path(database.DATABASE_PATH)
            inside_cache = Path(vector_cache.CACHE_DIR)

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

        print("PASS: isolated_environment 恢复进入前的数据库与缓存路径")
    finally:
        database.DATABASE_PATH = original_db
        vector_cache.CACHE_DIR = original_cache
        shutil.rmtree(root, ignore_errors=True)
        agent_eval.clean_fixtures()


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(str(error))
        sys.exit(1)
