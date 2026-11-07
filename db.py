# -*- coding: utf-8 -*-
"""M0 · SQLite 数据层（建表 + 连接）。数据库可随时重建，唯一事实来源仍是 22,898 份 docx。"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- t51 · 多学科：一个库可以装多套教材/学科；老库默认全部归到 math7a（数学·七年级上·沪科版）
CREATE TABLE IF NOT EXISTS subjects (
  subject_pk  TEXT PRIMARY KEY,
  name        TEXT NOT NULL,          -- 学科：数学 / 语文 / 英语…
  grade       TEXT DEFAULT '',        -- 年级：七年级上
  version     TEXT DEFAULT '',        -- 教材版本：沪科版 / 人教版
  created_at  TEXT,
  sort_order  INTEGER DEFAULT 0       -- t69：顺序管理页调出来的显示顺序（0/全 0 = 原顺序）
);

-- t69 · 单元（章节）顺序：老师按自己讲课次序摆单元；没记录的就按原 SQL 顺序排在后面
CREATE TABLE IF NOT EXISTS chapter_order (
  subject_pk TEXT NOT NULL,
  chapter    TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(subject_pk, chapter)
);

CREATE TABLE IF NOT EXISTS questions (
  no            TEXT PRIMARY KEY,
  subject_pk    TEXT DEFAULT 'math7a',
  qtype         TEXT NOT NULL,
  option_count  INTEGER DEFAULT 0,
  sub_count     INTEGER DEFAULT 0,
  is_empty      INTEGER DEFAULT 0,
  media_count   INTEGER DEFAULT 0,
  text_len      INTEGER DEFAULT 0,
  text_plain    TEXT DEFAULT '',
  tokens_json   TEXT DEFAULT '[]',
  fmt           TEXT DEFAULT '',
  src_title     TEXT DEFAULT '',
  src_para      INTEGER,
  family_key    TEXT,
  kp_id         TEXT,
  chapter       TEXT,
  kp            TEXT,
  kp_source     TEXT,
  answer_raw    TEXT DEFAULT '',
  check_status  TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_q_kp     ON questions(kp_id);
CREATE INDEX IF NOT EXISTS idx_q_chap   ON questions(chapter);
CREATE INDEX IF NOT EXISTS idx_q_qtype  ON questions(qtype);
CREATE INDEX IF NOT EXISTS idx_q_family ON questions(family_key);
CREATE INDEX IF NOT EXISTS idx_q_empty  ON questions(is_empty);

CREATE TABLE IF NOT EXISTS families (
  family_key TEXT PRIMARY KEY,
  src_title  TEXT,
  example_no TEXT,
  size       INTEGER DEFAULT 0,
  kp_id      TEXT
);

CREATE TABLE IF NOT EXISTS kps (
  kp_id       TEXT PRIMARY KEY,
  subject_pk  TEXT DEFAULT 'math7a',
  chapter     TEXT,
  kp          TEXT,
  n_questions INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS papers (
  pid         TEXT PRIMARY KEY,
  created_at  TEXT,
  title       TEXT,
  params_json TEXT,
  manifest    TEXT,
  docx_path   TEXT,
  pdf_path    TEXT,
  n_items     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS submissions (
  sid         TEXT PRIMARY KEY,
  pid         TEXT,
  created_at  TEXT,
  image_path  TEXT,
  warp_ok     INTEGER DEFAULT 0,
  qr_ok       INTEGER DEFAULT 0,
  score       REAL,
  full        REAL,
  detail_json TEXT
);

-- t6 · 掌握度（错题训练用）：key 就是题号 no。
-- 对的次数越多间隔越长（1→3→7→15 天），错了回到 1 天；plan() 优先挑 due_at 已到期的题。
CREATE TABLE IF NOT EXISTS mastery (
  key           TEXT PRIMARY KEY,
  attempts      INTEGER DEFAULT 0,
  correct       INTEGER DEFAULT 0,
  last_seen     TEXT,
  interval_days INTEGER DEFAULT 0,
  due_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_m_due ON mastery(due_at);

-- t16 · 题库质量体检结果（quality.py 写入）：flags 是逗号分隔的多值串，
-- detail 是 JSON 证据（每个 flag 一条人话说明 + 片段），供组卷/验收查询。
CREATE TABLE IF NOT EXISTS quality_flags (
  no     TEXT PRIMARY KEY,
  flags  TEXT DEFAULT '',
  detail TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_qf_flags ON quality_flags(flags);

-- t23 · 近重复分组（dedupe.py 写入）：同一 group_id 的题互为近重复（MinHash，J≥0.85）。
-- rep_no 是该组里「最相似代表题号」，人工核对时看得懂的那个锚点。
CREATE TABLE IF NOT EXISTS dup_groups (
  group_id INTEGER,
  no       TEXT PRIMARY KEY,
  size     INTEGER DEFAULT 0,
  rep_no   TEXT
);

CREATE INDEX IF NOT EXISTS idx_dg_group ON dup_groups(group_id);

-- t53 · 导入日志（Web/CLI 每次导入记一行，便于追溯「谁什么时候导了什么」）
-- t90 · 题号号段注册表：跨学科导入时按学科固定号段分配题号，避免与其它学科撞主键
CREATE TABLE IF NOT EXISTS subject_no_block (
  subject_pk TEXT PRIMARY KEY,
  prefix     TEXT NOT NULL,
  max_used   INTEGER NOT NULL DEFAULT 0
);

-- t90 · 内容指纹 → 已分配题号（同一批题重复导入得同一个号，幂等不靠原题号）
CREATE TABLE IF NOT EXISTS import_no_map (
  subject_pk  TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  no          TEXT NOT NULL,
  PRIMARY KEY(subject_pk, fingerprint)
);

CREATE TABLE IF NOT EXISTS import_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ran_at      TEXT,
  source      TEXT,
  format      TEXT,
  subject_pk  TEXT,
  total       INTEGER DEFAULT 0,
  inserted    INTEGER DEFAULT 0,
  updated     INTEGER DEFAULT 0,
  skipped     INTEGER DEFAULT 0,
  failed      INTEGER DEFAULT 0,
  dry_run     INTEGER DEFAULT 0,
  elapsed_s   REAL DEFAULT 0,
  report_json TEXT
);
"""


DEFAULT_SUBJECT = "math7a"


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：补 subject_pk 列 + 默认学科行（幂等）。"""
    for table in ("questions", "kps"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if cols and "subject_pk" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN subject_pk TEXT DEFAULT 'math7a'" % table)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subjects)")}
    if cols and "sort_order" not in cols:
        conn.execute("ALTER TABLE subjects ADD COLUMN sort_order INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_subject ON questions(subject_pk)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kp_subject ON kps(subject_pk)")
    conn.execute(
        "INSERT OR IGNORE INTO subjects(subject_pk, name, grade, version, created_at)"
        " VALUES(?,?,?,?,datetime('now'))",
        (DEFAULT_SUBJECT, "数学", "七年级上", "沪科版"))


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def ensure_subject(conn: sqlite3.Connection, subject_pk: str, name: str,
                   grade: str = "", version: str = "") -> str:
    """登记（或更新）一个学科，返回 subject_pk。"""
    conn.execute(
        "INSERT INTO subjects(subject_pk, name, grade, version, created_at)"
        " VALUES(?,?,?,?,datetime('now'))"
        " ON CONFLICT(subject_pk) DO UPDATE SET name=excluded.name,"
        " grade=excluded.grade, version=excluded.version",
        (subject_pk, name, grade, version))
    return subject_pk


def log_import_run(conn: sqlite3.Connection, report: dict) -> None:
    """把一次导入的结果记进 import_runs（失败也不影响导入本身）。"""
    import json as _json

    conn.execute(
        "INSERT INTO import_runs(ran_at, source, format, subject_pk, total, inserted,"
        " updated, skipped, failed, dry_run, elapsed_s, report_json)"
        " VALUES(datetime('now'), ?,?,?,?,?,?,?,?,?,?,?)",
        (str(report.get("source") or report.get("db_path") or ""),
         str(report.get("format") or ""), str(report.get("subject_pk") or ""),
         int(report.get("total") or 0), int(report.get("inserted") or 0),
         int(report.get("updated") or 0), int(report.get("skipped") or 0),
         int(report.get("failed") or 0), 1 if report.get("dry_run") else 0,
         float(report.get("elapsed_s") or 0), _json.dumps(report, ensure_ascii=False)))
    conn.commit()


#: 各学科默认号段（题号前缀 → 5 位号段，如 physics 6xxxx = 60000..69999）
NO_BLOCK_DEFAULTS = {
    "physics": "6", "chemistry": "7", "chinese": "3", "english": "4",
    "history": "5", "politics": "8", "geography": "9", "biology": "2",
}


def no_block_prefix(conn, subject_pk: str) -> str:
    """该学科的号段前缀：注册表里有就用它，否则按学科名/科目表推断。"""
    try:
        row = conn.execute("SELECT prefix FROM subject_no_block WHERE subject_pk=?",
                           (subject_pk,)).fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.OperationalError:
        pass
    try:
        name = conn.execute("SELECT name FROM subjects WHERE subject_pk=?",
                            (subject_pk,)).fetchone()
    except sqlite3.OperationalError:
        name = None
    import re as _re
    text = (name[0] if name else "") + " " + str(subject_pk)
    for key, prefix in NO_BLOCK_DEFAULTS.items():
        if key in text.lower():
            return prefix
    cn = {"物理": "6", "化学": "7", "语文": "3", "英语": "4", "历史": "5",
          "道法": "8", "政治": "8", "地理": "9", "生物": "2"}
    for word, prefix in cn.items():
        if word in text:
            return prefix
    if str(subject_pk).startswith("math"):
        return "9"            # 数学导入件默认 9xxxx（不动原库的 0xxxx）
    return "1"                # 其它学科兜底号段


def ensure_no_block(conn, subject_pk: str, prefix: str = "") -> dict:
    """登记/读取某学科的号段（幂等）。"""
    prefix = str(prefix or no_block_prefix(conn, subject_pk))
    conn.execute("INSERT INTO subject_no_block(subject_pk, prefix, max_used)"
                 " VALUES(?,?,0) ON CONFLICT(subject_pk) DO NOTHING", (subject_pk, prefix))
    conn.commit()
    row = conn.execute("SELECT subject_pk, prefix, max_used FROM subject_no_block"
                       " WHERE subject_pk=?", (subject_pk,)).fetchone()
    return {"subject_pk": row[0], "prefix": row[1], "max_used": int(row[2] or 0)}


def alloc_subject_no(conn, subject_pk: str, fingerprint: str) -> str:
    """给「该学科 + 该内容指纹」分配题号：已分配过就返回同一个号（幂等）。"""
    hit = conn.execute("SELECT no FROM import_no_map WHERE subject_pk=? AND fingerprint=?",
                       (subject_pk, str(fingerprint))).fetchone()
    if hit:
        return str(hit[0])
    block = ensure_no_block(conn, subject_pk)
    prefix = str(block["prefix"])
    nxt = max(int(block["max_used"]), 0) + 1
    # 跳过已被占用的号（其它学科历史数据 / 手工号）
    while True:
        no = "%s%04d" % (prefix, nxt)
        taken = conn.execute("SELECT 1 FROM questions WHERE no=?", (no,)).fetchone()
        if not taken:
            break
        nxt += 1
    conn.execute("UPDATE subject_no_block SET max_used=? WHERE subject_pk=?", (nxt, subject_pk))
    conn.execute("INSERT OR REPLACE INTO import_no_map(subject_pk, fingerprint, no)"
                 " VALUES(?,?,?)", (subject_pk, str(fingerprint), no))
    conn.commit()
    return no


def list_subjects_ordered(conn: sqlite3.Connection) -> list:
    """学科按人工顺序（sort_order）→ 没排过的按 subject_pk，保证与老行为一致。"""
    try:
        return conn.execute(
            "SELECT subject_pk, name, grade, version, created_at,"
            " COALESCE(sort_order, 0) AS sort_order FROM subjects"
            " ORDER BY COALESCE(sort_order, 0), subject_pk").fetchall()
    except sqlite3.OperationalError:
        return []


def _subject_exists(conn, subject_pk: str) -> bool:
    try:
        if conn.execute("SELECT 1 FROM subjects WHERE subject_pk=?", (subject_pk,)).fetchone():
            return True
        return bool(conn.execute("SELECT 1 FROM questions WHERE subject_pk=? LIMIT 1",
                                 (subject_pk,)).fetchone())
    except sqlite3.OperationalError:
        return False


def list_chapters_ordered(conn: sqlite3.Connection, subject_pk: str) -> list:
    """该学科的单元列表：已排过序的在前（按 sort_order），其余按章节名追加在后。"""
    try:
        rows = conn.execute(
            "SELECT DISTINCT chapter FROM questions WHERE subject_pk=? AND chapter IS NOT NULL"
            " AND chapter<>'' ORDER BY chapter", (subject_pk,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    chapters = [r[0] for r in rows]
    try:
        ordered = {r[0]: int(r[1]) for r in conn.execute(
            "SELECT chapter, sort_order FROM chapter_order WHERE subject_pk=?",
            (subject_pk,))}
    except sqlite3.OperationalError:
        ordered = {}
    head = sorted([c for c in chapters if c in ordered], key=lambda c: (ordered[c], c))
    tail = [c for c in chapters if c not in ordered]
    out = [{"chapter": c, "sort_order": ordered.get(c, 0), "ordered": c in ordered}
           for c in head + tail]
    return out


def save_subject_order(conn: sqlite3.Connection, pks) -> int:
    """按给定顺序重写 subjects.sort_order（1..N）。未知 pk → ValueError。"""
    pks = [str(p).strip() for p in (pks or []) if str(p).strip()]
    if not pks:
        raise ValueError("学科顺序不能为空")
    known = {r[0] for r in conn.execute("SELECT subject_pk FROM subjects")}
    unknown = [p for p in pks if p not in known]
    if unknown:
        raise ValueError("未知学科：%s" % "、".join(unknown[:5]))
    seen = set()
    order = []
    for p in pks:                       # 去重保序
        if p not in seen:
            seen.add(p)
            order.append(p)
    for i, pk in enumerate(order, 1):
        conn.execute("UPDATE subjects SET sort_order=? WHERE subject_pk=?", (i, pk))
    conn.commit()
    return len(order)


def save_chapter_order(conn: sqlite3.Connection, subject_pk: str, chapters) -> int:
    """按给定顺序重写某学科的单元顺序。未知学科/章节 → ValueError。"""
    subject_pk = str(subject_pk or "").strip()
    if not subject_pk:
        raise ValueError("缺少学科（subject_pk）")
    if not _subject_exists(conn, subject_pk):
        raise ValueError("未知学科：%s" % subject_pk)
    chapters = [str(c).strip() for c in (chapters or []) if str(c).strip()]
    if not chapters:
        raise ValueError("单元顺序不能为空")
    known = {r[0] for r in conn.execute(
        "SELECT DISTINCT chapter FROM questions WHERE subject_pk=?", (subject_pk,))}
    unknown = [c for c in chapters if c not in known]
    if unknown:
        raise ValueError("该学科下没有这些单元：%s" % "、".join(unknown[:5]))
    seen, order = set(), []
    for c in chapters:
        if c not in seen:
            seen.add(c)
            order.append(c)
    for i, ch in enumerate(order, 1):
        conn.execute("INSERT INTO chapter_order(subject_pk, chapter, sort_order)"
                     " VALUES(?,?,?) ON CONFLICT(subject_pk, chapter)"
                     " DO UPDATE SET sort_order=excluded.sort_order",
                     (subject_pk, ch, i))
    conn.commit()
    return len(order)


def reset_order(conn: sqlite3.Connection, subject_pk: str = None) -> dict:
    """恢复默认：清掉人工顺序（学科 sort_order 归 0、单元记录删除）。"""
    if subject_pk:
        subject_pk = str(subject_pk).strip()
        if not _subject_exists(conn, subject_pk):
            raise ValueError("未知学科：%s" % subject_pk)
        n = conn.execute("DELETE FROM chapter_order WHERE subject_pk=?", (subject_pk,)).rowcount
        conn.execute("UPDATE subjects SET sort_order=0 WHERE subject_pk=?", (subject_pk,))
        conn.commit()
        return {"subject_pk": subject_pk, "chapters_reset": int(n or 0)}
    n = conn.execute("DELETE FROM chapter_order").rowcount
    conn.execute("UPDATE subjects SET sort_order=0")
    conn.commit()
    return {"subject_pk": "", "chapters_reset": int(n or 0)}


def list_subjects(conn: sqlite3.Connection) -> list:
    try:
        return conn.execute("SELECT subject_pk, name, grade, version, created_at"
                            " FROM subjects ORDER BY subject_pk").fetchall()
    except sqlite3.OperationalError:
        return []


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
