# -*- coding: utf-8 -*-
"""M0 · 题目 docx → 富文本 token 流：契约测试。

语料里有两种公式编码，都必须原样保住：
  00001.docx  原生 OMML（m:oMath）× 6
  01000.docx  MathType/OLE 公式对象（w:object + WMF 预览）× 4
运行：$env:PYTHONIOENCODING='utf-8'; python E:\\OCR\\app\\tests\\test_docx_rich.py
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx_rich import Token, extract_tokens, plain_text, stats  # noqa: E402

ROOT = r"E:\OCR"
Q_OMML = os.path.join(ROOT, "00001.docx")      # 6 个 OMML，0 张图
Q_OLE = os.path.join(ROOT, "01000.docx")       # 4 个 OLE 公式对象（WMF 预览）
Q_TABS = os.path.join(ROOT, "00002.docx")      # 选项用制表符分隔


class TestTokenizer(unittest.TestCase):
    def test_omml_preserved(self):
        """00001：6 个 OMML 公式一个不漏，且 XML 可独立解析。"""
        tokens = extract_tokens(Q_OMML)
        maths = [t for t in tokens if t.kind == "math"]
        self.assertEqual(6, len(maths), f"期望 6 个 OMML，实际 {len(maths)}")
        from lxml import etree
        for m in maths:
            wrapped = ('<root xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
                       + m.xml + "</root>")
            frag = etree.fromstring(wrapped.encode("utf-8"))
            self.assertEqual(1, len(frag), "公式 XML 片段不可解析")

    def test_ole_series_preserved(self):
        """01000：4 个 MathType 公式对象，每个带 1 份 WMF 预览与显示尺寸。"""
        tokens = extract_tokens(Q_OLE)
        embeds = [t for t in tokens if t.kind == "embed"]
        self.assertEqual(4, len(embeds), f"期望 4 个 OLE 对象，实际 {len(embeds)}")
        for e in embeds:
            self.assertEqual(1, len(e.media), "每个 OLE 对象应带 1 份预览图")
            _rid, path, data = e.media[0]
            self.assertTrue(path.startswith("word/media/"), path)
            self.assertGreater(len(data), 100, "预览图二进制为空")
            self.assertGreater(e.width_pt, 1.0, "缺少显示宽度")
            self.assertGreater(e.height_pt, 1.0, "缺少显示高度")
            self.assertIn("Equation.DSMT4", e.xml, "OLE ProgID 应保留在 XML 中")

    def test_text_in_order(self):
        """文本按文档顺序保留，公式两侧的文字都在，不是文字全在前、公式全在后。"""
        tokens = extract_tokens(Q_OMML)
        text = plain_text(tokens)
        self.assertIn("某同学解方程", text)
        self.assertIn("系数", text)
        kinds = [t.kind for t in tokens]
        first = kinds.index("math")
        self.assertTrue(any(k == "text" for k in kinds[:first]))
        self.assertTrue(any(k == "text" for k in kinds[first:]))

    def test_tabs_kept(self):
        """00002 的四个选项靠制表符分隔，制表符必须保留。"""
        text = "".join(t.text for t in extract_tokens(Q_TABS) if t.kind == "text")
        self.assertIn("\t", text)

    def test_no_empty_tokens(self):
        for f in (Q_OMML, Q_OLE, Q_TABS):
            for t in extract_tokens(f):
                if t.kind == "text":
                    self.assertTrue(t.text.strip() or t.text == "\t")
                elif t.kind == "math":
                    self.assertTrue(t.xml.strip())
                elif t.kind == "embed":
                    self.assertTrue(t.xml.strip())
                    self.assertGreater(len(t.media), 0)

    def test_serializable(self):
        tokens = extract_tokens(Q_OLE)
        raw = json.dumps([t.to_dict(with_data=False) for t in tokens], ensure_ascii=False)
        self.assertIn("embed", raw)
        self.assertIn("media/image1.wmf", raw)
        full = json.dumps([t.to_dict(with_data=True) for t in tokens], ensure_ascii=False)
        self.assertIn("media_b64", full)

    def test_stats(self):
        s = stats(extract_tokens(Q_OLE))
        self.assertEqual(4, s["embed"])
        self.assertEqual(4, s["media"])

    def test_readonly(self):
        before = (os.path.getsize(Q_OLE), os.path.getmtime(Q_OLE))
        extract_tokens(Q_OLE)
        after = (os.path.getsize(Q_OLE), os.path.getmtime(Q_OLE))
        self.assertEqual(before, after)



    def test_table_collects_media(self):
        """P0：表格里嵌的图片必须被收集进 table token 的 media，否则拼卷后 rId 悬空、docx 打不开。

        00568 的 1×7 表格里有 5 个 r:embed（rIdImg1..5）。
        """
        toks = extract_tokens(os.path.join(ROOT, "00568.docx"))
        tables = [t for t in toks if t.kind == "table"]
        self.assertTrue(tables, "00568 应该有 table token")
        media = [m for t in tables for m in t.media]
        self.assertGreaterEqual(len(media), 5, "表格里的 5 张图必须进 media，实际 %d" % len(media))
        self.assertTrue(all(m[2] for m in media), "媒体二进制不能为空")
        self.assertTrue(all(m[1].startswith("word/media/") for m in media))



    def test_table_token_serializes_media(self):
        """table token 的 to_dict 必须带媒体信息，否则入库后 n_media 丢失（00568 曾因此空着）。"""
        toks = extract_tokens(os.path.join(ROOT, "00568.docx"))
        table = [t for t in toks if t.kind == "table"][0]
        d = table.to_dict(with_data=False)
        self.assertGreaterEqual(d.get("n_media") or 0, 5)
        self.assertTrue(all(n.startswith("word/media/") for n in d.get("media_names") or []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
