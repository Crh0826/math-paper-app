# -*- coding: utf-8 -*-
"""3-2（分数 2026/4053）识别为空，试几种预处理。"""
import os
import sys
import time

from PIL import Image

sys.path.insert(0, r"E:\OCR\app")
os.chdir(r"E:\OCR\app")

import ocr  # noqa: E402

SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-103711-b813ae"
TMP = r"E:\OCR\app\out\_vlm_probe"
os.makedirs(TMP, exist_ok=True)
CFG = r"E:\OCR\app\config.json"

src = os.path.join(SUB, "3-2.png")


def prep(kind: str) -> str:
    with Image.open(src) as im:
        im = im.convert("RGB")
        if kind == "raw":
            out = im
        elif kind == "x2":
            out = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
        elif kind == "pad":
            out = Image.new("RGB", (im.width + 80, im.height + 60), "white")
            out.paste(im, (40, 30))
        elif kind == "x1_5pad":
            im2 = im.resize((int(im.width * 1.5), int(im.height * 1.5)), Image.LANCZOS)
            out = Image.new("RGB", (im2.width + 80, im2.height + 60), "white")
            out.paste(im2, (40, 30))
        p = os.path.join(TMP, f"3-2_{kind}.png")
        out.save(p)
        return p


for kind in ("raw", "x2", "pad", "x1_5pad"):
    p = prep(kind)
    rec = ocr.Recognizer(backend="vlm", config_path=CFG, use_env=False, timeout_s=120)
    t0 = time.time()
    r = rec.read(p, "填空")
    print(f"{kind:10s} {time.time()-t0:5.1f}s text={r.get('text')!r} "
          f"backend={r.get('backend')} err={str(rec.last_error)[:60]}")
