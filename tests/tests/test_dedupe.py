# -*- coding: utf-8 -*-
"""t23 · 近重复检测测试（先红后绿）。

手工样本：3 组近重复（① 标点/空格/全角差异 ② 长题干改一个数字 J≈0.87 ③ 带【例N】标签且改数字）
+ 4 道互不相同的题（必须各自独立，不能并进任何组）。

Jaccard 是按「规范化题干的前 120 字 + 字符 5-gram」算的：
  - 标点/空格/全角差异 → 规范化后完全相同 → J=1.0
  - 长题干改 1 个数字 → J≈0.87（≥0.85 阈值，仍算近重复）
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import db as dbmod  # noqa: E402
import dedupe  # noqa: E402

REAL_DB = os.path.join(APP, "data", "app.db")

INSERT = ("INSERT OR REPLACE INTO questions(no, qtype, option_count, sub_count, is_empty,"
          " media_count, text_len, text_plain, tokens_json, fmt, src_title, src_para,"
          " family_key, kp_id, chapter, kp, kp_source, answer_raw, check_status)"
          " VALUES(?,?,0,0,0,0,?,?,?,'plain','',0,NULL,'1.2','第1章 有理数','','title',?,'')")

# —— 组 A：标点/空格/全角差异（规范化后完全相同，J=1.0）
A1 = "小明去文具店买了3支铅笔和2本笔记本，铅笔每支2元，笔记本每本5元，请问小明一共花了多少元？请写出完整的计算过程．"
A2 = "小明去文具店买了３支铅笔和２本笔记本,铅笔每支２元,笔记本每本５元,请问小明一共花了多少元?请写出完整的计算过程"
A3 = "小明去文具店买了 3 支铅笔和 2 本笔记本， 铅笔每支 2 元， 笔记本每本 5 元， 请问小明一共花了多少元？请写出完整的计算过程"

# —— 组 B：长题干只改一个数字（J≈0.87）
B1 = ("某校七年级组织学生参加社会实践活动，共有学生120人，老师8人，租用客车前往实践基地，"
      "每辆客车可坐45人，问至少需要租用多少辆客车？请写出完整的计算过程并说明理由．")
B2 = B1.replace("120人", "150人")

# —— 组 C：带【例N】标签 + 改一个数字 + 句末标点不同
C_BASE = ("【例8】某工厂计划生产一批零件，原计划每天生产60个，需要15天完成；"
          "实际每天比原计划多生产15个，已知每名工人每小时可以生产5个零件，"
          "问实际需要多少天完成？请写出完整的计算过程．")
C1 = C_BASE
C2 = C_BASE.replace("60个", "80个")
C3 = C_BASE.replace("．", "。")

# —— 非重复：4 道完全不同的题
D1 = "下列说法中，正确的是（　）A．0 是最小的整数 B．相反数等于本身的数只有 0"
D2 = "如图，直线 AB 与 CD 相交于点 O，若∠AOC=40°，则∠BOD 的度数是（　）"
E1 = "计算：（﹣2）³ + |﹣5| ﹣ 3×（﹣1）²"
E2 = "先化简，再求值：2（a²b+ab²）﹣（a²b﹣ab²），其中 a=﹣1，b=2"

SAMPLE = {"A1": A1, "A2": A2, "A3": A3, "B1": B1, "B2": B2,
          "C1": C1, "C2": C2, "C3": C3, "D1": D1, "D2": D2, "E1": E1, "E2": E2}


def add(conn, no, text, qtype="解答", tokens=None):
    conn.execute(INSERT, (no, qtype, len(text or ""), text or "",
                          json.dumps(tokens or [], ensure_ascii=False), "3"))


def math_tok(expr):
    return {"kind": "math", "xml": "<m:oMath><m:r><m:t>%s</m:t></m:r></m:oMath>" % expr}


def embed_tok(w=40.0, h=15.0):
    return {"kind": "embed", "xml": "<w:drawing/>", "width_pt": w, "height_pt": h, "n_media": 1}


def build_fixture(conn):
    for key, text in SAMPLE.items():
        add(conn, key, text)
    # 太短的题干（<5 字）没有 5-gram，不该进分组表
    add(conn, "SHORT", "计算：")


class TestNormalizeStem(unittest.TestCase):
    def test_fullwidth_and_spaces(self):
        self.assertEqual(dedupe.normalize_stem("ＡＢＣ　１２３"), "ABC123")

    def test_strips_placeholders_and_labels(self):
        self.assertEqual(dedupe.normalize_stem("【例3】解方程 ⟨式⟩，并检验．"), "解方程并检验")

    def test_caps_at_120_chars(self):
        self.assertLessEqual(len(dedupe.normalize_stem("数" * 500)), 120)


class TestJaccardAndSignature(unittest.TestCase):
    def test_jaccard_identity_and_disjoint(self):
        g1 = dedupe.shingle_hashes(dedupe.normalize_stem(A1))
        g2 = dedupe.shingle_hashes(dedupe.normalize_stem(A2))
        g3 = dedupe.shingle_hashes(dedupe.normalize_stem(D1))
        self.assertEqual(1.0, dedupe.jaccard(g1, g2))
        self.assertEqual(0.0, dedupe.jaccard(g1, g3))

    def test_number_change_is_near_but_not_equal(self):
        g1 = dedupe.shingle_hashes(dedupe.normalize_stem(B1))
        g2 = dedupe.shingle_hashes(dedupe.normalize_stem(B2))
        j = dedupe.jaccard(g1, g2)
        self.assertGreaterEqual(j, 0.85, "长题干改一个数字应该仍算近重复，实际 %.3f" % j)
        self.assertLess(j, 1.0)

    def test_signature_is_deterministic(self):
        g = dedupe.shingle_hashes(dedupe.normalize_stem(A1))
        s1, s2 = dedupe.signature(g), dedupe.signature(g)
        self.assertEqual(s1, s2)
        self.assertEqual(64, len(s1))
        other = dedupe.signature(dedupe.shingle_hashes(dedupe.normalize_stem(D2)))
        self.assertNotEqual(s1, other)

    def test_signature_estimate_tracks_jaccard(self):
        g1 = dedupe.shingle_hashes(dedupe.normalize_stem(B1))
        g2 = dedupe.shingle_hashes(dedupe.normalize_stem(B2))
        self.assertLess(dedupe.signature_jaccard(dedupe.signature(g1), dedupe.signature(g2)),
                        1.0)
        self.assertGreater(dedupe.signature_jaccard(dedupe.signature(g1), dedupe.signature(g2)),
                           0.7)


class TestScanFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dedupe_")
        cls.db = os.path.join(cls.tmp, "d.db")
        cls.conn = dbmod.connect(cls.db)
        build_fixture(cls.conn)
        cls.conn.commit()
        cls.res = dedupe.scan(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _gid(self, no):
        row = self.conn.execute("SELECT group_id FROM dup_groups WHERE no=?", (no,)).fetchone()
        return None if row is None else row[0]

    def test_three_groups(self):
        self.assertEqual(3, self.res["groups"], "应该正好 3 组近重复")
        self.assertEqual(8, self.res["affected"], "A3+B2+C3 = 8 题")

    def test_group_membership(self):
        self.assertEqual(self._gid("A1"), self._gid("A2"))
        self.assertEqual(self._gid("A1"), self._gid("A3"))
        self.assertEqual(self._gid("B1"), self._gid("B2"))
        self.assertEqual(self._gid("C1"), self._gid("C2"))
        self.assertEqual(self._gid("C1"), self._gid("C3"))

    def test_distinct_questions_not_grouped(self):
        grouped = {self._gid("D1"), self._gid("D2"), self._gid("E1"), self._gid("E2")}
        self.assertEqual({None}, grouped, "4 道不同的题都不该进组")
        self.assertNotEqual(self._gid("A1"), self._gid("C1"))
        self.assertNotEqual(self._gid("A1"), self._gid("B1"))

    def test_group_sizes_and_rep(self):
        for no, size in (("A1", 3), ("B1", 2), ("C1", 3)):
            row = self.conn.execute("SELECT size, rep_no FROM dup_groups WHERE no=?",
                                    (no,)).fetchone()
            self.assertEqual(size, row["size"])
            self.assertEqual(self._gid(no), self._gid(row["rep_no"]),
                             "代表题号必须是同组成员")

    def test_short_question_skipped(self):
        self.assertIsNone(self._gid("SHORT"), "不足 5 字的题干没有 5-gram，不该分组")

    def test_same_group_api(self):
        self.assertTrue(dedupe.same_group(self.conn, "A1", "A3"))
        self.assertTrue(dedupe.same_group(self.conn, "B1", "B2"))
        self.assertFalse(dedupe.same_group(self.conn, "A1", "D1"))
        self.assertFalse(dedupe.same_group(self.conn, "A1", "NOPE"))
        self.assertFalse(dedupe.same_group(self.conn, "NOPE", "NOPE2"))

    def test_groups_of_api(self):
        out = dedupe.groups_of(self.conn, ["A1", "A2", "D1"])
        self.assertEqual(1, len(out), "D1 没组，不该出现在结果里")
        (gid, members), = out.items()
        self.assertEqual(["A1", "A2", "A3"], sorted(members))
        self.assertEqual(self._gid("A1"), gid)

    def test_report_shape(self):
        res = self.res
        for k in ("groups", "affected", "elapsed_s", "top_groups", "threshold",
                  "scanned", "candidate_pairs", "linked_pairs"):
            self.assertIn(k, res)
        self.assertLessEqual(len(res["top_groups"]), 10)
        top = [g for g in res["top_groups"] if g["size"] == 3][0]
        self.assertLessEqual(len(top["nos"]), 3)
        self.assertTrue(all(len(s) <= 40 for s in top["stems"]), "示例题干最多 40 字")

    def test_scan_writes_file_report(self):
        path = os.path.join(self.tmp, "r.json")
        dedupe.write_report(self.res, path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(3, data["groups"])
        self.assertEqual(8, data["affected"])

    def test_rescan_stable(self):
        again = dedupe.scan(self.conn)
        self.assertEqual(self.res["groups"], again["groups"])
        self.assertEqual(self.res["affected"], again["affected"])
        self.assertEqual(self._gid("A1"), self._gid("A2"), "重扫后分组不能变")

    def test_threshold_parameter_splits_number_edits(self):
        strict = dedupe.scan(self.conn, threshold=0.95)
        # 阈值 0.95：标点/空格组（J=1.0）还留两组（A 3 题、C1+C3 2 题），
        # 改数字的 B1/B2 与 C2 被拆出去
        self.assertEqual(2, strict["groups"])
        self.assertEqual(5, strict["affected"])
        self.assertIsNone(self.conn.execute(
            "SELECT group_id FROM dup_groups WHERE no='B1'").fetchone())


class TestContentGate(unittest.TestCase):
    """题干骨架相同但公式/图片不同的题，不是重复题，不能被合并。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dedupe_gate_")
        cls.conn = dbmod.connect(os.path.join(cls.tmp, "g.db"))
        # 同一句骨架，公式不同 → 不该合并
        for no, expr in (("M1", "x+1=3"), ("M2", "x+1=3"), ("M3", "2x-7=9")):
            add(cls.conn, no, "计算下列方程：⟨式⟩，并把解写在横线上．", tokens=[math_tok(expr)])
        # 同一句骨架，图片公式尺寸相同但媒体不同 → 靠 media_fp 区分
        for no in ("E1", "E2", "E3"):
            add(cls.conn, no, "如图，求阴影部分的面积⟨图⟩．请写出计算过程．", tokens=[embed_tok()])
        cls.conn.commit()
        fake = {"E1": ("sha-a",), "E2": ("sha-b",), "E3": ("sha-a",)}
        cls.res = dedupe.scan(cls.conn, media_fp=lambda no: fake.get(no))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _gid(self, no):
        row = self.conn.execute("SELECT group_id FROM dup_groups WHERE no=?", (no,)).fetchone()
        return None if row is None else row[0]

    def test_same_formula_grouped(self):
        self.assertIsNotNone(self._gid("M1"))
        self.assertEqual(self._gid("M1"), self._gid("M2"))

    def test_different_formula_not_grouped(self):
        self.assertIsNone(self._gid("M3"), "公式不同（2x-7=9）不能并进 x+1=3 的组")

    def test_different_image_bytes_not_grouped(self):
        self.assertIsNotNone(self._gid("E1"))
        self.assertEqual(self._gid("E1"), self._gid("E3"), "媒体字节相同应合并")
        self.assertIsNone(self._gid("E2"), "媒体字节不同（不同公式图）不能合并")
        self.assertGreaterEqual(self.res["media_rejected_pairs"], 1)

    def test_text_only_mode_over_merges(self):
        """关掉内容门控（只按题干骨架）会把公式不同的题也并进来 —— 这才是默认要开门的理由。"""
        fake = {"E1": ("sha-a",), "E2": ("sha-b",), "E3": ("sha-a",)}
        loose = dedupe.scan(self.conn, content_gate=False, media_fp=lambda no: fake.get(no))
        self.assertIsNotNone(self._gid("M3"), "只按题干分组时 M3（公式不同）会被误并")
        self.assertIsNotNone(self._gid("E2"), "只按题干分组时 E2（图片不同）会被误并")
        self.assertGreaterEqual(loose["text_only_groups"], 2)
        # 恢复严格的表，别污染同类里其它用例
        dedupe.scan(self.conn, media_fp=lambda no: fake.get(no))
        self.assertIsNone(self._gid("M3"), "重扫后严格口径要把它剔除")


class TestRealLibrary(unittest.TestCase):
    """真实 22,898 题：全库近重复分组（只读 + write=False，不污染 app.db）。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL_DB):
            raise unittest.SkipTest("app.db 不存在")

    def test_full_scan(self):
        import time
        conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        try:
            t0 = time.time()
            res = dedupe.scan(conn, write=False)
            elapsed = time.time() - t0
        finally:
            conn.close()
        self.assertEqual(12280, res["scanned"])  # t25 裁剪后：只保留有答案的题
        self.assertLess(elapsed, 60.0, "全库近重复检测太慢：%.1fs" % elapsed)
        # t25 裁剪后（只留有答案题）：真重复为 0；只剩「题干骨架相同、公式不同」的假阳性，
        # 这正是内容门控要挡住的，text_only 口径仍能看到它们。
        self.assertEqual(0, res["groups"], "裁剪后不该还有真重复")
        self.assertGreater(res["text_only_groups"], 0, "只按题干比会误并出一堆")
        self.assertGreaterEqual(res["affected"], 0)

    def test_known_dups_and_non_dups_from_real_db(self):
        """真实库：真重复（题干+公式都一样）判重复；同骨架不同公式判不重复。"""
        conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        try:
            # 00088/00136：题干一字不差（J=1.0），但公式不同 → 不是重复题
            strict, sim = dedupe.are_similar(conn, "00088", "00136")
            self.assertEqual(1.0, round(sim, 3))
            self.assertFalse(strict, "同题干但公式不同，不能判重复")
            loose, _ = dedupe.are_similar(conn, "00088", "00136", content_gate=False)
            self.assertTrue(loose, "关掉内容门控才会把它们当重复（说明门控在起作用）")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
