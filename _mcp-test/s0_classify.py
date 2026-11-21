# -*- coding: utf-8 -*-
"""S0 摄取与分类：文字层 / 单双栏 / 页眉页脚带 / 倾斜角 → book-profile.json

用法:
  python -X utf8 s0_classify.py profile --pdf <pdf> --out <json> [--pages 1,2,3 | --step 20] [--dpi 100]
  python -X utf8 s0_classify.py cut     --pdf <pdf> --start 41 --end 43 --out <pdf>
  python -X utf8 s0_classify.py selftest

设计（SPEC v2 §9.1 / §7.6"优先删代码"）:
  * **不依赖任何 OCR**：只用 PyMuPDF 的文字层 + 页面位图的投影剖面。
  * 页眉/页脚用"**空白间隔切块**"判定（不是固定百分比带），因为正文与页眉之间的距离随书而异。
  * `skew_correction_deg` 是**修正角**：把它交给旋转函数即可把页面摆正（0 = 正）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import numpy as np
import render
from PIL import Image
from tuning import TUNE
INK_THRESHOLD = TUNE["ink_threshold_gray"]  # L2：见 tuning.json

# ── L2 调优参数（B1-4b）：这 12 个版面阈值现在都在 `tuning.json`（每个带 basis/依据）。
#    取值与原先**完全一致**；此处只做绑定，变量名不变（消费点零改动）。
ROW_INK_MIN_PX = TUNE["s0_row_ink_min_px"]
MIN_BLOCK_INK_PX = TUNE["s0_min_block_ink_px"]
MIN_BLOCK_INK_PX_MARGIN = TUNE["s0_min_block_ink_px_margin"]
MARGIN_FRAC = TUNE["s0_margin_frac"]
FURNITURE_MIN_INK_PX = TUNE["s0_furniture_min_ink_px"]
FURNITURE_MIN_HEIGHT_PX = TUNE["s0_furniture_min_height_px"]
BLOCK_GAP_FRAC = TUNE["s0_block_gap_frac"]
LINE_GAP_FRAC = TUNE["s0_line_gap_frac"]
MAX_FURNITURE_FRAC = TUNE["s0_max_furniture_frac"]
GUTTER_INK_FRAC = TUNE["s0_gutter_ink_frac"]
GUTTER_MIN_WIDTH_FRAC = TUNE["s0_gutter_min_width_frac"]
SKEW_RANGE = tuple(TUNE["s0_skew_range"])


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gray_array(doc, idx: int, dpi: int) -> np.ndarray:
    """第 idx 页（0 起）→ 灰度数组（0=黑）。渲染后端见 `render.py`（pypdfium2/PDFium）。"""
    return render.render_gray(doc, idx, dpi)


def segment_blocks(ink: np.ndarray, gap_frac: float = BLOCK_GAP_FRAC,
                   min_ink_px: float = MIN_BLOCK_INK_PX) -> list[tuple[int, int]]:
    """按"行是否有墨 + 空白间隔"把页面纵向切块（页眉块 / 正文块 / 页脚块…）。"""
    h, w = ink.shape
    row_ink = ink.sum(axis=1).astype(np.float64)
    on = row_ink >= ROW_INK_MIN_PX
    gap_px = max(3, int(h * gap_frac))
    blocks, i = [], 0
    while i < h:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < h:
            if on[j]:
                j += 1
                continue
            k = j
            while k < h and not on[k]:
                k += 1
            if k >= h:          # 尾部空白：块在此处结束（否则会一直延伸到图像底边）
                break
            if (k - j) >= gap_px:
                break           # 够宽的空白且后面还有内容 → 断块
            j = k
        blocks.append((i, j))
        i = j

    def keep(b):
        px = float(ink[b[0]:b[1], :].sum())
        in_margin = (b[0] <= MARGIN_FRAC * h) or (b[1] >= (1 - MARGIN_FRAC) * h)
        return px >= (MIN_BLOCK_INK_PX_MARGIN if in_margin else min_ink_px)

    return [b for b in blocks if keep(b)]


def block_stats(blocks, ink):
    out = []
    h, w = ink.shape
    for (y0, y1) in blocks:
        rowmax = ink[y0:y1, :].sum(axis=1)
        cols = ink[y0:y1, :].sum(axis=0)
        nz = np.nonzero(cols > 0)[0]
        width_frac = float(nz[-1] - nz[0] + 1) / w if nz.size else 0.0
        out.append({"y_frac": [round(y0 / h, 4), round(y1 / h, 4)],
                    "height_frac": round((y1 - y0) / h, 4),
                    "ink_width_frac": round(width_frac, 4),
                    "ink": float(ink[y0:y1, :].sum())})
    return out


def classify_bands(ink: np.ndarray) -> dict:
    """行级切分 + "行间距显著更宽 + 落在页边" → **页眉/页脚候选**。

    为什么用行级而不是段落块级：行间距的中位数由**几十行**估计，稳健；
    段落块的间距中位数在只有 3 个块的页面上会退化成循环论证。
    注意：几何只能给候选；最终判定按 SPEC U-04 —— **标签优先、位置兜底**，并在 S6 剔除。
    """
    h, w = ink.shape
    lines = segment_blocks(ink, gap_frac=LINE_GAP_FRAC)
    if not lines:
        return {"body": None, "top": None, "bottom": None, "stats": [], "median_gap_px": None,
                "line_count": 0, "block_count": 0}
    stats = block_stats(lines, ink)
    hs = [b[1] - b[0] for b in lines]
    gaps = [lines[i + 1][0] - lines[i][1] for i in range(len(lines) - 1)]
    pos = sorted(g for g in gaps if g > 0)
    med_gap = float(np.median(pos)) if pos else 0.0
    med_h = float(np.median(hs)) if hs else 1.0
    top = bot = False
    if len(lines) > 1:
        # 阈值取 1.6×中位间距（实测：本书行距 17 px，页眉组与正文间距 31 px；2× 会漏掉）
        need = max(0.012 * h, 1.6 * med_gap)
        h_cap = max(1.6 * med_h, 0.02 * h)

        def group_from(seq, reverse=False):
            """把互相靠近的行并成一组（页眉常常是"文字 + 其下横线"两行）。"""
            items = list(reversed(seq)) if reverse else list(seq)
            end = 0
            while end + 1 < len(items):
                gap = (items[end + 1][0] - items[end][1]) if not reverse else (items[end][0] - items[end + 1][1])
                if gap >= need:
                    break
                end += 1
            if end + 1 >= len(items):
                return None                      # 组一直连到另一端 → 是正文，不是页眉/页脚
            g = (min(items[0][0], items[end][1]), max(items[0][0], items[end][1]))
            gap_after = (items[end + 1][0] - g[1]) if not reverse else (g[0] - items[end + 1][1])
            return {"span": g, "gap_after": gap_after, "height": g[1] - g[0]}

        g_top = group_from(lines)
        if g_top and g_top["span"][0] <= 0.15 * h and g_top["gap_after"] >= need and g_top["height"] <= h_cap:
            top = True
        g_bot = group_from(lines, reverse=True)
        if g_bot and g_bot["span"][1] >= 0.85 * h and g_bot["gap_after"] >= need and g_bot["height"] <= h_cap:
            bot = True
    stats[0]["is_furniture_candidate"] = top
    stats[-1]["is_furniture_candidate"] = bot
    stats[-1]["is_page_number_candidate"] = bool(
        stats[-1]["y_frac"][0] >= 0.88 and stats[-1]["ink_width_frac"] <= 0.2
        and hs[-1] <= max(1.6 * med_h, 0.02 * h))
    body = (lines[1][0] if top else lines[0][0], lines[-2][1] if bot else lines[-1][1])
    return {"body": body, "top": stats[0], "bottom": stats[-1], "stats": stats,
            "median_gap_px": round(med_gap, 1), "median_line_height_px": round(med_h, 1),
            "line_count": len(lines), "block_count": len(segment_blocks(ink))}


def estimate_skew_correction(ink: np.ndarray) -> float:
    """投影剖面锐度法。返回**修正角**：旋转函数吃这个角度即把页面摆正。"""
    small = Image.fromarray((~ink).astype(np.uint8) * 255)
    if small.width > 700:
        small = small.resize((700, max(1, int(small.height * 700 / small.width))))
    lo, hi, step = SKEW_RANGE
    best_deg, best_score, deg = 0.0, -1.0, lo
    while deg <= hi + 1e-9:
        rot = np.array(small.rotate(deg, resample=Image.BILINEAR, fillcolor=255)) < INK_THRESHOLD
        rp = rot.sum(axis=1).astype(np.float64)
        score = float(((rp[1:] - rp[:-1]) ** 2).sum())
        if score > best_score:
            best_deg, best_score = round(deg, 2), score
        deg += step
    return best_deg


def detect_columns(ink: np.ndarray, y0: int, y1: int) -> dict:
    body = ink[y0:y1, :]
    if body.size == 0:
        return {"columns": 1, "confidence": 0.0, "gutter": None}
    col = body.sum(axis=0).astype(np.float64)
    mx = col.max() or 1.0
    w = ink.shape[1]
    lo, hi = int(w * 0.25), int(w * 0.75)
    low = col < GUTTER_INK_FRAC * mx
    best_run, i = None, lo
    while i < hi:
        if low[i]:
            j = i
            while j < hi and low[j]:
                j += 1
            if best_run is None or (j - i) > (best_run[1] - best_run[0]):
                best_run = (i, j)
            i = j
        else:
            i += 1
    if best_run:
        run_w = best_run[1] - best_run[0]
        left_ink = col[:best_run[0]].max() if best_run[0] > 0 else 0
        right_ink = col[best_run[1]:].max() if best_run[1] < w else 0
        if run_w >= GUTTER_MIN_WIDTH_FRAC * w and left_ink > 0.15 * mx and right_ink > 0.15 * mx:
            return {"columns": 2, "confidence": round(min(1.0, run_w / (0.08 * w)), 3),
                    "gutter": [int(best_run[0]), int(best_run[1])],
                    "gutter_width_frac": round(run_w / w, 4)}
    conf = round(1.0 - (best_run[1] - best_run[0]) / w, 3) if best_run else 0.9
    return {"columns": 1, "confidence": conf, "gutter": None}


def otsu(arr: np.ndarray) -> int:
    hist = np.bincount(arr.ravel(), minlength=256).astype(np.float64)
    total = arr.size
    idx = np.arange(256)
    sum_total = (idx * hist).sum()
    w_b = np.cumsum(hist)
    w_f = total - w_b
    sum_b = np.cumsum(idx * hist)
    with np.errstate(invalid="ignore", divide="ignore"):
        m_b = np.where(w_b > 0, sum_b / np.maximum(w_b, 1), 0.0)
        m_f = np.where(w_f > 0, (sum_total - sum_b) / np.maximum(w_f, 1), 0.0)
    var = w_b * w_f * (m_b - m_f) ** 2
    var[(w_b <= 0) | (w_f <= 0)] = -1
    if var.max() <= 0:          # 单峰（纯纸白或纯黑）→ 无阈值可言
        return -1
    return int(np.argmax(var))


def margin_ink(gray: np.ndarray, y0: int, y1: int, clamp: int = 190) -> dict | None:
    """**页边带内自适应阈值**探针：找页眉/页脚的文字墨水。

    为什么必须单独做：页码这类小字号在 100 dpi 下每行只有 **1 px** 低于全局阈值 128，
    全局行阈值（≥2 px）整块丢弃；而带内基本只有纸白，用带内 Otsu（上限 190）即可显出来。
    """
    band = gray[y0:y1, :]
    if band.size == 0 or y1 <= y0:
        return None
    t_raw = otsu(band)
    if t_raw < 0:
        return None
    t = min(t_raw, clamp)
    ink = band <= t              # Otsu 约定：≤ 阈值的一侧为墨水（"<" 会漏掉恰好等于阈值的浅色字号）
    row = ink.sum(axis=1)
    rows = np.nonzero(row > 0)[0]
    if rows.size == 0:
        return None
    # 取墨量最大的连续行段（间隔 ≤2 px 合并）
    runs, start, prev = [], rows[0], rows[0]
    for r in rows[1:]:
        if r - prev <= 2:
            prev = r
            continue
        runs.append((start, prev))
        start = prev = r
    runs.append((start, prev))
    w = gray.shape[1]
    cands = []
    for (a, b) in runs:
        seg = ink[a:b + 1, :]
        cols = np.nonzero(seg.sum(axis=0) > 0)[0]
        cands.append({"a": a, "b": b, "px": int(seg.sum()), "h": int(b - a + 1),
                      "wf": round(float(cols[-1] - cols[0] + 1) / w, 4) if cols.size else 0.0})
    # 优先选"像文字"的行段：页眉下的**横线**又宽又矮，墨量很大，会把文字挤掉（已实测）
    text_like = [c for c in cands if c["h"] >= 3 and c["wf"] <= 0.85]
    best = max(text_like or cands, key=lambda c: c["px"])
    h = gray.shape[0]
    return {"threshold": int(t), "ink_px": best["px"], "text_like": bool(text_like),
            "y_frac": [round((y0 + best["a"]) / h, 4), round((y0 + best["b"] + 1) / h, 4)],
            "height_px": best["h"], "width_frac": best["wf"],
            "runs_in_band": len(cands)}


def _accept_furniture(m: dict | None) -> bool:
    """页边带的墨水要够多、够高，才算"页眉/页脚文字"（防正文边缘 1–2 px 渗漏）。"""
    return bool(m) and m["ink_px"] >= FURNITURE_MIN_INK_PX and m["height_px"] >= FURNITURE_MIN_HEIGHT_PX


def analyze_page(doc, idx: int, dpi: int, do_skew: bool) -> dict:
    text = render.page_text(doc, idx)
    gray = gray_array(doc, idx, dpi)
    ink = gray < INK_THRESHOLD
    h, w = ink.shape
    bands = classify_bands(ink)
    body = bands["body"] or (0, h - 1)
    top, bottom = bands.get("top") or {}, bands.get("bottom") or {}
    header_raw = margin_ink(gray, 0, body[0]) if body[0] > 0.02 * h else None
    footer_raw = margin_ink(gray, body[1], h) if (h - body[1]) > 0.02 * h else None
    header_ink = header_raw if _accept_furniture(header_raw) else None
    footer_ink = footer_raw if _accept_furniture(footer_raw) else None
    return {
        "page": idx + 1,
        "size_px": [int(w), int(h)],
        "text_chars": len(text.strip()),
        "images": render.page_image_count(doc, idx),
        "ink_density": round(float(ink.mean()), 5),
        "block_count": bands.get("block_count", 0),
        "line_count": bands.get("line_count", 0),
        "median_block_gap_px": bands.get("median_gap_px"),
        "body_span_frac": [round(body[0] / h, 4), round(body[1] / h, 4)],
        "top_block": {k: top[k] for k in ("y_frac", "height_frac", "ink_width_frac")} if top else None,
        "bottom_block": {k: bottom[k] for k in ("y_frac", "height_frac", "ink_width_frac")} if bottom else None,
        "top_furniture_candidate": bool(top.get("is_furniture_candidate")),
        "bottom_furniture_candidate": bool(bottom.get("is_furniture_candidate")),
        "page_number_candidate": bool(bottom.get("is_page_number_candidate")),
        "header_ink": header_ink,
        "footer_ink": footer_ink,
        "header_ink_raw_px": (header_raw or {}).get("ink_px", 0),
        "footer_ink_raw_px": (footer_raw or {}).get("ink_px", 0),
        "columns": detect_columns(ink, body[0], body[1]),
        "skew_correction_deg": estimate_skew_correction(ink) if do_skew else None,
    }


COLUMNS_AGREEMENT_MIN = 0.8          # 栏判定的"可信"门槛：逐页结论一致度低于它就别信


def columns_reliable(agreement) -> bool:
    """栏判定是否可信：**逐页一致度**是它唯一的自证信号。

    实测（2026-10-13，`渲染后端替换-pypdfium2-结果.md` §10）：
      * 5 本教材扫描书：一致度 **1.0**，两库结论一致；
      * 电子版冒险模组（正栏 + 侧栏/整幅图混排）：一致度只有 **0.44–0.67**，且**对渲染后端敏感**
        （p5/p42 在 MuPDF 下 2 栏、PDFium 下 1 栏）。
    该字段**没有任何消费方**；此处只把"别信它"变成机器可见的布尔值。
    """
    try:
        return float(agreement) >= COLUMNS_AGREEMENT_MIN
    except (TypeError, ValueError):
        return False


def profile(pdf: str, pages: list[int] | None, step: int, dpi: int, do_skew: bool) -> dict:
    doc = render.open_doc(pdf)
    n = render.page_count(doc)
    idxs = ([p - 1 for p in pages] if pages else list(range(0, n, step)))
    idxs = [i for i in idxs if 0 <= i < n]
    per_page = [analyze_page(doc, i, dpi, do_skew) for i in idxs]
    _wpt, _hpt = render.page_size_pt(doc, 0)
    out = {"source": os.path.abspath(pdf), "sha256": sha256_file(pdf), "page_count": n,
           # 版本标识（B1-4b 教训：画像必须自带"谁生成的"）：渲染后端与阈值变了，像素统计必然变
           "profile_schema": 2,
           "render_backend": render.BACKEND, "render_versions": render.BACKEND_VERSIONS,
           "tuning": {k: TUNE[k] for k in sorted(TUNE)},
           "media_box_pt": [round(_wpt, 2), round(_hpt, 2)],
           "sampled_pages": [p["page"] for p in per_page], "sample_step": step, "dpi": dpi}
    if per_page:
        cols = [p["columns"]["columns"] for p in per_page]
        skews = [p["skew_correction_deg"] for p in per_page if p["skew_correction_deg"] is not None]
        text_ratio = sum(1 for p in per_page if p["text_chars"] > 20) / len(per_page)
        tops = [p["top_block"]["y_frac"] for p in per_page if p["top_block"]]
        bots = [p["bottom_block"]["y_frac"] for p in per_page if p["bottom_block"]]

        def repeat_ratio(ranges, tol=0.02):
            if len(ranges) < 2:
                return None
            med = [float(np.median([r[0] for r in ranges])), float(np.median([r[1] for r in ranges]))]
            return round(sum(1 for r in ranges if abs(r[0] - med[0]) <= tol and abs(r[1] - med[1]) <= tol)
                         / len(ranges), 3)

        out.update({
            "text_layer_ratio": round(text_ratio, 4), "has_text_layer": text_ratio > 0.8,
            "columns_mode": max(set(cols), key=cols.count) if cols else None,
            "columns_agreement": round(cols.count(max(set(cols), key=cols.count)) / len(cols), 3) if cols else None,
            # 栏判定**不可全信**：一致度低或对渲染后端敏感时，别拿它当结论（该字段无消费方）
            "columns_reliable": columns_reliable(
                (cols.count(max(set(cols), key=cols.count)) / len(cols)) if cols else None),
            "columns_note": ("`columns_mode` 是**咨询字段**（无消费方）。实测：教材扫描书一致度 1.0 可靠；"
                             "正栏+侧栏/整幅图混排的电子版书一致度仅 0.44–0.67，且**换渲染后端会翻面**"
                             "（详见 `渲染后端替换-pypdfium2-结果.md` §10）。若将来要消费它，"
                             "必须先做稳定性工程（按页多数票 + 置信度门槛），不要直接读这个数。"),
            "top_furniture_candidate_ratio": round(sum(p["top_furniture_candidate"] for p in per_page) / len(per_page), 3),
            "bottom_furniture_candidate_ratio": round(sum(p["bottom_furniture_candidate"] for p in per_page) / len(per_page), 3),
            "page_number_candidate_ratio": round(sum(p["page_number_candidate"] for p in per_page) / len(per_page), 3),
            "top_block_repeat_ratio": repeat_ratio(tops),
            "bottom_block_repeat_ratio": repeat_ratio(bots),
            "top_block_median_y_frac": [round(float(np.median([r[0] for r in tops])), 4),
                                        round(float(np.median([r[1] for r in tops])), 4)] if tops else None,
            "bottom_block_median_y_frac": [round(float(np.median([r[0] for r in bots])), 4),
                                           round(float(np.median([r[1] for r in bots])), 4)] if bots else None,
            "header_ink_detected_ratio": round(sum(1 for p in per_page if p["header_ink"]) / len(per_page), 3),
            "footer_ink_detected_ratio": round(sum(1 for p in per_page if p["footer_ink"]) / len(per_page), 3),
            "header_ink_median_y_frac": [round(float(np.median([p["header_ink"]["y_frac"][0] for p in per_page if p["header_ink"]])), 4),
                                         round(float(np.median([p["header_ink"]["y_frac"][1] for p in per_page if p["header_ink"]])), 4)]
                                        if any(p["header_ink"] for p in per_page) else None,
            "footer_ink_median_y_frac": [round(float(np.median([p["footer_ink"]["y_frac"][0] for p in per_page if p["footer_ink"]])), 4),
                                         round(float(np.median([p["footer_ink"]["y_frac"][1] for p in per_page if p["footer_ink"]])), 4)]
                                        if any(p["footer_ink"] for p in per_page) else None,
            "ink_density_median": round(float(np.median([p["ink_density"] for p in per_page])), 5),
            "skew_correction_median_deg": round(float(np.median(skews)), 2) if skews else None,
            "skew_correction_max_abs_deg": round(float(max(abs(s) for s in skews)), 2) if skews else None,
        })
        route = "direct_text_extract" if out["has_text_layer"] else "ocr_required"
        out["route"] = route
        out["furniture_policy"] = ("页眉/页脚此处只给**候选**；最终判定按 SPEC U-04："
                                  "标签优先（MinerU type=header/page_number/page_footnote）、位置兜底，"
                                  "并在 S6 剔除（SPEC §2.2）")
        out["classification"] = (f"{route}|columns={out['columns_mode']}"
                                 f"|top_furn={out['top_furniture_candidate_ratio']}"
                                 f"|bot_furn={out['bottom_furniture_candidate_ratio']}"
                                 f"|pageno={out['page_number_candidate_ratio']}"
                                 f"|skew_corr={out['skew_correction_median_deg']}")
    out["per_page"] = per_page
    doc.close()
    return out


def cmd_profile(args) -> int:
    pages = [int(x) for x in args.pages.split(",")] if args.pages else None
    prof = profile(args.pdf, pages, args.step, args.dpi, do_skew=not args.no_skew)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(prof, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in prof.items() if k != "per_page"}, ensure_ascii=False, indent=2))
    return 0


def cmd_cut(args) -> int:
    info = render.slice_pages(args.pdf, args.start, args.end, args.out)   # pypdf 搬页
    print(json.dumps({"out": info["out"], "pages": [args.start, args.end],
                      "bytes": info["bytes"], "sha256": info["sha256"]},
                     ensure_ascii=False, indent=2))
    return 0


def _synth_lines(y0, y1, pitch=30, height=14, x0=250, x1=450):
    arr = np.zeros((1000, 700), dtype=bool)
    y = y0
    while y + height <= y1:
        arr[y:y + height, x0:x1] = True
        y += pitch
    return arr


def _synth_page(with_header=True, with_footer=True, double=False):
    """正文用**行结构**画（每 30 px 一行、行高 14 px），以便行间距中位数有意义。"""
    if double:
        ink = _synth_lines(250, 900, x0=60, x1=300) | _synth_lines(250, 900, x0=380, x1=640)
    else:
        ink = _synth_lines(250, 900, x0=80, x1=620)
    if with_header:
        ink[60:74, 250:450] = True
    if with_footer:
        ink[955:969, 330:370] = True
    return ink


def selftest() -> int:
    cases, passed = [], 0

    r = detect_columns(_synth_page(double=False), 250, 900)
    cases.append(("单栏不被误判为双栏", r["columns"] == 1, r))

    r = detect_columns(_synth_page(double=True), 250, 900)
    cases.append(("双栏识别且 gutter 落位正确", r["columns"] == 2 and 295 <= r["gutter"][0] <= 305, r))

    b = classify_bands(_synth_page(True, True))
    cases.append(("页眉/页脚候选被切出（已知运行页眉 + 页码）",
                  bool(b["top"]["is_furniture_candidate"]) and bool(b["bottom"]["is_furniture_candidate"])
                  and bool(b["bottom"]["is_page_number_candidate"]), b.get("top")))

    b = classify_bands(_synth_lines(60, 900))
    cases.append(("无页眉页脚时不误报", not b["top"]["is_furniture_candidate"]
                  and not b["bottom"]["is_furniture_candidate"], b["top"]))

    arr = np.zeros((600, 420), dtype=bool)
    for k in range(0, 600, 20):
        arr[k:k + 3, 40:380] = True
    rot = np.array(Image.fromarray((~arr).astype(np.uint8) * 255)
                   .rotate(1.5, resample=Image.BILINEAR, fillcolor=255)) < INK_THRESHOLD
    est = estimate_skew_correction(rot)
    cases.append(("倾斜修正角 ≈ −1.5°（符号语义：修正角）", abs(est + 1.5) <= 0.5, {"est": est}))

    r = detect_columns(np.zeros((500, 400), dtype=bool), 0, 500)
    cases.append(("空白页不崩且判单栏", r["columns"] == 1, r))
    cases.append(("栏判定可信度：一致度 1.0 → 可信", columns_reliable(1.0) is True, None))
    cases.append(("栏判定可信度：一致度 0.44（电子版实测） → **不可信**",
                  columns_reliable(0.44) is False and columns_reliable(None) is False, None))

    # 页边带自适应阈值：浅灰小字号（模拟页码）必须被检出；纯纸白不得误报
    gray = np.full((1000, 700), 240, dtype=np.uint8)
    gray[930:944, 320:352] = 165          # 淡淡的一小块"页码"
    m = margin_ink(gray, int(0.88 * 1000), 1000)
    cases.append(("页边带自适应阈值检出浅色页码", bool(m) and m["ink_px"] > 50 and 0.92 < m["y_frac"][0] < 0.95, m))
    clean = np.full((1000, 700), 240, dtype=np.uint8)
    cases.append(("纯纸白不误报页脚墨水", margin_ink(clean, int(0.88 * 1000), 1000) is None, None))
    blank_top = np.full((300, 500), 238, dtype=np.uint8)
    cases.append(("空白页边带（无页眉）返回 None", margin_ink(blank_top, 0, 300) is None, None))
    sliver = np.full((300, 500), 238, dtype=np.uint8)
    sliver[297:300, 100:400] = 60        # 正文顶边渗漏 3 px
    cases.append(("正文边缘渗漏不得当成页眉", not _accept_furniture(margin_ink(sliver, 0, 300)), None))
    band = np.full((200, 500), 238, dtype=np.uint8)
    band[20:34, 60:200] = 40             # 页眉文字（矮而窄）
    band[40:42, 30:470] = 40             # 页眉下的横线（高墨量、又宽又矮）
    m = margin_ink(band, 0, 200)
    cases.append(("横线不得挤掉页眉文字", bool(m) and m["text_like"] and m["y_frac"][0] < 0.12, m))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S0 tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile")
    p.add_argument("--pdf", required=True); p.add_argument("--out", required=True)
    p.add_argument("--pages"); p.add_argument("--step", type=int, default=20)
    p.add_argument("--dpi", type=int, default=100); p.add_argument("--no-skew", action="store_true")
    p.set_defaults(func=cmd_profile)
    c = sub.add_parser("cut")
    c.add_argument("--pdf", required=True); c.add_argument("--start", type=int, required=True)
    c.add_argument("--end", type=int, required=True); c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_cut)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
