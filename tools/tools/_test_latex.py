# -*- coding: utf-8 -*-
"""验证 LaTeX/Markdown → 纯答案 的转换（GLM-OCR 输出格式）。"""
import sys

sys.path.insert(0, r"E:\OCR\app")

from ocr import latex_to_plain  # noqa: E402

CASES = [
    r"## 0.6, $ \frac{2011}{2012}, 368 $",
    r"## $ \pi $; -100; $ -2\frac{5}{7} $",
    '<div align="center">\n\n# x=3/2\n\n</div>',
    "$$\nx = \\frac {3}{2}\n$$",
    "$$\n0. 6, \\frac {2 0 1 1}{2 0 1 2}, 3 6 8\n$$",
    "---",
    r"$$\ny = \frac {4}{5}\n$$",
    "x=11",
]
for t in CASES:
    print(f"{t[:44]!r:50s} -> {latex_to_plain(t)!r}")
