# -*- coding: utf-8 -*-
"""验收 local_formula 后端：read_batch 走本机 Qwen2.5-VL，9 张全过一遍。"""
import json
import os
import sys
import time

sys.path.insert(0, r"E:\OCR\app")
os.chdir(r"E:\OCR\app")

import ocr  # noqa: E402
from normalize import is_equivalent  # noqa: E402

SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"
CASES = [
    ("1-1.png", "0.6, (2011)/(2012), 368"),
    ("1-2.png", "-π, -100, -2(5)/(7)"),
    ("2-1.png", "-6"),
    ("3-1.png", "99"),
    ("3-2.png", "(2026)/(4053)"),
    ("4-1.png", "x=3"),
    ("4-2.png", "x=(3)/(2)"),
    ("4-3.png", "x=11"),
    ("4-4.png", "y=(4)/(5)"),
]

cfg = json.load(open(r"E:\OCR\app\config.json", encoding="utf-8"))
cfg["ocr"]["backend"] = "local_formula"
cfg["ocr"]["local_formula"] = {
    "python": r"E:\OCR\.tools\venv_formula\Scripts\python.exe",
    "model": "Qwen/Qwen2.5-VL-3B-Instruct",
    "fallback": True,
    "timeout_s": 900,
}
tmp = r"E:\OCR\app\out\_cfg_local_formula.json"
json.dump(cfg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)

rec = ocr.Recognizer(backend="local_formula", config_path=tmp, use_env=False, timeout_s=120)
print("backend =", rec.backend, "| python =", os.path.basename(os.path.dirname(os.path.dirname(rec.lf_python))),
      "| model =", rec.lf_model, "| 兜底 =", rec.lf_fallback)

pairs = [("%d" % i, os.path.join(SUB, f)) for i, (f, _e) in enumerate(CASES)]
t0 = time.time()
got = rec.read_batch(pairs)
wall = time.time() - t0

ok_n = 0
for (key, _p), (fname, expect) in zip(pairs, CASES):
    item = got.get(key) or {}
    text = (item.get("text") or "").strip()
    qtype = "填空" if fname[0] in "12" else "解答"
    ok = bool(text) and is_equivalent(text, expect, qtype)[0]
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} [{item.get('backend','?'):20s}] {text!r:24.24} 期望 {expect!r}")
print(f"\nread_batch 墙钟 {wall:.1f}s；判对 {ok_n}/{len(CASES)}")
print("last_error =", rec.last_error or "(无)")
