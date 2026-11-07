# -*- coding: utf-8 -*-
"""t53 · 题库导入 Web 化：GET/POST /import + 校验预览 + 结构化报告。

两步式导入（防误点）：

1. ``POST /import``（不带 ``confirm=1``）→ **只校验不落库**，返回预览页（合法 N 行 / 坏 K 行 + 每行原因）
2. ``POST /import``（带 ``confirm=1``）→ 真正写库，返回同样的报告

报告字段固定：``format / total / inserted / updated / skipped / failed / errors[{line,reason}] /
elapsed_s / db_path / dry_run``，并可用 ``GET /import/report.json`` 取同一份 JSON（给脚本/测试用）。

与 CLI（``import_qbank.py``）共用 ``importers`` 层的同一套校验：``read_source`` + ``normalize_rows``。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DEFAULT_DB = os.path.join(BASE_DIR, "data", "app.db")
OUT_ROOT = os.path.join(BASE_DIR, "out")
REPORT_DIRNAME = "import_reports"

#: 上传体积上限（20 MB）：题库文件通常是几百 KB ~ 几 MB，超了基本都是选错文件
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
#: 单次导入行数上限（50000）：再多应该先拆分，避免一次性占满内存
MAX_ROWS = 50000
#: 认得的表格扩展名
KIND_BY_EXT = {".json": "json", ".xlsx": "excel", ".xlsm": "excel", ".csv": "csv"}
KIND_LABELS = {"json": "JSON", "excel": "Excel", "csv": "CSV", "docx": "docx 目录",
               "zip": "ZIP（题目 + images/ 图片）"}
#: 支持的上传格式（白名单）→ 临时文件后缀；不在表里的一律走「不支持的格式」分支
FORMAT_SUFFIX = {"json": ".json", "excel": ".xlsx", "csv": ".csv", "zip": ".zip"}
SUPPORTED_HINT = "支持的格式：csv / excel / json / docx"
#: 预览最多列多少行（页面「前 20 行预览」）
PREVIEW_LIMIT = 20

log = logging.getLogger(__name__)

#: 「让 AI 帮我把 Word 转成 JSON」的提示词。页面上可一键复制，docs/AI-转JSON-提示词.md 同文。
#: 改这里 = 改产品里的提示词；模板与文档都引用/抄这一份（tests/test_ai_prompt.py 会核对一致）。
AI_PROMPT = """你是题库格式转换器。下面我贴一段题目（从 Word 复制即可），请转成我需要的 JSON。
只输出 JSON，不要解释、不要 Markdown 代码块、不要省略号。

{"subject":"数学","grade":"七年级上","version":"沪科版","items":[{"no":"01001","qtype":"选择","stem":"…","options":["A. …","B. …","C. …","D. …"],"answer":"B","kp":"","chapter":""}]}

规则：
- no：5 位数字字符串，从 00001 顺序编，文件内不重复
- qtype：只能是「选择」「填空」「解答」
- stem：题干纯文本，公式保留原样（2x+1=5、x²、√3），不要改写或翻译
- options：仅选择题需要，4 项 A-D 开头；其他题型给 []
- answer：只写最终答案。选择写字母（B 或 AB）；填空/解答写答案本身；多解用英文逗号分隔（-1,2）；没有答案给 ""
- kp / chapter：能从原文看出才填，看不出给 ""
- 分数写 3/4，带分数写 1 3/4，圆周率写 π
- 一份材料里有多道题就逐题输出；只有一道就 1 项
- 不确定的字段宁可留空，不要编造"""

#: 「下载示例 JSON」给的文件内容：1 道选择题 + 1 道填空题，保证 importers 能零错误解析。
#: 题号故意用 99001/99002（不在真库号段里）—— 免得老师把示例拖进去就把真题覆盖了。
SAMPLE_JSON = {
    "subject": "数学",
    "grade": "七年级上",
    "version": "沪科版",
    "items": [
        {
            "no": "99001",
            "qtype": "选择",
            "stem": "计算：(-3) + 5 的结果是（　）",
            "options": ["A. -8", "B. 2", "C. -2", "D. 8"],
            "answer": "B",
            "kp": "1.1",
            "chapter": "第1章 有理数",
        },
        {
            "no": "99002",
            "qtype": "填空",
            "stem": "计算：3/4 + 1/4 = ____．",
            "options": [],
            "answer": "1",
            "kp": "1.4",
            "chapter": "第1章 有理数",
        },
    ],
}

TEMPLATE_FILENAME = "qbank-template.json"

router = APIRouter()

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR, encoding="utf-8"),
    autoescape=select_autoescape(("html", "xml")),
    auto_reload=True,
)
_env.filters["num"] = lambda v: ("{:,}".format(int(v)) if isinstance(v, int) else v)


def render(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(_env.get_template(name).render(**ctx), status_code=status_code)


# ---------------------------------------------------------------- 小工具

def _db_path(request: Request) -> str:
    return getattr(request.app.state, "db_path", None) or DEFAULT_DB


def _out_root(request: Request) -> str:
    return getattr(request.app.state, "out_dir", None) or OUT_ROOT


def _empty_report(kind: str, db_path: str, dry_run: bool) -> dict:
    return {"format": kind or "", "total": 0, "inserted": 0, "updated": 0, "skipped": 0,
            "failed": 0, "errors": [], "elapsed_s": 0.0, "db_path": db_path,
            "dry_run": dry_run, "items": [], "by_subject": {},
            # t87：这批题里「题干说如图、题面却没图」的（组卷时 picker 会自动跳过）
            "fig_missing": 0, "fig_missing_nos": []}


def _writable(db_path: str) -> bool:
    """库文件/目录可写吗？只读时降级成「只校验不写库」，绝不 500。"""
    if not os.path.exists(db_path):
        parent = os.path.dirname(os.path.abspath(db_path))
        return os.access(parent or ".", os.W_OK)
    return os.access(db_path, os.W_OK)


def _save_report(request: Request, report: dict) -> str:
    """落一份 out/import_reports/<ts>.json 便于追溯（顺手记 meta.import_runs）。"""
    folder = os.path.join(_out_root(request), REPORT_DIRNAME)
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + ("-%d" % (int(time.time() * 1000) % 1000))
    path = os.path.join(folder, "%s.json" % stamp)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("写导入报告失败 %s: %s", type(exc).__name__, exc)
    try:
        import db as dbmod

        conn = dbmod.connect(_db_path(request))
        try:
            dbmod.log_import_run(conn, report)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 记录失败不影响导入结果
        log.warning("记 import_runs 失败 %s: %s", type(exc).__name__, exc)
    request.app.state.import_report = report
    return path


def _last_report(request: Request):
    rep = getattr(request.app.state, "import_report", None)
    if rep:
        return rep
    folder = os.path.join(_out_root(request), REPORT_DIRNAME)
    try:
        files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    except OSError:
        return None
    if not files:
        return None
    try:
        with open(os.path.join(folder, files[-1]), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _subjects(request: Request):
    try:
        import db as dbmod

        conn = dbmod.connect(_db_path(request))
        try:
            return [dict(r) for r in dbmod.list_subjects(conn)]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 没库就空列表，页面照样能开
        return []


def _read_upload(upload: UploadFile, limit: int):
    """分块读，超限立刻停（不把 200MB 读进内存）。返回 (data, err)。"""
    chunks, size = [], 0
    while True:
        chunk = upload.file.read(1024 * 256)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            def _human(n):
                return ("%.1f MB" % (n / 1048576.0)) if n >= 1048576 else ("%.0f KB" % (n / 1024.0))
            return None, "文件超过上限 %s（当前已读到 %s），请拆分后再传" % (
                _human(limit), _human(size))
        chunks.append(chunk)
    return b"".join(chunks), None


def _kind_of(filename: str, form_kind: str) -> str:
    kind = (form_kind or "").strip().lower()
    if kind in KIND_BY_EXT.values() or kind == "docx":
        return kind
    return KIND_BY_EXT.get(os.path.splitext(filename or "")[1].lower(), "")


def _explain_exception(exc: Exception) -> str:
    """把解析异常翻译成人话（损坏的 xlsx / 非 UTF-8 文本 都要给出修复建议）。"""
    name = type(exc).__name__
    text = str(exc)
    if name in ("BadZipFile", "InvalidFileException") or "not a zip file" in text:
        return ("这个文件打不开（%s）：可能已损坏，或者只是把旧的 .xls 改了扩展名——"
                "请用 Excel 打开后「另存为」.xlsx 再传" % name)
    if isinstance(exc, UnicodeDecodeError):
        return "文本不是 UTF-8 编码（%s）：请另存为 UTF-8 的 CSV 再传" % text
    return "导入失败：%s: %s" % (name, text)


def _fig_missing_among(db_path: str, nos=None) -> dict:
    """题干指代了图但题面没图 → 组卷时会被跳过（口径与 picker 完全一致，直接问 picker）。

    ``nos`` 给了就只算这批题号；不给就数全库（docx 全量重建用）。
    """
    try:
        import db as dbmod
        import picker

        conn = dbmod.connect(db_path)
        try:
            rows = picker._fig_missing_rows(conn, None)
        finally:
            conn.close()
        if nos is None:
            hit = sorted({str(r[0]) for r in rows})
        else:
            wanted = {str(n) for n in nos if str(n).strip()}
            hit = sorted({str(r[0]) for r in rows if str(r[0]) in wanted})
        return {"count": len(hit), "nos": hit[:20]}
    except Exception as exc:  # noqa: BLE001 - 数不出来不影响导入结果
        log.warning("缺图统计失败 %s: %s", type(exc).__name__, exc)
        return {"count": 0, "nos": []}


def _preview_items(items, errors, raw_rows, limit: int = PREVIEW_LIMIT) -> list:
    """把校验结果压成「前 N 行预览」：line / no / qtype / stem(截 60) / answer / status。

    * 合法行 → status="ok"（预览里还会被 existed 判成 skip，但那要在查库后才定）
    * 问题行 → status="error"，题干位放错误原因（模板同一列显示）
    按行号排序后取前 limit 条。
    """
    rows = []
    for item in items or []:
        rows.append({
            "line": int(item.get("_line") or 0),
            "no": str(item.get("no") or ""),
            "qtype": str(item.get("qtype") or ""),
            "stem": str(item.get("stem") or "")[:60],
            "answer": str(item.get("answer") or ""),
            "status": "ok",
        })
    for err in errors or []:
        rows.append({
            "line": int(err.get("line") or 0),
            "no": "",
            "qtype": "",
            "stem": str(err.get("reason") or "")[:60],
            "answer": "",
            "status": "error",
        })
    rows.sort(key=lambda r: (r["line"] or 0))
    return rows[:max(1, int(limit))]


# ---------------------------------------------------------------- 页面

def _page(request: Request, report=None, message: str = "", status_code: int = 200,
          subjects=None, form=None):
    if report is None:                 # GET 打开页面时显示「上一次导入结果」
        report = _last_report(request)
    return render("import.html", status_code=status_code,
                  page_title="题库导入", nav_key="import",
                  report=report, message=message,
                  subjects=subjects if subjects is not None else _subjects(request),
                  form=form or {}, max_upload_mb=MAX_UPLOAD_BYTES / 1048576.0,
                  max_rows=MAX_ROWS, ai_prompt=AI_PROMPT,
                  template_name=TEMPLATE_FILENAME,
                  last_report=_last_report(request))


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return _page(request)


@router.get("/import/report.json")
def import_report_json(request: Request):
    rep = _last_report(request)
    if not rep:
        return JSONResponse(_empty_report("", _db_path(request), True), status_code=200)
    return JSONResponse(rep)


@router.get("/import/template.json")
def import_template_json():  # noqa: ANN202
    """示例 JSON：1 道选择 + 1 道填空，拖回 /import 就能直接导入（给 AI 当输出样板）。"""
    payload = json.dumps(SAMPLE_JSON, ensure_ascii=False, indent=2) + "\n"
    return Response(
        content=payload.encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % TEMPLATE_FILENAME},
    )


@router.post("/import", response_class=HTMLResponse)
def import_post(request: Request,
                file: UploadFile | None = File(None),
                format: str = Form(""),
                subject: str = Form(""),
                grade: str = Form(""),
                version: str = Form(""),
                dry_run: str = Form(""),
                docx_dir: str = Form(""),
                renumber: str = Form(""),
                confirm: str = Form("")):
    started = time.time()
    db_path = _db_path(request)
    wants_dry = bool(str(dry_run).strip() in ("1", "true", "on", "yes"))
    confirmed = str(confirm).strip() in ("1", "true", "on", "yes")
    # t90：题号撞别的学科时，是否按本学科号段自动改号（默认关 = 拒绝该行）
    do_renumber = str(renumber).strip() in ("1", "true", "on", "yes")
    kind = _kind_of(getattr(file, "filename", ""), format) or (format or "").strip().lower()
    form = {"format": kind, "subject": subject, "grade": grade, "version": version,
            "dry_run": "1" if wants_dry else "", "docx_dir": docx_dir,
            "renumber": "1" if do_renumber else ""}

    report = _empty_report(kind, db_path, True)

    # ---- docx：目录导入（复用 build_db），不做单文件上传
    if kind == "docx":
        target = (docx_dir or "").strip() or (getattr(file, "filename", "") or "").strip()
        if not target or not os.path.isdir(target):
            report["errors"].append(
                {"line": 0, "reason": "docx 需要给一个**目录**路径（题库的 *.docx 所在目录），"
                                      "单文件上传不支持；也可用命令行 python import_qbank.py --from docx <目录>"})
            report["failed"] = 1
            report["elapsed_s"] = round(time.time() - started, 3)
            _save_report(request, report)
            return _page(request, report, "docx 导入需要目录路径", form=form)
        if wants_dry or not confirmed:
            report["errors"].append({"line": 0, "reason": "docx 是全量重建，需勾选「确认导入」后执行"})
            report["elapsed_s"] = round(time.time() - started, 3)
            _save_report(request, report)
            return _page(request, report, "docx 导入需确认", form=form)
        try:
            import importers as imp

            res = imp.import_docx(target, db_path=db_path)
            report.update({"total": int(res.get("questions") or 0),
                           "inserted": int(res.get("questions") or 0), "dry_run": False})
            _fig = _fig_missing_among(db_path)
            report["fig_missing"] = _fig["count"]
            report["fig_missing_nos"] = _fig["nos"]
        except Exception as exc:  # noqa: BLE001 - docx 失败也要给人话
            log.warning("导入失败 %s: %s", type(exc).__name__, exc)
            report["errors"].append({"line": 0, "reason": "docx 导入失败：%s: %s"
                                                          % (type(exc).__name__, exc)})
            report["failed"] = 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, "docx 导入完成" if not report["errors"] else "docx 导入失败",
                     form=form)

    # ---- 表格/JSON：先存临时文件，再走 importers 的同一套校验
    if file is None or not getattr(file, "filename", ""):
        report["errors"].append({"line": 0, "reason": "没有收到文件（表单字段名要是 file）"})
        report["failed"] = 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, "请选择要导入的文件", form=form)

    data, err = _read_upload(file, MAX_UPLOAD_BYTES)
    if err:
        report["errors"].append({"line": 0, "reason": err})
        report["failed"] = 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, err, form=form)
    if not data.strip():
        report["errors"].append({"line": 0, "reason": "文件是空的（0 字节）"})
        report["failed"] = 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, "文件是空的", form=form)
    if not kind:
        report["errors"].append({"line": 0, "reason": "认不出格式：请选 format，或使用 .csv/.xlsx/.json 扩展名"})
        report["failed"] = 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, "认不出文件格式", form=form)

    suffix = FORMAT_SUFFIX.get(kind)
    if suffix is None:
        # 认得出文件名/表单给了一个格式，但它不在白名单里（txt/md/… ）：
        # 以前这里是 `{...}[kind]` 直接 KeyError → 真服 500，现在走人话分支
        hint = SUPPORTED_HINT
        report["errors"].append({"line": 0, "reason": "不支持的格式：%s（%s）"
                                                  % (kind or "未知", hint)})
        report["failed"] = 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, "不支持的格式：%s" % (kind or "未知"), form=form)
    tmp_dir = tempfile.mkdtemp(prefix="import_web_")
    tmp_path = os.path.join(tmp_dir, "upload" + suffix)
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)

        import db as dbmod
        import importers as imp
        from importers import core as imp_core

        # ---- t88：ZIP = 题目文件 + images/，一次导完（也支持只传 images.zip 挂图）
        if kind == "zip":
            subj = {k: str(v).strip() for k, v in
                    (("name", subject), ("grade", grade), ("version", version))
                    if str(v or "").strip()}
            try:
                rep_zip = imp_core.import_zip(
                    tmp_path, "zip", db_path=db_path,
                    subject=subj or None, dry_run=(not confirmed or wants_dry),
                    renumber=do_renumber)
            except Exception as exc:  # noqa: BLE001 - 坏 zip 给人话，不 500
                report["errors"].append({"line": 0, "reason": _explain_exception(exc)})
                report["failed"] = 1
                report["elapsed_s"] = round(time.time() - started, 3)
                _save_report(request, report)
                return _page(request, report, "ZIP 导入失败（详见下方原因）", form=form)
            report.update({
                "format": "zip",
                "total": int(rep_zip.get("rows") or 0),
                "inserted": int(rep_zip.get("new") or 0),
                "updated": int(rep_zip.get("updated") or 0),
                "skipped": int(rep_zip.get("skipped") or 0),
                "failed": int(rep_zip.get("failed") or 0),
                "errors": list(rep_zip.get("errors") or []),
                "items": list(rep_zip.get("items") or [])[:PREVIEW_LIMIT],
                "by_subject": rep_zip.get("by_subject") or {},
                "subject_pk": rep_zip.get("subject_pk") or "",
                "figures_saved": rep_zip.get("figures_saved", 0),
                "figures_skipped": rep_zip.get("figures_skipped", 0),
                "figures_updated": rep_zip.get("figures_updated", 0),
                "figures_unmatched": rep_zip.get("figures_unmatched") or [],
                "figures_unmatched_count": rep_zip.get("figures_unmatched_count", 0),
                "figures_still_missing": rep_zip.get("figures_still_missing") or [],
                "figures_still_missing_count": rep_zip.get("figures_still_missing_count", 0),
                "dry_run": bool(not confirmed or wants_dry),
            })
            _fig2 = _fig_missing_among(_db_path(request), None)
            report["fig_missing"] = _fig2["count"]
            report["fig_missing_nos"] = _fig2["nos"]
            report["elapsed_s"] = round(time.time() - started, 3)
            _save_report(request, report)
            msg = ("ZIP 导入完成：新增 %d / 更新 %d；图片落盘 %d 张、跳过(重复) %d、未匹配 %d、仍缺图 %d"
                   % (report["inserted"], report["updated"], report["figures_saved"],
                      report["figures_skipped"], report["figures_unmatched_count"],
                      report["figures_still_missing_count"]))
            return _page(request, report, msg, form=form)

        # 学科信息
        subj = {}
        for key, val in (("name", subject), ("grade", grade), ("version", version)):
            if str(val or "").strip():
                subj[key] = str(val).strip()

        # 1) 读源 + 全量校验（不落库）
        meta, raw_rows = imp.read_source(tmp_path, kind)
        if len(raw_rows) > MAX_ROWS:
            report["errors"].append(
                {"line": 0, "reason": "行数 %d 超过单次上限 %d，请拆分后再导入"
                                      % (len(raw_rows), int(MAX_ROWS))})
            report["failed"] = 1
            report["total"] = len(raw_rows)
            report["elapsed_s"] = round(time.time() - started, 3)
            _save_report(request, report)
            return _page(request, report, "行数超过上限", form=form)
        if not raw_rows:
            report["errors"].append({"line": 0, "reason": "文件里没有数据行（只有表头或缺表头）"})
            report["failed"] = 1
            report["elapsed_s"] = round(time.time() - started, 3)
            _save_report(request, report)
            return _page(request, report, "没有数据行", form=form)

        name = subj.get("name") or meta.get("subject") or "数学"
        grade_v = subj.get("grade") or meta.get("grade") or "七年级上"
        version_v = subj.get("version") or meta.get("version") or "沪科版"
        subject_pk = imp._subject_pk(name, grade_v, version_v)

        items, errors = imp.normalize_rows(raw_rows, subject_pk)
        report.update({"total": len(raw_rows), "failed": len(errors), "errors": errors})

        readonly = not _writable(db_path)
        do_write = confirmed and not wants_dry and not readonly
        if readonly:
            report["errors"].append({"line": 0, "reason": "题库文件没有写权限，本次只做了校验（未写库）"})
        report["dry_run"] = not do_write

        if do_write:
            conn = dbmod.connect(db_path)
            try:
                dbmod.ensure_subject(conn, subject_pk, name, grade_v, version_v)
                res = imp.upsert_items(conn, items, subject_pk, renumber=do_renumber)
            finally:
                conn.close()
            report.update({"inserted": res["new"], "updated": res["updated"],
                           "skipped": res.get("skipped", 0),
                           "rejected": res.get("rejected", 0),
                           "conflicts": res.get("conflicts", []),
                           "renumbered": res.get("renumbered", [])})
            # t73：导完立刻失效「剩余题量」缓存，老师切回组卷页看到的必须是新数字
            try:
                import server as _srv

                _srv.clear_available_cache()
            except Exception:  # noqa: BLE001 - 单独跑 import_routes 时没有 server 也不该炸
                traceback.print_exc()
            # 重复题号（同一文件里出现两次）在报告里也要看得见
        else:
            # 预览还要告诉用户：哪些行本来会被跳过（重复题号）
            conn = dbmod.connect(db_path)
            try:
                existing = {r[0] for r in conn.execute("SELECT no FROM questions")}
            finally:
                conn.close()
            seen = set()
            dups = 0
            for item in items:
                if item["no"] in existing or item["no"] in seen:
                    dups += 1
            report["skipped"] = dups

        # 逐行预览（t63）：页面「前 20 行预览」表要能看到每一行的题号/题型/题干/答案/状态
        report["items"] = _preview_items(items, errors, raw_rows, PREVIEW_LIMIT)
        # t87：导入时就把「含图但缺图」的题点出来（组卷时它们会被自动跳过）
        _fig = _fig_missing_among(_db_path(request), [it["no"] for it in items])
        report["fig_missing"] = _fig["count"]
        report["fig_missing_nos"] = _fig["nos"]
        # 学科维度（t63）：这次导入落在哪个学科、新增/跳过/失败各几行
        report["by_subject"] = {
            subject_pk: {
                "inserted": int(report.get("inserted") or 0) if do_write else len(items),
                "skipped": int(report.get("skipped") or 0),
                "failed": int(report.get("failed") or 0),
            }
        }
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        msg = ("导入完成：新增 %d / 更新 %d / 跳过 %d / 失败 %d"
               % (report["inserted"], report["updated"], report["skipped"], report["failed"])
               if do_write else
               "校验完成（未写库）：合法 %d 行 / 问题 %d 行"
               % (len(items), len(errors)))
        return _page(request, report, msg, form=form)
    except Exception as exc:  # noqa: BLE001 - 任何解析异常都要变成人话，不许 500
        log.warning("导入失败 %s: %s", type(exc).__name__, exc)
        report["errors"].append({"line": 0, "reason": _explain_exception(exc)})
        report["failed"] = report["failed"] + 1
        report["elapsed_s"] = round(time.time() - started, 3)
        _save_report(request, report)
        return _page(request, report, "导入失败（详见下方原因）", form=form)
    finally:
        try:
            os.remove(tmp_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass
