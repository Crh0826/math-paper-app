# -*- coding: utf-8 -*-
"""探测智谱 GLM-OCR：先确认模型名可用，再跑真实裁切图。"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

KEY = "8a047fb66ec54b42bc1edde3f9257c0e.O0bgUyqGnFVWh2BP"
URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"

CANDIDATES = ["glm-ocr", "glm-4.6v", "glm-4.5v", "glm-4v-plus", "glm-4v-flash", "glm-4v"]


def call(model, image_path, prompt="请识别图中手写的数学答案，只输出答案本身。"):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": 256,
        "temperature": 0,
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=120)
        obj = json.loads(r.read().decode())
        msg = (obj.get("choices") or [{}])[0].get("message", {})
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return time.time() - t0, str(content or "").strip()[:80], None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:180]
        return time.time() - t0, "", f"HTTP {e.code} {detail}"
    except Exception as exc:  # noqa: BLE001
        return time.time() - t0, "", f"{type(exc).__name__}: {exc}"


img = os.path.join(SUB, "4-2.png")
for m in CANDIDATES:
    dt, text, err = call(m, img)
    print(f"{m:14s} {dt:5.1f}s  text={text!r:24.24}  err={err if err else ''}")
