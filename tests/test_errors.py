# -*- coding: utf-8 -*-
"""t18 · 错误文案字典测试：每个真实错误码都要能说清「发生了什么/为什么/怎么办」。

守两件事：
  1. 用户看到的是**可执行动作**，不是「请联系管理员」；
  2. 文案能吃真实 detail（picker 的 details、grade 的 detail 字符串、ocr 的降级原因），
     缺字段时不许抛异常，未知错误码要有兜底而不是白屏。
"""
from __future__ import annotations

import os
import sys
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import errors  # noqa: E402

#: 任务点名要覆盖的 11 个码（来自 grade / picker / paper / ocr / server 的真实抛错）
REQUIRED_CODES = (
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
)

#: grade.py 真的会返回的另外几个码，结果页也一定会渲染
GRADE_CODES = ("paper_not_found", "no_card", "submission_not_found", "internal_error")

FIELDS = ("title", "what", "why", "fix", "retry_label")


class TestEveryCodeExplains(unittest.TestCase):
    def test_required_codes_present(self):
        for code in REQUIRED_CODES:
            with self.subTest(code=code):
                self.assertIn(code, errors.CODES, "错误码字典缺 %s" % code)

    def test_grade_codes_present(self):
        for code in GRADE_CODES:
            with self.subTest(code=code):
                self.assertIn(code, errors.CODES, "grade.py 会返回的 %s 没登记" % code)

    def test_five_fields_are_non_empty_strings(self):
        for code in errors.CODES:
            entry = errors.explain(code)
            with self.subTest(code=code):
                for field in FIELDS:
                    self.assertIn(field, entry, "%s 缺字段 %s" % (code, field))
                    self.assertIsInstance(entry[field], str)
                    self.assertTrue(entry[field].strip(), "%s.%s 是空的" % (code, field))

    def test_titles_are_distinct(self):
        titles = [errors.explain(c)["title"] for c in errors.CODES]
        self.assertEqual(len(titles), len(set(titles)), "不同错误码撞了同一个标题")

    def test_fix_is_actionable_not_a_handoff(self):
        banned = ("联系管理员", "请联系", "联系客服", "询问管理员", "等待管理员")
        for code in errors.CODES:
            fix = errors.explain(code)["fix"]
            with self.subTest(code=code):
                for word in banned:
                    self.assertNotIn(word, fix, "%s 的 fix 在踢皮球：%s" % (code, fix))

    def test_retry_labels_are_real_actions(self):
        self.assertEqual("重新拍照", errors.explain("warp_failed")["retry_label"])
        self.assertEqual("重新识别", errors.explain("empty_batch")["retry_label"])
        self.assertEqual("手动填卷号", errors.explain("qr_failed")["retry_label"])
        for code in errors.CODES:
            self.assertTrue(errors.explain(code)["retry_label"].strip())


class TestSpecificWordings(unittest.TestCase):
    """队长点名要用的几句文案，写成断言防止后面被改写掉。"""

    def test_warp_failed_talks_about_four_corners(self):
        e = errors.explain("warp_failed")
        blob = e["what"] + e["why"] + e["fix"]
        self.assertIn("四角", blob)
        for action in ("拍", "折", "挡", "正对"):
            self.assertIn(action, blob + e["title"])

    def test_warp_failed_uses_found_count(self):
        e = errors.explain("warp_failed", {"found": 2})
        self.assertIn("2", e["what"])

    def test_qr_failed_offers_manual_pid(self):
        e = errors.explain("qr_failed")
        self.assertIn("手动", e["fix"])
        self.assertIn("卷号", e["fix"])

    def test_pid_mismatch_shows_both_pids(self):
        e = errors.explain("pid_mismatch", {"pid": "P20261031-001", "qr_pid": "P20261031-009"})
        blob = e["what"] + e["fix"]
        self.assertIn("P20261031-001", blob)
        self.assertIn("P20261031-009", blob)

    def test_insufficient_questions_shows_need_available_short(self):
        e = errors.explain("InsufficientQuestions",
                           {"details": {"选择": {"need": 10, "available": 4, "short": 6}}})
        blob = e["what"] + e["fix"]
        for token in ("选择", "10", "4", "6"):
            self.assertIn(token, blob, "题量不足文案缺 %s" % token)
        self.assertIn("差", e["what"])

    def test_insufficient_questions_accepts_real_exception_details(self):
        # picker.InsufficientQuestions.details 的真实结构
        import picker

        exc = picker.InsufficientQuestions(
            {"选择": {"need": 10, "available": 4, "short": 6},
             "解答": {"need": 6, "available": 0, "short": 6}},
            hint="已按 scope 与 requirements 过滤，不做凑数")
        e = errors.explain(type(exc).__name__, {"details": exc.details, "message": str(exc)})
        blob = e["what"] + e["fix"]
        self.assertIn("选择", blob)
        self.assertIn("解答", blob)
        self.assertIn("凑数", blob)  # message 透传，不当噪声丢掉

    def test_insufficient_questions_parses_picker_message(self):
        """只给 message（没给 details）时，也要把每类题差多少讲清楚。"""
        e = errors.explain("InsufficientQuestions", {
            "message": "InsufficientQuestions: 题量不足：选择 需要 99，可用 10，差 89"})
        self.assertIn("选择：要 99 题", e["what"])
        self.assertIn("差 89 题", e["what"])

    def test_scope_error_can_list_choices(self):
        e = errors.explain("ScopeError",
                           {"unknown": "第9章 不存在的章",
                            "available": ["第1章 有理数", "第2章 整式及其加减"]})
        blob = e["what"] + e["fix"]
        self.assertIn("第9章 不存在的章", blob)
        self.assertIn("第1章 有理数", blob)

    def test_word_unavailable_mentions_degraded_render(self):
        e = errors.explain("WordUnavailable")
        blob = e["what"] + e["why"] + e["fix"]
        self.assertIn("Word", blob)
        self.assertTrue("失真" in blob or "降级" in blob, blob)

    def test_empty_batch_says_service_not_responding(self):
        e = errors.explain("empty_batch")
        self.assertIn("识别服务暂时无响应", e["what"])
        self.assertIn("重新识别", e["fix"] + e["retry_label"])

    def test_ocr_timeout_and_quota_are_different(self):
        a, b = errors.explain("ocr_timeout"), errors.explain("ocr_quota")
        self.assertNotEqual(a["title"], b["title"])
        self.assertIn("超时", a["title"] + a["what"])
        self.assertIn("额度", b["why"] + b["what"] + b["title"])

    def test_db_missing_points_to_build_db(self):
        e = errors.explain("db_missing", {"path": r"E:\OCR\app\data\app.db"})
        self.assertIn("build_db.py", e["fix"])
        self.assertIn("app.db", e["what"])

    def test_db_locked_mentions_lock_holder(self):
        e = errors.explain("db_locked", {"error": "database is locked"})
        blob = e["what"] + e["fix"]
        self.assertIn("锁", blob)
        self.assertIn("database is locked", e["what"])


class TestRobustness(unittest.TestCase):
    def test_unknown_code_has_fallback(self):
        for code in ("这是什么码", "unknown_error", "", None):
            with self.subTest(code=code):
                e = errors.explain(code)
                for field in FIELDS:
                    self.assertTrue(e[field].strip(), "兜底缺 %s" % field)
                self.assertIn("重试", e["retry_label"])

    def test_alias_variants_resolve(self):
        pairs = [
            ("insufficient_questions", "InsufficientQuestions"),
            ("scope_error", "ScopeError"),
            ("word_unavailable", "WordUnavailable"),
        ]
        for alias, canonical in pairs:
            with self.subTest(alias=alias):
                self.assertEqual(errors.explain(canonical)["title"], errors.explain(alias)["title"])

    def test_detail_none_and_string_and_weird_types(self):
        self.assertTrue(errors.explain("warp_failed", None)["what"])
        e = errors.explain("warp_failed", "只检出 2/4 个 ArUco")
        self.assertIn("2/4", e["what"])
        # 奇怪的 detail 不能炸
        for detail in ({"found": object()}, {"details": "oops"}, {"available": 123}, 12345, ["x"]):
            with self.subTest(detail=repr(detail)):
                e = errors.explain("InsufficientQuestions", detail)
                self.assertTrue(e["what"].strip())

    def test_codes_tuple_is_unique_and_stable(self):
        self.assertEqual(len(errors.CODES), len(set(errors.CODES)))
        self.assertIn("warp_failed", errors.CODES)
        self.assertEqual(tuple(errors.CODES), tuple(errors.CODES))

    def test_no_placeholder_leaks_without_detail(self):
        """指南页是不带 detail 渲染的：缺 context 时不能吐出「只找到 ? 个」。"""
        for code in errors.CODES:
            entry = errors.explain(code)
            with self.subTest(code=code):
                for field in FIELDS:
                    self.assertNotIn("?", entry[field],
                                     "%s.%s 漏了占位符：%s" % (code, field, entry[field]))
                self.assertNotIn("{", entry["what"] + entry["fix"],
                                 "%s 有没被替换的模板占位符" % code)


if __name__ == "__main__":
    unittest.main()
