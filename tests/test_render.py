# -*- coding: utf-8 -*-
"""T6 · Word 渲染（render.py）：docx→PDF（Word COM）→PNG（PyMuPDF）。先红后绿。

本文件先于 ``app/render.py`` 写。覆盖：

1. **Word 探测**：注册表 / 常见安装目录 / PATH 找 WINWORD.EXE；找不到抛
   ``WordUnavailable("not_installed")``，DispatchEx 失败抛 ``WordUnavailable``（reason 非空），
   绝不让 com_error 原样穿透到上层；
2. **看门狗**：Word 卡死超过 ``WORD_TIMEOUT_S`` → 强杀 WINWORD.EXE → ``WordUnavailable("timeout")``
   （测试里把杀进程换成记账，别真杀本机 Word）；
3. **真转换**：3 行中文 docx（特意放在中文目录里）→ PDF：pages>0、seconds>0、A4 595×842、
   中文文字没丢；
4. **PDF → PNG**：300dpi = 2480×3508、命名 ``page-01.png`` 起、页序与页数正确、上一轮的脏 PNG 清掉；
5. **临时文件**：转换后 ``app/`` 与系统 temp 都不留 docx/pdf/~$ 残渣，源 docx 只读、字节不变；
6. **目标 PDF 被占用**：报明确的 ``RenderError``（含"占用"），且已生成的 PDF 不丢。
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import pymupdf  # noqa: E402
from docx import Document  # noqa: E402
from docx.shared import Pt  # noqa: E402
from PIL import Image  # noqa: E402

import render  # noqa: E402

PT2PX = 2480 / 595.3            # A4 300dpi：pt → px
CHINESE_DIR = "中文 目录"        # 带空格 + 中文，验路径处理


def sha1_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def make_docx(path: str, lines=None) -> str:
    """造一份 3 行中文 docx（A4 + 2cm 页边距，跟 paper.py 出的卷面同规格）。

    python-docx 默认模板是 Letter（612×792pt），不显式设 A4 的话 Word 导出的 PDF
    会是 Letter，跟 CONTRACT §3 的 2480×3508 规范像素对不上 —— 夹具必须像真卷面一样设 A4。
    """
    lines = lines or ["七年级数学 · 渲染自检卷",
                      "1．化简：x﹣（2x﹣y）＋3x﹣2y．",
                      "2．计算：﹣3＋5＝2，所以答案是 2．"]
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Pt(595.3)
    sec.page_height = Pt(841.9)
    sec.left_margin = sec.right_margin = Pt(56.7)
    sec.top_margin = sec.bottom_margin = Pt(56.7)
    for line in lines:
        doc.add_paragraph(line)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc.save(path)
    return path


def make_pdf(path: str, n_pages: int = 3, marks=None) -> str:
    """造一个 n 页 A4 PDF：第 i 页在固定位置画一条黑条，用来验 PNG 页序。"""
    marks = marks or [(250, 105 + i * 260) for i in range(n_pages)]
    doc = pymupdf.open()
    try:
        for i in range(n_pages):
            page = doc.new_page(width=595.3, height=841.9)
            cx, cy = marks[i]
            rect = pymupdf.Rect(cx - 150, cy - 25, cx + 150, cy + 25)
            page.draw_rect(rect, color=(0, 0, 0), fill=(0, 0, 0))
        doc.save(path)
    finally:
        doc.close()
    return path


def gray_at(png_path: str, x_px: float, y_px: float) -> int:
    with Image.open(png_path) as im:
        return im.convert("L").getpixel((int(x_px), int(y_px)))


def residue_temp_dirs() -> set:
    """系统 temp 下真正残留的转换目录。

    Windows 把目录标记成「删除挂起」后名字还会短暂存在（此时列不出任何内容），
    那不算残留；只要还能列出文件的才算真残留。
    """
    out = set()
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "mathapp-render-*")):
        try:
            if os.listdir(path):
                out.add(path)
        except OSError:
            continue
    return out


def owned_temp_dirs(pid: int) -> list:
    """meta.json 里写着"属主是这个 pid"的转换目录（本次转换的物证，别人起的不会被算进来）。"""
    out = []
    for path in glob.glob(os.path.join(tempfile.gettempdir(), "mathapp-render-*")):
        try:
            with open(os.path.join(path, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh) or {}
        except (OSError, ValueError):
            continue
        try:
            if int(meta.get("pid")) == int(pid):
                out.append(path)
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ 1. Word 探测
class WordProbeTest(unittest.TestCase):
    def test_probe_never_raises(self):
        self.assertIsInstance(render.word_available(), bool)
        path = render.word_path()
        self.assertIn(path, (None, path))
        if path:
            self.assertTrue(os.path.isfile(path), path)
            self.assertEqual(os.path.basename(path).lower(), "winword.exe")

    def test_find_word_exe_ignores_bad_candidates(self):
        self.assertIsNone(render._find_word_exe([]))
        self.assertIsNone(render._find_word_exe([r"C:\no\such\dir\WINWORD.EXE", "", None]))

    def test_find_word_exe_accepts_real_path(self):
        found = render._find_word_exe()
        if not found:
            self.skipTest("本机没装 Word：跳过（探测本身已由上面的用例覆盖）")
        self.assertTrue(os.path.isfile(found))
        self.assertEqual(render._find_word_exe([found]), found)


# ------------------------------------------------------------------ 2. 不可用 → 降级信号
class WordUnavailableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_unavail_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.docx = make_docx(os.path.join(self.tmp, "in.docx"))
        self.pdf = os.path.join(self.tmp, "out.pdf")

    def test_not_installed_raises_word_unavailable(self):
        with mock.patch.object(render, "_find_word_exe", return_value=None):
            with self.assertRaises(render.WordUnavailable) as ctx:
                render.docx_to_pdf(self.docx, self.pdf)
        exc = ctx.exception
        self.assertEqual(exc.reason, "not_installed")
        self.assertTrue(str(exc).strip(), "异常文本不能是空的（上层要直接显示）")
        self.assertFalse(os.path.exists(self.pdf), "没 Word 就不能产出半截 PDF")

    def test_dispatch_error_becomes_word_unavailable(self):
        with mock.patch.object(render, "_find_word_exe", return_value=sys.executable), \
                mock.patch.object(render.win32, "DispatchEx",
                                  side_effect=RuntimeError("COM 炸了")):
            with self.assertRaises(render.WordUnavailable) as ctx:
                render.docx_to_pdf(self.docx, self.pdf)
        self.assertTrue(ctx.exception.reason, "reason 不能为空（供 errors.py 降级文案用）")
        self.assertIn("Word", str(ctx.exception))
        self.assertFalse(os.path.exists(self.pdf))

    def test_missing_docx_is_render_error_not_word_signal(self):
        with self.assertRaises(render.RenderError) as ctx:
            render.docx_to_pdf(os.path.join(self.tmp, "nope.docx"), self.pdf)
        self.assertNotIsInstance(ctx.exception, render.WordUnavailable)


# ------------------------------------------------------------------ 3. 看门狗
class _FakeDocs:
    def Open(self, *args, **kwargs):          # noqa: N802 - 模仿 COM 方法名
        time.sleep(1.0)                       # 假装 Word 卡死
        raise RuntimeError("卡死之后才报错（不该被上层看到）")


class _FakeWordApp:
    Visible = True
    DisplayAlerts = 1
    AutomationSecurity = 1
    Documents = _FakeDocs()

    def __init__(self):
        self.quit_called = False

    def Quit(self):                           # noqa: N802
        self.quit_called = True


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_wd_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.docx = make_docx(os.path.join(self.tmp, "in.docx"))
        self.pdf = os.path.join(self.tmp, "out.pdf")

    def test_hung_word_is_killed_and_reported_as_timeout(self):
        killed = []
        app = _FakeWordApp()
        with mock.patch.object(render, "_find_word_exe", return_value=sys.executable), \
                mock.patch.object(render, "WORD_TIMEOUT_S", 0.3), \
                mock.patch.object(render, "_cross_process_lock",
                                  lambda timeout=None: contextlib.nullcontext()), \
                mock.patch.object(render, "_kill_word_processes",
                                  side_effect=lambda *a, **k: killed.append(True) or []), \
                mock.patch.object(render.win32, "DispatchEx", return_value=app):
            t0 = time.time()
            with self.assertRaises(render.WordUnavailable) as ctx:
                render.docx_to_pdf(self.docx, self.pdf)
            elapsed = time.time() - t0
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertTrue(killed, "看门狗超时后必须强杀 WINWORD.EXE")
        self.assertLess(elapsed, 4.0, "看门狗没在超时后立刻返回（%.1fs）" % elapsed)
        self.assertFalse(os.path.exists(self.pdf))

    def test_word_pids_lists_ints(self):
        pids = render._word_pids()
        self.assertIsInstance(pids, list)
        for pid in pids:
            self.assertIsInstance(pid, int)
            self.assertGreater(pid, 0)

    def test_process_helpers_are_safe_on_empty_input(self):
        self.assertEqual(render._live_word_pids([]), [])
        self.assertTrue(render._wait_word_gone([], timeout=0.1))
        self.assertEqual(render._kill_pids([]), [])


# ------------------------------------------------------------------ 4. 真转换
class RealConvertTest(unittest.TestCase):
    """真跑 Word：小 docx（中文）→ PDF → PNG。没有 Word 就跳过。"""

    @classmethod
    def setUpClass(cls):
        if not render.word_available():
            raise unittest.SkipTest("本机没有可用的 Word：跳过真转换")
        cls.tmp = tempfile.mkdtemp(prefix="render_real_")
        cls.work = os.path.join(cls.tmp, CHINESE_DIR)      # 中文 + 空格目录
        os.makedirs(cls.work, exist_ok=True)
        cls.docx = make_docx(os.path.join(cls.work, "自检 卷.docx"))
        cls.docx_sha1 = sha1_file(cls.docx)
        cls.out = os.path.join(cls.work, "png 输出")
        cls.app_names = set(os.listdir(APP))
        cls.tmp_before = residue_temp_dirs()
        cls.word_pids_before = set(render._word_pids())

        t0 = time.time()
        cls.pages, cls.seconds = render.docx_to_pdf(cls.docx, os.path.join(cls.work, "自检.pdf"))
        cls.convert_wall = time.time() - t0
        cls.pdf = os.path.join(cls.work, "自检.pdf")
        t1 = time.time()
        cls.pngs = render.pdf_to_png(cls.pdf, cls.out, dpi=300)
        cls.png_wall = time.time() - t1

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_pdf_created_and_pages_positive(self):
        self.assertTrue(os.path.isfile(self.pdf))
        self.assertGreater(os.path.getsize(self.pdf), 1000, "PDF 太小，像是空文件")
        self.assertGreaterEqual(self.pages, 1)
        self.assertIsInstance(self.pages, int)
        with pymupdf.open(self.pdf) as doc:
            self.assertEqual(doc.page_count, self.pages, "返回页数与 PDF 实际页数不一致")

    def test_seconds_positive_and_sane(self):
        self.assertGreater(self.seconds, 0.0)
        self.assertLess(self.seconds, 90.0)
        self.assertGreaterEqual(self.convert_wall, self.seconds * 0.5)
        self.assertLess(self.convert_wall, 120.0)

    def test_pdf_is_a4(self):
        with pymupdf.open(self.pdf) as doc:
            rect = doc[0].rect
        self.assertAlmostEqual(rect.width, 595.3, delta=1.0)
        self.assertAlmostEqual(rect.height, 841.9, delta=1.0)

    def test_chinese_text_survived(self):
        with pymupdf.open(self.pdf) as doc:
            text = "".join(page.get_text() for page in doc)
        self.assertIn("化简", text.replace(" ", ""), "PDF 里没有中文题干：%r" % text[:200])
        self.assertIn("七年级数学", text.replace(" ", ""))

    def test_png_count_and_order_matches_pdf(self):
        self.assertEqual(len(self.pngs), self.pages)
        self.assertEqual([os.path.basename(p) for p in self.pngs],
                         ["page-%02d.png" % i for i in range(1, self.pages + 1)])
        for path in self.pngs:
            self.assertTrue(os.path.isfile(path), path)
            self.assertGreater(os.path.getsize(path), 5000)

    def test_png_is_a4_300dpi(self):
        for path in self.pngs:
            with Image.open(path) as im:
                self.assertEqual(im.size, (2480, 3508), path)
                self.assertEqual(im.mode, "RGB", path)

    def test_png_page_order_visual(self):
        """第 1 页 PNG 的顶部（页边距之下）有文字墨迹：说明渲染没偏、没空白。"""
        with Image.open(self.pngs[0]) as im:
            arr = im.convert("L")
            # A4 + 2cm 页边距：正文从 y≈236px 开始，取顶部正文带扫一遍
            band = [arr.getpixel((x, y))
                    for y in range(220, 760, 6) for x in range(200, 2300, 24)]
        self.assertTrue(any(v < 200 for v in band), "第 1 页顶部正文带没有字：渲染可能不对")

    def test_source_docx_untouched(self):
        self.assertEqual(sha1_file(self.docx), self.docx_sha1, "原始 docx 被改了（必须只读打开）")

    def test_no_orphan_word_process(self):
        """转换结束后本次起的 WINWORD.EXE 必须已经退出。

        只认 ``render.last_spawned_pids()``（本次 DispatchEx 的物证）——多人同时跑 Word 时
        "全局进程数变多"是环境噪声，不能算到本模块头上（队长已踩过这个坑）。
        """
        pids = render.last_spawned_pids()
        self.assertIsInstance(pids, list, "render 要能报出本次转换起的 PID 供排查")
        deadline = time.time() + 10.0
        alive = []
        while time.time() < deadline:
            current = set(render._word_pids())
            alive = [p for p in pids if p in current]
            if not alive:
                break
            time.sleep(0.2)
        self.assertEqual(alive, [], "本次转换起的 Word 没退出：%r（跨进程锁文件：%s）"
                         % (alive, render.LOCK_PATH))

    def test_no_temp_residue(self):
        """不许留自己的转换临时目录 / .part- 半成品 / Word 属主文件。

        只查"能证明是自己的"东西（meta.json 属主 = 本进程），不断言 ``app/`` 目录集合不变 ——
        队友正常提交（如 app/tools/）不该把这条测试打红。
        """
        self.assertEqual(owned_temp_dirs(os.getpid()), [],
                         "本次转换的临时目录没清：%r" % owned_temp_dirs(os.getpid()))
        for pattern in ("*.part-*", "~$*.docx", "~$*.pdf"):
            self.assertEqual(glob.glob(os.path.join(APP, pattern)), [],
                             "app/ 下不该有转换残留：%r" % pattern)
            self.assertEqual(glob.glob(os.path.join(self.work, pattern)), [],
                             "产物目录下不该有 .part- 半成品：%r" % pattern)
        # 源目录里不许有 Word 属主文件（~$）—— 转换走 temp 副本
        leftovers = glob.glob(os.path.join(self.work, "~$*"))
        self.assertEqual(leftovers, [], "Word 的 ~$ 属主文件留在源目录了：%r" % leftovers)


# ------------------------------------------------------------------ 5. PDF → PNG
class PdfToPngTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_png_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "输出 目录")
        self.pdf = make_pdf(os.path.join(self.tmp, "三页.pdf"), n_pages=3)

    def test_names_start_at_page_01(self):
        pngs = render.pdf_to_png(self.pdf, self.out, dpi=300)
        self.assertEqual([os.path.basename(p) for p in pngs],
                         ["page-01.png", "page-02.png", "page-03.png"])

    def test_a4_300dpi_size(self):
        pngs = render.pdf_to_png(self.pdf, self.out, dpi=300)
        for path in pngs:
            with Image.open(path) as im:
                self.assertEqual(im.size, render.PAGE_PX, path)

    def test_page_order(self):
        """第 i 页的黑条只出现在第 i 张 PNG 上（页序不能反、不能串）。"""
        pngs = render.pdf_to_png(self.pdf, self.out, dpi=300)
        marks = [(250, 105 + i * 260) for i in range(3)]
        for i, path in enumerate(pngs):
            for j, (cx, cy) in enumerate(marks):
                value = gray_at(path, cx * PT2PX, cy * PT2PX)
                if i == j:
                    self.assertLess(value, 60, "第 %d 张缺自己的黑条（灰度 %d）" % (i + 1, value))
                else:
                    self.assertGreater(value, 200,
                                       "第 %d 张上出现了第 %d 页的墨迹（页序错了）" % (i + 1, j + 1))

    def test_dpi_150_not_forced_to_a4(self):
        pngs = render.pdf_to_png(self.pdf, self.out, dpi=150)
        with Image.open(pngs[0]) as im:
            self.assertLess(im.size[0], 2480)
            self.assertAlmostEqual(im.size[0] / 1240.0, 1.0, delta=0.02)

    def test_stale_png_removed(self):
        os.makedirs(self.out, exist_ok=True)
        stale = os.path.join(self.out, "page-09.png")
        with open(stale, "wb") as fh:
            fh.write(b"old")
        other = os.path.join(self.out, "keep-me.txt")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("别删我")
        pngs = render.pdf_to_png(self.pdf, self.out, dpi=300)
        self.assertFalse(os.path.exists(stale), "上一轮的 page-09.png 没清掉，页数会虚高")
        self.assertTrue(os.path.exists(other), "只清自己的 page-NN.png，别动别人的文件")
        self.assertEqual(len(pngs), 3)

    def test_missing_pdf_raises_render_error(self):
        with self.assertRaises(render.RenderError):
            render.pdf_to_png(os.path.join(self.tmp, "没有这个.pdf"), self.out)

    def test_empty_pdf_raises_render_error(self):
        empty = os.path.join(self.tmp, "empty.pdf")
        open(empty, "wb").close()
        with self.assertRaises(render.RenderError):
            render.pdf_to_png(empty, self.out)


class CrossProcessLockTest(unittest.TestCase):
    """跨进程锁：多人/多进程同时出卷时按锁排队，别把别人的 Word 算到自己头上。

    用例用自己的临时目录做锁（``lock_dir=`` / ``MATHAPP_LOCK_DIR``），跟别人正在导 PDF
    完全隔离 —— 不会因为队友占着默认锁而假红。
    """

    WAIT = 180.0        # 产品默认 20s；测试里肯等（拿的是自己目录的锁，基本秒到）

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_lockdir_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_lock_is_reentrant_and_keeps_lock_file(self):
        with render._cross_process_lock(timeout=self.WAIT, required=True,
                                        lock_dir=self.tmp):
            with render._cross_process_lock(timeout=self.WAIT, required=True,
                                            lock_dir=self.tmp):   # 嵌套不该自锁
                self.assertTrue(os.path.exists(render.lock_path(self.tmp)))
        self.assertTrue(os.path.exists(render.lock_path(self.tmp)),
                        "锁文件应留着复用（同目录下次直接锁它）")

    def test_lock_blocks_another_process(self):
        script = os.path.join(self.tmp, "try_lock.py")
        code = "\n".join([
            "import sys, time",
            "sys.path.insert(0, " + repr(APP) + ")",
            "import render",
            "t0 = time.time()",
            "try:",
            "    with render._cross_process_lock(timeout=2.0, required=True):",
            "        print('GOT %.1fs' % (time.time() - t0))",
            "except render.WordUnavailable as exc:",
            "    print('BUSY %s' % exc.reason)",
        ]) + "\n"
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(code)
        env = dict(os.environ, MATHAPP_LOCK_DIR=self.tmp)   # 子进程用同一个隔离锁目录
        with render._cross_process_lock(timeout=self.WAIT, required=True,
                                        lock_dir=self.tmp):
            blocked = subprocess.run([sys.executable, script], capture_output=True,
                                     text=True, timeout=120, env=env)
        self.assertIn("BUSY", blocked.stdout,
                      "锁被本进程持有时，别的进程必须拿不到（stdout=%r stderr=%r）"
                      % (blocked.stdout, blocked.stderr[-500:]))
        free = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              timeout=120, env=env)
        self.assertIn("GOT", free.stdout,
                      "锁释放后别的进程应该能拿到（stdout=%r stderr=%r）"
                      % (free.stdout, free.stderr[-500:]))

    def test_env_var_selects_lock_dir(self):
        self.assertEqual(render.lock_path(self.tmp), os.path.join(self.tmp,
                                                                  "mathapp-render.lock"))
        with mock.patch.dict(os.environ, {"MATHAPP_LOCK_DIR": self.tmp}):
            self.assertEqual(render.lock_path(), os.path.join(self.tmp,
                                                              "mathapp-render.lock"))


# ------------------------------------------------------------------ 6. 目标 PDF 被占用
class LockedTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_lock_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "new.pdf")
        with open(self.src, "wb") as fh:
            fh.write(b"%PDF-1.4 new")
        self.dst = os.path.join(self.tmp, "target.pdf")
        with open(self.dst, "wb") as fh:
            fh.write(b"%PDF-1.4 old")

    def test_locked_target_raises_clear_error(self):
        holder = open(self.dst, "rb")
        try:
            with self.assertRaises(render.RenderError) as ctx:
                render._move_into_place(self.src, self.dst, retries=2, delay=0.02)
        finally:
            holder.close()
        self.assertIn("占用", str(ctx.exception))
        self.assertTrue(os.path.exists(self.src), "落位失败后新 PDF 不能丢，重试还要用")

    def test_succeeds_after_lock_released(self):
        render._move_into_place(self.src, self.dst, retries=2, delay=0.02)
        with open(self.dst, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 new")
        self.assertFalse(os.path.exists(self.src))

    def test_readonly_target_is_replaced(self):
        """目标 PDF 带只读属性（下载/邮件里来的 PDF 常见）→ 也要能覆盖。"""
        os.chmod(self.dst, 0o444)
        render._move_into_place(self.src, self.dst, retries=8, delay=0.02)
        with open(self.dst, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 new")
        self.assertFalse(os.path.exists(self.src))

    def test_docx_to_pdf_fails_fast_when_target_is_locked(self):
        """目标 PDF 被占用：转换前就报清楚（别先跑几秒 Word 再报"被占用"）。"""
        docx = make_docx(os.path.join(self.tmp, "in.docx"))
        holder = open(self.dst, "rb")
        try:
            t0 = time.time()
            with mock.patch.object(render, "_find_word_exe", return_value=sys.executable):
                with self.assertRaises(render.RenderError) as ctx:
                    render.docx_to_pdf(docx, self.dst)
            elapsed = time.time() - t0
        finally:
            holder.close()
        self.assertIn("占用", str(ctx.exception))
        self.assertIn("关掉", str(ctx.exception), "报错要告诉用户怎么办：%s" % ctx.exception)
        self.assertLess(elapsed, 2.0, "占用应在转换前就发现（用了 %.1fs）" % elapsed)


class CrossVolumeTest(unittest.TestCase):
    """P0 回归：TEMP 在 C:、产物在 E:（用户真实布局）时 os.replace 必报 WinError 17。

    必须先拷到目标卷再原子改名，不能把跨卷失败谎报成"目标被占用"。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_xvol_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "new.pdf")
        with open(self.src, "wb") as fh:
            fh.write(b"%PDF-1.4 cross-volume")

    def test_mocked_cross_volume_moves_file(self):
        dst = os.path.join(self.tmp, "sub", "target.pdf")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with mock.patch.object(render, "_same_volume", return_value=False):
            render._move_into_place(self.src, dst, retries=2, delay=0.02)
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 cross-volume")
        self.assertFalse(os.path.exists(self.src), "跨卷落位后源文件应删掉")
        self.assertEqual(glob.glob(os.path.join(os.path.dirname(dst), "*.part-*")), [],
                         "跨卷落位用的 .part- 临时文件没清掉")

    def test_real_cross_volume_move(self):
        """真跨盘：源在系统 temp（C:），目标在 app/out（E:）。同卷机器上跳过。"""
        target_dir = os.path.join(APP, "out", "_t21_crossvol")
        if (os.path.splitdrive(tempfile.gettempdir())[0].lower()
                == os.path.splitdrive(target_dir)[0].lower()):
            self.skipTest("本机 TEMP 与 app 在同一卷，跨卷用例无意义")
        os.makedirs(target_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, target_dir, True)
        dst = os.path.join(target_dir, "cross_vol.pdf")
        render._move_into_place(self.src, dst, retries=2, delay=0.02)
        with open(dst, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 cross-volume")
        self.assertFalse(os.path.exists(self.src))
        self.assertEqual(glob.glob(os.path.join(target_dir, "*.part-*")), [])

    def test_winerror_17_is_reported_as_cross_volume_not_busy(self):
        dst = os.path.join(self.tmp, "target.pdf")
        exdev = OSError(17, "系统无法将文件移到不同的磁盘驱动器。")
        exdev.winerror = 17
        with mock.patch.object(render, "_same_volume", return_value=True), \
                mock.patch.object(render.os, "replace", side_effect=exdev):
            with self.assertRaises(render.RenderError) as ctx:
                render._move_into_place(self.src, dst, retries=1, delay=0.0)
        message = str(ctx.exception)
        self.assertIn("NOT_SAME_DEVICE", message, message)
        self.assertNotIn("被占用", message, "WinError 17 是跨卷，不是被占用：%s" % message)
        self.assertIn("源盘", message, "错误信息要能看出源盘/目标盘：%s" % message)
        self.assertTrue(os.path.exists(self.src))


class OrphanSweepTest(unittest.TestCase):
    """父进程被强杀（Ctrl-C / 测试超时）后留下的 Word + temp 目录，下次转换要能清掉。

    清扫必须**只**动"属主进程已死"的目录；用户/别人正在跑的转换一律不碰。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="render_orphan_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _fake_orphan_dir(self, pid, age_s=700.0, name="mathapp-render-t21orphan"):
        path = os.path.join(tempfile.gettempdir(), name)
        os.makedirs(path, exist_ok=True)
        self.addCleanup(shutil.rmtree, path, True)
        with open(os.path.join(path, "in.docx"), "wb") as fh:
            fh.write(b"fake docx")
        with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "ts": time.time() - age_s}, fh)
        old = time.time() - age_s
        os.utime(path, (old, old))
        return path

    def test_pid_alive(self):
        self.assertTrue(render._pid_alive(os.getpid()))
        self.assertFalse(render._pid_alive(99999999), "不存在的 PID 必须判死")

    def test_process_creation_time(self):
        created = render._process_creation_time(os.getpid())
        self.assertIsNotNone(created)
        self.assertAlmostEqual(created, time.time(), delta=3600)

    def test_visible_window_pids_is_a_set_of_ints(self):
        pids = render._visible_window_pids()
        self.assertIsInstance(pids, set)
        for pid in pids:
            self.assertIsInstance(pid, int)

    def test_orphan_dir_with_dead_owner_is_removed(self):
        path = self._fake_orphan_dir(99999999)
        with mock.patch.object(render, "_word_pids", return_value=[]):
            report = render._sweep_orphan_word(max_age_s=600)
        self.assertFalse(os.path.exists(path), "属主已死的孤儿目录没清掉")
        self.assertGreaterEqual(report["dirs"], 1)

    def test_live_owner_dir_is_kept(self):
        path = self._fake_orphan_dir(os.getpid())
        with mock.patch.object(render, "_word_pids", return_value=[]):
            render._sweep_orphan_word(max_age_s=600)
        self.assertTrue(os.path.isdir(path), "属主还活着的目录不能动（可能正在转换）")

    def test_fresh_dir_is_ignored(self):
        path = self._fake_orphan_dir(99999999, age_s=5.0)
        with mock.patch.object(render, "_word_pids", return_value=[]):
            render._sweep_orphan_word(max_age_s=600)
        self.assertTrue(os.path.isdir(path), "刚建的目录不能当孤儿清掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
