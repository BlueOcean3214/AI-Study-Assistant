"""Run-Provenance 生产器整改的独立测试（标准库 unittest）。

覆盖第二轮复审的 8 项缺陷：
1. collect_inputs -> group_inputs -> run_one 的真实记忆来源链；
2. short-term 三事实以组别最终块为准；
3. Git staged / unstaged / untracked / clean 与统一摘要；
4. injected.json 按 run 隔离；
5. provenance / 哈希校验在模型调用之前；
6. 批次运行中 Git 环境变化立即拒绝；
7. UUID4 run_id；
8. 严格 Schema 不接受 null 关键哈希。
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

try:
    import agent_plan_draft
except ImportError:  # pragma: no cover - 生产包应当在项目根
    agent_plan_draft = None

NOW = datetime(2026, 11, 24, 17, 0, tzinfo=timezone.utc)


def git(root, *args):
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.decode("utf-8", "replace").strip()


def make_repo(tmp, *, second_file=False):
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "audit@example.invalid")
    git(root, "config", "user.name", "Audit Test")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    if second_file:
        (root / "second.txt").write_text("second\n", encoding="utf-8")
        git(root, "add", "tracked.txt", "second.txt")
    else:
        git(root, "add", "tracked.txt")
    git(root, "commit", "-q", "-m", "base")
    return root


class FakeDatabase:
    """fixed_clock() 只替换 datetime；DATABASE_PATH 是 run_one 的真实保护检查。"""

    datetime = datetime

    def __init__(self, path):
        self.DATABASE_PATH = str(path)


class FakeMemoryService:
    def __init__(self, memories, subject_alias=None):
        self.memories = [dict(row) for row in memories]
        self.subject_alias = subject_alias or {}

    def _clean_text(self, text):
        return (text or "").strip()

    def normalize_subject(self, text):
        for key, label in self.subject_alias.items():
            if key in (text or ""):
                return key, label
        return text, text

    def retrieve(self, memory_type=None, subject=None, status="active", limit=5):
        rows = [
            dict(row)
            for row in self.memories
            if status is None or row.get("status") == status
        ]
        if subject is not None:
            rows = [row for row in rows if row.get("subject_key") == subject]
        return {"ok": True, "memories": rows[:limit]}


class FakeAgentTools:
    def __init__(
        self,
        memory_service,
        *,
        feedback_count=5,
        difficult=False,
        tool_override=None,
    ):
        self.memory_service = memory_service
        self.feedback_count = feedback_count
        self.difficult = difficult
        self.tool_override = tool_override

    def get_recent_feedback(self, days=7):
        return {
            "days": days,
            "count": self.feedback_count,
            "summary": {"count": self.feedback_count},
            "feedback": [
                {"task": "fake", "status": "completed", "reason": None}
            ]
            * self.feedback_count,
        }

    def _derive_difficult_previous_task(self):
        return bool(self.difficult), None

    def get_learning_memory(self, memory_type=None, subject=None, limit=5):
        if self.tool_override is not None:
            return {"ok": True, "memories": [dict(row) for row in self.tool_override]}
        rows = self.memory_service.retrieve(
            memory_type=memory_type, subject=subject, status="active", limit=limit
        )["memories"]
        return {
            "ok": True,
            "memories": [
                {
                    "type": row["memory_type"],
                    "subject": row.get("subject_label") or row["subject_key"],
                    "strength": row["strength"],
                    "content": row["content"],
                }
                for row in rows
            ],
        }


class FakeMemoryContext:
    def __init__(self, short_text, short_count):
        self.short_text = short_text
        self.short_count = short_count

    def collect_short_term(self, plan_date):
        return {
            "plan_date": plan_date,
            "feedback_count": self.short_count,
            "学习进度": {"已读取反馈数": self.short_count},
        }

    def render_short_term(self, data):
        return {"text": self.short_text, "info": dict(data)}


def raw_memory(
    memory_id,
    *,
    content,
    memory_type="task_topic",
    subject_key="一元二次方程",
    subject_label="一元二次方程练习",
    strength=0.9,
    status="active",
):
    return {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "subject_key": subject_key,
        "subject_label": subject_label,
        "strength": strength,
        "status": status,
        "content": content,
    }


def make_sample(sample_id="S1", request="请帮我制定一元二次方程练习计划"):
    return {
        "sample_id": sample_id,
        "current_user_request": request,
        "feedbacks": [{"task": "一元二次方程", "status": "completed", "reason": None}],
        "simulation": {"is_simulated": True, "source": "unit-test"},
        "gold_standard": {"simulation": True},
    }


def make_config():
    return {
        "recent_feedback_days": 7,
        "memory_limit": 5,
        "knowledge_results": [],
        "knowledge_search_failed": False,
    }


def make_api(
    tmp,
    memories,
    *,
    feedback_count=5,
    difficult=False,
    short_text="【学习者近期情况（程序统计，非模型总结）】\n{\"学习进度\": {\"已读取反馈数\": 5}}",
    short_count=None,
    subject_alias=None,
    tool_override=None,
):
    if agent_plan_draft is None:
        raise RuntimeError("test 需要项目根目录的 agent_plan_draft")
    memory_service = FakeMemoryService(memories, subject_alias=subject_alias)
    return {
        "database": FakeDatabase(Path(tmp) / "fake.db"),
        "memory_service": memory_service,
        "agent_tools": FakeAgentTools(
            memory_service,
            feedback_count=feedback_count,
            difficult=difficult,
            tool_override=tool_override,
        ),
        "memory_context": FakeMemoryContext(
            short_text, feedback_count if short_count is None else short_count
        ),
        "agent_plan_draft": agent_plan_draft,
    }


def run_id_value():
    return "20261124T170000Z-S1-A-r0-" + "0123456789abcdef" * 2


def valid_record():
    return {
        "run_id": run_id_value(),
        "base_sha": "a" * 40,
        "runtime_source_commit": "a" * 40,
        "runtime_worktree_clean": True,
        "runtime_patch_sha256": None,
        "runtime_staged_patch_sha256": None,
        "runtime_unstaged_patch_sha256": None,
        "runtime_untracked_sha256": None,
        "batch_context": {
            "runtime_source_commit": "a" * 40,
            "base_sha": "a" * 40,
            "captured_at": "2026-11-24T17:00:00+00:00",
            "project_root": "/tmp/repo",
        },
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
        "injected_json_ref": "injected/S1/A/" + run_id_value() + ".json",
        "result_schema_version": run_provenance.PROVENANCE_SCHEMA_VERSION,
        "experiment_design_version": run_provenance.DESIGN_VERSION,
        "generated_at": "2026-11-24T17:00:00+00:00",
        "model_alias": None,
        "model_snapshot_id": None,
        "model_snapshot_status": "not_provided_by_provider",
    }


class GitCaptureTests(unittest.TestCase):
    def test_clean_context_has_no_patches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            context = run_provenance.capture_context(root)
            self.assertTrue(context.runtime_worktree_clean)
            self.assertEqual(context.dirty_entries, ())
            for field in (
                "runtime_patch_sha256",
                "runtime_staged_patch_sha256",
                "runtime_unstaged_patch_sha256",
                "runtime_untracked_sha256",
            ):
                self.assertIsNone(getattr(context, field), field)
            self.assertEqual(context.project_root, str(root.resolve()))

    def test_unstaged_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            (root / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
            context = run_provenance.capture_context(root)
            self.assertFalse(context.runtime_worktree_clean)
            self.assertIsNone(context.runtime_staged_patch_sha256)
            self.assertIsNotNone(context.runtime_unstaged_patch_sha256)
            self.assertIsNone(context.runtime_untracked_sha256)
            self.assertIsNotNone(context.runtime_patch_sha256)

    def test_staged_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
            git(root, "add", "tracked.txt")
            context = run_provenance.capture_context(root)
            self.assertFalse(context.runtime_worktree_clean)
            self.assertIsNotNone(context.runtime_staged_patch_sha256)
            self.assertIsNone(context.runtime_unstaged_patch_sha256)
            self.assertIsNone(context.runtime_untracked_sha256)

    def test_staged_and_unstaged_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo", second_file=True)
            (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
            git(root, "add", "tracked.txt")
            (root / "second.txt").write_text("unstaged\n", encoding="utf-8")
            context = run_provenance.capture_context(root)
            self.assertIsNotNone(context.runtime_staged_patch_sha256)
            self.assertIsNotNone(context.runtime_unstaged_patch_sha256)
            self.assertIsNone(context.runtime_untracked_sha256)
            self.assertNotEqual(
                context.runtime_staged_patch_sha256,
                context.runtime_unstaged_patch_sha256,
            )

    def test_untracked_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            context = run_provenance.capture_context(root)
            self.assertFalse(context.runtime_worktree_clean)
            self.assertIsNone(context.runtime_staged_patch_sha256)
            self.assertIsNone(context.runtime_unstaged_patch_sha256)
            self.assertIsNotNone(context.runtime_untracked_sha256)

    def test_unstaged_hash_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            (root / "tracked.txt").write_text("one\n", encoding="utf-8")
            first = run_provenance.capture_context(root)
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            second = run_provenance.capture_context(root)
            self.assertNotEqual(first.runtime_patch_sha256, second.runtime_patch_sha256)
            self.assertNotEqual(
                first.runtime_unstaged_patch_sha256,
                second.runtime_unstaged_patch_sha256,
            )

    def test_staged_hash_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            (root / "tracked.txt").write_text("one\n", encoding="utf-8")
            git(root, "add", "tracked.txt")
            first = run_provenance.capture_context(root)
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            git(root, "add", "tracked.txt")
            second = run_provenance.capture_context(root)
            self.assertNotEqual(
                first.runtime_staged_patch_sha256,
                second.runtime_staged_patch_sha256,
            )

    def test_untracked_hash_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            (root / "new.txt").write_text("one\n", encoding="utf-8")
            first = run_provenance.capture_context(root)
            (root / "new.txt").write_text("two\n", encoding="utf-8")
            second = run_provenance.capture_context(root)
            self.assertNotEqual(
                first.runtime_untracked_sha256, second.runtime_untracked_sha256
            )

    def test_context_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp) / "repo")
            expected = run_provenance.capture_context(root)
            (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
            current = run_provenance.capture_context(root)
            with self.assertRaises(run_provenance.ProvenanceError):
                run_provenance.assert_context_matches(expected, current)

    def test_capture_rejects_non_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(run_provenance.ProvenanceError):
                run_provenance.capture_context(tmp)


class RunIdTests(unittest.TestCase):
    def test_uuid4_suffix_and_same_second_uniqueness(self):
        values = {
            run_provenance.make_run_id("S1", "D", 3, now=NOW) for _ in range(1000)
        }
        self.assertEqual(len(values), 1000)
        for value in values:
            self.assertRegex(value, r"^20261124T170000Z-S1-D-r3-[0-9a-f]{32}$")
            suffix = value.rsplit("-", 1)[1]
            self.assertEqual(suffix[12], "4", "run_id 后缀应为 UUID4")

    def test_does_not_use_time_for_uniqueness(self):
        first = run_provenance.make_run_id("S1", "A", 0, now=NOW)
        second = run_provenance.make_run_id("S1", "A", 0, now=NOW)
        self.assertNotEqual(first, second)


class KnowledgeTests(unittest.TestCase):
    def test_empty_and_actual_ids(self):
        self.assertEqual(
            run_provenance.extract_knowledge_result_ids({"knowledge_results": []}), []
        )
        self.assertEqual(
            run_provenance.extract_knowledge_result_ids(
                {"knowledge_results": [{"id": "kb-1"}, {"id": "kb-2"}]}
            ),
            ["kb-1", "kb-2"],
        )

    def test_missing_id_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.extract_knowledge_result_ids(
                {"knowledge_results": [{"content": "x"}]}
            )


class MemoryMappingTests(unittest.TestCase):
    def tool(self, **overrides):
        row = {
            "type": "task_topic",
            "subject": "一元二次方程练习",
            "strength": 0.9,
            "content": "内容 A",
        }
        row.update(overrides)
        return row

    def raw(self, **overrides):
        row = raw_memory("m-1", content="内容 A")
        row.update(overrides)
        return row

    def test_unique_mapping_preserves_id_status_key(self):
        tagged = run_provenance.tag_memory_provenance(
            {"ok": True, "memories": [self.tool()]},
            {"ok": True, "memories": [self.raw()]},
        )
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["memory_id"], "m-1")
        self.assertEqual(tagged[0]["status"], "active")
        self.assertEqual(tagged[0]["subject_key"], "一元二次方程")
        self.assertEqual(
            tagged[0]["content_sha256"],
            run_provenance.sha256_bytes("内容 A".encode("utf-8")),
        )

    def test_ambiguous_content_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.tag_memory_provenance(
                {"ok": True, "memories": [self.tool()]},
                {"ok": True, "memories": [self.raw(), self.raw(memory_id="m-2")]},
            )

    def test_tool_raw_count_mismatch_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.tag_memory_provenance(
                {"ok": True, "memories": [self.tool(), self.tool(content="B")]},
                {"ok": True, "memories": [self.raw()]},
            )

    def test_missing_runtime_status_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.tag_memory_provenance(
                {"ok": True, "memories": [self.tool()]},
                {"ok": True, "memories": [self.raw(status=None)]},
            )

    def test_no_memory_slot_returns_empty(self):
        self.assertEqual(
            run_provenance.select_injected_memories(
                {"memory": False}, {"memory_provenance": {"default": [self.raw()]}}
            ),
            [],
        )


class InjectedJsonTests(unittest.TestCase):
    def test_run_isolation_and_hash(self):
        rows = [raw_memory("m-1", content="内容 A")]
        clean_rows = [
            {
                "memory_id": "m-1",
                "memory_type": "task_topic",
                "subject_key": "一元二次方程",
                "strength": 0.9,
                "status": "active",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run1 = "20261124T170000Z-S1-C-r0-" + "1" * 32
            run2 = "20261124T170000Z-S1-C-r0-" + "2" * 32
            ref1, sha1 = run_provenance.write_injected_json(
                tmp, run1, "S1", "C", 0, "2026-11-24T17:00:00+00:00", clean_rows
            )
            ref2, sha2 = run_provenance.write_injected_json(
                tmp, run2, "S1", "C", 0, "2026-11-24T17:00:00+00:00", clean_rows
            )
            self.assertNotEqual(ref1, ref2)
            self.assertNotEqual(sha1, sha2)
            path1 = Path(tmp) / ref1
            path2 = Path(tmp) / ref2
            self.assertTrue(path1.is_file())
            self.assertTrue(path2.is_file())
            self.assertEqual(run_provenance.sha256_bytes(path1.read_bytes()), sha1)
            payload = json.loads(path1.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], run1)
            self.assertEqual(payload["sample_id"], "S1")
            self.assertEqual(payload["group"], "C")
            self.assertEqual(payload["repeat_index"], 0)
            self.assertEqual(payload["memory_injected"], clean_rows)
            self.assertFalse(path1.with_name(path1.name + ".tmp").exists())

    def test_same_run_id_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = "20261124T170000Z-S1-C-r0-" + "3" * 32
            run_provenance.write_injected_json(
                tmp, run, "S1", "C", 0, "2026-11-24T17:00:00+00:00", []
            )
            with self.assertRaises(run_provenance.ProvenanceError):
                run_provenance.write_injected_json(
                    tmp, run, "S1", "C", 0, "2026-11-24T17:00:00+00:00", []
                )


class ShortTermTests(unittest.TestCase):
    def test_slot_disabled_ignores_nonempty_input(self):
        facts = run_provenance.short_term_facts(
            {"short_term_block": "原始上下文", "short_term_info": {"feedback_count": 5}},
            {"short_term": False},
            None,
        )
        self.assertFalse(facts["short_term_slot_enabled"])
        self.assertFalse(facts["short_term_block_present"])
        self.assertTrue(facts["short_term_feedback_history_present"])

    def test_slot_enabled_and_selected_block_present(self):
        facts = run_provenance.short_term_facts(
            {"short_term_block": "D", "short_term_info": {"feedback_count": 5}},
            {"short_term": True},
            "实际选择的块",
        )
        self.assertTrue(facts["short_term_slot_enabled"])
        self.assertTrue(facts["short_term_block_present"])
        self.assertTrue(facts["short_term_feedback_history_present"])

    def test_slot_enabled_but_selected_block_empty(self):
        facts = run_provenance.short_term_facts(
            {"short_term_block": "非空原始块", "short_term_info": {"feedback_count": 5}},
            {"short_term": True},
            "",
        )
        self.assertTrue(facts["short_term_slot_enabled"])
        self.assertFalse(facts["short_term_block_present"])
        self.assertTrue(facts["short_term_feedback_history_present"])

    def test_zero_feedback_but_block_present(self):
        facts = run_provenance.short_term_facts(
            {"short_term_info": {"feedback_count": 0}},
            {"short_term": True},
            "区块存在但反馈为零",
        )
        self.assertTrue(facts["short_term_block_present"])
        self.assertFalse(facts["short_term_feedback_history_present"])

    def test_feedback_positive_but_block_absent(self):
        facts = run_provenance.short_term_facts(
            {"short_term_info": {"feedback_count": 3}},
            {"short_term": True},
            None,
        )
        self.assertFalse(facts["short_term_block_present"])
        self.assertTrue(facts["short_term_feedback_history_present"])

    def test_missing_count_strict_rejected(self):
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.short_term_facts(
                {"short_term_block": "x"}, {"short_term": True}, "x", strict=True
            )


class DataKindTests(unittest.TestCase):
    def test_simulated(self):
        self.assertEqual(
            run_provenance.resolve_data_kind(
                {"simulation": {"is_simulated": True}, "gold_standard": {"simulation": True}}
            ),
            "simulated_learner_fixture",
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


class ValidationTests(unittest.TestCase):
    def test_valid_record(self):
        run_provenance.validate_provenance(valid_record())

    def test_required_null_hashes_rejected(self):
        for field in (
            "config_sha256",
            "plan_sha256",
            "prompt_sha256",
            "injected_json_sha256",
            "injected_json_ref",
        ):
            record = valid_record()
            record[field] = None
            with self.subTest(field=field):
                with self.assertRaises(run_provenance.ProvenanceError):
                    run_provenance.validate_provenance(record)

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

    def test_dirty_without_patch_rejected(self):
        record = valid_record()
        record["runtime_worktree_clean"] = False
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

    def test_model_snapshot_consistency(self):
        record = valid_record()
        record["model_snapshot_status"] = "provided"
        with self.assertRaises(run_provenance.ProvenanceError):
            run_provenance.validate_provenance(record)

    def test_batch_context_mismatch_rejected(self):
        record = valid_record()
        record["batch_context"]["base_sha"] = "b" * 40
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
        self.moment = NOW

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, sample, api, config=None):
        return build_prompts.collect_inputs(
            sample, config or make_config(), api, self.moment
        )

    def run_chain(
        self,
        group,
        sample,
        api,
        *,
        inputs=None,
        draft_runner=None,
        config_sha256="b" * 64,
        plan_sha256="c" * 64,
    ):
        calls = []

        def counting_runner(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "answer": "ok", "plan": {"title": "t"}}

        record = run_experiment.run_one(
            experiment_id="exp-test",
            sample_id=sample["sample_id"],
            group=group,
            repeat_index=0,
            inputs=inputs if inputs is not None else self.collect(sample, api),
            api=api,
            run_dir=self.run_dir,
            recorder=run_experiment.UsageRecorder(),
            sampling={"temperature": 0.7, "top_p": 1.0, "max_tokens": 2048},
            now=self.moment,
            plan_seq=1,
            round_index=0,
            seed=1,
            scheme="test",
            algorithm_version="test",
            plan_sha256=plan_sha256,
            config_sha256=config_sha256,
            sample=sample,
            provenance_context=self.context,
            draft_runner=draft_runner or counting_runner,
        )
        return record, calls

    def default_api(self, **kwargs):
        return make_api(
            self.root,
            [raw_memory("m-1", content="内容 A")],
            short_text="短期上下文块",
            **kwargs,
        )

    def test_ab_cd_chain_and_memory_slots(self):
        api = self.default_api()
        sample = make_sample()
        for group, expected_memory in (("A", 0), ("B", 0), ("C", 1), ("D", 1)):
            with self.subTest(group=group):
                record, calls = self.run_chain(group, sample, api)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(record["memory_injected"]), expected_memory)
                self.assertEqual(record["short_term_block_present"], group in {"B", "D"})
                self.assertFalse(record["difficult_flag"])
                self.assertEqual(record["run_id"].rsplit("-", 1)[1][12], "4")
                run_provenance.validate_provenance(record)
                path = self.run_dir / record["injected_json_ref"]
                self.assertTrue(path.is_file())
                self.assertEqual(
                    run_provenance.sha256_bytes(path.read_bytes()),
                    record["injected_json_sha256"],
                )

    def test_subject_groups_use_subject_memory(self):
        memories = [
            raw_memory(
                f"m-{index}",
                content=f"内容 {index}",
                subject_key=key,
                subject_label=key,
            )
            for index, key in enumerate(
                ["线性代数", "线性代数", "线性代数", "概率论", "概率论"]
            )
        ]
        api = make_api(
            self.root,
            memories,
            short_text="短期上下文块",
            subject_alias={"概率论": "概率论"},
        )
        sample = make_sample("S5", request="请帮我复习概率论")
        sample["feedbacks"] = [
            {"task": "概率论", "status": "completed", "reason": None}
        ]
        for group in ("C-subj", "D-subj"):
            with self.subTest(group=group):
                inputs = self.collect(sample, api)
                self.assertEqual(
                    len(inputs["subject_memory_result"]["memories"]), 2
                )
                record, _ = self.run_chain(group, sample, api, inputs=inputs)
                self.assertEqual(len(record["memory_injected"]), 2)
                self.assertEqual(
                    {item["subject_key"] for item in record["memory_injected"]},
                    {"概率论"},
                )

    def test_empty_memory_sample(self):
        api = make_api(self.root, [], feedback_count=0, short_text="")
        sample = make_sample("S7-empty", request="请帮我制定计划")
        inputs = self.collect(sample, api)
        self.assertEqual(inputs["memory_provenance"], {"default": [], "subject": []})
        record, _ = self.run_chain("D", sample, api, inputs=inputs)
        self.assertEqual(record["memory_injected"], [])
        self.assertTrue(record["short_term_slot_enabled"])
        self.assertFalse(record["short_term_block_present"])
        self.assertFalse(record["short_term_feedback_history_present"])

    def test_difficult_flag_uses_group_slots(self):
        api = make_api(
            self.root,
            [raw_memory("m-1", content="内容 A")],
            difficult=True,
            short_text="短期上下文块",
        )
        sample = make_sample("S7", request="请帮我制定计划")
        for group, expected in (("A", False), ("B", True), ("C", False), ("D", True)):
            with self.subTest(group=group):
                record, _ = self.run_chain(group, sample, api)
                self.assertEqual(record["difficult_flag"], expected)

    def test_duplicate_content_is_rejected_by_collect_inputs(self):
        api = make_api(
            self.root,
            [
                raw_memory("m-1", content="重复内容"),
                raw_memory("m-2", content="重复内容"),
            ],
        )
        with self.assertRaises(run_provenance.ProvenanceError):
            self.collect(make_sample(), api)

    def test_tool_result_mismatch_is_rejected(self):
        api = make_api(
            self.root,
            [raw_memory("m-1", content="内容 A")],
            tool_override=[
                {
                    "type": "task_topic",
                    "subject": "x",
                    "strength": 0.9,
                    "content": "内容 A",
                },
                {
                    "type": "task_topic",
                    "subject": "y",
                    "strength": 0.9,
                    "content": "额外内容",
                },
            ],
        )
        with self.assertRaises(run_provenance.ProvenanceError):
            self.collect(make_sample(), api)

    def test_missing_memory_provenance_blocks_model(self):
        api = self.default_api()
        sample = make_sample()
        inputs = self.collect(sample, api)
        del inputs["memory_provenance"]
        calls = []
        with self.assertRaises(run_provenance.ProvenanceError):
            self.run_chain(
                "C",
                sample,
                api,
                inputs=inputs,
                draft_runner=lambda **kwargs: calls.append(1),
            )
        self.assertEqual(calls, [])

    def test_missing_config_hash_blocks_model(self):
        api = self.default_api()
        sample = make_sample()
        calls = []
        with self.assertRaises(run_provenance.ProvenanceError):
            self.run_chain(
                "A",
                sample,
                api,
                config_sha256=None,
                draft_runner=lambda **kwargs: calls.append(1),
            )
        self.assertEqual(calls, [])

    def test_git_change_after_batch_capture_blocks_model(self):
        api = self.default_api()
        sample = make_sample()
        inputs = self.collect(sample, api)
        (self.repo / "tracked.txt").write_text("changed mid batch\n", encoding="utf-8")
        calls = []
        with self.assertRaises(run_provenance.ProvenanceError):
            self.run_chain(
                "A",
                sample,
                api,
                inputs=inputs,
                draft_runner=lambda **kwargs: calls.append(1),
            )
        self.assertEqual(calls, [])

    def test_model_exception_still_has_full_provenance(self):
        api = self.default_api()
        sample = make_sample()

        def boom(**kwargs):
            raise RuntimeError("mock boom")

        record, _ = self.run_chain("A", sample, api, draft_runner=boom)
        self.assertFalse(record["success"])
        self.assertEqual(record["error_type"], "runner_exception")
        self.assertEqual(record["batch_context"]["base_sha"], self.context.base_sha)
        self.assertTrue(record["injected_json_ref"])
        self.assertTrue(record["injected_json_sha256"])
        run_provenance.validate_provenance(record)

    def test_each_record_references_own_injected_file(self):
        api = self.default_api()
        sample = make_sample()
        inputs = self.collect(sample, api)
        first, _ = self.run_chain("C", sample, api, inputs=inputs)
        second, _ = self.run_chain("C", sample, api, inputs=inputs)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["injected_json_ref"], second["injected_json_ref"])
        self.assertTrue((self.run_dir / first["injected_json_ref"]).is_file())
        self.assertTrue((self.run_dir / second["injected_json_ref"]).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
