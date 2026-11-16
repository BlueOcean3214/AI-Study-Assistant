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
    if _DRIVE_PREFIX.match(text):
        raise ManifestError(
            f"manifest 的 {RELATIVE_PATH_COLUMN} 不是仓库内相对路径（带盘符），拒绝使用：{value!r}"
        )
    if text.startswith("/"):
        raise ManifestError(
            f"manifest 的 {RELATIVE_PATH_COLUMN} 不是仓库内相对路径（绝对路径），拒绝使用：{value!r}"
        )
    normalized = posixpath.normpath(text)
    if normalized in (".", "..") or normalized.startswith("../") or posixpath.isabs(normalized):
        raise ManifestError(
            f"manifest 的 {RELATIVE_PATH_COLUMN} 不是仓库内相对路径，拒绝使用：{value!r}"
        )
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
