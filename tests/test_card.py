# -*- coding: utf-8 -*-
"""T4 · 机读答题卡生成（card.py）的结构测试。

覆盖（不需要相机/网络，秒级）：
  1. 16bit 题序 ID 编解码自洽 + 校验位能抓错；
  2. 所有几何（ArUco / QR / ID 格 / 选择格 / 填空框 / 过程区 / 答案框 / 小节标题）
     都在 2480×3508 页内，且两两不重叠；
  3. 分页正确：题不重不漏、顺序不乱、每页都有独立的 ArUco 四角 + QR + 页码；
  4. 生成的 PNG 可读（PIL/cv2），尺寸=300dpi A4，墨迹占比合理；
  5. 真实 ArUco 四角能被 cv2.aruco 检出（id 0/1/2/3，位置误差 < 10px）——
     这是判卷侧真正依赖的东西，不能只信自己写的坐标；
  6. QR 能被 cv2.QRCodeDetector 解出 {"pid","p"}；
  7. 从渲染出的 PNG 上按坐标采样 ID 格，能解回 idx（判卷侧同款取数路径）；
  8. card 字段合并回 manifest，PDF 页数 = PNG 张数、页面是 A4；
  9. 三种题型的作答单元形状符合契约（选择 4×6mm、填空 40×10mm、解答过程区+逐问答案框）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import card as cardmod  # noqa: E402
from card import (  # noqa: E402
    ARUCO_POS,
    ARUCO_SIZE,
    BODY_RECT,
    DICT_4X4_50,
    KIND_FILL,
    KIND_OMR,
    KIND_SOLUTION,
    PAGE_H,
    PAGE_W,
    QR_SIZE,
    build_card,
    decode_idx_bits,
    encode_idx_bits,
)

PID = "P20261031-001"


def _item(idx, qtype, n_blank=1, options=None):
    return {
        "idx": idx,
        "no": "%05d" % (100 + idx),
        "qtype": qtype,
        "kp_id": "1.2",
        "family_key": None,
        "stem_tokens": [],
        "answer_key": [{"blank": b + 1, "value": "x", "norm": "x"} for b in range(n_blank)],
        "options": options if options is not None else ["A", "B", "C", "D"],
        "space_paras": 0,
    }


def make_manifest(items, pid=PID, title="七年级数学 · 训练卷 A", paper_pages=None):
    m = {
        "pid": pid,
        "created_at": "2026-10-31T14:00:00+08:00",
        "title": title,
        "params": {},
        "page": {"w_pt": 595.3, "h_pt": 841.9, "dpi": 300, "w_px": PAGE_W, "h_px": PAGE_H},
        "items": items,
    }
    if paper_pages is not None:
        m["paper"] = {"pages": paper_pages}
    return m


def rects_of_page(page):
    """把一页里所有几何矩形摊平成 [(label, x, y, w, h)]，供越界/重叠检查。"""
    out = []
    for a in page["marks"]["aruco"]:
        out.append(("aruco%d" % a["id"], a["x"], a["y"], a["size"], a["size"]))
    q = page.get("qr")
    if q:
        out.append(("qr", q["x"], q["y"], q["size"], q["size"]))
    for hd in page.get("headers", []):
        out.append(("header:%s" % hd.get("text", "")[:8], hd["x"], hd["y"], hd["w"], hd["h"]))
    for it in page["items"]:
        tag = "%s#%d" % (it["kind"], it["idx"])
        if "id_box" in it:
            b = it["id_box"]
            grid = 4 if len(b.get("bits") or []) == 16 else 5
            out.append((tag + ".id", b["x"], b["y"], b["cell"] * grid, b["cell"] * grid))
        for c in it.get("cells", []):
            out.append((tag + ".cell" + str(c["opt"]), c["x"], c["y"], c["w"], c["h"]))
        for b in it.get("boxes", []):
            out.append((tag + ".box%d" % b["blank"], b["x"], b["y"], b["w"], b["h"]))
        if "process_box" in it:
            b = it["process_box"]
            out.append((tag + ".process", b["x"], b["y"], b["w"], b["h"]))
        for b in it.get("answer_boxes", []):
            out.append((tag + ".ans%d" % b["blank"], b["x"], b["y"], b["w"], b["h"]))
    return out


class _Base(unittest.TestCase):
    manifest = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="card_")
        cls.manifest = make_manifest(cls.manifest)
        cls.res = build_card(cls.manifest, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestIdCodec(unittest.TestCase):
    def test_roundtrip_all_no(self):
        """t46：标识编码的是题号（5×5=25 位），不是卷内题序。"""
        for v in list(range(1, 500)) + [9999, 22897, 99999]:
            bits = cardmod.encode_id_bits(v)
            self.assertEqual(25, len(bits), "ID 必须是 5x5=25 格")
            self.assertEqual("%05d" % v, cardmod.decode_id_bits(bits),
                             "题号 %r 编解码不自洽" % (v,))

    def test_bits_are_01(self):
        self.assertTrue(set(cardmod.encode_id_bits(1234)) <= {0, 1})

    def test_rejects_out_of_range_no(self):
        with self.assertRaises(ValueError):
            cardmod.encode_id_bits(1 << 20)

    def test_zero_rejected_on_decode(self):
        self.assertIsNone(cardmod.decode_id_bits(cardmod.encode_id_bits(0)), "00000 不是题号")

    def test_checksum_rejects_corruption(self):
        bits = cardmod.encode_id_bits("03370")
        for k in range(25):
            bad = list(bits)
            bad[k] ^= 1
            self.assertIsNone(
                cardmod.decode_id_bits(bad), "第 %d 位翻错居然还能解出题号，校验位无效" % k)
        self.assertIsNone(cardmod.decode_id_bits(bits[:16]), "旧 4×4 长度不该被当成新卡")

    def test_rejects_bad_length(self):
        self.assertIsNone(cardmod.decode_id_bits([1, 0, 1]))


class TestIdCodecSixDigit(unittest.TestCase):
    """t11 · 15 万题的题号是 6 位（100000..1048575）：标识仍是 25 格，编码口径不变。

    老题号（00001..99999）必须逐字节不变 —— 已打印的旧答题卡还得能判。
    """

    SIX = ("100000", "100001", "123456", "999999", "1048575")

    def test_roundtrip_six_digit(self):
        for s in self.SIX:
            bits = cardmod.encode_id_bits(s)
            self.assertEqual(25, len(bits), "6 位题号也必须是 5x5=25 格")
            self.assertEqual(s, cardmod.decode_id_bits(bits),
                             "6 位题号 %s 编解码不自洽" % s)

    def test_max_no_is_2_pow_20_minus_1(self):
        self.assertEqual((1 << 20) - 1, cardmod.MAX_NO)
        self.assertEqual("1048575", cardmod.decode_id_bits(cardmod.encode_id_bits(cardmod.MAX_NO)))
        with self.assertRaises(ValueError):
            cardmod.encode_id_bits(cardmod.MAX_NO + 1)

    def test_five_digit_stays_zero_padded(self):
        """老题号口径不变：解出来还是 5 位补零字符串。"""
        self.assertEqual("00001", cardmod.decode_id_bits(cardmod.encode_id_bits(1)))
        self.assertEqual("99999", cardmod.decode_id_bits(cardmod.encode_id_bits(99999)))

    def test_encode_accepts_int_and_str(self):
        self.assertEqual(cardmod.encode_id_bits("100000"), cardmod.encode_id_bits(100000))

    def test_crc_still_catches_six_digit_corruption(self):
        """6 位题号每一位翻错都必须被 CRC 抓住（不能靠范围检查兜底）。"""
        bits = cardmod.encode_id_bits("100001")
        for k in range(25):
            bad = list(bits)
            bad[k] ^= 1
            self.assertIsNone(cardmod.decode_id_bits(bad),
                              "6 位题号第 %d 位翻错还能解出题号，校验位失效" % k)

    def test_decode_idx_returns_int(self):
        self.assertEqual(100001, cardmod.decode_idx_bits(cardmod.encode_id_bits("100001")))

    def test_plan_item_uses_six_digit_no(self):
        """出卡几何里的 id_bits 也必须encode 出 6 位题号（判卷侧按它反查题目）。"""
        geom, _h = cardmod.plan_item(
            {"idx": 1, "no": "100002", "qtype": "选择", "options": ["A", "B", "C", "D"]},
            cardmod.KIND_OMR, 1000)
        self.assertEqual(25, len(geom["id_bits"]))
        self.assertEqual("100002", cardmod.decode_id_bits(geom["id_bits"]))


class TestSinglePage(_Base):
    manifest = [
        _item(1, "选择", 1),
        _item(2, "选择", 1),
        _item(3, "填空", 2),
        _item(4, "解答", 2),
    ]

    def test_outputs_exist(self):
        self.assertTrue(os.path.exists(self.res["pdf"]), "card.pdf 未生成")
        self.assertGreater(os.path.getsize(self.res["pdf"]), 10 * 1024)
        self.assertEqual(1, len(self.res["png"]), "4 题应放得进 1 页")
        for p in self.res["png"]:
            self.assertTrue(os.path.exists(p))

    def test_manifest_gets_card(self):
        c = self.manifest.get("card")
        self.assertIsNotNone(c, "card 字段没合并进 manifest")
        self.assertEqual(c, self.res["card"])
        self.assertEqual(1, len(c["pages"]))

    def test_page_contract_shape(self):
        page = self.res["card"]["pages"][0]
        self.assertEqual(1, page["page"])
        aruco = page["marks"]["aruco"]
        self.assertEqual(4, len(aruco))
        self.assertEqual({0, 1, 2, 3}, {a["id"] for a in aruco})
        self.assertEqual(DICT_4X4_50, cardmod.ARUCO_DICT_ID, "必须用 DICT_4X4_50 字典")
        for a in aruco:
            self.assertAlmostEqual(ARUCO_SIZE, a["size"], delta=2)
            self.assertEqual((a["x"], a["y"]), ARUCO_POS[a["id"]])
        qr = page["qr"]
        self.assertEqual({"pid": PID, "p": 1, "v": cardmod.CARD_VERSION}, qr["payload"])
        self.assertAlmostEqual(QR_SIZE, qr["size"], delta=2)

    def test_kinds_match_qtype(self):
        kinds = [it["kind"] for it in self.res["card"]["pages"][0]["items"]]
        self.assertEqual([KIND_OMR, KIND_OMR, KIND_FILL, KIND_SOLUTION], kinds)
        idxs = [it["idx"] for it in self.res["card"]["pages"][0]["items"]]
        self.assertEqual([1, 2, 3, 4], idxs)

    def test_omr_cells_geometry(self):
        it = self.res["card"]["pages"][0]["items"][0]
        self.assertEqual(["A", "B", "C", "D"], [c["opt"] for c in it["cells"]])
        for c in it["cells"]:
            self.assertAlmostEqual(71, c["w"], delta=2, msg="选择格应约 6mm=71px")
            self.assertAlmostEqual(71, c["h"], delta=2)
        self.assertEqual(24, it["id_box"]["cell"])
        self.assertEqual(25, len(it["id_box"]["bits"]), "标识格 5×5=25 位")
        self.assertEqual(str(it["no"]), cardmod.decode_id_bits(it["id_box"]["bits"]))

    def test_fill_boxes_geometry(self):
        it = self.res["card"]["pages"][0]["items"][2]
        self.assertEqual(2, len(it["boxes"]), "2 空应给 2 个书写框")
        self.assertEqual([1, 2], [b["blank"] for b in it["boxes"]])
        for b in it["boxes"]:
            self.assertAlmostEqual(472, b["w"], delta=4, msg="填空框应约 40mm=472px")
            self.assertAlmostEqual(118, b["h"], delta=4, msg="填空框应约 10mm=118px")

    def test_solution_geometry(self):
        it = self.res["card"]["pages"][0]["items"][3]
        self.assertEqual(2, len(it["answer_boxes"]), "解答按小问给独立答案框")
        p = it["process_box"]
        self.assertGreater(p["h"], 300, "过程区应是大框")
        self.assertGreater(p["w"], 1500)
        for b in it["answer_boxes"]:
            self.assertGreaterEqual(b["w"], 400)
        # 答案框必须在过程区下方，不叠在一起
        self.assertGreaterEqual(min(b["y"] for b in it["answer_boxes"]), p["y"] + p["h"])

    def test_all_geometry_inside_page(self):
        for page in self.res["card"]["pages"]:
            for label, x, y, w, h in rects_of_page(page):
                self.assertGreaterEqual(x, 0, label)
                self.assertGreaterEqual(y, 0, label)
                self.assertLessEqual(x + w, PAGE_W, "%s 右越界" % label)
                self.assertLessEqual(y + h, PAGE_H, "%s 下越界" % label)

    def test_geometry_inside_body(self):
        bx, by, bw, bh = BODY_RECT
        for page in self.res["card"]["pages"]:
            for it in page["items"]:
                one = {"page": page["page"], "marks": {"aruco": []},
                       "qr": {"x": 0, "y": 0, "size": 0}, "items": [it], "headers": []}
                rects = [r for r in rects_of_page(one) if not r[0].startswith("qr")]
                for label, x, y, w, h in rects:
                    self.assertGreaterEqual(y, by - 1, "%s 高于正文区" % label)
                    self.assertLessEqual(y + h, by + bh + 1, "%s 超出正文区底部" % label)

    def test_no_overlap(self):
        for page in self.res["card"]["pages"]:
            rects = rects_of_page(page)
            for i in range(len(rects)):
                for j in range(i + 1, len(rects)):
                    la, ax, ay, aw, ah = rects[i]
                    lb, bx, by, bw, bh = rects[j]
                    hit = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
                    self.assertFalse(hit, "%s 与 %s 重叠：%s / %s" % (la, lb, rects[i], rects[j]))

    def test_png_readable(self):
        for p in self.res["png"]:
            with Image.open(p) as im:
                self.assertEqual((PAGE_W, PAGE_H), im.size, "PNG 必须是 2480x3508")
            self.assertIsNotNone(cv2.imread(p), "cv2 读不出 PNG")
            arr = np.array(Image.open(p).convert("L"))
            ink = float((arr < 128).mean())
            self.assertGreater(ink, 0.002, "页面几乎空白")
            self.assertLess(ink, 0.35, "页面墨迹太多，可能画错")

    def test_aruco_detectable_on_png(self):
        det = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
            cv2.aruco.DetectorParameters(),
        )
        for p in self.res["png"]:
            arr = cv2.imread(p)
            corners, ids, _ = det.detectMarkers(arr)
            self.assertIsNotNone(ids, "渲染出的页面里检不到 ArUco")
            got = sorted(int(v) for v in ids.ravel())
            self.assertEqual([0, 1, 2, 3], got, "四角 ArUco 编号不对")
            for c, i in zip(corners, ids.ravel()):
                ex, ey = ARUCO_POS[int(i)]
                x0, y0 = c.reshape(-1, 2).min(axis=0)
                self.assertLess(abs(x0 - ex), 10, "aruco%d x 偏移过大" % i)
                self.assertLess(abs(y0 - ey), 10, "aruco%d y 偏移过大" % i)

    def test_qr_decodable_on_png(self):
        det = cv2.QRCodeDetector()
        for p, page in zip(self.res["png"], self.res["card"]["pages"]):
            arr = cv2.imread(p)
            q = page["qr"]
            crop = arr[q["y"]: q["y"] + q["size"], q["x"]: q["x"] + q["size"]]
            data, _, _ = det.detectAndDecode(crop)
            self.assertTrue(data, "QR 解不出来（解到空串）")
            self.assertEqual(
                {"pid": PID, "p": page["page"], "v": cardmod.CARD_VERSION},
                json.loads(data), "QR payload 与卡面不一致")

    def test_id_box_decodable_from_png(self):
        """按坐标采样渲染图上的 5×5 标识 → 必须解回题号（判卷侧取数路径）。"""
        arr = np.array(Image.open(self.res["png"][0]).convert("L"))
        for it in self.res["card"]["pages"][0]["items"]:
            b = it["id_box"]
            cell = b["cell"]
            bits = []
            for r in range(cardmod.ID_GRID):
                for c in range(cardmod.ID_GRID):
                    cy = int(b["y"] + cell * (r + 0.5))
                    cx = int(b["x"] + cell * (c + 0.5))
                    bits.append(1 if arr[cy, cx] < 128 else 0)
            self.assertEqual(str(it["no"]), cardmod.decode_id_bits(bits),
                             "渲染后的标识格解不回题号")

    def test_pdf_is_a4(self):
        import pymupdf

        doc = pymupdf.open(self.res["pdf"])
        try:
            self.assertEqual(len(self.res["png"]), doc.page_count)
            for page in doc:
                self.assertAlmostEqual(595.28, page.rect.width, delta=1.0)
                self.assertAlmostEqual(841.89, page.rect.height, delta=1.0)
        finally:
            doc.close()


class TestMultipage(_Base):
    manifest = [_item(i, "解答", 2) for i in range(1, 9)] + [
        _item(i, "选择", 1) for i in range(9, 19)
    ]

    def test_paginates(self):
        pages = self.res["card"]["pages"]
        self.assertGreater(len(pages), 1, "8 道解答题不可能一页放完")
        self.assertEqual(len(pages), len(self.res["png"]))
        self.assertEqual(list(range(1, len(pages) + 1)), [p["page"] for p in pages])

    def test_each_page_has_grouped_marks_and_qr_only_first(self):
        for p in self.res["card"]["pages"]:
            self.assertEqual(4, len(p["marks"]["aruco"]))
            want = sorted(cardmod.page_marker_ids(p["page"]).values())
            self.assertEqual(want, sorted(a["id"] for a in p["marks"]["aruco"]),
                             "第 %d 页四角 id = 4(p-1)..4(p-1)+3" % p["page"])
            if p["page"] == 1:
                self.assertEqual(1, p["qr"]["payload"]["p"])
            else:
                self.assertIsNone(p["qr"], "v4 只有第 1 页印 QR")

    def test_no_item_lost_or_duplicated(self):
        seen = []
        for p in self.res["card"]["pages"]:
            self.assertTrue(p["items"], "第 %d 页是空页" % p["page"])
            seen.extend(it["idx"] for it in p["items"])
        self.assertEqual([it["idx"] for it in self.manifest["items"]], seen, "题序丢失/重复/乱序")
        for p in self.res["card"]["pages"]:
            for it in p["items"]:
                self.assertEqual(str(it["no"]), cardmod.decode_id_bits(it["id_box"]["bits"]),
                                 "标识格必须编码题号 no")

    def test_no_overlap_multipage(self):
        for page in self.res["card"]["pages"]:
            rects = rects_of_page(page)
            for i in range(len(rects)):
                for j in range(i + 1, len(rects)):
                    la, ax, ay, aw, ah = rects[i]
                    lb, bx, by, bw, bh = rects[j]
                    hit = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
                    self.assertFalse(hit, "%s 与 %s 重叠" % (la, lb))

    def test_pages_are_full_enough(self):
        """不许一页只放一题就翻页（会浪费纸）。"""
        for p in self.res["card"]["pages"][:-1]:
            self.assertGreaterEqual(len(p["items"]), 2, "第 %d 页利用不足" % p["page"])

    def test_qr_decodes_on_first_page_only(self):
        """v4：QR 只在第 1 页；其余页 read_qr(None) 必须安全返回 None（不抛）。"""
        self.assertIsNotNone(cardmod.read_qr, "card.read_qr 必须存在，grade.py 直接复用")
        for png, page in zip(self.res["png"], self.res["card"]["pages"]):
            got = cardmod.read_qr(png, page.get("qr"))
            if page["page"] == 1:
                self.assertIsNotNone(got, "第 1 页 QR 解不出来")
                self.assertEqual(page["qr"]["payload"], got)
            else:
                self.assertIsNone(got, "第 %d 页没有 QR，read_qr 应返回 None" % page["page"])


class TestNumberingContinues(_Base):
    manifest = [_item(1, "选择", 1), _item(2, "解答", 1)]
    manifest_paper_pages = 2

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="card2_")
        cls.manifest = make_manifest(cls.manifest, paper_pages=cls.manifest_paper_pages)
        cls.res = build_card(cls.manifest, cls.tmp)

    def test_label_continues_after_paper(self):
        p = self.res["card"]["pages"][0]
        self.assertEqual(1, p["page"], "卡内页序从 1 起")
        self.assertEqual(3, p["label"], "卡面印的页码应接在卷面最后一页之后")
        self.assertEqual(1, p["qr"]["payload"]["p"])

    def test_start_page_and_warnings(self):
        c = self.res["card"]
        self.assertEqual(3, c["start_page"], "start_page 必须 = A+1")
        self.assertEqual(2, c["paper_pages"], "要记录读到的卷面页数 A")
        self.assertEqual([], c["warnings"], "A 存在时不该有告警")

    def test_footer_text_uses_absolute_page(self):
        """页脚口径：卷面与答题卡各自编号，卡只标续页绝对页号 P{A+M}。"""
        self.assertEqual("答题卡 · 第 1 页 / 共 1 页 ·（卷面续页 P3）",
                         cardmod.page_footer_text(1, 1, 3))
        self.assertEqual("答题卡 · 第 2 页 / 共 2 页 ·（卷面续页 P4）",
                         cardmod.page_footer_text(2, 2, 4))

    def test_footer_rendered_on_png(self):
        """页脚真的画进 PNG 了（页脚区有墨迹）。"""
        import numpy as np
        arr = np.array(Image.open(self.res["png"][0]).convert("L"))
        band = arr[3150:3300, 600:1900]
        self.assertLess(int(band.min()), 128, "页脚区应有文字墨迹")


class TestMissingPaperPages(unittest.TestCase):
    """旧卷/单测没有 manifest['paper']：A=0 起算，但必须告警（不许静默）。"""

    def test_warns_and_does_not_crash(self):
        tmp = tempfile.mkdtemp(prefix="card_nopaper_")
        try:
            m = make_manifest([_item(i, "选择", 1) for i in range(1, 20)])
            with self.assertLogs("card", level="WARNING") as logs:
                res = cardmod.build_card(m, tmp)
            self.assertTrue(any("paper" in ln for ln in logs.output), logs.output)
            c = res["card"]
            self.assertEqual(1, c["start_page"], "A 缺失时按 1 起算")
            self.assertEqual(0, c["paper_pages"])
            self.assertTrue(c["warnings"], "card['warnings'] 要带上告警")
            self.assertIn("paper", c["warnings"][0])
            self.assertEqual(list(range(1, c["n_pages"] + 1)),
                             [p["label"] for p in c["pages"]])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestJudgementType(unittest.TestCase):
    """判断题用 omr 两格（对/错），不能崩。"""

    def test_juedge(self):
        tmp = tempfile.mkdtemp(prefix="card3_")
        try:
            m = make_manifest([_item(1, "判断", 1, options=["√", "×"]), _item(2, "其他", 1)])
            res = build_card(m, tmp)
            items = res["card"]["pages"][0]["items"]
            self.assertEqual(KIND_OMR, items[0]["kind"])
            self.assertEqual(["√", "×"], [c["opt"] for c in items[0]["cells"]])
            self.assertEqual(2, len(items[0]["cells"]))
            self.assertEqual(KIND_FILL, items[1]["kind"])
            self.assertAlmostEqual(472, items[1]["boxes"][0]["w"], delta=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestLooseItems(unittest.TestCase):
    """上游 manifest 字段不全时不许整条流水线崩（按题内序号兜底）。"""

    def test_missing_idx_and_qtype(self):
        tmp = tempfile.mkdtemp(prefix="card5_")
        try:
            m = make_manifest([
                {"no": "00001", "qtype": "选择", "options": ["A", "B", "C", "D"],
                 "answer_key": [{"blank": 1, "value": "A", "norm": "A"}]},
                {"no": "00002", "qtype": "解答",
                 "answer_key": [{"blank": 1, "value": "x", "norm": "x"}]},
                {"no": "00003"},   # 连 qtype 都没有 → 当填空题
            ])
            res = build_card(m, tmp)
            items = res["card"]["pages"][0]["items"]
            self.assertEqual([1, 2, 3], [it["idx"] for it in items], "缺 idx 应按顺序补齐")
            self.assertEqual([KIND_OMR, KIND_SOLUTION, KIND_FILL], [it["kind"] for it in items])
            for it in items:
                self.assertEqual(it["idx"], decode_idx_bits(it["id_box"]["bits"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAnswerCellEnumeration(unittest.TestCase):
    """t9 拼长图要用：把本卷所有「非选择」作答格按页/题序枚举出来。"""

    def test_iter_answer_cells(self):
        tmp = tempfile.mkdtemp(prefix="card6_")
        try:
            m = make_manifest([
                _item(1, "选择", 1),
                _item(2, "填空", 2),
                _item(3, "解答", 3),
                _item(4, "填空", 1),
            ])
            res = build_card(m, tmp)
            cells = list(cardmod.iter_answer_cells(res["card"]))
            self.assertEqual(6, len(cells), "2+3+1=6 个作空格，选择题不计")
            got = [(c["idx"], c["blank"], c["kind"]) for c in cells]
            self.assertEqual([(2, 1, KIND_FILL), (2, 2, KIND_FILL),
                              (3, 1, KIND_SOLUTION), (3, 2, KIND_SOLUTION),
                              (3, 3, KIND_SOLUTION), (4, 1, KIND_FILL)], got)
            for c in cells:
                b = c["box"]
                self.assertGreater(b["w"], 100)
                self.assertGreater(b["h"], 50)
                self.assertEqual(1, c["page"])
                self.assertEqual(PID, c["pid"])
            # 枚举顺序必须与版面从上到下一致
            ys = [c["box"]["y"] for c in cells]
            self.assertEqual(sorted(ys), ys, "枚举顺序应与版面一致")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_iter_empty_when_only_choice(self):
        tmp = tempfile.mkdtemp(prefix="card7_")
        try:
            m = make_manifest([_item(1, "选择", 1)])
            res = build_card(m, tmp)
            self.assertEqual([], list(cardmod.iter_answer_cells(res["card"])))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDeterministic(unittest.TestCase):
    def test_same_manifest_same_geometry(self):
        tmp = tempfile.mkdtemp(prefix="card4_")
        try:
            m = make_manifest([_item(i, "选择", 1) for i in range(1, 7)])
            a = build_card(m, tmp)["card"]
            b = build_card(make_manifest([_item(i, "选择", 1) for i in range(1, 7)]), tmp)["card"]
            self.assertEqual(json.dumps(a, sort_keys=True, ensure_ascii=False),
                             json.dumps(b, sort_keys=True, ensure_ascii=False))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCardVersion(unittest.TestCase):
    """t50 回归：新卡版本号要跟着标识格式升到 2（旧卡文件里的 1 不动）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ver_")
        m = make_manifest([_item(i, "选择", 1) for i in range(1, 4)], pid="P20261101-001")
        m["paper"] = {"pages": 1}
        cls.res = build_card(m, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_new_card_version_is_4(self):
        self.assertEqual(4, cardmod.CARD_VERSION)
        self.assertEqual(1, cardmod.LEGACY_CARD_VERSION)
        self.assertEqual(4, self.res["card"]["version"])
        self.assertEqual(4, self.res["manifest"]["card"]["version"], "要合并进 manifest")
        self.assertTrue(self.res["card"]["aruco_groups"], "v4 要标出「页序在 ArUco id 里」")

    def test_qr_payload_carries_version(self):
        page = self.res["card"]["pages"][0]
        payload = page["qr"]["payload"]
        self.assertEqual(cardmod.CARD_VERSION, payload.get("v"), "QR 里要带卡版本")
        self.assertEqual(page["page"], payload["p"], "p 仍是本卡页序（判卷按它选页）")

    def test_id_bits_still_25(self):
        for page in self.res["card"]["pages"]:
            for it in page["items"]:
                self.assertEqual(25, len(it["id_box"]["bits"]))
                self.assertEqual(str(it["no"]), cardmod.decode_id_bits(it["id_box"]["bits"]))

    def test_health_is_return_value_not_manifest(self):
        self.assertIn("health", self.res, "health 是 build_card 的返回值")
        self.assertNotIn("health", self.res["manifest"].get("card") or {},
                         "health 不写进 manifest（docstring 已写明）")


class TestStudentArea(unittest.TestCase):
    """t57：v3 学生信息区（学号涂卡 0-9 × 8 位 + 姓名/班级框）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="stu_")
        m = make_manifest([_item(i, "选择", 1) for i in range(1, 3)], pid="P20261107-100")
        m["paper"] = {"pages": 1}
        cls.res = build_card(m, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_area_present(self):
        st = self.res["card"].get("student")
        self.assertIsNotNone(st, "v3 卡必须带 student")
        self.assertEqual(8, st["no_digits"])
        self.assertEqual(8, len(st["columns"]))
        for col in st["columns"]:
            self.assertEqual(list(range(10)), [c["digit"] for c in col["cells"]])
        self.assertGreaterEqual(st["cell_px"], cardmod.STUDENT_MIN_CELL)
        self.assertIn("name_box", st)
        self.assertIn("class_box", st)

    def test_geometry_no_overlap(self):
        st = self.res["card"]["student"]
        page = self.res["card"]["pages"][0]
        rects = [("student_area", st["area"]["x"], st["area"]["y"],
                  st["area"]["w"], st["area"]["h"])]
        for i, col in enumerate(st["columns"]):
            for c in col["cells"]:
                rects.append(("stu_%d_%d" % (i, c["digit"]), c["x"], c["y"], c["w"], c["h"]))
        rects.append(("name_box", st["name_box"]["x"], st["name_box"]["y"],
                      st["name_box"]["w"], st["name_box"]["h"]))
        rects.append(("class_box", st["class_box"]["x"], st["class_box"]["y"],
                      st["class_box"]["w"], st["class_box"]["h"]))
        # 与 ArUco / QR / 正文区 不能重叠
        fixed = [("aruco_%d" % mid, x, y, cardmod.ARUCO_SIZE, cardmod.ARUCO_SIZE)
                 for mid, (x, y) in cardmod.ARUCO_POS.items()]
        fixed.append(("qr", page["qr"]["x"], page["qr"]["y"], page["qr"]["size"], page["qr"]["size"]))
        fixed.append(("body", cardmod.BODY_RECT[0], cardmod.BODY_RECT[1],
                      cardmod.BODY_RECT[2], cardmod.BODY_RECT[3]))
        for label, x, y, w, h in rects:
            self.assertGreaterEqual(x, 0, label)
            self.assertLessEqual(x + w, cardmod.PAGE_W, "%s 右越界" % label)
            self.assertLessEqual(y + h, cardmod.PAGE_H, "%s 下越界" % label)
            for fl, fx, fy, fw, fh in fixed:
                hit = x < fx + fw and fx < x + w and y < fy + fh and fy < y + h
                self.assertFalse(hit, "%s 与 %s 重叠" % (label, fl))

    def test_encode_decode_roundtrip(self):
        for no in ("20250317", "00000001", "99999999", "12345678"):
            digits = cardmod.encode_student_bits(no)
            self.assertEqual(8, len(digits))
            ratios = [[0.0] * 10 for _ in digits]
            for i, d in enumerate(digits):
                ratios[i][d] = 0.9
            self.assertEqual(no, cardmod.decode_student_bits(ratios))

    def test_decode_tolerates_one_blank(self):
        digits = cardmod.encode_student_bits("20250317")
        ratios = [[0.0] * 10 for _ in digits]
        for i, d in enumerate(digits):
            ratios[i][d] = 0.9
        ratios[3] = [0.05] * 10               # 第 4 位没涂
        self.assertEqual("202?0317", cardmod.decode_student_bits(ratios))

    def test_decode_blank_returns_none(self):
        self.assertIsNone(cardmod.decode_student_bits([[0.0] * 10] * 8))
        self.assertIsNone(cardmod.decode_student_bits([[0.0] * 10] * 8, max_unknown=0))
        self.assertIsNone(cardmod.decode_student_bits([]))
        self.assertIsNone(cardmod.decode_student_bits(None))

    def test_decode_two_blanks_returns_none(self):
        digits = cardmod.encode_student_bits("20250317")
        ratios = [[0.0] * 10 for _ in digits]
        for i, d in enumerate(digits):
            ratios[i][d] = 0.9
        ratios[1] = [0.02] * 10
        ratios[6] = [0.02] * 10
        self.assertIsNone(cardmod.decode_student_bits(ratios))

    def test_decode_takes_deepest_cell(self):
        """两个格都有墨迹时取更深的那一格（涂改容错）。"""
        ratios = [[0.0] * 10 for _ in range(8)]
        for col in ratios:
            col[7] = 0.9
        ratios[2][5] = 0.45          # 同一行涂重了，浅的那个不算
        self.assertEqual("77777777", cardmod.decode_student_bits(ratios))

    def test_read_student_no_from_render(self):
        from PIL import ImageDraw
        st = self.res["card"]["student"]
        page = self.res["card"]["pages"][0]
        img = Image.open(self.res["png"][0]).convert("L")
        d = ImageDraw.Draw(img)
        want = "20250317"
        for i, ch in enumerate(want):
            cell = st["columns"][i]["cells"][int(ch)]
            d.rectangle([cell["x"] + 3, cell["y"] + 3,
                         cell["x"] + cell["w"] - 3, cell["y"] + cell["h"] - 3], fill=0)
        filled = os.path.join(self.tmp, "filled.png")
        img.save(filled)
        self.assertEqual(want, cardmod.read_student_no(self.res["card"], filled))
        # 空白卡 → 空串（不是异常）
        self.assertEqual("", cardmod.read_student_no(self.res["card"], self.res["png"][0]))

    def test_old_card_without_student_is_safe(self):
        """v1/v2 卡（没有 student 字段）读学号不报错，返回空串。"""
        self.assertEqual("", cardmod.read_student_no({"version": 2}, self.res["png"][0]))
        self.assertEqual("", cardmod.read_student_no({}, None))

    def test_self_check_sample_has_student_area(self):
        import numpy as np
        arr = np.array(Image.open(self.res["png"][0]).convert("L"))
        st = self.res["card"]["student"]
        area = arr[st["area"]["y"]:st["area"]["y"] + st["area"]["h"],
                   st["area"]["x"]:st["area"]["x"] + st["area"]["w"]]
        self.assertLess(int(area.min()), 128, "学生区应有墨迹（网格/文字）")
        self.assertLess(float((area < 128).mean()), 0.3, "学生区不能糊成一团")

    def test_digits_configurable(self):
        for n in (6, 8, 10):
            st = cardmod.student_layout(digits=n)
            self.assertEqual(n, st["no_digits"])
            self.assertEqual(n, len(st["columns"]))
            last = st["columns"][-1]["cells"][0]
            self.assertLess(last["x"] + st["cell_px"], cardmod.STUDENT_RIGHT)


class TestV4SingleQrAndMarkerGroups(unittest.TestCase):
    """t78：二维码精简到只剩首页 1 个；页序改用 ArUco id 分组。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="v4card_")
        cls.manifest = make_manifest(
            [_item(i, "选择", 1) for i in range(1, 13)]
            + [_item(i, "填空", 1) for i in range(13, 19)]
            + [_item(i, "解答", 1) for i in range(19, 25)],
            pid="P20261107-401")
        cls.manifest["paper"] = {"pages": 1}
        cls.res = build_card(cls.manifest, cls.tmp)
        cls.card = cls.res["card"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_version_is_4_and_flags_grouped(self):
        self.assertEqual(4, cardmod.CARD_VERSION)
        self.assertEqual(4, self.card["version"])
        self.assertTrue(self.card.get("aruco_groups"), "v4 要在卡信息里标出「页序在 id 组里」")
        self.assertEqual([1], self.card.get("qr_pages"))

    def test_three_pages(self):
        self.assertEqual(3, self.card["n_pages"], "24 题（12 选 + 6 填 + 6 解）应是 3 页")

    def test_only_first_page_has_qr(self):
        qr_pages = [p["page"] for p in self.card["pages"] if p.get("qr")]
        self.assertEqual([1], qr_pages, "v4 只有第 1 页印 QR，实际 %s" % qr_pages)

    def test_marker_ids_encode_page_number(self):
        for p in self.card["pages"]:
            ids = sorted(a["id"] for a in p["marks"]["aruco"])
            want = sorted(cardmod.page_marker_ids(p["page"]).values())
            self.assertEqual(want, ids, "第 %d 页四角 id 应为 %s" % (p["page"], want))
            self.assertEqual(p["page"], cardmod.decode_page_from_ids(ids),
                             "从四角 id 应能反推页序")

    def test_decode_page_rejects_incomplete(self):
        self.assertIsNone(cardmod.decode_page_from_ids([0, 1, 2]), "缺一个角不能说它是某页")
        self.assertIsNone(cardmod.decode_page_from_ids([0, 1, 2, 4]), "不是同一页的四个 id")
        self.assertIsNone(cardmod.decode_page_from_ids([]))
        self.assertEqual(12, cardmod.decode_page_from_ids([44, 45, 46, 47]), "第 12 页（上限）")

    def test_marker_ids_cover_12_pages_without_repeat(self):
        seen = []
        for page in range(1, cardmod.ARUCO_MAX_PAGES + 1):
            seen.extend(cardmod.page_marker_ids(page).values())
        self.assertEqual(len(seen), len(set(seen)), "12 页的 id 不能重复")
        self.assertLess(max(seen), 50, "DICT_4X4_50 只有 50 个 id")

    def test_qr_shrunk_to_24mm(self):
        qr = self.card["pages"][0]["qr"]
        self.assertEqual(cardmod.mm(24), qr["size"], "QR 从 30mm 缩到 24mm")
        self.assertLess(qr["size"], cardmod.QR_SIZE_V3)

    def test_area_drops_at_least_half(self):
        """QR+ArUco 墨迹面积：v3（每页 QR30+4×18mm）→ v4（首页 QR24 + 每页 4×14mm）。"""

        def v3_area(n_pages):
            return n_pages * (4 * cardmod.ARUCO_SIZE_V3 ** 2 + cardmod.QR_SIZE_V3 ** 2)

        def v4_area(card):
            total = 0
            for p in card["pages"]:
                total += sum(a["size"] ** 2 for a in p["marks"]["aruco"])
                if p.get("qr"):
                    total += p["qr"]["size"] ** 2
            return total

        old = v3_area(self.card["n_pages"])
        new = v4_area(self.card)
        drop = 1.0 - new / float(old)
        self.assertGreaterEqual(drop, 0.5,
                                "面积只降了 %.1f%%（v3 %d → v4 %d）" % (drop * 100, old, new))

    def test_qr_count_and_no_overlap(self):
        """二维码数量 3 → 1；4 角标记仍不被任何内容压住。"""
        self.assertEqual(1, sum(1 for p in self.card["pages"] if p.get("qr")))
        for p in self.card["pages"]:
            marks = [(a["x"], a["y"], a["size"]) for a in p["marks"]["aruco"]]
            for (mx, my, msize) in marks:
                self.assertGreaterEqual(mx, 0)
                self.assertGreaterEqual(my, 0)
                self.assertLessEqual(mx + msize, PAGE_W)
                self.assertLessEqual(my + msize, PAGE_H)
            for label, x, y, w, h in rects_of_page(p):
                if label.startswith("aruco"):
                    continue
                for (mx, my, msize) in marks:
                    hit = x < mx + msize and mx < x + w and y < my + msize and my < y + h
                    self.assertFalse(hit, "第 %d 页 %s 压住了四角标记" % (p["page"], label))


class TestPlanFromAnchor(unittest.TestCase):
    """t46：只给标识块位置 → 反推作答格（两列排布也要成立）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="anchor_")
        items = [{"idx": i, "no": "%05d" % (1000 + i), "qtype": "选择",
                  "options": ["A", "B", "C", "D"],
                  "answer_key": [{"blank": 1, "value": "A"}]} for i in range(1, 5)]
        items += [{"idx": 5, "no": "00005", "qtype": "填空",
                   "answer_key": [{"blank": 1, "value": "1"}, {"blank": 2, "value": "2"}]}]
        m = make_manifest(items, pid="P20261031-901")
        m["paper"] = {"pages": 1}
        cls.res = build_card(m, cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_reverse_geometry_matches_plan_item(self):
        page = self.res["card"]["pages"][0]
        cols = set()
        for it in page["items"]:
            b = it["id_box"]
            cols.add(0 if b["x"] < cardmod.PAGE_W // 2 else 1)
            if it["kind"] == cardmod.KIND_OMR:
                n_blank = 1
                geom = cardmod.plan_from_anchor(b["x"], b["y"], cardmod.KIND_OMR,
                                                n_options=4, n_blanks=n_blank)
                self.assertEqual([(c["x"], c["y"], c["w"], c["h"]) for c in it["cells"]],
                                 [(c["x"], c["y"], c["w"], c["h"]) for c in geom["cells"]],
                                 "题 %d 选择格反推不一致" % it["idx"])
            else:
                geom = cardmod.plan_from_anchor(b["x"], b["y"], cardmod.KIND_FILL,
                                                n_options=0, n_blanks=2)
                self.assertEqual([(x["x"], x["y"], x["w"], x["h"]) for x in it["boxes"]],
                                 [(x["x"], x["y"], x["w"], x["h"]) for x in geom["boxes"]],
                                 "题 %d 填空框反推不一致" % it["idx"])
        self.assertEqual({0, 1}, cols, "测试用例必须同时覆盖左右两列")

    def test_id_bits_match_no(self):
        for page in self.res["card"]["pages"]:
            for it in page["items"]:
                self.assertEqual(cardmod.encode_id_bits(it["no"]), it["id_bits"])
                self.assertEqual(str(it["no"]), cardmod.decode_id_bits(it["id_bits"]))


class TestCompactLayout(unittest.TestCase):
    """t38 省纸两列版式：选择/填空两列、解答过程区 360、24 题 ≤3 页。"""

    @staticmethod
    def make(n_ch, n_fill, n_sol, fb=2, sb=2, pid=None):
        items, i = [], 0
        for _ in range(n_ch):
            i += 1
            items.append({"idx": i, "no": "%05d" % i, "qtype": "选择",
                          "options": ["A", "B", "C", "D"],
                          "answer_key": [{"blank": 1, "value": "A"}]})
        for _ in range(n_fill):
            i += 1
            items.append({"idx": i, "no": "%05d" % i, "qtype": "填空",
                          "answer_key": [{"blank": b + 1, "value": "1"} for b in range(fb)]})
        for _ in range(n_sol):
            i += 1
            items.append({"idx": i, "no": "%05d" % i, "qtype": "解答",
                          "answer_key": [{"blank": b + 1, "value": "x"} for b in range(sb)]})
        m = make_manifest(items, pid=pid or "P20261031-%03d" % (n_ch + n_fill + n_sol))
        m["paper"] = {"pages": 2}
        return m

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="compact_")
        cls.c24 = build_card(cls.make(10, 8, 6), os.path.join(cls.tmp, "c24"))
        cls.c10 = build_card(cls.make(5, 3, 2), os.path.join(cls.tmp, "c10"))
        cls.c50 = build_card(cls.make(20, 15, 15), os.path.join(cls.tmp, "c50"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_page_targets(self):
        self.assertLessEqual(self.c24["card"]["n_pages"], 3,
                             "24 题（10/8/6）必须 ≤3 页，实际 %d" % self.c24["card"]["n_pages"])
        self.assertLessEqual(self.c10["card"]["n_pages"], 2,
                             "10 题必须 ≤2 页，实际 %d" % self.c10["card"]["n_pages"])
        self.assertLessEqual(self.c50["card"]["n_pages"], 6,
                             "50 题必须 ≤6 页，实际 %d" % self.c50["card"]["n_pages"])

    def test_two_columns_are_used(self):
        """选择/填空两列：同一页里既有左半页的题，也有右半页的题。"""
        page = self.c24["card"]["pages"][0]
        xs = [it["id_box"]["x"] for it in page["items"] if it["kind"] in (KIND_OMR, KIND_FILL)]
        left = [x for x in xs if x < PAGE_W // 2 - 200]
        right = [x for x in xs if x > PAGE_W // 2 - 200]
        self.assertTrue(left and right, "两列版式应有左右两列：xs=%s" % xs)
        self.assertLess(min(right) - max(left), PAGE_W // 2, "两列不该跨列重叠")

    def test_geometry_in_page_and_no_overlap(self):
        for res in (self.c24, self.c10, self.c50):
            for page in res["card"]["pages"]:
                rects = rects_of_page(page)
                for label, x, y, w, h in rects:
                    self.assertGreaterEqual(x, 0, label)
                    self.assertGreaterEqual(y, 0, label)
                    self.assertLessEqual(x + w, PAGE_W, "%s 右越界" % label)
                    self.assertLessEqual(y + h, PAGE_H, "%s 下越界" % label)
                for i in range(len(rects)):
                    for j in range(i + 1, len(rects)):
                        la, ax, ay, aw, ah = rects[i]
                        lb, bx, by, bw, bh = rects[j]
                        hit = ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
                        self.assertFalse(hit, "%s 与 %s 重叠" % (la, lb))

    def test_cell_count_reconciles(self):
        """iter_answer_cells 的格数 == card 几何里非选择作答格数；选择格恒 4/题。"""
        for res in (self.c24, self.c10, self.c50):
            omr_cells = sum(len(it.get("cells") or [])
                            for p in res["card"]["pages"] for it in p["items"]
                            if it["kind"] == KIND_OMR)
            non_choice = sum(len(it.get("boxes") or it.get("answer_boxes") or [])
                             for p in res["card"]["pages"] for it in p["items"]
                             if it["kind"] != KIND_OMR)
            enumerated = list(cardmod.iter_answer_cells(res["card"]))
            self.assertEqual(non_choice, len(enumerated), "作空格数对不上")
            self.assertEqual(0, omr_cells % 4, "选择格必须 4 的倍数")
            self.assertEqual(4 * (omr_cells // 4), omr_cells)
            for c in enumerated:
                self.assertIn("box", c)
                self.assertIn("idx", c)

    def test_aruco_and_qr_still_decodable(self):
        """每页四角 ArUco 可解（v4：id = 4p..4p+3）；只有第 1 页有 QR。"""
        import cv2
        det = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
            cv2.aruco.DetectorParameters())
        for png, page in zip(self.c24["png"], self.c24["card"]["pages"]):
            arr = cv2.imread(png)
            _, ids, _ = det.detectMarkers(arr)
            self.assertIsNotNone(ids, "第 %d 页检不到 ArUco" % page["page"])
            want = sorted(cardmod.page_marker_ids(page["page"]).values())
            self.assertEqual(want, sorted(int(v) for v in ids.ravel()),
                             "第 %d 页四角 id 应是 %s" % (page["page"], want))
            payload = cardmod.read_qr(png, page.get("qr"))
            if page["page"] == 1:
                self.assertIsNotNone(payload, "第 1 页 QR 解不出来")
                self.assertEqual(1, payload["p"])
            else:
                self.assertIsNone(page.get("qr"), "v4 第 %d 页不该再印 QR" % page["page"])
                self.assertIsNone(payload)

    def test_solution_process_height_360(self):
        hs = [it["process_box"]["h"]
              for p in self.c24["card"]["pages"] for it in p["items"]
              if it["kind"] == KIND_SOLUTION]
        self.assertTrue(hs)
        self.assertEqual(360, max(hs), "过程区标称高必须 360px（空间不足时才压缩）")

    def test_answer_boxes_still_800(self):
        ws = [b["w"] for p in self.c24["card"]["pages"] for it in p["items"]
              for b in (it.get("answer_boxes") or [])]
        self.assertTrue(ws)
        self.assertEqual(800, max(ws), "最终答案框保持 800×118")

    def test_footer_text_unchanged(self):
        page = self.c24["card"]["pages"][0]
        self.assertEqual(cardmod.page_footer_text(page["page"], page["label"] and
                                                  self.c24["card"]["n_pages"] or 0, page["label"]),
                         "答题卡 · 第 1 页 / 共 %d 页 ·（卷面续页 P%d）"
                         % (self.c24["card"]["n_pages"], page["label"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
