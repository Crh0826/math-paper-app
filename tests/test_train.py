# -*- coding: utf-8 -*-
"""t6 · 错题训练规划测试（先红后绿）。

覆盖：
  1) 四级回退：同族兄弟 → 同 kp+同 qtype（难度相近优先）→ 同章+同 qtype → 跨章同 qtype；
  2) 训练卷题数 = 错题数 × n_per，且**不含原错题**；
  3) 每条错题要么命中同族、要么命中同知识点（覆盖）；
  4) seed/入参顺序可复现；
  5) 池子不够时如实标 shortfall，不凑数；
  6) 掌握度曲线 1→3→7→15 天、错回 1 天；plan 优先挑已到期题。

用临时库造 10 道错题（跨 3 个族 / 3 个知识点），不碰 app/data/app.db。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import db as dbmod  # noqa: E402
import train  # noqa: E402

REAL_DB = os.path.join(APP, "data", "app.db")

NOW = "2026-10-31T10:00:00+08:00"

# 10 道错题：3 族 / 3 知识点
WRONG = ["10001", "10002", "20001", "20002", "20003", "30001", "30002", "30003", "30004", "30005"]

INSERT = ("INSERT OR REPLACE INTO questions(no, qtype, option_count, sub_count, is_empty,"
          " media_count, text_len, text_plain, tokens_json, fmt, src_title, src_para,"
          " family_key, kp_id, chapter, kp, kp_source, answer_raw, check_status)"
          " VALUES(?,?,?,?,?,0,10,'x','[]','plain','',0,?,?,?,'','title',?,'')")


def add(conn, no, qtype, kp, chapter, family=None, sub=0, opt=0, ans="A", empty=0):
    conn.execute(INSERT, (no, qtype, opt, sub, empty, family, kp, chapter, ans))


def build_fixture(conn, with_neighbours=True):
    """3 个题族 + 同 kp / 同章 / 跨章 的邻居题。"""
    CH1, CH2, CH3 = "第1章 有理数", "第2章 整式及其加减", "第3章 一次方程与方程组"

    # 族 A：kp 1.2 选择，2 道错题 + 3 个兄弟
    for no in ("10001", "10002"):
        add(conn, no, "选择", "1.2", CH1, "srcA::例1", sub=1, opt=4, ans="A")
    for no in ("10003", "10004", "10005"):
        add(conn, no, "选择", "1.2", CH1, "srcA::例1", sub=1, opt=4, ans="B")

    # 族 B：kp 2.1 填空，3 道错题 + 4 个兄弟
    for no in ("20001", "20002", "20003"):
        add(conn, no, "填空", "2.1", CH2, "srcB::例2", sub=2, ans="3")
    for no in ("20004", "20005", "20006", "20007"):
        add(conn, no, "填空", "2.1", CH2, "srcB::例2", sub=2, ans="4")

    # 族 C：kp 3.2 解答，5 道错题 + 7 个兄弟
    for no in ("30001", "30002", "30003", "30004", "30005"):
        add(conn, no, "解答", "3.2", CH3, "srcC::例3", sub=3, ans="(1)x=4")
    for i in range(6, 13):
        add(conn, "300%02d" % i, "解答", "3.2", CH3, "srcC::例3", sub=3, ans="(1)x=5")

    if not with_neighbours:
        return

    # 二级：不同族但同 kp + 同 qtype
    for i in range(1, 4):
        add(conn, "110%02d" % i, "选择", "1.2", CH1, "other1::例9", sub=1, opt=4, ans="C")
        add(conn, "210%02d" % i, "填空", "2.1", CH2, "other2::例9", sub=2, ans="5")
        add(conn, "310%02d" % i, "解答", "3.2", CH3, "other3::例9", sub=3, ans="(1)x=6")

    # 三级：同章不同 kp，同 qtype
    for i in range(1, 4):
        add(conn, "120%02d" % i, "选择", "1.3", CH1, None, sub=1, opt=4, ans="D")
        add(conn, "220%02d" % i, "填空", "2.4", CH2, None, sub=2, ans="6")
        add(conn, "320%02d" % i, "解答", "3.1", CH3, None, sub=3, ans="(1)x=7")

    # 四级：跨章同 qtype
    for i in range(1, 4):
        add(conn, "410%02d" % i, "选择", "4.1", "第4章 几何图形初步", None, sub=1, opt=4, ans="A")
        add(conn, "420%02d" % i, "填空", "4.2", "第4章 几何图形初步", None, sub=2, ans="7")
        add(conn, "430%02d" % i, "解答", "4.4", "第4章 几何图形初步", None, sub=3, ans="(1)x=8")


class TestTrainPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="train_")
        cls.db = os.path.join(cls.tmp, "t.db")
        cls.conn = dbmod.connect(cls.db)
        build_fixture(cls.conn)
        cls.conn.commit()
        cls.plan = train.plan(cls.conn, WRONG, n_per=3, now=NOW)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 基本形状
    def test_plan_shape_is_picker_params(self):
        p = self.plan
        for k in ("title", "mix", "scope", "requirements", "seed", "space"):
            self.assertIn(k, p, "缺 CONTRACT §2 字段 %s" % k)
        self.assertEqual({"need_answer": True, "exclude_empty": True,
                          "exclude_no_family": False}, p["requirements"])
        self.assertIn("chapters", p["scope"])
        self.assertIn("kps", p["scope"])
        self.assertIsInstance(p["seed"], int)
        self.assertTrue(p["nos"], "nos（精确题集）不能为空")
        self.assertEqual(sorted(WRONG), p["exclude_nos"])

    def test_item_count_is_sources_times_n_per(self):
        p = self.plan
        self.assertEqual(len(WRONG) * 3, len(p["nos"]))
        self.assertEqual(len(WRONG) * 3, sum(p["mix"].values()))
        by_qtype = {}
        for no in p["nos"]:
            row = self.conn.execute(
                "SELECT qtype FROM questions WHERE no=?", (no,)).fetchone()
            by_qtype[row["qtype"]] = by_qtype.get(row["qtype"], 0) + 1
        self.assertEqual(by_qtype, {k: v for k, v in p["mix"].items() if v})

    def test_no_original_question_in_paper(self):
        self.assertEqual([], [no for no in self.plan["nos"] if no in WRONG])
        self.assertEqual(len(set(self.plan["nos"])), len(self.plan["nos"]), "训练卷内不能重题")

    def test_all_chosen_exist_and_usable(self):
        for no in self.plan["nos"]:
            row = self.conn.execute(
                "SELECT is_empty, answer_raw FROM questions WHERE no=?", (no,)).fetchone()
            self.assertIsNotNone(row, "选了不存在的题 %s" % no)
            self.assertEqual(0, row["is_empty"])
            self.assertTrue(row["answer_raw"], "%s 无答案，判不了分" % no)

    # ---------------------------------------------------------- 覆盖
    def test_coverage_family_or_kp(self):
        """每条错题的族或知识点必须被训练卷覆盖到。"""
        picked = self.plan["nos"]
        rows = {r["no"]: r for r in self.conn.execute(
            "SELECT no, family_key, kp_id FROM questions WHERE no IN (%s)"
            % ",".join("?" * len(picked)), picked)}
        fam = {r["family_key"] for r in rows.values() if r["family_key"]}
        kps = {r["kp_id"] for r in rows.values()}
        bad = []
        for no in WRONG:
            w = self.conn.execute(
                "SELECT family_key, kp_id FROM questions WHERE no=?", (no,)).fetchone()
            if not ((w["family_key"] and w["family_key"] in fam) or w["kp_id"] in kps):
                bad.append(no)
        self.assertEqual([], bad, "这些错题的族和知识点都没覆盖到")

    def test_priority_family_first(self):
        """族里有兄弟题时，优先全都是同族兄弟（10001 有 3 个空闲兄弟）。"""
        src = [s for s in self.plan["train"]["sources"] if s["no"] == "10001"][0]
        self.assertEqual(3, len(src["picked"]))
        fams = {self.conn.execute("SELECT family_key FROM questions WHERE no=?",
                                  (no,)).fetchone()["family_key"] for no in src["picked"]}
        self.assertEqual({"srcA::例1"}, fams)
        self.assertEqual(["family", "family", "family"], src["levels"])

    def test_fallback_levels_when_family_exhausted(self):
        """族里兄弟被吃光后必须退到 kp / 章级，且 levels 如实记录。

        同族错题共享一个族池，所以只有每族的**第一道**错题能拿满同族题；
        后面的必然回退（这正是分级的意义），断言不能要求每道都命中族级。
        """
        levels = {s["no"]: s["levels"] for s in self.plan["train"]["sources"]}
        used = {lvl for seq in levels.values() for lvl in seq}
        self.assertIn("family", used)
        self.assertTrue(used & {"kp", "chapter", "cross"}, "没有回退到下级：%s" % used)
        for no in ("10001", "20001", "30001"):        # 每族的第一道
            self.assertEqual("family", levels[no][0], "%s 第一条应该同族" % no)
        self.assertIn("kp", used, "族池吃光后要退到知识点级")

    def test_no_shortfall_when_pool_is_deep(self):
        self.assertEqual({}, self.plan["train"]["shortfall"])

    # ---------------------------------------------------------- 可复现
    def test_deterministic_same_inputs(self):
        again = train.plan(self.conn, WRONG, n_per=3, now=NOW)
        self.assertEqual(self.plan, again)

    def test_input_order_irrelevant(self):
        shuffled = [WRONG[i] for i in (7, 0, 9, 3, 1, 8, 5, 2, 6, 4)]
        self.assertEqual(self.plan, train.plan(self.conn, shuffled, n_per=3, now=NOW))

    def test_explicit_seed_kept(self):
        p = train.plan(self.conn, WRONG, n_per=3, now=NOW, seed=2026)
        self.assertEqual(2026, p["seed"])
        self.assertEqual(self.plan["nos"], p["nos"], "换 seed 不该换掉已规划好的题集")

    def test_n_per_respected(self):
        p = train.plan(self.conn, WRONG, n_per=2, now=NOW)
        self.assertEqual(len(WRONG) * 2, len(p["nos"]))
        for s in p["train"]["sources"]:
            self.assertLessEqual(len(s["picked"]), 2)

    # ---------------------------------------------------------- 边界
    def test_empty_wrong_nos_raises(self):
        with self.assertRaises(ValueError):
            train.plan(self.conn, [], n_per=3, now=NOW)

    def test_unknown_no_recorded_not_crash(self):
        p = train.plan(self.conn, WRONG + ["99999"], n_per=1, now=NOW)
        self.assertIn("99999", p["train"]["skipped"])
        self.assertNotIn("99999", p["nos"])


class TestShortfall(unittest.TestCase):
    """池子不够时：如实标 shortfall，绝不凑数、绝不放原题。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="train_s_")
        self.conn = dbmod.connect(os.path.join(self.tmp, "s.db"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_one_neighbour(self):
        add(self.conn, "W1", "选择", "1.2", "第1章 有理数", "f::例1", ans="A")
        add(self.conn, "N1", "选择", "1.2", "第1章 有理数", "f::例1", ans="B")
        self.conn.commit()
        p = train.plan(self.conn, ["W1"], n_per=3, now=NOW)
        self.assertEqual(["N1"], p["nos"])
        self.assertEqual({"W1": {"need": 3, "got": 1}}, {
            k: {"need": v["need"], "got": v["got"]} for k, v in p["train"]["shortfall"].items()})

    def test_no_candidate_at_all_raises(self):
        add(self.conn, "W1", "选择", "1.2", "第1章 有理数", "f::例1", ans="A")
        self.conn.commit()
        with self.assertRaises(ValueError) as ctx:
            train.plan(self.conn, ["W1"], n_per=3, now=NOW)
        self.assertIn("W1", str(ctx.exception))

    def test_cross_chapter_fallback_marked(self):
        add(self.conn, "W1", "解答", "3.2", "第3章 一次方程与方程组", None, sub=3, ans="(1)x=4")
        add(self.conn, "X1", "解答", "4.4", "第4章 几何图形初步", None, sub=3, ans="(1)x=5")
        self.conn.commit()
        p = train.plan(self.conn, ["W1"], n_per=1, now=NOW)
        self.assertEqual(["X1"], p["nos"])
        self.assertEqual(["cross"], p["train"]["sources"][0]["levels"])


class TestMastery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="train_m_")
        self.conn = dbmod.connect(os.path.join(self.tmp, "m.db"))
        add(self.conn, "Q1", "填空", "1.2", "第1章 有理数", None, ans="3")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_interval_grows_on_correct(self):
        seen = []
        for i in range(5):
            r = train.record_result(self.conn, "Q1", True, now=NOW)
            seen.append(r["interval_days"])
        self.assertEqual([1, 3, 7, 15, 15], seen, "间隔应按 1→3→7→15 增长并封顶")
        self.assertEqual(5, r["attempts"])
        self.assertEqual(5, r["correct"])

    def test_wrong_resets_to_one_day(self):
        train.record_result(self.conn, "Q1", True, now=NOW)
        train.record_result(self.conn, "Q1", True, now=NOW)
        r = train.record_result(self.conn, "Q1", False, now=NOW)
        self.assertEqual(1, r["interval_days"])
        self.assertEqual(3, r["attempts"])
        self.assertEqual(2, r["correct"])
        self.assertTrue(r["due_at"].startswith("2026-11-01"), r["due_at"])

    def test_due_after_interval(self):
        r = train.record_result(self.conn, "Q1", True, now=NOW, interval_days=3)
        self.assertEqual(3, r["interval_days"])
        self.assertTrue(r["due_at"].startswith("2026-11-03"), r["due_at"])

    def test_due_nos_filter(self):
        train.record_result(self.conn, "Q1", True, now=NOW)          # due 11-01
        self.assertEqual([], train.due_nos(self.conn, now=NOW))       # 还没到期
        self.assertEqual(["Q1"], train.due_nos(self.conn, now="2026-11-02T00:00:00+08:00"))

    def test_plan_prefers_due_candidates(self):
        add(self.conn, "W1", "填空", "2.1", "第2章 整式及其加减", None, ans="3")
        add(self.conn, "C_FUTURE", "填空", "2.1", "第2章 整式及其加减", None, ans="4")
        add(self.conn, "C_DUE", "填空", "2.1", "第2章 整式及其加减", None, ans="5")
        self.conn.commit()
        # 让 C_DUE 早已到期，C_FUTURE 很久以后才到期
        train.record_result(self.conn, "C_DUE", True, now="2026-10-01T10:00:00+08:00")
        train.record_result(self.conn, "C_FUTURE", True, now="2026-10-30T10:00:00+08:00")
        train.record_result(self.conn, "C_FUTURE", True, now="2026-10-30T10:00:00+08:00", interval_days=15)
        p = train.plan(self.conn, ["W1"], n_per=2, now=NOW)
        self.assertEqual(["C_DUE", "C_FUTURE"], p["nos"], "到期题必须排在前面")

    def test_mastery_table_exists(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("mastery", names, "db.py 里要加 mastery 表")

    def test_record_results_bulk(self):
        r = train.record_results(self.conn, [("Q1", True), ("Q1", False)], now=NOW)
        self.assertEqual(2, len(r))
        row = self.conn.execute("SELECT attempts, correct FROM mastery WHERE key='Q1'").fetchone()
        self.assertEqual(2, row["attempts"])
        self.assertEqual(1, row["correct"])


class TestNoMasteryTable(unittest.TestCase):
    """老库（t1 建的 app.db）里还没有 mastery 表时，plan 必须照常工作。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="train_n_")
        self.conn = dbmod.connect(os.path.join(self.tmp, "n.db"))
        self.conn.execute("DROP TABLE mastery")
        self.conn.commit()
        add(self.conn, "W1", "选择", "1.2", "第1章 有理数", "f::例1", ans="A")
        add(self.conn, "N1", "选择", "1.2", "第1章 有理数", "f::例1", ans="B")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_without_mastery_table(self):
        p = train.plan(self.conn, ["W1"], n_per=1, now=NOW)
        self.assertEqual(["N1"], p["nos"])
        self.assertEqual([], p["train"]["due_first"])

    def test_due_nos_without_mastery_table(self):
        self.assertEqual([], train.due_nos(self.conn, now=NOW))

    def test_get_mastery_without_table_is_none(self):
        self.assertIsNone(train.get_mastery(self.conn, "W1"))


class TestRealCorpus(unittest.TestCase):
    """真实 22,898 题库上跑一遍：只读打开 app.db，不写 -wal。"""

    @classmethod
    def setUpClass(cls):
        real = os.path.join(APP, "data", "app.db")
        if not os.path.exists(real):
            raise unittest.SkipTest("app.db 不存在（先跑 build_db.py）")
        import sqlite3
        cls.conn = sqlite3.connect("file:%s?mode=ro" % real.replace("\\", "/"), uri=True)
        rows = cls.conn.execute(
            "SELECT no FROM questions WHERE is_empty=0 AND answer_raw<>''"
            " AND family_key IS NOT NULL AND family_key<>'' ORDER BY no LIMIT 40").fetchall()
        cls.wrong = [r[0] for r in rows[:10]]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_plan_on_real_library(self):
        import time
        t0 = time.time()
        p = train.plan(self.conn, self.wrong, n_per=3, now=NOW)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0, "真实库规划太慢：%.2fs" % elapsed)
        self.assertLessEqual(len(p["nos"]), len(self.wrong) * 3)
        self.assertGreaterEqual(len(p["nos"]), len(self.wrong), "至少每条错题一道")
        self.assertEqual([], [n for n in p["nos"] if n in self.wrong], "训练卷不能含原题")
        self.assertEqual(len(set(p["nos"])), len(p["nos"]), "训练卷不能重题")
        for no in p["nos"]:
            row = self.conn.execute(
                "SELECT is_empty, answer_raw FROM questions WHERE no=?", (no,)).fetchone()
            self.assertEqual(0, row[0])
            self.assertTrue(row[1])
        # 覆盖：每条错题的族或知识点都要被覆盖
        fam, kps = set(), set()
        for no in p["nos"]:
            r = self.conn.execute("SELECT family_key, kp_id FROM questions WHERE no=?",
                                  (no,)).fetchone()
            fam.add(r[0])
            kps.add(r[1])
        miss = []
        for no in self.wrong:
            w = self.conn.execute("SELECT family_key, kp_id FROM questions WHERE no=?",
                                  (no,)).fetchone()
            if not ((w[0] and w[0] in fam) or w[1] in kps):
                miss.append(no)
        self.assertEqual([], miss, "真实库上这些错题没被覆盖")

    def test_plan_reproducible_on_real_library(self):
        a = train.plan(self.conn, self.wrong, n_per=2, now=NOW)
        b = train.plan(self.conn, list(reversed(self.wrong)), n_per=2, now=NOW)
        self.assertEqual(a, b)


class TestPickerIntegration(unittest.TestCase):
    """plan() 的 nos 能不能真的变成卷子：取决于 picker 是否支持 nos（可选增强）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="train_p_")
        self.conn = dbmod.connect(os.path.join(self.tmp, "p.db"))
        build_fixture(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _load_picker():
        path = os.path.join(APP, "picker.py")
        if not os.path.exists(path):
            return None
        spec = importlib.util.spec_from_file_location("picker_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # select.py 与 stdlib 重名，按路径加载更稳
        return mod

    def test_plan_params_pass_picker_validation(self):
        """plan() 的参数必须能过 picker.normalize_params（CONTRACT §2 合法性）。"""
        picker = self._load_picker()
        if picker is None:
            self.skipTest("picker.py 还没落地")
        p = train.plan(self.conn, WRONG, n_per=2, now=NOW)
        norm = picker.normalize_params(p)
        self.assertEqual(p["mix"], norm["mix"])
        self.assertEqual(p["seed"], norm["seed"])
        self.assertTrue(norm["requirements"]["need_answer"])
        # picker 现在会原样保留未知顶层键（nos/exclude_nos 靠它透传）
        self.assertEqual(p["nos"], norm.get("nos"))
        self.assertEqual(p["exclude_nos"], norm.get("exclude_nos"))

    def test_build_training_paper_if_picker_supports_nos(self):
        picker = self._load_picker()
        if picker is None:
            self.skipTest("picker.py 还没落地")
        with open(os.path.join(APP, "picker.py"), encoding="utf-8") as f:
            src = f.read()
        if '"nos"' not in src and "'nos'" not in src:
            self.skipTest("picker 尚不支持 params['nos']（已告知 paper-engineer），"
                          "plan 已返回 nos/exclude_nos，支持后此测试即可生效")
        p = train.plan(self.conn, WRONG, n_per=2, now=NOW)
        m = picker.build(p, self.conn, now=NOW)
        got = [it["no"] for it in m["items"]]
        self.assertEqual(p["nos"], got, "训练卷必须等于 plan 的题集")
        self.assertEqual([], [no for no in got if no in WRONG])


class TestQualityGate(unittest.TestCase):
    """训练卷候选必须 ⊆ picker 可印池（render-engineer 在 t33 踩到 SelectError）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="train_q_")
        self.conn = dbmod.connect(os.path.join(self.tmp, "q.db"))
        add(self.conn, "W1", "填空", "2.1", "第2章 整式及其加减", None, ans="3")
        add(self.conn, "C_OK", "填空", "2.1", "第2章 整式及其加减", None, ans="4")
        # 题干说「如图」但没有图 → quality 会给 fig_missing
        add(self.conn, "C_FIG", "填空", "2.1", "第2章 整式及其加减", None, ans="5")
        self.conn.execute("UPDATE questions SET text_plain=? WHERE no='C_FIG'",
                          ("如图，求阴影部分的面积，请写出计算过程．",))
        # 教师版残留 → analysis_left（打印级拦截）
        add(self.conn, "C_ANALYSIS", "填空", "2.1", "第2章 整式及其加减", None, ans="6")
        self.conn.execute("UPDATE questions SET text_plain=? WHERE no='C_ANALYSIS'",
                          ("【分析】本题考查整式加减法，先合并同类项再代入计算．",))
        self.conn.commit()
        import quality
        quality.scan(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blocked_candidates_not_picked(self):
        p = train.plan(self.conn, ["W1"], n_per=2, now=NOW)
        self.assertNotIn("C_FIG", p["nos"], "缺图题不能进训练卷（picker 会拒）")
        self.assertNotIn("C_ANALYSIS", p["nos"], "教师版残留不能进训练卷")
        self.assertIn("C_OK", p["nos"])
        self.assertGreaterEqual(p["train"]["quality_filtered"], 2,
                                "被体检挡掉的题数要记账给 UI")

    def test_gate_can_be_disabled(self):
        p = train.plan(self.conn, ["W1"], n_per=2, now=NOW, quality_gate=False)
        self.assertEqual(0, p["train"]["quality_filtered"])
        self.assertGreaterEqual(len(p["nos"]), 1)

    def test_every_picked_question_is_printable(self):
        import quality
        p = train.plan(self.conn, ["W1"], n_per=2, now=NOW)
        ok, rejected = quality.filter_printable(self.conn, p["nos"], need_answer=True)
        self.assertEqual(list(p["nos"]), list(ok), "plan 选的题必须全部可印可判（被拦：%s）" % rejected)


class TestRealPlanPassesPicker(unittest.TestCase):
    """真实库端到端：plan() 的 nos 直接喂 picker.build 不许再抛 SelectError。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(REAL_DB):
            raise unittest.SkipTest("app.db 不存在")
        import sqlite3
        cls.conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        rows = cls.conn.execute(
            "SELECT no FROM questions WHERE is_empty=0 AND answer_raw<>''"
            " AND family_key IS NOT NULL AND family_key<>'' ORDER BY no LIMIT 40").fetchall()
        cls.wrong = [r[0] for r in rows[:10]]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_plan_to_picker_build(self):
        picker = TestPickerIntegration._load_picker()
        if picker is None:
            self.skipTest("picker.py 不在")
        p = train.plan(self.conn, self.wrong, n_per=3, now=NOW)
        try:
            m = picker.build(p, self.conn, now=NOW)
        except Exception as exc:          # noqa: BLE001 - 这里就是要断言不许抛
            self.fail("plan → picker.build 抛异常：%s: %s" % (type(exc).__name__, exc))
        self.assertEqual(p["nos"], [it["no"] for it in m["items"]])
        self.assertEqual([], [n for n in p["nos"] if n in self.wrong])


if __name__ == "__main__":
    unittest.main(verbosity=2)
