# -*- coding: utf-8 -*-
"""T6 · Word 渲染：docx → PDF（Word COM）→ PNG（PyMuPDF）。

契约：CONTRACT §4

    docx_to_pdf(docx_path, pdf_path) -> (pages, seconds)
    pdf_to_png(pdf_path, out_dir, dpi=300) -> list[str]

设计要点
--------
* **只读、不碰源文件**：docx 先复制到系统 temp 下的 ASCII 临时目录再交给 Word。
  中文/空格路径、Word 的 ``~$`` 属主文件、被占用产生的锁文件全落在 temp 里，转换结束
  整个目录删掉 —— ``app/`` 目录下永远不留临时 docx/pdf。
* **Word 不在就降级，不让 COM 报错穿透**：先探测 WINWORD.EXE（PATH → App Paths 注册表
  → 常见安装目录），没有就抛 ``WordUnavailable("not_installed")``；DispatchEx 失败、
  COM 断连同样收敛成 ``WordUnavailable``（带机器可读的 ``reason``，供 errors.py 写文案）。
* **看门狗**：COM 调用放在工作线程里，主线程 join ``WORD_TIMEOUT_S``（90s）；超时后按
  转换前快照 diff 出本次新起的 WINWORD.EXE 并 ``taskkill /F``，抛
  ``WordUnavailable("timeout")``。同一进程内 Word 转换串行（``_WORD_LOCK``），
  这样"谁是我起的 Word"永不含糊。
* **落位（关键坑）**：Windows 上 ``os.replace`` 跨卷必报 ``ERROR_NOT_SAME_DEVICE(17)``
  （用户的真实布局：``%TEMP%`` 在 C:、``app/out`` 在 E:）。所以 PDF 先写在 temp 里，落位时
  **同卷**直接 ``os.replace``；**跨卷**先 ``shutil.copyfile`` 到目标卷上的 ``<dst>.part-*``，
  再在目标卷内 ``os.replace`` 原子顶上。目标被占用（32/5）重试并自动去掉只读属性；仍失败
  抛 ``RenderError``，文案区分"占用 / 无权限 / 跨卷"并带「源盘 → 目标盘」。转换前还会用
  DELETE 权限探针预检目标 PDF，被 Word/阅读器锁住就立刻报错（不等 Word 转完才失败）。
* **崩溃孤儿**：父进程被强杀（Ctrl-C / 测试超时）时 ``finally`` 不会执行，Word 和 temp 目录
  会留下来。临时目录里写 ``meta.json``（pid + 创建时刻），下次转换开始时 ``_sweep_orphan_word``
  只清「属主已死 + 超龄」的目录，并且只杀"无可见窗口 + 存活超过阈值 + 创建时刻紧跟该目录"的
  WINWORD.EXE —— 用户自己开着的 Word、别人正在跑的转换都不会被误伤（拿不到目录证据时宁可
  留一个隐藏孤儿，也不乱杀）。
* **PNG 归一**：300dpi + A4 时用精确 zoom 渲染到 2480×3508（CONTRACT §3 规范像素），
  只在极端取整偏差时补一次 LANCZOS；页序严格等于 PDF 页序，命名 ``page-01.png`` 起。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pymupdf

try:                                   # pywin32 只做 import，不在这里启动 Word
    import win32com.client as win32
except Exception:                      # noqa: BLE001 - 非 Windows / 没装 pywin32
    win32 = None

# ---------------------------------------------------------------- 常量
PAGE_PX = (2480, 3508)                 # A4 @300dpi（CONTRACT §3 的规范像素）
PAGE_PT = (595.3, 841.9)               # A4
A4_TOLERANCE_PT = 2.0
WORD_TIMEOUT_S = 90.0                  # 看门狗：Word 超过这么久没返回就强杀
PDF_MOVE_RETRIES = 20                  # 目标 PDF 被占用时的重试次数（20 × 0.15s = 3s）
PDF_MOVE_DELAY = 0.15
TEMP_PREFIX = "mathapp-render-"        # 转换临时目录前缀（系统 temp 下）
TEMP_MAX_AGE_S = 24 * 3600             # 崩溃残留的临时目录，超过一天就清掉
MIN_PDF_BYTES = 1000

#: COM「服务器没了」类错误（Word 被关掉/崩了），这类要收敛成 WordUnavailable
_RPC_DEAD_HRESULTS = {
    0x800706BE,   # RPC_S_CALL_FAILED
    0x800706BF,   # RPC_S_CALL_FAILED_DNE
    0x800706BA,   # RPC_S_SERVER_UNAVAILABLE
    0x80080005,   # CO_E_SERVER_EXEC_FAILURE
    0x80010108,   # RPC_E_DISCONNECTED
}

_WORD_LOCK = threading.Lock()          # 进程内 Word 转换串行，保证 PID 快照 diff 干净
_word_path_cache = "unset"

#: 跨进程串行：Word 是单实例最省心的用法；测试/演示/服务同时跑时按锁排队，
#: 避免"别人的 Word"被算到自己头上、也避免一堆隐藏 Word 抢内存。
def lock_path(lock_dir: str = None) -> str:
    """跨进程锁文件路径：``lock_dir`` 参数 > 环境变量 ``MATHAPP_LOCK_DIR`` > 系统 temp。

    可注入是为了**测试隔离**：用例用自己的临时目录做锁，天然不受别人正在导 PDF 的影响。
    """
    base = lock_dir or os.environ.get("MATHAPP_LOCK_DIR") or tempfile.gettempdir()
    return os.path.join(base, "mathapp-render.lock")


LOCK_PATH = lock_path()                # 默认路径（测试/演示可换成自己的目录）
_lock_depth = 0
_lock_state = {"skipped": 0}
_spawned_pids = []

try:
    import msvcrt
except ImportError:                    # 非 Windows
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None


# ---------------------------------------------------------------- 异常
class RenderError(RuntimeError):
    """渲染失败（文件不存在、Word 转换报错、目标被占用、PDF 坏了…）。"""


class WordUnavailable(RenderError):
    """本机没有可用的 Word / Word 卡死 —— 上层据此降级（errors.py 的 WordUnavailable）。

    ``reason`` 是机器可读的短码，当前取值：
    ``not_installed`` / ``pywin32_missing`` / ``dispatch_failed`` / ``com_failed`` / ``timeout``
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = str(reason or "unknown")
        self.detail = str(detail or "")
        message = "Word 不可用（%s）" % self.reason
        if self.detail:
            message += "：" + self.detail
        super().__init__(message)


# ---------------------------------------------------------------- 小工具
def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return ("%s: %s" % (type(exc).__name__, text))[:limit]


def _hresult(exc: BaseException):
    hr = getattr(exc, "hresult", None)
    if hr is None and exc.args and isinstance(exc.args[0], int):
        hr = exc.args[0]
    return (hr & 0xFFFFFFFF) if isinstance(hr, int) else None


def _com_dead(exc: BaseException) -> bool:
    return type(exc).__name__.lower() == "com_error" and _hresult(exc) in _RPC_DEAD_HRESULTS


def _convert_error(step: str, exc: BaseException) -> RenderError:
    """转换中途的异常 → 明确错误：COM 断连算 Word 不可用，其它算渲染失败。"""
    if _com_dead(exc):
        return WordUnavailable("com_failed", "%s时 COM 断连：%s" % (step, _short(exc)))
    return RenderError("%s失败（Word COM）：%s" % (step, _short(exc)))


# ---------------------------------------------------------------- Word 探测
def _winword_candidates() -> list:
    """可能的 WINWORD.EXE 路径：PATH → App Paths 注册表 → 常见安装目录。"""
    found = []
    which = shutil.which("WINWORD.EXE")
    if which:
        found.append(which)
    if sys.platform == "win32":
        try:
            import winreg

            app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"
            keys = [
                (winreg.HKEY_LOCAL_MACHINE, app_paths),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE"),
                (winreg.HKEY_CURRENT_USER, app_paths),
            ]
            for root, sub in keys:
                try:
                    with winreg.OpenKey(root, sub) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                except OSError:
                    continue
                if value:
                    found.append(os.path.expandvars(str(value).strip('"')))
            # ClickToRun 安装（Office 2016+ 走这里，InstallPath 里有 root\Office16）
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration") as key:
                    install = winreg.QueryValueEx(key, "InstallPath")[0]
            except OSError:
                install = ""
            if install:
                for sub in ("root\\Office16", "Office16", "root\\Office15", "Office15"):
                    found.append(os.path.join(str(install), sub, "WINWORD.EXE"))
        except ImportError:            # 剪裁过的 stdlib
            pass
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not base:
                continue
            for office in ("Office16", "Office15", "Office14"):
                found.append(os.path.join(base, "Microsoft Office", "root", office, "WINWORD.EXE"))
                found.append(os.path.join(base, "Microsoft Office", office, "WINWORD.EXE"))
    return found


def _find_word_exe(candidates=None) -> str | None:
    """只做路径探测，绝不启动 Word。"""
    for path in (candidates if candidates is not None else _winword_candidates()):
        if not path:
            continue
        try:
            if os.path.isfile(path) and os.path.basename(path).lower() == "winword.exe":
                return os.path.abspath(path)
        except OSError:
            continue
    return None


def word_path(refresh: bool = False) -> str | None:
    """WINWORD.EXE 路径（缓存）；找不到返回 None。"""
    global _word_path_cache
    if refresh or _word_path_cache == "unset":
        _word_path_cache = _find_word_exe()
    return _word_path_cache


def word_available(refresh: bool = False) -> bool:
    """本机能不能用 Word 导 PDF（pywin32 可 import + 找得到 WINWORD.EXE）。"""
    return bool(win32 is not None and word_path(refresh=refresh))


def _require_word() -> str:
    """没有 Word 就抛 WordUnavailable —— 不让 COM 的 com_error 穿透到上层。"""
    if win32 is None:
        raise WordUnavailable("pywin32_missing",
                              "import win32com.client 失败：python -m pip install pywin32")
    path = _find_word_exe()
    if not path:
        raise WordUnavailable("not_installed",
                              "找不到 WINWORD.EXE（本机没装 Microsoft Word 2016+）")
    return path


def _dispatch_word():
    if win32 is None:
        raise WordUnavailable("pywin32_missing", "import win32com.client 失败")
    return win32.DispatchEx("Word.Application")


def _quiet_word(app) -> None:
    """别弹窗、别改用户设置：隐藏窗口 + 关掉所有对话框 + 禁宏。"""
    for name, value in (("Visible", False), ("DisplayAlerts", 0),
                        ("AutomationSecurity", 3), ("ScreenUpdating", False)):
        try:
            setattr(app, name, value)
        except Exception:              # noqa: BLE001 - 老版本/受限对象没有这个属性
            pass
    try:
        app.Options.UpdateLinksAtOpen = False
    except Exception:                  # noqa: BLE001
        pass


# ---------------------------------------------------------------- WINWORD 进程
def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 跨进程锁


@contextlib.contextmanager
def _cross_process_lock(timeout: float = None, required: bool = False, lock_dir: str = None):
    """跨进程串行 Word 转换（Windows 用 msvcrt 字节锁，类 Unix 用 flock）。

    **默认 best-effort**：等 ``timeout`` 秒还拿不到就放行（生产里只有一个进程，锁只是
    "少起几个隐藏 Word"的优化；多人跑测试时死等会让一条命令看起来卡住）。
    ``required=True`` 时拿不到抛 ``WordUnavailable("busy")``（测试要用它保证独占）。
    ``lock_dir`` 可把锁文件放到自己的临时目录，测试因此完全不受并发影响。

    同进程内可重入（嵌套直接放行，``_WORD_LOCK`` 已经把本进程的转换串起来了）。
    """
    global _lock_depth
    timeout = (max(10.0, min(WORD_TIMEOUT_S, 20.0)) if timeout is None else float(timeout))
    if _lock_depth > 0:                # 同进程重入
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    path = lock_path(lock_dir)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:                    # 锁文件都建不了：best-effort 直接放行
        if required:
            raise WordUnavailable("busy", "建不了跨进程锁文件：%s" % path)
        _lock_state["skipped"] += 1
        yield
        return
    acquired = False
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            if msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            if time.time() >= deadline:
                break
            time.sleep(0.1)
    if not acquired:
        try:
            os.close(fd)
        except OSError:
            pass
        if required:
            raise WordUnavailable("busy", "另一个转换正占用 Word，%.0f 秒内没拿到跨进程锁"
                                  % timeout)
        _lock_state["skipped"] += 1
        yield                          # best-effort：照样干，只是可能和别的进程并发
        return
    _lock_depth = 1
    try:
        yield
    finally:
        _lock_depth = 0
        try:
            if msvcrt is not None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def last_lock_skipped() -> int:
    """因为别的进程占着锁而跳过串行的次数（排查"为什么这次没排队"用）。"""
    return int(_lock_state["skipped"])


def last_spawned_pids() -> list:
    """本次/上次转换真正新起的 WINWORD.EXE PID（测试与排查用；不含别人起的）。"""
    return list(_spawned_pids)


def _new_word_pids(pids_before, timeout: float = 2.0) -> set:
    """等本次 DispatchEx 起的 Word 出现在 tasklist 里（最多等 timeout 秒）。"""
    before = {int(p) for p in pids_before}
    deadline = time.time() + max(0.0, timeout)
    while True:
        new = set(_word_pids()) - before
        if new or time.time() >= deadline:
            return new
        time.sleep(0.1)


def _word_pids() -> list:
    """当前所有 WINWORD.EXE 的 PID（tasklist；拿不到就空表，绝不抛）。"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=_no_window(),
        ).stdout
    except Exception:                  # noqa: BLE001
        return []
    pids = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "winword.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def _live_word_pids(pids) -> list:
    """给定 PID 里现在还活着的（交集）。"""
    alive = set(_word_pids())
    return sorted(int(p) for p in pids if int(p) in alive)


def _wait_word_gone(pids, timeout: float = 10.0, interval: float = 0.1) -> bool:
    """等这些 WINWORD.EXE 真正退出。

    ``Application.Quit()`` 是异步的：它返回时进程可能还没退干净、还锁着 temp 里的
    docx/~$ 文件。不等就会留下"删不掉的临时目录"，也会让 ``Get-Process WINWORD``
    短暂看到孤儿。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        if not _live_word_pids(pids):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def _kill_pids(pids) -> list:
    """按 PID 强杀（只杀点名的那些，不碰别人的 Word）。"""
    killed = []
    for pid in sorted({int(p) for p in pids}):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=15,
                           creationflags=_no_window())
            killed.append(pid)
        except Exception:              # noqa: BLE001
            continue
    return killed


def _kill_word_processes(pids_before=(), fallback_all: bool = True) -> list:
    """强杀本次转换新起的 WINWORD.EXE，返回被杀掉的 PID。

    只杀「转换前快照里没有、现在有」的进程，避免误杀用户自己开着的 Word 文档。
    ``fallback_all`` 只在看门狗超时时为真：那时确实有一个卡死的 Word 必须清掉，快照
    抓不到（tasklist 挂了/为空）就退化成按映像名杀；转换失败后的自清理走 ``fallback_all=False``，
    宁可留个孤儿也不误杀用户正在编辑的 Word。
    """
    current = _word_pids()
    targets = sorted(set(current) - set(int(p) for p in pids_before))
    if not targets and fallback_all:
        targets = sorted(current)      # 兜底：快照没抓到（或为空），按映像名清
    killed = _kill_pids(targets)
    if not killed and not targets and fallback_all:
        try:
            subprocess.run(["taskkill", "/F", "/IM", "WINWORD.EXE"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=_no_window())
        except Exception:              # noqa: BLE001
            pass
    return killed


# ---------------------------------------------------------------- 临时目录
def _rmtree(path: str, attempts: int = 10, delay: float = 0.15) -> None:
    """删临时目录。

    Word 有时晚一拍才放开文件句柄；Windows 把目录标记为「删除挂起」时名字还会短暂存在
    （此时列不出内容）。所以：删 → 名字没了或列不出内容 → 算干净。
    """
    if not path or not os.path.isdir(path):
        return
    for _ in range(max(1, attempts)):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        try:
            os.listdir(path)
        except OSError:                # 删除挂起：系统已经删了，只是名字还没消失
            return
        time.sleep(delay)
    shutil.rmtree(path, ignore_errors=True)


def _sweep_stale_temp(max_age_s: float = TEMP_MAX_AGE_S) -> int:
    """清掉上次崩溃留下的 mathapp-render-* 临时目录（只清超龄的）。"""
    root = tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for name in names:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path) or now - os.path.getmtime(path) < max_age_s:
                continue
        except OSError:
            continue
        _rmtree(path)
        removed += 1
    return removed


# ---------------------------------------------------------------- 崩溃孤儿清扫
#: 转换到一半父进程被强杀（Ctrl-C / 测试超时 / 进程崩溃）时，Word 和 temp 目录会留下来。
#: 这种孤儿 finally 管不到，只能下次转换开始时清 —— 但绝不能误杀用户自己开着的 Word，
#: 所以三重条件：① 目录属主进程已死；② Word 是目录创建之后才起的；③ Word 没有可见窗口。
ORPHAN_MIN_AGE_S = 600             # 10 分钟还活着的转换一定不正常
_META_NAME = "meta.json"


def _write_meta(tmp_dir: str) -> None:
    """在临时目录里留个属主标记（pid + 创建时间），供崩溃后的孤儿清扫判断。"""
    try:
        with open(os.path.join(tmp_dir, _META_NAME), "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "ts": time.time()}, fh)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """进程还活着吗（Windows：OpenProcess + GetExitCodeProcess；绝不发信号）。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                  # noqa: BLE001
        return True
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                  # noqa: BLE001
        return True


def _process_creation_time(pid: int):
    """进程创建时间（Unix 时间戳）；拿不到返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                  # noqa: BLE001
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, kernel_t, user_t = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                            ctypes.byref(exited),
                                            ctypes.byref(kernel_t),
                                            ctypes.byref(user_t)):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ticks / 1e7 - 11644473600.0     # FILETIME(1601) → Unix
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                  # noqa: BLE001
        return None


def _visible_window_pids() -> set:
    """有可见顶层窗口的 PID（用户正开着的 Word 一定在里面 → 不许杀）。"""
    if sys.platform != "win32":
        return set()
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                  # noqa: BLE001
        return set()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    pids = set()
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.add(int(pid.value))
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
    except Exception:                  # noqa: BLE001
        return set()
    return pids


def _sweep_orphan_word(max_age_s: float = ORPHAN_MIN_AGE_S) -> dict:
    """清"转换进程被杀"留下的 Word + temp 目录；返回 {"dirs": n, "killed": [pid]}。

    只处理 **属主进程已死** 且存在超过 ``max_age_s`` 的目录；杀 Word 还要额外满足
    "创建时间在目录之后"且"没有可见窗口"，所以用户自己开着的 Word 不会被误伤。
    """
    root = tempfile.gettempdir()
    report = {"dirs": 0, "killed": []}
    try:
        names = os.listdir(root)
    except OSError:
        return report
    now = time.time()
    visible = _visible_window_pids()
    for name in names:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path) or now - os.path.getmtime(path) < max_age_s:
                continue
            meta = {}
            meta_path = os.path.join(path, _META_NAME)
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        meta = json.load(fh) or {}
                except (OSError, ValueError):
                    meta = {}
            started = float(meta.get("ts") or os.path.getmtime(path))
        except OSError:
            continue
        owner = meta.get("pid")
        if owner and _pid_alive(int(owner)):
            continue                   # 属主还活着：可能只是跑得久，别动
        _rmtree(path)
        if not os.path.isdir(path) or not os.listdir(path):
            report["dirs"] += 1
        # 父进程死了，但它起的 Word 可能还赖着（doc 已关、Quit 没执行）：只杀"隐藏窗口 +
        # 存在时间超过阈值 + 创建时刻紧跟这个目录"的，宁可漏杀也不误伤用户开关的 Word。
        for pid in _word_pids():
            if pid in visible:
                continue
            created = _process_creation_time(pid)
            if created is None or now - created < max_age_s:
                continue
            if not (started - 5.0 <= created <= started + 120.0):
                continue
            if _kill_pids([pid]):
                report["killed"].append(pid)
        _rmtree(path)
    return report


# ---------------------------------------------------------------- 落位 / 校验
def _same_volume(a: str, b: str) -> bool:
    """两个路径是否在同一卷（盘符）；无盘符的路径按同卷处理，交给 os.replace 自己判断。"""
    try:
        da = os.path.splitdrive(os.path.abspath(str(a)))[0]
        db = os.path.splitdrive(os.path.abspath(str(b)))[0]
    except Exception:                  # noqa: BLE001 - 路径怪就按同卷试
        return True
    return da.lower() == db.lower()


def _write_error(dst: str, exc: BaseException, src: str = "") -> RenderError:
    """落位失败 → 说清到底是什么错（别把跨卷当成「被占用」，也别把占用说成没权限）。"""
    winerror = getattr(exc, "winerror", None)
    drives = ""
    if src:
        drives = "（源盘 %s → 目标盘 %s）" % (
            os.path.splitdrive(os.path.abspath(src))[0] or "?",
            os.path.splitdrive(os.path.abspath(dst))[0] or "?")
    if winerror in (5, 32, 33):
        name = {5: "ERROR_ACCESS_DENIED", 32: "ERROR_SHARING_VIOLATION",
                33: "ERROR_LOCK_VIOLATION"}[winerror]
        return RenderError("目标 PDF 被占用或没有写权限（%s %s）：%s%s（%s）"
                           % (name, winerror, dst, drives, _short(exc)))
    if winerror == 17:
        return RenderError("跨卷移动失败（ERROR_NOT_SAME_DEVICE 17）：%s%s（%s）"
                           % (dst, drives, _short(exc)))
    return RenderError("PDF 落位失败：%s%s（%s）" % (dst, drives, _short(exc)))


def _probe_replaceable(path: str) -> int:
    """这个文件现在能不能被替换/删除？返回 0 = 可以，否则返回 WinError。

    ``os.replace`` 能否成功取决于**所有已打开的句柄是否都允许删除（FILE_SHARE_DELETE）**，
    这是"能写"查不出来的（Python 的 open 只共享读写）。所以这里用 DELETE 权限打开一次
    （不带 FILE_FLAG_DELETE_ON_CLOSE，CloseHandle 后文件原样还在）：打得开 = 能替换。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                  # noqa: BLE001
        return 0
    DELETE = 0x00010000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    invalid = wintypes.HANDLE(-1).value
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                           wintypes.HANDLE]
        handle = create(str(path), DELETE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if handle is None or handle == invalid:
            return int(ctypes.get_last_error() or 1)
        kernel32.CloseHandle(handle)
        return 0
    except Exception:                  # noqa: BLE001 - 探测失败就当能写，别卡主流程
        return 0


def _preflight_target(pdf_path: str) -> None:
    """落位前先看目标 PDF 能不能被替换：被 Word/WPS/PDF 阅读器占着就立刻说清楚。

    否则要等 Word 转完（好几秒）才在最后一步报"被占用"，用户还以为是 Word 出问题。
    """
    if not os.path.exists(pdf_path):
        return
    winerror = _probe_replaceable(pdf_path)
    if winerror == 5:                  # 只读属性也会报 ACCESS_DENIED：先去掉再探
        try:
            os.chmod(pdf_path, 0o666)
        except OSError:
            pass
        winerror = _probe_replaceable(pdf_path)
    if winerror == 0:
        return
    hint = ""
    pids = _word_pids()
    if pids:
        hint = "；系统里有 %d 个 Word 进程（PID %s），可能就是它们锁着" % (
            len(pids), ", ".join(str(p) for p in pids[:5]))
    raise RenderError(
        "目标 PDF 正被别的程序占用，写不进去：%s。请关掉打开它的程序"
        "（Word / WPS / PDF 阅读器）后重试%s。（WinError %s）"
        % (pdf_path, hint, winerror))


def _make_target_replaceable(dst: str) -> None:
    """把挡路的目标 PDF 变成可覆盖：去掉只读属性；占用它的程序允许删时直接删掉。"""
    try:
        if os.path.exists(dst) and not os.access(dst, os.W_OK):
            os.chmod(dst, 0o666)
    except OSError:
        pass
    try:
        if os.path.exists(dst):
            os.remove(dst)
    except OSError:
        pass                           # 还被占用：留着让 os.replace 继续重试


def _move_into_place(src: str, dst: str, retries: int = PDF_MOVE_RETRIES,
                     delay: float = PDF_MOVE_DELAY) -> str:
    """把 temp 里的 PDF 搬到位；目标被占用就重试，仍失败抛明确的 RenderError。

    落位策略（Windows 跨卷 os.replace 必报 ERROR_NOT_SAME_DEVICE=17，不能直接用）：
      * **同卷**：直接 ``os.replace``（原子 rename，最省事）；
      * **跨卷**（用户真实布局：TEMP 在 C:、app/out 在 E:）：先把 PDF 拷到目标卷上的
        ``<dst>.part-*``，再在目标卷内 ``os.replace`` 原子顶上 —— 等价于 shutil.move 的
        copy+unlink，但保留了"目标被占用时重试"和"原子可见"两个好处；
      * 目标被别的程序打开（ERROR_SHARING_VIOLATION 32）→ 重试；带只读属性 → 先 chmod；
        打开它的程序允许删除 → 先删旧再落位。

    失败时 **src 原样保留**（调用方还能重试落位），绝不静默丢产物；错误信息区分
    占用 / 无权限 / 跨卷，不谎报。
    """
    if not os.path.isfile(src):
        raise RenderError("要落位的 PDF 不存在：%s" % src)

    staging = None
    source = src
    if not _same_volume(src, dst):
        staging = "%s.part-%d-%d" % (dst, os.getpid(), threading.get_ident())
        try:
            shutil.copyfile(src, staging)      # 先拷到目标卷（同卷拷贝，后续 rename 才是原子的）
        except OSError as exc:                 # 目标盘满/无权限
            try:
                os.remove(staging)
            except OSError:
                pass
            raise _write_error(dst, exc, src) from exc
        source = staging

    last = None
    try:
        for attempt in range(max(1, int(retries))):
            try:
                os.replace(source, dst)
                if staging is not None:        # 跨卷：temp 里的原件已经拷完了，删掉
                    try:
                        os.remove(src)
                    except OSError:
                        pass
                return dst
            except OSError as exc:
                last = exc
                if getattr(exc, "winerror", None) not in (None, 5, 32, 33):
                    raise _write_error(dst, exc, src) from exc
                if attempt % 5 == 4:
                    _make_target_replaceable(dst)
                time.sleep(delay)
        raise _write_error(dst, last, src)
    finally:
        if staging is not None and os.path.exists(staging):
            try:
                os.remove(staging)
            except OSError:
                pass


def _pdf_page_count(pdf_path: str) -> int:
    """PDF 实际页数；读不了返回 0（不抛）。"""
    try:
        doc = pymupdf.open(pdf_path)
    except Exception:                  # noqa: BLE001
        return 0
    try:
        return int(doc.page_count)
    except Exception:                  # noqa: BLE001
        return 0
    finally:
        doc.close()


# ---------------------------------------------------------------- Word COM 转换
def _convert_blocking(docx_path: str, pdf_path: str) -> int:
    """在工作线程里真跑 Word：打开 → 导出 PDF → 关文档 → 退 Word。返回 Word 报的页数。

    收尾保证不留孤儿：``Quit()`` 失败（Word 正在弹窗/半死）时，只杀「本线程 DispatchEx
    之前不存在」的 WINWORD.EXE，不碰用户自己开着的 Word。
    """
    import pythoncom

    pythoncom.CoInitialize()
    app = None
    doc = None
    pids_before = _word_pids()
    own_pids = set()                   # 本次 DispatchEx 新起的 Word（收尾只动它）
    quit_failed = False
    try:
        try:
            app = _dispatch_word()
        except WordUnavailable:
            raise
        except Exception as exc:       # noqa: BLE001 - DispatchEx 失联/未注册
            raise WordUnavailable("dispatch_failed", _short(exc)) from exc
        if app is None:
            raise WordUnavailable("dispatch_failed", "DispatchEx 返回 None")
        own_pids = _new_word_pids(pids_before)      # 等它出现在 tasklist 里（本次实例的物证）
        _spawned_pids[:] = sorted(own_pids)

        _quiet_word(app)

        try:
            doc = app.Documents.Open(docx_path, ConfirmConversions=False,
                                     ReadOnly=True, AddToRecentFiles=False)
        except Exception as exc:       # noqa: BLE001
            raise _convert_error("打开 docx", exc) from exc
        if doc is None:
            raise RenderError("Word 打不开 docx（返回 None）：%s" % docx_path)

        try:
            doc.SaveAs(pdf_path, FileFormat=17)          # wdFormatPDF
        except Exception as exc:       # noqa: BLE001
            raise _convert_error("导出 PDF", exc) from exc

        try:
            return int(doc.ComputeStatistics(2))         # wdStatisticPages
        except Exception:              # noqa: BLE001 - 页数另有 PyMuPDF 兜底
            return 0
    finally:
        if doc is not None:
            try:
                doc.Close(False)                             # 不保存
            except Exception:          # noqa: BLE001
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:          # noqa: BLE001
                quit_failed = True
        doc = app = None
        try:
            pythoncom.CoUninitialize()
        except Exception:              # noqa: BLE001
            pass
        # 收尾只针对本次起的 Word：Quit 失败/迟迟不退 → 强杀它；杀不到也绝不误伤用户的 Word
        mine = own_pids or (set(_word_pids()) - set(pids_before))
        if quit_failed:
            _kill_pids(mine)
        elif mine and not _wait_word_gone(mine):
            _kill_pids(mine)


def _run_with_watchdog(docx_path: str, pdf_path: str) -> int:
    """带看门狗的转换：超时杀 WINWORD.EXE 并抛 WordUnavailable("timeout")。"""
    box = {}

    def work():
        try:
            box["pages"] = _convert_blocking(docx_path, pdf_path)
        except BaseException as exc:   # noqa: BLE001 - 任何异常都带回主线程
            box["error"] = exc

    with _cross_process_lock():        # 跨进程排队：测试/演示/服务同时跑也不会互相踩
        with _WORD_LOCK:               # 进程内串行：一处卡死也不会把别人的 Word 杀掉
            pids_before = _word_pids()
            worker = threading.Thread(target=work, name="word-convert", daemon=True)
            worker.start()
            worker.join(WORD_TIMEOUT_S)
            if worker.is_alive():
                killed = _kill_word_processes(pids_before)
                worker.join(3.0)
                raise WordUnavailable(
                    "timeout",
                    "Word 超过 %.0f 秒没返回，已强杀 WINWORD.EXE（%s）"
                    % (WORD_TIMEOUT_S,
                       ("PID " + ", ".join(str(p) for p in killed)) if killed else "未抓到 PID"))
    if "error" in box:
        exc = box["error"]
        raise exc if isinstance(exc, RenderError) else RenderError(
            "Word 转换失败：%s" % _short(exc))
    return int(box.get("pages") or 0)


def docx_to_pdf(docx_path: str, pdf_path: str) -> tuple:
    """docx → PDF（Word COM 导出），返回 ``(pages, seconds)``。

    * 源 docx 只读打开（先复制到 temp 的 ASCII 文件名下），绝不改原文件；
    * 没有 Word / Word 卡死 → ``WordUnavailable``（带 reason，供上层降级）；
    * 目标 PDF 被别的程序锁住 → 转换前就抛 ``RenderError``（含"关掉打开它的程序"提示）；
    * 其它失败（文件不存在、Word 报错、跨卷/占用）→ ``RenderError``。
    """
    t0 = time.perf_counter()
    docx_path = os.path.abspath(str(docx_path))
    pdf_path = os.path.abspath(str(pdf_path))
    if not os.path.isfile(docx_path):
        raise RenderError("docx 不存在：%s" % docx_path)
    if not docx_path.lower().endswith(".docx"):
        raise RenderError("只接受 .docx：%s" % docx_path)
    if os.path.isdir(pdf_path):
        raise RenderError("pdf_path 指向目录，不是文件：%s" % pdf_path)
    _require_word()
    _preflight_target(pdf_path)

    parent = os.path.dirname(pdf_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _sweep_stale_temp()
    try:
        _sweep_orphan_word()           # 上次转换被强杀留下的 Word/temp 顺手清掉
    except Exception:                  # noqa: BLE001 - 清扫失败不能拖累本次转换
        pass

    tmp_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    _write_meta(tmp_dir)
    try:
        src = os.path.join(tmp_dir, "in.docx")   # ASCII 名字：Word 最稳的输入
        try:
            shutil.copy2(docx_path, src)
        except OSError as exc:                   # 源 docx 被 Word 锁着 / 权限不足
            raise RenderError("读不了源 docx（可能被 Word 打开占用或没有读权限）：%s（%s）"
                              % (docx_path, _short(exc))) from exc
        out = os.path.join(tmp_dir, "out.pdf")
        word_pages = _run_with_watchdog(src, out)
        if not os.path.isfile(out) or os.path.getsize(out) < MIN_PDF_BYTES:
            raise RenderError("Word 没产出可用 PDF：%s（%s 字节）"
                              % (pdf_path, os.path.getsize(out) if os.path.isfile(out) else 0))
        _move_into_place(out, pdf_path)
    finally:
        _rmtree(tmp_dir)

    pages = _pdf_page_count(pdf_path) or int(word_pages or 0)
    if pages <= 0:
        raise RenderError("PDF 页数为 0，渲染失败：%s" % pdf_path)
    return int(pages), round(time.perf_counter() - t0, 3)


# ---------------------------------------------------------------- PDF → PNG
def _png_name(prefix: str, index: int) -> str:
    return "%s-%02d.png" % (prefix, index)


def _is_a4(rect) -> bool:
    return (abs(float(rect.width) - PAGE_PT[0]) <= A4_TOLERANCE_PT
            and abs(float(rect.height) - PAGE_PT[1]) <= A4_TOLERANCE_PT)


def _resize_png(path: str, size) -> None:
    from PIL import Image

    with Image.open(path) as im:
        im.convert("RGB").resize(size, Image.LANCZOS).save(path)


def _render_page(page, path: str, dpi: int) -> None:
    """一页 → PNG。300dpi + A4 时直接渲染到 CONTRACT 的 2480×3508。"""
    rect = page.rect
    if dpi == 300 and _is_a4(rect):
        matrix = pymupdf.Matrix(PAGE_PX[0] / float(rect.width),
                                PAGE_PX[1] / float(rect.height))
        pix = page.get_pixmap(matrix=matrix, alpha=False)
    else:
        pix = page.get_pixmap(dpi=dpi, alpha=False)
    pix.save(path)
    if dpi == 300 and _is_a4(rect) and (pix.width, pix.height) != PAGE_PX:
        _resize_png(path, PAGE_PX)     # 极端取整偏差兜底（正常走不到）


def _clean_stale_png(out_dir: str, prefix: str, keep: int) -> None:
    """清掉上一轮多出来的 page-NN.png（否则页数会虚高）；别人的文件一律不动。"""
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    pattern = re.compile(r"^%s-(\d+)\.png$" % re.escape(prefix))
    for name in names:
        match = pattern.match(name)
        if not match or int(match.group(1)) <= keep:
            continue
        try:
            os.remove(os.path.join(out_dir, name))
        except OSError:
            continue


def _open_pdf(pdf_path: str, attempts: int = 5, delay: float = 0.2):
    """打开 PDF；刚写完/被杀软扫的 PDF 偶尔短暂被占用，重试几次。"""
    last = None
    for _ in range(max(1, attempts)):
        try:
            return pymupdf.open(pdf_path)
        except Exception as exc:       # noqa: BLE001
            last = exc
            time.sleep(delay)
    raise RenderError("打不开 PDF（可能正被其它程序占用或文件损坏）：%s（%s）"
                      % (pdf_path, _short(last)))


def pdf_to_png(pdf_path: str, out_dir: str, dpi: int = 300, prefix: str = "page") -> list:
    """PDF → 每页一张 PNG，按页序返回路径；命名 ``page-01.png`` 起。

    300dpi + A4 时输出严格 2480×3508（CONTRACT §3 规范像素，判卷按这套坐标取数）。
    """
    pdf_path = os.path.abspath(str(pdf_path))
    out_dir = os.path.abspath(str(out_dir))
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        raise RenderError("dpi 必须是整数：%r" % (dpi,))
    if dpi <= 0:
        raise RenderError("dpi 必须 > 0：%d" % dpi)
    if not os.path.isfile(pdf_path):
        raise RenderError("PDF 不存在：%s" % pdf_path)
    if os.path.getsize(pdf_path) < 8:
        raise RenderError("PDF 是空文件：%s" % pdf_path)

    prefix = re.sub(r'[\\/:*?"<>|\s]+', "_", str(prefix or "page")).strip("_") or "page"
    os.makedirs(out_dir, exist_ok=True)

    doc = _open_pdf(pdf_path)
    try:
        pages = int(doc.page_count)
        if pages <= 0:
            raise RenderError("PDF 没有页面：%s" % pdf_path)
        _clean_stale_png(out_dir, prefix, pages)
        out = []
        for index in range(pages):
            path = os.path.join(out_dir, _png_name(prefix, index + 1))
            _render_page(doc[index], path, dpi)
            out.append(path)
        return out
    finally:
        doc.close()
