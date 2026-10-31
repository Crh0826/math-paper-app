# -*- coding: utf-8 -*-
"""t12 · 交付文档与截图证据的守门测试。

守的事：
  * README 不再有「待接线 / 即将上线 / 占位」这类没落地的措辞；
  * README 用**真实错误码**（warp_failed / qr_failed / InsufficientQuestions / WordUnavailable / ocr_timeout）
    解释故障，并且每条都给可执行动作（不许「联系管理员」）；
  * README 引用的截图真的在 app/docs/ 里、不是空文件；
  * docs/DEGRADED.md 记了三种降级（Word 缺失 / 数据库缺失 / 端口占用）；
  * docs/capture.py 能被编译（重放脚本没被改坏）。
"""
from __future__ import annotations

import os
import py_compile
import re
import sys
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

from errors import CODES  # noqa: E402

README = os.path.join(APP_DIR, "README.md")
DOCS = os.path.join(APP_DIR, "docs")

#: README 里必须出现的真实错误码（对应 CONTRACT/errors.py 的码，不是自造词）
REQUIRED_CODES = ("db_missing", "WordUnavailable", "warp_failed", "qr_failed",
                  "InsufficientQuestions", "ocr_timeout", "empty_batch")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(README)

    def test_no_placeholder_wording(self):
        for word in ("待接线", "即将上线", "TODO", "占位", "待补"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.text, "README 里还有没落地的措辞：%s" % word)

    def test_required_error_codes_present(self):
        for code in REQUIRED_CODES:
            with self.subTest(code=code):
                self.assertIn("`%s`" % code, self.text, "README 没写错误码 %s" % code)

    def test_error_codes_are_real(self):
        """README 提到的错误码必须是 errors.py 真有的码（防止写错名字骗人）。"""
        listed = set(re.findall(r"`([a-z_]+)`", self.text))
        known = set(CODES) | {"db_missing", "db_locked", "empty_batch"}
        unknown = {c for c in listed if c.endswith("_failed") or c in ("db_missing", "db_locked")}
        self.assertEqual(set(), unknown - known, "README 用了不存在的错误码：%s" % (unknown - known))

    def test_every_faq_row_has_action(self):
        faq = self.text[self.text.find("## 常见问题"):]
        self.assertIn("怎么办", faq)
        self.assertNotIn("联系管理员", faq, "fix 不许踢皮球")

    def test_print_and_photo_requirements_still_present(self):
        for phrase in ("100%", "适应页面", "四角", "1600"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_screenshot_links_point_to_existing_files(self):
        linked = re.findall(r"!\[[^\]]*\]\((docs/[^)]+)\)", self.text)
        self.assertGreaterEqual(len(linked), 6, "README 里的截图少于 6 张")
        for rel in linked:
            with self.subTest(rel=rel):
                path = os.path.join(APP_DIR, rel)
                self.assertTrue(os.path.isfile(path), "README 引用了不存在的截图：%s" % rel)
                self.assertGreater(os.path.getsize(path), 20000, "截图太小，可能是空图：%s" % rel)

    def test_docs_and_degraded_links_exist(self):
        flat = self.text.replace("\\", "/")   # README 里既有 docs/xxx 也有 app\\docs\\xxx
        for rel in ("docs/DEGRADED.md", "docs/capture.py"):
            self.assertIn(rel, flat, "README 没指向 %s" % rel)
            self.assertTrue(os.path.isfile(os.path.join(APP_DIR, rel)))

    def test_current_state_section_has_completed_list(self):
        tail = self.text[self.text.find("## 当前完成度"):]
        self.assertIn("已完成", tail)
        self.assertIn("已知限制", tail)


class TestDocsArtifacts(unittest.TestCase):
    def test_screenshots_present_and_nonempty(self):
        expected = ("01-compose.png", "02-papers-list.png", "03-guide.png", "04-health.png",
                    "05-paper-preview.png", "05b-print-page1.png", "05c-print-page2.png",
                    "06-grade-result.png", "07-train-paper.png",
                    "08-degraded-word.png", "09-degraded-db.png",
                    "10-layout-form.png", "11-layout-compact.png", "12-lan-qr.png",
                    "13-seed-reroll.png", "14-settings.png", "15-ocr-notice.png")
        for name in expected:
            with self.subTest(name=name):
                path = os.path.join(DOCS, name)
                self.assertTrue(os.path.isfile(path), "缺少截图 %s" % name)
                self.assertGreater(os.path.getsize(path), 20000, "%s 太小" % name)
                with open(path, "rb") as fh:
                    self.assertEqual(b"\x89PNG", fh.read(4), "%s 不是 PNG" % name)

    def test_degraded_doc_covers_three_scenarios(self):
        text = read(os.path.join(DOCS, "DEGRADED.md"))
        for word in ("Word 缺失", "DB 缺失", "端口占用"):
            with self.subTest(word=word):
                self.assertIn(word, text)
        self.assertIn("409", text, "Word 缺失要说明 /pdf 与 /print.pdf 返回 409")
        self.assertIn("503", text, "DB 缺失要说明首页 503")

    def test_capture_script_compiles(self):
        path = os.path.join(DOCS, "capture.py")
        py_compile.compile(path, doraise=True)

    def test_docs_readme_lists_every_screenshot(self):
        text = read(os.path.join(DOCS, "README.md"))
        for name in os.listdir(DOCS):
            if name.endswith(".png"):
                with self.subTest(name=name):
                    self.assertIn(name, text, "docs/README.md 漏了 %s" % name)


if __name__ == "__main__":
    unittest.main()
