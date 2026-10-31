# -*- coding: utf-8 -*-
"""查真库 papers/submissions 现状（给最终报告用）。"""
import json
import sqlite3

c = sqlite3.connect(r"E:\OCR\app\data\app.db")
c.row_factory = sqlite3.Row
papers = [dict(r) for r in c.execute(
    "SELECT pid, title, created_at, n_items, deleted_at, docx_path, pdf_path"
    " FROM papers ORDER BY pid").fetchall()]
subs = [dict(r) for r in c.execute(
    "SELECT sid, pid, created_at, score, full FROM submissions ORDER BY sid").fetchall()]
c.close()
print("papers:", json.dumps(papers, ensure_ascii=False, indent=1))
print("submissions:", json.dumps(subs, ensure_ascii=False, indent=1))
print("total papers:", len(papers),
      "| visible:", sum(1 for p in papers if not p["deleted_at"]),
      "| soft-deleted:", sum(1 for p in papers if p["deleted_at"]))
