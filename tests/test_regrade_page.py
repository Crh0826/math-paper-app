# -*- coding: utf-8 -*-
"""t56 · 改卷页面重构（两列布局 + 保存反馈 + 改卷记录时间线 + 撤销）。

夹具直接复用 test_grade_routes 的（真答题卡 → 当照片），保证和判卷链路一致。
两种跑法都要能用：

    cd E:\\OCR\\app && python -m unittest tests.test_regrade_page
    cd E:\\OCR     && python -m unittest app.tests.test_regrade_page
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
import test_grade_routes as T           # noqa: E402  同目录夹具：_Base / build_photo / make_manifest


class TestRegradePage(T._Base):
    """页面侧：改卷记录、保存反馈、撤销、导出、移动端、原有能力不降级。"""

    # ------------------------------------------------------------------ 工具
    def _page(self, sid, **params):
        return self.client.get("/submissions/%s" % sid, params=params)

    def _save(self, sid, data, follow=True):
        return self.client.post("/submissions/%s/confirm" % sid, data=data,
                                follow_redirects=follow)

    def _edit_one(self, sid, idx=4, blank=1, verdict="correct", text="4"):
        """改一个空的判定（第 4 题第 1 空默认是待识别的填空）→ 回页面。"""
        return self._save(sid, {"verdict_%s_%s" % (idx, blank): verdict,
                                "text_%s_%s" % (idx, blank): text})

    # ------------------------------------------------------------------ 1. 记录区渲染
    def test_edits_section_renders_after_save(self):
        sid = self.sid_of()
        self._edit_one(sid)
        html = self._page(sid).text
        self.assertIn("改卷记录", html)
        self.assertIn("撤销这批", html, "每条记录旁要有撤销按钮")
        self.assertIn("批次", html, "要显示批次号")
        self.assertIn("→", html, "要显示 改前→改后")
        self.assertIn("4-1", html, "要显示题号-空")
        self.assertIn("teacher", html, "要显示操作者")
        self.assertIn("/submissions/%s/edits.csv" % sid, html, "要有导出改卷记录 CSV 入口")
        self.assertIn("timeline", html, "时间线容器")

    # ------------------------------------------------------------------ 2. 空态
    def test_edits_empty_state(self):
        sid = self.sid_of()          # 没改过
        html = self._page(sid).text
        self.assertIn("这次判卷还没有人工改判", html)

    # ------------------------------------------------------------------ 3. 保存反馈（成功）
    def test_save_feedback_success(self):
        sid = self.sid_of()
        res = self._save(sid, {"verdict_4_1": "correct", "text_4_1": "4"}, follow=False)
        self.assertEqual(303, res.status_code)
        loc = res.headers["location"]
        self.assertIn("saved=1", loc, "要带 ?saved=N 回结果页（实际 %s）" % loc)
        self.assertIn("batch=", loc, "要带批次号回结果页")
        html = self.client.get(loc).text
        self.assertIn("已保存", html)
        self.assertIn("1 项", html, "要写清保存了几项")
        self.assertIn("批次", html)

    # ------------------------------------------------------------------ 4. 无改动不提交
    def test_no_change_reports_not_submitted(self):
        sid = self.sid_of()
        detail = self.detail(sid)
        # 原值原样回传（第 1 题本来就是判对 A）
        row = [u for u in detail["items"] if u["idx"] == 1][0]
        res = self._save(sid, {"verdict_1_1": row["verdict"],
                               "text_1_1": str(row.get("picked") or "")}, follow=False)
        self.assertEqual(303, res.status_code)
        loc = res.headers["location"]
        self.assertIn("saved=0", loc, "无改动要明确 saved=0（实际 %s）" % loc)
        html = self.client.get(loc).text
        self.assertIn("没有改动，未提交", html)

    # ------------------------------------------------------------------ 5. 撤销整批
    def test_undo_batch_restores_score_and_leaves_record(self):
        sid = self.sid_of()
        before = self.score(sid)[0]
        self._edit_one(sid)
        after = self.score(sid)[0]
        self.assertNotEqual(before, after, "改判后分数应该变")
        html = self._page(sid).text
        batch = [b for b in html.split("/edits/")[1:]]
        self.assertTrue(batch, "页面要有撤销表单")
        batch_id = batch[0].split("/undo")[0]
        res = self.client.post("/submissions/%s/edits/%s/undo" % (sid, batch_id),
                               follow_redirects=False)
        self.assertEqual(303, res.status_code)
        self.assertIn("undone=", res.headers["location"])
        page = self.client.get(res.headers["location"]).text
        self.assertIn("已撤销", page)
        self.assertEqual(before, self.score(sid)[0], "撤销后分数回到改前")
        self.assertIn("撤销", page, "撤销本身要留一条记录")

    # ------------------------------------------------------------------ 6. 导出入口
    def test_export_edits_csv(self):
        sid = self.sid_of()
        self._edit_one(sid)
        self.assertIn("/submissions/%s/edits.csv" % sid, self._page(sid).text)
        res = self.client.get("/submissions/%s/edits.csv" % sid)
        self.assertEqual(200, res.status_code)
        self.assertIn("text/csv", res.headers["content-type"])
        self.assertTrue(res.content.startswith(b"\xef\xbb\xbf"), "CSV 要带 BOM")

    # ------------------------------------------------------------------ 7. 两列 / 移动端
    def test_two_column_layout_mobile_first(self):
        sid = self.sid_of()
        html = self._page(sid).text
        self.assertIn("regrade-grid", html, "两列布局容器")
        self.assertIn('class="regrade-side"', html, "左列=批注图")
        self.assertIn('class="regrade-main"', html, "右列=逐题")
        self.assertIn("annotated.png", html)
        self.assertIn("@media (max-width: 900px)", html, "窄屏单列")
        self.assertIn('name="viewport"', html)
        self.assertIn("overflow-wrap", html, "375px 不许横向溢出")

    # ------------------------------------------------------------------ 8. 原有能力不降级
    def test_existing_capabilities_kept(self):
        sid = self.sid_of()
        html = self._page(sid).text
        for hook in ("为什么", "item/4/why", "crop/", "判对", "判错", "待确认",
                     "verdict_4_1", "text_4_1", "row-4-1", "score-chip", "pending-chip"):
            self.assertIn(hook, html, "结果页少了原有能力：%s" % hook)

    def test_per_unit_confirm_still_scores(self):
        sid = self.sid_of()
        before = self.score(sid)[0]
        self._edit_one(sid, idx=4, blank=1, verdict="correct", text="4")
        self.assertGreater(self.score(sid)[0], before, "逐空改判仍要能加分")

    # ------------------------------------------------------------------ 9. 保存中…（前端幂等）
    def test_submit_button_guards_double_click(self):
        sid = self.sid_of()
        html = self._page(sid).text
        self.assertIn("保存中", html, "提交时要显示「保存中…」")
        self.assertIn("disabled", html, "提交后按钮要禁用，防双击")


if __name__ == "__main__":
    unittest.main(verbosity=2)
