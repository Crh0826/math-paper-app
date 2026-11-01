# -*- coding: utf-8 -*-
"""调试：为什么 x=3 2 vs x=(3)/(2) 没走到分数对齐分支。"""
import sys

sys.path.insert(0, r"E:\OCR\app")

import normalize as N

sa, sb = "x=3 2", "x=(3)/(2)"
pa, pb = N._pre(sa), N._pre(sb)
print("归一化后:", repr(pa.text), "|", repr(pb.text))
ca, cb = N._split_multi(pa.text), N._split_multi(pb.text)
print("拆多值:", ca, cb)
print("_FRAC_RE 命中 b:", bool(N._FRAC_RE.match(pb.text.strip())))
print("_FRAC_RE 命中 (3)/(2):", N._FRAC_RE.match("(3)/(2)"))
r = N._cmp_body(pa, pb)
print("_cmp_body 结果:", r)
print("is_equivalent:", N.is_equivalent(sa, sb, "填空"))
