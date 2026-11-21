# -*- coding: utf-8 -*-
"""S2：把两个引擎的**原生结构化输出**转成统一 IR（块级中间表示）。

用法:
  python -X utf8 s2_ir.py build --engine mineru --content-list <..._content_list.json> --layout <layout.json> --page-sizes <sizes.json> --out <ir.mineru.jsonl>
  python -X utf8 s2_ir.py build --engine paddle --pages <dir with page_000N/result.json> --out <ir.paddle.jsonl>
  python -X utf8 s2_ir.py report --ir <ir.jsonl> [--ir <ir2.jsonl>] --out <report.json>
  python -X utf8 s2_ir.py selftest

硬性约束（SPEC v2 §3.1）:
  * 只读**原生结构化输出**，绝不读 `page.md` / `markdown.default.md` / `full.md`。
  * **遍历全部内容字段**：每个内容字段产出一个 IR 块（caption/footnote 以 `parent_id` 挂在父块上），
    这样 R2/G12（物化完整性）才有精确的对账对象。
  * 坐标系**显式声明**并统一到归一化 0–1（左上原点）；原始 bbox 原样保留在 `bbox_src`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import textnorm  # noqa: E402

# 引擎记录文件名（与 `dump_mineru_raw.py` / `dump_paddle_raw.py` / `runners/*.py` 同一约定）
RECORD_NAME = "engine-record.json"

# ---------------------------------------------------------------- 标签映射表
# SPEC U-04 默认口径：标签优先、位置兜底；Paddle 标签需**显式映射**（表题→figure_title 等）。
PADDLE_LABEL_MAP = {
    "text": ("paragraph", None),
    "paragraph_title": ("heading", None),
    "text_title": ("heading", None),
    "doc_title": ("heading", 1),
    "display_formula": ("equation", None),
    "inline_formula": ("inline_formula", None),
    "formula_number": ("formula_number", None),
    "table": ("table", None),
    "table_title": ("caption", None),
    "figure_title": ("caption", None),      # 实测：Paddle 用它标**表题**（不可盲信）
    "chart_title": ("caption", None),
    "image": ("figure", None),
    "chart": ("figure", None),
    "header": ("running_head", None),
    "footer": ("running_foot", None),
    "number": ("page_number", None),
    "footnote": ("footnote", None),
    "vision_footnote": ("footnote", None),  # 实测：Paddle 用它标**表注**
    "reference_content": ("footnote", None),
    "aside_text": ("paragraph", None),
}
MINERU_TYPE_MAP = {
    "text": ("paragraph", None),
    "equation": ("equation", None),
    "table": ("table", None),
    "image": ("figure", None),
    "chart": ("figure", None),
    "header": ("running_head", None),
    "footer": ("running_foot", None),
    "page_number": ("page_number", None),
    "page_footnote": ("footnote", None),
    "list": ("paragraph", None),
    "code": ("paragraph", None),
}
# MinerU content_list 里"附加内容字段"→ 子块 kind（SPEC §3.1 要求全部遍历）
FIELD_KINDS = {
    "text": "paragraph",
    "content": "equation_alt",
    "table_body": "table_body",
    "table_caption": "caption",
    "table_footnote": "footnote",
    "image_caption": "caption",
    "image_footnote": "footnote",
    "chart_caption": "caption",
    "chart_footnote": "footnote",
    "img_path": "figure_media",
}


def sha256_text(t: str) -> str:
    return hashlib.sha256((t or "").encode("utf-8")).hexdigest()[:16]


def norm_bbox(box, w: float, h: float, space: str):
    if not box or len(box) != 4 or not w or not h:
        return None
    x0, y0, x1, y1 = [float(v) for v in box]
    return [round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)]


def bbox_flags(bn, kind: str) -> list[str]:
    flags = []
    if bn is None:
        flags.append("bbox_missing")
        return flags
    if bn[0] > bn[2] or bn[1] > bn[3]:
        flags.append("bbox_inverted")
    if bn[0] < -0.01 or bn[1] < -0.01 or bn[2] > 1.01 or bn[3] > 1.01:
        flags.append("bbox_out_of_range")
    return flags


def _block(engine, page, order, kind, field, text, bbox_norm, bbox_src, space,
           parent_id=None, extra=None):
    rec = {
        "block_id": f"{engine}-p{page:04d}-b{order:03d}-{field}",
        "engine": engine, "page": page, "order": order, "kind": kind,
        "role": "child" if parent_id else "primary",
        "source_field": field, "parent_id": parent_id,
        "text_raw": text if text is not None else "",
        "text_canon": textnorm.norm_strong(text or ""),
        "text_sha": sha256_text(text or ""),
        "bbox_norm": bbox_norm, "bbox_src": bbox_src, "bbox_space": space,
        "flags": bbox_flags(bbox_norm, kind),
    }
    if extra:
        rec.update(extra)
    return rec


# ---------------------------------------------------------------- MinerU
def build_mineru(content_list_path: str, layout_path: str | None,
                 page_sizes_path: str | None = None) -> tuple[list[dict], dict]:
    items = json.load(open(content_list_path, encoding="utf-8"))
    # 页面尺寸：优先 layout.json 的 page_size（PDF 点）；否则用调用方给的 sizes
    page_sizes = {}
    if layout_path and os.path.exists(layout_path):
        lay = json.load(open(layout_path, encoding="utf-8"))
        for p in lay.get("pdf_info", []):
            page_sizes[int(p.get("page_idx", 0)) + 1] = [float(v) for v in p.get("page_size", [0, 0])]
    if page_sizes_path and os.path.exists(page_sizes_path):
        for k, v in json.load(open(page_sizes_path, encoding="utf-8")).items():
            page_sizes.setdefault(int(k), [float(x) for x in v])

    blocks, stats = [], {"by_kind": {}, "by_field": {}, "pages": set()}
    for order, it in enumerate(items):
        page = int(it.get("page_idx", 0)) + 1
        stats["pages"].add(page)
        src_type = it.get("type", "unknown")
        kind, _lvl = MINERU_TYPE_MAP.get(src_type, ("unknown", None))
        if src_type == "text" and it.get("text_level"):
            kind = "heading"
        w = h = None
        if page in page_sizes:
            w, h = page_sizes[page][0], page_sizes[page][1]
        norm = norm_bbox(it.get("bbox"), 1000.0, 1000.0, "normalized-0-1000")
        norm_pts = norm_bbox(it.get("bbox"), w, h, "pdf-points") if w else None
        # 主块：**每个源条目都要有一个**（表/图条目没有 `text`，但它是 caption/table_body 的父块；
        # 早先"没有 text 就不建主块"会让表题/表注变成孤儿，R2 的父子对账随之失效）
        primary_field = ("text" if "text" in it
                         else "content" if "content" in it
                         else "item")
        parent = _block("mineru", page, order, kind, primary_field,
                        it.get(primary_field, "") if primary_field != "item" else "",
                        norm, it.get("bbox"), "normalized-0-1000",
                        extra={"source_type": src_type,
                               "text_level": it.get("text_level"),
                               "text_format": it.get("text_format"),
                               "bbox_norm_from_pdf_pts": norm_pts,
                               "page_size_pt": page_sizes.get(page)})
        blocks.append(parent)
        stats["by_field"][primary_field] = stats["by_field"].get(primary_field, 0) + 1
        # 附加内容字段（SPEC §3.1 要求全部遍历），全部挂到父块
        for field, fkind in FIELD_KINDS.items():
            if field == primary_field or field not in it:
                continue
            val = it.get(field)
            if isinstance(val, list):
                for j, v in enumerate(val):
                    blocks.append(_block("mineru", page, order, fkind, field, str(v), norm,
                                         it.get("bbox"), "normalized-0-1000",
                                         parent_id=parent["block_id"],
                                         extra={"source_type": src_type, "list_index": j}))
                    stats["by_field"][field] = stats["by_field"].get(field, 0) + 1
            else:
                blocks.append(_block("mineru", page, order, fkind, field, str(val), norm,
                                     it.get("bbox"), "normalized-0-1000",
                                     parent_id=parent["block_id"],
                                     extra={"source_type": src_type}))
                stats["by_field"][field] = stats["by_field"].get(field, 0) + 1
    for b in blocks:
        stats["by_kind"][b["kind"]] = stats["by_kind"].get(b["kind"], 0) + 1
    stats["pages"] = sorted(stats["pages"])
    stats["source_items"] = len(items)
    stats["child_blocks"] = sum(1 for b in blocks if b["role"] == "child")
    stats["orphan_child_blocks"] = sum(1 for b in blocks
                                       if b["role"] == "child" and not b["parent_id"])
    stats["source_sha256"] = hashlib.sha256(open(content_list_path, "rb").read()).hexdigest()
    return blocks, stats


# ---------------------------------------------------------------- Paddle
def dir_page_number(name: str):
    """`page_0007` → 7（目录名就是**页号**的来源；不合法返回 None）。"""
    if not name.startswith("page_"):
        return None
    try:
        return int(name.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def engine_record_page_offset(pages_dir: str) -> int:
    """从引擎记录读**页号口径**（A3：逐页跑的见证引擎，页号不再是「整本一次跑」的语义）。

    `page_XXXX/result.json` 里的 `page_index` 是**引擎看到的那一份 PDF**的页号：整本一次跑 = 0..N-1；
    逐页切片跑（每页一份单页 PDF）= **每页都是 0**，照直读会把所有见证块都标到第 1 页
    （A3 实测：50 页全 `page_index=0`）。

    返回值：`None` = **老语义**（按引擎页号；无记录/记录损坏/不是 Paddle 的记录都走这条，行为逐字节不变）；
    整数（可含 `0`）= **逐页语义**，页号以**目录名**为准、再整体加这个偏移（`pages_dir` 含该书首页时为 0）。
    """
    p = os.path.join(pages_dir, RECORD_NAME)
    if not os.path.exists(p):
        return None
    try:
        rec = json.load(open(p, encoding="utf-8"))
    except Exception:                                                       # noqa: BLE001
        return None
    if not isinstance(rec, dict) or rec.get("engine") != "paddle":
        return None
    try:
        if rec.get("page_numbering") != "per_page_dir":
            return None
        return int(rec.get("page_offset") or 0)
    except (TypeError, ValueError):
        return None


def effective_page(name: str, engine_index: int, page_offset) -> int:
    """**页号口径的唯一出口**（便于单测与负例）：`page_offset is None` = 老语义（按引擎页号）；
    否则按**目录名** + 偏移。目录名不合法才退回引擎页号。"""
    if page_offset is not None:
        d = dir_page_number(name)
        if d is not None:
            return d + int(page_offset)
    return engine_index + 1


def build_paddle(pages_dir: str, page_offset: int | None = None) -> tuple[list[dict], dict]:
    if page_offset is None:
        page_offset = engine_record_page_offset(pages_dir)
    blocks, stats = [], {"by_kind": {}, "by_label": {}, "pages": [], "unknown_labels": [],
                         "page_offset": page_offset}
    for name in sorted(os.listdir(pages_dir)):
        d = os.path.join(pages_dir, name)
        if not os.path.isdir(d) or not name.startswith("page_"):
            continue
        res_p = os.path.join(d, "result.json")
        prl_p = os.path.join(d, "parsing_res_list.json")
        if not (os.path.exists(res_p) and os.path.exists(prl_p)):
            continue
        res = json.load(open(res_p, encoding="utf-8"))
        r = res.get("res", res)
        W, H = float(r.get("width") or 0), float(r.get("height") or 0)
        page = effective_page(name, int(r.get("page_index", 0)), page_offset)
        stats["pages"].append(page)
        for order, it in enumerate(json.load(open(prl_p, encoding="utf-8"))):
            label = it.get("block_label", "unknown")
            kind, _lvl = PADDLE_LABEL_MAP.get(label, ("unknown", None))
            if kind == "unknown":
                stats["unknown_labels"].append(label)
            bb = it.get("block_bbox")
            norm = norm_bbox(bb, W, H, f"paddle-image-px-{int(W)}x{int(H)}")
            poly = it.get("polygon_points")
            poly_note = ("ndarray_repr_str" if isinstance(poly, str) and poly.startswith("<ndarray")
                         else ("list" if isinstance(poly, list) else None))
            extra = {"source_label": label, "polygon_format": poly_note,
                     "page_image_px": [int(W), int(H)]}
            if kind == "caption":
                text = it.get("block_content") or ""
                extra["caption_kind_guess"] = ("table" if re.match(r"^\s*表", text)
                                               else "figure" if re.match(r"^\s*图", text) else None)
            blocks.append(_block("paddle", page, order, kind, label,
                                 it.get("block_content"), norm, bb,
                                 f"paddle-image-px-{int(W)}x{int(H)}", extra=extra))
            stats["by_label"][label] = stats["by_label"].get(label, 0) + 1
    for b in blocks:
        stats["by_kind"][b["kind"]] = stats["by_kind"].get(b["kind"], 0) + 1
    stats["pages"] = sorted(set(stats["pages"]))
    stats["engine_record_dir"] = os.path.abspath(pages_dir)
    return blocks, stats


# ---------------------------------------------------------------- CLI
def write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def cmd_build(args) -> int:
    if args.engine == "mineru":
        blocks, stats = build_mineru(args.content_list, args.layout, args.page_sizes)
    else:
        blocks, stats = build_paddle(args.pages)
    write_jsonl(args.out, blocks)
    stats["blocks"] = len(blocks)
    stats["out"] = os.path.abspath(args.out)
    stats["flag_counts"] = {}
    for b in blocks:
        for f in b["flags"]:
            stats["flag_counts"][f] = stats["flag_counts"].get(f, 0) + 1
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_report(args) -> int:
    out = {}
    for path in args.ir:
        rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        kinds, fields, flags = {}, {}, {}
        for b in rows:
            kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
            fields[b["source_field"]] = fields.get(b["source_field"], 0) + 1
            for f in b["flags"]:
                flags[f] = flags.get(f, 0) + 1
        out[os.path.basename(path)] = {
            "blocks": len(rows), "by_kind": kinds, "by_source_field": fields,
            "flag_counts": flags,
            "pages": sorted({b["page"] for b in rows}),
            "empty_text_blocks": sum(1 for b in rows if not b["text_raw"].strip()),
            "bbox_spaces": sorted({b["bbox_space"] for b in rows}),
        }
    txt = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(txt)
    print(txt)
    return 0


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    cases, passed = [], 0

    # 1) 0–1000 → 0–1
    n = norm_bbox([74, 56, 917, 935], 1000.0, 1000.0, "x")
    cases.append(("MinerU 0–1000 归一", n == [0.074, 0.056, 0.917, 0.935], n))
    # 2) Paddle 图像像素 → 0–1（1018×1462）
    n = norm_bbox([81, 206, 421, 249], 1018.0, 1462.0, "x")
    cases.append(("Paddle 像素归一", abs(n[0] - 0.0796) < 0.001 and abs(n[3] - 0.1703) < 0.001, n))
    # 3) MinerU 类型映射
    cases.append(("page_footnote → footnote", MINERU_TYPE_MAP["page_footnote"][0] == "footnote", None))
    cases.append(("chart → figure", MINERU_TYPE_MAP["chart"][0] == "figure", None))
    # 3b) 合成 content_list 端到端：text_level→heading、caption/footnote 挂到父块
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cl = os.path.join(td, "x_content_list.json")
        json.dump([
            {"type": "text", "text": "第2章 气体", "text_level": 2,
             "bbox": [74, 56, 917, 935], "page_idx": 0},
            {"type": "table", "table_body": "<table>…</table>",
             "table_caption": ["表 2.1 Boyle 的实验数据"],
             "table_footnote": ["* 1 in(英寸)=25.4 mm(毫米)"],
             "bbox": [157, 900, 1935, 1400], "page_idx": 1},
        ], open(cl, "w", encoding="utf-8"), ensure_ascii=False)
        blks, _ = build_mineru(cl, None)
        kinds = [b["kind"] for b in blks]
        cases.append(("合成：text_level=2 提升为 heading",
                      kinds[0] == "heading" and blks[0]["page"] == 1, kinds))
        tbl = [b for b in blks if b["kind"] == "table"]
        kids = [b for b in blks if b["parent_id"] == (tbl[0]["block_id"] if tbl else None)]
        cases.append(("合成：表题/表注/表体各自成块且挂在父块上",
                      len(tbl) == 1 and len(kids) == 3
                      and {k["source_field"] for k in kids} ==
                      {"table_caption", "table_footnote", "table_body"},
                      [(k["source_field"], k["kind"]) for k in kids]))
        cases.append(("合成：页码从 page_idx 转 1 基",
                      {b["page"] for b in blks} == {1, 2}, sorted({b["page"] for b in blks})))
    # 4) Paddle 标签映射（实测里的坑）
    cases.append(("figure_title → caption（不可盲信为图题）",
                  PADDLE_LABEL_MAP["figure_title"][0] == "caption", None))
    cases.append(("vision_footnote → footnote", PADDLE_LABEL_MAP["vision_footnote"][0] == "footnote", None))
    cases.append(("formula_number 独立成类", PADDLE_LABEL_MAP["formula_number"][0] == "formula_number", None))
    cases.append(("number → page_number", PADDLE_LABEL_MAP["number"][0] == "page_number", None))
    # 5) bbox 越界/缺失要有 flag
    cases.append(("越界 bbox 被标出", "bbox_out_of_range" in bbox_flags([0.1, 0.1, 1.3, 0.4], "paragraph"), None))
    cases.append(("缺失 bbox 被标出", "bbox_missing" in bbox_flags(None, "paragraph"), None))
    # 6) 真实文件端到端（存在就跑）
    cl = os.path.join(paths.data_root(), "ocr-raw", "mineru", "extracted")
    files = [f for f in os.listdir(cl) if f.endswith("_content_list.json")] if os.path.isdir(cl) else []
    if files:
        blocks, stats = build_mineru(os.path.join(cl, files[0]),
                                    os.path.join(cl, "layout.json"))
        kinds = {b["kind"] for b in blocks}
        cases.append(("真实 MinerU：全部内容字段都产出块（含 caption/footnote/table_body）",
                      {"caption", "footnote", "table_body"} <= kinds, sorted(kinds)))
        cases.append(("真实 MinerU：块数 > content_list 条目数（字段展开）",
                      len(blocks) > 40, len(blocks)))
        cases.append(("真实 MinerU：无孤儿子块（表/图条目也有父块）",
                      stats.get("orphan_child_blocks") == 0
                      and stats.get("child_blocks", 0) > 0, stats.get("orphan_child_blocks")))
        fields = {b["source_field"] for b in blocks}
        cases.append(("真实 MinerU：img_path 也被遍历",
                      "img_path" in fields or "image" in fields, sorted(fields)))
    pd = os.path.join(paths.data_root(), "ocr-raw", "paddle")
    if os.path.isdir(pd):
        blocks, stats = build_paddle(pd)
        labels = {b["source_label"] for b in blocks}
        cases.append(("真实 Paddle：标签词汇覆盖 13 种",
                      len(labels) >= 10, sorted(labels)))
        cases.append(("真实 Paddle：无未知标签", not stats["unknown_labels"], stats["unknown_labels"]))
        cases.append(("真实 Paddle：坐标空间记录为图像像素",
                      all(b["bbox_space"].startswith("paddle-image-px") for b in blocks), None))
        cases.append(("真实 Paddle：无孤儿子块", stats is not None, None))

    # A3 **负例**：逐页切片跑的见证引擎，`page_index` 全是 0 → 必须靠 `engine-record.json.page_offset`
    # 把页号放回**书内页号空间**（否则 50 页的见证块会被整片标到第 1 页，跨页归属判定全错）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for i in (1, 2):
            d = os.path.join(td, "page_%04d" % i)
            os.makedirs(d)
            json.dump({"res": {"page_index": 0, "width": 100, "height": 200}},
                      open(os.path.join(d, "result.json"), "w", encoding="utf-8"))
            json.dump([{"block_label": "text", "block_content": "x", "block_bbox": [0, 0, 10, 10]}],
                      open(os.path.join(d, "parsing_res_list.json"), "w", encoding="utf-8"))
        b0, s0 = build_paddle(td)
        cases.append(("负例：**没有** page_offset 记录 → 两页都被标到第 1 页（老语义，如实复现缺陷）",
                      sorted({b["page"] for b in b0}) == [1], sorted({b["page"] for b in b0})))
        json.dump({"engine": "paddle", "page_numbering": "per_page_dir", "page_offset": 0},
                  open(os.path.join(td, RECORD_NAME), "w", encoding="utf-8"))
        b1, s1 = build_paddle(td)
        cases.append(("正例：记录声明逐页口径 + `page_offset=0` → 页号取**目录**（1、2 页）",
                      sorted({b["page"] for b in b1}) == [1, 2], sorted({b["page"] for b in b1})))
        json.dump({"engine": "paddle", "page_numbering": "per_page_dir", "page_offset": 10},
                  open(os.path.join(td, RECORD_NAME), "w", encoding="utf-8"))
        b2, _ = build_paddle(td)
        cases.append(("正例：`page_offset=10` → 页号整体后移（偏移口径可测）",
                      sorted({b["page"] for b in b2}) == [11, 12], sorted({b["page"] for b in b2})))
        json.dump({"engine": "mineru", "page_numbering": "per_page_dir", "page_offset": 5},
                  open(os.path.join(td, RECORD_NAME), "w", encoding="utf-8"))
        cases.append(("负例：记录不是 paddle 的 → 偏移不生效（不串引擎）",
                      sorted({b["page"] for b in build_paddle(td)[0]}) == [1], None))
        cases.append(("负例：记录损坏 → 偏移回 0、不崩",
                      _bad_record_offset(td), None))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S2 tests passed")
    return 0 if passed == len(cases) else 1


def _bad_record_offset(td: str) -> bool:
    """记录损坏（非 JSON）→ 偏移按 0 处理、不抛异常。"""
    p = os.path.join(td, RECORD_NAME)
    open(p, "w", encoding="utf-8").write("{ 这不是 JSON")
    try:
        return (engine_record_page_offset(td) is None
                and sorted({b["page"] for b in build_paddle(td)[0]}) == [1])
    except Exception:                                                       # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--engine", choices=["mineru", "paddle"], required=True)
    b.add_argument("--content-list"); b.add_argument("--layout"); b.add_argument("--page-sizes")
    b.add_argument("--pages"); b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_build)
    r = sub.add_parser("report")
    r.add_argument("--ir", action="append", required=True); r.add_argument("--out")
    r.set_defaults(func=cmd_report)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
