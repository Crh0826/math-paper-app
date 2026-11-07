# -*- coding: utf-8 -*-
"""t61 独立验收（verifier 另写，不复用开发侧用例）。

三项新功能：
  ① 题库导入 `/import`：20 行 CSV（2 行坏数据）→ 校验预览 → 确认导入 → 计数 +18 → 再导一次不变 → 坏行逐行有原因
  ② 改卷记录留痕与撤销：判一张卡 → 改 2 个空 → 记录 2 条 → 撤销该批 → 分数回到改前 + undo 留痕 → CSV 行数对得上 → 重复提交不产生重复行
  ③ 学生信息：v3 卡涂学号 → 模拟拍照判卷 → submissions.student_no 有值 → 结果页/记录页显示 → 旧 v2 卡仍能判且 student_no 为空

跑法：``cd E:\\OCR\\app && python -m unittest tests.test_acceptance_v2``
证据：全部写 ``app/out/acceptance_v2/``（JSON 数字 + 页面 HTML + PNG 截图 + CSV）。

环境：真 HTTP（fastapi TestClient，进程内）+ **真库副本**（不碰 app/data/app.db）+ 独立 out 目录。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest

import cv2

import card
import numpy as np
from PIL import Image, ImageDraw

APP = r"E:\OCR\app"
if APP not in sys.path:
    sys.path.insert(0, APP)

DB_SRC = os.path.join(APP, "data", "app.db")
EV = os.path.join(APP, "out", "acceptance_v2")
WORK = os.path.join(EV, "work")
OUTD = os.path.join(WORK, "out")
CSS = os.path.join(APP, "static", "app.css")
EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
             if os.path.exists(p)), None)


# ---------------------------------------------------------------- 基础设施
def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def dump(name, obj):
    _ensure(EV)
    with open(os.path.join(EV, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return os.path.join(EV, name)


def _db_copy():
    """每次进程一份全新副本（导入测试会改库；复用旧副本会让第二次跑假红）。"""
    _ensure(WORK)
    dst = os.path.join(WORK, "app_%d.db" % os.getpid())
    if not os.path.exists(dst):
        for name in os.listdir(WORK):           # 清掉上次进程留下的副本
            if name.startswith("app_") and name.endswith(".db") and name != os.path.basename(dst):
                try:
                    os.remove(os.path.join(WORK, name))
                except OSError:
                    pass
        shutil.copyfile(DB_SRC, dst)
    return dst


def _conn(path=None):
    conn = sqlite3.connect(path or _db_copy())
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql, args=()):
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def save_page(html, name):
    """页面 HTML 落盘（把 /static/app.css 内联进去，截图才有样式）。"""
    _ensure(EV)
    css = ""
    if os.path.exists(CSS):
        with open(CSS, encoding="utf-8") as f:
            css = f.read()
    html = re.sub(r'<link[^>]+href="/static/app\.css"[^>]*>',
                  "<style>%s</style>" % css, html)
    path = os.path.join(EV, name + ".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def shot(html_path, name, width=1280, height=1900):
    """headless Edge 截图（Edge 不在就跳过，不判失败）。"""
    if not EDGE:
        return ""
    png = os.path.join(EV, name + ".png")
    url = "file:///" + html_path.replace("\\", "/")
    try:
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--window-size=%d,%d" % (width, height),
                        "--screenshot=%s" % png, url],
                       timeout=90, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:            # noqa: BLE001
        return ""
    return png if os.path.exists(png) else ""


def read_gray(path, tries=6):
    """读 PNG（并发/半写场景重试）。"""
    for _ in range(tries):
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None and img.size:
            return img
        time.sleep(0.15)
    return None


def paint_card(png_path, card_info, choice_of, student_no=None):
    """在答题卡 PNG 上作答：选择题涂指定选项 +（v3）涂学号。返回新 PNG 路径。"""
    gray = read_gray(png_path)
    if gray is None:
        raise AssertionError("答题卡 PNG 读不出来：%s" % png_path)
    img = Image.fromarray(gray)
    d = ImageDraw.Draw(img)
    for page in card_info["pages"]:
        for it in page["items"]:
            if it["kind"] != "omr":
                continue
            want = choice_of.get(it["idx"])
            if want is None:
                continue
            for c in it["cells"]:
                if str(c["opt"]) == str(want):
                    d.rectangle([c["x"] + 9, c["y"] + 9,
                                 c["x"] + c["w"] - 9, c["y"] + c["h"] - 9], fill=0)
    st = (card_info or {}).get("student") or {}
    if student_no and st.get("columns"):
        digits = [int(ch) for ch in str(student_no)]
        cols = st["columns"]
        assert len(digits) == len(cols), "学号位数与卡面位数不一致：%d vs %d" % (
            len(digits), len(cols))
        for row, digit in zip(cols, digits):
            for c in row["cells"]:
                if int(c["digit"]) == digit:
                    d.rectangle([c["x"] + 5, c["y"] + 5,
                                 c["x"] + c["w"] - 5, c["y"] + c["h"] - 5], fill=0)
    path = os.path.join(os.path.dirname(png_path), "filled_%s.png" % os.path.basename(png_path))
    img.save(path)
    return path


def synth_photo(src_png, dst_jpg, seed=61, jitter=0.03, quality=72, scale=0.72):
    """答题卡 → 模拟手机拍照：透视 3% + 光照梯度 + 噪点 + 模糊 + JPEG72 + 缩到 72%。"""
    img = cv2.imread(src_png, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise AssertionError("读不出答题卡：%s" % src_png)
    h, w = img.shape[:2]
    rng = np.random.RandomState(seed)
    W, H = int(w * scale), int(h * scale)
    j = jitter
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([
        [W * rng.uniform(j * 0.3, j), H * rng.uniform(j * 0.3, j)],
        [W * (1 - rng.uniform(j * 0.3, j)), H * rng.uniform(j * 0.4, j * 1.2)],
        [W * (1 - rng.uniform(j * 0.3, j)), H * (1 - rng.uniform(j * 0.3, j))],
        [W * rng.uniform(j * 0.3, j), H * (1 - rng.uniform(j * 0.3, j))],
    ])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, M, (W, H), borderValue=190)
    yy, xx = np.mgrid[0:H, 0:W]
    grad = 0.72 + 0.28 * (xx / max(W - 1, 1)) + 0.22 * (yy / max(H - 1, 1))
    out = warped.astype(np.float32) * grad + rng.normal(0, 4, (H, W))
    out = cv2.GaussianBlur(np.clip(out, 0, 255).astype(np.uint8), (3, 3), 0)
    cv2.imwrite(dst_jpg, out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return dst_jpg


def _client():
    from fastapi.testclient import TestClient
    import server
    app = server.create_app(db_path=_db_copy(), out_dir=_ensure(OUTD),
                            config_path=os.path.join(APP, "config.json"))
    return app, TestClient(app, follow_redirects=False)


def _manifest_of(pid):
    rows = _q("SELECT manifest FROM papers WHERE pid=?", (pid,))
    assert rows, "papers 里没有 %s" % pid
    return json.loads(rows[0]["manifest"])


def _detail_of(sid):
    rows = _q("SELECT detail_json FROM submissions WHERE sid=?", (sid,))
    assert rows, "submissions 里没有 %s" % sid
    return json.loads(rows[0]["detail_json"])


def _create_paper(client, title, mix=(10, 8, 6), seed=20261031):
    r = client.post("/papers", data={"title": title, "seed": str(seed),
                                     "n_choice": str(mix[0]), "n_fill": str(mix[1]),
                                     "n_solution": str(mix[2]),
                                     "layout": "compact", "need_answer": "on"})
    assert r.status_code == 302, "POST /papers 应 302，实际 %s" % r.status_code
    pid = (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
    assert pid.startswith("P"), "没拿到 pid：%r" % r.headers.get("location")
    return pid


def _upload(client, pid, photo_path):
    with open(photo_path, "rb") as f:
        r = client.post("/submissions", data={"pid": pid},
                        files={"file": ("photo.jpg", f.read(), "image/jpeg")})
    assert r.status_code == 302, "上传应 302，实际 %s（%s）" % (
        r.status_code, (r.text or "")[:200])
    return (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------- ① 题库导入
class T1ImportFlow(unittest.TestCase):
    """20 行 CSV（18 好 + 2 坏）→ 预览 → 确认 → +18 → 再导一次不变。"""

    HEADER = "no,qtype,stem,answer,options,analysis,kp_id,chapter,figure"
    SUBJECT = "验收科目61"

    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()
        cls.csv_path = os.path.join(_ensure(EV), "import_20rows.csv")
        rows = [cls.HEADER]
        for i in range(1, 19):                       # 18 条好数据
            qtype = "选择" if i % 3 == 0 else ("填空" if i % 3 == 1 else "解答")
            opts = "A.1|B.2|C.3|D.4" if qtype == "选择" else ""
            rows.append("ACC61%03d,%s,验收题面 %d（%s）,答案%d,%s,,1.1,验收章,"
                        % (i, qtype, i, qtype, i, opts))
        # 2 条坏数据：缺 answer / 选择题没选项
        rows.append("ACC61091,填空,缺答案的题面,,,  ,,,")
        rows.append("ACC61092,选择,选择题但没选项,答案,  ,,,,")
        with open(cls.csv_path, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(rows) + "\n")
        conn = _conn()
        try:
            cls.before = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        finally:
            conn.close()
        cls.preview = cls.confirm = cls.again = None

    def _post(self, extra):
        with open(self.csv_path, "rb") as f:
            content = f.read()
        data = {"format": "csv", "subject": self.SUBJECT,
                "grade": "七年级上", "version": "验收版"}
        data.update(extra)
        return self.client.post("/import", data=data,
                                files={"file": ("t61_20rows.csv", content, "text/csv")})

    def test_01_preview_does_not_write(self):
        r = self._post({"dry_run": "1"})
        self.assertEqual(200, r.status_code, "预览应 200，实际 %s" % r.status_code)
        rep = self.client.get("/import/report.json").json()
        type(self).preview = rep
        dump("import_preview.json", rep)
        save_page(r.text, "01_import_preview")
        shot(os.path.join(EV, "01_import_preview.html"), "01_import_preview")
        self.assertTrue(rep.get("dry_run"), "预览态报告应标 dry_run")
        self.assertEqual(20, int(rep.get("total") or 0), "总行数应为 20")
        self.assertEqual(2, int(rep.get("failed") or 0), "问题行应为 2")
        self.assertEqual(0, int(rep.get("inserted") or 0), "预览不许写库")
        after = _q("SELECT COUNT(*) n FROM questions")[0]["n"]
        self.assertEqual(self.before, after, "预览后题库行数不该变")
        reasons = [e.get("reason") or "" for e in rep.get("errors") or []]
        self.assertEqual(2, len(reasons), "坏行要逐行给原因：%s" % rep.get("errors"))
        self.assertTrue(any("answer" in x for x in reasons), "缺答案那行应说缺 answer：%s" % reasons)
        self.assertTrue(any("选项" in x for x in reasons), "无选项那行应说缺选项：%s" % reasons)
        self.assertTrue(all(e.get("line") for e in rep["errors"]), "每条坏行都要带行号")

    def test_02_confirm_inserts_18(self):
        r = self._post({"confirm": "1"})
        self.assertEqual(200, r.status_code)
        rep = self.client.get("/import/report.json").json()
        type(self).confirm = rep
        dump("import_confirm.json", rep)
        save_page(r.text, "02_import_report")
        shot(os.path.join(EV, "02_import_report.html"), "02_import_report")
        self.assertEqual(18, int(rep.get("inserted") or 0), "应新增 18 条：%s" % rep)
        self.assertEqual(2, int(rep.get("failed") or 0))
        rows = _q("SELECT no, qtype, answer_raw, subject_pk FROM questions"
                  " WHERE no LIKE 'ACC61%' ORDER BY no")
        self.assertEqual(18, len(rows), "库里应有 18 条 ACC61 题")
        after = _q("SELECT COUNT(*) n FROM questions")[0]["n"]
        dump("import_db_counts.json", {"before": self.before, "after": after,
                                       "delta": after - self.before,
                                       "acc61_rows": len(rows),
                                       "subject_pk": rows[0]["subject_pk"] if rows else ""})
        self.assertEqual(self.before + 18, after, "questions 应 +18")

    def test_03_reimport_is_idempotent(self):
        r = self._post({"confirm": "1"})
        self.assertEqual(200, r.status_code)
        rep = self.client.get("/import/report.json").json()
        type(self).again = rep
        dump("import_reimport.json", rep)
        self.assertEqual(0, int(rep.get("inserted") or 0), "第二次导入不该新增：%s" % rep)
        after = _q("SELECT COUNT(*) n FROM questions")[0]["n"]
        self.assertEqual(self.before + 18, after, "第二次导入后行数不该变")
        self.assertEqual(2, int(rep.get("failed") or 0), "坏行仍是 2 条")


# ---------------------------------------------------------------- ② 改卷记录
class T2EditTrail(unittest.TestCase):
    """判一张卡 → 改 2 个空 → 2 条记录 → 撤销 → 分数回退 + undo 留痕 + CSV 对得上。"""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()
        cls.pid = _create_paper(cls.client, "t61 改卷记录卷", mix=(6, 4, 2), seed=20261031)
        man = _manifest_of(cls.pid)
        card = man["card"]
        by_idx = {it["idx"]: it for it in man["items"]}
        choice = {}
        for page in card["pages"]:
            for it in page["items"]:
                if it["kind"] != "omr":
                    continue
                key = (by_idx[it["idx"]].get("answer_key") or [{}])[0]
                choice[it["idx"]] = str(key.get("value") or key.get("opt") or "")
        pngs = sorted(p for p in os.listdir(os.path.join(OUTD, cls.pid))
                      if p.startswith("card_") and p.endswith(".png"))
        first = os.path.join(OUTD, cls.pid, pngs[0])
        filled = paint_card(first, card, choice)
        photo = synth_photo(filled, os.path.join(WORK, "edit_photo.jpg"), seed=611)
        cls.sid = _upload(cls.client, cls.pid, photo)
        cls.status_before = cls.client.get("/submissions/%s/status" % cls.sid).json()
        cls.detail_before = _detail_of(cls.sid)

    def test_01_baseline_graded(self):
        st = self.status_before
        dump("edits_00_baseline.json", st)
        self.assertTrue(st.get("warp_ok"), "标定应成功：%s" % st)
        self.assertGreater(st.get("total") or 0, 0, "应有判分单元")
        self.assertGreater(st.get("pending") or 0, 1, "填空/解答应留待确认（≥2 空）：%s" % st)

    def test_02_confirm_two_units_records_two(self):
        pending = [u for u in self.detail_before["items"] if u["verdict"] == "pending"]
        self.assertGreaterEqual(len(pending), 2, "待确认单元不足 2 个")
        picked = pending[:2]
        data = {}
        for u in picked:
            data["verdict_%s_%s" % (u["idx"], u["blank"])] = "correct"
            data["text_%s_%s" % (u["idx"], u["blank"])] = str(u.get("expected") or "")
        r = self.client.post("/submissions/%s/confirm" % self.sid, data=data)
        self.assertIn(r.status_code, (200, 303), "confirm 应 303，实际 %s" % r.status_code)
        st = self.client.get("/submissions/%s/status" % self.sid).json()
        type(self).status_after = st
        edits = self.client.get("/submissions/%s/edits" % self.sid).json()
        type(self).edits_after = edits
        dump("edits_01_after_confirm.json", {"status": st, "edits": edits,
                                             "confirmed_units": [(u["idx"], u["blank"]) for u in picked]})
        self.assertEqual(2, len(edits.get("edits") or []),
                         "应恰好 2 条改卷记录：%s" % edits.get("edits"))
        self.assertEqual(self.status_before["score"] + 2, st["score"],
                         "分数应 +2（%s → %s）" % (self.status_before["score"], st["score"]))
        batch = edits["edits"][0].get("batch_id")
        type(self).batch = batch
        self.assertTrue(batch, "记录里要有 batch_id")
        # 结果页能打开且带改卷记录区
        page = self.client.get("/submissions/%s" % self.sid)
        self.assertEqual(200, page.status_code)
        save_page(page.text, "03_grade_result_after_confirm")
        shot(os.path.join(EV, "03_grade_result_after_confirm.html"),
             "03_grade_result_after_confirm")
        self.assertIn("改卷记录", page.text, "结果页应有改卷记录区")

    def test_03_duplicate_submit_no_new_rows(self):
        edits_before = self.client.get("/submissions/%s/edits" % self.sid).json()
        pending = [u for u in _detail_of(self.sid)["items"] if u["verdict"] == "pending"]
        data = {}
        # 原样再提交上一次的改动（此时已是「改后」状态 → 不该再记一笔）
        for e in edits_before["edits"]:
            data["verdict_%s_%s" % (e["idx"], e["blank"])] = e["after_verdict"]
            data["text_%s_%s" % (e["idx"], e["blank"])] = e.get("after_text") or ""
        self.client.post("/submissions/%s/confirm" % self.sid, data=data)
        edits_after = self.client.get("/submissions/%s/edits" % self.sid).json()
        dump("edits_02_duplicate.json", {"before": len(edits_before["edits"]),
                                         "after": len(edits_after["edits"])})
        self.assertEqual(len(edits_before["edits"]), len(edits_after["edits"]),
                         "同一改动重复提交不该产生新记录")

    def test_04_csv_matches(self):
        r = self.client.get("/submissions/%s/edits.csv" % self.sid)
        self.assertEqual(200, r.status_code)
        text = r.content.decode("utf-8-sig")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        with open(os.path.join(EV, "edits.csv"), "wb") as f:
            f.write(r.content)
        edits = self.client.get("/submissions/%s/edits" % self.sid).json()["edits"]
        dump("edits_03_csv.json", {"csv_lines": len(lines), "data_rows": len(lines) - 1,
                                   "records": len(edits),
                                   "header": lines[0] if lines else ""})
        self.assertEqual(len(edits) + 1, len(lines), "CSV 行数应为 表头 + 记录数")

    def test_05_undo_restores_score_and_keeps_trail(self):
        r = self.client.post("/submissions/%s/edits/%s/undo" % (self.sid, self.batch))
        self.assertIn(r.status_code, (200, 303), "撤销应 303，实际 %s" % r.status_code)
        st = self.client.get("/submissions/%s/status" % self.sid).json()
        edits = self.client.get("/submissions/%s/edits" % self.sid).json()
        dump("edits_04_after_undo.json", {"status": st, "n_edits": len(edits["edits"]),
                                          "sources": [e.get("source") for e in edits["edits"]],
                                          "notes": [e.get("note") for e in edits["edits"]],
                                          "restored_score": st["score"],
                                          "baseline_score": self.status_before["score"]})
        self.assertEqual(self.status_before["score"], st["score"],
                         "撤销后分数应回到改前（%s → %s）"
                         % (self.status_after["score"], st["score"]))
        self.assertGreater(len(edits["edits"]), 2, "撤销本身也要留痕")
        self.assertTrue(any((e.get("source") == "undo") for e in edits["edits"]),
                        "应有 source=undo 的记录")
        page = self.client.get("/submissions/%s?undone=2" % self.sid)
        self.assertEqual(200, page.status_code)
        save_page(page.text, "04_grade_result_after_undo")
        shot(os.path.join(EV, "04_grade_result_after_undo.html"),
             "04_grade_result_after_undo")


# ---------------------------------------------------------------- ③ 学生信息
class T3StudentInfo(unittest.TestCase):
    """v3 卡涂学号 → 判卷写入 student_no → 页面显示；旧 v2 卡（无学生区）→ 空、不报错。"""

    STUDENT_NO = "20250317"

    @classmethod
    def setUpClass(cls):
        cls.app, cls.client = _client()

    def _paper_and_grade(self, title, with_student, seed):
        pid = _create_paper(self.client, title, mix=(6, 3, 2), seed=seed)
        man = _manifest_of(pid)
        card = man["card"]
        by_idx = {it["idx"]: it for it in man["items"]}
        choice = {}
        for page in card["pages"]:
            for it in page["items"]:
                if it["kind"] != "omr":
                    continue
                key = (by_idx[it["idx"]].get("answer_key") or [{}])[0]
                choice[it["idx"]] = str(key.get("value") or key.get("opt") or "")
        png = os.path.join(OUTD, pid, "card_1.png")
        filled = paint_card(png, card, choice,
                            student_no=self.STUDENT_NO if with_student else None)
        if with_student:
            shutil.copyfile(filled, os.path.join(EV, "05_v3_card_filled.png"))
        photo = synth_photo(filled, os.path.join(WORK, "stu_%s.jpg" % pid), seed=621)
        sid = _upload(self.client, pid, photo)
        return pid, sid, man

    def test_01_v3_card_writes_student_no(self):
        pid, sid, man = self._paper_and_grade("t61 学生信息卷（v3）", True, 20261031)
        st = self.client.get("/submissions/%s/status" % sid).json()
        row = _q("SELECT student_no, student_name, score, full FROM submissions WHERE sid=?",
                 (sid,))[0]
        detail = _detail_of(sid)
        page = self.client.get("/submissions/%s" % sid)
        records = self.client.get("/records")
        dump("student_01_v3.json", {
            "pid": pid, "sid": sid,
            "card_version": (man.get("card") or {}).get("version"),
            "has_student_area": bool((man.get("card") or {}).get("student")),
            "painted_student_no": self.STUDENT_NO,
            "db_student_no": row["student_no"],
            "detail_student": (detail.get("student") or {}),
            "status": st,
            "result_page_has_no": self.STUDENT_NO in page.text,
            "records_page_has_no": self.STUDENT_NO in records.text,
        })
        version = (man.get("card") or {}).get("version")
        # t84：v4 起版本号跟着 card.CARD_VERSION 走，这里只要求「是新版卡（>=3）」，
        # 重点在下一行的「学生信息区存在 + 学生号写进判卷记录」。
        self.assertIsNotNone(version, "manifest 里没有卡版本号")
        self.assertGreaterEqual(version, 3, "新卡应是 v3 或更高（当前 %s）" % version)
        self.assertEqual(card.CARD_VERSION, version,
                         "新卡的版本号应等于 card.CARD_VERSION（当前 %s）" % card.CARD_VERSION)
        self.assertTrue((man.get("card") or {}).get("student"), "v3 卡应有学生区几何")
        self.assertEqual(self.STUDENT_NO, row["student_no"],
                         "submissions.student_no 应是涂的学号：%r" % row["student_no"])
        self.assertEqual(self.STUDENT_NO, (detail.get("student") or {}).get("no"),
                         "detail_json.student.no 也应写上")
        self.assertEqual(200, page.status_code)
        save_page(page.text, "06_result_page_student")
        shot(os.path.join(EV, "06_result_page_student.html"), "06_result_page_student")
        self.assertIn(self.STUDENT_NO, page.text, "结果页应显示学号")
        self.assertEqual(200, records.status_code)
        save_page(records.text, "07_records_page_student")
        shot(os.path.join(EV, "07_records_page_student.html"), "07_records_page_student")
        self.assertIn(self.STUDENT_NO, records.text, "记录页应显示学号")

    def test_02_legacy_v2_card_still_grades(self):
        pid = _create_paper(self.client, "t61 旧卡兼容卷（v2）", mix=(4, 2, 2), seed=20261031)
        man = _manifest_of(pid)
        card = man["card"]
        # 旧 v2 卡没有学生区：把 manifest 里的 student 区去掉（这就是 v2 卡的形状）
        card.pop("student", None)
        card["version"] = 2
        conn = _conn()
        try:
            conn.execute("UPDATE papers SET manifest=? WHERE pid=?",
                         (json.dumps(man, ensure_ascii=False), pid))
            conn.commit()
        finally:
            conn.close()
        by_idx = {it["idx"]: it for it in man["items"]}
        choice = {}
        for page in card["pages"]:
            for it in page["items"]:
                if it["kind"] != "omr":
                    continue
                key = (by_idx[it["idx"]].get("answer_key") or [{}])[0]
                choice[it["idx"]] = str(key.get("value") or key.get("opt") or "")
        png = os.path.join(OUTD, pid, "card_1.png")
        filled = paint_card(png, card, choice)          # 不涂学号（v2 也没有学号区）
        photo = synth_photo(filled, os.path.join(WORK, "stu_legacy.jpg"), seed=622)
        sid = _upload(self.client, pid, photo)
        st = self.client.get("/submissions/%s/status" % sid).json()
        row = _q("SELECT student_no FROM submissions WHERE sid=?", (sid,))[0]
        dump("student_02_legacy.json", {"pid": pid, "sid": sid,
                                        "manifest_card_version": card["version"],
                                        "has_student_area": bool(card.get("student")),
                                        "student_no": row["student_no"], "status": st})
        self.assertTrue(st.get("warp_ok"), "旧卡也要能标定：%s" % st)
        self.assertEqual("", row["student_no"] or "", "旧卡没有学号 → 应为空串")
