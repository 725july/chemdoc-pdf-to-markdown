#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图区问责（figcov）—— **只读探针**（不进管线、不写产物、不调模型）。

给 02 开发树做“先量、再接线”的第一步：只读两套 IR（MinerU / Paddle），
在 89 页既有语料上复现候选表；**不读 PDF、不渲染、不写 s2/s3/s5**。
算法与口径见 `docs/new/图缺陷检测-方案.md` 附录 C。

算法：
  1. 对每个 Paddle figure 框 F，剔除题注带后得到内容区，在内容区内铺网格；
  2. 把任何 MinerU 块（text/equation/table/figure）盖住的格子标为 covered
     （figure 块给非对称容差：左右 0.003、上 0.01、下 0.02，避免裁图偏紧或题注前的空白造成假洞）；
  3. 把 F 内的 Paddle 题注格标为 ignored（题注不是图内容）；
  4. 剩下的连通格 = 洞；洞占**内容区** ≥ min_frac、整页面积 ≥ min_page_area、
     宽高 ≥ min_side → 候选；
  5. 用 Paddle 图题给候选配 caption_hint（题注中心落在洞的 x 范围、y 在洞的下沿附近）；
  6. 候选之间重叠 ≥ 50% 去重。**与已有 S3 问题的去重不在这里做**（本探针不读 S3；
     接进 `s3_compare` 时补，见方案 §C.3 第 10 条）。

默认阈值：`min_frac = 0.30`（2026-10-24 定：p4 真命中 0.54，binwei p1 logo 0.24；
25% 离负例只有 1 个百分点，30% 留出余量，且仍低于 3 等分一排图缺 1 个的 ~0.33）。

用法：
  python figcov_probe.py --mineru ir.mineru.<书>.jsonl --paddle ir.paddle.<书>.jsonl
  python figcov_probe.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import deque

from contracts import FURNITURE

LABEL_RE = re.compile(r"^[\(（]?\s*(?:[ivxlcdm]+|\d+|[a-zA-Z])\s*[\)）]?$", re.I)
TABLE_RE = re.compile(r"^\s*(?:表|table)", re.I)


# ---------------------------------------------------------------- 基础
def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def valid_bbox(bb) -> bool:
    return (isinstance(bb, (list, tuple)) and len(bb) == 4
            and all(isinstance(v, (int, float)) for v in bb)
            and bb[2] > bb[0] and bb[3] > bb[1]
            and bb[0] >= -0.01 and bb[1] >= -0.01
            and bb[2] <= 1.01 and bb[3] <= 1.01)


def rect_overlap(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def overlap_ratio(a, b) -> float:
    """交集 / 较小面积（用于去重）。"""
    inter = rect_overlap(a, b)
    if inter <= 0:
        return 0.0
    return inter / max(1e-9, min((a[2] - a[0]) * (a[3] - a[1]),
                                 (b[2] - b[0]) * (b[3] - b[1])))


def attach_eff(blocks: list[dict]) -> None:
    """把子块并进父块的有效 bbox（MinerU 的图题/表题是子块，自己没有独立 bbox）。"""
    kids: dict[str, list[dict]] = {}
    for b in blocks:
        if b.get("role") == "child" and b.get("parent_id"):
            kids.setdefault(b["parent_id"], []).append(b)
    for b in blocks:
        boxes = [b.get("bbox_norm")] + [k.get("bbox_norm") for k in kids.get(b.get("block_id"), [])]
        boxes = [x for x in boxes if valid_bbox(x)]
        if boxes:
            b["_figcov_bbox"] = [min(x[0] for x in boxes), min(x[1] for x in boxes),
                         max(x[2] for x in boxes), max(x[3] for x in boxes)]


# ---------------------------------------------------------------- 网格 + 连通域
def _cell_range(a: float, b: float, base: float, step: float, n: int) -> tuple[int, int]:
    i0 = max(0, min(n, int(math.floor((a - base) / step))))
    i1 = max(0, min(n, int(math.ceil((b - base) / step))))
    return i0, i1


def hole_components(F, mblocks, pcaps, grid: int):
    """返回 F 内的洞（连通域）列表：[{bbox, cells, area_frac, area_page}]。

    F 的**题注带**先被剔除：Paddle 的图框常把题注行包进来（第 4 页 F 到 y=0.726，
    题注在 0.709–0.723）。题注不是图内容，留着会把题注行算成洞，也会把题注裁进图里。
    """
    fx0, fy0, fx1, fy1 = F
    # 题注带上沿：F 内最靠上的题注 y0
    cap_tops = [c["bbox_norm"][1] for c in pcaps
                if valid_bbox(c.get("bbox_norm")) and rect_overlap(c["bbox_norm"], F) > 0
                and fy0 <= c["bbox_norm"][1] <= fy1 + 0.02]
    content_bottom = min(fy1, min(cap_tops) - 0.002) if cap_tops else fy1
    if content_bottom <= fy0 + 1e-6:
        return []
    content_area = (fx1 - fx0) * (content_bottom - fy0)
    gx = (fx1 - fx0) / grid
    gy = (content_bottom - fy0) / grid
    state = [[0] * grid for _ in range(grid)]   # 0 free / 1 covered / 2 ignored

    for b in mblocks:
        bb = b.get("_figcov_bbox") or b.get("_bbox_eff") or b.get("bbox_norm")
        if not valid_bbox(bb):
            continue
        # 图块：左右 0.003、上 0.01、下 0.02 的容差。
        # 下沿加大是为了吞掉“结构式与题注之间那条空白”：它会把真正的洞和两侧的空白连成一片。
        if b.get("kind") == "figure":
            x0 = max(fx0, bb[0] - 0.003); x1 = min(fx1, bb[2] + 0.003)
            y0 = max(fy0, bb[1] - 0.01); y1 = min(content_bottom, bb[3] + 0.02)
        else:
            x0 = max(fx0, bb[0]); x1 = min(fx1, bb[2])
            y0 = max(fy0, bb[1]); y1 = min(content_bottom, bb[3])
        if x1 <= x0 or y1 <= y0:
            continue
        i0, i1 = _cell_range(x0, x1, fx0, gx, grid)
        j0, j1 = _cell_range(y0, y1, fy0, gy, grid)
        for j in range(j0, j1):
            row = state[j]
            for i in range(i0, i1):
                row[i] = 1

    for c in pcaps:
        cb = c.get("bbox_norm")
        if not valid_bbox(cb) or rect_overlap(cb, F) <= 0:
            continue
        x0 = max(fx0, cb[0]); x1 = min(fx1, cb[2])
        y0 = max(fy0, cb[1]); y1 = min(content_bottom, cb[3])
        i0, i1 = _cell_range(x0, x1, fx0, gx, grid)
        j0, j1 = _cell_range(y0, y1, fy0, gy, grid)
        for j in range(j0, j1):
            row = state[j]
            for i in range(i0, i1):
                if row[i] == 0:
                    row[i] = 2

    seen = [[False] * grid for _ in range(grid)]
    comps = []
    for j in range(grid):
        for i in range(grid):
            if state[j][i] != 0 or seen[j][i]:
                continue
            q = deque([(i, j)])
            seen[j][i] = True
            cells = 0
            min_i = max_i = i
            min_j = max_j = j
            while q:
                ci, cj = q.popleft()
                cells += 1
                min_i = min(min_i, ci); max_i = max(max_i, ci)
                min_j = min(min_j, cj); max_j = max(max_j, cj)
                for ni, nj in ((ci + 1, cj), (ci - 1, cj), (ci, cj + 1), (ci, cj - 1)):
                    if 0 <= ni < grid and 0 <= nj < grid and not seen[nj][ni] and state[nj][ni] == 0:
                        seen[nj][ni] = True
                        q.append((ni, nj))
            bbox = [round(fx0 + min_i * gx, 4), round(fy0 + min_j * gy, 4),
                    round(fx0 + (max_i + 1) * gx, 4), round(fy0 + (max_j + 1) * gy, 4)]
            area = cells * gx * gy
            comps.append({"bbox": bbox, "cells": cells, "area": area,
                          "area_frac": area / max(1e-9, content_area)})
    return comps


# ---------------------------------------------------------------- 题注
def pick_caption(H, pcaps):
    """给洞 H 找题注：x 落在洞内（或重叠 ≥ 50%），y 在洞下沿或上沿附近。"""
    best = None
    for c in pcaps:
        txt = (c.get("text_raw") or "").strip()
        if not txt or LABEL_RE.match(txt) or TABLE_RE.match(txt):
            continue
        cb = c.get("bbox_norm")
        if not valid_bbox(cb):
            continue
        cx = (cb[0] + cb[2]) / 2.0
        x_ok = ((H[0] - 0.01) <= cx <= (H[2] + 0.01)
                or (min(H[2], cb[2]) - max(H[0], cb[0])) > 0.5 * min(H[2] - H[0], cb[2] - cb[0]))
        if not x_ok:
            continue
        below = (H[3] - 0.03) <= cb[1] <= (H[3] + 0.12)
        above = (H[1] - 0.12) <= cb[3] <= (H[1] + 0.03)
        if not (below or above):
            continue
        dist = abs(cb[1] - H[3]) if below else abs(H[1] - cb[3])
        key = (round(dist, 6), abs(cx - (H[0] + H[2]) / 2.0))
        if best is None or key < best[0]:
            best = (key, txt, cb, c.get("block_id"))
    if not best:
        return None
    return {"text": best[1], "bbox": best[2], "block_id": best[3]}


# ---------------------------------------------------------------- 单页
def figure_candidates(page: int, mblocks: list[dict], pblocks: list[dict], *,
                      grid: int = 200, min_frac: float = 0.30,
                      min_page_area: float = 0.01, min_side: float = 0.02) -> tuple[list[dict], dict]:
    """返回 (候选, 统计)。mblocks/pblocks 都是同页 primary/非家具块，已 attach_eff。"""
    figs = [b for b in pblocks if b.get("kind") == "figure" and valid_bbox(b.get("bbox_norm"))]
    caps = [b for b in pblocks if b.get("kind") == "caption" and valid_bbox(b.get("bbox_norm"))]
    stats = {"figures": len(figs), "holes": 0, "skipped_frac": 0,
             "skipped_area": 0, "skipped_side": 0, "skipped_fullpage": 0, "skipped_tiny": 0}
    out = []
    for b in figs:
        F = b["bbox_norm"]
        area = (F[2] - F[0]) * (F[3] - F[1])
        if area <= 0:
            continue
        if area > 0.6:
            stats["skipped_fullpage"] += 1
            continue
        if area < 0.005:
            stats["skipped_tiny"] += 1
            continue
        for h in hole_components(F, mblocks, caps, grid):
            stats["holes"] += 1
            if h["area_frac"] < min_frac:
                stats["skipped_frac"] += 1
                continue
            if h["area"] < min_page_area:
                stats["skipped_area"] += 1
                continue
            bb = h["bbox"]
            if (bb[2] - bb[0]) < min_side or (bb[3] - bb[1]) < min_side:
                stats["skipped_side"] += 1
                continue
            cap = pick_caption(bb, caps)
            out.append({
                "page": page,
                "bbox_norm": bb,
                "uncovered_frac": round(h["area_frac"], 4),
                "hole_page_area": round(h["area"], 4),
                "paddle_figure_bbox": F,
                "paddle_block_id": b.get("block_id"),
                "caption_hint": cap["text"] if cap else None,
                "caption_bbox": cap["bbox"] if cap else None,
                "caption_block_id": cap["block_id"] if cap else None,
            })
    # 同一 F 内的洞按面积排序；候选之间重叠 ≥ 50% 的只留大的
    out.sort(key=lambda r: -r["hole_page_area"])
    kept = []
    for r in out:
        if any(overlap_ratio(r["bbox_norm"], k["bbox_norm"]) >= 0.5 for k in kept):
            continue
        kept.append(r)
    kept.sort(key=lambda r: (r["bbox_norm"][1], r["bbox_norm"][0]))
    return kept, stats


def analyze(ir_mineru: str, ir_paddle: str, *, grid: int = 200, min_frac: float = 0.30,
            min_page_area: float = 0.01, min_side: float = 0.02) -> dict:
    M = load_jsonl(ir_mineru); P = load_jsonl(ir_paddle)
    attach_eff(M); attach_eff(P)
    m = [b for b in M if b.get("role") != "child" and b.get("kind") not in FURNITURE]
    p = [b for b in P if b.get("role") != "child" and b.get("kind") not in FURNITURE]
    pages = sorted({b.get("page") for b in m} | {b.get("page") for b in p})
    cands, per_page = [], {}
    for page in pages:
        mp = [b for b in m if b.get("page") == page and valid_bbox(b.get("_figcov_bbox") or b.get("bbox_norm"))]
        pp = [b for b in p if b.get("page") == page]
        cs, st = figure_candidates(page, mp, pp, grid=grid, min_frac=min_frac,
                                   min_page_area=min_page_area, min_side=min_side)
        cands.extend(cs)
        if st["figures"]:
            per_page[page] = st
    return {"candidates": cands, "pages": per_page,
            "summary": {"pages": len(pages), "candidates": len(cands)}}


def attach_ink(res: dict, pdf: str, dpi: int = 300) -> None:
    """可选：给候选补 `ink_px`（300dpi 裁图内灰度 <128 的暗像素绝对数）。

    这是**图区题专用**的准入证据，用来替代“密度 ≥0.01”（p4 的候选框密度只有
    0.0050，按密度会被误判纯空白；暗像素绝对数是 659）。
    **不改 R1**：R1/G11 仍用 `r1_ink_min = 0.02` 的密度判据。
    渲染走 `render.render_gray_page`（唯一出口），本函数只读、不写产物。
    """
    if not res.get("candidates"):
        return
    import render  # 同目录；只在 --pdf 时才需要
    by_page = {}
    for c in res["candidates"]:
        pg = int(c["page"])
        if pg not in by_page:
            by_page[pg] = render.render_gray_page(pdf, pg, dpi)
        a = by_page[pg]
        h, w = a.shape
        x0, y0, x1, y1 = c["bbox_norm"]
        crop = a[max(0, int(y0 * h)):min(h, int(y1 * h)),
                 max(0, int(x0 * w)):min(w, int(x1 * w))]
        c["ink_px"] = int((crop < 128).sum()) if crop.size else 0
        c["ink_dpi"] = dpi


# ---------------------------------------------------------------- 自测
def _blk(i, kind, bbox, text="", role="primary"):
    return {"block_id": i, "kind": kind, "bbox_norm": bbox, "text_raw": text,
            "role": role, "page": 1}


def selftest() -> int:
    cases = []

    def run(mblocks, pblocks, **kw):
        return figure_candidates(1, mblocks, pblocks, **kw)[0]

    # 正例：Paddle 一个宽图框，MinerU 两侧两个窄图框，中间有洞 + 题注
    F = [0.3, 0.6, 0.8, 0.7]
    m1 = [_blk("m1", "figure", [0.3, 0.6, 0.45, 0.7]),
          _blk("m2", "figure", [0.65, 0.6, 0.8, 0.7])]
    p1 = [_blk("p1", "figure", F), _blk("c1", "caption", [0.5, 0.71, 0.55, 0.72], "叁键")]
    r = run(m1, p1)
    cases.append(("正例：中间洞 + 题注", len(r) == 1 and r[0]["caption_hint"] == "叁键", r))

    # 负例：MinerU 用一个大图框把 F 全盖住
    r = run([_blk("m1", "figure", [0.25, 0.55, 0.85, 0.75])], p1)
    cases.append(("负例：MinerU 图框更大（白边）", len(r) == 0, r))

    # 负例：中间由 equation 块交代
    m3 = m1 + [_blk("m3", "equation", [0.44, 0.6, 0.66, 0.7])]
    r = run(m3, p1)
    cases.append(("负例：中间是公式块", len(r) == 0, r))

    # 负例：完全一致
    r = run([_blk("m1", "figure", F)], p1)
    cases.append(("负例：两引擎图框一致", len(r) == 0, r))

    # 题注是子图号 → 不配题注，但洞仍然出
    p2 = [_blk("p1", "figure", F), _blk("c1", "caption", [0.5, 0.71, 0.52, 0.72], "(i)")]
    r = run(m1, p2)
    cases.append(("子图号不当题注", len(r) == 1 and r[0]["caption_hint"] is None, r))

    # 题注是表题 → 不配
    p3 = [_blk("p1", "figure", F), _blk("c1", "caption", [0.5, 0.71, 0.55, 0.72], "表2.1 数据")]
    r = run(m1, p3)
    cases.append(("表题不当图题", len(r) == 1 and r[0]["caption_hint"] is None, r))

    # 洞太小（低于 30%）→ 不出
    m4 = [_blk("m1", "figure", [0.3, 0.6, 0.62, 0.7]),
          _blk("m2", "figure", [0.65, 0.6, 0.8, 0.7])]
    r = run(m4, p1)
    cases.append(("洞太小（低于 30%）", len(r) == 0, r))

    passed = sum(1 for _, ok, _ in cases if ok)
    for name, ok, detail in cases:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + json.dumps(detail, ensure_ascii=False)[:300]))
    print(f"\n{passed}/{len(cases)} figcov selftest passed")
    return 0 if passed == len(cases) else 1


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="figcov 只读探针")
    ap.add_argument("--mineru")
    ap.add_argument("--paddle")
    ap.add_argument("--pdf", help="可选：给候选补 300dpi 暗像素数 ink_px（只读渲染）")
    ap.add_argument("--grid", type=int, default=200)
    ap.add_argument("--min-frac", type=float, default=0.30)
    ap.add_argument("--min-page-area", type=float, default=0.01)
    ap.add_argument("--min-side", type=float, default=0.02)
    ap.add_argument("--json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.mineru and a.paddle):
        print("需要 --mineru 和 --paddle（或用 --selftest）", file=sys.stderr)
        return 2
    res = analyze(a.mineru, a.paddle, grid=a.grid, min_frac=a.min_frac,
                  min_page_area=a.min_page_area, min_side=a.min_side)
    if a.pdf:
        attach_ink(res, a.pdf)
    print(f"页={res['summary']['pages']}  候选={res['summary']['candidates']}")
    head = f"{'页':>3}  {'bbox_norm':<34} {'未覆盖':>6}  {'暗像素':>7}  {'题注':<12}  paddle_block"
    print(head)
    for c in res["candidates"]:
        bb = ",".join(f"{v:.3f}" for v in c["bbox_norm"])
        ink = c.get("ink_px", "-")
        print(f"{c['page']:>3}  {bb:<34} {c['uncovered_frac']:>6.2f}  {str(ink):>7}  {(c['caption_hint'] or '-'):<12}  {c['paddle_block_id']}")
    if a.json:
        with open(a.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"已写 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
