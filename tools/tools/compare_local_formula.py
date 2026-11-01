# -*- coding: utf-8 -*-
"""路线一验收：pix2tex（本地 CPU）在 9 张真实作答裁切上的判对率与耗时。

用主环境的 python 跑（负责判分口径），推理走 venv 里的 p2t_bridge 子进程。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

APP = r"E:\OCR\app"
sys.path.insert(0, APP)
os.chdir(APP)

from normalize import is_equivalent   # noqa: E402
from ocr import latex_to_plain        # noqa: E402

VENV_PY = r"E:\OCR\.tools\venv_formula\Scripts\python.exe"
BRIDGE = os.path.join(APP, "tools", "p2t_bridge.py")
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

paths = [os.path.join(SUB, f) for f, _ in CASES if os.path.isfile(os.path.join(SUB, f))]
kept = [(f, e) for f, e in CASES if os.path.isfile(os.path.join(SUB, f))]

t0 = time.time()
proc = subprocess.run([VENV_PY, "-X", "utf8", BRIDGE],
                      input=json.dumps({"images": paths}),
                      capture_output=True, text=True, encoding="utf-8", timeout=900)
wall = time.time() - t0
if proc.returncode != 0:
    print("桥返回非 0：", proc.returncode)
    print("stdout 尾:", (proc.stdout or "")[-400:])
    print("stderr 尾:", (proc.stderr or "")[-800:])
    raise SystemExit(1)

line = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip().startswith("{")][-1]
obj = json.loads(line)
print(f"模型加载 {obj.get('model_load_s')}s，9 张推理总墙钟 {wall:.1f}s\n")

ok_n, infer = 0, 0.0
for (fname, expected), item in zip(kept, obj.get("results") or []):
    latex = (item.get("latex") or "").strip()
    infer += float(item.get("elapsed_s") or 0)
    text = latex_to_plain(latex)
    ok, why = (False, item.get("error") or "空结果")
    if text:
        ok, why = is_equivalent(text, expected, "填空")
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} {item.get('elapsed_s')}s latex={latex!r:36.36}")
    print(f"           → {text!r:24.24} 期望 {expected!r}  ({why[:26]})")

print(f"\npix2tex：{ok_n}/{len(kept)} 判对，纯推理 {infer:.1f}s（含模型加载 {obj.get('model_load_s')}s）")
print("\n对比基线（同一批 9 张）：")
print("  智谱 glm-4.5v                 9/9   21.2s")
print("  GLM-OCR + glm-4.5v 兜底        8/9   15.4s")
print("  zndjs /ocrread                4/9   54.2s")
