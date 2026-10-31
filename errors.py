# -*- coding: utf-8 -*-
"""错误文案字典：把真实错误码翻成「发生了什么 / 为什么 / 怎么办 / 按钮文案」。

唯一入口：``explain(code, detail=None) -> {"title", "what", "why", "fix", "retry_label"}``

设计约定（写给接线的人）：
* 本文件**纯数据**，不 import grade / picker / paper / render / ocr，避免循环依赖与启动变慢；
* 文案里的 ``{xxx}`` 用 detail 里的同名键填，缺了显示 ``?``，**绝不抛异常**——错误提示本身再崩就太糟了；
* ``fix`` 必须是用户能立刻做的动作（改数量、重拍、手动填卷号…），不写「请联系管理员」；
* ``code`` 的取值来自真实抛错处，别在这里发明新错误：
    - grade.py   : ``warp_failed`` / ``pid_mismatch`` / ``paper_not_found`` / ``no_card``
                   / ``submission_not_found`` / ``internal_error``（``qr_ok=false`` 时结果页用 ``qr_failed``）
    - picker.py  : ``InsufficientQuestions``（``details={题型:{need,available,short}}``）/ ``ScopeError``
    - paper.py   : ``PaperError``；Word 不可用走 ``WordUnavailable``（render.py 落地后同名信号）
    - ocr.py     : 从不抛异常，降级 manual；整批空结果由结果页报 ``empty_batch``，
                   超时/429 分别报 ``ocr_timeout`` / ``ocr_quota``
    - server.py  : ``db_missing`` / ``db_locked``

t11 结果页用法示例::

    from errors import explain
    e = explain(result["error"], {"pid": pid, "message": result.get("detail")})
    # 模板里：{{ e.title }} / {{ e.what }} / {{ e.why }} / {{ e.fix }} / {{ e.retry_label }}
"""
from __future__ import annotations

import re

#: 结果页/指南页会渲染的全部错误码（顺序即展示顺序）
CODES = (
    "warp_failed",
    "qr_failed",
    "pid_mismatch",
    "InsufficientQuestions",
    "ScopeError",
    "WordUnavailable",
    "empty_batch",
    "ocr_timeout",
    "ocr_quota",
    "db_missing",
    "db_locked",
    "paper_not_found",
    "no_card",
    "submission_not_found",
    "internal_error",
)

#: 别名：同一个错误在不同层可能写作 camel / snake，统一收敛到 CODES 里的键
_ALIASES = {
    "insufficient_questions": "InsufficientQuestions",
    "insufficient": "InsufficientQuestions",
    "select_insufficient": "InsufficientQuestions",
    "scope_error": "ScopeError",
    "word_unavailable": "WordUnavailable",
    "no_word": "WordUnavailable",
    "wordmissing": "WordUnavailable",
    "qr_bad": "qr_failed",
    "qr_decode_failed": "qr_failed",
    "db_is_locked": "db_locked",
    "database_locked": "db_locked",
    "ocr_empty": "empty_batch",
    "empty_ocr": "empty_batch",
    "timeout": "ocr_timeout",
    "quota": "ocr_quota",
    "429": "ocr_quota",
}


class _Safe(dict):
    """缺字段时给 ``?``，让模板永远能渲染出来。"""

    def __missing__(self, key):  # noqa: D105
        return "?"


def _opt(detail: dict, key: str):
    """取一个可选字段；空串/None 当作没给（避免指南页出现“只找到 ? 个”）。"""
    value = detail.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _warp_what(detail: dict) -> str:
    found = _opt(detail, "found")
    if found:
        return "照片里只找到 %s 个黑色定位块（需要 4 个），没法把照片校正成标准答题卡。" % found
    return "照片里没凑齐四个黑色定位块（需要 4 个），没法把照片校正成标准答题卡。"


def _pid_what(detail: dict) -> str:
    pid, qr_pid = _opt(detail, "pid"), _opt(detail, "qr_pid")
    if pid and qr_pid:
        return "照片上的卷号是 %s，但这次上传选的是 %s。" % (qr_pid, pid)
    if qr_pid:
        return "照片上的卷号是 %s，和这次上传选的卷号不是同一份。" % qr_pid
    return "照片上的卷号和这次上传选的卷号不是同一份（拍错卷子了）。"


def _db_missing_what(detail: dict) -> str:
    path = _opt(detail, "path")
    if path:
        return "程序在 %s 找不到题库文件 app.db。" % path
    return "程序找不到题库文件 app.db（默认在 app\\data\\app.db）。"


def _db_locked_what(detail: dict) -> str:
    error = _opt(detail, "error")
    if error:
        return "打开 app.db 时被拒绝：%s（文件正被别的程序锁着）。" % error
    return "打开 app.db 时被拒绝：文件正被别的程序锁着。"


def _timeout_what(detail: dict) -> str:
    timeout_s = _opt(detail, "timeout_s")
    if timeout_s:
        return "这张长图提交给识别服务后，等了 %s 秒还没拿到结果。" % timeout_s
    return "这张长图提交给识别服务后，等太久还没拿到结果。"


def _quota_what(detail: dict) -> str:
    status = _opt(detail, "status")
    if status:
        return "识别服务返回了限流或额度不足（%s），这一批没能识别。" % status
    return "识别服务返回了限流或额度不足，这一批没能识别。"


def _no_card_what(detail: dict) -> str:
    pid = _opt(detail, "pid")
    if pid:
        return "卷号 %s 在库里没有答题卡几何（card 字段），没法定位选项框。" % pid
    return "这份卷子在库里没有答题卡几何（card 字段），没法定位选项框。"


def _paper_what(detail: dict) -> str:
    pid = _opt(detail, "pid")
    if pid:
        return "数据库 papers 表里没有卷号 %s。" % pid
    return "数据库 papers 表里找不到这份卷子。"


def _submission_what(detail: dict) -> str:
    sid = _opt(detail, "sid")
    if sid:
        return "数据库里没有判卷编号 %s。" % sid
    return "数据库里找不到这次判卷记录。"


def _internal_what(detail: dict) -> str:
    message = _opt(detail, "message")
    if message:
        return "这一步抛了没预料到的异常：%s" % message
    return "这一步抛了没预料到的异常，程序没拿到更多线索。"


def _fallback_what(detail: dict) -> str:
    message = _opt(detail, "message")
    if message:
        return "这一步没成功：%s" % message
    return "这一步没成功，程序没拿到更多信息。"


def _parse_shortfall_message(message: str) -> list:
    """从 picker 的原话里抠出逐题型差额。

    picker.InsufficientQuestions 的 message 形如：
        「题量不足：选择 需要 99，可用 10，差 89；填空 需要 8，可用 3，差 5」
    调用方只传了 message（没传 details）时，也要能把「要多少/可用多少/差多少」讲清楚。
    """
    parts = []
    pattern = r"([^\s；;，,：:]+)\s*需要\s*(\d+)\s*[，,]\s*可用\s*(\d+)\s*[，,]\s*差\s*(\d+)"
    for m in re.finditer(pattern, str(message or "")):
        parts.append("%s：要 %s 题，符合条件只有 %s 题，差 %s 题"
                     % (m.group(1), m.group(2), m.group(3), m.group(4)))
    return parts


def _insufficient_what(detail: dict) -> str:
    """题量不足：按题型列出「要多少 / 有多少 / 差多少」。"""
    details = detail.get("details") or detail.get("shortfalls") or detail.get("mix") or {}
    if isinstance(details, dict) and details:
        parts = []
        for qtype, info in details.items():
            if isinstance(info, dict):
                parts.append("%s：要 %s 题，符合条件只有 %s 题，差 %s 题"
                             % (qtype, info.get("need", "?"),
                                info.get("available", "?"), info.get("short", "?")))
            else:
                parts.append("%s：%s" % (qtype, info))
        return "题量不够，没凑够数 —— " + "；".join(parts) + "。"
    if isinstance(details, str) and details.strip():
        return "题量不够，没凑够数 —— " + details.strip()
    parsed = _parse_shortfall_message(detail.get("message") or "")
    if parsed:
        return "题量不够，没凑够数 —— " + "；".join(parsed) + "。"
    return "题量不够：按当前条件能抽的题，比你填的数量少。"


def _insufficient_fix(detail: dict) -> str:
    details = detail.get("details") or {}
    short_total = 0
    if isinstance(details, dict):
        for info in details.values():
            if isinstance(info, dict):
                try:
                    short_total += int(info.get("short") or 0)
                except (TypeError, ValueError):
                    pass
    first = ("① 按上面的差额把数量改小（总共少 %d 题）" % short_total) if short_total else "① 按上面的差额把数量改小"
    return (first + "\n"
            "② 放宽范围：少勾几个知识点，或干脆不勾章节（全库可抽）\n"
            "③ 只选有答案题会砍掉一部分候选，可以按需关掉它再试\n"
            "④ 换个 seed 再抽一次，同一范围抽到的题不一样")


def _scope_what(detail: dict) -> str:
    unknown = detail.get("unknown") or detail.get("value") or ""
    available = detail.get("available") or detail.get("choices") or ()
    if isinstance(available, str):
        available = [available]
    head = "选的章节/知识点在题库里找不到"
    if unknown:
        head += "：%s" % unknown
    if available:
        shown = [str(x) for x in list(available)[:8]]
        head += "。库里现有：" + "、".join(shown)
        if len(available) > len(shown):
            head += " 等 %d 项" % len(available)
    return head + "。"


_ENTRIES = {
    # ---------------------------------------------------------------- 判卷侧
    "warp_failed": {
        "title": "答题卡四角没认出来",
        "what": _warp_what,
        "why": "四个角上的黑块是定位用的：少一个就算不出拍摄角度和缩放，后面每个选项框的位置都会偏。",
        "fix": "① 重拍：把答题卡四个角的黑色方块全部拍进画面，四边各留一点白边\n"
               "② 别折叠、别压皱答题卡，压平再拍\n"
               "③ 手、笔、橡皮别挡住四角黑块\n"
               "④ 手机正对答题卡，倾斜别超过 30°",
        "retry_label": "重新拍照",
    },
    "qr_failed": {
        "title": "卷号二维码没读出来",
        "what": "答题卡上的卷号二维码认不出来，程序不知道这是哪一份卷子。",
        "why": "二维码太小、被裁掉、照片虚焦或反光，都会解码失败；判卷需要先知道卷号才能取到题号与答案。",
        "fix": "① 先手动选卷号继续判（判分结果一样准）\n"
               "② 重拍：让二维码完整入画、对焦清楚、避开反光\n"
               "③ 打印时别裁页边，二维码离纸张边缘很近",
        "retry_label": "手动填卷号",
    },
    "pid_mismatch": {
        "title": "拍错卷子了（卷号对不上）",
        "what": _pid_what,
        "why": "不同卷子的题号、选项框位置完全不同，硬判会大面积判错，所以程序宁可拒绝。",
        "fix": "① 看清这份答题卡属于哪份卷子，回到判卷页重新选卷号上传\n"
               "② 如果刚才是选错了卷号，换成本次卷子的卷号再来一次",
        "retry_label": "重新选卷上传",
    },
    "paper_not_found": {
        "title": "找不到这份卷子",
        "what": _paper_what,
        "why": "卷子记录是组卷时写进 papers 表的；换库、删过 data 目录、或手改了卷号都会查不到。",
        "fix": "① 回组卷页重新生成一份卷子并打印\n"
               "② 判卷时用新卷子顶部的卷号（别用旧纸上的）\n"
               "③ 看过自检页确认数据库路径没被改过",
        "retry_label": "重新选卷",
    },
    "no_card": {
        "title": "这份卷子还没生成答题卡",
        "what": _no_card_what,
        "why": "答题卡是组卷流程的最后一步；那次生成如果中途失败，卷子有了、卡可能没写进去。",
        "fix": "① 重新生成这份卷子（会连答题卡一起出）\n"
               "② 打印时确认后半部分有答题卡页面（共 N 页 = 卷面 a 页 + 卡 b 页）",
        "retry_label": "重新生成卷子",
    },
    "submission_not_found": {
        "title": "找不到这次判卷记录",
        "what": _submission_what,
        "why": "记录可能被删过，或者这是另一个库里的编号（换过 app.db 就会这样）。",
        "fix": "① 回判卷页重新上传答题卡照片\n"
               "② 确认打开的是同一份数据库（自检页能看到路径）",
        "retry_label": "重新上传",
    },
    # ---------------------------------------------------------------- 识别侧
    "empty_batch": {
        "title": "这一批没识别出文字",
        "what": "识别服务暂时无响应：这一批作答格一张都没返回文字，结果全留在「待人工确认」，没有丢。",
        "why": "云端识别有时会排队、限流或返回空结果；程序不会因为一次没识别出来就猜答案，也不会把题判错。",
        "fix": "① 点「重新识别」再试一次（重复识别是幂等的，不会重复计分）\n"
               "② 还是空：换几分钟再试，或直接在待确认里手动填答案\n"
               "③ 长期不稳定：把 config.json 的 ocr.backend 改成 manual，全人工确认",
        "retry_label": "重新识别",
    },
    "ocr_timeout": {
        "title": "识别超时（等太久没回）",
        "what": _timeout_what,
        "why": "拼图太长或服务端排队时会超过等待上限；超时不算判错，这一批仍在待确认队列里。",
        "fix": "① 点「重新识别」再来一次\n"
               "② 这张卡的填空/解答作答格越多，拼图越长——字写小一点、少写草稿能更快\n"
               "③ 网络不稳时把 ocr.backend 改成 manual，先手工判这一份",
        "retry_label": "重新识别",
    },
    "ocr_quota": {
        "title": "识别额度用完 / 被限流",
        "what": _quota_what,
        "why": "云端 OCR 有每分钟/每天的调用额度，短时间连着判好几份最容易触发。",
        "fix": "① 等几分钟再点「重新识别」\n"
               "② 今天额度用完了就明天再识别，或先在待确认里手动判\n"
               "③ 换成本机 manual 后端（config.json 的 ocr.backend），完全不依赖云端",
        "retry_label": "重新识别",
    },
    # ---------------------------------------------------------------- 组卷侧
    "InsufficientQuestions": {
        "title": "题库里凑不够你填的题量",
        "what": _insufficient_what,
        "why": "程序按章节/知识点/题型过滤后，可用题池是有限的；它不会拿别的题凑数，也不会重复用同一道题。",
        "fix": _insufficient_fix,
        "retry_label": "按提示改数量",
    },
    "ScopeError": {
        "title": "选的范围在题库里不存在",
        "what": _scope_what,
        "why": "范围来自下拉框里的章节/知识点；如果题库刚重建过，旧页面里的选项可能已经失效。",
        "fix": "① 刷新页面，让章节/知识点选择器重新从数据库加载\n"
               "② 重新勾选页面上真实存在的章节或知识点\n"
               "③ 想全库出题就什么都别勾（范围留空 = 不限制）",
        "retry_label": "重选范围",
    },
    "WordUnavailable": {
        "title": "本机没有可用的 Word",
        "what": "把卷面 docx 转成 PDF 需要 Microsoft Word；现在检测不到可用的 Word。",
        "why": "排版与分页交给 Word 做，公式、表格、分页才准确；没有它只能走降级渲染，公式与排版可能失真。",
        "fix": "① 装 Microsoft Word 2016 或更新版本，装完重启本程序\n"
               "② 已装但检测不到：手动打开一次 Word，让它完成激活/初始化\n"
               "③ 暂时不想装：先只组卷看预览（公式可能失真），判卷功能不受影响",
        "retry_label": "重新生成卷子",
    },
    # ---------------------------------------------------------------- 数据侧
    "db_missing": {
        "title": "找不到题库数据库",
        "what": _db_missing_what,
        "why": "数据库是建库脚本从 22,898 份 docx 生成的；换电脑、删过 data 目录、或环境变量指错都会这样。",
        "fix": "① 在 E:\\OCR 目录下执行 python app\\build_db.py（约 20 秒）\n"
               "② 路径被改过就清掉环境变量 MATH_APP_DB，或把它指到正确的 app.db\n"
               "③ 建完刷新本页",
        "retry_label": "重新加载",
    },
    "db_locked": {
        "title": "数据库被占用，暂时打不开",
        "what": _db_locked_what,
        "why": "SQLite 同一时刻只允许一个写入者；旧的服务窗口、DB 工具、或云盘同步都会占着锁。",
        "fix": "① 关掉正在跑的旧服务窗口（黑色命令行）\n"
               "② 关掉打开着 app.db 的数据库工具/Excel\n"
               "③ 暂停 OneDrive/坚果云对 app\\data 的同步，再点重试",
        "retry_label": "重试",
    },
    "internal_error": {
        "title": "程序内部出错了",
        "what": _internal_what,
        "why": "多半是数据缺字段或环境缺东西（比如 Word 中途被关掉）；报错信息里通常能看出是哪一步。",
        "fix": "① 先点重试一次，多数是偶发\n"
               "② 打开 /health 看数据库、Word、OCR、目录四项是否正常\n"
               "③ 把这段报错和 /healthz 的 JSON 一起反馈，能最快定位",
        "retry_label": "重试",
    },
}

#: 兜底条目：未知错误码也要给用户一条能走的路
_FALLBACK = {
    "title": "出了点问题（错误码 {code}）",
    "what": _fallback_what,
    "why": "这个错误码还没登记对应文案，程序不想乱猜，怕给你错的建议。",
    "fix": "① 点下面的按钮重试一次\n"
           "② 打开 /health 检查数据库、Word、OCR、数据目录四项\n"
           "③ 还不行就把错误码和 /healthz 的 JSON 一起反馈",
    "retry_label": "重试",
}

_CASE_INSENSITIVE = {code.lower(): code for code in CODES}

_TEXT_LIMIT = 300


def _normalize_detail(detail) -> dict:
    """detail 允许 dict / 字符串 / 异常对象 / None，统一成 dict，坏的也不许抛。"""
    if detail is None:
        return {}
    if isinstance(detail, dict):
        return {str(k): v for k, v in detail.items()}
    if isinstance(detail, BaseException):
        return {"message": str(detail), "error": type(detail).__name__}
    text = str(detail)
    return {"message": text[:_TEXT_LIMIT]}


def _render(value, detail: dict) -> str:
    """字段可以是字符串模板，也可以是 ``f(detail) -> str``。"""
    if callable(value):
        try:
            text = value(detail)
        except Exception as exc:  # noqa: BLE001 - 文案生成不许影响主流程
            text = str(exc)
    else:
        try:
            text = str(value).format_map(_Safe(detail))
        except Exception:  # noqa: BLE001 - 占位符写错也不能崩
            text = str(value)
    return text.strip()


def _resolve(code) -> str | None:
    if not code:
        return None
    key = str(code).strip()
    if key in _ENTRIES:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    lowered = key.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    return _CASE_INSENSITIVE.get(lowered)


def explain(code, detail=None) -> dict:
    """把错误码翻成四段中文 + 一个按钮文案。

    :param code: 错误码（见 :data:`CODES`），支持大小写/下划线别名，未知码走兜底
    :param detail: 可选上下文（dict / 字符串 / 异常），用于填充文案里的 {占位符}
    :return: ``{"title", "what", "why", "fix", "retry_label"}``，五段都非空
    """
    info = _normalize_detail(detail)
    info.setdefault("code", code if code else "unknown")
    if "message" in info:
        info["message"] = str(info["message"])[:_TEXT_LIMIT]

    key = _resolve(code)
    entry = _ENTRIES.get(key) if key else None
    source = entry or _FALLBACK

    out = {}
    for field in ("title", "what", "why", "fix", "retry_label"):
        out[field] = _render(source.get(field, ""), info)

    # 原始信息（picker/paper 的异常 message 里常带可用值）附在 what 后面，别丢掉线索
    message = str(info.get("message") or "").strip()
    if entry and message and message not in out["what"]:
        out["what"] = (out["what"] + "\n（原始信息：%s）" % message).strip()

    for field in ("title", "what", "why", "fix", "retry_label"):
        if not out[field]:
            out[field] = _render(_FALLBACK[field], info)
    return out
