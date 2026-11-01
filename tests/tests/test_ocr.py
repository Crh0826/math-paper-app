# -*- coding: utf-8 -*-
"""T9 · 识别适配层（ocr.py）测试。

全部用本地 mock HTTP 服务，不碰真实网络、不烧 30s 的云端 job：
  1. manual 后端永不崩、永不给假答案；
  2. vlm（OpenAI 兼容多模态）：成功解析 / 带解释文字后处理 / 503 重试 / 超时 / 乱码 → 一律降级 manual；
  3. paddle_vl（job 轮询 + jsonl + block_bbox 归格）：成功按格回填 / 失败与超时整批降级；
  4. 拼长图：宽度 900、格间 ≥8px 纯白、左上角印 [idx-blank] 编号、y 区间可归格；
  5. 任何异常都不许往外抛（判卷主流程不能被 OCR 拖死）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

# 先把标准库钉进 sys.modules：app/ 下有 select.py（组卷选题模块），
# 3.14 里 stdlib select 是启动即预装的内置模块；若别的测试用 importlib 以 "select"
# 之名加载 app/select.py，就会把 sys.modules['select'] 换掉，socketserver 立刻崩
# （AttributeError: module 'select' has no attribute 'select'）。这里存一份真身。
import http.server  # noqa: F401,E402
import select as _REAL_SELECT  # noqa: F401,E402
import socketserver  # noqa: F401,E402
import urllib.request  # noqa: F401,E402

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import ocr as ocrmod  # noqa: E402
from ocr import Recognizer, clean_answer, load_config, stitch_cells  # noqa: E402


def _pin_stdlib_select():
    """测试前置：确保 sys.modules['select'] 是真的标准库。

    背景（实测）：`select` 在 Windows 上是 DLLS 里的扩展模块，会被 `app/select.py`
    （组卷选题）按 sys.path 顺序遮蔽；谁用 importlib 以 "select" 之名加载组卷模块，
    `selectors` 之后 `import select` 就拿到组卷模块，socketserver 立刻
    AttributeError: module 'select' has no attribute 'select'。
    这里临时把 app/ 移出 sys.path 再 import，拿回真身，并让 selectors 重新绑定。
    """
    for cand in (sys.modules.get("select"), _REAL_SELECT):
        if cand is not None and hasattr(cand, "select"):
            if sys.modules.get("select") is not cand:
                sys.modules["select"] = cand
            return cand
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    saved = sys.path[:]
    try:
        sys.path[:] = [p for p in sys.path
                       if os.path.abspath(p or os.getcwd()) != here]
        sys.modules.pop("select", None)
        import select as _real
        if not hasattr(_real, "select"):
            raise RuntimeError("标准库 select 恢复失败")
        sys.modules["select"] = _real
        import importlib
        import selectors
        importlib.reload(selectors)      # 让 selectors 里的全局名重新绑定到真 select
        return _real
    finally:
        sys.path[:] = saved

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ mock HTTP
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _reply(self, code, body=b"", ctype="application/json"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # 客户端先超时断开：测试里正常，别刷屏

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self):
        body = self._read_body()
        self.server.calls.append(("POST", self.path, body, dict(self.headers)))
        route = self.server.routes.get(("POST", self.path))
        if route is None:
            return self._reply(404)
        return route(self, body)

    def do_GET(self):
        self.server.calls.append(("GET", self.path, b"", dict(self.headers)))
        route = self.server.routes.get(("GET", self.path))
        if route is None:
            return self._reply(404)
        return route(self, b"")


class MockServer:
    """本地 mock 服务：routes={(method, path): fn(handler, body)}。"""

    def __init__(self):
        _pin_stdlib_select()
        self.calls = []
        self.routes = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.calls = self.calls
        self.httpd.routes = self.routes
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        _pin_stdlib_select()
        self.thread.start()
        return self

    def __exit__(self, *exc):
        _pin_stdlib_select()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        return False

    @property
    def base(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def url(self, path):
        return self.base + path

    def add_json(self, method, path, obj, code=200):
        body = json.dumps(obj).encode("utf-8")

        def handler(h, _body, code=code, body=body):
            h._reply(code, body)
        self.routes[(method, path)] = handler
        return self

    def add_raw(self, method, path, body, code=200, ctype="application/json"):
        def handler(h, _b, code=code, body=body, ctype=ctype):
            h._reply(code, body, ctype)
        self.routes[(method, path)] = handler
        return self

    def count(self, method, path=None):
        return len([c for c in self.calls
                    if c[0] == method and (path is None or c[1] == path)])


def make_crop(path, w=600, h=150, ink=True):
    img = Image.new("L", (w, h), 255)
    if ink:
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.line([(20, h - 30), (w - 20, 40)], fill=0, width=4)
    img.save(path)
    return path


def _jsonl(blocks):
    payload = json.dumps({"result": {"layoutParsingResults": [{
        "markdown": {"text": ""},
        "prunedResult": {"parsing_res_list": blocks},
    }]}}, ensure_ascii=False)
    return (payload + "\n").encode("utf-8")


# ------------------------------------------------------------------ 纯函数
class TestCleanAnswer(unittest.TestCase):
    def test_latex_wrappers(self):
        cases = {
            "$$x=4$$": "x=4",
            "\\left[13\\right]": "13",
            "$$\\left[13\\right] \\left[\\begin{array}{cc} 1/2 \\end{array}\\right]$$": "1/2",
            "\\begin{array}{cc} 1/2 \\end{array}": "1/2",
            "x^2_1": "x^2_1",
        }
        for raw, want in cases.items():
            self.assertEqual(want, clean_answer(raw), "清理错误: %r" % raw)

    def test_labels_and_prefixes(self):
        cases = {
            "[11-1]\nx=4": "x=4",
            "[11-1] x=4": "x=4",
            "答案是：x=4。": "x=4",
            "答案：3": "3",
            "答:x=4": "x=4",
            "最终答案：1/2": "1/2",
            '“x=4”': "x=4",
            '"13"': "13",
            "  x=4  ": "x=4",
        }
        for raw, want in cases.items():
            self.assertEqual(want, clean_answer(raw), "清理错误: %r" % raw)

    def test_garbled_label_is_not_an_answer(self):
        """实测：空格子会被 OCR 读成编号（连 '11' 都认成 'fi11'），绝不能当答案。"""
        self.assertEqual("", clean_answer("[fi11-1]"))
        self.assertEqual("", clean_answer("[11-1]"))
        self.assertEqual("x=4", clean_answer("[fi11-1]\nx=4"))
        self.assertEqual("x=4", clean_answer("[11 - 1] x=4"))

    def test_interval_not_mistaken_for_label(self):
        """真答案里的区间 [1,2] 不能被当编号删掉。"""
        self.assertEqual("[1,2]", clean_answer("[1,2]"))

    def test_empty_and_none(self):
        self.assertEqual("", clean_answer(""))
        self.assertEqual("", clean_answer(None))
        self.assertEqual("", clean_answer("   \n "))

    def test_keeps_juxtaposition(self):
        self.assertEqual("x=4，y=9", clean_answer("$$x=4，y=9$$"))


# ------------------------------------------------------------------ manual
class TestManualBackend(unittest.TestCase):
    def test_manual_read(self):
        r = Recognizer(backend="manual")
        out = r.read("whatever.png", "fill")
        self.assertEqual("", out["text"])
        self.assertEqual(0.0, out["confidence"])
        self.assertEqual("manual", out["backend"])

    def test_manual_read_batch(self):
        r = Recognizer(backend="manual")
        out = r.read_batch([("1-1", "a.png"), ("1-2", "b.png")])
        self.assertEqual({"1-1", "1-2"}, set(out))
        for v in out.values():
            self.assertEqual("", v["text"])
            self.assertEqual("manual", v["backend"])

    def test_unknown_backend_falls_back(self):
        r = Recognizer(backend="no_such_backend")
        out = r.read("x.png", "fill")
        self.assertEqual("", out["text"])
        self.assertIn("manual", out["backend"])

    def test_missing_file_never_raises(self):
        for backend in ("manual", "vlm", "paddle_vl", "local"):
            r = Recognizer(backend=backend, base_url="http://127.0.0.1:9", key="k",
                           job_url="http://127.0.0.1:9/jobs", timeout_s=0.3, poll_s=0.05)
            out = r.read(os.path.join(tempfile.gettempdir(), "no_such_ocr_file.png"), "fill")
            self.assertIn("text", out)
            self.assertIsInstance(out["text"], str)


# ------------------------------------------------------------------ vlm
class TestVlmBackend(unittest.TestCase):
    def _recognizer(self, srv, **kw):
        opts = dict(backend="vlm", base_url=srv.base, key="test-key", model="m",
                    vlm_timeout_s=3, vlm_retries=1)
        opts.update(kw)
        return Recognizer(**opts)

    def test_success_and_cleanup(self):
        with MockServer() as srv:
            srv.add_json("POST", "/v1/chat/completions", {
                "choices": [{"message": {"content": "答案是：x=4。"}}]})
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                p = make_crop(os.path.join(tmp, "c.png"))
                out = self._recognizer(srv).read(p, "fill")
                self.assertEqual("x=4", out["text"], "解释性文字必须被剥掉")
                self.assertEqual("vlm", out["backend"])
                self.assertGreater(out["confidence"], 0.0)
                self.assertLessEqual(out["confidence"], 1.0)
                body = srv.calls[0][2].decode("utf-8")
                self.assertIn("image_url", body)
                self.assertIn("base64", body)
                self.assertIn("只输出最终答案", body, "提示词必须约束只输出答案")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_retry_once_then_success(self):
        with MockServer() as srv:
            state = {"n": 0}

            def flaky(h, body):
                state["n"] += 1
                if state["n"] == 1:
                    h._reply(503, b'{"error":"busy"}')
                else:
                    h._reply(200, json.dumps({"choices": [{"message": {"content": "13"}}]}).encode())
            srv.routes[("POST", "/v1/chat/completions")] = flaky
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                p = make_crop(os.path.join(tmp, "c.png"))
                out = self._recognizer(srv).read(p, "fill")
                self.assertEqual("13", out["text"], "503 后重试一次应成功")
                self.assertEqual(2, srv.count("POST", "/v1/chat/completions"))
                self.assertEqual("vlm", out["backend"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_503_always_degrades(self):
        with MockServer() as srv:
            srv.add_raw("POST", "/v1/chat/completions", b'{"error":"busy"}', code=503)
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                p = make_crop(os.path.join(tmp, "c.png"))
                out = self._recognizer(srv).read(p, "fill")
                self.assertEqual("", out["text"], "503 必须降级为人工，不许编答案")
                self.assertEqual("vlm->manual", out["backend"])
                self.assertEqual(0.0, out["confidence"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_timeout_degrades_fast(self):
        with MockServer() as srv:
            def slow(h, body):
                time.sleep(2.0)
                h._reply(200, json.dumps({"choices": [{"message": {"content": "x"}}]}).encode())
            srv.routes[("POST", "/v1/chat/completions")] = slow
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                p = make_crop(os.path.join(tmp, "c.png"))
                t0 = time.time()
                out = self._recognizer(srv, vlm_timeout_s=0.4).read(p, "fill")
                dt = time.time() - t0
                self.assertEqual("", out["text"])
                self.assertEqual("vlm->manual", out["backend"])
                self.assertLess(dt, 2.0, "超时必须在 timeout 后立刻降级，不能等满 2s")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_garbage_response_degrades(self):
        with MockServer() as srv:
            srv.add_raw("POST", "/v1/chat/completions", b"<html>oops</html>", ctype="text/html")
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                p = make_crop(os.path.join(tmp, "c.png"))
                out = self._recognizer(srv).read(p, "fill")
                self.assertEqual("", out["text"])
                self.assertEqual("vlm->manual", out["backend"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ paddle_vl
class TestPaddleVlBatch(unittest.TestCase):
    def _crops(self, tmp):
        a = make_crop(os.path.join(tmp, "a.png"), 600, 150)
        b = make_crop(os.path.join(tmp, "b.png"), 600, 150)
        return [("1-1", a), ("2-1", b)]

    def _server_with_blocks(self, srv, items, contents, extra_label_blocks=True):
        png, ranges = stitch_cells(items, os.path.join(tempfile.mkdtemp(prefix="stitch_"), "s.png"))
        blocks = []
        for rng, content in zip(ranges, contents):
            if extra_label_blocks:
                blocks.append({"block_bbox": [rng["x"], rng["label_y0"],
                                              rng["x"] + 80, rng["label_y0"] + 30],
                               "block_content": "[%s]" % rng["key"]})
            blocks.append({"block_bbox": [rng["x"], rng["y0"],
                                          rng["x"] + rng["w"], rng["y1"]],
                           "block_content": content})
        jsonl = _jsonl(blocks)
        srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "J1"}})
        state = {"n": 0}

        def poll(h, body):
            state["n"] += 1
            if state["n"] < 2:
                h._reply(200, json.dumps({"data": {"state": "running"}}).encode())
            else:
                h._reply(200, json.dumps({"data": {
                    "state": "done",
                    "resultUrl": {"jsonUrl": srv.url("/res.jsonl")}}}).encode())
        srv.routes[("GET", "/api/v2/ocr/jobs/J1")] = poll
        srv.add_raw("GET", "/res.jsonl", jsonl)
        return png, ranges

    def test_batch_success_by_bbox(self):
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = self._crops(tmp)
                self._server_with_blocks(srv, items, ["x=4", "$$\\left[13\\right]$$"])
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="test-token", model="PaddleOCR-VL-1.6",
                               timeout_s=5, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual("x=4", out["1-1"]["text"])
                self.assertEqual("13", out["2-1"]["text"], "LaTeX 包装必须还原")
                for v in out.values():
                    self.assertEqual("paddle_vl", v["backend"])
                    self.assertGreater(v["confidence"], 0.0)
                # job 只提交一次；授权头照抄小写 bearer
                self.assertEqual(1, srv.count("POST", "/api/v2/ocr/jobs"))
                self.assertIn(b"PaddleOCR-VL-1.6", srv.calls[0][2], "model 字段必须随 multipart 提交")
                auth = srv.calls[0][3].get("Authorization") or ""
                self.assertTrue(auth.startswith("bearer "), "Authorization 必须是小写 bearer <token>，实际 %r" % auth)
                self.assertIn("test-token", auth)
                # 轮询也要带同一套 token
                poll_headers = [c[3] for c in srv.calls if c[0] == "GET"
                                and c[1].startswith("/api/v2/ocr/jobs/")]
                self.assertTrue(poll_headers)
                self.assertIn("test-token", poll_headers[0].get("Authorization") or "")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_job_post_503_degrades(self):
        with MockServer() as srv:
            srv.add_raw("POST", "/api/v2/ocr/jobs", b'{"error":"busy"}', code=503)
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = self._crops(tmp)
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=3, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual({"1-1", "2-1"}, set(out))
                for v in out.values():
                    self.assertEqual("", v["text"])
                    self.assertEqual("paddle_vl->manual", v["backend"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_job_failed_state_degrades(self):
        with MockServer() as srv:
            srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "J2"}})
            srv.add_json("GET", "/api/v2/ocr/jobs/J2", {"data": {"state": "failed"}})
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = self._crops(tmp)
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=3, poll_s=0.05)
                out = r.read_batch(items)
                self.assertTrue(all(v["backend"] == "paddle_vl->manual" for v in out.values()))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_poll_timeout_degrades(self):
        with MockServer() as srv:
            srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "J3"}})
            srv.add_json("GET", "/api/v2/ocr/jobs/J3", {"data": {"state": "running"}})
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = self._crops(tmp)
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=0.6, poll_s=0.05)
                t0 = time.time()
                out = r.read_batch(items)
                dt = time.time() - t0
                self.assertTrue(all(v["backend"] == "paddle_vl->manual" for v in out.values()),
                                "轮询超时必须整批降级")
                self.assertLess(dt, 3.0)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_result_url_500_degrades(self):
        with MockServer() as srv:
            srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "J4"}})
            srv.add_json("GET", "/api/v2/ocr/jobs/J4", {"data": {
                "state": "done", "resultUrl": {"jsonUrl": srv.url("/res.jsonl")}}})
            srv.add_raw("GET", "/res.jsonl", b"boom", code=500)
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = self._crops(tmp)
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=3, poll_s=0.05)
                out = r.read_batch(items)
                self.assertTrue(all(v["backend"] == "paddle_vl->manual" for v in out.values()))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_label_block_in_strip_is_dropped(self):
        """编号块（哪怕被 OCR 认花）只要整块落在编号条里就必须丢掉，空格子判空。"""
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                blank = make_crop(os.path.join(tmp, "blank.png"), 600, 150, ink=False)
                filled = make_crop(os.path.join(tmp, "filled.png"), 600, 150)
                items = [("1-1", blank), ("2-1", filled)]
                png, ranges = stitch_cells(items, os.path.join(tmp, "s.png"))
                blocks = [
                    # 空格子的编号块（被认花）+ 一个整块落在编号条里的黑块
                    {"block_bbox": [ranges[0]["x"], ranges[0]["label_y0"],
                                    ranges[0]["x"] + 120, ranges[0]["label_y0"] + 34],
                     "block_content": "[fi1-1]"},
                    # 有字的格子：编号与答案被合成一块，仍要能剥出答案
                    {"block_bbox": [ranges[1]["x"], ranges[1]["label_y0"],
                                    ranges[1]["x"] + 400, ranges[1]["y1"]],
                     "block_content": "[2-1]\nx=4"},
                ]
                srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "J9"}})
                srv.add_json("GET", "/api/v2/ocr/jobs/J9", {"data": {
                    "state": "done", "resultUrl": {"jsonUrl": srv.url("/res.jsonl")}}})
                srv.add_raw("GET", "/res.jsonl", _jsonl(blocks))
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=5, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual("", out["1-1"]["text"], "编号不能被当成答案")
                self.assertEqual("x=4", out["2-1"]["text"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_merged_block_split_by_labels(self):
        """实测：相邻格子会被 OCR 合成一块（哪怕间隔 64px），必须按我们印的编号拆开。"""
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                a = make_crop(os.path.join(tmp, "a.png"), 600, 150)
                b = make_crop(os.path.join(tmp, "b.png"), 600, 150)
                c = make_crop(os.path.join(tmp, "c.png"), 600, 150)
                items = [("3-2", a), ("4-1", b), ("4-2", c)]
                png, ranges = stitch_cells(items, os.path.join(tmp, "s.png"))
                merged = {"block_bbox": [13, ranges[0]["label_y0"], 883, ranges[-1]["y1"]],
                          "block_content": "[3-2]\n1/2\n[4-1]\n9\n[4-2]\n3"}
                srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "JM"}})
                srv.add_json("GET", "/api/v2/ocr/jobs/JM", {"data": {
                    "state": "done", "resultUrl": {"jsonUrl": srv.url("/res.jsonl")}}})
                srv.add_raw("GET", "/res.jsonl", _jsonl([merged]))
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=5, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual("1/2", out["3-2"]["text"])
                self.assertEqual("9", out["4-1"]["text"])
                self.assertEqual("3", out["4-2"]["text"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_aligned_formula_block_falls_back_to_order(self):
        """实测最坏情况：整张拼图被识别成一个 aligned 公式块，编号还被认错（[5-1]）。

        这时按编号切段顺序 == 格子顺序，用顺序兜底把内容放回对应格。
        """
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = [(k, make_crop(os.path.join(tmp, k + ".png"), 600, 150))
                         for k in ("3-1", "3-2", "4-1", "4-2")]
                png, ranges = stitch_cells(items, os.path.join(tmp, "s.png"))
                content = ("$$ \\begin{aligned}&\\left[5-1\\right]\\\\&\\quad4\\\\"
                           "&\\quad\\left[3-2\\right]\\\\&\\quad1/2\\\\"
                           "&\\quad\\left[4-1\\right]\\\\&\\quad9\\\\"
                           "&\\quad\\left[4-2\\right]\\\\&\\quad3\\end{aligned} $$")
                block = {"block_bbox": [12, ranges[0]["label_y0"], 880, ranges[-1]["y1"]],
                         "block_content": content}
                srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "JA"}})
                srv.add_json("GET", "/api/v2/ocr/jobs/JA", {"data": {
                    "state": "done", "resultUrl": {"jsonUrl": srv.url("/res.jsonl")}}})
                srv.add_raw("GET", "/res.jsonl", _jsonl([block]))
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=5, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual("4", out["3-1"]["text"], "编号认错也要靠顺序还原")
                self.assertEqual("1/2", out["3-2"]["text"])
                self.assertEqual("9", out["4-1"]["text"])
                self.assertEqual("3", out["4-2"]["text"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_one_blank_cell_no_order_guess(self):
        """段数对不上格子数时不许瞎猜顺序（宁可留空给人工确认）。"""
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                a = make_crop(os.path.join(tmp, "a.png"), 600, 150)
                b = make_crop(os.path.join(tmp, "b.png"), 600, 150, ink=False)
                items = [("1-1", a), ("2-1", b)]
                png, ranges = stitch_cells(items, os.path.join(tmp, "s.png"))
                block = {"block_bbox": [12, ranges[0]["label_y0"], 880, ranges[-1]["y1"]],
                         "block_content": "[1-1]\nx=4"}
                srv.add_json("POST", "/api/v2/ocr/jobs", {"data": {"jobId": "JB"}})
                srv.add_json("GET", "/api/v2/ocr/jobs/JB", {"data": {
                    "state": "done", "resultUrl": {"jsonUrl": srv.url("/res.jsonl")}}})
                srv.add_raw("GET", "/res.jsonl", _jsonl([block]))
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=5, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual("x=4", out["1-1"]["text"])
                self.assertEqual("", out["2-1"]["text"], "空格子不能靠顺序瞎猜")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_result_retries_once(self):
        """实测云端偶发返回空结果（jsonl 里没有 layoutParsingResults），必须重试一次。"""
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                a = make_crop(os.path.join(tmp, "a.png"), 600, 150)
                items = [("1-1", a)]
                png, ranges = stitch_cells(items, os.path.join(tmp, "s.png"))
                state = {"job": 0}

                def post_job(h, body):
                    state["job"] += 1
                    jid = "J%d" % state["job"]
                    h._reply(200, json.dumps({"data": {"jobId": jid}}).encode())

                def poll(h, body):
                    jid = h.path.rsplit("/", 1)[-1]
                    n = jid[1:]
                    h._reply(200, json.dumps({"data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": srv.url("/res_%s.jsonl" % n)}}}).encode())

                srv.routes[("POST", "/api/v2/ocr/jobs")] = post_job
                for n in ("1", "2"):
                    srv.routes[("GET", "/api/v2/ocr/jobs/J%s" % n)] = poll
                # 第一次是空 jsonl；第二次给正确块
                srv.add_raw("GET", "/res_1.jsonl", b"\n")
                srv.add_raw("GET", "/res_2.jsonl",
                            _jsonl([{"block_bbox": [ranges[0]["x"], ranges[0]["y0"],
                                                    ranges[0]["x"] + 400, ranges[0]["y1"]],
                                     "block_content": "[1-1]\nx=7"}]))
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=5, poll_s=0.05)
                out = r.read_batch(items)
                self.assertEqual("x=7", out["1-1"]["text"], "空结果应重试一次后成功")
                self.assertEqual(2, state["job"], "应该提交了两次 job")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_items(self):
        r = Recognizer(backend="paddle_vl", job_url="http://127.0.0.1:9/jobs", token="t",
                       timeout_s=0.2, poll_s=0.05)
        self.assertEqual({}, r.read_batch([]))

    def test_read_single_uses_batch(self):
        with MockServer() as srv:
            tmp = tempfile.mkdtemp(prefix="ocr_")
            try:
                items = [("1-1", self._crops(tmp)[0][1])]
                self._server_with_blocks(srv, items, ["x=9"])
                r = Recognizer(backend="paddle_vl", job_url=srv.url("/api/v2/ocr/jobs"),
                               token="t", timeout_s=5, poll_s=0.05)
                out = r.read(items[0][1], "fill")
                self.assertEqual("x=9", out["text"])
                self.assertEqual("paddle_vl", out["backend"])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 拼长图
class TestStitch(unittest.TestCase):
    def test_layout(self):
        tmp = tempfile.mkdtemp(prefix="stitch_")
        try:
            a = make_crop(os.path.join(tmp, "a.png"), 600, 150)
            b = make_crop(os.path.join(tmp, "b.png"), 400, 100)
            png, ranges = stitch_cells([("1-1", a), ("2-1", b)], os.path.join(tmp, "s.png"))
            self.assertTrue(os.path.exists(png))
            with Image.open(png) as im:
                # 2026-11-01 起：拼图先贴到「一页纸」画布（1200×1600）再送云端——
                # 条状长图会被版面模型整批丢掉（实测 0 个 block），所以宽度不再是 900。
                self.assertEqual(ocrmod.PAGE_CANVAS[0], im.width, "拼图已页面化：宽 = PAGE_CANVAS")
                self.assertEqual(ocrmod.PAGE_CANVAS[1], im.height, "拼图已页面化：高 = PAGE_CANVAS")
                W, H = im.size
            self.assertEqual(["1-1", "2-1"], [r["key"] for r in ranges])
            # 顺序、不重叠、格间纯白 ≥8px
            self.assertLess(ranges[0]["y1"], ranges[1]["y0"])
            arr = __import__("numpy").array(Image.open(png).convert("L"))
            for prev, nxt in zip(ranges, ranges[1:]):
                gap = arr[prev["y1"] + 1: nxt["label_y0"], :]
                self.assertGreaterEqual(gap.shape[0], 8, "格间纯白间隔必须 ≥8px")
                self.assertEqual(255, int(gap.min()), "格与格之间必须纯白，否则 OCR 会连成一块")
            # 每格上方有编号；高度保持原尺寸（只缩不放，放大字会被当图片块）
            for rng, (w0, h0) in zip(ranges, [(600, 150), (400, 100)]):
                self.assertAlmostEqual(h0, rng["y1"] - rng["y0"], delta=1)
                self.assertLessEqual(rng["w"], ocrmod.STITCH_W - 2 * ocrmod.STITCH_PAD)
                strip = arr[rng["label_y0"]:rng["label_y0"] + rng["label_h"], rng["x"]:rng["x"] + 200]
                self.assertLess(int(strip.min()), 128, "左上角必须印上 [idx-blank] 编号")
            self.assertLessEqual(max(r["y1"] for r in ranges), H)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 后端选择/配置
class TestBackendSelection(unittest.TestCase):
    def test_explicit_beats_config(self):
        r = Recognizer(backend="manual", config={"ocr": {"backend": "paddle_vl"}})
        self.assertEqual("manual", r.backend)

    def test_config_used_when_no_explicit(self):
        r = Recognizer(config={"ocr": {"backend": "vlm"}})
        self.assertEqual("vlm", r.backend)

    def test_env_override(self):
        old = os.environ.get("OCR_BACKEND")
        try:
            os.environ["OCR_BACKEND"] = "manual"
            r = Recognizer(config={"ocr": {"backend": "paddle_vl"}}, use_env=True)
            self.assertEqual("manual", r.backend)
        finally:
            if old is None:
                os.environ.pop("OCR_BACKEND", None)
            else:
                os.environ["OCR_BACKEND"] = old

    def test_default_is_manual_without_config(self):
        r = Recognizer(config={}, config_path=os.path.join(tempfile.gettempdir(), "no_such.json"))
        self.assertEqual("manual", r.backend)

    def test_load_config_example_template(self):
        """config.json 允许含真实 Token；example 模板必须把 token 留空，且结构一致。"""
        example = os.path.join(APP_DIR, "config.example.json")
        self.assertTrue(os.path.exists(example), "app/config.example.json 模板必须存在")
        with open(example, encoding="utf-8") as f:
            cfg = json.load(f)
        ocr = cfg["ocr"]
        self.assertEqual("", ocr["paddle_vl"]["token"], "example 模板不许带真实 token")
        self.assertEqual("", ocr["vlm"]["key"])
        self.assertIn(ocr["backend"], ("manual", "vlm", "paddle_vl", "local"))
        self.assertTrue(load_config(example)["ocr"]["backend"])


class TestCleanAnswerKeepsValueSignal(unittest.TestCase):
    """t39：clean_answer 不能把多值分隔符吃掉——判分层要知道「有几个值」。"""

    def test_keeps_chinese_comma(self):
        self.assertIn("，", clean_answer("3a^2 b，54"))
        self.assertEqual(2, len([p for p in clean_answer("3a^2 b，54").split("，") if p.strip()]))

    def test_keeps_ascii_comma_and_semicolon(self):
        self.assertIn(",", clean_answer("x=1, y=2"))
        self.assertIn(";", clean_answer("1;2"))

    def test_latex_clean_does_not_merge_values(self):
        out = clean_answer("$$3a^2b$$，$$54$$")
        self.assertIn("，", out)
        self.assertTrue(out.split("，")[0].strip())
        self.assertTrue(out.split("，")[1].strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
