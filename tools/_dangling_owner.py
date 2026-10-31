# -*- coding: utf-8 -*-
"""定位卷面里悬空 rId 来自哪道题（manifest 现建，不信 picker.json 的摘要）。"""
import os
import re
import sqlite3
import sys
import zipfile

from lxml import etree

sys.path.insert(0, r"E:\OCR\app")
import docx_rich     # noqa: E402
import picker        # noqa: E402

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOC = r"E:\OCR\app\out\acceptance_v1\paper\acceptance_paper.docx"

conn = sqlite3.connect("file:%s?mode=ro" % r"E:\OCR\app\data\app.db".replace("\\", "/"), uri=True)
conn.row_factory = sqlite3.Row
man = picker.build({"title": "验收卷 A · 七年级数学",
                    "mix": {"选择": 10, "填空": 8, "解答": 6},
                    "requirements": {"need_answer": True, "exclude_empty": True},
                    "seed": 42}, conn, pid="P20261031-001")
conn.close()

with zipfile.ZipFile(DOC) as z:
    root = etree.fromstring(z.read("word/document.xml"))
    rels = {rel.get("Id") for rel in etree.fromstring(z.read("word/_rels/document.xml.rels"))}

boxes = [el for el in root.find("{%s}body" % W) if isinstance(el.tag, str)
         and el.tag.endswith("}tbl")]
print("boxes:", len(boxes), "items:", len(man["items"]))

for item, box in zip(man["items"], boxes):
    xml = etree.tostring(box, encoding="unicode")
    used = set()
    for el in etree.fromstring(xml.encode("utf-8")).iter():
        if not isinstance(el.tag, str):
            continue
        for a in ("id", "embed", "link"):
            v = el.get("{%s}%s" % (R_NS, a))
            if v:
                used.add(v)
    dangling = sorted(used - rels)
    if not dangling:
        continue
    print("题框 idx=%s no=%s 悬空 %s" % (item["idx"], item["no"], dangling))
    src = os.path.join(r"E:\OCR", item["no"] + ".docx")
    for t in docx_rich.extract_tokens(src):
        if t.kind in ("table", "embed"):
            hits = re.findall(r'r:(?:id|embed|link)="([^"]+)"', t.xml or "")
            print("    %-6s refs=%-28s rows=%s cols=%s media=%d"
                  % (t.kind, hits, t.rows, t.cols, len(t.media)))
    with zipfile.ZipFile(src) as z:
        print("    源 media:", [n for n in z.namelist() if n.startswith("word/media/")][:10])
