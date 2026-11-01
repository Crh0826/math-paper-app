# -*- coding: utf-8 -*-
"""把两个测试里的 JSONL 常量改成跟 build_db.JSONL（走 paths）走。"""
import io

NEW = (
    "try:\n"
    "    import build_db as _bdb\n"
    "    JSONL = _bdb.JSONL          # 跟着 paths 走（题库搬进 qbank 后不再写死 E:\\OCR\\dedup）\n"
    "except Exception:               # noqa: BLE001\n"
    "    JSONL = r\"E:\\OCR\\dedup\\data\\questions.jsonl\""
)
OLD = 'JSONL = r"E:\\OCR\\dedup\\data\\questions.jsonl"'

for p in (r"E:\OCR\app\tests\test_families.py", r"E:\OCR\app\tests\test_taxonomy.py"):
    s = io.open(p, encoding="utf-8").read()
    if OLD not in s:
        print(p, "没找到旧常量，跳过")
        continue
    io.open(p, "w", encoding="utf-8").write(s.replace(OLD, NEW, 1))
    print(p, "已改")
