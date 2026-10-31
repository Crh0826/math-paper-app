# -*- coding: utf-8 -*-
"""t51 · 题库导入核心：列名归一 → 校验 → 幂等入库 → 导入报告。

设计要点
--------
* **统一条目模型**（JSON/Excel/CSV/docx 都归到这一份）::

      {"no": "00001", "qtype": "选择", "stem": "...", "options": ["A．…", "B．…"],
       "answer": "B", "analysis": "...", "kp_id": "1.2", "chapter": "第1章 有理数",
       "figures": ["media/1.png"], "subject_pk": "math7a"}

* **幂等**：以 `no` 为主键 upsert，重复导入 = 更新，不会产生重复行。
* **坏行不落库**：缺 no / 缺 answer / qtype 非法 / 选择题没选项 → 记进报告的 errors。
* **多学科**：条目挂 `subject_pk`；kps 也按学科分开，`subject_scope()` 给 picker 做学科过滤。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3

import db as dbmod

DEFAULT_SUBJECT = dbmod.DEFAULT_SUBJECT
QTYPES = ("选择", "判断", "填空", "解答", "其他")
REQUIRED = ("no", "qtype", "stem", "answer")

#: 列名别名（中文表头 / 英文表头都认）。key 是统一字段名。
ALIASES = {
    "no": ("no", "题号", "编号", "题目编号", "id", "qid", "序号"),
    "qtype": ("qtype", "题型", "类型", "题目类型", "type"),
    "stem": ("stem", "题干", "题目", "题目内容", "question", "content", "题干内容"),
    "options": ("options", "选项", "选项内容", "choices"),
    "answer": ("answer", "答案", "正确答案", "参考答案", "key"),
    "analysis": ("analysis", "解析", "答案解析", "详解", "explain", "solution"),
    "kp_id": ("kp_id", "知识点", "知识点编号", "考点", "kp", "knowledge"),
    "chapter": ("chapter", "章节", "章", "所属章节", "unit"),
    "figure": ("figure", "figures", "图片", "图", "插图", "图片路径", "media"),
    "subject": ("subject", "学科", "科目"),
    "grade": ("grade", "年级"),
    "version": ("version", "版本", "教材", "教材版本"),
}
#: 选择题选项列（A/B/C/D 四列，或一列里用 | /、/ 竖线分隔）
OPTION_COLS = {"A": ("a", "选项a", "a．"), "B": ("b", "选项b"), "C": ("c", "选项c"),
               "D": ("d", "选项d"), "E": ("e", "选项e"), "F": ("f", "选项f")}

_SUBJECT_SLUG = {"数学": "math", "语文": "chinese", "英语": "english",
                 "物理": "physics", "化学": "chemistry", "生物": "biology",
                 "历史": "history", "地理": "geography", "道法": "politics",
                 "科学": "science"}
_GRADE_SLUG = {"七年级上": "7a", "七年级下": "7b", "八年级上": "8a", "八年级下": "8b",
               "九年级上": "9a", "九年级下": "9b", "高一": "10a", "高二": "10b", "高三": "10c"}
_QTYPE_FIX = {"单选": "选择", "多选": "选择", "选择题": "选择", "判断题": "判断",
              "填空题": "填空", "解答题": "解答", "计算题": "解答", "简答题": "解答",
              "证明题": "解答", "应用题": "解答", "其他题": "其他"}
_RE_OPTION_SPLIT = re.compile(r"\s*[|｜]\s*")


# ---------------------------------------------------------------- 工具

def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_key(s):
    return re.sub(r"[\s_\-（）()]+", "", str(s or "")).lower()


def _pick(row, field):
    """按别名从一行里取值（大小写/空格/下划线不敏感）。"""
    table = {}
    for k, v in row.items():
        table[_norm_key(k)] = v
    for alias in ALIASES.get(field, (field,)):
        key = _norm_key(alias)
        if key in table and table[key] not in (None, ""):
            return table[key]
    return None


def _pick_options(row):
    """选项：优先一列竖线分隔，其次 A/B/C/D 四列，最后 options 数组。"""
    raw = _pick(row, "options")
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    if raw:
        parts = [p.strip() for p in _RE_OPTION_SPLIT.split(str(raw)) if p.strip()]
        if len(parts) >= 2:
            return parts
    opts = []
    for letter, aliases in sorted(OPTION_COLS.items()):
        for alias in aliases:
            key = _norm_key(alias)
            for k, v in row.items():
                if _norm_key(k) == key and v not in (None, ""):
                    text = str(v).strip()
                    if not text.upper().startswith(letter):
                        text = "%s．%s" % (letter, text)
                    opts.append(text)
                    break
            else:
                continue
            break
    return opts


def _subject_pk(name, grade, version):
    """学科主键：math7a 这种可读 slug；生僻学科用哈希兜底。"""
    if (name or "数学") == "数学" and (grade or "七年级上") == "七年级上" \
            and (version or "沪科版") == "沪科版":
        return DEFAULT_SUBJECT
    slug = "%s%s" % (_SUBJECT_SLUG.get(name or "数学", ""),
                     _GRADE_SLUG.get(grade or "", ""))
    if slug and slug != "math":
        return slug
    token = "|".join([name or "", grade or "", version or ""])
    return "subj_" + hashlib.sha1(token.encode("utf-8")).hexdigest()[:8]


def subject_scope(conn, subject_pk):
    """给 picker 用的学科过滤：该学科出现过的章节列表。

    picker 的 scope 目前只认 chapters/kps；学科隔离靠"章节集合"实现，
    库里同时还有 `questions.subject_pk` 供 UI/统计直接按学科查。
    """
    try:
        chapters = [r[0] for r in conn.execute(
            "SELECT DISTINCT chapter FROM questions WHERE subject_pk=? AND chapter IS NOT NULL"
            " AND chapter<>'' ORDER BY chapter", (subject_pk,))]
    except sqlite3.OperationalError:
        chapters = []
    return {"chapters": chapters, "kps": []}


# ---------------------------------------------------------------- 校验

def normalize_item(raw, subject_pk=DEFAULT_SUBJECT, row_no=None):
    """一行/一个 JSON 对象 → (item, error)。error 是给报告看的人话。"""
    where = ("第 %s 行" % row_no) if row_no is not None else "条目"
    if not isinstance(raw, dict):
        return None, "%s：不是对象" % where

    no = _pick(raw, "no")
    no = str(no).strip() if no is not None else ""
    if not no:
        return None, "%s：缺 no（题号）" % where

    qtype = str(_pick(raw, "qtype") or "").strip()
    qtype = _QTYPE_FIX.get(qtype, qtype)
    if not qtype:
        return None, "%s %s：缺 qtype（题型）" % (where, no)
    if qtype not in QTYPES:
        return None, "%s %s：qtype 非法 %r（允许 %s）" % (where, no, qtype, "/".join(QTYPES))

    stem = str(_pick(raw, "stem") or "").strip()
    if not stem:
        return None, "%s %s：缺题干（stem）" % (where, no)

    answer = _pick(raw, "answer")
    answer = str(answer).strip() if answer is not None else ""
    if not answer:
        return None, "%s %s：缺 answer（答案）—— 无答案的题不收" % (where, no)

    options = _pick_options(raw)
    if qtype == "选择" and len(options) < 2:
        return None, "%s %s：选择题必须有选项（至少 2 个）" % (where, no)

    figures = _pick(raw, "figure") or []
    if isinstance(figures, str):
        figures = [p.strip() for p in re.split(r"[|｜,，;；]", figures) if p.strip()]
    figures = [str(f) for f in figures if str(f).strip()]

    kp_id = _pick(raw, "kp_id")
    chapter = _pick(raw, "chapter")
    return {
        "no": no,
        "qtype": qtype,
        "stem": stem,
        "options": options,
        "answer": answer,
        "analysis": str(_pick(raw, "analysis") or "").strip(),
        "kp_id": str(kp_id).strip() if kp_id else None,
        "chapter": str(chapter).strip() if chapter else None,
        "figures": figures,
        "subject_pk": subject_pk,
    }, None


# ---------------------------------------------------------------- 入库

def _row_of_item(item):
    """item → questions 表的一行（含拼好的卷面文本与 token）。"""
    options = item["options"]
    text = item["stem"]
    if options:
        text = text + "\n" + "\n".join(options)
    tokens = [{"kind": "text", "text": item["stem"]}]
    tokens += [{"kind": "text", "text": o} for o in options]
    tokens += [{"kind": "embed", "name": f, "n_media": 1} for f in item["figures"]]
    if item["analysis"]:
        tokens.append({"kind": "analysis", "text": item["analysis"]})
    fmt = "embed" if item["figures"] else "plain"
    return {
        "no": item["no"], "qtype": item["qtype"],
        "option_count": len(options), "sub_count": 0, "is_empty": 0 if text.strip() else 1,
        "media_count": len(item["figures"]), "text_len": len(text),
        "text_plain": text, "tokens_json": json.dumps(tokens, ensure_ascii=False),
        "fmt": fmt, "src_title": item.get("analysis", "")[:0] or "import",
        "src_para": None, "family_key": None,
        "kp_id": item["kp_id"], "chapter": item["chapter"],
        "kp": None, "kp_source": "import",
        "answer_raw": item["answer"], "check_status": "",
        "subject_pk": item["subject_pk"],
    }


_COLS = ("no", "qtype", "option_count", "sub_count", "is_empty", "media_count", "text_len",
         "text_plain", "tokens_json", "fmt", "src_title", "src_para", "family_key",
         "kp_id", "chapter", "kp", "kp_source", "answer_raw", "check_status", "subject_pk")


def upsert_items(conn, items, subject_pk=DEFAULT_SUBJECT):
    """幂等入库：同 no 更新、无则新增；返回计数 + 明细。"""
    conn.row_factory = sqlite3.Row
    existing = {r[0] for r in conn.execute("SELECT no FROM questions")}
    new = updated = 0
    detail = []
    placeholders = ",".join("?" * len(_COLS))
    sets = ",".join("%s=excluded.%s" % (c, c) for c in _COLS if c != "no")
    sql = "INSERT INTO questions(%s) VALUES(%s) ON CONFLICT(no) DO UPDATE SET %s" % (
        ",".join(_COLS), placeholders, sets)
    for item in items:
        row = _row_of_item(item)
        conn.execute(sql, [row[c] for c in _COLS])
        if row["no"] in existing:
            updated += 1
            detail.append({"no": row["no"], "status": "updated"})
        else:
            new += 1
            detail.append({"no": row["no"], "status": "new"})
    # 知识点表：按学科分开，重算 n_questions
    kp_ids = {item["kp_id"] for item in items if item.get("kp_id")}
    for kp_id in kp_ids:
        chapter = next((i["chapter"] for i in items if i.get("kp_id") == kp_id), None)
        conn.execute("INSERT OR IGNORE INTO kps(kp_id, subject_pk, chapter, kp, n_questions)"
                     " VALUES(?,?,?,?,0)", (kp_id, subject_pk, chapter, None))
    for kp_id in kp_ids:
        n = conn.execute("SELECT COUNT(*) FROM questions WHERE kp_id=? AND subject_pk=?",
                         (kp_id, subject_pk)).fetchone()[0]
        conn.execute("UPDATE kps SET n_questions=? WHERE kp_id=? AND subject_pk=?",
                     (n, kp_id, subject_pk))
    conn.commit()
    return {"new": new, "updated": updated, "items": detail}


# ---------------------------------------------------------------- 入口

def import_file(path, kind=None, *, db_path=None, subject=None, report_path=None,
                dry_run=False):
    """导入一个文件。kind 为空时按扩展名判断。

    返回报告 dict（同时写 `report_path`，默认 `data/import_report.json`）。
    """
    from . import jsonfile, tabular

    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    kind = (kind or "").lower()
    if not kind:
        ext = os.path.splitext(path)[1].lower()
        kind = {".json": "json", ".xlsx": "excel", ".xlsm": "excel",
                ".csv": "csv"}.get(ext, "")
    if kind not in ("json", "excel", "csv"):
        raise ValueError("不支持的格式：%r（可用 json/excel/csv）" % (kind or path))

    started = _now()
    if kind == "json":
        meta, raw_items = jsonfile.read_json(path)
    else:
        meta, raw_items = tabular.read_table(path, kind)

    sub = dict(subject or {})
    name = sub.get("name") or meta.get("subject") or "数学"
    grade = sub.get("grade") or meta.get("grade") or "七年级上"
    version = sub.get("version") or meta.get("version") or "沪科版"
    subject_pk = sub.get("subject_pk") or _subject_pk(name, grade, version)

    db_path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "..", "data", "app.db")
    report_path = report_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                              "..", "data", "import_report.json")

    conn = dbmod.connect(db_path)
    try:
        dbmod.ensure_subject(conn, subject_pk, name, grade, version)
        items, errors = [], []
        for i, raw in enumerate(raw_items, 1):
            item, err = normalize_item(raw, subject_pk, row_no=i)
            if err:
                errors.append({"row": i, "reason": err})
            else:
                items.append(item)
        res = {"new": 0, "updated": 0, "items": []} if dry_run else \
            upsert_items(conn, items, subject_pk)
        skipped = 0
        report = {
            "generated_at": started,
            "format": kind,
            "source": path,
            "dry_run": bool(dry_run),
            "subject_pk": subject_pk,
            "subject": {"name": name, "grade": grade, "version": version},
            "rows": len(raw_items),
            "new": res["new"],
            "updated": res["updated"],
            "skipped": skipped,
            "failed": len(errors),
            "errors": errors,
            "items": res["items"],
        }
        if not dry_run:
            os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        report["report_path"] = report_path
        return report
    finally:
        conn.close()
