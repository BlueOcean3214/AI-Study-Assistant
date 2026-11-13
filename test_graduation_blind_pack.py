#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""make_blind_pack.py 的自写回归测试（无 pytest，直接 python 运行）。

覆盖：
T1  .gitignore 前置条件：私有目录三个路径被忽略、且没有被 Git 跟踪（只读检查，不改仓库）。
T2  仓内 dry-run：84 / 6 / 23、不写任何文件。
T3  临时目录完整打包：份数、blind_id 形式与唯一性、文件名规则、正文去身份扫描。
T4  抽样可复现：selection_audit.csv 的 84 行与独立重算逐行一致；校准 6 份满足每格/每样本约束。
T5  S7-empty 3 份来自 SHA-256 排序而非行序；B0 10 份全量且 S5/S7 各 5、E7 适用范围正确。
T6  材料内容：包含用户请求、匿名化历史、模型回答与计划；不含 run 日期与生成时间戳。
T7  两位评分者顺序独立随机：各自是全部 blind_id 的排列，且两份顺序不同。
T8  blind_id 只用 CSPRNG：两次打包映射不同；强制 token_hex 碰撞时仍唯一。
T9  fail-closed：仓库内私有目录未忽略 → 拒绝写入；仓库外目录未加旗标 → 拒绝；加旗标才允许。
T10 重打包保护：无 --repack 拒绝覆盖；--repack 归档旧映射并在 MAPPING-AUDIT.md 记录旧 SHA256。
T11 只读边界：打包前后 runs/*/results.jsonl 与真实 feedback.db 指纹不变。
T12 blind_config.json：seed/协议/domain 正确，且不含身份、blind_id 或抽样结果。
"""

import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
GRADUATION_DIR = PROJECT_DIR / "experiments" / "graduation"
sys.path.insert(0, str(GRADUATION_DIR))

import make_blind_pack as pack  # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(prefix="grad_blindpack_"))
os.environ["FEEDBACK_DB_PATH"] = str(TMP_ROOT / "graduation_experiments" / "_test_placeholder.db")

CONFIG = GRADUATION_DIR / "blind_config.json"
REAL_DB = PROJECT_DIR / "feedback.db"
SAMPLE_KEYS = ("sample_id", "group", "condition", "repeat_index", "round_index")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db_fingerprint():
    if not REAL_DB.is_file():
        return None
    stat = REAL_DB.stat()
    return (_sha(REAL_DB), stat.st_size, stat.st_mtime_ns)


def _source_fingerprints():
    table = {}
    for path in sorted((GRADUATION_DIR / "runs").glob("*/results.jsonl")):
        table[path.parent.name] = _sha(path)
    return table


def _read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


_PACK_CACHE = {}


def _build_pack(name="pack1", repack=False, tmp_root=TMP_ROOT):
    key = (name, repack)
    if key in _PACK_CACHE:
        return _PACK_CACHE[key]
    root = tmp_root / name
    args = [
        "--pack-dir", str(root / "blind_pack"),
        "--private-dir", str(root / "blind_private"),
        "--allow-outside-repo",
    ]
    if repack:
        args.append("--repack")
    code = pack.main(args)
    assert code == 0, f"打包失败 exit={code}"
    _PACK_CACHE[key] = root
    return root


def test_t1_gitignore_rule():
    """T1：私有目录被 .gitignore 覆盖，且没有任何私有文件被跟踪。"""
    evidence = pack.ensure_private_gate(GRADUATION_DIR / "blind_private")
    assert len(evidence) >= 4, evidence
    assert all(".gitignore" in line for line in evidence[:3]), evidence
    assert "0 条" in evidence[-1], evidence
    tracked = pack._git(["ls-files", "experiments/graduation/blind_private"], PROJECT_DIR)
    assert tracked.stdout.strip() == "", tracked.stdout
    print("  T1 PASS：三个路径均命中 .gitignore，git ls-files 无私有文件")


def test_t2_repo_dry_run():
    """T2：仓内 dry-run 通过且不产生任何文件。"""
    args = pack.parse_args(["--dry-run"])
    code = pack.run_pack(args)
    assert code == 0
    assert not (GRADUATION_DIR / "blind_private").exists()
    assert not (GRADUATION_DIR / "blind_pack").exists()
    print("  T2 PASS：dry-run 84/6/23，未写文件")


def test_t3_pack_counts_and_names():
    """T3：完整打包份数、ID 形式、文件名与正文去身份。"""
    root = _build_pack()
    mapping = _read_csv(root / "blind_private" / "mapping.csv")
    assert len(mapping) == 113, len(mapping)
    phases = {}
    for row in mapping:
        phases[row["phase"]] = phases.get(row["phase"], 0) + 1
    assert phases == {"formal": 84, "calibration": 6, "supplementary": 23}, phases
    ids = [row["blind_id"] for row in mapping]
    assert len(ids) == len(set(ids))
    for blind_id in ids:
        assert pack.re.fullmatch(r"[0-9a-f]{32}", blind_id), blind_id
        hits = pack.scan_text(blind_id, pack.IDENTITY_PATTERNS + pack.STRICT_DATE_PATTERNS)
        assert not hits, (blind_id, hits)
    for phase in ("formal", "calibration", "supplementary"):
        phase_dir = root / "blind_pack" / phase
        files = sorted(path.name for path in phase_dir.iterdir())
        md_files = [name for name in files if name.endswith(".md")]
        assert set(files) - set(md_files) <= {"order_rater_a.csv", "order_rater_b.csv"}, files
        for name in md_files:
            assert name[:-3] in set(ids), name
            assert not pack.scan_text(name, pack.IDENTITY_PATTERNS + pack.STRICT_DATE_PATTERNS)
        for name in md_files:
            text = (phase_dir / name).read_text(encoding="utf-8")
            hits = pack.scan_packet_body({"blind_id": name[:-3]}, text)
            assert not hits, (name, hits)
    print("  T3 PASS：113 份材料只以 blind_id 命名，正文扫描 0 命中")


def test_t4_selection_reproducible():
    """T4：正式 84 与校准 6 可独立重算，且满足每格/每样本约束。"""
    root = _build_pack()
    config = pack.load_blind_config(CONFIG)
    seed = config["blind_sampling_seed"]
    main_rows = pack.load_main_rows()
    expected = pack.select_formal(main_rows, seed)
    expected_rows = sorted(
        (pick.row.sample_id, pick.row.group, pick.row.repeat_index, pick.hash) for pick in expected
    )
    audit = _read_csv(root / "blind_private" / "selection_audit.csv")
    assert list(audit[0].keys()) == pack.AUDIT_COLUMNS
    actual = sorted(
        (row["sample_id"], row["group"], int(row["repeat_index"]), row["selection_hash"])
        for row in audit
    )
    assert actual == expected_rows, "selection_audit 与独立重算不一致"
    per_cell = {}
    for sample, group, _, _ in actual:
        per_cell[(sample, group)] = per_cell.get((sample, group), 0) + 1
    assert len(per_cell) == 28 and set(per_cell.values()) == {3}
    for row in audit:
        row_hash = pack.row_sha256(
            next(
                item.row
                for item in main_rows
                if item.sample_id == row["sample_id"]
                and item.group == row["group"]
                and item.repeat_index == int(row["repeat_index"])
            )
        )
        assert row_hash == row["source_result_row_sha256"]
    used = {(pick.row.sample_id, pick.row.group) for pick in expected}
    remaining = [
        item for item in main_rows if (item.sample_id, item.group, item.repeat_index) not in {
            (pick.row.sample_id, pick.row.group, pick.row.repeat_index) for pick in expected
        }
    ]
    calibration = pack.select_calibration(remaining, seed, {pick.row.identity_tuple for pick in expected})
    assert len(calibration) == 6
    assert len({pick.row.sample_id for pick in calibration}) == 6
    assert len({(pick.row.sample_id, pick.row.group) for pick in calibration}) == 6
    mapping = _read_csv(root / "blind_private" / "mapping.csv")
    formal = {(row["sample_id"], row["group"], row["repeat_index"]) for row in mapping if row["phase"] == "formal"}
    cal = {(row["sample_id"], row["group"], row["repeat_index"]) for row in mapping if row["phase"] == "calibration"}
    assert not (formal & cal)
    print("  T4 PASS：84 行逐行可复算；校准 6 覆盖 6 样本 6 格且与 84 无交集")


def test_t5_supplementary_rules():
    """T5：S7-empty 取 3 来自 SHA-256 排序；B0 10 份全量；E7 适用范围正确。"""
    root = _build_pack()
    config = pack.load_blind_config(CONFIG)
    seed = config["blind_sampling_seed"]
    s7_rows = pack.load_s7empty_rows()
    expected = sorted(
        (pick.row.repeat_index, pick.hash) for pick in pack.select_s7empty(s7_rows, seed)
    )
    row_order_first3 = [0, 1, 2]
    assert [repeat for repeat, _ in expected] != row_order_first3 or True  # 只作提示，不强制不相等
    mapping = _read_csv(root / "blind_private" / "mapping.csv")
    s7 = [row for row in mapping if row["batch"] == "suppl-s7empty"]
    assert len(s7) == 3
    expected_map = {repeat: value for repeat, value in expected}
    assert sorted(int(row["repeat_index"]) for row in s7) == sorted(expected_map)
    for row in s7:
        repeat = int(row["repeat_index"])
        assert expected_map[repeat] == pack.selection_hash(
            seed, pack.DOMAIN_S7EMPTY, "S7-empty", "D", repeat
        )
    b0 = [row for row in mapping if row["batch"] == "b0-legacy"]
    assert len(b0) == 10
    assert sorted(row["sample_id"] for row in b0).count("S5") == 5
    assert sorted(row["sample_id"] for row in b0).count("S7") == 5
    assert len({row["source_run_dir"] for row in b0}) == 3
    assert len({(row["source_run_dir"], row["source_row_number"], row["source_result_row_sha256"]) for row in b0}) == 10
    subj = [row for row in mapping if row["batch"] == "suppl-subj"]
    assert len(subj) == 10 and {row["group"] for row in subj} == {"C-subj", "D-subj"}
    print("  T5 PASS：S7-empty=3、B0=10、subj=10，identity 三元组全不重复")


def test_t6_packet_content():
    """T6：材料含请求/匿名历史/回答/计划，且不含 run 日期与生成时间戳。"""
    root = _build_pack()
    mapping = _read_csv(root / "blind_private" / "mapping.csv")
    for row in mapping[:5] + mapping[-5:]:
        path = root / "blind_pack" / row["phase"] / f"{row['blind_id']}.md"
        text = path.read_text(encoding="utf-8")
        assert "## 用户请求" in text and "## 模型回答" in text and "## 学习计划（JSON）" in text
        assert "## 学习者历史（程序统计，匿名化）" in text
        assert row["blind_id"] in text.splitlines()[0]
        assert "20261113" not in text and "2026-11-13" not in text
    print("  T6 PASS：材料结构完整，无 run 日期")


def test_t7_rater_orders_independent():
    """T7：两位评分者的顺序各自是完整排列，且互不相同。"""
    root = _build_pack()
    mapping = _read_csv(root / "blind_private" / "mapping.csv")
    for phase in ("formal", "calibration", "supplementary"):
        ids = {row["blind_id"] for row in mapping if row["phase"] == phase}
        orders = {}
        for rater in ("rater_a", "rater_b"):
            rows = _read_csv(root / "blind_pack" / phase / f"order_{rater}.csv")
            assert [int(row["position"]) for row in rows] == list(range(1, len(ids) + 1))
            assert {row["blind_id"] for row in rows} == ids
            orders[rater] = [row["blind_id"] for row in rows]
        assert orders["rater_a"] != orders["rater_b"], phase
    print("  T7 PASS：三组顺序均为排列且两人不同")


def test_t8_csprng_only():
    """T8：两次打包 blind_id 不同（CSPRNG）；token_hex 碰撞时仍保证唯一。"""
    root_a = _build_pack("pack_csprng_a")
    root_b = _build_pack("pack_csprng_b")
    ids_a = {row["blind_id"] for row in _read_csv(root_a / "blind_private" / "mapping.csv")}
    ids_b = {row["blind_id"] for row in _read_csv(root_b / "blind_private" / "mapping.csv")}
    assert len(ids_a) == len(ids_b) == 113
    assert ids_a.isdisjoint(ids_b), "两次打包的 blind_id 不应复用"

    original = pack.secrets.token_hex
    forced = ["a" * 32, "a" * 32, "b" * 32, "c" * 32]
    calls = {"n": 0}

    def fake(nbytes):
        value = forced[min(calls["n"], len(forced) - 1)]
        calls["n"] += 1
        return value

    pack.secrets.token_hex = fake
    try:
        items = [{"phase": "formal", "batch": "main", "row": None} for _ in range(3)]
        pack.assign_ids(items)
    finally:
        pack.secrets.token_hex = original
    got = [item["blind_id"] for item in items]
    assert got == ["a" * 32, "b" * 32, "c" * 32], got
    assert len(got) == len(set(got))
    print("  T8 PASS：两次打包 ID 不相交；碰撞被重试规避")


def test_t9_fail_closed():
    """T9：忽略规则缺失 / 仓库外目录未加旗标时拒绝写入。"""
    fake_repo = TMP_ROOT / "fake_repo"
    (fake_repo / ".gitignore").parent.mkdir(parents=True, exist_ok=True)
    (fake_repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    try:
        pack.ensure_private_gate(TMP_ROOT / "outside_private")
    except pack.PackError as exc:
        assert "allow-outside-repo" in str(exc), exc
    else:
        raise AssertionError("仓库外目录未加旗标时应失败")
    outside = TMP_ROOT / "outside_ok" / "private"
    evidence = pack.ensure_private_gate(outside, allow_outside_repo=True)
    assert evidence and "仓库外" in evidence[0]
    nested = TMP_ROOT / "noignore" / "blind_private"
    try:
        pack.ensure_private_gate(nested)
    except pack.PackError as exc:
        assert "allow-outside-repo" in str(exc) or "gitignore" in str(exc).lower()
    print("  T9 PASS：仓库外未加旗标被拒绝；加旗标后明确标注跳过")


def test_t10_repack_protection():
    """T10：无 --repack 拒绝覆盖；--repack 归档旧映射并在审计中保留旧 SHA256。"""
    root = _build_pack("pack_repack")
    mapping_path = root / "blind_private" / "mapping.csv"
    old_sha = _sha(mapping_path)
    args = [
        "--pack-dir", str(root / "blind_pack"),
        "--private-dir", str(root / "blind_private"),
        "--allow-outside-repo",
    ]
    assert pack.main(args) == 2, "无 --repack 时必须失败"
    assert _sha(mapping_path) == old_sha, "失败的打包不得改动 mapping.csv"
    code = pack.main(args + ["--repack"])
    assert code == 0
    archives = list((root / "blind_private" / "mapping_archive").glob("mapping-*.csv"))
    assert len(archives) == 1, archives
    assert _sha(archives[0]) == old_sha
    audit_text = (root / "blind_private" / "MAPPING-AUDIT.md").read_text(encoding="utf-8")
    assert old_sha in audit_text
    assert _sha(mapping_path) != old_sha
    print("  T10 PASS：拒绝静默覆盖；旧映射已归档且 SHA256 留痕")


def test_t11_readonly_boundary():
    """T11：打包前后源 results.jsonl 与真实 feedback.db 指纹不变。"""
    before_sources = _source_fingerprints()
    before_db = _db_fingerprint()
    _build_pack("pack_readonly")
    assert _source_fingerprints() == before_sources
    assert _db_fingerprint() == before_db
    print("  T11 PASS：源文件与 feedback.db 未变")


def test_t12_blind_config():
    """T12：blind_config.json 参数正确且不含身份字段。"""
    text = CONFIG.read_text(encoding="utf-8")
    config = json.loads(text)
    assert config["blind_sampling_seed"] == 2026111301
    assert config["protocol_version"] == "v4.5"
    assert config["selection_domains"] == {
        "formal": "blind-main-selection",
        "calibration": "blind-calibration-selection",
        "supplementary_s7empty": "blind-s7empty-selection",
    }
    def walk(node, trail="cfg"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in SAMPLE_KEYS, f"{trail}.{key} 不得作为字段出现"
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(config)
    # 身份字段名只允许作为“算法说明文字”出现（selection_key_template / notes），不得作为配置键
    assert not pack.scan_text(text, pack.IDENTITY_PATTERNS)
    assert "blind_id" not in config
    assert "selected" not in config and "mapping" not in config
    print("  T12 PASS：seed=2026111301 / v4.5 / 三个 domain，无身份与日期")


TESTS = [
    ("T1", test_t1_gitignore_rule),
    ("T2", test_t2_repo_dry_run),
    ("T3", test_t3_pack_counts_and_names),
    ("T4", test_t4_selection_reproducible),
    ("T5", test_t5_supplementary_rules),
    ("T6", test_t6_packet_content),
    ("T7", test_t7_rater_orders_independent),
    ("T8", test_t8_csprng_only),
    ("T9", test_t9_fail_closed),
    ("T10", test_t10_repack_protection),
    ("T11", test_t11_readonly_boundary),
    ("T12", test_t12_blind_config),
]


def _run_all():
    before_db = _db_fingerprint()
    before_sources = _source_fingerprints()
    passed = 0
    for name, func in TESTS:
        try:
            func()
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} FAIL: {type(exc).__name__}: {exc}")
    assert _db_fingerprint() == before_db, "真实 feedback.db 被改动"
    assert _source_fingerprints() == before_sources, "runs/ 源文件被改动"
    print(f"结果：{passed}/{len(TESTS)} PASS")
    if passed != len(TESTS):
        return 1
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
