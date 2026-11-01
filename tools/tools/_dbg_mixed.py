# -*- coding: utf-8 -*-
"""定位带分数 A\\frac{B}{C} 为什么没被识别成 A B/C。"""
import re

S = r"## $ \pi $; -100; $ -2\frac{5}{7} $"
MIX = r"(-?\d+)\s*\\(?:d|t)?frac\s*\{\s*([^{}]*?)\s*\}\s*\{\s*([^{}]*?)\s*\}"
print("原文:", S)
print("混合匹配:", re.findall(MIX, S))
sub = re.sub(MIX, r"\1 \2/\3", S)
print("替换后:", sub)

for name, pat in [
    ("不带可选 d/t", r"(-?\d+)\s*\\frac\s*\{\s*([^{}]*?)\s*\}\s*\{\s*([^{}]*?)\s*\}"),
    ("宽松", r"(-?\d+)\s*\\\w*frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}"),
]:
    print(name, "→", re.findall(pat, S))
