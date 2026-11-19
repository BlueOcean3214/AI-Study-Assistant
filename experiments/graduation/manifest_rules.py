#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""执行报告附录 A（`docs/GRADUATION-EXPERIMENT-FILE-MANIFEST.csv`）的共享解析规则。

`make_blind_pack.py`（打包器）与 `check_auto.py`（核查器）必须对同一份 manifest
得出同一个结论。两边各写一套解析时，一边修了规范化、另一边还只做反斜杠替换，
同一份清单就会有两种「可信」判定——打包放行、核查拒绝，或反过来。

因此这里只放**无副作用**的纯规则：不 import 任一工具、不读写临时文件、不打印。
失败一律抛 `ManifestError`，由调用方包装成自己的异常类型（`PackError` / `CheckError`），
这样两边的 CLI 退出码与错误前缀保持不变。

规则（与 2026-11-16 第四轮任务书一致）：

* `relative_path` 必须是有效的仓库内相对路径：拒绝盘符、绝对路径、`..` 上跳、
  控制字符；反斜杠与重复分隔符、`./` 都规范化成唯一形式。
* CSV 中真正全空的行可以忽略。
* 只要该行其他列有内容而 `relative_path` 为空，整份清单不可信（不能静默跳过，
  否则「清空路径」就能让 B0 记录重新掉进 legacy 例外）。
* 规范化后重复的路径一律失败，错误信息给出两个行号。
* `file` 列必须等于规范路径的文件名（本仓库为 `results.jsonl`）：空或写错都失败。
"""

from __future__ import annotations

import csv
import posixpath
import re
from pathlib import Path

RELATIVE_PATH_COLUMN = "relative_path"
FILE_COLUMN = "file"
REQUIRED_COLUMNS = (RELATIVE_PATH_COLUMN, FILE_COLUMN)

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_FORBIDDEN_CHARS = ("\r", "\n", "\x00")


class ManifestError(ValueError):
    """manifest 不可信。调用方负责包装成自己的异常类型。"""


def row_has_content(row) -> bool:
    """该行除空格外是否还写了东西（含 DictReader 的 restkey 多余列）。"""

    for key, value in row.items():
        if key is None:
            if value:
                return True
            continue
        if str(value or "").strip():
            return True
    return False


def _reject_not_relative(text: str, original, when: str) -> None:
    """`text` 必须是仓库内相对路径；盘符、绝对路径、`..` 上跳一律失败。

    规范化前后各调用一次，`when` 用于说明是在哪一步发现的（便于定位原始写法）。
    """

    if _DRIVE_PREFIX.match(text):
        raise ManifestError(
            f"manifest 的 {RELATIVE_PATH_COLUMN} 不是仓库内相对路径（{when}带盘符），"
            f"拒绝使用：{original!r}"
        )
    if text.startswith("/") or posixpath.isabs(text):
        raise ManifestError(
            f"manifest 的 {RELATIVE_PATH_COLUMN} 不是仓库内相对路径（{when}是绝对路径），"
            f"拒绝使用：{original!r}"
        )
    if text in (".", "..") or text.startswith("../"):
        raise ManifestError(
            f"manifest 的 {RELATIVE_PATH_COLUMN} 不是仓库内相对路径（{when}仍指向仓库外），"
            f"拒绝使用：{original!r}"
        )


def normalize_relative_path(value) -> str:
    """把 `relative_path` 规范化成唯一形式，作为条目存在性的主键。

    只 `strip("/")` 或只替换反斜杠都不够：`a\\b`、`a//b`、`./a/b`、`a/./b`
    指向同一个文件，规范化不彻底就能用另一种写法绕过「重复条目」检查，
    条目缺失也就重新可辩称成 legacy。空输入返回空串，由调用方决定是「全空行」还是失败。
    """

    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    for char in _FORBIDDEN_CHARS:
        if char in text:
            raise ManifestError(
                f"manifest 的 {RELATIVE_PATH_COLUMN} 含有控制字符，拒绝使用：{value!r}"
            )
    _reject_not_relative(text, value, "规范化前")
    normalized = posixpath.normpath(text)
    # 规范化会把 `./C:/outside/results.jsonl`、`a/../C:/outside/results.jsonl` 变成
    # `C:/outside/results.jsonl`：盘符在规范化之后才露出来，只在规范化前查一次就能绕过。
    # 因此规范化后整组再查一遍（盘符 / 绝对路径 / `..` 上跳）。
    _reject_not_relative(normalized, value, "规范化后")
    return normalized.strip("/")


def expected_file_name(relative: str) -> str:
    """规范路径对应的文件名。`file` 列必须正好等于它。"""

    return posixpath.basename(relative)


def entry_file_problem(relative, entry, expected_file=None) -> tuple[str, str]:
    """检查一行的 `file` 列 → (问题代码, 说明)。

    问题代码：`""`（通过）、`file_empty`、`file`。说明在通过时是实际取值，
    便于调用方原样写进报告（打包器的 `manifest_file` 字段）。
    """

    expected = expected_file or expected_file_name(str(relative))
    raw = str(entry.get(FILE_COLUMN) or "").strip()
    if not raw:
        return "file_empty", f"{FILE_COLUMN} 列为空（应为 {expected}）"
    if raw != expected:
        return "file", f"{FILE_COLUMN}={raw!r} 与路径不符（应为 {expected}）"
    return "", raw


def validate_header(fieldnames) -> None:
    """表头必须自洽：含 `relative_path` 与 `file` 各一次，且不得有空列名或重复列名。

    `csv.DictReader` 遇到重复列名**不报错**：同名列互相覆盖，字典里只留最后一列的值，
    读出来的行看起来完全正常。清单若带着重复的 `relative_path`/`file`，
    「哪一列才是被冻结的版本」在读取阶段就已经丢失，所以必须在迭代任何数据行之前拒绝。
    """

    names = ["" if name is None else str(name) for name in (fieldnames or [])]
    if not names:
        raise ManifestError(
            "manifest 缺少表头（文件为空或第一行为空行），拒绝使用：无法确认清单包含哪些列"
        )
    blank = [str(index + 1) for index, name in enumerate(names) if not name.strip()]
    if blank:
        raise ManifestError(f"manifest 表头第 {'、'.join(blank)} 列为空列名，清单不可信")
    duplicates = []
    seen = set()
    for name in names:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise ManifestError(
            f"manifest 表头出现重复列名：{'、'.join(duplicates)}，拒绝使用"
            "（同名列会被 csv 静默覆盖，读到的值不可信）"
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in names]
    if missing:
        raise ManifestError(
            f"manifest 表头缺少必需列：{'、'.join(missing)}（实际表头：{'、'.join(names)}）"
        )


def load_manifest(path) -> dict[str, dict]:
    """读取 manifest → {规范化 relative_path: row}；不可信时整份抛错，不返回半份表。

    缺文件返回空表（打包器/核查器各自的「manifest 缺失」语义由调用方判断）。
    """

    path = Path(path)
    table: dict[str, dict] = {}
    if not path.is_file():
        return table
    first_lines: dict[str, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        # 读取表头就校验：空文件、无表头、缺列、重复列都在解析第一行数据之前失败，
        # 不让 DictReader 把重复列悄悄合并成一行看似正常的数据。
        validate_header(reader.fieldnames)
        for row in reader:
            # DictReader.line_num 是最近一次读取的行号；行号从 2 开始（第 1 行是表头）
            line_no = reader.line_num
            if not row_has_content(row):
                continue
            relative = normalize_relative_path(row.get(RELATIVE_PATH_COLUMN) or "")
            if not relative:
                raise ManifestError(
                    f"manifest 第 {line_no} 行 {RELATIVE_PATH_COLUMN} 为空，但该行其他字段有内容；"
                    "清单不可信（空路径不能当成「没有这条记录」跳过）"
                )
            code, detail = entry_file_problem(relative, row)
            if code:
                raise ManifestError(f"manifest 第 {line_no} 行（{relative}）{detail}")
            if relative in table:
                raise ManifestError(
                    f"manifest 中 {relative} 出现重复条目（第 {first_lines[relative]} 行与第 "
                    f"{line_no} 行），拒绝静默覆盖：请先确认哪一条才是被冻结的版本"
                )
            first_lines[relative] = line_no
            table[relative] = row
    return table
