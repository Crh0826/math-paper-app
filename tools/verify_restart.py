# -*- coding: utf-8 -*-
"""重启后验收：服务用的是新代码 + 智谱 vlm 真能识别 + 设置页测试连接通过。"""
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8710"


def get(path, timeout=60):
    r = urllib.request.urlopen(BASE + path, timeout=timeout)
    return r.status, r.read().decode("utf-8", "replace")


def post(path, data, timeout=120):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


code, txt = get("/healthz")
h = json.loads(txt)
print("healthz:", code, "| ok =", h.get("ok"), "| stale =", (h.get("code") or {}).get("stale"))
print("  进程启动:", (h.get("code") or {}).get("process_started"),
      "| 最新代码:", (h.get("code") or {}).get("code_modified"),
      (h.get("code") or {}).get("newest_file"))

code, page = post("/settings/test", {"backend": "vlm"})
print("\n/settings/test vlm:", code)
for key in ("识别成功", "已连通", "失败", "不是 Key 的问题", "耗时"):
    if key in page:
        print("  页面含:", key)
import re
m = re.search(r"(识别成功|已连通云端|没成功)[^<]{0,120}", page)
print("  结论行:", m.group(0)[:120] if m else "(没抓到)")

code, page = get("/settings")
print("\n/settings:", code, "| 含 pv_model:", 'name="pv_model"' in page,
      "| 含 vlm_model:", 'name="vlm_model"' in page)
