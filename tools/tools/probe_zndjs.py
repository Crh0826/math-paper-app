# -*- coding: utf-8 -*-
"""抓取第三方 OCR 服务的测试页/接口说明，看看它要什么参数。"""
import ssl
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

URLS = [
    "https://www.zndjs.cn:8888/ocrtest",
    "http://www.zndjs.cn:9999/api",
    "https://www.zndjs.cn:8888/",
    "http://www.zndjs.cn:9999/",
]

for u in URLS:
    print("=" * 20, u)
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
            body = r.read()
            print("status", r.status, "| bytes", len(body),
                  "| type", r.headers.get("Content-Type"))
            text = body.decode("utf-8", "replace")
            print(text[:1500])
    except Exception as exc:  # noqa: BLE001
        print("失败:", type(exc).__name__, str(exc)[:160])
