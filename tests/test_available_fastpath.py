# -*- coding: utf-8 -*-
"""t73 · 剩余题量快路径：纯 SQL 计数（<100ms）、与逐行实现逐组一致、导入后立即刷新。

覆盖：快路径耗时、20 组一致性、导入后即时刷新、TTL 60 秒、体检过滤口径、空范围 0。
"""
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

from fastapi.testclient import TestClient   # noqa: E402

import db as dbmod                          # noqa: E402
import picker                               # noqa: E402
import server                               # noqa: E402

REAL_DB = os.path.join(APP_DIR, "data", "app.db")


def _real_conn():
    return sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)


def seed_two_subjects(path):
    conn = dbmod.connect(path)
    try:
        dbmod.ensure_subject(conn, "math7a", "数学", "七年级上", "沪科版")
        dbmod.ensure_subject(conn, "chinese7a", "语文", "七年级上", "人教版")
        rows = []
        for i in range(12):
            rows.append(("M%03d" % i, "math7a", "选择" if i % 2 else "填空",
                         "第1章 有理数" if i < 6 else "第2章 整式",
                         "1.1" if i < 6 else "2.1", "A" if i % 2 else str(i), "", "plain"))
        for i in range(4):
            rows.append(("C%03d" % i, "chinese7a", "选择", "第一单元 春", "CN1", "A", "", "plain"))
        for no, sub, qt, ch, kp, ans, flags, fmt in rows:
            conn.execute("INSERT INTO questions(no,qtype,subject_pk,chapter,kp_id,answer_raw,"
                         "text_plain,is_empty,fmt,option_count) VALUES(?,?,?,?,?,?,?,0,?,4)",
                         (no, qt, sub, ch, kp, ans, "题目 " + no, fmt))
        conn.commit()
    finally:
        conn.close()


class FastPathConsistencyTest(unittest.TestCase):
    """快路径与逐行实现必须逐组一致（真库上随机抽 20 组范围）。"""

    @classmethod
    def setUpClass(cls):
        cls.conn = _real_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _scopes(self, rnd):
        chapters = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT chapter FROM questions WHERE subject_pk='math7a'"
            " AND chapter<>'' ORDER BY chapter")]
        kps = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT kp_id FROM questions WHERE subject_pk='math7a'"
            " AND kp_id<>'' ORDER BY kp_id")]
        alts = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT subject_pk FROM questions WHERE subject_pk<>'math7a'")]
        alt = "english9a" if "english9a" in alts else (alts[0] if alts else "math7a")
        scopes = [{"subject_pk": "math7a"}]                    # 整册
        scopes.append({"subject_pk": alt})                     # 另一学科整册
        for ch in chapters[:4]:
            scopes.append({"subject_pk": "math7a", "chapters": [ch]})       # 单章节
        scopes.append({"subject_pk": "math7a", "chapters": chapters[:3]})   # 多章节
        for kp in kps[:2]:
            scopes.append({"subject_pk": "math7a", "kps": [kp], "kp_mode": "any"})
        for kp in kps[2:4]:
            scopes.append({"subject_pk": "math7a", "kps": [kp], "kp_mode": "all"})
        scopes.append({"subject_pk": "math7a", "chapters": chapters[:2],
                       "kps": kps[:2], "kp_mode": "all"})       # 混合
        scopes.append({"subject_pk": "math7a", "strict": True})  # strict 口径
        scopes.append({"subject_pk": "math7a", "need_answer": False})
        eng_chapters = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT chapter FROM questions WHERE subject_pk=?"
            " AND chapter<>'' ORDER BY chapter", (alt,))]
        while len(scopes) < 20:                                 # 随机补足到 20 组
            sub = alt if (eng_chapters and rnd.random() < 0.5) else "math7a"
            pool = chapters if sub == "math7a" else eng_chapters
            if not pool:
                continue
            n = rnd.randint(1, min(4, len(pool)))
            scopes.append({"subject_pk": sub, "chapters": rnd.sample(pool, n)})
        return scopes[:20]

    def test_twenty_scopes_identical(self):
        rnd = random.Random(20261107)
        for i, scope in enumerate(self._scopes(rnd), 1):
            params = {"mix": {"选择": 1}, "scope": scope}
            fast = picker.count_available(self.conn, params)
            slow = picker.count_available_slow(self.conn, params)
            self.assertEqual(slow, fast, "第 %d 组范围两种实现不一致：%s" % (i, scope))

    def test_fast_path_under_100ms(self):
        params = {"mix": {"选择": 1}, "scope": {"subject_pk": "math7a"}}
        picker.count_available(self.conn, params)          # 预热（fig 文本扫描）
        times = []
        for _ in range(3):
            t0 = time.time()
            picker.count_available(self.conn, params)
            times.append((time.time() - t0) * 1000)
        self.assertTrue(max(times) < 100,
                        "快路径三次耗时 %s ms，要求 <100ms" % ["%.0f" % t for t in times])
        print("\n[快路径耗时] " + " / ".join("%.0fms" % t for t in times))


class FastPathFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fastpath_")
        self.db = os.path.join(self.tmp, "app.db")
        seed_two_subjects(self.db)
        self.conn = sqlite3.connect(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quality_flags_excluded_like_selection(self):
        """有 no_stem 体检标记的题，快路径与选题一样数不到。"""
        before = picker.count_available(self.conn, {"mix": {"选择": 1},
                                                    "scope": {"subject_pk": "math7a"}})
        self.conn.execute("INSERT INTO quality_flags(no, flags) VALUES('M001','no_stem')")
        self.conn.commit()
        after = picker.count_available(self.conn, {"mix": {"选择": 1},
                                                   "scope": {"subject_pk": "math7a"}})
        self.assertEqual(before["选择"] - 1, after["选择"])
        self.assertEqual(picker.count_available_slow(
            self.conn, {"mix": {"选择": 1}, "scope": {"subject_pk": "math7a"}}), after)

    def test_fig_missing_computed_excluded(self):
        """题干说「如图」但没有图 → 快路径也要排除（与 fig_missing 规则一致）。"""
        before = picker.count_available(self.conn, {"mix": {"填空": 1},
                                                    "scope": {"subject_pk": "math7a"}})
        self.conn.execute("UPDATE questions SET text_plain='如图所示，求角度', fmt='plain'"
                          " WHERE no='M000'")
        self.conn.commit()
        picker.clear_cache()          # 行内容变了，清 fig 缓存
        after = picker.count_available(self.conn, {"mix": {"填空": 1},
                                                   "scope": {"subject_pk": "math7a"}})
        self.assertEqual(before["填空"] - 1, after["填空"])
        self.assertEqual(picker.count_available_slow(
            self.conn, {"mix": {"填空": 1}, "scope": {"subject_pk": "math7a"}}), after)

    def test_empty_scope_is_zero(self):
        out = picker.count_available(self.conn, {"mix": {"选择": 1},
                                                 "scope": {"subject_pk": "chinese7a"}})
        self.assertIn("判断", out)
        self.assertEqual(0, out["判断"], "没有判断题的学科应为 0")
        self.assertEqual(0, picker.count_available(
            self.conn, {"mix": {"选择": 1}, "scope": {"subject_pk": "chinese7a",
                                                      "chapters": ["第一单元 春"]}})["解答"])

    def test_ttl_is_60_seconds(self):
        self.assertEqual(60.0, server._AVAIL_TTL, "TTL 必须从 300s 降到 60s")


class ImportInvalidatesCacheTest(unittest.TestCase):
    """导入成功后，/api/available 必须立刻反映新题数（不用等 TTL）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fastpath_srv_")
        cls.db = os.path.join(cls.tmp, "app.db")
        seed_two_subjects(cls.db)
        cls.cfg = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg, "w", encoding="utf-8") as f:
            f.write('{"ocr": {"backend": "manual"}, "server": {"host": "127.0.0.1", "port": 8710}}')
        cls.app = server.create_app(db_path=cls.db, config_path=cls.cfg)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _total(self, subject="math7a"):
        r = self.client.get("/api/available", params={"subject_pk": subject})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()["total"]

    def test_import_one_question_increases_total_immediately(self):
        before = self._total()                      # 先让缓存热起来
        payload = [{"no": "M900", "qtype": "填空", "subject_pk": "math7a",
                    "chapter": "第1章 有理数", "kp_id": "1.1", "answer": "3",
                    "stem": "导入进来的一道题"}]
        path = os.path.join(self.tmp, "one.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        with open(path, "rb") as f:
            res = self.client.post("/import",
                                   data={"confirm": "1", "kind": ""},
                                   files={"file": ("one.json", f, "application/json")},
                                   follow_redirects=True)
        self.assertEqual(200, res.status_code, res.text)
        after = self._total()
        self.assertEqual(before + 1, after,
                         "导入 1 题后 total 必须 +1（缓存要清）：%s → %s" % (before, after))

    def test_first_open_under_200ms(self):
        """无缓存首次打开：/api/available 端到端 <200ms。"""
        server.clear_available_cache()
        t0 = time.time()
        r = self.client.get("/api/available", params={"subject_pk": "math7a",
                                                      "chapters": ["第2章 整式"]})
        cost = (time.time() - t0) * 1000
        self.assertEqual(200, r.status_code)
        self.assertLess(cost, 200, "首次 /api/available 用了 %.0fms" % cost)


if __name__ == "__main__":
    unittest.main()
