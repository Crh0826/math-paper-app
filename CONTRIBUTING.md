# 贡献指南

先谢谢你愿意花时间。这是个自用工具长出来的项目，代码不完美，但**测试是硬的**——改之前先看一眼这部分。

## 环境

```bat
git clone https://github.com/Crh0826/math-paper-app.git
cd math-paper-app
python -m pip install -r requirements.txt
copy config.example.json config.json
```

- Windows + Microsoft Word（卷面转 PDF 靠 Word COM；没 Word 会走降级）
- Python 3.11+（本机开发环境 3.14）

## 题库（可选）

仓库不含题库。要用真实数据跑，把 docx 放到 `E:\OCR\`（或 `qbank/`）后：

```bat
python build_db.py --fresh
python quality.py
```

没有题库也能跑大部分测试（少数用例会 skip）。

## 开发约定

1. **先写测试**：`tests/test_<模块>.py`，先跑红，再实现，再跑绿。
2. **跑全量**：`python -m unittest discover -s tests`，提交前必须全绿。
3. **不要改 `config.json`**：里面有云端 OCR token，已在 `.gitignore`。
4. **媒体铁律**：任何带 `r:embed / r:id / r:link` 的 XML（**包括 `w:tbl` 表格**）插入新文档前，必须做 rId 重映射并注册二进制，否则 Word 判定文件损坏。
5. **跨盘落位**：Windows `os.replace` 跨卷必报 `ERROR_NOT_SAME_DEVICE(17)`，要 `copyfile` 到目标卷再本卷 `os.replace`。
6. 中文输出统一 `PYTHONIOENCODING=utf-8`，文件 UTF-8 无 BOM。

## 提 PR

- 一个 PR 只做一件事，标题写清「模块: 做了什么」
- 描述里给出**实测数字**（不是「应该能行」）：改了哪几页、判对率变化、测试条数
- 涉及判卷口径的改动，请附一个真实/合成样例的前后对比
- 如果改了答题卡版式，必须同步 `manifest` 里的坐标并在描述里说明旧卡是否兼容

## 提交信息

```
<模块>: <一句话>

<可选的细节>
```

模块名：`picker` / `paper` / `card` / `grade` / `ocr` / `normalize` / `train` / `server` / `docs`
