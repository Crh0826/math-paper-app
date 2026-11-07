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


class TestFeatureDocs(unittest.TestCase):
    """t64：三项新功能（导入 / 改卷记录 / 学生信息）必须有文档入口，且模板文件名不能写错。"""

    def setUp(self):
        self.readme = read(README)
        self.index = read(os.path.join(DOCS, "INDEX.md"))

    def test_readme_lists_new_screenshots(self):
        for name, kw in (("17-grade-records.png", "改卷记录"), ("18-student-info.png", "学生信息")):
            with self.subTest(name=name):
                self.assertIn("docs/%s" % name, self.readme, "README 没引用 %s" % name)
                row = [ln for ln in self.readme.splitlines() if name in ln]
                self.assertTrue(row and kw in row[0], "README 里 %s 那行没写清楚功能：%r" % (name, row))

    def test_index_has_three_feature_sections(self):
        for kw in ("导入题库", "改卷记录", "学生信息", "/import", "/records"):
            with self.subTest(kw=kw):
                self.assertIn(kw, self.index, "docs/INDEX.md 缺入口：%s" % kw)

    def test_index_template_names_match_repo(self):
        tpl = os.path.join(APP_DIR, "templates")
        for name in ("grade_records.html", "import.html", "grade_result.html", "grade_upload.html"):
            with self.subTest(name=name):
                self.assertIn(name, self.index, "INDEX 没写模板文件名 %s" % name)
                self.assertTrue(os.path.isfile(os.path.join(tpl, name)), "模板不存在：%s" % name)

    def test_no_wrong_template_name(self):
        """/records 的模板叫 grade_records.html，全文档不许出现裸 records.html。"""
        wrong = re.compile(r"(?<!grade_)records\.html")
        for path in [README, os.path.join(DOCS, "INDEX.md")]:
            with self.subTest(path=os.path.basename(path)):
                text = read(path)
                bad = [m.group(0) for m in wrong.finditer(text)
                       if not any(w in text[max(0, m.start() - 12):m.start()]
                                  for w in ("不是", "没有", "不存在"))]
                self.assertEqual([], bad, "写成了不存在的 records.html：%s" % bad)

    def test_index_relative_links_resolve(self):
        base = DOCS
        for m in re.finditer(r"\]\((\.{0,2}/?[^)]+)\)", self.index):
            ref = m.group(1).split("#")[0]
            if not ref or ref.startswith(("http://", "https://", "mailto:")):
                continue
            with self.subTest(ref=ref):
                self.assertTrue(os.path.exists(os.path.join(base, ref)),
                                "docs/INDEX.md 链接指向不存在的路径：%s" % ref)


class TestDesignTokens(unittest.TestCase):
    """t100：全站视觉 token 必须真的落在 app.css 里，且旧类名有映射说明。"""

    @classmethod
    def setUpClass(cls):
        cls.css = read(os.path.join(APP_DIR, "static", "app.css"))
        cls.readme = read(README)

    def test_token_variables_present(self):
        for var in ("--brand:", "--brand-soft:", "--brand-strong:",
                    "--gray-900:", "--gray-700:", "--gray-500:", "--gray-200:", "--gray-50:",
                    "--r-lg:", "--r-sm:", "--s1:", "--s2:", "--s3:", "--s4:",
                    "--fs-h:", "--fs-body:", "--fs-small:"):
            with self.subTest(var=var):
                self.assertIn(var, self.css, "app.css 缺设计 token %s" % var)

    def test_old_variables_map_to_tokens(self):
        """旧变量名必须仍在（页面在用），且指向新 token。"""
        for old, new in (("--accent:", "var(--brand)"), ("--accent-soft:", "var(--brand-soft)"),
                         ("--ink:", "var(--gray-900)"), ("--ink-2:", "var(--gray-700)"),
                         ("--muted:", "var(--gray-500)"), ("--line:", "var(--gray-200)"),
                         ("--bg:", "var(--gray-50)"), ("--radius:", "var(--r-lg)")):
            with self.subTest(var=old):
                line = next((ln for ln in self.css.splitlines() if ln.strip().startswith(old)), "")
                self.assertIn(new, line, "%s 没有映射到 %s" % (old, new))

    def test_three_buttons_and_two_notices(self):
        for sel in (".btn-primary", ".btn-secondary", ".btn-ghost",
                    ".notice.ok", ".notice.warn", ".banner-ok"):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.css, "app.css 缺组件 %s" % sel)

    def test_mapping_documented(self):
        """改页面的人要能一眼看到「旧类名 → 新 token」的对照。"""
        blob = self.css[:4000]
        for word in ("设计 token", "旧类名映射", "--accent", "--radius", ".btn-ghost"):
            with self.subTest(word=word):
                self.assertIn(word, blob, "app.css 开头缺映射说明：%s" % word)
        for word in ("设计 token", "--brand", ".btn-secondary"):
            with self.subTest(word=word):
                self.assertIn(word, self.readme, "README 缺「设计 token 与旧类名映射」小节：%s" % word)


class TestDocsArtifacts(unittest.TestCase):
    def test_screenshots_present_and_nonempty(self):
        expected = ("01-compose.png", "02-papers-list.png", "03-guide.png", "04-health.png",
                    "05-paper-preview.png", "05b-print-page1.png", "05c-print-page2.png",
                    "06-grade-result.png", "07-train-paper.png",
                    "08-degraded-word.png", "09-degraded-db.png",
                    "10-layout-form.png", "11-layout-compact.png", "12-lan-qr.png",
                    "13-seed-reroll.png", "14-settings.png", "15-ocr-notice.png", "16-import-mobile.png",
                    "17-grade-records.png", "18-student-info.png", "19-avail-line.png")
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
