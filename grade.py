# -*- coding: utf-8 -*-
"""grade.py · 判卷流水线（T8）

同步阶段（毫秒级）：ArUco 标定 → 复原到 2480×3508 → 解卷号 QR → 判选择题（OMR）。
填空/解答的「最终答案框」在同步阶段标 `pending` 并把裁切图落盘，
由 `fill_pending()` 在后台跑识别（CONTRACT §4.1）后回填判定。

判卷只判：选择题序号、填空/解答的最终答案。过程区不参与判分。

铁律：
* 任何异常都不许抛给调用方：标定失败 → {"error": "warp_failed"}，串卷 → {"error": "pid_mismatch"}；
* 拿不准就 `unsure`（带 crop_path 供人工确认），识别不出就留在 `pending`，**绝不静默判错**；
* 标定失败不落库，避免脏 submission。

对外 API（CONTRACT §4）：
    grade_image(conn, pid, image_path, ocr=None, out_dir=None) -> dict
    fill_pending(conn, sid, recognizer=None) -> dict
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

import cv2
import numpy as np

# app/select.py 会遮蔽 stdlib select；ocr 模块在 import 时已自救，这里保证它先被导入
import ocr as _ocr
import card
import normalize

APP_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(APP_DIR, "out")

PAGE_W = card.PAGE_W
PAGE_H = card.PAGE_H
WORK_MAX = 1400            # 检测用工作图最长边（手机照片动辄 4000px，先缩）
OMR_INK_THRESHOLD = 0.35   # 框内墨迹占比下限
OMR_MARGIN = 0.15          # 与次优框的最小差距
OMR_INSET_MIN = 5

KIND_OMR = card.KIND_OMR
KIND_FILL = card.KIND_FILL
KIND_SOLUTION = card.KIND_SOLUTION

VERDICTS = ("correct", "wrong", "unsure", "pending", "ungraded")


# ---------------------------------------------------------------- 小工具
def _err(code: str, detail: str = "", pid: str = None) -> dict:
    return {"sid": None, "pid": pid, "warp_ok": False, "qr_ok": False,
            "score": 0, "full": 0, "items": [], "error": code, "detail": str(detail)}


def _json_loads(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def _new_sid(now=None) -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    return "S%s-%s" % (ts, uuid.uuid4().hex[:6])


def _now_iso(now=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(now or time.time()))


def _load_manifest(conn, pid: str):
    try:
        row = conn.execute("SELECT manifest FROM papers WHERE pid=?", (pid,)).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    manifest = _json_loads(row["manifest"])
    return manifest if isinstance(manifest, dict) else None


#: 读图上限：手机 12MP 正常，避免 8000px 全景图把内存/耗时打爆
MAX_SIDE = 4096

#: HEIC/HEIF 支持（pillow-heif 装了才开；没装就给可读中文错误）
try:                                     # pragma: no cover - 环境相关
    from pillow_heif import register_heif_opener as _register_heif
    _register_heif()
    _HEIF_OK = True
except Exception:                        # pragma: no cover
    _HEIF_OK = False

HEIC_MSG = "暂不支持 HEIC，请用 JPG/PNG 或截图后再传（iPhone 可在「设置-相机-格式」改兼容性最佳）"
WARP_HINT = "请让答题卡四角黑框完整入镜、避免反光/阴影，光线不足时开闪光或换个角度重拍"

_HEIF_EXTS = (".heic", ".heif")


def _work_image(full):
    """原图 → (检测用工作图, scale)。"""
    h, w = full.shape[:2]
    if max(h, w) > WORK_MAX:
        scale = WORK_MAX / float(max(h, w))
        work = cv2.resize(full, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                          interpolation=cv2.INTER_AREA)
        return work, scale
    return full, 1.0


def _read_photo(image_path: str, max_side: int = None):
    """真机照片读图：EXIF 转正 + 格式识别 + 尺寸上限。

    → (gray | None, meta dict, error_code, error_detail)
    meta 含：format/bytes/original_size/size/orientation/transposed/clamped/camera/taken_at
    """
    max_side = MAX_SIDE if max_side is None else int(max_side)
    meta = {}
    if not image_path or not os.path.exists(image_path):
        return None, meta, "image_unreadable", "读图失败：文件不存在（%s）" % image_path
    ext = os.path.splitext(str(image_path))[1].lower()
    if ext in _HEIF_EXTS and not _HEIF_OK:
        return None, meta, "unsupported_format", HEIC_MSG
    try:
        meta["bytes"] = os.path.getsize(image_path)
    except OSError:
        meta["bytes"] = 0

    from PIL import Image, ImageOps
    try:
        with Image.open(image_path) as im:
            meta["format"] = im.format or ""
            meta["original_size"] = [im.width, im.height]
            orientation, camera, taken_at = 1, "", ""
            try:
                ex = im.getexif()
                if ex:
                    try:
                        orientation = int(ex.get(0x0112) or 1)
                    except Exception:
                        orientation = 1
                    make = str(ex.get(0x010F) or "").strip()
                    model = str(ex.get(0x0110) or "").strip()
                    taken_at = str(ex.get(0x0132) or ex.get(0x9003) or "").strip()
                    camera = ("%s %s" % (make, model)).strip()
            except Exception:
                pass
            meta["orientation"] = orientation
            meta["camera"] = camera
            meta["taken_at"] = taken_at
            upright = ImageOps.exif_transpose(im)
            meta["transposed"] = bool(orientation not in (0, 1, None))
            arr = np.array(upright.convert("L"))
    except Exception as e:
        if ext in _HEIF_EXTS and not _HEIF_OK:
            return None, meta, "unsupported_format", HEIC_MSG
        return None, meta, "image_unreadable", "读图失败（不是图片或文件损坏）：%s" % e

    h, w = arr.shape[:2]
    if max(h, w) > max_side:
        scale = max_side / float(max(h, w))
        arr = cv2.resize(arr, (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                         interpolation=cv2.INTER_AREA)
        meta["clamped"] = True
    else:
        meta["clamped"] = False
    meta["size"] = [int(arr.shape[1]), int(arr.shape[0])]
    return np.ascontiguousarray(arr), meta, "", ""


def _read_gray(image_path: str):
    """兼容旧调用：读图 → (full_gray, work_gray, scale)；读不出返回 (None, None, 1.0)。"""
    full, _meta, _code, _detail = _read_photo(image_path)
    if full is None:
        return None, None, 1.0
    work, scale = _work_image(full)
    return full, work, scale


def detect_markers(gray) -> dict:
    """检出四角 ArUco → {id: (cx, cy)}（只认 0..3）。"""
    try:
        det = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(card.ARUCO_DICT_ID),
            cv2.aruco.DetectorParameters())
        corners, ids, _ = det.detectMarkers(gray)
    except Exception:
        return {}
    out = {}
    if ids is None:
        return out
    for c, i in zip(corners, ids.ravel()):
        mid = int(i)
        if mid not in card.ARUCO_POS:
            continue
        pts = c.reshape(-1, 2)
        out[mid] = (float(pts[:, 0].mean()), float(pts[:, 1].mean()))
    return out


def detect_markers_with_mirror(work):
    """ArUco 检测：常规失败时再试一次水平翻转（前置摄像头镜像照片）。

    → (centers, mirrored)；镜像时把坐标翻回原图坐标系，单应会顺带把内容“反镜像”。
    """
    centers = detect_markers(work)
    if len(centers) >= 4:
        return centers, False
    try:
        flipped = np.ascontiguousarray(work[:, ::-1])
        c2 = detect_markers(flipped)
    except Exception:
        return centers, False
    if len(c2) < 4:
        return centers, False
    w = work.shape[1]
    return {k: (float(w - 1 - v[0]), float(v[1])) for k, v in c2.items()}, True


def homography_from_markers(centers: dict):
    """四角中心 → 单应矩阵（照片坐标 → 规范页坐标）。"""
    if not centers or len(centers) < 4:
        return None
    src, dst = [], []
    for mid, (cx, cy) in centers.items():
        px, py = card.ARUCO_POS[mid]
        half = card.ARUCO_SIZE / 2.0
        src.append([cx, cy])
        dst.append([px + half, py + half])
    src = np.float32(src)
    dst = np.float32(dst)
    try:
        if len(src) == 4:
            return cv2.getPerspectiveTransform(src, dst)
        M, _ = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        return M
    except Exception:
        return None


# ---------------------------------------------------------------- OMR
def _ink_ratio(gray, cell: dict) -> float:
    x, y, w, h = int(cell["x"]), int(cell["y"]), int(cell["w"]), int(cell["h"])
    inset = max(OMR_INSET_MIN, min(w, h) // 5)
    patch = gray[y + inset:y + h - inset, x + inset:x + w - inset]
    if patch is None or patch.size == 0:
        return 0.0
    return float((patch < 128).mean())


def read_omr(gray, cells) -> tuple:
    """选择格组 → (picked_opt | None, [(opt, ratio)], confidence)。

    规则：墨迹占比最大者 ≥0.35 且与次优差 ≥0.15 才算选中，否则 None（unsure）。
    """
    ratios = [(str(c["opt"]), _ink_ratio(gray, c)) for c in cells or []]
    if not ratios:
        return None, [], 0.0
    ratios_sorted = sorted(ratios, key=lambda t: t[1], reverse=True)
    best_opt, best = ratios_sorted[0]
    second = ratios_sorted[1][1] if len(ratios_sorted) > 1 else 0.0
    if best < OMR_INK_THRESHOLD or (best - second) < OMR_MARGIN:
        return None, ratios, round(min(best, 0.99), 3)
    return best_opt, ratios, round(min(0.99, 0.5 + best / 2.0), 3)


# ---------------------------------------------------------------- 判错/判对
#: 允许出现在答案里的非 ASCII 数学相关字符（其余汉字/乱码一律当“可能识别串行”）
UNSURE_ALLOWED_CHARS = set("或和度元角分秒±√°（）")
UNSURE_LEN_RATIO = 3.0     # 学生答案长度 ≥3 倍 或 ≤1/3 → 疑似识别串行
UNSURE_CONF_MIN = 0.5      # 后端真报置信度时才用；paddle_vl 恒 None → 跳过


def _suspicious_text(text: str, expected: str, confidence=None):
    """t17 的可解释 unsure 启发式 → (是否可疑, 中文原因)。

    只用来把 `wrong` 抬成 `unsure`（判对的不动），每条原因都能给 UI 显示。
    """
    t = str(text or "")
    e = str(expected or "")
    if not t:
        return False, ""

    # (b) 非数学字符：汉字/乱码/控制符（白名单里的数学用字除外）
    bad = []
    for ch in t:
        if ch in UNSURE_ALLOWED_CHARS:
            continue
        if ch.isascii():
            continue
        bad.append(ch)
    if bad:
        shown = "".join(sorted(set(bad))[:6])
        return True, "答案含非数学字符「%s」，疑似识别串行" % shown

    # (c) 长度比例异常（期望太短时不启用，避免误伤）
    if len(e) >= 2:
        lt, le = len(t), len(e)
        if lt >= UNSURE_LEN_RATIO * le or lt * UNSURE_LEN_RATIO <= le:
            return True, "答案长度异常（学生 %d 字 vs 期望 %d 字），疑似串行" % (lt, le)

    # (d) 后端真给了置信度且很低
    if confidence is not None:
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf = None
        if conf is not None and conf < UNSURE_CONF_MIN:
            return True, "识别置信度低（%.2f < %.1f）" % (conf, UNSURE_CONF_MIN)

    return False, ""


#: 多值答案分隔符（t39：中文/英文逗号、分号，或连续空格）
VALUE_SEP_RE = re.compile(r"[，,;；]|\s{2,}")


def split_values(text) -> list:
    """把「A，54」这类多值答案按分隔符拆开（保留顺序，空段丢掉）。"""
    return [p.strip() for p in VALUE_SEP_RE.split(str(text or "")) if p.strip()]


def _match_distinct(vals, expected_vals, qtype) -> tuple:
    """vals 逐个去匹配 expected_vals（一一对应）→ (匹配数, 是否有值匹配不上)。"""
    used, matched, unmatched = set(), 0, False
    for v in vals:
        hit = None
        for i, e in enumerate(expected_vals):
            if i in used:
                continue
            try:
                ok = normalize.is_equivalent(v, e, qtype or "填空")[0]
            except Exception:
                ok = False
            if ok:
                hit = i
                break
        if hit is None:
            unmatched = True
        else:
            used.add(hit)
            matched += 1
    return matched, unmatched


def _multi_value_verdict(text, expected, qtype):
    """多值期望 → 明确结论；返回 None 表示「不是多值题，走原逻辑」。

    覆盖 4 种情况：
      * 全部值都在、个数相等      → correct
      * 只识别到真子集（都等价）  → unsure「只识别到部分答案（缺 X 个值）」（永不静默判错）
      * 个数相等但有值对不上      → wrong（真错）
      * 识别出多余的值            → unsure（宁可人工确认）
    """
    exp_vals = split_values(expected)
    if len(exp_vals) < 2:
        return None
    got_vals = split_values(text)
    if not got_vals:
        return None
    matched, unmatched = _match_distinct(got_vals, exp_vals, qtype)
    if len(got_vals) < len(exp_vals):
        if unmatched:
            return "wrong", "多值答案有值对不上：%d/%d 个值能对应" % (matched, len(exp_vals))
        missing = len(exp_vals) - matched
        return "unsure", "只识别到部分答案（缺 %d 个值）" % missing
    if len(got_vals) == len(exp_vals):
        if unmatched:
            return "wrong", "多值答案有值对不上：%d/%d 个值能对应" % (matched, len(exp_vals))
        return "correct", "多值全部一致（%d 个值）" % len(exp_vals)
    # 值比期望多：可能多写/串行，交人工
    return "unsure", "识别到多余的值（%d 个 vs 期望 %d 个），需人工确认" % (
        len(got_vals), len(exp_vals))


def _judge(text: str, expected: str, qtype: str, confidence=None):
    """→ (verdict, reason)；expected 为空 = 该空不判分。

    判错但“看起来不对劲”时抬成 unsure，并把原因写进 reason（UI 显示“为什么待确认”）。
    """
    if not str(expected or "").strip():
        return "ungraded", "无标准答案"
    try:
        ok, reason = normalize.is_equivalent(text, expected, qtype or "填空")
    except Exception as e:      # normalize 出问题也不能崩，交人工
        return "unsure", "归一化异常: %s" % e
    if ok:
        return "correct", reason
    multi = _multi_value_verdict(text, expected, qtype)
    if multi:
        return multi        # 多值题给明确结论，不再被「含逗号=非数学字符」的启发式误导
    suspicious, why = _suspicious_text(text, expected, confidence)
    if suspicious:
        return "unsure", why
    return "wrong", reason


def _score(units) -> tuple:
    full = sum(1 for it in units if it.get("verdict") != "ungraded")
    score = sum(1 for it in units if it.get("verdict") == "correct")
    return score, full


def _save_crop(gray, box: dict, crop_dir: str, name: str, expand: bool = False):
    """裁切作答格。expand=True（多值题）：右侧 +8%、下侧 +12%，别把行尾的「, 54」切掉。"""
    try:
        os.makedirs(crop_dir, exist_ok=True)
        b = dict(box)
        if expand:
            b["w"] = min(PAGE_W - int(b["x"]) - 2, int(round(b["w"] * 1.08)))
            b["h"] = min(PAGE_H - int(b["y"]) - 2, int(round(b["h"] * 1.12)))
        patch = card.crop_cell(gray, b, inset=6)
        if patch is None or patch.size == 0:
            return None
        path = os.path.join(crop_dir, "%s.png" % name)
        cv2.imwrite(path, patch)
        return path
    except Exception:
        return None


# ---------------------------------------------------------------- 主流程
# ---------------------------------------------------------------- 无 manifest 判卷（t46）
def _runs(mask, min_len: int = 12):
    """一维布尔序列里的连续 True 段 → [(start, end)]。"""
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    if start is not None and len(mask) - start >= min_len:
        out.append((start, len(mask)))
    return out


def _merge_runs(runs, gap: int = 12):
    """把间隔很小的 run 合并（翻白的格子会把一个标识块切成两段）。"""
    out = []
    for a, b in runs:
        if out and a - out[-1][1] <= gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _id_bits_at(gray, x0: int, y0: int):
    """在 (x0,y0) 处按 5×5/24px 采样 → 25 个 0/1；越界返回 None。"""
    bits = []
    cell = card.ID_CELL
    h, w = gray.shape[:2]
    for r in range(card.ID_GRID):
        for c in range(card.ID_GRID):
            y = y0 + r * cell + 4
            x = x0 + c * cell + 4
            if y + cell - 8 > h or x + cell - 8 > w or y < 0 or x < 0:
                return None
            patch = gray[y:y + cell - 8, x:x + cell - 8]
            if patch.size == 0:
                return None
            bits.append(1 if float(patch.mean()) < 128.0 else 0)
    return bits


def _legacy_bits_at(gray, x0: int, y0: int):
    """旧 4×4（24px）采样，只用于识别「这是旧版本卡」。"""
    bits = []
    cell = 24
    h, w = gray.shape[:2]
    for r in range(4):
        for c in range(4):
            y, x = y0 + r * cell + 4, x0 + c * cell + 4
            if y + cell - 8 > h or x + cell - 8 > w or y < 0 or x < 0:
                return None
            patch = gray[y:y + cell - 8, x:x + cell - 8]
            if patch.size == 0:
                return None
            bits.append(1 if float(patch.mean()) < 128.0 else 0)
    return bits


def _cell_stats(gray, x, y, cell):
    """格内 (均值, 标准差)；越界返回 (None, None)。"""
    h, w = gray.shape[:2]
    x0, y0 = int(x) + 3, int(y) + 3
    x1, y1 = x0 + cell - 6, y0 + cell - 6
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
        return None, None
    patch = gray[y0:y1, x0:x1]
    return float(patch.mean()), float(patch.std())


def _cell_mean(gray, x, y, cell):
    return _cell_stats(gray, x, y, cell)[0]


def _classify_block(gray, x0, y0, grid, cell):
    """采样一个 grid×grid 方块 → ("bits", bits) / ("bad", None) / ("none", None)。

    自适应阈值（按 25 格亮度中分），手机光照/JPEG 都不会把黑白判糊；
    整块全黑/全白分别代表「被涂黑」和「这里没有标识」。
    """
    vals, stds = [], []
    for r in range(grid):
        for c in range(grid):
            m, sd = _cell_stats(gray, x0 + c * cell, y0 + r * cell, cell)
            if m is None:
                return "none", None
            vals.append(m)
            stds.append(sd)
    # 真标识格每格要么全黑要么全白（格内方差小）；文字/错位采样会花
    if sum(1 for sd in stds if sd < 45) < grid * grid - 3:
        return "none", None
    lo, hi = min(vals), max(vals)
    if hi - lo < 80:
        return ("bad", None) if lo < 80 else ("none", None)
    thr = (lo + hi) / 2.0
    return "bits", [1 if v < thr else 0 for v in vals]


def _frame_is_dark(gray, x0: int, y0: int, size: int, t: int = 2) -> bool:
    """标识块外面画了一圈 2px 黑框 —— 用它当锚点，比投影稳得多。"""
    h, w = gray.shape[:2]
    x1, y1 = x0 + size, y0 + size
    if x0 - t - 1 < 0 or y0 - t - 1 < 0 or x1 + t + 1 > w or y1 + t + 1 > h:
        return False
    bands = [gray[y0 - t - 1:y0 - 1, x0:x1],
             gray[y1 + 1:y1 + t + 1, x0:x1],
             gray[y0:y1, x0 - t - 1:x0 - 1],
             gray[y0:y1, x1 + 1:x1 + t + 1]]
    return all(b.size and float(b.mean()) < 140 for b in bands)


def _try_decode_at(gray, base_x: int, y: int, size: int, grid: int, cell: int):
    """在 y 附近微调找可解的标识 → (y, no) 或 (None, None)。"""
    h = gray.shape[0]
    for dy in (0, 2, -2, 4, -4):
        yy = y + dy
        if yy < 0 or yy + size > h:
            continue
        kind, bits = _classify_block(gray, base_x, yy, grid, cell)
        if kind == "bits":
            no = card.decode_id_bits(bits)
            if no:
                return yy, no
    return None, None


def _scan_column(gray, base_x: int, col: int, out: dict):
    """单遍扫一列：读到 CRC 的进 blocks，像标识但读不出的进 unreadable（该题 unsure）。"""
    h, w = gray.shape[:2]
    y_from = card.BODY_RECT[1] - 30
    y_to = min(h, card.BODY_RECT[1] + card.BODY_RECT[3])
    cell, grid, size = card.ID_CELL, card.ID_GRID, card.ID_SIZE
    if base_x + size > w:
        return
    good, bad = [], []
    y = y_from
    while y + size <= y_to:
        win = gray[y:y + size, base_x:base_x + size]
        if win.size == 0:
            y += 2
            continue
        mean = float(win.mean())
        if mean < 70:                     # 整块涂黑
            bad.append(y)
            y += size + 4
            continue
        if float(win.min()) < 90 and float(win.max()) > 160 and float(win.std()) > 40:
            yy, no = _try_decode_at(gray, base_x, y, size, grid, cell)
            if no:
                good.append((yy, no))
                y = yy + size + 4
                continue
            kind, bits = _classify_block(gray, base_x, y, grid, cell)
            if kind == "bad" or (kind == "bits" and 3 <= sum(bits) <= 22):
                bad.append(y)
                y += size + 4
                continue
        elif not good and mean < 200:     # 旧 4×4 卡
            k4, b4 = _classify_block(gray, base_x, y, 4, 24)
            if k4 == "bits" and card.legacy_idx_bits(b4) is not None:
                out["legacy"] += 1
                y += 4 * 24 + 4
                continue
        y += 2

    for yy, no in good:
        out["blocks"].append({"x": base_x, "y": yy, "no": no, "col": col})
    for yb in bad:
        if any(abs(yb - yy) < size for yy, _ in good):
            continue
        out["unreadable"].append({"x": base_x, "y": yb, "col": col})


def scan_id_blocks(gray) -> dict:
    """扫页面上所有标识块（5×5=25 位，编码题号）。

    只在每列标识左缘（`card.ID_ORIGIN_X`）所在的竖带里找，靠「25 格黑白分明 + CRC-5」
    确认，所以顶行全白、光照不匀都不会漏；解不出校验的块记 `unreadable`（该题进 unsure）。
    """
    out = {"blocks": [], "unreadable": [], "legacy": 0}
    for col, base_x in enumerate(card.ID_ORIGIN_X):
        _scan_column(gray, base_x, col, out)
    # 去重：同一个坏块可能被相邻几个窗口重复命中
    def _dedupe(items):
        keep = []
        for it in sorted(items, key=lambda x: (x["col"], x["y"])):
            if keep and it["col"] == keep[-1]["col"] and it["y"] - keep[-1]["y"] < card.ID_SIZE + 8:
                continue
            keep.append(it)
        return keep

    out["blocks"] = _dedupe(out["blocks"])
    out["unreadable"] = _dedupe([u for u in out["unreadable"]
                                 if not any(abs(u["y"] - b["y"]) < card.ID_SIZE
                                            and u["col"] == b["col"] for b in out["blocks"])])
    out["blocks"].sort(key=lambda b: (b["y"], b["col"]))
    out["unreadable"].sort(key=lambda b: (b["y"], b["col"]))
    return out


def scan_ids(image_path):
    """拍照 → 标定 → 扫标识块（给 UI/测试用的轻量入口）。"""
    norm, photo_meta, code, why = _warp_photo(image_path)
    if norm is None:
        return {"error": code or "warp_failed", "detail": why, "blocks": [],
                "unreadable": [], "legacy": 0}
    out = scan_id_blocks(norm)
    out["photo"] = photo_meta
    return out


def _quick_legacy_probe(gray) -> int:
    """粗扫旧 4×4 标识（16 格 CRC-4）→ 命中数。命中 ≥2 基本可断定是旧卡。"""
    hits = 0
    size4 = 4 * 24
    h = gray.shape[0]
    y_to = min(h, card.BODY_RECT[1] + card.BODY_RECT[3])
    for base_x in card.ID_ORIGIN_X:
        y = card.BODY_RECT[1] - 20
        while y + size4 <= y_to:
            k4, b4 = _classify_block(gray, base_x, y, 4, 24)
            if k4 == "bits" and card.legacy_idx_bits(b4) is not None:
                hits += 1
                y += size4 + 4
                continue
            y += 16
    return hits


def _question_row(conn, no: str):
    try:
        row = conn.execute(
            "SELECT no,qtype,option_count,sub_count,answer_raw FROM questions WHERE no=?",
            (str(no),)).fetchone()
    except Exception:
        return None
    return row


def _answer_key_of(row):
    """questions 行 → answer_key（复用 picker 的拆解口径）。"""
    try:
        import picker
        return picker.parse_answer_key(row["answer_raw"] or "", row["qtype"] or "")
    except Exception:
        return []


def grade_by_ids(conn, image_path, out_dir: str = None) -> dict:
    """无 manifest 判卷：靠每题 5×5 标识里的题号查库、反推几何。永不抛异常。"""
    out_root = out_dir or OUT_ROOT
    try:
        return _grade_by_ids(conn, image_path, out_root)
    except Exception as e:
        return _err("internal_error", "%s: %s" % (type(e).__name__, e), None)


def _grade_by_ids(conn, image_path, out_root):
    norm, photo_meta, code, why = _warp_photo(image_path)
    if norm is None:
        return _err(code or "warp_failed", why, None)
    qr_payload = card.read_qr(norm, {"x": card.QR_POS[0], "y": card.QR_POS[1],
                                     "size": card.QR_SIZE})
    qr_ok = qr_payload is not None
    pid_hint = (qr_payload or {}).get("pid")

    legacy_hits = _quick_legacy_probe(norm)
    if legacy_hits >= 2:      # 旧卡：先给明确错误，别耗时间扫 5×5
        return _err("legacy_card", "这张卡是旧版本（4×4 题目标识），请重新打印答题卡。", pid_hint)
    scan = scan_id_blocks(norm)
    if not scan["blocks"]:
        if legacy_hits or scan["legacy"]:
            return _err("legacy_card", "这张卡是旧版本（4×4 标识），请重新打印答题卡。", pid_hint)
        return _err("no_question_ids",
                    "标定成功，但没扫到任何题目标识。请确认拍到的是答题卡作答区。", pid_hint)

    sid = _new_sid()
    crop_dir = os.path.join(out_root, str(pid_hint or "no_pid"), "sub_%s" % sid)
    items, by_idx, unknown, unreadable_units = [], {}, [], []
    seq = 0
    events = [("ok", b) for b in scan["blocks"]] + [("bad", b) for b in scan["unreadable"]]
    events.sort(key=lambda e: (e[1]["y"], e[1]["col"]))
    for kind_ev, b in events:
        seq += 1
        if kind_ev == "bad":
            unreadable_units.append({
                "idx": seq, "no": None, "qtype": "", "kind": "id", "blank": 1,
                "picked": None, "expected": "", "verdict": "unsure",
                "confidence": 0.0, "reason": "题目标识不可读",
                "unsure_reason": "题目标识不可读", "ratios": {},
                "crop_path": _save_crop(norm, {"x": b["x"], "y": b["y"],
                                               "w": card.ID_SIZE, "h": card.ID_SIZE},
                                        crop_dir, "id_%d" % seq),
            })
            continue
        row = _question_row(conn, b["no"])
        if row is None:
            unknown.append(b["no"])
            continue
        qtype = row["qtype"] or ""
        key = _answer_key_of(row)
        kind = card.kind_of({"qtype": qtype})
        n_opt = int(row["option_count"] or 4) if row["option_count"] is not None else 4
        n_blank = max(1, len(key))
        geom = card.plan_from_anchor(b["x"], b["y"], kind, n_options=n_opt, n_blanks=n_blank)
        it = {"idx": seq, "no": b["no"], "qtype": qtype, "kind": kind,
              "id_box": dict(geom["id_box"]),
              "id_bits": card.encode_id_bits(b["no"]),
              "label_x": geom.get("label_x", card.CONTENT_X)}
        if kind == KIND_OMR:
            it["cells"] = geom.get("cells") or []
        elif kind == KIND_FILL:
            it["boxes"] = geom.get("boxes") or []
        else:
            it["process_box"] = geom.get("process_box")
            it["answer_boxes"] = geom.get("answer_boxes") or []
        items.append(it)
        by_idx[seq] = {"answer_key": key, "qtype": qtype, "no": b["no"]}

    if not items and unknown:
        return _err("unknown_questions",
                    "扫到 %d 个题目标识，但本机题库里没有这些题号（%s…）。"
                    "可能是别的题库/别的版本，或题目被删了。" % (
                        len(unknown), "、".join(unknown[:5])), pid_hint)
    units, pending_units = _units_for_page(norm, items, by_idx, crop_dir)
    units.extend(unreadable_units)

    score, full = _score(units)
    detail = {"version": 1, "pid": pid_hint, "sid": sid,
              "identified_by": "question_id", "qr": qr_payload, "qr_ok": qr_ok,
              "page": None, "score": score, "full": full, "backend": "manual",
              "photo": photo_meta, "items": units,
              "unreadable_ids": len(unreadable_units), "unknown_nos": unknown[:20]}
    _save_submission(conn, sid, pid_hint or "", image_path, True, qr_ok, score, full, detail)
    return {"sid": sid, "pid": pid_hint, "warp_ok": True, "qr_ok": qr_ok,
            "score": score, "full": full, "error": None, "detail": "",
            "identified_by": "question_id",
            "pending": len([u for u in units if u["verdict"] == "pending"]),
            "unreadable_ids": len(unreadable_units), "unknown_nos": unknown[:20],
            "items": units}


def grade_image(conn, pid: str, image_path: str, ocr=None, out_dir: str = None) -> dict:
    """拍照 → 标定 → 判选择（同步）→ 填空/解答待识别。永不抛异常。"""
    out_root = out_dir or OUT_ROOT
    try:
        manifest = _load_manifest(conn, pid) if pid else None
        if manifest:
            return _grade_image(conn, pid, image_path, ocr=ocr, out_root=out_root)
        # 没有卷号 / 库里查不到 → 靠每题标识独立判卷（t46）
        res = _grade_by_ids(conn, image_path, out_root)
        if res.get("error") is None:
            res["fallback_from_pid"] = pid
            return res
        if pid and res.get("error") in ("no_question_ids", "legacy_card"):
            res["pid"] = pid
            res["detail"] = "卷号 %s 不在本机库中；%s" % (pid, res.get("detail") or "")
        return res
    except Exception as e:      # 兜底
        return _err("internal_error", "%s: %s" % (type(e).__name__, e), pid)


def _units_for_page(norm, page_items, by_idx, crop_dir):
    """把一页的作答单元逐个判出来 → (units, pending_units)。"""
    units = []
    pending_units = []
    for it in page_items:
        src = by_idx.get(it.get("idx")) or {}
        keys = {int(k.get("blank", 1)): k for k in (src.get("answer_key") or [])}
        qtype = src.get("qtype") or it.get("qtype") or ""
        if it.get("kind") == KIND_OMR:
            picked, ratios, conf = read_omr(norm, it.get("cells"))
            expected = str((keys.get(1) or {}).get("value") or "")
            if not expected:
                verdict, reason = "ungraded", "无标准答案"
            elif picked is None:
                verdict, reason = "unsure", "涂黑不唯一/太淡，需人工确认"
            else:
                verdict, reason = _judge(picked, expected, "选择")
            unsure_reason = reason if verdict == "unsure" else ""
            units.append({
                "idx": it.get("idx"), "no": it.get("no"), "qtype": qtype,
                "kind": KIND_OMR, "blank": 1,
                "picked": picked, "expected": expected, "verdict": verdict,
                "confidence": conf, "reason": reason, "unsure_reason": unsure_reason,
                "ratios": {opt: round(r, 3) for opt, r in ratios},
                "crop_path": _save_crop(norm, it.get("cells")[0], crop_dir,
                                        "%d-1" % it.get("idx")) if it.get("cells") else None,
            })
            continue

        boxes = it.get("boxes") or it.get("answer_boxes") or []
        for b in boxes:
            blank = int(b.get("blank", 1))
            expected = str((keys.get(blank) or {}).get("value") or "")
            multi = len(split_values(expected)) >= 2      # t39：多值题放宽裁切
            crop = _save_crop(norm, b, crop_dir, "%d-%d" % (it.get("idx"), blank),
                              expand=multi)
            unit = {
                "idx": it.get("idx"), "no": it.get("no"), "qtype": qtype,
                "kind": it.get("kind"), "blank": blank,
                "picked": "", "expected": expected,
                "verdict": "pending" if expected else "ungraded",
                "confidence": 0.0,
                "reason": "待识别" if expected else "无标准答案",
                "box": {"x": b.get("x"), "y": b.get("y"), "w": b.get("w"), "h": b.get("h")},
                "crop_path": crop,
                "multi_value": multi,
                "crop_expanded": bool(multi),
                "expected_values": len(split_values(expected)),
            }
            units.append(unit)
            if unit["verdict"] == "pending":
                pending_units.append(unit)

    return units, pending_units


def _warp_photo(image_path):
    """读图 → ArUco 标定 → 复原到 2480×3508。→ (norm|None, photo_meta, err_code, err_msg)"""
    full_img, photo_meta, code, why = _read_photo(image_path)
    if full_img is None:
        return None, photo_meta, code or "warp_failed", why or ("读图失败: %s" % image_path)
    work, scale = _work_image(full_img)
    centers, mirrored = detect_markers_with_mirror(work)
    if mirrored:
        photo_meta["mirror_fixed"] = True
    if len(centers) < 4:
        return None, photo_meta, "warp_failed", "只检出 %d/4 个 ArUco。%s" % (
            len(centers), WARP_HINT)
    if scale and scale != 1.0:
        centers = {k: (v[0] / scale, v[1] / scale) for k, v in centers.items()}
    M = homography_from_markers(centers)
    if M is None:
        return None, photo_meta, "warp_failed", "四角单应矩阵求解失败。%s" % WARP_HINT
    try:
        norm = cv2.warpPerspective(full_img, M, (PAGE_W, PAGE_H),
                                   flags=cv2.INTER_LINEAR, borderValue=255)
    except Exception as e:
        return None, photo_meta, "warp_failed", "透视复原失败（%s）。%s" % (e, WARP_HINT)
    return norm, photo_meta, None, ""


def _looks_legacy(gray, page) -> bool:
    """第一题的标识位置：5×5 解不出、旧 4×4 解得出来 → 是老卡。"""
    items = page.get("items") or []
    if not items:
        return False
    b = items[0].get("id_box") or {}
    if not b:
        return False
    x, y = int(b.get("x", 0)), int(b.get("y", 0))
    kind, bits = _classify_block(gray, x, y, card.ID_GRID, card.ID_CELL)
    if kind == "bits" and card.decode_id_bits(bits):
        return False
    k4, b4 = _classify_block(gray, x, y, 4, 24)
    return bool(k4 == "bits" and card.legacy_idx_bits(b4) is not None)


def _grade_image(conn, pid, image_path, ocr, out_root):
    manifest = _load_manifest(conn, pid)
    if not manifest:
        return _err("paper_not_found", "papers 表里没有 %s" % pid, pid)
    card_info = manifest.get("card") or {}
    pages = card_info.get("pages") or []
    if not pages:
        return _err("no_card", "该卷没有 card 几何（还没生成答题卡？）", pid)

    norm, photo_meta, code, why = _warp_photo(image_path)
    if norm is None:
        return _err(code or "warp_failed", why, pid)

    # ---- 卷号 QR：防串卷
    qr_payload, qr_page = None, None
    for page in pages:
        payload = card.read_qr(norm, page["qr"])
        if payload:
            qr_payload, qr_page = payload, page
            break
    if qr_payload and str(qr_payload.get("pid") or "") not in ("", str(pid)):
        return _err("pid_mismatch",
                    "QR 卷号 %s ≠ 传入 %s（拍错卷子？）" % (qr_payload.get("pid"), pid), pid)
    qr_ok = qr_payload is not None
    page = qr_page
    if page is None:
        # QR 失效：按传入 pid 继续，用第一页几何（结果里 qr_ok=false）
        page = pages[0]
    elif qr_payload.get("p") is not None:
        for p in pages:
            if p.get("page") == qr_payload.get("p"):
                page = p
                break

    # ---- 老卡识别（4×4 idx 版）：明确报 legacy_card，别拿新坐标去套旧卡
    if _looks_legacy(norm, page):
        return _err("legacy_card",
                    "这张卡是旧版本（4×4 题目标识），请用当前版本重新打印答题卡。", pid)

    sid = _new_sid()
    crop_dir = os.path.join(out_root, str(pid), "sub_%s" % sid)
    by_idx = {it.get("idx"): it for it in (manifest.get("items") or [])}

    units, pending_units = _units_for_page(norm, page.get("items") or [],
                                          by_idx, crop_dir)

    # 调用方显式传了识别器 → 同步就地识别（默认 None = 走两阶段）
    backend = "manual"
    if ocr is not None and pending_units:
        got = _recognize(ocr, pending_units)
        backend = _apply_recognition(pending_units, got) or "manual"

    score, full = _score(units)
    detail = {
        "version": 1, "pid": pid, "sid": sid,
        "qr": qr_payload, "qr_ok": qr_ok, "page": page.get("page"),
        "score": score, "full": full, "backend": backend,
        "photo": photo_meta,          # t27：EXIF 方向/机型/拍摄时间/原始尺寸/是否clamp
        "items": units,
    }
    _save_submission(conn, sid, pid, image_path, True, qr_ok, score, full, detail)
    return {"sid": sid, "pid": pid, "warp_ok": True, "qr_ok": qr_ok,
            "score": score, "full": full, "error": None, "detail": "",
            "pending": len([u for u in units if u["verdict"] == "pending"]),
            "items": units}


def _save_submission(conn, sid, pid, image_path, warp_ok, qr_ok, score, full, detail):
    try:
        conn.execute(
            "INSERT OR REPLACE INTO submissions"
            "(sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, pid, _now_iso(), os.path.abspath(image_path) if image_path else "",
             1 if warp_ok else 0, 1 if qr_ok else 0, score, full,
             json.dumps(detail, ensure_ascii=False)))
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- 识别（两阶段）
def _recognize(recognizer, pend_units) -> dict:
    """一次 read_batch（云端只跑一个 job）；任何异常都当没识别出来。"""
    pairs = [("%d-%d" % (u["idx"], u["blank"]), u["crop_path"]) for u in pend_units
             if u.get("crop_path")]
    if not pairs:
        return {}
    try:
        return recognizer.read_batch(pairs) or {}
    except Exception:
        return {}


def _apply_recognition(pend_units, got: dict) -> str:
    """把识别结果写回 unit → 返回 backend 名。空文本留在 pending（人工确认）。"""
    backend = ""
    for u in pend_units:
        key = "%d-%d" % (u["idx"], u["blank"])
        res = got.get(key) or {}
        text = str(res.get("text") or "").strip()
        b = str(res.get("backend") or "").strip()
        if b:
            backend = b
        if not text:
            u["verdict"] = "pending"
            u["reason"] = "未识别（%s），待人工确认" % (b or "manual")
            u["confidence"] = 0.0
            continue
        u["picked"] = text
        conf = res.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        verdict, reason = _judge(text, u.get("expected"), u.get("qtype") or "填空", conf)
        u["verdict"] = verdict
        u["reason"] = reason
        u["confidence"] = conf if conf is not None else 0.0
        # 可解释的 unsure：把原因单独存一份，UI 显示“为什么待确认”
        if verdict == "unsure":
            u["unsure_reason"] = reason
        else:
            u.pop("unsure_reason", None)
    return backend


# ---------------------------------------------------------------- 掌握度接线（t17）
def _record_one(conn, no, correct, now=None):
    """记一次掌握度；train 不可用也绝不拖死判卷。"""
    try:
        from train import record_result
        return record_result(conn, no, correct, now=now)
    except Exception:
        return None


def _record_mastery(conn, units, now=None) -> int:
    """把所有 correct/wrong 的题喂给 train.record_result（**每题只记一次**）。

    同一题多个空按“有一个错就算错”合并；同一结论重复调用不再记账（幂等）。
    返回本次实际记账条数。
    """
    grouped = {}
    for u in units or []:
        no = str(u.get("no") or "")
        verdict = u.get("verdict")
        if not no or verdict not in ("correct", "wrong"):
            continue
        g = grouped.setdefault(no, {"verdict": "correct", "units": []})
        g["units"].append(u)
        if verdict == "wrong":
            g["verdict"] = "wrong"
    n = 0
    for no, g in grouped.items():
        verdict = g["verdict"]
        if all(u.get("mastery_verdict") == verdict for u in g["units"]):
            continue
        _record_one(conn, no, verdict == "correct", now=now)
        for u in g["units"]:
            u["mastery_verdict"] = verdict
        n += 1
    return n


def _default_recognizer():
    try:
        return _ocr.Recognizer()
    except Exception:
        return None


def fill_pending(conn, sid: str, recognizer=None) -> dict:
    """异步阶段：pending 的裁切图 → 识别 → 归一化判分 → 更新 detail 与分数（可重复调用）。"""
    try:
        return _fill_pending(conn, sid, recognizer)
    except Exception as e:
        return {"error": "internal_error", "detail": "%s: %s" % (type(e).__name__, e),
                "sid": sid}


def _fill_pending(conn, sid, recognizer):
    row = conn.execute("SELECT sid,pid,detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()
    if row is None:
        return {"error": "submission_not_found", "sid": sid}
    detail = _json_loads(row["detail_json"]) or {}
    units = detail.get("items") or []
    pend = [u for u in units if u.get("verdict") == "pending" and u.get("crop_path")]
    if not pend:
        score, full = _score(units)
        return {"sid": sid, "pid": row["pid"], "filled": 0, "correct": 0, "wrong": 0,
                "left_manual": 0, "backend": detail.get("backend") or "manual",
                "score": score, "full": full, "empty_batch": False, "recorded": 0,
                "error": None}

    recognizer = recognizer if recognizer is not None else _default_recognizer()
    got = _recognize(recognizer, pend) if recognizer is not None else {}
    backend = _apply_recognition(pend, got) or "manual"

    # 整批空结果：一个都没读出来 → 只标 empty_batch，pending 保持 pending（绝不改 wrong）
    filled = sum(1 for u in pend if str(u.get("picked") or "").strip())
    empty_batch = filled == 0

    score, full = _score(units)
    recorded = _record_mastery(conn, units)
    detail["backend"] = backend
    detail["score"], detail["full"] = score, full
    detail["recognized_at"] = _now_iso()
    detail["empty_batch"] = empty_batch
    _update_submission(conn, sid, score, full, detail)

    correct = sum(1 for u in pend if u.get("verdict") == "correct")
    wrong = sum(1 for u in pend if u.get("verdict") == "wrong")
    left_manual = sum(1 for u in pend if u.get("verdict") in ("pending", "unsure", "ungraded"))
    return {"sid": sid, "pid": row["pid"], "filled": filled, "correct": correct,
            "wrong": wrong, "left_manual": left_manual, "backend": backend,
            "score": score, "full": full, "empty_batch": empty_batch,
            "recorded": recorded, "error": None}


# ---------------------------------------------------------------- 人工确认（t17）
def apply_confirm(conn, sid: str, corrections: dict) -> dict:
    """人工确认/改判：{idx: {"verdict": "correct|wrong", "text": "..."}} → 重算并落库（幂等）。"""
    try:
        return _apply_confirm(conn, sid, corrections)
    except Exception as e:
        return {"error": "internal_error", "detail": "%s: %s" % (type(e).__name__, e),
                "sid": sid}


def _parse_corrections(corrections):
    """corrections 归一化 → (wide: {idx: corr}, narrow: {(idx, blank): corr})。

    支持三种键（都向后兼容）：
      * `{idx: {...}}`              整题所有空（原有行为）
      * `{"idx-blank": {...}}`      只改该空（JSON 友好，推荐）
      * `{(idx, blank): {...}}`     只改该空（Python 调用方便）
    """
    wide, narrow = {}, {}
    for k, v in (corrections or {}).items():
        if not isinstance(v, dict):
            continue
        if isinstance(k, tuple) and len(k) == 2:
            try:
                narrow[(str(int(k[0])), int(k[1]))] = v
            except (TypeError, ValueError):
                continue
            continue
        if isinstance(k, str):
            m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", k)
            if m:
                narrow[(str(int(m.group(1))), int(m.group(2)))] = v
                continue
            if k.strip().lstrip("+-").isdigit():
                wide[int(k)] = v
            continue
        try:
            wide[int(k)] = v
        except (TypeError, ValueError):
            continue
    return wide, narrow


def _apply_one_correction(u, corr) -> int:
    """把一条改判落到 unit 上 → 返回是否有变化（0/1）。"""
    verdict = str(corr.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        return 0
    text = corr.get("text")
    old_verdict, old_picked = u.get("verdict"), u.get("picked")
    if text is not None:
        u["picked"] = str(text)
    u["verdict"] = verdict
    u["confirmed"] = True
    u["reason"] = str(corr.get("reason") or "人工确认")
    u.pop("unsure_reason", None)
    if u.get("mastery_verdict") != verdict:
        u.pop("mastery_verdict", None)      # 结论变了 → 允许重新记掌握度
    changed = int(u.get("verdict") != old_verdict or u.get("picked") != old_picked)
    return changed


def _apply_confirm(conn, sid, corrections):
    row = conn.execute("SELECT sid,pid,detail_json FROM submissions WHERE sid=?", (sid,)).fetchone()
    if row is None:
        return {"error": "submission_not_found", "sid": sid}
    detail = _json_loads(row["detail_json"]) or {}
    units = detail.get("items") or []
    wide, narrow = _parse_corrections(corrections)

    changed = 0
    for u in units:
        idx = u.get("idx")
        key_idx = int(idx) if str(idx).strip().lstrip("+-").isdigit() else idx
        # 1) 整题
        corr = wide.get(key_idx)
        if corr is None and str(idx).strip().isdigit():
            corr = wide.get(int(str(idx).strip()))
        if isinstance(corr, dict):
            changed += _apply_one_correction(u, corr)
        # 2) 逐空覆盖（优先于整题）
        if u.get("blank") is not None:
            c2 = narrow.get((str(int(idx)) if str(idx).strip().isdigit() else str(idx),
                             int(u["blank"])))
            if isinstance(c2, dict):
                changed += _apply_one_correction(u, c2)

    score, full = _score(units)
    recorded = _record_mastery(conn, units)
    detail["score"], detail["full"] = score, full
    detail["confirmed_at"] = _now_iso()
    detail["item_verdicts"] = _item_verdicts(units)   # 聚合判定（一空错算整题错）
    _update_submission(conn, sid, score, full, detail)
    return {"sid": sid, "pid": row["pid"], "score": score, "full": full,
            "changed": changed, "recorded": recorded,
            "item_verdicts": detail["item_verdicts"], "error": None}


def _item_verdicts(units) -> dict:
    """按题聚合：有错即错 > unsure > pending > 全对。"""
    grouped = {}
    for u in units or []:
        key = str(u.get("idx"))
        grouped.setdefault(key, []).append(u.get("verdict"))
    out = {}
    for idx, vs in grouped.items():
        if "wrong" in vs:
            out[idx] = "wrong"
        elif "unsure" in vs:
            out[idx] = "unsure"
        elif "pending" in vs:
            out[idx] = "pending"
        elif vs and all(v == "correct" for v in vs):
            out[idx] = "correct"
        else:
            out[idx] = vs[0] if vs else "ungraded"
    return out


def _update_submission(conn, sid, score, full, detail):
    try:
        conn.execute("UPDATE submissions SET score=?, full=?, detail_json=? WHERE sid=?",
                     (score, full, json.dumps(detail, ensure_ascii=False), sid))
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- 可核对性（t24）
#: 判定 → 颜色（RGB）
VERDICT_COLOR = {
    "correct": (0, 150, 0),
    "wrong": (205, 30, 30),
    "unsure": (235, 140, 0),
    "pending": (0, 95, 200),
    "ungraded": (120, 120, 120),
}

_ANNOT_FONTS = {}


def _annot_font(size: int, bold: bool = False):
    """标注用中文字体（与答题卡同款，取不到就退回默认）。"""
    key = (size, bold)
    if key in _ANNOT_FONTS:
        return _ANNOT_FONTS[key]
    from PIL import ImageFont
    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    font = None
    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            font = ImageFont.truetype(p, size, index=0)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _ANNOT_FONTS[key] = font
    return font


def _dashed_rect(draw, box, color, width=3, dash=12):
    """PIL 没有虚线矩形，手工按段画。box = (x1, y1, x2, y2)。"""
    x1, y1, x2, y2 = [int(v) for v in box]
    for (xa, ya, xb, yb) in ((x1, y1, x2, y1), (x2, y1, x2, y2),
                             (x2, y2, x1, y2), (x1, y2, x1, y1)):
        length = max(abs(xb - xa), abs(yb - ya))
        if length == 0:
            continue
        step = 0
        while step < length:
            seg = min(dash, length - step)
            if (step // dash) % 2 == 0:
                if xa == xb:
                    draw.line([(xa, ya + step * (1 if yb > ya else -1)),
                               (xb, ya + (step + seg) * (1 if yb > ya else -1))],
                              fill=color, width=width)
                else:
                    draw.line([(xa + step * (1 if xb > xa else -1), ya),
                               (xa + (step + seg) * (1 if xb > xa else -1), yb)],
                              fill=color, width=width)
            step += seg


def _tint(draw, box, color, alpha=0.30):
    """半透明着色（把颜色与白混合，模拟 alpha）。"""
    x1, y1, x2, y2 = [int(v) for v in box]
    blended = tuple(int(255 - (255 - c) * alpha) for c in color)
    draw.rectangle([x1, y1, x2, y2], fill=blended)


def _open_submission(conn, sid):
    try:
        return conn.execute("SELECT * FROM submissions WHERE sid=?", (str(sid),)).fetchone()
    except Exception:
        return None


def _card_pages(manifest):
    card_info = (manifest or {}).get("card") or {}
    return card_info.get("pages") or []


def _page_for(pages, page_no):
    for p in pages:
        if p.get("page") == page_no:
            return p
    return pages[0] if pages else None


def _restore_canonical(image_path):
    """原图 → 规范图（2480×3508, gray np）；失败返回 (None, 错误串)。"""
    full_img, _meta, code, why = _read_photo(image_path)   # t27：EXIF 转正在这里同样生效
    if full_img is None:
        return None, why or code or ("读图失败: %s" % image_path)
    work, scale = _work_image(full_img)
    centers, _mirrored = detect_markers_with_mirror(work)
    if len(centers) < 4:
        return None, "只检出 %d/4 个 ArUco。%s" % (len(centers), WARP_HINT)
    if scale and scale != 1.0:
        centers = {k: (v[0] / scale, v[1] / scale) for k, v in centers.items()}
    M = homography_from_markers(centers)
    if M is None:
        return None, "四角单应矩阵求解失败。%s" % WARP_HINT
    norm = cv2.warpPerspective(full_img, M, (PAGE_W, PAGE_H),
                               flags=cv2.INTER_LINEAR, borderValue=255)
    return norm, ""


def annotate(conn, sid: str, out_path: str = None):
    """把判卷过程画回规范图（2480×3508）→ PNG 路径；出错返回 None 并写 annotate.last_error。"""
    annotate.last_error = ""
    try:
        return _annotate(conn, sid, out_path)
    except Exception as e:      # 标注失败绝不能拖死结果页
        annotate.last_error = "internal_error: %s: %s" % (type(e).__name__, e)
        return None


annotate.last_error = ""


def _annotate(conn, sid, out_path):
    from PIL import Image, ImageDraw

    row = _open_submission(conn, sid)
    if row is None:
        annotate.last_error = "submission_not_found: %s" % sid
        return None
    if not int(row["warp_ok"] or 0):
        annotate.last_error = ("warp_failed: %s 四角标定失败，没有规范图可标注" % sid)
        return None
    image_path = row["image_path"]
    if not image_path or not os.path.exists(image_path):
        annotate.last_error = "image_missing: %s" % image_path
        return None

    norm, err = _restore_canonical(image_path)
    if norm is None:
        annotate.last_error = "warp_failed: %s" % err
        return None

    detail = _json_loads(row["detail_json"]) or {}
    units = detail.get("items") or []
    if not out_path:
        out_path = os.path.join(OUT_ROOT, str(row["pid"]), "sub_%s" % sid, "annotated.png")
    manifest = _load_manifest(conn, row["pid"]) or {}
    pages = _card_pages(manifest)
    page = _page_for(pages, detail.get("page"))
    page_items = {it.get("idx"): it for it in (page or {}).get("items", [])}
    qr = (page or {}).get("qr") or {"x": card.QR_POS[0], "y": card.QR_POS[1],
                                    "size": card.QR_SIZE}

    img = Image.fromarray(cv2.cvtColor(norm, cv2.COLOR_GRAY2RGB), "RGB")
    draw = ImageDraw.Draw(img)

    # 1) 顶部信息条（高度 128，避开 y=142 起的 ArUco）
    bar_h = 128
    draw.rectangle([0, 0, PAGE_W - 1, bar_h], fill=(255, 255, 255), outline=(0, 0, 0), width=3)
    def _num(v):
        try:
            f = float(v)
            return int(f) if abs(f - round(f)) < 1e-9 else round(f, 2)
        except (TypeError, ValueError):
            return 0
    score = _num(row["score"])
    full = _num(row["full"])
    empty_batch = bool(detail.get("empty_batch"))
    backend = detail.get("backend") or "manual"
    line1 = "%s · %s" % (row["pid"], sid)
    line2 = "分数 %s/%s　　后端 %s　　空批次：%s" % (score, full, backend,
                                              "是（识别服务无响应）" if empty_batch else "否")
    line3 = "判卷可核对图　ArUco ✓　QR %s　页 %s" % (
        "✓" if int(row["qr_ok"] or 0) else "✗", detail.get("page") or 1)
    draw.text((24, 10), line1, font=_annot_font(38, True), fill=(0, 0, 0))
    draw.text((24, 54), line2, font=_annot_font(30), fill=(20, 20, 20))
    draw.text((24, 90), line3, font=_annot_font(24), fill=(90, 90, 90))

    # 2) 四角 ArUco：圆圈 + 编号
    for mid, (mx, my) in sorted(card.ARUCO_POS.items()):
        cx = mx + card.ARUCO_SIZE / 2.0
        cy = my + card.ARUCO_SIZE / 2.0
        r = card.ARUCO_SIZE / 2.0 + 14
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 150, 0), width=5)
        draw.rectangle([mx, my, mx + card.ARUCO_SIZE, my + card.ARUCO_SIZE],
                       outline=(0, 150, 0), width=3)
        draw.text((mx - 6, my - 42), "A%d" % mid, font=_annot_font(34, True), fill=(0, 120, 0))

    # 3) 卷号 QR 框
    draw.rectangle([qr["x"] - 6, qr["y"] - 6, qr["x"] + qr["size"] + 6, qr["y"] + qr["size"] + 6],
                   outline=(0, 90, 200), width=4)
    draw.text((qr["x"], qr["y"] + qr["size"] + 14),
              "QR pid=%s p=%s" % (row["pid"], (detail.get("qr") or {}).get("p", "-")),
              font=_annot_font(26), fill=(0, 90, 200))

    # 4) 逐题标注
    for u in units:
        verdict = u.get("verdict") or "ungraded"
        color = VERDICT_COLOR.get(verdict, (120, 120, 120))
        idx = u.get("idx")
        citem = page_items.get(idx) or {}

        if u.get("kind") == KIND_OMR:
            picked = u.get("picked")
            ratios = u.get("ratios") or {}
            cells = citem.get("cells") or []
            for c in cells:
                opt = str(c.get("opt"))
                box = (c["x"], c["y"], c["x"] + c["w"], c["y"] + c["h"])
                ratio = float(ratios.get(opt, 0.0))
                label = "%s %.0f%%" % (opt, ratio * 100)
                # 写在答题卡自带 A/B/C/D 字母的上方（-64），避免压住印刷字
                ly = c["y"] - 64
                if verdict == "unsure":
                    _dashed_rect(draw, box, VERDICT_COLOR["unsure"], width=4)
                    draw.text((c["x"], ly), label, font=_annot_font(24),
                              fill=VERDICT_COLOR["unsure"])
                elif str(picked) == opt:
                    _tint(draw, box, color, 0.35)
                    draw.rectangle(box, outline=color, width=5)
                    draw.text((c["x"], ly), label, font=_annot_font(26, True), fill=color)
                else:
                    draw.rectangle(box, outline=(150, 150, 150), width=2)
                    draw.text((c["x"], ly), label, font=_annot_font(20),
                              fill=(140, 140, 140))
            if verdict == "unsure" and cells:
                why = u.get("unsure_reason") or u.get("reason") or "涂黑不唯一/太淡"
                draw.text((cells[-1]["x"] + cells[-1]["w"] + 24, cells[0]["y"] + 16),
                          "待确认：%s" % why, font=_annot_font(24),
                          fill=VERDICT_COLOR["unsure"])
            elif verdict in ("correct", "wrong") and cells:
                draw.text((cells[-1]["x"] + cells[-1]["w"] + 24, cells[0]["y"] + 16),
                          "%s（期望 %s）" % ("判对" if verdict == "correct" else "判错",
                                          u.get("expected") or "-"),
                          font=_annot_font(28, True), fill=color)

        else:
            box0 = u.get("box") or {}
            if box0:
                box = (box0["x"], box0["y"], box0["x"] + box0["w"], box0["y"] + box0["h"])
                _tint(draw, box, color, 0.18)
                if verdict == "unsure":
                    _dashed_rect(draw, box, color, width=4)
                else:
                    draw.rectangle(box, outline=color, width=4)
                got = (u.get("picked") or "").strip()
                txt = "第%s题 空%s：%s" % (idx, u.get("blank"), got if got else "（待识别）")
                if got and u.get("expected"):
                    txt += "　期望 %s" % u["expected"]
                draw.text((box[0], box[1] - 32), txt, font=_annot_font(24), fill=color)
            crop = u.get("crop_path")
            if crop and os.path.exists(crop):
                try:
                    with Image.open(crop) as ci:
                        thumb = ci.convert("RGB")
                        tw = 300
                        th = max(1, int(thumb.height * tw / max(1, thumb.width)))
                        thumb = thumb.resize((tw, th), Image.LANCZOS)
                        px = 2140 if idx is None else 2140
                        py = max(150, min(PAGE_H - th - 10, int(box0.get("y", 200)) if box0 else 200))
                        img.paste(thumb, (px, py))
                        draw.rectangle([px - 2, py - 2, px + tw + 2, py + th + 2],
                                       outline=color, width=2)
                except Exception:
                    pass

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def explain_item(conn, sid: str, idx) -> dict:
    """单题完整证据（结果页“为什么”抽屉）：裁切图、识别文本、期望、reason、墨迹原始值。"""
    try:
        return _explain_item(conn, sid, idx)
    except Exception as e:
        return {"error": "internal_error", "detail": "%s: %s" % (type(e).__name__, e),
                "sid": sid, "idx": idx}


def _explain_item(conn, sid, idx):
    row = _open_submission(conn, sid)
    if row is None:
        return {"error": "submission_not_found", "sid": sid, "idx": idx}
    detail = _json_loads(row["detail_json"]) or {}
    units = [u for u in (detail.get("items") or []) if str(u.get("idx")) == str(idx)]
    if not units:
        return {"error": "item_not_found", "sid": sid, "idx": idx}

    manifest = _load_manifest(conn, row["pid"]) or {}
    page = _page_for(_card_pages(manifest), detail.get("page"))
    citem = next((it for it in (page or {}).get("items", [])
                  if str(it.get("idx")) == str(idx)), {})
    first = units[0]

    # 单题结论：有错即错 > unsure > pending > 全对
    verdicts = [u.get("verdict") for u in units]
    if "wrong" in verdicts:
        verdict = "wrong"
    elif "unsure" in verdicts:
        verdict = "unsure"
    elif "pending" in verdicts:
        verdict = "pending"
    elif verdicts and all(v == "correct" for v in verdicts):
        verdict = "correct"
    else:
        verdict = verdicts[0] if verdicts else "ungraded"

    blanks = [{
        "blank": u.get("blank"),
        "verdict": u.get("verdict"),
        "picked": u.get("picked") or "",
        "expected": u.get("expected") or "",
        "reason": u.get("reason") or "",
        "unsure_reason": u.get("unsure_reason") or "",
        "confidence": u.get("confidence"),
        "crop_path": u.get("crop_path"),
        "box": u.get("box"),
        "mastery_verdict": u.get("mastery_verdict"),
    } for u in units]

    info = {
        "sid": sid, "pid": row["pid"], "idx": first.get("idx"), "no": first.get("no"),
        "qtype": first.get("qtype"), "kind": first.get("kind"),
        "page": detail.get("page"),
        "verdict": verdict,
        "picked": "、".join(b["picked"] for b in blanks if b["picked"]) or "",
        "expected": "、".join(b["expected"] for b in blanks if b["expected"]) or "",
        "reason": "；".join(dict.fromkeys(b["reason"] for b in blanks if b["reason"])),
        "unsure_reason": "；".join(dict.fromkeys(
            b["unsure_reason"] for b in blanks if b["unsure_reason"])),
        "confidence": blanks[0]["confidence"],
        "crop_path": next((b["crop_path"] for b in blanks if b["crop_path"]), None),
        "crop_paths": [b["crop_path"] for b in blanks if b["crop_path"]],
        "blanks": blanks,
        "backend": detail.get("backend"),
        "empty_batch": bool(detail.get("empty_batch")),
        "score": row["score"], "full": row["full"],
    }
    info["normalize_reason"] = info["reason"]      # 兼容叫法

    if first.get("kind") == KIND_OMR:
        info["ink_ratios"] = {k: float(v) for k, v in (first.get("ratios") or {}).items()}
        info["cells"] = citem.get("cells") or []
    else:
        info["boxes"] = citem.get("boxes") or citem.get("answer_boxes") or []

    ann = os.path.join(OUT_ROOT, str(row["pid"]), "sub_%s" % sid, "annotated.png")
    info["annotated_path"] = ann if os.path.exists(ann) else None
    return info


# ---------------------------------------------------------------- 自检 CLI
def _main(argv=None):
    """python grade.py <db> <pid> <image> [--fill]"""
    import sqlite3

    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3:
        print("用法: python grade.py <db_path> <pid> <image_path> [--fill]")
        return 2
    conn = sqlite3.connect(argv[0])
    conn.row_factory = sqlite3.Row
    res = grade_image(conn, argv[1], argv[2])
    if "--fill" in argv and res.get("sid"):
        res = {"grade": res, "fill": fill_pending(conn, res["sid"], _default_recognizer())}
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
