# -*- coding: utf-8 -*-
"""t44 · 便携版启动器 launcher.py 的测试。

覆盖打包版最容易翻车的四件事（都不需要真 PyInstaller，也不需要真启动浏览器）：

1. **题库定位回退链**：``MATH_QB_ROOT`` / ``<exe目录>/qbank`` / ``E:\\OCR`` 的先后；
2. **只读资源落地**：config.json、data/app.db 首次启动复制到 exe 旁边，已存在不覆盖；
3. **端口占用自动 +1**：``_pick_port`` 跳过被占端口；``free_port`` 找不到就明确报错；
4. **``MATH_NO_BROWSER=1`` 生效**（打包机没有浏览器/远程桌面时不弹窗、也不报错）；
   + ``serve()`` 真起一个 uvicorn（假 app）验证"起来→可访问→优雅停"。
"""
from __future__ import annotations

import io
import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import launcher  # noqa: E402


def _touch_docx(folder: str, name: str = "00001.docx") -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(b"fake docx")
    return path


def _occupy(host: str = "127.0.0.1") -> tuple:
    """占一个端口，返回 (sock, port)；调用方负责 close。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


class QbRootTest(unittest.TestCase):
    def setUp(self):
        # 别的用例（--check）可能改过进程级环境；这里先清掉，保证回退链测的是本用例的目录
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("MATH_QB_ROOT", None)

    def test_explicit_candidates_win(self):
        tmp = tempfile.mkdtemp(prefix="qbank_a_")
        self.addCleanup(shutil.rmtree, tmp, True)
        qbank = os.path.join(tmp, "qbank")
        _touch_docx(qbank)
        self.assertEqual(launcher.resolve_qb_root(base=tmp), qbank)

    def test_prefix_order_qbank_before_fallback(self):
        """<base>/qbank 命中时不该继续找 E:\\OCR（哪怕 E:\\OCR 存在）。"""
        tmp = tempfile.mkdtemp(prefix="qbank_b_")
        self.addCleanup(shutil.rmtree, tmp, True)
        qbank = os.path.join(tmp, "qbank")
        _touch_docx(qbank)
        self.assertEqual(launcher.resolve_qb_root(base=tmp), qbank)

    def test_empty_qbank_dir_is_skipped(self):
        tmp = tempfile.mkdtemp(prefix="qbank_c_")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "qbank"), exist_ok=True)   # 只有空目录，没有 docx
        found = launcher.resolve_qb_root(base=tmp)
        # 允许回退到本机 E:\OCR；但绝不能把空 qbank 当成题库
        self.assertNotEqual(found, os.path.join(tmp, "qbank"))

    def test_missing_everywhere_returns_none(self):
        tmp = tempfile.mkdtemp(prefix="qbank_d_")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIsNone(launcher.resolve_qb_root(candidates=[None, "", os.path.join(tmp, "nope")]))

    def test_env_override(self):
        tmp = tempfile.mkdtemp(prefix="qbank_e_")
        self.addCleanup(shutil.rmtree, tmp, True)
        qb = os.path.join(tmp, "题库")
        _touch_docx(qb)
        with mock.patch.dict(os.environ, {"MATH_QB_ROOT": qb}):
            self.assertEqual(launcher.resolve_qb_root(base=tmp), qb)

    def test_point_paper_to_qbank_repoints_module_constant(self):
        import paper

        tmp = tempfile.mkdtemp(prefix="qbank_f_")
        self.addCleanup(shutil.rmtree, tmp, True)
        old = paper.SRC_ROOT
        self.addCleanup(setattr, paper, "SRC_ROOT", old)
        self.assertTrue(launcher.point_paper_to_qbank(tmp))
        self.assertEqual(paper.SRC_ROOT, os.path.abspath(tmp))
        self.assertFalse(launcher.point_paper_to_qbank(None))     # 找不到题库 → 不改


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="launcher_boot_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.base = os.path.join(self.tmp, "程序目录")
        self.bundle = os.path.join(self.tmp, "_internal")
        os.makedirs(os.path.join(self.bundle, "data"), exist_ok=True)
        with open(os.path.join(self.bundle, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"server": {"port": 8710, "pin": "1234"}}, fh)
        with open(os.path.join(self.bundle, "data", "app.db"), "wb") as fh:
            fh.write(b"SQLite format 3\x00fake")

    def test_copies_when_missing(self):
        dst = launcher.bootstrap_file("config.json", self.base, self.bundle)
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(os.path.dirname(dst), os.path.abspath(self.base))
        db = launcher.bootstrap_file(os.path.join("data", "app.db"), self.base, self.bundle)
        self.assertTrue(os.path.isfile(db))
        self.assertEqual(os.path.getsize(db), len(b"SQLite format 3\x00fake"))

    def test_does_not_overwrite_existing(self):
        self.base and os.makedirs(self.base, exist_ok=True)
        dst = os.path.join(self.base, "config.json")
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write('{"server": {"pin": "9999"}}')
        launcher.bootstrap_file("config.json", self.base, self.bundle)
        with open(dst, encoding="utf-8") as fh:
            self.assertIn("9999", fh.read(), "用户的 config（访问码/token）不能被升级包覆盖")

    def test_missing_source_returns_target_path(self):
        dst = launcher.bootstrap_file("nope.json", self.base, self.bundle)
        self.assertEqual(dst, os.path.join(os.path.abspath(self.base), "nope.json"))
        self.assertFalse(os.path.exists(dst))

    def test_prepare_paths_layout(self):
        info = launcher.prepare_paths(base=self.base, bundle=self.bundle)
        self.assertTrue(os.path.isfile(info["db"]))
        self.assertTrue(os.path.isfile(info["config"]))
        self.assertEqual(info["out"], os.path.join(self.base, "out"))


class PortTest(unittest.TestCase):
    def test_free_port_skips_occupied(self):
        sock, busy = _occupy()
        self.addCleanup(sock.close)
        for _ in range(3):
            self.assertNotEqual(launcher.free_port("127.0.0.1", busy, attempts=5), busy)

    def test_pick_port_from_configured(self):
        sock, busy = _occupy()
        self.addCleanup(sock.close)
        chosen = launcher._pick_port("127.0.0.1", busy, attempts=10)
        self.assertGreater(chosen, busy)
        self.assertLessEqual(chosen, busy + 9)

    def test_free_port_raises_when_exhausted(self):
        sock, busy = _occupy()
        self.addCleanup(sock.close)
        with self.assertRaises(OSError):
            launcher.free_port("127.0.0.1", busy, attempts=1)


class BrowserTest(unittest.TestCase):
    def test_no_browser_env_suppresses(self):
        calls = []
        with mock.patch.dict(os.environ, {"MATH_NO_BROWSER": "1"}):
            self.assertFalse(launcher.open_browser("http://127.0.0.1:8700/",
                                                   opener=lambda url: calls.append(url)))
        self.assertEqual(calls, [], "MATH_NO_BROWSER=1 时不许弹浏览器")

    def test_browser_opened_by_default(self):
        calls = []
        env = {k: v for k, v in os.environ.items() if k != "MATH_NO_BROWSER"}
        self.assertTrue(launcher.open_browser("http://127.0.0.1:8700/", env=env,
                                              opener=lambda url: calls.append(url)))
        self.assertEqual(calls, ["http://127.0.0.1:8700/"])

    def test_browser_failure_is_swallowed(self):
        def boom(url):
            raise RuntimeError("no browser in this session")

        env = {k: v for k, v in os.environ.items() if k != "MATH_NO_BROWSER"}
        self.assertFalse(launcher.open_browser("http://x/", env=env, opener=boom))


class ServeTest(unittest.TestCase):
    """真起一个 uvicorn（假 app）：证明 work-thread + 就绪探测 + 优雅停止可用。"""

    def test_serves_and_stops(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/healthz")
        def healthz():                     # noqa: ANN202
            return {"ok": True}

        sock, _busy = _occupy()
        port = _busy
        sock.close()
        seen = {}

        def on_ready(server):
            url = "http://127.0.0.1:%d/healthz" % port

            def hit():
                time.sleep(0.2)
                with urllib.request.urlopen(url, timeout=5) as resp:
                    seen["body"] = resp.read().decode("utf-8")
                seen["started"] = True
                server.should_exit = True

            threading.Thread(target=hit, daemon=True).start()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = launcher.serve(app, "127.0.0.1", port, no_browser=True,
                                  ready_timeout=15, on_ready=on_ready)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(seen.get("started"), "uvicorn 没在就绪后响应请求")
        self.assertIn('"ok"', seen.get("body", ""))

    def test_wait_started_times_out(self):
        class Never:
            started = False
            should_exit = False

        t0 = time.time()
        self.assertFalse(launcher.wait_started(Never(), timeout=0.3))
        self.assertLess(time.time() - t0, 2.0)


class FrozenBridgeTest(unittest.TestCase):
    """打包版特有坑：ocr.pin_stdlib_select() 会把 _internal 移出 sys.path 再 import select，
    冻结包里 select.pyd 就在 _internal → ModuleNotFoundError，进而 grade_routes 挂不上、判卷页 404。
    launcher 在导入业务模块前先把 select 钉住。"""

    def test_bridge_keeps_select_importable_after_path_removal(self):
        import importlib
        self.assertTrue(launcher._install_frozen_stdlib_bridge(force=True))
        saved = sys.path[:]
        try:
            sys.path[:] = [p for p in sys.path if "site-packages" not in p.lower()]
            sys.modules.pop("select", None)
            real = importlib.import_module("select")
            self.assertTrue(hasattr(real, "select"), "拿回来的不是真的 stdlib select")
            importlib.reload(real)          # ocr 里还会 reload selectors，不能炸
        finally:
            sys.path[:] = saved

    def test_bridge_is_noop_outside_frozen(self):
        before = len(sys.meta_path)
        self.assertFalse(launcher._install_frozen_stdlib_bridge())
        self.assertEqual(len(sys.meta_path), before)


class CheckModeTest(unittest.TestCase):
    def test_check_runs_and_reports(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = launcher.main(["--check"])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        for key in ("环境自检", "题库", "数据库", "配置", "PDF 后端", "可用端口", "结论"):
            self.assertIn(key, text)
        self.assertIn("打包运行", text)
        self.assertRegex(text, r"PDF 后端\s+(word|libreoffice|都没有)")

    def test_check_does_not_copy_database_into_package(self):
        """构建脚本在 dist 里跑 --check 自检，绝不能顺手把 120MB 数据库复制进包里。"""
        tmp = tempfile.mkdtemp(prefix="launcher_check_")
        self.addCleanup(shutil.rmtree, tmp, True)
        base = os.path.join(tmp, "程序目录")
        bundle = os.path.join(tmp, "_internal")
        os.makedirs(os.path.join(bundle, "data"), exist_ok=True)
        with open(os.path.join(bundle, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"server": {"port": 8710}}, fh)
        with open(os.path.join(bundle, "data", "app.db"), "wb") as fh:
            fh.write(b"SQLite format 3\x00tiny")
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        out = io.StringIO()
        with mock.patch.object(launcher, "base_dir", return_value=base), \
                mock.patch.object(launcher, "bundle_dir", return_value=bundle), \
                mock.patch.object(launcher, "resolve_qb_root", return_value=None):
            with contextlib.redirect_stdout(out):
                launcher.main(["--check"])
        self.assertFalse(os.path.exists(os.path.join(base, "config.json")),
                         "--check 不许把 config.json 复制到 exe 旁边")
        self.assertFalse(os.path.exists(os.path.join(base, "data", "app.db")),
                         "--check 不许把 app.db 复制到包里（会让 zip 多 120MB）")
        self.assertIn("PDF 后端", out.getvalue())

class PackagingGuardTest(unittest.TestCase):
    """打包前的红线：答题卡必须是 5×5（t46），否则用户装了会被判 legacy_card。"""

    def test_card_module_is_5x5(self):
        import card
        self.assertEqual(getattr(card, "ID_GRID", None), 5,
                         "card.py 的 ID_GRID 不是 5：旧 4×4 卡不能打包")
        self.assertEqual(getattr(card, "ID_GRID") ** 2 - getattr(card, "NO_BITS", 20), 5,
                         "5×5=25 位里应留 5 位 CRC（NO_BITS=20）")

    def test_build_script_checks_card_before_packing(self):
        path = os.path.join(launcher.HERE, "build_portable.ps1")
        with open(path, encoding="utf-8-sig") as fh:
            src = fh.read()
        self.assertIn("ID_GRID", src, "构建脚本要读 card.py 的 ID_GRID 判断版本")
        self.assertIn("legacy_card", src, "要写明为什么要拦（用户被判 legacy_card）")


class VersionTest(unittest.TestCase):
    def test_read_version_prefers_version_file(self):
        tmp = tempfile.mkdtemp(prefix="launcher_ver_")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "VERSION.txt"), "w", encoding="utf-8") as fh:
            fh.write("v1.9\n")
        self.assertEqual(launcher.read_version(base=tmp, bundle=tmp), "1.9")

    def test_read_version_falls_back_to_source_default(self):
        tmp = tempfile.mkdtemp(prefix="launcher_ver_")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(launcher.read_version(base=tmp, bundle=tmp), launcher.VERSION)

if __name__ == "__main__":
    unittest.main(verbosity=2)
