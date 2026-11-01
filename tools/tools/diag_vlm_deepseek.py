# -*- coding: utf-8 -*-
"""验证 DeepSeek 视觉模型（vlm 后端）在修正 base_url 拼接后能否识别手写答案。"""
import os
import sys
import time

sys.path.insert(0, r"E:\OCR\app")
os.chdir(r"E:\OCR\app")

import ocr  # noqa: E402

SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-103711-b813ae"
CFG = r"E:\OCR\app\config.json"

cases = [
    ("3-1.png", "填空", "期望 99"),
    ("3-2.png", "填空", "期望 2026/4053"),
]

for base in ("https://api.deepseek.com/v1", "https://api.deepseek.com"):
    print(f"\n=== base_url = {base} ===")
    for name, kind, expect in cases:
        p = os.path.join(SUB, name)
        if not os.path.isfile(p):
            continue
        rec = ocr.Recognizer(backend="vlm", config_path=CFG, use_env=False, timeout_s=120)
        rec.base_url = base
        t0 = time.time()
        r = rec.read(p, kind)
        dt = time.time() - t0
        print(f"  {name}: {dt:5.1f}s  text={r.get('text')!r}  backend={r.get('backend')}"
              f"  err={str(rec.last_error)[:110]}   （{expect}）")
