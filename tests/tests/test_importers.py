# -*- coding: utf-8 -*-
"""t51 · 题库导入接口的测试（JSON / Excel / CSV / docx + 多学科 + 幂等 + 导入报告）。

先红后绿：本文件先写成"期望行为"，importers 包实现后转绿。
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from openpyxl import Workbook

import db as dbmod
import importers
import picker

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = "2026-10-31T10:00:00+08:00"


def _json_items(n=10):
    """10 道导入题：4 选择 + 4 填空 + 2 解答（都带答案，能直接组卷）。"""
    items = []
    for i in range(1, n + 1):
        if i <= 4:
            items.append({
                "no": "T%03d" % i, "qtype": "选择",
                "stem": "导入题 %d：下列各数中最小的是（　　）" % i,
                "options": ["A．-3", "B．0", "C．1", "D．2"],
                "answer": "ABCD"[i - 1], "analysis": "解析 %d" % i,
                "kp_id": "1.%d" % i, "chapter": "第1章 有理数",
            })
        elif i <= 8:
            items.append({
                "no": "T%03d" % i, "qtype": "填空",
                "stem": "导入题 %d：-5 的绝对值是____．" % i,
                "answer": "-5" if i % 2 else "5", "kp_id": "1.%d" % i,
                "chapter": "第1章 有理数",
            })
        else:
            items.append({
                "no": "T%03d" % i, "qtype": "解答",
                "stem": "导入题 %d：计算 (-2)³+8÷(-2)．" % i,
                "answer": "(1)-12", "kp_id": "1.%d" % i,
                "chapter": "第1章 有理数",
            })
    return items


class ImportersTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="imp_")
        self.db_path = os.path.join(self.tmp, "app.db")
        self.report_path = os.path.join(self.tmp, "import_report.json")
        self.conn = dbmod.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_json(self, items, subject=None, name="qbank.json"):
        doc = {"subject": (subject or {}).get("name", "数学"),
               "grade": (subject or {}).get("grade", "七年级上"),
               "version": (subject or {}).get("version", "沪科版"),
               "items": items}
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        return path


class TestJsonImport(ImportersTestBase):
    def test_json_10_items_imported(self):
        path = self._write_json(_json_items(10))
        rep = importers.import_file(path, "json", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertEqual(10, rep["new"])
        self.assertEqual(0, rep["failed"])
        n = self.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        self.assertEqual(10, n)
        # 都能被 picker 抽到（有答案 → 可判）
        m = picker.build({"title": "导入卷", "mix": {"选择": 2, "填空": 2, "解答": 1},
                          "seed": 1, "requirements": {"need_answer": True}}, self.conn, now=NOW)
        self.assertEqual(5, len(m["items"]))
        self.assertTrue(all(it["no"].startswith("T") for it in m["items"]))

    def test_subject_row_created(self):
        path = self._write_json(_json_items(3))
        rep = importers.import_file(path, "json", db_path=self.db_path,
                                    report_path=self.report_path)
        subs = dbmod.list_subjects(self.conn)
        self.assertTrue(subs, "导入要建 subjects 行")
        row = [s for s in subs if s["subject_pk"] == rep["subject_pk"]]
        self.assertEqual(1, len(row))
        self.assertEqual("数学", row[0]["name"])
        self.assertEqual("七年级上", row[0]["grade"])
        self.assertEqual("沪科版", row[0]["version"])

    def test_items_carry_subject_pk(self):
        path = self._write_json(_json_items(3))
        rep = importers.import_file(path, "json", db_path=self.db_path,
                                    report_path=self.report_path)
        got = {r[0] for r in self.conn.execute("SELECT DISTINCT subject_pk FROM questions")}
        self.assertEqual({rep["subject_pk"]}, got)

    def test_idempotent_reimport_updates(self):
        path = self._write_json(_json_items(5))
        first = importers.import_file(path, "json", db_path=self.db_path,
                                      report_path=self.report_path)
        second = importers.import_file(path, "json", db_path=self.db_path,
                                       report_path=self.report_path)
        self.assertEqual(5, first["new"])
        self.assertEqual(0, second["new"])
        self.assertEqual(5, second["updated"])
        n = self.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        self.assertEqual(5, n, "重复导入不能产生重复行")

    def test_update_changes_content(self):
        path = self._write_json(_json_items(1))
        importers.import_file(path, "json", db_path=self.db_path, report_path=self.report_path)
        items = _json_items(1)
        items[0]["answer"] = "D"
        items[0]["stem"] = "改过的题干"
        path2 = self._write_json(items, name="qbank2.json")
        rep = importers.import_file(path2, "json", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertEqual(1, rep["updated"])
        row = self.conn.execute("SELECT answer_raw, text_plain FROM questions WHERE no=?",
                                ("T001",)).fetchone()
        self.assertEqual("D", row[0])
        self.assertTrue(row[1].startswith("改过的题干"), "题干要更新（选项会拼在后面）")

    def test_bad_rows_reported_and_not_inserted(self):
        items = _json_items(3)
        items.append({"no": "BAD1", "qtype": "选择", "stem": "没有答案的选择题"})   # 缺 answer
        items.append({"no": "BAD2", "qtype": "作文", "stem": "题型非法"})          # qtype 非法
        items.append({"qtype": "填空", "stem": "没有题号"})                        # 缺 no
        path = self._write_json(items)
        rep = importers.import_file(path, "json", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertEqual(3, rep["new"])
        self.assertGreaterEqual(rep["failed"], 3)
        reasons = " ".join(e["reason"] for e in rep["errors"])
        self.assertIn("answer", reasons)
        self.assertIn("qtype", reasons)
        self.assertIn("no", reasons)
        n = self.conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        self.assertEqual(3, n, "坏行不能污染库")

    def test_report_file_written(self):
        path = self._write_json(_json_items(4))
        rep = importers.import_file(path, "json", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertTrue(os.path.exists(self.report_path))
        with open(self.report_path, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(rep["new"], disk["new"])
        self.assertIn("items", disk)
        self.assertEqual("json", disk["format"])


class TestExcelImport(ImportersTestBase):
    HEADERS = ["题号", "题型", "题干", "A", "B", "C", "D", "答案", "解析", "知识点", "章节"]

    def _write_xlsx(self, rows, headers=None, name="qbank.xlsx"):
        wb = Workbook()
        ws = wb.active
        ws.append(headers or self.HEADERS)
        for r in rows:
            ws.append(r)
        path = os.path.join(self.tmp, name)
        wb.save(path)
        return path

    def test_excel_with_chinese_headers(self):
        rows = [["E001", "选择", "下列正确的是（　　）", "A．1", "B．2", "C．3", "D．4",
                 "B", "解析", "1.1", "第1章 有理数"],
                ["E002", "填空", "-3 的相反数是____．", "", "", "", "", "3", "", "1.2", "第1章 有理数"]]
        path = self._write_xlsx(rows)
        rep = importers.import_file(path, "excel", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertEqual(2, rep["new"], rep.get("errors"))
        row = self.conn.execute(
            "SELECT qtype, text_plain, answer_raw, option_count FROM questions WHERE no='E001'"
        ).fetchone()
        self.assertEqual("选择", row[0])
        self.assertIn("下列正确的是", row[1])
        self.assertIn("A．1", row[1], "选项要拼进题面（卷面靠 text_plain 排版）")
        self.assertEqual("B", row[2])
        self.assertEqual(4, row[3])

    def test_excel_header_aliases(self):
        headers = ["编号", "类型", "题目", "选项", "正确答案", "答案解析", "考点", "章"]
        rows = [["F001", "填空", "-2 的绝对值是____", "", "2", "", "1.3", "第1章 有理数"]]
        path = self._write_xlsx(rows, headers=headers, name="alias.xlsx")
        rep = importers.import_file(path, "excel", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertEqual(1, rep["new"], rep.get("errors"))
        row = self.conn.execute("SELECT answer_raw FROM questions WHERE no='F001'").fetchone()
        self.assertEqual("2", row[0])

    def test_excel_options_in_one_cell(self):
        headers = ["题号", "题型", "题干", "选项", "答案"]
        rows = [["G001", "选择", "选一个（　　）", "A．1|B．2|C．3|D．4", "C"]]
        path = self._write_xlsx(rows, headers=headers, name="opt1.xlsx")
        rep = importers.import_file(path, "excel", db_path=self.db_path,
                                    report_path=self.report_path)
        if rep["new"] == 0:      # 选择必须有选项，缺了就报错——这里给了竖线分隔所以要成功
            self.fail("竖线分隔的选项应能识别：%s" % rep.get("errors"))
        row = self.conn.execute("SELECT option_count, text_plain FROM questions WHERE no='G001'"
                                ).fetchone()
        self.assertEqual(4, row[0])
        self.assertIn("D．4", row[1])


class TestCsvImport(ImportersTestBase):
    def test_csv_roundtrip(self):
        path = os.path.join(self.tmp, "qbank.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("题号,题型,题干,答案\n")
            f.write("C001,填空,-7 的相反数是____,7\n")
            f.write("C002,填空,|-3|=____,3\n")
        rep = importers.import_file(path, "csv", db_path=self.db_path,
                                    report_path=self.report_path)
        self.assertEqual(2, rep["new"], rep.get("errors"))


class TestMultiSubject(ImportersTestBase):
    def test_two_subjects_coexist_and_filter(self):
        math_path = self._write_json(_json_items(6), name="math.json")
        zh_path = self._write_json(
            [{"no": "Z001", "qtype": "填空", "stem": "「春」字的部首是____．", "answer": "日",
              "chapter": "语文·识字", "kp_id": "1.1"}],
            subject={"name": "语文", "grade": "七年级上", "version": "人教版"},
            name="zh.json")
        r1 = importers.import_file(math_path, "json", db_path=self.db_path,
                                   report_path=self.report_path)
        r2 = importers.import_file(zh_path, "json", db_path=self.db_path,
                                   report_path=self.report_path)
        self.assertNotEqual(r1["subject_pk"], r2["subject_pk"])
        self.assertEqual(2, len(dbmod.list_subjects(self.conn)))
        # 库级：subject_pk 分得清
        n_math = self.conn.execute("SELECT COUNT(*) FROM questions WHERE subject_pk=?",
                                   (r1["subject_pk"],)).fetchone()[0]
        n_zh = self.conn.execute("SELECT COUNT(*) FROM questions WHERE subject_pk=?",
                                 (r2["subject_pk"],)).fetchone()[0]
        self.assertEqual(6, n_math)
        self.assertEqual(1, n_zh)
        # 组卷级：用 subject_scope 过滤后只抽到本学科
        scope = importers.subject_scope(self.conn, r2["subject_pk"])
        m = picker.build({"title": "语文卷", "mix": {"填空": 1}, "scope": scope,
                          "seed": 1, "requirements": {"need_answer": True}}, self.conn, now=NOW)
        self.assertEqual(["Z001"], [it["no"] for it in m["items"]])

    def test_default_subject_is_math7a(self):
        self.assertEqual("math7a", importers.DEFAULT_SUBJECT)


class TestDocxPassthrough(unittest.TestCase):
    def test_docx_kind_delegates_to_build_db(self):
        """--from docx 仍走 build_db（不重复实现抽取），只做参数翻译。"""
        src = open(os.path.join(APP, "importers", "__init__.py"), encoding="utf-8").read()
        self.assertIn("build_db", src, "docx 分支必须复用 build_db")
        self.assertIn("docx", src)
