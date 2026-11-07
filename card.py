# -*- coding: utf-8 -*-
"""card.py · 机读答题卡生成（T4）

设计要点（判卷侧 grade.py 直接依赖这些坐标，改动必须同步契约）：

* 坐标系：300dpi 规范像素，A4 = 2480×3508，原点左上（CONTRACT §3）。
* 四角 ArUco：DICT_4X4_50，**第 p 页 id = 4p..4p+3**（左上/右上/右下/左下），边长 14mm(165px)，
  距纸边 12mm(142px)。标定侧按 4 个角点做透视复原到 2480×3508。
* 右上卷号 QR：**只有第 1 页**，24mm(283px)，纠错级 H，payload ``{"pid","p":1,"v":4}``；
  v4 起页序不写在 QR 里，而是用 ArUco id 分组（第 p 页 = 4p..4p+3）。
  frame 内自带 4 模块静区；QR 的 p 用「卡内页序 1..N」（稳定、好查表），
  卡面印刷的页码另放在 page["label"]（接在卷面最后一页之后）。
* 每题左侧 4×4 ID 格：16bit = 12bit 题序 + 4bit 校验，cell 24px，
  bit=1 涂黑，解码见 decode_idx_bits()。
* 分页：单页放不下自动翻页；每页都有独立 ArUco 四角 + QR + 页码 + 题型标注。

对外 API：
    build_card(manifest, out_dir)
        -> {"pdf", "png": [...], "card": {...}, "manifest": manifest, "health": {...}}

依赖说明（别误会成循环依赖）：出卡后**复用 paper.check_pdf_a4 做页面尺寸校验**
（本模块只加校验、不改版式）。依赖方向是 card → paper 单向；paper.py 不 import card.py，
所以在 build_card 里用函数内 `import paper` 就够，模块顶层不引。
    encode_idx_bits(idx) -> list[int]  # 16 个 0/1
    decode_idx_bits(bits) -> int | None
"""
from __future__ import annotations

import json
import logging
import os
import sys

import cv2
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("card")

# ---------------------------------------------------------------- 页面常量
PAGE_W = 2480
PAGE_H = 3508
DPI = 300
MM = DPI / 25.4  # 11.811 px/mm


def mm(v: float) -> int:
    """毫米 → 300dpi 像素（四舍五入到整像素）。"""
    return int(round(v * MM))


#: 四角 ArUco
ARUCO_DICT_ID = cv2.aruco.DICT_4X4_50
DICT_4X4_50 = ARUCO_DICT_ID  # 便于测试/判卷侧按名字断言
ARUCO_SIZE = mm(14)      # 165（v4：从 18mm 缩到 14mm，纸面标记面积减半）
ARUCO_SIZE_V3 = mm(18)   # 212（旧卡尺寸；判卷按 manifest 里的 marks.aruco[].size 走，不靠常量）
ARUCO_MARGIN = mm(12)    # 142
ARUCO_POS = {
    0: (ARUCO_MARGIN, ARUCO_MARGIN),                                          # 左上
    1: (PAGE_W - ARUCO_MARGIN - ARUCO_SIZE, ARUCO_MARGIN),                     # 右上
    2: (PAGE_W - ARUCO_MARGIN - ARUCO_SIZE, PAGE_H - ARUCO_MARGIN - ARUCO_SIZE),  # 右下
    3: (ARUCO_MARGIN, PAGE_H - ARUCO_MARGIN - ARUCO_SIZE),                     # 左下
}

#: 卷号 QR（右上，位于右上 ArUco 正下方）
QR_SIZE = mm(24)         # 283（v4：从 30mm 缩到 24mm，只在第 1 页）
QR_SIZE_V3 = mm(30)      # 354（旧卡尺寸，判卷按 manifest 里的 qr.size 走）
QR_POS = (PAGE_W - ARUCO_MARGIN - QR_SIZE, ARUCO_MARGIN + ARUCO_SIZE + mm(4))
#: v4 页序编码：第 p 页（0 基）四角 id = 4p + 角号；一页 4 个，DICT_4X4_50 够 12 页
ARUCO_PER_PAGE = 4
ARUCO_MAX_PAGES = 50 // ARUCO_PER_PAGE      # 12
QR_ECC = "H"

#: 正文区（所有作答单元都必须落在里面）
BODY_RECT = (100, 800, 2240, 2320)   # x, y, w, h  → 100..2340 / 800..3120

#: 每题标识格（t46）：5×5=25 位，前 20 位 = 题号，后 5 位 = CRC-5 校验
CARD_VERSION = 4                  # 4 = 只在第 1 页印 QR；页序改用 ArUco id 分组
CARD_VERSION_V3 = 3               # 3 = 每页 1 QR + 固定 0..3 四角（只兼容不生成）
LEGACY_CARD_VERSION = 1           # 旧卡：4×4 标识（编码卷内题序），只兼容不生成
ID_GRID = 5
LEGACY_ID_GRID = 4                # 旧卡（manifest.card.version=1）的 4×4 标识
ID_CELL = 24                      # 约 2mm@300dpi，与旧版视觉一致
ID_SIZE = ID_GRID * ID_CELL       # 120
ID_TOP_DY = 6                     # id_box.y = 题块 top + 6
NO_BITS = 20                      # 题号位宽：22,898 老题（5 位）+ 15 万题（6 位）都编得下
CHK_BITS = ID_GRID * ID_GRID - NO_BITS   # 5 位校验
MAX_NO = (1 << NO_BITS) - 1       # 1,048,575：t11 起 6 位题号（100000..1048575）合法
NO_MIN_WIDTH = 5                  # 题号最小位数（"%05d"）：6 位/7 位自然变长，旧号仍是 5 位
ID_COL_X = 155

#: 省纸两列版式（t38）：正文左右各一列，每列内部左右各留 8mm
COL_W = BODY_RECT[2] // 2                    # 1120
COL_PAD = mm(8)                              # 94
COL_ID_X = ID_COL_X - BODY_RECT[0]           # 55 → 左列 ID 格仍在 155
COL_CONTENT_X = 275 - BODY_RECT[0]           # 175 → 左列内容仍在 275
COL_CONTENT_W = COL_W - COL_PAD - COL_CONTENT_X   # 851

#: 整宽内容（解答题用）
CONTENT_X = BODY_RECT[0] + COL_CONTENT_X          # 275
CONTENT_RIGHT = BODY_RECT[0] + BODY_RECT[2] - COL_PAD  # 2246
CONTENT_W = CONTENT_RIGHT - CONTENT_X             # 1971


def col_content_x(col: int) -> int:
    """第 col（0/1）列的内容左缘。"""
    return BODY_RECT[0] + col * COL_W + COL_CONTENT_X


def col_id_x(col: int) -> int:
    return BODY_RECT[0] + col * COL_W + COL_ID_X


#: 标识块可能出现的两列左缘（判卷扫块用）
ID_ORIGIN_X = None      # 由下方常量就绪后赋值

#: 题型
KIND_OMR = "omr"
KIND_FILL = "fill"
KIND_SOLUTION = "solution"

KIND_LABEL = {
    KIND_OMR: "选择题（涂黑方框）",
    KIND_FILL: "填空题（写在框内）",
    KIND_SOLUTION: "解答题（过程写在过程区，最终答案写在答案框）",
}

#: 版面尺寸（毫米级）
OMR_CELL = mm(6)             # 71
OMR_PITCH = 170              # 相邻选择格中心间距
OMR_START_X = 420            # 旧整宽版式的起点（仅做兼容；两列版式按列计算）
OMR_OFFSET = 145             # 选择格相对列内容左缘的偏移（275+145=420，左列与旧版一致）
FILL_W = mm(40)              # 472
FILL_H = mm(10)              # 118
FILL_GAP = mm(3.4)           # 40
BOX_GAP = 40
PROCESS_H = 360              # 过程区高度（t38：520→360，只判最终答案）
ANSWER_H = mm(10)            # 118
ITEM_GAP = 16
HEADER_H = 44                # 题型标注条高
HEADER_GAP = 10
QUESTION_NO_FONT = 26

ID_ORIGIN_X = (col_id_x(0), col_id_x(1))

#: 学生信息区（v3）：页眉下、正文上，不进正文流
STUDENT_TOP = 380                # 页眉下方、让开左上 ArUco（142..354）
STUDENT_H = 410                  # 底部 790 < 正文 800，绝不压题
STUDENT_DIGITS = 8
STUDENT_GAP = 4                  # 行间距
STUDENT_COL_GAP = 18             # 位间距
STUDENT_MIN_CELL = 30
STUDENT_THRESHOLD = 0.35         # 与 OMR 同一套墨迹阈值
STUDENT_RIGHT = 1960             # 让开右上 QR（1984 起）

CARD_PDF_NAME = "card.pdf"

DEFAULT_OPTIONS = ["A", "B", "C", "D"]

# ---------------------------------------------------------------- 字体
_FONT_REG_CANDIDATES = [
    os.environ.get("CARD_FONT_REG", ""),
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
]
_FONT_BOLD_CANDIDATES = [
    os.environ.get("CARD_FONT_BOLD", ""),
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
]
_font_cache: dict = {}


def _pick_font(candidates) -> str:
    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise RuntimeError(
        "找不到中文字体（msyh/simhei/simsun 都没有）；请装微软雅黑或设 CARD_FONT_REG/CARD_FONT_BOLD"
    )


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _font_cache:
        path = _pick_font(_FONT_BOLD_CANDIDATES if bold else _FONT_REG_CANDIDATES)
        try:
            _font_cache[key] = ImageFont.truetype(path, size, index=0)
        except Exception:
            _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


# ---------------------------------------------------------------- 题序 ID 编解码
_ID_CHECK_MASK = 0b1001


def _crc5(value: int) -> int:
    """20 位题号的 CRC-5（多项式 x^5+x^2+1），能检出全部 1 位/2 位错误。"""
    crc = 0b11111
    for i in range(NO_BITS - 1, -1, -1):
        bit = (value >> i) & 1
        top = (crc >> 4) & 1
        crc = (crc << 1) & 0b11111
        if top ^ bit:
            crc ^= 0b00101
    return crc & 0b11111


def fmt_no(v) -> str:
    """题号整数 → 字符串（"03370" 5 位补零；10 万以上自然是 6 位）。

    15 万题口径（t11）：题号 100000..1048575 也编进同一个 20 位字段，
    所以这里只保证**最小** 5 位，不截断位数。
    """
    return "%0*d" % (NO_MIN_WIDTH, int(v))


def encode_id_bits(no) -> list:
    """题号（"03370" / 3370 / "100001"）→ 25 个 0/1（5×5 行优先）：20 位题号 + 5 位 CRC。"""
    try:
        v = int(str(no).strip())
    except (TypeError, ValueError):
        raise ValueError("题号不是整数：%r" % (no,))
    if not (0 <= v <= MAX_NO):
        raise ValueError("题号超出 20bit 范围：%r" % (no,))
    data = [(v >> (NO_BITS - 1 - i)) & 1 for i in range(NO_BITS)]
    crc = _crc5(v)
    return data + [(crc >> (CHK_BITS - 1 - i)) & 1 for i in range(CHK_BITS)]


def decode_id_bits(bits):
    """25 个 0/1 → 题号字符串（"03370" / "100001"）；长度/CRC 不对返回 None。"""
    try:
        bits = [1 if int(b) else 0 for b in bits]
    except Exception:
        return None
    if len(bits) != ID_GRID * ID_GRID:
        return None
    v = 0
    for b in bits[:NO_BITS]:
        v = (v << 1) | b
    chk = 0
    for b in bits[NO_BITS:]:
        chk = (chk << 1) | b
    if chk != _crc5(v):
        return None
    if not (1 <= v <= MAX_NO):       # 00000 不是题号；20 位装得下的都是本库可能有的题号
        return None
    return fmt_no(v)


def encode_idx_bits(idx) -> list:
    """兼容旧名：现在编码的是**题号**（不是卷内题序）。"""
    return encode_id_bits(idx)


def decode_idx_bits(bits):
    """兼容旧名：返回题号整数；旧 4×4 卡（16 位）会返回 None（长度不符）。"""
    no = decode_id_bits(bits)
    return int(no) if no else None


def legacy_idx_bits(bits):
    """旧 4×4 卡解码（只用于识别「这是旧版本卡」）。"""
    try:
        bits = [1 if int(b) else 0 for b in bits]
    except Exception:
        return None
    if len(bits) != 16:
        return None
    idx = 0
    for b in bits[:12]:
        idx = (idx << 1) | b
    check = 0
    for b in bits[12:]:
        check = (check << 1) | b
    nib = [(idx >> (4 * i)) & 0xF for i in range(3)]
    if check != ((nib[0] ^ nib[1] ^ nib[2] ^ _ID_CHECK_MASK) & 0xF):
        return None
    return idx


def bits_fill_ratio(bits) -> float:
    """ID 格里黑格占比，供判卷侧粗查是否读到了东西。"""
    return sum(1 for b in bits if b) / 16.0


# ---------------------------------------------------------------- 题型/页序
def kind_of(item: dict) -> str:
    """manifest item → 答题单元种类。"""
    qtype = (item.get("qtype") or "").strip()
    if qtype == "选择" or qtype == "判断":
        return KIND_OMR
    if qtype == "解答":
        return KIND_SOLUTION
    return KIND_FILL


def _options_of(item: dict) -> list:
    qtype = (item.get("qtype") or "").strip()
    opts = item.get("options") or []
    if qtype == "判断" and not opts:
        return ["√", "×"]
    return list(opts) if opts else list(DEFAULT_OPTIONS)


def _n_blanks(item: dict) -> int:
    keys = item.get("answer_key") or []
    blanks = {k.get("blank", i + 1) for i, k in enumerate(keys)}
    return max(1, len(blanks))


def paper_pages(manifest: dict) -> int:
    """卷面（纸质卷）页数 A；card 页码接在它后面（P{A+M}）。"""
    for key in ("paper", "paper_info"):
        p = manifest.get(key)
        if isinstance(p, dict) and p.get("pages"):
            try:
                return int(p["pages"])
            except Exception:
                pass
    try:
        return int(manifest.get("paper_pages") or 0)
    except Exception:
        return 0


#: 卷面页数缺失时的告警文案（不静默按 1 起算）
PAPER_PAGES_WARNING = (
    "manifest['paper']['pages'] 缺失（旧卷/单测）：答题卡页码按 A=0 起算，"
    "与卷面合并后页码会对不上；render_paper 收尾应写入 manifest['paper']={'pages': n}")


def page_footer_text(page_no: int, n_pages: int, label: int) -> str:
    """卡页脚文案（口径：卷面与答题卡各自编号，卡只标注续页绝对页号 P{A+M}）。"""
    return "答题卡 · 第 %d 页 / 共 %d 页 ·（卷面续页 P%d）" % (page_no, n_pages, label)


# ---------------------------------------------------------------- 版面规划（两列省纸版）
def _id_box(top: int, col: int = 0) -> dict:
    """每题标识格（5×5）：位置随列走，编码的是**题号**（t46）。"""
    return {"x": col_id_x(col), "y": top + ID_TOP_DY, "cell": ID_CELL,
            "grid": ID_GRID, "bits": None}


def _plan_omr(item: dict, top: int, col: int = 0) -> tuple:
    opts = _options_of(item)
    n = max(1, min(len(opts), 6))
    label_x = col_content_x(col)
    cells = []
    for i, opt in enumerate(opts[:n]):
        cells.append({
            "opt": str(opt),
            "x": label_x + OMR_OFFSET + i * OMR_PITCH,
            "y": top + 56,
            "w": OMR_CELL,
            "h": OMR_CELL,
        })
    h = 56 + OMR_CELL + 12
    return {"id_box": _id_box(top, col), "cells": cells, "label_x": label_x}, h


def _plan_fill(item: dict, top: int, col: int = 0) -> tuple:
    n = min(_n_blanks(item), 12)
    label_x = col_content_x(col)
    per_row = max(1, (COL_CONTENT_W + FILL_GAP) // (FILL_W + FILL_GAP))
    boxes = []
    rows = 0
    for i in range(n):
        r, c = divmod(i, per_row)
        rows = r + 1
        boxes.append({
            "blank": i + 1,
            "x": label_x + c * (FILL_W + FILL_GAP),
            "y": top + 72 + r * (FILL_H + 40),
            "w": FILL_W,
            "h": FILL_H,
        })
    h = 72 + (rows - 1) * (FILL_H + 40) + FILL_H + 12
    return {"id_box": _id_box(top, col), "boxes": boxes, "label_x": label_x}, h


#: 解答块内边距（t38 收紧）：顶部给题号/ID，下面留过程区→答案的书写间距
SOL_TOP = 52
SOL_GAP = 26
SOL_BOTTOM = 4
MIN_PROCESS_H = mm(20)       # 压缩下限（20mm 仍能写两行过程）
MIN_SOLUTION_H = SOL_TOP + MIN_PROCESS_H + SOL_GAP + ANSWER_H + SOL_BOTTOM  # 可塞入的最小高度


def _plan_solution(item: dict, top: int, max_h: int = None) -> tuple:
    """解答题整宽（占满两列），过程区 360px，最终答案框 800×118。"""
    n = min(_n_blanks(item), 20)
    cols = 1 if n == 1 else (2 if n == 2 else 3)
    w_box = min(mm(67.7), (CONTENT_W - (cols - 1) * mm(8.5)) // cols)  # ≈800
    rows = (n + cols - 1) // cols
    process_h = PROCESS_H
    answer_top = top + SOL_TOP + process_h + SOL_GAP
    h = (answer_top - top) + (rows - 1) * (ANSWER_H + 40) + ANSWER_H + SOL_BOTTOM
    if max_h is not None and h > max_h:
        process_h = max(MIN_PROCESS_H, PROCESS_H - (h - max_h))
        answer_top = top + SOL_TOP + process_h + SOL_GAP
        h = (answer_top - top) + (rows - 1) * (ANSWER_H + 40) + ANSWER_H + SOL_BOTTOM
    process_box = {"x": CONTENT_X, "y": top + SOL_TOP, "w": CONTENT_W, "h": process_h}
    answer_boxes = []
    for i in range(n):
        r, c = divmod(i, cols)
        answer_boxes.append({
            "blank": i + 1,
            "x": CONTENT_X + c * (w_box + mm(8.5)),
            "y": answer_top + r * (ANSWER_H + 40),
            "w": w_box,
            "h": ANSWER_H,
        })
    return {"id_box": _id_box(top, 0), "process_box": process_box,
            "answer_boxes": answer_boxes, "label_x": CONTENT_X}, h


def plan_item(item: dict, kind: str, top: int, max_h: int = None, col: int = 0) -> tuple:
    """规划一题的作答单元几何 → (几何 dict, 块高)。几何只依赖 (top, col) 的平移。"""
    if kind == KIND_OMR:
        geom, h = _plan_omr(item, top, col=col)
    elif kind == KIND_SOLUTION:
        geom, h = _plan_solution(item, top, max_h=max_h)
    else:
        geom, h = _plan_fill(item, top, col=col)
    no = item.get("no") if item.get("no") is not None else item.get("idx")
    bits = encode_id_bits(no)
    geom["id_box"]["bits"] = bits
    geom["id_bits"] = bits
    return geom, h


def plan_from_anchor(anchor_x: int, anchor_y: int, kind: str,
                     n_options: int = 4, n_blanks: int = 1, grid: int = None) -> dict:
    """只给标识块左上角 → 反推这一题的作答格几何（t46 无 manifest 判卷用）。

    标识块固定在每题块左上角（`id_box = (col_id_x, top + ID_TOP_DY)`），
    所以 `top = anchor_y - ID_TOP_DY`，列由 anchor_x 落在左/右半页决定。
    返回的几何与 `plan_item` 完全同源，两列排布同样成立。
    """
    grid = int(grid or ID_GRID)
    col = 0 if int(anchor_x) < PAGE_W // 2 else 1
    top = int(anchor_y) - ID_TOP_DY
    _ = grid        # 旧卡（4×4）的几何由 manifest 直接给；这里只为接口对称，不参与推算
    if kind == KIND_OMR:
        opts = [chr(ord("A") + i) for i in range(max(1, int(n_options or 4)))]
        item = {"idx": 1, "options": opts}
        geom, _h = _plan_omr(item, top, col=col)
    elif kind == KIND_SOLUTION:
        item = {"idx": 1, "answer_key": [{"blank": i + 1} for i in range(max(1, int(n_blanks)))]}
        geom, _h = _plan_solution(item, top)
    else:
        item = {"idx": 1, "answer_key": [{"blank": i + 1} for i in range(max(1, int(n_blanks)))]}
        geom, _h = _plan_fill(item, top, col=col)
    return geom


def _row_height(row: dict) -> int:
    """一行（选择/填空 1~2 题，解答 1 题）的高度 = 各题块高最大值。"""
    return max(plan_item(it, row["kind"], 0, col=col)[1] for it, col in row["cells"])


def _build_rows(items: list) -> list:
    """把题流切成「行」：选择/填空两题一行（左右列），解答一题一行（整宽）。"""
    rows, buf, buf_kind = [], [], None

    def flush():
        nonlocal buf
        if not buf:
            return
        cells = [(it, i) for i, it in enumerate(buf[:2])]
        rows.append({"kind": buf_kind, "cells": cells})
        buf = []

    for item in items:
        kind = kind_of(item)
        if kind == KIND_SOLUTION:
            flush()
            rows.append({"kind": kind, "cells": [(item, 0)]})
            continue
        if kind != buf_kind:
            flush()
            buf_kind = kind
        buf.append(item)
        if len(buf) == 2:
            flush()
    flush()
    return rows


def paginate(items: list) -> list:
    """把 item 切成页 → [{items:[(item, kind, geom)], headers:[rect]}]（按行装箱）。"""
    body_top = BODY_RECT[1]
    body_bottom = body_top + BODY_RECT[3]
    pages = []
    cur = None
    y = None
    prev_kind = None

    def new_page():
        p = {"items": [], "headers": []}
        pages.append(p)
        return p

    for row in _build_rows(items):
        kind = row["kind"]
        row_h = _row_height(row)
        need_header = (kind != prev_kind) or (cur is None)
        header_h = HEADER_H + HEADER_GAP if need_header else 0
        gap = ITEM_GAP if (cur is not None and cur["items"]) else 0
        remain = body_bottom - (y if y is not None else body_top) - header_h - gap
        can_squeeze = (kind == KIND_SOLUTION and remain >= MIN_SOLUTION_H)
        if cur is None or (y + header_h + gap + row_h > body_bottom and not can_squeeze):
            cur = new_page()
            y = body_top
            need_header = True
            header_h = HEADER_H + HEADER_GAP
            gap = 0
            remain = body_bottom - y - header_h - gap
        if need_header:
            cur["headers"].append({
                "x": CONTENT_X,
                "y": y,
                "w": COL_CONTENT_W * 2 + (COL_W - COL_CONTENT_X - COL_CONTENT_W) * 2,
                "h": HEADER_H,
                "text": KIND_LABEL[kind],
            })
            y += header_h
            prev_kind = kind
        else:
            y += gap
        for item, col in row["cells"]:
            geom, _h = plan_item(item, kind, y, max_h=body_bottom - y - 5, col=col)
            cur["items"].append((item, kind, geom))
        # plan_item 已按剩余空间压缩过程区；行高按「实际可用」推进，避免算出负空间
        y += min(row_h, body_bottom - y) if kind == KIND_SOLUTION else row_h

    if not pages:
        pages = [{"items": [], "headers": []}]
    return pages


# ---------------------------------------------------------------- 绘图
def _text(d, x, y, s, font, fill=0):
    d.text((x, y), s, font=font, fill=fill)


def _text_center(d, cx, y, s, font, fill=0):
    w = d.textlength(s, font=font)
    d.text((cx - w / 2.0, y), s, font=font, fill=fill)


def _text_right(d, rx, y, s, font, fill=0):
    w = d.textlength(s, font=font)
    d.text((rx - w, y), s, font=font, fill=fill)


def _wrap(d, s, font, max_w):
    lines, cur = [], ""
    for ch in s:
        if not cur or d.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _fit_size(d, s, max_w, size, bold=False, min_size=12):
    while size > min_size and d.textlength(s, font=_font(size, bold)) > max_w:
        size -= 2
    return size


def page_marker_ids(page_no: int) -> dict:
    """第 page_no 页（1 基）的四角 id → {角号(0..3): marker_id}。

    v4 约定：第 p 页（0 基）= ``4p .. 4p+3``；角号沿用 ARUCO_POS：
    0=左上 1=右上 2=右下 3=左下。第 1 页 → 0/1/2/3（与旧卡同 id，
    所以旧卡的第 1 页照片用新逻辑也能标定）。
    """
    base = (max(1, int(page_no)) - 1) * ARUCO_PER_PAGE
    return {corner: base + corner for corner in ARUCO_POS}


def decode_page_from_ids(ids) -> int | None:
    """四角 marker id → 页序（1 基）；不是「同一页的完整四角」就返回 None。

    * 新卡（v4）：id 4p..4p+3 全在 → p+1；
    * 旧卡（v2/v3）：每页都是 0..3 → 返回 1（页序只能靠 QR 定）。
    """
    try:
        got = {int(i) for i in ids}
    except (TypeError, ValueError):
        return None
    if len(got) != ARUCO_PER_PAGE:
        return None
    for page0 in range(ARUCO_MAX_PAGES):
        want = set(range(page0 * ARUCO_PER_PAGE, page0 * ARUCO_PER_PAGE + ARUCO_PER_PAGE))
        if got == want:
            return page0 + 1
    return None


def _qr_np(payload: dict, size: int = QR_SIZE) -> np.ndarray:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    q = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=4,
    )
    q.add_data(data)
    q.make(fit=True)
    m = np.array(q.get_matrix(), dtype=np.uint8)
    n = m.shape[0]
    scale = max(1, size // n)
    img = Image.fromarray(np.where(m == 1, 0, 255).astype(np.uint8), "L")
    img = img.resize((n * scale, n * scale), Image.NEAREST)
    canvas = Image.new("L", (size, size), 255)  # 静区：QR 四周留白
    off = (size - n * scale) // 2
    canvas.paste(img, (off, off))
    return np.array(canvas)


_ARUCO_IMGS: dict = {}


def _aruco_np(marker_id: int, size: int = ARUCO_SIZE) -> np.ndarray:
    key = (marker_id, size)
    if key not in _ARUCO_IMGS:
        dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICT_ID)
        _ARUCO_IMGS[key] = cv2.aruco.generateImageMarker(dictionary, marker_id, size)
    return _ARUCO_IMGS[key]


def _paste(img: Image.Image, arr: np.ndarray, x: int, y: int):
    img.paste(Image.fromarray(arr, "L"), (int(x), int(y)))


def _draw_header(d, manifest: dict, page_no: int, n_pages: int, label: int):
    """页眉压到 2 行（t38 省纸）：第 1 行标题+卷号，第 2 行姓名/日期+涂卡说明。

    卷号仍保持大字号（QR 失效时供人工录入）；页码口径不变，见页脚。
    """
    right = QR_POS[0] - 30  # 不压 QR
    left = 390

    title = (manifest.get("title") or "答题卡").strip()
    pid = str(manifest.get("pid") or "")
    line1 = "%s　·　卷号 %s" % (title, pid)
    size = _fit_size(d, line1, right - left, 44, bold=True)
    _text(d, left, 170, line1, _font(size, True))

    hint = ("姓名：__________　班级：________　日期：__________　·　"
            "选择题涂黑方框；填空/解答写在框内，勿越界；左侧 %d×%d 黑白格是机读题号，勿涂改"
            % (ID_GRID, ID_GRID))
    size = _fit_size(d, hint, right - left, 24)
    _text(d, left, 232, hint, _font(size), fill=80)

    d.line([(left, 286), (right, 286)], fill=120, width=2)


def _draw_footer(d, page_no: int, n_pages: int, label: int):
    cx = PAGE_W / 2.0
    _text_center(d, cx, 3206, page_footer_text(page_no, n_pages, label),
                 _font(24, True), fill=40)
    _text_center(d, cx, 3248, "请勿折叠 · 四角黑框与右上二维码用于自动对位，勿涂画", _font(20), fill=100)


def _draw_id_box(d, id_box: dict):
    """画 5×5 标识格：黑=1、白底细框、外面一圈黑框（判卷靠框找块）。"""
    x, y, cell = id_box["x"], id_box["y"], id_box.get("cell", ID_CELL)
    bits = id_box["bits"]
    for r in range(ID_GRID):
        for c in range(ID_GRID):
            x0 = x + c * cell
            y0 = y + r * cell
            if bits[r * ID_GRID + c]:
                d.rectangle([x0, y0, x0 + cell - 2, y0 + cell - 2], fill=0)
            else:
                d.rectangle([x0, y0, x0 + cell - 2, y0 + cell - 2], outline=170, width=1)
    d.rectangle([x - 3, y - 3, x + ID_GRID * cell + 1, y + ID_GRID * cell + 1],
                outline=0, width=2)


def _draw_item(d, it: dict):
    idx = it["idx"]
    kind = it["kind"]
    qno = "第 %d 题" % idx
    _text(d, it.get("label_x", CONTENT_X), it["id_box"]["y"] - 4, qno,
          _font(QUESTION_NO_FONT, True), fill=20)
    _draw_id_box(d, it["id_box"])

    if kind == KIND_OMR:
        for c in it["cells"]:
            d.rectangle([c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]], outline=40, width=3)
            _text_center(d, c["x"] + c["w"] / 2.0, c["y"] - 28, str(c["opt"]), _font(24, True), fill=30)
    elif kind == KIND_FILL:
        for b in it["boxes"]:
            _text(d, b["x"], b["y"] - 30, "空 %d" % b["blank"], _font(20), fill=70)
            d.rectangle([b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]], outline=40, width=3)
    else:
        p = it["process_box"]
        d.rectangle([p["x"], p["y"], p["x"] + p["w"], p["y"] + p["h"]], outline=40, width=3)
        _text(d, p["x"] + 14, p["y"] + 10, "过程区（过程不计分，写清步骤即可）", _font(22), fill=120)
        for b in it["answer_boxes"]:
            _text(d, b["x"], b["y"] - 28, "最终答案 (%d)" % b["blank"], _font(20, True), fill=30)
            d.rectangle([b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]], outline=0, width=3)


def render_page(manifest: dict, page: dict, png_path: str, n_pages: int) -> str:
    """渲染一页到 PNG（2480×3508，灰度）。"""
    img = Image.new("L", (PAGE_W, PAGE_H), 255)
    d = ImageDraw.Draw(img)

    _draw_header(d, manifest, page["page"], n_pages, page["label"])

    # ArUco 四角（v4：每页 id = 4(p-1)..4(p-1)+3，页序就在标记里）
    for mk in (page.get("marks") or {}).get("aruco") or []:
        _paste(img, _aruco_np(int(mk["id"])), int(mk["x"]), int(mk["y"]))
    # 卷号 QR（v4 只有第 1 页有）
    qr = page.get("qr")
    if qr:
        _paste(img, _qr_np(qr["payload"], int(qr.get("size") or QR_SIZE)),
               qr["x"], qr["y"])

    # 学生信息区（v3，只第 1 页）
    if page.get("student"):
        _draw_student(d, page["student"])

    # 题型标注（中文说明）
    for hd in page.get("headers", []):
        _text(d, hd["x"], hd["y"], hd["text"], _font(28, True), fill=0)

    for it in page["items"]:
        _draw_item(d, it)

    _draw_footer(d, page["page"], n_pages, page["label"])

    os.makedirs(os.path.dirname(png_path) or ".", exist_ok=True)
    img.save(png_path, "PNG", optimize=True)
    return png_path


def _load_gray(image) -> object:
    """路径 / ndarray → 单通道灰度图；读不出返回 None。"""
    if isinstance(image, str):
        arr = cv2.imread(image, cv2.IMREAD_GRAYSCALE)
    else:
        arr = image
        if arr is None:
            return None
        if arr.ndim == 3:
            arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY if arr.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
    return arr


_QR_DECODERS = None


def _qr_decoders():
    """按可靠度排序的二维码解码器（cv2 的 QRCodeDetector 对某些 payload 会失手）。"""
    global _QR_DECODERS
    if _QR_DECODERS is None:
        out = []
        if hasattr(cv2, "QRCodeDetectorAruco"):
            out.append(cv2.QRCodeDetectorAruco())
        out.append(cv2.QRCodeDetector())
        _QR_DECODERS = out
    return _QR_DECODERS


def read_qr(image, qr: dict, pad: int = 24) -> object:
    """解答题卡上的卷号 QR → {"pid":..., "p":...}；解不出返回 None。

    image 可以是 PNG 路径或已读入的 ndarray（BGR/灰度都行）。
    grade.py 直接复用这个函数；多后端 + 放大重试，避免单一 cv2 解码器的偶发失手。
    """
    img = _load_gray(image)
    if img is None or not qr:
        return None
    x, y, s = int(qr["x"]), int(qr["y"]), int(qr.get("size") or QR_SIZE)
    x0, y0 = max(0, x - pad), max(0, y - pad)
    crop = img[y0:y + s + pad, x0:x + s + pad]
    if crop is None or crop.size == 0:
        return None
    variants = [crop, cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)]
    for det in _qr_decoders():
        for cand in variants:
            try:
                data = det.detectAndDecode(cand)[0]
            except Exception:
                continue
            if data:
                try:
                    return json.loads(data)
                except Exception:
                    return None
    return None


# ---------------------------------------------------------------- 作答格枚举
def iter_answer_cells(card: dict, pid: str = None):
    """枚举本卷所有「非选择」作答格（填空框 + 解答最终答案框），按页/版面顺序。

    产出 dict：{"page", "label", "pid", "idx", "no", "kind", "blank", "box"}。
    判卷侧（grade.py / 批量 OCR 拼图）直接按 box 在原图上裁切即可，格内是纯白不留字。
    """
    for page in card.get("pages") or []:
        page_pid = ((page.get("qr") or {}).get("payload") or {}).get("pid") or pid
        for it in page.get("items") or []:
            kind = it.get("kind")
            if kind == KIND_FILL:
                boxes = it.get("boxes") or []
            elif kind == KIND_SOLUTION:
                boxes = it.get("answer_boxes") or []
            else:
                continue
            for b in boxes:
                yield {
                    "page": page.get("page"),
                    "label": page.get("label"),
                    "pid": pid if pid is not None else page_pid,
                    "idx": it.get("idx"),
                    "no": it.get("no"),
                    "kind": kind,
                    "blank": b.get("blank"),
                    "box": {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]},
                }


def crop_cell(image, box: dict, inset: int = 6):
    """按作答格矩形裁切（往里收 inset，避开 3px 边框）；越界自动夹到图内。"""
    img = _load_gray(image)
    if img is None:
        return None
    h, w = img.shape[:2]
    x0 = max(0, int(box["x"]) + inset)
    y0 = max(0, int(box["y"]) + inset)
    x1 = min(w, int(box["x"]) + int(box["w"]) - inset)
    y1 = min(h, int(box["y"]) + int(box["h"]) - inset)
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


# ---------------------------------------------------------------- PDF
def pngs_to_pdf(png_paths: list, pdf_path: str, title: str = "答题卡") -> str:
    """把 2480×3508@300dpi 的 PNG 按 A4 页面顺序写成 PDF（1 图 1 页，无重采样）。"""
    import pymupdf

    doc = pymupdf.open()
    for p in png_paths:
        page = doc.new_page(width=595.2756, height=841.8898)  # A4 pt
        with open(p, "rb") as f:
            page.insert_image(page.rect, stream=f.read())
    doc.set_metadata({"title": title, "producer": "math-paper-app/card.py", "creator": "card.py"})
    os.makedirs(os.path.dirname(pdf_path) or ".", exist_ok=True)
    doc.save(pdf_path, deflate=True, garbage=4)
    doc.close()
    return pdf_path


# ---------------------------------------------------------------- 主入口
# ---------------------------------------------------------------- 学生信息区（v3）
def student_layout(digits: int = None, top: int = None, cell_px: int = None,
                   gap: int = STUDENT_GAP) -> dict:
    """学号涂卡区版式：**每位一行 10 格（0-9）**，共 N 行；右侧姓名/班级手写框。

    * 位数默认 8，可配 6/8/10（`student_digits`）；位数越多行越高，格子会自动缩；
    * 46mm 高度装不下「6mm 格 × 10 行」，所以格子按可用高度自适应（实测 ~40px），
      真实 `cell_px` 写进 manifest，判卷按它裁切即可，不依赖常量；
    * 每个格都带绝对坐标，判卷侧不用再算。
    """
    digits = int(digits or STUDENT_DIGITS)
    if digits not in (6, 8, 10):
        digits = STUDENT_DIGITS
    top = STUDENT_TOP if top is None else int(top)
    if cell_px is None:
        cell = (STUDENT_H - 64 - (digits - 1) * gap) // digits
        cell = max(STUDENT_MIN_CELL, min(71, cell))
    else:
        cell = max(STUDENT_MIN_CELL, min(71, int(cell_px)))
    col_pitch = cell + 6           # 同一行里相邻数字的间距
    row_pitch = cell + gap
    x0 = 380
    y0 = top + 64
    rows = []
    for i in range(digits):
        cells = []
        for dgt in range(10):
            cells.append({
                "digit": dgt,
                "x": x0 + dgt * col_pitch,
                "y": y0 + i * row_pitch,
                "w": cell, "h": cell,
            })
        rows.append({"index": i + 1, "cells": cells})
    grid_w = 10 * col_pitch - 6
    name_x = x0 + grid_w + 120
    return {
        "no_digits": digits,
        "origin": [x0, y0],
        "cell_px": cell,
        "grid_px": row_pitch,
        "gap_px": gap,
        "col_pitch": col_pitch,
        "row_pitch": row_pitch,
        "threshold": STUDENT_THRESHOLD,
        "layout": "row-per-digit",     # 每位一行 10 格
        "area": {"x": 275, "y": top, "w": STUDENT_RIGHT - 275, "h": STUDENT_H},
        "digits_grid": {"x": x0, "y": y0, "w": grid_w,
                        "h": digits * row_pitch - gap},
        "columns": rows,
        "name_box": {"x": name_x, "y": y0 + 20, "w": 560, "h": 100},
        "class_box": {"x": name_x, "y": y0 + 170, "w": 560, "h": 100},
        "hint": "学号请用黑色笔把对应数字整个涂黑，涂错一格即作废",
    }


def encode_student_bits(student_no, digits: int = STUDENT_DIGITS) -> list:
    """学号 → 每位数字（0-9）列表；不足左补 0，非数字位记 None。"""
    digits = int(digits or STUDENT_DIGITS)
    s = str(student_no if student_no is not None else "").strip()
    s = "".join(ch for ch in s if ch.isdigit())
    if not s:
        return [None] * digits
    s = s[-digits:].rjust(digits, "0")
    return [int(ch) for ch in s]


def decode_student_bits(col_ratios, threshold: float = STUDENT_THRESHOLD,
                        max_unknown: int = 1):
    """每位 10 个墨迹比 → 学号字符串（未知位写成 '?'）。

    * 每位取墨迹最深的那一格，低于阈值算「该位未知」；
    * 未知位超过 `max_unknown`（默认 1）→ 返回 None；
    * 全空/格式不对一律返回 None，**绝不抛异常**。
    """
    try:
        if not col_ratios:
            return None
        out = []
        unknown = 0
        for col in col_ratios:
            best_i, best = None, -1.0
            for d, r in enumerate(col or []):
                try:
                    rv = float(r)
                except (TypeError, ValueError):
                    continue
                if rv > best:
                    best, best_i = rv, d
            if best_i is None or best < float(threshold):
                out.append("?")
                unknown += 1
            else:
                out.append(str(best_i))
        if unknown > int(max_unknown):
            return None
        return "".join(out)
    except Exception:
        return None


def student_ink_ratios(card_info: dict, image, threshold: float = None) -> list:
    """从复原图上学号区取每格墨迹比 → [[10 个], ...]（按位）。"""
    st = (card_info or {}).get("student") or {}
    cols = st.get("columns") or []
    if not cols:
        return []
    img = _load_gray(image)
    if img is None:
        return []
    out = []
    for col in cols:
        row = []
        for c in col.get("cells") or []:
            patch = crop_cell(img, c, inset=max(3, int(c.get("w", 41)) // 6))
            if patch is None or getattr(patch, "size", 0) == 0:
                row.append(0.0)
            else:
                row.append(float((patch < 128).mean()))
        out.append(row)
    return out


def read_student_no(card_info: dict, image, threshold: float = None) -> str:
    """读学号 → 字符串（含 '?' 表示该位没涂/太淡）；读不出返回 ""，绝不抛异常。"""
    try:
        st = (card_info or {}).get("student") or {}
        if not st.get("columns"):
            return ""          # v1/v2 卡没有学生区
        th = float(threshold if threshold is not None
                   else st.get("threshold", STUDENT_THRESHOLD))
        ratios = student_ink_ratios(card_info, image)
        if not ratios:
            return ""
        return decode_student_bits(ratios, threshold=th, max_unknown=1) or ""
    except Exception:
        return ""


def _dashed_rect(d, x0, y0, x1, y1, dash: int = 14, gap: int = 10, fill=120, width=2):
    x = x0
    while x < x1:
        d.line([(x, y0), (min(x + dash, x1), y0)], fill=fill, width=width)
        d.line([(x, y1), (min(x + dash, x1), y1)], fill=fill, width=width)
        x += dash + gap
    y = y0
    while y < y1:
        d.line([(x0, y), (x0, min(y + dash, y1))], fill=fill, width=width)
        d.line([(x1, y), (x1, min(y + dash, y1))], fill=fill, width=width)
        y += dash + gap


def _draw_student(d, st: dict, dark: bool = False):
    """画学生信息区：虚线外框 + 每位一行 10 格 + 姓名/班级手写框。"""
    a = st["area"]
    _dashed_rect(d, a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"])
    _text(d, a["x"] + 16, a["y"] + 6, "学生信息（学号涂卡）", _font(22, True))
    _text(d, st["name_box"]["x"], a["y"] + 12, st.get("hint") or "", _font(18), fill=90)
    # 列头：0..9（对齐每一行的 10 个格子）
    first = st["columns"][0]["cells"]
    for c in first:
        _text(d, c["x"] + c["w"] // 3, st["origin"][1] - 24, str(c["digit"]), _font(18))
    # 行号：第 1..N 位
    for i, row in enumerate(st["columns"]):
        y = row["cells"][0]["y"]
        _text(d, a["x"] + 16, y + st["cell_px"] // 3, "第%d位" % (i + 1), _font(18))
    for row in st["columns"]:
        for c in row["cells"]:
            d.rectangle([c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"]],
                        outline=40 if dark else 60, width=2)
    for key, label in (("name_box", "姓名："), ("class_box", "班级：")):
        b = st[key]
        _text(d, b["x"] - 90, b["y"] + b["h"] // 2 - 14, label, _font(26, True))
        d.rectangle([b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]], outline=80, width=2)


def build_card(manifest: dict, out_dir: str) -> dict:
    """生成答题卡 → {"pdf", "png": [...], "card": {...}, "manifest": manifest, "health": {...}}

    * `card` 字段同时合并进传入的 manifest（`manifest["card"]`），页码接卷面最后一页；
    * `manifest["card"]["version"]` = `CARD_VERSION`（2 = 5×5 编码题号；1 = 旧卡 4×4，只兼容不生成）；
    * QR payload = `{"pid", "p", "v"}`：v4 只在第 1 页印（`p` 恒为 1），页序看 `marks.aruco[].id`；
    * **`health` 是返回值，不写进 manifest**（`manifest["card"]["health"]` 一直为空，别去那里找）。
    """
    os.makedirs(out_dir, exist_ok=True)
    # 上游 manifest 字段不全时兜底（idx 缺 → 按顺序补；不影响 manifest 本身）
    items = []
    for pos, raw in enumerate(manifest.get("items") or [], start=1):
        it = dict(raw)
        try:
            it["idx"] = int(it.get("idx") or pos)
        except (TypeError, ValueError):
            it["idx"] = pos
        items.append(it)
    start = paper_pages(manifest)
    warnings = []
    if start <= 0:
        warnings.append(PAPER_PAGES_WARNING)
        log.warning(PAPER_PAGES_WARNING)      # 不静默：合并 PDF 时页码会对不上

    planned = paginate(items)
    n_pages = len(planned)
    pages = []
    for i, pg in enumerate(planned, start=1):
        label = start + i
        page = {
            "page": i,
            "label": label,
            "body": {"x": BODY_RECT[0], "y": BODY_RECT[1], "w": BODY_RECT[2], "h": BODY_RECT[3]},
            # v4：页序编码在 ArUco id 里（第 p 页 = 4p..4p+3），见 page_marker_ids()
            "group": i - 1,
            "marks": {"aruco": [
                {"id": mid, "corner": corner, "x": ARUCO_POS[corner][0],
                 "y": ARUCO_POS[corner][1], "size": ARUCO_SIZE}
                for corner, mid in sorted(page_marker_ids(i).items())
            ]},
            # 只有第 1 页印 QR（带 pid + 版本；p 固定 1，页序不再靠它）
            "qr": ({
                "x": QR_POS[0], "y": QR_POS[1], "size": QR_SIZE,
                "payload": {"pid": str(manifest.get("pid") or ""), "p": 1,
                            "v": CARD_VERSION},
            } if i == 1 else None),
            "headers": list(pg["headers"]),
            "items": [],
        }
        for item, kind, geom in pg["items"]:
            entry = {
                "idx": item["idx"],
                "no": item.get("no"),
                "qtype": item.get("qtype"),
                "kind": kind,
            }
            entry.update(geom)
            page["items"].append(entry)
        pages.append(page)

    student = None
    if manifest.get("student_area", True) is not False:
        student = student_layout(digits=manifest.get("student_digits"),
                                 cell_px=manifest.get("student_cell_px"))
        if pages:
            pages[0]["student"] = student

    card = {
        "pages": pages,
        "n_pages": n_pages,
        "start_page": start + 1,
        "paper_pages": start,
        "warnings": list(warnings),
        "dpi": DPI,
        "w_px": PAGE_W,
        "h_px": PAGE_H,
        "version": CARD_VERSION,
        # 判卷侧据此判断「页序在 ArUco id 里」（v4）还是「靠每页 QR 的 p」（v2/v3）
        "aruco_groups": True,
        "aruco_per_page": ARUCO_PER_PAGE,
        "qr_pages": [1],
    }
    if student is not None:
        card["student"] = student

    pngs = []
    for page in pages:
        p = os.path.join(out_dir, "card_%d.png" % page["page"])
        render_page(manifest, page, p, n_pages)
        pngs.append(p)
    pdf = pngs_to_pdf(pngs, os.path.join(out_dir, CARD_PDF_NAME),
                      title=(manifest.get("title") or "答题卡") + " · 答题卡")

    # 打印容错：卡的 Aruco/QR 坐标全按 A4 pt 写死，页面尺寸一被改（Letter/缩放）判卷必错
    import paper as _paper              # 只在生成时导入：A4 规则只有 paper.check_pdf_a4 一份
    sizes = _paper.check_pdf_a4(pdf)
    health = {"page_size_pt": [round(sizes[0][0], 2), round(sizes[0][1], 2)],
              "pages": len(sizes), "a4_ok": True}

    manifest["card"] = card
    return {"pdf": pdf, "png": pngs, "card": card, "manifest": manifest, "health": health}


# ---------------------------------------------------------------- 自检 CLI
def _main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = argv[0] if argv else os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    manifest = {
        "pid": "P20261031-001",
        "title": "七年级数学 · 训练卷 A（自检）",
        "page": {"w_pt": 595.3, "h_pt": 841.9, "dpi": DPI, "w_px": PAGE_W, "h_px": PAGE_H},
        # 卷面 2 页 → 答题卡页脚从 P3 起（口径：卷面/答题卡各自编号，卡标续页绝对页号）
        "paper": {"pages": 2},
        "items": [
            {"idx": i, "no": "%05d" % i, "qtype": "选择",
             "answer_key": [{"blank": 1, "value": "A", "norm": "A"}],
             "options": ["A", "B", "C", "D"]}
            for i in range(1, 13)
        ] + [
            {"idx": 7, "no": "00007", "qtype": "填空",
             "answer_key": [{"blank": 1, "value": "3", "norm": "3"},
                            {"blank": 2, "value": "5", "norm": "5"}]},
            {"idx": 8, "no": "00008", "qtype": "解答",
             "answer_key": [{"blank": 1, "value": "(1) x=4", "norm": "4"},
                            {"blank": 2, "value": "(2) x=9", "norm": "9"}]},
        ],
    }
    res = build_card(manifest, out_dir)
    # 学生信息区裁剪图（给人工核对版式用）
    st = res["card"].get("student") or {}
    crop_path = None
    if st.get("area"):
        from PIL import Image as _Image
        a = st["area"]
        with _Image.open(res["png"][0]) as im:
            crop = im.crop((a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]))
            crop_path = os.path.join(out_dir, "student_area.png")
            crop.save(crop_path)
    print(json.dumps({"pdf": res["pdf"], "png": res["png"], "pages": res["card"]["n_pages"],
                      "version": res["card"]["version"],
                      "student_no_digits": st.get("no_digits"),
                      "student_cell_px": st.get("cell_px"),
                      "student_area_png": crop_path},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
