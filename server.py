# -*- coding: utf-8 -*-
"""M5 · 本地 Web 骨架（FastAPI + jinja2 + 原生 JS；固定 127.0.0.1:8710）。

本文件当前只做「骨架 + 只读 DB」：

  GET  /          组卷表单：章节/知识点选择器数据来自 kps 表；提交按钮先禁用（t11 接线）
  GET  /health    自检页（HTML）
  GET  /healthz   自检数据（JSON；诊断口永远 200，健康与否看 ok / status 字段）
  GET  /static/*  样式与脚本（无框架、无构建工具）

刻意不 import select / paper / card / grade / ocr：那些由别的任务接线。
DB 一律以 `mode=ro` 只读 URI 打开，缺少库文件时绝不顺手建库。
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import pathlib
import platform
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from typing import Iterable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import errors as errs  # 错误文案字典（纯数据，无副作用）

import paths as app_paths

BASE_DIR = app_paths.APP_DIR
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_CONFIG = app_paths.config_path()
DEFAULT_DB = app_paths.resolve_db(DEFAULT_CONFIG)
OUT_DIR = app_paths.resolve_out_dir(DEFAULT_CONFIG)
#: 题库目录（原始 docx）：环境变量 > config.paths.qbank > 老路径 E:\OCR（存在才用）> <程序>/qbank
QBDIR = app_paths.resolve_qbank(DEFAULT_CONFIG, legacy=app_paths.LEGACY_QBANK)

APP_BRAND = "数学题库工作台"
#: seed：留空时在这个范围里随机（表单 max=999999 对齐）
SEED_MAX = 1000000

#: 卷面书写区：题型顺序与段数上限跟 picker 保持一致（默认值一律取 picker.DEFAULT_SPACE）
SPACE_QTYPES = ("选择", "填空", "解答", "判断", "其他")
MAX_SPACE_PARAS = 40

#: 版面：省纸（compact，页边距/行距收紧）与宽裕（normal，原版面）
LAYOUT_COMPACT = "compact"
LAYOUT_NORMAL = "normal"

APP_SUBTITLE = "沪科版七年级上 · 本地运行"
APP_TITLE = "题库组卷 · 答题卡生成 · 拍照判卷 · 错题训练"
APP_VERSION = "0.1.0"

HOST = "127.0.0.1"
PORT = 8710

STARTED_AT = time.time()

QTYPE_DEFAULTS = (("选择", "n_choice", 10), ("填空", "n_fill", 8), ("解答", "n_solution", 6))

#: 顶部导航。enabled=False 的由后续任务接线（t10/t11），现在只作占位，避免点进 404。
NAV = (
    {"key": "compose", "label": "组卷", "href": "/", "enabled": True, "soon_hint": ""},
    {"key": "papers", "label": "我的卷子", "href": "/papers", "enabled": True, "soon_hint": ""},
    {"key": "grade", "label": "判卷", "href": "/submissions", "enabled": True, "soon_hint": ""},
    {"key": "train", "label": "训练", "href": "/train", "enabled": True, "soon_hint": ""},
    {"key": "settings", "label": "设置", "href": "/settings", "enabled": True, "soon_hint": ""},
    {"key": "guide", "label": "使用指南", "href": "/guide", "enabled": True, "soon_hint": ""},
    {"key": "health", "label": "自检", "href": "/health", "enabled": True, "soon_hint": ""},
)

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR, encoding="utf-8"),
    autoescape=select_autoescape(("html", "xml")),
    auto_reload=True,
)


def _thousands(value) -> str:
    """把整数写成 22,898；非整数原样输出，模板里 {% ... | num %} 用。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "" if value is None else str(value)
    return f"{int(value):,}"


_env.filters["num"] = _thousands
_env.globals.update(brand=APP_BRAND, brand_sub=APP_SUBTITLE,
                    app_title=APP_TITLE, app_version=APP_VERSION)


class DbMissing(Exception):
    """库文件不存在（不是 sqlite 错误，单独区分是为了给出「先建库」的提示）。"""

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


# --------------------------------------------------------------------------- 渲染

def render(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    """渲染模板。统一注入导航/横幅上下文，页面只关心自己的内容。"""
    ctx.setdefault("nav", NAV)
    ctx.setdefault("nav_active", None)
    ctx.setdefault("page_title", APP_BRAND)
    ctx.setdefault("error", None)
    ctx.setdefault("warnings", [])
    ctx.setdefault("links", [])
    html = _env.get_template(name).render(**ctx)
    return HTMLResponse(html, status_code=status_code)


def _banner(title: str, detail: str | None = None, hints: Iterable[str] | None = None,
            code: str | None = None) -> dict:
    return {"title": title, "detail": detail, "code": code,
            "hints": [h for h in (hints or []) if h]}


def _db_error(exc: BaseException, db_path: str) -> dict:
    """把 DB 侧的异常翻成人话 + 「怎么办」，绝不把 traceback 丢给用户。"""
    if isinstance(exc, DbMissing):
        return _banner(
            "找不到题库数据库",
            f"期望路径：{db_path}（文件不存在）",
            ["先建库：在 E:\\OCR 目录下执行 python app\\build_db.py（需要原始 22,898 份 docx，约几分钟）",
             "确认路径没写错；也可以用环境变量 MATH_APP_DB 指向另一份 app.db",
             "建库成功后刷新本页"],
            code="db_missing",
        )
    message = str(exc) or exc.__class__.__name__
    if isinstance(exc, sqlite3.OperationalError) and "no such table" in message:
        return _banner(
            "题库结构不完整",
            f"{db_path} 里缺少表：{message}",
            ["库文件是旧的或建了一半：重新执行 python app\\build_db.py",
             "如果这份 app.db 是手工拷来的，确认它来自同一个版本的 build_db.py",
             "备份后删掉 app/data/app.db 再重建"],
            code="db_schema",
        )
    if isinstance(exc, sqlite3.OperationalError) and "unable to open" in message:
        return _banner(
            "打不开题库数据库",
            f"{db_path}：{message}",
            ["确认文件没有被别的程序独占（关掉正在跑的旧服务/DB 工具）",
             "确认当前用户有读权限",
             "确认 app.db 与 app.db-wal 在同一个目录"],
            code="db_open",
        )
    return _banner(
        "读取题库失败",
        f"{exc.__class__.__name__}: {message}",
        ["先在自检页看数据库、目录两项的状态：/health",
         "库文件可能损坏：备份后执行 python app\\build_db.py 重建"],
        code="db_error",
    )


# --------------------------------------------------------------------------- 只读 DB

def _db_uri(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri() + "?mode=ro"


def _open_ro(db_path: str) -> sqlite3.Connection:
    if not os.path.isfile(db_path):
        raise DbMissing(os.path.abspath(db_path))
    conn = sqlite3.connect(_db_uri(db_path), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_kps(conn: sqlite3.Connection) -> list[dict]:
    """章节/知识点选择器的数据源（契约指定的那条 SQL）。"""
    rows = conn.execute(
        "SELECT chapter, kp_id, kp, n_questions FROM kps ORDER BY chapter, kp_id"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "chapter": r["chapter"] or "未分章",
            "kp_id": r["kp_id"] or "",
            "kp": r["kp"] or "",
            "n_questions": int(r["n_questions"] or 0),
        })
    return out


def group_chapters(kps: list[dict]) -> list[dict]:
    order: list[str] = []
    bucket: dict[str, dict] = {}
    for k in kps:
        chapter = k["chapter"]
        if chapter not in bucket:
            bucket[chapter] = {"chapter": chapter, "kps": [], "n_questions": 0}
            order.append(chapter)
        item = bucket[chapter]
        item["kps"].append(k)
        item["n_questions"] += k["n_questions"]
    return [bucket[c] for c in order]


def read_qtype_stats(conn: sqlite3.Connection) -> list[dict]:
    """配比输入框旁边的「库里有多少题/有多少有答案」提示。"""
    sql = """
        SELECT qtype,
               COUNT(*) AS n,
               SUM(CASE WHEN TRIM(COALESCE(answer_raw, '')) <> '' THEN 1 ELSE 0 END) AS n_answer,
               SUM(CASE WHEN COALESCE(is_empty, 0) <> 0 THEN 1 ELSE 0 END) AS n_empty
        FROM questions GROUP BY qtype
    """
    rows = {r["qtype"]: r for r in conn.execute(sql)}
    stats = []
    for qtype, field, default in QTYPE_DEFAULTS:
        r = rows.get(qtype)
        stats.append({
            "qtype": qtype, "field": field, "default": default,
            "n": int(r["n"] or 0) if r else 0,
            "n_answer": int(r["n_answer"] or 0) if r else 0,
            "n_empty": int(r["n_empty"] or 0) if r else 0,
        })
    return stats


def read_db_summary(db_path: str) -> dict:
    """DB 体检：路径/大小/各表行数/缺表。只读，不建库。"""
    path = os.path.abspath(db_path)
    summary = {
        "path": path, "exists": os.path.isfile(path), "size_mb": None,
        "counts": {}, "questions": 0, "kps": 0, "error": None, "tables_missing": [],
    }
    if not summary["exists"]:
        summary["error"] = "数据库文件不存在"
        return summary
    try:
        summary["size_mb"] = round(os.path.getsize(path) / 1048576.0, 1)
    except OSError:
        pass
    try:
        conn = _open_ro(path)
    except (DbMissing, sqlite3.Error) as exc:
        summary["error"] = f"打不开：{exc.__class__.__name__}: {exc}"
        return summary
    try:
        for table in ("questions", "kps", "families", "papers", "submissions"):
            try:
                summary["counts"][table] = int(
                    conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
            except sqlite3.Error as exc:
                summary["counts"][table] = None
                summary["tables_missing"].append(f"{table}（{exc}）")
        summary["questions"] = summary["counts"].get("questions") or 0
        summary["kps"] = summary["counts"].get("kps") or 0
    finally:
        conn.close()
    if summary["tables_missing"]:
        summary["error"] = "缺表：" + "；".join(summary["tables_missing"][:3])
    return summary


# --------------------------------------------------------------------------- 环境探测

def _winword_candidates() -> list[str]:
    """列出可能的 WINWORD.EXE 路径：PATH → App Paths 注册表 → 常见安装目录。"""
    found: list[str] = []
    which = shutil.which("WINWORD.EXE")
    if which:
        found.append(which)
    if sys.platform == "win32":
        try:
            import winreg
            app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"
            keys = [
                (winreg.HKEY_LOCAL_MACHINE, app_paths),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"),
                (winreg.HKEY_CURRENT_USER, app_paths),
            ]
            for root, sub in keys:
                try:
                    with winreg.OpenKey(root, sub) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                    if value:
                        found.append(os.path.expandvars(str(value).strip('"')))
                except OSError:
                    continue
        except ImportError:  # 非 Windows / 剪裁过的 stdlib
            pass
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not base:
                continue
            for office in ("Office16", "Office15", "Office14"):
                found.append(os.path.join(base, "Microsoft Office", "root", office, "WINWORD.EXE"))
                found.append(os.path.join(base, "Microsoft Office", office, "WINWORD.EXE"))
    return found


def _find_winword(candidates: Iterable[str] | None = None) -> str | None:
    """只做路径探测，绝不启动 Word。"""
    for path in (candidates if candidates is not None else _winword_candidates()):
        if not path:
            continue
        try:
            if os.path.isfile(path) and os.path.basename(path).lower() == "winword.exe":
                return path
        except OSError:
            continue
    return None


def probe_word(find=None) -> dict:
    """Word 是否可用：pywin32 能 import + WINWORD.EXE 存在（不启动、不弹窗）。"""
    finder = find or _find_winword
    try:
        import win32com.client  # noqa: F401  只 import，不 Dispatch
        com_ok, com_error = True, None
    except Exception as exc:  # noqa: BLE001 - 任何 import 失败都只是「不可用」
        com_ok, com_error = False, f"{exc.__class__.__name__}: {exc}"
    path = finder()
    if com_ok and path:
        return {"available": True, "path": path, "com": True, "reason": None, "hints": []}
    if not com_ok:
        reason = "pywin32 不可用（import win32com 失败）：" + (com_error or "")
        hints = ["安装 pywin32：python -m pip install pywin32",
                 "装不了也不影响出卷：打印走 PDF 即可"]
    else:
        reason = "找不到 WINWORD.EXE（本机没装 Microsoft Word）"
        hints = ["安装 Microsoft Word 后才能用 docx 原生排版/转 PDF",
                 "先不装也行：卷面可先用 PDF 输出，Word 相关按钮会提示不可用"]
    return {"available": False, "path": path, "com": com_ok, "reason": reason, "hints": hints}


def probe_writable(path: str, create: bool = True) -> dict:
    """能否在目录里建/删文件（真写一个临时文件，比 os.access 可靠）。"""
    target = os.path.abspath(path)
    info = {"path": target, "exists": os.path.isdir(target), "writable": False, "error": None}
    tmp = None
    fd = None
    try:
        if not info["exists"]:
            if not create:
                info["error"] = "目录不存在"
                return info
            os.makedirs(target, exist_ok=True)
            info["exists"] = True
        fd, tmp = tempfile.mkstemp(prefix=".write_test_", suffix=".tmp", dir=target)
        os.write(fd, b"ok")
        info["writable"] = True
    except OSError as exc:
        info["error"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return info


def load_ocr_config(config_path: str | None = None) -> dict:
    """读 config.json 的 ocr 段；环境变量优先。token 只报「有没有」，绝不回显。"""
    path = os.path.abspath(config_path or os.environ.get("MATH_APP_CONFIG") or DEFAULT_CONFIG)
    out = {"config_path": path, "exists": os.path.isfile(path), "backend": None,
           "source": None, "model": None, "timeout_s": None, "poll_s": None,
           "token_present": False, "error": None}
    cfg: dict = {}
    if out["exists"]:
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
        except (OSError, json.JSONDecodeError) as exc:
            out["error"] = f"config.json 解析失败：{exc.__class__.__name__}: {exc}"
    else:
        out["error"] = f"config.json 不存在：{path}"
    ocr = cfg.get("ocr") if isinstance(cfg.get("ocr"), dict) else {}
    env_backend = os.environ.get("OCR_BACKEND")
    out["backend"] = env_backend or ocr.get("backend")
    out["source"] = "env" if env_backend else ("config.json" if out["backend"] else None)
    out["model"] = os.environ.get("OCR_MODEL") or (ocr.get("paddle_vl") or {}).get("model")
    out["timeout_s"] = ocr.get("timeout_s")
    out["poll_s"] = ocr.get("poll_s")
    tokens = [
        os.environ.get("OCR_TOKEN"),
        (ocr.get("paddle_vl") or {}).get("token"),
        (ocr.get("vlm") or {}).get("key"),
    ]
    out["token_present"] = any(str(t or "").strip() for t in tokens)
    # 设置页/自检页要显示「已配置 · 尾号 abcd」：只暴露尾号，绝不回显明文
    def _tail(value) -> str:
        text = str(value or "").strip()
        return text[-4:] if len(text) >= 4 else ("" if not text else "***")

    pv_token = os.environ.get("OCR_TOKEN") or (ocr.get("paddle_vl") or {}).get("token")
    vlm_key = os.environ.get("OCR_KEY") or (ocr.get("vlm") or {}).get("key")
    out["paddle_vl"] = {"token_present": bool(str(pv_token or "").strip()),
                        "token_tail": _tail(pv_token),
                        "model": (ocr.get("paddle_vl") or {}).get("model"),
                        "job_url": (ocr.get("paddle_vl") or {}).get("job_url")}
    out["vlm"] = {"key_present": bool(str(vlm_key or "").strip()),
                  "key_tail": _tail(vlm_key),
                  "base_url": (ocr.get("vlm") or {}).get("base_url"),
                  "model": (ocr.get("vlm") or {}).get("model")}
    out["backend_label"] = {
        "paddle_vl": "PaddleOCR-VL 云端",
        "vlm": "视觉模型（OpenAI 兼容）",
        "local": "本机离线 OCR",
        "manual": "全人工确认（不联网）",
    }.get(str(out["backend"] or ""), str(out["backend"] or "未设置"))
    try:
        import ocr as _ocr
        out["local_ready"] = bool(getattr(_ocr, "_HAS_WINOCR", None)
                                  or getattr(_ocr, "LOCAL_OK", None))
    except Exception:  # noqa: BLE001
        out["local_ready"] = None
    return out


LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def is_lan_host(host: str) -> bool:
    """监听地址是不是「对外开放」（0.0.0.0 / 本机局域网 IP）；loopback 不算。"""
    return str(host or "").strip().lower() not in LOOPBACK_HOSTS


def _lan_ip_score(ip: str) -> int:
    """给候选地址打分：家里的私有网段优先，虚拟网卡（198.18/198.19、169.254）不要。"""
    if not ip:
        return 0
    if ip.startswith("198.18.") or ip.startswith("198.19.") or ip.startswith("169.254."):
        return 0
    if ip.startswith("127.") or ip == "0.0.0.0":
        return 0
    if ip.startswith("192.168."):
        return 100
    if ip.startswith("10."):
        return 90
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
        return 80
    return 30          # 其它公网/非常规网段：兜底，别当首选


def lan_ip() -> str | None:
    """探测本机在局域网里的 IPv4；拿不到（或只有虚拟网卡）返回 None。

    UDP connect 不发包，只用来问路由表「走哪张网卡出」：先问同网段网关（拿到的是
    真实家用网卡地址），再问公网。虚拟网卡（VPN / WSL / 198.18.x 段）会被打分淘汰。
    只在真正需要时调用（首页 LAN 区块 / /healthz / /qr.png），import 阶段不碰网络。
    """
    import socket as _socket

    candidates = []
    for target in (("192.168.0.1", 80), ("192.168.1.1", 80), ("10.0.0.1", 80),
                   ("192.168.0.140", 80), ("8.8.8.8", 80)):
        sock = None
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            sock.settimeout(0.25)
            sock.connect(target)
            ip = sock.getsockname()[0]
            if ip and _lan_ip_score(ip):
                candidates.append(ip)
        except OSError:
            continue
        finally:
            if sock is not None:
                sock.close()
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ip = info[4][0]
            if _lan_ip_score(ip):
                candidates.append(ip)
    except OSError:
        pass
    if not candidates:
        return None
    # 同分时优先出现早的（网关探测拿到的更可信）
    best, best_score = None, -1
    for ip in candidates:
        score = _lan_ip_score(ip)
        if score > best_score:
            best, best_score = ip, score
    return best


def load_server_config(config_path: str | None = None) -> dict:
    """host/port/pin 从 config.json 的 server 段读；环境变量优先；默认 127.0.0.1:8710。

    LAN 判定：`MATHAPP_LAN=1` 或 host 不是 loopback —— 两种情况都算「对外开放」，
    此时必须启用访问码（pin），否则同一 WiFi 下谁都能翻卷子和答案。
    """
    path = os.path.abspath(config_path or os.environ.get("MATH_APP_CONFIG") or DEFAULT_CONFIG)
    host, port, pin, error = HOST, PORT, "", None
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
            server = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
            host = str(server.get("host") or HOST)
            port = int(server.get("port") or PORT)
            pin = str(server.get("pin") or "").strip()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            error = f"{exc.__class__.__name__}: {exc}"
    host = os.environ.get("MATH_APP_HOST") or host
    try:
        port = int(os.environ.get("MATH_APP_PORT") or port)
    except ValueError:
        pass
    lan_env = str(os.environ.get("MATHAPP_LAN") or "").strip().lower() in ("1", "true", "yes", "on")
    lan = lan_env or is_lan_host(host)
    return {"host": host, "port": port, "url": f"http://{host}:{port}/",
            "lan": lan, "pin": pin, "config_path": path, "error": error}


def lan_url(port: int | None = None, pin: str = "") -> str | None:
    """局域网访问地址（手机用）；探测不到 IP 就返回 None。pin 只拼给二维码用。"""
    ip = lan_ip()
    if not ip:
        return None
    base = "http://%s:%d/" % (ip, int(port or load_server_config()["port"]))
    return base + (("?pin=" + pin) if pin else "")


#: 内置测试图上的算式：既给云端"文档里的一行字"的版式，也方便用户核对识别结果
SELFTEST_EXPR = "40 + 2 = 42"
SELFTEST_CAPTION = "识别测试 · 请忽略这一行小字"


def make_selftest_image(target: str, size=(900, 260)) -> str:
    """生成「像文档里一行字」的测试图。

    之前用 640×160 的小图，PaddleOCR-VL 会把整张当图片块、解析不出 block，
    于是「云端明明是通的」也被报成失败。这里改成正常页面尺寸 + 96px 黑体居中，
    并加一行小字，识别结果可以人工核对。
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = size
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    def _font(px: int):
        for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "arialbd.ttf", "arial.ttf"):
            path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
            if os.path.isfile(path):
                try:
                    return ImageFont.truetype(path, px)
                except OSError:
                    continue
        return ImageFont.load_default()

    big, small = _font(96), _font(28)
    box = draw.textbbox((0, 0), SELFTEST_EXPR, font=big)
    draw.text(((width - (box[2] - box[0])) / 2, (height - (box[3] - box[1])) / 2 - 18),
              SELFTEST_EXPR, fill=(10, 10, 10), font=big)
    cap = draw.textbbox((0, 0), SELFTEST_CAPTION, font=small)
    draw.text(((width - (cap[2] - cap[0])) / 2, height - 52),
              SELFTEST_CAPTION, fill=(120, 120, 120), font=small)
    img.save(target)
    return target


def classify_ocr_probe(*, text: str, backend: str, error: str = "",
                       elapsed: float = 0.0) -> dict:
    """把一次「测试连接」的结果翻译成人话，**别把「连通但没解析出 block」说成 Key 坏了**。

    分四种：
      ok                    真的认出了字
      connected_no_content  云端通了、但这张图没解析出内容（图太简单 / 没有文本行）
      auth_or_quota         401 / 鉴权失败 / 额度不足 —— 这才该查 Key
      network               连不上、超时、DNS
    """
    text = (text or "").strip()
    error = (error or "").strip()
    backend = (backend or "").strip()
    lowered = error.lower()
    if text:
        return {"ok": True, "kind": "ok", "message": "连接正常，认出了：%s" % text[:80]}
    auth_keys = ("401", "403", "unauthor", "invalid token", "invalid api key", "forbidden",
                 "quota", "额度", "余额", "鉴权", "认证", "token 无效", "key 无效")
    net_keys = ("timeout", "timed out", "超时", "connection", "connect", "dns", "resolve",
                "network", "unreachable", "ssl", "proxy")
    if any(word in lowered for word in auth_keys):
        return {"ok": False, "kind": "auth_or_quota",
                "message": "鉴权/额度有问题：%s —— 这只可能是 Key 或额度的问题，去查一下。" % error[:120]}
    if any(word in lowered for word in net_keys):
        return {"ok": False, "kind": "network",
                "message": "没连上云端：%s —— 先查网络 / 代理 / 防火墙，不是 Key 的问题。" % error[:120]}
    if "->manual" in backend or "没有可用" in error or "no block" in lowered \
            or "empty" in lowered or not error:
        return {"ok": False, "kind": "connected_no_content",
                "message": "已连通云端，但这张测试图没解析出内容（可能是图像过于简单）；"
                           "这不代表 Key 有问题 —— 建议用一张真实答题卡裁切图再测。"}
    return {"ok": False, "kind": "error",
            "message": "测试没跑完：%s（先看上面的错误，再决定要不要查 Key）" % error[:120]}


def lan_startup_text(config_path: str | None = None) -> str:
    """start.bat 用：打印本机 / 局域网两个地址 + 防火墙指引（LAN 模式下）。"""
    cfg = load_server_config(config_path)
    port = cfg["port"]
    lines = ["  本机：  http://127.0.0.1:%d/" % port]
    if cfg["lan"]:
        ip = lan_ip()
        lines.append("  局域网：%s" % (("http://%s:%d/" % (ip, port)) if ip
                                    else "（探测不到局域网 IP，命令行敲 ipconfig 看 IPv4）"))
        lines.append("  访问码：启动后看本窗口（也写在 config.json 的 server.pin；本机 127.0.0.1 不用输）")
        lines.append("  防火墙：首次启动若弹 Windows 防火墙提示，勾『专用网络』→『允许访问』；")
        lines.append("          没弹窗就手动运行（管理员 CMD）：")
        lines.append('          netsh advfirewall firewall add rule name="数学题库8710" '
                     'dir=in action=allow protocol=TCP localport=%d' % port)
        lines.append("  提醒：只在自己家的 WiFi 上开 LAN 模式；公用 WiFi 别开。")
    return "\n".join(lines)


def ensure_lan_pin(config_path: str | None = None, log=print) -> str:
    """LAN 模式下保证有一个 4 位访问码：config 里没有就生成并写回 config.json。

    写回只动 server.pin 这一个键，其余内容（含 OCR token）原样保留；文件不可写时
    仍返回内存里的 pin（本次会话有效），并给出提示。
    """
    import secrets

    cfg = load_server_config(config_path)
    if not cfg["lan"]:
        return ""
    if cfg["pin"] and len(cfg["pin"]) == 4 and cfg["pin"].isdigit():
        return cfg["pin"]
    pin = "%04d" % secrets.randbelow(10000)
    path = cfg["config_path"]
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh) or {}
        if not isinstance(raw.get("server"), dict):
            raw["server"] = {}
        raw["server"]["pin"] = pin
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        log("  [局域网] 已生成 4 位访问码：%s（写进 config.json 的 server.pin）" % pin)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log("  [局域网] 访问码 %s（写 config.json 失败：%s，本次会话有效）" % (pin, exc))
    return pin


# --------------------------------------------------------------------------- 自检

def _check(key: str, label: str, status: str, detail: str,
           hints: Iterable[str] | None = None) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail,
            "hints": [h for h in (hints or []) if h]}


def local_ocr_support() -> dict:
    """本机离线 OCR（local 后端）到底能不能用：要 Windows + winsdk。

    grading-engineer 实测：本机 Python 3.14 装不上 winsdk（编译失败），
    `Recognizer(backend="local")` 会静默降级成 local->manual —— 设置页必须如实说，不能装能用。
    """
    import platform as _platform

    is_windows = sys.platform.startswith("win") or _platform.system() == "Windows"
    winsdk_ok, error = False, ""
    if is_windows:
        try:
            import winsdk  # noqa: F401
            from winsdk.windows.media.ocr import OcrEngine  # noqa: F401
            winsdk_ok = True
        except Exception as exc:  # noqa: BLE001 - ImportError / OSError 都算不可用
            error = "%s: %s" % (type(exc).__name__, exc)
    else:
        error = "不是 Windows 系统"
    available = bool(is_windows and winsdk_ok)
    if available:
        detail = "可用：winsdk 已装，走 Windows 内置 OCR（不联网）"
    elif not is_windows:
        detail = "本机不支持：local 走 Windows 内置 OCR，当前系统不是 Windows"
    else:
        detail = ("本机不支持：缺 winsdk（Windows 内置 OCR 的 Python 绑定）。"
                  "pip install winsdk 在 Python 3.14 上常编译失败 —— 建议改用 paddle_vl / vlm / manual")
    return {"available": available, "windows": is_windows, "winsdk": winsdk_ok,
            "detail": detail, "error": error,
            "python": _platform.python_version()}


def probe_pdf_backend() -> dict:
    """PDF 后端探测：优先用 render.probe_backend()（Word / LibreOffice / 都没有）。

    render 模块缺失或探测抛错时退回旧的「只探 Word」，保证 /health 不会 500。
    """
    try:
        render = _load_module("render")
        probe = getattr(render, "probe_backend", None)
        if callable(probe):
            data = probe()
            if isinstance(data, dict) and "chosen" in data:
                return data
    except Exception:  # noqa: BLE001
        pass
    word = probe_word()
    ok = bool(word.get("available"))
    return {"word": ok, "libreoffice": False,
            "chosen": "word" if ok else None,
            "detail": (word.get("path") if ok else word.get("reason")) or "",
            "word_path": word.get("path") if ok else None,
            "soffice_path": None, "last_used": None,
            "fallback": True}


def pdf_backend_note(probe: dict) -> str:
    """给页面用的一句话：LibreOffice 时说明「版面可能与 Word 有差异」。"""
    chosen = (probe or {}).get("chosen")
    if chosen == "libreoffice":
        return ("当前用 LibreOffice 渲染 PDF，公式/字体/页脚可能与 Word 略有细微差异，"
                "打印前建议先看 PDF。")
    if chosen == "word":
        return ""
    return "没有可用的 PDF 后端：装 Word 2016+ 或 LibreOffice 才能出卷面 PDF。"


def collect_health(db_path: str, config_path: str | None = None,
                   out_root: str | None = None, grade_ok: bool | None = None) -> dict:
    out_path = os.path.abspath(out_root or OUT_DIR)
    db = read_db_summary(db_path)
    word = probe_word()
    ocr = load_ocr_config(config_path)
    data_dir = probe_writable(os.path.dirname(os.path.abspath(db_path)))
    out_dir = probe_writable(out_path)
    server_cfg = load_server_config(config_path)

    checks: list[dict] = []

    if not db["exists"]:
        checks.append(_check("db", "题库数据库", "fail",
                             f"{db['path']} 不存在",
                             ["先建库：在 E:\\OCR 目录下执行 python app\\build_db.py",
                              "或设环境变量 MATH_APP_DB 指向已有的 app.db",
                              "建库需要原始 22,898 份 docx，只读，不会改动它们"]))
    elif db["error"]:
        checks.append(_check("db", "题库数据库", "fail", db["error"],
                             ["库文件损坏或结构不全：备份后执行 python app\\build_db.py 重建",
                              "确认没有别的程序正独占这个库（关掉旧服务/DB 工具）"]))
    elif db["questions"] == 0:
        checks.append(_check("db", "题库数据库", "warn",
                             f"{db['path']} 结构完整但 0 道题",
                             ["执行 python app\\build_db.py 灌入题库"]))
    else:
        checks.append(_check(
            "db", "题库数据库", "ok",
            f"{db['questions']:,} 题 · {db['kps']} 个知识点 · "
            f"{db['counts'].get('families') or 0:,} 个题族 · {db['size_mb']} MB"))

    if grade_ok is not None:
        checks.append(_check(
            "grade", "判卷页（/submissions）",
            "ok" if grade_ok else "fail",
            "grade_routes 已挂载：上传 / 结果 / 重新识别 / 人工确认" if grade_ok
            else "grade_routes 没挂上：/submissions* 打不开",
            [] if grade_ok else ["确认 app/grade_routes.py 存在且 import 不报错",
                                 "看服务启动时的 traceback（本窗口往上翻）"]))

    checks.append(_check(
        "runtime", "组卷/判卷记录", "ok",
        f"卷 {db['counts'].get('papers') or 0} 份 · 提交 {db['counts'].get('submissions') or 0} 次"))

    pdf_backend = probe_pdf_backend()
    _chosen = pdf_backend.get("chosen")
    _pdf_detail = str(pdf_backend.get("detail") or "")
    if _chosen == "word":
        checks.append(_check("pdf", "PDF 后端（docx→PDF）", "ok", _pdf_detail or "Word"))
    elif _chosen == "libreoffice":
        checks.append(_check(
            "pdf", "PDF 后端（docx→PDF）", "warn",
            "LibreOffice 渲染（版面可能与 Word 略有差异）· " + (_pdf_detail or "soffice"),
            ["没有 Word 时会用 LibreOffice；公式/字体/页脚可能与 Word 略有差异，打印前先看 PDF",
             "想完全一致：装 Microsoft Word 2016+，程序会自动优先用 Word",
             "soffice 路径特殊时可用环境变量 LIBREOFFICE_PATH 指定"]))
    else:
        checks.append(_check(
            "pdf", "PDF 后端（docx→PDF）", "fail",
            _pdf_detail or "Word 与 LibreOffice 都没检测到",
            ["装 Microsoft Word 2016+（推荐，版面最稳）",
             "或装 LibreOffice（macOS/Linux 只装这个就行）",
             "soffice 不在 PATH 时用 LIBREOFFICE_PATH 指定路径",
             "只想要答题卡、不需要卷面 PDF：可以先这样用（答题卡仍可打印）"]))
    if not word["available"]:
        checks.append(_check("word", "Microsoft Word（docx 排版/转 PDF）", "warn",
                             word["reason"], word.get("hints")))

    if not ocr["backend"]:
        checks.append(_check("ocr", "OCR 后端（填空/解答识别）", "fail",
                             ocr["error"] or "没有配置 ocr.backend",
                             ["在 app/config.json 的 ocr.backend 里选 manual / paddle_vl / vlm",
                              "或设环境变量 OCR_BACKEND=manual 先把流程跑通"]))
    elif ocr["backend"] == "paddle_vl" and not ocr["token_present"]:
        checks.append(_check("ocr", "OCR 后端（填空/解答识别）", "fail",
                             "paddle_vl 缺 token，识别会整批降级",
                             ["把 PaddleOCR-VL token 写进 config.json 的 ocr.paddle_vl.token",
                              "或设环境变量 OCR_TOKEN",
                              "暂时没有 token：把 ocr.backend 改成 manual，走人工确认"]))
    elif ocr["backend"] == "manual":
        checks.append(_check("ocr", "OCR 后端（填空/解答识别）", "ok",
                             "manual：填空/解答裁切图留人工确认（选择题走 OMR，不受影响）"))
    else:
        checks.append(_check("ocr", "OCR 后端（填空/解答识别）", "ok",
                             f"{ocr['backend']} · 模型 {ocr.get('model') or '-'} · token 已配置"))

    for key, label, info in (("data_dir", "数据目录", data_dir), ("out_dir", "产物目录", out_dir)):
        if info["writable"]:
            checks.append(_check(key, label, "ok", f"{info['path']} 可写"))
        else:
            checks.append(_check(key, label, "fail", f"{info['path']} 不可写：{info['error']}",
                                 ["确认当前用户对 app\\data 与 app\\out 有写权限",
                                  "关掉占用这些目录的 Word/Excel 窗口后重试"]))

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    status = "bad" if fails else ("degraded" if warns else "ok")
    hints: list[str] = []
    for check in fails + warns:
        for hint in check.get("hints") or []:
            if hint not in hints:
                hints.append(hint)

    # OCR 行补「后端 · token 尾号」，设置页/自检页都靠它核对（不回显明文）
    for _row in checks:
        if _row.get("key") != "ocr":
            continue
        _detail = _row.get("detail") or ""
        _label = ocr.get("backend_label") or ocr.get("backend") or "未设置"
        _tail = ""
        if ocr.get("backend") == "paddle_vl" and (ocr.get("paddle_vl") or {}).get("token_present"):
            _tail = " · token 已配置（尾号 %s）" % (ocr["paddle_vl"].get("token_tail") or "****")
        elif ocr.get("backend") == "vlm" and (ocr.get("vlm") or {}).get("key_present"):
            _tail = " · key 已配置（尾号 %s）" % (ocr["vlm"].get("key_tail") or "****")
        _row["detail"] = "后端：%s%s。%s" % (_label, _tail, _detail)

    return {
        "ok": not fails,
        "status": status,
        "generated_at": _now_iso(),
        "app": {
            "name": APP_TITLE,
            "version": APP_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "started_at": _dt.datetime.fromtimestamp(STARTED_AT).astimezone().isoformat(timespec="seconds"),
            "uptime_s": round(time.time() - STARTED_AT, 1),
            "base_dir": BASE_DIR,
        },
        "server": server_cfg,
        "db": db,
        "word": word,
        "pdf_backend": pdf_backend,
        "ocr": ocr,
        "data_dir": data_dir,
        "out_dir": out_dir,
        "out_dir_mb": _dir_size_mb(out_path),
        "papers_count": int(db["counts"].get("papers") or 0),
        "papers_with_submissions_mb": _papers_with_submissions_mb(out_path, db_path),
        "checks": checks,
        "hints": hints,
    }


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- 组卷参数 / 落盘

def _to_int(value, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(text) if text else default
    except (TypeError, ValueError):
        return default


def _to_bool(value) -> bool:
    return str(value).strip().lower() not in ("", "0", "false", "off", "no", "none")


#: 表单/查询串里的题型数量字段 → 契约 §2 的 mix 键
MIX_FIELDS = (("选择", "n_choice"), ("填空", "n_fill"), ("解答", "n_solution"))


def _values_from_pairs(pairs, *, need_answer_default: bool = True) -> dict:
    """把表单或查询串的键值对收成一份「表单值」。缺字段给默认值，脏值不抛。"""
    got: dict[str, list[str]] = {}
    for key, value in pairs:
        got.setdefault(str(key), []).append(str(value))

    def one(key: str, default: str = "") -> str:
        values = got.get(key) or []
        return values[0] if values else default

    def many(key: str) -> list[str]:
        return [v for v in (got.get(key) or []) if str(v).strip()]

    def num(key: str, default: int) -> int:
        return _to_int(one(key), default) if key in got else default

    # 卷面书写区：默认全 0（作答在答题卡上）；表单可用 space_解答=4 这类字段显式覆盖
    space_over = {}
    for qtype in SPACE_QTYPES:
        key = "space_" + qtype
        if key in got:
            space_over[qtype] = max(0, min(MAX_SPACE_PARAS, num(key, 0)))

    if "need_answer" in got:
        need_answer = _to_bool(one("need_answer"))
    else:
        need_answer = need_answer_default

    # 省纸模式：表单里隐藏域先给 normal，勾上复选框再追加 compact，所以取**最后**一个值
    # （不依赖 JS；脏值/缺省都退回省纸，跟复选框默认勾选一致）
    layout_values = got.get("layout") or []
    layout = (layout_values[-1] if layout_values else "compact").strip().lower()
    if layout not in ("compact", "normal"):
        layout = "compact"

    # seed：留空 = 自动（每次换一套）；填数字 = 可精确复现。非法内容按留空处理。
    seed_text = one("seed").strip()
    try:
        seed = int(seed_text) if seed_text else None
    except ValueError:
        seed = None

    kp_mode = one("kp_mode", "any").strip().lower()
    return {
        "title": (one("title").strip() or "七年级数学 · 训练卷 A")[:80],
        "seed": seed,
        "seed_text": seed_text,
        "n_choice": num("n_choice", 10),
        "n_fill": num("n_fill", 8),
        "n_solution": num("n_solution", 6),
        "chapters": many("chapters"),
        "kps": many("kps"),
        "kp_mode": kp_mode if kp_mode in ("any", "all") else "any",
        "need_answer": need_answer,
        "exclude_empty": True,
        "layout": layout,
        "space_over": space_over,
    }


def _space_from_values(values: dict) -> dict:
    """卷面书写区：默认全 0（作答在答题卡上，卷面不留白）。

    默认值不在这里写死 —— 取 `picker.DEFAULT_SPACE`（全 0）当单一真值来源；
    表单/接口给 `space_<题型>` 时才覆盖（例如 space_解答=4 需要草稿区）。
    """
    space = dict(getattr(_load_module("picker"), "DEFAULT_SPACE", {}) or {})
    for qtype, n in (values.get("space_over") or {}).items():
        if qtype in SPACE_QTYPES:
            space[qtype] = max(0, min(MAX_SPACE_PARAS, int(n)))
    return space


def _params_from_values(values: dict) -> dict:
    """表单值 → 契约 §2 的组卷参数（picker.build / count_available 直接吃）。"""
    return {
        "title": values["title"],
        "mix": {qtype: int(values[field]) for qtype, field in MIX_FIELDS},
        "scope": {"chapters": list(values["chapters"]),
                  "kps": list(values["kps"]),
                  "kp_mode": values["kp_mode"]},
        "requirements": {"need_answer": bool(values["need_answer"]),
                         "exclude_empty": True},
        "seed": int(values["seed"]) if values.get("seed") is not None
                else secrets.randbelow(SEED_MAX),
        "layout": values.get("layout") or LAYOUT_COMPACT,
        "space": _space_from_values(values),
    }


def _load_module(name: str):
    """按需 import 队友模块：起服务不依赖它们，缺了也能翻成友好错误页。"""
    import importlib

    return importlib.import_module(name)


def _count_available(conn, values: dict) -> dict:
    """各题型「当前可出多少题」。mix 全 0 时给个哑 mix（count 只看 scope/requirements）。"""
    params = _params_from_values(values)
    if sum(params["mix"].values()) <= 0:
        params["mix"] = {"选择": 1}
    raw = _load_module("picker").count_available(conn, params)
    data = raw.get("available") if isinstance(raw, dict) and "available" in raw else raw
    return {str(k): int(v) for k, v in (data or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _connect_rw(db_path: str) -> sqlite3.Connection:
    if not os.path.isfile(db_path):
        raise DbMissing(os.path.abspath(db_path))
    return _load_module("db").connect(db_path)


#: 已经确认过 papers.deleted_at 存在的库（避免每个请求都 PRAGMA）
_SCHEMA_READY: set[str] = set()


def _ensure_papers_schema(db_path: str) -> None:
    """软删支持：给 papers 加 deleted_at（幂等 ALTER，不重建表、不动既有数据）。"""
    key = os.path.abspath(db_path)
    if key in _SCHEMA_READY or not os.path.isfile(key):
        return
    conn = _connect_rw(key)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN deleted_at TEXT")
            conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY.add(key)


def _papers_with_submissions_mb(out_root: str, db_path: str) -> float:
    """有判卷记录的卷子占了多少磁盘（这些目录是"为什么判错"的证据，软删也不清）。"""
    try:
        conn = _open_ro(db_path)
        try:
            pids = [r[0] for r in conn.execute(
                "SELECT DISTINCT pid FROM submissions WHERE pid IS NOT NULL AND pid <> ''")]
        finally:
            conn.close()
    except (DbMissing, sqlite3.Error):
        return 0.0
    total = 0
    for pid in pids:
        if _safe_pid(str(pid)):
            total += _dir_size_mb(_paper_dir(out_root, str(pid)))
    return round(total, 1)


def _safe_pid(pid: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(pid or "")))


def _paper_dir(out_root: str, pid: str) -> str:
    return os.path.join(out_root, pid)


def _meta_path(out_root: str, pid: str) -> str:
    return os.path.join(_paper_dir(out_root, pid), "preview.json")


def _read_meta(out_root: str, pid: str) -> dict:
    path = _meta_path(out_root, pid)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(out_root: str, pid: str, meta: dict) -> None:
    path = _meta_path(out_root, pid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _scan_pngs(path: str, prefix: str) -> list[str]:
    """没有 preview.json 时的兜底：按文件名里的页码排好序。"""
    if not os.path.isdir(path):
        return []
    found = []
    for name in os.listdir(path):
        if not name.lower().startswith(prefix) or not name.lower().endswith(".png"):
            continue
        stem = name[len(prefix):-4]
        if stem.isdigit():
            found.append((int(stem), os.path.join(path, name)))
    return [p for _, p in sorted(found)]


def _dir_size_mb(path: str) -> float:
    """目录占用（MB，保留 1 位）。目录不存在算 0，读不到的单个文件跳过。"""
    total = 0
    if not os.path.isdir(path):
        return 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return round(total / 1048576.0, 1)


#: 合法卷号（picker 生成的格式）。删除/落盘路径只认这个，杜绝目录穿越。
PID_RX = re.compile(r"^P\d{8}-\d{3}$")


def _delete_target(out_root: str, pid: str) -> str | None:
    """删除目标目录：pid 必须形如 P20261031-001，且解析出的绝对路径必须在 out 根目录之下。"""
    if not PID_RX.match(str(pid or "")):
        return None
    root = os.path.abspath(out_root)
    target = os.path.abspath(os.path.join(root, pid))
    try:
        if os.path.commonpath([root, target]) != root:
            return None
    except ValueError:            # 不同盘符时 commonpath 会抛
        return None
    if target == root or not target.startswith(root + os.sep):
        return None
    return target


def _paper_summaries(out_root: str, db_path: str) -> list[dict]:
    """「我的卷子」列表：一行一份卷子，状态看产物文件，不信数据库里的路径字段。"""
    conn = _open_ro(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
        where = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
        rows = conn.execute(
            "SELECT pid, title, created_at, n_items, manifest,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.pid = papers.pid) AS n_subs"
            " FROM papers" + where + " ORDER BY created_at DESC, pid DESC").fetchall()
    finally:
        conn.close()

    summaries = []
    for row in rows:
        pid = row["pid"]
        folder = _paper_dir(out_root, pid)
        meta = _read_meta(out_root, pid)
        paper_png = list(meta.get("paper_png") or _scan_pngs(folder, "paper_"))
        card_png = list(meta.get("card_png") or _scan_pngs(folder, "card_"))
        paper_pdf = os.path.join(folder, "paper.pdf")
        card_pdf = os.path.join(folder, "card.pdf")
        n_items = row["n_items"] or 0
        if not n_items:
            try:
                n_items = len(json.loads(row["manifest"] or "{}").get("items") or [])
            except (TypeError, ValueError):
                n_items = 0
        summaries.append({
            "pid": pid,
            "title": row["title"] or pid,
            "created_at": row["created_at"] or "",
            "n_items": int(n_items),
            "paper_n": len(paper_png),
            "card_n": len(card_png),
            "has_paper_pdf": os.path.isfile(paper_pdf),
            "has_card_pdf": os.path.isfile(card_pdf),
            "has_print_pdf": os.path.isfile(os.path.join(folder, "print.pdf")),
            "degraded": bool(meta.get("degraded")) or not os.path.isfile(paper_pdf),
            "n_submissions": int(row["n_subs"] or 0),
        })
    return summaries


def _is_word_missing(exc: BaseException) -> bool:
    """判「是不是 Word 不可用」：render.py 的 WordUnavailable，或 paper 的 PaperError 文案。"""
    if exc.__class__.__name__ in ("WordUnavailable", "WordMissing", "WordError"):
        return True
    text = str(exc)
    if "Word" not in text:
        return False
    return any(k in text for k in ("不可用", "没有可用的", "未安装", "Dispatch", "win32com"))


def _is_cross_drive_error(exc: BaseException) -> bool:
    """render.py 把 PDF 先写系统临时盘再 os.replace 到 out_dir；跨盘时 Windows 报 WinError 17。"""
    if getattr(exc, "winerror", None) == 17:
        return True
    text = str(exc)
    return "不同的磁盘驱动器" in text or "WinError 17" in text


def _relocate_render(result: dict, src_dir: str, dst_dir: str) -> dict:
    """把同盘临时目录里渲染好的 docx/pdf/png 拷回真正的产物目录，并改写返回路径。"""
    os.makedirs(dst_dir, exist_ok=True)

    def move_one(path):
        if not path:
            return path
        target = os.path.join(dst_dir, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(target):
            if not os.path.isfile(path):
                return target if os.path.isfile(target) else path
            shutil.copy2(path, target)
        return target

    out = dict(result)
    out["docx"] = move_one(result.get("docx"))
    out["pdf"] = move_one(result.get("pdf"))
    out["png"] = [move_one(p) for p in (result.get("png") or [])]
    health = dict(result.get("health") or {})
    if health.get("pdf"):
        health["pdf"] = out["pdf"]
    if health.get("png"):
        health["png"] = list(out["png"])
    out["health"] = health
    return out


def _render_paper_anywhere(renderer, manifest: dict, folder: str) -> dict:
    """渲染卷面。跨盘落位失败时，退一步「同盘临时目录渲染 + 拷回」，不把整卷搞丢。"""
    try:
        return renderer(manifest, folder)
    except Exception as exc:  # noqa: BLE001
        if not _is_cross_drive_error(exc):
            raise
        work = tempfile.mkdtemp(prefix="compose_render_")
        try:
            rendered = renderer(manifest, work)
            return _relocate_render(rendered, work, folder)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _banner_from_explain(code: str, detail: dict | None = None) -> dict:
    entry = errs.explain(code, detail)
    hints = [line.strip() for line in str(entry.get("fix") or "").splitlines() if line.strip()]
    what = str(entry.get("what") or "")
    why = str(entry.get("why") or "")
    detail_text = (what + ("\n" + why if why else "")).strip()
    return {"title": entry.get("title") or "出错了", "detail": detail_text,
            "hints": hints, "code": code}


def _merge_pdfs(sources: list[str], target: str) -> None:
    """卷面在前、答题卡在后，合并成一个 PDF（pymupdf）。"""
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - 旧版 pymupdf 只有 fitz 别名
        import fitz  # type: ignore

    merged = fitz.open()
    try:
        for path in sources:
            src = fitz.open(path)
            try:
                merged.insert_pdf(src)
            finally:
                src.close()
        tmp = target + ".tmp"
        merged.save(tmp)
    finally:
        merged.close()
    os.replace(tmp, target)


def _defaults(stats: list[dict]) -> dict:
    mix = {s["qtype"]: s["default"] for s in stats}
    return {
        "title": "七年级数学 · 训练卷 A",
        "seed": None,
        "seed_text": "",
        "need_answer": True,
        "exclude_empty": True,
        "layout": LAYOUT_COMPACT,
        "mix": mix,
        "total": sum(mix.values()),
        "kp_mode": "any",
    }


def _index_context(dbp: str, values: dict | None = None) -> dict:
    """打开只读库，取选择器数据 + 各题型「可出多少题」；失败时抛给调用方翻成 banner。"""
    values = values or _values_from_pairs([])
    conn = _open_ro(dbp)
    warnings: list[dict] = []
    try:
        kps = read_kps(conn)
        stats = read_qtype_stats(conn)
        n_questions = int(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
        n_families = 0
        try:
            n_families = int(conn.execute("SELECT COUNT(*) FROM families").fetchone()[0])
        except sqlite3.Error:
            pass
        available = None
        try:
            available = _count_available(conn, values)
        except Exception as exc:  # noqa: BLE001 - 可出题量算不出来不该挡住组卷页
            if exc.__class__.__name__ == "ScopeError":
                warnings.append(_banner(
                    "选的知识点已经不在库里",
                    "题库重建过，页面上带的旧知识点失效了；已按全库重新计算可出题量。",
                    ["刷新页面重新勾选", "都不勾 = 全库不限制"], code="scope_stale"))
                values = dict(values, kps=[], chapters=[])
                try:
                    available = _count_available(conn, values)
                except Exception:
                    available = None
            else:
                warnings.append(_banner(
                    "算不出可出题量",
                    f"{exc.__class__.__name__}: {exc}",
                    ["先看自检页 /health：数据库是否正常、表是否齐"], code="count_failed"))
    finally:
        conn.close()
    chapters = group_chapters(kps)
    if not kps:
        warnings.append(_banner(
            "知识点表为空",
            "kps 表里没有记录，装不出章节/知识点选择器。",
            ["执行 python app\\build_db.py 重建题库（会重算知识点）",
             "或在自检页确认数据库路径是否指向了一份空库"],
            code="kps_empty",
        ))
    return {
        "chapters": chapters,
        "stats": stats,
        "defaults": _defaults(stats),
        "values": values,
        "available": available,
        "db_summary": {"questions": n_questions,
                       "kps": len(kps), "families": n_families},
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- 卷子预览

def _paper_row(db_path: str, pid: str):
    conn = _open_ro(db_path)
    try:
        return conn.execute("SELECT * FROM papers WHERE pid=?", (pid,)).fetchone()
    finally:
        conn.close()


def _kp_names(db_path: str) -> dict:
    try:
        conn = _open_ro(db_path)
    except (DbMissing, sqlite3.Error):
        return {}
    try:
        return {r["kp_id"]: {"chapter": r["chapter"], "kp": r["kp"]}
                for r in conn.execute("SELECT kp_id, chapter, kp FROM kps")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _preview_ctx(out_root: str, pid: str, row, kp_map: dict) -> dict:
    try:
        manifest = json.loads(row["manifest"] or "{}")
    except (TypeError, ValueError):
        manifest = {}
    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, ValueError):
        params = {}

    meta = _read_meta(out_root, pid)
    folder = _paper_dir(out_root, pid)
    paper_png = list(meta.get("paper_png") or _scan_pngs(folder, "paper_"))
    card_png = list(meta.get("card_png") or _scan_pngs(folder, "card_"))
    paper_pdf = meta.get("paper_pdf") or os.path.join(folder, "paper.pdf")
    card_pdf = meta.get("card_pdf") or os.path.join(folder, "card.pdf")
    has_paper_pdf = bool(paper_pdf) and os.path.isfile(paper_pdf)
    has_card_pdf = bool(card_pdf) and os.path.isfile(card_pdf)

    items = []
    for pos, raw in enumerate(manifest.get("items") or [], start=1):
        keys = raw.get("answer_key") or []
        answer = "；".join(str(k.get("value") or "").strip()
                          for k in keys if str(k.get("value") or "").strip())
        info = kp_map.get(str(raw.get("kp_id") or "")) or {}
        items.append({
            "idx": raw.get("idx") or pos,
            "no": raw.get("no") or "",
            "qtype": raw.get("qtype") or "",
            "kp_id": raw.get("kp_id") or "",
            "kp": info.get("kp") or "",
            "chapter": info.get("chapter") or "",
            "has_answer": bool(answer),
            "answer": answer,
        })

    layout = str(params.get("layout")
                 or (manifest.get("params") or {}).get("layout")
                 or LAYOUT_COMPACT).strip().lower()
    if layout not in (LAYOUT_COMPACT, LAYOUT_NORMAL):
        layout = LAYOUT_COMPACT

    mix = params.get("mix") or {}
    scope = params.get("scope") or {}
    reroll = {
        "title": row["title"] or manifest.get("title") or "",
        "n_choice": int(mix.get("选择") or 0),
        "n_fill": int(mix.get("填空") or 0),
        "n_solution": int(mix.get("解答") or 0),
        "chapters": list(scope.get("chapters") or []),
        "kps": list(scope.get("kps") or []),
        "kp_mode": scope.get("kp_mode") or "any",
        "need_answer": bool((params.get("requirements") or {}).get("need_answer")),
        "layout": layout,
    }

    return {
        "pid": pid,
        "title": row["title"] or manifest.get("title") or pid,
        "layout": layout,
        "reroll": reroll,
        "layout_label": "省纸" if layout == LAYOUT_COMPACT else "宽裕",
        "created_at": row["created_at"] or manifest.get("created_at") or "",
        "params": params,
        "items": items,
        "n_items": len(items),
        "paper_pages": [f"/papers/{pid}/img/paper/{i}.png" for i in range(1, len(paper_png) + 1)],
        "card_pages": [f"/papers/{pid}/img/card/{i}.png" for i in range(1, len(card_png) + 1)],
        "paper_n": len(paper_png),
        "card_n": len(card_png),
        "total_n": len(paper_png) + len(card_png),
        "has_paper_pdf": has_paper_pdf,
        "has_card_pdf": has_card_pdf,
        "degraded": bool(meta.get("degraded")),
        "degraded_kind": meta.get("degraded_kind") or ("word" if meta.get("degraded") else ""),
        "degraded_reason": meta.get("warning") or "",
        "out_dir": folder,
    }


def create_app(db_path: str | None = None, config_path: str | None = None,
               out_dir: str | None = None, paper_renderer=None) -> FastAPI:
    """应用工厂。

    db_path/config_path 缺省走 app/data/app.db 与 app/config.json；
    out_dir 缺省 app/out；paper_renderer 可注入（测试里模拟 Word 不可用）。
    """
    dbp = os.path.abspath(db_path or os.environ.get("MATH_APP_DB") or DEFAULT_DB)
    cfp = os.path.abspath(config_path or os.environ.get("MATH_APP_CONFIG") or DEFAULT_CONFIG)
    out_root = os.path.abspath(out_dir or os.environ.get("MATH_APP_OUT") or OUT_DIR)

    app = FastAPI(title=APP_TITLE, version=APP_VERSION, docs_url=None, redoc_url=None)
    app.state.db_path = dbp
    app.state.config_path = cfp
    app.state.out_dir = out_root
    app.state.paper_renderer = paper_renderer

    # 局域网模式：host 不是 loopback（或 MATHAPP_LAN=1）就算对外开放 —— 必须带访问码。
    # 本地 127.0.0.1 永远免 pin，免得自己用着麻烦。
    server_cfg = load_server_config(cfp)
    lan_on = bool(server_cfg.get("lan"))
    lan_pin = ensure_lan_pin(cfp) if lan_on else ""
    ip = lan_ip() if lan_on else None
    app.state.lan = lan_on
    app.state.lan_pin = lan_pin
    app.state.lan_port = int(server_cfg.get("port") or PORT)
    app.state.lan_ip = ip
    app.state.lan_url = ("http://%s:%d/" % (ip, app.state.lan_port)) if (lan_on and ip) else None
    if lan_on:
        print("[局域网] 监听 %s:%d —— 手机访问 %s（访问码见下）"
              % (server_cfg.get("host"), app.state.lan_port,
                 app.state.lan_url or "（探测不到局域网 IP）"))
        if lan_pin:
            print("[局域网] 4 位访问码：%s（config.json 的 server.pin；127.0.0.1 不需要）" % lan_pin)

    PIN_COOKIE = "mathapp_pin"
    PIN_MAX_AGE = 30 * 24 * 3600

    def _client_host(request: Request) -> str:
        return (request.client.host if request.client else "") or ""

    def _is_loopback_client(request: Request) -> bool:
        host = _client_host(request)
        return host in LOOPBACK_HOSTS or host.startswith("127.") or host == "testclient"

    @app.middleware("http")
    async def _pin_gate(request: Request, call_next):  # noqa: ANN001, ANN202
        """LAN 模式下，非 loopback 请求必须带访问码（?pin= 一次，之后 30 天 cookie）。"""
        if not app.state.lan_pin:
            return await call_next(request)
        if _is_loopback_client(request):
            return await call_next(request)
        path = request.url.path
        if path.startswith("/static") or path == "/favicon.ico" or path == "/healthz":
            return await call_next(request)
        given = (request.query_params.get("pin") or "").strip()
        cookie = request.cookies.get(PIN_COOKIE) or ""
        if given == app.state.lan_pin or cookie == app.state.lan_pin:
            response = await call_next(request)
            if given == app.state.lan_pin and cookie != app.state.lan_pin:
                response.set_cookie(PIN_COOKIE, app.state.lan_pin, max_age=PIN_MAX_AGE,
                                    httponly=True, samesite="lax")
            return response
        # 未通过：极简输入页，**不含任何卷子/答案/题库信息**
        return render("pin.html", status_code=401, page_title="需要访问码", nav_active="",
                      bad=bool(given), path=path,
                      lan_url=app.state.lan_url or "")

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # 判卷路由由 grade_routes 提供（t22）：这里只接线，绝不再定义 /submissions*
    app.state.recognizer = None          # 缺省 None = 让 grade 按 config.json 选后端
    try:
        from grade_routes import router as grade_router
        app.include_router(grade_router)
        app.state.grade_routes_ok = True
    except Exception as exc:  # noqa: BLE001 - 判卷模块缺了也要能起服务、能组卷
        traceback.print_exc()
        app.state.grade_routes_ok = False
        app.state.grade_routes_error = f"{type(exc).__name__}: {exc}"

    def _not_found(title: str, detail: str, status_code: int = 404, links=None):
        return render("error.html", status_code=status_code, page_title=title,
                      error=_banner(title, detail, ["回组卷首页看看", "打开使用指南 /guide"]),
                      links=links or [{"href": "/", "label": "回组卷首页"},
                                      {"href": "/guide", "label": "使用指南"}])

    def _ocr_notice_ctx() -> dict:
        """首页/设置页用：还没配识别服务时提醒一句（填空/解答会全进人工确认）。"""
        try:
            info = load_ocr_config(cfp)
        except Exception:  # noqa: BLE001
            return {"show": False, "text": "", "backend": ""}
        backend = str(info.get("backend") or "manual")
        has_token = bool((info.get("paddle_vl") or {}).get("token_present")
                         or (info.get("vlm") or {}).get("key_present"))
        if backend == "manual":
            return {"show": True, "backend": backend, "level": "warn",
                    "text": "尚未配置识别服务（当前是「全人工确认」）：填空/解答会全部进待人工确认队列，"
                            "选择题仍自动判。"}
        if backend in ("paddle_vl", "vlm") and not has_token:
            return {"show": True, "backend": backend, "level": "warn",
                    "text": "识别后端是 %s，但还没填 Key/Token：填空/解答会降级为人工确认。"
                            % info.get("backend_label", backend)}
        return {"show": False, "backend": backend, "level": "", "text": ""}

    def _lan_ctx() -> dict:
        """模板用的局域网信息：开没开、手机地址、二维码开关。"""
        return {"on": bool(app.state.lan), "pin_required": bool(app.state.lan_pin),
                "url": app.state.lan_url, "ip": app.state.lan_ip,
                "port": app.state.lan_port,
                "qr": "/qr.png?target=judge" if (app.state.lan and app.state.lan_url) else "",
                "pin": app.state.lan_pin}

    def _render_index(values=None, error=None, status_code: int = 200):
        values = values or _values_from_pairs([])
        try:
            ctx = _index_context(dbp, values)
        except (DbMissing, sqlite3.Error) as exc:
            return render("index.html", status_code=503, page_title="组卷",
                          nav_active="compose", error=_db_error(exc, dbp),
                          chapters=[], stats=[], defaults=_defaults([]),
                          values=values, available=None, db_summary=None, warnings=[],
                          lan=_lan_ctx())
        if error:
            ctx["error"] = error
        ctx["lan"] = _lan_ctx()
        ctx["ocr_notice"] = _ocr_notice_ctx()
        ctx["pdf_backend"] = probe_pdf_backend()
        ctx["pdf_note"] = pdf_backend_note(ctx["pdf_backend"])
        return render("index.html", status_code=status_code, page_title="组卷",
                      nav_active="compose", **ctx)

    # ---------------------------------------------------------------- 组卷表单
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):  # noqa: ANN202
        # 查询串用来「试算可出题量」：/?kps=1.1&kps=1.2&need_answer=on
        values = _values_from_pairs(request.query_params.multi_items())
        return _render_index(values)

    # ---------------------------------------------------------------- 建卷
    def _persist_manifest(conn, manifest: dict, fallback_title: str = "") -> str:
        """排版 + 答题卡 + 落库 + 旁车信息，返回 pid。重活，调用方负责丢线程池。"""
        picker = _load_module("picker")
        pid = manifest["pid"]
        folder = _paper_dir(out_root, pid)
        meta = {
            "pid": pid, "title": manifest.get("title") or fallback_title,
            "created_at": _now_iso(), "degraded": False, "warning": "",
            "docx": "", "paper_pdf": None, "paper_png": [], "paper_pages": 0,
            "card_pdf": None, "card_png": [], "card_pages": 0,
            "n_items": len(manifest.get("items") or []),
        }

        renderer = paper_renderer
        try:
            if renderer is None:
                renderer = lambda m, o: _load_module("paper").render_paper(m, o)  # noqa: E731
            rendered = _render_paper_anywhere(renderer, manifest, folder)
            meta.update({
                "docx": rendered.get("docx") or "",
                "paper_pdf": rendered.get("pdf"),
                "paper_png": list(rendered.get("png") or []),
                "paper_pages": int(rendered.get("pages")
                                   or len(rendered.get("png") or [])),
            })
        except Exception as exc:  # noqa: BLE001
            if _is_word_missing(exc):
                # 降级：没 Word 就没卷面 PDF/PNG，但答题卡照常生成、照常可打印
                entry = errs.explain("WordUnavailable", {"message": str(exc)})
                meta["degraded"] = True
                meta["degraded_kind"] = "word"
                meta["warning"] = entry["what"] + " 答题卡仍可打印。"
            else:
                # 其他渲染故障也别把整卷搞丢：答题卡照常出，卷面标成失败并留下原始报错
                traceback.print_exc()
                meta["degraded"] = True
                meta["degraded_kind"] = "render"
                meta["warning"] = ("卷面渲染失败：%s: %s。答题卡仍可打印，"
                                   "请把这条报错反馈（渲染由 paper/render 模块负责）。"
                                   % (type(exc).__name__, exc))

        card = _load_module("card").build_card(manifest, folder)
        meta.update({
            "card_pdf": card.get("pdf"),
            "card_png": list(card.get("png") or []),
            "card_pages": int((card.get("card") or {}).get("n_pages")
                              or len(card.get("png") or [])),
        })
        picker.save(conn, manifest, meta["docx"] or "", meta["paper_pdf"] or "")
        _write_meta(out_root, pid, meta)
        return pid

    def _compose(values: dict) -> str:
        """组卷：表单值 → 参数 → picker.build → 排版落库，返回 pid。"""
        _ensure_papers_schema(dbp)
        conn = None
        try:
            conn = _connect_rw(dbp)
            manifest = _load_module("picker").build(_params_from_values(values), conn)
            return _persist_manifest(conn, manifest, values["title"])
        finally:
            if conn is not None:
                conn.close()

    @app.post("/papers")
    async def create_paper(request: Request):  # noqa: ANN202
        form = await request.form()
        values = _values_from_pairs(form.multi_items(), need_answer_default=False)
        try:
            pid = await run_in_threadpool(_compose, values)
        except DbMissing as exc:
            return render("index.html", status_code=503, page_title="组卷",
                          nav_active="compose", error=_db_error(exc, dbp),
                          chapters=[], stats=[], defaults=_defaults([]),
                          values=values, available=None, db_summary=None, warnings=[])
        except Exception as exc:  # noqa: BLE001
            name = exc.__class__.__name__
            if name in ("InsufficientQuestions", "ScopeError"):
                detail = {"message": str(exc)}
                if name == "InsufficientQuestions":
                    detail["details"] = getattr(exc, "details", {}) or {}
                page = _render_index(values=values, status_code=400,
                                     error=_banner_from_explain(name, detail))
                return page
            if name in ("SelectError", "ValueError"):
                banner = _banner(
                    "组卷参数不对",
                    str(exc),
                    ["检查题量：三类题不能全是 0，至少给一种题型填数量",
                     "检查范围：勾的章节/知识点要在库里真实存在",
                     "改完再点「生成试卷」"])
                banner["code"] = "SelectError"
                return _render_index(values=values, status_code=400, error=banner)
            if isinstance(exc, sqlite3.Error):
                return render("error.html", status_code=503, page_title="出错了",
                              error=_db_error(exc, dbp))
            traceback.print_exc()
            return render("error.html", status_code=500, page_title="组卷失败",
                          error=_banner_from_explain(
                              "internal_error", {"message": f"{type(exc).__name__}: {exc}"}),
                          links=[{"href": "/", "label": "回组卷页重新填"},
                                 {"href": "/health", "label": "打开自检页"}])
        return RedirectResponse(f"/papers/{pid}", status_code=302)

    # ---------------------------------------------------------------- 错题训练
    def _submission_wrong_nos(conn, sid: str):
        """一次判卷里判错/待确认的题号（去重、保持卷面顺序）。"""
        row = conn.execute("SELECT pid, detail_json FROM submissions WHERE sid=?",
                           (sid,)).fetchone()
        if row is None:
            return [], {}
        detail = json.loads(row["detail_json"] or "{}")
        nos, seen = [], set()
        for unit in detail.get("items") or []:
            if unit.get("verdict") not in ("wrong", "unsure"):
                continue
            no = str(unit.get("no") or "")
            if no and no not in seen:
                seen.add(no)
                nos.append(no)
        return nos, {"pid": row["pid"], "detail": detail}

    def _parse_nos(text) -> list:
        nos, seen = [], set()
        for chunk in re.split(r"[\s,，、;；]+", str(text or "")):
            no = chunk.strip()
            if no and no not in seen:
                seen.add(no)
                nos.append(no)
        return nos

    @app.get("/train", response_class=HTMLResponse)
    def train_page(request: Request):  # noqa: ANN202
        sid = (request.query_params.get("sid") or "").strip()
        nos_text, summary, error = "", None, None
        if sid:
            try:
                conn = _open_ro(dbp)
                try:
                    nos, info = _submission_wrong_nos(conn, sid)
                finally:
                    conn.close()
            except (DbMissing, sqlite3.Error) as exc:
                return render("train.html", status_code=503, page_title="错题训练",
                              nav_active="train", error=_db_error(exc, dbp), sid=sid,
                              nos_text="", summary=None)
            if nos:
                nos_text = ",".join(nos)
                summary = {"sid": sid, "pid": info.get("pid"), "n": len(nos)}
            else:
                error = _banner("这次没有错题（或还没判完）",
                                f"判卷 {sid} 里没有判错或待确认的题。",
                                ["等填空/解答识别完再生成训练卷",
                                 "也可以直接把题号贴进来手动生成"])
        return render("train.html", page_title="错题训练", nav_active="train",
                      error=error, sid=sid, nos_text=nos_text, summary=summary)

    def _train_compose(payload: dict) -> str:
        """错题 → train.plan → picker.build(nos) → 排版落库。重活，跑在线程池里。"""
        _ensure_papers_schema(dbp)
        conn = None
        try:
            conn = _connect_rw(dbp)
            params = _load_module("train").plan(conn, payload["wrong_nos"],
                                                n_per=payload["n_per"])
            # train.plan 默认按错题集合派生 seed（同一批错题永远同一份训练卷）；
            # 用户没指定 seed 时改成随机 —— 和组卷页「留空=每次换一套」同一套语义。
            if payload.get("seed") is None:
                params["seed"] = secrets.randbelow(SEED_MAX)
            else:
                params["seed"] = int(payload["seed"])
            # train.plan 只看题族/知识点，不知道题库体检（缺图、不可印、答案不符）；
            # picker.build 会用 quality_flags 硬挡，所以这里先按同一口径滤一遍，
            # 剔掉被挡的替代题（宁可卷子少两道，也不印缺图的废题）。
            nos = list(params.get("nos") or [])
            if nos:
                ok_nos, blocked = _load_module("quality").filter_printable(
                    conn, nos, need_answer=True)
                if blocked:
                    params["nos"] = ok_nos
                    params["mix"] = {}          # 让 picker 按过滤后的 nos 重算题型数量
                    info = params.setdefault("train", {})
                    info["dropped_printable"] = sorted(blocked)
                    print("[train] 剔掉题库体检挡掉的替代题 %d 道：%s"
                          % (len(blocked), ",".join(sorted(blocked)[:8])))
            if not params.get("nos"):
                raise ValueError("同类题都被题库体检挡掉了（缺图/不可印），换几道错题或放宽范围再试")
            if payload.get("title"):
                params["title"] = payload["title"]
            params["layout"] = payload.get("layout") or LAYOUT_COMPACT
            manifest = _load_module("picker").build(params, conn)
            return _persist_manifest(conn, manifest,
                                     params.get("title") or "错题训练卷")
        finally:
            if conn is not None:
                conn.close()

    @app.post("/train")
    async def create_train(request: Request):  # noqa: ANN202
        form = await request.form()
        sid = str(form.get("sid") or "").strip()
        nos_text = str(form.get("nos") or "").strip()
        n_per = max(1, min(10, _to_int(form.get("n_per"), 3)))
        wrong_nos = _parse_nos(nos_text)
        seed_text = str(form.get("seed") or "").strip()
        try:
            seed_value = int(seed_text) if seed_text else None
        except ValueError:
            seed_value = None
        if not wrong_nos and sid:
            try:
                conn = _open_ro(dbp)
                try:
                    wrong_nos, _info = _submission_wrong_nos(conn, sid)
                finally:
                    conn.close()
            except (DbMissing, sqlite3.Error) as exc:
                return render("error.html", status_code=503, page_title="错题训练",
                              error=_db_error(exc, dbp))
        if not wrong_nos:
            banner = _banner("没有可训练的错题",
                             "这次判卷里没有判错或待确认的题，也没填题号。",
                             ["在结果页确认几道错题，再点「用错题生成训练卷」",
                              "或把题号按 00001,00002 贴进输入框",
                              "还没判卷就先上传答题卡"])
            banner["code"] = "train_empty"
            return render("train.html", status_code=400, page_title="错题训练",
                          nav_active="train", error=banner, sid=sid,
                          nos_text=nos_text, summary=None)
        try:
            pid = await run_in_threadpool(_train_compose,
                                          {"wrong_nos": wrong_nos, "n_per": n_per,
                                           "seed": seed_value})
        except DbMissing as exc:
            return render("error.html", status_code=503, page_title="错题训练",
                          error=_db_error(exc, dbp))
        except Exception as exc:  # noqa: BLE001
            name = exc.__class__.__name__
            if name in ("InsufficientQuestions", "ScopeError"):
                detail = {"message": str(exc)}
                if name == "InsufficientQuestions":
                    detail["details"] = getattr(exc, "details", {}) or {}
                return render("train.html", status_code=400, page_title="错题训练",
                              nav_active="train",
                              error=_banner_from_explain(name, detail),
                              sid=sid, nos_text=nos_text, summary=None)
            if isinstance(exc, ValueError):
                banner = _banner("找不出可替代的同类题", str(exc),
                                 ["少选几道错题再试（同类题不够分）",
                                  "放宽知识点范围，或手动选题号",
                                  "确认题库里有同类型（选择/填空/解答）的题"])
                banner["code"] = "train_no_alt"
                return render("train.html", status_code=400, page_title="错题训练",
                              nav_active="train", error=banner, sid=sid,
                              nos_text=nos_text, summary=None)
            traceback.print_exc()
            return render("error.html", status_code=500, page_title="训练卷没生成",
                          error=_banner_from_explain(
                              "internal_error", {"message": f"{type(exc).__name__}: {exc}"}),
                          links=[{"href": "/train", "label": "回训练页"},
                                 {"href": "/health", "label": "打开自检页"}])
        return RedirectResponse(f"/papers/{pid}", status_code=302)

    # ---------------------------------------------------------------- 我的卷子
    def _deleted_page(pid: str):
        return render("error.html", status_code=410, page_title="这份卷子已删除",
                      error=_banner(
                          "这份卷子已删除",
                          f"卷号 {pid} 已从「我的卷子」里删掉。"
                          "它判过卷，所以判卷记录与批注图按策略保留，不随着删除清掉。",
                          ["历史判卷结果仍能打开（从判卷页或用原来的结果链接）",
                           "要重新出这份卷：回组卷页按同样参数再生成一份",
                           "想连磁盘一起清掉：先删掉它的判卷记录，再删卷子"]),
                      links=[{"href": "/papers", "label": "回我的卷子"},
                             {"href": "/submissions", "label": "去判卷页"}])

    @app.get("/papers", response_class=HTMLResponse)
    def papers_list():  # noqa: ANN202
        try:
            _ensure_papers_schema(dbp)
            papers = _paper_summaries(out_root, dbp)
        except (DbMissing, sqlite3.Error) as exc:
            return render("papers.html", status_code=503, page_title="我的卷子",
                          nav_active="papers", error=_db_error(exc, dbp),
                          papers=[], total_mb=0.0, n_papers=0)
        return render("papers.html", page_title="我的卷子", nav_active="papers",
                      papers=papers, total_mb=_dir_size_mb(out_root),
                      n_papers=len(papers))

    @app.post("/papers/{pid}/delete")
    def delete_paper(pid: str):  # noqa: ANN202
        target = _delete_target(out_root, pid)
        if target is None:
            return render("error.html", status_code=400, page_title="卷号不合法",
                          error=_banner(
                              "卷号不合法，没删任何东西",
                              f"{pid!r} 不是合法卷号（应形如 P20261031-001）。",
                              ["回「我的卷子」列表，从那一行的「删除」按钮删",
                               "卷号印在卷面与答题卡顶部"]),
                          links=[{"href": "/papers", "label": "回我的卷子"}])
        try:
            _ensure_papers_schema(dbp)
            conn = _connect_rw(dbp)
            try:
                row = conn.execute("SELECT pid FROM papers WHERE pid=?", (pid,)).fetchone()
                n_subs = int(conn.execute(
                    "SELECT COUNT(*) FROM submissions WHERE pid=?", (pid,)).fetchone()[0])
                if row is None and not os.path.isdir(target):
                    return render("error.html", status_code=404, page_title="找不到这份卷子",
                                  error=_banner("找不到这份卷子",
                                                f"数据库和产物目录里都没有 {pid}，可能已经删过了。",
                                                ["回列表看看还剩哪些卷子"]),
                                  links=[{"href": "/papers", "label": "回我的卷子"}])
                if n_subs > 0:
                    # 判过卷：只软删，保留 out/<pid>/ 里的判卷证据（裁切图、批注图、原图）
                    conn.execute("UPDATE papers SET deleted_at=? WHERE pid=?",
                                 (_now_iso(), pid))
                    conn.commit()
                    return RedirectResponse("/papers", status_code=302)
                conn.execute("DELETE FROM papers WHERE pid=?", (pid,))
                conn.commit()
            finally:
                conn.close()
        except (DbMissing, sqlite3.Error) as exc:
            return render("error.html", status_code=503, page_title="删不掉这份卷子",
                          error=_db_error(exc, dbp),
                          links=[{"href": "/papers", "label": "回我的卷子"}])
        # 纯草稿卷（没判过卷）：连产物目录一起删
        shutil.rmtree(target, ignore_errors=True)
        return RedirectResponse("/papers", status_code=302)

    def _is_deleted(pid: str) -> bool:
        try:
            row = _paper_row(dbp, pid)
        except (DbMissing, sqlite3.Error):
            return False
        return bool(row is not None and "deleted_at" in row.keys() and row["deleted_at"])

    # ---------------------------------------------------------------- 卷子预览
    @app.get("/papers/{pid}", response_class=HTMLResponse)
    def paper_preview(pid: str):  # noqa: ANN202
        if not _safe_pid(pid):
            return _not_found("卷号不合法", f"{pid!r} 不是合法卷号。")
        try:
            row = _paper_row(dbp, pid)
        except (DbMissing, sqlite3.Error) as exc:
            return render("error.html", status_code=503, page_title="出错了",
                          error=_db_error(exc, dbp))
        if row is None:
            return _not_found("找不到这份卷子", f"数据库里没有卷号 {pid}。",
                              links=[{"href": "/", "label": "回组卷页重新生成"},
                                     {"href": "/guide", "label": "使用指南"}])
        if "deleted_at" in row.keys() and row["deleted_at"]:
            return _deleted_page(pid)
        try:
            ctx = _preview_ctx(out_root, pid, row, _kp_names(dbp))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return render("error.html", status_code=500, page_title="卷子读不出来",
                          error=_banner_from_explain(
                              "internal_error", {"message": f"{type(exc).__name__}: {exc}"}))
        return render("preview.html", page_title=f"卷子 {pid}",
                      nav_active="compose", **ctx)

    # ---------------------------------------------------------------- 预览图 / PDF
    @app.get("/papers/{pid}/img/{kind}/{page_no}.png")
    def paper_image(pid: str, kind: str, page_no: int):  # noqa: ANN202
        if not _safe_pid(pid) or kind not in ("paper", "card"):
            return _not_found("图片不存在", "这个图片地址不对。")
        if _is_deleted(pid):
            return _deleted_page(pid)
        folder = _paper_dir(out_root, pid)
        meta = _read_meta(out_root, pid)
        pngs = list(meta.get(kind + "_png") or _scan_pngs(folder, kind + "_"))
        if not (1 <= page_no <= len(pngs)):
            return _not_found("这一页不存在",
                              f"{'卷面' if kind == 'paper' else '答题卡'}只有 {len(pngs)} 页。")
        path = os.path.abspath(pngs[page_no - 1])
        if not path.startswith(os.path.abspath(folder) + os.sep) or not os.path.isfile(path):
            return _not_found("图片不存在", "文件被移动或删除了。")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "private, max-age=600"})

    def _pdf_paths(pid: str) -> dict:
        folder = _paper_dir(out_root, pid)
        meta = _read_meta(out_root, pid)
        paper_pdf = meta.get("paper_pdf") or os.path.join(folder, "paper.pdf")
        card_pdf = meta.get("card_pdf") or os.path.join(folder, "card.pdf")
        return {
            "folder": folder, "meta": meta,
            "paper": paper_pdf if paper_pdf and os.path.isfile(paper_pdf) else None,
            "card": card_pdf if card_pdf and os.path.isfile(card_pdf) else None,
        }

    def _degraded_pdf_page(pid: str, title: str, detail: str):
        banner = _banner_from_explain("WordUnavailable")
        banner["title"] = title
        banner["detail"] = detail
        return render("error.html", status_code=409, page_title=title, error=banner,
                      links=[{"href": f"/papers/{pid}", "label": "回预览页"},
                             {"href": f"/papers/{pid}/card.pdf", "label": "下载答题卡 PDF"}])

    @app.get("/papers/{pid}/pdf")
    def paper_pdf(pid: str):  # noqa: ANN202
        if not _safe_pid(pid):
            return _not_found("卷号不合法", f"{pid!r} 不是合法卷号。")
        if _is_deleted(pid):
            return _deleted_page(pid)
        paths = _pdf_paths(pid)
        if not paths["paper"]:
            return _degraded_pdf_page(
                pid, "卷面 PDF 未生成",
                "卷面 PDF 未生成（本机未检测到 Word），答题卡仍可打印。")
        return FileResponse(paths["paper"], media_type="application/pdf",
                            filename=f"{pid}-paper.pdf")

    @app.get("/papers/{pid}/card.pdf")
    def card_pdf(pid: str):  # noqa: ANN202
        if not _safe_pid(pid):
            return _not_found("卷号不合法", f"{pid!r} 不是合法卷号。")
        if _is_deleted(pid):
            return _deleted_page(pid)
        paths = _pdf_paths(pid)
        if not paths["card"]:
            return _not_found("答题卡 PDF 还没生成", "这份卷子没有答题卡文件，重新生成一次卷子。",
                              status_code=404)
        return FileResponse(paths["card"], media_type="application/pdf",
                            filename=f"{pid}-card.pdf")

    @app.get("/papers/{pid}/print.pdf")
    def print_pdf(pid: str):  # noqa: ANN202
        if not _safe_pid(pid):
            return _not_found("卷号不合法", f"{pid!r} 不是合法卷号。")
        if _is_deleted(pid):
            return _deleted_page(pid)
        paths = _pdf_paths(pid)
        if not paths["paper"]:
            return _degraded_pdf_page(
                pid, "合并打印件还没生成",
                "卷面 PDF 未生成（本机未检测到 Word），没法把卷面与答题卡合并；"
                "答题卡仍可单独打印。")
        if not paths["card"]:
            return _not_found("答题卡 PDF 还没生成",
                              "这份卷子没有答题卡文件，重新生成一次卷子。", status_code=404)
        target = os.path.join(paths["folder"], "print.pdf")
        try:
            newest = max(os.path.getmtime(paths["paper"]), os.path.getmtime(paths["card"]))
            if (not os.path.isfile(target)) or os.path.getmtime(target) < newest:
                _merge_pdfs([paths["paper"], paths["card"]], target)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return render("error.html", status_code=500, page_title="合并 PDF 失败",
                          error=_banner_from_explain(
                              "internal_error", {"message": f"{type(exc).__name__}: {exc}"}),
                          links=[{"href": f"/papers/{pid}", "label": "回预览页"}])
        return FileResponse(target, media_type="application/pdf",
                            filename=f"{pid}-print.pdf")

    # ---------------------------------------------------------------- 手机入口二维码
    def _qr_target_url(target: str) -> str:
        """二维码里放什么地址：判卷页；LAN 模式下把 pin 带上，扫一次就通。"""
        port = app.state.lan_port
        base = app.state.lan_url
        if not base:
            if app.state.lan_pin:
                base = "http://127.0.0.1:%d/" % port
            else:
                base = "http://127.0.0.1:%d/" % port
        path = "/submissions" if target in ("judge", "submissions", "") else "/" + target.lstrip("/")
        url = base.rstrip("/") + path
        if app.state.lan_pin and "?pin=" not in url:
            url += "?pin=" + app.state.lan_pin
        return url

    @app.get("/qr.png")
    def qr_png(target: str = "judge"):  # noqa: ANN202
        """手机扫码入口：默认指向判卷上传页（LAN 模式自动带访问码）。"""
        try:
            import qrcode
        except ImportError:  # pragma: no cover - 依赖缺失时给友好页
            return _not_found("生成二维码失败", "没装 qrcode：python -m pip install -r requirements.txt")
        img = qrcode.make(_qr_target_url(target))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- 设置（OCR / AI Key）
    OCR_BACKENDS = (
        ("paddle_vl", "PaddleOCR-VL 云端", "把作答格拼图发给 PaddleOCR-VL，识别质量最好"),
        ("vlm", "视觉模型（OpenAI 兼容）", "自己填 base_url + key + 模型名，走兼容 OpenAI 的视觉接口"),
        ("local", "本机离线（Windows OCR）", "不联网，识别质量一般，胜在隐私与零配置"),
        ("manual", "全人工确认（不联网）", "不识别，填空/解答全部进待确认队列，选择仍走 OMR"),
    )

    def _read_cfg_json() -> dict:
        try:
            with open(cfp, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_cfg_json(cfg: dict) -> str:
        """写 config.json：先留 .bak；其它键原样保留（只改调用方动过的那段）。"""
        backup = ""
        if os.path.isfile(cfp):
            backup = cfp + ".bak"
            try:
                shutil.copyfile(cfp, backup)
            except OSError:
                backup = ""
        with open(cfp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return backup

    def _settings_ctx(status_code: int = 200, notice=None, test_result=None):
        info = load_ocr_config(cfp)
        cfg = _read_cfg_json()
        ocr_cfg = cfg.get("ocr") if isinstance(cfg.get("ocr"), dict) else {}
        local = local_ocr_support()
        backends = []
        for key, label, hint in OCR_BACKENDS:
            item = {"key": key, "label": label, "hint": hint, "available": True, "note": ""}
            if key == "local" and not local["available"]:
                item["available"] = False
                item["note"] = "本机不支持（缺 winsdk）"
            backends.append(item)
        return dict(
            status_code=status_code, notice=notice, test_result=test_result,
            info=info, ocr_cfg=ocr_cfg, config_path=cfp,
            local_ocr=local,
            backends=backends,
            current=info.get("backend") or "manual",
            pdf_backend=probe_pdf_backend(),
            pdf_note=pdf_backend_note(probe_pdf_backend()),
            ocr_notice=_ocr_notice_ctx(),
            paths=app_paths.describe(cfp),
            env_override=bool(os.environ.get("OCR_BACKEND") or os.environ.get("OCR_TOKEN")
                              or os.environ.get("OCR_KEY")),
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(saved: str = "", cleared: str = ""):  # noqa: ANN202
        notice = None
        if saved:
            notice = {"kind": "ok", "text": "已保存（config.json 更新，旧版本备份为 config.json.bak）"}
        elif cleared:
            notice = {"kind": "ok", "text": "已清除该后端的 Key/Token"}
        ctx = _settings_ctx(notice=notice)
        ctx.pop("status_code", None)
        return render("settings.html", page_title="设置", nav_active="settings", **ctx)

    @app.post("/settings")
    async def settings_save(request: Request):  # noqa: ANN202
        form = await request.form()
        backend = str(form.get("backend") or "").strip()
        valid = {key for key, _label, _hint in OCR_BACKENDS}
        if backend not in valid:
            ctx = _settings_ctx(status_code=400, notice={
                "kind": "bad",
                "text": "后端名字不对：%r 不在 %s 里，没保存。" % (backend, " / ".join(sorted(valid))),
            })
            ctx.pop("status_code", None)
            return render("settings.html", status_code=400, page_title="设置",
                          nav_active="settings", **ctx)

        cfg = _read_cfg_json()
        ocr_cfg = cfg.get("ocr") if isinstance(cfg.get("ocr"), dict) else {}
        ocr_cfg["backend"] = backend
        clear = str(form.get("clear") or "").strip()

        def _text(key: str) -> str:
            return str(form.get(key) or "").strip()

        if backend == "paddle_vl":
            pv = ocr_cfg.get("paddle_vl") if isinstance(ocr_cfg.get("paddle_vl"), dict) else {}
            if clear == "paddle_vl":
                pv["token"] = ""
            elif _text("token"):
                pv["token"] = _text("token")
            for key in ("model", "job_url"):
                if _text(key):
                    pv[key] = _text(key)
            ocr_cfg["paddle_vl"] = pv
        elif backend == "vlm":
            vlm = ocr_cfg.get("vlm") if isinstance(ocr_cfg.get("vlm"), dict) else {}
            if clear == "vlm":
                vlm["key"] = ""
            elif _text("key"):
                vlm["key"] = _text("key")
            for key in ("base_url", "model"):
                if _text(key):
                    vlm[key] = _text(key)
            ocr_cfg["vlm"] = vlm
        cfg["ocr"] = ocr_cfg
        _write_cfg_json(cfg)
        if clear:
            return RedirectResponse("/settings?cleared=1", status_code=303)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/test", response_class=HTMLResponse)
    async def settings_test(request: Request):  # noqa: ANN202
        form = await request.form()
        backend = str(form.get("backend") or "").strip()
        valid = {key for key, _label, _hint in OCR_BACKENDS}
        if backend not in valid:
            return render("settings.html", status_code=400, page_title="设置",
                          nav_active="settings",
                          **_settings_ctx(notice={"kind": "bad", "text": "后端名字不对，没法测。"}))
        info = load_ocr_config(cfp)
        need = {"paddle_vl": (info.get("paddle_vl") or {}).get("token_present"),
                "vlm": (info.get("vlm") or {}).get("key_present")}.get(backend, True)
        if not need:
            ctx = _settings_ctx(test_result={
                "ok": False, "backend": backend, "elapsed_s": 0.0, "text": "",
                "error": "还没填 %s，先把 Key/Token 填进上面的框再测"
                         % ("token" if backend == "paddle_vl" else "key"),
            }, notice={"kind": "bad", "text": "缺 Key/Token，没发请求（不会 500）"})
            ctx.pop("status_code", None)
            return render("settings.html", page_title="设置", nav_active="settings", **ctx)
        if backend == "local" and not local_ocr_support()["available"]:
            support = local_ocr_support()
            ctx = _settings_ctx(test_result={
                "ok": False, "kind": "unsupported", "elapsed_s": 0.0, "text": "",
                "backend": "local", "error": support["error"],
                "message": support["detail"] + "。这不是 Key 的问题，换 paddle_vl / vlm，"
                                               "或先用 manual 全人工确认。",
            })
            ctx.pop("status_code", None)
            return render("settings.html", page_title="设置", nav_active="settings", **ctx)
        if backend == "manual":
            ctx = _settings_ctx(test_result={
                "ok": True, "backend": "manual", "elapsed_s": 0.0, "text": "",
                "error": "", "note": "manual 不联网：填空/解答留人工确认，选择题走 OMR 照常判",
            })
            ctx.pop("status_code", None)
            return render("settings.html", page_title="设置", nav_active="settings", **ctx)

        # 真跑一次识别：内置小图（12×2 的算式），30s 超时
        result = await run_in_threadpool(_ocr_selftest, backend)
        ctx = _settings_ctx(test_result=result)
        ctx.pop("status_code", None)
        return render("settings.html", page_title="设置", nav_active="settings", **ctx)

    def _ocr_selftest(backend: str) -> dict:
        started = time.time()
        tmp_png = os.path.join(out_root, "_settings_selftest.png")
        try:
            os.makedirs(os.path.dirname(tmp_png), exist_ok=True)
            make_selftest_image(tmp_png)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "backend": backend, "elapsed_s": 0.0, "text": "",
                    "kind": "error", "message": "生成测试图失败：%s: %s"
                                              % (type(exc).__name__, exc)}
        try:
            import ocr as _ocr
            rec = _ocr.Recognizer(config_path=cfp, backend=backend, use_env=False, timeout_s=30)
            res = rec.read(tmp_png, "test") or {}
            elapsed = round(time.time() - started, 2)
            text = str(res.get("text") or "").strip()
            error = str(getattr(rec, "last_error", "") or res.get("error") or "")
            got = str(res.get("backend") or backend)
            verdict = classify_ocr_probe(text=text, backend=got, error=error,
                                         elapsed=elapsed)
            verdict.update({"backend": got, "elapsed_s": elapsed, "text": text[:160],
                            "confidence": res.get("confidence"), "error": error})
            return verdict
        except Exception as exc:  # noqa: BLE001 - 测试连接不许 500
            return {"ok": False, "backend": backend,
                    "elapsed_s": round(time.time() - started, 2), "text": "",
                    "kind": "error", "error": "%s: %s" % (type(exc).__name__, exc),
                    "message": "测试没跑完：%s: %s（不是 Key 的问题，先看网络/依赖）"
                               % (type(exc).__name__, exc)}
        finally:
            try:
                os.remove(tmp_png)
            except OSError:
                pass

    # ---------------------------------------------------------------- 使用指南（纯静态，不碰库）
    @app.get("/guide", response_class=HTMLResponse)
    def guide():  # noqa: ANN202
        entries = [dict(code=code, **errs.explain(code)) for code in errs.CODES]
        return render("guide.html", page_title="使用指南", nav_active="guide",
                      error=None, warnings=[], entries=entries)

    # ---------------------------------------------------------------- 自检
    def _health_payload(request: Request) -> dict:
        payload = collect_health(
            dbp, cfp, out_root, grade_ok=getattr(app.state, "grade_routes_ok", None))
        lan = dict(_lan_ctx())
        # 访问码只给本机：/healthz 是诊断口（不拦 pin），LAN 上的设备不该从 JSON 里读到它
        if not _is_loopback_client(request):
            lan.pop("pin", None)
        payload["lan"] = lan
        payload["lan_url"] = app.state.lan_url      # 兼容只读这个字段的老脚本
        return payload

    @app.get("/healthz")
    def healthz(request: Request):  # noqa: ANN202
        # 诊断口永远 200：浏览器/脚本都能读到「哪里坏了」；健康与否看 ok / status。
        return JSONResponse(_health_payload(request))

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request):  # noqa: ANN202
        payload = _health_payload(request)
        return render("health.html", page_title="自检", nav_active="health",
                      h=payload, error=None, warnings=[],
                      json_pretty=json.dumps(payload, ensure_ascii=False, indent=2))

    # ---------------------------------------------------------------- 兜底：不把 traceback 丢给用户
    @app.exception_handler(sqlite3.Error)
    async def _on_sqlite_error(request: Request, exc: sqlite3.Error):  # noqa: ANN202
        return render("error.html", status_code=503, page_title="出错了",
                      error=_db_error(exc, dbp))

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_error(request: Request, exc: StarletteHTTPException):  # noqa: ANN202
        if exc.status_code == 404:
            error = _banner("页面不存在", f"没有这个地址：{request.url.path}",
                            ["回组卷首页：/", "看环境状态：/health"])
        else:
            error = _banner(f"请求失败（HTTP {exc.status_code}）", str(exc.detail),
                            ["刷新重试", "看环境状态：/health"])
        return render("error.html", status_code=exc.status_code, page_title="出错了", error=error)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):  # noqa: ANN202
        return render("error.html", status_code=422, page_title="参数不对",
                      error=_banner("提交的参数不合法", str(exc),
                                    ["刷新页面重新填写", "如果反复出现，把操作步骤记下来反馈"]))

    return app


# --------------------------------------------------------------------------- 入口

#: uvicorn 的目标：`python -m uvicorn server:app --host 127.0.0.1 --port 8710`
#: create_app() 只解析路径、挂 static，不碰数据库，所以 import 时构造是安全的。
app = create_app()


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    cfg = load_server_config()
    application = app
    print(f"[{APP_BRAND}] {APP_TITLE} v{APP_VERSION}")
    print(f"  数据库 {application.state.db_path}")
    print(f"  打开   {cfg['url']}    （Ctrl+C 停止）")
    uvicorn.run(application, host=cfg["host"], port=cfg["port"], log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
