# -*- coding: utf-8 -*-
"""T55 改卷留痕：grading_edits 表 + 确认时写入 + 查询/导出/撤销。

断言口径（与任务验收一一对应）：
1. 建表幂等；旧库（没有这张表）打开时自动补建，原数据不丢
2. apply_confirm 只记**真正变化**的项；一次提交共用一个 batch_id；operator 缺省 'teacher'
3. list_edits 时间倒序；结果页上下文里有 edits
4. undo_batch 整批回滚（恢复改前判定/文本 + 重算 score），并写 source='undo' 的留痕
5. 同一 batch_id 重复提交不产生重复行（UNIQUE(batch_id, idx, blank) 兜底）
6. CSV 导出带 UTF-8 BOM，列齐
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db as dbmod  # noqa: E402
import grade  # noqa: E402
import grade_routes  # noqa: E402

PID = "P20261031-955"


def units_fixture():
    """两条多空题 + 一条选择题：够覆盖整题改判与逐空改判。"""
    return [
        {"idx": 1, "no": "00001", "qtype": "选择", "kind": "omr", "blank": 1,
         "picked": None, "expected": "B", "verdict": "unsure", "reason": "涂黑不唯一"},
        {"idx": 2, "no": "00002", "qtype": "填空", "kind": "fill", "blank": 1,
         "picked": "4", "expected": "4", "verdict": "correct", "reason": "一致"},
        {"idx": 2, "no": "00002", "qtype": "填空", "kind": "fill", "blank": 2,
         "picked": "1/2", "expected": "1/2", "verdict": "correct", "reason": "一致"},
    ]


def make_submission(conn, sid, units=None, score=0, full=3):
    units = units_fixture() if units is None else units
    detail = {"version": 1, "pid": PID, "sid": sid, "items": units,
              "score": score, "full": full}
    conn.execute(
        "INSERT OR REPLACE INTO submissions"
        "(sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, PID, "2026-10-31T15:00:00+08:00", "", 1, 1, score, full,
         json.dumps(detail, ensure_ascii=False)))
    conn.commit()
    return detail


def load_units(conn, sid):
    row = conn.execute("SELECT detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()
    return json.loads(row["detail_json"])["items"]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ge_")
        self.db_path = os.path.join(self.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = dbmod.connect(self.db_path)
        self.sid = "S20261031-150000-ge0001"
        make_submission(self.conn, self.sid)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)


class EditsTableTest(_Base):
    def test_ensure_table_is_idempotent(self):
        grade._ensure_edits_table(self.conn)
        grade._ensure_edits_table(self.conn)          # 再来一次不许报错
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(grading_edits)")}
        for need in ("sid", "idx", "blank", "qtype", "before_verdict", "after_verdict",
                     "before_text", "after_text", "operator", "batch_id", "source",
                     "note", "created_at"):
            self.assertIn(need, cols, "grading_edits 缺列：%s" % need)

    def test_old_db_gets_table_without_losing_data(self):
        # 模拟旧库：把表删掉，再走一次「打开即建表」
        self.conn.execute("DROP TABLE IF EXISTS grading_edits")
        self.conn.commit()
        grade._ensure_edits_table(self.conn)
        rows = self.conn.execute("SELECT COUNT(*) c FROM submissions").fetchone()["c"]
        self.assertEqual(rows, 1, "补建表不许动 submissions")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM grading_edits")
                         .fetchone()["c"], 0)


class ApplyConfirmEditsTest(_Base):
    def test_only_real_changes_are_recorded(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        edits = grade.list_edits(self.conn, self.sid)
        self.assertEqual(len(edits), 1, "值没变不该产生第二条留痕：%r" % edits)
        self.assertEqual((edits[0]["before_verdict"], edits[0]["after_verdict"]),
                         ("unsure", "correct"))
        self.assertEqual(edits[0]["idx"], 1)

    def test_unchanged_submit_writes_nothing(self):
        # 原样回传（verdict 与 text 都没变）→ 一条都不写
        # 注意用逐空键：整题键会把同一题另一个空也盖成同一答案（那是真改动）
        grade.apply_confirm(self.conn, self.sid, {"2-1": {"verdict": "correct", "text": "4"}})
        self.assertEqual(grade.list_edits(self.conn, self.sid), [])

    def test_one_batch_covers_all_changes_of_one_submit(self):
        res = grade.apply_confirm(self.conn, self.sid, {
            "2-1": {"verdict": "wrong", "text": "5"},
            "2-2": {"verdict": "wrong", "text": "2/3"},
        })
        self.assertIsNone(res.get("error"))
        edits = grade.list_edits(self.conn, self.sid)
        self.assertEqual(len(edits), 2)
        self.assertEqual(len({e["batch_id"] for e in edits}), 1, "同一次提交必须共用一个 batch_id")
        self.assertEqual({e["source"] for e in edits}, {"confirm"})

    def test_operator_defaults_to_teacher(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        self.assertEqual(grade.list_edits(self.conn, self.sid)[0]["operator"], "teacher")

    def test_operator_is_recorded(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}},
                            operator="王老师")
        self.assertEqual(grade.list_edits(self.conn, self.sid)[0]["operator"], "王老师")

    def test_same_batch_twice_is_idempotent(self):
        batch = "b-dupe"
        for _ in range(2):      # 双击提交
            grade.apply_confirm(self.conn, self.sid,
                                {1: {"verdict": "correct", "text": "B"}},
                                batch_id=batch)
        rows = self.conn.execute("SELECT COUNT(*) c FROM grading_edits WHERE batch_id=?",
                                 (batch,)).fetchone()["c"]
        self.assertEqual(rows, 1, "UNIQUE(batch_id, idx, blank) 应该挡住重复行")

    def test_text_change_alone_is_recorded(self):
        grade.apply_confirm(self.conn, self.sid,
                            {"2-1": {"verdict": "correct", "text": "4.0"}})
        edits = grade.list_edits(self.conn, self.sid)
        self.assertEqual(len(edits), 1)
        self.assertEqual((edits[0]["before_text"], edits[0]["after_text"]), ("4", "4.0"))


class UndoTest(_Base):
    def test_undo_restores_values_and_rescores(self):
        grade.apply_confirm(self.conn, self.sid, {
            "2-1": {"verdict": "wrong", "text": "5"},
            "2-2": {"verdict": "wrong", "text": "2/3"},
        })
        batch = grade.list_edits(self.conn, self.sid)[0]["batch_id"]
        res = grade.undo_batch(self.conn, self.sid, batch)
        self.assertIsNone(res.get("error"), res)
        self.assertEqual(res["restored"], 2)
        units = {u["blank"]: u for u in load_units(self.conn, self.sid) if u["idx"] == 2}
        self.assertEqual((units[1]["verdict"], units[1]["picked"]), ("correct", "4"))
        self.assertEqual((units[2]["verdict"], units[2]["picked"]), ("correct", "1/2"))
        self.assertEqual(res["score"], 2, "回滚后要按改前状态重算分数")

    def test_undo_writes_its_own_trace(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        batch = grade.list_edits(self.conn, self.sid)[0]["batch_id"]
        grade.undo_batch(self.conn, self.sid, batch)
        edits = grade.list_edits(self.conn, self.sid)
        self.assertEqual(len(edits), 2)
        undo_rows = [e for e in edits if e["source"] == "undo"]
        self.assertEqual(len(undo_rows), 1, "撤销本身也要留痕")
        self.assertIn(batch, undo_rows[0]["note"] or "")
        self.assertEqual(undo_rows[0]["after_verdict"], "unsure")

    def test_undo_twice_is_rejected(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        batch = grade.list_edits(self.conn, self.sid)[0]["batch_id"]
        self.assertIsNone(grade.undo_batch(self.conn, self.sid, batch).get("error"))
        second = grade.undo_batch(self.conn, self.sid, batch)
        self.assertEqual(second.get("error"), "already_undone")

    def test_undo_unknown_batch_reports_error(self):
        self.assertEqual(grade.undo_batch(self.conn, self.sid, "nope").get("error"),
                         "batch_not_found")


class CsvTest(_Base):
    def test_export_csv_has_bom_header_and_rows(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}},
                            operator="李老师")
        text = grade.export_edits_csv(self.conn, self.sid)
        self.assertTrue(text.startswith("\ufeff"), "CSV 要带 UTF-8 BOM（Excel 打开不乱码）")
        head = text.lstrip("\ufeff").splitlines()[0]
        for col in ("时间", "题号", "空", "改前", "改后", "操作者", "批次"):
            self.assertIn(col, head, "CSV 表头缺列：%s" % col)
        body = text.lstrip("\ufeff").splitlines()[1]
        self.assertIn("李老师", body)
        self.assertIn("unsure", body)
        self.assertIn("correct", body)

    def test_export_csv_empty_has_header_only(self):
        text = grade.export_edits_csv(self.conn, self.sid)
        self.assertTrue(text.startswith("\ufeff"))
        self.assertEqual(len(text.lstrip("\ufeff").strip().splitlines()), 1)


class RouteTest(_Base):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(grade_routes.router)
        app.state.db_path = self.db_path
        app.state.out_dir = os.path.join(self.tmp, "out")
        app.state.recognizer = None
        app.state.auto_ocr = False
        self.client = TestClient(app)

    def test_confirm_route_writes_edits_and_operator(self):
        res = self.client.post("/submissions/%s/confirm" % self.sid,
                               data={"verdict_1_1": "correct", "text_1_1": "B",
                                     "operator": "张老师"},
                               follow_redirects=False)
        self.assertIn(res.status_code, (303, 200))
        edits = grade.list_edits(self.conn, self.sid)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["operator"], "张老师")

    def test_get_edits_json(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        res = self.client.get("/submissions/%s/edits" % self.sid)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("sid"), self.sid)
        edits = data.get("edits") or []
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["idx"], 1)
        self.assertEqual(edits[0]["after_verdict"], "correct")

    def test_get_edits_csv(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        res = self.client.get("/submissions/%s/edits.csv" % self.sid)
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("content-type", ""))
        self.assertTrue(res.content.startswith(b"\xef\xbb\xbf"), "导出的 CSV 要带 BOM")
        self.assertIn("correct", res.content.decode("utf-8-sig"))

    def test_undo_route_redirects_back(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        batch = grade.list_edits(self.conn, self.sid)[0]["batch_id"]
        res = self.client.post("/submissions/%s/edits/%s/undo" % (self.sid, batch),
                               follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/submissions/%s" % self.sid, res.headers.get("location", ""))

    def test_result_page_context_has_edits(self):
        grade.apply_confirm(self.conn, self.sid, {1: {"verdict": "correct", "text": "B"}})
        seen = {}

        def fake_render(name, status_code=200, **ctx):
            seen.update(ctx)
            from fastapi.responses import HTMLResponse
            return HTMLResponse("<html>ok</html>")

        orig = grade_routes.render
        grade_routes.render = fake_render
        try:
            res = self.client.get("/submissions/%s" % self.sid)
        finally:
            grade_routes.render = orig
        self.assertEqual(res.status_code, 200)
        self.assertIn("edits", seen, "结果页上下文必须带 edits（t56 页面要用）")
        self.assertEqual(len(seen["edits"]), 1)


if __name__ == "__main__":
    unittest.main()
