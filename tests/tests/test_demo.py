# -*- coding: utf-8 -*-
"""t33 · 一键演示 demo.py 的冒烟测试。

只断言「跑得通 + 产物齐全 + 库不脏」，不重复验证各模块自己的正确性（那些在各自套件里）。

为了**不污染真库**（也避免跟 data-engineer 的裁剪打架），这里用 `app/data/app.db` 复制出一个
只含几十道题的小库：questions 按题型各取若干 + families/kps/quality_flags/meta 全量（都很小）。
demo 默认会清理自己写进 papers/submissions/mastery 的记录，测试顺带验证"库没变脏"。

跑一次约 8~12 秒（`--fast`：3+2+1 题、训练卷只出 docx，只做 1 次 Word 往返）。
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import db  # noqa: E402
import demo  # noqa: E402
import picker  # noqa: E402
import render  # noqa: E402

REAL_DB = os.path.join(APP, "data", "app.db")
SUBSET = {"选择": 24, "填空": 14, "解答": 10}


def _copy_table(src, dst, table, where="", args=()):
    """整表（或带条件）复制；两库表结构一致，按源表列名动态拼 INSERT。"""
    try:
        cur = src.execute("SELECT * FROM %s%s" % (table, (" WHERE " + where) if where else ""),
                          args)
    except sqlite3.Error:
        return 0
    rows = cur.fetchall()
    if not rows:
        return 0
    cols = [d[0] for d in cur.description]
    sql = "INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (
        table, ", ".join(cols), ", ".join("?" * len(cols)))
    dst.executemany(sql, [tuple(r) for r in rows])
    return len(rows)


def make_subset_db(real_db: str, dst_db: str) -> str:
    """从真库复制一个小库：三种题型各取若干（有答案、非空题）+ 支撑表全量。"""
    src = sqlite3.connect("file:%s?mode=ro" % real_db.replace("\\", "/"), uri=True)
    src.row_factory = sqlite3.Row
    dst = db.connect(dst_db)                       # 建好 schema（含 papers/submissions/mastery）
    try:
        for qtype, limit in SUBSET.items():
            _copy_table(
                src, dst, "questions",
                "qtype=? AND is_empty=0 AND answer_raw<>'' AND tokens_json<>'' "
                "ORDER BY no LIMIT ?", (qtype, limit))
        for table in ("families", "kps", "quality_flags", "meta"):
            _copy_table(src, dst, table)
        # 只留本次复制到的题的体检标记，小库更干净
        dst.execute("DELETE FROM quality_flags WHERE no NOT IN (SELECT no FROM questions)")
        dst.commit()
        counts = {t: dst.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                  for t in ("questions", "families", "kps")}
        if counts["questions"] < 6:
            raise unittest.SkipTest("真库 questions 里的可用题太少（%r），跳过 demo 测试" % counts)
    finally:
        src.close()
        dst.close()
    return dst_db


class DemoFailurePathTest(unittest.TestCase):
    """失败路径：给可执行建议、不甩栈、退出码 1（不需要 Word）。"""

    def test_missing_db_gives_actionable_advice(self):
        tmp = tempfile.mkdtemp(prefix="demo_nodb_")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = demo.main(["--db", os.path.join(tmp, "nope.db"), "--out", tmp, "--fast"])
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("找不到题库", text)
        self.assertIn("build_db.py", text, "要给出可执行的修复命令：\n%s" % text)
        self.assertNotIn("Traceback", text, "默认不许把栈甩给用户")

    def test_insufficient_questions_uses_friendly_explanation(self):
        """题量不足 → errors.explain 的「按提示改数量」那套文案，不是栈。"""
        from unittest import mock

        tmp = tempfile.mkdtemp(prefix="demo_short_")
        self.addCleanup(shutil.rmtree, tmp, True)
        short = picker.InsufficientQuestions(
            {"选择": {"need": 99, "available": 10, "short": 89}})
        out = io.StringIO()
        with mock.patch.object(demo.picker, "build", side_effect=short):
            with contextlib.redirect_stdout(out):
                code = demo.main(["--db", REAL_DB if os.path.isfile(REAL_DB) else "",
                                  "--out", tmp, "--fast"])
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("凑不够你填的题量", text, text)
        self.assertIn("选择：要 99 题", text, "要把差多少题告诉用户：\n%s" % text)
        self.assertIn("放宽范围", text)
        self.assertNotIn("Traceback", text)


class DemoEndToEndTest(unittest.TestCase):
    """真跑一遍（小库 + --fast）：产物齐全、print.pdf 页数对、库不脏。"""

    @classmethod
    def setUpClass(cls):
        if not render.word_available():
            raise unittest.SkipTest("本机没有 Word：一键演示需要它导 PDF，跳过")
        if not os.path.isfile(REAL_DB):
            raise unittest.SkipTest("没有真库 app/data/app.db")
        cls.tmp = tempfile.mkdtemp(prefix="demo_e2e_")
        cls.db_path = os.path.join(cls.tmp, "subset.db")
        cls.out = os.path.join(cls.tmp, "out")
        try:
            make_subset_db(REAL_DB, cls.db_path)
        except unittest.SkipTest:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise
        cls.conn = db.connect(cls.db_path)
        cls.before = cls._snapshot()
        cls.stdout = io.StringIO()
        with contextlib.redirect_stdout(cls.stdout):
            cls.code = demo.main(["--db", cls.db_path, "--out", cls.out, "--fast"])
        cls.text = cls.stdout.getvalue()

    @classmethod
    def _snapshot(cls):
        out = {}
        for table in ("papers", "submissions", "mastery"):
            try:
                out[table] = cls.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            except sqlite3.Error:
                out[table] = -1
        return out

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "conn", None) is not None:
            cls.conn.close()
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_exit_code_zero(self):
        self.assertEqual(self.code, 0, "演示失败：\n%s" % self.text[-3000:])

    def test_core_artifacts_exist(self):
        for name in ("paper.pdf", "paper.docx", "card.pdf", "print.pdf",
                     "card_1.png", "page-01.png", "answer_1.png", "photo_page1.jpg",
                     "summary.txt", "summary.png",
                     os.path.join("train", "train.docx")):
            path = os.path.join(self.out, name)
            self.assertTrue(os.path.isfile(path), "缺产物：%s\n%s" % (name, self.text[-2000:]))
            self.assertGreater(os.path.getsize(path), 500, name)

    def test_print_pdf_merges_paper_then_card(self):
        import pymupdf

        with pymupdf.open(os.path.join(self.out, "print.pdf")) as merged:
            n_merged = merged.page_count
            first_w = merged[0].rect.width
        with pymupdf.open(os.path.join(self.out, "paper.pdf")) as p:
            n_paper = p.page_count
        with pymupdf.open(os.path.join(self.out, "card.pdf")) as c:
            n_card = c.page_count
        self.assertEqual(n_merged, n_paper + n_card, "print.pdf 页数 ≠ 卷面 + 答题卡")
        self.assertAlmostEqual(first_w, 595.3, delta=1.0, msg="合订后首页不是 A4（卷面应在前）")

    def test_handwriting_and_photo_were_really_made(self):
        from PIL import Image

        with Image.open(os.path.join(self.out, "card_1.png")) as blank:
            with Image.open(os.path.join(self.out, "answer_1.png")) as filled:
                self.assertEqual(blank.size, filled.size)
                self.assertNotEqual(blank.tobytes(), filled.tobytes(),
                                    "作答页和空白答题卡一模一样：没真的涂卡/写字")
        with Image.open(os.path.join(self.out, "photo_page1.jpg")) as photo:
            self.assertGreater(photo.size[0], 2480, "照片应比原卡大（含桌面留白）")

    def test_summary_is_readable(self):
        for key in ("一键演示 · 结果摘要", "卷号", "选择题判分", "训练卷", "阶段耗时",
                    "总耗时", "产物目录", "print.pdf"):
            self.assertIn(key, self.text, "摘要缺字段：%s\n%s" % (key, self.text[-2000:]))

    def test_really_graded_and_wrong_ones_reported(self):
        # 故意答错 2 道选择题 → 摘要里必须出现「错 2」，且要有训练卷
        self.assertIn("错 2", self.text, self.text[-2000:])
        self.assertRegex(self.text, r"训练卷\s+\d+ 题")

    def test_db_not_polluted(self):
        after = self._snapshot()
        self.assertEqual(after, self.before,
                         "演示跑完 papers/submissions/mastery 没还原：before=%r after=%r"
                         % (self.before, after))
        row = self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        self.assertEqual(row, 0)


    def test_missing_word_degrades_with_advice(self):
        """没有 Word → 明确说"装 Word"，而不是让我看 com_error。"""
        from unittest import mock

        tmp = tempfile.mkdtemp(prefix="demo_noword_")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = io.StringIO()
        with mock.patch.object(render, "word_available", return_value=False):
            with contextlib.redirect_stdout(out):
                code = demo.main(["--db", REAL_DB if os.path.isfile(REAL_DB) else "",
                                  "--out", tmp, "--fast"])
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("没有可用的 Word", text, text)
        self.assertIn("Word 2016", text, "要给出可执行的修复建议：%s" % text)
        self.assertNotIn("Traceback", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
