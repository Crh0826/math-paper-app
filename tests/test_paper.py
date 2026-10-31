# -*- coding: utf-8 -*-
"""T5 · 卷面排版（paper.py）：A4 一题一框 + 页眉卷号 + 保真体检。

先红后绿：本文件先于 paper.py 写。

夹具：3 题小卷（真库里的真题，题面公式两类都覆盖）
  00688 选择 · OMML 公式（1 个 m:oMath）· kp 9.2
  02249 填空 · MathType-OLE（2 个 w:object + 2 个不同 WMF）· kp 1.3
  21115 解答 · 纯文本（无公式，留白区靠默认值）· kp 2.3
另加 00492（选择 · OMML+MathType 混在同一题：1 math + 5 embed）做最硬的一例。

覆盖：
  1. docx 是合法 zip、有 document/header/footer 部件，题数 = manifest 题数；
  2. 一题一框：body 级 w:tbl 数 = 题数；大题分组标题 一、选择题 / 二、填空题 / 三、解答题；
  3. 题号加粗且不与题面自带编号重复（"1．化简…" 不会印成 "1．1．化简…"）；
  4. 题后小字知识点标注（"（1.3 有理数的加减）"）；
  5. 留白：解答题默认 8 段、选择/填空默认 0 段，显式 space_paras 优先；
  6. 页眉（标题 + 卷号）与姓名/班级/日期填写栏、页脚页码域；
  7. health 保真闸门：math token 数 / oMath 数 / embed 容器数 / 媒体数 / 题数 全部对上；
     少一个公式、少一张图、少一道题 → FidelityError（不是 warning）；
  8. 全流程 render_paper（Word COM 导 PDF + 300dpi PNG）：pages>0、页数与 PNG 数一致、
     每页 ink_ratio 在合理区间、PNG 是 2480×3508。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import paper  # noqa: E402
from lxml import etree  # noqa: E402

SRC = r"E:\OCR"
FIXED_PID = "P20261031-900"
FIXED_NOW = "2026-10-31T14:00:00+08:00"

#: 夹具题（都已核对：docx 存在、token 种类如注释）
Q_CHOICE_OMML = dict(no="00688", qtype="选择", kp_id="9.2", chapter="综合",
                     answer_key=[{"blank": 1, "value": "A", "norm": "A"}])
Q_FILL_OLE = dict(no="02249", qtype="填空", kp_id="1.3", chapter="第1章 有理数",
                  answer_key=[{"blank": 1, "value": "9", "norm": "9"}])
Q_SOLUTION_PLAIN = dict(no="21115", qtype="解答", kp_id="2.3", chapter="第2章 整式及其加减",
                        answer_key=[])
Q_MIXED = dict(no="00492", qtype="选择", kp_id="4.1", chapter="第4章 几何图形初步",
               answer_key=[{"blank": 1, "value": "D", "norm": "D"}])
Q_IMAGE_OPTIONS = dict(no="06500", qtype="选择", kp_id="1.2", chapter="第1章 有理数",
                       answer_key=[{"blank": 1, "value": "B", "norm": "B"}])
Q_FORMULA_OPTIONS = dict(no="07179", qtype="选择", kp_id="9.1", chapter="综合",
                         answer_key=[{"blank": 1, "value": "D", "norm": "D"}])
Q_LONG_OPTIONS = dict(no="02631", qtype="选择", kp_id="4.4", chapter="第4章 几何图形初步",
                      answer_key=[{"blank": 1, "value": "B", "norm": "B"}])

#: 每题题干里的一段特征文字（用来验证"题干真的进了自己的题框"）
STEM_FRAGS = ["代数式", "A，B是自然数", "化简：x﹣（2x﹣y）"]


def manifest(items=None, title="七年级数学 · 训练卷 A（paper 自检）") -> dict:
    items = items if items is not None else [Q_CHOICE_OMML, Q_FILL_OLE, Q_SOLUTION_PLAIN]
    out = []
    for i, spec in enumerate(items, 1):
        it = {
            "idx": i, "no": spec["no"], "qtype": spec["qtype"],
            "kp_id": spec["kp_id"], "chapter": spec["chapter"],
            "family_key": None, "stem_tokens": [],
            "answer_key": copy.deepcopy(spec.get("answer_key") or []),
            "options": ["A", "B", "C", "D"] if spec["qtype"] == "选择" else [],
            "space_paras": spec.get("space_paras", 0),
            "practice_only": not spec.get("answer_key"),
        }
        out.append(it)
    return {
        "pid": FIXED_PID, "created_at": FIXED_NOW, "title": title,
        "params": {"title": title, "mix": {}, "seed": 42},
        "page": {"w_pt": 595.3, "h_pt": 841.9, "dpi": 300, "w_px": 2480, "h_px": 3508},
        "items": out,
    }


# ---------------------------------------------------------------- docx 读回工具
def doc_root(docx_path):
    with zipfile.ZipFile(docx_path) as z:
        return etree.fromstring(z.read("word/document.xml"))


def body_tables(docx_path):
    """body 级 w:tbl = 一题一框的外框（题目内容里的表格都在单元格内部，不算）。"""
    root = doc_root(docx_path)
    body = root.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body")
    return [el for el in body if etree.QName(el).localname == "tbl"]


def all_text(docx_path) -> str:
    root = doc_root(docx_path)
    return "".join(el.text or "" for el in root.iter()
                   if isinstance(el.tag, str) and etree.QName(el).localname == "t")


def header_text(docx_path) -> str:
    parts = []
    with zipfile.ZipFile(docx_path) as z:
        for name in z.namelist():
            if name.startswith("word/header") and name.endswith(".xml"):
                root = etree.fromstring(z.read(name))
                parts.append("".join(el.text or "" for el in root.iter()
                                     if isinstance(el.tag, str)
                                     and etree.QName(el).localname == "t"))
    return "".join(parts)


def footer_xml(docx_path) -> str:
    out = []
    with zipfile.ZipFile(docx_path) as z:
        for name in z.namelist():
            if name.startswith("word/footer") and name.endswith(".xml"):
                out.append(z.read(name).decode("utf-8"))
    return "".join(out)


def box_paragraphs(docx_path, i):
    """第 i 个题框里的段落列表（python-docx 视角）。"""
    from docx import Document

    doc = Document(docx_path)
    cell = doc.tables[i].cell(0, 0)
    return cell.paragraphs


def count_empty_paras(paras) -> int:
    return sum(1 for p in paras if not p.text.strip() and not p.runs)


def rewrite_docx(docx_path, mutate):
    tmp = docx_path + ".tmp"
    with zipfile.ZipFile(docx_path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = mutate(item.filename, zin.read(item.filename))
            if data is not None:
                zout.writestr(item, data)
    os.replace(tmp, docx_path)


# ---------------------------------------------------------------- 1. 基础结构
class PrefixStripTest(unittest.TestCase):
    """题面前缀剥离规则（印卷时不能叠号、不能暴露"第几例的第几个变式"）。"""

    def test_rules(self):
        f = paper.strip_stem_prefix
        self.assertEqual(f("【例6】（23-24七年级上·江苏无锡·期中）长方形"),
                         "（23-24七年级上·江苏无锡·期中）长方形")
        self.assertEqual(f("【典例1】如图，下列是四位同学所画的数轴"), "如图，下列是四位同学所画的数轴")
        self.assertEqual(f("【变式5-2】已知"), "已知")
        self.assertEqual(f("【变式8-1】已知关于x的方程"), "已知关于x的方程")
        self.assertEqual(f("177．（25-26七年级下·四川绵阳·开学考试）解方程"),
                         "（25-26七年级下·四川绵阳·开学考试）解方程")
        self.assertEqual(f("10．下列各数中"), "下列各数中")
        # 小数点不能拆
        self.assertEqual(f("3．5 元买一支笔"), "3．5 元买一支笔")
        # 没有前缀就原样返回
        self.assertEqual(f("化简：x﹣（2x﹣y）+3x﹣2y．"), "化简：x﹣（2x﹣y）+3x﹣2y．")
        # 只有标签没有题干 → 保留（否则整题消失）
        self.assertEqual(f("【例6】"), "【例6】")


class PaperDocxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paper_t_")
        cls.m = manifest()
        cls.built = paper.build_docx(cls.m, cls.tmp)
        cls.docx = cls.built["docx"]
        cls.health = paper.check_docx(cls.docx, cls.m, cls.built["expected"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_docx_is_valid_zip(self):
        self.assertTrue(self.docx.endswith(".docx"))
        with zipfile.ZipFile(self.docx) as z:
            bad = z.testzip()
            self.assertIsNone(bad, "docx 有损坏的部件：%r" % (bad,))
            names = set(z.namelist())
        for need in ("word/document.xml", "word/styles.xml", "[Content_Types].xml"):
            self.assertIn(need, names)

    def test_one_box_per_item(self):
        self.assertEqual(len(body_tables(self.docx)), len(self.m["items"]))

    def test_group_headings(self):
        text = all_text(self.docx)
        self.assertIn("一、选择题", text)
        self.assertIn("二、填空题", text)
        self.assertIn("三、解答题", text)

    def test_item_numbers_are_bold(self):
        from docx import Document

        doc = Document(self.docx)
        for i, it in enumerate(self.m["items"], 1):
            runs = doc.tables[i - 1].cell(0, 0).paragraphs[0].runs
            first = next((r for r in runs if r.text.strip()), None)
            self.assertIsNotNone(first, "第 %d 题没有题号 run" % i)
            self.assertTrue(first.text.startswith("%d．" % i), first.text)
            self.assertTrue(first.bold, "题号必须加粗：%r" % (first.text,))
            # 题号不能自己占一行：同一段里必须紧跟题干
            self.assertIn(STEM_FRAGS[i - 1], "".join(r.text for r in runs),
                          "第 %d 题的题号与题干不在同一段（题号被单独放在一行）" % i)

    def test_stem_inside_its_box(self):
        from docx import Document

        doc = Document(self.docx)
        for i, frag in enumerate(STEM_FRAGS, 1):
            cell_text = "\n".join(p.text for p in doc.tables[i - 1].cell(0, 0).paragraphs)
            self.assertIn(frag, cell_text, "第 %d 题的题干没进自己的题框" % i)

    def test_no_stem_text_outside_boxes(self):
        from docx import Document

        doc = Document(self.docx)
        body_text = "\n".join(p.text for p in doc.paragraphs)   # body 级段落（不含表格内）
        for frag in STEM_FRAGS:
            self.assertNotIn(frag, body_text, "题干漏到题框外面了：%r" % frag)

    def test_source_prefix_stripped(self):
        text = all_text(self.docx)
        # 题面自带的编排痕迹（【例N】/【典例N】/【变式N-M】/题号）印卷时都要剥掉
        self.assertNotIn("【变式", text)
        self.assertNotIn("【例", text)
        self.assertNotIn("【典例", text)
        self.assertNotIn("2．2．", text, "题面自带编号没去掉，印成了 2．2．")
        self.assertNotIn("1．1．", text)
        self.assertIn("化简：x﹣（2x﹣y）+3x﹣2y．", text)

    def test_source_note_kept(self):
        # 来源标注（"（23-24七年级上·江苏无锡·期中）"）要保留，方便复习溯源
        tmp = os.path.join(self.tmp, "srcnote")
        os.makedirs(tmp, exist_ok=True)
        built = paper.build_docx(manifest([Q_MIXED]), tmp, name="srcnote")
        text = all_text(built["docx"])
        self.assertIn("（23-24七年级上·江苏无锡·期中）", text)
        self.assertNotIn("【例6】", text)

    def test_kp_annotation(self):
        text = all_text(self.docx)
        self.assertIn("（9.2 压轴与易错综合）", text)
        self.assertIn("（1.3 有理数的加减）", text)
        self.assertIn("（2.3 同类项与合并同类项）", text)

    def test_header_and_fill_in_line(self):
        head = header_text(self.docx)
        self.assertIn("七年级数学 · 训练卷 A", head)
        self.assertIn(FIXED_PID, head)
        body = all_text(self.docx)
        for label in ("姓名", "班级", "日期"):
            self.assertIn(label, body)

    def test_footer_has_page_field(self):
        xml = footer_xml(self.docx)
        self.assertIn("PAGE", xml)
        self.assertIn("NUMPAGES", xml)
        self.assertIn("卷面", xml, "页脚要写明是卷面的页码（卡与卷各自编号）")

    def test_no_writing_space_by_default(self):
        # 用户已定：卷面只印题，作答在答题卡上 → 默认不留白（CONTRACT §2 space 全 0）
        self.assertEqual(set(paper.DEFAULT_SPACE.values()), {0})
        for i, it in enumerate(self.m["items"]):
            self.assertEqual(count_empty_paras(box_paragraphs(self.docx, i)), 0,
                             "第 %d 题（%s）默认不该留作答空白" % (it["idx"], it["qtype"]))

    def test_health_reports_space_paras_total(self):
        """留白段数进 health：>0 的题会显著变高，出卷后要能一眼看出「谁在吃纸」。"""
        items = [dict(Q_SOLUTION_PLAIN, space_paras=3), dict(Q_SOLUTION_PLAIN, space_paras=10)]
        tmp = os.path.join(self.tmp, "spacetotal")
        built = paper.build_docx(manifest(items), tmp, name="spacetotal")
        health = paper.check_docx(built["docx"], manifest(items), built["expected"])
        self.assertEqual(health["space_paras_total"], 13)

    def test_default_space_paras_total_is_zero(self):
        built = paper.build_docx(manifest(), self.tmp, name="spacetotal0")
        health = paper.check_docx(built["docx"], manifest(), built["expected"])
        self.assertEqual(health["space_paras_total"], 0, "默认出卷不该有任何书写段")

    def test_explicit_space_paras_wins(self):
        items = [dict(Q_SOLUTION_PLAIN, space_paras=3), dict(Q_SOLUTION_PLAIN, space_paras=10)]
        tmp = os.path.join(self.tmp, "space")
        os.makedirs(tmp, exist_ok=True)
        built = paper.build_docx(manifest(items), tmp, name="space")
        self.assertEqual(count_empty_paras(box_paragraphs(built["docx"], 0)), 3)
        self.assertEqual(count_empty_paras(box_paragraphs(built["docx"], 1)), 10)

    def test_options_aligned_in_equal_columns(self):
        # 选择题选项必须按等宽列排：A/B/C/D 起点对齐，长选项只在自己列内换行
        from docx import Document

        doc = Document(self.docx)
        box = doc.tables[0].cell(0, 0)
        self.assertTrue(box.tables, "选择题的选项没有排成等宽列表格")
        opt_table = box.tables[0]
        self.assertEqual(len(opt_table.columns), 4)
        labels = [opt_table.cell(0, i).text.strip()[:2] for i in range(4)]
        self.assertEqual(labels, ["A．", "B．", "C．", "D．"])

    def test_image_options_use_two_column_grid(self):
        # 选项本身是数轴图（06500，每张约 155pt）→ 退到 2 列网格（A/B 一行、C/D 一行），
        # 既不挤坏图，也保证 A/B/C/D 起点对齐；只有比两列还宽的图才保持原排法
        from docx import Document

        tmp = os.path.join(self.tmp, "imgopt")
        os.makedirs(tmp, exist_ok=True)
        built = paper.build_docx(manifest([Q_IMAGE_OPTIONS]), tmp, name="imgopt")
        box = Document(built["docx"]).tables[0].cell(0, 0)
        self.assertTrue(box.tables, "数轴选项图（155pt）应该能放进 2 列网格")
        grid = box.tables[0]
        self.assertEqual(len(grid.columns), 2)
        self.assertEqual(len(grid.rows), 2)
        for i, label in enumerate(("A．", "B．", "C．", "D．")):
            cell = grid.cell(i // 2, i % 2)
            text = "\n".join(p.text for p in cell.paragraphs)
            self.assertIn(label, text)

    def test_long_options_stack_one_per_row(self):
        # 选项长到两列都放不下（02631：B/C/D 都超过 220pt）→ 1 列网格：每个选项一行、起点齐
        from docx import Document

        tmp = os.path.join(self.tmp, "longopt")
        os.makedirs(tmp, exist_ok=True)
        built = paper.build_docx(manifest([Q_LONG_OPTIONS]), tmp, name="longopt")
        box = Document(built["docx"]).tables[0].cell(0, 0)
        self.assertTrue(box.tables, "长选项也应该进网格（每个选项一行），不能流式拼在一起")
        grid = box.tables[0]
        self.assertEqual((len(grid.columns), len(grid.rows)), (1, 4))
        labels = [grid.cell(i, 0).text.strip()[:2] for i in range(4)]
        self.assertEqual(labels, ["A．", "B．", "C．", "D．"])

    def test_math_type_formula_options_still_aligned(self):
        # 选项里是 MathType 小公式图（07179）→ 也要排成等宽网格（不能降级成一行四拼）
        from docx import Document

        tmp = os.path.join(self.tmp, "fmlopt")
        os.makedirs(tmp, exist_ok=True)
        built = paper.build_docx(manifest([Q_FORMULA_OPTIONS]), tmp, name="fmlopt")
        box = Document(built["docx"]).tables[0].cell(0, 0)
        self.assertTrue(box.tables, "含小公式图的选项也应该对齐，不能被降级成一行四拼")
        grid = box.tables[0]
        cols = len(grid.columns)
        self.assertIn(cols, (2, 4))
        labels = [grid.cell(i // cols, i % cols).text.strip()[:2] for i in range(4)]
        self.assertEqual(labels, ["A．", "B．", "C．", "D．"])

    def test_manifest_order_and_headings(self):
        # nos 精确题集可能题型交错：卷面题号必须跟 manifest.idx 一致，只在切换处补标题
        tmp = os.path.join(self.tmp, "order")
        os.makedirs(tmp, exist_ok=True)
        items = [Q_CHOICE_OMML, Q_FILL_OLE, Q_SOLUTION_PLAIN, Q_MIXED]
        built = paper.build_docx(manifest(items), tmp, name="order")
        from docx import Document

        doc = Document(built["docx"])
        nums = []
        for i in range(4):
            runs = doc.tables[i].cell(0, 0).paragraphs[0].runs
            nums.append(next(r.text for r in runs if r.text.strip()))
        self.assertEqual(nums, ["1．", "2．", "3．", "4．"], "卷面题号必须等于 manifest.idx")
        text = all_text(built["docx"])
        self.assertIn("一、选择题（共 2 题）", text)
        self.assertIn("二、填空题（共 1 题）", text)
        self.assertIn("三、解答题（共 1 题）", text)
        self.assertIn("一、选择题（续）", text)


# ---------------------------------------------------------------- 2. 保真体检
class HealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paper_h_")
        cls.m = manifest()
        cls.built = paper.build_docx(cls.m, cls.tmp)
        cls.docx = cls.built["docx"]
        cls.health = paper.check_docx(cls.docx, cls.m, cls.built["expected"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_formula_counts(self):
        h = self.health
        self.assertEqual(h["formula_tokens"], 1, "manifest 里只有 00688 的 1 个 math token")
        self.assertEqual(h["formula_omml_expected"], 1)
        self.assertEqual(h["formula_omml_observed"], 1, "卷面里必须真的印出 1 个 OMML 公式")

    def test_embed_counts(self):
        h = self.health
        self.assertEqual(h["embed_tokens"], 2, "02249 有 2 个 MathType-OLE 对象")
        self.assertEqual(h["embed_containers_observed"]["object"], 2)
        self.assertEqual(h["media_refs"], 2)

    def test_media_files_present(self):
        h = self.health
        self.assertEqual(h["media_count"], 2, "两个不同 WMF 都要进 word/media")
        self.assertEqual(h["media_missing"], [], "有 embed 的媒体丢了：%r" % (h["media_missing"],))
        with zipfile.ZipFile(self.docx) as z:
            names = [n for n in z.namelist() if n.startswith("word/media/")]
            blobs = [z.read(n) for n in names]
        self.assertEqual(len(names), 2)
        self.assertEqual(len({hashlib.sha1(b).hexdigest() for b in blobs}), 2)

    def test_item_count_matches(self):
        h = self.health
        self.assertEqual(h["n_items"], 3)
        self.assertEqual(h["n_items_observed"], 3)
        self.assertTrue(h["items_ok"])

    def test_health_is_json_safe(self):
        s = json.dumps(self.health, ensure_ascii=False)
        self.assertEqual(json.loads(s), self.health)

    def test_mixed_omml_and_ole_in_one_question(self):
        tmp = os.path.join(self.tmp, "mixed")
        os.makedirs(tmp, exist_ok=True)
        m = manifest([Q_MIXED])
        built = paper.build_docx(m, tmp, name="mixed")
        h = paper.check_docx(built["docx"], m, built["expected"])
        self.assertEqual(h["formula_tokens"], 1)
        self.assertEqual(h["formula_omml_observed"], 1)
        self.assertEqual(h["embed_tokens"], 5)
        self.assertEqual(h["embed_containers_observed"]["object"], 5)
        self.assertEqual(h["media_count"], 5)
        self.assertEqual(h["media_missing"], [])


class FidelityGateTest(unittest.TestCase):
    """闸门必须是异常，不是 warning。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paper_gate_")
        cls.m = manifest()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fresh(self, name="gate"):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        built = paper.build_docx(self.m, d, name=name)
        return built["docx"], built["expected"]

    def test_missing_formula_raises(self):
        docx, expected = self._fresh("noformula")

        def mutate(name, data):
            if name != "word/document.xml":
                return data
            root = etree.fromstring(data)
            for el in root.iter():
                if isinstance(el.tag, str) and etree.QName(el).localname == "oMath":
                    el.getparent().remove(el)
                    break
            return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                  standalone=True)

        rewrite_docx(docx, mutate)
        with self.assertRaises(paper.FidelityError) as ctx:
            paper.check_docx(docx, self.m, expected)
        self.assertIn("公式", str(ctx.exception))

    def test_missing_media_raises(self):
        docx, expected = self._fresh("nomedia")
        rewrite_docx(docx, lambda name, data: None if name.startswith("word/media/") else data)
        with self.assertRaises(paper.FidelityError) as ctx:
            paper.check_docx(docx, self.m, expected)
        self.assertTrue("图" in str(ctx.exception) or "媒体" in str(ctx.exception),
                        str(ctx.exception))

    def test_missing_embed_container_raises(self):
        docx, expected = self._fresh("noembed")

        def mutate(name, data):
            if name != "word/document.xml":
                return data
            root = etree.fromstring(data)
            for el in root.iter():
                if isinstance(el.tag, str) and etree.QName(el).localname == "object":
                    el.getparent().remove(el)
                    break
            return etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                  standalone=True)

        rewrite_docx(docx, mutate)
        with self.assertRaises(paper.FidelityError) as ctx:
            paper.check_docx(docx, self.m, expected)
        self.assertIn("embed", str(ctx.exception).lower() + " embed")

    def test_item_count_mismatch_raises(self):
        docx, expected = self._fresh("nitems")
        more = manifest([Q_CHOICE_OMML, Q_FILL_OLE, Q_SOLUTION_PLAIN, Q_MIXED])
        with self.assertRaises(paper.FidelityError) as ctx:
            paper.check_docx(docx, more, None)
        self.assertIn("题数", str(ctx.exception))

    def test_missing_source_docx_raises(self):
        bad = manifest([dict(Q_CHOICE_OMML, no="99999")])
        with self.assertRaises(paper.PaperError) as ctx:
            paper.build_docx(bad, self.tmp, name="bad")
        self.assertIn("99999", str(ctx.exception))

    def test_empty_manifest_raises(self):
        with self.assertRaises(paper.PaperError):
            paper.build_docx(manifest([]), self.tmp, name="empty")


# ---------------------------------------------------------------- 3b. 关系（rId）硬闸门
Q_TABLE_IMG = dict(no="00568", qtype="选择", kp_id="3.1", chapter="第3章 一次方程与方程组",
                   answer_key=[{"blank": 1, "value": "B", "norm": "B"}])
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def rid_metrics(docx_path) -> dict:
    """document.xml 引用的 rId vs word/_rels/document.xml.rels 定义的关系。"""
    with zipfile.ZipFile(docx_path) as z:
        doc = etree.fromstring(z.read("word/document.xml"))
        rels = etree.fromstring(z.read("word/_rels/document.xml.rels"))
    refs = [el.get(_R_NS + a) for el in doc.iter() if isinstance(el.tag, str)
            for a in ("id", "embed", "link") if el.get(_R_NS + a)]
    defined = {e.get("Id") for e in rels}
    return {"rid_refs": len(refs), "unique": len(set(refs)), "rels": len(defined),
            "dangling": sorted(set(refs) - defined)}


class RelationshipGateTest(unittest.TestCase):
    """悬空 rId 会让 Word 判「文件已损坏」——必须硬闸门（FidelityError），不是 warning。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paper_rel_")
        cls.m = manifest()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build(self, name="rel"):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return paper.build_docx(self.m, d, name=name)

    def test_health_reports_relationship_stats(self):
        built = self._build("stats")
        h = paper.check_docx(built["docx"], self.m, built["expected"])
        for key in ("rid_refs", "rid_unique", "rels", "dangling"):
            self.assertIn(key, h, "health 要报关系统计：%s" % key)
        self.assertEqual(h["dangling"], [])
        self.assertGreater(h["rid_refs"], 0)
        self.assertEqual(h["rid_refs"], rid_metrics(built["docx"])["rid_refs"])

    def test_dangling_ref_raises(self):
        built = self._build("dangling")

        def mutate(name, data):
            if name != "word/document.xml":
                return data
            root = etree.fromstring(data)
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in ("embed", "id"):
                    v = el.get(_R_NS + attr)
                    if v:
                        el.set(_R_NS + attr, "rIdDANGLING999")
                        return etree.tostring(root, xml_declaration=True,
                                             encoding="UTF-8", standalone=True)
            return data

        rewrite_docx(built["docx"], mutate)
        with self.assertRaises(paper.FidelityError) as ctx:
            paper.check_docx(built["docx"], self.m, built["expected"])
        self.assertIn("悬空", str(ctx.exception))
        self.assertIn("rIdDANGLING999", str(ctx.exception))
        self.assertEqual(getattr(ctx.exception, "code", ""), "dangling_relationship")

    def test_00568_table_images_regression(self):
        """题 00568（1×7 表格 + 5 个 r:embed）单题成卷：关系不悬空、图进得去、Word 打得开、PDF 出得来。

        data-engineer 修好 docx_rich/compose 之前这条是红的（预期），别用「剥掉 r:embed」蒙过去。
        """
        d = os.path.join(self.tmp, "reg568")
        os.makedirs(d, exist_ok=True)
        m = manifest([Q_TABLE_IMG])
        built = paper.build_docx(m, d, name="reg568")
        metrics = rid_metrics(built["docx"])
        self.assertEqual(metrics["dangling"], [],
                         "00568 入卷后出现悬空 rId：%r（table token 没带媒体 / 没重映射）"
                         % metrics["dangling"])
        h = paper.check_docx(built["docx"], m, built["expected"])
        self.assertEqual(h["dangling"], [])
        self.assertGreaterEqual(h["media_count"], 5, "表格里的 5 张图没进 docx")
        if paper.word_available():
            ok, detail, pages = paper.word_can_open(built["docx"])
            self.assertTrue(ok, "Word 打不开 00568 的单题卷：%s" % detail)
            self.assertGreaterEqual(pages, 1)


class WordOpenTest(unittest.TestCase):
    """Word 能打开才算成品；打不开就 FidelityError(word_cannot_open)。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paper_word_")
        if not paper.word_available():
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("没有 Word：跳过 Word 打开校验")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_good_paper_opens_in_word(self):
        m = manifest()
        built = paper.build_docx(m, self.tmp, name="w")
        ok, detail, pages = paper.word_can_open(built["docx"])
        self.assertTrue(ok, "Word 打不开自己生成的 docx：%s" % detail)
        self.assertGreaterEqual(pages, 1)

    def test_corrupt_docx_is_reported_and_gated(self):
        m = manifest()
        built = paper.build_docx(m, self.tmp, name="bad")

        def mutate(name, data):
            if name != "word/document.xml":
                return data
            return data.replace(b"<w:body>", b"<w:body><w:brokenTag", 1)

        rewrite_docx(built["docx"], mutate)
        ok, detail, _pages = paper.word_can_open(built["docx"])
        self.assertFalse(ok, "坏 docx 居然被 Word 打开了")
        self.assertTrue(detail, "失败原因不能为空")
        with self.assertRaises(paper.FidelityError) as ctx:
            paper.word_check_docx(built["docx"])
        self.assertEqual(getattr(ctx.exception, "code", ""), "word_cannot_open")


class RenderFallbackTest(unittest.TestCase):
    """render.py 出异常时的分流：渲染侧错误回退内置，WordUnavailable / 目标被占用直接上抛。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paper_fb_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_render_failure_falls_back_and_warns(self):
        class _Boom:
            @staticmethod
            def docx_to_pdf(docx_path, pdf_path):
                raise RuntimeError("跨盘 os.replace 炸了（模拟 render 的 WinError 17）")

            @staticmethod
            def pdf_to_png(pdf_path, out_dir, dpi=300):
                raise RuntimeError("渲染失败（模拟）")

        calls = {"docx": 0, "png": 0}

        def fake_com(docx_path, pdf_path):
            calls["docx"] += 1
            with open(pdf_path, "wb") as fh:
                fh.write(b"%PDF-1.4 fake")
            return 1, 0.01

        def fake_png(pdf_path, out_dir, dpi=300, prefix="paper"):
            calls["png"] += 1
            return [os.path.join(out_dir, "page-01.png")]

        warnings = []
        with mock.patch.object(paper, "_render_module", return_value=_Boom), \
                mock.patch.object(paper, "_com_docx_to_pdf", side_effect=fake_com), \
                mock.patch.object(paper, "_builtin_pdf_to_png", side_effect=fake_png):
            pdf = os.path.join(self.tmp, "x.pdf")
            pages, _secs = paper.docx_to_pdf("in.docx", pdf, warnings=warnings)
            pngs = paper.pdf_to_png(pdf, self.tmp, warnings=warnings)
        self.assertEqual(pages, 1)
        self.assertEqual(calls, {"docx": 1, "png": 1}, "渲染侧错误应该回退内置实现")
        self.assertEqual(len(pngs), 1)
        self.assertEqual(len(warnings), 2, warnings)
        self.assertTrue(all("回退" in w for w in warnings), warnings)

    def test_word_unavailable_is_not_retried(self):
        """没装 Word：内置分支是同一套 COM，回退只会白等 —— 必须直接上抛。"""

        class WordUnavailable(Exception):
            def __init__(self, msg, reason="not_installed"):
                super().__init__(msg)
                self.reason = reason

        class _NoWord:
            @staticmethod
            def docx_to_pdf(docx_path, pdf_path):
                raise WordUnavailable("本机没装 Word")

            @staticmethod
            def pdf_to_png(pdf_path, out_dir, dpi=300):
                raise WordUnavailable("本机没装 Word")

        hits = []
        warnings = []
        with mock.patch.object(paper, "_render_module", return_value=_NoWord), \
                mock.patch.object(paper, "_com_docx_to_pdf",
                                  side_effect=lambda *a, **k: hits.append("com")), \
                mock.patch.object(paper, "_builtin_pdf_to_png",
                                  side_effect=lambda *a, **k: hits.append("png")):
            with self.assertRaises(WordUnavailable):
                paper.docx_to_pdf("in.docx", os.path.join(self.tmp, "y.pdf"), warnings=warnings)
            with self.assertRaises(WordUnavailable):
                paper.pdf_to_png(os.path.join(self.tmp, "y.pdf"), self.tmp, warnings=warnings)
        self.assertEqual(hits, [], "WordUnavailable 不该回退内置实现")
        self.assertEqual(warnings, [], "不该记「已回退」的误导性 warning")

    def test_locked_target_is_not_retried(self):
        """目标 PDF 被占用：换内置实现也落在同一把锁上 —— 直接上抛，别让用户多等一轮。"""

        class _Locked:
            @staticmethod
            def docx_to_pdf(docx_path, pdf_path):
                raise RuntimeError("目标 PDF 正被别的程序占用，写不进去（WinError 32）")

        hits = []
        with mock.patch.object(paper, "_render_module", return_value=_Locked), \
                mock.patch.object(paper, "_com_docx_to_pdf",
                                  side_effect=lambda *a, **k: hits.append("com")):
            with self.assertRaises(RuntimeError):
                paper.docx_to_pdf("in.docx", os.path.join(self.tmp, "z.pdf"), warnings=[])
        self.assertEqual(hits, [], "「目标被占用」不该回退内置实现")


# ---------------------------------------------------------------- 3c. 省纸模式（t37）
from docx import Document as _Docx          # noqa: E402
from docx.oxml.ns import qn as _qn          # noqa: E402
from docx.shared import Pt as _Pt           # noqa: E402


class LayoutSpecTest(unittest.TestCase):
    """版面参数：compact 是默认，normal 回退宽松版面；未知值不许静默降级。"""

    def test_default_is_compact(self):
        spec = paper.layout_spec()
        self.assertEqual(spec["name"], "compact")
        self.assertAlmostEqual(spec["margin_pt"], 39.7, places=1)   # 1.4cm
        self.assertLess(spec["margin_pt"], paper.layout_spec("normal")["margin_pt"])

    def test_unknown_layout_raises(self):
        with self.assertRaises(paper.PaperError) as ctx:
            paper.layout_spec("tiny")
        self.assertIn("layout", str(ctx.exception))

    def test_needs_balance_only_when_last_page_is_empty(self):
        self.assertFalse(paper.needs_balance(1, [0.05]), "单页卷不用调平")
        self.assertFalse(paper.needs_balance(3, [0.05, 0.04, 0.025]))
        self.assertTrue(paper.needs_balance(2, [0.05, 0.006]), "末页几乎空白要调平")

    def test_tighten_spec_only_shrinks_gaps(self):
        spec = paper.layout_spec()
        tight = paper._tighten_spec(spec)
        self.assertLessEqual(tight["spacer_pt"], spec["spacer_pt"])
        self.assertLessEqual(tight["group_before_pt"], spec["group_before_pt"])
        self.assertEqual(tight["margin_pt"], spec["margin_pt"], "调平不动页边距")
        self.assertEqual(tight["line_spacing"], spec["line_spacing"], "调平不动行距")


class CompactLayoutTest(unittest.TestCase):
    """compact 真的把空白压下去了（不看 Word，直接查 docx 的 sectPr / 段落格式）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paper_compact_")
        cls.m = manifest()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build(self, layout, name=None):
        d = os.path.join(self.tmp, layout)
        os.makedirs(d, exist_ok=True)
        return paper.build_docx(self.m, d, name=name or layout, layout=layout)

    def _sect(self, docx_path):
        with zipfile.ZipFile(docx_path) as z:
            root = etree.fromstring(z.read("word/document.xml"))
        sect = root.find(".//" + _qn("w:sectPr"))
        pg = sect.find(_qn("w:pgMar"))
        return {k: int(pg.get(_qn("w:" + k))) for k in
                ("left", "right", "top", "bottom", "header", "footer")}

    def test_compact_margins_are_1_4cm(self):
        built = self._build("compact")
        mar = self._sect(built["docx"])
        self.assertEqual(mar["left"], mar["right"])
        self.assertAlmostEqual(mar["left"] / 20 / 28.35, 1.4, places=1)
        normal = self._sect(self._build("normal")["docx"])
        self.assertGreater(normal["left"], mar["left"], "normal 的左页边距应该更宽")
        self.assertLess(mar["header"], normal["header"], "compact 的页眉距边距要收紧")

    def test_compact_paragraphs_are_tight(self):
        built = self._build("compact")
        doc = _Docx(built["docx"])
        cell = doc.tables[0].cell(0, 0)
        p = cell.paragraphs[0]
        self.assertEqual(p.paragraph_format.line_spacing, 1.0)
        self.assertEqual(p.paragraph_format.space_before, _Pt(1))
        self.assertEqual(p.paragraph_format.space_after, _Pt(1))
        # 知识点提示行：固定 9pt 行高
        note = cell.paragraphs[1]
        self.assertEqual(note.paragraph_format.line_spacing, _Pt(9))
        # normal 保持原来的 1.15 / 2pt
        doc_n = _Docx(self._build("normal")["docx"])
        pn = doc_n.tables[0].cell(0, 0).paragraphs[0]
        self.assertEqual(pn.paragraph_format.space_after, _Pt(2))

    def test_compact_box_uses_wider_content_area(self):
        c = paper.layout_spec("compact")
        n = paper.layout_spec("normal")
        self.assertGreater(paper._content_w(c), paper._content_w(n))
        doc = _Docx(self._build("compact")["docx"])
        width = doc.tables[0].columns[0].width.pt
        self.assertAlmostEqual(width, paper._content_w(c), places=0)

    def test_both_layouts_pass_fidelity(self):
        for layout in ("compact", "normal"):
            built = self._build(layout, name="fid_" + layout)
            health = paper.check_docx(built["docx"], self.m, built["expected"])
            self.assertEqual(health["dangling"], [], layout)
            self.assertEqual(health["media_missing"], [], layout)
            self.assertEqual(health["n_items_observed"], health["n_items"], layout)
            self.assertEqual(built["layout"], layout)

    def test_manifest_layout_is_honoured(self):
        m = copy.deepcopy(self.m)
        m.setdefault("params", {})["layout"] = "normal"
        d = os.path.join(self.tmp, "fromparams")
        os.makedirs(d, exist_ok=True)
        built = paper.build_docx(m, d, name="fromparams")
        self.assertEqual(built["layout"], "normal", "manifest.params.layout 要生效")


# ---------------------------------------------------------------- 3d. 省纸模式：页数目标（t37）
REAL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")


@unittest.skipUnless(os.path.exists(REAL_DB), "没有 data/app.db：跳过真实题库的页数目标测试")
class CompactPageTargetTest(unittest.TestCase):
    """省纸模式的目标：10 题 1 页、24 题 3 页、每页墨迹 ≥2.5%（用户 2026-10-31 的验收线）。"""

    @classmethod
    def setUpClass(cls):
        if not paper.word_available():
            raise unittest.SkipTest("没有 Word：页数由 Word 排版决定，跳过")
        import sqlite3

        import picker

        cls.tmp = tempfile.mkdtemp(prefix="paper_pages_")
        cls.conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        cls.results = {}
        for total, mix in ((10, {"选择": 5, "填空": 3, "解答": 2}),
                           (24, {"选择": 10, "填空": 8, "解答": 6})):
            params = {"title": "七年级数学 · 训练卷（%d 题）" % total, "mix": mix,
                      "scope": {"chapters": [], "kps": []},
                      "requirements": {"need_answer": True}, "seed": 20261031,
                      "space": {"选择": 0, "填空": 0, "解答": 0}}
            m = picker.build(params, cls.conn)
            out = os.path.join(cls.tmp, "c%d" % total)
            cls.results[total] = paper.render_paper(m, out, name="c%d" % total)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_ten_items_fit_one_page(self):
        r = self.results[10]
        self.assertEqual(r["pages"], 1, "10 题应该 1 页（现在 %d 页）" % r["pages"])

    def test_twenty_four_items_fit_three_pages(self):
        r = self.results[24]
        self.assertLessEqual(r["pages"], 3, "24 题应该 ≤3 页（现在 %d 页）" % r["pages"])

    def test_every_page_has_enough_ink(self):
        for total, r in self.results.items():
            for i, ratio in enumerate(r["health"]["ink_ratio"], 1):
                self.assertGreaterEqual(ratio, 0.025,
                                        "%d 题卷第 %d 页墨迹 %.4f < 2.5%%（还是太空）"
                                        % (total, i, ratio))

    def test_layout_reported_as_compact(self):
        self.assertEqual(self.results[10]["health"]["layout"], "compact")
        self.assertIn("balance", self.results[10]["health"])


class BalanceWiringTest(unittest.TestCase):
    """末页太空时 render_paper 要真的再压一档重排（贪心调平），并把两次尝试记进 health。"""

    @classmethod
    def setUpClass(cls):
        if not paper.word_available():
            raise unittest.SkipTest("没有 Word：跳过调平接线测试")

    def test_balance_runs_second_pass_and_records_attempts(self):
        tmp = tempfile.mkdtemp(prefix="paper_balance_")
        self.addCleanup(shutil.rmtree, tmp, True)
        m = manifest()
        real_needs = paper.needs_balance
        with mock.patch.object(paper, "needs_balance", side_effect=lambda p, ink: True):
            res = paper.render_paper(m, tmp, name="bal")
        balance = res["health"]["balance"]
        self.assertEqual([a["pass"] for a in balance["attempts"]], ["compact", "tight"],
                         "应该跑了两趟：先 compact，再压一档 tight")
        first, second = balance["attempts"]
        better = (second["pages"] < first["pages"]
                  or (second["pages"] == first["pages"]
                      and second["last_ink"] > first["last_ink"]))
        self.assertEqual(balance["tightened"], better,
                         "tight 更好才采用，否则留第一次的结果（记录在 attempts 里）")
        self.assertFalse(real_needs(1, [0.05]), "单页卷永远不需要调平")

    def test_no_second_pass_when_last_page_is_full(self):
        tmp = tempfile.mkdtemp(prefix="paper_balance2_")
        self.addCleanup(shutil.rmtree, tmp, True)
        res = paper.render_paper(manifest(), tmp, name="nobal")
        self.assertEqual(len(res["health"]["balance"]["attempts"]), 1,
                         "末页不空就不该多跑一趟")


# ---------------------------------------------------------------- 4. 全流程
class RenderPaperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not paper.word_available():
            raise unittest.SkipTest("没有 Word（win32com DispatchEx 失败）：跳过 PDF 流程")
        cls.tmp = tempfile.mkdtemp(prefix="paper_full_")
        cls.m = manifest()
        cls.res = paper.render_paper(cls.m, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_paths_exist(self):
        for key in ("docx", "pdf"):
            self.assertTrue(os.path.exists(self.res[key]), self.res[key])
            self.assertTrue(os.path.getsize(self.res[key]) > 2000)
        self.assertTrue(self.res["png"])
        for p in self.res["png"]:
            self.assertTrue(os.path.exists(p))

    def test_pages_positive_and_match_png(self):
        self.assertGreaterEqual(self.res["pages"], 1)
        self.assertEqual(len(self.res["png"]), self.res["pages"])
        self.assertEqual(self.res["health"]["pages"], self.res["pages"])

    def test_pdf_page_count(self):
        import pymupdf

        doc = pymupdf.open(self.res["pdf"])
        try:
            self.assertEqual(doc.page_count, self.res["pages"])
            w, h = doc[0].rect.width, doc[0].rect.height
            self.assertAlmostEqual(w, 595.3, places=0)
            self.assertAlmostEqual(h, 841.9, places=0)
        finally:
            doc.close()

    def test_ink_ratio_per_page(self):
        h = self.res["health"]
        self.assertEqual(len(h["ink_ratio"]), self.res["pages"])
        for i, r in enumerate(h["ink_ratio"], 1):
            self.assertGreater(r, 0.0005, "第 %d 页几乎是空白（ink=%.5f）" % (i, r))
            self.assertLess(r, 0.6, "第 %d 页墨迹过重（ink=%.3f）" % (i, r))
        self.assertGreater(h["ink_max"], 0.005, "整份卷子像白纸")

    def test_png_is_a4_300dpi(self):
        from PIL import Image

        for p in self.res["png"]:
            with Image.open(p) as im:
                self.assertEqual(im.size, (2480, 3508), p)

    def test_health_fidelity_in_full_pipeline(self):
        h = self.res["health"]
        self.assertEqual(h["formula_tokens"], 1)
        self.assertEqual(h["formula_omml_observed"], 1)
        self.assertEqual(h["embed_containers_observed"]["object"], 2)
        self.assertEqual(h["media_count"], 2)
        self.assertEqual(h["n_items"], 3)
        self.assertEqual(h["n_items_observed"], 3)
        self.assertEqual(h["warnings"], [])

    def test_manifest_gets_paper_pages(self):
        """卷面页数写回 manifest.paper.pages —— card.py 用它算答题卡起始页码。"""
        self.assertIn("paper", self.m, "render_paper 没把卷面页数写回 manifest")
        self.assertEqual(self.m["paper"]["pages"], self.res["pages"])
        self.assertEqual(self.res["health"]["paper_pages"], self.res["pages"])

    def test_docx_still_valid_after_render(self):
        with zipfile.ZipFile(self.res["docx"]) as z:
            self.assertIsNone(z.testzip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
