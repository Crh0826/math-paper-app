# -*- coding: utf-8 -*-
"""M0-c · 全量建库：22,898 份 docx → app.db（题目元数据 + 富文本 token + 题族 + 知识点 + 答案）。

用法：
  python build_db.py --limit 300 --db data/test.db     # 冒烟
  python build_db.py                                   # 全量（覆盖 data/app.db）
  python build_db.py --fresh                           # 先清空再全量
只读原始 docx；产物只写 app/data 下。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db as dbmod
from docx_rich import M_NS, extract_tokens, plain_text, stats
from families import family_key, parse_label
from taxonomy import classify

import paths as app_paths

#: 题库目录（原始 docx）：环境变量 > config.paths.qbank > 老路径 E:\OCR（存在才用）> <程序>/qbank
ROOT = app_paths.resolve_qbank(legacy=app_paths.LEGACY_QBANK)
HERE = os.path.dirname(os.path.abspath(__file__))
JSONL = os.path.join(ROOT, "dedup", "data", "questions.jsonl")
DEFAULT_DB = os.path.join(HERE, "data", "app.db")
DEFAULT_REPORT = os.path.join(HERE, "data", "build_report.json")
DEFAULT_PRUNE_REPORT = os.path.join(HERE, "data", "prune_report.json")
#: 用户要求：无答案的题不入库（判不了分、训练也没法用）。--keep-unanswered 回到旧行为。
ANSWERED_ONLY = True
#: 全量期望题数：只收有答案 → 12,280；--keep-unanswered → 22,898
FULL_N = 12280
FULL_N_ALL = 22898


def _fmt(tok_stats: dict, raw_xml_flags: dict) -> str:
    parts = []
    if tok_stats["math"]:
        parts.append("omml")
    if tok_stats["embed"]:
        parts.append("ole" if raw_xml_flags.get("ole") else "embed")
    if tok_stats["table"]:
        parts.append("table")
    return "+".join(parts) or "plain"


_Q_COLS = ("no,qtype,option_count,sub_count,is_empty,media_count,text_len,text_plain,"
           "tokens_json,fmt,src_title,src_para,family_key,kp_id,chapter,kp,kp_source,"
           "answer_raw,check_status,subject_pk")
_INSERT_Q = ("INSERT OR REPLACE INTO questions(%s) VALUES (%s)"
             % (_Q_COLS, ",".join("?" * 20)))


def build(limit: int = 0, db_path: str = DEFAULT_DB, fresh: bool = False,
          skip_tokens: bool = False, progress_every: int = 2000,
          keep_unanswered: bool = False) -> dict:
    t0 = time.time()
    if fresh and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    conn = dbmod.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM questions")
    cur.execute("DELETE FROM families")
    cur.execute("DELETE FROM kps")

    rows = []
    with open(JSONL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if limit:
        rows = rows[:limit]

    dropped_no_answer = 0
    if not keep_unanswered:
        kept = [r for r in rows if str(r.get("answer") or "").strip()]
        dropped_no_answer = len(rows) - len(kept)
        rows = kept

    c = Counter()
    fam_members = Counter()
    kp_members = Counter()
    batch = []
    t_tok = 0.0

    for i, r in enumerate(rows, 1):
        no = r["no"]
        path = os.path.join(ROOT, f"{no}.docx")
        if skip_tokens:
            tokens, tok_stats = [], {"text": 0, "math": 0, "embed": 0, "table": 0, "media": 0}
            text = r.get("text") or ""
        else:
            t1 = time.time()
            try:
                tokens = extract_tokens(path)
            except Exception as e:  # noqa: BLE001
                c["token_error"] += 1
                tokens = []
            t_tok += time.time() - t1
            tok_stats = stats(tokens)
            text = plain_text(tokens)

        src_title = os.path.basename(r.get("src_file") or "")
        kp = classify(src_title, text or r.get("text") or "")
        fam = family_key(text or r.get("text") or "", src_title)

        flags = {"ole": False}
        if not skip_tokens:
            for t in tokens:
                if t.kind == "embed" and "Equation.DSMT4" in t.xml:
                    flags["ole"] = True
                    break

        tokens_json = json.dumps([t.to_dict(with_data=False) for t in tokens], ensure_ascii=False) \
            if tokens else "[]"

        batch.append((
            no, r["qtype"], int(r.get("option_count") or 0), int(r.get("sub_count") or 0),
            1 if r.get("is_empty") else 0, int(r.get("media_count") or 0),
            len(text or ""), text or "", tokens_json, _fmt(tok_stats, flags),
            src_title, r.get("src_para"), fam,
            kp["kp_id"], kp["chapter"], kp["kp"], kp["source"],
            (r.get("answer") or "").strip(), (r.get("check_status") or ""),
            dbmod.DEFAULT_SUBJECT,
        ))
        c["questions"] += 1
        c[f"kp_source:{kp['source']}"] += 1
        c[f"qtype:{r['qtype']}"] += 1
        c["with_answer"] += bool((r.get("answer") or "").strip())
        if fam:
            fam_members[fam] += 1
        kp_members[kp["kp_id"]] += 1

        if len(batch) >= 500:
            cur.executemany(
                _INSERT_Q, batch)
            batch = []
        if progress_every and i % progress_every == 0:
            print(f"  ... {i}/{len(rows)}  ({time.time()-t0:.1f}s, tokens {t_tok:.1f}s)", flush=True)

    if batch:
        cur.executemany(
            _INSERT_Q, batch)

    # 题族表（只有 ≥2 题的族才有训练价值，但全存便于查看）
    for k, n in fam_members.items():
        cur.execute("INSERT OR REPLACE INTO families(family_key, src_title, example_no, size, kp_id) "
                    "VALUES(?,?,?,?,?)",
                    (k, k.split("::")[0], k.split("::")[1], n,
                     conn.execute("SELECT kp_id FROM questions WHERE family_key=? LIMIT 1", (k,)).fetchone()[0]))
    # 知识点表
    from taxonomy import all_kps
    for kp in all_kps():
        cur.execute("INSERT OR REPLACE INTO kps(kp_id, chapter, kp, n_questions, subject_pk)"
                    " VALUES(?,?,?,?,?)",
                    (kp["kp_id"], kp["chapter"], kp["kp"], kp_members.get(kp["kp_id"], 0),
                     dbmod.DEFAULT_SUBJECT))

    dbmod.set_meta(conn, "built_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    dbmod.set_meta(conn, "source_jsonl", JSONL)
    dbmod.set_meta(conn, "n_questions", str(c["questions"]))
    dbmod.set_meta(conn, "policy", "all" if keep_unanswered else "answered_only")
    dbmod.set_meta(conn, "dropped_no_answer", str(dropped_no_answer))
    conn.commit()
    conn.close()

    res = {
        "db": os.path.abspath(db_path),
        "size_mb": round(os.path.getsize(db_path) / 1024 / 1024, 1),
        "questions": c["questions"],
        "policy": "all" if keep_unanswered else "answered_only",
        "dropped_no_answer": dropped_no_answer,
        "answers": c["with_answer"],
        "families": len(fam_members),
        "families_ge3": sum(1 for n in fam_members.values() if n >= 3),
        "kps_used": len(kp_members),
        "elapsed_s": round(time.time() - t0, 1),
        "token_time_s": round(t_tok, 1),
        "errors": {k: v for k, v in c.items() if k.endswith("error")},
        "kp_source": {k.split(":", 1)[1]: v for k, v in c.items() if k.startswith("kp_source:")},
        "qtype": {k.split(":", 1)[1]: v for k, v in c.items() if k.startswith("qtype:")},
    }
    return res


def collect_stats(db_path: str = DEFAULT_DB) -> dict:
    """把库里的实测统计读回来（不信任 build() 的计数器，直接查库）。"""
    conn = dbmod.connect(db_path)

    def one(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    combos = {r[0] or "": r[1] for r in
              conn.execute("SELECT fmt, COUNT(*) FROM questions GROUP BY fmt")}
    n_omml = sum(n for f, n in combos.items() if "omml" in f)
    n_ole = sum(n for f, n in combos.items() if "ole" in f)
    n_embed = sum(n for f, n in combos.items() if "embed" in f)
    n_tbl = sum(n for f, n in combos.items() if "table" in f)
    ans = {r[0]: {"n": r[1], "answered": r[2]} for r in
           conn.execute("SELECT qtype, COUNT(*), SUM(answer_raw<>'') FROM questions GROUP BY qtype")}
    kp_src = {r[0] or "none": r[1] for r in
              conn.execute("SELECT kp_source, COUNT(*) FROM questions GROUP BY kp_source")}
    st = {
        "db": os.path.abspath(db_path),
        "size_mb": round(os.path.getsize(db_path) / 1024 / 1024, 1),
        "questions": one("SELECT COUNT(*) FROM questions"),
        "distinct_no": one("SELECT COUNT(DISTINCT no) FROM questions"),
        "dup_no": one("SELECT COUNT(*) FROM (SELECT no FROM questions GROUP BY no HAVING COUNT(*)>1)"),
        "tokens_empty": one("SELECT COUNT(*) FROM questions WHERE tokens_json='[]'"),
        "text_empty": one("SELECT COUNT(*) FROM questions WHERE text_plain=''"),
        "text_len_avg": round(one("SELECT AVG(text_len) FROM questions") or 0.0, 1),
        "media_count_total": one("SELECT SUM(media_count) FROM questions") or 0,
        "qtype": {k: v["n"] for k, v in ans.items()},
        "answer_by_qtype": ans,
        "answer_raw_nonempty": one("SELECT COUNT(*) FROM questions WHERE answer_raw<>''"),
        "fmt": {
            "omml": n_omml, "ole": n_ole, "embed": n_embed,
            "ole_or_embed": sum(n for f, n in combos.items() if "ole" in f or "embed" in f),
            "table": n_tbl, "plain": combos.get("plain", 0),
            "combos": dict(sorted(combos.items(), key=lambda kv: -kv[1])),
        },
        "families": one("SELECT COUNT(*) FROM families"),
        "family_members": one("SELECT SUM(size) FROM families") or 0,
        "families_ge2": one("SELECT COUNT(*) FROM families WHERE size>=2"),
        "families_ge3": one("SELECT COUNT(*) FROM families WHERE size>=3"),
        "family_max_size": one("SELECT MAX(size) FROM families") or 0,
        "kps_rows": one("SELECT COUNT(*) FROM kps"),
        "kps_used": one("SELECT COUNT(DISTINCT kp_id) FROM questions WHERE kp_id IS NOT NULL"),
        "kps_questions_sum": one("SELECT SUM(n_questions) FROM kps") or 0,
        "kp_source": kp_src,
        "meta": {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")},
    }
    for suf in ("-wal", "-shm"):
        p = db_path + suf
        if os.path.exists(p):
            st["size_mb_with" + suf.replace("-", "_")] = round(os.path.getsize(p) / 1024 / 1024, 1)
    conn.close()
    return st


def acceptance_checks(st: dict) -> list:
    """M0 验收阈值。区间说明写进 note，实测值原样落盘，不粉饰。"""
    omml = st["fmt"]["omml"]
    ole = st["fmt"].get("ole_or_embed", st["fmt"].get("ole", 0))
    checks = [
        ("questions", "12280（有答案）", st.get("questions"), st.get("questions") == FULL_N,
         "只收有答案的题：22,898 里 10,618 道没答案被丢弃（meta.dropped_no_answer）"),
        ("no_unique", "dup=0", st.get("dup_no"), st.get("dup_no") == 0,
         "questions.no 是主键，不允许重复"),
        ("fmt_omml", "~7166", omml, 6800 <= omml <= 7500,
         "OMML 公式题；表格内嵌公式按 token 记为 table"),
        ("fmt_ole_embed", "~2856", ole, 2600 <= ole <= 3100,
         "OLE(MathType/DSMT4)+内联 embed 对象"),
        ("answer_raw", "12280", st.get("answer_raw_nonempty"),
         st.get("answer_raw_nonempty") == FULL_N, "入库题必须都有答案"),
        ("families", "~2091 [1900,2300]", st.get("families"),
         1900 <= st.get("families", 0) <= 2300, "裁剪后剩余的题族数"),
        ("families_ge3", ">=1200", st.get("families_ge3", 0), st.get("families_ge3", 0) >= 1200,
         "size>=3 的族（训练卷扩题要用）"),
        ("kps_rows", "~30 [25,45]", st.get("kps_rows"), 25 <= st.get("kps_rows", 0) <= 45,
         "知识点表行数；taxonomy 实际定义 34 条（5 章 + 综合）"),
    ]
    return [{"criterion": c, "expected": e, "observed": o, "ok": bool(ok), "note": n}
            for c, e, o, ok, n in checks]


def _sites(xml: bytes, kind: str):
    """在 document.xml 里精确定位公式节点：omml → m:oMath，ole → ProgID=Equation.DSMT4 的 OLEObject。"""
    from lxml import etree

    root = etree.fromstring(xml)
    out = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        q = etree.QName(el)
        if kind == "omml" and q.namespace == M_NS and q.localname == "oMath":
            out.append(el)
        elif kind == "ole" and q.localname == "OLEObject" \
                and "Equation.DSMT4" in (el.get("ProgID") or ""):
            out.append(el)
    return out


# token 侧会把这些容器整体当一个 embed token 搬走（XML 原样保留）
_EMBED_CONTAINERS = ("txbxContent", "drawing", "pict", "AlternateContent")


def _where(el) -> str:
    """公式节点挂在谁下面：top=段落里（应为 math/embed token）、table=表格内、object=图形/文本框内。"""
    from lxml import etree

    anc, p = [], el.getparent()
    while p is not None:
        if isinstance(p.tag, str):
            anc.append(etree.QName(p).localname)
        p = p.getparent()
    if "tbl" in anc:
        return "table"
    if any(a in _EMBED_CONTAINERS for a in anc):
        return "object"
    return "top"


def _raw_flags(xml: bytes) -> dict:
    return {"omml": b"<m:oMath" in xml, "ole": b"Equation.DSMT4" in xml}


def audit_one(no: str, db_path: str = DEFAULT_DB) -> dict:
    """单题核对：raw XML 里有、fmt 里没有的公式，到底跑到哪个容器里去了。"""
    import zipfile

    conn = dbmod.connect(db_path)
    row = conn.execute("SELECT fmt FROM questions WHERE no=?", (no,)).fetchone()
    conn.close()
    fmt = (row["fmt"] if row else "") or ""
    with zipfile.ZipFile(os.path.join(ROOT, f"{no}.docx")) as z:
        xml = z.read("word/document.xml")
    entries = []
    for kind in ("omml", "ole"):
        if not _raw_flags(xml)[kind] or kind in fmt:
            continue
        wheres = Counter(_where(s) for s in _sites(xml, kind))
        entries.append({"kind": kind,
                        "where": wheres.most_common(1)[0][0] if wheres else "unknown",
                        "count": sum(wheres.values()), "wheres": dict(wheres)})
    first = entries[0] if entries else {"kind": None, "where": "in_fmt", "count": 0}
    return {"no": no, "fmt": fmt, **first, "entries": entries}


def audit_vs_raw_xml(db_path: str = DEFAULT_DB, limit: int = 0) -> dict:
    """fmt 与原始 docx XML 的差集核对：任何对不上的题都要能解释（表格内 / 文本框内的嵌套公式）。

    只读原始 docx，用来证明“入库没有丢公式”，而不是拿编码扫描的旧数字凑答案。
    """
    import zipfile

    conn = dbmod.connect(db_path)
    rows = [(r[0], r[1] or "") for r in
            conn.execute("SELECT no, fmt FROM questions ORDER BY no")]
    conn.close()
    if limit:
        rows = rows[:limit]

    c = Counter()
    mixed, unexplained = [], []
    for no, fmt in rows:
        try:
            with zipfile.ZipFile(os.path.join(ROOT, f"{no}.docx")) as z:
                xml = z.read("word/document.xml")
        except Exception:  # noqa: BLE001
            c["read_error"] += 1
            unexplained.append(f"{no}:read")
            continue
        raw = _raw_flags(xml)
        for kind in ("omml", "ole"):
            c[f"raw_{kind}"] += raw[kind]
            if not raw[kind]:
                continue
            if kind in fmt:
                c[f"db_{kind}"] += 1
                continue
            wheres = Counter(_where(s) for s in _sites(xml, kind))
            if not wheres or wheres.get("top"):
                unexplained.append(f"{no}:{kind}")
            elif "table" in wheres and "object" in wheres:
                mixed.append(f"{no}:{kind}")
                c[f"in_table_{kind}"] += 1
            elif "table" in wheres:
                c[f"in_table_{kind}"] += 1
            else:
                c[f"in_object_{kind}"] += 1
    return {
        "scanned": len(rows),
        "raw_omml": c["raw_omml"], "db_omml": c["db_omml"],
        "in_table_omml": c["in_table_omml"], "in_object_omml": c["in_object_omml"],
        "raw_ole": c["raw_ole"], "db_ole": c["db_ole"],
        "in_table_ole": c["in_table_ole"], "in_object_ole": c["in_object_ole"],
        "read_error": c["read_error"],
        "mixed_table_object": mixed,
        "unexplained": unexplained,
        "explained": "差集全部落在 w:tbl（整表一个 table token）或 w:txbxContent/w:drawing/w:pict"
                     "（整块一个 embed token）里，XML 原样保留，组卷时 compose 整块搬运，不丢公式。",
    }


def write_report(db_path: str = DEFAULT_DB, report_path: str = DEFAULT_REPORT,
                 build_res: dict | None = None, audit: bool = False) -> dict:
    """写 data/build_report.json：库里读回的实测统计 + 验收判定 + 可选 XML 差集核对。"""
    st = collect_stats(db_path)
    st["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["full_corpus"] = st["questions"] >= FULL_N
    checks = acceptance_checks(st)
    if st["full_corpus"]:
        st["acceptance"] = checks
        st["acceptance_all_ok"] = all(a["ok"] for a in checks)
    else:
        st["acceptance"] = [{**a, "ok": None} for a in checks]
        st["acceptance_all_ok"] = None
    if build_res:
        st["build"] = {k: v for k, v in build_res.items() if k != "db"}
    if audit:
        st["raw_xml_audit"] = audit_vs_raw_xml(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    return st


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--skip-tokens", action="store_true", help="只导元数据（快速调试）")
    ap.add_argument("--report", nargs="?", const=DEFAULT_REPORT, default=None,
                    help=f"建库后写统计报告（默认 {DEFAULT_REPORT}）")
    ap.add_argument("--audit", action="store_true", help="报告里附原始 docx XML 差集核对（+~25s）")
    args = ap.parse_args(argv)
    res = build(limit=args.limit, db_path=args.db, fresh=args.fresh, skip_tokens=args.skip_tokens)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.report is not None:
        st = write_report(args.db, args.report, build_res=res, audit=args.audit)
        print("report -> " + os.path.abspath(args.report))
        print(json.dumps({"acceptance": st["acceptance"],
                          "acceptance_all_ok": st["acceptance_all_ok"]},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
