# -*- coding: utf-8 -*-
"""§16 省纸模式实测：卷面页数/墨迹率（compact vs normal，10/24/50 题）+ 答题卡页数/格几何/ArUco/QR。

只读产品代码、不改任何产品文件；产物写 app/out/acceptance_v1/layout/ 与 layout_compact.json。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

import cv2
import numpy as np

APP = r"E:\OCR\app"
sys.path.insert(0, APP)

import card as cardmod      # noqa: E402
import grade                # noqa: E402
import paper                # noqa: E402
import picker               # noqa: E402
import sqlite3               # noqa: E402

EV = os.path.join(APP, "out", "acceptance_v1")
WORK = os.path.join(EV, "layout")
OUT = {}
SEED = 20261031


def mix_for(n):
    """按 10/8/6 的比例缩放题量（10→4/4/2，24→10/8/6，50→25/15/10）。"""
    table = {10: {"选择": 5, "填空": 3, "解答": 2},
             24: {"选择": 10, "填空": 8, "解答": 6},
             50: {"选择": 25, "填空": 15, "解答": 10}}
    return table[n]


def _qr_ok(qr):
    """payload 形如 {"pid": "...", "p": N}（或含该二键的 JSON）。"""
    if not isinstance(qr, str) or not qr.startswith("{"):
        return False
    try:
        d = json.loads(qr)
    except ValueError:
        return False
    return "pid" in d and "p" in d


def measure_paper(conn, n_items, layout, tag):
    out = os.path.join(WORK, tag)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    params = {"title": "%s %d题" % (layout, n_items), "mix": mix_for(n_items),
              "scope": {}, "seed": SEED, "layout": layout,
              "requirements": {"need_answer": True, "exclude_empty": True}}
    t0 = time.time()
    manifest = picker.build(params, conn, pid="P20261031-9%02d" % (n_items % 100))
    built = paper.render_paper(manifest, out, name="paper", layout=layout)
    elapsed = time.time() - t0
    pngs = sorted(p for p in os.listdir(out)
                  if (p.startswith("page-") or p.startswith("paper")) and p.endswith(".png"))
    inks = [round(100 * paper.ink_ratio(os.path.join(out, p)), 2) for p in pngs]
    row = {"tag": tag, "layout": layout, "n_items": n_items,
           "paper_pages": len(pngs), "ink_pct": inks,
           "ink_min": min(inks) if inks else None, "ink_max": max(inks) if inks else None,
           "render_s": round(elapsed, 1),
           "manifest_n_items": len(manifest["items"]),
           "card_start_page": (manifest.get("card") or {}).get("start_page"),
           "paper_pages_meta": (manifest.get("paper") or {}).get("pages"),
           "docx": built.get("docx"), "pdf": built.get("pdf")}
    OUT.setdefault("papers", []).append(row)
    return manifest, row, out


def measure_card(manifest, out, tag):
    build = cardmod.build_card(manifest, out)
    card = manifest["card"]
    pages = card["pages"]
    per_page = []
    for i, page in enumerate(pages, 1):
        png = page.get("png") or os.path.join(out, "card_%d.png" % i)
        img = cv2.imread(png, cv2.IMREAD_GRAYSCALE)
        gray = img if img is not None else None
        found = []
        if gray is not None:
            det = grade.detect_markers(gray)
            found = [k for k in (0, 1, 2, 3) if k in det]
        qr = None
        if gray is not None:
            try:
                qr = cv2.QRCodeDetector().detectAndDecode(gray)[0]   # 独立用 cv2 解整页 PNG
            except Exception as exc:                       # noqa: BLE001
                qr = "ERR:%s" % exc.__class__.__name__
        per_page.append({"page": i, "aruco": found, "n_aruco": len(found),
                         "qr": qr, "h": (gray.shape[0] if gray is not None else None),
                         "w": (gray.shape[1] if gray is not None else None)})
    # 格几何：选题格（每题 4 个 OMR 小方格）+ 填答格（iter_answer_cells）
    omr_boxes, fill_boxes = [], []
    for i, page in enumerate(pages, 1):
        for item in page.get("items") or []:
            if item.get("kind") != "omr":
                continue
            for c in item.get("cells") or []:
                omr_boxes.append({"page": i, "x": c["x"], "y": c["y"],
                                  "w": c["w"], "h": c["h"], "opt": c.get("opt")})
    for c in cardmod.iter_answer_cells(card, pid=manifest.get("pid")):
        b = c.get("box") or {}
        fill_boxes.append({"page": c.get("page"), "x": b.get("x", -1), "y": b.get("y", -1),
                           "w": b.get("w", 0), "h": b.get("h", 0),
                           "idx": c.get("idx"), "blank": c.get("blank")})
    all_boxes = omr_boxes + fill_boxes
    H = per_page[0]["h"] or 3508
    W = per_page[0]["w"] or 2480
    oob = [b for b in all_boxes if b["x"] < 0 or b["y"] < 0
           or b["x"] + b["w"] > W or b["y"] + b["h"] > H]
    overlaps = []
    for i in range(len(all_boxes)):
        for j in range(i + 1, len(all_boxes)):
            a, b = all_boxes[i], all_boxes[j]
            if a["page"] != b["page"]:
                continue
            if (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                    and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]):
                overlaps.append((i, j))
    omr_sizes = sorted({(b["w"], b["h"]) for b in omr_boxes})
    fill_sizes = sorted({(b["w"], b["h"]) for b in fill_boxes})
    omr_cells, fill_cells = 0, 0
    for item in (manifest.get("items") or []):
        n_blank = len(item.get("answer_key") or []) or 1
        if item.get("qtype") == "选择":
            omr_cells += 4
        else:
            fill_cells += n_blank
    row = {"tag": tag, "card_pages": len(pages), "build_pages_meta": build.get("pages"),
           "per_page": per_page,
           "n_aruco_ok": sum(1 for p in per_page if p["n_aruco"] == 4),
           "n_qr_ok": sum(1 for p in per_page if _qr_ok(p["qr"])),
           "answer_cells": len(all_boxes),
           "omr_cells": len(omr_boxes), "fill_cells": len(fill_boxes),
           "omr_cell_sizes": omr_sizes, "fill_cell_sizes": fill_sizes,
           "omr_cells_expected": omr_cells, "fill_cells_expected": fill_cells,
           "oob": len(oob), "overlaps": len(overlaps),
           "omr_cell_px": cardmod.OMR_CELL,
           "start_page": card.get("start_page"),
           "page_labels": [p.get("label") for p in pages],
           "card_pdf_pages": build.get("pdf_pages") or build.get("pages")}
    OUT.setdefault("cards", []).append(row)
    return row


def main():
    os.makedirs(WORK, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(APP, "data", "app.db").replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        OUT["pools"] = picker.count_available(conn, {
            "mix": {k: 1 for k in ("选择", "填空", "解答")},
            "requirements": {"need_answer": True, "exclude_empty": True},
            "scope": {}})
        for n in (10, 24, 50):
            for layout in ("compact", "normal"):
                tag = "%s_%d" % (layout, n)
                manifest, row, out = measure_paper(conn, n, layout, tag)
                row.update(measure_card(manifest, out, tag))
                print("%-12s 卷面 %d 页 ink=%s | 卡 %d 页 | ArUco %d/%d | QR %d/%d"
                      % (tag, row["paper_pages"], row["ink_pct"], row["card_pages"],
                         row["n_aruco_ok"], row["card_pages"],
                         row["n_qr_ok"], row["card_pages"]))
    finally:
        conn.close()
    with open(os.path.join(EV, "layout_compact.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("\n证据:", os.path.join(EV, "layout_compact.json"))


if __name__ == "__main__":
    main()
