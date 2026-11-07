# -*- coding: utf-8 -*-
"""M0-d · 知识点打标测试：规则命中、优先级、覆盖率。"""
import io
import json
import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from taxonomy import CHAPTER_ORDER, all_kps, classify  # noqa: E402

try:
    import build_db as _bdb
    JSONL = _bdb.JSONL          # 跟着 paths 走（题库搬进 qbank 后不再写死 E:\OCR\dedup）
except Exception:               # noqa: BLE001
    JSONL = r"E:\OCR\dedup\data\questions.jsonl"


class TestRules(unittest.TestCase):
    def test_specific_title_beats_generic(self):
        c = classify("专题01 一元一次方程的解法（200题）（举一反三专项训练）（教师版）.docx")
        self.assertEqual("3.2", c["kp_id"])
        self.assertEqual("第3章 一次方程与方程组", c["chapter"])
        self.assertEqual("title", c["source"])

    def test_abs_value_title(self):
        c = classify("专题1.2 数轴、相反数和绝对值&有理数的大小比较（举一反三讲义）（教师版）.docx")
        self.assertEqual("1.2", c["kp_id"])

    def test_geometry_title(self):
        c = classify("第四章 几何图形初步（举一反三讲义）（教师版）.docx")
        self.assertEqual("4.1", c["kp_id"])

    def test_multiply_title(self):
        c = classify("专题05 有理数的乘法与除法重难点题型专训（5个知识点 10大题型 5大拓展训练 自我检测）.docx")
        self.assertEqual("1.4", c["kp_id"])

    def test_generic_title_falls_back_to_text(self):
        """'数学（解析版）.docx' 没有主题，必须靠题干关键词兜底。"""
        c = classify("数学（解析版）.docx", "如图，在数轴上点A表示的数可能是（　　）")
        self.assertEqual("1.2", c["kp_id"])
        self.assertEqual("text", c["source"])

    def test_generic_title_and_text(self):
        c = classify("真题必刷基础60题（60个考点专练）解析版.docx",
                     "下列计算正确的是（　　）A．3a+2b=5ab")
        self.assertEqual("9.1", c["kp_id"])  # 只有粗分类，题干无更具体线索
        self.assertEqual("综合", c["chapter"])

    def test_unclassified(self):
        c = classify("数学.docx", "已知某班有 40 人。")
        self.assertEqual("9.3", c["kp_id"])
        self.assertEqual("none", c["source"])

    def test_every_kp_has_chapter(self):
        for kp in all_kps():
            self.assertIn(kp["chapter"], CHAPTER_ORDER)
        self.assertGreaterEqual(len(all_kps()), 20)


class TestCorpusCoverage(unittest.TestCase):
    """全量覆盖率：打标率必须够高，否则"按知识点组卷"会缺题。"""

    @classmethod
    def setUpClass(cls):
        rows = [json.loads(l) for l in io.open(JSONL, encoding="utf-8") if l.strip()]
        cls.rows = rows
        cls.tags = [classify(os.path.basename(r["src_file"]), r["text"]) for r in rows]

    def test_coverage(self):
        n = len(self.tags)
        hit = sum(1 for t in self.tags if t["source"] != "none")
        ratio = hit / n
        print(f"\n打标覆盖率 {hit}/{n} = {ratio*100:.1f}%")
        self.assertGreaterEqual(ratio, 0.88, f"覆盖率过低: {ratio:.3f}")

    def test_chapter_distribution(self):
        c = Counter(t["chapter"] for t in self.tags)
        print("\n章节分布:", dict(c))
        # 阈值口径：2026-11-01 起语料是 answered-only 的 12,280 道（用户决定删掉无答案题），
        # 不再是 22,898 份 docx 全量；第 5 章「数据的收集与整理」实测 298 道，
        # 所以门槛从 300 下调到 250 —— 仍然能抓住「某一章整个没标上」这种真问题。
        for ch in ("第1章 有理数", "第2章 整式及其加减", "第3章 一次方程与方程组",
                   "第4章 几何图形初步", "第5章 数据的收集与整理"):
            self.assertGreater(c[ch], 250, f"{ch} 题量过少: {c[ch]}")
        self.assertLess(c["综合"] / len(self.tags), 0.30, "综合类占比过高，按知识点组卷会缺题")

    def test_specific_kp_ratio(self):
        """落到具体知识点（x.y）与章级兜底（x.0）的比例分别统计。

        剩余约 12% 是"期中/期末/压轴"整卷型题目：题干公式被图片化（如只有"计算："），
        纯文本判不出章节，硬塞会给错标签 —— 宁可留在 9.x 综合，等 M3 的 VLM 读题面回填。
        """
        n = len(self.tags)
        spec = sum(1 for t in self.tags if not t["kp_id"].startswith("9.") and not t["kp_id"].endswith(".0"))
        chap = sum(1 for t in self.tags if t["kp_id"].endswith(".0"))
        comp = n - spec - chap
        print(f"\n具体知识点 {spec}/{n} = {spec/n*100:.1f}%；章级兜底 {chap}/{n} = {chap/n*100:.1f}%；"
              f"综合 {comp}/{n} = {comp/n*100:.1f}%")
        self.assertGreaterEqual(spec / n, 0.80)
        self.assertGreaterEqual((spec + chap) / n, 0.87)
        self.assertLessEqual(comp / n, 0.13)

    def test_chapter_hint_mixed(self):
        """'考试范围：第1章~第3章' 是混合范围，不能硬塞给某一章。"""
        from taxonomy import chapter_hint
        self.assertIsNone(chapter_hint("期中易错题专项复习（考试范围：第1~3章）"))
        self.assertEqual("第1章 有理数", chapter_hint("专题1.2 数轴（沪科版）"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
