# -*- coding: utf-8 -*-
"""**版式声明助手**（只读；给 agent 备料用，不是给人敲的配置工具）。

两段式的第二段用：引擎跑完、IR 出来后，
  1. `suggest` 从**块坐标**算每页候选（区数 + 切分线），给一段可粘贴的 `--layout`，
     并生成一段**人话问法**（不含 IR/术语），agent 照它问用户；
  2. 用户确认后，agent 用 `intake.py layout --book <key> --layout "…"` 落盘；
  3. 想先看效果就 `preview`：并排打印「未声明顺序 vs 声明后顺序」。

**纪律**：
  * 只读：**从不写 books.json、从不改产物**；
  * 只给候选：不替人判断「这是主副栏还是两栏」，判断在用户；
  * 页号：有注册表时用 `page_labels` 换算成**源 PDF 物理页号**；只有 `--ir` 时按 IR 页序，
    并明确提示「未关联注册表，页号可能不是源页号」。

用法：
  python -X utf8 layout_declare.py suggest --book mybook [--pages 11-21]
  python -X utf8 layout_declare.py suggest --ir s2-ir/ir.mineru.mybook.jsonl
  python -X utf8 layout_declare.py preview --book mybook --layout "1-10:单栏, 11-21:主副栏@0.275"
  python -X utf8 layout_declare.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
import books as books_mod  # noqa: E402
import s3_compare  # noqa: E402
import s6_assemble as s6  # noqa: E402
import intake  # noqa: E402

_SIDE_MAX_W = 0.24          # 窄侧栏单块最大宽度（归一化）
_SIDE_MIN_H = 0.08          # 窄侧栏最小高度覆盖（归一化）
MIN_SIDE_BLOCKS = 2          # 一个候选区至少这么多块，否则不算「分栏」
MIN_REGION_WIDTH = 0.08      # 一个候选区至少占页面这么宽
NARROW_RATIO = 1.6           # 两区宽度比 ≥ 此值 → 主副栏；否则两栏
MAX_REGIONS = 3
_COL = {0: "通", 1: "左", 2: "右"}


def content_blocks(blocks: list) -> list:
    return [b for b in blocks if b.get("role") == "primary"
            and b.get("kind") not in s6.FURNITURE and b.get("bbox_norm")]


def _rows(page_blocks: list) -> list:
    return [(b["block_id"], float(b["bbox_norm"][0]), float(b["bbox_norm"][2]))
            for b in page_blocks]


def _regions(rows: list, cuts: list) -> list:
    """按切分线把块分到各区；**有块横跨切分线就返回 None**（这种线不能声明）。"""
    regions = [[] for _ in range(len(cuts) + 1)]
    for bid, x0, x1 in rows:
        if any(x0 < c < x1 for c in cuts):
            return None
        regions[sum(1 for c in cuts if x0 >= c)].append((bid, x0, x1))
    return regions


def _gap_cuts(rows: list, k: int):
    """从**块区间的并集**里找栏缝（合并后的区间之间才是真空隙）。

    不用「x0 聚类的最大间隙」：主+边栏页里，窄栏自身可能有两个 x0（缩进/小标题），
    最大间隙会落在窄栏**内部**，切出来的线穿过块 → 这种线不能声明。
    """
    iv = sorted((x0, x1) for _b, x0, x1 in rows)
    merged = []
    for x0, x1 in iv:
        if merged and x0 <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    gaps = [((merged[i + 1][0] - merged[i][1]), (merged[i][1] + merged[i + 1][0]) / 2.0, i)
            for i in range(len(merged) - 1)]
    if len(gaps) < k - 1:
        return None
    picks = sorted(gaps, key=lambda g: -g[0])[:k - 1]
    picks.sort(key=lambda g: g[2])
    return [round(g[1], 4) for g in picks]


_SOFT_KINDS = ("figure", "table", "equation")   # 这些块**跨切分线是正常的**（通栏图/表）


def _regions_soft(page_blocks: list, cut: float):
    """按一条切分线分区；**图/表/公式跨线算通栏**（归到中心所在区），**文字块跨线则拒绝**。

    为什么：真书 p5 有一张横跨切分线的大图（0.137–0.895）——它本来就该留在正文里；严格版
    `_regions()` 会因为"有块跨线"直接判这条线不能用（实测 p5 因此被判成单栏）。
    """
    left, right = [], []
    for b in page_blocks:
        bb = b["bbox_norm"]
        x0, x1 = float(bb[0]), float(bb[2])
        if x0 < cut < x1:
            if (b.get("kind") or "") not in _SOFT_KINDS:
                return None
            (left if (x0 + x1) / 2.0 < cut else right).append(b)
            continue
        (right if x0 >= cut else left).append(b)
    if not left or not right:
        return None
    return [left, right]


def _side_edge_cuts(page_blocks: list) -> list:
    """**按"窄侧栏"识别切分线**（2026-11-15 / P1-2）。

    实测形态（真书 p2–p5）：左边一条**又高又窄**的块（旁注/图说），正文从它右边开始；
    `_gap_cuts()` 要求缝**贯穿整页**，这种页缝不贯穿 → 会被误判成单栏（统一切分线切进正文）。

    做法（纯几何、不看图）：把"窄块"（宽度 < `_SIDE_MAX_W` 且整块在一侧）按**边缘**（左取 `x1`、
    右取 `x0`）**聚类**；每个簇独立判断"够高（单块够高 或 多块合计够高）"；切分线取
    「簇边缘 与 正文宽块最近边」的中点，并要求**没有任何块横跨这条线**。

    为什么必须聚类：同一页还有图注里的小字块（宽 < 0.24、也落在左半页），它们的边缘与真窄栏
    差 0.15 以上；不聚类就会把中位数算到图注上（实测踩到：p2 得到 0.423）。
    """
    out = []
    rows = [(b["block_id"], float(b["bbox_norm"][0]), float(b["bbox_norm"][2])) for b in page_blocks]
    for side in ("left", "right"):
        cand = []
        for b in page_blocks:
            bb = b["bbox_norm"]
            x0, x1 = float(bb[0]), float(bb[2])
            h = float(bb[3]) - float(bb[1])
            if (x1 - x0) >= _SIDE_MAX_W:
                continue
            if side == "left" and not (x1 < 0.45 and x0 < 0.32):
                continue
            if side == "right" and not (x0 > 0.55 and x1 > 0.70):
                continue
            cand.append((b["block_id"], x0, x1, h))
        if not cand:
            continue
        edges = sorted(((c[2] if side == "left" else c[1]), c) for c in cand)
        clusters, cur = [], []
        for e, c in edges:
            if cur and abs(e - cur[-1][0]) > 0.02:
                clusters.append(cur)
                cur = []
            cur.append((e, c))
        if cur:
            clusters.append(cur)
        for cl in clusters:
            e0 = sorted(x[0] for x in cl)[len(cl) // 2]
            cover = sum(x[1][3] for x in cl)
            if len(cl) < 2 and cover < _SIDE_MIN_H:
                continue                      # 单个矮块 → 更像图注小字，不是窄栏
            if side == "left":
                others = [float(b["bbox_norm"][0]) for b in page_blocks
                          if (float(b["bbox_norm"][2]) - float(b["bbox_norm"][0])) >= 0.30
                          and float(b["bbox_norm"][0]) > e0]
                cut = (e0 + min(others)) / 2.0 if others else None
            else:
                others = [float(b["bbox_norm"][2]) for b in page_blocks
                          if (float(b["bbox_norm"][2]) - float(b["bbox_norm"][0])) >= 0.30
                          and float(b["bbox_norm"][2]) < e0]
                cut = (e0 + max(others)) / 2.0 if others else None
            if cut is None or not (0.03 < cut < 0.97):
                continue
            regions = _regions_soft(page_blocks, round(cut, 4))
            if regions is None or any(len(r) < 1 for r in regions):
                continue
            out.append((round(cut, 4), side, round(e0, 4), len(cl)))
    return out


def candidates(page_blocks: list) -> dict:
    """一页的版式候选（纯函数）：先试 3 区，再试 2 区，都不成立就是单栏。"""
    rows = _rows(page_blocks)
    if len(rows) < MIN_SIDE_BLOCKS * 2:
        return {"regions": 1, "kind": "单栏", "cuts": [], "widths": [1.0],
                "counts": [len(rows)], "strong": True,
                "note": "块太少（<%d），按单栏" % (MIN_SIDE_BLOCKS * 2)}
    # 2026-11-15（P1-2）：**先试"窄侧栏边缘对齐"**（部分高度窄栏靠它），再回退到贯穿缝隙算法
    for cut, side, edge, n in _side_edge_cuts(page_blocks):
        regions = _regions_soft(page_blocks, cut)
        # 窄侧可能只有**一个**高块（旁注/图说）→ 不对窄侧要求 MIN_SIDE_BLOCKS，只要求两侧都非空，
        # 且**正文侧**至少 MIN_SIDE_BLOCKS 块（否则更像装订线/页边空白，不是分栏）。
        if regions is None or any(len(r) < 1 for r in regions):
            continue
        _body = max(regions, key=len)
        if len(_body) < MIN_SIDE_BLOCKS:
            continue
        widths = []
        for _r in regions:
            _x0s = [float(b["bbox_norm"][0]) for b in _r]
            _x1s = [float(b["bbox_norm"][2]) for b in _r]
            widths.append(max(_x1s) - min(_x0s))
        if min(widths) < MIN_REGION_WIDTH:
            continue
        ratio = max(widths) / max(min(widths), 1e-6)
        kind = "主副栏" if ratio >= NARROW_RATIO else "两栏"
        return {"regions": 2, "kind": kind, "cuts": [cut],
                "widths": [round(w, 4) for w in widths], "counts": [len(r) for r in regions],
                "strong": True,
                "note": ("两区宽度 " + " / ".join("%.2f" % w for w in widths)
                         + "（窄侧在%s）；按窄侧栏边缘对齐（%s 侧，边缘 %.3f，%d 块共线）"
                         % ("左" if side == "left" else "右", side, edge, n))}
    for k in range(MAX_REGIONS, 1, -1):
        cuts = _gap_cuts(rows, k)
        if not cuts:
            continue
        regions = _regions(rows, cuts)
        if regions is None:
            continue
        if any(len(r) < MIN_SIDE_BLOCKS for r in regions):
            continue
        widths = [max(x1 for _b, _x0, x1 in r) - min(x0 for _b, x0, _x1 in r)
                  for r in regions]
        if min(widths) < MIN_REGION_WIDTH:
            continue
        if k == 3 and min(widths) < 0.10:
            continue
        counts = [len(r) for r in regions]
        if k == 2:
            ratio = max(widths) / max(min(widths), 1e-6)
            kind = "主副栏" if ratio >= NARROW_RATIO else "两栏"
            note = ("两区宽度 %.2f / %.2f（窄侧在%s）"
                    % (widths[0], widths[1], "左" if widths[0] < widths[1] else "右"))
        else:
            kind = "三栏"
            note = "三区宽度 %s" % " / ".join("%.2f" % w for w in widths)
        return {"regions": k, "kind": kind, "cuts": [round(c, 4) for c in cuts],
                "widths": [round(w, 4) for w in widths], "counts": counts,
                "strong": all(c >= 3 for c in counts), "note": note}
    return {"regions": 1, "kind": "单栏", "cuts": [], "widths": [1.0],
            "counts": [len(rows)], "strong": True, "note": "没有干净的栏缝"}


def analyze(blocks: list) -> dict:
    """逐页候选：`{page: candidate}`。"""
    by_page = {}
    for b in content_blocks(blocks):
        by_page.setdefault(b["page"], []).append(b)
    return {p: candidates(bs) for p, bs in sorted(by_page.items())}


def _seg_text(pages: list, cand: dict) -> str:
    a, b = pages[0], pages[-1]
    span = str(a) if a == b else "%d-%d" % (a, b)
    cut = ("@" + "|".join("%g" % c for c in cand["cuts"])) if cand.get("cuts") else ""
    return "%s:%s%s" % (span, cand["kind"], cut)


def spec_from(analysis: dict, src_of=None) -> str:
    """把逐页候选压成一段 `--layout` 字符串（**覆盖全部页**）。"""
    src_of = src_of or (lambda p: p)
    items = [(src_of(p), analysis[p]) for p in sorted(analysis)]
    segs, cur = [], None
    for src, cand in items:
        sig = (cand["kind"], tuple(cand.get("cuts") or []))
        if cur and cur["sig"] == sig and src == cur["pages"][-1] + 1:
            cur["pages"].append(src)
        else:
            cur = {"sig": sig, "pages": [src], "cand": cand}
            segs.append(cur)
    return ", ".join(_seg_text(s["pages"], s["cand"]) for s in segs)


def _pages_text(pages: list) -> str:
    rs, start = [], pages[0]
    prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        rs.append((start, prev))
        start = prev = p
    rs.append((start, prev))
    return "、".join(("%d–%d" % (a, b)) if a != b else str(a) for a, b in rs)


def _ask_line(pages: list, cand: dict) -> str:
    span = "第 %s 页" % _pages_text(pages)
    if cand["kind"] == "单栏":
        return "%s：从上到下一行行读就行，不用特别处理。" % span
    if cand["kind"] == "主副栏":
        narrow = "左" if cand["widths"][0] < cand["widths"][1] else "右"
        return ("%s：OCR 显示一边是正文、%s边有一条窄栏（窄栏约 %.0f%% 宽）。"
                "要按「先读完正文、再读%s边窄栏」排吗？"
                % (span, narrow, min(cand["widths"]) * 100, narrow))
    if cand["kind"] == "两栏":
        return ("%s：OCR 显示左右两栏差不多宽（约 %.0f%% / %.0f%%）。"
                "要按「左栏读到底、再读右栏」排吗？"
                % (span, cand["widths"][0] * 100, cand["widths"][1] * 100))
    return ("%s：OCR 显示三栏并排（约 %.0f%% / %.0f%% / %.0f%%）。"
            "要按「左→中→右」排吗？"
            % (span, cand["widths"][0] * 100, cand["widths"][1] * 100,
               cand["widths"][2] * 100))


def ask_draft(analysis: dict, src_of=None) -> str:
    """生成一段可照读的人话问法（**不用 IR 这类词**）。

    按「版式种类」归组（不要求页号连续）——用户看到的是
    「第 11、14–15、17–18、20–21 页：…」而不是 20 行逐页复读。
    """
    src_of = src_of or (lambda p: p)
    groups: dict = {}
    for p in sorted(analysis):
        cand = analysis[p]
        key = cand["kind"]          # 只按种类归组：切分线每页略有不同，用户不必逐页听
        g = groups.setdefault(key, {"kind": cand["kind"], "cand": cand, "pages": []})
        g["pages"].append(src_of(p))
    order = {"主副栏": 0, "两栏": 1, "三栏": 2, "单栏": 3}
    lines = []
    for g in sorted(groups.values(),
                    key=lambda x: (order.get(x["kind"], 9), x["pages"][0])):
        pages = sorted(g["pages"])
        # 单栏只把「不是别的组」的页收进来：这里按组内页列即可，用户看得懂
        lines.append("- " + _ask_line(pages, g["cand"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 载入
def find_ir(book: str = "", ir: str = "", root: str = "") -> tuple:
    """返回 `(ir_path, page_labels, key)`；book 优先从注册表找 IR。"""
    root = root or paths.data_root()
    if ir:
        if not os.path.exists(ir):
            raise SystemExit("IR 不存在：%s" % ir)
        return os.path.abspath(ir), None, os.path.basename(ir).replace("ir.mineru.", "").replace(".jsonl", "")
    if not book:
        raise SystemExit("必须给 --book 或 --ir")
    reg = books_mod.load(root=root)
    if book not in reg:
        raise SystemExit("注册表里没有这本书：%s" % book)
    entry = reg[book]
    for name in ("ir.mineru.%s.jsonl" % book, "ir.mineru.%s.book.jsonl" % book):
        p = os.path.join(root, "s2-ir", name)
        if os.path.exists(p):
            return p, entry.get("page_labels"), book
    raise SystemExit("找不到 %s 的 IR（应先跑引擎并 build；期望 s2-ir/ir.mineru.%s.jsonl）"
                     % (book, book))


def _parse_pages(spec: str) -> list:
    if not spec:
        return []
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _src2ir(page_labels, max_ir: int) -> dict:
    if page_labels:
        return {int(p): i + 1 for i, p in enumerate(page_labels)}
    return {p: p for p in range(1, max_ir + 1)}


def cmd_suggest(a) -> int:
    ir, labels, key = find_ir(a.book, a.ir, a.root)
    blocks = s3_compare.load_ir(ir)
    analysis = analyze(blocks)
    src_of = (lambda p: int(labels[p - 1])) if labels else (lambda p: p)
    want = _parse_pages(a.pages) or sorted(analysis)
    print("== 版式候选（只读；请人工确认）==")
    print("书：%s   IR：%s（%d 页）" % (key, ir, len(analysis)))
    if not labels:
        if a.book:
            print("⚠ 该书没有 page_labels → 页号按 IR 页序（= 切片页）")
        else:
            print("⚠ 未关联注册表：页号按 IR 页序；若这本书不是从第 1 页切起，请改用 --book。")
    print("\n%-6s %-6s %-14s %s" % ("页", "块数", "候选", "依据"))
    for p in want:
        cand = analysis.get(p)
        if not cand:
            print("%-6d %-6s %-14s %s" % (src_of(p), "-", "无正文块", ""))
            continue
        print("%-6d %-6d %-14s %s" % (src_of(p), sum(cand["counts"]),
                                      "%s%s" % (cand["kind"],
                                                ("@" + "|".join("%g" % c for c in cand["cuts"]))
                                                if cand["cuts"] else ""),
                                      cand["note"]))
    print("\n== 可直接粘贴的声明（覆盖全部页）==")
    spec = spec_from(analysis, src_of)
    print('--layout "%s"' % spec)
    print("\n== 可照读的问法（给用户；不含术语）==")
    print(ask_draft(analysis, src_of))
    print("\n下一步：用户确认后 → python _mcp-test/intake.py layout --book %s --layout \"%s\""
          % (key, spec))
    return 0


def cmd_preview(a) -> int:
    ir, labels, key = find_ir(a.book, a.ir, a.root)
    blocks = s3_compare.load_ir(ir)
    content = content_blocks(blocks)
    max_ir = max(b["page"] for b in content)
    src_pages = list(labels) if labels else list(range(1, max_ir + 1))
    if a.layout:
        segs = intake.parse_layout(a.layout, src_pages)
    elif a.book:                       # 没给 --layout 就读 books.json 里已存的声明
        entry = books_mod.load(root=a.root or paths.data_root()).get(a.book) or {}
        segs = entry.get("layout")
        if not segs:
            raise SystemExit("这本书还没有版式声明；先给 --layout（或用 intake.py layout 落盘）")
    else:
        raise SystemExit("必须给 --layout（或用 --book 读已存声明）")
    if not segs:
        raise SystemExit("--layout 为空")
    s2i = _src2ir(labels, max_ir)
    declared = {}
    for seg in segs:
        for rng in seg["pages"]:
            for src in range(rng[0], rng[1] + 1):
                if src not in s2i:
                    raise SystemExit("声明页 %d 不在本书 page_labels 里" % src)
                declared[s2i[src]] = {"regions": seg["regions"], "cuts": seg["cuts"],
                                      "kind": seg["kind"]}
    cols = s6.page_columns(content, declared=declared)
    base = s6.page_columns(content)
    want = [s2i[s] for s in _parse_pages(a.pages) if s in s2i] or sorted(declared)
    print("== 声明预览（只读）==")
    print("书：%s   IR：%s" % (key, ir))
    for p in want:
        if p not in declared:
            print("\np%d —— 未在本次声明里" % p)
            continue
        bs = [b for b in content if b["page"] == p]
        if not bs:
            print("\np%d —— 无正文块" % p)
            continue
        d = declared[p]
        print("\np%d → 声明：%s%s" % (p, d["kind"],
                                      ("@" + "|".join("%g" % c for c in d["cuts"]))
                                      if d["cuts"] else ""))
        order_d = sorted(bs, key=s6.reading_order_key(content, cols))
        order_b = sorted(bs, key=s6.reading_order_key(content, base))
        # 单栏声明没有「左/右」可言：两版都打「·」，避免把基线标签（通/左）当结论
        lab = (cols.get(p) or {}).get("col", {}) if d["regions"] > 1 else {}
        print("  未声明（今天的行为）：")
        for b in order_b[:a.n]:
            print("    [%s] %s" % (_COL.get(lab.get(b["block_id"], 0), "·"),
                                  " ".join((b.get("text_raw") or "").split())[:34]))
        print("  声明后：")
        for b in order_d[:a.n]:
            print("    [%s] %s" % (_COL.get(lab.get(b["block_id"], 0), "·"),
                                  " ".join((b.get("text_raw") or "").split())[:34]))
        for m in (cols.get(p) or {}).get("mismatch") or []:
            print("  ⚠ 声明自检：%s" % m)
            # 2026-11-15（P1-2）：切进块里时，**直接给最近的空白缝**（纯几何，不用看图）
            if (m.get("why") or "") == "cut_inside_block":
                _c = float(m.get("cut") or 0)
                _gaps = []
                for b in bs:
                    bb = b["bbox_norm"]
                    _gaps += [float(bb[0]), float(bb[2])]
                _near = sorted(_gaps, key=lambda g: abs(g - _c))[:2]
                if _near:
                    print("     ↳ 最近的块边界：%s（建议切分线 = 相邻两块之间，如 %.3f）"
                          % (" / ".join("%.3f" % g for g in _near), sum(_near) / 2.0))
    return 0


# ---------------------------------------------------------------- selftest
def _blk(page, i, x0, x1, order, text="t", y=None):
    # `y` 可以是单个 y0（高度固定 0.04），也可以是 (y0, y1) —— 后者用于"又高又窄的旁注块"
    if isinstance(y, (list, tuple)) and len(y) == 2:
        y0, y1 = float(y[0]), float(y[1])
    else:
        y0 = 0.10 + 0.05 * i if y is None else float(y)
        y1 = y0 + 0.04
    return {"block_id": "b-%d-%d" % (page, i), "page": page, "order": order,
            "kind": "paragraph", "role": "primary",
            "bbox_norm": [x0, y0, x1, y1],
            "text_raw": text, "text_canon": text}


def selftest() -> int:
    cases = []
    # 单栏：没有干净栏缝
    one = [_blk(1, i, 0.05, 0.95, i) for i in range(4)]
    c1 = candidates(one)
    # 2026-11-15（P1-2）：**部分高度窄栏**（旁注：一条又高又窄的块）→ 用"边缘对齐"给出切分线
    _side_blocks = [
        _blk(1, 30, 0.05, 0.23, 1, "旁注：这一条很窄但很高", y=(0.20, 0.70)),
        _blk(1, 31, 0.26, 0.95, 2, "正文第一段", y=(0.10, 0.18)),
        _blk(1, 32, 0.26, 0.95, 3, "正文第二段", y=(0.20, 0.28)),
        _blk(1, 33, 0.26, 0.95, 4, "正文第三段", y=(0.72, 0.80)),
    ]
    _c_side = candidates(_side_blocks)
    cases.append(("2026-11-15（P1-2）：部分高度窄栏（1 条高窄块）→ 主副栏 + 切分线在窄栏右缘与正文左缘之间",
                  _c_side["regions"] == 2 and len(_c_side["cuts"]) == 1
                  and 0.23 < _c_side["cuts"][0] < 0.26, _c_side))
    _with_fig = [dict(b) for b in _side_blocks] + [
        dict(_blk(1, 34, 0.13, 0.90, 5, "通栏大图", y=(0.40, 0.52)), kind="figure")]
    _c_fig = candidates(_with_fig)
    cases.append(("2026-11-15（P1-2）：**通栏图/表跨切分线** → 仍可声明（文字块跨线才拒绝）",
                  _c_fig["regions"] == 2 and bool(_c_fig["cuts"]), _c_fig))
    _text_cross = [dict(b) for b in _side_blocks] + [
        _blk(1, 35, 0.10, 0.80, 6, "一条横跨切分线的正文段", y=(0.85, 0.95))]
    _c_cross = candidates(_text_cross)
    cases.append(("2026-11-15（P1-2）负例：**文字块**跨切分线 → 不采用那条线",
                  _c_cross["cuts"] != _c_side["cuts"], _c_cross))
    cases.append(("单栏：不给切分线", c1["kind"] == "单栏" and not c1["cuts"], c1))
    # 对称两栏：宽度接近 → 两栏@0.5
    two = ([_blk(1, i, 0.05, 0.45, i) for i in range(3)]
           + [_blk(1, i + 3, 0.55, 0.95, i + 3) for i in range(3)])
    c2 = candidates(two)
    cases.append(("对称两栏：两栏 + 切分线≈0.5",
                  c2["kind"] == "两栏" and len(c2["cuts"]) == 1
                  and abs(c2["cuts"][0] - 0.5) < 0.02, c2))
    # 主+边栏：宽度比大 → 主副栏，窄侧在左（窄条顶边比正文略高，用来区分两种顺序）
    ms = ([_blk(1, i, 0.28, 0.96, i, y=0.12 + 0.05 * i) for i in range(4)]
          + [_blk(1, i + 4, 0.08, 0.27, i + 4, y=0.09 + 0.05 * i) for i in range(3)])
    c3 = candidates(ms)
    cases.append(("主+边栏：主副栏 + 窄侧在左 + 切分线≈0.275",
                  c3["kind"] == "主副栏" and "左" in c3["note"]
                  and abs(c3["cuts"][0] - 0.275) < 0.02, c3))
    # 三栏
    three = ([_blk(1, i, 0.05, 0.31, i) for i in range(2)]
             + [_blk(1, i + 2, 0.35, 0.61, i + 2) for i in range(2)]
             + [_blk(1, i + 4, 0.65, 0.95, i + 4) for i in range(2)])
    c4 = candidates(three)
    cases.append(("三栏：三栏 + 两条切分线",
                  c4["kind"] == "三栏" and len(c4["cuts"]) == 2, c4))
    # 跨栏块（通栏标题）→ 不硬切
    span = two + [_blk(1, 9, 0.05, 0.95, 9, "通栏标题", y=0.02)]
    c5 = candidates(span)
    cases.append(("负例：有块横跨栏缝 → 不硬切（退回单栏）",
                  c5["kind"] == "单栏" and not c5["cuts"], c5))
    # 生成 spec 且能被唯一解析器接受
    a = {1: candidates(one), 2: candidates(one),
         3: candidates(ms), 4: candidates(ms)}
    spec = spec_from(a)
    ok = True
    try:
        segs = intake.parse_layout(spec, [1, 2, 3, 4])
        ok = (len(segs) == 2 and segs[0]["kind"] == "单栏"
              and segs[1]["kind"] == "主副栏")
    except Exception as exc:                                        # noqa: BLE001
        ok, spec = False, "%s / %s" % (spec, exc)
    cases.append(("spec 覆盖全部页且被 parse_layout 接受", ok, spec))
    # 问法：不含 IR / agent 这类词，且说人话
    q = ask_draft(a)
    cases.append(("人话问法：不含「IR」「agent」，且用「窄栏」描述",
                  "IR" not in q and "agent" not in q and "窄栏" in q, q))
    # 声明预览真的换了顺序（主栏先 vs 未声明按 y）
    blocks = ms
    content = [b for b in blocks]
    cols = s6.page_columns(content, declared={1: {"regions": 2, "cuts": c3["cuts"],
                                                  "kind": "主副栏"}})
    base = s6.page_columns(content)
    od = [b["block_id"] for b in sorted(content, key=s6.reading_order_key(content, cols))]
    ob = [b["block_id"] for b in sorted(content, key=s6.reading_order_key(content, base))]
    cases.append(("声明后首块是主栏首块、顺序与未声明不同",
                  od[0] == "b-1-0" and od != ob, (od[:3], ob[:3])))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:160]))
        passed += bool(ok)
    print("\n%d/%d layout_declare tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("suggest", "preview"):
        sp = sub.add_parser(name)
        sp.add_argument("--book", default="")
        sp.add_argument("--ir", default="")
        sp.add_argument("--pages", default="", help="只看这些页（缺省=全部）")
        sp.add_argument("--root", default="")
        if name == "preview":
            sp.add_argument("--layout", default="",
                            help="缺省时用 --book 在 books.json 里已存的声明")
            sp.add_argument("--n", type=int, default=6, help="每版打印几块")
        sp.set_defaults(func=cmd_suggest if name == "suggest" else cmd_preview)
    sub.add_parser("selftest").set_defaults(func=lambda _a: selftest())
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
