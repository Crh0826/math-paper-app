# -*- coding: utf-8 -*-
"""t51 · 表格导入：CSV / Excel(.xlsx) → 统一行 dict（表头原样保留，别名映射在 core 里做）。"""
from __future__ import annotations

import csv
import os


def read_csv(path):
    """CSV（自动识别 UTF-8-BOM / UTF-8 / GBK，中文 Excel 导出的通常是 GBK）。"""
    last = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            if rows:
                return {}, rows
        except UnicodeDecodeError as exc:      # noqa: PERF203 —— 逐个编码试
            last = exc
    if last:
        raise last
    return {}, []


def read_excel(path):
    """Excel 第一张工作表 → [{表头: 值}]。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    rows = [r for r in rows if any(v not in (None, "") for v in r)]
    if not rows:
        return {}, []
    header = [("" if v is None else str(v)).strip() for v in rows[0]]
    out = []
    for r in rows[1:]:
        row = {}
        for i, key in enumerate(header):
            if not key:
                continue
            row[key] = r[i] if i < len(r) else None
        out.append(row)
    return {}, out


def read_table(path, kind="excel"):
    if kind == "csv" or os.path.splitext(path)[1].lower() == ".csv":
        return read_csv(path)
    return read_excel(path)
