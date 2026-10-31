# -*- coding: utf-8 -*-
"""t13 · 省纸模式（layout=compact / normal）从表单一路串到预览页。

守的事：
  * 组卷表单有「省纸模式」复选框，**默认勾选**，文案写清收益；
  * 勾选 → 表单提交出 layout=compact（隐藏域在前、复选框在后，服务端取最后一个值，不依赖 JS）；
    取消勾选 → layout=normal；
  * 值落到 CONTRACT §2 的 params.layout，并随卷子存进 papers.params_json；
  * 预览页回显「版面：省纸 / 宽裕」，页数拆成「卷面 a + 卡 b」；
  * 训练卷同样默认省纸；
  * /guide 与 README 都提一句省纸模式（指南里还要说明不影响「100% 打印」的要求）。
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


FORM = {"title": "省纸测试卷", "seed": "7", "n_choice": "2", "n_fill": "1",
        "n_solution": "0", "need_answer": "on"}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="layout_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=120, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
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

    def compose(self, **over):
        data = dict(FORM)
        data.update(over)
        r = self.client.post("/papers", data=data, follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:400])
        return r.headers["location"].rsplit("/", 1)[-1]

    def params_of(self, pid):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT params_json FROM papers WHERE pid=?", (pid,)).fetchone()
        finally:
            conn.close()
        return json.loads(row["params_json"] or "{}")


class TestForm(_Base):
    def test_checkbox_present_and_checked_by_default(self):
        html = self.client.get("/").text
        self.assertIn('name="layout" value="compact"', html)
        block = html[html.find('value="compact"'):]
        block = block[:block.find(">") + 1]
        self.assertIn("checked", block, "省纸模式默认要勾上")
        self.assertIn('name="layout" value="normal"', html, "取消勾选要能提交 normal")
        # 隐藏域必须排在复选框前面，服务端取最后一个值才不会把勾选吃掉
        self.assertLess(html.find('value="normal"'), html.find('value="compact"'))

    def test_copy_mentions_benefit(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("省纸模式", t)
        self.assertIn("页边距与行距收紧", t)
        self.assertIn("10 题 1 页", t)

    def test_guide_page_sends_layout_too(self):
        """GET / 带 layout=normal 时，复选框要回显为未勾选。"""
        html = self.client.get("/?layout=normal").text
        block = html[html.find('name="layout" value="compact"'):]
        block = block[:block.find(">") + 1]
        self.assertNotIn("checked", block)


class TestParams(_Base):
    def test_compact_is_default(self):
        pid = self.compose(layout="compact")
        self.assertEqual("compact", self.params_of(pid).get("layout"))

    def test_normal_when_unchecked(self):
        pid = self.compose(layout="normal")
        self.assertEqual("normal", self.params_of(pid).get("layout"))

    def test_last_value_wins(self):
        """隐藏域 normal + 勾选 compact 一起提交时，取最后一个（compact）。"""
        r = self.client.post("/papers", data=None,
                             content=b"title=x&seed=7&n_choice=2&n_fill=0&n_solution=0"
                                     b"&need_answer=on&layout=normal&layout=compact",
                             headers={"Content-Type": "application/x-www-form-urlencoded"},
                             follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:300])
        pid = r.headers["location"].rsplit("/", 1)[-1]
        self.assertEqual("compact", self.params_of(pid).get("layout"))

    def test_invalid_value_falls_back_to_compact(self):
        pid = self.compose(layout="weird")
        self.assertEqual("compact", self.params_of(pid).get("layout"), "脏值必须退回默认省纸")

    def test_missing_value_defaults_to_compact(self):
        pid = self.compose()
        self.assertEqual("compact", self.params_of(pid).get("layout"))


class TestPreview(_Base):
    def test_preview_shows_compact_label_and_page_split(self):
        pid = self.compose(layout="compact")
        html = self.client.get("/papers/%s" % pid).text
        t = text_of(html)
        self.assertIn("版面", t)
        self.assertIn("省纸", t)
        self.assertRegex(t, r"共 \d+ 页（卷面 \d+ 页 \+ 答题卡 \d+ 页）")

    def test_preview_shows_normal_label(self):
        pid = self.compose(layout="normal")
        t = text_of(self.client.get("/papers/%s" % pid).text)
        self.assertIn("宽裕", t)

    def test_train_paper_defaults_to_compact(self):
        pid = self.compose()
        r = self.client.post("/train", data={"nos": "00001"}, follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:300])
        tpid = r.headers["location"].rsplit("/", 1)[-1]
        self.assertEqual("compact", self.params_of(tpid).get("layout"))
        self.assertIn("省纸", text_of(self.client.get("/papers/%s" % tpid).text))


class TestDocs(_Base):
    def test_guide_mentions_compact_and_print_requirements(self):
        t = text_of(self.client.get("/guide").text)
        self.assertIn("省纸模式", t)
        self.assertIn("100%", t)

    def test_readme_mentions_compact(self):
        with open(os.path.join(APP_DIR, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("省纸模式", readme)
        self.assertIn("layout=normal", readme)


if __name__ == "__main__":
    unittest.main()
