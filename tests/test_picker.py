# -*- coding: utf-8 -*-
"""T3 · 组卷选题引擎（picker.py，原名 select.py）：数量 / 可复现 / scope / 答案拆解 / 题量不足。

先红后绿：本文件先于实现写（红 = FileNotFoundError / ImportError）。

模块名注意：原名 select.py 与 stdlib 的 select 扩展模块同名，会把 asyncio/selectors
的 `select.select` 抢掉（`selectors.SelectSelector.select()` → AttributeError）。
2026-10-31 按队长要求改名 picker.py，所以这里可以正常 `import picker`——本测试顺带
守住"不要再改回 select.py"这件事。

覆盖：
  1. answer_key 拆解：选择单字母 / 多选 / 带【知识点】注释；解答 (1)(2) 小问，
     分数 (4)/(3) 不被误拆；填空多空（2+ 空格 / 分号），括号内的分号不拆；
     无答案 → [] 且 practice_only=true。
  2. build(params, conn) 按 mix 抽题：数量、题型、题序 1..N、按题型分组、无重复。
  3. manifest 结构严格按 CONTRACT §3：page 常量、params 回填、items 不含 card、
     可 JSON 落库；且 build 不改入参。
  4. seed 可复现（同参数同 seed 逐字节一致）；换 seed 重复率 < 20%。
  5. scope：chapters / kps 过滤生效、两个都空 = 不限、x.0 章级、未知章节/知识点报错。
  6. requirements：need_answer 默认 true（只取可用答案）；need_answer=false 时无答案题
     以 practice_only=true + answer_key=[] 出现；exclude_no_family；is_empty=1 永不入选。
  7. 题量不足：明确到题型 + 需要/可用/差多少，抛异常而不是静默凑数。
  8. save() 落 papers 表；pid 按日期递增。
  9. 真实库（app/data/app.db）集成：跑通默认卷 + scope + 可复现（不写死题库行数，
   因为题库会按质量门裁剪；断言一律跟 count_available 自洽）。
"""
from __future__ import annotations

import copy
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
import picker as sel  # noqa: E402  （T3 原 select.py；改名后不再撞 stdlib）

FIXED_NOW = "2026-10-31T14:00:00+08:00"
FIXED_PID = "P20261031-900"
CH1 = "第1章 有理数"
CH2 = "第2章 整式及其加减"
CH3 = "第3章 一次方程与方程组"
REAL_DB = os.path.join(APP, "data", "app.db")

QCOLS = ("no qtype option_count sub_count is_empty media_count text_len text_plain "
         "tokens_json fmt src_title src_para family_key kp_id chapter kp kp_source "
         "answer_raw check_status").split()


# ---------------------------------------------------------------- 夹具库
def _insert_q(conn, no, qtype, chapter, kp_id, answer="", family=None, is_empty=0,
              option_count=0, tokens=None, text=None, fmt="plain", subject=None):
    vals = {
        "no": no, "qtype": qtype, "option_count": option_count, "sub_count": 0,
        "is_empty": is_empty, "media_count": 0, "text_len": 10,
        "text_plain": "题干" + no if text is None else text,
        "tokens_json": json.dumps(tokens or [], ensure_ascii=False),
        "fmt": fmt, "src_title": "", "src_para": 0, "family_key": family,
        "kp_id": kp_id, "chapter": chapter, "kp": "", "kp_source": "title",
        "answer_raw": answer, "check_status": "",
    }
    conn.execute("INSERT INTO questions(%s) VALUES(%s)"
                 % (",".join(QCOLS), ",".join("?" * len(QCOLS))),
                 [vals[c] for c in QCOLS])
    if subject:                     # t11 · 多学科：不传就走表默认值 math7a
        conn.execute("UPDATE questions SET subject_pk=? WHERE no=?", (subject, no))


def make_pool_db(path):
    """一个可控的小题库：每种题型 300 题，覆盖多章/多知识点/有答案无答案/空题。

    选择 10000..10299：100 题 1.1、100 题 1.2（第1章）、100 题 2.1（第2章），答案 A/B/C/D 轮转，
                       偶数题无题族（family_key=None）
    填空 11000..11299：前 150 题 2.3（第2章）、后 150 题 3.1（第3章）；答案轮转
                       ["3", "2  -5", ""]（每 3 题 1 题无答案）
    解答 12000..12299：3.2（第3章）；答案轮转 ["(1)x=4\\n(2)x=9", "", "a的值为1．"]
    选择 13000..13009：is_empty=1 的空题（任何情况下都不该被选中）
    """
    conn = dbmod.connect(path)
    for i in range(300):
        no = "%05d" % (10000 + i)
        if i < 100:
            ch, kp = CH1, "1.1"
        elif i < 200:
            ch, kp = CH1, "1.2"
        else:
            ch, kp = CH2, "2.1"
        _insert_q(conn, no, "选择", ch, kp, "ABCD"[i % 4],
                  family=("fam%02d" % (i % 40) if i % 2 else None),
                  option_count=4, tokens=[{"kind": "text", "text": "题干 " + no}])
    for i in range(300):
        no = "%05d" % (11000 + i)
        ch, kp = (CH2, "2.3") if i < 150 else (CH3, "3.1")
        ans = ["3", "2  -5", ""][i % 3]
        _insert_q(conn, no, "填空", ch, kp, ans,
                  tokens=[{"kind": "text", "text": "填空 " + no}])
    for i in range(300):
        no = "%05d" % (12000 + i)
        ans = ["(1)x=4\n(2)x=9", "", "a的值为1．"][i % 3]
        _insert_q(conn, no, "解答", CH3, "3.2", ans,
                  tokens=[{"kind": "math", "xml": "<m:oMath/>"}])
    for i in range(10):
        _insert_q(conn, "%05d" % (13000 + i), "选择", CH1, "1.2", "A",
                  family="famX", is_empty=1, option_count=4, tokens=[])
    for kp, ch in (("1.0", CH1), ("1.1", CH1), ("1.2", CH1), ("2.0", CH2),
                   ("2.1", CH2), ("2.3", CH2), ("3.0", CH3), ("3.1", CH3),
                   ("3.2", CH3)):
        conn.execute("INSERT INTO kps(kp_id, chapter, kp, n_questions) VALUES(?,?,?,?)",
                     (kp, ch, "kp" + kp, 0))
    conn.commit()
    return conn


def params(mix=None, scope=None, req=None, seed=42, space=None, title="测试卷", nos=None):
    p = {"title": title, "mix": mix if mix is not None else {"选择": 5, "填空": 4, "解答": 3},
         "seed": seed}
    if scope is not None:
        p["scope"] = scope
    if req is not None:
        p["requirements"] = req
    if space is not None:
        p["space"] = space
    if nos is not None:
        p["nos"] = nos
    return p


# ---------------------------------------------------------------- 1. answer_key
class AnswerKeyTest(unittest.TestCase):
    """answer_raw → answer_key 的拆解（判卷侧只比最终答案，拆错就判错）。"""

    def keys(self, raw, qtype):
        return sel.parse_answer_key(raw, qtype)

    def test_choice_single_letter(self):
        self.assertEqual(self.keys("A", "选择"),
                         [{"blank": 1, "value": "A", "norm": "A"}])

    def test_choice_strips_annotation_block(self):
        # 语料里 168 条 answer_raw 尾部粘了【知识点】/【易错总结】，答案本身在前一行
        self.assertEqual(self.keys("B\n【知识点】合并同类项", "选择"),
                         [{"blank": 1, "value": "B", "norm": "B"}])

    def test_choice_strips_trailing_fullwidth_period(self):
        self.assertEqual(self.keys("D．", "选择"),
                         [{"blank": 1, "value": "D", "norm": "D"}])

    def test_choice_multi_letter(self):
        self.assertEqual(self.keys("ACD", "选择"),
                         [{"blank": 1, "value": "ACD", "norm": "ACD"}])

    def test_choice_non_letter_is_unusable(self):
        # qtype 标错成"选择"、答案其实是解答内容 → 不可判，宁可判为无答案
        self.assertEqual(self.keys("(1)3a−b\n(2)−2b\n(3)B", "选择"), [])

    def test_empty_answer(self):
        for qtype in ("选择", "填空", "解答"):
            self.assertEqual(self.keys("", qtype), [])
            self.assertEqual(self.keys("   \n", qtype), [])

    def test_solution_sub_questions(self):
        ks = self.keys("(1)x=4\n(2)x=9\n(3)x=13", "解答")
        self.assertEqual([k["blank"] for k in ks], [1, 2, 3])
        self.assertEqual([k["value"] for k in ks], ["x=4", "x=9", "x=13"])
        self.assertEqual([k["norm"] for k in ks], ["x=4", "x=9", "x=13"])

    def test_solution_fullwidth_markers(self):
        ks = self.keys("（1）x+5\n（2）k=1", "解答")
        self.assertEqual([k["value"] for k in ks], ["x+5", "k=1"])

    def test_solution_fraction_not_split(self):
        # (4)/(3) 是分数，不是第 4 小问
        ks = self.keys("x=(4)/(3)", "解答")
        self.assertEqual(len(ks), 1)
        self.assertEqual(ks[0]["value"], "x=(4)/(3)")

    def test_solution_sub_then_fraction(self):
        ks = self.keys("(1)−5\n(2)(17)/(2)", "解答")
        self.assertEqual([k["value"] for k in ks], ["−5", "(17)/(2)"])

    def test_solution_single_answer(self):
        ks = self.keys("a的值为1．", "解答")
        self.assertEqual(len(ks), 1)
        self.assertEqual(ks[0]["value"], "a的值为1")

    def test_fill_multiple_blanks_by_spaces(self):
        ks = self.keys("2  -5", "填空")
        self.assertEqual([k["value"] for k in ks], ["2", "-5"])

    def test_fill_multiple_blanks_by_semicolon(self):
        ks = self.keys("8；16", "填空")
        self.assertEqual([k["value"] for k in ks], ["8", "16"])

    def test_fill_blanks_keep_blank_numbering(self):
        ks = self.keys("5x；EB；7x；(1)/(2)AB或BC", "填空")
        self.assertEqual([k["blank"] for k in ks], [1, 2, 3, 4])
        self.assertIn("AB或BC", ks[3]["value"])

    def test_fill_wrapped_pair_is_one_blank(self):
        # 方程组的解 (x=4; y=−2) 是「一空」，括号内的分号不能拆
        ks = self.keys("(x=4; y=−2)", "填空")
        self.assertEqual(len(ks), 1)
        self.assertEqual(ks[0]["norm"], "(x=4;y=-2)")

    def test_fill_annotation_block(self):
        ks = self.keys("0\n【知识点】数轴上两点之间的距离", "填空")
        self.assertEqual(len(ks), 1)
        self.assertEqual(ks[0]["value"], "0")

    def test_fill_single_value(self):
        self.assertEqual([k["value"] for k in self.keys("-1", "填空")], ["-1"])

    def test_blank_counts_stay_within_card_capacity(self):
        # card.py：填空最多 12 空、解答最多 20 空；超了必须合并，不能丢答案
        for raw, qtype, cap in (("；".join("v%d" % i for i in range(30)), "填空", 12),
                                ("".join("(%d)v%d" % (i + 1, i) for i in range(30)),
                                 "解答", 20)):
            ks = self.keys(raw, qtype)
            self.assertLessEqual(len(ks), cap, "空数超过 card 容量")
            self.assertEqual([k["blank"] for k in ks], list(range(1, len(ks) + 1)))


# ---------------------------------------------------------------- 2. mix / 结构
class BuildMixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_mix_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_counts_match_mix(self):
        m = sel.build(params({"选择": 7, "填空": 4, "解答": 3}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        items = m["items"]
        self.assertEqual(len(items), 14)
        by = {}
        for it in items:
            by[it["qtype"]] = by.get(it["qtype"], 0) + 1
        self.assertEqual(by, {"选择": 7, "填空": 4, "解答": 3})

    def test_idx_and_grouping(self):
        m = sel.build(params({"选择": 5, "填空": 4, "解答": 3}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        items = m["items"]
        self.assertEqual([it["idx"] for it in items], list(range(1, len(items) + 1)),
                         "题序必须 1..N 连续")
        self.assertEqual([it["qtype"] for it in items],
                         ["选择"] * 5 + ["填空"] * 4 + ["解答"] * 3,
                         "按题型分组（卷面版式依赖这个顺序）")

    def test_no_duplicate_question(self):
        m = sel.build(params({"选择": 30, "填空": 30, "解答": 30}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        nos = [it["no"] for it in m["items"]]
        self.assertEqual(len(nos), len(set(nos)), "同一份卷里不能重复选题")

    def test_selected_question_matches_requested_qtype(self):
        m = sel.build(params({"选择": 20, "填空": 20, "解答": 20}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        for it in m["items"]:
            row = self.conn.execute("SELECT qtype, is_empty FROM questions WHERE no=?",
                                    (it["no"],)).fetchone()
            self.assertIsNotNone(row, "选中的题必须真的在库里")
            self.assertEqual(row["qtype"], it["qtype"])
            self.assertEqual(row["is_empty"], 0, "空题永不入选")

    def test_options_for_choice(self):
        m = sel.build(params({"选择": 6}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        for it in m["items"]:
            # 题库里既有 4 选项题也有 3 选项题（导入件），只要选项是连续字母开头就对
            opts = it["options"]
            self.assertIn(len(opts), (2, 3, 4), "选项数异常：%r" % opts)
            self.assertEqual(opts, list("ABCD"[:len(opts)]), "选项字母应从 A 起连续")

    def test_space_paras_from_params(self):
        m = sel.build(params({"选择": 3, "填空": 3, "解答": 3},
                             space={"选择": 0, "填空": 2, "解答": 8}),
                      self.conn, now=FIXED_NOW, pid=FIXED_PID)
        want = {"选择": 0, "填空": 2, "解答": 8}
        for it in m["items"]:
            self.assertEqual(it["space_paras"], want[it["qtype"]])

    def test_space_defaults_to_zero(self):
        m = sel.build(params({"解答": 2}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertTrue(all(it["space_paras"] == 0 for it in m["items"]))


class ManifestShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_shape_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))
        cls.p = params({"选择": 5, "填空": 4, "解答": 3},
                       scope={"chapters": [CH1, CH3], "kps": [], "kp_mode": "any"},
                       space={"选择": 0, "填空": 2, "解答": 8},
                       title="七年级数学 · 训练卷 A")
        cls.m = sel.build(cls.p, cls.conn, now=FIXED_NOW, pid=FIXED_PID)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_top_level_keys(self):
        for k in ("pid", "created_at", "title", "params", "page", "items"):
            self.assertIn(k, self.m)
        self.assertEqual(self.m["pid"], FIXED_PID)
        self.assertEqual(self.m["created_at"], FIXED_NOW)
        self.assertEqual(self.m["title"], "七年级数学 · 训练卷 A")
        self.assertNotIn("card", self.m, "select.build 不产出 card（card.py 的活）")

    def test_page_matches_contract(self):
        self.assertEqual(self.m["page"]["w_pt"], 595.3)
        self.assertEqual(self.m["page"]["h_pt"], 841.9)
        self.assertEqual(self.m["page"]["dpi"], 300)
        self.assertEqual(self.m["page"]["w_px"], 2480)
        self.assertEqual(self.m["page"]["h_px"], 3508)

    def test_item_keys(self):
        need = ("idx", "no", "qtype", "kp_id", "family_key", "stem_tokens",
                "answer_key", "options", "space_paras")
        for it in self.m["items"]:
            for k in need:
                self.assertIn(k, it, "manifest item 缺字段 %s" % k)
            self.assertNotIn("card", it, "item 里不能有 card 字段")
            self.assertIsInstance(it["stem_tokens"], list)
            self.assertTrue(all(isinstance(t, dict) and "kind" in t
                                for t in it["stem_tokens"]))

    def test_params_echoed(self):
        p = self.m["params"]
        self.assertEqual(p["mix"], {"选择": 5, "填空": 4, "解答": 3})
        self.assertEqual(p["seed"], 42)
        self.assertEqual(p["space"]["解答"], 8)
        self.assertEqual(p["scope"]["chapters"], [CH1, CH3])
        self.assertEqual(p["requirements"]["need_answer"], True)
        self.assertEqual(p["requirements"]["exclude_no_family"], False)

    def test_manifest_is_json_safe(self):
        s = json.dumps(self.m, ensure_ascii=False)
        self.assertEqual(json.loads(s), self.m)
        self.assertGreater(len(s), 100)

    def test_build_does_not_mutate_params(self):
        p = params({"选择": 3})
        before = copy.deepcopy(p)
        sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(p, before, "build 不能改调用方传进来的 params")


# ---------------------------------------------------------------- 3. seed
class SeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_seed_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _nos(self, seed):
        m = sel.build(params({"选择": 10, "填空": 8, "解答": 6}, seed=seed),
                      self.conn, now=FIXED_NOW, pid=FIXED_PID)
        return [(it["idx"], it["no"], it["qtype"]) for it in m["items"]]

    def test_same_seed_same_paper(self):
        a = sel.build(params({"选择": 10, "填空": 8, "解答": 6}, seed=42), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        b = sel.build(params({"选择": 10, "填空": 8, "解答": 6}, seed=42), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True),
                         "同参数同 seed 必须逐字节一致")

    def test_different_seed_different_paper(self):
        self.assertNotEqual(self._nos(42), self._nos(43))

    def test_overlap_between_seeds_under_20pct(self):
        a = {no for _, no, _ in self._nos(42)}
        b = {no for _, no, _ in self._nos(43)}
        overlap = len(a & b) / float(len(a))
        self.assertLess(overlap, 0.20, "换 seed 重复率应 <20%%，实测 %.1f%%" % (100 * overlap))

    def test_seed_changes_only_its_own_qtype(self):
        # 只改选择的数量，不该影响填空/解答抽到哪些题（每种题型各用 Random(seed)）
        a = sel.build(params({"选择": 5, "填空": 8, "解答": 6}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        b = sel.build(params({"选择": 50, "填空": 8, "解答": 6}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        keep = lambda m, qt: [it["no"] for it in m["items"] if it["qtype"] == qt]
        self.assertEqual(keep(a, "填空"), keep(b, "填空"))
        self.assertEqual(keep(a, "解答"), keep(b, "解答"))

    def test_seed_default_is_deterministic(self):
        p = {"title": "x", "mix": {"选择": 5}}
        a = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        b = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual([it["no"] for it in a["items"]],
                         [it["no"] for it in b["items"]])
        self.assertIsInstance(a["params"]["seed"], int)

    def test_same_seed_same_items_even_without_fixed_clock(self):
        # 不给 now/pid（真实调用路径）：卷子内容也必须逐字节一致，只有 pid/created_at 可能不同
        p = params({"选择": 6, "填空": 4, "解答": 2}, seed=99)
        a = sel.build(p, self.conn)
        b = sel.build(p, self.conn)
        self.assertEqual(json.dumps(a["items"], ensure_ascii=False, sort_keys=True),
                         json.dumps(b["items"], ensure_ascii=False, sort_keys=True))


# ---------------------------------------------------------------- 4. scope
class ScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_scope_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, mix, scope=None, req=None):
        return sel.build(params(mix, scope=scope, req=req), self.conn,
                         now=FIXED_NOW, pid=FIXED_PID)

    def test_empty_scope_means_unlimited(self):
        m = self.build({"选择": 300}, scope={"chapters": [], "kps": []})
        self.assertEqual(len(m["items"]), 300, "scope 全空 = 不限，300 题都该能抽到")
        self.assertEqual(len({it["chapter"] for it in m["items"]}), 2,
                         "不限 scope 时应跨章节")

    def test_no_scope_key_is_unlimited(self):
        m = self.build({"选择": 300})
        self.assertEqual(len(m["items"]), 300)

    def test_chapters_filter(self):
        m = self.build({"选择": 30, "填空": 20}, scope={"chapters": [CH2]})
        self.assertEqual({it["chapter"] for it in m["items"]}, {CH2})

    def test_chapters_filter_limit_pool(self):
        # 第3章没有选择题 → 只要选择题就必然报题量不足（说明过滤真的生效了）
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            self.build({"选择": 1}, scope={"chapters": [CH3]})
        self.assertEqual(ctx.exception.details["选择"]["available"], 0)

    def test_kps_filter(self):
        m = self.build({"选择": 20}, scope={"kps": ["1.1"]})
        self.assertEqual({it["kp_id"] for it in m["items"]}, {"1.1"})

    def test_kp_chapter_level_x0(self):
        # 1.0 = 章级：第1章的全部具体知识点（1.1 + 1.2）
        m = self.build({"选择": 200}, scope={"kps": ["1.0"]})
        self.assertEqual(len(m["items"]), 200)
        self.assertEqual({it["kp_id"] for it in m["items"]}, {"1.1", "1.2"})

    def test_chapters_and_kps_or(self):
        # kp_mode=any：命中任一条件即入选
        m = self.build({"选择": 50},
                       scope={"chapters": [CH2], "kps": ["1.2"], "kp_mode": "any"})
        got = {(it["chapter"], it["kp_id"]) for it in m["items"]}
        self.assertTrue(got <= {(CH2, "2.1"), (CH1, "1.2")}, got)
        self.assertTrue(any(kp == "1.2" for _, kp in got))
        self.assertTrue(any(kp == "2.1" for _, kp in got))

    def test_chapters_and_kps_all(self):
        m = self.build({"选择": 20},
                       scope={"chapters": [CH1], "kps": ["1.2"], "kp_mode": "all"})
        self.assertEqual({(it["chapter"], it["kp_id"]) for it in m["items"]},
                         {(CH1, "1.2")})

    def test_unknown_chapter_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.build({"选择": 1}, scope={"chapters": ["第9章 不存在"]})
        self.assertIn("第9章 不存在", str(ctx.exception))

    def test_unknown_kp_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.build({"选择": 1}, scope={"kps": ["9.9"]})
        self.assertIn("9.9", str(ctx.exception))

    def test_bad_kp_mode_raises(self):
        with self.assertRaises(ValueError):
            self.build({"选择": 1}, scope={"kps": ["1.1"], "kp_mode": "some"})

    def test_available_reports_pool(self):
        av = sel.count_available(self.conn, params({"选择": 1}))
        self.assertEqual(av["选择"], 300)
        self.assertEqual(av["填空"], 200, "默认 need_answer=true：每 3 题 1 题无答案")
        self.assertEqual(av["解答"], 200)


# ---------------------------------------------------------------- 5. requirements
class RequirementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_req_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_need_answer_default_true(self):
        m = sel.build(params({"填空": 200}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        for it in m["items"]:
            self.assertTrue(it["answer_key"], "need_answer 默认 true，选中题必须有答案")
            self.assertFalse(it["practice_only"])

    def test_need_answer_excludes_empty_answer_rows(self):
        m = sel.build(params({"填空": 200}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        picked = {it["no"] for it in m["items"]}
        no_answer = {"%05d" % (11000 + i) for i in range(300) if i % 3 == 2}
        self.assertEqual(picked & no_answer, set(), "无答案题不能出现在判分卷里")

    def test_need_answer_false_allows_practice_only(self):
        m = sel.build(params({"填空": 300}, req={"need_answer": False}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        po = [it for it in m["items"] if it["practice_only"]]
        self.assertTrue(po, "need_answer=false 时应能选到无答案题")
        for it in po:
            self.assertEqual(it["answer_key"], [], "无答案题 answer_key 必须是 []")
        self.assertEqual(len(po), 100, "夹具里 300 题填空有 100 题无答案")

    def test_exclude_no_family(self):
        m = sel.build(params({"选择": 150}, req={"exclude_no_family": True}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertTrue(all(it["family_key"] for it in m["items"]))
        self.assertLessEqual(len(m["items"]), 150)

    def test_exclude_no_family_pool_count(self):
        av = sel.count_available(self.conn, params({"选择": 1},
                                                  req={"exclude_no_family": True}))
        self.assertEqual(av["选择"], 150, "夹具里 300 道选择题一半没有题族")

    def test_empty_question_never_selected(self):
        m = sel.build(params({"选择": 300}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        picked = {it["no"] for it in m["items"]}
        self.assertEqual(picked & {"%05d" % (13000 + i) for i in range(10)}, set(),
                         "is_empty=1 的题没有题干，任何参数下都不入选")

    def test_choice_non_letter_answer_not_used_for_grading(self):
        conn = self.conn
        conn.execute("UPDATE questions SET answer_raw='(1)3a−b' WHERE no='10001'")
        conn.commit()
        av = sel.count_available(conn, params({"选择": 1}, scope={"kps": ["1.1"]}))
        self.assertEqual(av["选择"], 99, "答案不是选项字母的题不进判分池")
        conn.execute("UPDATE questions SET answer_raw='B' WHERE no='10001'")
        conn.commit()

    def test_exclude_empty_false_is_rejected_not_ignored(self):
        # 契约里有这个开关，但空题没有题干、印出来是空白题：明确报错，不静默忽略、也不塞进卷子
        with self.assertRaises(ValueError) as ctx:
            sel.build(params({"选择": 3}, req={"exclude_empty": False}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("exclude_empty", str(ctx.exception))


# ---------------------------------------------------------------- 6. 题量不足
class InsufficientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_short_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_shortfall_reports_qtype_and_counts(self):
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            sel.build(params({"选择": 5000}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        e = ctx.exception
        self.assertIsInstance(e, ValueError)
        msg = str(e)
        self.assertIn("选择", msg)
        self.assertIn("需要 5000", msg)
        self.assertIn("可用 300", msg)
        self.assertIn("差 4700", msg)
        self.assertEqual(e.details["选择"], {"need": 5000, "available": 300, "short": 4700})

    def test_shortfall_lists_every_short_qtype(self):
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            sel.build(params({"选择": 301, "填空": 9999, "解答": 3}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        d = ctx.exception.details
        self.assertEqual(set(d), {"选择", "填空"}, "只报真正缺的题型")
        self.assertEqual(d["填空"]["short"], 9999 - 200)
        self.assertNotIn("解答", str(ctx.exception))

    def test_no_partial_paper_on_shortfall(self):
        # 报错而不是静默凑数：异常里不能带半份卷
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            sel.build(params({"选择": 5, "解答": 9999}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertGreater(ctx.exception.details["解答"]["short"], 0)

    def test_zero_pool_qtype(self):
        # 语料里判断 2 题但 0 答案 → need_answer 下池子为空
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            sel.build(params({"判断": 1}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(ctx.exception.details["判断"]["available"], 0)

    def test_empty_mix_rejected(self):
        with self.assertRaises(ValueError):
            sel.build(params({"选择": 0, "填空": 0}), self.conn, now=FIXED_NOW, pid=FIXED_PID)

    def test_unknown_qtype_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            sel.build(params({"选择题": 3}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("选择题", str(ctx.exception))
        self.assertIn("选择", str(ctx.exception))


# ---------------------------------------------------------------- 7. pid / save
class SaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_save_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_pid_format(self):
        m = sel.build(params({"选择": 2}), self.conn, now=FIXED_NOW)
        self.assertRegex(m["pid"], r"^P\d{8}-\d{3}$")

    def test_pid_increments_per_day(self):
        a = sel.build(params({"选择": 2}), self.conn, now=FIXED_NOW)
        sel.save(self.conn, a, os.path.join(self.tmp, "a.docx"), os.path.join(self.tmp, "a.pdf"))
        b = sel.build(params({"选择": 2}), self.conn, now=FIXED_NOW)
        self.assertEqual(int(b["pid"].rsplit("-", 1)[1]),
                         int(a["pid"].rsplit("-", 1)[1]) + 1)

    def test_save_writes_papers_row(self):
        m = sel.build(params({"选择": 3, "填空": 2}, title="落库测试"), self.conn,
                      now=FIXED_NOW, pid="P20261031-777")
        docx_path = os.path.join(self.tmp, "p.docx")
        pdf_path = os.path.join(self.tmp, "p.pdf")
        sel.save(self.conn, m, docx_path, pdf_path)
        row = self.conn.execute("SELECT * FROM papers WHERE pid=?", (m["pid"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "落库测试")
        self.assertEqual(row["n_items"], 5)
        self.assertEqual(row["docx_path"], docx_path)
        self.assertEqual(row["pdf_path"], pdf_path)
        self.assertEqual(json.loads(row["manifest"]), m)
        self.assertEqual(json.loads(row["params_json"]), m["params"])

    def test_save_upserts(self):
        m = sel.build(params({"选择": 2}), self.conn, now=FIXED_NOW, pid="P20261031-778")
        sel.save(self.conn, m, "a.docx", "a.pdf")
        m2 = dict(m)
        m2["title"] = "改名"
        sel.save(self.conn, m2, "b.docx", "b.pdf")
        rows = self.conn.execute("SELECT title, docx_path FROM papers WHERE pid=?",
                                 (m["pid"],)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "改名")


# ---------------------------------------------------------------- 7b. 精确题集 / 拉黑
class NosTest(unittest.TestCase):
    """CONTRACT §2 的 nos（精确题集，训练卷用）与 exclude_nos（拉黑）。"""

    NOS = ["11001", "10005", "12000"]   # 填空 / 选择 / 解答，故意不按题型排序

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_nos_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "pool.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_nos_exact_and_order(self):
        m = sel.build({"title": "训练卷", "nos": self.NOS}, self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual([it["no"] for it in m["items"]], self.NOS,
                         "nos 必须按给定顺序原样出卷")
        self.assertEqual([it["idx"] for it in m["items"]], [1, 2, 3])
        self.assertEqual(m["params"]["mix"], {"选择": 1, "填空": 1, "解答": 1},
                         "没给 mix 时应由 nos 推导")

    def test_nos_with_consistent_mix(self):
        m = sel.build({"nos": self.NOS, "mix": {"选择": 1, "填空": 1, "解答": 1}},
                      self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual([it["no"] for it in m["items"]], self.NOS)

    def test_nos_inconsistent_mix_raises(self):
        with self.assertRaises(ValueError) as ctx:
            sel.build({"nos": self.NOS, "mix": {"选择": 3}}, self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        msg = str(ctx.exception)
        self.assertIn("不一致", msg)
        self.assertIn("选择", msg)

    def test_nos_qtype_not_in_mix_raises(self):
        with self.assertRaises(ValueError):
            sel.build({"nos": ["10005"], "mix": {"填空": 1}}, self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)

    def test_nos_unknown_no_raises(self):
        with self.assertRaises(ValueError) as ctx:
            sel.build({"nos": ["10005", "99999"]}, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("99999", str(ctx.exception))
        self.assertIn("不存在", str(ctx.exception))

    def test_nos_duplicate_collapses(self):
        m = sel.build({"nos": ["10005", "10005", "10006"]}, self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual([it["no"] for it in m["items"]], ["10005", "10006"])

    def test_nos_without_answer_conflicts_with_need_answer(self):
        with self.assertRaises(ValueError) as ctx:
            sel.build({"nos": ["11002"]}, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("11002", str(ctx.exception))
        self.assertIn("答案", str(ctx.exception))

    def test_nos_without_answer_ok_for_practice_paper(self):
        m = sel.build({"nos": ["11002"], "requirements": {"need_answer": False}},
                      self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(len(m["items"]), 1)
        self.assertTrue(m["items"][0]["practice_only"])
        self.assertEqual(m["items"][0]["answer_key"], [])

    def test_nos_and_exclude_overlap_raises(self):
        with self.assertRaises(ValueError) as ctx:
            sel.build({"nos": self.NOS, "exclude_nos": ["10005"]}, self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("10005", str(ctx.exception))

    def test_exclude_nos_shrinks_random_pool(self):
        excluded = ["%05d" % (10000 + i) for i in range(10)]
        m = sel.build({"mix": {"选择": 290}, "exclude_nos": excluded}, self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        picked = {it["no"] for it in m["items"]}
        self.assertEqual(len(picked), 290)
        self.assertEqual(picked & set(excluded), set(), "拉黑的题号不能出现")
        av = sel.count_available(self.conn, {"mix": {"选择": 1}, "exclude_nos": excluded})
        self.assertEqual(av["选择"], 290)

    def test_exclude_nos_keeps_seed_reproducible(self):
        p = {"mix": {"选择": 10}, "exclude_nos": ["10001", "10002"]}
        a = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        b = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual([it["no"] for it in a["items"]],
                         [it["no"] for it in b["items"]])


# ---------------------------------------------------------------- 7c. quality_flags 过滤
def make_quality_db(path):
    """12 道题 + quality_flags：覆盖 print_blocking / answer_mismatch / strict / 缺图。"""
    conn = dbmod.connect(path)
    specs = [
        ("20001", "no_stem"),
        ("20002", "no_stem"),
        ("20003", "no_stem"),
        ("20004", "analysis_left"),
        ("20005", "analysis_left"),
        ("20006", "answer_mismatch"),
        ("20007", "short_stem"),
        ("20008", "heavy_layout"),
        ("20009", "dup_text"),
        ("20010", ""),
        ("20011", ""),
    ]
    for no, flags in specs:
        _insert_q(conn, no, "选择", CH1, "1.1", "A", option_count=4,
                  tokens=[{"kind": "text", "text": "题干 " + no}])
        if flags:
            conn.execute("INSERT INTO quality_flags(no, flags, detail) VALUES(?,?,?)",
                         (no, flags, "{}"))
    # 缺图题：题干说「如图」，token 里却没有 embed/table（源 docx 也没图）
    _insert_q(conn, "20012", "选择", CH1, "1.2", "B", option_count=4,
              text="如图，下列哪个数轴画得对", tokens=[{"kind": "text", "text": "如图，下列哪个数轴画得对"}])
    # 对照：同样说「如图」，但 fmt 里有 embed（真有图），不许误杀
    _insert_q(conn, "20013", "选择", CH1, "1.2", "C", option_count=4,
              text="如图所示，求阴影面积", tokens=[{"kind": "text", "text": "如图所示，求阴影面积"},
                                            {"kind": "embed", "xml": "<w:drawing/>", "n_media": 1}],
              fmt="omml+embed")
    # 对照：图在表格里（fmt 带 table），也不算缺图
    _insert_q(conn, "20014", "选择", CH1, "1.2", "D", option_count=4,
              text="如图表格所示", tokens=[{"kind": "table", "xml": "<w:tbl/>", "rows": 1, "cols": 1}],
              fmt="table")
    conn.commit()
    return conn


class QualityFlagsTest(unittest.TestCase):
    """t20：picker 接 quality_flags（默认排除不可打印 / 答案不符的题）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_qf_")
        cls.conn = make_quality_db(os.path.join(cls.tmp, "qf.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, mix=None, req=None, scope=None):
        return sel.build(params(mix or {"选择": 14}, req=req, scope=scope), self.conn,
                         now=FIXED_NOW, pid=FIXED_PID)

    def test_print_blocking_excluded_by_default(self):
        av = sel.count_available(self.conn, params({"选择": 1}))
        # 14 题里 3 no_stem + 2 analysis_left + 1 answer_mismatch + 1 缺图 默认被排除
        self.assertEqual(av["选择"], 7)
        m = self.build({"选择": 7})
        picked = {it["no"] for it in m["items"]}
        self.assertEqual(picked & {"20001", "20002", "20003", "20004", "20005"}, set(),
                         "no_stem / analysis_left 不许进卷面")
        self.assertEqual(m["stats"]["excluded"]["print_blocking"], 5)
        self.assertEqual(m["stats"]["excluded"]["mismatch"], 1)
        self.assertEqual(m["stats"]["excluded"]["fig_missing"], 1)

    def test_mismatch_follows_need_answer(self):
        m = self.build({"选择": 7})
        self.assertNotIn("20006", {it["no"] for it in m["items"]})
        # 关掉 need_answer 时默认不再排除答案不符（练习卷）
        m2 = self.build({"选择": 8}, req={"need_answer": False})
        self.assertEqual(m2["stats"]["excluded"]["mismatch"], 0)
        self.assertIn("20006", {it["no"] for it in m2["items"]})

    def test_exclude_mismatch_explicit_off(self):
        m = self.build({"选择": 8}, req={"exclude_mismatch": False})
        self.assertIn("20006", {it["no"] for it in m["items"]})

    def test_strict_switch(self):
        m = self.build({"选择": 7})
        self.assertEqual(m["stats"]["excluded"]["strict"], 0, "默认不排 short_stem/heavy_layout/dup_text")
        self.assertIn("20007", {it["no"] for it in m["items"]})
        m2 = self.build({"选择": 4}, req={"strict": True})
        picked = {it["no"] for it in m2["items"]}
        self.assertEqual(picked & {"20007", "20008", "20009"}, set())
        self.assertEqual(m2["stats"]["excluded"]["strict"], 3)

    def test_fig_missing_excluded_by_default(self):
        av = sel.count_available(self.conn, params({"选择": 1}))
        m = self.build({"选择": 7})
        self.assertNotIn("20012", {it["no"] for it in m["items"]},
                         "题干说「如图」但没图的题必须默认排除")
        self.assertIn("20013", {it["no"] for it in m["items"]}, "真有图的题不能误杀")
        self.assertIn("20014", {it["no"] for it in m["items"]}, "图在表格里的题不能误杀")
        self.assertEqual(m["stats"]["excluded"]["fig_missing"], 1)
        self.assertEqual(av["选择"], 7)

    def test_fig_missing_catches_rushia_tu(self):
        """「按规律拼成如下的图案」也算图引用（真卷里 01034 就是图丢了还进卷）。"""
        self.assertTrue(sel.fig_missing("正方形纸片按规律拼成如下的图案，第（ ）个图案中恰好有365个纸片", False))
        self.assertTrue(sel.fig_missing("如下图，求阴影面积", False))
        self.assertFalse(sel.fig_missing("正方形纸片按规律拼成如下的图案", True), "有图的题不能误杀")
        self.assertFalse(sel.fig_missing("下列各式中正确的是（ ）", False), "没提图不能误杀")

    def test_fig_missing_can_be_relaxed(self):
        m = self.build({"选择": 8}, req={"exclude_fig_missing": False})
        self.assertIn("20012", {it["no"] for it in m["items"]})
        self.assertEqual(m["stats"]["excluded"]["fig_missing"], 0)

    def test_quality_table_missing_degrades(self):
        tmp = tempfile.mkdtemp(prefix="sel_noqf_")
        try:
            path = os.path.join(tmp, "noqf.db")
            conn = dbmod.connect(path)
            _insert_q(conn, "21001", "选择", CH1, "1.1", "A", option_count=4,
                      tokens=[{"kind": "text", "text": "题干"}])
            conn.execute("DROP TABLE quality_flags")
            conn.commit()
            m = sel.build(params({"选择": 1}), conn, now=FIXED_NOW, pid=FIXED_PID)
            self.assertEqual(len(m["items"]), 1, "没有 quality_flags 表时不能崩，也不能全排空")
            self.assertEqual(m["stats"]["excluded"]["print_blocking"], 0)
            rep = sel.pool_report(conn, params({"选择": 1}))
            self.assertFalse(rep["quality_table"], "pool_report 要告诉 UI 体检表在不在")
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_count_available_matches_build(self):
        av = sel.count_available(self.conn, params({"选择": 1}))
        rep = sel.pool_report(self.conn, params({"选择": 1}))
        self.assertEqual(av, rep["available"])
        m = self.build({"选择": av["选择"]})
        self.assertEqual(len(m["items"]), av["选择"], "显示能出 N 题就必须真能出 N 题")

    def test_shortfall_hint_mentions_relax(self):
        # 1.2 只有一道缺图题 → 默认池子为空，报错里要提示"放宽缺图过滤能多出几题"
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            self.build({"选择": 3}, scope={"kps": ["1.2"]})   # 1.2 只有 20012/20013/20014
        msg = str(ctx.exception)
        self.assertIn("排除缺图题", msg)
        self.assertIn("可多出 1 题", msg)      # 20012 缺图；20013/20014 有图（fmt 带 embed/table）不算
        self.assertEqual(ctx.exception.relax["选择"]["exclude_fig_missing"], 1)

    def test_nos_blocked_by_quality_raises(self):
        with self.assertRaises(ValueError) as ctx:
            sel.build({"nos": ["20004"]}, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("20004", str(ctx.exception))
        self.assertIn("analysis_left", str(ctx.exception))

    def test_manifest_stats_excluded_shape(self):
        m = self.build({"选择": 7})
        ex = m["stats"]["excluded"]
        self.assertEqual(set(ex), {"print_blocking", "mismatch", "strict", "fig_missing"})
        for v in ex.values():
            self.assertIsInstance(v, int)


class LayoutParamTest(unittest.TestCase):
    """layout 进组卷参数（CONTRACT §2）：默认 compact，normal 可回退，非法值报错。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_layout_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "layout.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_default_is_compact(self):
        m = sel.build(params({"选择": 2}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(m["params"]["layout"], "compact")
        self.assertEqual(sel.normalize_params({"mix": {"选择": 1}})["layout"], "compact")

    def test_normal_is_echoed(self):
        p = params({"选择": 2}, req=None)
        p["layout"] = "normal"
        m = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(m["params"]["layout"], "normal")

    def test_unknown_layout_raises(self):
        p = params({"选择": 2}, req=None)
        p["layout"] = "tiny"
        with self.assertRaises(sel.SelectError) as ctx:
            sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIn("layout", str(ctx.exception))


class CalibrationParamTest(unittest.TestCase):
    """calibration 进组卷参数：默认 true（附校准页），显式 false 才不附。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_cal_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "cal.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_calibration_defaults_to_true(self):
        m = sel.build(params({"选择": 2}), self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIs(m["params"]["calibration"], True)

    def test_calibration_can_be_switched_off(self):
        p = params({"选择": 2})
        p["calibration"] = False
        m = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertIs(m["params"]["calibration"], False)


class SpaceDefaultTest(unittest.TestCase):
    """卷面留白默认全 0（答案写在答题卡上）；老师显式传 space 仍生效。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sel_space_")
        cls.conn = make_pool_db(os.path.join(cls.tmp, "space.db"))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_default_space_is_all_zero(self):
        m = sel.build(params({"选择": 2, "填空": 1, "解答": 1}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(m["params"]["space"],
                         {"选择": 0, "判断": 0, "填空": 0, "解答": 0, "其他": 0},
                         "五个题型都必须默认 0（以前解答题默认 8 段 = 6.8cm 空白）")
        self.assertTrue(all(it["space_paras"] == 0 for it in m["items"]),
                        "默认出卷不许给任何题留书写段")

    def test_explicit_space_is_honoured(self):
        p = params({"解答": 1})
        p["space"] = {"解答": 4}
        m = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual([it["space_paras"] for it in m["items"]], [4],
                         "老师要草稿区时必须照传（能力保留）")
        self.assertEqual(m["params"]["space"]["解答"], 4)
        self.assertEqual(m["params"]["space"]["选择"], 0, "没传的题型仍是 0")


# ---------------------------------------------------------------- 8. 真实库集成
@unittest.skipUnless(os.path.exists(REAL_DB), "真实题库不存在：%s" % REAL_DB)
class RealDbTest(unittest.TestCase):
    """在真实题库上跑（只读打开；不写死行数 —— 题库会按质量门裁剪）。"""

    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        cls.conn.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_default_paper(self):
        # t17 之后真库里有 34 个 subject_pk（语文/英语/物理/化学/政史…），
        # 不传 scope 会跨学科抽题（组卷页已强制选学科），所以这里显式限定数学七上。
        p = params({"选择": 10, "填空": 8, "解答": 6}, seed=42)
        p["scope"] = {"subject_pk": "math7a"}
        m = sel.build(p, self.conn, now=FIXED_NOW, pid=FIXED_PID)
        items = m["items"]
        self.assertEqual(len(items), 24)
        self.assertEqual([it["idx"] for it in items], list(range(1, 25)))
        self.assertEqual([it["qtype"] for it in items],
                         ["选择"] * 10 + ["填空"] * 8 + ["解答"] * 6)
        nos = {it["no"] for it in items}
        self.assertEqual(len(nos), 24)
        for it in items:
            # 章节名两种写法并存：老库「第2章 整式及其加减」，t17 入库「数学·七上·第2章 整式及其加减」
            unit = str(it["chapter"]).split("·")[-1]
            self.assertIn(unit,
                          {"第1章 有理数", "第2章 整式及其加减", "第3章 一次方程与方程组",
                           "第4章 几何图形初步", "第5章 数据的收集与整理", "综合"},
                          "跨学科/跨册混卷：%r" % (it["chapter"],))
            self.assertTrue(it["answer_key"], "默认卷每题都要有答案")
            self.assertTrue(it["family_key"] is None or isinstance(it["family_key"], str))

    def test_real_choice_answers_are_letters(self):
        m = sel.build(params({"选择": 40}, seed=7), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        for it in m["items"]:
            key = it["answer_key"][0]
            self.assertRegex(key["norm"], r"^[A-F]+$")
            # 题库里既有 4 选项题也有 3 选项题（导入件），只要选项字母从 A 起连续就对
            opts = it["options"]
            self.assertIn(len(opts), (2, 3, 4), "选项数异常：%r" % opts)
            self.assertEqual(opts, list("ABCD"[:len(opts)]), "选项字母应从 A 起连续")

    def test_real_reproducible(self):
        a = sel.build(params({"选择": 10, "填空": 8, "解答": 6}, seed=2024), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        b = sel.build(params({"选择": 10, "填空": 8, "解答": 6}, seed=2024), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True))

    def test_real_overlap_between_seeds(self):
        a = {it["no"] for it in sel.build(params({"选择": 10, "填空": 8, "解答": 6},
                                               seed=42), self.conn,
                                          now=FIXED_NOW, pid=FIXED_PID)["items"]}
        b = {it["no"] for it in sel.build(params({"选择": 10, "填空": 8, "解答": 6},
                                               seed=43), self.conn,
                                          now=FIXED_NOW, pid=FIXED_PID)["items"]}
        self.assertLess(len(a & b) / float(len(a)), 0.20)

    def test_real_chapter_scope(self):
        m = sel.build(params({"解答": 6}, scope={"chapters": [CH3]}, seed=1), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual({it["chapter"] for it in m["items"]}, {CH3})

    def test_real_kp_chapter_level(self):
        m = sel.build(params({"选择": 20}, scope={"kps": ["3.0"]}, seed=1), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertTrue(all(it["kp_id"].startswith("3.") for it in m["items"]))

    def test_real_multiblank_fill_and_solution(self):
        m = sel.build(params({"填空": 40, "解答": 20}, seed=5), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertTrue(any(len(it["answer_key"]) > 1 for it in m["items"]),
                        "真实库里必须能抽到多空填空/多小问解答")
        for it in m["items"]:
            self.assertLessEqual(len(it["answer_key"]), 20)
            self.assertEqual([k["blank"] for k in it["answer_key"]],
                             list(range(1, len(it["answer_key"]) + 1)))

    def test_real_small_kp_pool_reports_accurate_shortfall(self):
        """小知识点（如 5.0 只有几道题）要 20 题时，need/available/short 必须与真实池一致。

        不写死数字：先问 count_available 拿真值，再要「多 1 道」，核对报错里的三个数。
        """
        scope = {"kps": ["5.0"]}
        av = sel.count_available(self.conn, params({"填空": 1}, scope=scope))
        avail = max(av.values())
        qt = max(av, key=lambda k: av[k])
        if avail == 0:
            self.skipTest("该知识点当前没有可用题（题库裁剪中）")
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            sel.build(params({qt: avail + 1}, scope=scope), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        d = ctx.exception.details[qt]
        self.assertEqual(d, {"need": avail + 1, "available": avail, "short": 1})
        msg = str(ctx.exception)
        self.assertIn("%s 需要 %d" % (qt, avail + 1), msg)
        self.assertIn("可用 %d" % avail, msg)
        self.assertIn("差 1", msg)


class SubjectScopeTest(unittest.TestCase):
    """t11 · 学科过滤：两学科各 2 题，按 scope.subject_pk / scope.subjects 过滤只出本科题。

    15 万题是 7 个学科混在一个库里的，跨学科混卷（数学卷里冒出英语题）是不可接受的。
    """

    CN_CH = "第一单元 春"
    CN_KP = "CN1.1"        # kps.kp_id 是主键（跨学科也不能重复）→ 语文用带前缀的 kp_id

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="subject_")
        cls.db_path = os.path.join(cls.tmp, "app.db")
        conn = dbmod.connect(cls.db_path)
        try:
            # 数学（math7a）：2 道选择
            for no, ans in (("100001", "A"), ("100002", "B")):
                _insert_q(conn, no, "选择", CH1, "1.1", ans, option_count=4,
                          tokens=[{"kind": "text", "text": "数学 " + no}], subject="math7a")
            # 语文（chinese7a）：2 道选择
            for no, ans in (("200001", "C"), ("200002", "D")):
                _insert_q(conn, no, "选择", cls.CN_CH, cls.CN_KP, ans, option_count=4,
                          tokens=[{"kind": "text", "text": "语文 " + no}], subject="chinese7a")
            conn.executemany(
                "INSERT INTO kps(kp_id, subject_pk, chapter, kp, n_questions) VALUES(?,?,?,?,?)",
                [("1.1", "math7a", CH1, "有理数", 2),
                 (cls.CN_KP, "chinese7a", cls.CN_CH, "春", 2)])
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.conn = dbmod.connect(self.db_path)

    def tearDown(self):
        self.conn.close()

    def _nos(self, scope, mix=None):
        m = sel.build(params(mix or {"选择": 2}, scope=scope), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        return {it["no"] for it in m["items"]}

    def test_subject_pk_filters(self):
        self.assertEqual({"200001", "200002"},
                         self._nos({"subject_pk": "chinese7a"}), "只该抽语文题")

    def test_subject_pk_math(self):
        self.assertEqual({"100001", "100002"}, self._nos({"subject_pk": "math7a"}))

    def test_subjects_list_filters(self):
        self.assertEqual({"100001", "100002"}, self._nos({"subjects": ["math7a"]}))

    def test_subjects_list_multiple_is_union(self):
        """列多个学科 = 并集（老师可能想混一份跨学科练习卷，但必须是显式选的）。"""
        m = sel.build(params({"选择": 4}, scope={"subjects": ["math7a", "chinese7a"]}),
                      self.conn, now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual({"100001", "100002", "200001", "200002"},
                         {it["no"] for it in m["items"]})

    def test_subject_pk_and_subjects_merge(self):
        self.assertEqual({"100001", "100002"},
                         self._nos({"subject_pk": "math7a", "subjects": ["math7a"]}))

    def test_no_subject_defaults_to_math7a(self):
        """t62 · 不传学科 = 数学（math7a）：多学科库里绝不能跨科混卷。"""
        self.assertEqual({"100001", "100002"}, self._nos({}, mix={"选择": 2}))

    def test_unknown_subject_raises(self):
        with self.assertRaises(sel.ScopeError) as ctx:
            self._nos({"subject_pk": "physics9x"})
        self.assertIn("学科", str(ctx.exception))
        self.assertIn("physics9x", str(ctx.exception))

    def test_subject_and_chapter_combined(self):
        """选了语文后，数学的章节名不该再被认作合法范围（跨学科混选要报错）。"""
        with self.assertRaises(sel.ScopeError):
            self._nos({"subject_pk": "chinese7a", "chapters": [CH1]})

    def test_count_available_respects_subject(self):
        self.assertEqual(2, sel.count_available(
            self.conn, params({"选择": 1}, scope={"subject_pk": "chinese7a"}))["选择"])
        self.assertEqual(2, sel.count_available(
            self.conn, params({"选择": 1}, scope={"subject_pk": "math7a"}))["选择"])

    def test_subject_shortfall_is_reported(self):
        """某学科题量不够时要明确报不足（而不是偷偷从别的学科补）。"""
        with self.assertRaises(sel.InsufficientQuestions) as ctx:
            sel.build(params({"选择": 3}, scope={"subject_pk": "chinese7a"}), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)
        self.assertEqual({"need": 3, "available": 2, "short": 1},
                         ctx.exception.details["选择"])

    def test_nos_outside_subject_raises(self):
        """nos 精确题集给别的学科的题也要报错（卷子口径不能被绕过）。"""
        with self.assertRaises(sel.SelectError):
            sel.build(params({"选择": 1}, scope={"subject_pk": "chinese7a"},
                             nos=["100001"]), self.conn,
                      now=FIXED_NOW, pid=FIXED_PID)

    def test_scope_normalized_shape(self):
        p = sel.normalize_params(params({"选择": 1}, scope={"subject_pk": " math7a "}))
        self.assertEqual("math7a", p["scope"]["subject_pk"])
        self.assertEqual(["math7a"], p["scope"]["subjects"])


class TestSubjectDefault(unittest.TestCase):
    """t62 · 不传学科 = math7a；跨科题被挡；pool_report 报出被排除数。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="subj_")
        self.db = os.path.join(self.tmp, "s.db")
        conn = dbmod.connect(self.db)
        for i in range(3):
            _insert_q(conn, "M%03d" % i, "填空", CH1, "1.1", str(i + 1),
                      tokens=[{"kind": "text", "text": "数学 %d" % i}])
        for i in range(2):
            _insert_q(conn, "C%03d" % i, "填空", "语文·识字", "1.1", "日",
                      tokens=[{"kind": "text", "text": "语文 %d" % i}], subject="chinese7a")
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _nos(self, params):
        m = sel.build(dict(params, title="t", seed=7), self.conn)
        return [it["no"] for it in m["items"]]

    def test_default_subject_is_math7a(self):
        nos = self._nos({"mix": {"填空": 3}})
        self.assertEqual(3, len(nos))
        self.assertTrue(all(n.startswith("M") for n in nos), "默认只该出数学题：%s" % nos)

    def test_chinese_subject_picks_chinese(self):
        nos = self._nos({"mix": {"填空": 2}, "scope": {"subject_pk": "chinese7a"}})
        self.assertEqual(2, len(nos))
        self.assertTrue(all(n.startswith("C") for n in nos), nos)

    def test_pool_report_subject_and_excluded(self):
        rep = sel.pool_report(self.conn, {"mix": {"填空": 3}})
        self.assertEqual(["math7a"], rep["subject"])
        self.assertTrue(rep["subject_default"], "没传学科要标成默认")
        self.assertEqual(2, rep["excluded_other_subject"], "2 道语文题应被报为已排除")
        rep2 = sel.pool_report(self.conn, {"mix": {"填空": 2},
                                           "scope": {"subject_pk": "chinese7a"}})
        self.assertEqual(3, rep2["excluded_other_subject"], "反过来该排除 3 道数学题")

    def test_null_subject_counts_as_math7a(self):
        """老库（NULL 学科）仍能选中，等价于 math7a。"""
        self.conn.execute("UPDATE questions SET subject_pk=NULL WHERE no='M000'")
        self.conn.commit()
        nos = self._nos({"mix": {"填空": 3}})
        self.assertIn("M000", nos, "NULL 学科要当 math7a 处理")

    def test_real_english_paper_only_english(self):
        """真实库：英语九年级上出 3 题，题号都属于 english9a。"""
        real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "app.db")
        if not os.path.exists(real):
            self.skipTest("app.db 不存在")
        conn = sqlite3.connect("file:%s?mode=ro" % real.replace("\\", "/"), uri=True)
        try:
            if not conn.execute("SELECT 1 FROM questions WHERE subject_pk='english9a'"
                                " LIMIT 1").fetchone():
                self.skipTest("真库当前没有 english9a（题库已改为纯数学多册）")
            m = sel.build({"title": "英语卷", "mix": {"选择": 3}, "seed": 1,
                           "scope": {"subject_pk": "english9a"}}, conn)
            nos = [it["no"] for it in m["items"]]
            self.assertEqual(3, len(nos))
            marks = ",".join("?" * len(nos))
            got = {r[1] for r in conn.execute(
                "SELECT no, subject_pk FROM questions WHERE no IN (%s)" % marks, nos)}
            self.assertEqual({"english9a"}, got, "英语卷出现了非英语题：%s" % nos)
            chapters = [it.get("chapter") or "" for it in m["items"]]
            self.assertTrue(all(c.startswith("英语") for c in chapters),
                            "英语卷的章节应都是英语：%s" % chapters)
        finally:
            conn.close()


class CrossSubjectChapterTest(unittest.TestCase):
    """t97：章节必须属于当前学科，残留别的学科的章节要报「不属于当前学科」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xsub_")
        self.db = os.path.join(self.tmp, "x.db")
        conn = dbmod.connect(self.db)
        for i in range(3):
            _insert_q(conn, "M%03d" % i, "选择", CH1, "1.1", "A",
                      tokens=[{"kind": "text", "text": "数学 %d" % i}])
        # physics：有题但 chapter 为空（线上就是这样导入的）
        for i in range(3):
            _insert_q(conn, "P%03d" % i, "选择", "", "", "A",
                      tokens=[{"kind": "text", "text": "物理 %d" % i}], subject="physics")
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cross_subject_chapter_raises_scope_error(self):
        with self.assertRaises(sel.ScopeError) as ctx:
            sel.count_available(self.conn, {"mix": {"选择": 1},
                                            "scope": {"subject_pk": "physics",
                                                      "chapters": [CH1]}})
        msg = str(ctx.exception)
        self.assertIn("不属于当前学科", msg)
        self.assertIn("physics", msg)

    def test_cross_subject_chapter_does_not_look_like_shortage(self):
        with self.assertRaises(sel.ScopeError):
            sel.count_available(self.conn, {"mix": {"选择": 10},
                                            "scope": {"subject_pk": "physics",
                                                      "chapters": [CH1]}})

    def test_own_chapter_still_passes(self):
        out = sel.count_available(self.conn, {"mix": {"选择": 1},
                                              "scope": {"subject_pk": "math7a",
                                                        "chapters": [CH1]}})
        self.assertEqual(3, out["选择"])

    def test_clean_physics_counts(self):
        out = sel.count_available(self.conn, {"mix": {"选择": 1},
                                              "scope": {"subject_pk": "physics"}})
        self.assertEqual(3, out["选择"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
