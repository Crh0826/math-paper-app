# -*- coding: utf-8 -*-
"""t23 · 近重复检测（**仅供分析，不参与组卷**：结论是裁剪后真重复=0，别拿它当组卷排除条件）。

结论（2026-10-31，12,280 题裁剪后语料）：
  - 内容门控（题干 Jaccard≥0.85 **且** 公式/图片内容一致）下：**真重复 0 组**；
  - 只按题干骨架比（text_only）会并出 98 组 / 250 题，但组内公式都不同 —— 是不同题，误并；
  - 因此 `dup_groups` 留空，`same_group/groups_of` 只作分析用；组卷/训练不要据此排除题目。

MinHash 近似指纹 + LSH 分带 + 并查集分组。

为什么需要它：`quality.dup_text` 只能抓「规范化后完全相同」的题；实际题库里还有
「改了数字」「换了标点/空格」「带【例N】标签」这类**近重复**——同一道题重复入库、
或者变式题被拆散。组卷和错题训练都要求同一份卷子里不出现两道近重复题。

做法（纯 stdlib，无第三方）：
  1) `normalize_stem()`：全角转半角(NFKC) → 去占位符 ⟨式⟩/⟨图⟩ → 去【分析】【例3】这类标签
     → 去空白与标点 → 取前 120 字；
  2) 字符 5-gram → 64 个 hash 的 MinHash 签名（`h_i(x) = (x*A_i + B_i) mod 2^64`，
     系数由固定种子 blake2b 派生，跨进程稳定）；
  3) 8 条带 × 8 行分带，同带同桶的题才两两比，比的是**精确 Jaccard**（不是签名估计），
     ≥ 阈值(0.85) 就连边；
  4) 并查集聚合 → 每个成员写一行 `dup_groups(group_id, no, size, rep_no)`。

对外：
    dedupe.scan(conn)                       # 全库算 + 写表，返回统计
    dedupe.same_group(conn, a, b)           # 两道题是否同组
    dedupe.groups_of(conn, nos)             # 给定题号命中的组 → 全组成员
    dedupe.are_similar(conn, a, b)          # 现算两道题的 Jaccard（不依赖已扫表）

    python dedupe.py --report data/dedupe_report.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from array import array
from collections import Counter, defaultdict
from functools import lru_cache

__all__ = ["scan", "same_group", "groups_of", "are_similar", "normalize_stem",
           "shingle_hashes", "signature", "signature_jaccard", "jaccard",
           "write_report", "DEFAULT_REPORT", "STEM_KEEP", "SHINGLE", "NUM_HASH",
           "THRESHOLD"]

MASK = (1 << 64) - 1
STEM_KEEP = 120          # 只看规范化题干的前 120 字（长题后半段是选项/小问，噪声大）
SHINGLE = 5              # 字符 5-gram
NUM_HASH = 64            # MinHash 签名长度
BAND_ROWS = 8            # 每条带的签名行数（64/8 = 8 条带）
THRESHOLD = 0.85         # 判定近重复的 Jaccard 阈值
_SEED = b"dedupe-v1"

DEFAULT_REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "dedupe_report.json")

_PLACEHOLDER = re.compile(r"[⟨<][式图表][⟩>]")
_LABEL = re.compile(r"【[^】]{0,16}】")                  # 【例3】【变式1-2】【分析】…
_PUNCT = re.compile(r"[\s\u00a0\u3000-\u303f\uff01-\uff65!-/:-@\[-`{-~]")
_MATH_T = re.compile(r"<m:t[^>]*>([^<]*)</m:t>")


# ---------------------------------------------------------------- 指纹

def content_key(tokens_json) -> tuple:
    """公式/图/表「内容指纹」：题干骨架相同时用它区分不同的题。

    text_plain 里公式被压成 ⟨式⟩，所以「计算：(1)⟨式⟩」这种骨架会撞在一起；
    真正的区分信息在公式文本（OMML 的 m:t）和图片对象上：
      - math token → 取出 m:t 文本（公式内容）
      - embed token → 记数量与显示尺寸（OLE 公式是图片，只能按尺寸弱区分）
      - table token → 记数量
    """
    try:
        toks = json.loads(tokens_json or "[]")
    except (TypeError, ValueError):
        return ((), (), 0)
    maths, media, tables = [], [], 0
    for t in toks if isinstance(toks, list) else []:
        if not isinstance(t, dict):
            continue
        kind = t.get("kind")
        if kind == "math":
            maths.append("".join(_MATH_T.findall(t.get("xml") or "")))
        elif kind == "embed":
            media.append((round(float(t.get("width_pt") or 0), 1),
                          round(float(t.get("height_pt") or 0), 1),
                          int(t.get("n_media") or 0)))
        elif kind == "table":
            tables += 1
    return (tuple(maths), tuple(media), tables)


#: docx 里媒体字节指纹的缓存（只对"图片公式"题按需读取，读不到就返回 None 退回按尺寸比）
_MEDIA_CACHE = {}
_DOCX_ROOT = os.environ.get("OCR_DOCX_ROOT", r"E:\OCR")


def media_fingerprint(no):
    """题号 → 该题 docx 内嵌媒体字节的 sha1 元组（None 表示读不到）。

    OLE/MathType 公式在 docx 里是图片，光比尺寸会把「尺寸相同、公式不同」的两题误并；
    这里把真实媒体字节哈希一下，只有图片完全相同才算同一道题。
    """
    if no in _MEDIA_CACHE:
        return _MEDIA_CACHE[no]
    fp = None
    try:
        from docx_rich import extract_tokens
        path = os.path.join(_DOCX_ROOT, "%s.docx" % no)
        blobs = []
        for t in extract_tokens(path):
            if t.kind == "embed":
                for _rid, _name, data in t.media:
                    blobs.append(hashlib.sha1(data).hexdigest())
        fp = tuple(blobs)
    except Exception:                     # noqa: BLE001 - 缺 docx / 解析失败 → 退回按尺寸
        fp = None
    _MEDIA_CACHE[no] = fp
    return fp

def normalize_stem(text) -> str:
    """规范化题干：去占位符/标签/空白/标点，全角转半角，截前 120 字。"""
    t = unicodedata.normalize("NFKC", "" if text is None else str(text))
    t = _PLACEHOLDER.sub("", t)
    t = _LABEL.sub("", t)
    t = _PUNCT.sub("", t)
    return t[:STEM_KEEP]


def shingle_hashes(stem: str, k: int = SHINGLE) -> list:
    """字符 k-gram 的 64 位滚动哈希（去重后返回，顺序稳定）。"""
    n = len(stem) - k + 1
    if n <= 0:
        return []
    out = []
    mask = MASK
    for i in range(n):
        h = 0
        for ch in stem[i:i + k]:
            h = (h * 131 + ord(ch)) & mask
        out.append(h)
    return sorted(set(out))


@lru_cache(maxsize=8)
def _coeffs(num_hash: int = NUM_HASH):
    """64 组 (A, B)：由固定种子派生，跨进程/跨机器稳定。"""
    a_list, b_list = [], []
    for i in range(num_hash):
        d = hashlib.blake2b(_SEED + b"|%d" % i, digest_size=16).digest()
        a_list.append(int.from_bytes(d[:8], "big") | 1)      # 奇数，避免退化
        b_list.append(int.from_bytes(d[8:], "big"))
    return a_list, b_list


def signature(grams, num_hash: int = NUM_HASH) -> tuple:
    """MinHash 签名：对每个 hash 函数取所有 gram 的最小值。"""
    a_list, b_list = _coeffs(num_hash)
    mins = [MASK] * num_hash
    for x in grams:
        mins = [m if m < v else v
                for m, v in zip(mins, ((x * a + b) & MASK for a, b in zip(a_list, b_list)))]
    return tuple(mins)


def signature_jaccard(sig_a, sig_b) -> float:
    """签名估计的 Jaccard（对位相等比例）。"""
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return 0.0
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)


def jaccard(grams_a, grams_b) -> float:
    """精确 Jaccard（对 5-gram 集合）。"""
    a, b = set(grams_a), set(grams_b)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


# ---------------------------------------------------------------- 并查集

def _find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _union(parent, i, j):
    ri, rj = _find(parent, i), _find(parent, j)
    if ri != rj:
        parent[rj] = ri
        return True
    return False


# ---------------------------------------------------------------- 全库扫描

def scan(conn, *, threshold: float = THRESHOLD, num_hash: int = NUM_HASH,
         band_rows: int = BAND_ROWS, limit=None, write: bool = True,
         content_gate: bool = True, media_fp=None) -> dict:
    """全库（或前 limit 题）算近重复，写 dup_groups，返回统计。

    :param content_gate: True（默认）时还要求「公式/图/表内容」一致才合并——
        题干骨架相同但公式不同的题（如「计算：(1)⟨式⟩」）不是重复题，不能合并。
        关掉它只能按题干骨架分组（会大量误并，仅用于对比统计）。
    :param media_fp: 图片公式的内容指纹函数（题号→hash 元组），缺省读 docx 里的媒体字节；
        测试可注入假函数。
    """
    media_fp = media_fp or media_fingerprint
    t0 = time.time()
    if num_hash % band_rows:
        raise ValueError("num_hash 必须能被 band_rows 整除：%d / %d"
                         % (num_hash, band_rows))
    n_bands = num_hash // band_rows

    sql = "SELECT no, text_plain, tokens_json FROM questions ORDER BY no"
    args = []
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    rows = conn.execute(sql, args).fetchall()

    nos, grams_list, raw_texts, ckeys = [], [], [], []
    buckets = defaultdict(list)
    skipped = 0
    for row in rows:
        no, text = row[0], row[1]
        stem = normalize_stem(text)
        if len(stem) < SHINGLE:
            skipped += 1
            continue
        grams = shingle_hashes(stem)
        idx = len(nos)
        nos.append(no)
        grams_list.append(array("Q", grams))
        raw_texts.append(re.sub(r"\s+", " ", str(text or "")).strip())
        ckeys.append(content_key(row[2]))
        sig = signature(grams, num_hash)
        for b in range(n_bands):
            buckets[(b, sig[b * band_rows:(b + 1) * band_rows])].append(idx)

    # LSH 分带 → 候选对（同桶两两）
    candidates = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            mi = members[i]
            for j in range(i + 1, len(members)):
                candidates.add((mi, members[j]) if mi < members[j] else (members[j], mi))

    # 候选对用**精确 Jaccard** 复核（签名只负责召回）；再过内容门控
    parent = list(range(len(nos)))
    parent_text = list(range(len(nos)))          # 只按题干骨架连边（对比用）
    linked = 0
    text_only = 0
    media_checked = 0
    media_rejected = 0
    for i, j in candidates:
        if jaccard(grams_list[i], grams_list[j]) < threshold:
            continue
        text_only += 1
        _union(parent_text, i, j)
        if content_gate:
            if ckeys[i] != ckeys[j]:
                continue
            # 没有公式文本可比、只有图片对象（OLE/MathType）时，再比真实媒体字节
            if not ckeys[i][0] and ckeys[i][1]:
                media_checked += 1
                fa, fb = media_fp(nos[i]), media_fp(nos[j])
                if fa and fb and fa != fb:
                    media_rejected += 1
                    continue
        if _union(parent, i, j):
            linked += 1

    def _count_groups(par):
        buckets_g = defaultdict(int)
        for idx in range(len(nos)):
            buckets_g[_find(par, idx)] += 1
        sizes = [n for n in buckets_g.values() if n >= 2]
        return len(sizes), sum(sizes)

    text_groups, text_affected = _count_groups(parent_text)

    groups = defaultdict(list)
    for idx in range(len(nos)):
        groups[_find(parent, idx)].append(idx)
    multi = [members for members in groups.values() if len(members) >= 2]
    multi.sort(key=lambda members: nos[min(members)])      # group_id 按最小题号，重扫稳定

    result_groups = []
    rows_out = []
    for gid, members in enumerate(multi, 1):
        members = sorted(members, key=lambda idx: nos[idx])
        # 代表题：与组内其他题平均相似度最高（并列取题号小的）
        best_idx, best_score = None, -1.0
        for idx in members:
            score = 0.0
            for other in members:
                if other != idx:
                    score += jaccard(grams_list[idx], grams_list[other])
            if score > best_score:
                best_idx, best_score = idx, score
        rep_no = nos[best_idx]
        for idx in members:
            rows_out.append((gid, nos[idx], len(members), rep_no))
        result_groups.append({
            "group_id": gid,
            "size": len(members),
            "rep_no": rep_no,
            "nos": [nos[idx] for idx in members],
        })

    if write:
        conn.execute("DELETE FROM dup_groups")
        if limit is None:
            conn.executemany("INSERT OR REPLACE INTO dup_groups(group_id, no, size, rep_no)"
                             " VALUES(?,?,?,?)", rows_out)
        elif rows_out:
            conn.executemany("INSERT OR REPLACE INTO dup_groups(group_id, no, size, rep_no)"
                             " VALUES(?,?,?,?)", rows_out)
        conn.commit()

    affected = sum(g["size"] for g in result_groups)
    decision = {
        "true_duplicates": len(result_groups),
        "true_duplicate_questions": affected,
        "text_skeleton_only_groups": text_groups,
        "text_skeleton_only_questions": text_affected,
        "usage": "仅供分析：裁剪后真重复=0；text_only 的 98 组是同一骨架但公式不同的不同题，"
                 "不得作为组卷/训练排除条件。",
    }
    sizes = Counter(g["size"] for g in result_groups)
    top = sorted(result_groups, key=lambda g: (-g["size"], g["group_id"]))[:10]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scanned": len(rows),
        "skipped_short": skipped,
        "elapsed_s": round(time.time() - t0, 2),
        "threshold": threshold,
        "num_hash": num_hash,
        "band_rows": band_rows,
        "n_bands": n_bands,
        "candidate_pairs": len(candidates),
        "linked_pairs": linked,
        "text_only_pairs": text_only,
        "content_gate": bool(content_gate),
        "media_checked_pairs": media_checked,
        "media_rejected_pairs": media_rejected,
        "text_only_groups": text_groups,
        "text_only_affected": text_affected,
        "groups": len(result_groups),
        "affected": affected,
        "largest_group": top[0]["size"] if top else 0,
        "sizes_hist": {str(k): v for k, v in sorted(sizes.items())},
        "decision": decision,
        "top_groups": [{
            "group_id": g["group_id"],
            "size": g["size"],
            "rep_no": g["rep_no"],
            "nos": g["nos"][:3],
            "stems": [raw_texts[nos.index(no)][:40] for no in g["nos"][:3]],
        } for g in top],
    }


# ---------------------------------------------------------------- 查询 API

def same_group(conn, no_a, no_b) -> bool:
    """两道题是否在同一近重复组（要求已 scan 过；没分组返回 False）。"""
    a, b = str(no_a), str(no_b)
    if a == b:
        return False
    try:
        rows = conn.execute("SELECT no, group_id FROM dup_groups WHERE no IN (?,?)",
                            (a, b)).fetchall()
    except Exception:                     # noqa: BLE001 - 老库没表
        return False
    gids = {r[0]: r[1] for r in rows}
    if a not in gids or b not in gids:
        return False
    return gids[a] == gids[b]


def groups_of(conn, nos) -> dict:
    """给定题号命中的组：{group_id: [该组全部题号]}（没分组的题不出现）。"""
    wanted = [str(n) for n in (nos or [])]
    if not wanted:
        return {}
    out = {}
    try:
        marks = ",".join("?" * len(wanted))
        gids = {r[0] for r in conn.execute(
            "SELECT DISTINCT group_id FROM dup_groups WHERE no IN (%s)" % marks, wanted)}
        for gid in gids:
            out[gid] = [r[0] for r in conn.execute(
                "SELECT no FROM dup_groups WHERE group_id=? ORDER BY no", (gid,))]
    except Exception:                     # noqa: BLE001 - 老库没表
        return {}
    return dict(sorted(out.items()))


def are_similar(conn, no_a, no_b, *, threshold: float = THRESHOLD,
                content_gate: bool = True, media_fp=None):
    """现算两道题是否近重复（不依赖已扫表）：返回 (是否近重复, 题干相似度)。

    与 scan() 的口径一致：题干 Jaccard 达标 **且** 公式/图片内容一致才算重复。
    """
    rows = conn.execute("SELECT no, text_plain, tokens_json FROM questions WHERE no IN (?,?)",
                        (str(no_a), str(no_b))).fetchall()
    info = {r[0]: (r[1], r[2]) for r in rows}
    if len(info) < 2:
        return False, 0.0
    ta, tb = info[str(no_a)], info[str(no_b)]
    sim = jaccard(shingle_hashes(normalize_stem(ta[0])),
                  shingle_hashes(normalize_stem(tb[0])))
    if sim < threshold:
        return False, sim
    if content_gate:
        ca, cb = content_key(ta[1]), content_key(tb[1])
        if ca != cb:
            return False, sim
        if not ca[0] and ca[1]:
            fp = (media_fp or media_fingerprint)
            fa, fb = fp(str(no_a)), fp(str(no_b))
            if fa and fb and fa != fb:
                return False, sim
    return True, sim


# ---------------------------------------------------------------- 报告 / CLI

def write_report(result: dict, path: str = DEFAULT_REPORT) -> str:
    """把 scan() 结果写成 dedupe_report.json（组数/受影响题数/最大 10 组样例）。"""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return path


def _main(argv=None):
    import argparse
    import sqlite3
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="近重复检测（MinHash）")
    ap.add_argument("--db", default=os.path.join(here, "data", "app.db"))
    ap.add_argument("--report", nargs="?", const=DEFAULT_REPORT, default=DEFAULT_REPORT)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-write", action="store_true", help="只统计，不写 dup_groups")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        res = scan(conn, threshold=args.threshold, limit=args.limit or None,
                   write=not args.no_write)
    finally:
        conn.close()
    path = write_report(res, args.report)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    sys.stderr.write("report -> %s\n" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
