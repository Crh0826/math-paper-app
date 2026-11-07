# -*- coding: utf-8 -*-
"""t43 · seed 默认自动换（留空=每次换一套，填数字=可复现）。

用户原话：「seed 应当可以自动更改」。原来表单默认写死 42，不改就永远同一套题，
很容易让人以为题库就这么点。现在：

  * 留空 → 服务端 `secrets.randbelow(1000000)` 抽一个，写进 params.seed（并回显在预览页）；
  * 填数字 → 原样使用，同参数 + 同 seed 逐题一致（可复现）；
  * 预览页有「同参数换一套」按钮：同 mix/范围/版面，不带 seed，302 出新卷；
  * 预览页把最终 seed 显示出来，并写一句「填这个可复现」。
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


BASE_FORM = {"title": "seed 测试卷", "n_choice": "4", "n_fill": "0",
             "n_solution": "0", "need_answer": "on", "layout": "compact"}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="seed_")
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
        data = dict(BASE_FORM)
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

    def nos_of(self, pid):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT manifest FROM papers WHERE pid=?", (pid,)).fetchone()
        finally:
            conn.close()
        return [it["no"] for it in json.loads(row["manifest"])["items"]]


class TestForm(_Base):
    def test_seed_input_is_empty_by_default(self):
        html = self.client.get("/").text
        m = re.search(r'<input[^>]*name="seed"[^>]*>', html)
        self.assertIsNotNone(m, "表单里没有 seed 输入框")
        tag = m.group(0)
        self.assertRegex(tag, r'value=""', "seed 默认要留空，不能写死 42")
        self.assertNotIn('value="42"', tag)

    def test_copy_explains_auto_and_reproducible(self):
        t = text_of(self.client.get("/").text)
        self.assertIn("留空", t)
        self.assertIn("每份卷不同", t)
        self.assertIn("复现", t)


class TestAutoSeed(_Base):
    def test_blank_seed_is_random_each_time(self):
        a = self.compose(seed="")
        b = self.compose(seed="")
        sa, sb = self.params_of(a).get("seed"), self.params_of(b).get("seed")
        self.assertIsInstance(sa, int)
        self.assertIsInstance(sb, int)
        self.assertNotEqual(sa, sb, "留空两次拿到同一个 seed：%r" % sa)
        self.assertNotEqual(self.nos_of(a), self.nos_of(b), "留空两次抽到同一套题")

    def test_omitted_seed_field_also_auto(self):
        data = dict(BASE_FORM)
        data.pop("layout", None)
        r = self.client.post("/papers", data=data, follow_redirects=False)
        self.assertEqual(302, r.status_code)
        pid = r.headers["location"].rsplit("/", 1)[-1]
        self.assertIsInstance(self.params_of(pid).get("seed"), int)

    def test_fixed_seed_repeats_exactly(self):
        a = self.compose(seed="20261031")
        b = self.compose(seed="20261031")
        self.assertEqual(20261031, self.params_of(a)["seed"])
        self.assertEqual(self.nos_of(a), self.nos_of(b), "同参数同 seed 必须逐题一致")

    def test_auto_seed_bounds(self):
        pid = self.compose(seed="")
        seed = self.params_of(pid)["seed"]
        self.assertGreaterEqual(seed, 0)
        self.assertLess(seed, 1000000)


class TestPreview(_Base):
    def test_preview_shows_final_seed_and_hint(self):
        pid = self.compose(seed="")
        seed = self.params_of(pid)["seed"]
        t = text_of(self.client.get("/papers/%s" % pid).text)
        self.assertIn("随机种子", t)
        self.assertIn(str(seed), t, "预览页要把最终 seed 显示出来")
        self.assertIn("复现", t)

    def test_reroll_button_present(self):
        pid = self.compose(seed="123")
        html = self.client.get("/papers/%s" % pid).text
        self.assertIn("同参数换一套", text_of(html))
        self.assertIn('action="/papers"', html)

    def test_reroll_keeps_params_but_changes_seed(self):
        pid = self.compose(seed="123")
        page = self.client.get("/papers/%s" % pid).text
        form = page[page.find('action="/papers"'):]
        form = form[form.find(">") + 1:]
        form = form[:form.find("</form>")]
        fields = {}
        for name, value in re.findall(r'name="([^"]+)"\s+value="([^"]*)"', form):
            if name == "seed":
                continue
            fields[name] = value
        r = self.client.post("/papers", data=fields, follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:400])
        new_pid = r.headers["location"].rsplit("/", 1)[-1]
        old, new = self.params_of(pid), self.params_of(new_pid)
        self.assertEqual(old["mix"], new["mix"])
        self.assertEqual(old["scope"], new["scope"])
        self.assertEqual(old.get("layout"), new.get("layout"))
        self.assertNotEqual(old["seed"], new["seed"], "换一套必须换 seed")


class TestTrainSeed(_Base):
    """训练卷走同一条路：留空=自动换，显式给 seed=可复现。"""

    def make_paper(self):
        return self.compose(seed="777")

    def test_train_seed_auto(self):
        pid = self.make_paper()
        nos = self.nos_of(pid)[:2]
        a = self.client.post("/train", data={"nos": ",".join(nos), "n_per": "2"},
                             follow_redirects=False)
        b = self.client.post("/train", data={"nos": ",".join(nos), "n_per": "2"},
                             follow_redirects=False)
        self.assertEqual(302, a.status_code, a.text[:300])
        self.assertEqual(302, b.status_code, b.text[:300])
        pa = self.params_of(a.headers["location"].rsplit("/", 1)[-1])
        pb = self.params_of(b.headers["location"].rsplit("/", 1)[-1])
        self.assertNotEqual(pa["seed"], pb["seed"], "训练卷留空 seed 也要每次不同")

    def test_train_seed_fixed_is_reproducible(self):
        pid = self.make_paper()
        nos = self.nos_of(pid)[:2]
        a = self.client.post("/train", data={"nos": ",".join(nos), "n_per": "2", "seed": "1234"},
                             follow_redirects=False)
        b = self.client.post("/train", data={"nos": ",".join(nos), "n_per": "2", "seed": "1234"},
                             follow_redirects=False)
        pid_a = a.headers["location"].rsplit("/", 1)[-1]
        pid_b = b.headers["location"].rsplit("/", 1)[-1]
        self.assertEqual(1234, self.params_of(pid_a)["seed"])
        self.assertEqual(self.nos_of(pid_a), self.nos_of(pid_b), "同 seed 训练卷必须一致")


if __name__ == "__main__":
    unittest.main()
