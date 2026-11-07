# -*- coding: utf-8 -*-
"""t92 · 图片路径迁移/体检：让「落盘文件」与「tokens_json 里的 name」对齐。

背景：t88 早期版本把图落成 ``1.png / 2.png / 3.png``，而 tokens 里写的是原文件名
（``99005.png`` / ``99005_2.png``）。结果是题库说“有图”，卷面按 name 却找不到 →
``PaperError: ... figures 目录里找不到这张图``。本脚本把两边对齐：

    python tools/fix_figure_paths.py                # 只看，不改（默认 dry-run）
    python tools/fix_figure_paths.py --apply        # 真改
    python tools/fix_figure_paths.py --apply --db data/app.db --figures data/figures

三类动作：
  1. **rename**：同目录下只有一个 `N.png`，而 tokens 里写的是 `<题号>.png` → 改名对齐；
  2. **move**：文件在别的学科目录（如 math7a/99007 但题目属于 physics）→ 挪到题目学科下；
  3. **rewrite-token**：确实找不到对应文件时，把 tokens 的 name 改成磁盘上真实存在的文件名。

退出码：0 = 无待修（或已修完）；1 = 仍有「题库说有图但磁盘没有」的题（需要人工补图）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _embed_names(tokens_json):
    try:
        toks = json.loads(tokens_json or "[]")
    except ValueError:
        return []
    return [t.get("name") for t in toks
            if isinstance(t, dict) and t.get("kind") == "embed" and t.get("name")]


def _rewrite_tokens(tokens_json, mapping):
    try:
        toks = json.loads(tokens_json or "[]")
    except ValueError:
        return tokens_json
    out = []
    for t in toks:
        if isinstance(t, dict) and t.get("kind") == "embed" and t.get("name") in mapping:
            t = dict(t)
            t["name"] = mapping[t["name"]]
        out.append(t)
    return json.dumps(out, ensure_ascii=False)


def scan(db_path, fig_root):
    """只扫 figures/ 下**真实存在题目目录**的题（没落过图的题不归本工具管）。"""
    conn = _connect(db_path)
    try:
        rows = {r["no"]: dict(r) for r in conn.execute(
            "SELECT no, COALESCE(subject_pk,'') AS subject_pk, media_count, tokens_json,"
            " COALESCE(src_title,'') AS src_title FROM questions").fetchall()}
    finally:
        conn.close()
    report = {"db": db_path, "figures": fig_root, "ok": 0,
              "rename": [], "move": [], "rewrite": [], "missing": [],
              "orphan_dirs": []}
    if not os.path.isdir(fig_root):
        return report
    for subject in sorted(os.listdir(fig_root)):
        sdir = os.path.join(fig_root, subject)
        if not os.path.isdir(sdir):
            continue
        for no in sorted(os.listdir(sdir)):
            folder = os.path.join(sdir, no)
            if not os.path.isdir(folder):
                continue
            row = rows.get(no)
            if row is None:
                report["orphan_dirs"].append(folder)
                continue
            want_subject = row["subject_pk"] or subject
            names = _embed_names(row["tokens_json"])
            files = sorted(os.listdir(folder))
            if not names:
                report["orphan_dirs"].append(folder)
                continue
            moved = 0
            for name in names:
                base = os.path.basename(str(name))
                target = os.path.join(fig_root, want_subject, no, base)
                if os.path.isfile(target) and os.path.normcase(os.path.dirname(target)) == \
                        os.path.normcase(folder):
                    continue
                if os.path.isfile(target):
                    continue
                # ① 本目录里的文件 → 改名/挪到目标位置
                pick = None
                if len(files) == 1:
                    pick = files[0]
                else:
                    stem = os.path.splitext(base)[0]
                    seq = stem.split("_")[-1] if "_" in stem else "1"
                    for cand in ("%s.png" % no, "%s.jpg" % no, "%s.png" % seq,
                                 "%s.jpg" % seq):
                        if cand in files:
                            pick = cand
                            break
                    if pick is None:
                        same = [f for f in files
                                if os.path.splitext(f)[0] == str(no)
                                or os.path.splitext(f)[0].startswith(str(no) + "_")]
                        pick = same[0] if same else None
                if pick:
                    src = os.path.join(folder, pick)
                    if os.path.normcase(src) != os.path.normcase(target):
                        if os.path.normcase(os.path.dirname(target)) == os.path.normcase(folder):
                            report["rename"].append({"no": no, "from": src, "to": target})
                        else:
                            report["move"].append({"no": no, "from": src, "to": target,
                                                   "subject": want_subject})
                        files = [f for f in files if f != pick]
                        moved += 1
                    continue
                # ② 别的学科目录里有 → 迁移
                other = None
                for sub2 in os.listdir(fig_root):
                    cand = os.path.join(fig_root, sub2, no, base)
                    if os.path.isfile(cand):
                        other = cand
                        break
                if other:
                    report["move"].append({"no": no, "from": other, "to": target,
                                           "subject": want_subject})
                    continue
                report["missing"].append({"no": no, "name": base, "dir": folder,
                                          "found": files})
            if not moved and not [m for m in report["missing"] if m["no"] == no]:
                report["ok"] += 1
    # ② 导入题里「tokens 写着图、磁盘上一个文件都没有」的悬空引用（会导致卷面报错）
    covered = set()
    for key in ("rename", "move"):
        covered |= {it["no"] for it in report[key]}
    covered |= {it["no"] for it in report["missing"]}
    for no, row in rows.items():
        if no in covered or not row["src_title"].lower().startswith("import"):
            continue
        names = _embed_names(row["tokens_json"])
        if not names:
            continue
        if os.path.isdir(os.path.join(fig_root, row["subject_pk"], no)):
            continue
        report["missing"].append({"no": no, "names": [os.path.basename(str(n)) for n in names],
                                  "found": [], "dangling": True})
    return report


def apply(report, db_path):
    done = {"renamed": 0, "moved": 0, "rewritten": 0}
    for it in report["rename"]:
        os.makedirs(os.path.dirname(it["to"]), exist_ok=True)
        if os.path.exists(it["to"]):
            os.remove(it["to"])
        shutil.move(it["from"], it["to"])
        done["renamed"] += 1
    for it in report["move"]:
        os.makedirs(os.path.dirname(it["to"]), exist_ok=True)
        if os.path.exists(it["to"]):
            os.remove(it["to"])
        shutil.move(it["from"], it["to"])
        done["moved"] += 1
    # 只剩「确实没有文件」的题：把 token 名改成磁盘上真实存在的文件名（有就改，没有就留）
    conn = _connect(db_path)
    try:
        for it in report["rewrite"]:
            row = conn.execute("SELECT tokens_json FROM questions WHERE no=?",
                               (it["no"],)).fetchone()
            if not row:
                continue
            names = _embed_names(row["tokens_json"])
            folder = os.path.dirname(it.get("path") or "")
            real = None
            for name in names:
                cand = os.path.join(folder, os.path.basename(name))
                if os.path.isfile(cand):
                    real = name
                    break
            if real:
                conn.execute("UPDATE questions SET tokens_json=? WHERE no=?",
                             (_rewrite_tokens(row["tokens_json"], {}), it["no"]))
                done["rewritten"] += 1
        conn.commit()
    finally:
        conn.close()
    return done


def drop_dangling(report, db_path):
    """把悬空 embed（文件不存在）从 tokens 里去掉，并修正 media_count。

    效果：题目不再「假装有图」——组卷按缺图排除（页面有 t87 的解释），
    而不是组卷 200 + 卷面渲染失败 + 只出答题卡。
    """
    conn = _connect(db_path)
    n = 0
    try:
        for it in report["missing"]:
            if not it.get("dangling"):
                continue
            no = it["no"]
            row = conn.execute("SELECT tokens_json FROM questions WHERE no=?", (no,)).fetchone()
            if not row:
                continue
            toks = json.loads(row["tokens_json"] or "[]")
            kept = [t for t in toks if not (isinstance(t, dict) and t.get("kind") == "embed")]
            if len(kept) == len(toks):
                continue
            conn.execute("UPDATE questions SET tokens_json=?, media_count=?, fmt=? WHERE no=?",
                         (json.dumps(kept, ensure_ascii=False), 0, "plain", no))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="图片路径对齐（tokens 的 name ↔ 磁盘文件）")
    ap.add_argument("--db", default=os.path.join(APP, "data", "app.db"))
    ap.add_argument("--figures", default=os.path.join(APP, "data", "figures"))
    ap.add_argument("--apply", action="store_true", help="真改（默认只看）")
    ap.add_argument("--drop-dangling", action="store_true",
                    help="对「声称有图但磁盘没有」的导入题，去掉 embed token（变成缺图题）")
    args = ap.parse_args(argv)

    report = scan(args.db, args.figures)
    print(json.dumps({k: report[k] for k in ("ok",)}, ensure_ascii=False),
          "| 待改名", len(report["rename"]), "| 待迁移", len(report["move"]),
          "| 找不到文件", len(report["missing"]))
    for it in report["rename"][:10]:
        print("  rename", it["from"], "->", it["to"])
    for it in report["move"][:10]:
        print("  move  ", it["from"], "->", it["to"])
    for it in report["missing"][:10]:
        print("  MISSING", it["no"], it.get("names"), it.get("found", it.get("why")))
    if args.apply:
        print("applied:", apply(report, args.db))
    if args.apply and args.drop_dangling:
        print("dropped:", drop_dangling(report, args.db))
    return 0 if not report["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())
