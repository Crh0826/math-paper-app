# -*- coding: utf-8 -*-
"""用修好的等价判断，重判用户那张卷（只重算，不重拍）。"""
import json
import sqlite3
import sys

sys.path.insert(0, r"E:\OCR\app")

import grade  # noqa: E402

DB = r"E:\OCR\app\data\app.db"
SID = "S20261101-103711-b813ae"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT pid, detail_json FROM submissions WHERE sid=?", (SID,)).fetchone()
conn.close()
if not row:
    print("找不到提交", SID)
    raise SystemExit(1)
detail = json.loads(row["detail_json"] or "{}")
items = detail.get("items") or detail.get("units") or []
if not items:
    # 兼容别的结构
    for k in detail:
        if isinstance(detail[k], list) and detail[k] and isinstance(detail[k][0], dict):
            items = detail[k]
            break
print("卷号", row["pid"], "，判分单元", len(items))
print(f"{'题号':>6} {'空':>2} {'标准答案':<22} {'识别值':<22} {'旧判定':<8} {'新判定'}")
for it in items:
    no = it.get("no")
    blank = it.get("blank")
    exp = str(it.get("expected") or it.get("answer_key") or "")
    got = str(it.get("text") or it.get("recognized") or "")
    old = it.get("verdict")
    qtype = it.get("qtype") or "填空"
    if not exp:
        new = "(不判分)"
    else:
        v, why = grade._judge(got, exp, qtype)
        new = v if v == old else "%s ← %s" % (v, why[:26])
    print(f"{str(no):>6} {str(blank):>2} {exp:<22.22} {got:<22.22} {str(old):<8} {new}")
