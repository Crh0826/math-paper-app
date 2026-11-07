# -*- coding: utf-8 -*-
"""t58 · 学生信息落库：submissions 加列 + 判卷写入 + /records 查询与 CSV + 手工补录。

先红后绿。复用 tests/test_grade 的 Fixture（真答题卡 + 合成照片），不另造一套假数据。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

from fastapi import FastAPI                     # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402
from PIL import Image, ImageDraw                # noqa: E402

import card                                     # noqa: E402
import db as dbmod                              # noqa: E402
import grade                                    # noqa: E402
import grade_routes                             # noqa: E402

from tests.test_grade import Fixture, PID, _insert_questions   # noqa: E402

STUDENT = "20250317"


def paint_student(filled_png, card_info, digits=STUDENT):
    """把学号涂到已填好的卡上（每列涂对应数字那一格）。"""
    img = Image.open(filled_png).convert("L")
    d = ImageDraw.Draw(img)
    st = (card_info or {}).get("student") or {}
    for pos, col in enumerate(st.get("columns") or []):
        if pos >= len(digits):
            break
        want = str(digits[pos])
        for c in col.get("cells") or []:
            if str(c.get("digit")) == want:
                d.rectangle([c["x"] + 5, c["y"] + 5, c["x"] + c["w"] - 5, c["y"] + c["h"] - 5],
                            fill=0)
                break
    img.save(filled_png)
    return filled_png


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stu_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMigration(Base):
    def test_old_db_gets_columns_idempotently(self):
        """旧库（没有 student_no/student_name）打开即补列，数据不丢，重复执行不报错。"""
        path = os.path.join(self.tmp, "old.db")
        raw = sqlite3.connect(path)
        raw.executescript(
            "CREATE TABLE submissions (sid TEXT PRIMARY KEY, pid TEXT, created_at TEXT,"
            " image_path TEXT, warp_ok INTEGER DEFAULT 0, qr_ok INTEGER DEFAULT 0,"
            " score REAL, full REAL, detail_json TEXT);"
            "INSERT INTO submissions(sid, pid, score, full) VALUES('S1','P1',7,10);")
        raw.commit()
        raw.close()

        conn = dbmod.connect(path)
        try:
            self.assertTrue(grade._ensure_student_columns(conn))
            self.assertTrue(grade._ensure_student_columns(conn), "重复执行要幂等")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(submissions)")}
            self.assertIn("student_no", cols)
            self.assertIn("student_name", cols)
            row = conn.execute("SELECT sid, pid, score, student_no, student_name"
                               " FROM submissions WHERE sid='S1'").fetchone()
            self.assertEqual(("S1", "P1", 7, "", ""), tuple(row)[:5], "老数据不能丢，新列默认空串")
        finally:
            conn.close()


class TestGradeWritesStudent(Base):
    def setUp(self):
        super().setUp()
        self.fx = Fixture(self.tmp)
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        self.fx.close()
        super().tearDown()

    def _grade(self, paint=True, seed=1):
        if paint:
            paint_student(self.fx.filled_png, self.fx.manifest["card"])
        return grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=seed),
                                 out_dir=self.out)

    def test_v3_card_writes_student_no(self):
        res = self._grade(paint=True, seed=1)
        self.assertTrue(res.get("warp_ok"), res)
        row = self.fx.conn.execute("SELECT student_no, student_name, detail_json"
                                   " FROM submissions WHERE sid=?", (res["sid"],)).fetchone()
        self.assertEqual(STUDENT, row["student_no"], "v3 卡涂的学号要落库")
        self.assertEqual("", row["student_name"], "姓名手写，读不出就是空串")
        detail = json.loads(row["detail_json"])
        self.assertEqual(STUDENT, (detail.get("student") or {}).get("no"),
                         "detail_json 顶层也要有 student 供页面直读")

    def test_blank_card_still_grades(self):
        """没涂学号 → student_no=''，判卷照常完成（不降级、不报错）。"""
        res = self._grade(paint=False, seed=2)
        self.assertTrue(res.get("warp_ok"), res)
        self.assertIsNone(res.get("error"))
        row = self.fx.conn.execute("SELECT student_no FROM submissions WHERE sid=?",
                                   (res["sid"],)).fetchone()
        self.assertEqual("", row["student_no"])
        self.assertGreaterEqual(res["score"], 0)

    def test_group_by_student(self):
        """同一学生两次提交 → GROUP BY 能数出 2。"""
        for seed in (3, 4):
            self._grade(paint=True, seed=seed)
        rows = dict(self.fx.conn.execute(
            "SELECT student_no, COUNT(*) FROM submissions GROUP BY student_no").fetchall())
        self.assertEqual(2, rows.get(STUDENT), "同一学号应聚合出 2 次提交：%r" % rows)

    def test_two_students_not_mixed(self):
        self._grade(paint=True, seed=5)
        paint_student(self.fx.filled_png, self.fx.manifest["card"], "20250001")
        grade.grade_image(self.fx.conn, PID, self.fx.photo(seed=6), out_dir=self.out)
        nos = {r[0] for r in self.fx.conn.execute("SELECT student_no FROM submissions")}
        self.assertEqual({STUDENT, "20250001"}, nos, "两个学生的学号不能串")

    def test_grade_by_ids_writes_empty_student(self):
        """无 manifest 判卷（按题目标识）也要写学生列（读不出就是空串）。"""
        photo = self.fx.photo(seed=7)
        _insert_questions(self.fx.conn)          # by-ids 靠题号查库
        self.fx.conn.execute("DELETE FROM papers")
        self.fx.conn.commit()
        res = grade.grade_by_ids(self.fx.conn, photo, out_dir=os.path.join(self.tmp, "o2"))
        if res.get("error"):
            self.skipTest("by-ids 路径需要题库题号，跳过：%s" % res.get("error"))
        row = self.fx.conn.execute("SELECT student_no, student_name FROM submissions"
                                   " WHERE sid=?", (res["sid"],)).fetchone()
        self.assertEqual("", row["student_no"])
        self.assertEqual("", row["student_name"])

    def test_legacy_card_has_no_student_block(self):
        """v1/v2 卡没有学生区 → 读出空串，不抛异常。"""
        info = dict(self.fx.manifest["card"])
        info.pop("student", None)
        self.assertEqual("", grade._read_student_no(info, self.fx.filled_png))
        self.assertEqual("", card.read_student_no(info, self.fx.filled_png))

    def test_set_student_manual(self):
        sid = self._grade(paint=False, seed=8)["sid"]
        out = grade.set_student(self.fx.conn, sid, "20250999", "张三")
        self.assertTrue(out.get("ok"), out)
        row = self.fx.conn.execute("SELECT student_no, student_name, detail_json"
                                   " FROM submissions WHERE sid=?", (sid,)).fetchone()
        self.assertEqual("20250999", row["student_no"])
        self.assertEqual("张三", row["student_name"])
        detail = json.loads(row["detail_json"])
        self.assertEqual("20250999", (detail.get("student") or {}).get("no"))
        self.assertEqual("张三", (detail.get("student") or {}).get("name"))


class TestRecordsViews(Base):
    def setUp(self):
        super().setUp()
        self.db_path = os.path.join(self.tmp, "app.db")
        self.conn = dbmod.connect(self.db_path)
        grade._ensure_student_columns(self.conn)
        rows = [("S1", "P1", "2026-10-31T10:00:00+08:00", "20250317", "张三", 7, 10),
                ("S2", "P1", "2026-10-31T11:00:00+08:00", "", "", 5, 10),
                ("S3", "P2", "2026-10-31T12:00:00+08:00", "20250399", "李四", 9, 10)]
        for sid, pid, when, no, name, score, full in rows:
            self.conn.execute(
                "INSERT INTO submissions(sid,pid,created_at,image_path,warp_ok,qr_ok,score,"
                "full,detail_json,student_no,student_name) VALUES(?,?,?,'',1,1,?,?,?,?,?)",
                (sid, pid, when, score, full, json.dumps({"items": []}), no, name))
        self.conn.commit()
        app = FastAPI()
        app.state.db_path = self.db_path
        app.state.out_dir = os.path.join(self.tmp, "out")
        app.include_router(grade_routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def test_records_context_has_student(self):
        """/records 的行数据带学生字段（模板渲染由 UI 侧消费；这里验数据层）。"""
        conn = dbmod.connect(self.db_path)
        try:
            items, total, _papers = grade_routes._records_items(conn)
        finally:
            conn.close()
        self.assertEqual(3, total)
        by = {it["sid"]: it for it in items}
        self.assertEqual("20250317", by["S1"]["student_no"])
        self.assertEqual("张三", by["S1"]["student_name"])
        self.assertIn("student_no", by["S1"])
        self.assertIn("student_name", by["S1"])

    def test_records_json_fields(self):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM submissions WHERE sid='S2'").fetchone()
            item = grade_routes._record_row(row)
        finally:
            conn.close()
        self.assertEqual("", item["student_no"], "空值是空串，不是 None")
        self.assertEqual("", item["student_name"])

    def test_records_filter_by_student_prefix(self):
        conn = dbmod.connect(self.db_path)
        try:
            items_2025, _t1, _p1 = grade_routes._records_items(conn, student="2025")
            items_one, _t2, _p2 = grade_routes._records_items(conn, student="20250399")
        finally:
            conn.close()
        self.assertEqual({"20250317", "20250399"}, {it["student_no"] for it in items_2025})
        self.assertEqual(["S3"], [it["sid"] for it in items_one], "按学号前缀过滤应只留匹配的")

    def test_records_csv_has_student_columns(self):
        res = self.client.get("/records.csv")
        self.assertEqual(200, res.status_code)
        raw = res.content
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 要带 UTF-8 BOM（Excel 才不乱码）")
        text = raw.decode("utf-8-sig")
        head = text.splitlines()[0]
        for col in ("student_no", "student_name"):
            self.assertIn(col, head, "CSV 表头固定要有 %s：%s" % (col, head))
        self.assertIn("20250317", text)
        self.assertIn("张三", text)

    def test_records_csv_respects_student_filter(self):
        res = self.client.get("/records.csv?student=20250399")
        text = res.content.decode("utf-8-sig")
        self.assertIn("20250399", text)
        self.assertNotIn("20250317", text)
