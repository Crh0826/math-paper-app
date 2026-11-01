# -*- coding: utf-8 -*-
"""对比三家视觉模型在真实答题卡裁切图上的识别质量与耗时。

用法：python tools/compare_vlm.py
（key 从 app/config.json 的 ocr.vlm.key 与下面的 ZHIPU_KEY 读；不打印明文）
"""
import json
import os
import sys
import time

sys.path.insert(0, r"E:\OCR\app")
os.chdir(r"E:\OCR\app")

import ocr  # noqa: E402

ZT_KEY = "8a047fb66ec54b42bc1edde3f9257c0e.O0bgUyqGnFVWh2BP"
CFG = os.path.join(r"E:\OCR\app", "config.json")
SUB = r"E:\OCR\app\out\P20261101-002\sub_S20261101-120053-63e6b9"

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

PROVIDERS = [
    ("deepseek-flash", "https://api.deepseek.com", "deepseek-flash", None),
    ("zhipu glm-4v-flash", "https://open.bigmodel.cn/api/paas/v4", "glm-4v-flash", ZT_KEY),
    ("zhipu glm-4.5v", "https://open.bigmodel.cn/api/paas/v4", "glm-4.5v", ZT_KEY),
]

base_cfg = json.load(open(CFG, encoding="utf-8"))
ds_key = (base_cfg.get("ocr", {}).get("vlm") or {}).get("key") or ""

from normalize import is_equivalent  # noqa: E402

for name, base_url, model, key in PROVIDERS:
    cfg = json.loads(json.dumps(base_cfg))
    cfg["ocr"]["vlm"] = {"base_url": base_url, "model": model, "key": key or ds_key}
    tmp = os.path.join(r"E:\OCR\app\out", "_vlm_cmp.json")
    json.dump(cfg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)

    rec = ocr.Recognizer(backend="vlm", config_path=tmp, use_env=False, timeout_s=120)
    ok_n, total_time, results = 0, 0.0, []
    for fname, expected in CASES:
        p = os.path.join(SUB, fname)
        if not os.path.isfile(p):
            continue
        t0 = time.time()
        r = rec.read(p, "填空")
        dt = time.time() - t0
        total_time += dt
        text = (r.get("text") or "").strip()
        ok = False
        if text and expected:
            ok, _why = is_equivalent(text, expected, "填空")
        ok_n += 1 if ok else 0
        results.append((fname, expected, text, ok, dt))

    print(f"\n=== {name} ===  {ok_n}/{len(results)} 判对 · 合计 {total_time:.1f}s")
    for fname, expected, text, ok, dt in results:
        flag = "✔" if ok else "✘"
        print(f"  {flag} {fname:9s} {dt:5.1f}s  得到 {text!r:26.26} 期望 {expected!r}")
