# -*- coding: utf-8 -*-
"""从 app.db 反向导出 questions.jsonl（build_db 的中间产物）。

背景：2026-11-01 清理目录时把 `qbank/dedup/data/questions.jsonl`（docx 抽取中间件）
一起删了，`build_db.py` 与一批测试都依赖它。这里用 already-built 的 app.db
（12,280 行真实数据）把它重建出来，字段与 build_db 消费的一致：
`no / answer / text / src_file / kp_id / chapter`。

用法：python tools/dump_jsonl.py [--db data/app.db] [--out <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
os.chdir(APP)

import build_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(APP, "data", "app.db"))
    ap.add_argument("--out", default=build_db.JSONL)
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print("找不到数据库：%s" % args.db)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(questions)")}

    def col(name, default=""):
        return name if name in cols else "NULL AS " + name

    rows = conn.execute(
        "SELECT no, qtype, answer_raw, text_plain, src_title, kp_id, chapter, "
        "option_count, sub_count FROM questions ORDER BY no").fetchall()
    n = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            rec = {
                "no": r["no"],
                "qtype": r["qtype"],
                "answer": r["answer_raw"] or "",
                "text": r["text_plain"] or "",
                "src_file": r["src_title"] or "",
                "kp_id": r["kp_id"] or "",
                "chapter": r["chapter"] or "",
                "option_count": r["option_count"],
                "sub_count": r["sub_count"],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    conn.close()
    size = os.path.getsize(args.out)
    print(f"导出 {n} 行 → {args.out}（{size/1024/1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
