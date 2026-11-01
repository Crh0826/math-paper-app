# -*- coding: utf-8 -*-
"""pix2tex 对答题卡裁切图是否吃"预处理"：灰度/二值/反色/放大/紧裁 组合对比。

结论导向：如果预处理也救不回来，说明 pix2tex 的域（印刷体公式）和我们的域（手写答案格）差太远。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageOps

APP = r"E:\OCR\app"
sys.path.insert(0, APP)

VENV_PY = r"E:\OCR\.tools\venv_formula\Scripts\python.exe"
BRIDGE = os.path.join(APP, "tools", "p2t_bridge.py")
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"
TMP = os.path.join(APP, "out", "_p2t_prep")
os.makedirs(TMP, exist_ok=True)

CASES = [("3-1.png", "99"), ("4-2.png", "x=3/2"), ("4-1.png", "x=3"), ("3-2.png", "2026/4053")]


def variants(src: str, tag: str):
    im = Image.open(src).convert("L")
    out = {}
    out["orig"] = im
    # 1) 紧裁到墨迹 + 加白边
    a = np.array(im)
    mask = a < 200
    if mask.any():
        ys, xs = np.where(mask)
        im2 = im.crop((max(0, xs.min() - 4), max(0, ys.min() - 4),
                       min(im.width, xs.max() + 5), min(im.height, ys.max() + 5)))
    else:
        im2 = im
    out["tight"] = ImageOps.expand(im2, border=20, fill=255)
    # 2) 二值化
    out["binary"] = out["tight"].point(lambda p: 0 if p < 160 else 255)
    # 3) 放大 3 倍
    t = out["tight"]
    out["tight3x"] = t.resize((t.width * 3, t.height * 3), Image.LANCZOS)
    # 4) 二值 + 放大 3 倍
    b = out["binary"]
    out["bin3x"] = b.resize((b.width * 3, b.height * 3), Image.LANCZOS)
    paths = {}
    for name, img in out.items():
        p = os.path.join(TMP, f"{tag}_{name}.png")
        img.save(p)
        paths[name] = p
    return paths


all_paths, index = [], []
for fname, expect in CASES:
    vp = variants(os.path.join(SUB, fname), fname.split(".")[0])
    for name, p in vp.items():
        all_paths.append(p)
        index.append((fname, expect, name))

t0 = time.time()
proc = subprocess.run([VENV_PY, "-X", "utf8", BRIDGE],
                      input=json.dumps({"images": all_paths}),
                      capture_output=True, text=True, encoding="utf-8", timeout=1800)
wall = time.time() - t0
line = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip().startswith("{")][-1]
obj = json.loads(line)
print(f"模型加载 {obj.get('model_load_s')}s，{len(all_paths)} 张总 {wall:.1f}s\n")

from ocr import latex_to_plain  # noqa: E402
from normalize import is_equivalent  # noqa: E402

for (fname, expect, name), item in zip(index, obj.get("results") or []):
    latex = (item.get("latex") or "").strip()
    text = latex_to_plain(latex)
    ok = bool(text) and is_equivalent(text, expect, "填空")[0]
    print(f"{'✔' if ok else '✘'} {fname:9s} {name:8s} {item.get('elapsed_s')}s "
          f"latex={latex[:44]!r}")
    print(f"      → {text[:30]!r} 期望 {expect!r}")
