#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""make_blind_pack.py 回归测试（无 pytest，直接 `python test_graduation_blind_pack.py`）。

隔离边界
--------
* 所有用例都在 `tempfile.mkdtemp()` 下新建的**真实临时 Git 仓库**里运行，仓库里放合成的
  165 条实验记录（主 140 + B0 10 + S7-empty 5 + C/D-subj 10）与 8 个合成样本文件。
* 不读取、不引用任何正式实验 run 文件、manifest 或 blind_config.json 来构造盲评包；
  真实 `experiments/graduation/runs/*/results.jsonl` 与 `feedback.db` 只在首尾各取一次
  只读指纹，用来证明测试没有改动正式数据（T20）。
* 临时目录由 `_run_all()` 的 finally 统一清理，断言失败也会执行。

覆盖
----
T1  私有目录在仓库外 → 拒绝（无绕过开关）。
T2  pack_dir 与 private_dir 相同 → 拒绝。
T3  pack_dir 包含 private_dir → 拒绝。
T4  private_dir 包含 pack_dir → 拒绝。
T5  合法仓内路径：真实 git check-ignore / ls-files 通过；缺忽略规则、已跟踪文件均拒绝。
T6  合成数据规模与协议约束：165 条源记录、84/6/23、格子与校准约束、抽样哈希可复算。
T7  完整打包：113 份、blind_id 唯一且形式合法、mapping 可回溯到源行与源文件、
    评分者顺序是完整排列且两份不同、材料正文无身份泄露。
T8  manifest 正常（含 B0 条目）→ 打包成功，全部 match。
T9  SHA256 不一致 → 失败。
T10 文件大小不一致 → 失败。
T11 记录数不一致 → 失败。
T12 manifest 缺条目：主实验缺 → 失败；S7-empty 缺 → 失败（legacy 例外不得外溢）。
T13 只缺 B0 条目 → 允许（legacy），状态为 legacy_no_manifest。
T14 哈希来自磁盘实时计算：写盘后 snapshot 必须与 SourceRow 里缓存的旧哈希不同，
    并让 verify_snapshot 立即失败（不是复用加载阶段缓存）。
T15 打包期间源文件被改动 → 失败，且不留下任何包/私有文件。
T16 重打包：无 --repack 拒绝；--repack 把旧包+旧私有文件成套归档，活动目录只含新数据。
T17 归档目标已存在 → 拒绝，且旧 mapping 与旧包原状保留（不静默删除旧数据）。
T18 评分者顺序：独立随机且必定不同；即使 order_rows 恒等也靠兜底保证不同与完整。
T19 blind_config 冻结校验：改 seed / 改算法版本 / frozen=false / 含身份键 一律拒绝。
T20 只读边界：正式 runs 与 feedback.db 指纹不变，且未在正式仓库生成盲评包。
"""

import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
GRADUATION_DIR = PROJECT_DIR / "experiments" / "graduation"
sys.path.insert(0, str(GRADUATION_DIR))

import make_blind_pack as pack  # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(prefix="grad_blindpack_"))
REAL_RUNS = GRADUATION_DIR / "runs"
REAL_DB = PROJECT_DIR / "feedback.db"
REAL_BLIND_PACK = GRADUATION_DIR / "blind_pack"
REAL_BLIND_PRIVATE = GRADUATION_DIR / "blind_private"

FROZEN_SEED = 2026111301
FORMAL_SAMPLES = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
FORMAL_GROUPS = ["A", "B", "C", "D"]
REPEATS = [0, 1, 2, 3, 4]
# B0 10 份拆到 3 个 run（同一样本内 repeat 不重复），按真实协议的 legacy 形态
B0_LAYOUT = [
    (pack.B0_RUNS[0], [("S5", 0), ("S5", 1), ("S5", 2)]),
    (pack.B0_RUNS[1], [("S5", 3), ("S5", 4), ("S7", 0)]),
    (pack.B0_RUNS[2], [("S7", 1), ("S7", 2), ("S7", 3), ("S7", 4)]),
]

# 合成正文：不得出现 S1–S7、B0、C-subj/D-subj、r0–r4、裸 A/B/C/D、run 日期等身份片段
REQUEST = "请根据我最近几轮反馈，安排下一阶段的学习计划。"
ANSWER = "建议先把薄弱知识点复习一遍，再用两道综合题检验掌握程度；若时间不足，优先保证基础概念。"
PLAN = {"steps": ["复习基础概念", "完成两道综合题", "整理错题并复盘"]}
FEEDBACKS = [
    {"status": "completed", "reason": "按计划完成"},
    {"status": "partial", "reason": "题量偏多"},
    {"status": "not_completed", "reason": "时间不足"},
]

CONFIG_BODY = {
    "protocol_version": "v4.5",
    "protocol_doc": "docs/GRADUATION-EXPERIMENT-DESIGN.md",
    "blind_sampling_seed": FROZEN_SEED,
    "algorithm_version": "sha256-domain-separated-selection-v1",
    "selection_domains": {
        "formal": "blind-main-selection",
        "calibration": "blind-calibration-selection",
        "supplementary_s7empty": "blind-s7empty-selection",
    },
    "created_utc": "2026-11-13T07:35:00Z",
    "frozen": True,
    "notes": ["合成测试配置：只含抽样参数，不含任何身份、盲评 ID 或抽样结果。"],
}

MANIFEST_COLUMNS = [
    "batch",
    "batch_desc",
    "relative_path",
    "file",
    "size_bytes",
    "sha256",
    "records",
]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha(path: Path) -> str:
    return _sha_bytes(Path(path).read_bytes())


def _git(args, cwd):
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _expect_pack_error(func, *fragments):
    try:
        func()
    except pack.PackError as exc:
        text = str(exc)
        for fragment in fragments:
            assert fragment in text, f"错误信息缺 {fragment!r}：{text}"
        return text
    raise AssertionError(f"应抛出 PackError（期望包含 {fragments}），实际成功")


def _read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# 合成仓库
# --------------------------------------------------------------------------


@dataclass
class Sandbox:
    root: Path
    runs_dir: Path
    samples_dir: Path
    config: Path
    manifest: Path
    pack_dir: Path
    private_dir: Path


def _make_row(sample_id: str, group: str, repeat: int) -> dict:
    digest = _sha_bytes(f"prompt|{sample_id}|{group}|{repeat}".encode("utf-8"))
    return {
        "experiment_id": f"synthetic-{sample_id}-{group}-{repeat}".lower(),
        "sample_id": sample_id,
        "group": group,
        "condition": group,
        "repeat_index": repeat,
        "round_index": repeat,
        "plan_seq": repeat + 1,
        "prompt_sha256": digest,
        "success": True,
        "final_output": {"answer": ANSWER, "plan": PLAN},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False))
            handle.write("\n")


def _write_runs(sb: Sandbox) -> None:
    main_rows = [
        _make_row(sample, group, repeat)
        for sample in FORMAL_SAMPLES
        for group in FORMAL_GROUPS
        for repeat in REPEATS
    ]
    _write_jsonl(sb.runs_dir / pack.MAIN_RUN / pack.SOURCE_FILE, main_rows)

    for run_dir, layout in B0_LAYOUT:
        _write_jsonl(
            sb.runs_dir / run_dir / pack.SOURCE_FILE,
            [_make_row(sample, "B0", repeat) for sample, repeat in layout],
        )

    _write_jsonl(
        sb.runs_dir / pack.S7EMPTY_RUN / pack.SOURCE_FILE,
        [_make_row("S7-empty", "D", repeat) for repeat in REPEATS],
    )
    subj_rows = [
        _make_row("S5", group, repeat)
        for group in ("C-subj", "D-subj")
        for repeat in REPEATS
    ]
    _write_jsonl(sb.runs_dir / pack.SUBJ_RUN / pack.SOURCE_FILE, subj_rows)


def _write_samples(sb: Sandbox) -> None:
    sb.samples_dir.mkdir(parents=True, exist_ok=True)
    for sample_id in FORMAL_SAMPLES + ["S7-empty"]:
        body = {
            "sample_id": sample_id,
            "current_user_request": REQUEST,
            "feedbacks": FEEDBACKS,
        }
        (sb.samples_dir / f"{sample_id}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _write_config(path: Path, **overrides) -> Path:
    body = json.loads(json.dumps(CONFIG_BODY))
    body.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _manifest_rows(sb: Sandbox, run_dirs=None) -> list[dict]:
    snapshot = pack.snapshot_sources(run_dirs or pack.source_run_dirs(), runs_dir=sb.runs_dir)
    rows = []
    for run_dir, info in snapshot.items():
        rows.append(
            {
                "batch": "synthetic",
                "batch_desc": "合成测试数据（非真实实验结果）",
                "relative_path": f"experiments/graduation/runs/{run_dir}/{pack.SOURCE_FILE}",
                "file": pack.SOURCE_FILE,
                "size_bytes": str(info["size_bytes"]),
                "sha256": info["sha256"],
                "records": str(info["records"]),
            }
        )
    return rows


def _write_manifest(sb: Sandbox, rows: list[dict]) -> None:
    sb.manifest.parent.mkdir(parents=True, exist_ok=True)
    with sb.manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _make_sandbox(name: str, *, git: bool = True, ignore_private: bool = True) -> Sandbox:
    root = TMP_ROOT / name
    grad = root / "experiments" / "graduation"
    sb = Sandbox(
        root=root,
        runs_dir=grad / "runs",
        samples_dir=grad / "samples",
        config=grad / "blind_config.json",
        manifest=root / "docs" / "GRADUATION-EXPERIMENT-FILE-MANIFEST.csv",
        pack_dir=grad / "blind_pack",
        private_dir=grad / "blind_private",
    )
    sb.runs_dir.mkdir(parents=True, exist_ok=True)
    _write_runs(sb)
    _write_samples(sb)
    _write_config(sb.config)
    _write_manifest(sb, _manifest_rows(sb))
    if git:
        init = _git(["init", "-q"], root)
        assert init.returncode == 0, init.stderr
    if ignore_private:
        lines = ["experiments/graduation/blind_private/", "experiments/graduation/blind_pack/"]
        (root / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sb


@contextlib.contextmanager
def _patched_paths(sb: Sandbox):
    """把脚本里的默认根目录指向临时仓库（生产代码不变，跑的是同一条逻辑）。"""

    names = ("REPO_ROOT", "MANIFEST_PATH", "SAMPLES_DIR", "CONFIG_PATH")
    saved = {name: getattr(pack, name) for name in names}
    pack.REPO_ROOT = sb.root
    pack.MANIFEST_PATH = sb.manifest
    pack.SAMPLES_DIR = sb.samples_dir
    pack.CONFIG_PATH = sb.config
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(pack, name, value)


def _argv(sb: Sandbox, extra=()):
    return [
        "--runs-dir", str(sb.runs_dir),
        "--config", str(sb.config),
        "--pack-dir", str(sb.pack_dir),
        "--private-dir", str(sb.private_dir),
        *extra,
    ]


def _args(sb: Sandbox, extra=()):
    return pack.parse_args(_argv(sb, extra))


def _main(sb: Sandbox, extra=()):
    """走真实 CLI 入口：main() 只接受 argv，内部自己 parse。"""

    out, err = io.StringIO(), io.StringIO()
    with _patched_paths(sb), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pack.main(_argv(sb, extra))
    return code, out.getvalue(), err.getvalue()


def _run_pack(sb: Sandbox, extra=()):
    out, err = io.StringIO(), io.StringIO()
    with _patched_paths(sb), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        pack.run_pack(_args(sb, extra))
    return out.getvalue()


def _build(sb: Sandbox, extra=()) -> Sandbox:
    code, out, err = _main(sb, extra)
    assert code == 0, f"打包应成功，实际 exit={code}\nstdout={out}\nstderr={err}"
    return sb


@contextlib.contextmanager
def _clock(value: str):
    saved = pack.utc_now
    pack.utc_now = lambda: value
    try:
        yield
    finally:
        pack.utc_now = saved


# --------------------------------------------------------------------------
# T1–T5 路径保护
# --------------------------------------------------------------------------


def test_t1_private_outside_repo():
    sb = _make_sandbox("t1")
    outside = TMP_ROOT / "t1_outside" / "blind_private"
    outside.mkdir(parents=True, exist_ok=True)
    with _patched_paths(sb):
        _expect_pack_error(lambda: pack.ensure_private_gate(outside), "不在仓库", "拒绝写入", "不存在绕过开关")
    print("  T1 PASS：仓库外私有目录被拒绝，失败信息明确说明没有绕过开关")


def test_t2_pack_equals_private():
    sb = _make_sandbox("t2")
    same = sb.private_dir
    _expect_pack_error(lambda: pack.ensure_path_layout(same, same), "同一目录")
    print("  T2 PASS：pack_dir == private_dir 被拒绝")


def test_t3_pack_contains_private():
    sb = _make_sandbox("t3")
    _expect_pack_error(
        lambda: pack.ensure_path_layout(sb.pack_dir, sb.pack_dir / "blind_private"),
        "private_dir 位于 pack_dir 内",
    )
    print("  T3 PASS：private_dir 落在 pack_dir 内被拒绝")


def test_t4_private_contains_pack():
    sb = _make_sandbox("t4")
    _expect_pack_error(
        lambda: pack.ensure_path_layout(sb.private_dir / "pack", sb.private_dir),
        "pack_dir 位于 private_dir 内",
    )
    print("  T4 PASS：pack_dir 落在 private_dir 内被拒绝")


def test_t5_private_gate_real_git():
    sb = _make_sandbox("t5")
    with _patched_paths(sb):
        evidence = pack.ensure_private_gate(sb.private_dir)
    assert len(evidence) >= 5, evidence
    # 证据里含 .gitignore 的行 = 1 条「目录级规则」锚点 + 每条探针各一行；
    # 探针覆盖三个私有文件，也覆盖此刻不存在的未来文件名与 archive/ 深层路径。
    assert sum(".gitignore" in line for line in evidence) == 1 + len(pack.PRIVATE_IGNORE_PROBES), evidence
    assert "git ls-files" in evidence[-1] and "0 条" in evidence[-1], evidence

    # 真实 git 复核（不是 mock）：三个私有文件都被忽略，且目录内没有被跟踪的文件
    for name in pack.PRIVATE_FILES:
        result = _git(["check-ignore", "-v", str(sb.private_dir / name)], sb.root)
        assert result.returncode == 0 and ".gitignore" in result.stdout, result.stdout
    tracked = _git(["ls-files", "--", str(sb.private_dir)], sb.root)
    assert tracked.stdout.strip() == "", tracked.stdout

    # 缺忽略规则 → 真实 check-ignore 返回非 0 → 拒绝
    # （此处 .gitignore 只忽略 blind_pack/，私有目录完全没被覆盖，
    #   因此先触发第 1 步的「目录级规则」判定；只点名若干文件的场景由 T21 覆盖）
    (sb.root / ".gitignore").write_text("experiments/graduation/blind_pack/\n", encoding="utf-8")
    missing = _git(["check-ignore", "-v", str(sb.private_dir / "mapping.csv")], sb.root)
    assert missing.returncode != 0, "缺少忽略规则时 git check-ignore 应为非 0"
    with _patched_paths(sb):
        _expect_pack_error(lambda: pack.ensure_private_gate(sb.private_dir), "目录级 .gitignore 规则")

    # 已被跟踪的敏感文件：即使被忽略也必须拒绝（不能把“被忽略”当成“安全”）
    (sb.root / ".gitignore").write_text("experiments/graduation/blind_private/\n", encoding="utf-8")
    sb.private_dir.mkdir(parents=True, exist_ok=True)
    secret = sb.private_dir / "mapping.csv"
    secret.write_text("blind_id,phase\n", encoding="utf-8")
    added = _git(["add", "-f", str(secret)], sb.root)
    assert added.returncode == 0, added.stderr
    # Git 对已跟踪路径的 check-ignore 默认返回 1（即使文件确实命中忽略规则），
    # 所以实现的 gate 必须用 --no-index 才能看穿跟踪状态、给出准确报错。
    assert _git(["check-ignore", "-v", str(secret)], sb.root).returncode == 1
    ignored_anyway = _git(["check-ignore", "-v", "--no-index", str(secret)], sb.root)
    assert ignored_anyway.returncode == 0 and ".gitignore" in ignored_anyway.stdout, ignored_anyway.stdout
    with _patched_paths(sb):
        _expect_pack_error(lambda: pack.ensure_private_gate(sb.private_dir), "已被 Git 跟踪")
    print("  T5 PASS：真实 git 下忽略/跟踪两种失败都被 fail-closed 拦下")


# --------------------------------------------------------------------------
# T6 合成数据的规模与协议约束
# --------------------------------------------------------------------------


def test_t6_synthetic_counts_and_selection():
    sb = _make_sandbox("t6")
    total = sum(
        len(pack.read_rows(run_dir, batch, sb.runs_dir))
        for run_dir, batch in [
            (pack.MAIN_RUN, pack.BATCH_MAIN),
            (pack.S7EMPTY_RUN, pack.BATCH_S7EMPTY),
            (pack.SUBJ_RUN, pack.BATCH_SUBJ),
            *[(run_dir, pack.BATCH_B0) for run_dir in pack.B0_RUNS],
        ]
    )
    assert total == 165, total

    built = pack.build_items(FROZEN_SEED, sb.runs_dir)
    counts = built["counts"]
    assert counts == {"formal": 84, "calibration": 6, "supplementary": 23}, counts
    assert len(built["items"]) == 113

    cells = {}
    for pick in built["formal"]:
        key = (pick.row.sample_id, pick.row.group)
        cells[key] = cells.get(key, 0) + 1
        # 抽样哈希必须与协议定义逐项一致（可独立重算）
        assert pick.hash == pack.selection_hash(
            FROZEN_SEED, pack.DOMAIN_FORMAL, pick.row.sample_id, pick.row.group, pick.row.repeat_index
        )
    assert len(cells) == 28 and set(cells.values()) == {3}, cells

    formal_ids = {pick.row.identity_tuple for pick in built["formal"]}
    cal_ids = {pick.row.identity_tuple for pick in built["calibration"]}
    assert len(formal_ids) == 84 and len(cal_ids) == 6
    assert not (formal_ids & cal_ids)
    assert len({pick.row.sample_id for pick in built["calibration"]}) == 6
    assert len({(pick.row.sample_id, pick.row.group) for pick in built["calibration"]}) == 6

    supplementary = [item for item in built["items"] if item["phase"] == pack.PHASE_SUPPLEMENTARY]
    by_batch = {}
    for item in supplementary:
        by_batch[item["batch"]] = by_batch.get(item["batch"], 0) + 1
    assert by_batch == {"suppl-s7empty": 3, "b0-legacy": 10, "suppl-subj": 10}, by_batch
    for item in supplementary:
        if item["batch"] == "suppl-s7empty":
            assert item["evaluate_e7"] is False
        elif item["batch"] == "b0-legacy":
            assert item["evaluate_e7"] is (item["row"].sample_id == "S5")
        else:
            assert item["evaluate_e7"] is True
    print("  T6 PASS：165 条源数据 → 84/6/23，格子与校准约束、抽样哈希逐项可复算")


# --------------------------------------------------------------------------
# T7 完整打包与 mapping 回溯
# --------------------------------------------------------------------------


def test_t7_pack_and_mapping_backtrace():
    sb = _make_sandbox("t7")
    _build(sb)

    mapping = _read_csv(sb.private_dir / "mapping.csv")
    assert len(mapping) == 113
    phases = {}
    for row in mapping:
        phases[row["phase"]] = phases.get(row["phase"], 0) + 1
    assert phases == {"formal": 84, "calibration": 6, "supplementary": 23}, phases

    ids = [row["blind_id"] for row in mapping]
    assert len(ids) == len(set(ids))
    for blind_id in ids:
        assert pack.re.fullmatch(r"[0-9a-f]{32}", blind_id), blind_id

    # 文件与 mapping 一一对应，文件名只含 blind_id
    for phase in ("formal", "calibration", "supplementary"):
        phase_dir = sb.pack_dir / phase
        expected_ids = {row["blind_id"] for row in mapping if row["phase"] == phase}
        files = {path.name for path in phase_dir.iterdir()}
        assert {name for name in files if name.endswith(".md")} == {f"{i}.md" for i in expected_ids}
        assert files - {f"{i}.md" for i in expected_ids} == {"order_rater_a.csv", "order_rater_b.csv"}

    # 回溯：每条 mapping 都必须能落回真实源文件的那一行，且哈希来自磁盘
    source_cache = {}
    for row in mapping:
        run_dir = row["source_run_dir"]
        if run_dir not in source_cache:
            path = sb.runs_dir / run_dir / pack.SOURCE_FILE
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            source_cache[run_dir] = (path, lines)
        path, lines = source_cache[run_dir]
        source_row = lines[int(row["source_row_number"]) - 1]
        assert source_row["sample_id"] == row["sample_id"]
        assert source_row["group"] == row["group"]
        assert str(source_row["repeat_index"]) == row["repeat_index"]
        assert source_row["prompt_sha256"] == row["source_prompt_sha256"]
        assert pack.row_sha256(source_row) == row["source_result_row_sha256"]
        assert row["source_results_file_sha256"] == _sha(path)
        assert row["source_file"] == pack.SOURCE_FILE and row["generator_version"] == pack.VERSION
        assert pack.re.fullmatch(r"[0-9a-f]{32}", row["blind_id"])

    # 评分者顺序：两套完整排列且不同，位置号连续
    for phase in ("formal", "calibration", "supplementary"):
        expected_ids = {row["blind_id"] for row in mapping if row["phase"] == phase}
        orders = {}
        for rater in ("rater_a", "rater_b"):
            rows = _read_csv(sb.pack_dir / phase / f"order_{rater}.csv")
            assert [int(row["position"]) for row in rows] == list(range(1, len(expected_ids) + 1))
            orders[rater] = [row["blind_id"] for row in rows]
            assert len(orders[rater]) == len(set(orders[rater])) == len(expected_ids)
            assert set(orders[rater]) == expected_ids
        assert orders["rater_a"] != orders["rater_b"], phase

    # 材料正文：结构与去身份（正文只允许 blind_id，不含样本/组别/run 日期）
    for row in mapping[:3] + mapping[-3:]:
        text = (sb.pack_dir / row["phase"] / f"{row['blind_id']}.md").read_text(encoding="utf-8")
        for heading in ("## 用户请求", "## 学习者历史（程序统计，匿名化）", "## 模型回答", "## 学习计划（JSON）"):
            assert heading in text, heading
        assert row["blind_id"] in text.splitlines()[0]
        assert not pack.scan_packet_body({}, text), pack.scan_packet_body({}, text)
        # 正向断言：材料必须真的带上请求与回答原文，否则“去身份通过”毫无意义
        assert REQUEST in text, "材料缺少用户请求"
        assert ANSWER in text, "材料缺少模型回答原文"
        assert "S1" not in text and "2026-11-13" not in text

    # selection_audit：84 行，字段与 mapping 一致
    audit = _read_csv(sb.private_dir / "selection_audit.csv")
    assert list(audit[0].keys()) == pack.AUDIT_COLUMNS
    assert len(audit) == 84
    formal_rows = {(row["sample_id"], row["group"], row["repeat_index"]) for row in mapping if row["phase"] == "formal"}
    assert {(row["sample_id"], row["group"], row["repeat_index"]) for row in audit} == formal_rows
    assert "MAPPING-AUDIT.md" in {path.name for path in sb.private_dir.iterdir()}
    print("  T7 PASS：113 份材料与 mapping 双向可回溯，顺序文件完整且两人不同")


# --------------------------------------------------------------------------
# T8–T13 manifest 与源文件完整性
# --------------------------------------------------------------------------


def _manifest_with(sb: Sandbox, mutate) -> list[dict]:
    rows = _manifest_rows(sb)
    mutate(rows)
    return rows


def _row_for(rows, run_dir):
    relative = f"experiments/graduation/runs/{run_dir}/{pack.SOURCE_FILE}"
    return next(row for row in rows if row["relative_path"] == relative)


def test_t8_manifest_match():
    sb = _make_sandbox("t8")
    _build(sb)
    report = pack.verify_snapshot(pack.snapshot_sources(runs_dir=sb.runs_dir), pack.manifest_hashes(sb.manifest))
    assert len(report) == 6, report
    assert {row["status"] for row in report} == {"match"}, report
    print("  T8 PASS：6 个源文件 SHA256/大小/条数与 manifest 全部一致")


def test_t9_manifest_sha_mismatch():
    sb = _make_sandbox("t9")
    rows = _manifest_with(sb, lambda items: _row_for(items, pack.MAIN_RUN).update({"sha256": "0" * 64}))
    _write_manifest(sb, rows)
    code, out, err = _main(sb)
    assert code == 2, (code, out, err)
    assert "与 manifest 不一致" in err, err
    # 证据必须落在 sha 字段上：expected 就是被改成全 0 的那个值
    assert "status=mismatch:sha256" in err, err
    assert "expected_sha256=" + "0" * 64 in err, err
    assert "actual_sha256=" in err, err
    assert pack.MAIN_RUN in err, err
    # 只错在 sha 时不得顺带报 size/records 不一致
    assert "expected_size_bytes" not in err and "expected_records" not in err, err
    assert not sb.pack_dir.exists() and not (sb.private_dir / "mapping.csv").exists()
    print("  T9 PASS：SHA256 不一致 → exit 2，错误指到 expected 值，未写任何产物")


def test_t10_manifest_size_mismatch():
    sb = _make_sandbox("t10")
    real = pack.snapshot_sources([pack.SUBJ_RUN], runs_dir=sb.runs_dir)[pack.SUBJ_RUN]
    wrong_size = real["size_bytes"] + 1

    def mutate(items):
        row = _row_for(items, pack.SUBJ_RUN)
        assert row["sha256"] == real["sha256"]  # 只改大小，SHA 保持正确
        row["size_bytes"] = str(wrong_size)

    _write_manifest(sb, _manifest_with(sb, mutate))
    code, _, err = _main(sb)
    assert code == 2 and "与 manifest 不一致" in err, (code, err)
    # 只有大小不符也必须失败（不能因为 SHA 相同就放过），且必须报出两边的字节数
    assert "status=mismatch:size_bytes" in err, err
    assert f"expected_size_bytes={wrong_size}" in err, err
    assert f"actual_size_bytes={real['size_bytes']}" in err, err
    assert "expected_records" not in err, err
    print("  T10 PASS：仅 size_bytes 不符（SHA 正确）→ exit 2，错误给出两边字节数")


def test_t11_manifest_records_mismatch():
    sb = _make_sandbox("t11")
    real = pack.snapshot_sources([pack.S7EMPTY_RUN], runs_dir=sb.runs_dir)[pack.S7EMPTY_RUN]
    wrong_records = real["records"] + 1

    def mutate(items):
        row = _row_for(items, pack.S7EMPTY_RUN)
        assert row["sha256"] == real["sha256"] and row["size_bytes"] == str(real["size_bytes"])
        row["records"] = str(wrong_records)

    _write_manifest(sb, _manifest_with(sb, mutate))
    code, _, err = _main(sb)
    assert code == 2 and "与 manifest 不一致" in err, (code, err)
    assert "status=mismatch:records" in err, err
    assert f"expected_records={wrong_records}" in err, err
    assert f"actual_records={real['records']}" in err, err
    print("  T11 PASS：仅 records 不符（SHA 与大小正确）→ exit 2")


def test_t12_manifest_missing_entries():
    # 主实验缺条目 → 失败
    sb = _make_sandbox("t12a")
    rows = [row for row in _manifest_rows(sb) if pack.MAIN_RUN not in row["relative_path"]]
    _write_manifest(sb, rows)
    code, _, err = _main(sb)
    assert code == 2 and "缺失" in err and pack.MAIN_RUN in err, (code, err)

    # S7-empty 缺条目 → 同样失败（legacy 例外只对 B0）
    sb2 = _make_sandbox("t12b")
    rows2 = [row for row in _manifest_rows(sb2) if pack.S7EMPTY_RUN not in row["relative_path"]]
    _write_manifest(sb2, rows2)
    code2, _, err2 = _main(sb2)
    assert code2 == 2 and "缺失" in err2 and pack.S7EMPTY_RUN in err2, (code2, err2)

    # manifest 文件整体缺失 → 按规则失败（不是静默通过）
    sb3 = _make_sandbox("t12c")
    with _patched_paths(sb3):
        saved = pack.MANIFEST_PATH
        pack.MANIFEST_PATH = sb3.root / "docs" / "not-there.csv"
        try:
            _expect_pack_error(lambda: pack.verify_snapshot(pack.snapshot_sources(runs_dir=sb3.runs_dir), pack.manifest_hashes()), "缺失")
        finally:
            pack.MANIFEST_PATH = saved
    print("  T12 PASS：主实验/S7-empty 缺条目与 manifest 缺失均 fail-closed")


def test_t13_b0_legacy_optional():
    sb = _make_sandbox("t13")
    rows = [row for row in _manifest_rows(sb) if not any(run in row["relative_path"] for run in pack.B0_RUNS)]
    _write_manifest(sb, rows)
    report = pack.verify_snapshot(pack.snapshot_sources(runs_dir=sb.runs_dir), pack.manifest_hashes(sb.manifest))
    status = {row["run_dir"]: row["status"] for row in report}
    for run_dir in pack.B0_RUNS:
        assert status[run_dir] == "legacy_no_manifest", status
    assert status[pack.MAIN_RUN] == "match"
    _build(sb)  # 打包必须成功
    print("  T13 PASS：只允许 B0 缺条目（legacy_no_manifest），其余必须 match")


# --------------------------------------------------------------------------
# T14–T15 哈希来自磁盘 / 打包期间变化
# --------------------------------------------------------------------------


def test_t14_hash_from_disk_not_cache():
    sb = _make_sandbox("t14")
    rows = pack.load_main_rows(sb.runs_dir)
    cached = {row.run_dir: row.results_file_sha256 for row in rows}
    assert cached[pack.MAIN_RUN] == _sha(sb.runs_dir / pack.MAIN_RUN / pack.SOURCE_FILE)

    path = sb.runs_dir / pack.MAIN_RUN / pack.SOURCE_FILE
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n")  # 只增加空白行：SHA256/大小变、记录数不变
    snapshot = pack.snapshot_sources([pack.MAIN_RUN], runs_dir=sb.runs_dir)
    assert snapshot[pack.MAIN_RUN]["sha256"] != cached[pack.MAIN_RUN], "snapshot 复用了加载阶段缓存"
    assert snapshot[pack.MAIN_RUN]["sha256"] == _sha(path)
    assert snapshot[pack.MAIN_RUN]["records"] == 140

    stale_manifest = pack.manifest_hashes(sb.manifest)
    _expect_pack_error(
        lambda: pack.verify_snapshot(pack.snapshot_sources(runs_dir=sb.runs_dir), stale_manifest),
        "不一致",
    )
    print("  T14 PASS：snapshot 每次从磁盘重算，缓存哈希不会冒充当前状态")


def test_t15_source_changed_during_pack():
    sb = _make_sandbox("t15")
    original = pack.build_items
    target = sb.runs_dir / pack.S7EMPTY_RUN / pack.SOURCE_FILE

    def mutating_build(*args, **kwargs):
        built = original(*args, **kwargs)
        with target.open("ab") as handle:
            handle.write(b"\n")  # 模拟打包期间源文件被外部改动
        return built

    pack.build_items = mutating_build
    try:
        with _patched_paths(sb):
            _expect_pack_error(lambda: pack.run_pack(_args(sb)), "打包期间源文件被改动")
    finally:
        pack.build_items = original
    assert not sb.pack_dir.exists()
    assert not (sb.private_dir / "mapping.csv").exists()
    print("  T15 PASS：打包期间源文件变化 → 失败且不落盘")


# --------------------------------------------------------------------------
# T16–T17 重打包与归档
# --------------------------------------------------------------------------


def test_t16_repack_archives_old_output():
    sb = _make_sandbox("t16")
    with _clock("2026-11-16T12:00:00Z"):
        _build(sb)
    old_mapping = _read_csv(sb.private_dir / "mapping.csv")
    old_ids = {row["blind_id"] for row in old_mapping}
    old_mapping_sha = _sha(sb.private_dir / "mapping.csv")
    old_pack_files = {
        path.relative_to(sb.pack_dir).as_posix(): _sha(path)
        for path in sb.pack_dir.rglob("*")
        if path.is_file()
    }
    assert len(old_pack_files) == 113 + 6, len(old_pack_files)

    # 无 --repack：拒绝覆盖，且旧 mapping 一字节不变
    code, _, err = _main(sb)
    assert code == 2 and "repack" in err, (code, err)
    assert _sha(sb.private_dir / "mapping.csv") == old_mapping_sha

    with _clock("2026-11-16T12:00:01Z"):
        _build(sb, ["--repack"])

    archive = sb.private_dir / "archive" / "pack-20261116T120001Z"
    assert archive.is_dir(), list((sb.private_dir / "archive").iterdir())
    archived_private = {path.name: _sha(path) for path in (archive / "private").iterdir()}
    assert set(archived_private) == set(pack.PRIVATE_FILES), archived_private
    assert archived_private["mapping.csv"] == old_mapping_sha

    archived_pack = {
        path.relative_to(archive / "blind_pack").as_posix(): _sha(path)
        for path in (archive / "blind_pack").rglob("*")
        if path.is_file()
    }
    assert archived_pack == old_pack_files, "旧包未被完整归档"

    manifest_text = (archive / "ARCHIVE-MANIFEST.md").read_text(encoding="utf-8")
    assert old_mapping_sha in manifest_text
    listed = [
        line
        for line in manifest_text.splitlines()
        if line.startswith("| ") and "SHA256" not in line and "---" not in line
    ]
    assert len(listed) == len(old_pack_files) + len(pack.PRIVATE_FILES), len(listed)
    assert f"归档文件数：{len(listed)}" in manifest_text
    for relative, digest in old_pack_files.items():
        assert f"| blind_pack/{relative} | {digest} |" in manifest_text, relative

    # 活动目录只含本次新生成的数据
    new_mapping = _read_csv(sb.private_dir / "mapping.csv")
    new_ids = {row["blind_id"] for row in new_mapping}
    assert len(new_ids) == 113 and new_ids.isdisjoint(old_ids)
    active_files = {path.name for path in sb.pack_dir.rglob("*.md")}
    assert active_files == {f"{blind_id}.md" for blind_id in new_ids}
    assert not (set(active_files) & {f"{blind_id}.md" for blind_id in old_ids})
    print("  T16 PASS：无 --repack 拒绝；--repack 成套归档旧包与旧私有文件，活动目录只含新数据")


def test_t17_archive_collision_keeps_old_data():
    sb = _make_sandbox("t17")
    with _clock("2026-11-16T13:00:00Z"):
        _build(sb)
    old_mapping_sha = _sha(sb.private_dir / "mapping.csv")
    old_pack = {
        path.relative_to(sb.pack_dir).as_posix(): _sha(path)
        for path in sb.pack_dir.rglob("*")
        if path.is_file()
    }
    # 预先占住归档目标名，模拟“归档失败”。归档名由 generated_utc 去掉 - 和 : 得到，
    # 必须不含冒号：Windows 不允许文件名出现冒号，这类名字会直接抛 OSError。
    stamp = "2026-11-16T13:00:01Z".replace(":", "").replace("-", "")
    assert ":" not in stamp and stamp == "20261116T130001Z", stamp
    (sb.private_dir / "archive" / f"pack-{stamp}").mkdir(parents=True)
    with _clock("2026-11-16T13:00:01Z"):
        with _patched_paths(sb):
            _expect_pack_error(lambda: pack.run_pack(_args(sb, ["--repack"])), "归档目标已存在")

    assert _sha(sb.private_dir / "mapping.csv") == old_mapping_sha, "归档失败不得改动旧 mapping"
    assert {
        path.relative_to(sb.pack_dir).as_posix(): _sha(path)
        for path in sb.pack_dir.rglob("*")
        if path.is_file()
    } == old_pack, "归档失败不得删除或改动旧包"
    print("  T17 PASS：归档失败时旧 mapping 与旧包原状保留")


# --------------------------------------------------------------------------
# T18 评分者顺序
# --------------------------------------------------------------------------


def test_t18_rater_orders_guaranteed_different():
    items = [{"blind_id": "%032x" % index} for index in range(1, 7)]
    for _ in range(20):
        first, second = pack.build_rater_orders(items)
        seq_a, seq_b = [blind_id for _, blind_id in first], [blind_id for _, blind_id in second]
        assert sorted(seq_a) == sorted(seq_b) == sorted(item["blind_id"] for item in items)
        assert [position for position, _ in first] == list(range(1, 7))
        assert [position for position, _ in second] == list(range(1, 7))
        assert seq_a != seq_b, "两次 shuffle 恰好相同时也必须保证不同"

    # 恒等序列（模拟两次随机结果完全一致）时靠兜底仍然不同且完整
    saved = pack.order_rows
    pack.order_rows = lambda rows, rng=None: [(index + 1, item["blind_id"]) for index, item in enumerate(rows)]
    try:
        first, second = pack.build_rater_orders(items)
    finally:
        pack.order_rows = saved
    seq_a, seq_b = [blind_id for _, blind_id in first], [blind_id for _, blind_id in second]
    assert seq_a != seq_b
    assert sorted(seq_b) == sorted(seq_a) == sorted(item["blind_id"] for item in items)
    print("  T18 PASS：两人顺序必定不同，且各自是完整无重复排列")


# --------------------------------------------------------------------------
# T19 配置冻结
# --------------------------------------------------------------------------


def test_t19_config_frozen():
    sb = _make_sandbox("t19", git=False)
    assert pack.load_blind_config(sb.config)["blind_sampling_seed"] == FROZEN_SEED

    altered_seed = _write_config(sb.root / "cfg_seed.json", blind_sampling_seed=FROZEN_SEED + 1)
    _expect_pack_error(lambda: pack.load_blind_config(altered_seed), "已被冻结为 2026111301")

    altered_algo = _write_config(sb.root / "cfg_algo.json", algorithm_version="other-v2")
    _expect_pack_error(lambda: pack.load_blind_config(altered_algo), "algorithm_version 已被冻结")

    unfrozen = _write_config(sb.root / "cfg_frozen.json", frozen=False)
    _expect_pack_error(lambda: pack.load_blind_config(unfrozen), "frozen")

    identity = json.loads(json.dumps(CONFIG_BODY))
    identity["sample_id"] = "S1"
    bad = sb.root / "cfg_identity.json"
    bad.write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")
    _expect_pack_error(lambda: pack.load_blind_config(bad), "不得出现在")

    broken = sb.root / "cfg_broken.json"
    broken.write_text("{not json", encoding="utf-8")
    _expect_pack_error(lambda: pack.load_blind_config(broken), "不是合法 JSON")
    print("  T19 PASS：seed/算法版本/frozen/身份键/坏 JSON 全部 fail-closed")


# --------------------------------------------------------------------------
# T20 只读边界
# --------------------------------------------------------------------------


REAL_BEFORE = None


def _fingerprints():
    table = {}
    for path in sorted(REAL_RUNS.glob("*/results.jsonl")):
        table[path.parent.name] = _sha(path)
    if REAL_DB.is_file():
        stat = REAL_DB.stat()
        table["__feedback_db__"] = (_sha(REAL_DB), stat.st_size, stat.st_mtime_ns)
    return table


def test_t20_real_data_untouched():
    global REAL_BEFORE
    if REAL_BEFORE is None:
        REAL_BEFORE = _fingerprints()
    assert _fingerprints() == REAL_BEFORE, "正式实验源文件或 feedback.db 被改动"
    assert not REAL_BLIND_PACK.exists(), "正式仓库不应存在 blind_pack/"
    assert not REAL_BLIND_PRIVATE.exists(), "正式仓库不应存在 blind_private/"
    print("  T20 PASS：正式 runs/feedback.db 指纹未变，正式仓库无盲评产物")


# --------------------------------------------------------------------------
# T21–T27 第二轮修复：私有目录硬门 / manifest 空字段 / 归档完整性
# --------------------------------------------------------------------------


def _git_add_all(root: Path) -> None:
    added = _git(["add", "-f", "-A"], root)
    assert added.returncode == 0, added.stderr


def test_t21_private_gate_real_git():
    """私有目录硬门必须靠真实 git 证明，任一环节不满足就拒绝写入。"""

    # 1) 正常：目录级规则 → 通过，且证据里有目录级规则与 ls-files 结果
    sb = _make_sandbox("t21-ok")
    sb.private_dir.mkdir(parents=True, exist_ok=True)
    evidence = pack.ensure_private_gate(sb.private_dir, repo_root=sb.root)
    joined = "\n".join(evidence)
    assert "目录级忽略规则" in joined, joined
    assert "git ls-files 无私有文件" in joined, joined

    # 2) 只点名若干文件名（没有整目录规则）→ 拒绝
    sb2 = _make_sandbox("t21-nameonly", ignore_private=False)
    (sb2.root / ".gitignore").write_text(
        "mapping.csv\nselection_audit.csv\nMAPPING-AUDIT.md\n", encoding="utf-8", newline="\n"
    )
    sb2.private_dir.mkdir(parents=True, exist_ok=True)
    _expect_pack_error(
        lambda: pack.ensure_private_gate(sb2.private_dir, repo_root=sb2.root),
        "目录级 .gitignore 规则",
    )

    # 3) 私有目录里有被跟踪文件 → 拒绝（.gitignore 生效前 add 过的那种）
    sb3 = _make_sandbox("t21-tracked")
    sb3.private_dir.mkdir(parents=True, exist_ok=True)
    (sb3.private_dir / "mapping.csv").write_text("blind_id\n", encoding="utf-8", newline="\n")
    _git_add_all(sb3.root)
    _expect_pack_error(
        lambda: pack.ensure_private_gate(sb3.private_dir, repo_root=sb3.root),
        "已被 Git 跟踪",
    )

    # 4) 私有目录在仓库外 → 拒绝（不存在绕过开关）
    sb4 = _make_sandbox("t21-outside")
    outside = TMP_ROOT / "t21-outside-elsewhere"
    outside.mkdir(parents=True, exist_ok=True)
    _expect_pack_error(
        lambda: pack.ensure_private_gate(outside, repo_root=sb4.root),
        "不在仓库",
    )

    # 5) 根本不是 Git 仓库 → 拒绝，且错误里带 git 的真实输出
    sb5 = _make_sandbox("t21-nogit", git=False)
    sb5.private_dir.mkdir(parents=True, exist_ok=True)
    _expect_pack_error(
        lambda: pack.ensure_private_gate(sb5.private_dir, repo_root=sb5.root),
        "git rev-parse 失败",
    )

    # 6) .gitignore 整个缺失 → 拒绝
    sb6 = _make_sandbox("t21-noignore", ignore_private=False)
    sb6.private_dir.mkdir(parents=True, exist_ok=True)
    _expect_pack_error(
        lambda: pack.ensure_private_gate(sb6.private_dir, repo_root=sb6.root),
        "无法证明私有目录被忽略",
    )
    print("  T21 PASS：目录级规则/跟踪文件/仓库外/非 Git/缺 .gitignore 全部拒绝")


def test_t22_manifest_empty_fields_fail():
    """manifest 字段为空不是「没写就先放过」：空值与非法值都必须在算指标前失败。"""

    cases = [
        ("t22-sha-empty", "sha256", "", "sha256_empty"),
        ("t22-sha-invalid", "sha256", "z" * 64, "sha256_invalid"),
        ("t22-size-empty", "size_bytes", "", "size_bytes_empty"),
        ("t22-size-invalid", "size_bytes", "-1", "size_bytes_invalid"),
        ("t22-records-empty", "records", "", "records_empty"),
        ("t22-records-invalid", "records", "abc", "records_invalid"),
    ]
    for name, field, value, fragment in cases:
        sb = _make_sandbox(name)
        _write_manifest(
            sb,
            _manifest_with(sb, lambda rows, f=field, v=value: _row_for(rows, pack.MAIN_RUN).update({f: v})),
        )
        code, out, err = _main(sb)
        assert code == 2, (name, code, out, err)
        assert "与 manifest 不一致" in err, (name, err)
        assert fragment in err, (name, fragment, err)
        # 失败必须发生在写产物之前
        assert not sb.pack_dir.exists(), name
        assert not (sb.private_dir / "mapping.csv").exists(), name
    print("  T22 PASS：sha256/size_bytes/records 的空值与非法值一律 fail-closed")


def test_t23_manifest_duplicate_entries():
    sb = _make_sandbox("t23")
    rows = _manifest_rows(sb)
    rows.append(dict(_row_for(rows, pack.MAIN_RUN)))
    _write_manifest(sb, rows)
    _expect_pack_error(lambda: pack.manifest_hashes(sb.manifest), "重复条目")
    code, _, err = _main(sb)
    assert code == 2 and "重复条目" in err, (code, err)
    assert not sb.pack_dir.exists()
    print("  T23 PASS：manifest 同一文件重复条目 → 拒绝，不静默覆盖")


def _build_and_snapshot(sb: Sandbox, stamp: str):
    with _clock(stamp):
        _build(sb)
    mapping = (sb.private_dir / "mapping.csv").read_bytes()
    files = sorted(
        path.relative_to(sb.pack_dir).as_posix()
        for path in sb.pack_dir.rglob("*")
        if path.is_file()
    )
    assert len(files) == 113 + 2 * 3, len(files)
    return mapping, files


def test_t24_archive_verifies_copied_bytes():
    """复制后的归档文件必须与源文件逐条 SHA256 一致，否则拒绝发布并保留旧数据。"""

    sb = _make_sandbox("t24")
    mapping_before, files_before = _build_and_snapshot(sb, "2026-11-16T14:00:00Z")

    real_copy2 = shutil.copy2

    def corrupting_copy(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        if str(src).endswith("selection_audit.csv"):
            with open(dst, "ab") as handle:
                handle.write(b"\n")  # 模拟复制过程把内容写坏
        return result

    pack.shutil.copy2 = corrupting_copy
    try:
        with _patched_paths(sb), _clock("2026-11-16T14:00:01Z"):
            _expect_pack_error(
                lambda: pack.run_pack(_args(sb, ["--repack"])),
                "步骤 4/归档文件 SHA256 与清单不一致",
            )
    finally:
        pack.shutil.copy2 = real_copy2

    archive = sb.private_dir / "archive"
    assert (sb.private_dir / "mapping.csv").read_bytes() == mapping_before
    files_after = sorted(
        path.relative_to(sb.pack_dir).as_posix()
        for path in sb.pack_dir.rglob("*")
        if path.is_file()
    )
    assert files_after == files_before
    assert not list(archive.glob("pack-*")), "失败的归档不得占住归档名"
    assert not list(archive.glob(".*staging")), "失败的 staging 必须清理干净"
    print("  T24 PASS：复制内容被改坏 → 步骤 4 拒绝发布，旧映射与活动包原样保留")


def test_t25_archive_copy_failure_keeps_old():
    sb = _make_sandbox("t25")
    mapping_before, files_before = _build_and_snapshot(sb, "2026-11-16T15:00:00Z")

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def failing_copy(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("模拟磁盘写满")
        return real_copy2(src, dst, *args, **kwargs)

    pack.shutil.copy2 = failing_copy
    try:
        with _patched_paths(sb), _clock("2026-11-16T15:00:01Z"):
            _expect_pack_error(
                lambda: pack.run_pack(_args(sb, ["--repack"])),
                "步骤 3/复制旧产物到 staging 失败",
            )
    finally:
        pack.shutil.copy2 = real_copy2

    archive = sb.private_dir / "archive"
    assert (sb.private_dir / "mapping.csv").read_bytes() == mapping_before
    assert sorted(
        path.relative_to(sb.pack_dir).as_posix() for path in sb.pack_dir.rglob("*") if path.is_file()
    ) == files_before
    assert not list(archive.glob(".*staging")), "OSError 路径必须清掉 staging"
    print("  T25 PASS：复制中途 OSError → 步骤 3 失败、staging 清理、旧数据不动")


def test_t26_repack_refuses_incomplete_old_output():
    """旧产物不完整时必须拒绝重打包，不能把不完整的旧东西静默归档。"""

    # 1) 旧 mapping 被删 → 步骤 1 拒绝
    sb = _make_sandbox("t26-nomapping")
    _build_and_snapshot(sb, "2026-11-16T16:00:00Z")
    (sb.private_dir / "mapping.csv").unlink()
    with _patched_paths(sb), _clock("2026-11-16T16:00:01Z"):
        _expect_pack_error(
            lambda: pack.run_pack(_args(sb, ["--repack"])),
            "步骤 1/旧私有文件缺失",
        )
    assert not list((sb.private_dir / "archive").glob("pack-*"))

    # 2) 旧盲审包被删（只剩私有文件）→ 步骤 1 拒绝
    sb2 = _make_sandbox("t26-nopack")
    mapping_before, _ = _build_and_snapshot(sb2, "2026-11-16T17:00:00Z")
    _force_rmtree(sb2.pack_dir)
    with _patched_paths(sb2), _clock("2026-11-16T17:00:01Z"):
        _expect_pack_error(
            lambda: pack.run_pack(_args(sb2, ["--repack"])),
            "步骤 1/旧盲审包不存在",
        )
    assert (sb2.private_dir / "mapping.csv").read_bytes() == mapping_before
    assert not list((sb2.private_dir / "archive").glob("pack-*"))

    # 3) 旧包缺一个材料文件 → 步骤 1 拒绝
    sb3 = _make_sandbox("t26-shortpack")
    _build_and_snapshot(sb3, "2026-11-16T18:00:00Z")
    victim = sorted((sb3.pack_dir / "formal").glob("*.md"))[0]
    victim.unlink()
    with _patched_paths(sb3), _clock("2026-11-16T18:00:01Z"):
        _expect_pack_error(
            lambda: pack.run_pack(_args(sb3, ["--repack"])),
            "步骤 1/",
        )
    assert not list((sb3.private_dir / "archive").glob("pack-*"))
    print("  T26 PASS：旧 mapping/旧包/旧材料任一缺失都拒绝重打包，归档目录保持干净")


def test_t27_publish_failure_keeps_active_pack():
    """发布后复核失败时，归档目录与活动包都必须保留，不能删了旧包又没有可用的归档。"""

    sb = _make_sandbox("t27")
    _build_and_snapshot(sb, "2026-11-16T19:00:00Z")

    real_verify = pack._verify_archive_tree
    seen = {"n": 0}

    def flaky_verify(root, step="归档复核"):
        seen["n"] += 1
        if seen["n"] == 2:  # 第二次调用是步骤 6 的发布后复核
            raise pack.PackError("步骤 6/发布后复核/归档文件与清单不一致：缺少 ['private/x']")
        return real_verify(root, step)

    pack._verify_archive_tree = flaky_verify
    try:
        with _patched_paths(sb), _clock("2026-11-16T19:00:01Z"):
            _expect_pack_error(
                lambda: pack.run_pack(_args(sb, ["--repack"])),
                "活动包未被清理",
            )
    finally:
        pack._verify_archive_tree = real_verify

    assert seen["n"] >= 2, seen
    assert sb.pack_dir.is_dir(), "活动包不能被清掉"
    assert len(list(sb.pack_dir.glob("formal/*.md"))) >= 1
    archived = [path for path in (sb.private_dir / "archive").glob("pack-*") if path.is_dir()]
    assert archived, "已发布的归档目录必须保留"
    print("  T27 PASS：发布后复核失败 → 归档与活动包都保留，并提示人工核对")


def _make_dir_link(link: Path, target: Path) -> None:
    """在临时仓库里造一个目录链接（junction / symlink 均可）。

    优先用 os.symlink；Windows 未开启开发者模式时会失败，退回 `mklink /J`（junction
    不需要管理员权限）。两种都失败就直接判失败——这类测试不能因为「造不出链接」就静默通过。
    """

    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:
        first_error = exc
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    # 悬空目标时 link.exists() 必然为 False，所以只能看 mklink 的返回码；
    # 真正的「是不是链接」由被测代码自己用 lstat 判断。
    if done.returncode == 0:
        return
    raise AssertionError(
        f"无法在临时目录创建目录链接（symlink: {first_error}; mklink: {done.stdout.strip()} "
        f"{done.stderr.strip()}）——归档链接防护无法验证"
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): _sha(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _active_tree_hashes(sb: Sandbox) -> dict[str, str]:
    """活动位置上的旧产物指纹（不含 `archive/` 历史归档）：伪造前缀 + 相对路径。"""

    result: dict[str, str] = {}
    for name in pack.PRIVATE_FILES:
        path = sb.private_dir / name
        if path.is_file():
            result[f"private/{name}"] = _sha(path)
    for rel, digest in _tree_hashes(sb.pack_dir).items():
        result[f"pack/{rel}"] = digest
    return result


def _relative_to(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def test_t28_no_repack_refuses_full_old_output():
    """不带 --repack 时，完整旧产物必须在写入任何文件之前被拒绝。"""

    sb = _make_sandbox("t28")
    _build_and_snapshot(sb, "2026-11-16T20:00:00Z")
    before = _active_tree_hashes(sb)
    assert "private/mapping.csv" in before and len(before) > 100, len(before)

    code, out, err = _main(sb)
    assert code == 2, (code, out, err)
    assert "发现未归档的旧运行产物" in err, err
    assert "未写入或删除任何文件" in err, err
    assert "--repack" in err, err
    # 拒绝之后旧材料逐字节不变，也没有悄悄写出新的私有文件
    assert _active_tree_hashes(sb) == before
    assert not list((sb.private_dir / pack.ARCHIVE_DIRNAME).glob("pack-*"))
    print("  T28 PASS：不带 --repack 时完整旧产物在写入前拒绝，旧文件逐字节不变")


def test_t29_no_repack_refuses_old_pack_without_mapping():
    """mapping 被删、旧盲审包还在：旧包不能被新私有文件配上（必须在写入前拒绝）。"""

    sb = _make_sandbox("t29")
    _build_and_snapshot(sb, "2026-11-16T20:30:00Z")
    (sb.private_dir / "mapping.csv").unlink()
    before = _active_tree_hashes(sb)
    assert "private/mapping.csv" not in before and before

    code, out, err = _main(sb)
    assert code == 2, (code, out, err)
    assert "发现未归档的旧运行产物" in err, err
    assert str(sb.pack_dir) in err, err
    assert _active_tree_hashes(sb) == before
    existing = {name for name in pack.PRIVATE_FILES if (sb.private_dir / name).exists()}
    assert existing == {"selection_audit.csv", "MAPPING-AUDIT.md"}, existing
    print("  T29 PASS：mapping 被删但旧包还在 → 写入前拒绝，不产生新 mapping")


def test_t30_no_repack_refuses_audit_files_without_mapping():
    """只剩 selection_audit.csv 或 MAPPING-AUDIT.md 也必须拒绝（不只认 mapping）。"""

    for index, keep in enumerate(("selection_audit.csv", "MAPPING-AUDIT.md")):
        name = f"t30-{index}"
        sb = _make_sandbox(name)
        _build_and_snapshot(sb, f"2026-11-16T21:0{index}:00Z")
        _force_rmtree(sb.pack_dir)
        for other in pack.PRIVATE_FILES:
            path = sb.private_dir / other
            if other != keep and path.exists():
                path.unlink()
        before = _active_tree_hashes(sb)
        assert list(before) == [f"private/{keep}"], before

        code, out, err = _main(sb)
        assert code == 2, (keep, code, out, err)
        assert "发现未归档的旧运行产物" in err and keep in err, (keep, err)
        assert _active_tree_hashes(sb) == before
        assert not (sb.private_dir / "mapping.csv").exists()
    print("  T30 PASS：只剩审计文件时同样在写入前拒绝，mapping 不会被重新写出来")


def test_t31_repack_still_archives_complete_old_output():
    """--repack 下完整旧产物仍走成套归档，且 archive/ 不会被当成活动旧产物。"""

    sb = _make_sandbox("t31")
    _build_and_snapshot(sb, "2026-11-16T22:00:00Z")
    with _patched_paths(sb), _clock("2026-11-16T22:30:00Z"):
        _run_pack(sb, ["--repack"])

    archives = [
        path
        for path in (sb.private_dir / pack.ARCHIVE_DIRNAME).glob("pack-*")
        if path.is_dir()
    ]
    assert len(archives) == 1, archives
    assert (archives[0] / pack.ARCHIVE_MANIFEST_NAME).is_file()
    assert len(list(sb.pack_dir.glob("formal/*.md"))) == 84

    stale = pack.stale_output_paths(sb.pack_dir, sb.private_dir)
    rels = [_relative_to(path, sb.private_dir) for path in stale]
    assert "mapping.csv" in rels, rels
    assert not any(rel.startswith(pack.ARCHIVE_DIRNAME) for rel in rels), rels
    print("  T31 PASS：--repack 完整旧产物成功归档，archive/ 不参与活动旧产物判定")


def test_t32_archive_dir_is_not_active_output():
    """只有历史归档目录时，不带 --repack 也必须能正常打包（第 7 条误判防线）。"""

    sb = _make_sandbox("t32")
    archive = sb.private_dir / pack.ARCHIVE_DIRNAME / "pack-20261116T225959Z"
    archive.mkdir(parents=True)
    (archive / pack.ARCHIVE_MANIFEST_NAME).write_text("# 合成历史归档\n", encoding="utf-8")

    assert pack.stale_output_paths(sb.pack_dir, sb.private_dir) == []
    _build(sb)
    assert (sb.private_dir / "mapping.csv").is_file()
    assert archive.is_dir() and (archive / pack.ARCHIVE_MANIFEST_NAME).is_file()
    print("  T32 PASS：历史归档不被误判成未归档旧产物，正常打包不受影响")


def test_t33_manifest_entry_presence_and_file_column():
    """条目存在性只看规范化路径；`file` 列空/错必须在清单解析层就失败，B0 不得再走 legacy 放行。"""

    b0 = pack.B0_RUNS[0]

    # 1) B0 条目真的不存在 → 白名单 legacy（只有「真的没有这条记录」才允许）
    sb = _make_sandbox("t33-legacy")
    with _patched_paths(sb):
        snapshot = pack.snapshot_sources([b0], runs_dir=sb.runs_dir)
        report = pack.verify_snapshot(snapshot, {})
    assert [row["status"] for row in report] == ["legacy_no_manifest"], report

    # 2) `file` 列空/错：清单本身就不可信，load_manifest 阶段就抛，不会走到 verify_snapshot
    file_cases = [
        ("t33-file-empty", {"file": ""}, "file 列为空"),
        ("t33-file-wrong", {"file": "plan.json"}, "与路径不符"),
    ]
    for name, patch, fragment in file_cases:
        box = _make_sandbox(name)
        rows = _manifest_rows(box)
        _row_for(rows, b0).update(patch)
        _write_manifest(box, rows)
        with _patched_paths(box):
            _expect_pack_error(lambda b=box: pack.manifest_hashes(b.manifest), fragment)
        code, _, err = _main(box)
        assert code == 2, (name, code, err)
        assert fragment in err, (name, err)
        assert "legacy" not in err, (name, err)
        assert not box.pack_dir.exists(), name

    # 3) 第二层防线：即使有人绕过清单解析、直接把 dict 交给 verify_snapshot，file 空/错仍判失败
    box = _make_sandbox("t33-second-layer")
    rows = _manifest_rows(box)
    good_entry = dict(_row_for(rows, b0))
    with _patched_paths(box):
        snapshot = pack.snapshot_sources([b0], runs_dir=box.runs_dir)
        for patch, fragment in (
            ({"file": ""}, "file_empty"),
            ({"file": "plan.json"}, "manifest_file"),
        ):
            entry = dict(good_entry, **patch)
            _expect_pack_error(
                lambda e=entry: pack.verify_snapshot(snapshot, {entry["relative_path"]: e}),
                "与 manifest 不一致",
                fragment,
            )
        wrong = dict(good_entry, file="plan.json")
        text = _expect_pack_error(
            lambda: pack.verify_snapshot(snapshot, {wrong["relative_path"]: wrong}),
            "与 manifest 不一致",
        )
        assert "legacy" not in text, text
        assert "manifest_file='plan.json'" in text, text

    # 4) sha256 / size_bytes / records 仍由 verify_snapshot 判定（file 列此时是对的）
    cases = [
        ("t33-sha-wrong", {"sha256": "0" * 64}, "sha256"),
        ("t33-size-wrong", {"size_bytes": "1"}, "size_bytes"),
        ("t33-records-wrong", {"records": "1"}, "records"),
    ]
    for name, patch, fragment in cases:
        box = _make_sandbox(name)
        rows = _manifest_rows(box)
        _row_for(rows, b0).update(patch)
        _write_manifest(box, rows)
        with _patched_paths(box):
            snapshot = pack.snapshot_sources([b0], runs_dir=box.runs_dir)
            manifest = pack.manifest_hashes(box.manifest)
            text = _expect_pack_error(
                lambda: pack.verify_snapshot(snapshot, manifest),
                "与 manifest 不一致",
                fragment,
            )
        assert "legacy" not in text, (name, text)

    # 5) 非 B0 条目缺失 → manifest_missing（B0 才允许 legacy）
    box = _make_sandbox("t33-missing")
    rows = [
        row
        for row in _manifest_rows(box)
        if not row["relative_path"].endswith(pack.MAIN_RUN + "/" + pack.SOURCE_FILE)
    ]
    _write_manifest(box, rows)
    with _patched_paths(box):
        snapshot = pack.snapshot_sources([pack.MAIN_RUN], runs_dir=box.runs_dir)
        _expect_pack_error(
            lambda: pack.verify_snapshot(snapshot, pack.manifest_hashes(box.manifest)),
            "在 manifest 中缺失",
            pack.MAIN_RUN,
        )
    print("  T33 PASS：manifest 条目缺失与 file 列错误分开判定，B0 不再被误放行")


def test_t35_blank_path_row_fails_both_tools():
    """路径列空着但同行有内容：打包器与核查器都必须判整份清单不可信，不能当空行跳过。"""

    import check_auto as ca  # noqa: PLC0415 - 只在一致性测试里需要

    sb = _make_sandbox("t35-blank-path")
    rows = _manifest_rows(sb)
    blank = dict(_row_for(rows, pack.MAIN_RUN))
    blank["relative_path"] = ""
    rows.append(blank)
    _write_manifest(sb, rows)

    with _patched_paths(sb):
        pack_text = _expect_pack_error(lambda: pack.manifest_hashes(sb.manifest), "relative_path 为空")
        saved = ca.MANIFEST_PATH
        ca.MANIFEST_PATH = sb.manifest
        try:
            check_text = None
            try:
                ca.manifest_hashes(sb.manifest)
            except ca.CheckError as error:
                check_text = str(error)
        finally:
            ca.MANIFEST_PATH = saved
        assert check_text is not None, "核查器必须拒绝空路径行"
        assert "relative_path 为空" in check_text, check_text

    # 两个工具的结论必须一致：都拒绝，且都指出是空路径行
    assert "清单不可信" in pack_text, pack_text
    assert "清单不可信" in check_text, check_text
    code, _, err = _main(sb)
    assert code == 2, (code, err)
    assert "relative_path 为空" in err, err
    assert not sb.pack_dir.exists()
    assert not sb.private_dir.exists(), "失败时不得写出任何私有文件"
    print("  T35 PASS：空路径行在打包器与核查器两侧都 fail-closed，且未写任何产物")


def test_t36_archive_symlink_never_escapes_private_dir():
    """导出目录被换成链接时，归档必须拒绝，且旧私有文件逐字节不变。"""

    # 1) archive/ 本身是指向仓库外的链接 → 复制前就拒绝
    sb = _make_sandbox("t36-archive-link")
    _build(sb)
    sb.private_dir.mkdir(parents=True, exist_ok=True)
    outside = TMP_ROOT / "t36-outside"
    outside.mkdir(parents=True, exist_ok=True)
    archive = sb.private_dir / pack.ARCHIVE_DIRNAME
    _make_dir_link(archive, outside)
    before = _tree_hashes(sb.private_dir)
    with _patched_paths(sb):
        _expect_pack_error(
            lambda: pack.ensure_archive_root(sb.private_dir),
            "符号链接",
            "拒绝",
        )
    assert _tree_hashes(sb.private_dir) == before, "拒绝时不得动任何旧文件"
    assert not any(outside.iterdir()), "仓库外目录不得出现任何私有材料"

    # 2) 归档目标名被预先做成链接（含指向仓库外）→ 拒绝覆盖，旧文件不动
    sb2 = _make_sandbox("t36-target-link")
    with _clock("2026-11-16T00:00:00Z"):
        _build(sb2)
        stamp = "20261116T000000Z"
        archive2 = sb2.private_dir / pack.ARCHIVE_DIRNAME
        archive2.mkdir(parents=True, exist_ok=True)
        outer = TMP_ROOT / "t36-target-outside"
        outer.mkdir(parents=True, exist_ok=True)
        _make_dir_link(archive2 / f"pack-{stamp}", outer)
        before2 = _tree_hashes(sb2.private_dir)
        with _patched_paths(sb2):
            _expect_pack_error(
                lambda: pack.archive_previous_outputs(sb2.pack_dir, sb2.private_dir, "2026-11-16T00:00:00Z"),
                "符号链接",
            )
        assert _tree_hashes(sb2.private_dir) == before2, "拒绝时不得改动旧归档与旧私有文件"
        assert not any(outer.iterdir()), "仓库外目录不得出现任何归档内容"
    print("  T36 PASS：归档目录/目标为链接时拒绝，旧文件指纹不变，仓库外目录保持空")


def test_t37_pack_dir_type_checked_before_writing():
    """pack_dir 是普通文件或悬空链接时，必须在写任何私有文件之前拒绝。"""

    # 1) 普通文件占用 pack_dir
    sb = _make_sandbox("t37-file")
    sb.pack_dir.parent.mkdir(parents=True, exist_ok=True)
    sb.pack_dir.write_text("not a dir\n", encoding="utf-8", newline="\n")
    code, _, err = _main(sb)
    assert code == 2, (code, err)
    assert "不是目录" in err, err
    assert sb.pack_dir.read_text(encoding="utf-8") == "not a dir\n", "占用文件不得被改写"
    assert not sb.private_dir.exists(), "被拒绝时不得写私有文件"

    # 2) 悬空链接占用 pack_dir
    sb2 = _make_sandbox("t37-dangling")
    sb2.pack_dir.parent.mkdir(parents=True, exist_ok=True)
    _make_dir_link(sb2.pack_dir, TMP_ROOT / "t37-no-such-target")
    code, _, err = _main(sb2)
    assert code == 2, (code, err)
    assert "符号链接" in err, err
    assert not sb2.private_dir.exists(), "被拒绝时不得写私有文件"

    # 3) 正常空目录仍然合法（不能把上面的规则误伤成「目录必须不存在」）
    sb3 = _make_sandbox("t37-empty-dir")
    sb3.pack_dir.mkdir(parents=True, exist_ok=True)
    _build(sb3)
    assert any(sb3.pack_dir.iterdir()), "空目录应能正常打包"
    print("  T37 PASS：pack_dir 普通文件/悬空链接提前拒绝且无副作用，正常空目录不受影响")



def test_t34_manifest_path_normalization_blocks_duplicates():
    """同一路径的其它写法必须被规范化后判重复，不能绕过清单改写检查。"""

    assert pack.normalize_manifest_path("experiments//graduation/./runs/a.jsonl") == (
        "experiments/graduation/runs/a.jsonl"
    )
    assert pack.normalize_manifest_path("experiments\\graduation\\runs\\a.jsonl") == (
        "experiments/graduation/runs/a.jsonl"
    )
    _expect_pack_error(lambda: pack.normalize_manifest_path("../outside.jsonl"), "不是仓库内相对路径")
    _expect_pack_error(lambda: pack.normalize_manifest_path("/abs/path.jsonl"), "不是仓库内相对路径")

    variants = [
        lambda value: value.replace("/", "\\"),
        lambda value: value.replace("/runs/", "//runs/./"),
    ]
    for index, mutate in enumerate(variants):
        sb = _make_sandbox(f"t34-{index}")
        rows = _manifest_rows(sb)
        duplicate = dict(_row_for(rows, pack.MAIN_RUN))
        duplicate["relative_path"] = mutate(duplicate["relative_path"])
        assert duplicate["relative_path"] != _row_for(rows, pack.MAIN_RUN)["relative_path"]
        rows.append(duplicate)
        _write_manifest(sb, rows)
        _expect_pack_error(lambda s=sb: pack.manifest_hashes(s.manifest), "重复条目")
        code, _, err = _main(sb)
        assert code == 2 and "重复条目" in err, (code, err)
        assert not sb.pack_dir.exists()
    print("  T34 PASS：反斜杠与重复分隔符写法规范化后判重复，无法绕过清单检查")


TESTS = [
    ("T1", test_t1_private_outside_repo),
    ("T2", test_t2_pack_equals_private),
    ("T3", test_t3_pack_contains_private),
    ("T4", test_t4_private_contains_pack),
    ("T5", test_t5_private_gate_real_git),
    ("T6", test_t6_synthetic_counts_and_selection),
    ("T7", test_t7_pack_and_mapping_backtrace),
    ("T8", test_t8_manifest_match),
    ("T9", test_t9_manifest_sha_mismatch),
    ("T10", test_t10_manifest_size_mismatch),
    ("T11", test_t11_manifest_records_mismatch),
    ("T12", test_t12_manifest_missing_entries),
    ("T13", test_t13_b0_legacy_optional),
    ("T14", test_t14_hash_from_disk_not_cache),
    ("T15", test_t15_source_changed_during_pack),
    ("T16", test_t16_repack_archives_old_output),
    ("T17", test_t17_archive_collision_keeps_old_data),
    ("T18", test_t18_rater_orders_guaranteed_different),
    ("T19", test_t19_config_frozen),
    ("T20", test_t20_real_data_untouched),
    ("T21", test_t21_private_gate_real_git),
    ("T22", test_t22_manifest_empty_fields_fail),
    ("T23", test_t23_manifest_duplicate_entries),
    ("T24", test_t24_archive_verifies_copied_bytes),
    ("T25", test_t25_archive_copy_failure_keeps_old),
    ("T26", test_t26_repack_refuses_incomplete_old_output),
    ("T27", test_t27_publish_failure_keeps_active_pack),
    ("T28", test_t28_no_repack_refuses_full_old_output),
    ("T29", test_t29_no_repack_refuses_old_pack_without_mapping),
    ("T30", test_t30_no_repack_refuses_audit_files_without_mapping),
    ("T31", test_t31_repack_still_archives_complete_old_output),
    ("T32", test_t32_archive_dir_is_not_active_output),
    ("T33", test_t33_manifest_entry_presence_and_file_column),
    ("T34", test_t34_manifest_path_normalization_blocks_duplicates),
    ("T35", test_t35_blank_path_row_fails_both_tools),
    ("T36", test_t36_archive_symlink_never_escapes_private_dir),
    ("T37", test_t37_pack_dir_type_checked_before_writing),
]


def _force_rmtree(path: Path) -> list[str]:
    """删掉临时仓库；Windows 上 .git 里的只读文件会让 rmtree 静默失败，先改权限重试。"""

    def _retry(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        func(target)

    if not path.exists():
        return []
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:
        shutil.rmtree(path, onerror=_retry)
    return [] if not path.exists() else [str(path)]


def _run_all() -> int:
    global REAL_BEFORE
    REAL_BEFORE = _fingerprints()
    passed = 0
    failures = []
    try:
        for name, func in TESTS:
            try:
                func()
                passed += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  {name} FAIL: {type(exc).__name__}: {exc}")
        assert _fingerprints() == REAL_BEFORE, "真实 runs/feedback.db 在测试期间被改动"
    finally:
        # 断言失败也要清理，绝不能遗留临时仓库；清理失败必须显式报出来
        leftovers = _force_rmtree(TMP_ROOT)
        if leftovers:
            failures.append(f"清理失败，残留临时目录：{leftovers}")
            print(f"  清理失败：残留 {leftovers}")
    print(f"结果：{passed}/{len(TESTS)} PASS")
    if failures:
        print("失败明细：")
        for item in failures:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
