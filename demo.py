# -*- coding: utf-8 -*-
"""一键演示 / 集成冒烟：组卷 → 出卷 → 答题卡 → 合订 → 模拟作答+拍照 → 判卷 → 训练卷 → 摘要。

一条命令跑完整条业务链（不依赖 Web 服务）：

    python app/demo.py [--out DIR] [--db PATH] [--fast] [--keep-db]

它同时是**交付 demo**（给用户看"这套东西真能跑"）和**集成冒烟**（任一环节接错就炸）。

设计约定
--------
* **只走现有模块的公开 API**：`picker.build` / `paper.render_paper` / `card.build_card` /
  `grade.grade_image` / `grade.fill_pending` / `train.plan` / `errors.explain`，不复制业务逻辑。
* **模拟作答是"真涂真写"**：把答案画到答题卡 PNG 上（选择题涂黑选项框、填空/解答在框里写
  手写体文字），再把整页做透视 + 光照 + JPEG 压缩成"手机照片"，交给 `grade.grade_image` 真的
  标定/读 QR/判 OMR —— 不是伪造判卷结果。
* **OCR 用 manual 后端**：填空/解答在演示里保持 `pending`（"待人工确认"），不联网、不等 30 秒；
  摘要里如实写"待确认 N 项"。
* **跑完清理库记录**：演示卷的 `papers` / `submissions` / `mastery` 行会删掉（mastery 按跑前
  快照还原），避免污染用户的"我的卷子"和掌握度；产物文件留在 `--out` 目录里供查看。
  想保留库记录看链路效果就加 `--keep-db`。
* **失败给可执行建议**：任何一步出错都用 `errors.explain` 打印「发生了什么 / 为什么 / 怎么办」，
  不甩栈（加 `--debug` 才打完整 traceback）。
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import sys
import time
import traceback

APP = os.path.dirname(os.path.abspath(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pymupdf  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import card  # noqa: E402
import db  # noqa: E402
import errors  # noqa: E402
import grade  # noqa: E402
import ocr  # noqa: E402
import paper  # noqa: E402
import picker  # noqa: E402
import render  # noqa: E402
import train  # noqa: E402

DEFAULT_OUT = os.path.join(APP, "out", "demo")
DEFAULT_DB = os.path.join(APP, "data", "app.db")
DEFAULT_SEED = 20261031
DEFAULT_MIX = {"选择": 10, "填空": 8, "解答": 6}
FAST_MIX = {"选择": 3, "填空": 2, "解答": 1}
N_DELIBERATE_WRONG = 2          # 故意答错的选择题数量（进训练卷用）
PHOTO_JPEG_QUALITY = 72
PHOTO_MARGIN = 140              # 照片四周留白（模拟手机拍到桌面）
PHOTO_JITTER = 55               # 四角抖动 ≈ 透视倾斜

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


class DemoError(RuntimeError):
    """演示失败：带 errors.py 的错误码，供打印"怎么办"。"""

    def __init__(self, code: str, detail: str = "", hints=()):
        self.code = str(code or "internal_error")
        self.detail = str(detail or "")
        self.hints = [h for h in (hints or []) if h]
        super().__init__("%s: %s" % (self.code, self.detail) if self.detail else self.code)


# ---------------------------------------------------------------- 小工具
def _stage(rows: list, label: str, fn, *args, **kwargs):
    """跑一个阶段并记账耗时（失败也记，方便看卡在哪）。"""
    t0 = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        rows.append((label, time.perf_counter() - t0))


def _human(seconds: float) -> str:
    return "%.1fs" % seconds if seconds >= 0.05 else "%.2fs" % seconds


def _load_font(size: int):
    for path in FONT_CANDIDATES:
        try:
            if os.path.isfile(path):
                return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                       # 老 Pillow
        return ImageFont.load_default()


def _fit_font(text: str, max_w: int, max_h: int):
    """把文字塞进作答框：字号按框高取，太宽就缩。"""
    size = max(18, int(max_h * 0.55))
    while size > 14:
        font = _load_font(size)
        try:
            left, top, right, bottom = font.getbbox(text)
            width, height = right - left, bottom - top
        except Exception:                   # noqa: BLE001 - 老字体对象没有 getbbox
            width, height = size * len(text) * 0.6, size
        if width <= max_w * 0.92 and height <= max_h * 0.9:
            return font
        size -= max(2, size // 12)
    return _load_font(18)


def _code_for_exception(exc: BaseException) -> str:
    """异常 → errors.py 的错误码（优先用模块自己给的 code）。"""
    if isinstance(exc, render.WordUnavailable):
        return "WordUnavailable"
    if isinstance(exc, picker.InsufficientQuestions):
        return "InsufficientQuestions"
    if isinstance(exc, picker.ScopeError):
        return "ScopeError"
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "db_locked" if ("lock" in text or "busy" in text) else "internal_error"
    code = getattr(exc, "code", "") or ""
    if code in errors.CODES:
        return code
    return "internal_error"


def _report(exc: BaseException, debug: bool = False) -> None:
    """按 errors.py 的口径打印「发生了什么 / 为什么 / 怎么办」。"""
    code = exc.code if isinstance(exc, DemoError) else _code_for_exception(exc)
    detail = exc.detail if isinstance(exc, DemoError) else str(exc)
    payload = {"message": detail, "code": code}
    # InsufficientQuestions 的逐题型差额要带给 errors.explain，用户才知道该改哪个数量
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details:
        payload["details"] = details
    info = errors.explain(code, payload)
    print("")
    print("✗ 演示没跑完：%s（错误码 %s）" % (info["title"], code))
    print("  发生了什么：%s" % info["what"])
    print("  为什么：%s" % info["why"])
    print("  怎么办：")
    for line in info["fix"].splitlines():
        print("    " + line)
    if isinstance(exc, DemoError):
        for hint in exc.hints:
            print("    · " + hint)
    if detail and detail not in info["what"]:
        print("  原始信息：%s" % detail[:400])
    if debug:
        print("")
        traceback.print_exception(type(exc), exc, exc.__traceback__)


# ---------------------------------------------------------------- 组卷参数
def build_params(mix: dict, seed: int = DEFAULT_SEED, title: str = "七年级数学 · 一键演示卷") -> dict:
    """CONTRACT §2 的组卷参数（scope 空 = 全库；只抽有答案、且图不缺的题）。"""
    return {
        "title": title,
        "mix": {k: int(v) for k, v in mix.items() if int(v) > 0},
        "scope": {"chapters": [], "kps": [], "kp_mode": "any"},
        "requirements": {"need_answer": True, "exclude_empty": True,
                         "exclude_no_family": False, "exclude_fig_missing": True},
        "seed": int(seed),
        "space": {"选择": 0, "填空": 0, "解答": 0},      # 卷面只印题，作答在答题卡（CONTRACT §2）
    }


# ---------------------------------------------------------------- 模拟作答
def plan_answers(manifest: dict, n_wrong: int = N_DELIBERATE_WRONG) -> tuple:
    """按标准答案造"学生作答"：选择全对，最后 n_wrong 道**选择题**故意涂错。

    返回 (作答计划, 故意答错的 idx 集合)。只对选择题故意错 —— 填空题故意写错也只会留在
    ``pending``（manual OCR 不识别），进不了错题集，演示效果反而更差。
    """
    items = list(manifest.get("items") or [])
    choice_items = [it for it in items if str(it.get("qtype")) == "选择"]
    wrong_idx = {it["idx"] for it in choice_items[-max(0, int(n_wrong)):]} if n_wrong else set()
    plan = {}
    for it in items:
        idx = int(it["idx"])
        keys = {int(k.get("blank", 1)): str(k.get("value") or "") for k in it.get("answer_key") or []}
        if str(it.get("qtype")) == "选择":
            expected = keys.get(1, "")
            options = [str(o) for o in (it.get("options") or ["A", "B", "C", "D"])]
            if idx in wrong_idx:
                others = [o for o in options if o != expected]
                picked = others[0] if others else (options[0] if options else "A")
            else:
                picked = expected or (options[0] if options else "A")
            plan[idx] = {"kind": card.KIND_OMR, "choice": picked, "expected": expected}
        else:
            texts = {}
            for blank, value in keys.items():
                texts[blank] = value
            plan[idx] = {"kind": "text", "texts": texts}
    return plan, wrong_idx


def compose_answer_sheet(manifest: dict, card_info: dict, card_pngs: list, plan: dict,
                         out_dir: str) -> list:
    """把作答画到答题卡上（选择题涂黑、填空/解答写字）→ 每页一张 PNG。"""
    out = []
    for page in card_info.get("pages") or []:
        page_no = int(page.get("page") or 1)
        src = card_pngs[page_no - 1]
        img = Image.open(src).convert("L")
        draw = ImageDraw.Draw(img)
        for it in page.get("items") or []:
            idx = int(it.get("idx"))
            answer = plan.get(idx) or {}
            if it.get("kind") == card.KIND_OMR:
                picked = str(answer.get("choice") or "")
                for cell in it.get("cells") or []:
                    if str(cell.get("opt")) != picked:
                        continue
                    # 涂卡：框内收 8px 画一个实心椭圆（铅笔涂黑的样子）
                    x0, y0 = cell["x"] + 8, cell["y"] + 8
                    x1, y1 = cell["x"] + cell["w"] - 8, cell["y"] + cell["h"] - 8
                    draw.ellipse([x0, y0, x1, y1], fill=55)
            else:
                boxes = it.get("boxes") if it.get("kind") == card.KIND_FILL else it.get("answer_boxes")
                for box in boxes or []:
                    blank = int(box.get("blank", 1))
                    text = str((answer.get("texts") or {}).get(blank) or "")
                    if not text:
                        continue
                    font = _fit_font(text, int(box["w"]), int(box["h"]))
                    draw.text((box["x"] + 20, box["y"] + box["h"] * 0.18), text, font=font, fill=45)
        path = os.path.join(out_dir, "answer_%d.png" % page_no)
        img.save(path)
        out.append(path)
    return out


def shoot_photo(sheet_png: str, out_path: str, seed: int = 7) -> str:
    """把"作答完的答题卡"拍成手机照片：透视倾斜 + 光照渐变 + JPEG 压缩。"""
    sheet = cv2.imread(sheet_png, cv2.IMREAD_GRAYSCALE)
    if sheet is None:
        raise DemoError("image_unreadable", "读不到作答页：%s" % sheet_png)
    h, w = sheet.shape[:2]
    canvas_w, canvas_h = w + 2 * PHOTO_MARGIN, h + 2 * PHOTO_MARGIN
    rng = random.Random(seed)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([
        [PHOTO_MARGIN + rng.randint(-PHOTO_JITTER, PHOTO_JITTER),
         PHOTO_MARGIN + rng.randint(-PHOTO_JITTER, PHOTO_JITTER)],
        [PHOTO_MARGIN + w + rng.randint(-PHOTO_JITTER, PHOTO_JITTER),
         PHOTO_MARGIN + rng.randint(-PHOTO_JITTER, PHOTO_JITTER)],
        [PHOTO_MARGIN + w + rng.randint(-PHOTO_JITTER, PHOTO_JITTER),
         PHOTO_MARGIN + h + rng.randint(-PHOTO_JITTER, PHOTO_JITTER)],
        [PHOTO_MARGIN + rng.randint(-PHOTO_JITTER, PHOTO_JITTER),
         PHOTO_MARGIN + h + rng.randint(-PHOTO_JITTER, PHOTO_JITTER)],
    ])
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(sheet, matrix, (canvas_w, canvas_h),
                                 flags=cv2.INTER_LINEAR, borderValue=205)
    # 光照：左上亮、右下暗（手机挡住一侧灯的典型效果）
    yy, xx = np.mgrid[0:canvas_h, 0:canvas_w]
    light = 1.06 - 0.22 * (xx / float(canvas_w)) - 0.10 * (yy / float(canvas_h))
    photo = np.clip(warped.astype(np.float32) * light, 0, 255).astype(np.uint8)
    cv2.imwrite(out_path, photo, [int(cv2.IMWRITE_JPEG_QUALITY), PHOTO_JPEG_QUALITY])
    return out_path


# ---------------------------------------------------------------- 合订 PDF
def merge_print_pdf(paper_pdf: str, card_pdf: str, out_path: str) -> dict:
    """卷面页 + 答题卡页 → 一个 print.pdf（一次打印，CONTRACT §4 合并规则）。"""
    out = pymupdf.open()
    try:
        for path in (paper_pdf, card_pdf):
            src = pymupdf.open(path)
            try:
                out.insert_pdf(src)
            finally:
                src.close()
        out.set_metadata({"title": "一键演示 · 卷面 + 答题卡", "producer": "app/demo.py"})
        out.save(out_path, garbage=3, deflate=True)
        pages = int(out.page_count)
    finally:
        out.close()
    return {"pdf": out_path, "pages": pages}


# ---------------------------------------------------------------- 数据库清理
_MASTERY_COLS = ("key", "attempts", "correct", "last_seen", "interval_days", "due_at")


def snapshot_mastery(conn, nos) -> dict:
    """跑前给掌握度拍快照（演示写进去的掌握度记录要能原样还原）。"""
    nos = [str(n) for n in nos]
    if not nos:
        return {}
    sql = "SELECT %s FROM mastery WHERE key IN (%s)" % (
        ", ".join(_MASTERY_COLS), ",".join("?" * len(nos)))
    try:
        rows = conn.execute(sql, nos).fetchall()
    except sqlite3.Error:
        return {}
    return {str(r[0]): tuple(r) for r in rows}


def restore_mastery(conn, nos, snapshot: dict) -> None:
    """按快照还原掌握度：演示期间**新增**的行删掉，**被改动**的行恢复原值。

    只处理 nos 里的题；快照里没有、但演示前就存在的行（快照读取失败等极端情况）不做删除，
    宁可留一条演示记录也不误删用户的学习进度。
    """
    nos = [str(n) for n in nos]
    if not nos:
        return
    marks = ",".join("?" * len(nos))
    snapshot = snapshot or {}
    current = {}
    try:
        for row in conn.execute("SELECT %s FROM mastery WHERE key IN (%s)"
                                % (", ".join(_MASTERY_COLS), marks), nos):
            current[str(row[0])] = tuple(row)
    except sqlite3.Error:
        return
    for key, row in current.items():
        if key not in snapshot:              # 演示跑出来的新行 → 删
            conn.execute("DELETE FROM mastery WHERE key=?", (key,))
        elif tuple(row) != tuple(snapshot[key]):   # 被 demo 改过 → 还原
            conn.execute("INSERT OR REPLACE INTO mastery(%s) VALUES(%s)"
                         % (", ".join(_MASTERY_COLS), ",".join("?" * len(_MASTERY_COLS))),
                         tuple(snapshot[key]))
    conn.commit()


def cleanup_db(conn, pids, sids, mastery_nos, mastery_snapshot) -> dict:
    """把演示写进去的库记录删掉：papers / submissions / mastery（按快照还原）。"""
    done = {"papers": 0, "submissions": 0, "mastery": 0}
    for pid in pids:
        if not pid:
            continue
        cur = conn.execute("DELETE FROM papers WHERE pid=?", (str(pid),))
        done["papers"] += max(0, cur.rowcount or 0)
    for sid in sids:
        if not sid:
            continue
        cur = conn.execute("DELETE FROM submissions WHERE sid=?", (str(sid),))
        done["submissions"] += max(0, cur.rowcount or 0)
    if mastery_nos:
        before = len(snapshot_mastery(conn, mastery_nos))
        restore_mastery(conn, mastery_nos, mastery_snapshot)
        after = len(snapshot_mastery(conn, mastery_nos))
        done["mastery"] = max(0, before - after)
    conn.commit()
    return done


# ---------------------------------------------------------------- 主流程
def _reset_out_dir(out_dir: str) -> None:
    """清掉上一次 demo 的产物（只动 demo 自己的命名，绝不整目录删）。"""
    patterns = ("paper", "card", "print", "answer_", "photo_page")
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(out_dir, name)
        base = name.lower()
        if os.path.isdir(path):
            if base in ("train",) or base.startswith("p2026"):
                shutil.rmtree(path, ignore_errors=True)
            continue
        if base.endswith((".pdf", ".png", ".jpg", ".docx")) and any(
                base.startswith(p) or base == p + ".pdf" for p in patterns):
            try:
                os.remove(path)
            except OSError:
                pass


def run_demo(args) -> int:
    out_dir = os.path.abspath(args.out or DEFAULT_OUT)
    db_path = os.path.abspath(args.db or DEFAULT_DB)
    os.makedirs(out_dir, exist_ok=True)
    _reset_out_dir(out_dir)

    if not os.path.isfile(db_path):
        raise DemoError("db_missing", "找不到数据库：%s" % db_path,
                        ["先在 E:\\OCR 下执行：python app\\build_db.py（约 20 秒）"])
    if not render.word_available():
        raise DemoError("WordUnavailable", "本机没有可用的 Microsoft Word（找不到 WINWORD.EXE）",
                        ["装 Word 2016+ 后重试；或先用 python paper.py --sizes 10 只出 docx 看排版"])

    timings = []
    print("== 一键演示开始 ==")
    print("   数据库：%s" % db_path)
    print("   产物：  %s" % out_dir)

    conn = _stage(timings, "连接数据库", _connect, db_path)
    pids, sids = [], []
    mastery_nos, mastery_snapshot = [], {}
    keep_db = bool(args.keep_db)
    plan, wrong_idx = {}, set()
    graded = {"choice_full": 0, "choice_correct": 0, "choice_wrong": 0,
              "choice_unsure": 0, "pending": 0, "qr_ok": 0, "pages": 0}
    train_pages = 0
    train_items = 0
    try:
        # 1) 组卷
        mix = FAST_MIX if args.fast else dict(DEFAULT_MIX)
        params = build_params(mix, seed=args.seed)
        manifest = _stage(timings, "组卷选题", picker.build, params, conn)
        pid = str(manifest["pid"])
        pids.append(pid)
        print("   卷号：%s（%d 题：%s）"
              % (pid, len(manifest["items"]),
                 " / ".join("%s %d" % (k, v) for k, v in mix.items() if v > 0)))

        # 2) 卷面（render.py 通过 paper 内部调用：docx → PDF → PNG）
        pres = _stage(timings, "卷面排版+导 PDF", paper.render_paper, manifest, out_dir,
                      name="paper")
        manifest["paper"] = {"pages": int(pres["pages"])}
        print("   卷面：%d 页，%d 张 PNG" % (pres["pages"], len(pres["png"])))

        # 3) 答题卡
        cres = _stage(timings, "答题卡生成", card.build_card, manifest, out_dir)
        manifest = cres.get("manifest") or manifest
        card_info = manifest.get("card") or {}
        print("   答题卡：%d 页（接在卷面第 %d 页之后）"
              % (len(cres["png"]), int(card_info.get("start_page") or 0)))

        # 4) 落库（判卷要靠 papers.manifest 里的 card 几何）
        _stage(timings, "落库", picker.save, conn, manifest, pres["docx"], pres["pdf"])

        # 5) 合订打印
        merged = _stage(timings, "合并打印 PDF", merge_print_pdf, pres["pdf"], cres["pdf"],
                        os.path.join(out_dir, "print.pdf"))
        print("   print.pdf：%d 页（卷面 %d + 答题卡 %d）"
              % (merged["pages"], pres["pages"], len(cres["png"])))

        # 6) 模拟作答 + 拍照
        plan, wrong_idx = plan_answers(manifest, args.wrong)
        sheets = _stage(timings, "模拟作答", compose_answer_sheet, manifest, card_info,
                        cres["png"], plan, out_dir)
        photos = []
        for i, sheet in enumerate(sheets, 1):
            photos.append(_stage(
                timings, "拍照模拟·第%d页" % i, shoot_photo, sheet,
                os.path.join(out_dir, "photo_page%d.jpg" % i), seed=args.seed + i))
        print("   模拟作答：%d 页（故意答错 %d 道选择题）" % (len(sheets), len(wrong_idx)))

        # 7) 判卷（同步：标定 + QR + OMR；填空/解答 pending）
        mastery_nos = [str(it["no"]) for it in manifest["items"]]
        mastery_snapshot = snapshot_mastery(conn, mastery_nos)
        results = []
        for i, photo in enumerate(photos, 1):
            res = _stage(timings, "判卷·第%d页" % i, grade.grade_image, conn, pid, photo,
                         None, out_dir)
            if res.get("error"):
                raise DemoError(_code_for_exception_code(res["error"]),
                                res.get("detail") or "", ["照片：%s" % photo])
            sids.append(res.get("sid"))
            results.append(res)
            graded["pages"] += 1
            graded["qr_ok"] += 1 if res.get("qr_ok") else 0
            for unit in res.get("items") or []:
                verdict = unit.get("verdict")
                if unit.get("kind") == card.KIND_OMR:      # 选择题：同步阶段就判出结果
                    if unit.get("expected"):
                        graded["choice_full"] += 1
                    if verdict == "correct":
                        graded["choice_correct"] += 1
                    elif verdict == "wrong":
                        graded["choice_wrong"] += 1
                    elif verdict == "unsure":
                        graded["choice_unsure"] += 1
                elif verdict == "pending":                  # 填空/解答：等 OCR 或人工
                    graded["pending"] += 1

        # 8) 异步识别阶段（manual 后端：不联网，pending 保持待人工确认）
        recognizer = ocr.Recognizer(backend="manual")
        fill_stats = {"backend": "manual", "left_manual": 0, "filled": 0}
        for sid in sids:
            got = _stage(timings, "识别回填", grade.fill_pending, conn, sid, recognizer)
            fill_stats["backend"] = got.get("backend") or "manual"
            fill_stats["left_manual"] += int(got.get("left_manual") or 0)
            fill_stats["filled"] += int(got.get("filled") or 0)

        # 9) 训练卷（错题 → train.plan → picker.build → 出卷）
        wrong_nos = []
        for res in results:
            for unit in res.get("items") or []:
                if unit.get("verdict") in ("wrong", "unsure") and unit.get("no"):
                    wrong_nos.append(str(unit["no"]))
        wrong_nos = list(dict.fromkeys(wrong_nos))
        if not wrong_nos:
            raise DemoError("internal_error",
                            "判卷没得到任何错题（选择题全对），没法生成训练卷",
                            ["把 --wrong 调大，或换 --seed 换一套题"])
        train_info = _make_training_paper(conn, wrong_nos, out_dir, timings,
                                          fast=bool(args.fast), seed=args.seed)
        pids.append(train_info["pid"])
        train_pages = train_info["pages"]
        train_items = train_info["items"]
        if train_info.get("note"):
            print("   " + train_info["note"])

        # 10) 摘要
        total = sum(sec for _name, sec in timings)
        _print_summary(manifest, pres, cres, merged, photos, graded, fill_stats,
                       train_info, wrong_nos, timings, total, out_dir, keep_db)
        return 0
    finally:
        if not keep_db:
            try:
                done = cleanup_db(conn, pids, sids, mastery_nos, mastery_snapshot)
                if done["papers"] or done["submissions"] or done["mastery"]:
                    print("   库清理：papers -%d / submissions -%d / mastery 还原 %d"
                          % (done["papers"], done["submissions"], done["mastery"]))
            except sqlite3.Error as exc:      # 清理失败不影响演示结论，但要告诉用户
                print("   ⚠ 库清理失败（%s）：papers/submissions 里可能留下本次演示记录" % exc)
        else:
            print("   库记录：按 --keep-db 保留（papers/submissions 里有本次演示卷）")
        conn.close()


def _code_for_exception_code(code: str) -> str:
    """grade.py 返回的错误码 → errors.py 认识的那套（大部分同名）。"""
    code = str(code or "")
    if code in errors.CODES:
        return code
    return {"image_unreadable": "internal_error"}.get(code, "internal_error")


def _connect(db_path: str):
    conn = db.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.Error:
        pass
    return conn


def _make_training_paper(conn, wrong_nos: list, out_dir: str, timings: list,
                         fast: bool, seed: int) -> dict:
    """错题 → 训练卷参数 → 组卷 → 出卷（--fast 只出 docx，省一次 Word 往返）。

    ``train.plan`` 的 ``n_per`` 从 3 往下试：题库里同类替代题不够时会降档，降到几就在
    ``note`` 里说明（不静默降级）。

    选题质量由 ``train.plan`` 自己把关（t36 起候选池接 ``quality_flags``，默认
    ``quality_gate=True``），被挡的数量在 ``train.quality_filtered`` 里，demo 原样显示。
    """
    last_error = None
    note = ""
    for n_per in (3, 2, 1):
        try:
            tparams = _stage(timings, "训练卷规划(n_per=%d)" % n_per, train.plan, conn,
                             wrong_nos, n_per, title="错题训练卷 · 一键演示",
                             seed=seed)
            filtered = 0
            try:
                filtered = int((tparams.get("train") or {}).get("quality_filtered") or 0)
            except (TypeError, ValueError):
                filtered = 0
            if filtered:
                note = ("训练卷：train 已按题库体检挡掉 %d 道替代题（quality_gate），"
                        "卷面只用可印题" % filtered)
            tmanifest = _stage(timings, "训练卷组卷", picker.build, tparams, conn)
        except (ValueError, picker.SelectError) as exc:
            last_error = exc
            note = "训练卷：每道错题配 %d 道太多（题库里同类替代题不够或题量被过滤），自动降到更小的配比" % n_per
            continue
        train_dir = os.path.join(out_dir, "train")
        os.makedirs(train_dir, exist_ok=True)
        if fast:
            built = _stage(timings, "训练卷排版(docx)", paper.build_docx, tmanifest,
                           train_dir, "train")
            _stage(timings, "训练卷保真体检", paper.check_docx, built["docx"], tmanifest,
                   built["expected"])
            picker.save(conn, tmanifest, built["docx"], "")
            return {"pid": tmanifest["pid"], "items": len(tmanifest["items"]), "pages": 0,
                    "pdf": "", "docx": built["docx"], "n_per": n_per, "note": note}
        tres = _stage(timings, "训练卷排版+导 PDF", paper.render_paper, tmanifest, train_dir,
                      name="train")
        picker.save(conn, tmanifest, tres["docx"], tres["pdf"])
        return {"pid": tmanifest["pid"], "items": len(tmanifest["items"]),
                "pages": int(tres["pages"]), "pdf": tres["pdf"], "docx": tres["docx"],
                "n_per": n_per, "note": note}
    raise DemoError("internal_error",
                    "训练卷组不出来：%s" % (last_error,),
                    ["错题太少或题库里同类替代题被过滤光了；试试 python app/demo.py --seed 7"])


def _save_summary_artifacts(out_dir: str, lines: list) -> dict:
    """把摘要存成 txt + png（交付说明里贴图/贴文本都方便）。"""
    saved = {}
    txt = os.path.join(out_dir, "summary.txt")
    try:
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        saved["txt"] = txt
    except OSError:
        pass
    try:
        font = _load_font(21)
        head = _load_font(26)
        line_h = 34
        widths = []
        for i, line in enumerate(lines):
            f = head if i == 0 else font
            left, _t, right, _b = f.getbbox(line) if hasattr(f, "getbbox") else (0, 0, len(line) * 20, 0)
            widths.append(right - left)
        width = max(widths or [600]) + 80
        height = line_h * len(lines) + 70
        img = Image.new("RGB", (max(width, 640), height), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, img.width - 1, img.height - 1], outline=(190, 190, 190))
        y = 34
        for i, line in enumerate(lines):
            draw.text((36, y), line, font=(head if i == 0 else font), fill=(20, 20, 20))
            y += line_h
        png = os.path.join(out_dir, "summary.png")
        img.save(png)
        saved["png"] = png
    except Exception:                  # noqa: BLE001 - 截图失败不影响演示
        pass
    return saved


def _print_summary(manifest, pres, cres, merged, photos, graded, fill_stats,
                   train_info, wrong_nos, timings, total, out_dir, keep_db) -> None:
    print("")
    title = "================ 一键演示 · 结果摘要 ================"
    by_qtype = manifest.get("stats", {}).get("by_qtype") or {}
    rows = [
        ("卷号", "%s" % manifest["pid"]),
        ("题数", "%d（%s）" % (len(manifest["items"]),
                            " / ".join("%s %d" % (k, v) for k, v in by_qtype.items()))),
        ("卷面", "paper.pdf %d 页 / PNG %d 张（2480×3508）" % (pres["pages"], len(pres["png"]))),
        ("答题卡", "card.pdf %d 页" % len(cres["png"])),
        ("打印", "print.pdf %d 页（卷面在前 + 答题卡在后）" % merged["pages"]),
        ("模拟拍照", "%d 张（四角抖动 %dpx + 光照渐变 + JPEG Q%d）"
         % (len(photos), PHOTO_JITTER, PHOTO_JPEG_QUALITY)),
        ("选择题判分", "%d/%d 正确，错 %d，存疑 %d（QR 读出 %d/%d 页）"
         % (graded["choice_correct"], graded["choice_full"], graded["choice_wrong"],
            graded["choice_unsure"], graded["qr_ok"], graded["pages"])),
        ("待人工确认", "%d 项（OCR=%s：填空/解答不联网，保持 pending）"
         % (fill_stats["left_manual"], fill_stats["backend"])),
        ("训练卷", "%d 题（来自 %d 道错题，n_per=%d）%s"
         % (train_info["items"], len(wrong_nos), train_info["n_per"],
            ("→ train/train.pdf %d 页" % train_info["pages"]) if train_info["pages"]
            else "→ train/train.docx（--fast 只出 docx）")),
    ]
    width = max(len(k) for k, _v in rows)
    lines = [title]
    lines += ["  %-*s  %s" % (width, key, value) for key, value in rows]
    lines.append("  %-*s  %s" % (width, "阶段耗时",
                                  " · ".join("%s %s" % (name, _human(sec)) for name, sec in timings)))
    lines.append("  %-*s  %s" % (width, "总耗时", _human(total)))
    lines.append("  %-*s  %s" % (width, "产物目录", out_dir))
    lines.append("  %-*s  %s" % (width, "打印", os.path.join(out_dir, "print.pdf")
                                  + "（直接打印：卷面在前、答题卡在后）"))
    lines.append("  %-*s  %s" % (width, "预览", " / ".join(
        os.path.basename(p) for p in list(pres["png"])[:1] + list(cres["png"])[:1]
        + list(photos)[:1]) + "（用看图软件打开即可）"))
    lines.append("  %-*s  %s" % (width, "库记录",
                                  "已清理（不污染「我的卷子」；--keep-db 可保留）" if not keep_db
                                  else "已保留（--keep-db）"))
    lines.append("  用浏览器看：先 python app/server.py，再打开 http://127.0.0.1:8710/papers")
    lines.append("  直接打印：%s" % os.path.join(out_dir, "print.pdf"))
    lines.append("====================================================")
    for line in lines:
        print(line)
    saved = _save_summary_artifacts(out_dir, lines)
    if saved:
        print("  摘要存档：%s" % " / ".join(os.path.basename(p) for p in saved.values()))


# ---------------------------------------------------------------- CLI
def _parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="demo.py", description="一键演示：组卷 → 出卷 → 答题卡 → 拍照判卷 → 训练卷")
    ap.add_argument("--out", default=DEFAULT_OUT, help="产物目录（默认 app/out/demo）")
    ap.add_argument("--db", default=DEFAULT_DB, help="题库（默认 app/data/app.db）")
    ap.add_argument("--fast", action="store_true",
                    help="小卷（3+2+1）且训练卷只出 docx，用于冒烟/测试，省一次 Word 往返")
    ap.add_argument("--keep-db", action="store_true",
                    help="保留 papers/submissions/mastery 里的演示记录（默认跑完清理）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="组卷种子（默认 %(default)s）")
    ap.add_argument("--wrong", type=int, default=N_DELIBERATE_WRONG,
                    help="故意答错的选择题数（默认 %(default)s）")
    ap.add_argument("--debug", action="store_true", help="失败时打印完整 traceback")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    """CLI 入口：跑完整演示，返回退出码（0 成功 / 1 失败）。"""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run_demo(args)
    except DemoError as exc:
        _report(exc, args.debug)
        return 1
    except Exception as exc:  # noqa: BLE001 - 演示脚本：任何异常都要翻成人话
        wrapped = DemoError(_code_for_exception(exc), "%s: %s" % (type(exc).__name__, exc))
        details = getattr(exc, "details", None)     # 题量差额等结构化信息别丢
        if isinstance(details, dict):
            wrapped.details = details
        _report(wrapped, args.debug)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
