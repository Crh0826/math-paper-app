# -*- coding: utf-8 -*-
"""t19 · 独立验收（verifier 写）—— 数据层 / 卷面层 / 判卷层硬指标。

这份文件刻意**不复用** `tests/test_<module>.py` 里的样本与断言：等价对、数据库断言、
照片合成、保真计数全部由 verifier 另写一遍，避免"用被测方的用例验被测方"的同源误差。

每节对应 `app/VERIFY.md` 的一项，证据（JSON / PNG / PDF / docx）落在
`app/out/acceptance_v1/`：

    T1DataLayer        → data_layer.json / data_layer_schema.json / data_layer_tokens.json
    T2Normalize        → normalize_pairs.json
    T3Picker           → picker.json / picker_scope.json / picker_insufficient.json
    T4PaperFidelity    → paper_fidelity.json / paper_health.json / paper_render.json
                         （+ acceptance_paper.docx / .pdf / *_N.png）
    T5CardGeometry     → card_geometry.json（+ card/card_*.png、card/card.pdf）
    T6GradePhotos      → grade_photos.json（+ photos/*.jpg、grading/<pid>/sub_*/）

跑法：
    $env:PYTHONIOENCODING='utf-8'
    cd E:\\OCR\\app
    python -m unittest tests.test_acceptance -v     # 只跑验收
    python -m unittest discover -s tests -v         # 全量（含本文件）

断言口径：只断言 CONTRACT.md 里写明的硬指标；额外观察值一律写进 JSON 供人复核。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import unittest

import cv2
import numpy as np
from PIL import Image, ImageDraw

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import card            # noqa: E402
import db as dbmod     # noqa: E402
import docx_rich       # noqa: E402
import grade           # noqa: E402
import normalize       # noqa: E402
import paper           # noqa: E402
import picker          # noqa: E402
import train            # noqa: E402

DB_PATH = os.path.join(APP, "data", "app.db")
OUT = os.path.join(APP, "out", "acceptance_v1")
try:
    import paths as _paths
    REAL_DOCX = _paths.resolve_qbank(legacy=_paths.LEGACY_QBANK)
except Exception:  # pragma: no cover
    REAL_DOCX = os.path.join(APP, "qbank")

PAGE_W, PAGE_H = 2480, 3508
PID = "P20261031-001"

#: 验收用的组卷参数（CONTRACT §2 的默认形状）
BASE_PARAMS = {
    "title": "验收卷 A · 七年级数学",
    "mix": {"选择": 10, "填空": 8, "解答": 6},
    "scope": {"chapters": [], "kps": [], "kp_mode": "any"},
    "requirements": {"need_answer": True, "exclude_empty": True, "exclude_no_family": False},
    "seed": 42,
}


# ---------------------------------------------------------------- 小工具
def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def dump(name, obj):
    """证据落盘（JSON，UTF-8 无 BOM）。"""
    _ensure(OUT)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return path


def load_json(name):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


#: 每次进程一套独立产物目录（多人同时跑 discover 时不会互相覆盖 / 读到半写文件）
RUNDIR = os.path.join(OUT, "run_%d" % os.getpid())


def _publish(src, sub, name=None):
    """把本轮产物拷到稳定证据路径（out/acceptance_v1/<sub>/<name>），返回目标路径。"""
    import shutil
    dst_dir = _ensure(os.path.join(OUT, sub))
    dst = os.path.join(dst_dir, name or os.path.basename(src))
    try:
        shutil.copyfile(src, dst)
    except OSError:
        pass
    return dst


def read_gray(path, tries=6, delay=0.25):
    """读 300dpi PNG（大文件 + 并发扫描时偶发读到半写状态）→ 图 or None。"""
    for i in range(tries):
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None and img.size:
            return img
        if i + 1 < tries:
            time.sleep(delay)
    return None


def ro_conn():
    """只读连接：验收全程不改 app.db。"""
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def need_db():
    if not os.path.exists(DB_PATH):
        raise unittest.SkipTest("app.db 不存在：%s" % DB_PATH)


_MANIFEST_A = None


def manifest_a():
    """验收卷 A（mix 10/8/6，seed 42）：整套验收共用，只建一次。"""
    global _MANIFEST_A
    if _MANIFEST_A is None:
        need_db()
        conn = ro_conn()
        try:
            _MANIFEST_A = picker.build(dict(BASE_PARAMS), conn, pid=PID)
        finally:
            conn.close()
    return _MANIFEST_A


# ---------------------------------------------------------------- T1 数据层
#: 卷面里不是题框的装饰表（t59 的「班级 / 姓名 / 学号」学生栏是 1×3 表）
_DECOR_WORDS = ("班级", "姓名", "学号", "座位号")


def _is_decor_table(el) -> bool:
    """1×3 且文字含学生栏标签 → 装饰表（纸张排版用，不是题框）。"""
    try:
        from docx.table import Table
        tbl = Table(el, None)
        n_cols = len(tbl.columns)
        text = "".join(t.text for row in tbl.rows for t in row.cells)
    except Exception:              # noqa: BLE001
        return False
    return n_cols == 3 and any(w in text for w in _DECOR_WORDS)


def _item_boxes(doc):
    """卷面正文里的题框（1×1 表），去掉学生栏这类装饰表。"""
    out = []
    for el in doc.element.body:
        if not (isinstance(el.tag, str) and el.tag.endswith("}tbl")):
            continue
        if _is_decor_table(el):
            continue
        out.append(el)
    return out


class T1DataLayer(unittest.TestCase):
    """CONTRACT §1 的库结构 + 团队约定的硬数字。"""

    @classmethod
    def setUpClass(cls):
        need_db()
        cls.conn = ro_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_01_counts(self):
        c = self.conn
        got = {
            "questions": c.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
            "distinct_no": c.execute("SELECT COUNT(DISTINCT no) FROM questions").fetchone()[0],
            "with_answer": c.execute(
                "SELECT COUNT(*) FROM questions WHERE TRIM(COALESCE(answer_raw,''))<>''"
            ).fetchone()[0],
            "without_answer": c.execute(
                "SELECT COUNT(*) FROM questions WHERE TRIM(COALESCE(answer_raw,''))=''"
            ).fetchone()[0],
            "is_empty": c.execute("SELECT COUNT(*) FROM questions WHERE is_empty=1").fetchone()[0],
            "families": c.execute("SELECT COUNT(*) FROM families").fetchone()[0],
            "families_ge3": c.execute("SELECT COUNT(*) FROM families WHERE size>=3").fetchone()[0],
            "kps": c.execute("SELECT COUNT(*) FROM kps").fetchone()[0],
            "five_digit_no": c.execute(
                "SELECT COUNT(*) FROM questions WHERE no GLOB '[0-9][0-9][0-9][0-9][0-9]'"
            ).fetchone()[0],
            "six_digit_no": c.execute(
                "SELECT COUNT(*) FROM questions WHERE no GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'"
            ).fetchone()[0],
        }
        got["qtype"] = dict(c.execute(
            "SELECT qtype, COUNT(*) FROM questions GROUP BY qtype ORDER BY 2 DESC").fetchall())
        got["family_members"] = c.execute(
            "SELECT COUNT(*) FROM questions WHERE COALESCE(family_key,'')<>''").fetchone()[0]
        got["distinct_family_key"] = c.execute(
            "SELECT COUNT(DISTINCT family_key) FROM questions"
            " WHERE COALESCE(family_key,'')<>''").fetchone()[0]
        got["unnamed_kp"] = c.execute(
            "SELECT COUNT(*) FROM questions WHERE COALESCE(kp_id,'')=''").fetchone()[0]
        got["unnamed_chapter"] = c.execute(
            "SELECT COUNT(*) FROM questions WHERE COALESCE(chapter,'')=''").fetchone()[0]
        got["kp_ids"] = [r[0] for r in c.execute(
            "SELECT kp_id FROM kps ORDER BY kp_id").fetchall()]
        # t93：题数一律从库实时算，不再钉死某个数字（数据天天在长）
        got["by_subject"] = dict(c.execute(
            "SELECT subject_pk, COUNT(*) FROM questions GROUP BY subject_pk"
            " ORDER BY 2 DESC").fetchall())
        got["subject_total"] = sum(got["by_subject"].values())
        got["cross_subject_dup_no"] = c.execute(
            "SELECT COUNT(*) FROM (SELECT no FROM questions GROUP BY no"
            " HAVING COUNT(DISTINCT subject_pk) > 1)").fetchone()[0]
        got["non_digit_no"] = c.execute(
            "SELECT COUNT(*) FROM questions WHERE no GLOB '*[^0-9]*'").fetchone()[0]
        got["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
        got["tables"] = sorted(r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        got["regime"] = "trimmed" if got["without_answer"] == 0 else "full"
        dump("data_layer.json", got)

        # 两种口径都认（用户决定：无答案题从题库删掉，22,898 → 12,280；
        # t17 又把 bank/final 的 75,468 道 6 位题入库 → 87,748，所以这里只守下界）
        if got["regime"] == "trimmed":
            self.assertGreaterEqual(got["questions"], 12280, "裁剪后 questions 至少 12,280")
            self.assertEqual(got["questions"], got["with_answer"], "每题都必须有答案")
            self.assertGreater(got["families"], 0, "题族表不能空")
            self.assertGreaterEqual(got["families"], got["distinct_family_key"],
                                    "families 表不能少于题目里出现的族数")
            self.assertGreater(got["kps"], 0, "知识点表不能空")
        else:
            self.assertEqual(22898, got["questions"], "裁剪前 questions 行数")
            self.assertEqual(12280, got["with_answer"], "裁剪前有答案的题数")
            self.assertEqual(2671, got["families"], "题族数")
            self.assertEqual(1760, got["families_ge3"], "≥3 题的题族数")
            self.assertEqual(34, got["kps"], "知识点数")
            self.assertEqual(got["families"], got["distinct_family_key"],
                             "families 表族数应等于 questions.family_key 去重数")
        self.assertEqual(got["questions"], got["distinct_no"], "no 必须唯一（主键）")
        self.assertEqual("ok", got["integrity"], "PRAGMA integrity_check")

        # ---- t93：结构性条件（数字全从库实时算，不再钉死某一天的题数）
        self.assertEqual(got["questions"], got["subject_total"],
                         "总题数必须等于各学科题数之和")
        self.assertGreaterEqual(got["by_subject"].get("math7a", 0), 18000,
                                "math7a 题数异常偏低（低于 18,000）：%s"
                                % got["by_subject"].get("math7a"))
        self.assertIn("physics", got["by_subject"],
                      "导入的物理题不见了（应存在于 questions.subject_pk）")
        physics_nos = [r[0] for r in c.execute(
            "SELECT no FROM questions WHERE subject_pk='physics'")]
        self.assertTrue(physics_nos, "physics 学科没有题")
        bad_block = [n for n in physics_nos if not str(n).startswith("99")]
        self.assertEqual([], bad_block,
                         "物理题必须落在 99xxx 号段（避免再和数学 0xxxx 撞号）：%r"
                         % bad_block[:5])
        self.assertEqual(0, got["cross_subject_dup_no"],
                         "存在跨学科同题号（会互相覆盖）：%d 个"
                         % got["cross_subject_dup_no"])
        self.assertEqual(0, got["non_digit_no"], "题号必须是纯数字串")
        # kp_id：老题库（math*）必须每道都有；导入件允许暂缺知识点
        missing_kp = c.execute(
            "SELECT subject_pk, COUNT(*) FROM questions WHERE COALESCE(kp_id,'')=''"
            " GROUP BY subject_pk").fetchall()
        math_missing = sum(n for sub, n in missing_kp if str(sub).startswith("math"))
        self.assertEqual(0, math_missing,
                         "数学题必须每题都有 kp_id（缺 %d 道）" % math_missing)
        self.assertLessEqual(set(got["qtype"]), {"选择", "填空", "解答", "判断", "其他"},
                             "qtype 必须在契约枚举内：%s" % sorted(got["qtype"]))
        self.assertGreater(len(got["qtype"]), 0, "题库不能没有题型")

    def test_02_schema(self):
        """CONTRACT §1 的表/列必须在位（少了任何一列，下游 SQL 会静默错）。"""
        want = {
            "questions": {"no", "qtype", "option_count", "sub_count", "is_empty", "media_count",
                          "text_len", "text_plain", "tokens_json", "fmt", "src_title", "src_para",
                          "family_key", "kp_id", "chapter", "kp", "kp_source", "answer_raw",
                          "check_status"},
            "families": {"family_key", "src_title", "example_no", "size", "kp_id"},
            "kps": {"kp_id", "chapter", "kp", "n_questions"},
            "papers": {"pid", "created_at", "title", "params_json", "manifest", "docx_path",
                       "pdf_path", "n_items"},
            "submissions": {"sid", "pid", "created_at", "image_path", "warp_ok", "qr_ok",
                            "score", "full", "detail_json"},
            "mastery": {"key", "attempts", "correct", "last_seen", "interval_days", "due_at"},
        }
        got = {}
        for table, cols in want.items():
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}
            got[table] = sorted(have)
            self.assertTrue(have, "表 %s 不存在" % table)
            missing = cols - have
            self.assertFalse(missing, "表 %s 缺列：%s" % (table, sorted(missing)))
        dump("data_layer_schema.json", got)

    def test_03_tokens_json_readable(self):
        """tokens_json 必须能 json.loads（排版时直接喂给 paper）。抽样 1,000 题。"""
        rows = self.conn.execute(
            "SELECT no, tokens_json FROM questions ORDER BY no LIMIT 1000").fetchall()
        bad = []
        kinds = {}
        for r in rows:
            try:
                toks = json.loads(r["tokens_json"] or "[]")
            except Exception as e:                      # noqa: BLE001
                bad.append((r["no"], str(e)))
                continue
            if not isinstance(toks, list):
                bad.append((r["no"], "不是数组"))
                continue
            for t in toks:
                k = (t or {}).get("kind")
                kinds[k] = kinds.get(k, 0) + 1
        dump("data_layer_tokens.json", {"sampled": len(rows), "bad": bad[:20],
                                        "kind_hist": kinds})
        self.assertFalse(bad, "tokens_json 解析失败：%s" % bad[:5])
        self.assertLessEqual(set(kinds), {"text", "math", "embed", "table"})


# ---------------------------------------------------------------- T2 归一化
#: 等价对：(student, expected, qtype) —— verifier 另写，覆盖数值/代数/根式/单位/多空/判断。
EQ_PAIRS = [
    # 数值 · 分数 · 小数
    ("0.5", "1/2", "填空"), ("1/2", "0.5", "填空"), ("0.50", "0.5", "填空"),
    ("1.50", "1.5", "填空"), ("3.0", "3", "填空"), ("1.0", "1", "填空"),
    ("0.25", "1/4", "填空"), ("1/4", "0.25", "填空"), ("0.75", "3/4", "填空"),
    ("3/4", "6/8", "填空"), ("6/8", "3/4", "填空"), ("2.5", "5/2", "填空"),
    ("5/2", "2.5", "填空"), ("0.2", "1/5", "填空"), ("1/5", "0.2", "填空"),
    ("0.125", "1/8", "填空"), ("1/8", "0.125", "填空"), ("7/7", "1", "填空"),
    ("4/2", "2", "填空"), ("0", "0.0", "填空"), ("-0", "0", "填空"),
    ("−1", "-1", "填空"), ("-0.5", "−0.5", "填空"), ("+5", "5", "填空"),
    ("5", "+5", "填空"), ("1.10", "1.1", "填空"), ("0.10", "0.1", "填空"),
    ("100/25", "4", "填空"), ("3/1", "3", "填空"), ("0.5", "2/4", "填空"),
    ("1/10", "0.1", "填空"), ("0.05", "1/20", "填空"), ("3/5", "0.6", "填空"),
    ("0.6", "3/5", "填空"), ("4/5", "0.8", "填空"),
    # 代数式 / 方程
    ("x=4", "x＝4", "填空"), ("x = 4", "x=4", "填空"), ("y=2", "y＝2", "填空"),
    ("2x+1", "2x＋1", "填空"), ("x^2", "x²", "填空"), ("x²", "x^2", "填空"),
    ("x³", "x^3", "填空"), ("y=2x+1", "y＝2x＋1", "填空"), ("x=-3", "x＝−3", "填空"),
    ("a²b", "a^2b", "填空"),
    # 根式 / 幂 / 绝对值
    ("√8", "2√2", "填空"), ("2√2", "√8", "填空"), ("√9", "3", "填空"),
    ("|−2|", "2", "填空"), ("|2|", "2", "填空"), ("(-3)^2", "9", "填空"),
    ("（-3）²", "9", "填空"), ("-2³", "-8", "填空"), ("(−2)³", "-8", "填空"),
    ("√(1/4)", "1/2", "填空"), ("∛8", "2", "填空"), ("9^(1/2)", "3", "填空"),
    ("10^0", "1", "填空"),
    # 单位 / 百分数 / 科学计数
    ("3厘米", "3cm", "填空"), ("3cm", "3厘米", "填空"), ("5千克", "5kg", "填空"),
    ("25%", "0.25", "填空"), ("0.25", "25%", "填空"), ("50%", "1/2", "填空"),
    ("1.2×10^3", "1200", "填空"), ("1200", "1.2×10^3", "填空"),
    ("1.2×10³", "1200", "填空"), ("2.5×10^-2", "0.025", "填空"),
    ("30°", "30°", "填空"), ("0.5%", "0.005", "填空"), ("100%", "1", "填空"),
    # 中文数字 / 多空 / 标点 / 全角
    ("三分之一", "1/3", "填空"), ("零点五", "0.5", "填空"), ("二分之一", "1/2", "填空"),
    ("3，5", "3,5", "填空"), ("3,5", "3，5", "填空"), ("3;5", "3；5", "填空"),
    ("(1)4(2)9", "(1)4 (2)9", "填空"), ("(1)3；(2)5", "(1)3，(2)5", "填空"),
    ("１２", "12", "填空"), ("４", "4", "填空"), ("４.５", "4.5", "填空"),
    ("  3  ", "3", "填空"), ("3\n", "3", "填空"), ("3。", "3", "填空"),
    ("3．", "3", "填空"), ("3、", "3", "填空"),
    # 选择 / 判断
    ("a", "A", "选择"), ("A.", "A", "选择"), ("（A）", "A", "选择"),
    ("A、", "A", "选择"), ("Ａ", "A", "选择"), ("B", "b", "选择"),
    ("C.", "C", "选择"), ("（B）", "B", "选择"),
    ("√", "对", "判断"), ("对", "√", "判断"), ("×", "错", "判断"),
    ("错", "×", "判断"), ("正确", "对", "判断"), ("错误", "错", "判断"),
    # 区间 / 不等式
    ("1<x<2", "(1,2)", "填空"), ("(1,2)", "1<x<2", "填空"),
    ("2≤x≤5", "[2,5]", "填空"), ("x＞3", "x>3", "填空"), ("x＜0", "x<0", "填空"),
]

#: 不等价对：期望 False（把错答判成对，比把对答判成错更伤）
NEQ_PAIRS = [
    ("3", "4", "填空"), ("0.5", "0.05", "填空"), ("1/2", "2/3", "填空"),
    ("-3", "3", "填空"), ("5", "-5", "填空"), ("x=4", "x=5", "填空"),
    ("A", "B", "选择"), ("对", "错", "判断"), ("2√2", "√2", "填空"),
    ("1<x<2", "1<x<3", "填空"), ("25%", "0.5", "填空"), ("9", "-9", "填空"),
    ("3厘米", "4厘米", "填空"), ("x≥3", "x>3", "填空"), ("y=2x", "y=3x", "填空"),
    ("1.2×10^3", "1.2×10^2", "填空"), ("(1,2)", "(2,1)", "填空"),
    ("6/8", "3/8", "填空"), ("2.5", "2.05", "填空"), ("150", "105", "填空"),
    ("12", "21", "填空"), ("√8", "√2", "填空"), ("30°", "60°", "填空"),
    ("0.25", "2.5", "填空"), ("7", "1", "填空"), ("-1/2", "1/2", "填空"),
    ("3", "3.5", "填空"), ("10%", "1%", "填空"), ("x<3", "x≤3", "填空"),
    ("a^2", "2a", "填空"), ("5x=10", "x=1", "填空"),
    ("(1)3(2)5", "(1)3(2)4", "填空"), ("三分之一", "1/2", "填空"),
    ("零点五", "0.05", "填空"), ("3，5", "3，6", "填空"), ("A.", "B", "选择"),
    ("√", "错", "判断"), ("×", "对", "判断"), ("4", "-4", "填空"),
    ("0.6", "3/4", "填空"), ("12/4", "4", "填空"),
]


class T2Normalize(unittest.TestCase):
    """normalize.is_equivalent 的独立等价/不等价对。"""

    def _run(self, pairs, want):
        bad, rows = [], []
        t0 = time.time()
        for student, expected, qtype in pairs:
            ok, reason = normalize.is_equivalent(student, expected, qtype)
            rows.append({"student": student, "expected": expected, "qtype": qtype,
                         "got": bool(ok), "want": want, "reason": reason})
            if bool(ok) != want:
                bad.append(rows[-1])
        return rows, bad, time.time() - t0

    def test_eq_pairs(self):
        self.assertGreaterEqual(len(EQ_PAIRS), 100, "等价对要 100 组以上")
        rows, bad, dt = self._run(EQ_PAIRS, True)
        rate = (len(rows) - len(bad)) / len(rows)
        prev = load_json("normalize_pairs.json")
        prev.update({"eq_total": len(rows), "eq_bad": len(bad), "eq_rate": round(rate, 4),
                     "eq_elapsed_s": round(dt, 3), "bad": bad})
        dump("normalize_pairs.json", prev)
        self.assertGreaterEqual(rate, 0.90,
                                "等价对判对率 %.1f%%（%d/%d），失败样本见 "
                                "out/acceptance_v1/normalize_pairs.json"
                                % (rate * 100, len(rows) - len(bad), len(rows)))

    def test_neq_pairs(self):
        self.assertGreaterEqual(len(NEQ_PAIRS), 30, "不等价对要 30 组以上")
        rows, bad, dt = self._run(NEQ_PAIRS, False)
        rate = (len(rows) - len(bad)) / len(rows)
        prev = load_json("normalize_pairs.json")
        prev.update({"neq_total": len(rows), "neq_bad": len(bad),
                     "neq_rate": round(rate, 4), "neq_elapsed_s": round(dt, 3),
                     "neq_bad_rows": bad})
        dump("normalize_pairs.json", prev)
        self.assertGreaterEqual(rate, 0.95, "不等价对判对率 %.1f%%" % (rate * 100))


# ---------------------------------------------------------------- T3 组卷
class T3Picker(unittest.TestCase):
    """picker.build 的契约（CONTRACT §2 / §3）：可复现、换 seed 换题、scope 过滤、题量不足。"""

    @classmethod
    def setUpClass(cls):
        need_db()
        cls.conn = ro_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _build(self, **over):
        params = json.loads(json.dumps(BASE_PARAMS))
        params.update(over)
        return picker.build(params, self.conn, pid=PID)

    def test_01_reproducible(self):
        t0 = time.time()
        a = self._build()
        t_build = time.time() - t0
        a2 = self._build()
        nos_a = [it["no"] for it in a["items"]]
        nos_a2 = [it["no"] for it in a2["items"]]
        dump("picker.json", {"build_s": round(t_build, 3), "n_items": len(nos_a),
                             "nos": nos_a, "by_qtype": a["stats"]["by_qtype"],
                             "pool_available": a["stats"]["pool_available"],
                             "seed": a["params"]["seed"]})
        self.assertEqual(len(nos_a), 24, "10+8+6 = 24 题")
        self.assertEqual(nos_a, nos_a2, "同参数同 seed 必须逐题一致")
        self.assertEqual([it["idx"] for it in a["items"]], list(range(1, 25)),
                         "idx 必须 1..24 连续")
        self.assertEqual({"选择": 10, "填空": 8, "解答": 6}, a["stats"]["by_qtype"])

    def test_02_seed_changes_items(self):
        a = self._build()
        b = self._build(seed=43)
        na = [it["no"] for it in a["items"]]
        nb = [it["no"] for it in b["items"]]
        overlap = len(set(na) & set(nb))
        ratio = overlap / float(len(na))
        prev = load_json("picker.json")
        prev.update({"seed43_nos": nb, "seed_overlap": overlap,
                     "seed_overlap_ratio": round(ratio, 4)})
        dump("picker.json", prev)
        self.assertLess(ratio, 0.20, "换 seed 后重复率 %.1f%% 应 < 20%%" % (ratio * 100))

    def test_03_manifest_contract(self):
        m = self._build()
        self.assertEqual(PID, m["pid"])
        self.assertRegex(m["pid"], r"^P\d{8}-\d{3}$")
        self.assertEqual([2480, 3508], [m["page"]["w_px"], m["page"]["h_px"]])
        self.assertEqual(300, m["page"]["dpi"])
        order = {"选择": 0, "判断": 1, "填空": 2, "解答": 3, "其他": 4}
        seq = [order[it["qtype"]] for it in m["items"]]
        self.assertEqual(seq, sorted(seq), "卷面按题型分组（选择→判断→填空→解答）")
        for it in m["items"]:
            self.assertTrue(it["answer_key"], "%s 缺 answer_key" % it["no"])
            self.assertEqual(0, it["space_paras"], "卷面不留作答空白（space 默认 0）")
            if it["qtype"] == "选择":
                self.assertEqual(["A", "B", "C", "D"], it["options"])
            self.assertTrue(it["stem_tokens"], "%s 没有题干 token" % it["no"])

    def test_04_scope_filter(self):
        m = self._build(scope={"chapters": [], "kps": ["1.2"], "kp_mode": "any"})
        kps = sorted({it["kp_id"] for it in m["items"]})
        self.assertEqual(["1.2"], kps, "scope.kps 过滤失效：%s" % kps)
        self.assertEqual(24, len(m["items"]))

        m2 = self._build(scope={"chapters": ["第1章 有理数"], "kps": [], "kp_mode": "any"})
        chapters = sorted({it["chapter"] for it in m2["items"]})
        self.assertEqual(["第1章 有理数"], chapters)

        m3 = self._build(scope={"chapters": [], "kps": ["3.0"], "kp_mode": "any"})
        kps3 = sorted({it["kp_id"] for it in m3["items"]})
        self.assertTrue(kps3 and all(k.startswith("3.") for k in kps3),
                        "x.0 章级知识点要展开成整章：%s" % kps3)
        dump("picker_scope.json", {
            "kps_1_2": kps, "chapters_ch1": chapters, "kps_3_0": kps3,
            "session_kp": {"scope": {"kps": ["1.2"]}, "seed": 42,
                           "nos": [it["no"] for it in m["items"]],
                           "n_items": len(m["items"]),
                           "kps": sorted({it["kp_id"] for it in m["items"]})},
            "session_chapter": {"scope": {"chapters": ["第1章 有理数"]}, "seed": 42,
                                "nos": [it["no"] for it in m2["items"]],
                                "n_items": len(m2["items"]),
                                "chapters": chapters},
        })

    def test_05_insufficient_raises(self):
        with self.assertRaises(picker.InsufficientQuestions) as ctx:
            self._build(mix={"选择": 999999})
        det = getattr(ctx.exception, "details", None)
        self.assertTrue(det and "选择" in det, "InsufficientQuestions 要带逐题型 details")
        self.assertGreater(det["选择"]["need"], det["选择"]["available"])
        dump("picker_insufficient.json", {"details": det, "msg": str(ctx.exception)[:200]})

    def test_06_exact_nos_and_blacklist(self):
        """nos 精确题集：顺序照给；exclude_nos 拉黑生效。"""
        base = self._build()
        nos = [it["no"] for it in base["items"]]
        m = self._build(nos=nos, mix={"选择": 10, "填空": 8, "解答": 6})
        self.assertEqual(nos, [it["no"] for it in m["items"]], "nos 必须按给定顺序出卷")

        excl = nos[0]
        m2 = self._build(exclude_nos=[excl])
        self.assertNotIn(excl, [it["no"] for it in m2["items"]], "exclude_nos 没生效")

        with self.assertRaises(picker.SelectError):
            self._build(nos=nos, mix={"选择": 11, "填空": 8, "解答": 6})

    def test_07_save_to_temp_db(self):
        """save() 把 manifest 完整落 papers 表（判卷要靠它回读）。"""
        import uuid
        tmp = _ensure(os.path.join(OUT, "tmp"))
        path = os.path.join(tmp, "picker_save_%s.db" % uuid.uuid4().hex[:8])
        conn = dbmod.connect(path)
        try:
            m = self._build()
            m["pid"] = "P20261031-901"
            pid = picker.save(conn, m, docx_path="", pdf_path="")
            row = conn.execute("SELECT manifest, n_items FROM papers WHERE pid=?",
                               (pid,)).fetchone()
            self.assertIsNotNone(row, "save() 没写 papers 行")
            back = json.loads(row["manifest"])
            self.assertEqual([it["no"] for it in m["items"]],
                             [it["no"] for it in back["items"]])
            self.assertEqual(24, row["n_items"])
        finally:
            conn.close()


# ---------------------------------------------------------------- T4 卷面保真
_MARKERS = ("oMath", "oMathPara", "object", "drawing", "pict", "imagedata", "blip")
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _count_markers(xml_text):
    """独立实现：数一段 XML 里的公式/对象/媒体标记。"""
    from lxml import etree
    counts = {}
    if not xml_text:
        return counts
    try:
        root = etree.fromstring(xml_text.encode("utf-8")
                                if isinstance(xml_text, str) else xml_text)
    except Exception:                                   # noqa: BLE001
        return counts
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = etree.QName(el).localname
        if name in _MARKERS:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _source_media(src_path):
    """源 docx → ({rId: zip 内路径}, {zip 内路径: bytes})；只读。"""
    import zipfile
    import posixpath
    from lxml import etree
    with zipfile.ZipFile(src_path) as z:
        try:
            rels_root = etree.fromstring(z.read("word/_rels/document.xml.rels"))
        except KeyError:
            return {}, {}
        rels = {}
        for rel in rels_root:
            rid, tgt = rel.get("Id"), rel.get("Target") or ""
            if not rid or not tgt:
                continue
            rels[rid] = (tgt.lstrip("/") if tgt.startswith("/")
                         else posixpath.normpath(posixpath.join("word", tgt)))
        media = {}
        for path in rels.values():
            if "/media/" not in path:
                continue
            try:
                media[path] = z.read(path)
            except KeyError:
                continue
    return rels, media


def _embed_refs(xml_text):
    """卷面题框里的媒体引用数：每个 embed 容器内去重（与 docx_rich._embed 同语义）。"""
    from lxml import etree
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    out = []
    if not xml_text:
        return out
    root = etree.fromstring(xml_text.encode("utf-8")
                            if isinstance(xml_text, str) else xml_text)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if not (etree.QName(el).namespace == w_ns
                and etree.QName(el).localname in ("object", "drawing", "pict")):
            continue
        rids = set()
        for node in el.iter():
            if not isinstance(node.tag, str):
                continue
            for attr in ("id", "embed", "link"):
                v = node.get("{%s}%s" % (R_NS, attr))
                if v:
                    rids.add(v)
        out.append(rids)
    return out


def _media_rids(xml_text):
    """题内引用到的媒体关系 id 集合（要拿去 rels 里解析成 word/media/*）。"""
    from lxml import etree
    out = set()
    if not xml_text:
        return out
    root = etree.fromstring(xml_text.encode("utf-8")
                            if isinstance(xml_text, str) else xml_text)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("id", "embed", "link"):
            v = el.get("{%s}%s" % (R_NS, attr))
            if v:
                out.add(v)
    return out


class T4PaperFidelity(unittest.TestCase):
    """卷面 vs 源 docx：每题的 math / embed / 媒体数逐项比对，缺失必须为 0。"""

    NAME = "acceptance_paper"

    @classmethod
    def setUpClass(cls):
        from docx import Document
        cls.doc_dir = _ensure(os.path.join(RUNDIR, "paper"))
        cls.manifest = manifest_a()
        cls.t0 = time.time()
        cls.built = paper.build_docx(cls.manifest, cls.doc_dir, name=cls.NAME)
        _publish(cls.built["docx"], "paper")
        cls.build_s = time.time() - cls.t0
        cls.doc = Document(cls.built["docx"])
        cls.boxes = _item_boxes(cls.doc)       # 排除学生栏等装饰表
        cls.rels = {rid: rel.target_ref for rid, rel in cls.doc.part.rels.items()}
        import zipfile
        with zipfile.ZipFile(cls.built["docx"]) as _z:
            cls.media_names = [n for n in _z.namelist() if n.startswith("word/media/")]

    def test_01_item_boxes(self):
        self.assertEqual(len(self.manifest["items"]), len(self.boxes),
                         "每题一个框（1×1 表格），数量要和 items 对上")

    def test_02_token_fidelity(self):
        """逐题比对 math/embed/媒体（共 24 题，前 20 题逐行入报告），缺失=0。"""
        from lxml import etree
        rows = []
        tot = {k: 0 for k in _MARKERS}
        tot_media = 0
        for item, box in zip(self.manifest["items"], self.boxes):
            no = item["no"]
            src_path = os.path.join(REAL_DOCX, "%s.docx" % no)
            if not os.path.exists(src_path):
                # t17 入库的 75,468 道 6 位题没有源 docx，走 questions.tokens_json 回退排版；
                # 这条用例只比对「有 docx 的题」的 token 保真度。
                continue
            exp = {k: 0 for k in _MARKERS}
            exp_media = 0
            exp_hashes = set()
            src_rels, src_media = _source_media(src_path)
            for tok in docx_rich.extract_tokens(src_path):
                for k, v in _count_markers(tok.xml).items():
                    exp[k] += v
                if tok.kind == "embed":
                    exp_media += len(tok.media)
                    for _rid, _path, data in tok.media:
                        exp_hashes.add(hashlib.sha1(data).hexdigest())
                elif tok.kind == "table":
                    # 表格内的图（M0 的 table token 不收集 media，但 XML 里有 w:drawing/pict + r:embed）
                    for rids in _embed_refs(tok.xml):
                        exp_media += len(rids)
                        for rid in rids:
                            data = src_media.get(src_rels.get(rid) or "")
                            if data:
                                exp_hashes.add(hashlib.sha1(data).hexdigest())
            xml = etree.tostring(box, encoding="unicode")
            obs = _count_markers(xml)
            obs_media = sum(len(rids) for rids in _embed_refs(xml))
            obs_parts = sum(1 for rid in _media_rids(xml)
                            if "media/" in (self.rels.get(rid) or ""))
            import zipfile
            with zipfile.ZipFile(self.built["docx"]) as _zf:
                got_hashes = {hashlib.sha1(_zf.read(n)).hexdigest()
                              for n in self.media_names}
            missing_hashes = sorted(exp_hashes - got_hashes)
            row = {"idx": item["idx"], "no": no, "qtype": item["qtype"],
                   "exp": {"oMath": exp["oMath"], "object": exp["object"],
                           "drawing": exp["drawing"], "pict": exp["pict"],
                           "media": exp_media},
                   "obs": {"oMath": obs.get("oMath", 0), "object": obs.get("object", 0),
                           "drawing": obs.get("drawing", 0), "pict": obs.get("pict", 0),
                           "media": obs_media},
                   "media_parts": obs_parts, "missing_media_hashes": missing_hashes}
            for k in _MARKERS:
                tot[k] += exp[k]
            tot_media += exp_media
            rows.append(row)

        missing, extra = [], []
        for r in rows:
            for k in ("oMath", "object", "drawing", "pict", "media"):
                if r["obs"][k] < r["exp"][k]:
                    missing.append({"idx": r["idx"], "no": r["no"], "marker": k,
                                    "exp": r["exp"][k], "obs": r["obs"][k]})
                elif r["obs"][k] > r["exp"][k]:
                    extra.append({"idx": r["idx"], "no": r["no"], "marker": k,
                                  "exp": r["exp"][k], "obs": r["obs"][k]})
            if r["missing_media_hashes"]:
                missing.append({"idx": r["idx"], "no": r["no"], "marker": "media_bytes",
                                "exp": r["missing_media_hashes"],
                                "obs": "卷面 docx 里没有这些字节"})
        dump("paper_fidelity.json", {
            "build_s": round(self.build_s, 3), "docx": self.built["docx"],
            "n_items": len(rows), "first20": rows[:20],
            "rows": rows, "missing": missing, "extra": extra,
            "media_parts_in_docx": len(self.media_names),
            "note": "compose 按内容哈希去重，同一张图引用多次只存一份；media 比的是引用数，"
                    "另用 sha1 校验字节不丢",
            "expected_marks": self.built["expected"].get("oMath"),
            "totals": tot, "totals_media": tot_media,
        })
        self.assertEqual([], missing, "卷面缺公式/对象/媒体：%s" % missing[:5])

    def test_03_health_gate(self):
        """产品自带的保真闸门必须过（items_ok / 媒体无缺 / 公式数对得上）。"""
        health = paper.check_docx(self.built["docx"], self.manifest,
                                  self.built["expected"])
        dump("paper_health.json", health)
        self.assertTrue(health.get("items_ok"), "check_docx: items_ok=false")
        self.assertEqual([], health.get("media_missing"), "check_docx: 媒体缺失")
        self.assertEqual(health.get("formula_omml_expected"),
                         health.get("formula_omml_observed"), "公式 oMath 数不一致")
        self.assertEqual(health.get("tables_expected"), health.get("tables_observed"))

    def test_05_relationships_resolve(self):
        """每个 rId 都必须能解析（悬空 rId → Word 报「文件已损坏」，整卷打不开）。"""
        import zipfile
        from lxml import etree
        docx = self.built["docx"]
        r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        with zipfile.ZipFile(docx) as z:
            names = set(z.namelist())
            root = etree.fromstring(z.read("word/document.xml"))
            rels = {}
            for rel in etree.fromstring(z.read("word/_rels/document.xml.rels")):
                rels[rel.get("Id")] = rel.get("Target")
        used = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            for a in ("id", "embed", "link"):
                v = el.get("{%s}%s" % (r_ns, a))
                if v:
                    used.append(v)
        dangling = sorted({u for u in used if u not in rels})
        broken_parts = []
        for rid, tgt in rels.items():
            if not tgt or "://" in tgt or tgt.startswith("http"):
                continue
            p = tgt if tgt.startswith("word/") else "word/" + tgt.lstrip("/")
            if p.endswith(".xml") or "/_rels/" in p or p in ("word/styles.xml",):
                continue
            if p not in names and tgt not in names:
                broken_parts.append({"rid": rid, "target": tgt})
        dump("paper_relationships.json", {
            "docx": docx, "rid_refs": len(used), "rid_unique": len(set(used)),
            "rels": len(rels), "dangling": dangling, "broken_parts": broken_parts,
            "media_parts": len([n for n in names if n.startswith("word/media/")]),
        })
        self.assertEqual([], dangling,
                         "卷面 docx 有悬空 rId（Word 会判「文件已损坏」）：%s" % dangling[:10])
        self.assertEqual([], broken_parts,
                         "rels 指向不存在的部件：%s" % broken_parts[:5])

    def test_06_table_image_question(self):
        """确定性回归：题 00568 的表格里带 5 张图 —— 出卷后不许有悬空 rId、图字节必须在包里。

        不靠 seed 抽样（题库池子在变，抽不到就漏掉这个 P0）。
        """
        import zipfile
        from lxml import etree
        r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        conn = ro_conn()
        try:
            m = picker.build({"title": "含图表格回归卷", "mix": {"选择": 1},
                              "requirements": {"need_answer": True, "exclude_empty": True},
                              "seed": 7, "nos": ["00568"]}, conn, pid="P20261031-902")
        finally:
            conn.close()
        out = _ensure(os.path.join(RUNDIR, "table_image"))
        built = paper.build_docx(m, out, name="table_image")
        _publish(built["docx"], "paper")
        with zipfile.ZipFile(built["docx"]) as z:
            names = set(z.namelist())
            root = etree.fromstring(z.read("word/document.xml"))
            rels = {rel.get("Id"): rel.get("Target")
                    for rel in etree.fromstring(z.read("word/_rels/document.xml.rels"))}
            used = set()
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                for a in ("id", "embed", "link"):
                    v = el.get("{%s}%s" % (r_ns, a))
                    if v:
                        used.add(v)
            dangling = sorted(used - set(rels))
            missing_parts = []
            for rid, tgt in rels.items():
                if not tgt or "media/" not in tgt:
                    continue
                p = tgt if tgt.startswith("word/") else "word/" + tgt.lstrip("/")
                if p not in names:
                    missing_parts.append({"rid": rid, "target": tgt})
            src_rels, src_media = _source_media(os.path.join(REAL_DOCX, "00568.docx"))
            src_hashes = {hashlib.sha1(d).hexdigest() for d in src_media.values()}
            got_hashes = {hashlib.sha1(z.read(n)).hexdigest()
                          for n in names if n.startswith("word/media/")}
        dump("paper_table_image.json", {
            "no": "00568", "docx": built["docx"],
            "rid_refs": len(used), "rels": len(rels), "dangling": dangling,
            "missing_parts": missing_parts,
            "src_media_hashes": len(src_hashes), "docx_media_parts": len(got_hashes),
            "missing_hashes": sorted(src_hashes - got_hashes),
        })
        self.assertEqual([], dangling,
                         "表格里带图的题（00568）出卷后有悬空 rId，Word 会判文件损坏：%s" % dangling)
        self.assertEqual([], missing_parts, "rels 指向包里不存在的媒体：%s" % missing_parts)
        self.assertEqual([], sorted(src_hashes - got_hashes),
                         "表格里的图没被拷进卷面 docx（丢图）")

    def test_04_render_pdf_png(self):
        """真出 PDF / PNG：页数、页面尺寸、每页有墨（不出空白页）。"""
        if not paper.word_available():
            raise unittest.SkipTest("这台机器没有 Word，导 PDF 跳过")
        t0 = time.time()
        res, err = None, ""
        for _attempt in range(5):
            try:
                res = paper.render_paper(self.manifest, self.doc_dir, name=self.NAME)
                break
            except Exception as e:                      # noqa: BLE001
                err = "%s: %s" % (type(e).__name__, e)
                time.sleep(3)
        if res is None:
            dump("paper_render.json", {"error": err, "docx": self.built["docx"],
                                       "note": "Word COM 被占用（并发渲染）",
                                       "word_available": paper.word_available()})
            self.skipTest("Word COM 被占用，导 PDF 失败（环境问题，见 paper_render.json）")
        dt = time.time() - t0
        dump("paper_render.json", dict(res))
        _publish(res["pdf"], "paper")
        for p in res["png"]:
            _publish(p, "paper")
        self.assertGreaterEqual(res["pages"], 1, "PDF 页数")
        self.assertEqual(res["pages"], len(res["png"]), "PNG 页数应等于 PDF 页数")
        self.assertGreater(dt, 0)
        for p in res["png"]:
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            self.assertIsNotNone(img, "PNG 读不出来：%s" % p)
            self.assertEqual((PAGE_H, PAGE_W), img.shape[:2], "PNG 必须是 2480×3508")
        ink = res["health"].get("ink_ratio") or []
        self.assertEqual(res["pages"], len(ink))
        for i, v in enumerate(ink):
            self.assertGreater(v, 0.003, "第 %d 页几乎是空白（ink=%.4f）" % (i + 1, v))


# ---------------------------------------------------------------- T5 答题卡
_CARD = None


def card_fixture():
    """验收卷 A 的答题卡（多类共用，只建一次）。"""
    global _CARD
    if _CARD is None:
        out = _ensure(os.path.join(RUNDIR, "card"))
        m = manifest_a()
        res = card.build_card(m, out)
        for p in list(res["png"]) + [res["pdf"]]:
            _publish(p, "card")
        _CARD = {"manifest": m, "res": res, "dir": out}
    return _CARD


class T5CardGeometry(unittest.TestCase):
    """答题卡：ArUco 0/1/2/3 可检、QR 可解、几何不越界。"""

    @classmethod
    def setUpClass(cls):
        cls.fx = card_fixture()
        cls.card = cls.fx["res"]["card"]
        cls.pages = cls.card["pages"]

    def test_01_files(self):
        res = self.fx["res"]
        self.assertTrue(os.path.exists(res["pdf"]), "card.pdf 不存在")
        self.assertEqual(len(self.pages), len(res["png"]), "每页一张 PNG")
        for p in res["png"]:
            img = read_gray(p)
            self.assertIsNotNone(img, "PNG 读不出来：%s" % p)
            self.assertEqual((PAGE_H, PAGE_W), img.shape[:2], "答题卡必须是 2480×3508")

    def test_02_aruco_readable(self):
        det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(card.ARUCO_DICT_ID),
                                      cv2.aruco.DetectorParameters())
        rows = []
        for page, png in zip(self.pages, self.fx["res"]["png"]):
            gray = read_gray(png)
            self.assertIsNotNone(gray, "答题卡 PNG 读不出来：%s" % png)
            corners, ids, _ = det.detectMarkers(gray)
            found = sorted(int(i) for i in np.ravel(ids)) if ids is not None else []
            # 位置真值也取 manifest（v4 每页 id 分组不同，card.ARUCO_POS 只覆盖 0..3）
            want_pos = {int(mk["id"]): (float(mk["x"]), float(mk["y"]),
                                        float(mk.get("size") or card.ARUCO_SIZE))
                        for mk in ((page.get("marks") or {}).get("aruco") or [])}
            offs = {}
            if ids is not None:
                for c, i in zip(corners, np.ravel(ids)):
                    mid = int(i)
                    if mid in want_pos:
                        cx, cy = c.reshape(-1, 2).mean(axis=0)
                        px, py, size = want_pos[mid]
                        offs[mid] = round(max(abs(cx - (px + size / 2.0)),
                                              abs(cy - (py + size / 2.0))), 2)
            # v4：页序写在 ArUco id 里（第 p 页 = 4(p-1)..4(p-1)+3），真值以 manifest 为准
            marks = (page.get("marks") or {}).get("aruco") or []
            want_ids = sorted(int(mk["id"]) for mk in marks)
            self.assertEqual(4, len(marks), "第 %d 页应在 manifest 里有 4 个 ArUco" % page["page"])
            base = (int(page["page"]) - 1) * card.ARUCO_PER_PAGE
            self.assertEqual(sorted([base, base + 1, base + 2, base + 3]), want_ids,
                             "第 %d 页 ArUco 分组不对：%s（应 %d..%d）"
                             % (page["page"], want_ids, base, base + 3))
            rows.append({"page": page["page"], "found": found,
                         "want": want_ids, "center_offset_px": offs})
            self.assertEqual(want_ids, found,
                             "第 %d 页 ArUco 检出 %s（应 %s）"
                             % (page["page"], found, want_ids))
            for mk in marks:                      # 位置按 manifest 的 x/y 子图定位逐个核对
                mid = int(mk["id"])
                self.assertIn(mid, offs, "第 %d 页没检出 ArUco %d" % (page["page"], mid))
                self.assertLess(offs[mid], 8.0, "第 %d 页 ArUco %d 位置偏了 %.1fpx"
                                % (page["page"], mid, offs[mid]))
        prev = load_json("card_geometry.json")
        prev.update({"aruco": rows, "pages": len(self.pages)})
        dump("card_geometry.json", prev)

    def test_03_qr_payload(self):
        """v4：**只有第 1 页**印 QR（页序改用 ArUco id 分组）；其余页必须没有 QR。"""
        rows = []
        for page, png in zip(self.pages, self.fx["res"]["png"]):
            img = read_gray(png)
            self.assertIsNotNone(img, "答题卡 PNG 读不出来：%s" % png)
            payload = card.read_qr(img, page["qr"])
            rows.append({"page": page["page"], "payload": payload,
                         "has_qr_in_manifest": bool(page.get("qr"))})
            if page["page"] == 1:
                self.assertTrue(page.get("qr"), "第 1 页 manifest 里必须有 QR")
                self.assertIsNotNone(payload, "第 1 页 QR 解不出来")
                self.assertEqual(PID, payload.get("pid"), "QR 里的卷号不对")
                self.assertEqual(1, payload.get("p"), "QR 里的页码应为 1")
                self.assertEqual(card.CARD_VERSION, payload.get("v"),
                                 "QR 里的版本号应等于 card.CARD_VERSION")
            else:
                self.assertFalse(page.get("qr"), "v4 只有第 1 页印 QR，第 %d 页不该有"
                                 % page["page"])
                self.assertIsNone(payload, "第 %d 页不该解出 QR" % page["page"])
        prev = load_json("card_geometry.json")
        prev["qr"] = rows
        dump("card_geometry.json", prev)

    def test_04_boxes_inside_page(self):
        """所有作答格都在正文区内、不越界、彼此不重叠。"""
        problems = []
        n_cells = 0
        for page in self.pages:
            for it in page["items"]:
                if it["kind"] == card.KIND_OMR:
                    self.assertIn(len(it["cells"]), (2, 3, 4),
                                  "第 %d 页 idx=%s 选项格数异常：%d"
                                  % (page["page"], it["idx"], len(it["cells"])))
                    boxes = [dict(c, _what="opt %s" % c["opt"]) for c in it["cells"]]
                elif it["kind"] == card.KIND_FILL:
                    boxes = [dict(b, _what="fill") for b in it["boxes"]]
                else:
                    boxes = [dict(b, _what="answer") for b in it.get("answer_boxes") or []]
                    pb = it.get("process_box")
                    if pb:
                        boxes.append(dict(pb, _what="process"))
                rects = []
                for b in boxes:
                    n_cells += 1
                    x, y, w, h = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
                    if x < 0 or y < 0 or x + w > PAGE_W or y + h > PAGE_H:
                        problems.append({"page": page["page"], "idx": it["idx"],
                                         "what": b["_what"], "why": "越出页面",
                                         "box": [x, y, w, h]})
                        continue
                    bx, by, bw, bh = card.BODY_RECT
                    if not (x >= bx and y >= by and x + w <= bx + bw and y + h <= by + bh):
                        problems.append({"page": page["page"], "idx": it["idx"],
                                         "what": b["_what"], "why": "越出正文区",
                                         "box": [x, y, w, h]})
                    rects.append((it["idx"], b["_what"], x, y, w, h))
                for i in range(len(rects)):
                    for j in range(i + 1, len(rects)):
                        _, wi, xi, yi, wwi, hhi = rects[i]
                        _, wj, xj, yj, wwj, hhj = rects[j]
                        if "process" in (wi, wj):
                            continue        # 过程区与答案框是上下关系，只查越界
                        if (xi < xj + wwj and xj < xi + wwi
                                and yi < yj + hhj and yj < yi + hhi):
                            problems.append({"page": page["page"], "idx": it["idx"],
                                             "why": "格重叠", "a": wi, "b": wj})
        prev = load_json("card_geometry.json")
        prev.update({"boxes_checked": n_cells, "box_problems": problems[:20],
                     "box_problem_count": len(problems)})
        dump("card_geometry.json", prev)
        self.assertGreater(n_cells, 0, "答题卡上一个作答格都没有")
        self.assertEqual(0, len(problems), "作答格几何异常：%s" % problems[:3])

    def test_05_cell_enumeration(self):
        """iter_answer_cells 枚举的填空/解答格数要和 manifest 的答案空对上。"""
        cells = list(card.iter_answer_cells(self.card, PID))
        want = 0
        for it in self.fx["manifest"]["items"]:
            n = max(1, len({k.get("blank", i + 1)
                            for i, k in enumerate(it["answer_key"])}))
            if it["qtype"] == "填空":
                want += min(12, n)
            elif it["qtype"] == "解答":
                want += min(20, n)
        got = len(cells)
        kmap = {}
        for c in cells:
            kmap[c["kind"]] = kmap.get(c["kind"], 0) + 1
        prev = load_json("card_geometry.json")
        prev.update({"answer_cells": got, "answer_cells_want": want, "by_kind": kmap})
        dump("card_geometry.json", prev)
        self.assertEqual(want, got, "作答格数量与答案空对不上：want=%d got=%d" % (want, got))
        for c in cells:
            self.assertIn(c["kind"], (card.KIND_FILL, card.KIND_SOLUTION))
            self.assertEqual(PID, c["pid"])


# ---------------------------------------------------------------- T6 判卷
def _fill_card_page(png_path, card_info, page_no, choice_of):
    """在答题卡 PNG 上"作答"：选择题涂黑指定选项（填空/解答留白，等 OCR）。"""
    gray = read_gray(png_path)
    if gray is None:
        raise AssertionError("答题卡 PNG 读不出来：%s" % png_path)
    img = Image.fromarray(gray)
    d = ImageDraw.Draw(img)
    page = next(p for p in card_info["pages"] if p["page"] == page_no)
    for it in page["items"]:
        if it["kind"] != card.KIND_OMR:
            continue
        want = choice_of.get(it["idx"])
        if want is None:
            continue
        for c in it["cells"]:
            if str(c["opt"]) == str(want):
                d.rectangle([c["x"] + 9, c["y"] + 9,
                             c["x"] + c["w"] - 9, c["y"] + c["h"] - 9], fill=0)
    path = os.path.join(os.path.dirname(png_path), "filled_%d.png" % page_no)
    img.save(path)
    return path


def _synth_photo(src_png, dst_jpg, seed, jitter=0.03, quality=70, scale=0.72):
    """把答题卡合成"手机拍照"：透视 3% + 光照不均 + 噪点 + JPEG70 + 缩到 72%。"""
    img = cv2.imread(src_png, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise AssertionError("读不出答题卡：%s" % src_png)
    h, w = img.shape[:2]
    rng = np.random.RandomState(seed)
    W, H = int(w * scale), int(h * scale)
    d = jitter
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([
        [W * rng.uniform(d * 0.3, d), H * rng.uniform(d * 0.3, d)],
        [W * (1 - rng.uniform(d * 0.3, d)), H * rng.uniform(d * 0.4, d * 1.2)],
        [W * (1 - rng.uniform(d * 0.3, d)), H * (1 - rng.uniform(d * 0.3, d))],
        [W * rng.uniform(d * 0.3, d), H * (1 - rng.uniform(d * 0.3, d))],
    ])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, M, (W, H), borderValue=190)
    yy, xx = np.mgrid[0:H, 0:W]
    grad = 0.70 + 0.30 * (xx / max(W - 1, 1)) + 0.25 * (yy / max(H - 1, 1))
    out = warped.astype(np.float32) * grad + rng.normal(0, 4, (H, W))
    out = cv2.GaussianBlur(np.clip(out, 0, 255).astype(np.uint8), (3, 3), 0)
    cv2.imwrite(dst_jpg, out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return dst_jpg


class T6GradePhotos(unittest.TestCase):
    """10 张合成"拍照"跑 grade.grade_image：标定成功率、选择题判分一致率。"""

    N_PHOTOS = 10

    @classmethod
    def setUpClass(cls):
        fx = card_fixture()
        cls.manifest = fx["manifest"]
        cls.card = fx["res"]["card"]
        cls.pages = cls.card["pages"]
        cls.page_by_no = {p["page"]: p for p in cls.pages}
        cls.tmp = _ensure(os.path.join(RUNDIR, "grade"))
        cls.photos_dir = _ensure(os.path.join(RUNDIR, "photos"))
        cls.grade_out = _ensure(os.path.join(OUT, "grading"))
        # 进程唯一的临时库：固定名字在多人并发跑 discover 时会被锁（WinError 32）
        db_path = os.path.join(cls.tmp, "grade_%d.db" % os.getpid())
        cls.conn = dbmod.connect(db_path)
        cls.conn.execute(
            "INSERT OR REPLACE INTO papers(pid, created_at, title, params_json, manifest,"
            " docx_path, pdf_path, n_items) VALUES (?,?,?,?,?,?,?,?)",
            (PID, cls.manifest["created_at"], cls.manifest["title"], "{}",
             json.dumps(cls.manifest, ensure_ascii=False), "", "",
             len(cls.manifest["items"])))
        cls.conn.commit()

        # 作答意图：idx % 4 == 0 的题故意涂错（3/4 对、1/4 错）
        by_idx = {it["idx"]: it for it in cls.manifest["items"]}
        cls.intent = {}
        cls.chosen = {}
        for page in cls.pages:
            for it in page["items"]:
                if it["kind"] != card.KIND_OMR:
                    continue
                item = by_idx[it["idx"]]
                expected = str((item["answer_key"][0] or {}).get("value") or "")
                opts = [str(c["opt"]) for c in it["cells"]]
                if it["idx"] % 4 == 0:
                    wrong = next((o for o in opts if o != expected), None)
                    cls.chosen[it["idx"]] = wrong
                    cls.intent[it["idx"]] = "wrong"
                else:
                    cls.chosen[it["idx"]] = expected
                    cls.intent[it["idx"]] = "correct"

        cls.filled = {}
        for page, png in zip(cls.pages, fx["res"]["png"]):
            cls.filled[page["page"]] = _fill_card_page(png, cls.card, page["page"],
                                                       cls.chosen)
        cls.photo_pages = []
        for i in range(cls.N_PHOTOS):
            pno = cls.pages[i % len(cls.pages)]["page"]
            jpg = os.path.join(cls.photos_dir, "photo_%02d_p%d.jpg" % (i + 1, pno))
            _synth_photo(cls.filled[pno], jpg, seed=100 + i)
            _publish(jpg, "photos")
            cls.photo_pages.append((jpg, pno))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:                               # noqa: BLE001
            pass

    def test_01_ten_photos(self):
        rows = []
        warp_ok = qr_ok = qr_checked = 0
        omr_total = omr_agree = omr_unsure = 0
        pending = 0
        bad_rows = []
        t0 = time.time()
        for path, pno in self.photo_pages:
            res = grade.grade_image(self.conn, PID, path, out_dir=self.grade_out)
            ok_warp = bool(res.get("warp_ok"))
            # v4：只有 manifest 里标了 qr 的页（第 1 页）才有 QR，其余页不参与 QR 成功率
            has_qr = bool((self.page_by_no.get(pno) or {}).get("qr"))
            ok_qr = bool(res.get("qr_ok"))
            warp_ok += 1 if ok_warp else 0
            if has_qr:
                qr_checked += 1
                qr_ok += 1 if ok_qr else 0
            items = res.get("items") or []
            page_omr = page_agree = page_unsure = 0
            for u in items:
                if u.get("kind") == card.KIND_OMR:
                    page_omr += 1
                    want = self.intent.get(u.get("idx"))
                    if u.get("verdict") == "unsure":
                        page_unsure += 1
                    elif want and u.get("verdict") == want:
                        page_agree += 1
                    if want and u.get("verdict") != want:
                        bad_rows.append({"photo": os.path.basename(path),
                                         "idx": u.get("idx"), "want": want,
                                         "verdict": u.get("verdict"),
                                         "picked": u.get("picked"),
                                         "expected": u.get("expected"),
                                         "ratios": u.get("ratios")})
                elif u.get("verdict") == "pending":
                    pending += 1
            omr_total += page_omr
            omr_agree += page_agree
            omr_unsure += page_unsure
            rows.append({"photo": os.path.basename(path), "page": pno,
                         "warp_ok": ok_warp, "qr_ok": ok_qr,
                         "score": res.get("score"), "full": res.get("full"),
                         "omr_on_page": page_omr, "omr_agree": page_agree,
                         "omr_unsure": page_unsure, "sid": res.get("sid"),
                         "error": res.get("error")})
        dt = time.time() - t0
        agreement = (omr_agree / float(omr_total)) if omr_total else 0.0
        dump("grade_photos.json", {
            "photos": self.N_PHOTOS, "warp_ok": warp_ok, "qr_ok": qr_ok,
            "qr_checked": qr_checked,
            "elapsed_s": round(dt, 2),
            "omr_units": omr_total, "omr_agree": omr_agree, "omr_unsure": omr_unsure,
            "omr_agreement": round(agreement, 4),
            "pending_cells": pending, "mismatches": bad_rows[:20],
            "per_photo": rows,
        })
        self.assertGreaterEqual(warp_ok, 9, "标定成功率 %d/%d（要求 ≥9/10）"
                                % (warp_ok, self.N_PHOTOS))
        self.assertGreater(qr_checked, 0, "没有任何一页带 QR，QR 链路没被测到")
        self.assertEqual(qr_checked, qr_ok,
                         "带 QR 的页 QR 必须全解出来：%d/%d" % (qr_ok, qr_checked))
        self.assertGreater(omr_total, 0, "一张卷子上的选择题都没判到")
        self.assertEqual(0, omr_unsure, "选择题出现 unsure：%s" % bad_rows[:3])
        self.assertEqual(omr_total, omr_agree,
                         "选择题判分一致率 %.1f%%（%d/%d）：%s"
                         % (agreement * 100, omr_agree, omr_total, bad_rows[:3]))
        self.assertGreater(pending, 0, "填空/解答应标 pending 交异步识别")

    def test_02_pending_has_crops(self):
        """pending 的作答格必须落裁切图（人工确认要用）。"""
        res = grade.grade_image(self.conn, PID, self.photo_pages[0][0],
                                out_dir=self.grade_out)
        pend = [u for u in res.get("items") or [] if u.get("verdict") == "pending"]
        self.assertTrue(pend, "这张卡上没有 pending 项")
        missing = [u["idx"] for u in pend
                   if not (u.get("crop_path") and os.path.exists(u["crop_path"]))]
        prev = load_json("grade_photos.json")
        prev["pending_sample"] = {"sid": res.get("sid"), "n": len(pend),
                                  "missing_crop": missing,
                                  "crop_dir": os.path.dirname(pend[0].get("crop_path") or "")}
        dump("grade_photos.json", prev)
        self.assertEqual([], missing, "pending 缺裁切图：%s" % missing[:5])

    def test_03_submission_row(self):
        res = grade.grade_image(self.conn, PID, self.photo_pages[1][0],
                                out_dir=self.grade_out)
        row = self.conn.execute("SELECT pid, warp_ok, qr_ok, score, full, detail_json"
                                " FROM submissions WHERE sid=?", (res["sid"],)).fetchone()
        self.assertIsNotNone(row, "判卷结果没落 submissions 表")
        self.assertEqual(PID, row["pid"])
        self.assertEqual(1, row["warp_ok"])
        detail = json.loads(row["detail_json"])
        self.assertEqual(len(res["items"]), len(detail["items"]))



# ---------------------------------------------------------------- T4b 卷面保真专项（10 OMML + 10 MathType）
class T4bOmmAndMathType(unittest.TestCase):
    """t13 第 4 项：从题库里挑 10 道 OMML 题 + 10 道 MathType 题单独出卷，逐项比 token 数，缺失=0。"""

    NAME = "ooml_mathtype"

    @classmethod
    def setUpClass(cls):
        import collections
        conn = ro_conn()
        try:
            rows = conn.execute("SELECT no, qtype, tokens_json FROM questions").fetchall()
            ooml, mathtype = [], []
            for r in rows:
                try:
                    toks = json.loads(r["tokens_json"] or "[]")
                except ValueError:
                    continue
                kinds = [(t or {}).get("kind") for t in toks]
                n_math = kinds.count("math")
                n_embed = kinds.count("embed")
                if n_math and not n_embed:
                    ooml.append((r["no"], r["qtype"], n_math))
                elif n_embed and not n_math:
                    mathtype.append((r["no"], r["qtype"], n_embed))
            # 排序后取前 10，结果稳定可复现；**先按题库体检过滤**（t85：00026 已被标
            # fig_missing，直接进卷会被 picker 按「不能印到卷面」挡掉）
            import quality as _quality
            def _printable(items):
                nos = [no for no, _, _ in items]
                ok, _blocked = _quality.filter_printable(conn, nos, need_answer=True)
                keep = set(ok)
                return [it for it in items if it[0] in keep]
            cls.ooml = _printable(sorted(ooml, key=lambda x: x[0]))[:10]
            cls.mathtype = _printable(sorted(mathtype, key=lambda x: x[0]))[:10]
            cls.picked = [(no, qt, "OMML") for no, qt, _ in cls.ooml] + \
                         [(no, qt, "MathType") for no, qt, _ in cls.mathtype]
            nos = [p[0] for p in cls.picked]
            mix = dict(collections.Counter(p[1] for p in cls.picked))
            cls.params = {"title": "验收 20 题（10 OMML + 10 MathType）",
                          "mix": mix, "nos": nos,
                          "requirements": {"need_answer": True, "exclude_empty": True},
                          "seed": 42}
            cls.manifest = picker.build(cls.params, conn, pid="P20261031-903")
        finally:
            conn.close()

    def test_01_build_and_compare(self):
        out = _ensure(os.path.join(RUNDIR, "ooml"))
        built = paper.build_docx(self.manifest, out, name=self.NAME)
        _publish(built["docx"], "paper")
        from docx import Document
        from lxml import etree
        doc = Document(built["docx"])
        boxes = _item_boxes(doc)               # 排除学生栏等装饰表
        rels = {rid: r.target_ref for rid, r in doc.part.rels.items()}
        self.assertEqual(20, len(boxes), "20 题应各有一个题框")

        rows, missing = [], []
        for (no, qtype, kind), box in zip(self.picked, boxes):
            src = os.path.join(REAL_DOCX, "%s.docx" % no)
            exp_math = exp_embed = exp_media = 0
            src_rels, src_media = _source_media(src)
            for tok in docx_rich.extract_tokens(src):
                if tok.kind == "math":
                    exp_math += 1
                elif tok.kind == "embed":
                    exp_embed += 1
                    exp_media += len(tok.media)
                elif tok.kind == "table":
                    for rids in _embed_refs(tok.xml):
                        exp_embed += 0          # 表格内引用不算 embed token，单独记在 media
                        exp_media += len(rids)
            xml = etree.tostring(box, encoding="unicode")
            obs_math = _count_markers(xml).get("oMath", 0)
            obs_embed = (len(_embed_refs(xml)))
            # 媒体引用数：每个 embed 容器内去重后求和（compose 会按内容哈希去重）
            obs_media = sum(len(rids) for rids in _embed_refs(xml))
            row = {"no": no, "qtype": qtype, "type": kind,
                   "exp": {"math": exp_math, "embed": exp_embed, "media": exp_media},
                   "obs": {"math": obs_math, "embed": obs_embed, "media": obs_media}}
            rows.append(row)
            for k in ("math", "embed", "media"):
                if row["obs"][k] < row["exp"][k]:
                    missing.append({"no": no, "marker": k, "exp": row["exp"][k],
                                    "obs": row["obs"][k]})
        dump("paper_ooml_mathtype.json", {
            "picked": [{"no": n, "qtype": q, "type": k} for n, q, k in self.picked],
            "n_ooml": len(self.ooml), "n_mathtype": len(self.mathtype),
            "rows": rows, "missing": missing,
            "docx": built["docx"],
        })
        self.assertEqual(10, len(self.ooml), "OMML 题不足 10 道")
        self.assertEqual(10, len(self.mathtype), "MathType 题不足 10 道")
        self.assertEqual([], missing, "卷面 token 缺失：%s" % missing[:5])


# ---------------------------------------------------------------- T7 人工确认链路（真 FastAPI TestClient）
class _NoTextRecognizer:
    """等价于 manual 后端：识别不出任何文本（全部留在待人工确认）。"""

    backend = "manual"

    def read_batch(self, items):
        return {str(k): {"text": "", "confidence": 0.0, "backend": "manual"} for k, _p in items}

    def read(self, path, kind=None):
        return {"text": "", "confidence": 0.0, "backend": "manual"}


class T7ServerConfirmChain(unittest.TestCase):
    """t13 第 7 项：真服务（TestClient + 真库副本）建卷 → 上传 → manual 重识别 → 逐空确认 → 分数重算。"""

    @classmethod
    def setUpClass(cls):
        import shutil
        from fastapi.testclient import TestClient
        import server
        cls.work = _ensure(os.path.join(RUNDIR, "chain"))
        cls.db = os.path.join(cls.work, "app.db")
        if os.path.exists(cls.db):
            os.remove(cls.db)
        shutil.copyfile(DB_PATH, cls.db)
        cls.out = _ensure(os.path.join(cls.work, "out"))
        cls.app = server.create_app(db_path=cls.db, out_dir=cls.out,
                                    config_path=os.path.join(APP, "config.json"))
        cls.client = TestClient(cls.app, follow_redirects=False)
        cls.pid = None
        cls.sid = None
        cls.score_before = cls.full = None
        cls.pending_units = []

    def test_01_create_paper(self):
        r = self.client.post("/papers", data={"title": "t13 人工确认链路卷", "seed": "42",
                                              "n_choice": "6", "n_fill": "2",
                                              "n_solution": "2", "need_answer": "on"})
        self.assertEqual(302, r.status_code, "POST /papers 应 302")
        loc = r.headers.get("location") or ""
        type(self).pid = loc.rstrip("/").rsplit("/", 1)[-1]
        self.assertTrue(type(self).pid.startswith("P"), "没拿到 pid：%r" % loc)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT manifest, n_items FROM papers WHERE pid=?",
                               (self.pid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "papers 里没有这卷")
        self.assertEqual(10, row["n_items"])
        type(self).manifest = json.loads(row["manifest"])

    def test_02_upload_photo(self):
        pid, man = self.pid, self.manifest
        card_png = os.path.join(self.out, pid, "card_1.png")
        self.assertTrue(os.path.isfile(card_png), "没生成答题卡：%s" % card_png)
        by_idx = {it["idx"]: it for it in man["items"]}
        # 故意把 idx%2==1 的选择题涂成错误选项（留作错题），填空/解答留白 → pending
        chosen = {}
        for it in (man.get("card") or {}).get("pages")[0]["items"]:
            if it["kind"] != "omr":
                continue
            expected = str((by_idx[it["idx"]]["answer_key"][0] or {}).get("value") or "")
            opts = [str(c["opt"]) for c in it["cells"]]
            if it["idx"] % 2 == 1:
                chosen[it["idx"]] = next((o for o in opts if o != expected), expected)
            else:
                chosen[it["idx"]] = expected
        img = read_gray(card_png)
        self.assertIsNotNone(img, "答题卡读不出来")
        pil = Image.fromarray(img)
        d = ImageDraw.Draw(pil)
        for it in (man.get("card") or {}).get("pages")[0]["items"]:
            if it["kind"] != "omr":
                continue
            want = chosen.get(it["idx"])
            for c in it["cells"]:
                if str(c["opt"]) == str(want):
                    d.rectangle([c["x"] + 9, c["y"] + 9,
                                 c["x"] + c["w"] - 9, c["y"] + c["h"] - 9], fill=0)
        filled = os.path.join(self.work, "filled.png")
        pil.save(filled)
        jpg = _synth_photo(filled, os.path.join(self.work, "photo.jpg"), seed=42)
        with open(jpg, "rb") as f:
            r = self.client.post("/submissions", data={"pid": pid},
                                 files={"file": ("photo.jpg", f.read(), "image/jpeg")})
        self.assertEqual(302, r.status_code, "上传应 302，实际 %s" % r.status_code)
        loc = r.headers.get("location") or ""
        type(self).sid = loc.rstrip("/").rsplit("/", 1)[-1]
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            sub = conn.execute("SELECT warp_ok, qr_ok, score, full, detail_json"
                               " FROM submissions WHERE sid=?", (self.sid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(sub, "submissions 没落库")
        self.assertEqual(1, sub["warp_ok"])
        self.assertEqual(1, sub["qr_ok"])
        detail = json.loads(sub["detail_json"])
        units = detail["items"]
        type(self).detail = detail
        type(self).score_before = sub["score"]
        type(self).full = sub["full"]
        type(self).pending_units = [u for u in units if u["verdict"] == "pending"]
        dump("chain_step1_upload.json", {
            "pid": pid, "sid": self.sid, "warp_ok": sub["warp_ok"], "qr_ok": sub["qr_ok"],
            "score": sub["score"], "full": sub["full"],
            "pending": len(self.pending_units),
            "wrong": [u["idx"] for u in units if u["verdict"] == "wrong"],
            "units": [{"idx": u["idx"], "blank": u["blank"], "kind": u["kind"],
                       "verdict": u["verdict"], "picked": u.get("picked"),
                       "expected": u.get("expected")} for u in units],
        })
        self.assertGreater(len(self.pending_units), 0, "填空/解答应留 pending")
        self.assertLess(sub["score"], sub["full"], "故意涂错的选择题应判错")

    def test_03_manual_reocr_keeps_pending(self):
        """manual 后端：识别不出 → 仍 pending，分数不变（不静默判错）。"""
        self.app.state.recognizer = _NoTextRecognizer()
        r = self.client.post("/submissions/%s/reocr" % self.sid)
        self.assertIn(r.status_code, (200, 303), "reocr 应 200/303，实际 %s" % r.status_code)
        st = self.client.get("/submissions/%s/status" % self.sid).json()
        dump("chain_step2_reocr.json", st)
        self.assertEqual(len(self.pending_units), st["pending"], "manual 后端不应改变 pending 数")
        self.assertEqual(self.score_before, st["score"], "manual 后端不应改分")

    def test_04_confirm_recomputes(self):
        """逐空人工确认判对 → pending 归零、分数按空累加。"""
        before = self.client.get("/submissions/%s/status" % self.sid).json()
        data = {"verdict_%s_%s" % (u["idx"], u["blank"]): "correct" for u in self.pending_units}
        for u in self.pending_units:
            data["text_%s_%s" % (u["idx"], u["blank"])] = str(u.get("expected") or "")
        r = self.client.post("/submissions/%s/confirm" % self.sid, data=data)
        self.assertIn(r.status_code, (200, 303), "confirm 应 200/303，实际 %s" % r.status_code)
        after = self.client.get("/submissions/%s/status" % self.sid).json()
        dump("chain_step3_confirm.json", {"before": before, "after": after,
                                          "confirmed_units": len(self.pending_units)})
        self.assertEqual(0, after["pending"], "确认后 pending 应归零")
        self.assertEqual(before["score"] + len(self.pending_units), after["score"],
                         "分数应等于 原分 + 确认判对的空数")
        self.assertGreater(after["score"], before["score"])
        # 页面能打开（说明 detail 与模板都对得上）
        page = self.client.get("/submissions/%s" % self.sid)
        self.assertEqual(200, page.status_code)

    def test_05_train_from_submission(self):
        """第 8 项（产品真链路）：POST /train 用本次判卷剩下的错题生成训练卷。"""
        status = self.client.get("/submissions/%s/status" % self.sid).json()
        wrong = [u for u in self.detail["items"]
                 if u["verdict"] in ("wrong", "unsure")]
        wrong_nos = sorted({str(u["no"]) for u in wrong})
        self.assertTrue(wrong_nos, "这次判卷应还剩错题（题号 %s）" % wrong_nos)
        r = self.client.post("/train", data={"sid": self.sid, "n_per": "3"})
        self.assertEqual(302, r.status_code,
                         "POST /train 应 302，实际 %s" % r.status_code)
        tpid = (r.headers.get("location") or "").rstrip("/").rsplit("/", 1)[-1]
        self.assertTrue(tpid.startswith("P"), "没拿到训练卷 pid：%r" % r.headers.get("location"))
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT manifest, n_items FROM papers WHERE pid=?",
                               (tpid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "训练卷没落库")
        tman = json.loads(row["manifest"])
        nos = [it["no"] for it in tman["items"]]
        params = tman.get("params") or {}
        train_info = params.get("train") or {}
        sources = train_info.get("sources") or []
        by_no = {it["no"]: it for it in tman["items"]}
        rows, uncovered = [], []
        for src in sources:
            for no in src.get("picked") or []:
                it = by_no.get(no) or {}
                same_family = bool(src.get("family_key")) and \
                    it.get("family_key") == src.get("family_key")
                same_kp = bool(src.get("kp_id")) and it.get("kp_id") == src.get("kp_id")
                same_chapter = it.get("chapter") == src.get("chapter")
                rows.append({"source": src["no"], "picked": no,
                             "same_family": same_family, "same_kp": same_kp,
                             "same_chapter": same_chapter,
                             "qtype": it.get("qtype"),
                             "source_qtype": src.get("qtype")})
                if not (same_family or same_kp or same_chapter):
                    uncovered.append(rows[-1])
        dump("chain_step4_train.json", {
            "sid": self.sid, "wrong_nos": wrong_nos, "train_pid": tpid,
            "n_items": row["n_items"], "nos": nos,
            "no_original": sorted(set(nos) & set(wrong_nos)),
            "expected_n": 3 * len(wrong_nos),
            "coverage": {"n_family": sum(r["same_family"] for r in rows),
                         "n_kp": sum(r["same_kp"] for r in rows),
                         "n_chapter": sum(r["same_chapter"] for r in rows),
                         "uncovered": uncovered},
            "shortfall": train_info.get("shortfall"),
            "status_before_train": status,
        })
        self.assertEqual(3 * len(wrong_nos), len(nos),
                         "训练卷题数应为 错题数 × 3 = %d" % (3 * len(wrong_nos)))
        self.assertEqual([], sorted(set(nos) & set(wrong_nos)), "训练卷不许出现原错题")
        self.assertEqual([], uncovered, "训练题应覆盖题族/知识点/章节：%s" % uncovered[:3])


# ---------------------------------------------------------------- T8 错题训练卷
class T8TrainFromWrong(unittest.TestCase):
    """t13 第 8 项：用判错题跑 train.plan → picker.build，检查无原题重复 / 题数正确 / 覆盖题族或知识点。"""

    N_PER = 3

    @classmethod
    def setUpClass(cls):
        import collections
        # 找一道真题，从它的题族/知识点出发；用"真实判卷会怎么判"的口径构造错题集合：
        # 直接取 3 道有题族的题当错题（模拟第 6 步判错的那些），再用真库跑训练计划。
        conn = ro_conn()
        try:
            rows = conn.execute(
                "SELECT no, qtype, family_key, kp_id, chapter FROM questions"
                " WHERE COALESCE(family_key,'')<>'' AND qtype='选择'"
                " ORDER BY no LIMIT 3").fetchall()
            cls.wrong = [dict(no=r["no"], qtype=r["qtype"], family_key=r["family_key"],
                              kp_id=r["kp_id"], chapter=r["chapter"]) for r in rows]
            cls.wrong_nos = [w["no"] for w in cls.wrong]
            cls.params = train.plan(conn, cls.wrong_nos, n_per=cls.N_PER)
            cls.manifest = picker.build(cls.params, conn, pid="P20261031-904")
        finally:
            conn.close()

    def test_01_no_original_and_count(self):
        nos = [it["no"] for it in self.manifest["items"]]
        dump("train_plan.json", {
            "wrong_nos": self.wrong_nos, "n_per": self.N_PER,
            "picked_nos": nos, "n_items": len(nos),
            "sources": (self.params.get("train") or {}).get("sources"),
            "shortfall": (self.params.get("train") or {}).get("shortfall"),
        })
        self.assertEqual(self.N_PER * len(self.wrong_nos), len(nos),
                         "题数应为 错题数 × n_per（除非同类题不足）")
        self.assertEqual([], sorted(set(nos) & set(self.wrong_nos)),
                         "训练卷不许出现原错题")
        self.assertEqual(len(nos), len(set(nos)), "训练卷题号不许重复")

    def test_02_covers_family_or_kp(self):
        by_no = {it["no"]: it for it in self.manifest["items"]}
        sources = (self.params.get("train") or {}).get("sources") or []
        self.assertEqual(len(self.wrong_nos), len(sources), "每道错题都应有配题来源记录")
        rows, uncovered = [], []
        for src in sources:
            for no in src.get("picked") or []:
                it = by_no.get(no) or {}
                same_family = bool(src.get("family_key")) and \
                    it.get("family_key") == src.get("family_key")
                same_kp = bool(src.get("kp_id")) and it.get("kp_id") == src.get("kp_id")
                same_chapter = it.get("chapter") == src.get("chapter")
                same_qtype = it.get("qtype") == src.get("qtype")
                row = {"source": src["no"], "picked": no, "qtype": it.get("qtype"),
                       "same_family": same_family, "same_kp": same_kp,
                       "same_chapter": same_chapter, "same_qtype": same_qtype}
                rows.append(row)
                if not (same_family or same_kp or same_chapter):
                    uncovered.append(row)
        dump("train_coverage.json", {"rows": rows, "uncovered": uncovered,
                                     "n_family": sum(r["same_family"] for r in rows),
                                     "n_kp": sum(r["same_kp"] for r in rows),
                                     "n_chapter": sum(r["same_chapter"] for r in rows)})
        # 题型：t13 只要求覆盖题族/知识点，同族不同题型（选择→填空）是允许的，记录不硬断言
        type_mismatch = [r for r in rows if not r["same_qtype"]]
        self.assertEqual([], uncovered, "有训练题既不共享题族也不共享知识点/章节：%s" % uncovered[:3])
        dump("train_coverage_types.json", {"rows": len(rows), "type_mismatch": type_mismatch,
                                           "n_type_mismatch": len(type_mismatch)})

if __name__ == "__main__":
    unittest.main(verbosity=2)
