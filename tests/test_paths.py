# -*- coding: utf-8 -*-
"""t47 · 路径可移植：qbank / data / out 都不许写死 E:\\OCR。

解析顺序（三级 + 兜底）：
  1. 环境变量（MATH_APP_QBDIR / MATH_APP_DATA / MATH_APP_OUT / MATH_APP_CONFIG）
  2. config.json 的 paths 段（qbank / data / out）
  3. 程序目录下的 qbank/ data/ out/
  4. 只有 qbank 还留着 E:\\OCR 这个旧机器兜底（别的机器没有就返回程序目录的 qbank）
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import paths  # noqa: E402


def write_cfg(path: str, cfg: dict) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    return path


class TestQbankResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paths_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self._env = {k: os.environ.pop(k, None)
                     for k in ("MATH_APP_QBDIR", "MATH_APP_DATA", "MATH_APP_OUT",
                               "MATH_APP_CONFIG", "MATH_QB_ROOT")}

    def tearDown(self):
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_wins(self):
        target = os.path.join(self.tmp, "qbank-env")
        os.makedirs(target)
        write_cfg(self.cfg, {"paths": {"qbank": os.path.join(self.tmp, "qbank-cfg")}})
        os.environ["MATH_APP_QBDIR"] = target
        self.assertEqual(os.path.abspath(target), paths.resolve_qbank(self.cfg))

    def test_config_wins_over_default(self):
        target = os.path.join(self.tmp, "qbank-cfg")
        os.makedirs(target)
        write_cfg(self.cfg, {"paths": {"qbank": target}})
        self.assertEqual(os.path.abspath(target), paths.resolve_qbank(self.cfg))

    def test_relative_config_path_is_relative_to_app_dir(self):
        os.makedirs(os.path.join(APP_DIR, "qbank"), exist_ok=True)
        write_cfg(self.cfg, {"paths": {"qbank": "qbank"}})
        self.assertEqual(os.path.join(APP_DIR, "qbank"), paths.resolve_qbank(self.cfg))

    def test_fallback_is_never_hardcoded_ocr_root(self):
        """没配、没有 qbank/ 目录时也不能直接返回 E:\\OCR（别的机器没有这个盘）。"""
        got = paths.resolve_qbank(self.cfg)
        self.assertTrue(os.path.isabs(got))
        self.assertEqual(os.path.join(APP_DIR, "qbank"), got)

    def test_legacy_fallback_used_when_dir_exists(self):
        legacy = os.path.join(self.tmp, "legacy-ocr")
        os.makedirs(legacy)
        write_cfg(self.cfg, {})
        self.assertEqual(os.path.abspath(legacy),
                         paths.resolve_qbank(self.cfg, legacy=legacy))

    def test_data_and_out_under_app_dir(self):
        write_cfg(self.cfg, {})
        self.assertEqual(os.path.join(APP_DIR, "data"), paths.resolve_data_dir(self.cfg))
        self.assertEqual(os.path.join(APP_DIR, "out"), paths.resolve_out_dir(self.cfg))
        write_cfg(self.cfg, {"paths": {"data": os.path.join(self.tmp, "d"),
                                       "out": os.path.join(self.tmp, "o")}})
        self.assertEqual(os.path.abspath(os.path.join(self.tmp, "d")),
                         paths.resolve_data_dir(self.cfg))
        self.assertEqual(os.path.abspath(os.path.join(self.tmp, "o")),
                         paths.resolve_out_dir(self.cfg))

    def test_describe_shows_source(self):
        write_cfg(self.cfg, {"paths": {"qbank": os.path.join(self.tmp, "qb")}})
        d = paths.describe(self.cfg)
        self.assertIn("qbank", d)
        self.assertEqual("config.json", d["qbank"]["source"])
        self.assertTrue(d["qbank"]["path"].endswith("qb"))


class TestServerUsesPaths(unittest.TestCase):
    def test_server_defaults_come_from_paths(self):
        import server
        self.assertEqual(os.path.join(paths.resolve_data_dir(), "app.db"),
                         os.path.abspath(server.DEFAULT_DB))
        self.assertEqual(paths.resolve_out_dir(), os.path.abspath(server.OUT_DIR))

    def test_no_hardcoded_root_in_server_source(self):
        with open(os.path.join(APP_DIR, "server.py"), encoding="utf-8") as fh:
            source = fh.read()
        # 只允许出现在"提示文案"里（说明旧路径），不允许在路径常量里拼接
        self.assertNotIn('os.path.join(r"E:\\OCR"', source)
        self.assertNotIn('BASE_DIR / "data"', source)


class TestQbankUsable(unittest.TestCase):
    """把 qbank 指向一个临时目录（放 2 个 docx），paths 要能解析到它。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qbank_")
        self.cfg = os.path.join(self.tmp, "config.json")
        self._env = {k: os.environ.pop(k, None)
                     for k in ("MATH_APP_QBDIR", "MATH_QB_ROOT")}

    def tearDown(self):
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_db_uses_paths(self):
        """build_db 的题库目录要走 paths（不是源码里写死 E:\\OCR）。"""
        try:
            import build_db
        except ImportError:  # pragma: no cover
            self.skipTest("没有 build_db")
        self.assertTrue(hasattr(build_db, "ROOT"), "build_db 要能被指向别的题库目录")
        expected = paths.resolve_qbank(legacy=paths.LEGACY_QBANK)
        self.assertEqual(os.path.abspath(expected), os.path.abspath(build_db.ROOT),
                         "build_db.ROOT 没走 paths：%r" % (build_db.ROOT,))
        with open(os.path.join(APP_DIR, "build_db.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('ROOT = r"E:\\OCR"', source, "build_db.py 里还有写死的 ROOT")

    def test_custom_qbank_dir_is_picked_up(self):
        """把 qbank 指到临时目录（放 2 个 docx）→ paths 解析到它，build_db 会跟着走。"""
        target = os.path.join(self.tmp, "qbank2")
        os.makedirs(target)
        for name in ("00001.docx", "00002.docx"):
            open(os.path.join(target, name), "wb").close()
        write_cfg(self.cfg, {"paths": {"qbank": target}})
        self.assertEqual(os.path.abspath(target), paths.resolve_qbank(self.cfg, legacy=paths.LEGACY_QBANK))


if __name__ == "__main__":
    unittest.main()
