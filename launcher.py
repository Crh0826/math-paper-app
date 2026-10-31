# -*- coding: utf-8 -*-
"""便携版入口（PyInstaller onedir 打包）：双击即用，不装 Python、不装依赖。

职责（打包 ≥ 开发都走这一份逻辑）：

* **定目录**：把工作目录固定到 exe 所在目录；``qbank/``（题库 docx）、``data/app.db``、
  ``config.json``、``out/`` 全部用**exe 旁边的相对路径**，U 盘拔了插到别的电脑也能跑。
* **只读资源落地**：``config.json`` 与 ``data/app.db`` 打进包时是只读资源（在 ``_internal``），
  首次启动复制到 exe 旁边 —— 数据库要能写、访问码要能存回 config；已存在就不覆盖。
* **题库定位**：``MATH_QB_ROOT`` 显式指定 > ``<exe目录>/qbank`` > ``E:\\OCR``（开发机老路径），
  然后把 ``paper.SRC_ROOT`` 指过去（卷面/保真体检要从原始 docx 取公式与图片）。
* **起服务**：uvicorn 在工作线程里跑；端口被占自动 +1 重试；起来后再开浏览器
  （``MATH_NO_BROWSER=1`` 不自动开，命令行窗口也能看到地址）。
* **局域网**：``--lan`` 或 ``MATHAPP_LAN=1`` → 绑 0.0.0.0 并保证有 4 位访问码（复用 server 实现），
  打印手机可访问的地址；本机 127.0.0.1 永远免访问码。
* **降级提示**：没装 Word 只提示「卷面 docx 仍在，PDF 会降级」，不退出（组卷/判卷照常用）。
* **--check**：只体检打印（题库/数据库/配置/Word/端口），不启服务，方便排查与冒烟。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)          # E:\OCR（开发时）
FALLBACK_QB_ROOT = r"E:\OCR"                  # 开发机的题库老路径（仅回退用）
QB_DIRNAME = "qbank"
DB_REL = os.path.join("data", "app.db")
CONFIG_REL = "config.json"
OUT_REL = "out"
VERSION = "1.1"


# ---------------------------------------------------------------- 目录解析
def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


def base_dir() -> str:
    """用户可见的"程序目录"：打包版 = exe 所在目录；开发版 = app/。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return HERE


def bundle_dir() -> str:
    """只读资源根：打包版 = _internal（sys._MEIPASS）；开发版 = app/。"""
    return getattr(sys, "_MEIPASS", HERE)


def has_docx(folder: str) -> bool:
    """目录里有没有题库 docx（只看一层，qbank 是平的）。"""
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_file() and entry.name.lower().endswith(".docx"):
                    return True
    except OSError:
        return False
    return False


def resolve_qb_root(base: str = None, candidates=None) -> str | None:
    """题库目录：显式候选 > MATH_QB_ROOT > <base>/qbank > E:\\OCR；都没有返回 None。"""
    base = base or base_dir()
    if candidates is None:
        candidates = [os.environ.get("MATH_QB_ROOT"),
                      os.path.join(base, QB_DIRNAME),
                      FALLBACK_QB_ROOT]
    for path in candidates:
        if not path:
            continue
        try:
            if os.path.isdir(path) and has_docx(path):
                return os.path.abspath(path)
        except OSError:
            continue
    return None


def bootstrap_file(rel: str, base: str = None, bundle: str = None,
                   overwrite: bool = False) -> str:
    """把打包进 _internal 的只读资源复制到 exe 旁边（可写），返回目标路径。

    目标已存在就不覆盖（用户的数据库/配置不能被升级包冲掉）；返回的路径即便源缺失也
    照样给出，调用方自己判断存不存在。
    """
    base = base or base_dir()
    bundle = bundle or bundle_dir()
    dst = os.path.join(base, rel)
    if os.path.exists(dst) and not overwrite:
        return dst
    src = os.path.join(bundle, rel)
    if not os.path.exists(src):
        return dst
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    return dst


def prepare_paths(base: str = None, bundle: str = None, db: str = None,
                  config: str = None, out: str = None) -> dict:
    """算好本次运行用到的全部路径（不创建除 data/out 之外的目录）。"""
    base = base or base_dir()
    bundle = bundle or bundle_dir()
    info = {
        "base": base,
        "bundle": bundle,
        "qbank": resolve_qb_root(base),
        "db": os.path.abspath(db) if db else bootstrap_file(DB_REL, base, bundle),
        "config": os.path.abspath(config) if config else bootstrap_file(CONFIG_REL, base, bundle),
        "out": os.path.abspath(out) if out else os.path.join(base, OUT_REL),
    }
    return info


# ---------------------------------------------------------------- 端口 / 浏览器
def free_port(host: str, start: int, attempts: int = 20) -> int:
    """从 start 往上找一个能 bind 的端口；找不到抛 OSError。"""
    tried = []
    for port in range(int(start), int(start) + max(1, int(attempts))):
        family = socket.AF_INET6 if ":" in str(host) else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                tried.append(port)
                continue
    raise OSError("端口 %s~%s 都被占用了（试过：%s）"
                  % (start, int(start) + attempts - 1, tried[0] if tried else "?"))


def open_browser(url: str, env: dict = None, opener=None) -> bool:
    """自动开浏览器；``MATH_NO_BROWSER=1`` 时不打开（返回 False）。"""
    env = os.environ if env is None else env
    if str(env.get("MATH_NO_BROWSER") or "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    opener = opener or webbrowser.open
    try:
        opener(url)
        return True
    except Exception:              # noqa: BLE001 - 打不开浏览器不能影响服务
        return False


def wait_started(server, timeout: float = 20.0) -> bool:
    """等 uvicorn 真的 listening（uvicorn.Server.started）。"""
    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        if getattr(server, "started", False):
            return True
        if getattr(server, "should_exit", False):
            return False
        time.sleep(0.05)
    return bool(getattr(server, "started", False))


# ---------------------------------------------------------------- 环境装配
def apply_environment(info: dict) -> dict:
    """把路径写进环境变量（server/paper/render 都认这几个）+ 定向题库。"""
    os.environ["MATH_APP_DB"] = info["db"]
    os.environ["MATH_APP_CONFIG"] = info["config"]
    os.environ["MATH_APP_OUT"] = info["out"]
    if info.get("qbank"):
        os.environ["MATH_QB_ROOT"] = info["qbank"]
        os.environ["OCR_DOCX_ROOT"] = info["qbank"]
    return info


def point_paper_to_qbank(qb_root: str | None) -> bool:
    """把纸面排版读 docx 的根目录指到 qbank（paper.SRC_ROOT 是模块常量）。

    返回是否真的改了指过去（qbank 没找到就保持 E:\\OCR 原样，开发机照常）。
    """
    if not qb_root:
        return False
    try:
        import paper  # noqa: PLC0415 - 只在这里 import，--check 也用它
    except Exception:              # noqa: BLE001 - 打包缺件时也要能启动并报错
        return False
    try:
        paper.SRC_ROOT = os.path.abspath(qb_root)
        return True
    except Exception:              # noqa: BLE001
        return False


def _ensure_out_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 打印
def _say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except Exception:              # noqa: BLE001 - 控制台编码异常不能影响服务
        pass


def banner(info: dict, cfg: dict, port: int) -> None:
    _say("=" * 64)
    _say("  数学题库工作台 v%s  ·  组卷 / 答题卡 / 拍照判卷 / 错题训练" % VERSION)
    _say("  题库：沪科版七年级上 · 约 22,898 题（只读）")
    _say("=" * 64)
    _say("  数据库   %s" % info["db"])
    _say("  题库目录 %s" % (info["qbank"] or "未找到（卷面/PDF 会失败：把 qbank/ 放回程序目录）"))
    _say("  配置     %s" % info["config"])
    _say("  产物     %s" % info["out"])
    _say("")
    _say("  本机打开：http://127.0.0.1:%d/" % port)
    if cfg.get("lan"):
        ip_url = None
        try:
            import server  # noqa: PLC0415

            ip_url = server.lan_url(port, cfg.get("pin") or "")
        except Exception:          # noqa: BLE001
            ip_url = None
        if ip_url:
            _say("  手机/同 WiFi 打开：%s" % ip_url)
        if cfg.get("pin"):
            _say("  访问码：%s（只在本 WiFi 内有效；本机 127.0.0.1 不用输）" % cfg["pin"])
        _say("  提醒：公用 WiFi 别开 --lan；防火墙要放行本程序。")
    _say("")
    _say("  按 Ctrl+C 停止服务；关掉本窗口也可以。")
    _say("=" * 64)


def word_notice() -> bool:
    """没 Word 只提示降级，不退出。返回 Word 是否可用。"""
    try:
        import render  # noqa: PLC0415

        ok = bool(render.word_available())
    except Exception:              # noqa: BLE001
        ok = False
    if not ok:
        _say("  [!] 没检测到 Microsoft Word：卷面 docx 仍会生成，转 PDF 会失败/降级。")
        _say("      装 Word 2016+ 后重启本程序即可；组卷选题与判卷功能不受影响。")
    return ok


# ---------------------------------------------------------------- --check
def describe_environment(info: dict, argv: argparse.Namespace) -> int:
    """只体检不启动：给用户/冒烟脚本一个"到底哪里不对"的答案。"""
    _say("数学题库工作台 v%s · 环境自检" % VERSION)
    _say("  打包运行   %s" % ("是（exe）" if is_frozen() else "否（源码）"))
    _say("  程序目录   %s" % info["base"])
    _say("  只读资源   %s" % info["bundle"])
    _say("  题库       %s" % (info["qbank"] or "缺失"))
    _say("  数据库     %s%s" % (info["db"], "" if os.path.isfile(info["db"]) else "（缺失）"))
    _say("  配置       %s%s" % (info["config"], "" if os.path.isfile(info["config"]) else "（缺失）"))
    ok_word = word_notice()
    _say("  Word       %s" % ("可用" if ok_word else "不可用（PDF 会降级）"))
    host = argv.host or "127.0.0.1"
    try:
        port = free_port(host, argv.port or 8710)
        _say("  可用端口   %d" % port)
    except OSError as exc:
        _say("  端口       %s" % exc)
    qcount = 0
    if info["qbank"]:
        try:
            qcount = sum(1 for name in os.listdir(info["qbank"]) if name.lower().endswith(".docx"))
        except OSError:
            qcount = 0
    _say("  题库文件   %d 份 docx" % qcount)
    db_bytes = os.path.getsize(info["db"]) if os.path.isfile(info["db"]) else 0
    _say("  数据库大小 %.1f MB" % (db_bytes / 1048576.0))
    ok = bool(info["qbank"] and os.path.isfile(info["db"]) and os.path.isfile(info["config"]))
    _say("  结论       %s" % ("可以运行" if ok else "缺件：qbank/数据库/配置 至少缺一个"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程
def _load_cfg(config_path: str, host_override: str = None, port_override: int = None,
              lan: bool = False) -> dict:
    """读 config.json 的 server 段（host/port/pin），命令行/env 优先。"""
    if host_override:
        os.environ["MATH_APP_HOST"] = str(host_override)
    if port_override:
        os.environ["MATH_APP_PORT"] = str(port_override)
    if lan:
        os.environ["MATHAPP_LAN"] = "1"
    import server  # noqa: PLC0415

    return server.load_server_config(config_path)


def _pick_port(host: str, want: int, attempts: int = 20) -> int:
    """先看 want 空不空，被占就 +1（最多试 attempts 个）。"""
    return free_port(host, int(want or 8710), attempts)


def serve(app, host: str, port: int, open_url: str | None = None,
          no_browser: bool = False, ready_timeout: float = 25.0,
          on_ready=None) -> int:
    """在工作线程里跑 uvicorn；返回退出码（0 = 正常运行结束）。

    ``on_ready(server)`` 可选回调：服务真的 listening 后调用一次（测试里用它取证并优雅停止）。
    """
    import uvicorn  # noqa: PLC0415

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False, loop="asyncio", lifespan="auto")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    if not wait_started(server, ready_timeout):
        server.should_exit = True
        thread.join(5)
        _say("  [x] 服务没起来（端口 %d 可能被占用）。换端口重试：--port %d"
             % (port, port + 1))
        return 1
    if on_ready is not None:
        try:
            on_ready(server)
        except Exception:          # noqa: BLE001 - 回调出错也不能带崩服务
            pass
    if open_url and not no_browser:
        threading.Thread(target=lambda: (time.sleep(0.4), open_browser(open_url)),
                         name="browser", daemon=True).start()
    try:
        while thread.is_alive() and not server.should_exit:
            thread.join(0.2)
    except KeyboardInterrupt:
        _say("  正在停止服务…（Ctrl+C 已收到）")
        server.should_exit = True
        thread.join(5)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="数学题库工作台", description="题库组卷 · 答题卡生成 · 拍照判卷 · 错题训练（便携版）")
    parser.add_argument("--host", default=None, help="监听地址（默认读 config.json；--lan 用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None, help="端口（默认 8710；被占自动 +1）")
    parser.add_argument("--lan", action="store_true", help="开放到局域网（手机拍照判卷用，需访问码）")
    parser.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    parser.add_argument("--db", default=None, help="指定 app.db（默认程序目录 data/app.db）")
    parser.add_argument("--config", default=None, help="指定 config.json")
    parser.add_argument("--out", default=None, help="指定产物目录（默认程序目录 out/）")
    parser.add_argument("--qbank", default=None, help="指定题库 docx 目录（默认程序目录 qbank/）")
    parser.add_argument("--check", action="store_true", help="只自检并打印环境，不启动服务")
    args = parser.parse_args(argv)

    base = base_dir()
    try:
        os.chdir(base)             # 让相对路径、模板查找都落在程序目录
    except OSError:
        pass
    if args.qbank:
        os.environ["MATH_QB_ROOT"] = os.path.abspath(args.qbank)
    info = prepare_paths(base=base, db=args.db, config=args.config, out=args.out)
    if args.qbank and os.path.isdir(args.qbank):
        info["qbank"] = os.path.abspath(args.qbank)

    if args.check:
        # 自检不能改环境/建目录：不然同一个进程里连调两次会互相污染（测试也依赖这点）
        return describe_environment(info, args)

    apply_environment(info)
    point_paper_to_qbank(info.get("qbank"))
    _ensure_out_dir(info["out"])

    cfg = _load_cfg(info["config"], args.host or None, args.port, args.lan)
    host = "0.0.0.0" if args.lan else cfg["host"]
    pin = ""
    if cfg.get("lan"):
        try:
            import server  # noqa: PLC0415

            pin = server.ensure_lan_pin(info["config"], log=_say)
        except Exception:          # noqa: BLE001 - 访问码生成失败不挡启动
            pin = cfg.get("pin") or ""
        cfg["pin"] = pin or cfg.get("pin") or ""

    try:
        port = _pick_port(host, cfg["port"] or 8710)
    except OSError as exc:
        _say("  [x] %s" % exc)
        return 1
    if port != int(cfg["port"] or 8710):
        _say("  [!] 端口 %s 被占用，改用 %d" % (cfg["port"], port))

    import server  # noqa: PLC0415

    app = server.create_app(db_path=info["db"], config_path=info["config"],
                            out_dir=info["out"])
    banner(info, cfg, port)
    word_notice()
    local_url = "http://127.0.0.1:%d/" % port
    return serve(app, host, port, open_url=local_url, no_browser=bool(args.no_browser))


if __name__ == "__main__":
    raise SystemExit(main())
