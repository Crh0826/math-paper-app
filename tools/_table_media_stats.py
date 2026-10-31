# -*- coding: utf-8 -*-
"""全库统计：多少道题的 table token 里带媒体引用（r:embed / r:id / r:link）。

这类题一旦被组卷命中，paper 直接把 table XML 原样插入、不重映射 rId →
卷面 docx 出现悬空 rId → Word 判「文件已损坏」 → 整份卷子打不开、导不出 PDF。
"""
import json
import re
import sqlite3

c = sqlite3.connect("file:E:/OCR/app/data/app.db?mode=ro", uri=True)
pat = re.compile(r'r:(?:id|embed|link)="([^"]+)"')
hits = []
with_table = 0
for no, tokens_json in c.execute("SELECT no, tokens_json FROM questions"):
    try:
        toks = json.loads(tokens_json or "[]")
    except Exception:                                   # noqa: BLE001
        continue
    for t in toks or []:
        if t.get("kind") != "table":
            continue
        with_table += 1
        m = pat.findall(t.get("xml") or "")
        if m:
            hits.append((no, len(m), m[:3]))
            break
print("带 table token 的题:", with_table)
print("表格里带媒体引用的题:", len(hits))
print("样例:", hits[:10])
q = c.execute("SELECT COUNT(*) FROM questions WHERE TRIM(COALESCE(answer_raw,''))<>''").fetchone()[0]
print("其中（有答案的比例待算）total_with_answer:", q)
