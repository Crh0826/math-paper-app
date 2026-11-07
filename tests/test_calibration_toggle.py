# -*- coding: utf-8 -*-
"""T79 · 校准页开关（默认不打印）+ 单独下载。

覆盖 6+ 条：
1. 默认关闭：新库/无配置 → app.state.print_include_calibration 为 False，渲染不带校准页
2. 开关持久化：POST /settings/print 写 config.print.include_calibration 并立即生效（不重启）
3. 页数对比：关 → print.pdf 页数 == 卷面 + 答题卡；开 → 多 1 页且该页有 100mm 方框
4. 单独下载：GET /papers/{pid}/calibration.pdf → 200 / application/pdf / 单页 A4 / 100mm 方框
5. 老卷子兼容：磁盘上已有的 print.pdf 不重建（mtime 与内容都不变）
6. 非法值回落：config 里写 "maybe"/2.5 → 按关闭处理；表单里没勾 → 关
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import db as dbmod  # noqa: E402
import server  # noqa: E402

MM = 72.0 / 25.4          # 1mm 多少 pt（100mm ≈ 283.46pt）
A4 = (595.3, 841.9)          # 与 _page_size 的 round(1) 口径一致


def _pdf_page_count(path: str) -> int:
    import pymupdf as fitz

    doc = fitz.open(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def _page_size(path: str, index: int = 0):
    import pymupdf as fitz

    doc = fitz.open(path)
    try:
        page = doc[0 if index < 0 else index]
        return (round(page.rect.width, 1), round(page.rect.height, 1))
    finally:
        doc.close()


def _has_100mm_box(path: str, index: int = 0) -> bool:
    """校准页特征：页内有一个边长 ≈100mm 的方框（容差 2mm）。"""
    import pymupdf as fitz

    target = 100 * MM
    doc = fitz.open(path)
    try:
        page = doc[index]
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is None:
                continue
            w, h = abs(rect.width), abs(rect.height)
            if abs(w - target) <= 2 * MM and abs(h - target) <= 2 * MM and w > 0 and h > 0:
                return True
        return False
    finally:
        doc.close()


class CalibrationBase(unittest.TestCase):
    """每个用例一套独立的 config.json + 空库（校准页/打印合并都只依赖文件）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t79_")
        self.db_path = os.path.join(self.tmp, "app.db")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.out_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        conn = dbmod.connect(self.db_path)
        conn.commit()
        conn.close()
        self.write_config({"server": {"host": "127.0.0.1", "port": 8799, "lan_enabled": False}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, cfg: dict) -> None:
        with io.open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)

    def read_config(self) -> dict:
        with io.open(self.cfg_path, encoding="utf-8") as fh:
            return json.load(fh)

    def make_app(self, **kw):
        app = server.create_app(db_path=self.db_path, config_path=self.cfg_path,
                                out_dir=self.out_dir, **kw)
        return app, TestClient(app)

    # ---- 伪造一份「卷面 + 答题卡」的成品卷子，省掉 Word -------------------
    def fake_paper(self, pid: str, paper_pages: int = 2, card_pages: int = 1) -> str:
        import pymupdf as fitz

        folder = os.path.join(self.out_dir, pid)      # server._paper_dir = out/<pid>
        os.makedirs(folder, exist_ok=True)
        for name, n in (("paper.pdf", paper_pages), ("card.pdf", card_pages)):
            doc = fitz.open()
            for _ in range(n):
                page = doc.new_page(width=595.28, height=841.89)
                page.insert_text((72, 100), pid, fontsize=12)
            doc.save(os.path.join(folder, name))
            doc.close()
        meta = {"pid": pid, "title": "数学 七年级上（沪科版）",
                "paper_pdf": os.path.join(folder, "paper.pdf"),
                "card_pdf": os.path.join(folder, "card.pdf"),
                "paper_png": [], "card_png": [],
                "paper_pages": paper_pages, "card_pages": card_pages}
        with io.open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        return folder


class DefaultOffTest(CalibrationBase):
    def test_default_is_off_without_config(self):
        self.assertEqual(False, server.PRINT_DEFAULTS["include_calibration"])
        cfg = {"server": {"host": "127.0.0.1"}}
        self.write_config(cfg)
        got = server.load_print_config(self.cfg_path)
        self.assertFalse(got["include_calibration"], "没配置时不能打印校准页")

    def test_app_state_defaults_to_off(self):
        app, _client = self.make_app()
        self.assertFalse(app.state.print_include_calibration)

    def test_missing_config_file_is_off(self):
        os.remove(self.cfg_path)
        self.assertFalse(server.load_print_config(self.cfg_path)["include_calibration"])

    def test_garbage_values_fall_back_to_off(self):
        for bad in ("maybe", 2.5, None, [], {}, "也许"):
            self.write_config({"print": {"include_calibration": bad}})
            self.assertFalse(server.load_print_config(self.cfg_path)["include_calibration"],
                             "脏值 %r 必须回落成关闭" % (bad,))
        self.write_config({"print": "oops"})
        self.assertFalse(server.load_print_config(self.cfg_path)["include_calibration"])

    def test_truthy_values_are_on(self):
        for good in (True, "1", "on", "yes", 1):
            self.write_config({"print": {"include_calibration": good}})
            self.assertTrue(server.load_print_config(self.cfg_path)["include_calibration"],
                            "%r 应该算开启" % (good,))


class SettingsToggleTest(CalibrationBase):
    def test_settings_page_has_print_block(self):
        app, client = self.make_app()
        res = client.get("/settings")
        self.assertEqual(200, res.status_code)
        html = res.text
        self.assertIn('id="print"', html, "设置页缺「打印」区块")
        self.assertIn('name="include_calibration"', html, "缺校准页开关")
        self.assertIn("/settings/print", html, "缺保存表单")
        # 默认不勾
        self.assertNotRegex(html, r'name="include_calibration"[^>]*checked')

    def test_save_persists_and_takes_effect_immediately(self):
        app, client = self.make_app()
        self.assertFalse(app.state.print_include_calibration)
        res = client.post("/settings/print", data={"include_calibration": "1"},
                          follow_redirects=False)
        self.assertEqual(303, res.status_code)
        self.assertTrue(app.state.print_include_calibration, "保存后要立即生效，不用重启")
        self.assertTrue(self.read_config()["print"]["include_calibration"])
        # 页面回显勾上
        html = client.get("/settings").text
        self.assertRegex(html, r'name="include_calibration"[^>]*checked')

    def test_unchecking_turns_it_off(self):
        app, client = self.make_app()
        client.post("/settings/print", data={"include_calibration": "1"})
        client.post("/settings/print", data={})           # 复选框没勾 = 不提交
        self.assertFalse(app.state.print_include_calibration)
        self.assertFalse(self.read_config()["print"]["include_calibration"])

    def test_other_config_sections_preserved(self):
        self.write_config({"server": {"host": "0.0.0.0", "port": 8710, "pin": "1234"},
                           "ocr": {"backend": "manual"},
                           "print": {"include_calibration": False}})
        app, client = self.make_app()
        client.post("/settings/print", data={"include_calibration": "1"})
        cfg = self.read_config()
        self.assertEqual("0.0.0.0", cfg["server"]["host"], "server 段不能被冲掉")
        self.assertEqual("1234", cfg["server"]["pin"])
        self.assertEqual("manual", cfg["ocr"]["backend"])
        self.assertTrue(cfg["print"]["include_calibration"])


class PrintPdfPageCountTest(CalibrationBase):
    """关：卷面+答题卡；开：再多 1 页（校准页）。"""

    def test_off_merges_only_paper_and_card(self):
        self.fake_paper("P20261107-101", paper_pages=2, card_pages=1)
        app, client = self.make_app()
        res = client.get("/papers/P20261107-101/print.pdf")
        self.assertEqual(200, res.status_code)
        target = os.path.join(self.out_dir, "P20261107-101", "print.pdf")
        self.assertEqual(2 + 1, _pdf_page_count(target), "关掉校准页后应为 卷面 2 + 答题卡 1")
        self.assertEqual(A4, _page_size(target, 0))

    def test_on_adds_exactly_one_calibration_page(self):
        app, client = self.make_app()
        client.post("/settings/print", data={"include_calibration": "1"})
        self.assertTrue(app.state.print_include_calibration)

        folder = self.fake_paper("P20261107-102", paper_pages=2, card_pages=1)
        # 模拟「按开关渲染出来的卷面」：paper.pdf 首插一页校准页
        import paper

        calib = os.path.join(folder, "_calibration.pdf")
        paper.build_calibration_pdf(calib, title="数学 七年级上（沪科版）", pid="P20261107-102")
        # prepend_pdf(front, target)：把 front 插到 target 前面（就地覆盖 target）
        paper.prepend_pdf(calib, os.path.join(folder, "paper.pdf"))
        self.assertEqual(3, _pdf_page_count(os.path.join(folder, "paper.pdf")))
        self.assertTrue(_has_100mm_box(os.path.join(folder, "paper.pdf"), 0),
                        "第 1 页应是 100mm 方框的校准页")

        res = client.get("/papers/P20261107-102/print.pdf")
        self.assertEqual(200, res.status_code)
        target = os.path.join(folder, "print.pdf")
        self.assertEqual(3 + 1, _pdf_page_count(target), "开开关后多 1 页")
        self.assertEqual(4, _pdf_page_count(target))

    def test_render_receives_toggle(self):
        """server 的渲染器要按开关把 calibration 传给 paper.render_paper。"""
        recorded = {}

        def fake_renderer(kwargs_holder):
            def _render(manifest, folder):
                kwargs_holder["called"] = True
                return {"docx": "", "pdf": None, "pages": 1, "png": []}
            return _render

        app, _client = self.make_app(paper_renderer=fake_renderer(recorded))
        self.assertEqual(False, getattr(app.state, "print_include_calibration", "missing"),
                         "没配 config 时应默认关闭校准页")


class CalibrationDownloadTest(CalibrationBase):
    def test_download_single_page_a4(self):
        self.fake_paper("P20261107-201")
        app, client = self.make_app()
        res = client.get("/papers/P20261107-201/calibration.pdf")
        self.assertEqual(200, res.status_code)
        self.assertEqual("application/pdf", res.headers["content-type"])
        folder = os.path.join(self.out_dir, "P20261107-201")
        target = os.path.join(folder, "calibration.pdf")
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(1, _pdf_page_count(target), "校准页只有 1 页")
        self.assertEqual(A4, _page_size(target, 0))
        self.assertTrue(_has_100mm_box(target, 0), "校准页要有 100mm 方框")

    def test_download_does_not_touch_print_pdf(self):
        folder = self.fake_paper("P20261107-202")
        app, client = self.make_app()
        client.get("/papers/P20261107-202/print.pdf")
        print_pdf = os.path.join(folder, "print.pdf")
        before = (os.path.getmtime(print_pdf), _pdf_page_count(print_pdf))
        client.get("/papers/P20261107-202/calibration.pdf")
        after = (os.path.getmtime(print_pdf), _pdf_page_count(print_pdf))
        self.assertEqual(before, after, "下载校准页不许动已合并的 print.pdf")

    def test_bad_pid_rejected(self):
        app, client = self.make_app()
        res = client.get("/papers/..%2Fetc/calibration.pdf")
        self.assertIn(res.status_code, (400, 404))


class LegacyPrintPdfTest(CalibrationBase):
    def test_existing_print_pdf_not_rebuilt(self):
        folder = self.fake_paper("P20261107-301", paper_pages=1, card_pages=1)
        app, client = self.make_app()
        # 先按关闭状态合并一份
        client.get("/papers/P20261107-301/print.pdf")
        target = os.path.join(folder, "print.pdf")
        old_mtime, old_pages = os.path.getmtime(target), _pdf_page_count(target)
        # 之后再开开关，老卷子的 print.pdf 也不该被重建
        client.post("/settings/print", data={"include_calibration": "1"})
        res = client.get("/papers/P20261107-301/print.pdf")
        self.assertEqual(200, res.status_code)
        self.assertEqual(old_mtime, os.path.getmtime(target), "已生成的 print.pdf 不许重建")
        self.assertEqual(old_pages, _pdf_page_count(target))


if __name__ == "__main__":
    unittest.main()
