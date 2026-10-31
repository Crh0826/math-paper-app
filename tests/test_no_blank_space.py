"""守门测试：全仓不许再出现「非 0 留白默认」。

背景（2026-10-31 用户实测）：卷面本来只印题、作答在答题卡上，但组卷表单/训练卷的默认参数里
写着 `"解答": 8`、`"填空": 2`，于是每道解答题白占 8×24pt ≈ **6.8cm**，24 题卷从 3 页涨到 6 页。
这条测试把「留白默认必须是 0」钉死：

1. 扫描 `app/*.py` 里的字典字面量（用 ast，不看注释/文档字符串），任何
   `{"选择"|"判断"|"填空"|"解答"|"其他": >0}` 都算违规 → 报文件:行号；
2. `picker.DEFAULT_SPACE` / `paper.DEFAULT_SPACE` 五个题型必须全 0；
3. 真实库出 24 题卷，`health.space_paras_total` 必须是 0。
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)

import paper  # noqa: E402
import picker  # noqa: E402

QTYPE_KEYS = {"选择", "判断", "填空", "解答", "其他"}
REAL_DB = os.path.join(APP, "data", "app.db")


def _is_space_dict(node, parents: dict) -> bool:
    """这个字典是不是「留白」字典？（别把 mix={} 里的题量当成留白）

    认三种写法：`space={...}` 关键字实参、`X_SPACE = {...}` 赋值、`"space": {...}` 字典项。
    """
    child = node
    for parent in parents.get(id(node), []):
        if isinstance(parent, ast.keyword) and (parent.arg or "").lower() == "space":
            return True
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            for tgt in targets:
                name = getattr(tgt, "id", None) or getattr(tgt, "attr", None) or ""
                if "space" in name.lower():
                    return True
            return False
        if isinstance(parent, ast.Dict):
            for key, value in zip(parent.keys, parent.values):
                if value is child and isinstance(key, ast.Constant) \
                        and str(key.value).lower() == "space":
                    return True
            return False
        if isinstance(parent, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            return False
        child = parent
    return False


def _positive_space_literals(path: str) -> list:
    """返回这个文件里「留白字典里 >0」的位置：[(line, qtype, value)]。"""
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []                      # 语法坏掉的文件不归这条测试管

    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents.setdefault(id(child), []).append(parent)

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or not _is_space_dict(node, parents):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if key.value not in QTYPE_KEYS:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, int) \
                    and value.value > 0:
                hits.append((node.lineno, key.value, value.value))
    return hits


class NoBlankSpaceDefaultTest(unittest.TestCase):
    """留白默认必须是 0（答案写在答题卡上）。"""

    def test_no_positive_space_literal_in_app(self):
        offenders = []
        for name in sorted(os.listdir(APP)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(APP, name)
            for line, qtype, value in _positive_space_literals(path):
                offenders.append("%s:%d  {%r: %d}" % (name, line, qtype, value))
        self.assertEqual(
            offenders, [],
            "卷面留白默认必须是 0（作答在答题卡上）。发现非 0 留白字面量：\n  "
            + "\n  ".join(offenders) + "\n改成 0，或用 picker.DEFAULT_SPACE")

    def test_scanner_catches_planted_literal(self):
        """守门测试自身要能抓到违规：埋一处 `"解答": 8` 必须被扫出来。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad_module.py")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write('PARAMS = {"title": "t",\n'
                         '          "space": {"选择": 0, "填空": 2, "解答": 8}}\n')
            hits = _positive_space_literals(bad)
            self.assertEqual(sorted((q, v) for _line, q, v in hits),
                             [("填空", 2), ("解答", 8)])
            good = os.path.join(tmp, "good_module.py")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write('PARAMS = {"mix": {"选择": 10, "填空": 8, "解答": 6},\n'
                         '          "space": {"选择": 0, "填空": 0, "解答": 0}}\n')
            self.assertEqual(_positive_space_literals(good), [],
                             "mix 里的题量不是留白，不能误报")

    def test_picker_default_space_all_zero(self):
        self.assertEqual(set(picker.DEFAULT_SPACE), QTYPE_KEYS)
        self.assertEqual(set(picker.DEFAULT_SPACE.values()), {0},
                         "picker.DEFAULT_SPACE 必须五题型全 0：%r" % (picker.DEFAULT_SPACE,))

    def test_paper_default_space_all_zero(self):
        self.assertEqual(set(paper.DEFAULT_SPACE.values()), {0},
                         "paper.DEFAULT_SPACE 必须全 0：%r" % (paper.DEFAULT_SPACE,))

    @unittest.skipUnless(os.path.exists(REAL_DB), "没有 data/app.db：跳过真实库留白检查")
    def test_real_24_item_paper_has_no_blank_space(self):
        conn = sqlite3.connect("file:%s?mode=ro" % REAL_DB.replace("\\", "/"), uri=True)
        tmp = tempfile.mkdtemp(prefix="paper_nospace_")
        try:
            params = {"title": "七年级数学 · 训练卷（24 题）",
                      "mix": {"选择": 10, "填空": 8, "解答": 6},
                      "scope": {"chapters": [], "kps": []},
                      "requirements": {"need_answer": True}, "seed": 20261031}
            manifest = picker.build(params, conn)
            self.assertEqual(manifest["params"]["space"], dict(picker.DEFAULT_SPACE))
            self.assertTrue(all(it["space_paras"] == 0 for it in manifest["items"]))
            built = paper.build_docx(manifest, tmp, name="nospace")
            health = paper.check_docx(built["docx"], manifest, built["expected"])
            self.assertEqual(health["space_paras_total"], 0,
                             "24 题卷不该有任何书写留白段（现在 %d 段）"
                             % health["space_paras_total"])
        finally:
            conn.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
