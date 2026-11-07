# -*- coding: utf-8 -*-
"""本地 Qwen2.5-VL-3B 的判对口径复核（与线上同一套 is_equivalent）。"""
import json
import os
import sys

sys.path.insert(0, r"E:\OCR\app")

from normalize import is_equivalent  # noqa: E402

RES = r"E:\OCR\app\out\_qwen25vl.json"
rows = json.load(open(RES, encoding="utf-8"))
TYPE = {"1": "填空", "2": "填空", "3": "解答", "4": "解答"}

ok_n = 0
for r in rows:
    f = r["file"]
    qtype = "填空" if f[0] in "12" else "解答"
    text, expect = r["text"], r["expect"]
    ok, why = is_equivalent(text, expect, qtype)
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {f:9s} {r['elapsed_s']:6.2f}s  {text!r:26.26} 期望 {expect!r:24.24} ({why[:24]})")

total = sum(float(r["elapsed_s"]) for r in rows)
print(f"\nQwen2.5-VL-3B（本地 CPU）：{ok_n}/{len(rows)} 判对，推理合计 {total:.1f}s"
      f"（平均 {total/len(rows):.1f}s/张，另加模型加载 8.9s）")
