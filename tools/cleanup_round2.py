# -*- coding: utf-8 -*-
"""第二轮清理：删掉可再生的中间件与旧版本包（保留最新包、解压目录、题库、文档、仓库）。"""
from __future__ import annotations

import os
import shutil

ROOT = r"E:\OCR"
DIST = os.path.join(ROOT, "dist")
APP = os.path.join(ROOT, "app")

KEEP_ZIP = "数学题库工作台-v1.2.4.zip"


def size_of(p: str) -> int:
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for root, _d, names in os.walk(p):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def drop(p: str) -> None:
    if not os.path.exists(p):
        return
    sz = size_of(p)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            os.remove(p)
        except OSError as exc:
            print("  ! 删不掉", p, exc)
            return
    print("  ✔ %-58s %6.0f MB" % (os.path.basename(p), sz / 1024 / 1024))


print("1) PyInstaller 中间件")
drop(os.path.join(ROOT, "build"))

print("2) 旧版本包 / 重复包（保留 %s 与解压目录）" % KEEP_ZIP)
if os.path.isdir(DIST):
    for n in os.listdir(DIST):
        p = os.path.join(DIST, n)
        if os.path.isfile(p) and n.endswith(".zip") and n != KEEP_ZIP:
            drop(p)

print("3) 空目录 / 备份")
drop(os.path.join(APP, "qbank"))          # 空壳（题库在 E:\OCR\qbank）
drop(os.path.join(ROOT, "_backups"))      # 队友改代码前留的副本，git 里都有

print("4) app/out 里的测试卷（保留 delivery / demo / compact* / quality_baseline）")
out = os.path.join(APP, "out")
keep = {"delivery", "demo", "compact", "compact_card", "quality_baseline"}
if os.path.isdir(out):
    for n in sorted(os.listdir(out)):
        p = os.path.join(out, n)
        if os.path.isdir(p) and n not in keep:
            drop(p)
        elif os.path.isfile(p):
            drop(p)

print("5) __pycache__")
for base, dirs, _files in os.walk(ROOT):
    for d in list(dirs):
        if d == "__pycache__":
            drop(os.path.join(base, d))
            dirs.remove(d)
