# -*- coding: utf-8 -*-
"""t11 · 判卷页接线 + 错题训练卷全链路（建卷 → 预览 → 上传 → 结果 → 确认 → 训练卷）。

真实链路，不打桩：
  * 照片是真的：拿 card.py 出的答题卡 PNG，按 manifest 里的选项坐标把错误选项涂黑，
    再当「拍照上传」喂给 grade_image（ArUco 四角、QR、OMR 全走真代码）；
  * 训练卷是真的：train.plan → picker.build(nos) → paper.render_paper → card.build_card；
  * 断言训练卷里不含原错题（题号集合不相交）。
"""
from __future__ import annotations

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
import db as dbmod  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def manifest_of(db_path: str, pid: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        row = conn.execute("SELECT manifest FROM papers WHERE pid=?", (pid,)).fetchone()
    finally:
        conn.close()
    return json.loads(row["manifest"])


def marked_card_png(out_dir: str, pid: str, manifest: dict, all_wrong: bool = True) -> bytes:
    """把答题卡 PNG 的选项格涂黑（默认故意选错的那个选项），当照片用。"""
    src = os.path.join(out_dir, pid, "card_1.png")
    image = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(image)
    by_idx = {it.get("idx"): it for it in manifest.get("items") or []}
    filled = 0
    for page in (manifest.get("card") or {}).get("pages") or []:
        for item in page.get("items") or []:
            if item.get("kind") != "omr":
                continue
            src_item = by_idx.get(item.get("idx")) or {}
            expected = str(((src_item.get("answer_key") or [{}])[0]).get("value") or "")
            cells = item.get("cells") or []
            target = None
            if all_wrong:
                target = next((c for c in cells if c.get("opt") != expected), None)
            else:
                target = next((c for c in cells if c.get("opt") == expected), None)
            if not target:
                continue
            pad = 10
            draw.rectangle(
                [target["x"] + pad, target["y"] + pad,
                 target["x"] + target["w"] - pad, target["y"] + target["h"] - pad],
                fill=(10, 10, 10))
            filled += 1
    assert filled, "答题卡上没找到可涂的选项格"
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="trainflow_")
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
        data = {"title": "训练链路卷", "seed": "5", "n_choice": "2",
                "n_fill": "0", "n_solution": "0", "need_answer": "on"}
        data.update(over)
        r = self.client.post("/papers", data=data, follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:400])
        return r.headers["location"].rsplit("/", 1)[-1]

    def paper_rows(self):
        conn = dbmod.connect(self.db_path)
        try:
            return {r["pid"]: dict(r) for r in conn.execute("SELECT * FROM papers")}
        finally:
            conn.close()


class TestRouterWiring(_Base):
    def test_grade_pages_mounted(self):
        r = self.client.get("/submissions")
        self.assertEqual(200, r.status_code, "判卷上传页没挂上（include_router 漏了？）")
        self.assertIn("判卷", text_of(r.text))

    def test_grade_page_nav_matches_base(self):
        html = self.client.get("/submissions").text
        for href in ('href="/"', 'href="/papers"', 'href="/submissions"',
                     'href="/train"', 'href="/guide"', 'href="/health"'):
            self.assertIn(href, html, "判卷页导航缺 %s" % href)

    def test_base_nav_points_to_real_grade_and_train(self):
        html = self.client.get("/").text
        self.assertIn('href="/submissions"', html)
        self.assertIn('href="/train"', html)
        t = text_of(html)
        self.assertNotIn("判卷待接线", t)
        self.assertNotIn("训练待接线", t)

    def test_submissions_answered_by_grade_routes(self):
        """server.py 不许自己再定义 /submissions*：空表单必须由 grade_routes 以 400 挡回。"""
        r = self.client.post("/submissions", data={"pid": ""}, follow_redirects=False)
        self.assertEqual(400, r.status_code, r.text[:300])
        self.assertNotIn("Traceback", r.text)


class TestTrainPage(_Base):
    def test_train_page_200(self):
        r = self.client.get("/train")
        self.assertEqual(200, r.status_code)
        t = text_of(r.text)
        self.assertIn("错题", t)
        self.assertIn("训练", t)
        self.assertIn("/submissions", r.text)

    def test_train_empty_input_is_friendly(self):
        r = self.client.post("/train", data={"nos": ""}, follow_redirects=False)
        self.assertIn(r.status_code, (400, 422))
        self.assertNotIn("Traceback", r.text)


class TestFullChain(_Base):
    """建卷 → 预览 → 上传 → 结果 → 确认 → 训练卷。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pid = cls.client.post("/papers", data={
            "title": "链路卷", "seed": "5", "n_choice": "2",
            "n_fill": "0", "n_solution": "0", "need_answer": "on"},
            follow_redirects=False).headers["location"].rsplit("/", 1)[-1]
        cls.manifest = manifest_of(cls.db_path, cls.pid)
        cls.wrong_nos = [it["no"] for it in cls.manifest["items"]]
        png = marked_card_png(cls.out_dir, cls.pid, cls.manifest, all_wrong=True)
        up = cls.client.post("/submissions", data={"pid": cls.pid},
                             files={"file": ("card.png", png, "image/png")},
                             follow_redirects=False)
        cls.upload_status = up.status_code
        cls.sid = up.headers.get("location", "").rstrip("/").rsplit("/", 1)[-1]
        conn = dbmod.connect(cls.db_path)
        try:
            row = conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                               (cls.sid,)).fetchone()
        finally:
            conn.close()
        cls.initial_score = float(row["score"])
        cls.initial_units = json.loads(row["detail_json"])["items"]

    def test_preview_ok(self):
        r = self.client.get("/papers/%s" % self.pid)
        self.assertEqual(200, r.status_code)
        self.assertIn("题目清单", text_of(r.text))

    def test_upload_redirects_to_result(self):
        self.assertEqual(302, self.upload_status)
        self.assertRegex(self.sid, r"^S\d{8}-\d{6}-[0-9a-f]+$")

    def test_result_page_shows_all_wrong(self):
        r = self.client.get("/submissions/%s" % self.sid)
        self.assertEqual(200, r.status_code)
        verdicts = [u["verdict"] for u in self.initial_units]
        self.assertEqual(len(self.wrong_nos), len(verdicts))
        self.assertTrue(all(v == "wrong" for v in verdicts), verdicts)
        self.assertEqual(0.0, self.initial_score)

    def test_confirm_one_item(self):
        r = self.client.post("/submissions/%s/confirm" % self.sid,
                             data={"verdict_1_1": "correct", "text_1_1": "A"},
                             follow_redirects=False)
        self.assertEqual(303, r.status_code)
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                               (self.sid,)).fetchone()
        finally:
            conn.close()
        detail = json.loads(row["detail_json"])
        verdicts = [u["verdict"] for u in detail["items"]]
        self.assertIn("correct", verdicts)
        self.assertGreater(float(row["score"]), 0.0)

    def test_train_paper_excludes_wrong_questions(self):
        r = self.client.post("/train", data={"sid": self.sid, "n_per": "3"},
                             follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:600])
        train_pid = r.headers["location"].rsplit("/", 1)[-1]
        self.assertNotEqual(self.pid, train_pid)
        rows = self.paper_rows()
        self.assertIn(train_pid, rows)
        train_manifest = manifest_of(self.db_path, train_pid)
        nos = [it["no"] for it in train_manifest["items"]]
        self.assertTrue(nos, "训练卷一道题都没有")
        self.assertEqual(set(), set(nos) & set(self.wrong_nos), "训练卷里还有原错题")
        self.assertIn("训练", (rows[train_pid]["title"] or ""))

    def test_train_paper_preview_renders(self):
        r = self.client.post("/train", data={"sid": self.sid, "n_per": "2"},
                             follow_redirects=False)
        train_pid = r.headers["location"].rsplit("/", 1)[-1]
        page = self.client.get("/papers/%s" % train_pid)
        self.assertEqual(200, page.status_code)
        t = text_of(page.text)
        self.assertIn("题目清单", t)
        self.assertRegex(t, r"共 \d+ 页（卷面 \d+ 页 \+ 答题卡 \d+ 页）")


class TestManualTrain(_Base):
    def test_train_from_explicit_nos(self):
        pid = self.compose()
        manifest = manifest_of(self.db_path, pid)
        nos = [it["no"] for it in manifest["items"]]
        r = self.client.post("/train", data={"nos": ",".join(nos), "n_per": "2"},
                             follow_redirects=False)
        self.assertEqual(302, r.status_code, r.text[:600])
        train_pid = r.headers["location"].rsplit("/", 1)[-1]
        train_nos = [it["no"] for it in manifest_of(self.db_path, train_pid)["items"]]
        self.assertEqual(set(), set(train_nos) & set(nos))

    def test_train_unknown_nos_is_friendly(self):
        r = self.client.post("/train", data={"nos": "99999"}, follow_redirects=False)
        self.assertGreaterEqual(r.status_code, 400)
        self.assertLess(r.status_code, 500)
        self.assertNotIn("Traceback", r.text)


if __name__ == "__main__":
    unittest.main()
