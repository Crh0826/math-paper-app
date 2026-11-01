# -*- coding: utf-8 -*-
"""验收：设置页字段不再串后端（pv_* / vlm_*），护栏只拦 paddle_vl 的模型名。"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP = r"E:\OCR\app"
TMP = r"E:\OCR\app\out\_settings_check"
os.makedirs(TMP, exist_ok=True)
cfg = os.path.join(TMP, "config.json")
shutil.copy2(os.path.join(APP, "config.json"), cfg)
BASE = "http://127.0.0.1:8750"

env = dict(os.environ, MATH_APP_CONFIG=cfg, MATH_NO_BROWSER="1",
           PYTHONIOENCODING="utf-8", PYTHONPATH=APP)
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--app-dir", APP, "server:app",
                         "--host", "127.0.0.1", "--port", "8750"], cwd=APP, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def post(data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + "/settings", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def cfg_now():
    return json.load(open(cfg, encoding="utf-8"))["ocr"]


try:
    for _ in range(30):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=2)
            break
        except Exception:
            time.sleep(0.5)

    html = urllib.request.urlopen(BASE + "/settings", timeout=20).read().decode("utf-8", "replace")
    print("页面字段：pv_model=%s vlm_model=%s 重名 model=%d 重名 token=%d"
          % ('name="pv_model"' in html, 'name="vlm_model"' in html,
             html.count('name="model"'), html.count('name="token"') + html.count('name="key"')))

    code, page = post({"backend": "paddle_vl", "pv_model": "deepseek-flash"})
    print("1) paddle_vl 存 deepseek-flash → HTTP %d，paddle 模型=%s"
          % (code, cfg_now()["paddle_vl"]["model"]))
    print("   提示里有没有教用户去哪填:", "视觉模型" in page)

    code, _ = post({"backend": "vlm", "vlm_base_url": "https://api.deepseek.com",
                    "vlm_model": "deepseek-flash"})
    o = cfg_now()
    print("2) vlm 存 deepseek-flash → HTTP %d，vlm=%s / paddle=%s"
          % (code, o["vlm"]["model"], o["paddle_vl"]["model"]))

    code, _ = post({"backend": "paddle_vl", "pv_model": "PaddleOCR-VL-1.7"})
    o = cfg_now()
    print("3) paddle 存 PaddleOCR-VL-1.7 → HTTP %d，paddle=%s / vlm=%s"
          % (code, o["paddle_vl"]["model"], o["vlm"]["model"]))
    print("   两边互不污染:", o["paddle_vl"]["model"] == "PaddleOCR-VL-1.7"
          and o["vlm"]["model"] == "deepseek-flash")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
