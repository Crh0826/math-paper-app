# -*- coding: utf-8 -*-
"""摸清 zndjs 三个接口 /ocr /ocrread /ocridcard 与 method 参数的差别。"""
import base64
import json
import os
import ssl
import time
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TOKEN = "zndjpf0dBJemD5piONox"
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"
IMGS = [("4-2.png", "x=3/2"), ("3-1.png", "99")]

b64 = {f: base64.b64encode(open(os.path.join(SUB, f), "rb").read()).decode() for f, _ in IMGS}


def call(endpoint, fname, method=1, extra=None):
    payload = {"token": TOKEN, "method": method, "image": b64[fname]}
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = raw[:200]
        return time.time() - t0, obj
    except Exception as exc:  # noqa: BLE001
        return time.time() - t0, f"{type(exc).__name__}: {str(exc)[:100]}"


for ep in ("https://www.zndjs.cn:8888/ocr",
           "https://www.zndjs.cn:8888/ocrread",
           "https://www.zndjs.cn:8888/ocridcard"):
    for fname, desc in IMGS:
        for method in (1, 2):
            dt, obj = call(ep, fname, method)
            s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
            print(f"{ep.rsplit('/',1)[-1]:10s} method={method} {fname:9s}({desc:6s}) "
                  f"{dt:4.1f}s → {s[:120]}")
