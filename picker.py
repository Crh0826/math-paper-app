# -*- coding: utf-8 -*-
"""T3 · 组卷选题引擎（picker.py，原名 select.py）。

契约：CONTRACT §2（组卷参数）/ §3（manifest）/ §4（`build` / `save`）。

> **不要改回 select.py**：本模块原名 `select.py`，与 stdlib 的 `select` 扩展模块同名。
> app/ 在 `sys.path` 上时，`import select` 会静默拿到本模块，`asyncio`/`selectors` 随后
> 取不到 `select.select` → `selectors.SelectSelector.select()` 直接 AttributeError，
> 排查成本极高。2026-10-31 按队长要求改名为 `picker.py`（CONTRACT §2/§4 已同步）。

设计要点
--------
* **纯读**：`build()` 只读 `questions` 表，不写库；落库由 `save()` 单独做（server 在
  paper/card 渲染成功后才写，避免半成品 manifest 进库）。
* **可复现**：每种题型各用一条 `random.Random(seed)` 随机流对「按 no 排序后的候选池」
  抽样，所以同参数同 seed 逐字节一致；且只改某一种题型的数量不会影响别的题型抽到哪些题。
* **不凑数**：某个题型的可用池不够就抛 `InsufficientQuestions`，`details` 里逐题型给出
  need / available / short，绝不静默少给题。
* **答案拆解**：语料里 `answer_raw` 有些尾部粘了 `【知识点】/【易错总结】` 注释块（168 条），
  有些题目 qtype 标错（"选择"题里装着解答内容）。拆不出可用答案的题不算"有答案"，
  不会被选进需要判分的卷子（need_answer=true 时）；关掉 need_answer 则作为练习卷入选并
  标 `practice_only=true`、`answer_key=[]`。
* **空题**：`is_empty=1` 的 512 条题没有题干，任何参数下都不入选。契约里的
  `requirements.exclude_empty` 默认 true；显式置 false 会得到明确报错（而不是静默忽略）。

manifest item 字段（CONTRACT §3）：idx / no / qtype / kp_id / family_key / stem_tokens /
answer_key / options / space_paras，另有 `practice_only`（无答案）与 `chapter`（卷面分组、
前端展示用）。`card` 字段由 card.py 负责，这里不产出。
"""
from __future__ import annotations

import copy
import datetime
import json
import random
import sqlite3
import re
import time
from collections.abc import Mapping

from taxonomy import CHAPTER_ORDER as _CHAPTER_ORDER

# ---------------------------------------------------------------- 常量
#: 契约 §1 的 qtype 取值；顺序 = 卷面分组顺序（选择 → 判断 → 填空 → 解答 → 其他）
QTYPES = ("选择", "判断", "填空", "解答", "其他")
_QT_ORDER = {q: i for i, q in enumerate(QTYPES)}

#: 契约 §3 page
PAGE = {"w_pt": 595.3, "h_pt": 841.9, "dpi": 300, "w_px": 2480, "h_px": 3508}

DEFAULT_SUBJECT = "math7a"
_FIG_CACHE: dict = {}
_EXCL_CACHE: dict = {}     # t73：体检排除题的属性（按库+学科+口径），供纯 Python 减法计数      # t73：fig_missing 计算式缓存（键=库+行数）     # t62：多学科库里不传学科 = 数学（七年级上·沪科版），防跨科混卷
DEFAULT_SEED = 42
DEFAULT_TITLE = "训练卷"
#: requirements 默认值；`exclude_mismatch=None` 表示「跟着 need_answer 走」（判分卷默认排掉答案不符的题）
DEFAULT_REQUIREMENTS = {"need_answer": True, "exclude_empty": True, "exclude_no_family": False,
                        "exclude_fig_missing": True, "exclude_mismatch": None, "strict": False}
#: 版面模式（paper.py 实现）：compact = 省纸模式（默认），normal = 宽松版面
DEFAULT_LAYOUT = "compact"
LAYOUTS = ("compact", "normal")
DEFAULT_OPTIONS = ["A", "B", "C", "D"]
LETTERS = "ABCDEF"

# ---------------------------------------------------------------- 题库体检 flag
#: 会把「不该印的东西」印上卷面（教师版解析 / 空白题干）—— 任何情况下默认排除。
#: 与 quality.py 的 PRINT_BLOCKING 一致；这里不 import quality，避免选题层依赖体检层。
PRINT_BLOCKING_FLAGS = ("no_stem", "stem_only_placeholder", "analysis_left")
#: strict 模式下额外排除（残句 / 排版过重 / 题干重复）
STRICT_FLAGS = ("short_stem", "heavy_layout", "dup_text")
#: 答案与题型不符（判卷必错）；need_answer 时默认排除
MISMATCH_FLAGS = ("answer_mismatch",)
#: manifest.stats.excluded 的四个计数键（按「先命中先记账」的顺序）
EXCLUDE_KEYS = ("print_blocking", "mismatch", "strict", "fig_missing")

#: 题干指代图的说法（用来发现「语料里根本没有图」的题）；paper.py 复用同一份规则。
#: 「如下的图案 / 如下图案」也算（语料里有「按规律拼成如下的图案，第（）个图案…」，图丢了就无解）。
FIG_REF_RX = re.compile(r"如图|见图|图中|下图|上图|图①|图②|如下的?图")

#: card.py 的作答单元容量（填空最多 12 空、解答最多 20 问）；超出的答案合并进最后一空，
#: 宁可一空里放长答案，也不能丢答案（丢答案 = 判卷必错）。
MAX_FILL_BLANKS = 12
MAX_SOLUTION_BLANKS = 20

#: 空格数上限（卷面留白段落，防手滑传 9999 把纸撑爆）
MAX_SPACE_PARAS = 40

#: 卷面留白默认全 0：答案写在答题卡上，卷面只印题目（CONTRACT §2）。
#: 要草稿区的老师显式传 space={"解答": 4}，否则一道解答题就会白占 8×24pt ≈ 6.8cm。
DEFAULT_SPACE = {"选择": 0, "判断": 0, "填空": 0, "解答": 0, "其他": 0}


class SelectError(ValueError):
    """组卷参数错误。"""


class ScopeError(SelectError):
    """scope 里的学科/章节/知识点在库里不存在（或章节不属于所选学科）。"""


class InsufficientQuestions(SelectError):
    """某题型可用题量不够。``details = {qtype: {need, available, short}}``。

    ``relax[qtype] = {"exclude_fig_missing": n, ...}``：放宽哪个开关还能多出几题，
    供 UI 直接显示「放宽『排除缺图题』可多出 N 题」。
    """

    def __init__(self, details: dict, hint: str = ""):
        self.details = dict(details)
        self.relax = {}
        parts = ["%s 需要 %d，可用 %d，差 %d" % (qt, d["need"], d["available"], d["short"])
                 for qt, d in self.details.items()]
        msg = "题量不足：" + "；".join(parts)
        if hint:
            msg += "（%s）" % hint
        super().__init__(msg)


# ---------------------------------------------------------------- 缺图判定
def has_figure(fmt: str) -> bool:
    """按 questions.fmt 判断这道题的 token 里有没有图/表（fmt 由 token 统计得来）。"""
    fmt = fmt or ""
    return ("embed" in fmt) or ("ole" in fmt) or ("table" in fmt)


def fig_missing(text_plain: str, has_fig: bool) -> bool:
    """题干指代了图（如图/图中/下图…），但题面里没有任何承载它的图/表 → 缺图题。

    判定口径（队长 2026-10-31 定）：题干有指代 **且** 该题 token 里 embed/table 为 0。
    「图在表格里」的题 fmt 带 table → 不算缺图（不误杀）。quality.py 落地 fig_missing flag 后，
    picker 会优先用体检表里的 flag（那里的判定还核对了源 docx 的 media/drawing/pict）。
    """
    return bool(FIG_REF_RX.search(text_plain or "")) and not has_fig


# ---------------------------------------------------------------- 答案拆解
#: 语料里答案尾部粘的注释块（【知识点】/【易错总结】… 或 "易错点2：" 起头）
_ANN_RX = re.compile(r"\n?\s*(?:【[^】]{1,12}】|易错点\s*\d+\s*[:：])")
#: (1) / （1）形式的第几小问
_SUB_RX = re.compile(r"[（(]\s*(\d+)\s*[)）]")
#: 整体被一对括号包住的答案（如方程组的解 (x=4; y=−2)）→ 只算一空
_WRAP_RX = re.compile(r"^[（(][^（()）]*[)）]$", re.S)
#: 多空分隔：换行 / 分号 / 2 个以上空格 / 制表符
_SPLIT_RX = re.compile(r"\n|[；;]| {2,}|\t+")
#: 每个答案片段的收尾标点
_TRAIL = " \t\u3000。．.,，;；"

#: 全角/异体符号 → ASCII（只做"看起来一样"的归一，真正的等价判定归 normalize.py）
_NORM_TABLE = str.maketrans({
    "＝": "=", "；": ";", "，": ",", "（": "(", "）": ")",
    "−": "-", "－": "-", "﹣": "-", "＋": "+", "／": "/", "：": ":",
    "\u3000": " ",
})


def clean_answer_raw(raw: str) -> str:
    """去掉答案尾部的【知识点】/【易错总结】注释块，返回纯答案文本。"""
    s = (raw or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    m = _ANN_RX.search(s)
    if m:
        s = s[:m.start()] if m.start() > 0 else s[m.end():]
    return s.strip()


def norm_value(v: str) -> str:
    """answer_key.norm：轻量归一（全角符号、空白、句末标点）。

    真正的判分等价（分数书写、单位、中文表述）由 normalize.is_equivalent 负责，
    这里只保证 "同一答案的不同输入法/空格" 看起来一致。
    """
    s = (v or "").translate(_NORM_TABLE)
    s = re.sub(r"\s+", "", s)
    return s.rstrip("。．.").strip()


def _split_sub_questions(s: str):
    """按 (1)(2)(3) 拆小问；不是从 1 开始连续编号、或少于 2 问 → None。

    关键是不能把分数 ``(4)/(3)`` 误当成第 4 小问：``)`` 后面紧跟 ``/ . ．`` 的一律不算小问标记。
    """
    marks = []
    for m in _SUB_RX.finditer(s):
        nxt = s[m.end():m.end() + 1]
        prv = s[m.start() - 1:m.start()] if m.start() else ""
        if nxt in ("/", "／", ".", "．"):
            continue
        # 前面的字符：数字/上标/除号/右括号都说明这不是小问编号，而是分数 (17)/(2) 的分子分母
        if prv and (prv.isdigit() or prv in "^_/／)）"):
            continue
        marks.append((m.start(), m.end(), int(m.group(1))))
    if len(marks) < 2:
        return None
    if [n for _, _, n in marks] != list(range(1, len(marks) + 1)):
        return None
    parts = []
    for i, (_a, b, _n) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(s)
        parts.append(s[b:end])
    return parts


def _split_simple(s: str):
    """填空/单问解答：按换行、分号、2+ 空格拆；整体被括号包住的一律不拆。"""
    if _WRAP_RX.match(s):
        return [s]
    return _SPLIT_RX.split(s)


def _blanks_key(s: str, cap: int):
    """把一段答案文本拆成多空 → [{blank, value, norm}]；超过 cap 空则合并到最后一空。"""
    parts = _split_sub_questions(s)
    if parts is None:
        parts = _split_simple(s)
    out = []
    for piece in parts:
        v = re.sub(r"\s+", " ", (piece or "").strip()).strip(_TRAIL).strip()
        if not v or v in ("．", "。", "."):
            continue
        out.append({"blank": len(out) + 1, "value": v, "norm": norm_value(v)})
    if not out:
        return []
    if len(out) > cap:  # 合并尾巴，绝不丢答案
        head = out[:cap - 1]
        tail = "；".join(k["value"] for k in out[cap - 1:])
        head.append({"blank": cap, "value": tail, "norm": norm_value(tail)})
        out = head
    return out


def _choice_key(s: str):
    """选择题/判断题：取答案首行里的选项字母（A–F）。多选 → 一空，norm 按字母排序。"""
    first = s.split("\n", 1)[0]
    letters = [c for c in first if c in LETTERS]
    if not letters:
        return []
    value = "".join(letters)
    return [{"blank": 1, "value": value, "norm": "".join(sorted(set(letters)))}]


def parse_answer_key(answer_raw: str, qtype: str):
    """answer_raw → answer_key（CONTRACT §3）。

    * 选择/判断：单字母 → ``[{"blank":1,"value":"A","norm":"A"}]``；"ACD" → 一空 value/norm
      "ACD"；答案里根本没有选项字母（qtype 标错）→ ``[]``（不可判）
    * 解答：按 (1)(2)(3) 拆小问；拆不出小问 → 一空
    * 填空：按换行/分号/2+ 空格拆多空；``(x=4; y=−2)`` 这种整体带括号的算一空
    * 无答案 → ``[]``（调用方据此标 practice_only）
    """
    s = clean_answer_raw(answer_raw)
    if not s:
        return []
    if qtype in ("选择", "判断"):
        return _choice_key(s)
    if qtype == "填空":
        return _blanks_key(s, MAX_FILL_BLANKS)
    return _blanks_key(s, MAX_SOLUTION_BLANKS)


# ---------------------------------------------------------------- 参数规整
def _as_int(v, name: str) -> int:
    if isinstance(v, bool):
        raise SelectError("%s 必须是整数（不是布尔）：%r" % (name, v))
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise SelectError("%s 必须是整数：%r" % (name, v))
    if n < 0:
        raise SelectError("%s 不能为负：%r" % (name, v))
    return n


def _as_bool(v, name: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", "", "none", "null"):
        return False
    raise SelectError("%s 必须是布尔：%r" % (name, v))


def _as_str_list(v, name: str):
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[，,、\n]", v)]
    if not isinstance(v, (list, tuple, set)):
        raise SelectError("%s 必须是字符串数组：%r" % (name, v))
    out = []
    for x in v:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def normalize_params(params: dict) -> dict:
    """校验并补默认值；返回新 dict（不改调用方入参）。未知的顶层键原样保留。"""
    if not isinstance(params, Mapping):
        raise SelectError("params 必须是 dict：%r" % (params,))
    p = copy.deepcopy(dict(params))

    mix_in = params.get("mix") or {}
    if not isinstance(mix_in, Mapping):
        raise SelectError("mix 必须是 {题型: 数量}：%r" % (mix_in,))
    mix = {}
    for k, v in mix_in.items():
        qt = str(k).strip()
        if qt not in _QT_ORDER:
            raise SelectError("未知题型：%r；可用：%s" % (k, " / ".join(QTYPES)))
        mix[qt] = _as_int(v, "mix.%s" % qt)

    #: 精确题集（训练卷用）：给了就按这个顺序出卷；exclude_nos 是拉黑名单
    nos = _as_str_list(params.get("nos"), "nos")
    exclude_nos = _as_str_list(params.get("exclude_nos"), "exclude_nos")
    p["nos"] = nos
    p["exclude_nos"] = exclude_nos

    if not mix and not nos:
        raise SelectError("mix 不能为空（至少给一种题型的数量，或给 nos 精确题集）")
    if mix and sum(mix.values()) <= 0 and not nos:
        raise SelectError("mix 总题数必须 > 0：%r" % (mix,))
    p["mix"] = mix

    title = str(params.get("title") or "").strip() or DEFAULT_TITLE
    p["title"] = title

    seed = params.get("seed", DEFAULT_SEED)
    if seed is None:
        seed = DEFAULT_SEED
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise SelectError("seed 必须是整数：%r" % (seed,))
    p["seed"] = seed

    sc = params.get("scope") or {}
    if not isinstance(sc, Mapping):
        raise SelectError("scope 必须是 dict：%r" % (sc,))
    mode = str(sc.get("kp_mode") or "any").strip().lower() or "any"
    if mode not in ("any", "all"):
        raise SelectError("scope.kp_mode 只能是 any / all：%r" % (sc.get("kp_mode"),))
    #: t11 · 学科过滤：subject_pk（单选，组卷页下拉）与 subjects（多选/接口）取并集。
    #: 两个都不给 = 不限（老接口兼容；只有单学科库的机器行为完全不变）。
    subject_pk = str(sc.get("subject_pk") or "").strip()
    subjects = _as_str_list(sc.get("subjects"), "scope.subjects")
    if subject_pk and subject_pk not in subjects:
        subjects = [subject_pk] + subjects
    subject_default = not subjects
    if subject_default:                 # t62：不传学科 → 默认数学，避免多学科混卷
        subjects = [DEFAULT_SUBJECT]
    p["scope"] = {
        "chapters": _as_str_list(sc.get("chapters"), "scope.chapters"),
        "kps": _as_str_list(sc.get("kps"), "scope.kps"),
        "kp_mode": mode,
        "subject_pk": subject_pk,
        "subjects": subjects,
        "subject_default": subject_default,
    }

    req = params.get("requirements") or {}
    if not isinstance(req, Mapping):
        raise SelectError("requirements 必须是 dict：%r" % (req,))
    rq = {}
    for k, default in DEFAULT_REQUIREMENTS.items():
        if k == "exclude_mismatch":
            continue                      # 默认值跟 need_answer 走，最后再定
        rq[k] = _as_bool(req.get(k, default), "requirements.%s" % k)
    if req.get("exclude_mismatch") is None:
        rq["exclude_mismatch"] = rq["need_answer"]   # 判分卷默认排掉答案不符的题
    else:
        rq["exclude_mismatch"] = _as_bool(req["exclude_mismatch"],
                                          "requirements.exclude_mismatch")
    if not rq["exclude_empty"]:
        # 空题（is_empty=1）没有题干，印出来是一道空白题；宁可明确报错也不静默塞进去
        raise SelectError("requirements.exclude_empty=false 不支持："
                          "空题（is_empty=1）没有题干，无法印卷")
    p["requirements"] = rq

    # 附校准页：用户反馈第 5 条（打印要 100% 比例，太脆）→ 默认附一页 100mm 校准页
    p["calibration"] = _as_bool(params.get("calibration", True), "calibration")

    layout = str(params.get("layout") or DEFAULT_LAYOUT).strip().lower()
    if layout not in LAYOUTS:
        raise SelectError("layout 只能是 %s：%r" % (" / ".join(LAYOUTS), params.get("layout")))
    p["layout"] = layout

    sp = params.get("space") or {}
    if not isinstance(sp, Mapping):
        raise SelectError("space 必须是 {题型: 段落数}：%r" % (sp,))
    space = dict(DEFAULT_SPACE)      # 没传的一律 0：卷面不留书写区
    for k, v in sp.items():
        qt = str(k).strip()
        if qt not in _QT_ORDER:
            continue  # 前端可能多传，忽略未知题型
        space[qt] = min(MAX_SPACE_PARAS, _as_int(v, "space.%s" % qt))
    p["space"] = space
    return p


# ---------------------------------------------------------------- 库查询
def _distinct(conn, sql: str, args=()):
    try:
        return {str(r[0]) for r in conn.execute(sql, list(args))
                if r[0] is not None and str(r[0]) != ""}
    except Exception:  # noqa: BLE001 - 裸连接/空库时不该炸
        return set()


def _unknown_kp(kp: str, known: set) -> bool:
    """kp 是否在库里：精确命中，或 x.0 章级命中同章的任一具体知识点。"""
    if kp in known:
        return False
    if kp.endswith(".0") and len(kp) > 2:
        return not any(k.startswith(kp[:-1]) for k in known)
    return True


def scope_subjects(scope: dict) -> list:
    """scope 里的学科列表（subject_pk + subjects 去重合并）；空 = 不限。"""
    out = []
    single = str(scope.get("subject_pk") or "").strip()
    for s in ([single] if single else []) + list(scope.get("subjects") or []):
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    return out


def _subject_clause(scope: dict, null_safe: bool = True):
    """(SQL 片段, 参数)：学科过滤。与章节/知识点是 AND 关系（选了学科再在其中选范围）。"""
    subjects = scope_subjects(scope)
    if not subjects:
        return "", []
    # 与 COALESCE(subject_pk,'math7a') IN (...) 语义等价，但写成 subject_pk IN (...) 才能吃到
    # idx_q_subject 索引（COALESCE 会让 SQLite 全表扫，t73 实测 89ms → 20ms）
    marks = ",".join("?" * len(subjects))
    if null_safe and DEFAULT_SUBJECT in subjects:
        return (" AND (subject_pk IN (%s) OR subject_pk IS NULL)" % marks, list(subjects))
    return " AND subject_pk IN (%s)" % marks, list(subjects)


def _validate_scope(conn, scope: dict) -> None:
    """校验 scope：学科必须存在，章节/知识点必须在该学科范围内（避免跨学科混选）。"""
    subjects = scope_subjects(scope)
    subject_sql, subject_args = _subject_clause(scope)
    known_sub = (_distinct(conn, "SELECT subject_pk FROM subjects")
                 | _distinct(conn, "SELECT DISTINCT subject_pk FROM questions"))
    try:
        if conn.execute("SELECT 1 FROM questions WHERE subject_pk IS NULL LIMIT 1").fetchone():
            known_sub = known_sub | {DEFAULT_SUBJECT}          # NULL 当 math7a
    except Exception:
        pass
    if known_sub:
        bad_sub = [s for s in subjects if s not in known_sub]
        if bad_sub:
            raise ScopeError("未知学科：%s；库里可用学科：%s"
                             % ("、".join(bad_sub), "、".join(sorted(known_sub))))

    chapters, kps = scope["chapters"], scope["kps"]
    if not chapters and not kps:
        return
    ch_sql = "SELECT DISTINCT chapter FROM questions WHERE 1=1" + subject_sql
    kp_sql_q = "SELECT kp_id FROM kps WHERE 1=1" + subject_sql
    kp_q_sql = "SELECT DISTINCT kp_id FROM questions WHERE 1=1" + subject_sql
    known_ch = _distinct(conn, ch_sql, subject_args)
    known_kp = _distinct(conn, kp_sql_q, subject_args) | _distinct(conn, kp_q_sql, subject_args)
    if subjects:
        # 只查了学科内的章节；若该学科在 questions 里还没有行，用 kps 表兜底一次
        if not known_ch:
            known_ch = _distinct(conn, "SELECT DISTINCT chapter FROM kps WHERE 1=1" + subject_sql,
                                 subject_args)
        if not known_kp:
            known_kp = _distinct(conn, "SELECT kp_id FROM kps WHERE 1=1" + subject_sql,
                                 subject_args)
    # t97：章节必须属于当前学科。以前 known_ch 为空（该学科题目没写章节）时整段跳过，
    # 于是「物理 + 残留的数学章节」被当成合法过滤 → 命中 0 行 → 报「题量不够」。
    if subjects:
        if chapters and not known_ch:
            raise ScopeError(
                "这些章节不属于当前学科（%s）：%s；本学科可用章节：（无，该学科题目还没有章节信息）"
                % ("、".join(subjects), "、".join(chapters)))
        bad_ch = [c for c in chapters if c not in known_ch]
        if bad_ch:
            raise ScopeError(
                "这些章节不属于当前学科（%s）：%s；本学科可用章节：%s"
                % ("、".join(subjects), "、".join(bad_ch),
                   "、".join(sorted(known_ch)[:80])))
    else:
        bad_ch = [c for c in chapters if known_ch and c not in known_ch]
        if bad_ch:
            raise ScopeError("未知章节：%s；库里可用章节：%s"
                             % ("、".join(bad_ch), "、".join(sorted(known_ch)[:80])))
    bad_kp = [k for k in kps if known_kp and _unknown_kp(k, known_kp)]
    if bad_kp:
        raise ScopeError("未知知识点：%s；库里可用知识点（部分）%s：%s"
                         % ("、".join(bad_kp),
                            "（学科 %s 内）" % "、".join(subjects) if subjects else "",
                            "、".join(sorted(known_kp)[:80])))


def _scope_clause(scope: dict, null_safe: bool = True):
    """(SQL 片段, 参数)。学科是 AND；章节/知识点两列表都空 → 该维度不限制；
    x.0 → 整章（同前缀的全部 kp）。"""
    subject_sql, subject_args = _subject_clause(scope, null_safe=null_safe)
    chapters, kps, mode = scope["chapters"], scope["kps"], scope["kp_mode"]
    if not chapters and not kps:
        return subject_sql, subject_args
    sql, args = "", []
    if chapters:
        sql = "chapter IN (%s)" % ",".join("?" * len(chapters))
        args += list(chapters)
    if kps:
        ors, kp_args = [], []
        for kp in kps:
            if kp.endswith(".0") and len(kp) > 2:      # 1.0 → 第1章全部知识点
                ors.append("(kp_id = ? OR kp_id LIKE ?)")
                kp_args += [kp, kp[:-1] + "%"]
            else:
                ors.append("kp_id = ?")
                kp_args.append(kp)
        kp_sql = "(" + " OR ".join(ors) + ")"
        if sql:
            sql = "(" + sql + (" OR " if mode == "any" else " AND ") + kp_sql + ")"
        else:
            sql = kp_sql
        args += kp_args
    return " AND " + sql + subject_sql, args + subject_args


def _load_quality(conn):
    """读题库体检表 → ({no: set(flags)}, 表在不在)。表不存在（旧库/没跑体检）时优雅降级。"""
    try:
        rows = conn.execute("SELECT no, flags FROM quality_flags").fetchall()
    except Exception:  # noqa: BLE001 - 没这张表就当没体检过
        return {}, False
    flags = {}
    for row in rows:
        fs = {f for f in str(row[1] or "").split(",") if f}
        if fs:
            flags[str(row[0])] = fs
    return flags, True


def _quality_block(no: str, flags: set, p: dict, text_plain: str, fmt: str):
    """这道题会被哪一类挡掉 → EXCLUDE_KEYS 里的 key，或 None。

    顺序即优先级（先命中先记账，这样 stats 各类相加 = 被挡总数）：print_blocking → mismatch
    → strict → fig_missing。体检表里有 fig_missing flag 就优先用它（那份判定还核对了源 docx）。
    """
    req = p["requirements"]
    if flags & set(PRINT_BLOCKING_FLAGS):
        return "print_blocking"
    if req["exclude_mismatch"] and (flags & set(MISMATCH_FLAGS)):
        return "mismatch"
    if req["strict"] and (flags & set(STRICT_FLAGS)):
        return "strict"
    if req["exclude_fig_missing"]:
        if "fig_missing" in flags:
            return "fig_missing"
        if fig_missing(text_plain, has_figure(fmt)):
            return "fig_missing"
    return None


def _candidates(conn, qtype: str, p: dict, quality: dict = None, stats: dict = None):
    """某题型的候选池（已按 scope / requirements / 题库体检过滤，并算好 answer_key）。

    ``stats`` 是这一轮的计数桶（EXCLUDE_KEYS），被哪一类挡掉就记在哪个桶里。
    """
    sql = ("SELECT no, chapter, kp_id, family_key, answer_raw, option_count, text_plain, fmt "
           "FROM questions WHERE qtype=? AND is_empty=0")
    args = [qtype]
    if p["requirements"]["exclude_no_family"]:
        sql += " AND family_key IS NOT NULL AND family_key<>''"
    clause, clause_args = _scope_clause(p["scope"])
    sql += clause + " ORDER BY no"
    args += clause_args

    need_answer = p["requirements"]["need_answer"]
    excluded = set(p.get("exclude_nos") or [])
    out = []
    for row in conn.execute(sql, args):
        no = row[0]
        chapter, kp_id, family = row[1], row[2], row[3]
        answer_raw, option_count = row[4], row[5]
        text_plain, fmt = row[6] or "", row[7] or ""
        if excluded and no in excluded:      # exclude_nos：拉黑名单（原错题不要重复出现）
            continue
        reason = _quality_block(no, (quality or {}).get(no, set()), p, text_plain, fmt)
        if reason:
            if stats is not None:
                stats[reason] = stats.get(reason, 0) + 1
            continue
        key = parse_answer_key(answer_raw, qtype)
        if need_answer and not key:
            continue
        out.append({
            "no": no, "qtype": qtype, "chapter": chapter, "kp_id": kp_id,
            "family_key": family, "answer_raw": answer_raw,
            "option_count": option_count, "answer_key": key,
            "practice_only": not key,
        })
    return out


def _rows_by_nos(conn, nos: list) -> dict:
    """按题号批量取行（nos 精确题集用）：

    {no: (no, qtype, chapter, kp_id, family, answer_raw, option_count, is_empty,
          text_plain, fmt, subject_pk)}
    """
    out = {}
    chunk = 400
    for i in range(0, len(nos), chunk):
        part = list(nos[i:i + chunk])
        sql = ("SELECT no, qtype, chapter, kp_id, family_key, answer_raw, option_count, "
               "is_empty, text_plain, fmt, subject_pk FROM questions WHERE no IN (%s)"
               % ",".join("?" * len(part)))
        for row in conn.execute(sql, part):
            out[str(row[0])] = row
    return out


def _picked_from_nos(conn, p: dict, quality: dict = None):
    """按 params.nos 出精确卷：顺序照给、不随机；题号/题型/学科/答案/体检对不上一律报错。"""
    nos = list(p["nos"])
    excluded = set(p.get("exclude_nos") or [])
    clash = [no for no in nos if no in excluded]
    if clash:
        raise SelectError("nos 里的题号同时被 exclude_nos 拉黑：%s" % "、".join(clash[:10]))

    rows = _rows_by_nos(conn, nos)
    missing = [no for no in nos if no not in rows]
    if missing:
        raise SelectError("nos 里的题号在题库里不存在：%s" % "、".join(missing[:10]))

    subjects = scope_subjects(p["scope"])
    if subjects:
        outside = [no for no in nos
                   if str(rows[no][10] or "") and str(rows[no][10]) not in subjects]
        if outside:
            raise SelectError("nos 里的题号不属于所选学科（%s）：%s"
                              % ("、".join(subjects), "、".join(outside[:10])))

    quality = {} if quality is None else quality
    need_answer = p["requirements"]["need_answer"]
    picked = []
    for no in nos:
        row = rows[no]
        qtype = str(row[1])
        if int(row[7] or 0):
            raise SelectError("nos 里的 %s 是空题（is_empty=1），没有题干，无法印卷" % no)
        reason = _quality_block(no, quality.get(no, set()), p, row[8] or "", row[9] or "")
        if reason:
            raise SelectError(
                "nos 里的 %s 被题库体检挡掉（%s：%s），不能印到卷面上；"
                "请先用 quality.filter_printable 过滤，或放宽 requirements 对应开关"
                % (no, ",".join(sorted(quality.get(no, set()) & (
                    set(PRINT_BLOCKING_FLAGS) | set(MISMATCH_FLAGS) | set(STRICT_FLAGS)
                    | {"fig_missing"}))) or reason, reason))
        key = parse_answer_key(row[5], qtype)
        if need_answer and not key:
            raise SelectError("nos 里的 %s 没有可用答案，与 requirements.need_answer=true 冲突"
                              % no)
        picked.append({
            "no": no, "qtype": qtype, "chapter": row[2], "kp_id": row[3],
            "family_key": row[4], "answer_raw": row[5],
            "option_count": row[6], "answer_key": key, "practice_only": not key,
        })

    counts = {}
    for c in picked:
        counts[c["qtype"]] = counts.get(c["qtype"], 0) + 1
    mix = {q: n for q, n in (p["mix"] or {}).items() if n}
    if mix:
        if mix != counts:
            raise SelectError("mix 与 nos 的题型数量不一致：mix=%r，nos 实际=%r"
                              % (mix, counts))
    else:
        p["mix"] = {q: counts[q] for q in QTYPES if q in counts}
    pools = {q: 0 for q in (p["mix"] or {})}
    for c in picked:
        pools[c["qtype"]] = pools.get(c["qtype"], 0) + 1
    return picked, pools


def _picked_random(conn, p: dict, quality: dict = None):
    """按 mix 随机抽（可复现）：先按题型取候选池、校验题量，再抽样并排序。"""
    pools, shortfalls, stats = {}, {}, {}
    quality = {} if quality is None else quality
    for qt, need in p["mix"].items():
        if need <= 0:
            continue
        qt_stats = {}
        cands = _candidates(conn, qt, p, quality, qt_stats)
        stats[qt] = qt_stats
        pools[qt] = cands
        if len(cands) < need:
            shortfalls[qt] = {"need": need, "available": len(cands), "short": need - len(cands)}
    if shortfalls:
        relax = {}
        for qt in shortfalls:
            qt_stats = stats.get(qt) or {}
            extra = {}
            if p["requirements"]["exclude_fig_missing"] and qt_stats.get("fig_missing"):
                extra["exclude_fig_missing"] = qt_stats["fig_missing"]
            if p["requirements"]["exclude_mismatch"] and qt_stats.get("mismatch"):
                extra["exclude_mismatch"] = qt_stats["mismatch"]
            if p["requirements"]["strict"] and qt_stats.get("strict"):
                extra["strict"] = qt_stats["strict"]
            if not extra and qt_stats.get("print_blocking"):
                extra["print_blocking"] = qt_stats["print_blocking"]
            if extra:
                relax[qt] = extra
        tips = []
        for qt in sorted(relax, key=lambda q: _QT_ORDER[q]):
            if "exclude_fig_missing" in relax[qt]:
                tips.append("%s放宽『排除缺图题』可多出 %d 题"
                            % (qt, relax[qt]["exclude_fig_missing"]))
            if "exclude_mismatch" in relax[qt]:
                tips.append("%s放宽『排除答案不符』可多出 %d 题"
                            % (qt, relax[qt]["exclude_mismatch"]))
            if "strict" in relax[qt]:
                tips.append("%s关掉 strict 可多出 %d 题" % (qt, relax[qt]["strict"]))
        hint = "已按 scope / requirements / 题库体检过滤，不做凑数"
        if tips:
            hint += "；" + "；".join(tips)
        exc = InsufficientQuestions(
            {qt: shortfalls[qt] for qt in sorted(shortfalls, key=lambda q: _QT_ORDER[q])},
            hint=hint)
        exc.relax = relax
        exc.excluded = {k: sum((s or {}).get(k, 0) for s in stats.values())
                        for k in EXCLUDE_KEYS}
        raise exc
    picked = []
    for qt in QTYPES:                       # 卷面分组顺序
        need = p["mix"].get(qt, 0)
        if need <= 0:
            continue
        cands = pools.get(qt) or []
        # 每种题型一条独立随机流：同 seed 可复现，且改一种题型不影响别的题型
        rng = random.Random(p["seed"])
        chosen = rng.sample(cands, need)
        chosen.sort(key=_order_key)
        picked.extend(chosen)
    totals = {k: sum((s or {}).get(k, 0) for s in stats.values()) for k in EXCLUDE_KEYS}
    return picked, pools, totals


def _count_other_subject(conn, p: dict) -> int:
    """其他学科里「本来可出」的题量（只按题型/有答案算，不含体检与 scope 的章节限制）。

    用于组卷页提示「已排除非本学科 N 道」，让用户知道跨科题被挡掉了。
    """
    subs = scope_subjects(p["scope"])
    if not subs:
        return 0
    marks = ",".join("?" * len(subs))
    cond = " AND NOT (COALESCE(subject_pk, '%s') IN (%s))" % (DEFAULT_SUBJECT, marks)
    extra = " AND answer_raw<>''" if p["requirements"]["need_answer"] else ""
    total = 0
    for qt in QTYPES:
        try:
            total += int(conn.execute(
                "SELECT COUNT(*) FROM questions WHERE qtype=? AND is_empty=0"
                + extra + cond, [qt] + list(subs)).fetchone()[0])
        except Exception:
            return 0
    return total


def pool_report(conn, params: dict) -> dict:
    """各题型可选题量 + 被题库体检挡掉的逐类计数。

    server 组卷表单用它显示「还能出多少题」和「已跳过 N 道不适合打印的题」；
    与 build 用的是同一套过滤（scope / requirements / quality_flags / exclude_nos）。
    """
    p = normalize_params(params)
    _validate_scope(conn, p["scope"])
    quality, has_table = _load_quality(conn)
    stats = {k: 0 for k in EXCLUDE_KEYS}
    available = {}
    for qt in QTYPES:
        qt_stats = {}
        available[qt] = len(_candidates(conn, qt, p, quality, qt_stats))
        for k in EXCLUDE_KEYS:
            stats[k] += qt_stats.get(k, 0)
    try:
        other = _count_other_subject(conn, p)
    except Exception:
        other = 0
    return {"available": available, "excluded": stats, "quality_table": has_table,
            "subject": scope_subjects(p["scope"]),
            "excluded_other_subject": other,
            "subject_default": bool(p["scope"].get("subject_default"))}


def _fig_missing_rows(conn, subjects=None) -> list:
    """「缺图」题（体检 flag fig_missing 或题干说如图却没图）的属性行，按库缓存。"""
    subs = tuple(sorted(str(x) for x in (subjects or []) if str(x).strip()))
    key = ("figrows", _db_tag(conn), subs)
    hit = _FIG_CACHE.get(key)
    if hit is not None:
        return hit
    refs = ("如图", "见图", "图中", "下图", "上图", "图①", "图②", "如下的图")
    sql = ("SELECT q.no, q.qtype, q.subject_pk, q.chapter, q.kp_id FROM questions q"
           " WHERE q.is_empty=0 AND ("
           "  EXISTS (SELECT 1 FROM quality_flags qg WHERE qg.no=q.no"
           "          AND qg.flags LIKE '%fig_missing%')"
           "  OR ((" + " OR ".join(["q.text_plain LIKE ?" for _ in refs]) + ")"
           "      AND q.fmt NOT LIKE '%embed%' AND q.fmt NOT LIKE '%ole%'"
           "      AND q.fmt NOT LIKE '%table%'))")
    args = ["%%%s%%" % r for r in refs]
    if subs:
        sql += " AND q.subject_pk IN (%s)" % ",".join("?" * len(subs))
        args += list(subs)
    try:
        rows = [tuple(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:
        rows = []
    _FIG_CACHE[key] = rows
    return rows


def _bad_choice_nos(conn, subjects=None) -> set:
    """选择题里答案不是选项字母的题号（need_answer 时 _candidates 会跳过）。"""
    subs = tuple(sorted(str(x) for x in (subjects or []) if str(x).strip()))
    key = ("choice", _db_tag(conn), subs)
    hit = _FIG_CACHE.get(key)
    if hit is not None:
        return hit
    sql = ("SELECT no, answer_raw FROM questions WHERE qtype='选择' AND is_empty=0"
           " AND answer_raw<>''")
    args = []
    if subs:
        sql += " AND subject_pk IN (%s)" % ",".join("?" * len(subs))
        args += list(subs)
    try:
        nos = {str(no) for no, ans in conn.execute(sql, args)
               if not parse_answer_key(ans, "选择")}
    except sqlite3.OperationalError:
        nos = set()
    _FIG_CACHE[key] = nos
    return nos


def _scope_hit(scope: dict, subject_pk: str, chapter: str, kp_id: str) -> bool:
    """Python 版 scope 判定（与 _scope_clause 等价），给减法用。"""
    subs = scope_subjects(scope)
    if subs and subject_pk not in subs:
        return False
    chapters = scope.get("chapters") or []
    kps = scope.get("kps") or []
    mode = scope.get("kp_mode") or "any"
    chapter_ok = (not chapters) or (chapter in chapters)
    kp_id = kp_id or ""
    if kps:
        def _kp_hit(k):
            if k.endswith(".0") and len(k) > 2:      # 1.0 = 整章知识点
                return kp_id == k or kp_id.startswith(k[:-1])
            return kp_id == k
        kp_ok = any(_kp_hit(k) for k in kps)
    else:
        kp_ok = True
    if chapters and kps:
        return (chapter_ok or kp_ok) if mode != "all" else (chapter_ok and kp_ok)
    return chapter_ok and kp_ok


def _count_nos_in_scope(conn, p: dict, nos: list) -> dict:
    """给定题号里落在本范围（且满足基础过滤）的题量，按题型。"""
    if not nos:
        return {}
    clause, args = _scope_clause(p["scope"], null_safe=False)
    where = ["is_empty = 0"]
    if p["requirements"]["need_answer"]:
        where.append("answer_raw<>''")
    if p["requirements"]["exclude_no_family"]:
        where.append("family_key IS NOT NULL AND family_key<>''")
    if clause:
        where.append(clause.replace(" AND ", "", 1))
    out = {}
    for i in range(0, len(nos), 200):
        chunk = nos[i:i + 200]
        sql = ("SELECT qtype, COUNT(*) FROM questions WHERE no IN (%s) AND %s GROUP BY qtype"
               % (",".join("?" * len(chunk)), " AND ".join(where)))
        for row in conn.execute(sql, chunk + list(args)):
            out[row[0]] = out.get(row[0], 0) + int(row[1] or 0)
    return out


def _db_tag(conn) -> str:
    """缓存键：库文件 + 行数 + 体检行数 + 本连接写入次数（导入/改答案后自动失效）。"""
    try:
        rows = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    except sqlite3.OperationalError:
        return ""
    try:
        flags = conn.execute("SELECT COUNT(*) FROM quality_flags").fetchone()[0]
    except sqlite3.OperationalError:
        flags = 0
    try:
        changes = int(getattr(conn, "total_changes", 0) or 0)
    except Exception:  # noqa: BLE001
        changes = 0
    try:
        name = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except sqlite3.OperationalError:
        name = ""
    return "%s|%s|%s|%s" % (name, rows, flags, changes)


def clear_cache() -> None:
    """清掉 picker 侧的计数缓存（导入/重跑体检后由 server 调用）。"""
    _FIG_CACHE.clear()
    _EXCL_CACHE.clear()


def _excluded_rows(conn, req: dict, subjects=None) -> list:
    """被体检/缺图挡掉的题的属性行（qtype, subject, chapter, kp, has_answer, has_family）。

    冷算一次（质量 flags + 缺图文本扫描 + 一次 join，约 100ms），之后按 scope 在内存里减。
    """
    subs = tuple(sorted(str(x) for x in (subjects or []) if str(x).strip()))
    key = ("excl", _db_tag(conn), subs,
           (tuple(MISMATCH_FLAGS) if req["exclude_mismatch"] else ()),
           bool(req["strict"]), bool(req["exclude_fig_missing"]))
    hit = _EXCL_CACHE.get(key)
    if hit is not None:
        return hit
    flags = list(PRINT_BLOCKING_FLAGS)
    if req["exclude_mismatch"]:
        flags += list(MISMATCH_FLAGS)
    if req["strict"]:
        flags += list(STRICT_FLAGS)
    nos = set()
    if flags:
        like = " OR ".join(["flags LIKE ?" for _ in flags])
        try:
            nos |= {str(r[0]) for r in conn.execute(
                "SELECT no FROM quality_flags WHERE " + like, ["%%%s%%" % f for f in flags])}
        except sqlite3.OperationalError:
            pass
    if req["exclude_fig_missing"]:
        nos |= {str(r[0]) for r in _fig_missing_rows(conn, subs)}
    rows = []
    if nos:
        # 用 CTE VALUES 而不是临时表：临时表写入会改 conn.total_changes，缓存键就永远命中不了
        vals = ",".join(["(?)"] * len(nos))
        sql = ("WITH excl(no) AS (VALUES " + vals + ")"
               " SELECT q.qtype, q.subject_pk, q.chapter, q.kp_id, q.answer_raw<>'',"
               " (q.family_key IS NOT NULL AND q.family_key<>'')"
               " FROM questions q WHERE q.no IN (SELECT no FROM excl) AND q.is_empty=0")
        rows = [(r[0], r[1], r[2], r[3], bool(r[4]), bool(r[5]))
                for r in conn.execute(sql, list(nos))]
    _EXCL_CACHE[key] = rows
    return rows


def _excluded_in_scope(conn, p: dict) -> dict:
    """排除题里落在本范围、且本会入选的数量（按题型）。"""
    req = p["requirements"]
    out = {}
    for qtype, subject_pk, chapter, kp_id, has_answer, has_family in _excluded_rows(
            conn, req, scope_subjects(p["scope"])):
        if req["need_answer"] and not has_answer:
            continue
        if req["exclude_no_family"] and not has_family:
            continue
        if not _scope_hit(p["scope"], subject_pk, chapter, kp_id):
            continue
        out[qtype] = out.get(qtype, 0) + 1
    return out


def _excluded_nos(conn, req: dict, subjects=None) -> set:
    """被体检/缺图挡掉的题号（按库+口径缓存）。"""
    subs = tuple(sorted(str(x) for x in (subjects or []) if str(x).strip()))
    key = ("excl", _db_tag(conn), subs,
           (tuple(MISMATCH_FLAGS) if req["exclude_mismatch"] else ()),
           bool(req["strict"]), bool(req["exclude_fig_missing"]))
    hit = _EXCL_CACHE.get(key)
    if hit is not None:
        return hit
    flags = list(PRINT_BLOCKING_FLAGS)
    if req["exclude_mismatch"]:
        flags += list(MISMATCH_FLAGS)
    if req["strict"]:
        flags += list(STRICT_FLAGS)
    nos = set()
    if flags:
        like = " OR ".join(["flags LIKE ?" for _ in flags])
        try:
            nos |= {str(r[0]) for r in conn.execute(
                "SELECT no FROM quality_flags WHERE " + like, ["%%%s%%" % f for f in flags])}
        except sqlite3.OperationalError:
            pass
    if req["exclude_fig_missing"]:
        nos |= {str(r[0]) for r in _fig_missing_rows(conn, subs)}
    _EXCL_CACHE[key] = nos
    return nos


def _excluded_index(conn, req: dict, where: list, args: list, nos) -> list:
    """排除题按 (qtype, subject, chapter, kp) 聚合成条数并缓存。

    只算一次（约 70ms），之后每个范围在内存里按 scope 求和（~1ms）。
    """
    # 注意：key 里不放 scope（where/args），只按 库+口径 缓存 —— 换成别的单元也命中
    key = ("exclidx", _db_tag(conn), len(nos), hash(frozenset(nos)))
    hit = _EXCL_CACHE.get(key)
    if hit is not None:
        return hit
    if not nos:
        _EXCL_CACHE[key] = []
        return []
    vals = ",".join(["(?)"] * len(nos))
    sql = ("WITH excl(no) AS (VALUES " + vals + ")"
           " SELECT q.qtype, q.subject_pk, q.chapter, q.kp_id,"
           " q.answer_raw<>'', (q.family_key IS NOT NULL AND q.family_key<>''), COUNT(*)"
           " FROM questions q WHERE q.no IN (SELECT no FROM excl) AND q.is_empty=0"
           " GROUP BY 1,2,3,4,5,6")
    rows = [(r[0], r[1], r[2], r[3], bool(r[4]), bool(r[5]), int(r[6]))
            for r in conn.execute(sql, list(nos))]
    _EXCL_CACHE[key] = rows
    return rows


def _count_these(conn, where: list, args: list, nos) -> dict:
    """在给定题号集合里、再按 base 过滤条件计数（按题型）。"""
    if not nos:
        return {}
    vals = ",".join(["(?)"] * len(nos))
    sql = ("WITH excl(no) AS (VALUES " + vals + ")"
           " SELECT qtype, COUNT(*) FROM questions"
           " WHERE no IN (SELECT no FROM excl) AND " + " AND ".join(where)
           + " GROUP BY qtype")
    return {row[0]: int(row[1] or 0) for row in conn.execute(sql, list(nos) + list(args))}


def _ensure_count_index(conn) -> None:
    """确保「剩余题量」用的覆盖索引存在（真库 87k 行下 113ms → 28ms）。

    只读连接/无权限时静默跳过（不影响正确性，只是慢一点）。
    """
    key = ("idx", _db_tag(conn))
    if key in _EXCL_CACHE:
        return
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_q_count ON questions("
                     "subject_pk, is_empty, qtype, answer_raw, fmt, text_plain)")
        conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        pass
    _EXCL_CACHE[key] = True


def _aggregate_counts(conn, subs, req: dict) -> list:
    """把题库按 (subject, chapter, kp, qtype, 有答案, 有题族) 预聚合一次并缓存。

    t85：换 scope 首次也要快 —— 真库 27,774 行扫一遍约 150ms，之后任何学科/单元/知识点
    组合都只是在内存里按 scope 求和（~1ms）。
    """
    subs = tuple(sorted(str(x) for x in (subs or []) if str(x).strip()))
    key = ("agg", _db_tag(conn), subs,
           bool(req.get("need_answer")), bool(req.get("exclude_no_family")))
    hit = _EXCL_CACHE.get(key)
    if hit is not None:
        return hit
    sql = ("SELECT subject_pk, COALESCE(chapter,''), COALESCE(kp_id,''), qtype,"
           " (answer_raw<>''), (family_key IS NOT NULL AND family_key<>''), COUNT(*)"
           " FROM questions WHERE is_empty=0")
    args = []
    if subs:
        sql += " AND subject_pk IN (%s)" % ",".join("?" * len(subs))
        args += list(subs)
    sql += " GROUP BY 1,2,3,4,5,6"
    try:
        rows = [(r[0], r[1], r[2], r[3], bool(r[4]), bool(r[5]), int(r[6]))
                for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:
        rows = []
    _EXCL_CACHE[key] = rows
    return rows


def _count_sql(conn, p: dict) -> dict:
    """纯 SQL 计数快路径：范围总数（覆盖索引）+ 几处小减法（按题号集合精确减）。"""
    req = p["requirements"]
    subs = scope_subjects(p["scope"])
    if DEFAULT_SUBJECT in subs:
        # 老库可能有 subject_pk IS NULL 的行（等价 math7a）；`OR ... IS NULL` 会让索引失效，
        # 这种库直接回退慢路径，保证结果一致。
        try:
            if conn.execute("SELECT 1 FROM questions WHERE subject_pk IS NULL LIMIT 1").fetchone():
                return None
        except sqlite3.OperationalError:
            return None
    where = ["is_empty = 0"]
    args = []
    clause, clause_args = _scope_clause(p["scope"], null_safe=False)
    if clause:
        where.append(clause.replace(" AND ", "", 1))
        args += clause_args
    if req["need_answer"]:
        where.append("answer_raw <> ''")
    if req["exclude_no_family"]:
        where.append("family_key IS NOT NULL AND family_key<>''")
    out = {qt: 0 for qt in QTYPES}
    try:
        for (_sub, chapter, kp_id, qtype, has_answer, has_family,
             n) in _aggregate_counts(conn, subs, req):
            if qtype not in out:
                continue
            if req["need_answer"] and not has_answer:
                continue
            if req["exclude_no_family"] and not has_family:
                continue
            if _scope_hit(p["scope"], _sub, chapter, kp_id):
                out[qtype] += n
        drops = set(_excluded_nos(conn, req, subs))
        if req["need_answer"]:
            drops |= _bad_choice_nos(conn, subs)
        drops |= {str(x) for x in (p.get("exclude_nos") or []) if str(x).strip()}
        for (qtype, subject_pk, chapter, kp_id, has_answer, has_family,
             n) in _excluded_index(conn, req, where, args, drops):
            if req["need_answer"] and not has_answer:
                continue
            if req["exclude_no_family"] and not has_family:
                continue
            if qtype in out and _scope_hit(p["scope"], subject_pk, chapter, kp_id):
                out[qtype] -= n
        for qt in out:
            out[qt] = max(0, out[qt])
    except sqlite3.OperationalError:
        return None
    return out


def count_available(conn, params: dict) -> dict:
    """各题型当前可选题量（按 scope / requirements / 题库体检过滤后）。

    t73：走纯 SQL 计数快路径（真库 87k 行 <100ms）；``count_available_slow`` 保留逐行版
    用于一致性测试（两者结果必须逐组相等）。
    """
    p = normalize_params(params)
    _validate_scope(conn, p["scope"])
    try:
        fast = _count_sql(conn, p)
    except sqlite3.OperationalError:
        fast = None
    if fast is None:                     # 老库/索引缺失 → 逐行参考实现
        return count_available_slow(conn, params)
    return fast


def count_available_slow(conn, params: dict) -> dict:
    """逐行实现的参考版（与选题完全同一套代码路径），只用于对拍与降级。"""
    return pool_report(conn, params)["available"]


# ---------------------------------------------------------------- 组卷
def _order_key(c: dict):
    """卷面顺序：章 → 知识点 → 题号（同一题型内按知识点聚拢，卷子好读）。"""
    kp_parts = []
    for piece in str(c.get("kp_id") or "").split("."):
        try:
            kp_parts.append(int(piece))
        except ValueError:
            kp_parts.append(99)
    return (_CHAPTER_ORDER.get(c.get("chapter") or "", 99), tuple(kp_parts), c.get("no") or "")


def options_for(qtype: str, option_count) -> list:
    """选择题的选项字母；其他题型返回 []（card.py 对判断题自己补 √/×）。"""
    if qtype != "选择":
        return []
    try:
        n = int(option_count or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 2:
        return list(DEFAULT_OPTIONS)
    return [LETTERS[i] for i in range(min(n, len(LETTERS)))]


def _fetch_tokens(conn, nos: list) -> dict:
    out = {}
    if not nos:
        return out
    sql = ("SELECT no, tokens_json FROM questions WHERE no IN (%s)"
           % ",".join("?" * len(nos)))
    for row in conn.execute(sql, list(nos)):
        try:
            out[row[0]] = json.loads(row[1] or "[]")
        except (TypeError, ValueError):
            out[row[0]] = []
    return out


def _iso_now(now=None) -> str:
    if now is None:
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if isinstance(now, datetime.datetime):
        return (now if now.tzinfo else now.astimezone()).isoformat(timespec="seconds")
    return str(now).strip()


def _resolve_pid(conn, created_at: str, pid=None) -> str:
    """P{YYYYMMDD}-{NNN}：当天已有卷号 +1。"""
    if pid:
        return str(pid)
    day = (created_at or "")[:10].replace("-", "")
    if len(day) != 8 or not day.isdigit():
        day = time.strftime("%Y%m%d")
    prefix = "P%s-" % day
    seq = 1
    try:
        rows = conn.execute("SELECT pid FROM papers WHERE pid LIKE ?",
                            (prefix + "%",)).fetchall()
    except Exception:  # noqa: BLE001 - 裸连接可能没有 papers 表
        rows = []
    for r in rows:
        try:
            seq = max(seq, int(str(r[0]).rsplit("-", 1)[1]) + 1)
        except (ValueError, IndexError, TypeError):
            pass
    return "%s%03d" % (prefix, seq)


def build(params: dict, conn, *, now=None, pid=None) -> dict:
    """按组卷参数抽题 → manifest（CONTRACT §3，含 items，不含 card）。

    * 给了 `nos` → 精确题集：按给定顺序出卷、不随机抽（训练卷用）；
    * 否则按 `mix` 随机抽（同 seed 可复现）；`exclude_nos` 在两种路径下都会拉黑。

    :param params: 见 CONTRACT §2
    :param conn: sqlite3 连接（只读使用；可以是 db.connect 的 Row 连接，也可以是裸连接）
    :param now: 测试用：固定 created_at（datetime 或 ISO 字符串）
    :param pid: 测试用：固定卷号；缺省按当天已有卷号自增
    :raises ScopeError: scope 里的章节/知识点库里没有
    :raises InsufficientQuestions: 某题型可用题量不足（details 里逐题型给 need/available/short）
    :raises SelectError: nos 里的题号不存在 / 题型与 mix 对不上 / 没有可用答案
    """
    p = normalize_params(params)
    _validate_scope(conn, p["scope"])
    created_at = _iso_now(now)
    quality, quality_table = _load_quality(conn)

    if p["nos"]:
        picked, pools = _picked_from_nos(conn, p, quality)   # 顺序照给，不再排序
        excluded = {k: 0 for k in EXCLUDE_KEYS}
    else:
        picked, pools, excluded = _picked_random(conn, p, quality)

    tokens = _fetch_tokens(conn, [c["no"] for c in picked])
    items = []
    for i, c in enumerate(picked, 1):
        items.append({
            "idx": i,
            "no": c["no"],
            "qtype": c["qtype"],
            "kp_id": c["kp_id"],
            "family_key": c["family_key"],
            "stem_tokens": tokens.get(c["no"], []),
            "answer_key": c["answer_key"],
            "options": options_for(c["qtype"], c["option_count"]),
            "space_paras": p["space"].get(c["qtype"], 0),
            "practice_only": c["practice_only"],
            "chapter": c["chapter"],
        })

    by_qtype = {}
    for it in items:
        by_qtype[it["qtype"]] = by_qtype.get(it["qtype"], 0) + 1
    if not p["nos"]:
        pass                             # 随机卷的 excluded 由 _picked_random 一并返回
    manifest = {
        "pid": _resolve_pid(conn, created_at, pid),
        "created_at": created_at,
        "title": p["title"],
        "params": p,
        "page": dict(PAGE),
        "items": items,
        "stats": {
            "n_items": len(items),
            "by_qtype": by_qtype,
            "pool_available": {qt: (v if isinstance(v, int) else len(v))
                               for qt, v in pools.items()},
            "n_practice_only": sum(1 for it in items if it["practice_only"]),
            "excluded": excluded,
            "quality_table": quality_table,
            "seed": p["seed"],
        },
    }
    return manifest


def save(conn, manifest: dict, docx_path: str = "", pdf_path: str = "") -> str:
    """把 manifest 落 papers 表（同 pid 覆盖）。返回 pid。"""
    pid = str(manifest.get("pid") or "").strip()
    if not pid:
        raise SelectError("manifest 缺 pid，无法落库")
    items = manifest.get("items") or []
    conn.execute(
        "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest,"
        " docx_path, pdf_path, n_items) VALUES(?,?,?,?,?,?,?,?)",
        (pid,
         str(manifest.get("created_at") or ""),
         str(manifest.get("title") or ""),
         json.dumps(manifest.get("params") or {}, ensure_ascii=False),
         json.dumps(manifest, ensure_ascii=False),
         str(docx_path or ""),
         str(pdf_path or ""),
         len(items)))
    conn.commit()
    return pid


# ---------------------------------------------------------------- 自检 CLI
def _main(argv=None):
    import os
    import sqlite3
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    here = os.path.dirname(os.path.abspath(__file__))
    db_path = argv[0] if argv else os.path.join(here, "data", "app.db")
    # 只读打开：build() 不写库，别给 app.db 留 -wal/-shm
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    params = {"title": "自检卷", "mix": {"选择": 10, "填空": 8, "解答": 6},
              "scope": {"chapters": [], "kps": []}, "seed": 42,
              "space": dict(DEFAULT_SPACE)}          # 卷面不留书写区（作答在答题卡）
    print(json.dumps(count_available(conn, params), ensure_ascii=False))
    m = build(params, conn)
    brief = {k: v for k, v in m.items() if k != "items"}
    brief["items"] = [{k: it[k] for k in ("idx", "no", "qtype", "kp_id", "practice_only")}
                      | {"n_blank": len(it["answer_key"]), "n_tok": len(it["stem_tokens"])}
                      for it in m["items"]]
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    _main()
