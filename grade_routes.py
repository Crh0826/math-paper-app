# -*- coding: utf-8 -*-
"""T22 · 判卷页面路由（APIRouter 独立模块，**不改 server.py**）。

接线方式（ui-engineer 在 server.py 里加两行即可）：

    from grade_routes import router as grade_router
    app.include_router(grade_router)

路由（CONTRACT §4 + §4.1 两阶段判卷）：

    GET  /submissions                    上传页（最近卷子 + 选图/拍照）
    POST /submissions                    存图 → grade.grade_image → 302 /submissions/{sid}
    GET  /submissions/{sid}              结果页（总分 + 逐题表 + 待确认裁切图 + 识别中轮询）
    POST /submissions/{sid}/reocr        grade.fill_pending（幂等，可重复点）
    POST /submissions/{sid}/confirm      grade.apply_confirm（逐题改判 / 填答案）
    GET  /submissions/{sid}/status       JSON：pending / filled / backend / empty_batch
    GET  /submissions/{sid}/crop/{name}  待确认裁切图（只认 detail 里登记过的文件，防目录穿越）

可注入的 app.state（都有缺省值，不注入也能跑）：

    app.state.db_path     题库路径（server.create_app 已设置）；缺省 app/data/app.db
    app.state.out_dir     产物根目录；缺省 app/out（与 grade.OUT_ROOT 一致）
    app.state.recognizer  识别器；缺省 None = 让 grade 按 config.json 选后端（测试注入 manual）

设计取舍：
* 同步阶段只判选择题，填空/解答留 pending；页面显示「识别中（约 30 秒）」并轮询 status，
  识别完自动刷新——对应 CONTRACT §4.1 的 UI 要求，超时/失败一律留在待人工确认，不卡页面；
* `grade.apply_confirm` 是**整题粒度**（corrections 以 idx 为键）：多空题只改其中一空时，
  整题改判会把另一空也覆盖掉。所以本模块把改判拆成两条路：
  整题一致 → grade.apply_confirm；同题各空判定不同 → `_apply_per_unit` 按空落库
  （分数口径与 grade._score 一致，掌握度仍复用 grade 的幂等记账）。已在任务报告里报给队长。
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from jinja2 import Environment, FileSystemLoader, select_autoescape

import db as dbmod
import errors as errs
import grade

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DEFAULT_DB = os.path.join(BASE_DIR, "data", "app.db")
OUT_ROOT = os.path.join(BASE_DIR, "out")

MAX_UPLOAD = 40 * 1024 * 1024        # 40 MB：手机原图 3~12 MB，留足余量
PID_MAX_LEN = 64
PID_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
UPLOAD_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
#: 只做「像不像图片」的粗判（cv2.imread 认内容不认后缀，这里挡的是 PDF/docx 之类）
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"BM", b"II*\x00", b"MM\x00*")

VERDICTS = ("correct", "wrong", "unsure", "pending", "ungraded")
VERDICT_LABELS = {"correct": "判对", "wrong": "判错", "unsure": "待确认",
                  "pending": "待识别", "ungraded": "不判分"}
VERDICT_TONES = {"correct": "ok", "wrong": "fail", "unsure": "warn",
                 "pending": "warn", "ungraded": ""}

#: grade 是否已支持「逐空」改判键（`"idx-blank"` / `(idx, blank)`）。
#: t24 之后的 grade.py 有 `_parse_corrections`；老版本没有 → 走本地 `_apply_per_unit` 兜底。
_GRADE_NARROW_OK = callable(getattr(grade, "_parse_corrections", None))

router = APIRouter()

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR, encoding="utf-8"),
    autoescape=select_autoescape(("html", "xml")),
    auto_reload=True,
)


def _thousands(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "" if value is None else str(value)
    return f"{int(value):,}"


_env.filters["num"] = _thousands


def pid_is_safe(pid: str) -> bool:
    """卷号会拼进产物目录名，只允许字母数字与 -_.（防 ../ 之类跑出 app/out）。"""
    pid = str(pid or "")
    return 0 < len(pid) <= PID_MAX_LEN and all(ch in PID_SAFE_CHARS for ch in pid) and ".." not in pid


def _looks_like_image(data: bytes) -> bool:
    """粗判：JPEG / PNG / BMP / TIFF / WebP。PDF、docx、改了后缀的文本一律挡在门外。"""
    head = bytes(data[:16])
    if any(head.startswith(magic) for magic in _IMAGE_MAGIC):
        return True
    return head.startswith(b"RIFF") and head[8:12] == b"WEBP"


def render(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    html = _env.get_template(name).render(**ctx)
    return HTMLResponse(html, status_code=status_code)


# --------------------------------------------------------------------------- 小工具
def _as_int(value):
    """REAL 列里的 3.0 要显示成 3（分数是整数个判分单元）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    return int(f) if abs(f - int(f)) < 1e-9 else f


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime())


def _rel(path) -> str:
    """页面里只显示相对 app/ 的路径，别把 C:\\... 甩给用户。"""
    try:
        return os.path.relpath(str(path), BASE_DIR)
    except (ValueError, TypeError):
        return str(path or "")


def _db_path(request: Request) -> str:
    state = getattr(request.app, "state", None)
    path = getattr(state, "db_path", None) or os.environ.get("MATH_APP_DB") or DEFAULT_DB
    return os.path.abspath(path)


def _out_root(request: Request) -> str:
    state = getattr(request.app, "state", None)
    root = getattr(state, "out_dir", None) or OUT_ROOT
    return os.path.abspath(root)


def _recognizer(request: Request):
    """测试注入用；缺省 None = grade 自己按 config.json 选后端。"""
    state = getattr(request.app, "state", None)
    return getattr(state, "recognizer", None)


# --------------------------------------------------------------------------- 后台识别（P0）
#: 用户实测 bug：上传后没人触发 fill_pending，页面永远「识别中」。修法：
#:   ① 上传成功立刻起一个后台线程跑 fill_pending；
#:   ② /status 兜底：还有 pending 且没有 job 在跑、且距上次识别/创建超过 OCR_RETRY_AFTER 秒 → 再起一次；
#:   ③ 同一个 sid 在 OCR_JOB_TTL 内只跑一个（防每次轮询都发一次云端请求）。
OCR_RETRY_AFTER = 20.0       # 秒：多久没人跑就兜底重试
OCR_JOB_TTL = 90.0           # 秒：一个 job 最多算「在跑」这么久（防线程卡死把去重锁死）
OCR_MAX_RETRIES = 3          # 同一 sid 最多自动重试几次（防每 20 秒烧一次云端额度）
_JOBS_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """后台线程的日志：只写 stderr，绝不影响页面。"""
    try:
        print("[grade_routes %s] %s" % (time.strftime("%H:%M:%S"), msg),
              file=sys.stderr, flush=True)
    except Exception:      # noqa: BLE001 - 日志失败也不能出事
        pass


def _auto_ocr(request: Request) -> bool:
    """缺省开（生产行为）；测试要观察「同步阶段」状态时置 False。"""
    state = getattr(request.app, "state", None)
    value = getattr(state, "auto_ocr", None)
    return True if value is None else bool(value)


def _retry_after(request: Request) -> float:
    state = getattr(request.app, "state", None)
    try:
        value = getattr(state, "ocr_retry_after", None)
        return float(value) if value is not None else OCR_RETRY_AFTER
    except (TypeError, ValueError):
        return OCR_RETRY_AFTER


def _jobs(app) -> dict:
    state = app.state
    jobs = getattr(state, "ocr_jobs", None)
    if not isinstance(jobs, dict):
        jobs = {}
        state.ocr_jobs = jobs
    return jobs


def _attempts(app) -> dict:
    state = app.state
    counts = getattr(state, "ocr_attempts", None)
    if not isinstance(counts, dict):
        counts = {}
        state.ocr_attempts = counts
    return counts


def _job_running(app, sid: str) -> bool:
    with _JOBS_LOCK:
        started = _jobs(app).get(sid)
    return bool(started) and (time.time() - float(started)) < OCR_JOB_TTL


def _clear_job(app, sid: str) -> None:
    with _JOBS_LOCK:
        _jobs(app).pop(sid, None)


def _start_ocr_job(app, sid: str, db_path: str, recognizer, reason: str = "") -> bool:
    """起一个后台 fill_pending；同一 sid 在 OCR_JOB_TTL 内只跑一个。返回是否真的起了。"""
    now = time.time()
    with _JOBS_LOCK:
        jobs = _jobs(app)
        started = jobs.get(sid)
        if started and (now - float(started)) < OCR_JOB_TTL:
            return False
        jobs[sid] = now
        counts = _attempts(app)
        counts[sid] = int(counts.get(sid) or 0) + 1
        try:
            app.state.ocr_starts = int(getattr(app.state, "ocr_starts", 0) or 0) + 1
        except (TypeError, ValueError):
            app.state.ocr_starts = 1
    _log("起识别 job %s（%s）" % (sid, reason or "-"))
    threading.Thread(target=_run_fill, args=(sid, db_path, recognizer, app),
                     name="ocr-%s" % sid, daemon=True).start()
    return True


def _run_fill(sid: str, db_path: str, recognizer, app) -> None:
    """后台跑一次 fill_pending：独立连接、异常只记日志、结束一定释放 job 标记。"""
    conn = None
    try:
        conn = dbmod.connect(db_path)
        res = grade.fill_pending(conn, sid, recognizer)
        if res.get("error"):
            _log("识别 job %s 失败：%s %s" % (sid, res.get("error"), res.get("detail") or ""))
        else:
            _log("识别 job %s 完成：filled=%s left=%s backend=%s empty_batch=%s"
                 % (sid, res.get("filled"), res.get("left_manual"),
                    res.get("backend"), res.get("empty_batch")))
    except Exception as exc:       # noqa: BLE001 - 后台线程绝不能让主流程背锅
        _log("识别 job %s 抛异常：%s: %s" % (sid, type(exc).__name__, exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:      # noqa: BLE001
                pass
        _clear_job(app, sid)


def _parse_iso(text) -> float:
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    try:
        return _dt.datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _pending_age(detail: dict, row) -> float:
    """距「最后一次识别/确认」或「创建」过去多久（秒）；拿不到时间就当很久以前。"""
    stamp = _detail_stamp(detail) or _parse_iso(row["created_at"])
    if stamp <= 0:
        return float("inf")
    return max(0.0, time.time() - stamp)


def _should_retry(detail: dict) -> bool:
    """manual 后端 + 整批空 = 再试也没用，别烧云端额度。"""
    return not (detail.get("empty_batch") and str(detail.get("backend") or "") == "manual")


class _DbMissing(Exception):
    """库文件不存在（不顺手建空库，交给错误页提示去建库）。"""


class _conn:
    """一次请求一个连接（用完就关；dbmod.connect 负责建表/开 WAL）。"""

    def __init__(self, request: Request):
        self.path = _db_path(request)
        self.conn = None

    def __enter__(self):
        if not os.path.isfile(self.path):
            raise _DbMissing(self.path)
        self.conn = dbmod.connect(self.path)
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
        return False


def _guard(request: Request, fn, json_errors: bool = False):
    """跑一段需要 DB 的逻辑；缺库/被锁 → 错误页（或 JSON），绝不把 traceback 丢给用户。"""
    try:
        with _conn(request) as conn:
            return fn(conn)
    except _DbMissing as exc:
        if json_errors:
            return JSONResponse({"error": "db_missing", "path": str(exc)}, status_code=503)
        return _render_error("db_missing", {"path": str(exc)}, status_code=503)
    except sqlite3.OperationalError as exc:
        message = str(exc)
        code = "db_locked" if "locked" in message.lower() else "internal_error"
        if json_errors:
            return JSONResponse({"error": code, "message": message}, status_code=503)
        return _render_error(code, {"message": message}, status_code=503)
    except sqlite3.Error as exc:
        if json_errors:
            return JSONResponse({"error": "internal_error", "message": str(exc)},
                                status_code=503)
        return _render_error("internal_error", {"message": str(exc)}, status_code=503)


def _error_detail(res: dict) -> dict:
    """grade 的返回 → errors.explain 的 detail（把「只检出 2/4 个 ArUco」翻成 found=2）。"""
    detail = {"pid": res.get("pid") or "", "message": res.get("detail") or ""}
    text = str(res.get("detail") or "")
    for mark in ("只检出 ", "只找到 "):
        if mark in text:
            tail = text.split(mark, 1)[1]
            num = ""
            for ch in tail:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                detail["found"] = num
                break
    return detail


def _render_error(code: str, detail=None, status_code: int = 422,
                  retry_href: str = "/submissions", retry_label: str = None,
                  extra_note: str = ""):
    info = errs.explain(code, detail or {})
    return render("grade_error.html", status_code=status_code, page_title=info["title"],
                  e=info, code=str(code), retry_href=retry_href,
                  retry_label=retry_label or info.get("retry_label") or "重试",
                  extra_note=extra_note)


#: grade 新加、errors.py 还没登记的错误码 → 给一段人话（否则用户只看到「出了点问题（错误码 xxx）」）
_GRADE_EXTRA_ERRORS = {
    "legacy_card": {
        "title": "这张答题卡是旧版，判不了",
        "what": "卡上的题目标识是旧版（4×4）格式，现在的判卷程序不认。",
        "why": "答题卡模板升级过：旧卡上的标识块和现在的不一样，硬判会错位，所以宁可拒绝。",
        "fix": "① 回组卷页重新生成这份卷子，重新打印答题卡\n"
               "② 用新打印的卡再拍一次（旧卡可以丢掉）",
        "retry_label": "重新打印答题卡",
    },
    "no_question_ids": {
        "title": "照片里没找到题目标识",
        "what": "照片拍清楚了，但一个题目编号黑块都没扫到。",
        "why": "可能拍到的不是答题卡作答区，或者卡被裁掉/被手挡住了标识块那一列。",
        "fix": "① 重拍：让答题卡整页入画，四个角都拍到\n"
               "② 确认拍的是答题卡（不是卷面）\n"
               "③ 别裁页边：题目编号黑块在每题左侧一列",
        "retry_label": "重新拍照",
    },
}


def _render_grade_error(res: dict, retry_href: str = "/submissions", extra_note: str = ""):
    """grade 的 error 回执 → 错误页。已知码走 errors.explain；grade 新加的码给专用文案。"""
    code = str(res.get("error") or "internal_error")
    detail = _error_detail(res)
    aliases = getattr(errs, "_ALIASES", {}) or {}
    if code in errs.CODES or code.lower() in aliases:
        return _render_error(code, detail, status_code=422, retry_href=retry_href,
                             extra_note=extra_note)
    info = _GRADE_EXTRA_ERRORS.get(code) or {
        "title": "这张答题卡判不了",
        "what": str(res.get("detail") or "判卷没给出原因。"),
        "why": "判卷程序遇到没预料到的情况就停下来了——它没有把题判错，只是没出结果。",
        "fix": "① 重拍一张再试\n② 确认用的是最新打印的答题卡\n③ 还不行把错误码 %s 和照片一起反馈" % code,
        "retry_label": "重新拍照",
    }
    return render("grade_error.html", status_code=422, page_title=info["title"],
                  e=info, code=code, retry_href=retry_href,
                  retry_label=info["retry_label"], extra_note=extra_note)


def _json_error(code: str, detail=None, status_code: int = 422, **extra):
    """fetch 用的错误回执：带 errors.explain 的中文四段，抽屉里直接显示，不跳页。"""
    info = errs.explain(code, detail or {})
    payload = {"ok": False, "error": str(code), "title": info["title"],
               "what": info["what"], "fix": info["fix"],
               "retry_label": info["retry_label"]}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


# --------------------------------------------------------------------------- 读库
def _load_submission(conn, sid: str):
    row = conn.execute(
        "SELECT sid,pid,created_at,image_path,warp_ok,qr_ok,score,full,detail_json"
        " FROM submissions WHERE sid=?", (sid,)).fetchone()
    if row is None:
        return None, {}
    detail = {}
    try:
        detail = json.loads(row["detail_json"] or "{}") or {}
    except (TypeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    return row, detail


def _score(units) -> tuple:
    """与 grade._score 同一口径：不判分的（无标准答案）不进分母。"""
    full = sum(1 for u in units if u.get("verdict") != "ungraded")
    score = sum(1 for u in units if u.get("verdict") == "correct")
    return score, full


def _pending_items(units) -> tuple:
    """待确认的「题」数（同一题多个空只算一题）+ 第一条待确认的锚点（滚动用）。"""
    seen, first = [], None
    for u in units or []:
        if u.get("verdict") not in ("pending", "unsure"):
            continue
        idx = u.get("idx")
        if idx in seen:
            continue
        seen.append(idx)
        if first is None:
            try:
                blank = int(u.get("blank", 1) or 1)
            except (TypeError, ValueError):
                blank = 1
            first = {"idx": idx, "blank": blank}
    return len(seen), first


def _wants_json(request: Request, fmt: str) -> bool:
    """fetch 要 JSON、浏览器导航要页面：显式 ?format= 优先，其次看 Accept。"""
    fmt = str(fmt or "").strip().lower()
    if fmt in ("json", "1", "true", "ajax"):
        return True
    if fmt in ("html", "page"):
        return False
    accept = str(request.headers.get("accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept


def _items_view(units) -> list:
    """detail.items → 模板行（裁切图名、判定中文、是否可改判、候选按钮）。"""
    rows = []
    for u in units or []:
        idx = u.get("idx")
        try:
            blank = int(u.get("blank", 1) or 1)
        except (TypeError, ValueError):
            blank = 1
        verdict = str(u.get("verdict") or "")
        kind = str(u.get("kind") or "")
        crop = str(u.get("crop_path") or "")
        name = os.path.basename(crop) if crop else ""
        reason = str(u.get("unsure_reason") or u.get("reason") or "")
        if reason == VERDICT_LABELS.get(verdict):
            reason = ""     # 「待识别」这种和判定徽章重复的说明不再重复一遍
        conf = u.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        rows.append({
            "idx": idx,
            "blank": blank,
            "qtype": str(u.get("qtype") or ""),
            "kind": kind,
            "picked": str(u.get("picked") or ""),
            "expected": str(u.get("expected") or ""),
            "verdict": verdict,
            "label": VERDICT_LABELS.get(verdict, verdict or "—"),
            "tone": VERDICT_TONES.get(verdict, ""),
            # grade 的约定：0.0 = 没报置信度（pending / 人工确认），显示成「—」而不是「0.00」
            "conf_text": ("%.2f" % conf) if (conf and conf > 0
                                            and verdict not in ("pending", "ungraded")) else "—",
            "reason": reason,
            "crop_name": name,
            "crop_ok": bool(name) and os.path.isfile(crop),
            "options": sorted(str(k) for k in (u.get("ratios") or {}).keys()) if kind == "omr" else [],
            "editable": verdict in ("unsure", "wrong", "pending"),
            "confirmed": bool(u.get("confirmed")),
        })
    return rows


def _status_payload(row, detail: dict) -> dict:
    units = detail.get("items") or []
    pending = sum(1 for u in units if u.get("verdict") == "pending")
    unsure = sum(1 for u in units if u.get("verdict") == "unsure")
    filled = sum(1 for u in units
                 if str(u.get("kind") or "") != "omr" and str(u.get("picked") or "").strip())
    return {
        "sid": row["sid"], "pid": row["pid"],
        "pending": pending, "unsure": unsure, "filled": filled, "total": len(units),
        "backend": detail.get("backend") or "manual",
        "empty_batch": bool(detail.get("empty_batch")),
        "score": _as_int(row["score"]), "full": _as_int(row["full"]),
        "done": pending == 0,
        "warp_ok": bool(row["warp_ok"]), "qr_ok": bool(row["qr_ok"]),
        "recognized_at": detail.get("recognized_at"),
        "confirmed_at": detail.get("confirmed_at"),
    }


# --------------------------------------------------------------------------- 上传页
def _render_upload(conn, pid: str = "", notice: dict = None, status_code: int = 200):
    papers = []
    try:
        rows = conn.execute(
            "SELECT pid,title,created_at,n_items FROM papers"
            " ORDER BY created_at DESC, pid DESC LIMIT 30").fetchall()
        papers = [{"pid": r["pid"], "title": r["title"] or "", "created_at": r["created_at"] or "",
                   "n_items": int(r["n_items"] or 0)} for r in rows]
    except sqlite3.Error:
        papers = []
    return render("grade_upload.html", status_code=status_code, page_title="判卷",
                  papers=papers, pid=pid or "", notice=notice)


@router.get("/submissions", response_class=HTMLResponse)
def upload_page(request: Request, pid: str = ""):
    """上传页：最近的卷子 + 选图/拍照。"""
    return _guard(request, lambda conn: _render_upload(conn, pid.strip()))


# --------------------------------------------------------------------------- 上传判卷
def _stage_upload(out_root: str, pid: str, data: bytes) -> str:
    stage_dir = os.path.join(out_root, pid, "_incoming")
    os.makedirs(stage_dir, exist_ok=True)
    name = "%s-%s.jpg" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    path = os.path.join(stage_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _move_into_submission(out_root: str, pid: str, sid: str, stage_path: str) -> str:
    target_dir = os.path.join(out_root, pid, "sub_%s" % sid)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, "source.jpg")
    try:
        os.replace(stage_path, target)
    except OSError:
        shutil.copyfile(stage_path, target)
        try:
            os.remove(stage_path)
        except OSError:
            pass
    return target


@router.post("/submissions")
async def create_submission(request: Request, pid: str = Form(""),
                            file: UploadFile = File(None)):
    """multipart(pid + file) → 存图 → grade_image → 302 /submissions/{sid}。"""
    pid = (pid or "").strip()
    out_root = _out_root(request)

    if not pid:
        return _guard(request, lambda conn: _render_upload(
            conn, "", {"tone": "warn", "title": "先填卷号",
                       "detail": "请填这份答题卡上的卷号（pid），或在下面的最近卷子里点一个，再上传照片。",
                       "hints": ["卷号印在答题卡右上角的二维码旁边，形如 P20261031-001",
                                 "找不到卷号：回组卷页看看最近生成的那份卷子"]},
            status_code=400))
    if not pid_is_safe(pid):
        return _guard(request, lambda conn: _render_upload(
            conn, pid, {"tone": "warn", "title": "卷号格式不对",
                        "detail": "卷号里只能有字母、数字和 - _ .，且不超过 %d 个字符。" % PID_MAX_LEN,
                        "hints": ["照抄答题卡上印的卷号，别自己改格式",
                                  "卷号形如 P20261031-001"]},
            status_code=400))

    data = b""
    try:
        data = await file.read() if file is not None else b""
    except Exception as exc:      # noqa: BLE001 - 读上传流失败也只是「没收到图片」
        data = b""
    if not data:
        return _guard(request, lambda conn: _render_upload(
            conn, pid, {"tone": "warn", "title": "没收到图片",
                        "detail": "没收到图片文件（0 字节）：重新选一张，或直接用手机拍照上传。",
                        "hints": ["手机拍的话：点「拍照」按钮，别选空相册项",
                                  "电脑上：确认选的是 jpg/png 图片文件，不是快捷方式"]},
            status_code=400))
    if len(data) > MAX_UPLOAD:
        return _guard(request, lambda conn: _render_upload(
            conn, pid, {"tone": "warn", "title": "图片太大",
                        "detail": "这张图 %.1f MB，超过 %d MB 上限：用手机原图或先压一下再传。"
                                  % (len(data) / 1048576.0, MAX_UPLOAD // 1048576),
                        "hints": ["手机相机直出的原图一般 3~12 MB，不用改",
                                  "超过上限多半是扫描件/TIFF：先导出成 jpg 再传"]},
            status_code=400))
    if not _looks_like_image(data):
        return _guard(request, lambda conn: _render_upload(
            conn, pid, {"tone": "warn", "title": "这不是图片文件",
                        "detail": "上传的内容不是图片（可能是 PDF / docx，或改了后缀的其它文件）。"
                                  "判卷要的是照片：%s。"
                                  % " / ".join(e.lstrip(".").upper() for e in UPLOAD_EXTS[:4]),
                        "hints": ["手机或相机直出的 jpg / png 最稳",
                                  "扫描件先导出成 jpg 再传；PDF 判不了（要先截图/导出图片）"]},
            status_code=400))

    stage_path = _stage_upload(out_root, pid, data)

    def run(conn):
        # 先确认卷子在 papers 里：不在就直接给「找不到这份卷子」，
        # 别让 grade 走到按题目标识扫描那条路再报 no_question_ids（那个码对用户没意义）
        try:
            known = conn.execute("SELECT 1 FROM papers WHERE pid=? LIMIT 1", (pid,)).fetchone()
        except sqlite3.Error:
            known = True
        if known is None:
            return _render_error("paper_not_found",
                                 {"pid": pid, "message": "papers 表里没有 %s" % pid},
                                 status_code=422,
                                 retry_href="/submissions?pid=%s" % quote(pid),
                                 retry_label="重新选卷")
        res = grade.grade_image(conn, pid, stage_path, out_dir=out_root)
        if res.get("error"):
            return _render_grade_error(
                res, retry_href="/submissions?pid=%s" % quote(pid),
                extra_note="这张照片留在 %s（可删）。" % _rel(stage_path))
        sid = str(res.get("sid") or "")
        if not sid:
            return _render_error("internal_error", {"message": "判卷没有返回提交编号"},
                                 status_code=500, retry_href="/submissions?pid=%s" % quote(pid))
        target = _move_into_submission(out_root, pid, sid, stage_path)
        try:
            conn.execute("UPDATE submissions SET image_path=? WHERE sid=?", (target, sid))
            conn.commit()
        except sqlite3.Error:
            pass
        # 同步阶段只判了选择题：填空/解答立刻丢给后台线程识别（P0：不触发就永远 pending）
        if int(res.get("pending") or 0) > 0 and _auto_ocr(request):
            _start_ocr_job(request.app, sid, _db_path(request), _recognizer(request),
                           reason="upload")
        return RedirectResponse("/submissions/%s" % sid, status_code=302)

    return _guard(request, run)


# --------------------------------------------------------------------------- 结果页
def _render_result(conn, sid: str, reocr: bool = False, filled: int = 0, left: int = 0,
                   empty_batch: bool = False, changed=None, status_code: int = 200):
    row, detail = _load_submission(conn, sid)
    if row is None:
        return _render_error("submission_not_found", {"sid": sid}, status_code=404,
                             retry_href="/submissions")

    units = detail.get("items") or []
    rows = _items_view(units)
    pending = sum(1 for r in rows if r["verdict"] == "pending")
    unsure = sum(1 for r in rows if r["verdict"] == "unsure")
    filled_n = sum(1 for r in rows if r["kind"] != "omr" and r["picked"].strip())
    score = _as_int(row["score"])
    full = _as_int(row["full"])
    is_empty = bool(detail.get("empty_batch")) or bool(empty_batch)
    # 空批提示只在「还有东西等着人工确认」或「刚点过重新识别」时出现：
    # 全部确认完还挂着这条黄色告示，只会变成噪音。
    show_empty = is_empty and (pending > 0 or bool(empty_batch))

    notice = None
    if changed is not None:
        # 人工改判是用户刚做的事，优先级最高（哪怕上一批识别是空的）
        if changed:
            text = "分数已重算：%s / %s。重复提交是幂等的，不会重复计分。" % (score, full)
            notice = {"tone": "ok", "title": "已保存 %s 处改判" % changed, "detail": text}
        else:
            notice = {"tone": "ok", "title": "没有需要保存的改动",
                      "detail": "改判框里的值和现在一样，分数没变。"}
    elif show_empty:
        info = errs.explain("empty_batch", {"sid": sid})
        hints = [h.strip() for h in info["fix"].splitlines() if h.strip()]
        if str(detail.get("backend") or "").endswith("manual"):
            hints = [h for h in hints if "ocr.backend" not in h]   # 已经是 manual 了就别再建议改
        notice = {"tone": "warn", "title": info["title"], "detail": info["what"],
                  "hints": hints, "code": "empty_batch"}
    elif reocr:
        notice = {"tone": "ok", "title": "重新识别完成",
                  "detail": "本次识别出 %d 个空，还有 %d 个留在待人工确认。" % (filled, left)}

    pending_items, first_pending = _pending_items(units)
    return render("grade_result.html", status_code=status_code, page_title="判卷结果",
                  sid=sid, pid=row["pid"], created_at=row["created_at"] or "",
                  image_path=_rel(row["image_path"]),
                  qr_ok=bool(row["qr_ok"]), warp_ok=bool(row["warp_ok"]),
                  qr=detail.get("qr"), backend=detail.get("backend") or "manual",
                  score=score, full=full,
                  pct=int(round(100.0 * score / full)) if full else 0,
                  rows=rows, pending=pending, unsure=unsure, filled=filled_n,
                  empty_batch=show_empty, notice=notice,
                  recognized_at=detail.get("recognized_at") or "",
                  confirmed_at=detail.get("confirmed_at") or "",
                  show_blank=any(r["blank"] != 1 for r in rows),
                  n_units=len(rows),
                  pending_items=pending_items, first_pending=first_pending)


@router.get("/submissions/{sid}", response_class=HTMLResponse)
@router.get("/submissions/{sid}/", response_class=HTMLResponse)
def result_page(request: Request, sid: str, reocr: int = 0, filled: int = 0, left: int = 0,
                empty_batch: int = 0, changed: int = None):
    """结果页：总分 + 逐题表 + 待确认裁切图 + 识别中轮询。"""
    return _guard(request, lambda conn: _render_result(
        conn, sid, reocr=bool(reocr), filled=filled, left=left,
        empty_batch=bool(empty_batch), changed=changed))


# --------------------------------------------------------------------------- 重新识别
@router.post("/submissions/{sid}/reocr")
def reocr(request: Request, sid: str):
    """异步阶段重跑一次：fill_pending（幂等，可重复点）。"""
    def run(conn):
        row, _detail = _load_submission(conn, sid)
        if row is None:
            return _render_error("submission_not_found", {"sid": sid}, status_code=404,
                                 retry_href="/submissions")
        res = grade.fill_pending(conn, sid, _recognizer(request))
        if res.get("error"):
            return _render_error(res["error"], _error_detail(res), status_code=422,
                                 retry_href="/submissions/%s" % sid)
        _drop_annotation(_out_root(request), row["pid"], sid)      # 重新识别 → 批注图过期
        # 用户手动点过「重新识别」：清掉 job 标记并重置自动重试计数，之后兜底还能再试
        _clear_job(request.app, sid)
        try:
            _attempts(request.app)[sid] = 0
        except Exception:      # noqa: BLE001 - 计数重置失败不影响主流程
            pass
        return RedirectResponse(
            "/submissions/%s?reocr=1&filled=%d&left=%d&empty_batch=%d"
            % (sid, int(res.get("filled") or 0), int(res.get("left_manual") or 0),
               1 if res.get("empty_batch") else 0),
            status_code=303)

    return _guard(request, run)


# --------------------------------------------------------------------------- 人工确认
def _parse_confirm(form, units):
    """表单 → (整题改判 {idx: {...}}, 按空改判 {(idx, blank): {...}})。

    只收真正变了的值：原值原样回传不算改判（避免手滑点保存就把所有题标成「已人工确认」）。
    """
    changed = {}
    for u in units:
        idx = u.get("idx")
        try:
            blank = int(u.get("blank", 1) or 1)
        except (TypeError, ValueError):
            blank = 1
        key_v = "verdict_%s_%s" % (idx, blank)
        key_t = "text_%s_%s" % (idx, blank)
        if key_v not in form and key_t not in form:
            continue
        verdict = str(form.get(key_v) or "").strip()
        if verdict not in VERDICTS:
            continue
        text = form.get(key_t)
        text = None if text is None else str(text).strip()
        if verdict == u.get("verdict") and (text is None or text == str(u.get("picked") or "")):
            continue
        changed[(idx, blank)] = {"verdict": verdict, "text": text}

    unit_count = {}
    for u in units:
        unit_count[u.get("idx")] = unit_count.get(u.get("idx"), 0) + 1

    groups = {}
    for (idx, blank), corr in changed.items():
        groups.setdefault(idx, {})[blank] = corr

    question_level, per_unit = {}, {}
    for idx, blanks in groups.items():
        verdicts = {c["verdict"] for c in blanks.values()}
        texts = {c["text"] for c in blanks.values()}
        if len(verdicts) == 1 and len(texts) == 1 and len(blanks) == unit_count.get(idx, 0):
            question_level[idx] = {"verdict": verdicts.pop(), "text": texts.pop()}
        else:
            for blank, corr in blanks.items():
                per_unit[(idx, blank)] = {"verdict": corr["verdict"], "text": corr["text"]}
    return question_level, per_unit


def _record_mastery(conn, units) -> int:
    """复用 grade 的幂等记账（同一题只记一次）；拿不到就算了，绝不拖死改判。"""
    fn = getattr(grade, "_record_mastery", None)
    if fn is None:
        return 0
    try:
        return int(fn(conn, units) or 0)
    except Exception:       # noqa: BLE001 - 掌握度是加分项，不能影响判卷
        return 0


def _apply_per_unit(conn, sid: str, updates: dict) -> dict:
    """按「空」改判（多空题只改一空时用）：分数口径与 grade._score 一致。"""
    row, detail = _load_submission(conn, sid)
    if row is None:
        return {"error": "submission_not_found", "sid": sid}
    units = detail.get("items") or []
    changed = 0
    for u in units:
        try:
            blank = int(u.get("blank", 1) or 1)
        except (TypeError, ValueError):
            blank = 1
        corr = updates.get((u.get("idx"), blank))
        if not corr:
            continue
        if corr.get("text") is not None:
            u["picked"] = corr["text"]
        u["verdict"] = corr["verdict"]
        u["confirmed"] = True
        u["reason"] = "人工确认"
        u.pop("unsure_reason", None)
        u.pop("mastery_verdict", None)
        changed += 1

    score, full = _score(units)
    detail["score"], detail["full"] = score, full
    detail["confirmed_at"] = _now_iso()
    try:
        conn.execute("UPDATE submissions SET score=?, full=?, detail_json=? WHERE sid=?",
                     (score, full, json.dumps(detail, ensure_ascii=False), sid))
        conn.commit()
    except sqlite3.Error as exc:
        return {"error": "db_locked", "detail": str(exc), "sid": sid}
    return {"sid": sid, "changed": changed, "score": score, "full": full,
            "recorded": _record_mastery(conn, units), "error": None}


def _confirm_payload(conn, sid: str, changed: int) -> dict:
    """fetch 回执：分数 + 逐空行 + 按题聚合结论，供抽屉/表格局部刷新（不再整页跳）。"""
    row, detail = _load_submission(conn, sid)
    if row is None:
        return {"ok": False, "error": "submission_not_found", "sid": sid}
    units = detail.get("items") or []
    score, full = _as_int(row["score"]), _as_int(row["full"])
    pending_items, first_pending = _pending_items(units)
    return {
        "ok": True, "sid": sid, "pid": row["pid"],
        "score": score, "full": full,
        "pct": int(round(100.0 * score / full)) if full else 0,
        "changed": int(changed or 0),
        "pending": sum(1 for u in units if u.get("verdict") == "pending"),
        "unsure": sum(1 for u in units if u.get("verdict") == "unsure"),
        "pending_items": pending_items, "first_pending": first_pending,
        "item_verdicts": detail.get("item_verdicts") or {},
        "items": _items_view(units),
        "notice": ("已保存 %d 处改判" % changed) if changed else "没有需要保存的改动",
    }


@router.post("/submissions/{sid}/confirm")
async def confirm(request: Request, sid: str, fmt: str = Query("", alias="format")):
    """逐题改判 / 填答案 → 重算分数。

    * 普通表单（无 JS）→ 303 回结果页；
    * fetch（`?format=json` 或 Accept: application/json）→ JSON 回执，抽屉里局部刷新不跳页。
    """
    form = await request.form()
    wants_json = _wants_json(request, fmt)

    def run(conn):
        row, detail = _load_submission(conn, sid)
        if row is None:
            if wants_json:
                return _json_error("submission_not_found", {"sid": sid}, status_code=404,
                                   sid=sid)
            return _render_error("submission_not_found", {"sid": sid}, status_code=404,
                                 retry_href="/submissions")
        units = detail.get("items") or []
        question_level, per_unit = _parse_confirm(form, units)
        changed = 0
        if question_level:
            res = grade.apply_confirm(conn, sid, question_level)
            if res.get("error"):
                if wants_json:
                    return _json_error(res["error"], _error_detail(res), status_code=422,
                                       sid=sid)
                return _render_error(res["error"], _error_detail(res), status_code=422,
                                     retry_href="/submissions/%s" % sid)
            changed += int(res.get("changed") or 0)
        if per_unit:
            # grade（t24 之后）已支持逐空键 "idx-blank"：走官方实现，掌握度与 item_verdicts 一并落库；
            # 老版本没有 _parse_corrections → 用本地 _apply_per_unit 兜底（队长要求先留着）
            if _GRADE_NARROW_OK:
                narrow = {"%s-%s" % (k[0], k[1]): v for k, v in per_unit.items()}
                res = grade.apply_confirm(conn, sid, narrow)
            else:
                res = _apply_per_unit(conn, sid, per_unit)
            if res.get("error"):
                if wants_json:
                    return _json_error(res["error"], {"message": res.get("detail") or ""},
                                       status_code=422, sid=sid)
                return _render_error(res["error"], {"message": res.get("detail") or ""},
                                     status_code=422, retry_href="/submissions/%s" % sid)
            changed += int(res.get("changed") or 0)
        if changed:
            _drop_annotation(_out_root(request), row["pid"], sid)   # 改判了 → 批注图过期
        if wants_json:
            return JSONResponse(_confirm_payload(conn, sid, changed))
        return RedirectResponse("/submissions/%s?changed=%d" % (sid, changed), status_code=303)

    return _guard(request, run, json_errors=wants_json)


# --------------------------------------------------------------------------- 状态 / 裁切图
@router.get("/submissions/{sid}/status")
def submission_status(request: Request, sid: str):
    """轮询口：pending / filled / backend / empty_batch（JSON，永不抛异常）。

    兜底（P0）：还有 pending、没有 job 在跑、且距上次识别/创建超过阈值 → 再起一次后台识别。
    这样即使上传时那次线程没起来/半路挂了，用户开着页面也能自愈，不用手点「重新识别」。
    """
    def run(conn):
        row, detail = _load_submission(conn, sid)
        if row is None:
            return JSONResponse({"error": "submission_not_found", "sid": sid}, status_code=404)
        payload = _status_payload(row, detail)
        app = request.app
        payload["running"] = _job_running(app, sid)
        payload["started"] = False
        if payload["pending"] and _auto_ocr(request):
            attempts = int(_attempts(app).get(sid) or 0)
            if (not payload["running"]
                    and _pending_age(detail, row) >= _retry_after(request)
                    and attempts < OCR_MAX_RETRIES
                    and _should_retry(detail)):
                payload["started"] = _start_ocr_job(
                    app, sid, _db_path(request), _recognizer(request), reason="status")
                payload["running"] = bool(payload["started"]) or payload["running"]
        return JSONResponse(payload)

    return _guard(request, run, json_errors=True)


def _media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".bmp": "image/bmp",
            ".tif": "image/tiff", ".tiff": "image/tiff"}.get(ext, "application/octet-stream")


@router.get("/submissions/{sid}/crop/{name}")
def submission_crop(request: Request, sid: str, name: str):
    """待确认裁切图：只认 detail 里登记过的文件名（不做任意路径读取）。"""
    def run(conn):
        row, detail = _load_submission(conn, sid)
        if row is None:
            return JSONResponse({"error": "submission_not_found", "sid": sid}, status_code=404)
        want = os.path.basename(str(name or ""))
        for u in detail.get("items") or []:
            path = str(u.get("crop_path") or "")
            if path and os.path.basename(path) == want and os.path.isfile(path):
                return FileResponse(path, media_type=_media_type(path))
        return JSONResponse({"error": "not_found", "name": want}, status_code=404)

    return _guard(request, run, json_errors=True)


# --------------------------------------------------------------------------- 批注图（t30）
#: 批注图配色说明（页面与图注用同一句，别两处写不一样）
ANNOTATED_LEGEND = "绿=判对、红=判错、橙虚线=待确认、蓝=待识别"


def _annotation_path(out_root: str, pid: str, sid: str) -> str:
    """批注图落在 app.state.out_dir 下（不写死 grade.OUT_ROOT，测试/自定义目录才对得上）。"""
    return os.path.join(out_root, str(pid), "sub_%s" % sid, "annotated.png")


def _drop_annotation(out_root: str, pid: str, sid: str) -> None:
    """改判/重识别之后批注图就过期了：直接删，下次访问重画（比时间戳比对可靠）。"""
    try:
        os.remove(_annotation_path(out_root, pid, sid))
    except OSError:
        pass


def _detail_stamp(detail: dict) -> float:
    """detail 里最近一次变化的时间（识别/确认）→ epoch 秒；没有就当 0。"""
    best = 0.0
    for key in ("recognized_at", "confirmed_at"):
        raw = str(detail.get(key) or "").strip()
        if not raw:
            continue
        try:
            best = max(best, _dt.datetime.fromisoformat(raw).timestamp())
        except (TypeError, ValueError):
            continue
    return best


def _annotation_fresh(path: str, detail: dict) -> bool:
    """缓存命中判定：文件在，且不早于 detail 里最后一次识别/确认。"""
    if not os.path.isfile(path):
        return False
    stamp = _detail_stamp(detail)
    if stamp <= 0:
        return True
    try:
        return os.path.getmtime(path) >= stamp
    except OSError:
        return False


def _annot_reason(raw: str) -> str:
    """annotate.last_error → 人话（原始串仍会出现在 explain 的「原始信息」里）。"""
    text = str(raw or "")
    if text.startswith("warp_failed"):
        return "这张照片当初就没标定成功（四角黑块没认全），没有规范图可以标注。"
    if text.startswith("image_missing"):
        return "找不到这张答题卡的原图（可能被删了，或换过电脑）。"
    if text.startswith("submission_not_found"):
        return "找不到这次判卷记录。"
    return text or "标注图生成失败，原因未知。"


@router.get("/submissions/{sid}/annotated.png")
def submission_annotated(request: Request, sid: str, refresh: int = 0):
    """批注图：命中缓存直接发文件；标注失败给 404 + 中文说明（绝不 500）。"""
    out_root = _out_root(request)

    def run(conn):
        row, detail = _load_submission(conn, sid)
        if row is None:
            return _render_error("submission_not_found", {"sid": sid}, status_code=404,
                                 retry_href="/submissions")
        target = _annotation_path(out_root, row["pid"], sid)
        if not refresh and _annotation_fresh(target, detail):
            return FileResponse(target, media_type="image/png")
        path = grade.annotate(conn, sid, target)
        if not path or not os.path.isfile(path):
            reason = str(getattr(grade.annotate, "last_error", "") or "")
            return _render_error(
                "internal_error", {"message": reason}, status_code=404,
                retry_href="/submissions/%s" % sid, retry_label="回结果页",
                extra_note="批注图没生成出来：%s（配色：%s）" % (_annot_reason(reason),
                                                              ANNOTATED_LEGEND))
        return FileResponse(path, media_type="image/png")

    return _guard(request, run)


# --------------------------------------------------------------------------- 「为什么这么判」（t30）
def _why_view(payload: dict) -> dict:
    """grade.explain_item 的 JSON → 抽屉模板用的视图（中文标签 + 裁切图名 + 墨迹行）。"""
    blanks = []
    for b in payload.get("blanks") or []:
        verdict = str(b.get("verdict") or "")
        crop = str(b.get("crop_path") or "")
        conf = b.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        # grade 的约定：0.0 = 没报置信度（pending / 人工确认），别显示成「0.00 很不准」
        has_conf = conf is not None and conf > 0 and verdict not in ("pending", "ungraded")
        blanks.append({
            "blank": b.get("blank"),
            "verdict": verdict,
            "label": VERDICT_LABELS.get(verdict, verdict or "—"),
            "tone": VERDICT_TONES.get(verdict, ""),
            "picked": str(b.get("picked") or ""),
            "expected": str(b.get("expected") or ""),
            "reason": str(b.get("reason") or ""),
            "unsure_reason": str(b.get("unsure_reason") or ""),
            "conf_text": "%.2f" % conf if has_conf else "—",
            "crop_name": os.path.basename(crop) if crop else "",
            "crop_ok": bool(crop) and os.path.isfile(crop),
        })

    picked = str(payload.get("picked") or "")
    ink_rows = []
    for opt in sorted(payload.get("ink_ratios") or {}):
        ratio = float(payload.get("ink_ratios", {}).get(opt) or 0.0)
        ink_rows.append({"opt": opt, "ratio": ratio, "pct": int(round(ratio * 100)),
                         "picked": picked == str(opt)})

    verdict = str(payload.get("verdict") or "")
    return {
        "sid": payload.get("sid"), "pid": payload.get("pid"),
        "idx": payload.get("idx"), "no": payload.get("no") or "",
        "qtype": payload.get("qtype") or "", "kind": payload.get("kind") or "",
        "page": payload.get("page"),
        "verdict": verdict,
        "label": VERDICT_LABELS.get(verdict, verdict or "—"),
        "tone": VERDICT_TONES.get(verdict, ""),
        "picked": picked, "expected": str(payload.get("expected") or ""),
        "reason": str(payload.get("reason") or ""),
        "unsure_reason": str(payload.get("unsure_reason") or ""),
        "backend": payload.get("backend") or "manual",
        "empty_batch": bool(payload.get("empty_batch")),
        "blanks": blanks,
        "ink_rows": ink_rows,
        "is_omr": str(payload.get("kind") or "") == "omr",
        "multi": len(blanks) > 1,
        "legend": ANNOTATED_LEGEND,
        "crop_ok": any(b["crop_ok"] for b in blanks),
        "annotated_href": "annotated.png",
    }


@router.get("/submissions/{sid}/item/{idx}/why")
def submission_item_why(request: Request, sid: str, idx: str,
                        fmt: str = Query("json", alias="format"), embed: int = 0):
    """单题证据：默认 JSON（grade.explain_item）；format=html → 抽屉片段（embed=1）或独立页。"""
    def run(conn):
        payload = grade.explain_item(conn, sid, idx)
        error = str(payload.get("error") or "") if isinstance(payload, dict) else "internal_error"
        if error:
            status = 404 if error in ("submission_not_found", "item_not_found") else 422
            if fmt != "html":
                return JSONResponse(payload, status_code=status)
            return _render_error(error, {"message": payload.get("detail") or "", "sid": sid,
                                         "idx": idx}, status_code=status,
                                 retry_href="/submissions/%s" % sid, retry_label="回结果页")
        if fmt == "html":
            return render("grade_why.html", page_title="为什么这么判",
                          w=_why_view(payload), standalone=not embed,
                          legend=ANNOTATED_LEGEND)
        return JSONResponse(payload)

    return _guard(request, run, json_errors=(fmt != "html"))


# --------------------------------------------------------------------------- 证据包导出（t35）
def _submission_dir(out_root: str, pid: str, sid: str) -> str:
    return os.path.join(out_root, str(pid), "sub_%s" % sid)


def _safe_zip_name(name: str) -> str:
    """zip 内条目名：只留文件名（去路径分隔符/盘符/..），防穿越。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).replace("\x00", "").strip()
    if not base or base in (".", "..") or ":" in base:
        return ""
    return base


def _under_dir(path: str, root: str) -> bool:
    """path 是否真在 root 里（realpath + commonpath，挡 ../ 与软链）。"""
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
        if os.path.commonpath([real, base]) != base:
            return False
        return os.path.isfile(real)
    except (OSError, ValueError):
        return False


def _evidence(row, detail: dict, sid: str, notes, crop_names: dict, file_name: str) -> dict:
    """evidence.json：这份卷是怎么判的，逐题逐空都留痕（人可读，也够程序复核）。"""
    units = detail.get("items") or []
    items = []
    for u in units:
        ink = {}
        for opt, ratio in (u.get("ratios") or {}).items():
            try:
                ink[str(opt)] = round(float(ratio), 4)
            except (TypeError, ValueError):
                continue
        key = (u.get("idx"), u.get("blank"))
        items.append({
            "idx": u.get("idx"), "no": u.get("no") or "",
            "qtype": u.get("qtype") or "", "kind": u.get("kind") or "",
            "blank": u.get("blank"),
            "verdict": u.get("verdict") or "",
            "picked": str(u.get("picked") or ""),
            "expected": str(u.get("expected") or ""),
            "reason": str(u.get("reason") or ""),
            "unsure_reason": str(u.get("unsure_reason") or ""),
            "confidence": u.get("confidence"),
            "ink_ratios": ink,
            "confirmed": bool(u.get("confirmed")),
            "crop": crop_names.get(key) or "",
        })
    score, full = _as_int(row["score"]), _as_int(row["full"])
    pending_items, _first = _pending_items(units)
    return {
        "version": 1,
        "file_name": file_name,
        "pid": row["pid"], "sid": sid,
        "created_at": row["created_at"] or "",
        "exported_at": _now_iso(),
        "score": score, "full": full,
        "pct": int(round(100.0 * score / full)) if full else 0,
        "pending_items": pending_items,
        "warp_ok": bool(row["warp_ok"]), "qr_ok": bool(row["qr_ok"]),
        "backend": detail.get("backend") or "manual",
        "empty_batch": bool(detail.get("empty_batch")),
        "page": detail.get("page"), "qr": detail.get("qr"),
        "recognized_at": detail.get("recognized_at") or "",
        "confirmed_at": detail.get("confirmed_at") or "",
        "item_verdicts": detail.get("item_verdicts") or {},
        "legend": ANNOTATED_LEGEND,
        "notes": list(notes or []),
        "items": items,
    }


@router.get("/submissions/{sid}/export.zip")
def submission_export(request: Request, sid: str):
    """把这次判卷的证据打包成一个 zip：annotated.png + crops/ + evidence.json。

    安全：只打包 `out/<pid>/sub_<sid>/` 里真实存在的文件，zip 条目名只留文件名。
    批注图生成失败不算错误：跳过并在 evidence.json 的 notes 里说明，绝不 500。
    """
    out_root = _out_root(request)

    def run(conn):
        row, detail = _load_submission(conn, sid)
        if row is None:
            return _render_error("submission_not_found", {"sid": sid}, status_code=404,
                                 retry_href="/submissions")
        pid = str(row["pid"])
        sub_dir = _submission_dir(out_root, pid, sid)
        file_name = "判卷_%s_%s.zip" % (pid, sid[-6:])
        notes, crop_names = [], {}

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            ann_path = _annotation_path(out_root, pid, sid)
            if not _annotation_fresh(ann_path, detail):
                grade.annotate(conn, sid, ann_path)
            if _under_dir(ann_path, sub_dir):
                zf.write(ann_path, "annotated.png")
            else:
                reason = str(getattr(grade.annotate, "last_error", "") or "")
                notes.append("批注图没生成出来（%s），本包只含裁切图与 evidence.json。"
                             % _annot_reason(reason))

            for u in detail.get("items") or []:
                crop = str(u.get("crop_path") or "")
                name = _safe_zip_name(crop)
                if not name or not _under_dir(crop, sub_dir):
                    continue
                arc = "crops/%s" % name
                if arc in crop_names.values():
                    continue
                zf.write(crop, arc)
                crop_names[(u.get("idx"), u.get("blank"))] = arc

            zf.writestr("evidence.json",
                        json.dumps(_evidence(row, detail, sid, notes, crop_names, file_name),
                                   ensure_ascii=False, indent=1))

        headers = {
            "Content-Disposition":
                "attachment; filename=\"evidence.zip\"; filename*=UTF-8''%s" % quote(file_name),
            "X-Evidence-Filename": quote(file_name),
        }
        return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)

    return _guard(request, run)


# --------------------------------------------------------------------------- 判卷记录列表（t42）
#: 用户需求：「我需要能查看判过的卷子」——历史记录的总入口。
RECORDS_LIMIT = 100          # 列表最多显示这么多条
RECORDS_SCAN = 300           # only=pending 时先在内存里筛，最多扫这么多行
SID_RE = re.compile(r"^S\d{8}-\d{6}-[0-9a-f]+$")
PID_RE = re.compile(r"^P\d{8}-\d{3}$")

RECORD_STATUS = {
    "done": ("已确认", "ok"),
    "pending": ("待确认", "warn"),
    "recognizing": ("识别中", ""),
}


def _fmt_when(text) -> str:
    """ISO 时间 → 手机上好读的「月-日 时:分」。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        return _dt.datetime.fromisoformat(raw).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw[:16].replace("T", " ")


def _record_status(pending_units: int, unsure_units: int, empty_batch: bool) -> dict:
    """已确认 / 待确认 / 识别中（三态，用户一眼能判断要不要点进去）。"""
    if pending_units == 0 and unsure_units == 0:
        label, tone = RECORD_STATUS["done"]
    elif pending_units > 0 and not empty_batch:
        label, tone = RECORD_STATUS["recognizing"]
    else:
        label, tone = RECORD_STATUS["pending"]
    return {"label": label, "tone": tone}


def _record_row(row) -> dict:
    detail = {}
    try:
        detail = json.loads(row["detail_json"] or "{}") or {}
    except (TypeError, ValueError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    units = detail.get("items") or []
    pending_units = sum(1 for u in units if u.get("verdict") == "pending")
    unsure_units = sum(1 for u in units if u.get("verdict") == "unsure")
    pending_items, _first = _pending_items(units)
    empty = bool(detail.get("empty_batch"))
    score, full = _as_int(row["score"]), _as_int(row["full"])
    status = _record_status(pending_units, unsure_units, empty)
    return {
        "sid": row["sid"], "pid": row["pid"] or "",
        "title": (row["title"] or "") if "title" in row.keys() else "",
        "created_at": row["created_at"] or "",
        "when": _fmt_when(row["created_at"]),
        "score": score, "full": full,
        "pct": int(round(100.0 * score / full)) if full else 0,
        "pending_items": pending_items,
        "pending_units": pending_units,
        "unsure_units": unsure_units,
        "empty_batch": empty,
        "status": status["label"], "tone": status["tone"],
        "needs_human": bool(pending_items or unsure_units),
        "backend": detail.get("backend") or "manual",
        "warp_ok": bool(row["warp_ok"]),
    }


def _render_records(conn, pid: str = "", q: str = "", only: str = "",
                    deleted: bool = False, status_code: int = 200):
    where, params = [], []
    if pid:
        where.append("s.pid = ?")
        params.append(pid)
    if q:
        like = "%%%s%%" % q.replace("%", "").replace("_", "")
        where.append("(s.sid LIKE ? OR s.pid LIKE ? OR COALESCE(p.title, '') LIKE ?)")
        params += [like, like, like]

    sql = ("SELECT s.sid, s.pid, s.created_at, s.score, s.full, s.warp_ok,"
           " s.detail_json, COALESCE(p.title, '') AS title"
           " FROM submissions s LEFT JOIN papers p ON p.pid = s.pid")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.created_at DESC, s.sid DESC LIMIT %d" % (
        RECORDS_SCAN if only == "pending" else RECORDS_LIMIT)

    items = [_record_row(r) for r in conn.execute(sql, params).fetchall()]
    if only == "pending":
        items = [it for it in items if it["needs_human"]][:RECORDS_LIMIT]

    try:
        total = int(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0])
    except sqlite3.Error:
        total = len(items)

    papers = []
    try:
        papers = [{"pid": r["pid"], "title": r["title"] or "", "n": int(r["n"] or 0)}
                  for r in conn.execute(
                      "SELECT s.pid, COALESCE(p.title, '') AS title, COUNT(*) AS n"
                      " FROM submissions s LEFT JOIN papers p ON p.pid = s.pid"
                      " GROUP BY s.pid ORDER BY MAX(s.created_at) DESC LIMIT 50")]
    except sqlite3.Error:
        papers = []

    notice = None
    if deleted:
        notice = {"tone": "ok", "title": "已删除这条判卷记录",
                  "detail": "记录与它的批注图/裁切图目录都已删除，删了不能恢复。"}

    return render("grade_records.html", status_code=status_code, page_title="判卷记录",
                  items=items, total=total, papers=papers, notice=notice,
                  f_pid=pid, f_q=q, f_only=only,
                  n_pending=sum(1 for it in items if it["needs_human"]),
                  limit=RECORDS_LIMIT)


@router.get("/records", response_class=HTMLResponse)
def records_page(request: Request, pid: str = "", q: str = "", only: str = "",
                 deleted: int = 0):
    """判卷记录列表（历史判过的卷子）。独立路径，不会被 /submissions/{sid} 吞掉。"""
    return _guard(request, lambda conn: _render_records(
        conn, pid=(pid or "").strip(), q=(q or "").strip(),
        only=(only or "").strip(), deleted=bool(deleted)))


def _path_within(path: str, root: str) -> bool:
    """path 是否真在 root 里（realpath + commonpath，挡 ../ 与软链）；root 本身不算。"""
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
        return real != base and os.path.commonpath([real, base]) == base
    except (OSError, ValueError):
        return False


def _render_bad_request(title: str, detail: str):
    """删除请求不合法：400 + 中文说明（走错误页模板，但不编造错误码）。"""
    e = {"title": title, "what": detail,
         "why": "判卷记录的编号是程序生成的；手改地址栏、书签过期、或数据被改过都会这样。",
         "fix": "① 回「判卷记录」列表重新点一次\n"
                "② 要删某一卷：在列表里点那一行的「删除」\n"
                "③ 别手改地址里的编号",
         "retry_label": "回判卷记录"}
    return render("grade_error.html", status_code=400, page_title=title, e=e,
                  code="bad_request", retry_href="/records", retry_label="回判卷记录",
                  extra_note="本次请求没有改动任何文件。")


@router.post("/records/{sid}/delete")
def delete_record(request: Request, sid: str):
    """删除一条判卷记录：submissions 行 + out/<pid>/sub_<sid>/ 目录。

    安全：sid/pid 先正则校验，拼出来的绝对路径必须落在 out_root 下（挡 ../ 与软链）。
    """
    out_root = _out_root(request)

    def run(conn):
        sid_text = str(sid or "")
        if not SID_RE.match(sid_text):
            return _render_bad_request("判卷编号不合法", "「%s」不是合法的判卷编号。" % sid_text)
        row = conn.execute("SELECT sid, pid FROM submissions WHERE sid=?",
                           (sid_text,)).fetchone()
        if row is None:
            return _render_error("submission_not_found", {"sid": sid_text}, status_code=404,
                                 retry_href="/records", retry_label="回判卷记录")
        pid = str(row["pid"] or "")
        if not PID_RE.match(pid):
            return _render_bad_request("卷号不合法，拒绝删除",
                                       "这条记录挂的卷号「%s」不是合法卷号，为安全起见不删文件。" % pid)
        target = _submission_dir(out_root, pid, sid_text)
        if not _path_within(target, out_root):
            return _render_bad_request("路径越界，拒绝删除",
                                       "这条记录的产物目录不在 app/out 下：%s" % target)
        if os.path.isdir(target):
            try:
                shutil.rmtree(target)
            except OSError as exc:
                _log("删除目录失败 %s：%s: %s" % (target, type(exc).__name__, exc))
        conn.execute("DELETE FROM submissions WHERE sid=?", (sid_text,))
        conn.commit()
        _log("删除判卷记录 %s（pid=%s）" % (sid_text, pid))
        return RedirectResponse("/records?deleted=1", status_code=302)

    return _guard(request, run)
