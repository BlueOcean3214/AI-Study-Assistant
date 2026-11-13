"""五轮随机区组调度的确定性计划（plan.json）构建、校验与原子落盘。

设计（docs/GRADUATION-EXPERIMENT-DESIGN.md §2.3 / §5.3）：

- 每个样本是一个区组（block_unit="sample"）：先跑完该样本的全部轮次，再进入
  下一个样本；样本区块之间的顺序也做确定性随机化。
- 每个样本内 5 轮（round_count=5），每轮 A/B/C/D 各一次，轮内顺序确定性随机；
  repeat_index == round_index，取值 0..4。
- 随机化只用 SHA-256 排序键，不依赖 Python 全局 random，也不声称
  random.Random 的序列跨版本一致：

      排序键 = SHA-256( "seed|algorithm_version|domain|part1|part2|..." )[hex]
      升序；键相同时按次级键（样本名 / 组名）稳定排序。

  domain 区分用途：样本区块顺序用 "sample-block-order"，轮内组顺序用
  "round-group-order"。算法版本写进计划，改算法必须同时改 algorithm_version。
- plan.json 是执行顺序的唯一依据：执行器只读计划，不重算顺序。
- plan_sha256 = SHA-256(规范化 JSON(整份计划去掉 plan_sha256 字段))，
  规范化 = key 升序 + 紧凑分隔符 + UTF-8 + 不转义非 ASCII；加载时重算校验。
- 落盘是原子写：同目录 tmp + flush + fsync + os.replace；失败不留残缺文件。

本模块不导入实验执行器、不发任何请求。
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PLAN_NAME = "plan.json"

SCHEMA_VERSION = 1
DEFAULT_SCHEME = "five_round_randomized_block_v1"
DEFAULT_ALGORITHM_VERSION = "sha256-block-order-v1"
DEFAULT_SEED = 20261113
DEFAULT_ROUND_COUNT = 5
BLOCK_UNIT = "sample"

SAMPLE_BLOCK_DOMAIN = "sample-block-order"
ROUND_GROUP_DOMAIN = "round-group-order"

# 与 build_prompts.SUBJECT_GROUPS / EMPTY_SAMPLE_ID 保持一致（只用于给任务打
# kind 标签；本模块刻意不 import build_prompts，保持纯算法、可独立测试）。
SUPPLEMENTARY_GROUPS = ("C-subj", "D-subj")
EMPTY_SAMPLE_ID = "S7-empty"
B0_GROUP = "B0"

KIND_MAIN = "main"
KIND_B0 = "b0"
KIND_SUPPLEMENTARY = "supplementary"


class PlanIntegrityError(RuntimeError):
    """计划缺失、被篡改、与当前配置/身份不一致，或结果无法安全续跑。"""


# ---------------------------------------------------------------------------
# 规范化 JSON 与哈希
# ---------------------------------------------------------------------------


def canonical_json(value):
    """规范化 JSON 文本：稳定 key 顺序、紧凑分隔符、UTF-8、不转义中文。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_sha256(config):
    """整份 config.json 的规范化哈希；任何字段变化都会让续跑 fail-closed。"""

    return sha256_text(canonical_json(config))


def sort_key(seed, algorithm_version, domain, *parts):
    """确定性排序键：SHA-256("seed|algorithm_version|domain|part1|part2|...")。"""

    payload = "|".join(
        [str(seed), str(algorithm_version), str(domain)] + [str(part) for part in parts]
    )

    return sha256_text(payload)


def _ordered(values, seed, algorithm_version, domain, tiebreak):
    def key(value):
        digest = sort_key(seed, algorithm_version, domain, *value)
        return (digest, tuple(str(item) for item in tiebreak(value)))

    return sorted(values, key=key)


def order_samples(sample_ids, seed, algorithm_version):
    """样本区块顺序：按 SHA-256 排序键升序，平手按样本名。"""

    return [
        value[0]
        for value in _ordered(
            [(str(sample_id),) for sample_id in sample_ids],
            seed,
            algorithm_version,
            SAMPLE_BLOCK_DOMAIN,
            tiebreak=lambda value: value,
        )
    ]


def order_groups(sample_id, round_index, groups, seed, algorithm_version):
    """某一轮内 A/B/C/D 的顺序：按 SHA-256 排序键升序，平手按组名。"""

    return [
        value[2]
        for value in _ordered(
            [(str(sample_id), int(round_index), str(group)) for group in groups],
            seed,
            algorithm_version,
            ROUND_GROUP_DOMAIN,
            tiebreak=lambda value: (value[2],),
        )
    ]


# ---------------------------------------------------------------------------
# 计划构建
# ---------------------------------------------------------------------------


def task_kind(sample_id, group):
    """任务类别：main（主对照）/ b0（legacy 探索组）/ supplementary（补充分析）。"""

    if group == B0_GROUP:
        return KIND_B0

    if group in SUPPLEMENTARY_GROUPS or sample_id == EMPTY_SAMPLE_ID:
        return KIND_SUPPLEMENTARY

    return KIND_MAIN


def build_schedule(
    *,
    experiment_id,
    sample_groups,
    rounds,
    seed,
    scheme=DEFAULT_SCHEME,
    algorithm_version=DEFAULT_ALGORITHM_VERSION,
    config_hash,
    task_meta=None,
    created_at=None,
):
    """构建完整计划（尚不含 plan_sha256 之外的运行时状态）。

    sample_groups：{sample_id: [group, ...]}，组集合按声明顺序给出（会被排序去重，
    以避免调用方顺序影响哈希）；b0 任务由调用方决定是否放进来。
    task_meta：{(sample_id, group): {"sample_selection": ..., "b0_sample_override": ...}}。
    """

    sample_groups = {
        str(sample_id): sorted({str(group) for group in groups})
        for sample_id, groups in sample_groups.items()
    }

    if not sample_groups:
        raise PlanIntegrityError("计划为空：没有任何样本/组")

    if int(rounds) < 1:
        raise PlanIntegrityError(f"轮次数必须 >= 1，收到 {rounds!r}")

    task_meta = dict(task_meta or {})

    created_at = created_at or datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )

    sample_order = order_samples(sample_groups, seed, algorithm_version)
    blocks = []
    plan_seq = 0

    for sample_id in sample_order:
        tasks = []

        for round_index in range(int(rounds)):
            for group in order_groups(
                sample_id, round_index, sample_groups[sample_id], seed, algorithm_version
            ):
                meta = task_meta.get((sample_id, group), {})

                tasks.append(
                    {
                        "plan_seq": plan_seq,
                        "round_index": round_index,
                        "repeat_index": round_index,
                        "group": group,
                        "kind": task_kind(sample_id, group),
                        "sample_selection": meta.get("sample_selection", "default"),
                        "b0_sample_override": bool(meta.get("b0_sample_override", False)),
                    }
                )

                plan_seq += 1

        blocks.append({"sample_id": sample_id, "tasks": tasks})

    plan = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": str(algorithm_version),
        "experiment_id": str(experiment_id),
        "created_at": created_at,
        "seed": int(seed),
        "scheme": str(scheme),
        "block_unit": BLOCK_UNIT,
        "round_count": int(rounds),
        "config_sha256": str(config_hash),
        "sample_order": list(sample_order),
        "blocks": blocks,
    }

    plan["plan_sha256"] = plan_digest(plan)

    return plan


def plan_digest(plan):
    """plan_sha256：规范化 JSON（自身不参与哈希）的 SHA-256。"""

    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}

    return sha256_text(canonical_json(payload))


def plan_tasks(plan):
    """按执行顺序展开全部任务（block → round → group 的 plan_seq 顺序）。"""

    return [task for block in plan["blocks"] for task in block["tasks"]]


def plan_identities(plan):
    """计划内全部实验身份的集合；身份仍是四元组，plan_seq 不参与。"""

    return {
        (
            plan["experiment_id"],
            block["sample_id"],
            task["group"],
            task["repeat_index"],
        )
        for block in plan["blocks"]
        for task in block["tasks"]
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _expect(plan, field, expected):
    actual = plan.get(field)

    if actual != expected:
        raise PlanIntegrityError(
            f"plan.json 的 {field} 与当前运行不一致：计划内 {actual!r}，当前 {expected!r}；"
            "拒绝重洗或覆盖已有计划，请换新的 --run-dir。"
        )


def _require_int(where, field, value):
    """整数类型校验：bool 是 int 的子类，必须单独拒绝，不能用 == 顶替。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanIntegrityError(
            f"plan.json 的 {where} 的 {field} 类型不正确：{value!r}"
            f"（{type(value).__name__}），必须是整数（且不能是 bool）；"
            "拒绝自动类型转换，请换新的 --run-dir。"
        )

    return value


def _require_str(where, field, value):
    if not isinstance(value, str):
        raise PlanIntegrityError(
            f"plan.json 的 {where} 的 {field} 类型不正确：{value!r}"
            f"（{type(value).__name__}），必须是字符串；"
            "拒绝自动类型转换，请换新的 --run-dir。"
        )

    return value


def _check_plan_scalar_types(plan):
    """顶层标量字段的严格类型校验，必须早于任何 _expect() 等值比较。

    SCHEMA_VERSION 是整数 1，而 True == 1、1.0 == 1，若先做等值比较，
    篡改后重算 plan_sha256 的 plan.json 就能绕过完整性要求。
    """

    _require_int("顶层字段", "schema_version", plan.get("schema_version"))
    _require_int("顶层字段", "seed", plan.get("seed"))
    _require_int("顶层字段", "round_count", plan.get("round_count"))


def _check_plan_types(plan, sample_order, blocks):
    """整份计划的严格类型校验；在任何范围/顺序/身份比较之前执行。

    Python 里 True == 1、False == 0，单纯的值比较无法识别 bool 冒充整数，
    因此 plan_seq / round_index / repeat_index 必须先确认是 int 且不是 bool，
    group / sample_id / sample_order 必须先确认是字符串。
    """

    _check_plan_scalar_types(plan)

    for index, sample_id in enumerate(sample_order):
        _require_str(f"sample_order 第 {index} 项", "sample_order", sample_id)

    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise PlanIntegrityError(
                f"plan.json 的 blocks 第 {block_index} 项必须是 JSON 对象，"
                f"收到 {type(block).__name__}"
            )

        _require_str(f"blocks 第 {block_index} 项", "sample_id", block.get("sample_id"))
        tasks = block.get("tasks")

        if not isinstance(tasks, list):
            raise PlanIntegrityError(
                f"plan.json 的 {block.get('sample_id')} 区块缺少 tasks"
            )

        for task_index, task in enumerate(tasks):
            where = f"{block.get('sample_id')} 第 {task_index} 个任务"

            if not isinstance(task, dict):
                raise PlanIntegrityError(
                    f"plan.json 的 {where} 必须是 JSON 对象，收到 {type(task).__name__}"
                )

            _require_int(where, "plan_seq", task.get("plan_seq"))
            _require_int(where, "round_index", task.get("round_index"))
            _require_int(where, "repeat_index", task.get("repeat_index"))
            _require_str(where, "group", task.get("group"))


def validate_plan(
    plan,
    *,
    experiment_id,
    config_hash,
    seed,
    scheme,
    algorithm_version,
    rounds,
    sample_groups,
):
    """加载后的完整校验：任何字段被改、任务矩阵变化、plan_seq 断裂都失败。"""

    if not isinstance(plan, dict):
        raise PlanIntegrityError("plan.json 的顶层必须是 JSON 对象")

    if plan.get("plan_sha256") != plan_digest(plan):
        raise PlanIntegrityError(
            "plan.json 的 plan_sha256 校验失败：计划已被修改，拒绝继续"
        )

    _check_plan_scalar_types(plan)

    _expect(plan, "schema_version", SCHEMA_VERSION)
    _expect(plan, "algorithm_version", str(algorithm_version))
    _expect(plan, "experiment_id", str(experiment_id))
    _expect(plan, "seed", int(seed))
    _expect(plan, "scheme", str(scheme))
    _expect(plan, "block_unit", BLOCK_UNIT)
    _expect(plan, "round_count", int(rounds))
    _expect(plan, "config_sha256", str(config_hash))

    expected_samples = {
        str(sample_id): sorted({str(group) for group in groups})
        for sample_id, groups in sample_groups.items()
    }
    sample_order = plan.get("sample_order")
    blocks = plan.get("blocks")

    if not isinstance(sample_order, list) or not isinstance(blocks, list):
        raise PlanIntegrityError("plan.json 缺少 sample_order / blocks")

    # 先做严格类型校验，再做任何范围、顺序、身份比较：True == 1 会让
    # 普通 == 比较漏掉 bool 冒充整数，sorted() 混类型还会直接 TypeError。
    _check_plan_types(plan, sample_order, blocks)

    if sorted(sample_order) != sorted(expected_samples):
        raise PlanIntegrityError(
            "plan.json 的样本集合与当前 config/CLI 不一致："
            f"计划内 {sorted(sample_order)}，当前 {sorted(expected_samples)}"
        )

    if [block.get("sample_id") for block in blocks] != list(sample_order):
        raise PlanIntegrityError("plan.json 的 blocks 顺序与 sample_order 不一致")

    if len(set(sample_order)) != len(sample_order):
        raise PlanIntegrityError("plan.json 的 sample_order 有重复样本")

    expected_seq = 0

    for block in blocks:
        sample_id = block.get("sample_id")
        tasks = block.get("tasks")

        if not isinstance(tasks, list):
            raise PlanIntegrityError(f"plan.json 的 {sample_id} 区块缺少 tasks")

        expected_groups = expected_samples.get(sample_id)

        if expected_groups is None:
            raise PlanIntegrityError(f"plan.json 出现了计划外样本：{sample_id}")

        if len(tasks) != len(expected_groups) * int(rounds):
            raise PlanIntegrityError(
                f"plan.json 的 {sample_id} 任务数 {len(tasks)} "
                f"≠ {len(expected_groups)} 组 × {rounds} 轮"
            )

        for round_index in range(int(rounds)):
            round_tasks = [
                task for task in tasks if task.get("round_index") == round_index
            ]

            if sorted(task.get("group") for task in round_tasks) != expected_groups:
                raise PlanIntegrityError(
                    f"plan.json 的 {sample_id} 第 {round_index} 轮组集合不是 "
                    f"{expected_groups}：{[task.get('group') for task in round_tasks]}"
                )

        seen = set()

        for task in tasks:
            if task.get("plan_seq") != expected_seq:
                raise PlanIntegrityError(
                    f"plan.json 的 plan_seq 不连续：期望 {expected_seq}，"
                    f"收到 {task.get('plan_seq')}"
                )

            if task.get("repeat_index") != task.get("round_index"):
                raise PlanIntegrityError(
                    f"plan.json 的 repeat_index 必须等于 round_index：{task}"
                )

            if task.get("kind") != task_kind(sample_id, task.get("group")):
                raise PlanIntegrityError(
                    f"plan.json 的 kind 与组不匹配：{sample_id}/{task.get('group')}"
                )

            identity = (sample_id, task["group"], task["repeat_index"])

            if identity in seen:
                raise PlanIntegrityError(f"plan.json 出现重复身份：{identity}")

            seen.add(identity)
            expected_seq += 1

    return plan


# ---------------------------------------------------------------------------
# 原子写 / 读取
# ---------------------------------------------------------------------------


def write_plan_atomic(path, plan):
    """原子写 plan.json：tmp + flush + fsync + os.replace；失败不留残缺文件。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )

    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass

        raise

    # 目录项落盘是尽力而为：Windows 上打开目录失败不影响已完成的原子替换。
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)

        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass

    return path


def load_plan(path):
    """读取 plan.json 并重算 plan_sha256；非法 JSON 与篡改一律失败。"""

    path = Path(path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise PlanIntegrityError(f"读取 plan.json 失败：{error}") from error

    try:
        plan = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanIntegrityError(f"plan.json 不是合法 JSON：{error}") from error

    if not isinstance(plan, dict) or "plan_sha256" not in plan:
        raise PlanIntegrityError("plan.json 缺少 plan_sha256")

    if plan["plan_sha256"] != plan_digest(plan):
        raise PlanIntegrityError(
            "plan.json 的 plan_sha256 校验失败：计划已被修改，拒绝继续"
        )

    return plan
