# -*- coding: utf-8 -*-
"""把 GOT-OCR2 的硬编码 .cuda() 改成 CPU 可用（仅本地试验用）。"""
import os
import re

F = (r"C:\Users\CRH\.cache\huggingface\hub\models--stepfun-ai--GOT-OCR2_0"
     r"\snapshots\979938bf89ccdc949c0131ddd3841e24578a4742\modeling_GOT.py")

src = open(F, encoding="utf-8").read()
before = src

src = src.replace(".half().cuda()", ".float()")
src = src.replace('.unsqueeze(0).half().cuda()', '.unsqueeze(0).float()')
src = src.replace("torch.as_tensor(inputs.input_ids).cuda()",
                  'torch.as_tensor(inputs.input_ids).to("cpu")')
src = src.replace("image_tensor_1.unsqueeze(0).cuda()", 'image_tensor_1.unsqueeze(0).to("cpu")')
src = src.replace("image_list.half().cuda()", "image_list.float()")
src = re.sub(r"\.cuda\(\)", '.to("cpu")', src)

if src == before:
    print("没有改动（可能已打过补丁）")
else:
    open(F, "w", encoding="utf-8").write(src)
    print("已打补丁；剩余 .cuda() 出现次数:", src.count(".cuda()"))
print("剩余 half() 次数:", src.count(".half()"))
