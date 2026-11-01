# -*- coding: utf-8 -*-
"""测几个 GitHub 镜像对 pix2tex 权重的下载速度，选最快的。"""
import time
import urllib.request

GH = "https://github.com/lukas-blecher/LaTeX-OCR/releases/download/v0.0.1/weights.pth"
CANDIDATES = [
    ("github 直连", GH),
    ("ghproxy.net", "https://ghproxy.net/" + GH),
    ("gh-proxy.com", "https://gh-proxy.com/" + GH),
    ("ghfast.top", "https://ghfast.top/" + GH),
    ("hk.gh-proxy.com", "https://hk.gh-proxy.com/" + GH),
    ("moeyy", "https://github.moeyy.xyz/" + GH),
]

for name, url in CANDIDATES:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-3145728"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=20) as r:
            got = 0
            while got < 3 * 1024 * 1024:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
        dt = max(0.001, time.time() - t0)
        print(f"{name:16s} {got/1024/1024:5.1f} MB in {dt:5.1f}s → {got/dt/1024:6.0f} KB/s")
    except Exception as exc:  # noqa: BLE001
        print(f"{name:16s} 失败 {type(exc).__name__}: {str(exc)[:60]}")
