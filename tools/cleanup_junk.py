# -*- coding: utf-8 -*-
"""删掉 E:\\OCR 里不该留的东西（历史产物 / 中间件 / 旧包 / 测试输出），打印释放空间。"""
from __future__ import annotations

import os
import shutil

ROOT = r"E:\OCR"
APP = os.path.join(ROOT, "app")

DIRS = [
    os.path.join(ROOT, "_archive"),
    os.path.join(ROOT, "_work"),
    os.path.join(ROOT, "build"),
    os.path.join(ROOT, "packages"),
    os.path.join(ROOT, "dedup"),
    os.path.join(ROOT, "plan"),
    os.path.join(ROOT, "__pycache__"),
    os.path.join(ROOT, "promo", "build"),
    os.path.join(APP, "__pycache__"),
    os.path.join(APP, "tests", "__pycache__"),
    os.path.join(APP, "data", "snapshots"),
    os.path.join(APP, "out", "_ocr_probe"),
    os.path.join(APP, "out", "_diag"),
    os.path.join(APP, "tools", "logs"),
]

FILES = [
    os.path.join(ROOT, "dist", "数学题库工作台-v1.1.zip"),
    os.path.join(ROOT, "dist", "数学题库工作台-v1.2.zip"),
    os.path.join(ROOT, "dist", "math-paper-app-v1.2-portable.zip"),
    os.path.join(ROOT, "dist", "math-paper-app-v1.2.1-portable.zip"),
    os.path.join(ROOT, "dist", "数学题库工作台"),          # 解压目录（会重新生成）
]


def dir_size(p: str) -> int:
    total = 0
    for root, _d, names in os.walk(p):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def main() -> int:
    freed = 0
    print("=== 删目录 ===")
    for p in DIRS:
        if os.path.isdir(p):
            sz = dir_size(p)
            shutil.rmtree(p, ignore_errors=True)
            ok = not os.path.isdir(p)
            print(f"  {'✔' if ok else '✘'} {p}  ({sz/1024/1024:.0f} MB)")
            if ok:
                freed += sz

    print("\n=== 删文件 ===")
    for p in FILES:
        if os.path.isdir(p):
            sz = dir_size(p)
            shutil.rmtree(p, ignore_errors=True)
            print(f"  ✔ {p}（目录） ({sz/1024/1024:.0f} MB)")
            freed += sz
        elif os.path.isfile(p):
            sz = os.path.getsize(p)
            try:
                os.remove(p)
                print(f"  ✔ {os.path.basename(p)} ({sz/1024/1024:.1f} MB)")
                freed += sz
            except OSError as exc:
                print(f"  ✘ {p}: {exc}")

    print("\n=== 删测试判卷产物 out/P2026-* ===")
    out = os.path.join(APP, "out")
    n = 0
    if os.path.isdir(out):
        for name in os.listdir(out):
            p = os.path.join(out, name)
            if os.path.isdir(p) and name.startswith("P2026"):
                sz = dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                freed += sz
                n += 1
    print(f"  删了 {n} 个历史卷目录")

    print("\n=== 删日志/临时 ===")
    for folder in (APP, os.path.join(APP, "data")):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.endswith((".log",)):
                continue
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                sz = os.path.getsize(p)
                try:
                    os.remove(p)
                    freed += sz
                    print(f"  ✔ {name} ({sz/1024:.0f} KB)")
                except OSError:
                    pass

    print(f"\n合计释放 {freed/1024/1024:.0f} MB")
    print("\n=== 剩下的一级目录 ===")
    for name in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, name)
        if os.path.isdir(p):
            print(f"  {name}/")
        else:
            print(f"  {name}  ({os.path.getsize(p)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
