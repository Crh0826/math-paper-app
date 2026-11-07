#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""整卷 PDF 导入 · 提取器：扫描页切题裁图 + 答案页文字层解析。

用法（命令行可复用，不是一次性脚本）：

    python tools/extract_exam_pdf.py --pdf 试卷.pdf --subject physics --out out/_c20
    python tools/extract_exam_pdf.py --pdf 卷.pdf --subject physics --out out/x --emit-docx out/x/docx
    python tools/extract_exam_pdf.py --pdf 卷.pdf --subject physics --out out/x --layout opencv

产出（``--out`` 目录 / 同名 ZIP）：

    questions.json          每个题号一条：no/qtype/stem/answer/figure/figure_nos/page
    images/<no>.png         按题裁出来的图（一题多图：<no>_2.png …），长边 ≥800px
    report.json             本次提取的统计与告警（题数、图数、未配图的「如图」题、耗时）
    <no>.docx               （可选，--emit-docx）题干 + 图，供 paper 按 SRC_ROOT 直接排版

版面分析两种后端（``--layout``）：
* ``paddle``（默认）：PaddleOCR-VL 云端 job，一个页面一次 job，返回 block_bbox + block_content，
  文字块可直接读题号与题干；图块由「无文字 / 内容为空 / label=image」判定。**首选**。
* ``opencv``：纯本地启发式 —— 二值化 → 抹掉长横竖线 → 行投影切带 → 按「墨迹密度 / 连通域大小」
  区分文字行与图块。**不需要网络**，但没有 OCR：只能给出「文字区/图区」的 bbox，
  题号切分需要用 ``--numbers`` 手工给（或先跑 paddle 再回退）。

答案页：答案 PDF 页自带文字层（真卷实测 853/1000/793/663 字），按「1．60；2．人耳处；」解析，
不做 OCR。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

#: 裁图长边下限（验收要求 ≥800px）
MIN_FIG_LONG_SIDE = 800
#: 题号：「1．」「2.」「18、」「7）」
QNUM_RX = re.compile(r"^\s*(\d{1,2})\s*\\?\s*[．.、)）]\s*")
#: 答案页分隔：「1．60；」「2．人耳处；」
ANS_SPLIT_RX = re.compile(r"(?:(?<=[；;。\n])|^)\s*(\d{1,2})\s*[．.、]\s*")
#: 图块判定关键词（Paddle 的 block_label）
FIG_LABELS = ("image", "figure", "picture", "img", "chart", "diagram", "table")


def log(msg: str) -> None:
    print("[extract] %s" % msg, flush=True)


# ---------------------------------------------------------------- PDF → 页面

def page_kinds(pdf_path: str) -> list:
    """每页是「扫描题页」还是「答案文字页」：按文字层字数判断。"""
    import pymupdf as fitz

    doc = fitz.open(pdf_path)
    try:
        out = []
        for i, page in enumerate(doc, 1):
            text = page.get_text("text") or ""
            out.append({"page": i, "chars": len(text.strip()),
                        "kind": "answer" if len(text.strip()) >= 20 else "scan",
                        "width": page.rect.width, "height": page.rect.height})
        return out
    finally:
        doc.close()


def render_page(pdf_path: str, page_no: int, dpi: int = 200):
    """渲染一页 → (PIL.Image RGB, png bytes)。"""
    import pymupdf as fitz
    from PIL import Image

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=dpi)
        png = pix.tobytes("png")
    finally:
        doc.close()
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return img, png


# ---------------------------------------------------------------- 版面分析：Paddle

def _blocks_from_paddle(png: bytes) -> list:
    """PaddleOCR-VL 一次 job → [{bbox, content, label}]；失败返回 []。"""
    try:
        import ocr
    except Exception as exc:  # noqa: BLE001
        log("ocr 模块不可用：%s" % exc)
        return []
    try:
        rec = ocr.Recognizer(backend="paddle_vl")
    except Exception as exc:  # noqa: BLE001
        log("PaddleOCR-VL 初始化失败：%s" % exc)
        return []
    tmp = os.path.join(os.environ.get("TEMP", "."), "_extract_page_%d.png" % os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(png)
        raw_blocks, _mds = rec._paddle_job(tmp)
    except Exception as exc:  # noqa: BLE001
        log("PaddleOCR-VL job 失败：%s" % exc)
        return []
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = []
    for b in raw_blocks or []:
        bbox = b.get("bbox") or []
        if len(bbox) < 4:
            continue
        content = str(b.get("content") or "")
        label = str(b.get("label") or "").lower()
        out.append({"bbox": [float(v) for v in bbox[:4]], "content": content,
                    "label": label,
                    # 真卷实测：PaddleOCR-VL 对插图返回空 content 的块（带 bbox），图注另起一块
                    "figure": (not content.strip())})
    if rec.last_error:
        log("PaddleOCR-VL 告警：%s" % rec.last_error)
    return out


# ---------------------------------------------------------------- 版面分析：OpenCV

def layout_opencv(img) -> list:
    """纯本地启发式版面：返回 [{bbox, content:'', label:'text'|'figure'}]（按 y 排序）。

    步骤：灰度 → Otsu 二值 → 抹掉长横/竖线（表格框） → 行投影切带 →
    每带看「墨迹密度 + 连通域尺寸」判文字行 / 图块；相邻文字行合并成段。
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]
    _th, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # 抹掉长线（表格/边框）
    hor = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 25), 1)))
    ver = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 40))))
    clean = cv2.subtract(ink, cv2.add(hor, ver))
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    rows = (clean > 0).sum(axis=1)
    bands, start = [], None
    gap_min = max(4, int(h * 0.006))
    blank = 0
    for y, v in enumerate(rows):
        if v > 0:
            if start is None:
                start = y
            blank = 0
        else:
            if start is not None:
                blank += 1
                if blank >= gap_min:
                    bands.append((start, y - blank))
                    start = None
    if start is not None:
        bands.append((start, len(rows) - 1))

    blocks = []
    for (y0, y1) in bands:
        if y1 - y0 < 3:
            continue
        strip = clean[y0:y1 + 1, :]
        area = strip.shape[0] * strip.shape[1]
        density = float((strip > 0).sum()) / max(1, area)
        n, _lbl, stats, _c = cv2.connectedComponentsWithStats(strip, 8)
        heights = [stats[i, cv2.CC_STAT_HEIGHT] for i in range(1, n)]
        widths = [stats[i, cv2.CC_STAT_WIDTH] for i in range(1, n)]
        if not heights:
            continue
        xs = [stats[i, cv2.CC_STAT_LEFT] for i in range(1, n)]
        xe = [stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] for i in range(1, n)]
        x0, x1 = max(0, min(xs) - 2), min(w - 1, max(xe) + 2)
        # 文字行：字高小、连通域多、密度中等偏高；图块：连通域少而大、密度低
        block_h = y1 - y0 + 1
        tall = max(heights) > h * 0.05
        sparse = density < 0.03
        few = len(heights) <= 3
        label = "figure" if (tall and (sparse or few)) else "text"
        blocks.append({"bbox": [float(x0), float(y0), float(x1), float(y1 + 1)],
                       "content": "", "label": label, "figure": label == "figure"})
    # 二次判定：比「中位带高」高很多的带几乎都是插图（文字行高度很一致）
    if blocks:
        heights_all = sorted(b["bbox"][3] - b["bbox"][1] for b in blocks)
        median_h = heights_all[len(heights_all) // 2] or 1
        for b in blocks:
            bh = b["bbox"][3] - b["bbox"][1]
            if bh > median_h * 1.8:
                b["label"] = "figure"
                b["figure"] = True
    merged = []
    for b in blocks:
        if (merged and b["label"] == "text" and merged[-1]["label"] == "text"
                and b["bbox"][1] - merged[-1]["bbox"][3] <= h * 0.004):
            last = merged[-1]
            last["bbox"][2] = max(last["bbox"][2], b["bbox"][2])
            last["bbox"][3] = b["bbox"][3]
            continue
        merged.append(b)
    return merged


# ---------------------------------------------------------------- 切题 / 归属

#: 试卷须知/注意事项/考生须知：这些段落不是题目，必须剔除
INSTRUCTION_RX = re.compile(
    r"你拿到|本试卷|请务必|考试结束|考试时间|闭卷|请将答案|试题卷|答题卷|满分|注意事项|考生须知")
#: 选项标签（A．/B./C、/D）——选择题的四个选项可能被插图隔开
#: 只认「A．」「A.」；顿号不是选项分隔（「C、F 点等高」会误命中）
OPTION_RX = re.compile(r"(?<![A-Za-z0-9])([A-D])\s*\\?\s*[．.]\s*")


def is_instruction(text: str) -> bool:
    """是不是「试卷须知」类说明文字（不是题目）。"""
    t = str(text or "")
    if not t:
        return False
    hits = sum(1 for kw in ("你拿到", "本试卷", "请务必", "考试结束", "考试时间", "闭卷",
                           "请将答案", "试题卷", "答题卷", "满分", "注意事项", "考生须知")
               if kw in t)
    return hits >= 1 and ("试卷" in t or "答题" in t or "考试" in t or "注意事项" in t)


def split_options(text: str) -> list:
    """从题干里切出 A/B/C/D 选项（允许选项之间夹着图注/甲乙丙等噪声）。

    返回 ["A．…", "B．…", …]；少于 2 个返回 []。
    """
    s = str(text or "")
    marks = list(OPTION_RX.finditer(s))
    if len(marks) < 2:
        return []
    picked, seen = [], set()
    for i, m in enumerate(marks):
        label = m.group(1)
        if label in seen:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
        body = s[m.end():end]
        body = re.sub(r"第\s*\d+\s*题\s*图|图\s*[甲乙丙丁]|\s+", " ", body).strip(" ；;，,。")
        body = re.sub(r"\s*[甲乙丙丁]\s*$", "", body).strip(" ；;，,。")   # 选项后面跟着小图标注 甲乙丙
        if len(body) < 1:
            continue
        seen.add(label)
        picked.append("%s．%s" % (label, body))
    labels = [p[0] for p in picked]
    if labels != list("ABCD"[:len(labels)]) or len(picked) < 2:
        return []
    return picked


#: 原卷页眉页脚：「物理 第 2 页（共 6 页）」「（共 6 页）」「第 2 页」
PAGE_RX = re.compile(r"(?:物\s*理\s*)?第\s*\d+\s*页\s*[（(]\s*共\s*\d+\s*页\s*[）)]"
                     r"|[（(]\s*共\s*\d+\s*页\s*[）)]"
                     r"|[（(]?\s*共\s*\d+\s*页\s*[）)]?"
                     r"|第\s*\d+\s*页")
#: 图注残留：「第14题图」「第 5 题图」
FIG_CAP_RX = re.compile(r"第\s*\d+\s*题\s*图")
#: 孤立的小图标注：前后是空白/标点时才算（「甲图中的手影」里的甲不算）
SUBFIG_RX = re.compile(r"(?<=[\s。，、；：（）()])[甲乙丙丁](?=[\s。，、；：（）()]|$)")


def clean_stem(text: str) -> str:
    """题干清洗：去掉原卷页眉页脚、图注残留、孤立的小图标注，压掉多余空白。"""
    t = str(text or "")
    t = PAGE_RX.sub(" ", t)
    t = FIG_CAP_RX.sub(" ", t)
    t = SUBFIG_RX.sub(" ", t)
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return t.strip(" ；;，,。")


#: 图注：「第3题图」「图甲」「图 2」
CAPTION_RX = re.compile(r"^\s*(?:第\s*\d+\s*题\s*)?图\s*[甲乙丙丁]?\s*\d*\s*$|"
                        r"^\s*第\s*\d+\s*题\s*图\s*$")
#: 图注里点名的题号（「第13题图」→ 13）
CAPTION_QNO_RX = re.compile(r"第\s*(\d{1,2})\s*题\s*图")
#: 大题头：「一、填空题」「二、选择题」
SECTION_RX = re.compile(r"^\s*[一二三四五六七]、")


def _overlap_x(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0])


def merge_captions(blocks: list, max_gap: float = 200.0) -> list:
    """把「第N题图」这类图注并进它上面的图块（bbox 向下扩到图注底部），并从文字流里摘掉。"""
    figures = [b for b in blocks if b.get("figure")]
    keep = []
    for b in blocks:
        if b.get("figure") or not CAPTION_RX.match(b.get("content") or ""):
            keep.append(b)
            continue
        cap = b["bbox"]
        best, best_gap = None, max_gap
        for f in figures:
            fb = f["bbox"]
            below = cap[1] - fb[3]                    # 图注在图下面
            above = fb[1] - cap[3]                    # 图注在图上面
            gap = below if below >= -8 else above
            if abs(gap) <= best_gap and _overlap_x(fb, cap):
                best, best_gap = f, abs(gap)
        if best is None:
            keep.append(b)                       # 没主的图注当普通块留着
            continue
        best["bbox"][2] = max(best["bbox"][2], cap[2])
        best["bbox"][3] = max(best["bbox"][3], cap[3])
        best.setdefault("caption", b["content"].strip())
        m = CAPTION_QNO_RX.search(b["content"] or "")
        if m:
            best["caption_qno"] = int(m.group(1))     # 「第N题图」直接点名归属
    return keep


def segment_questions(blocks: list, page_tag: int = 0, skipped: list = None) -> list:
    """文字块 → 题目区间：[{qno, page_y0, text, figures:[bbox…]}]。

    题号块（``1．`` 开头）开一道新题；其后的文字块都算这题的题干，直到下一个题号块。
    """
    ordered = sorted([b for b in blocks if not b.get("figure")
                      and b.get("label") not in FIG_LABELS
                      and (b.get("content") or "").strip()],
                     key=lambda b: (b["bbox"][1], b["bbox"][0]))
    # 卷首「注意事项：1.…2.…」里的数字不是题号：只有「大题头出现在第一个题号块之前」才裁掉前导
    # 卷首注意事项：按关键字直接摘掉（比「靠大题头裁」稳，页中没有大题头时也不会误伤）
    INSTR_RX = re.compile(r"你拿到|本试卷|请务必|考试结束|注意事项|答题卷|试题卷|满分")
    while ordered and INSTR_RX.search(ordered[0].get("content") or "") \
            and not SECTION_RX.match(ordered[0].get("content") or ""):
        head = ordered.pop(0)
        if skipped is not None:                      # 卷首须知也要记账，别静默丢
            skipped.append({"page": page_tag,
                            "text": (head.get("content") or "")[:120],
                            "reason": "试卷须知/注意事项（卷首裁剪）"})
    first_section = next((i for i, b in enumerate(ordered)
                          if SECTION_RX.match(b.get("content") or "")), None)
    first_q = next((i for i, b in enumerate(ordered)
                    if QNUM_RX.match(b.get("content") or "")), None)
    if first_section is not None and (first_q is None or first_section < first_q):
        ordered = ordered[first_section:]
    questions, current = [], None
    for b in ordered:
        text = (b.get("content") or "").strip()
        if is_instruction(text):
            # 试卷须知/注意事项：不是题目，记到 report.skipped_sections 里备查
            if skipped is not None:
                skipped.append({"page": page_tag, "text": text[:120],
                                "reason": "试卷须知/注意事项（非题目）"})
            continue
        m = QNUM_RX.match(text)
        if m and int(m.group(1)) <= 30:
            qno = int(m.group(1))
            prev_same_page = [q for q in questions if q.get("_page") == page_tag]
            if prev_same_page and qno <= prev_same_page[-1]["qno"]:
                qno = prev_same_page[-1]["qno"] + 1   # OCR 把 6 读成 5：同页内单调纠正
            current = {"qno": qno, "y0": b["bbox"][1], "_page": page_tag,
                       "text": clean_stem(QNUM_RX.sub("", text, count=1)),
                       "figures": [], "figure_captions": []}
            questions.append(current)
        elif current is not None:
            piece = clean_stem(text)
            if piece:
                current["text"] = (current["text"] + " " + piece).strip()
    return questions


def assign_figures(questions: list, blocks: list, all_questions: list = None) -> list:
    """图块按 y 归到「其上方最近的题」；一题多图按出现顺序。"""
    figs = sorted([b for b in blocks
                   if b.get("figure") or b.get("label") in FIG_LABELS],
                  key=lambda b: (b["bbox"][1], b["bbox"][0]))
    if not questions:
        return figs
    by_qno = {q["qno"]: q for q in (all_questions or questions)}   # 全局：跨页也能按图注点名
    owner_of = {}
    # 第一轮：图注点名「第N题图」→ 直接归属（能跨页）
    for f in figs:
        qno = f.get("caption_qno")
        if qno is not None and qno in by_qno:
            owner_of[id(f)] = by_qno[qno]
    # 第二轮：没点名的图，跟同页「最近的已归属图」或最近的上方题
    for f in figs:
        if id(f) in owner_of:
            continue
        fy = f["bbox"][1]
        owner = None
        for i, q in enumerate(questions):
            if q["y0"] <= fy:
                nxt = questions[i + 1]["y0"] if i + 1 < len(questions) else 10 ** 9
                if fy < nxt:
                    owner = q
            else:
                break
        owner_of[id(f)] = owner          # 落在两题之间/页脚 → 谁都不归，交给图注点名
    for f in figs:
        q = owner_of.get(id(f))
        if q is not None:
            q.setdefault("figures", []).append(f["bbox"])
            cap = (f.get("caption") or "").strip()
            if not cap and f.get("caption_qno"):
                cap = "第%d题图" % f["caption_qno"]
            q.setdefault("figure_captions", []).append(cap)
    # 一题多图按 y 排序
    for q in questions:
        pairs = list(zip(q.get("figures") or [], q.get("figure_captions") or []))
        pairs.sort(key=lambda pc: (pc[0][1], pc[0][0]))
        q["figures"] = [pc[0] for pc in pairs]
        q["figure_captions"] = [pc[1] for pc in pairs]
    return figs


def crop_figure(img, bbox, pad: int = 6, min_side: int = MIN_FIG_LONG_SIDE):
    """按 bbox 裁图（外扩 pad），保证长边 ≥ min_side。返回 PIL.Image。"""
    from PIL import Image

    w, h = img.size
    x0 = int(max(0, min(bbox[0], bbox[2]) - pad))
    y0 = int(max(0, min(bbox[1], bbox[3]) - pad))
    x1 = int(min(w, max(bbox[0], bbox[2]) + pad))
    y1 = int(min(h, max(bbox[1], bbox[3]) + pad))
    # 原始框太小 → 不裁（避免把标点/噪点当插图放大 800px）
    if abs(float(bbox[2]) - float(bbox[0])) < 6 or abs(float(bbox[3]) - float(bbox[1])) < 6:
        return None
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = img.crop((x0, y0, x1, y1))
    long_side = max(crop.size)
    if long_side < min_side:
        # 用 ceil 保证长边「至少」800（LANCZOS 会四舍五入出 799 这种差 1px 的结果）
        import math

        scale = float(min_side) / max(1, long_side)
        new_w = max(1, int(math.ceil(crop.width * scale)))
        new_h = max(1, int(math.ceil(crop.height * scale)))
        if max(new_w, new_h) < min_side:
            if new_w >= new_h:
                new_w = min_side
            else:
                new_h = min_side
        crop = crop.resize((new_w, new_h), Image.LANCZOS)
    return crop


# ---------------------------------------------------------------- 答案解析

def parse_answers(text: str) -> dict:
    """答案页文字 → {题号: 答案}。支持「1．60；2．人耳处；」与多行续写。"""
    if not text:
        return {}
    flat = re.sub(r"[ \t\u3000]+", " ", text.replace("\n", "\n"))
    out, marks = {}, list(ANS_SPLIT_RX.finditer(flat))
    for i, m in enumerate(marks):
        qno = int(m.group(1))
        if not (1 <= qno <= 99):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(flat)
        body = flat[m.end():end].strip()
        body = re.sub(r"\s*\n\s*", " ", body).strip(" ；;")
        body = re.sub(r"^选\s*[:：]\s*", "", body)
        # 单个答案：到第一个「；/。」为止；多小问（含「（1）」）保留整段
        if "（" not in body and "(" not in body:
            body = re.split(r"[；;。]|根据|\n", body)[0]
        else:
            body = re.split(r"根据|\n", body)[0]
        body = body.strip(" ；;")
        if qno in out and out[qno]:
            continue
        out[qno] = body
    return out


#: 选择题答案表：「题号 11 12 13 … 选项 D B D …」
CHOICE_TABLE_RX = re.compile(r"题\s*号\s*((?:\d{1,2}\s+){2,})\s*选\s*项\s*((?:[A-D]\s*){2,})")


def parse_choice_table(text: str) -> dict:
    """答案页底部的「题号/选项」对照表 → {11: 'D', 12: 'B', …}。"""
    flat = re.sub(r"\s+", " ", str(text or ""))
    out = {}
    for m in CHOICE_TABLE_RX.finditer(flat):
        nums = [int(x) for x in m.group(1).split()]
        opts = [c for c in m.group(2).split() if c]
        if len(nums) != len(opts):
            continue
        for n, o in zip(nums, opts):
            out[n] = o
    return out


def answers_from_pdf(pdf_path: str, pages=None) -> dict:
    """取答案页（默认为文字层 ≥20 字的所有页）文字 → {题号: 答案}。"""
    import pymupdf as fitz

    kinds = page_kinds(pdf_path)
    answer_pages = [k["page"] for k in kinds if k["kind"] == "answer"]
    if pages:
        answer_pages = [p for p in answer_pages if p in set(pages)]
    doc = fitz.open(pdf_path)
    try:
        text = "\n".join((doc[p - 1].get_text("text") or "") for p in answer_pages)
    finally:
        doc.close()
    parsed = parse_answers(text)
    table = parse_choice_table(text)
    for qno, opt in table.items():          # 选择题以对照表为准（比正文里的「选：X」更全）
        parsed[qno] = opt
    return parsed


# ---------------------------------------------------------------- 主流程

def extract(pdf_path: str, subject: str = "physics", *, out_dir: str = "",
            dpi: int = 200, layout: str = "paddle", no_base: int = 1,
            no_width: int = 5, question_pages=None, answer_pages=None,
            emit_docx: str = "", zip_path: str = "") -> dict:
    """跑一遍：返回报告 dict，并在 out_dir 落 questions.json / images/ / report.json。"""
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    started = time.time()

    kinds = page_kinds(pdf_path)
    if question_pages is None:
        question_pages = [k["page"] for k in kinds if k["kind"] == "scan"]
    if answer_pages is None:
        answer_pages = [k["page"] for k in kinds if k["kind"] == "answer"]

    answers = answers_from_pdf(pdf_path, answer_pages)
    log("答案页 %s → 解析出 %d 道题的答案" % (answer_pages, len(answers)))

    questions, fig_rows, per_page, skipped_sections = [], [], [], []
    page_questions = []
    for page_no in question_pages:
        img, png = render_page(pdf_path, page_no, dpi=dpi)
        if layout == "opencv":
            blocks = layout_opencv(img)
        else:
            blocks = _blocks_from_paddle(png)
            if not blocks:
                log("第 %d 页 Paddle 没返回块，退回 OpenCV 启发式" % page_no)
                blocks = layout_opencv(img)
        blocks = merge_captions(blocks)
        qs = segment_questions(blocks, page_tag=page_no, skipped=skipped_sections)
        for q in qs:
            q["page"] = page_no
            q["_img"] = img
            q["_blocks"] = blocks
        page_questions.append(qs)
        n_fig = sum(len(q["figures"]) for q in qs)
        per_page.append({"page": page_no, "blocks": len(blocks),
                         "figures": len([b for b in blocks if b.get("figure")]),
                         "text_blocks": len([b for b in blocks if not b.get("figure")]),
                         "questions": [q["qno"] for q in qs], "figs_assigned": n_fig})
        log("第 %d 页：块 %d（图 %d）→ 题 %s，归属图 %d"
            % (page_no, len(blocks), per_page[-1]["figures"],
               [q["qno"] for q in qs], n_fig))
        questions.extend(qs)

    # 图归属：全局一次（图注「第N题图」可跨页点名；y 归属仍限同页）
    for qs in page_questions:
        if not qs:
            continue
        blocks = qs[0].get("_blocks") or []
        assign_figures(qs, blocks, all_questions=[q for page in page_questions for q in page])
        n_fig = sum(len(q.get("figures") or []) for q in qs)
        for row in per_page:
            if row["page"] == qs[0].get("page"):
                row["figs_assigned"] = n_fig

    # 跨页去重：同一题号只留一条（文本取长的，图取并集）
    merged, order = {}, []
    for q in questions:
        k = q["qno"]
        if k not in merged:
            merged[k] = dict(q)
            merged[k]["figures"] = list(q.get("figures") or [])
            order.append(k)
            continue
        cur = merged[k]
        if len(q.get("text") or "") > len(cur.get("text") or ""):
            cur["text"] = q["text"]
            cur["_img"] = q["_img"]
            cur["page"] = q["page"]
        for bbox in q.get("figures") or []:
            if bbox not in cur["figures"]:
                cur["figures"].append(bbox)
    questions = [merged[k] for k in sorted(merged)]

    # 裁图 + 编号（exam 题号 → 库题号）
    out_rows = []
    for q in questions:
        no = "%0*d" % (no_width, no_base + q["qno"] - 1)
        figs = []
        for i, bbox in enumerate(q.get("figures") or [], 1):
            crop = crop_figure(q["_img"], bbox)
            if crop is None:
                continue
            name = "%s.png" % no if i == 1 else "%s_%d.png" % (no, i)
            crop.save(os.path.join(img_dir, name), "PNG")
            figs.append(name)
            caps = q.get("figure_captions") or []
            fig_rows.append({"no": no, "qno": q["qno"], "idx": i, "name": name,
                             "caption": caps[i] if i < len(caps) else "",
                             "bbox": [round(v, 1) for v in bbox],
                             "size": list(crop.size), "page": q["page"]})
        stem = clean_stem(q.get("text") or "")
        options = split_options(stem)     # 不管题型都试着拆：选择题的选项常被并进题干
        if options:                       # 选项切出来以后，题干只留问句部分
            head = clean_stem(OPTION_RX.split(stem, maxsplit=1)[0])
            if len(head) >= 6:
                # 题干末尾补一个空格：卷面拼接时「（）」后紧跟「A．」会让下游认不出选项标签
                # （paper 要求选项标签前是空白/左括号），补空格后 A/B/C/D 才能各占一行
                stem = head + " "
        qtype = guess_qtype(stem)
        out_rows.append({
            "no": no, "qno": q["qno"], "qtype": qtype,
            # 选项前面留一个空格：paper 认选项标签要求标签前是空白/左括号，
            # 而题干常以「（）」结尾（`）` 不在允许集合里），不补空格就拆不开、卷面挤成一行
            "stem": stem, "options": [" " + o for o in options],
            # 选择题但选项切不全 → 明确标出来（不静默丢题，导入端会拒但不至于"消失"）
            "options_incomplete": bool(qtype == "选择" and len(options) < 2),
            "answer": answers.get(q["qno"], ""),
            "figure": figs, "subject": subject, "page": q["page"],
            "figure_meta": [{"name": n, "caption": cap}
                            for n, cap in zip(figs, (q.get("figure_captions")
                                                     or [""] * len(figs)))],
        })

    no_figure = [r["no"] for r in out_rows if not r["figure"] and wants_figure(r["stem"])]
    no_answer = [r["no"] for r in out_rows if not r["answer"]]
    report = {
        "pdf": os.path.abspath(pdf_path),
        "subject": subject,
        "layout": layout,
        "dpi": dpi,
        "pages": kinds,
        "question_pages": list(question_pages),
        "answer_pages": list(answer_pages),
        "per_page": per_page,
        "questions": len(out_rows),
        "with_figure": len([r for r in out_rows if r["figure"]]),
        "figures": len(fig_rows),
        "missing_figure": no_figure,
        "missing_answer": no_answer,
        "skipped_sections": skipped_sections,
        "options_incomplete": [r["no"] for r in out_rows if r.get("options_incomplete")],
        "with_options": len([r for r in out_rows if r.get("options")]),
        "answers_parsed": len(answers),
        "elapsed_s": round(time.time() - started, 2),
        "figure_table": fig_rows,
    }
    with open(os.path.join(out_dir, "questions.json"), "w", encoding="utf-8") as fh:
        json.dump({"subject": subject, "source_pdf": os.path.abspath(pdf_path),
                   "items": out_rows}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    if emit_docx:
        report["docx"] = emit_question_docx(out_rows, img_dir, emit_docx)
        with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)

    zip_path = zip_path or (out_dir.rstrip("\\/") + ".zip")
    report["zip"] = make_zip(out_dir, zip_path, out_rows)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def wants_figure(stem: str) -> bool:
    """题干自称有图（与 picker.fig_missing 同一套说法，但不改它的规则）。"""
    return bool(re.search(r"如图|见图|图中|下图|上图|图甲|图乙|图①|如图所示的", str(stem or "")))


def guess_qtype(stem: str) -> str:
    """按题干形态猜题型（填空 / 选择 / 解答）—— 导入后仍以答案页为准。"""
    s = str(stem or "")
    if re.search(r"(?:^|[\s。；;])([A-D])\s*[．.]", s) and s.count("．") >= 2:
        return "选择"
    if re.search(r"（\s*）|\(\s*\)", s) or "___" in s or "____" in s:
        return "填空"
    return "解答"


def emit_question_docx(rows: list, img_dir: str, out_dir: str) -> dict:
    """把每道题写成 <no>.docx（题干 + 图），供 paper 通过 SRC_ROOT 直接排版。"""
    from docx import Document
    from docx.shared import Inches

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for row in rows:
        doc = Document()
        doc.add_paragraph("%s．%s" % (row["qno"], row["stem"]))
        for name in row.get("figure") or []:
            path = os.path.join(img_dir, name)
            if not os.path.isfile(path):
                continue
            # 按「宽 4.2in」和「高 3.6in」两条约束取更小的那个，避免长条图把题顶到下一页
            try:
                from PIL import Image

                with Image.open(path) as im:
                    iw, ih = im.size
            except Exception:            # noqa: BLE001
                iw, ih = 1, 1
            max_w, max_h = 4.2, 3.6
            w_in = min(max_w, max_h * (float(iw) / max(1, ih)))
            doc.add_picture(path, width=Inches(max(1.2, w_in)))
        target = os.path.join(out_dir, "%s.docx" % row["no"])
        doc.save(target)
        written.append(os.path.basename(target))
    log("已生成 %d 份题面 docx → %s" % (len(written), out_dir))
    return {"dir": out_dir, "count": len(written), "files": written}


def make_zip(out_dir: str, zip_path: str, rows: list) -> str:
    """questions.json + images/*.png → ZIP（导入端直接吃）。"""
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(out_dir, "questions.json"), "questions.json")
        img_dir = os.path.join(out_dir, "images")
        for row in rows:
            for name in row.get("figure") or []:
                path = os.path.join(img_dir, name)
                if os.path.isfile(path):
                    zf.write(path, "images/" + name)
    log("ZIP：%s（%.1f KB）" % (zip_path, os.path.getsize(zip_path) / 1024))
    return zip_path


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="整卷 PDF → 题目 + 图 + 答案（ZIP）")
    ap.add_argument("--pdf", required=True, help="试卷 PDF（题目页可以是扫描图，答案页带文字层）")
    ap.add_argument("--subject", default="physics", help="学科 subject_pk（默认 physics）")
    ap.add_argument("--out", default="", help="输出目录（默认 out/_exam_<pdf名>）")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--layout", choices=("paddle", "opencv"), default="paddle",
                    help="版面分析后端：paddle（PaddleOCR-VL，能读题号）/ opencv（本地启发式）")
    ap.add_argument("--no-base", type=int, default=1, help="第 1 题的库题号（默认 1 → 00001）")
    ap.add_argument("--no-width", type=int, default=5, help="题号补零宽度（默认 5）")
    ap.add_argument("--question-pages", default="", help="题目页，如 1-6（默认按文字层自动判断）")
    ap.add_argument("--answer-pages", default="", help="答案页，如 7-10")
    ap.add_argument("--emit-docx", default="", help="额外生成 <no>.docx 到该目录（供 paper 排版）")
    ap.add_argument("--zip", default="", help="ZIP 路径（默认 <out>.zip）")
    args = ap.parse_args(argv)

    def pages(text):
        if not text:
            return None
        out = []
        for part in str(text).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                out.extend(range(int(a), int(b) + 1))
            else:
                out.append(int(part))
        return out

    out = args.out or os.path.join(APP_DIR, "out", "_exam_" +
                                   re.sub(r"\W+", "_", os.path.splitext(os.path.basename(args.pdf))[0]))
    report = extract(args.pdf, args.subject, out_dir=out, dpi=args.dpi, layout=args.layout,
                     no_base=args.no_base, no_width=args.no_width,
                     question_pages=pages(args.question_pages),
                     answer_pages=pages(args.answer_pages),
                     emit_docx=args.emit_docx, zip_path=args.zip)
    print(json.dumps({k: report[k] for k in
                      ("questions", "with_figure", "figures", "missing_figure",
                       "missing_answer", "answers_parsed", "layout", "elapsed_s", "zip")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
