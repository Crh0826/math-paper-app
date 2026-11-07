# -*- coding: utf-8 -*-
"""M1 · 拼装 docx 的结构测试（不需要 Word，秒级）。

验证：把 00001(OMML) + 01000(OLE公式) 的 token 拼进同一份文档后，
      公式/图片对象数量不丢，rId 已重映射，文本顺序正确，且能被 Word 正常打开（结构层）。
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx_rich import extract_tokens  # noqa: E402
from compose import build_paper  # noqa: E402

# 题库目录不再写死 E:\OCR（2026-11-01 题库搬进 E:\OCR\qbank）：走 paths 解析
try:
    import paths as _paths
    ROOT = _paths.resolve_qbank(legacy=_paths.LEGACY_QBANK)
except Exception:  # noqa: BLE001
    ROOT = r"E:\OCR"
Q_OMML = os.path.join(ROOT, "00001.docx")
Q_OLE = os.path.join(ROOT, "01000.docx")


class TestCompose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="compose_")
        cls.out = os.path.join(cls.tmp, "paper.docx")
        build_paper(
            [
                {"heading": "1.（一元一次方程）", "tokens": extract_tokens(Q_OMML), "space_paras": 1},
                {"heading": "2.（有理数运算）", "tokens": extract_tokens(Q_OLE), "space_paras": 1},
            ],
            cls.out,
            title="七年级数学 · 单元测试",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_file_created(self):
        self.assertTrue(os.path.exists(self.out))
        self.assertGreater(os.path.getsize(self.out), 5000)

    def test_zip_integrity(self):
        """docx 是 zip：必须能完整读出且 [Content_Types].xml 存在。"""
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
            self.assertIn("[Content_Types].xml", names)
            self.assertIn("word/document.xml", names)
            bad = z.testzip()
            self.assertIsNone(bad)

    def test_math_count(self):
        with zipfile.ZipFile(self.out) as z:
            xml = z.read("word/document.xml")
        self.assertEqual(6, xml.count(b"<m:oMath"), "OMML 公式丢失")

    def test_ole_and_media(self):
        with zipfile.ZipFile(self.out) as z:
            xml = z.read("word/document.xml")
            media = [n for n in z.namelist() if n.startswith("word/media/")]
            rels = z.read("word/_rels/document.xml.rels")
        self.assertEqual(4, xml.count(b"Equation.DSMT4"), "OLE 公式对象丢失")
        self.assertEqual(4, len(media), f"媒体文件应有 4 份，实际 {len(media)}")
        # 每个 rId 引用都必须在新文档的 rels 中存在
        import re
        used = set(re.findall(r'r:id="(rId\d+)"', xml.decode("utf-8")))
        used |= set(re.findall(r'r:embed="(rId\d+)"', xml.decode("utf-8")))
        rel_text = rels.decode("utf-8")
        for rid in used:
            self.assertIn(f'Id="{rid}"', rel_text, f"{rid} 未注册关系")

    def test_text_present(self):
        with zipfile.ZipFile(self.out) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        for needle in ("某同学解方程", "下列计算正确的是", "单元测试"):
            self.assertIn(needle, xml)

    def test_east_asia_font(self):
        """中文字体必须设 eastAsia，否则 Word 用西文字体渲染中文会字形错乱。"""
        with zipfile.ZipFile(self.out) as z:
            styles = z.read("word/styles.xml").decode("utf-8")
        self.assertIn("eastAsia", styles)



    def test_table_media_no_dangling_rels(self):
        """P0：表格里的图片必须重映射 rId + 注册媒体，拼完不许有悬空关系（Word 打不开的根因）。"""
        toks = extract_tokens(os.path.join(ROOT, "00568.docx"))
        out = os.path.join(self.tmp, "t568.docx")
        build_paper([{"heading": "1.", "tokens": toks, "space_paras": 0}], out, title="表格图")
        with zipfile.ZipFile(out) as z:
            doc = z.read("word/document.xml").decode("utf-8")
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        used = set(re.findall(r'r:(?:id|embed|link)="([^"]+)"', doc))
        have = set(re.findall(r'Id="([^"]+)"', rels))
        dangling = sorted(used - have)
        self.assertEqual([], dangling, "悬空关系：%s" % dangling)
        self.assertGreaterEqual(len([u for u in used if u.startswith("rId")]), 5,
                                "00568 表格里那 5 张图应该都还在")


if __name__ == "__main__":
    unittest.main(verbosity=2)
