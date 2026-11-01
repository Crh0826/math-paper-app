# -*- coding: utf-8 -*-
"""从第三方 OCR 服务的文档页里抽出接口调用示例（URL / 参数 / 鉴权）。"""
import re
import ssl
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

URLS = ["http://www.zndjs.cn:9999/api", "https://www.zndjs.cn:8888/ocrtest"]

for u in URLS:
    print("=" * 25, u)
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=25, context=CTX).read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print("失败:", type(exc).__name__, str(exc)[:120])
        continue
    # 去掉 style/script 里的纯样式噪音，只留可能含接口的片段
    hits = []
    for pat in (r"https?://[^\s\"'<>]+", r"fetch\([^)]{0,160}\)", r"(?i)(?:token|apikey|api_key|auth|key)\s*[:=]\s*['\"][^'\"]{6,80}['\"]",
                r"(?i)(?:ocr|upload|recognize|api)[a-z_/]*\s*[:=]\s*['\"][^'\"]{3,80}['\"]"):
        for m in re.finditer(pat, html):
            s = m.group(0).strip()
            if s not in hits:
                hits.append(s)
    print("抽到 %d 条线索：" % len(hits))
    for h in hits[:40]:
        print("  ", h[:150])
    # 找 <pre>/<code> 块（示例代码通常在这）
    for m in re.finditer(r"<(?:pre|code)[^>]*>(.*?)</(?:pre|code)>", html, re.S):
        block = re.sub(r"<[^>]+>", "", m.group(1))
        import html as H
        block = H.unescape(block).strip()
        if len(block) > 40:
            print("  --- 代码块 ---")
            print("\n".join("    " + ln for ln in block.splitlines()[:18]))
