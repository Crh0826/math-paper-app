# -*- coding: utf-8 -*-
"""t70 · 组卷页「本范围剩余 N 题」：/api/available + 前端防抖提示 + 无 JS 退化。

只验证接口契约与页面要素，不改 picker 的选题算法（一律走 picker.count_available）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import build_db  # noqa: E402
import importers  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

QTYPES = ("选择", "填空", "解答", "判断", "其他")


class AvailableBase(unittest.TestCase):
    LIMIT = 400

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="avail_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=cls.LIMIT, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as fh:
            json.dump({"ocr": {"backend": "manual"}}, fh)
        server.clear_avail_cache()
        cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg,
                                    out_dir=cls.out_dir)
        cls.client = TestClient(cls.app)
        cls.chapters = cls._chapters()

    @classmethod
    def _chapters(cls) -> list:
        conn = sqlite3.connect(cls.db_path)
        try:
            rows = conn.execute(
                "SELECT chapter, COUNT(*) n FROM questions WHERE subject_pk='math7a'"
                " AND chapter <> '' GROUP BY chapter ORDER BY n DESC").fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        server.clear_avail_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, pairs=None, **params):
        query = list(pairs or []) + list(params.items())
        return self.client.get("/api/available", params=query)

    def body(self, pairs=None, **params) -> dict:
        r = self.get(pairs, **params)
        self.assertEqual(200, r.status_code, r.text)
        return r.json()


class TestCounts(AvailableBase):
    def test_whole_book_counts(self):
        data = self.body(subject_pk="math7a")
        self.assertTrue(data["ok"])
        self.assertEqual("math7a", data["subject_pk"])
        self.assertEqual([], data["chapters"])
        for qt in QTYPES:
            with self.subTest(qtype=qt):
                self.assertIn(qt, data["by_qtype"])
                self.assertGreaterEqual(data["by_qtype"][qt], 0)
        self.assertEqual(sum(data["by_qtype"].values()), data["total"])
        self.assertGreater(data["total"], 0, "整册应该数得出题")
        self.assertIn("elapsed_ms", data)

    def test_chapter_filter_is_smaller(self):
        whole = self.body(subject_pk="math7a")["total"]
        chapter = self.chapters[0]
        data = self.body([("subject_pk", "math7a"), ("chapters", chapter)])
        self.assertEqual([chapter], data["chapters"])
        self.assertGreater(data["total"], 0, "有题的章节不该是 0")
        self.assertLess(data["total"], whole, "单元应该比整册少")
        self.assertEqual(sum(data["by_qtype"].values()), data["total"])

    def test_multi_chapter_union(self):
        if len(self.chapters) < 2:
            self.skipTest("题库里章节不足两个")
        one = self.body([("subject_pk", "math7a"), ("chapters", self.chapters[0])])["total"]
        two = self.body([("subject_pk", "math7a"), ("chapters", self.chapters[1])])["total"]
        both = self.body([("subject_pk", "math7a"),
                          ("chapters", self.chapters[0]), ("chapters", self.chapters[1])])
        self.assertEqual(2, len(both["chapters"]))
        self.assertGreaterEqual(both["total"], max(one, two), "并集不该比单个少")
        self.assertLessEqual(both["total"], one + two, "并集不该超过两个之和")

    def test_kps_filter_accepted(self):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT kp_id FROM questions WHERE subject_pk='math7a'"
                               " AND kp_id <> '' LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row:
            self.skipTest("题库没有知识点")
        data = self.body([("subject_pk", "math7a"), ("kps", row[0])])
        self.assertEqual([row[0]], data["kps"])
        self.assertEqual(sum(data["by_qtype"].values()), data["total"])


class TestBadInput(AvailableBase):
    def test_missing_subject_is_400_with_chinese(self):
        r = self.client.get("/api/available")
        self.assertEqual(400, r.status_code)
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertIn("subject_pk", data["error"])

    def test_unknown_subject_is_400_with_chinese(self):
        r = self.get(subject_pk="no_such_subject")
        self.assertEqual(400, r.status_code, r.text)
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertRegex(data["error"], r"[\u4e00-\u9fa5]", "错误文案必须是中文")

    def test_unknown_chapter_is_400_or_empty(self):
        r = self.get([("subject_pk", "math7a"), ("chapters", "第99章 不存在")])
        self.assertIn(r.status_code, (200, 400), "不存在章节不许 500")
        data = r.json()
        if r.status_code == 200:
            self.assertEqual(0, data["total"])
            self.assertEqual({qt: 0 for qt in QTYPES}, data["by_qtype"])
        else:
            self.assertRegex(data["error"], r"[\u4e00-\u9fa5]")

    def test_empty_chapters_equals_whole_book(self):
        whole = self.body(subject_pk="math7a")["total"]
        empty = self.body([("subject_pk", "math7a"), ("chapters", "")])["total"]
        self.assertEqual(whole, empty, "空 chapters 按整册算")


class TestPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="avail_page_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=200, db_path=cls.db_path)
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as fh:
            json.dump({"ocr": {"backend": "manual"}}, fh)
        server.clear_avail_cache()
        cls.client = TestClient(server.create_app(db_path=cls.db_path, config_path=cls.cfg,
                                                  out_dir=os.path.join(cls.tmp, "out")))
        cls.html = cls.client.get("/").text

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        server.clear_avail_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_page_has_avail_line_and_debounce(self):
        self.assertIn('id="avail-line"', self.html)
        self.assertIn("/api/available", self.html)
        self.assertIn("DEBOUNCE_MS = 300", self.html, "防抖必须 300ms")
        self.assertIn('id="avail-text"', self.html)
        self.assertIn("本范围剩余", self.html)

    def test_page_has_shortage_hints(self):
        self.assertIn("data-shortage=", self.html)
        self.assertIn("data-qtype=", self.html)
        self.assertIn("道，本范围只剩", self.html, "红字文案要对得上")

    def test_submit_not_disabled(self):
        self.assertNotIn("disabled", self.html.split('id="compose-submit"')[0][-200:])
        self.assertIn('id="compose-submit"', self.html)

    def test_no_js_fallback_button(self):
        self.assertIn('formaction="/"', self.html)
        self.assertIn('formmethod="get"', self.html)
        self.assertIn('name="check"', self.html)
        self.assertIn('value="1"', self.html)
        self.assertIn("查询剩余题数", self.html)
        self.assertIn("<noscript>", self.html)

    def test_check_one_renders_server_side_numbers(self):
        r = self.client.get("/", params={"check": "1", "subject_pk": "math7a"})
        self.assertEqual(200, r.status_code)
        html = r.text
        self.assertIn("本范围剩余", html)
        block = html.split('id="avail-line"')[1][:400]
        self.assertNotIn("hidden", block.split(">")[0], "check=1 时这行要直接可见")
        self.assertRegex(block, r"<strong>\d+</strong>", "要渲染出数字")

    def test_page_without_check_hides_line_at_first(self):
        block = self.html.split('id="avail-line"')[1][:120]
        self.assertIn("hidden", block.split(">")[0], "没 check=1 时先藏起来，等 JS 填")

    def test_check_one_with_chapter_is_smaller(self):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT chapter FROM questions WHERE subject_pk='math7a'"
                               " AND chapter <> '' LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row:
            self.skipTest("没有章节")
        whole = self.client.get("/", params={"check": "1", "subject_pk": "math7a"}).text
        part = self.client.get("/", params=[("check", "1"), ("subject_pk", "math7a"),
                                           ("chapters", row[0])]).text
        n_whole = int(re.search(r'id="avail-text"><strong>(\d+)</strong> 题可选', whole).group(1))
        n_part = int(re.search(r'id="avail-text"><strong>(\d+)</strong> 题可选', part).group(1))
        self.assertGreater(n_whole, n_part)


class TestCrossSubjectChapter(AvailableBase):
    """t97：切学科后残留的旧章节必须报「不属于当前学科」，而不是「0 题」。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sqlite3 as _sq
        conn = _sq.connect(cls.db_path)
        try:
            conn.execute("INSERT OR IGNORE INTO subjects(subject_pk, name, grade, version)"
                         " VALUES('physics','物理','九年级上','沪科版')")
            for i in range(3):
                conn.execute(
                    "INSERT OR IGNORE INTO questions(no,qtype,subject_pk,chapter,kp_id,"
                    "text_plain,answer_raw,is_empty,fmt,option_count)"
                    " VALUES(?,?,?,?,?,?,?,0,'plain',4)",
                    ("9900%d" % i, "选择", "physics", "", "",
                     "物理题%d：下列说法正确的是____。" % i, "A"))
            conn.commit()
        finally:
            conn.close()
        server.clear_avail_cache()

    def _conn(self):
        import sqlite3 as _sq
        return _sq.connect(self.db_path)

    def test_physics_questions_have_no_chapter(self):
        conn = self._conn()
        try:
            n = conn.execute("SELECT COUNT(*) FROM questions WHERE subject_pk='physics'"
                             " AND COALESCE(chapter,'')<>''").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(0, n, "夹具前提：physics 的题不带章节")

    def test_available_cross_subject_chapter_is_400(self):
        r = self.client.get("/api/available",
                            params={"subject_pk": "physics",
                                    "chapters": "数学·七上·第1章 有理数"})
        self.assertEqual(400, r.status_code, r.text)
        self.assertIn("不属于当前学科", r.text)

    def test_clean_physics_is_200(self):
        r = self.client.get("/api/available", params={"subject_pk": "physics"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertGreater(r.json()["total"], 0, "物理干净请求应能出题")

    def test_post_papers_cross_subject_chapter_is_400(self):
        r = self.client.post("/papers", data={
            "subject_pk": "physics", "title": "物理卷",
            "n_choice": "1", "n_fill": "0", "n_solution": "0",
            "chapters": "数学·七上·第1章 有理数",
        }, follow_redirects=False)
        self.assertEqual(400, r.status_code, r.text[:400])
        self.assertIn("不属于当前学科", r.text)

    def test_post_papers_clean_physics_ok(self):
        r = self.client.post("/papers", data={
            "subject_pk": "physics", "title": "物理卷",
            "n_choice": "1", "n_fill": "0", "n_solution": "0",
        }, follow_redirects=False)
        self.assertIn(r.status_code, (200, 302, 303), r.text[:400])

    def test_page_clears_checks_on_subject_switch(self):
        html = self.client.get("/").text
        for token, why in (("clearScopeChecks", "切学科清空勾选逻辑"),
                           ("subject-switch-note", "切换提示条"),
                           ("clear-chapters", "「清空章节」一键按钮"),
                           ("chapter-zero", "「勾了章节但 0 题」红字位"),
                           ("chapterZeroHint", "红字逻辑接进渲染")):
            self.assertIn(token, html, "缺 %s" % why)


class TestExcludedExplained(unittest.TestCase):
    """t87 · 「导入 23 题却只剩 6 题」必须能自我解释：缺图 N 题 + 题号，中文说人话。"""

    #: 一道「题干说如图、题面没图」+ 一道正常题
    ITEMS = [
        {"no": "99001", "qtype": "填空", "stem": "如图所示的电路中，电流表的示数是____．",
         "answer": "0.5", "kp": "", "chapter": ""},
        {"no": "99002", "qtype": "填空", "stem": "1 米等于多少厘米？____．",
         "answer": "100", "kp": "", "chapter": ""},
    ]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="excl_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=200, db_path=cls.db_path)
        src = os.path.join(cls.tmp, "physics.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"subject": "物理", "grade": "七年级上", "version": "沪科版",
                       "items": cls.ITEMS}, fh, ensure_ascii=False)
        cls.import_report = importers.import_file(
            src, "json", db_path=cls.db_path,
            subject={"name": "物理", "grade": "七年级上", "version": "沪科版"})
        cls.subject_pk = importers._subject_pk("物理", "七年级上", "沪科版")
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as fh:
            json.dump({"ocr": {"backend": "manual"}}, fh)
        server.clear_avail_cache()
        cls.client = TestClient(server.create_app(db_path=cls.db_path, config_path=cls.cfg,
                                                  out_dir=os.path.join(cls.tmp, "out")))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        server.clear_avail_cache()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_api_returns_excluded_detail_with_chinese_labels(self):
        data = self.client.get("/api/available",
                               params={"subject_pk": self.subject_pk}).json()
        self.assertTrue(data["ok"])
        self.assertIn("excluded", data, "接口要回传被排除明细，别让前端重算")
        ex = data["excluded"]
        self.assertGreaterEqual(ex["total"], 1)
        labels = [row["label"] for row in ex["rows"]]
        self.assertIn("缺图", labels)
        self.assertNotIn("fig_missing", labels, "页面/接口都不许用内部键名")
        fig = next(r for r in ex["rows"] if r["label"] == "缺图")
        self.assertEqual(1, fig["count"], "只有那一道说如图的题该被排除")
        self.assertIn("99001", fig["nos"], "缺图要能给到题号")
        self.assertNotIn("99002", fig["nos"], "有图的题干不该被算成缺图")

    def test_api_has_no_internal_keys_anywhere(self):
        raw = self.client.get("/api/available", params={"subject_pk": self.subject_pk}).text
        for key in ("fig_missing", "print_blocking", "mismatch", "strict"):
            with self.subTest(key=key):
                self.assertNotIn(key, raw, "接口不该把内部键名露给页面：%s" % key)

    def test_page_shows_excluded_line_server_side(self):
        html = self.client.get("/", params={"check": "1",
                                            "subject_pk": self.subject_pk}).text
        m = re.search(r'<(?:div|details|p) class="excluded-line[^"]*" id="excluded-line"([^>]*)>(.*?)</(?:div|details)>',
                      html, re.S)
        self.assertIsNotNone(m, "页面上要有「被排除」说明块")
        attrs, block = m.group(1), m.group(2)
        self.assertNotIn("hidden", attrs, "有排除项时必须显示")
        self.assertIn("已排除", block)
        self.assertIn("缺图", block)
        self.assertIn("99001", block, "缺图题号要在页面上看得到")
        self.assertIn("印出来学生做不了", html, "要解释为什么跳过")
        for key in ("fig_missing", "print_blocking"):
            with self.subTest(key=key):
                self.assertNotIn(key, html)

    def test_excluded_line_hidden_when_nothing_excluded(self):
        # 干净学科（题干里没有「如图」）→ 不该冒出一行「题被排除」（避免误报吓人）
        src = os.path.join(self.tmp, "clean.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"subject": "生物", "grade": "七年级上", "version": "沪科版",
                       "items": [{"no": "88001", "qtype": "填空", "stem": "细胞的基本结构是____．",
                                  "answer": "细胞膜", "kp": "", "chapter": ""}]},
                      fh, ensure_ascii=False)
        importers.import_file(src, "json", db_path=self.db_path,
                              subject={"name": "生物", "grade": "七年级上", "version": "沪科版"})
        server.clear_avail_cache()
        clean_pk = importers._subject_pk("生物", "七年级上", "沪科版")
        data = self.client.get("/api/available", params={"subject_pk": clean_pk}).json()
        self.assertEqual(0, data["excluded"]["total"], "没有说如图的题，就不该算缺图")
        html = self.client.get("/", params={"check": "1", "subject_pk": clean_pk}).text
        m = re.search(r'<(?:div|details|p) class="excluded-line[^"]*" id="excluded-line"([^>]*)>', html)
        self.assertIsNotNone(m)
        self.assertIn("hidden", m.group(1), "没有排除项时这一行要藏起来")

    def test_import_report_counts_fig_missing(self):
        items = [dict(it) for it in self.ITEMS]
        payload = json.dumps({"subject": "物理", "grade": "七年级上", "version": "沪科版",
                              "items": items}, ensure_ascii=False).encode("utf-8")
        r = self.client.post("/import", data={"confirm": "1", "subject": "物理"},
                             files={"file": ("p.json", payload, "application/json")})
        self.assertEqual(200, r.status_code)
        rep = self.client.get("/import/report.json").json()
        self.assertEqual(1, rep.get("fig_missing"), "报告要数出含图缺图的题")
        self.assertIn("99001", rep.get("fig_missing_nos") or [])
        self.assertIn("含图题缺图", r.text)
        self.assertIn("组卷时会被自动跳过", r.text)
        self.assertNotIn("fig_missing", r.text, "页面不许出现内部键名")

    def test_import_runs_keeps_fig_missing(self):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT report_json FROM import_runs"
                               " ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "导入要留痕（import_runs）")
        stored = json.loads(row[0])
        self.assertIn("fig_missing", stored, "import_runs 里也要能查到缺图数")


class TestPerformance(unittest.TestCase):
    """真库（87,748 行）的缓存命中必须 <300ms；冷启动如实记录。"""

    REAL_DB = os.path.join(APP_DIR, "data", "app.db")

    def test_cached_count_is_fast_on_real_db(self):
        if not os.path.isfile(self.REAL_DB):
            self.skipTest("没有真库")
        server.clear_avail_cache()
        conn = sqlite3.connect("file:%s?mode=ro" % self.REAL_DB.replace("\\", "/"), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            values = server._values_from_pairs([("subject_pk", "math7a")])
            t0 = time.perf_counter()
            first = server._count_available(conn, values)
            cold_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            second = server._count_available(conn, values)
            warm_ms = (time.perf_counter() - t0) * 1000
            self.assertEqual(first, second, "缓存不能改变结果")
            self.assertLess(warm_ms, 300, "缓存命中要 <300ms（实测 %.1f ms）" % warm_ms)
            self.assertLess(warm_ms, cold_ms, "缓存命中必须比冷算快")
        finally:
            conn.close()
            server.clear_avail_cache()

    def test_api_elapsed_ms_reported(self):
        tmp = tempfile.mkdtemp(prefix="avail_perf_")
        try:
            db = os.path.join(tmp, "app.db")
            build_db.build(limit=200, db_path=db)
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"ocr": {"backend": "manual"}}, fh)
            server.clear_avail_cache()
            client = TestClient(server.create_app(db_path=db, config_path=cfg,
                                                  out_dir=os.path.join(tmp, "out")))
            try:
                data = client.get("/api/available", params={"subject_pk": "math7a"}).json()
                self.assertIn("elapsed_ms", data)
                self.assertLess(data["elapsed_ms"], 300, "小库单次响应要 <300ms")
                again = client.get("/api/available", params={"subject_pk": "math7a"}).json()
                self.assertEqual(data["total"], again["total"])
            finally:
                client.close()
        finally:
            server.clear_avail_cache()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
