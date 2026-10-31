# -*- coding: utf-8 -*-
"""M0-c · 建库测试：结构、样例正确性、可重建性。用 --limit 控制规模，秒级。"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_db  # noqa: E402
import db as dbmod  # noqa: E402


class TestBuildDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="db_")
        cls.path = os.path.join(cls.tmp, "t.db")
        cls.res = build_db.build(limit=300, db_path=cls.path)
        cls.conn = dbmod.connect(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_basic_count(self):
        """limit=300 只收前 300 道里有答案的题（无答案的被丢弃）。"""
        n = self.conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
        self.assertEqual(self.res["questions"], n)
        self.assertGreater(n, 0)
        self.assertLessEqual(n, 300)
        self.assertGreater(self.res["dropped_no_answer"], 0, "前 300 道里应该有没答案的")

    def test_answered_only_policy(self):
        """回归：默认建库不许导入无答案的题（用户要求无答案题删掉）。"""
        empty = self.conn.execute(
            "SELECT COUNT(*) c FROM questions WHERE answer_raw=''").fetchone()["c"]
        self.assertEqual(0, empty, "库里不许有无答案的题")
        self.assertEqual("answered_only", dbmod.get_meta(self.conn, "policy"))
        self.assertTrue(int(dbmod.get_meta(self.conn, "dropped_no_answer")) > 0)

    def test_no_primary_key_duplicates(self):
        d = self.conn.execute("SELECT COUNT(*) c FROM (SELECT no FROM questions GROUP BY no HAVING COUNT(*)>1)").fetchone()["c"]
        self.assertEqual(0, d)

    def test_00001_rich_tokens(self):
        row = self.conn.execute("SELECT * FROM questions WHERE no='00001'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("选择", row["qtype"])
        self.assertIn("omml", row["fmt"])
        toks = json.loads(row["tokens_json"])
        maths = [t for t in toks if t["kind"] == "math"]
        self.assertEqual(6, len(maths), "入库后 OMML 公式丢失")
        self.assertIn("某同学解方程", row["text_plain"])

    def test_ole_formula_file(self):
        """01000 走 MathType/OLE 分支，入库后 embed token 的媒体名要保留。"""
        row = self.conn.execute("SELECT * FROM questions WHERE no='01000'").fetchone()
        if row is None:
            self.skipTest("limit 未覆盖 01000")
        toks = json.loads(row["tokens_json"])
        embeds = [t for t in toks if t["kind"] == "embed"]
        self.assertEqual(4, len(embeds))
        self.assertTrue(all(t["n_media"] == 1 for t in embeds))
        self.assertIn("image", "".join(embeds[0]["media_names"]))

    def test_answer_imported(self):
        n = self.conn.execute("SELECT COUNT(*) c FROM questions WHERE answer_raw<>''").fetchone()["c"]
        self.assertGreater(n, 100, "答案几乎没有导入")
        row = self.conn.execute("SELECT answer_raw FROM questions WHERE no='00001'").fetchone()
        self.assertTrue(row["answer_raw"])

    def test_kp_populated(self):
        n = self.conn.execute("SELECT COUNT(*) c FROM questions WHERE kp_id IS NOT NULL").fetchone()["c"]
        self.assertEqual(self.res["questions"], n)
        ncomp = self.conn.execute("SELECT COUNT(*) c FROM questions WHERE kp_id LIKE '9.%'").fetchone()["c"]
        self.assertLess(ncomp / max(1, n), 0.35)

    def test_families_and_kps_tables(self):
        f = self.conn.execute("SELECT COUNT(*) c FROM families").fetchone()["c"]
        k = self.conn.execute("SELECT COUNT(*) c FROM kps").fetchone()["c"]
        self.assertGreater(f, 0, "题族表为空")
        self.assertGreaterEqual(k, 20, "知识点表条目过少")
        row = self.conn.execute("SELECT * FROM families LIMIT 1").fetchone()
        self.assertIn("::", row["family_key"])
        self.assertGreaterEqual(row["size"], 1)

    def test_rebuild_idempotent(self):
        res2 = build_db.build(limit=300, db_path=self.path)
        self.assertEqual(self.res["questions"], res2["questions"])
        n = self.conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
        self.assertEqual(res2["questions"], n)

    def test_meta_written(self):
        self.assertEqual(str(self.res["questions"]), dbmod.get_meta(self.conn, "n_questions"))
        self.assertTrue(dbmod.get_meta(self.conn, "built_at"))

    # ---- M0 交付报告：统计必须能从库里读回来，且小样本不做阈值判定 ----

    def test_report_written_and_consistent(self):
        rp = os.path.join(self.tmp, "report.json")
        rep = build_db.write_report(self.path, rp)
        self.assertTrue(os.path.exists(rp), "build_report.json 没写到")
        with open(rp, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(self.res["questions"], disk["questions"])
        self.assertEqual(self.res["questions"], rep["questions"])
        self.assertEqual(self.res["questions"], rep["distinct_no"])
        self.assertEqual(0, rep["dup_no"])
        self.assertFalse(rep["full_corpus"], "limit 建库不该被当成全量")
        for k in ("db", "size_mb", "qtype", "fmt", "answer_raw_nonempty",
                  "families", "families_ge3", "kps_rows", "acceptance"):
            self.assertIn(k, rep)
        self.assertEqual(self.res["questions"], sum(rep["fmt"]["combos"].values()),
                         "fmt 分布加总应等于题数")
        self.assertIn("omml", rep["fmt"])
        self.assertTrue(all(a["ok"] is None for a in rep["acceptance"]),
                        "小样本不能给阈值结论")

    def test_acceptance_thresholds(self):
        """阈值判定逻辑：全量实测值通过，明显偏差值不通过。"""
        good = build_db.acceptance_checks({
            "questions": 12280, "dup_no": 0,
            "fmt": {"omml": 7166, "ole": 2660, "ole_or_embed": 2856},
            "answer_raw_nonempty": 12280, "families": 2091,
            "families_ge3": 1310, "kps_rows": 34,
        })
        self.assertTrue(all(a["ok"] for a in good), [a for a in good if not a["ok"]])
        self.assertEqual("questions", good[0]["criterion"])

        bad = build_db.acceptance_checks({
            "questions": 22897, "dup_no": 3,
            "fmt": {"omml": 5000, "ole": 600, "ole_or_embed": 700},
            "answer_raw_nonempty": 100, "families": 900,
            "families_ge3": 200, "kps_rows": 12,
        })
        self.assertTrue(all(a["ok"] is False for a in bad), [a for a in bad if a["ok"]])

    def test_audit_vs_raw_xml_subset(self):
        """fmt 与原始 XML 的差集必须能解释：只允许是表格内/文本框内嵌套公式（token 侧算 table/embed）。"""
        a = build_db.audit_vs_raw_xml(self.path, limit=80)
        self.assertGreater(a["raw_omml"] + a["raw_ole"], 0)
        self.assertEqual([], a["unexplained"])
        self.assertEqual(a["raw_omml"], a["db_omml"] + a["in_table_omml"] + a["in_object_omml"])
        self.assertEqual(a["raw_ole"], a["db_ole"] + a["in_table_ole"] + a["in_object_ole"])

    def test_audit_locates_textbox_formula(self):
        """19382 的 6 个 oMath 全在文本框里（token 侧是一个 embed），必须被归到 in_object 而不是算丢失。"""
        a = build_db.audit_one("19382")
        self.assertEqual("omml", a["kind"])
        self.assertEqual("object", a["where"], "文本框内公式应归为 object（embed token 保留 XML）")
        self.assertEqual(6, a["count"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
