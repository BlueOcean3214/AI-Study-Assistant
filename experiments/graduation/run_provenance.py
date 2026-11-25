"""运行时来源采集（Run Provenance）。

冻结设计 §7.1 的七个字段在历史记录中缺失，原因不是统计层，而是生产器
从未在运行时采集它们。本模块把"运行时真实对象"固定成一条可校验记录：

- run_id：每次 run 开始时生成，只在本进程内唯一；
- base_sha / runtime_source_commit / runtime_worktree_clean / runtime_patch_sha256：
  在发起任何模型请求之前从 Git 读取，工作树不干净时保留差异证据；
- knowledge_result_ids：来自本次实际检索结果对象；
- memory_injected：来自本次实际注入对象（memory_id / 类型 / 主题 / 强度 / 状态），
  不允许由 Prompt 文本反推；
- short_term_slot_enabled / short_term_block_present /
  short_term_feedback_history_present：三个独立事实，不合并成一个 Boolean；
- difficult_flag：来自组槽位最终结果；
- data_kind：来自样本创建阶段的显式 simulation 标记；
- prompt_sha256 / config_sha256 / plan_sha256 / injected_json_sha256：
  对实际发送内容与计划/注入清单做绑定。

写记录前必须通过 validate_provenance()。缺失关键字段时抛 ProvenanceError，
调用方应 fail-closed，不写一条"看起来完整"的记录。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROVENANCE_SCHEMA_VERSION = "graduation-run-provenance/v1"
DESIGN_VERSION = "v4.5"

MEMORY_TYPES = ("difficulty", "task_performance", "time_pattern", "task_topic")
DATA_KINDS = ("simulated_learner_fixture", "real_user_data")

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_.-]+-[A-Za-z0-9_.-]+-r[0-9]+-[0-9a-f]{32}$")


class ProvenanceError(RuntimeError):
    """运行时来源不可证明；调用方必须拒绝写正式记录。"""


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_git(project_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ProvenanceError(f"无法执行 git {' '.join(args)}：{error}") from error

    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise ProvenanceError(f"git {' '.join(args)} 失败：{message}")
    return completed.stdout


@dataclass(frozen=True)
class ProvenanceContext:
    runtime_source_commit: str
    runtime_worktree_clean: bool
    runtime_patch_sha256: str | None
    dirty_entries: tuple[str, ...]
    captured_at: str
    runtime_staged_patch_sha256: str | None = None
    runtime_unstaged_patch_sha256: str | None = None
    runtime_untracked_sha256: str | None = None
    project_root: str = ""

    @property
    def base_sha(self) -> str:
        return self.runtime_source_commit

    def to_dict(self) -> dict:
        return {
            "runtime_source_commit": self.runtime_source_commit,
            "base_sha": self.base_sha,
            "runtime_worktree_clean": self.runtime_worktree_clean,
            "runtime_patch_sha256": self.runtime_patch_sha256,
            "runtime_staged_patch_sha256": self.runtime_staged_patch_sha256,
            "runtime_unstaged_patch_sha256": self.runtime_unstaged_patch_sha256,
            "runtime_untracked_sha256": self.runtime_untracked_sha256,
            "dirty_entries": list(self.dirty_entries),
            "captured_at": self.captured_at,
            "project_root": self.project_root,
        }


def assert_context_matches(expected: ProvenanceContext, current: ProvenanceContext) -> None:
    """批次开始时与每个 run 开始前的环境必须完全一致。

    只比较源码环境事实，不比较 captured_at（每次采集时间必然不同）。
    """

    fields = (
        "runtime_source_commit",
        "runtime_worktree_clean",
        "runtime_patch_sha256",
        "runtime_staged_patch_sha256",
        "runtime_unstaged_patch_sha256",
        "runtime_untracked_sha256",
        "dirty_entries",
        "project_root",
    )
    changed = [field for field in fields if getattr(expected, field) != getattr(current, field)]
    if changed:
        raise ProvenanceError(
            "批次运行期间源码环境发生变化，拒绝继续："
            + ", ".join(changed)
            + f"（批次 captured_at={expected.captured_at}，"
            + f"当前 captured_at={current.captured_at}）"
        )


def capture_context(project_root=None, now=None) -> ProvenanceContext:
    """在运行开始时采集 Git 事实。

    base_sha 只取 HEAD；工作树是否干净单独记录。staged / unstaged diff
    分别计算 SHA-256，统一 patch 摘要还覆盖 git status 与每个未跟踪文件内容。
    未跟踪文件也会让 runtime_worktree_clean=false。

    注意：这里只保存摘要，不保存 patch 正文；仅凭 runtime_patch_sha256
    无法还原当时的源代码。
    """

    root = Path(project_root).resolve() if project_root else Path(__file__).resolve().parents[2]
    head = _run_git(root, "rev-parse", "HEAD").decode("ascii", "replace").strip()
    if not _HEX40.fullmatch(head):
        raise ProvenanceError(f"git rev-parse HEAD 不是 40 位 SHA：{head!r}")

    status = _run_git(root, "status", "--porcelain").decode("utf-8", "replace")
    dirty_entries = tuple(line for line in status.splitlines() if line.strip())
    clean = not dirty_entries

    patch_sha = None
    staged_sha = None
    unstaged_sha = None
    untracked_sha = None

    if not clean:
        # staged 与 unstaged 必须分别计算，否则 git add 后只改暂存区会被漏掉。
        staged = _run_git(root, "diff", "--cached", "--binary", "--no-color")
        unstaged = _run_git(root, "diff", "--binary", "--no-color")
        staged_sha = sha256_bytes(staged) if staged else None
        unstaged_sha = sha256_bytes(unstaged) if unstaged else None

        untracked = _run_git(root, "ls-files", "--others", "--exclude-standard").decode(
            "utf-8", "replace"
        )
        untracked_paths = sorted(line for line in untracked.splitlines() if line.strip())

        # 统一摘要覆盖：status + staged diff + unstaged diff + 每个未跟踪文件内容。
        # 只保存摘要，不能仅凭 runtime_patch_sha256 还原源代码。
        digest = hashlib.sha256()
        digest.update(b"status\0")
        digest.update(status.encode("utf-8"))
        digest.update(b"\0staged\0")
        digest.update(staged)
        digest.update(b"\0unstaged\0")
        digest.update(unstaged)
        digest.update(b"\0")
        untracked_digest = hashlib.sha256()
        for relative in untracked_paths:
            path = root / relative
            content = path.read_bytes() if path.is_file() else b""
            untracked_digest.update(relative.encode("utf-8"))
            untracked_digest.update(b"\0")
            untracked_digest.update(content)
            untracked_digest.update(b"\0")
            digest.update(b"untracked\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        if untracked_paths:
            untracked_sha = untracked_digest.hexdigest()
        patch_sha = digest.hexdigest()

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    captured_at = moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    return ProvenanceContext(
        runtime_source_commit=head,
        runtime_worktree_clean=clean,
        runtime_patch_sha256=patch_sha,
        dirty_entries=dirty_entries,
        captured_at=captured_at,
        runtime_staged_patch_sha256=staged_sha,
        runtime_unstaged_patch_sha256=unstaged_sha,
        runtime_untracked_sha256=untracked_sha,
        project_root=str(root),
    )


def make_run_id(sample_id: str, group: str, repeat_index: int, now=None) -> str:
    """生成一次运行的唯一 ID；它不是历史记录的 run_id，也不能事后补写。"""

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sample_id)).strip("_") or "sample"
    group_text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(group)).strip("_") or "group"
    # 完整 UUID4（32 hex），同一秒内大量生成也不依赖时间戳保证唯一性。
    run_id = f"{stamp}-{sample}-{group_text}-r{int(repeat_index)}-{uuid.uuid4().hex}"
    if not _RUN_ID.fullmatch(run_id):
        raise ProvenanceError(f"run_id 形态不合法：{run_id!r}")
    return run_id


def extract_knowledge_result_ids(inputs) -> list[str]:
    results = inputs.get("knowledge_results")
    if results is None:
        raise ProvenanceError("inputs 缺少 knowledge_results，无法记录实际检索对象")
    if not isinstance(results, list):
        raise ProvenanceError("knowledge_results 必须是 list")
    ids = []
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            raise ProvenanceError(f"knowledge_results[{index}] 不是对象，无法取得稳定 ID")
        value = item.get("id")
        if not isinstance(value, str) or not value.strip():
            raise ProvenanceError(
                f"knowledge_results[{index}] 缺少稳定 id；不能用文本或序号冒充知识结果 ID"
            )
        ids.append(value)
    return ids


def _memory_key(entry):
    return (
        entry.get("type") if "type" in entry else entry.get("memory_type"),
        entry.get("subject") if "subject" in entry else entry.get("subject_key"),
        entry.get("strength"),
        entry.get("content"),
    )


def tag_memory_provenance(tool_result, raw_result) -> list[dict]:
    """把 Tool 返回的已注入条目逐条映射到运行时原始记忆对象。

    Tool 故意剥离 memory_id / status；原始对象只在本模块内使用，不进入 Prompt。
    映射以 content 唯一匹配，再用 type/strength 交叉校验；任何歧义直接失败。
    """

    tool_rows = []
    if isinstance(tool_result, dict):
        tool_rows = tool_result.get("memories") or []
    raw_rows = []
    if isinstance(raw_result, dict):
        raw_rows = raw_result.get("memories") or []

    if len(raw_rows) != len(tool_rows):
        raise ProvenanceError(
            "Tool 返回的注入条目数与运行时原始对象不一致："
            f"tool={len(tool_rows)} raw={len(raw_rows)}"
        )

    by_content = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            raise ProvenanceError("运行时原始记忆对象不是 dict")
        by_content.setdefault(row.get("content"), []).append(row)

    tagged = []
    for index, entry in enumerate(tool_rows):
        if not isinstance(entry, dict):
            raise ProvenanceError(f"memory_result.memories[{index}] 不是对象")
        content = entry.get("content")
        candidates = [
            row
            for row in by_content.get(content, [])
            if row.get("memory_type") == entry.get("type")
            and row.get("strength") == entry.get("strength")
        ]
        if len(candidates) != 1:
            raise ProvenanceError(
                "无法把注入记忆唯一映射到运行时对象："
                f"content={content!r} candidates={len(candidates)}"
            )
        row = candidates[0]
        for field in ("memory_id", "memory_type", "subject_key", "strength", "status"):
            if row.get(field) is None:
                raise ProvenanceError(f"运行时记忆对象缺少 {field}：content={content!r}")
        tagged.append(
            {
                "memory_id": str(row["memory_id"]),
                "memory_type": str(row["memory_type"]),
                "subject_key": str(row["subject_key"]),
                "strength": float(row["strength"]),
                "status": str(row["status"]),
                "content_sha256": sha256_bytes(str(row.get("content") or "").encode("utf-8")),
            }
        )
    return tagged


def select_injected_memories(group_slots, inputs, strict=True) -> list[dict]:
    if not group_slots.get("memory"):
        return []

    scope = "subject" if group_slots.get("memory_scope") == "subject" else "default"
    provenance = inputs.get("memory_provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get(scope), list):
        rows = provenance[scope]
        if strict:
            actual = inputs.get(
                "subject_memory_result" if scope == "subject" else "memory_result"
            )
            tool_rows = (actual or {}).get("memories") or []
            if len(rows) != len(tool_rows):
                raise ProvenanceError(
                    "注入来源条目数与实际注入不一致："
                    f"provenance={len(rows)} actual={len(tool_rows)}"
                )
            expected = {
                (
                    item.get("type"),
                    item.get("strength"),
                    sha256_bytes(str(item.get("content") or "").encode("utf-8")),
                )
                for item in tool_rows
            }
            recorded = {
                (
                    item.get("memory_type"),
                    item.get("strength"),
                    item.get("content_sha256"),
                )
                for item in rows
            }
            if expected != recorded:
                raise ProvenanceError(
                    "注入来源条目与实际注入内容不一致（type/strength/content 指纹）"
                )
        return [dict(item) for item in rows]

    if strict:
        raise ProvenanceError(
            "inputs 缺少运行时 memory_provenance；不能用 Prompt 文本反推 memory_injected"
        )

    # 仅用于旧测试/直接调用：Tool 条目本来就没有 memory_id/status，明确留空。
    result = inputs.get("subject_memory_result" if scope == "subject" else "memory_result")
    rows = (result or {}).get("memories") or []
    return [
        {
            "memory_id": None,
            "memory_type": row.get("type"),
            "subject_key": row.get("subject"),
            "strength": row.get("strength"),
            "status": None,
        }
        for row in rows
    ]


def short_term_facts(inputs, group_slots, selected_block, strict=True) -> dict:
    """三个独立事实必须以组别最终选择的 block 为准，而不是原始 inputs。

    selected_block 必须来自 build_prompts.group_inputs(group, inputs)
    ["short_term_block"]：槽位关闭时它是 None，槽位开启但上下文为空时是空串。
    """

    info = inputs.get("short_term_info")
    count = None
    if isinstance(info, dict):
        count = info.get("feedback_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        if strict:
            raise ProvenanceError("inputs.short_term_info.feedback_count 缺失或不合法")
        count = None
    slot_enabled = bool(group_slots.get("short_term"))
    block_present = bool(
        slot_enabled and isinstance(selected_block, str) and selected_block.strip()
    )
    return {
        "short_term_slot_enabled": slot_enabled,
        "short_term_block_present": block_present,
        "short_term_feedback_history_present": None if count is None else count > 0,
        "short_term_feedback_count": count,
    }


def resolve_data_kind(sample, strict=True) -> str:
    if not isinstance(sample, dict):
        if strict:
            raise ProvenanceError("缺少样本对象，无法证明 data_kind")
        return "simulated_learner_fixture"
    explicit = sample.get("data_kind")
    if isinstance(explicit, str) and explicit.strip():
        if explicit not in DATA_KINDS:
            raise ProvenanceError(f"样本 data_kind 不在允许值内：{explicit!r}")
        return explicit
    simulation = sample.get("simulation")
    if not isinstance(simulation, dict):
        if strict:
            raise ProvenanceError("样本既无 data_kind 也无 simulation 标记")
        return "simulated_learner_fixture"
    if simulation.get("is_simulated") is True:
        gold = sample.get("gold_standard")
        if not isinstance(gold, dict) or gold.get("simulation") is not True:
            raise ProvenanceError("样本 simulation.is_simulated=true 但 gold_standard.simulation 不是 true")
        return "simulated_learner_fixture"
    if simulation.get("is_simulated") is False:
        return "real_user_data"
    raise ProvenanceError("样本 simulation.is_simulated 不是显式布尔值")


def injected_json_path(run_dir, run_id, sample_id, group) -> Path:
    safe_sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sample_id)) or "sample"
    safe_group = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(group)) or "group"
    safe_run = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run_id)) or "run"
    return Path(run_dir) / "injected" / safe_sample / safe_group / f"{safe_run}.json"


def write_injected_json(
    run_dir,
    run_id,
    sample_id,
    group,
    repeat_index,
    generated_at,
    memories,
) -> tuple[str, str]:
    """按 run 隔离原子写注入清单，返回 (相对路径, sha256)。

    路径为 injected/<sample>/<group>/<run_id>.json，保证同一 sample/group
    的多次重复运行不会互相覆盖。文件内容只含运行时对象字段。
    """

    path = injected_json_path(run_dir, run_id, sample_id, group)
    if path.exists():
        raise ProvenanceError(f"注入清单已存在，拒绝覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "graduation-injected-memory/v1",
        "schema_version": "graduation-injected-memory/v1",
        "run_id": run_id,
        "sample_id": sample_id,
        "group": group,
        "repeat_index": repeat_index,
        "generated_at": generated_at,
        "memory_injected": list(memories),
    }
    data = (canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temporary.write_bytes(data)
    temporary.replace(path)
    try:
        relative = path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    except ValueError:
        relative = path.name
    return relative, sha256_bytes(data)


def build_provenance(
    *,
    context: ProvenanceContext,
    run_id: str,
    sample_id: str,
    group: str,
    repeat_index: int,
    group_slots: dict,
    inputs: dict,
    sample,
    selected_short_term_block,
    selected_difficult_flag,
    prompt_sha256: str,
    config_sha256: str | None,
    plan_sha256: str | None,
    generated_at: str,
    model_alias: str | None,
    injected_json_ref: str | None = None,
    injected_json_sha256: str | None = None,
    model_snapshot_id: str | None = None,
    model_snapshot_status: str | None = None,
    strict=True,
) -> dict:
    knowledge_ids = extract_knowledge_result_ids(inputs)
    memories = select_injected_memories(group_slots, inputs, strict=strict)
    facts = short_term_facts(
        inputs, group_slots, selected_short_term_block, strict=strict
    )
    data_kind = resolve_data_kind(sample, strict=strict)
    if model_snapshot_status is None:
        model_snapshot_status = (
            "provided" if model_snapshot_id else "not_provided_by_provider"
        )

    return {
        "run_id": run_id,
        "base_sha": context.base_sha,
        "runtime_source_commit": context.runtime_source_commit,
        "runtime_worktree_clean": context.runtime_worktree_clean,
        "runtime_patch_sha256": context.runtime_patch_sha256,
        "runtime_staged_patch_sha256": context.runtime_staged_patch_sha256,
        "runtime_unstaged_patch_sha256": context.runtime_unstaged_patch_sha256,
        "runtime_untracked_sha256": context.runtime_untracked_sha256,
        "batch_context": context.to_dict(),
        "knowledge_result_ids": knowledge_ids,
        "memory_injected": memories,
        "short_term_slot_enabled": facts["short_term_slot_enabled"],
        "short_term_block_present": facts["short_term_block_present"],
        "short_term_feedback_history_present": facts["short_term_feedback_history_present"],
        "short_term_feedback_count": facts["short_term_feedback_count"],
        "difficult_flag": bool(selected_difficult_flag),
        "data_kind": data_kind,
        "config_sha256": config_sha256,
        "plan_sha256": plan_sha256,
        "prompt_sha256": prompt_sha256,
        "injected_json_sha256": injected_json_sha256,
        "injected_json_ref": injected_json_ref,
        "result_schema_version": PROVENANCE_SCHEMA_VERSION,
        "experiment_design_version": DESIGN_VERSION,
        "generated_at": generated_at,
        "model_alias": model_alias,
        "model_snapshot_id": model_snapshot_id,
        "model_snapshot_status": model_snapshot_status,
    }


def validate_provenance(record, strict=True) -> None:
    """正式记录写盘前的 fail-closed 校验。"""

    required = (
        "run_id",
        "base_sha",
        "runtime_source_commit",
        "runtime_worktree_clean",
        "runtime_patch_sha256",
        "runtime_staged_patch_sha256",
        "runtime_unstaged_patch_sha256",
        "runtime_untracked_sha256",
        "batch_context",
        "knowledge_result_ids",
        "memory_injected",
        "short_term_slot_enabled",
        "short_term_block_present",
        "short_term_feedback_history_present",
        "short_term_feedback_count",
        "difficult_flag",
        "data_kind",
        "config_sha256",
        "plan_sha256",
        "prompt_sha256",
        "injected_json_sha256",
        "injected_json_ref",
        "result_schema_version",
        "experiment_design_version",
        "generated_at",
        "model_alias",
        "model_snapshot_id",
        "model_snapshot_status",
    )
    missing = [field for field in required if field not in record]
    if missing:
        raise ProvenanceError(f"正式记录缺少来源字段：{', '.join(missing)}")

    run_id = record["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ProvenanceError(f"run_id 不合法：{run_id!r}")
    for field in ("base_sha", "runtime_source_commit"):
        value = record[field]
        if not isinstance(value, str) or not _HEX40.fullmatch(value):
            raise ProvenanceError(f"{field} 不是 40 位 Git SHA：{value!r}")
    if not isinstance(record["runtime_worktree_clean"], bool):
        raise ProvenanceError("runtime_worktree_clean 必须是布尔值")
    if record["runtime_patch_sha256"] is not None and not _HEX64.fullmatch(
        str(record["runtime_patch_sha256"])
    ):
        raise ProvenanceError("runtime_patch_sha256 不是 64 位 SHA")
    for field in (
        "runtime_staged_patch_sha256",
        "runtime_unstaged_patch_sha256",
        "runtime_untracked_sha256",
    ):
        value = record[field]
        if value is not None and (
            not isinstance(value, str) or not _HEX64.fullmatch(value)
        ):
            raise ProvenanceError(f"{field} 不是 64 位 SHA")
    dirty_patch_fields = (
        "runtime_patch_sha256",
        "runtime_staged_patch_sha256",
        "runtime_unstaged_patch_sha256",
        "runtime_untracked_sha256",
    )
    if record["runtime_worktree_clean"]:
        if any(record[field] is not None for field in dirty_patch_fields):
            raise ProvenanceError("工作树干净时不应有 patch 摘要")
    else:
        if record["runtime_patch_sha256"] is None:
            raise ProvenanceError("工作树不干净时必须记录统一 patch 摘要")
        if all(record[field] is None for field in dirty_patch_fields[1:]):
            raise ProvenanceError("工作树不干净但没有任何 staged/unstaged/untracked 摘要")

    batch = record["batch_context"]
    if not isinstance(batch, dict):
        raise ProvenanceError("batch_context 必须是对象")
    for field in ("runtime_source_commit", "base_sha", "captured_at"):
        if not isinstance(batch.get(field), str) or not batch.get(field).strip():
            raise ProvenanceError(f"batch_context 缺少 {field}")
    if batch["base_sha"] != record["base_sha"]:
        raise ProvenanceError("batch_context.base_sha 与记录不一致")
    if batch["runtime_source_commit"] != record["runtime_source_commit"]:
        raise ProvenanceError("batch_context.runtime_source_commit 与记录不一致")

    knowledge = record["knowledge_result_ids"]
    if not isinstance(knowledge, list) or any(not isinstance(item, str) for item in knowledge):
        raise ProvenanceError("knowledge_result_ids 必须是字符串列表")

    memories = record["memory_injected"]
    if not isinstance(memories, list):
        raise ProvenanceError("memory_injected 必须是列表")
    for index, item in enumerate(memories):
        if not isinstance(item, dict):
            raise ProvenanceError(f"memory_injected[{index}] 不是对象")
        for field in ("memory_id", "memory_type", "subject_key", "strength", "status"):
            if field not in item:
                raise ProvenanceError(f"memory_injected[{index}] 缺少 {field}")
        if not isinstance(item["memory_id"], str) or not item["memory_id"].strip():
            raise ProvenanceError(f"memory_injected[{index}].memory_id 不合法")
        if item["memory_type"] not in MEMORY_TYPES:
            raise ProvenanceError(f"memory_injected[{index}].memory_type 不合法")
        if not isinstance(item["subject_key"], str) or not item["subject_key"].strip():
            raise ProvenanceError(f"memory_injected[{index}].subject_key 不合法")
        if not isinstance(item["strength"], (int, float)) or isinstance(item["strength"], bool):
            raise ProvenanceError(f"memory_injected[{index}].strength 不是数值")
        if not 0.0 <= float(item["strength"]) <= 1.0:
            raise ProvenanceError(f"memory_injected[{index}].strength 越界")
        if not isinstance(item["status"], str) or not item["status"].strip():
            raise ProvenanceError(f"memory_injected[{index}].status 不合法")
        if "content_sha256" in item and not _HEX64.fullmatch(str(item["content_sha256"])):
            raise ProvenanceError(f"memory_injected[{index}].content_sha256 不合法")

    for field in (
        "short_term_slot_enabled",
        "short_term_block_present",
        "short_term_feedback_history_present",
        "difficult_flag",
    ):
        if not isinstance(record[field], bool):
            raise ProvenanceError(f"{field} 必须是布尔值")
    if not record["short_term_slot_enabled"] and record["short_term_block_present"]:
        raise ProvenanceError("short_term 槽位关闭时 block_present 不能为 true")
    if record["short_term_feedback_count"] is not None and (
        not isinstance(record["short_term_feedback_count"], int)
        or isinstance(record["short_term_feedback_count"], bool)
        or record["short_term_feedback_count"] < 0
    ):
        raise ProvenanceError("short_term_feedback_count 不合法")

    if record["data_kind"] not in DATA_KINDS:
        raise ProvenanceError(f"data_kind 不合法：{record['data_kind']!r}")
    for field in ("config_sha256", "plan_sha256", "prompt_sha256"):
        value = record[field]
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise ProvenanceError(f"{field} 缺失或不是 64 位 SHA")
    if record["injected_json_sha256"] is None or not _HEX64.fullmatch(
        str(record["injected_json_sha256"])
    ):
        raise ProvenanceError("injected_json_sha256 缺失或不合法")
    if not isinstance(record["injected_json_ref"], str) or not record["injected_json_ref"].strip():
        raise ProvenanceError("injected_json_ref 缺失")
    if record["result_schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise ProvenanceError("result_schema_version 与当前运行时不符")
    if record["experiment_design_version"] != DESIGN_VERSION:
        raise ProvenanceError("experiment_design_version 与冻结设计版本不符")
    try:
        datetime.fromisoformat(str(record["generated_at"]))
    except ValueError as error:
        raise ProvenanceError("generated_at 不是 ISO 时间") from error
    if record["model_alias"] is not None and not isinstance(record["model_alias"], str):
        raise ProvenanceError("model_alias 必须是字符串或 null")
    if record["model_snapshot_id"] is not None and not isinstance(record["model_snapshot_id"], str):
        raise ProvenanceError("model_snapshot_id 必须是字符串或 null")
    allowed_snapshot_status = ("provided", "not_provided_by_provider", "not_available")
    if record["model_snapshot_status"] not in allowed_snapshot_status:
        raise ProvenanceError("model_snapshot_status 不在允许值内")
    if record["model_snapshot_id"] is None and record["model_snapshot_status"] == "provided":
        raise ProvenanceError("model_snapshot_id 为空时不能标记 provided")
    if record["model_snapshot_id"] is not None and record["model_snapshot_status"] != "provided":
        raise ProvenanceError("model_snapshot_id 存在时必须标记 provided")
