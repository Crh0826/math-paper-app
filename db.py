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
  created_at  TEXT
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
"""


DEFAULT_SUBJECT = "math7a"


def _migrate(conn: sqlite3.Connection) -> None:
    """老库升级：补 subject_pk 列 + 默认学科行（幂等）。"""
    for table in ("questions", "kps"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if cols and "subject_pk" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN subject_pk TEXT DEFAULT 'math7a'" % table)
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
