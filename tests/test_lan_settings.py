# -*- coding: utf-8 -*-
"""t75 · 一套程序：局域网开关 + PIN 管理搬进设置页。

守的事（对应 CONTRACT 里明确的行为）：
  * 绑定地址只看 ``config.server.host``（缺省 0.0.0.0），``--lan`` 不再决定绑定；
  * 回环（127.0.0.1 / TestClient 默认）**永远放行**，不要访问码；
  * 开关关 → 非回环一律 **403** + 中文提示（页面里不含任何卷子/答案/题号）；
  * 开关开 → 无 PIN **401**；``?pin=`` 正确 → 200 + cookie；错误 PIN → 401 且不泄漏；
  * ``/settings`` 有「局域网访问」区块：开关、掩码 PIN、手机地址、二维码、防火墙命令；
  * PIN 可改（4-6 位数字校验）、可重新生成，**运行时生效**并写回 config.json；
  * ``/healthz`` 的 lan 段 = ``{on, url, pin_required, pin_tail}``，明文只给回环；
  * ``--lan`` 兼容：等价于打开开关，旧命令能启动。

跑法：``cd E:\\OCR\\app && python -m unittest tests.test_lan_settings -v``
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
LEAK_WORDS = ("题目清单", "标准答案", "答对", "备选", "知识点")


def text_of(html: str) -> str:
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def add_paper(db_path: str, out_dir: str, pid: str) -> str:
    """造一份卷子（用于验证「403/401 不泄漏卷子内容」）。"""
    os.makedirs(os.path.join(out_dir, pid), exist_ok=True)
    manifest = {"pid": pid, "title": "机密卷·别泄漏", "created_at": "2026-11-07T10:00:00+08:00",
                "items": [{"idx": 1, "no": "00001", "qtype": "选择", "kp_id": "1.1",
                           "answer_key": [{"blank": 1, "value": "A"}],
                           "options": ["A", "B", "C", "D"]}],
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


class _Base(unittest.TestCase):
    """每个类一套独立 目录/库/config（不碰真库与真 config.json）。"""

    LAN_ON = False
    PIN = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lan75_")
        cls.db_path = os.path.join(cls.tmp, "data", "app.db")
        os.makedirs(os.path.dirname(cls.db_path), exist_ok=True)
        build_db.build(limit=40, db_path=cls.db_path)
        cls.out_dir = os.path.join(cls.tmp, "out")
        cls.cfg_path = os.path.join(cls.tmp, "config.json")
        cfg = {"server": {"host": "0.0.0.0", "port": 8710, "lan_enabled": cls.LAN_ON}}
        if cls.PIN:
            cfg["server"]["pin"] = cls.PIN
        with open(cls.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        cls._env = {k: os.environ.get(k) for k in ("MATHAPP_LAN", "MATH_APP_HOST", "MATH_APP_PORT")}
        for key in ("MATHAPP_LAN", "MATH_APP_HOST", "MATH_APP_PORT"):
            os.environ.pop(key, None)
        try:
            cls.app = server.create_app(db_path=cls.db_path, config_path=cls.cfg_path,
                                        out_dir=cls.out_dir)
        finally:
            pass
        cls.pid = add_paper(cls.db_path, cls.out_dir, "P20261107-901")

    def setUp(self):
        # 每条用例一对干净客户端：cookie（mathapp_pin）不能跨用例泄漏，否则「错误 PIN 401」会被上一条的 cookie 放行
        self.local = TestClient(self.app)
        self.lan = TestClient(self.app, client=LAN_CLIENT)

    def tearDown(self):
        for client in (self.local, self.lan):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    def tearDownClass(cls):
        for key, value in (cls._env or {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def cfg(self) -> dict:
        with open(self.cfg_path, encoding="utf-8") as fh:
            return json.load(fh)


# ---------------------------------------------------------------- 1. 一套程序
class TestOneProgram(_Base):
    """启动不再需要 --lan；绑定地址来自 config.server.host。"""

    def test_default_host_is_all_interfaces(self):
        self.assertEqual("0.0.0.0", server.HOST, "默认监听 0.0.0.0（一套程序）")
        cfg = server.load_server_config(os.path.join(self.tmp, "nope.json"))
        self.assertEqual("0.0.0.0", cfg["host"])
        self.assertFalse(cfg["lan_enabled"], "没配过就是关着（安全默认）")

    def test_host_comes_from_config(self):
        path = os.path.join(self.tmp, "c2.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "192.168.0.10", "port": 9000,
                                  "lan_enabled": True, "pin": "2025"}}, fh)
        cfg = server.load_server_config(path)
        self.assertEqual("192.168.0.10", cfg["host"])
        self.assertEqual(9000, cfg["port"])
        self.assertTrue(cfg["lan_enabled"])
        self.assertEqual("2025", cfg["pin"])

    def test_lan_switch_is_not_derived_from_host(self):
        """host=0.0.0.0 但开关没开 → 不算开放（不能靠 host 猜）。"""
        path = os.path.join(self.tmp, "c3.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "0.0.0.0", "lan_enabled": False}}, fh)
        self.assertFalse(server.load_server_config(path)["lan_enabled"])

    def test_loopback_never_needs_pin(self):
        self.assertEqual(200, self.local.get("/").status_code)
        self.assertEqual(200, self.local.get("/settings").status_code)
        self.assertEqual(200, self.local.get("/papers/%s" % self.pid).status_code)


# ---------------------------------------------------------------- 2. 开关关 → 403
class TestLanOff(_Base):
    LAN_ON = False

    def test_lan_gets_403_with_chinese(self):
        r = self.lan.get("/papers/%s" % self.pid)
        self.assertEqual(403, r.status_code, "开关关着时非回环应 403")
        body = text_of(r.text)
        self.assertIn("局域网", body)
        self.assertIn("设置", body)

    def test_403_does_not_leak(self):
        r = self.lan.get("/papers/%s" % self.pid)
        body = text_of(r.text)
        for word in LEAK_WORDS + ("机密卷", "00001"):
            with self.subTest(word=word):
                self.assertNotIn(word, body, "403 页面泄漏了 %r" % word)

    def test_toggle_takes_effect_at_runtime(self):
        """设置页打开后，同一个 app 实例立刻放行（不用重启）。"""
        r = self.local.post("/settings/lan", data={"enabled": "1", "action": "save"})
        self.assertEqual(200, r.status_code, r.text[:300])
        try:
            self.assertTrue(self.app.state.lan_enabled)
            pin = self.app.state.lan_pin
            self.assertTrue(server.valid_pin(pin), "打开时必须生成/保留访问码")
            self.assertEqual(200, self.lan.get("/papers/%s?pin=%s" % (self.pid, pin)).status_code)
            # 再关掉 → 又 403
            r2 = self.local.post("/settings/lan", data={"action": "save"})   # 不勾 = 关
            self.assertEqual(200, r2.status_code)
            self.assertFalse(self.app.state.lan_enabled)
            self.assertEqual(403, self.lan.get("/papers/%s" % self.pid).status_code)
        finally:
            self.lan.post("/settings/lan", data={"action": "save"})

    def test_switch_persisted_to_config(self):
        self.local.post("/settings/lan", data={"enabled": "1", "action": "save"})
        try:
            saved = self.cfg()
            self.assertTrue(saved["server"]["lan_enabled"], "开关要写回 config.json")
            self.assertTrue(server.valid_pin(saved["server"]["pin"]))
        finally:
            self.local.post("/settings/lan", data={"action": "save"})
        self.assertFalse(self.cfg()["server"]["lan_enabled"])


# ---------------------------------------------------------------- 3. 开关开 → PIN
class TestLanOn(_Base):
    LAN_ON = True
    PIN = "2025"

    def test_no_pin_gets_401(self):
        r = self.lan.get("/papers/%s" % self.pid)
        self.assertEqual(401, r.status_code)
        body = text_of(r.text)
        self.assertIn("访问码", body)
        for word in LEAK_WORDS + ("机密卷", "00001"):
            with self.subTest(word=word):
                self.assertNotIn(word, body, "401 页面泄漏了 %r" % word)

    def test_wrong_pin_401(self):
        r = self.lan.get("/papers/%s?pin=0000" % self.pid)
        self.assertEqual(401, r.status_code)
        for word in LEAK_WORDS + ("机密卷", "00001"):
            self.assertNotIn(word, text_of(r.text))

    def test_correct_pin_200_and_cookie(self):
        client = TestClient(self.app, client=LAN_CLIENT)
        try:
            r = client.get("/?pin=%s" % self.PIN)
            self.assertEqual(200, r.status_code)
            self.assertIn("mathapp_pin", r.cookies, "要对一次种 cookie，之后不用再输")
            self.assertEqual(200, client.get("/papers/%s" % self.pid).status_code)
        finally:
            client.close()

    def test_change_pin_takes_effect(self):
        r = self.local.post("/settings/lan", data={"enabled": "1", "pin": "987654",
                                                  "action": "save"})
        self.assertEqual(200, r.status_code)
        try:
            self.assertEqual("987654", self.app.state.lan_pin)
            self.assertEqual(401, self.lan.get("/?pin=2025").status_code, "旧 PIN 必须失效")
            self.assertEqual(200, self.lan.get("/?pin=987654").status_code)
            self.assertEqual("987654", self.cfg()["server"]["pin"], "新 PIN 要写回 config.json")
        finally:
            self.local.post("/settings/lan", data={"enabled": "1", "pin": self.PIN,
                                                  "action": "save"})

    def test_bad_pin_rejected(self):
        for bad in ("12", "1234567", "abcd", "12 34"):
            with self.subTest(bad=bad):
                r = self.local.post("/settings/lan", data={"enabled": "1", "pin": bad,
                                                          "action": "save"})
                self.assertEqual(400, r.status_code, "非法 PIN 应 400")
                self.assertIn("4-6 位数字", text_of(r.text))
                self.assertEqual(self.PIN, self.app.state.lan_pin, "非法 PIN 不能覆盖旧值")

    def test_regen_gives_new_pin(self):
        r = self.local.post("/settings/lan", data={"enabled": "1", "action": "regen"})
        self.assertEqual(200, r.status_code)
        new_pin = self.app.state.lan_pin
        try:
            self.assertTrue(server.valid_pin(new_pin))
            self.assertEqual(200, self.lan.get("/?pin=%s" % new_pin).status_code)
        finally:
            self.local.post("/settings/lan", data={"enabled": "1", "pin": self.PIN,
                                                  "action": "save"})


# ---------------------------------------------------------------- 4. 设置页区块
class TestSettingsBlock(_Base):
    LAN_ON = True
    PIN = "2025"

    def test_page_has_lan_section(self):
        r = self.local.get("/settings")
        self.assertEqual(200, r.status_code)
        html = r.text
        for needle in ('id="lan"', "局域网访问", 'action="/settings/lan"',
                       "访问码", "手机地址", "/qr.png?target=judge", "netsh advfirewall"):
            with self.subTest(needle=needle):
                self.assertIn(needle, html, "设置页缺少 %r" % needle)

    def test_pin_is_masked(self):
        html = self.local.get("/settings").text
        self.assertIn("••", html, "页面要显示掩码")
        self.assertNotIn(">2025<", html)
        self.assertIn("25", html)          # 尾号露出来方便核对

    def test_qr_png_ok(self):
        r = self.local.get("/qr.png?target=judge")
        self.assertEqual(200, r.status_code)
        self.assertEqual("image/png", r.headers.get("content-type"))

    def test_firewall_command_present(self):
        html = self.local.get("/settings").text
        m = re.search(r'netsh advfirewall[^"<]+', html)
        self.assertIsNotNone(m, "要有可复制的防火墙命令")
        self.assertIn("8710", m.group(0))


# ---------------------------------------------------------------- 5. /healthz
class TestHealthz(_Base):
    LAN_ON = True
    PIN = "2025"

    def test_lan_segment_fields(self):
        data = self.local.get("/healthz").json()
        lan = data["lan"]
        for key in ("on", "url", "pin_required", "pin_tail"):
            with self.subTest(key=key):
                self.assertIn(key, lan, "/healthz 的 lan 段缺 %s" % key)
        self.assertTrue(lan["on"])
        self.assertTrue(lan["pin_required"])
        self.assertEqual(server.pin_tail(self.PIN), lan["pin_tail"])
        self.assertEqual("2025", lan.get("pin"), "回环要能读到明文（本机自用）")

    def test_pin_plaintext_only_loopback(self):
        data = self.lan.get("/healthz").json()      # 非回环：/healthz 免 pin，但不能给明文
        self.assertNotIn("pin", data["lan"], "非回环不该拿到明文 PIN")
        self.assertEqual(server.pin_tail(self.PIN), data["lan"]["pin_tail"])


# ---------------------------------------------------------------- 6. --lan 兼容
class TestLanCompatFlag(unittest.TestCase):
    def test_lan_flag_means_switch_on(self):
        tmp = tempfile.mkdtemp(prefix="lanflag_")
        cfg = os.path.join(tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "0.0.0.0", "port": 8710}}, fh)
        old = os.environ.get("MATHAPP_LAN")
        try:
            os.environ.pop("MATHAPP_LAN", None)
            self.assertFalse(server.load_server_config(cfg)["lan_enabled"])
            out = server.save_server_settings(cfg, lan_enabled=True)
            self.assertTrue(out["lan_enabled"])
            self.assertTrue(server.load_server_config(cfg)["lan_enabled"],
                            "--lan 等价于打开开关（写进 config）")
            self.assertTrue(os.path.isfile(cfg + ".bak"), "改配置要留 .bak")
        finally:
            if old is None:
                os.environ.pop("MATHAPP_LAN", None)
            else:
                os.environ["MATHAPP_LAN"] = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_launcher_lan_flag_opens_switch(self):
        """旧命令 ``launcher --lan`` 仍能用：等价于把开关写进 config，不报错。"""
        import launcher  # noqa: PLC0415
        tmp = tempfile.mkdtemp(prefix="lanflag2_")
        cfg = os.path.join(tmp, "config.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "0.0.0.0", "port": 8710}}, fh)
        old_env = {k: os.environ.get(k) for k in ("MATHAPP_LAN", "MATH_APP_HOST", "MATH_APP_PORT")}
        try:
            for key in old_env:
                os.environ.pop(key, None)
            conf = launcher._load_cfg(cfg, None, None, True)
            self.assertTrue(conf["lan_enabled"], "launcher --lan 要打开开关")
            self.assertEqual("0.0.0.0", conf["host"], "绑定地址仍来自 config.server.host")
            with open(cfg, encoding="utf-8") as fh:
                self.assertTrue(json.load(fh)["server"]["lan_enabled"])
            # 不传 --lan：开关保持原样（不被覆盖）
            os.environ.pop("MATHAPP_LAN", None)
            self.assertTrue(launcher._load_cfg(cfg, None, None, False)["lan_enabled"])
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmp, ignore_errors=True)

    def test_launcher_host_never_forced_by_lan(self):
        import launcher  # noqa: PLC0415
        self.assertTrue(launcher.server_is_lan_host("0.0.0.0"))
        self.assertFalse(launcher.server_is_lan_host("127.0.0.1"))
        self.assertTrue(launcher.server_is_lan_host("192.168.0.140"))


if __name__ == "__main__":
    unittest.main()
