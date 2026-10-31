# -*- coding: utf-8 -*-
"""t31 · 判卷质量基线：合成答题卡照片压测 → report.json / report.md

回答一个问题：**用户拿这套软件判 100 张照片，有多少张需要人工确认？**

做法（全离线、manual 后端，不碰云端 OCR）：
  1. `picker.build` 组 2 份卷（A: 10 选择/8 填空/6 解答，B: 5/3/2），`card.build_card` 出答题卡；
  2. 每张卡合成 N 次「拍照」：随机透视 2%~4%、随机光照梯度、随机 JPEG 60~80、随机轻微模糊；
     填涂模拟：选择题随机单涂（80% 涂对）/ 10% 不涂 / 10% 双涂；填空/解答框贴「模拟手写」小块
     （不同字体 + 随机 ±5° 旋转）；
  3. 跑 `grade_image`，与真值逐项比对，统计：标定率、QR 率、选择判分准确率（含「该 unsure 时是否
     真的 unsure」）、unsure 率、pending 率、耗时 p50/p95/max、失败样本清单。

用法：
    python app/quality_baseline.py --photos 50      # 正式报表（默认 50/张）
    python app/quality_baseline.py --quick          # 2×5 冒烟
    python app/quality_baseline.py --repro A 17     # 重建第 17 号样本照片（失败样本复现用）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import card          # noqa: E402
import db            # noqa: E402
import grade         # noqa: E402
import picker        # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(APP_DIR, "out", "quality_baseline")
DB_PATH = os.path.join(OUT_DIR, "_db", "quality_baseline.db")
SRC_DB = os.path.join(APP_DIR, "data", "app.db")
BASE_SEED = 20261031

PAPERS = [
    ("A", "质量基线卷 A", {"选择": 10, "填空": 8, "解答": 6}, 42),
    ("B", "质量基线卷 B", {"选择": 5, "填空": 3, "解答": 2}, 7),
]

HAND_TEXTS = ["4", "1/2", "x=4", "9", "3.5", "12", "-2", "√3", "x=7"]
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


# ---------------------------------------------------------------- 组卷 & 卡片
def _open_work_db():
    """拷一份产品库做工作库（不污染 app.db），再往里插 papers → grade 才找得到卷子。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suf)
        except OSError:
            pass
    if os.path.exists(SRC_DB):
        import sqlite3
        src = sqlite3.connect(SRC_DB)
        dst = sqlite3.connect(DB_PATH)
        try:
            src.backup(dst)          # 走 SQLite backup，正确处理 WAL
        finally:
            dst.close()
            src.close()
    return db.connect(DB_PATH)


def build_papers(conn, out_root: str):
    probes = []
    for tag, title, mix, seed in PAPERS:
        params = {
            "title": title,
            "mix": dict(mix),
            "scope": {"chapters": [], "kps": [], "kp_mode": "any"},
            "requirements": {"need_answer": True, "exclude_empty": True,
                             "exclude_no_family": False},
            "seed": seed,
            "space": dict(picker.DEFAULT_SPACE),   # 卷面不留书写区（作答在答题卡）
        }
        manifest = picker.build(params, conn, pid="QB%s" % tag)
        res = card.build_card(manifest, os.path.join(out_root, "cards", tag))
        conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest, n_items)"
            " VALUES (?,?,?,?,?,?)",
            (manifest["pid"], manifest.get("created_at") or "", manifest.get("title") or "",
             json.dumps(params, ensure_ascii=False),
             json.dumps(manifest, ensure_ascii=False), len(manifest.get("items") or [])))
        conn.commit()
        pages = (manifest.get("card") or {}).get("pages") or []
        all_items = [it for pg in pages for it in (pg.get("items") or [])]
        n_choice = sum(1 for it in all_items if it["kind"] == card.KIND_OMR)
        n_fill = sum(1 for it in all_items if it["kind"] == card.KIND_FILL)
        n_sol = sum(1 for it in all_items if it["kind"] == card.KIND_SOLUTION)
        probes.append({
            "tag": tag, "pid": manifest["pid"], "title": manifest.get("title"),
            "seed": seed, "mix": mix, "manifest": manifest,
            "card_png": res["png"][0], "page_pngs": res["png"], "pages": pages,
            "n_pages": len(pages),
            "n_choice_items": n_choice, "n_fill_items": n_fill, "n_solution_items": n_sol,
            "n_choice_units": sum(1 for it in all_items if it["kind"] == card.KIND_OMR),
            "n_non_choice_units": sum(len(it.get("boxes") or it.get("answer_boxes") or [])
                                      for it in all_items
                                      if it["kind"] != card.KIND_OMR),
        })
    return probes


# ---------------------------------------------------------------- 模拟作答
def _font(size: int, rng: random.Random):
    for p in rng.sample(FONT_CANDIDATES, len(FONT_CANDIDATES)):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size, index=0)
            except Exception:
                continue
    return ImageFont.load_default()


def _handwriting(text: str, w: int, h: int, rng: random.Random) -> Image.Image:
    """模拟手写：短答案 + 随机字体 + ±5° 旋转。"""
    im = Image.new("L", (max(8, w), max(8, h)), 255)
    d = ImageDraw.Draw(im)
    size = max(24, min(64, int(h * 0.5)))
    d.text((max(6, int(w * 0.08)), max(4, int(h * 0.12))), text, font=_font(size, rng), fill=0)
    return im.rotate(rng.uniform(-5, 5), resample=Image.BILINEAR, fillcolor=255)


def fill_page(probe, page_idx: int, sample_no: int, rng: random.Random, out_png: str):
    """把「学生作答」画到答题卡**指定页**上 → (filled_png, truth)。"""
    page = probe["pages"][page_idx]
    page_png = probe["page_pngs"][page_idx]
    items_by_idx = {it["idx"]: it for it in (probe["manifest"].get("items") or [])}
    img = Image.open(page_png).convert("L")
    d = ImageDraw.Draw(img)

    truth = {"omr": {}, "non_choice": [], "sample": sample_no,
             "card_page": page.get("page")}
    for it in page.get("items") or []:
        idx = it["idx"]
        if it["kind"] == card.KIND_OMR:
            cells = it.get("cells") or []
            opts = [str(c["opt"]) for c in cells]
            expected = str(((items_by_idx.get(idx) or {}).get("answer_key") or [{}])[0]
                           .get("value") or "").strip()
            roll = rng.random()
            entry = {"idx": idx, "expected": expected, "scenario": "", "picked": "",
                     "expect_verdict": "", "opts": opts}
            if roll < 0.10:                      # 不涂
                entry["scenario"] = "blank"
                entry["expect_verdict"] = "unsure"
            elif roll < 0.20:                    # 双涂
                entry["scenario"] = "double"
                picked = expected if expected in opts else rng.choice(opts)
                other = rng.choice([o for o in opts if o != picked] or opts)
                for c in cells:
                    if str(c["opt"]) in (picked, other):
                        d.rectangle([c["x"] + 8, c["y"] + 8,
                                     c["x"] + c["w"] - 8, c["y"] + c["h"] - 8], fill=0)
                entry["expect_verdict"] = "unsure"
            else:                                # 单涂（80% 涂对）
                entry["scenario"] = "single"
                if expected in opts and rng.random() < 0.8:
                    picked = expected
                else:
                    picked = rng.choice([o for o in opts if o != expected] or opts)
                entry["picked"] = picked
                entry["expect_verdict"] = "correct" if picked == expected else "wrong"
                for c in cells:
                    if str(c["opt"]) == picked:
                        d.rectangle([c["x"] + 8, c["y"] + 8,
                                     c["x"] + c["w"] - 8, c["y"] + c["h"] - 8], fill=0)
            truth["omr"][idx] = entry
        else:
            boxes = it.get("boxes") or it.get("answer_boxes") or []
            for b in boxes:
                text = rng.choice(HAND_TEXTS)
                patch = _handwriting(text, b["w"], b["h"], rng)
                img.paste(patch, (int(b["x"]), int(b["y"])))
                truth["non_choice"].append({"idx": idx, "blank": b["blank"], "text": text})
    img.save(out_png)
    return out_png, truth


# ---------------------------------------------------------------- 模拟拍照
def make_photo(filled_png: str, rng: random.Random, out_jpg: str, harsh: bool = False,
               jitter: float = None, jpeg_q: int = None, blur_sigma: float = None):
    """合成拍照。harsh=True 时加重；也可用 jitter/jpeg_q/blur_sigma 显式覆盖（降质阶梯用）。"""
    img = cv2.imread(filled_png, cv2.IMREAD_GRAYSCALE)
    h, w = img.shape[:2]
    jitter = jitter if jitter is not None else (
        rng.uniform(0.05, 0.08) if harsh else rng.uniform(0.02, 0.04))
    scale = rng.uniform(0.55, 0.85) if harsh else rng.uniform(0.78, 1.0)
    W, H = int(w * scale), int(h * scale)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([
        [W * rng.uniform(jitter * 0.3, jitter), H * rng.uniform(jitter * 0.3, jitter)],
        [W * (1 - rng.uniform(jitter * 0.3, jitter)), H * rng.uniform(jitter * 0.4, jitter * 1.2)],
        [W * (1 - rng.uniform(jitter * 0.3, jitter)), H * (1 - rng.uniform(jitter * 0.3, jitter))],
        [W * rng.uniform(jitter * 0.3, jitter), H * (1 - rng.uniform(jitter * 0.3, jitter))],
    ])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, M, (W, H), borderValue=rng.randint(140, 210))

    yy, xx = np.mgrid[0:H, 0:W]
    gx, gy = rng.uniform(0.25, 0.5), rng.uniform(0.2, 0.45)
    grad = rng.uniform(0.7, 0.85) + gx * (xx / max(W - 1, 1)) * 0.5 + gy * (yy / max(H - 1, 1)) * 0.5
    np_rng = np.random.default_rng(rng.getrandbits(64))   # 与 Python rng 同源，保证可复现
    out = warped.astype(np.float32) * grad + np_rng.normal(0, rng.uniform(2, 6), (H, W))
    out = np.clip(out, 0, 255).astype(np.uint8)
    k = rng.choice([3, 5]) if harsh else rng.choice([0, 0, 3])
    if k:
        sigma = blur_sigma if blur_sigma is not None else rng.uniform(0.3, 0.9)
        out = cv2.GaussianBlur(out, (k, k), sigma)
    q = jpeg_q if jpeg_q is not None else (rng.randint(35, 55) if harsh else rng.randint(60, 80))
    cv2.imwrite(out_jpg, out, [cv2.IMWRITE_JPEG_QUALITY, q])
    return out_jpg, {"jitter": round(jitter, 4), "scale": round(scale, 3),
                     "jpeg_q": q, "size": [W, H], "blur": k}


# ---------------------------------------------------------------- 判卷 & 比对
def grade_sample(conn, pid: str, photo: str, runs_dir: str):
    t0 = time.perf_counter()
    res = grade.grade_image(conn, pid, photo, ocr=None, out_dir=runs_dir)
    return res, time.perf_counter() - t0


def compare(res, truth):
    """逐项比对真值 → 统计增量 + 不匹配明细。"""
    stat = {"choice_total": 0, "choice_ok": 0, "letter_total": 0, "letter_ok": 0,
            "expect_unsure": 0, "unsure_matched": 0, "non_choice_total": 0,
            "pending": 0, "unsure_units": 0, "crop_ok": 0, "mismatch": []}
    units = res.get("items") or []
    for u in units:
        if u.get("kind") == card.KIND_OMR:
            t = truth["omr"].get(u.get("idx")) or {}
            if not t:
                continue
            stat["choice_total"] += 1
            actual = u.get("verdict")
            sc = t.get("scenario") or "single"
            stat.setdefault("by_scenario", {}).setdefault(sc, {"total": 0, "ok": 0})
            stat["by_scenario"][sc]["total"] += 1
            if actual == t["expect_verdict"]:
                stat["choice_ok"] += 1
                stat["by_scenario"][sc]["ok"] += 1
            else:
                stat["mismatch"].append({"idx": u.get("idx"), "scenario": t["scenario"],
                                         "expected_verdict": t["expect_verdict"],
                                         "actual": actual, "picked": u.get("picked"),
                                         "truth_picked": t["picked"],
                                         "ratios": u.get("ratios")})
            if t["expect_verdict"] == "unsure":
                stat["expect_unsure"] += 1
                if actual == "unsure":
                    stat["unsure_matched"] += 1
            if t["expect_verdict"] in ("correct", "wrong") and actual in ("correct", "wrong"):
                stat["letter_total"] += 1
                if str(u.get("picked")) == str(t["picked"]):
                    stat["letter_ok"] += 1
            if actual == "unsure":
                stat["unsure_units"] += 1
        else:
            stat["non_choice_total"] += 1
            if u.get("verdict") == "pending":
                stat["pending"] += 1
            crop = u.get("crop_path")
            if crop and os.path.exists(crop) and os.path.getsize(crop) > 200:
                stat["crop_ok"] += 1
    return stat


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[k], 3)


def _rate(num, den):
    return round(num / den, 4) if den else 0.0


# ---------------------------------------------------------------- 主流程
def harsh_check(probes, conn, per_paper: int = 10, seed: int = BASE_SEED + 991,
                runs_dir: str = None, photos_dir: str = None):
    """压力档：更狠的透视/JPEG/模糊，回答「什么条件下开始掉」。"""
    runs_dir = runs_dir or os.path.join(OUT_DIR, "runs_harsh")
    photos_dir = photos_dir or os.path.join(OUT_DIR, "photos_harsh")
    os.makedirs(runs_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    agg = {"photos": 0, "warp_ok": 0, "qr_ok": 0, "choice_total": 0, "choice_ok": 0,
           "expect_unsure": 0, "unsure_matched": 0, "by_scenario": {}}
    for p_i, probe in enumerate(probes):
        for n in range(1, per_paper + 1):
            rng = random.Random(seed * 1_000_003 + p_i * 10_000 + n)
            page_idx = (n - 1) % max(1, probe["n_pages"])
            filled = os.path.join(photos_dir, "%s_filled_%03d.png" % (probe["tag"], n))
            photo = os.path.join(photos_dir, "%s_photo_%03d.jpg" % (probe["tag"], n))
            _, truth = fill_page(probe, page_idx, n, rng, filled)
            make_photo(filled, rng, photo, harsh=True)
            os.remove(filled)
            res, _el = grade_sample(conn, probe["pid"], photo, runs_dir)
            agg["photos"] += 1
            if res.get("warp_ok"):
                agg["warp_ok"] += 1
            if res.get("qr_ok"):
                agg["qr_ok"] += 1
            if res.get("warp_ok"):
                cmp = compare(res, truth)
                for k in ("choice_total", "choice_ok", "expect_unsure", "unsure_matched"):
                    agg[k] += cmp[k]
                for sc, v in (cmp.get("by_scenario") or {}).items():
                    dst = agg["by_scenario"].setdefault(sc, {"total": 0, "ok": 0})
                    dst["total"] += v["total"]
                    dst["ok"] += v["ok"]
    return {
        "photos": agg["photos"],
        "warp_rate": _rate(agg["warp_ok"], agg["photos"]),
        "qr_rate": _rate(agg["qr_ok"], agg["photos"]),
        "choice_decisions": agg["choice_total"],
        "choice_accuracy": _rate(agg["choice_ok"], agg["choice_total"]),
        "unsure_match_rate": _rate(agg["unsure_matched"], agg["expect_unsure"]),
        "by_scenario": {k: {"total": v["total"], "ok": v["ok"],
                            "accuracy": _rate(v["ok"], v["total"])}
                        for k, v in sorted(agg["by_scenario"].items())},
        "note": "压力档：透视 5%~8%、画面占比 55%~85%、JPEG 35~55、3~5px 模糊",
    }


def adversarial_check(probes, conn, n_per: int = 6, seed: int = BASE_SEED + 777,
                      runs_dir: str = None, photos_dir: str = None):
    """对抗条件：角标反光 / 强模糊 / 纸张占比过小——这些是合成光照造不出来的真机问题。"""
    runs_dir = runs_dir or os.path.join(OUT_DIR, "runs_adv")
    photos_dir = photos_dir or os.path.join(OUT_DIR, "photos_adv")
    os.makedirs(runs_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    cases = [("glare", "左上角标 50% 被打白（强反光）"),
             ("blur", "强模糊 sigma=3.0（手抖/失焦）"),
             ("small", "纸张只占画面约 40%（离太远）")]
    probe = probes[0]
    rows = []
    for ci, (name, desc) in enumerate(cases):
        ok = qr = photos = 0
        for n in range(1, n_per + 1):
            rng = random.Random(seed + ci * 100 + n)
            page_idx = (n - 1) % max(1, probe["n_pages"])
            filled = os.path.join(photos_dir, "A_filled_%03d.png" % n)
            photo = os.path.join(photos_dir, "A_%s_%03d.jpg" % (name, n))
            _, truth = fill_page(probe, page_idx, n, rng, filled)
            if name == "blur":
                make_photo(filled, rng, photo, blur_sigma=3.0)
            elif name == "small":
                make_photo(filled, rng, photo, jitter=0.03)
                im = cv2.imread(photo, cv2.IMREAD_GRAYSCALE)
                small = cv2.resize(im, (int(im.shape[1] * 0.42), int(im.shape[0] * 0.42)),
                                   interpolation=cv2.INTER_AREA)
                canvas = np.full((im.shape[0], im.shape[1]), 205, np.uint8)
                y0 = (im.shape[0] - small.shape[0]) // 2
                x0 = (im.shape[1] - small.shape[1]) // 2
                canvas[y0:y0 + small.shape[0], x0:x0 + small.shape[1]] = small
                cv2.imwrite(photo, canvas, [cv2.IMWRITE_JPEG_QUALITY, 75])
            else:
                make_photo(filled, rng, photo, jitter=0.03)
                im = cv2.imread(photo, cv2.IMREAD_GRAYSCALE)
                h, w = im.shape
                im[:int(h * 0.10), :int(w * 0.30)] = 255     # 左上角标一半打白
                cv2.imwrite(photo, im, [cv2.IMWRITE_JPEG_QUALITY, 75])
            os.remove(filled)
            res, _el = grade_sample(conn, probe["pid"], photo, runs_dir)
            photos += 1
            ok += 1 if res.get("warp_ok") else 0
            qr += 1 if res.get("qr_ok") else 0
        rows.append({"case": name, "desc": desc, "photos": photos,
                     "warp_rate": _rate(ok, photos), "qr_rate": _rate(qr, photos)})
    return rows


def ladder_check(probes, conn, per_level: int = 6, seed: int = BASE_SEED + 4242,
                 runs_dir: str = None, photos_dir: str = None):
    """降质阶梯：JPEG 质量 × 透视幅度，找「什么时候开始掉」。只跑第一份卷。"""
    runs_dir = runs_dir or os.path.join(OUT_DIR, "runs_ladder")
    photos_dir = photos_dir or os.path.join(OUT_DIR, "photos_ladder")
    os.makedirs(runs_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    probe = probes[0]
    rows = []
    for jpeg_q in (60, 45, 30, 20):
        for jit in (0.03, 0.06, 0.10):
            photos = warp_ok = qr_ok = ch_total = ch_ok = 0
            for n in range(1, per_level + 1):
                rng = random.Random(seed + jpeg_q * 1000 + int(jit * 100) * 7 + n)
                page_idx = (n - 1) % max(1, probe["n_pages"])
                filled = os.path.join(photos_dir, "L_filled_%03d.png" % n)
                photo = os.path.join(photos_dir, "L_q%d_j%d_%03d.jpg" % (jpeg_q, int(jit * 100), n))
                _, truth = fill_page(probe, page_idx, n, rng, filled)
                make_photo(filled, rng, photo, jitter=jit, jpeg_q=jpeg_q, blur_sigma=0.6)
                os.remove(filled)
                res, _el = grade_sample(conn, probe["pid"], photo, runs_dir)
                photos += 1
                if res.get("warp_ok"):
                    warp_ok += 1
                    qr_ok += 1 if res.get("qr_ok") else 0
                    cmp_res = compare(res, truth)
                    ch_total += cmp_res["choice_total"]
                    ch_ok += cmp_res["choice_ok"]
            rows.append({"jpeg_q": jpeg_q, "jitter": jit, "photos": photos,
                         "warp_rate": _rate(warp_ok, photos),
                         "qr_rate": _rate(qr_ok, photos),
                         "choice_decisions": ch_total,
                         "choice_accuracy": _rate(ch_ok, ch_total)})
    return rows


def run(photos_per_paper: int, seed: int = BASE_SEED, verbose: bool = True):
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    photos_dir = os.path.join(OUT_DIR, "photos")
    runs_dir = os.path.join(OUT_DIR, "runs")
    fail_dir = os.path.join(OUT_DIR, "failures")
    for d in (photos_dir, runs_dir, fail_dir):
        os.makedirs(d, exist_ok=True)

    conn = _open_work_db()
    probes = build_papers(conn, OUT_DIR)

    samples = []
    failures = []
    timings = []
    agg = {"choice_total": 0, "choice_ok": 0, "letter_total": 0, "letter_ok": 0,
           "expect_unsure": 0, "unsure_matched": 0, "non_choice_total": 0,
           "pending": 0, "unsure_units": 0, "crop_ok": 0, "mismatch": [],
           "by_scenario": {}}
    warp_ok = qr_ok = 0
    per_paper = {}

    t_start = time.perf_counter()
    for p_i, probe in enumerate(probes):
        stats = {"photos": 0, "warp_ok": 0, "qr_ok": 0, "choice_total": 0, "choice_ok": 0,
                 "letter_total": 0, "letter_ok": 0, "expect_unsure": 0, "unsure_matched": 0,
                 "non_choice_total": 0, "pending": 0, "unsure_units": 0, "crop_ok": 0,
                 "timings": [], "mismatch": []}
        for n in range(1, photos_per_paper + 1):
            sample_seed = seed * 1_000_003 + p_i * 10_000 + n
            rng = random.Random(sample_seed)
            page_idx = (n - 1) % max(1, probe["n_pages"])
            filled = os.path.join(photos_dir, "%s_filled_%03d.png" % (probe["tag"], n))
            photo = os.path.join(photos_dir, "%s_photo_%03d.jpg" % (probe["tag"], n))
            _, truth = fill_page(probe, page_idx, n, rng, filled)
            _, shot = make_photo(filled, rng, photo)
            os.remove(filled)                      # 只留「照片」，省空间

            res, elapsed = grade_sample(conn, probe["pid"], photo, runs_dir)
            timings.append(elapsed)
            stats["timings"].append(elapsed)
            stats["photos"] += 1
            err = res.get("error")
            ok = bool(res.get("warp_ok"))
            qok = bool(res.get("qr_ok"))
            if ok:
                warp_ok += 1
                stats["warp_ok"] += 1
            if qok:
                qr_ok += 1
                stats["qr_ok"] += 1

            row = {"paper": probe["tag"], "pid": probe["pid"], "sample": n,
                   "card_page": truth.get("card_page"),
                   "photo": photo, "warp_ok": ok, "qr_ok": qok, "error": err,
                   "detail": (res.get("detail") or "")[:200], "elapsed_s": round(elapsed, 4),
                   "shot": shot, "seed": sample_seed}
            if ok:
                cmp = compare(res, truth)
                for k in ("choice_total", "choice_ok", "letter_total", "letter_ok",
                          "expect_unsure", "unsure_matched", "non_choice_total",
                          "pending", "unsure_units", "crop_ok"):
                    stats[k] += cmp[k]
                    agg[k] += cmp[k]
                for sc, v in (cmp.get("by_scenario") or {}).items():
                    dst = agg["by_scenario"].setdefault(sc, {"total": 0, "ok": 0})
                    dst["total"] += v["total"]
                    dst["ok"] += v["ok"]
                if cmp["mismatch"]:
                    stats["mismatch"].extend([dict(m, sample=n, paper=probe["tag"])
                                              for m in cmp["mismatch"]])
                    agg["mismatch"].extend([dict(m, sample=n, paper=probe["tag"])
                                            for m in cmp["mismatch"]])
                row["cur"] = {"score": res.get("score"), "full": res.get("full"),
                              "pending_units": res.get("pending")}
            else:
                failures.append({"paper": probe["tag"], "sample": n, "stage": "warp",
                                 "error": err, "detail": row["detail"], "photo": photo})
                shutil.copy2(photo, os.path.join(fail_dir, "%s_%03d.jpg" % (probe["tag"], n)))
            samples.append(row)
            if verbose:
                print("[%s %3d/%d] warp=%s qr=%s %5.2fs %s" % (
                    probe["tag"], n, photos_per_paper, "Y" if ok else "N",
                    "Y" if qok else "-", elapsed, err or ""), flush=True)

        per_paper[probe["tag"]] = {
            "pid": probe["pid"], "title": probe["title"], "mix": probe["mix"],
            "n_pages": probe["n_pages"],
            "n_choice_items": probe["n_choice_items"], "n_fill_items": probe["n_fill_items"],
            "n_solution_items": probe["n_solution_items"],
            "n_choice_units": probe["n_choice_units"],
            "n_non_choice_units": probe["n_non_choice_units"],
            "photos": stats["photos"],
            "warp_ok": stats["warp_ok"], "warp_rate": _rate(stats["warp_ok"], stats["photos"]),
            "qr_ok": stats["qr_ok"], "qr_rate": _rate(stats["qr_ok"], stats["photos"]),
            "choice_decisions": stats["choice_total"],
            "choice_accurate": stats["choice_ok"],
            "choice_accuracy": _rate(stats["choice_ok"], stats["choice_total"]),
            "letter_fidelity": _rate(stats["letter_ok"], stats["letter_total"]),
            "expect_unsure": stats["expect_unsure"],
            "unsure_matched": stats["unsure_matched"],
            "unsure_match_rate": _rate(stats["unsure_matched"], stats["expect_unsure"]),
            "unsure_units": stats["unsure_units"],
            "unsure_rate": _rate(stats["unsure_units"], stats["choice_total"]),
            "non_choice_units": stats["non_choice_total"],
            "pending": stats["pending"],
            "pending_rate": _rate(stats["pending"], stats["non_choice_total"]),
            "crop_ok": stats["crop_ok"],
            "crop_ok_rate": _rate(stats["crop_ok"], stats["non_choice_total"]),
            "timing": {"p50": _pct(stats["timings"], 50), "p95": _pct(stats["timings"], 95),
                       "max": round(max(stats["timings"]), 3),
                       "mean": round(statistics.fmean(stats["timings"]), 3)},
            "mismatch": stats["mismatch"][:20],
        }

    total_photos = len(samples)
    total_units = agg["choice_total"] + agg["non_choice_total"]
    overall = {
        "photos": total_photos,
        "warp_ok": warp_ok, "warp_rate": _rate(warp_ok, total_photos),
        "qr_ok": qr_ok, "qr_rate": _rate(qr_ok, total_photos),
        "choice_decisions": agg["choice_total"],
        "choice_accurate": agg["choice_ok"],
        "choice_accuracy": _rate(agg["choice_ok"], agg["choice_total"]),
        "letter_fidelity": _rate(agg["letter_ok"], agg["letter_total"]),
        "expect_unsure": agg["expect_unsure"],
        "unsure_matched": agg["unsure_matched"],
        "unsure_match_rate": _rate(agg["unsure_matched"], agg["expect_unsure"]),
        "unsure_units": agg["unsure_units"],
        "unsure_rate": _rate(agg["unsure_units"], agg["choice_total"]),
        "non_choice_units": agg["non_choice_total"],
        "pending": agg["pending"],
        "pending_rate": _rate(agg["pending"], agg["non_choice_total"]),
        "crop_ok": agg["crop_ok"],
        "crop_ok_rate": _rate(agg["crop_ok"], agg["non_choice_total"]),
        "units_total": total_units,
        "manual_confirm_units": agg["unsure_units"] + agg["pending"],
        "manual_confirm_rate": _rate(agg["unsure_units"] + agg["pending"], total_units),
        "timing": {"p50": _pct(timings, 50), "p95": _pct(timings, 95),
                   "max": round(max(timings), 3) if timings else 0.0,
                   "mean": round(statistics.fmean(timings), 3) if timings else 0.0},
        "by_scenario": {sc: {"total": v["total"], "ok": v["ok"],
                             "accuracy": _rate(v["ok"], v["total"])}
                        for sc, v in sorted(agg["by_scenario"].items())},
        "wall_clock_s": round(time.perf_counter() - t_start, 1),
    }

    harsh = None
    ladder = None
    adversarial = None
    if photos_per_paper >= 20:      # 正式跑才加压；冒烟省时间
        harsh = harsh_check(probes, conn, per_paper=10)
        ladder = ladder_check(probes, conn)
        adversarial = adversarial_check(probes, conn)
        if verbose:
            print("[harsh] 标定 %.1f%% 选择准确率 %.2f%%" % (
                harsh["warp_rate"] * 100, harsh["choice_accuracy"] * 100), flush=True)
            print("[ladder] " + " | ".join("q%d/j%.0f%%: 标定 %.0f%%" % (
                r["jpeg_q"], r["jitter"] * 100, r["warp_rate"] * 100) for r in ladder), flush=True)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime()),
        "config": {
            "photos_per_paper": photos_per_paper, "papers": [p[0] for p in PAPERS],
            "base_seed": seed, "backend": "manual（不跑 OCR，只看标定/QR/OMR/裁切）",
            "photo_model": "透视 2%~4% + 随机光照梯度 + JPEG 60~80 + 随机轻微模糊",
            "fill_model": "单涂 80%（其中 20% 故意选错）/ 不涂 10% / 双涂 10%；"
                          "填空解答贴模拟手写（多字体 + ±5° 旋转）",
        },
        "papers": [{"tag": p["tag"], "pid": p["pid"], "title": p["title"], "mix": p["mix"],
                    "n_choice_items": p["n_choice_items"],
                    "n_fill_items": p["n_fill_items"],
                    "n_solution_items": p["n_solution_items"],
                    "card_png": os.path.relpath(p["card_png"], APP_DIR)}
                   for p in probes],
        "overall": overall,
        "per_paper": per_paper,
        "harsh": harsh,
        "ladder": ladder,
        "adversarial": adversarial,
        "failures": failures,
        "mismatch_samples": agg["mismatch"][:30],
        "notes": [
            "manual 后端下填空/解答在同步阶段恒为 pending（设计如此），pending_rate≈1；"
            "本条只验证「裁切可用率 crop_ok_rate」，文本识别要靠 fill_pending 二次跑。",
            "「需要人工确认」= 选择题 unsure + 填空/解答 pending，占比见 manual_confirm_rate。",
            "合成照片不含真实手写笔画细节与纸面反光，真实场景标定率可能更低；"
            "角标反光/缺角仍会 warp_failed（README 已写怎么拍）。",
            "多页答题卡按页轮换拍照（每页都有样本），页数见分卷明细 n_pages。",
            "可复现：固定 seed 下两次运行聚合指标完全一致；照片留在 photos/，"
            "失败样本复制到 failures/，单张可用 --repro 重建。",
            "注意：本报表只覆盖「标定/QR/OMR/裁切」，不含云端 OCR 文本识别准确率"
            "（那条链路由 ocr.py 单独测试 + fill_pending 处理）。",
        ],
    }

    with open(os.path.join(OUT_DIR, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, "samples.json"), "w", encoding="utf-8") as f:
        json.dump(samples, f, ensure_ascii=False)
    _write_markdown(report)
    conn.close()
    return report


def _write_markdown(rep: dict) -> str:
    o = rep["overall"]
    per = rep["per_paper"]
    lines = []
    lines.append("# 判卷质量基线（t31）\n")
    lines.append("生成时间：%s　·　样本：**%d 张合成照片**（%s）\n" % (
        rep["generated_at"], o["photos"],
        " / ".join("%s 卷 %d 张" % (k, v["photos"]) for k, v in per.items())))
    lines.append("> 判卷只判「选择题序号」和「填空/解答的最终答案」；本报表用 manual 后端，"
                 "不调用云端 OCR，因此只衡量**标定 / QR / OMR / 裁切**这条链路。\n")

    lines.append("\n## 一句话结论\n")
    lines.append("**%d 张合成照片：标定 %.1f%%、QR %.1f%%、选择题判分准确率 %.2f%%、"
                 "unsure %.1f%%、单张 p50 %.2fs / p95 %.2fs / max %.2fs。**\n" % (
                     o["photos"], o["warp_rate"] * 100, o["qr_rate"] * 100,
                     o["choice_accuracy"] * 100, o["unsure_rate"] * 100,
                     o["timing"]["p50"], o["timing"]["p95"], o["timing"]["max"]))
    lines.append("判分单元层面「需人工确认」占 %.1f%%，**但这个数字被口径放大**："
                 "manual 后端下所有填空/解答必然 pending（%d/%d），"
                 "选择题层面只有 %.1f%% 进人工（且其中大部分是我们故意造的"
                 "「不涂/双涂」）。\n" % (
                     o["manual_confirm_rate"] * 100, o["pending"], o["non_choice_units"],
                     o["unsure_rate"] * 100))
    adv0 = next((r for r in (rep.get("adversarial") or []) if r.get("case") == "glare"), None)
    if adv0:
        lines.append("**真正的风险点不在这 100 张里**：合成照片造不出强反光——"
                     "「左上角标被打白」这组对抗条件下标定率直接掉到 %.0f%%。"
                     "所以现场拍照的第一要求是四角黑框清晰、别反光。\n"
                     % (adv0["warp_rate"] * 100))

    lines.append("\n## 总体指标\n")
    lines.append("| 指标 | 数值 | 说明 |\n|---|---|---|")
    lines.append("| 标定成功率 | **%.1f%%**（%d/%d） | 四角 ArUco 全检出并复原 |" % (
        o["warp_rate"] * 100, o["warp_ok"], o["photos"]))
    lines.append("| 卷号 QR 成功率 | **%.1f%%**（%d/%d） | QR 失效仍按传入 pid 判，只是 `qr_ok=false` |" % (
        o["qr_rate"] * 100, o["qr_ok"], o["photos"]))
    lines.append("| 选择题判分准确率 | **%.2f%%**（%d/%d） | 与真值逐题比，含「该 unsure 时真的 unsure」 |" % (
        o["choice_accuracy"] * 100, o["choice_accurate"], o["choice_decisions"]))
    lines.append("| 涂黑字母保真率 | %.2f%% | 判对/判错的题里，读出的字母与真值一致 |" % (
        o["letter_fidelity"] * 100))
    sc = o.get("by_scenario") or {}
    for name, label in (("single", "单涂（真判对错）"), ("blank", "不涂（应 unsure）"),
                        ("double", "双涂（应 unsure）")):
        v = sc.get(name)
        if v:
            lines.append("| └ %s | %.2f%%（%d/%d） | 该场景的判定与真值一致率 |" % (
                label, v["accuracy"] * 100, v["ok"], v["total"]))
    lines.append("| 应判 unsure 命中率 | %.1f%%（%d/%d） | 不涂/双涂这类本来就不该瞎判的题 |" % (
        o["unsure_match_rate"] * 100, o["unsure_matched"], o["expect_unsure"]))
    lines.append("| 选择题 unsure 率 | %.1f%%（%d/%d） | 进了人工确认队 |" % (
        o["unsure_rate"] * 100, o["unsure_units"], o["choice_decisions"]))
    lines.append("| 填空/解答 pending 率 | %.1f%%（%d/%d） | manual 后端恒 pending；看下一行裁切率 |" % (
        o["pending_rate"] * 100, o["pending"], o["non_choice_units"]))
    lines.append("| 作答格裁切可用率 | **%.1f%%**（%d/%d） | 裁切图存在且非空白 |" % (
        o["crop_ok_rate"] * 100, o["crop_ok"], o["non_choice_units"]))
    lines.append("| 单张耗时 p50 / p95 / max | %.2fs / %.2fs / %.2fs | 纯 CPU，含读图+标定+OMR |" % (
        o["timing"]["p50"], o["timing"]["p95"], o["timing"]["max"]))
    lines.append("| **需人工确认的判分单元** | **%.1f%%**（%d/%d） | 选择题 unsure + 填空/解答 pending |" % (
        o["manual_confirm_rate"] * 100, o["manual_confirm_units"], o["units_total"]))

    lines.append("\n## 分卷明细\n")
    lines.append("| 卷 | 题量（选/填/解） | 标定 | QR | 选择准确率 | unsure 命中 | p95 |\n|---|---|---|---|---|---|---|")
    for tag, v in per.items():
        lines.append("| %s | %d/%d/%d | %.1f%% | %.1f%% | %.2f%% | %.1f%% | %.2fs |" % (
            tag, v["n_choice_items"], v["n_fill_items"], v["n_solution_items"],
            v["warp_rate"] * 100, v["qr_rate"] * 100, v["choice_accuracy"] * 100,
            v["unsure_match_rate"] * 100, v["timing"]["p95"]))

    h = rep.get("harsh")
    if h:
        lines.append("\n## 压力档（更狠的拍照条件）\n")
        lines.append("> %s，每份卷 %d 张。\n" % (h["note"], 10))
        lines.append("| 指标 | 数值 |\n|---|---|")
        lines.append("| 标定成功率 | **%.1f%%** |" % (h["warp_rate"] * 100))
        lines.append("| QR 成功率 | %.1f%% |" % (h["qr_rate"] * 100))
        lines.append("| 选择题判分准确率 | **%.2f%%**（%d 次判定） |" % (
            h["choice_accuracy"] * 100, h["choice_decisions"]))
        lines.append("| 应判 unsure 命中率 | %.1f%% |" % (h["unsure_match_rate"] * 100))
        for name, label in (("single", "单涂"), ("blank", "不涂"), ("double", "双涂")):
            v = (h.get("by_scenario") or {}).get(name)
            if v:
                lines.append("| └ %s | %.2f%%（%d/%d） |" % (
                    label, v["accuracy"] * 100, v["ok"], v["total"]))

    lad = rep.get("ladder")
    if lad:
        lines.append("\n## 降质阶梯（第一份卷，每格 6 张）\n")
        lines.append("| JPEG 质量 \\ 透视幅度 | 3% | 6% | 10% |\n|---|---|---|---|")
        by_q = {}
        for r in lad:
            by_q.setdefault(r["jpeg_q"], {})[round(r["jitter"] * 100)] = r
        for q in sorted(by_q, reverse=True):
            cells = []
            for j in (3, 6, 10):
                r = by_q[q].get(j)
                cells.append("%.0f%% / %.0f%%" % (r["warp_rate"] * 100, r["choice_accuracy"] * 100)
                             if r else "-")
            lines.append("| q=%d | %s | %s | %s |" % (q, cells[0], cells[1], cells[2]))
        lines.append("\n> 每格显示「标定率 / 选择题准确率」。空/低值即「该条件下开始需要重拍」。\n")

    adv = rep.get("adversarial")
    if adv:
        lines.append("\n## 对抗条件（真机会遇到、合成光照造不出来的）\n")
        lines.append("| 条件 | 说明 | 标定成功率 | QR 成功率 |\n|---|---|---|---|")
        for r in adv:
            lines.append("| %s | %s | **%.0f%%**（%d/%d） | %.0f%% |" % (
                r["case"], r["desc"], r["warp_rate"] * 100,
                int(r["warp_rate"] * r["photos"]), r["photos"], r["qr_rate"] * 100))

    lines.append("\n## 失败样本（%d 张）\n" % len(rep["failures"]))
    if rep["failures"]:
        lines.append("| 卷 | 样本 | 阶段 | 错误码 | 明细 | 照片 |\n|---|---|---|---|---|---|")
        for f in rep["failures"][:30]:
            lines.append("| %s | #%d | %s | %s | %s | `%s` |" % (
                f["paper"], f["sample"], f["stage"], f["error"],
                (f["detail"] or "").replace("|", "/")[:60],
                os.path.relpath(f["photo"], APP_DIR)))
    else:
        lines.append("无：50×2 张全部标定成功。\n")

    if rep["mismatch_samples"]:
        lines.append("\n## 选择题判分不匹配样本（前 %d 条）\n" % len(rep["mismatch_samples"]))
        lines.append("| 卷/样本 | 题 | 场景 | 应有判定 | 实际判定 | 读出 | 真值 |\n|---|---|---|---|---|---|---|")
        for m in rep["mismatch_samples"]:
            lines.append("| %s#%s | %s | %s | %s | %s | %s | %s |" % (
                m.get("paper"), m.get("sample"), m.get("idx"), m.get("scenario"),
                m.get("expected_verdict"), m.get("actual"), m.get("picked"), m.get("truth_picked")))

    lines.append("\n## 说明与已知局限\n")
    for n in rep["notes"]:
        lines.append("- " + n)
    lines.append("- 复现：`python app/quality_baseline.py --photos %d`（固定 seed）；"
                 "单张复现：`python app/quality_baseline.py --repro A 17`。" %
                 rep["config"]["photos_per_paper"])

    text = "\n".join(lines) + "\n"
    with open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


def repro(tag: str, sample_no: int, seed: int = BASE_SEED):
    """重建某一张样本照片（失败样本复现用）。"""
    conn = _open_work_db()
    probes = build_papers(conn, OUT_DIR)
    probe = next((p for p in probes if p["tag"] == tag), None)
    if probe is None:
        print("没有这个卷：%s" % tag)
        return 2
    p_i = [p[0] for p in PAPERS].index(tag)
    rng = random.Random(seed * 1_000_003 + p_i * 10_000 + sample_no)
    os.makedirs(os.path.join(OUT_DIR, "repro"), exist_ok=True)
    filled = os.path.join(OUT_DIR, "repro", "%s_filled_%03d.png" % (tag, sample_no))
    photo = os.path.join(OUT_DIR, "repro", "%s_photo_%03d.jpg" % (tag, sample_no))
    _, truth = fill_page(probe, (sample_no - 1) % max(1, probe["n_pages"]),
                         sample_no, rng, filled)
    _, shot = make_photo(filled, rng, photo)
    res = grade.grade_image(conn, probe["pid"], photo, out_dir=os.path.join(OUT_DIR, "repro"))
    print(json.dumps({"photo": photo, "shot": shot, "warp_ok": res.get("warp_ok"),
                      "qr_ok": res.get("qr_ok"), "error": res.get("error"),
                      "truth_omr": {k: v["expect_verdict"] for k, v in truth["omr"].items()}},
                     ensure_ascii=False, indent=1))
    conn.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="判卷质量基线（合成照片压测）")
    ap.add_argument("--photos", type=int, default=50, help="每份卷合成多少张照片")
    ap.add_argument("--quick", action="store_true", help="冒烟：每份卷 5 张")
    ap.add_argument("--seed", type=int, default=BASE_SEED)
    ap.add_argument("--repro", nargs=2, metavar=("TAG", "N"), help="重建某张样本照片")
    args = ap.parse_args(argv)
    if args.repro:
        return repro(args.repro[0], int(args.repro[1]), args.seed)
    n = 5 if args.quick else args.photos
    rep = run(n, seed=args.seed, verbose=not args.quick)
    o = rep["overall"]
    print("\n=== 汇总 ===")
    print("照片 %d 张 | 标定 %.1f%% | QR %.1f%% | 选择准确率 %.2f%% | unsure %.1f%% | "
          "裁切可用 %.1f%% | p50 %.2fs p95 %.2fs max %.2fs | 需人工确认单元 %.1f%%" % (
              o["photos"], o["warp_rate"] * 100, o["qr_rate"] * 100,
              o["choice_accuracy"] * 100, o["unsure_rate"] * 100,
              o["crop_ok_rate"] * 100, o["timing"]["p50"], o["timing"]["p95"],
              o["timing"]["max"], o["manual_confirm_rate"] * 100))
    print("报表：%s" % os.path.join(OUT_DIR, "report.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
