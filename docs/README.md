# 界面截图与降级证据（t12）

这些图片是**真库真跑**出来的，不是设计稿：`python app/docs/capture.py` 会起真服
（`uvicorn server:app --port 8710`）、用真库组卷、拿 `card.py` 出的答题卡 PNG 按 manifest
坐标把错误选项涂黑当「照片」上传、走完整判卷与训练卷流程，再用 Edge headless 截图。

| 文件 | 内容 |
|---|---|
| `01-compose.png` | 组卷首页：题量 / 「可出 N 题」/ 章节知识点 / 组题口径 |
| `02-papers-list.png` | 我的卷子：每行「判卷 N 次」、删除策略说明 |
| `03-guide.png` | 使用指南：打印要求（100% / 不要适应页面）、拍照要求（四角入画 / ≤30° / 关闪光灯）、错误速查 |
| `04-health.png` | 自检页：题库、Word、OCR、产物占用、判卷证据占用 |
| `05-paper-preview.png` | 卷子预览：打印须知 + 三个下载按钮 + 卷面/答题卡预览 + 题目清单 + 参数 |
| `05b-print-page1.png` | `print.pdf` 第 1 页（卷面）：页脚「卷面 · 第 N 页 / 共 A 页」 |
| `05c-print-page2.png` | `print.pdf` 第 2 页（答题卡）：页脚「答题卡 · 第 M 页 / 共 B 页 ·（卷面续页 P{A+M}）」 |
| `06-grade-result.png` | 判卷结果：分数卡 + 逐题判定（含裁切图、OMR 候选、为什么）+ 待确认 + 训练入口 |
| `07-train-paper.png` | 训练卷：由错题抽同类题（原题不重复），带答题卡 |
| `08-degraded-word.png` | Word 缺失：预览页降级横幅 + 只给答题卡 PDF + 卷面预览空态 |
| `20-lan-settings.png` | 设置 →「局域网访问」：开关（默认关）、当前状态「只允许本机」、局域网地址、访问码、防火墙命令 |
| `19-avail-line.png` | 组卷页「本范围剩余 343 题（选择/填空/解答）」实时提示 + 「查询剩余题数」退化按钮 |
| `16-import-mobile.png` | 导入页 375px 手机视图（拖放区/字段/按钮都排得下，无横向滚动） |
| `15-ocr-notice.png` | 首页顶部「尚未配置识别服务」横幅（manual 后端时）+「去设置」入口 |
| `14-settings.png` | 设置页：后端下拉、Key 只显尾号、测试连接、当前路径 |
| `13-seed-reroll.png` | 预览页「换一套题」按钮 + 参数区「随机种子 226007（填这个数可以复现）」 |
| `12-lan-qr.png` | 局域网模式下的首页：手机扫码入口（二维码 + 局域网地址 + 访问码提示） |
| `17-grade-records.png` | 结果页「改卷记录」时间线：改前→改后、批次、撤销入口（来源：`out/acceptance_v2/03_grade_result_after_confirm.png`） |
| `18-student-info.png` | 结果页学生抬头：答题卡 v3 学号涂卡识别出的学号 + 姓名（来源：`out/acceptance_v2/06_result_page_student.png`） |
| `10-layout-form.png` | 组卷表单「选题要求」区：省纸模式复选框（默认勾上）与文案 |
| `11-layout-compact.png` | 省纸版面的预览页：题更密、页数更少（24 题 = 卷面 5 页 + 卡 3 页） |
| `09-degraded-db.png` | 数据库缺失：首页 503 友好页（错误码 `db_missing` + 三条「怎么办」） |
| `DEGRADED.md` | 三种降级场景的实测结论（Word 缺失 / 数据库缺失 / 端口占用），由脚本自动写 |

重放：

```bat
cd /d E:\OCR\app
python app\docs\capture.py
```

脚本结束会删掉自己造的那几份卷子与判卷记录（训练卷硬删；演示卷软删后连证据一起清），
所以跑完 `app/out` 里不会留下截图用的垃圾；截图本身保留在 `app/docs/`。
