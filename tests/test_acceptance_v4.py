# -*- coding: utf-8 -*-
"""t71 · 独立验收：顺序管理持久化 + 剩余题量计数与 SQL 一致。

跑法：``cd E:\\OCR\\app && python -m unittest tests.test_acceptance_v4``

约定
- 顺序管理写库用**临时小库**（不复制 150MB 真库，磁盘紧张）。
- 计数复核用真库**只读**连接（`mode=ro`），不写不复制。
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
import picker                               # noqa: E402
import server                               # noqa: E402

REAL_DB = os.path.join(APP_DIR, "data", "app.db")
CH = ["第1章 有理数", "第2章 整式及其加减", "第3章 一次方程与方程组"]


def seed(path):
    """两个学科、各 3 个单元的小库（够验顺序持久化）。"""
    conn = dbmod.connect(path)
    try:
        dbmod.ensure_subject(conn, "math7a", "数学", "七年级上", "沪科版")
        dbmod.ensure_subject(conn, "math7b", "数学", "七年级下", "沪科版")
        for i, ch in enumerate(CH, 1):
            conn.execute("INSERT INTO kps(kp_id, subject_pk, chapter, kp, n_questions)"
                         " VALUES(?,?,?,?,1)", ("%d.1" % i, "math7a", ch, "知识点%d" % i))
            conn.execute("INSERT INTO questions(no,qtype,subject_pk,chapter,kp_id,text_plain,"
                         "answer_raw,is_empty,fmt,option_count)"
                         " VALUES(?,?,?,?,?,?,?,0,'plain',4)",
                         ("M%02d" % i, "选择", "math7a", ch, "%d.1" % i, "题%d" % i, "A"))
        conn.execute("INSERT INTO kps(kp_id, subject_pk, chapter, kp, n_questions)"
                     " VALUES('9.1','math7b','第9章 分式','分式',1)")
        conn.execute("INSERT INTO questions(no,qtype,subject_pk,chapter,kp_id,text_plain,"
                     "answer_raw,is_empty,fmt,option_count)"
                     " VALUES('N01','选择','math7b','第9章 分式','9.1','题N','B',0,'plain',4)")
        conn.commit()
    finally:
        conn.close()


class OrderAcceptanceTest(unittest.TestCase):
    """① 顺序管理：保存 → 两页都保持；恢复默认可用；老库能打开。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acc4_")
        self.db = os.path.join(self.tmp, "app.db")
        seed(self.db)
        cfg = os.path.join(self.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('{"ocr": {"backend": "manual"}, "server": {"host": "127.0.0.1", "port": 8710}}')
        self.app = server.create_app(db_path=self.db, config_path=cfg)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _order(self, html):
        return re.findall(r'name="subjects" value="([^"]+)"', html)

    def _chapters(self, html):
        return re.findall(r'name="chapters" value="([^"]+)"', html)

    def test_save_order_persists_on_both_pages(self):
        page = self.client.get("/manage/order").text
        subs = self._order(page)
        chs = self._chapters(page)
        self.assertEqual(["math7a", "math7b"], subs)
        new_subs = ["math7b", "math7a"]                 # 把「数学七年级下」上移到第一
        swap = [chs[1], chs[0]] + chs[2:]               # 前两个单元互换
        r = self.client.post("/manage/order",
                             data={"action": "save", "subject_pk": "math7a",
                                   "subjects": new_subs, "chapters": swap},
                             follow_redirects=False)
        self.assertEqual(303, r.status_code)

        page2 = self.client.get("/manage/order?subject_pk=math7a").text
        self.assertEqual(new_subs, self._order(page2), "顺序页没保持学科新顺序")
        self.assertEqual(swap, self._chapters(page2), "顺序页没保持单元新顺序")

        home = self.client.get("/?subject_pk=math7a").text
        self.assertEqual(swap, self._chapters(home), "组卷页没跟着 chapter_order 走")

    def test_reset_restores_default(self):
        self.client.post("/manage/order",
                         data={"action": "save", "subject_pk": "math7a",
                               "subjects": ["math7b", "math7a"],
                               "chapters": [CH[2], CH[0], CH[1]]})
        self.client.post("/manage/order", data={"action": "reset", "subject_pk": ""})
        conn = dbmod.connect(self.db)
        try:
            self.assertEqual(CH, [c["chapter"] for c in dbmod.list_chapters_ordered(conn, "math7a")])
            self.assertEqual(["math7a", "math7b"],
                             [r["subject_pk"] for r in dbmod.list_subjects_ordered(conn)])
        finally:
            conn.close()

    def test_old_db_without_sort_order_opens(self):
        """老库（subjects 无 sort_order）打开即迁移，不报错、旧数据不丢。"""
        raw = sqlite3.connect(self.db)
        raw.close()
        old = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(old)
        c.executescript(
            "CREATE TABLE subjects (subject_pk TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " grade TEXT DEFAULT '', version TEXT DEFAULT '', created_at TEXT);"
            "INSERT INTO subjects VALUES('math7a','数学','七年级上','沪科版',NULL);")
        c.commit()
        c.close()
        conn = dbmod.connect(old)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(subjects)")}
            self.assertIn("sort_order", cols)
            self.assertEqual("数学", conn.execute(
                "SELECT name FROM subjects WHERE subject_pk='math7a'").fetchone()[0])
        finally:
            conn.close()


class AvailableAcceptanceTest(unittest.TestCase):
    """② /api/available 与 SQL/慢路径复核：差 0；单章节 ≤ 整册；非法入参 400 中文。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL_DB):
            raise unittest.SkipTest("没有真库")
        cls.conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        cls.chapters = [r[0] for r in cls.conn.execute(
            "SELECT chapter FROM questions WHERE subject_pk='math7a' AND chapter<>''"
            " GROUP BY chapter ORDER BY COUNT(*) DESC")]
        if not cls.chapters:
            raise unittest.SkipTest("真库里没有 math7a 章节")
        cls.tmp = tempfile.mkdtemp(prefix="acc4_api_")
        cfg = os.path.join(cls.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('{"ocr": {"backend": "manual"}, "server": {"host": "127.0.0.1", "port": 8710}}')
        cls.app = server.create_app(db_path=REAL_DB, config_path=cfg)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.conn.close()
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def _api(self, **params):
        r = self.client.get("/api/available", params=params)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _picker(self, chapters=None):
        p = {"mix": {"选择": 1}, "scope": {"subject_pk": "math7a"}}
        if chapters:
            p["scope"]["chapters"] = chapters
        return picker.count_available(self.conn, p)

    def test_whole_book_matches_sql(self):
        data = self._api(subject_pk="math7a")
        want = self._picker()
        self.assertEqual(sum(want.values()), data["total"], "整册总数与 SQL 复核不一致")
        for qt, n in want.items():
            self.assertEqual(n, data["by_qtype"].get(qt, 0), "%s 题型不一致" % qt)

    def test_single_chapter_matches_and_le_whole(self):
        ch = self.chapters[0]
        one = self._api(subject_pk="math7a", chapters=[ch])
        whole = self._api(subject_pk="math7a")
        want = self._picker([ch])
        self.assertEqual(sum(want.values()), one["total"], "单章节与 SQL 复核不一致")
        self.assertLessEqual(one["total"], whole["total"], "单章节不应超过整册")

    def test_three_chapters_union_matches(self):
        chs = self.chapters[:3]
        data = self._api(subject_pk="math7a", chapters=chs)
        want = self._picker(chs)
        self.assertEqual(sum(want.values()), data["total"], "多章节并集与 SQL 复核不一致")

    def test_invalid_params_are_400_chinese(self):
        for params in ({"subject_pk": "nope"},
                       {"subject_pk": "math7a", "chapters": ["根本不存在的单元"]}):
            r = self.client.get("/api/available", params=params)
            self.assertEqual(400, r.status_code, params)
            self.assertTrue(re.search(r"[\u4e00-\u9fa5]", r.text), "错误信息要中文")
        r = self.client.get("/api/available")
        self.assertEqual(400, r.status_code)
        self.assertIn("subject_pk", r.text)


class ChapterValueRoundTripTest(unittest.TestCase):
    """t82 / F2 回归：组卷页下拉里的**每个**单元值回传后都必须被接受，
    不得弹「不在库里」，而且数字要与 /api/available 一致。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL_DB):
            raise unittest.SkipTest("没有真库")
        cls.tmp = tempfile.mkdtemp(prefix="acc4_ch_")
        cfg = os.path.join(cls.tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('{"ocr": {"backend": "manual"}, "server": {"host": "127.0.0.1", "port": 8710}}')
        cls.app = server.create_app(db_path=REAL_DB, config_path=cfg)
        cls.client = TestClient(cls.app)
        conn = dbmod.connect(REAL_DB)
        try:
            cls.chapters = [it["chapter"] for it in dbmod.list_chapters_ordered(conn, "math7a")]
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_every_chapter_value_round_trips(self):
        self.assertTrue(self.chapters, "math7a 应该有单元")
        bad = []
        for ch in self.chapters:
            r = self.client.get("/", params={"subject_pk": "math7a", "chapters": ch})
            self.assertEqual(200, r.status_code, ch)
            if "不在库里" in r.text:
                bad.append((ch, "出现「不在库里」告警"))
                continue
            m = re.search(r"本范围剩余\s*<strong>(\d+)</strong>", r.text)
            api = self.client.get("/api/available",
                                  params={"subject_pk": "math7a", "chapters": ch}).json()
            if not m:
                bad.append((ch, "页面上没有剩余题量"))
            elif int(m.group(1)) != api["total"]:
                bad.append((ch, "页面 %s ≠ API %s" % (m.group(1), api["total"])))
        self.assertEqual([], bad, "这些单元值回传后不一致：%r" % bad)


if __name__ == "__main__":
    unittest.main()
