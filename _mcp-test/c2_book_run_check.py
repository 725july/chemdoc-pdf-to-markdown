# -*- coding: utf-8 -*-
"""C2-2 回归：**书级流水线**（真跨卷）——合并卷 → S6/S7（C1 只跑一次）→ S8 → 书级判决。

样本：`-22-30.pdf` 的**冻结原生产物**（片内页 1–9 = 物理页 22–30 = 印刷页 6–14）
  * 第 2 章标题 `第2章 气体` 在**片内第 5 页**
  * 拆卷：**v01 = 片 1–4**、**v02 = 片 5–9**（章边界**正好落在卷界** → 最坏情形）

断言：
  A. `merge_volumes` 成功；`input_continuity.contiguous = true`（本输入页映射连续）
  B. 书级 S7 产出 `chapter-00-front` + `chapter-02`（**绝不出现第二个 `chapter-01`**）
  C. `report.json` 里带 `volumes[]` 与 `merge{}`（含 `input_continuity`）——**书级元数据落在书级报告**
  D. `chapter_split.assembly_check` 两条不变量为真（页标记集合相同 + 去注释正文逐行相同）
  E. 书级判决可得出（G20 可判且不因切章而 fail）
  F. **单卷对照**：同一份内容不拆卷直通 → 章号与拆分后**一致**（拆卷不改变章号）

用法: python -X utf8 c2_book_run_check.py [--root <数据根>]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import chapters as C        # noqa: E402
import books as books_mod   # noqa: E402
import volumes as V         # noqa: E402

DEFAULT_BOOK = "fx-sample"    # 自制样书（SPEC §3.14）：发布集里只有它；真样本只在本机显式传 --book
FXLIT = "fxbook2"
V1 = [1, 2, 3, 4]
V2 = [5, 6, 7, 8, 9]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=paths.data_root())
    ap.add_argument("--book", default=DEFAULT_BOOK,
                    help="种子输入取自哪本书的 `s2-ir`/`s6-out`（默认 = 自制样书）")
    a = ap.parse_args()
    KEY = a.book
    src_ir = os.path.join(a.root, "s2-ir", f"ir.mineru.{KEY}.jsonl")
    src_st = os.path.join(a.root, "s6-out", KEY, "structure.json")
    if not (os.path.exists(src_ir) and os.path.exists(src_st)):
        # **夹具缺失 → 明示跳过并说清缺哪个文件**（2026-10-13）。本样本是受版权书的切片及其衍生数据，
        # 发布集已排除 → 新机器上必然没有它；缺夹具是"跳过"，不是失败（与 `c2_volume_merge_check` 一致）。
        _miss = [p for p in (src_ir, src_st) if not os.path.exists(p)]
        print("SKIP：缺输入 " + "；".join(_miss))
        return 0
    rows = [json.loads(l) for l in open(src_ir, encoding="utf-8") if l.strip()]
    tree_src = json.load(open(src_st, encoding="utf-8"))["heading_tree"]
    root = tempfile.mkdtemp(prefix="c2book-")
    cases = []

    def add(name, ok, extra=None):
        cases.append((name, bool(ok), extra))

    try:
        os.makedirs(os.path.join(root, "s2-ir"), exist_ok=True)
        os.makedirs(os.path.join(root, "ocr-raw"), exist_ok=True)
        # 逐卷 IR：页号 = 该卷 cut_pages 里的页号
        for vol, pages in (("v01", V1), ("v02", V2)):
            with open(os.path.join(root, "s2-ir", f"ir.mineru.{FXLIT}.{vol}.jsonl"),
                      "w", encoding="utf-8", newline="\n") as fh:
                for p in pages:
                    for r in rows:
                        if r["page"] == p:
                            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        json.dump({"book": FXLIT, "engine": "mineru", "page_count": 9,
                   "volumes": [{"id": "v01", "cut_pages": V1, "book_pages": V1, "sha256": "V1"},
                               {"id": "v02", "cut_pages": V2, "book_pages": V2, "sha256": "V2"}]},
                  open(os.path.join(root, "ocr-raw", f"volumes-{FXLIT}.json"), "w",
                       encoding="utf-8"), ensure_ascii=False)

        # ---- A) 合并 ----
        m = V.merge_volumes(FXLIT, root, "mineru")
        ic = (m.get("merge") or {}).get("input_continuity") or {}
        add("A. 合并成功；输入页映射连续（input_continuity.contiguous=true）",
            m["ok"] and ic.get("contiguous") is True, ic)

        # ---- B/C/D) 书级切章：直接驱动 S7 的核心路径（与 run_book 同口径）----
        merged = V.load_ir(m["merged_ir"])
        tree_all = list(tree_src)                     # 页号已是书内页号（=片内页号）
        res = C.split_chapters({"heading_tree": tree_all}, merged)
        stems = [c["file_stem"] for c in res["chapters"]]
        add("B. 合并后按**书里印的章号**切：与不拆卷对照一致、**无重复章号**、含 `chapter-02`"
            "（章数随书而定）",
            stems == [c["file_stem"] for c in C.split_chapters({"heading_tree": tree_src}, rows)["chapters"]]
            and len(stems) == len(set(stems)) and any(s.startswith("chapter-02") for s in stems),
            [(c["file_stem"], c["pages"]) for c in res["chapters"]])
        add("C. 书级元数据齐备（volumes[] / merge{input_continuity} 可写进书级 report）",
            bool(m.get("volumes")) and "input_continuity" in (m.get("merge") or {}),
            {"volumes": len(m.get("volumes") or []),
             "merge_keys": sorted((m.get("merge") or {}).keys())})
        # 覆盖不变量只对**参与切章的块**（primary 且非家具）有意义：合并 IR 里还有子块与家具
        _n_primary = len([r for r in merged
                          if r.get("role") == "primary"
                          and r.get("kind") not in ("running_head", "running_foot", "page_number")])
        add("D. 覆盖不变量成立（参与切章的块恰好都属于某一章、块数守恒）",
            res["covered_blocks"] == res["content_blocks"] == _n_primary,
            {"covered": res["covered_blocks"], "content": res["content_blocks"],
             "primary_nonfurniture": _n_primary, "merged_rows": len(merged)})

        # ---- E) 同一内容**不拆卷**直通 → 章号必须一致 ----
        single_root = tempfile.mkdtemp(prefix="c2single-")
        try:
            os.makedirs(os.path.join(single_root, "s2-ir"), exist_ok=True)
            os.makedirs(os.path.join(single_root, "ocr-raw"), exist_ok=True)
            shutil.copy(src_ir, os.path.join(single_root, "s2-ir", f"ir.mineru.{FXLIT}.jsonl"))
            json.dump({"book": FXLIT, "engine": "mineru", "page_count": 9,
                       "volumes": [{"id": "v01", "cut_pages": [1, 2, 3, 4, 5, 6, 7, 8, 9],
                                    "book_pages": [1, 2, 3, 4, 5, 6, 7, 8, 9], "sha256": "S"}]},
                      open(os.path.join(single_root, "ocr-raw", f"volumes-{FXLIT}.json"), "w",
                           encoding="utf-8"), ensure_ascii=False)
            ms = V.merge_volumes(FXLIT, single_root, "mineru")
            rs = C.split_chapters({"heading_tree": tree_all}, V.load_ir(ms["merged_ir"]))
            add("E. **单卷直通**：章号与拆卷合并后**完全一致**（拆卷不改变切章结果）",
                ms["ok"] and [c["file_stem"] for c in rs["chapters"]] == stems,
                [(c["file_stem"], c["pages"]) for c in rs["chapters"]])
        finally:
            shutil.rmtree(single_root, ignore_errors=True)

        # ---- F) **真跑书级流水线 `run_book`**（端到端：合并 → S6/S7 → S8 → 书级判决）----
        import pipeline_run as pr
        KEY2 = "fxbookrun"
        # 书级真跑的**外在依赖只有 pdf/profile** → 从注册表取（不写死真样本路径）；
        # 混合夹具的 `s2-ir` 与卷规范在下面写到临时根里。
        _reg = books_mod.load(root=a.root)
        if KEY not in _reg:
            print("SKIP：注册表里没有 %s（先跑 fx_sample.py make 造样书）" % KEY)
            return 0
        pr.BOOKS[KEY2] = dict(_reg[KEY], key=KEY2, label="C2-2 回归（%s 的衍生夹具）" % KEY)
        br = tempfile.mkdtemp(prefix="c2run-")
        try:
            os.makedirs(os.path.join(br, "s2-ir"), exist_ok=True)
            os.makedirs(os.path.join(br, "ocr-raw"), exist_ok=True)
            # **夹具自建**：从种子书的逐页序列现场切出两卷（页号落在该卷 cut_pages 空间里）。
            # 这样不需要任何"预先切好的 v01/v02 文件"（老做法要先手工跑 c2_make_multivolume_fixture）。
            for vol, pages in (("v01", V1), ("v02", V2)):
                with open(os.path.join(br, "s2-ir", f"ir.mineru.{KEY2}.{vol}.jsonl"),
                          "w", encoding="utf-8", newline="\n") as fh:
                    for phys in pages:
                        for r in rows:
                            if r["page"] == phys:
                                fh.write(json.dumps(dict(r, page=phys), ensure_ascii=False,
                                                    sort_keys=True) + "\n")
            json.dump({"book": KEY2, "engine": "mineru", "page_count": 9,
                       "volumes": [{"id": "v01", "cut_pages": V1, "book_pages": V1, "sha256": "V1"},
                                   {"id": "v02", "cut_pages": V2, "book_pages": V2, "sha256": "V2"}]},
                      open(os.path.join(br, "ocr-raw", f"volumes-{KEY2}.json"), "w",
                           encoding="utf-8"), ensure_ascii=False)
            res = pr.run_book(KEY2, br, do_publish=False)
            s7 = res.get("stages", {}).get("S7", {})
            chs = [c[1] for c in s7.get("chapters", [])]
            add("F. **真跑 `run_book`**：书级流水线跑通、各章切出且**无重复章号**（含 `chapter-02`）",
                res.get("stages", {}).get("merge", {}).get("ok")
                and len(chs) == len(set(chs)) and any(c.startswith("chapter-02") for c in chs),
                {"merge": res.get("stages", {}).get("merge", {}).get("ok"), "chapters": chs})
            add("F2. `run_book` 的装配不变量为真 + 书级判决可得出（G20 可判）",
                (s7.get("assembly_check") or {}).get("pages_multiset_equal") is True
                and (s7.get("assembly_check") or {}).get("body_lines_equal") is True
                and res.get("stages", {}).get("validation", {}).get("G20") in ("pass", "fail", "warn"),
                {"assembly": s7.get("assembly_check"),
                 "G20": res.get("stages", {}).get("validation", {}).get("G20"),
                 "verdict": res.get("stages", {}).get("validation", {}).get("verdict")})
            rep = json.load(open(os.path.join(br, "s7-out", "_staging", KEY2, "report.json"),
                                 encoding="utf-8"))
            add("F3. 书级 report 里带 `volumes[]` + `merge{input_continuity}`",
                len(rep.get("volumes") or []) == 2
                and "input_continuity" in (rep.get("merge") or {}),
                {"volumes": len(rep.get("volumes") or []),
                 "merge_keys": sorted((rep.get("merge") or {}).keys())})
            # ---- C2-3：`input_kind` 与判据⑥ ----
            cs = rep.get("chapter_split") or {}
            add("G. `chapter_split.input_kind = book_sequence`（合并后的完整书级序列）",
                cs.get("input_kind") == "book_sequence", cs.get("input_kind"))
            add("G2. `chapter_split.input_pages` 逐页留档（9 页）",
                len(cs.get("input_pages") or []) == 9, len(cs.get("input_pages") or []))
            add("G3. G20 的 measured 里带 `input_kind`（判据⑥可见）",
                (res.get("stages", {}).get("validation", {}).get("G20") == "pass")
                and (json.load(open(os.path.join(br, "s7-out", "_staging", KEY2,
                                                 "validation-report.json"), encoding="utf-8"))
                     ["gates"]["G20"]["measured"].get("input_kind") == "book_sequence"), None)
        finally:
            shutil.rmtree(br, ignore_errors=True)

        # ---- H) **判据⑥ 真跑**：整本书却没有任何章标题 → 登记可疑 + **G20 fail**（不静默）----
        br2 = tempfile.mkdtemp(prefix="c2run2-")
        KEY3 = "fxnorun"
        pr.BOOKS[KEY3] = dict(pr.BOOKS[KEY2], key=KEY3)
        try:
            os.makedirs(os.path.join(br2, "s2-ir"), exist_ok=True)
            os.makedirs(os.path.join(br2, "ocr-raw"), exist_ok=True)
            # 只取**第 2 章内部**的片内页（7,8,9）→ 合并后的"书"里没有任何章级标题
            for vol, pages in (("v01", [7, 8]), ("v02", [9])):
                with open(os.path.join(br2, "s2-ir", f"ir.mineru.{KEY3}.{vol}.jsonl"),
                          "w", encoding="utf-8", newline="\n") as fh:
                    for p in pages:
                        for r in rows:
                            if r["page"] == p:
                                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            json.dump({"book": KEY3, "engine": "mineru", "page_count": 3,
                       "volumes": [{"id": "v01", "cut_pages": [1, 2], "book_pages": [1, 2],
                                    "sha256": "A"},
                                   {"id": "v02", "cut_pages": [3], "book_pages": [3], "sha256": "B"}]},
                      open(os.path.join(br2, "ocr-raw", f"volumes-{KEY3}.json"), "w",
                           encoding="utf-8"), ensure_ascii=False)
            res2 = pr.run_book(KEY3, br2, do_publish=False)
            rep2 = json.load(open(os.path.join(br2, "s7-out", "_staging", KEY3, "report.json"),
                                  encoding="utf-8"))
            cs2 = rep2.get("chapter_split") or {}
            add("H. **整本书却无章标题**：input_kind=book_sequence + rule=single → **必须登记可疑**（留痕）；"
                "登记后 G20 **pass**（判据⑥的语义是「不得静默」，不是「必须阻断」）",
                cs2.get("input_kind") == "book_sequence" and cs2.get("rule") == "single"
                and any("可疑" in str(s.get("why") or "") for s in (cs2.get("suspects") or []))
                and res2.get("stages", {}).get("validation", {}).get("G20") == "pass",
                {"input_kind": cs2.get("input_kind"), "rule": cs2.get("rule"),
                 "suspects": [s.get("why", "")[:36] for s in (cs2.get("suspects") or [])],
                 "G20": res2.get("stages", {}).get("validation", {}).get("G20")})
        finally:
            shutil.rmtree(br2, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = sum(1 for _n, ok, _e in cases if ok)
    for n, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + n + ("" if ok else f"   -> {extra}"))
    print(f"\n{passed}/{len(cases)} C2-2/C2-3 书级流水线回归通过")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
