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
from .core import (ALIASES, _subject_pk, guess_kind, import_file, normalize_item,  # noqa: F401
                   normalize_rows, read_source, subject_scope, upsert_items)

__all__ = ["import_file", "normalize_item", "upsert_items", "subject_scope",
           "normalize_rows", "read_source", "guess_kind",
           "import_docx", "DEFAULT_SUBJECT", "QTYPES", "ALIASES"]

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(APP_DIR, "data", "app.db")
DEFAULT_REPORT = os.path.join(APP_DIR, "data", "import_report.json")


def import_docx(src_dir=None, db_path=None, fresh=False, limit=0, report_path=None):
    """docx 题库导入：直接调用 build_db.build（抽取逻辑只有一份，避免两套实现漂移）。

    ``build()`` 的签名随建库脚本演进，这里按探测到的关键字调用，缺参数就退化。
    """
    import inspect

    import build_db

    kwargs = {"limit": limit, "db_path": db_path or DEFAULT_DB, "fresh": fresh}
    params = inspect.signature(build_db.build).parameters
    if report_path and "report" in params:
        kwargs["report"] = report_path
    res = build_db.build(**kwargs)
    if report_path and "report" not in params:
        try:
            build_db.write_report(db_path=kwargs["db_path"], report_path=report_path)
        except Exception:  # noqa: BLE001 - 报告失败不影响导入结果
            pass
    return res


def list_subjects(db_path=None):
    import db as dbmod

    conn = dbmod.connect(db_path or DEFAULT_DB)
    try:
        return [dict(r) for r in dbmod.list_subjects(conn)]
    finally:
        conn.close()
