# -*- coding: utf-8 -*-
"""zndjs /ocrread（理解型，返回 LaTeX）在 9 张真实作答裁切上的表现。"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, r"E:\OCR\app")
from normalize import is_equivalent  # noqa: E402
from ocr import latex_to_plain     # noqa: E402

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TOKEN = "zndjpf0dBJemD5piONox"
EP = "https://www.zndjs.cn:8888/ocrread"
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


def call(path, method=2):
    raw = open(path, "rb").read()
    body = json.dumps({"token": TOKEN, "method": method,
                       "image": base64.b64encode(raw).decode("ascii")}).encode()
    req = urllib.request.Request(EP, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180, context=CTX) as r:
        return time.time() - t0, json.loads(r.read().decode("utf-8", "replace"))


ok_n, total = 0, 0.0
for fname, expected in CASES:
    p = os.path.join(SUB, fname)
    if not os.path.isfile(p):
        continue
    try:
        dt, obj = call(p)
    except Exception as exc:  # noqa: BLE001
        print(f"✘ {fname:9s} 失败 {type(exc).__name__}: {str(exc)[:70]}")
        continue
    total += dt
    raw = str(obj.get("result") or "")
    text = latex_to_plain(raw)
    ok = bool(text) and is_equivalent(text, expected, "填空")[0]
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} {dt:4.1f}s 原文 {raw!r:34.34}")
    print(f"           → {text!r:24.24} 期望 {expected!r}")
print(f"\n/ocrread(method=2)：{ok_n}/{len(CASES)} 判对，合计 {total:.1f}s")
