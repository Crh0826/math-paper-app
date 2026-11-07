# -*- coding: utf-8 -*-
"""t90 · 跨学科题号安全线：导入器不许拿同号覆盖别的学科的题。

真实事故：物理卷按 00001..00023 导入，把 math7a 的 00001..00023 覆盖成物理题。
本测试锁死四条：
* 默认拒绝（报告列「题号 / 原有学科 / 本次学科」，不覆盖、不静默更新）；
* ``renumber=True`` 按学科号段自动改号，报告列「原号 → 新号」；
* 号段注册表持久化（``subject_no_block``），同内容重复导入得到同一个号（幂等靠内容指纹）；
* 回归：用旧号重导物理，math7a 的 00001..00023 一道都不许变。
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

import db as dbmod                      # noqa: E402
from importers import core as imp       # noqa: E402

MATH = {"subject_pk": "math7a", "name": "数学", "grade": "七年级上", "version": "沪科版"}
PHYS = {"subject_pk": "physics9a", "name": "物理", "grade": "九年级上", "version": "沪科版"}


def q(no, stem, subject="物理"):
    return {"no": no, "qtype": "填空", "stem": stem, "answer": "1",
            "chapter": ("第1章 声" if subject == "物理" else "第1章 有理数"), "kp_id": "1.1"}


class NoCollisionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="no_col_")
        self.db = os.path.join(self.tmp, "app.db")
        self.conn = dbmod.connect(self.db)
        # 先放 3 道数学题（00001..00003），模拟线上被覆盖前的状态
        self.math_items, _ = imp.normalize_rows(
            [q("00001", "数学题一", "数学"), q("00002", "数学题二", "数学"),
             q("00003", "数学题三", "数学")], "math7a")
        dbmod.ensure_subject(self.conn, "math7a", "数学", "七年级上", "沪科版")
        imp.upsert_items(self.conn, self.math_items, "math7a")
        self.math_before = self._snapshot_math()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snapshot_math(self):
        rows = self.conn.execute(
            "SELECT no, subject_pk, text_plain, answer_raw FROM questions"
            " WHERE no IN ('00001','00002','00003') ORDER BY no").fetchall()
        return [tuple(r) for r in rows]

    def _json(self, rows, subject, name="x.json"):
        path = os.path.join(self.tmp, name)
        payload = {"subject": {"name": subject["name"], "grade": subject["grade"],
                               "version": subject["version"]},
                   "subject_pk": subject["subject_pk"], "items": rows}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path

    def _import(self, rows, subject, **kw):
        path = self._json(rows, subject, name=kw.pop("name", "x.json"))
        return imp.import_file(path, "json", db_path=self.db,
                               report_path=os.path.join(self.tmp, "r.json"),
                               subject=subject, **kw)

    def _phys_items(self, nos=("00001", "00002", "00003")):
        items, _ = imp.normalize_rows([q(n, "物理题%s" % n) for n in nos], "physics9a")
        return items


class RejectConflictTest(NoCollisionBase):
    def test_default_rejects_cross_subject_rows(self):
        res = imp.upsert_items(self.conn, self._phys_items(), "physics9a")
        self.assertEqual(0, res["new"] + res["updated"], "冲突行既不该新增也不该更新")
        self.assertEqual(3, res["rejected"], "三道撞号应被拒")
        self.assertEqual(3, len(res["conflicts"]))
        c = res["conflicts"][0]
        self.assertEqual({"no", "existing_subject", "new_subject"}, set(c))
        self.assertEqual("math7a", c["existing_subject"])
        self.assertEqual("physics9a", c["new_subject"])

    def test_math_rows_unchanged_after_rejected_import(self):
        self._import([q(n, "物理题%s" % n) for n in ("00001", "00002", "00003")], PHYS)
        self.assertEqual(self.math_before, self._snapshot_math(),
                         "数学题被改动了：%r" % (self._snapshot_math(),))

    def test_report_has_conflict_fields(self):
        rep = self._import([q("00001", "物理题1")], PHYS)
        self.assertIn("conflicts", rep)
        self.assertIn("rejected", rep)
        self.assertEqual(1, rep["rejected"])
        self.assertEqual("00001", rep["conflicts"][0]["no"])


class RenumberTest(NoCollisionBase):
    def test_renumber_moves_to_subject_block(self):
        res = imp.upsert_items(self.conn, self._phys_items(), "physics9a", renumber=True)
        self.assertEqual(3, res["new"], "改号后应真的写进去")
        self.assertEqual(0, res["rejected"])
        moved = {r["from"]: r["to"] for r in res["renumbered"]}
        self.assertEqual({"00001": "60001", "00002": "60002", "00003": "60003"}, moved)
        stored = {r[0] for r in self.conn.execute(
            "SELECT no FROM questions WHERE subject_pk='physics9a'")}
        self.assertEqual({"60001", "60002", "60003"}, stored)
        self.assertEqual(self.math_before, self._snapshot_math(), "改号时也不许动数学题")

    def test_block_registry_persisted(self):
        imp.upsert_items(self.conn, self._phys_items(), "physics9a", renumber=True)
        row = self.conn.execute(
            "SELECT subject_pk, prefix, max_used FROM subject_no_block WHERE subject_pk=?",
            ("physics9a",)).fetchone()
        self.assertIsNotNone(row, "号段注册表要落库")
        self.assertEqual("6", row[1])
        self.assertEqual(3, int(row[2]))

    def test_reimport_is_idempotent(self):
        first = imp.upsert_items(self.conn, self._phys_items(), "physics9a", renumber=True)
        second = imp.upsert_items(self.conn, self._phys_items(), "physics9a", renumber=True)
        self.assertEqual({r["from"]: r["to"] for r in first["renumbered"]},
                         {r["from"]: r["to"] for r in second["renumbered"]},
                         "同一批题重复导入必须得到同样的号（靠内容指纹）")
        self.assertEqual(3, second["skipped"], "第二次应全部跳过（内容没变）")
        self.assertEqual(0, second["new"])


class NormalImportTest(NoCollisionBase):
    def test_free_numbers_import_normally(self):
        res = imp.upsert_items(self.conn, self._phys_items(("70001", "70002")), "physics9a")
        self.assertEqual(2, res["new"], "不撞号的正常导入不该被拦")
        self.assertEqual(0, res["rejected"])

    def test_same_subject_update_still_works(self):
        res = imp.upsert_items(self.conn, self._phys_items(("70001",)), "physics9a")
        self.assertEqual(1, res["new"])
        items, _ = imp.normalize_rows(
            [{"no": "70001", "qtype": "填空", "stem": "改过的物理题", "answer": "2",
              "chapter": "第1章 声", "kp_id": "1.1"}], "physics9a")
        res2 = imp.upsert_items(self.conn, items, "physics9a")
        self.assertEqual(1, res2["updated"], "同学科同号应正常更新")


if __name__ == "__main__":
    unittest.main()
