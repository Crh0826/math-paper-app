# -*- coding: utf-8 -*-
"""本地 CPU 跑 Qwen2.5-VL-3B 识别答题卡裁切（路线一最后一个候选）。"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"
OUT = r"E:\OCR\app\out\_qwen25vl.json"
CASES = [
    ("1-1.png", "0.6, (2011)/(2012), 368"),
    ("1-2.png", "-π, -100, -2(5)/(7)"),
    ("2-1.png", "-6"),
    ("3-1.png", "99"),
    ("3-2.png", "(2026)/(4053)"),
    ("4-1.png", "x=3"),
    ("4-2.png", "x=(3)/(2)"),
    ("4-3.png", "x=11"),
    ("4-4.png", "y=(4)/(5)"),
]

PROMPT = ("识别图中手写的数学答案，只输出答案本身。分数写成 a/b，带分数写成 A B/C，"
          "保留 x= 这样的前缀，多个答案用逗号分隔。看不懂就输出空。")


def main() -> int:
    from huggingface_hub import snapshot_download

    t0 = time.time()
    path = snapshot_download(MODEL, allow_patterns=["*.json", "*.py", "*.safetensors",
                                                    "*.txt", "*.model"])
    print(f"[权重] {time.time()-t0:.1f}s → {path}", flush=True)

    import torch
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

    t1 = time.time()
    torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        path, torch_dtype=torch.float32, low_cpu_mem_usage=True)
    model.eval()
    proc = AutoProcessor.from_pretrained(path, min_pixels=256 * 28 * 28, max_pixels=768 * 28 * 28)
    print(f"[加载] {time.time()-t1:.1f}s", flush=True)

    results = []
    for fname, expect in CASES:
        p = os.path.join(SUB, fname)
        if not os.path.isfile(p):
            continue
        t2 = time.time()
        try:
            messages = [{"role": "user", "content": [
                {"type": "image", "image": p},
                {"type": "text", "text": PROMPT}]}]
            text_in = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            from qwen_vl_utils import process_vision_info  # 可能没装
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = proc(text=[text_in], images=image_inputs, videos=video_inputs,
                          padding=True, return_tensors="pt")
            with torch.no_grad():
                out = model.generate(**inputs, max_new_tokens=48, do_sample=False)
            gen = out[:, inputs["input_ids"].shape[1]:]
            text = proc.batch_decode(gen, skip_special_tokens=True)[0].strip()
        except Exception as exc:  # noqa: BLE001
            text = ""
            print("  报错:", type(exc).__name__, str(exc)[:140], flush=True)
        results.append({"file": fname, "expect": expect, "text": text,
                        "elapsed_s": round(time.time() - t2, 2)})
        print(f"  {fname:9s} {results[-1]['elapsed_s']:6.2f}s → {text[:50]!r}", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("结果:", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
