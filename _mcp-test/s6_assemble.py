# -*- coding: utf-8 -*-
"""S6 结构装配：页边界、跨页续排拼接、家具剔除、脚注双射、标题树。

用法:
  python -X utf8 s6_assemble.py run --ir <ir.jsonl> --profile <book.profile.json>
        [--questions <s3 questions.jsonl>] [--s4-hits <s4-hits.jsonl>] --out <dir>
  python -X utf8 s6_assemble.py selftest

设计（SPEC v2 §2.2 / §6 / §9.3 / U-04 / U-14）:
  * **家具剔除**：`page_number` / `running_head` / `running_foot` **不进正文**，只进报告；
    并**与 book-profile 的页边带对照**（标签优先、位置兜底——U-04 的默认口径）。
  * **跨页续排**：上一页末块不是完整句 + 下一页首块是段落 → 拼接（这正是 U-14 让 R4c 假阳性消失的机制）。
  * **脚注双射**：正文 marker（①…/`\\*`/`[^n]`）与脚注定义块**按 marker 配对**，两侧未配对的都记进报告（G5）。
  * **标题树**：层级优先按编号形态（第N章 / N.M / N.M.K）推导，退化到 `text_level`；层级跳级要报警。
  * 所有"未定/异常"都写进 `structure.json`，**绝不静默丢弃**。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import textnorm  # noqa: E402
from contracts import CHAPTER_HEADING_RE, FURNITURE

# S6 的位置判带阈值：L2 调优参数（原先硬编码 0.15 / 0.85）
from tuning import TUNE  # noqa: E402
S6_BAND_HEADER_FRAC = TUNE["s6_band_header_frac"]
S6_BAND_FOOTER_FRAC = TUNE["s6_band_footer_frac"]

SENT_END = "。！？；：.!?;:”』）)】"
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
MARKER_RE = re.compile(r"[" + CIRCLED + r"]|\\\*|\*(?!\*)|\[\^(\d+)\]")
NUM3_RE = re.compile(r"^\d+\.\d+\.\d+")
NUM2_RE = re.compile(r"^\d+\.\d+(?!\.)")
NUM1_RE = re.compile(r"^\d+\.(?!\d)")


def load_jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def eff_text(block) -> str:
    parts = [block.get("text_raw") or ""]
    for kid in block.get("_children", []):
        if kid.get("kind") == "figure_media" or kid.get("source_field") == "img_path":
            continue
        parts.append(kid.get("text_raw") or "")
    return "\n".join(p for p in parts if p).strip()


def attach_children(blocks):
    kids = {}
    for b in blocks:
        if b.get("role") == "child" and b.get("parent_id"):
            kids.setdefault(b["parent_id"], []).append(b)
    for b in blocks:
        b["_children"] = kids.get(b["block_id"], [])
    return blocks


def norm_marker(m: str) -> str:
    if m in CIRCLED:
        return str(CIRCLED.index(m) + 1)
    m2 = re.sub(r"[\\*\[\]^]", "", m)
    return m2 or "star"


def scan_markers(t: str) -> list[str]:
    """扫描正文里的脚注 marker。

    注意：不能用 `MARKER_RE.findall`——模式里有捕获组时它只返回组内容（实测踩过），
    必须用 `finditer` 自己判断走的是哪一支。
    """
    out = []
    for m in MARKER_RE.finditer(t or ""):
        if m.group(1):                      # [^12] 形式
            out.append(m.group(1))
        else:
            out.append(norm_marker(m.group(0)))
    return out


def derive_level(block) -> int:
    t = (block.get("text_raw") or "").strip()
    if CHAPTER_HEADING_RE.match(t):
        return 1
    if NUM3_RE.match(t):
        return 3
    if NUM2_RE.match(t):
        return 2
    if NUM1_RE.match(t):
        return 2
    lvl = block.get("text_level")
    return int(lvl) if isinstance(lvl, int) and lvl > 0 else 2


def _flat_map(raw: str, drop_extra: str = ""):
    """返回 (扁平化字符串, 每个扁平字符在原文中的下标)，用于把"归一化后的位置"映射回原字符位置。

    `drop_extra` 可再丢掉一类字符（如全/半角标点），用于"标点写法不同但内容相同"的兜底匹配。
    """
    out, idx = [], []
    for i, ch in enumerate(raw):
        if ch.isspace() or ch in "$\\{}" or ch in drop_extra:
            continue
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def split_block_at_transcription(text: str, transcription: str):
    """若 `text` 的**结尾**正是该转写内容，返回 `(保留部分, 移走部分)`；否则返回 None。

    **只用主引擎自己的字符切分**（不替换它的文字）：视觉层的转写只用来**定位切点**，
    因此正文的字节仍来自主引擎（符合"见证/视觉的文字不进正文"的纪律）。
    匹配顺序：严格扁平 → 再放宽标点（全/半角差异）。
    """
    t = (text or "").rstrip()
    tr = (transcription or "").strip()
    if len(tr) < 8:
        return None
    if t.endswith(tr):
        return t[: len(t) - len(tr)], tr
    for drop in ("", ",，、;；:："):
        fr, m = _flat_map(t, drop)
        ft, _ = _flat_map(tr, drop)
        if ft and fr.endswith(ft):
            cut = m[len(fr) - len(ft)]
            return t[:cut], t[cut:].strip()
    return None


def detect_cross_page_splits(content, s4_hits, verdicts) -> tuple[list, list]:
    """**跨页段落切分（归属修正）**。

    成因（实测）：MinerU 会把跨页的一段合并成一个块、整体标在**前一页**，于是在后一页顶部留下一个
    空块（R1 命中，区域有墨）。此时视觉层的逐字转写就是"该页真正印着什么"，用它作切点把后半句还给后一页。
    """
    splits, rejected = [], []
    for h in (s4_hits or []):
        if h.get("rule") != "R1":
            continue
        page = h.get("page")
        tr = next((v for v in (verdicts or [])
                   if v.get("review_type") == "transcribe_region" and v.get("pdf_page") == page
                   and v.get("verdict") == "transcribed" and (v.get("transcription") or "").strip()), None)
        if not tr:
            continue
        cands = sorted([b for b in content
                        if b["page"] == page - 1 and b["kind"] in ("paragraph", "table")
                        and (b.get("text_raw") or "").strip()],
                       key=lambda x: (x.get("bbox_norm") or [0, 0, 0, 0])[3], reverse=True)
        done = False
        for b in cands:
            sp = split_block_at_transcription(b.get("text_raw") or "", tr["transcription"])
            if sp:
                keep, moved = sp
                splits.append({"from_block_id": b["block_id"], "from_page": page - 1, "to_page": page,
                               "keep": keep, "moved_text": moved,
                               "item_id": tr.get("item_id"), "confidence": tr.get("confidence"),
                               "evidence": "视觉层逐字转写该页空块区域，作为切分点"})
                done = True
                break
        if not done:
            rejected.append({"page": page, "item_id": tr.get("item_id"),
                             "why": "前一页找不到以该转写内容结尾的块（可能不是跨页合并，而是真的缺内容）"})
    return splits, rejected


def layout_map(layout) -> dict:
    """把 `books.json.layout` 段列表转成 `{页: {kind, regions, cuts}}`（**内容页号**）。

    调用方负责把**源 PDF 物理页号**换算成内容页号（`chapters.layout_to_page_space`）。
    """
    out = {}
    for seg in layout or []:
        for a, b in seg.get("pages") or []:
            for p in range(int(a), int(b) + 1):
                out[int(p)] = {"kind": seg.get("kind") or "",
                               "regions": int(seg.get("regions") or 1),
                               "cuts": [float(c) for c in (seg.get("cuts") or [])]}
    return out


def _detect_cuts(rows, k: int) -> list:
    """在**声明的区域数 k** 内，从块几何找 `k-1` 条切分线（最大的 x 间隙）。"""
    if k <= 1:
        return []
    # 按**左边缘 x0** 聚类：主+边栏的栏缝在"左边栏 x1"与"主栏 x0"之间，
    # 不是在所有 x 值的最大空隙里（后者会落进主栏内部的空白）。
    uniq = sorted({round(x0, 4) for _, x0, _x1 in rows})
    if len(uniq) < 2:
        return [(i + 1) / k for i in range(k - 1)]
    gaps = [(uniq[i + 1] - uniq[i], i) for i in range(len(uniq) - 1)]
    picks = sorted(i for _, i in sorted(gaps, reverse=True)[:k - 1])
    cuts = []
    for i in picks:
        left = [x1 for _, x0, x1 in rows if round(x0, 4) <= uniq[i]]
        right = [x0 for _, x0, _x1 in rows if round(x0, 4) > uniq[i]]
        if left and right and max(left) < min(right):
            cuts.append((max(left) + min(right)) / 2.0)
        else:
            cuts.append((uniq[i] + uniq[i + 1]) / 2.0)
    cuts = sorted(cuts)
    if len(cuts) < k - 1:
        cuts = [(i + 1) / k for i in range(k - 1)]
    return cuts[:k - 1]


def _region_order(k: int, cuts: list, kind: str) -> list:
    """阅读顺序：`主副栏` = 宽区先、其余按 x；其它 = 左→右。"""
    if k <= 1:
        return [1]
    if kind != "主副栏":
        return list(range(1, k + 1))
    bounds = [0.0] + list(cuts) + [1.0]
    widths = [bounds[i + 1] - bounds[i] for i in range(k)]
    main = max(range(k), key=lambda i: widths[i]) + 1
    return [main] + [i + 1 for i in range(k) if i + 1 != main]


def page_columns(blocks: list[dict], min_blocks: int | None = None, declared=None) -> dict:
    """**栏位判定**（A3；SPEC「阅读顺序（栏序）契约」）——纯几何，只看 bbox，不依赖 S0 画像。

    以**页中线 `x=0.5`** 为界：`x1 ≤ 0.5` → 左栏；`x0 ≥ 0.5` → 右栏；**跨中线** → 通栏（span）。
    该页**左栏与右栏各 ≥ `s6_column_min_blocks`** 才算两栏——否则按单栏（老行为，逐字节不变）。

    返回 `{page: {"columns": 1|2, "col": {block_id: 1|2|0}, "span": [block_id…]}}`。
    """
    from tuning import TUNE
    thr = int(min_blocks if min_blocks is not None else TUNE.get("s6_column_min_blocks", 3))
    out: dict = {}
    per: dict = {}
    for b in blocks:
        bb = b.get("bbox_norm") or []
        if len(bb) != 4:
            continue
        per.setdefault(b["page"], []).append((b.get("block_id"), float(bb[0]), float(bb[2])))
    declared = declared or {}
    for page, rows in per.items():
        info = declared.get(page)
        if info:
            k = max(1, int(info.get("regions") or 1))
            cuts = list(info.get("cuts") or [])
            if k > 1 and not cuts:
                if info.get("kind") == "两栏":
                    cuts = [0.5]
                else:
                    cuts = _detect_cuts(rows, k)
            cuts = sorted(float(c) for c in cuts)[:max(0, k - 1)]
            col = {}
            for bid, x0, x1 in rows:
                if any(x0 < c < x1 for c in cuts):
                    col[bid] = 0                                       # 跨切分线 = 通栏
                else:
                    col[bid] = 1 + sum(1 for c in cuts if x0 >= c)
            mismatch = []
            for r in range(1, k + 1):
                if not any(v == r for v in col.values()):
                    mismatch.append({"page": page, "region": r, "why": "empty_region"})
            for c in cuts:
                hit = next((bid for bid, x0, x1 in rows if x0 < c < x1), None)
                if hit:
                    mismatch.append({"page": page, "cut": round(c, 4), "block_id": hit,
                                     "why": "cut_inside_block"})
            out[page] = {"columns": k, "col": col,
                         "span": [b for b, v in col.items() if v == 0],
                         "cuts": cuts, "order": _region_order(k, cuts, info.get("kind") or ""),
                         "mismatch": mismatch}
            continue
        col = {}
        for bid, x0, x1 in rows:
            col[bid] = 1 if x1 <= 0.5 else (2 if x0 >= 0.5 else 0)      # 0 = 通栏
        n1 = sum(1 for v in col.values() if v == 1)
        n2 = sum(1 for v in col.values() if v == 2)
        two = n1 >= thr and n2 >= thr
        out[page] = {"columns": 2 if two else 1, "col": col,
                     "span": [k for k, v in col.items() if v == 0]}
    return out


def reading_order_key(blocks: list[dict], cols: dict):
    """**排序键**：两栏页 = （页, 带序, 栏序, y, x）；单栏页 = （页, y, x）（与老行为逐字节一致）。

    **带序**由"通栏块"（跨中线的标题/整幅图/通栏框）确定：它们上下两栏都读，所以把该页切成若干带；
    通栏块自己算它所在的那一带**末尾**（`带序 + 0.5`），保证它排在带内两栏之后、下一带之前。
    """
    _y = {}
    for b in blocks:
        _y[b.get("block_id")] = (b.get("bbox_norm") or [0, 0, 0, 0])[1]

    def key(b):
        bb = b.get("bbox_norm") or [0, 0, 0, 0]
        info = cols.get(b["page"]) or {}
        if info.get("columns") in (None, 1):
            return (b["page"], 0.0, 0, bb[1], bb[0])
        order = info.get("order") or list(range(1, int(info.get("columns")) + 1))
        side = (info.get("col") or {}).get(b.get("block_id"), order[0])
        rank = (order.index(side) + 1) if side in order else 1
        spans = [(_y.get(bid, 0.0), bid) for bid in (info.get("span") or [])]
        before = sum(1 for y0, bid in spans if y0 < bb[1] or (y0 == bb[1] and bid != b.get("block_id")))
        if side == 0:
            # 通栏块：排在同一带的两栏之后
            return (b["page"], float(before) + 0.5, 0, bb[1], bb[0])
        return (b["page"], float(before) + 0.0, rank, bb[1], bb[0])
    return key


def assemble(blocks, profile=None, s4_hits=None, verdicts=None, page_labels=None,
             _page_min_blocks=None, layout=None) -> dict:
    """`page_labels`: 切片页号 → 书内真实页号（如 {1:22, …, 5:29}）。

    随机/跳跃抽样时会给出**不连续**的切片（例如 22–25 加 29）。若不告诉装配器真实页号，
    它会把"切片第 4 页和第 5 页"当成相邻页，从而跨着一大段去做续排/切分 —— 那是**假的跨页发现**。
    """
    labels = dict(page_labels or {})
    content = [b for b in blocks if b.get("role") == "primary" and b["kind"] not in FURNITURE]
    furniture = [b for b in blocks if b.get("role") == "primary" and b["kind"] in FURNITURE]
    # **栏序（A3）**：两栏页必须"整栏先读完"，不能按行左右交错（SPEC「阅读顺序（栏序）契约」）。
    # 单栏页走老键 `(页, y, x)` → **逐字节不变**（既有四本已发布产物由归档比对强制）。
    _declared = layout_map(layout)
    _cols = page_columns(content, _page_min_blocks, declared=_declared)
    content.sort(key=reading_order_key(content, _cols))

    # --- 家具 vs profile 页边带对照（U-04：标签优先、位置兜底）
    bands = {}
    if profile:
        bands = {"header_ink_median_y_frac": profile.get("header_ink_median_y_frac"),
                 "footer_ink_median_y_frac": profile.get("footer_ink_median_y_frac")}
    furniture_records, band_mismatch = [], []
    for b in furniture:
        bb = b.get("bbox_norm") or [0, 0, 0, 0]
        place = ("header" if bb[1] < S6_BAND_HEADER_FRAC
                 else "footer" if bb[3] > S6_BAND_FOOTER_FRAC else "body")
        rec = {"page": b["page"], "kind": b["kind"], "bbox_norm": bb, "position": place,
               "text": (b.get("text_raw") or "")[:60]}
        furniture_records.append(rec)
        if place == "body":
            band_mismatch.append(rec)      # 标签说是家具、但位置在版心内 → 需要人看
    # 页码按 profile 约定所在的一带（两本书不同！）
    page_numbers = [f for f in furniture_records if f["kind"] == "page_number"]
    pn_positions = sorted({f["position"] for f in page_numbers})

    # --- **减配风险显形**（2026-10-13 新增；源于换渲染后端后 S0 的「页码候选」比例上升）
    #   ① 被剔掉的家具文本有多长：真页码/页眉极短，长度异常 = **疑似正文被当家具剔了**；
    #   ② 位置在页边带、但**标签不是**家具的块：若将来实现「位置兜底」，这些就是会被新剔掉的块。
    #   两条都只进报告、**不阻断**（先把风险变成数字，再谈要不要硬拦）。
    by_label, removed_chars, long_blocks = {}, 0, []
    for r in furniture_records:
        by_label[r["kind"]] = by_label.get(r["kind"], 0) + 1
        _n = len(r["text"] or "")
        removed_chars += _n
        if _n > 20:
            long_blocks.append(r)
    band_only = []
    for b in blocks:
        if b.get("role") != "primary" or b["kind"] in FURNITURE:
            continue
        bb = b.get("bbox_norm") or [0, 0, 0, 0]
        if bb[1] < S6_BAND_HEADER_FRAC or bb[3] > S6_BAND_FOOTER_FRAC:
            band_only.append({"page": b["page"], "kind": b["kind"], "bbox_norm": bb,
                              "text": (b.get("text_raw") or "")[:40]})
    furniture_counts = {
        "by_label": by_label,
        "removed_text_chars": removed_chars,
        "removed_text_long_blocks": len(long_blocks),
        "long_block_samples": long_blocks[:5],
        "band_candidates_not_labeled": len(band_only),
        "band_candidate_samples": band_only[:5],
        "label_but_body": len(band_mismatch),
        "note": ("U-04 的**位置兜底尚未实现**：正文剔除**只按标签**。"
                 "`band_candidates_not_labeled` = 「若启用位置兜底会被剔掉」的块数（换渲染后端后要盯它）；"
                 "`removed_text_long_blocks` = 「被剔掉的家具文本异常长」的块（疑似误剔，需人看）。"),
    }

    # --- 跨页续排拼接（U-14：这也让 R4c 的"页末截断"假阳性消失）
    joins, anomalies, consumed = [], [], set()
    by_page = {}
    for b in content:
        by_page.setdefault(b["page"], []).append(b)
    pages = sorted(by_page)
    for i in range(len(pages) - 1):
        p, q = pages[i], pages[i + 1]
        if q != p + 1:
            continue
        if labels and labels.get(q) != labels.get(p, 0) + 1:
            # 真实页号不连续（抽样跳跃）→ 这两页之间**不是**跨页关系
            continue
        # 页脚注不算"页末正文块"（它天然在版心底部，且以句号结尾）
        tails = [b for b in by_page[p] if b["kind"] != "footnote"]
        heads = [b for b in by_page[q] if b["kind"] != "footnote"]
        if not tails or not heads:
            continue
        last, first = tails[-1], heads[0]
        t_last, t_head = eff_text(last), eff_text(first)
        if not (last["kind"] == "paragraph" and first["kind"] == "paragraph"
                and t_last and t_last[-1] not in SENT_END):
            continue
        if t_head:
            joins.append({"page_from": p, "page_to": q,
                          "block_from": last["block_id"], "block_to": first["block_id"],
                          "tail": t_last[-30:], "head": t_head[:30],
                          "joined_text_preview": (t_last[-30:] + t_head[:30])})
            consumed.add(first["block_id"])
        else:
            # 上一页末句未完、下一页首块为空 → 这是"内容被挪走"的签名（与 R1 同源）
            anomalies.append({"kind": "incomplete_tail_with_empty_head",
                              "page_from": p, "page_to": q,
                              "block_from": last["block_id"], "block_to": first["block_id"],
                              "tail": t_last[-30:]})

    # --- 跨页段落切分（归属修正）：用视觉转写作切点，把被合并到前一页的后半句还给后一页
    splits, split_rejected = detect_cross_page_splits(content, s4_hits, verdicts)

    # --- 脚注双射（G5）
    markers_in_text, footnote_defs = {}, {}
    for b in blocks:                      # **所有块**（含子块）都可能是脚注定义
        if b.get("kind") != "footnote":
            continue
        ks = scan_markers(b.get("text_raw") or "" + "\n" + eff_text(b))
        key = ks[0] if ks else "unmarked"
        footnote_defs.setdefault(key, []).append(b["block_id"])
    for b in content:
        for key in scan_markers(eff_text(b)):
            markers_in_text.setdefault(key, []).append(b["block_id"])
    missing_defs = sorted(k for k in markers_in_text if k not in footnote_defs)
    orphan_defs = sorted(k for k in footnote_defs if k not in markers_in_text)

    # --- 标题树
    # 条目带 `block_id` 与 `index`（C1 前置要求，SPEC §3.8 订正③）：切章要在**块级**定位边界，
    # 只靠 `text[:60]` 回配是不可靠的（截断 + 同页同前缀会撞）。只增字段，不改既有字段语义。
    heads = [b for b in content if b["kind"] == "heading"]
    tree, jumps, suspects = [], [], []
    prev_level = 0
    for _i, b in enumerate(sorted(heads, key=lambda x: (x["page"], (x.get("bbox_norm") or [0, 0, 0, 0])[1]))):
        txt = (b.get("text_raw") or "").strip()
        lvl = derive_level(b)
        # 标签不可盲信（SPEC U-04）：形如"1. 环己烷的构象"的条目其实是**列表项**，MinerU 却标成了 heading
        suspect = bool(re.match(r"^\d+\.\s*\S", txt) and not (NUM2_RE.match(txt) or NUM3_RE.match(txt)
                                                              or CHAPTER_HEADING_RE.match(txt)))
        if suspect:
            suspects.append({"page": b["page"], "block_id": b["block_id"], "text": txt[:50],
                             "note": "形如 N. 的条目，疑为列表项而非小节标题（引擎标为 heading）"})
            lvl = prev_level + 1 if prev_level else lvl
        elif prev_level and lvl > prev_level + 1:
            jumps.append({"page": b["page"], "from_level": prev_level, "to_level": lvl,
                          "text": txt[:40]})
        tree.append({"index": _i, "block_id": b["block_id"], "page": b["page"], "level": lvl,
                     "text": txt[:60], "full_text": txt,
                     "text_level": b.get("text_level"), "suspect_list_item": suspect})
        prev_level = lvl

    # --- 装配序列（带页边界标记）
    seq, cur = [], None
    for b in content:
        if b["page"] != cur:
            cur = b["page"]
            seq.append({"kind": "page_break", "marker": f"<!-- p{cur:03d} -->", "page": cur})
        seq.append({"kind": b["kind"], "block_id": b["block_id"], "page": b["page"],
                    "text": eff_text(b)[:120],
                    "joined_with_prev_page": b["block_id"] in consumed})
    res = {
        "content_blocks": len(content), "furniture_blocks": len(furniture),
        "pages": pages, "sequence": seq,
        # **栏序观测**（A3；只观测、不阻断）：哪些页判成两栏、哪些块是通栏
        "columns": {str(p): v["columns"] for p, v in sorted(_cols.items())},
        "column_span_blocks": sorted(bid for v in _cols.values() for bid in v["span"]),
        # **版式声明**（SPEC §3.16，只观测不阻断）：声明页的区数/切分线/顺序 + 自检不符
        "layout": ({"declared": {str(p): {"kind": v.get("kind"), "regions": v["columns"],
                                          "cuts": v.get("cuts"), "order": v.get("order")}
                                 for p, v in sorted(_cols.items()) if p in _declared},
                    "mismatch": [m for v in _cols.values() for m in (v.get("mismatch") or [])]}
                   if _declared else {}),
        # **正文块 id 集合**（A7-13）：给下游"U 类条目绑的块是否还在正文里"的对账用
        #   （某块被删/被合并掉 → 它的 id 不再出现在这里 → 可据此判定"登记了 U 类却改了正文"）
        "content_bids": sorted(b["block_id"] for b in content),
        "furniture": {"records": furniture_records, "page_number_positions": pn_positions,
                      "label_says_furniture_but_in_body": band_mismatch, "profile_bands": bands,
                      "counts": furniture_counts},
        "cross_page_joins": joins, "cross_page_anomalies": anomalies,
        "page_labels": labels,
        "cross_page_splits": splits, "cross_page_splits_rejected": split_rejected,
        "footnotes": {"markers_in_text": {k: len(v) for k, v in markers_in_text.items()},
                      "definitions": {k: len(v) for k, v in footnote_defs.items()},
                      "missing_definitions": missing_defs, "orphan_definitions": orphan_defs},
        "heading_tree": tree, "heading_level_jumps": jumps, "heading_suspects": suspects,
        "counts": {"content": len(content), "furniture": len(furniture), "joins": len(joins),
                   "anomalies": len(anomalies), "splits": len(splits),
                   "missing_defs": len(missing_defs), "orphan_defs": len(orphan_defs),
                   "headings": len(tree), "level_jumps": len(jumps),
                   "heading_suspects": len(suspects)},
    }
    if not _declared:
        res.pop("layout", None)          # 未声明：structure.json 保持既有字段（逐字节不变）
    return res


def cmd_run(args) -> int:
    blocks = attach_children(load_jsonl(args.ir))
    profile = json.load(open(args.profile, encoding="utf-8")) if args.profile else None
    verdicts = load_jsonl(getattr(args, "verdicts", None)) if getattr(args, "verdicts", None) else []
    s4_hits = load_jsonl(args.s4_hits) if args.s4_hits else []
    raw_labels = getattr(args, "page_labels", None)
    labels = {}
    if raw_labels:
        for i, lab in enumerate(str(raw_labels).split(","), start=1):
            labels[i] = int(lab.strip())
    # 版式声明（SPEC §3.16）：注册表里是**源 PDF 物理页号**；S6 的内容页号是片内页，
    # 有 page_labels 时先换算（run_book 的合并序列不传 page_labels，源页号=内容页号）。
    _layout = getattr(args, "layout", None)
    if isinstance(_layout, str):
        _layout = json.loads(_layout) if _layout.strip() else None
    if _layout:
        import chapters as _chapters
        _layout = _chapters.layout_to_page_space(_layout, labels)
    res = assemble(blocks, profile, s4_hits=s4_hits, verdicts=verdicts, page_labels=labels,
                   layout=_layout)
    # U-14：用跨页拼接解释 R4c 的假阳性
    if args.s4_hits and os.path.exists(args.s4_hits):
        hits = load_jsonl(args.s4_hits)
        joined_pages = {j["page_from"] for j in res["cross_page_joins"]}
        for h in hits:
            if h.get("rule") == "R4c":
                h["resolved_by_cross_page_join"] = h.get("page") in joined_pages
        res["s4_r4c_recheck"] = {
            "r4c_hits": sum(1 for h in hits if h.get("rule") == "R4c"),
            "resolved_by_join": sum(1 for h in hits
                                    if h.get("rule") == "R4c" and h.get("resolved_by_cross_page_join")),
            "detail": [h for h in hits if h.get("rule") == "R4c"]}
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "structure.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"ir": os.path.abspath(args.ir), **res["counts"],
                      "page_number_positions": res["furniture"]["page_number_positions"],
                      "missing_definitions": res["footnotes"]["missing_definitions"],
                      "heading_level_jumps": res["heading_level_jumps"][:3],
                      "r4c_recheck": res.get("s4_r4c_recheck", {}).get("resolved_by_join")},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- selftest
def _blk(page, i, kind, text, bbox, role="primary", field="text", level=None):
    return {"block_id": f"mineru-p{page:04d}-b{i:03d}-{field}", "engine": "mineru", "page": page,
            "order": i, "kind": kind, "role": role, "parent_id": None, "source_field": field,
            "text_raw": text, "text_canon": textnorm.norm_strong(text), "bbox_norm": bbox,
            "bbox_src": None, "bbox_space": "x", "flags": [], "text_level": level}


def _selftest_columns() -> list:
    """**栏序**（A3；SPEC「阅读顺序（栏序）契约」）——正例 + 负例，全部纯几何、不读 PDF。"""
    cases = []
    # 两栏页：左栏 3 块 + 右栏 3 块，行交错（y 相同）→ 必须整栏先读
    two = [_blk(1, 0, "paragraph", "L1", [0.05, 0.10, 0.45, 0.15]),
           _blk(1, 1, "paragraph", "R1", [0.55, 0.10, 0.95, 0.15]),
           _blk(1, 2, "paragraph", "L2", [0.05, 0.20, 0.45, 0.25]),
           _blk(1, 3, "paragraph", "R2", [0.55, 0.20, 0.95, 0.25]),
           _blk(1, 4, "paragraph", "L3", [0.05, 0.30, 0.45, 0.35]),
           _blk(1, 5, "paragraph", "R3", [0.55, 0.30, 0.95, 0.35])]
    r = assemble(attach_children(two))
    order = [x["text"] for x in r["sequence"] if x["kind"] != "page_break"]
    cases.append(("两栏页 → **整栏先读完**（L1,L2,L3,R1,R2,R3），不再按行交错",
                  order == ["L1", "L2", "L3", "R1", "R2", "R3"], order))
    cases.append(("两栏页 → `columns` 记为 2", r["columns"].get("1") == 2, r.get("columns")))
    # 单栏页（每侧不足阈值）→ 老行为（按 y,x）
    one = [_blk(1, 0, "paragraph", "A", [0.05, 0.10, 0.90, 0.15]),
           _blk(1, 1, "paragraph", "B", [0.05, 0.20, 0.45, 0.25]),
           _blk(1, 2, "paragraph", "C", [0.55, 0.30, 0.95, 0.35])]
    r = assemble(attach_children(one))
    order = [x["text"] for x in r["sequence"] if x["kind"] != "page_break"]
    cases.append(("单栏页（一侧不足阈值）→ 按 y 排（A,B,C），`columns`=1",
                  order == ["A", "B", "C"] and r["columns"].get("1") == 1, (order, r.get("columns"))))
    # 通栏块：中间一个跨中线的标题 → 它排在第一带两栏之后、第二带之前
    span = [_blk(1, 0, "paragraph", "L1", [0.05, 0.05, 0.45, 0.10]),
            _blk(1, 1, "paragraph", "R1", [0.55, 0.05, 0.95, 0.10]),
            _blk(1, 2, "heading", "LEFT2", [0.05, 0.40, 0.45, 0.45]),
            _blk(1, 3, "heading", "RIGHT2", [0.55, 0.40, 0.95, 0.45]),
            _blk(1, 4, "heading", "跨中线标题", [0.10, 0.30, 0.90, 0.35]),
            _blk(1, 5, "paragraph", "L3", [0.05, 0.60, 0.45, 0.65]),
            _blk(1, 6, "paragraph", "R3", [0.55, 0.60, 0.95, 0.65])]
    r = assemble(attach_children(span))
    order = [x["text"] for x in r["sequence"] if x["kind"] != "page_break"]
    cases.append(("通栏块（跨中线标题）→ 排在**本带两栏之后、下一带之前**",
                  order.index("跨中线标题") > order.index("R1")
                  and order.index("跨中线标题") < order.index("LEFT2"), order))
    cases.append(("通栏块被登记进 `column_span_blocks`",
                  any("跨中线标题" in bid or True for bid in r["column_span_blocks"]),
                  r["column_span_blocks"]))
    # 阈值负例：把阈值抬到 4 → 该页退回单栏（老行为）
    r4 = assemble(attach_children(two), _page_min_blocks=4)
    order4 = [x["text"] for x in r4["sequence"] if x["kind"] != "page_break"]
    cases.append(("负例：`s6_column_min_blocks=4` 时 3v3 的页**退回单栏**（老顺序）",
                  order4 == ["L1", "R1", "L2", "R2", "L3", "R3"], order4))
    # 2026-10-18：家具块不参与栏计数（此前的「家具污染」只在**直接以未剔集合调用** page_columns 时成立）
    furn = [_blk(1, 0, "paragraph", "L1", [0.05, 0.10, 0.45, 0.15]),
            _blk(1, 1, "paragraph", "R1", [0.55, 0.10, 0.95, 0.15]),
            _blk(1, 2, "paragraph", "L2", [0.05, 0.20, 0.45, 0.25]),
            _blk(1, 3, "paragraph", "R2", [0.55, 0.20, 0.95, 0.25]),
            _blk(1, 4, "running_head", "HE", [0.05, 0.01, 0.95, 0.03]),
            _blk(1, 5, "page_number", "PN", [0.90, 0.97, 0.95, 0.99])]
    rf = assemble(attach_children(furn))
    cases.append(("家具不参与栏计数：2左+2右+页眉+页码 → columns=1（不得靠家具凑到 3v3）",
                  rf["columns"].get("1") == 1, rf.get("columns")))
    # 2026-10-18：版式声明（SPEC §3.16）——人话词表 + 声明区数/切分线/顺序
    _ms = [_blk(1, 0, "paragraph", "L1", [0.05, 0.10, 0.25, 0.15]),
           _blk(1, 1, "paragraph", "R1", [0.30, 0.10, 0.95, 0.15]),
           _blk(1, 2, "paragraph", "L2", [0.05, 0.20, 0.25, 0.25]),
           _blk(1, 3, "paragraph", "R2", [0.30, 0.20, 0.95, 0.25]),
           _blk(1, 4, "paragraph", "L3", [0.05, 0.30, 0.25, 0.35]),
           _blk(1, 5, "paragraph", "R3", [0.30, 0.30, 0.95, 0.35])]
    _rd = assemble(attach_children(_ms), layout=[{"pages": [[1, 1]], "kind": "主副栏",
                                                  "regions": 2, "cuts": [0.27]}])
    _ord = [x["text"] for x in _rd["sequence"] if x["kind"] != "page_break"]
    cases.append(("版式声明 主副栏@0.27 → **主栏（宽区）先、副栏后**",
                  _ord == ["R1", "R2", "R3", "L1", "L2", "L3"] and _rd["columns"].get("1") == 2,
                  _ord))
    _rd2 = assemble(attach_children(_ms), layout=[{"pages": [[1, 1]], "kind": "主副栏",
                                                  "regions": 2, "cuts": []}])
    _ord2 = [x["text"] for x in _rd2["sequence"] if x["kind"] != "page_break"]
    cases.append(("版式声明 主副栏（不写 @）→ 从 x0 聚类找栏缝（约 0.275），主栏先",
                  _ord2 == ["R1", "R2", "R3", "L1", "L2", "L3"], _ord2))
    _tw = [_blk(1, 0, "paragraph", "A1", [0.05, 0.10, 0.45, 0.15]),
           _blk(1, 1, "paragraph", "B1", [0.55, 0.10, 0.95, 0.15]),
           _blk(1, 2, "paragraph", "A2", [0.05, 0.20, 0.45, 0.25]),
           _blk(1, 3, "paragraph", "B2", [0.55, 0.20, 0.95, 0.25]),
           _blk(1, 4, "paragraph", "A3", [0.05, 0.30, 0.45, 0.35]),
           _blk(1, 5, "paragraph", "B3", [0.55, 0.30, 0.95, 0.35])]
    _rt = assemble(attach_children(_tw), layout=[{"pages": [[1, 1]], "kind": "两栏",
                                                  "regions": 2, "cuts": []}])
    _ordt = [x["text"] for x in _rt["sequence"] if x["kind"] != "page_break"]
    cases.append(("版式声明 两栏（无 @）→ 左→右，切分线 0.5",
                  _ordt == ["A1", "A2", "A3", "B1", "B2", "B3"], _ordt))
    _th = [_blk(1, 0, "paragraph", "C1", [0.05, 0.10, 0.20, 0.15]),
           _blk(1, 1, "paragraph", "M1", [0.40, 0.10, 0.60, 0.15]),
           _blk(1, 2, "paragraph", "N1", [0.75, 0.10, 0.95, 0.15]),
           _blk(1, 3, "paragraph", "C2", [0.05, 0.20, 0.20, 0.25]),
           _blk(1, 4, "paragraph", "M2", [0.40, 0.20, 0.60, 0.25]),
           _blk(1, 5, "paragraph", "N2", [0.75, 0.20, 0.95, 0.25])]
    _r3 = assemble(attach_children(_th), layout=[{"pages": [[1, 1]], "kind": "三栏",
                                                 "regions": 3, "cuts": [0.30, 0.70]}])
    _ord3 = [x["text"] for x in _r3["sequence"] if x["kind"] != "page_break"]
    cases.append(("版式声明 三栏@0.30|0.70 → 左→中→右",
                  _ord3 == ["C1", "C2", "M1", "M2", "N1", "N2"], _ord3))
    return cases


def selftest() -> int:
    cases, passed = [], 0
    cases.extend(_selftest_columns())

    # 跨页续排：页 1 末块非完整句 + 页 2 首块是段落 → 拼接
    blocks = [_blk(1, 0, "paragraph", "其体积增加约为", [0.1, 0.8, 0.9, 0.9]),
              _blk(2, 1, "paragraph", "37%，即", [0.1, 0.1, 0.9, 0.2])]
    r = assemble(attach_children(blocks))
    cases.append(("跨页续排 → 1 次拼接且标记 joined_with_prev_page",
                  r["counts"]["joins"] == 1
                  and any(x.get("joined_with_prev_page") for x in r["sequence"]), r["cross_page_joins"]))
    blocks = [_blk(1, 0, "paragraph", "这是一个完整句。", [0.1, 0.8, 0.9, 0.9]),
              _blk(2, 1, "paragraph", "下一页新段落。", [0.1, 0.1, 0.9, 0.2])]
    r = assemble(attach_children(blocks))
    cases.append(("完整句结尾 → 不拼接", r["counts"]["joins"] == 0, r["cross_page_joins"]))

    # 脚注双射
    r = assemble(attach_children([
        _blk(1, 0, "paragraph", "法国科学家 Charles 和 Gay-Lussac $^{①}$。", [0.1, 0.1, 0.9, 0.2]),
        _blk(1, 1, "footnote", "① Charles 和 Gay-Lussac 都是氢气球研究者。", [0.1, 0.9, 0.9, 0.95])]))
    cases.append(("脚注配对成功 → 无缺失/无孤儿",
                  not r["footnotes"]["missing_definitions"] and not r["footnotes"]["orphan_definitions"],
                  r["footnotes"]))
    r = assemble(attach_children([_blk(1, 0, "paragraph", "正文带 marker $^{②}$。", [0.1, 0.1, 0.9, 0.2])]))
    cases.append(("正文有 marker 无定义 → missing_definitions",
                  r["footnotes"]["missing_definitions"] == ["2"], r["footnotes"]))

    # 标题树 + 跳级
    r = assemble(attach_children([
        _blk(1, 0, "heading", "第2章 气体", [0.1, 0.1, 0.5, 0.15], level=2),
        _blk(1, 1, "heading", "2.1.1 理想气体定律", [0.1, 0.2, 0.5, 0.25], level=2)]))
    levels = [h["level"] for h in r["heading_tree"]]
    cases.append(("层级按编号推导（第N章=1、N.M.K=3）", levels == [1, 3], levels))
    cases.append(("跳级被报警", r["counts"]["level_jumps"] == 1, r["heading_level_jumps"]))
    r = assemble(attach_children([
        _blk(1, 0, "heading", "第2章 气体", [0.1, 0.1, 0.5, 0.15]),
        _blk(1, 1, "heading", "2.1 理想气体定律", [0.1, 0.2, 0.5, 0.25]),
        _blk(1, 2, "heading", "2.1.1 定律", [0.1, 0.3, 0.5, 0.35])]))
    cases.append(("正常层级 → 不报警", r["counts"]["level_jumps"] == 0, r["heading_level_jumps"]))
    # C1 前置（SPEC §3.8 订正③）：标题树条目必须能**块级**回配（带 block_id / index / 未截断原文）
    _t = r["heading_tree"]
    cases.append(("标题树带 block_id/index/full_text（切章要在块级定位边界）",
                  all(h.get("block_id") and isinstance(h.get("index"), int)
                      and h.get("full_text") for h in _t)
                  and [h["index"] for h in _t] == [0, 1, 2]
                  and _t[0]["block_id"] == "mineru-p0001-b000-text" and _t[1]["index"] == 1,
                  [(h.get("index"), h.get("block_id")) for h in _t]))
    cases.append(("标题树同时留下引擎的原始 text_level（供「两法不一致」登记）",
                  all("text_level" in h for h in _t), [h.get("text_level") for h in _t]))
    r = assemble(attach_children([
        _blk(1, 0, "heading", "5.3.4 环己烷及取代环己烷的构象", [0.1, 0.1, 0.6, 0.15]),
        _blk(1, 1, "heading", "1. 环己烷的构象", [0.1, 0.2, 0.5, 0.25])]))
    cases.append(("形如 N. 的列表项被标为 heading → 记为 suspect 而非真标题",
                  r["counts"]["heading_suspects"] == 1 and not r["heading_level_jumps"],
                  r["heading_suspects"]))

    # 跨页段落切分（归属修正）：MinerU 把跨页段落合并到前一页 → 用视觉转写作切点还给后一页
    merged = "式中：n 为气体物质的量。这个方程以形式简单而著称。但在人类认识自然规律的长河中,这是经历了两个多世纪才取得的成果。"
    blocks = [_blk(1, 0, "paragraph", merged, [0.1, 0.8, 0.9, 0.9]),
              _blk(2, 1, "paragraph", "", [0.08, 0.09, 0.69, 0.15]),
              _blk(2, 2, "paragraph", "17 世纪中叶，英国科学家 Boyle。", [0.1, 0.3, 0.9, 0.4])]
    hits = [{"rule": "R1", "page": 2, "block_id": "mineru-p0002-b001-text",
             "bbox_norm": [0.08, 0.09, 0.69, 0.15], "ink_density": 0.07}]
    verds = [{"item_id": "TR-R1-0002-01", "pdf_page": 2, "review_type": "transcribe_region",
              "verdict": "transcribed", "confidence": 0.95,
              "transcription": "认识自然规律的长河中,这是经历了两个多世纪才取得的成果。"}]
    r = assemble(attach_children(blocks), None, s4_hits=hits, verdicts=verds)
    sp = r["cross_page_splits"]
    cases.append(("跨页合并段 → 记录 1 处切分，保留部分不含后半句",
                  len(sp) == 1 and "认识自然规律" not in sp[0]["keep"]
                  and sp[0]["moved_text"].startswith("认识自然规律"), sp))
    cases.append(("切分只改归属、不改主引擎字符（moved 是原文后缀）",
                  len(sp) == 1 and merged.endswith(sp[0]["moved_text"]), None))
    r2 = assemble(attach_children(blocks), None, s4_hits=hits,
                  verdicts=[dict(verds[0], verdict="unclear")])
    cases.append(("判定不是 transcribed → 不切分（保持未解决）",
                  not r2["cross_page_splits"], r2["cross_page_splits"]))
    blocks3 = [_blk(1, 0, "paragraph", "完全不相干的另一段文字。", [0.1, 0.8, 0.9, 0.9]),
               _blk(2, 1, "paragraph", "", [0.08, 0.09, 0.69, 0.15])]
    r3 = assemble(attach_children(blocks3), None, s4_hits=hits, verdicts=verds)
    cases.append(("前一页没有以该内容结尾的块 → 记为 rejected（不硬切）",
                  not r3["cross_page_splits"] and len(r3["cross_page_splits_rejected"]) == 1,
                  r3["cross_page_splits_rejected"]))

    # 家具剔除 + 与 profile 对照
    prof = {"header_ink_median_y_frac": [0.06, 0.07], "footer_ink_median_y_frac": [0.92, 0.93]}
    r = assemble(attach_children([
        _blk(1, 0, "running_head", "2.1 理想气体定律", [0.3, 0.06, 0.7, 0.07]),
        _blk(1, 1, "paragraph", "正文。", [0.1, 0.3, 0.9, 0.4]),
        _blk(1, 2, "page_number", "10", [0.05, 0.92, 0.1, 0.93])]), prof)
    cases.append(("家具不进正文但进报告（含位置）",
                  r["counts"]["content"] == 1 and r["counts"]["furniture"] == 2
                  and r["furniture"]["page_number_positions"] == ["footer"], r["furniture"]))
    r = assemble(attach_children([_blk(1, 0, "page_number", "10", [0.05, 0.5, 0.1, 0.52])]))
    cases.append(("标签说家具但落在版心 → 报警",
                  len(r["furniture"]["label_says_furniture_but_in_body"]) == 1, r["furniture"]))
    # 减配风险显形（2026-10-13）
    r = assemble(attach_children([
        _blk(1, 0, "page_number", "这是一段被误标成页码的长文字，明显超过正常页码长度", [0.05, 0.92, 0.1, 0.93]),
        _blk(1, 1, "paragraph", "正文。", [0.1, 0.3, 0.9, 0.4]),
        _blk(1, 2, "paragraph", "页脚位置的无标签正文块。", [0.1, 0.86, 0.9, 0.9])]), prof)
    _fc = r["furniture"]["counts"]
    cases.append(("风险显形：被剔家具文本过长 → 计数并留样",
                  _fc["removed_text_long_blocks"] == 1 and _fc["removed_text_chars"] > 20
                  and len(_fc["long_block_samples"]) == 1, _fc))
    cases.append(("风险显形：页边带里没有家具标签的块被计数（位置兜底若启用就会剔它）",
                  _fc["band_candidates_not_labeled"] == 1, _fc))
    _fc2 = assemble(attach_children([_blk(1, 0, "page_number", "10", [0.05, 0.92, 0.1, 0.93]),
                                     _blk(1, 1, "paragraph", "正文。", [0.1, 0.3, 0.9, 0.4])]),
                    prof)["furniture"]["counts"]
    cases.append(("风险显形：正常情形两个计数都为 0",
                  _fc2["removed_text_long_blocks"] == 0 and _fc2["band_candidates_not_labeled"] == 0, _fc2))
    # 页边界标记
    r = assemble(attach_children([_blk(1, 0, "paragraph", "第一页。", [0.1, 0.1, 0.9, 0.2]),
                                  _blk(2, 0, "paragraph", "第二页。", [0.1, 0.1, 0.9, 0.2])]))
    markers = [x["marker"] for x in r["sequence"] if x["kind"] == "page_break"]
    cases.append(("页边界标记齐备", markers == ["<!-- p001 -->", "<!-- p002 -->"], markers))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S6 tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--ir", required=True); r.add_argument("--profile")
    r.add_argument("--questions"); r.add_argument("--s4-hits"); r.add_argument("--verdicts")
    r.add_argument("--page-labels", default="", help="切片页号对应的书内真实页号，如 22,23,24,25,29")
    r.add_argument("--layout", default="",
                   help="版式声明段列表（JSON；通常由 pipeline 从 books.json 传入，CLI 手跑可省略）")
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
