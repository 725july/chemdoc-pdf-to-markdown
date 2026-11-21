# -*- coding: utf-8 -*-
"""S3：引擎存在性/归属对比 → **新管线自产的待问清单**（只问两件事）。

用法:
  python -X utf8 s3_compare.py compare --mineru <ir.mineru.jsonl> --paddle <ir.paddle.jsonl> --out <dir>
  python -X utf8 s3_compare.py selftest

设计（SPEC v2 §3.1 / §3.3 / §3.4 / §4.1 / §4.7 / §6 / N-1…N-8）:
  * **区域（region）是比较的唯一单位**（§3.4）：同页两侧 primary 块按纵向重叠 + 横向重叠聚类，
    区域内两侧文本各自按阅读顺序拼成一条，再在**区域级**比较。
    这样"并段 / 切段 / 子块 vs 独立块"在区域层面天然合并，不再需要逐类特例规则。
  * 只对 **primary** 块做比较；子块（table_body/caption/footnote/img_path）只作为父块的**有效文本**参与，
    并留给 R2/R3 对账。
  * **N-8 由构造满足**：`existence_*` 的锚点来自**主张方**；`content_diff`/`ownership`/`structure_diff` 给**双侧锚点**。
  * 家具（running_head/running_foot/page_number）**不进**视觉队列（SPEC §2.2 / U-04），单列统计。
  * 所有丢弃都带**名目**写进 `s3-dropped.jsonl`，**绝不静默丢弃**。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import textnorm  # noqa: E402
from contracts import FURNITURE
from figcov_probe import figure_candidates, overlap_ratio
from tuning import TUNE

FIGCOV_GRID = TUNE["figcov_grid"]
FIGCOV_MIN_FRAC = TUNE["figcov_hole_min_frac"]
FIGCOV_MIN_PAGE_AREA = TUNE["figcov_hole_min_page_area"]
FIGCOV_MIN_SIDE = TUNE["figcov_hole_min_side"]

FAMILY = {
    "paragraph": "text", "heading": "text", "caption": "text",
    "equation": "math", "formula_number": "math", "inline_formula": "math", "equation_alt": "math",
    "table": "table", "table_body": "table",
    "figure": "figure", "figure_media": "figure",
    "footnote": "note", "running_head": "furniture", "running_foot": "furniture",
    "page_number": "furniture", "unknown": "unknown",
}
Y_GAP_FRAC = TUNE["s3_y_gap_frac"]                     # L2：纵向区间允许的间隙（页高比例）
REGION_LEN_RATIO_MAX = TUNE["s3_region_len_ratio_max"]  # L2：区域级文本长度比上限
SEVERE_PATTERNS = [
    ("digit", re.compile(r"\d")),
    ("unit", re.compile(r"(?i)\b(k|mol|pa|kpa|mm|cm|dm|inhg|mmhg|j|kj|nm|ppm)\b|℃|°C|%")),
    ("sub_sup", re.compile(r"[_^]\s*\{|[₀-₉⁰-⁹]")),
    ("tag", re.compile(r"\\tag\{|\(\s*\d+\.\d+\s*\)")),
    ("negation", re.compile(r"[不无未非没]")),
    ("chem", re.compile(r"(?<![A-Za-z])[A-Z][a-z]?\d*(?![a-z])")),
]
# "像编号而不是像内容"的差异区间形态（必须同时含 tag/括号，见 classify_regions 的注释）
TAGISH = re.compile(r"^(?:\\?tag\s*\{?[\d.]*\}?|[()\d.\s]*)$")


def load_ir(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def iou(a, b) -> float:
    if not a or not b:
        return 0.0
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    union = ((a[2] - a[0]) * (a[3] - a[1])) + ((b[2] - b[0]) * (b[3] - b[1])) - inter
    return inter / union if union > 0 else 0.0


def risk_of(*texts) -> dict:
    joined = " ".join(t or "" for t in texts)
    signals = [name for name, pat in SEVERE_PATTERNS if pat.search(joined)]
    return {"severe": bool(signals), "signals": signals}


def drop(reason: str, **kw) -> dict:
    return {"dropped_reason": reason, **kw}


def attach_children(blocks: list[dict]) -> None:
    kids: dict[str, list[dict]] = {}
    for b in blocks:
        if b.get("role") == "child" and b.get("parent_id"):
            kids.setdefault(b["parent_id"], []).append(b)
    for b in blocks:
        b["_children"] = sorted(kids.get(b["block_id"], []), key=lambda x: x.get("order", 0))
        # 区域聚类与锚点都要用"含子块"的框：MinerU 把图题/表题挂成子块，
        # 若只用主块框，图题会落到**另一个区域**，于是变成一对假的 existence 问题（实测）。
        boxes = [x["bbox_norm"] for x in [b] + b["_children"] if x.get("bbox_norm")]
        b["_bbox_eff"] = ([min(v[0] for v in boxes), min(v[1] for v in boxes),
                           max(v[2] for v in boxes), max(v[3] for v in boxes)] if boxes else None)


def eff_text(block: dict) -> str:
    """有效文本 = 主块 + 子块（排除 img_path）。否则"内容全在子块里"的表/图会变成假差异。"""
    if block.get("_eff") is not None:
        return block["_eff"]
    parts = [block.get("text_raw") or ""]
    for kid in block.get("_children", []):
        if kid.get("kind") == "figure_media" or kid.get("source_field") == "img_path":
            continue
        parts.append(kid.get("text_raw") or "")
    block["_eff"] = "\n".join(p for p in parts if p).strip()
    return block["_eff"]


def union_bbox(blocks: list[dict]):
    boxes = [b.get("_bbox_eff") or b.get("bbox_norm") for b in blocks]
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def line_multiset(t: str) -> list[str]:
    """归一后按行排序的多重集：用来识别"同样内容、只是顺序/切分不同"（实测：表体的表题/表体顺序差异造出 45 个差异区间）。"""
    return sorted(x for x in (textnorm.norm_weak(l) for l in (t or "").split("\n")) if x)


def classify_regions(dr: dict) -> dict:
    """给每个差异区间分类：纯空白 / 纯标点 / 编号表示法 / 真内容。

    内容判定已在**区域级**做过（区域文本不相等）；这里只回答"差在哪一类"，
    以免把纯空白与标点差异当成内容差异喂给视觉层（D2：标点/空格不算内容）。
    """
    classes = []
    for r in dr["regions"]:
        a, b = r["a_raw"] or "", r["b_raw"] or ""
        if textnorm.norm_nospace(a) == textnorm.norm_nospace(b):
            classes.append("whitespace_only")
        elif textnorm.norm_weak(a) == textnorm.norm_weak(b):
            classes.append("punct_only")
        elif (TAGISH.fullmatch(a) and TAGISH.fullmatch(b)
              and re.search(r"\\?tag|\(|\)", a + b)):
            # 必须是"确实含编号/括号"才算表示法差异；否则裸数字差异会被误判（实测过的坑）
            classes.append("representation_tag")
        else:
            classes.append("content")
    return {"classes": classes, "has_content": "content" in classes,
            "histogram": {c: classes.count(c) for c in set(classes)}}


def x_overlaps(a, b) -> bool:
    if not a or not b:
        return True
    return min(a[2], b[2]) > max(a[0], b[0])


def build_regions(mblocks: list[dict], pblocks: list[dict]) -> list[dict]:
    """把同页两侧 primary 块聚成区域：纵向区间重叠（允许 Y_GAP_FRAC 间隙）且横向相交。

    单栏书即"文本带"；这样 MinerU 的并段块与 Paddle 的切分段会落进**同一个区域**。
    """
    items = ([("mineru", b) for b in mblocks] + [("paddle", b) for b in pblocks])
    items = [(eng, b) for eng, b in items if b.get("bbox_norm")]
    items.sort(key=lambda t: (t[1]["bbox_norm"][1], t[1]["bbox_norm"][0]))
    regions: list[dict] = []
    for eng, blk in items:
        bb = blk.get("_bbox_eff") or blk["bbox_norm"]
        placed = False
        for reg in regions:
            gap = Y_GAP_FRAC
            if bb[1] <= reg["y1"] + gap and bb[3] >= reg["y0"] - gap and x_overlaps(bb, reg["x"]):
                reg["m"].append(blk) if eng == "mineru" else reg["p"].append(blk)
                reg["y0"] = min(reg["y0"], bb[1]); reg["y1"] = max(reg["y1"], bb[3])
                reg["x"] = [min(reg["x"][0], bb[0]), min(reg["x"][1], bb[1]),
                            max(reg["x"][2], bb[2]), max(reg["x"][3], bb[3])] if reg["x"] else list(bb)
                placed = True
                break
        if not placed:
            regions.append({"y0": bb[1], "y1": bb[3], "x": list(bb),
                            "m": [blk] if eng == "mineru" else [],
                            "p": [blk] if eng == "paddle" else []})
    # 单趟贪心聚类不传递：图块与其图题（子块）会把区域撑开，后面的 Paddle 图题可能另起一区，
    # 于是同一内容被拆成"M-only + P-only"两条假问题（实测两本书各一例）→ 这里做**合并到不动点**。
    merged = True
    while merged and len(regions) > 1:
        merged = False
        out: list[dict] = []
        for reg in regions:
            hit = None
            for cand in out:
                if (reg["y0"] <= cand["y1"] + Y_GAP_FRAC and reg["y1"] >= cand["y0"] - Y_GAP_FRAC
                        and x_overlaps(reg["x"], cand["x"])):
                    hit = cand
                    break
            if hit is not None:
                hit["m"] += reg["m"]; hit["p"] += reg["p"]
                hit["y0"] = min(hit["y0"], reg["y0"]); hit["y1"] = max(hit["y1"], reg["y1"])
                hit["x"] = [min(hit["x"][0], reg["x"][0]), min(hit["x"][1], reg["x"][1]),
                            max(hit["x"][2], reg["x"][2]), max(hit["x"][3], reg["x"][3])]
                merged = True
            else:
                out.append(reg)
        regions = out
    for reg in regions:
        reg["m"].sort(key=lambda b: (b["bbox_norm"][1], b["bbox_norm"][0]))
        reg["p"].sort(key=lambda b: (b["bbox_norm"][1], b["bbox_norm"][0]))
        reg["m_text"] = "\n".join(eff_text(b) for b in reg["m"]).strip()
        reg["p_text"] = "\n".join(eff_text(b) for b in reg["p"]).strip()
        reg["bbox"] = union_bbox(reg["m"] + reg["p"])
        reg["kinds_m"] = sorted({b["kind"] for b in reg["m"]})
        reg["kinds_p"] = sorted({b["kind"] for b in reg["p"]})
    return regions


def _q(item_id, page, review_type, kind, operation, anchor_provider, bbox, region, question, **extra):
    rec = {
        "item_id": item_id, "pdf_page": page, "review_type": review_type,
        "kind": kind, "operation": operation, "anchor_provider": anchor_provider,
        "anchor_source": ",".join([b["block_id"] for b in region["m"] + region["p"]]),
        "bbox": bbox, "bbox_space": "normalized-0-1",
        "mineru_block_ids": [b["block_id"] for b in region["m"]],
        "paddle_block_ids": [b["block_id"] for b in region["p"]],
        "mineru_span": region["m_text"], "paddle_span": region["p_text"],
        "question": question,
    }
    rec.update(extra)
    return rec


def figure_region_questions(page: int, mp: list[dict], pp: list[dict],
                            existing: list[dict]) -> tuple[list[dict], list[dict]]:
    """U-15：图区问责候选（S3 的第 6 种问题）。

    只读 IR、不渲染；候选与已有 S3 问题 bbox 重叠 ≥50% 时不重复出题。
    `existing` 传**同页**已有问题，避免跨页误去重。
    """
    cands, stats = figure_candidates(
        page, mp, pp, grid=FIGCOV_GRID, min_frac=FIGCOV_MIN_FRAC,
        min_page_area=FIGCOV_MIN_PAGE_AREA, min_side=FIGCOV_MIN_SIDE)
    out, dropped = [], []
    seq = 0
    for c in cands:
        bb = c["bbox_norm"]
        if any(q.get("bbox") and q.get("review_type") not in ("existence_paddle_only", "ownership")
               and overlap_ratio(bb, q["bbox"]) >= 0.5 for q in existing):
            dropped.append(drop("figure_region_merged", page=page, bbox=bb,
                                paddle_block_id=c.get("paddle_block_id")))
            continue
        pfig = next((b for b in pp if b.get("block_id") == c.get("paddle_block_id")), None)
        if pfig is None:
            dropped.append(drop("figure_region_paddle_block_missing", page=page, bbox=bb))
            continue
        seq += 1
        cap = c.get("caption_hint") or ""
        nearby = [b["block_id"] for b in mp
                  if b.get("kind") == "figure"
                  and (b.get("_bbox_eff") or b.get("bbox_norm"))
                  and min((b.get("_bbox_eff") or b["bbox_norm"])[3], bb[3])
                  > max((b.get("_bbox_eff") or b["bbox_norm"])[1], bb[1])]
        region = {"m": [], "p": [pfig], "m_text": "", "p_text": cap,
                  "bbox": bb, "kinds_m": [], "kinds_p": ["figure"]}
        out.append(_q(
            f"S3-P{page:04d}-FIG-{seq:03d}", page, "figure_region", "figure",
            "insert", "paddle", bb, region,
            "页面这一区域是否有一幅图/结构式？主引擎没有交代。",
            caption_hint=cap,
            context={"paddle_figure_bbox": c.get("paddle_figure_bbox"),
                     "uncovered_frac": c.get("uncovered_frac"),
                     "caption_hint": cap, "covered_by": nearby},
            risk={"severe": True, "signals": ["figure"]}))
    if any(stats.get(k) for k in ("skipped_frac", "skipped_area", "skipped_side",
                                  "skipped_fullpage", "skipped_tiny")):
        dropped.append(drop("figure_region_skipped", page=page, **stats))
    return out, dropped


def merge_attachment_regions(regions: list[dict]) -> list[dict]:
    """U-13：把"单侧独立 caption/footnote"并入"另一侧把它当子块的区域"。

    成因（实测）：MinerU 的图题/表题作为**子块**没有自己的 bbox（继承父块），所以区域框够不到
    Paddle 那个位置略低的独立图题块（间隙 0.014 > Y_GAP_FRAC 0.010）→ 同一内容被拆成两个区域，
    变成一对假的 existence 问题。这里按**文本归属**把它们并回一区。
    """
    def refresh(r):
        r["m_text"] = "\n".join(eff_text(b) for b in r["m"]).strip()
        r["p_text"] = "\n".join(eff_text(b) for b in r["p"]).strip()
        r["bbox"] = union_bbox(r["m"] + r["p"])
        r["kinds_m"] = sorted({b["kind"] for b in r["m"]})
        r["kinds_p"] = sorted({b["kind"] for b in r["p"]})

    for r in regions:
        refresh(r)
    changed = True
    while changed and len(regions) > 1:
        changed = False
        for i, r1 in enumerate(regions):
            side = "p" if (r1["p"] and not r1["m"]) else ("m" if (r1["m"] and not r1["p"]) else None)
            if side is None:
                continue
            blocks = r1[side]
            if not all(b["kind"] in ("caption", "footnote") for b in blocks):
                continue
            key = textnorm.norm_weak(r1[f"{side}_text"])
            if len(key) < 4:
                continue
            other = "m" if side == "p" else "p"
            for j, r2 in enumerate(regions):
                if i == j or not r2[other]:
                    continue
                if key in textnorm.norm_weak(r2[f"{other}_text"]):
                    r2[side] += blocks
                    r2["y0"] = min(r2["y0"], r1["y0"]); r2["y1"] = max(r2["y1"], r1["y1"])
                    r2["x"] = [min(r2["x"][0], r1["x"][0]), min(r2["x"][1], r1["x"][1]),
                               max(r2["x"][2], r1["x"][2]), max(r2["x"][3], r1["x"][3])]
                    refresh(r2)
                    regions.pop(i)
                    changed = True
                    break
            if changed:
                break
    # 第二种情形：两侧**各自单边**成区、文本完全相同 → 同一内容被聚类拆成两个区域
    #（实测：有机化学 p2 的"纽曼式"图题：MinerU 侧在 R3、Paddle 侧在 R5，中间夹着"锯架式"区）
    changed = True
    while changed and len(regions) > 1:
        changed = False
        for i, r1 in enumerate(regions):
            if not (r1["m"] and not r1["p"]):
                continue
            t1 = textnorm.norm_weak(r1["m_text"])
            if len(t1) < 2:
                continue
            for j, r2 in enumerate(regions):
                if i == j or not (r2["p"] and not r2["m"]):
                    continue
                if textnorm.norm_weak(r2["p_text"]) == t1:
                    r2["m"] += r1["m"]
                    r2["y0"] = min(r2["y0"], r1["y0"]); r2["y1"] = max(r2["y1"], r1["y1"])
                    r2["x"] = [min(r2["x"][0], r1["x"][0]), min(r2["x"][1], r1["x"][1]),
                               max(r2["x"][2], r1["x"][2]), max(r2["x"][3], r1["x"][3])]
                    refresh(r2)
                    regions.pop(i)
                    changed = True
                    break
            if changed:
                break
    return regions


def compare(mineru_ir: str, paddle_ir: str) -> dict:
    m_all, p_all = load_ir(mineru_ir), load_ir(paddle_ir)
    attach_children(m_all); attach_children(p_all)
    m = [b for b in m_all if b.get("role") == "primary"]
    p = [b for b in p_all if b.get("role") == "primary"]
    questions, dropped = [], []
    pages = sorted({b["page"] for b in m} | {b["page"] for b in p})

    for page in pages:
        mp = [b for b in m if b["page"] == page and b["kind"] not in FURNITURE]
        pp = [b for b in p if b["page"] == page and b["kind"] not in FURNITURE]
        for b in m + p:
            if b["page"] == page and b["kind"] in FURNITURE:
                dropped.append(drop("furniture_excluded", page=page, block_id=b["block_id"],
                                    kind=b["kind"], engine=b["engine"]))
        for reg in merge_attachment_regions(build_regions(mp, pp)):
            a, b = reg["m_text"], reg["p_text"]
            ids = {"mineru_block_ids": [x["block_id"] for x in reg["m"]],
                   "paddle_block_ids": [x["block_id"] for x in reg["p"]]}
            n = len(questions) + 1
            if a and not b:
                questions.append(_q(f"S3-P{page:04d}-M-ONLY-{n:03d}", page, "existence_mineru_only",
                                    "|".join(reg["kinds_m"]), "insert", "mineru", reg["bbox"], reg,
                                    "页面上是否存在这一块？（主引擎有、见证引擎无）",
                                    risk=risk_of(a)))
                continue
            if b and not a:
                questions.append(_q(f"S3-P{page:04d}-P-ONLY-{n:03d}", page, "existence_paddle_only",
                                    "|".join(reg["kinds_p"]), "delete", "paddle", reg["bbox"], reg,
                                    "页面上是否存在这一块？（见证引擎有、主引擎无——可能是伪造，也可能是主引擎漏了）",
                                    risk=risk_of(b)))
                continue
            if not a and not b:
                dropped.append(drop("region_both_empty", page=page, **ids))
                continue
            # 区域级内容比较
            if textnorm.norm_alias(a) == textnorm.norm_alias(b):
                dropped.append(drop("N-3_notation_alias_only", page=page, **ids))
                continue
            if textnorm.norm_weak(a) == textnorm.norm_weak(b):
                dropped.append(drop("N-2_no_content_diff", page=page, **ids))
                continue
            # 区域内 kind 家族不一致 → 结构问题（先于文本差异）
            fam_m = {FAMILY.get(k, "unknown") for k in reg["kinds_m"]}
            fam_p = {FAMILY.get(k, "unknown") for k in reg["kinds_p"]}
            if fam_m and fam_p and not (fam_m & fam_p):
                questions.append(_q(f"S3-P{page:04d}-STRUCT-{n:03d}", page, "structure_diff",
                                    f"{reg['kinds_m']}|{reg['kinds_p']}", "structure", "both",
                                    reg["bbox"], reg,
                                    f"页面这一区域是「{reg['kinds_m']}」还是「{reg['kinds_p']}」？",
                                    anchors={"mineru": union_bbox(reg["m"]),
                                             "paddle": union_bbox(reg["p"])}))
                continue
            la, lb = len(textnorm.norm_weak(a)), len(textnorm.norm_weak(b))
            ratio = max(la, lb) / max(1, min(la, lb))
            if ratio > REGION_LEN_RATIO_MAX:
                dropped.append(drop("region_segmentation_mismatch", page=page, len_mineru=la,
                                    len_paddle=lb, ratio=round(ratio, 2), **ids))
                continue
            # 同样内容、只是顺序/切分不同（实测：表体与表题的顺序差异会造出 45 个差异区间）
            if line_multiset(a) == line_multiset(b):
                dropped.append(drop("line_multiset_matched", page=page, **ids))
                continue
            dr = textnorm.diff_regions(a, b)
            if not dr["regions"]:
                dropped.append(drop("N-6_span_truncation_only", page=page, tail=dr["tail_text"], **ids))
                continue
            if max(dr["a_len"], dr["b_len"]) > textnorm.ALIGN_ARTIFACT_CHARS:
                dropped.append(drop("N-7_alignment_artifact", page=page, **ids))
                continue
            cls = classify_regions(dr)
            dr["region_classes"] = cls["classes"]
            dr["class_histogram"] = cls["histogram"]
            if not cls["has_content"]:
                reason = ("representation_formula_number" if set(cls["classes"]) == {"representation_tag"}
                          else "all_regions_non_content")
                dropped.append(drop(reason, page=page, classes=cls["histogram"], **ids))
                continue
            content_text = " ".join(f"{r['a_raw']} {r['b_raw']}"
                                    for r, c in zip(dr["regions"], cls["classes"]) if c == "content")
            rk = risk_of(content_text)
            if not rk["severe"]:
                dropped.append(drop("non_severe_text_variant", page=page, risk=rk, **ids))
                continue
            questions.append(_q(f"S3-P{page:04d}-CONTENT-{n:03d}", page, "content_diff",
                                "|".join(sorted(set(reg["kinds_m"] + reg["kinds_p"]))), "replace",
                                "both", reg["bbox"], reg,
                                "页面上的这段文字究竟怎么写的？（差异区间已给出，逐字核对）",
                                span_diff=dr, risk=rk,
                                anchors={"mineru": union_bbox(reg["m"]),
                                         "paddle": union_bbox(reg["p"])}))

    # 归属：同一文本在两侧落在不同页
    def canon_map(blocks):
        d = {}
        for b in blocks:
            if b["kind"] in FURNITURE:
                continue
            c = textnorm.norm_weak(eff_text(b))
            if len(c) >= 12:
                d.setdefault(c, []).append(b)
        return d
    mc, pc = canon_map(m), canon_map(p)
    for c, mbs in mc.items():
        if c not in pc:
            continue
        for mb in mbs:
            for pb in pc[c]:
                if mb["page"] == pb["page"]:
                    continue
                n = len(questions) + 1
                reg = {"m": [mb], "p": [pb], "m_text": eff_text(mb), "p_text": eff_text(pb)}
                questions.append(_q(f"S3-OWN-{n:03d}", mb["page"], "ownership", mb["kind"], None,
                                    "both", mb.get("bbox_norm"), reg,
                                    "这段内容印在哪一页上？（两引擎给出不同页）",
                                    anchors={"mineru": {"page": mb["page"], "bbox": mb.get("bbox_norm")},
                                             "paddle": {"page": pb["page"], "bbox": pb.get("bbox_norm")}}))

    # U-15：图区问责（最后追加，**不改变既有 item_id 的编号**）
    for page in pages:
        mp = [b for b in m if b["page"] == page and b["kind"] not in FURNITURE]
        pp = [b for b in p if b["page"] == page and b["kind"] not in FURNITURE]
        fq, fd = figure_region_questions(
            page, mp, pp, [q for q in questions if q["pdf_page"] == page])
        questions.extend(fq)
        dropped.extend(fd)

    questions.sort(key=lambda r: (r["pdf_page"], (r["bbox"] or [0, 1])[1] if r["bbox"] else 1,
                                  r["item_id"]))
    return {"questions": questions, "dropped": dropped,
            "counts": {"questions": len(questions), "dropped": len(dropped),
                       "by_type": {}, "dropped_by_reason": {}}}


def _tally(result: dict) -> None:
    for q in result["questions"]:
        t = q["review_type"]
        result["counts"]["by_type"][t] = result["counts"]["by_type"].get(t, 0) + 1
    for d in result["dropped"]:
        r = d["dropped_reason"]
        result["counts"]["dropped_by_reason"][r] = result["counts"]["dropped_by_reason"].get(r, 0) + 1


def write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def cmd_compare(args) -> int:
    res = compare(args.mineru, args.paddle)
    _tally(res)
    os.makedirs(args.out, exist_ok=True)
    write_jsonl(os.path.join(args.out, "questions.jsonl"), res["questions"])
    write_jsonl(os.path.join(args.out, "s3-dropped.jsonl"), res["dropped"])
    meta = {"mineru_ir": os.path.abspath(args.mineru), "paddle_ir": os.path.abspath(args.paddle),
            "comparison_unit": "region-union (SPEC §3.4)", **res["counts"]}
    with open(os.path.join(args.out, "s3-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- selftest
def _blk(engine, page, i, kind, text, bbox, role="primary", parent=None):
    return {"block_id": f"{engine}-p{page:04d}-b{i:03d}-x", "engine": engine, "page": page,
            "order": i, "kind": kind, "role": role, "parent_id": parent,
            "source_field": "text", "text_raw": text,
            "text_canon": textnorm.norm_strong(text), "bbox_norm": bbox,
            "bbox_src": None, "bbox_space": "x", "flags": []}


def selftest() -> int:
    import tempfile
    cases, passed = [], 0

    def run(mrows, prows):
        with tempfile.TemporaryDirectory() as td:
            mp, pp = os.path.join(td, "m.jsonl"), os.path.join(td, "p.jsonl")
            write_jsonl(mp, mrows); write_jsonl(pp, prows)
            r = compare(mp, pp); _tally(r); return r

    r = run([_blk("mineru", 1, 1, "paragraph", "气体状态方程", [0.1, 0.1, 0.5, 0.2])],
            [_blk("paddle", 1, 1, "paragraph", "气体状态方程", [0.1, 0.1, 0.5, 0.2])])
    cases.append(("完全相同 → 0 问题", r["counts"]["questions"] == 0, r["counts"]))

    r = run([_blk("mineru", 1, 1, "paragraph", "只有主引擎有的段落", [0.1, 0.1, 0.5, 0.2])], [])
    q = r["questions"][0] if r["questions"] else {}
    cases.append(("主有见证无 → existence_mineru_only + anchor=mineru",
                  q.get("review_type") == "existence_mineru_only" and q.get("anchor_provider") == "mineru", q))

    r = run([], [_blk("paddle", 1, 1, "paragraph", "只有见证引擎有的段落", [0.1, 0.1, 0.5, 0.2])])
    q = r["questions"][0] if r["questions"] else {}
    cases.append(("见证有主无 → existence_paddle_only + anchor=paddle",
                  q.get("review_type") == "existence_paddle_only" and q.get("anchor_provider") == "paddle", q))

    r = run([_blk("mineru", 1, 1, "paragraph", "表 2.1 的数据", [0.1, 0.1, 0.5, 0.2])],
            [_blk("paddle", 1, 1, "paragraph", "表2.1的数据", [0.1, 0.1, 0.5, 0.2])])
    cases.append(("空白差异 → N-2 丢弃", r["counts"]["questions"] == 0
                  and "N-2_no_content_diff" in r["counts"]["dropped_by_reason"], r["counts"]))

    r = run([_blk("mineru", 1, 1, "equation", r"t=^{\circ}C", [0.1, 0.1, 0.5, 0.2])],
            [_blk("paddle", 1, 1, "equation", "t=℃", [0.1, 0.1, 0.5, 0.2])])
    cases.append(("记号别名 → N-3 丢弃", r["counts"]["questions"] == 0
                  and "N-3_notation_alias_only" in r["counts"]["dropped_by_reason"], r["counts"]))

    r = run([_blk("mineru", 1, 1, "paragraph", "凝固点是 1.0 K(25 atm)", [0.1, 0.1, 0.9, 0.2])],
            [_blk("paddle", 1, 1, "paragraph", "凝固点是 4.2 K(25 atm)", [0.1, 0.1, 0.9, 0.2])])
    q = r["questions"][0] if r["questions"] else {}
    cases.append(("数字差异 → content_diff（带区间与 risk）",
                  q.get("review_type") == "content_diff" and q.get("span_diff")
                  and "digit" in q["risk"]["signals"], q.get("risk")))

    r = run([_blk("mineru", 1, 1, "page_number", "10", [0.1, 0.9, 0.2, 0.95])],
            [_blk("paddle", 1, 1, "page_number", "10", [0.1, 0.9, 0.2, 0.95])])
    cases.append(("家具被排除", r["counts"]["questions"] == 0
                  and "furniture_excluded" in r["counts"]["dropped_by_reason"], r["counts"]))

    # **U-11 的核心用例**：MinerU 1 块 = Paddle 3 块（并段 vs 切段）→ 区域并集后文本相同 → 0 问题
    merged = "第一句内容。第二句内容。第三句内容。"
    r = run([_blk("mineru", 1, 1, "paragraph", merged, [0.1, 0.10, 0.9, 0.25])],
            [_blk("paddle", 1, 1, "paragraph", "第一句内容。", [0.1, 0.10, 0.9, 0.15]),
             _blk("paddle", 1, 2, "paragraph", "第二句内容。", [0.1, 0.15, 0.9, 0.20]),
             _blk("paddle", 1, 3, "paragraph", "第三句内容。", [0.1, 0.20, 0.9, 0.25])])
    cases.append(("U-11：并段 vs 切段 → 区域并集相同 → 0 问题",
                  r["counts"]["questions"] == 0, r["counts"]))

    # U-11 反例：并段 vs 切段但内容真的不同 → 仍要报 1 条（区域级），而不是 3 条
    r = run([_blk("mineru", 1, 1, "paragraph", "第一句 1.0 K。第二句内容。第三句内容。", [0.1, 0.10, 0.9, 0.25])],
            [_blk("paddle", 1, 1, "paragraph", "第一句 4.2 K。", [0.1, 0.10, 0.9, 0.15]),
             _blk("paddle", 1, 2, "paragraph", "第二句内容。", [0.1, 0.15, 0.9, 0.20]),
             _blk("paddle", 1, 3, "paragraph", "第三句内容。", [0.1, 0.20, 0.9, 0.25])])
    cases.append(("U-11：并段 vs 切段且有真差异 → 只报 1 条 content_diff",
                  r["counts"]["questions"] == 1
                  and r["questions"][0]["review_type"] == "content_diff", r["counts"]))

    r = run([_blk("mineru", 1, 1, "paragraph", "认识自然规律的长河中这是经历了两个多世纪", [0.1, 0.8, 0.9, 0.9])],
            [_blk("paddle", 2, 1, "paragraph", "认识自然规律的长河中这是经历了两个多世纪", [0.1, 0.1, 0.9, 0.2])])
    q = [x for x in r["questions"] if x["review_type"] == "ownership"]
    cases.append(("跨页归属 → ownership（双侧锚点）",
                  len(q) == 1 and q[0]["anchor_provider"] == "both"
                  and set(q[0]["anchors"]) == {"mineru", "paddle"}, q[0] if q else None))

    r = run([_blk("mineru", 1, 1, "table", "表 2.1 数据", [0.1, 0.1, 0.9, 0.5])],
            [_blk("paddle", 1, 1, "figure", "图 2.2 关系", [0.1, 0.1, 0.9, 0.5])])
    q = [x for x in r["questions"] if x["review_type"] == "structure_diff"]
    cases.append(("区域 kind 家族不同 → structure_diff", len(q) == 1, q[0] if q else None))

    r = run([_blk("mineru", 1, 1, "table", "表", [0.1, 0.1, 0.9, 0.5]),
             _blk("mineru", 1, 1, "table_body", "<table>body</table>", [0.1, 0.1, 0.9, 0.5],
                  role="child", parent="mineru-p0001-b001-x")], [])
    cases.append(("子块只作为父块有效文本参与（仍只 1 个区域问题）",
                  r["counts"]["questions"] == 1, r["counts"]))

    r = run([_blk("mineru", 1, 1, "equation", r"$$pV=nRT\tag{2.1}$$", [0.1, 0.1, 0.9, 0.2])],
            [_blk("paddle", 1, 1, "equation", "$$pV=nRT$$", [0.1, 0.1, 0.9, 0.2]),
             _blk("paddle", 1, 2, "formula_number", "(2.1)", [0.8, 0.1, 0.9, 0.2])])
    reasons = r["counts"]["dropped_by_reason"]
    cases.append(("编号表示法（\\tag vs 独立 formula_number）→ 不进队列",
                  r["counts"]["questions"] == 0
                  and ({"representation_formula_number", "all_regions_non_content",
                        "non_severe_text_variant"} & set(reasons)),
                  reasons))
    # 区间分类单元的**单元测试**：`\tag{2.1}` vs `(2.1)` 必须判为表示法差异，而裸数字差异必须判为 content
    c1 = classify_regions({"regions": [{"a_raw": r"\tag{2.1}", "b_raw": "(2.1)"}]})
    c2 = classify_regions({"regions": [{"a_raw": "1.0", "b_raw": "4.2"}]})
    cases.append(("区间分类：编号→representation_tag，裸数字→content",
                  c1["classes"] == ["representation_tag"] and c2["classes"] == ["content"],
                  {"tag": c1["classes"], "digit": c2["classes"]}))

    # U-13：MinerU 把图题挂在 figure 子块上（子块无自己的 bbox），Paddle 让它独立成块且位置略低
    r = run([_blk("mineru", 1, 1, "figure", "图 2.2 恒压下气体体积与温度的关系", [0.31, 0.27, 0.69, 0.47])],
            [_blk("paddle", 1, 1, "figure", "", [0.31, 0.27, 0.69, 0.47]),
             _blk("paddle", 1, 2, "caption", "图2.2 恒压下气体体积与温度的关系", [0.35, 0.48, 0.65, 0.49])])
    cases.append(("U-13：图块+图题（跨引擎挂载不同）→ 0 问题",
                  r["counts"]["questions"] == 0, r["counts"]))

    # ---- U-15：图区问责（图缺框；只读 IR，不渲染） ----
    m_fig = [_blk("mineru", 1, 1, "figure", "", [0.30, 0.60, 0.45, 0.70]),
             _blk("mineru", 1, 2, "figure", "", [0.65, 0.60, 0.80, 0.70])]
    p_fig = [_blk("paddle", 1, 1, "figure", "", [0.30, 0.60, 0.80, 0.70]),
             _blk("paddle", 1, 2, "caption", "叁键", [0.50, 0.71, 0.55, 0.72])]
    m_cap = m_fig + [_blk("mineru", 1, 3, "caption", "叁键", [0.30, 0.60, 0.45, 0.70],
                          role="child", parent="mineru-p0001-b001-x")]
    r = run(m_cap, p_fig)
    q = [x for x in r["questions"] if x["review_type"] == "figure_region"]
    cases.append(("U-15：图区问责正例 → figure_region + caption_hint",
                  len(q) == 1 and q[0]["caption_hint"] == "叁键"
                  and q[0]["anchor_provider"] == "paddle"
                  and q[0]["context"]["uncovered_frac"] >= 0.30, q))

    r = run(m_fig + [_blk("mineru", 1, 3, "equation", "", [0.44, 0.60, 0.66, 0.70])], p_fig)
    cases.append(("U-15：中间由 equation 交代 → 0 条 figure_region",
                  not [x for x in r["questions"] if x["review_type"] == "figure_region"],
                  r["counts"]))

    r = run([_blk("mineru", 1, 1, "figure", "", [0.25, 0.55, 0.85, 0.75])], p_fig)
    cases.append(("U-15：MinerU 图框更大（白边）→ 0 条 figure_region",
                  not [x for x in r["questions"] if x["review_type"] == "figure_region"],
                  r["counts"]))

    p_table = [p_fig[0], _blk("paddle", 1, 2, "caption", "表2.1 数据", [0.50, 0.71, 0.55, 0.72])]
    r = run(m_fig, p_table)
    q = [x for x in r["questions"] if x["review_type"] == "figure_region"]
    cases.append(("U-15：表题不当图题 → figure_region 但 caption_hint 为空",
                  len(q) == 1 and q[0]["caption_hint"] == "", q))

    m_small = [_blk("mineru", 1, 1, "figure", "", [0.30, 0.60, 0.62, 0.70]),
               _blk("mineru", 1, 2, "figure", "", [0.65, 0.60, 0.80, 0.70])]
    r = run(m_small, p_fig)
    cases.append(("U-15：洞太小（<30%）→ 0 条 figure_region",
                  not [x for x in r["questions"] if x["review_type"] == "figure_region"],
                  r["counts"]))

    r = run(m_fig, [])
    cases.append(("U-15：没有 Paddle 图框（两引擎都漏）→ 0 条 figure_region",
                  not [x for x in r["questions"] if x["review_type"] == "figure_region"],
                  r["counts"]))

    fq, fd = figure_region_questions(1, m_fig, p_fig,
                                     [{"review_type": "content_diff",
                                       "bbox": [0.44, 0.60, 0.66, 0.70]}])
    cases.append(("U-15：与已有 S3 问题重叠 ≥50% → 去重",
                  len(fq) == 0 and any(d["dropped_reason"] == "figure_region_merged" for d in fd),
                  fd))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S3 tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--mineru", required=True); c.add_argument("--paddle", required=True)
    c.add_argument("--out", required=True); c.set_defaults(func=cmd_compare)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
