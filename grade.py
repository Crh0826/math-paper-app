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

LEGACY_NEED_PID_MSG = (
    "这是旧版答题卡（4×4 标识）。旧卡不能只靠题号判：请在上传页填上这份卷子的卷号"
    "（卷子在本机库里就能判）；没有卷号请重新打印新版答题卡。")

KIND_OMR = card.KIND_OMR
KIND_FILL = card.KIND_FILL
KIND_SOLUTION = card.KIND_SOLUTION

VERDICTS = ("correct", "wrong", "unsure", "pending", "ungraded")


# ---------------------------------------------------------------- 小工具
def _err(code: str, detail: str = "", pid: str = None, diag: dict = None) -> dict:
    out = {"sid": None, "pid": pid, "warp_ok": False, "qr_ok": False,
           "score": 0, "full": 0, "items": [], "error": code, "detail": str(detail)}
    if diag is not None:
        out["diag"] = diag
    return out


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
        if not unmatched:
            # 认出来的都对，只是个数不够 → 老口径：只识别到部分答案
            return "unsure", "只识别到部分答案（缺 %d 个值）" % (len(exp_vals) - matched)
        if matched > 0:
            # 学生写了多个值、只对上其中一部分：更可能是 OCR 漏了（例如 π 这种符号），
            # 不能直接判错 —— 交人工确认。
            return "unsure", "只识别到 %d/%d 个值（可能漏识别），需人工确认" % (
                matched, len(exp_vals))
        return "wrong", "多值答案有值对不上：%d/%d 个值能对应" % (matched, len(exp_vals))
    if len(got_vals) == len(exp_vals):
        if unmatched:
            return "wrong", "多值答案有值对不上：%d/%d 个值能对应" % (matched, len(exp_vals))
        return "correct", "多值全部一致（%d 个值）" % len(exp_vals)
    # 值比期望多：可能多写/串行，交人工
    return "unsure", "识别到多余的值（%d 个 vs 期望 %d 个），需人工确认" % (
        len(got_vals), len(exp_vals))


def _sign_flip_variant_match(text: str, expected: str, qtype: str) -> bool:
    """把某一个数的负号翻一下是否就等价了（= 很可能是 OCR 丢了负号）。

    实测（Qwen2.5-VL 本地、GLM-OCR 都出现过）：手写的 `-6` 被读成 `6`、`-π` 读成 `π`。
    这种"只差一个负号"的情况不该判错，交人工确认。
    """
    import re as _re

    s = str(text or "")
    # 逐个数字/符号 token 尝试翻负号
    for m in _re.finditer(r"-?\s*(?:\d+(?:\.\d+)?|[πp])", s):
        tok = m.group(0)
        body = tok.lstrip("- ").strip()
        flipped = body if tok.strip().startswith("-") else "-" + body
        cand = s[:m.start()] + flipped + s[m.end():]
        try:
            if normalize.is_equivalent(cand, expected, qtype or "填空")[0]:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


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
    if _sign_flip_variant_match(text, expected, qtype):
        return "unsure", "只差一个负号（可能是符号没识别出来），需人工确认"
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
            m, sd = _cell_stats(gray, x0 + round(c * cell), y0 + round(r * cell),
                                max(8, int(round(cell))))
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


def _align_decode(gray, x: int, y: int, grid: int, cell: float, span: int = 6):
    """在 ±span 内微调（由小到大逐圈，先命中即返回）→ (dx, dy, no) 或 None。"""
    for rad in (1, 2, 3, 5, 8, max(8, span)):
        for dy in range(-rad, rad + 1):
            for dx in range(-rad, rad + 1):
                kind, bits = _classify_block(gray, x + dx, y + dy, grid, cell)
                if kind != "bits" or not bits:
                    continue
                no = (card.decode_id_bits(bits) if grid == card.ID_GRID
                      else card.legacy_idx_bits(bits))
                if no is not None:
                    return dx, dy, no
    return None


def _frame_corner_dark(gray, x: int, y: int, size: int) -> bool:
    """标识块左上那一圈黑框（上边+左边各 2px）在不在 —— 比四边都亮/暗更稳。"""
    h, w = gray.shape[:2]
    if x - 3 < 0 or y - 3 < 0 or x + size > w or y + size > h:
        return False
    top = gray[y - 3:y - 1, x:x + size]
    left = gray[y:y + size, x - 3:x - 1]
    return bool(top.size and left.size and float(top.mean()) < 150 and float(left.mean()) < 150)


def _candidate_rects(gray):
    """标识块候选 (x, y, size)：轮廓法 + 列扫黑框兜底（粘连时轮廓不是方的）。"""
    h = gray.shape[0]
    rects = []
    try:
        bin_ = (gray < 100).astype(np.uint8) * 255
        cnts, _ = cv2.findContours(bin_, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, cw, ch = cv2.boundingRect(c)
            if 88 <= cw <= 140 and 88 <= ch <= 140 and abs(cw - ch) <= 14:
                yy = int(y) + 3
                if card.BODY_RECT[1] - 60 <= yy <= card.BODY_RECT[1] + card.BODY_RECT[3]:
                    rects.append((int(x) + 3, yy, int(max(cw, ch)) - 6))
    except Exception:
        pass
    y_from = max(0, card.BODY_RECT[1] - 50)
    y_to = min(h, card.BODY_RECT[1] + card.BODY_RECT[3])
    for base_x in card.ID_ORIGIN_X:
        for size in (card.ID_SIZE, card.ID_SIZE * card.LEGACY_ID_GRID // card.ID_GRID):
            y = y_from
            while y + size <= y_to:
                if _frame_corner_dark(gray, base_x, y, size):
                    rects.append((base_x, y, size))
                    y += size + 4
                    continue
                y += 8
    out = []
    for r in sorted(rects, key=lambda t: (t[1], t[0])):
        if any(abs(r[0] - o[0]) < 50 and abs(r[1] - o[1]) < 50 for o in out):
            continue
        out.append(r)
    return out


def _sweep_column(*_a, **_kw):      # 兼容旧名（不再使用）
    return None


def scan_id_blocks(gray) -> dict:
    """扫页面上的标识块：候选（轮廓/黑框）→ 按实测块宽自适应 5×5 或 4×4 → ±6px 对齐解码。

    * 块宽 ~120 → 5×5（编码题号）；~96 → 4×4（旧卡，编码卷内题序，只兼容）；
    * 候选实测尺寸决定 cell，不写死 24px（打印缩放/手机透视图都能扛）；
    * 解不出 CRC → unreadable（该题进 unsure），绝不静默判错。
    """
    out = {"blocks": [], "unreadable": [], "legacy": 0}
    for (x, y, size) in _candidate_rects(gray):
        grid = card.ID_GRID if size >= 108 else card.LEGACY_ID_GRID
        cell = (float(size) - 2.0) / grid
        got = _align_decode(gray, x, y, grid, cell, span=8)
        col = 0 if x < card.PAGE_W // 2 else 1
        if not got:
            out["unreadable"].append({"x": x, "y": y, "col": col})
            continue
        dx, dy, no = got
        if grid == card.LEGACY_ID_GRID:
            out["legacy"] += 1
            out["blocks"].append({"x": x + dx, "y": y + dy, "no": None, "idx": no,
                                  "col": col, "grid": grid})
        else:
            out["blocks"].append({"x": x + dx, "y": y + dy, "no": no, "col": col, "grid": grid})

    # 补一遍 5×5 列扫：边框与答题框粘连时轮廓/黑框都可能漏
    y_from = max(0, card.BODY_RECT[1] - 40)
    y_to = min(gray.shape[0], card.BODY_RECT[1] + card.BODY_RECT[3])
    for col, base_x in enumerate(card.ID_ORIGIN_X):
        y = y_from
        while y + card.ID_SIZE <= y_to:
            if any(abs(base_x - b["x"]) < 60 and abs(y - b["y"]) < 130 for b in out["blocks"]):
                y += 8
                continue
            kind, _bits = _classify_block(gray, base_x, y, card.ID_GRID, card.ID_CELL)
            if kind == "bits":          # 先便宜地筛一遍，只有像网格才做对齐搜索
                got = _align_decode(gray, base_x, y, card.ID_GRID, card.ID_CELL, span=4)
                if got:
                    dx, dy, no = got
                    out["blocks"].append({"x": base_x + dx, "y": y + dy, "no": no,
                                          "col": col, "grid": card.ID_GRID})
                    y += card.ID_SIZE + 4
                    continue
            y += 8

    def _dedupe(items, gap=110):
        keep = []
        for it in sorted(items, key=lambda b: (b["x"], b["y"])):
            if any(abs(it["x"] - o["x"]) < 70 and abs(it["y"] - o["y"]) < gap for o in keep):
                continue
            keep.append(it)
        return keep

    out["blocks"] = _dedupe(out["blocks"])
    out["unreadable"] = _dedupe([u for u in out["unreadable"]
                                 if not any(abs(u["x"] - b["x"]) < 70 and
                                            abs(u["y"] - b["y"]) < 130 for b in out["blocks"])])
    out["blocks"].sort(key=lambda b: (b["y"], b["x"]))
    out["unreadable"].sort(key=lambda b: (b["y"], b["x"]))
    return out


def scan_ids(image_path):
    """拍照 → 标定 → 扫标识块（给 UI/测试用的轻量入口）。"""
    norm, photo_meta, code, why, diag = _warp_photo(image_path)
    if norm is None:
        return {"error": code or "warp_failed", "detail": why, "blocks": [],
                "unreadable": [], "legacy": 0, "diag": diag}
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


def _id_unreadable_unit(seq: int, norm, box: dict, crop_dir: str, reason: str) -> dict:
    """「这道题的标识不可读」→ unsure 单元（进人工确认，绝不猜答案）。

    t11：6 位题号放宽后，被涂黑/采样跑偏的标识格也可能解出一个「像题号」的号，
    所以除了 CRC 读不出，**解出的号在题库里查不到**也归到这一类（reason 里带原题号）。
    """
    return {
        "idx": seq, "no": None, "qtype": "", "kind": "id", "blank": 1,
        "picked": None, "expected": "", "verdict": "unsure",
        "confidence": 0.0, "reason": reason, "unsure_reason": "题目标识不可读",
        "ratios": {},
        "crop_path": _save_crop(norm, {"x": box["x"], "y": box["y"],
                                       "w": card.ID_SIZE, "h": card.ID_SIZE},
                                crop_dir, "id_%d" % seq),
    }


def _grade_by_ids(conn, image_path, out_root):
    norm, photo_meta, code, why, diag = _warp_photo(image_path)
    if norm is None:
        return _err(code or "warp_failed", why, None, diag=diag)
    qr_payload = card.read_qr(norm, {"x": card.QR_POS[0], "y": card.QR_POS[1],
                                     "size": card.QR_SIZE})
    qr_ok = qr_payload is not None
    pid_hint = (qr_payload or {}).get("pid")

    legacy_hits = _quick_legacy_probe(norm)
    if legacy_hits >= 2:      # 旧卡：没有卷号就判不了（旧卡标识只编码卷内题序）
        return _err("legacy_card_need_pid", LEGACY_NEED_PID_MSG, pid_hint)
    scan = scan_id_blocks(norm)
    if not scan["blocks"]:
        if legacy_hits or scan["legacy"]:
            return _err("legacy_card_need_pid", LEGACY_NEED_PID_MSG, pid_hint)
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
            unreadable_units.append(
                _id_unreadable_unit(seq, norm, b, crop_dir, "题目标识不可读"))
            continue
        row = _question_row(conn, b["no"])
        if row is None:
            unknown.append(b["no"])
            unreadable_units.append(_id_unreadable_unit(
                seq, norm, b, crop_dir,
                "题目标识不可读：解出的题号 %s 在题库里查不到" % b["no"]))
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

    if not items and scan["blocks"] and all(b.get("no") is None for b in scan["blocks"]):
        return _err("legacy_card_need_pid", LEGACY_NEED_PID_MSG, pid_hint)
    if not items and unknown:
        return _err("unknown_questions",
                    "扫到 %d 个题目标识，但本机题库里没有这些题号（%s…）。"
                    "可能是别的题库/别的版本，或题目被删了。" % (
                        len(unknown), "、".join(str(n) for n in unknown[:5] if n)), pid_hint)
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


# ---------------------------------------------------------------- 拍照诊断（t50）
CORNER_NAMES = ("TL", "TR", "BR", "BL")
TILT_LIMIT_DEG = 15.0
MIN_PAPER_RATIO = 0.25
GLARE_MEAN = 240.0

SUGGEST_TEXT = {
    "missing_corner": "有角没拍到：让答题卡四角的黑块都落在取景框内，别用手指挡住",
    "tilt": "拍歪了：正对答题卡，让四个黑块连成的框尽量是矩形",
    "too_small": "太远了：靠近一点，让答题卡差不多填满取景框",
    "glare": "角上反光：换个角度或避开顶光，别让黑块发白",
}


def _quad_angles(pts):
    """四点（TL,TR,BR,BL 顺序）→ 四个内角（度）。"""
    out = []
    n = len(pts)
    for i in range(n):
        a = np.array(pts[i - 1], dtype=float)
        b = np.array(pts[i], dtype=float)
        c = np.array(pts[(i + 1) % n], dtype=float)
        v1, v2 = a - b, c - b
        denom = float(np.linalg.norm(v1) * np.linalg.norm(v2)) + 1e-9
        cos = max(-1.0, min(1.0, float(np.dot(v1, v2)) / denom))
        out.append(float(np.degrees(np.arccos(cos))))
    return out


def _poly_area(pts) -> float:
    x = [p[0] for p in pts]
    y = [p[1] for p in pts]
    return abs(sum(x[i] * y[(i + 1) % len(pts)] - x[(i + 1) % len(pts)] * y[i]
                   for i in range(len(pts)))) / 2.0


def _diag_from_centers(full_img, centers: dict) -> dict:
    """四角中心（原图坐标）→ 结构化诊断。"""
    h, w = full_img.shape[:2]
    corners, glare = {}, {}
    for name, mid in zip(CORNER_NAMES, (0, 1, 2, 3)):
        pt = centers.get(mid)
        if pt:
            corners[name] = [round(float(pt[0]), 1), round(float(pt[1]), 1)]
            x, y = int(pt[0]), int(pt[1])
            patch = full_img[max(0, y - 40):y + 40, max(0, x - 40):x + 40]
            glare[name] = bool(patch.size and float(patch.mean()) > GLARE_MEAN)
        else:
            # 没检到的角不判反光（空白纸面本来就亮，不能当反光），但键要在
            corners[name] = None
            glare[name] = False
    found = [n for n in CORNER_NAMES if corners[n]]
    missing = [n for n in CORNER_NAMES if not corners[n]]
    max_dev, ratio, rotation = None, None, None
    if len(found) == 4:
        pts = [corners[n] for n in CORNER_NAMES]
        max_dev = max(abs(a - 90.0) for a in _quad_angles(pts))
        ratio = _poly_area(pts) / float(w * h)
        # 页面在画面里整体转了多少度（上边相对水平线，折算到 -45..45）
        import math as _math
        ang = _math.degrees(_math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]))
        ang = (ang + 90.0) % 180.0 - 90.0
        rotation = abs(ang) if abs(ang) <= 45 else 180.0 - abs(ang)
    if missing:
        suggest = "missing_corner"
    elif max((max_dev or 0.0), (rotation or 0.0)) > TILT_LIMIT_DEG:
        suggest = "tilt"      # 透视歪（内角偏）或整体转歪都算
    elif ratio is not None and ratio < MIN_PAPER_RATIO:
        suggest = "too_small"
    elif any(glare.values()):
        suggest = "glare"
    else:
        suggest = None
    return {
        "found": len(found), "expected": 4, "missing": missing, "corners": corners,
        "angles_deg": round(max_dev, 1) if max_dev is not None else None,
        "rotation_deg": round(rotation, 1) if rotation is not None else None,
        "paper_ratio": round(ratio, 3) if ratio is not None else None,
        "glare": glare, "suggest": suggest,
        "suggest_text": SUGGEST_TEXT.get(suggest, ""),
    }


def diagnose(image_path):
    """只做标定诊断（不判卷）→ {"warp_ok", "diag", "error"}。永不抛异常。"""
    try:
        full_img, meta, code, why = _read_photo(image_path)
        if full_img is None:
            return {"warp_ok": False, "diag": None,
                    "error": code or "image_unreadable", "detail": why}
        work, scale = _work_image(full_img)
        centers, mirrored = detect_markers_with_mirror(work)
        if centers and scale and scale != 1.0:
            centers = {k: (v[0] / scale, v[1] / scale) for k, v in centers.items()}
        diag = _diag_from_centers(full_img, centers)
        diag["mirrored"] = mirrored
        diag["photo"] = meta
        return {"warp_ok": len(centers) >= 4, "diag": diag, "error": None}
    except Exception as e:
        return {"warp_ok": False, "diag": None, "error": "internal_error",
                "detail": "%s: %s" % (type(e).__name__, e)}


def _diag_font(size: int, bold: bool = False):
    for p in (r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            try:
                from PIL import ImageFont
                return ImageFont.truetype(p, size, index=0)
            except Exception:
                continue
    from PIL import ImageFont
    return ImageFont.load_default()


def diag_image(image_path, diag: dict, out_path: str):
    """把诊断画成图：绿圈=检出的角，红箭头=没拍到的角 + 顶部一句话原因。"""
    from PIL import Image, ImageDraw
    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return None
    W, H = img.size
    d = ImageDraw.Draw(img)
    small, big = _diag_font(max(22, int(min(W, H) * 0.022))), _diag_font(
        max(26, int(min(W, H) * 0.03)), bold=True)
    corners = (diag or {}).get("corners") or {}
    r = max(18, int(min(W, H) * 0.018))
    targets = {"TL": (int(W * 0.12), int(H * 0.10)), "TR": (int(W * 0.88), int(H * 0.10)),
               "BR": (int(W * 0.88), int(H * 0.90)), "BL": (int(W * 0.12), int(H * 0.90))}
    for name in CORNER_NAMES:
        pt = corners.get(name)
        if pt:
            x, y = float(pt[0]), float(pt[1])
            d.ellipse([x - r, y - r, x + r, y + r], outline=(0, 170, 60), width=max(4, r // 5))
            d.text((x + r + 6, y - r), name, font=small, fill=(0, 140, 40))
        else:
            tx, ty = targets[name]
            cx, cy = int(W * 0.5), int(H * 0.5)
            d.line([(cx, cy), (tx, ty)], fill=(215, 30, 30), width=max(5, r // 4))
            d.ellipse([tx - r, ty - r, tx + r, ty + r], outline=(215, 30, 30),
                      width=max(4, r // 5))
            d.text((tx - r, ty + r + 4), "让 %s 入镜" % name, font=small, fill=(180, 20, 20))
    text = (diag or {}).get("suggest_text") or "四角没有全部检出，请重拍"
    band = max(46, int(H * 0.05))
    d.rectangle([0, 0, W, band], fill=(255, 238, 238))
    d.text((int(W * 0.012), int(band * 0.2)), text, font=big, fill=(160, 0, 0))
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        img.save(out_path)
        return out_path
    except Exception:
        return None


def _warp_photo(image_path):
    """读图 → ArUco 标定 → 复原到 2480×3508。→ (norm|None, photo_meta, err_code, err_msg)"""
    full_img, photo_meta, code, why = _read_photo(image_path)
    if full_img is None:
        return None, photo_meta, code or "warp_failed", (
            why or ("读图失败: %s" % image_path)), None
    work, scale = _work_image(full_img)
    centers, mirrored = detect_markers_with_mirror(work)
    if mirrored:
        photo_meta["mirror_fixed"] = True
    if scale and scale != 1.0:
        centers = {k: (v[0] / scale, v[1] / scale) for k, v in centers.items()}
    diag = _diag_from_centers(full_img, centers)
    diag["mirrored"] = mirrored
    if len(centers) < 4:
        txt = "%s（只检出 %d/4 个）" % (diag.get("suggest_text") or WARP_HINT, len(centers))
        return None, photo_meta, "warp_failed", txt, diag
    M = homography_from_markers(centers)
    if M is None:
        return None, photo_meta, "warp_failed", "四角单应矩阵求解失败。%s" % WARP_HINT, diag
    try:
        norm = cv2.warpPerspective(full_img, M, (PAGE_W, PAGE_H),
                                   flags=cv2.INTER_LINEAR, borderValue=255)
    except Exception as e:
        return None, photo_meta, "warp_failed", "透视复原失败（%s）。%s" % (e, WARP_HINT), diag
    return norm, photo_meta, None, "", diag


def _read_id_from_photo(gray, box: dict, grid: int):
    """按 manifest 里那一题的 id_box 坐标，到照片上采样并解码 → idx / no / None。"""
    try:
        x, y = int(box.get("x", 0)), int(box.get("y", 0))
        cell = int(box.get("cell") or card.ID_CELL)
    except Exception:
        return None
    kind, bits = _classify_block(gray, x, y, grid, cell)
    if kind != "bits" or not bits:
        return None
    if grid == card.LEGACY_ID_GRID:
        return card.legacy_idx_bits(bits)      # 旧卡：卷内题序
    return card.decode_id_bits(bits)           # 新卡：题号


def _same_no(a, b) -> bool:
    """两个题号是不是同一道题（t11：5 位老题号与 6 位新题号都要认）。

    先按字符串比（库里就是 "03370" 这种补零写法），再退化成整数比 ——
    "03370" vs "3370"、"100001" vs "100001" 都算同一道题。
    """
    sa, sb = str(a).strip(), str(b).strip()
    if sa == sb:
        return True
    try:
        return int(sa) == int(sb)
    except (TypeError, ValueError):
        return False


def _check_manifest_ids(gray, page):
    """抽查前几题的标识，确认拍的就是这份卷子。

    → None(通过) | "legacy"(未知版本) | "mismatch"(不是这张卷)
    * manifest 是自描述的：判据是 **id_box.bits 的长度**，不是全局常量；
    * 16 位 = 旧卡（4×4，编码卷内题序 idx）；25 位 = 新卡（5×5，编码题号 no）；
    * **题号位数不是判据**：5 位（00001）与 6 位（100001）都走同一条 25 位链路；
    * 读不出的跳过，不阻断判分（后面 OMR 自然会给 unsure）。
    """
    checked = 0
    for it in (page.get("items") or [])[:4]:
        box = it.get("id_box") or {}
        bits = box.get("bits") or []
        n = len(bits)
        if n == 16:
            got = _read_id_from_photo(gray, box, card.LEGACY_ID_GRID)
            expect = it.get("idx")
            same = (got is not None and expect is not None and int(got) == int(expect))
        elif n == 25:
            got = _read_id_from_photo(gray, box, card.ID_GRID)
            expect = it.get("no")
            same = (got is not None and expect is not None and _same_no(got, expect))
        else:
            return "legacy"
        if got is None:
            continue
        checked += 1
        if not same:
            return "mismatch"
    if checked == 0:
        # 一个都没读出来：看看是不是「库里是新卡、手上拍的是旧卡」
        for it in (page.get("items") or [])[:4]:
            box = it.get("id_box") or {}
            if _read_id_from_photo(gray, box, card.LEGACY_ID_GRID) is not None:
                return "legacy"
    return None


def _grade_image(conn, pid, image_path, ocr, out_root):
    manifest = _load_manifest(conn, pid)
    if not manifest:
        return _err("paper_not_found", "papers 表里没有 %s" % pid, pid)
    card_info = manifest.get("card") or {}
    pages = card_info.get("pages") or []
    if not pages:
        return _err("no_card", "该卷没有 card 几何（还没生成答题卡？）", pid)

    norm, photo_meta, code, why, diag = _warp_photo(image_path)
    if norm is None:
        return _err(code or "warp_failed", why, pid, diag=diag)

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

    # ---- 版本/串卷校验：看 manifest 每题 id_box.bits 的长度（自描述，不靠全局常量）
    id_check = _check_manifest_ids(norm, page)
    if id_check == "legacy":
        return _err("legacy_card",
                    "这张卡的题目标识是未知版本（既不是 4×4 也不是 5×5），"
                    "请用当前版本重新打印答题卡。", pid)
    if id_check == "mismatch":
        return _err("pid_mismatch",
                    "卷号是 %s，但题目标识对不上（拍的不是这份卷子？）" % pid, pid)

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
