# -*- coding: utf-8 -*-
"""T5 · 卷面排版（paper.py）：A4 一题一框 + 页眉卷号 + 保真体检。

契约：CONTRACT §3（manifest）/ §4（`render_paper`）。

版式
----
* A4（595.3×841.9pt）+ 2cm 页边距；页眉每页印「标题 + 卷号」并带分隔线，
  页脚「卷面 · 第 PAGE 页 / 共 NUMPAGES 页 · 卷号」（Word 域，导 PDF 时自动刷新）。
  卷面与答题卡**各自编号**：卷面页数由 render_paper 写回 ``manifest["paper"]["pages"]``，
  card.py 拿它算答题卡的起始页码（CONTRACT §3 / §4）。
* 首页顶部：大标题 + 卷号/姓名/班级/日期填写栏 + 一行作答说明 + 分隔线。
* 按大题分组：一、选择题（共 N 题）二、填空题 … 依次编号；标题加粗且与下一题同页。
* 每题一个单元格框（1×1 表格，Table Grid 边框，行不许跨页拆开），题号加粗前置在题干首段，
  题干右下角小字灰字标注知识点「（1.3 有理数的加减）」方便复习。
* 留白：`items[].space_paras > 0` 时按它留空段；为 0 时按题型默认（全 0——答案写在答题卡上，
  选择/填空不留白 —— 卷面只印题，作答在答题卡上）。每段空行 24pt，够手写。

保真体检（`check_docx`）
-----------------------
印卷时题面必须原样落地：OMML 公式（`m:oMath`）、MathType-OLE（`w:object` + WMF 预览）、
几何图（`w:drawing`/`w:pict`）、题内表格（`w:tbl`）、媒体二进制，全都要对得上。

做法是「两路独立数」：期望值从**原始 docx 重新抽 token**算（`docx_rich.extract_tokens`），
观测值从**印出来的 docx** 的 document.xml + word/media 里数。对不上抛 `FidelityError`，
不是 warning —— 少一个公式就等于卷子印错了，必须炸在生成阶段。

`render_paper` 额外做分页体检：页数>0、页数与 PNG 张数一致、每页 ink_ratio 在合理区间
（空白页 = 排版断层，末页空白降级为 warning，因为 Word 常把恰好满页的内容挤出空页）。

模块关系
--------
`render.py`（T6）落地后提供 `docx_to_pdf` / `pdf_to_png`，本模块检测到就直接用；
还没落地时用这里自带的内置实现（Word COM + PyMuPDF），保证 t5 能独立出成品。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import zipfile

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from lxml import etree

import compose
from docx_rich import Token, extract_tokens
from picker import fig_missing          # 缺图判定只有一份实现（picker 负责选题口径）
from taxonomy import all_kps

# ---------------------------------------------------------------- 版式常量
PAGE_W_PT = 595.3          # A4
PAGE_H_PT = 841.9
MARGIN_PT = 56.7           # 2cm（normal 版面）
CONTENT_W_PT = PAGE_W_PT - 2 * MARGIN_PT

#: 版面模式：compact = 省纸模式（默认，用户 2026-10-31 提的「留白太大」）；
#: normal = 原来那套宽松版面（2cm 边距 / 1.15 行距 / 题间 6pt 空段）。
#: A4 规范尺寸（PDF 点）：595.28 × 841.89。允许 ±0.5pt 的舍入误差。
A4_W_PT = 595.28
A4_H_PT = 841.89
A4_TOL_PT = 0.5
PT_PER_MM = 72.0 / 25.4
#: 校准页里那个「量一下」的方框边长（mm → pt ≈ 283.46）
CALIBRATION_MM = 100.0
CALIBRATION_RULER_MM = 10.0

LAYOUT_COMPACT = "compact"
LAYOUT_NORMAL = "normal"
LAYOUTS = (LAYOUT_COMPACT, LAYOUT_NORMAL)
DEFAULT_LAYOUT = LAYOUT_COMPACT

#: 两套版面的几何/间距参数。compact 只压「空白」，不动字号、公式与图片尺寸。
_LAYOUT_SPEC = {
    LAYOUT_COMPACT: {
        "margin_pt": 39.7,        # 1.4cm
        "hf_dist_pt": 14.0,       # 页眉/页脚距边距
        "line_spacing": 1.0,      # 行距
        "space_pt": 1.0,          # 段前/段后
        "cell_mar_x_pt": 4.25,    # 题框左右内边距 0.15cm
        "cell_mar_y_pt": 0.0,     # 上下不额外留白（Word 默认就是 0，再加会白吃 5.7pt/题）
        "spacer_pt": 0.5,         # 题与题之间（不能为 0：两张表贴在一起会被 Word 合并）
        "group_before_pt": 3.0,
        "group_after_pt": 1.0,
        "group_line": 1.0,
        "title_gap_pt": 0.5,
        "kp_line_pt": 9.0,        # 知识点提示行：固定 9pt 行高（字号仍是 8pt）
        "kp_space_pt": 0.5,
    },
    LAYOUT_NORMAL: {
        "margin_pt": MARGIN_PT,
        "hf_dist_pt": 28.0,
        "line_spacing": None,     # None = 沿用 compose 自带的 1.15
        "space_pt": None,         # None = 沿用 compose 自带的 2pt
        "cell_mar_x_pt": None,
        "cell_mar_y_pt": None,
        "spacer_pt": 6.0,
        "group_before_pt": 10.0,
        "group_after_pt": 4.0,
        "group_line": None,
        "title_gap_pt": 2.0,
        "kp_line_pt": None,
        "kp_space_pt": None,
    },
}


def layout_spec(name=None) -> dict:
    """版面名 → 参数（未知名字直接报错，别静默回退成 normal 让用户以为省纸没生效）。"""
    key = str(name or DEFAULT_LAYOUT).strip().lower()
    if key not in _LAYOUT_SPEC:
        raise PaperError("未知版面 layout=%r（只支持 %s）" % (name, " / ".join(LAYOUTS)))
    return dict(_LAYOUT_SPEC[key], name=key)


def _content_w(spec: dict) -> float:
    return PAGE_W_PT - 2 * spec["margin_pt"]


#: 末页墨迹低于这个值就当「末页还偏空」，值得再压一档重排（目标线 2.5%，末页给 2% 的宽容）
LAST_PAGE_INK_MIN = 0.025


def _tighten_spec(spec: dict) -> dict:
    """再压一档（只压空白，不动字号/公式/图片）：末页太空时用它重排一次。"""
    out = dict(spec)
    out["spacer_pt"] = 0.0
    out["group_before_pt"] = max(0.0, spec["group_before_pt"] - 1.5)
    out["group_after_pt"] = 0.0
    out["kp_space_pt"] = 0.0
    out["title_gap_pt"] = 0.0
    return out


def needs_balance(pages: int, ink: list) -> bool:
    """末页是否「太空」→ 值得再压一档重排（单页卷不判：一页就是满的）。"""
    if pages <= 1 or not ink:
        return False
    return float(ink[-1]) < LAST_PAGE_INK_MIN
DPI = 300
PAGE_PX = (2480, 3508)     # A4 @300dpi（CONTRACT §3 的规范像素）

FONT_SIZE_TITLE = 18
FONT_SIZE_GROUP = 13
FONT_SIZE_STEM = 10.5
FONT_SIZE_KP = 8
FONT_SIZE_HEADER = 10
FONT_SIZE_FOOTER = 8
LINE_SPACE_PT = 24         # 书写留白每段的高度（≈0.85cm，够手写一行）
TABLE_STYLE = "Table Grid"
SRC_ROOT = r"E:\OCR"

#: 题型默认留白段数：卷面只印题、作答在答题卡上（用户已定），所以默认全 0。
#: 老师想要"卷面草稿区"时用 params.space 单独调大（CONTRACT §2）。
DEFAULT_SPACE = {"选择": 0, "判断": 0, "填空": 0, "解答": 0, "其他": 0}
QTYPE_ORDER = ("选择", "判断", "填空", "解答", "其他")
_CN_NUM = "一二三四五六七八九十"
_GROUP_LABEL = {"选择": "选择题", "判断": "判断题", "填空": "填空题",
                "解答": "解答题", "其他": "其他题"}

#: 题面自带的编号前缀（"10．" "177．" "3、" "1)"），印卷时去掉，避免"1．1．化简"叠号
_NUM_PREFIX = re.compile(r"^\s*\d{1,3}\s*[．.、)）]\s*")
#: 原题的编排痕迹：【例6】【典例1】【变式5-2】，印卷时连标签一起剥掉（不暴露"第几例的第几个变式"）
_BRACKET_LABEL_RX = re.compile(
    r"^\s*【\s*(?:例|典例|变式)\s*\d+(?:\s*[-－–]\s*\d+)?\s*】\s*|^\s*【\s*变式\s*】\s*")
#: 题干提到图但题面无图的判定，复用 picker.fig_missing（一份规则，别写两套）
#: 需要从卷面观测的 XML 标记
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MARKERS = ("oMath", "object", "drawing", "pict", "tbl", "imagedata", "blip")
_BLANK_INK = 1e-4


class PaperError(RuntimeError):
    """排版/渲染失败。"""


class FidelityError(PaperError):
    """保真体检没过：公式/图片/媒体/题数/关系/Word 可打开性对不上。

    ``code`` 给上层做降级文案用（errors.py 可以直接映射）：
    ``dangling_relationship`` / ``word_cannot_open`` / ``fidelity`` / ``pages``。
    """

    def __init__(self, message: str, code: str = "fidelity"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 小工具
def _qn_local(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _insert_before_any(parent, child, successor_names) -> None:
    """把 child 插到第一个属于 successor_names 的子元素前面（保持 OOXML 元素顺序）。"""
    for existing in parent:
        if _qn_local(existing) in successor_names:
            existing.addprevious(child)
            return
    parent.append(child)


def _bottom_border(par, color="808080", sz=6) -> None:
    pPr = par._p.get_or_add_pPr()
    for old in pPr.findall(qn("w:pBdr")):
        pPr.remove(old)
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(sz))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    pbdr.append(bottom)
    _insert_before_any(pPr, pbdr, {
        "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct",
        "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
        "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
        "suppressOverlap", "jc", "textDirection", "textAlignment", "textboxTightWrap",
        "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"})


def _table_borders(table, sz=6, color="404040") -> None:
    """兜底：模板没有 Table Grid 时手工加边框（schema 顺序：tblBorders 在 tblLayout 之前）。"""
    tblPr = table._tbl.tblPr
    for old in tblPr.findall(qn("w:tblBorders")):
        tblPr.remove(old)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement("w:" + edge)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    _insert_before_any(tblPr, borders, {"shd", "tblLayout", "tblCellMar", "tblLook",
                                        "tblCaption", "tblDescription", "tblPrChange"})


def _set_cn_font(run, name="宋体") -> None:
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), name)
    rFonts.set(qn("w:ascii"), name)
    rFonts.set(qn("w:hAnsi"), name)


def _add_field(par, instr: str, placeholder: str = "1") -> None:
    """插入 Word 域（PAGE / NUMPAGES），Word 打开或导 PDF 时自动算值。"""
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), instr)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = placeholder
    run.append(text)
    fld.append(run)
    par._p.append(fld)


def _gray(run, hex_color="666666") -> None:
    run.font.color.rgb = RGBColor.from_string(hex_color)


# ---------------------------------------------------------------- 题库取 token
_tokens_cache = {}


def source_tokens(no: str):
    """原始题目 docx → token 列表（带媒体二进制）。原始 docx 全程只读。"""
    if no in _tokens_cache:
        return _tokens_cache[no]
    path = os.path.join(SRC_ROOT, "%s.docx" % no)
    if not os.path.exists(path):
        raise PaperError("题目 docx 不存在，没法印卷：%s（题号 %s）" % (path, no))
    try:
        tokens = extract_tokens(path)
    except Exception as exc:  # noqa: BLE001
        raise PaperError("题目 %s 的 docx 解析失败：%s" % (no, exc))
    if not tokens:
        raise PaperError("题目 %s 的 docx 里没有可排版内容：%s" % (no, path))
    _tokens_cache[no] = tokens
    return tokens


def strip_stem_prefix(text: str) -> str:
    """去掉题面自带的编排痕迹，避免与卷面题号叠号 / 暴露题源编排。

    依次尝试：
      1. 编排标签：【例6】【典例1】【变式5-2】【变式】
      2. 题号前缀："1．" "10．" "3、" "1)"
    只在去掉后还有题干、且剩余不是数字开头时才去（"3．5 元…" 这种小数点不能拆）。
    来源标注（"（2023·河南·期末）"）保留。
    """
    for rx in (_BRACKET_LABEL_RX, _NUM_PREFIX):
        m = rx.match(text or "")
        if not m:
            continue
        rest = text[m.end():]
        if not rest.strip():
            continue
        if rx is _NUM_PREFIX and rest[:1].isdigit():
            continue
        return rest
    return text


def prepared_tokens(no: str):
    """(token 列表, 是否去掉了题面前缀)。返回浅拷贝，不改缓存里的原始 token。"""
    toks = source_tokens(no)
    out = list(toks)
    stripped = 0
    for i, tok in enumerate(out):
        if tok.kind != "text" or not (tok.text or "").strip():
            continue
        new_text = strip_stem_prefix(tok.text)
        if new_text != tok.text:
            out[i] = Token(kind="text", text=new_text)
            stripped = 1
        break
    return out, stripped


def kp_name_map() -> dict:
    try:
        return {kp["kp_id"]: kp["kp"] for kp in all_kps()}
    except Exception:  # noqa: BLE001 - taxonomy 不可用时退化为只印 kp_id
        return {}


# ---------------------------------------------------------------- 文档骨架
def _setup_page(doc, spec: dict = None) -> None:
    spec = spec or layout_spec()
    sec = doc.sections[0]
    sec.page_width = Pt(PAGE_W_PT)
    sec.page_height = Pt(PAGE_H_PT)
    sec.left_margin = sec.right_margin = Pt(spec["margin_pt"])
    sec.top_margin = sec.bottom_margin = Pt(spec["margin_pt"])
    sec.header_distance = Pt(spec["hf_dist_pt"])
    sec.footer_distance = Pt(spec["hf_dist_pt"])


def _setup_header_footer(doc, manifest: dict, spec: dict = None) -> None:
    spec = spec or layout_spec()
    sec = doc.sections[0]
    title = str(manifest.get("title") or "训练卷")
    pid = str(manifest.get("pid") or "")

    head = sec.header.paragraphs[0]
    head.text = ""
    head.paragraph_format.tab_stops.add_tab_stop(Pt(_content_w(spec)), WD_TAB_ALIGNMENT.RIGHT)
    run = head.add_run(title)
    run.bold = True
    run.font.size = Pt(FONT_SIZE_HEADER)
    _set_cn_font(run, "黑体")
    run2 = head.add_run("\t卷号 " + pid)
    run2.font.size = Pt(FONT_SIZE_HEADER - 1)
    _set_cn_font(run2)
    _bottom_border(head, color="999999", sz=4)

    foot = sec.footer.paragraphs[0]
    foot.text = ""
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    def _ftext(s):
        r = foot.add_run(s)
        r.font.size = Pt(FONT_SIZE_FOOTER)
        _gray(r, "777777")
        _set_cn_font(r)
    # 卷面与答题卡各自编号（队长 2026-10-31 定）：卷面只说自己，答题卡页码接着卷面数。
    # 页脚写「卷面 · 第 N 页 / 共 A 页」，card.py 从 manifest["paper"]["pages"] 拿 A。
    _ftext("卷面 · 第 ")
    _add_field(foot, " PAGE ")
    _ftext(" 页 / 共 ")
    _add_field(foot, " NUMPAGES ")
    _ftext(" 页　·　卷号 %s" % pid)


def _add_title_block(doc, manifest: dict, spec: dict = None) -> None:
    spec = spec or layout_spec()
    title = str(manifest.get("title") or "训练卷")
    pid = str(manifest.get("pid") or "")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(spec["title_gap_pt"])
    run = p.add_run(title)
    run.bold = True
    run.font.size = Pt(FONT_SIZE_TITLE)
    _set_cn_font(run, "黑体")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(2)
    r = p.add_run("卷号：%s　　姓名：____________　　班级：____________　　日期：____________"
                  % pid)
    r.font.size = Pt(10.5)
    _set_cn_font(r)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(spec["title_gap_pt"] * 3)
    r = p.add_run("说明：本卷只印题目；答案请写在答题卡上"
                  "（选择题涂黑所选字母，填空／解答写最终答案）。")
    r.font.size = Pt(9)
    _set_cn_font(r)
    _gray(r, "666666")
    _bottom_border(p, color="404040", sz=6)


def _groups(items) -> list:
    """[(qtype, [items])]，按 选择→判断→填空→解答→其他 分组，组内保持 manifest 顺序。"""
    buckets, order = {}, []
    for it in items:
        qtype = str(it.get("qtype") or "其他")
        if qtype not in buckets:
            buckets[qtype] = []
            order.append(qtype)
        buckets[qtype].append(it)
    order.sort(key=lambda q: QTYPE_ORDER.index(q) if q in QTYPE_ORDER else len(QTYPE_ORDER))
    return [(q, buckets[q]) for q in order]


def _iter_with_headings(items):
    """按 manifest 顺序逐题产出 (大题标题 or None, item)。

    正常情况下 picker 已经把同题型排在一起 → 就是"一、选择题 / 二、填空题 / 三、解答题"。
    但 nos 精确题集（训练卷）可能题型交错，这时**不能重排**（卷面题号必须等于 manifest.idx，
    答题卡按 idx 判卷），只在题型切换处补标题，第二次出现同一题型标"（续）"。
    """
    counts, order = {}, []
    for it in items:
        qt = str(it.get("qtype") or "其他")
        if qt not in counts:
            counts[qt] = 0
            order.append(qt)
        counts[qt] += 1
    order.sort(key=lambda q: QTYPE_ORDER.index(q) if q in QTYPE_ORDER else len(QTYPE_ORDER))
    number = {q: i + 1 for i, q in enumerate(order)}

    seen, prev = set(), None
    for it in items:
        qt = str(it.get("qtype") or "其他")
        heading = None
        if qt != prev:
            if qt not in seen:
                heading = _group_title(number[qt], qt, counts[qt])
                seen.add(qt)
            else:
                heading = _group_title_continued(number[qt], qt)
            prev = qt
        yield heading, it


def _group_title(index: int, qtype: str, n: int) -> str:
    cn = _CN_NUM[index - 1] if 1 <= index <= len(_CN_NUM) else str(index)
    label = _GROUP_LABEL.get(qtype, qtype + "题")
    return "%s、%s（共 %d 题）" % (cn, label, n)


def _group_title_continued(index: int, qtype: str) -> str:
    cn = _CN_NUM[index - 1] if 1 <= index <= len(_CN_NUM) else str(index)
    label = _GROUP_LABEL.get(qtype, qtype + "题")
    return "%s、%s（续）" % (cn, label)


def _add_group_heading(doc, text: str, spec: dict = None) -> None:
    spec = spec or layout_spec()
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(spec["group_before_pt"])
    p.paragraph_format.space_after = Pt(spec["group_after_pt"])
    if spec["group_line"]:
        p.paragraph_format.line_spacing = spec["group_line"]
    p.paragraph_format.keep_with_next = True
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(FONT_SIZE_GROUP)
    _set_cn_font(run, "黑体")


# ---------------------------------------------------------------- 一题一框
def _new_registry(doc):
    """跨题共享的媒体注册表：同一份 WMF 只进 docx 一次（同名 part 重复会坏包）。"""
    registry_cls = getattr(compose, "_MediaRegistry", None)
    if registry_cls is None:
        raise PaperError("compose._MediaRegistry 不见了：媒体去重依赖它，"
                         "请确认 compose.py 没被改坏")
    return registry_cls(doc)


def _set_cell_margins(cell, spec: dict) -> None:
    """题框内边距（compact 左右 0.15cm；上下按版面参数，normal 不设、用 Word 默认）。"""
    if spec.get("cell_mar_x_pt") is None:
        return
    tcPr = cell._tc.get_or_add_tcPr()
    mar = tcPr.find(qn("w:tcMar"))
    if mar is None:
        mar = OxmlElement("w:tcMar")
        tcPr.append(mar)
    for side, value in (("top", spec["cell_mar_y_pt"] or 0),
                        ("left", spec["cell_mar_x_pt"]),
                        ("bottom", spec["cell_mar_y_pt"] or 0),
                        ("right", spec["cell_mar_x_pt"])):
        el = mar.find(qn("w:" + side))
        if el is None:
            el = OxmlElement("w:" + side)
            mar.append(el)
        el.set(qn("w:w"), str(int(round(value * 20))))   # twips
        el.set(qn("w:type"), "dxa")


def _box_table(doc, spec: dict = None):
    spec = spec or layout_spec()
    width = _content_w(spec)
    table = doc.add_table(rows=1, cols=1)
    try:
        table.style = TABLE_STYLE
    except KeyError:
        _table_borders(table)
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.columns[0].width = Pt(width)
    table.cell(0, 0).width = Pt(width)
    _set_cell_margins(table.cell(0, 0), spec)
    tr = table.rows[0]._tr
    trPr = tr.get_or_add_trPr()
    trPr.append(OxmlElement("w:cantSplit"))   # 一题一框：行不跨页拆开
    return table


def _prepend_bold_number(par, text: str) -> None:
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rPr.append(OxmlElement("w:b"))
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(FONT_SIZE_STEM * 2)))
    rPr.append(sz)
    szcs = OxmlElement("w:szCs")
    szcs.set(qn("w:val"), str(int(FONT_SIZE_STEM * 2)))
    rPr.append(szcs)
    run.append(rPr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run.append(t)
    p = par._p
    pPr = p.find(qn("w:pPr"))
    if pPr is not None:
        pPr.addnext(run)
    else:
        p.insert(0, run)


def _fill_cell(doc, registry, cell, tokens) -> None:
    """把一题（或一个选项）的 token 印进某个单元格：先在 body 上排，再整块搬进单元格。"""
    tc = cell._tc
    body = doc.element.body
    for el in list(tc):
        tc.remove(el)
    # compose.add_tokens 往 body 末尾（sectPr 之前）追加段落/表格；按「非 sectPr 子元素」的
    # 个数切出新元素。不能用 id(el) 做集合：lxml 的代理对象 id 会被回收复用。
    before_k = sum(1 for el in body.iterchildren() if _qn_local(el) != "sectPr")
    compose.add_tokens(doc, tokens, registry=registry, empty_after_paras=0)
    non_sect = [el for el in body.iterchildren() if _qn_local(el) != "sectPr"]
    new_els = non_sect[before_k:]
    if not new_els:                     # 理论上不会发生（token 非空）
        new_els = [doc.add_paragraph()._p]
    for el in new_els:                  # lxml 移动语义：从 body 摘下来塞进单元格
        tc.append(el)
    last = [el for el in tc.iterchildren() if isinstance(el.tag, str)][-1]
    if _qn_local(last) != "p":          # 单元格必须以段落收尾（OOXML 要求）
        tc.append(doc.add_paragraph()._p)


# ---------------------------------------------------------------- 选项对齐
_OPT_PREV_OK = " \t\u3000\n\x00（(：:；;、"


def _units_of(tokens):
    """token 流 → [(token_index, char_index|None)]，用于按字符位置切片。"""
    units = []
    for ti, tok in enumerate(tokens):
        if tok.kind == "text":
            units.extend((ti, ci) for ci in range(len(tok.text)))
        else:
            units.append((ti, None))
    return units


def _tokens_from_units(units, tokens):
    out, cur = [], None
    for ti, ci in units:
        tok = tokens[ti]
        if ci is None:
            out.append(tok)
            cur = None
            continue
        if cur == ti and out and out[-1].kind == "text":
            out[-1] = Token(kind="text", text=out[-1].text + tok.text[ci])
        else:
            out.append(Token(kind="text", text=tok.text[ci]))
            cur = ti
    return out


def _trim(tokens):
    toks = list(tokens)
    while toks and toks[0].kind == "text" and not toks[0].text.strip():
        toks.pop(0)
    while toks and toks[-1].kind == "text" and not toks[-1].text.strip():
        toks.pop()
    if toks and toks[0].kind == "text":
        toks[0] = Token(kind="text", text=toks[0].text.lstrip())
    if toks and toks[-1].kind == "text":
        toks[-1] = Token(kind="text", text=toks[-1].text.rstrip())
    return toks


def split_options(tokens):
    """把题面里的 A．/B．/C．/D． 拆出来 → (题干 tokens, [选项 tokens, ...])。

    拆不出来（没有选项标签、编号不连续）就原样返回 (tokens, [])。
    """
    units = _units_of(tokens)
    chars = []
    for ti, ci in units:
        chars.append(tokens[ti].text[ci] if ci is not None else "\x00")
    s = "".join(chars)
    pos = {}
    for m in re.finditer(r"[A-D][．.]", s):
        letter = m.group(0)[0]
        if letter in pos:             # 同一字母第二次出现 → 是选项内容，不是标签
            continue
        prev = s[m.start() - 1] if m.start() else "\n"
        if prev not in _OPT_PREV_OK:
            continue
        pos[letter] = m.start()
    order = [l for l in "ABCD" if l in pos]
    if len(order) < 2 or order != list("ABCD"[:len(order)]):
        return tokens, []
    starts = [pos[l] for l in order]
    if starts != sorted(starts):
        return tokens, []
    stem = _trim(_tokens_from_units(units[:starts[0]], tokens))
    opts = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(units)
        opts.append(_trim(_tokens_from_units(units[st:en], tokens)))
    return stem, opts


def _segment_width(seg) -> float:
    """粗估一段选项排出来有多宽（pt）：中文按字号宽、西文按 0.55 字号、公式/图按实测宽。"""
    width = 0.0
    for tok in seg:
        if tok.kind == "text":
            for ch in tok.text or "":
                width += FONT_SIZE_STEM if ord(ch) > 127 else FONT_SIZE_STEM * 0.55
        elif tok.kind == "embed":
            width += float(tok.width_pt or 0.0)
        elif tok.kind == "math":
            width += FONT_SIZE_STEM * 2.2
        elif tok.kind == "table":
            return 1e6                      # 选项里带表格：不排网格
    return width


def _options_grid(opts, content_w: float = None):
    """选项排成几列几行 → (cols, rows)；放不下返回 None（保持题面原排法）。

    先试「一个选项一列」（4 个选项 = 4 列，A/B/C/D 严格竖向对齐）；选项太长退到 2 列
    （A/B 一行、C/D 一行）；图太宽（超过两列宽）才放弃网格、保持原排法。
    """
    n = len(opts)
    if n < 2:
        return None
    widest = max(_segment_width(seg) for seg in opts)
    for cols in (n, 2, 1):
        if cols > n:
            continue
        col_w = ((content_w or CONTENT_W_PT) - 14) / cols
        if widest <= col_w * 0.95:
            return cols, (n + cols - 1) // cols
    return None


def _tight_cells(table) -> None:
    tblPr = table._tbl.tblPr
    for old in tblPr.findall(qn("w:tblCellMar")):
        tblPr.remove(old)
    mar = OxmlElement("w:tblCellMar")
    for side, width in (("top", "0"), ("left", "40"), ("bottom", "0"), ("right", "40")):
        el = OxmlElement("w:" + side)
        el.set(qn("w:w"), width)
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    _insert_before_any(tblPr, mar, {"tblLook", "tblCaption", "tblDescription", "tblPrChange"})


def _add_options_table(doc, registry, cell, opts, cols: int, rows: int) -> None:
    """选项按等宽网格排：A/B/C/D 起点严格对齐，选项再长也只在自己列内换行。"""
    table = cell.add_table(rows=rows, cols=cols)
    table.autofit = False
    width = (CONTENT_W_PT - 14) / cols
    for j in range(cols):
        table.columns[j].width = Pt(width)
    for r in range(rows):
        for j in range(cols):
            table.cell(r, j).width = Pt(width)
    _tight_cells(table)
    for i, seg in enumerate(opts):
        _fill_cell(doc, registry, table.cell(i // cols, i % cols), seg)
    _drop_trailing_empty_paragraph(cell)   # python-docx 会在表格后补空段落，占位又难看


def _drop_trailing_empty_paragraph(cell) -> None:
    kids = [el for el in cell._tc.iterchildren() if isinstance(el.tag, str)]
    if not kids or _qn_local(kids[-1]) != "p":
        return
    text = "".join(el.text or "" for el in kids[-1].iter()
                   if isinstance(el.tag, str) and _qn_local(el) == "t")
    if not text.strip():
        cell._tc.remove(kids[-1])


def _tighten_cell(cell, spec: dict) -> None:
    """把单元格里所有段落的行距/段前后压到版面参数（compact：1.0 行距 / 1pt 段距）。"""
    if spec.get("line_spacing") is None:
        return
    from docx.text.paragraph import Paragraph

    line, space = spec["line_spacing"], spec["space_pt"]
    for el in cell._tc.iter(qn("w:p")):
        fmt = Paragraph(el, cell).paragraph_format
        fmt.line_spacing = line
        fmt.space_before = Pt(space)
        fmt.space_after = Pt(space)


def _add_item_box(doc, registry, item: dict, tokens, kp_names: dict, space_paras: int,
                  spec: dict = None) -> None:
    spec = spec or layout_spec()
    table = _box_table(doc, spec)
    cell = table.cell(0, 0)

    stem, opts = split_options(tokens)
    grid = _options_grid(opts, _content_w(spec))
    if grid:
        _fill_cell(doc, registry, cell, stem)
        _add_options_table(doc, registry, cell, opts, *grid)
    else:                           # 选项里是超宽图：整段照原样印（图会自己撑开）
        _fill_cell(doc, registry, cell, tokens)

    first = next((p for p in cell.paragraphs if (p.text or "").strip()), cell.paragraphs[0])
    _prepend_bold_number(first, "%d．" % int(item.get("idx") or 0))
    _tighten_cell(cell, spec)

    kp_id = str(item.get("kp_id") or "")
    kp_name = kp_names.get(kp_id, "")
    note = ("（%s %s）" % (kp_id, kp_name)).replace(" ）", "）") if kp_id else ""
    if note:
        p = cell.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(spec["kp_space_pt"] if spec["kp_space_pt"] is not None else 2)
        if spec.get("kp_line_pt"):
            p.paragraph_format.line_spacing = Pt(spec["kp_line_pt"])
        r = p.add_run(note)
        r.font.size = Pt(FONT_SIZE_KP)
        _set_cn_font(r)
        _gray(r, "7090a0")

    for _ in range(max(0, int(space_paras))):
        p = cell.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = Pt(LINE_SPACE_PT)

    kids = [el for el in cell._tc.iterchildren() if isinstance(el.tag, str)]
    if not kids or _qn_local(kids[-1]) != "p":     # 单元格必须以段落收尾（OOXML 要求）
        cell._tc.append(doc.add_paragraph()._p)

    # 题与题之间必须有段落（两张表贴在一起会被 Word 合并成一张），compact 压到 1pt
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_before = Pt(0)
    spacer.paragraph_format.space_after = Pt(0)
    spacer.paragraph_format.line_spacing = Pt(spec["spacer_pt"])


def _space_for(item: dict) -> int:
    try:
        n = int(item.get("space_paras") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    return int(DEFAULT_SPACE.get(str(item.get("qtype") or ""), 0))


def build_docx(manifest: dict, out_dir: str, name: str = "paper", layout: str = None,
               _spec: dict = None) -> dict:
    """只排版（不导 PDF）→ {"docx", "expected", "n_items"}；expected 供 check_docx 用。"""
    items = list(manifest.get("items") or [])
    if not items:
        raise PaperError("manifest 没有题目（items 为空），不印空卷")
    os.makedirs(out_dir, exist_ok=True)

    spec = _spec or layout_spec(layout or (manifest.get("params") or {}).get("layout"))
    doc = compose.new_document(margin_cm=spec["margin_pt"] / 28.35)
    _setup_page(doc, spec)
    _setup_header_footer(doc, manifest, spec)
    _add_title_block(doc, manifest, spec)

    registry = _new_registry(doc)
    kp_names = kp_name_map()
    stripped = 0
    for heading, item in _iter_with_headings(items):
        if heading:
            _add_group_heading(doc, heading, spec)
        no = str(item.get("no") or "")
        if not no:
            raise PaperError("manifest 里有 item 缺 no，没法找原始 docx：idx=%r"
                             % (item.get("idx"),))
        tokens, st = prepared_tokens(no)
        stripped += st
        _add_item_box(doc, registry, item, tokens, kp_names, _space_for(item), spec)

    docx_path = os.path.join(out_dir, name + ".docx")
    doc.save(docx_path)
    expected = expected_counts(manifest)
    expected["stem_prefix_stripped"] = stripped
    expected["media_warnings"] = list(getattr(registry, "warnings", []) or [])
    return {"docx": docx_path, "expected": expected, "n_items": len(items),
            "layout": spec["name"]}


# ---------------------------------------------------------------- 保真计数
def _count_markers(xml_text: str) -> dict:
    counts = {}
    if not xml_text:
        return counts
    try:
        root = etree.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:  # noqa: BLE001 - 单块 XML 坏了就让观测值偏低，闸门自然会报
        return counts
    for el in root.iter():
        name = _qn_local(el)
        if name in _MARKERS:
            counts[name] = counts.get(name, 0) + 1
    return counts


def expected_counts(manifest: dict) -> dict:
    """从原始 docx 重新抽 token，算「卷面应该有多少公式/对象/媒体/题内表格」。"""
    exp = {k: 0 for k in _MARKERS}
    exp.update({"formula_tokens": 0, "embed_tokens": 0, "media_refs": 0,
                "media_hashes": set(), "n_items": 0, "tables_with_refs": 0,
                "fig_ref_missing": 0, "option_tables": 0, "media_warnings": [],
                "space_paras_total": 0})
    for item in manifest.get("items") or []:
        no = str(item.get("no") or "")
        if not no:
            raise PaperError("manifest 里有 item 缺 no：idx=%r" % (item.get("idx"),))
        tokens = source_tokens(no)
        exp["n_items"] += 1
        # 留白段数记进 health：>0 的题会明显变高（排查「大片空白」用）
        exp["space_paras_total"] += max(0, int(item.get("space_paras") or 0))
        has_fig = False
        for tok in tokens:
            if tok.kind == "math":
                exp["formula_tokens"] += 1
            elif tok.kind == "embed":
                exp["embed_tokens"] += 1
            if tok.kind in ("embed", "table"):
                has_fig = True
            for _rid, _path, data in tok.media or []:
                exp["media_refs"] += 1
                exp["media_hashes"].add(hashlib.sha1(data).hexdigest())
            for k, n in _count_markers(tok.xml).items():
                exp[k] += n
        if not has_fig:
            text = "".join(t.text or "" for t in tokens if t.kind == "text")
            if fig_missing(text, False):
                exp["fig_ref_missing"] += 1
        if _option_table_needed(tokens):
            exp["option_tables"] += 1
    return exp


def _option_table_needed(tokens) -> bool:
    """这道题会不会被排成等宽选项表格（与 _add_item_box 用同一套判断）。"""
    _stem, opts = split_options(tokens)
    return _options_grid(opts) is not None


def observed_counts(docx_path: str) -> dict:
    """从印出来的 docx 数实际落地的东西。"""
    if not os.path.exists(docx_path):
        raise PaperError("docx 不存在：%s" % docx_path)
    with zipfile.ZipFile(docx_path) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            raise PaperError("docx 缺 word/document.xml，不是合法 Word 文件：%s" % docx_path)
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        media_hashes = {hashlib.sha1(z.read(n)).hexdigest() for n in media}
    root = etree.fromstring(xml)
    counts = {}
    body_tables = 0
    for el in root.iter():
        name = _qn_local(el)
        if name in _MARKERS:
            counts[name] = counts.get(name, 0) + 1
    body = root.find(qn("w:body"))
    if body is not None:
        body_tables = sum(1 for el in body if _qn_local(el) == "tbl")
    return {"counts": counts, "body_tables": body_tables,
            "media_files": len(media), "media_hashes": media_hashes}


def relationship_metrics(docx_path: str) -> dict:
    """卷面里的关系引用 vs 定义：悬空 rId 会让 Word 判「文件已损坏」，必须为 0。"""
    if not os.path.exists(docx_path):
        raise PaperError("docx 不存在：%s" % docx_path)
    with zipfile.ZipFile(docx_path) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            raise PaperError("docx 缺 word/document.xml，不是合法 Word 文件：%s" % docx_path)
        try:
            rels_xml = z.read("word/_rels/document.xml.rels")
        except KeyError:
            raise PaperError("docx 缺 word/_rels/document.xml.rels（媒体关系无处解析）")
    root = etree.fromstring(xml)
    rels_root = etree.fromstring(rels_xml)
    refs = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("id", "embed", "link"):
            value = el.get("{%s}%s" % (R_NS, attr))
            if value:
                refs.append(value)
    defined = {e.get("Id") for e in rels_root if e.get("Id")}
    return {"rid_refs": len(refs), "rid_unique": len(set(refs)),
            "rels": len(defined), "dangling": sorted(set(refs) - defined)}


def check_docx(docx_path: str, manifest: dict, expected: dict = None) -> dict:
    """保真体检：期望值（原始 docx）vs 观测值（印出来的 docx），对不上抛 FidelityError。"""
    exp = expected if expected is not None else expected_counts(manifest)
    obs = observed_counts(docx_path)
    counts = obs["counts"]
    rel_metrics = relationship_metrics(docx_path)

    if rel_metrics["dangling"]:
        raise FidelityError(
            "卷面里有 %d 个悬空媒体引用（%s）—— Word 会判「文件已损坏」，整份卷子打不开"
            % (len(rel_metrics["dangling"]), ", ".join(rel_metrics["dangling"][:8])),
            code="dangling_relationship")

    if obs["body_tables"] != exp["n_items"]:
        raise FidelityError("题数不一致：manifest %d 题，卷面只有 %d 个题框"
                            % (exp["n_items"], obs["body_tables"]))

    got_math = counts.get("oMath", 0)
    if got_math != exp["oMath"]:
        raise FidelityError(
            "公式数不一致（OMML m:oMath）：原始题面 %d 个，卷面 %d 个 —— 排版时丢了公式"
            % (exp["oMath"], got_math))

    for kind in ("object", "drawing", "pict"):
        if counts.get(kind, 0) != exp[kind]:
            raise FidelityError(
                "embed 对象数不一致（w:%s）：原始题面 %d 个，卷面 %d 个 —— "
                "MathType-OLE／图片对象没搬回卷面" % (kind, exp[kind], counts.get(kind, 0)))

    missing = sorted(exp["media_hashes"] - obs["media_hashes"])
    if missing:
        raise FidelityError(
            "媒体丢失：%d 张图片/公式预览图没进 docx（sha1 前 8 位：%s）"
            % (len(missing), ", ".join(h[:8] for h in missing[:5])))

    content_tables = (counts.get("tbl", 0) - obs["body_tables"]
                      - exp.get("option_tables", 0))
    if content_tables != exp["tbl"]:
        raise FidelityError("题内表格数不一致：原始题面 %d 个，卷面 %d 个"
                            % (exp["tbl"], content_tables))

    warnings = list(exp.get("media_warnings") or [])
    if exp["media_hashes"] and obs["media_files"] > len(exp["media_hashes"]):
        warnings.append("docx 里媒体文件 %d 个多于去重后的 %d 个（同图重复注册）"
                        % (obs["media_files"], len(exp["media_hashes"])))
    if exp.get("fig_ref_missing"):
        warnings.append("%d 道题的题干写着「如图」但原始 docx 里没有任何图（语料缺失，"
                        "照实排版；要排除这类题需 picker 加过滤）" % exp["fig_ref_missing"])

    return {
        "pages": 0,
        "n_items": exp["n_items"],
        "n_items_observed": obs["body_tables"],
        "items_ok": exp["n_items"] == obs["body_tables"],
        "formula_tokens": exp["formula_tokens"],
        "formula_omml_expected": exp["oMath"],
        "formula_omml_observed": got_math,
        "embed_tokens": exp["embed_tokens"],
        "embed_containers_expected": {k: exp[k] for k in ("object", "drawing", "pict")},
        "embed_containers_observed": {k: counts.get(k, 0) for k in ("object", "drawing", "pict")},
        "media_refs": exp["media_refs"],
        "media_count": obs["media_files"],
        "media_expected_unique": len(exp["media_hashes"]),
        "media_missing": missing,
        "tables_expected": exp["tbl"],
        "tables_observed": content_tables,
        "option_tables": exp.get("option_tables", 0),
        "fig_ref_missing": exp.get("fig_ref_missing", 0),
        "space_paras_total": exp.get("space_paras_total", 0),
        "stem_prefix_stripped": exp.get("stem_prefix_stripped", 0),
        "media_warnings": list(exp.get("media_warnings") or []),
        "rid_refs": rel_metrics["rid_refs"],
        "rid_unique": rel_metrics["rid_unique"],
        "rels": rel_metrics["rels"],
        "dangling": rel_metrics["dangling"],
        "docx": os.path.abspath(docx_path),
        "docx_bytes": os.path.getsize(docx_path),
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 打印校准 / 尺寸自检
def pdf_page_sizes(pdf_path: str) -> list:
    """PDF 每页尺寸（pt）：[(w, h), ...]。读不了直接抛 PaperError。"""
    import pymupdf

    if not os.path.exists(pdf_path):
        raise PaperError("PDF 不存在：%s" % pdf_path)
    doc = pymupdf.open(pdf_path)
    try:
        return [(round(p.rect.width, 2), round(p.rect.height, 2)) for p in doc]
    finally:
        doc.close()


def check_pdf_a4(pdf_path: str, tol: float = A4_TOL_PT) -> list:
    """每页都必须是 A4（595.28 × 841.89pt，±tol）→ 否则 FidelityError(code="pdf_not_a4")。

    为什么卡这么死：答题卡的二维码/Aruco 四角坐标全是按 A4 的 pt 坐标写死的，
    页面尺寸一被改（Letter / 被缩放的 PDF），判卷就对着错位的坐标找标记，必错。
    """
    sizes = pdf_page_sizes(pdf_path)
    bad = [(i, w, h) for i, (w, h) in enumerate(sizes, 1)
           if abs(w - A4_W_PT) > tol or abs(h - A4_H_PT) > tol]
    if bad:
        detail = "; ".join("第 %d 页 %.2f×%.2fpt" % b for b in bad)
        raise FidelityError(
            "PDF 页面不是 A4（pdf_not_a4）：期望 %.2f×%.2fpt ±%.1fpt，实际 %s —— "
            "打印会整体错位，判卷坐标会失效" % (A4_W_PT, A4_H_PT, tol, detail),
            code="pdf_not_a4")
    return sizes


def _draw_mixed(page, x: float, y: float, text: str, size: float = 10,
                color=(0.1, 0.1, 0.1)) -> float:
    """一行里中英混排：ASCII 串用 helv（数字不会散开），其余用 china-s。返回结束 x。"""
    import pymupdf

    for piece in re.findall(r"[\x20-\x7e]+|[^\x20-\x7e]+", text):
        font = "helv" if piece.isascii() else "china-s"
        page.insert_text((x, y), piece, fontname=font, fontsize=size, color=color)
        x += pymupdf.get_text_length(piece, fontname=font, fontsize=size)
    return x


def build_calibration_pdf(path: str, *, title: str = "", pid: str = "") -> str:
    """生成一页 A4 校准页：100mm 方框 + 10mm 刻度尺 + A4 外框 + 四角标记。

    用户反馈第 5 条：打印要求 100% 比例太脆 —— 打印这页量一下方框就知道有没有被缩放。
    本页不判卷，只用于校准。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=A4_W_PT, height=A4_H_PT)
    mm = PT_PER_MM
    W, H = A4_W_PT, A4_H_PT
    grey = (0.55, 0.55, 0.55)
    ink = (0.85, 0.1, 0.1)

    # A4 外框（10mm 内缩）：差 1mm 以上就说明被缩放了
    margin = 10 * mm
    page.draw_rect(pymupdf.Rect(margin, margin, W - margin, H - margin),
                   color=grey, width=0.6)
    _draw_mixed(page, margin + 4, margin + 12, "A4 外框：210 × 297 mm", 8, grey)

    # 四角标记（十字）
    arm = 8 * mm
    for cx, cy in ((margin, margin), (W - margin, margin),
                   (margin, H - margin), (W - margin, H - margin)):
        page.draw_line((cx - arm, cy), (cx + arm, cy), color=grey, width=0.8)
        page.draw_line((cx, cy - arm), (cx, cy + arm), color=grey, width=0.8)

    # 标题与说明
    _draw_mixed(page, margin + 4, margin + 40, "打印校准页（本页不判卷，只为校准）", 16)
    if title or pid:
        _draw_mixed(page, margin + 4, margin + 58,
                    ("%s  卷号 %s" % (title, pid)).strip(), 9, grey)

    # 100mm 方框（实线方框；边框本身粗 1.5pt，量外沿）
    side = CALIBRATION_MM * mm
    x0 = (W - side) / 2
    y0 = 120
    page.draw_rect(pymupdf.Rect(x0, y0, x0 + side, y0 + side),
                   color=ink, width=1.5)

    # 上边 10mm 刻度尺
    tick_y = y0 - 2 * mm
    page.draw_line((x0, tick_y), (x0 + side, tick_y), color=(0.2, 0.2, 0.2), width=0.7)
    n = int(CALIBRATION_MM // CALIBRATION_RULER_MM)
    for i in range(n + 1):
        tx = x0 + i * CALIBRATION_RULER_MM * mm
        long_tick = (i % 5 == 0)
        page.draw_line((tx, tick_y - (3 * mm if long_tick else 1.6 * mm)),
                       (tx, tick_y), color=(0.2, 0.2, 0.2), width=0.7)
        if long_tick:
            _draw_mixed(page, tx - 6, tick_y - 4 * mm, str(i * int(CALIBRATION_RULER_MM)),
                        7, (0.2, 0.2, 0.2))
    _draw_mixed(page, x0 + side + 6, tick_y - 2 * mm, "mm 刻度（每格 10mm）", 7,
                (0.2, 0.2, 0.2))

    # 左边 10mm 刻度尺
    tick_x = x0 - 2 * mm
    page.draw_line((tick_x, y0), (tick_x, y0 + side), color=(0.2, 0.2, 0.2), width=0.7)
    for i in range(n + 1):
        ty = y0 + i * CALIBRATION_RULER_MM * mm
        long_tick = (i % 5 == 0)
        page.draw_line((tick_x - (3 * mm if long_tick else 1.6 * mm), ty),
                       (tick_x, ty), color=(0.2, 0.2, 0.2), width=0.7)

    # 方框内文字（纯 ASCII → 全部走 helv，宽度自己算，居中）
    box_text = "100 mm × 100 mm"
    tw = pymupdf.get_text_length(box_text, fontname="helv", fontsize=16)
    page.insert_text((x0 + (side - tw) / 2, y0 + side / 2),
                     box_text, fontname="helv", fontsize=16, color=ink)

    # 使用说明
    lines = [
        "打印后量一下：这个红色方框的四边必须都是 100 mm（用尺子量外沿）。",
        "不是 100 mm → 说明打印时被缩放了。请在打印设置里选「实际大小 / 100%」，",
        "不要选「适合页面 / 缩放以适合」。",
        "",
        "A4 外框量出来差 1 mm 以上，也说明被缩放。",
        "刻度尺每格 10 mm，共 100 mm，可直接对照。",
        "",
        "本页不判卷，只为校准；在组卷页取消勾选「附校准页」即可不打印这一页。",
    ]
    ty = y0 + side + 30
    for line in lines:
        if line:
            _draw_mixed(page, margin + 4, ty, line, 10)
        ty += 18

    doc.save(path, garbage=3, deflate=True)
    doc.close()
    return path


def prepend_pdf(front_pdf: str, target_pdf: str) -> str:
    """把 front_pdf 的页面插到 target_pdf 最前面（先写 .tmp 再原子替换）。"""
    import pymupdf

    merged = pymupdf.open()
    tmp = target_pdf + ".tmp"
    try:
        for src_path in (front_pdf, target_pdf):
            src = pymupdf.open(src_path)
            try:
                merged.insert_pdf(src)
            finally:
                src.close()
        merged.save(tmp, garbage=3, deflate=True)
    finally:
        merged.close()
    os.replace(tmp, target_pdf)
    return target_pdf


# ---------------------------------------------------------------- 导 PDF / PNG
_render_mod = "unset"


def _render_module():
    """T6 的 render.py 落地后优先用它的 docx_to_pdf/pdf_to_png。"""
    global _render_mod
    if _render_mod == "unset":
        try:
            import render as _r  # type: ignore
            _render_mod = _r if hasattr(_r, "docx_to_pdf") else None
        except ImportError:
            _render_mod = None
    return _render_mod


_word_state = None


def word_available() -> bool:
    """本机能不能用 Word COM 导 PDF（结果缓存）。"""
    global _word_state
    if _word_state is None:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                app = win32com.client.DispatchEx("Word.Application")
                try:
                    app.Quit()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                pythoncom.CoUninitialize()
            _word_state = True
        except Exception:  # noqa: BLE001
            _word_state = False
    return bool(_word_state)


def word_can_open(docx_path: str):
    """Word 真的能打开这份 docx 吗 → (ok, detail, pages)。

    这是「文件能不能用」的最终裁判：XML 合法、rels 齐全都不代表 Word 会认（VML 尺寸、OLE 结构、
    rId 悬空都可能被判「文件已损坏」）。只读打开、不保存，随即关闭。
    """
    if not word_available():
        return False, "本机没有可用的 Word（无法做打开校验）", 0
    if not os.path.exists(docx_path):
        return False, "docx 不存在：%s" % docx_path, 0
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    app = None
    doc = None
    try:
        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(os.path.abspath(docx_path), ReadOnly=True,
                                 AddToRecentFiles=False)
        try:
            pages = int(doc.ComputeStatistics(2))
        except Exception:  # noqa: BLE001
            pages = 0
        return True, "", pages
    except Exception as exc:  # noqa: BLE001 - COM 报错文本就是给用户看的
        return False, str(exc), 0
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:  # noqa: BLE001
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()


def word_check_docx(docx_path: str) -> int:
    """硬闸门：Word 打不开就 FidelityError(code="word_cannot_open")；返回页数。"""
    ok, detail, pages = word_can_open(docx_path)
    if not ok:
        raise FidelityError("Word 打不开这份 docx（word_cannot_open）：%s" % detail,
                            code="word_cannot_open")
    return pages


def _com_docx_to_pdf(docx_path: str, pdf_path: str):
    """Word COM 导出 PDF → (pages, seconds)。原始 docx 只读打开，绝不改源文件。"""
    import pythoncom
    import win32com.client

    if not word_available():
        raise PaperError("本机没有可用的 Word（win32com DispatchEx 失败），"
                         "装 Word 或等 render.py 提供别的 PDF 后端")
    t0 = time.time()
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx("Word.Application")
    doc = None
    try:
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(os.path.abspath(docx_path), ReadOnly=True,
                                 AddToRecentFiles=False)
        doc.SaveAs(os.path.abspath(pdf_path), FileFormat=17)   # wdFormatPDF
        try:
            pages = int(doc.ComputeStatistics(2))              # wdStatisticPages
        except Exception:  # noqa: BLE001
            pages = 0
    except Exception as exc:  # noqa: BLE001
        raise PaperError("Word 导出 PDF 失败：%s" % exc)
    finally:
        if doc is not None:
            try:
                doc.Close(SaveChanges=0)
            except Exception:  # noqa: BLE001
                pass
        try:
            app.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 1000:
        raise PaperError("Word 没产出 PDF：%s" % pdf_path)
    return pages, time.time() - t0


def _warn(warnings, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


#: render 的「回退无效」错误：同一套 Word、同一把锁，回退只会把等待时间翻倍并掩盖真实原因。
_NON_FALLBACK_HINTS = ("占用", "写权限", "被别的程序", "PermissionError", "Permission denied")


def _can_fall_back(exc: Exception) -> bool:
    """这个异常值不值得回退内置实现？

    不值得的两类：
      1) ``WordUnavailable``（reason ∈ not_installed / pywin32_missing / dispatch_failed /
         com_failed / timeout）—— 内置分支用的是同一套 Word COM，必然同样失败；timeout 刚强杀过
         Word，立刻再起一次更容易二次卡死。
      2) 目标 PDF 被占用 / 没写权限 —— 复制到新文件后落到同一把锁上，用户要再等一遍。
    其余（渲染逻辑、跨盘落位、pymupdf 抽风…）都值得用自带实现再试一次。
    """
    if type(exc).__name__ == "WordUnavailable":
        return False
    text = str(exc)
    return not any(hint in text for hint in _NON_FALLBACK_HINTS)


def docx_to_pdf(docx_path: str, pdf_path: str, warnings: list = None):
    """→ (pages, seconds)。优先 render.py；它抛任何异常都回退内置 Word COM 并记 warning。

    为什么不能让它把整条链带崩：render.py 是独立模块（跨盘落位、Word 杀进程、看门狗都可能出
    问题），而组卷/出卷是主路径。回退之后卷子照样能出，问题以 health.warnings 的形式上报。
    """
    mod = _render_module()
    if mod is not None:
        try:
            pages, secs = mod.docx_to_pdf(docx_path, pdf_path)
            if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000:
                return int(pages), float(secs)
            raise PaperError("render.docx_to_pdf 没产出有效 PDF")
        except Exception as exc:  # noqa: BLE001 - 分流：有的回退没意义
            if not _can_fall_back(exc):
                raise                           # 没装 Word / 目标被占用：直接上抛，别白等一轮
            _warn(warnings, "render.docx_to_pdf 失败（%s: %s），已回退内置 Word COM"
                  % (type(exc).__name__, exc))
            if os.path.exists(pdf_path):        # 清掉半截产物，免得内置实现写到坏文件上
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
    return _com_docx_to_pdf(docx_path, pdf_path)


def _builtin_pdf_to_png(pdf_path: str, out_dir: str, dpi: int = DPI,
                        prefix: str = "paper") -> list:
    """内置实现（PyMuPDF）：A4@300dpi 归一成 2480×3508。"""
    import pymupdf
    from PIL import Image

    os.makedirs(out_dir, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    out = []
    try:
        for i, page in enumerate(doc, 1):
            pix = page.get_pixmap(dpi=dpi)
            path = os.path.join(out_dir, "%s_%d.png" % (prefix, i))
            pix.save(path)
            if dpi == DPI:
                with Image.open(path) as im:
                    if im.size != PAGE_PX:
                        im.convert("RGB").resize(PAGE_PX, Image.LANCZOS).save(path)
            out.append(path)
    finally:
        doc.close()
    return out


def pdf_to_png(pdf_path: str, out_dir: str, dpi: int = DPI, prefix: str = "paper",
               warnings: list = None) -> list:
    """PDF → 每页一张 PNG（默认 300dpi，A4 = 2480×3508）。

    PyMuPDF 按页面 pt 尺寸 × dpi 取整（A4 595.3pt → 2481px），内置实现统一归一成 CONTRACT §3
    的规范像素 2480×3508，保证前端/判卷侧拿到的每一页尺寸稳定。
    render.pdf_to_png 抛异常时同样回退内置实现并记 warning。
    """
    mod = _render_module()
    if mod is not None and hasattr(mod, "pdf_to_png"):
        try:
            pngs = list(mod.pdf_to_png(pdf_path, out_dir, dpi=dpi))
            if pngs and all(os.path.exists(p) for p in pngs):
                return pngs
            raise PaperError("render.pdf_to_png 没产出 PNG")
        except Exception as exc:  # noqa: BLE001 - 分流：有的回退没意义
            if not _can_fall_back(exc):
                raise
            _warn(warnings, "render.pdf_to_png 失败（%s: %s），已回退内置 PyMuPDF"
                  % (type(exc).__name__, exc))
    return _builtin_pdf_to_png(pdf_path, out_dir, dpi=dpi, prefix=prefix)


def ink_ratio(png_path: str, threshold: int = 200) -> float:
    """墨迹占比（灰度 < threshold 的像素比例）。空白页 ≈ 0。"""
    import numpy as np
    from PIL import Image

    with Image.open(png_path) as im:
        arr = np.asarray(im.convert("L"), dtype=np.uint8)
    return float((arr < threshold).mean())


def _png_size(png_path: str):
    from PIL import Image

    with Image.open(png_path) as im:
        return im.size


# ---------------------------------------------------------------- 主入口
def render_paper(manifest: dict, out_dir: str, *, name: str = "paper", dpi: int = DPI,
                 deep_check: bool = False, layout: str = None,
                 calibration: bool = None) -> dict:
    """排版 → PDF → PNG，并跑保真 + 分页体检。返回 {"docx","pdf","pages","png","health"}。

    ``deep_check=True``（出卷/CLI 默认走这条）会把 docx 交给 Word 真的打开一次：
    XML 合法、关系齐全都不代表 Word 认这份文件，打不开就 ``FidelityError(code="word_cannot_open")``。
    预览路径可以关掉，省掉一次 Word 往返。

    ``calibration``（默认取 manifest.params.calibration，缺省 True）：在卷面 PDF 前插一页
    100mm 校准页（打印时量一下就知道有没有被缩放）。卷面页数仍按内容页算（manifest.paper.pages），
    答题卡页码接着内容页数走，不受校准页影响。
    """
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    spec = layout_spec(layout or (manifest.get("params") or {}).get("layout"))
    attempts = []

    def _render_once(spec_now: dict) -> dict:
        built = build_docx(manifest, out_dir, name=name, _spec=dict(spec_now, name=spec["name"]))
        health = check_docx(built["docx"], manifest, built["expected"])
        health["layout"] = built["layout"]
        pdf_path = os.path.join(out_dir, name + ".pdf")
        pages, convert_s = docx_to_pdf(built["docx"], pdf_path, warnings=health["warnings"])
        pngs = pdf_to_png(pdf_path, out_dir, dpi=dpi, prefix=name, warnings=health["warnings"])
        if not pngs:
            raise PaperError("PDF 没渲染出任何页面：%s" % pdf_path)
        if pages and pages != len(pngs):
            raise FidelityError("PDF 页数不一致：Word 报 %d 页，实际渲染 %d 页"
                                % (pages, len(pngs)))
        return {"built": built, "health": health, "pdf": pdf_path, "png": pngs,
                "pages": len(pngs), "ink": [ink_ratio(p) for p in pngs],
                "convert_s": convert_s, "spec": spec_now}

    first = _render_once(spec)
    attempts.append({"pass": "compact", "pages": first["pages"],
                     "last_ink": round(first["ink"][-1], 4)})
    chosen = first
    # 调平：末页几乎空白 → 再压一档重排一次，取「页数更少 / 末页更满」的那次
    if needs_balance(first["pages"], first["ink"]):
        second = _render_once(_tighten_spec(spec))
        attempts.append({"pass": "tight", "pages": second["pages"],
                         "last_ink": round(second["ink"][-1], 4)})
        if (second["pages"] < first["pages"]
                or (second["pages"] == first["pages"]
                    and second["ink"][-1] > first["ink"][-1])):
            chosen = second
        else:                               # 更差不接受：把第一次的产物重新落盘
            _render_once(dict(first["spec"], name=spec["name"]))
            chosen = first

    built, health = chosen["built"], chosen["health"]
    health["balance"] = {"attempts": attempts, "tightened": chosen is not first}

    if deep_check:
        if not word_available():
            health["warnings"].append("本机没有 Word：跳过「Word 能打开」校验（导 PDF 也会不可用）")
            health["word_open"] = "skipped"
        else:
            health["word_pages"] = word_check_docx(built["docx"])
            health["word_open"] = "ok"

    pdf_path = chosen["pdf"]
    content_pages = chosen["pages"]
    convert_s = chosen["convert_s"]
    check_pdf_a4(pdf_path)                     # 内容 PDF 必须是 A4（打印/判卷坐标都靠它）

    want_calibration = (manifest.get("params") or {}).get("calibration") \
        if calibration is None else calibration
    want_calibration = True if want_calibration is None else bool(want_calibration)
    if want_calibration:
        calib_pdf = os.path.join(out_dir, "_calibration.pdf")
        build_calibration_pdf(calib_pdf, title=str(manifest.get("title") or ""),
                              pid=str(manifest.get("pid") or ""))
        prepend_pdf(calib_pdf, pdf_path)
        pngs = pdf_to_png(pdf_path, out_dir, dpi=dpi, prefix=name, warnings=health["warnings"])
        ink = [ink_ratio(p) for p in pngs]
        check_pdf_a4(pdf_path)                 # 合并后再验一次（防合并把尺寸搞坏）
    else:
        pngs = chosen["png"]
        ink = chosen["ink"]
    size = _png_size(pngs[0])
    health.update({
        "pages": len(pngs),
        "paper_pages": content_pages,
        "calibration": want_calibration,
        "page_size_pt": [round(A4_W_PT, 2), round(A4_H_PT, 2)],
        "pdf": os.path.abspath(pdf_path),
        "pdf_bytes": os.path.getsize(pdf_path),
        "png": list(pngs),
        "dpi": dpi,
        "page_size_px": list(size),
        "ink_ratio": ink,
        "ink_min": min(ink),
        "ink_max": max(ink),
        "convert_s": round(convert_s, 2),
        "elapsed_s": round(time.time() - t0, 2),
    })
    _gate_pages(health, content_pages=content_pages)
    # 卷面页数写回 manifest：card.py 用它算答题卡起始页码（各自编号，不合并页脚）
    manifest["paper"] = {"pages": content_pages, "dpi": dpi, "w_px": size[0], "h_px": size[1],
                         "calibration": want_calibration}
    return {"docx": built["docx"], "pdf": pdf_path, "pages": len(pngs),
            "png": list(pngs), "health": health}


def _gate_pages(health: dict, content_pages: int = None) -> None:
    """分页体检：页数>0、A4@300dpi、没有空白页。

    **校准页是工具页，不参与填充率闸门**（它本来就只画一个 100mm 方框 + 说明，墨迹 ~2% 是正常的；
    判定「末页是否几乎空白」时也要把这一页摘出去，否则会把校准页当成末页误判）。
    """
    ink = list(health.get("ink_ratio") or [])
    if health.get("calibration") and len(ink) > 1:
        ink = ink[1:]                      # 去掉第 1 页校准页，只看内容页
    pages = content_pages or len(ink)
    if pages <= 0:
        raise FidelityError("页数为 0：PDF 渲染失败")
    if health.get("dpi") == DPI and tuple(health.get("page_size_px") or ()) != PAGE_PX:
        raise FidelityError("页面不是 A4@300dpi：实测 %r，期望 %r"
                            % (health.get("page_size_px"), PAGE_PX))
    for i, ratio in enumerate(ink, 1):
        if ratio >= _BLANK_INK:
            continue
        if i == pages and pages > 1:
            health["warnings"].append(
                "末页几乎空白（第 %d 页 ink=%.6f）：Word 把刚好满页的内容挤到了新页" % (i, ratio))
            continue
        raise FidelityError("第 %d 页几乎空白（ink_ratio=%.6f）—— 排版出现断层" % (i, ratio))


# ---------------------------------------------------------------- 自检 CLI
def _main(argv=None):
    import argparse
    import json
    import sqlite3
    import sys

    ap = argparse.ArgumentParser(description="出卷：picker 抽题 → paper 排版 → PDF/PNG")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "data", "app.db"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "out"))
    ap.add_argument("--sizes", default="10,24", help="出几套卷，逗号分隔的题数（默认 10,24）")
    ap.add_argument("--name", default="", help="产物目录名（只出一套时用；默认 paper{题数}）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mix", default="",
                    help="显式配比，如 \"选择:5,填空:3,解答:2\"；多套用 ; 分隔，与 --sizes 一一对应")
    ap.add_argument("--flat", action="store_true",
                    help="产物直接放在 --out 下（交付卷用：out/delivery/paper24.pdf）")
    ap.add_argument("--summary", default="", help="把每套卷的 pid/题号/页数/health 写成 JSON")
    ap.add_argument("--no-calibration", action="store_true",
                    help="不附 100mm 校准页（默认附：打印前量一下就知道有没有被缩放）")
    ap.add_argument("--layout", default=DEFAULT_LAYOUT, choices=list(LAYOUTS),
                    help="版式：compact 省纸（默认）/ normal 宽松")
    ap.add_argument("--no-deep-check", action="store_true",
                    help="跳过 Word 真打开校验（预览用；交付卷不要加）")
    args = ap.parse_args(argv)

    import picker

    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    report = []
    try:
        sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
        explicit = [m.strip() for m in args.mix.split(";") if m.strip()] if args.mix else []

        def _parse_mix(text):
            mix = {}
            for piece in text.split(","):
                if not piece.strip():
                    continue
                key, _, value = piece.partition(":")
                mix[key.strip()] = int(value)
            return mix

        base_pid = None
        used_nos = []

        def _bump_pid(pid, k):
            head, _, tail = str(pid).rpartition("-")
            if tail.isdigit():
                return "%s-%0*d" % (head, len(tail), int(tail) + k)
            return "%s-%d" % (pid, k)

        for idx, total in enumerate(sizes):
            if idx < len(explicit):
                mix = _parse_mix(explicit[idx])
                if sum(mix.values()) != total:
                    raise SystemExit("--mix %r 合计 %d ≠ --sizes 的 %d"
                                     % (explicit[idx], sum(mix.values()), total))
            elif total >= 20:
                mix = {"选择": max(1, total * 5 // 12), "填空": max(1, total // 3),
                       "解答": max(1, total - total * 5 // 12 - total // 3)}
            else:
                mix = {"选择": max(1, total * 4 // 10), "填空": max(1, total * 3 // 10),
                       "解答": max(1, total - total * 4 // 10 - total * 3 // 10)}
            params = {
                "title": "七年级数学 · 训练卷（%d 题）" % total,
                "mix": mix,
                "scope": {"chapters": [], "kps": []},
                "requirements": {"need_answer": True},
                "seed": args.seed,
                # 卷面只印题、作答在答题卡上 → 不留白（CONTRACT §2）
                "space": {"选择": 0, "填空": 0, "解答": 0},
                "layout": args.layout,
                "calibration": not args.no_calibration,
            }
            build_kw = {} if base_pid is None else {"pid": _bump_pid(base_pid, idx)}
            if used_nos:
                params["exclude_nos"] = list(used_nos)
            manifest = picker.build(params, conn, **build_kw)
            if base_pid is None:
                base_pid = manifest["pid"]
            used_nos.extend(it["no"] for it in manifest["items"])
            if args.flat:
                out_dir, name = args.out, (args.name or "paper%d" % total)
            else:
                out_dir = os.path.join(args.out, args.name if (args.name and len(sizes) == 1)
                                       else "paper%d" % total)
                name = "paper"
            res = render_paper(manifest, out_dir, name=name, layout=args.layout,
                               deep_check=not args.no_deep_check,
                               calibration=not args.no_calibration)
            h = res["health"]
            item = {
                "n_requested": total, "n_items": h["n_items"],
                "mix": mix, "pid": manifest["pid"],
                "pages": res["pages"], "docx": res["docx"], "pdf": res["pdf"],
                "png": res["png"], "first_png": res["png"][0] if res["png"] else None,
                "formula_tokens": h["formula_tokens"],
                "omml_expected": h["formula_omml_expected"],
                "omml_observed": h["formula_omml_observed"],
                "embed_tokens": h["embed_tokens"],
                "embed_expected": h["embed_containers_expected"],
                "embed_observed": h["embed_containers_observed"],
                "media_count": h["media_count"],
                "option_tables": h.get("option_tables", 0),
                "seed": args.seed,
                "nos": [it["no"] for it in manifest["items"]],
                "layout": h.get("layout", args.layout),
                "balance": h.get("balance", {}),
                "dangling": h.get("dangling", []),
                "media_missing": h.get("media_missing", []),
                "rid_refs": h.get("rid_refs", 0),
                "word_open": h.get("word_open", ""),
                "paper_pages": h.get("paper_pages", 0),
                "fig_ref_missing": h.get("fig_ref_missing", 0),
                "space_paras_total": h.get("space_paras_total", 0),
                "calibration": h.get("calibration", False),
                "paper_pages": h.get("paper_pages", 0),
                "page_size_pt": h.get("page_size_pt"),
                "stem_prefix_stripped": h.get("stem_prefix_stripped", 0),
                "ink_ratio": [round(x, 4) for x in h["ink_ratio"]],
                "warnings": h["warnings"],
                "elapsed_s": h["elapsed_s"],
            }
            if args.flat and res["png"]:
                png_dir = os.path.join(out_dir, name + "_pages")
                os.makedirs(png_dir, exist_ok=True)
                moved = []
                for src_png in res["png"]:
                    dst_png = os.path.join(png_dir, os.path.basename(src_png))
                    if os.path.abspath(src_png) != os.path.abspath(dst_png):
                        if os.path.exists(dst_png):
                            os.remove(dst_png)
                        shutil.move(src_png, dst_png)
                    moved.append(dst_png)
                res["png"] = moved
                item["png"] = moved
                item["first_png"] = moved[0]
            report.append(item)
            print(json.dumps(item, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    print("== 汇总 ==")
    for it in report:
        print("%d 题 → %d 页 · OMML %d/%d · OLE %d/%d · 媒体 %d · 选项表 %d · %.1fs · %s"
              % (it["n_items"], it["pages"], it["omml_observed"], it["omml_expected"],
                 it["embed_observed"]["object"], it["embed_expected"]["object"],
                 it["media_count"], it["option_tables"], it["elapsed_s"], it["pdf"]))
        for w in it["warnings"]:
            print("   ⚠ " + w)
    if args.summary:
        os.makedirs(os.path.dirname(os.path.abspath(args.summary)), exist_ok=True)
        with open(args.summary, "w", encoding="utf-8") as fh:
            json.dump({"db": os.path.abspath(args.db), "seed": args.seed,
                       "papers": report}, fh, ensure_ascii=False, indent=2)
        print("summary → %s" % args.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
