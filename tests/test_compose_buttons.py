# -*- coding: utf-8 -*-
"""T77 · 组卷页按钮接线与「无 JS 退化」防回归。

锁住三件事（对应任务验收）：
1. 「刷新可出题量」不再是死按钮：页面脚本里 `getElementById('refresh-counts')` 真绑上了，
   而且用**捕获阶段**拦截 —— 因为 static/app.js 里老的那段 click 会 `location.assign("/?"+params)`，
   params 不含 subject_pk，重载后学科退回数学（用户报的「点一下就跳到数学」）。
2. 页面里没有「有 id 但没人用」的按钮/元素：每个 id 要么被脚本取到，要么是表单控件/分区锚点。
3. 无 JS 退化：`GET /?subject_pk=english9a&check=1` 渲染后年级下拉仍选中 english9a，标题是英语的。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import db as dbmod  # noqa: E402
import server  # noqa: E402

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_TPL = os.path.join(APP_DIR, "templates", "index.html")
APP_JS = os.path.join(APP_DIR, "static", "app.js")

MATH = ("math7a", "数学", "七年级上", "沪科版")
ENG = ("english9a", "英语", "九年级上", "人教版")

#: 允许「有 id 但脚本不取」的元素（表单语义 / 分区锚点），逐条写清理由
ID_ALLOWLIST = {
    "subject": "分区锚点（<section id=subject>）",
    "mix": "分区锚点（<section id=mix>）",
    "range": "分区锚点（<section id=range>，app.js 会跳到 #range）",
    "advanced": "分区锚点（<section id=advanced>，高级设置块）",
    "check-avail": "type=submit 按钮，靠表单语义（formaction/formmethod）工作",
    "subject-note": "纯文本提示容器",
    "subject-meta": "JSON 数据块，由 t72 标题脚本 JSON.parse 读取",
    # ui 域新增的纯说明文字（不参与交互，脚本不需要取它；保留 id 便于将来按需显隐）
    "excluded-hint": "纯静态提示文案（缺图口径说明）",
    # UI 落地：纯样式折叠块（原生 <details>，不需要 JS 取它）
    "advanced-fold": "纯样式折叠块（高级：标题/seed），原生 details 即可",
    "req-fold": "纯样式折叠块（选题要求），原生 details 即可",
}


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


class ComposeButtonsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="t77_")
        cls.db_path = os.path.join(cls.tmp, "app.db")
        conn = dbmod.connect(cls.db_path)
        try:
            for pk, name, grade, version in (MATH, ENG):
                dbmod.ensure_subject(conn, pk, name, grade, version)
            conn.execute(
                "INSERT OR REPLACE INTO questions(no,qtype,chapter,kp_id,subject_pk,"
                "text_plain,answer_raw,fmt) VALUES(?,?,?,?,?,?,?,?)",
                ("00001", "填空", "第1章 有理数", "1.1", MATH[0],
                 "1 的相反数是___", "-1", "plain"))
            conn.execute(
                "INSERT OR REPLACE INTO questions(no,qtype,chapter,kp_id,subject_pk,"
                "text_plain,answer_raw,fmt) VALUES(?,?,?,?,?,?,?,?)",
                ("00002", "填空", "Unit 1", "u1", ENG[0],
                 "I ___ a student.", "am", "plain"))
            conn.execute("INSERT OR REPLACE INTO kps(kp_id,subject_pk,chapter,kp,n_questions)"
                         " VALUES(?,?,?,?,?)", ("1.1", MATH[0], "第1章 有理数", "相反数", 1))
            conn.execute("INSERT OR REPLACE INTO kps(kp_id,subject_pk,chapter,kp,n_questions)"
                         " VALUES(?,?,?,?,?)", ("u1", ENG[0], "Unit 1", "be 动词", 1))
            conn.commit()
        finally:
            conn.close()
        cls.out_dir = os.path.join(cls.tmp, "out")
        os.makedirs(cls.out_dir, exist_ok=True)
        cls.app = server.create_app(db_path=cls.db_path, out_dir=cls.out_dir)
        cls.client = TestClient(cls.app)

    def page(self, url: str = "/") -> str:
        res = self.client.get(url)
        self.assertEqual(200, res.status_code, res.text[:200])
        return res.text


class RefreshButtonRemovedTest(ComposeButtonsBase):
    """t98：「刷新可出题量」按钮已删除 —— 勾选/换学科本来就自动刷新。"""

    def test_refresh_button_is_gone_from_page(self):
        html = self.page()
        self.assertNotIn("refresh-counts", html, "「刷新可出题量」按钮必须删掉")
        self.assertNotIn("刷新可出题量", html, "按钮文案也不该再出现")
        self.assertNotIn("refreshBtn", html, "它的点击处理也要一起删")

    def test_page_says_auto_refresh(self):
        html = self.page()
        self.assertIn("自动刷新", html, "提示要说明「改完勾选会自动刷新」")

    def test_app_js_has_no_page_navigation(self):
        """防回归：static/app.js 里不许再出现整页跳转（刷新按钮跳回数学的真凶）。"""
        js = _read(APP_JS)
        self.assertNotIn("location.assign", js,
                         "app.js 不许整页跳转（旧代码拼参数不带 subject_pk，刷新会跳回数学）")
        self.assertNotIn("#range", js, "app.js 不许再跳到 #range 锚点")
        self.assertNotIn("location.href", js, "app.js 不许改 location.href 做整页跳转")
        self.assertNotIn("refresh-counts", js,
                         "刷新按钮的点击行为应只在 index.html（就地 fetch），app.js 不再碰它")

    def test_page_does_not_navigate_on_refresh(self):
        html = self.page()
        # 注释里会提到 location.assign（记录 app.js 那个坑），只看**可执行代码**
        code = re.sub(r"/\*.*?\*/", " ", html, flags=re.S)
        code = re.sub(r"<!--.*?-->", " ", code, flags=re.S)
        # 行尾的 // 注释也去掉（本文件里就有一句「不让 app.js 的 location.assign 跑起来」）
        code = "\n".join(re.sub(r"//.*$", "", line) for line in code.splitlines())
        self.assertNotIn("location.assign", code,
                         "index.html 自己不许整页跳转（老 app.js 那段才是跳转元凶）")
        self.assertIn("refresh()", html, "仍要有就地刷新函数（自动刷新用）")
        # t98：按钮删了，自动刷新靠 change 事件 + 300ms 防抖
        self.assertRegex(html, r"DEBOUNCE_MS\s*=\s*300", "自动刷新仍是 300ms 防抖")
        self.assertRegex(html, r"form\.addEventListener\('change'[\s\S]{0,300}?schedule\(\)",
                         "勾选/换学科变化要触发自动刷新")

    def test_refresh_calls_available_api(self):
        html = self.page()
        self.assertIn("/api/available?", html, "刷新要走 /api/available，不能重载页面")


class DeadIdTest(ComposeButtonsBase):
    """页面上不许再有「有 id 但没人用」的元素（防回归）。"""

    def _ids(self, html: str) -> list:
        return re.findall(r'\bid="([^"]+)"', html)

    def _referenced(self, html: str) -> set:
        refs = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", html))
        refs |= set(re.findall(r"querySelector(?:All)?\(\s*['\"]#([A-Za-z0-9_-]+)", html))
        refs |= set(re.findall(r"['\"]#([A-Za-z0-9_-]+)['\"]", html))       # $("#id")
        refs |= set(re.findall(r"href=\"#([A-Za-z0-9_-]+)\"", html))
        # app.js 用的是 $("#total-count") / $("#compose-overlay") 这种选择器
        app_js = _read(APP_JS)
        refs |= set(re.findall(r"\$\(\s*[\"']#([A-Za-z0-9_-]+)[\"']", app_js))
        refs |= set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", app_js))
        return refs

    def test_every_id_has_a_user(self):
        html = self.page()
        refs = self._referenced(html)
        dead = [i for i in self._ids(html)
                if i not in refs and i not in ID_ALLOWLIST]
        self.assertEqual([], dead,
                         "这些 id 没人用（脚本不取、也不是表单控件/锚点）：%r" % dead)

    def test_refresh_counts_is_removed_and_noscript_kept(self):
        """t98：「刷新可出题量」连 id 都不该有；无 JS 回退按钮只在 <noscript> 里。"""
        html = self.page()
        self.assertNotIn("refresh-counts", html, "多余额按钮已删除，不许复活")
        ns = re.search(r"<noscript>(.*?)</noscript>", html, re.S)
        self.assertIsNotNone(ns, "缺 <noscript> 无 JS 回退块")
        self.assertIn('id="check-avail"', ns.group(1),
                      "「查询剩余题数」必须只在 <noscript> 里（有 JS 的浏览器看不到）")
        self.assertNotIn('id="check-avail"', html[:ns.start()] + html[ns.end():],
                         "<noscript> 之外不许再出现「查询剩余题数」按钮")

    def test_allowlist_is_documented(self):
        for key, why in ID_ALLOWLIST.items():
            self.assertTrue(why, "白名单项 %s 要写清理由" % key)


class NoJsFallbackTest(ComposeButtonsBase):
    """无 JS 退化：GET ?check=1 之后学科不许被重置回数学。"""

    def _selected_subject(self, html: str) -> str:
        m = re.search(r'<option value="([^"]+)"[^>]*data-subject[^>]*selected', html)
        if m:
            return m.group(1)
        m = re.search(r'data-subject="[^"]*"[^>]*selected', html)
        self.assertIsNotNone(m, "年级下拉没有 selected 项")
        return ""

    def test_check_get_keeps_english_subject(self):
        html = self.page("/?subject_pk=english9a&check=1")
        self.assertIn('value="english9a"', html)
        self.assertRegex(
            html, r'<option value="english9a"[^>]*selected',
            "?subject_pk=english9a&check=1 之后年级仍要选中 english9a")
        self.assertIn("英语 九年级上（人教版）", html, "标题要跟着学科走")
        self.assertNotRegex(
            html, r'<option value="math7a"[^>]*selected',
            "不许退回数学")

    def test_check_get_keeps_math_subject(self):
        html = self.page("/?subject_pk=math7a&check=1")
        self.assertRegex(html, r'<option value="math7a"[^>]*selected')

    def test_hidden_subject_field_present_and_after_select(self):
        html = self.page("/?subject_pk=english9a&check=1")
        self.assertIn('id="subject-pk"', html)
        self.assertRegex(html, r'<input[^>]*id="subject-pk"[^>]*name="subject_pk"',
                         "隐藏域要带 name=subject_pk（GET 退化兜底）")
        # DOM 顺序：年级下拉在前 → 服务端取第一个值 = 用户选的
        self.assertLess(html.index('id="subject-grade"'), html.index('id="subject-pk"'),
                        "年级下拉必须在隐藏域之前，否则服务端会取到隐藏域的旧值")

    def test_hidden_value_synced_by_script(self):
        html = self.page()
        self.assertIn("syncSubject", html, "缺隐藏域同步逻辑")
        self.assertIn("data-subject-pk", html, "缺排查用的 data-subject-pk 属性")
        self.assertIn("form.addEventListener('submit', syncSubject)", html,
                      "提交前也要同步一次（换学科后立刻点提交）")


class ButtonSemanticsTest(ComposeButtonsBase):
    def test_check_avail_is_submit_get(self):
        html = self.page()
        m = re.search(r'<button[^>]*id="check-avail"[^>]*>', html)
        self.assertIsNotNone(m, "缺「查询剩余题数」按钮")
        ns = re.search(r"<noscript>(.*?)</noscript>", html, re.S)
        self.assertIsNotNone(ns, "缺 <noscript> 回退块")
        self.assertIn('id="check-avail"', ns.group(1), "回退按钮要在 <noscript> 里")
        tag = m.group(0)
        for need in ('type="submit"', 'formaction="/"', 'formmethod="get"', 'name="check"'):
            self.assertIn(need, tag, "无 JS 退化按钮要能 GET 到 /?check=1：%s" % need)

    def test_template_has_no_orphan_script_errors(self):
        """脚本里不该出现引用不存在元素后立刻取属性的写法（老的死按钮就是这样埋的）。"""
        html = self.page()
        self.assertNotIn("refreshCounts.addEventListener", html,
                         "index.html 里不要重复绑 refresh-counts（会和 app.js 抢）")


if __name__ == "__main__":
    unittest.main()
