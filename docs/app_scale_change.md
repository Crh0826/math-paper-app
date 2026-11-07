# 应用规模化改造（t11 · I1b）：6 位题号 / tokens_json 回退 / 学科过滤

> 目标：把 E:\OCR\app 从「22,898 道数学题（5 位题号、每题一份原始 docx、单学科）」改造到
> 「15 万题（6 位题号、绝大多数没有原始 docx、7 个学科混库）」还能组卷、出卡、判卷。
>
> 本文件是改动清单 + 测试日志 + 遗留风险，由 `app-integrator` 在 2026-11-01 完成。

---

## 0. 一句话结论

| 改造项 | 结果 | 关键证据 |
|---|---|---|
| 1) 题号解码上限 99999 → 2^20-1（1,048,575） | ✅ 编码/出卡/判卷全链路兼容 6 位题号 | `tests.test_card.TestIdCodecSixDigit`（7 例）；`tests.test_grade.TestSixDigitChain` 端到端（组卷→出卡→判卷 3 道选择题全对） |
| 2) `paper.source_tokens` 缺 `<no>.docx` 时回退 `questions.tokens_json` | ✅ 无图题可排版；有图/空/坏 JSON/都没有 → 明确 `PaperError` | `tests.test_paper.TestTokensJsonFallback`（9 例） |
| 3) `scope` 增加 `subject_pk` / `subjects`，组卷页学科下拉默认必选一个 | ✅ 两学科混库时按学科只出本科题；跨学科选章节报错 | `tests.test_picker.SubjectScopeTest`（12 例）、`tests.test_server.TestSubjectPicker`（9 例） |
| 旧测试无回归 | ✅ 相关 6 组 416 例全绿（改动前 374 例全绿） | 见 §4 测试日志 |

---

## 1. 改动清单（源码）

> 备份（改动前）：`C:\Users\CRH\OneDrive\Desktop\题库\data\app_backup_pre_t11\`
> （card.py / paper.py / picker.py / server.py / grade.py + templates/ + `data/app.db`，app.db sha256
> `949F40F6859FF2BD70D1D903D08CD254955171BD1DD160BF5CE48898EBF0FDDC`）

### 1.1 `card.py` —— 题号位宽真正放开到 20 位

| 位置 | 改动 |
|---|---|
| 常量区 | `NO_BITS = 20` 注释更新：老 22,898 题（5 位）+ 15 万题（6 位）都编得下；新增 `NO_MIN_WIDTH = 5` 与 `fmt_no()` |
| `decode_id_bits()` | 范围检查 `1 <= v <= 99999` → **`1 <= v <= MAX_NO`（= 2^20-1 = 1,048,575）**；输出的题号字符串改用 `fmt_no()`（最小 5 位补零，10 万以上自然是 6 位） |
| `encode_id_bits()` | 行为不变（本来就允许到 `MAX_NO`），只更新文档串说明 6 位题号 |

要点：
* **旧号逐字节不变**：`00001..99999` 仍是 `"%05d"`，已打印的旧答题卡照样能判。
* 5×5=25 位标识格结构没动（20 位题号 + 5 位 CRC-5），所以**旧卡兼容逻辑（16 位 4×4）完全没碰**。
* 6 位题号每一位翻错仍然被 CRC 抓住（`TestIdCodecSixDigit.test_crc_still_catches_six_digit_corruption` 逐位验证）。

### 1.2 `grade.py` —— 判卷侧认 6 位题号，且「解出的号」必须能在库里落地

| 位置 | 改动 |
|---|---|
| 新增 `_same_no(a, b)` | 两个题号是否同一道题：先字符串比，再整数比（`"03370"` vs `"3370"`、`"100001"` vs `"100001"` 都算同一题） |
| `_check_manifest_ids()` | 原来用 `str(got).zfill(5) == str(expect).zfill(5)`，改为 `_same_no()`；文档串写明「题号位数不是判据，5 位/6 位走同一条 25 位链路」 |
| 新增 `_id_unreadable_unit()` | 把「标识不可读 → 进 unsure」的单元构造抽成一个函数（原来内联） |
| `_grade_by_ids()` | **6 位题号放宽后，被涂黑/采样跑偏的标识格也可能解出一个「像题号」的号**（旧代码靠 `>99999` 顺手挡掉了）。现在：解出的号在 `questions` 里查不到 → 仍记进 `unknown_nos`（排障用），同时产出一个 `unsure` 单元（reason=`题目标识不可读：解出的题号 N 在题库里查不到`），**不再静默丢弃**。全是库外题号时仍返回原来的 `unknown_questions` 错误。 |

> 这条是 `tests.test_grade.TestUnreadableIds.test_blacked_out_id_is_unreadable`（整块涂黑 → 必须 unsure）
> 在放宽范围后暴露出来的回归点：实测涂黑块会被 CRC 蒙对成 `1015808`，必须靠「库里有这题吗」兜底。

### 1.3 `paper.py` —— 缺原始 docx 时回退 `questions.tokens_json`

| 位置 | 改动 |
|---|---|
| 顶部 | `import json` |
| 新增 `set_tokens_db_path(path)` / `tokens_db_path()` | 指定/读取回退用的题库（默认 `paths.resolve_db()`；测试与打包可注入） |
| 新增 `clear_tokens_cache()` | 清 token 缓存（测试、题库文件补齐后重读） |
| 新增 `_db_tokens_json(no)` | 只读打开 DB 查 `SELECT tokens_json FROM questions WHERE no=?`；库不存在/锁库/无表 → 返回 `""`（不抛 sqlite 原始异常） |
| 新增 `_token_media_count(item)` | 读 `n_media`/`media_names`，判断这条 token 是不是带图 |
| 新增 `tokens_from_json(no, raw)` | `tokens_json` → `Token` 列表：`text`/`math`/无图 `table` 原样排版；`embed` 或带媒体的 `table` → `PaperError`（JSON 里只有图片名没有二进制，印出来会缺图）；其它 kind（importers 写的 `analysis` 等）跳过；解析不出内容 → `PaperError` |
| `source_tokens(no, tokens_json=None)` | 取数顺序：**① `<no>.docx`（保真首选，带媒体二进制）→ ② `questions.tokens_json`（只支持无图题）→ ③ 报 `PaperError`**，错误信息同时给出 docx 路径、DB 路径与「补 docx / 跑导入管道」两条出路 |

为什么不是「有图也硬排」：`build_db.py` 存的是 `Token.to_dict(with_data=False)`，`tokens_json` 里只有
`n_media`/`media_names`，没有图片字节。硬排的结果是卷面上少一张图（学生做题就废了），
所以按队长口径**无图题可用、有图题报错**。

### 1.4 `picker.py` —— `scope.subject_pk` / `scope.subjects` 学科过滤

| 位置 | 改动 |
|---|---|
| `normalize_params()` | `scope` 新增 `subject_pk`（单选，去空白）与 `subjects`（列表）；`subject_pk` 自动并入 `subjects`（并集语义）；两个都空 = 不限（老调用/老链接兼容） |
| 新增 `scope_subjects(scope)` / `_subject_clause(scope)` | 学科 SQL 片段（`AND subject_pk IN (...)`），与章节/知识点是 **AND** 关系 |
| `_validate_scope()` | ① 未知学科 → `ScopeError`（列出库里可用学科）；② 章节/知识点校验**限定在所选学科内**（选了语文却传数学章节 → 报错，不静默抽到别的科目） |
| `_scope_clause()` | 返回 `AND (<章节/知识点>) AND subject_pk IN (...)`；只有学科时也生效 |
| `_rows_by_nos()` | SELECT 增加 `subject_pk`（第 11 列） |
| `_picked_from_nos()` | `nos` 精确题集也要过学科闸门：给了学科却塞了别的学科的题号 → `SelectError` |
| `ScopeError` 文档串 | 改成「学科/章节/知识点不存在（或章节不属于所选学科）」 |

`build()` / `count_available()` / `pool_report()` 共用同一套 clause，所以「可出 N 题」与真正抽到的题永远同一口径。

### 1.5 `server.py` + `templates/` —— 组卷页学科下拉（默认必选一个）

| 位置 | 改动 |
|---|---|
| `read_kps(conn, subject_pk="")` | 给学科就只列该学科的章节/知识点（空 = 老行为） |
| 新增 `read_subjects(conn)` | `questions.subject_pk` 分组 + `subjects` 表名字/年级/版本 + 题量；`ORDER BY n DESC` → **题最多的学科排第一（= 下拉默认值）** |
| 新增 `subject_label(s)` | 下拉显示文本：「数学 · 七年级上 · 沪科版（22,898 题）」 |
| `_values_from_pairs()` | 收 `subject_pk`（单选）与 `subjects`（多选） |
| `_params_from_values()` | 写进 `params.scope.subject_pk` / `params.scope.subjects`（`subject_pk` 自动并入列表，落库参数自解释） |
| `_defaults()` | 补 `subject_pk` / `subjects` 默认值 |
| `_index_context()` | 读学科列表；**无 `subject_pk` 时自动选第一个**（默认必选一个）；`read_kps` 按学科过滤；`ScopeError` 兜底分两级（先清章节/知识点，再退「不限学科」），banner 里带上原始报错文本（脏参数能看出是哪个学科） |
| `_preview_ctx()` | 「同参数换一套」带上 `subject_pk` / `subjects`（换题不跨学科）；预览页显示学科 |
| `templates/index.html` | 新增「学科」卡片：`<select name="subject_pk" required>` + 默认选中 + 说明；顶部字段映射注释同步；范围提示改为「都不勾 = 该学科下不限制」 |
| `templates/preview.html` | reroll 表单带 `subject_pk`/`subjects` 隐藏域；参数区新增「学科」行 |

页面上「可出 N 题」、章节选择器、实际抽题**全部按同一个学科**算，所以不会出现「数学卷里冒出英语题」。

---

## 2. 兼容性

| 场景 | 行为 |
|---|---|
| 老 5 位题号（`00001..99999`） | 编码/解码/判卷/旧卡兼容**完全不变** |
| 只有一个学科的库（当前 E:\OCR\app\data\app.db） | 下拉只有一个选项（math7a），默认选中；题量/章节与改造前一致 |
| 老接口/老链接不带 `subject_pk` | `picker` 视为「不限学科」（与改造前行为一致）；组卷页会把下拉默认值带上，所以正常用户永远带学科 |
| `paper.source_tokens` 的旧调用方 | 签名向后兼容（`tokens_json` 是可选参数）；有 docx 时行为完全不变 |
| 已打印的旧答题卡 | 仍是 25 位 5×5 标识、5 位题号，照常判 |

---

## 3. 已知限制 & 给采集管道（t10 / 导入侧）的约定

1. **有图题没有原始 docx 就印不了**（设计如此，避免缺图卷面）。
   * `tokens_json` 里带 `kind="embed"` 或带媒体的 `table` → `PaperError`。
   * 组卷侧目前只有 `requirements.exclude_fig_missing`（题干提到「如图」且无图）会挡这类题；
     `fmt="embed"` 的题若被抽中，会在排版阶段报错。
   * **建议**：管道对无 docx 的图题，要么补图（裁图进 `media` 或用 OCR 出来的图片另存），
     要么在导入时把它们标成不参与自动组卷（例如写 `quality_flags` 或约定 `fmt` 前缀）。
2. **`kps.kp_id` 是全局主键，跨学科会撞**（实测 `UNIQUE constraint failed: kps.kp_id`）。
   * `importers/core.py` 的 `INSERT OR IGNORE INTO kps(kp_id, subject_pk, ...)` 在第二个学科写入同一个
     `kp_id` 时会**静默忽略**，那一科的 `n_questions` 永远不更新。
   * **建议**：导入时给 kp_id 加学科前缀（例如语文 `CN1.1`、英语 `EN1.1`），或把主键改成
     `(subject_pk, kp_id)`（改表结构的活没在本次做，属于管道/建库侧）。
   * 本次测试夹具就是这么处理的（语文用 `CN1.1`）。
3. **照片扫描（无卷号的 `scan_ids`）对密集标识格召回下降**：6 位题号的比特更密（≈10~12 个黑格 / 25），
   0.72 缩放 + 3% 透视 + JPEG70 的手机照片上，轮廓法可能漏检个别标识格。
   * 实测：卡面 PNG 直扫 5/5 全解出；同一张卡的合成照片 3/5（漏掉填空/解答那两题）。
   * **不会解出错号**（测试 `test_photo_scan_never_invents_wrong_no` 守着这点）。
   * 正规链路（卷子在 `papers` 表里 → 按 manifest 精确坐标判）**不受影响**，端到端测试全对。
   * 后续可优化：`_candidate_rects` 对密集块改用「列扫 + 黑框」优先，或提高工作分辨率。
4. **`taxonomy.all_kps()` 不带学科**：知识点名字跨学科可能同名（如「1.1」）。卷面知识点提示按 `kp_id`
   查名字，跨学科同号会显示错名 —— 与第 2 条同一个根因（kp_id 需要学科前缀）。

---

## 4. 测试日志

### 4.1 改动前基线（旧代码 + 旧测试）

```
cd E:\OCR\app && python tools\run_tests.py --groups 6 --only tests.test_card tests.test_paper tests.test_picker tests.test_grade tests.test_server tests.test_compose
[组 1/6] OK  Ran 51 tests   [组 2/6] OK  Ran 71 tests   [组 3/6] OK  Ran 105 tests
[组 4/6] OK  Ran 102 tests  [组 5/6] OK  Ran 38 tests   [组 6/6] OK  Ran 7 tests
总用时 120.8s，失败组数 0/6            （合计 374 例全绿）
```
（原始日志文件在任务期间被外部清理 `E:\OCR\_backups` 时删除，数字取自当时的完整输出；
可用 §4.2 的「改动前代码 + 新测试」树复现同样的旧行为。）

### 4.2 红：改动前代码 + 新测试（可复现）

用备份的改动前源码在 `E:\t11_pristine` 起一棵树（当前测试文件 + 旧源码），跑新增测试类：

```
cd E:\t11_pristine && python -m unittest tests.test_card.TestIdCodecSixDigit \
    tests.test_paper.TestTokensJsonFallback tests.test_picker.SubjectScopeTest \
    tests.test_server.TestSubjectPicker tests.test_grade.TestSixDigitChain
Ran 33 tests ... FAILED (failures=22, errors=6)
```
失败原因与三块改造一一对应：`decode_id_bits` 返回 `None`（6 位题号）、`paper.tokens_db_path`/`set_tokens_db_path`
不存在、`scope.subjects` 不支持、页面没有 `name="subject_pk"` 下拉。

### 4.3 绿：改动后（相关 6 组）

```
cd E:\OCR\app && python tools\run_tests.py --groups 6 --only tests.test_card tests.test_paper tests.test_picker tests.test_grade tests.test_server tests.test_compose
[组 1/6] OK  Ran 58 tests   [组 2/6] OK  Ran 80 tests   [组 3/6] OK  Ran 117 tests
[组 4/6] OK  Ran 107 tests  [组 5/6] OK  Ran 47 tests   [组 6/6] OK  Ran 7 tests
总用时 148.7s，失败组数 0/6            （合计 416 例全绿；比基线 +42 例新增）
```

逐模块对比：

| 模块 | 改动前 | 改动后 | 说明 |
|---|---|---|---|
| test_card | 51 | 58 | +7：`TestIdCodecSixDigit` |
| test_paper | 71 | 80 | +9：`TestTokensJsonFallback` |
| test_picker | 105 | 117 | +12：`SubjectScopeTest` |
| test_grade | 102 | 107 | +5：`TestSixDigitChain` |
| test_server | 38 | 47 | +9：`TestSubjectPicker` |
| test_compose | 7 | 7 | — |

> 注意：这套测试会写 `app/out/`、读 `app/data/app.db`，**不要两个测试进程并行跑**
> （并行跑会互相污染，出现过 5 例 test_grade 多值答案用例假红，单跑全绿）。

### 4.4 6 位题号端到端自测（组卷 → 出卡 → 判卷）

`tests/test_grade.py::TestSixDigitChain`（题号 100001..100005）：

1. **组卷**：`picker.build({"mix": {"选择":3,"填空":1,"解答":1}, "scope":{"subject_pk":"math7a"}}, 夹具库)`
   → manifest 里 5 个题号全是 6 位；
2. **出卡**：`card.build_card(manifest)` → 每页 `items[].id_bits` 25 位，`decode_id_bits` 还原 6 位题号；
3. **判卷**：合成手机照片（0.72 缩放 + 3% 透视 + JPEG70）→ `grade_image()`：
   `warp_ok=true`、3 个选择题作答单元全部 `correct`、判回来的题号都在 100001..100005 内。

自测脚本的实跑输出（识别回填后满分 7/7）：

```
组卷: 5 题 ['100001', '100002', '100003', '100004', '100005']
出卡: pages=1 decoded=['100001', '100002', '100003', '100004', '100005']
判卷: warp_ok=True score=7/7
      score_ok=True
```

### 4.5 全量测试

`python tools/run_tests.py --groups 6`（全部 45 个测试模块）：

```
[组 1/6] OK  Ran 142 tests   [组 2/6] OK  Ran 146 tests   [组 3/6] OK  Ran 212 tests
[组 4/6] OK  Ran 101 tests   [组 5/6] OK  Ran 288 tests   [组 6/6] OK  Ran 181 tests
总用时 626.8s，失败组数 0/6            （合计 1070 例全绿）
```

交付前又整体复跑了一次（所有源码 + CONTRACT.md / 本文件的最终版本，无并发干扰）：

```
[组 1/6] OK  Ran 142 tests   [组 2/6] OK  Ran 146 tests   [组 3/6] OK  Ran 212 tests
[组 4/6] OK  Ran 101 tests   [组 5/6] OK  Ran 288 tests   [组 6/6] OK  Ran 181 tests
总用时 576.4s，失败组数 0/6            （合计 1070 例全绿，exit 0）
```

### 4.6 原始日志

| 日志 | 位置 |
|---|---|
| 改动前代码 + 新测试（红） | `C:\Users\CRH\OneDrive\Desktop\题库\data\t11-before.txt` |
| 改动后相关 6 组（绿） | `C:\Users\CRH\OneDrive\Desktop\题库\data\t11-after2.txt` |
| 改动后全量（绿） | `C:\Users\CRH\OneDrive\Desktop\题库\data\t11-full-after.txt` |
| 交付前最终全量复跑（绿） | `C:\Users\CRH\OneDrive\Desktop\题库\data\t11-full-final.txt` |

（`E:\OCR\_backups` 在任务期间被外部清理删掉了，所以日志改放工作区 `data/`，与备份同处。）

---

## 5. 复现命令

```powershell
cd E:\OCR\app
# 相关 6 组（组卷/出卡/判卷/服务端/模板）
python tools\run_tests.py --groups 6 --only tests.test_card tests.test_paper tests.test_picker tests.test_grade tests.test_server tests.test_compose
# 全量
python tools\run_tests.py --groups 6
# 只看新增的三块
python -m unittest tests.test_card.TestIdCodecSixDigit tests.test_paper.TestTokensJsonFallback tests.test_picker.SubjectScopeTest tests.test_server.TestSubjectPicker tests.test_grade.TestSixDigitChain
```

回滚：把备份目录里的 5 个 `.py` + `templates/*.html` 覆盖回 `E:\OCR\app`；数据库无需回滚
（本次改动**不改表结构、不写生产库**，测试一律用临时库）。
