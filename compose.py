# -*- coding: utf-8 -*-
"""M1 · 把题目 token 流拼装成可打印的 docx（公式与图片原样搬回）。

难点在 embed：题目里的 MathType 公式是 <w:object> + WMF 预览，搬进新文档时必须
把 XML 里的 rId 换成新文档的 rId，并把媒体二进制注册成新部件，否则 Word 打开会报"内容有问题"。
"""
from __future__ import annotations

import hashlib
import posixpath
from typing import Dict, Iterable, List, Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.shared import Pt
from lxml import etree

from docx_rich import Token

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

CONTENT_TYPES = {
    "wmf": "image/x-wmf",
    "emf": "image/x-emf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}


def _ctype(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    return CONTENT_TYPES.get(ext, "application/octet-stream")


class _MediaRegistry:
    """同一文档内去重：相同内容的媒体只注册一次；找不到媒体的 ref 记进 warnings。"""

    def __init__(self, doc: Document):
        self.doc = doc
        self.by_hash: Dict[str, str] = {}
        self.warnings: List[str] = []

    def register(self, path: str, data: bytes) -> str:
        h = hashlib.sha1(data).hexdigest()
        if h in self.by_hash:
            return self.by_hash[h]
        ext = path.rsplit(".", 1)[-1].lower()
        name = f"q{h[:16]}.{ext}"
        part = Part(PackURI("/word/media/" + name), _ctype(path), data)
        rid = self.doc.part.relate_to(part, RT.IMAGE)
        self.by_hash[h] = rid
        return rid


def _remap(element, mapping: Dict[str, str]):
    """把 XML 子树里旧 rId 换成新 rId（r:id / r:embed / r:link）。

    返回**没映射上**的旧 rId 列表（调用方要记 warning，不许静默）。
    """
    missing: List[str] = []
    if not mapping:
        for node in element.iter():
            if not isinstance(node.tag, str):
                continue
            for attr in ("{%s}id" % R_NS, "{%s}embed" % R_NS, "{%s}link" % R_NS):
                v = node.get(attr)
                if v:
                    missing.append(v)
        return missing
    for node in element.iter():
        if not isinstance(node.tag, str):
            continue
        for attr in ("{%s}id" % R_NS, "{%s}embed" % R_NS, "{%s}link" % R_NS):
            v = node.get(attr)
            if not v:
                continue
            if v in mapping:
                node.set(attr, mapping[v])
            else:
                missing.append(v)
    return missing


def _add_text(par, text: str):
    """按 \n 拆段、\t 转制表位写入段落。"""
    from docx.enum.text import WD_BREAK
    chunks = text.split("\n")
    for i, chunk in enumerate(chunks):
        if i:
            par.add_run().add_break(WD_BREAK.LINE)
        for j, piece in enumerate(chunk.split("\t")):
            if j:
                par.add_run().add_tab()
            if piece:
                par.add_run(piece)


def add_tokens(doc: Document, tokens: Iterable[Token], registry: Optional[_MediaRegistry] = None,
               size_pt: float = 10.5, empty_after_paras: int = 0) -> None:
    """把一题的 token 流追加到文档末尾。"""
    reg = registry or _MediaRegistry(doc)
    par = doc.add_paragraph()
    fmt = par.paragraph_format
    fmt.space_before = Pt(2)
    fmt.space_after = Pt(2)
    fmt.line_spacing = 1.15

    for tok in tokens:
        if tok.kind == "text":
            _add_text(par, tok.text)
        elif tok.kind == "math":
            frag = etree.fromstring(tok.xml.encode("utf-8"))
            par._p.append(frag)
        elif tok.kind == "embed":
            frag = etree.fromstring(tok.xml.encode("utf-8"))
            mapping = {}
            for old_rid, path, data in tok.media:
                mapping[old_rid] = reg.register(path, data)
            missing = _remap(frag, mapping)
            for rid in missing:
                reg.warnings.append("embed 引用 %s 找不到媒体" % rid)
            par._p.append(frag)
        elif tok.kind == "table":
            # 表格整块搬：必须重映射 rId 并注册媒体，否则 rels 缺关系 → Word 报文件损坏
            frag = etree.fromstring(tok.xml.encode("utf-8"))
            mapping = {}
            for old_rid, path, data in tok.media:
                mapping[old_rid] = reg.register(path, data)
            missing = _remap(frag, mapping)
            for rid in missing:
                reg.warnings.append("table 引用 %s 找不到媒体（题内表格有图但没收集到）" % rid)
            doc.element.body.insert(len(doc.element.body) - 1, frag)
            par = doc.add_paragraph()

    for r in par.runs:
        if r.font.size is None:
            r.font.size = Pt(size_pt)

    for _ in range(empty_after_paras):
        doc.add_paragraph()


def new_document(margin_cm: float = 2.0):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Pt(595.3)   # A4 210mm
    sec.page_height = Pt(841.9)  # A4 297mm
    sec.left_margin = sec.right_margin = Pt(margin_cm * 28.35)
    sec.top_margin = sec.bottom_margin = Pt(margin_cm * 28.35)
    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(10.5)
    # 中文字体（东亚）也要设，否则 Word 用默认西文字体渲染中文
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
    if rfonts is None:
        rfonts = etree.SubElement(rpr, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
    rfonts.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia", "宋体")
    return doc


def build_paper(items: List[dict], out_path: str, title: str = "") -> str:
    """items: [{"heading": "1.（有理数）", "tokens": [...], "space_paras": 2}]"""
    doc = new_document()
    if title:
        head = doc.add_paragraph()
        head.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = head.add_run(title)
        run.bold = True
        run.font.size = Pt(16)

    for it in items:
        heading = it.get("heading") or ""
        if heading:
            p = doc.add_paragraph()
            r = p.add_run(heading)
            r.bold = True
            r.font.size = Pt(10.5)
        add_tokens(doc, it["tokens"], empty_after_paras=it.get("space_paras", 0))
    doc.save(out_path)
    return out_path
