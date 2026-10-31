# -*- coding: utf-8 -*-
"""语料级统计：公式编码分布（OMML / OLE-WMF / 混合）、图片与表格规模。只读。"""
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter

ROOT = r"E:\OCR"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)


def main():
    t0 = time.time()
    c = Counter()
    embed_per_file = Counter()
    total_media = 0
    total_media_bytes = 0
    rows = []
    for i in range(1, 22899):
        p = os.path.join(ROOT, f"{i:05d}.docx")
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                xml = z.read("word/document.xml")
                media = [n for n in names if n.startswith("word/media/")]
                total_media += len(media)
                for m in media:
                    total_media_bytes += z.getinfo(m).file_size
        except Exception as e:  # noqa: BLE001
            c["open_error"] += 1
            rows.append({"no": f"{i:05d}", "error": str(e)})
            continue
        has_omml = b"<m:oMath" in xml
        has_ole = b"Equation.DSMT4" in xml
        has_blip = b"<a:blip" in xml
        has_tbl = b"<w:tbl>" in xml
        key = ("omml" if has_omml else "") + ("+ole" if has_ole else "")
        c[key or "none"] += 1
        c["blip"] += has_blip
        c["table"] += has_tbl
        c["media_file"] += bool(media)
        embed_per_file[len(media)] += 1
        rows.append({"no": f"{i:05d}", "omml": has_omml, "ole": has_ole,
                     "blip": has_blip, "tbl": has_tbl, "media": len(media)})

    dt = time.time() - t0
    res = {
        "files": len(rows),
        "elapsed_s": round(dt, 2),
        "encoding": {k: v for k, v in c.items()},
        "media_files_total": total_media,
        "media_mb_total": round(total_media_bytes / 1024 / 1024, 1),
        "media_count_hist": dict(sorted(embed_per_file.items())[:12]),
    }
    json.dump(res, open(os.path.join(OUT, "encoding_stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    json.dump(rows, open(os.path.join(OUT, "encoding_rows.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
