# -*- coding: utf-8 -*-
"""列出最近的判卷提交，并用修好的规则重判其中最新一次（找出用户截图那次）。"""
import json
import sqlite3
import sys

sys.path.insert(0, r"E:\OCR\app")

import grade  # noqa: E402

DB = r"E:\OCR\app\data\app.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT sid,pid,created_at,score,full,detail_json FROM submissions "
                    "ORDER BY created_at DESC LIMIT 6").fetchall()
print("最近提交：")
for r in rows:
    print(f"  {r['sid']}  {r['pid']}  {r['created_at']}  {r['score']}/{r['full']}")

for r in rows[:4]:
    detail = json.loads(r["detail_json"] or "{}")
    items = []
    for k in ("items", "units", "detail"):
        v = detail.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            items = v
            break
    if not items:
        continue
    texts = [str(i.get("text") or i.get("recognized") or "") for i in items]
    if not any(texts):
        continue
    print(f"\n=== 重判 {r['sid']}（{len(items)} 个单元）===")
    print(f"{'题号':>6} {'空':>2} {'标准答案':<20} {'识别值':<20} {'旧':<8} {'新'}")
    changed = 0
    for it in items:
        no, blank = it.get("no"), it.get("blank")
        exp = str(it.get("expected") or it.get("answer_key") or "")
        got = str(it.get("text") or it.get("recognized") or "")
        old = it.get("verdict")
        qtype = it.get("qtype") or "填空"
        if not exp:
            new = "(不判分)"
        else:
            v, why = grade._judge(got, exp, qtype)
            new = v if v == old else f"{v} ← {why[:24]}"
            if v != old:
                changed += 1
        print(f"{str(no):>6} {str(blank):>2} {exp:<20.20} {got:<20.20} {str(old):<8} {new}")
    print(f"  变化 {changed} 个单元")
    break
conn.close()
