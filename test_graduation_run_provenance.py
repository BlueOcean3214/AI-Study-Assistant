"""Run-Provenance 生产器改造的独立测试（标准库 unittest）。

覆盖：
- Git 运行时事实（HEAD、工作树干净/脏、patch 摘要）
- run_id 唯一性
- 知识结果 ID 缺失时 fail-closed
- 注入记忆逐条映射与歧义拒绝
- 注入清单原子写与哈希
- 短期三事实独立
- data_kind 来源
- 严格 Schema 校验
- run_one 严格路径成功 / 缺来源拒绝 / 失败记录仍保留来源
"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GRAD = ROOT / "experiments" / "graduation"
for entry in (str(ROOT), str(GRAD)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import build_prompts
import run_experiment
import run_provenance


def git(root, *args):
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.decode("utf-8", "replace").strip()


def make_repo(tmp):
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "audit@example.invalid")
    git(root, "config", "user.name", "Audit Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-q", "-m", "base")
    return root


class ProvenanceContextTests(unittest.TestCase):
    def test_clean_and_dirty_git_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            context = run_provenance.capture_context(root)
            self.assertTrue(context.runtime_worktree_clean)
            self.assertIsNone(context.runtime_patch_sha256)
            self.assertEqual(context.base_sha, git(root, "rev-parse", "HEAD"))

            (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            dirty = run_provenance.capture_context(root)
            self.assertFalse(dirty.runtime_worktree_clean)
            self.assertEqual(len(dirty.runtime_patch_sha256), 64)
            self.assertEqual(dirty.base_sha, context.base_sha)
            self.assertTrue(dirty.dirty_entries)

    def test_capture_rejects_non_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(run_provenance.ProvenanceError):
                run_provenance.capture_context(tmp)


class RunIdTests(unittest.TestCase):
    def test_run_id_unique_and_pattern(self):
        now = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)
        values = {
            run_provenance.make_run_id("S1", "D", 3, now=now)
            for _ in range(100)
        }
        self.assertEqual(len(values), 100)
        for value in values:
            self.assertRegex(
                value,
                r"^20261124T170000Z-S1-D-r3-[0-9a-f]{8}$",
            )


class KnowledgeTests(unittest.TestCase):
    def test_empty_is_allowed(self):
        self.assertEqual(
            run_provenance.extract_knowledge_result_ids({"knowledge_results": []}),
            [],
        )

    def test_missing_id_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.extract_knowledge_result_ids(
                {"knowledge_results": [{"content": "x"}]}
            )

    def test_actual_ids_kept(self):
        self.assertEqual(
            run_provenance.extract_knowledge_result_ids(
                {"knowledge_results": [{"id": "kb-1"}, {"id": "kb-2"}]}
            ),
            ["kb-1", "kb-2"],
        )


class MemoryMappingTests(unittest.TestCase):
    def raw(self, **overrides):
        row = {
            "memory_id": "m-1",
            "memory_type": "task_topic",
            "subject_key": "一元二次方程",
            "subject_label": "一元二次方程练习",
            "strength": 0.9,
            "status": "active",
            "content": "内容 A",
        }
        row.update(overrides)
        return row

    def tool(self, **overrides):
        row = {
            "type": "task_topic",
            "subject": "一元二次方程练习",
            "strength": 0.9,
            "content": "内容 A",
        }
        row.update(overrides)
        return row

    def test_unique_mapping(self):
        tagged = run_provenance.tag_memory_provenance(
            {"ok": True, "memories": [self.tool()]},
            {"ok": True, "memories": [self.raw()]},
        )
        self.assertEqual(
            tagged,
            [
                {
                    "memory_id": "m-1",
                    "memory_type": "task_topic",
                    "subject_key": "一元二次方程",
                    "strength": 0.9,
                    "status": "active",
                    "content_sha256": run_provenance.sha256_bytes("内容 A".encode("utf-8")),
                }
            ],
        )

    def test_ambiguous_content_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.tag_memory_provenance(
                {"ok": True, "memories": [self.tool()]},
                {"ok": True, "memories": [self.raw(), self.raw(memory_id="m-2")]},
            )

    def test_missing_status_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.tag_memory_provenance(
                {"ok": True, "memories": [self.tool()]},
                {"ok": True, "memories": [self.raw(status=None)]},
            )

    def test_no_memory_group_returns_empty(self):
        self.assertEqual(
            run_provenance.select_injected_memories(
                {"memory": False}, {"memory_provenance": {"default": [self.raw()]}}
            ),
            [],
        )

    def test_strict_missing_provenance_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.select_injected_memories({"memory": True}, {}, strict=True)


class InjectedJsonTests(unittest.TestCase):
    def test_atomic_and_deterministic(self):
        rows = [
            {
                "memory_id": "m-1",
                "memory_type": "task_topic",
                "subject_key": "k",
                "strength": 0.9,
                "status": "active",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            ref1, sha1 = run_provenance.write_injected_json(tmp, "S1", "C", rows)
            ref2, sha2 = run_provenance.write_injected_json(tmp, "S1", "C", rows)
            self.assertEqual((ref1, sha1), (ref2, sha2))
            path = Path(tmp) / ref1
            self.assertTrue(path.is_file())
            self.assertEqual(
                path.read_bytes(),
                (run_provenance.canonical_json(
                    {
                        "schema": "graduation-injected-memory/v1",
                        "sample_id": "S1",
                        "group": "C",
                        "memory_injected": rows,
                    }
                ) + "\n").encode("utf-8"),
            )
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


class ShortTermTests(unittest.TestCase):
    def test_three_facts_are_independent(self):
        slots = {"short_term": True}
        facts = run_provenance.short_term_facts(
            {
                "short_term_block": "【学习者近期情况（程序统计，非模型总结）】x",
                "short_term_info": {"feedback_count": 0},
            },
            slots,
        )
        self.assertTrue(facts["short_term_slot_enabled"])
        self.assertTrue(facts["short_term_block_present"])
        self.assertFalse(facts["short_term_feedback_history_present"])

        facts = run_provenance.short_term_facts(
            {"short_term_block": "", "short_term_info": {"feedback_count": 3}},
            {"short_term": False},
        )
        self.assertFalse(facts["short_term_slot_enabled"])
        self.assertFalse(facts["short_term_block_present"])
        self.assertTrue(facts["short_term_feedback_history_present"])

    def test_missing_count_strict_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.short_term_facts(
                {"short_term_block": "x"}, {"short_term": True}, strict=True
            )


class DataKindTests(unittest.TestCase):
    def test_simulated(self):
        sample = {
            "simulation": {"is_simulated": True},
            "gold_standard": {"simulation": True},
        }
        self.assertEqual(
            run_provenance.resolve_data_kind(sample), "simulated_learner_fixture"
        )

    def test_explicit_kind_wins(self):
        self.assertEqual(
            run_provenance.resolve_data_kind({"data_kind": "real_user_data"}),
            "real_user_data",
        )

    def test_simulation_mismatch_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.resolve_data_kind(
                {"simulation": {"is_simulated": True}, "gold_standard": {}}
            )


def valid_record():
    return {
        "run_id": "20261124T170000Z-S1-A-r0-0123abcd",
        "base_sha": "a" * 40,
        "runtime_source_commit": "a" * 40,
        "runtime_worktree_clean": True,
        "runtime_patch_sha256": None,
        "knowledge_result_ids": [],
        "memory_injected": [],
        "short_term_slot_enabled": False,
        "short_term_block_present": False,
        "short_term_feedback_history_present": False,
        "short_term_feedback_count": 0,
        "difficult_flag": False,
        "data_kind": "simulated_learner_fixture",
        "config_sha256": "b" * 64,
        "plan_sha256": "c" * 64,
        "prompt_sha256": "d" * 64,
        "injected_json_sha256": "e" * 64,
        "injected_json_ref": "injected/S1/A.json",
        "result_schema_version": run_provenance.PROVENANCE_SCHEMA_VERSION,
        "experiment_design_version": run_provenance.DESIGN_VERSION,
        "generated_at": "2026-11-24T17:00:00+00:00",
        "model_alias": None,
        "model_snapshot_id": None,
    }


class ValidationTests(unittest.TestCase):
    def test_valid_record(self):
        run_provenance.validate_provenance(valid_record())

    def test_missing_field_rejected(self):
        record = valid_record()
        del record["knowledge_result_ids"]
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.validate_provenance(record)

    def test_clean_with_patch_rejected(self):
        record = valid_record()
        record["runtime_patch_sha256"] = "f" * 64
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.validate_provenance(record)

    def test_bad_run_id_rejected(self):
        record = valid_record()
        record["run_id"] = "not-a-run-id"
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.validate_provenance(record)

    def test_missing_memory_id_rejected(self):
        record = valid_record()
        record["memory_injected"] = [{"memory_type": "task_topic"}]
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.validate_provenance(record)


class RunOneIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root / "repo")
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.context = run_provenance.capture_context(self.repo)
        self.sample = {
            "sample_id": "S1",
            "simulation": {"is_simulated": True},
            "gold_standard": {"simulation": True},
        }
        self.api = {
            "database": type("FakeDB", (), {"DATABASE_PATH": str(self.root / "fake.db")})(),
            "agent_plan_draft": __import__("agent_plan_draft"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def base_inputs(self, group):
        return {
            "user_message": "请帮我制定计划",
            "knowledge_results": [],
            "search_failed": False,
            "feedback_result": {"feedback": [], "summary": {}, "count": 0},
            "raw_feedback_result": {"feedback": [], "summary": {}, "count": 0},
            "empty_feedback": build_prompts.empty_feedback_result(7),
            "difficult": False,
            "short_term_block": "",
            "short_term_info": {"feedback_count": 0},
            "memory_result": None,
            "memory_tool": {"ok": True, "memories": []},
            "subject_memory_result": None,
            "subject_memory_tool": None,
            "memory_provenance": {"default": [], "subject": []},
            "subject_filter": None,
        }

    def run_one(self, group="A", inputs=None, draft_runner=None):
        return run_experiment.run_one(
            experiment_id="exp-test",
            sample_id="S1",
            group=group,
            repeat_index=0,
            inputs=inputs or self.base_inputs(group),
            api=self.api,
            run_dir=self.run_dir,
            recorder=run_experiment.UsageRecorder(),
            sampling={"temperature": 0.7, "top_p": 1.0, "max_tokens": 2048},
            now=datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc),
            plan_seq=1,
            round_index=0,
            seed=1,
            scheme="test",
            algorithm_version="test",
            plan_sha256="c" * 64,
            config_sha256="b" * 64,
            sample=self.sample,
            provenance_context=self.context,
            draft_runner=draft_runner
            or (lambda **kwargs: {"ok": True, "answer": "ok", "plan": {"title": "t"}}),
        )

    def test_strict_success_record(self):
        record = self.run_one()
        self.assertTrue(record["success"])
        run_provenance.validate_provenance(record, strict=True)
        self.assertRegex(record["run_id"], r"^20261124T170000Z-S1-A-r0-")
        self.assertEqual(record["base_sha"], self.context.base_sha)
        self.assertEqual(record["memory_injected"], [])
        self.assertFalse(record["short_term_block_present"])
        self.assertEqual(record["data_kind"], "simulated_learner_fixture")
        path = self.run_dir / record["injected_json_ref"]
        self.assertTrue(path.is_file())
        self.assertEqual(
            record["injected_json_sha256"],
            run_provenance.sha256_bytes(path.read_bytes()),
        )

    def test_strict_missing_memory_provenance_rejected(self):
        inputs = self.base_inputs("C")
        inputs["memory_result"] = {
            "count": 1,
            "memories": [
                {
                    "type": "task_topic",
                    "subject": "x",
                    "strength": 0.9,
                    "content": "内容",
                }
            ],
        }
        with self.assertRaises(run_provenance.ProvenanceError):
            self.run_one("C", inputs=inputs)

    def test_failed_run_still_has_provenance(self):
        def boom(**kwargs):
            raise RuntimeError("mock boom")

        record = self.run_one("A", draft_runner=boom)
        self.assertFalse(record["success"])
        run_provenance.validate_provenance(record, strict=True)
        self.assertEqual(record["error_type"], "runner_exception")


if __name__ == "__main__":
    unittest.main(verbosity=2)
