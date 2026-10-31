# -*- coding: utf-8 -*-
"""路径解析：题库（qbank）/ data / out / config 都不许写死 `E:\\OCR`。

别人的机器上 clone 下来就能跑：把程序放任意目录，题库放 `<程序>/qbank/`，或者改
`config.json` 的 `paths.qbank`，或者设环境变量 `MATH_APP_QBDIR`。

解析顺序（先命中先用）：

    题库 qbank   : MATH_APP_QBDIR / MATH_QB_ROOT > config.paths.qbank > 显式 legacy > <程序>/qbank
    数据 data    : MATH_APP_DATA        > config.paths.data    > <程序>/data
    产物 out     : MATH_APP_OUT         > config.paths.out     > <程序>/out
    配置 config  : MATH_APP_CONFIG      > <程序>/config.json

`legacy` 只给「开发机老路径」用（默认 `E:\\OCR`），而且**只在它真的存在时才生效**，
别的机器上自然跳过 —— 所以它不会把程序绑死在这台机器。
"""
from __future__ import annotations

import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_QBANK = r"E:\OCR"


def app_dir() -> str:
    return APP_DIR


def config_path(config_path: str | None = None) -> str:
    """config.json 的绝对路径（环境变量 MATH_APP_CONFIG 优先）。"""
    return os.path.abspath(config_path or os.environ.get("MATH_APP_CONFIG")
                           or os.path.join(APP_DIR, "config.json"))


def read_config(config_path: str | None = None) -> dict:
    """读 config.json；不存在 / 坏格式返回 {}（绝不抛异常）。"""
    try:
        with open(config_path_fn(config_path), encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def config_path_fn(config_path: str | None = None) -> str:
    return globals()["config_path"](config_path)


def _section(cfg: dict, key: str) -> dict:
    value = cfg.get(key)
    return value if isinstance(value, dict) else {}


def _cfg_str(cfg: dict, key: str) -> str:
    raw = _section(cfg, "paths").get(key)
    return str(raw).strip() if raw else ""


def _abs(raw: str) -> str:
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(APP_DIR, raw))


def _env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return os.path.abspath(value)
    return ""


def resolve_qbank(config_path: str | None = None, *, legacy: str | None = None) -> str:
    """题库目录（放 *.docx 的地方）。"""
    hit = _env("MATH_APP_QBDIR", "MATH_QB_ROOT")
    if hit:
        return hit
    raw = _cfg_str(read_config(config_path), "qbank")
    if raw:
        return _abs(raw)
    if legacy and os.path.isdir(legacy):
        return os.path.abspath(legacy)
    return os.path.join(APP_DIR, "qbank")


def resolve_data_dir(config_path: str | None = None) -> str:
    hit = _env("MATH_APP_DATA")
    if hit:
        return hit
    raw = _cfg_str(read_config(config_path), "data")
    return _abs(raw) if raw else os.path.join(APP_DIR, "data")


def resolve_out_dir(config_path: str | None = None) -> str:
    hit = _env("MATH_APP_OUT")
    if hit:
        return hit
    raw = _cfg_str(read_config(config_path), "out")
    return _abs(raw) if raw else os.path.join(APP_DIR, "out")


def resolve_db(config_path: str | None = None) -> str:
    return os.path.join(resolve_data_dir(config_path), "app.db")


def describe(config_path: str | None = None) -> dict:
    """给 /health 与设置页用：每项「最终路径 + 来源」，让用户看懂自己在用哪份数据。"""
    env_q = _env("MATH_APP_QBDIR", "MATH_QB_ROOT")
    cfg = read_config(config_path)
    cfg_q = _cfg_str(cfg, "qbank")
    qbank = resolve_qbank(config_path, legacy=LEGACY_QBANK)
    if env_q:
        q_source = "环境变量"
    elif cfg_q:
        q_source = "config.json"
    elif os.path.abspath(qbank) == os.path.abspath(LEGACY_QBANK):
        q_source = "老路径回退"
    else:
        q_source = "程序目录"
    return {
        "app_dir": APP_DIR,
        "config": {"path": config_path_fn(config_path),
                   "exists": os.path.isfile(config_path_fn(config_path))},
        "qbank": {"path": qbank, "source": q_source, "exists": os.path.isdir(qbank)},
        "data": {"path": resolve_data_dir(config_path),
                 "source": "环境变量" if _env("MATH_APP_DATA")
                 else ("config.json" if _cfg_str(cfg, "data") else "程序目录")},
        "out": {"path": resolve_out_dir(config_path),
                "source": "环境变量" if _env("MATH_APP_OUT")
                else ("config.json" if _cfg_str(cfg, "out") else "程序目录")},
        "db": resolve_db(config_path),
    }
