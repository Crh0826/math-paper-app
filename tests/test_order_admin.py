# -*- coding: utf-8 -*-
"""t69 · 顺序管理：学科/单元排序持久化 + 组卷页按序渲染。

覆盖：迁移幂等、chapter_order 建表、学科排序持久化、单元排序持久化、未排序追加在后、
恢复默认、组卷页按序渲染、非法输入 400 中文提示。
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

from fastapi.testclient import TestClient   # noqa: E402

import db as dbmod                          # noqa: E402
import server                               # noqa: E402

CH_MATH = ["第1章 有理数", "第2章 整式及其加减", "第3章 一次方程与方程组"]
CH_CN = ["第一单元 春", "第二单元 济南的冬天"]


def seed(path):
    """两学科（数学 3 单元 / 语文 2 单元）+ kps + 题，够组卷页与顺序页渲染。"""
    conn = dbmod.connect(path)
    try:
        dbmod.ensure_subject(conn, "math7a", "数学", "七年级上", "沪科版")
        dbmod.ensure_subject(conn, "chinese7a", "语文", "七年级上", "人教版")
        for i, ch in enumerate(CH_MATH, 1):
            conn.execute("INSERT INTO kps(kp_id, subject_pk, chapter, kp, n_questions)"
                         " VALUES(?,?,?,?,1)", ("%d.1" % i, "math7a", ch, "知识点%d" % i))
            conn.execute("INSERT INTO questions(no,qtype,subject_pk,chapter,kp_id,text_plain,"
                         "answer_raw,is_empty,fmt,option_count) VALUES(?,?,?,?,?,?,?,0,'plain',4)",
                         ("M%02d" % i, "选择", "math7a", ch, "%d.1" % i, "数学题%d" % i, "A"))
        for i, ch in enumerate(CH_CN, 1):
            conn.execute("INSERT INTO kps(kp_id, subject_pk, chapter, kp, n_questions)"
                         " VALUES(?,?,?,?,1)", ("CN%d" % i, "chinese7a", ch, "识字%d" % i))
            conn.execute("INSERT INTO questions(no,qtype,subject_pk,chapter,kp_id,text_plain,"
                         "answer_raw,is_empty,fmt,option_count) VALUES(?,?,?,?,?,?,?,0,'plain',4)",
                         ("C%02d" % i, "选择", "chinese7a", ch, "CN%d" % i, "语文题%d" % i, "A"))
        conn.commit()
    finally:
        conn.close()


class OrderDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="order_")
        self.db = os.path.join(self.tmp, "app.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_adds_sort_order_idempotently(self):
        """老库（subjects 没有 sort_order）打开即补列，数据不丢、重复执行没事。"""
        raw = sqlite3.connect(self.db)
        raw.executescript(
            "CREATE TABLE subjects (subject_pk TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " grade TEXT DEFAULT '', version TEXT DEFAULT '', created_at TEXT);"
            "INSERT INTO subjects(subject_pk,name,grade,version) VALUES('math7a','数学','七年级上','沪科版');")
        raw.commit()
        raw.close()
        for _ in range(2):
            conn = dbmod.connect(self.db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(subjects)")}
            self.assertIn("sort_order", cols)
            row = conn.execute("SELECT subject_pk, name, sort_order FROM subjects").fetchone()
            self.assertEqual(("math7a", "数学", 0), tuple(row))
            conn.close()

    def test_chapter_order_table_created(self):
        conn = dbmod.connect(self.db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chapter_order)")}
            self.assertEqual({"subject_pk", "chapter", "sort_order"}, cols)
            pk = [r[1] for r in conn.execute("PRAGMA table_info(chapter_order)") if r[5]]
            self.assertEqual(["subject_pk", "chapter"], pk, "主键应是 (subject_pk, chapter)")
        finally:
            conn.close()

    def test_subject_order_persists(self):
        seed(self.db)
        conn = dbmod.connect(self.db)
        try:
            before = [r["subject_pk"] for r in dbmod.list_subjects_ordered(conn)]
            self.assertEqual(["chinese7a", "math7a"], before, "默认按 subject_pk 排")
            dbmod.save_subject_order(conn, ["math7a", "chinese7a"])
            after = [r["subject_pk"] for r in dbmod.list_subjects_ordered(conn)]
            self.assertEqual(["math7a", "chinese7a"], after)
            self.assertEqual(0, after.index("math7a"))
            # 再打开一个连接：顺序是落库的
            conn2 = dbmod.connect(self.db)
            try:
                self.assertEqual(["math7a", "chinese7a"],
                                 [r["subject_pk"] for r in dbmod.list_subjects_ordered(conn2)])
            finally:
                conn2.close()
        finally:
            conn.close()

    def test_chapter_order_persists(self):
        seed(self.db)
        conn = dbmod.connect(self.db)
        try:
            want = [CH_MATH[2], CH_MATH[0], CH_MATH[1]]
            dbmod.save_chapter_order(conn, "math7a", want)
            got = [c["chapter"] for c in dbmod.list_chapters_ordered(conn, "math7a")]
            self.assertEqual(want, got)
            self.assertTrue(all(c["ordered"] for c in dbmod.list_chapters_ordered(conn, "math7a")))
        finally:
            conn.close()

    def test_unsorted_chapters_appended_last(self):
        seed(self.db)
        conn = dbmod.connect(self.db)
        try:
            dbmod.save_chapter_order(conn, "math7a", [CH_MATH[1]])
            got = dbmod.list_chapters_ordered(conn, "math7a")
            self.assertEqual(CH_MATH[1], got[0]["chapter"], "排过序的在前")
            self.assertEqual({CH_MATH[0], CH_MATH[2]}, {c["chapter"] for c in got[1:]},
                             "没排过的按原顺序追加在后")
            self.assertFalse(any(c["ordered"] for c in got[1:]))
        finally:
            conn.close()

    def test_reset_order(self):
        seed(self.db)
        conn = dbmod.connect(self.db)
        try:
            dbmod.save_subject_order(conn, ["math7a", "chinese7a"])
            dbmod.save_chapter_order(conn, "math7a", [CH_MATH[2], CH_MATH[0], CH_MATH[1]])
            out = dbmod.reset_order(conn)
            self.assertGreaterEqual(out["chapters_reset"], 3)
            self.assertEqual(["chinese7a", "math7a"],
                             [r["subject_pk"] for r in dbmod.list_subjects_ordered(conn)])
            self.assertEqual(CH_MATH,
                             [c["chapter"] for c in dbmod.list_chapters_ordered(conn, "math7a")])
        finally:
            conn.close()

    def test_invalid_input_raises_valueerror(self):
        seed(self.db)
        conn = dbmod.connect(self.db)
        try:
            with self.assertRaises(ValueError):
                dbmod.save_subject_order(conn, ["nope"])
            with self.assertRaises(ValueError):
                dbmod.save_chapter_order(conn, "nope", [CH_MATH[0]])
            with self.assertRaises(ValueError):
                dbmod.save_chapter_order(conn, "math7a", ["不存在的单元"])
            with self.assertRaises(ValueError):
                dbmod.save_chapter_order(conn, "math7a", [])
        finally:
            conn.close()


class OrderPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="orderpage_")
        cls.db = os.path.join(cls.tmp, "app.db")
        seed(cls.db)
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as f:
            f.write('{"ocr": {"backend": "manual"}, "server": {"host": "127.0.0.1", "port": 8710}}')
        cls.app = server.create_app(db_path=cls.db, config_path=cls.cfg)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._reset()          # 每个用例从「默认顺序」开始，避免互相污染

    def _reset(self):
        conn = dbmod.connect(self.db)
        try:
            dbmod.reset_order(conn)
        finally:
            conn.close()

    def test_page_renders_lists_and_buttons(self):
        html = self.client.get("/manage/order").text
        self.assertIn("subject-list", html)
        self.assertIn("chapter-list", html)
        self.assertIn("保存顺序", html)
        self.assertIn("恢复默认", html)
        self.assertIn("ord-up", html)
        self.assertIn("ord-down", html)
        self.assertIn("数学", html)
        self.assertIn(CH_MATH[0], html)

    def test_save_subject_order_then_page_and_index_follow(self):
        self._reset()
        res = self.client.post("/manage/order",
                               data={"action": "save", "subject_pk": "math7a",
                                     "subjects": ["chinese7a", "math7a"],
                                     "chapters": [CH_MATH[2], CH_MATH[0], CH_MATH[1]]},
                               follow_redirects=True)
        self.assertEqual(200, res.status_code)
        page = res.text
        self.assertIn("顺序已保存", page)
        # 顺序页：学科列表按保存顺序
        idx = page.index("subject-list")
        seg = page[idx:idx + 1200]
        self.assertLess(seg.index("chinese7a"), seg.index("math7a"),
                        "学科列表要按保存的顺序：%s" % seg[:200])
        # 组卷页：学科下拉按保存顺序 + 单元按 chapter_order
        home = self.client.get("/").text
        self.assertLess(home.index('value="chinese7a"'), home.index('value="math7a"'),
                        "组卷页学科下拉要按 save_subject_order 的顺序")
        self.assertLess(home.index(CH_MATH[2]), home.index(CH_MATH[0]),
                        "组卷页单元列表要按 chapter_order")

    def test_reset_restores_default(self):
        conn = dbmod.connect(self.db)
        try:
            dbmod.save_subject_order(conn, ["math7a", "chinese7a"])
            dbmod.save_chapter_order(conn, "math7a", [CH_MATH[2], CH_MATH[0], CH_MATH[1]])
        finally:
            conn.close()
        res = self.client.post("/manage/order", data={"action": "reset", "subject_pk": ""},
                               follow_redirects=False)
        self.assertEqual(303, res.status_code)
        self.assertIn("恢复默认", self.client.get("/manage/order").text)
        conn = dbmod.connect(self.db)
        try:
            self.assertEqual(CH_MATH,
                             [c["chapter"] for c in dbmod.list_chapters_ordered(conn, "math7a")])
            self.assertEqual(["chinese7a", "math7a"],
                             [r["subject_pk"] for r in dbmod.list_subjects_ordered(conn)])
        finally:
            conn.close()

    def test_invalid_subject_returns_400(self):
        res = self.client.post("/manage/order",
                               data={"action": "save", "subject_pk": "nope", "chapters": ["x"]})
        self.assertEqual(400, res.status_code, "非法输入要 400 而不是 500")
        self.assertIn("未知学科", res.text)

    def test_invalid_chapter_returns_400(self):
        res = self.client.post("/manage/order",
                               data={"action": "save", "subject_pk": "math7a",
                                     "chapters": ["根本不存在的单元"]})
        self.assertEqual(400, res.status_code)
        self.assertIn("没有这些单元", res.text)

    def test_nav_has_order_entry_in_more(self):
        """t100 起导航收成 5 项 + 「更多 ▾」：顺序管理在「更多」里，不再排在「设置」之前。"""
        html = self.client.get("/").text
        self.assertIn("顺序管理", html)
        more = html[html.find('class="nav-more-panel"'):]
        self.assertIn('href="/manage/order"', more, "「更多」里要能点进顺序管理")
        self.assertLess(html.index("设置"), html.index("顺序管理"),
                        "顺序管理已收进「更多」，位置在「设置」之后")
