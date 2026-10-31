# -*- coding: utf-8 -*-
"""M1 · Word 渲染端到端：token → docx → PDF → PNG，人眼可核对公式是否还原。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win32com.client as win32
import pythoncom
import pymupdf

from docx_rich import extract_tokens
from compose import build_paper

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "render")
os.makedirs(OUT, exist_ok=True)
ROOT = r"E:\OCR"


def docx_to_pdf(docx_path: str, pdf_path: str) -> tuple:
    t0 = time.time()
    pythoncom.CoInitialize()
    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        doc = word.Documents.Open(docx_path, ReadOnly=True, AddToRecentFiles=False)
        doc.SaveAs(pdf_path, FileFormat=17)
        pages = doc.ComputeStatistics(2)
        doc.Close(False)
    finally:
        word.Quit()
        pythoncom.CoUninitialize()
    return pages, time.time() - t0


def main():
    items = []
    for i, (no, head) in enumerate([("00001", "1.（一元一次方程 · OMML 公式）"),
                                   ("01000", "2.（有理数运算 · MathType/OLE 公式）"),
                                   ("00002", "3.（一元一次方程 · 选项制表符）")], 1):
        items.append({"heading": head, "tokens": extract_tokens(os.path.join(ROOT, f"{no}.docx")),
                      "space_paras": 1})
    docx_path = os.path.join(OUT, "paper.docx")
    pdf_path = os.path.join(OUT, "paper.pdf")
    build_paper(items, docx_path, title="七年级数学 · 卷面渲染验证")
    pages, dt = docx_to_pdf(docx_path, pdf_path)
    print(f"docx->pdf ok: {pages} page(s), {dt:.1f}s, {os.path.getsize(pdf_path)} bytes")

    d = pymupdf.open(pdf_path)
    for i, page in enumerate(d):
        pix = page.get_pixmap(dpi=150)
        p = os.path.join(OUT, f"paper_p{i+1}.png")
        pix.save(p)
        ink = (pix.samples.count(0)) if False else None
        print("page", i + 1, pix.width, "x", pix.height, "->", p)
    d.close()


if __name__ == "__main__":
    main()
