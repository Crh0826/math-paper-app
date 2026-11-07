# -*- coding: utf-8 -*-
"""t76 · 「让 AI 把 Word 转成 JSON」：页面一键复制提示词 + 示例 JSON 下载 + 文档。

页面与 docs/AI-转JSON-提示词.md 必须是同一份提示词（队长给定，一字不差），
示例 JSON 必须能被 importers 真实解析并零错误导入。
"""
from __future__ import annotations

import html as html_mod
import io
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
import importers  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 队长给定的提示词（测试里独立抄一份：页面/文档任何一方漂移都会红）
CANONICAL_PROMPT = """你是题库格式转换器。下面我贴一段题目（从 Word 复制即可），请转成我需要的 JSON。
只输出 JSON，不要解释、不要 Markdown 代码块、不要省略号。

{"subject":"数学","grade":"七年级上","version":"沪科版","items":[{"no":"01001","qtype":"选择","stem":"…","options":["A. …","B. …","C. …","D. …"],"answer":"B","kp":"","chapter":""}]}

规则：
- no：5 位数字字符串，从 00001 顺序编，文件内不重复
- qtype：只能是「选择」「填空」「解答」
- stem：题干纯文本，公式保留原样（2x+1=5、x²、√3），不要改写或翻译
- options：仅选择题需要，4 项 A-D 开头；其他题型给 []
- answer：只写最终答案。选择写字母（B 或 AB）；填空/解答写答案本身；多解用英文逗号分隔（-1,2）；没有答案给 ""
- kp / chapter：能从原文看出才填，看不出给 ""
- 分数写 3/4，带分数写 1 3/4，圆周率写 π
- 一份材料里有多道题就逐题输出；只有一道就 1 项
- 不确定的字段宁可留空，不要编造"""

DOCS_PATH = os.path.join(APP_DIR, "docs", "AI-转JSON-提示词.md")
INDEX_PATH = os.path.join(APP_DIR, "docs", "INDEX.md")


class AIPromptBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ai_prompt_")
        cls.db_path = os.path.join(cls.tmp, "app.db")
        build_db.build(limit=60, db_path=cls.db_path)
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as fh:
            json.dump({"ocr": {"backend": "manual"}}, fh)
        cls.client = TestClient(server.create_app(db_path=cls.db_path, config_path=cls.cfg,
                                                  out_dir=os.path.join(cls.tmp, "out")))
        cls.page = cls.client.get("/import").text

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPage(AIPromptBase):
    def test_prompt_is_verbatim_in_page(self):
        m = re.search(r'<textarea[^>]*id="ai-prompt"[^>]*>(.*?)</textarea>',
                      self.page, re.S)
        self.assertIsNotNone(m, "页面里要有可复制的提示词 textarea")
        shown = html_mod.unescape(m.group(1)).strip()
        self.assertEqual(CANONICAL_PROMPT, shown, "页面上的提示词必须与规范一字不差")

    def test_prompt_key_snippets(self):
        for snippet in ("你是题库格式转换器", "只输出 JSON", "qtype：只能是「选择」「填空」「解答」",
                        "分数写 3/4", "不确定的字段宁可留空，不要编造"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.page)

    def test_collapsible_block_closed_by_default(self):
        m = re.search(r"<details[^>]*id=\"ai-hint\"[^>]*>", self.page)
        self.assertIsNotNone(m, "要有可折叠的 ai-hint 区块")
        self.assertNotIn("open", m.group(0), "默认必须折叠，别挤占上传区")
        self.assertIn("没有 JSON？让 AI 帮你转", self.page)

    def test_three_steps_present(self):
        for word in ("复制提示词", "贴给任意 AI", "存成 .json 再拖回来"):
            with self.subTest(step=word):
                self.assertIn(word, self.page)

    def test_copy_button_and_fallback(self):
        self.assertIn('id="copy-prompt"', self.page)
        self.assertIn("navigator.clipboard", self.page, "优先用剪贴板 API")
        self.assertIn("execCommand", self.page, "失败要回退到「选中 + 复制」")
        self.assertIn('id="copy-feedback"', self.page, "复制成功要有反馈位置")

    def test_download_link(self):
        self.assertIn('href="/import/template.json"', self.page)
        self.assertIn("下载示例 JSON", self.page)
        m = re.search(r'<a[^>]*href="/import/template\.json"[^>]*>', self.page)
        self.assertIsNotNone(m)
        self.assertIn("download", m.group(0))

    def test_prompt_textarea_mobile_safe_css(self):
        css = self.page
        block = css[css.find(".ai-prompt {"):]
        block = block[:block.find("}") + 1]
        for rule in ("width: 100%", "box-sizing: border-box", "overflow-wrap: anywhere",
                     "white-space: pre-wrap"):
            with self.subTest(rule=rule):
                self.assertIn(rule, block, "375px 下提示词不能撑破页面")


class TestTemplateJson(AIPromptBase):
    def _download(self):
        r = self.client.get("/import/template.json")
        self.assertEqual(200, r.status_code, r.text)
        return r

    def test_attachment_and_shape(self):
        r = self._download()
        disp = r.headers.get("content-disposition", "")
        self.assertIn("attachment", disp)
        self.assertIn(".json", disp)
        self.assertIn("application/json", r.headers.get("content-type", ""))
        data = json.loads(r.content.decode("utf-8"))
        self.assertIn("items", data)
        self.assertEqual(2, len(data["items"]))
        qtypes = [it["qtype"] for it in data["items"]]
        self.assertIn("选择", qtypes)
        self.assertIn("填空", qtypes)
        choose = next(it for it in data["items"] if it["qtype"] == "选择")
        self.assertEqual(4, len(choose["options"]))
        self.assertEqual("B", choose["answer"])

    def test_template_is_parseable_by_importers(self):
        tmp = tempfile.mkdtemp(prefix="ai_tpl_")
        try:
            path = os.path.join(tmp, "qbank-template.json")
            with open(path, "wb") as fh:
                fh.write(self._download().content)
            meta, items = importers.read_source(path, "json")
            self.assertEqual(2, len(items))
            self.assertEqual("数学", meta.get("subject"))
            normalized, errors = importers.normalize_rows(items)
            self.assertEqual([], errors, "示例 JSON 必须零错误：%s" % errors)
            self.assertEqual(2, len(normalized))
            for it in normalized:
                with self.subTest(no=it["no"]):
                    self.assertIn(it["qtype"], ("选择", "填空", "解答"))
                    self.assertTrue(it["stem"])
                    self.assertTrue(it["answer"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_template_imports_through_the_route(self):
        r = self.client.post("/import", data={"confirm": "1"},
                             files={"file": ("qbank-template.json",
                                             self._download().content, "application/json")})
        self.assertEqual(200, r.status_code)
        rep = self.client.get("/import/report.json").json()
        self.assertEqual(2, rep["total"])
        self.assertEqual(2, rep["inserted"], "示例 JSON 应该能直接新增 2 道题")
        self.assertEqual(0, rep["updated"], "示例题号不该撞上真题（撞了会覆盖）")
        self.assertEqual(0, rep["failed"])

    def test_sample_numbers_avoid_real_corpus(self):
        data = json.loads(self._download().content.decode("utf-8"))
        for it in data["items"]:
            with self.subTest(no=it["no"]):
                self.assertTrue(it["no"].startswith("99"),
                                "示例题号要在 99xxx 高位段，避免覆盖真题")


class TestDocs(unittest.TestCase):
    def test_docs_file_exists_and_has_verbatim_prompt(self):
        self.assertTrue(os.path.isfile(DOCS_PATH), "docs/AI-转JSON-提示词.md 必须存在")
        with open(DOCS_PATH, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("```text", text, "提示词要放在可直接复制的代码块里")
        normalised = re.sub(r"\r\n", "\n", text)
        self.assertIn(CANONICAL_PROMPT, normalised, "文档里的提示词必须与规范一字不差")

    def test_docs_has_three_steps(self):
        with open(DOCS_PATH, encoding="utf-8") as fh:
            text = fh.read()
        for step in ("复制提示词", "贴给任意 AI", "存成 `.json` 再拖回来",
                     "/import/template.json"):
            with self.subTest(step=step):
                self.assertIn(step, text)

    def test_index_links_the_doc(self):
        with open(INDEX_PATH, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("AI-转JSON-提示词.md", text, "docs/INDEX.md 要指向这个文件")
        self.assertIn("template.json", text)


if __name__ == "__main__":
    unittest.main()
