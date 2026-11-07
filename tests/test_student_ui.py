# -*- coding: utf-8 -*-
"""t60 · 学生信息上页面（结果页显示/补录 + 记录页显示/过滤 + CSV 列）。

夹具复用 test_grade_routes（真答题卡当照片）。
两种跑法都支持：

    cd E:\\OCR\\app && python -m unittest tests.test_student_ui
    cd E:\\OCR     && python -m unittest app.tests.test_student_ui
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
for _p in (APP_DIR, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import db as dbmod                      # noqa: E402
import grade                            # noqa: E402
import test_grade_routes as T           # noqa: E402


class TestStudentUi(T._Base):
    """学生信息在页面上的显示、补录与过滤。"""

    NO = "20250317"
    NAME = "张三"

    # ------------------------------------------------------------------ 工具
    def _set_student(self, sid, no=NO, name=NAME):
        conn = dbmod.connect(self.db_path)
        try:
            grade.set_student(conn, sid, no, name)
        finally:
            conn.close()

    def _student_of(self, sid):
        conn = dbmod.connect(self.db_path)
        try:
            row = conn.execute("SELECT student_no, student_name FROM submissions WHERE sid=?",
                               (sid,)).fetchone()
        finally:
            conn.close()
        return (row["student_no"] or "", row["student_name"] or "")

    # ------------------------------------------------------------------ 1. 结果页显示学号
    def test_result_page_shows_student(self):
        sid = self.sid_of()
        self._set_student(sid)
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("学生", html)
        self.assertIn(self.NO, html, "学号要显示出来")
        self.assertIn(self.NAME, html, "姓名要显示出来")
        # 与卷号/提交号同区（同一个 kv 卡里）
        marker = 'class="kv score-kv"'
        self.assertIn(marker, html, "结果页要有卷号/提交号那块 kv 卡")
        head = html.split(marker)[1].split("</dl>")[0]
        self.assertIn(self.NO, head, "学生行要和卷号/提交号在同一块")

    # ------------------------------------------------------------------ 2. 未识别占位 + 补录入口
    def test_unread_student_placeholder_and_form(self):
        sid = self.sid_of()          # 夹具照片没涂学号 → student_no = ''
        html = self.client.get("/submissions/%s" % sid).text
        self.assertIn("学号未识别", html, "读不到学号要给占位文案")
        self.assertIn("补录", html, "要有补录入口")
        self.assertIn('action="student"', html, "补录表单要 POST 到 student")
        self.assertIn('name="student_no"', html)
        self.assertIn('name="student_name"', html)

    # ------------------------------------------------------------------ 3. 补录成功
    def test_set_student_saves_and_reports(self):
        sid = self.sid_of()
        res = self.client.post("/submissions/%s/student" % sid,
                               data={"student_no": "20250318", "student_name": "李四"},
                               follow_redirects=False)
        self.assertEqual(303, res.status_code, res.text[:400])
        loc = res.headers["location"]
        self.assertIn("/submissions/%s" % sid, loc)
        self.assertEqual(("20250318", "李四"), self._student_of(sid), "库里要存下来")
        page = self.client.get(loc).text
        self.assertIn("已保存", page, "回结果页要提示已保存")
        self.assertIn("20250318", page)
        self.assertIn("李四", page)

    # ------------------------------------------------------------------ 4. 记录页每行显示学生
    def test_records_page_shows_student_per_row(self):
        sid = self.sid_of()
        self._set_student(sid)
        html = self.client.get("/records").text
        self.assertIn(self.NO, html, "记录页每行要显示学号")
        self.assertIn(self.NAME, html, "记录页每行要显示姓名")

    # ------------------------------------------------------------------ 5. 按学号过滤
    def test_records_filter_by_student_prefix(self):
        sid = self.sid_of()
        self._set_student(sid, no="20250317", name="张三")
        other = "S20261031-190000-abcdef"
        self.insert_fake(other, T.PID, score=1, full=1)
        self._set_student(other, no="20260001", name="王五")
        hit = self.client.get("/records", params={"student": "202503"}).text
        self.assertIn(sid, hit)
        self.assertNotIn(other, hit, "?student= 只回匹配前缀的")
        self.assertIn("202503", hit, "抬头要显示当前筛选条件")
        self.assertIn("清除", hit, "要有清除筛选链接")

    # ------------------------------------------------------------------ 6. CSV 列
    def test_records_csv_has_student_columns(self):
        sid = self.sid_of()
        self._set_student(sid)
        res = self.client.get("/records.csv")
        self.assertEqual(200, res.status_code)
        self.assertTrue(res.content.startswith(b"\xef\xbb\xbf"), "CSV 要带 BOM")
        text = res.content.decode("utf-8-sig")
        head = text.splitlines()[0]
        self.assertIn("student_no", head)
        self.assertIn("student_name", head)
        self.assertIn(self.NO, text, "导出的行里要有学号")
        self.assertIn(self.NAME, text)
        cols = [c.strip() for c in head.split(",")]
        self.assertLess(cols.index("student_no"), cols.index("student_name"))
        self.assertGreater(cols.index("student_no"), cols.index("卷号"), "student 列放在卷号之后")

    # ------------------------------------------------------------------ 7. 涉及学生 N 人
    def test_records_student_count(self):
        sid = self.sid_of()
        self._set_student(sid, no="20250317", name="张三")
        other = "S20261031-190100-abcdef"
        self.insert_fake(other, T.PID, score=1, full=1)
        self._set_student(other, no="20250318", name="李四")
        same = "S20261031-190200-abcdef"
        self.insert_fake(same, T.PID, score=1, full=1)
        self._set_student(same, no="20250317", name="张三")     # 同一个学生第二份卷
        html = self.client.get("/records").text
        self.assertIn("涉及学生 2 人", html, "按 student_no 去重（实际页面 %s）"
                      % [ln for ln in html.splitlines() if "涉及学生" in ln][:2])

    # ------------------------------------------------------------------ 8. 移动端不溢出
    def test_records_mobile_hooks(self):
        sid = self.sid_of()
        self._set_student(sid)
        html = self.client.get("/records").text
        self.assertIn('data-layout="cards-mobile"', html)
        self.assertIn("rec-student", html, "学生行要有自己的类名，窄屏才好排")
        self.assertIn("overflow-wrap", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
