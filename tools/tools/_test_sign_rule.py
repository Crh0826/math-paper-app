# -*- coding: utf-8 -*-
"""复核"只差一个负号 → 待确认"规则（含 π 这种非数字符号）。"""
import sys

sys.path.insert(0, r"E:\OCR\app")

import grade  # noqa: E402

CASES = [
    ("π, -100, -2 5/7", "-π, -100, -2(5)/(7)", "填空"),
    ("6", "-6", "填空"),
    ("7", "-6", "填空"),
    ("-6", "-6", "填空"),
    ("x=3", "x=-3", "解答"),
    ("y = 4/5", "y=(4)/(5)", "解答"),
]
for t, e, q in CASES:
    print(f"{t!r:24s} vs {e!r:24s} [{q}] → {grade._judge(t, e, q)}")
