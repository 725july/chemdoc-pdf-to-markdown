# -*- coding: utf-8 -*-
"""golden transcript ⇄ 产物 的逐页比对（§4.6 误差率测量的工具）。

做法：
  1. golden 文件取「正文」+「页底注」两节（丢弃元数据表与"存疑与核实记录"）；`[图:…]` 行标注的是图，不参与文字比对。
  2. 产物按 `<!-- pNNN -->` 标记**按字符串切分**（页标记可能是行内的——跨页续句接成同段时），
     这样每段文字的页码归属仍然精确。
  3. 两边都做"去排版"归一（LaTeX 上标/下标/命令、Unicode 上下标、标点、空白），
     再按句子（。！？；）双向查找"golden 有、产物无"（**漏**）与"产物有、golden 无"（**多/错**）。

用法:
  python -X utf8 golden_diff.py run --book <注册表里的条目名>        # 书单来自 books.json（不写死）
  python -X utf8 golden_diff.py run --book <条目名> --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import textnorm as TN  # noqa: E402

ROOT = paths.data_root()  # 可迁移：_mcp-test 的上一级 = 产物根
SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7",
       "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+", "₀": "0", "₁": "1", "₂": "2", "₃": "3",
       "₄": "4", "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9", "α": "alpha", "ρ": "rho",
       "δ": "delta", "°": "deg", "×": "x", "≈": "~", "⟶": "->", "→": "->", "←": "<-"}
SENT_SPLIT = re.compile(r"(?<=[。！？；])")


def latex_plain(s: str) -> str:
    s = re.sub(r"\\(?:mathrm|text|mathdefault|quad|,|;|!| )", "", s)
    s = re.sub(r"\\(?:frac|dfrac)\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", s)
    s = re.sub(r"\\(?:times|cdot|approx|circ|to|rightarrow|longrightarrow|pm|Omega|alpha|rho|delta)", " ", s)
    s = re.sub(r"\^\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"_\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\^(\w)", r"\1", s)
    s = re.sub(r"_(\w)", r"\1", s)
    s = s.replace("$", " ").replace("{", "").replace("}", "").replace("\\", "")
    for k, v in SUP.items():
        s = s.replace(k, v)
    return s


def norm(s: str) -> str:
    return TN.norm_nospace(latex_plain(s))


def golden_body(path: str) -> str:
    keep, out = False, []
    for line in open(path, encoding="utf-8"):
        if line.startswith("## "):
            keep = ("正文" in line) or ("页底注" in line)
            continue
        if not keep:
            continue
        t = line.rstrip("\n")
        if t.strip().startswith("[图:") or t.strip().startswith("|"):
            continue
        t = re.sub(r"←.*$", "", t)                      # 我方批注
        out.append(t)
    return "\n".join(out)


def artifact_pages(md_path: str) -> dict:
    md = open(md_path, encoding="utf-8").read()
    # **先按页标记切分**（页标记可能是行内的：跨页续句接成同段时），**再**清理每页里的注释/图片
    parts = re.split(r"<!--\s*p(\d{3,4})\s*-->", md)
    pages = {}
    if len(parts) >= 3:
        pages[1] = parts[0]
        for i in range(1, len(parts), 2):
            pages[int(parts[i])] = parts[i + 1] if i + 1 < len(parts) else ""
    for k in list(pages):
        t = re.sub(r"<!--.*?-->", " ", pages[k], flags=re.S)
        t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
        pages[k] = t
    return pages


def _reviewed_mds(art_dir: str) -> list[str]:
    """本书**全部**留底版章文件，按章号排序（C1：一本书可能多章；只取 `[0]` 会静默只比第 1 章）。"""
    if not os.path.isdir(art_dir):
        return []

    def _k(f: str) -> tuple:
        m = re.match(r"chapter-(\d+)", f)
        return (0 if "-front" in f else 1, int(m.group(1)) if m else 10 ** 6, f)

    return sorted((f for f in os.listdir(art_dir)
                   if f.startswith("chapter-") and f.endswith(".md") and not f.endswith("-clean.md")),
                  key=_k)


def cmp_book(book: str, keys: dict) -> dict:
    key, gold_dir, labels = keys["key"], keys["golden"], keys["labels"]
    art_dir = os.path.join(ROOT, "s7-out", key)
    pages = {}
    for _f in _reviewed_mds(art_dir):        # 逐章合并（章内页号互不重叠）
        pages.update(artifact_pages(os.path.join(art_dir, _f)))
    result = {"book": book, "key": key, "pages": []}
    for i, label in enumerate(labels, start=1):
        gp = os.path.join(ROOT, "golden", f"{gold_dir}-p{label:04d}.md")
        if not os.path.exists(gp):
            continue
        g = golden_body(gp)
        a = pages.get(i, "")
        gn, an = norm(g), norm(a)
        miss, extra = [], []
        for sent in SENT_SPLIT.split(g):
            s = norm(sent)
            if len(s) >= 12 and s not in an:
                miss.append(sent.strip()[:110])
        for sent in SENT_SPLIT.split(a):
            s = norm(re.sub(r"[*`>#]", "", sent))
            if len(s) >= 12 and s not in gn:
                extra.append(re.sub(r"\s+", " ", sent.strip())[:110])
        result["pages"].append({"pdf_page": i, "book_page": label,
                                "golden_chars": len(gn), "artifact_chars": len(an),
                                "golden_not_in_artifact": miss, "artifact_not_in_golden": extra})
    return result


def books_map(root: str = "") -> dict:
    """`书名 → {key, golden, labels}` 的映射 —— **从注册表推导**（不写死）。

    `golden` 取该条目 `source_pdf` 的词干（没有就取条目名的词干）；`labels` 取 `page_labels`。
    没有注册表（新机器）→ 返回空 dict（调用方明示跳过）。
    """
    import books as _b
    try:
        bks = _b.load(root=root) if root else _b.load()
    except Exception:                                            # noqa: BLE001
        return {}
    out = {}
    for name, cfg in bks.items():
        src = cfg.get("source_pdf") or ""
        stem = os.path.splitext(os.path.basename(src))[0] if src else name.split("-")[0]
        out[name] = {"key": cfg.get("key"), "golden": stem, "labels": list(cfg.get("page_labels") or [])}
    return out


def _selftest_artifact(root: str) -> int:
    """C1 回归：**多章**产物必须逐章合并后再比（旧实现只取 `[0]` → 第 2 章的页全成"漏"）。"""
    import shutil
    import tempfile
    art = os.path.join(root, "s7-out")
    d = tempfile.mkdtemp(prefix="gdiff-")   # 用系统临时目录：建在产物根里会踩沙箱权限（实测挂住）
    os.makedirs(os.path.join(d, "s7-out", "fx"), exist_ok=True)
    mk = lambda n, body: open(os.path.join(d, "s7-out", "fx", n), "w", encoding="utf-8",
                              newline="\n").write(body)
    mk("chapter-01-reviewed.md", "# 第1章\n\n<!-- p001 -->\n\n机器视觉与化学测量的关系。\n")
    mk("chapter-02-reviewed.md", "# 第2章\n\n<!-- p002 -->\n\n理想气体状态方程的形式。\n")
    mk("chapter-10-reviewed.md", "# 第10章\n\n<!-- p003 -->\n\n高分子化学的分支。\n")
    cases, passed = [], 0
    files = _reviewed_mds(os.path.join(d, "s7-out", "fx"))
    cases.append(("多章：三个章文件都被列出且按章号数值序（10 在 2 之后）",
                  files == ["chapter-01-reviewed.md", "chapter-02-reviewed.md",
                            "chapter-10-reviewed.md"], files))
    pages = {}
    for f in files:
        pages.update(artifact_pages(os.path.join(d, "s7-out", "fx", f)))
    cases.append(("多章：逐章合并后页 1/2/3 都在（旧实现只有页 1）",
                  set(pages) == {1, 2, 3}, sorted(pages)))
    cases.append(("合并后的正文内容取自各自那一章",
                  "理想气体状态方程" in pages[2] and "高分子化学" in pages[3], None))
    shutil.rmtree(d, ignore_errors=True)
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} golden_diff tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--book", required=True); r.add_argument("--json", default="")
    st = sub.add_parser("selftest"); st.set_defaults(_selftest=True)
    a = ap.parse_args()
    if getattr(a, "_selftest", False):
        return _selftest_artifact(ROOT)
    _bm = books_map()
    if a.book not in _bm:
        print("注册表里没有这本书：%s（现有：%s）——**书单从注册表来**，不写死在代码里"
              % (a.book, ", ".join(sorted(_bm)) or "（空）"))
        return 1
    res = cmp_book(a.book, _bm[a.book])
    for p in res["pages"]:
        print("=" * 96)
        print("切片第 %d 页（书内第 %d 页）| golden %d 字 / 产物 %d 字" %
              (p["pdf_page"], p["book_page"], p["golden_chars"], p["artifact_chars"]))
        if p["golden_not_in_artifact"]:
            print("  ▼ 誊录有、产物里**找不到**（疑漏）:")
            for s in p["golden_not_in_artifact"][:10]:
                print("     -", s)
        if p["artifact_not_in_golden"]:
            print("  ▲ 产物有、誊录里**找不到**（疑多/疑错）:")
            for s in p["artifact_not_in_golden"][:10]:
                print("     +", s)
        if not p["golden_not_in_artifact"] and not p["artifact_not_in_golden"]:
            print("  ✓ 双向一致（无内容级差异）")
    if a.json:
        json.dump(res, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("\n已写", a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
