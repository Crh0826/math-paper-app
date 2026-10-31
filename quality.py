# -*- coding: utf-8 -*-
"""t16 · 题库质量体检：把「能不能印、能不能判」变成可查询的数据。

产出的 `quality_flags(no, flags, detail)` 表供组卷 / 验收 / UI 直接查；这里的函数只读
`questions`，不改 picker / paper / grade 的行为（他们想用就调 `is_printable`）。

八类 flag（每条都留证据片段在 detail 里）：

| flag | 判定 | 为什么要管 |
|---|---|---|
| `no_stem` | text_plain 去占位后为空 **且** embed=0 | 印出来是个空白框 |
| `stem_only_placeholder` | text_plain 只剩 ⟨式⟩/⟨图⟩/⟨表⟩ | 题干全靠公式对象，纯 OCR 认不出 |
| `analysis_left` | 题干含【分析】【详解】【解答】【点评】【点睛】【答案】或行首「分析：」 | 教师版解析混进卷面 = 泄题 |
| `short_stem` | 去占位后 < 8 字符 | 「计算：」这种没有可做题干的残句 |
| `answer_mismatch` | 选择答案不是选项字母 / 填空答案 > 60 字 / 解答答案只有单字母 | 判卷必错 |
| `no_answer` | answer_raw 为空 | 组卷 need_answer 已排除，但训练/统计要知道 |
| `heavy_layout` | embed > 8 或 table > 2 | 排版容易溢出、体检会报错 |
| `dup_text` | 规范化题干（去空白标点、前 200 字）完全相同 | **题干骨架重复，仅提示**：公式不计入（12,280 题里 34 条），默认**不**排除 |

用法：
    import quality
    res = quality.scan(conn)                 # 全库体检 + 写 quality_flags
    ok, rejected = quality.filter_printable(conn, nos, need_answer=True)
    quality.write_report(res, "data/quality_report.json")

    python quality.py --report data/quality_report.json
"""
from __future__ import annotations

import json
import os
import re
import time

__all__ = [
    "scan", "flags_for_no", "load_flags", "load_detail", "is_printable",
    "filter_printable", "write_report", "ALL_FLAGS", "PRINT_BLOCKING",
    "ANSWER_BLOCKING", "LAYOUT_FLAGS", "DEFAULT_REPORT",
]

#: 八类 flag，顺序即写入 flags 串的顺序
ALL_FLAGS = ("no_stem", "stem_only_placeholder", "analysis_left", "short_stem",
             "answer_mismatch", "no_answer", "heavy_layout", "dup_text", "fig_missing")

#: 一印就会出问题：空白框 / 只有占位 / 教师版残留 / 题干说「如图」但根本没有图
PRINT_BLOCKING = ("no_stem", "stem_only_placeholder", "analysis_left", "fig_missing")

#: 能印但判不了分（组卷 need_answer=true 时要排除）
ANSWER_BLOCKING = ("no_answer", "answer_mismatch")

#: 排版/重复类：默认放行，strict=True 时排除
LAYOUT_FLAGS = ("short_stem", "heavy_layout", "dup_text")

SHORT_STEM_MIN = 8          # 去占位后可读内容少于 8 字符算残句
FILL_ANSWER_MAX = 60        # 填空答案超过 60 字基本是把解答抄进来了
HEAVY_EMBED_MAX = 8         # 公式/图片对象超过 8 个，排版风险高
HEAVY_TABLE_MAX = 2         # 表格超过 2 个，排版风险高
DUP_KEEP = 200              # 判重只看前 200 字

DEFAULT_REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "quality_report.json")

_PLACEHOLDER = re.compile(r"[⟨<][式图表][⟩>]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_DIGIT = re.compile(r"[0-9]")
# 去空白 + 中英标点（保留 CJK / 字母 / 数字 / 占位符）
_PUNCT = re.compile(r"[\s\u00a0\u3000-\u303f\uff01-\uff65!-/:-@\[-`{-~]")

_ANALYSIS = re.compile(r"【(?:分析|详解|解答|点评|点睛|答案)】")
_ANALYSIS_LINE = re.compile(r"(?m)^\s*(?:分析|详解|点评|点睛|解答|解析)\s*[：:]")

#: 题干自称有图：「如图 / 见图 / 图中 / 下图 / 上图 / 所示的图 / 图形中」
_FIG_CLAIM = re.compile(r"如图|见图|图中|下图|上图|所示的?图|图形中")
#: 图可能藏在这些标记后面（表格、文本框里的也算）
_IMG_MARKERS = ("w:drawing", "w:pict", "w:object", "v:imagedata", "a:blip")

_ANSWER_PUNCT = re.compile(r"[\s\u3000-\u303f\uff01-\uff65！-～]")


# ---------------------------------------------------------------- 小工具

def _snip(s, n=48):
    """证据片段：压平空白后截断。"""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def _residual(text):
    """去掉占位符与标点空白后剩下的"人读得出"的内容。"""
    return _PUNCT.sub("", _PLACEHOLDER.sub("", text or ""))


def _count_kinds(tokens_json):
    """数 embed / table token 个数（tokens_json 坏掉就按 0 算，不让体检崩）。"""
    embeds = tables = 0
    try:
        toks = json.loads(tokens_json or "[]")
    except (TypeError, ValueError):
        return 0, 0
    if not isinstance(toks, list):
        return 0, 0
    for t in toks:
        if not isinstance(t, dict):
            continue
        kind = t.get("kind")
        if kind == "embed":
            embeds += 1
        elif kind == "table":
            tables += 1
    return embeds, tables


def norm_text(text):
    """判重用的规范化题干：去占位符？不——占位符也是内容，只去空白标点。"""
    return _PUNCT.sub("", text or "")[:DUP_KEEP]


def _answer_flags(qtype, answer):
    """answer_mismatch / no_answer 的判定，返回 (flags, detail)。"""
    ans = (answer or "").strip()
    if not ans:
        return ["no_answer"], {"no_answer": "answer_raw 为空"}
    squeezed = _ANSWER_PUNCT.sub("", ans)
    qt = (qtype or "").strip()
    if qt == "选择" and not re.fullmatch(r"[A-Fa-f]+", squeezed):
        return (["answer_mismatch"],
                {"answer_mismatch": "选择题答案应为选项字母，实际 %r" % _snip(ans, 60)})
    if qt == "填空" and len(re.sub(r"\s", "", ans)) > FILL_ANSWER_MAX:
        return (["answer_mismatch"],
                {"answer_mismatch": "填空题答案 %d 字 > %d，像是把解答抄进来了：%r"
                                    % (len(re.sub(r"\s", "", ans)), FILL_ANSWER_MAX,
                                       _snip(ans, 60))})
    if qt == "解答" and re.fullmatch(r"[A-Fa-f]", squeezed):
        return (["answer_mismatch"],
                {"answer_mismatch": "解答题答案只有单个字母 %r（多半是选择题串行）" % squeezed})
    return [], {}


def _as_dict(row):
    """SELECT 固定 5 列（no, qtype, text_plain, answer_raw, tokens_json）→ dict。

    裸 sqlite3 连接给的是 tuple，db.connect 给的是 Row，这里统一成 dict。
    """
    if isinstance(row, dict):
        return row
    try:
        return {"no": row["no"], "qtype": row["qtype"], "text_plain": row["text_plain"],
                "answer_raw": row["answer_raw"], "tokens_json": row["tokens_json"]}
    except (TypeError, IndexError):
        return {"no": row[0], "qtype": row[1], "text_plain": row[2],
                "answer_raw": row[3], "tokens_json": row[4]}


def _figure_presence(tokens_json):
    """这一题的 docx 里到底有没有图：(严格, 宽松)。

    严格 = 只有顶层 embed 对象才算有图；
    宽松 = 再把 table / embed 的 XML 里 `w:drawing|w:pict|w:object|v:imagedata|a:blip` 也算上
           （图藏在表格单元格里时，顶层 embed 数是 0，严格口径会误杀）。
    判定用**宽松**，严格口径只用来报对比数。
    """
    try:
        toks = json.loads(tokens_json or "[]")
    except (TypeError, ValueError):
        return False, False
    if not isinstance(toks, list):
        return False, False
    strict = False
    lenient = False
    for t in toks:
        if not isinstance(t, dict):
            continue
        kind = t.get("kind")
        if kind == "embed":
            strict = True
            lenient = True
        xml = t.get("xml") or ""
        if xml and any(m in xml for m in _IMG_MARKERS):
            lenient = True
    return strict, lenient


def _evaluate(row, dup_with=None):
    """单题体检：返回 (flags 列表, detail dict, residual)。"""
    row = _as_dict(row)
    text = row["text_plain"] or ""
    qtype = row["qtype"]
    ans = row["answer_raw"] or ""
    embeds, tables = _count_kinds(row["tokens_json"])

    residual = _residual(text)
    flags, detail = [], {}

    if not residual and embeds == 0:
        flags.append("no_stem")
        detail["no_stem"] = "去占位后题干为空且无图片/公式对象（embed=0），原文 %r" % _snip(text)

    if _PLACEHOLDER.search(text) and not residual:
        flags.append("stem_only_placeholder")
        detail["stem_only_placeholder"] = "题干只剩占位符：%r" % _snip(text)

    m = _ANALYSIS.search(text) or _ANALYSIS_LINE.search(text)
    if m:
        flags.append("analysis_left")
        detail["analysis_left"] = "命中教师版标记 %r，片段 %r" % (m.group(0), _snip(text, 80))

    if len(residual) < SHORT_STEM_MIN:
        flags.append("short_stem")
        detail["short_stem"] = "去占位后只有 %d 字：%r" % (len(residual), _snip(residual, 40))

    af, ad = _answer_flags(qtype, ans)
    flags.extend(af)
    detail.update(ad)

    # 题干说「如图」但题里根本没有图 → 印出来学生看不到图，题做不了
    claim = _FIG_CLAIM.search(text)
    if claim:
        strict, lenient = _figure_presence(row["tokens_json"])
        if not lenient:
            flags.append("fig_missing")
            detail["fig_missing"] = ("题干命中 %r（声称有图），但题内无任何图片对象"
                                     "（严格=顶层 embed 0 个，宽松=表格/文本框里也没有）：%r"
                                     % (claim.group(0), _snip(text, 60)))

    if embeds > HEAVY_EMBED_MAX or tables > HEAVY_TABLE_MAX:
        flags.append("heavy_layout")
        detail["heavy_layout"] = "embed=%d（>%d 判重）、table=%d（>%d 判重）" % (
            embeds, HEAVY_EMBED_MAX, tables, HEAVY_TABLE_MAX)

    if dup_with:
        flags.append("dup_text")
        others = [x for x in dup_with if x != row["no"]]
        detail["dup_text"] = "与前 %d 字规范化题干完全相同，同组共 %d 题：%s（片段 %r）" % (
            DUP_KEEP, len(dup_with), "、".join(others[:10]), _snip(norm_text(text), 60))

    return flags, detail, residual


def _pack(flags, detail):
    ordered = [f for f in ALL_FLAGS if f in set(flags)]
    return ",".join(ordered), json.dumps(detail, ensure_ascii=False)


# ---------------------------------------------------------------- 扫描

def scan(conn, limit=None, *, write=True) -> dict:
    """全库（或前 limit 题）体检。

    :param conn: sqlite3 连接（write=False 时可只读打开）
    :param limit: 只体检前 N 题（按 no 排序）
    :param write: 是否写 quality_flags 表（只读连接必须 False）
    :returns: 统计 dict（各 flag 计数、样例、可打印题池、耗时）
    """
    t0 = time.time()
    sql = ("SELECT no, qtype, text_plain, answer_raw, tokens_json"
           " FROM questions ORDER BY no")
    args = []
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()

    # 第一遍：算规范化题干 → 判重组
    norm_of, groups = {}, {}
    for r in rows:
        key = norm_text(r[2])
        norm_of[r[0]] = key
        if key:
            groups.setdefault(key, []).append(r[0])
    dup_groups = {k: v for k, v in groups.items() if len(v) >= 2}

    # 第二遍：逐题定 flag
    counts = {f: 0 for f in ALL_FLAGS}
    samples = {f: [] for f in ALL_FLAGS}
    printable = {}
    printable_gradable = {}
    fig_claims = 0
    fig_missing_strict = 0
    batch = []
    for r in rows:
        d = _as_dict(r)
        text = d["text_plain"] or ""
        if _FIG_CLAIM.search(text):
            fig_claims += 1
            strict, _lenient = _figure_presence(d["tokens_json"])
            if not strict:
                fig_missing_strict += 1
        key = norm_of.get(r[0])
        dup_with = dup_groups.get(key) if key else None
        flags, detail, _residual_text = _evaluate(r, dup_with)
        flags_str, detail_str = _pack(flags, detail)
        if write:
            batch.append((r[0], flags_str, detail_str))
            if len(batch) >= 800:
                conn.executemany("INSERT OR REPLACE INTO quality_flags(no, flags, detail)"
                                 " VALUES(?,?,?)", batch)
                batch = []
        fset = set(flags)
        for f in fset:
            counts[f] += 1
            if len(samples[f]) < 5:
                samples[f].append(r[0])
        if not (fset & set(PRINT_BLOCKING)):
            printable[r[1]] = printable.get(r[1], 0) + 1
            if not (fset & set(ANSWER_BLOCKING)):
                printable_gradable[r[1]] = printable_gradable.get(r[1], 0) + 1

    if write:
        if batch:
            conn.executemany("INSERT OR REPLACE INTO quality_flags(no, flags, detail)"
                             " VALUES(?,?,?)", batch)
        if limit is None:
            # 全量重扫前先清空，避免修复过的题留着旧 flag
            conn.execute("DELETE FROM quality_flags WHERE no NOT IN"
                         " (SELECT no FROM questions)")
        conn.commit()

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scanned": len(rows),
        "elapsed_s": round(time.time() - t0, 2),
        "flags": counts,
        "samples": samples,
        "dup_groups": len(dup_groups),
        "dup_questions": sum(len(v) for v in dup_groups.values()),
        "printable_by_qtype": dict(sorted(printable.items(), key=lambda kv: -kv[1])),
        "printable_gradable_by_qtype": dict(
            sorted(printable_gradable.items(), key=lambda kv: -kv[1])),
        "fig_claims": fig_claims,
        "fig_missing_strict": fig_missing_strict,
        "print_blocking": list(PRINT_BLOCKING),
        "answer_blocking": list(ANSWER_BLOCKING),
        "layout_flags": list(LAYOUT_FLAGS),
        "thresholds": {"short_stem_lt": SHORT_STEM_MIN, "fill_answer_gt": FILL_ANSWER_MAX,
                       "heavy_embed_gt": HEAVY_EMBED_MAX, "heavy_table_gt": HEAVY_TABLE_MAX,
                       "dup_keep_chars": DUP_KEEP},
    }


# ---------------------------------------------------------------- 查询 API

def load_flags(conn) -> dict:
    """no → set(flags)。"""
    out = {}
    for row in conn.execute("SELECT no, flags FROM quality_flags"):
        out[row[0]] = {f for f in str(row[1] or "").split(",") if f}
    return out


def load_detail(conn) -> dict:
    """no → detail dict（证据）。"""
    out = {}
    for row in conn.execute("SELECT no, detail FROM quality_flags"):
        try:
            out[row[0]] = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            out[row[0]] = {}
    return out


def flags_for_no(conn, no):
    """单题**现算** flag（不依赖已扫描的表；不判 dup_text）。

    给 is_printable 兜底，也让测试能直接验一道题而不必全库扫描。
    """
    row = conn.execute(
        "SELECT no, qtype, text_plain, answer_raw, tokens_json FROM questions WHERE no=?",
        (str(no),)).fetchone()
    if row is None:
        return set(), {}
    flags, detail, _res = _evaluate(row)
    return set(flags), detail


def _flags_of(conn, no):
    """优先用体检表；没体检过就现算（dup_text 需要全库，现算时不含）。"""
    try:
        row = conn.execute("SELECT flags FROM quality_flags WHERE no=?", (str(no),)).fetchone()
    except Exception:                     # noqa: BLE001 - 老库没表
        row = None
    if row is not None:
        return {f for f in str(row[0] or "").split(",") if f}
    return flags_for_no(conn, no)[0]


def _blocking(need_answer=False, strict=False):
    blocked = set(PRINT_BLOCKING)
    if need_answer:
        blocked |= set(ANSWER_BLOCKING)
    if strict:
        blocked |= set(LAYOUT_FLAGS)
    return blocked


def is_printable(conn, no, *, need_answer=False, strict=False) -> bool:
    """这道题能不能印（need_answer=True 时连"判不了分"的也排除）。"""
    blocked = _blocking(need_answer, strict)
    return not (_flags_of(conn, no) & blocked)


def filter_printable(conn, nos, *, need_answer=False, strict=False):
    """批量过滤：返回 (可用的题号列表[保序], {被剔除题号: [命中的 flag]})。"""
    blocked = _blocking(need_answer, strict)
    ok, rejected = [], {}
    for no in nos or []:
        hit = _flags_of(conn, no) & blocked
        if hit:
            rejected[str(no)] = sorted(hit)
        else:
            ok.append(no)
    return ok, rejected


# ---------------------------------------------------------------- 报告

def write_report(result: dict, path: str = DEFAULT_REPORT) -> str:
    """把 scan() 结果写成 quality_report.json。"""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(result)
    payload["report_path"] = path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _main(argv=None):
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="题库质量体检")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--db", default=os.path.join(here, "data", "app.db"))
    ap.add_argument("--report", nargs="?", const=DEFAULT_REPORT, default=DEFAULT_REPORT)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-write", action="store_true", help="只统计，不写 quality_flags")
    args = ap.parse_args(argv)

    import sqlite3
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        res = scan(conn, limit=args.limit or None, write=not args.no_write)
    finally:
        conn.close()
    path = write_report(res, args.report)
    brief = {k: v for k, v in res.items() if k not in ("samples",)}
    brief["samples"] = res["samples"]
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    sys.stderr.write("report -> %s\n" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
