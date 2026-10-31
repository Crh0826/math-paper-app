# -*- coding: utf-8 -*-
"""t40 · 局域网访问（0.0.0.0 + 手机二维码 + 4 位访问码 PIN）。

守的事：
  * LAN 判定：`MATHAPP_LAN=1` 或 host 非 loopback 都算对外开放，此时**必须**有 pin；
  * 非 loopback 不带 pin → 401 极简输入页，且**页面里不能有**任何卷子/答案/题库文本；
  * `?pin=` 正确 → 200 + Set-Cookie（30 天）；之后凭 cookie 直达任意页面；
  * loopback（127.0.0.1 / TestClient 默认）不需要 pin；
  * `/qr.png` 是 image/png，且能被解码出含 `:8710` 的地址；
  * `/healthz` 有 `lan_url` 字段；LAN 模式下首页有二维码区块；
  * `start.bat lan` / `start_lan.bat` 真的会把 host 换成 0.0.0.0，并打印防火墙指引。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import build_db  # noqa: E402
import db as dbmod  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

LAN_CLIENT = ("192.168.0.50", 43821)
LEAK_WORDS = ("题目清单", "标准答案", "答对", "判对", "备选", "知识点")


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def add_paper(db_path: str, out_dir: str, pid: str) -> str:
    folder = os.path.join(out_dir, pid)
    os.makedirs(folder, exist_ok=True)
    manifest = {"pid": pid, "title": "LAN 私密卷", "created_at": "2026-10-31T21:00:00+08:00",
                "items": [{"idx": 1, "no": "00001", "qtype": "选择", "kp_id": "1.1",
                           "answer_key": [{"blank": 1, "value": "A"}], "options": ["A", "B"]}],
                "card": {"n_pages": 1}}
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest,"
            " docx_path, pdf_path, n_items) VALUES(?,?,?,?,?,?,?,?)",
            (pid, manifest["created_at"], manifest["title"], "{}",
             json.dumps(manifest, ensure_ascii=False), "", "", 1))
        conn.commit()
    finally:
        conn.close()
    return pid


class TestLanDetection(unittest.TestCase):
    def test_is_lan_host(self):
        for host in ("0.0.0.0", "192.168.0.140", "10.0.0.5"):
            with self.subTest(host=host):
                self.assertTrue(server.is_lan_host(host))
        for host in ("127.0.0.1", "localhost", "::1", ""):
            with self.subTest(host=host):
                self.assertFalse(server.is_lan_host(host))

    def test_env_enables_lan(self):
        old = os.environ.get("MATHAPP_LAN")
        try:
            os.environ["MATHAPP_LAN"] = "1"
            cfg = server.load_server_config()
            self.assertTrue(cfg["lan"])
            text = server.lan_startup_text()
            self.assertIn("本机：", text)
            self.assertIn("局域网：", text)
            self.assertIn("netsh advfirewall", text)
        finally:
            if old is None:
                os.environ.pop("MATHAPP_LAN", None)
            else:
                os.environ["MATHAPP_LAN"] = old

    def test_local_mode_has_no_lan_block(self):
        old = os.environ.pop("MATHAPP_LAN", None)
        try:
            text = server.lan_startup_text()
            self.assertIn("本机：", text)
            self.assertNotIn("netsh advfirewall", text)
        finally:
            if old is not None:
                os.environ["MATHAPP_LAN"] = old


class _LanBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lan_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=40, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        with open(cls.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "0.0.0.0", "port": 8710}}, fh)
        cls._env = os.environ.get("MATHAPP_LAN")
        os.environ["MATHAPP_LAN"] = "1"
        try:
            cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg_path,
                                        out_dir=cls.out_dir)
        finally:
            if cls._env is None:
                os.environ.pop("MATHAPP_LAN", None)
            else:
                os.environ["MATHAPP_LAN"] = cls._env
        cls.pin = cls.app.state.lan_pin
        cls.pid = add_paper(cls.db_path, cls.out_dir, "P20261031-901")
        cls.local = TestClient(cls.app)                      # 默认 client=("testclient", ...)
        cls.lan = TestClient(cls.app, client=LAN_CLIENT)      # 模拟手机

    @classmethod
    def tearDownClass(cls):
        cls.local.close()
        cls.lan.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPinGate(_LanBase):
    def test_lan_mode_has_pin(self):
        self.assertTrue(self.app.state.lan)
        self.assertRegex(self.pin, r"^\d{4}$")
        with open(self.cfg_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(self.pin, saved["server"]["pin"], "pin 要写回 config.json 的 server.pin")

    def test_loopback_needs_no_pin(self):
        self.assertEqual(200, self.local.get("/").status_code)
        self.assertEqual(200, self.local.get("/papers/%s" % self.pid).status_code)

    def test_lan_without_pin_gets_gate_and_no_leak(self):
        r = self.lan.get("/papers/%s" % self.pid)
        self.assertEqual(401, r.status_code)
        body = text_of(r.text)
        self.assertIn("访问码", body)
        for word in LEAK_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, body, "未通过访问码的页面泄露了「%s」" % word)
        self.assertNotIn("LAN 私密卷", body)
        self.assertNotIn("00001", body)

    def test_lan_wrong_pin_rejected(self):
        wrong = "0000" if self.pin != "0000" else "1111"
        r = self.lan.get("/?pin=%s" % wrong)
        self.assertEqual(401, r.status_code)
        self.assertIn("不对", text_of(r.text))

    def test_lan_correct_pin_sets_cookie(self):
        client = TestClient(self.app, client=LAN_CLIENT)
        try:
            r = client.get("/?pin=%s" % self.pin)
            self.assertEqual(200, r.status_code)
            self.assertIn("mathapp_pin", r.cookies, "要通过 Set-Cookie 记住 30 天")
            # cookie 生效后，别的页面直接进
            self.assertEqual(200, client.get("/papers/%s" % self.pid).status_code)
            self.assertEqual(200, client.get("/papers").status_code)
        finally:
            client.close()

    def test_static_is_open_but_data_is_not(self):
        self.assertEqual(200, self.lan.get("/static/app.css").status_code)
        self.assertEqual(401, self.lan.get("/papers").status_code)

    def test_healthz_reports_lan_block(self):
        payload = self.lan.get("/healthz").json()
        self.assertIn("lan_url", payload)
        lan = payload["lan"]
        for key in ("on", "pin_required", "url", "ip", "port", "qr"):
            with self.subTest(key=key):
                self.assertIn(key, lan)
        self.assertTrue(lan["on"])
        self.assertTrue(lan["pin_required"])
        self.assertNotIn("pin", lan, "非 loopback 的 /healthz 不许带明文 pin")
        if payload["lan_url"]:
            self.assertRegex(payload["lan_url"], r"^http://\d+\.\d+\.\d+\.\d+:8710/$")

    def test_healthz_loopback_can_see_pin(self):
        lan = self.local.get("/healthz").json()["lan"]
        self.assertEqual(self.pin, lan.get("pin"), "本机 /healthz 要能拿到访问码，方便核对")


class TestQr(_LanBase):
    def test_qr_png_decodes_to_judge_url(self):
        r = self.local.get("/qr.png?target=judge")
        self.assertEqual(200, r.status_code)
        self.assertEqual("image/png", r.headers.get("content-type"))
        self.assertGreater(len(r.content), 300)
        # 用 opencv 解二维码，确认地址确实是本机 LAN 判卷页
        try:
            import cv2
            import numpy as np
        except ImportError:  # pragma: no cover
            self.skipTest("没装 opencv，跳过解码")
        img = cv2.imdecode(np.frombuffer(r.content, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(img)
        text, _points, _straight = cv2.QRCodeDetector().detectAndDecode(img)
        self.assertTrue(text, "二维码解不出来")
        self.assertIn(":8710", text)
        self.assertIn("/submissions", text)
        self.assertIn("pin=%s" % self.pin, text)

    def test_home_page_shows_qr_block(self):
        html = self.local.get("/?pin=%s" % self.pin).text
        t = text_of(html)
        self.assertIn("手机拍照判卷", t)
        self.assertIn("/qr.png?target=judge", html)
        self.assertIn("扫一下", t)

    def test_local_mode_hint_points_to_start_lan(self):
        app = server.create_app(db_path=self.db_path, config_path=self.cfg_path,
                                out_dir=self.out_dir)
        app.state.lan = False
        app.state.lan_pin = ""
        client = TestClient(app)
        try:
            t = text_of(client.get("/").text)
            self.assertIn("start_lan.bat", t)
        finally:
            client.close()


class TestLaunchers(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(APP_DIR, name), "rb") as fh:
            return fh.read().decode("utf-8")

    def test_start_bat_supports_lan(self):
        t = self.read("start.bat")
        self.assertIn('if /i "%~1"=="lan"', t)
        self.assertIn("MATHAPP_LAN", t)
        self.assertIn('set "HOST=0.0.0.0"', t)
        self.assertIn("--host %HOST%", t)
        self.assertIn("lan_startup_text", t)
        self.assertNotIn("--host 127.0.0.1 --port 8710\r\n", t)

    def test_start_lan_bat_exists(self):
        t = self.read("start_lan.bat")
        self.assertIn('call "%~dp0start.bat" lan', t)
        with open(os.path.join(APP_DIR, "start_lan.bat"), "rb") as fh:
            raw = fh.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "bat 不能带 BOM")
        self.assertIn(b"\r\n", raw)

    def test_readme_documents_lan(self):
        t = self.read("README.md")
        for word in ("局域网", "start_lan.bat", "访问码", "netsh advfirewall"):
            with self.subTest(word=word):
                self.assertIn(word, t)


if __name__ == "__main__":
    unittest.main()
