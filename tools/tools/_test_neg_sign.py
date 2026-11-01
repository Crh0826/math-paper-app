# -*- coding: utf-8 -*-
"""负号丢失试验：调用参数/提示词怎么调才能稳定保住 `-`。

在 venv_formula 里跑。
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import torch  # noqa: E402
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration  # noqa: E402
from qwen_vl_utils import process_vision_info  # noqa: E402

MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"
IMGS = [("1-2.png", "-π, -100, -2 5/7"), ("2-1.png", "-6"), ("1-1.png", "0.6, 2011/2012, 368")]

BASE = ("识别图中手写的数学答案，只输出答案本身。"
        "分数写成 a/b，带分数写成 A B/C，保留 x= 这样的变量前缀，多个答案用逗号分隔。"
        "看不懂就输出空。")
NEG = BASE + "特别注意：负号 - 一定要保留（例如 -6、-π），不要丢掉。"

CONFIGS = [
    ("min256 + 基础提示", BASE, 256),
    ("min256 + 强调负号", NEG, 256),
    ("min512 + 强调负号", NEG, 512),
]

torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))
t0 = time.time()
model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
    MODEL, torch_dtype=torch.float32, low_cpu_mem_usage=True)
model.eval()
print(f"[加载] {time.time()-t0:.1f}s", flush=True)

for name, prompt, minp in CONFIGS:
    proc = AutoProcessor.from_pretrained(MODEL, min_pixels=minp * 28 * 28,
                                         max_pixels=1024 * 28 * 28)
    print(f"\n=== {name} ===", flush=True)
    for fname, expect in IMGS:
        p = os.path.join(SUB, fname)
        msgs = [{"role": "user", "content": [{"type": "image", "image": p},
                                             {"type": "text", "text": prompt}]}]
        text_in = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        imgs, vids = process_vision_info(msgs)
        inputs = proc(text=[text_in], images=imgs, videos=vids, padding=True, return_tensors="pt")
        t1 = time.time()
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=48, do_sample=False)
        ans = proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
        print(f"  {fname:9s} {time.time()-t1:5.1f}s → {ans.strip()!r:30.30} 期望 {expect!r}",
              flush=True)
