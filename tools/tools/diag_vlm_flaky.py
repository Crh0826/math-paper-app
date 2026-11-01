# -*- coding: utf-8 -*-
"""3-2 分数框为什么时而识别不出：对比 JPEG/PNG、单次/多次。"""
import os
import sys
import time

sys.path.insert(0, r"E:\OCR\app")
os.chdir(r"E:\OCR\app")

import ocr  # noqa: E402

SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-103711-b813ae"
CFG = r"E:\OCR\app\config.json"
src = os.path.join(SUB, "3-2.png")

rec = ocr.Recognizer(backend="vlm", config_path=CFG, use_env=False, timeout_s=120)
for i in range(3):
    t0 = time.time()
    r = rec.read(src, "填空")
    print(f"read#{i+1}: {time.time()-t0:5.1f}s text={r.get('text')!r} backend={r.get('backend')} "
          f"err={str(rec.last_error)[:60]}")

# 直接看送出去的东西多大
mime, b64 = rec._vlm_image_b64(src, 2.0)
print(f"\n放大2倍后：mime={mime} base64 长度={len(b64)} （约 {len(b64)*3/4/1024:.0f} KB）")
mime1, b641 = rec._vlm_image_b64(src, 1.0)
print(f"原图：       mime={mime1} base64 长度={len(b641)} （约 {len(b641)*3/4/1024:.0f} KB）")
