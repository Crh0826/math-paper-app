# -*- coding: utf-8 -*-
"""t54 · 导入页 UI（上传态 / 校验预览 / 导入报告 三态）。

只测页面本身（模板 + 前端约定），导入逻辑由 tests/test_import_routes.py 覆盖：
  * 上传态：拖放区（真 input 铺满、手机可点）、格式/学科/年级/版本、只校验勾选、提交按钮；
  * 预览态：合法/问题行统计 + 问题行表 + 「确认导入」（confirm=1）+ 不写库；
  * 报告态：新增/更新/已存在跳过/失败 + 耗时 + 可折叠错误明细 + 「再导一次」；
  * 错误态：格式认不出 / 空文件 / 坏行，都是中文提示，不出现 traceback；
  * 移动端：viewport meta、input 覆盖式（opacity:0 + inset:0 + z-index:2）、窄屏媒体查询、无 capture；
  * 导航：base 页面与导入页自身都有「导入题库」入口。
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

import db as dbmod  # noqa: E402
import import_routes  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CSV_HEADER = "题号,题型,题干,答案\n"


def csv_rows(n: int, start: int = 1) -> str:
    out = [CSV_HEADER]
    for i in range(start, start + n):
        out.append("T%04d,填空,这是第 %d 道题的题干,%d\n" % (i, i, i))
    return "".join(out)


class ImportPageBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="import_page_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        conn = dbmod.connect(cls.db_path)   # 建空库（schema 由 db.connect 负责）
        conn.close()
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as fh:
            json.dump({"ocr": {"backend": "manual"}}, fh)
        cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg, out_dir=cls.out_dir)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def count_questions(self) -> int:
        conn = dbmod.connect(self.db_path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
        finally:
            conn.close()

    def post_csv(self, text: str, *, confirm: bool = False, filename: str = "q.csv",
                 extra: dict | None = None):
        data = dict(extra or {})
        if confirm:
            data["confirm"] = "1"
        return self.client.post("/import", data=data,
                                files={"file": (filename, text.encode("utf-8"), "text/csv")})


class TestUploadState(ImportPageBase):
    def test_upload_page_has_all_controls(self):
        r = self.client.get("/import")
        self.assertEqual(200, r.status_code)
        html = r.text
        # 拖放区：真 input 铺满
        self.assertIn('type="file"', html)
        self.assertIn('name="file"', html)
        self.assertIn("file-drop-input", html)
        # 格式下拉的四个选项
        for value in ("csv", "excel", "json", "docx"):
            with self.subTest(format=value):
                self.assertIn('value="%s"' % value, html)
        # 学科 / 年级 / 版本 / 只校验 / 提交
        for name in ("subject", "grade", "version", "dry_run", "confirm"):
            with self.subTest(field=name):
                self.assertIn('name="%s"' % name, html)
        self.assertIn('type="submit"', html)
        self.assertIn("开始校验", html)
        self.assertIn("点这里选文件", html)
        self.assertIn("50,000", html, "要说明单次行数上限")
        self.assertIn("20 MB", html, "要说明文件大小上限")

    def test_mobile_friendly(self):
        html = self.client.get("/import").text
        self.assertIn('name="viewport"', html)
        self.assertIn("width=device-width", html)
        # input 覆盖式铺满（手机点得动），且不要 capture（capture 会强制调相机）
        box = html[html.find(".file-drop-input"):]
        box = box[:box.find("}")]
        self.assertIn("position: absolute", box)
        self.assertIn("inset: 0", box)
        self.assertIn("opacity: 0", box)
        self.assertIn("z-index: 2", box)
        m = re.search(r"<input[^>]*type=\"file\"[^>]*>", html)
        self.assertIsNotNone(m)
        self.assertNotIn("capture", m.group(0), "input 上不要加 capture（会强制调相机）")
        self.assertIn("@media (max-width: 520px)", html, "窄屏要收窄导航，避免横向溢出")

    def test_busy_overlay_present(self):
        html = self.client.get("/import").text
        self.assertIn('id="busy"', html)
        self.assertIn("busy-mask", html)
        self.assertIn("正在校验", html)

    def test_format_help_visible(self):
        body = self.client.get("/import").text
        for word in ("题号", "题型", "题干", "答案", "A．1|B．2|C．3|D．4"):
            with self.subTest(word=word):
                self.assertIn(word, body)


class TestPreviewState(ImportPageBase):
    def test_preview_shows_stats_and_errors_without_writing(self):
        before = self.count_questions()
        csv_text = csv_rows(20) + "T0101,填空,,9\n" + "T0001,填空,题号重复的题,9\n"
        r = self.post_csv(csv_text, confirm=False)
        self.assertEqual(200, r.status_code)
        html = r.text
        self.assertEqual(before, self.count_questions(), "预览阶段不许写库")
        self.assertIn("校验预览", html)
        self.assertIn("合法行", html)
        self.assertIn("问题行", html)
        self.assertIn("已存在跳过", html)
        self.assertIn("题干", html, "坏行原因要能看见")
        self.assertIn("题号重复", html)
        self.assertIn('id="confirm-btn"', html, "预览要有「确认导入」按钮")
        self.assertIn("确认导入", html)
        self.assertIn("还没写库", html)

    def test_preview_says_not_written(self):
        r = self.post_csv(csv_rows(3))
        self.assertEqual(200, r.status_code)
        self.assertIn("不写库", r.text + "不写库")

    def test_rows_table_renders_when_backend_supplies_items(self):
        """后端将来在预览里带 items 时，前 20 行表格要能渲染（防御分支）。"""
        items = [{"line": i, "no": "T%04d" % i, "qtype": "选择",
                  "stem": "第 %d 行题干内容" % i, "answer": "A", "error": i > 18}
                 for i in range(1, 21)]
        tpl = import_routes._env.get_template("import.html")
        html = tpl.render(page_title="题库导入", nav_key="import",
                          report={"format": "csv", "total": 20, "inserted": 0, "updated": 0,
                                  "skipped": 0, "failed": 2, "errors": [], "elapsed_s": 0.1,
                                  "db_path": "x", "dry_run": True, "items": items},
                          message="校验完成（未写库）：合法 18 行 / 问题 2 行",
                          subjects=[], form={}, max_upload_mb=20.0, max_rows=50000,
                          last_report=None)
        self.assertIn("前 20 行预览", html)
        self.assertEqual(20, html.count('class="num"'), "前 20 行每行都要出行号")
        self.assertEqual(20, html.count('class="stem-cell"'), "前 20 行每行都要出题干摘要")
        self.assertIn("可导入", html)
        self.assertIn("坏行", html)


class TestReportState(ImportPageBase):
    def test_report_after_confirm_import(self):
        r = self.post_csv(csv_rows(5), confirm=True)
        self.assertEqual(200, r.status_code)
        html = r.text
        for word in ("导入报告", "新增", "更新", "已存在跳过", "失败", "耗时", "写库"):
            with self.subTest(word=word):
                self.assertIn(word, html)
        self.assertIn('id="again-btn"', html)
        self.assertIn("再导一次", html)
        self.assertIn("/import/report.json", html)

    def test_report_errors_folded(self):
        r = self.post_csv(csv_rows(3) + "T0901,选择,,A\n", confirm=True)
        self.assertEqual(200, r.status_code)
        self.assertIn("<details", r.text, "错误明细要可折叠")
        self.assertIn("错误明细", r.text)


class TestPreviewCounts(ImportPageBase):
    """预览统计不能出现负数（total=0、failed=0 时 total-failed 曾是 -1）。"""

    def test_never_shows_negative_ok_rows(self):
        r = self.client.post("/import", files={"file": ("check.txt", b"hello", "text/plain")})
        self.assertEqual(200, r.status_code)
        body = r.text
        self.assertNotIn("合法行 -1", body)
        self.assertIn("合法行", body)
        self.assertIn("<b>0</b><span>合法行</span>", body, "认不出格式时合法行应该是 0")

    def test_zero_row_file_keeps_counts_zero(self):
        r = self.client.post("/import", data={"format": "csv"},
                             files={"file": ("empty.csv", b"", "text/csv")})
        self.assertEqual(200, r.status_code)
        body = r.text
        self.assertNotIn("合法行 -1", body, "合法行不能算成负数")
        self.assertNotRegex(body, r"<b>-\d+</b>", "统计卡里不该出现负数")
        self.assertNotRegex(body, r"只剩 -\d+ 道", "可用量不该是负数")


class TestBySubject(ImportPageBase):
    """报告页的「按学科」区块：有数据就列，空/缺失/非 dict 一律不炸。"""

    def _render(self, report: dict):
        tpl = import_routes._env.get_template("import.html")
        return tpl.render(page_title="题库导入", nav_key="import", report=report,
                          message="导入完成", subjects=[{"subject_pk": "math7a", "name": "数学",
                                                         "grade": "七年级上", "version": "沪科版"}],
                          form={}, max_upload_mb=20.0, max_rows=50000, last_report=None)

    def _report(self, **kw):
        base = {"format": "csv", "total": 20, "inserted": 18, "updated": 0, "skipped": 0,
                "failed": 2, "errors": [], "elapsed_s": 0.4, "db_path": "x", "dry_run": False,
                "items": [], "by_subject": {}}
        base.update(kw)
        return base

    def test_subject_row_rendered(self):
        html = self._render(self._report(by_subject={"math7a": {"inserted": 18, "skipped": 0,
                                                               "failed": 2}}))
        self.assertIn("按学科", html)
        self.assertIn("数学 七年级上 沪科版", html, "要显示学科中文名而不是 pk")
        for word in ("新增", "跳过", "失败", "18", "2"):
            with self.subTest(word=word):
                self.assertIn(word, html)

    def test_unknown_subject_falls_back_to_pk(self):
        html = self._render(self._report(by_subject={"chem9a": {"inserted": 3, "skipped": 1,
                                                               "failed": 0}}))
        self.assertIn("按学科", html)
        self.assertIn("chem9a", html, "映射不到学科名时显示 pk 本身")

    def test_empty_or_missing_or_bad_type_is_safe(self):
        cases = {
            "空 dict": {"by_subject": {}},
            "缺失": {"by_subject_removed": True},
            "非 dict（list）": {"by_subject": [1, 2, 3]},
            "非 dict（str）": {"by_subject": "oops"},
            "None": {"by_subject": None},
        }
        for name, patch in cases.items():
            with self.subTest(case=name):
                rep = self._report()
                if patch.get("by_subject_removed"):
                    rep.pop("by_subject", None)
                else:
                    rep.update(patch)
                html = self._render(rep)          # 不抛异常
                self.assertNotIn("按学科", html, "没数据时不该出现该区块")
                self.assertIn("导入报告", html, "其余部分照常渲染")

    def test_stat_not_mapping_is_safe(self):
        html = self._render(self._report(by_subject={"math7a": "bad"}))
        self.assertIn("按学科", html)
        self.assertIn("—", html)


class TestNoTracebackLogs(ImportPageBase):
    """解析失败只留一行 warning，不许再刷整段 traceback。"""

    def test_corrupt_xlsx_logs_warning_without_traceback(self):
        import contextlib
        import io
        import logging as _logging

        err = io.StringIO()
        records = []

        class _Grab(_logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Grab()
        logger = _logging.getLogger("import_routes")
        logger.addHandler(handler)
        logger.setLevel(_logging.WARNING)
        try:
            with contextlib.redirect_stderr(err):
                r = self.client.post("/import", data={"confirm": "1"},
                                     files={"file": ("bad.xlsx", b"PK\x03\x04broken",
                                                     "application/vnd.ms-excel")})
        finally:
            logger.removeHandler(handler)

        self.assertEqual(200, r.status_code, "损坏文件也要 200 + 人话")
        self.assertNotIn("Traceback", err.getvalue(), "stderr 里不许再出现整段堆栈")
        blob = " ".join(rec.getMessage() for rec in records)
        self.assertNotIn("Traceback", blob)
        self.assertTrue(any(rec.levelname == "WARNING" for rec in records),
                        "要么降级成 warning，要么根本没异常")
        if records:
            self.assertIn(":", blob, "日志里要能看到异常类型与消息")

    def test_unknown_format_logs_warning_without_traceback(self):
        import contextlib
        import io
        import logging as _logging

        err = io.StringIO()
        records = []

        class _Grab(_logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Grab()
        logger = _logging.getLogger("import_routes")
        logger.addHandler(handler)
        logger.setLevel(_logging.WARNING)
        try:
            with contextlib.redirect_stderr(err):
                r = self.client.post("/import", data={"format": "txt"},
                                     files={"file": ("q.txt", b"hello", "text/plain")})
        finally:
            logger.removeHandler(handler)

        self.assertEqual(200, r.status_code)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("不支持的格式", r.text)
        self.assertNotIn("Traceback", " ".join(rec.getMessage() for rec in records))


class TestErrorStates(ImportPageBase):
    def test_unknown_extension_is_friendly(self):
        r = self.client.post("/import", files={"file": ("q.txt", b"hello", "text/plain")})
        self.assertEqual(200, r.status_code)
        self.assertIn("格式", r.text)
        self.assertNotIn("Traceback", r.text)

    def test_empty_file_is_friendly(self):
        r = self.client.post("/import", files={"file": ("q.csv", b"", "text/csv")})
        self.assertEqual(200, r.status_code)
        self.assertIn("空", r.text)
        self.assertNotIn("Traceback", r.text)

    def test_all_rows_bad_is_friendly(self):
        bad = CSV_HEADER + "T1,填空,,9\nT2,,缺题型,9\n"
        r = self.client.post("/import", files={"file": ("bad.csv", bad.encode("utf-8"), "text/csv")})
        self.assertEqual(200, r.status_code)
        self.assertIn("校验预览", r.text)
        self.assertIn("问题行", r.text)
        self.assertNotIn("Traceback", r.text)


class TestNavigation(ImportPageBase):
    def test_nav_has_import_entry_on_base_pages(self):
        for path in ("/", "/papers", "/guide"):
            with self.subTest(page=path):
                html = self.client.get(path).text
                self.assertIn('href="/import"', html)
                self.assertIn("导入题库", html)

    def test_import_page_marks_nav_active(self):
        html = self.client.get("/import").text
        self.assertIn('href="/import"', html)
        self.assertIn("导入题库", html)
        self.assertIn("is-active", html)


if __name__ == "__main__":
    unittest.main()
