# -*- coding: utf-8 -*-
"""t40 · 卷面不留书写区（space 默认全 0），但显式给值时能力还在。

背景（用户实测 2026-10-31）：组卷默认参数写着 `{"选择":0,"填空":2,"解答":8}`，
于是 30 题卷里每道解答题硬塞 8 段（≈6.8cm）空白 —— 可作答其实写在答题卡上，卷面根本不需要。
修法：server 不再自己写死一份，直接取 `picker.DEFAULT_SPACE`（全 0）；表单可用 `space_<题型>` 覆盖。

守的事：
  * 真库出 24 题（10/8/6）卷：manifest 里每道题 `space_paras == 0`，卷面页数不因空白膨胀；
  * 显式 `space_解答=4`：解答题确实多出 4 段（能力没丢），填空仍是 0；
  * `_params_from_values` 的默认值就是 picker.DEFAULT_SPACE（单一真值来源，不许再写死）。
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
import picker  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


FORM = {"title": "留白测试卷", "seed": "11", "n_choice": "10", "n_fill": "8",
        "n_solution": "6", "need_answer": "on"}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="space_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=200, db_path=cls.db_path)
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

    def manifest_of(self, pid):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT manifest FROM papers WHERE pid=?", (pid,)).fetchone()
        finally:
            conn.close()
        return json.loads(row["manifest"])

    def preview_pages(self, pid):
        t = text_of(self.client.get("/papers/%s" % pid).text)
        m = re.search(r"共 (\d+) 页（卷面 (\d+) 页 \+ 答题卡 (\d+) 页）", t)
        self.assertIsNotNone(m, t[:300])
        return int(m.group(1)), int(m.group(2)), int(m.group(3))


class TestParamsDefault(_Base):
    def test_default_space_is_picker_default(self):
        values = {"title": "x", "seed": 1, "n_choice": 1, "n_fill": 0, "n_solution": 0,
                  "chapters": [], "kps": [], "kp_mode": "any", "need_answer": True,
                  "exclude_empty": True, "layout": "compact"}
        params = server._params_from_values(values)
        self.assertEqual(dict(picker.DEFAULT_SPACE), params["space"],
                         "默认必须直接取 picker.DEFAULT_SPACE，不许再写死一份")

    def test_no_positive_space_literal_in_server(self):
        with open(os.path.join(APP_DIR, "server.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('"解答": 8', source)
        self.assertNotIn('"填空": 2', source)


class TestComposeNoBlank(_Base):
    def test_24_item_paper_has_no_writing_space(self):
        pid = self.compose()
        manifest = self.manifest_of(pid)
        self.assertEqual(24, len(manifest["items"]))
        for it in manifest["items"]:
            with self.subTest(no=it["no"], qtype=it["qtype"]):
                self.assertEqual(0, it["space_paras"],
                                 "%s（%s）还留着 %d 段书写区" % (it["no"], it["qtype"], it["space_paras"]))
        self.assertEqual(dict(picker.DEFAULT_SPACE), manifest["params"]["space"])

    def test_preview_pages_do_not_inflate(self):
        pid = self.compose()
        total, paper_n, card_n = self.preview_pages(pid)
        self.assertEqual(total, paper_n + card_n)
        self.assertLessEqual(paper_n, 4,
                             "24 题卷面 %d 页 —— 留白又回来了？" % paper_n)

    def test_preview_mentions_no_extra_blank(self):
        """预览页不该出现「书写区 / 作答区」这类卷面留白说法（作答在答题卡上）。"""
        pid = self.compose()
        t = text_of(self.client.get("/papers/%s" % pid).text)
        self.assertNotIn("书写区", t)
        self.assertIn("答题卡", t)


class TestExplicitSpace(_Base):
    def test_explicit_solution_space_still_works(self):
        pid = self.compose(space_解答="4")
        manifest = self.manifest_of(pid)
        solutions = [it for it in manifest["items"] if it["qtype"] == "解答"]
        fills = [it for it in manifest["items"] if it["qtype"] == "填空"]
        self.assertTrue(solutions and fills)
        self.assertTrue(all(it["space_paras"] == 4 for it in solutions),
                        [it["space_paras"] for it in solutions])
        self.assertTrue(all(it["space_paras"] == 0 for it in fills),
                        [it["space_paras"] for it in fills])

    def test_explicit_space_increases_pages(self):
        plain = self.compose()
        plain_pages = self.preview_pages(plain)[1]
        wide = self.compose(space_解答="8")
        wide_pages = self.preview_pages(wide)[1]
        self.assertGreater(wide_pages, plain_pages,
                           "显式给 8 段后卷面页数该变多：%d vs %d" % (wide_pages, plain_pages))


class TestFormCopy(_Base):
    def test_form_mentions_answers_on_card(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("答题卡", t)
        self.assertIn("不留书写区", t)


if __name__ == "__main__":
    unittest.main()
