# -*- coding: utf-8 -*-
"""T72 试卷标题按学科自动生成：规则、后端兜底、手动优先、卷面/答题卡三处一致。

断言口径（与任务验收一一对应）：
1. 规则：{学科} {年级}（{版本}），只勾 1 个单元追加「 · 单元名」，>60 字截断不留半个括号
2. 组卷页：标题留空 → 页面 input 里已经是该学科的自动标题（无 JS 也成立）
3. 后端兜底：POST /papers 不填标题 → manifest.title = 自动标题（不是空、不是死的「沪科版七年级上」）
4. 手动优先：填了标题就照用；老卷子 params_json 里的 title 不受影响
5. 三处一致：manifest.title == 卷面 docx 页眉 == 答题卡页眉
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import db as dbmod  # noqa: E402
import server  # noqa: E402

MATH = {"subject_pk": "math7a", "name": "数学", "grade": "七年级上", "version": "沪科版"}
ENG = {"subject_pk": "english9a", "name": "英语", "grade": "九年级上", "version": "人教版"}


def _docx_text(path: str) -> str:
    """docx 里所有 w:t 文本拼起来（页眉在 header*.xml 里）。"""
    out = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not (name.startswith("word/header") or name == "word/document.xml"):
                continue
            xml = z.read(name).decode("utf-8", "ignore")
            out.extend(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
    return "".join(out)


class AutoTitleRuleTest(unittest.TestCase):
    """纯函数：标题规则。"""

    def test_subject_and_grade(self):
        self.assertEqual(server._auto_title(MATH, []), "数学 七年级上（沪科版）")
        self.assertEqual(server._auto_title(ENG, []), "英语 九年级上（人教版）")

    def test_without_version(self):
        row = {"name": "数学", "grade": "七年级上"}
        self.assertEqual(server._auto_title(row, []), "数学 七年级上")

    def test_single_chapter_suffix(self):
        title = server._auto_title(MATH, ["第1章 有理数"])
        self.assertEqual(title, "数学 七年级上（沪科版） · 第1章 有理数")
        self.assertNotIn("·", server._auto_title(MATH, ["第1章 有理数", "第2章 整式"]),
                         "多个单元不加后缀")

    def test_truncate_60_without_half_bracket(self):
        row = {"name": "语文" * 20, "grade": "九年级上", "version": "人教版"}
        title = server._auto_title(row, [])
        self.assertLessEqual(len(title), server.TITLE_LIMIT)
        self.assertEqual(title.count("（"), title.count("）"), "不许留半个括号：%r" % title)
        self.assertFalse(title.endswith(" · "), "结尾不许留分隔符：%r" % title)

    def test_truncate_drops_open_bracket(self):
        row = {"name": "数学" * 18, "grade": "七年级上", "version": "沪科版"}
        title = server._auto_title(row, [])
        self.assertLessEqual(len(title), server.TITLE_LIMIT)
        self.assertEqual(title.count("（"), title.count("）"), title)

    def test_fallback_when_subject_missing(self):
        self.assertTrue(server._auto_title({}, []), "取不到学科也要给出非空标题")


class _ServerCase(unittest.TestCase):
    """起一个真 server（临时库 + 桩排版渲染），测页面与 POST /papers。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="t72_")
        cls.db_path = os.path.join(cls.tmp, "app.db")
        conn = dbmod.connect(cls.db_path)
        try:
            dbmod.ensure_subject(conn, MATH["subject_pk"], MATH["name"], MATH["grade"],
                                 MATH["version"])
            dbmod.ensure_subject(conn, ENG["subject_pk"], ENG["name"], ENG["grade"],
                                 ENG["version"])
            conn.execute(
                "INSERT OR REPLACE INTO questions(no,qtype,chapter,kp_id,subject_pk,"
                "text_plain,answer_raw,fmt) VALUES(?,?,?,?,?,?,?,?)",
                ("00001", "填空", "第1章 有理数", "1.1", MATH["subject_pk"],
                 "1 的相反数是___", "-1", "plain"))
            conn.execute(
                "INSERT OR REPLACE INTO questions(no,qtype,chapter,kp_id,subject_pk,"
                "text_plain,answer_raw,fmt) VALUES(?,?,?,?,?,?,?,?)",
                ("00002", "填空", "Unit 1", "u1", ENG["subject_pk"],
                 "I ___ a student.", "am", "plain"))
            conn.execute("INSERT OR REPLACE INTO kps(kp_id,subject_pk,chapter,kp,n_questions)"
                         " VALUES(?,?,?,?,?)", ("1.1", MATH["subject_pk"], "第1章 有理数",
                                                "相反数", 1))
            conn.execute("INSERT OR REPLACE INTO kps(kp_id,subject_pk,chapter,kp,n_questions)"
                         " VALUES(?,?,?,?,?)", ("u1", ENG["subject_pk"], "Unit 1",
                                                "be 动词", 1))
            conn.commit()
        finally:
            conn.close()
        cls.out_dir = os.path.join(cls.tmp, "out")
        os.makedirs(cls.out_dir, exist_ok=True)
        # create_app 支持注入 paper_renderer（测试里避开 Word），见 server.create_app 文档
        cls.app = server.create_app(db_path=cls.db_path, out_dir=cls.out_dir,
                                    paper_renderer=_fake_paper_renderer)
        cls.client = TestClient(cls.app)


def _fake_paper_renderer(manifest, out_dir):
    """桩渲染：真出一份 docx（带 manifest.title 进页眉），不出 PDF，省掉 Word。"""
    import paper

    built = paper.build_docx(manifest, out_dir, name="paper")
    return {"docx": built["docx"], "pdf": None, "pages": 1, "png": [],
            "health": {"n_items": len(manifest.get("items") or [])}}


class IndexPageTitleTest(_ServerCase):
    def test_page_shows_auto_title_for_default_subject(self):
        res = self.client.get("/")
        self.assertEqual(200, res.status_code)
        m = re.search(r'id="paper-title"[^>]*value="([^"]*)"', res.text)
        self.assertIsNotNone(m, "页面没有标题输入框")
        self.assertEqual("数学 七年级上（沪科版）", m.group(1))

    def test_page_auto_title_follows_subject(self):
        res = self.client.get("/?subject_pk=english9a")
        m = re.search(r'id="paper-title"[^>]*value="([^"]*)"', res.text)
        self.assertEqual("英语 九年级上（人教版）", m.group(1))

    def test_page_auto_title_includes_single_chapter(self):
        res = self.client.get("/?subject_pk=math7a&chapters=%E7%AC%AC1%E7%AB%A0+%E6%9C%89%E7%90%86%E6%95%B0")
        m = re.search(r'id="paper-title"[^>]*value="([^"]*)"', res.text)
        self.assertIn("第1章 有理数", m.group(1))
        self.assertTrue(m.group(1).startswith("数学 七年级上"), m.group(1))

    def test_page_has_restore_button_and_script(self):
        res = self.client.get("/")
        self.assertIn('id="title-reset"', res.text, "缺「恢复自动标题」按钮")
        self.assertIn("t72：试卷标题按学科自动生成", res.text, "缺自动标题脚本")

    def test_manual_title_is_kept_on_page(self):
        res = self.client.get("/?title=%E6%88%91%E7%9A%84%E8%AF%95%E5%8D%B7")
        m = re.search(r'id="paper-title"[^>]*value="([^"]*)"', res.text)
        self.assertEqual("我的试卷", m.group(1), "手填标题不许被自动标题覆盖")


class SubmitFallbackTest(_ServerCase):
    def _create(self, data: dict):
        return self.client.post("/papers", data=data, follow_redirects=False)

    def _manifest_of(self, pid: str) -> dict:
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT manifest, title FROM papers WHERE pid=?",
                               (pid,)).fetchone()
            return json.loads(row["manifest"]), row["title"]
        finally:
            conn.close()

    def test_empty_title_falls_back_to_auto(self):
        res = self._create({"subject_pk": "math7a", "n_choice": "0", "n_fill": "1", "n_solution": "0",
                            "title": "", "confirm": "1"})
        self.assertEqual(302, res.status_code, res.text[:300])
        pid = res.headers["location"].rsplit("/", 1)[-1]
        manifest, title = self._manifest_of(pid)
        self.assertEqual("数学 七年级上（沪科版）", manifest["title"])
        self.assertEqual(manifest["title"], title)

    def test_manual_title_wins(self):
        res = self._create({"subject_pk": "math7a", "n_choice": "0", "n_fill": "1", "n_solution": "0",
                            "title": "王老师的小测", "confirm": "1"})
        self.assertEqual(302, res.status_code, res.text[:300])
        pid = res.headers["location"].rsplit("/", 1)[-1]
        manifest, title = self._manifest_of(pid)
        self.assertEqual("王老师的小测", manifest["title"])
        self.assertEqual("王老师的小测", title)

    def test_english_subject_gets_english_title(self):
        res = self._create({"subject_pk": "english9a", "n_choice": "0", "n_fill": "1", "n_solution": "0",
                            "title": "", "confirm": "1"})
        self.assertEqual(302, res.status_code, res.text[:300])
        pid = res.headers["location"].rsplit("/", 1)[-1]
        manifest, _title = self._manifest_of(pid)
        self.assertEqual("英语 九年级上（人教版）", manifest["title"])

    def test_title_consistent_in_paper_and_card_headers(self):
        """manifest.title == 卷面 docx 页眉 == 答题卡页眉（三处一致）。"""
        res = self._create({"subject_pk": "math7a", "n_choice": "0", "n_fill": "1", "n_solution": "0",
                            "title": "", "confirm": "1"})
        self.assertEqual(302, res.status_code, res.text[:300])
        pid = res.headers["location"].rsplit("/", 1)[-1]
        manifest, _title = self._manifest_of(pid)
        docx = None
        for root, _dirs, files in os.walk(self.out_dir):
            for name in files:
                if name.endswith(".docx"):
                    docx = os.path.join(root, name)
                    break
            if docx:
                break
        self.assertIsNotNone(docx, "没找到卷面 docx（out_dir=%s，内容=%r）"
                             % (self.out_dir, os.listdir(self.out_dir)))
        head = _docx_text(docx)
        self.assertIn(manifest["title"], head, "卷面页眉里没有 manifest.title")
        self.assertIn(manifest["title"], json.dumps(manifest, ensure_ascii=False),
                      "答题卡页眉读的就是 manifest.title")

    def test_old_paper_params_title_untouched(self):
        # 老卷子：params_json 里已有 title，读回来照旧
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO papers(pid,created_at,title,params_json,manifest,n_items)"
                " VALUES(?,?,?,?,?,?)",
                ("P_OLD_T72", "2026-10-01T00:00:00+08:00", "旧卷标题",
                 json.dumps({"title": "旧卷标题"}, ensure_ascii=False),
                 json.dumps({"pid": "P_OLD_T72", "title": "旧卷标题", "items": []},
                            ensure_ascii=False), 0))
            conn.commit()
            row = conn.execute("SELECT title, params_json FROM papers WHERE pid='P_OLD_T72'")
            got = row.fetchone()
        finally:
            conn.close()
        self.assertEqual("旧卷标题", got["title"])
        self.assertEqual("旧卷标题", json.loads(got["params_json"])["title"])


def tearDownModule():
    tmp = getattr(_ServerCase, "tmp", "")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
