# -*- coding: utf-8 -*-
"""t12 · 断链检查：页面上给出的每个站内链接都必须能打开（不再有「待接线」占位）。

覆盖：组卷 / 我的卷子 / 判卷上传 / 训练 / 使用指南 / 自检 六个入口，
以及「我的卷子」里每份卷子的 预览 / 卷面 PDF / 答题卡 PDF / 合并打印 四个下载链接。

做法：真实的卷子（用最小合法 PDF/PNG 伪造产物，避免每次跑测试都等 Word），
把页面里的 href 抠出来逐个 GET，只允许 200（POST 表单 action 单独检查存在性）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import build_db  # noqa: E402
import db as dbmod  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

try:
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # type: ignore

STATIC_PAGES = ("/", "/papers", "/submissions", "/train", "/guide", "/health")
LINK_RE = re.compile(r'href="([^"]+)"')


def make_pdf(path: str, pages: int = 1) -> None:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "smoke")
    doc.save(path)
    doc.close()


def make_paper(db_path: str, out_dir: str, pid: str) -> None:
    """造一份「产物齐全」的卷子：预览页 + 四个下载链接都能 200。"""
    folder = os.path.join(out_dir, pid)
    os.makedirs(folder, exist_ok=True)
    items = [{"idx": 1, "no": "00001", "qtype": "选择", "kp_id": "1.1",
              "answer_key": [{"blank": 1, "value": "A"}], "options": ["A", "B", "C", "D"]}]
    manifest = {"pid": pid, "title": "断链检查卷", "created_at": "2026-10-31T16:30:00+08:00",
                "items": items, "card": {"n_pages": 1}}
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest,"
            " docx_path, pdf_path, n_items) VALUES(?,?,?,?,?,?,?,?)",
            (pid, manifest["created_at"], manifest["title"], json.dumps({"title": manifest["title"]}),
             json.dumps(manifest, ensure_ascii=False),
             os.path.join(folder, "paper.docx"), os.path.join(folder, "paper.pdf"), 1))
        conn.commit()
    finally:
        conn.close()
    make_pdf(os.path.join(folder, "paper.pdf"), 1)
    make_pdf(os.path.join(folder, "card.pdf"), 1)
    with open(os.path.join(folder, "paper_1.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
    with open(os.path.join(folder, "card_1.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
    with open(os.path.join(folder, "preview.json"), "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "paper_pages": 1, "card_pages": 1,
                   "paper_pdf": os.path.join(folder, "paper.pdf"),
                   "card_pdf": os.path.join(folder, "card.pdf"),
                   "paper_png": [os.path.join(folder, "paper_1.png")],
                   "card_png": [os.path.join(folder, "card_1.png")]}, fh, ensure_ascii=False)


class TestNoDanglingLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="links_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=60, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        make_paper(cls.db_path, cls.out_dir, "P20261031-901")
        cls.app = server.create_app(
            db_path=cls.db_path,
            config_path=os.path.join(cls.tmp, "none.json"),
            out_dir=cls.out_dir,
        )
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def hrefs(self, path: str) -> set:
        r = self.client.get(path)
        self.assertEqual(200, r.status_code, "%s 打不开" % path)
        found = set()
        for href in LINK_RE.findall(r.text):
            href = href.strip()
            if not href or href.startswith(("http://", "https://", "mailto:", "#", "javascript:")):
                continue
            parts = urlsplit(href)
            path_only = parts.path or path
            if parts.fragment:
                path_only = path_only + "#" + parts.fragment
            found.add(path_only)
        return found

    def test_no_placeholder_wording(self):
        for path in STATIC_PAGES:
            with self.subTest(page=path):
                t = self.client.get(path).text
                self.assertNotIn("待接线", t, "%s 还有「待接线」占位" % path)

    def test_every_internal_link_resolves(self):
        seen = set()
        for page in STATIC_PAGES:
            for href in self.hrefs(page):
                if href in seen:
                    continue
                seen.add(href)
                if href.startswith("/papers/P20261031-901/img/"):
                    continue  # 图片占位符由预览页生成，单独在 test_image_links 里查
                url = href.split("#", 1)[0]
                with self.subTest(page=page, href=href):
                    r = self.client.get(url)
                    self.assertEqual(200, r.status_code,
                                     "%s 上的链接 %s -> %s" % (page, href, r.status_code))

    def test_image_links_on_preview(self):
        r = self.client.get("/papers/P20261031-901")
        self.assertEqual(200, r.status_code)
        for src in re.findall(r'src="(/papers/[^"]+\.png)"', r.text):
            with self.subTest(src=src):
                self.assertEqual(200, self.client.get(src).status_code, "%s 打不开" % src)

    def test_delete_forms_target_real_route(self):
        # 单独造一份「可删的草稿卷」，删它，别动主 fixture
        make_paper(self.db_path, self.out_dir, "P20261031-902")
        html = self.client.get("/papers").text
        actions = re.findall(r'action="(/papers/[^"]+/delete)"', html)
        self.assertIn("/papers/P20261031-901/delete", actions)
        self.assertIn("/papers/P20261031-902/delete", actions)
        r = self.client.post("/papers/P20261031-902/delete", follow_redirects=False)
        self.assertEqual(302, r.status_code)
        self.assertNotIn("P20261031-902", self.client.get("/papers").text)

    def test_nav_entries_all_wired(self):
        html = self.client.get("/").text
        for href in ('href="/"', 'href="/papers"', 'href="/submissions"',
                     'href="/train"', 'href="/guide"', 'href="/health"'):
            self.assertIn(href, html, "导航缺 %s" % href)


if __name__ == "__main__":
    unittest.main()
