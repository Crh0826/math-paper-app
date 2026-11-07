# 卷子契约（CONTRACT）· 所有模块必须照此实现

> 本文件是并行开发的**唯一接口事实来源**。改契约必须先改本文件并在团队里说一声。
> 代码根目录：`E:\OCR\app`；原始题库 `E:\OCR\00001.docx … 22898.docx` **只读**。
> Python 3.14 · 已有依赖：numpy / opencv(cv2, 含 aruco) / pillow / python-docx / openpyxl / pymupdf / reportlab / lxml / qrcode / pywin32 / jinja2 / requests / fastapi / uvicorn / python-multipart（后三个为 Web 层服务端依赖，已装）

## 0. 已冻结的底座（不要重写，只允许按需扩展）

| 模块 | 作用 | 状态 |
|---|---|---|
| `docx_rich.py` | 题目 docx → token 流（text / math(OMML) / embed(MathType-WMF) / table） | 已绿，8 测试 |
| `compose.py` | token → docx（rId 重映射、媒体注册、中文字体） | 已绿，6 测试 |
| `taxonomy.py` | 知识点体系 + 标题/题干打标（82.7% 具体知识点，5.2% 章级，12.1% 综合） | 已绿，12 测试 |
| `families.py` | `【例N】/【变式N-M】` → 题族键（2,671 族，1,760 族 ≥3 题） | 已绿，10 测试 |
| `db.py` | SQLite 建表 / 连接 | 已绿（表结构见下） |

> **媒体铁律（2026-10-31 踩坑后写死）**：任何带 `r:embed / r:id / r:link` 的 XML 片段
> —— **包括 `w:tbl` 表格** —— 插入新文档前都必须做 **rId 重映射**并把二进制注册进新包。
> 漏一个就是悬空关系，Word 判"文件可能已经损坏"，PDF 导不出。
> 历史 bug：`table` token 只搬 XML 不搬媒体，导致 **398 题（1.7%）** 入卷即废；
> 24 题卷子命中概率约 34%。`paper.check_docx` 必须硬校验：document.xml 里所有
> `r:id/r:embed/r:link` 都能在 `word/_rels/document.xml.rels` 中找到（dangling=0）。

## 1. 数据库（`app/data/app.db`，由 `build_db.py` 重建）

> **题库口径（2026-10-31 用户决定）：只保留有答案的题。** `build_db.py` 默认不导入 `answer_raw=''` 的题
> （`--keep-unanswered` 可保留旧行为）。裁剪后 questions = **12,280** 行（其中 `is_empty=0` 12,184），
> 题型池：选择 5,012 / 解答 4,323 / 填空 2,787 / 其他 62 / 判断 0。
> 原始 22,898 份 docx 仍只读保留，随时可重建；任何统计脚本不要硬编码 22,898。

### 1.1 外部题库导入（`import_qbank.py`，2026-10-31 增）

```bash
python import_qbank.py --from json|excel|csv|docx --file <路径> [--db <app.db>] \
                       [--subject 数学] [--grade 七年级上] [--version 沪科版] \
                       [--dry-run] [--report] [--list-subjects]
```

统一 JSON 结构（顶层也接受裸数组）：

```json
{"subject":"数学","grade":"七年级上","version":"沪科版","items":[
  {"no":"00001","qtype":"选择","stem":"…","options":["A．…","B．…"],
   "answer":"B","analysis":"…","kp_id":"1.2","chapter":"第1章 有理数",
   "figures":["media/1.png"]}]}
```

- `qtype`：选择 / 判断 / 填空 / 解答 / 其他（单选、多选题、判断题、计算题、简答题、证明题 自动映射）。
- Excel/CSV：表头别名（题号/编号、题型/类型、题干/题目、答案/正确答案、解析、知识点/考点、章节、图片）；
  选项可 A/B/C/D 四列，或一列竖线分隔 `A．1|B．2|C．3|D．4`；CSV 自动识别 UTF-8-BOM / UTF-8 / GBK。
- **幂等**：按 `no` upsert；重复导入只更新不重复插入。
- 坏行（缺 `no`/`answer`、`qtype` 非法）只进报告，不落库；报告写 `data/import_report.json`。
- 多学科：`subjects(subject_pk, name, grade, version)` + `questions.subject_pk` + `kps.subject_pk`；
  老库自动迁移并把存量归到 `math7a`；组卷侧按 `scope.subject_pk/subjects` 过滤（见 §2，t11）；
  注意 `kps.kp_id` 仍是全局主键 —— **导入多学科时 kp_id 必须带学科前缀**（如语文 `CN1.1`），否则会撞主键。
- 无原始 docx 的题（15 万题口径）：`paper.source_tokens` 回退读 `questions.tokens_json`，
  只支持**无图题**（text/math/无图表格）；含 `embed` 或带媒体表格的题会报 `PaperError`（避免缺图卷面）。
- 导入的题**不自动生成** `quality_flags`，需另跑 `python quality.py`。


`questions(no TEXT PK, qtype, option_count, sub_count, is_empty, media_count, text_len, text_plain,
 tokens_json, fmt, src_title, src_para, family_key, kp_id, chapter, kp, kp_source,
 answer_raw, check_status)`
`families(family_key PK, src_title, example_no, size, kp_id)`
`kps(kp_id PK, chapter, kp, n_questions)`
`papers(pid PK, created_at, title, params_json, manifest, docx_path, pdf_path, n_items)`
`submissions(sid PK, pid, created_at, image_path, warp_ok, qr_ok, score, full, detail_json)`
`mastery(key PK, attempts, correct, last_seen, interval_days, due_at)` ← 掌握度（简化 SM-2：对 1→3→7→15 天，错回 1 天），`key` 用题号
`quality_flags(no PK, flags TEXT, detail TEXT)` ← 题库体检快照（flags 逗号分隔：`no_stem` / `stem_only_placeholder` / `analysis_left` / `short_stem` / `answer_mismatch` / `no_answer` / `heavy_layout` / `dup_text` / `fig_missing`）；**`build_db --fresh` 后必须重跑 `python quality.py` 刷新**
`dup_groups(group_id, no PK, size)` ← 近重复分组（裁剪后真重复=0，仅骨架重复，作为可选项不默认排除）

> 裁剪后的实测（12,280 题）：`fig_missing` 2,032（严格口径 2,048，差 16 题是"图在表格里"）、`no_stem` 2、`short_stem` 96、`answer_mismatch` 156、`heavy_layout` 389、`dup_text` 34、`no_answer` 0。
> `quality.filter_printable(..., need_answer=True)` 默认排除 `no_stem/stem_only_placeholder/analysis_left/answer_mismatch/fig_missing`。

- `qtype ∈ {选择, 填空, 解答, 判断, 其他}`
- `tokens_json`：`Token.to_dict(with_data=False)` 的数组，**不含二进制**；排版时用 `docx_rich.extract_tokens(E:\OCR\{no}.docx)` 重新取（快，<3ms/题）
- `answer_raw`：原文答案；选择是单字母，解答是 `(1)x=4 (2)x=9` 形式，填空多半是单值

## 2. 组卷参数（web 表单 → `picker.py`）

```json
{
  "title": "七年级数学 · 训练卷 A",
  "mix":   {"选择": 10, "填空": 8, "解答": 6},
  "scope": {"subject_pk": "math7a", "subjects": ["math7a"],
            "chapters": ["第1章 有理数"], "kps": ["1.2", "3.2"], "kp_mode": "any"},
  "requirements": {"need_answer": true, "exclude_empty": true, "exclude_no_family": false,
                   "exclude_fig_missing": true, "exclude_mismatch": true, "strict": false},
  "seed": 42,
  "layout": "compact",
  "calibration": true,
  "space": {"选择": 0, "填空": 0, "解答": 0},
  "nos": ["00012", "00457"],
  "exclude_nos": ["00001"]
}
```
- `calibration`（默认 **true**）：卷面 PDF 前附一页 **100mm 校准页**（100×100mm 方框 + 10mm 刻度尺 +
  A4 外框 + 四角标记 + 「打印后量一下，不是 100mm 说明被缩放，请选实际大小/100%」说明；本页不判卷）。
  840 打印设置一旦缩放，答题卡的 Aruco/QR 坐标就全废 —— 这页是给用户的物理校准手段。
  卷面页数仍按内容页算（`manifest.paper.pages`），答题卡页码接着内容页数走，不受校准页影响；
  关掉它（`false`）就只出内容页。`/print.pdf` = 1(校准) + 卷面 + 答题卡。
- `layout`（默认 **compact**）：版式。`compact` = 省纸模式（页边距 1.4cm、行距 1.0、段距 1pt、
  题框内边距减半、题间空段 0.5pt；10 题 1 页 / 24 题 3 页 / 50 题 5 页，每页墨迹 ≥2.5%）；
  `normal` = 原来的宽松版面（2cm 边距 / 1.15 行距 / 题间 6pt）。**不动**：字号、公式与图片尺寸、
  选项 4 列网格、题框 `cantSplit`、页脚文案、保真闸门。末页若几乎空白（墨迹 <2%），
  `render_paper` 会自动再压一档重排一次（`health.balance` 记录两次尝试）。
- `nos`（可选）：**精确题集**，给了就按这个顺序出卷、不再随机抽（训练卷用；题量与 mix 必须一致，或 mix 由 nos 推导）。
- `exclude_nos`（可选）：拉黑名单（如"原错题不要重复出现"）。
- **题库体检（`quality_flags` 表）默认参与过滤**，三个开关都在 `requirements` 里：
  - `exclude_fig_missing`（默认 **true**）：题干写了「如图/见图/图中/下图/上图」但题面里没有图（token 无
    embed/table，fmt 也证明没有）→ 学生做不了，默认排除；放宽可多出不少题（真实库约 +1,960）。
    `quality_flags` 里出现 `fig_missing` flag 时以 flag 为准（那份判定还核对了源 docx 的 media/drawing/pict）。
  - `exclude_mismatch`（默认 **跟随 `need_answer`**）：排除 `answer_mismatch`（选择答案不是选项字母 /
    填空答案超长 / 解答答案只有一个字母）——判卷必错的题。
  - `strict`（默认 false）：额外排除 `short_stem` / `heavy_layout` / `dup_text`（残句、排版过重、题干重复）。
  - `no_stem` / `stem_only_placeholder` / `analysis_left` 属**硬排除**（空白题干、教师版解析混入），
    没有开关，任何参数下都不入选。
- 体检表不存在时（旧库/没跑 `quality.scan`）自动降级：只按 scope/requirements 过滤，不报错。
- `picker.pool_report(conn, params)` 返回 `{"available": {题型: 可用题量}, "excluded": {print_blocking/mismatch/strict/fig_missing 各挡掉几题}, "quality_table": bool}`；
  `build()` 把同一份 `excluded` 写进 `manifest.stats.excluded`，UI 用它显示「已跳过 N 道不适合打印的题」。
  题量不足时 `InsufficientQuestions.relax` 给出「放宽哪个开关还能多出几题」，异常文本里也带这句话。
- **卷面不留作答空白**（用户已确认"只在答题卡作答"）：`space` 默认全 0，题目框高度按内容自适应，
  不要把解答题框撑成大空白；作答区只在答题卡上。
- `space` 仅在特殊场合（老师想要"卷面草稿区"）才调大，UI 默认不暴露。
- `scope.chapters` 与 `scope.kps` 任一为空表示不限制；`kps` 支持 `x.0`（章级）
- **学科过滤（t11）**：`scope.subject_pk`（单选，组卷页下拉默认必选一个）与 `scope.subjects`（多选/接口）
  取并集，SQL 上是 `AND subject_pk IN (...)`，与 chapters/kps 是 **AND** 关系；两个都空 = 不限（老接口兼容）。
  未知学科、或章节/知识点不属于所选学科 → `ScopeError`；`nos` 精确题集里混进别的学科 → `SelectError`。
  组卷页的「可出 N 题」与章节选择器都按同一个学科算（避免 7 科混库时跨学科混卷）。
- `need_answer=true` 时只从 `answer_raw<>''` 的题里选（判卷需要）
- `seed` 固定 → 同一参数同一份卷（可复现）；换 seed 换题
- 返回：`pid` 与完整 manifest（见 §3）

## 3. manifest（`papers.manifest`，JSON；**所有模块的中间语言**）

```json
{
  "pid": "P20261031-001",
  "created_at": "2026-10-31T14:00:00+08:00",
  "title": "七年级数学 · 训练卷 A",
  "params": { "...": "原样回填组卷参数" },
  "page": {"w_pt": 595.3, "h_pt": 841.9, "dpi": 300, "w_px": 2480, "h_px": 3508},
  "items": [
    {
      "idx": 1, "no": "00001", "qtype": "选择", "kp_id": "3.1", "family_key": null,
      "stem_tokens": "见 §4",
      "answer_key": [{"blank": 1, "value": "A", "norm": "A"}],
      "options": ["A", "B", "C", "D"],
      "space_paras": 0
    }
  ],
  "paper": {"pages": 4, "dpi": 300, "w_px": 2480, "h_px": 3508},
  "card": {
    "pages": [
      {
        "page": 1,
        "marks": {"aruco": [{"id": 0, "x": 150, "y": 150, "size": 120}, "... 4 个"],},
        "qr": {"x": 2000, "y": 150, "size": 354, "payload": {"pid": "P20261031-001", "p": 1}},
        "items": [
          {
            "idx": 1, "no": "00001", "kind": "omr",
            "id_box": {"x": 200, "y": 700, "cell": 24, "bits": [1,0,1,1, 0,1,0,0, 1,0,0,1, 0,1,1,0]},
            "cells": [
              {"opt": "A", "x": 420, "y": 900, "w": 70, "h": 70},
              {"opt": "B", "x": 590, "y": 900, "w": 70, "h": 70},
              {"opt": "C", "x": 760, "y": 900, "w": 70, "h": 70},
              {"opt": "D", "x": 930, "y": 900, "w": 70, "h": 70}
            ]
          },
          {
            "idx": 11, "no": "00010", "kind": "fill",
            "boxes": [{"blank": 1, "x": 400, "y": 1200, "w": 700, "h": 120}]
          },
          {
            "idx": 21, "no": "00008", "kind": "solution",
            "process_box": {"x": 300, "y": 1800, "w": 1900, "h": 600},
            "answer_boxes": [{"blank": 1, "x": 300, "y": 2450, "w": 800, "h": 120},
                             {"blank": 2, "x": 1200, "y": 2450, "w": 800, "h": 120}]
          }
        ]
      }
    ]
  }
}
```
坐标统一用 **300dpi 规范像素**（A4 = 2480×3508），原点左上。判卷时照片先被标定复原成同尺寸规范图，再按这些坐标取数。

## 4. 模块 API（必须实现这些函数签名）

```python
# picker.py  (paper-engineer)   ← 原名 select.py，与 stdlib `select` 重名会污染 asyncio/selectors，已改名
def build(params: dict, conn) -> dict:      # 返回 manifest（含 items，不含 card）
def save(conn, manifest: dict, docx_path: str, pdf_path: str) -> None

# paper.py   (paper-engineer)
def render_paper(manifest: dict, out_dir: str, *, name="paper", dpi=300, deep_check=False) -> dict
#   -> {"docx": path, "pdf": path, "pages": int, "png": [paths], "health": {...}}
#   health 至少含：ink_ratio 每页、formula_count（应等于 manifest 里 math 数）、media_count，
#   硬闸门（不通过抛 FidelityError，不是 warning）：
#     dangling=0（r:id/r:embed/r:link 全部能在 word/_rels/document.xml.rels 找到）
#     media_missing=[]（原始 docx 的媒体 sha1 全部进了卷面）、oMath/object/drawing/pict 数与题数一致
#     deep_check=True 时用 Word COM 真打开一次，打不开 → FidelityError(code="word_cannot_open")
#   出卷后把卷面页数写回 manifest["paper"] = {"pages", "dpi", "w_px", "h_px", "calibration"}
#   （card.py 据此接页码；pages 只数内容页，不含校准页）
#   硬闸门：PDF 每页必须是 A4（595.28×841.89pt ±0.5），否则 FidelityError(code="pdf_not_a4")；
#   card.build_card 同样校验并把 page_size_pt 写进返回的 health
#   页码口径：卷面与答题卡**各自编号**（卷面页脚「卷面 · 第 N 页 / 共 A 页」，
#   答题卡页脚「答题卡 · 第 M 页 / 共 B 页 ·（卷面续页 P{A+M}）」），合并件不盖统一页脚。

# render.py  (paper-engineer)
def docx_to_pdf(docx_path: str, pdf_path: str) -> tuple[int, float]   # (pages, seconds)
def pdf_to_png(pdf_path: str, out_dir: str, dpi: int = 300) -> list[str]

# card.py    (grading-engineer)
def build_card(manifest: dict, out_dir: str) -> dict
#   -> {"pdf": path, "png": [...], "card": { ...见 §3 card 字段... }}
#   同时把 card 字段合并进 manifest 并返回；页码从 paper 最后一页 +1 续起
#
# 已实现的辅助函数（判卷端直接复用，别重复写）：
#   card.read_qr(image_gray, qr_box) -> (payload_str, ok)   # 内部 Aruco 检测器优先 + 放大重试
#   card.iter_answer_cells(card) -> [{page,label,pid,idx,no,kind,blank,box}]
#   card.crop_cell(image_gray, box, inset=6) -> crop       # 框内缩 6px，格内是纯白
#
# ⚠ 实测坑：cv2.QRCodeDetector 对 {"pid","p"} 这种短 payload 会返回空串（p=1 时尤其明显），
#   必须用 cv2.QRCodeDetectorAruco（card.read_qr 已封装）。grade.py 一律走 card.read_qr。

# grade.py   (grading-engineer)
def grade_image(conn, pid: str, image_path: str, ocr=None) -> dict
#   -> {"sid", "warp_ok", "qr_ok", "score", "full",
#       "items": [{"idx","no","qtype","picked","expected","verdict","confidence","crop_path"}]}
#   verdict ∈ {"correct","wrong","unsure","pending","ungraded"}；unsure 必须给 crop_path 供人工确认
#   同步阶段只判选择题；填空/解答 verdict="pending"，crop 落在 app/out/<pid>/sub_<sid>/

def fill_pending(conn, sid: str, recognizer=None) -> dict
#   异步阶段：把 pending 的裁切图拼一张长图 → OCR(read_batch) → 归一化判分 → 更新 detail_json 与分数
#   返回 {"filled": n, "correct": n, "wrong": n, "left_manual": n, "backend": "..."}

# ocr.py     (grading-engineer)
class Recognizer:  # 后端可插拔：manual / paddle_vl / vlm
    def read(self, image_path: str, kind: str) -> dict
    #   -> {"text": str, "confidence": float, "backend": str}
    def read_batch(self, items: list[tuple[str, str]]) -> dict
    #   items = [(key, crop_path), ...] -> {key: {"text","confidence","backend"}}
    #   一次拼图 + 一次云端 job（见下），失败整批降级 manual（text=""），绝不抛异常

# normalize.py (data-engineer)
def normalize_answer(s: str, qtype: str) -> str
def is_equivalent(student: str, expected: str, qtype: str) -> tuple[bool, str]  # (verdict, reason)

# train.py   (data-engineer)
def plan(conn, wrong_nos: list[str], n_per: int = 3) -> dict   # 返回组卷参数（可直接给 picker.build）

# server.py  (ui-engineer)
#   GET  /                    组卷表单
#   POST /papers              建卷（调 picker→paper→card）返回 pid
#   GET  /papers/{pid}        预览（卷面 PNG + 答题卡 PNG）
#   GET  /papers/{pid}/pdf    下载打印
#   POST /submissions         上传图片判卷（multipart：pid + file）
#   GET  /submissions/{sid}   判分结果 + 逐题（含待确认队列）
#   POST /submissions/{sid}/confirm   人工确认/改判
#   POST /train               从错题生成训练卷
#   GET  /papers/{pid}/print.pdf   ★ 卷面 + 答题卡合并成一个 PDF（按此顺序），一次打印
```

> 合并规则：`paper.pdf` 的页在前、`card.pdf` 的页在后；用 pymupdf 合并（`fitz.open()` +
> `insert_pdf()`）。合并后页数 = 卷面页数 + 卡页数，UI 要显示"共 N 页（卷面 a 页 + 答题卡 b 页）"。
> 判卷只认答题卡页，靠每页的 ArUco + QR 定位，不依赖它在合并 PDF 里的绝对页号。

## 4.1 判卷两阶段 + PaddleOCR-VL 接入（已实测，按此实现）

PaddleOCR-VL 实测：单次 job 约 **5~35s**（并发会排队），而 OMR 是毫秒级。所以判卷拆两阶段：

1. **同步阶段**（<2s）：标定 + QR + OMR（选择题）+ 填空/解答标 `pending` → 立刻回结果页；
2. **异步阶段**：后台把本卷所有非选择作答格拼成 1 张长图（每格左上角印 `[编号]`，
   编号 = `f"{idx}-{blank}"`），提交**一次** OCR job，回填文本 → 归一化判分 → 更新结果。

UI 要求：结果页先显示选择题得分 + "识别中（约 30 秒）"，刷新/轮询后填出填空与解答判定；
超时或失败一律留在"待人工确认"，不得卡死页面。

**调用链路（已实测跑通）**

```
POST https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
  headers: Authorization: "bearer <TOKEN>"
  multipart: file=<图片>, model=PaddleOCR-VL-1.6,
             optionalPayload={"useDocOrientationClassify":false,"useDocUnwarping":false,"useChartRecognition":false}
  -> {"data":{"jobId":"..."}}
GET  {同 URL}/{jobId}   -> data.state: pending|running|done|failed
  done -> data.resultUrl.jsonUrl（jsonl，每行一个 result）
jsonl -> result.layoutParsingResults[0]
          .markdown.text                     # 人类可读，公式会包 $$...$$
          .prunedResult.parsing_res_list[]   # ★ 关键：每块有 block_bbox=[x1,y1,x2,y2] + block_content
```

**按坐标对齐**：拼图时记录每格 y 区间，`parsing_res_list` 的 `block_bbox` 中心 y 落在哪格就归哪格；
`block_content` 清理：去掉 `$$`、`\begin{...}`/`\end{...}`、`\\`、多余花括号、`[编号]` 残留、首尾空白；
`\left[13\right]` 这类 LaTeX 包装要还原成 `13`。

**配置** `app/config.json`（真实 Token 只在这里；`config.example.json` 留空）：
```json
{"ocr": {"backend": "paddle_vl",
         "paddle_vl": {"token": "…", "model": "PaddleOCR-VL-1.6",
                       "job_url": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"},
         "timeout_s": 120, "poll_s": 2}}
```
环境变量优先：`OCR_BACKEND` / `OCR_TOKEN` / `OCR_MODEL` / `OCR_JOB_URL`。

## 5. 工程纪律

1. **测试先行**：每个模块先写 `tests/test_<module>.py`（unittest），跑红 → 实现 → 跑绿；
   提交前必须 `python -m unittest discover -s app/tests` 全绿。
2. 只用 stdlib + 已装依赖；要新依赖先在本文件登记。
3. 所有中文输出用 `$env:PYTHONIOENCODING='utf-8'`，文件 UTF-8 无 BOM。
4. **绝不修改/移动/删除 `E:\OCR\*.docx` 与 `E:\OCR\dedup`、`E:\OCR\packages`**；产物只写 `app/data`、`app/out`。
5. 每个任务结束在任务输出里写：改了哪些文件、跑了什么命令、测试结果、遗留问题。
6. 写文件范围严格按任务里 `write_scopes`，跨范围改动先在团队里说。
7. **并发期假红**：别人正在写某个模块时跑全量，会出现"导入到半成品"的假红。**收尾判定必须等所有改动落盘后再跑一次**，并在报告里写明跑的时刻与 exit code；"与我无关"不是结论，给出"当前失败的用例 + 归属"才算。
8. **交付前必须过 `app/tools/_t34_delivery_audit.py`**（只读 3 秒）：缺失=0、文档不一致=0；多余项按 §13 清单清理。
