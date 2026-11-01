# -*- coding: utf-8 -*-
"""GLM-OCR（layout_parsing）在 9 张真实裁切图上的识别质量与耗时，含 Markdown/LaTeX 后处理。"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, r"E:\OCR\app")
from normalize import is_equivalent  # noqa: E402

KEY = "8a047fb66ec54b42bc1edde3f9257c0e.O0bgUyqGnFVWh2BP"
URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"

CASES = [
    ("1-1.png", "0.6, (2011)/(2012), 368"),
    ("1-2.png", "-π, -100, -2(5)/(7)"),
    ("2-1.png", "-6"),
    ("3-1.png", "99"),
    ("3-2.png", "(2026)/(4053)"),
    ("4-1.png", "x=3"),
    ("4-2.png", "x=(3)/(2)"),
    ("4-3.png", "x=11"),
    ("4-4.png", "y=(4)/(5)"),
]

LATEX = {
    r"\frac": "", r"\pi": "π", r"\times": "×", r"\div": "÷", r"\left": "", r"\right": "",
    r"\cdot": "×", r"\sqrt": "sqrt", r"\,": " ", r"\;": " ", r"\!": "", r"\%": "%",
}


def to_plain(md: str) -> str:
    """把 GLM-OCR 的 Markdown/LaTeX 输出压成"人能读的一行答案"。"""
    t = md or ""
    t = re.sub(r"<[^>]+>", "", t)                 # div 等标签
    t = t.replace("&nbsp;", " ")
    t = t.replace("$$", "$")
    # \frac{a}{b} → a/b（可嵌套一层）
    for _ in range(3):
        t2 = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1/\2", t)
        if t2 == t:
            break
        t = t2
    for k, v in LATEX.items():
        t = t.replace(k, v)
    t = t.replace("$", "").replace("\\", "")
    t = re.sub(r"^\s*#+\s*", "", t)               # markdown 标题
    t = re.sub(r"[ \t]+", " ", t)
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    return ", ".join(lines) if len(lines) > 1 else (lines[0] if lines else "")


def ocr(path):
    raw = open(path, "rb").read()
    data = "data:image/png;base64," + base64.b64encode(raw).decode()
    body = json.dumps({"model": "glm-ocr", "file": data}, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": KEY})
    t0 = time.time()
    try:
        obj = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
    except urllib.error.HTTPError as e:
        return time.time() - t0, "", f"HTTP {e.code}"
    dt = time.time() - t0
    parts = []
    for page in (obj.get("layout_details") or []):
        for blk in (page or []):
            c = blk.get("content")
            if isinstance(c, str) and c.strip():
                parts.append(c)
    return dt, "\n".join(parts), None


ok_n, total = 0, 0.0
for fname, expected in CASES:
    p = os.path.join(SUB, fname)
    if not os.path.isfile(p):
        continue
    dt, raw_md, err = ocr(p)
    total += dt
    plain = to_plain(raw_md)
    ok, why = (False, err or "空")
    if plain:
        ok, why = is_equivalent(plain, expected, "填空")
    ok_n += 1 if ok else 0
    print(f"{'✔' if ok else '✘'} {fname:9s} {dt:5.1f}s  原文 {raw_md.strip()[:40]!r}")
    print(f"           → 压平 {plain!r:30.30} 期望 {expected!r}")
print(f"\nGLM-OCR：{ok_n}/{len(CASES)} 判对，合计 {total:.1f}s")
