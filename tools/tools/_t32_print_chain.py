# -*- coding: utf-8 -*-
"""t32 · 起真服验「papers 落库链路 + 打印合并 PDF」。

用一份 **app.db 的副本** + 独立 out 目录起 uvicorn（真 HTTP，不经 TestClient），流程：

1. `POST /papers` 建 10 题卷 → 302 → `/papers/<pid>`
2. 查库：papers 行字段齐全、manifest 里 card 已合并、n_items=10
3. 查盘：`out/<pid>/` 下有 docx / paper.pdf / paper_*.png / card.pdf / card_*.png
4. 合成一张「照片」上传 `POST /submissions` → 302 → 查 submissions 行（证明判卷是从库里取到 manifest）
5. `GET /papers/<pid>/print.pdf`：页数 = 卷面页 + 卡页、**卷面在前卡在后**、页脚页码
6. `GET /papers/<pid>/pdf` 与 `/card.pdf`：Content-Type + 页数
7. 停服、删副本与产物（证据另存到 out/acceptance_v1/）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import cv2
import httpx
import numpy as np
from PIL import Image, ImageDraw

APP = r"E:\OCR\app"
OUT = os.path.join(APP, "out")
EVID = os.path.join(OUT, "acceptance_v1")
RUN = os.path.join(OUT, "acceptance_t32")
REAL_DB = os.path.join(APP, "data", "app.db")

TITLE = "T32 打印链路验收卷"
PID_HINT = None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(client, base, timeout=40.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = client.get(base + "/healthz")
            if r.status_code == 200:
                return True
        except Exception:                               # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def db_rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def pdf_pages(path):
    import pymupdf
    doc = pymupdf.open(path)
    try:
        texts = []
        for page in doc:
            texts.append(page.get_text().strip())
        return doc.page_count, texts
    finally:
        doc.close()


def synth_photo(card_png, manifest, out_jpg, seed=20261031):
    """按 manifest 的 card 几何涂黑正确答案 → 透视 3% + 光照 + JPEG70。"""
    img = cv2.imread(card_png, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError("读不出答题卡：%s" % card_png)
    pil = Image.fromarray(img)
    d = ImageDraw.Draw(pil)
    by_idx = {it["idx"]: it for it in manifest["items"]}
    page = manifest["card"]["pages"][0]
    for it in page["items"]:
        if it["kind"] != "omr":
            continue
        expected = str((by_idx[it["idx"]]["answer_key"][0] or {}).get("value") or "")
        for c in it["cells"]:
            if str(c["opt"]) == expected:
                d.rectangle([c["x"] + 9, c["y"] + 9,
                             c["x"] + c["w"] - 9, c["y"] + c["h"] - 9], fill=0)
    img = np.array(pil)
    h, w = img.shape[:2]
    rng = np.random.RandomState(seed)
    W, H = int(w * 0.72), int(h * 0.72)
    dd = 0.03
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[W * rng.uniform(0, dd), H * rng.uniform(0, dd)],
                      [W * (1 - rng.uniform(0, dd)), H * rng.uniform(0, dd)],
                      [W * (1 - rng.uniform(0, dd)), H * (1 - rng.uniform(0, dd))],
                      [W * rng.uniform(0, dd), H * (1 - rng.uniform(0, dd))]])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, M, (W, H), borderValue=190)
    yy, xx = np.mgrid[0:H, 0:W]
    grad = 0.72 + 0.28 * (xx / max(W - 1, 1)) + 0.22 * (yy / max(H - 1, 1))
    out = np.clip(warped.astype(np.float32) * grad + rng.normal(0, 4, (H, W)), 0, 255)
    out = cv2.GaussianBlur(out.astype(np.uint8), (3, 3), 0)
    cv2.imwrite(out_jpg, out, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return out_jpg


def main() -> int:
    os.makedirs(RUN, exist_ok=True)
    os.makedirs(EVID, exist_ok=True)
    db_copy = os.path.join(RUN, "app_copy.db")
    out_root = os.path.join(RUN, "out")
    os.makedirs(out_root, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = db_copy + suffix
        if os.path.exists(p):
            os.remove(p)
    t_copy = time.time()
    shutil.copyfile(REAL_DB, db_copy)
    copy_s = time.time() - t_copy

    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ,
               PYTHONIOENCODING="utf-8",
               MATH_APP_DB=db_copy,
               MATH_APP_OUT=out_root,
               MATH_APP_CONFIG=os.path.join(APP, "config.json"),
               MATH_NO_BROWSER="1")
    log_path = os.path.join(RUN, "server.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                             "--host", "127.0.0.1", "--port", str(port),
                             "--log-level", "warning"],
                            cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT)
    report = {"pid": None, "port": port, "copy_s": round(copy_s, 2), "steps": {}}
    try:
        with httpx.Client(timeout=180.0, follow_redirects=False) as client:
            up = wait_up(client, base)
            report["steps"]["health_up"] = up
            if not up:
                tail = ""
                try:
                    tail = open(log_path, encoding="utf-8", errors="replace").read()[-2000:]
                except OSError:
                    pass
                report["error"] = "server 没起来：" + tail
                return finish(report, proc, log, RUN)

            # ---- 1) 建卷（Word 被占用时可能降级，重试）
            form = {"title": TITLE, "seed": "42", "n_choice": "6", "n_fill": "2",
                    "n_solution": "2", "need_answer": "on", "kp_mode": "any"}
            pid, tries = None, []
            for attempt in range(3):
                t0 = time.time()
                r = client.post(base + "/papers", data=form)
                cost = round(time.time() - t0, 2)
                loc = r.headers.get("location") or ""
                pid_try = loc.rstrip("/").rsplit("/", 1)[-1] if loc else None
                tries.append({"attempt": attempt + 1, "status": r.status_code,
                              "location": loc, "s": cost})
                if r.status_code == 302 and pid_try:
                    pid = pid_try
                    break
                time.sleep(3)
            report["steps"]["create_paper"] = {"status": 302 if pid else None,
                                               "pid": pid, "tries": tries}
            if not pid:
                report["error"] = "POST /papers 没拿到 pid"
                return finish(report, proc, log, RUN)
            report["pid"] = PID_HINT = pid
            folder = os.path.join(out_root, pid)

            # ---- 2) 查库
            rows = db_rows(db_copy, "SELECT pid, created_at, title, params_json, manifest,"
                                    " docx_path, pdf_path, n_items FROM papers WHERE pid=?",
                           (pid,))
            row = rows[0] if rows else None
            assert row is not None, "papers 表里没有 %s" % pid
            man = json.loads(row["manifest"])
            card = man.get("card") or {}
            pages = card.get("pages") or []
            report["steps"]["db_papers_row"] = {
                "rows": len(rows), "pid": row["pid"], "title": row["title"],
                "created_at": row["created_at"], "n_items": row["n_items"],
                "params_ok": bool(json.loads(row["params_json"] or "{}")),
                "manifest_items": len(man.get("items") or []),
                "docx_path": row["docx_path"], "pdf_path": row["pdf_path"],
                "card_merged": bool(card), "card_pages": len(pages),
                "card_start_page": card.get("start_page"),
                "card_page_labels": [p.get("label") for p in pages],
                "first_item": (man.get("items") or [{}])[0].get("no"),
            }

            # ---- 3) 查盘
            files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
            meta_path = os.path.join(folder, "preview.json")
            meta = {}
            if os.path.isfile(meta_path):
                meta = json.load(open(meta_path, encoding="utf-8"))
            report["steps"]["files"] = {
                "folder": folder, "files": files,
                "paper_pages": meta.get("paper_pages"), "card_pages": meta.get("card_pages"),
                "degraded": meta.get("degraded"), "has_print_pdf_before": "print.pdf" in files,
            }

            # ---- 4) 上传判卷（先合成照片）
            card_png = None
            for name in (meta.get("card_png") or []):
                if os.path.isfile(name):
                    card_png = name
                    break
            if card_png is None:
                cand = [os.path.join(folder, n) for n in files if n.startswith("card_")]
                card_png = cand[0] if cand else None
            assert card_png, "找不到答题卡 PNG，没法合成照片"
            photo = synth_photo(card_png, man, os.path.join(RUN, "photo.jpg"))
            t0 = time.time()
            with open(photo, "rb") as f:
                r = client.post(base + "/submissions",
                                data={"pid": pid},
                                files={"file": ("photo.jpg", f.read(), "image/jpeg")})
            grade_s = round(time.time() - t0, 2)
            loc = r.headers.get("location") or ""
            sid = loc.rstrip("/").rsplit("/", 1)[-1] if loc else None
            subs = db_rows(db_copy, "SELECT sid, pid, warp_ok, qr_ok, score, full, detail_json"
                                    " FROM submissions WHERE pid=?", (pid,))
            detail = json.loads(subs[0]["detail_json"]) if subs else {}
            report["steps"]["submission"] = {
                "status": r.status_code, "sid": sid, "grade_s": grade_s,
                "rows": len(subs),
                "warp_ok": subs[0]["warp_ok"] if subs else None,
                "qr_ok": subs[0]["qr_ok"] if subs else None,
                "score": subs[0]["score"] if subs else None,
                "full": subs[0]["full"] if subs else None,
                "items_in_detail": len(detail.get("items") or []),
                "manifest_pid_in_detail": detail.get("pid"),
            }

            # ---- 5) 合并打印 PDF
            r = client.get(base + "/papers/%s/print.pdf" % pid)
            print_pdf = os.path.join(EVID, "print_%s.pdf" % pid)
            if r.status_code == 200:
                with open(print_pdf, "wb") as f:
                    f.write(r.content)
            n_pages, texts = (pdf_pages(print_pdf) if os.path.isfile(print_pdf) else (0, []))
            paper_pages = int(meta.get("paper_pages") or 0)
            card_pages = int(meta.get("card_pages") or 0)
            page_kind = []
            for i, t in enumerate(texts):
                if t:
                    page_kind.append("paper")
                else:
                    page_kind.append("card")
            order_ok = page_kind == (["paper"] * paper_pages + ["card"] * card_pages)
            footers = []
            for i, t in enumerate(texts):
                m = re.search(r"第\s*(\d+)\s*页\s*/\s*共\s*(\d+)\s*页", t or "")
                if m:
                    footers.append({"page": i + 1, "n": int(m.group(1)),
                                    "total": int(m.group(2))})
            report["steps"]["print_pdf"] = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "bytes": len(r.content) if r.status_code == 200 else 0,
                "saved": print_pdf,
                "pages": n_pages,
                "paper_pages_meta": paper_pages, "card_pages_meta": card_pages,
                "pages_expected": paper_pages + card_pages,
                "page_kinds": page_kind,
                "order_ok": order_ok,
                "footers": footers,
                "paper_footer_totals": sorted({f["total"] for f in footers}),
            }

            # ---- 6) 分开下载
            for kind, path in (("paper", "/papers/%s/pdf" % pid), ("card", "/papers/%s/card.pdf" % pid)):
                rr = client.get(base + path)
                body = rr.content if rr.status_code == 200 else b""
                tmp = os.path.join(RUN, "%s_%s.pdf" % (pid, kind))
                if body:
                    with open(tmp, "wb") as f:
                        f.write(body)
                pg = pdf_pages(tmp)[0] if os.path.isfile(tmp) else 0
                report["steps"][kind + "_pdf"] = {
                    "status": rr.status_code,
                    "content_type": rr.headers.get("content-type"),
                    "bytes": len(body), "pages": pg,
                    "pages_meta": paper_pages if kind == "paper" else card_pages,
                }

            # ---- 7) 结果页/预览页也能打开（顺带证明 manifest 可用）
            for name, path in (("preview", "/papers/%s" % pid),
                               ("result", "/submissions/%s" % sid) if sid else ("result", None)):
                if not path:
                    continue
                rr = client.get(base + path)
                report["steps"][name + "_page"] = {"status": rr.status_code,
                                                   "bytes": len(rr.content)}
    finally:
        return finish(report, proc, log, RUN)


def finish(report, proc, log, run_dir):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:                                  # noqa: BLE001
        try:
            proc.kill()
        except Exception:                              # noqa: BLE001
            pass
    try:
        log.close()
    except Exception:                                  # noqa: BLE001
        pass
    report["server_exit"] = proc.returncode
    ev = os.path.join(EVID, "print_chain.json")
    with open(ev, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str)[:6000])
    print("\n证据：", ev)
    return 0 if not report.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
