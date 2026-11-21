# -*- coding: utf-8 -*-
"""S4 确定性检查 R1–R4（SPEC v2 §4.3）：免费、不用 LLM。

用法:
  python -X utf8 s4_checks.py run --ir <ir.jsonl> --engine mineru --raw <content_list.json>
        [--images <extracted/images>] [--pdf <sample.pdf>] --out <dir> [--dpi 300]
  python -X utf8 s4_checks.py selftest

R1 有几何无内容：块有 bbox、有效文本为空、该 bbox 在渲染图上的墨迹密度 ≥ 阈值（默认 0.02），
                且内容**未落在别的字段**（有 img_path 的图/表块豁免）。
R2 物化完整性：原生输出的**每个非空内容字段**都要在 IR 里有对应块（按 源条目序号 + 字段名 对账）。
R3 媒体落盘：原生声明 `img_path` 的图片必须**存在**且**被 IR 引用**。
R4 文本自洽性：① 只含单个符号的行间公式；② 推导序列标记（首先/其次/然后/代入…）必须成序列；③ 页末块是否为完整句。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import textnorm  # noqa: E402
from contracts import FURNITURE
from tuning import TUNE
import render
INK_THRESHOLD = TUNE["ink_threshold_gray"]   # L2：像素级灰度阈值（与下面那条密度阈值不是一回事）

R1_INK_MIN = TUNE["r1_ink_min"]   # L2：直接决定 R1 命中集 → G11（硬门禁）
CONTENT_FIELDS = ("text", "content", "table_body", "table_caption", "table_footnote",
                  "image_caption", "image_footnote", "chart_caption", "chart_footnote", "img_path")
SINGLE_SYMBOL_EQ = re.compile(r"^\$+\s*[A-Za-z](?:\s*[_^]\s*\{[^{}]*\})?\s*\$+$")
SEQ_MARKERS = ["首先", "其次", "再次", "然后", "最后"]
SENT_END = "。！？；：.!?;:”』）)】"


# ---------------------------------------------------------------- helpers
def load_jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def eff_text(block) -> str:
    if block.get("_eff") is not None:
        return block["_eff"]
    parts = [block.get("text_raw") or ""]
    for kid in block.get("_children", []):
        if kid.get("kind") == "figure_media" or kid.get("source_field") == "img_path":
            continue
        parts.append(kid.get("text_raw") or "")
    block["_eff"] = "\n".join(p for p in parts if p).strip()
    return block["_eff"]


def attach_children(blocks):
    kids = {}
    for b in blocks:
        if b.get("role") == "child" and b.get("parent_id"):
            kids.setdefault(b["parent_id"], []).append(b)
    for b in blocks:
        b["_children"] = kids.get(b["block_id"], [])
    return blocks


def ink_density(gray: np.ndarray, bbox_norm) -> float:
    if gray is None or not bbox_norm:
        return 0.0
    h, w = gray.shape
    x0 = max(0, min(w - 1, int(bbox_norm[0] * w)))
    x1 = max(x0 + 1, min(w, int(bbox_norm[2] * w)))
    y0 = max(0, min(h - 1, int(bbox_norm[1] * h)))
    y1 = max(y0 + 1, min(h, int(bbox_norm[3] * h)))
    patch = gray[y0:y1, x0:x1]
    if patch.size == 0:
        return 0.0
    return float((patch < INK_THRESHOLD).mean())


def render_gray(pdf_path: str, page: int, dpi: int) -> np.ndarray:
    """某页 → 灰度数组（0=黑）。渲染后端见 `render.py`（pypdfium2/PDFium）。"""
    return render.render_gray_page(pdf_path, page, dpi)


# ---------------------------------------------------------------- R1..R4
def r1_geometry_without_content(blocks, pdf_path, dpi) -> list[dict]:
    hits, cache = [], {}
    for b in blocks:
        if b.get("role") != "primary" or b["kind"] in FURNITURE or b["kind"].startswith("figure"):
            continue
        if eff_text(b):
            continue
        if any((k.get("text_raw") or "").strip() for k in b.get("_children", [])
               if k.get("source_field") == "img_path"):
            continue                      # 内容落在 img_path 字段里 → 豁免（SPEC §4.3 的承重豁免）
        if not b.get("bbox_norm"):
            continue
        pg = b["page"]
        if pg not in cache:
            cache[pg] = render_gray(pdf_path, pg, dpi) if pdf_path and os.path.exists(pdf_path) else None
        d = ink_density(cache[pg], b["bbox_norm"])
        if d >= R1_INK_MIN:
            hits.append({"rule": "R1", "page": pg, "block_id": b["block_id"], "kind": b["kind"],
                         "bbox_norm": b["bbox_norm"], "ink_density": round(d, 4),
                         "note": "有几何、无内容、区域有墨迹"})
    return hits


def r2_materialization(blocks, raw_path) -> list[dict]:
    raw = json.load(open(raw_path, encoding="utf-8"))
    have = {(b.get("order"), b.get("source_field")) for b in blocks}
    hits = []
    for order, item in enumerate(raw):
        if item.get("type") == "text" and item.get("text_level"):
            pass
        for field in CONTENT_FIELDS:
            if field not in item:
                continue
            val = item[field]
            vals = val if isinstance(val, list) else [val]
            for j, v in enumerate(vals):
                if not str(v).strip():
                    continue
                if (order, field) not in have:
                    hits.append({"rule": "R2", "order": order, "field": field, "list_index": j,
                                 "value": str(v)[:80], "note": "原生字段未物化进 IR"})
    return hits


def r3_media(blocks, raw_path, images_dir) -> list[dict]:
    raw = json.load(open(raw_path, encoding="utf-8"))
    referenced = {b["text_raw"].strip() for b in blocks if b.get("source_field") == "img_path"}
    hits = []
    for order, item in enumerate(raw):
        p = item.get("img_path")
        if not p:
            continue
        base = os.path.basename(str(p))
        exists = bool(images_dir) and os.path.exists(os.path.join(images_dir, base))
        if not exists or str(p) not in referenced:
            hits.append({"rule": "R3", "order": order, "img_path": str(p),
                         "on_disk": exists, "referenced_in_ir": str(p) in referenced,
                         "note": "媒体未落盘或未被引用"})
    return hits


def _clause_initial(text: str, marker: str) -> list:
    """返回 `marker` 在 `text` 中**出现在小句开头**的位置列表（**A7-1** 的核心判据）。

    **为什么需要它**：R4b 原先只看"这一页有没有出现「最后/然后」"，于是把
    「在**最后**的竖琴手同盟任务中」（名词修饰）、「D. **最后**的拐弯」（标题词）、
    「**最后**一个流传奇闻异事的地方」全判成"序列缺起点" —— 实测 **11 条里 10 条误报**。
    真正的序列标记会出现在**小句开头**（"首先，" / "然后，" / "最后。"），名词修饰则嵌在短语中间。

    判据：位置 `i` 合法 ⇔ `i == 0`，或前一字符是句子结束符/换行/分号/冒号。
    """
    out = []
    start = 0
    while True:
        i = text.find(marker, start)
        if i < 0:
            break
        start = i + len(marker)
        if i == 0:
            out.append(i)
            continue
        prev = text[i - 1]
        if prev in "。！？；：\n" or (prev == "." and not text[i - 2:i - 1].isdigit()):
            out.append(i)
    return out


def seq_marker_hits(blocks: list, page: int) -> list:
    """**R4b（A7-1 收紧版）**：出现后续序列标记、但**缺少起点**（首先…）。

    与旧口径的差别（旧口径实测 10/11 误报）：

    | | 旧 | 新 |
    |---|---|---|
    | 标记位置 | **不看**，页面里出现就算 | **必须出现在小句开头**（见 `_clause_initial`） |
    | 标题块 | 照算 | **排除**（`kind == "heading"` 或带 `text_level`）：标题里的「D. 最后的拐弯」是名词 |
    | 标记个数 | **1 个**就命中 | **至少 2 处**才构成"序列"（单处更可能是普通用词） |
    | 命中证据 | 只记页号与词 | **记下标记所在块 `block_id` + `bbox`**（便于精确裁图复核，A7-1 要求） |

    这样"真缺起点"仍会被抓到（例如"然后…，最后…"却通篇没有"首先"），而"最后一个…的地方"这类
    普通连词/名词修饰不再误报。返回 hit 列表（保持 `rule=R4b` 与 `note` 字段兼容）。
    """
    starts, marks = [], []
    for b in blocks:
        if b.get("kind") == "heading" or b.get("text_level"):
            continue                                          # 标题里的序列词是名词用法，不是序列
        t = (b.get("text_raw") or b.get("text") or "")
        for m in SEQ_MARKERS:
            for pos in _clause_initial(t, m):
                starts.append(m)
                marks.append({"marker": m, "block_id": b.get("block_id"),
                              "bbox_norm": b.get("bbox_norm"), "pos": pos})
    if len(starts) < 2:
        return []                                             # 单处不构成"序列"
    if not any(m == "首先" for m in starts):
        return [{"rule": "R4b", "page": page,
                 "markers": sorted(set(starts)),
                 "mark_blocks": marks,
                 "note": "出现后续序列标记（小句开头）但没有起点（首先…）"}]
    return []


def r4_self_consistency(blocks) -> list[dict]:
    hits = []
    content = [b for b in blocks if b.get("role") == "primary" and b["kind"] not in FURNITURE]
    for b in content:
        t = (b.get("text_raw") or "").strip()
        if b["kind"] == "equation" and SINGLE_SYMBOL_EQ.match(t.replace("\n", "")):
            hits.append({"rule": "R4a", "page": b["page"], "block_id": b["block_id"],
                         "text": t[:60], "note": "只含单个符号的行间公式（推导链条可疑）"})
    # 序列标记必须成序列（**A7-1 收紧**：见 `seq_marker_hits()` 的 docstring）
    by_page = {}
    for b in content:
        by_page.setdefault(b["page"], []).append(b)
    for page, bs in by_page.items():
        bs.sort(key=lambda x: x.get("bbox_norm", [0, 0])[1])
        for _hit in seq_marker_hits(bs, page):
            hits.append(_hit)
    # 页末块是否完整句
    for page, bs in by_page.items():
        last = bs[-1]
        t = eff_text(last).strip()
        if not t or last["kind"] in ("equation", "figure", "table"):
            continue
        if t[-1] not in SENT_END:
            hits.append({"rule": "R4c", "page": page, "block_id": last["block_id"],
                         "tail": t[-24:], "note": "页末块不是完整句（可能被页边界截断）"})
    return hits


# ---------------------------------------------------------------- CLI
def cmd_run(args) -> int:
    blocks = attach_children(load_jsonl(args.ir))
    os.makedirs(args.out, exist_ok=True)
    hits = []
    hits += r1_geometry_without_content(blocks, args.pdf, args.dpi)
    hits += r4_self_consistency(blocks)
    if args.raw:
        hits += r2_materialization(blocks, args.raw)
        if args.images:
            hits += r3_media(blocks, args.raw, args.images)
    summary = {"ir": os.path.abspath(args.ir), "blocks": len(blocks),
               "pdf": args.pdf, "raw": args.raw, "hits_total": len(hits),
               "by_rule": {}}
    for h in hits:
        summary["by_rule"][h["rule"]] = summary["by_rule"].get(h["rule"], 0) + 1
    with open(os.path.join(args.out, "s4-hits.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for h in hits:
            fh.write(json.dumps(h, ensure_ascii=False, sort_keys=True) + "\n")
    with open(os.path.join(args.out, "s4-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- selftest
def _blk(page, i, kind, text, bbox, role="primary", parent=None, field="text", engine="mineru"):
    return {"block_id": f"{engine}-p{page:04d}-b{i:03d}-{field}", "engine": engine, "page": page,
            "order": i, "kind": kind, "role": role, "parent_id": parent, "source_field": field,
            "text_raw": text, "text_canon": textnorm.norm_strong(text), "bbox_norm": bbox,
            "bbox_src": None, "bbox_space": "x", "flags": []}


def selftest() -> int:
    import tempfile
    cases, passed = [], 0

    # R1：有几何无内容且区域有墨 → 命中；有 img_path 的图块 → 豁免
    gray_ink = np.full((100, 100), 255, dtype=np.uint8)
    gray_ink[40:60, 40:60] = 0
    cases.append(("R1 墨迹密度计算", abs(ink_density(gray_ink, [0.4, 0.4, 0.6, 0.6]) - 1.0) < 1e-6,
                  ink_density(gray_ink, [0.4, 0.4, 0.6, 0.6])))
    cases.append(("R1 空白区域密度为 0", ink_density(gray_ink, [0.0, 0.0, 0.2, 0.2]) == 0.0, None))

    # R2：原生字段未物化 → 命中
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "cl.json")
        json.dump([{"type": "table", "table_body": "<table>x</table>",
                    "table_caption": ["表 2.1 数据"], "bbox": [1, 2, 3, 4], "page_idx": 0}],
                  open(raw, "w", encoding="utf-8"), ensure_ascii=False)
        blks = [_blk(1, 0, "table_body", "<table>x</table>", [0.1, 0.1, 0.9, 0.5], field="table_body")]
        hits = r2_materialization(blks, raw)
        cases.append(("R2 缺 table_caption → 命中", len(hits) == 1 and hits[0]["field"] == "table_caption", hits))
        blks2 = blks + [_blk(1, 0, "caption", "表 2.1 数据", [0.1, 0.1, 0.9, 0.2], field="table_caption")]
        cases.append(("R2 字段齐全 → 0 命中", not r2_materialization(blks2, raw), None))
        # R3：图片不在磁盘 / 未被引用 → 命中
        raw2 = os.path.join(td, "cl2.json")
        json.dump([{"type": "image", "img_path": "images/a.jpg", "bbox": [1, 2, 3, 4], "page_idx": 0}],
                  open(raw2, "w", encoding="utf-8"), ensure_ascii=False)
        hits = r3_media(blks2 + [_blk(1, 1, "figure_media", "images/a.jpg", [0.1, 0.1, 0.2, 0.2],
                                      field="img_path")], raw2, os.path.join(td, "images"))
        cases.append(("R3 图片不在磁盘 → 命中", len(hits) == 1 and hits[0]["on_disk"] is False, hits))

    # R4a：孤立单符号公式
    hits = r4_self_consistency([_blk(1, 0, "equation", "$$\nT _ {1}\n$$", [0.1, 0.1, 0.2, 0.2])])
    cases.append(("R4a 孤立单符号公式 → 命中", any(h["rule"] == "R4a" for h in hits), hits))
    hits = r4_self_consistency([_blk(1, 0, "equation", "$$p_{1}V_{1}=p_{2}V^{\\prime}$$", [0.1, 0.1, 0.4, 0.2])])
    cases.append(("R4a 正常公式 → 不命中", not hits, hits))

    # R4b（**A7-1 收紧后的口径**）：真序列且缺起点才命中
    #   ① 正例：两处**小句开头**的后续标记、通篇无「首先」→ 命中，且必须记下标记块
    hits = r4_self_consistency([_blk(1, 0, "paragraph", "然后，压力 $p_2$ 不变。", [0.1, 0.1, 0.5, 0.2]),
                                _blk(1, 1, "paragraph", "最后，体积 $V_2$ 不变。", [0.1, 0.2, 0.5, 0.3])])
    _r4b = [h for h in hits if h["rule"] == "R4b"]
    cases.append(("R4b 正例：两处小句开头的后续标记、无起点 → 命中且**记下标记块**（A7-1）",
                  len(_r4b) == 1 and len(_r4b[0].get("mark_blocks") or []) >= 2
                  and all(m.get("block_id") for m in _r4b[0]["mark_blocks"]), _r4b))
    cases.append(("R4b 成序列（首先 + 其次）→ 不命中",
                  not [h for h in r4_self_consistency(
                      [_blk(1, 0, "paragraph", "首先，温度 $T_1$ 不变。", [0.1, 0.1, 0.5, 0.2]),
                       _blk(1, 1, "paragraph", "其次，压力 $p_2$ 不变。", [0.1, 0.2, 0.5, 0.3])])
                      if h["rule"] == "R4b"], None))
    #   ② 负例（**实测踩到的两种误报形态**）：名词修饰 / 只出现一次 → 不命中
    cases.append(("R4b 负例：单处「其次」→ **不命中**（单处不构成序列，旧口径在此误报）",
                  not [h for h in r4_self_consistency(
                      [_blk(1, 0, "paragraph", "其次，压力 $p_2$ 不变。", [0.1, 0.1, 0.5, 0.2])])
                      if h["rule"] == "R4b"], None))
    cases.append(("R4b 负例：名词修饰「在最后的竖琴手同盟任务中」+「最后一个…的地方」→ **不命中**（实测真书误报形态）",
                  not [h for h in r4_self_consistency(
                      [_blk(1, 0, "paragraph", "在最后的竖琴手同盟任务中，蛇人会试图偷走它。", [0.1, 0.1, 0.6, 0.2]),
                       _blk(1, 1, "paragraph", "也是你们所知最后一个流传奇闻异事的地方。", [0.1, 0.2, 0.6, 0.3])])
                      if h["rule"] == "R4b"], None))
    cases.append(("R4b 负例：**标题**里的「D. 最后的拐弯」→ **不命中**（标题里的序列词是名词）",
                  not [h for h in r4_self_consistency(
                      [dict(_blk(1, 0, "heading", "D. 最后的拐弯 The Final Twist", [0.1, 0.1, 0.6, 0.2]),
                            text_level=2),
                       _blk(1, 1, "paragraph", "然后进入走廊。", [0.1, 0.2, 0.6, 0.3])])
                      if h["rule"] == "R4b"], None))
    #   （补一条）标题块是否被排除：用 text_level 标注的普通块也应豁免
    cases.append(("R4b：带 `text_level` 的块（标题语义）→ 其序列词不参与序列判定",
                  not [h for h in r4_self_consistency(
                      [dict(_blk(1, 0, "paragraph", "最后，走廊尽头。", [0.1, 0.1, 0.6, 0.2]), text_level=1),
                       _blk(1, 1, "paragraph", "然后进入走廊。", [0.1, 0.2, 0.6, 0.3])])
                      if h["rule"] == "R4b"], None))

    # R4c：页末非完整句
    hits = r4_self_consistency([_blk(1, 0, "paragraph", "以往也经", [0.1, 0.8, 0.9, 0.9])])
    cases.append(("R4c 页末截断 → 命中", any(h["rule"] == "R4c" for h in hits), hits))
    hits = r4_self_consistency([_blk(1, 0, "paragraph", "这是一个完整句。", [0.1, 0.8, 0.9, 0.9])])
    cases.append(("R4c 完整句 → 不命中", not hits, hits))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S4 tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--ir", required=True); r.add_argument("--engine", default="mineru")
    r.add_argument("--raw"); r.add_argument("--images"); r.add_argument("--pdf")
    r.add_argument("--out", required=True); r.add_argument("--dpi", type=int, default=300)
    r.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
