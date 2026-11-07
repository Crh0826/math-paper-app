# -*- coding: utf-8 -*-
"""M5-a · Web 骨架测试：GET / 表单、GET /healthz 诊断、降级路径给友好错误而不是 500。

测的是骨架能承诺的事：
  1. / 返回 200，表单字段齐全，章节/知识点来自 kps 表且按 chapter,kp_id 排序；
  2. /healthz 返回 200 且字段齐全（DB 行数、papers/submissions、Word、OCR、数据目录）；
  3. DB 缺失 / 结构不全 / kps 为空时，页面是带「怎么办」的友好提示，不是 traceback；
  4. / 不会偷偷建库；/healthz 不泄漏 OCR token。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbmod  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def text_of(html: str) -> str:
    """把 HTML 压成纯文本（断言中文标签时用），保留标签之间的空格。"""
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = TAG_RE.sub("", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return SPACE_RE.sub(" ", s).strip()


def seed_db(path: str) -> None:
    """造一份最小但结构完整的库：3 个知识点 / 4 道题 / 1 份卷 / 1 次提交。"""
    conn = dbmod.connect(path)
    try:
        conn.executemany(
            "INSERT INTO kps(kp_id, chapter, kp, n_questions) VALUES(?,?,?,?)",
            [
                ("1.1", "第1章 有理数", "正数与负数", 552),
                ("1.2", "第1章 有理数", "数轴·相反数·绝对值", 2706),
                ("2.1", "第2章 整式及其加减", "用字母表示数", 300),
            ],
        )
        conn.executemany(
            "INSERT INTO questions(no, qtype, answer_raw, is_empty, chapter, kp_id, kp) VALUES(?,?,?,?,?,?,?)",
            [
                ("00001", "选择", "A", 0, "第1章 有理数", "1.1", "正数与负数"),
                ("00004", "选择", "", 0, "第1章 有理数", "1.2", "数轴·相反数·绝对值"),
                ("00002", "填空", "3", 0, "第2章 整式及其加减", "2.1", "用字母表示数"),
                ("00003", "解答", "", 1, "第1章 有理数", "1.2", "数轴·相反数·绝对值"),
            ],
        )
        conn.execute(
            "INSERT INTO papers(pid, created_at, title, n_items) VALUES('P20261031-001','2026-10-31T14:00:00+08:00','样例卷',3)"
        )
        conn.execute(
            "INSERT INTO submissions(sid, pid, created_at, score, full) "
            "VALUES('S001','P20261031-001','2026-10-31T15:00:00+08:00',7.0,10.0)"
        )
        conn.commit()
    finally:
        conn.close()


def write_config(path: str, payload: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="srv_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        seed_db(cls.db_path)
        cls.cfg_path = write_config(
            os.path.join(cls.tmp, "config.json"),
            {"ocr": {"backend": "manual", "timeout_s": 5, "poll_s": 1},
             "server": {"host": "127.0.0.1", "port": 8710}},
        )
        cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg_path)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- 组卷表单

class TestIndex(_Base):
    def test_index_200_and_form_fields(self):
        r = self.client.get("/")
        self.assertEqual(200, r.status_code)
        html = r.text
        self.assertIn("<form", html)
        for field in ("title", "n_choice", "n_fill", "n_solution", "seed",
                      "chapters", "kps", "kp_mode", "need_answer"):
            self.assertIn('name="%s"' % field, html, "表单缺字段 name=%s" % field)
        self.assertIn("只选有答案题", text_of(html))
        self.assertIn("合计", text_of(html))

    def test_exclude_empty_is_fixed_not_a_checkbox(self):
        """空题印不出卷：picker 在 exclude_empty=False 时会拒绝，所以不暴露成勾选框。"""
        html = self.client.get("/").text
        self.assertNotIn('type="checkbox" name="exclude_empty"', html)
        self.assertIn('<input type="hidden" name="exclude_empty" value="1">', html)
        self.assertIn("已固定排除", text_of(html))

    def test_nav_labels(self):
        html = self.client.get("/").text
        t = text_of(html)
        for label in ("组卷", "我的卷子", "判卷", "训练", "使用指南", "自检"):
            self.assertIn(label, t)
        # t11 接线后判卷/训练都是真链接，不该再有「待接线」占位
        self.assertNotIn("待接线", t)
        self.assertIn('href="/submissions"', html)
        self.assertIn('href="/train"', html)

    def test_kp_label_reads_full_path_from_db(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("第1章 有理数 › 1.2 数轴·相反数·绝对值（2706 题）", t)
        self.assertIn("第2章 整式及其加减 › 2.1 用字母表示数（300 题）", t)

    def test_kp_sorted_by_chapter_then_kp_id(self):
        html = self.client.get("/").text
        self.assertLess(html.index('value="1.1"'), html.index('value="1.2"'))
        self.assertLess(html.index('value="1.2"'), html.index('value="2.1"'))

    def test_chapter_selector_uses_scope_param_name(self):
        html = self.client.get("/").text
        self.assertIn('name="chapters"', html)
        self.assertIn('value="第1章 有理数"', html)
        self.assertIn("（合计 3258 题）", text_of(html))

    def test_qtype_stock_from_db(self):
        """题库裁掉无答案题后，每条配比只显示一条「可出 N 题」（不再有 库内/有答案 两行）。"""
        t = text_of(self.client.get("/").text)
        self.assertIn("可出 1 题", t)   # 选择：00001 有答案，00004 无答案
        self.assertIn("可出 0 题", t)   # 解答：00003 无答案且是空题
        self.assertNotIn("库内", t)

    def test_submit_enabled_and_posts_to_papers(self):
        html = self.client.get("/").text
        self.assertIn('action="/papers"', html)
        self.assertIn('method="post"', html)
        self.assertRegex(html, r'<button[^>]*type="submit"[^>]*id="compose-submit"')

    def test_static_assets_linked(self):
        html = self.client.get("/").text
        self.assertIn("/static/app.css", html)
        self.assertIn("/static/app.js", html)


class TestStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="srv_static_")
        cls.db_path = os.path.join(cls.tmp, "app.db")
        seed_db(cls.db_path)
        cls.client = TestClient(server.create_app(
            db_path=cls.db_path, config_path=os.path.join(cls.tmp, "none.json")))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_css_served_and_has_print_paper_rules(self):
        r = self.client.get("/static/app.css")
        self.assertEqual(200, r.status_code)
        self.assertIn("text/css", r.headers.get("content-type", ""))
        css = r.text
        self.assertIn("@media print", css)
        self.assertIn("A4", css)
        self.assertIn("aspect-ratio", css)  # 打印预览纸张比例，杜绝变形

    def test_js_served(self):
        r = self.client.get("/static/app.js")
        self.assertEqual(200, r.status_code)
        self.assertIn("javascript", r.headers.get("content-type", ""))


# --------------------------------------------------------------------------- 自检

class TestHealth(_Base):
    def test_healthz_200_and_json(self):
        r = self.client.get("/healthz")
        self.assertEqual(200, r.status_code)
        self.assertIn("application/json", r.headers.get("content-type", ""))
        self.payload = r.json()

    def test_healthz_fields_complete(self):
        p = self.client.get("/healthz").json()
        for key in ("ok", "status", "generated_at", "app", "db", "word", "ocr", "data_dir", "checks"):
            self.assertIn(key, p, "healthz 缺字段 %s" % key)
        self.assertIsInstance(p["ok"], bool)
        self.assertIn(p["status"], ("ok", "degraded", "bad"))
        self.assertIsInstance(p["checks"], list)
        self.assertTrue(p["checks"])
        for c in p["checks"]:
            for key in ("key", "label", "status", "detail"):
                self.assertIn(key, c, "check 缺字段 %s" % key)
            self.assertIn(c["status"], ("ok", "warn", "fail"))

    def test_healthz_counts(self):
        p = self.client.get("/healthz").json()
        db = p["db"]
        self.assertTrue(db["exists"])
        self.assertEqual(self.db_path, db["path"])
        self.assertEqual(4, db["counts"]["questions"])
        self.assertEqual(3, db["counts"]["kps"])
        self.assertEqual(1, db["counts"]["papers"])
        self.assertEqual(1, db["counts"]["submissions"])
        self.assertIsNone(db["error"])

    def test_healthz_ocr_backend_from_config(self):
        p = self.client.get("/healthz").json()
        self.assertEqual("manual", p["ocr"]["backend"])
        self.assertEqual("config.json", p["ocr"]["source"])
        self.assertFalse(p["ocr"]["token_present"])

    def test_healthz_word_shape(self):
        p = self.client.get("/healthz").json()
        w = p["word"]
        for key in ("available", "path", "com", "reason"):
            self.assertIn(key, w)
        self.assertIsInstance(w["available"], bool)
        if w["available"]:
            self.assertTrue(w["path"].lower().endswith("winword.exe"))
        else:
            self.assertTrue(w["reason"])

    def test_healthz_data_dir_writable(self):
        p = self.client.get("/healthz").json()
        self.assertTrue(p["data_dir"]["writable"])
        self.assertTrue(os.path.isdir(p["data_dir"]["path"]))

    def test_healthz_ok_when_all_green(self):
        p = self.client.get("/healthz").json()
        fails = [c for c in p["checks"] if c["status"] == "fail"]
        self.assertEqual([], fails)
        self.assertTrue(p["ok"])
        self.assertEqual("ok", p["status"])

    def test_health_page_html(self):
        r = self.client.get("/health")
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("自检", t)
        self.assertIn("题库数据库", t)
        self.assertIn("OCR", t)
        self.assertIn("Word", t)
        self.assertIn("数据目录", t)


class TestHealthSecrets(unittest.TestCase):
    """config.json 里有真 token：自检页/JSON 只能报「已配置」，不能回显。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="srv_secret_")
        cls.db_path = os.path.join(cls.tmp, "app.db")
        seed_db(cls.db_path)
        cls.cfg_path = write_config(os.path.join(cls.tmp, "config.json"), {
            "ocr": {"backend": "paddle_vl",
                    "paddle_vl": {"token": "SECRET_TOKEN_X", "model": "PaddleOCR-VL-1.6"}},
            "server": {"host": "127.0.0.1", "port": 8710},
        })
        cls.client = TestClient(server.create_app(db_path=cls.db_path, config_path=cls.cfg_path))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_token_not_leaked(self):
        for url in ("/healthz", "/health"):
            body = self.client.get(url).text
            self.assertNotIn("SECRET_TOKEN_X", body, "%s 泄漏了 OCR token" % url)

    def test_token_presence_reported(self):
        p = self.client.get("/healthz").json()
        self.assertTrue(p["ocr"]["token_present"])
        self.assertEqual("paddle_vl", p["ocr"]["backend"])


class TestDegraded(unittest.TestCase):
    """库没了/库坏了：给红色 banner + 「怎么办」，不是 500 + traceback。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="srv_bad_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self, db_path):
        cfg = write_config(os.path.join(self.tmp, "config.json"), {"ocr": {"backend": "manual"}})
        return server.create_app(db_path=db_path, config_path=cfg)

    def test_missing_db_index_friendly(self):
        db_path = os.path.join(self.tmp, "data", "app.db")
        client = TestClient(self._app(db_path))
        try:
            r = client.get("/")
            self.assertEqual(503, r.status_code)
            t = text_of(r.text)
            self.assertIn("找不到题库数据库", t)
            self.assertIn("怎么办", t)
            self.assertIn("build_db.py", t)
            self.assertNotIn("Traceback", r.text)
            self.assertNotIn("<form", r.text)  # 没库就别摆一个装不了的表单
        finally:
            client.close()

    def test_missing_db_does_not_create_file(self):
        db_path = os.path.join(self.tmp, "data", "app.db")
        client = TestClient(self._app(db_path))
        try:
            client.get("/")
            client.get("/healthz")
            self.assertFalse(os.path.exists(db_path), "/ 不该偷偷建库")
        finally:
            client.close()

    def test_missing_db_healthz_is_200_and_friendly(self):
        client = TestClient(self._app(os.path.join(self.tmp, "nope", "app.db")))
        try:
            r = client.get("/healthz")
            self.assertEqual(200, r.status_code)  # 诊断口永远可读
            p = r.json()
            self.assertFalse(p["ok"])
            self.assertEqual("bad", p["status"])
            self.assertFalse(p["db"]["exists"])
            db_check = [c for c in p["checks"] if c["key"] == "db"][0]
            self.assertEqual("fail", db_check["status"])
            self.assertTrue(db_check["hints"])
            self.assertIn("怎么办", text_of(client.get("/health").text))
        finally:
            client.close()

    def test_empty_db_file_no_tables(self):
        db_path = os.path.join(self.tmp, "empty.db")
        sqlite3.connect(db_path).close()  # 0 表的合法 sqlite 文件
        client = TestClient(self._app(db_path))
        try:
            r = client.get("/")
            self.assertEqual(503, r.status_code)
            t = text_of(r.text)
            self.assertIn("题库结构不完整", t)
            self.assertIn("怎么办", t)
            self.assertNotIn("Traceback", r.text)
        finally:
            client.close()

    def test_empty_kps_warns_but_renders(self):
        db_path = os.path.join(self.tmp, "nokps.db")
        conn = dbmod.connect(db_path)
        conn.close()
        client = TestClient(self._app(db_path))
        try:
            r = client.get("/")
            self.assertEqual(200, r.status_code)
            t = text_of(r.text)
            self.assertIn("知识点表为空", t)
            self.assertIn("怎么办", t)
            self.assertIn("<form", r.text)
        finally:
            client.close()

    def test_unknown_path_is_friendly_404(self):
        db_path = os.path.join(self.tmp, "app.db")
        seed_db(db_path)
        client = TestClient(self._app(db_path))
        try:
            r = client.get("/definitely-not-a-route")
            self.assertEqual(404, r.status_code)
            t = text_of(r.text)
            self.assertIn("页面不存在", t)
            self.assertIn("组卷", t)
        finally:
            client.close()

    def test_unknown_paper_pid_is_friendly_404(self):
        """卷号不存在是「找不到这份卷子」，比笼统的「页面不存在」更有用。"""
        db_path = os.path.join(self.tmp, "app.db")
        seed_db(db_path)
        client = TestClient(self._app(db_path))
        try:
            r = client.get("/papers/P19700101-999")
            self.assertEqual(404, r.status_code)
            t = text_of(r.text)
            self.assertIn("找不到这份卷子", t)
            self.assertIn("组卷", t)
        finally:
            client.close()


# --------------------------------------------------------------------------- 探测函数

class TestProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="probe_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_winword_hits_candidate(self):
        fake = os.path.join(self.tmp, "Office16", "WINWORD.EXE")
        os.makedirs(os.path.dirname(fake), exist_ok=True)
        open(fake, "wb").close()
        self.assertEqual(fake, server._find_winword([
            os.path.join(self.tmp, "不存在", "WINWORD.EXE"), fake]))
        self.assertIsNone(server._find_winword([os.path.join(self.tmp, "不存在", "WINWORD.EXE")]))
        self.assertIsNone(server._find_winword([os.path.join(self.tmp, "WINWORD.EXE.bak")]))

    def test_probe_word_never_launches_word(self):
        # find 注入成 None：必须给出原因，不能抛异常
        r = server.probe_word(find=lambda: None)
        self.assertIn("available", r)
        self.assertFalse(r["available"])
        self.assertTrue(r["reason"])
        self.assertIsInstance(r["hints"], list)

    def test_probe_word_requires_com_and_binary(self):
        fake = os.path.join(self.tmp, "WINWORD.EXE")
        open(fake, "wb").close()
        r = server.probe_word(find=lambda: fake)
        self.assertEqual(r["available"], bool(r["com"]))

    def test_probe_writable_cleans_up(self):
        target = os.path.join(self.tmp, "out")
        r = server.probe_writable(target)
        self.assertTrue(r["writable"])
        self.assertEqual([], os.listdir(target))

    def test_probe_writable_failure_is_reported(self):
        # 指向一个文件当目录用，必然失败；要求返回原因而不是抛异常
        blocker = os.path.join(self.tmp, "blocker")
        open(blocker, "w").close()
        r = server.probe_writable(os.path.join(blocker, "sub"))
        self.assertFalse(r["writable"])
        self.assertTrue(r["error"])

    def test_load_ocr_config_missing_file(self):
        r = server.load_ocr_config(os.path.join(self.tmp, "none.json"))
        self.assertIsNone(r["backend"])
        self.assertIn("不存在", r["error"])

    def test_load_ocr_config_env_override(self):
        cfg = write_config(os.path.join(self.tmp, "config.json"),
                           {"ocr": {"backend": "manual", "paddle_vl": {"token": "t"}}})
        with mock.patch.dict(os.environ, {"OCR_BACKEND": "vlm", "OCR_TOKEN": "x"}):
            r = server.load_ocr_config(cfg)
        self.assertEqual("vlm", r["backend"])
        self.assertEqual("env", r["source"])
        self.assertTrue(r["token_present"])

    def test_db_summary_shape(self):
        db_path = os.path.join(self.tmp, "app.db")
        seed_db(db_path)
        s = server.read_db_summary(db_path)
        self.assertEqual(4, s["counts"]["questions"])
        self.assertEqual(3, s["kps"])
        self.assertIsNone(s["error"])


# --------------------------------------------------------------------------- 真库冒烟

@unittest.skipUnless(os.path.isfile(server.DEFAULT_DB), "真库 app/data/app.db 不存在")
class TestRealLibrary(unittest.TestCase):
    """默认工厂 + 真库跑一遍，确认骨架在真实数据上不是空的。

    题数是动态的：题库会随数据治理（删除无答案题等）变化，测试只断言「等于库里实际行数」。
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.create_app())
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(server.DEFAULT_DB)
        try:
            cls.n_questions = int(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
            cls.n_kps = int(conn.execute("SELECT COUNT(*) FROM kps").fetchone()[0])
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_real_index_has_all_kps(self):
        r = self.client.get("/")
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("第1章 有理数 › 1.2 数轴·相反数·绝对值（", t)
        self.assertIn("综合 › 9.1 期中期末综合（", t)
        self.assertIn("当前题库：%s 题" % f"{self.n_questions:,}", t)
        self.assertNotIn("知识点表为空", t)

    def test_real_healthz(self):
        p = self.client.get("/healthz").json()
        self.assertEqual(self.n_questions, p["db"]["counts"]["questions"])
        self.assertEqual(self.n_kps, p["db"]["counts"]["kps"])
        self.assertTrue(p["db"]["size_mb"] and p["db"]["size_mb"] > 50)
        self.assertTrue(p["ok"], [c for c in p["checks"] if c["status"] != "ok"])


class TestSubjectPicker(unittest.TestCase):
    """t11 · 组卷页的学科下拉：默认必选一个，且「可出题量」跟着学科走（避免跨学科混卷）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="srv_subject_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        conn = dbmod.connect(cls.db_path)
        try:
            dbmod.ensure_subject(conn, "math7a", "数学", "七年级上", "沪科版")
            dbmod.ensure_subject(conn, "chinese7a", "语文", "七年级上", "人教版")
            conn.executemany(
                "INSERT INTO questions(no, subject_pk, qtype, answer_raw, is_empty,"
                " chapter, kp_id, text_plain, fmt, option_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [("100001", "math7a", "选择", "A", 0, "第1章 有理数", "1.1", "数学选择题1", "plain", 4),
                 ("100002", "math7a", "选择", "B", 0, "第1章 有理数", "1.2", "数学选择题2", "plain", 4),
                 ("100003", "math7a", "填空", "3", 0, "第1章 有理数", "1.1", "数学填空题", "plain", 0),
                 ("200001", "chinese7a", "选择", "C", 0, "第一单元 春", "CN1.1", "语文选择题", "plain", 4)])
            conn.executemany(
                "INSERT INTO kps(kp_id, subject_pk, chapter, kp, n_questions) VALUES(?,?,?,?,?)",
                [("1.1", "math7a", "第1章 有理数", "正数与负数", 2),
                 ("1.2", "math7a", "第1章 有理数", "数轴", 1),
                 # kps.kp_id 是主键：跨学科必须用不同 kp_id（语文侧加 CN 前缀）
                 ("CN1.1", "chinese7a", "第一单元 春", "春", 1)])
            conn.commit()
        finally:
            conn.close()
        cls.cfg_path = write_config(
            os.path.join(cls.tmp, "config.json"),
            {"ocr": {"backend": "manual", "timeout_s": 5, "poll_s": 1},
             "server": {"host": "127.0.0.1", "port": 8710}},
        )
        cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg_path)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _select_html(self, html: str) -> str:
        m = re.search(r'<select[^>]*name="subject_pk"[^>]*>.*?</select>', html, re.S)
        self.assertIsNotNone(m, "组卷页缺学科下拉 name=subject_pk")
        return m.group(0)

    def _available_choice(self, html: str) -> int:
        counts = re.findall(r"可出\s*<strong>(\d+)</strong>\s*题", html)
        self.assertTrue(counts, "页面没显示「可出 N 题」")
        return int(counts[0])          # stats 顺序：选择题在前

    def test_subject_select_present(self):
        """t62 · 学科 + 年级两个下拉（级联），年级下拉的 value 就是 subject_pk。"""
        html = self.client.get("/").text
        sel = self._select_html(html)
        self.assertIn('id="subject-name"', html, "缺学科下拉")
        self.assertIn('value="math7a"', sel, "年级下拉里要有 subject_pk")
        self.assertIn('value="chinese7a"', sel)
        self.assertIn("数学", html)
        self.assertIn("语文", html)
        self.assertIn("学科", text_of(html))

    def test_subject_select_defaults_to_one(self):
        """默认必选一个：下拉里恰好一个 option 带 selected（默认数学）。"""
        sel = self._select_html(self.client.get("/").text)
        self.assertEqual(1, len(re.findall(r"\bselected\b", sel)),
                         "学科下拉必须默认选中一个（不许空着）")
        conn = dbmod.connect(self.db_path)
        try:
            subjects = server.read_subjects(conn)
        finally:
            conn.close()
        self.assertEqual("数学", subjects[0]["name"], "read_subjects 应按 subject_pk 排序")
        self.assertRegex(sel, r'value="%s"[^>]*selected' % subjects[0]["subject_pk"])

    def test_counts_follow_subject(self):
        math_n = self._available_choice(self.client.get("/?subject_pk=math7a").text)
        cn_n = self._available_choice(self.client.get("/?subject_pk=chinese7a").text)
        self.assertEqual(2, math_n, "数学有 2 道可选选择题")
        self.assertEqual(1, cn_n, "语文只有 1 道可选选择题（不许把数学题算进来）")

    def test_default_subject_equals_first_subject(self):
        """不传 subject_pk 的首页 = 第一个学科的题量（默认必选一个，不是「全库不限」）。"""
        html = self.client.get("/").text
        self.assertEqual(2, self._available_choice(html))

    def test_chapters_follow_subject(self):
        t = text_of(self.client.get("/?subject_pk=chinese7a").text)
        self.assertIn("第一单元 春", t)
        self.assertNotIn("第1章 有理数", t, "选了语文不该还列数学的章节")

    def test_unknown_subject_shows_friendly_banner(self):
        r = self.client.get("/?subject_pk=physics9x")
        self.assertEqual(200, r.status_code, "脏学科参数不该 500")
        self.assertIn("physics9x", text_of(r.text))

    def test_params_carry_subject(self):
        values = server._values_from_pairs([("subject_pk", "chinese7a"), ("n_choice", "2")])
        self.assertEqual("chinese7a", values["subject_pk"])
        params = server._params_from_values(values)
        self.assertEqual("chinese7a", params["scope"]["subject_pk"])
        self.assertEqual(["chinese7a"], params["scope"]["subjects"])

    def test_params_multiple_subjects(self):
        values = server._values_from_pairs([("subjects", "math7a"), ("subjects", "chinese7a")])
        params = server._params_from_values(values)
        self.assertEqual(["math7a", "chinese7a"], params["scope"]["subjects"])

    def test_read_subjects_reports_counts(self):
        conn = dbmod.connect(self.db_path)
        try:
            subs = {s["subject_pk"]: s for s in server.read_subjects(conn)}
        finally:
            conn.close()
        self.assertEqual(3, subs["math7a"]["n_questions"])
        self.assertEqual(1, subs["chinese7a"]["n_questions"])
        self.assertEqual("沪科版", subs["math7a"]["version"])


class TestSubjectUI(unittest.TestCase):
    """t62 · 组卷页有「学科 + 年级」级联；知识点只列所选学科。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="subjui_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        seed_db(cls.db_path)
        conn = dbmod.connect(cls.db_path)
        conn.execute("UPDATE questions SET subject_pk='math7a'")
        conn.execute("INSERT OR REPLACE INTO subjects(subject_pk,name,grade,version) VALUES"
                     "('math7a','数学','七年级上','沪科版'),"
                     "('chinese7a','语文','七年级上','人教版')")
        conn.execute("INSERT INTO kps(kp_id,subject_pk,chapter,kp,n_questions)"
                     " VALUES('9.9','chinese7a','语文·识字','识字',1)")
        conn.execute("INSERT INTO questions(no,qtype,subject_pk,chapter,kp_id,text_plain,"
                     "answer_raw,is_empty) VALUES('Z001','填空','chinese7a','语文·识字',"
                     "'9.9','语文题','x',0)")
        conn.commit()
        conn.close()
        cls.cfg_path = write_config(
            os.path.join(cls.tmp, "config.json"),
            {"ocr": {"backend": "manual", "timeout_s": 5, "poll_s": 1},
             "server": {"host": "127.0.0.1", "port": 8710}})
        cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg_path)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_index_has_subject_and_grade_selects(self):
        html = self.client.get("/").text
        self.assertIn('id="subject-name"', html, "缺学科下拉")
        self.assertIn('id="subject-grade"', html, "缺年级下拉")
        self.assertIn('name="subject_pk"', html)
        self.assertIn("数学", html)
        self.assertIn("语文", html)

    def test_default_subject_is_math7a(self):
        html = self.client.get("/").text
        self.assertIn("math7a", html)
        ctx = server._index_context(self.db_path)
        self.assertEqual("math7a", ctx["values"].get("subject_pk"))

    def test_kps_only_selected_subject(self):
        ctx = server._index_context(self.db_path, {"subject_pk": "chinese7a"})
        chapters = [c["chapter"] for c in ctx["chapters"]]
        self.assertIn("语文·识字", chapters, "选语文时应列出语文章节")
        self.assertFalse([c for c in chapters if c.startswith("数学")],
                         "选语文时不该混进数学章节：%s" % chapters)
        ctx2 = server._index_context(self.db_path, {"subject_pk": "math7a"})
        ch2 = [c["chapter"] for c in ctx2["chapters"]]
        self.assertFalse([c for c in ch2 if c.startswith("语文")], ch2)


    def test_train_nos_filtered_by_subject(self):
        """训练卷：跨科题号会被剔掉（/train 走同一套过滤）。"""
        conn = dbmod.connect(self.db_path)
        try:
            nos = [r[0] for r in conn.execute(
                "SELECT no FROM questions WHERE subject_pk='math7a' LIMIT 3")]
            mixed = nos + ["Z001"]
            kept, dropped = server._filter_nos_by_subject(conn, mixed, "math7a")
        finally:
            conn.close()
        self.assertEqual(nos, kept)
        self.assertEqual(["Z001"], dropped, "语文题必须被剔出数学训练卷")

    def test_params_default_and_explicit_subject(self):
        v = server._values_from_pairs([("n_choice", "3")])
        self.assertEqual("math7a", server._params_from_values(v)["scope"]["subject_pk"])
        v2 = server._values_from_pairs([("n_choice", "3"), ("subject_pk", "chinese7a")])
        self.assertEqual(["chinese7a"], server._params_from_values(v2)["scope"]["subjects"])


if __name__ == "__main__":
    unittest.main()
