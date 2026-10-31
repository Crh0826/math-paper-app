# -*- coding: utf-8 -*-
"""查 acceptance_paper.docx 是不是真的坏了：zip 完整性 + python-docx 打开 + 重新生成对比。"""
import hashlib
import io
import os
import sqlite3
import sys
import zipfile

sys.path.insert(0, r"E:\OCR\app")
import paper     # noqa: E402
import picker    # noqa: E402

DOC = r"E:\OCR\app\out\acceptance_v1\paper\acceptance_paper.docx"


def check(path, label):
    print("=" * 60)
    print(label, path, os.path.getsize(path) if os.path.exists(path) else "MISSING")
    if not os.path.exists(path):
        return
    b = io.open(path, "rb").read()
    print(" sha1:", hashlib.sha1(b).hexdigest()[:12], "zip?", b[:2] == b"PK")
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            print(" zip testzip:", bad, "| parts:", len(z.namelist()))
    except Exception as e:                              # noqa: BLE001
        print(" zip FAIL:", e)
    try:
        from docx import Document
        d = Document(path)
        print(" python-docx OK, body blocks:", len(d.element.body),
              "tables:", sum(1 for el in d.element.body
                             if isinstance(el.tag, str) and el.tag.endswith("}tbl")))
    except Exception as e:                              # noqa: BLE001
        print(" python-docx FAIL:", e)


check(DOC, "现有产物")

# 重新生成一份到新名字（同一 manifest）
conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(r"E:\OCR\app\data\app.db").replace("\\", "/"),
                       uri=True)
conn.row_factory = sqlite3.Row
m = picker.build({"title": "验收卷 A · 七年级数学",
                  "mix": {"选择": 10, "填空": 8, "解答": 6},
                  "requirements": {"need_answer": True, "exclude_empty": True},
                  "seed": 42}, conn, pid="P20261031-001")
conn.close()
out = paper.build_docx(m, r"E:\OCR\app\out\acceptance_v1\paper", name="rebuilt_paper")
check(out["docx"], "重新生成")

# Word 能不能打开这两份
for path in (DOC, out["docx"]):
    try:
        res = paper.docx_to_pdf(path, path.replace(".docx", "_probe.pdf"))
        print("Word 打开成功:", os.path.basename(path), res)
    except Exception as e:                              # noqa: BLE001
        print("Word 打开失败:", os.path.basename(path), str(e)[:160])
