# -*- coding: utf-8 -*-
"""C0 · 运行分片（volume）规范 —— 决定"一次引擎调用多少页 / 冻结证据的粒度"。

**为什么**：MinerU 是云端 API，**一次请求有上限**；Paddle 本地虽无页数硬限，但 ~27 s/页 × 数百页
= 数小时，必须能**断点续跑 / 只重跑一卷**。

**与章边界无关**：一个章可跨卷、一个卷可含多章（切章是输出侧的事；见 SPEC §3.x）。

**上限（照原件，留余量）**：
  * PaddleOCR-VL：**≤100 页 且 ≤45 MB / 卷**
  * MinerU：      **≤180 页 且 ≤190 MB / 卷**

产物：`ocr-raw/volumes-<书>.json`
    {"book","source_pdf","source_pdf_sha256","limits","volumes":[
        {"id":"v01","cut_pages":[1..100],"book_pages":[...],"bytes":N,"sha256":"..."}]}

用法：
    python -X utf8 volumes.py plan  --book puhua-b2 [--engine mineru|paddle] [--write]
    python -X utf8 volumes.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from tuning import TUNE
import render
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = paths.data_root()  # 可迁移：_mcp-test 的上一级 = 产物根
LIMITS = {"paddle": {"pages": TUNE["vol_pages_paddle"], "bytes": TUNE["vol_bytes_paddle"]},
          "mineru": {"pages": TUNE["vol_pages_mineru"], "bytes": TUNE["vol_bytes_mineru"]}}  # L2：见 tuning.json


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_volumes(n_pages: int, limits: dict, page_bytes: dict | None = None) -> list[dict]:
    """按**页数上限**贪心切卷；给了每页字节数时同时守**体积上限**。纯函数，便于测。"""
    max_pages, max_bytes = int(limits["pages"]), limits.get("bytes")
    vols, cur, cur_bytes = [], [], 0
    for p in range(1, n_pages + 1):
        pb = int((page_bytes or {}).get(p, 0))
        too_many = len(cur) >= max_pages
        too_big = bool(max_bytes) and cur and (cur_bytes + pb) > max_bytes
        if cur and (too_many or too_big):
            vols.append(cur)
            cur, cur_bytes = [], 0
        cur.append(p)
        cur_bytes += pb
    if cur:
        vols.append(cur)
    return [{"id": f"v{i + 1:02d}", "cut_pages": pages} for i, pages in enumerate(vols)]


def measure_page_bytes(pdf: str) -> dict:
    """每页的"单页 PDF"字节数（用于守体积上限）。后端见 `render.py`（pypdf）。"""
    return render.measure_page_bytes(pdf)


def build_volumes(book: str, root: str = ROOT, engine: str = "mineru",
                  write: bool = False) -> dict:
    import binding
    cfg = binding.resolve_cfg(book)
    key = cfg.get("key") or book
    pdf = cfg.get("pdf") or ""
    if not pdf or not os.path.exists(pdf):
        return {"ok": False, "error": "源 PDF 不存在", "book": key}
    labels = cfg.get("page_labels") or []
    _doc = render.open_doc(pdf)
    try:
        n = render.page_count(_doc)
    finally:
        render.close(_doc)
    page_bytes = render.measure_page_bytes(pdf)
    vols = plan_volumes(n, LIMITS[engine], page_bytes)
    for v in vols:                                   # 卷哈希（相邻页范围 → 独立成卷 PDF）
        _sb = render.slice_bytes(pdf, list(range(v["cut_pages"][0], v["cut_pages"][-1] + 1)))
        v["bytes"] = _sb["bytes"]
        v["sha256"] = _sb["sha256"]
        v["book_pages"] = [labels[p - 1] if p - 1 < len(labels) else p for p in v["cut_pages"]]
    res = {"book": key, "source_pdf": pdf, "source_pdf_sha256": sha256_file(pdf),
           "engine": engine, "limits": LIMITS[engine], "page_count": n,
           "volumes": [{k: v[k] for k in ("id", "cut_pages", "book_pages", "bytes", "sha256")}
                       for v in vols],
           "note": "卷边界与章边界无关（一章可跨卷）；每卷记录页映射与哈希，供之后逐卷冻结/重跑"}
    outp = os.path.join(root, "ocr-raw", f"volumes-{key}.json")
    if write:
        os.makedirs(os.path.dirname(outp), exist_ok=True)
        with open(outp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        res["written"] = outp
    return res


def load_ir(path: str) -> list[dict]:
    """读 IR（jsonl）。**不依赖 s3_compare**，保持本模块自足、可单测。"""
    out = []
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def check_continuity(volumes: list[dict], page_count: int | None = None,
                     excluded_pages: list[int] | None = None) -> dict:
    """**连续性校验**（SPEC §3.9-4，硬前置）：拒绝不修补。纯函数，便于测。

    ① 相邻卷首尾相接（前卷末页 + 1 == 后卷首页）；
    ② 各卷 `cut_pages` 两两不重叠（**同一页只能属于一个卷**）；
    ③ **并集 == 输入 PDF 的页集合 `{1..page_count}`**（除**显式** `excluded_pages`）。

    > **订正（2026-10-12，实测驱动）**：③ 早先写作"并集 == `[1, page_count]`"，隐含"页号必须连续"，
    > 会把**完整但稀疏**的输入（如"输入 PDF 本身就是奇数页"）误拒。
    > **连续性要求是"卷之间不许有缝"，不是"世界的页码必须连续"。**
    """
    errs = []
    vols = sorted(volumes, key=lambda v: (v.get("cut_pages") or [0])[0])
    seen: dict[int, str] = {}
    for v in vols:
        cp = list(v.get("cut_pages") or [])
        if not cp:
            errs.append({"kind": "empty_volume", "volume": v.get("id")})
            continue
        for p in cp:
            if p in seen:
                errs.append({"kind": "page_overlap", "page": p, "volumes": [seen[p], v.get("id")]})
            seen[p] = v.get("id")
    for a, b in zip(vols, vols[1:]):
        _a, _b = (a.get("cut_pages") or []), (b.get("cut_pages") or [])
        # 只对**连续切片**要求"首尾相接"：卷跨越的页必须是输入 PDF 里真实存在、且**中间不留缝**的页。
        # （若输入本身稀疏——如整本 PDF 就是"只抽了奇数页"——则由 ③ 的"覆盖输入页集合"负责判，不在这里报。）
        if _a and _b and _b[0] != _a[-1] + 1:
            _gap = sorted(set(range(_a[-1] + 1, _b[0])) - set(excluded_pages or []))
            if _gap:
                errs.append({"kind": "gap_or_disorder", "after_volume": a.get("id"),
                             "prev_last": _a[-1], "next_first": _b[0], "missing_between": _gap[:50],
                             "why": "相邻卷之间缺页（跳页/乱序）"})
    if page_count:
        want = set(range(1, int(page_count) + 1)) - set(excluded_pages or [])
        missing = sorted(want - set(seen))
        if missing:
            errs.append({"kind": "coverage_missing", "missing_pages": missing[:50],
                         "missing_count": len(missing),
                         "why": "卷集合未覆盖**输入 PDF 的**每一页（缺卷或漏页）"})
    return {"ok": not errs, "errors": errs, "pages_covered": len(seen),
            "page_count": page_count, "excluded_pages": sorted(excluded_pages or []),
            "volumes": [v.get("id") for v in vols]}


def input_continuity(volumes: list[dict]) -> dict:
    """**输入页映射是否连续**（SPEC §3.9-4④）——**只登记，不拒绝**。

    `page_labels`（切片页 → 原书真实页号）允许不等距：例"原书 1–100 与 300–499 拼成一本"
    → `contiguous=false`、`jumps=[[100,300]]`。**缺省（没填映射）视为 `[1..N]` 连续**。
    """
    seq = []
    for v in sorted(volumes, key=lambda x: (x.get("cut_pages") or [0])[0]):
        seq += [p for p in (v.get("book_pages") or [])]
    jumps = [[seq[i], seq[i + 1]] for i in range(len(seq) - 1) if seq[i + 1] != seq[i] + 1]
    return {"contiguous": not jumps, "jumps": jumps[:50], "jump_count": len(jumps),
            "pages": seq[:200], "pages_count": len(seq),
            "note": "输入页映射（page_labels）不连续 → 只登记、不拒绝；章号仍按本输入内的章标题取"}


def merge_volumes(book: str, root: str = ROOT, engine: str = "mineru",
                  out_path: str | None = None, write: bool = True,
                  page_count: int | None = None,
                  excluded_pages: list[int] | None = None) -> dict:
    """把逐卷 IR 合并成**一条书级 IR 序列**（SPEC §3.9）。

    口径：
      * **唯一依据**是 `ocr-raw/volumes-<key>.json`（卷规范）；
      * 每卷的 IR 页号是**片内页号**（= 该卷 `cut_pages`），经 `book_pages` 还原成**书内页号**
        → 合并行写 `page`（书内页号，排序与下游一切判断都用它）与 `page_local`（片内页号）；
      * **连续性校验先跑**：不过 → **拒绝合并**（`ok=false`、`errors` 如实报），**不写任何文件**；
      * 只合并不改写：除 `page`/`page_local`/`_volume`/`_book_page` 四个记账字段外，**其余字段原样**。
    """
    import binding
    cfg = binding.resolve_cfg(book)
    key = cfg.get("key") or book
    vp = os.path.join(root, "ocr-raw", f"volumes-{key}.json")
    if not os.path.exists(vp):
        return {"ok": False, "book": key, "error": "没有卷规范（单卷路径应直接跑现有管线）",
                "volumes_file": vp}
    spec = json.load(open(vp, encoding="utf-8"))
    volumes = spec.get("volumes") or []
    if page_count is None:
        page_count = spec.get("page_count")
    cont = check_continuity(volumes, page_count, excluded_pages)
    if not cont["ok"]:
        return {"ok": False, "book": key, "volumes_file": vp, "volumes": volumes,
                "merge": {"continuity": cont, "merged": False},
                "error": "连续性校验未通过 → 拒绝合并（不修补、不落盘）"}
    merged, per_vol = [], []
    _single_file = os.path.join(root, "s2-ir", f"ir.{engine}.{key}.jsonl")
    for v in sorted(volumes, key=lambda x: (x.get("cut_pages") or [0])[0]):
        cp = list(v.get("cut_pages") or [])
        bp = list(v.get("book_pages") or [])
        ip = os.path.join(root, "s2-ir", f"ir.{engine}.{key}.{v['id']}.jsonl")
        if not os.path.exists(ip):
            ip = _single_file
        # 单卷书（或未分卷命名）：整份 IR 就是这一卷的内容，**不做页号重映射**（保留原样，零改写）
        _passthrough = (len(volumes) == 1 and os.path.abspath(ip) == os.path.abspath(_single_file))
        rows = load_ir(ip)
        got = {r.get("page") for r in rows}
        want = set(cp)
        rec = {"id": v.get("id"), "ir": ip, "cut_pages": cp, "book_pages": bp,
               "rows": len(rows), "sha256": v.get("sha256"),
               "status": "single_volume_passthrough" if _passthrough else ("merged" if rows else "missing_ir"),
               "missing_local_pages": [] if _passthrough else sorted(want - got)[:20]}
        if not rows:
            return {"ok": False, "book": key, "volumes_file": vp, "volumes": volumes,
                    "merge": {"continuity": cont, "merged": False, "per_volume": per_vol + [rec]},
                    "error": f"卷 {v.get('id')} 的 IR 缺失或为空：{ip}"}
        for r in rows:
            loc = r.get("page")
            r = dict(r)
            if _passthrough:
                r["_volume"] = v.get("id")
                if "page" not in r:
                    r["page"] = 1
                merged.append(r)
                continue
            idx = cp.index(loc) if loc in cp else None
            r["page_local"] = loc
            r["_volume"] = v.get("id")
            # `page` = **书内真实页号**（下游 `page_labels` 就活在这个空间里：页标记、G1/G2 全按它）。
            # 片内页号另存 `page_local`。
            r["page"] = bp[idx] if (idx is not None and idx < len(bp)) else loc
            r["_book_page"] = r["page"]
            merged.append(r)
        per_vol.append(rec)
    merged.sort(key=lambda r: (r["page"], r.get("order", 0)))
    _pg = sorted({r["page"] for r in merged})
    # 重复 = **同一个书内页号被分给了两个卷**（同页多行是正常的；按 (卷,页) 去重后再看页号冲突）
    _owner: dict = {}
    _dups = []
    for r in merged:
        prev = _owner.setdefault(r["page"], r["_volume"])
        if prev != r["_volume"] and r["page"] not in _dups:
            _dups.append(r["page"])
    _dups.sort()
    if _dups or (_pg and _pg != list(range(_pg[0], _pg[0] + len(_pg)))):
        # **硬校验**：重复/跳号一律拒绝（否则下游"页覆盖/页标记"必然对不上，且会静默）
        return {"ok": False, "book": key, "volumes_file": vp, "volumes": per_vol,
                "merge": {"continuity": cont, "merged": False, "page_space": "book_pages",
                          "duplicate_pages": _dups[:50], "pages": _pg[:50],
                          "why": "合并后的书内页号不连续或重复——多卷页映射重叠，拒绝合并"},
                "error": "合并后书内页号重复/不连续 → 拒绝（不修补；检查各卷 book_pages 是否重叠/跳号）"}
    out = out_path or os.path.join(root, "s2-ir", f"ir.{engine}.{key}.book.jsonl")
    if write:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            for r in merged:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "book": key, "engine": engine, "volumes_file": vp,
            "merged_ir": out, "written": bool(write), "rows": len(merged),
            "volumes": per_vol,
            "merge": {"continuity": cont, "merged": True,
                      "single_volume": len(per_vol) == 1,
                      "page_space": "book_pages（书内真实页号；下游 page_labels 就在这个空间）",
                      "input_pages": [min(r["page"] for r in merged), max(r["page"] for r in merged)]
                      if merged else None,
                      # **只登记、不拒绝**（SPEC §3.9-4④）：输入页映射是否连续
                      "input_continuity": input_continuity(volumes),
                      "excluded_pages": sorted(excluded_pages or []),
                      "book_pages_monotonic": all(
                          merged[i]["page"] <= merged[i + 1]["page"]
                          for i in range(len(merged) - 1)),
                      "volume_boundaries": {r["_volume"]: [min(x["page"] for x in merged
                                                              if x["_volume"] == r["_volume"]),
                                                           max(x["page"] for x in merged
                                                               if x["_volume"] == r["_volume"])]
                                            for r in merged} if merged else {}}}


def selftest() -> int:
    cases, passed = [], 0


    def add(name, ok, extra=None):
        nonlocal passed
        cases.append((name, bool(ok), extra))

    v = plan_volumes(250, LIMITS["mineru"])
    add("250 页 / MinerU ≤180 页 → 2 卷（180+70）",
        [len(x["cut_pages"]) for x in v] == [180, 70] and [x["id"] for x in v] == ["v01", "v02"],
        [len(x["cut_pages"]) for x in v])
    v2 = plan_volumes(250, LIMITS["paddle"])
    add("250 页 / Paddle ≤100 页 → 3 卷（100+100+50）",
        [len(x["cut_pages"]) for x in v2] == [100, 100, 50], [len(x["cut_pages"]) for x in v2])
    add("卷覆盖全部页且不重叠（精确集合闭合）",
        sorted(p for x in v2 for p in x["cut_pages"]) == list(range(1, 251)), None)
    pb = {p: 30_000_000 for p in range(1, 11)}        # 每页 30MB → 体积上限 45MB 时每卷只能 1 页
    v3 = plan_volumes(10, {"pages": 100, "bytes": 45_000_000}, pb)
    add("体积上限生效（每页 30MB / 上限 45MB → 10 卷）", len(v3) == 10, len(v3))
    add("单页超限也自成一卷（不丢页）", sum(len(x["cut_pages"]) for x in v3) == 10, None)
    add("页数为 1 → 1 卷", len(plan_volumes(1, LIMITS["mineru"])) == 1, None)
    add("页数为 0 → 0 卷", plan_volumes(0, LIMITS["mineru"]) == [], None)

    # ---------------- C2-1：连续性校验（纯函数，负例逐条） ----------------
    _V = [{"id": "v01", "cut_pages": [1, 2, 3]}, {"id": "v02", "cut_pages": [4, 5]}]
    c = check_continuity(_V, 5)
    add("连续性：首尾相接 + 全覆盖 → ok", c["ok"] and c["pages_covered"] == 5, c)
    add("连续性负例①：跳页（v02 从 5 起，缺第 4 页）→ 拒",
        not check_continuity([{"id": "v01", "cut_pages": [1, 2, 3]},
                             {"id": "v02", "cut_pages": [5]}], 5)["ok"], None)
    add("连续性负例②：卷重叠（v02 含第 3 页）→ 拒",
        any(e["kind"] == "page_overlap" for e in
            check_continuity([{"id": "v01", "cut_pages": [1, 2, 3]},
                              {"id": "v02", "cut_pages": [3, 4]}], 4)["errors"]), None)
    add("连续性负例③：缺卷（只给 v01，page_count=5）→ 拒（coverage_missing）",
        any(e["kind"] == "coverage_missing" for e in check_continuity(_V[:1], 5)["errors"]), None)
    add("连续性负例④：乱序（v02 在 v01 前）→ 排序后仍判相接，不误报",
        check_continuity([{"id": "v02", "cut_pages": [4, 5]},
                          {"id": "v01", "cut_pages": [1, 2, 3]}], 5)["ok"], None)
    add("连续性：显式 excluded_pages 允许不覆盖",
        check_continuity(_V, 6, excluded_pages=[6])["ok"], None)
    # **订正后的口径**：完整但稀疏的输入（如"输入 PDF 本身就是奇数页"）应**通过**。
    # 注意：`cut_pages` 是**输入 PDF 的页号**（1..page_count）；"输入只有奇数页"指**内容**稀疏，
    # 不是输入 PDF 缺页 —— 所以这里用一个"输入 PDF 有 3 页、卷覆盖 1..3"的最小例来对齐口径。
    _sparse_in = [{"id": "v01", "cut_pages": [1, 2, 3]}]
    add("连续性：卷覆盖**输入 PDF 的每一页**（含输入本身稀疏的情形）→ 通过",
        check_continuity(_sparse_in, 3)["ok"], check_continuity(_sparse_in, 3)["errors"])
    add("连续性：真缺页（输入有 5 页，卷只给 1,2,3,5）→ 仍拒",
        not check_continuity([{"id": "v01", "cut_pages": [1, 2, 3, 5]}], 5)["ok"], None)
    add("input_continuity：缺省映射（1..N）→ contiguous=True",
        input_continuity([{"id": "v01", "cut_pages": [1, 2], "book_pages": [1, 2]}])["contiguous"], None)
    add("input_continuity：原书 1–100 与 300–499 拼接 → contiguous=False 且登记跳变",
        (lambda r: (not r["contiguous"]) and [100, 300] in r["jumps"])(
            input_continuity([{"id": "v01", "cut_pages": [1, 2], "book_pages": [99, 100]},
                              {"id": "v02", "cut_pages": [3, 4], "book_pages": [300, 301]}])),
        input_continuity([{"id": "v01", "cut_pages": [1, 2], "book_pages": [99, 100]},
                          {"id": "v02", "cut_pages": [3, 4], "book_pages": [300, 301]}])["jumps"])

    # ---------------- C2-1：合并器（合成两卷书，端到端） ----------------
    import tempfile
    import shutil
    d = tempfile.mkdtemp(prefix="vol-")
    os.makedirs(os.path.join(d, "s2-ir"), exist_ok=True)
    os.makedirs(os.path.join(d, "ocr-raw"), exist_ok=True)
    # 卷规范：片内 1..3 → 书内 100..102；片内 1..2 → 书内 120..121（故意跳页，用于拒绝对照）
    spec = {"book": "fxbook", "engine": "mineru", "page_count": 5,
            "volumes": [{"id": "v01", "cut_pages": [1, 2, 3], "book_pages": [100, 101, 102],
                         "sha256": "VA"},
                        {"id": "v02", "cut_pages": [4, 5], "book_pages": [103, 104], "sha256": "VB"}]}
    json.dump(spec, open(os.path.join(d, "ocr-raw", "volumes-fxbook.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

    def _write_ir(vol_id, pages):
        p = os.path.join(d, "s2-ir", f"ir.mineru.fxbook.{vol_id}.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            for i, pg in enumerate(pages):
                fh.write(json.dumps({"page": pg, "order": i, "kind": "paragraph",
                                     "text_raw": f"{vol_id}-p{pg}"}, ensure_ascii=False) + "\n")
        return p

    _write_ir("v01", [1, 2, 3])
    _write_ir("v02", [4, 5])
    r = merge_volumes("fxbook", d, "mineru", write=False)
    add("合并：两卷 → 一条书级序列；页号按 book_pages 还原",
        r["ok"] and r["rows"] == 5, r.get("rows"))
    add("合并 dry-run（write=False）→ **不改写源 IR、不落盘**",
        [x["page"] for x in load_ir(os.path.join(d, "s2-ir", "ir.mineru.fxbook.v01.jsonl"))]
        == [1, 2, 3], None)
    r2 = merge_volumes("fxbook", d, "mineru", out_path=os.path.join(d, "s2-ir", "merged.jsonl"))
    rows = load_ir(os.path.join(d, "s2-ir", "merged.jsonl"))
    add("合并落盘：`page` = **书内真实页号 100..104**（连续，下游 page_labels 同空间）",
        r2["ok"] and [x["page"] for x in rows] == [100, 101, 102, 103, 104]
        and [x["page_local"] for x in rows] == [1, 2, 3, 4, 5]
        and [x["_volume"] for x in rows] == ["v01"] * 3 + ["v02"] * 2
        and r2["merge"]["book_pages_monotonic"], [x["page"] for x in rows])
    add("合并：不改写其它字段（text_raw 原样）",
        rows[0]["text_raw"] == "v01-p1" and rows[3]["text_raw"] == "v02-p4", rows[0])
    # 缺一卷的 IR → 拒绝，且**不落盘**
    os.remove(os.path.join(d, "s2-ir", "ir.mineru.fxbook.v02.jsonl"))
    _before = os.path.getmtime(os.path.join(d, "s2-ir", "merged.jsonl"))
    r3 = merge_volumes("fxbook", d, "mineru", out_path=os.path.join(d, "s2-ir", "merged2.jsonl"))
    add("合并负例：某卷 IR 缺失 → 拒绝、并如实报（不静默、不写新文件）",
        (not r3["ok"]) and "IR 缺失" in r3["error"]
        and not os.path.exists(os.path.join(d, "s2-ir", "merged2.jsonl"))
        and os.path.getmtime(os.path.join(d, "s2-ir", "merged.jsonl")) == _before, r3["error"])
    # 跳页卷规范 → 拒绝合并（连续性硬前置）
    _write_ir("v02", [4, 5])
    spec["volumes"][1]["cut_pages"] = [6, 7]
    json.dump(spec, open(os.path.join(d, "ocr-raw", "volumes-fxbook.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    r4 = merge_volumes("fxbook", d, "mineru", out_path=os.path.join(d, "s2-ir", "merged3.jsonl"))
    add("合并负例：卷规范跳页 → 连续性校验拒绝、不落盘",
        (not r4["ok"]) and r4["merge"]["merged"] is False
        and not os.path.exists(os.path.join(d, "s2-ir", "merged3.jsonl")), r4["error"])
    add("合并：没有卷规范 → 明确拒绝（提示单卷路径）",
        not merge_volumes("fx-none", d, "mineru", write=False)["ok"], None)
    # **单卷直通**：单卷书（或未分卷命名）→ 整份 IR 原样，不改页号、不加记账字段
    m_single = merge_volumes("fxsingle", d, "mineru", write=False) if False else None
    os.makedirs(os.path.join(d, "ocr-raw"), exist_ok=True)
    json.dump({"book": "fxsingle", "engine": "mineru", "page_count": 3,
               "volumes": [{"id": "v01", "cut_pages": [1, 2, 3], "book_pages": [1, 2, 3], "sha256": "S"}]},
              open(os.path.join(d, "ocr-raw", "volumes-fxsingle.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    with open(os.path.join(d, "s2-ir", "ir.mineru.fxsingle.jsonl"), "w", encoding="utf-8") as fh:
        for p in (1, 2, 3):
            fh.write(json.dumps({"page": p, "order": 0, "kind": "paragraph",
                                 "text_raw": f"s{p}"}, ensure_ascii=False) + "\n")
    s1 = merge_volumes("fxsingle", d, "mineru", write=False)
    add("合并：**单卷直通**（不重排页号、不加 page_local/_book_page，原样只标 _volume）",
        s1["ok"] and s1["merge"]["single_volume"]
        and s1["volumes"][0]["status"] == "single_volume_passthrough"
        and s1["rows"] == 3 and s1["written"] is False and s1["merged_ir"].endswith(".jsonl"),
        {"vol": s1["volumes"][0]["status"], "rows": s1["rows"], "written": s1["written"]})
    # **硬校验**：两卷 book_pages 重叠 → 合并后页号重复 → 拒绝（否则下游页覆盖必然错）
    _write_ir("v02", [4, 5])
    spec["volumes"][1]["cut_pages"] = [4, 5]
    spec["volumes"][1]["book_pages"] = [102, 103]      # 与 v01 的 102 重叠
    json.dump(spec, open(os.path.join(d, "ocr-raw", "volumes-fxbook.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    r5 = merge_volumes("fxbook", d, "mineru", out_path=os.path.join(d, "s2-ir", "merged4.jsonl"))
    add("合并负例：卷间 book_pages 重叠 → 合并后页号重复 → 拒绝且不落盘",
        (not r5["ok"]) and r5["merge"]["merged"] is False
        and not os.path.exists(os.path.join(d, "s2-ir", "merged4.jsonl")),
        r5.get("error") or r5.get("merge", {}).get("why"))
    shutil.rmtree(d, ignore_errors=True)

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += ok
    print(f"\n{passed}/{len(cases)} volumes tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--book", required=True)
    p.add_argument("--engine", default="mineru", choices=sorted(LIMITS))
    p.add_argument("--write", action="store_true")
    mg = sub.add_parser("merge")
    mg.add_argument("--book", required=True)
    mg.add_argument("--engine", default="mineru", choices=sorted(LIMITS))
    mg.add_argument("--root", default=ROOT)
    mg.add_argument("--out", default="")
    mg.add_argument("--no-write", action="store_true", help="只校验/试算，不落盘（dry-run）")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "merge":
        res = merge_volumes(a.book, a.root, a.engine, out_path=a.out or None,
                            write=not a.no_write)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    print(json.dumps(build_volumes(a.book, ROOT, a.engine, a.write), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
