# -*- coding: utf-8 -*-
"""下载 pix2tex 权重（97.4MB）到 %LOCALAPPDATA%\\pix2tex\\weights.pth，支持断点续传与镜像回退。"""
from __future__ import annotations

import os
import time
import urllib.request

DEST_DIR = os.path.join(os.environ["LOCALAPPDATA"], "pix2tex")
DEST = os.path.join(DEST_DIR, "weights.pth")
GH = ("https://github.com/lukas-blecher/LaTeX-OCR/releases/download/v0.0.1/weights.pth")
MIRRORS = [
    "https://gh-proxy.com/" + GH,      # 实测 1.3 MB/s（2026-11-01）
    "https://ghfast.top/" + GH,
    GH,                                 # 直连只有 ~126 KB/s
    "https://ghproxy.net/" + GH,
    "https://mirror.ghproxy.com/" + GH,
]
EXPECT = 974_000_00  # 约 97.4MB，仅用于进度显示


def human(n: int) -> str:
    return "%.1f MB" % (n / 1024 / 1024)


def try_url(url: str, timeout: int = 60) -> bool:
    os.makedirs(DEST_DIR, exist_ok=True)
    have = os.path.getsize(DEST) if os.path.exists(DEST) else 0
    headers = {"User-Agent": "Mozilla/5.0"}
    if have:
        headers["Range"] = "bytes=%d-" % have
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = r.headers.get("Content-Length")
            total = int(total) + have if total else None
            mode = "ab" if have and r.status == 206 else "wb"
            if mode == "wb":
                have = 0
            t0 = time.time()
            with open(DEST, mode) as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    have += len(chunk)
                    if int(time.time() - t0) % 10 == 0:
                        pass
            size = os.path.getsize(DEST)
            print(f"  完成 {url[:60]} → {human(size)}")
            return size > 50 * 1024 * 1024
    except Exception as exc:  # noqa: BLE001
        print(f"  失败 {url[:60]} → {type(exc).__name__}: {str(exc)[:80]}")
        return False


if os.path.exists(DEST) and os.path.getsize(DEST) > 50 * 1024 * 1024:
    print("已存在:", DEST, human(os.path.getsize(DEST)))
    raise SystemExit(0)

for u in MIRRORS:
    print("尝试:", u[:70])
    if try_url(u):
        print("OK →", DEST)
        raise SystemExit(0)
    # 换下一个镜像前，把已下载的部分留着续传
print("全部镜像失败")
raise SystemExit(1)
