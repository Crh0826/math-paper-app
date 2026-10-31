# -*- coding: utf-8 -*-
"""t10 · 组卷/预览/下载全链路测试：建卷 → 预览 → 三种 PDF。

守的事：
  1. 表单 → picker.build → paper.render_paper → card.build_card → picker.save 这条链真的通；
  2. 预览页有题目清单、逐页 PNG、"共 N 页（卷面 a + 卡 b）"；
  3. /pdf、/card.pdf、/print.pdf 都能下，且 /print.pdf 的页数 = 卷面 + 答题卡（卷面在前）；
  4. 两个坏分支不许 500：题量不足 / Word 不可用（后者要降级成"答题卡仍可打印"）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import build_db  # noqa: E402
import picker  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

try:  # pymupdf 1.28 起正式包名是 pymupdf，fitz 只是别名
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


class WordUnavailable(RuntimeError):
    """模拟 render-engineer 的 WordUnavailable 信号（类名一致）。"""


def _failing_render(manifest, out_dir):  # noqa: ARG001
    raise WordUnavailable("本机没有可用的 Word（win32com DispatchEx 失败）")


def form_data(**over):
    base = {
        "title": "七年级数学 · 测试卷",
        "seed": "7",
        "n_choice": "1",
        "n_fill": "1",
        "n_solution": "0",
        "kp_mode": "any",
        "need_answer": "on",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class _Base(unittest.TestCase):
    renderer = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="papers_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=120, db_path=cls.db_path)
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

    def compose(self, **over):
        r = self.client.post("/papers", data=form_data(**over), follow_redirects=False)
        return r


class TestComposeAndPreview(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resp = cls.client.post("/papers", data=form_data(), follow_redirects=False)
        cls.location = cls.resp.headers.get("location", "")

    def test_post_redirects_to_preview(self):
        self.assertEqual(302, self.resp.status_code)
        self.assertRegex(self.location, r"^/papers/P\d{8}-\d{3}$")

    def test_preview_200_with_items_and_pager(self):
        r = self.client.get(self.location)
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("题目清单", t)
        self.assertIn("卷面", t)
        self.assertIn("答题卡", t)
        self.assertRegex(t, r"共 \d+ 页（卷面 \d+ 页 \+ 答题卡 \d+ 页）")
        self.assertIn("100%", t)
        self.assertIn("适应页面", t)

    def test_preview_lists_each_item(self):
        r = self.client.get(self.location)
        p = self.preview_payload()
        self.assertEqual(2, len(p["items"]))
        for item in p["items"]:
            self.assertIn(item["no"], r.text, "题目清单缺题号 %s" % item["no"])
            self.assertIn(item["qtype"], text_of(r.text))

    def test_preview_shows_page_images(self):
        r = self.client.get(self.location)
        self.assertRegex(r.text, r"/papers/[^/]+/img/paper/1\.png")
        self.assertRegex(r.text, r"/papers/[^/]+/img/card/1\.png")

    def preview_payload(self):
        import json

        import db as dbmod

        pid = self.location.rsplit("/", 1)[-1]
        with open(os.path.join(self.out_dir, pid, "preview.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT manifest FROM papers WHERE pid=?", (pid,)).fetchone()
        finally:
            conn.close()
        return {"meta": meta, "items": json.loads(row["manifest"])["items"]}


class TestDownloads(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.word_ok = False
        try:
            import paper

            cls.word_ok = bool(paper.word_available())
        except Exception:
            cls.word_ok = False
        cls.resp = cls.client.post("/papers", data=form_data(), follow_redirects=False)
        cls.pid = cls.resp.headers.get("location", "").rsplit("/", 1)[-1]

    def setUp(self):
        if not self.word_ok:
            self.skipTest("本机没有 Word，PDF 分支由降级用例覆盖")

    def test_three_pdfs_download(self):
        paper_pdf = self.client.get("/papers/%s/pdf" % self.pid)
        card_pdf = self.client.get("/papers/%s/card.pdf" % self.pid)
        print_pdf = self.client.get("/papers/%s/print.pdf" % self.pid)
        for name, r in (("paper", paper_pdf), ("card", card_pdf), ("print", print_pdf)):
            with self.subTest(pdf=name):
                self.assertEqual(200, r.status_code)
                self.assertEqual("application/pdf", r.headers.get("content-type", "").split(";")[0])
                self.assertTrue(r.content.startswith(b"%PDF"), "%s 不是 PDF" % name)

    def test_print_pdf_merges_paper_then_card(self):
        import json

        with open(os.path.join(self.out_dir, self.pid, "preview.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        r = self.client.get("/papers/%s/print.pdf" % self.pid)
        doc = fitz.open(stream=r.content, filetype="pdf")
        try:
            self.assertEqual(meta["paper_pages"] + meta["card_pages"], doc.page_count)
        finally:
            doc.close()
        self.assertGreaterEqual(meta["paper_pages"], 1)

    def test_image_routes_serve_png(self):
        page = self.client.get("/papers/%s/img/card/1.png" % self.pid)
        self.assertEqual(200, page.status_code)
        self.assertEqual("image/png", page.headers.get("content-type"))
        self.assertTrue(page.content.startswith(b"\x89PNG"))
        self.assertEqual(404, self.client.get("/papers/%s/img/card/99.png" % self.pid).status_code)
        self.assertEqual(404, self.client.get("/papers/%s/img/bogus/1.png" % self.pid).status_code)


class TestInsufficientQuestions(_Base):
    def test_insufficient_shows_need_available_short(self):
        r = self.compose(n_choice="9999")
        self.assertEqual(400, r.status_code)
        t = text_of(r.text)
        self.assertIn("题量", t)
        self.assertIn("选择", t)
        self.assertNotIn("Traceback", r.text)
        # 具体数字：需要 9999，可用 = 库里实际可出的选择题数
        import db as dbmod

        conn = dbmod.connect(self.db_path)
        try:
            avail = picker.count_available(conn, {
                "title": "t", "mix": {"选择": 1},
                "scope": {"chapters": [], "kps": [], "kp_mode": "any"},
                "requirements": {"need_answer": True, "exclude_empty": True},
                "seed": 7,
            })["选择"]
        finally:
            conn.close()
        self.assertIn("9999", t)
        self.assertIn(str(avail), t)
        self.assertIn(str(9999 - avail), t, "没告诉用户差多少题")

    def test_all_zero_counts_is_friendly(self):
        r = self.compose(n_choice="0", n_fill="0", n_solution="0")
        self.assertEqual(400, r.status_code)
        self.assertNotIn("Traceback", r.text)

    def test_unknown_scope_is_friendly(self):
        r = self.compose(kps=["99.9"])
        self.assertEqual(400, r.status_code)
        t = text_of(r.text)
        self.assertNotIn("Traceback", r.text)
        self.assertIn("知识点", t)


class TestWordDegraded(_Base):
    renderer = staticmethod(_failing_render)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resp = cls.client.post("/papers", data=form_data(), follow_redirects=False)
        cls.pid = cls.resp.headers.get("location", "").rsplit("/", 1)[-1]

    def test_compose_still_succeeds(self):
        self.assertEqual(302, self.resp.status_code)

    def test_preview_explains_degradation(self):
        r = self.client.get("/papers/%s" % self.pid)
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("卷面 PDF 未生成", t)
        self.assertIn("答题卡仍可打印", t)
        self.assertIn("Word", t)

    def test_card_pdf_still_available(self):
        r = self.client.get("/papers/%s/card.pdf" % self.pid)
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_paper_and_print_pdf_degrade_without_500(self):
        paper_pdf = self.client.get("/papers/%s/pdf" % self.pid)
        print_pdf = self.client.get("/papers/%s/print.pdf" % self.pid)
        for r in (paper_pdf, print_pdf):
            self.assertLess(r.status_code, 500)
            self.assertIn(r.status_code, (409, 404))
            self.assertIn("Word", text_of(r.text))
        self.assertIn("/papers/%s/card.pdf" % self.pid, print_pdf.text)


class TestAvailabilityOnIndex(_Base):
    def test_index_shows_how_many_can_be_picked(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("可出", t)
        self.assertNotIn("库内", t)  # 裁剪库里无答案题后，只保留「可出」一条

    def test_scoped_availability_matches_picker(self):
        import db as dbmod

        conn = dbmod.connect(self.db_path)
        try:
            kp_id = conn.execute("SELECT kp_id FROM kps ORDER BY kp_id LIMIT 1").fetchone()["kp_id"]
            params = {"title": "t", "mix": {"选择": 1, "填空": 1, "解答": 1},
                      "scope": {"chapters": [], "kps": [kp_id], "kp_mode": "any"},
                      "requirements": {"need_answer": True, "exclude_empty": True}, "seed": 7}
            expect = picker.count_available(conn, params)
        finally:
            conn.close()
        r = self.client.get("/?kps=%s" % kp_id)
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("可出 %d 题" % expect["选择"], t)


class TestCrossDriveRenderWorkaround(_Base):
    """render.py 把 PDF 先写系统临时盘再 os.replace 到 out_dir，跨盘会抛 WinError 17。

    这种时候不能整卷失败：退一步「同盘临时目录渲染 + 拷回 app/out」，卷面仍要能下。
    """

    calls = []

    @staticmethod
    def renderer(manifest, out_dir):
        import pymupdf

        TestCrossDriveRenderWorkaround.calls.append(out_dir)
        if len(TestCrossDriveRenderWorkaround.calls) == 1:
            exc = OSError("[WinError 17] 系统无法将文件移到不同的磁盘驱动器。")
            exc.winerror = 17
            raise exc
        os.makedirs(out_dir, exist_ok=True)
        pdf = os.path.join(out_dir, "paper.pdf")
        doc = pymupdf.open()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        docx = os.path.join(out_dir, "paper.docx")
        with open(docx, "wb") as fh:
            fh.write(b"PK\x03\x04fake")
        return {"docx": docx, "pdf": pdf, "pages": 1, "png": [], "health": {}}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resp = cls.client.post("/papers", data=form_data(), follow_redirects=False)
        cls.pid = cls.resp.headers.get("location", "").rsplit("/", 1)[-1]

    def test_compose_survives_cross_drive(self):
        self.assertEqual(302, self.resp.status_code)
        self.assertGreaterEqual(len(self.calls), 2, "没有走同盘临时目录重试")

    def test_paper_pdf_lands_in_out_dir(self):
        path = os.path.join(self.out_dir, self.pid, "paper.pdf")
        self.assertTrue(os.path.isfile(path), "PDF 没拷回产物目录")
        r = self.client.get("/papers/%s/pdf" % self.pid)
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.content.startswith(b"%PDF"))

    def test_preview_is_not_degraded(self):
        t = text_of(self.client.get("/papers/%s" % self.pid).text)
        self.assertNotIn("卷面渲染失败", t)
        self.assertIn("下载卷面 PDF", t)


class TestRenderFailureDegrades(_Base):
    """渲染模块抛非 Word 的异常时，答题卡仍要出，卷面标失败但别 500。"""

    @staticmethod
    def renderer(manifest, out_dir):
        raise RuntimeError("排版炸了：选项表格宽度为负")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.resp = cls.client.post("/papers", data=form_data(), follow_redirects=False)
        cls.pid = cls.resp.headers.get("location", "").rsplit("/", 1)[-1]

    def test_degrades_instead_of_500(self):
        self.assertEqual(302, self.resp.status_code)
        r = self.client.get("/papers/%s" % self.pid)
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("卷面渲染失败", t)
        self.assertIn("答题卡仍可打印", t)
        self.assertIn("排版炸了", t)

    def test_card_still_downloadable(self):
        r = self.client.get("/papers/%s/card.pdf" % self.pid)
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.content.startswith(b"%PDF"))


class TestPreviewErrors(_Base):
    def test_unknown_pid_is_friendly_404(self):
        r = self.client.get("/papers/P19700101-999")
        self.assertEqual(404, r.status_code)
        self.assertIn("找不到", text_of(r.text))

    def test_bad_pid_path_is_rejected(self):
        r = self.client.get("/papers/..%2F..%2Fetc")
        self.assertLess(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()
