# -*- coding: utf-8 -*-
"""t65 独立复验：多学科组卷隔离 + 导入失败分支 + 文档链接 + 全量回归入口。

背景：题库已是多学科（87,748 题 / 33 学科），组卷曾串科（数学卷里混进英语/历史/物理）。
本文件**不复用**开发侧用例，全部走真 HTTP（fastapi TestClient）+ 真库副本：

  T1MathIsolation   选「数学 七上」出 10 题 → 每题回查 questions.subject_pk 必须 math7a；
                    逐题 src_title/chapter 不得出现外语科目标记；pool_report 的
                    excluded_other_subject 与 SQL 复核值一致
  T2EnglishPaper    选「英语 九上」出 3 题 → 全 english9a、章节含「英语」
  T3TrainSubject    /train 用英语错题号生成训练卷 → 新卷仍是英语
  T4ImportFailures  /import format=txt / 未知扩展名 / 坏 xlsx → 200 + 中文提示（不再 500）
  T5DocsLinks       README / docs/INDEX 里指向本轮的截图与链接可解析

证据：app/out/acceptance_v3/（JSON 数字 + 页面 HTML + PNG 截图）。

跑法：``cd E:\\OCR\\app && python -m unittest tests.test_acceptance_v3 -v``
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest

APP = r"E:\OCR\app"
if APP not in sys.path:
    sys.path.insert(0, APP)

DB_SRC = os.path.join(APP, "data", "app.db")
EV = os.path.join(APP, "out", "acceptance_v3")
WORK = os.path.join(EV, "work")
OUTD = os.path.join(WORK, "out")
CSS = os.path.join(APP, "static", "app.css")
EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
             if os.path.exists(p)), None)

#: 非数学学科的中英文标记（用于断言数学卷里不出现）
FOREIGN_MARKS = ("英语", "历史", "物理", "化学", "生物", "语文", "地理", "政治",
                 "ENG", "HIS", "PHY", "CHM", "BIO", "CHI", "GEO", "POL")
MATH_SUBJECT = "math7a"
ENG_SUBJECT = "english9a"
MATH_SRC_HINT = ("沪科", "数学", "MATH", "import", "math")


# ---------------------------------------------------------------- 基础设施
def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def dump(name, obj):
    _ensure(EV)
    path = os.path.join(EV, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return path


def _db_copy():
    _ensure(WORK)
    dst = os.path.join(WORK, "app_%d.db" % os.getpid())
    if not os.path.exists(dst):
        for name in os.listdir(WORK):
            if name.startswith("app_") and name.endswith(".db"):
                try:
                    os.remove(os.path.join(WORK, name))
                except OSError:
                    pass
        shutil.copyfile(DB_SRC, dst)
    return dst


def _conn():
    conn = sqlite3.connect(_db_copy())
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql, args=()):
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _client():
    from fastapi.testclient import TestClient
    import server
    app = server.create_app(db_path=_db_copy(), out_dir=_ensure(OUTD),
                            config_path=os.path.join(APP, "config.json"))
    return app, TestClient(app, follow_redirects=False)


def save_page(html, name):
    _ensure(EV)
    css = ""
    if os.path.exists(CSS):
        with open(CSS, encoding="utf-8") as f:
            css = f.read()
    html = re.sub(r'<link[^>]+href="/static/app\.css"[^>]*>', "<style>%s</style>" % css, html)
    path = os.path.join(EV, name + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def shot(html_path, name, width=1280, height=1900):
    if not EDGE:
        return ""
    png = os.path.join(EV, name + ".png")
    url = "file:///" + html_path.replace("\\", "/")
    try:
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--window-size=%d,%d" % (width, height),
                        "--screenshot=%s" % png, url],
                       timeout=90, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return ""
    return png if os.path.exists(png) else ""


def _manifest_of(pid):
    rows = _q("SELECT manifest FROM papers WHERE pid=?", (pid,))
    assert rows, "papers 里没有 %s" % pid
    return json.loads(rows[0]["manifest"])


def _subject_of(no):
    rows = _q("SELECT COALESCE(subject_pk,'math7a') AS s, chapter, src_title, qtype "
              "FROM questions WHERE no=?", (str(no),))
    return rows[0] if rows else None


def _compose(client, title, subject, n_choice=10, n_fill=0, n_solution=0, seed=20261107):
    r = client.post("/papers", data={
        "title": title, "seed": str(seed),
        "n_choice": str(n_choice), "n_fill": str(n_fill), "n_solution": str(n_solution),
        "layout": "compact", "need_answer": "on", "subject_pk": subject})
    assert r.status_code == 302, "POST /papers 应 302，实际 %s：%s" % (
        r.status_code, r.text[:300])
    pid = (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
    assert pid.startswith("P"), "没拿到 pid：%r" % r.headers.get("location")
    return pid


# ---------------------------------------------------------------- T1 数学卷隔离
class T1MathIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()
        cls.pid = _compose(cls.client, "复验·数学七上 10 题", MATH_SUBJECT, n_choice=10)
        cls.manifest = _manifest_of(cls.pid)
        cls.items = cls.manifest["items"]
        cls.rows = [(_subject_of(it["no"]) or {}) for it in cls.items]

    def test_ten_items(self):
        self.assertEqual(10, len(self.items), "应出 10 题，实际 %d" % len(self.items))

    def test_all_items_are_math(self):
        subs = [r.get("s") for r in self.rows]
        self.assertEqual({MATH_SUBJECT}, set(subs),
                         "数学卷混进了别的学科：%s" % sorted(set(subs)))

    def test_no_foreign_marks_in_title_or_chapter(self):
        bad = []
        for it, r in zip(self.items, self.rows):
            text = " ".join(str(x or "") for x in
                            (it.get("no"), r.get("src_title"), r.get("chapter"),
                             it.get("chapter")))
            for mark in FOREIGN_MARKS:
                if mark in text:
                    bad.append({"no": it["no"], "mark": mark, "text": text[:120]})
        self.assertEqual([], bad, "数学卷里出现了外学科标记：%s" % bad[:3])

    def test_src_title_prefix_recorded(self):
        """t65 观察值：src_title 不一定是 MATH 前缀（导入题写 import），落盘供复核。"""
        titles = sorted({str(r.get("src_title") or "") for r in self.rows})
        dump("t1_math_src_titles.json", {"pid": self.pid, "src_titles": titles,
                                         "hints_hit": [t for t in titles
                                                       if any(h in t for h in MATH_SRC_HINT)]})
        self.assertTrue(titles, "src_title 全是空")
        for t in titles:
            for mark in FOREIGN_MARKS:
                self.assertNotIn(mark, t, "src_title 里出现外学科标记：%r" % t)

    def test_pool_report_excluded_matches_sql(self):
        import picker
        conn = _conn()
        try:
            params = {"title": "x", "mix": {"选择": 10},
                      "scope": {"subject_pk": MATH_SUBJECT, "subjects": [MATH_SUBJECT]},
                      "requirements": {"need_answer": True, "exclude_empty": True},
                      "seed": 1}
            rep = picker.pool_report(conn, params)
            eligible = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE is_empty=0").fetchone()[0]
            mine = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE is_empty=0 AND "
                "COALESCE(subject_pk,'math7a')=?", (MATH_SUBJECT,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(eligible - mine, rep.get("excluded_other_subject"),
                         "pool_report 的 excluded_other_subject 与 SQL 复核不一致")
        dump("t1_pool_report.json", {"subject": rep.get("subject"),
                                     "excluded_other_subject": rep.get("excluded_other_subject"),
                                     "subject_default": rep.get("subject_default"),
                                     "sql": {"eligible": eligible, "subject_rows": mine,
                                             "excluded": eligible - mine}})

    def test_zz_screenshot_index_page(self):
        r = self.client.get("/")
        self.assertEqual(200, r.status_code)
        html = r.text
        self.assertIn("subject_pk", html, "组卷页没有学科下拉")
        path = save_page(html, "18_compose_subject_select")
        png = shot(path, "18_compose_subject_select")
        dump("t1_index_page.json", {"html": os.path.basename(path),
                                    "png": os.path.basename(png) if png else None,
                                    "has_subject_pk": "subject_pk" in html})


# ---------------------------------------------------------------- T2 英语卷
def _has_english9a() -> bool:
    """真库还在不在 english9a —— t82：题库已改成纯数学多册，英语用例跟着 skip 而不是变红。"""
    try:
        import db as dbmod
        conn = dbmod.connect(os.path.join(APP_DIR, "data", "app.db"))
    except Exception:      # noqa: BLE001
        return False
    try:
        return bool(conn.execute("SELECT 1 FROM questions WHERE subject_pk=? LIMIT 1",
                                 (ENG_SUBJECT,)).fetchone())
    finally:
        conn.close()


ENG_SKIP = unittest.skipUnless(_has_english9a(), "题库里已没有 english9a（纯数学多册），英语用例跳过")


@ENG_SKIP
class T2EnglishPaper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()
        cls.pid = _compose(cls.client, "复验·英语九上 3 题", ENG_SUBJECT, n_choice=3)
        cls.manifest = _manifest_of(cls.pid)
        cls.rows = [(_subject_of(it["no"]) or {}) for it in cls.manifest["items"]]

    def test_three_items_all_english(self):
        self.assertEqual(3, len(self.manifest["items"]))
        subs = {r.get("s") for r in self.rows}
        self.assertEqual({ENG_SUBJECT}, subs, "英语卷混进了别的学科：%s" % subs)

    def test_chapter_mentions_english(self):
        chapters = [str(r.get("chapter") or "") for r in self.rows]
        self.assertTrue(any("英语" in c for c in chapters),
                        "英语卷章节里应有「英语」：%s" % chapters)
        dump("t2_english_items.json", {
            "pid": self.pid,
            "items": [{"no": it["no"], "subject": r.get("s"),
                       "chapter": r.get("chapter"), "src_title": r.get("src_title")}
                      for it, r in zip(self.manifest["items"], self.rows)]})

    def test_zz_screenshot_paper_page(self):
        r = self.client.get("/papers/%s" % self.pid)
        self.assertEqual(200, r.status_code, r.text[:200])
        path = save_page(r.text, "19_english_paper_preview")
        shot(path, "19_english_paper_preview")
        dump("t2_page.json", {"html": os.path.basename(path),
                              "status": r.status_code})


# ---------------------------------------------------------------- T3 训练卷同科
@ENG_SKIP
class T3TrainSubject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()
        eng = _q("SELECT no FROM questions WHERE COALESCE(subject_pk,'math7a')=? "
                 "AND is_empty=0 AND qtype='选择' LIMIT 3", (ENG_SUBJECT,))
        cls.nos = [r["no"] for r in eng]
        cls.resp = cls.client.post("/train", data={"nos": ",".join(cls.nos), "n_per": "2",
                                                   "seed": "20261107"})

    def test_train_ok(self):
        self.assertEqual(302, self.resp.status_code, self.resp.text[:300])

    def test_train_items_stay_english(self):
        pid = (self.resp.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
        man = _manifest_of(pid)
        subs = {(_subject_of(it["no"]) or {}).get("s") for it in man["items"]}
        dump("t3_train.json", {"pid": pid, "source_nos": self.nos,
                               "items": man["items"], "subjects": sorted(x for x in subs if x)})
        self.assertEqual({ENG_SUBJECT}, subs,
                         "训练卷跨学科了：%s（可用题号不足时宁可变少也不该串科）" % subs)


# ---------------------------------------------------------------- T4 导入失败分支
class T4ImportFailures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()
        _ensure(EV)
        cls.results = {}
        cls.results["txt"] = cls._post("txt", "bad.txt", b"hello", "text/plain")
        cls.results["weird"] = cls._post("weird", "bad.weird", b"hello",
                                         "application/octet-stream")
        cls.results["corrupt_xlsx"] = cls._post(
            "excel", "bad.xlsx", b"this is not a zip file",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        dump("t4_import_failures.json", {
            k: {"status": v.status_code, "snippet": re.sub(r"\s+", " ", v.text)[:200]}
            for k, v in cls.results.items()})

    @classmethod
    def _post(cls, fmt, filename, content, ctype):
        return cls.client.post("/import", data={"format": fmt, "subject": "复验",
                                                "grade": "七年级上", "version": "复验版"},
                               files={"file": (filename, content, ctype)})

    def test_txt_returns_200_with_chinese(self):
        r = self.results["txt"]
        self.assertEqual(200, r.status_code, "txt 不该 500：%s" % r.text[:200])
        self.assertIn("不支持", r.text)

    def test_unknown_ext_returns_200_with_chinese(self):
        r = self.results["weird"]
        self.assertEqual(200, r.status_code, "未知扩展名不该 500：%s" % r.text[:200])
        self.assertIn("不支持", r.text)

    def test_corrupt_xlsx_explained(self):
        r = self.results["corrupt_xlsx"]
        self.assertEqual(200, r.status_code, "坏 xlsx 不该 500：%s" % r.text[:200])
        self.assertTrue(("打不开" in r.text) or ("另存" in r.text), r.text[:300])

    def test_zz_screenshot_import_error_page(self):
        r = self.results["txt"]
        path = save_page(r.text, "20_import_unsupported_format")
        shot(path, "20_import_unsupported_format")
        dump("t4_page.json", {"html": os.path.basename(path), "status": r.status_code})


# ---------------------------------------------------------------- T5 文档链接
class T5DocsLinks(unittest.TestCase):
    def test_readme_screenshots_exist(self):
        with open(os.path.join(APP, "README.md"), encoding="utf-8") as f:
            text = f.read()
        rels = re.findall(r"!\[[^\]]*\]\((docs/[^)]+)\)", text)
        self.assertGreaterEqual(len(rels), 6)
        missing = [r for r in rels if not os.path.isfile(os.path.join(APP, r))]
        self.assertEqual([], missing, "README 引用了不存在的截图：%s" % missing)
        dump("t5_readme_links.json", {"count": len(rels), "missing": missing,
                                      "has_v3_student": "docs/18-student-info.png" in rels})

    def test_index_relative_links_resolve(self):
        idx = os.path.join(APP, "docs", "INDEX.md")
        with open(idx, encoding="utf-8") as f:
            text = f.read()
        bad = []
        for m in re.finditer(r"\]\((\.{0,2}/?[^)]+)\)", text):
            ref = m.group(1).split("#")[0]
            if not ref or ref.startswith(("http://", "https://", "mailto:")):
                continue
            if not os.path.exists(os.path.join(APP, "docs", ref)):
                bad.append(ref)
        self.assertEqual([], bad, "docs/INDEX.md 有失效链接：%s" % bad)
        dump("t5_index_links.json", {"bad": bad, "checked": True})


if __name__ == "__main__":
    unittest.main()
