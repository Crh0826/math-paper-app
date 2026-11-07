# -*- coding: utf-8 -*-
"""在独立 venv（E:\\OCR\\.tools\\venv_formula）里跑 pix2tex 推理的桥。

主程序用 subprocess 调它，避免把 torch/albumentations 塞进主环境。

输入（stdin，JSON）：
    {"images": ["a.png", "b.png"], "timeout_s": 300}
输出（stdout，JSON，一行）：
    {"ok": true, "results": [{"path": "a.png", "latex": "...", "elapsed_s": 0.8}]}
"""
from __future__ import annotations

import json
import sys
import time
import traceback


def main() -> int:
    raw = sys.stdin.read()
    try:
        req = json.loads(raw or "{}")
    except json.JSONDecodeError:
        req = {}
    images = req.get("images") or []
    if not images:
        print(json.dumps({"ok": False, "error": "没有图片"}, ensure_ascii=False))
        return 2

    out = {"ok": True, "results": [], "model_load_s": 0.0}
    try:
        from PIL import Image
        t0 = time.time()
        from pix2tex.cli import LatexOCR
        model = LatexOCR()
        out["model_load_s"] = round(time.time() - t0, 2)
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
               "trace": traceback.format_exc()[-800:], "results": []}
        print(json.dumps(out, ensure_ascii=False))
        return 1

    for path in images:
        item = {"path": path, "latex": "", "elapsed_s": 0.0, "error": None}
        try:
            with Image.open(path) as im:
                im = im.convert("RGB")
                t1 = time.time()
                latex = model(im)
            item["latex"] = (latex or "").strip()
            item["elapsed_s"] = round(time.time() - t1, 2)
        except Exception as exc:  # noqa: BLE001
            item["error"] = "%s: %s" % (type(exc).__name__, exc)
        out["results"].append(item)

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
