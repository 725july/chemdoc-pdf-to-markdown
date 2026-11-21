# -*- coding: utf-8 -*-
"""C2-1 回归：**真样本分卷**（同一连续范围切成两卷）——逐卷切 vs 合并后切。

样本：`ir.mineru.puhua-22-30.jsonl`（**片内页 1–9** = 物理页 22–30 = 印刷页 6–14）
  * 第 2 章标题 `第2章 气体` 在**片内第 5 页**（= 书内页 26）
  * 拆卷：**v01 = 片 1–4**、**v02 = 片 5–9**  → 章边界**正好落在卷界**（v02 首页即章首页）
  * `book_pages = 片内页 + 21`（片 1 = 书内页 22）

断言：
  A. **逐卷分别切**（错法）：v01 与 v02 **都**只看到自己范围 → v02 也出 `chapter-01`（**这就是那个洞**）
  B. **合并后切**（正法）：`volumes.merge_volumes` 还原书内页号 → `chapters.split_chapters`
     切出 `chapter-00-front`(书内22–25) + **`chapter-02`**(书内26起)；**绝不出现第二个 `chapter-01`**
  C. 合并的连续性校验：首尾相接 / 不重叠 / 全覆盖；`input_pages` = [22, 30]；页号单调
  D. 合并只做记账：新增字段仅 `page`/`page_local`/`_volume`/`_book_page`

用法: python -X utf8 c2_volume_merge_check.py [--root <数据根>]
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
import chapters as C          # noqa: E402
import books as books_mod     # noqa: E402
import volumes as V           # noqa: E402

DEFAULT_BOOK = "fx-sample"    # 自制样书（SPEC §3.14）：发布集里只有它；真样本只在本机显式传 --book
FXLIT = "fxsplit"


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
        print("SKIP：缺输入", src_ir)
        return 0
    rows = [json.loads(l) for l in open(src_ir, encoding="utf-8") if l.strip()]
    tree_src = json.load(open(src_st, encoding="utf-8"))["heading_tree"]
    root = tempfile.mkdtemp(prefix="c2check-")
    cases = []

    def add(name, ok, extra=None):
        cases.append((name, bool(ok), extra))

    try:
        os.makedirs(os.path.join(root, "s2-ir"), exist_ok=True)
        os.makedirs(os.path.join(root, "ocr-raw"), exist_ok=True)
        v1p, v2p = [1, 2, 3, 4], [5, 6, 7, 8, 9]           # 片内页号

        def write_vol(vol, pages):
            """卷文件里的页号 = **该卷 `cut_pages` 里的页号**（v01 写 1–4、v02 写 5–9）。

            这与真实分卷产物一致：卷的 IR 是按该卷 PDF 跑的，页号落在该卷的 `cut_pages` 空间里；
            合并时靠 `cut_pages.index(loc)` 找到它在卷内的位置，再用 `book_pages` 还原书内页号。
            """
            p = os.path.join(root, "s2-ir", f"ir.mineru.{FXLIT}.{vol}.jsonl")
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                for phys in pages:
                    for r in rows:
                        if r["page"] == phys:
                            fh.write(json.dumps(dict(r, page=phys),
                                                ensure_ascii=False, sort_keys=True) + "\n")
            return p

        write_vol("v01", v1p)
        write_vol("v02", v2p)
        # 卷规范的 `book_pages` 用**片内序本身**（本回归自造一本 9 页的"书"，保证合并后页号连续；
        # 真实书的书内页号连续，而"跳跃抽样"必须先重排成连续序列才能合并 —— 这正是 §3.9-4 的要求）
        spec = {"book": FXLIT, "engine": "mineru", "page_count": 9,
                "volumes": [{"id": "v01", "cut_pages": v1p, "book_pages": [1, 2, 3, 4], "sha256": "V1"},
                            {"id": "v02", "cut_pages": v2p, "book_pages": [5, 6, 7, 8, 9], "sha256": "V2"}]}
        json.dump(spec, open(os.path.join(root, "ocr-raw", f"volumes-{FXLIT}.json"), "w",
                             encoding="utf-8"), ensure_ascii=False)

        # ---- A) 逐卷分别切（错法）：**构造"卷首落在章中间"** ----
        #     取**第 2 章内部**的一段（片 7–9：2.1.2 / 2.2 / 2.2.1…），把页号重排为 1..3。
        #     这一段里没有任何 `level==1` 的章标题 → 逐卷切必然回退顺序号 `chapter-01`（**这就是那个洞**）。
        _mid_pages = [7, 8, 9]
        _mid = []
        for nl, phys in enumerate(_mid_pages, start=1):
            _mid += [dict(r, page=nl) for r in rows if r["page"] == phys]
        _mid_tree = [dict(h, page=_mid_pages.index(h["page"]) + 1)
                     for h in tree_src if h["page"] in _mid_pages]
        _res_mid = C.split_chapters({"heading_tree": _mid_tree}, _mid)
        add("A. 卷首落在**章中间**（取第2章内部一段，无章标题）→ 出 `chapter-01`（错标且无提示）",
            [c["file_stem"] for c in _res_mid["chapters"]] == ["chapter-01"],
            [(c["file_stem"], c["pages"]) for c in _res_mid["chapters"]])
        # 对照：这一段一旦放回**连续序列**（v01 + v02 合并，片 1–9），章号立刻正确
        add("A2. 对照：同样的内容放回连续序列 → 章号正确（`chapter-02`）",
            [c["file_stem"] for c in C.split_chapters({"heading_tree": tree_src}, rows)["chapters"]][-1]
            == "chapter-02", None)

        # ---- B/C/D) 合并后切（正法）----
        m = V.merge_volumes(FXLIT, root, "mineru")
        add("C. 合并：连续性校验通过（首尾相接/不重叠/全覆盖）",
            m["ok"] and m["merge"]["continuity"]["ok"]
            and m["merge"]["continuity"]["pages_covered"] == 9,
            m.get("merge", {}).get("continuity"))
        merged = V.load_ir(m["merged_ir"])
        add("C2. 合并：`page` = 书内页号 **1..9 连续**（下游 page_labels 就在这个空间）",
            m["merge"]["input_pages"] == [1, 9]
            and sorted({r["page"] for r in merged}) == list(range(1, 10))
            and m["merge"]["book_pages_monotonic"], m["merge"].get("input_pages"))
        # 判据（最直接、与回配无关）：把合并结果里每行的 3 个记账字段删掉后逐字符序列化，
        # 必须与**源文件里的某一行**逐字符相同 —— 即"除记账字段外一个字节都没动"。
        _REC = {"page_local", "_volume", "_book_page"}
        _src_lines = set()
        for _v in (m.get("volumes") or []):
            if _v.get("ir") and os.path.exists(_v["ir"]):
                _src_lines |= {json.dumps({k: v for k, v in json.loads(l).items() if k not in _REC},
                                          ensure_ascii=False, sort_keys=True)
                               for l in open(_v["ir"], encoding="utf-8") if l.strip()}
        _out_lines = [json.dumps({k: v for k, v in r.items() if k not in _REC},
                                 ensure_ascii=False, sort_keys=True) for r in merged]
        _unknown = [x[:60] for x in _out_lines if x not in _src_lines]
        add("D. 合并只做记账：删除 3 个记账字段后，每行与源文件某行**逐字符相同**（未改任何其它字段）",
            bool(merged) and not _unknown and len(_out_lines) == len(merged),
            {"rows": len(merged), "src_lines": len(_src_lines), "unknown_first": _unknown[:3]})
        add("D2. 合并行数 = 两卷行数之和（不丢不重）", len(merged) == len(rows),
            {"merged": len(merged), "src": len(rows)})
        add("D3. 合并后 `page` 连续且与 `page_local` 的映射可回溯（v02 覆盖全局 5..9）",
            [r["page"] for r in merged if r["_volume"] == "v02"][:1] and
            sorted({r["page"] for r in merged if r["_volume"] == "v02"}) == [5, 6, 7, 8, 9],
            sorted({r["page"] for r in merged if r["_volume"] == "v02"}))

        tree_all = list(tree_src)                                          # 页号 = 书内页号（此处 = 片内页）
        res = C.split_chapters({"heading_tree": tree_all}, merged)
        stems = [c["file_stem"] for c in res["chapters"]]
        add("B. 合并后切 == **不拆卷对照**，且**无重复章号**、含 `chapter-02`"
            "（章数随书而定；只有单章边界的老样本才是 front+02）",
            stems == [c["file_stem"] for c in C.split_chapters({"heading_tree": tree_src}, rows)["chapters"]]
            and len(stems) == len(set(stems)) and any(s.startswith("chapter-02") for s in stems),
            [(c["file_stem"], c["pages"]) for c in res["chapters"]])
        ch2 = next((c for c in res["chapters"] if c["file_stem"] == "chapter-02"), None)
        add("B2. 合并后：第 2 章从**第 5 页**起（章首页即 v02 首页）",
            ch2 is not None and min(ch2["pages"]) == 5, ch2 and ch2["pages"])
        add("B3. 合并后：章号取**书里印的** 02（rule=printed）", res["rule"] == "printed", res["rule"])
        add("B4. 合并后：覆盖不变量成立（每块恰好属于一章）",
            res["covered_blocks"] == res["content_blocks"] and res["covered_blocks"] > 0,
            {"covered": res["covered_blocks"], "content": res["content_blocks"]})
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = sum(1 for _n, ok, _e in cases if ok)
    for n, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + n + ("" if ok else f"   -> {extra}"))
    print(f"\n{passed}/{len(cases)} C2-1 分卷合并回归通过")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
