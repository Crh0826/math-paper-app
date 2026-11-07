# -*- coding: utf-8 -*-
"""把 pix2tex 缺的 image_resizer.pth 从镜像下下来，放进包内 checkpoints 目录。"""
import os
import urllib.request

CK = r"E:\OCR\.tools\venv_formula\Lib\site-packages\pix2tex\model\checkpoints"
GH = ("https://github.com/lukas-blecher/LaTeX-OCR/releases/download/v0.0.1/image_resizer.pth")
MIRRORS = ["https://gh-proxy.com/" + GH, "https://ghfast.top/" + GH, GH]
dest = os.path.join(CK, "image_resizer.pth")

for url in MIRRORS:
    try:
        print("尝试:", url[:70])
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=60) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)
        print("OK →", dest, "%.1f MB" % (len(data) / 1024 / 1024))
        break
    except Exception as exc:  # noqa: BLE001
        print("  失败:", type(exc).__name__, str(exc)[:90])
