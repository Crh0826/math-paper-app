# -*- coding: utf-8 -*-
"""把 6 个自写导航的模板统一成 include _nav.html，并让 base.html 也用它。

用法：python tools/unify_nav.py [--apply]
"""
import io
import os
import re
import sys

APP = r"E:\OCR\app"
TPL = os.path.join(APP, "templates")
APPLY = "--apply" in sys.argv

# 文件 → nav_active 值
TARGETS = {
    "grade_upload.html": "grade",
    "grade_result.html": "grade",
    "grade_records.html": "records",
    "grade_why.html": "grade",
    "grade_error.html": "grade",
    "import.html": "import",
}

NAV_RE = re.compile(r"<nav class=\"nav\"[^>]*>.*?</nav>", re.S)

changed = []
for fname, active in TARGETS.items():
    path = os.path.join(TPL, fname)
    if not os.path.isfile(path):
        print(f"  跳过（不存在）{fname}")
        continue
    src = io.open(path, encoding="utf-8").read()
    m = NAV_RE.search(src)
    if not m:
        print(f"  跳过（没找到 <nav>）{fname}")
        continue
    block = '{% set nav_active = "' + active + '" %}\n      {% include "_nav.html" %}'
    new = src[:m.start()] + block + src[m.end():]
    if new == src:
        print(f"  无变化 {fname}")
        continue
    changed.append((fname, active, m.group(0).count("<a")))
    if APPLY:
        io.open(path, "w", encoding="utf-8").write(new)

print("\n=== 计划 ===")
for fname, active, n_a in changed:
    print(f"  {fname:24s} nav_active={active:8s} 原导航 {n_a} 个链接 → include _nav.html")

# base.html：导航块整体换成 include（它的 key 分流逻辑并入 _nav.html）
base_path = os.path.join(TPL, "base.html")
bsrc = io.open(base_path, encoding="utf-8").read()
bm = NAV_RE.search(bsrc)
if bm:
    print(f"\n  base.html 原导航 {bm.group(0).count('<a')} 个链接 → include _nav.html")
    if APPLY:
        bsrc2 = bsrc[:bm.start()] + '{%% include "_nav.html" %%}' + bsrc[bm.end():]
        io.open(base_path, "w", encoding="utf-8").write(bsrc2)
else:
    print("\n  base.html 没找到 <nav>")

if not APPLY:
    print("\n[DRY-RUN] 加 --apply 执行")
