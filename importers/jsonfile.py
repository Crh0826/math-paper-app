# -*- coding: utf-8 -*-
"""t51 · JSON 题库导入：``{"subject","grade","version","items":[...]}``（也接受裸数组）。"""
from __future__ import annotations

import json


def read_json(path):
    """返回 (meta, items)；items 里每项必须至少带 no/qtype/stem/answer。"""
    with open(path, encoding="utf-8-sig") as f:
        doc = json.load(f)
    if isinstance(doc, list):
        return {}, doc
    if not isinstance(doc, dict):
        raise ValueError("JSON 顶层必须是对象或数组")
    items = doc.get("items") or doc.get("questions") or []
    if not isinstance(items, list):
        raise ValueError("items 必须是数组")
    meta = {k: doc.get(k) for k in ("subject", "grade", "version") if doc.get(k)}
    return meta, items
