# -*- coding: utf-8 -*-
"""区分：是我们的 docx 坏了，还是 Word/COM 环境坏了。
用一份**合法的源题 docx**（E:\\OCR\\00001.docx，只读打开）走同一条 COM 路径试一次。"""
import os
import sys

sys.path.insert(0, r"E:\OCR\app")
import paper     # noqa: E402

tmp_pdf = r"E:\OCR\app\out\acceptance_v1\paper\_word_probe.pdf"
for src in (r"E:\OCR\00001.docx", r"E:\OCR\00023.docx"):
    try:
        pages, secs = paper.docx_to_pdf(src, tmp_pdf)
        print("OK  :", os.path.basename(src), "pages=", pages, "s=", round(secs, 2))
    except Exception as e:                              # noqa: BLE001
        print("FAIL:", os.path.basename(src), str(e)[:200])
    if os.path.exists(tmp_pdf):
        os.remove(tmp_pdf)
