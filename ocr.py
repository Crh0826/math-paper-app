# -*- coding: utf-8 -*-
"""ocr.py · 识别适配层（T9）

判卷只需要「填空/解答最终答案」的文字，过程不判。识别后端可插拔：

| backend    | 说明 |
|------------|------|
| `manual`   | 默认。永远返回空文本，让 UI 走人工确认；零依赖、永不崩 |
| `vlm`      | OpenAI 兼容多模态（POST {base_url}/v1/chat/completions，图片走 data URI） |
| `paddle_vl`| PaddleOCR-VL 云端 job（见 CONTRACT §4.1）：整卷非选择作答格拼一张长图，一次 job，按 block_bbox 归格 |
| `local`    | Windows 内置 OCR（winsdk 可用才生效，否则降级 manual） |

铁律：**任何失败都不许抛异常、不许猜答案**，一律降级为 manual（`text=""`，
`backend="<原后端>->manual"`），由 UI 留在「待人工确认」。

配置：`app/config.json`（真实 Token 只放这里；`config.example.json` 是脱敏模板）。
环境变量优先：`OCR_BACKEND` / `OCR_TOKEN` / `OCR_MODEL` / `OCR_JOB_URL`
（兼容旧名 `DSH_OCR_BACKEND` / `DSH_OCR_KEY` / `DSH_OCR_MODEL` / `DSH_OCR_BASE_URL`）。

对外 API（CONTRACT §4）：
    Recognizer(backend=None, config=None, config_path=None, use_env=True, **overrides)
        .read(image_path, kind) -> {"text", "confidence", "backend"}
        .read_batch(items) -> {key: {"text", "confidence", "backend"}}
    辅助：stitch_cells / clean_answer / load_config
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import uuid
from urllib.parse import urljoin


def pin_stdlib_select():
    """把标准库 `select` 钉进 sys.modules。

    Windows 上 stdlib select 是 DLLS 里的扩展模块，`app/select.py`（组卷选题）
    在 sys.path 前面时会把它遮蔽，随后 `socket/selectors/socketserver/urllib`
    全部报 `AttributeError: module 'select' has no attribute 'select'`。
    本函数临时把 app/ 移出 sys.path 再 import，拿回真身。
    """
    mod = sys.modules.get("select")
    if mod is not None and hasattr(mod, "select"):
        return mod
    here = os.path.dirname(os.path.abspath(__file__))
    saved = sys.path[:]
    try:
        sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]
        sys.modules.pop("select", None)
        import select as _real
        sys.modules["select"] = _real
        import importlib
        import selectors
        importlib.reload(selectors)      # 让 selectors 的全局名重新绑定
        return _real
    finally:
        sys.path[:] = saved


pin_stdlib_select()   # 必须在 import urllib.request 之前

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

# ---------------------------------------------------------------- 常量
BACKENDS = ("manual", "vlm", "paddle_vl", "local")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")

#: 拼图参数（判卷侧按这些常量算 y 区间；改之前先跑 tests/test_ocr.py）
STITCH_W = 900          # 统一宽度：太窄字看不清，太宽 OCR 会缩坏
STITCH_PAD = 24         # 左右留白
STITCH_GAP = 40         # 格与格之间纯白间隔（≥8px；实测 24 会被 OCR 合成一块，40 更稳）
LABEL_H = 40            # 每格上方编号条高度
LABEL_FONT_SIZE = 28

VLM_PROMPT = (
    "你是数学答题卡的识别助手。图片是答题卡上的一个作答框，里面是学生手写的最终答案。"
    "只输出最终答案本身，不要解题过程、不要多余文字、不要写“答案是”。"
    "有多个小问时按小问顺序用逗号分隔。看不清就输出空字符串。"
)
VLM_USER_TEXT = "请识别框内的最终答案。"

PADDLE_OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}

_ANSWER_PREFIX = re.compile(
    r"^\s*(?:(?:最终|正确)?答案(?:是|为)?|答(?:案)?(?:是|为)?|final\s*answer|answer)\s*[:：]?\s*",
    re.IGNORECASE,
)
_ONLY_INDEX_BOX = re.compile(r"\s*\\left\[\s*(\d{1,3})\s*\\right\]\s*")
_LEADING_INDEX_BOX = re.compile(r"^\s*(?:\\left\[\s*\d{1,3}\s*\\right\]\s*)+")
_LABEL_RE = re.compile(r"\[?\s*(\d+)\s*-\s*(\d+)\s*\]?\s*$")
_LABEL_ANY = re.compile(r"\[?\b(\d+)-(\d+)\b\]?")
#: 拼图上的编号标记：`[3-1]` / `\left[3-1\right]` / `[ 3 - 1 ]` 都算
_LABEL_MARK = re.compile(r"\[\s*(\d+)\s*-\s*(\d+)\s*(?:\\?right\s*)?\]")


# ---------------------------------------------------------------- 配置
def load_config(path: str = None) -> dict:
    """读 app/config.json；不存在/坏格式一律返回 {}（绝不抛异常）。"""
    path = path or CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


# ---------------------------------------------------------------- 文本清理
def clean_answer(text) -> str:
    """把 OCR 出来的东西洗成“最终答案”。

    处理：`$$…$$`、`\\left[13\\right]`、`\\begin{array}{cc}…`、`\\\\`、花括号、
    `[idx-blank]` 残留、`答案是：` 前缀、中文/英文引号、首尾空白与句号。
    """
    if text is None:
        return ""
    s = str(text)
    if not s.strip():
        return ""
    # 1) 去 LaTeX 环境与包装
    s = s.replace("$$", "").replace("$", "")
    s = s.replace("&", " ")          # aligned/array 的对齐符
    # \left[13\right] 单独出现 → 就是 13；跟在别的答案前面时是编号残留，丢掉
    m = _ONLY_INDEX_BOX.fullmatch(s)
    if m:
        return m.group(1)
    s = _LEADING_INDEX_BOX.sub("", s)
    s = re.sub(r"\\begin\{[^}]*\}(?:\{[^}]*\})*", " ", s)   # \begin{array}{cc}
    s = re.sub(r"\\end\{[^}]*\}", " ", s)
    s = s.replace("\\\\", " ")
    s = re.sub(r"\\left\s*[\[\(\{\.|]", " ", s)             # \left[  \left(
    s = re.sub(r"\\right\s*[\]\)\}\.\|]", " ", s)           # \right] \right)
    s = re.sub(r"\\[a-zA-Z]+\s*", " ", s)                   # 剩下的 \frac \times 等命令名
    s = s.replace("{", " ").replace("}", " ")
    # 2) 去 [idx-blank] 编号残留（出现在开头或整块只有编号）
    #    实测 OCR 会把 "11" 认成 "fi11"，所以只要方括号里有数字+连字符就当编号删掉
    s = re.sub(r"^\s*\[[^\]\n]{0,14}-\s*\d[^\]\n]{0,6}\]\s*", "", s)
    s = re.sub(r"^\s*\[?\s*\d+\s*-\s*\d+\s*\]?\s*", "", s)
    s = re.sub(r"\[\s*\d+\s*-\s*\d+\s*\]", " ", s)
    # 3) 去“答案是：”这类前缀（可能重复两层）
    for _ in range(3):
        new = _ANSWER_PREFIX.sub("", s)
        if new == s:
            break
        s = new
    # 4) 去各种引号
    for q in ('"', "'", "“", "”", "‘", "’", "「", "」", "《", "》", "`"):
        s = s.replace(q, "")
    # 5) 空白与句末标点
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("。．.；;，,")
    return s.strip()


def _clean_or_empty(text) -> str:
    try:
        return clean_answer(text)
    except Exception:
        return ""


# ---------------------------------------------------------------- 拼图
_FONT_CACHE = {}


def _label_font(size: int = LABEL_FONT_SIZE):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    from PIL import ImageFont
    cands = [
        os.environ.get("CARD_FONT_REG", ""),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\DejaVuSans.ttf",
    ]
    font = None
    for p in cands:
        if not p or not os.path.exists(p):
            continue
        try:
            font = ImageFont.truetype(p, size, index=0)
            break
        except Exception:
            try:
                font = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def stitch_cells(items, out_png: str, width: int = STITCH_W):
    """把多个作答格竖排拼成一张长图，并在每格左上角印 `[idx-blank]` 编号。

    items = [(key, crop_path), ...]，key 形如 "11-1"（idx-blank）。
    返回 (out_png, ranges)，ranges[i] = {key,x,w,y0,y1,label_y0,label_h}，
    y0..y1 是该格作答区域的 y 区间，判卷/OCR 归格按它算。
    """
    from PIL import Image, ImageDraw

    inner_w = max(50, int(width) - 2 * STITCH_PAD)
    imgs = []
    for key, path in items:
        with Image.open(path) as im:
            im = im.convert("L")
            w0, h0 = im.size
            if w0 <= 0 or h0 <= 0:
                raise ValueError("空图: %s" % path)
            # 只缩不放：放大字会让 layout 检测把整块当成图片（use_ocr_for_image_block=false → 不出字）
            if w0 > inner_w:
                scale = inner_w / float(w0)
                nh = max(1, int(round(h0 * scale)))
                im = im.resize((inner_w, nh), Image.LANCZOS)
                imgs.append((key, im, inner_w, nh))
            else:
                imgs.append((key, im, w0, h0))

    total_h = STITCH_PAD
    for _key, im, _w, _h in imgs:
        total_h += LABEL_H + im.height + STITCH_GAP
    total_h += STITCH_PAD

    canvas = Image.new("L", (int(width), max(total_h, STITCH_PAD * 2 + 1)), 255)
    draw = ImageDraw.Draw(canvas)
    font = _label_font()
    ranges = []
    y = STITCH_PAD
    for key, im, w1, h1 in imgs:
        label_y0 = y
        draw.text((STITCH_PAD, label_y0 + 4), "[%s]" % key, font=font, fill=0)
        y0 = label_y0 + LABEL_H
        canvas.paste(im, (STITCH_PAD, y0))
        y1 = y0 + h1
        ranges.append({"key": str(key), "x": STITCH_PAD, "w": w1,
                       "y0": y0, "y1": y1, "label_y0": label_y0, "label_h": LABEL_H})
        y = y1 + STITCH_GAP

    os.makedirs(os.path.dirname(os.path.abspath(out_png)) or ".", exist_ok=True)
    canvas.save(out_png, "PNG", optimize=True)
    return out_png, ranges


# ---------------------------------------------------------------- HTTP 工具
def _request(url: str, data: bytes = None, headers: dict = None,
             method: str = None, timeout: float = 30.0):
    """最小 HTTP 客户端（stdlib）。返回 (status, body)；网络层错误往上抛。"""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body


def _multipart(fields: dict, files: list):
    """files=[(name, filename, data, content_type)] → (body, content_type)。"""
    boundary = "----ocr" + uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, k, str(v))).encode("utf-8"))
    for name, filename, data, ctype in files:
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                       "Content-Type: %s\r\n\r\n" % (boundary, name, filename, ctype)).encode("utf-8"))
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


def _json(resp_body: bytes):
    try:
        return json.loads(resp_body.decode("utf-8", "replace"))
    except Exception:
        return None


def _manual(backend: str = "manual") -> dict:
    return {"text": "", "confidence": 0.0, "backend": backend}


def _degraded(backend: str) -> dict:
    return {"text": "", "confidence": 0.0, "backend": "%s->manual" % backend}


# ---------------------------------------------------------------- 主类
class Recognizer:
    """可插拔识别器。构造参数优先于 config，config 优先于环境变量。"""

    def __init__(self, backend: str = None, config: dict = None, config_path: str = None,
                 use_env: bool = True, **overrides):
        cfg = load_config(config_path) if config is None else (config or {})
        self.config = cfg if isinstance(cfg, dict) else {}
        ocr_cfg = self.config.get("ocr") or {}

        ov = {k: v for k, v in overrides.items() if v not in (None, "")}
        if backend:
            ov["backend"] = backend

        def pick(ov_key, env_names, cfg_val, default=None):
            """优先级：构造参数 > 环境变量 > config.json（CONTRACT §4.1：环境变量优先）。"""
            if ov.get(ov_key):
                return ov[ov_key]
            e = _env(*env_names) if use_env else None
            if e:
                return e
            if cfg_val not in (None, ""):
                return cfg_val
            return default

        self.backend = str(pick("backend", ("OCR_BACKEND", "DSH_OCR_BACKEND"),
                                ocr_cfg.get("backend"), "manual")).strip() or "manual"
        self.last_error = ""

        # ---- paddle_vl
        pv = (ocr_cfg.get("paddle_vl") or {})
        self.token = str(pick("token", ("OCR_TOKEN", "DSH_OCR_TOKEN"), pv.get("token"), "") or "")
        self.model = str(pick("model", ("OCR_MODEL", "DSH_OCR_MODEL"), pv.get("model"),
                              "PaddleOCR-VL-1.6"))
        self.job_url = str(pick("job_url", ("OCR_JOB_URL",), pv.get("job_url"),
                                "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"))
        self.timeout_s = float(ov.get("timeout_s") or ocr_cfg.get("timeout_s") or 120.0)
        self.poll_s = float(ov.get("poll_s") or ocr_cfg.get("poll_s") or 2.0)

        # ---- vlm（OpenAI 兼容）
        vlm = (ocr_cfg.get("vlm") or {})
        self.base_url = str(pick("base_url", ("DSH_OCR_BASE_URL", "OCR_BASE_URL"),
                                 vlm.get("base_url"), "") or "").rstrip("/")
        self.key = str(pick("key", ("DSH_OCR_KEY", "OCR_KEY"), vlm.get("key"), "") or "")
        self.vlm_model = str(pick("vlm_model", ("OCR_VLM_MODEL",), vlm.get("model"),
                                  "gpt-4o-mini"))
        self.vlm_timeout_s = float(ov.get("vlm_timeout_s") or ocr_cfg.get("vlm_timeout_s") or 30.0)
        self.vlm_retries = int(ov.get("vlm_retries") if ov.get("vlm_retries") is not None
                               else ocr_cfg.get("vlm_retries", 1))

    # ------------------------------------------------------------ read
    def read(self, image_path: str, kind: str = None) -> dict:
        """识别单个作答格 → {"text","confidence","backend"}；永不抛异常。"""
        try:
            if self.backend == "manual":
                return _manual()
            if self.backend == "vlm":
                return self._vlm_guarded(image_path, kind)
            if self.backend == "paddle_vl":
                key = "%s-1" % (kind or "x")
                out = self.read_batch([(key, image_path)])
                return out.get(key) or _degraded("paddle_vl")
            if self.backend == "local":
                return self._local_guarded(image_path)
            return _manual()
        except Exception as e:  # 兜底：任何意外都不许拖死判卷
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return _degraded(self.backend)

    # ------------------------------------------------------------ read_batch
    def read_batch(self, items) -> dict:
        """批量识别 → {key: {"text","confidence","backend"}}；整批失败降级 manual。"""
        pairs = []
        for it in (items or []):
            try:
                key, path = it[0], it[1]
            except Exception:
                continue
            pairs.append((str(key), path))
        if not pairs:
            return {}
        try:
            if self.backend == "paddle_vl":
                return self._paddle_batch(pairs)
            if self.backend == "vlm":
                return {k: self._vlm_guarded(p, "fill") for k, p in pairs}
            if self.backend == "local":
                return {k: self._local_guarded(p) for k, p in pairs}
            return {k: _manual() for k, _ in pairs}
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return {k: _degraded(self.backend) for k, _ in pairs}

    # ------------------------------------------------------------ vlm
    def _vlm_guarded(self, image_path: str, kind: str = None) -> dict:
        try:
            return self._vlm_read(image_path, kind)
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return _degraded("vlm")

    def _vlm_read(self, image_path: str, kind: str = None) -> dict:
        if not os.path.exists(image_path):
            raise FileNotFoundError(image_path)
        ext = os.path.splitext(image_path)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        payload = {
            "model": self.vlm_model,
            "temperature": 0,
            "max_tokens": 128,
            "messages": [
                {"role": "system", "content": VLM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": VLM_USER_TEXT + ("（类型：%s）" % kind if kind else "")},
                    {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                ]},
            ],
        }
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_err = None
        attempts = 1 + max(0, self.vlm_retries)
        for i in range(attempts):
            try:
                status, raw = _request(url, data=body, headers=headers, method="POST",
                                       timeout=self.vlm_timeout_s)
                if status != 200:
                    last_err = "HTTP %s" % status
                    continue
                obj = _json(raw)
                content = ""
                if obj:
                    try:
                        content = obj["choices"][0]["message"]["content"]
                    except Exception:
                        content = ""
                if not isinstance(content, str):
                    last_err = "bad content"
                    continue
                text = _clean_or_empty(content)
                if text:
                    return {"text": text, "confidence": 0.85, "backend": "vlm"}
                last_err = "empty content"
            except Exception as e:      # 超时/连接错误 → 重试
                last_err = "%s: %s" % (type(e).__name__, e)
            if i + 1 < attempts:
                time.sleep(0.05)
        self.last_error = str(last_err)
        return _degraded("vlm")

    # ------------------------------------------------------------ paddle_vl
    def _paddle_batch(self, pairs) -> dict:
        try:
            return self._paddle_batch_inner(pairs)
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return {k: _degraded("paddle_vl") for k, _ in pairs}

    def _paddle_batch_inner(self, pairs) -> dict:
        if not os.path.exists(pairs[0][1]):
            raise FileNotFoundError(pairs[0][1])
        tmp_dir = os.path.join(os.path.dirname(os.path.abspath(pairs[0][1])), "_ocr_stitch")
        os.makedirs(tmp_dir, exist_ok=True)
        stitched = os.path.join(tmp_dir, "batch_%s.png" % uuid.uuid4().hex[:8])
        try:
            png, ranges = stitch_cells(pairs, stitched)
            blocks, markdowns = None, []
            # 实测云端偶发返回空结果（jsonl 里没有 layoutParsingResults）→ 重试一次
            for _attempt in range(2):
                blocks, markdowns = self._paddle_job(png)
                if blocks or markdowns:
                    break
        finally:
            try:
                os.remove(stitched)
            except Exception:
                pass
        if blocks is not None:
            texts = _assign_by_bbox(blocks, ranges)
            texts = _fill_by_order(blocks, ranges, texts)
        elif markdowns:
            texts = _split_by_label("\n".join(markdowns), [r["key"] for r in ranges])
        else:
            raise RuntimeError("OCR 结果里没有可用的块")
        out = {}
        for key, _path in pairs:
            text = texts.get(key, "")
            out[key] = ({"text": text, "confidence": 0.85, "backend": "paddle_vl"}
                        if text else {"text": "", "confidence": 0.0, "backend": "paddle_vl"})
        return out

    def _paddle_job(self, image_path: str):
        """提交 job、轮询、取 jsonl → (blocks, markdowns)；失败返回 (None, [])。"""
        with open(image_path, "rb") as f:
            img = f.read()
        body, ctype = _multipart(
            {"model": self.model,
             "optionalPayload": json.dumps(PADDLE_OPTIONAL_PAYLOAD, ensure_ascii=False)},
            [("file", os.path.basename(image_path), img, "image/png")],
        )
        headers = {"Content-Type": ctype,
                   "Authorization": "bearer %s" % self.token}
        deadline = time.time() + self.timeout_s
        status, raw = _request(self.job_url, data=body, headers=headers, method="POST",
                               timeout=min(30.0, max(5.0, self.timeout_s)))
        if status != 200:
            self.last_error = "job POST HTTP %s" % status
            return None, []
        obj = _json(raw) or {}
        job_id = ((obj.get("data") or {}).get("jobId")
                  or (obj.get("data") or {}).get("job_id"))
        if not job_id:
            self.last_error = "job POST 无 jobId"
            return None, []

        poll_url = self.job_url.rstrip("/") + "/" + str(job_id)
        json_url = None
        while time.time() < deadline:
            st, raw = _request(poll_url, headers={"Authorization": "bearer %s" % self.token},
                               timeout=min(30.0, max(5.0, deadline - time.time() + 1)))
            if st != 200:
                self.last_error = "poll HTTP %s" % st
                return None, []
            data = (_json(raw) or {}).get("data") or {}
            state = str(data.get("state") or "").lower()
            if state == "done":
                result_url = data.get("resultUrl") or data.get("result_url") or {}
                json_url = (result_url.get("jsonUrl") or result_url.get("json_url")
                            or data.get("jsonUrl"))
                break
            if state == "failed":
                self.last_error = "job state=failed"
                return None, []
            time.sleep(max(0.02, self.poll_s))
        if not json_url:
            self.last_error = "job 轮询超时"
            return None, []

        url = urljoin(self.job_url, json_url)
        st, raw = _request(url, timeout=min(30.0, max(5.0, self.timeout_s)))
        if st != 200:
            self.last_error = "jsonUrl HTTP %s" % st
            return None, []
        return _parse_jsonl(raw)

    # ------------------------------------------------------------ local (Windows OCR)
    def _local_guarded(self, image_path: str) -> dict:
        try:
            if not os.path.exists(image_path):
                raise FileNotFoundError(image_path)
            text = _win_ocr(image_path)
            text = _clean_or_empty(text)
            if not text:
                return {"text": "", "confidence": 0.0, "backend": "local"}
            return {"text": text, "confidence": 0.6, "backend": "local"}
        except Exception as e:
            self.last_error = "%s: %s" % (type(e).__name__, e)
            return _degraded("local")


# ---------------------------------------------------------------- 结果归格
def _parse_jsonl(raw: bytes):
    """PaddleOCR-VL jsonl → (blocks, markdowns)。"""
    blocks, markdowns = [], []
    try:
        lines = raw.decode("utf-8", "replace").splitlines()
    except Exception:
        return None, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        obj = _json(line.encode("utf-8"))
        if not isinstance(obj, dict):
            continue
        res = obj.get("result") or obj.get("data") or {}
        for lpr in (res.get("layoutParsingResults") or []):
            pr = lpr.get("prunedResult") or {}
            for b in (pr.get("parsing_res_list") or []):
                bbox = b.get("block_bbox") or b.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                try:
                    box = [float(v) for v in bbox[:4]]
                except Exception:
                    continue
                blocks.append({"bbox": box, "content": b.get("block_content") or ""})
            md = ((lpr.get("markdown") or {}).get("text") or "")
            if md:
                markdowns.append(md)
    return (blocks if blocks else None), markdowns


def _cell_of(cy: float, ranges) -> str:
    """按块中心 y 找最近的作答格（比“区间包含”稳：整块被拉长也不会跨错格）。"""
    best, best_d = None, None
    for r in ranges:
        center = (r["y0"] + r["y1"]) / 2.0
        d = abs(cy - center)
        if best_d is None or d < best_d:
            best, best_d = r, d
    return best["key"] if best else None


def _inside_label_strip(bbox, ranges) -> bool:
    """块是否整块落在某格的编号条里（那就是编号块，不是答案）。"""
    x1, y1, x2, y2 = bbox
    for r in ranges:
        top, bottom = r["label_y0"], r["label_y0"] + r["label_h"]
        if y1 >= top - 2 and y2 <= bottom + 2:
            return True
    return False


def _split_segments(content: str):
    """按编号标记把一块文本切成有序段 [(key|None, text)]。

    `\\left[5-1\\right]`、`[3-1]`、`[ 3 - 1 ]` 都算标记；标记前的残留丢掉，
    段文本经 clean_answer 清理（公式环境里的 & / \\quad / \\\\ 一并去掉）。
    """
    if not content:
        return []
    marks = list(_LABEL_MARK.finditer(content))
    segs = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        text = _clean_or_empty(content[m.end():end])
        if text:
            segs.append(("%s-%s" % (m.group(1), m.group(2)), text))
    return segs


def _fill_by_order(blocks, ranges, texts) -> dict:
    """最坏情况兜底：整张拼图被识别成一个公式块时，按“段序 == 格序”还原。

    只在「段数恰好等于格数」时启用（多个格都识别出东西），否则宁可留空给人工确认。
    """
    segs = []
    for b in sorted(blocks or [], key=lambda x: (x["bbox"][1], x["bbox"][0])):
        segs.extend(_split_segments(b.get("content")))
    if not segs or len(segs) != len(ranges):
        return texts
    for r, (_key, text) in zip(ranges, segs):
        if not texts.get(r["key"]) and text:
            texts[r["key"]] = text
    return texts


def _split_block_by_label(content: str, valid_keys) -> list:
    """把一块文本按我们印的 `[idx-blank]` 编号切成 [(key, text)]。

    实测：相邻作答格会被 OCR 合成一块（间隔 64px 也一样），
    这时只能靠我们自己印的编号归属，比 bbox 中心稳得多。
    """
    if not content:
        return []
    valid = set(valid_keys)
    marks = list(_LABEL_MARK.finditer(content))
    out = []
    for i, m in enumerate(marks):
        key = "%s-%s" % (m.group(1), m.group(2))
        if key not in valid:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        text = _clean_or_empty(content[m.end():end])
        if text:
            out.append((key, text))
    return out


def _assign_by_bbox(blocks, ranges) -> dict:
    """归格：能按 `[idx-blank]` 拆就按编号归（最稳）；否则退回 bbox 中心就近归。

    编号条里的块（纯编号）直接丢弃；同格多块取清理后最长的一条。
    """
    valid = {r["key"] for r in ranges}
    bucket = {r["key"]: [] for r in ranges}
    for b in blocks or []:
        segs = _split_block_by_label(b.get("content"), valid)
        if segs:
            for key, text in segs:
                bucket[key].append(text)
            continue
        if _inside_label_strip(b["bbox"], ranges):
            continue
        x1, y1, x2, y2 = b["bbox"]
        key = _cell_of((y1 + y2) / 2.0, ranges)
        if key is None:
            continue
        text = _clean_or_empty(b.get("content"))
        if text:
            bucket[key].append(text)
    return {k: (max(v, key=len) if v else "") for k, v in bucket.items()}


def _split_by_label(text: str, keys) -> dict:
    """没有 block_bbox 时的兜底：按我们自己印的 [idx-blank] 编号切 markdown。"""
    out = {}
    marks = list(re.finditer(r"\[(\d+-\d+)\]", text or ""))
    for i, m in enumerate(marks):
        key = m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.setdefault(key, []).append(text[m.end():end])
    return {k: _clean_or_empty(" ".join(out[k])) for k in keys if k in out}


def _win_ocr(image_path: str) -> str:
    """Windows 内置 OCR（winsdk 可用时）；不可用就抛异常让上层降级。"""
    import asyncio

    from winsdk.windows.globalization import Language  # noqa: F401
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    async def run():
        f = await StorageFile.get_file_from_path_async(os.path.abspath(image_path))
        stream = await f.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_language(Language("zh-Hans"))
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("系统没有可用 OCR 语言包")
        result = await engine.recognize_async(bitmap)
        return result.text or ""

    return asyncio.run(run())


if __name__ == "__main__":
    import sys
    rec = Recognizer()
    print(json.dumps({"backend": rec.backend, "model": rec.vlm_model,
                      "paddle_model": rec.model,
                      "has_token": bool(rec.token),
                      "config_exists": os.path.exists(CONFIG_PATH)},
                     ensure_ascii=False, indent=1))
    if len(sys.argv) > 1:
        print(json.dumps(rec.read(sys.argv[1], "fill"), ensure_ascii=False))
