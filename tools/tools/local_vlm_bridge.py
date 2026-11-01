# -*- coding: utf-8 -*-
"""本地视觉模型推理桥（跑在 E:\\OCR\\.tools\\venv_formula 里）。

主程序通过 subprocess 调它，避免把 torch/transformers 塞进主环境。

stdin（JSON）:  {"images": ["a.png", ...], "model": "Qwen/Qwen2.5-VL-3B-Instruct", "prompt": "..."}
stdout（JSON）: {"ok": true, "model_load_s": 8.9,
                 "results": [{"path": "...", "text": "...", "elapsed_s": 9.0, "error": null}]}

实测（i5-12490F，CPU，2026-11-01）：9 张真实作答裁切 → 9/9 判对，平均 9.0s/张。
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

DEFAULT_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
DEFAULT_PROMPT = (
    "识别图中手写的数学答案，只输出答案本身。"
    "分数写成 a/b，带分数写成 A B/C，保留 x= 这样的变量前缀，多个答案用逗号分隔。"
    "特别注意：负号 - 一定要保留（例如 -6、-π），不要丢掉。"
    "看不懂就输出空。"
)


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        req = {}
    images = req.get("images") or []
    if not images:
        print(json.dumps({"ok": False, "error": "没有图片", "results": []}, ensure_ascii=False))
        return 2
    model_id = req.get("model") or DEFAULT_MODEL
    prompt = req.get("prompt") or DEFAULT_PROMPT
    max_new = int(req.get("max_new_tokens") or 48)

    out = {"ok": True, "model_load_s": 0.0, "results": []}
    try:
        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
        from qwen_vl_utils import process_vision_info

        torch.set_num_threads(max(1, (os.cpu_count() or 4) - 1))
        t0 = time.time()
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_id, torch_dtype=torch.float32, low_cpu_mem_usage=True)
        model.eval()
        proc = AutoProcessor.from_pretrained(
            model_id, min_pixels=256 * 28 * 28, max_pixels=1024 * 28 * 28)
        out["model_load_s"] = round(time.time() - t0, 2)
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "results": []}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    for path in images:
        item = {"path": path, "text": "", "elapsed_s": 0.0, "error": None}
        try:
            messages = [{"role": "user", "content": [
                {"type": "image", "image": path},
                {"type": "text", "text": prompt}]}]
            text_in = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = proc(text=[text_in], images=image_inputs, videos=video_inputs,
                          padding=True, return_tensors="pt")
            t1 = time.time()
            with torch.no_grad():
                gen = model.generate(**inputs, max_new_tokens=max_new, do_sample=False)
            new_tokens = gen[:, inputs["input_ids"].shape[1]:]
            item["text"] = proc.batch_decode(new_tokens, skip_special_tokens=True)[0].strip()
            item["elapsed_s"] = round(time.time() - t1, 2)
        except Exception as exc:  # noqa: BLE001
            item["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["results"].append(item)

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
