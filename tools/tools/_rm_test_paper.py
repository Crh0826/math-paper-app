# -*- coding: utf-8 -*-
"""删掉刚才共用验证建的那张卷（DB 记录 + out 目录）。"""
import os
import shutil
import sqlite3

PID = "P20261101-001"
DB = r"E:\OCR\app\data\app.db"
OUT = os.path.join(r"E:\OCR\app\out", PID)

conn = sqlite3.connect(DB)
n = conn.execute("DELETE FROM papers WHERE pid=?", (PID,)).rowcount
conn.commit()
conn.close()
if os.path.isdir(OUT):
    shutil.rmtree(OUT, ignore_errors=True)
print("删除", PID, "记录数:", n, "；目录已删:", not os.path.isdir(OUT))
