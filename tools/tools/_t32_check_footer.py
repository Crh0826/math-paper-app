# -*- coding: utf-8 -*-
"""复验页脚新口径的 5 条判据（读 print_chain.json + 直接解析合并件文字层）。"""
import io
import json
import re

import pymupdf

J = r"E:\OCR\app\out\acceptance_v1\print_chain.json"
PDF = r"E:\OCR\app\out\acceptance_v1\print_P20261031-001.pdf"
d = json.load(io.open(J, encoding="utf-8"))
db = d["steps"]["db_papers_row"]
pp = d["steps"]["print_pdf"]

print("A = 卷面页:", pp["paper_pages_meta"], "| B = 卡页:", pp["card_pages_meta"])
print("① manifest['paper']['pages'] 写回:", db.get("card_start_page"), "->",
      "card_start_page 应为 A+1 =", pp["paper_pages_meta"] + 1)
print("   卡页 label:", db.get("card_page_labels"),
      "| 期望:", [pp["paper_pages_meta"] + i for i in range(1, pp["card_pages_meta"] + 1)])
print("③ print.pdf 页数:", pp["pages"], "| A+B =", pp["pages_expected"],
      "| 通过:", pp["pages"] == pp["pages_expected"])
print("④ 页序:", pp["page_kinds"], "| 通过:", pp["order_ok"])

doc = pymupdf.open(PDF)
print("\n合并件逐页页脚原文（卷面页有文字层，卡页是图片）：")
for i, page in enumerate(doc, 1):
    text = page.get_text()
    foot_lines = [ln.strip() for ln in text.splitlines()
                  if re.search(r"第\s*\d+\s*页|共\s*\d+\s*页|卷面|答题卡", ln)]
    print("  page %d: %s" % (i, foot_lines))
    hits = len(re.findall(r"共\s*\d+\s*页", text))
    print("      「共 N 页」出现次数:", hits, "（>1 = 叠了两行页脚）")
doc.close()

# 卷面单独 PDF 也看一遍
doc = pymupdf.open(r"E:\OCR\app\out\acceptance_v1\print_chain.json".replace("print_chain.json", "print_P20261031-001.pdf"))
doc.close()
