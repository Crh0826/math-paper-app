# -*- coding: utf-8 -*-
"""验收：服务上的 GLM-OCR 后端（下拉里有、测试连接通过、真判卷能认出）。"""
import json
import os
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8710"


def get(path, timeout=90):
    r = urllib.request.urlopen(BASE + path, timeout=timeout)
    return r.status, r.read().decode("utf-8", "replace")


def post(path, data, timeout=180):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


code, page = get("/settings")
print("/settings:", code,
      "| 下拉含 glm_ocr:", 'value="glm_ocr"' in page,
      "| 有 glm_key 输入:", 'name="glm_key"' in page,
      "| 有 glm_model 输入:", 'name="glm_model"' in page)

code, page = post("/settings/test", {"backend": "glm_ocr"})
import re
m = re.search(r"(识别成功|已连通云端|没成功)[^<]{0,90}", page)
print("/settings/test glm_ocr:", code, "|", (m.group(0)[:110] if m else "(没抓到结论)"))
for kw in ("识别成功", "已连通", "不是 Key 的问题"):
    if kw in page:
        print("   页面含:", kw)

code, h = get("/healthz")
d = json.loads(h)
print("\n/healthz backend 行:", json.dumps(
    [c for c in (d.get("checks") or []) if "OCR" in str(c.get("key", "")).upper()
     or "识别" in str(c.get("title", ""))][:1], ensure_ascii=False)[:220])
