# -*- coding: utf-8 -*-
import sqlite3

c = sqlite3.connect("file:E:/OCR/app/data/app.db?mode=ro", uri=True)
terms = ("如图", "见图", "图所示", "图中", "下图", "上图")
where = " OR ".join("text_plain LIKE ?" for _ in terms)
args = ["%" + t + "%" for t in terms]
rows = c.execute(
    "SELECT qtype, COUNT(*) FROM questions WHERE (" + where + ")"
    " AND COALESCE(media_count,0)=0 GROUP BY qtype ORDER BY 2 DESC", args).fetchall()
print("图缺媒体 按题型:", rows)
n_ans = c.execute(
    "SELECT COUNT(*) FROM questions WHERE (" + where + ") AND COALESCE(media_count,0)=0"
    " AND TRIM(COALESCE(answer_raw,''))<>''", args).fetchone()[0]
print("图缺媒体 且有答案:", n_ans)
n_sel = c.execute(
    "SELECT COUNT(*) FROM questions WHERE qtype='选择' AND TRIM(COALESCE(answer_raw,''))<>''"
).fetchone()[0]
n_sel_bad = c.execute(
    "SELECT COUNT(*) FROM questions WHERE qtype='选择' AND TRIM(COALESCE(answer_raw,''))<>''"
    " AND (" + where + ") AND COALESCE(media_count,0)=0", args).fetchone()[0]
print("选择可用池:", n_sel, "其中图缺媒体:", n_sel_bad,
      "占比 %.1f%%" % (100.0 * n_sel_bad / n_sel))
