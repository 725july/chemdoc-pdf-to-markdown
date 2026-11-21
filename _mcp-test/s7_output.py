# -*- coding: utf-8 -*-
"""S7 输出：把 IR + S6 装配结果 → 交付物（SPEC v2 §1 / §2 / §4.4）。

产物（`<out>/<book>/`）：
  chapter-NN.md          正文（唯一真相；家具不进正文；页边界 <!-- pNNN -->；未决项 <!-- REVIEW -->）
  figures/fig-p<页4位>-<图号>.png   一律从 300dpi 渲染裁剪（SPEC §2.3）
  errata.md              勘误（U 类：源文本如此、非错误）
  report.json            门禁 / 升级清单（按类别聚合）/ 未决项 / 证据哈希
  index/entities.jsonl   检索 sidecar（规则型别名；U-02 的默认口径）

用法:
  python -X utf8 s7_output.py run --ir <ir.jsonl> --structure <structure.json> --pdf <sample.pdf>
        [--questions <questions.jsonl>] [--s4-hits <s4-hits.jsonl>] --out <dir> --book <name>
  python -X utf8 s7_output.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import textnorm  # noqa: E402
import chapters as chapters_mod  # noqa: E402  （C1 章切分，SPEC §3.8）
from contracts import CHAPTER_ARABIC_RE, FURNITURE, HTML_TAG_RE
from tuning import TUNE   # B1：`vision_cover_min_overlap`（转写覆盖的记账阈值）
import render

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
NUM_LEAD_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")
TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
FIGNO_RE = re.compile(r"图\s*(\d+)\s*[.\-–]\s*(\d+)")
TABLENO_RE = re.compile(r"表\s*(\d+)\.(\d+)")


def sha256_text(t: str) -> str:
    return hashlib.sha256((t or "").encode("utf-8")).hexdigest()[:16]


def strip_tags(s: str) -> str:
    return HTML_TAG_RE.sub("", s or "").strip()


def html_table_to_gfm(html: str) -> tuple[str, list[str]]:
    """HTML 表 → GFM 管道表。GFM 不支持 rowspan/colspan → 命中就**记 flag**，不静默丢结构。"""
    flags = []
    if re.search(r'rowspan\s*=', html or "", re.I):
        flags.append("rowspan_unsupported")
    if re.search(r'colspan\s*=', html or "", re.I):
        flags.append("colspan_unsupported")
    rows = []
    for tr in TR_RE.findall(html or ""):
        cells = [strip_tags(td).replace("|", "\\|") for td in TD_RE.findall(tr)]
        if cells:
            rows.append(cells)
    if not rows:
        return "", flags + ["table_unparsed"]
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out), flags


def clean_equation(text: str) -> str:
    """行间公式：压掉 $$ 内多余的换行/空白（K7 math_lint 的口径），保留 \\tag。"""
    t = (text or "").strip()
    m = re.match(r"^\$\$(.*?)\$\$$", t, re.S)
    if not m:
        return t
    inner = re.sub(r"\s*\n\s*", "", m.group(1)).strip()
    return f"$${inner}$$"


def derive_chapter(heading_tree: list[dict], blocks: list[dict]) -> str:
    for h in heading_tree:
        m = CHAPTER_ARABIC_RE.search(h["text"])
        if m:
            return f"{int(m.group(1)):02d}"
    for h in heading_tree:
        m = NUM_LEAD_RE.match(h["text"].strip())
        if m:
            return f"{int(m.group(1)):02d}"
    for b in blocks:
        m = CHAPTER_ARABIC_RE.search(b.get("text_raw") or "")
        if m:
            return f"{int(m.group(1)):02d}"
    return "00"


def _figno_of(text: str) -> str | None:
    """只返回**唯一**的印刷图号；一段文字里出现多个号（多子图共享题注）→ None（宁缺勿错）。"""
    nums = {f"{a}.{b}" for a, b in FIGNO_RE.findall(text or "")}
    return next(iter(nums)) if len(nums) == 1 else None


def _caption_figno(text: str) -> str | None:
    """只有**以图号开头**的短文本才算题注；排除“如图 1-2 所示”这类正文引用。"""
    t = (text or "").strip()
    if not t or len(t) > 80:
        return None
    if not re.match(r"^图\s*\d+\s*[.\-–]\s*\d+", t):
        return None
    return _figno_of(t)


def _nearby_figno(b: dict, blocks: list[dict]) -> str | None:
    """U-15：图块自己的文字/子块没有图号时，找**邻近的题注块**（p6 的“图 1-1 …”是独立块）。"""
    bb = b.get("bbox_norm")
    page = b.get("page")
    if not bb:
        return None
    best = None
    found = set()
    for o in blocks:
        if o is b or o.get("page") != page or o.get("kind") == "figure":
            continue
        num = _caption_figno(o.get("text_raw") or "")
        ob = o.get("bbox_norm")
        if not num or not ob:
            continue
        if min(bb[2], ob[2]) - max(bb[0], ob[0]) <= 0:
            continue
        gap = min(abs(ob[1] - bb[3]), abs(bb[1] - ob[3]))
        if gap > 0.05:
            continue
        found.add(num)
        key = (round(gap, 4), abs(((ob[0] + ob[2]) / 2) - ((bb[0] + bb[2]) / 2)))
        if best is None or key < best[0]:
            best = (key, num)
    # 邻近出现多个不同图号 → 归属不明，宁缺勿错
    return best[1] if best and len(found) == 1 else None


def apply_figure_verdicts(blocks: list[dict], verdicts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """U-15：把 `figure_region` 的 `exists` 判定变成可装配的合成 figure 块。

    **agent 只下判定；这里由 S7 确定性写入**（不改任何主引擎字符）：
      * 合成块 `block_id=vision-pNNNN-figK`，`figure_id=vNN`，`figure_number=None`（无印刷号）；
      * 题注从原图 children 摘走、挂到新块（`caption_reassigned`）；
      * `VISION-FIGURE` 元数据留给 `build_markdown` 落注释。
    返回 `(synth, reassigned, skipped)`，供 report.json 记录。
    """
    synth, reassigned, skipped = [], [], []
    seq_by_page: dict[int, int] = {}
    for v in verdicts or []:
        if v.get("review_type") != "figure_region" or v.get("verdict") != "exists":
            continue
        page = int(v.get("pdf_page") or 0)
        bb = v.get("bbox_norm") or v.get("bbox")
        if page <= 0 or not bb or len(bb) != 4:
            skipped.append({"item_id": v.get("item_id"), "why": "missing_page_or_bbox"})
            continue
        cap = (v.get("caption_hint")
               or (v.get("context") or {}).get("caption_hint") or "").strip()
        seq_by_page[page] = seq_by_page.get(page, 0) + 1
        n = seq_by_page[page]
        bid = f"vision-p{page:04d}-fig{n:02d}"
        fid = f"v{n:02d}"
        child = {"block_id": f"{bid}-caption", "engine": "vision", "page": page,
                 "order": 0, "kind": "caption", "role": "child", "parent_id": bid,
                 "source_field": "image_caption", "source_type": "caption",
                 "text_raw": cap, "text_canon": cap, "bbox_norm": list(bb),
                 "bbox_src": list(bb), "bbox_space": "normalized-0-1", "flags": []}
        meta = {"item_id": v.get("item_id"), "bbox": list(bb),
                "model": (v.get("judge") or {}).get("model"),
                "mode": (v.get("judge") or {}).get("mode"),
                "confidence": v.get("confidence")}
        b = {"block_id": bid, "engine": "vision", "page": page, "order": 10000 + n,
             "kind": "figure", "role": "primary", "parent_id": None,
             "source_field": "vision_figure", "source_type": "vision",
             "text_raw": cap, "text_canon": cap, "bbox_norm": list(bb),
             "bbox_src": list(bb), "bbox_space": "normalized-0-1", "flags": [],
             "figure_number": None, "figure_id": fid, "crop_margin_px": 0,
             "vision_figure": meta, "_children": [child]}
        blocks.append(b)
        # 题注重挂：只从“同一页的 figure 子块”里摘；不改任何 text_raw 字符
        old = None
        for o in blocks:
            if o is b or o.get("kind") != "figure" or o.get("page") != page:
                continue
            for k in o.get("_children") or []:
                if k.get("kind") == "caption" and (k.get("text_raw") or "").strip() == cap:
                    old = o
                    break
            if old is not None:
                break
        if old is not None and cap:
            before = list(old.get("_children") or [])
            old["_children"] = [k for k in before
                                if not (k.get("kind") == "caption"
                                        and (k.get("text_raw") or "").strip() == cap)]
            if len(old["_children"]) < len(before):
                reassigned.append({"page": page, "caption": cap,
                                   "from_block_id": old["block_id"],
                                   "to_block_id": bid, "item_id": v.get("item_id")})
        synth.append({"item_id": v.get("item_id"), "block_id": bid, "figure_id": fid,
                      "page": page, "bbox_norm": list(bb), "caption": cap,
                      "model": meta["model"], "confidence": meta["confidence"]})
    return synth, reassigned, skipped


def crop_figures(blocks, pdf_path, out_dir, dpi=300, margin_px=24, max_px=0) -> dict:
    """从 300dpi 渲染裁剪插图（SPEC §2.3：一律自己裁，不用引擎返回的图）。"""
    import numpy as np
    from PIL import Image
    os.makedirs(out_dir, exist_ok=True)
    doc = render.open_doc(pdf_path) if pdf_path and os.path.exists(pdf_path) else None
    made, cache = {}, {}
    used_names = set()
    for b in blocks:
        if b["kind"] != "figure" or not b.get("bbox_norm"):
            continue
        page = b["page"]
        if doc is not None and page not in cache:
            cache[page] = Image.fromarray(render.render_rgb(doc, page - 1, dpi))
        img = cache.get(page)
        if img is None:
            continue
        W, H = img.size
        bb = b["bbox_norm"]
        _m = int(b.get("crop_margin_px", margin_px))
        box = (max(0, int(bb[0] * W) - _m), max(0, int(bb[1] * H) - _m),
               min(W, int(bb[2] * W) + _m), min(H, int(bb[3] * H) + _m))
        text = " ".join([b.get("text_raw") or ""] + [k.get("text_raw") or "" for k in b.get("_children", [])])
        # U-15：figure_number 只放**印刷图号**；无印刷号时用 figure_id（xK/vNN）做文件名。
        if "figure_number" in b:
            num = b.get("figure_number")
        else:
            num = _figno_of(text) or _nearby_figno(b, blocks)
        fid = b.get("figure_id")
        base = num or fid or f"x{len(made) + 1}"
        file_label, k = base, 2
        while f"fig-p{page:04d}-{file_label}.png" in used_names:
            file_label = f"{base}-{k}"
            k += 1
        name = f"fig-p{page:04d}-{file_label}.png"
        used_names.add(name)
        crop = img.crop(box)
        # **2026-11-15（P1-7）**：宽图按 `max_px` 等比缩小（0 = 不缩）。
        # 为什么：300dpi 的宽图动辄 1300–1900 px，Markdown 的 `![]()` **无法指定显示宽度**，
        # 于是渲染出来比正文宽一大截、逼得"一图一行"（用户实测：1318 px 的结构图铺满整行）。
        _small = None
        if max_px and crop.width > int(max_px):
            _h = max(1, round(crop.height * int(max_px) / crop.width))
            _small = [crop.width, crop.height, int(max_px), _h]
            crop = crop.resize((int(max_px), _h), Image.LANCZOS)
        crop.save(os.path.join(out_dir, name))
        made[b["block_id"]] = {"file": f"figures/{name}", "page": page, "figure_number": num,
                               "figure_id": fid or (None if num and file_label == num else file_label),
                               "vision": bool(b.get("vision_figure")),
                               "box_px": list(box), "dpi": dpi,
                               "px": [crop.width, crop.height],
                               "downscaled_from": _small,
                               "sha256": hashlib.sha256(open(os.path.join(out_dir, name), "rb").read()).hexdigest()[:16]}
    if doc is not None:
        render.close(doc)
    return made


def _flat(s: str) -> str:
    """扁平化：去掉空白与 LaTeX 包装字符（`$`/`\\`/`{}`），只留可见字符。"""
    return re.sub(r"[\s$\\{}]+", "", s or "")


def derive_absorbed(blocks, insertions) -> dict:
    """**视觉插入会吸收被它覆盖的片段。**

    实测成因：第 3 页原生把「首先，温度 T₁ 不变」整句切成一个孤立的 `$$T_1$$` 公式块；
    视觉层补回整句后，那个残块若照常输出，就会与插入内容并排出现（实测"多一个 T₁"）。
    吸收条件（两个都要满足，缺一不可）：
      ① 块 bbox **落在**转写区域内；② 块的文字**已被转写内容包含**（扁平化后子串）。
    ② 是关键控制：同区域里的 `$$p_1V_1=p_2V'$$` 不会被吸收（它的文字不在那句转写里）。
    """
    absorbed: dict[str, dict] = {}
    for ins in (insertions or []):
        reg = ins.get("bbox_norm") or ins.get("bbox")
        ft = _flat(ins.get("transcription"))
        if not reg or len(reg) != 4 or len(ft) < 8:
            continue
        for b in blocks:
            if b.get("role") != "primary" or b.get("kind") in FURNITURE:
                continue
            bb = b.get("bbox_norm") or []
            if len(bb) != 4:
                continue
            if not (reg[0] <= bb[0] and reg[1] <= bb[1] and bb[2] <= reg[2] and bb[3] <= reg[3]):
                continue
            fb = _flat(b.get("text_raw"))
            if fb and fb in ft:
                absorbed[b["block_id"]] = {"block_id": b["block_id"], "page": b["page"],
                                           "text": (b.get("text_raw") or "")[:60],
                                           "item_id": ins.get("item_id")}
    return absorbed


def markdown_lint(md: str) -> list[str]:
    """轻量 G9 检查：公式定界符/脚注定界符配对、管道表列数一致。"""
    flags = []
    if md.count("$$") % 2:
        flags.append("unbalanced_display_math")
    inline = md.replace("$$", "")
    if inline.count("$") % 2:
        flags.append("unbalanced_inline_math")
    widths = {}
    for line in md.splitlines():
        if line.startswith("|") and line.endswith("|"):
            n = line.count("|")
            if "---" in line:
                widths["sep"] = n
            elif "header" not in widths:
                widths["header"] = n
            elif n != widths.get("header"):
                flags.append("table_row_width_mismatch")
                break
    if widths.get("header") and widths.get("sep") and widths["header"] != widths["sep"]:
        flags.append("table_separator_mismatch")
    return sorted(set(flags))


def _same_figure_row(a: dict, b: dict) -> bool:
    """U-15：两张图是否同一行（只用于**图块**，不碰正文）。"""
    A, B = a.get("bbox_norm"), b.get("bbox_norm")
    if not A or not B:
        return False
    wa, ha = A[2] - A[0], A[3] - A[1]
    wb, hb = B[2] - B[0], B[3] - B[1]
    if wa <= 0 or ha <= 0 or wb <= 0 or hb <= 0:
        return False
    # 横向必须基本不重叠（排除上下叠放 / inset）
    if min(A[2], B[2]) - max(A[0], B[0]) >= 0.1 * min(wa, wb):
        return False
    y0diff = abs(A[1] - B[1])
    yov = min(A[3], B[3]) - max(A[1], B[1])
    if y0diff > 0.06:
        return False
    # 同一行：纵向重叠 ≥30% 较小高度；或上沿几乎齐平（p4 顶部“高肖像 + 宽图”）
    return yov >= 0.3 * min(ha, hb) or y0diff <= 0.02


def load_block_flags(root: str, book: str) -> dict:
    """读 `s5-out/<书>/block-flags.jsonl`（判定者对手写/印章/装饰的**块级**标记，P1-1）。

    返回 `{block_id: {kind, basis, page}}`；文件不在/坏掉 → 空表（**降级**，不带走主流程）。
    """
    import json as _json
    p = os.path.join(root, "s5-out", str(book), "block-flags.jsonl")
    out: dict = {}
    if not os.path.exists(p):
        return out
    try:
        for line in open(p, encoding="utf-8"):
            if not line.strip():
                continue
            r = _json.loads(line)
            if r.get("block_id") and (r.get("kind") or "") == "non_book_ink":
                out[str(r["block_id"])] = r
    except Exception:                                              # noqa: BLE001
        return {}
    return out


def figure_row_runs(content: list[dict]) -> list[list[str]]:
    """把**连续的同排图块**分成组（2026-11-15 / P1-6）。

    为什么需要：每张图各自一段（`![](a)` 空行 `![](b)`）→ 渲染器必然**一图一行**；
    而书里同一排的四张结构式本来是并排的。这里只做"**同排 + 在内容里连续**"的合并：
      * 同排：沿用 `_same_figure_row()`（y 区间重叠 ≥50%）；
      * 连续：中间不能夹正文/题注块（跨栏或跨段不合并，避免把上下两排拍成一行）。
    返回 `[[block_id, …], …]`（只含 ≥2 张的组，按内容顺序）。
    """
    runs: list[list[str]] = []
    cur: list[dict] = []
    for b in content:
        if b.get("kind") != "figure" or not b.get("bbox_norm"):
            if len(cur) >= 2:
                runs.append([x["block_id"] for x in cur])
            cur = []
            continue
        if cur and all(_same_figure_row(b, x) for x in cur):
            cur.append(b)
            continue
        if len(cur) >= 2:
            runs.append([x["block_id"] for x in cur])
        cur = [b]
    if len(cur) >= 2:
        runs.append([x["block_id"] for x in cur])
    return runs


def reorder_figure_rows(content: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """U-15：把所有页的**同一行**图块按 x 从左到右重排；不动非图块。

    fail-safe：只交换**已有图块所在的位置**；题注若在图块 children 里会跟着图走；
    独立题注块不移动（记 `caption_unassigned` 由后续人工/校对表看）。
    """
    figs = [b for b in content if b.get("kind") == "figure" and b.get("bbox_norm")]
    by_page: dict[int, list[dict]] = {}
    for b in figs:
        by_page.setdefault(int(b.get("page") or 0), []).append(b)
    changes, ambiguous = [], []
    for page, arr in sorted(by_page.items()):
        arr = sorted(arr, key=lambda b: (b["bbox_norm"][1], b["bbox_norm"][0]))
        groups: list[list[dict]] = []
        for b in arr:
            for g in groups:
                if all(_same_figure_row(b, x) for x in g):
                    g.append(b)
                    break
            else:
                groups.append([b])
        for g in groups:
            if len(g) < 2:
                continue
            ids = {b["block_id"] for b in g}
            pos = [i for i, b in enumerate(content) if b.get("block_id") in ids]
            if len(pos) != len(g):
                ambiguous.append({"page": page, "blocks": sorted(ids),
                                  "why": "row_blocks_not_in_content"})
                continue
            # **只重排内容里连续的图块**：不连续 = 中间夹正文/题注，或分属不同栏
            # （p4 顶部的“高肖像 + 宽图”就是主栏+副栏，按 x 排会把肖像搬到页首）
            if pos != list(range(pos[0], pos[0] + len(pos))):
                ambiguous.append({"page": page, "blocks": sorted(ids),
                                  "why": "not_contiguous_or_cross_column"})
                continue
            before = [content[i]["block_id"] for i in pos]
            after = [b["block_id"] for b in sorted(g, key=lambda b: (b["bbox_norm"][0], b["bbox_norm"][1]))]
            if before == after:
                continue
            # 题注是否是独立块（不在任何图块 children 里）→ 记录，但仍只重排图块本身
            child_ids = {k.get("block_id") for b in g for k in (b.get("_children") or [])}
            for i, b in zip(pos, sorted(g, key=lambda b: (b["bbox_norm"][0], b["bbox_norm"][1]))):
                content[i] = b
            changes.append({"page": page, "before": before, "after": after,
                            "confidence": "x-order"})
            _ = child_ids  # 保留可读性；独立题注的检测留给 caption_unassigned（v1 未强判）
    return content, changes, ambiguous


def build_markdown(blocks, structure, questions, s4_hits, figures, chapter,
                   insertions=None, page_markers=None, verdict_by_item=None,
                   chapter_filter=None, skip_header=False, page_state=None,
                   layout=None) -> tuple[str, dict]:
    """装配一章的 Markdown。

    `chapter_filter`（C1）：`{block_id, …}` 集合——只输出这些块；**页级机制（页边界、页首 REVIEW、
    折叠备查块）属于该页第一次出现的那一章**（由调用方共用的 `page_state` 记录），因此"逐章拼接"
    与"一次装配"的**页标记集合完全相同**、正文去注释后**逐行相同**（两条都是可判定的不变量）。
    `skip_header`：非首章不再重复产物标题与说明。
    """
    content = [b for b in blocks if b.get("role") == "primary" and b["kind"] not in FURNITURE]
    # **栏序（A3）**：这里原先也按 `(页, y, x)` 排 —— 两栏书会被**再交错一遍**（S6 排对了也白搭，
    # 实测踩到：S6 的 sequence 已整栏先读，产物却仍是左右交替）。复用 S6 的同一份栏序逻辑，口径不分裂。
    import s6_assemble as _s6
    _decl = _s6.layout_map(layout) if layout else None
    content.sort(key=_s6.reading_order_key(content, _s6.page_columns(content, declared=_decl)))
    if chapter_filter is not None:
        content = [b for b in content if b["block_id"] in chapter_filter]
    content, _fro, _fra = reorder_figure_rows(content)
    _nb_flag_on = True            # P1-1：块级 `non_book_ink` 标记是否生效（默认生效）
    # P1-6：同排且连续的图块分组（排头 → 整组；排尾 → 跳过，避免重复输出）
    _runs = figure_row_runs(content)
    _row_head = {}
    _row_tail = set()
    for _r in _runs:
        _by = {b["block_id"]: b for b in content}
        _row_head[_r[0]] = [_by[x] for x in _r if x in _by]
        _row_tail |= set(_r[1:])
    joined = {j["block_to"] for j in structure.get("cross_page_joins", [])}
    reviews, lines = [], []
    stats = {"blocks": 0, "tables": 0, "figures": 0, "equations": 0, "footnotes": 0,
             "review_markers": 0, "folded_judged_markers": 0, "vision_insertions": 0, "cross_page_splits": 0,
            "non_book_ink": 0, "empty_block_covered": 0,
             "cross_page_split_sources": 0, "absorbed_by_transcription": 0, "table_flags": [],
             "vision_figures": 0, "figure_row_order": _fro, "figure_row_ambiguous": _fra}
    # 跨页切分（S6 的归属修正）：源块只保留到页边界；被移走的后半句放到目标页开头
    _splits = structure.get("cross_page_splits") or []
    keep_by_block = {s["from_block_id"]: s["keep"] for s in _splits}
    moved_by_page: dict[int, list[str]] = {}
    for _s in _splits:
        moved_by_page.setdefault(_s["to_page"], []).append(_s["moved_text"])
    # 被视觉插入吸收的片段（不再单独输出；见 derive_absorbed 的两个条件）
    absorbed = derive_absorbed(blocks, insertions)
    absorbed_list: list[dict] = []
    # 视觉插入（U-10 裁定：转写类结果**直接写入正文**）：按页按 y 排序，到位置就落笔
    ins_by_page: dict[int, list[dict]] = {}
    for ins in (insertions or []):
        ins_by_page.setdefault(ins["pdf_page"], []).append(ins)
    for p in ins_by_page:
        ins_by_page[p].sort(key=lambda r: (r.get("bbox") or [0, 0])[1])
    markers_by_page: dict[int, list[dict]] = {}
    for m in (page_markers or []):
        markers_by_page.setdefault(m["pdf_page"], []).append(m)
    inserted_keys: dict[int, set[str]] = {}
    # **B1：转写覆盖记账**（纯对账，不是新判据）。已经按图补过的区域，后面同区域的
    # `empty_block` / `page_end_truncation` 就**不再要求人看第二遍**——否则同一块地方一边
    # 正文里写着"已补"，一边在校对表里让你"把字读出来"（实测：p3 的 TR-R1-0003-01）。
    emitted_cover: dict[int, list[tuple]] = {}
    _COVER_MIN = float(TUNE.get("vision_cover_min_overlap", 0.5))

    def _cover_hit(page: int, bbox) -> dict | None:
        """这条块/标记是否已被**同一页的转写**覆盖：面积重合 ≥ 阈值，或块中心落在转写框内。"""
        bb = list(bbox or [])
        if len(bb) != 4:
            return None
        for cov, item in emitted_cover.get(page, []):
            if len(cov) != 4:
                continue
            cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
            inside = cov[0] <= cx <= cov[2] and cov[1] <= cy <= cov[3]
            iw = min(bb[2], cov[2]) - max(bb[0], cov[0])
            ih = min(bb[3], cov[3]) - max(bb[1], cov[1])
            frac = 0.0
            if iw > 0 and ih > 0:
                area = max(1e-9, (bb[2] - bb[0]) * (bb[3] - bb[1]))
                frac = (iw * ih) / area
            if frac >= _COVER_MIN or inside:
                return {"item": item, "frac": round(frac, 3)}
        return None

    def marker(cat, page, bbox=None, note="", verdict=None):
        stats["review_markers"] += 1
        # 注释里的文本要**清洗**：去掉 `$`（截断后的 LaTeX 可能只剩落单的 `$`，会污染数学区检查）
        note = re.sub(r"\s+", " ", (note or "").replace("$", "")).strip()
        r = {"category": cat, "page": page, "bbox": bbox, "note": note, "verdict": verdict}
        reviews.append(r)
        bb = f" bbox={','.join(f'{v:.3f}' for v in bbox)}" if bbox else ""
        vv = f" verdict={verdict}" if verdict else ""
        nn = f" {note}" if note else ""
        lines.append(f"<!-- REVIEW: {cat} page={page}{bb}{vv}{nn} -->")

    def _folded_block(items):
        """已判定条目**折叠成一行注释**：人只看真待办；要追溯时 item_id/结论/批次都在。"""
        return f"<!-- 已判定（备查·无需人工）：{len(items)} 条 → " + "；".join(items) + " -->"

    if not skip_header:
        lines.append(f"# 化学教材 Markdown 产物（{chapter if chapter != '00' else '未编号'}）")
        lines.append("")
        lines.append(f"> 由 S7 从 IR + S6 装配结果生成；页边界以 `<!-- pNNN -->` 标记；"
                     f"家具（页眉/页脚/页码）不进正文；未决项以 `<!-- REVIEW: … -->` 显式标出；"
                     f"视觉层**逐字转写**补入的内容以 `<!-- VISION-INSERT … -->` 标注（U-10 裁定）。")
    lines.append("")

    def _ins_y(ins: dict) -> float:
        """插入锚点的纵向位置。**必须与块坐标同空间**：优先归一化框（bbox_norm），
        否则像素框会被当成归一化值比较（实测踩过：插入永远落不到位置，全掉到页尾兜底）。"""
        bb = ins.get("bbox_norm") or ins.get("bbox") or [0, 0, 0, 0]
        return bb[1]

    def _emit_insertion(page: int, ins: dict) -> None:
        key = textnorm.norm_weak(ins["transcription"])
        if key in inserted_keys.get(page, set()):
            # 同一页多个转写任务命中同一句话（实测：R4a 与 R4b 指向同一处）→ 只插一次并记录
            reviews.append({"category": "duplicate_transcription_skipped", "page": page,
                            "bbox": ins.get("bbox_norm") or ins.get("bbox"),
                            "note": ins.get("item_id", "")})
            return
        inserted_keys.setdefault(page, set()).add(key)
        bb = ins.get("bbox_norm") or ins.get("bbox") or [0, 0, 0, 0]
        # B1：记下"已覆盖的框"，供同区域的 empty_block / 截断标记对账
        emitted_cover.setdefault(page, []).append((tuple(bb)[:4], ins.get("item_id") or ""))
        lines.append(ins["transcription"])
        lines.append(f"<!-- VISION-INSERT: page={page} bbox={','.join(f'{v:.3f}' for v in bb)} "
                     f"model={(ins.get('judge') or {}).get('model', '?')} "
                     f"conf={ins.get('confidence', 0):.2f} item={ins.get('item_id')} -->")
        lines.append("")
        stats["vision_insertions"] += 1
        reviews.append({"category": "vision_insertion", "page": page, "bbox": bb,
                        "note": ins["transcription"][:60]})

    def flush_insertions(page: int, upto_y: float) -> None:
        for ins in list(ins_by_page.get(page, [])):
            if _ins_y(ins) > upto_y:
                continue
            _emit_insertion(page, ins)
            ins_by_page[page].remove(ins)

    # 同文本多条转写任务时**取区域推导最精确的那条**（R4a 的行带 > R4b 的上方窗口 > R1 空块）：
    # 否则去重会留下"猜"的那条（实测：留下 R4b 会把句子插到上一段之前，位置偏一段）。
    _RULE_PRIORITY = {"R4a": 0, "R4b": 1, "R1": 2}

    def _rule_rank(ins: dict) -> int:
        iid = ins.get("item_id") or ""
        return _RULE_PRIORITY.get(iid.split("-")[1] if iid.startswith("TR-") else "", 3)

    for _p in ins_by_page:
        ins_by_page[_p].sort(key=lambda r: (_rule_rank(r), _ins_y(r)))

    # **同文本先按精度去重，再排位置**：落笔顺序是由 y 触发的，不是列表顺序，
    # 所以"先排位置再靠去重拦"必然留下 y 更小的那条（实测：留下 R4b，句子被插到上一段之前）。
    for _p, _lst in list(ins_by_page.items()):
        _best: dict[str, dict] = {}
        for _ins in _lst:
            _k = textnorm.norm_weak(_ins["transcription"])
            _cur = _best.get(_k)
            if _cur is None or _rule_rank(_ins) < _rule_rank(_cur):
                if _cur is not None:
                    reviews.append({"category": "duplicate_transcription_skipped", "page": _p,
                                    "bbox": _cur.get("bbox_norm") or _cur.get("bbox"),
                                    "note": _cur.get("item_id", "")})
                _best[_k] = _ins
            else:
                reviews.append({"category": "duplicate_transcription_skipped", "page": _p,
                                "bbox": _ins.get("bbox_norm") or _ins.get("bbox"),
                                "note": _ins.get("item_id", "")})
        ins_by_page[_p] = sorted(_best.values(), key=_ins_y)

    cur_page = None
    s3_by_page = {}
    folded: dict[int, list[str]] = {}
    # 2026-11-15：`missing_text`（主引擎漏、页面有）按页分组，页末留一条 REVIEW（见下）。
    # 数据源是**判定行**（含 `errata`），从 `verdict_by_item` 里取（兼容直接传 dict 的调用方）。
    mt_by_page: dict[int, list[dict]] = {}
    for _iid, _row in (verdict_by_item or {}).items():
        _e = (_row or {}).get("errata") or {}
        if isinstance(_e, dict) and str(_e.get("kind") or "") == "missing_text":
            _bb = _row.get("bbox_norm") or _row.get("bbox")
            _bb = list(_bb) if isinstance(_bb, (list, tuple)) and len(_bb) == 4 else None
            mt_by_page.setdefault(int(_row.get("pdf_page") or 0), []).append(
                {"item": _iid, "bbox": _bb})
    for q in questions or []:
        s3_by_page.setdefault(q["pdf_page"], []).append(q)
    # **2026-10-26：`non_book_ink` 隔离区**。判成"非本书内容"（透印/手写/印章/装饰）的块，
    # 不写正文、降级成 `<!-- NON-BOOK-INK … 原文: … -->` 注释（净版靠 G9 去注释自然不含）。
    # 只认**几何覆盖**（问题带的 bbox），且只作用于**非图块**——图块另有 U-15 的终态判定，
    # 这里静默丢图的风险不对称，宁可不碰。
    _nb_regions: list[tuple] = []
    for q in questions or []:
        _v = (verdict_by_item or {}).get(q.get("item_id")) or {}
        if str(_v.get("verdict") or "") == "non_book_ink":
            _bb = list(q.get("bbox_norm") or q.get("bbox") or [])
            if len(_bb) == 4:
                _nb_regions.append((int(q.get("pdf_page") or 0),
                                    [float(x) for x in _bb], q.get("item_id") or ""))

    def _nb_hit(page: int, bbox) -> dict | None:
        bb = list(bbox or [])
        if len(bb) != 4:
            return None
        for pg, cov, iid in _nb_regions:
            if pg != page:
                continue
            cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
            inside = cov[0] <= cx <= cov[2] and cov[1] <= cy <= cov[3]
            iw = min(bb[2], cov[2]) - max(bb[0], cov[0])
            ih = min(bb[3], cov[3]) - max(bb[1], cov[1])
            frac = 0.0
            if iw > 0 and ih > 0:
                area = max(1e-9, (bb[2] - bb[0]) * (bb[3] - bb[1]))
                frac = (iw * ih) / area
            if frac >= _COVER_MIN or inside:
                return {"item": iid, "frac": round(frac, 3)}
        return None
    for b in content:
        if b["page"] != cur_page:
            # **2026-11-15（P0-1/P0-2）**：`errata(kind=missing_text)` 的条目要在**它所属页的末尾**留一条
            # `REVIEW: missing_text` —— 这样它计入 `counts.human_facing_markers`（G19 口径不变），
            # S8 也会把它放进校对表第 1 节（"已判定、但产物里缺这段"），而不是折进"备查"。
            for _mt in mt_by_page.get(cur_page, []):
                marker("missing_text", cur_page, _mt.get("bbox"), "item=%s" % _mt["item"])
            # 收尾上一页的"已判定（备查）"折叠块（读者在正文旁只看到真待办）。
            # **A7-2：落笔位置要推迟到"知道这是不是句中接页"之后** —— 折叠块必须留在**上一页的段内**：
            #   ① 普通切页：`…上一页正文 / 折叠块 / <!-- pNNN --> / 本页正文`（折叠块在标记之前 ✓）；
            #   ② **句中接页**：标记要写进"上一页最后一条正文行"里（行内），若折叠块仍按①先落笔，
            #      它就会落到**标记之后**（= 被算成下一页的东西）→ G2 按页核对痕迹时找不到上一页的条目
            #      （真书实测：4 条 S3 判定被算成"静默丢失"）。故②时把它插到接句锚点**之前**。
            _fold_txt = _folded_block(folded[cur_page]) if folded.get(cur_page) else ""
            cur_page = b["page"]
            # C1：**页首机制（页标记 / 页首 REVIEW / 折叠块 / 视觉插入落笔 / 跨页后半句）属于"该页
            # 第一次出现在装配序列里"的那一章**。多章共用 `page_state` 时，后续章在某页中间续排
            # 就不再重复这套东西——这样"逐章拼接"与"一次装配"的**页标记集合完全相同**（可判定不变量）。
            if page_state is not None and page_state.get(cur_page):
                # 该页已由**前面那一章**开过头 → 只跳过页首机制，**当前块照常输出**（否则会丢块）
                pass
            else:
                # `_prev` 必须在**写入本页页标记之前**取：否则"上一行"会变成刚写下的页标记，
                # 于是 `_join` 误判（页标记以 `>` 结尾、不在句末集合里）→ 页标记被写两次（实测踩到）。
                # **A7-2**：`_prev` 还必须**跳过注释行**——上一页的"已判定（备查）"折叠块/REVIEW 标记
                # 都是 `<!-- … -->` 行，`rstrip()[-1]` 是 `>`（不在句末集合里）→ 会被误当成"句中接句锚点"，
                # 于是**页标记与后半句一起被粘进注释行**、按行切页看不见这条边界（真书 p8/p26/p34 实测）。
                _prev = next((i for i in range(len(lines) - 1, -1, -1)
                              if lines[i].strip() and not lines[i].lstrip().startswith("<!--")), None)
                _moved = moved_by_page.get(cur_page, [])
                # 跨页切分：若上一页末尾**不是句末**（词/句中间被页边界切开），把后半句**接成同一段**，
                # 页标记改为行内 —— 否则渲染时会硬断成两段（实测 p22–23、p23–24 两处）。
                _join = bool(_moved and _prev is not None and lines[_prev].strip()
                             and lines[_prev].rstrip()[-1] not in "。！？；：.!?;:)）】")
                if page_state is not None:
                    page_state[cur_page] = True          # 页首机制只由"第一次出现该页的那一章"负责
                if _join:
                    # **A7-2**：句中接页时，标记要写进"上一页最后一条正文行"里；折叠块必须留在
                    # **上一页段内** → 插到锚点之前（否则它落到标记之后 = 被算成下一页的东西，
                    # G2 按页核对痕迹时找不到上一页的条目：真书实测 4 条被判"静默丢失"）。
                    if _fold_txt:
                        lines.insert(_prev, "")
                        lines.insert(_prev, _fold_txt)
                        _prev += 2
                    lines[_prev] = lines[_prev].rstrip() + f"<!-- p{cur_page:03d} -->" + _moved[0]
                    lines.append("<!-- SPLIT-JOIN: 与上页同段（页边界在词/句中间） -->")
                    lines.append("")
                    stats["cross_page_splits"] += 1
                    _rest = _moved[1:]
                else:
                    if _fold_txt:                        # 普通切页：折叠块在页标记**之前**落笔（A7-2 起两条路径统一）
                        lines.append("")
                        lines.append(_fold_txt)
                    lines.append(f"<!-- p{cur_page:03d} -->")
                    lines.append("")
                    _rest = _moved
                # S3 问题：**未判定的留在页首**（人必须看到）；**已判定的折叠到页尾一个备查块**
                # （用户裁定：读者看到的数量应 = 真待办数；结论/批次仍可追溯）
                for q in s3_by_page.get(cur_page, []):
                    _v = (verdict_by_item or {}).get(q.get("item_id")) or {}
                    if _v.get("verdict"):
                        folded.setdefault(cur_page, []).append(
                            f"{q['item_id']}={_v['verdict']}@{_v.get('arm') or '?'}")
                        stats["folded_judged_markers"] += 1
                        stats["review_markers"] += 1
                        continue
                    marker(f"S3_{q['review_type']}", q["pdf_page"], q.get("bbox"), "")
                # 转写任务未解决 / S4 命中未覆盖 → 同样在页首显式标记（绝不静默）
                for m in markers_by_page.get(cur_page, []):
                    marker(m["category"], cur_page, m.get("bbox"), (m.get("note") or "")[:40])
                for _mv in _rest:
                    lines.append(_mv)
                    lines.append("<!-- joined from previous page（跨页切分：该句原本被合并到上一页） -->")
                    lines.append("")
                    stats["cross_page_splits"] += 1
        flush_insertions(cur_page, (b.get("bbox_norm") or [0, 0, 0, 0])[1])
        stats["blocks"] += 1
        kind, raw = b["kind"], (b.get("text_raw") or "").strip()
        if b["block_id"] in keep_by_block:
            raw = keep_by_block[b["block_id"]]          # 只保留到页边界（后半句已还给下一页）
            stats["cross_page_split_sources"] += 1
        # **2026-10-26：非本书内容 → 降级为注释**（不改写、不删除；净版靠 G9 去注释自然不含）
        if kind != "figure":
            _nb = _nb_hit(b["page"], b.get("bbox_norm"))
            if _nb:
                _orig = re.sub(r"\s+", " ", raw or "").strip()[:120]
                lines.append("<!-- NON-BOOK-INK: page=%d bbox=%s item=%s 原文: %s -->"
                             % (b["page"],
                                ",".join(f"{v:.3f}" for v in (b.get("bbox_norm") or [0, 0, 0, 0])),
                                _nb["item"], _orig))
                lines.append("")
                stats["non_book_ink"] = stats.get("non_book_ink", 0) + 1
                continue
        if b["block_id"] in absorbed:
            # 该片段的文字已被视觉转写的整句覆盖 → 不再单独输出（留审计痕迹，绝不静默）
            a = absorbed[b["block_id"]]
            lines.append(f"<!-- VISION-ABSORBED: block={a['block_id']} page={a['page']} "
                         f"item={a['item_id']} reason=该片段已被视觉转写的整句覆盖 -->")
            lines.append("")
            stats["absorbed_by_transcription"] += 1
            absorbed_list.append(a)
            continue
        # 页末截断且未拼接 → 标 REVIEW（U-14：拼接过的就不再报；B1：已被转写覆盖的也不再报）
        if any(h.get("rule") == "R4c" and h.get("block_id") == b["block_id"] for h in s4_hits):
            _cov_t = _cover_hit(b["page"], b.get("bbox_norm"))
            if _cov_t:
                folded.setdefault(cur_page, []).append(
                    f"{_cov_t['item']}=transcribed@S7（页末截断已补）")
                stats["folded_judged_markers"] += 1
                stats["review_markers"] += 1
            elif b["block_id"] in keep_by_block:
                # 2026-10-26（2.3）：这段的字已被跨页切分接上（后半句在下一页）→ 不再报"页末截断"
                lines.append("<!-- TRUNCATION-COVERED: block=%s page=%d reason=跨页切分已接上 -->"
                             % (b["block_id"], b["page"]))
            else:
                marker("page_end_truncation", b["page"], b.get("bbox_norm"), raw[-20:])
        if b["block_id"] in joined:
            lines.append(f"<!-- joined from previous page -->")
        if kind == "heading":
            lvl = next((h["level"] for h in structure.get("heading_tree", [])
                        if h["text"] and h["text"] in raw), 2)
            susp = next((h for h in structure.get("heading_tree", [])
                         if h["text"] and h["text"] in raw and h.get("suspect_list_item")), None)
            if susp:
                marker("suspect_list_item_heading", b["page"], b.get("bbox_norm"), raw[:30])
            lines.append(f"{'#' * max(1, min(6, lvl))} {raw}")
        elif kind == "equation":
            stats["equations"] += 1
            lines.append(clean_equation(raw))
        elif kind == "table_body" or kind == "table":
            html = ""
            for k in [b] + b.get("_children", []):
                if k.get("source_field") == "table_body" or k["kind"] == "table_body":
                    html = k.get("text_raw") or html
            caps = [k.get("text_raw") or "" for k in b.get("_children", [])
                    if k.get("kind") == "caption"]
            foots = [k.get("text_raw") or "" for k in b.get("_children", [])
                     if k.get("kind") == "footnote"]
            for c in caps:
                lines.append(c)
            gfm, flags = html_table_to_gfm(html)
            stats["tables"] += 1
            stats["table_flags"] += flags
            if flags:
                marker("table_structure_flag", b["page"], b.get("bbox_norm"), ",".join(flags))
            lines.append(gfm or "<!-- REVIEW: table_unparsed -->")
            for f in foots:
                lines.append(f)
        elif b.get("_nb_flag") and _nb_flag_on and (b["_nb_flag"].get("mode") or "quarantine") == "quarantine":
            # **P1-1（A）**：判定者对这个块下了 `non_book_ink`（手写/印章/装饰/透印）→
            # 不写正文（净版靠 G9 去注释自然不含），留一行审计痕迹；图块也一样（不引用图）。
            _flag = b["_nb_flag"]
            _raw = (b.get("text_raw") or "").strip().replace("\n", " ")
            _raw = _raw or "（图/无文字）"
            lines.append("<!-- NON-BOOK-INK: block=%s page=%d 原文: %s（依据：%s） -->"
                         % (b["block_id"], b["page"], _raw[:120], _flag.get("basis") or "—"))
            stats["non_book_ink"] += 1
            lines.append("")
            continue
        elif kind == "figure":
            # **2026-11-15（P1-6）：同排的连续图块 → 合并成一个段落**（渲染器才会并排显示；
            # 一图一段的话必然是"一图一行"，书里四张并排的结构式就被拆成四行）。
            if b["block_id"] in _row_tail:
                lines.append("")            # 本排已由"排头"输出过 → 跳过（保持块间空行节奏）
                continue
            _run = _row_head.get(b["block_id"]) or [b]
            _imgs, _cmts, _caps, _foots = [], [], [], []
            for _bb in _run:
                _fig = figures.get(_bb["block_id"])
                _alt = (_bb.get("text_raw") or "插图").strip().replace("\n", " ")[:60] or "插图"
                if _fig:
                    stats["figures"] += 1
                    _imgs.append(f"![{_alt}]({_fig['file']})")
                else:
                    marker("figure_missing_crop", _bb["page"], _bb.get("bbox_norm"), _alt)
                _vf = _bb.get("vision_figure")
                if _vf:
                    _vb = _vf.get("bbox") or _bb.get("bbox_norm") or [0, 0, 0, 0]
                    _cmts.append("<!-- VISION-FIGURE page=%d bbox=%s item=%s model=%s conf=%s -->"
                                 % (_bb["page"], ",".join(f"{v:.3f}" for v in _vb),
                                    _vf.get("item_id") or "", _vf.get("model") or "?",
                                    _vf.get("confidence") if _vf.get("confidence") is not None else "?"))
                    stats["vision_figures"] += 1
                _caps += [k.get("text_raw") or "" for k in _bb.get("_children", [])
                          if k.get("kind") == "caption"
                          and k.get("source_field", "").endswith("caption")]
                _foots += [k.get("text_raw") or "" for k in _bb.get("_children", [])
                           if k.get("kind") == "footnote"
                           or k.get("source_field", "").endswith("footnote")]
            if _imgs:
                # **一行多图**：同一段落用空格分隔（不加空行）→ 并排显示、窗口窄时自动折行
                lines.append(" ".join(_imgs))
            lines += _cmts
            for c in _caps:
                lines.append(c)
            for f_ in _foots:
                if f_.strip():
                    lines.append(f_)
                    stats["figure_footnotes"] = stats.get("figure_footnotes", 0) + 1
        elif kind == "footnote":
            # **页下注直接按页底位置显示，不用 Markdown 脚注语法**（用户裁定：`[^n]:` 在部分阅读器里不显示）。
            # 脚注块本身就在该页底部，按阅读顺序输出就落在页底位置；来源与是否带标记用注释留审计。
            stats["footnotes"] += 1
            m = next((i + 1 for i, ch in enumerate(CIRCLED) if ch in raw), None)
            lines.append(raw)
            lines.append(f"<!-- page-footnote: page={b['page']} source={b.get('engine', '?')}"
                         f"{'' if m else ' unmarked=1'}（源页底部注释；按普通行显示，避免部分阅读器不解析脚注语法） -->")
        else:
            txt = raw if raw else (b.get("_eff") or "")
            if txt:
                lines.append(txt)
            else:
                _cov = _cover_hit(b["page"], b.get("bbox_norm"))
                if _cov:
                    # B1：这块**已经按图补过**（正文里已有 VISION-INSERT）→ 记进"已判定（备查）"，
                    # 不再让人"对着图把字读出来"（纯对账；计数与 S3 折叠同一套）
                    folded.setdefault(cur_page, []).append(f"{_cov['item']}=transcribed@S7")
                    stats["folded_judged_markers"] += 1
                    stats["review_markers"] += 1
                elif b["block_id"] in keep_by_block or b["block_id"] in joined:
                    # **2026-10-26（2.3）**：这块的字已被**跨页切分/接句**搬到上一页或下一页 →
                    # 空块是"搬家造成的"，不是"页上有墨迹没读出来"。留一行审计痕迹，**不发 REVIEW**
                    # （否则校对表里会出现"对着图把字读出来"，而那句话其实在隔壁页）。
                    lines.append("<!-- EMPTY-BLOCK-COVERED: block=%s page=%d reason=跨页切分（字在相邻页） -->"
                                 % (b["block_id"], b["page"]))
                    stats["empty_block_covered"] += 1
                else:
                    marker("empty_block", b["page"], b.get("bbox_norm"), "")
        _mk = b.get("_nb_flag") or {}
        if _mk and (_mk.get("mode") or "quarantine") == "mark":
            # **mark 模式**：块里混有印刷正文 → **不删正文**，只在块后留一行标记（宁多勿少）
            lines.append("<!-- NON-BOOK-INK-MARK: block=%s page=%d（含印刷内容，未移除；依据：%s） -->"
                         % (b["block_id"], b["page"], _mk.get("basis") or "—"))
            stats["non_book_ink_marked"] = stats.get("non_book_ink_marked", 0) + 1
        lines.append("")
    # 页尾仍未落笔的插入（y 超出该页所有块）→ 收尾时补上（同样走去重）
    for p, rest in ins_by_page.items():
        for ins in rest:
            _emit_insertion(p, ins)
    # 收尾最后一页的"已判定（备查）"折叠块
    if folded.get(cur_page):
        lines.append("")
        lines.append(_folded_block(folded[cur_page]))
    # 2026-11-15：最后一页的 `missing_text` 也要留痕（否则末页缺口不计入 G19）
    for _mt in mt_by_page.get(cur_page, []):
        marker("missing_text", cur_page, _mt.get("bbox"), "item=%s" % _mt["item"])
    # **给人看的标记数**（D6 口径：预算只约束"需要人看"的条数）= 在位的标记数
    stats["human_facing_markers"] = stats["review_markers"] - stats["folded_judged_markers"]
    return "\n".join(lines).rstrip() + "\n", {"reviews": reviews, "absorbed": absorbed_list, **stats}


def strip_comments(md: str) -> str:
    """**干净版 = 留底版去掉 HTML 注释**（只动标记，正文一字不改）；顺带收敛多余空行。

    G9 会用同一个函数重算一遍并比对 → 保证"干净版确由留底版派生"（防手改、防两版漂移）。
    """
    s = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s)


def build_entities(blocks) -> list[dict]:
    """U-02 默认口径：**单文件、每块一条、别名先只用规则**（LLM 只留给"中文名↔化学式"且本轮不启用）。"""
    rows = []
    for b in blocks:
        if b.get("role") != "primary" or b["kind"] in FURNITURE:
            continue
        t = b.get("text_raw") or ""
        if not t.strip():
            continue
        norm = textnorm.norm_weak(t)
        aliases = sorted({norm, norm.replace(" ", ""), textnorm.unwrap_commands(norm)} - {""})
        rows.append({"page": b["page"], "block_id": b["block_id"], "kind": b["kind"],
                     "text_sha": sha256_text(t), "text": t[:80],
                     "aliases": aliases[:6], "entities": [],
                     "latex": re.findall(r"\$[^$]{1,60}\$", t)[:4],
                     "confidence": 1.0, "provenance": f"{b['engine']}:{b['source_field']}"})
    return rows


def _norm_ws(s: str) -> str:
    return re.sub(r"[ \t\r\n\u3000]+", " ", (s or "").strip())


_FOLD_PAIRS = (("\u2212", "-"), ("\u2013", "-"), ("\u2014", "-"), ("\u2010", "-"),
               ("\u2011", "-"), ("\u2018", "'"), ("\u2019", "'"),
               ("\u201c", '"'), ("\u201d", '"'))
_WS = " \t\r\n\u3000"


def _fold_map(text: str) -> tuple[str, list]:
    """**只用于定位**的宽松化 + `折叠下标 → 原文下标` 映射。

    折叠内容：空白串 → 单个空格；破折号/引号归一。正文一个字都不改（只用来找位置）。
    为什么必须**带映射**：定位到折叠串后要回到**原文**取锚点，否则取出来的是折叠过的文本、
    与 md 对不上（踩过：`_fold` 折叠空白、`_fold_with_map` 不折叠 → 下标错位、永远定位失败）。
    """
    out, idx = [], []
    i, n = 0, len(text or "")
    while i < n:
        ch = text[i]
        if ch in _WS:
            j = i
            while j < n and text[j] in _WS:
                j += 1
            out.append(" "); idx.append(i); i = j
            continue
        rep = ch
        for a, b in _FOLD_PAIRS:
            if ch == a:
                rep = b
                break
        out.append(rep); idx.append(i); i += 1
    return "".join(out), idx


def _fold(s: str) -> str:
    return _fold_map(s)[0]


def refresh_missing_anchors(rows: list[dict], blocks: list[dict], md_text: str,
                            tail: int = 35, spans: dict | None = None) -> list[dict]:
    """**锚点保鲜**（2026-11-15 / 1c）：每次构建都确认锚点对得上**当前**正文，必要时按见证片段重定位。

    判定时的 `anchor.before/after` 是从**当时那份正文**摘的引文；上游一改（S6 切分/合并、S7 归一化），
    引文可能过期 → 读者侧 `errata_apply` 只能拒绝。这里分三步：

      ① **原锚点还有效**（宽松化后都能在当前 md 里找到）→ `source=stored_valid`，原样保留；
      ② 失效 → 用**见证片段**（`paddle_span` 里一定含那段字）定位缺口：取片段里缺口之前的 40 字去
         md 里找（破折号/引号折叠后匹配）→ 在**md 原文**上重取"前文结尾 / 后文开头"→ `source=recomputed`；
      ③ 都失败 → 保留存档锚点、`source=stale`（读者侧会拒绝并报"锚点不再相邻"，不会乱插）。

    锚点**永远取自 md 原文**（不在正文里凭空造字）；`inline` 不重算（没有块内偏移量可用）。
    """
    out = []
    md_fold, md_map = _fold_map(md_text or "")
    md_norm = _norm_ws(md_text)
    for r in rows or []:
        r = dict(r)
        an = dict(r.get("anchor") or {})
        an.setdefault("source", "stored")
        r["anchor"] = an
        if ((r.get("kind") or "") != "missing_text"):
            out.append(r)
            continue
        _b, _a = _norm_ws(an.get("before")), _norm_ws(an.get("after"))
        # ① 原锚点仍有效？
        if _b and _a and _fold(_b) in md_fold and _fold(_a) in md_fold:
            an["source"] = "stored_valid"
            out.append(r)
            continue
        if (an.get("position") or "") != "own_line":
            an["source"] = "stale"
            out.append(r)
            continue
        # ② 用见证片段重定位（缺口就是片段里 missing_text 所在位置）
        _sp = (spans or {}).get(r.get("item_id")) or ""
        _miss = _norm_ws(r.get("missing_text"))
        _hit = None
        if _sp and _miss:
            _spf, _ = _fold_map(_sp)
            _k = _spf.find(_fold(_miss))
            if _k >= 0:
                _anchor_key = _spf[max(0, _k - 40):_k]
                if _anchor_key.strip():
                    _p = md_fold.find(_anchor_key)
                    if _p >= 0:
                        # 取**最后一个匹配字符**的原文下标 +1（不要跨过它，否则会把下一个字也吞进来）
                        _raw_end = md_map[min(_p + len(_anchor_key) - 1, len(md_map) - 1)] + 1
                        _before_raw = md_text[: _raw_end].rstrip()
                        _after_raw = md_text[_raw_end:].lstrip()
                        _before = _norm_ws(_before_raw)[-tail:]
                        _after = _norm_ws(_after_raw)[:tail]
                        if _before and _after:
                            _hit = (_before, _after)
        if _hit:
            an["before"], an["after"] = _hit
            an["source"] = "recomputed"
            r["anchor"] = an
            out.append(r)
            continue
        # ③ bbox 邻居兜底（题目区域很大时可能取不到，失败就如实标 stale）
        bb = r.get("bbox") or an.get("bbox")
        page = r.get("page")
        if isinstance(bb, (list, tuple)) and len(bb) == 4 and page:
            paras = [b for b in (blocks or [])
                     if b.get("page") == page and b.get("kind") == "paragraph"
                     and (b.get("text_raw") or "").strip()
                     and isinstance(b.get("bbox_norm"), (list, tuple)) and len(b["bbox_norm"]) == 4]
            above = [b for b in paras if b["bbox_norm"][3] <= bb[1] + 1e-6]
            below = [b for b in paras if b["bbox_norm"][1] >= bb[3] - 1e-6]
            if above and below:
                up = max(above, key=lambda b: b["bbox_norm"][3])
                dn = min(below, key=lambda b: b["bbox_norm"][1])
                _nb = _norm_ws(up.get("text_raw"))[-tail:]
                _na = _norm_ws(dn.get("text_raw"))[:tail]
                if _nb and _na and _fold(_nb) in md_fold and _fold(_na) in md_fold:
                    an["before"], an["after"] = _nb, _na
                    an["source"] = "recomputed_bbox"
                    r["anchor"] = an
                    out.append(r)
                    continue
        an["source"] = "stale"
        out.append(r)
    return out


def cmd_run(args) -> int:
    import s3_compare as s3mod
    blocks = s3mod.load_ir(args.ir)
    s3mod.attach_children(blocks)          # 原地补齐 _children / _bbox_eff（返回 None，别链式调用）
    structure = json.load(open(args.structure, encoding="utf-8"))
    # P1-1：块级标记（判定者对手写/印章/装饰的直接结论）→ 挂到块上，build_markdown 据此降级
    # 产物根本：优先 `--out-root`；否则由 `--out`（s7-out/_staging/<书>）上溯两级
    _flag_root = (getattr(args, "out_root", "") or
                  os.path.dirname(os.path.dirname(os.path.abspath(args.out))))
    try:
        _flags = load_block_flags(_flag_root, args.book)
    except Exception:                                              # noqa: BLE001
        _flags = {}
    for _b in blocks:
        if _b.get("block_id") in _flags:
            _b["_nb_flag"] = _flags[_b["block_id"]]
    questions = s3mod.load_ir(args.questions) if args.questions else []
    s4_hits = s3mod.load_ir(args.s4_hits) if args.s4_hits else []
    verdicts = s3mod.load_ir(args.verdicts) if getattr(args, "verdicts", None) else []
    # **过期判定不生效**（与校验器 `binding.py` 同源口径）：产物**不得**把"绑定已不成立"的判定
    # 标成"已判定·无需人工"。否则 G2 会因为"产物声称已判定、校验器认为是未判定"而报红。
    try:
        import binding as _bd
        # **产物根必须由调用方给**：产物写在 `s7-out/_staging/<书>` 下，靠 `dirname(dirname(out))` 会算成
        # `s7-out`（不是产物根）→ `binding.current()` 读不到源 PDF/引擎产物/IR → **每条判定都被判过期**
        # → 转写不插入、REVIEW 痕迹不写、门禁与产物互相打架（实测踩到）。
        _broot = getattr(args, "out_root", None) or os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(args.out)))))
        # **必须逐条按它自己那一页算**（`binding.status_for_verdict` 的口径，也是判定落盘时用的口径）：
        # 单卷书里 `current(key, root)`（不传页）走的是**全书**指纹，与判定存的**逐卷**指纹永远不等
        # → 61 条会整片被判 "expired"（实测踩到：G21 报 `expired_counted_as_decided` × 61）。
        _kept, _expired_ids = [], []
        for _v in verdicts:
            if _bd.status_for_verdict(_v, args.book, _broot) == "expired":
                _expired_ids.append(_v.get("item_id"))
            else:
                _kept.append(_v)
        verdicts, expired_ids = _kept, _expired_ids
    except Exception:                                             # noqa: BLE001
        expired_ids = []
    # U-15：把 `figure_region` 的 `exists` 判定变成合成 figure 块（题注重挂也在这里做）。
    # **必须在切章之前**：合成块要进入 chapters[] 的 filter 与 build_markdown 的 content。
    _synth_figs, _cap_reassigned, _fig_skipped = apply_figure_verdicts(blocks, verdicts)
    # C1（SPEC §3.8）：从 IR 的 heading 树切章。**单章时逐字节等价于旧行为**（回归基线）；
    # 多章时每章一对 `-reviewed` / `-clean`，判决/errata/报告仍是**书级一份**（不按章拆包）。
    _book_bl = [b for b in blocks if b.get("role") == "primary" and b["kind"] not in FURNITURE]
    # **章锚（SPEC §3.8）**：显式 `--anchors` 优先；没有时按 `--book` 从注册表回退读取
    # （独立跑 s7 也能吃到 `books.json.chapter_anchors`；注册表不存在 → 当作未声明，行为不变）。
    _anchors = getattr(args, "anchors", None)
    _layout = getattr(args, "layout", None)
    _toc = getattr(args, "toc", None)
    if _anchors in (None, "") or not _layout or _toc in (None, ""):
        try:
            import books as _books_mod
            _cfg = next((_b for _b in (_books_mod.load() or {}).values()
                         if _b.get("key") == args.book), {})
            if _anchors in (None, ""):
                _anchors = _cfg.get("chapter_anchors")
            if not _layout:
                _layout = _cfg.get("layout")
            if _toc in (None, ""):
                _toc = _cfg.get("toc_pages")
        except Exception:                                          # noqa: BLE001
            pass
    # 页号口径（SPEC §3.8/§3.16）：`--anchors` / `--layout` / `--toc` / 注册表里都是**源 PDF 物理页号**；
    # 切章/排序吃的是**内容页号**（切片页），这里用 `structure.page_labels` 换算。
    _labels = (structure or {}).get("page_labels")
    _anchors = chapters_mod.anchors_to_page_space(_anchors, _labels)
    _layout = chapters_mod.layout_to_page_space(_layout, _labels)
    _toc = chapters_mod.anchors_to_page_space(_toc, _labels)
    _chap = chapters_mod.split_chapters(structure, blocks,
                                        input_kind=getattr(args, "input_kind", "") or "",
                                        anchors=_anchors, toc_pages=_toc)
    _plan = [{"stem": cc["file_stem"],
              # 章号标签必须与**文件名前缀**一致：单章时 `chapter_no` 为 None，但文件是 `chapter-01`
              # （早先写死 "00" → 报告里的章号与文件名自相矛盾，被 G20 判据⑤误报为"章前内容没单独成篇"）
              "chapter": (f"{cc['chapter_no']:02d}" if cc["chapter_no"] is not None
                          else (cc["file_stem"].split("-")[1]
                                if cc["file_stem"].startswith("chapter-")
                                and len(cc["file_stem"].split("-")) > 1
                                and cc["file_stem"].split("-")[1].isdigit() else "00")),
              "filter": {b["block_id"] for b in cc["blocks"]},
              "title": cc["title"], "blocks": cc["blocks"], "pages": cc["pages"],
              "rule": cc["rule"], "boundaries": cc["boundaries"]}
             for cc in _chap["chapters"]]
    chapter = _plan[0]["chapter"]                       # 兼容字段：首个章的章号（旧行为：唯一章）
    book_dir = os.path.join(args.out, args.book)
    os.makedirs(book_dir, exist_ok=True)
    # 2026-11-15（P1-7）：宽图按 `tuning.figure_max_px` 等比缩小（`![]()` 无法指定显示宽度）
    figures = crop_figures(blocks, args.pdf, os.path.join(book_dir, "figures"),
                           max_px=int(TUNE.get("figure_max_px", 900) or 0))
    # U-10 裁定：转写类判定**直接写入正文**；未解决的转写任务与未被覆盖的 S4 命中都要显形
    insertions = [v for v in verdicts if v.get("verdict") == "transcribed" and v.get("transcription")]
    # **已被"跨页切分"消费的转写不再插入**：切分已经把那段文字搬回后一页，
    # 若再插入一次，同一段会被写两遍（实测：已发布产物里那句出现过两次）。
    _split_items = {s.get("item_id") for s in (structure.get("cross_page_splits") or [])
                    if s.get("item_id")}
    _consumed = [v.get("item_id") for v in insertions if v.get("item_id") in _split_items]
    insertions = [v for v in insertions if v.get("item_id") not in _split_items]
    # 同文本多条 → 取**区域推导最精确**的那条（R4a 行带 > R4b 上方窗口 > R1 空块）：显式排序并记进报告
    _RANK = {"R4a": 0, "R4b": 1, "R1": 2}
    insertions.sort(key=lambda v: (
        _RANK.get((v.get("item_id") or "").split("-")[1]
                  if (v.get("item_id") or "").startswith("TR-") else "", 3),
        (v.get("bbox_norm") or v.get("bbox") or [0, 0, 0, 0])[1]))
    insertion_order = [v.get("item_id") for v in insertions]
    page_markers = []
    for v in verdicts:
        if v.get("review_type") == "transcribe_region" and v.get("verdict") not in ("transcribed", "no_text"):
            page_markers.append({"pdf_page": v["pdf_page"], "category": "transcribe_unresolved",
                                 "bbox": v.get("bbox"),
                                 "note": f"{v['item_id']} verdict={v.get('verdict')}"})
    covered = set()
    for v in verdicts:
        iid = v.get("item_id") or ""
        if iid.startswith("TR-"):
            parts = iid.split("-")
            covered.add((parts[1], v.get("pdf_page")))
    for h in s4_hits:
        if h.get("rule") in ("R4a", "R4b", "R1") and (h["rule"], h.get("page")) not in covered:
            page_markers.append({"pdf_page": h.get("page") or 1,
                                 "category": f"s4_unresolved_{h['rule']}",
                                 "bbox": h.get("bbox_norm"), "note": (h.get("note") or "")[:40]})
    _verdict_by_item = {v.get("item_id"): v for v in verdicts if v.get("item_id")}

    def _normalize_md(text: str) -> str:
        """S7 的两条"块结构"后处理：行间公式独占块 + GFM 表格前补空行（G16-①）。

        「正文: $$…$$」必须拆成两行（否则严格渲染器不认 display math、分块器可能从中间切开）；
        表格前必须有空行（实测表题紧贴表头时 markdown-it 仍渲染，但 **cmark-gfm/GitHub 不认**）。
        """
        text = re.sub(r"(?m)^([^\n$]*?[^\s$])[ \t]*(\$\$[^\n]*\$\$)[ \t]*$", r"\1\n\n\2", text)
        _ls, _o = text.split("\n"), []
        for _l in _ls:
            _s = _l.strip()
            _tbl_start = (_s.startswith("|") and _s.endswith("|")
                          and not (_o and _o[-1].strip().startswith("|")))
            if _tbl_start and _o and _o[-1].strip():
                _o.append("")
            _o.append(_l)
        return "\n".join(_o)

    # 全书一次装配（多章时**只用于验证**"逐章拼接 == 一次装配"，不落盘）
    md, mstats = build_markdown(blocks, structure, questions, s4_hits, figures, chapter,
                                insertions=insertions, page_markers=page_markers,
                                verdict_by_item=_verdict_by_item, layout=_layout)
    md = _normalize_md(md)
    md_parts, chapters_meta, clean_infos = [], [], []
    # 全书共用一份"页已开过头"的状态：页首机制只在该页第一次出现的那一章里跑
    _page_state: dict = {}
    for _ci, _pl in enumerate(_plan):
        _t, _st = build_markdown(blocks, structure, questions, s4_hits, figures, _pl["chapter"],
                                 insertions=insertions, page_markers=page_markers,
                                 verdict_by_item=_verdict_by_item,
                                 chapter_filter=_pl["filter"],
                                 skip_header=(_ci > 0), page_state=_page_state,
                                 layout=_layout)
        _t = _normalize_md(_t)
        # **双版本产物**（用户裁定）：`-reviewed` = 留底/真身（带标记与审计痕迹）；
        # `-clean` = **确定性派生**（**只去 HTML 注释，正文一个字节不动**）→ 由 G9 逐章校验
        _rpath = os.path.join(book_dir, f"{_pl['stem']}-reviewed.md")
        with open(_rpath, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_t)
        _clean = strip_comments(_t)
        _cpath = os.path.join(book_dir, f"{_pl['stem']}-clean.md")
        with open(_cpath, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_clean)
        md_parts.append(_t)
        clean_infos.append({"chapter": _pl["chapter"], "file": os.path.basename(_cpath),
                            "derived_from": os.path.basename(_rpath),
                            "identical_to_strip": _clean == strip_comments(_t),
                            "comments_in_clean": _clean.count("<!--"),
                            "footnote_syntax_in_clean": _clean.count("[^"),
                            "chars": len(_clean)})
        chapters_meta.append({"chapter": _pl["chapter"], "file": os.path.basename(_rpath),
                              "file_stem": _pl["stem"],
                              "clean_file": os.path.basename(_cpath), "title": _pl["title"],
                              "rule": _pl["rule"], "pages": _pl["pages"],
                              "block_count": len(_pl["blocks"]),
                              "body_count": sum(1 for b in _pl["blocks"]
                                                if b.get("kind") in ("paragraph", "equation",
                                                                     "table", "figure", "unknown")),
                              "char_count": sum(len(b.get("text_raw") or "") for b in _pl["blocks"]),
                              "stats": _st})
    # **拼接不变量**（G20 判据②的可判定形式）——刻意用**两条各自充分**的判据，而不是"逐字节相等"：
    #   ① **页标记集合完全相同**：漏章 / 重复章 / 丢页 都会当场破（"恰好覆盖全部内容块"）
    #   ② **去注释后逐行相同**：正文内容不漏不重（页标记的位置差异不算——S6 会把跨页整段的页标记
    #      留在前一页，那是合法归属，不是内容差异）
    _joined = "\n\n".join(p.rstrip("\n") for p in md_parts) + "\n"
    _PAGE_MARK = re.compile(r"<!--\s*p(\d{3,4})\s*-->")

    def _pages_of(t: str) -> list:
        return [int(x) for x in _PAGE_MARK.findall(t)]

    def _body_lines(t: str) -> list:
        return [re.sub(r"\s+", " ", ln).strip()
                for ln in re.sub(r"<!--.*?-->", "", t, flags=re.S).split("\n") if ln.strip()]

    assembly_check = {"blocks_direct": len(_book_bl),
                      "blocks_in_chapters": sum(m["block_count"] for m in chapters_meta),
                      "pages_multiset_equal": _pages_of(_joined) == _pages_of(md),
                      "body_lines_equal": _body_lines(_joined) == _body_lines(md),
                      "markdown_equals_single_build": _joined == md}
    md_path = os.path.join(book_dir, f"{_plan[0]['stem']}-reviewed.md")
    clean_md = strip_comments(md)
    clean_path = os.path.join(book_dir, f"{_plan[0]['stem']}-clean.md")
    clean_info = clean_infos[0]
    # entities.jsonl
    os.makedirs(os.path.join(book_dir, "index"), exist_ok=True)
    ents = build_entities(blocks)
    with open(os.path.join(book_dir, "index", "entities.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in ents:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    # errata.md（U 类：源文本如此、非错误）—— 从 `errata\<book>.jsonl` 读入（若有），否则留空模板
    # **A7-13 搜索顺序**：① **台账目录**（`s5-out\<书>\errata.jsonl`，A7-13 起由 `judge_worklist.apply`
    # 自动重建；与 waivers 同理：**不会被 publish 的整体搬移带走**）→ ② 工作区 `errata\<书>.jsonl`（老位置）
    errata_rows = []
    _root_guess = os.path.dirname(os.path.dirname(os.path.abspath(args.out)))
    _ep_cands = [os.path.join(_root_guess, "s5-out", str(args.book), "errata.jsonl"),
                 os.path.join(_root_guess, "errata", f"{args.book}.jsonl")]
    _ep = next((p for p in _ep_cands if os.path.exists(p)), _ep_cands[-1])
    if os.path.exists(_ep):
        errata_rows = [json.loads(l) for l in open(_ep, encoding="utf-8") if l.strip()]
    # 2026-11-15（1c）：**按当前读序重算锚点**（上游改动后不让引文过期）。
    # 台账行里没有 bbox（它在队列/ingest 行里）→ 先按 item_id 补上，再重算。
    try:
        _bbmap = {}
        for _v in (verdicts or []):
            _bb = _v.get("bbox_norm") or _v.get("bbox")
            if _v.get("item_id") and isinstance(_bb, (list, tuple)) and len(_bb) == 4:
                _bbmap[_v["item_id"]] = list(_bb)
        for _r in errata_rows:
            if (_r.get("kind") or "") == "missing_text" and not _r.get("bbox"):
                _r["bbox"] = _bbmap.get(_r.get("item_id"))
        # 见证/主引擎片段在**队列**里（ingest 行不带 span）→ 直接读队列补齐
        _spans = {}
        try:
            _qp = os.path.join(_root_guess, "s5-out", "visual-queue.s3.%s.jsonl" % args.book)
            if os.path.exists(_qp):
                for _l in open(_qp, encoding="utf-8"):
                    if not _l.strip():
                        continue
                    _it = json.loads(_l)
                    _sp = ((_it.get("span") or {}).get("paddle_span")
                           or (_it.get("span") or {}).get("mineru_span") or "")
                    if _it.get("item_id") and _sp:
                        _spans[_it["item_id"]] = _sp
        except Exception:                                           # noqa: BLE001
            _spans = {}
        errata_rows = refresh_missing_anchors(errata_rows, blocks, "\n\n".join(md_parts),
                                              spans=_spans)
    except Exception:                                               # noqa: BLE001
        pass                                                        # 重算失败**降级**为存档锚点，不带走主流程
    with open(os.path.join(book_dir, "errata.md"), "w", encoding="utf-8", newline="\n") as fh:
        _typo = [r for r in errata_rows if (r.get("kind") or "source_typo") == "source_typo"]
        _nb = [r for r in errata_rows if (r.get("kind") or "") == "non_book_ink"]
        fh.write("# 勘误与「非本书内容」（errata）\n\n")
        fh.write("> **两类分开**：① `source_typo` = 原书印错/笔误，**正文一个字都不改**；"
                 "② `non_book_ink` = 非本书内容（背面透印 / 读者手写 / 印章 / 贴纸 / 装饰），"
                 "**已从净版移出**，留底版保留 `<!-- NON-BOOK-INK … -->` 注释可追溯。\n\n")
        fh.write("## 1. 原书印错（source_typo，正文不改）\n\n")
        fh.write("| 页码 | 位置 | 原文 | 建议 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n")
        if _typo:
            for r in _typo:
                fh.write("| %s | %s | %s | %s | %s | %s |\n" % (
                    r.get("page", "—"), r.get("position", "—"), str(r.get("original", "")).replace("|", "\\|"),
                    r.get("suggestion", "保持原样"), r.get("basis", "—"), r.get("confidence", "—")))
        else:
            fh.write("| — | — | — | — | 本次未登记 | — |\n")
        fh.write("\n## 2. 非本书内容（non_book_ink，已移出净版）\n\n")
        fh.write("| 页码 | 位置 | 页上原文 | 是什么 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n")
        if _nb:
            for r in _nb:
                fh.write("| %s | %s | %s | %s | %s | %s |\n" % (
                    r.get("page", "—"), r.get("position", "—"), str(r.get("original", "")).replace("|", "\\|"),
                    r.get("suggestion", "移出净版（非本书内容）"), r.get("basis", "—"), r.get("confidence", "—")))
        else:
            fh.write("| — | — | — | — | 本次未登记 | — |\n")
        # 2026-11-15（P1-1）：**块级标记**（判定者直接对手写/印章/装饰下的结论）也要登记
        try:
            _bflags = load_block_flags(_root_guess, str(args.book))
        except Exception:                                           # noqa: BLE001
            _bflags = {}
        if _bflags:
            fh.write("\n### 2b. 判定者直接标记的块（块级 `non_book_ink`）\n\n")
            fh.write("| 页 | 块 | 依据 |\n|---|---|---|\n")
            for _bid, _f in sorted(_bflags.items(), key=lambda kv: (kv[1].get("page") or 0, kv[0])):
                fh.write("| %s | `%s` | %s |\n" % (_f.get("page", "—"), _bid,
                                                   str(_f.get("basis") or "—").replace("|", "\\|")))
        # 2026-11-15（P0-1/P0-2）：**主引擎漏掉、页面确实有**的内容 —— 读者确认后可脚本插回
        _mt = [r for r in errata_rows if (r.get("kind") or "") == "missing_text"]
        fh.write("\n## 3. 主引擎漏掉、页面上确实有（missing_text，正文暂缺）\n\n")
        fh.write("> 判定者已看图确认这些内容**在页面上**，但主引擎没读出来 → 正文里没有。"
                 "确认无误后，可用 `errata_apply.py` 按「前文/后文」锚点把它们**插回正文**"
                 "（插入处会留 `<!-- MISSING-TEXT-INSERT … -->` 痕迹；净版不含注释、正文保留）。\n\n")
        fh.write("| 页码 | 缺的内容（页上原文） | 插在哪 | 依据 | 置信度 |\n|---|---|---|---|---|\n")
        if _mt:
            for r in _mt:
                _an = r.get("anchor") or {}
                _pos = {"inline": "接在前文后面（同段）", "own_line": "另起一行"}.get(
                    _an.get("position"), "—")
                _where = "在「%s」与「%s」之间，%s" % (
                    str(_an.get("before") or "—")[-20:], str(_an.get("after") or "—")[:20], _pos)
                fh.write("| %s | %s | %s | %s | %s |\n" % (
                    r.get("page", "—"),
                    str(r.get("missing_text") or "").replace("|", "\\|"),
                    _where.replace("|", "\\|"),
                    str(r.get("basis") or "—").replace("|", "\\|"),
                    (_an.get("confidence") or "—")))
        else:
            fh.write("| — | — | — | 本次未登记 | — |\n")
        if _mt:
            # 2026-11-15（P0-1）：**机器可读块** —— 读者拿着交付目录就能用 `errata_apply.py`
            # （或随包发出的 `missing-text-insert.py`）按锚点插回，不需要台账、不需要本仓库。
            fh.write("\n> 下面每行是一条可直接插回的条目（机器读；渲染后看不见）。\n\n")
            for r in _mt:
                _payload = {"item_id": r.get("item_id"), "page": r.get("page"),
                            "missing_text": r.get("missing_text") or "",
                            "anchor": r.get("anchor") or {}, "bbox": r.get("bbox"),
                            "basis": r.get("basis") or "—"}
                fh.write("<!-- MISSING-TEXT-ANCHOR %s -->\n"
                         % json.dumps(_payload, ensure_ascii=False, sort_keys=True))
    # report.json
    esc = {}
    for r in mstats["reviews"]:
        esc[r["category"]] = esc.get(r["category"], 0) + 1
    # 版式报告：优先用 S6 写进 structure 的 `layout`；独立跑 s7 时从 `_layout` 现算（同口径）
    _layout_report = structure.get("layout") or {}
    if not _layout_report and _layout:
        try:
            import s6_assemble as _s6m
            _layout_report = {"declared": {str(p): {"kind": v.get("kind"),
                                                    "regions": v.get("regions"),
                                                    "cuts": v.get("cuts")}
                                           for p, v in _s6m.layout_map(_layout).items()},
                              "mismatch": []}
        except Exception:                                          # noqa: BLE001
            _layout_report = {}
    report = {
        "book": args.book, "chapter": chapter, "source_ir": os.path.abspath(args.ir),
        "expired_verdicts": expired_ids,
        "clean_edition": clean_info,
        # C1（SPEC §3.8）：多章产物 —— `chapters[]` + `chapter_split{rule,suspects,empty_chapters}`
        # 加 G20 判据②的可判定形式（`assembly_check`：逐章拼接 == 一次装配的全文）
        "chapters": chapters_meta,
        "clean_editions": clean_infos,
        # 只增字段：**未声明锚点的书不写 `anchors`**（保持既有报告逐字节不变）
        **({"layout": _layout_report} if _layout_report else {}),
        "chapter_split": {"rule": _chap["rule"],
                          **({"anchors": _chap["anchors"]} if _chap.get("anchors") else {}),
                          **({"toc_pages": _chap["toc_pages"]} if _chap.get("toc_pages") else {}),
                          **({"toc_skipped": _chap["toc_skipped"]}
                             if _chap.get("toc_skipped") else {}),
                          "suspects": _chap["suspects"],
                          "empty_chapters": _chap["empty_chapters"], "notes": _chap["notes"],
                          "boundaries": _chap["boundaries"],
                          # C2-3（SPEC §3.8 订正④ / §3.9）：输入性质与切片页清单（供 G20 判据⑥）
                          "input_kind": _chap.get("input_kind"),
                          "input_pages": _chap.get("input_pages"),
                          "content_blocks": _chap["content_blocks"],
                          "covered_blocks": _chap["covered_blocks"],
                          "assembly_check": assembly_check},
        "markdown": os.path.relpath(md_path, book_dir),
        "markdown_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
        "counts": {k: v for k, v in mstats.items()
                   if k not in ("absorbed", "figure_row_order", "figure_row_ambiguous")},
        "vision_absorbed": mstats.get("absorbed", []),
        "split_consumed_transcriptions": _consumed,
        "figures": figures,
        "vision_figures": _synth_figs,
        "caption_reassigned": _cap_reassigned,
        "figure_verdicts_skipped": _fig_skipped,
        "figure_row_order": mstats.get("figure_row_order", []),
        "figure_row_ambiguous": mstats.get("figure_row_ambiguous", []),
        "furniture_excluded": structure.get("furniture", {}).get("records", []),
        # 家具减配的风险计数（位置兜底尚未实现；供人看「有没有疑似误剔 / 位置兜底会多剔多少」）
        "furniture_counts": structure.get("furniture", {}).get("counts", {}),
        "cross_page_joins": structure.get("cross_page_joins", []),
        "footnotes": structure.get("footnotes", {}),
        "headings": structure.get("heading_tree", []),
        # 门禁**不在这里判**（产出者不得自判）：由 _mcp-test\validate_run.py 独立校验后写入 gates/blocking
        "gates": None,
        "gates_note": "由 validate_run.py 独立写入（SPEC §5.1「产出者不自判」）",
        "escalations": {"count": len(mstats["reviews"]), "by_category": esc,
                        "rate_per_100_pages": round(len(mstats["reviews"]) / (len(structure.get("pages", [1])) / 100.0), 1),
                        "items": mstats["reviews"]},
        "unresolved": {"s3_questions": len(questions), "s4_hits": len(s4_hits),
                       "s5_verdicts": len(verdicts), "vision_insertions": len(insertions),
                       "note": (f"S5 判定已执行（{len(verdicts)} 条）；转写类结果按 **U-10 裁定直接写入正文**"
                                f"（{len(insertions)} 处，带 VISION-INSERT 审计痕迹）；"
                                f"S3 的存在性/删除类判定仍按 U-07 默认口径**只标记不改写**")
                       if verdicts else
                       "S5 判定尚未在本队列上执行；全部保留主引擎原文并标记（U-07 默认口径）"},
        "vision_insertions": [{"page": v["pdf_page"], "bbox": v.get("bbox_norm") or v.get("bbox"),
                               "text": v.get("transcription"), "item_id": v.get("item_id"),
                               "model": (v.get("judge") or {}).get("model"),
                               "mode": (v.get("judge") or {}).get("mode"),
                               "confidence": v.get("confidence")} for v in insertions],
        "insertion_order": insertion_order,
        "insertions_applied": mstats["vision_insertions"],
    }
    # C2（SPEC §3.9）：书级编排喂进来的 `volumes` / `merge` 元数据（**只增字段，不覆盖已算出的字段**）
    _extra = {}
    _raw_in = getattr(args, "extra_in", "") or ""
    if _raw_in:
        try:
            _extra = json.loads(_raw_in) if _raw_in.lstrip().startswith("{") \
                else json.load(open(_raw_in, encoding="utf-8"))
        except Exception as _exc:                                       # noqa: BLE001
            print(f"[warn] --in 解析失败，忽略：{_exc}")
            _extra = {}
    for _k, _v in (_extra or {}).items():
        report.setdefault(_k, _v)
        if _k not in report or report[_k] is None:
            report[_k] = _v
    with open(os.path.join(book_dir, "report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"book": args.book, "chapter": chapter,
                      "chapters": [(m["chapter"], m["file"], m["block_count"]) for m in chapters_meta],
                      "markdown": md_path, "assembly_check": assembly_check,
                      "counts": mstats, "review_categories": esc,
                      "entities": len(ents), "figures": len(figures)}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- selftest
def _blk(page, i, kind, text, bbox, field="text", role="primary", parent=None, kids=None):
    return {"block_id": f"mineru-p{page:04d}-b{i:03d}-{field}", "engine": "mineru", "page": page,
            "order": i, "kind": kind, "role": role, "parent_id": parent, "source_field": field,
            "text_raw": text, "text_canon": textnorm.norm_strong(text), "bbox_norm": bbox,
            "bbox_src": None, "bbox_space": "x", "flags": [], "_children": kids or []}


def selftest() -> int:
    cases, passed = [], 0

    gfm, flags = html_table_to_gfm(
        "<table><tr><td>l</td><td>40</td></tr><tr><td>Δh</td><td>6.2</td></tr></table>")
    rows = gfm.splitlines()
    cases.append(("HTML 表 → GFM 管道表（表头/分隔/数据行）",
                  len(rows) == 3 and rows[0] == "| l | 40 |" and rows[1] == "| --- | --- |"
                  and rows[2] == "| Δh | 6.2 |" and not flags, gfm))
    _, flags2 = html_table_to_gfm('<table><tr><td rowspan="2">x</td><td>1</td></tr></table>')
    cases.append(("rowspan 必须记 flag（不静默丢结构）", "rowspan_unsupported" in flags2, flags2))

    eq = clean_equation("$$\np V = n R T\\tag{2.1}\n$$")
    cases.append(("行间公式压掉内部换行、保留 tag", eq == "$$p V = n R T\\tag{2.1}$$", eq))

    struct = {"heading_tree": [{"page": 1, "level": 1, "text": "第2章 气体", "suspect_list_item": False},
                               {"page": 1, "level": 3, "text": "2.1.1 理想气体定律", "suspect_list_item": False}],
              "cross_page_joins": [], "pages": [1], "furniture": {"records": []},
              "footnotes": {"missing_definitions": [], "orphan_definitions": []}}
    blocks = [
        _blk(1, 0, "heading", "第2章 气体", [0.1, 0.14, 0.4, 0.16]),
        _blk(1, 1, "paragraph", "正文一段。", [0.1, 0.2, 0.9, 0.25]),
        _blk(1, 2, "equation", "$$\npV=nRT\\tag{2.1}\n$$", [0.3, 0.3, 0.7, 0.35]),
        _blk(1, 3, "heading", "2.1.1 理想气体定律", [0.1, 0.4, 0.5, 0.42]),
        _blk(1, 4, "running_head", "2.1 理想气体定律", [0.3, 0.06, 0.7, 0.07]),
        _blk(1, 5, "page_number", "10", [0.05, 0.92, 0.1, 0.93]),
        _blk(1, 6, "table_body", "<table><tr><td>l</td><td>40</td></tr></table>", [0.1, 0.5, 0.9, 0.6],
             field="table_body", kids=[_blk(1, 6, "caption", "表 2.1 Boyle 的实验数据",
                                            [0.1, 0.5, 0.9, 0.52], field="table_caption",
                                            role="child", parent="mineru-p0001-b006-table_body"),
                                       _blk(1, 6, "footnote", "* 1 in(英寸)=25.4 mm(毫米)",
                                            [0.1, 0.6, 0.9, 0.62], field="table_footnote",
                                            role="child", parent="mineru-p0001-b006-table_body")]),
    ]
    qs = [{"pdf_page": 1, "review_type": "content_diff", "bbox": [0.3, 0.3, 0.7, 0.35],
           "item_id": "S3-P0001-CONTENT-001", "question": "页面上这段怎么写的？"}]
    md, ms = build_markdown(blocks, struct, qs, [], {}, "02")
    checks = [
        ("标题层级写进 Markdown", "## 第2章 气体" in md or "# 第2章 气体" in md),
        ("页边界标记存在", "<!-- p001 -->" in md),
        ("家具不进正文", "2.1 理想气体定律" not in md.split("<!-- p001 -->")[-1].split("## ")[0]),
        ("公式保留 tag", "\\tag{2.1}" in md),
        ("表格转成 GFM", "| l | 40 |" in md),
        ("表题与表注都在", "表 2.1 Boyle 的实验数据" in md and "1 in(英寸)" in md),
        ("S3 未决问题带 REVIEW 标记", any(r["category"] == "S3_content_diff" for r in ms["reviews"])),
        ("空块/无图被标记", all(k in md for k in ("<!-- REVIEW:",))),
    ]
    for name, ok in checks:
        cases.append((name, ok, None))
    ents = build_entities(blocks)
    cases.append(("entities.jsonl 每块一条且带别名",
                  len(ents) >= 4 and all(r["aliases"] for r in ents), len(ents)))

    # U-10：转写类结果直接写入正文 + 审计痕迹 + 未解决者在页首显形
    ins = [{"pdf_page": 1, "bbox": [0.05, 0.25, 0.95, 0.29],
            "transcription": "首先，温度 $T_{1}$ 不变", "item_id": "TR-R4a-0001-01",
            "confidence": 0.96, "judge": {"model": "test-model"}}]
    pm = [{"pdf_page": 1, "category": "transcribe_unresolved", "bbox": [0.1, 0.2, 0.2, 0.3],
           "note": "TR-R4b-0001-02 verdict=unclear"}]
    _vmap = {q["item_id"]: {"item_id": q["item_id"], "verdict": "no_content_diff"}
             for q in qs if q.get("item_id")}
    md2, ms2 = build_markdown(blocks, struct, qs, [], {}, "02", insertions=ins, page_markers=pm,
                              verdict_by_item=_vmap)
    cases.append(("已判定的 S3 问题被折叠到页尾备查块（人只看到真待办）",
                  "已判定（备查" in md2 and ms2["folded_judged_markers"] == 1
                  and "<!-- REVIEW: S3_content_diff" not in md2,
                  {"folded": ms2["folded_judged_markers"], "human_facing": ms2.get("human_facing_markers")}))
    lines2 = md2.splitlines()
    idx_ins = next((i for i, l in enumerate(lines2) if "首先，温度" in l), None)
    idx_para = next((i for i, l in enumerate(lines2) if l.startswith("正文一段。")), None)
    idx_eq = next((i for i, l in enumerate(lines2) if "\\tag{2.1}" in l), None)
    cases.append(("视觉转写被插入正文（且带 VISION-INSERT 痕迹）",
                  idx_ins is not None and "<!-- VISION-INSERT:" in md2
                  and ms2["vision_insertions"] == 1, ms2.get("vision_insertions")))
    cases.append(("插入位置落在锚点之后、下一块之前",
                  None not in (idx_ins, idx_para, idx_eq)
                  and idx_para < idx_ins < idx_eq,
                  {"para": idx_para, "ins": idx_ins, "eq": idx_eq}))
    cases.append(("未解决的转写任务在页首显形",
                  any(r["category"] == "transcribe_unresolved" for r in ms2["reviews"]), None))
    # B1：转写**覆盖**后不再让人读第二遍（纯对账，不是新判据）
    _eb = [_blk(1, 9, "paragraph", "", [0.2, 0.6, 0.9, 0.66])]
    _ins_full = [{"pdf_page": 1, "bbox": [0.2, 0.6, 0.9, 0.66], "transcription": "按图补的一句",
                  "item_id": "TR-R1-0001-01", "confidence": 0.9, "judge": {"model": "t"}}]
    md_b1, ms_b1 = build_markdown(_eb, struct, [], [], {}, "02", insertions=_ins_full)
    cases.append(("B1：转写**完全覆盖**空块 → 不再发 empty_block，改记「已判定（备查）」",
                  "<!-- REVIEW: empty_block" not in md_b1
                  and "TR-R1-0001-01=transcribed@S7" in md_b1
                  and ms_b1["folded_judged_markers"] == 1, md_b1[-160:]))
    _ins_part = [{"pdf_page": 1, "bbox": [0.2, 0.6, 0.41, 0.66], "transcription": "只盖住左边一点",
                  "item_id": "TR-R1-0001-02", "confidence": 0.9, "judge": {"model": "t"}}]
    md_b1n, _ms_b1n = build_markdown(_eb, struct, [], [], {}, "02", insertions=_ins_part)
    cases.append(("B1：只覆盖约 30% → **仍**发 empty_block（阈值负例，不许误折叠）",
                  "<!-- REVIEW: empty_block" in md_b1n, md_b1n[-160:]))
    # 2026-10-26：**非本书内容**（透印 / 手写）→ 降级为注释，净版不含，原文留在注释里
    _nb_blk = [_blk(1, 11, "paragraph", "学氏法学外部，支令", [0.1, 0.1, 0.9, 0.2])]
    _nb_q = [{"pdf_page": 1, "review_type": "content_diff", "bbox": [0.1, 0.1, 0.9, 0.2],
              "item_id": "S3-P0001-CONTENT-099", "question": "页面上这段怎么写的？"}]
    _nb_v = {"S3-P0001-CONTENT-099": {"item_id": "S3-P0001-CONTENT-099", "verdict": "non_book_ink"}}
    md_nb, ms_nb = build_markdown(_nb_blk, struct, _nb_q, [], {}, "02", verdict_by_item=_nb_v)
    cases.append(("2026-10-26：`non_book_ink` → 正文不发该段、改 `NON-BOOK-INK` 注释（净版不含、原文留痕）",
                  "学氏法学外部" not in strip_comments(md_nb)
                  and "<!-- NON-BOOK-INK:" in md_nb and "学氏法学外部" in md_nb
                  and ms_nb["non_book_ink"] == 1, md_nb[-200:]))
    _ev_v = {"S3-P0001-CONTENT-099": {"item_id": "S3-P0001-CONTENT-099", "verdict": "exists"}}
    md_ev, _ms_ev = build_markdown(_nb_blk, struct, _nb_q, [], {}, "02", verdict_by_item=_ev_v)
    cases.append(("2026-10-26：同一块若判 `exists` → 照常进正文（隔离只认 non_book_ink）",
                  "学氏法学外部" in strip_comments(md_ev) and "<!-- NON-BOOK-INK:" not in md_ev, None))
    # 2026-11-15（1c）：锚点保鲜 —— 有效则保留、失效按见证片段重定位、都失败标 stale
    _md_ctx = ("上一段的结尾文字就在这里。\n\n下一段的开头文字从这里开始。\n")
    _md_ctx2 = "习题 12 …说明(2)的省略是否合理。\n\n下一段的开头文字从这里开始。\n"
    _rows_ok = [{"kind": "missing_text", "item_id": "MT-A", "page": 1, "missing_text": "[答案:9 J]",
                 "anchor": {"before": "就在这里。", "after": "下一段的开头",
                            "position": "own_line", "confidence": "high"}}]
    _r1 = refresh_missing_anchors(_rows_ok, [], _md_ctx)
    cases.append(("2026-11-15（1c）：原锚点在当前正文里还有效 → 保留并标记 `stored_valid`",
                  _r1[0]["anchor"]["source"] == "stored_valid"
                  and _r1[0]["anchor"]["before"] == "就在这里。", _r1[0]["anchor"]))
    _rows_stale = [{"kind": "missing_text", "item_id": "MT-B", "page": 1,
                    "missing_text": "[答案:−3.057 J]",
                    "anchor": {"before": "已经过期的旧引文", "after": "也过期了",
                               "position": "own_line", "confidence": "high"}}]
    _spans = {"MT-B": "习题 12 …说明(2)的省略是否合理。 [答案:-3.057 J] 下一段的开头文字从这里开始。"}
    _r2 = refresh_missing_anchors(_rows_stale, [], _md_ctx2, spans=_spans)
    cases.append(("2026-11-15（1c）：旧引文失效 → 按**见证片段**定位、锚点取自 md 原文（`recomputed`）",
                  _r2[0]["anchor"]["source"] == "recomputed"
                  and _r2[0]["anchor"]["before"].endswith("是否合理。")
                  and _r2[0]["anchor"]["after"].startswith("下一段的开头"),
                  _r2[0]["anchor"]))
    _r3 = refresh_missing_anchors(_rows_stale, [], _md_ctx, spans={})
    cases.append(("2026-11-15（1c）负例：定位不到 → 保留存档锚点并标 `stale`（读者侧会拒绝，不乱插）",
                  _r3[0]["anchor"]["source"] == "stale"
                  and _r3[0]["anchor"]["before"] == "已经过期的旧引文", _r3[0]["anchor"]))
    _rows_inline = [{"kind": "missing_text", "item_id": "MT-C", "page": 1, "missing_text": "x",
                     "anchor": {"before": "过期", "after": "过期", "position": "inline",
                                "confidence": "high"}}]
    cases.append(("2026-11-15（1c）：`inline`（段中）不重算（没有块内偏移量）→ 标 stale",
                  refresh_missing_anchors(_rows_inline, [], _md_ctx)[0]["anchor"]["source"] == "stale",
                  None))
    _mt_v = {"MT-P0001-01": {"item_id": "MT-P0001-01", "pdf_page": 1, "bbox_norm": [0.1, 0.3, 0.9, 0.4],
                             "verdict": "exists",
                             "errata": {"kind": "missing_text", "missing_text": "[答案: 1 J]",
                                        "anchor": {"before": "题 12 结尾。", "after": "题 13 开头。",
                                                   "position": "own_line", "confidence": "high"},
                                        "basis": "主引擎无此段；见证有；该处有墨迹"}}}
    # 2026-11-15（P1-6）：同排连续图块 → **合并成一个段落**（一图一行 → 一行多图）
    def _figblk(pid, i, x0, x1, y0, y1, cap=""):
        bb = {"block_id": "mineru-p0001-b%03d-content" % i, "page": pid, "order": i,
              "kind": "figure", "role": "primary", "text_raw": "", "bbox_norm": [x0, y0, x1, y1],
              "children": []}
        if cap:
            bb["_children"] = [{"kind": "caption", "source_field": "image_caption",
                                "text_raw": cap, "parent_id": bb["block_id"]}]
        return bb
    _f1 = _figblk(1, 60, 0.05, 0.28, 0.30, 0.42, "（a）")
    _f2 = _figblk(1, 61, 0.30, 0.53, 0.30, 0.42, "（b）")
    _f3 = _figblk(1, 62, 0.55, 0.78, 0.31, 0.43, "（c）")
    _fig_map = {b["block_id"]: {"file": "figures/%s.png" % b["block_id"][-14:]} for b in (_f1, _f2, _f3)}
    _st_row = dict(struct, cross_page_splits=[], cross_page_joins=[])
    md_row, _ms_row = build_markdown([_f1, _f2, _f3], _st_row, [], [], _fig_map, "02")
    _img_lines = [l for l in md_row.splitlines() if l.startswith("![")]
    cases.append(("2026-11-15（P1-6）：同排三张图 → **同一行三段 `![…]`**（不再一图一行）",
                  len(_img_lines) == 1 and _img_lines[0].count("![") == 3
                  and _ms_row["figures"] == 3, {"lines": _img_lines, "figs": _ms_row["figures"]}))
    cases.append(("2026-11-15（P1-6）：题注仍各自成行（不丢、也不并进图片那行）",
                  "（a）" in md_row and "（b）" in md_row and "（c）" in md_row
                  and "（a）" not in _img_lines[0], None))
    _f4 = _figblk(1, 63, 0.05, 0.28, 0.60, 0.72, "（d）")
    _fig_map2 = dict(_fig_map, **{_f4["block_id"]: {"file": "figures/x4.png"}})
    md_two, _ms_two = build_markdown([_f1, _f4], _st_row, [], [], _fig_map2, "02")
    cases.append(("2026-11-15（P1-6）：**不同排**的两张图 → 仍各占一段",
                  len([l for l in md_two.splitlines() if l.startswith("![")]) == 2, None))
    # 负例：同一排里**中间夹一个表**（图-表-图）→ 不许把两侧的图并成一行
    _tb = {"block_id": "mineru-p0001-b065-table_body", "page": 1, "order": 65,
           "kind": "table", "role": "primary", "text_raw": "<table><tr><td>x</td></tr></table>",
           "html": "<table><tr><td>x</td></tr></table>",
           "bbox_norm": [0.30, 0.30, 0.53, 0.42], "children": []}
    md_gap, _ = build_markdown([_f1, _tb, _f3], _st_row, [], [], _fig_map, "02")
    cases.append(("2026-11-15（P1-6）负例：同排「图-表-图」 → 不合并（只在连续图块之间合）",
                  len([l for l in md_gap.splitlines() if l.startswith("![")]) == 2, None))
    # 2026-11-15（P1-1）：块级 `non_book_ink` → 该块不进正文、留审计注释（图块也不引用图）
    _nb_flag_blk = dict(_blk(1, 70, "paragraph", "红笔手写字：抓手", [0.1, 0.6, 0.9, 0.65]))
    _nb_flag_blk["_nb_flag"] = {"kind": "non_book_ink", "basis": "红笔手写（非印刷体）", "page": 1}
    md_flag, ms_flag = build_markdown([_nb_flag_blk], struct, [], [], {}, "02")
    cases.append(("2026-11-15（P1-1）：块被标 `non_book_ink` → 正文不含该段、留 `NON-BOOK-INK` 注释",
                  "抓手" not in strip_comments(md_flag)
                  and "<!-- NON-BOOK-INK: block=" in md_flag
                  and "抓手" in md_flag and ms_flag["non_book_ink"] == 1, md_flag[-160:]))
    _fig_nb = dict(_blk(1, 71, "paragraph", "", [0.2, 0.3, 0.6, 0.5]), kind="figure",
                   block_id="mineru-p0001-b071-content")
    _fig_nb["_nb_flag"] = {"kind": "non_book_ink", "basis": "红笔圈注被读成图", "page": 1}
    _figmap_nb = {"mineru-p0001-b071-content": {"file": "figures/x.png"}}
    md_fignb, ms_fignb = build_markdown([_fig_nb], struct, [], [], _figmap_nb, "02")
    cases.append(("2026-11-15（P1-1）：**图块**被标 `non_book_ink` → 不引用图、留注释（手写被读成图）",
                  "figures/x.png" not in md_fignb and "NON-BOOK-INK: block=mineru-p0001-b071-content" in md_fignb,
                  md_fignb[-160:]))
    # 2026-11-15（P1-1）：`mark` 模式 —— 块里混有印刷正文时**不删正文**，只留标记（宁多勿少）
    _mix = dict(_blk(1, 72, "paragraph", "印刷正文若干字，后面跟了红笔圈注", [0.1, 0.7, 0.9, 0.75]))
    _mix["_nb_flag"] = {"kind": "non_book_ink", "basis": "同一块里既有印刷正文又有手写圈注",
                        "mode": "mark", "page": 1}
    md_mark, ms_mark = build_markdown([_mix], struct, [], [], {}, "02")
    cases.append(("2026-11-15（P1-1）：`mode=mark`（块里混有印刷正文）→ **正文保留** + 只加标记",
                  "印刷正文若干字" in strip_comments(md_mark)
                  and "<!-- NON-BOOK-INK-MARK:" in md_mark
                  and ms_mark.get("non_book_ink_marked") == 1
                  and ms_mark["non_book_ink"] == 0, md_mark[-160:]))
    md_mt, ms_mt = build_markdown(_nb_blk, struct, [], [], {}, "02", verdict_by_item=_mt_v)
    cases.append(("2026-11-15：`missing_text` → 页末发 `REVIEW: missing_text`（计入 human_facing_markers）",
                  "<!-- REVIEW: missing_text page=1" in md_mt
                  and "item=MT-P0001-01" in md_mt
                  and ms_mt["review_markers"] == 1
                  and ms_mt["human_facing_markers"] == 1, md_mt[-120:]))
    # 2026-10-26（2.3）：空块的字被**跨页切分**搬走了 → 不再报 empty_block（否则校对表让人白看）
    _eb2 = _blk(1, 12, "paragraph", "", [0.2, 0.7, 0.9, 0.76])
    _st2 = dict(struct, cross_page_splits=[
        {"from_block_id": _eb2["block_id"], "from_page": 1, "to_page": 2,
         "keep": "", "moved_text": "后半句在下一页。"}])
    md_eb2, ms_eb2 = build_markdown(
        [_eb2, _blk(2, 13, "paragraph", "后半句在下一页。", [0.2, 0.1, 0.9, 0.16])], _st2, [], [], {}, "02")
    cases.append(("2026-10-26（2.3）：空块的字被跨页切分搬走 → **不发 empty_block**，留 `EMPTY-BLOCK-COVERED` 痕迹",
                  "<!-- REVIEW: empty_block" not in md_eb2 and "EMPTY-BLOCK-COVERED" in md_eb2
                  and ms_eb2["empty_block_covered"] == 1, md_eb2[-200:]))
    # 负例：与切分无关的空块**仍要报**（不许借这个规则把噪音也吞掉）
    md_eb3, ms_eb3 = build_markdown([_blk(1, 14, "paragraph", "", [0.2, 0.7, 0.9, 0.76])],
                                    struct, [], [], {}, "02")
    cases.append(("2026-10-26（2.3）负例：不相干的空块**仍**报 empty_block",
                  "<!-- REVIEW: empty_block" in md_eb3 and ms_eb3["empty_block_covered"] == 0, None))
    # 跨页切分：源块截断 + 后半句落到目标页开头
    merged = "式中：n 为气体物质的量。…而著称。但在人类认识自然规律的长河中,这是两个多世纪的成果。"
    struct_split = dict(struct, cross_page_splits=[
        {"from_block_id": "mineru-p0001-b000-text", "from_page": 1, "to_page": 2,
         "keep": "式中：n 为气体物质的量。…而著称。但在人类", "moved_text": "认识自然规律的长河中,这是两个多世纪的成果。"}])
    blocks_s = [_blk(1, 0, "paragraph", merged, [0.1, 0.2, 0.9, 0.25]),
                _blk(2, 1, "paragraph", "17 世纪中叶，Boyle。", [0.1, 0.3, 0.9, 0.35])]
    md4, ms4 = build_markdown(blocks_s, struct_split, [], [], {}, "02")
    lines4 = md4.splitlines()
    i_moved = next((i for i, l in enumerate(lines4) if l.startswith("认识自然规律的长河中")), None)
    i_p2 = next((i for i, l in enumerate(lines4) if l.strip() == "<!-- p002 -->"), None)
    i_body2 = next((i for i, l in enumerate(lines4) if l.startswith("17 世纪中叶")), None)
    cases.append(("跨页切分（词/句中断开）→ 后半句**接成同一段**，页标记改为行内",
                  any(("但在人类" in l and "认识自然规律的长河中" in l and "<!-- p002 -->" in l)
                      for l in lines4) and "<!-- SPLIT-JOIN" in md4,
                  [l[:60] for l in lines4 if "但在人类" in l]))
    struct_sent = dict(struct, cross_page_splits=[
        {"from_block_id": "mineru-p0001-b000-text", "from_page": 1, "to_page": 2,
         "keep": "式中：n 为气体物质的量。这句话到此结束。",
         "moved_text": "认识自然规律的长河中，这是两个多世纪的成果。"}])
    blocks_t = [_blk(1, 0, "paragraph", "式中：n 为气体物质的量。这句话到此结束。", [0.1, 0.2, 0.9, 0.25]),
                _blk(2, 1, "paragraph", "17 世纪中叶，Boyle。", [0.1, 0.3, 0.9, 0.35])]
    md6, ms6 = build_markdown(blocks_t, struct_sent, [], [], {}, "02")
    lines6 = md6.splitlines()
    i_mv = next((i for i, l in enumerate(lines6) if l.startswith("认识自然规律的长河中")), None)
    i_p2b = next((i for i, l in enumerate(lines6) if l.strip() == "<!-- p002 -->"), None)
    cases.append(("跨页切分（句末断开）→ 后半句另起一行、页标记独占一行",
                  None not in (i_mv, i_p2b) and i_p2b < i_mv, {"p2": i_p2b, "moved": i_mv}))
    cases.append(("跨页切分：源块按页边界切开（保留部分 = keep，后半句接在其后）",
                  ms4["cross_page_split_sources"] == 1
                  and any(l.startswith("式中：n 为气体物质的量。…而著称。但在人类") for l in lines4),
                  ms4.get("cross_page_split_sources")))
    # **A7-2 负例（真书 p8/p26/p34 的形态）**：上一页有"已判定（备查）"折叠块时，
    #   句中断开的接句锚点**不得**取到那行注释（否则页标记与后半句一起被粘进注释行：
    #   按行切页看不见这条边界，且后半句在句中硬断）
    _qs_fold = [{"item_id": "S3-P0001-CONTENT-001", "pdf_page": 1, "review_type": "content_diff",
                 "bbox": [0.1, 0.2, 0.9, 0.25]}]
    _vmap_fold = {"S3-P0001-CONTENT-001": {"item_id": "S3-P0001-CONTENT-001", "verdict": "exists"}}
    md7, _ms7 = build_markdown(blocks_s, struct_split, _qs_fold, [], {}, "02",
                               verdict_by_item=_vmap_fold)
    lines7 = md7.splitlines()
    i_join = next((i for i, l in enumerate(lines7) if "但在人类" in l), None)
    _joined = lines7[i_join] if i_join is not None else ""
    _glued = [l for l in lines7 if "已判定" in l and "<!-- p002 -->" in l]
    cases.append(("A7-2：上一页有折叠注释时，接句锚点仍取**正文行**（页标记不得被粘进注释行）",
                  "已判定（备查" in md7 and i_join is not None
                  and "认识自然规律的长河中" in _joined and "<!-- p002 -->" in _joined
                  and not _glued,
                  {"joined": _joined[:90], "glued": [l[:90] for l in _glued]}))
    # 视觉插入吸收片段：区域内且文字被插入内容包含 → 不单独输出；区域内但文字不被包含 → 保留
    blocks_a = [_blk(1, 0, "paragraph", "正文。", [0.1, 0.1, 0.9, 0.15]),
                _blk(1, 1, "equation", "$$T _ {1}$$", [0.169, 0.668, 0.19, 0.68]),
                _blk(1, 2, "equation", "$$p _ {1} V _ {1} = p _ {2} V ^ {\\prime}$$",
                     [0.476, 0.665, 0.576, 0.682])]
    ins_a = [{"pdf_page": 1, "bbox_norm": [0.05, 0.643, 0.95, 0.705],
              "transcription": "首先，温度 $T_1$ 不变", "item_id": "TR-R4a-0001-01",
              "confidence": 0.96, "judge": {"model": "test-model"}}]
    md5, ms5 = build_markdown(blocks_a, struct, [], [], {}, "02", insertions=ins_a)
    lines5 = md5.splitlines()
    cases.append(("插入吸收：被覆盖的片段不再单独输出（且留 VISION-ABSORBED）",
                  ms5["absorbed_by_transcription"] == 1
                  and not any(l.strip() == "$$T _ {1}$$" for l in lines5)
                  and "<!-- VISION-ABSORBED:" in md5 and len(ms5["absorbed"]) == 1,
                  {"absorbed": ms5["absorbed_by_transcription"]}))
    cases.append(("插入吸收：同区域但文字**不在**转写里的公式必须保留",
                  any("p _ {1} V _ {1}" in l for l in lines5), None))
    # 同一页两个转写任务命中同一句话 → 只插一次；且必须用归一化框定位（像素框会掉到页尾兜底）
    ins2 = [dict(ins[0], item_id="TR-R4a-0001-01"),
            dict(ins[0], item_id="TR-R4b-0001-02", bbox_norm=[0.05, 0.21, 0.95, 0.24])]
    md3, ms3 = build_markdown(blocks, struct, qs, [], {}, "02", insertions=ins2, page_markers=[])
    cats = [r["category"] for r in ms3["reviews"]]
    cases.append(("重复转写只插一次（并记录 duplicate_transcription_skipped）",
                  ms3["vision_insertions"] == 1 and "duplicate_transcription_skipped" in cats, cats))
    cases.append(("去重保留**最精确**的区域（R4a 行带，而非 R4b 上方窗口）",
                  "item=TR-R4a-0001-01" in md3, None))
    cases.append(("插入按归一化框定位（落在锚点之后、下一块之前）",
                  md3.splitlines().index("首先，温度 $T_{1}$ 不变")
                  < next(i for i, l in enumerate(md3.splitlines()) if "\\tag{2.1}" in l),
                  None))

    # ---- C1（SPEC §3.8）：多章装配 --------------------------------------------------------
    def _mk(page, i, kind, text, y):
        return {"block_id": f"mineru-p{page:04d}-b{i:03d}-text", "engine": "mineru", "page": page,
                "order": i, "kind": kind, "role": "primary", "parent_id": None, "source_field": "text",
                "text_raw": text, "text_canon": text, "bbox_norm": [0.1, y, 0.9, y + 0.05],
                "bbox_src": None, "bbox_space": "x", "flags": [], "_children": []}

    def _hd(page, i, text):
        return {"index": i, "block_id": f"mineru-p{page:04d}-b{i:03d}-text", "page": page, "level": 1,
                "text": text[:60], "full_text": text, "text_level": 2, "suspect_list_item": False}

    _bl_c1 = [_mk(5, 0, "paragraph", "第 1 章末尾的正文。", 0.10),
              _mk(5, 1, "heading", "第2章 气体", 0.20),
              _mk(5, 2, "heading", "2.1 理想气体定律", 0.30),
              _mk(5, 3, "paragraph", "气体是物质的一种状态。", 0.40),
              _mk(6, 0, "paragraph", "下一页的正文。", 0.10)]
    _st_c1 = dict(struct, heading_tree=[_hd(5, 1, "第2章 气体")], cross_page_splits=[],
                  cross_page_joins=[], pages=[5, 6])
    _full_c1, _ = build_markdown(_bl_c1, _st_c1, [], [], {}, "02")
    _split_c1 = chapters_mod.split_chapters(_st_c1, _bl_c1)
    _ps: dict = {}
    _parts = []
    for _i, _cc in enumerate(_split_c1["chapters"]):
        _t, _ = build_markdown(_bl_c1, _st_c1, [], [], {}, "02",
                               chapter_filter={b["block_id"] for b in _cc["blocks"]},
                               skip_header=(_i > 0), page_state=_ps)
        _parts.append(_t)
    _joined = "\n\n".join(p.rstrip("\n") for p in _parts) + "\n"
    _PM = re.compile(r"<!--\s*p(\d{3,4})\s*-->")
    _pages_of = lambda t: [int(x) for x in _PM.findall(t)]
    _body_of = lambda t: [re.sub(r"\s+", " ", ln).strip()
                          for ln in re.sub(r"<!--.*?-->", "", t, flags=re.S).split("\n") if ln.strip()]
    cases.append(("C1 多章：切出「章前 + 第2章」两个文件（页内块级切分）",
                  [c["file_stem"] for c in _split_c1["chapters"]] == ["chapter-00-front", "chapter-02"],
                  [c["file_stem"] for c in _split_c1["chapters"]]))
    cases.append(("C1 多章：**页标记集合完全相同**（不漏页/不重复页）",
                  _pages_of(_joined) == _pages_of(_full_c1),
                  {"joined": _pages_of(_joined), "single": _pages_of(_full_c1)}))
    cases.append(("C1 多章：**去注释后正文逐行相同**（内容不漏不重）",
                  _body_of(_joined) == _body_of(_full_c1), None))
    cases.append(("C1 多章：章标题块归**它自己那一章**（不被当上一章的尾巴）",
                  any(l.strip() == "# 第2章 气体" for l in _parts[1].splitlines()), None))
    cases.append(("C1 多章：产物标题与说明**只出现一次**（非首章不重复页头）",
                  sum(p.count("# 化学教材 Markdown 产物") for p in _parts) == 1, None))
    _single_ps: dict = {}
    _t_single, _ = build_markdown(_bl_c1, _st_c1, [], [], {}, "02",
                                  chapter_filter={b["block_id"] for b in _bl_c1},
                                  page_state=_single_ps)
    cases.append(("C1 单章：走 `chapter_filter` + `page_state` 路径时**逐字节等于**旧路径输出",
                  _t_single == _full_c1, None))
    # 单章时 `chapter_no` 为 None，但产物是 `chapter-01-*` → **报告里的章号标签必须与文件名一致**
    _cc0 = _split_c1["chapters"][0]
    _lab = (f"{_cc0['chapter_no']:02d}" if _cc0["chapter_no"] is not None
            else _cc0["file_stem"].split("-")[1])
    cases.append(("C1：报告章号标签与文件名前缀一致（单章 None → 取文件名里的 01）",
                  _cc0["file_stem"] == "chapter-00-front" and _lab == "00", _lab))

    # 章锚（SPEC §3.8）：`cmd_run` 端到端把声明写进 `chapter_split.anchors`，且幽灵章被跳过
    import tempfile as _tf
    import shutil as _sh
    from types import SimpleNamespace as _NS7
    _td = _tf.mkdtemp(prefix="s7-anchor-")
    try:
        _abl = [_blk(1, 0, "paragraph", "第1页正文。", [0.1, 0.1, 0.9, 0.2]),
                _blk(2, 0, "heading", "第9章 幽灵", [0.1, 0.1, 0.9, 0.2]),
                _blk(2, 1, "paragraph", "目录条目。", [0.1, 0.2, 0.9, 0.3]),
                _blk(5, 0, "heading", "第2章 气体", [0.1, 0.1, 0.9, 0.2]),
                _blk(5, 1, "paragraph", "气体正文。", [0.1, 0.2, 0.9, 0.3]),
                _blk(6, 0, "paragraph", "下一页正文。", [0.1, 0.1, 0.9, 0.2])]
        _air = os.path.join(_td, "ir.jsonl")
        with open(_air, "w", encoding="utf-8", newline="\n") as _fh:
            for _b in _abl:
                _fh.write(json.dumps(_b, ensure_ascii=False) + "\n")
        _ast = os.path.join(_td, "structure.json")
        _astr = {"heading_tree": [
                     {"index": 0, "block_id": "mineru-p0002-b000-text", "page": 2, "level": 1,
                      "text": "第9章 幽灵", "full_text": "第9章 幽灵", "text_level": 2,
                      "suspect_list_item": False},
                     {"index": 1, "block_id": "mineru-p0005-b000-text", "page": 5, "level": 1,
                      "text": "第2章 气体", "full_text": "第2章 气体", "text_level": 2,
                      "suspect_list_item": False}],
                 "cross_page_joins": [], "pages": [1, 2, 5, 6],
                 "furniture": {"records": [], "counts": {}},
                 "footnotes": {"missing_definitions": [], "orphan_definitions": []},
                 "page_labels": {"1": 1, "2": 2, "5": 5, "6": 6}}
        with open(_ast, "w", encoding="utf-8", newline="\n") as _fh:
            json.dump(_astr, _fh, ensure_ascii=False)
        _aout = os.path.join(_td, "s7-out")
        _rc7 = cmd_run(_NS7(ir=_air, structure=_ast, out=_aout, book="fx", pdf=None,
                            questions=None, s4_hits=None, verdicts=None, out_root=_td,
                            anchors=[1, 5], layout=[{"pages": [[1, 6]], "kind": "单栏",
                                                     "regions": 1, "cuts": []}],
                            toc=[2], input_kind="book_sequence"))
        _arep = json.load(open(os.path.join(_aout, "fx", "report.json"), encoding="utf-8"))
        cases.append(("章锚+版式+目录：cmd_run 端到端写入 anchors/layout/toc 并跳过目录章",
                      _rc7 == 0 and _arep["chapter_split"]["anchors"] == [1, 5]
                      and len(_arep["chapters"]) == 2
                      and _arep["layout"]["declared"]["1"]["regions"] == 1
                      and _arep["chapter_split"].get("toc_pages") == [2]
                      and len(_arep["chapter_split"].get("toc_skipped") or []) == 1,
                      {"anchors": _arep["chapter_split"].get("anchors"),
                       "layout": _arep.get("layout"),
                       "chapters": [c.get("file") for c in _arep.get("chapters", [])]}))
    finally:
        _sh.rmtree(_td, ignore_errors=True)

    cases.append(("U-15 图号：横线/点都认；正文引用不算题注；多号共用 → 不编号",
                  _figno_of("图 1-1") == "1.1"
                  and _caption_figno("图 1-2 二氯甲烷的球棍模型") == "1.2"
                  and _caption_figno("如图 1-2 所示，二氯甲烷的模型") is None
                  and _figno_of("图 1-1 A 图 1-2 B") is None, None))

    _old = _blk(1, 10, "figure", "", [0.30, 0.60, 0.45, 0.70],
                kids=[_blk(1, 10, "caption", "叁键", [0.30, 0.60, 0.45, 0.70],
                           field="image_caption", role="child",
                           parent="mineru-p0001-b010-text")])
    _v = [{"item_id": "S3-P0001-FIG-001", "pdf_page": 1, "review_type": "figure_region",
           "verdict": "exists", "bbox_norm": [0.44, 0.60, 0.66, 0.70],
           "caption_hint": "叁键", "confidence": 0.95,
           "judge": {"model": "test-model", "mode": "session"}}]
    _blocks = [_old]
    _synth, _reassigned, _skipped = apply_figure_verdicts(_blocks, _v)
    cases.append(("U-15 exists → 合成 figure 块 + 题注从原图摘走",
                  len(_synth) == 1 and len(_reassigned) == 1
                  and not _old.get("_children")
                  and _synth[0]["caption"] == "叁键", (_synth, _reassigned)))

    _figs = {_synth[0]["block_id"]: {"file": "figures/fig-p0001-v01.png"}}
    _md, _ms = build_markdown(_blocks, struct, [], [], _figs, "01")
    cases.append(("U-15 物化：VISION-FIGURE + 图片 + 题注只出现一次",
                  "<!-- VISION-FIGURE page=1" in _md
                  and "![叁键](figures/fig-p0001-v01.png)" in _md
                  and _md.count("叁键") >= 2 and _ms["vision_figures"] == 1, _ms))

    _synth2, _, _skipped2 = apply_figure_verdicts([], [{"item_id": "X", "pdf_page": 1,
                                                           "review_type": "figure_region",
                                                           "verdict": "exists"}])
    cases.append(("U-15 exists 缺 bbox → skipped（不静默）",
                  not _synth2 and bool(_skipped2), _skipped2))

    _f1 = _blk(1, 20, "figure", "", [0.30, 0.60, 0.40, 0.70])
    _f2 = _blk(1, 21, "figure", "", [0.50, 0.60, 0.60, 0.70])
    _f3 = _blk(1, 22, "figure", "", [0.70, 0.60, 0.80, 0.70])
    _out, _ch, _amb = reorder_figure_rows([_f2, _f1, _f3])
    cases.append(("U-15 图行：同一行按 x 重排（不动正文）",
                  [b["block_id"] for b in _out] == [_f1["block_id"], _f2["block_id"], _f3["block_id"]]
                  and len(_ch) == 1 and not _amb, _ch))
    _s1 = _blk(1, 23, "figure", "", [0.20, 0.10, 0.80, 0.30])
    _s2 = _blk(1, 24, "figure", "", [0.20, 0.35, 0.80, 0.55])
    _out2, _ch2, _ = reorder_figure_rows([_s2, _s1])
    cases.append(("U-15 图行：上下叠放（x 重叠）不重排",
                  [b["block_id"] for b in _out2] == [_s2["block_id"], _s1["block_id"]]
                  and not _ch2, _ch2))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S7 tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    for a in ("--ir", "--structure", "--out", "--book"):
        r.add_argument(a, required=True)
    r.add_argument("--pdf"); r.add_argument("--questions"); r.add_argument("--s4-hits")
    r.add_argument("--verdicts")
    r.add_argument("--input-kind", dest="input_kind", default="",
                   choices=["", "book_sequence", "partial_range"],
                   help="输入性质（C2-3）：合并后的完整书级序列=book_sequence；不连续/只交一部分=partial_range")
    r.add_argument("--anchors", default="",
                   help="章锚（SPEC §3.8）：**源 PDF 物理页号**、逗号分隔、严格递增；缺省时按 --book 从注册表回退；不传=行为不变")
    r.add_argument("--layout", default="",
                   help="版式声明（SPEC §3.16）：单栏/两栏/主副栏/三栏；页号=源 PDF 物理页号；缺省时按 --book 从注册表回退")
    r.add_argument("--toc", default="",
                   help="目录页（SPEC §3.8）：源 PDF 物理页号、逗号分隔；这些页的章标题不进切章器")
    # C2（SPEC §3.9）：书级编排把 `volumes`/`merge` 元数据喂进来，落到 report.json（只增字段）
    r.add_argument("--in", dest="extra_in", default="",
                   help="JSON 文件或字面量：额外并入 report.json 的顶层字段（如 {\"volumes\":[...],\"merge\":{...}}）")
    r.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
