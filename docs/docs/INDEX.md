# E:\OCR 目录说明（2026-11-01 两次清理后）

```
E:\OCR
├── app\        程序本体（源码 / 模板 / 测试 / 配置 / 数据库 / 产物）
├── qbank\      题库：22,898 份题目 docx（只读，别手改）
├── dist\       打包产物：数学题库工作台-v1.2.4.zip + 解压好的同名目录
├── repo\       开源仓库工作副本（推 GitHub 用；不含题库、数据库、密钥）
├── docs\       交付文档（本文件所在处）
├── promo\      宣传片（成片 + 封面 + B 站文案 + 构建脚本）
├── .tools\     PortableGit（只用于推仓库，不需要推就可以整个删掉）
└── README.md   入口说明
```

合计约 **1.4 GB**。历史产物、中间件、旧版本包、测试判卷输出都已删除（两轮共释放约 **7.6 GB**）。

## 每个目录里是什么

| 目录 | 关键内容 | 说明 |
|---|---|---|
| `app\` | `server.py` `paper.py` `card.py` `grade.py` `ocr.py` `normalize.py` `picker.py` `train.py` | 入口：`python -m uvicorn --app-dir E:\OCR\app server:app --host 0.0.0.0 --port 8710` |
| | `config.json`（573B） | **含 OCR token / API key，不要外传**；保存设置时自动备份 `.bak` |
| | `data\app.db`（120MB） | 题库 + 卷子 + 判卷记录；便携版共用同一份 |
| | `out\`（29MB） | 生成的卷子 / 答题卡 / 判卷裁切图（按卷号分目录） |
| | `tests\`（38 个文件） | 1,000+ 条自动化测试 |
| | `CONTRACT.md` / `VERIFY.md` | 接口契约（单一事实来源）/ 验收记录 |
| `qbank\` | `00001.docx … 22898.docx` | 只读题库；换题库就换这里，然后 `python build_db.py --fresh` |
| `dist\` | `数学题库工作台-v1.2.4.zip` | 便携版 219.9MB，解压双击即用（和本地版共用配置与数据库） |
| | `数学题库工作台\` | 已解压好的那份，直接运行 `数学题库工作台.exe` |
| `docs\` | `INDEX.md`（本文件）、`交付说明*.md`、`release-v1.2.*.md` | 所有给人看的文档 |
| `promo\` | `out\promo_bilibili.mp4`、`cover.jpg`、`bilibili.md` | 宣传片与投稿材料 |

## 常用命令

```bat
:: 启动（局域网）
cd /d E:\OCR\app
python -m uvicorn --app-dir E:\OCR\app server:app --host 0.0.0.0 --port 8710

:: 启动便携版（同一套数据）
E:\OCR\dist\数学题库工作台\数学题库工作台.exe

:: 全量测试（分组跑，避免单次 15 分钟超时被杀）
python tools\run_tests.py --groups 6

:: 换题库后重建数据库（读 config.paths.qbank = E:/OCR/qbank）
python build_db.py --fresh --report
python quality.py

:: 出便携版（约 70 秒，收尾自动 --check）
powershell -File app\build_portable.ps1 -Version v1.2.5

:: 清垃圾（幂等，可随时跑）
python app\tools\cleanup_junk.py
python app\tools\cleanup_round2.py
```

## 约定（别再乱放）

1. **密钥只在 `app\config.json`**，仓库已 `.gitignore`；包里出厂是脱敏的（`backend=manual`，无 token）
2. 改设置走网页 `/settings`，不要手改 json；`paddle_vl` 的 model 必须是 `PaddleOCR-VL-*`
3. 文档进 `docs\`，一次性脚本进 `app\tools\`，产物进 `dist\`，临时实验进 `app\out\_*`（每次清理会删）
4. `repo\` 是开源仓库副本：改完代码要同步过去再 `git push`，**不要把 config.json / app.db / qbank 拷进去**
