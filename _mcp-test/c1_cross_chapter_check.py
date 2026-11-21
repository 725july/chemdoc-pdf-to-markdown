# -*- coding: utf-8 -*-
"""C1 真跨章回归（SPEC §3.8 验证方案②）——**一次性检查脚本**，不属管线。

样本：`普通化学原理 第4版…-22-30.pdf`（9 页 = 物理页 22–30 = **印刷页 6–14**）
- 印刷页 8（切片第 3 页）是第 1 章末页（`课外读物` + 参考文献）
- 印刷页 **10（切片第 5 页）** 是第 2 章首页：`第2章 气体` 与 `2.1`/`2.1.1` **同页**
  → 切分必须支持**页内块级切分**

断言：
  A. 切出 ≥2 个章文件，且含 `chapter-02`（章号用**书里印的**）
  B. 第 1 章尾部内容（参考文献/课外读物）落在**章前或第 1 章**文件里，第 2 章标题块落在 `chapter-02`
  C. 第 2 章首页的**同页块级切分**：`第2章 气体` 是 `chapter-02` 的**首块**，而同一页上它之前的块不在该文件里
  D. 装配不变量：页标记集合相同 + 去注释后正文逐行相同（G20 判据②的两条判据）
  E. `report.chapter_split.suspects` 为空（本页不涉及冲突）；`rule == "printed"`
用法: python -X utf8 c1_cross_chapter_check.py [--root <数据根>] [--book <key>] [--staging]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录

PAGE_MARK = re.compile(r"<!--\s*p(\d{3,4})\s*-->")


def _pages(t: str) -> list[int]:
    return [int(x) for x in PAGE_MARK.findall(t)]


def _body(t: str) -> list[str]:
    return [re.sub(r"\s+", " ", ln).strip()
            for ln in re.sub(r"<!--.*?-->", "", t, flags=re.S).split("\n") if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=paths.data_root())
    ap.add_argument("--book", default="fx-sample",
                    help="默认 = 自制样书（SPEC §3.14）；真样本只在本机显式传参")
    ap.add_argument("--staging", action="store_true", help="从 s7-out/_staging 读（未发布时用）")
    a = ap.parse_args()
    _cands = [os.path.join(a.root, "s7-out", "_staging", a.book),
              os.path.join(a.root, "s7-out", a.book)]
    s7 = next((p for p in _cands if os.path.exists(os.path.join(p, "report.json"))), None)
    if s7 is None:
        # **夹具缺失 → 明示跳过**（2026-10-13）。为什么不是报错：
        #   本样本是**受版权书的切片及其衍生数据**，`release-exclude.json` 已把 `_test-inputs`/`ocr-raw`/`s0-profiles`…`s7-out`
        #   全部排除出发布集 → **新机器上必然没有它**。老代码 `next(..., _cands[-1])` 会回落到一个不存在的路径然后
        #   `json.load` 抛 `FileNotFoundError`，把整条回归链带崩（实测 exit=1）。
        #   口径：缺夹具 = **跳过**（与 `c2_volume_merge_check` / `c2_book_run_check` 一致），不是失败。
        print("SKIP：缺输入 %s" % " 或 ".join(os.path.join(p, "report.json") for p in _cands))
        return 0
    rep = json.load(open(os.path.join(s7, "report.json"), encoding="utf-8"))
    cs = rep.get("chapter_split") or {}
    chs = rep.get("chapters") or []
    cases = []

    def add(name, ok, extra=None):
        cases.append((name, bool(ok), extra))

    files = sorted(f for f in os.listdir(s7) if f.endswith("-reviewed.md"))
    bodies = {f: open(os.path.join(s7, f), encoding="utf-8").read() for f in files}
    add("A. 切出 ≥2 章，且含 chapter-02（书里印的章号）",
        len(files) >= 2 and any(f.startswith("chapter-02") for f in files), files)
    add("A2. rule == printed（章号来自标题，不是顺序号）", cs.get("rule") == "printed", cs.get("rule"))
    ch2 = next((f for f in files if f.startswith("chapter-02")), None)
    ch_front = next((f for f in files if f.startswith("chapter-00-front")), None)
    first_ch = next((f for f in files if f == "chapter-01-reviewed.md"), None)
    tail_file = ch_front or first_ch
    add("B. 第 1 章尾部痕迹（课外读物 / 参考文献 [1]）不在 chapter-02 里",
        ch2 is not None and "课外读物" not in bodies[ch2] and "[1]" not in bodies[ch2],
        None)
    # B2：尾部内容必须在 **chapter-02 之前的那些文件**里（前置篇 + 第 1 章都算；
    #     有的样本只有一个章边界 → 第 1 章的内容整个落进 `chapter-00-front`）
    _before = "".join(bodies[f] for f in files if ch2 and f < ch2)
    add("B2. 上述尾部内容出现在 **chapter-02 之前的文件**里（前置篇 / 第 1 章）",
        ("课外读物" in _before) or ("[1]" in _before),
        [f for f in files if ch2 and f < ch2])
    _ch2_head = re.sub(r"<!--.*?-->", "", bodies[ch2] if ch2 else "", flags=re.S).strip()
    _ch2_first = next((ln.strip() for ln in _ch2_head.split("\n") if ln.strip()), "")
    # **章标题从数据推导**（不写死某一本书的标题）：优先取样书规格，其次取报告里的章级边界
    _title = ""
    try:
        import fx_sample
        _sp = fx_sample.load_spec()
        if _sp["key"] == a.book:
            _title = [b["text"] for pg in _sp["pages"] for b in pg["blocks"] if b["kind"] == "chapter"][-1]
    except Exception:                                                     # noqa: BLE001
        _title = ""
    if not _title:
        _bds = [b.get("title") or "" for b in (cs.get("boundaries") or [])]
        _title = _bds[-1] if _bds else ""
    _want = ("# " + _title) if _title else ""
    add("C. 章标题是 chapter-02 的**首个正文行**（页内块级切分；页标记注释可在前）",
        bool(_want) and _ch2_first == _want, {"got": _ch2_first[:40], "want": _want[:40]})
    joined = "\n\n".join(bodies[f].rstrip("\n") for f in files) + "\n"
    body_all = "\n".join(bodies[f] for f in files)
    add("D. 页标记集合：逐章拼接 == 各页集合一致（无漏页/重复页）",
        sorted(_pages(joined)) == sorted({p for f in files for p in _pages(bodies[f])}),
        {"joined": _pages(joined)[:12]})
    ac = cs.get("assembly_check") or {}
    add("D2. S7 自证的装配不变量成立（pages_multiset_equal + body_lines_equal）",
        ac.get("pages_multiset_equal") is True and ac.get("body_lines_equal") is True, ac)
    add("E. 无冲突登记（本样本不该有层级冲突）；notes 允许非空",
        not (cs.get("suspects") or []), cs.get("suspects"))
    add("F. 覆盖：章节块数之和 == 正文块数",
        (ac.get("blocks_in_chapters") == ac.get("blocks_direct")) and ac.get("blocks_direct", 0) > 0, ac)
    passed = sum(1 for _n, ok, _e in cases if ok)
    for n, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + n + ("" if ok else f"   -> {extra}"))
    print(f"\n{passed}/{len(cases)} 跨章回归断言通过  （{a.book}）")
    print("章文件：", files)
    print("chapter_split：rule=%s suspects=%d notes=%d empty=%d boundaries=%d"
          % (cs.get("rule"), len(cs.get("suspects") or []), len(cs.get("notes") or []),
             len(cs.get("empty_chapters") or []), len(cs.get("boundaries") or [])))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
