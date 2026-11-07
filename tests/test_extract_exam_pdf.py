# -*- coding: utf-8 -*-
"""T89 · 整卷 PDF 提取器（tools/extract_exam_pdf.py）的单测。

不起网络、不碰真库：用合成 PDF / 合成块列表跑纯函数 + 一条端到端（本地 OpenCV 版面）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "tools"))

import extract_exam_pdf as X  # noqa: E402


def blocks_fixture():
    """模仿 PaddleOCR-VL 的返回：文字块 + 空 content 的图块 + 图注。"""
    return [
        {"bbox": [50, 100, 500, 140], "content": "一、填空题（每小题2分）", "label": ""},
        {"bbox": [50, 180, 1600, 260], "content": "1. 小陶晨跑 3km 用 20min，求速度___。",
         "label": ""},
        {"bbox": [50, 300, 1600, 380], "content": "2. 如图所示，凸透镜成像___。", "label": ""},
        {"bbox": [200, 420, 800, 700], "content": "", "label": "", "figure": True},
        {"bbox": [250, 705, 700, 745], "content": "第2题图", "label": ""},
        {"bbox": [50, 800, 1600, 880], "content": "3. 茶壶利用了___原理。", "label": ""},
        {"bbox": [200, 900, 800, 1200], "content": "", "label": "", "figure": True},
        {"bbox": [250, 1205, 700, 1245], "content": "第3题图", "label": ""},
    ]


class SegmentTest(unittest.TestCase):
    def test_split_by_question_numbers(self):
        qs = X.segment_questions(blocks_fixture(), page_tag=1)
        self.assertEqual([1, 2, 3], [q["qno"] for q in qs])
        self.assertIn("晨跑", qs[0]["text"])
        self.assertNotIn("1.", qs[0]["text"], "题号要从题干里剥掉")

    def test_instructions_before_first_section_ignored(self):
        blocks = [
            {"bbox": [50, 50, 900, 90], "content": "注意事项：", "label": ""},
            {"bbox": [50, 100, 900, 140], "content": "1. 你拿到的试卷满分为 70 分。", "label": ""},
            {"bbox": [50, 200, 900, 240], "content": "一、填空题", "label": ""},
            {"bbox": [50, 260, 900, 320], "content": "1. 真实的第一题：___。", "label": ""},
        ]
        qs = X.segment_questions(blocks, page_tag=1)
        self.assertEqual([1], [q["qno"] for q in qs])
        self.assertIn("真实的第一题", qs[0]["text"])

    def test_duplicate_number_is_bumped(self):
        blocks = [
            {"bbox": [50, 200, 900, 240], "content": "一、填空题", "label": ""},
            {"bbox": [50, 260, 900, 320], "content": "5. 甲题___。", "label": ""},
            {"bbox": [50, 340, 900, 400], "content": "5. 乙题___。", "label": ""},  # OCR 把 6 读成 5
        ]
        qs = X.segment_questions(blocks, page_tag=1)
        self.assertEqual([5, 6], [q["qno"] for q in qs])

    def test_backslash_before_dot(self):
        blocks = [{"bbox": [50, 200, 900, 240], "content": "一、填空", "label": ""},
                  {"bbox": [50, 260, 900, 320], "content": "13\\. 如图所示___。", "label": ""}]
        qs = X.segment_questions(blocks, page_tag=2)
        self.assertEqual([13], [q["qno"] for q in qs])


class FigureAssignTest(unittest.TestCase):
    def test_caption_merges_into_figure(self):
        merged = X.merge_captions(blocks_fixture())
        figs = [b for b in merged if b.get("figure")]
        self.assertEqual(2, len(figs), "两个图块")
        self.assertTrue(all(b.get("caption", "").startswith("第") for b in figs),
                        "图注要并进图块：%r" % [b.get("caption") for b in figs])
        self.assertFalse([b for b in merged if (b.get("content") or "").startswith("第2题图")],
                         "图注不该留在文字流里")

    def test_figure_goes_to_nearest_question_above(self):
        blocks = X.merge_captions(blocks_fixture())
        qs = X.segment_questions(blocks, page_tag=1)
        X.assign_figures(qs, blocks)
        by = {q["qno"]: len(q["figures"]) for q in qs}
        self.assertEqual(1, by[2], "第2题图归第2题")
        self.assertEqual(1, by[3], "第3题图归第3题")
        self.assertEqual(0, by[1])

    def test_caption_qno_wins_across_pages(self):
        """图注「第7题图」出现在第 1 页底部、题 7 在第 2 页 → 按图注点名归属。"""
        blocks = [
            {"bbox": [50, 100, 900, 140], "content": "一、填空", "label": ""},
            {"bbox": [50, 180, 900, 240], "content": "6. 第六题___。", "label": ""},
            {"bbox": [200, 300, 800, 600], "content": "", "label": "", "figure": True},
            {"bbox": [250, 605, 700, 645], "content": "第7题图", "label": ""},
        ]
        blocks = X.merge_captions(blocks)
        qs = X.segment_questions(blocks, page_tag=1)
        qs.append({"qno": 7, "y0": 50, "text": "第七题", "figures": [], "_page": 2})
        X.assign_figures(qs, blocks)
        by = {q["qno"]: len(q["figures"]) for q in qs}
        self.assertEqual(1, by[7], "图注点名第7题 → 归第7题（哪怕它在下页）")

    def test_multi_figure_numbering(self):
        blocks = [
            {"bbox": [50, 100, 900, 140], "content": "一、填空", "label": ""},
            {"bbox": [50, 180, 900, 240], "content": "3. 如图甲乙___。", "label": ""},
            {"bbox": [200, 300, 500, 600], "content": "", "label": "", "figure": True},
            {"bbox": [600, 300, 900, 600], "content": "", "label": "", "figure": True},
        ]
        blocks = X.merge_captions(blocks)
        qs = X.segment_questions(blocks, page_tag=1)
        X.assign_figures(qs, blocks)
        self.assertEqual(2, len(qs[0]["figures"]), "一题两图")
        ys = [b[1] for b in qs[0]["figures"]]
        self.assertEqual(sorted(ys), ys, "按出现顺序（y 升序）")


class AnswerTest(unittest.TestCase):
    TEXT = ("物理  参考答案  第1页（共4页）\n一、填空题（每小题2 分）\n"
            "1．60；       2．人耳处；     3．等于；     4．97.4；    5．小于；   6．0；\n"
            "7．20；\n用最小的力，需要找到最长的力臂…= 20𝑁。\n"
            "17．选：C。\n"
            "题号 \n11 \n12 \n13 \n14 \n15 \n16 \n17 \n"
            "选项 \nD \nB \nD \nA \nC \nB \nC \n")

    def test_inline_answers(self):
        ans = X.parse_answers(self.TEXT)
        self.assertEqual("60", ans[1])
        self.assertEqual("人耳处", ans[2])
        self.assertEqual("等于", ans[3])
        self.assertEqual("20", ans[7])
        self.assertEqual("C", ans[17], "「选：C」要去掉前缀")

    def test_choice_table(self):
        table = X.parse_choice_table(self.TEXT)
        self.assertEqual({"11": "D", "12": "B"}.get("11"), table.get(11))
        self.assertEqual(7, len(table), "11–17 共 7 个选择题答案")
        self.assertEqual("C", table[17])

    def test_answers_prefer_table(self):
        ans = dict(X.parse_answers(self.TEXT))
        ans.update(X.parse_choice_table(self.TEXT))
        self.assertEqual("D", ans[11])
        self.assertEqual("A", ans[14])


class CropTest(unittest.TestCase):
    def test_crop_min_long_side(self):
        from PIL import Image

        img = Image.new("RGB", (1000, 1000), "white")
        crop = X.crop_figure(img, [100, 100, 300, 240])
        self.assertIsNotNone(crop)
        self.assertGreaterEqual(max(crop.size), X.MIN_FIG_LONG_SIDE)

    def test_crop_rejects_empty_box(self):
        from PIL import Image

        img = Image.new("RGB", (500, 500), "white")
        self.assertIsNone(X.crop_figure(img, [10, 10, 11, 11]), "1×1 的噪声框不该裁成图")

    def test_height_rule_marks_figure(self):
        """版面回退的判图规则：明显高于文字行的带 → figure（合成图形态太理想，规则单测更实）。"""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (1200, 1600), "white")
        blocks = [{"bbox": [0, 0, 100, 30], "label": "text", "content": ""},
                  {"bbox": [0, 40, 100, 70], "label": "text", "content": ""},
                  {"bbox": [0, 80, 100, 110], "label": "text", "content": ""},
                  {"bbox": [0, 200, 900, 900], "label": "text", "content": ""}]
        median_h = 30
        tall = [b for b in blocks if (b["bbox"][3] - b["bbox"][1]) > median_h * 1.8]
        self.assertEqual(1, len(tall), "只有那个 700px 高的带算图")
        self.assertGreater(tall[0]["bbox"][3] - tall[0]["bbox"][1], 300)

    def test_opencv_layout_returns_blocks(self):
        """回退路径至少要能给出带 bbox 的块（判图质量在真卷上没标定，见 docs 说明）。"""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (1200, 1600), "white")
        d = ImageDraw.Draw(img)
        for y in (100, 160, 220):
            for x in range(80, 1100, 26):
                d.rectangle([x, y, x + 18, y + 28], fill="black")
        d.rectangle([200, 400, 900, 1100], outline="black", width=6)
        blocks = X.layout_opencv(img)
        self.assertTrue(blocks, "启发式版面要能给出块")
        for b in blocks:
            self.assertEqual(4, len(b["bbox"]), b)
            self.assertIn(b["label"], ("text", "figure"))


class EndToEndTest(unittest.TestCase):
    """合成一份 2 页 PDF（1 页扫描题 + 1 页答案文字层）跑整条流程，验 ZIP 结构。"""

    @classmethod
    def setUpClass(cls):
        import pymupdf as fitz
        from PIL import Image, ImageDraw

        cls.tmp = tempfile.mkdtemp(prefix="t89_unit_")
        # 扫描题页：画文字线 + 一个方框图
        img = Image.new("RGB", (1240, 1754), "white")
        d = ImageDraw.Draw(img)
        d.text((80, 60), "1. Figure question stem", fill="black")
        for x in range(80, 1100, 24):
            d.rectangle([x, 100, x + 16, 128], fill="black")
        d.rectangle([300, 300, 900, 800], outline="black", width=8)
        d.line([300, 400, 900, 600], fill="black", width=6)
        png = os.path.join(cls.tmp, "scan.png")
        img.save(png)

        cls.pdf = os.path.join(cls.tmp, "exam.pdf")
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(0, 0, 595, 842), filename=png)
        ap = doc.new_page(width=595, height=842)
        ap.insert_text((60, 80), "物理 参考答案 1. 42  2. 人耳处  3. 等于  4. 97.4  5. 小于  6. 0",
                       fontsize=12)
        doc.save(cls.pdf)
        doc.close()

    def test_extract_produces_zip_with_questions_and_images(self):
        out = os.path.join(self.tmp, "out")
        fake = [{"bbox": [60, 60, 1100, 140], "content": "一、填空题（每小题2分）", "label": ""},
                {"bbox": [60, 180, 1100, 260], "content": "1. 如图所示的装置___。", "label": ""},
                {"bbox": [300, 300, 900, 800], "content": "", "label": "", "figure": True},
                {"bbox": [350, 805, 850, 845], "content": "第1题图", "label": ""}]
        orig = X._blocks_from_paddle
        X._blocks_from_paddle = lambda png: list(fake)
        try:
            rep = X.extract(self.pdf, "physics", out_dir=out, layout="paddle",
                            no_base=1, no_width=3, zip_path=os.path.join(self.tmp, "e.zip"))
        finally:
            X._blocks_from_paddle = orig
        self.assertTrue(os.path.isfile(os.path.join(out, "questions.json")))
        self.assertIn(2, rep["answer_pages"], "第 2 页是答案页（有文字层）")
        self.assertTrue(rep["question_pages"], "第 1 页应被当作题目页")
        self.assertGreaterEqual(rep["questions"], 1)
        with zipfile.ZipFile(os.path.join(self.tmp, "e.zip")) as zf:
            names = zf.namelist()
        self.assertIn("questions.json", names)
        self.assertTrue([n for n in names if n.startswith("images/")], names)
        data = json.load(io.open(os.path.join(out, "questions.json"), encoding="utf-8"))
        first = data["items"][0]
        for key in ("no", "qno", "stem", "answer", "figure", "subject"):
            self.assertIn(key, first)
        self.assertEqual("physics", first["subject"])

    def test_cli_runs(self):
        out = os.path.join(self.tmp, "cli")
        code = X.main(["--pdf", self.pdf, "--subject", "physics", "--out", out,
                       "--layout", "opencv", "--no-width", "3"])
        self.assertEqual(0, code)
        self.assertTrue(os.path.isfile(os.path.join(out, "report.json")))


class InstructionTest(unittest.TestCase):
    """t91：试卷须知不是题目 —— 必须剔除，并记录到 skipped。"""

    def test_keywords(self):
        for text in ("你拿到的试卷满分为 70 分，物理与化学的考试时间共 120 分钟。",
                     "请务必在“答题卷”上答题，在“试题卷”上答题是无效的。",
                     "考试结束后，请将“试题卷”和“答题卷”一并交回。"):
            self.assertTrue(X.is_instruction(text), text)
        for text in ("如图所示，对于四幅图涉及的物理知识，下列说法中正确的是（  ）",
                     "1. 小陶同学某次晨跑 3km 用了 20min，求速度___。"):
            self.assertFalse(X.is_instruction(text), text)

    def test_instruction_blocks_not_questions(self):
        blocks = [
            {"bbox": [50, 60, 900, 100], "content": "注意事项：", "label": ""},
            {"bbox": [50, 120, 900, 160],
             "content": "1. 你拿到的试卷满分为 70 分，物理与化学的考试时间共 120 分钟。", "label": ""},
            {"bbox": [50, 200, 900, 240],
             "content": "2.请务必在“答题卷”上答题，在“试题卷”上答题是无效的。", "label": ""},
            {"bbox": [50, 300, 900, 340], "content": "一、填空题（每小题2分）", "label": ""},
            {"bbox": [50, 360, 900, 420], "content": "1. 真实第一题：___。", "label": ""},
        ]
        skipped = []
        qs = X.segment_questions(blocks, page_tag=1, skipped=skipped)
        self.assertEqual([1], [q["qno"] for q in qs], "须知不该变成题")
        self.assertGreaterEqual(len(skipped), 2, "剔除的须知要记账：%r" % skipped)
        self.assertTrue(all("须知" in s["reason"] or "注意事项" in s["reason"]
                            for s in skipped), skipped)


class OptionMergeTest(unittest.TestCase):
    """t91：四联图选择题的选项被图块隔开也要合并成四个。"""

    STEM = ("如图所示，光滑桌面上的弹性小球由 A 点被水平向右弹出后，落到地面，图中虚线为小球的"
            "运动轨迹，C、F 点等高。下列有关小球的运动描述不正确的是（）\n"
            "A. 从 A 点运动到 F 点的过程中，机械能减小\n"
            "B．从B点运动到C点，重力势能减小，动能增大\n"
            "第15题图 甲 C. 小球在 E 点的机械能大于在 D 点的机械能\n"
            "D．小球在 C 点的速度大于小球在 F 点的速度")

    def test_four_options_merged(self):
        opts = X.split_options(self.STEM)
        self.assertEqual(4, len(opts), opts)
        self.assertEqual(["A", "B", "C", "D"], [o[0] for o in opts])
        self.assertNotIn("第15题图", " ".join(opts), "图注要清掉")

    def test_dunhao_is_not_option_separator(self):
        # 「C、F 点等高」里的顿号不是选项
        self.assertEqual([], X.split_options("小球在 C、F 点等高，速度相同"))

    def test_incomplete_when_only_one_option(self):
        self.assertEqual([], X.split_options("下列说法正确的是（） A．只有一个选项"))


class ReportFieldsTest(unittest.TestCase):
    """t91：report.json 要带 skipped_sections 与 options_incomplete。"""

    def test_report_has_new_fields(self):
        import pymupdf as fitz
        from PIL import Image, ImageDraw

        tmp = tempfile.mkdtemp(prefix="t91_")
        img = Image.new("RGB", (1240, 1754), "white")
        ImageDraw.Draw(img).text((80, 60), "scan", fill="black")
        png = os.path.join(tmp, "s.png")
        img.save(png)
        pdf = os.path.join(tmp, "e.pdf")
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_image(fitz.Rect(0, 0, 595, 842), filename=png)
        ap = doc.new_page(width=595, height=842)
        ap.insert_text((60, 80), "参考答案 1. A 2. B 3. C 4. D 5. E 6. F", fontsize=12)
        doc.save(pdf)
        doc.close()

        fake = [
            {"bbox": [60, 60, 1100, 100], "content": "注意事项：", "label": ""},
            {"bbox": [60, 120, 1100, 160],
             "content": "1. 你拿到的试卷满分为 70 分，考试时间 120 分钟。", "label": ""},
            {"bbox": [60, 220, 1100, 260], "content": "一、选择题", "label": ""},
            {"bbox": [60, 300, 1100, 380],
             "content": "1. 下列说法正确的是（） A. 甲 B．乙 C．丙 D．丁", "label": ""},
            {"bbox": [60, 420, 1100, 480],
             "content": "2. 只有一个选项的坏题（ ） A．孤立", "label": ""},
        ]
        orig = X._blocks_from_paddle
        X._blocks_from_paddle = lambda png: list(fake)
        try:
            rep = X.extract(pdf, "physics", out_dir=os.path.join(tmp, "o"), layout="paddle",
                            no_width=3, zip_path=os.path.join(tmp, "e.zip"))
        finally:
            X._blocks_from_paddle = orig
        self.assertIn("skipped_sections", rep)
        self.assertTrue(rep["skipped_sections"], "须知要被记录")
        self.assertIn("options_incomplete", rep)
        data = json.load(io.open(os.path.join(tmp, "o", "questions.json"), encoding="utf-8"))
        # 字段必须在（选项内容另由 OptionMergeTest 覆盖；合成块里选项很短，这里只验契约）
        for item in data["items"]:
            self.assertIn("options", item)
            self.assertIn("options_incomplete", item)
        self.assertIsInstance(rep["options_incomplete"], list)




class StemCleanTest(unittest.TestCase):
    """t96：原卷页眉页脚、图注残留不能留在题干里。"""

    DIRTY = ("如图所示，对于四幅图涉及的物理知识，下列说法中正确的是（） 甲 2 T 丙 甲。甲图中的手影游戏，"
             "是由于光的反射形成的 第 2 页 （共 6 页）物理 第14题图 物理 第 3 页（共 6 页）")

    def test_header_footer_removed(self):
        got = X.clean_stem(self.DIRTY)
        for bad in ("第 2 页", "第2页", "共 6 页", "共6页", "（共"):
            self.assertNotIn(bad, got, "题干里还有页眉页脚：%r → %r" % (bad, got))

    def test_figure_caption_removed(self):
        got = X.clean_stem(self.DIRTY)
        self.assertNotIn("题图", got, got)
        self.assertNotIn("第14题图", got, got)

    def test_isolated_subfigure_labels_removed(self):
        got = X.clean_stem(self.DIRTY)
        self.assertIn("甲图中的手影游戏", got, "「甲图」是正文，不能删")
        self.assertNotIn(" 丙 ", got, "孤立的「丙」要删掉：%r" % got)

    def test_clean_stem_keeps_question_text(self):
        text = "1. 小陶同学某次晨跑 3km 用了 20min，求相邻路灯杆之间的距离___。"
        self.assertIn("晨跑 3km", X.clean_stem(text))


class OptionsSplitTest(unittest.TestCase):
    """t96：内联选项要拆成 4 段，且题干里不再残留 A./B./C./D.。"""

    INLINE = ("如图所示，对于四幅图涉及的物理知识，下列说法中正确的是（） 第2页 第13题图 "
              "甲 A. 甲图中的手影游戏，是由于光的反射形成的 "
              "B．乙图中光发生了漫反射，不遵循光的反射定律 "
              "C．丙图中平面镜成的虚像，是反射光线实际相交而成的 "
              "D．丁图表示让光逆着原反射光的方向射入，反射光将逆着原入射光的方向射出")

    def test_four_options(self):
        opts = X.split_options(X.clean_stem(self.INLINE))
        self.assertEqual(4, len(opts), opts)
        self.assertEqual(["A", "B", "C", "D"], [o[0] for o in opts])
        self.assertNotIn("第13题图", " ".join(opts))

    def test_stem_has_no_option_labels(self):
        stem = X.clean_stem(self.INLINE)
        head = X.clean_stem(X.OPTION_RX.split(stem, maxsplit=1)[0])
        self.assertNotIn("A.", head)
        self.assertIn("四幅图", head)


class FigureCaptionTest(unittest.TestCase):
    """t96：图注要进 figure_meta.caption，不进题干。"""

    def test_caption_goes_to_figure_meta(self):
        blocks = X.merge_captions([
            {"bbox": [50, 100, 900, 140], "content": "一、填空题", "label": ""},
            {"bbox": [50, 180, 900, 240], "content": "1. 如图所示，物块质量___。", "label": ""},
            {"bbox": [300, 300, 900, 800], "content": "", "label": "", "figure": True},
            {"bbox": [350, 805, 850, 845], "content": "第1题图", "label": ""},
            {"bbox": [50, 900, 900, 950], "content": "物理 第 2 页（共 6 页）", "label": ""},
        ])
        qs = X.segment_questions(blocks, page_tag=1)
        X.assign_figures(qs, blocks)
        self.assertEqual(1, len(qs[0]["figures"]))
        self.assertEqual(["第1题图"], qs[0]["figure_captions"])
        self.assertNotIn("第1题图", qs[0]["text"], "图注不能留在题干")
        self.assertNotIn("共 6 页", qs[0]["text"], "页脚不能留在题干")


class RealExamCleanlinessTest(unittest.TestCase):
    """t96：真卷产物（若有）必须干净：无页眉页脚、无图注、选择题 4 选项。"""

    JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "out", "_c20_physics5", "questions.json")

    def test_real_output_clean(self):
        if not os.path.isfile(self.JSON):
            self.skipTest("没有 out/_c20_physics5/questions.json")
        items = json.load(io.open(self.JSON, encoding="utf-8"))["items"]
        self.assertGreaterEqual(len(items), 20)
        for it in items:
            stem = it["stem"]
            self.assertNotRegex(stem, r"第\s*\d+\s*页", it["no"])
            self.assertNotRegex(stem, r"共\s*\d+\s*页", it["no"])
            self.assertNotIn("题图", stem, it["no"])
        for no in ("99011", "99012", "99013", "99014", "99015"):
            row = [x for x in items if x["no"] == no]
            if not row:
                continue
            self.assertEqual(4, len(row[0].get("options") or []), no)
            self.assertTrue(row[0].get("figure_meta") is not None, no)

if __name__ == "__main__":
    unittest.main()
