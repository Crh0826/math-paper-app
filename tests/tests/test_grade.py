# -*- coding: utf-8 -*-
"""T8 · 判卷流水线（grade.py）测试。

不依赖相机：用 card.build_card 生成真答题卡 → 把「学生答案」画上去（涂黑 OMR 框 /
在填空框里写字）→ 合成手机拍照效果（透视 3% + 光照梯度 + 噪声 + 模糊 + JPEG70）→ 判卷。

断言：
  1. 10 张模拟照里标定成功 ≥9，且标定成功的那些选择题 100% 判对；
  2. 缺角/读不出图 → {"error":"warp_failed"}，绝不抛异常、不落库；
  3. QR 读到别的 pid → 串卷报错；QR 被遮 → qr_ok=false 仍按传入 pid 判；
  4. 同步阶段填空/解答 = pending + crop 落盘；fill_pending(recognizer) 回填后重算分数；
  5. 识别不出来（空文本）→ unsure（进人工确认），绝不判错。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import card  # noqa: E402
import db  # noqa: E402
import grade  # noqa: E402

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID = "P20261031-901"
PID2 = "P20261031-902"

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

CHOICES = {1: "A", 2: "B", 3: "C"}          # 全对
TEXTS = {"4-1": "4", "4-2": "1/2", "5-1": "9", "5-2": "3"}


def make_manifest(pid=PID):
    return {
        "pid": pid,
        "created_at": "2026-10-31T15:00:00+08:00",
        "title": "七年级数学 · 判卷测试卷",
        "params": {},
        "page": {"w_pt": 595.3, "h_pt": 841.9, "dpi": 300,
                 "w_px": card.PAGE_W, "h_px": card.PAGE_H},
        "items": [dict(it) for it in ITEMS],
    }


def _font(size=56):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size, index=0)
    return ImageFont.load_default()


class Fixture:
    """一套卡片 + 合成照片的公共装置。"""

    def __init__(self, tmp, choices=None, texts=None, pid=PID):
        self.tmp = tmp
        self.card_dir = os.path.join(tmp, "card")
        self.manifest = make_manifest(pid)
        card.build_card(self.manifest, self.card_dir)
        self.card_png = os.path.join(self.card_dir, "card_1.png")
        self.choices = choices if choices is not None else dict(CHOICES)
        self.texts = texts if texts is not None else dict(TEXTS)
        self.filled_png = os.path.join(tmp, "filled.png")
        self._fill()
        self.db_path = os.path.join(tmp, "grade.db")
        self.conn = db.connect(self.db_path)
        self._insert_paper(pid, self.manifest)

    def _insert_paper(self, pid, manifest):
        self.conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest, n_items) "
            "VALUES (?,?,?,?,?,?)",
            (pid, manifest["created_at"], manifest["title"], "{}",
             json.dumps(manifest, ensure_ascii=False), len(manifest["items"])))
        self.conn.commit()

    def _fill(self):
        img = Image.open(self.card_png).convert("L")
        d = ImageDraw.Draw(img)
        pages = self.manifest["card"]["pages"]
        for page in pages:
            for it in page["items"]:
                if it["kind"] == card.KIND_OMR:
                    want = self.choices.get(it["idx"])
                    for c in it["cells"]:
                        if str(c["opt"]) == str(want):
                            d.rectangle([c["x"] + 8, c["y"] + 8,
                                         c["x"] + c["w"] - 8, c["y"] + c["h"] - 8], fill=0)
                elif it["kind"] == card.KIND_FILL:
                    for b in it["boxes"]:
                        t = self.texts.get("%d-%d" % (it["idx"], b["blank"]))
                        if t:
                            d.text((b["x"] + 24, b["y"] + 24), t, font=_font(), fill=0)
                else:
                    for b in it["answer_boxes"]:
                        t = self.texts.get("%d-%d" % (it["idx"], b["blank"]))
                        if t:
                            d.text((b["x"] + 24, b["y"] + 24), t, font=_font(), fill=0)
        img.save(self.filled_png)

    # ---- 合成“拍照”
    def photo(self, seed=1, jitter=0.03, quality=70, w_scale=0.72):
        img = cv2.imread(self.filled_png, cv2.IMREAD_GRAYSCALE)
        h, w = img.shape
        rng = np.random.RandomState(seed)
        W, H = int(w * w_scale), int(h * w_scale)
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        d = jitter
        dst = np.float32([
            [W * rng.uniform(d * 0.3, d), H * rng.uniform(d * 0.3, d)],
            [W * (1 - rng.uniform(d * 0.3, d)), H * rng.uniform(d * 0.4, d * 1.2)],
            [W * (1 - rng.uniform(d * 0.3, d)), H * (1 - rng.uniform(d * 0.3, d))],
            [W * rng.uniform(d * 0.3, d), H * (1 - rng.uniform(d * 0.3, d))],
        ])
        M = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(img, M, (W, H), borderValue=170)
        yy, xx = np.mgrid[0:H, 0:W]
        grad = 0.72 + 0.45 * (xx / max(W - 1, 1)) * 0.5 + 0.35 * (yy / max(H - 1, 1)) * 0.5
        out = np.clip(warped.astype(np.float32) * grad + rng.normal(0, 5, (H, W)), 0, 255)
        out = cv2.GaussianBlur(out.astype(np.uint8), (3, 3), 0)
        path = os.path.join(self.tmp, "photo_%s.jpg" % seed)
        cv2.imwrite(path, out, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return path

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class FakeOcr:
    """假识别器：按 idx-blank 直接给文本（不碰网络）。"""

    def __init__(self, mapping, backend="fake", conf=None):
        self.mapping = dict(mapping)
        self.backend_name = backend
        self.conf = conf          # None = 沿用旧行为（有文本 0.9 / 无文本 0.0）
        self.last_error = ""

    def _one(self, key):
        text = self.mapping.get(key, "")
        if self.conf is None:
            conf = 0.9 if text else 0.0
        else:
            conf = self.conf if text else 0.0
        return {"text": text, "confidence": conf, "backend": self.backend_name}

    def read(self, path, kind=None):
        base = os.path.splitext(os.path.basename(path))[0]
        return self._one(base)

    def read_batch(self, items):
        return {str(k): self._one(str(k)) for k, _p in items}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="grade_")
        cls.fx = Fixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestCalibrationAndChoice(_Base):
    def test_ten_photos(self):
        ok, total = 0, 10
        bad_choice = []
        for seed in range(1, total + 1):
            photo = self.fx.photo(seed=seed)
            res = grade.grade_image(self.fx.conn, PID, photo,
                                    out_dir=os.path.join(self.tmp, "out"))
            if res.get("warp_ok"):
                ok += 1
                for it in res["items"]:
                    if it["kind"] == card.KIND_OMR:
                        if it["verdict"] != "correct" or it["picked"] != it["expected"]:
                            bad_choice.append((seed, it["idx"], it["verdict"], it["picked"]))
        self.assertGreaterEqual(ok, 9, "10 张模拟照标定应 ≥9，实际 %d" % ok)
        self.assertEqual([], bad_choice, "标定成功时选择题必须全对，错例: %s" % bad_choice)

    def test_returns_contract_fields(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=1),
                                out_dir=os.path.join(self.tmp, "out"))
        for k in ("sid", "pid", "warp_ok", "qr_ok", "score", "full", "items"):
            self.assertIn(k, res)
        self.assertTrue(res["warp_ok"])
        self.assertTrue(res["qr_ok"])
        self.assertEqual(7, res["full"], "3 选择 + 2 填空空 + 2 解答空 = 7 个判分单元")
        self.assertEqual(3, res["score"], "同步阶段只有选择题算分")
        self.assertEqual(PID, res["pid"])

    def test_submission_row_written(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=2),
                                out_dir=os.path.join(self.tmp, "out"))
        row = self.fx.conn.execute("SELECT * FROM submissions WHERE sid=?", (res["sid"],)).fetchone()
        self.assertIsNotNone(row, "submissions 必须落库")
        self.assertEqual(PID, row["pid"])
        self.assertEqual(1, row["warp_ok"])
        self.assertEqual(1, row["qr_ok"])
        detail = json.loads(row["detail_json"])
        self.assertEqual(res["score"], row["score"])
        self.assertEqual(res["full"], row["full"])
        self.assertTrue(detail["items"])


class TestFailures(_Base):
    def test_missing_corner(self):
        page = cv2.imread(self.fx.filled_png, cv2.IMREAD_GRAYSCALE)
        x, y = card.ARUCO_POS[2]
        page[y - 10:y + card.ARUCO_SIZE + 10, x - 10:x + card.ARUCO_SIZE + 10] = 255
        broken = os.path.join(self.tmp, "broken.png")
        cv2.imwrite(broken, page)
        before = self.fx.conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
        res = grade.grade_image(self.fx.conn, PID, broken, out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual("warp_failed", res.get("error"))
        self.assertIn("detail", res)
        self.assertFalse(res["warp_ok"])
        after = self.fx.conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
        self.assertEqual(before, after, "标定失败不该落库")

    def test_unreadable_image(self):
        bogus = os.path.join(self.tmp, "not_an_image.txt")
        with open(bogus, "w", encoding="utf-8") as f:
            f.write("hello")
        res = grade.grade_image(self.fx.conn, PID, bogus, out_dir=os.path.join(self.tmp, "out"))
        self.assertIn("error", res)
        self.assertFalse(res["warp_ok"])

    def test_missing_paper(self):
        """卷号不在库 → 自动转「按题目标识判卷」；库中也没有这些题号时明确报错，不静默空判。"""
        res = grade.grade_image(self.fx.conn, "P-NOT-EXIST", self.fx.photo(seed=3),
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertIn(res.get("error"), ("unknown_questions", "no_question_ids", "legacy_card"),
                      "不该静默成功：%r" % (res,))
        self.assertIn("题库", res.get("detail") or "")

    def test_pid_mismatch_detected(self):
        fx2 = Fixture(os.path.join(self.tmp, "mismatch"), pid=PID2)
        try:
            # PID2 的卷里塞 PID 的卡：QR 读到 PID，与传入 PID2 不一致 → 串卷
            fx2._insert_paper(PID2, self.fx.manifest)
            res = grade.grade_image(fx2.conn, PID2, self.fx.photo(seed=4),
                                    out_dir=os.path.join(self.tmp, "out"))
            self.assertEqual("pid_mismatch", res.get("error"), res)
        finally:
            fx2.close()

    def test_qr_covered_still_grades(self):
        page = cv2.imread(self.fx.filled_png, cv2.IMREAD_GRAYSCALE)
        q = self.fx.manifest["card"]["pages"][0]["qr"]
        page[q["y"] - 5:q["y"] + q["size"] + 5, q["x"] - 5:q["x"] + q["size"] + 5] = 255
        no_qr = os.path.join(self.tmp, "no_qr.png")
        cv2.imwrite(no_qr, page)
        res = grade.grade_image(self.fx.conn, PID, no_qr, out_dir=os.path.join(self.tmp, "out"))
        self.assertTrue(res.get("warp_ok"), res)
        self.assertFalse(res["qr_ok"], "QR 被遮必须标 qr_ok=False")
        self.assertEqual(3, res["score"], "QR 失效仍应按传入 pid 判选择")


class TestTwoPhase(_Base):
    def test_pending_and_crops(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=5),
                                out_dir=os.path.join(self.tmp, "out"))
        pend = [it for it in res["items"] if it["verdict"] == "pending"]
        self.assertEqual(4, len(pend), "2 填空空 + 2 解答空 = 4 个待识别")
        for it in pend:
            self.assertTrue(it.get("crop_path"), "pending 必须给 crop_path")
            self.assertTrue(os.path.exists(it["crop_path"]), it["crop_path"])
        self.assertEqual(0, sum(1 for it in res["items"] if it["verdict"] == "wrong"))

    def test_fill_pending_all_correct(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=6),
                                out_dir=os.path.join(self.tmp, "out"))
        out = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(TEXTS))
        self.assertEqual(4, out["filled"])
        self.assertEqual(4, out["correct"])
        self.assertEqual(0, out["wrong"])
        self.assertEqual(0, out["left_manual"])
        row = self.fx.conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                                   (res["sid"],)).fetchone()
        self.assertEqual(7, row["score"], "3 选择 + 4 填空/解答 全对 = 7")
        detail = json.loads(row["detail_json"])
        self.assertEqual(0, sum(1 for it in detail["items"] if it["verdict"] == "pending"))
        self.assertEqual("fake", detail.get("backend"))

    def test_fill_pending_partial_wrong(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=7),
                                out_dir=os.path.join(self.tmp, "out"))
        bad = dict(TEXTS)
        bad["4-2"] = "1/3"
        bad["5-1"] = "8"
        out = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(bad))
        self.assertEqual(2, out["correct"])
        self.assertEqual(2, out["wrong"])
        self.assertEqual(5, out["score"], "3 选择 + 4-1 + 5-2 = 5 对")

    def test_fill_pending_empty_stays_manual(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=8),
                                out_dir=os.path.join(self.tmp, "out"))
        out = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr({}))
        self.assertEqual(0, out["correct"])
        self.assertEqual(0, out["wrong"], "识别不出来绝不能判错")
        self.assertEqual(4, out["left_manual"])
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (res["sid"],)).fetchone()[0])
        pend = [it for it in detail["items"] if it["verdict"] == "pending"]
        self.assertEqual(4, len(pend), "空识别应留在 pending 待人工确认")

    def test_fill_pending_unknown_sid(self):
        out = grade.fill_pending(self.fx.conn, "S-NOT-EXIST", FakeOcr({}))
        self.assertIn("error", out)


class TestOmrRobustness(_Base):
    def test_blank_choice_is_unsure(self):
        fx = Fixture(os.path.join(self.tmp, "blank"), choices={})
        try:
            res = grade.grade_image(fx.conn, PID, fx.photo(seed=9),
                                    out_dir=os.path.join(self.tmp, "out"))
            omr = [it for it in res["items"] if it["kind"] == card.KIND_OMR]
            self.assertEqual(3, len(omr))
            for it in omr:
                self.assertEqual("unsure", it["verdict"], "没涂黑必须 unsure，不能猜")
        finally:
            fx.close()

    def test_two_marks_is_unsure(self):
        fx = Fixture(os.path.join(self.tmp, "two"))
        try:
            page = Image.open(fx.filled_png).convert("L")
            d = ImageDraw.Draw(page)
            # 第 1 题再涂黑 B（原本 A），两个框都满墨 → 歧义
            for it in fx.manifest["card"]["pages"][0]["items"]:
                if it.get("kind") == card.KIND_OMR and it["idx"] == 1:
                    for c in it["cells"]:
                        if str(c["opt"]) in ("A", "B"):
                            d.rectangle([c["x"] + 8, c["y"] + 8,
                                         c["x"] + c["w"] - 8, c["y"] + c["h"] - 8], fill=0)
            page.save(os.path.join(fx.tmp, "two.png"))
            cv2.imwrite(os.path.join(fx.tmp, "filled.png"), np.array(page))
            res = grade.grade_image(fx.conn, PID, fx.photo(seed=10),
                                    out_dir=os.path.join(self.tmp, "out"))
            it1 = [it for it in res["items"] if it["idx"] == 1][0]
            self.assertEqual("unsure", it1["verdict"], "两框同墨应 unsure")
        finally:
            fx.close()


class TestUnsureHeuristics(unittest.TestCase):
    """t17：四条可解释 unsure 规则，只把 wrong 往 unsure 抬，判对的绝不动。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="unsure_")
        cls.fx = Fixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, texts, conf=None, seed=21):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=seed),
                                out_dir=os.path.join(self.tmp, "out"))
        grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(texts, conf=conf))
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (res["sid"],)).fetchone()[0])
        return {(u["idx"], u["blank"]): u for u in detail["items"]}

    def test_non_math_chars_become_unsure(self):
        """含多余汉字/乱码 → unsure，并写清楚为什么。"""
        u = self._run(dict(TEXTS, **{"4-2": "1/2 这道题"}))
        self.assertEqual("unsure", u[(4, 2)]["verdict"], u[(4, 2)])
        reason = u[(4, 2)].get("unsure_reason") or ""
        self.assertTrue(reason, "必须给 unsure_reason 供 UI 显示")
        self.assertIn("非数学字符", reason)

    def test_allowlisted_chars_are_fine(self):
        """或/和/度/元/角/分/秒/±/√/°/（） 不算乱码，不能被 unsure 掉。"""
        u = self._run(dict(TEXTS, **{"5-1": "9或8"}))
        self.assertNotEqual("unsure", u[(5, 1)]["verdict"], "合法字符不该进人工队列")
        self.assertFalse(u[(5, 1)].get("unsure_reason"))

    def test_length_ratio_become_unsure(self):
        """长度 ≥3 倍或 ≤1/3（期望≥2 字）→ 疑似识别串行 → unsure。"""
        u = self._run(dict(TEXTS, **{"4-2": "1/2 1/2 1/2"}))
        # 3-2 期望 "1/2"（3 字），学生 11 字 ≥ 9 → unsure
        self.assertEqual("unsure", u[(4, 2)]["verdict"], u[(4, 2)])
        self.assertIn("长度", u[(4, 2)].get("unsure_reason") or "")

    def test_short_expected_skips_length_rule(self):
        """期望只有 1 个字时长度规则不启用（避免误伤）。"""
        u = self._run(dict(TEXTS, **{"5-1": "9" * 6}))
        self.assertEqual("wrong", u[(5, 1)]["verdict"], "期望 1 字不该判 unsure")

    def test_low_confidence_become_unsure(self):
        """后端真报 confidence 且 <0.5 → unsure；等后端有置信度就用上。"""
        u = self._run(dict(TEXTS, **{"4-2": "1/3"}), conf=0.2)
        self.assertEqual("unsure", u[(4, 2)]["verdict"], u[(4, 2)])
        self.assertIn("置信度", u[(4, 2)].get("unsure_reason") or "")

    def test_correct_verdict_never_downgraded(self):
        """判对的绝不能被 unsure 规则动：写法不同的正确答案仍是 correct。"""
        u = self._run(dict(TEXTS, **{"4-1": "x=4"}))
        self.assertEqual("correct", u[(4, 1)]["verdict"], "x=4 与 4 等价，不能被降级")
        self.assertFalse(u[(4, 1)].get("unsure_reason"))

    def test_high_confidence_keeps_wrong(self):
        """置信度足够就是普通判错，不要淹人工队列。"""
        u = self._run(dict(TEXTS, **{"4-2": "1/3"}), conf=0.95)
        self.assertEqual("wrong", u[(4, 2)]["verdict"])
        self.assertFalse(u[(4, 2)].get("unsure_reason"))


class TestEmptyBatchAndIdempotency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="empty_")
        cls.fx = Fixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_empty_batch_keeps_pending(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=31),
                                out_dir=os.path.join(self.tmp, "out"))
        out = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr({}))
        self.assertTrue(out["empty_batch"], "整批空结果必须标 empty_batch")
        self.assertEqual(0, out["wrong"], "空批次绝不能判错")
        self.assertEqual(4, out["left_manual"])
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (res["sid"],)).fetchone()[0])
        self.assertEqual(4, sum(1 for u in detail["items"] if u["verdict"] == "pending"))

    def test_rerun_is_idempotent(self):
        """「重新识别」可重复点：已判完的不再动，空批次重复调用结果一致。"""
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=32),
                                out_dir=os.path.join(self.tmp, "out"))
        grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(TEXTS))
        row1 = self.fx.conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                                    (res["sid"],)).fetchone()
        out2 = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr({}))
        row2 = self.fx.conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                                    (res["sid"],)).fetchone()
        self.assertEqual(row1["score"], row2["score"])
        self.assertEqual(row1["full"], row2["full"])
        self.assertEqual(0, out2["filled"], "没有 pending 了就不该再识别")
        self.assertEqual(json.loads(row1["detail_json"])["items"],
                         json.loads(row2["detail_json"])["items"])

    def test_not_empty_batch_flag(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=33),
                                out_dir=os.path.join(self.tmp, "out"))
        out = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(TEXTS))
        self.assertFalse(out["empty_batch"])


class TestApplyConfirm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="confirm_")
        cls.fx = Fixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fresh(self, seed):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=seed),
                                out_dir=os.path.join(self.tmp, "out"))
        return res["sid"]

    def test_confirm_marks_correct_and_recomputes(self):
        sid = self._fresh(41)
        out = grade.apply_confirm(self.fx.conn, sid, {
            4: {"verdict": "correct", "text": "4"},     # 填空 2 空 → 都判对
            5: {"verdict": "wrong", "text": "8"},       # 解答 2 空 → 都判错
        })
        self.assertEqual(5, out["score"], "3 选择 + 填空 2 空 = 5")
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()[0])
        by = {(u["idx"], u["blank"]): u for u in detail["items"]}
        self.assertEqual("correct", by[(4, 1)]["verdict"])
        self.assertEqual("correct", by[(4, 2)]["verdict"])
        self.assertEqual("wrong", by[(5, 1)]["verdict"])
        self.assertEqual("4", by[(4, 1)]["picked"])

    def test_confirm_is_idempotent(self):
        sid = self._fresh(42)
        corr = {4: {"verdict": "correct", "text": "4"}}
        a = grade.apply_confirm(self.fx.conn, sid, corr)
        row1 = self.fx.conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                                    (sid,)).fetchone()
        b = grade.apply_confirm(self.fx.conn, sid, corr)
        row2 = self.fx.conn.execute("SELECT score, full, detail_json FROM submissions WHERE sid=?",
                                    (sid,)).fetchone()
        self.assertEqual(row1["score"], row2["score"])
        self.assertEqual(a["score"], b["score"])
        self.assertGreaterEqual(a["recorded"], 1, "第一次确认应记账")
        self.assertEqual(0, b["recorded"], "重复确认不该再记账（幂等）")

    def test_confirm_can_flip_verdict(self):
        sid = self._fresh(43)
        grade.apply_confirm(self.fx.conn, sid, {4: {"verdict": "correct", "text": "4"}})
        s1 = self.fx.conn.execute("SELECT score FROM submissions WHERE sid=?", (sid,)).fetchone()[0]
        grade.apply_confirm(self.fx.conn, sid, {4: {"verdict": "wrong", "text": "4"}})
        s2 = self.fx.conn.execute("SELECT score FROM submissions WHERE sid=?", (sid,)).fetchone()[0]
        self.assertEqual(s1 - 2, s2, "改判后分数要跟着变（填空 2 空）")

    def test_confirm_single_blank_by_string_key(self):
        """逐空改判："idx-blank" 键只动该空，别把同题另一空也覆盖。"""
        sid = self._fresh(44)
        out = grade.apply_confirm(self.fx.conn, sid, {"4-1": {"verdict": "correct", "text": "4"}})
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()[0])
        by = {(u["idx"], u["blank"]): u for u in detail["items"]}
        self.assertEqual("correct", by[(4, 1)]["verdict"])
        self.assertEqual("pending", by[(4, 2)]["verdict"], "另一空不该被连带改判")
        self.assertEqual(4, out["score"], "3 选择 + 1 个空 = 4")
        self.assertEqual(7, out["full"])

    def test_confirm_single_blank_by_tuple_key(self):
        sid = self._fresh(45)
        grade.apply_confirm(self.fx.conn, sid, {(4, 2): {"verdict": "correct", "text": "1/2"}})
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()[0])
        by = {(u["idx"], u["blank"]): u for u in detail["items"]}
        self.assertEqual("correct", by[(4, 2)]["verdict"])
        self.assertEqual("pending", by[(4, 1)]["verdict"])
        self.assertEqual("1/2", by[(4, 2)]["picked"])

    def test_confirm_mixed_wide_then_narrow(self):
        """整题 + 逐空混用：整题先落，逐空覆盖指定格；聚合判定一空错算整题错。"""
        sid = self._fresh(46)
        out = grade.apply_confirm(self.fx.conn, sid, {
            4: {"verdict": "correct", "text": "4"},
            "4-2": {"verdict": "wrong", "text": "1/3"},
        })
        detail = json.loads(self.fx.conn.execute(
            "SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()[0])
        by = {(u["idx"], u["blank"]): u for u in detail["items"]}
        self.assertEqual("correct", by[(4, 1)]["verdict"])
        self.assertEqual("wrong", by[(4, 2)]["verdict"])
        self.assertEqual(4, out["score"], "3 选择 + 1 个判对 = 4")
        self.assertEqual("wrong", grade.explain_item(self.fx.conn, sid, 4)["verdict"],
                         "一空错算整题错")

    def test_confirm_string_key_json_roundtrip(self):
        """corrections 要能过 JSON（路由从 JSON body 收的键只能是字符串）。"""
        sid = self._fresh(47)
        raw = {"4-1": {"verdict": "correct", "text": "4"}}
        corr = json.loads(json.dumps(raw))
        out = grade.apply_confirm(self.fx.conn, sid, corr)
        self.assertGreaterEqual(out["score"], 4)
        a = grade.apply_confirm(self.fx.conn, sid, corr)
        self.assertEqual(0, a["recorded"], "重复逐空确认应幂等")

    def test_confirm_unknown_sid(self):
        out = grade.apply_confirm(self.fx.conn, "S-NOPE", {1: {"verdict": "correct"}})
        self.assertIn("error", out)


class TestMasteryWiring(unittest.TestCase):
    """判完/确认后要喂 train.record_result；pending/unsure 不记。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mastery_")
        cls.fx = Fixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.calls = []
        self._orig = grade._record_one
        grade._record_one = lambda conn, no, correct, now=None: self.calls.append(
            (no, bool(correct)))

    def tearDown(self):
        grade._record_one = self._orig

    def test_records_after_fill(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=51),
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual([], self.calls, "同步阶段不该记账（还没判完）")
        out = grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(TEXTS))
        # 5 道题各记 1 次（同一题多个空合并成一次，按题记账）
        self.assertEqual(5, len(self.calls), "判完后每道题记账一次")
        self.assertTrue(all(c[1] for c in self.calls))
        nos = {c[0] for c in self.calls}
        self.assertEqual({"00001", "00002", "00003", "00004", "00005"}, nos)
        # 重复调用不再记账
        grade.fill_pending(self.fx.conn, res["sid"], FakeOcr(TEXTS))
        self.assertEqual(5, len(self.calls), "重复 fill_pending 不该重复记账")
        self.assertGreaterEqual(out["recorded"], 5)

    def test_unsure_not_recorded(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=52),
                                out_dir=os.path.join(self.tmp, "out"))
        grade.fill_pending(self.fx.conn, res["sid"], FakeOcr({"4-1": "4 因为", "4-2": "1/2 1/2 1/2"}))
        # 只有 3 个选择题是确定的 correct → 只记这 3 条；unsure/pending 不记
        self.assertEqual(3, len(self.calls), "unsure 不该进掌握度")
        self.assertEqual({"00001", "00002", "00003"}, {c[0] for c in self.calls})

    def test_confirm_records(self):
        res = grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=53),
                                out_dir=os.path.join(self.tmp, "out"))
        out = grade.apply_confirm(self.fx.conn, res["sid"],
                                  {4: {"verdict": "correct", "text": "4"}})
        self.assertGreaterEqual(len(self.calls), 1, "人工判对也要记账")
        self.assertIn(("00004", True), self.calls, "确认的那道题必须记账")
        self.assertGreaterEqual(out["recorded"], 1)


class TestAnnotateAndExplain(unittest.TestCase):
    """t24：判卷可核对性——标注图 + 单题证据。

    一次造出四种状态：idx1 涂错（wrong）、idx2 涂对（correct）、idx3 空白（unsure）、
    idx4 填空待识别（pending）。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="annot_")
        cls.fx = Fixture(cls.tmp, choices={1: "B", 2: "B"})   # 1 错、2 对、3 空白
        cls.res = grade.grade_image(cls.fx.conn, PID, cls.fx.photo(seed=71),
                                    out_dir=os.path.join(cls.tmp, "out"))
        cls.sid = cls.res["sid"]

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_four_states_present(self):
        by = {(u["idx"], u["blank"]): u for u in self.res["items"]}
        self.assertEqual("wrong", by[(1, 1)]["verdict"])
        self.assertEqual("correct", by[(2, 1)]["verdict"])
        self.assertEqual("unsure", by[(3, 1)]["verdict"])
        self.assertEqual("pending", by[(4, 1)]["verdict"])

    def test_annotate_png(self):
        out = os.path.join(self.tmp, "annotated.png")
        path = grade.annotate(self.fx.conn, self.sid, out_path=out)
        self.assertIsNotNone(path, getattr(grade.annotate, "last_error", ""))
        self.assertTrue(os.path.exists(path))
        with Image.open(path) as im:
            self.assertEqual((grade.PAGE_W, grade.PAGE_H), im.size, "标注图必须 2480×3508")
            arr = np.array(im.convert("RGB")).astype(np.int16)
        colored = ((np.abs(arr[:, :, 0] - arr[:, :, 1]) + np.abs(arr[:, :, 1] - arr[:, :, 2])) > 12)
        ink = (arr.mean(axis=2) < 200)
        self.assertGreater(max(float(colored.mean()), float(ink.mean())), 0.05,
                           "标注图不能是空白（彩色/墨迹像素 >5%%）：彩色 %.3f 墨迹 %.3f"
                           % (colored.mean(), ink.mean()))
        self.assertGreater(float(colored.mean()), 0.01, "必须真的画上彩色标注")

    def test_annotate_default_path(self):
        path = grade.annotate(self.fx.conn, self.sid)
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("annotated.png"))
        self.assertIn("sub_%s" % self.sid, path.replace("\\", "/"))
        try:
            os.remove(path)
        except OSError:
            pass

    def test_annotate_warp_failed(self):
        conn = self.fx.conn
        conn.execute(
            "INSERT OR REPLACE INTO submissions"
            "(sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("S-BAD-001", PID, "2026-10-31T16:00:00+08:00",
             os.path.join(self.tmp, "nope.jpg"), 0, 0, 0, 0,
             json.dumps({"items": [], "error": "warp_failed"}, ensure_ascii=False)))
        conn.commit()
        self.assertIsNone(grade.annotate(conn, "S-BAD-001",
                                         out_path=os.path.join(self.tmp, "bad.png")))
        err = getattr(grade.annotate, "last_error", "")
        self.assertTrue(err, "标定失败必须给出明确 error")
        self.assertIn("warp", err.lower())

    def test_annotate_idempotent(self):
        """结果页会反复调 annotate：同一条 submission 必须得到同一路径、同一张图。"""
        p1 = grade.annotate(self.fx.conn, self.sid, out_path=os.path.join(self.tmp, "a1.png"))
        p2 = grade.annotate(self.fx.conn, self.sid, out_path=os.path.join(self.tmp, "a1.png"))
        self.assertEqual(p1, p2)
        import hashlib
        with open(p1, "rb") as f:
            h1 = hashlib.sha256(f.read()).hexdigest()
        grade.annotate(self.fx.conn, self.sid, out_path=p1)
        with open(p1, "rb") as f:
            h2 = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h1, h2, "重复标注必须字节一致（幂等）")

    def test_annotate_unknown_sid(self):
        self.assertIsNone(grade.annotate(self.fx.conn, "S-NOT-EXIST"))
        self.assertTrue(getattr(grade.annotate, "last_error", ""))

    def test_explain_item_required_fields(self):
        need = ("sid", "pid", "idx", "no", "qtype", "kind", "verdict",
                "expected", "picked", "reason", "unsure_reason", "crop_path")
        for idx, want in ((1, "wrong"), (2, "correct"), (3, "unsure"), (4, "pending")):
            info = grade.explain_item(self.fx.conn, self.sid, idx)
            for k in need:
                self.assertIn(k, info, "explain_item(idx=%d) 缺字段 %s" % (idx, k))
            self.assertEqual(want, info["verdict"], "idx=%d verdict" % idx)

    def test_explain_item_choice_ratios(self):
        info = grade.explain_item(self.fx.conn, self.sid, 1)
        self.assertEqual("B", info["picked"])
        self.assertEqual("A", info["expected"])
        self.assertIn("ink_ratios", info)
        self.assertEqual({"A", "B", "C", "D"}, set(info["ink_ratios"]))
        for opt, val in info["ink_ratios"].items():
            self.assertIsInstance(val, float, "%s 占比必须是原始数值" % opt)
        self.assertGreater(info["ink_ratios"]["B"], info["ink_ratios"]["A"])
        self.assertTrue(info["crop_path"], "必须给裁切图路径")
        self.assertTrue(info["reason"])

    def test_explain_item_unsure_has_reason(self):
        info = grade.explain_item(self.fx.conn, self.sid, 3)
        self.assertEqual("unsure", info["verdict"])
        self.assertTrue(info["unsure_reason"], "unsure 必须给 unsure_reason（UI 要显示为什么）")
        self.assertIn("涂黑", info["unsure_reason"])

    def test_explain_item_pending_has_crop(self):
        info = grade.explain_item(self.fx.conn, self.sid, 4)
        self.assertEqual("pending", info["verdict"])
        self.assertTrue(info["crop_path"] and os.path.exists(info["crop_path"]))

    def test_explain_item_unknown(self):
        self.assertIn("error", grade.explain_item(self.fx.conn, self.sid, 999))
        self.assertIn("error", grade.explain_item(self.fx.conn, "S-NOT-EXIST", 1))


class TestRealPhoneRobustness(unittest.TestCase):
    """t27：真实手机拍照健壮性（EXIF 方向 / 12MP / WEBP+HEIC / 极端拍摄）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="phone_")
        cls.fx = Fixture(cls.tmp, choices={1: "B", 2: "B"})     # 1 错、2 对、3 空白
        cls.page = cv2.imread(cls.fx.filled_png, cv2.IMREAD_GRAYSCALE)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 工具
    def _persp(self, W=1800, H=2500, jitter=0.03, seed=1, scale=1.0):
        h, w = self.page.shape
        rng = np.random.RandomState(seed)
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        d = jitter
        dst = np.float32([
            [W * rng.uniform(d * 0.3, d), H * rng.uniform(d * 0.3, d)],
            [W * (1 - rng.uniform(d * 0.3, d)), H * rng.uniform(d * 0.4, d * 1.2)],
            [W * (1 - rng.uniform(d * 0.3, d)), H * (1 - rng.uniform(d * 0.3, d))],
            [W * rng.uniform(d * 0.3, d), H * (1 - rng.uniform(d * 0.3, d))],
        ])
        M = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(self.page, M, (W, H), borderValue=170)

    def _save(self, arr, name, fmt="JPEG", exif=None, quality=88):
        path = os.path.join(self.tmp, name)
        im = Image.fromarray(arr)
        if fmt == "JPEG":
            if exif is None:
                im.save(path, "JPEG", quality=quality)
            else:
                im.save(path, "JPEG", quality=quality, exif=exif)
        elif fmt == "WEBP":
            im.save(path, "WEBP", quality=quality)
        elif fmt == "HEIC":
            import pillow_heif
            pillow_heif.from_pillow(im.convert("RGB")).save(path, quality=quality)
        else:
            im.save(path, fmt)
        return path

    @staticmethod
    def _exif(orientation=None, make="TestMake", model="PhoneX 12", dt="2026:10:31 15:30:00"):
        ex = Image.Exif()
        if orientation is not None:
            ex[0x0112] = orientation
        ex[0x010F] = make
        ex[0x0110] = model
        ex[0x0132] = dt
        return ex

    def _grade(self, path, seed=1):
        t0 = time.time()
        res = grade.grade_image(self.fx.conn, PID, path,
                                out_dir=os.path.join(self.tmp, "out"))
        return res, time.time() - t0

    def _detail(self, sid):
        row = self.fx.conn.execute("SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()
        return json.loads(row["detail_json"]) if row else {}

    # ---- 1) EXIF 方向
    def test_exif_orientation_6(self):
        """竖拍 Orientation=6：存的是逆时针 90° 的像素，必须按 EXIF 转正再标定。"""
        arr = self._persp()
        rotated = np.ascontiguousarray(np.rot90(arr, 1))       # 像素逆时针 90°
        p = self._save(rotated, "exif6.jpg", exif=self._exif(6))
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"), res.get("detail"))
        self.assertEqual(1, res["score"], "转正后判分应与正拍一致")
        photo = self._detail(res["sid"]).get("photo") or {}
        self.assertEqual(6, photo.get("orientation"), "EXIF 方向要记进 detail")
        self.assertTrue(photo.get("transposed"), "必须真的按 EXIF 转过")

    def test_exif_orientation_8(self):
        arr = self._persp(seed=2)
        rotated = np.ascontiguousarray(np.rot90(arr, 3))       # 顺时针 90°
        p = self._save(rotated, "exif8.jpg", exif=self._exif(8))
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"), res.get("detail"))
        photo = self._detail(res["sid"]).get("photo") or {}
        self.assertEqual(8, photo.get("orientation"))
        self.assertTrue(photo.get("transposed"))

    def test_exif_meta_in_detail(self):
        arr = self._persp(seed=3)
        p = self._save(arr, "meta.jpg", exif=self._exif(1))
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"))
        photo = self._detail(res["sid"]).get("photo") or {}
        self.assertIn("PhoneX 12", str(photo.get("camera")), "手机型号要进 detail")
        self.assertTrue(str(photo.get("taken_at") or "").startswith("2026"), photo)

    # ---- 2) 12MP 大图
    def test_12mp_photo_fast_enough(self):
        arr = self._persp(W=4000, H=3000, jitter=0.02, seed=4)
        p = self._save(arr, "big12mp.jpg", quality=90)
        res, elapsed = self._grade(p)
        self.assertTrue(res.get("warp_ok"), res.get("detail"))
        self.assertLess(elapsed, 1.5, "12MP 单张端到端应 <1.5s，实测 %.2fs" % elapsed)
        photo = self._detail(res["sid"]).get("photo") or {}
        self.assertEqual([4000, 3000], list(photo.get("original_size") or []),
                         "原始尺寸要记进 detail")

    def test_huge_image_clamped(self):
        """8000px 全景图要有 max_side 保护，不许卡死/爆内存。"""
        arr = self._persp(W=6000, H=4000, jitter=0.02, seed=5)
        p = self._save(arr, "huge.jpg", quality=85)
        res, elapsed = self._grade(p)
        self.assertLess(elapsed, 3.0, "超大图读图必须有上限保护，实测 %.2fs" % elapsed)
        photo = self._detail(res["sid"]).get("photo") or {}
        self.assertTrue(photo.get("clamped"), "超 max_side 应标记 clamped")

    # ---- 3) 格式
    def test_webp(self):
        arr = self._persp(seed=6)
        p = self._save(arr, "photo.webp", fmt="WEBP")
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"), "WEBP 必须直接支持：%s" % res.get("detail"))

    def test_png(self):
        arr = self._persp(seed=7)
        p = self._save(arr, "photo.png", fmt="PNG")
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"))

    def test_heic_supported_or_clean_error(self):
        """能装 pillow-heif 就真测 HEIC；装不上必须给可读错误文案。"""
        arr = self._persp(seed=8)
        if getattr(grade, "_HEIF_OK", False):
            p = self._save(arr, "photo.heic", fmt="HEIC")
            res, _ = self._grade(p)
            self.assertTrue(res.get("warp_ok"), "装了 pillow-heif 就该支持 HEIC：%s"
                            % res.get("detail"))
        else:
            p = self._save(arr, "photo2.heic", fmt="HEIC") if _can_write_heic() else \
                os.path.join(self.tmp, "fake.heic")
            if not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(b"\x00\x00\x00\x18ftypheic")
            res, _ = self._grade(p)
            self.assertEqual("unsupported_format", res.get("error"))
            self.assertIn("HEIC", res.get("detail", ""))

    def test_heic_unsupported_message(self):
        """没有 pillow-heif 时：明确中文报错，不是底层异常。"""
        orig = getattr(grade, "_HEIF_OK", False)
        try:
            grade._HEIF_OK = False
            p = os.path.join(self.tmp, "nope.heic")
            with open(p, "wb") as f:
                f.write(b"\x00\x00\x00\x18ftypheic")
            res, _ = self._grade(p)
            self.assertEqual("unsupported_format", res.get("error"))
            self.assertIn("HEIC", res.get("detail", ""))
            self.assertIn("JPG", res.get("detail", ""), "要告诉用户改用 JPG/PNG")
        finally:
            grade._HEIF_OK = orig

    # ---- 4) 极端拍摄
    def _assert_ok_or_actionable(self, res, scene):
        if res.get("warp_ok"):
            return
        self.assertEqual("warp_failed", res.get("error"), "%s：%s" % (scene, res.get("detail")))
        detail = res.get("detail") or ""
        self.assertTrue(any(k in detail for k in ("重拍", "反光", "四角", "完整", "光线", "阴影")),
                        "%s 的 fix 文案要可操作，实际：%r" % (scene, detail))

    def test_tilt_45(self):
        arr = self._persp(seed=9)
        h, w = arr.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), 45, 1.0)
        cos, sin = abs(M[0, 0]), abs(M[0, 1])
        nW, nH = int(h * sin + w * cos), int(h * cos + w * sin)
        M[0, 2] += nW / 2 - w / 2
        M[1, 2] += nH / 2 - h / 2
        rot = cv2.warpAffine(arr, M, (nW, nH), borderValue=170)
        p = self._save(rot, "tilt45.jpg")
        res, _ = self._grade(p)
        self._assert_ok_or_actionable(res, "倾斜 45°")

    def test_upside_down(self):
        arr = self._persp(seed=10)
        p = self._save(np.ascontiguousarray(np.rot90(arr, 2)), "flip180.jpg")
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"), "上下颠倒应能靠 ArUco 编号自动纠正：%s"
                        % res.get("detail"))

    def test_mirror(self):
        """镜像（前置摄像头）：应自动翻转纠正，仍能标定并判分。"""
        arr = self._persp(seed=11)
        p = self._save(np.ascontiguousarray(arr[:, ::-1]), "mirror.jpg")
        res, _ = self._grade(p)
        self.assertTrue(res.get("warp_ok"), "镜像应自动纠正：%s" % res.get("detail"))
        self.assertEqual(1, res["score"], "镜像纠正后判分应与正拍一致")
        photo = self._detail(res["sid"]).get("photo") or {}
        self.assertTrue(photo.get("mirror_fixed"), "要在 detail 里标出做过镜像纠正")

    def test_small_occupancy(self):
        small = self._persp(W=900, H=1250, seed=12)
        canvas = np.full((2500, 1800), 200, np.uint8)
        canvas[600:600 + small.shape[0], 400:400 + small.shape[1]] = small
        p = self._save(canvas, "small.jpg")
        res, _ = self._grade(p)
        self._assert_ok_or_actionable(res, "纸张占画面 <40%")

    def test_glare_on_marker(self):
        arr = self._persp(seed=13).copy()
        h, w = arr.shape
        arr[:int(h * 0.16), :int(w * 0.22)] = 255        # 左上角标被强反光打白
        p = self._save(arr, "glare.jpg")
        res, _ = self._grade(p)
        self._assert_ok_or_actionable(res, "角标强反光")

    def test_never_raises_on_garbage(self):
        for name, blob in (("empty.jpg", b""), ("junk.jpg", b"not an image at all"),
                           ("tiny.png", b"\x89PNG\r\n\x1a\n")):
            p = os.path.join(self.tmp, name)
            with open(p, "wb") as f:
                f.write(blob)
            try:
                res = grade.grade_image(self.fx.conn, PID, p,
                                        out_dir=os.path.join(self.tmp, "out"))
            except Exception as e:      # 绝不抛异常
                self.fail("%s 让 grade_image 抛了 %s: %s" % (name, type(e).__name__, e))
            self.assertIn("error", res)
            self.assertFalse(res.get("warp_ok"))


def _can_write_heic():
    try:
        import pillow_heif  # noqa: F401
        return True
    except Exception:
        return False


class TestMultiValueAnswers(unittest.TestCase):
    """t39：多值答案只识别出一部分 → unsure（不是 wrong）。真实用例见 S20261031-173210-47cff3 题3空2。"""

    REAL_EXPECTED = "3(a)^(2)b−a(b)^(2)，54"
    REAL_PICKED = "3a^ 2 b-a b^ 2"

    def test_partial_multi_value_is_unsure(self):
        verdict, reason = grade._judge("A", "A，54", "填空")
        self.assertEqual("unsure", verdict, "只识别到一个值应进待确认，不能判错")
        self.assertIn("缺 1 个值", reason)

    def test_full_multi_value_is_correct(self):
        self.assertEqual("correct", grade._judge("A，54", "A，54", "填空")[0])

    def test_wrong_second_value_stays_wrong(self):
        self.assertEqual("wrong", grade._judge("A，53", "A，54", "填空")[0],
                         "值对不上是真错，不能因为多值就一律 unsure")

    def test_single_value_unchanged(self):
        self.assertEqual("correct", grade._judge("5", "5", "填空")[0])
        self.assertEqual("wrong", grade._judge("6", "5", "填空")[0])

    def test_real_user_case_is_unsure(self):
        """用户真实卷：期望两个值，OCR 只读到式子 → 必须 unsure。"""
        verdict, reason = grade._judge(self.REAL_PICKED, self.REAL_EXPECTED, "填空")
        self.assertEqual("unsure", verdict, "真实用例回归：%r" % (reason,))
        self.assertIn("部分答案", reason)

    def test_tail_only_is_unsure(self):
        verdict, reason = grade._judge("54", "A，54", "填空")
        self.assertEqual("unsure", verdict)
        self.assertIn("缺 1 个值", reason)

    def test_unrelated_text_stays_wrong(self):
        # 用纯数字噪音：汉字噪音按 t17 设计本来就该进 unsure
        self.assertEqual("wrong", grade._judge("999", "A，54", "填空")[0])

    def test_split_values(self):
        self.assertEqual(["A", "54"], grade.split_values("A，54"))
        self.assertEqual(["A", "54"], grade.split_values("A, 54"))
        self.assertEqual(["A", "54", "x"], grade.split_values("A；54;x"))
        self.assertEqual(["single"], grade.split_values("single"))


T_PID = PID


class TestMultiValueCrop(unittest.TestCase):
    """多值作答题的裁切框要放宽（右侧 +8%、下侧留余量），别把行尾的 ", 54" 切掉。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mvcrop_")
        cls.fx = Fixture(cls.tmp, choices={1: "B", 2: "B"})
        # 把第 4 题（填空）改成多值答案
        man = cls.fx.manifest
        for it in man["items"]:
            if it["idx"] == 4:
                it["answer_key"] = [{"blank": 1, "value": "A，54", "norm": "A,54"}]
        cls.fx.conn.execute("UPDATE papers SET manifest=? WHERE pid=?",
                            (json.dumps(man, ensure_ascii=False), T_PID))
        cls.fx.conn.commit()
        cls.res = grade.grade_image(cls.fx.conn, T_PID, cls.fx.photo(seed=81),
                                    out_dir=os.path.join(cls.tmp, "out"))
        cls.by = {(u["idx"], u["blank"]): u for u in cls.res["items"]}

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_multi_value_flagged(self):
        u = self.by[(4, 1)]
        self.assertTrue(u.get("multi_value"), "多值题要标记 multi_value")
        self.assertTrue(u.get("crop_expanded"), "多值题的裁切框要放宽")

    def test_crop_is_wider_than_box(self):
        u = self.by[(4, 1)]
        self.assertTrue(os.path.exists(u["crop_path"]))
        cw = Image.open(u["crop_path"]).width
        box_w = u["box"]["w"]
        self.assertGreater(cw, box_w, "裁切宽应大于原框宽（右侧 +8%%）")

    def test_single_value_box_not_expanded(self):
        u = self.by[(5, 1)]      # 解答题答案框，期望单值 "9"
        self.assertFalse(u.get("multi_value"))


class TestRealMultiValueRegression(unittest.TestCase):
    """真实卷回归：S20261031-173210-47cff3 题3空2（期望「式子，54」，OCR 只读到式子）。"""

    APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CROP = os.path.join(APP, "out", "regression", "t39_multi_value_3-2.png")
    DB = os.path.join(APP, "data", "app.db")

    def test_regression_crop_kept(self):
        if not os.path.exists(self.CROP):
            self.skipTest("真实裁切样本不在：%s" % self.CROP)
        im = Image.open(self.CROP)
        self.assertEqual("L", im.mode if im.mode in ("L", "RGB") else "L")
        self.assertGreater(im.size[0], 100)
        self.assertGreater(im.size[1], 50)

    def test_real_submission_is_now_unsure(self):
        if not os.path.exists(self.DB):
            self.skipTest("没有产品库：%s" % self.DB)
        conn = sqlite3.connect(self.DB)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT detail_json FROM submissions WHERE sid LIKE '%47cff3%'").fetchone()
        finally:
            conn.close()
        if row is None:
            self.skipTest("产品库里没有这条提交")
        units = json.loads(row["detail_json"]).get("items") or []
        u = next((x for x in units if x.get("idx") == 3 and x.get("blank") == 2), None)
        self.assertIsNotNone(u, "真实提交里应有题3空2")
        self.assertEqual("wrong", u["verdict"], "旧数据当时被判错——这就是 bug 现场")
        verdict, reason = grade._judge(u["picked"], u["expected"], u.get("qtype") or "填空")
        self.assertEqual("unsure", verdict, "修好后同一证据必须进待确认：%r" % (reason,))
        self.assertIn("部分答案", reason)


class TestMultiValueThroughRecognition(unittest.TestCase):
    """走「拍照 → 识别 → 判分」全链路：只识别出部分值 → unsure。"""

    def test_partial_recognition_is_unsure(self):
        tmp = tempfile.mkdtemp(prefix="mvrec_")
        try:
            fx = Fixture(tmp)
            man = fx.manifest
            for it in man["items"]:
                if it["idx"] == 4:
                    it["answer_key"] = [{"blank": 1, "value": "A，54", "norm": "A,54"}]
            fx.conn.execute("UPDATE papers SET manifest=? WHERE pid=?",
                            (json.dumps(man, ensure_ascii=False), T_PID))
            fx.conn.commit()
            ocr = FakeOcr({"4-1": "A", "5-1": "9"})
            res = grade.grade_image(fx.conn, T_PID, fx.photo(seed=83), ocr=ocr,
                                    out_dir=os.path.join(tmp, "out"))
            u = next(x for x in res["items"] if x["idx"] == 4 and x["blank"] == 1)
            self.assertEqual("unsure", u["verdict"])
            self.assertIn("缺 1 个值", u["reason"])
            self.assertIn("部分答案", u.get("unsure_reason") or u["reason"])
            # 单值题行为不变
            u5 = next(x for x in res["items"] if x["idx"] == 5 and x["blank"] == 1)
            self.assertEqual("correct", u5["verdict"])
            # 分数口径不变：只有 correct 计分，unsure 既不算对也不算错
            self.assertEqual(sum(1 for x in res["items"] if x["verdict"] == "correct"),
                             res["score"])
        finally:
            fx.close()
            shutil.rmtree(tmp, ignore_errors=True)


def _insert_questions(conn):
    """把 ITEMS 的答案按 questions 表口径写进去（answer_raw），供 grade_by_ids 查。"""
    raws = {"00001": ("选择", 4, 0, "A"), "00002": ("选择", 4, 0, "B"),
            "00003": ("选择", 4, 0, "C"), "00004": ("填空", 0, 0, "4;1/2"),
            "00005": ("解答", 0, 2, "(1)9\n(2)3")}
    for no, (qt, oc, sc, raw) in raws.items():
        conn.execute(
            "INSERT OR REPLACE INTO questions(no,qtype,option_count,sub_count,answer_raw,"
            "is_empty,media_count,text_len,text_plain,tokens_json,fmt,src_title,src_para,"
            "family_key,kp_id,chapter,kp,kp_source,check_status)"
            " VALUES (?,?,?,?,?,0,0,10,'x','[]','','','','','','','','','')",
            (no, qt, oc, sc, raw))
    conn.commit()


class TestGradeByIds(unittest.TestCase):
    """t46：不给 manifest，只靠每题 5×5 标识里的题号判卷。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="byids_")
        cls.fx = Fixture(cls.tmp)
        _insert_questions(cls.fx.conn)
        cls.photo = cls.fx.photo(seed=21)
        cls.base = grade.grade_image(cls.fx.conn, T_PID, cls.photo,
                                     out_dir=os.path.join(cls.tmp, "o1"))
        cls.fx.conn.execute("DELETE FROM papers")      # 卷子从库里消失
        cls.fx.conn.commit()
        cls.ids = grade.grade_by_ids(cls.fx.conn, cls.photo,
                                     out_dir=os.path.join(cls.tmp, "o2"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_identified_by_question_id(self):
        self.assertIsNone(self.ids.get("error"), self.ids.get("detail"))
        self.assertEqual("question_id", self.ids.get("identified_by"))
        self.assertTrue(self.ids.get("items"))

    def test_omr_verdicts_match_manifest_path(self):
        a = sorted((u["no"], u["verdict"], u["picked"]) for u in self.base["items"]
                   if u["kind"] == card.KIND_OMR)
        b = sorted((u["no"], u["verdict"], u["picked"]) for u in self.ids["items"]
                   if u["kind"] == card.KIND_OMR)
        self.assertTrue(a)
        self.assertEqual(a, b, "无 manifest 判卷的选择题结论必须与有 manifest 一致")

    def test_score_matches(self):
        self.assertEqual(self.base["score"], self.ids["score"])
        self.assertEqual(self.base["full"], self.ids["full"])

    def test_units_carry_no_and_expected(self):
        by_no = {}
        for u in self.ids["items"]:
            if u["verdict"] == "unsure" and "题目标识不可读" in (u.get("reason") or ""):
                self.assertIsNone(u.get("no"))
                continue
            self.assertTrue(u.get("no"), "每个单元都要带题号 no")
            self.assertIsNotNone(u.get("expected"))
            by_no.setdefault(u["no"], []).append(u)
        self.assertIn("00004", by_no)
        self.assertEqual(2, len(by_no["00004"]), "2 空填空应展开成两个单元")

    def test_two_columns_both_decoded(self):
        """选择/填空并排两列时，左右列的题都能被扫到并判出。"""
        xs = {0: 0, 1: 0}
        for u in self.ids["items"]:
            if u.get("no") and u.get("crop_path") is None:
                continue
        blocks = grade.scan_ids(self.photo)
        for b in blocks["blocks"]:
            xs[b["col"]] += 1
        self.assertTrue(xs[0] >= 1 and xs[1] >= 1, "两列都应有标识块：%s" % xs)


class TestUnreadableIds(unittest.TestCase):
    """标识块翻位/涂黑 → 该题 unsure（题目标识不可读），绝不静默判错。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="badid_")
        self.fx = Fixture(self.tmp)
        _insert_questions(self.fx.conn)
        self.fx.conn.execute("DELETE FROM papers")
        self.fx.conn.commit()

    def tearDown(self):
        self.fx.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_id(self, mutate):
        img = Image.open(self.fx.filled_png).convert("L")
        d = ImageDraw.Draw(img)
        page = self.fx.manifest["card"]["pages"][0]
        it = page["items"][1]            # 第 2 题
        b = it["id_box"]
        mutate(d, b, it)
        img.save(self.fx.filled_png)
        return it

    def test_one_bit_flip_is_unreadable(self):
        def mutate(d, b, it):
            cell = b["cell"]
            fill = 255 if it["id_bits"][0] else 0       # 只翻左上角那一格
            d.rectangle([b["x"] + 1, b["y"] + 1,
                         b["x"] + cell - 3, b["y"] + cell - 3], fill=fill)
        it = self._patch_id(mutate)
        res = grade.grade_by_ids(self.fx.conn, self.fx.photo(seed=22),
                                 out_dir=os.path.join(self.tmp, "out"))
        self.assertIsNone(res.get("error"), res.get("detail"))
        self.assertEqual(1, res.get("unreadable_ids"))
        bad = [u for u in res["items"] if u["verdict"] == "unsure"
               and "题目标识不可读" in (u.get("reason") or "")]
        self.assertEqual(1, len(bad), "翻位的那题必须进 unsure")
        self.assertIsNone(bad[0].get("no"))
        # 其他题照常判出
        self.assertTrue(any(u.get("no") for u in res["items"]))

    def test_blacked_out_id_is_unreadable(self):
        def mutate(d, b, it):
            d.rectangle([b["x"] - 3, b["y"] - 3,
                         b["x"] + card.ID_SIZE + 1, b["y"] + card.ID_SIZE + 1], fill=0)
        self._patch_id(mutate)
        res = grade.grade_by_ids(self.fx.conn, self.fx.photo(seed=23),
                                 out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual(1, res.get("unreadable_ids"), res.get("detail"))
        self.assertTrue(any(u["verdict"] == "unsure" and "题目标识不可读" in (u.get("reason") or "")
                            for u in res["items"]))


class TestLegacyCardWithManifest(unittest.TestCase):
    """有 manifest（卷号在库）但拍到的是旧 4×4 卡 → legacy_card。"""

    def test_grade_image_reports_legacy_card(self):
        tmp = tempfile.mkdtemp(prefix="legacy2_")
        try:
            fx = Fixture(tmp)
            img = Image.open(fx.filled_png).convert("L")
            d = ImageDraw.Draw(img)
            for page in fx.manifest["card"]["pages"]:
                for it in page["items"]:
                    b = it["id_box"]
                    d.rectangle([b["x"] - 4, b["y"] - 4,
                                 b["x"] + card.ID_SIZE + 2, b["y"] + card.ID_SIZE + 2], fill=255)
            page = fx.manifest["card"]["pages"][0]
            b = page["items"][0]["id_box"]
            idx = 3
            nib = [(idx >> (4 * i)) & 0xF for i in range(3)]
            chk = (nib[0] ^ nib[1] ^ nib[2] ^ card._ID_CHECK_MASK) & 0xF
            bits = [(idx >> (11 - i)) & 1 for i in range(12)] + \
                   [(chk >> (3 - i)) & 1 for i in range(4)]
            d.rectangle([b["x"] - 3, b["y"] - 3, b["x"] + 97, b["y"] + 97],
                        outline=0, width=2)
            for r in range(4):
                for c in range(4):
                    if bits[r * 4 + c]:
                        d.rectangle([b["x"] + c * 24, b["y"] + r * 24,
                                     b["x"] + c * 24 + 22, b["y"] + r * 24 + 22], fill=0)
            img.save(fx.filled_png)
            res = grade.grade_image(fx.conn, T_PID, fx.photo(seed=25),
                                    out_dir=os.path.join(tmp, "out"))
            self.assertEqual("legacy_card", res.get("error"), res.get("detail"))
            self.assertIn("重新打印", res.get("detail") or "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestLegacyCard(unittest.TestCase):
    """旧 4×4 卡：明确报 legacy_card，而不是判一堆错。"""

    def test_legacy_4x4_reports_legacy_card(self):
        tmp = tempfile.mkdtemp(prefix="legacy_")
        try:
            fx = Fixture(tmp)
            _insert_questions(fx.conn)
            fx.conn.execute("DELETE FROM papers")
            fx.conn.commit()
            img = Image.open(fx.filled_png).convert("L")
            d = ImageDraw.Draw(img)
            # 把 5×5 标识全涂白（当旧卡没这些块），再画一个旧 4×4 块
            for page in fx.manifest["card"]["pages"]:
                for it in page["items"]:
                    b = it["id_box"]
                    d.rectangle([b["x"] - 4, b["y"] - 4,
                                 b["x"] + card.ID_SIZE + 2, b["y"] + card.ID_SIZE + 2], fill=255)
            idx = 5
            nib = [(idx >> (4 * i)) & 0xF for i in range(3)]
            chk = (nib[0] ^ nib[1] ^ nib[2] ^ card._ID_CHECK_MASK) & 0xF
            bits = [(idx >> (11 - i)) & 1 for i in range(12)] + \
                   [(chk >> (3 - i)) & 1 for i in range(4)]
            x0, y0, cell = 155, 900, 24
            d.rectangle([x0 - 3, y0 - 3, x0 + 4 * cell + 1, y0 + 4 * cell + 1],
                        outline=0, width=2)          # 旧卡也有一圈黑框
            for r in range(4):
                for c in range(4):
                    if bits[r * 4 + c]:
                        d.rectangle([x0 + c * cell, y0 + r * cell,
                                     x0 + c * cell + cell - 2, y0 + r * cell + cell - 2], fill=0)
            img.save(fx.filled_png)
            res = grade.grade_by_ids(fx.conn, fx.photo(seed=24),
                                     out_dir=os.path.join(tmp, "out"))
            self.assertEqual("legacy_card_need_pid", res.get("error"), res.get("detail"))
            self.assertIn("旧版答题卡", res.get("detail") or "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPhotoDiagnostics(unittest.TestCase):
    """t50：拍不清时要给结构化诊断（缺哪角/太斜/太小/反光）+ 反馈图。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="diag_")
        self.fx = Fixture(self.tmp)

    def tearDown(self):
        self.fx.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _photo_from(self, img, seed=51):
        path = os.path.join(self.tmp, "photo_%d.jpg" % seed)
        img.convert("L").save(path, quality=88)
        return path

    def test_missing_one_corner(self):
        """把右上角 ArUco 涂白 → 必须报 missing=["TR"] + missing_corner。"""
        img = Image.open(self.fx.filled_png).convert("L")
        d = ImageDraw.Draw(img)
        x, y = card.ARUCO_POS[1]          # TR
        d.rectangle([x - 6, y - 6, x + card.ARUCO_SIZE + 6, y + card.ARUCO_SIZE + 6], fill=255)
        photo = self._photo_from(img)
        res = grade.grade_image(self.fx.conn, T_PID, photo,
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual("warp_failed", res.get("error"))
        diag = res.get("diag") or {}
        self.assertEqual(3, diag.get("found"))
        self.assertEqual(["TR"], diag.get("missing"))
        self.assertEqual("missing_corner", diag.get("suggest"))
        self.assertTrue(diag.get("suggest_text"))
        self.assertIsNone(diag["corners"]["TR"])
        self.assertTrue(diag["corners"]["TL"])

    def test_tilted_20deg(self):
        """整页旋转 20° 放回去 → 角度诊断必须报 tilt（>15°）。"""
        base = Image.open(self.fx.filled_png).convert("L")
        base = base.resize((base.width // 2, base.height // 2), Image.BILINEAR)  # 省内存
        big = Image.new("L", (base.width + 600, base.height + 600), 255)
        big.paste(base, (300, 300))
        rot = big.rotate(20, resample=Image.BILINEAR, fillcolor=255, expand=True)
        photo = self._photo_from(rot, seed=52)
        out = grade.diagnose(photo)
        diag = out.get("diag") or {}
        self.assertEqual(4, diag.get("found"), "四角都拍到，应该标定成功：%s" % (diag,))
        self.assertGreater(diag.get("rotation_deg") or 0, 15.0, diag)
        self.assertEqual("tilt", diag.get("suggest"))

    def test_too_small(self):
        """缩到 30% 再放到 2 倍白底上（占比 ~9%）→ too_small。"""
        base = Image.open(self.fx.filled_png).convert("L")
        base = base.resize((base.width // 2, base.height // 2), Image.BILINEAR)  # 省内存
        small = base.resize((int(base.width * 0.3), int(base.height * 0.3)), Image.BILINEAR)
        canvas = Image.new("L", (base.width, base.height), 250)
        canvas.paste(small, ((base.width - small.width) // 2,
                             (base.height - small.height) // 2))
        photo = self._photo_from(canvas, seed=53)
        out = grade.diagnose(photo)
        diag = out.get("diag") or {}
        self.assertEqual(4, diag.get("found"), "缩小后四角仍应检到")
        self.assertLess(diag.get("paper_ratio") or 1, 0.25)
        self.assertEqual("too_small", diag.get("suggest"))

    def test_ok_card_has_no_suggest(self):
        out = grade.diagnose(self.fx.photo(seed=54))
        diag = out.get("diag") or {}
        self.assertTrue(out.get("warp_ok"))
        self.assertEqual(4, diag.get("found"))
        self.assertIsNone(diag.get("suggest"))

    def test_diag_image_written_and_openable(self):
        img = Image.open(self.fx.filled_png).convert("L")
        d = ImageDraw.Draw(img)
        x, y = card.ARUCO_POS[1]
        d.rectangle([x - 6, y - 6, x + card.ARUCO_SIZE + 6, y + card.ARUCO_SIZE + 6], fill=255)
        photo = self._photo_from(img, seed=55)
        out = grade.diagnose(photo)
        path = os.path.join(self.tmp, "diag.png")
        got = grade.diag_image(photo, out["diag"], path)
        self.assertEqual(path, got)
        self.assertTrue(os.path.exists(path))
        im = Image.open(path)
        self.assertGreater(im.size[0], 100)
        self.assertGreater(os.path.getsize(path), 5000)

    def test_diag_points_at_missing_corner(self):
        """诊断图里“让 TR 入镜”的文案要出现（红箭头指向没拍到的角）。"""
        img = Image.open(self.fx.filled_png).convert("L")
        d = ImageDraw.Draw(img)
        x, y = card.ARUCO_POS[1]
        d.rectangle([x - 6, y - 6, x + card.ARUCO_SIZE + 6, y + card.ARUCO_SIZE + 6], fill=255)
        photo = self._photo_from(img, seed=56)
        diag = grade.diagnose(photo)["diag"]
        self.assertIn("missing_corner", [diag["suggest"]])
        self.assertIn("四角", diag["suggest_text"])


class TestLegacyCardSupport(unittest.TestCase):
    """回归：旧版 4×4 答题卡在「卷号在库」时必须照常判分（v1.2 硬伤修复）。

    判据看 manifest 每题 id_box.bits 的长度：16=旧卡（卷内题序）、25=新卡（题号）。
    """

    APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB = os.path.join(APP, "data", "app.db")
    REAL_PHOTO = os.path.join(APP, "out", "P20261031-007",
                              "sub_S20261031-205422-c1227f", "source.jpg")
    LEGACY_PID = "P20261031-007"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="legacy_ok_")
        if not (os.path.exists(self.DB) and os.path.exists(self.REAL_PHOTO)):
            self.skipTest("没有真实老卡样张/产品库")
        self.db_path = os.path.join(self.tmp, "db.sqlite")
        src = sqlite3.connect(self.DB)
        dst = sqlite3.connect(self.db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self, pid=LEGACY_PID):
        row = self.conn.execute("SELECT manifest FROM papers WHERE pid=?", (pid,)).fetchone()
        return json.loads(row["manifest"]) if row else None

    def _save_manifest(self, manifest, pid=LEGACY_PID):
        self.conn.execute("UPDATE papers SET manifest=? WHERE pid=?",
                          (json.dumps(manifest, ensure_ascii=False), pid))
        self.conn.commit()

    def test_real_legacy_photo_grades(self):
        """真实老照片必须判出来，且复现历史分数 1/2。"""
        res = grade.grade_image(self.conn, self.LEGACY_PID, self.REAL_PHOTO,
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertIsNone(res.get("error"), res.get("detail"))
        self.assertEqual(1, res.get("score"), "历史记录是 1/2，必须复现")
        self.assertEqual(2, res.get("full"))
        kinds = [u["kind"] for u in res["items"]]
        self.assertIn("omr", kinds)

    def test_legacy_manifest_bits_are_16(self):
        m = self._manifest()
        self.assertEqual(1, m["card"].get("version"))
        for it in m["card"]["pages"][0]["items"]:
            self.assertEqual(16, len(it["id_box"]["bits"]), "旧卡标识是 16 位")

    def test_legacy_without_manifest_needs_pid(self):
        """卷号不在库 + 旧卡 → legacy_card_need_pid（而不是 legacy_card）。"""
        res = grade.grade_image(self.conn, "P-NOT-IN-DB", self.REAL_PHOTO,
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual("legacy_card_need_pid", res.get("error"), res.get("detail"))
        self.assertIn("卷号", res.get("detail") or "")

    def test_grade_by_ids_on_legacy_needs_pid(self):
        res = grade.grade_by_ids(self.conn, self.REAL_PHOTO,
                                 out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual("legacy_card_need_pid", res.get("error"))
        self.assertIn("旧版答题卡", res.get("detail") or "")

    def test_new_card_still_grades(self):
        """新卡（25 位）不受影响。"""
        fx = Fixture(os.path.join(self.tmp, "fx"))
        try:
            res = grade.grade_image(fx.conn, T_PID, fx.photo(seed=71),
                                    out_dir=os.path.join(self.tmp, "o_new"))
            self.assertIsNone(res.get("error"), res.get("detail"))
            self.assertEqual(3, res.get("score"))
        finally:
            fx.close()

    def test_legacy_idx_mismatch_is_pid_mismatch(self):
        """16 位标识解出的题序与 manifest 不符 → pid_mismatch（拍的确实是别的卷）。"""
        m = self._manifest()
        m["card"]["pages"][0]["items"][0]["idx"] = 99
        self._save_manifest(m)
        res = grade.grade_image(self.conn, self.LEGACY_PID, self.REAL_PHOTO,
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual("pid_mismatch", res.get("error"), res.get("detail"))

    def test_unknown_bit_length_is_legacy_card(self):
        """既不是 16 也不是 25（这里造 12 位）→ legacy_card。"""
        m = self._manifest()
        m["card"]["pages"][0]["items"][0]["id_box"]["bits"] = [0, 1, 0, 1] * 3
        self._save_manifest(m)
        res = grade.grade_image(self.conn, self.LEGACY_PID, self.REAL_PHOTO,
                                out_dir=os.path.join(self.tmp, "out"))
        self.assertEqual("legacy_card", res.get("error"), res.get("detail"))


class TestRealPhotoByIds(unittest.TestCase):
    """t54 回归：用户真实照片（P20261101-002，7×? 列版式）by_ids 要把 4 个标识全解出来。

    这张卡是 5×5 新卡（不是旧卡）：之前 id_*.png 是 108px 只是因为裁切时 inset=6
    （120-12=108），不是尺度问题；真正的 bug 是扫描按固定坐标、没有本地对齐。
    """

    APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    PHOTO = os.path.join(APP, "tests", "fixtures", "t54", "p20261101_002_source.jpg")
    if not os.path.exists(PHOTO):
        PHOTO = os.path.join(APP, "out", "P20261101-002",
                             "sub_S20261101-103711-b813ae", "source.jpg")
    EXPECT = ["10101", "12665", "11198", "05852"]

    def setUp(self):
        if not os.path.exists(self.PHOTO):
            self.skipTest("没有 t54 真实照片")

    def test_four_ids_decode(self):
        scan = grade.scan_ids(self.PHOTO)
        got = [b.get("no") for b in scan["blocks"] if b.get("no")]
        self.assertEqual(self.EXPECT, got, "四个标识都要解出题号：%s" % (got,))
        self.assertEqual(0, len(scan["unreadable"]), "不该再有「题目标识不可读」")

    def test_ids_exist_in_bank(self):
        conn = db.connect(os.path.join(self.APP, "data", "app.db"))
        try:
            scan = grade.scan_ids(self.PHOTO)
            for b in scan["blocks"]:
                row = grade._question_row(conn, b["no"])
                self.assertIsNotNone(row, "题库里查不到 %s" % b["no"])
        finally:
            conn.close()

    def test_by_ids_judges_nine_units(self):
        conn = db.connect(os.path.join(self.APP, "data", "app.db"))
        try:
            res = grade.grade_by_ids(conn, self.PHOTO,
                                     out_dir=os.path.join(self.APP, "out", "_t54_test"))
            self.assertIsNone(res.get("error"), res.get("detail"))
            self.assertEqual("question_id", res.get("identified_by"))
            nos = {u["no"] for u in res["items"]}
            self.assertEqual(set(self.EXPECT), nos, "9 个作答单元应覆盖这 4 道题")
            # 没有 OCR（manual）时填空/解答应是 pending，不是「题目标识不可读」
            self.assertFalse([u for u in res["items"]
                              if "题目标识不可读" in (u.get("reason") or "")])
        finally:
            conn.close()

    def test_scan_is_fast(self):
        import time as _t
        t0 = _t.perf_counter()
        grade.scan_ids(self.PHOTO)
        self.assertLess(_t.perf_counter() - t0, 3.0, "扫描不该超过 3 秒")


if __name__ == "__main__":
    unittest.main(verbosity=2)
