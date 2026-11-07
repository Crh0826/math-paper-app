# -*- coding: utf-8 -*-
"""t98 真机证据：375 / 1080 视口截图 + 题量区按钮清点 + scrollWidth。

复用 out/_cdp83.py 的 Browser（headless Chrome + 手写 CDP）。
产物：out/acceptance_t98/compose_375.png、compose_1080.png、summary.json
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "out"))
OUT = os.path.join(APP, "out", "acceptance_t98")

import _cdp83  # noqa: E402  （本地小工具，不是产品代码）

PROBE = r"""
(() => {
  const mix = document.getElementById('mix');
  const btns = [...mix.querySelectorAll('button')].map(function (b) {
    const rect = b.getBoundingClientRect();
    return { id: b.id || '', text: (b.textContent || '').trim().slice(0, 16),
             visible: rect.width > 0 && rect.height > 0 };
  });
  const de = document.scrollingElement;
  return JSON.stringify({
    refreshCounts: mix.querySelectorAll('#refresh-counts').length,
    checkAvailTotal: document.querySelectorAll('#check-avail').length,
    checkAvailInNoscript: document.querySelectorAll('noscript #check-avail').length,
    visibleButtons: btns.filter(b => b.visible),
    availText: (document.getElementById('avail-text') || {}).textContent || '',
    availNote: (document.getElementById('avail-note') || {}).textContent || '',
    scrollWidth: de.scrollWidth, clientWidth: de.clientWidth,
    innerWidth: window.innerWidth,
    bodyOverflow: de.scrollWidth - de.clientWidth
  });
})()
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.read()
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("服务没起来：%s" % url)


def main():
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="t98_")
    db = os.path.join(tmp, "data", "app.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    import build_db
    build_db.build(limit=400, db_path=db)
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        json.dump({"ocr": {"backend": "manual"}}, fh)

    port = free_port()
    srv = subprocess.Popen(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,%r);import uvicorn,server;"
         "uvicorn.run(server.create_app(db_path=%r,config_path=%r,out_dir=%r),"
         "host='127.0.0.1',port=%d,log_level='error')" % (APP, db, cfg, tmp, port)],
        cwd=APP, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    base = "http://127.0.0.1:%d" % port
    try:
        wait_http(base + "/healthz")
        br = _cdp83.Browser(port=free_port(), width=1080, height=1000)
        try:
            result = {}
            for width, tag in ((375, "375"), (1080, "1080")):
                br.emulate(width, 900, mobile=(width < 600))
                br.goto(base + "/?check=1", wait=2.2)
                data = json.loads(br.eval(PROBE))
                png = os.path.join(OUT, "compose_%s.png" % tag)
                br.screenshot(png)
                data["screenshot"] = png
                result[tag] = data
            with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            br.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
