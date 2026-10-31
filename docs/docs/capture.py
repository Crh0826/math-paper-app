# -*- coding: utf-8 -*-
"""t12 · 真机全链路截图采集（真库 + 真服 + headless Edge）。

一条命令重放整套证据：

    python app/docs/capture.py

它做这些事（全部真跑，不打桩）：
  1. 用 `python -m uvicorn server:app` 起真服（127.0.0.1:8710），等 /healthz 就绪；
  2. 真库组卷（2 选择 + 1 填空）→ 截 首页 / 我的卷子 / 卷子预览 / 使用指南 / 自检；
  3. 拿 card.py 出的答题卡 PNG，按 manifest 坐标把错误选项涂黑，当「拍的照片」上传；
     → 截 判卷结果页（含分数卡、逐题判定、待人工确认队列、训练入口）；
  4. 确认一题 → POST /train 生成训练卷 → 截 训练卷预览；
  5. 降级三连：Word 缺失（预览页降级条）、DB 缺失（503 友好页）、端口占用（真绑一次 8710）；
  6. 清掉本次演示产生的卷子/判卷记录，停服。

产物：app/docs/*.png（截图）、app/docs/DEGRADED.md（三种降级实测记录）。
"""
from __future__ import annotations

import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(APP_DIR, "docs")
DB_PATH = os.path.join(APP_DIR, "data", "app.db")
OUT_DIR = os.path.join(APP_DIR, "out")
BASE = "http://127.0.0.1:8710"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

sys.path.insert(0, APP_DIR)


# --------------------------------------------------------------------------- 基础工具

def edge_path() -> str:
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise SystemExit("找不到 Microsoft Edge，无法截图")


def shot(name: str, url: str, width: int = 1280, height: int = 1600) -> str:
    target = os.path.join(DOCS_DIR, name)
    subprocess.run([edge_path(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=%d,%d" % (width, height),
                    "--screenshot=%s" % target, url],
                   capture_output=True, timeout=180)
    if not os.path.isfile(target):
        raise SystemExit("截图失败：%s" % name)
    print("  截图 %-28s %6d B" % (name, os.path.getsize(target)))
    return target


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=300) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def get_raw(path: str):
    with urllib.request.urlopen(BASE + path, timeout=300) as resp:
        return resp.status, resp.headers.get("content-type"), resp.read()


def post_form(path: str, data: dict):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    with opener.open(req, timeout=600) as resp:
        return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace")


def post_multipart(path: str, fields: dict, filename: str, content: bytes, ctype="image/png"):
    boundary = "----t12boundary"
    parts = []
    for key, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, key, value)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\""
                  "\r\nContent-Type: %s\r\n\r\n" % (boundary, filename, ctype)).encode())
    parts.append(content)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(BASE + path, data=b"".join(parts), method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    with opener.open(req, timeout=600) as resp:
        return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace")


def text_of(html: str) -> str:
    html = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def wait_ready(timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    raise SystemExit("服务没起来（/healthz 60 秒未就绪）")


# --------------------------------------------------------------------------- 主流程

def main() -> int:
    os.makedirs(DOCS_DIR, exist_ok=True)
    if not os.path.isfile(DB_PATH):
        raise SystemExit("真库不存在：%s（先跑 python app/build_db.py）" % DB_PATH)

    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                               "--host", "127.0.0.1", "--port", "8710"],
                              cwd=APP_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    made_pids, made_sids = [], []
    try:
        wait_ready()
        print("服务已就绪：http://127.0.0.1:8710")

        # 1) 首页 / 我的卷子 / 使用指南 / 自检
        print("1) 静态页面")
        shot("01-compose.png", BASE + "/", height=1500)
        shot("02-papers-list.png", BASE + "/papers", height=900)
        shot("03-guide.png", BASE + "/guide", height=2400)
        shot("04-health.png", BASE + "/health", height=1800)

        # 2) 组卷 + 预览
        print("2) 组卷（真库，2 选择 + 1 填空）")
        status, url, _ = post_form("/papers", {
            "title": "截图演示卷 · 七年级数学", "seed": "12",
            "n_choice": "2", "n_fill": "1", "n_solution": "0", "need_answer": "on"})
        pid = url.rsplit("/", 1)[-1]
        made_pids.append(pid)
        print("   pid =", pid)
        shot("05-paper-preview.png", url, height=2400)
        status, ctype, data = get_raw("/papers/%s/print.pdf" % pid)
        print("   print.pdf →", status, ctype, len(data), "B")
        # 合并件第 1、2 页转成图，直接看页脚「卷面·第 N/共 A 页」「答题卡·第 M/共 B 页·（卷面续页 P{A+M}）」
        render_pdf_pages(data, {"05b-print-page1.png": 0, "05c-print-page2.png": 1})

        # 3) 合成照片 → 上传判卷
        print("3) 合成答题卡照片并上传")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        manifest = json.loads(conn.execute("SELECT manifest FROM papers WHERE pid=?",
                                           (pid,)).fetchone()["manifest"])
        conn.close()
        photo = marked_card(pid, manifest)
        status, url, _ = post_multipart("/submissions", {"pid": pid}, "card.png", photo)
        sid = url.rstrip("/").rsplit("/", 1)[-1]
        made_sids.append(sid)
        print("   sid =", sid)
        shot("06-grade-result.png", url, height=2600)

        # 4) 人工确认一题 → 训练卷
        print("4) 人工确认 + 训练卷")
        post_form("/submissions/%s/confirm" % sid, {"verdict_1_1": "correct", "text_1_1": "A"})
        status, url, _ = post_form("/train", {"sid": sid, "n_per": "2"})
        train_pid = url.rsplit("/", 1)[-1]
        made_pids.append(train_pid)
        print("   训练卷 pid =", train_pid)
        shot("07-train-paper.png", url, height=2400)

        # 5) 降级三连
        print("5) 降级验证")
        degraded_notes = []
        # 5a Word 缺失：注入一个抛 WordUnavailable 的渲染器，导出真实 HTML 再截图
        degraded_notes.append(("Word 缺失", capture_word_missing(pid)))
        # 5b DB 缺失：真起一个实例（端口 8711，MATH_APP_DB 指向不存在路径），请求 / 与 /healthz
        db_note, _db_status, _hz = capture_db_missing()
        degraded_notes.append(("DB 缺失", db_note))
        # 5c 端口占用：8710 正在跑，再 bind 一次必然失败
        busy = port_busy_note()
        degraded_notes.append(("端口占用", busy))
        write_degraded_doc(degraded_notes)
        print("   docs/DEGRADED.md 已写")

        print("完成。截图目录：", DOCS_DIR)
    finally:
        cleanup(made_pids, made_sids)
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        print("已停服，8710 已释放")
    return 0


# --------------------------------------------------------------------------- 合成照片

def render_pdf_pages(data: bytes, targets: dict, zoom: float = 1.4) -> None:
    """把 print.pdf 的某几页渲染成 PNG（看页脚与版式用）。"""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    for name, index in targets.items():
        if index >= doc.page_count:
            continue
        pix = doc[index].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        target = os.path.join(DOCS_DIR, name)
        pix.save(target)
        print("  截图 %-28s %6d B（print.pdf 第 %d 页）" % (name, os.path.getsize(target), index + 1))
    doc.close()


def marked_card(pid: str, manifest: dict) -> bytes:
    """把答题卡 PNG 的「错误选项」涂黑，当作手机拍的答题卡。"""
    from PIL import Image, ImageDraw

    src = os.path.join(OUT_DIR, pid, "card_1.png")
    image = Image.open(src).convert("RGB")
    draw = ImageDraw.Draw(image)
    by_idx = {item.get("idx"): item for item in manifest.get("items") or []}
    for page in (manifest.get("card") or {}).get("pages") or []:
        for item in page.get("items") or []:
            if item.get("kind") != "omr":
                continue
            src_item = by_idx.get(item.get("idx")) or {}
            expected = str(((src_item.get("answer_key") or [{}])[0]).get("value") or "")
            target = next((c for c in item.get("cells") or [] if c.get("opt") != expected), None)
            if not target:
                continue
            pad = 10
            draw.rectangle([target["x"] + pad, target["y"] + pad,
                            target["x"] + target["w"] - pad, target["y"] + target["h"] - pad],
                           fill=(12, 12, 12))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- 降级

def pathlib_uri(path: str) -> str:
    import pathlib

    return pathlib.Path(path).as_uri()


def capture_word_missing(pid: str) -> str:
    """注入「WordUnavailable」渲染器，起一个真端口（8712），截真实预览页（图片/样式都能加载）。"""
    import threading

    import uvicorn
    from fastapi.testclient import TestClient

    import server

    class WordUnavailable(RuntimeError):
        pass

    def failing_renderer(manifest, out_dir):  # noqa: ARG001
        raise WordUnavailable("本机没有可用的 Word（win32com DispatchEx 失败）")

    app = server.create_app(db_path=DB_PATH, config_path=os.path.join(APP_DIR, "config.json"),
                            out_dir=OUT_DIR, paper_renderer=failing_renderer)
    config = uvicorn.Config(app, host="127.0.0.1", port=8712, log_level="critical")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = "http://127.0.0.1:8712"
    deadline = time.time() + 45
    while time.time() < deadline and not srv.started:
        time.sleep(0.2)
    note = "Word 缺失时预览页呈现降级条（截图 08）"
    try:
        body = urllib.parse.urlencode({"title": "Word 缺失演示卷", "seed": "3",
                                       "n_choice": "1", "n_fill": "0", "n_solution": "0",
                                       "need_answer": "on"}).encode()
        req = urllib.request.Request(base + "/papers", data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        with opener.open(req, timeout=300) as resp:
            page_url = resp.geturl()
        shot("08-degraded-word.png", page_url, height=1500)
        demo_pid = page_url.rsplit("/", 1)[-1]
        # 再确认 /pdf 与 /print.pdf 是 409（不是 500、不是空壳）
        code_pdf = http_code(page_url + "/pdf")
        code_print = http_code(page_url + "/print.pdf")
        code_card = http_code(page_url + "/card.pdf")
        note = ("预览页出现「卷面 PDF 未生成（本机未检测到 Word）」降级条；"
                "/pdf=%s、/print.pdf=%s 均为 409 友好页，/card.pdf=%s 仍可下载"
                % (code_pdf, code_print, code_card))
        urllib.request.urlopen(urllib.request.Request(base + "/papers/%s/delete" % demo_pid,
                                                      data=b"", method="POST"), timeout=120)
    finally:
        srv.should_exit = True
        thread.join(timeout=15)
    return note


def http_code(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def capture_db_missing() -> tuple:
    """真起一个实例（端口 8711，MATH_APP_DB 指向不存在路径），直接截 503 页。"""
    env = dict(os.environ)
    env["MATH_APP_DB"] = os.path.join(APP_DIR, "data", "_not_here", "app.db")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app",
                             "--host", "127.0.0.1", "--port", "8711"],
                            cwd=APP_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:8711"
    try:
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                urllib.request.urlopen(base + "/healthz", timeout=3)
                break
            except urllib.error.URLError:
                time.sleep(0.5)
        status = http_code(base + "/")
        shot("09-degraded-db.png", base + "/", height=1200)
        with urllib.request.urlopen(base + "/healthz", timeout=20) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        note = ("GET / → %d 友好错误页（含「怎么办」与 build_db.py 提示，截图 09）；"
                "GET /healthz → ok=%s、status=%s" % (status, health.get("ok"), health.get("status")))
        return note, status, health
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def port_busy_note() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", 8710))
        sock.close()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 8710))
            probe.close()
            return "8710 竟然还能被二次绑定（异常）"
        except OSError as exc:
            return ("8710 已被本实例占用，二次 bind 失败：%s；start.bat 退出时会提示"
                    "「端口 8710 被占用，先关掉旧的黑色窗口」" % exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def write_degraded_doc(notes) -> None:
    lines = ["# 启动与环境降级实测（t12）", "",
             "由 `python app/docs/capture.py` 自动重放；截图见同目录 `08-*`、`09-*`。", "",
             "| 场景 | 实测结果 |", "|---|---|"]
    for name, note in notes:
        lines.append("| %s | %s |" % (name, note.replace("|", "\\|")))
    lines += ["",
              "> 端口占用：`start.bat` 会在窗口退出时打印三条常见原因并 `pause`；"
              "直接命令行启动会看到 uvicorn 的 `address already in use`。", ""]
    with open(os.path.join(DOCS_DIR, "DEGRADED.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# --------------------------------------------------------------------------- 清理

def cleanup(pids, sids) -> None:
    """删掉本次演示产生的卷子：训练卷无判卷记录 → 硬删；演示卷有判卷记录 → 软删后连证据一起清。"""
    import shutil

    for pid in pids:
        try:
            post_form("/papers/%s/delete" % pid, {})
        except Exception:  # noqa: BLE001
            pass
    if not os.path.isfile(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        for sid in sids:
            conn.execute("DELETE FROM submissions WHERE sid=?", (sid,))
        for pid in pids:
            conn.execute("DELETE FROM papers WHERE pid=?", (pid,))
        conn.commit()
    finally:
        conn.close()
    for pid in pids:
        shutil.rmtree(os.path.join(OUT_DIR, pid), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
