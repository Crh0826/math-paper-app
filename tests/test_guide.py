# -*- coding: utf-8 -*-
"""t18 · 使用指南页 /guide 测试：打印 100%、拍照四角、判卷流程、错误速查都要在。

/guide 是纯静态页：不读数据库，库坏了也能打开（判卷出问题时用户第一件事就是翻它）。
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

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


class TestGuidePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 故意指向不存在的库：/guide 不依赖数据库，必须照样 200
        cls.tmp = tempfile.mkdtemp(prefix="guide_")
        cls.client = TestClient(server.create_app(
            db_path=os.path.join(cls.tmp, "nope", "app.db"),
            config_path=os.path.join(cls.tmp, "none.json")))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_guide_is_200_without_db(self):
        r = self.client.get("/guide")
        self.assertEqual(200, r.status_code, "库坏了也要能打开指南页")
        self.assertIn("text/html", r.headers.get("content-type", ""))

    def test_has_print_photo_and_flow_sections(self):
        t = text_of(self.client.get("/guide").text)
        for heading in ("打印", "拍照", "判卷"):
            self.assertIn(heading, t)
        self.assertIn("100%", t)
        self.assertIn("四角", t)
        self.assertIn("闪光灯", t)
        self.assertIn("A4", t)
        self.assertIn("适应页面", t)

    def test_print_section_warns_about_scaling(self):
        t = text_of(self.client.get("/guide").text)
        self.assertRegex(t, r"(不要|别|勿).{0,12}(适应页面|缩放)")
        self.assertIn("对不齐", t)

    def test_photo_section_covers_corners_and_angle(self):
        t = text_of(self.client.get("/guide").text)
        self.assertIn("四角", t)
        self.assertRegex(t, r"30\s*°")
        self.assertIn("1600", t)
        self.assertRegex(t, r"一页|一次")

    def test_flow_has_two_phase_and_manual_queue(self):
        t = text_of(self.client.get("/guide").text)
        for token in ("上传", "选择", "识别", "待人工确认", "重新识别"):
            self.assertIn(token, t)

    def test_draws_inline_svg_no_external_images(self):
        html = self.client.get("/guide").text
        self.assertIn("<svg", html, "示意图要用内联 SVG 画")
        self.assertNotRegex(html, r'<img[^>]+src="https?://', "不许引外部图片")
        self.assertNotIn("http://", html.replace("http://127.0.0.1", ""))

    def test_lists_error_codes_for_quick_lookup(self):
        html = self.client.get("/guide").text
        self.assertIn('id="err-warp_failed"', html, "错误速查要有锚点，结果页可直接跳")
        self.assertIn("识别服务暂时无响应", text_of(html))
        self.assertIn("重新拍照", text_of(html))

    def test_nav_has_guide_entry(self):
        for path in ("/", "/guide", "/health"):
            html = self.client.get(path).text
            self.assertIn('href="/guide"', html, "%s 的导航里没有使用指南" % path)
            self.assertIn("使用指南", text_of(html))
        self.assertIn("is-active", self.client.get("/guide").text)

    def test_guide_has_anchors_for_sections(self):
        html = self.client.get("/guide").text
        for anchor in ('id="print"', 'id="photo"', 'id="flow"'):
            self.assertIn(anchor, html)


if __name__ == "__main__":
    unittest.main()
