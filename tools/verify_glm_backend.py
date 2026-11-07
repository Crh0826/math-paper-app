# -*- coding: utf-8 -*-
"""验收 GLM-OCR 后端（含视觉模型兜底）：9 张真实作答裁切怎么判。"""
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

rec = ocr.Recognizer(backend="glm_ocr", config_path=r"E:\OCR\app\config.json", use_env=False,
                     timeout_s=120)
print("backend =", rec.backend, "| glm model =", rec.glm_model, "| 兜底 =", rec.glm_fallback,
      "| vlm =", rec.vlm_model)
ok_n, total = 0, 0.0
for fname, expected in CASES:
    p = os.path.join(SUB, fname)
    if not os.path.isfile(p):
        continue
    t0 = time.time()
    r = rec.read(p, "填空")
    dt = time.time() - t0
    total += dt
    text = (r.get("text") or "").strip()
    backend = r.get("backend")
    ok = False
    if text:
        ok, _why = is_equivalent(text, expected, "填空")
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} {dt:5.1f}s [{backend:12s}] {text!r:26.26} 期望 {expected!r}")
print(f"\nGLM-OCR(+兜底)：{ok_n}/{len(CASES)} 判对，合计 {total:.1f}s")
