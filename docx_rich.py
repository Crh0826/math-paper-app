# -*- coding: utf-8 -*-
"""M0 · 题目 docx → 可再排版的富文本 token 流（保留公式与图片原位）。

为什么不用 python-docx 的 paragraph.text：
  本语料的公式有两种编码，python-docx 的 .text 会把它们整体丢掉：
    ① 原生 OMML（<m:oMath>），如 00001.docx
    ② MathType/OLE 公式对象（<w:object> 内嵌 WMF 预览图，ProgID=Equation.DSMT4），如 01000.docx
  丢掉后题干会变成 "解方程时，把“”处的系数看错了"，无法印卷。

token 契约（4 类，按文档顺序）：
  text  纯文本（含 \t 制表符、\n 换行）
  math  OMML 公式，保留完整 XML，可直接插回 Word
  embed 不透明内联对象（w:drawing / w:object / w:pict），保留 XML + 其引用的媒体二进制 + 显示尺寸，
        排版时整体搬回并重映射 rId（这样 WMF 公式预览、几何图、文本框都能原样还原）
  table 表格，保留 XML

原始 docx 全程只读。
"""
from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Tuple

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V_NS = "urn:schemas-microsoft-com:vml"

NS = {"w": W_NS, "m": M_NS, "r": R_NS, "a": A_NS, "wp": WP_NS, "v": V_NS}
EMU_PER_PT = 12700.0


@dataclass
class Token:
    """一处可排版内容。"""

    kind: str                       # text | math | embed | table
    text: str = ""
    xml: str = ""
    media: List[Tuple[str, str, bytes]] = field(default_factory=list)  # (old_rId, zip_path, bytes)
    width_pt: float = 0.0
    height_pt: float = 0.0
    rows: int = 0
    cols: int = 0

    def to_dict(self, with_data: bool = False) -> dict:
        d = {"kind": self.kind}
        if self.kind == "text":
            d["text"] = self.text
        elif self.kind == "math":
            d["xml"] = self.xml
        elif self.kind == "embed":
            d.update(
                xml=self.xml,
                width_pt=round(self.width_pt, 2),
                height_pt=round(self.height_pt, 2),
                n_media=len(self.media),
                media_names=[m[1] for m in self.media],
            )
            if with_data:
                import base64
                d["media_b64"] = [base64.b64encode(m[2]).decode("ascii") for m in self.media]
        elif self.kind == "table":
            d.update(xml=self.xml, rows=self.rows, cols=self.cols,
                     n_media=len(self.media), media_names=[m[1] for m in self.media])
        return d


def _local(el) -> str:
    return etree.QName(el).localname


def _ns(el) -> str:
    return etree.QName(el).namespace or "" if isinstance(el.tag, str) else ""


def _load_rels(z: zipfile.ZipFile) -> Dict[str, str]:
    try:
        root = etree.fromstring(z.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    out = {}
    for rel in root:
        rid, target = rel.get("Id"), rel.get("Target") or ""
        if not rid or not target:
            continue
        out[rid] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
            posixpath.join("word", target))
    return out


def _emu_to_pt(v) -> float:
    try:
        return float(v) / EMU_PER_PT
    except (TypeError, ValueError):
        return 0.0


def _style_size(style: str) -> Tuple[float, float]:
    w = h = 0.0
    for part in (style or "").split(";"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        try:
            if k == "width" and v.endswith("pt"):
                w = float(v[:-2])
            elif k == "height" and v.endswith("pt"):
                h = float(v[:-2])
        except ValueError:
            pass
    return w, h


def _intrinsic_pt(data: bytes) -> Tuple[float, float]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        return w * 72.0 / 96.0, h * 72.0 / 96.0
    except Exception:
        return 0.0, 0.0


def _media_refs(el) -> List[Tuple[str, str]]:
    """收集元素内所有指向媒体文件的 r:id / r:embed / r:link。"""
    refs = []
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        for attr in ("{%s}id" % R_NS, "{%s}embed" % R_NS, "{%s}link" % R_NS):
            v = node.get(attr)
            if v:
                refs.append((v, attr))
    return refs


class _Walker:
    def __init__(self, z: zipfile.ZipFile, rels: Dict[str, str]):
        self.z = z
        self.rels = rels
        self.tokens: List[Token] = []
        self._buf: List[str] = []

    def flush(self):
        if self._buf:
            s = "".join(self._buf)
            self._buf = []
            if s:
                self.tokens.append(Token("text", text=s))

    def walk(self, el):
        ns, ln = _ns(el), _local(el)
        if ns == W_NS and ln == "p":
            for ch in el:
                self.walk(ch)
            self.flush()
            self._buf.append("\n")
            return
        if ns in (W_NS, M_NS) and ln in ("pPr", "rPr", "sectPr", "tblPr", "trPr", "tcPr"):
            return
        if ns == W_NS and ln == "t":
            self._buf.append(el.text or "")
            return
        if ns == W_NS and ln == "tab":
            self._buf.append("\t")
            return
        if ns == W_NS and ln in ("br", "cr"):
            self._buf.append("\n")
            return
        if ns == W_NS and ln in ("delText", "instrText", "fldChar", "bookmarkStart", "bookmarkEnd"):
            return
        if ns == M_NS and ln == "oMath":
            self.flush()
            self.tokens.append(Token("math", xml=etree.tostring(el, encoding="unicode")))
            return
        if ns in (M_NS, W_NS) and ln in ("oMathPara", "smartTag", "hyperlink", "fldSimple", "ins"):
            for ch in el:
                self.walk(ch)
            return
        if ns == W_NS and ln in ("drawing", "object", "pict"):
            self.flush()
            self._embed(el)
            return
        if ns == W_NS and ln == "tbl":
            self.flush()
            trs = el.findall("w:tr", NS)
            self.tokens.append(Token(
                "table",
                xml=etree.tostring(el, encoding="unicode"),
                media=self._collect_media(el),   # 表格内可能嵌图；不收集 → 拼卷后 rId 悬空、docx 打不开
                rows=len(trs),
                cols=max([len(tr.findall("w:tc", NS)) for tr in trs] or [0]),
            ))
            return
        for ch in el:
            self.walk(ch)

    def _collect_media(self, el):
        """收集元素内所有 r:id/r:embed/r:link 指向的媒体：[(old_rId, zip_path, bytes)]。"""
        media: List[Tuple[str, str, bytes]] = []
        seen = set()
        for rid, _attr in _media_refs(el):
            path = self.rels.get(rid)
            if not path or rid in seen:
                continue
            try:
                data = self.z.read(path)
            except KeyError:
                continue
            seen.add(rid)
            media.append((rid, path, data))
        return media

    def _embed(self, el):
        media = self._collect_media(el)

        w = h = 0.0
        ext = el.find(".//wp:extent", NS)
        if ext is not None:
            w, h = _emu_to_pt(ext.get("cx")), _emu_to_pt(ext.get("cy"))
        if (w <= 0 or h <= 0):
            shape = el.find(".//v:shape", NS)
            if shape is not None:
                sw, sh = _style_size(shape.get("style"))
                w, h = (sw or w), (sh or h)
        if (w <= 0 or h <= 0) and media:
            iw, ih = _intrinsic_pt(media[0][2])
            w, h = (iw or w), (ih or h)
        if w <= 0:
            w, h = 12.0, 12.0

        self.tokens.append(Token(
            "embed",
            xml=etree.tostring(el, encoding="unicode"),
            media=media,
            width_pt=w,
            height_pt=h,
        ))


def extract_tokens(path: str) -> List[Token]:
    """读取一份题目 docx，返回按文档顺序的可排版 token。只读，不修改原文件。"""
    with zipfile.ZipFile(path) as z:
        rels = _load_rels(z)
        try:
            root = etree.fromstring(z.read("word/document.xml"))
        except KeyError:
            return []
        body = root.find("w:body", NS)
        if body is None:
            return []
        w = _Walker(z, rels)
        w.walk(body)
        w.flush()

    out: List[Token] = []
    for t in w.tokens:
        if t.kind == "text":
            s = t.text
            if not s:
                continue
            if out and out[-1].kind == "text":
                out[-1].text += s
                continue
            out.append(t)
        else:
            out.append(t)
    while out and out[-1].kind == "text" and not out[-1].text.strip():
        out.pop()
    if out and out[-1].kind == "text":
        out[-1].text = out[-1].text.rstrip()
    return out


def iter_body_items(path: str) -> Iterator[Token]:
    yield from extract_tokens(path)


def plain_text(tokens: Iterable[Token]) -> str:
    """带占位的纯文本：用于检索、打标、判分比对。"""
    parts = []
    for t in tokens:
        if t.kind == "text":
            parts.append(t.text)
        elif t.kind == "math":
            parts.append("⟨式⟩")
        elif t.kind == "embed":
            parts.append("⟨图⟩")
        elif t.kind == "table":
            parts.append("⟨表⟩")
    return "".join(parts)


def stats(tokens: Iterable[Token]) -> dict:
    c = {"text": 0, "math": 0, "embed": 0, "table": 0, "media": 0}
    for t in tokens:
        c[t.kind] += 1
        if t.kind == "embed":
            c["media"] += len(t.media)
    return c
