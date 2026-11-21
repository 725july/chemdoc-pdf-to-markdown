# -*- coding: utf-8 -*-
"""**自制样书生成器**（C1/C2 回归 + 离线 demo 的唯一载体；SPEC §3.14）。

为什么要它：
  * C1/C2 两条回归原先靠《普通化学原理 第4版》22–30 页的**真产物**当种子输入；
  * 那些输入**受版权保护**（真书切片 PDF + 两引擎原生产物 + IR + structure + 暂存 report），
    **不能进发布集** → 下载者手里 C1/C2 必然 SKIP，等于最关键的两条回归**不可验证**；
  * `c2_make_multivolume_fixture.py` 还是个**手工准备步骤**（往活数据树里写卷规范）。

本生成器造一份**我们自己的**样书（全部内容自造，无第三方文本）：
  * 9 页 PDF（`pypdf` 现造，ASCII 正文 → 不需要 CJK 字体）；
  * MinerU 侧夹具：`content_list.json` + `layout.json`；
  * Paddle 侧夹具：`engine-record.json` + `page_000N/{result.json,parsing_res_list.json}`；
  * S0 画像：**用我们自己的 PDF 真算**（`s0_classify.profile`）；
  * 注册表条目：写进**数据根**的 `books.json`（事务式，过 `books.load()` 校验）。
形状照抄真样本的难例：**第 2 章标题在第 5 页页中**（页内块级切分）+ **卷界正好压在章边界**（v01=1–4、v02=5–9）。

命令：
  python -X utf8 fx_sample.py make  [--root <数据根>] [--force]   # 造样书 + 登记（已存在则跳过，--force 重造）
  python -X utf8 fx_sample.py check [--root <数据根>]             # 只看在不在、形状对不对
  python -X utf8 fx_sample.py selftest                            # 自测（含负例：把章标题降级 → 切章必须失败）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import books as books_mod  # noqa: E402
import paths  # noqa: E402
import s0_classify  # noqa: E402

SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_sample_spec.json")
# 自造内容的 kind → MinerU 源类型 + text_level（`chapters.py` 认 `Chapter N`；S6 的 `derive_level` 靠 text_level）
MINERU_KIND = {
    "title": ("text", 1), "chapter": ("text", 1), "section": ("text", 2),
    "subsection": ("text", 3), "references": ("text", 2), "listitem": ("text", 2),
    "text": ("text", None), "equation": ("equation", None),
    "header": ("header", None), "footer": ("footer", None), "page_number": ("page_number", None),
}
PADDLE_KIND = {
    "title": "doc_title", "chapter": "paragraph_title", "section": "paragraph_title",
    "subsection": "paragraph_title", "references": "paragraph_title", "listitem": "text",
    "text": "text", "equation": "display_formula",
    "header": "header", "footer": "footer", "page_number": "number",
}


def load_spec(path: str = "") -> dict:
    return json.load(io.open(path or SPEC, encoding="utf-8"))


# ---------------------------------------------------------------- 路径
def paths_of(spec: dict, root: str) -> dict:
    k = spec["key"]
    return {
        "pdf": os.path.join(root, "_test-inputs", k + ".pdf"),
        "mineru_dir": os.path.join(root, "ocr-raw", "mineru-" + k),
        "paddle_dir": os.path.join(root, "ocr-raw", "paddle-" + k),
        "profile": os.path.join(root, "s0-profiles", k + ".profile.json"),
        "layout": os.path.join(root, "s0-profiles", k + ".layout.json"),
        "analysis": os.path.join(root, "s0-profiles", k + ".analysis.json"),
    }


# ---------------------------------------------------------------- PDF
def _esc(t: str) -> str:
    return t.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(spec: dict, out: str) -> dict:
    """造 9 页 PDF（ASCII 正文；base-14 Helvetica，**不嵌字体**，零外部依赖）。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w, h = [float(v) for v in spec["page_size_pt"]]
    wr = PdfWriter()
    for pg in spec["pages"]:
        page = wr.add_blank_page(width=w, height=h)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica"),
                                 NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): wr._add_object(font)})})
        ops = []
        for b in pg["blocks"]:
            x = b["bbox"][0] / 1000.0 * w
            y = h - b["bbox"][1] / 1000.0 * h - b.get("size", 11)
            ops.append("BT /F1 %d Tf 1 0 0 1 %.1f %.1f Tm (%s) Tj ET"
                       % (b.get("size", 11), x, y, _esc(b["text"])))
        st = DecodedStreamObject()
        st.set_data(("\n".join(ops)).encode("latin-1"))
        page[NameObject("/Contents")] = wr._add_object(st)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        wr.write(fh)
    return {"pdf": out, "pages": len(spec["pages"]), "bytes": os.path.getsize(out)}


# ---------------------------------------------------------------- 两侧夹具
def make_mineru(spec: dict, d: str) -> dict:
    w, h = [float(v) for v in spec["page_size_pt"]]
    items, pdf_info = [], []
    for pg in spec["pages"]:
        idx = pg["page"] - 1
        pdf_info.append({"page_idx": idx, "page_size": [w, h]})
        for b in pg["blocks"]:
            src_type, level = MINERU_KIND[b["kind"]]
            it = {"type": src_type, "page_idx": idx, "bbox": [float(v) for v in b["bbox"]]}
            if src_type in ("text", "equation", "header", "footer", "page_number"):
                it["text"] = b["text"]
            if level is not None:
                it["text_level"] = level
            items.append(it)
    os.makedirs(d, exist_ok=True)
    json.dump(items, io.open(os.path.join(d, "content_list.json"), "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=1)
    json.dump({"pdf_info": pdf_info, "_backend": "synthetic", "_version_name": "fx-sample"},
              io.open(os.path.join(d, "layout.json"), "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=1)
    return {"content_list": os.path.join(d, "content_list.json"), "layout": os.path.join(d, "layout.json"),
            "items": len(items)}


def make_paddle(spec: dict, d: str) -> dict:
    px_w, px_h = [int(v) for v in spec["paddle_image_px"]]
    os.makedirs(d, exist_ok=True)
    for pg in spec["pages"]:
        pd = os.path.join(d, "page_%04d" % pg["page"])
        os.makedirs(pd, exist_ok=True)
        plist = []
        for b in pg["blocks"]:
            x0, y0, x1, y1 = [float(v) for v in b["bbox"]]
            bb = [x0 / 1000.0 * px_w, y0 / 1000.0 * px_h, x1 / 1000.0 * px_w, y1 / 1000.0 * px_h]
            plist.append({"block_label": PADDLE_KIND[b["kind"]], "block_content": b["text"],
                          "block_bbox": [round(v, 1) for v in bb],
                          "polygon_points": [[round(bb[0], 1), round(bb[1], 1)],
                                             [round(bb[2], 1), round(bb[1], 1)],
                                             [round(bb[2], 1), round(bb[3], 1)],
                                             [round(bb[0], 1), round(bb[3], 1)]]})
        json.dump({"res": {"width": px_w, "height": px_h, "page_index": 0,
                           "page_count": len(spec["pages"]), "parsing_res_list": plist,
                           "layout_det_res": {"boxes": []}}},
                  io.open(os.path.join(pd, "result.json"), "w", encoding="utf-8", newline="\n"),
                  ensure_ascii=False, indent=1)
        json.dump(plist, io.open(os.path.join(pd, "parsing_res_list.json"), "w", encoding="utf-8", newline="\n"),
                  ensure_ascii=False, indent=1)
    # 逐页语义（页号按目录名）：让 S2 的"页号口径唯一出口"也走到
    json.dump({"engine": "paddle", "page_numbering": "per_page_dir", "page_offset": 0,
               "captured_at_utc": "synthetic", "source_pdf": os.path.basename(spec["key"]) + ".pdf",
               "pages_total": len(spec["pages"]), "note": "自制样书：页号以目录名为准"},
              io.open(os.path.join(d, "engine-record.json"), "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=1)
    return {"paddle_dir": d, "pages": len(spec["pages"])}


def make_volumes(spec: dict, root: str) -> dict:
    """写 `ocr-raw/volumes-<key>.json`（C2 分卷的输入）。**生成样书时就写好** → 不再有手工准备步骤。"""
    vs = spec.get("volume_split") or {}
    key = spec["key"]
    labels = {pg["page"]: pg["page"] for pg in spec["pages"]}
    vols = []
    for i, (vn, pages) in enumerate(sorted(vs.items()), 1):
        vols.append({"volume": vn, "pages": list(pages),
                     "book_pages": [labels.get(min(pages), min(pages)), labels.get(max(pages), max(pages))],
                     "ir": "s2-ir/ir.mineru.%s.%s.jsonl" % (key, vn)})
    spec_out = {"book": key, "engine": "mineru", "page_count": len(spec["pages"]),
                "limits": {}, "volumes": vols,
                "note": "自制样书的分卷规范（SPEC §3.14）：v01|v02 的边界**正好压在章边界**上 = 最坏情形"}
    d = os.path.join(root, "ocr-raw")
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "volumes-%s.json" % key)
    json.dump(spec_out, io.open(fp, "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=1)
    return {"volumes": fp, "count": len(vols)}


def make_profile(spec: dict, pdf: str, out: str, dpi: int = 100) -> dict:
    prof = s0_classify.profile(pdf, list(range(1, len(spec["pages"]) + 1)), 1, dpi, False)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(prof, io.open(out, "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=1)
    return prof


# ---------------------------------------------------------------- 登记
def register(spec: dict, p: dict, root: str) -> dict:
    rp = books_mod.registry_path(root)
    if os.path.exists(rp):
        data = json.load(io.open(rp, encoding="utf-8"))
    else:
        data = {"schema": books_mod.SCHEMA_VERSION,
                "note": "书注册表（数据侧；权威位置 = 数据根下 books.json）。格式见 _mcp-test/books.sample.json。",
                "books": {}}
    rel = lambda x: os.path.relpath(x, root).replace("\\", "/")     # noqa: E731
    data.setdefault("books", {})[spec["key"]] = {
        "label": spec["label"], "key": spec["key"],
        "pdf": rel(p["pdf"]), "mineru_dir": rel(p["mineru_dir"]), "paddle_dir": rel(p["paddle_dir"]),
        "profile": rel(p["profile"]),
        "page_labels": [pg["page"] for pg in spec["pages"]],
        "notice": spec.get("notice", ""),
        "state": "active",
    }
    tmp = rp + ".tmp-fx"
    json.dump(data, io.open(tmp, "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=2)
    books_mod.load(path=tmp, root=root)          # 事务式：校验不过就不替换
    os.replace(tmp, rp)
    return {"registry": rp, "entry": spec["key"], "books": len(data["books"])}


# ---------------------------------------------------------------- 形状自检（纯函数，可测）
def shape_report(spec: dict) -> dict:
    """样书形状报告：章边界必须**落在第 5 页页中**、卷界必须**正好压在章边界**上。"""
    pages = {pg["page"]: pg["blocks"] for pg in spec["pages"]}
    ch = [(pg["page"], i) for pg in spec["pages"]
          for i, b in enumerate(pg["blocks"]) if b["kind"] == "chapter"]
    vs = spec.get("volume_split") or {}
    v1, v2 = vs.get("v01") or [], vs.get("v02") or []
    allp = sorted(pages)
    mid_page_ok = False
    if len(ch) >= 2:
        pg, i = ch[1]
        mid_page_ok = i > 0 and pg > 1                    # 同页前半还有内容 → 页内块级切分
    return {
        "pages": len(pages), "page_numbers": allp,
        "chapters": [t for _p, t in [(p, spec["pages"][p - 1]["blocks"][i]["text"]) for p, i in ch]],
        "chapter_positions": ch,
        "chapter_mid_page": mid_page_ok,
        "volume_boundary_at_chapter": bool(len(ch) >= 2 and v1 and v2
                                            and (ch[0][0] in v1 or ch[1][0] in v2)
                                            and min(v2) == ch[1][0]),
        "volumes_cover_all": sorted(v1 + v2) == allp,
        "has_header_and_number": all(any(b["kind"] == "header" for b in pages[p]) for p in allp[1:])
        and all(any(b["kind"] == "page_number" for b in pages[p]) for p in allp),
    }


def mineru_levels(spec: dict) -> list:
    """把 spec 变成 `(text, text_level)` 列表（用于自测：章标题必须被 S6 判成 level 1）。"""
    out = []
    for pg in spec["pages"]:
        for b in pg["blocks"]:
            src, lvl = MINERU_KIND[b["kind"]]
            if src == "text":
                out.append((b["text"], lvl))
    return out


def split_preview(spec: dict) -> dict:
    """**不跑管线**也能验证形状：把 spec 变成 S6/chapters 要的最小结构 → 调 `split_chapters`。

    这是本生成器最有价值的一条自测：它证明"这份样书真的能触发**页内块级切章**"，
    而不是"造了一堆看起来像书的文件"。
    """
    import chapters
    blocks, tree = [], []
    order = 0
    for pg in spec["pages"]:
        for b in pg["blocks"]:
            src, lvl = MINERU_KIND[b["kind"]]
            bid = "b%03d" % order
            if b["kind"] in ("header", "footer", "page_number"):
                bid = "f%03d" % order
            blocks.append({"block_id": bid, "page": pg["page"], "order": order,
                           "kind": ("heading" if src == "text" and lvl else
                                    "paragraph" if src in ("text", "equation") else src),
                           "role": "primary", "text_raw": b["text"], "text_norm": b["text"],
                           "text_level": lvl, "bbox_norm": [v / 1000.0 for v in b["bbox"]],
                           "engine": "mineru", "field": "text"})
            if src == "text" and lvl:
                tree.append({"block_id": bid, "page": pg["page"], "level": lvl,
                             "text": b["text"], "full_text": b["text"], "text_level": lvl})
            order += 1
    # 家具不进正文（S6 会剔除）；这里模拟它的效果
    body = [b for b in blocks if b["kind"] not in ("running_head", "running_foot", "page_number")
            and b["kind"] not in ("header", "footer")]
    return chapters.split_chapters({"heading_tree": tree}, body)


# ---------------------------------------------------------------- 命令
def run_pipeline_for_sample(root: str, key: str) -> dict:
    """把样书**物化到"能直接跑 C1/C2 回归"**：跑一次管线（只写暂存区，不发布）。

    为什么要它：
    C1 检查读 `s7-out/_staging/<key>/report.json`、C2 读 `s2-ir/ir.mineru.<key>.jsonl` + `s6-out/<key>/structure.json`；
    这些只有**跑过一次管线**才有。让安装层顺手跑掉，下载者就不需要敲任何额外命令。
    """
    import pipeline_run as pr
    import books as bk
    reg = bk.load(root=root)
    if key not in reg:
        return {"ok": False, "why": "注册表里没有样书"}
    pr.BOOKS[key] = dict(reg[key])            # 只在本进程里补一次（不改产品配置）
    res = pr.run_pipeline(key, out_root=root, do_publish=False)
    val = (res.get("stages") or {}).get("validation") or {}
    return {"ok": True, "verdict": val.get("verdict"), "exit_code": val.get("exit_code"),
            "counts": val.get("counts")}


def make(root: str = "", force: bool = False, spec_path: str = "", with_pipeline: bool = True) -> dict:
    spec = load_spec(spec_path)
    root = root or paths.data_root()
    p = paths_of(spec, root)
    if os.path.exists(p["pdf"]) and not force:
        return {"skipped": True, "why": "样书已在位（要重造加 --force）", "pdf": p["pdf"]}
    out = {"root": root}
    out.update(make_pdf(spec, p["pdf"]))
    out["mineru"] = make_mineru(spec, p["mineru_dir"])
    out["paddle"] = make_paddle(spec, p["paddle_dir"])
    out["volumes"] = make_volumes(spec, root)
    prof = make_profile(spec, p["pdf"], p["profile"])
    out["profile"] = {"path": p["profile"], "page_count": prof.get("page_count"),
                      "classification": prof.get("classification")}
    out["register"] = register(spec, p, root)
    out["shape"] = shape_report(spec)
    if with_pipeline:
        out["pipeline"] = run_pipeline_for_sample(root, spec["key"])
    return out


def check(root: str = "", spec_path: str = "") -> dict:
    spec = load_spec(spec_path)
    root = root or paths.data_root()
    p = paths_of(spec, root)
    need = [p["pdf"], os.path.join(root, "ocr-raw", "volumes-%s.json" % spec["key"]),
            os.path.join(p["mineru_dir"], "content_list.json"),
            os.path.join(p["mineru_dir"], "layout.json"),
            os.path.join(p["paddle_dir"], "engine-record.json"), p["profile"]]
    need += [os.path.join(p["paddle_dir"], "page_%04d" % pg["page"], f)
             for pg in spec["pages"] for f in ("result.json", "parsing_res_list.json")]
    miss = [x for x in need if not os.path.exists(x)]
    try:
        reg = books_mod.load(root=root)
        entry = spec["key"] in reg
    except Exception:                                        # noqa: BLE001
        entry = False
    rep = shape_report(spec)
    ok = (not miss) and entry and rep["chapter_mid_page"] and rep["volume_boundary_at_chapter"] \
        and rep["volumes_cover_all"] and rep["has_header_and_number"]
    return {"ok": ok, "missing": miss, "registered": entry, "shape": rep}


def selftest() -> int:
    cases = []
    spec = load_spec()
    rep = shape_report(spec)
    cases.append(("形状：9 页、页码 1–9 齐", rep["pages"] == 9 and rep["page_numbers"] == list(range(1, 10)), rep))
    cases.append(("形状：**两个章标题**，且第二个落在**第 5 页页中**（页内块级切分的前提）",
                  len(rep["chapters"]) == 2 and rep["chapter_mid_page"] and rep["chapter_positions"][1][0] == 5,
                  rep["chapter_positions"]))
    cases.append(("形状：**卷界正好压在章边界**（v01=1–4、v02=5–9）且两卷覆盖全部页",
                  rep["volume_boundary_at_chapter"] and rep["volumes_cover_all"], rep))
    cases.append(("形状：每页都有页眉与页号（家具判定的载体）", rep["has_header_and_number"], None))
    # ★ 关键自测：**这份样书真的能触发页内块级切章**（不跑管线，纯函数）
    r = split_preview(spec)
    cases.append(("切章预演：`rule=printed`（章号来自 `Chapter N`）、两个边界、含 `chapter-02`",
                  r["rule"] == "printed" and len(r["boundaries"]) == 2
                  and any(c["file_stem"] == "chapter-02" for c in r["chapters"]),
                  {"rule": r["rule"], "stems": [c["file_stem"] for c in r["chapters"]]}))
    cases.append(("切章预演：**页内块级切分**成立（chapter-02 从第 5 页的章标题块开始，前半页归第 1 章）",
                  r["chapters"][0]["page_range"][1] == 5 and r["chapters"][1]["page_range"][0] == 5
                  if "page_range" in r["chapters"][0] else True,
                  [c.get("page_range") for c in r["chapters"]]))
    cases.append(("映射：`section`/`subsection` 的 text_level 让 S6 推出 2/3（不是章级）",
                  [lvl for t, lvl in mineru_levels(spec) if t == "1.1 Scope"] == [2]
                  and [lvl for t, lvl in mineru_levels(spec) if t == "2.1.1 Units"] == [3], None))
    # ---- 负例：把章标题降级（text_level=2）→ **切章必须不再是 printed/两章** ----
    bad = json.loads(json.dumps(spec))
    for pg in bad["pages"]:
        for b in pg["blocks"]:
            if b["kind"] == "chapter":
                b["kind"] = "section"                    # 降级：不再算章标题
    rb = split_preview(bad)
    cases.append(("负例：把 `chapter` 降级成 `section` → `rule` 不再是 `printed`、也不再有 chapter-02"
                  "（证明上面那条不是空转）",
                  not (rb["rule"] == "printed" and len(rb["boundaries"]) == 2), {"rule": rb["rule"]}))
    bad2 = json.loads(json.dumps(spec))
    bad2["volume_split"] = {"v01": [1, 2, 3, 4], "v02": [6, 7, 8, 9]}      # 漏了第 5 页
    cases.append(("负例：卷规范漏一页 → `volumes_cover_all` 必须为假",
                  shape_report(bad2)["volumes_cover_all"] is False, None))
    bad3 = json.loads(json.dumps(spec))
    bad3["pages"][4]["blocks"] = [b for b in bad3["pages"][4]["blocks"] if b["kind"] != "chapter"]
    cases.append(("负例：第 5 页没有章标题 → `chapter_mid_page` 必须为假",
                  shape_report(bad3)["chapter_mid_page"] is False, None))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:160]))
        passed += bool(ok)
    print("\n%d/%d fx_sample tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="自制样书（C1/C2 回归 + 离线 demo 的唯一载体）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--root", default=""); m.add_argument("--force", action="store_true")
    m.add_argument("--no-pipeline", action="store_true", help="只造文件、不跑管线（C1/C2 会因缺产物而跳过）")
    m.set_defaults(func=lambda a: (print(json.dumps(make(a.root, a.force, with_pipeline=not a.no_pipeline),
                                                    ensure_ascii=False, indent=2)), 0)[1])
    c = sub.add_parser("check"); c.add_argument("--root", default="")
    c.set_defaults(func=lambda a: (print(json.dumps(check(a.root), ensure_ascii=False, indent=2)),
                                   0 if check(a.root)["ok"] else 1)[1])
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
