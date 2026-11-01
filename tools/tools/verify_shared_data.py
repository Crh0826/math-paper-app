# -*- coding: utf-8 -*-
"""验证：便携版 exe 与本地版是否共用同一套题库 / 路径 / 试卷数据。

做法：让 exe 在不干扰 8710 的端口上跑起来 → 通过它建一份 3 题卷 →
     直接查本地 app.db 是否出现这条记录、PDF 是否落在 app/out。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

EXE = r"E:\OCR\dist\数学题库工作台\数学题库工作台.exe"
DB = r"E:\OCR\app\data\app.db"
OUT = r"E:\OCR\app\out"
BASE = "http://127.0.0.1:8760"

env = dict(os.environ, MATH_NO_BROWSER="1", PYTHONIOENCODING="utf-8")
proc = subprocess.Popen([EXE, "--port", "8760", "--no-browser"],
                        cwd=os.path.dirname(EXE), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        errors="ignore")
try:
    ready = False
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=2)
            ready = True
            break
        except Exception:
            time.sleep(1)
    print("服务起来:", ready)

    # 记下建卷前 app.db 里的卷数
    conn = sqlite3.connect(DB)
    before = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    conn.close()

    data = urllib.parse.urlencode({
        "title": "共用数据验证卷", "n_choice": "2", "n_fill": "1", "n_solution": "0",
    }, doseq=True).encode()
    req = urllib.request.Request(BASE + "/papers", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    t0 = time.time()
    r = urllib.request.urlopen(req, timeout=600)
    body = r.read().decode("utf-8", "replace")
    final_url = r.geturl()
    print(f"建卷耗时 {time.time()-t0:.1f}s → {final_url}")
    pid = final_url.rstrip("/").split("/")[-1]
    print("卷号:", pid)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    after = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    row = conn.execute("SELECT pid,title,docx_path,pdf_path FROM papers WHERE pid=?",
                       (pid,)).fetchone()
    conn.close()
    print(f"app.db 卷数 {before} → {after}；能查到这张卷:", bool(row))
    if row:
        print("  docx:", row["docx_path"])
        print("  pdf :", row["pdf_path"])
    d = os.path.join(OUT, pid)
    files = sorted(os.listdir(d)) if os.path.isdir(d) else []
    print(f"app\\out\\{pid} 下的文件:", files)
finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=15)
    except Exception:
        proc.kill()
        out = ""
    print("\n=== exe 启动日志（前 25 行）===")
    for line in (out or "").splitlines()[:25]:
        print(" ", line)
