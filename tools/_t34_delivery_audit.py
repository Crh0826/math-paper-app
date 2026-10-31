# -*- coding: utf-8 -*-
"""t34 · 交付清单核对（v2：证据路径支持通配符 + 收集「多余」清单）。"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import sys
from importlib import metadata

APP = r"E:\OCR\app"
OUT = os.path.join(APP, "out")
EVID = os.path.join(OUT, "acceptance_v1")
sys.path.insert(0, APP)

RESULT = {"tree": {}, "missing": [], "extra": [], "doc_mismatch": [],
          "deps": [], "out_dirs": [], "runtime": {}}

MODULES_IN_CONTRACT = ["docx_rich", "compose", "taxonomy", "families", "db", "build_db",
                       "picker", "paper", "render", "card", "grade", "ocr", "normalize",
                       "train", "quality", "dedupe", "errors", "server", "grade_routes", "demo"]


def tree():
    py = sorted(f for f in os.listdir(APP) if f.endswith(".py") and not f.startswith("_"))
    docs = sorted(f for f in os.listdir(APP) if f.endswith(".md"))
    misc = sorted(f for f in os.listdir(APP)
                  if f in ("requirements.txt", "start.bat", "config.example.json",
                           "config.json", ".gitignore"))
    tpl = sorted(os.path.basename(p) for p in glob.glob(os.path.join(APP, "templates", "*.html")))
    st = sorted(os.path.basename(p) for p in glob.glob(os.path.join(APP, "static", "*")))
    data = sorted(os.path.basename(p) for p in glob.glob(os.path.join(APP, "data", "*")))
    tests = sorted(os.path.basename(p) for p in glob.glob(os.path.join(APP, "tests", "test_*.py")))
    tools = sorted(os.path.basename(p) for p in glob.glob(os.path.join(APP, "tools", "*.py")))
    RESULT["tree"] = {"modules": py, "docs": docs, "misc": misc, "templates": tpl,
                      "static": st, "data": data, "tests": tests, "tools": tools}
    return RESULT["tree"]


def ref_exists(rel):
    """app/out/... 或带通配符的引用 → 是否存在。"""
    p = os.path.join(r"E:\OCR", rel.replace("/", "\\"))
    if "*" in p or "?" in p:
        return bool(glob.glob(p))
    if os.path.isdir(p) or os.path.isfile(p):
        return True
    # `.../sub_` `photo_` 这类被正则截断的前缀：当成 glob 再试
    if p.endswith(("_", "-", "/", "\\")):
        return bool(glob.glob(p + "*"))
    return bool(glob.glob(p + "*")) and "*" in p


def docs_check(t):
    for m in MODULES_IN_CONTRACT:
        if not os.path.isfile(os.path.join(APP, m + ".py")):
            RESULT["missing"].append({"item": "CONTRACT 提到的模块 %s.py" % m, "path": m + ".py"})

    readme = open(os.path.join(APP, "README.md"), encoding="utf-8").read()
    for name in ("start.bat", "requirements.txt", "config.example.json", "config.json",
                 "server.py", "picker.py", "paper.py", "card.py", "grade.py", "ocr.py",
                 "normalize.py", "train.py", "build_db.py", "README.md", "templates",
                 "static", "tests", "out", "data"):
        if name in readme and not os.path.exists(os.path.join(APP, name)):
            RESULT["missing"].append({"item": "README 提到的 %s" % name, "path": name})

    htmls = set()
    for mod in ("server.py", "grade_routes.py", "demo.py"):
        p = os.path.join(APP, mod)
        if os.path.isfile(p):
            htmls |= set(re.findall(r'render\(\s*["\']([\w\-]+\.html)["\']',
                                    open(p, encoding="utf-8").read()))
    for h in sorted(htmls):
        if h not in t["templates"]:
            RESULT["missing"].append({"item": "代码 render 的模板 %s" % h,
                                      "path": "templates/" + h})
    RESULT["runtime"]["templates_referenced"] = sorted(htmls)

    static_refs = set()
    for p in glob.glob(os.path.join(APP, "templates", "*.html")):
        static_refs |= set(re.findall(r"/static/([\w\-./]+)", open(p, encoding="utf-8").read()))
    for sref in sorted(static_refs):
        if not os.path.exists(os.path.join(APP, "static", sref)):
            RESULT["missing"].append({"item": "模板引用的静态文件 %s" % sref,
                                      "path": "static/" + sref})
    RESULT["runtime"]["static_referenced"] = sorted(static_refs)

    bat = open(os.path.join(APP, "start.bat"), encoding="utf-8").read()
    if "requirements.txt" in bat and not os.path.exists(os.path.join(APP, "requirements.txt")):
        RESULT["missing"].append({"item": "start.bat 引用 requirements.txt", "path": "requirements.txt"})
    if "server:app" in bat and not os.path.isfile(os.path.join(APP, "server.py")):
        RESULT["missing"].append({"item": "start.bat 引用 server:app", "path": "server.py"})
    if "server.probe_word" in bat:
        src = open(os.path.join(APP, "server.py"), encoding="utf-8").read()
        if "def probe_word" not in src:
            RESULT["missing"].append({"item": "start.bat 调用 server.probe_word()，函数不存在",
                                      "path": "server.py"})

    port_bat = re.search(r"--port\s+(\d+)", bat)
    cfg = json.load(open(os.path.join(APP, "config.json"), encoding="utf-8"))
    cfg_ex = json.load(open(os.path.join(APP, "config.example.json"), encoding="utf-8"))
    rd = open(os.path.join(APP, "README.md"), encoding="utf-8").read()
    m_rd = re.search(r"--port\s+(\d+)", rd)
    ports = {"start.bat": port_bat.group(1) if port_bat else None,
             "config.json": str((cfg.get("server") or {}).get("port")),
             "config.example.json": str((cfg_ex.get("server") or {}).get("port")),
             "README": m_rd.group(1) if m_rd else None}
    RESULT["runtime"]["ports"] = ports
    if len({v for v in ports.values() if v}) > 1:
        RESULT["doc_mismatch"].append({"item": "端口不一致", "detail": ports})

    tok = ((cfg_ex.get("ocr") or {}).get("paddle_vl") or {}).get("token") or ""
    real_tok = ((cfg.get("ocr") or {}).get("paddle_vl") or {}).get("token") or ""
    RESULT["runtime"]["example_token_empty"] = not tok
    RESULT["runtime"]["real_token_configured"] = bool(real_tok)
    if tok:
        RESULT["doc_mismatch"].append({"item": "config.example.json 带真 token（应留空）"})

    verify = open(os.path.join(APP, "VERIFY.md"), encoding="utf-8").read()
    refs = set(re.findall(r"`(app/out/[^`\s（）]+)`", verify))
    refs |= set(re.findall(r"app[/\\]out[/\\][\w./\\\-*?]+", verify))
    checked, missing_ev = 0, []
    for r in sorted(refs):
        if "…" in r or "|" in r:
            continue
        if ("*" in r or "?" in r) and "acceptance_v1" not in r:
            continue          # 目录树里 `out\_*.py` 这类是"不该交付"的写法，不是证据引用
        checked += 1
        if not ref_exists(r):
            missing_ev.append(r)
    RESULT["runtime"]["verify_evidence_refs"] = {"checked": checked, "missing": missing_ev}
    for m in missing_ev:
        RESULT["missing"].append({"item": "VERIFY.md 引用的证据不存在：%s" % m, "path": m})

    if "进行中" in rd:
        RESULT["doc_mismatch"].append({
            "item": "README「当前完成度」仍写「进行中：卷面/答题卡预览与下载、上传判卷页、训练卷页」",
            "detail": "这些已交付（server.py + grade_routes.py + train.py 齐备，§12 已跑通），文档过期"})

    # 多余：不该进交付的东西
    extras = []
    for p in glob.glob(os.path.join(APP, "__pycache__")) + \
             glob.glob(os.path.join(APP, "tests", "__pycache__")) + \
             glob.glob(os.path.join(APP, "*.pyc")):
        extras.append((os.path.relpath(p, APP), "pyc 缓存，不该进交付"))
    for p in glob.glob(os.path.join(APP, "_*.py")) + glob.glob(os.path.join(APP, "out", "_*.py")):
        extras.append((os.path.relpath(p, APP), "一次性探针脚本（应放 app/tools/ 或不进交付）"))
    for p in glob.glob(os.path.join(APP, "out", "*.log")) + glob.glob(os.path.join(APP, "data", "*.log")):
        extras.append((os.path.relpath(p, APP), "运行日志，不该进交付"))
    for p in glob.glob(os.path.join(APP, "out", "card.pdf")) + \
             glob.glob(os.path.join(APP, "out", "card_*.png")):
        extras.append((os.path.relpath(p, APP), "card.py 自检 CLI 的输出（不是用户卷子）"))
    for p in glob.glob(os.path.join(APP, "data", "render")):
        extras.append((os.path.relpath(p, APP), "渲染临时目录"))
    for p in glob.glob(os.path.join(APP, "data", "*_report.json")) + \
             glob.glob(os.path.join(APP, "data", "encoding_*.json")):
        extras.append((os.path.relpath(p, APP), "构建/体检报告（可重建，交付可选）"))
    for p in glob.glob(os.path.join(APP, "data", "app.db-wal")) + \
             glob.glob(os.path.join(APP, "data", "app.db-shm")):
        extras.append((os.path.relpath(p, APP), "SQLite WAL 残留（关库后应清）"))
    RESULT["extra"] = [{"path": a, "why": b} for a, b in sorted(set(extras))]


DIST_IMPORT = {
    "opencv-python": "cv2", "pillow": "PIL", "pywin32": "win32api",
    "python-docx": "docx", "python-multipart": "multipart", "pillow-heif": "pillow_heif",
    "pymupdf": "pymupdf", "jinja2": "jinja2", "uvicorn": "uvicorn", "fastapi": "fastapi",
    "lxml": "lxml", "openpyxl": "openpyxl", "numpy": "numpy", "reportlab": "reportlab",
    "qrcode": "qrcode", "requests": "requests",
}


def ver_tuple(v):
    return tuple(int(re.match(r"\d+", p).group()) if re.match(r"\d+", p) else 0
                 for p in re.split(r"[.\-+]", str(v)))


def cmp_ver(a, b):
    a, b = ver_tuple(a), ver_tuple(b)
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def deps_check():
    txt = open(os.path.join(APP, "requirements.txt"), encoding="utf-8").read()
    for line in txt.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)", line)
        if not m:
            continue
        name, spec = m.group(1), m.group(2).replace(" ", "")
        mod = DIST_IMPORT.get(name.lower(), name.lower().replace("-", "_"))
        importable = importlib.util.find_spec(mod) is not None
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        lo, hi = (re.search(r">=\s*([\w.\-]+)", spec), re.search(r"<\s*([\w.\-]+)", spec))
        ok = bool(installed)
        if installed and lo:
            ok = ok and cmp_ver(installed, lo.group(1)) >= 0
        if installed and hi:
            ok = ok and cmp_ver(installed, hi.group(1)) < 0
        RESULT["deps"].append({"package": name, "spec": spec or "(无)", "installed": installed,
                              "importable": importable, "range_ok": ok})
        if not importable:
            RESULT["missing"].append({"item": "依赖 %s 不可导入（import %s）" % (name, mod),
                                      "path": "requirements.txt"})
        elif not ok:
            RESULT["doc_mismatch"].append({"item": "依赖版本越界：%s 声明 %s，实测 %s"
                                                   % (name, spec, installed)})


KEEP = {"paper24v2", "captain_00568", "quality_baseline", "handwrite_check", "acceptance_v1"}


def size_mb(p):
    if os.path.isfile(p):
        return os.path.getsize(p) / 1e6
    return sum(os.path.getsize(os.path.join(r, f))
               for r, _d, fs in os.walk(p) for f in fs) / 1e6


def out_judge():
    for name in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, name)
        if name == "acceptance_v1":
            RESULT["out_dirs"].append({"name": name, "kind": "dir",
                                       "mb": round(size_mb(p), 1), "keep": True,
                                       "why": "验收证据（JSON/PDF/PNG）"})
            continue
        why = {"paper24v2": "captain 指定保留（交付配图）",
               "captain_00568": "P0 回归证据",
               "quality_baseline": "等 t31 报表，暂留",
               "handwrite_check": "captain 的手写验证卷（给用户打印）"}.get(name, "")
        RESULT["out_dirs"].append({"name": name,
                                   "kind": "dir" if os.path.isdir(p) else "file",
                                   "mb": round(size_mb(p), 1), "keep": name in KEEP, "why": why})


def runtime_check():
    bat = open(os.path.join(APP, "start.bat"), encoding="utf-8").read()
    RESULT["runtime"].update({
        "start_steps": ["找 Python（python / py -3）",
                        "查依赖（fastapi/uvicorn/jinja2/multipart，缺了问是否 pip install）",
                        "探 Word 路径（注册表兜底 server.probe_word）",
                        "起服务 python -m uvicorn server:app --host 127.0.0.1 --port 8710",
                        "3 秒后自动开浏览器（MATH_NO_BROWSER=1 可关）"],
        "bind_localhost_only": "127.0.0.1" in bat,
        "port": (re.search(r"--port\s+(\d+)", bat) or [None, None])[1],
        "degrade_word_missing": "没检测到 Microsoft Office Word" in bat,
        "degrade_ocr_manual": '"manual"' in open(os.path.join(APP, "config.example.json"),
                                                 encoding="utf-8").read(),
        "corpus_readonly_in_docs": "只读" in open(os.path.join(APP, "README.md"),
                                                  encoding="utf-8").read(),
    })


def main():
    t = tree()
    docs_check(t)
    deps_check()
    out_judge()
    runtime_check()
    with open(os.path.join(EVID, "delivery_check.json"), "w", encoding="utf-8") as f:
        json.dump(RESULT, f, ensure_ascii=False, indent=2)
    print("modules=%d templates=%d static=%s tests=%d tools=%d"
          % (len(t["modules"]), len(t["templates"]), t["static"], len(t["tests"]),
             len(t["tools"])))
    print("\n缺失（%d）:" % len(RESULT["missing"]))
    for m in RESULT["missing"]:
        print("  -", m["item"])
    print("\n多余（%d）:" % len(RESULT["extra"]))
    for m in RESULT["extra"]:
        print("  - %-30s %s" % (m["path"], m["why"]))
    print("\n文档不一致（%d）:" % len(RESULT["doc_mismatch"]))
    for m in RESULT["doc_mismatch"]:
        print("  -", m["item"])
    print("\n依赖 %d 个；全部可导入=%s；全部落在区间=%s"
          % (len(RESULT["deps"]), all(d["importable"] for d in RESULT["deps"]),
             all(d["range_ok"] for d in RESULT["deps"])))
    print("\nruntime:", json.dumps(RESULT["runtime"], ensure_ascii=False)[:600])
    print("\n证据:", os.path.join(EVID, "delivery_check.json"))


if __name__ == "__main__":
    main()
