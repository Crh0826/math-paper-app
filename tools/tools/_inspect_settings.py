# -*- coding: utf-8 -*-
"""看 settings 保存处理里 model 字段怎么取的，并复现"填了 vlm 的模型却报 paddle 模型错"。"""
import re

src = open(r"E:\OCR\app\server.py", encoding="utf-8").read().splitlines()
for n, l in enumerate(src, 1):
    if re.search(r'def _text\(key|_text\("model"\)|def settings_save|paddle_vl_model|vlm_model', l):
        print(n, l.rstrip()[:120])
