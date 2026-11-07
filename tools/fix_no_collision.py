# -*- coding: utf-8 -*-
"""修复「跨学科题号覆盖」造成的 21 道数学题丢失。

背景：questions.no 是全局主键；物理卷导入用 00001..00023，与 math7a 的 00001..00023 撞号，
importers 按「同号即更新」把它们改成了物理题 → 21 道数学题被覆盖（备份里有原件）。

步骤（默认 dry-run，--apply 才写）：
1. 备份当前库
2. 物理 00001..00023 改号到 99001..99023（避开真题号段与示例号段）
3. 从备份恢复 math7a 的 00001..00023 原始行（含 tokens/kp/chapter/answer 全字段）
4. 校验：math7a 题数回到 18086、物理 23 道、两科题号段不再重叠
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time

DB = r"E:\OCR\app\data\app.db"
BK = r"E:\OCR\app\data\backups\app-before-nonmath-delete-20261107-110752.db"
APPLY = "--apply" in sys.argv

COLS = ["no", "qtype", "option_count", "sub_count", "is_empty", "media_count", "text_len",
        "text_plain", "tokens_json", "fmt", "src_title", "src_para", "family_key", "kp_id",
        "chapter", "kp", "kp_source", "answer_raw", "check_status", "subject_pk"]

cur = sqlite3.connect(DB)
cur.row_factory = sqlite3.Row
bak = sqlite3.connect(BK)
bak.row_factory = sqlite3.Row

print("=== 变更前 ===")
for r in cur.execute("SELECT subject_pk, COUNT(*) n, MIN(no) mn, MAX(no) mx FROM questions "
                     "GROUP BY subject_pk ORDER BY n DESC"):
    print(f"  {r['subject_pk']:10s} {r['n']:6d}  {r['mn']}..{r['mx']}")

phys_nos = [f"{i:05d}" for i in range(1, 24)]
to_restore = []
for no in phys_nos:
    old = bak.execute("SELECT * FROM questions WHERE no=? AND subject_pk='math7a'", (no,)).fetchone()
    now = cur.execute("SELECT subject_pk FROM questions WHERE no=?", (no,)).fetchone()
    if old is not None and now is not None and now["subject_pk"] == "physics":
        to_restore.append(dict(old))

print(f"\n待恢复的 math7a 题: {len(to_restore)} 道")
print(f"物理题改号: 00001..00023 → 99001..99023")

if not APPLY:
    print("\n[DRY-RUN] 未写入。加 --apply 执行。")
    cur.close()
    bak.close()
    raise SystemExit(0)

ts = time.strftime("%Y%m%d-%H%M%S")
backup = os.path.join(r"E:\OCR\app\data\backups", f"app-before-no-fix-{ts}.db")
shutil.copy2(DB, backup)
print(f"\n已备份当前库: {backup}")

try:
    cur.execute("BEGIN")
    # 1) 物理改号
    for i in range(1, 24):
        old_no, new_no = f"{i:05d}", f"{99000 + i:05d}"
        cur.execute("UPDATE questions SET no=? WHERE no=? AND subject_pk='physics'", (new_no, old_no))
        for tbl in ("quality_flags", "mastery"):
            try:
                cur.execute(f"UPDATE {tbl} SET no=? WHERE no=?", (new_no, old_no))
            except sqlite3.OperationalError:
                pass
    # 2) 恢复数学原行
    for row in to_restore:
        cols = ",".join(COLS)
        ph = ",".join("?" * len(COLS))
        cur.execute(f"INSERT OR REPLACE INTO questions ({cols}) VALUES ({ph})",
                    [row[c] for c in COLS])
    # 3) 清理可能残留的 dup_groups 孤儿
    try:
        cur.execute("DELETE FROM dup_groups WHERE no NOT IN (SELECT no FROM questions)")
    except sqlite3.OperationalError:
        pass
    cur.commit()
    print("事务提交完成")
except Exception:
    cur.rollback()
    print("出错已回滚")
    raise

print("\n=== 变更后 ===")
for r in cur.execute("SELECT subject_pk, COUNT(*) n, MIN(no) mn, MAX(no) mx FROM questions "
                     "GROUP BY subject_pk ORDER BY n DESC"):
    print(f"  {r['subject_pk']:10s} {r['n']:6d}  {r['mn']}..{r['mx']}")

overlap = cur.execute("SELECT COUNT(*) FROM questions a JOIN questions b "
                      "ON a.no=b.no AND a.subject_pk<>b.subject_pk").fetchone()[0]
print("\n跨学科同题号残留:", overlap)
sample = cur.execute("SELECT no, subject_pk, substr(text_plain,1,20) h FROM questions "
                     "WHERE no IN ('00001','99001') ORDER BY no").fetchall()
for r in sample:
    print(f"  {r['no']} {r['subject_pk']:10s} {r['h']!r}")
cur.close()
bak.close()
