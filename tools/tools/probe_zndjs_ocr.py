# -*- coding: utf-8 -*-
"""试第三方 OCR（zndjs.cn）：先单张，再整页，最后跑 9 张裁切对比。"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, r"E:\OCR\app")
from normalize import is_equivalent  # noqa: E402

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TOKEN = "zndjpf0dBJemD5piONox"
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


def call(path, method=1, endpoint="https://www.zndjs.cn:8888/ocr"):
    raw = open(path, "rb").read()
    payload = {"token": TOKEN, "method": method,
               "image": base64.b64encode(raw).decode("ascii")}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90, context=CTX) as r:
        obj = json.loads(r.read().decode("utf-8", "replace"))
    return time.time() - t0, obj


print("=== 单张：4-2.png（x=3/2）===")
dt, obj = call(os.path.join(SUB, "4-2.png"))
print(f"  {dt:.1f}s  {json.dumps(obj, ensure_ascii=False)[:300]}")

print("\n=== 整页：source.jpg（答题卡）===")
dt, obj = call(os.path.join(SUB, "source.jpg"))
res = obj.get("result") or []
print(f"  {dt:.1f}s  code={obj.get('code')} 行数={len(res)} 余额={obj.get('Balance')}")
for i, line in enumerate(res[:14], 1):
    print(f"   {i:02d} {str(line)[:90]!r}")

print("\n=== 9 张裁切 ===")
ok_n, total = 0, 0.0
for fname, expected in CASES:
    p = os.path.join(SUB, fname)
    if not os.path.isfile(p):
        continue
    try:
        dt, obj = call(p)
    except Exception as exc:  # noqa: BLE001
        print(f"✘ {fname:9s} 调用失败 {type(exc).__name__}: {str(exc)[:60]}")
        continue
    total += dt
    text = " ".join(str(x) for x in (obj.get("result") or [])).strip()
    ok = bool(text) and is_equivalent(text, expected, "填空")[0]
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} {dt:5.1f}s {text!r:28.28} 期望 {expected!r}")
print(f"\n第三方 OCR：{ok_n}/{len(CASES)} 判对，合计 {total:.1f}s")
