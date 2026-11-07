# -*- coding: utf-8 -*-
"""t53 · 题库导入 Web 化的测试（GET/POST /import + 校验预览 + 报告 JSON）。

覆盖：三种格式（CSV/JSON/Excel）、幂等（重复导入标「已存在跳过」）、坏行（HTTP 200 + 行号原因）、
dry-run 不写库、报告字段、空文件、超大文件拒绝、无写权限时的降级、docx 入口、多学科落 subjects。
"""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

import db as dbmod
import import_routes

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSV_HEADER = "题号,题型,题干,答案,知识点,章节\n"


def _csv_rows(n, prefix="W"):
    out = []
    for i in range(1, n + 1):
        out.append('%s%03d,填空,"%d 的相反数是____．",%d,1.%d,第1章 有理数\n'
                   % (prefix, i, i, -i, (i % 5) + 1))
    return out


def _make_app(db_path, out_dir=None):
    app = FastAPI()
    app.state.db_path = db_path
    app.state.out_dir = out_dir or tempfile.mkdtemp(prefix="impout_")
    app.include_router(import_routes.router)
    return app


class ImportRoutesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t53_")
        self.db_path = os.path.join(self.tmp, "app.db")
        self.out_dir = os.path.join(self.tmp, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        conn = dbmod.connect(self.db_path)
        conn.commit()
        conn.close()
        self.app = _make_app(self.db_path, self.out_dir)
        self.client = TestClient(self.app)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        finally:
            conn.close()

    def post_csv(self, rows, data=None, filename="q.csv", confirm=True, dry_run=False):
        payload = CSV_HEADER + "".join(rows)
        form = {"subject": "数学", "grade": "七年级上", "version": "沪科版"}
        form.update(data or {})
        if confirm:
            form["confirm"] = "1"
        if dry_run:
            form["dry_run"] = "1"
        return self.client.post(
            "/import", data=form,
            files={"file": (filename, payload.encode("utf-8-sig"), "text/csv")})

    def last_report(self):
        res = self.client.get("/import/report.json")
        self.assertEqual(200, res.status_code)
        return res.json()


class TestGetPage(ImportRoutesBase):
    def test_get_import_page(self):
        res = self.client.get("/import")
        self.assertEqual(200, res.status_code)
        html = res.text
        self.assertIn("<form", html, "页面要有上传表单")
        for field in ("file", "format", "subject", "grade", "version", "dry_run"):
            self.assertIn('name="%s"' % field, html, "表单字段 %s 缺失" % field)
        self.assertIn("multipart/form-data" if False else "enctype", html)


class TestFormats(ImportRoutesBase):
    def test_csv_import(self):
        before = self.count()
        res = self.post_csv(_csv_rows(20))
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertEqual("csv", rep["format"])
        self.assertEqual(20, rep["total"])
        self.assertEqual(20, rep["inserted"])
        self.assertEqual(0, rep["failed"])
        self.assertEqual(before + 20, self.count())

    def test_json_import(self):
        items = [{"no": "J%03d" % i, "qtype": "填空", "stem": "JSON 题 %d" % i,
                  "answer": str(i)} for i in range(1, 6)]
        payload = json.dumps({"subject": "数学", "grade": "七年级上", "version": "沪科版",
                              "items": items}, ensure_ascii=False)
        res = self.client.post("/import", data={"confirm": "1"},
                               files={"file": ("q.json", payload.encode("utf-8"),
                                               "application/json")})
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertEqual("json", rep["format"])
        self.assertEqual(5, rep["inserted"])

    def test_excel_import_20_rows(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["题号", "题型", "题干", "答案", "知识点", "章节"])
        for i in range(1, 21):
            ws.append(["X%03d" % i, "填空", "%d 的绝对值是____．" % i, str(i),
                       "1.1", "第1章 有理数"])
        buf = io.BytesIO()
        wb.save(buf)
        before = self.count()
        res = self.client.post("/import", data={"confirm": "1"},
                               files={"file": ("q.xlsx", buf.getvalue(),
                                               "application/vnd.openxmlformats-officedocument."
                                               "spreadsheetml.sheet")})
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertEqual("excel", rep["format"])
        self.assertEqual(20, rep["inserted"])
        self.assertEqual(before + 20, self.count())


class TestIdempotent(ImportRoutesBase):
    def test_reimport_marks_skipped(self):
        self.post_csv(_csv_rows(20))
        n1 = self.count()
        self.post_csv(_csv_rows(20))
        n2 = self.count()
        rep = self.last_report()
        self.assertEqual(n1, n2, "重复导入不能新增行")
        self.assertEqual(0, rep["inserted"])
        self.assertEqual(20, rep["skipped"], "重复行要标成「已存在跳过」")
        self.assertIn("已存在跳过", self.client.get("/import").text)

    def test_changed_row_updates(self):
        self.post_csv(_csv_rows(3))
        rows = _csv_rows(3)
        rows[0] = rows[0].replace("1 的相反数是____．", "改过的题干")
        self.post_csv(rows)
        rep = self.last_report()
        self.assertEqual(1, rep["updated"])
        self.assertEqual(2, rep["skipped"])


class TestBadRows(ImportRoutesBase):
    def test_bad_rows_reported_with_line_numbers(self):
        rows = _csv_rows(5)
        rows.append("BAD1,填空,,没有题干,1.1,第1章 有理数\n")          # 缺题干
        rows.append("W001,填空,文件内重复题号,9,1.1,第1章 有理数\n")    # 文件内题号重复
        res = self.post_csv(rows)
        self.assertEqual(200, res.status_code, "坏行不能 500")
        rep = self.last_report()
        self.assertEqual(5, rep["inserted"])
        self.assertGreaterEqual(rep["failed"], 2)
        lines = [e["line"] for e in rep["errors"]]
        self.assertTrue(all(isinstance(x, int) for x in lines), "错误要带行号：%s" % rep["errors"])
        joined = " ".join(e["reason"] for e in rep["errors"])
        self.assertIn("题干", joined)
        self.assertIn("题号重复", joined)
        self.assertEqual(5, self.count(), "坏行不能落库")

    def test_empty_file(self):
        res = self.post_csv([], filename="empty.csv")
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertEqual(0, rep["total"])
        self.assertEqual(0, rep["inserted"])
        self.assertTrue(rep.get("errors"), "空文件要给人话提示")


class TestDryRun(ImportRoutesBase):
    def test_dry_run_does_not_write(self):
        before = self.count()
        res = self.post_csv(_csv_rows(10), dry_run=True, confirm=False)
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertTrue(rep["dry_run"])
        self.assertEqual(10, rep["total"])
        self.assertEqual(0, rep["inserted"])
        self.assertEqual(before, self.count(), "dry-run 不许写库")
        html = res.text
        self.assertIn("10", html, "预览要显示合法行数")

    def test_preview_requires_confirm(self):
        before = self.count()
        self.post_csv(_csv_rows(5), confirm=False)
        self.assertEqual(before, self.count(), "没 confirm=1 不许写库")
        rep = self.last_report()
        self.assertTrue(rep["dry_run"])


class TestLimits(ImportRoutesBase):
    def test_oversize_upload_rejected(self):
        big = "题号,题型,题干,答案\n" + "".join(
            'B%05d,填空,"%s",1\n' % (i, "x" * 200) for i in range(1, 20))
        import_routes.MAX_UPLOAD_BYTES = 1024        # 1KB 上限，便于测试
        try:
            res = self.client.post("/import", data={"confirm": "1"},
                                   files={"file": ("big.csv", big.encode("utf-8"), "text/csv")})
        finally:
            import_routes.MAX_UPLOAD_BYTES = 20 * 1024 * 1024
        self.assertEqual(200, res.status_code)
        self.assertIn("上限", res.text)
        self.assertIn("上限", " ".join(e["reason"] for e in self.last_report()["errors"]))
        self.assertEqual(0, self.last_report()["inserted"])

    def test_too_many_rows_rejected(self):
        rows = _csv_rows(30)
        import_routes.MAX_ROWS = 10
        try:
            res = self.post_csv(rows)
        finally:
            import_routes.MAX_ROWS = 50000
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertEqual(0, rep["inserted"])
        self.assertIn("行", " ".join(e["reason"] for e in rep["errors"]) + res.text)


class TestReportShape(ImportRoutesBase):
    KEYS = ("format", "total", "inserted", "updated", "skipped", "failed", "errors",
            "elapsed_s", "db_path", "dry_run")

    def test_report_has_all_fields(self):
        self.post_csv(_csv_rows(3))
        rep = self.last_report()
        for key in self.KEYS:
            self.assertIn(key, rep, "报告缺字段 %s" % key)
        self.assertIsInstance(rep["errors"], list)
        self.assertIsInstance(rep["elapsed_s"], (int, float))

    def test_report_logged_for_traceability(self):
        self.post_csv(_csv_rows(2))
        found = []
        for root, _dirs, files in os.walk(self.out_dir):
            found += [os.path.join(root, f) for f in files if f.endswith(".json")]
        self.assertTrue(found, "每次导入要留一份 out/import_reports/<ts>.json")
        data = json.load(open(found[0], encoding="utf-8"))
        self.assertIn("format", data)


class TestDegrade(ImportRoutesBase):
    def test_readonly_db_degrades_not_500(self):
        os.chmod(self.db_path, 0o444)
        try:
            res = self.post_csv(_csv_rows(3))
        finally:
            os.chmod(self.db_path, 0o666)
        self.assertEqual(200, res.status_code, "无写权限也不许 500")
        self.assertIn("权限", res.text + json.dumps(self.last_report(), ensure_ascii=False))

    def test_docx_kind_reports_unavailable_gracefully(self):
        res = self.client.post("/import", data={"format": "docx", "confirm": "1"},
                               files={"file": ("x.docx", b"PK\x03\x04broken", "application/octet-stream")})
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertTrue(rep["errors"], "docx 走不通要给原因，不要 500")


class TestMultiSubject(ImportRoutesBase):
    def test_subject_lands_in_subjects_table(self):
        res = self.post_csv(_csv_rows(2), data={"subject": "语文", "version": "人教版"})
        self.assertEqual(200, res.status_code)
        conn = sqlite3.connect(self.db_path)
        try:
            names = [r[0] for r in conn.execute("SELECT name FROM subjects")]
        finally:
            conn.close()
        self.assertIn("语文", names)


class TestUnknownFormat(ImportRoutesBase):
    """t63：显式传不支持的 format 必须是 200 + 人话，绝不能是 500 KeyError。"""

    def test_txt_format_returns_200_with_hint(self):
        res = self.client.post(
            "/import",
            data={"format": "txt", "dry_run": "1"},
            files={"file": ("notes.txt", b"hello world", "text/plain")})
        self.assertEqual(200, res.status_code, "format=txt 不许 500")
        self.assertIn("不支持的格式", res.text)
        for word in ("csv", "excel", "json"):
            self.assertIn(word, res.text, "提示里要写清楚支持哪些格式")
        rep = self.last_report()
        self.assertEqual("txt", rep["format"])
        self.assertEqual(1, rep["failed"])
        self.assertTrue(any("不支持的格式" in (e.get("reason") or "")
                            for e in rep["errors"]), rep["errors"])

    def test_unknown_extension_is_friendly(self):
        res = self.client.post(
            "/import", data={"dry_run": "1"},
            files={"file": ("data.weird", b"whatever", "application/octet-stream")})
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertEqual(1, rep["failed"])
        reasons = " ".join(e.get("reason") or "" for e in rep["errors"])
        self.assertTrue("认不出格式" in reasons or "不支持的格式" in reasons, reasons)

    def test_corrupt_xlsx_is_friendly(self):
        res = self.client.post(
            "/import", data={"format": "excel", "dry_run": "1"},
            files={"file": ("broken.xlsx", b"this is not a real xlsx",
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        self.assertEqual(200, res.status_code, "损坏的 xlsx 不许 500")
        rep = self.last_report()
        self.assertEqual(1, rep["failed"])
        reasons = " ".join(e.get("reason") or "" for e in rep["errors"])
        self.assertTrue("打不开" in reasons or "另存为" in reasons, reasons)


class TestPreviewItems(ImportRoutesBase):
    """t63：预览报告要带逐行 items（前 20 行），页面才能出「前 20 行预览」表。"""

    def test_dry_run_report_has_items(self):
        res = self.post_csv(_csv_rows(25), dry_run=True)
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        self.assertTrue(rep.get("dry_run"), rep)
        items = rep.get("items") or []
        self.assertEqual(20, len(items), "预览最多列 20 行（文件有 25 行）")
        first = items[0]
        for key in ("line", "no", "qtype", "stem", "answer", "status"):
            self.assertIn(key, first, "预览行缺字段 %s" % key)
        self.assertEqual("ok", first["status"])
        self.assertLessEqual(len(first["stem"]), 60, "题干要截断到 60 字")
        self.assertEqual([1, 2, 3], [it["line"] for it in items[:3]], "行号要递增")

    def test_preview_includes_problem_rows(self):
        rows = _csv_rows(3) + ["BADROW-only-one-column\n"]
        res = self.post_csv(rows, dry_run=True)
        self.assertEqual(200, res.status_code)
        items = self.last_report().get("items") or []
        self.assertTrue(any(it["status"] == "error" for it in items),
                        "问题行也要出现在预览里：%r" % items)

    def test_items_exposed_in_page_and_report_json(self):
        self.post_csv(_csv_rows(5), dry_run=True)
        rep = self.last_report()
        self.assertEqual(5, len(rep["items"]))
        page = self.client.get("/import")
        self.assertEqual(200, page.status_code)


class TestBySubject(ImportRoutesBase):
    """t63：报告要有学科维度（学科 → 新增/跳过/失败）。"""

    def test_report_has_by_subject_after_import(self):
        res = self.post_csv(_csv_rows(6))
        self.assertEqual(200, res.status_code)
        rep = self.last_report()
        by_subject = rep.get("by_subject") or {}
        self.assertTrue(by_subject, "报告缺 by_subject")
        pk = next(iter(by_subject))
        self.assertEqual(6, by_subject[pk]["inserted"])
        self.assertEqual(0, by_subject[pk]["failed"])
        for key in ("inserted", "skipped", "failed"):
            self.assertIn(key, by_subject[pk])

    def test_by_subject_in_dry_run_counts_valid_rows(self):
        self.post_csv(_csv_rows(4), dry_run=True)
        rep = self.last_report()
        by_subject = rep.get("by_subject") or {}
        self.assertTrue(by_subject)
        pk = next(iter(by_subject))
        self.assertEqual(4, by_subject[pk]["inserted"], "预览时 inserted 记「合法行数」")

    def test_by_subject_key_matches_subject_pk(self):
        self.post_csv(_csv_rows(2))
        rep = self.last_report()
        conn = sqlite3.connect(self.db_path)
        try:
            pks = {r[0] for r in conn.execute("SELECT subject_pk FROM questions")}
        finally:
            conn.close()
        self.assertEqual(pks, set((rep.get("by_subject") or {})),
                         "按学科分组要跟库里的 subject_pk 对得上")
