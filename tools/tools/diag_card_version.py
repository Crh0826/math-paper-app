# -*- coding: utf-8 -*-
"""判定这张卡到底是 4x4 还是 5x5，以及 by_ids 为什么读不出。"""
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, r"E:\OCR\app")
os.chdir(r"E:\OCR\app")

import card  # noqa: E402

SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-103711-b813ae"


def bits_from(path: str, grid: int):
    with Image.open(path) as im:
        g = np.array(im.convert("L").resize((grid * 12, grid * 12), Image.LANCZOS), dtype=float)
    out = []
    cell = g.shape[0] // grid
    for r in range(grid):
        for c in range(grid):
            blk = g[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell]
            out.append(1 if blk.mean() < 128 else 0)
    return out


for name in ("id_1.png", "id_2.png", "id_4.png"):
    p = os.path.join(SUB, name)
    if not os.path.isfile(p):
        continue
    with Image.open(p) as im:
        size = im.size
    print(f"\n{name} 尺寸={size}")
    for grid in (4, 5):
        b = bits_from(p, grid)
        if grid == 4:
            dec = card.legacy_idx_bits(b)
        else:
            dec = card.decode_id_bits(b)
        print(f"  按 {grid}x{grid}: bits={''.join(map(str,b))} -> {dec!r}")

print("\n=== 卡面文字自检 ===")
import subprocess  # noqa: E402
print("card.py 里写死的说明文字：")
for ln in open(os.path.join(r"E:\OCR\app", "card.py"), encoding="utf-8").read().splitlines():
    if "4×4" in ln or "5×5" in ln and "黑白格" in ln:
        print("  ", ln.strip()[:100])
