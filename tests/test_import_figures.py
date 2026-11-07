# -*- coding: utf-8 -*-
"""t88 · 带图导入：ZIP（题目 + images/）→ 图片落盘 → 卷面真出图。

守的事（每条都对应验收里的一句话）：
  * ZIP 一次导完（题目 + images/）；也能「先导题目、再单独传 images.zip」；
  * 图片落盘到 ``data/figures/<subject_pk>/<no>/<seq>.<ext>``，重复导入不重复写；
  * 报告给：图片 N 张 / 未匹配 M 个（带文件名与原因）/ 仍缺图 K 道（带题号）；
  * **「如图」题补上图之后必须真的能出卷**：picker 不再排除，且卷面 PDF 里真有一张图；
  * 没图时行为不变：仍被 fig_missing 排除；
  * 容错：非图片 / 坏 zip / 超大图 / 多图序号 / 没有 images 目录 → 中文原因、不 500。

跑法：``cd E:\\OCR\\app && python -m unittest tests.test_import_figures -v``
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

from PIL import Image, ImageDraw  # noqa: E402

import db as dbmod  # noqa: E402
import importers.core as imp  # noqa: E402
import paper  # noqa: E402
import picker  # noqa: E402

SUBJECT = {"name": "物理", "grade": "八年级上", "version": "人教版"}
SUBJECT_PK = "physics8a"


def qno(n: int) -> str:
    """99xxx：picker 要求题号是整数；同时避开真实 qbank（00001..87xxx）的 docx。

    paper 找原始 docx 是按 ``<题号>.docx`` 直查的，撞上就会优先用 docx（保真优先），
    测试里用 99xxx 段可以保证走 tokens_json + figures 这条链路。
    """
    return "99%03d" % int(n)


def make_png(label="FIG", size=(360, 220)) -> bytes:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    d.rectangle([8, 8, size[0] - 8, size[1] - 8], outline=(0, 0, 0), width=5)
    d.line([30, size[1] - 40, size[0] // 2, 40, size[0] - 30, size[1] - 60],
           fill=(0, 0, 0), width=4)
    d.text((size[0] // 3, size[1] // 2), label, fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def question(no, stem, answer="3", qtype="填空"):
    return {"no": no, "qtype": qtype, "stem": stem, "answer": answer}


class Base(unittest.TestCase):
    """每个用例一套独立 库 + 图片目录 + 产物目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t88_")
        self.db = os.path.join(self.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        dbmod.connect(self.db).close()                     # 只建表，不塞真实题
        self.fig_root = os.path.join(os.path.dirname(self.db), "figures")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out, exist_ok=True)
        paper.set_tokens_db_path(self.db)
        paper.set_figures_root(self.fig_root)
        paper.clear_tokens_cache()

    def tearDown(self):
        paper.set_tokens_db_path(None)
        paper.set_figures_root(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 工具
    def import_rows(self, rows, dry_run=False):
        src = os.path.join(self.tmp, "items.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"items": rows}, fh, ensure_ascii=False)
        return imp.import_file(src, "json", db_path=self.db,
                               report_path=os.path.join(self.tmp, "report.json"),
                               subject=SUBJECT, dry_run=dry_run)

    def make_zip(self, name="pack.zip", rows=None, images=None, images_dir="images/"):
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as zf:
            if rows is not None:
                zf.writestr("items.json", json.dumps({"items": rows}, ensure_ascii=False))
            if images:
                for fn, data in images.items():
                    zf.writestr(images_dir + fn, data)
        return path

    def q(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def media_count(self, no):
        rows = self.q("SELECT media_count FROM questions WHERE no=?", (no,))
        return rows[0]["media_count"] if rows else None

    def figure_names(self, no):
        rows = self.q("SELECT tokens_json FROM questions WHERE no=?", (no,))
        if not rows:
            return []
        return [t["name"] for t in json.loads(rows[0]["tokens_json"] or "[]")
                if t.get("kind") == "embed"]


class TestZipWithImages(Base):
    def test_zip_items_and_images_in_one_go(self):
        a, b = qno(4), qno(5)
        pack = self.make_zip(rows=[question(a, "如图，物块受到的摩擦力是____N。"),
                                   question(b, "如图所示的电路，电流为____A。")],
                             images={"%s.png" % a: make_png("A"),
                                     "%s_1.png" % b: make_png("B1"),
                                     "%s_2.png" % b: make_png("B2")})
        rep = imp.import_zip(pack, "zip", db_path=self.db, subject=SUBJECT,
                             report_path=os.path.join(self.tmp, "r.json"))
        self.assertEqual(2, rep["new"] + rep["updated"])
        self.assertEqual(3, rep["figures_saved"], "三张图都要落盘")
        self.assertEqual(0, rep["figures_unmatched_count"])

        self.assertTrue(os.path.isfile(os.path.join(self.fig_root, SUBJECT_PK, a, a + ".png")),
                        "%s.png 要按原名落成 <subject>/<no>/%s.png" % (a, a))
        self.assertTrue(os.path.isfile(os.path.join(self.fig_root, SUBJECT_PK, b, b + "_2.png")))
        self.assertEqual(1, self.media_count(a))
        self.assertEqual(2, self.media_count(b))
        self.assertTrue(self.figure_names(a)[0].startswith(SUBJECT_PK + "/" + a + "/"))

    def test_two_step_import_then_images_zip(self):
        """先导题目，再单独传 images.zip（没有题目文件）也要能挂图。"""
        no = qno(10)
        self.import_rows([question(no, "如图，求重力____N。")])
        pack = self.make_zip("images.zip", rows=None,
                             images={"%s.png" % no: make_png("IMG")})
        rep = imp.import_zip(pack, "zip", db_path=self.db, subject=SUBJECT)
        self.assertFalse(rep["has_item_file"])
        self.assertEqual(1, rep["figures_saved"])
        self.assertEqual(1, self.media_count(no))

    def test_multi_images_ordered_by_seq(self):
        no = qno(11)
        self.import_rows([question(no, "如图甲、图乙，分别求____。")])
        rep = imp.attach_figures(self.db, SUBJECT_PK,
                                 [("%s_3.png" % no, make_png("3")),
                                  ("%s_1.png" % no, make_png("1")),
                                  ("%s_2.png" % no, make_png("2"))], root=self.fig_root)
        self.assertEqual(3, rep["saved"])
        names = self.figure_names(no)
        self.assertTrue(names[0].endswith("/%s.png" % no))
        self.assertTrue(names[1].endswith("/%s_2.png" % no))
        self.assertTrue(names[2].endswith("/%s_3.png" % no), "多图必须按 seq 排序：%s" % names)
        self.assertEqual(3, self.media_count(no))

    def test_idempotent_no_duplicate_files(self):
        no = qno(12)
        pack = self.make_zip("p.zip", rows=[question(no, "如图，求____N。")],
                             images={"%s.png" % no: make_png()})
        imp.import_zip(pack, "zip", db_path=self.db, subject=SUBJECT)
        imp.import_zip(pack, "zip", db_path=self.db, subject=SUBJECT)
        folder = os.path.join(self.fig_root, SUBJECT_PK, no)
        self.assertTrue(os.path.isdir(folder), "第一次导入就该落盘：%s" % folder)
        self.assertEqual(["%s.png" % no], sorted(os.listdir(folder)), "重复导入不能多出文件")
        self.assertEqual(1, self.media_count(no))

    def test_unmatched_files_listed(self):
        no = qno(13)
        self.import_rows([question(no, "如图，求____N。")])
        rep = imp.attach_figures(self.db, SUBJECT_PK,
                                 [("%s.png" % no, make_png()),
                                  ("readme.txt", b"not an image"),
                                  ("99999.png", make_png()),
                                  ("bad_name.png", make_png())], root=self.fig_root)
        files = {u["file"] for u in rep["unmatched"]}
        self.assertIn("readme.txt", files, "非图片要列出来")
        self.assertIn("99999.png", files, "题库里没有的题号要列出来")
        self.assertEqual(3, rep["unmatched_count"])
        self.assertTrue(all(u["reason"] for u in rep["unmatched"]), "未匹配要带原因")
        self.assertEqual(1, rep["saved"])


class TestPickingAndRendering(Base):
    """核心验收：补图之后 picker 不再排除，且卷面 PDF 真的有图。"""

    def _params(self, n_fill=2):
        return {"title": "x", "mix": {"填空": n_fill},
                "scope": {"subject_pk": SUBJECT_PK, "subjects": [SUBJECT_PK],
                          "chapters": [], "kps": [], "kp_mode": "any"},
                "requirements": {"need_answer": True, "exclude_empty": True}, "seed": 1}

    def _available(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return picker.pool_report(conn, self._params())["available"]["填空"]
        finally:
            conn.close()

    def test_fig_missing_excludes_then_allows_after_images(self):
        fig, plain = qno(20), qno(21)
        self.import_rows([question(fig, "如图，摩擦力为____N。"),
                          question(plain, "直接计算：1+1=____。")])
        self.assertEqual(1, self._available(), "没图时「如图」题应被 fig_missing 排除")

        imp.attach_figures(self.db, SUBJECT_PK,
                           [("%s.png" % fig, make_png())], root=self.fig_root)
        self.assertEqual(2, self._available(), "补图后 fig_missing 不再排除，两题都能出")
        self.assertEqual(1, self.media_count(fig))

    def test_pdf_really_has_figure(self):
        no = qno(30)
        self.import_rows([question(no, "如图，物块受到的摩擦力为____N。")])
        imp.attach_figures(self.db, SUBJECT_PK,
                           [("%s.png" % no, make_png("FIG30"))], root=self.fig_root)
        self.assertEqual(1, self.media_count(no))

        manifest = {"pid": "P20261107-901", "title": "带图验收卷",
                    "created_at": "2026-11-07T13:00:00+08:00",
                    "items": [{"idx": 1, "no": no, "qtype": "填空", "kp_id": None,
                               "answer_key": [{"blank": 1, "value": "3"}]}]}
        res = paper.build_docx(manifest, self.out, name="paper")
        docx = res["docx"]
        with zipfile.ZipFile(docx) as zf:
            media = [n for n in zf.namelist() if n.startswith("word/media/")]
        self.assertTrue(media, "docx 里必须真有图片二进制：%s" % media)

        try:
            import pymupdf
            import render
        except ImportError:                                   # pragma: no cover
            self.fail("缺 render/pymupdf，无法验证卷面 PDF")
        pdf = os.path.join(self.out, "paper.pdf")
        try:
            render.docx_to_pdf(docx, pdf)
        except Exception as exc:                              # noqa: BLE001
            self.fail("卷面 PDF 转换失败（%s: %s）—— 验收要求真 PDF"
                      % (type(exc).__name__, exc))
        self.assertTrue(os.path.isfile(pdf))
        doc = pymupdf.open(pdf)
        try:
            self.assertGreaterEqual(doc.page_count, 1, "PDF 至少 1 页")
            images = sum(len(pg.get_images(full=True)) for pg in doc)
            self.assertGreaterEqual(images, 1, "卷面 PDF 里必须能看到图（实际 %d 张）" % images)
            png = os.path.join(self.out, "page1.png")
            doc[0].get_pixmap(dpi=110).save(png)
            self.assertGreater(os.path.getsize(png), 10000)
        finally:
            doc.close()

    def test_no_image_still_excluded(self):
        """没补图的「如图」题行为不变：仍被 fig_missing 排除。"""
        no = qno(40)
        self.import_rows([question(no, "如图所示的装置，求____N。")])
        self.assertEqual(0, self._available(), "没图就不能出卷")
        row = self.q("SELECT media_count, text_plain FROM questions WHERE no=?", (no,))[0]
        self.assertEqual(0, row["media_count"])
        self.assertIn("如图", row["text_plain"])


class TestIntegrationFixes(Base):
    """t92：落盘名 = token 名、学科透传、Web 端到端（PDF 里真要数得到图）。"""

    def test_disk_name_matches_token_name(self):
        no = qno(70)
        self.import_rows([question(no, "如图，求____N。")])
        imp.attach_figures(self.db, SUBJECT_PK,
                           [("%s.png" % no, make_png("a")),
                            ("%s_2.png" % no, make_png("b"))], root=self.fig_root)
        names = self.figure_names(no)
        self.assertEqual(["%s/%s/%s.png" % (SUBJECT_PK, no, no),
                          "%s/%s/%s_2.png" % (SUBJECT_PK, no, no)], names,
                         "tokens 里的 name 必须是 99005.png / 99005_2.png 这种原名")
        for rel in names:
            path = os.path.join(self.fig_root, *rel.split("/"))
            self.assertTrue(os.path.isfile(path), "token 说的文件必须真在：%s" % path)

    def test_subject_pk_passed_through(self):
        """只给 subject_pk（不带 name/grade）也要落到 physics，不能落到 math7a。"""
        no = qno(71)
        pack = self.make_zip("p71.zip", rows=[question(no, "如图，求____N。")],
                             images={"%s.png" % no: make_png()})
        rep = imp.import_zip(pack, "zip", db_path=self.db,
                             subject={"subject_pk": "physics"})
        self.assertEqual(1, rep["figures_saved"])
        self.assertTrue(os.path.isfile(os.path.join(self.fig_root, "physics", no,
                                                    "%s.png" % no)),
                        "图必须在 data/figures/physics/<no>/ 下")
        self.assertFalse(os.path.exists(os.path.join(self.fig_root, "math7a", no)),
                         "不能落到 math7a")
        row = self.q("SELECT subject_pk FROM questions WHERE no=?", (no,))[0]
        self.assertEqual("physics", row["subject_pk"])

    def test_web_end_to_end_pdf_has_images(self):
        """真 Web：POST /import(ZIP) → POST /papers → paper.pdf 里数图。"""
        try:
            from fastapi.testclient import TestClient
            import pymupdf
            import server
        except ImportError:                                   # pragma: no cover
            self.fail("缺 fastapi/pymupdf/server，无法做 Web 端到端")
        cfg = os.path.join(self.tmp, "cfg.json")
        app = server.create_app(db_path=self.db, config_path=cfg, out_dir=self.out)
        client = TestClient(app, follow_redirects=False)

        nos = [qno(80), qno(81), qno(82)]
        pack = self.make_zip("web.zip",
                             rows=[question(n, "如图，第 %d 题的受力为____N。" % i)
                                   for i, n in enumerate(nos, 1)],
                             images={"%s.png" % n: make_png(n) for n in nos})
        with open(pack, "rb") as fh:
            r = client.post("/import", data={"format": "zip", "subject_pk": "physics",
                                             "subject": "物理", "grade": "八年级上",
                                             "version": "人教版", "confirm": "1"},
                            files={"file": ("web.zip", fh.read(), "application/zip")})
        self.assertEqual(200, r.status_code, "ZIP 导入应 200")
        rep = client.get("/import/report.json").json()
        pk = rep.get("subject_pk") or SUBJECT_PK       # 真实学科（可能是 physics8a）
        self.assertGreaterEqual(int(rep.get("figures_saved") or 0), len(nos))

        r2 = client.post("/papers", data={"title": "t92 端到端", "seed": "7",
                                          "n_choice": "0", "n_fill": str(len(nos)),
                                          "n_solution": "0", "subject_pk": pk,
                                          "layout": "compact", "need_answer": "on"})
        self.assertEqual(302, r2.status_code, "组卷应 302（细节：%s）" % r2.text[:200])
        pid = (r2.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
        prev = os.path.join(self.tmp, "out", pid, "preview.json")
        if not os.path.exists(prev):
            prev = os.path.join(self.out, pid, "preview.json")
        self.assertTrue(os.path.exists(prev), "要有 preview.json：%s" % prev)
        with open(prev, encoding="utf-8") as fh:
            pv = json.load(fh)
        self.assertFalse(pv.get("degraded"), "卷面不能是降级状态（degraded=true 说明只出了答题卡）")
        pdf = os.path.join(os.path.dirname(prev), "paper.pdf")
        self.assertTrue(os.path.isfile(pdf), "必须真的出 paper.pdf：%s" % pdf)
        doc = pymupdf.open(pdf)
        try:
            images = sum(len(pg.get_images(full=True)) for pg in doc)
        finally:
            doc.close()
        self.assertGreaterEqual(images, len(nos),
                                "PDF 里的图数 %d 应 ≥ 带图题数 %d" % (images, len(nos)))


class TestTolerance(Base):
    def test_bad_zip_gives_chinese_reason(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as fh:
            fh.write(b"this is not a zip")
        with self.assertRaises(ValueError) as ctx:
            imp.import_zip(bad, "zip", db_path=self.db, subject=SUBJECT)
        self.assertIn("ZIP", str(ctx.exception))
        self.assertIn("损坏", str(ctx.exception))

    def test_zip_without_images_dir_is_ok(self):
        pack = self.make_zip(rows=[question(qno(50), "普通题，没有图。")], images=None)
        rep = imp.import_zip(pack, "zip", db_path=self.db, subject=SUBJECT)
        self.assertEqual(1, rep["new"] + rep["updated"])
        self.assertEqual(0, rep["figures_saved"])

    def test_oversized_image_is_resized(self):
        import random

        no = qno(60)
        self.import_rows([question(no, "如图，求____N。")])
        rng = random.Random(0)
        big = Image.new("RGB", (3600, 2400))
        px = big.load()
        for y in range(0, 2400, 4):                 # 噪声让 PNG 压不小（真·大图）
            for x in range(0, 3600, 4):
                v = rng.randrange(0, 256)
                for dy in range(4):
                    for dx in range(4):
                        px[x + dx, y + dy] = (v, (v * 7) % 256, (v * 13) % 256)
        buf = io.BytesIO()
        big.save(buf, format="PNG")
        data = buf.getvalue()
        self.assertGreater(len(data), 1024 * 1024, "测试图要够大才像「超大图」")

        rep = imp.attach_figures(self.db, SUBJECT_PK,
                                 [("%s.png" % no, data)], root=self.fig_root)
        self.assertEqual(1, rep["saved"])
        path = os.path.join(self.fig_root, SUBJECT_PK, no, "%s.png" % no)
        with Image.open(path) as im:
            self.assertLessEqual(max(im.size), imp.FIG_MAX_SIDE,
                                 "超大图要缩到长边 %d（实际 %s）" % (imp.FIG_MAX_SIDE, im.size))


if __name__ == "__main__":
    unittest.main()
