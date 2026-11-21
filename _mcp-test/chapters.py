# -*- coding: utf-8 -*-
"""C1 章切分（SPEC §3.8）：从 IR 的 **heading 树**切章——纯函数，不碰产物字节。

用法（库）:
  from chapters import split_chapters
  res = split_chapters(structure, blocks)          # -> {"chapters": [...], "boundaries": [...], ...}
用法（自测）:
  python -X utf8 chapters.py selftest

口径（SPEC §3.8 + 本轮订正，逐条可判）:
  1. **"原生层级"就是 `heading_tree.level`**（S6 的编号形态优先、`text_level` 兜底）。
     实测 MinerU 把章标题与节标题标成同一级（全部 `text_level=2`），所以**不得**直接用裸 `text_level`。
  2. **边界判据**：heading_tree 里 `level==1` 且文本命中章标题模式（`^第N章` / `^Chapter N`）。
     `level!=1` 但文本命中 → **冲突**：登记、**不切**（SPEC §3.8 裁定③"宁可文件大，不可切错"）。
  3. **同页两个章级候选** → 取阅读顺序第一个、登记 suspect。
  4. **章号**：优先"书里印的"（`第2章`→`02`）；中文数字先映射；映射不出/无号 → **出现顺序号**，并记 notes。
  5. **页内块级切分**：边界落在页中间时，同页块按阅读顺序分属两章（实测第 2 章首页就是这样）。
  6. **章前内容**（封面/前言/目录…）→ `chapter-00-front`；**没有任何章边界 → 单章交付（合法）**。
  7. **`running_head` 永不作为边界**：它已被 S6 当家具排除在 `content`/`heading_tree` 之外；
     本模块**只读 heading_tree**，不做任何"全文搜 `第N章`"的兜底（那正是靠页眉取号的旧缺陷）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contracts import CHAPTER_NO_RE, FURNITURE

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录


CHAPTER_EN_RE = re.compile(r"^Chapter\s+([0-9]+|[IVXLC]+)\b", re.I)

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100}

FRONT_FILE = "chapter-00-front"


def cn_to_int(s: str):
    """中文数字 → 整数（只处理章号范围，`第一章`=1、`第十章`=10、`第二十三章`=23）。

    解析不出返回 None（**不猜**）——调用方回退"出现顺序号"并登记。
    """
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, cur, seen = 0, 0, False
    for ch in s:
        if ch in _CN_DIGITS:
            cur = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (cur or 1) * unit
            cur = 0
            seen = True
        else:
            return None
    return (total + cur) if seen else None


def parse_chapter_no(text: str):
    """从标题文本抽出**书里印的**章号；抽不出返回 (None, kind)。

    `kind` 只用于审计：`arabic` / `cjk` / `latin` / `*_unparsed` / `none`。
    """
    t = (text or "").strip()
    m = CHAPTER_NO_RE.match(t)
    if m:
        raw = m.group(1)
        if raw.isdigit():
            return int(raw), "arabic"
        n = cn_to_int(raw)
        return (n, "cjk") if n is not None else (None, "cjk_unparsed")
    m = CHAPTER_EN_RE.match(t)
    if m:
        raw = m.group(1)
        if raw.isdigit():
            return int(raw), "latin"
        # 罗马数字（只支持常见范围）
        vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
        tot, prev = 0, 0
        for ch in reversed(raw.upper()):
            v = vals.get(ch)
            if not v:
                return None, "latin_unparsed"
            tot += -v if v < prev else v
            prev = max(prev, v)
        return (tot or None), "latin"
    # 形如章标题但号不可解析（如"第?章"）：**识别成候选**（好登记），但章号交回退顺序号
    if re.match(r"^第\s*\S{0,6}?\s*章", t) or re.match(r"^Chapter\b", t, re.I):
        return None, "unparsed"
    return None, "none"


def _order_key(b) -> tuple:
    bb = b.get("bbox_norm") or [0, 0, 0, 0]
    return (b.get("page", 0), bb[1], bb[0])


def content_blocks(blocks, page_labels=None) -> list:
    """参与切章的正文块：`role=primary` 且非家具，按 (page, y, x) 排序。

    `page_labels`：切片页号 → 书内真实页号（S6 已用的同一约定）；只影响报告里的页码显示。
    """
    out = [b for b in blocks
           if b.get("role") == "primary" and b.get("kind") not in FURNITURE]
    out.sort(key=_order_key)
    return out


def _exact_pos(content, page, block_id, text) -> int:
    """把 heading 条目定位到正文序列的下标。

    优先 `block_id` 精确命中（C1 前置已让 S6 写进标题树）；退化用 (page, 文本) 匹配。
    退化路径**必须唯一**：同页有多个块以该文本开头时返回 -1（登记"不硬切"），
    否则会静默把第二个同前缀标题也定位到第一个块上（实测自测里就撞了）。
    """
    if block_id:
        for i, b in enumerate(content):
            if b.get("block_id") == block_id:
                return i
    t = (text or "").strip()
    if not t:
        return -1
    exact = [i for i, b in enumerate(content)
             if b.get("page") == page and (b.get("text_raw") or "").strip() == t]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return -1
    pre = [i for i, b in enumerate(content)
           if b.get("page") == page and (b.get("text_raw") or "").strip().startswith(t[:40])]
    return pre[0] if len(pre) == 1 else -1


def _normalize_anchors(anchors):
    """把 `--anchors` / `books.json.chapter_anchors` 归一成**严格递增的正整数列表**。

    非法形状（非整数、<1、不递增/重复）→ `ValueError`（硬校验；不静默兜底）。
    """
    if anchors is None or anchors == "":
        return []
    if isinstance(anchors, str):
        try:
            vals = []
            for part in anchors.replace(" ", "").split(","):
                if not part:
                    continue
                if "-" in part:                     # 也接受范围写法（与 intake --toc 1-9 同口径）
                    a, b = part.split("-", 1)
                    a, b = int(a), int(b)
                    if a > b:
                        raise ValueError
                    vals.extend(range(a, b + 1))
                else:
                    vals.append(int(part))
            anchors = vals
        except ValueError:
            raise ValueError("anchors 必须是整数列表、逗号分隔整数，或 a-b 范围（如 1-3,5）")
    if not isinstance(anchors, (list, tuple)):
        raise ValueError("anchors 必须是整数列表或逗号分隔整数")
    out = []
    for a in anchors:
        if isinstance(a, bool) or not isinstance(a, int) or a < 1:
            raise ValueError(f"anchors 含非法值：{a!r}（必须 ≥1 的整数）")
        out.append(a)
    if out != sorted(set(out)):
        raise ValueError(f"anchors 必须严格递增且不重复：{out}")
    return out


def anchors_to_page_space(anchors, page_labels=None):
    """**源 PDF 物理页号 → 内容页号（切片页）**（SPEC §3.8 页号口径）。

    `page_labels` = `{片内页号: 源 PDF 物理页号}`（S6/S7 的 `structure.json.page_labels`）。
    恒等映射/缺省 → 原样返回（内容页号本来就等于源页号）。
    """
    a = _normalize_anchors(anchors)
    if not a:
        return []
    labels = {int(k): v for k, v in (page_labels or {}).items()}
    if not labels:
        return a
    inv = {v: k for k, v in labels.items()}
    return [inv.get(x, x) for x in a]


def layout_to_page_space(layout, page_labels=None):
    """**源 PDF 物理页号 → 内容页号（切片页）**（版式声明；SPEC §3.16）。

    `layout` = `books.json.layout` 段列表；`page_labels` = `{片内页: 源页}`。
    恒等/缺省 → 原样返回。
    """
    if not layout:
        return layout
    labels = {int(k): v for k, v in (page_labels or {}).items()}
    inv = {v: k for k, v in labels.items()} if labels else {}
    out = []
    for seg in layout:
        pages = []
        for a, b in seg.get("pages") or []:
            for s in range(int(a), int(b) + 1):
                p = inv.get(s, s)
                if p not in pages:
                    pages.append(p)
        pages.sort()
        rs = []
        if pages:
            start = prev = pages[0]
            for p in pages[1:]:
                if p == prev + 1:
                    prev = p
                    continue
                rs.append([start, prev])
                start = prev = p
            rs.append([start, prev])
        out.append({"pages": rs, "kind": seg.get("kind"),
                    "regions": seg.get("regions"), "cuts": list(seg.get("cuts") or [])})
    return out


def _anchor_boundaries(content, anchors, labels):
    """**章锚定边界**（SPEC §3.8「章锚」）：声明优先，不扫 heading_tree。

    - 每条锚点页取"第一个**章标题**"（无则页内首个 heading；再无则页内首个正文块）；
    - 锚点页无正文块 → 顺延到下一页并记 `notes`；超出内容范围 → 记 `suspects`（不阻断）；
    - 章号仍优先印刷号，解析不出回退**出现顺序号**（与无锚点同口径）。
    """
    notes, suspects, boundaries = [], [], []
    seen_pos = set()
    for a in anchors:
        page_rows = [(i, b) for i, b in enumerate(content) if b.get("page") == a]
        if not page_rows:
            nxt = next(((i, b) for i, b in enumerate(content) if (b.get("page") or 0) > a), None)
            if nxt is None:
                suspects.append({"page": a, "block_id": None, "text": "",
                                 "why": f"章锚 p{a} 超出内容范围 → 忽略（可疑）",
                                 "level": None, "engine_text_level": None})
                continue
            i, b = nxt
            notes.append({"page": a, "block_id": b.get("block_id"),
                          "why": f"章锚 p{a} 该页无正文块 → 边界顺延到 p{b.get('page')}"})
            page_rows = [(i, b)]
        chosen = None
        for i, b in page_rows:
            if b.get("kind") == "heading":
                _t = (b.get("text_raw") or "").strip()
                if parse_chapter_no(_t)[1] != "none":
                    chosen = (i, b)
                    break
        if chosen is None:
            for i, b in page_rows:
                if b.get("kind") == "heading":
                    chosen = (i, b)
                    break
        if chosen is None:
            chosen = page_rows[0]
        i, b = chosen
        if i in seen_pos:
            suspects.append({"page": b.get("page"), "block_id": b.get("block_id"), "text": "",
                             "why": f"章锚 p{a} 与前一锚点落到同一块 → 忽略（可疑）",
                             "level": None, "engine_text_level": None})
            continue
        seen_pos.add(i)
        txt = (b.get("text_raw") or "").strip()
        printed, _kind = parse_chapter_no(txt) if b.get("kind") == "heading" else (None, "none")
        boundaries.append({"seq": 0, "chapter_no": printed,
                           "chapter_rule": ("printed" if printed is not None else "sequence"),
                           "title": txt if b.get("kind") == "heading" else None,
                           "page": b.get("page"), "book_page": labels.get(b.get("page"), b.get("page")),
                           "block_id": b.get("block_id"), "pos": i,
                           "level": b.get("level"), "engine_text_level": b.get("text_level"),
                           "anchor": True, "anchor_page": a})
    boundaries.sort(key=lambda x: x["pos"])
    for n, bd in enumerate(boundaries, 1):
        bd["seq"] = n
        if bd["chapter_no"] is None:
            bd["chapter_no"] = n
            bd["chapter_rule"] = "sequence"
            notes.append({"page": bd["page"], "block_id": bd.get("block_id"),
                          "why": f"章锚 p{bd['anchor_page']} 的边界块不是可解析章标题 → 回退顺序号 {n:02d}"})
    seen_printed = {}
    for bd in boundaries:
        p = bd["chapter_no"]
        if bd["chapter_rule"] == "printed" and p in seen_printed:
            suspects.append({"page": bd["page"], "block_id": bd.get("block_id"), "text": "",
                             "why": f"章号 {p:02d} 重复出现（上一处 p{seen_printed[p]}）→ 保留两处边界、登记",
                             "level": None, "engine_text_level": None})
        seen_printed.setdefault(p, bd["page"])
    return boundaries, notes, suspects


def split_chapters(structure: dict, blocks: list, page_labels=None, input_kind: str = "",
                   anchors=None, toc_pages=None) -> dict:
    """切章。返回 `chapters` / `boundaries` / `suspects` / `empty_chapters` / `rule` / `notes`
    / `input_kind` / `input_pages`。

    `input_kind`（SPEC §3.8 订正④ + §3.9）：`book_sequence`（合并后的**完整书级序列**）或
    `partial_range`（不连续/只交一部分）。**缺省**（未声明）按 `partial_range` 处理——保持现状、不加提示。
    **判据**：`book_sequence` 却一个章边界都没有（`rule=single`）→ **可疑**（整本书不该没有章标题），
    进 `suspects`；**不阻断**（阻断与否由门禁 `G20` 判）。

    `anchors`（SPEC §3.8「章锚」）：片内页号、1 起、严格递增；**可选**。有锚点时
    **跳过 heading_tree 扫描**，按声明页定边界（见 `_anchor_boundaries`）；无锚点时行为逐字节不变。

    纯函数：只读入参与 `blocks`，不写盘、不改任何现有产物。
    """
    tree = (structure or {}).get("heading_tree") or []
    content = content_blocks(blocks, page_labels)
    labels = {int(k): v for k, v in (page_labels or (structure or {}).get("page_labels") or {}).items()}
    _ik = (input_kind or (structure or {}).get("input_kind") or "partial_range")
    if _ik not in ("book_sequence", "partial_range"):
        _ik = "partial_range"
    suspects, notes, boundaries = [], [], []
    seq = 0
    seen_printed = {}
    anchors = _normalize_anchors(anchors)
    toc = _normalize_anchors(toc_pages)
    toc_set = set(toc)
    toc_skipped = []

    # ---- 1) 候选识别（只认 level==1 且命中模式的标题；其余命中 → 冲突登记、不切）
    for h in tree:
        txt = (h.get("full_text") or h.get("text") or "").strip()
        if not txt:
            continue
        # **目录页声明**（SPEC §3.8）：目录里印着每一章的标题；这些 heading **不得**成为章边界。
        if h.get("page") in toc_set:
            toc_skipped.append({"page": h.get("page"), "block_id": h.get("block_id"),
                                "text": txt[:60]})
            continue
        printed, kind = parse_chapter_no(txt)
        matched = kind != "none"
        if not matched:
            continue
        lvl = h.get("level")
        if lvl != 1:
            suspects.append({"page": h.get("page"), "block_id": h.get("block_id"), "text": txt[:60],
                             "why": f"文本形如章标题，但 S6 层级={lvl}（非章级）→ 不切，登记",
                             "level": lvl, "engine_text_level": h.get("text_level")})
            continue
        pos = _exact_pos(content, h.get("page"), h.get("block_id"), txt)
        if pos < 0:
            suspects.append({"page": h.get("page"), "block_id": h.get("block_id"), "text": txt[:60],
                             "why": "章级标题无法在正文序列里定位 → 不切，登记",
                             "level": lvl, "engine_text_level": h.get("text_level")})
            continue
        seq += 1
        num, rule = printed, ("printed" if printed is not None else "sequence")
        if printed is None:
            num = seq
            notes.append({"page": h.get("page"), "block_id": h.get("block_id"),
                          "why": f"章号无法从「{txt[:30]}」解析 → 回退出现顺序号 {num:02d}", "kind": kind})
        # 两条"层级一致但 text_level 与推导不一致"的记录：**不是冲突**（SPEC §3.8 订正②），只记 note
        if h.get("text_level") not in (None, 1):
            notes.append({"page": h.get("page"), "block_id": h.get("block_id"),
                          "why": f"引擎 text_level={h.get('text_level')} 与编号推导 level=1 不一致"
                                 f"（MinerU 章/节同级，实测常见）→ 不阻断切分"})
        prev_same_page = any(b["page"] == h.get("page") for b in boundaries)
        if prev_same_page:
            suspects.append({"page": h.get("page"), "block_id": h.get("block_id"), "text": txt[:60],
                             "why": "同一页出现两个章级候选 → 取阅读顺序第一个，登记",
                             "level": lvl, "engine_text_level": h.get("text_level")})
        if printed is not None and printed in seen_printed:
            suspects.append({"page": h.get("page"), "block_id": h.get("block_id"), "text": txt[:60],
                             "why": f"章号 {printed:02d} 重复出现（上一处 p{seen_printed[printed]}）"
                                    f"→ 保留两处边界、登记",
                             "level": lvl, "engine_text_level": h.get("text_level")})
        if printed is not None:
            seen_printed.setdefault(printed, h.get("page"))
        boundaries.append({"seq": seq, "chapter_no": num, "chapter_rule": rule, "title": txt,
                           "page": h.get("page"), "book_page": labels.get(h.get("page"), h.get("page")),
                           "block_id": h.get("block_id"), "pos": pos,
                           "level": lvl, "engine_text_level": h.get("text_level")})

    boundaries.sort(key=lambda x: x["pos"])

    # **章锚（SPEC §3.8）**：有声明时，heading_tree 扫描结果整体作废，按锚点重新定边界。
    if anchors:
        boundaries, notes, suspects = _anchor_boundaries(content, anchors, labels)

    # ---- 2) 按边界切正文（**边界块本身归它自己那一章**；页内块级切分天然成立）
    chapters = []
    if not boundaries:
        chapters.append({"chapter_no": None, "file_stem": "chapter-01", "rule": "single",
                         "title": None, "blocks": list(content), "boundaries": [],
                         "pages": sorted({b["page"] for b in content})})
    else:
        first = boundaries[0]
        if first["pos"] > 0:                       # 首个边界之前 → 章前内容单独成篇
            head = content[: first["pos"]]
            chapters.append({"chapter_no": 0, "file_stem": FRONT_FILE, "rule": "front_matter",
                             "title": None, "blocks": head, "boundaries": [],
                             "pages": sorted({b["page"] for b in head})})
        for i, bd in enumerate(boundaries):
            start = bd["pos"]
            end = boundaries[i + 1]["pos"] if i + 1 < len(boundaries) else len(content)
            _no = bd["chapter_no"]
            # 文件前缀**必须唯一**（同名文件会互相覆盖、校验器也读不全）；
            # 同章号再次出现（重复边界）→ 同号但加序号后缀，并留 note（"不静默"）
            _stem = f"{FRONT_FILE}" if _no == 0 else f"chapter-{_no:02d}"
            if any(c["file_stem"] == _stem for c in chapters):
                _stem = f"{_stem}-{bd['seq']}"
                notes.append({"page": bd["page"], "block_id": bd["block_id"],
                              "why": f"章号 {_no:02d} 在同一交付里再次出现 → 文件前缀改为 {_stem}"
                                     f"（登记，不静默）"})
            chapters.append({"chapter_no": _no, "file_stem": _stem,
                             "rule": bd["chapter_rule"], "title": bd["title"],
                             "blocks": content[start:end], "boundaries": [bd],
                             "pages": sorted({b["page"] for b in content[start:end]})})

    # ---- 3) 逐章记账，再判"空章"（有边界但**只有标题、没有正文块**）→ 登记，不静默
    #      `chapter_no=0`（章前内容）不算空章：它是"没有章标题的正文"。
    for c in chapters:
        c["block_count"] = len(c["blocks"])
        c["body_count"] = sum(1 for b in c["blocks"]
                              if b.get("kind") in ("paragraph", "equation", "table", "figure", "unknown"))
        c["char_count"] = sum(len(b.get("text_raw") or "") for b in c["blocks"])
    empty = [{"chapter_no": c["chapter_no"], "file_stem": c["file_stem"],
              "page": c["boundaries"][0]["page"] if c["boundaries"] else None,
              "block_count": c["block_count"], "body_count": c["body_count"]}
             for c in chapters if c["body_count"] == 0 and c["chapter_no"] not in (0, None)]

    rule = ("single" if not boundaries else
            ("printed" if all(b["chapter_rule"] == "printed" for b in boundaries)
             else "mixed" if any(b["chapter_rule"] == "printed" for b in boundaries) else "sequence"))
    # **订正⑥的可判定形式**：整本书（`book_sequence`）却一个章标题都没有 → **可疑**（不静默、不阻断）
    if rule == "single" and _ik == "book_sequence":
        suspects.append({"page": None, "block_id": None,
                         "text": (content[0].get("text_raw") or "")[:40] if content else "",
                         "why": "输入是**完整书级序列**（input_kind=book_sequence）却没有任何章级标题 "
                                "→ 可疑（整本书不该没有章边界）；按现状仍单章交付，但登记待查",
                         "level": None, "engine_text_level": None})
    _pages = sorted({b["page"] for b in content})
    return {"rule": rule, "chapters": chapters, "boundaries": boundaries,
            "suspects": suspects, "empty_chapters": empty, "notes": notes,
            "input_kind": _ik, "anchors": anchors,
            "toc_pages": toc, "toc_skipped": toc_skipped,
            "input_pages": [{"page": p, "book_page": labels.get(p, p)} for p in _pages],
            "content_blocks": len(content),
            "covered_blocks": sum(c["block_count"] for c in chapters)}


# ---------------------------------------------------------------- selftest
def _blk(page, i, kind, text, y=0.1, role="primary", bid=None):
    return {"block_id": bid or f"mineru-p{page:04d}-b{i:03d}-text", "engine": "mineru", "page": page,
            "order": i, "kind": kind, "role": role, "parent_id": None, "source_field": "text",
            "text_raw": text, "text_canon": text, "bbox_norm": [0.1, y, 0.9, y + 0.05],
            "bbox_src": None, "bbox_space": "x", "flags": [], "text_level": 2}


def _h(page, i, text, level=1, bid=None, text_level=2):
    return {"index": i, "block_id": bid or f"mineru-p{page:04d}-b{i:03d}-text", "page": page,
            "level": level, "text": text[:60], "full_text": text, "text_level": text_level,
            "suspect_list_item": False}


def selftest() -> int:
    cases, passed = [], 0

    # ① 正常切：一章尾 + 一章首（**页内块级**）
    blocks = [_blk(5, 0, "paragraph", "第 1 章末尾的正文。", 0.10),
              _blk(5, 1, "heading", "第2章 气体", 0.20),
              _blk(5, 2, "heading", "2.1 理想气体定律", 0.30),
              _blk(5, 3, "paragraph", "气体(gas)是物质的一种状态。", 0.40)]
    st = {"heading_tree": [_h(5, 1, "第2章 气体", 1)]}
    r = split_chapters(st, blocks)
    cases.append(("页内块级切分：同页前块归第1章（章前）、边界块起归第2章",
                  [c["file_stem"] for c in r["chapters"]] == ["chapter-00-front", "chapter-02"]
                  and r["chapters"][0]["block_count"] == 1 and r["chapters"][1]["block_count"] == 3,
                  [(c["file_stem"], c["block_count"], c["pages"]) for c in r["chapters"]]))
    cases.append(("章号用**书里印的**（第2章 → 02）、rule=printed",
                  r["chapters"][1]["chapter_no"] == 2 and r["rule"] == "printed", r["rule"]))
    cases.append(("覆盖不变量：每块恰好属于一章",
                  r["covered_blocks"] == len(blocks) == r["content_blocks"], r["covered_blocks"]))

    # ② 无章边界 → 单章交付（合法）
    blocks2 = [_blk(1, 0, "paragraph", "正文一。"), _blk(2, 0, "paragraph", "正文二。")]
    r2 = split_chapters({"heading_tree": [_h(1, 0, "5.3.3 环戊烷的构象", 3)]}, blocks2)
    cases.append(("无章边界 → 单章（rule=single，不产 00-front）",
                  len(r2["chapters"]) == 1 and r2["chapters"][0]["rule"] == "single"
                  and r2["chapters"][0]["file_stem"] == "chapter-01" and r2["rule"] == "single",
                  [(c["file_stem"], c["rule"]) for c in r2["chapters"]]))

    # ③ 章号取不到（中文数字可解析 / 不可解析回退顺序号）
    blocks3 = [_blk(1, 0, "heading", "第一章 绪论", 0.1),
               _blk(1, 1, "paragraph", "正文。", 0.2),
               _blk(9, 0, "heading", "第十一章 元素化学", 0.1),
               _blk(9, 1, "paragraph", "正文。", 0.2)]
    r3 = split_chapters({"heading_tree": [_h(1, 0, "第一章 绪论", 1), _h(9, 0, "第十一章 元素化学", 1)]},
                        blocks3)
    cases.append(("中文数字章号 → 映射成书里印的 01 / 11",
                  [c["chapter_no"] for c in r3["chapters"]] == [1, 11]
                  and all(c["rule"] == "printed" for c in r3["chapters"]),
                  [c["chapter_no"] for c in r3["chapters"]]))
    r3b = split_chapters({"heading_tree": [_h(4, 0, "第?章 无从解析", 1)]},
                         [_blk(4, 0, "heading", "第?章 无从解析", 0.1),
                          _blk(4, 1, "paragraph", "正文。", 0.2)])
    cases.append(("章号解析不出 → **不猜**，回退出现顺序号并记 notes",
                  r3b["chapters"][0]["chapter_no"] == 1 and r3b["notes"], r3b["notes"]))

    # ④ 冲突：文本像章标题但 S6 层级不是 1 → **不切、登记**
    r4 = split_chapters({"heading_tree": [_h(2, 0, "第3章 化学平衡", 2)]},
                        [_blk(2, 0, "heading", "第3章 化学平衡", 0.1),
                         _blk(2, 1, "paragraph", "正文。", 0.2)])
    cases.append(("层级≠1 的章级文本 → 不切、进 suspects（宁可不切）",
                  len(r4["chapters"]) == 1 and r4["chapters"][0]["rule"] == "single"
                  and len(r4["suspects"]) == 1, r4["suspects"]))

    # ⑤ text_level 与编号推导不一致 → **不是冲突**（否则真实书上一条也切不出来）
    r5 = split_chapters({"heading_tree": [_h(1, 0, "第2章 气体", 1, text_level=2)]},
                        [_blk(1, 0, "heading", "第2章 气体", 0.1),
                         _blk(1, 1, "paragraph", "正文。", 0.2)])
    cases.append(("text_level=2 与 level=1 不一致 → 只记 notes、照切",
                  r5["chapters"][0]["chapter_no"] == 2 and not r5["suspects"] and r5["notes"],
                  {"suspects": r5["suspects"], "notes": len(r5["notes"])}))

    # ⑥ 同页两个章级候选 → 取第一个 + 登记
    r6 = split_chapters({"heading_tree": [_h(3, 0, "第4章 溶液", 1), _h(3, 5, "第5章 氧化还原", 1)]},
                        [_blk(3, 0, "heading", "第4章 溶液", 0.1),
                         _blk(3, 1, "paragraph", "正文。", 0.2),
                         _blk(3, 5, "heading", "第5章 氧化还原", 0.6),
                         _blk(3, 6, "paragraph", "正文。", 0.7)])
    cases.append(("同页两个章级候选 → 两个边界都在、并登记 suspect",
                  [c["chapter_no"] for c in r6["chapters"]] == [4, 5] and r6["suspects"],
                  {"chapters": [c["chapter_no"] for c in r6["chapters"]], "suspects": r6["suspects"]}))

    # ⑦ 空章：某条边界之后没有任何块 → 登记（不静默）
    #    （`第9章` 只有标题、正文全在别处：块数为 1，但没有**正文块**）
    r7 = split_chapters({"heading_tree": [_h(7, 1, "第8章 配位化合物", 1),
                                          _h(8, 9, "第9章 化学动力学", 1)]},
                        [_blk(7, 0, "paragraph", "第 7 章正文。", 0.1),
                         _blk(7, 1, "heading", "第8章 配位化合物", 0.2),
                         _blk(7, 2, "paragraph", "配位化合物的正文。", 0.25),
                         _blk(8, 9, "heading", "第9章 化学动力学", 0.3)])
    cases.append(("章里只有标题、没有正文 → 记进 empty_chapters（不静默）",
                  len(r7["empty_chapters"]) == 1 and r7["empty_chapters"][0]["chapter_no"] == 9,
                  r7["empty_chapters"]))
    # ⑦b 新增块（role=child）不参与切章，但**不能被丢掉**
    r7b = split_chapters({"heading_tree": [_h(1, 0, "第1章 绪论", 1)]},
                         [_blk(1, 0, "heading", "第1章 绪论", 0.1),
                          _blk(1, 1, "caption", "表 1.1 数据", 0.2, role="child"),
                          _blk(1, 2, "paragraph", "正文。", 0.3)])
    cases.append(("子块（图表题注）不进切章输入，但也不算丢块（只切 primary）",
                  r7b["content_blocks"] == 2 and r7b["covered_blocks"] == 2
                  and len(r7b["chapters"]) == 1, r7b["content_blocks"]))

    # ⑧ **跨页续句跨章边界**：续排已在 S6 做掉；切章只看块归属 → 后半句跟着它的块走
    blocks8 = [_blk(1, 0, "paragraph", "……而著称。但在人类", 0.9),
               _blk(2, 0, "paragraph", "认识自然规律的长河中", 0.1),
               _blk(2, 1, "heading", "第2章 气体", 0.2)]
    r8 = split_chapters({"heading_tree": [_h(2, 1, "第2章 气体", 1)]}, blocks8)
    cases.append(("跨页续句：两半块各归其页所在章，边界不吞掉续句",
                  r8["chapters"][0]["block_count"] == 2 and r8["chapters"][1]["block_count"] == 1,
                  [(c["file_stem"], c["block_count"]) for c in r8["chapters"]]))

    # ⑨ `running_head`（页眉里的"第N章"）**绝不**作为边界
    rh = _blk(2, 9, "running_head", "第5章 脂环烃", 0.06)
    r9 = split_chapters({"heading_tree": [_h(1, 0, "5.3.3 环戊烷的构象", 3)]},
                        [_blk(1, 0, "heading", "5.3.3 环戊烷的构象", 0.1), rh])
    cases.append(("页眉里的「第5章」不进正文、不成为边界",
                  len(r9["chapters"]) == 1 and r9["chapters"][0]["rule"] == "single"
                  and r9["content_blocks"] == 1, r9["content_blocks"]))

    # ⑩ 边界定位不到 → 不硬切、登记
    r10 = split_chapters({"heading_tree": [{"index": 0, "block_id": "missing-id", "page": 3,
                                            "level": 1, "text": "第6章 化学热力学", "full_text": "第6章 化学热力学",
                                            "text_level": 2, "suspect_list_item": False}]},
                         [_blk(3, 0, "paragraph", "正文。", 0.1)])
    cases.append(("边界块定位不到 → 不切、登记 suspect",
                  r10["chapters"][0]["rule"] == "single" and len(r10["suspects"]) == 1, r10["suspects"]))

    # ⑬ C2-3：`input_kind` 与订正⑥（整本书却单章 → 可疑）
    _ik_blocks = [_blk(1, 0, "paragraph", "正文一。"), _blk(2, 0, "paragraph", "正文二。")]
    _ik_tree = [_h(1, 0, "5.3.3 环戊烷的构象", 3)]
    r_bs = split_chapters({"heading_tree": _ik_tree}, _ik_blocks, input_kind="book_sequence")
    cases.append(("C2-3⑥：input_kind=book_sequence 却单章 → **登记可疑**（不静默）",
                  r_bs["rule"] == "single" and r_bs["input_kind"] == "book_sequence"
                  and any("可疑" in s["why"] for s in r_bs["suspects"]), r_bs["suspects"]))
    r_pr = split_chapters({"heading_tree": _ik_tree}, _ik_blocks, input_kind="partial_range")
    cases.append(("C2-3：input_kind=partial_range 单章 → **正常**（无提示，保持现状）",
                  r_pr["rule"] == "single" and not r_pr["suspects"], r_pr["suspects"]))
    r_dflt = split_chapters({"heading_tree": _ik_tree}, _ik_blocks)
    cases.append(("C2-3：未声明 input_kind → 缺省按 partial_range（不加提示，向后兼容）",
                  r_dflt["input_kind"] == "partial_range" and not r_dflt["suspects"],
                  r_dflt["input_kind"]))
    cases.append(("C2-3：`input_pages` 逐页留档（含书内页号映射）",
                  len(r_bs["input_pages"]) == 2 and r_bs["input_pages"][0]["page"] == 1,
                  r_bs["input_pages"]))

    # ⑪ 两个同前缀章标题：各自靠 `block_id` 定位（不撞到第一个块上），且**文件前缀不重名**
    blocks11 = [_blk(1, 0, "heading", "第2章 气体", 0.1),
                _blk(1, 1, "paragraph", "正文。", 0.2),
                _blk(1, 2, "heading", "第2章 气体的补充", 0.3)]
    r11 = split_chapters({"heading_tree": [_h(1, 0, "第2章 气体", 1),
                                           _h(1, 2, "第2章 气体的补充", 1)]}, blocks11)
    cases.append(("同前缀两个章级标题 → 各自块级定位、边界 2 条、文件前缀唯一且有登记",
                  [c["block_count"] for c in r11["chapters"]] == [2, 1]
                  and len({c["file_stem"] for c in r11["chapters"]}) == 2
                  and len(r11["boundaries"]) == 2 and r11["suspects"] and r11["notes"],
                  [(c["file_stem"], c["block_count"]) for c in r11["chapters"]]))

    # ⑫ 前置内容 → chapter-00-front，且不产空的前置章
    r12 = split_chapters({"heading_tree": [_h(1, 0, "第1章 绪论", 1)]},
                         [_blk(1, 0, "heading", "第1章 绪论", 0.1),
                          _blk(1, 1, "paragraph", "正文。", 0.2)])
    cases.append(("首块即章标题 → **不产** 00-front（前置章为空就不生成）",
                  [c["file_stem"] for c in r12["chapters"]] == ["chapter-01"]
                  and not r12["empty_chapters"], [c["file_stem"] for c in r12["chapters"]]))

    # ⑭ 章锚（SPEC §3.8）：声明优先，目录页幽灵章被跳过；前置页是副产品
    bA = [_blk(p, 0, "paragraph", f"p{p} 正文。", 0.1) for p in range(1, 13)]
    bA += [_blk(3, 1, "heading", "第16章 化学动力学", 0.2),
           _blk(5, 1, "heading", "第20章 元素化学", 0.2),
           _blk(6, 1, "heading", "第22章 化学文献", 0.2),
           _blk(7, 1, "heading", "第24章 网络检索", 0.2),
           _blk(10, 1, "heading", "第14章 脂肪胺", 0.2)]
    treeA = [_h(3, 1, "第16章 化学动力学", 1), _h(5, 1, "第20章 元素化学", 1),
             _h(6, 1, "第22章 化学文献", 1), _h(7, 1, "第24章 网络检索", 1),
             _h(10, 1, "第14章 脂肪胺", 1)]
    rA = split_chapters({"heading_tree": treeA}, bA, anchors=[1, 10])
    cases.append(("章锚：声明 [1,10] 跳过目录页幽灵章 → 2 章、无幽灵 16/20/22/24",
                  [c["file_stem"] for c in rA["chapters"]] == ["chapter-01", "chapter-14"]
                  and [b["page"] for b in rA["boundaries"]] == [1, 10]
                  and rA["anchors"] == [1, 10], [c["file_stem"] for c in rA["chapters"]]))
    rE = split_chapters({"heading_tree": treeA}, bA)
    cases.append(("无锚点：同一份 heading_tree 仍按扫描切（幽灵章存在）→ 证明锚点真的改变了行为",
                  len(rE["chapters"]) > len(rA["chapters"]), len(rE["chapters"])))
    bB = [b for b in bA if not (b.get("page") == 10 and b.get("kind") == "paragraph")]
    rB = split_chapters({"heading_tree": treeA}, bB, anchors=[10])
    cases.append(("章锚：只声明 p10 → 1-9 页自动成为 `chapter-00-front`（前置页是副产品）",
                  [c["file_stem"] for c in rB["chapters"]] == ["chapter-00-front", "chapter-14"]
                  and rB["chapters"][0]["pages"] == list(range(1, 10)),
                  [(c["file_stem"], c["pages"]) for c in rB["chapters"]]))
    rC = split_chapters({"heading_tree": []},
                        [_blk(1, 0, "paragraph", "p1 正文。", 0.1),
                         _blk(3, 0, "heading", "第2章 气体", 0.1)], anchors=[2])
    cases.append(("章锚：锚点页无正文块 → 顺延到下一页并记 notes（不静默丢锚点）",
                  [c["file_stem"] for c in rC["chapters"]] == ["chapter-00-front", "chapter-02"]
                  and any("顺延" in n.get("why", "") for n in rC["notes"]),
                  [(c["file_stem"], c["pages"]) for c in rC["chapters"]],))
    try:
        split_chapters({"heading_tree": []}, bA, anchors=[3, 2])
        _bad_ok = False
    except ValueError:
        _bad_ok = True
    cases.append(("章锚：非法锚点（非递增）→ ValueError（硬校验，不静默）", _bad_ok, None))
    rF = split_chapters({"heading_tree": []}, bA, anchors=[1])
    cases.append(("章锚：从第 1 页起 → 不产生 `chapter-00-front`",
                  [c["file_stem"] for c in rF["chapters"]] == ["chapter-01"], rF["chapters"][0]["file_stem"]))
    # 目录页声明：目录里的章标题不参与切章；真实章在 p10
    rT = split_chapters({"heading_tree": treeA}, bA, toc_pages=list(range(1, 10)))
    cases.append(("目录页声明 1-9：目录里的 16/20/22/24 不进切章器，只切出 p10 的真章",
                  [c["file_stem"] for c in rT["chapters"]] == ["chapter-00-front", "chapter-14"]
                  and [b["page"] for b in rT["boundaries"]] == [10]
                  and len(rT["toc_skipped"]) == 4, rT["boundaries"]))
    try:
        split_chapters({"heading_tree": []}, bA, toc_pages=[3, 2])
        _bad_toc = False
    except ValueError:
        _bad_toc = True
    cases.append(("目录页声明：非法页号（非递增）→ ValueError", _bad_toc, None))
    _conv = anchors_to_page_space([22, 26], {1: 22, 2: 23, 3: 24, 4: 25, 5: 26})
    cases.append(("章锚换算：源 PDF 物理页号 22,26 + page_labels → 片内 1,5",
                  _conv == [1, 5], _conv))
    _conv2 = anchors_to_page_space([1, 5], {1: 1, 2: 2, 3: 3, 4: 4, 5: 5})
    cases.append(("章锚换算：page_labels 恒等 → 原样", _conv2 == [1, 5], _conv2))
    cases.append(("锚点/目录页接受范围写法：1-3,5 → [1,2,3,5]",
                  _normalize_anchors("1-3,5") == [1, 2, 3, 5], _normalize_anchors("1-3,5")))
    try:
        _normalize_anchors("3-1")
        cases.append(("负例：范围 a>b → ValueError", False, None))
    except ValueError:
        cases.append(("负例：范围 a>b → ValueError", True, None))
    _layc = layout_to_page_space([{"pages": [[22, 26]], "kind": "两栏", "regions": 2, "cuts": []}],
                                 {1: 22, 2: 23, 3: 24, 4: 25, 5: 26})
    cases.append(("版式换算：源页段 22-26 + page_labels → 片内 1-5",
                  _layc and _layc[0]["pages"] == [[1, 5]], _layc))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} chapters tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
