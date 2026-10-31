# -*- coding: utf-8 -*-
"""最小复现：render._move_into_place 在「源在 C:\\Temp、目标在 E:\\」时必定失败。

WinError 17 = ERROR_NOT_SAME_DEVICE：os.replace 在 Windows 上跨卷不复制，
MoveFileEx 没有 MOVEFILE_COPY_ALLOWED → 跨盘直接失败。shutil.move 才带 copy 回退。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

APP = r"E:\OCR\app"
sys.path.insert(0, APP)
import render   # noqa: E402

print("render.py mtime:", time.strftime("%H:%M:%S", time.localtime(
    os.path.getmtime(os.path.join(APP, "render.py")))),
    "sha1:", hashlib.sha1(open(os.path.join(APP, "render.py"), "rb").read()).hexdigest()[:10])
print("tempdir:", tempfile.gettempdir())

src_dir = tempfile.mkdtemp(prefix="repro-cross-vol-")
src = os.path.join(src_dir, "out.pdf")
open(src, "wb").write(b"%PDF-1.4\n%%EOF\n")
dst_dir = r"E:\OCR\app\out\acceptance_v1\repro"
os.makedirs(dst_dir, exist_ok=True)
dst = os.path.join(dst_dir, "cross_vol.pdf")
if os.path.exists(dst):
    os.remove(dst)

print("src drive:", os.path.splitdrive(src)[0], "| dst drive:", os.path.splitdrive(dst)[0])
try:
    render._move_into_place(src, dst, retries=1, delay=0.01)
    print("RESULT: os.replace 成功（与假设不符）")
except Exception as e:                                  # noqa: BLE001
    print("RESULT: 失败 ->", type(e).__name__, str(e)[:200])
    print("  winerror:", getattr(e, "__cause__", None) and getattr(e.__cause__, "winerror", None))

# 同一个源 → 同样的目标，用 shutil.move 就能落位
try:
    if not os.path.exists(src):
        open(src, "wb").write(b"%PDF-1.4\n%%EOF\n")
    shutil.move(src, dst)
    print("shutil.move: 成功落位 ->", dst, os.path.getsize(dst), "B")
except Exception as e:                                  # noqa: BLE001
    print("shutil.move 也失败：", e)
