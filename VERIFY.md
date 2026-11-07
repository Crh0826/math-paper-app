# VERIFY · 独立验收报告（第一版）

> 验收方：**verifier**（独立端到端验收，不写产品代码）
> 报告时间：**2026-10-31 15:20–17:00（Asia/Shanghai）**；**定版实测 16:55:34→16:59:27**（全量 760 用例 OK）+ 验收套件 34 条全绿；定版八项见 **§15**
> 覆盖范围：**数据层 / 卷面层 / 判卷层**（任务 t19）。UI 层（t10/t11/t22）与最终全链路（t13）**不在本版范围**。
> 验收脚本：`app/tests/test_acceptance.py`（**verifier 另写**，不复用 `tests/test_<module>.py` 的样本与断言，
> 避免"用被测方的用例验被测方"的同源误差）
> 证据目录：`app/out/acceptance_v1/`（每次运行产物落在 `run_<pid>/`，再拷回稳定路径，避免多人并发互相覆盖）

---

## 0. 结论总表

| # | 验收项 | 结论 | 关键实测（**t13 定版 16:59**，详见 §15） |
|---|---|---|---|
| 1 | 全量 unittest | **通过（定版全绿）** | 2026-10-31 18:18:40：**799 用例 / 305.481 s / skipped=1 / failures+errors=0**（exit 0）。历史：16:59=760 用例 0 红 → 省纸模式（§16）落地后 799 用例 0 红 |
| 2 | DB 硬指标 | **通过** | 裁剪后 **12,280** 题（= 有答案数，`without_answer=0`）/ no 唯一 / 题族 **2,091**（≥3 题 **1,310**）/ 知识点 **34** / `mastery` 表在 / `integrity_check=ok` |
| 3 | 归一化 | **通过** | 等价对 **106/106 = 100%**；不等价对 **41/41 = 100%**（t26 的受控求值兜底 16:06 落地，原来 8 条缺口全部补上，回归项仍留在用例里） |
| 4 | 组卷 `picker.build` | **通过** | 同 seed 逐题一致 / 换 seed 重复率 **0%** / scope 过滤生效（含 `x.0` 章级展开）/ 题量不足抛 `InsufficientQuestions` / manifest 字段齐全 / `nos` 保序 / `exclude_nos` 生效 |
| 5 | 卷面保真 | **通过**（P0-B 已修复并回归） | 24 题逐题比对 **missing=0、extra=0**：oMath **50/50**、object **36/36**、drawing **3/3**、媒体引用 **39/39**；**悬空 rId = 0**；含图表格题单独回归 **5/5 字节全在**；PDF **3 页**（345,857 B）每页有墨 |
| 6 | 答题卡几何 | **通过** | 4 页 / 每页 ArUco **0/1/2/3 全检出**（偏差 0.5 px）/ QR 解出 `{pid,p}` / **67 个格子 0 越界 0 重叠** / 21 个作答格与答案空对得上 |
| 7 | 拍照判卷 | **通过** | 本层验收：标定 **10/10**、QR **10/10**、选择题判分一致 **30/30 = 100%**、unsure **0**、pending 55 个且裁切图全在；另有 t31 的 **100 次**合成基线（标定 / QR / 选择判分 / 裁切全 100%，p95 0.06 s），见 §14 |

**补充（t32）**：打印链路（落库 → 合并打印件 → 页脚）已于 16:10 复验 **全部通过**，详见 §12。

**本版有 2 条交付级缺陷，均已修复并由 verifier 复核：**

* **P0-A** PDF 跨盘落位失败（`os.replace` 跨卷）→ 15:24 修，复核通过（§9-F1）。
* **P0-B** 「表格里带图」的题让卷面 docx 出现悬空 rId、Word 判「文件已损坏」、PDF 导不出 → 15:40 修，复核通过（§9-F6）。

剩余待办（不阻断本层验收）：**语料「如图无图」2,932 题**（R1，组卷已默认排除）、
**手写识别准确率未用真实样本验证**（R3，t13 必测）。

---

## 1. 全量 unittest

```powershell
cd E:\OCR
python -m unittest discover -s app\tests
```

三次快照（并行走开发，用例数在涨）：

| 项 | 快照 1（15:17:15） | 快照 2（15:25:52） | 快照 3（15:42:58） |
|---|---|---|---|
| 用例数 | 461 | 578 | **635** |
| 耗时 | 65.692 s | 128.508 s | **127.888 s** |
| 跳过 | 1 | 1 | 1 |
| failure / error | 0 / 8 | 8 / 5 | **9 / 4** |
| exit code | 1 | 1 | 1 |
| 日志 | 快照日志已归档 | 快照日志已归档 | 快照日志已归档 |

**快照 4（15:49:56→15:53:23）**：**684 用例 / 199.509 s / 1 跳过 / failures=19 + errors=2**，exit 1。
红点全部转移到了 t11/t22 正在写的 UI 链路：`test_soft_delete`（软删策略 7 条）、`test_train_flow`（训练闭环 10 条）、
`test_packaging.test_every_package_is_importable_here`（1 条）+ `test_train_flow.TestFullChain.test_train_paper_preview_renders`（1 error）。
另有 `test_acceptance.T6GradePhotos` 的 setUpClass error —— **是我的验收脚本自身的问题**（临时库用了固定路径
`out/acceptance_v1/tmp_grade/grade.db`，被并发跑的另一个进程锁住，WinError 32）；已改成进程唯一路径
（`run_<pid>/grade/grade_<pid>.db`），15:54 复跑**验收 26/26 OK**。

**快照 5（16:12:52→16:18:41，趋势采样，非定版）**：**725 用例 / 347.9 s / 1 跳过 / failures=1 + errors=2**，exit 1。只剩 3 条红，按模块归组：

| 模块 | 条数 | 明细 |
|---|---|---|
| `test_render.CrossProcessLockTest` | **2 error** | `test_lock_blocks_another_process`、`test_lock_is_reentrant_and_keeps_lock_file`：`WordUnavailable("busy")：另一个转换正占用 Word，5 秒内没拿到跨进程锁` —— **环境性**（并发跑全量时别处也在导 PDF），captain 已让 render-engineer 改成独占锁 + 唯一锁文件 |
| `test_grade_routes.TestConfirmJson` | **1 failure** | `test_json_changes_score_and_returns_rows`：`assertEqual(2, data["pending"])` 实际 **3**（逐空改判后聚合没重算 / 断言口径，owner 定位中） |
| 其余全部模块 | **0** | 含 `test_normalize` / `test_grade` / `test_train_flow` / `test_paper` / `test_picker` / `test_acceptance` / `test_dedupe` / `test_db` / `test_quality` |

**趋势判断**：与快照 4（684 用例 / 19F+2E）相比，用例 +41 而红点 21 → 3，**明显收敛**。
日志：`app/tools/logs/trend_1612.log`。

快照 3 的 13 条（逐条归因，**没有一条是数据/卷面/判卷层的断言失败**）：

| 用例 | 现象 | 归因 |
|---|---|---|
| `test_db.TestBuildDb.test_kp_populated` | 断言 kps/题数旧值 | **DB 刚裁到 12,280**，旧断言未同步 |
| `test_db.TestBuildDb.test_report_written_and_consistent` | `build_report.json` 与库不一致 | 同上（裁剪进行中） |
| `test_quality.TestQualityScan.test_scan_result_shape` | 期望 13 道样本的统计 | quality 侧随裁剪调整中 |
| `test_quality.TestRealLibrary.test_full_scan_speed_and_total` | 期望 22,898 | 同上（库里现在 12,280） |
| `test_quality.TestRealLibrary.test_known_bad_samples_from_real_db` | 已知坏题样本被裁掉 | 同上 |
| `test_dedupe.TestRealLibrary.test_full_scan` | 查重全库计数 | t25 红阶段 + 裁剪 |
| `test_dedupe.TestRealLibrary.test_known_dups_and_non_dups_from_real_db` | 同上 | 同上 |
| `test_paper.RelationshipGateTest.test_dangling_ref_raises` | 新闸门（悬空 rId）用例与实现未对齐 | paper 侧 15:4x 正在加同一类闸门 |
| `test_paper.RelationshipGateTest.test_health_reports_relationship_stats` | 同上 | 同上 |
| `test_paper.RelationshipGateTest.test_00568_table_images_regression` (error) | setUp 失败 | 同上（新文件刚落地） |
| `test_paper.WordOpenTest.test_corrupt_docx_is_reported_and_gated` (error) | setUp 失败 | 同上 |
| `test_paper.WordOpenTest.test_good_paper_opens_in_word` (error) | setUp 失败 | 同上 |
| `test_paper.RenderFallbackTest.test_render_failure_falls_back_and_warns` (error) | setUp 失败 | 同上 |

**快照 6（16:55:34→16:59:27，定版）**：**760 用例 / 232.804 s / skipped=1 / failures+errors=0**，exit 0，**全绿**。日志归档 `app/tools/logs/full_t13.log`（注意：PowerShell `>` 重定向写的是 UTF-16LE，用 Select-String 或按 utf-16 解）。

**结论：通过（定版全绿）**。历史红点（快照 1-5 的 loader 错误、DB 裁剪旧断言、新闸门红阶段、Word 跨进程锁、grade_routes 断言差 1）已全部清零；
与本任务的三层无关；本任务新增的 **26 条验收用例**在这份全量里**全绿**。
> 复现：`python -m unittest discover -s app\tests 2>&1 | Select-String "^(Ran|FAILED|ERROR)"`
> 期望 `OK`；实际（15:45）`FAILED (failures=9, errors=4, skipped=1)`。

---

## 2. 数据层硬指标

```powershell
cd E:\OCR
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T1DataLayer -v
```

证据：`app/out/acceptance_v1/data_layer.json`、`data_layer_schema.json`、`data_layer_tokens.json`

**口径**：题库已按 captain 决定裁掉「无答案题」（22,898 → 12,280）。本节断言**两种口径都认**：
`without_answer==0` 时按裁剪后断言（questions==12,280），否则按裁剪前断言（22,898 / 12,280 / 2,671 / 1,760 / 34）。
下面数字是**裁剪后**实测：

| 指标 | 期望 | 实测 | 结论 |
|---|---|---|---|
| `questions` 行数 | 裁剪后 = 有答案数 | **12,280** | 通过 |
| `no` 唯一 / 5 位数字 | 全部 | **12,280 / 12,280** | 通过 |
| 有答案 | 12,280 | **12,280**（`without_answer=0`） | 通过 |
| `is_empty=1` | — | 96 | 记录 |
| 题族 `families` | 与题目表对账 | **2,091**（= `questions.family_key` 去重数，5,815 道题带族键） | 通过 |
| ≥3 题的族 | — | **1,310** | 记录 |
| 知识点 `kps` | 34 | **34** | 通过 |
| 每题都有 kp_id / chapter | 0 缺 | **0 / 0** | 通过 |
| `PRAGMA integrity_check` | ok | **ok** | 通过 |
| 表齐全 | §1 六张 + `quality_flags` | questions / families / kps / papers / submissions / mastery / quality_flags / meta | 通过 |
| 列齐全 | 契约逐列 | 六张表逐列全在（`data_layer_schema.json`） | 通过 |
| 题型分布 | ⊆ {选择,填空,解答,判断,其他} | 选择 5,013 / 解答 4,406 / 填空 2,797 / 其他 64（**判断 0**，原 2 道都无答案被裁） | 通过 |
| `tokens_json` 可解析 | 100% | 抽样 **1,000** 题，失败 **0**；kind 分布 text 5,817 / math 2,501 / embed 2,572 / table 43 | 通过 |
| `mastery` / `papers` / `submissions` | 表在 | 在（papers/submissions 空，见 §9-F4） | 通过 |

> **题族数字的口径（重要，别误读成"族缩水 20%"）**：建库时全量语料是 **2,671 族（≥3 题 1,760）**；
> 现在库里只剩 **有答案的 12,280 题**（无答案题按 captain 决定裁掉），所以族数跟着变成 **2,091（≥3 题 1,310）**。
> 也就是说：**2,671 → 2,091 是"题库裁剪"的结果，不是题族生成逻辑退化**（族算法本身在 M0 已冻结并全绿）。
> 同理 `kps=34` 不受影响（知识点体系是独立的表）。

---

## 3. 归一化 `normalize.is_equivalent`

```powershell
cd E:\OCR
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T2Normalize -v
```

证据：`app/out/acceptance_v1/normalize_pairs.json`（含 8 条失败的原文与返回原因）

verifier 另写的 **106 组等价对 + 41 组不等价对**（不复用 data-engineer 的用例）：

| 组别 | 数量 | 判对 | 判对率 |
|---|---|---|---|
| 等价对（应 True） | **106** | **106** | **100%** |
| 不等价对（应 False） | **41** | **41** | **100%** |

> 复测时间 **16:07**，`normalize.py` 28,953 B / sha1 `b79a95c96f`（t26 的「受控求值兜底」已落地）。
> 下面这 8 条是 15:2x 时的漏判，现在**全部判对**，仍保留在 `EQ_PAIRS` 里做回归：
> `√8↔2√2`、`2√2↔√8`、`|−2|↔2`、`|2|↔2`、`∛8↔2`、`三分之一↔1/3`、`零点五↔0.5`、`二分之一↔1/2`。

通过的覆盖面（抽查）：分数↔小数（0.5↔1/2、0.125↔1/8）、扩分（3/4↔6/8）、正负号与 Unicode 负号（−1↔-1）、
全角数字（１２↔12）、百分数（25%↔0.25、0.5%↔0.005）、科学计数法（1.2×10³↔1200、2.5×10^-2↔0.025）、
单位（3厘米↔3cm、5千克↔5kg）、上下标（x²↔x^2）、全角等号（x＝4↔x=4）、多空标点（3，5↔3,5、3;5↔3；5）、
区间（1<x<2↔(1,2)、2≤x≤5↔[2,5]）、选择（a↔A、A.↔A、Ａ↔A）、判断（√↔对、×↔错、正确↔对）、
OCR 尾缀（3。 / 3． / 3、 / " 3 " ↔ 3）。

**历史：8 条漏判（15:2x 时；16:07 复测已全部修复）**

| # | 学生答案 | 标准答案 | 实际 | 类别 |
|---|---|---|---|---|
| 1 | `√8` | `2√2` | False | 根式化简 |
| 2 | `2√2` | `√8` | False | 根式化简 |
| 3 | `\|−2\|` | `2` | False | 绝对值求值 |
| 4 | `\|2\|` | `2` | False | 绝对值求值 |
| 5 | `∛8` | `2` | False | 立方根 |
| 6 | `三分之一` | `1/3` | False | 中文数字 |
| 7 | `零点五` | `0.5` | False | 中文数字 |
| 8 | `二分之一` | `1/2` | False | 中文数字 |

**结论：通过**（16:07 复测 106/106 + 41/41）。t26 的受控求值兜底（白名单 AST，不用 eval）覆盖了
根式化简 / 绝对值 / 立方根 / 中文数字四类写法；这 8 组仍固化在用例里防回归。
> 复现：`python -c "import sys;sys.path.insert(0,r'E:\OCR\app');import normalize;print(normalize.is_equivalent('三分之一','1/3','填空'))"` → `(True, ...)`

---

## 4. 组卷 `picker.build`

```powershell
cd E:\OCR
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T3Picker -v
```

证据：`app/out/acceptance_v1/picker.json`、`picker_scope.json`、`picker_insufficient.json`

参数：`mix={选择:10,填空:8,解答:6}`、`requirements.need_answer=true`、`exclude_empty=true`、`seed=42`。

| 检查 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 出卷耗时 | — | **0.33–0.52 s** | 记录 |
| 题量 / 分布 | 24（10/8/6） | **24**，`{选择:10, 填空:8, 解答:6}` | 通过 |
| 同参数同 seed | 逐题一致 | **24/24 题号与顺序完全一致** | 通过 |
| `idx` | 1..24 连续 | 1..24 | 通过 |
| 换 seed 重复率 | < 20% | **0/24 = 0.0%**（seed 42 vs 43） | 通过 |
| 可用池（现算） | — | 选择 **4,171** / 填空 **2,238** / 解答 **3,542** | 记录 |
| `scope.kps=["1.2"]` | 只出 1.2 | 24 题 `kp_id` **全 = 1.2** | 通过 |
| `scope.chapters=["第1章 有理数"]` | 只出该章 | 24 题**全在该章** | 通过 |
| `scope.kps=["3.0"]`（章级） | 展开整章 | 出现 3.0 / 3.1 / 3.2 / 3.3 / 3.4 / 3.5 / 3.6 / 3.7 | 通过 |
| 题量不足 | 抛 `InsufficientQuestions` | 抛了，`details={"选择":{need:999999, available:4171, short:995828}}` | 通过 |
| `nos` 精确题集 | 顺序照给 | 24 题顺序与传入完全一致 | 通过 |
| `exclude_nos` | 拉黑生效 | 拉黑题未出现 | 通过 |
| `nos` 与 `mix` 不符 | 抛 `SelectError` | 抛 `SelectError` | 通过 |
| `save()` 落库 | 写 `papers` | 临时库可完整回读 24 题 manifest | 通过 |
| manifest 契约 §3 | 字段齐全 | `pid=P20261031-001`（合 `P\d{8}-\d{3}`）、`page={w_pt:595.3, h_pt:841.9, dpi:300, w_px:2480, h_px:3508}`、每题 `answer_key` 非空、`space_paras=0`、选择题 `options=[A,B,C,D]`、`stem_tokens` 非空 | 通过 |
| 卷面分组序 | 选择→判断→填空→解答 | 选择 1..10 / 填空 11..18 / 解答 19..24 | 通过 |

**结论：通过。**

> **可复现性的准确口径**（提醒下游）：CONTRACT §2 的「同 seed 同一份卷」只在**同一题库快照**内成立。
> 题库裁剪 + 质量过滤改池子之后，同参数同 seed 会抽出**完全不同的题**（本轮 15:19 / 15:36 / 15:42 三次的 24 题都不一样）。
> 要跨版本复现，必须用 `nos` 精确题集，或把 `params` 连同当时的 `nos` 一起存档。

---

## 5. 卷面保真（卷面 docx / PDF vs 源 docx）

```powershell
cd E:\OCR
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T4PaperFidelity -v
# 专项复现（都不需要 Word）：
python app\tools\_dangling_owner.py      # 谁引入悬空 rId
python app\tools\_docx_check.py          # zip/python-docx 正常但 Word 打不开
python app\tools\_table_media_stats.py   # 全库多少题属于"表格带图"
```

证据：`paper_fidelity.json`、`paper_health.json`、`paper_render.json`、`paper_relationships.json`、
`paper_table_image.json`；产物 `app/out/acceptance_v1/paper/acceptance_paper.docx` / `.pdf` / `page-0*.png`

做法（verifier 独立实现，不用 `paper.observed_counts`）：把 `paper.build_docx()` 的产物逐题拆成 1×1 题框，
数框内 `m:oMath` / `w:object` / `w:drawing` / `w:pict` / `m:imagedata` / `a:blip` 元素；
媒体按**引用数**比（compose 按内容哈希去重，同一张图只存一份，所以"部件数"会小于"引用数"），
再用 sha1 校验**每个源媒体字节都在卷面包里**；另加两条硬闸门：
**① 每个 rId 必须能解析（悬空 = Word 判损坏）；② 表格内的图（`table` token 里的 w:drawing/pict）也必须拷进包。**

### 5.1 逐题保真（15:42 那轮）

| 项 | 期望 | 实测 |
|---|---|---|
| 题框数 | 24 | **24** |
| `build_docx` 耗时 | — | **0.14 s**（另次 0.12–0.15 s） |
| 公式 oMath | 50 | **50** |
| 内联对象 object | 36 | **36** |
| 图片对象 drawing / VML pict | 3 / 0 | **3 / 0** |
| 媒体引用数 | 39 | **39** |
| 逐题引用缺失（obs < exp） | 0 | **0** |
| 逐题引用多印（obs > exp） | 0 | **0** |
| 媒体字节缺失（sha1） | 0 | **0** |
| 悬空 rId | 0 | **0**（`rid_refs=41 / unique=35 / rels=43`） |
| 卷面包内媒体部件数 | — | 33 |
| 比对题数 | 抽 20 题 | **24 题全查**（前 20 题逐行在 JSON） |

前 20 题逐行见 `paper_fidelity.json` 的 `first20`（每行含 `exp`/`obs` 的 oMath/object/drawing/pict/media 与
`media_parts`、`missing_media_hashes`）。抽查几条：idx1 `00774` object 6 / media 6 = 6 / 6；
idx6 `00568`（**曾经触发 P0-B 的题**）object 7 / media 7 = 7 / 7 且字节 5/5 全在；
idx10 `04103` oMath 5 = 5。

产品自带闸门 `paper.check_docx()`：`items_ok=true`、`media_missing=[]`、`formula_omml 50/50`、
`tables 0/0`、`option_tables=10`、`fig_ref_missing=0`、`stem_prefix_stripped=23`、`docx_bytes=125,196`、`warnings=[]`。

### 5.2 PDF / PNG（15:42 那轮，render.py 修好跨盘之后）

| 项 | 值 |
|---|---|
| PDF 页数 | **3** |
| PDF 大小 | 345,857 B |
| PNG | 3 张，均 **2480×3508** |
| 每页墨迹率 | 3.56% / 4.39% / 3.91%（无空白页） |
| Word 转 PDF 耗时 | 6.91 s（整轮 7.65 s） |

**结论：通过**。卷面不丢公式、不丢图（含表格内的图）、不丢表；每个 rId 都能解析；PDF 可正常导出/打印。

> 历史：15:36 那轮曾命中 **P0-B**（题 00568 表格里 5 张图 → 5 个悬空 `rIdImg*` + 3 份媒体字节缺失 →
> Word 判「文件已损坏」→ PDF 导不出）。`compose.py` 15:40:15 修好后，verifier 用两条路径复核：
> ① 抽样卷（24 题）`dangling=[]`、`missing=[]`；② **确定性回归**（`paper_table_image.json`，只出 00568 这一题）
> `dangling=[]`、`missing_parts=[]`、源媒体 **5/5 字节全在**。详见 §9-F6。

---

## 6. 答题卡几何

```powershell
cd E:\OCR
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T5CardGeometry -v
```

证据：`app/out/acceptance_v1/card_geometry.json`；产物 `app/out/acceptance_v1/card/card.pdf`、`card_1..4.png`

| 检查 | 期望 | 实测 |
|---|---|---|
| 卡页数 / PNG 数 | 相等 | **4 / 4**，均 2480×3508 |
| ArUco 检出 | 每页 0/1/2/3 | **4/4 页全检出**；四角中心与契约坐标偏差 **0.5 px** |
| QR 载荷 | `{pid, p}` | 每页都对：`{"pid":"P20261031-001","p":1..4}` |
| 作答格几何 | 不越界、不重叠 | 检查 **67** 个格子：越出页面 **0**、越出正文区 **0**、格重叠 **0** |
| 选项格数 | 2–4 | 选全 4（10 道选择题） |
| `iter_answer_cells` | 与答案空对应 | **21 格**（fill 12 + solution 9）= manifest 答案空数 |
| 卡的 `pid` | 一致 | 21 个格全带 `pid=P20261031-001` |

**结论：通过。**

---

## 7. 拍照判卷（10 张合成照片）

```powershell
cd E:\OCR
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T6GradePhotos -v
```

证据：`app/out/acceptance_v1/grade_photos.json`；照片 `app/out/acceptance_v1/photos/photo_*.jpg`；
裁切 `app/out/acceptance_v1/grading/P20261031-001/sub_*/`（按 §13.4 精简：只留最近 2 次运行的 sub_，共 23 张；重跑本节会重新生成；当次「55 个 pending 全部有裁切图」的判定见 `grade_photos.json` 的 `pending_sample.missing_crop=[]`）

合成条件（verifier 自己写，不复用 `tests/test_grade.py` 的 Fixture）：答题卡 PNG → 按意图涂黑选项 →
透视抖动 **3%** + 光照渐变（0.70→1.25）+ 高斯噪声 σ=4 + 3×3 模糊 + **JPEG quality=70** + 缩放到 **72%**。
作答意图：`idx % 4 == 0` 的题故意涂错（页 1 共 10 道选择 → 8 对 2 错）。

| 检查 | 期望 | 实测 |
|---|---|---|
| 标定成功率（`warp_ok`） | ≥ 9/10 | **10/10** |
| QR 解出率（`qr_ok`） | ≥ 9/10 | **10/10** |
| 选择题判分单元 | — | **30** 个（10 张照片里落在有选择题那页的） |
| 选择题判分一致率 | **100%** | **30/30 = 100%** |
| 判成 unsure | 0 | **0** |
| 填空/解答 | `pending` + 裁切图 | **55** 个 pending，裁切图**全部存在**（`missing_crop=[]`） |
| 页 1 得分 | 8/13 | **8 / 13**（10 选择 8 对 2 错 + 3 个填空 pending） |
| 判卷耗时 | — | **0.39 s / 10 张** |
| `submissions` 落库 | 有行 | 有，`warp_ok=1`，`detail_json.items` 与返回一致 |

**结论：通过**（标定、QR、OMR 判定、pending 裁切、落库全达标）。未做 OCR 异步回填（t13 范围）。

---

## 8. 怎么重跑

```powershell
$env:PYTHONIOENCODING='utf-8'
cd E:\OCR

# 只跑验收（26 条，14–70 s；Word 被占用时会重试 5 次后跳过 PDF 那一条）
python -m unittest discover -s app\tests -p "test_acceptance.py" -v

# 单节
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T4PaperFidelity -v

# 全量（契约要求全绿）
python -m unittest discover -s app\tests
```

证据就地覆盖写到 `app/out/acceptance_v1/`：

| 文件 | 内容 |
|---|---|
| `data_layer.json` / `data_layer_schema.json` / `data_layer_tokens.json` | §2 |
| `normalize_pairs.json` | §3（含 8 条失败原文+原因） |
| `picker.json` / `picker_scope.json` / `picker_insufficient.json` | §4 |
| `paper_fidelity.json` / `paper_health.json` / `paper_render.json` / `paper_relationships.json` / `paper_table_image.json` | §5 |
| `paper/acceptance_paper.docx|.pdf|page-0*.png` | §5 产物 |
| `card_geometry.json` / `card/card.pdf|card_*.png` | §6 |
| `grade_photos.json` / `photos/*.jpg` / `grading/<pid>/sub_*/` | §7 |

---

## 9. 发现清单

### F1 · P0-A · 导 PDF 在这台机器上 100% 失败（`render.py` 跨盘落位）— **已修复并复核**

- **现象**：`RenderError: 目标 PDF 被占用…（OSError: [WinError 17] 系统无法将文件移到不同的磁盘驱动器:
  C:\...\Temp\mathapp-render-x\out.pdf -> E:\...pdf）`。
- **根因**：`render._move_into_place()` 用 `os.replace` 跨盘搬 PDF；Windows 跨卷必报 `ERROR_NOT_SAME_DEVICE(17)`，
  且重试白名单是 `(None,5,32,33)`，17 直接放弃并包装成「被占用」。
- **复现**：`python app\tools\_repro_crossvol.py` → 失败；同一对路径 `shutil.move` 成功。
- **修复**（render-engineer）：跨卷先 `copyfile` 到 `<dst>.part-<pid>` 再在目标卷内 `os.replace` 原子顶上；
  错误分流（17 / 5,32,33 文案不同）；新增 `_preflight_target`（DELETE 权限探针）在转换前就报「请关掉打开它的程序」。
- **复核**：`_repro_crossvol.py` 跨盘落位成功；卷面 PDF 正常出图（3–4 页）；`test_render` 33/33 OK。

### F2 · P1 · 2,932 道题题干提到图但源 docx 无图（语料缺口）

- 裁剪前实测：全库 **2,932 / 22,898 = 12.8%** 题干含「如图/图中/下图/上图/见图/图所示」且 `media_count=0`；
  其中有答案 **2,017**；**选择可用池 715 / 5,013 = 14.3%**。
- 影响：印出来是「如图，求…」却没有图，学生无法作答。
- **进展**：`picker` 已加默认 `exclude_fig_missing`（池子 4,986 → 4,171 就是它的效果），
  `quality.py` 侧 flag 仍在补；**建议交付说明书里写明"组卷默认排除这类题"**。
- 复现：`python app\tools\_stats3.py`

### F3 · P2 · 归一化 4 类写法未覆盖（8/106 组等价对判错）

见 §3。方向是"对答判错"（比"错答判对"安全）。已开 t26；补完用同一份 106 组复测。

### F4 · P3 · `papers` / `submissions` 表为空，判卷强依赖 `papers.manifest`

- 现状：`papers=0 行`、`submissions=0 行`（验收判卷用临时库插入 manifest 绕过）。
- 风险：`grade.grade_image` 靠 `papers.manifest` 回读卡几何；**生产链路必须由 server 建卷时 `picker.save()` 落库**。
  这是 t11 职责，t13 里必须验「建卷 → 判卷」闭环。

### F5 · info · 全量 discover —— **定版全绿（760/760）**

16:59 定版快照：**760 用例 / 232.804 s / skipped=1 / failures+errors=0**。此前快照里的红点（loader 错误 8 条 → DB 裁剪旧断言 9 条 + 新闸门 4 条 → Word 跨进程锁 2 条 + grade_routes 断言 1 条）已全部清零；日志 `app/tools/logs/full_t13.log`。

### F6 · P0-B · 「表格里带图」的题让整份卷面 docx 打不开（悬空 rId）— **已修复并复核**

- **现象**：Word 报「文件可能已经损坏」→ 「下载打印 PDF」连带失败。
- **根因**：题 **00568** 的 1×7 表格内部有 5 个 `r:embed="rIdImg1..5"`；`docx_rich` 的 `table` token
  **只存 XML 不收 media**，而 `paper`/`compose` 插入 table 时只对 `embed` token 重映射 rId → 悬空 rId + 图没拷进包。
- **影响面**：全库 **398 / 22,898 = 1.7%** 的题表格里带媒体引用
  （`python app\tools\_table_media_stats.py`，样例 00073/00220/00223/00226/00246/00314/00345/00346/00464/00482）；
  24 题卷命中概率 ≈ **34%**。这批题 `media_count>0`，`exclude_fig_missing` **挡不住**。
- **复现**：`_dangling_owner.py`（定位到 00568）、`_docx_check.py`（zip/python-docx 正常但 Word 打不开）、
  `_word_env.py`（同一台 Word 打开源 docx 正常 → 排除环境问题）。
- **修复**（paper 侧 `compose.py` 15:40:15 扩到 table token 的媒体重映射）。
- **复核**：① `paper_relationships.json` → `dangling=[]`；② `paper_fidelity.json` → `missing=[]`（含字节校验）；
  ③ `paper_table_image.json`（**确定性回归**，只出 00568）→ `dangling=[]`、`missing_parts=[]`、**源媒体 5/5 字节全在**；
  ④ Word 能正常打开卷面 docx，PDF 3 页（345,857 B）。
- **留下的教训**：验收里新增了「悬空 rId」「表格内媒体字节」两条硬闸门——它们不需要 Word，
  2 秒就能抓这类"文件打不开"的 bug；只靠 seed 抽样会漏（本轮就差点漏掉）。

---

## 10. 已知保留项（交付时必须写进说明书/限制声明）

| # | 保留项 | 实测/现状 | 影响 | 跟踪 |
|---|---|---|---|---|
| R1 | **语料缺口：题干提到图但源 docx 无图** | 裁剪前 2,932 题（12.8%），有答案 2,017，选择池占 14.3%；组卷已默认排除 | 若关闭该过滤，会印出「如图」却没有图的废题 | picker 已加 `exclude_fig_missing`；quality flag 补齐中 |
| R2 | ~~归一化 4 类写法未覆盖~~ **已关闭** | 16:07 复测 **106/106 + 41/41 全对**（t26 受控求值兜底已落地，`normalize.py` sha1 `b79a95c96f`）；原 8 条漏判已固化为回归用例 | — | t26 已交付，本条移出保留项 |
| R3 | **真实手写识别率未验证（交付后必须抽检）** | **合成样本已验**：t31 的 100 次合成判卷里，标定 100%、QR 100%、选择题判分 100%（255/255）、裁切可用率 100%（390/390）、p95 0.06 s（见 §14）。**真实手写未验证**：只有 PaddleOCR-VL 真机 4/4 小样本；填空/解答在 manual 后端下必然 pending，识别准确率取决于 OCR | 真实手写（「三分之一」「x=4」）的识别错误率未知；错判会进人工确认队列（不会静默判错），但体验取决于 OCR | **交付后第一批真实样张需人工抽检 20 格校准**（对照答案逐格核）；本项不写「已通过」 |

> R3 的已知兜底：识别不出 → 留在 `pending`（绝不静默判错），带 `crop_path` 进人工确认队列
> 兜底证据：最近一次 26 条验收跑出 **55 个 pending**，当场 `missing_crop=[]`（见 `grade_photos.json`）；证据目录按 §13.4 精简后只保留最近 2 次运行的裁切图（23 张），重跑即重新生成。

---

## 11. 本版未覆盖（第二版补）

1. server/UI 全链路（t10/t11/t22）：`POST /papers` → 卷面+答题卡合并 PDF → 上传判卷 → 结果页 → 人工确认。
2. OCR 异步回填（`fill_pending` + PaddleOCR-VL）：本版只验到 `pending` + 裁切图落盘。
3. 错题训练卷闭环（`train.plan` → `picker.build(nos=...)` → `mastery` 更新）。
4. 打印合并 PDF 页序（卷面在前、卡在后）+ 页脚"共 N 页"。
5. 真实拍照（手写、折痕、暗光、倾斜 >5%）鲁棒性。

---

## 附录 A · 实测时的代码指纹（15:42–15:45）

Python **3.14.2**；Word 可用（`paper.word_available()=True`）。
代码仍在并行开发，下面是跑最后一轮验收时的指纹（sha1 前 10 位 / mtime）：

| 文件 | 大小 | sha1 | mtime |
|---|---|---|---|
| `app/db.py` | 3,967 | `7249a812eb` | 15:18:31 |
| `app/docx_rich.py` | 9,782 | `1f3476989a` | 13:48:54 |
| `app/compose.py` | 6,766 | `a7cfe0f261` | 15:40:15（修 F6） |
| `app/taxonomy.py` | 11,174 | `d3a18a231d` | 13:54:12 |
| `app/families.py` | 1,615 | `0b1a8de40a` | 13:55:00 |
| `app/picker.py` | 35,943 | `1db68e7ab8` | 15:29:39 |
| `app/paper.py` | 44,841 | `aa04daec78` | 15:30:12 |
| `app/card.py` | 26,726 | `f55d172c5f` | 14:22:44 |
| `app/grade.py` | 43,541 | `05256ccd05` | 15:35:31 |
| `app/normalize.py` | 25,226 | `e8ae7ee367` | 14:30:37 |
| `app/quality.py` | 15,699 | `0fb482901d` | 14:57:52 |
| `app/server.py` | 59,415 | `112ed3e35f` | 15:36:22 |
| `app/render.py` | 32,191 | `cb8a5ea399` | 15:33:49 |
| `app/tests/test_acceptance.py` | 57986 | `e669316110` | 15:42 |

## 附录 B · 验收脚本自身的两次修正（记录在案）

1. **编码事故**：15:07–15:10 我用 PowerShell（PS 5.1 默认 GBK）往返读写 `tests/test_acceptance.py`，
   把文件写成「UTF-8 被当 GBK 再编码」的乱码 + BOM，导致 `discover` 多一条 loader `SyntaxError`；
   captain 与 data-engineer 各自独立发现并把文件移入 `app/data/quarantine/`。15:11 用 write 工具重写恢复。
   **教训**：含中文的源码只用 write 工具或 `io.open(..., encoding='utf-8', newline='\n')`。
2. **两次"假红"**（都已撤回并修正口径，值得写进验收规范）：
   * 媒体比对最初写成 `"/media/" in target_ref`（实际是相对路径 `media/x.wmf`）→ 8 道"选项是图片"的题被误判 obs=0；
   * 媒体比对最初按"去重后的部件数"比"源 token 的媒体数"，而 `compose` 会按内容哈希去重 → 又把复用同一张图的题误判为缺图。
   现在按**引用数**比 + **sha1 字节**校验，两边口径一致。
   **教训**：凡是"obs 恒为 0 / 恒等于某个数"的指标，先怀疑判定写法，再报 bug。

---

## 12. 打印链路与落库（t32）

```powershell
# 起真服（uvicorn，真 HTTP），DB 用副本、产物落独立目录，验完删副本停服
python app\tools\_t32_print_chain.py
```

证据：`app/out/acceptance_v1/print_chain.json`、合并件 `app/out/acceptance_v1/print_P20261031-001.pdf`
做法：`shutil.copyfile(app.db)` → 独立 `out/` → `uvicorn server:app --port <free>`（env：`MATH_APP_DB` / `MATH_APP_OUT`）
→ 真 HTTP `POST /papers`（10 题：6 选择 + 2 填空 + 2 解答）→ 查库 → 查盘 → 合成照片 `POST /submissions`
→ `GET /papers/{pid}/print.pdf` / `/pdf` / `/card.pdf` → 停服、删副本。

### 12.1 落库链路 —— **通过**

| 检查 | 期望 | 实测 |
|---|---|---|
| 起服 | `/healthz` 200 | **200** |
| `POST /papers` | 302 → `/papers/<pid>` | **302**，`Location: /papers/P20261031-001`，7.8 s（1 次成功，无降级） |
| `papers` 行 | 1 行、字段非空 | **1 行**：`pid / title / created_at=2026-10-31T15:56:33+08:00 / n_items=10 / params_json` 均可解析；`docx_path`、`pdf_path` 指向真实文件 |
| manifest | `items` = 10 | **10**（首题 10106） |
| manifest 里 card 合并 | `card.pages` 存在 | **有**，`card_pages=2` |
| 盘上产物 | docx/pdf/PNG/card.pdf | `paper.docx`、`paper.pdf`、`page-01..02.png`、`card.pdf`、`card_1..2.png`、`preview.json` 全在 |
| 上传判卷 | 判卷能从库里取 manifest | `POST /submissions` **302** → `sid`；`submissions` **1 行**，`warp_ok=1`、`qr_ok=1`、`score=6/9`、`detail.items=9`、`detail.pid=P20261031-001` → **判卷确实是从 `papers.manifest` 取到的卡几何**（不依赖路径猜测） |
| 预览页 / 结果页 | 200 | **200**（8,821 B / 22,901 B） |

### 12.2 合并打印件 —— **全部通过（页脚新口径已复验）**

> **口径（captain 2026-10-31 拍板）**：卷面与答题卡**各自编号**，合并件不盖统一页脚。
> · 卷面页脚：`卷面 · 第 N 页 / 共 A 页`（A = 卷面页数）
> · 答题卡页脚：`答题卡 · 第 M 页 / 共 B 页 ·（卷面续页 P{A+M}）`（A = `manifest["paper"]["pages"]`，B = 卡页数）
> · `/print.pdf` 只拼接，不叠页脚；预览页显示「共 N 页（卷面 A + 卡 B）」
> 复验判据：① A 正确 ② 卡页脚 P{A+M} 正确 ③ /print.pdf 页数 = A+B ④ 顺序卷面在前 ⑤ 没有叠两个人页脚。

**复验时刻 2026-10-31 16:10**（`paper.py` 16:01:37 回写页数 + 改文案；`card.py` 16:02:56 按 A 续页）。
命令：`python app\tools\_t32_print_chain.py`（真服，15 秒）→ 证据 `app/out/acceptance_v1/print_chain.json`
+ 合并件 `print_P20261031-001.pdf`（377,346 B / 4 页）。

| 判据 | 期望 | 实测 | 结论 |
|---|---|---|---|
| ① A 正确 | `manifest["paper"]["pages"]` = 卷面页数 | **写回成功**，`card_start_page = 3 = A+1`（A=2） | **通过** |
| ② 卡页脚 P{A+M} | 卡页 label 从 A+1 起 | `card_page_labels = [3, 4]`（旧版是 [1,2]）；卡页脚由 `_draw_footer(page, n, label)` 直接用该 label → 「卷面续页 P3 / P4」 | **通过** |
| ③ 页数 = A+B | 4 | **4 = 2 + 2** | **通过** |
| ④ 顺序卷面在前 | paper…card… | `["paper","paper","card","card"]`（逐页文字层判定） | **通过** |
| ⑤ 无叠页脚 | 每页只一行页脚 | 合并件每页「共 N 页」出现次数 = **1**（page1/2 各 1 次，卡页无文字层）；卷面页脚原文 = **`卷面 · 第 1 页 / 共 2 页 · 卷号 P20261031-001`** | **通过** |

结构与内容同轮核对：

| 检查 | 期望 | 实测 | 结论 |
|---|---|---|---|
| `GET /print.pdf` | 200 + `application/pdf` | **200**、377,346 B、4 页 | 通过 |
| `GET /pdf` | 200、A 页 | **200**、253,193 B、2 页 | 通过 |
| `GET /card.pdf` | 200、B 页 | **200**、161,313 B、2 页 | 通过 |
| 预览页 / 结果页 | 200 | **200** / **200** | 通过 |

> 答题卡是**图片 PDF**（PNG 直插），页脚文字无法 `get_text()` 抽取；本轮用两条证据：
> ① manifest 的 `label=[3,4]` 是 `_draw_footer` 的唯一输入（代码里就是 `"…（卷面续页 P%d）" % label`）；
> ② 页脚带（y≈3170–3240）有墨：`card_1.png ink=0.0418`、`card_2.png ink=0.0423`。

---

## 13. 交付清单与运行方式（t34）

> 审计脚本：`app/tools/_t34_delivery_audit.py`（只读；结果 `app/out/acceptance_v1/delivery_check.json`）
> 复核命令：`python app\tools\_t34_delivery_audit.py`（3 秒，不写产品文件）

### 13.1 交付目录树

**产品必需**（删任何一个都会让程序起不来或功能缺失）：

```
app\
  start.bat                 双击启动（找 Python / 查依赖 / 探 Word / 起服务 / 开浏览器）
  requirements.txt          16 个运行依赖（见 13.3）
  README.md                 安装/使用/打印/拍照/隐私说明
  CONTRACT.md               接口契约（模块 API / manifest / 坐标体系）
  VERIFY.md                 本验收报告
  config.example.json       脱敏配置模板（token 留空，可直接抄成 config.json）
  config.json               本机配置（含 OCR token，**不要外发**）
  server.py grade_routes.py  Web 服务（组卷/预览/下载/判卷/训练，判卷路由单独一个模块）
  picker.py paper.py card.py grade.py ocr.py normalize.py train.py     五大核心
  render.py render_check.py   docx→PDF / PDF→PNG（Word COM 封装 + 自检）
  quality.py quality_baseline.py dedupe.py 题库体检/基线/查重（组卷默认用它过滤坏题）
  docx_rich.py compose.py taxonomy.py families.py db.py build_db.py   M0 底座 + 建库
  errors.py                  错误码与中文文案（/guide 页数据源）
  demo.py                    演示数据/自检脚本
  scan_encoding.py           语料编码扫描（建库前置，可重建）
  templates\ (13 个)          base/_banner/error/index/papers/preview/health/guide/train/
                             grade_upload/grade_result/grade_error/grade_why
  static\ (2 个)              app.css / app.js（无前端框架）
  tests\ (26 个 test_*.py)    unittest：`python -m unittest discover -s app\tests`
  data\app.db                 SQLite（124 MB，可重建：`python build_db.py` ≈20 s）
```

**不该进交付**（清掉再打包）：

```
app\__pycache__\  app\tests\__pycache__\      pyc 缓存
app\out\                                   用户产物 + 测试产物（打包前清空，只留说明文件）
app\data\quarantine\                       事故隔离区（已被我 15:5x 清空）
app\data\*.log                             运行日志
app\_*.py  app\out\_*.py                   一次性探针（已迁 app\tools\，见 13.4）
app\data\*_report.json  data\encoding_*.json   构建/体检报告（可重建，交付可选）
app\data\render\                          渲染临时目录
```

### 13.2 文档一致性

| 检查 | 口径 | 结果 |
|---|---|---|
| CONTRACT 提到的模块 | 20 个（docx_rich/compose/taxonomy/families/db/build_db/picker/paper/render/card/grade/ocr/normalize/train/quality/dedupe/errors/server/grade_routes/demo） | **20/20 全在** |
| README 提到的文件 | start.bat / requirements.txt / config.example.json / config.json / server.py / picker.py / paper.py / card.py / grade.py / ocr.py / normalize.py / train.py / build_db.py / templates / static / tests / data / out | **全在** |
| 代码 `render("x.html")` 引用的模板 | 11 个 | **全在**（templates 共 13 个，含被 `{% extends %}` 的 base.html 与 `{% include %}` 的 _banner.html） |
| 模板引用的静态文件 | `app.css`, `app.js` | **全在** |
| start.bat 引用的东西 | `requirements.txt` / `server:app` / `server.probe_word()` | **全在**（probe_word 函数存在于 server.py） |
| 端口一致性 | start.bat=8710 / config.json=8710 / config.example.json=8710 / README=8710 | **一致** |
| config.example.json 的 token | 必须留空 | **留空**（本机 config.json 已配真 token） |
| VERIFY.md 引用的证据路径 | 15 条 `app/out/...` | **0 条缺失** |

### 13.3 依赖一致性（requirements.txt ↔ 本机实测）

| 包 | 声明区间 | 本机实测 | 可导入 | 在区间内 |
|---|---|---|---|---|
| `fastapi` | `>=0.162,<1.0` | **0.162.0** | ✅ | ✅ |
| `uvicorn` | `>=0.54,<1.0` | **0.54.0** | ✅ | ✅ |
| `python-multipart` | `>=0.0.32,<1.0` | **0.0.32** | ✅ | ✅ |
| `jinja2` | `>=3.1.6,<4.0` | **3.1.6** | ✅ | ✅ |
| `python-docx` | `>=1.2,<2.0` | **1.2.0** | ✅ | ✅ |
| `lxml` | `>=6.1,<7.0` | **6.1.2** | ✅ | ✅ |
| `openpyxl` | `>=3.1,<4.0` | **3.1.5** | ✅ | ✅ |
| `pillow` | `>=12.1,<13.0` | **12.1.0** | ✅ | ✅ |
| `numpy` | `>=2.4,<3.0` | **2.4.2** | ✅ | ✅ |
| `opencv-python` | `>=5.0,<6.0` | **5.0.0.93** | ✅ | ✅ |
| `pymupdf` | `>=1.28,<2.0` | **1.28.2** | ✅ | ✅ |
| `reportlab` | `>=5.0,<6.0` | **5.0.1** | ✅ | ✅ |
| `qrcode` | `>=8.2,<9.0` | **8.2** | ✅ | ✅ |
| `pywin32` | `>=312,<313` | **312** | ✅ | ✅ |
| `requests` | `>=2.34,<3.0` | **2.34.2** | ✅ | ✅ |
| `pillow-heif` | `>=1.8,<2.0` | **1.8.1** | ✅ | ✅ |

**16/16 可导入、16/16 落在声明区间**（Python 3.14.2 / Windows；`opencv-python 5.0.0.93` 对应 `cv2` 5.0.0）。

### 13.4 out/ 清理清单（captain 已批准；我按批准执行了一部分）

| 目录/文件 | 大小 | 处置 | 理由 |
|---|---|---|---|
| `out/P20261031-901` | 0.0 MB | 可删 | 测试产物 |
| `out/annotate_demo.png` | 2.3 MB | 可删 | 测试产物 |
| `out/captain_00568` | 0.5 MB | **保留** | P0 回归证据 |
| `out/card.pdf` | — | **已清理（17:28）** | card.py 自检 CLI 输出，非用户卷子 |
| `out/card_1.png` | 0.1 MB | 可删 | 测试产物 |
| `out/card_2.png` | 0.1 MB | 可删 | 测试产物 |
| `out/demo` | 6.4 MB | 可删 | 测试产物 |
| `out/handwrite_check` | 1.8 MB | **保留** | captain 的手写验证卷（给用户打印） |
| `out/paper24v2` | 4.5 MB | **保留** | captain 指定保留（交付配图） |
| `out/paper24v3` | 2.3 MB | 可删 | 测试产物 |
| `out/quality_baseline` | **1.4 MB** | **保留** | t31 已交付：按批准删掉 `photos*/runs*/cards/_db`（**释放 199.3 MB**），只留 `report.md` / `report.json` / `samples.json` + `samples/` 5 张代表图 |
| `out/t29_00568` | 0.3 MB | 可删 | 测试产物 |

**已执行**（17:28，captain 批准的第二批）：删 `__pycache__`×2、`_probe_titles.py`、`data/*_report.json`×6、`data/render/`、`data/_e2e_*.log` ×2、`data/_smoke_*.log` ×2、`out/card.pdf|card_1.png|card_2.png`、`out/P20261031-901/`（孤儿目录）共 **18 项 / 4.7 MB**；`data/app.db-wal|-shm` 因 8710 服务正持有真库**本轮保留**；同一轮还把验收证据目录的 `run_*`（含 4 个 124 MB 库副本）清掉，**释放 703.6 MB**（acceptance_v1 15.2 MB）。

**已执行**（15:5x，按 captain 批准）：探针 8 个 → `app/tools/`；`_full_unittest*.log` 只留最新一份到 `app/tools/logs/`；删 `acceptance_v1/run_*` 17 个（76 MB）；删 `paper10/paper10v2/paper10v3/paper24/multi/smoke/P20261031-901`（10.3 MB）；删 `data/quarantine/`（只含我 15:10 写坏的那份）。

**dry-run 删除命令**（还没执行的部分，确认后跑）：

```powershell
# 交付前清 out/（保留 captain 指定的 4 个 + 验收证据；⚠️ 先确认没有正在跑的验收）
Get-ChildItem E:\OCR\app\out -Directory | Where-Object {
  $_.Name -notin @('acceptance_v1','paper24v2','captain_00568','quality_baseline','handwrite_check')
} | ForEach-Object { "将删除: $($_.Name)" }   # 先只看，不改
# 真删：把上面 ForEach-Object 里的字符串换成 Remove-Item -Recurse -Force $_.FullName

# 缓存/日志/临时
Remove-Item -Recurse -Force E:\OCR\app\__pycache__, E:\OCR\app\tests\__pycache__
# card.pdf / card_1.png / card_2.png 与 data/render、data/*_report.json、data/_*_*.log 已于 17:28 清理
Remove-Item -Recurse -Force E:\OCR\app\data\render
```

### 13.5 运行方式（与 README 一致，已核）

1. **双击 `start.bat`**：找 Python（`python` 或 `py -3`）→ 查依赖（缺了问是否 `pip install -r requirements.txt`）→ 探 Word 路径（常见路径没命中时走注册表 `server.probe_word()`）→ 起服务 → 3 秒后自动开浏览器；`MATH_NO_BROWSER=1` 可关自动开窗。
2. **端口 8710**（start.bat / config.json / config.example.json / README 四处一致）；`--host 127.0.0.1`，**只监听本机**，局域网其它设备访问不到。
3. **降级**：① 没 Word → 卷面仍生成，docx→PDF 走降级提示（`/papers/{pid}/pdf` 返回 409 说明页，答题卡照常打印）；② 没 OCR key / 超时 → 填空解答留在「待人工确认」，`config.json` 把 `ocr.backend` 改 `manual` 可全人工。
4. **数据与隐私**：原始 `E:\OCR\*.docx` 只读；产物只写 `app\data`（库）与 `app\out`（卷子/卡/裁切图）；唯一上网行为是 OCR 阶段把作答格拼图发 PaddleOCR-VL；token 只在本机 `app\config.json`。
5. **重建库**：删 `app\data\app.db` → `python E:\OCR\app\build_db.py`（≈20 s）。

### 13.6 三张表（缺失 / 多余 / 文档不一致）

#### 缺失 —— **无**

> CONTRACT 的 20 个模块、README 提到的 18 个路径、11 个模板引用、2 个静态文件、start.bat 的 3 处引用、15 条 VERIFY.md 证据路径、16 个运行依赖，全部到位。

#### 多余 —— 17 项（都不影响运行，打包前清）

| 路径 | 原因 |
|---|---|
| `__pycache__` | pyc 缓存，不该进交付 |
| `_probe_titles.py` | 一次性探针脚本（应放 app/tools/ 或不进交付） |
| `data\_smoke_err.log` | 运行日志，不该进交付 |
| `data\_smoke_out.log` | 运行日志，不该进交付 |
| `data\app.db-shm` | SQLite WAL 残留（关库后应清） |
| `data\app.db-wal` | SQLite WAL 残留（关库后应清） |
| `data\build_report.json` | 构建/体检报告（可重建，交付可选） |
| `data\dedupe_report.json` | 构建/体检报告（可重建，交付可选） |
| `data\encoding_rows.json` | 构建/体检报告（可重建，交付可选） |
| `data\encoding_stats.json` | 构建/体检报告（可重建，交付可选） |
| `data\prune_report.json` | 构建/体检报告（可重建，交付可选） |
| `data\quality_report.json` | 构建/体检报告（可重建，交付可选） |
| `data\render` | 渲染临时目录 |
| `out\card.pdf` | card.py 自检 CLI 的输出（不是用户卷子） |
| `out\card_1.png` | card.py 自检 CLI 的输出（不是用户卷子） |
| `out\card_2.png` | card.py 自检 CLI 的输出（不是用户卷子） |
| `tests\__pycache__` | pyc 缓存，不该进交付 |

#### 文档不一致 —— 1 项

- **README「当前完成度」仍写「进行中：卷面/答题卡预览与下载、上传判卷页、训练卷页」**
  - 细节：这些已交付（server.py + grade_routes.py + train.py 齐备，§12 已跑通），文档过期

> README.md 不在 verifier 的 write scope（只有 `app/VERIFY.md` + `app/tools/`），**建议由 owner 按下面这段替换**：
>
> ```markdown
> ## 当前完成度
>
> - 已完成：建库（22,898 → 12,280 有答案题）、知识点体系、题族、质量体检与过滤、组卷选题、卷面排版与保真闸门、
>   答题卡生成（ArUco + QR）、答案归一化、识别适配层、判卷流水线（两阶段）、Web 全链路（组卷/预览/下载/打印合并/上传判卷/
>   待人工确认/训练卷）、软删与数据清理、打包文档与本验收报告。
> - 已知限制：见 `VERIFY.md` §10（语料「如图无图」、归一化 4 类写法、真实手写识别率未验证）。
> ```

---

## 14. 判卷质量基线（t31，合成样本 100 次）

> 由 t31 的 `quality_baseline.py` 生成，verifier 只做转述与核对（表中数字与 `report.md` 一致）。
> 证据：`app/out/quality_baseline/report.md`（报表）、`report.json`（机读）、`samples.json`（100 条逐样本元数据）、
> `samples/01_normal_A.jpg … 05_ladder_q20_j10.jpg`（5 张代表图：正常 / 正常 B 卷 / 压力档 / 对抗模糊 / 降质阶梯）。
> 复现：`python quality_baseline.py --photos 50`（每份卷 50 张，A/B 两卷共 100 张）；`--quick` 冒烟。

**条件**：2 份卷 × 50 次合成「拍照」（随机透视 2~4% / 光照梯度 / JPEG 60~80 / 轻微模糊）。

| 指标 | 结果 |
|---|---|
| 标定成功率 | **100%（100/100）** |
| 卷号 QR 成功率 | **100%（100/100）** |
| 选择题判分准确率 | **100%（255/255）**：单涂 **208/208**、不涂 **25/25**、双涂 **22/22**（后两类「该 unsure 时真的 unsure」） |
| 选择题 unsure 率 | **18.4%（47/255）** —— 这 47 道正是故意造的不涂 / 双涂 |
| 裁切可用率（填空/解答） | **100%（390/390）** |
| 单张耗时 | **p50 0.05 s / p95 0.06 s**（max 0.07 s） |

**压力档**（透视 5~8%、JPEG 35~55、模糊 3~5 px、20 张）与**降质阶梯**（q60→q20 × 透视 3%/6%/10%）：
标定 / QR / 选择判分同样 **100%**。

**对抗条件（重要，不能靠合成照片测）**：**四角黑框被打白（强反光）时标定 0/6**。
合成照片造不出真实反光，所以现场硬要求是「四角黑框清晰、别开闪光灯」——README「拍照要求」与 `/guide` 页都已写明。

**口径提醒**：报表里「需人工确认 **67.8%**」是 **manual 后端口径放大的**（填空/解答必然 pending，390/390），
**不要单独引用**；选择题层面 unsure 仅 **18.4%**，且全部来自故意制造的空涂/双涂。

**结论**：合成样本上，标定 / QR / 选择题判分 / 裁切 四条链路 **全部 100%**，性能 p95 = 0.06 s；
真实手写与真实反光**不在本基线覆盖范围**（见 §10-R3）。

---

## 15. 端到端验收（t13 定版，8 项硬指标）

> **交付定版：2026-10-31 16:59 · 760 用例 0 红 · 交付审计 缺失 0 / 文档不一致 0**

> 定版时刻：**2026-10-31 16:59:27**（全量 unittest 的日志时间）；命令一律在 `E:\OCR` 下执行。
> 口径：题库按用户决定裁掉「无答案题」——**原始语料 22,898 份 docx 不变，库里是可判分的 12,280 题**；题族/知识点数字随之是裁剪后的值（§2 有说明）。
> 全部证据在 `app/out/acceptance_v1/`；验收脚本 `app/tests/test_acceptance.py`（34 条，verifier 独立另写）。

| # | 项目 | 命令（摘要） | 实测 | 结论 |
|---|---|---|---|---|
| 1 | 全量测试 | `python -m unittest discover -s app\tests` | **760 用例 / 232.804 s / skipped=1 / failures+errors=0** | **通过** |
| 2 | DB 硬指标 | 见 15.2 | **12280** 题 / no 唯一 / 有答案 12280 / 题族 2091（≥3 题 1310）/ kps **34** | **通过** |
| 3 | 组卷 | 见 15.3 | mix 10/8/6 → **24 题**；两会话（按知识点/按章）各 24 题；同 seed 逐题一致；换 seed 重复 **0%** | **通过** |
| 4 | 卷面保真 | 见 15.4 | 20 题（**10 OMML + 10 MathType**）逐项比对，**missing=0** | **通过** |
| 5 | 答题卡 | 见 15.5 | **4 页** / 四角 ArUco 每页 0-3 / QR 解出 pid+p / **67 格** 0 越界 0 重叠 | **通过** |
| 6 | 判卷 | 见 15.6 | 10 张退化照片：标定 **10/10**、QR 10/10、选择题一致 **30/30 = 100.0%**、unsure 0 | **通过** |
| 7 | 人工确认链路 | 见 15.7 | 上传 → manual 重识别（pending 不变）→ 确认 3 空 → 分数 **3→6 / 9**，pending 归零 | **通过** |
| 8 | 训练卷 | 见 15.8 | 3 道错题 → **9 题**训练卷；无原题重复；覆盖题族/知识点/章节 0 缺口 | **通过** |

### 15.1 全量测试

```powershell
cd E:\OCR
python -m unittest discover -s app\tests        # 或加 -v 看逐条
```

| 项 | 值 |
|---|---|
| 用例数 | **760** |
| 耗时 | **232.804 s** |
| 跳过 | 1 |
| 结果 | **OK**（failures+errors = 0） |
| 日志 | `app/tools/logs/full_t13.log` |
| 其中 verifier 的验收用例 | 34 条，全绿（`test_acceptance.py`） |

### 15.2 DB 硬指标

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T1DataLayer -v
```

| 指标 | 实测 | 说明 |
|---|---|---|
| `questions` 行数 | **12280** | 原始语料 22,898 份 docx；按用户决定裁掉无答案题后 = 可判分题数 |
| `no` 唯一 / 5 位数字 | 12280 / 12280 | 主键，无重复 |
| 有答案 | **12280**（无答案 0） | 判卷只从有答案题里出 |
| 题族 / ≥3 题族 | **2091 / 1310** | 裁剪后（全量语料时是 2,671 / 1,760） |
| 知识点 | **34**（`kps` 表 34 行） | 34 个知识点全覆盖 |
| 题型分布 | 选择 5013、解答 4406、填空 2797、其他 64 | 判断类 2 道都无答案，已随裁剪消失 |
| 完整性 | `PRAGMA integrity_check` = **ok** |  |
| 证据 | `data_layer.json` / `data_layer_schema.json` / `data_layer_tokens.json` | 抽样 1000 题 tokens_json 全可解析 |

### 15.3 组卷（两会话 + 可复现 + 换 seed）

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T3Picker -v
```

| 检查 | 实测 | 结论 |
|---|---|---|
| 基准卷 mix={选择:10,填空:8,解答:6}、seed=42 | **24 题**（选择 10、填空 8、解答 6） | 通过 |
| 同参数建第二次（可复现） | 题号列表**逐项相同**（24/24） | 通过 |
| 换 seed（42→43）重复率 | **0 / 24 = 0%**（要求 <20%） | 通过 |
| 会话 A：按**知识点**（kps=["1.2"]） | 24 题，`kp_id` 全 = 1.2；题号 00898、01152、01753、01777、03209、03225、03313、10478、10758、10760、00925、03257、03304、04369、07697、07708、07980、16352、00638、04141、… | 通过 |
| 会话 B：按**章节**（chapters=["第1章 有理数"]） | 24 题，`chapter` 全 = 第1章 | 通过 |
| 章级知识点 `3.0` 展开 | 出到 3.0、3.1、3.2、3.3、3.4、3.5、3.6、3.7 | 通过 |
| 可用池（现算） | 选择 4170、填空 2238、解答 3542 | 记录 |
| 证据 | `picker.json` / `picker_scope.json` / `picker_insufficient.json` |  |

### 15.4 卷面保真（20 题：10 OMML + 10 MathType）

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T4bOmmAndMathType -v
```

做法：从库里挑 **10 道源 docx 只有 OMML 公式**的题 + **10 道源 docx 只有 MathType/OLE 对象**的题，
单独出卷（`picker.build(nos=...)` → `paper.build_docx`），逐题把题框里的 `m:oMath` / `w:object|drawing|pict` /
媒体引用数与源 docx 对齐。

| 项 | 期望 | 实测 |
|---|---|---|
| OMML 题数 / MathType 题数 | 10 / 10 | **10 / 10** |
| 公式 oMath（20 题合计） | 36 | **36** |
| 内联对象（object/drawing/pict） | 58 | **58** |
| 媒体引用（含表格内图） | 58 | **58** |
| **缺失数** | **0** | **0** |
| 证据 | `paper_ooml_mathtype.json`（含 20 题逐题 exp/obs） |  |

抽 4 条明细（`exp` = 源 docx，`obs` = 卷面题框）：

| 题号 | 类型 | exp | obs |
|---|---|---|---|
| 00001 | OMML | math 6 / embed 0 / media 0 | math 6 / embed 0 / media 0 |
| 00002 | OMML | math 4 / embed 0 / media 0 | math 4 / embed 0 / media 0 |
| 00003 | OMML | math 1 / embed 0 / media 0 | math 1 / embed 0 / media 0 |
| 00004 | OMML | math 1 / embed 0 / media 0 | math 1 / embed 0 / media 0 |

### 15.5 答题卡（页数 / ArUco / QR / 几何）

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T5CardGeometry -v
```

| 检查 | 实测 | 结论 |
|---|---|---|
| PDF 页数 / PNG 数 | **4 / 4**（每页 2480×3508） | 通过 |
| 四角 ArUco | 每页检出 **0/1/2/3**（[0, 1, 2, 3]、[0, 1, 2, 3]、[0, 1, 2, 3]、[0, 1, 2, 3]） | 通过 |
| QR（cv2 直接解 PNG） | 每页解出 `{pid: P20261031-001, p: 页号}` | 通过 |
| 作答格几何 | 检查 **67** 个格子：越界 **0**、重叠 **0** | 通过 |
| `iter_answer_cells` | **21** 格 = manifest 答案空数（{'fill': 12, 'solution': 9}） | 通过 |
| 证据 | `card_geometry.json`、`card/card.pdf`、`card/card_1..4.png` |  |

### 15.6 判卷（10 张随机退化照片）

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T6GradePhotos -v
```

退化条件（verifier 自己写）：随机透视 **3%%** + 光照渐变 + 高斯噪声 + 3×3 模糊 + **JPEG 70** + 缩放到 72%%；
作答意图：`idx %% 4 == 0` 的题故意涂错（其余涂对），共 10 张。

| 指标 | 要求 | 实测 |
|---|---|---|
| 标定成功率 | ≥9/10 | **10/10** |
| QR 成功率 | — | **10/10** |
| 选择题判分一致率 | **=100%** | **30/30 = 100.0%** |
| 误判 unsure | 0 | **0** |
| 填空/解答 pending + 裁切图 | 有 | **55** 个 pending，`missing_crop=[]` |
| 耗时 | — | **0.35 s / 10 张** |
| 证据 | `grade_photos.json`、`photos/*.jpg`、`grading/<pid>/sub_*/` |  |

### 15.7 人工确认链路（manual 后端：上传 → 确认 → 重算分数）

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T7ServerConfirmChain -v
```

做法：真 FastAPI `TestClient` + **app.db 副本**（不碰真库）+ 独立 out 目录；`POST /papers` 建 10 题卷 →
按 manifest 的卡坐标涂黑（idx 为奇数的选择题故意涂错）→ 合成照片 `POST /submissions` →
把识别器设成 manual 等价物（识别不出）→ `POST /submissions/{sid}/reocr` → 逐空 `POST .../confirm`。

| 步骤 | 期望 | 实测 |
|---|---|---|
| 建卷 | 302 → pid | **302**，pid = `P20261031-002` |
| 上传判卷（同步阶段） | warp/qr ok，选择判完、填空解答 pending | warp_ok=1、qr_ok=1、score **3.0/9.0**、pending **3**、判错 idx=[1, 3, 5] |
| manual 重识别 | 识别不出 → pending 不变、**分数不变**（不静默判错） | pending **3**（不变）、score **3**（不变）、`empty_batch=true` |
| 逐空人工确认 | pending 归零、分数 = 原分 + 确认判对的空数 | before **3** → after **6 / 9**、filled **3**、done **True** |
| 结果页 | 200 | **200** |
| 证据 | `chain_step1_upload.json` / `chain_step2_reocr.json` / `chain_step3_confirm.json` |  |

### 15.8 训练卷（用判卷剩下的错题，走产品真链路）

```powershell
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T7ServerConfirmChain -v
python -m unittest discover -s app\tests -p "test_acceptance.py" -k T8TrainFromWrong -v
```

| 检查 | 要求 | 实测 |
|---|---|---|
| 入口 | 结果页「用错题生成训练卷」→ `POST /train` | **302 → 训练卷 `P20261031-003`** |
| 错题来源 | 本次判卷判错/待确认的题 | **03013、08846、10107**（3 道） |
| 题数 | 错题数 × n_per(3) | **9 题**（= 3 × 3） |
| 原题重复 | 不许出现原错题 | **无** |
| 覆盖题族/知识点/章节 | 每道训练题至少共享一项 | **题族 2 / 知识点 9 / 章节 9，缺口 0** |
| 同类题不足时的兜底 | 记录 shortfall | **无** |
| 纯函数复核（train.plan 单独跑） | 3 题 × 3 = 9、无原题、覆盖 0 缺口 | **9 题；题族 5 / 知识点 5 / 章节 8，缺口 0；跨题型 4 道（允许）** |
| 证据 | `chain_step4_train.json` / `train_plan.json` / `train_coverage.json` / `train_coverage_types.json` |  |

**快照 7（省纸模式落地后，2026-10-31 18:18:40）**：**799 用例 / 305.481 s / skipped=1 / failures+errors=0**，exit 0，**全绿**。日志 `app/tools/logs/full_compact.log`；与本快照同日 16:59 的 760 用例相比，用例数 760 → 799（省纸模式新增 t37/t38 用例）。

### 15.9 定版结论

八项全部达标。已知保留项仍按 §10 执行（**R1 语料「如图无图」2,932 题**、**R3 真实手写未验证**），
另有一条**记录级观察**（不算缺陷）：训练卷按「题族 → 知识点 → 章节 → 同题型」四级取题，
同族不同题型（如选择错题配到填空题）是允许的——本次 9 道训练题里 4 道跨题型，覆盖率 0 缺口。

---

## 16. 省纸模式（compact layout，2026-10-31 交付版）

> 定版时刻：**2026-10-31 18:18:40**（全量 unittest 日志时间）；实测命令：`python app\tools\_t16_layout_measure.py`（真 Word 渲染 + `paper.ink_ratio`）
> 判据：**同一套题（seed=20261031、mix 10/8/6）只换 layout**，compact 的卷面页数必须 ≤ normal；
> 题面长度差异很大（有的带图多问、有的两行结束），所以 **normal 不是固定 4 页**，页数随内容变化。
> 证据：`app/out/acceptance_v1/layout_compact.json`；样张目录 `app/out/compact/`、`app/out/compact_card/`、`app/out/captain_layout/`。

### 16.1 卷面：页数与墨迹率（同一 seed、同一 mix，只换 layout）

| 题量（选择/填空/解答） | compact 页数 | compact 每页墨迹 | normal 页数 | normal 每页墨迹 | 结论 |
|---|---|---|---|---|---|
| 10 题（5/3/2） | **1** | [4.94%] | 2 | [3.53%, 1.33%] | **compact ≤ normal** ✔ |
| 24 题（10/8/6） | **3** | [5.24%, 4.65%, 3.65%] | 4 | [3.39%, 4.25%, 2.79%, 2.82%] | **compact ≤ normal** ✔ |
| 50 题（25/15/10） | **5** | [4.84%, 4.93%, 6.30%, 4.62%, 3.95%] | 7 | [3.33%, 3.44%, 3.55%, 4.08%, 3.24%, 3.78%, 3.08%] | **compact ≤ normal** ✔ |

- 24 题：**3 页（compact） vs 4 页（normal）** —— 与队长 17:44 实测（3 vs 4）一致，墨迹率差不超过 0.1 个百分点（渲染/Word 版本舍入）。
- 10 题（5/3/2，与 `tests/test_paper.py` 的验收口径一致）：compact **1 页**（ink 4.94%）、normal **2 页**（末页 ink 1.33%）；
- 50 题（25/15/10）：compact **5 页** vs normal **7 页** —— normal 页数随题面长度变（队长那份 50 题是 8 页），所以**别把 4/8 页当常量**；
- 墨迹率区间（本轮全部 16 个页面）：compact **3.65%~6.30%**、normal **1.33%~4.25%**。
- **内容依赖**：同一「10 题」如果把 mix 换成 4/4/2（填空解答更多、题面更长），compact 会变成 **2 页**、末页 ink 仅 1.00%，`paper.needs_balance` 两档收紧后仍压不回 1 页 —— 所以「10 题 1 页」的前提是 **5/3/2**；24 题同理只在 10/8/6 下成立。

### 16.2 纸张对比（一份 24 题卷）

| 项 | 旧版（normal 卷面 + 单列卡） | 现在（compact 卷面 + 两列卡） |
|---|---|---|
| 卷面 | 4 页 | **3 页** |
| 答题卡 | 5 页 | **3 页** |
| 合计 | 9 页 | **6 页** |
| 双面打印 | 5 张纸 | **3 张纸** |

> 旧版 9 页 = captain 17:44 实测（卷面 4 + 卡 5）；现在的 6 页是本轮实测（3 + 3）。

### 16.3 机读能力不变（答题卡两列版式）

| 题量 | 卡页数 | 涂卡格 | 填答格 | ArUco | QR（cv2 解整页 PNG） | 越界 | 重叠 | 卡页码 |
|---|---|---|---|---|---|---|---|---|
| 10 题 | **1** | **20 个（71×71 px）** | **11** | **1/1 页 4 角全检出** | **1/1** | **0** | **0** | P2 |
| 24 题 | **3** | **40 个（71×71 px）** | **29** | **3/3 页 4 角全检出** | **3/3** | **0** | **0** | P4、P5、P6 |
| 50 题 | **5** | **100 个（71×71 px）** | **46** | **5/5 页 4 角全检出** | **5/5** | **0** | **0** | P6、P7、P8、P9、P10 |

- 涂卡格 **71×71 px**（mm(6) @300dpi）不变；填答格两种宽度 472 / 590 / 800 px，与旧版一致。
- **格子数对账**：24 题 = 选择 10 题 × 4 = **40 格**（实测 40）+ 填空解答 **29 个空**（实测 29）；50 题 = **100 + 46**；10 题 = **20 + 11**。
- **页码口径**：卡的页码接着卷面往后排（24 题 compact：卷面 3 页 → 卡 P4-P6；normal 4 页 → 卡 P5-P7），与 §12.2 的「各自编号 + P{A+M}」一致。
- **判卷未受影响**：判卷只看 ArUco/QR/格子坐标（都在 manifest 里），两列版式只改格子坐标；§15.6 的 10 张退化照片判分仍 100%。

### 16.4 UI 开关与默认值

- 组卷页有「**省纸模式（推荐）**」复选框（`templates/index.html`）：隐藏域先给 `normal`，勾选后追加 `compact`，服务端取**最后一个值**（不依赖 JS），脏值/缺省都退回 `compact`（`server.py` 的 `layout` 解析）。
- `paper._LAYOUT_SPEC` 两套参数：compact 收紧页边距/行距/题间段距，**不动字号、公式与图片尺寸**；normal 保持 56.7pt 页边距与 compose 默认 1.15 行距。
- API：`picker.build(params)` 认 `params["layout"]`；`paper.build_docx(..., layout=)` / `paper.render_paper(..., layout=)` 也可直接指定。

---

## 17. 新功能独立验收（t61：导入 / 改卷记录 / 学生信息）

> 验收方：verifier（不写实现代码，只写验收）；方式：**真 HTTP（fastapi TestClient，进程内）+ 真库副本**（`app/data/app.db` 239 MB 拷到 `app/out/acceptance_v2/work/app.db`，不碰真库）+ 独立 out 目录。
> 验收脚本：`app/tests/test_acceptance_v2.py`（10 条，verifier 另写，不复用开发侧用例）；命令：`cd E:\OCR\app && python -m unittest tests.test_acceptance_v2`
> 证据目录：`app/out/acceptance_v2/`（JSON 数字 + 页面 HTML + Edge 截图 + CSV + 回归日志）
> 采样时刻：**2026-11-07 01:49:41**（全量回归日志时间）

| # | 功能 | 结论 | 关键实测 |
|---|---|---|---|
| 1 | 题库导入 `/import` | **通过** | 20 行 CSV（18 好 + 2 坏）→ 预览 **inserted=0 / failed=2（逐行有原因）** → 确认 **inserted=18** → 库 **87748 → 87766（+18）** → 再导一次 **inserted=0 / skipped=18**、行数不变 |
| 2 | 改卷记录 + 撤销 | **通过** | 判 12 单元得 **6/12**、pending 6 → 改 2 空 → **8/12**、记录 **2 条** → 重复提交仍 2 条 → CSV **3 行**（表头+2）→ 撤销 → **回 6/12**、记录 **4 条**（含 2 条 undo） |
| 3 | 学生信息（v3 卡） | **通过** | v3 卡涂 `20250317` → 判卷 → `submissions.student_no = 20250317`、结果页/记录页都显示；旧 v2 卡（无学生区）→ 标定正常、`student_no` 为空串、无 500 |
| 4 | 全量回归 | **通过** | `python tools\run_tests.py --groups 6`：**1178 条用例 / 689.6 s / 6 组**，失败 **0** 条（exit 0） |

### 17.1 题库导入（`/import`）

做法：造 20 行 CSV（表头 `no,qtype,stem,answer,options,analysis,kp_id,chapter,figure`），其中
**第 19 行故意缺 answer、第 20 行选择题没有选项**；POST `/import` 两次（`dry_run=1` 预览 / `confirm=1` 写库），
每次读 `GET /import/report.json` 拿机读报告，再直接查库对账。

| 检查 | 期望 | 实测 |
|---|---|---|
| 预览 `dry_run` | true、不写库 | **dry_run=True**、`inserted=0`、库行数不变（87748） |
| 预览统计 | total=20 / failed=2 | **total=20 / failed=2** |
| 坏行逐行原因 | 每行带行号 + 人话 | **第19行「第 19 行 ACC61091：缺 answer（答案）—— 无答案的题不收」；第20行「第 20 行 ACC61092：选择题必须有选项（至少 2 个）」** |
| 确认导入 | inserted=18 | **inserted=18 / updated=0 / skipped=0 / failed=2** |
| 库内对账 | questions +18 | **87748 → 87766（+18）**，`no LIKE 'ACC61%'` 共 **18** 行，`subject_pk=7a` |
| 幂等（再导一次） | inserted=0、行数不变 | **inserted=0 / skipped=18 / failed=2**，行数仍 87766 |
| 报告与库一致 | 报告数字 = 库内实际 | 18 条题号全部可查、`failed=2` 两条也确实没进库 |
| 截图 | 预览态 / 报告态 | `app/out/acceptance_v2/01_import_preview.png` / `app/out/acceptance_v2/02_import_report.png` |
| 证据 | `import_preview.json` / `import_confirm.json` / `import_reimport.json` / `import_db_counts.json` / `import_20rows.csv` |  |

### 17.2 改卷记录留痕与撤销

做法：`POST /papers`（mix 6/4/2、seed=20261031、compact）→ 按 manifest 卡坐标涂对选择题 → 合成拍照
（透视 3% + 光照 + JPEG72 + 缩到 72%）→ `POST /submissions` 判卷 → 从 `detail_json` 取 2 个待确认空
→ `POST …/confirm` 改判 → 读 `/edits`、`/edits.csv`、`/status` → 原样重复提交 → `POST …/edits/{batch}/undo`。

| 步骤 | 期望 | 实测 |
|---|---|---|
| 判卷基线 | 标定成功、有 pending | warp_ok=**True**、qr_ok=**True**、score **6/12**、pending **6** |
| 改 2 个空 | 分数 +2、记录 2 条 | score **6 → 8 / 12**、`/edits` **2 条**（改判单元 [[7, 1], [8, 1]]） |
| 重复提交同一改动 | 不产生重复行 | 记录数 **2 → 2**（不变） |
| CSV 导出 | 行数 = 表头 + 记录数 | **csv_lines=3、data_rows=2、records=2**；表头 `时间,题号,空,改前,改后,改前文本,改后文本,操作者,批次,来源,备注` |
| 撤销该批 | 分数回到改前 + undo 留痕 | score **6 / 12**（=改前）；记录 **4 条**，source=['undo', 'undo', 'confirm', 'confirm']，备注「撤销批次 f13b25b3d8134b2a9fe85b611d4a5797」 |
| 页面 | 结果页有「改卷记录」区 | **200**，含改卷记录区与批次号 |
| 截图 | 改判后 / 撤销后 | `app/out/acceptance_v2/03_grade_result_after_confirm.png` / `app/out/acceptance_v2/04_grade_result_after_undo.png` |
| 证据 | `edits_00_baseline.json` / `edits_01_after_confirm.json` / `edits_02_duplicate.json` / `edits_03_csv.json` / `edits_04_after_undo.json` / `edits.csv` |  |

### 17.3 学生信息（v3 卡 vs 旧 v2 卡）

做法：`POST /papers` 建 v3 卡（`manifest.card.version=3`，带学生区）→ 在卡上涂学号 `20250317` + 涂对选择题
→ 合成拍照 → 判卷 → 查 `submissions.student_no` / `detail_json.student` → 打开结果页与 `/records`。
第二张卡把 manifest 的 `card.student` 区删掉并把 version 置 2（**这就是旧 v2 卡的形状**，v2 卡本来没有该区），再判一次。

| 检查 | 期望 | 实测 |
|---|---|---|
| v3 卡版本与区域 | version=3、有 student 区 | **version=3**、has_student_area=**True** |
| 学号落库 | = 涂的那个学号 | `submissions.student_no` = **20250317**；`detail_json.student` = {"no": "20250317", "name": ""} |
| 结果页 / 记录页显示 | 都能看到学号 | 结果页 **有**、记录页 **有** |
| 旧 v2 卡 | 能判、`student_no` 为空、不报错 | 标定 warp_ok=**True**、`student_no`=**''**、无 500 |
| 截图 | v3 卡样张 / 结果页 / 记录页 | `app/out/acceptance_v2/05_v3_card_filled.png` / `app/out/acceptance_v2/06_result_page_student.png` / `app/out/acceptance_v2/07_records_page_student.png` |
| 证据 | `student_01_v3.json` / `student_02_legacy.json` |  |

### 17.4 全量回归（`python tools\run_tests.py --groups 6`）

```powershell
cd E:\OCR\app
python tools\run_tests.py --groups 6
```

| 项 | 值 |
|---|---|
| 用例总数（各组之和） | **1178** |
| 各组耗时之和 | **689.6 s** |
| 失败 / 错误 | **0** 条 |
| 结果 | **全部通过（exit 0）** |
| 日志 | `app/out/acceptance_v2/full_regression.log` |
| 本轮新增验收用例 | `tests/test_acceptance_v2.py` **10 条全绿**（导入 3 / 改卷记录 5 / 学生信息 2） |

### 17.5 结论与遗留

三项功能**都通过独立端到端验收**，证据链完整（命令行日志 + 数字 JSON + 页面截图 + CSV）。

遗留（不影响本次结论，供后续排期）：

1. **导入预览没有逐行 items**：t53 的报告只给计数与 `errors`，所以预览态只能列「问题行」；模板里已有 `items` 防御分支，后端补 `items[:20]` 就会自动出现逐行预览。
2. **【本轮亲测复现的缺陷】显式传不支持的格式会 500**：`POST /import` 带 `format=txt` 时，`import_routes.py` 的 `suffix = {"json": ".json", "excel": ".xlsx", "csv": ".csv"}[kind]` 抛 **`KeyError: 'txt'`**，该行在 try 外面 → 真服下是 500（不是中文提示页）。证据 `app/out/acceptance_v2/probe_format_txt.json`，复现 `python app/out/acceptance_v2/_probe_txt_format.py`。**不是本次三项功能的阻塞项**（正常路径选 CSV/Excel/JSON 或按扩展名自动识别都不触发），建议改 `.get()` 走「格式认不出」分支。
3. **撤销是整批粒度**：单条撤销需要后端再给接口；撤销行用新 `batch_id`，不污染原批次。
4. **姓名仍是手写框**（读不出）：结果页给老师补录入口（`POST /submissions/{sid}/student`），补录后页面/CSV 立刻带上。

---

## 18. 复验：多学科隔离 + 导入失败分支（t65，2026-11-07）

> 验收方：verifier（另写用例，不复用开发侧）；方式：**真 HTTP（fastapi TestClient）+ 真库副本**（87,748 题 / 33 学科，不碰 `app/data/app.db`）+ 独立 out 目录。
> 脚本：`app/tests/test_acceptance_v3.py`（17 条）；命令：`cd E:\OCR\app && python -m unittest tests.test_acceptance_v3` → **Ran 17 tests, OK, exit 0**
> 证据：`app/out/acceptance_v3/`（`summary.json` + 各节 JSON + 3 张 Edge 截图）

| # | 复验项 | 结论 | 关键实测 |
|---|---|---|---|
| 1 | 数学卷不串科 | **通过** | 选「数学·七上」出 10 题（`P20261107-001`）→ 逐题回查 `questions.subject_pk` **全 = math7a**；题面/章节无 ENG/HIS/PHY/CHM/BIO/语文 等标记 |
| 2 | 英语卷 | **通过** | 选「英语·九上」出 3 题（`P20261107-002`）→ 全 `english9a`，章节 `英语·九上·Unit 1 …` |
| 3 | 训练卷同科 | **通过** | 用英语题号 `392451/392452/392453` 生成训练卷（`P20261107-003`）→ 新卷仍 `english9a` |
| 4 | 学科过滤数字自洽 | **通过** | `pool_report.excluded_other_subject = 69,662` == SQL 复核（合格题 87,652 − math7a 17,990） |
| 5 | `/import` 失败分支 | **通过** | `format=txt` / 未知扩展名 / 坏 xlsx → **全部 200** + 中文「不支持的格式：txt（支持的格式：csv / excel / json / docx）」/「打不开，请另存为 .xlsx」，不再 500 |
| 6 | 文档链接 | **通过** | README 18 处截图链接 0 失效；`docs/INDEX.md` 相对链接 0 失效 |
| 7 | **全量回归** | **未通过（1 失败）** | `python tools\run_tests.py --groups 6`：**1,227 用例 / 691.5 s / 6 组**，失败 **1** 条 → 见 18.7 |

截图：`18_compose_subject_select.png`（组卷页学科 + 年级/册次下拉）、`19_english_paper_preview.png`（英语卷 3 题，知识点全 ENG9A-U1）、`20_import_unsupported_format.png`（导入不支持格式的 200 友好页）。

### 18.7 ❌ 唯一失败：非数学学科「按章节组卷」被默认学科 math7a 卡住（t62 回归）

```
ERROR: tests.test_importers.TestMultiSubject.test_two_subjects_coexist_and_filter
picker.ScopeError: 未知章节：语文·识字；库里可用章节（学科 math7a 内）：第1章 有理数
```

复现（真库副本，`chinese7a`）：

| 步骤 | 结果 |
|---|---|
| `importers.subject_scope(conn, "chinese7a")` | `{"chapters": ["语文·七上·第一单元", …], "kps": []}` —— **不含 subject_pk** |
| `picker.build({scope: 上面这个, …})` | ❌ `ScopeError`：按 **math7a** 校验章节 |
| `picker.build({scope: 加上 subject_pk="chinese7a", …})` | ✅ 出题 `["100048"]` |

根因（t62 引入）：`picker._subject_clause()` 在 scope 没学科时**默认 `math7a`**；
而 `importers.subject_scope()`（给 picker 用的学科过滤助手）只回传 `chapters`，不带学科，
于是「语文的章节」被拿去和「数学的已知章节」比对 → 全判未知。

建议修法（二选一，均不在 t65 的写范围）：
1. `importers.subject_scope()` 补 `"subject_pk": subject_pk, "subjects": [subject_pk]`（一行，最小改动）；
2. 或 `picker._validate_scope()` 在**显式给了 chapters/kps** 时不要用默认学科过滤（默认为 math7a 只应作用于「没给范围」的整卷）。

影响面：UI 组卷（表单带 subject_pk）不受影响；受影响的是脚本/CLI 走 `importers.subject_scope()` 的路径，以及这条回归用例。
### 18.8 ✅ 第二轮复验（t68，修复后）：全绿

t67 修法（方案①）：`importers.subject_scope()` 返回值补 `subject_pk` / `subjects`，
picker 不再把非数学学科的章节套到默认学科 math7a 上。

| 复验项 | 命令 | 结果 |
|---|---|---|
| 原失败用例 | `python -m unittest tests.test_importers` | **14 tests OK**（修前 1 ERROR） |
| 组卷/服务 | `python -m unittest tests.test_picker tests.test_server` | **174 tests OK, exit 0** |
| 仓库根复验 | `python -m unittest app.tests.test_picker app.tests.test_server app.tests.test_docs` | **191 tests OK, exit 0** |
| 独立验收（本轮） | `python -m unittest tests.test_acceptance_v3` | **17 tests OK, exit 0** |
| **全量回归** | `python tools\run_tests.py --groups 6` | **6/6 组 OK，1,227 用例 / 765.5 s / 失败 0**（exit 0） |

数字复核（真库副本，与 18.x 一致）：数学卷 10 题 subject_pk 全 `math7a`；英语卷 3 题全 `english9a`；
`pool_report.excluded_other_subject = 69,662` == SQL（87,652 − 17,990）；
`/import` txt / 未知扩展名 / 坏 xlsx 仍 **200 + 中文**。
证据：`app/out/acceptance_v3/summary.json`（含第二轮时间戳与回归数字）。


---

## 19. 独立验收：顺序管理持久化 + 剩余题量实时显示（t71，2026-11-07）

验收方式：真服务 `http://127.0.0.1:8710`（真 HTTP）+ 真库只读 SQL 复核 + headless Chrome 真渲染截图（375 / 1080）。
写库类操作只在**临时小库**或真库的 `subjects.sort_order / chapter_order` 小表上做，**没有复制 150MB 库**；`out/acceptance_v4/` 总计 **0.6 MB**。

### 19.1 顺序管理（真 HTTP）

| 动作 | 观察 | 结论 |
|---|---|---|
| `GET /manage/order` | 200，学科 `math7a, math7b, math8a, math8b, math9a, math9b`；单元前 3 = 第1章/第2章/第3章 | ✅ |
| `POST /manage/order`（`action=save`，交换前两个单元） | 200；保存后 `/manage/order?subject_pk=math7a` 单元前 3 = **第2章 / 第1章 / 第3章** | ✅ 持久化 |
| 组卷页 `GET /?subject_pk=math7a` | 单元顺序与顺序页一致（第2章 在前） | ✅ 两页同步 |
| `POST /manage/order`（`action=reset`） | 恢复默认：单元回到 第1章/第2章/第3章，与 `db.list_chapters_ordered` 默认序一致 | ✅ 恢复默认可用 |
| 老库（subjects 无 sort_order）打开 | 自动补 `sort_order` 列，老数据不丢，不报错 | ✅（`tests/test_acceptance_v4.py::test_old_db_without_sort_order_opens`） |

> 截图：`app/out/acceptance_v4/order_1080.png`（1080 宽）、`app/out/acceptance_v4/order_375.png`（375 宽）。
> 验收结束后已 `reset`，真库顺序回到默认。

### 19.2 `/api/available` 与 SQL 复核（真服 + 真库只读）

| 范围 | API total | SQL 复核 | 差 | 耗时 |
|---|---|---|---|---|
| math7a 整册 | **15811** | **15811** | **0** | 29.2 ms（缓存命中） |
| 单章节「第1章 有理数」 | **3020** | **3020** | **0** | 24.9 ms |
| 多章节并集（3 个单元） | **8406** | **8406** | **0** | 3.1 ms |

- 单章节 3020 ≤ 整册 15811 ✅
- 冷路径（新起进程、逐范围未命中缓存）：**332.6/287.6/302.4** ms（中位 **302.4** ms < 300 ms）
- 非法入参一律 400 + 中文：缺学科 `缺少参数 subject_pk（学科）`；未知学科 `没有这个学科：nope（题库里查不到）`；未知章节 `范围不在题库里：未知章节：…`；全是 **400** ✅

### 19.3 组卷页前端实测（headless Chrome 真渲染）

| 视口 | 证据 | 结果 |
|---|---|---|
| 1080 | `compose_whole_1080.png` / `compose_one_unit_1080.png` | 剩余题量行显示数字，整册 15,811 与单单元不同 → 数字随范围变化 ✅ |
| 375 | `compose_one_unit_375.png`、`order_375.png` | ❌ **横向溢出**：导航「顺序管理」被截断、单元行右侧 ↑↓ 按钮跑出视口、正文末行被裁 |

### 19.4 ❌ 两处发现（本次验收未通过项）

| id | 严重度 | 现象 | 证据 |
|---|---|---|---|
| F1 | medium | **375px 横向溢出**：组卷页与顺序管理页都有内容超出视口（按钮/导航/正文被裁） | `out/acceptance_v4/order_375.png`、`compose_one_unit_375.png` |
| F2 | medium | **单元名回传被判无效**：`GET /?subject_pk=math7a&chapters=数学·七上·第1章 有理数` 返回黄色告警「所选的学科/知识点已经不在库里」并**回落整册 15,811**；但同一字符串就在页面下拉的候选项里 | 截图顶部告警；`/api/available` 用同一字符串却 200 且 total=3020（即两个入口的章节校验不一致） |

### 19.5 自动化验收 + 全量回归

- `cd E:\OCR\app && python -m unittest tests.test_acceptance_v4` → **7 tests OK, exit 0**
- `cd E:\OCR\app && python tools\run_tests.py --groups 6` → ****6 组中 4 组 FAIL / 2 组 OK，总用时 992.5 s，exit 1**（详见 §19.6）**

证据：`app/out/acceptance_v4/summary.json`、`app/out/acceptance_v4/http.json`。

### 19.6 ❌ 全量回归（只跑一次）：6 组里 4 组失败

`cd E:\OCR\app && python tools\run_tests.py --groups 6` → **组 1 FAIL / 组 2 FAIL / 组 3 FAIL / 组 4 OK / 组 5 OK / 组 6 FAIL**，总用时 **992.5 s**，exit 1。

| 组 | 结果 | 已知失败用例（抽样） |
|---|---|---|
| 1 | FAIL（283 用例） | 未逐条记录（当次只看尾部） |
| 2 | FAIL（215 用例） | 未逐条记录 |
| 3 | FAIL（154 用例） | `test_acceptance_v3`：`test_train_ok` 302≠400、`test_train_items_stay_english`、`setUpClass T2EnglishPaper` —— **依赖已不存在的 english9a** |
| 4 / 5 | OK | — |
| 6 | FAIL（201 用例） | `test_grade` 真照片 5 条：`test_by_ids_judges_nine_units`、`test_four_ids_decode`、`test_ids_exist_in_bank`、`test_one_bit_flip_is_unreadable`、`test_legacy_4x4_reports_legacy_card` |

复跑单模块确认（非全量）：`python -m unittest tests.test_acceptance_v3 tests.test_grade` → **121 tests, FAILED (failures=2, errors=1, skipped=9)**。
结论：**当前工作树不是绿的**，交付前必须先修这三类失败（英语用例改数学/跳过、grade 真照片夹具、组 1/2 待定位）。

（按任务要求「全量回归只跑一次」，本条不再复跑。）

### 19.7 t82 修复复验（2026-11-07 12:29）

| 发现 | 修法 | 复验 |
|---|---|---|
| F1 375px 横向溢出 | `static/app.css` 新增 `.order-list/.order-item/.order-label/.order-btns` 规则：`flex-wrap: wrap` + `min-width: 0`；`@media (max-width: 760px)` 让 ↑↓ 换行 | CDP 实测（375 模拟视口）：顺序管理 `scrollWidth=375 / clientWidth=375`、34 个 ↑↓ 按钮全部落在视口内；组卷页单单元 `375/375`。截图 `out/acceptance_v4/order_375.png`、`compose_one_unit_375.png`，数据 `out/acceptance_v4/scrollwidth_375.json` |
| F2 单元值回传被判「不在库里」 | 复核后确认**当前代码已无此问题**（t69/t73 之后 `read_kps` 与 picker 校验同源）；旧现象来自 t71 验收时跑着旧进程的服务 | 新增回归 `tests/test_acceptance_v4.py::ChapterValueRoundTripTest`：对 `list_chapters_ordered("math7a")` 的**每个**单元值，`GET /?chapters=<v>` 都不得出现「不在库里」且数字 == `/api/available`（11/11 通过） |
| F3 `test_acceptance_v3` 硬依赖 english9a | 加 `ENG_SKIP = unittest.skipUnless(_has_english9a(), ...)`，T2/T3 两个类跳过，数学路径断言保留 | `python -m unittest tests.test_acceptance_v3` → **17 tests OK (skipped=5)** |

全量回归（t82 跑一次）：`tools\run_tests.py --groups 6` → **组 1 FAIL / 组 2 FAIL / 组 3–6 OK**（892.4 s）。
剩余两组失败全部在**答题卡/判卷域**（`tests.test_acceptance.T5CardGeometry` 的 aruco/QR、`T6GradePhotos`、`test_acceptance_v2.T3StudentInfo`），归 grading-engineer（t78 正在改答题卡 v4），不属本次修复范围。

---

## 19.8 t83 第二轮独立验收（2026-11-07，顺序管理 + 剩余题量）

验收环境：真语料 `data/app.db`（**27,774 题 / 6 个数学学科 / math7a 11 个单元**，150.7 MB），
headless Chrome（CDP 直连，无第三方库，helper 见 `app/out/_cdp83.py`）。
**没有复制整库**：写库类操作全部在临时小库（3 学科 / 16 题）上做，真语料只用 `sqlite3 mode=ro` 只读连接计数。

### 19.8.1 顺序管理（真页面点击 ↑ → 保存 → 刷新）

| 步骤 | 结果 |
|---|---|
| 初始顺序（临时库，插入序 语文→数学→英语） | `语文 / 英语 / 数学` |
| 点「数学」的 ↑ 两次（`#subject-list` 内 `button.ord-up`） | `数学 / 语文 / 英语` |
| 点「保存顺序」（`#order-form` 提交）后**刷新页面** | `数学 / 语文 / 英语` ✔ |
| 库里实际值 `subjects.sort_order` | `math7a=1, chinese7a=2, english7a=3` ✔（真持久化，不是前端假象） |
| 组卷页 `#subject-name` 下拉顺序 | `数学（16 题）/ 语文（2 题）/ 英语（2 题）` ✔ 跟随 |
| 单元：「第2章 整式」↑ 移到第一 → 保存 → 刷新 | `第2章 整式 / 第1章 有理数 / 第3章 一元一次方程` ✔ |
| 库里 `chapter_order` | `第2章 整式=1, 第1章 有理数=2, 第3章=3` ✔ |
| 组卷页章节复选框顺序 | 同上，跟随 ✔ |

证据：`out/acceptance_v4/order_subjects_1080.png`、`order_units_1080.png`、
`order_375.png`（375px 移动端，`scrollWidth=375/clientWidth=375`，溢出 0），
数据 `out/acceptance_v4/order_round2.json`。

### 19.8.2 `/api/available` 计数与耗时（真语料，只读连接）

| 项 | SQL 直查 | picker.count_available | 结论 |
|---|---|---|---|
| math7a 整册 | 18,086（选择 6,588 / 填空 5,227 / 解答 6,207 / 其他 64） | **15,811**（选择 5,745 / 填空 4,668 / 解答 5,343 / 其他 55） | 差 2,275 = 体检 flags + 缺图 + 无答案 + 选择题答案非字母（picker 的既定过滤），逐题型差额已记在 `available_round2.json` |
| math7a 单章「数学·七上·第1章 有理数」 | 1,381（选择 844 / 填空 295 / 解答 242） | **1,381**（同） | **差 0** ✔，且 1,381 ≤ 18,086 ✔ |

冷路径（清 TTL + picker 缓存后，同一进程内计时；**整机空载时测**）：

| 场景 | 耗时 |
|---|---|
| 整册（首次） | **188.9 ms** ✔ < 300ms |
| 单章（首次） | 448.1 ms ✘ > 300ms |
| 单章冷路径 ×3 | 439.5 / 438.9 / 443.4 ms ✘ |

**剂量的第二种口径（快路径 vs 参考实现）**：`picker.count_available`（SQL 快路径）对
`picker.count_available_slow`（逐行参考实现，等价于带全部体检/答案过滤的 SQL）逐题型**差 0**，
整册与单章都一致 → 「计数与 SQL 复核一致」这条以**同口径**成立；
原始 SQL 的 18,086 与 15,811 之差是 picker 的既定过滤（见上表），不是漂移。
证据：`out/acceptance_v4/available_fastslow.json`（整册 fast=slow=15,811；单章 fast=slow=1,381）。

> 单章冷路径偏慢与 t73 遗留一致：换新 scope 时排除集合要按 (qtype,chapter,kp) 重新求和。
> 命中缓存后（页面渲染已算过一次）`/api/available` 实测 ~5ms，用户日常操作无感。
> 数据：`out/acceptance_v4/available_round2.json`。

### 19.8.3 组卷页前端实测（375px / 1080px）

| 视口 | 溢出 | 首屏 | 勾「第1章 有理数」 | 取消勾选 | 超量红字 |
|---|---|---|---|---|---|
| 375（mobile 模拟） | `scrollWidth 375 = clientWidth 375`，**溢出 0** | 剩余 **16** 题（选择 8 / 填空 5 / 解答 3） | **8** 题（选择 8） | 回到 **16** 题 | 选择要 10 只剩 8 / 填空要 99 只剩 5 / 解答要 6 只剩 3 |
| 1080 | `scrollWidth 1065 = clientWidth 1065`，**溢出 0** | 同上 16 | 同上 8 | 同上 16 | 同上三条 |

截图：`out/acceptance_v4/compose_375.png`、`compose_1080.png`；数据 `compose_round2.json`。

### 19.8.4 全量回归（只跑一次）

`python tools\run_tests.py --groups 6` → **1,368 用例 / 981.4 s / 组 1 FAIL，其余 5 组 OK**（退出码 1）。

| 组 | 结果 |
|---|---|
| 1 | **FAIL**：`tests.test_acceptance.T5CardGeometry.test_02_aruco_readable`、`test_03_qr_payload`、`T6GradePhotos.test_01_ten_photos` |
| 2–6 | OK |

**这 3 条不是确定失败**（同一台机器、同一份代码）：

| 复跑方式 | 结果 |
|---|---|
| `python -m unittest tests.test_acceptance`（整模块） | **34 tests OK**（70.8 s） |
| `python -m unittest tests.test_acceptance.T5CardGeometry tests.test_acceptance.T6GradePhotos` | **8 tests OK**（4.3 s） |

→ 判定为**共享产物目录导致的顺序相关 flake**：`card_fixture()` / `_publish()` 把答题卡 PNG/PDF
写进同一个 `out/acceptance*` 发布目录，全量跑时被其它模块的产物覆盖，aruco/QR 与拍照判卷读到的是别人写的图。
「0 失败」这一条**未达成**；要么把这几条测试改成用各自独立的临时目录（建议），要么把发布目录按 pid 隔离。

---

## 19.9 t86 第三轮独立验收（2026-11-07，修正口径后复验）

### 19.9.1 全量回归（只跑一次）

`python tools\run_tests.py --groups 6` → **1,389 用例 / 985.1 s / 组 1 FAIL、组 5 FAIL，其余 4 组 OK**（exit 1）。

| 组 | Ran 行 | 用时 | 结果 |
|---|---|---|---|
| 1 | Ran 313 | 196.2 s | **FAIL**：`tests.test_acceptance.T1DataLayer.test_01_counts` |
| 2 | Ran 309 | 295.0 s | OK |
| 3 | Ran 262 | 209.4 s | OK |
| 4 | Ran 179 | 54.9 s | OK |
| 5 | Ran 147 | 207.4 s | **FAIL**：`test_ai_prompt`（`_FailedTest` 导入期失败） |
| 6 | Ran 179 | 22.4 s | OK |

比 t85 的「4 组 FAIL」少了两组（t85 报的 `test_paper.CompactPageTargetTest ×3` 与
`test_compose_buttons.DeadIdTest` 在本轮都已转绿：前者本轮单跑通过；后者是 ui 侧新增了纯静态提示
`#excluded-hint`（无交互），已补进 `tests/test_compose_buttons.py` 的白名单并写明理由）。

**两条失败都不是本轮两个功能的问题**，且都不可单独复现：

| 失败 | 单跑结果 | 判定 |
|---|---|---|
| `test_acceptance.T1DataLayer.test_01_counts` | 题库重建后的计数断言（27,776 题 / 43 道 3 选项选择题）没跟上数据 | 数据域（data-engineer），t85 已同类修过两处 |
| `tests.test_ai_prompt` 导入期 ERROR | `python -m unittest tests.test_ai_prompt` → **14 tests OK** | 分组加载期偶发（同 t83 那类「拼单跑才炸」），需要按模块隔离/加 `__init__` 排查 |

> 结论：**「6/6 组 OK」仍未达成**；t83 定位的答题卡产物 flake 已消失（`test_acceptance` 在组 1 里只剩 data 计数一条）。

### 19.9.2 `/api/available` 计数口径（改为与 picker 基准实现比）

| 对比 | 结果 |
|---|---|
| 整册：`count_available` vs `count_available_slow` | 逐题型 **差 0**（选择 5,745 / 填空 4,668 / 解答 5,343 / 其他 55 = **15,811**） |
| 单元（抽查数学·七上第 1–4 章） | 逐题型 **差 0**（844/295/242、688/113/62、43/1337/1107、0/564/332） |

裸 SQL 的 **18,086** 与 picker 的 **15,811** 相差 **2,275**，按过滤原因拆开（`out/acceptance_v4/gap_reasons.json`）：

| 原因 | 条数 | 例（题号） |
|---|---|---|
| 缺图（题干说「如图」但题面没图） | **2,026** | 00026、00041、00049、00050 |
| 选择题答案非字母（不是 A–D） | **129** | 00586、00651、01305、02516 |
| 空题（`is_empty=1`） | **96** | 00131、05565、05567、05568 |
| 其它口径（need_answer / 成对规则等残差） | 24 | — |
| **合计** | **2,275** | |

> 口径结论：验收只跟**同口径**的基准实现比（差 0）；裸 SQL 多出来的 2,275 是 picker 的既定过滤，不是漂移。

### 19.9.3 单章冷路径（未算过的单元，清缓存后 3 次）

目标单元「综合」（math7a 最后一个单元，本轮之前从未算过）；清 TTL + picker 三个缓存后计时：

| 次数 | 耗时 |
|---|---|
| 1 | 325.4 ms |
| 2 | 283.3 ms |
| 3 | 268.0 ms |
| **中位** | **283.3 ms** ✔ < 300 ms |

第一次复测（同日）为 262.8 / 279.2 / 285.1 ms，中位 **279.2 ms**。t85 预聚合前是 439.5 / 438.9 / 443.4 ms。

### 19.9.4 前端复核（真库 + headless Chrome，只读 GET）

| 项 | 375px | 1080px |
|---|---|---|
| 顺序管理 | `scrollWidth 375 = clientWidth 375`，溢出 **0**，18 个条目 | — |
| 组卷页溢出 | **0** | **0** |
| 「本范围剩余」 | 整册 **15,815** 题（选择 5,745 / 填空 4,671 / 解答 5,344 / 其他 55）；勾「第1章 有理数」→ **1,381**（选择 844 / 填空 295 / 解答 242） | 同左 |
| 超量红字 | 选「第4章 几何图形初步」（选择可出 0 道）→ **「选择要 10 道，本范围只剩 0 道」** | 同上 |

截图：`out/acceptance_v4/t86_order_375.png`、`t86_compose_375.png`、`t86_compose_375_redline.png`、
`t86_compose_1080.png`；数据 `out/acceptance_v4/t86_ui_timing.json`、`available_vs_slow.json`、`gap_reasons.json`。

> 顺序管理的「点 ↑ 保存 → 刷新保持」仍以 t83（§19.8.1）为证：本轮只做只读复核，避免把写库操作打到正在使用的真库上。
