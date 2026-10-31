# -*- coding: utf-8 -*-
"""t15 · 打包与文档测试：requirements / start.bat / README 三者必须与代码对得上。

守的是「第一次双击能不能成功」这件事：
  1. requirements.txt 里每个包都能在本机 import（find_spec，不真导入重包），
     写的版本区间必须包含本机实测版本（区间写错 = 换台机器装不上）；
  2. start.bat 必须按实测踩过的坑来：先 cd 到 app 目录、用 `python -m uvicorn server:app`
     （PATH 里不一定有 uvicorn 命令）、端口 8710、起来之后才开浏览器、出错要 pause 不闪退；
  3. server:app 必须是 FastAPI 实例（start.bat 靠它启动，写成 None 服务就起不来）；
  4. README 必须有「打印要求」「拍照要求」两节，且写明 100% 比例 / 四角入画这些硬要求。
"""
from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import os
import re
import sys
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import server  # noqa: E402
from fastapi import FastAPI  # noqa: E402

REQUIREMENTS = os.path.join(APP_DIR, "requirements.txt")
START_BAT = os.path.join(APP_DIR, "start.bat")
README = os.path.join(APP_DIR, "README.md")

#: pip 包名 → 真正要 import 的模块名（这些名字是实测的，别猜）
IMPORT_NAMES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "python-multipart": "multipart",
    "jinja2": "jinja2",
    "opencv-python": "cv2",
    "numpy": "numpy",
    "pillow": "PIL",
    "python-docx": "docx",
    "lxml": "lxml",
    "pymupdf": "pymupdf",
    "reportlab": "reportlab",
    "qrcode": "qrcode",
    "pywin32": "win32com",
    "openpyxl": "openpyxl",
    "requests": "requests",
    "pillow-heif": "pillow_heif",   # 可选：HEIC 照片（iPhone）支持
}

#: 契约 §0 已冻结的底座 + 本轮新增，缺一个就不是完整安装
REQUIRED_PACKAGES = set(IMPORT_NAMES)


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_requirements(text: str) -> list[tuple[str, str]]:
    """(包名, 版本约束) 列表；跳过注释与空行，行内注释也剥掉。"""
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$", line)
        if not m:
            raise AssertionError("requirements.txt 有一行读不懂：%r" % raw)
        out.append((m.group(1).lower(), m.group(2).strip()))
    return out


def version_tuple(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text)
    return tuple(int(n) for n in nums) if nums else (0,)


def satisfies(version: str, spec: str) -> bool:
    """只认 >= / > / <= / < / == 这几种，够用且不引第三方依赖。"""
    got = version_tuple(version)
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        m = re.match(r"^(>=|<=|==|>|<)\s*([0-9][0-9A-Za-z.\-_]*)$", part)
        if not m:
            raise AssertionError("版本约束写不通：%r" % part)
        op, want = m.group(1), version_tuple(m.group(2))
        if op == ">=" and not got >= want:
            return False
        if op == ">" and not got > want:
            return False
        if op == "<=" and not got <= want:
            return False
        if op == "<" and not got < want:
            return False
        if op == "==" and not got == want:
            return False
    return True


class TestRequirements(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(REQUIREMENTS):
            raise AssertionError("缺 app/requirements.txt")
        cls.text = read_text(REQUIREMENTS)
        cls.packages = parse_requirements(cls.text)
        cls.names = [name for name, _ in cls.packages]

    def test_lists_every_needed_package(self):
        missing = REQUIRED_PACKAGES - set(self.names)
        self.assertEqual(set(), missing, "requirements.txt 漏了：%s" % sorted(missing))

    def test_no_duplicate_names(self):
        self.assertEqual(len(self.names), len(set(self.names)), "有重复行")

    def test_every_package_is_importable_here(self):
        for name, spec in self.packages:
            module = IMPORT_NAMES.get(name)
            self.assertIsNotNone(module, "测试里没登记 %s 的导入名" % name)
            with self.subTest(package=name, module=module):
                self.assertIsNotNone(importlib.util.find_spec(module),
                                     "%s 装不上/没装：import %s 失败" % (name, module))

    def test_version_ranges_contain_installed_versions(self):
        for name, spec in self.packages:
            with self.subTest(package=name):
                self.assertTrue(spec, "%s 没写版本区间" % name)
                installed = metadata.version(name)
                self.assertTrue(
                    satisfies(installed, spec),
                    "%s 区间 %s 不包含本机实测版本 %s" % (name, spec, installed))

    def test_no_environment_markers_or_editable_installs(self):
        self.assertNotIn("-e ", self.text)
        self.assertNotIn("--index-url", self.text)


class TestStartBat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(START_BAT):
            raise AssertionError("缺 app/start.bat")
        with open(START_BAT, "rb") as fh:
            cls.raw = fh.read()
        cls.text = cls.raw.decode("utf-8")

    def test_runs_from_app_dir(self):
        # 不先切目录，`python -m uvicorn server:app` 会因为找不到 server 模块直接失败
        self.assertIn('cd /d "%~dp0"', self.text)

    def test_starts_uvicorn_module_not_command(self):
        self.assertIn("python -m uvicorn server:app", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*uvicorn\s+server:app")

    def test_defaults_to_loopback_8710(self):
        # 默认必须只监听本机（安全）；LAN 模式由 start.bat lan / start_lan.bat 显式打开
        self.assertIn('set "HOST=127.0.0.1"', self.text)
        self.assertIn("--host %HOST% --port 8710", self.text)
        self.assertIn("http://127.0.0.1:8710", self.text)
        self.assertIn('if /i "%MATHAPP_LAN%"=="1" set "HOST=0.0.0.0"', self.text)

    def test_opens_browser_after_start(self):
        self.assertIn("webbrowser.open", self.text)

    def test_checks_python_and_deps_and_word(self):
        self.assertIn("python", self.text.lower())
        self.assertIn("requirements.txt", self.text)       # 缺依赖时给 pip 命令
        self.assertIn("WINWORD.EXE", self.text)            # Word 探测
        self.assertIn("import fastapi", self.text)         # 依赖自检

    def test_chinese_output_and_no_flash_close(self):
        self.assertIn("chcp 65001", self.text)             # 中文不乱码
        self.assertIn("pause", self.text)                  # 双击出错不闪退

    def test_has_headless_escape_hatch(self):
        # 自动化验收时不该往人桌面上弹浏览器
        self.assertIn("MATH_NO_BROWSER", self.text)

    def test_is_crlf_and_no_bom(self):
        # cmd 对 LF-only 的 bat 容忍度差，BOM 还会让第一行认不出来
        self.assertFalse(self.raw.startswith(b"\xef\xbb\xbf"), "start.bat 不能有 BOM")
        self.assertIn(b"\r\n", self.raw, "start.bat 要用 CRLF 换行")


class TestUvicornTarget(unittest.TestCase):
    def test_server_app_is_fastapi_instance(self):
        # start.bat 用 `server:app` 启动：这里不是 FastAPI 实例的话服务起不来
        self.assertIsInstance(server.app, FastAPI)

    def test_app_has_expected_routes(self):
        paths = {getattr(r, "path", None) for r in server.app.routes}
        for path in ("/", "/health", "/healthz"):
            self.assertIn(path, paths)


class TestReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(README):
            raise AssertionError("缺 app/README.md")
        cls.text = read_text(README)
        cls.flat = re.sub(r"\s+", " ", cls.text)

    def test_has_required_sections(self):
        for heading in ("安装", "启动", "组卷", "打印要求", "拍照要求",
                        "判卷", "训练", "常见问题", "隐私"):
            self.assertIn(heading, self.text, "README 缺「%s」相关章节" % heading)

    def test_print_requires_100_percent(self):
        self.assertIn("100%", self.text)
        self.assertIn("A4", self.text)
        # 「适应页面/缩放」是最常见的打印事故，必须明确写出不要勾
        self.assertRegex(self.flat, r"(不要|别|勿).{0,12}(适应页面|缩放|适应纸张)")

    def test_photo_requires_four_corners(self):
        self.assertIn("四角", self.text)
        self.assertRegex(self.flat, r"反光|闪光灯")
        self.assertRegex(self.flat, r"1600|分辨率")

    def test_privacy_and_readonly_source(self):
        self.assertIn("隐私", self.text)
        self.assertRegex(self.flat, r"只读|不修改|不改动")
        self.assertRegex(self.flat, r"本机|本地|不出这台机器|不外传")

    def test_mentions_start_bat_and_port(self):
        self.assertIn("start.bat", self.text)
        self.assertIn("8710", self.text)


if __name__ == "__main__":
    unittest.main()
