# -*- coding: utf-8 -*-
"""t28 · 「我的卷子」列表 + 删除卷子（防目录穿越）+ 提交进度提示 + /healthz 占用字段。

守的事：
  1. GET /papers 列出已建卷子（倒序），空态给引导，导航有入口；
  2. POST /papers/{pid}/delete 删行 + 删目录，非法 pid 400 且一个文件都不动；
  3. 删除前的目录穿越防护（解析出的绝对路径必须在 app/out 之下）；
  4. /healthz 多出 out_dir_mb 与 papers_count；
  5. 组卷表单有提交遮罩（7 秒等待别白屏），返回/错误页时能消失。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import build_db  # noqa: E402
import db as dbmod  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


class WordUnavailable(RuntimeError):
    """模拟 render.py 的 WordUnavailable：让建卷不依赖 Word，测试秒级完成。"""


def _degraded_render(manifest, out_dir):  # noqa: ARG001
    raise WordUnavailable("本机没有可用的 Word（win32com DispatchEx 失败）")


def add_paper(db_path: str, out_dir: str, pid: str, title: str = "样例卷",
              created_at: str = "2026-10-31T15:00:00+08:00", n_items: int = 2,
              paper_pdf: bool = True, card_pdf: bool = True) -> str:
    """直接造一条「已建好」的卷子（行 + 产物目录），不跑 Word，测试快。"""
    folder = os.path.join(out_dir, pid)
    os.makedirs(folder, exist_ok=True)
    items = [{
        "idx": i + 1, "no": "%05d" % (i + 1), "qtype": "选择" if i == 0 else "填空",
        "kp_id": "1.1", "answer_key": [{"blank": 1, "value": "A"}],
    } for i in range(n_items)]
    manifest = {"pid": pid, "title": title, "created_at": created_at,
                "items": items, "card": {"n_pages": 1}}
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest,"
            " docx_path, pdf_path, n_items) VALUES(?,?,?,?,?,?,?,?)",
            (pid, created_at, title, json.dumps({"title": title}),
             json.dumps(manifest, ensure_ascii=False),
             os.path.join(folder, "paper.docx") if paper_pdf else "",
             os.path.join(folder, "paper.pdf") if paper_pdf else "", n_items))
        conn.commit()
    finally:
        conn.close()
    if paper_pdf:
        with open(os.path.join(folder, "paper.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        with open(os.path.join(folder, "paper_1.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
    if card_pdf:
        with open(os.path.join(folder, "card.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        with open(os.path.join(folder, "card_1.png"), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
    with open(os.path.join(folder, "preview.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "paper_pages": 1 if paper_pdf else 0, "card_pages": 1,
                   "paper_pdf": os.path.join(folder, "paper.pdf") if paper_pdf else None,
                   "card_pdf": os.path.join(folder, "card.pdf") if card_pdf else None,
                   "paper_png": [os.path.join(folder, "paper_1.png")] if paper_pdf else [],
                   "card_png": [os.path.join(folder, "card_1.png")] if card_pdf else [],
                   "degraded": not paper_pdf}, fh, ensure_ascii=False)
    return folder


class _Base(unittest.TestCase):
    renderer = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="paperslist_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=60, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.app = server.create_app(
            db_path=cls.db_path,
            config_path=os.path.join(cls.tmp, "none.json"),
            out_dir=cls.out_dir,
            paper_renderer=cls.renderer,
        )
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPapersList(_Base):
    renderer = staticmethod(_degraded_render)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 一份真建卷（Word 不可用 → 仅答题卡），一份合成的完整卷（PDF 已生成）
        r = cls.client.post("/papers", data={
            "title": "降级卷", "seed": "3", "n_choice": "1", "n_fill": "0",
            "n_solution": "0", "need_answer": "on"}, follow_redirects=False)
        cls.degraded_pid = r.headers.get("location", "").rsplit("/", 1)[-1]
        add_paper(cls.db_path, cls.out_dir, "P20261030-004", "昨天的完整卷",
                  created_at="2026-10-30T09:00:00+08:00", paper_pdf=True)

    def test_list_is_200(self):
        r = self.client.get("/papers")
        self.assertEqual(200, r.status_code)

    def test_list_shows_created_papers(self):
        t = text_of(self.client.get("/papers").text)
        self.assertIn(self.degraded_pid, t)
        self.assertIn("降级卷", t)
        self.assertIn("P20261030-004", t)
        self.assertIn("昨天的完整卷", t)

    def test_list_shows_status_and_actions(self):
        html = self.client.get("/papers").text
        t = text_of(html)
        self.assertIn("仅答题卡", t)       # 降级那份
        self.assertIn("PDF 已生成", t)     # 合成那份
        self.assertIn("/papers/%s" % self.degraded_pid, html)   # 预览
        self.assertIn("/papers/P20261030-004/print.pdf", html)  # 合并下载
        self.assertIn('action="/papers/%s/delete"' % self.degraded_pid, html)
        self.assertIn("确认", html)        # 删除前要确认

    def test_list_sorted_newest_first(self):
        html = self.client.get("/papers").text
        self.assertLess(html.index(self.degraded_pid), html.index("P20261030-004"))

    def test_top_strip_totals(self):
        t = text_of(self.client.get("/papers").text)
        self.assertRegex(t, r"共\s*2\s*份")
        self.assertRegex(t, r"占用\s*[\d.]+\s*MB")

    def test_nav_has_papers_entry(self):
        for path in ("/", "/papers", "/guide"):
            t = text_of(self.client.get(path).text)
            self.assertIn("我的卷子", t, "%s 导航缺「我的卷子」" % path)


class TestPapersListEmpty(_Base):
    def test_empty_state(self):
        r = self.client.get("/papers")
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("还没有卷子", t)
        self.assertIn("组卷", t)


class TestDeletePaper(_Base):
    seq = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seq = 0

    def make(self, title: str = "待删除卷"):
        type(self).seq += 1
        pid = "P20261031-%03d" % (100 + type(self).seq)
        return pid, add_paper(self.db_path, self.out_dir, pid, title)

    def paper_rows(self):
        conn = dbmod.connect(self.db_path)
        try:
            return [r["pid"] for r in conn.execute("SELECT pid FROM papers ORDER BY pid")]
        finally:
            conn.close()

    def test_delete_redirects_and_removes_row_and_folder(self):
        pid, folder = self.make()
        r = self.client.post("/papers/%s/delete" % pid, follow_redirects=False)
        self.assertEqual(302, r.status_code)
        self.assertEqual("/papers", r.headers.get("location"))
        self.assertNotIn(pid, self.paper_rows(), "papers 行没删掉")
        self.assertFalse(os.path.isdir(folder), "产物目录没删掉")
        self.assertNotIn(pid, self.client.get("/papers").text, "列表里还看得到")

    def test_delete_leaves_other_papers_alone(self):
        pid, folder = self.make()
        other, other_folder = self.make("别动我")
        self.client.post("/papers/%s/delete" % pid, follow_redirects=False)
        self.assertIn(other, self.paper_rows())
        self.assertTrue(os.path.isdir(other_folder))

    def test_delete_missing_paper_is_404(self):
        r = self.client.post("/papers/P19990101-001/delete", follow_redirects=False)
        self.assertEqual(404, r.status_code)
        self.assertIn("找不到", text_of(r.text))

    def test_delete_illegal_pid_is_400_and_touches_nothing(self):
        canary = os.path.join(self.tmp, "canary.txt")
        with open(canary, "w", encoding="utf-8") as fh:
            fh.write("别删我")
        keep_pid, keep_folder = self.make("别删我卷")
        for bad in ("evil", "P2026-001", "P20261031-0007", "p20261031-007",
                    "P20261031-007-extra", "20261031-007"):
            with self.subTest(pid=bad):
                r = self.client.post("/papers/%s/delete" % bad, follow_redirects=False)
                self.assertEqual(400, r.status_code)
                self.assertIn("卷号不合法", text_of(r.text))
        self.assertTrue(os.path.isfile(canary))
        self.assertTrue(os.path.isdir(keep_folder))
        self.assertIn(keep_pid, self.paper_rows())

    def test_delete_guard_rejects_traversal(self):
        out_root = self.out_dir
        bad = ["../x", "..", ".", "", "P20261031-007/../..", "P20261031-007/../../canary",
               os.path.abspath(os.path.join(self.tmp, "canary")), "P20261031-007\\..\\..",
               "%2e%2e%2fcanary"]
        for pid in bad:
            with self.subTest(pid=pid):
                self.assertIsNone(server._delete_target(out_root, pid))
        good = server._delete_target(out_root, "P20261031-007")
        self.assertIsNotNone(good)
        self.assertEqual(os.path.abspath(os.path.join(out_root, "P20261031-007")), good)


class TestHealthExtras(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_paper(cls.db_path, cls.out_dir, "P20261031-020", "健康检查卷")

    def test_healthz_has_out_dir_mb(self):
        p = self.client.get("/healthz").json()
        self.assertIn("out_dir_mb", p)
        self.assertIsInstance(p["out_dir_mb"], (int, float))
        self.assertGreaterEqual(p["out_dir_mb"], 0)

    def test_healthz_has_papers_count(self):
        p = self.client.get("/healthz").json()
        self.assertIn("papers_count", p)
        self.assertEqual(1, p["papers_count"])

    def test_out_dir_mb_matches_disk(self):
        p = self.client.get("/healthz").json()
        total = 0
        for root, _dirs, files in os.walk(self.out_dir):
            for name in files:
                total += os.path.getsize(os.path.join(root, name))
        self.assertAlmostEqual(round(total / 1048576, 1), float(p["out_dir_mb"]), places=1)

    def test_health_page_shows_usage(self):
        t = text_of(self.client.get("/health").text)
        self.assertIn("占用", t)


class TestThinKnowledgePointBadge(_Base):
    """题量很少的知识点要直接标「仅 N 题」，别让用户填 20 题才发现抽不满。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        conn = dbmod.connect(cls.db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO kps(kp_id, chapter, kp, n_questions) "
                         "VALUES('5.0', '第5章 数据的收集与整理', '数据的收集与整理·综合', 4)")
            conn.execute("INSERT OR REPLACE INTO kps(kp_id, chapter, kp, n_questions) "
                         "VALUES('5.2', '第5章 数据的收集与整理', '统计图与频数分布', 200)")
            conn.commit()
        finally:
            conn.close()

    def test_thin_kp_gets_badge(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("仅 4 题", t)
        self.assertIn("别超过这个数", t)

    def test_normal_kp_keeps_count(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("（200 题）", t)
        self.assertNotIn("仅 200 题", t)


class TestComposeOverlay(_Base):
    def test_index_has_overlay_element(self):
        html = self.client.get("/").text
        self.assertIn('id="compose-overlay"', html)
        t = text_of(html)
        self.assertIn("正在排版", t)
        self.assertIn("约 7 秒", t)

    def test_js_shows_and_hides_overlay(self):
        js = self.client.get("/static/app.js").text
        self.assertIn("compose-overlay", js)
        self.assertIn("pageshow", js, "返回上一页时遮罩必须能消失")
        self.assertIn("disabled", js)

    def test_error_page_has_no_overlay(self):
        """错误页/409 页是整页导航，遮罩不会残留。"""
        r = self.client.post("/papers", data={
            "title": "x", "seed": "1", "n_choice": "0", "n_fill": "0",
            "n_solution": "0"}, follow_redirects=False)
        self.assertGreaterEqual(r.status_code, 400)
        self.assertNotIn('id="compose-overlay"', r.text)


if __name__ == "__main__":
    unittest.main()
