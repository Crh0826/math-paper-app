# -*- coding: utf-8 -*-
"""M0-d · 答案归一化与等价判断测试（先红后绿）。

判卷只判「选择序号」与「填空/解答的最终答案」，所以这里的等价对全部取自真实语料里
出现过的写法（app.db answer_raw 抽样）：U+2212 负号、线性分式 `(11)/(3)`、`(b)^(2)`、
`0或1`、`1.5     −1`、`②③`、`【知识点】` 尾巴、全角括号/数字……

指标（任务书要求）：等价对判对率 >= 95%，不等价对判错（判成不同）率 >= 95%。
"""
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import normalize  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")

# (学生写的, 标准答案, 题型, 覆盖点)
EQUIV = [
    # ---- 全半角 / 空格 / 大小写 ----
    ("Ａ", "A", "选择", "全角字母"),
    ("ａ", "A", "选择", "全角小写"),
    ("C", "Ｃ", "选择", "全角标准答案"),
    ("b", "B", "选择", "小写"),
    ("选A", "A", "选择", "OCR 带「选」前缀"),
    ("答案是B", "B", "选择", "OCR 带「答案是」前缀"),
    ("A.", "A", "选择", "选项带句点"),
    ("(A)", "A", "选择", "选项带括号"),
    ("A、", "A", "选择", "选项带顿号"),
    (" A ", "A", "选择", "首尾空格"),
    ("A\t", "A", "选择", "制表符尾巴"),
    ("1.5     −1", "1.5 -1", "填空", "多空多空格"),
    ("1.5 −1", "1.5；-1", "填空", "多空分隔符"),
    ("12　5", "12 5", "填空", "全角空格分隔"),
    ("0.5 ", "0.5", "填空", "尾空格"),
    (" 4", "4", "填空", "首空格"),
    ("x = 4", "x=4", "填空", "等号两侧空格"),
    ("3 , 5", "3,5", "填空", "逗号旁空格"),
    # ---- 正负号字符 ----
    ("−4", "-4", "填空", "U+2212 负号"),
    ("－4", "-4", "填空", "全角负号"),
    ("−1或−7", "-1或-7", "填空", "负号+或"),
    ("＋3", "3", "填空", "全角正号"),
    ("(-3)", "-3", "填空", "负数带括号"),
    ("（−3）", "-3", "填空", "全角括号负数"),
    ("﹣2.5", "-2.5", "填空", "小型负号"),
    # ---- 分数 ↔ 小数 / 带分数 ----
    ("1/2", "0.5", "填空", "分数→小数"),
    ("0.5", "1/2", "填空", "小数→分数"),
    ("(1)/(2)", "0.5", "填空", "线性分式"),
    ("3/4", "0.75", "填空", "3/4"),
    ("(11)/(3)", "11/3", "解答", "线性分式去括号"),
    ("(2)/(5)", "0.4", "解答", "线性分式小数"),
    ("0.50", "0.5", "填空", "尾零"),
    ("2.0", "2", "填空", "整数化"),
    ("3.00", "3", "填空", "双尾零"),
    ("1／2", "1/2", "填空", "全角斜杠"),
    ("1又1/2", "1.5", "填空", "带分数"),
    ("1 1/2", "1.5", "填空", "带分数空格式"),
    ("２又１／２", "2.5", "填空", "带分数全角"),
    ("一又二分之一", "1.5", "填空", "带分数中文"),
    # ---- 百分号 ----
    ("50%", "0.5", "填空", "百分号"),
    ("12.5%", "0.125", "填空", "百分号小数"),
    ("0.5", "50%", "填空", "小数对百分号"),
    ("25％", "0.25", "填空", "全角百分号"),
    # ---- 单位 ----
    ("5cm", "5", "填空", "单位cm"),
    ("5 cm", "5", "填空", "单位带空格"),
    ("30°", "30", "填空", "角度符号"),
    ("30度", "30°", "填空", "中文度"),
    ("12元", "12", "填空", "单位元"),
    ("12 元", "12", "填空", "单位元带空格"),
    ("5m", "500cm", "填空", "单位换算"),
    ("1.5cm", "15mm", "填空", "单位换算小数"),
    ("2小时", "120分钟", "填空", "时间换算"),
    ("3kg", "3000g", "填空", "质量换算"),
    ("7个", "7", "填空", "量词个"),
    ("5米", "5m", "填空", "中文单位"),
    # ---- x= 前缀 / 变量前缀 ----
    ("x=4", "4", "填空", "x=前缀"),
    ("x＝4", "4", "填空", "全角等号前缀"),
    ("y=1", "1", "解答", "y=前缀"),
    ("a=3", "3", "填空", "a=前缀"),
    ("n=2", "2", "填空", "n=前缀"),
    ("(1)x=4", "(1)4", "解答", "分段内x=前缀"),
    # ---- 根号 / 平方写法 ----
    ("√3", "根号3", "填空", "根号两种写法"),
    ("2√3", "2根号3", "填空", "根号带系数"),
    ("√9", "3", "填空", "根号可开方"),
    ("x^2", "x²", "填空", "上标平方"),
    ("x的平方", "x^2", "填空", "中文平方"),
    ("(b)^(2)", "b^2", "解答", "线性幂"),
    ("(a)^(2)", "a²", "解答", "线性幂上标"),
    ("(4ac−(b)^(2))/(4)", "(4ac-b^2)/4", "解答", "线性分式+幂"),
    ("2*n", "2n", "填空", "隐式乘号"),
    # ---- 度分秒 ----
    ("30°30′", "30.5°", "填空", "度分秒"),
    ("30°30′", "30.5", "填空", "度分秒无单位"),
    ("30度30分", "30.5", "填空", "中文度分"),
    ("30°15′", "30.25", "填空", "度分小数"),
    ("12°30′", "12.5°", "填空", "度分秒另一例"),
    # ---- 区间 / 集合 / 不等式 ----
    ("(1,3)", "1<x<3", "填空", "区间↔不等式"),
    ("1<x<3", "(1,3)", "填空", "不等式↔区间"),
    ("(1,3)", "1＜x＜3", "填空", "全角不等号"),
    ("[1,3]", "1≤x≤3", "填空", "闭区间"),
    ("(1,3]", "1<x≤3", "填空", "半开区间"),
    ("[1,3)", "1≤x<3", "填空", "半开区间2"),
    ("x>3", "(3,+∞)", "解答", "无穷区间"),
    ("x<3", "(-∞,3)", "解答", "负无穷区间"),
    ("x≥1", "[1,+∞)", "解答", "闭无穷"),
    ("{x|1<x<3}", "(1,3)", "解答", "集合描述法"),
    ("−2≤x≤4", "[-2,4]", "解答", "负数区间"),
    # ---- 序号集合（多选/多选题）----
    ("①④⑤⑥⑦", "14567", "填空", "圈号↔连写数字"),
    ("①④⑤⑥⑦", "①④⑤⑥⑦", "填空", "圈号自身"),
    ("②③", "23", "填空", "圈号两位"),
    ("③①②", "①②③", "填空", "圈号顺序无关"),
    ("1,4,5,6,7", "①④⑤⑥⑦", "填空", "逗号集合对圈号"),
    ("①④⑤ ⑤⑦", "①④⑤；⑤⑦", "填空", "多空圈号"),
    # ---- 多小问 (1)(2) ----
    ("(1)x=4;(2)x=9", "（1）x=4；（2）x=9", "解答", "多小问全半角"),
    ("(1)5；(2)10", "(1)5\n(2)10", "解答", "多小问换行"),
    ("(1)x=2;(2)x=-3", "（1）x=2（2）x=−3", "解答", "多小问无分号"),
    ("(1)x=(11)/(3)；(2)x=−(2)/(5)", "(1)x=11/3;(2)x=-2/5", "解答", "真实多小问"),
    ("(1)9\n(2)9", "(1)9；(2)9", "解答", "多小问同值"),
    ("(1)−8，12，20", "(1)-8,12,20", "解答", "分段内列表"),
    # ---- 多空 / 并列答案 ----
    ("0或1", "1或0", "填空", "或 顺序"),
    ("−1或−7", "-7或-1", "填空", "负数或顺序"),
    ("0或±1", "0或1或-1", "填空", "±拆开"),
    ("3、4、5", "3,4,5", "填空", "顿号列表"),
    ("1.2、1.8、3、4", "1.2,1.8,3,4", "解答", "小数顿号列表"),
    ("-8，12，20", "−8，12，20", "解答", "全角逗号列表"),
    # ---- 中文数字 ----
    ("十二", "12", "填空", "中文十二"),
    ("二十", "20", "填空", "中文二十"),
    ("一百二十", "120", "填空", "中文百位"),
    ("负三", "-3", "填空", "中文负号"),
    ("十五", "15", "填空", "中文十五"),
    ("三十", "30", "填空", "中文三十"),
    ("两", "2", "填空", "两=2"),
    # ---- 根式化简 / 绝对值 / 立方根 / 中文读法（t26 补齐）----
    ("√8", "2√2", "填空", "根式化简"),
    ("√18", "3√2", "填空", "根式化简2"),
    ("2√2", "√8", "填空", "根式反写"),
    ("√2+√2", "2√2", "填空", "同类根合并"),
    ("√(1/2)", "√2/2", "填空", "根式分母有理化"),
    ("√12", "2√3", "填空", "根式化简3"),
    ("√50", "5√2", "填空", "根式化简4"),
    ("2√8", "4√2", "填空", "系数×根式"),
    ("√(9/4)", "3/2", "填空", "分数根式"),
    ("|−2|", "2", "填空", "绝对值"),
    ("|2|", "2", "填空", "绝对值正数"),
    ("|-5|", "5", "填空", "绝对值一般"),
    ("|−2|+|3|", "5", "填空", "绝对值和"),
    ("|x|", "|x|", "填空", "含变量绝对值保持形式"),
    ("∛8", "2", "填空", "立方根"),
    ("∛27", "3", "填空", "立方根2"),
    ("∛(−8)", "-2", "填空", "负立方根"),
    ("∛64", "4", "填空", "立方根3"),
    ("三分之一", "1/3", "填空", "中文分数"),
    ("二分之一", "1/2", "填空", "中文分数2"),
    ("四分之三", "3/4", "填空", "中文分数3"),
    ("十分之一", "0.1", "填空", "中文分数4"),
    ("零点五", "0.5", "填空", "中文小数"),
    ("一点五", "1.5", "填空", "中文小数2"),
    ("负零点五", "-0.5", "填空", "中文负小数"),
    ("三块五", "3.5", "填空", "口语金额"),
    # ---- 判断 ----
    ("对", "正确", "判断", "对/正确"),
    ("√", "对", "判断", "勾对"),
    ("错误", "×", "判断", "错误/叉"),
    ("T", "true", "判断", "T/true"),
    ("F", "false", "判断", "F/false"),
    ("是", "对", "判断", "是/对"),
    ("×", "错", "判断", "叉/错"),
    # ---- 真实语料里的脏数据 ----
    ("3\n【知识点】合并同类项、已知同类项求指数中字母或代数式的值", "3", "填空", "知识点尾巴"),
    ("20     10", "20 10", "选择", "选择区多值多空格"),
    ("(4ac−(b)^(2))/(4)", "(4ac-b^(2))/4", "解答", "真实公式两写法"),
    ("(1)(3.5x+21.5) 元； (2.5x+46.5) 元\n(2)236 元",
     "(1)(3.5x+21.5)元；(2.5x+46.5)元\n(2)236", "解答", "真实解答含元"),
    ("（1）−2≤x≤4；（2）9；（3）2x+y的最小值是−8，最大值是5．",
     "(1)-2≤x≤4;(2)9;(3)2x+y的最小值是-8,最大值是5", "解答", "真实解答全角标点"),
    ("(1)6\n(2)14.6", "(1)6；(2)14.6", "解答", "真实两小问"),
]

INEQUIV = [
    ("A", "B", "选择", "不同选项"),
    ("A", "AB", "选择", "选项数不同"),
    ("4", "5", "填空", "不同整数"),
    ("-4", "4", "填空", "符号不同"),
    ("1/2", "1/3", "填空", "不同分数"),
    ("5m", "5cm", "填空", "单位不同不可换算"),
    ("5元", "5角", "填空", "元角不同"),
    ("(1,3)", "[1,3]", "填空", "开闭区间不同"),
    ("(1,3)", "(1,4)", "填空", "区间端点不同"),
    ("(1,3]", "(1,3)", "填空", "半开与开"),
    ("[1,3]", "[1,3)", "填空", "半开与闭"),
    ("①④⑤⑥⑦", "①④⑤⑦", "填空", "少一个序号"),
    ("1,4,5,6,7", "1,4,5,7", "填空", "数字集合少一个"),
    ("1<x<3", "1≤x<3", "填空", "不等式开闭"),
    ("x>3", "x≥3", "解答", "不等式符号"),
    ("50%", "50", "填空", "百分号不能丢"),
    ("十二", "21", "填空", "中文数字不同"),
    ("√3", "3", "填空", "根号不是原值"),
    ("√3", "√2", "填空", "根号被开方数不同"),
    ("x^2", "x^3", "填空", "幂次不同"),
    ("2n", "2+n", "填空", "乘加不同"),
    ("(1)5;(2)10", "(1)5;(2)11", "解答", "分段值不同"),
    ("(1)5;(2)10", "(1)5", "解答", "缺小问"),
    ("对", "错", "判断", "判断相反"),
    ("√", "×", "判断", "判断符号相反"),
    ("1.5 -1", "1.5 -2", "填空", "第二空不同"),
    ("1.5 -1", "-1 1.5", "填空", "多空按位判，顺序不同即不等价"),
    ("30°30′", "30.6", "填空", "度分值不同"),
    ("-1或-7", "-1或7", "填空", "或的一支符号不同"),
    ("0.5", "5", "填空", "小数位数不同"),
    ("3/4", "0.8", "填空", "分数小数不等"),
    ("1又1/2", "2.5", "填空", "带分数不同"),
    ("20     10", "20     11", "选择", "多值第二项不同"),
    ("(4ac−(b)^(2))/(4)", "(4ac+b^2)/4", "解答", "公式符号不同"),
    ("x=4", "x=5", "填空", "解不同"),
    ("12", "21", "填空", "数字位序不同（不能被当序号集合）"),
    ("30°", "30′", "填空", "度与分不同"),
    ("三", "4", "填空", "中文数字不同"),
    ("√8", "√9", "填空", "根式开方数不同"),
    ("√8", "2√3", "填空", "根式化简错"),
    ("√2", "1.4", "填空", "根号不能用近似值"),
    ("|−2|", "-2", "填空", "绝对值不为负"),
    ("∛8", "3", "填空", "立方根不是立方"),
    ("三分之一", "1/2", "填空", "中文分数不同"),
    ("零点五", "0.05", "填空", "中文小数位不同"),
    ("三块五", "3.15", "填空", "口语金额不同"),
    ("2√2", "2√3", "填空", "同系数不同根"),
    ("⅓", "0.3", "填空", "三分之一≠0.3"),

]


class TestEquivalentPairs(unittest.TestCase):
    def test_equiv_rate(self):
        bad = []
        for st, ex, qt, note in EQUIV:
            ok, reason = normalize.is_equivalent(st, ex, qt)
            if not ok:
                bad.append(f"  {note}: {st!r} vs {ex!r} [{qt}] -> {reason}")
        rate = 1 - len(bad) / len(EQUIV)
        self.assertGreaterEqual(rate, 0.95, "等价判对率 %.1f%% < 95%%（%d/%d 失败）\n%s" % (
            rate * 100, len(bad), len(EQUIV), "\n".join(bad)))

    def test_equiv_symmetric(self):
        """等价判定不能有方向性：反着判也应该等价。"""
        bad = []
        for st, ex, qt, note in EQUIV:
            ok, reason = normalize.is_equivalent(ex, st, qt)
            if not ok:
                bad.append(f"  {note}: 反向 {ex!r} vs {st!r} [{qt}] -> {reason}")
        rate = 1 - len(bad) / len(EQUIV)
        self.assertGreaterEqual(rate, 0.95, "反向等价判对率 %.1f%% < 95%%\n%s" % (
            rate * 100, "\n".join(bad)))

    def test_inequiv_rate(self):
        bad = []
        for st, ex, qt, note in INEQUIV:
            ok, reason = normalize.is_equivalent(st, ex, qt)
            if ok:
                bad.append(f"  {note}: {st!r} vs {ex!r} [{qt}] 被判等价（{reason}）")
        rate = 1 - len(bad) / len(INEQUIV)
        self.assertGreaterEqual(rate, 0.95, "不等价判错率 %.1f%% < 95%%（%d/%d 误判）\n%s" % (
            rate * 100, len(bad), len(INEQUIV), "\n".join(bad)))

    def test_corpus_size(self):
        self.assertGreaterEqual(len(EQUIV), 100, "等价对不足 100 组")
        self.assertGreaterEqual(len(INEQUIV), 30, "不等价对不足 30 组")


class TestNormalizeApi(unittest.TestCase):
    def test_returns_str(self):
        for s in ("Ａ", "−4", "", None, "  ", "1/2", "x^2"):
            out = normalize.normalize_answer(s, "填空")
            self.assertIsInstance(out, str)
        self.assertEqual("", normalize.normalize_answer("", "填空"))
        self.assertEqual("", normalize.normalize_answer(None, "填空"))

    def test_normalize_is_stable(self):
        """归一化要幂等：同一答案归一化两次结果一致。"""
        samples = [s for s, _e, _q, _n in EQUIV] + [e for _s, e, _q, _n in EQUIV]
        for s in samples:
            once = normalize.normalize_answer(s, "解答")
            twice = normalize.normalize_answer(once, "解答")
            self.assertEqual(once, twice, f"归一化不幂等: {s!r} -> {once!r} -> {twice!r}")

    def test_normalize_strips_meta_and_spaces(self):
        self.assertEqual("3", normalize.normalize_answer(
            "3\n【知识点】合并同类项、已知同类项求指数中字母或代数式的值", "填空"))
        self.assertNotIn(" ", normalize.normalize_answer("1.5     −1", "填空"))

    def test_reason_is_human_readable(self):
        for a, b in (("4", "5"), ("1/2", "0.5"), ("x=4", "4")):
            ok, reason = normalize.is_equivalent(a, b, "填空")
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(reason, str)
            self.assertTrue(reason.strip(), "reason 不能为空")

    def test_defensive_on_weird_input(self):
        weird = ["", " ", None, "（）", "，，，", "①②", "x=", "=", "1/0", "√", "^2", "%"]
        for s in weird:
            ok, reason = normalize.is_equivalent(s, s, "填空")
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(reason, str)

    def test_no_llm_or_network(self):
        """默认路径必须是纯规则：不许偷偷调 LLM / 联网。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "normalize.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for bad in ("import requests", "import openai", "urllib.request", "http://", "https://",
                    "socket", "anthropic", "dashscope"):
            self.assertNotIn(bad, src, f"normalize.py 不应出现 {bad!r}")


class TestRealCorpus(unittest.TestCase):
    """真实答案自反 + 真实脏数据：库里每条 answer_raw 都必须和自己等价（判卷不能自打脸）。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DB):
            raise unittest.SkipTest("app.db 不存在（先跑 build_db.py）")
        cls.conn = sqlite3.connect(DB)
        cls.rows = [r[0] for r in cls.conn.execute(
            "SELECT answer_raw FROM questions WHERE answer_raw<>'' ORDER BY no")]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_reflexive_on_real_answers(self):
        bad = []
        for s in self.rows:
            ok, reason = normalize.is_equivalent(s, s, "填空")
            if not ok:
                bad.append(f"  {s!r} -> {reason}")
        rate = 1 - len(bad) / len(self.rows)
        self.assertGreaterEqual(rate, 0.99, "真实答案自反率 %.2f%% < 99%%（%d/%d）\n%s" % (
            rate * 100, len(bad), len(self.rows), "\n".join(bad[:10])))

    def test_no_exception_on_real_answers(self):
        for s in self.rows[:1500]:
            normalize.normalize_answer(s, "解答")
            normalize.is_equivalent(s, s, "解答")

    def test_real_equivalent_variants(self):
        """同一道题的不同录入写法（语料里真实出现过）。"""
        cases = [
            ("−4", "-4"), ("−1或−7", "-1或-7"), ("②③", "23"),
            ("（1）3；（2）2，2n", "(1)3;(2)2,2n"),
            ("(1)x=(7)/(4)\n(2)x=1", "(1)x=1.75;(2)x=1"),
        ]
        for a, b in cases:
            ok, reason = normalize.is_equivalent(a, b, "解答")
            self.assertTrue(ok, f"{a!r} 应≡ {b!r}，实际 {reason}")

    def test_precision_on_random_real_pairs(self):
        """随机抽两题答案互判：不同答案不能被误判成等价（判卷不能送分）。"""
        import random
        pool = [r for r in self.rows if len(r) < 40]
        random.seed(20261031)
        pairs = [(random.choice(pool), random.choice(pool)) for _ in range(2000)]
        # 只差【知识点】这类元信息的，本来就该判等价，不算误判
        fp = [(a, b) for a, b in pairs
              if normalize.normalize_answer(a, "填空") != normalize.normalize_answer(b, "填空")
              and normalize.is_equivalent(a, b, "填空")[0]]
        rate = len(fp) / len(pairs)
        self.assertLessEqual(rate, 0.02, "真实语料随机配对误判率 %.2f%% > 2%%：%s" % (
            rate * 100, fp[:5]))


class TestT26Gaps(unittest.TestCase):
    """验收方那 106 组里漏判的 8 条（原文照抄）+ 受控求值兜底的行为约束。"""

    ACCEPTANCE = [
        ("√8", "2√2", "填空"),
        ("√18", "3√2", "填空"),
        ("√(1/2)", "√2/2", "填空"),
        ("2√2", "√8", "填空"),
        ("√2+√2", "2√2", "填空"),
        ("|−2|", "2", "填空"),
        ("|2|", "2", "填空"),
        ("∛8", "2", "填空"),
        ("∛27", "3", "填空"),
    ]

    def test_acceptance_pairs(self):
        bad = []
        for a, b, q in self.ACCEPTANCE:
            ok, reason = normalize.is_equivalent(a, b, q)
            if not ok:
                bad.append((a, b, reason))
        self.assertEqual([], bad, "这 8 类必须判等价：%s" % bad)

    def test_cn_readings(self):
        for a, b in (("三分之一", "1/3"), ("二分之一", "1/2"), ("零点五", "0.5"),
                     ("负二", "-2"), ("十分之一", "0.1"), ("三块五", "3.5")):
            ok, reason = normalize.is_equivalent(a, b, "填空")
            self.assertTrue(ok, "%s 应≡ %s，实际 %s" % (a, b, reason))

    def test_no_eval_in_source(self):
        """受控求值：源码不许出现 eval(/exec(。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "normalize.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for bad in ("eval(", "exec(", "__import__"):
            self.assertNotIn(bad, src, "normalize.py 不许用 %s" % bad)

    def test_abs_with_variable_stays_symbolic(self):
        """|x| 不能被数值化（否则 |x| 和 x 会判等价）。"""
        self.assertFalse(normalize.is_equivalent("|x|", "x", "填空")[0])
        self.assertTrue(normalize.is_equivalent("|x|", "|x|", "填空")[0])


class TestT36MixedNumbers(unittest.TestCase):
    """带分数写法 A(B)/(C)：题库用 `21(1)/(4)` 表示 21 又 1/4，学生写 `21又1/4`/`21 1/4`/`85/4`。

    这 8 个真实答案来自库里（00639/01168/03321/03799/01213/01187/03389/04114），
    以前全部判错 = 学生答对却被判 0 分。
    """

    #: (标准答案, [学生写法...]) —— 8 个真实答案，每位配 3 种手写习惯
    CASES = [
        ("21(1)/(4)", ["21又1/4", "21 1/4", "85/4"]),
        ("25(25)/(52)", ["25又25/52", "25 25/52", "1325/52"]),
        ("479(1)/(2)", ["479又1/2", "479 1/2", "959/2"]),
        ("x=6(1)/(7)", ["x=6又1/7", "x=6 1/7", "x=43/7"]),
        ("−249(4)/(5)", ["-249又4/5", "-249 4/5", "-1249/5"]),
        ("72(5)/(9)", ["72又5/9", "72 5/9", "653/9"]),
        ("−12(1)/(8)", ["-12又1/8", "-12 1/8", "-97/8"]),
        ("−49(14)/(19)", ["-49又14/19", "-49 14/19", "-945/19"]),
    ]

    NEGATIVE = [
        ("2×(5)/(7)", "2(5)/(7)", "显式乘号仍按乘法"),
        ("2x(5)/(7)", "2(5)/(7)", "带变量的不转换"),
        ("25(25)/(52)", "25×25/52", "带分数 ≠ 整数乘分数"),
        ("2(9)/(7)", "2又9/7", "B≥C 不是合法带分数，不转换"),
        ("21(1)/(4)", "21(1)/(5)", "分子不同"),
        ("21(1)/(4)", "5.25", "值不同"),
    ]

    def test_real_answers_times_three_handwritings(self):
        bad = []
        for expected, variants in self.CASES:
            for student in variants:
                ok, reason = normalize.is_equivalent(student, expected, "解答")
                if not ok:
                    bad.append((expected, student, reason))
        self.assertEqual([], bad, "带分数写法漏判 %d 条：%s" % (len(bad), bad))

    def test_symmetric(self):
        bad = [(e, v) for e, vs in self.CASES for v in vs
               if not normalize.is_equivalent(e, v, "解答")[0]]
        self.assertEqual([], bad, "反向判也应对：%s" % bad)

    def test_no_false_positives(self):
        bad = []
        for a, b, why in self.NEGATIVE:
            ok, reason = normalize.is_equivalent(a, b, "解答")
            if ok:
                bad.append((a, b, why))
        self.assertEqual([], bad, "误判成等价：%s" % bad)

    def test_pure_fraction_unchanged(self):
        """纯分数 (4)/(3) 不能被当成带分数（前面没有整数）。"""
        for a, b in (("(4)/(3)", "4/3"), ("(1)/(2)", "0.5"), ("(7)/(4)", "1.75")):
            ok, reason = normalize.is_equivalent(a, b, "解答")
            self.assertTrue(ok, "%s 应≡ %s：%s" % (a, b, reason))


class TestT41SciNotation(unittest.TestCase):
    """乘号丢失/变空格/变 e 记法、Unicode 上标 10¹² —— 用户手机实测踩到的判分错误（题 03370）。"""

    #: 题库原样答案（03370 = `1.6×(10)^(12)`），学生/OCR 各种写法都要判等价
    CASES = [
        ("1.6×(10)^(12)", ["1.6×10^12", "1.6 10^12", "1.6 10^ 12", "1.6*10^12",
                           "1.6e12", "1.6×10¹²", "1600000000000"]),
        ("3×10^8", ["3 10^8", "3e8", "3*10^8", "3×10⁸", "300000000"]),
        ("50×4", ["50 4", "50*4", "200"]),
        ("3×4n-1", ["3 4n-1", "3*4n-1"]),
        ("1.51×10^4", ["1.51 10^4", "1.51e4", "15100"]),
        ("2.5×10^(-3)", ["2.5 10^-3", "2.5e-3", "0.0025"]),
    ]

    NEGATIVE = [
        ("1.6×10^12", "1.6×10^11", "指数不同"),
        ("1.6×10^12", "1.7×10^12", "系数不同"),
        ("3×10^8", "3×10^9", "指数不同（3e8≠3e9）"),
        ("50×4", "50×5", "乘数不同"),
    ]

    def test_cases(self):
        bad = []
        for expected, variants in self.CASES:
            for student in variants:
                ok, reason = normalize.is_equivalent(student, expected, "填空")
                if not ok:
                    bad.append((expected, student, reason))
        self.assertEqual([], bad, "科学计数法漏判 %d 条：%s" % (len(bad), bad))

    def test_symmetric(self):
        bad = [(e, v) for e, vs in self.CASES for v in vs
               if not normalize.is_equivalent(e, v, "填空")[0]]
        self.assertEqual([], bad, "反向也应对：%s" % bad)

    def test_no_false_positives(self):
        bad = []
        for a, b, why in self.NEGATIVE:
            if normalize.is_equivalent(a, b, "填空")[0]:
                bad.append((a, b, why))
        self.assertEqual([], bad, "误判等价：%s" % bad)

    def test_multivalue_space_still_works(self):
        """多值答案（空格/逗号分隔）不能被当成乘法：库里 199 道是「61     12」这种写法。"""
        for a, b in (("61     12", "61,12"), ("1.5 -1", "1.5,-1"), ("20 10", "20,10"),
                     ("6     7", "6,7")):
            ok, reason = normalize.is_equivalent(a, b, "填空")
            self.assertTrue(ok, "%s 应≡ %s（多值）：%s" % (a, b, reason))

    def test_multivalue_is_not_product(self):
        """多值答案不能被当成乘积（库里 61     12 是两个答案，不是 61×12）。"""
        self.assertFalse(normalize.is_equivalent("61     12", "61*12", "填空")[0])
        self.assertFalse(normalize.is_equivalent("6     7", "6*7", "填空")[0])

    def test_mixed_number_not_multiplication(self):
        self.assertTrue(normalize.is_equivalent("2 5/7", "2又5/7", "填空")[0])
        self.assertTrue(normalize.is_equivalent("-2 5/7", "-2又5/7", "填空")[0])
        self.assertFalse(normalize.is_equivalent("2 5/7", "2*5/7", "填空")[0],
                         "带分数不能被当乘法")

    def test_adjacent_digits_are_multiplication_not_power(self):
        """`1.6 10^ 12` 不能先被去掉空格拼成 1.610^12 再算成 1.61^12（303.33）。"""
        self.assertFalse(normalize.is_equivalent("1.6 10^ 12", "303.3266107", "填空")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
