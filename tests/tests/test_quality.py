# -*- coding: utf-8 -*-
"""t16 · 题库质量体检测试（先红后绿）。

用手工构造的「坏题」样本逐条验证 8 类 flag，再用真实库断言全量扫描的总量与耗时。
真实坏样例来自全库探测：03506（题干只剩 ⟨式⟩．）、19272（含【详解】）、
00023（选择答案 "D．"）、00586（选择答案其实是解答内容）、07827（表格 >2）。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import db as dbmod  # noqa: E402
import quality  # noqa: E402

INSERT = ("INSERT OR REPLACE INTO questions(no, qtype, option_count, sub_count, is_empty,"
          " media_count, text_len, text_plain, tokens_json, fmt, src_title, src_para,"
          " family_key, kp_id, chapter, kp, kp_source, answer_raw, check_status)"
          " VALUES(?,?,0,0,0,0,?,?,?,'plain','',0,NULL,?,?,'','title',?,'')")

REAL_DB = os.path.join(APP, "data", "app.db")


def tok(kind, **kw):
    d = {"kind": kind}
    d.update(kw)
    return d


def add(conn, no, qtype, text, answer="", tokens=None, kp="1.2", chapter="第1章 有理数"):
    conn.execute(INSERT, (no, qtype, len(text or ""), text or "",
                          json.dumps(tokens or [], ensure_ascii=False),
                          kp, chapter, answer))


def build_fixture(conn):
    """12 道样本题，覆盖全部 8 类 flag + 1 道干净题。"""
    add(conn, "Q_CLEAN", "选择", "下列各数中，最小的数是（　）", "A",
        [tok("text", text="下列各数中，最小的数是（　）")])
    add(conn, "Q_NO_STEM", "其他", "", "", [])
    add(conn, "Q_PH_ONLY", "其他", "⟨图⟩", "",
        [tok("embed", xml="<w:drawing/>", n_media=1, width_pt=30, height_pt=20)])
    add(conn, "Q_ANALYSIS", "解答",
        "【分析】本题考查绝对值的意义\n【详解】因为|a|=3，所以 a=±3．",
        "(1)x=4", [tok("text", text="【分析】…")])
    add(conn, "Q_SHORT", "填空", "计算：", "3")
    add(conn, "Q_SEL_BAD", "选择", "下列说法正确的是（　）",
        "(1)−5\n(2)①7；②t=(4)/(3)")
    add(conn, "Q_FILL_LONG", "填空", "已知一元一次方程，求它的解并说明理由．",
        "解：" + "因为移项得" * 20)                      # > 60 字
    add(conn, "Q_SOL_LETTER", "解答", "解方程：2x+1=5", "A")
    add(conn, "Q_NO_ANS", "填空", "若 a+b=3，则 2a+2b 的值为____", "")
    add(conn, "Q_HEAVY_EMBED", "解答", "如图，已知△ABC，作它的三条中线．", "(1)图略",
        [tok("embed", xml="<w:drawing/>", n_media=1) for _ in range(9)])
    add(conn, "Q_HEAVY_TABLE", "解答", "填表并回答问题．", "(1)3",
        [tok("table", xml="<w:tbl/>", rows=3, cols=4) for _ in range(3)])
    # 题干只差空格/标点 → 规范化后前 200 字相同
    add(conn, "Q_FIG_MISSING", "解答", "如图，求阴影部分的面积，并写出计算过程．", "(1)12")
    add(conn, "Q_FIG_OK", "解答", "如图，求阴影部分的面积，并写出计算过程．", "(1)12",
        [tok("embed", xml="<w:drawing/>", n_media=1)])
    add(conn, "Q_FIG_IN_TABLE", "解答", "如图所示，填表并回答问题．", "(1)3",
        [tok("table", xml='<w:tbl xmlns:w="w"><w:tc><w:drawing/></w:tc></w:tbl>', rows=1, cols=1)])
    add(conn, "Q_DUP_A", "填空", "若 x=2，则 x² 的值是____．", "4")
    add(conn, "Q_DUP_B", "填空", "若  x=2 ，则 x² 的值是 ____ 。", "4")


FLAGS_OF = {}


class TestQualityScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="quality_")
        cls.db = os.path.join(cls.tmp, "q.db")
        cls.conn = dbmod.connect(cls.db)
        build_fixture(cls.conn)
        cls.conn.commit()
        cls.res = quality.scan(cls.conn)
        cls.flags = quality.load_flags(cls.conn)          # no -> set(flags)
        cls.detail = quality.load_detail(cls.conn)        # no -> dict

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 逐条 flag
    def test_clean_question_has_no_flags(self):
        self.assertEqual(set(), self.flags["Q_CLEAN"])

    def test_no_stem(self):
        self.assertIn("no_stem", self.flags["Q_NO_STEM"])
        self.assertNotIn("no_stem", self.flags["Q_PH_ONLY"], "有 embed 时不算 no_stem")

    def test_stem_only_placeholder(self):
        self.assertIn("stem_only_placeholder", self.flags["Q_PH_ONLY"])
        self.assertNotIn("stem_only_placeholder", self.flags["Q_CLEAN"])

    def test_analysis_left(self):
        self.assertIn("analysis_left", self.flags["Q_ANALYSIS"])
        self.assertNotIn("analysis_left", self.flags["Q_CLEAN"])
        self.assertIn("【分析】", self.detail["Q_ANALYSIS"]["analysis_left"])

    def test_short_stem(self):
        self.assertIn("short_stem", self.flags["Q_SHORT"])
        self.assertNotIn("short_stem", self.flags["Q_CLEAN"])

    def test_answer_mismatch_choice(self):
        self.assertIn("answer_mismatch", self.flags["Q_SEL_BAD"])
        self.assertNotIn("answer_mismatch", self.flags["Q_CLEAN"])

    def test_answer_mismatch_fill_too_long(self):
        self.assertIn("answer_mismatch", self.flags["Q_FILL_LONG"])

    def test_answer_mismatch_solution_is_letter(self):
        self.assertIn("answer_mismatch", self.flags["Q_SOL_LETTER"])

    def test_no_answer(self):
        self.assertIn("no_answer", self.flags["Q_NO_ANS"])
        self.assertIn("no_answer", self.flags["Q_NO_STEM"])
        self.assertNotIn("no_answer", self.flags["Q_CLEAN"])

    def test_heavy_layout_embed(self):
        self.assertIn("heavy_layout", self.flags["Q_HEAVY_EMBED"])
        self.assertIn("9", self.detail["Q_HEAVY_EMBED"]["heavy_layout"], "证据里要写 embed 数")

    def test_heavy_layout_table(self):
        self.assertIn("heavy_layout", self.flags["Q_HEAVY_TABLE"])
        self.assertNotIn("heavy_layout", self.flags["Q_CLEAN"])

    def test_dup_text_both_sides(self):
        self.assertIn("dup_text", self.flags["Q_DUP_A"])
        self.assertIn("dup_text", self.flags["Q_DUP_B"])
        self.assertIn("Q_DUP_B", self.detail["Q_DUP_A"]["dup_text"], "要写明和谁重复")
        self.assertNotIn("dup_text", self.flags["Q_CLEAN"])

    # ---------------------------------------------------------- 表/报告结构
    def test_fig_missing(self):
        """题干说「如图」但题里没图 → fig_missing；图在表格里不算缺（宽松口径）。"""
        self.assertIn("fig_missing", self.flags["Q_FIG_MISSING"])
        self.assertNotIn("fig_missing", self.flags["Q_FIG_OK"], "有嵌入图不算缺图")
        self.assertNotIn("fig_missing", self.flags["Q_FIG_IN_TABLE"],
                         "图藏在表格里不算缺图（严格口径会误杀）")
        self.assertNotIn("fig_missing", self.flags["Q_CLEAN"], "没声称有图的题不该误判")
        self.assertGreaterEqual(self.res["fig_claims"], 3)
        self.assertGreaterEqual(self.res["fig_missing_strict"], 1, "严格口径会多算出误判")

    def test_table_written(self):
        n = self.conn.execute("SELECT COUNT(*) FROM quality_flags").fetchone()[0]
        self.assertEqual(16, n, "16 道题都要有质量行")
        row = self.conn.execute(
            "SELECT flags, detail FROM quality_flags WHERE no='Q_SEL_BAD'").fetchone()
        self.assertIn("answer_mismatch", row[0])
        self.assertTrue(json.loads(row[1]), "detail 必须是可解析的证据 JSON")

    def test_scan_result_shape(self):
        r = self.res
        for k in ("scanned", "elapsed_s", "flags", "samples", "printable_by_qtype",
                  "printable_gradable_by_qtype", "dup_groups"):
            self.assertIn(k, r)
        self.assertEqual(16, r["scanned"])
        self.assertEqual(1, r["flags"]["no_stem"])
        self.assertEqual(1, r["flags"]["analysis_left"])
        self.assertEqual(3, r["flags"]["answer_mismatch"])
        self.assertEqual(4, r["flags"]["dup_text"], "两对同题干（含 如图 两题）")
        self.assertEqual(2, r["dup_groups"], "Q_DUP_A/B 与 Q_FIG_MISSING/OK 两对")
        self.assertLessEqual(len(r["samples"]["answer_mismatch"]), 5)

    def test_printable_by_qtype_excludes_unprintable(self):
        pool = self.res["printable_by_qtype"]
        # 选择题里 Q_CLEAN + Q_SEL_BAD 都能印（Q_SEL_BAD 只是判不了分，不影响印）
        self.assertEqual(2, pool.get("选择", 0))
        self.assertEqual(0, pool.get("其他", 0), "Q_NO_STEM / Q_PH_ONLY 不可印")
        # 能印且能判的题池：Q_SEL_BAD 被剔除
        self.assertEqual(1, self.res["printable_gradable_by_qtype"].get("选择", 0))

    # ---------------------------------------------------------- 对外函数
    def test_is_printable(self):
        self.assertTrue(quality.is_printable(self.conn, "Q_CLEAN"))
        self.assertFalse(quality.is_printable(self.conn, "Q_NO_STEM"))
        self.assertFalse(quality.is_printable(self.conn, "Q_PH_ONLY"))
        self.assertFalse(quality.is_printable(self.conn, "Q_ANALYSIS"))
        self.assertTrue(quality.is_printable(self.conn, "Q_SEL_BAD"),
                        "答案错不影响能不能印")
        self.assertFalse(quality.is_printable(self.conn, "Q_SEL_BAD", need_answer=True),
                         "组卷要判分时，答案错必须排除")

    def test_filter_printable(self):
        nos = ["Q_CLEAN", "Q_NO_STEM", "Q_ANALYSIS", "Q_SEL_BAD", "Q_NO_ANS", "Q_DUP_A"]
        # 默认只挡「印不出来」的：没有答案/答案错不影响印
        ok, rejected = quality.filter_printable(self.conn, nos)
        self.assertEqual(["Q_CLEAN", "Q_SEL_BAD", "Q_NO_ANS", "Q_DUP_A"], ok)
        self.assertEqual({"Q_NO_STEM", "Q_ANALYSIS"}, set(rejected))
        # 组卷要判分：答案错/没答案/重复题一并排除
        ok2, rejected2 = quality.filter_printable(self.conn, nos, need_answer=True,
                                                  strict=True)
        self.assertEqual(["Q_CLEAN"], ok2)
        self.assertEqual({"Q_NO_STEM", "Q_ANALYSIS", "Q_SEL_BAD", "Q_NO_ANS", "Q_DUP_A"},
                         set(rejected2))
        self.assertIn("dup_text", rejected2["Q_DUP_A"])

    def test_unknown_question_not_scanned(self):
        self.assertTrue(quality.is_printable(self.conn, "NOPE"), "没体检过的题默认放行")

    def test_scan_limit(self):
        conn = dbmod.connect(os.path.join(self.tmp, "q2.db"))
        add(conn, "A1", "填空", "正常题干一道填空题的样本", "1")
        add(conn, "A2", "填空", "", "")
        add(conn, "A3", "填空", "另一道正常题干填空题样本", "2")
        conn.commit()
        res = quality.scan(conn, limit=2)
        self.assertEqual(2, res["scanned"])
        n = conn.execute("SELECT COUNT(*) FROM quality_flags").fetchone()[0]
        self.assertEqual(2, n)
        conn.close()

    def test_rescan_updates_stale_flags(self):
        conn = dbmod.connect(os.path.join(self.tmp, "q3.db"))
        add(conn, "B1", "填空", "", "")                  # 一开始是坏题
        conn.commit()
        quality.scan(conn)
        self.assertFalse(quality.is_printable(conn, "B1"))
        conn.execute("UPDATE questions SET text_plain=?, answer_raw='7' WHERE no='B1'",
                     ("修好之后的完整题干内容",))
        conn.commit()
        quality.scan(conn)
        self.assertTrue(quality.is_printable(conn, "B1"), "重扫后旧 flag 必须被清掉")
        conn.close()


class TestRealLibrary(unittest.TestCase):
    """真实 22,898 题全库扫描（只读打开 + write=False，不污染 app.db）。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL_DB):
            raise unittest.SkipTest("app.db 不存在")

    def test_full_scan_speed_and_total(self):
        import time
        conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        try:
            t0 = time.time()
            res = quality.scan(conn, write=False)
            elapsed = time.time() - t0
        finally:
            conn.close()
        self.assertEqual(12280, res["scanned"])
        self.assertLess(elapsed, 120.0, "全库体检太慢：%.1fs" % elapsed)
        self.assertEqual(0, res["flags"]["no_answer"], "裁剪后不该还有无答案的题")
        self.assertGreater(res["flags"]["answer_mismatch"], 0)
        self.assertGreater(res["flags"]["dup_text"], 0)
        self.assertGreater(sum(res["printable_by_qtype"].values()), 8000)
        # 每题 flag 都是已知集合
        self.assertTrue(set(res["flags"]) <= set(quality.ALL_FLAGS))

    def test_known_bad_samples_from_real_db(self):
        """真实库里的已知坏题：单题体检必须命中（不依赖全库扫描顺序）。"""
        conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        try:
            res = quality.scan(conn, write=False)
            # dup_text 只能由全库扫描得出（flags_for_no 不判重），这里只验单题能现算的 flag
            for flag in ("answer_mismatch", "fig_missing", "heavy_layout", "short_stem"):
                self.assertTrue(res["samples"][flag], "每个 flag 都该有真实样例：%s" % flag)
                for no in res["samples"][flag][:2]:
                    flags, detail = quality.flags_for_no(conn, no)
                    self.assertIn(flag, flags, "%s 没命中 %s，实际 %s" % (no, flag, flags))
                    self.assertTrue(detail.get(flag), "%s 的 %s 缺证据" % (no, flag))
            # 残句（short_stem）与空白题（no_stem）在裁剪后各有少量
            self.assertGreaterEqual(res["flags"]["fig_missing"], 1000, "缺图题应上千")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
