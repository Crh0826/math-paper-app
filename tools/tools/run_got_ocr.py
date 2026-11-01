# -*- coding: utf-8 -*-
"""下载 GOT-OCR2_0 权重（HF 镜像），并跑我们 9 张裁切。

在 venv_formula 里执行：模型约 580M，CPU 推理。
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL = "stepfun-ai/GOT-OCR2_0"
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"
OUT = r"E:\OCR\app\out\_got_ocr.json"
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


def main() -> int:
    from huggingface_hub import snapshot_download

    t0 = time.time()
    path = snapshot_download(MODEL, allow_patterns=["*.json", "*.py", "*.safetensors",
                                                    "*.model", "*.txt", "*.bin"])
    print(f"[下载/缓存] {time.time()-t0:.1f}s → {path}", flush=True)

    import torch
    from transformers import AutoModel, AutoTokenizer

    t1 = time.time()
    tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
    model = AutoModel.from_pretrained(path, trust_remote_code=True, low_cpu_mem_usage=True,
                                      use_safetensors=True, pad_token_id=tok.pad_token_id)
    model = model.eval()
    print(f"[加载模型] {time.time()-t1:.1f}s", flush=True)

    results = []
    for fname, expect in CASES:
        p = os.path.join(SUB, fname)
        if not os.path.isfile(p):
            continue
        t2 = time.time()
        try:
            text = model.chat(tok, p, ocr_type="format")
        except Exception as exc:  # noqa: BLE001
            text = ""
            print("  报错:", type(exc).__name__, str(exc)[:120], flush=True)
        results.append({"file": fname, "expect": expect, "text": (text or "").strip(),
                        "elapsed_s": round(time.time() - t2, 2)})
        print(f"  {fname:9s} {results[-1]['elapsed_s']:5.2f}s → {results[-1]['text'][:60]!r}",
              flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("结果写入", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
