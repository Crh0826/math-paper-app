# -*- coding: utf-8 -*-
"""GLM-OCR 整页解析：看它能不能把答题卡上 9 个作答框都读出来（带 bbox）。"""
import base64
import json
import os
import time
import urllib.request

KEY = "8a047fb66ec54b42bc1edde3f9257c0e.O0bgUyqGnFVWh2BP"
URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
IMG = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9\source.jpg"

raw = open(IMG, "rb").read()
ext = "jpeg" if IMG.lower().endswith((".jpg", ".jpeg")) else "png"
data = "data:image/%s;base64,%s" % (ext, base64.b64encode(raw).decode())
body = json.dumps({"model": "glm-ocr", "file": data}, ensure_ascii=False).encode()
req = urllib.request.Request(URL, data=body, headers={
    "Content-Type": "application/json", "Authorization": KEY})
t0 = time.time()
obj = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
print(f"整页解析 {time.time()-t0:.1f}s；data_info = {obj.get('data_info')}")
n = 0
for page in (obj.get("layout_details") or []):
    for blk in (page or []):
        c = blk.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        n += 1
        print(f"  [{n:02d}] bbox={blk.get('bbox_2d')} label={blk.get('label')!r} "
              f"content={c.strip()[:60]!r}")
print("非空块:", n)
