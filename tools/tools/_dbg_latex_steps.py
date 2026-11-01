# -*- coding: utf-8 -*-
"""逐步跟踪 latex_to_plain 处理带分数那一步。"""
import re
import sys

sys.path.insert(0, r"E:\OCR\app")

import ocr  # noqa: E402

S = r"## $ \pi $; -100; $ -2\frac{5}{7} $"
print("最终:", repr(ocr.latex_to_plain(S)))

t = S
t = re.sub(r"<[^>]+>", "", t)
t = t.replace("&nbsp;", " ").replace("$$", "$")
print("步骤1:", repr(t))
t2 = re.sub(r"(-?\d+)\s*\\(?:d|t)?frac\s*\{\s*([^{}]*?)\s*\}\s*\{\s*([^{}]*?)\s*\}",
            r"\1 \2/\3", t)
print("步骤2:", repr(t2))
t = t2
t2 = re.sub(r"\\(?:d|t)?frac\s*\{\s*([^{}]*?)\s*\}\s*\{\s*([^{}]*?)\s*\}", r"\1/\2", t)
print("步骤3:", repr(t2))
