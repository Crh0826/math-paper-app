# -*- coding: utf-8 -*-
"""用截图里那 9 个真实识别值重判（新旧规则对比）。"""
import sys

sys.path.insert(0, r"E:\OCR\app")

import grade  # noqa: E402

CASES = [
    ("1", "1", "0.6, (2011)/(2012), 368", "0.6, 2011 2012 ,368"),
    ("1", "2", "-π, -100, -2(5)/(7)", "(-100,-2 5 7)"),
    ("2", "1", "-6", "(-6)"),
    ("3", "1", "99", "99"),
    ("3", "2", "(2026)/(4053)", "2026 4053"),
    ("4", "1", "x=3", "x=3"),
    ("4", "2", "x=(3)/(2)", "x=3 2"),
    ("4", "3", "x=11", "x=11"),
    ("4", "4", "y=(4)/(5)", "y=4 5"),
]

correct = wrong = unsure = 0
print(f"{'题-空':<6}{'标准答案':<22}{'识别值':<22}{'判定':<9}原因")
for no, blank, exp, got in CASES:
    v, why = grade._judge(got, exp, "填空" if no in ("1", "2") else "解答")
    print(f"{no}-{blank:<5}{exp:<22.22}{got:<22.22}{v:<9}{why[:30]}")
    if v == "correct":
        correct += 1
    elif v == "wrong":
        wrong += 1
    else:
        unsure += 1
print(f"\n合计：对 {correct} / 错 {wrong} / 待确认 {unsure}（原页面：对 5 / 错 3 / 待确认 1）")
