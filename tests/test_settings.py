# -*- coding: utf-8 -*-
"""t47 · 设置页：自己填 OCR/AI 的 Key、切后端、测连接（不回显明文）。

守的事：
  * `/settings` 200，4 个后端都在下拉里；token/key 是 password 输入框；
  * **页面上不出现明文 token**（只出现「已配置 · 尾号 xxxx」）；
  * 保存：只改 `config.json` 的 `ocr` 段，**其它键一个不丢**，写前留 `config.json.bak`；
  * 输入框留空 = 不修改；「清除」= 删掉该后端的 token/key；
  * 脏值（拼错的 backend）被拒并给中文提示，config 不变；
  * 「测试连接」在 token 为空时给明确提示，不 500；
  * `/health` 显示后端名与「token 已配置（尾号）」，不泄露完整 token。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SECRET = "9c06deadbeef1234"
VLM_SECRET = "sk-vlm-secret-9876"


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="settings_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        import build_db
        build_db.build(limit=30, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        cls.write_cfg({
            "_note": "别的键不能丢",
            "ocr": {
                "backend": "paddle_vl", "timeout_s": 120, "poll_s": 2,
                "paddle_vl": {"token": SECRET, "model": "PaddleOCR-VL-1.6",
                              "job_url": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"},
                "vlm": {"base_url": "https://example.invalid", "key": VLM_SECRET, "model": "m1"},
            },
            "server": {"host": "127.0.0.1", "port": 8710, "pin": "1234"},
            "paths": {"qbank": "D:/qbank-demo"},
        })
        cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg_path,
                                    out_dir=cls.out_dir)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def write_cfg(cls, cfg):
        with open(cls.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)

    def cfg(self):
        with open(self.cfg_path, encoding="utf-8") as fh:
            return json.load(fh)


class TestPage(_Base):
    def test_page_200_with_four_backends(self):
        r = self.client.get("/settings")
        self.assertEqual(200, r.status_code)
        html = r.text
        for backend in ("paddle_vl", "vlm", "local", "manual"):
            with self.subTest(backend=backend):
                self.assertIn('value="%s"' % backend, html)

    def test_password_inputs_and_no_plaintext(self):
        html = self.client.get("/settings").text
        self.assertIn('type="password"', html)
        self.assertNotIn(SECRET, html, "页面不能回显明文 token")
        self.assertNotIn(VLM_SECRET, html, "页面不能回显明文 vlm key")
        self.assertIn("尾号", text_of(html))
        self.assertIn("1234", text_of(html), "要显示 token 尾号方便核对")

    def test_nav_has_settings(self):
        for path in ("/", "/papers", "/guide"):
            with self.subTest(page=path):
                self.assertIn('href="/settings"', self.client.get(path).text)


class TestSave(_Base):
    def test_save_updates_ocr_keeps_other_keys(self):
        r = self.client.post("/settings", data={"backend": "vlm", "key": "sk-new-key-0001",
                                               "base_url": "https://api.example.com",
                                               "model": "vision-1"},
                             follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), r.text[:400])
        cfg = self.cfg()
        self.assertEqual("vlm", cfg["ocr"]["backend"])
        self.assertEqual("sk-new-key-0001", cfg["ocr"]["vlm"]["key"])
        self.assertEqual("vision-1", cfg["ocr"]["vlm"]["model"])
        # 其它键一个不丢
        self.assertEqual("别的键不能丢", cfg["_note"])
        self.assertEqual("1234", cfg["server"]["pin"])
        self.assertEqual("D:/qbank-demo", cfg["paths"]["qbank"])
        self.assertTrue(os.path.isfile(self.cfg_path + ".bak"), "写前要留 config.json.bak")

    def test_blank_token_keeps_old(self):
        self.write_cfg(self.cfg())
        before = self.cfg()["ocr"]["paddle_vl"]["token"]
        r = self.client.post("/settings", data={"backend": "paddle_vl", "token": "",
                                               "model": "PaddleOCR-VL-1.6"},
                             follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), r.text[:300])
        self.assertEqual(before, self.cfg()["ocr"]["paddle_vl"]["token"], "留空不该改写已有 token")

    def test_clear_token(self):
        r = self.client.post("/settings", data={"backend": "paddle_vl", "clear": "paddle_vl"},
                             follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), r.text[:300])
        self.assertEqual("", self.cfg()["ocr"]["paddle_vl"].get("token") or "")

    def test_bad_backend_rejected(self):
        before = self.cfg()
        r = self.client.post("/settings", data={"backend": "paddle-vl-typo"},
                             follow_redirects=False)
        self.assertEqual(400, r.status_code)
        self.assertIn("后端", text_of(r.text))
        self.assertEqual(before, self.cfg(), "脏值不该写 config")


class TestConnectionTest(_Base):
    def test_empty_token_gives_clear_message(self):
        self.write_cfg({"_note": "x", "ocr": {"backend": "paddle_vl", "paddle_vl": {"token": ""}}})
        r = self.client.post("/settings/test", data={"backend": "paddle_vl"},
                             follow_redirects=False)
        self.assertEqual(200, r.status_code, "空 token 不能 500")
        self.assertIn("token", text_of(r.text).lower())
        self.assertNotIn("Traceback", r.text)

    def test_manual_backend_says_no_network(self):
        self.write_cfg({"_note": "x", "ocr": {"backend": "manual"}})
        r = self.client.post("/settings/test", data={"backend": "manual"}, follow_redirects=False)
        self.assertEqual(200, r.status_code)
        self.assertIn("人工", text_of(r.text))


class TestProbeVerdict(unittest.TestCase):
    """测试连接的结论必须诚实：连通但没解析出 block ≠ Key 坏了。"""

    def test_connected_no_block_is_not_blamed_on_key(self):
        v = server.classify_ocr_probe(text="", backend="paddle_vl->manual",
                                      error="RuntimeError: OCR 结果里没有可用的块", elapsed=0.78)
        self.assertEqual("connected_no_content", v["kind"])
        self.assertFalse(v["ok"])
        msg = v["message"]
        self.assertIn("已连通", msg)
        self.assertIn("不代表 Key 有问题", msg)
        for bad in ("检查 Key/Token", "Key 有问题，去查", "额度"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, msg.split("不代表 Key 有问题")[0])

    def test_auth_error_blames_key(self):
        v = server.classify_ocr_probe(text="", backend="paddle_vl", error="HTTP 401 Unauthorized")
        self.assertEqual("auth_or_quota", v["kind"])
        self.assertIn("Key", v["message"])

    def test_network_error_blames_network(self):
        v = server.classify_ocr_probe(text="", backend="vlm", error="connect timeout after 30s")
        self.assertEqual("network", v["kind"])
        self.assertIn("网络", v["message"])

    def test_success_shows_text(self):
        v = server.classify_ocr_probe(text="40 + 2 = 42", backend="paddle_vl")
        self.assertTrue(v["ok"])
        self.assertIn("40 + 2 = 42", v["message"])

    def test_selftest_image_is_document_sized(self):
        import tempfile
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="probe_img_")
        try:
            path = os.path.join(tmp, "probe.png")
            server.make_selftest_image(path)
            with Image.open(path) as img:
                self.assertGreaterEqual(img.width, 800, "测试图太小，云端会解析不出版式")
                self.assertGreaterEqual(img.height, 200)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestProbePage(unittest.TestCase):
    """整页口径：mock 云端「连通但没 block」时，页面不许出现「检查 Key/Token」。"""

    def test_page_copy_when_cloud_returns_no_block(self):
        import ocr
        tmp = tempfile.mkdtemp(prefix="probe_page_")
        try:
            db = os.path.join(tmp, "data", "app.db")
            os.makedirs(os.path.dirname(db), exist_ok=True)
            import build_db
            build_db.build(limit=20, db_path=db)
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"ocr": {"backend": "paddle_vl",
                                   "paddle_vl": {"token": "tok-1234", "model": "m"}}}, fh)
            app = server.create_app(db_path=db, config_path=cfg,
                                    out_dir=os.path.join(tmp, "out"))
            client = TestClient(app)

            def fake_read(self, image_path, kind=None):
                self.last_error = "RuntimeError: OCR 结果里没有可用的块"
                return {"text": "", "confidence": 0.0, "backend": "paddle_vl->manual"}

            with unittest.mock.patch.object(ocr.Recognizer, "read", fake_read):
                r = client.post("/settings/test", data={"backend": "paddle_vl"},
                                follow_redirects=False)
            body = text_of(r.text)
            self.assertEqual(200, r.status_code)
            self.assertIn("已连通", body)
            self.assertNotIn("检查 Key/Token", body)
            self.assertNotIn("额度", body)
            client.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestLocalBackendHonesty(_Base):
    """本机没有 winsdk → local 必须如实标「本机不支持」，不能装能用、不能怪 Key。"""

    def test_local_availability_probe(self):
        support = server.local_ocr_support()
        for key in ("available", "windows", "winsdk", "detail", "error"):
            self.assertIn(key, support)
        if support["winsdk"]:
            self.skipTest("这台机器装了 winsdk，local 可用，跳过不可用分支")
        self.assertFalse(support["available"])
        self.assertIn("本机不支持", support["detail"])

    def test_settings_marks_local_unavailable(self):
        html = self.client.get("/settings").text
        body = text_of(html)
        support = server.local_ocr_support()
        if support["available"]:
            self.skipTest("local 可用，跳过")
        self.assertIn("本机不支持", body)
        self.assertIn("winsdk", body)
        self.assertIn("本机离线 OCR", body)

    def test_local_selftest_short_circuits(self):
        support = server.local_ocr_support()
        if support["available"]:
            self.skipTest("local 可用，跳过")
        r = self.client.post("/settings/test", data={"backend": "local"}, follow_redirects=False)
        self.assertEqual(200, r.status_code)
        body = text_of(r.text)
        self.assertIn("本机不支持", body)
        self.assertNotIn("检查 Key/Token", body, "本机不支持不该让用户去查 Key")
        self.assertIn("0.0 秒", body, "不该真跑一遍识别")


class TestPdfBackendUi(_Base):
    def test_health_reports_pdf_backend(self):
        payload = self.client.get("/healthz").json()
        self.assertIn("pdf_backend", payload)
        self.assertIn("chosen", payload["pdf_backend"])
        keys = [c["key"] for c in payload["checks"]]
        self.assertIn("pdf", keys, "/healthz 要有一条 PDF 后端检查")
        row = next(c for c in payload["checks"] if c["key"] == "pdf")
        self.assertIn(row["status"], ("ok", "warn", "fail"))
        if payload["pdf_backend"]["chosen"] is None:
            self.assertEqual("fail", row["status"])
            self.assertTrue(any("LibreOffice" in h or "Word" in h for h in row["hints"]))

    def test_libreoffice_warns_about_layout(self):
        import render
        fake = {"word": False, "libreoffice": True, "chosen": "libreoffice",
                "detail": "LibreOffice（C:/Program Files/LibreOffice/program/soffice.exe）",
                "word_path": None, "soffice_path": "soffice", "last_used": None}
        with unittest.mock.patch.object(render, "probe_backend", lambda: fake):
            payload = self.client.get("/healthz").json()
            row = next(c for c in payload["checks"] if c["key"] == "pdf")
            self.assertEqual("warn", row["status"])
            self.assertIn("LibreOffice", row["detail"])
            self.assertIn("差异", row["detail"])
            page = text_of(self.client.get("/settings").text)
            self.assertIn("PDF 后端", page)
            self.assertIn("LibreOffice", page)
            self.assertIn("版面可能与 Word 有差异", page)

    def test_no_backend_gets_install_hints(self):
        import render
        fake = {"word": False, "libreoffice": False, "chosen": None,
                "detail": "没有可用的 PDF 后端", "word_path": None,
                "soffice_path": None, "last_used": None}
        with unittest.mock.patch.object(render, "probe_backend", lambda: fake):
            payload = self.client.get("/healthz").json()
            row = next(c for c in payload["checks"] if c["key"] == "pdf")
            self.assertEqual("fail", row["status"])
            blob = " ".join(row["hints"])
            self.assertIn("Word", blob)
            self.assertIn("LibreOffice", blob)


class TestIndexOcrNotice(_Base):
    def test_manual_backend_shows_banner(self):
        self.write_cfg({"_note": "x", "ocr": {"backend": "manual"}})
        t = text_of(self.client.get("/").text)
        self.assertIn("尚未配置识别服务", t)
        self.assertIn("人工确认", t)
        self.assertIn('href="/settings"', self.client.get("/").text)

    def test_token_configured_hides_banner(self):
        self.write_cfg({"_note": "x", "ocr": {"backend": "paddle_vl",
                                              "paddle_vl": {"token": "tok-9c06"}}})
        t = text_of(self.client.get("/").text)
        self.assertNotIn("尚未配置识别服务", t)


class TestHealth(_Base):
    def test_health_shows_backend_and_token_tail(self):
        t = text_of(self.client.get("/health").text)
        self.assertIn("paddle_vl", t)
        self.assertIn("尾号", t)
        self.assertNotIn(SECRET, t, "/health 不能泄露完整 token")


if __name__ == "__main__":
    unittest.main()
