# -*- coding: utf-8 -*-
"""把真库里 captain 的冒烟卷 P20261031-001 走产品软删路由（deleted_at），保留判卷证据。

起真服（真库 / 真 out）→ POST /papers/P20261031-001/delete → 断言 302 /papers →
查库：deleted_at 非空、submissions 行还在、out/P20261031-001/ 目录还在、列表查询为 0 行 → 停服。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time

import httpx

APP = r"E:\OCR\app"
DB = os.path.join(APP, "data", "app.db")
OUT = os.path.join(APP, "out")
PID = "P20261031-001"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def db_query(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def main():
    before = db_query("SELECT pid,title,deleted_at FROM papers")
    subs_before = db_query("SELECT sid,pid FROM submissions")
    print("before papers:", before)
    print("before submissions:", subs_before)

    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               MATH_APP_DB=DB, MATH_APP_OUT=OUT,
               MATH_APP_CONFIG=os.path.join(APP, "config.json"), MATH_NO_BROWSER="1")
    log = open(os.path.join(APP, "tools", "logs", "softdelete.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                             "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"],
                            cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT)
    result = {"pid": PID, "port": port}
    try:
        with httpx.Client(timeout=60.0, follow_redirects=False) as client:
            for _ in range(80):
                try:
                    if client.get(base + "/healthz").status_code == 200:
                        break
                except Exception:                       # noqa: BLE001
                    pass
                time.sleep(0.4)
            r = client.post(base + "/papers/%s/delete" % PID)
            result["delete_status"] = r.status_code
            result["delete_location"] = r.headers.get("location")
            page = client.get(base + "/papers")
            result["list_status"] = page.status_code
            result["list_has_pid"] = PID in page.text
            result["list_has_title"] = "captain 端到端冒烟卷" in page.text
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:                               # noqa: BLE001
            try:
                proc.kill()
            except Exception:                           # noqa: BLE001
                pass
        log.close()

    after = db_query("SELECT pid,title,deleted_at FROM papers")
    subs_after = db_query("SELECT sid,pid FROM submissions")
    visible = db_query("SELECT pid FROM papers WHERE deleted_at IS NULL")
    folder = os.path.join(OUT, PID)
    result.update({
        "after_papers": after,
        "after_submissions": subs_after,
        "visible_papers": visible,
        "folder_exists": os.path.isdir(folder),
        "folder_files": len(os.listdir(folder)) if os.path.isdir(folder) else 0,
        "submissions_preserved": len(subs_before) == len(subs_after),
    })
    with open(os.path.join(APP, "out", "acceptance_v1", "softdelete.json"), "w",
              encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
