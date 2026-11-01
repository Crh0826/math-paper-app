# -*- coding: utf-8 -*-
"""GLM-OCR 对条状裁切图返回空 → 贴到"一页纸"画布再试。"""
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

from PIL import Image

sys.path.insert(0, r"E:\OCR\app")
from normalize import is_equivalent  # noqa: E402
from tools.compare_glm_ocr import to_plain  # noqa: E402

KEY = "8a047fb66ec54b42bc1edde3f9257c0e.O0bgUyqGnFVWh2BP"
URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
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


def pad_png(path, canvas=(1000, 1400)):
    with Image.open(path) as im:
        im = im.convert("RGB")
        scale = min(1.0, (canvas[0] * 0.9) / im.width)
        if scale < 1.0:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                           Image.LANCZOS)
        out = Image.new("RGB", canvas, "white")
        out.paste(im, ((canvas[0] - im.width) // 2, int(canvas[1] * 0.12)))
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()


def ocr_padded(path):
    raw = pad_png(path)
    data = "data:image/png;base64," + base64.b64encode(raw).decode()
    body = json.dumps({"model": "glm-ocr", "file": data}, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": KEY})
    t0 = time.time()
    try:
        obj = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
    except urllib.error.HTTPError as e:
        return time.time() - t0, "", f"HTTP {e.code}"
    dt = time.time() - t0
    parts = []
    for page in (obj.get("layout_details") or []):
        for blk in (page or []):
            c = blk.get("content")
            if isinstance(c, str) and c.strip():
                parts.append(c)
    return dt, "\n".join(parts), None


ok_n, total = 0, 0.0
for fname, expected in CASES:
    p = os.path.join(SUB, fname)
    if not os.path.isfile(p):
        continue
    dt, md, err = ocr_padded(p)
    total += dt
    plain = to_plain(md)
    ok, why = (False, err or "空")
    if plain:
        ok, why = is_equivalent(plain, expected, "填空")
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} {dt:5.1f}s  md={md.strip()[:46]!r}")
    print(f"           → {plain!r:26.26} 期望 {expected!r}")
print(f"\nGLM-OCR（页面化后）：{ok_n}/{len(CASES)} 判对，合计 {total:.1f}s")
