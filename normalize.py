# -*- coding: utf-8 -*-
"""M0-d · 答案归一化与等价判断（**纯规则**：无 LLM、无网络，判卷默认路径）。

判卷只判「选择序号」与「填空/解答的最终答案」，所以这里做的是字符串级的等价判断，
不做算理推导、不替代人工确认。

分层设计：
  1) `_pre()`      预处理：元信息剥离 → 上标/立方/平方 → 圈号 → 全半角(NFKC) → 符号统一
                   → 带分数 → 根号/立方根展开 → 绝对值展开 → 中文读法 → 度分秒 → ±
                   → 中文数字 → 空格收敛
  2) `normalize_answer()`  给库/给人看的规范串（幂等）
  3) `is_equivalent()`     多策略比较，返回 (bool, 人话原因)
       选择/判断 → 专用比较
       否则：多小问 (1)(2) → 多空/多项 → 单值（或 / 区间 / 数值+单位+百分号 / 序号集合 / 规范形）

数值化兜底（t26）：`sqrt()` / `cbrt()` / `abs()` / 四则 / 整数幂 用 ast 白名单求值
（**不用 eval**），相对误差 <1e-9 判等价 —— 覆盖 √8 ↔ 2√2、∛8 ↔ 2、|−2| ↔ 2 这类写法。

已知不覆盖（写进任务输出）：
  - 代数式真正化简（`3a+2a` vs `5a`）、三角/对数/指数函数求值；
  - 角度制的「分」单独出现（`30′`）不当数值换算，避免与 30 混淆；
  - 图形/文字描述类答案（"正方形JHMI"、"原方程无解"）只做字面等价；
  - 中文数字紧贴汉字时（"三十人"）不转换，避免把「同一个数」误伤。
"""
from __future__ import annotations

import ast
import math
import re
import unicodedata

__all__ = ["normalize_answer", "is_equivalent"]

# ---------------------------------------------------------------- 字符表

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_MAP = {ch: str(i) for i, ch in enumerate(_CIRCLED, 1)}
_PAREN_CIRCLED = "⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽"
_PAREN_MAP = {ch: str(i) for i, ch in enumerate(_PAREN_CIRCLED, 1)}

_SUPERSCRIPTS = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
                 "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
_RE_SUPERSCRIPT_RUN = re.compile(r"([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)")
_SUP_MAP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
            "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+"}
_VULGAR = {"½": "(1/2)", "⅓": "(1/3)", "⅔": "(2/3)", "¼": "(1/4)", "¾": "(3/4)",
           "⅕": "(1/5)", "⅖": "(2/5)", "⅗": "(3/5)", "⅘": "(4/5)",
           "⅙": "(1/6)", "⅚": "(5/6)", "⅛": "(1/8)", "⅜": "(3/8)", "⅝": "(5/8)", "⅞": "(7/8)"}

_MINUSES = "−–—―‐‑‒﹣"          # 各种横线/负号 → ASCII '-'

_META = re.compile(r"【(?:知识点|解析|分析|解答|点评|答案|点睛|考点|详解|思路)】")
_LEAD = re.compile(r"^(?:解|答|原式|证明|原方程)\s*[:：]\s*")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_RUN = r"[零〇一二三四五六七八九十百千万两]+"
_RE_CN = re.compile(r"(?<![\u4e00-\u9fff])(" + _CN_RUN + r")(?![\u4e00-\u9fff])")
_RE_CN_NEG = re.compile(r"负(?=[零〇一二三四五六七八九十百千万两\d])")
#: 「三分之一 / 百分之五十」：X分之Y → Y/X
_RE_CN_FRACTION = re.compile(
    r"([零〇一二三四五六七八九十百千万两\d]+)\s*分之\s*([零〇一二三四五六七八九十百千万两\d]+)")
#: 「零点五 / 一点五」：中文小数点
_RE_CN_DOT = re.compile(
    r"([零〇一二三四五六七八九十百千万两\d]+)\s*点\s*([零〇一二三四五六七八九\d]+)")
#: 口语金额「三块五」→ 3.5（块 = 元）
_RE_CN_KUAI = re.compile(
    r"([零〇一二三四五六七八九十百千万两\d]+)\s*块\s*([零〇一二三四五六七八九\d]+)")
_RE_MIXED_CN = re.compile(r"(" + _CN_RUN + r")又(" + _CN_RUN + r")分之(" + _CN_RUN + r")")
_RE_MIXED_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*又\s*(\d+)\s*/\s*(\d+)")
#: 学生手写「A B/C」（空格带分数）：不只整串，x=6 1/7 这种带前缀也要认
_RE_MIXED_SPACE = re.compile(r"(?<![\d.])(\d+)\s+(\d+)\s*/\s*(\d+)(?![\d/])")
#: 题库用 `整数(分子)/(分母)` 表示带分数（21(1)/(4) = 21 又 1/4）；前面不能是数字/字母/乘号，
#: 分子必须小于分母，后面不能紧跟数字或字母（`2*(5)/(7)`、`2x(5)/(7)` 不转）。
_RE_MIXED_PAREN = re.compile(r"(?<![\d.a-zA-Z*^])(-?)(\d+)\((\d+)\)/\((\d+)\)(?![\dA-Za-z])")

#: 简单分数 a/b（分子分母都是整数，允许括号与空白）—— 用于"分数被 OCR 拆成两个数"的兜底比对
_FRAC_RE = re.compile(r"^\(?\s*(-?\d+)\s*\)?\s*/\s*\(?\s*(-?\d+)\s*\)?$")

#: 答案前的变量前缀（`x=` / `y =` / `a：`）—— 判最终答案时等价于没写
_RE_LEAD_VAR = re.compile(r"^\s*([a-zA-Z])\s*[=＝:：]\s*")

_RE_SQRT = re.compile(r"(?:√|根号下?)\s*(\([^()]*\)|\d+(?:\.\d+)?|[a-zA-Z])")
_RE_CBRT = re.compile(r"(?:∛|三次根号下?)\s*(\([^()]*\)|\d+(?:\.\d+)?|[a-zA-Z])")
_RE_ABS = re.compile(r"\|([^|]*)\|")
_RE_DMS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:°|度)\s*"
    r"(?:(\d+(?:\.\d+)?)\s*(?:′|'|分)\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:″|\"|秒)\s*)?")

_RE_PLUSMINUS = re.compile(r"±\s*(\d+(?:\.\d+)?)")
#: 科学计数法的 e 记法：1.6e12、2.5e-3、3E8
_RE_E_NOTATION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[eE]\s*([+-]?\d+)(?![\d.])")

# 单位 → (基准, 系数)。只收「数值型」单位；单字母 m/g 靠"前面必须是纯数字"消歧。
_UNIT_TABLE = [
    ("平方厘米", "area_cm2", 1.0), ("平方米", "area_m2", 1.0), ("立方厘米", "vol_cm3", 1.0),
    ("立方分米", "vol_dm3", 1.0), ("立方米", "vol_m3", 1.0),
    ("千米", "m", 1000.0), ("公里", "m", 1000.0), ("厘米", "m", 0.01), ("毫米", "m", 0.001),
    ("分米", "m", 0.1), ("米", "m", 1.0),
    ("千克", "g", 1000.0), ("公斤", "g", 1000.0), ("克", "g", 1.0), ("吨", "g", 1000000.0),
    ("分钟", "s", 60.0), ("小时", "s", 3600.0), ("秒钟", "s", 1.0), ("秒", "s", 1.0),
    ("天", "s", 86400.0), ("日", "s", 86400.0),
    ("元", "y", 1.0), ("角", "y", 0.1),
    ("个", "", 1.0), ("人", "", 1.0), ("岁", "", 1.0), ("本", "", 1.0), ("支", "", 1.0),
    ("张", "", 1.0), ("页", "", 1.0), ("件", "", 1.0), ("次", "", 1.0), ("种", "", 1.0),
    ("倍", "", 1.0), ("度", "deg", 1.0), ("°", "deg", 1.0),
    ("km", "m", 1000.0), ("cm", "m", 0.01), ("mm", "m", 0.001), ("dm", "m", 0.1),
    ("kg", "g", 1000.0), ("m", "m", 1.0), ("g", "g", 1.0),
    ("%", "%", 0.01),
]
_UNITS = {name: (base, f) for name, base, f in _UNIT_TABLE}
_UNIT_NAMES = sorted(_UNITS, key=len, reverse=True)

_TRUE_WORDS = {"对", "正确", "√", "✓", "∨", "是", "t", "true", "y", "yes", "正确无误"}
_FALSE_WORDS = {"错", "错误", "×", "✗", "否", "f", "false", "n", "no", "x", "不正确"}

_RE_PART = re.compile(r"(?<![/\*^])[(（]\s*(\d{1,2})\s*[)）](?![/\*^])")
_RE_SPACE = re.compile(r"[ \t]+")


# ---------------------------------------------------------------- 基础工具

def _fmt_num(x):
    if x is None:
        return ""
    if isinstance(x, float) and math.isinf(x):
        return "∞" if x > 0 else "-∞"
    if abs(x - round(x)) < 1e-12:
        return str(int(round(x)))
    return ("%.10g" % x)


def _ev(node):
    """受限 AST 求值：只允许数字、四则、幂、括号、sqrt()/cbrt()/abs()。"""
    if isinstance(node, ast.Expression):
        return _ev(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise ValueError("非数值常量")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _ev(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp):
        left, right = _ev(node.left), _ev(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 24:
                raise ValueError("幂次过大")
            return left ** right
        raise ValueError("不支持的运算符")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in ("sqrt", "cbrt", "abs"):
        if len(node.args) != 1:
            raise ValueError("函数参数个数")
        v = _ev(node.args[0])
        if node.func.id == "sqrt":
            return math.sqrt(v)
        if node.func.id == "cbrt":
            return math.copysign(abs(v) ** (1.0 / 3.0), v)
        return abs(v)
    raise ValueError("不支持的表达式")


_RE_NUM_CHARS = re.compile(r"^[0-9+\-*/^().]*$")
_RE_NUM_CHARS_FN = re.compile(r"^[0-9+\-*/^().abcrqst]*$")   # sqrt/cbrt/abs 的字母


def _eval_num(expr):
    """把纯数值表达式算成 float；不是纯数值就 None（ast 白名单，不用 eval）。"""
    if expr is None:
        return None
    s = str(expr).strip()
    if re.search(r"\s{2,}", s):
        return None          # 多空格是「多个答案」的分隔（61     12），不是一行算式
    s = re.sub(r"(\d)\s+(\d)", r"\1*\2", s)     # 1.6 10^12 → 1.6*10^12（隐含乘号）
    s = s.replace(" ", "")
    if not s:
        return None
    if "sqrt" in s or "cbrt" in s or "abs" in s:
        if not _RE_NUM_CHARS_FN.match(s):
            return None
        s = re.sub(r"(\d)(sqrt|cbrt|abs|\()", r"\1*\2", s)      # 2sqrt(2) → 2*sqrt(2)
    elif not _RE_NUM_CHARS.match(s):
        return None
    try:
        return float(_ev(ast.parse(s.replace("^", "**"), mode="eval")))
    except Exception:  # noqa: BLE001 —— 任何异常都当"不是数值"
        return None


def _num_eq(x, y, tol=1e-6):
    if x is None or y is None:
        return False
    if x == y:                       # +∞ == +∞、-∞ == -∞
        return True
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))


def _strip_outer_parens(s):
    while len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        depth = 0
        wrapped = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    wrapped = False
                    break
        if not wrapped or depth != 0:
            break
        s = s[1:-1]
    return s


def _cn_run_to_int(run):
    total, num = 0, 0
    for ch in run:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if num == 0:
                num = 1
            total += num * unit
            num = 0
        elif ch == "万":
            total = (total + num) * 10000
            num = 0
    return total + num


class _Pre:
    """预处理结果：规范文本 + 是否出现过圈号（决定序号集合语义）。"""

    __slots__ = ("text", "circled")

    def __init__(self, text, circled=False):
        self.text = text
        self.circled = circled

    def __repr__(self):  # pragma: no cover - 调试用
        return f"_Pre({self.text!r}, circled={self.circled})"


# ---------------------------------------------------------------- 预处理

_RE_IMPLICIT_MUL = re.compile(r"(\d)\s+(\S+)")


def _implicit_mul_rep(m):
    right = m.group(2)
    if re.fullmatch(r"\d+(?:\.\d+)?", right):
        return m.group(0)                    # 右侧纯数字：可能是另一个答案，不合并
    if not re.search(r"[a-zA-Z(^]", right):
        return m.group(0)                    # 右侧没有式子特征（如「12元」），不合并
    if re.match(r"\(\d{1,2}\)(?!\^)", right):
        return m.group(0)                    # 「(1)5 (2)10」是小问编号，不是乘法（(10)^(12) 是底数）
    if right in _UNITS:
        return m.group(0)                    # 「5 cm」是数值+单位，不是 5×cm
    return m.group(1) + "*" + right


def _SUP_TRANS(run):
    return "".join(_SUP_MAP.get(ch, "") for ch in run)


def _expand_sqrt(s):
    """√x → sqrt(x)；∛x → cbrt(x)（「根号下 / 三次根号」都认）。"""
    s = s.replace("根号下", "根号").replace("三次根号下", "三次根号")
    prev = None
    while prev != s:
        prev = s
        s = _RE_SQRT.sub(lambda m: "sqrt(" + m.group(1) + ")", s)
        s = _RE_CBRT.sub(lambda m: "cbrt(" + m.group(1) + ")", s)
    return s


def _expand_abs(s):
    """|−2| → abs(-2)；含字母/汉字的 |x| 保持原样（变量绝对值不能数值化）。"""
    def rep(m):
        inner = m.group(1)
        if not inner or re.search(r"[a-zA-Z\u4e00-\u9fff]", inner):
            return m.group(0)
        return "abs(" + inner + ")"
    return _RE_ABS.sub(rep, s)


def _cn_value(text):
    """中文/阿拉伯混合数字串 → int。"""
    t = text.strip()
    if re.fullmatch(r"\d+", t):
        return int(t)
    return _cn_run_to_int(t)


def _expand_cn_readings(s):
    """中文读法：三分之一 → (1)/(3)、零点五 → 0.5、三块五 → 3.5。"""
    def frac(m):
        den, num = _cn_value(m.group(1)), _cn_value(m.group(2))
        return "(%d)/(%d)" % (num, den) if den else m.group(0)

    def decimal(m):
        whole, tail = m.group(1), m.group(2)
        if re.fullmatch(r"\d+", tail):
            digits = tail
        else:
            digits = "".join(str(_CN_DIGITS[ch]) for ch in tail if ch in _CN_DIGITS)
        return "%d.%s" % (_cn_value(whole), digits) if digits else m.group(0)

    s = _RE_CN_FRACTION.sub(frac, s)
    s = _RE_CN_DOT.sub(decimal, s)
    s = _RE_CN_KUAI.sub(decimal, s)
    return s


def _expand_dms(s):
    """30°30′ → 30.5（度分秒 → 十进制度）。"""
    def rep(m):
        d = float(m.group(1))
        mm = float(m.group(2)) if m.group(2) else 0.0
        ss = float(m.group(3)) if m.group(3) else 0.0
        return _fmt_num(d + mm / 60.0 + ss / 3600.0)
    return _RE_DMS.sub(rep, s)


def _expand_mixed_paren(s):
    """A(B)/(C) → 带分数 A + B/C（B<C 才转；负号在前整体取负）。

    题库/源 docx 的带分数就是这么写的（21(1)/(4) = 21 又 1/4），学生却写 21又1/4 / 21 1/4 / 85/4；
    不转就会「答对判错」。`2*(5)/(7)` / `2x(5)/(7)` 保持乘法/代数式不动。
    """
    def rep(m):
        sign, whole, num, den = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if den == 0 or num >= den:
            return m.group(0)                  # 不是合法带分数（分子 ≥ 分母）
        val = whole + num / den
        return _fmt_num(-val if sign == "-" else val)
    return _RE_MIXED_PAREN.sub(rep, s)


def _expand_mixed(s):
    """带分数：1又1/2、一又二分之一、1 1/2 → 1.5。"""
    def cn_rep(m):
        whole = _cn_run_to_int(m.group(1))
        den = _cn_run_to_int(m.group(2))      # 「二分之一」→ 分母 2
        num = _cn_run_to_int(m.group(3))      #              → 分子 1
        if not den:
            return m.group(0)
        return _fmt_num(whole + num / den)

    s = _RE_MIXED_CN.sub(cn_rep, s)
    s = _RE_MIXED_NUM.sub(
        lambda m: _fmt_num(float(m.group(1)) + float(m.group(2)) / float(m.group(3))), s)
    def space_rep(m):
        whole, num, den = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if den == 0 or num >= den:            # 不是合法带分数（分子 ≥ 分母）
            return m.group(0)
        return _fmt_num(whole + num / den)

    return _RE_MIXED_SPACE.sub(space_rep, s)


def _cn_to_digits(s):
    """中文数字 → 阿拉伯数字（独立出现才转，避免误伤「同一个数」）。"""
    s = _RE_CN_NEG.sub("-", s)
    return _RE_CN.sub(lambda m: str(_cn_run_to_int(m.group(1))), s)


def _pre(raw):
    s = "" if raw is None else str(raw)
    s = (s.replace("\u00a0", " ").replace("\u3000", " ").replace("\u200b", "")
          .replace("\ufeff", ""))
    s = _META.split(s)[0]
    s = _LEAD.sub("", s.strip())

    circled = any(ch in _CIRCLED for ch in s) or any(ch in _PAREN_CIRCLED for ch in s)

    # 上标 / 常见分数符号（必须在 NFKC 前，否则 ² 会被并成 2）
    s = _RE_SUPERSCRIPT_RUN.sub(lambda m: "^" + _SUP_TRANS(m.group(1)), s)
    for k, v in _SUPERSCRIPTS.items():
        s = s.replace(k, "^" + v)
    for k, v in _VULGAR.items():
        s = s.replace(k, v)
    for k, v in _CIRCLED_MAP.items():
        s = s.replace(k, v)
    for k, v in _PAREN_MAP.items():
        s = s.replace(k, v)

    # 全半角统一
    s = unicodedata.normalize("NFKC", s)

    # 符号统一
    for ch in _MINUSES:
        s = s.replace(ch, "-")
    s = (s.replace("×", "*").replace("·", "*").replace("÷", "/")
          .replace("✕", "*").replace("✖", "*").replace("⋅", "*").replace("∙", "*")
          .replace("＊", "*").replace("﹡", "*")
          .replace("≠", "!=").replace("的平方", "^2").replace("的立方", "^3")
          .replace("≤", "<=").replace("≥", ">=")
          .replace("＜", "<").replace("＞", ">"))

    # 数字 + 空格 + 含字母/括号/^ 的式子 → 隐含乘号（`3 4n-1` / `1.6 (10)^12`）；
    # 右侧是纯数字时**不**合并，留给多值判断（`61     12`、`20 10` 是两个答案）
    s = _RE_IMPLICIT_MUL.sub(_implicit_mul_rep, s)

    # 连乘链：`5 6 7 8+1`（OCR 把一串 × 都吃成单空格）→ 全部按乘法；
    # 多值答案用的是多空格（`61     12`），所以只要出现连续空格就不动
    if not re.search(r"\s{2,}", s) and len(re.findall(r"(?<=\d)\s+(?=\d)", s)) >= 2:
        s = re.sub(r"(?<=\d)\s+(?=\d)", "*", s)

    # 科学计数法 e 记法 → 10 的幂（1.6e12 / 2.5e-3）
    s = _RE_E_NOTATION.sub(lambda m: "%s*10^%s" % (m.group(1), m.group(2)), s)

    # 带分数（A(B)/(C) 与 又/空格）→ 根号/立方根 → 绝对值 → 中文读法 → 度分秒 → ± → 中文数字
    s = _expand_mixed_paren(s)
    s = _expand_mixed(s)
    s = _expand_sqrt(s)
    s = _expand_abs(s)
    s = _expand_cn_readings(s)
    s = _expand_dms(s)
    s = _RE_PLUSMINUS.sub(lambda m: "%s或-%s" % (m.group(1), m.group(1)), s)
    s = _cn_to_digits(s)

    s = s.replace("\r", " ").replace("\n", " ")
    s = _RE_SPACE.sub(" ", s).strip()
    return _Pre(s, circled)


# ---------------------------------------------------------------- 规范形

def _canon_body(s):
    """单块规范形：去空格、分隔符收敛、幂/分式括号收敛、去外层括号。"""
    if not s:
        return ""
    s = s.replace(" ", "")
    for ch in ("，", "、", "；", ";"):
        s = s.replace(ch, ",")
    s = re.sub(r"\^\(([^()]*)\)", r"^\1", s)          # ^(2) → ^2
    s = re.sub(r"\(([a-zA-Z0-9.]+)\)\^", r"\1^", s)   # (b)^2 → b^2
    s = re.sub(r"\((\d+(?:\.\d+)?)\)", r"\1", s)      # (11) → 11
    s = re.sub(r"(?<=[0-9])\*(?=[a-zA-Z(])", "", s)   # 2*n → 2n
    s = _strip_outer_parens(s)
    s = s.strip(" ,;.．。")
    return s


def _split_parts(text):
    """按 (1)(2)… 切小问；返回 [(编号或None, 正文)]。"""
    if not text:
        return [(None, "")]
    marks = list(_RE_PART.finditer(text))
    if not marks:
        return [(None, text)]
    parts = []
    if marks[0].start() > 0:
        parts.append((None, text[:marks[0].start()]))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts.append((m.group(1), text[m.end():end]))
    return parts


def _looks_atomic(x):
    x = x.strip()
    if not x:
        return False
    if re.fullmatch(r"[0-9]+", x):
        return True
    return _eval_num(x) is not None or _eval_num(_strip_lhs(x)) is not None


def _split_multi(text):
    """把「多空/多项」拆开：分号、顿号无条件拆；逗号/空格只在两侧都是数值时拆。"""
    if not text:
        return [text]
    pieces = [text]
    for sep in (";", "、", "；"):
        out = []
        for p in pieces:
            out.extend(p.split(sep))
        pieces = out
    if any("," in p for p in pieces):
        out = []
        for p in pieces:
            out.extend(p.split(","))
        out = [x for x in out if x.strip()]
        if len(out) > len(pieces) and all(_looks_atomic(x) for x in out):
            pieces = out
    out = []
    for p in pieces:
        out.extend(re.split(r"\s{2,}|\t+", p))
    pieces = [x for x in out if x.strip()]
    out = []
    for p in pieces:
        bits = p.split(" ")
        if len(bits) == 2 and all(_looks_atomic(x) for x in bits) \
                and "^" not in p:            # `1.6 10^12` 是一个乘式，不是两个答案
            out.extend(bits)
        else:
            out.append(p)
    return [x.strip() for x in out if x.strip()] or [text]


def _strip_lhs(s):
    """去掉 x= / y= / a= / n= 这类变量前缀（只认纯变量，不碰 x+y=5）。"""
    if s is None:
        return None
    m = re.fullmatch(r"\s*([a-zA-Z]{1,2})\s*=\s*(.+)", str(s).strip())
    if m:
        return m.group(2).strip()
    return str(s).strip()


# ---------------------------------------------------------------- 单位 / 数值

def _extract_unit(s):
    """拆出「数值 + 单位」：返回 (value_expr, unit, base, factor)。"""
    if not s:
        return s, None, None, None
    for name in _UNIT_NAMES:
        if s.endswith(name):
            head = s[:-len(name)].strip()
            if head and _eval_num(head) is not None:
                base, factor = _UNITS[name]
                return head, name, base, factor
    return s, None, None, None


def _cmp_numeric(a, b):
    va, ua, base_a, fa = _extract_unit(a)
    vb, ub, base_b, fb = _extract_unit(b)
    xa = _eval_num(_strip_lhs(va))
    xb = _eval_num(_strip_lhs(vb))
    if xa is None or xb is None:
        return None
    if ua == "%" or ub == "%":
        ya = xa * (fa if ua == "%" else 1.0)
        yb = xb * (fb if ub == "%" else 1.0)
        if _num_eq(ya, yb):
            return True, "百分数与数值等价"
        return False, "百分数不等价：%s vs %s" % (_fmt_num(ya), _fmt_num(yb))
    if ua and ub:
        if base_a != base_b:
            return False, "单位不同：%s vs %s" % (ua, ub)
        if _num_eq(xa * fa, xb * fb):
            return True, "数值等价（单位换算 %s/%s）" % (ua, ub)
        return False, "数值不等：%s%s vs %s%s" % (_fmt_num(xa), ua, _fmt_num(xb), ub)
    if _num_eq(xa, xb):
        return True, "数值等价"
    return False, "数值不等：%s vs %s" % (_fmt_num(xa), _fmt_num(xb))


def _cmp_eval_strict(a, b):
    """受控求值兜底（相对误差 <1e-9）：√8 ↔ 2√2、∛8 ↔ 2、|−2| ↔ 2。"""
    xa = _eval_num(_strip_lhs(a))
    xb = _eval_num(_strip_lhs(b))
    if xa is None or xb is None:
        return None
    if _num_eq(xa, xb, tol=1e-9):
        return True, "受控求值等价（%s ≈ %s）" % (_fmt_num(xa), _fmt_num(xb))
    return False, "求值不等：%s vs %s" % (_fmt_num(xa), _fmt_num(xb))


# ---------------------------------------------------------------- 区间 / 不等式

_RE_INTERVAL = re.compile(r"^([\[\(])\s*([^,]+?)\s*,\s*([^\]\)]+?)\s*([\]\)])$")
_RE_CHAIN = re.compile(r"^(.+?)\s*(<=|>=|<|>)\s*([a-zA-Z])\s*(<=|>=|<|>)\s*(.+)$")
_RE_SINGLE = re.compile(r"^([a-zA-Z])\s*(<=|>=|<|>)\s*(.+)$")
_INF = float("inf")


def _bound(text):
    t = text.strip()
    if t in ("+∞", "∞", "+inf", "inf"):
        return _INF
    if t in ("-∞", "-inf"):
        return -_INF
    return _eval_num(t)


def _to_interval(s):
    if not s:
        return None
    t = s.strip()
    m = re.fullmatch(r"\{\s*[a-zA-Z]\s*\|\s*(.+)\s*\}", t)
    if m:
        t = m.group(1).strip()
    m = _RE_INTERVAL.match(t)
    if m:
        lo, hi = _bound(m.group(2)), _bound(m.group(3))
        if lo is None or hi is None:
            return None
        return {"lo": lo, "lo_inc": m.group(1) == "[", "hi": hi, "hi_inc": m.group(4) == "]"}
    m = _RE_CHAIN.match(t)
    if m:
        lo, op1, _var, op2, hi = m.groups()
        xlo, xhi = _bound(lo), _bound(hi)
        if xlo is None or xhi is None:
            return None
        return {"lo": xlo, "lo_inc": op1 in ("<=", ">="), "hi": xhi, "hi_inc": op2 in ("<=", ">=")}
    m = _RE_SINGLE.match(t)
    if m:
        _var, op, val = m.groups()
        v = _bound(val)
        if v is None:
            return None
        if op in (">", ">="):
            return {"lo": v, "lo_inc": op == ">=", "hi": _INF, "hi_inc": False}
        return {"lo": -_INF, "lo_inc": False, "hi": v, "hi_inc": op == "<="}
    return None


def _cmp_interval(a, b):
    ia, ib = _to_interval(a), _to_interval(b)
    if not ia or not ib:
        return None
    ok = (ia["lo_inc"] == ib["lo_inc"] and ia["hi_inc"] == ib["hi_inc"]
          and _num_eq(ia["lo"], ib["lo"]) and _num_eq(ia["hi"], ib["hi"]))
    if ok:
        return True, "区间等价"
    return False, "区间不同：%s vs %s" % (a, b)


# ---------------------------------------------------------------- 序号集合

def _as_index_items(s, expand):
    if not re.fullmatch(r"[0-9,]*", s):
        return None
    toks = [t for t in re.split(r"[,\s]+", s) if t]
    if not toks:
        return None
    items = []
    for t in toks:
        if len(t) > 1 and expand:
            items.extend(list(t))
        else:
            items.append(t)
    return items


def _cmp_index(a, b, ca, cb):
    sa = _as_index_items(a, ca or cb)
    sb = _as_index_items(b, ca or cb)
    if sa is None or sb is None:
        return None
    if len(sa) <= 1 and len(sb) <= 1 and not (ca or cb):
        return None            # 纯数字交给数值比较，避免 12/21 被当集合
    if sorted(sa) == sorted(sb):
        return True, "序号集合等价"
    return False, "序号集合不同：%s vs %s" % (",".join(sa), ",".join(sb))


# ---------------------------------------------------------------- 或 / 分段

def _split_or(s):
    return [x for x in re.split(r"或|或者", s) if x.strip()]


def _cmp_or(a, b, ca, cb, depth):
    oa, ob = _split_or(a), _split_or(b)
    if len(oa) <= 1 and len(ob) <= 1:
        return None
    if len(oa) != len(ob):
        return False, "多解个数不同：%d vs %d" % (len(oa), len(ob))
    unused = list(ob)
    for x in oa:
        for i, y in enumerate(unused):
            if _cmp_chunk(x, y, ca, cb, depth + 1)[0]:
                unused.pop(i)
                break
        else:
            return False, "多解（或）不一致：%s vs %s" % ("或".join(oa), "或".join(ob))
    return True, "多解（或）逐支等价"


# ---------------------------------------------------------------- 单块比较

def _cmp_chunk(a, b, ca, cb, depth=0):
    if a == b:
        return True, "归一化后完全相同"
    if depth > 3:
        return False, "比较层数过深"
    r = _cmp_or(a, b, ca, cb, depth)
    if r is not None:
        return r
    r = _cmp_interval(a, b)
    if r is not None:
        return r
    if ca or cb:                     # 圈号答案优先按序号集合比（③①② ≠ 312）
        r = _cmp_index(a, b, ca, cb)
        if r is not None:
            return r
    r = _cmp_numeric(a, b)
    if r is not None:
        return r
    r = _cmp_index(a, b, ca, cb)
    if r is not None:
        return r
    r = _cmp_eval_strict(a, b)       # 受控求值兜底（根式/绝对值/立方根）
    if r is not None:
        return r
    xa, xb = _canon_body(_strip_lhs(a)), _canon_body(_strip_lhs(b))
    if xa and xa == xb:
        return True, "规范形一致"
    return False, "不等价：%r vs %r" % (a, b)


# ---------------------------------------------------------------- 多小问 / 多空

def _cmp_parts(pa, pb):
    sa, sb = _split_parts(pa.text), _split_parts(pb.text)
    has_a = len(sa) > 1 or sa[0][0] is not None
    has_b = len(sb) > 1 or sb[0][0] is not None
    if not (has_a and has_b):
        return None
    if any(k is None for k, _ in sa) or any(k is None for k, _ in sb):
        return None
    ma = {k: v for k, v in sa}
    mb = {k: v for k, v in sb}
    if set(ma) != set(mb):
        return False, "小问编号不同：%s vs %s" % (sorted(ma), sorted(mb))
    for k in sorted(ma, key=lambda x: int(x)):
        ok, reason = _cmp_chunk(ma[k].strip(" ;,，；"), mb[k].strip(" ;,，；"),
                                pa.circled, pb.circled)
        if not ok:
            return False, "第(%s)问不等价：%s" % (k, reason)
    return True, "多小问逐段等价（%d 段）" % len(ma)


def _expected_token_groups(val):
    """把"期望的一个答案"展开成它对应的整数序列（1/2/3 个 token）与种类。"""
    s = val.strip()
    m = _FRAC_RE.match(s)
    if m:
        return [m.group(1), m.group(2)], "分数"
    m = _RE_MIXED_PAREN.match(s)          # 21(1)/(4)：(-?)(整数)\(分子\)/\(分母\)
    if m:
        return [m.group(1) + m.group(2), m.group(3), m.group(4)], "带分数"
    m = _RE_MIXED_SPACE.match(s)          # 21 1/4
    if m:
        return [m.group(1), m.group(2), m.group(3)], "带分数"
    return [s], "普通"


def _tokens(s):
    return [t for t in re.split(r"[\s,，;；]+", str(s).strip()) if t]


def _cmp_pair_maybe_split(x, y, ca, cb):
    """单对比较；顺带认「一个分数被 OCR 拆成 2~3 个数」这种写法。"""
    ok, why = _cmp_chunk(x, y, ca, cb)
    if ok:
        return True, why
    for expect, got in ((y, x), (x, y)):
        groups, kind = _expected_token_groups(expect)
        if kind == "普通":
            continue
        toks = _tokens(got)
        if toks == groups:
            return True, "%s被识别成多个数（%s），分子分母对齐" % (kind, " ".join(groups))
    return False, why


def _align_split_values(split_vals, whole_vals, ca, cb):
    """一侧被 OCR 拆成了多个数（`2011 2012` / `-2 5 7`），按分子分母对齐后再比。

    只有"拆出来的数正好等于对面分数/带分数的分子分母"才认；任何一项对不上就返回 None，
    由调用方继续走别的兜底（真·多值答案不会被硬并成分数）。
    """
    used_split_frac = False
    i = 0
    for whole in whole_vals:
        groups, kind = _expected_token_groups(whole)
        if kind != "普通":
            used_split_frac = True
            n = len(groups)
            got = [t.strip() for t in split_vals[i:i + n]]
            if len(got) != n or got != groups:
                return None
            i += n
            continue
        if i >= len(split_vals):
            return None
        ok, _why = _cmp_chunk(split_vals[i], whole, ca, cb)
        if not ok:
            return None
        i += 1
    if i != len(split_vals) or not used_split_frac:
        return None
    return True, "分数/带分数被识别成多个数，已按分子分母对齐（%d 项）" % len(whole_vals)


def _strip_answer_parens(s):
    """去掉整串外面包的括号：`(-100,-2 5 7)` → `-100,-2 5 7`（学生习惯性加括号）。

    只在"剥掉后括号仍平衡"时才剥，避免把 `(3+4)` 这种真括号语义改掉。
    注意与上面 `_strip_outer_parens` 的区别：这个是给"整串答案"用的。
    """
    t = str(s).strip()
    for _ in range(3):
        if len(t) < 2 or t[0] not in "(（" or t[-1] not in ")）":
            break
        inner = t[1:-1]
        depth = 0
        ok = True
        for ch in inner:
            if ch in "(（":
                depth += 1
            elif ch in ")）":
                depth -= 1
                if depth < 0:
                    ok = False
                    break
        if not ok or depth != 0:
            break
        t = inner.strip()
    return t


def _try_split_fraction_match(sa, sb):
    """原始串层面先救一次：分数/带分数被 OCR 拆成多个数（`3 2` = 3/2，`-2 5 7` = -2 又 5/7）。

    必须在 `_pre()` 归一化之前做 —— 归一化会把空格变成乘号（`-2 5 7` → `-2*5*7` = -70），
    那时候再想还原就晚了。
    """
    ta, tb = _tokens(sa), _tokens(sb)
    if len(ta) < 2 and len(tb) < 2:
        return None
    r = _align_split_values(ta, tb, False, False)
    if r is not None and r[0]:
        return r
    r = _align_split_values(tb, ta, False, False)
    if r is not None and r[0]:
        return r
    return None


def _cmp_body(pa, pb):
    a, b = pa.text, pb.text
    if a == b:
        return True, "归一化后完全相同"
    r = _cmp_parts(pa, pb)
    if r is not None:
        return r
    ca, cb = _split_multi(a), _split_multi(b)
    if len(ca) > 1 or len(cb) > 1:
        if len(ca) == len(cb):
            for i, (x, y) in enumerate(zip(ca, cb), 1):
                ok, reason = _cmp_pair_maybe_split(x, y, pa.circled, pb.circled)
                if not ok:
                    return False, "第 %d 项不等价：%s" % (i, reason)
            return True, "多项逐项等价（%d 项）" % len(ca)
        r = _cmp_index(a.replace(" ", ""), b.replace(" ", ""), pa.circled, pb.circled)
        if r is not None:
            return r
        # OCR 常把乘号吃成空格：空格分隔的一侧按「隐含乘号」再试一次（逗号多值不走这条）
        # 只对「单个空格」丢的乘号兜底：多值答案在语料里是多空格分隔（61     12），不能被当乘积
        hardsep = (";" in a or ";" in b)      # 多空格换来的硬分隔：是多个答案，不是乘积
        singlespace = not (re.search(r"\s{2,}", a) or re.search(r"\s{2,}", b))
        if singlespace and not hardsep and (("," not in a and "，" not in a)
                                            or ("," not in b and "，" not in b)):
            r = _cmp_chunk("*".join(ca), "*".join(cb), pa.circled, pb.circled)
            if r is not None and r[0]:
                return True, "隐含乘号等价（%s）" % r[1]
        # 分数被 OCR 拆成两个整数（"2026 4053" 其实是 2026/4053），带分数被拆成三个
        # （"-2 5 7" 其实是 -2 又 5/7）：只有"拆出来的数正好等于对面分数的分子分母/整数部分"
        # 时才认，避免把真·多值答案并成一个分数。
        r = _align_split_values(ca, cb, pa.circled, pb.circled)
        if r is not None and r[0]:
            return r
        r = _align_split_values(cb, ca, pb.circled, pa.circled)
        if r is not None and r[0]:
            return r
        return False, "项数不同：%d vs %d" % (len(ca), len(cb))
    # 单块也可能被 OCR 拆成多个数（"-2 5 7" 其实是 -2 又 5/7），所以走带兜底的比较
    return _cmp_pair_maybe_split(a, b, pa.circled, pb.circled)


# ---------------------------------------------------------------- 选择题 / 判断题

def _only_letters(s):
    return re.fullmatch(r"[A-Za-z,，、;；\s()（）]*", s) is not None


def _choice_letters(s):
    """从选择项里取出选项字母集合；容忍 OCR 噪音（`选A` / `答案是B` / `A、`）。"""
    t = unicodedata.normalize("NFKC", s)
    if _only_letters(t):
        return {c.upper() for c in t if c.isalpha()} or None
    if re.search(r"[0-9]", t):
        return None
    letters = {c.upper() for c in t if c.isalpha() and c.upper() in "ABCDEFGH"}
    noise = re.sub(r"[A-Za-z\s,，、;；()（）.。]", "", t)
    if letters and len(noise) <= 4:      # 只容忍「选」「答案」这类少量前缀噪音
        return letters
    return None


def _cmp_choice(a, b):
    la, lb = _choice_letters(a), _choice_letters(b)
    if not la or not lb:
        return None
    if la == lb:
        return True, "选项集合等价"
    return False, "选项不同：%s vs %s" % ("".join(sorted(la)), "".join(sorted(lb)))


def _as_bool(s):
    t = s.strip().lower()
    t = t.replace(" ", "").rstrip("。.")
    if t in _TRUE_WORDS:
        return True
    if t in _FALSE_WORDS:
        return False
    return None


def _cmp_bool(a, b):
    va, vb = _as_bool(a), _as_bool(b)
    if va is None or vb is None:
        return None
    if va == vb:
        return True, "判断一致"
    return False, "判断相反：%s vs %s" % ("对" if va else "错", "对" if vb else "错")


# ---------------------------------------------------------------- 对外 API

def normalize_answer(s, qtype="填空"):
    """把答案归一化成规范串（幂等）。空答案返回 ""。"""
    if s is None:
        return ""
    p = _pre(s)
    t = p.text
    if not t:
        return ""
    q = (qtype or "").strip()
    if q == "选择":
        if _only_letters(t):
            letters = sorted({c.upper() for c in t if c.isalpha()})
            if letters:
                return "".join(letters)
        return t
    if q == "判断":
        v = _as_bool(t)
        if v is not None:
            return "对" if v else "错"
        return t
    parts = _split_parts(t)
    if len(parts) > 1 and all(k for k, _ in parts):
        return ";".join("(%s)%s" % (k, _canon_body(v.strip(" ;,，；"))) for k, v in parts)
    chunks = _split_multi(t)
    if len(chunks) > 1:
        return ",".join(_canon_body(c) for c in chunks)
    return _canon_body(t)


def is_equivalent(student, expected, qtype="填空"):
    """判等价：返回 (bool, 人话原因)。纯规则，无 LLM、无网络。"""
    if student is None or expected is None:
        return False, "答案缺失"
    sa, sb = str(student), str(expected)
    if not sa.strip() or not sb.strip():
        return False, "答案为空"
    if sa.strip() == sb.strip():
        return True, "原文完全一致"

    # 学生习惯把多个答案套在一对括号里：`(-100,-2 5 7)` 等价于 `-100,-2 5 7`
    sa, sb = _strip_answer_parens(sa), _strip_answer_parens(sb)
    if sa and sb and sa.strip() == sb.strip():
        return True, "去掉外层括号后完全一致"

    # 变量前缀 `x=` / `y=`：两边都有且不同 → 肯定不算等价；有一边没有 → 去掉再比
    # （判卷只判最终答案，"x=3" 和 "3" 是同一个答案）
    ma, mb_ = _RE_LEAD_VAR.match(sa), _RE_LEAD_VAR.match(sb)
    if ma and mb_:
        if ma.group(1).lower() != mb_.group(1).lower():
            return False, "变量不同：%s vs %s" % (ma.group(1), mb_.group(1))
        sa, sb = sa[ma.end():], sb[mb_.end():]
    elif ma:
        sa = sa[ma.end():]
    elif mb_:
        sb = sb[mb_.end():]

    # 多空格 = 多个答案的分隔（题库里写作 `61     12`）；先换成逗号，避免被当成「数字乘数字」
    sa, sb = re.sub(r"\s{2,}", ";", sa), re.sub(r"\s{2,}", ";", sb)

    # 原始串层面先救一次：分数/带分数被 OCR 拆成多个数（"3 2"=3/2、"-2 5 7"=-2又5/7）。
    # 这一步必须在 _pre() 之前，否则空格已被当成乘号（-2*5*7=-70）就还原不回来了。
    r_split = _try_split_fraction_match(sa, sb)
    if r_split is not None and r_split[0]:
        return r_split

    q = (qtype or "").strip()
    r = None
    if q == "选择":
        r = _cmp_choice(sa, sb)
    elif q == "判断":
        r = _cmp_bool(sa, sb)
    if r is None:
        pa, pb = _pre(sa), _pre(sb)
        if not pa.text or not pb.text:
            return False, "归一化后为空"
        r = _cmp_body(pa, pb)
    if r is None:
        return False, "无法判定：%r vs %r" % (sa, sb)
    return bool(r[0]), r[1]
