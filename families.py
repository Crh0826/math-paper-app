# -*- coding: utf-8 -*-
"""M0-e · 题族抽取：`【例 N】` 与 `【变式 N-M】` 归为同族。

这批题库来自"举一反三"系列文档：每个【例 N】下面挂若干【变式 N-M】，是同一考点的变式。
实测全量有 2,671 个族、平均 2.94 题 —— 错题定向训练可以直接"取同族变式"，不需要模型生成。
"""
from __future__ import annotations

import os
import re
from typing import Optional, Tuple

# 【例10】 / 【例 10】 / 【变式4-1】 / 【变式 10-2】 / 【变式4】
_例 = re.compile(r"【\s*例\s*(\d+)\s*】")
_变式 = re.compile(r"【\s*变式\s*(\d+)\s*(?:[-－—]\s*(\d+))?\s*】")


def parse_label(text: str) -> Optional[Tuple[str, str, str]]:
    """返回 (family_key, role, label)：

    ('例10', 'example', '例10')
    ('例10', 'variant', '变式10-2')
    非题族题返回 None。
    """
    if not text:
        return None
    head = text[:120]
    m = _例.search(head)
    if m:
        n = int(m.group(1))
        return (f"例{n}", "example", f"例{n}")
    m = _变式.search(head)
    if m:
        n = int(m.group(1))
        v = m.group(2)
        label = f"变式{n}-{v}" if v is not None else f"变式{n}"
        return (f"例{n}", "variant", label)
    return None


def family_key(text: str, src_title: str = "") -> Optional[str]:
    """题族键：同一来源文档内，例号相同即同族（不同文档的"例1"互不相干）。"""
    hit = parse_label(text)
    if not hit:
        return None
    base = os.path.splitext(os.path.basename(src_title or ""))[0][:60]
    return f"{base}::{hit[0]}"
