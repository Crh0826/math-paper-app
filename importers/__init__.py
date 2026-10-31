# -*- coding: utf-8 -*-
"""t51 · 题库导入接口（JSON / Excel / CSV / docx，多学科，幂等，带导入报告）。

用法::

    from importers import import_file
    rep = import_file("data/qbank.xlsx", "excel")          # → dict，同时写 data/import_report.json

命令行见 `import_qbank.py`。docx 分支**复用 build_db 的抽取逻辑**，不重复实现。
"""
from __future__ import annotations

import os

from .core import DEFAULT_SUBJECT, QTYPES  # noqa: F401
from .core import ALIASES, _subject_pk, import_file, normalize_item, subject_scope, upsert_items  # noqa: F401

__all__ = ["import_file", "normalize_item", "upsert_items", "subject_scope",
           "import_docx", "DEFAULT_SUBJECT", "QTYPES", "ALIASES"]

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(APP_DIR, "data", "app.db")
DEFAULT_REPORT = os.path.join(APP_DIR, "data", "import_report.json")


def import_docx(src_dir=None, db_path=None, fresh=False, limit=0, report_path=None):
    """docx 题库导入：直接调用 build_db.build（抽取逻辑只有一份，避免两套实现漂移）。"""
    import build_db

    res = build_db.build(limit=limit, db_path=db_path or DEFAULT_DB,
                         fresh=fresh, report=report_path)
    return res


def list_subjects(db_path=None):
    import db as dbmod

    conn = dbmod.connect(db_path or DEFAULT_DB)
    try:
        return [dict(r) for r in dbmod.list_subjects(conn)]
    finally:
        conn.close()
