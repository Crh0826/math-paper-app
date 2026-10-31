# -*- coding: utf-8 -*-
"""分组跑全量测试（解决单条 discover 撞 runner 超时被杀的问题）。

用法：
    python tools/run_tests.py            # 默认 6 组
    python tools/run_tests.py --groups 4
    python tools/run_tests.py --only test_grade test_card

每组建一个子进程跑 `python -m unittest <模块...>`，输出各自的 exit code 与失败清单；
任何一组非 0 → 整体 exit 1。组的划分按"模块数均分"，让每组耗时接近。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(APP, "tests")


def test_modules() -> list[str]:
    mods = []
    for name in sorted(os.listdir(TESTS)):
        if name.startswith("test_") and name.endswith(".py"):
            mods.append("tests." + name[:-3])
    return mods


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=6)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    mods = args.only or test_modules()
    if not mods:
        print("没有找到测试模块")
        return 1

    n = max(1, min(args.groups, len(mods)))
    groups = [[] for _ in range(n)]
    for i, m in enumerate(mods):
        groups[i % n].append(m)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=APP)
    total_fail = 0
    started = time.time()
    summary = []
    for gi, group in enumerate(groups, 1):
        t0 = time.time()
        cmd = [sys.executable, "-m", "unittest"] + group
        r = subprocess.run(cmd, cwd=APP, env=env, capture_output=True, text=True, errors="ignore")
        dt = time.time() - t0
        tail = (r.stderr or "").strip().splitlines()
        ran = next((ln for ln in reversed(tail) if ln.startswith("Ran ")), "?")
        result = "OK" if r.returncode == 0 else "FAIL"
        if r.returncode != 0:
            total_fail += 1
        summary.append((gi, len(group), result, ran, dt))
        print(f"[组 {gi}/{n}] {result}  {ran}  用时 {dt:.1f}s  （{len(group)} 个模块）")
        if r.returncode != 0:
            fails = [ln for ln in tail if ln.startswith(("FAIL:", "ERROR:"))]
            for ln in fails[:20]:
                print("    ", ln)
            if args.verbose:
                print((r.stderr or "")[-3000:])

    print("\n=== 汇总 ===")
    for gi, cnt, result, ran, dt in summary:
        print(f"  组 {gi}: {result:4s} {ran:30s} {dt:7.1f}s")
    print(f"总用时 {time.time() - started:.1f}s，失败组数 {total_fail}/{n}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
