# -*- coding: utf-8 -*-
"""探测 GLM-OCR 的正确调用方式：layout_parsing + file 字段的几种写法。"""
import base64
import json
import time
import urllib.error
import urllib.request

KEY = "8a047fb66ec54b42bc1edde3f9257c0e.O0bgUyqGnFVWh2BP"
URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
IMG = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9\4-2.png"

raw = open(IMG, "rb").read()
b64 = base64.b64encode(raw).decode()


def call(payload, note):
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": KEY})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=120)
        txt = r.read().decode("utf-8", "replace")
        print(f"{note:28s} {time.time()-t0:5.1f}s OK  {txt[:220]}")
    except urllib.error.HTTPError as e:
        print(f"{note:28s} {time.time()-t0:5.1f}s HTTP {e.code}  {e.read().decode('utf-8','replace')[:220]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{note:28s} {time.time()-t0:5.1f}s {type(exc).__name__}: {exc}")


call({"model": "glm-ocr", "file": "data:image/png;base64," + b64}, "file = data URI")
call({"model": "glm-ocr", "file": b64}, "file = 裸 base64")
call({"model": "glm-ocr", "file": "https://cdn.bigmodel.cn/static/logo/introduction.png"},
     "file = 官方示例 URL")
