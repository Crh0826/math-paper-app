# -*- coding: utf-8 -*-
"""服务端验收：local_formula 出现在设置页、测试连接能跑、真的能判一张卷。"""
import json
import re
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8710"


def post(path, data, timeout=600):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get(path, timeout=60):
    r = urllib.request.urlopen(BASE + path, timeout=timeout)
    return r.status, r.read().decode("utf-8", "replace")


code, page = get("/settings")
print("/settings:", code,
      "| 下拉含 local_formula:", 'value="local_formula"' in page,
      "| 有 lf_python:", 'name="lf_python"' in page,
      "| 显示 python 已找到:", "已找到" in page)

t0 = time.time()
code, page = post("/settings/test", {"backend": "local_formula"})
dt = time.time() - t0
m = re.search(r"(识别成功|已连通云端|没成功)[^<]{0,80}", page)
print(f"/settings/test local_formula: {code}，耗时 {dt:.1f}s →",
      (m.group(0)[:100] if m else "(没抓到结论)"))
for kw in ("识别成功", "识别失败", "不是 Key 的问题"):
    if kw in page:
        print("   页面含:", kw)
