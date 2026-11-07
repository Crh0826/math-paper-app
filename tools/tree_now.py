# -*- coding: utf-8 -*-
"""打印 E:\\OCR 的目录结构（深度 2，带体积/文件数，题库只报数量）。"""
import os

ROOT = r"E:\OCR"


def size_of(p: str) -> tuple:
    total, n = 0, 0
    for root, _dirs, names in os.walk(p):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                n += 1
            except OSError:
                pass
    return total, n


def mb(b: int) -> str:
    if b >= 1024 ** 3:
        return "%.2f GB" % (b / 1024 ** 3)
    if b >= 1024 ** 2:
        return "%.0f MB" % (b / 1024 ** 2)
    if b >= 1024:
        return "%.0f KB" % (b / 1024)
    return "%d B" % b


SKIP_CHILDREN = {".agent-teams", "__pycache__", ".pytest_cache", "node_modules"}

print("E:\\OCR")
entries = sorted(os.listdir(ROOT))
dirs = [e for e in entries if os.path.isdir(os.path.join(ROOT, e))]
files = [e for e in entries if os.path.isfile(os.path.join(ROOT, e))]

for d in dirs:
    p = os.path.join(ROOT, d)
    if d in SKIP_CHILDREN:
        continue
    total, n = size_of(p)
    note = ""
    if d == "qbank":
        note = "  ← 题库：%d 份 docx（只读）" % len([x for x in os.listdir(p) if x.endswith(".docx")])
    print(f"├── {d}/  [{mb(total)} · {n} 文件]{note}")
    if d in ("qbank", ".tools", "_archive"):
        continue
    children = sorted(os.listdir(p))
    subdirs = [c for c in children if os.path.isdir(os.path.join(p, c)) and c not in SKIP_CHILDREN]
    subfiles = [c for c in children if os.path.isfile(os.path.join(p, c))]
    show_max = 12
    for c in subdirs[:show_max]:
        cp = os.path.join(p, c)
        t2, n2 = size_of(cp)
        print(f"│   ├── {c}/  [{mb(t2)} · {n2}]")
    if len(subdirs) > show_max:
        print(f"│   └── …还有 {len(subdirs) - show_max} 个子目录")
    for c in subfiles[:show_max]:
        cp = os.path.join(p, c)
        print(f"│   ├── {c}  [{mb(os.path.getsize(cp))}]")
    if len(subfiles) > show_max:
        print(f"│   └── …还有 {len(subfiles) - show_max} 个文件")

for f in files:
    print(f"├── {f}  [{mb(os.path.getsize(os.path.join(ROOT, f)))}]")
