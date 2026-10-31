# -*- coding: utf-8 -*-
"""T22/T30/T35 · 判卷页面路由（grade_routes.py）测试。

只挂本 router 的临时 app + 临时库；不碰真网络（识别器一律注入 manual / 假识别器）。

流程断言：
  1. GET  /submissions            上传页：列出最近卷子 + 选图/拍照表单
  2. POST /submissions            存图 → grade_image → 302 /submissions/{sid}；warp 失败渲染错误页
  3. GET  /submissions/{sid}      结果页：总分 + 逐题表 + 待确认裁切图 + 「识别中」
  4. POST /submissions/{sid}/reocr 幂等重识别；manual 后端 → empty_batch 提示
  5. POST /submissions/{sid}/confirm 逐题改判/填答案 → 分数变化（含多空题部分改判、fetch 的 JSON 回执）
  6. GET  /submissions/{sid}/status  JSON：pending / filled / backend / empty_batch
  7. GET  /submissions/{sid}/annotated.png  批注图（缓存 + 404 不 500）
  8. GET  /submissions/{sid}/item/{idx}/why  单题证据（JSON / 抽屉片段 / 独立页）
  9. GET  /submissions/{sid}/export.zip     证据包（annotated + crops + evidence.json）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import card  # noqa: E402
import db as dbmod  # noqa: E402
import grade  # noqa: E402
import grade_routes  # noqa: E402
import ocr  # noqa: E402

PID = "P20261031-931"
ITEMS = [
    {"idx": 1, "no": "00001", "qtype": "选择", "options": ["A", "B", "C", "D"],
     "answer_key": [{"blank": 1, "value": "A", "norm": "A"}]},
    {"idx": 2, "no": "00002", "qtype": "选择", "options": ["A", "B", "C", "D"],
     "answer_key": [{"blank": 1, "value": "B", "norm": "B"}]},
    {"idx": 3, "no": "00003", "qtype": "选择", "options": ["A", "B", "C", "D"],
     "answer_key": [{"blank": 1, "value": "C", "norm": "C"}]},
    {"idx": 4, "no": "00004", "qtype": "填空",
     "answer_key": [{"blank": 1, "value": "4", "norm": "4"},
                    {"blank": 2, "value": "1/2", "norm": "1/2"}]},
    {"idx": 5, "no": "00005", "qtype": "解答",
     "answer_key": [{"blank": 1, "value": "9", "norm": "9"},
                    {"blank": 2, "value": "3", "norm": "3"}]},
]
CHOICES = {1: "A", 2: "B", 3: "C"}
TEXTS = {"4-1": "4", "4-2": "1/2", "5-1": "9", "5-2": "3"}


def _font(size=56):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size, index=0)
    return ImageFont.load_default()


def make_manifest(pid=PID):
    return {
        "pid": pid,
        "created_at": "2026-10-31T15:00:00+08:00",
        "title": "七年级数学 · 判卷路由测试卷",
        "params": {},
        "page": {"w_pt": 595.3, "h_pt": 841.9, "dpi": 300,
                 "w_px": card.PAGE_W, "h_px": card.PAGE_H},
        "items": [dict(it) for it in ITEMS],
    }


def build_photo(tmp, manifest, choices=None, texts=None, name="photo.png"):
    """真答题卡 PNG → 涂黑选择框 + 在填空/解答框写字 → 直接当「照片」（不模拟透视）。"""
    card_dir = os.path.join(tmp, "card")
    card.build_card(manifest, card_dir)
    choices = CHOICES if choices is None else choices
    texts = TEXTS if texts is None else texts
    img = Image.open(os.path.join(card_dir, "card_1.png")).convert("L")
    draw = ImageDraw.Draw(img)
    for page in manifest["card"]["pages"]:
        for it in page["items"]:
            if it["kind"] == card.KIND_OMR:
                want = choices.get(it["idx"])
                for c in it["cells"]:
                    if str(c["opt"]) == str(want):
                        draw.rectangle([c["x"] + 8, c["y"] + 8,
                                        c["x"] + c["w"] - 8, c["y"] + c["h"] - 8], fill=0)
            else:
                boxes = it.get("boxes") or it.get("answer_boxes") or []
                for b in boxes:
                    text = texts.get("%d-%d" % (it["idx"], b["blank"]))
                    if text:
                        draw.text((b["x"] + 24, b["y"] + 24), text, font=_font(), fill=0)
    path = os.path.join(tmp, name)
    img.save(path)
    return path


class FakeOcr:
    """假识别器：按 idx-blank 给文本（不碰网络）。"""

    def __init__(self, mapping, backend="fake"):
        self.mapping = dict(mapping)
        self.backend_name = backend
        self.last_error = ""

    def _one(self, key):
        text = self.mapping.get(key, "")
        return {"text": text, "confidence": 0.9 if text else 0.0, "backend": self.backend_name}

    def read(self, image_path, kind=None):
        return self._one(os.path.splitext(os.path.basename(image_path))[0])

    def read_batch(self, items):
        return {str(k): self._one(str(k)) for k, _p in items}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.mkdtemp(prefix="gr_shared_")
        cls.manifest = make_manifest()
        cls.photo = build_photo(cls.shared, cls.manifest)
        # 第 3 题故意不涂 → OMR 判 unsure（给「为什么」抽屉的 unsure 态用）
        cls.photo_unfilled = build_photo(cls.shared, cls.manifest,
                                         choices={1: "A", 2: "B"},
                                         name="photo_unfilled.png")
        cls.blank_png = os.path.join(cls.shared, "blank.png")
        Image.new("L", (1200, 1600), 255).save(cls.blank_png)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gr_")
        self.db_path = os.path.join(self.tmp, "data", "app.db")
        conn = dbmod.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO papers(pid,created_at,title,params_json,manifest,n_items)"
            " VALUES (?,?,?,?,?,?)",
            (PID, self.manifest["created_at"], self.manifest["title"], "{}",
             json.dumps(self.manifest, ensure_ascii=False), len(ITEMS)))
        conn.commit()
        conn.close()
        self.out_dir = os.path.join(self.tmp, "out")
        self.client = self.make_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 装置
    def make_client(self, db_path=None, out_dir=None, recognizer=None,
                    auto_ocr=False, retry_after=None):
        """auto_ocr 缺省 False：绝大多数用例要看「同步阶段」的状态，别让后台线程改写它。

        自动识别的真实路径由 TestAutoOcr 显式打开（auto_ocr=True）来测。
        """
        app = FastAPI()
        app.include_router(grade_routes.router)
        app.state.db_path = db_path or self.db_path
        app.state.out_dir = out_dir or self.out_dir
        app.state.recognizer = recognizer
        app.state.auto_ocr = auto_ocr
        if retry_after is not None:
            app.state.ocr_retry_after = retry_after
        return TestClient(app)

    def upload(self, client=None, pid=PID, photo=None, follow=True):
        client = client or self.client
        photo = photo or self.photo
        with open(photo, "rb") as fh:
            data = fh.read()
        return client.post("/submissions", data={"pid": pid},
                           files={"file": ("photo.png", data, "image/png")},
                           follow_redirects=follow)

    def sid_of(self, client=None):
        res = self.upload(client)
        self.assertEqual(200, res.status_code, res.text[:500])
        return res.url.path.rstrip("/").split("/")[-1]

    def detail(self, sid):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()
        finally:
            conn.close()
        return json.loads(row["detail_json"])

    def score(self, sid):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT score, full FROM submissions WHERE sid=?", (sid,)).fetchone()
        finally:
            conn.close()
        return row["score"], row["full"]

    def insert_fake(self, sid, pid, score=0, full=1, detail=None, created="2026-10-31T18:00:00+08:00"):
        """直接插一条 submission（不需要图片）：用来测筛选/删除/非法 pid 等边界。"""
        payload = detail if detail is not None else {
            "items": [{"idx": 1, "no": "00001", "qtype": "选择", "kind": "omr", "blank": 1,
                       "verdict": "correct", "picked": "A", "expected": "A",
                       "confidence": 0.99, "ratios": {"A": 1.0}}],
            "backend": "manual",
        }
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO submissions"
                "(sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, pid, created, os.path.join(self.tmp, "fake.jpg"), 1, 1, score, full,
                 json.dumps(payload, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()


# --------------------------------------------------------------------------- 路由表
class TestRouteTable(unittest.TestCase):
    def test_paths(self):
        paths = {getattr(r, "path", "") for r in grade_routes.router.routes}
        for want in ("/submissions", "/submissions/{sid}", "/submissions/{sid}/status",
                     "/submissions/{sid}/reocr", "/submissions/{sid}/confirm",
                     "/submissions/{sid}/crop/{name}"):
            self.assertIn(want, paths, "缺路由 %s，现有 %s" % (want, sorted(paths)))

    def test_methods(self):
        table = {}
        for r in grade_routes.router.routes:
            for m in getattr(r, "methods", set()) or ():
                table.setdefault(r.path, set()).add(m)
        self.assertIn("POST", table["/submissions"])
        self.assertIn("GET", table["/submissions"])
        self.assertIn("POST", table["/submissions/{sid}/reocr"])
        self.assertIn("POST", table["/submissions/{sid}/confirm"])
        self.assertIn("GET", table["/submissions/{sid}/status"])


# --------------------------------------------------------------------------- 上传页
class TestUploadPage(_Base):
    def test_lists_recent_papers(self):
        res = self.client.get("/submissions")
        self.assertEqual(200, res.status_code)
        html = res.text
        self.assertIn(PID, html)
        self.assertIn(self.manifest["title"], html)
        self.assertIn("上传并判卷", html)
        self.assertIn('name="file"', html)
        self.assertIn('name="pid"', html)

    def test_prefills_pid_from_query(self):
        res = self.client.get("/submissions?pid=" + PID)
        self.assertEqual(200, res.status_code)
        self.assertIn('value="%s"' % PID, res.text)

    def test_missing_db_does_not_create_file(self):
        missing = os.path.join(self.tmp, "nope", "app.db")
        client = self.make_client(db_path=missing)
        res = client.get("/submissions")
        self.assertEqual(503, res.status_code)
        self.assertIn("找不到题库数据库", res.text)
        self.assertFalse(os.path.exists(missing), "缺库时不许顺手建空库")

    def test_empty_papers_table(self):
        empty_db = os.path.join(self.tmp, "empty.db")
        dbmod.connect(empty_db).close()
        client = self.make_client(db_path=empty_db)
        res = client.get("/submissions")
        self.assertEqual(200, res.status_code)
        self.assertIn("还没有生成过卷子", res.text)


# --------------------------------------------------------------------------- 上传判卷
class TestUpload(_Base):
    def test_redirects_to_result(self):
        res = self.upload(follow=False)
        self.assertEqual(302, res.status_code)
        loc = res.headers["location"]
        self.assertRegex(loc, r"^/submissions/S[\w\-]+$")

    def test_saves_source_jpg_and_crops(self):
        sid = self.sid_of()
        sub_dir = os.path.join(self.out_dir, PID, "sub_%s" % sid)
        self.assertTrue(os.path.isfile(os.path.join(sub_dir, "source.jpg")), "source.jpg 必须落盘")
        crops = [n for n in os.listdir(sub_dir) if n.endswith(".png")]
        self.assertGreaterEqual(len(crops), 4, "填空/解答裁切图应在同一目录：%s" % crops)
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM submissions WHERE sid=?", (sid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(1, row["warp_ok"])
        self.assertEqual(PID, row["pid"])
        self.assertIn(sub_dir, row["image_path"])

    def test_sync_score_only_choices(self):
        sid = self.sid_of()
        score, full = self.score(sid)
        self.assertEqual(3, score, "同步阶段只算 3 道选择题")
        self.assertEqual(7, full, "3 选择 + 2 填空空 + 2 解答空 = 7 个判分单元")

    def test_warp_failed_renders_error_page(self):
        res = self.upload(photo=self.blank_png)
        self.assertEqual(422, res.status_code)
        html = res.text
        self.assertIn("答题卡四角没认出来", html)          # errors.explain("warp_failed").title
        self.assertIn("/guide#err-warp_failed", html)      # 指南锚点
        self.assertIn("重新拍照", html)                    # retry_label
        conn = dbmod.connect(self.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(0, n, "标定失败不落库")

    def test_paper_not_found(self):
        res = self.upload(pid="P-NOT-EXIST")
        self.assertEqual(422, res.status_code)
        self.assertIn("找不到这份卷子", res.text)

    def test_empty_file_returns_form(self):
        res = self.client.post("/submissions", data={"pid": PID},
                               files={"file": ("empty.jpg", b"", "image/jpeg")})
        self.assertEqual(400, res.status_code)
        self.assertIn("没收到图片", res.text)
        self.assertIn("上传并判卷", res.text, "校验失败要回到上传表单，而不是白屏")

    def test_missing_pid_returns_form(self):
        with open(self.photo, "rb") as fh:
            data = fh.read()
        res = self.client.post("/submissions", data={"pid": "  "},
                               files={"file": ("photo.png", data, "image/png")})
        self.assertEqual(400, res.status_code)
        self.assertIn("先填卷号", res.text)

    def test_unsafe_pid_rejected(self):
        """卷号会拼进产物目录名：../ 之类不许过（防写到 app/out 外面）。"""
        res = self.upload(pid="../../etc")
        self.assertEqual(400, res.status_code)
        self.assertIn("卷号格式不对", res.text)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "etc")),
                         "不安全卷号不能真的建目录")

    def test_not_an_image_rejected(self):
        """PDF/文本冒充照片：先挡在门外，别等 warp_failed 说「四角没认出来」。"""
        res = self.client.post("/submissions", data={"pid": PID},
                               files={"file": ("scan.pdf", b"%PDF-1.7\n...", "application/pdf")})
        self.assertEqual(400, res.status_code)
        self.assertIn("这不是图片文件", res.text)
        self.assertIn("上传并判卷", res.text)


# --------------------------------------------------------------------------- 结果页
class TestResultPage(_Base):
    def test_score_and_table(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s" % sid)
        self.assertEqual(200, res.status_code)
        html = res.text
        self.assertIn('data-score="3"', html)
        self.assertIn('data-full="7"', html)
        for word in ("题号", "题型", "你的答案", "标准答案", "判定", "置信度"):
            self.assertIn(word, html)
        self.assertIn("判对", html)
        self.assertIn("待识别", html)
        self.assertIn("识别中（约 30 秒）", html, "还有 pending 时要提示识别中")
        self.assertIn("crop/4-1.png", html, "unsure/pending 要显示裁切图")

    def test_trailing_slash_also_works(self):
        sid = self.sid_of()
        self.assertEqual(200, self.client.get("/submissions/%s/" % sid).status_code)

    def test_unknown_sid_404(self):
        res = self.client.get("/submissions/S-NOT-EXIST")
        self.assertEqual(404, res.status_code)
        self.assertIn("找不到这次判卷记录", res.text)

    def test_crop_route(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s/crop/4-1.png" % sid)
        self.assertEqual(200, res.status_code)
        self.assertEqual("image/png", res.headers["content-type"])
        self.assertEqual(404, self.client.get("/submissions/%s/crop/nope.png" % sid).status_code)
        self.assertEqual(404, self.client.get("/submissions/%s/crop/..%%2F..%%2Fapp.db" % sid).status_code)

    def test_status_json(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s/status" % sid)
        self.assertEqual(200, res.status_code)
        data = res.json()
        self.assertEqual(sid, data["sid"])
        self.assertEqual(PID, data["pid"])
        self.assertEqual(4, data["pending"])
        self.assertEqual(0, data["filled"])
        self.assertFalse(data["empty_batch"])
        self.assertEqual("manual", data["backend"])
        self.assertEqual(3, data["score"])
        self.assertEqual(7, data["full"])
        self.assertFalse(data["done"])
        self.assertEqual(404, self.client.get("/submissions/NOPE/status").status_code)


# --------------------------------------------------------------------------- 重新识别
class TestReocr(_Base):
    def test_manual_backend_keeps_pending_and_warns(self):
        sid = self.sid_of()
        client = self.make_client(recognizer=ocr.Recognizer(backend="manual"))
        res = client.post("/submissions/%s/reocr" % sid, follow_redirects=True)
        self.assertEqual(200, res.status_code)
        self.assertIn("识别服务暂时无响应", res.text)
        self.assertIn("重新识别", res.text)
        data = client.get("/submissions/%s/status" % sid).json()
        self.assertEqual(4, data["pending"])
        self.assertTrue(data["empty_batch"])
        score, _full = self.score(sid)
        self.assertEqual(3, score, "识别不出来绝不许判错")

    def test_fake_ocr_fills_and_rescores(self):
        sid = self.sid_of()
        client = self.make_client(recognizer=FakeOcr(TEXTS))
        res = client.post("/submissions/%s/reocr" % sid, follow_redirects=True)
        self.assertEqual(200, res.status_code)
        self.assertIn('data-score="7"', res.text)
        data = client.get("/submissions/%s/status" % sid).json()
        self.assertEqual(0, data["pending"])
        self.assertEqual(4, data["filled"])
        self.assertEqual("fake", data["backend"])
        self.assertFalse(data["empty_batch"])
        self.assertTrue(data["done"])

    def test_reocr_is_idempotent(self):
        sid = self.sid_of()
        client = self.make_client(recognizer=FakeOcr(TEXTS))
        for _ in range(2):
            res = client.post("/submissions/%s/reocr" % sid, follow_redirects=True)
            self.assertEqual(200, res.status_code)
        self.assertEqual((7, 7), self.score(sid))
        self.assertEqual(0, client.get("/submissions/%s/status" % sid).json()["pending"])

    def test_unknown_sid_404(self):
        res = self.client.post("/submissions/NOPE/reocr", follow_redirects=True)
        self.assertEqual(404, res.status_code)
        self.assertIn("找不到这次判卷记录", res.text)


# --------------------------------------------------------------------------- 人工确认
class TestConfirm(_Base):
    def _confirm(self, sid, fields, client=None):
        client = client or self.client
        return client.post("/submissions/%s/confirm" % sid, data=fields, follow_redirects=True)

    def test_confirm_pending_as_correct_raises_score(self):
        sid = self.sid_of()
        res = self._confirm(sid, {
            "verdict_4_1": "correct", "verdict_4_2": "correct",
            "verdict_5_1": "correct", "verdict_5_2": "correct",
        })
        self.assertEqual(200, res.status_code)
        self.assertIn('data-score="7"', res.text)
        self.assertIn("已保存", res.text, "改判后要有回执提示")
        self.assertEqual((7, 7), self.score(sid))
        detail = self.detail(sid)
        confirmed = {(u["idx"], u["blank"]) for u in detail["items"] if u.get("confirmed")}
        self.assertEqual({(4, 1), (4, 2), (5, 1), (5, 2)}, confirmed,
                         "改判过的 4 个空要标「已确认」（选择题没改就不标）")
        self.assertEqual(0, self.client.get("/submissions/%s/status" % sid).json()["pending"])

    def test_partial_multi_blank_keeps_other_blank(self):
        """多空题只改一空时，不能把另一空也拉下水（apply_confirm 是整题粒度）。"""
        sid = self.sid_of()
        self._confirm(sid, {"verdict_4_1": "correct", "verdict_4_2": "correct",
                            "verdict_5_1": "correct", "verdict_5_2": "correct"})
        self.assertEqual((7, 7), self.score(sid))
        res = self._confirm(sid, {"verdict_4_2": "wrong"})
        self.assertEqual(200, res.status_code)
        self.assertEqual((6, 7), self.score(sid), "4-1 仍应对，4-2 判错 → 6/7")
        by_key = {(u["idx"], u["blank"]): u for u in self.detail(sid)["items"]}
        self.assertEqual("correct", by_key[(4, 1)]["verdict"])
        self.assertEqual("wrong", by_key[(4, 2)]["verdict"])

    def test_confirm_text_and_verdict(self):
        sid = self.sid_of()
        res = self._confirm(sid, {"verdict_2_1": "wrong", "text_2_1": "B"})
        self.assertEqual(200, res.status_code)
        self.assertEqual((2, 7), self.score(sid), "第2题改判错 → 3-1=2")
        by_key = {(u["idx"], u["blank"]): u for u in self.detail(sid)["items"]}
        self.assertEqual("B", by_key[(2, 1)]["picked"])
        self.assertEqual("wrong", by_key[(2, 1)]["verdict"])

    def test_noop_confirm_does_not_touch(self):
        sid = self.sid_of()
        res = self._confirm(sid, {"verdict_1_1": "correct", "text_1_1": "A"})
        self.assertEqual(200, res.status_code)
        detail = self.detail(sid)
        self.assertFalse(any(u.get("confirmed") for u in detail["items"]),
                         "值没变就不该标记人工确认")
        self.assertEqual((3, 7), self.score(sid))

    def test_bad_verdict_value_ignored(self):
        sid = self.sid_of()
        res = self._confirm(sid, {"verdict_4_1": "banana"})
        self.assertEqual(200, res.status_code)
        self.assertEqual((3, 7), self.score(sid))

    def test_unknown_sid_404(self):
        res = self.client.post("/submissions/NOPE/confirm", data={"verdict_1_1": "wrong"},
                               follow_redirects=True)
        self.assertEqual(404, res.status_code)
        self.assertIn("找不到这次判卷记录", res.text)

    def test_mastery_recorded(self):
        sid = self.sid_of()
        self._confirm(sid, {"verdict_4_1": "correct", "verdict_4_2": "correct"})
        conn = dbmod.connect(self.db_path)
        try:
            rows = conn.execute("SELECT key, attempts FROM mastery").fetchall()
        finally:
            conn.close()
        self.assertTrue(rows, "判对/判错要喂给掌握度（错题训练要用）")
        self.assertIn("00004", [r["key"] for r in rows])


# --------------------------------------------------------------------------- t30 批注图
class TestAnnotated(_Base):
    """GET /submissions/{sid}/annotated.png：缓存命中直接发文件，改判后重画，失败 404 不 500。"""

    def _path(self, sid):
        return os.path.join(self.out_dir, PID, "sub_%s" % sid, "annotated.png")

    def test_returns_png_and_lands_in_out_dir(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s/annotated.png" % sid)
        self.assertEqual(200, res.status_code)
        self.assertEqual("image/png", res.headers["content-type"])
        self.assertTrue(res.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(os.path.isfile(self._path(sid)),
                        "批注图要落到 app.state.out_dir 下的 out/<pid>/sub_<sid>/")

    def test_cached_on_second_hit(self):
        sid = self.sid_of()
        first = self.client.get("/submissions/%s/annotated.png" % sid)
        mtime1 = os.path.getmtime(self._path(sid))
        second = self.client.get("/submissions/%s/annotated.png" % sid)
        self.assertEqual(first.content, second.content)
        self.assertEqual(mtime1, os.path.getmtime(self._path(sid)), "第二次命中缓存，不该重画")

    def test_regenerated_after_confirm(self):
        sid = self.sid_of()
        before = self.client.get("/submissions/%s/annotated.png" % sid)
        self.client.post("/submissions/%s/confirm" % sid, data={"verdict_1_1": "wrong"},
                         follow_redirects=True)
        after = self.client.get("/submissions/%s/annotated.png" % sid)
        self.assertEqual(200, after.status_code)
        self.assertNotEqual(before.content, after.content,
                            "改判后批注图要重画（绿→红），缓存必须失效")

    def test_refresh_query_forces_redraw(self):
        sid = self.sid_of()
        self.client.get("/submissions/%s/annotated.png" % sid)
        mtime1 = os.path.getmtime(self._path(sid))
        time.sleep(0.02)
        res = self.client.get("/submissions/%s/annotated.png?refresh=1" % sid)
        self.assertEqual(200, res.status_code)
        self.assertGreaterEqual(os.path.getmtime(self._path(sid)), mtime1)

    def test_404_when_no_canonical_image(self):
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO submissions"
                "(sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("S-BADWARP", PID, "2026-10-31T15:00:00+08:00",
                 os.path.join(self.tmp, "nope.jpg"), 0, 0, 0, 0, "{}"))
            conn.commit()
        finally:
            conn.close()
        res = self.client.get("/submissions/S-BADWARP/annotated.png")
        self.assertEqual(404, res.status_code, "标注不出来给 404，不要 500")
        self.assertIn("程序内部出错了", res.text)      # errors.explain("internal_error").title
        self.assertIn("warp_failed", res.text, "原始原因要露出来，方便排查")
        self.assertIn("回判卷页", res.text)

    def test_404_unknown_sid(self):
        res = self.client.get("/submissions/NOPE/annotated.png")
        self.assertEqual(404, res.status_code)
        self.assertIn("找不到这次判卷记录", res.text)

    def test_legend_and_entry_on_result_page(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("annotated.png", html, "结果页要有「查看批注图」入口")
        self.assertIn("绿=判对", html)
        self.assertIn("红=判错", html)
        self.assertIn("橙虚线=待确认", html)
        self.assertIn("蓝=待识别", html)


# --------------------------------------------------------------------------- t30 「为什么这么判」
class TestWhyJson(_Base):
    """GET /submissions/{sid}/item/{idx}/why → grade.explain_item 的 JSON。"""

    REQUIRED = ("sid", "pid", "idx", "no", "qtype", "kind", "verdict", "picked",
                "expected", "reason", "unsure_reason", "blanks", "backend",
                "empty_batch", "score", "full")

    def _why(self, sid, idx, client=None):
        return (client or self.client).get("/submissions/%s/item/%s/why" % (sid, idx))

    def test_correct_state(self):
        sid = self.sid_of()
        res = self._why(sid, 1)
        self.assertEqual(200, res.status_code)
        data = res.json()
        for key in self.REQUIRED:
            self.assertIn(key, data, "缺字段 %s" % key)
        self.assertEqual("correct", data["verdict"])
        self.assertEqual("A", data["picked"])
        self.assertEqual("A", data["expected"])
        self.assertEqual(4, len(data["ink_ratios"]), "选择题要给 4 个选项的墨迹占比原始值")
        self.assertGreater(data["ink_ratios"]["A"], 0.5)
        self.assertEqual(1, len(data["blanks"]))
        self.assertTrue(data["reason"], "normalize 的 reason 要带出来")
        self.assertTrue(data["crop_path"])

    def test_wrong_state(self):
        sid = self.sid_of()
        self.client.post("/submissions/%s/confirm" % sid, data={"verdict_2_1": "wrong"},
                         follow_redirects=True)
        data = self._why(sid, 2).json()
        self.assertEqual("wrong", data["verdict"])
        self.assertEqual("B", data["picked"])
        self.assertEqual("B", data["expected"])
        self.assertTrue(data["reason"])

    def test_unsure_state(self):
        res = self.upload(photo=self.photo_unfilled)
        sid = res.url.path.rstrip("/").split("/")[-1]
        data = self._why(sid, 3).json()
        self.assertEqual("unsure", data["verdict"])
        self.assertEqual("", data["picked"], "没涂黑 → 识别不出选项")
        self.assertTrue(data["unsure_reason"], "unsure 必须说清「为什么待确认」")

    def test_pending_state(self):
        sid = self.sid_of()
        data = self._why(sid, 4).json()
        self.assertEqual("pending", data["verdict"])
        self.assertEqual(2, len(data["blanks"]), "填空 2 空要给逐空明细")
        self.assertEqual([1, 2], [b["blank"] for b in data["blanks"]])
        self.assertTrue(data["boxes"], "作答框坐标要带出来")

    def test_unknown_item_404(self):
        sid = self.sid_of()
        res = self._why(sid, 999)
        self.assertEqual(404, res.status_code)
        self.assertEqual("item_not_found", res.json()["error"])

    def test_unknown_sid_404(self):
        res = self._why("NOPE", 1)
        self.assertEqual(404, res.status_code)
        self.assertEqual("submission_not_found", res.json()["error"])


class TestWhyDrawer(_Base):
    """同一份证据的 HTML 版：抽屉片段 + 无 JS 时的独立页。"""

    def test_embed_fragment(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s/item/4/why?format=html&embed=1" % sid)
        self.assertEqual(200, res.status_code)
        html = res.text
        self.assertNotIn("<!doctype html", html.lower(), "抽屉片段不带整页骨架")
        for word in ("识别文本", "期望答案", "判定", "裁切"):
            self.assertIn(word, html)
        self.assertIn('name="verdict_4_1"', html, "逐空改判按钮：第 4 题空 1")
        self.assertIn('name="verdict_4_2"', html, "逐空改判按钮：第 4 题空 2")
        self.assertIn('action="confirm"', html, "表单用相对路径（页面有 <base>）")
        self.assertIn("crop/4-1.png", html, "复用已有裁切图路由")
        self.assertIn("绿=判对", html, "配色图例要和批注图同一句")
        self.assertIn("蓝=待识别", html)

    def test_standalone_page(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s/item/4/why?format=html" % sid)
        self.assertEqual(200, res.status_code)
        self.assertIn("<!doctype html", res.text.lower())
        self.assertIn("为什么这么判", res.text)

    def test_omr_shows_ink_table(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s/item/1/why?format=html&embed=1" % sid).text
        self.assertIn("墨迹占比", html)
        self.assertIn("%", html)

    def test_result_page_has_why_buttons(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("why", html)
        self.assertIn('data-why="4"', html)
        self.assertIn("为什么", html)


# --------------------------------------------------------------------------- t30 逐空改判
class TestPerBlankConfirm(_Base):
    def test_only_target_blank_changes(self):
        sid = self.sid_of()
        self.client.post("/submissions/%s/confirm" % sid, data={"verdict_5_2": "wrong"},
                         follow_redirects=True)
        by_key = {(u["idx"], u["blank"]): u for u in self.detail(sid)["items"]}
        self.assertEqual("pending", by_key[(5, 1)]["verdict"], "5-1 不该被牵连")
        self.assertEqual("wrong", by_key[(5, 2)]["verdict"])
        self.assertEqual((3, 7), self.score(sid))

        self.client.post("/submissions/%s/confirm" % sid, data={"verdict_5_1": "correct"},
                         follow_redirects=True)
        by_key = {(u["idx"], u["blank"]): u for u in self.detail(sid)["items"]}
        self.assertEqual("correct", by_key[(5, 1)]["verdict"])
        self.assertEqual("wrong", by_key[(5, 2)]["verdict"], "反过来也不能被覆盖")
        self.assertEqual((4, 7), self.score(sid))

    def test_narrow_keys_go_through_grade_apply_confirm(self):
        sid = self.sid_of()
        self.client.post("/submissions/%s/confirm" % sid, data={"verdict_4_2": "wrong"},
                         follow_redirects=True)
        detail = self.detail(sid)
        by_key = {(u["idx"], u["blank"]): u for u in detail["items"]}
        self.assertEqual("wrong", by_key[(4, 2)]["verdict"])
        self.assertEqual("pending", by_key[(4, 1)]["verdict"])
        if getattr(grade_routes, "_GRADE_NARROW_OK", False):
            self.assertIn("item_verdicts", detail,
                          "grade 支持逐空键时改判应走 apply_confirm（它会写 item_verdicts）")

    def test_fallback_apply_per_unit_still_works(self):
        """grade 老版本（无逐空键）时的兜底路径，单独测，别让它变成没人跑的死代码。"""
        sid = self.sid_of()
        conn = dbmod.connect(self.db_path)
        try:
            res = grade_routes._apply_per_unit(
                conn, sid, {(4, 2): {"verdict": "correct", "text": "1/2"}})
        finally:
            conn.close()
        self.assertIsNone(res.get("error"))
        self.assertEqual(1, res["changed"])
        by_key = {(u["idx"], u["blank"]): u for u in self.detail(sid)["items"]}
        self.assertEqual("correct", by_key[(4, 2)]["verdict"])
        self.assertEqual("1/2", by_key[(4, 2)]["picked"])
        self.assertEqual("pending", by_key[(4, 1)]["verdict"], "兜底路径也只改目标空")
        self.assertEqual((4, 7), self.score(sid))


# --------------------------------------------------------------------------- t35 抽屉内无刷新改判
class TestConfirmJson(_Base):
    """confirm 支持 fetch 的 JSON 回执（局部刷新用），同时保留普通表单的 303。"""

    def _post(self, sid, data, headers=None):
        return self.client.post("/submissions/%s/confirm?format=json" % sid,
                                data=data, headers=headers or {})

    def test_json_changes_score_and_returns_rows(self):
        sid = self.sid_of()
        res = self._post(sid, {"verdict_4_1": "correct", "verdict_4_2": "correct"})
        self.assertEqual(200, res.status_code)
        self.assertEqual("application/json", res.headers["content-type"].split(";")[0])
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(2, data["changed"])
        self.assertEqual(5, data["score"], "3 选择 + 4-1 + 4-2")
        self.assertEqual(7, data["full"])
        self.assertEqual(2, data["pending"], "5-1 / 5-2 还在待确认")
        self.assertIn("4", data["item_verdicts"], "grade 的按题聚合结论要带回来")
        units = {(u["idx"], u["blank"]): u for u in data["items"]}
        self.assertEqual("correct", units[(4, 1)]["verdict"])
        self.assertEqual("判对", units[(4, 1)]["label"])
        self.assertEqual("ok", units[(4, 1)]["tone"])
        self.assertTrue(units[(4, 1)]["confirmed"])
        self.assertEqual((5, 7), self.score(sid), "JSON 改判也必须落库")

    def test_accept_header_also_gets_json(self):
        sid = self.sid_of()
        res = self.client.post("/submissions/%s/confirm" % sid,
                               data={"verdict_4_1": "correct"},
                               headers={"Accept": "application/json"})
        self.assertEqual(200, res.status_code)
        self.assertTrue(res.json()["ok"])

    def test_plain_form_still_redirects(self):
        """无 JS 时表单照旧能用：303 回结果页。"""
        sid = self.sid_of()
        res = self.client.post("/submissions/%s/confirm" % sid,
                               data={"verdict_4_1": "correct"}, follow_redirects=False)
        self.assertEqual(303, res.status_code)

    def test_json_noop_reports_zero(self):
        sid = self.sid_of()
        data = self._post(sid, {"verdict_1_1": "correct", "text_1_1": "A"}).json()
        self.assertEqual(0, data["changed"])
        self.assertEqual(3, data["score"])

    def test_json_unknown_sid_404(self):
        res = self.client.post("/submissions/NOPE/confirm?format=json",
                               data={"verdict_1_1": "wrong"})
        self.assertEqual(404, res.status_code)
        self.assertEqual("submission_not_found", res.json()["error"])

    def test_result_page_wires_ajax(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("confirm?format=json", html, "抽屉改判要走 fetch")
        self.assertIn('id="why-saved"', html, "抽屉里要有「已保存」反馈位")
        self.assertIn('action="confirm"', html, "无 JS 时的表单必须保留")
        self.assertIn('id="score-num"', html)
        self.assertIn('id="pending-chip"', html)


# --------------------------------------------------------------------------- t35 证据包导出
class TestExport(_Base):
    def _zip(self, sid):
        res = self.client.get("/submissions/%s/export.zip" % sid)
        self.assertEqual(200, res.status_code, res.text[:300])
        self.assertIn("zip", res.headers["content-type"])
        return res, zipfile.ZipFile(io.BytesIO(res.content))

    def test_zip_has_annotated_crops_and_evidence(self):
        sid = self.sid_of()
        res, zf = self._zip(sid)
        names = zf.namelist()
        self.assertIn("evidence.json", names)
        self.assertIn("annotated.png", names)
        self.assertTrue([n for n in names if n.startswith("crops/")], names)
        for name in names:
            self.assertFalse(name.startswith("/"), "zip 条目不许带绝对路径：%s" % name)
            self.assertNotIn("..", name)
            self.assertNotIn(":", name)
            self.assertNotIn("\\", name)

        evidence = json.loads(zf.read("evidence.json").decode("utf-8"))
        self.assertEqual(PID, evidence["pid"])
        self.assertEqual(sid, evidence["sid"])
        self.assertEqual(3, evidence["score"])
        self.assertEqual(7, evidence["full"])
        self.assertTrue(evidence["items"])
        first = evidence["items"][0]
        for key in ("idx", "no", "qtype", "verdict", "picked", "expected",
                    "reason", "unsure_reason", "ink_ratios"):
            self.assertIn(key, first, "evidence.json 缺 %s" % key)
        self.assertEqual("判卷_%s_%s.zip" % (PID, sid[-6:]), evidence["file_name"])

    def test_filename_is_chinese_and_rfc5987_encoded(self):
        sid = self.sid_of()
        res = self.client.get("/submissions/%s/export.zip" % sid)
        cd = res.headers["content-disposition"]
        want = quote("判卷_%s_%s.zip" % (PID, sid[-6:]))
        self.assertIn("attachment", cd)
        self.assertIn("filename*=UTF-8''", cd)
        self.assertIn(want, cd)

    def test_export_without_annotated_does_not_500(self):
        """warp 失败（没有规范图）时：跳过批注图 + 在 notes 里说明。"""
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO submissions"
                "(sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("S-BADWARP", PID, "2026-10-31T15:00:00+08:00",
                 os.path.join(self.tmp, "nope.jpg"), 0, 0, 0, 0,
                 json.dumps({"items": [], "backend": "manual"}, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        res, zf = self._zip("S-BADWARP")
        names = zf.namelist()
        self.assertIn("evidence.json", names)
        self.assertNotIn("annotated.png", names)
        evidence = json.loads(zf.read("evidence.json").decode("utf-8"))
        self.assertTrue(evidence["notes"], "要说明为什么没带批注图")
        self.assertIn("批注图", " ".join(evidence["notes"]))

    def test_does_not_pack_files_outside_submission_dir(self):
        """detail 里的 crop_path 被改成目录外的文件 → 不许打包（防路径穿越）。"""
        sid = self.sid_of()
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("TOPSECRET")
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT detail_json FROM submissions WHERE sid=?",
                               (sid,)).fetchone()
            detail = json.loads(row["detail_json"])
            detail["items"][0]["crop_path"] = secret
            conn.execute("UPDATE submissions SET detail_json=? WHERE sid=?",
                         (json.dumps(detail, ensure_ascii=False), sid))
            conn.commit()
        finally:
            conn.close()
        _res, zf = self._zip(sid)
        for name in zf.namelist():
            self.assertNotIn("secret", name)
            self.assertNotIn("TOPSECRET", zf.read(name).decode("utf-8", "ignore"))

    def test_unknown_sid_404(self):
        res = self.client.get("/submissions/NOPE/export.zip")
        self.assertEqual(404, res.status_code)

    def test_result_page_has_export_button(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("export.zip", html)
        self.assertIn("导出证据包", html)


# --------------------------------------------------------------------------- t35 标题区分数 + 待确认跳转
class TestScoreChip(_Base):
    def test_header_shows_score_and_pending_jump(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("3 / 7 分", html, "标题区要显示 X / Y 分")
        self.assertIn("待确认 2 题", html, "第 4、5 题各有一个以上空待确认")
        self.assertIn('href="#row-4-1"', html, "点一下要能滚到第一条待确认")

    def test_chip_disappears_when_all_confirmed(self):
        sid = self.sid_of()
        self.client.post("/submissions/%s/confirm?format=json" % sid, data={
            "verdict_4_1": "correct", "verdict_4_2": "correct",
            "verdict_5_1": "correct", "verdict_5_2": "correct"})
        html = self.client.get("/submissions/%s" % sid).text
        self.assertNotIn("待确认 2 题", html)
        self.assertIn("全部确认", html)
        self.assertIn("7 / 7 分", html)


# --------------------------------------------------------------------------- P0：上传后自动跑识别
class TestAutoOcr(_Base):
    """用户实测 bug：上传后没人触发 fill_pending，页面永远「识别中」。

    修法：上传成功即起后台线程；/status 在「还有 pending 且没人跑且超时」时兜底再起一次。
    """

    def _wait_done(self, client, sid, tries=20, gap=0.1):
        data = {}
        for _ in range(tries):
            data = client.get("/submissions/%s/status" % sid).json()
            if data.get("done") or data.get("empty_batch"):
                return data
            time.sleep(gap)
        return data

    def test_auto_ocr_defaults_to_on(self):
        """不注入 auto_ocr 时缺省就是开（生产行为），别让测试装置把线上路径藏起来。"""
        app = FastAPI()
        app.include_router(grade_routes.router)
        app.state.db_path = self.db_path
        app.state.out_dir = self.out_dir
        app.state.recognizer = FakeOcr(TEXTS)
        client = TestClient(app)
        sid = self.sid_of(client)
        data = self._wait_done(client, sid)
        self.assertTrue(data.get("done"), "上传后应自动跑识别，最终 pending 归零：%r" % (data,))
        self.assertEqual(4, data.get("filled"))
        self.assertEqual((7, 7), self.score(sid))

    def test_upload_triggers_background_fill(self):
        client = self.make_client(recognizer=FakeOcr(TEXTS), auto_ocr=True)
        sid = self.sid_of(client)
        data = {}
        for _ in range(3):                      # 用户就是这么轮询的
            time.sleep(0.15)
            data = client.get("/submissions/%s/status" % sid).json()
            if data.get("done"):
                break
        self.assertTrue(data.get("done"), "轮询 3 次内应自动识别完：%r" % (data,))
        self.assertEqual(0, data["pending"])
        self.assertEqual(4, data["filled"])
        self.assertEqual("fake", data["backend"])
        self.assertEqual((7, 7), self.score(sid), "识别结果要落库并重算分数")

    def test_status_does_not_retrigger_within_window(self):
        client = self.make_client(recognizer=FakeOcr(TEXTS), auto_ocr=True)
        sid = self.sid_of(client)
        for _ in range(3):
            client.get("/submissions/%s/status" % sid)
        self.assertEqual(1, int(getattr(client.app.state, "ocr_starts", 0)),
                         "20 秒内不许重复起 job（会把云端额度烧光）")

    def test_status_retries_after_window(self):
        """job 丢了（进程重启/线程挂了）→ 超过阈值后 /status 要能自愈。"""
        client = self.make_client(recognizer=FakeOcr(TEXTS), auto_ocr=True, retry_after=0)
        sid = self.sid_of(client)
        self._wait_done(client, sid)
        # 把 4-1 改回 pending，并模拟「没有人在跑」
        client.post("/submissions/%s/confirm?format=json" % sid,
                    data={"verdict_4_1": "pending"})
        client.app.state.ocr_jobs.clear()
        before = int(client.app.state.ocr_starts)
        data = client.get("/submissions/%s/status" % sid).json()
        self.assertGreaterEqual(data["pending"], 1)
        self.assertEqual(before + 1, int(client.app.state.ocr_starts),
                         "超过阈值且没有 job 在跑 → 兜底再起一次")
        self.assertTrue(data.get("started"), "回执要告诉前端这次触发了重试")

    def test_manual_backend_does_not_retry_forever(self):
        """manual 后端 + 整批空 = 再试也没用：不许每 20 秒烧一次。"""
        client = self.make_client(recognizer=ocr.Recognizer(backend="manual"),
                                  auto_ocr=True, retry_after=0)
        sid = self.sid_of(client)
        data = self._wait_done(client, sid)
        self.assertTrue(data.get("empty_batch"))
        starts = int(client.app.state.ocr_starts)
        for _ in range(3):
            client.get("/submissions/%s/status" % sid)
        self.assertEqual(starts, int(client.app.state.ocr_starts),
                         "manual 后端整批空之后不该再自动重试")

    def test_empty_batch_page_is_honest(self):
        """识别没成功时页面不许继续写「识别中（约 30 秒）」。"""
        client = self.make_client(recognizer=ocr.Recognizer(backend="manual"),
                                  auto_ocr=True, retry_after=0)
        sid = self.sid_of(client)
        self._wait_done(client, sid)
        html = client.get("/submissions/%s" % sid).text
        self.assertIn("识别没成功", html)
        self.assertIn("手工判定", html)
        self.assertIn("重新识别", html)
        self.assertNotIn("识别中（约 30 秒）", html, "已经失败了就不能再假装在识别")

    def test_recognizing_page_still_says_recognizing(self):
        """真正在识别时（非空批）仍显示「识别中」，别把好状态一起改坏。"""
        sid = self.sid_of()          # 默认 client：auto_ocr=False → 同步阶段，pending 且无 empty_batch
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("识别中（约 30 秒）", html)
        self.assertNotIn("识别没成功", html)

    def test_thread_exception_is_contained(self):
        """后台线程抛异常：不冒泡、不卡 job 标记、/status 仍 200。"""
        client = self.make_client(recognizer=FakeOcr(TEXTS), auto_ocr=True)
        sid = self.sid_of(client)
        with mock.patch.object(grade_routes.grade, "fill_pending",
                               side_effect=RuntimeError("boom")):
            grade_routes._run_fill(sid, self.db_path, None, client.app)   # 不许抛
        self.assertNotIn(sid, client.app.state.ocr_jobs, "job 标记必须清掉，否则再也不会重试")
        res = client.get("/submissions/%s/status" % sid)
        self.assertEqual(200, res.status_code)
        self.assertIn("pending", res.json())

    def test_job_cleared_after_run(self):
        client = self.make_client(recognizer=FakeOcr(TEXTS), auto_ocr=True)
        sid = self.sid_of(client)
        self._wait_done(client, sid)
        self.assertNotIn(sid, client.app.state.ocr_jobs, "跑完要释放，才能被兜底重试")


# --------------------------------------------------------------------------- t42 判卷记录列表
class TestRecords(_Base):
    """用户需求：「我需要能查看判过的卷子」。"""

    PID2 = "P20261031-932"

    def _records(self, client=None, **params):
        client = client or self.client
        return client.get("/records", params=params)

    def test_lists_submission_with_score_and_actions(self):
        sid = self.sid_of()
        res = self._records()
        self.assertEqual(200, res.status_code)
        html = res.text
        self.assertIn(sid, html, "列表里要有这次判卷")
        self.assertIn("3 / 7", html, "得分要显示 X / Y")
        self.assertIn(self.manifest["title"], html, "要显示卷子标题")
        self.assertIn(PID, html)
        for word in ("查看结果", "导出证据包", "删除"):
            self.assertIn(word, html)
        self.assertIn('href="/submissions/%s"' % sid, html)
        self.assertIn('href="/submissions/%s/export.zip"' % sid, html)
        self.assertIn('action="/records/%s/delete"' % sid, html)
        self.assertIn("判卷记录", html)

    def test_filter_by_pid(self):
        sid = self.sid_of()
        self.insert_fake("S20261031-180000-abc123", self.PID2, score=1, full=2)
        only_mine = self._records(pid=PID).text
        self.assertIn(sid, only_mine)
        self.assertNotIn("S20261031-180000-abc123", only_mine, "?pid= 只回该卷")
        only_other = self._records(pid=self.PID2).text
        self.assertIn("S20261031-180000-abc123", only_other)
        self.assertNotIn(sid, only_other)

    def test_only_pending_filter(self):
        sid = self.sid_of()                       # 4 个空待确认
        done_sid = "S20261031-181000-def456"
        self.insert_fake(done_sid, PID, score=1, full=1)   # 全对、无待确认
        pending = self._records(only="pending").text
        self.assertIn(sid, pending)
        self.assertNotIn(done_sid, pending, "?only=pending 只回有待确认的")
        everything = self._records().text
        self.assertIn(done_sid, everything, "不过滤时两者都在")

    def test_q_filter(self):
        sid = self.sid_of()
        self.insert_fake("S20261031-182000-fff999", self.PID2, score=1, full=1)
        hit = self._records(q=sid[-6:]).text
        self.assertIn(sid, hit)
        self.assertNotIn("S20261031-182000-fff999", hit)
        by_title = self._records(q=self.manifest["title"]).text
        self.assertIn(sid, by_title)

    def test_delete_removes_row_and_dir(self):
        sid = self.sid_of()
        sub_dir = os.path.join(self.out_dir, PID, "sub_%s" % sid)
        self.assertTrue(os.path.isdir(sub_dir))
        res = self.client.post("/records/%s/delete" % sid, follow_redirects=False)
        self.assertEqual(302, res.status_code)
        self.assertIn("/records", res.headers["location"])
        self.assertFalse(os.path.isdir(sub_dir), "目录要一起删掉")
        conn = dbmod.connect(self.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) c FROM submissions WHERE sid=?", (sid,)).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(0, n, "submissions 行要删掉")
        self.assertNotIn(sid, self._records().text)

    def test_delete_illegal_sid_400_and_files_untouched(self):
        sid = self.sid_of()
        sub_dir = os.path.join(self.out_dir, PID, "sub_%s" % sid)
        for bad in ("S-BAD", "..%2F..%2Fetc", "S20261031-000000-XYZ"):
            res = self.client.post("/records/%s/delete" % bad, follow_redirects=False)
            self.assertIn(res.status_code, (400, 404), "非法 sid 要挡：%s" % bad)
        self.assertTrue(os.path.isdir(sub_dir), "非法请求不许动文件")
        self.assertIn(sid, self._records().text)

    def test_delete_illegal_pid_400(self):
        sid = "S20261031-183000-evil01"
        self.insert_fake(sid, "../../etc", score=0, full=1)
        res = self.client.post("/records/%s/delete" % sid, follow_redirects=False)
        self.assertEqual(400, res.status_code)
        conn = dbmod.connect(self.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) c FROM submissions WHERE sid=?", (sid,)).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(1, n, "pid 非法时整条记录都不许删")

    def test_empty_state(self):
        conn = dbmod.connect(self.db_path)
        try:
            conn.execute("DELETE FROM submissions")
            conn.commit()
        finally:
            conn.close()
        html = self._records().text
        self.assertIn("还没有判过卷子", html)
        self.assertIn("/submissions", html, "空态要给去判卷的入口")

    def test_mobile_card_markup(self):
        sid = self.sid_of()
        html = self._records().text
        self.assertIn('data-layout="cards-mobile"', html)
        self.assertIn('class="records-table"', html)
        self.assertIn("rec-card", html, "每行要有卡片类名，窄屏才切得过去")
        self.assertIn("rec-score", html, "得分要单独一个块，窄屏放成大字号")
        self.assertIn("@media (max-width: 720px)", html)
        self.assertIn("rec-actions", html)
        self.assertIn(sid, html)

    def test_result_page_has_back_link(self):
        sid = self.sid_of()
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("返回判卷记录", html)
        self.assertIn('href="/records"', html)

    def test_nav_has_records_entry(self):
        for url in ("/submissions", "/records"):
            html = self.client.get(url).text
            self.assertIn('href="/records"', html, "%s 的导航要有判卷记录" % url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
