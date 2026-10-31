# -*- coding: utf-8 -*-
"""单独跑一次卷面 render，把 PDF/PNG + health 落成验收证据（T4 第 5 项）。

Word COM 是全机器共享的单实例，别的 agent 同时在导 PDF 时会「命令失败」，
所以这里重试 5 次（间隔 5s）并把每次的错误都记下来。
"""
import io
import json
import os
import sqlite3
import sys
import time

APP = r"E:\OCR\app"
sys.path.insert(0, APP)
import paper    # noqa: E402
import picker   # noqa: E402

OUT = os.path.join(APP, "out", "acceptance_v1")
DOC = os.path.join(OUT, "paper")
PARAMS = {
    "title": "验收卷 A · 七年级数学",
    "mix": {"选择": 10, "填空": 8, "解答": 6},
    "scope": {"chapters": [], "kps": [], "kp_mode": "any"},
    "requirements": {"need_answer": True, "exclude_empty": True, "exclude_no_family": False},
    "seed": 42,
}

conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(APP, "data", "app.db").replace("\\", "/"),
                       uri=True)
conn.row_factory = sqlite3.Row
m = picker.build(dict(PARAMS), conn, pid="P20261031-001")
conn.close()

attempts = []
res = None
for i in range(5):
    t0 = time.time()
    try:
        res = paper.render_paper(m, DOC, name="acceptance_paper")
        attempts.append({"attempt": i + 1, "ok": True, "s": round(time.time() - t0, 2)})
        break
    except Exception as e:                             # noqa: BLE001
        attempts.append({"attempt": i + 1, "ok": False, "s": round(time.time() - t0, 2),
                         "error": "%s: %s" % (type(e).__name__, e)})
        time.sleep(5)

payload = {"attempts": attempts, "word_available": paper.word_available(),
           "params": PARAMS}
if res is None:
    payload["failed"] = True
else:
    payload.update({"pages": res["pages"], "pdf": res["pdf"], "png": res["png"],
                    "health": {k: v for k, v in res["health"].items() if k != "png"}})
io.open(os.path.join(OUT, "paper_render.json"), "w", encoding="utf-8", newline="\n").write(
    json.dumps(payload, ensure_ascii=False, indent=2, default=str))
print(json.dumps({"failed": payload.get("failed", False),
                  "pages": payload.get("pages"),
                  "attempts": attempts}, ensure_ascii=False))
if res is not None:
    h = res["health"]
    print("ink_ratio:", [round(v, 4) for v in h["ink_ratio"]],
          "convert_s:", h["convert_s"], "elapsed_s:", h["elapsed_s"],
          "pdf_bytes:", h["pdf_bytes"], "page_size_px:", h["page_size_px"])
