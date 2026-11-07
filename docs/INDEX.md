# 功能索引（docs/INDEX）

> 交付版本索引：每个功能给 **入口 URL**、**怎么点**、**截图**、**验收证据**。
> 截图两套：`docs/*.png` = 真库真服 + Edge headless 采集（`python app\docs\capture.py` 可重放）；
> `app/out/acceptance_v2/*.png` = 本轮独立验收（t61）当场截的页面。

---

## 0. 一套程序：本机 + 局域网（一个开关）

| 项 | 内容 |
|---|---|
| 说法 | 不再分「本地版 / 局域网版」两套程序：**同一份程序**，默认只允许本机访问；要用手机就在设置里打开开关 |
| 监听 vs 允许 | `server.host`（默认 `0.0.0.0`）只决定**监听哪些网卡**；能不能从局域网进来由 `server.lan_enabled`（运行时开关，默认关）决定 |
| 开关在哪 | 设置页 <http://127.0.0.1:8710/settings#lan> 的「局域网访问」卡片 → `POST /settings/lan`（勾上/关掉都即时生效，不用重启） |
| 截图 | [`20-lan-settings.png`](./20-lan-settings.png)（开关 + 局域网地址 + 访问码 + 防火墙命令） |
| 老习惯 | `start_lan.bat`（= `start.bat lan`）与 `MATHAPP_LAN=1` 都等价于「把开关打开」，不写配置文件 |
| 升级行为 | 老 `config.json` 没有 `server.lan_enabled` → 升级后**默认关**，需要手动点一次开关（README「从旧版升级」一节） |

---

## 1. 导入题库（Web 化管理题目）

| 项 | 内容 |
|---|---|
| 入口 | `http://127.0.0.1:8710/import`（导航「导入题库」；手机同局域网可直接开） |
| 三步 | ① 选文件（CSV / Excel / JSON / docx 目录）+ 填学科年级版本 → 「开始校验」；② 看**校验预览**（总行数 / 合法 / 问题行逐行原因 / 已存在跳过）；③ 勾「确认导入」→ 导入报告（新增 / 更新 / 跳过 / 失败 + 耗时 + 下载 JSON 报告） |
| 数据落点 | `data/app.db` 的 `questions`（`subject_pk` 分学科），幂等：同题号同内容 → 跳过；内容变了 → 更新 |
| 截图 | 上传/预览/报告三态：[`../app/docs/16-import-mobile.png`](./16-import-mobile.png)（375px 手机视图）、验收实测 [01_import_preview](../out/acceptance_v2/01_import_preview.png) / [02_import_report](../out/acceptance_v2/02_import_report.png) |
| 模板文件 | `templates/import.html`（**不是** import_upload.html / upload.html，仓库里没有这两个名字） |
| 接口 | `GET/POST /import`、`GET /import/report.json`（上一次报告机读版）、`GET /import/template.json`（示例 JSON） |
| 没有 JSON 怎么办 | 页面上方的折叠块「**没有 JSON？让 AI 帮你转**」：一键复制提示词 → 贴给任意 AI → 存成 `.json` 拖回来；提示词全文见 [`AI-转JSON-提示词.md`](./AI-转JSON-提示词.md) |
| 验收证据（t61） | 20 行 CSV（18 好 + 2 坏）→ 预览 `inserted=0`、`failed=2`（第 19 行缺 answer、第 20 行选择无选项）→ 确认 `inserted=18` → 库 `87,748 → 87,766`（+18）→ 再导一次 `inserted=0 / skipped=18`、行数不变。数字见 [`import_preview.json`](../out/acceptance_v2/import_preview.json)、[`import_confirm.json`](../out/acceptance_v2/import_confirm.json)、[`import_db_counts.json`](../out/acceptance_v2/import_db_counts.json) |

---

## 2. 改卷记录留痕与撤销（谁改了哪一空、能不能撤回）

| 项 | 内容 |
|---|---|
| 入口 | 判卷结果页 `…/submissions/{sid}` 下半部「**改卷记录**」时间线；全卷记录页 `…/records` |
| 能做什么 | 每次人工确认改判 → 一条记录（时间 / 操作者 / 批次 / 题号-空 / **改前 → 改后**）；每批可「**撤销这批**」→ 分数立刻按改前重算，撤销本身也留痕；「导出改卷记录 CSV」（UTF-8 BOM，Excel 直开）；`/records` 可按学号前缀过滤、导出全量 CSV |
| 数据落点 | `data/app.db` 的 `grading_edits`（`UNIQUE(batch_id, idx, blank)` 挡双击重复） |
| 截图 | [`03_grade_result_after_confirm`](../out/acceptance_v2/03_grade_result_after_confirm.png)（改判 2 空后：分数 + 改卷记录 2 条）、[`04_grade_result_after_undo`](../out/acceptance_v2/04_grade_result_after_undo.png)（撤销后：分数回退 + undo 留痕） |
| 模板文件 | 结果页 `templates/grade_result.html`、记录页 `templates/grade_records.html`（**不是** records.html） |
| 接口 | `GET /submissions/{sid}/edits`（JSON）、`GET …/edits.csv`、`POST …/edits/{batch_id}/undo`、`POST …/confirm`（一次提交一个 `batch_id`） |
| 验收证据（t61） | 判 12 个判分单元得 **6/12**、待确认 6 → 逐空改判 2 空（idx 7/8 第 1 空）→ **8/12**、记录 **2 条** → 原样重复提交 → 记录仍 **2 条**（幂等）→ CSV **3 行 = 表头 + 2 条** → 撤销该批 → **分数回到 6/12**、记录变 **4 条**（2 confirm + 2 undo）。数字见 [`edits_01_after_confirm.json`](../out/acceptance_v2/edits_01_after_confirm.json)、[`edits_03_csv.json`](../out/acceptance_v2/edits_03_csv.json)、[`edits_04_after_undo.json`](../out/acceptance_v2/edits_04_after_undo.json) |

---

## 3. 学生信息（学号涂卡 → 落库 → 页面/CSV）

| 项 | 内容 |
|---|---|
| 入口 | 卷面首页「**班级 / 姓名 / 学号**」手写栏（打印后手填）；答题卡 **v3** 顶部「**学号涂卡区**」（每位一行 0-9，涂黑对应数字）+ 姓名/班级手写框 |
| 自动识别 | 判卷时在复原图上读学号 → 写 `submissions.student_no`（读不出为空串，**绝不报错**；旧 v1/v2 卡没有该区 → 空） |
| 页面 | 结果页抬头「学生」行（学号 + 姓名）；读不出显示「学号未识别」并给**老师补录**入口（`POST /submissions/{sid}/student`）；记录页每行显示「学号 … 姓名」，可按学号前缀过滤、统计「涉及学生 N 人」；`/records.csv` 末尾两列 `student_no,student_name` |
| 截图 | v3 卡涂好的样张 [`05_v3_card_filled.png`](../out/acceptance_v2/05_v3_card_filled.png)、结果页 [`06_result_page_student`](../out/acceptance_v2/06_result_page_student.png)、记录页 [`07_records_page_student`](../out/acceptance_v2/07_records_page_student.png) |
| 模板文件 | 结果页 `templates/grade_result.html`（学生抬头）、答题卡由 `card.py` 直接绘制（无模板） |
| 接口 | `GET /submissions/{sid}`（显示）、`POST /submissions/{sid}/student`（补录）、`GET /records?student=2025`（过滤）、`GET /records.csv` |
| 验收证据（t61） | v3 卡涂 `20250317` → 模拟拍照判卷 → `submissions.student_no = 20250317`、`detail_json.student.no` 同步、结果页与记录页都能看到；把 manifest 的学生区去掉（= 旧 v2 卡形状）再判一次 → 标定正常、`student_no` 为空串、无 500。数字见 [`student_01_v3.json`](../out/acceptance_v2/student_01_v3.json)、[`student_02_legacy.json`](../out/acceptance_v2/student_02_legacy.json) |

---

## 4. 模板文件名核对（URL → `templates/*.html`）

> t60 报过一次「inScope 写成了不存在的 `records.html`，仓库里根本没有」——以后按这张表找文件，别再猜名字。

| URL | 模板文件 | 说明 |
|---|---|---|
| `/` | `templates/index.html` | 组卷首页 |
| `/papers` | `templates/papers.html` | 我的卷子 |
| `/papers/{pid}` | `templates/preview.html` | 卷子预览 / 打印件 |
| `/import` | **`templates/import.html`** | 导入题库（上传 + 校验预览 + 报告三态同页）；**没有** `import_upload.html` / `upload.html` |
| `/submissions`（GET） | `templates/grade_upload.html` | 上传判卷页 |
| `/submissions`（POST 失败） | `templates/grade_error.html` | 判卷失败页（含四角诊断图） |
| `/submissions/{sid}` | **`templates/grade_result.html`** | 判卷结果页（分数卡 / 逐题 / 改卷记录 / 学生抬头 / 训练入口） |
| `/submissions/{sid}/why/{idx}` | `templates/grade_why.html` | 单题证据抽屉页（无 JS 时可用） |
| `/records` | **`templates/grade_records.html`** | 改卷记录时间线 / 撤销 / 导出 CSV；**没有** `records.html` |
| `/train` | `templates/train.html` | 训练卷 |
| `/guide` | `templates/guide.html` | 使用指南 |
| `/health` | `templates/health.html` | 自检 |
| `/settings` | `templates/settings.html` | 设置（含「局域网访问」开关，提交到 `POST /settings/lan`） |
| `/pin` | `templates/pin.html` | 访问码 |
| 错误页 | `templates/error.html` / `templates/_banner.html` | 通用错误 / 顶部提示条 |

---

## 5. 其它入口（速查）

| 页面 | URL | 截图 |
|---|---|---|
| 组卷 | `/` | `docs/01-compose.png` |
| 我的卷子 | `/papers` | `docs/02-papers-list.png` |
| 卷子预览 / 打印件 | `/papers/{pid}` | `docs/05-paper-preview.png`、`docs/05b/05c` |
| 上传判卷 | `/submissions` | `docs/06-grade-result.png` |
| 判卷记录 | `/records` | `docs/06`、本轮 `07_records_page_student.png` |
| 训练卷 | `/train` | `docs/07-train-paper.png` |
| 使用指南 | `/guide` | `docs/03-guide.png` |
| 自检 | `/health` | `docs/04-health.png` |
| 设置（识别后端 / Key / 局域网访问开关） | `/settings`（开关锚点 `/settings#lan`） | `docs/14-settings.png`、[`20-lan-settings.png`](./20-lan-settings.png) |
| 导入题库 | `/import` | `docs/16-import-mobile.png`、本轮 `01/02_import_*.png` |

> 完整验收报告见 [`../VERIFY.md`](../VERIFY.md)：§17 = 本轮三项（导入 / 改卷记录 / 学生信息）实测，§15 = 定版八项，§16 = 省纸模式。

---

## 6. 顺序管理（学科 / 单元排序）与剩余题量

| 项 | 内容 |
|---|---|
| 入口 | `http://127.0.0.1:8710/manage/order`（导航「顺序管理」；`?subject_pk=math8a` 直接看某学科的单元） |
| 怎么点 | 左栏改**学科**顺序、右侧改该学科**单元**顺序（↑↓ 即时换位）→「保存顺序」；「恢复默认」按题库原始顺序重排。改完刷新页面仍是新顺序，组卷页的学科下拉与单元复选框立刻跟随 |
| 存哪 | `subjects.sort_order` + `chapter_order(subject_pk, chapter, sort_order)`；只排「有题」的学科 |
| 截图 | [`../out/acceptance_v4/order_subjects_1080.png`](../out/acceptance_v4/order_subjects_1080.png)、[`order_375.png`](../out/acceptance_v4/order_375.png)（375px 无横向溢出） |
| 验收证据 | `VERIFY.md` §19.8.1；数据 `../out/acceptance_v4/order_round2.json`（含库里 `sort_order` 实际值） |

### `/api/available`（组卷页「本范围剩余 N 题」的后端）

| 项 | 内容 |
|---|---|
| 入口 | `GET /api/available?subject_pk=math7a[&chapters=<单元>][&kps=<kp>]`，返回 `{ok, subject_pk, chapters, kps, total, by_qtype{选择/填空/解答/判断/其他}, elapsed_ms}` |
| 谁在用 | 组卷页 `<http://127.0.0.1:8710/>` 的「本范围剩余 N 题」行 + 每个题量框下的超量红字（300ms 防抖后调本接口）；无 JS 时用「查询剩余题数」按钮走 `/?check=1` |
| 错误口径 | 缺/错学科 → 400 中文「没有这个学科：…（题库里查不到）」；章节不在库里 → 400 并列出可用章节；空 `chapters` = 整册 |
| 性能 | 命中 TTL 缓存 ~5ms；冷路径：整册 **188.9ms**、单章 448ms（超 300ms，见 VERIFY §19.8.2） |
| 验收证据 | `VERIFY.md` §19.8.2；数据 `../out/acceptance_v4/available_round2.json`（SQL 直查 vs picker 计数对照） |
