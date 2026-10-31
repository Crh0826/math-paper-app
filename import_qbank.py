# -*- coding: utf-8 -*-
"""t51 · 题库导入命令行。

    python import_qbank.py --from json  data/qbank.json
    python import_qbank.py --from excel data/qbank.xlsx --subject 语文 --grade 七年级上 --version 人教版
    python import_qbank.py --from csv   data/qbank.csv  --dry-run
    python import_qbank.py --from docx  E:\\OCR            # 复用 build_db 的 docx 抽取
    python import_qbank.py --list-subjects

统一写 `data/import_report.json`（新增/更新/跳过/失败 + 原因）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import importers  # noqa: E402


def _human(rep):
    lines = [
        "导入完成：%s" % os.path.basename(rep["source"]),
        "  学科：%s（%s）  subject_pk=%s" % (
            rep["subject"]["name"],
            " ".join(x for x in (rep["subject"]["grade"], rep["subject"]["version"]) if x),
            rep["subject_pk"]),
        "  格式：%s   总行数 %d" % (rep["format"], rep["rows"]),
        "  新增 %d / 更新 %d / 跳过 %d / 失败 %d" % (
            rep["new"], rep["updated"], rep["skipped"], rep["failed"]),
    ]
    for err in rep["errors"][:10]:
        lines.append("  ✗ %s" % err["reason"])
    if len(rep["errors"]) > 10:
        lines.append("  … 另有 %d 条错误见报告" % (len(rep["errors"]) - 10))
    lines.append("  报告：%s" % rep["report_path"])
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="import_qbank.py", description="题库导入（JSON/Excel/CSV/docx）")
    ap.add_argument("--from", dest="kind", choices=("json", "excel", "csv", "docx"),
                    help="来源格式；docx 复用 build_db 抽取")
    ap.add_argument("--file", dest="path", help="文件路径（docx 时是目录）")
    ap.add_argument("--db", dest="db_path", default=None, help="目标库，默认 data/app.db")
    ap.add_argument("--report", dest="report_path", default=None,
                    help="导入报告路径，默认 data/import_report.json")
    ap.add_argument("--subject", default=None, help="学科名，默认取文件里的 subject 或「数学」")
    ap.add_argument("--grade", default=None, help="年级，默认取文件里的 grade 或「七年级上」")
    ap.add_argument("--version", default=None, help="教材版本，默认取文件里的 version 或「沪科版」")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库（仍打印统计）")
    ap.add_argument("--fresh", action="store_true", help="docx 导入时重建库")
    ap.add_argument("--limit", type=int, default=0, help="docx 导入时限量（调试用）")
    ap.add_argument("--list-subjects", action="store_true", help="列出库里的学科")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（给程序用）")
    a = ap.parse_args(argv)

    if a.list_subjects:
        subs = importers.list_subjects(a.db_path)
        print(json.dumps(subs, ensure_ascii=False, indent=2) if a.json
              else "\n".join("%-12s %s %s %s" % (s["subject_pk"], s["name"], s["grade"], s["version"])
                             for s in subs))
        return 0

    if not a.kind or not a.path:
        ap.error("需要 --from 与 --file（或用 --list-subjects）")

    if a.kind == "docx":
        res = importers.import_docx(a.path, db_path=a.db_path, fresh=a.fresh,
                                    limit=a.limit, report_path=a.report_path)
        print(json.dumps(res, ensure_ascii=False, indent=2) if a.json
              else "docx 导入完成：questions=%s policy=%s" % (res.get("questions"), res.get("policy")))
        return 0

    subject = {}
    for key in ("subject", "grade", "version"):
        val = getattr(a, key)
        if val:
            subject["name" if key == "subject" else key] = val
    rep = importers.import_file(a.path, a.kind, db_path=a.db_path, subject=subject,
                                report_path=a.report_path, dry_run=a.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else _human(rep))
    return 0 if rep["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
