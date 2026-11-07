# -*- coding: utf-8 -*-
"""分组跑全量测试（解决单条 discover 撞 runner 超时被杀的问题）。

用法：
    python tools/run_tests.py            # 默认 6 组
    python tools/run_tests.py --groups 4
    python tools/run_tests.py --only test_grade test_card

t93：**每个测试模块一个子进程**（``python -m unittest <module>``）。
之前是「一组里跑多个模块」，某个模块导入期异常 / 名称解析失败会把整组记成失败，
失败归因被污染（"看着红了/看着绿了"的根因之一）。现在失败只算到具体模块头上。

每组的汇总行保留：Ran 总用例数 / 用时 / 失败模块名。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(APP, "tests")

_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.M)
_SKIP_RE = re.compile(r"^(OK|FAILED)\b.*skipped=(\d+)", re.M)


def test_modules() -> list[str]:
    mods = []
    for name in sorted(os.listdir(TESTS)):
        if name.startswith("test_") and name.endswith(".py"):
            mods.append("tests." + name[:-3])
    return mods


def run_module(mod: str, env: dict, verbose: bool) -> dict:
    """跑单个测试模块（独立子进程）→ {module, ok, ran, skipped, elapsed, fails, output}。"""
    t0 = time.time()
    r = subprocess.run([sys.executable, "-m", "unittest", mod], cwd=APP, env=env,
                       capture_output=True, text=True, errors="ignore")
    dt = time.time() - t0
    err = r.stderr or ""
    out = r.stdout or ""
    ran = 0
    m = _RAN_RE.search(err) or _RAN_RE.search(out)
    if m:
        ran = int(m.group(1))
    skipped = 0
    ms = _SKIP_RE.search(err) or _SKIP_RE.search(out)
    if ms:
        skipped = int(ms.group(2))
    fails = [ln.strip() for ln in (err + "\n" + out).splitlines()
             if ln.startswith(("FAIL:", "ERROR:"))]
    if verbose and fails:
        print((err or out)[-3000:])
    return {"module": mod, "ok": r.returncode == 0, "ran": ran, "skipped": skipped,
            "elapsed": dt, "fails": fails}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", type=int, default=6)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--verbose", action="store_true")
    #: 失败模块默认再跑一次：有测试读真库，别人并发导入时可能瞬时红（t93）
    ap.add_argument("--retry", type=int, default=1)
    args = ap.parse_args()

    mods = args.only or test_modules()
    if not mods:
        print("没找到测试模块")
        return 1
    n = max(1, args.groups)
    groups = [[] for _ in range(n)]
    for i, m in enumerate(mods):
        groups[i % n].append(m)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=APP)
    started = time.time()
    summary = []
    total_fail_modules = 0
    print("逐模块子进程模式：每个模块一个 python -m unittest <module>\n")
    for gi, group in enumerate(groups, 1):
        t0 = time.time()
        results = []
        for m in group:
            r = run_module(m, env, args.verbose)
            if not r["ok"] and args.retry > 0:
                again = run_module(m, env, args.verbose)
                if again["ok"]:
                    print("    ↻ %s 第一次红、第二次 OK（疑似并发写库带来的瞬时抖动）" % m)
                    again["flaky"] = True
                    r = again
            results.append(r)
        dt = time.time() - t0
        ran = sum(r["ran"] for r in results)
        skipped = sum(r["skipped"] for r in results)
        failed = [r for r in results if not r["ok"]]
        total_fail_modules += len(failed)
        status = "OK" if not failed else "FAIL"
        flaky = [r["module"] for r in results if r.get("flaky")]
        extra = "（skip %d）" % skipped if skipped else ""
        print("[组 %d/%d] %s  共 Ran %d 用例  用时 %.1fs  （%d 个模块）%s"
              % (gi, n, status, ran, dt, len(group), extra))
        if flaky:
            print("    （重跑后转绿的模块：%s）" % "、".join(flaky))
        for r in failed:
            print("    ✗ %s  Ran %d  用时 %.1fs" % (r["module"], r["ran"], r["elapsed"]))
            for ln in r["fails"][:20]:
                print("        " + ln)
        summary.append({"group": gi, "status": status, "ran": ran, "skipped": skipped,
                        "elapsed": dt, "modules": len(group), "failed": failed})

    print("\n=== 汇总 ===")
    total_ran = total_skipped = 0
    for s in summary:
        line = "  组 %d: %-4s Ran %4d 用例  %7.1fs  （%d 模块）" % (
            s["group"], s["status"], s["ran"], s["elapsed"], s["modules"])
        if s["failed"]:
            line += "  失败模块: " + "、".join(r["module"] for r in s["failed"])
        print(line)
        total_ran += s["ran"]
        total_skipped += s["skipped"]
    print("\n总用时 %.1fs，共 %d 用例（skip %d），失败模块数 %d/%d"
          % (time.time() - started, total_ran, total_skipped,
             total_fail_modules, len(mods)))
    return 0 if total_fail_modules == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
