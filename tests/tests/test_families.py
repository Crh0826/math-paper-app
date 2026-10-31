# -*- coding: utf-8 -*-
"""M0-e · 题族抽取测试。"""
import io
import json
import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from families import family_key, parse_label  # noqa: E402

JSONL = r"E:\OCR\dedup\data\questions.jsonl"


class TestParseLabel(unittest.TestCase):
    def test_example(self):
        self.assertEqual(("例10", "example", "例10"), parse_label("【例10】（2022·全国）某同学解方程…"))
        self.assertEqual(("例3", "example", "例3"), parse_label("【例 3】(2023·河北) …"))

    def test_variant(self):
        self.assertEqual(("例4", "variant", "变式4-1"), parse_label("【变式4-1】（25-26七年级上·湖南衡阳·期中）…"))
        self.assertEqual(("例10", "variant", "变式10-2"),
                         parse_label("【变式 10 - 2】（2023·河北）已知关于x的方程…"))

    def test_no_label(self):
        self.assertIsNone(parse_label("1．（2024七年级上·安徽安庆）指出图中各物体…"))
        self.assertIsNone(parse_label(""))

    def test_label_must_be_in_head(self):
        """标签出现在题干很后面时不算（避免"变式"二字在正文里）"""
        text = "某题" + "填充" * 80 + "【例9】"
        self.assertIsNone(parse_label(text))


class TestFamilyKey(unittest.TestCase):
    def test_same_source_same_family(self):
        a = family_key("【例4】（…）题干", "专题01 整式（教师版）.docx")
        b = family_key("【变式4-2】（…）题干", "专题01 整式（教师版）.docx")
        self.assertIsNotNone(a)
        self.assertEqual(a, b)

    def test_different_source_different_family(self):
        a = family_key("【例4】…", "A.docx")
        b = family_key("【例4】…", "B.docx")
        self.assertNotEqual(a, b)

    def test_plain_question_has_no_family(self):
        self.assertIsNone(family_key("1．下列计算正确的是（　）", "A.docx"))


class TestCorpusFamilies(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [json.loads(l) for l in io.open(JSONL, encoding="utf-8") if l.strip()]
        fam = {}
        for r in rows:
            k = family_key(r["text"], os.path.basename(r["src_file"]))
            if k:
                fam.setdefault(k, []).append(r["no"])
        cls.rows = rows
        cls.fam = fam
        sizes = sorted((len(v) for v in fam.values()), reverse=True)
        cls.sizes = sizes

    def test_family_count(self):
        print(f"\n题族数 {len(self.fam)}，成员总数 {sum(self.sizes)}，平均 {sum(self.sizes)/len(self.sizes):.2f}")
        self.assertGreaterEqual(len(self.fam), 2000)

    def test_families_usable_for_training(self):
        """至少 400 个族有 3 题以上（错题 + 2 道变式即可组训练卷）。"""
        n3 = sum(1 for s in self.sizes if s >= 3)
        print(f"≥3 题的族: {n3}")
        self.assertGreaterEqual(n3, 400)

    def test_no_family_spans_sources(self):
        for k in self.fam:
            self.assertIn("::", k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
