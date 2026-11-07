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
    # t67：t62 之后 picker 在「没给学科」时会默认 math7a；这里必须把学科一起带上，
    # 否则语文/英语等学科的 scope 会被默认数学过滤成 0 题（t65 发现）。
    return {"chapters": chapters, "kps": [],
            "subject_pk": str(subject_pk or ""),
            "subjects": [str(subject_pk)] if subject_pk else []}


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


def _signature(row):
    """判断"内容没变"用的指纹：题干 + 答案 + 题型 + 知识点 + 章节。"""
    # 图片也算「内容」：题还是那道题、但图变了（重挂/删旧图）必须更新 tokens/media_count
    return (row["text_plain"], row["answer_raw"], row["qtype"], row["kp_id"], row["chapter"],
            int(row.get("media_count") or 0))


def _fingerprint(row: dict) -> str:
    """内容指纹（不依赖原题号）：同内容重复导入要拿到同一个自动号。"""
    import hashlib
    body = "\x1f".join([str(row.get("qtype") or ""), str(row.get("text_plain") or ""),
                        str(row.get("answer_raw") or ""), str(row.get("options_json") or ""),
                        str(row.get("chapter") or ""), str(row.get("kp_id") or "")])
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def upsert_items(conn, items, subject_pk=DEFAULT_SUBJECT, renumber=False):
    """幂等入库：同 no 且同内容 → 跳过；同 no 同内容变 → 更新；没有 → 新增。

    t90 安全线：**同 no 已存在但属于别的学科 → 默认拒绝这一行**（绝不静默覆盖），
    并把它记进 ``conflicts``；``renumber=True`` 时改用本学科号段自动分配新号
    （同一内容指纹重复导入得到同一个号，幂等）。
    """
    conn.row_factory = sqlite3.Row
    existing = {}
    for r in conn.execute("SELECT no, subject_pk, text_plain, answer_raw, qtype, kp_id,"
                          " chapter, COALESCE(media_count,0) FROM questions"):
        # 与 _signature() 同序同长（含 media_count），否则「内容没变」永远判不出来
        existing[r[0]] = (r[1] or "", (r[2], r[3], r[4], r[5], r[6], int(r[7] or 0)))
    new = updated = skipped = rejected = 0
    detail, conflicts, renumbered = [], [], []
    placeholders = ",".join("?" * len(_COLS))
    sets = ",".join("%s=excluded.%s" % (c, c) for c in _COLS if c != "no")
    sql = "INSERT INTO questions(%s) VALUES(%s) ON CONFLICT(no) DO UPDATE SET %s" % (
        ",".join(_COLS), placeholders, sets)
    for item in items:
        row = _row_of_item(item)
        old = existing.get(row["no"])
        if old is not None and old[0] and old[0] != subject_pk:
            # ---- 跨学科同题号：默认拒绝；开了 renumber 才改号写入
            if not renumber:
                rejected += 1
                conflicts.append({"no": row["no"], "existing_subject": old[0],
                                  "new_subject": subject_pk})
                detail.append({"no": row["no"], "status": "rejected",
                               "reason": "题号已被学科 %s 占用" % old[0]})
                continue
            fp = _fingerprint(row)
            new_no = dbmod.alloc_subject_no(conn, subject_pk, fp)
            renumbered.append({"from": row["no"], "to": new_no,
                               "existing_subject": old[0]})
            item = dict(item, no=new_no)
            row = _row_of_item(item)
            old = existing.get(new_no)
        if old is not None and old[0] == subject_pk and old[1] == _signature(row):
            skipped += 1
            detail.append({"no": row["no"], "status": "skipped"})
            continue
        conn.execute(sql, [row[c] for c in _COLS])
        existing[row["no"]] = (subject_pk, _signature(row))
        if old is not None:
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
    return {"new": new, "updated": updated, "skipped": skipped,
            "rejected": rejected, "conflicts": conflicts, "renumbered": renumbered,
            "items": detail}


# ---------------------------------------------------------------- 入口

def guess_kind(path, kind=None):
    """按显式 kind 或扩展名判断格式；认不出返回 ""。"""
    kind = (kind or "").lower()
    if kind in ("json", "excel", "csv", "docx", "zip"):
        return kind
    ext = os.path.splitext(path)[1].lower()
    return {".json": "json", ".xlsx": "excel", ".xlsm": "excel",
            ".csv": "csv", ".docx": "docx", ".zip": "zip"}.get(ext, "")


def read_source(path, kind):
    """读源文件 → (meta, raw_rows)。CSV/Excel/JSON 三种都走这里。"""
    from . import jsonfile, tabular

    if kind == "json":
        return jsonfile.read_json(path)
    return tabular.read_table(path, kind)

# ---------------------------------------------------------------- t90 恢复：图片 / 批量导入

#: 认得的图片扩展名（导入图片时用）
FIGURE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
#: 图片长边上限（超过就等比缩到 1600，卷面 200pt 宽足够）
FIGURE_MAX_SIDE = 1600
FIG_MAX_SIDE = FIGURE_MAX_SIDE      # 兼容旧名（测试/外部脚本用过）
#: 单张图片体积上限（5 MB）
FIGURE_MAX_BYTES = 5 * 1024 * 1024
FIG_MAX_BYTES = FIGURE_MAX_BYTES


def normalize_rows(raw_items, subject_pk=DEFAULT_SUBJECT, start_line=1):
    """全量校验（不落库）→ (items, errors[{line, reason}])。Web 预览与真实导入同一套。"""
    items, errors, seen = [], [], {}
    for offset, raw in enumerate(raw_items or []):
        line = start_line + offset
        item, err = normalize_item(raw, subject_pk, line)
        if err:
            errors.append({"line": line, "reason": err})
            continue
        no = item["no"]
        if no in seen:
            errors.append({"line": line,
                           "reason": "第 %d 行 %s：文件内题号重复（首次出现在第 %d 行）"
                                     % (line, no, seen[no])})
            continue
        seen[no] = line
        item["_line"] = line
        items.append(item)
    return items, errors


def figures_root(db_path=None):
    """图片根目录：默认与 app.db 同级的 ``figures/``（即 ``app/data/figures``）。"""
    base = os.path.dirname(os.path.abspath(
        db_path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "app.db")))
    return os.path.join(base, "figures")


def parse_figure_name(filename):
    """``00004.png`` → (no="00004", seq=1, why="")；``00004_2.jpg`` → ("00004", 2, "")。

    规则：取第一个 ``_`` 前（或扩展名前）的段作题号；``_2`` 这种后缀作序号。
    题号必须是「字母/数字/短横」组成，别的当未匹配（why 给人话原因）。
    """
    name = os.path.basename(str(filename or "")).strip()
    ext = os.path.splitext(name)[1].lower()
    if ext not in FIGURE_EXTS:
        return None, 0, "不是图片（支持 %s）" % " / ".join(FIGURE_EXTS)
    stem = name[: -len(ext)]
    parts = stem.split("_")
    no = (parts[0] or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z\-]+", no or ""):
        return None, 0, "文件名里的题号不合法：%r" % (no or stem)
    seq = 1
    if len(parts) > 1:
        m = re.fullmatch(r"(\d+)", (parts[1] or "").strip())
        seq = int(m.group(1)) if m else 1
    return no, seq, ""


def _prepare_image(data, filename):
    """校验 + 必要时缩放 → (bytes, ext, note)。非图片/坏图给人话错误。"""
    from PIL import Image
    import io as _io

    raw = data or b""
    if not raw:
        raise ValueError("图片是空的：%s" % filename)
    try:
        img = Image.open(_io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - 坏图统一人话
        raise ValueError("不是能识别的图片（%s: %s）" % (filename, exc))
    fmt = (img.format or "").upper()
    note = ""
    w, h = img.size
    long_side = max(w, h)
    if len(raw) > FIGURE_MAX_BYTES or long_side > FIGURE_MAX_SIDE:
        scale = FIGURE_MAX_SIDE / float(long_side) if long_side > FIGURE_MAX_SIDE else 1.0
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            note = "已从 %dx%d 缩到长边 %d" % (w, h, FIGURE_MAX_SIDE)
        else:
            note = ""
        buf = _io.BytesIO()
        if fmt in ("JPEG", "JPG"):
            img.convert("RGB").save(buf, "JPEG", quality=88)
            return buf.getvalue(), ".jpg", note
        img.save(buf, "PNG")
        return buf.getvalue(), ".png", note
    if fmt in ("JPEG", "JPG"):
        return raw, ".jpg", note
    if fmt == "PNG":
        return raw, ".png", note
    if fmt == "GIF":
        return raw, ".gif", note
    if fmt == "BMP":
        return raw, ".bmp", note
    if fmt == "WEBP":
        return raw, ".webp", note
    raise ValueError("图片格式不支持（%s）" % (fmt or "未知"))


def figure_filename(no, seq, filename, ext=".png"):
    """题号 + 序号 + 原文件名 → 规范落盘名（**与 tokens 里的 name 完全一致**）。

    * ``99005.png``   → ``99005.png``（第 1 张，保持原名）
    * ``99005_2.png`` → ``99005_2.png``（第 2 张）
    * 原文件名不合规（没有 ``<题号>_<seq>`` 前缀）→ 按 ``<题号>_<seq><ext>`` 生成
    """
    base = os.path.basename(str(filename or "")).strip()
    stem, fext = os.path.splitext(base)
    fext = (fext or ext or ".png").lower()
    if fext == ".jpeg":
        fext = ".jpg"
    want = str(no) if int(seq) <= 1 else "%s_%d" % (no, int(seq))
    if stem == want or (int(seq) > 1 and stem == "%s_%d" % (no, int(seq))):
        return stem + fext
    return want + fext


def write_figure(root, subject_pk, no, seq, data, ext, filename=None):
    """落盘到 ``<root>/<subject_pk>/<no>/<原文件名>``；同内容已存在 → 不重写。

    **落盘名与 tokens_json 里的 embed.name 由同一个函数算**（figure_filename），
    否则 paper 按 name 找不到图 → 卷面报「有图但找不到」。
    → (相对路径, "new"|"same"|"updated")
    """
    name = figure_filename(no, seq, filename or ("%s%s" % (no, ext)), ext)
    rel = "/".join([str(subject_pk), str(no), name])
    path = os.path.join(root, str(subject_pk), str(no), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                if f.read() == data:
                    return rel, "same"
        except OSError:
            pass
        with open(path, "wb") as f:
            f.write(data)
        return rel, "updated"
    with open(path, "wb") as f:
        f.write(data)
    return rel, "new"


def _rebuild_tokens(tokens_json, figures, keep_analysis=True):
    """把「图片引用」写回 tokens_json：保留文字/解析，图片按序号追加成 embed token。"""
    try:
        tokens = json.loads(tokens_json or "[]")
    except ValueError:
        tokens = []
    if not isinstance(tokens, list):
        tokens = []
    kept = []
    for tok in tokens:
        kind = (tok or {}).get("kind")
        if kind == "embed":
            continue
        if kind == "analysis" and not keep_analysis:
            continue
        kept.append(tok)
    for fig in figures or []:
        kept.append({"kind": "embed", "name": fig})
    return json.dumps(kept, ensure_ascii=False)


def attach_figures(db_path, subject_pk, images, root=None):
    """把一批 ``(filename, bytes)`` 图片挂到题上（t88 核心）。

    * 文件名以题号开头（``00004.png`` / ``00004_2.jpg``），按 seq 排序落盘；
    * 幂等：同内容不重写；同一题重复导入只更新引用，不产生孤儿；
    * 写回 ``questions.media_count`` 与 ``tokens_json``（embed token，paper.py 认得）；
    * 题库里没有的题号 / 非图片 / 坏图 → 进 ``unmatched``（带原因），不写库。
    """
    root = root or figures_root(db_path)
    out = {"saved": 0, "skipped": 0, "updated": 0, "resized": 0,
           "unmatched": [], "unmatched_count": 0, "still_missing": [],
           "bysubject": subject_pk}
    conn = dbmod.connect(db_path)
    try:
        by_no = {}
        for filename, data in images or []:
            no, seq, why = parse_figure_name(filename)
            if not no:
                out["unmatched"].append({"file": os.path.basename(str(filename)),
                                         "reason": why or "文件名认不出题号"})
                continue
            row = conn.execute(
                "SELECT subject_pk FROM questions WHERE no=? AND COALESCE(subject_pk,?) IN (?,?)",
                (no, dbmod.DEFAULT_SUBJECT, subject_pk, dbmod.DEFAULT_SUBJECT)).fetchone()
            if row is None:
                out["unmatched"].append({
                    "file": os.path.basename(str(filename)),
                    "reason": "题库里没有题号 %s（先把题目导进来，或检查文件名）" % no})
                continue
            try:
                payload, ext, note = _prepare_image(data, filename)
            except ValueError as exc:
                out["unmatched"].append({"file": os.path.basename(str(filename)),
                                         "reason": str(exc)})
                continue
            if note:
                out["resized"] += 1
            # 图必须落在**题目实际所在的学科目录**（否则卷面按题号+学科找不到图）
            row_subject = str((row["subject_pk"] if "subject_pk" in row.keys() else "") or
                              subject_pk or dbmod.DEFAULT_SUBJECT)
            rel, status = write_figure(root, row_subject, no, seq, payload, ext,
                                       filename=filename)
            if status == "new":
                out["saved"] += 1
            elif status == "same":
                out["skipped"] += 1
            else:
                out["updated"] += 1
            by_no.setdefault(no, []).append((int(seq), rel))
        for no, figs in by_no.items():
            figs.sort(key=lambda x: x[0])
            row = conn.execute("SELECT tokens_json FROM questions WHERE no=?", (no,)).fetchone()
            tokens_json = row[0] if row else ""
            names = [rel for _, rel in figs]
            new_tokens = _rebuild_tokens(tokens_json, names)
            fmt_bits = ["embed"]
            conn.execute("UPDATE questions SET media_count=?, tokens_json=?, fmt=? WHERE no=?",
                         (len(names), new_tokens, ",".join(fmt_bits), no))
        conn.commit()
        # 仍然缺图（题干提到图但没有 media）的题号，方便页面提示
        try:
            marks = ("如图", "见图", "图中", "下图", "上图", "图①", "图②", "如下的图")
            cond = " OR ".join(["text_plain LIKE ?" for _ in marks])
            rows = conn.execute(
                "SELECT no, text_plain FROM questions WHERE subject_pk=? AND media_count=0"
                " AND (" + cond + ")", [subject_pk] + ["%%%s%%" % m for m in marks]).fetchall()
            out["still_missing"] = [r[0] for r in rows]
        except sqlite3.OperationalError:
            out["still_missing"] = []
        out["unmatched_count"] = len(out["unmatched"])
        return out
    finally:
        conn.close()


def _subject_of(subject):
    """表单 → (name, grade, version, subject_pk)。

    兼容三种传法：``{"subject_pk": "physics"}``、``{"name": "物理", ...}``、
    甚至直接把 subject_pk 字符串当 subject 传进来（``import_zip(..., subject="physics")``）。
    """
    if isinstance(subject, str):
        sub = {"subject_pk": subject.strip()} if subject.strip() else {}
    else:
        sub = dict(subject or {})
    name = sub.get("name") or "数学"
    grade = sub.get("grade") or "七年级上"
    version = sub.get("version") or "沪科版"
    subject_pk = str(sub.get("subject_pk") or "").strip()
    if subject_pk and not sub.get("name"):
        # 只给了 subject_pk：别再按默认「数学/七年级上」把它算成 math7a
        return name, grade, version, subject_pk
    subject_pk = subject_pk or _subject_pk(name, grade, version)
    return name, grade, version, subject_pk


def import_zip(path, kind=None, *, db_path=None, subject=None, report_path=None,
               dry_run=False, renumber=False):
    """ZIP 导入（t88）：包内 = 题目文件（json/csv/xlsx）+ ``images/`` 目录。

    * 也可以只传 ``images.zip``（没有题目文件）→ 只挂图（"先导题、再传图"两步式）；
    * 图片文件名以题号开头（``00004.png`` / ``00004_2.jpg``），按 seq 排序落盘；
    * 报告里给：图片 N 张 / 未匹配 M 个（列文件名）/ 仍缺图 K 道（列题号）。
    """
    import shutil
    import tempfile
    import zipfile

    path = os.path.abspath(path)
    if not zipfile.is_zipfile(path):
        raise ValueError("这不是有效的 ZIP 包（文件损坏或不是 zip）")
    name, grade, version, subject_pk = _subject_of(subject)
    db_path = db_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
    report_path = report_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
        "import_report.json")
    started = _now()
    tmp = tempfile.mkdtemp(prefix="import_zip_")
    try:
        try:
            with zipfile.ZipFile(path) as z:
                z.extractall(tmp)
        except Exception as exc:  # noqa: BLE001 - 坏包给人话
            raise ValueError("ZIP 里的文件损坏：%s" % exc)
        item_file = None
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in (".json", ".csv", ".xlsx", ".xlsm"):
                    item_file = os.path.join(root, fn)
                    break
            if item_file:
                break
        images = []
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in FIGURE_EXTS:
                    with open(os.path.join(root, fn), "rb") as f:
                        images.append((fn, f.read()))
        if not item_file and not images:
            raise ValueError("ZIP 里既没有题目文件（json/csv/xlsx），也没有图片（images/）")
        sub = {"name": name, "grade": grade, "version": version, "subject_pk": subject_pk}
        report = None
        if item_file:
            report = import_file(item_file, guess_kind(item_file), db_path=db_path,
                                 subject=sub, report_path=report_path, dry_run=dry_run,
                                 renumber=renumber)
            # 导入报告里的学科才是题真正落库的学科（防 name/grade 推错成 math7a）
            subject_pk = report.get("subject_pk") or subject_pk
        else:
            report = {"generated_at": started, "format": "zip", "source": path,
                      "dry_run": bool(dry_run), "subject_pk": subject_pk,
                      "rows": 0, "new": 0, "updated": 0, "skipped": 0, "failed": 0,
                      "errors": [], "items": [], "subject": {"name": name, "grade": grade,
                                                             "version": version},
                      "by_subject": {}}
        figs = attach_figures(db_path, subject_pk, images) if not dry_run else {
            "saved": 0, "skipped": 0, "updated": 0, "resized": 0, "unmatched": [],
            "unmatched_count": len(images), "still_missing": [], "bysubject": subject_pk}
        report.update({
            "format": "zip", "source": path, "dry_run": bool(dry_run),
            "has_item_file": bool(item_file),
            "figures_saved": figs["saved"], "figures_skipped": figs["skipped"],
            "figures_updated": figs["updated"],
            "figures_unmatched": figs["unmatched"],
            "figures_unmatched_count": figs["unmatched_count"],
            "figures_still_missing": figs["still_missing"],
        })
        report["inserted"] = report.get("new", 0)
        if report_path:
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
        return report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_file(path, kind=None, *, db_path=None, subject=None, report_path=None,
                dry_run=False, renumber=False):
    """导入一个文件。kind 为空时按扩展名判断。

    返回报告 dict（同时写 `report_path`，默认 `data/import_report.json`）。
    ``renumber=True``：题号撞别的学科时按本学科号段自动改号（报告列「原号 → 新号」）。
    """
    from . import jsonfile, tabular   # noqa: F401 - 保持与 read_source 一致的导出

    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    kind = guess_kind(path, kind)
    if kind == "zip":
        return import_zip(path, kind, db_path=db_path, subject=subject,
                          report_path=report_path, dry_run=dry_run, renumber=renumber)
    if kind not in ("json", "excel", "csv"):
        raise ValueError("不支持的格式：%r（支持的格式：csv / excel / json / docx / zip）"
                         % (kind or path))

    started = _now()
    meta, raw_items = read_source(path, kind)
    sub = dict(subject or {})
    name = sub.get("name") or (meta or {}).get("subject") or "数学"
    grade = sub.get("grade") or (meta or {}).get("grade") or "七年级上"
    version = sub.get("version") or (meta or {}).get("version") or "沪科版"
    subject_pk = sub.get("subject_pk") or _subject_pk(name, grade, version)
    db_path = db_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
    report_path = report_path or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data",
        "import_report.json")

    conn = dbmod.connect(db_path)
    try:
        dbmod.ensure_subject(conn, subject_pk, name, grade, version)
        items, errors = normalize_rows(raw_items, subject_pk)
        if dry_run:
            res = {"new": 0, "updated": 0, "skipped": 0, "rejected": 0,
                   "conflicts": [], "renumbered": [], "items": []}
        else:
            res = upsert_items(conn, items, subject_pk, renumber=renumber)
        report = {
            "generated_at": started,
            "format": kind,
            "source": path,
            "dry_run": bool(dry_run),
            "subject_pk": subject_pk,
            "subject": {"name": name, "grade": grade, "version": version},
            "rows": len(raw_items),
            "new": res.get("new", 0),
            "updated": res.get("updated", 0),
            "skipped": res.get("skipped", 0),
            "rejected": res.get("rejected", 0),
            "conflicts": res.get("conflicts", []),
            "renumbered": res.get("renumbered", []),
            "failed": len(errors),
            "errors": errors,
            "items": res.get("items", []),
            "by_subject": {subject_pk: {"inserted": res.get("new", 0),
                                        "updated": res.get("updated", 0),
                                        "skipped": res.get("skipped", 0)}},
            "inserted": res.get("new", 0),
        }
        # t87：这批题里「题干说如图但没有图」的数量与题号（组卷会被跳过）
        try:
            marks = ("如图", "见图", "图中", "下图", "上图", "图①", "图②", "如下的图")
            nos = [it["no"] for it in items if it.get("no")]
            if nos and not dry_run:
                cond = " OR ".join(["text_plain LIKE ?" for _ in marks])
                rows = conn.execute(
                    "SELECT no FROM questions WHERE no IN (%s) AND media_count=0 AND (%s)"
                    % (",".join("?" * len(nos)), cond),
                    nos + ["%%%s%%" % m for m in marks]).fetchall()
                miss = [r[0] for r in rows]
            else:
                miss = [it["no"] for it in items
                        if any(m in (it.get("stem") or "") for m in marks)]
        except sqlite3.OperationalError:
            miss = []
        report["fig_missing"] = len(miss)
        report["fig_missing_nos"] = miss[:50]
        if report_path:
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
        return report
    finally:
        conn.close()
