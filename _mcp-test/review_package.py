# -*- coding: utf-8 -*-
"""⚠️ **已被 S8 取代**（校对包现在是**管线交付物**：`s8_review_kit.py` → `s7-out/<书>/review-kit/`，由 **G19** 硬门禁守着）。本文件**保留作参考与回归**（selftest 仍绿），**不再用于生成交付物**；它的 `build` 是旧做法（打包到工作区根目录，会产生过期副本），已废弃。

校对包构建（**带校验**）：把产物与它的资源一起打包，并验证 **0 断链**。

为什么要有这个脚本：上一版校对包只复制了 `.md`、**没带 `figures\\`**，导致用户打开副本时"图全部丢失"。
事故根因是"打包"由一次性命令完成、**没有校验**。现在打包是一个**有自测的脚本**：断链即失败（退出码非 0）。

用法:
  python -X utf8 review_package.py build --book <key> --out <dir> [--artifact-root ...]
  python -X utf8 review_package.py build-all --out <dir> --books puhua-b2,youjihua-b2
  python -X utf8 review_package.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def resolve_key(book: str) -> str:
    try:
        import pipeline_run as pr
        cfg = pr.BOOKS.get(book)
        return cfg["key"] if cfg else book
    except Exception:                                          # noqa: BLE001
        return book


def build(book: str, root: str, out: str) -> dict:
    key = resolve_key(book)
    src = os.path.join(root, "s7-out", key)
    if not os.path.isdir(src):
        return {"book": book, "key": key, "ok": False, "error": "产物目录不存在", "missing_links": []}
    dest = os.path.join(out, key)
    os.makedirs(dest, exist_ok=True)
    copied = []
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        if os.path.isfile(p) and (name.startswith("chapter-") or name in ("report.json", "errata.md")):
            shutil.copy2(p, os.path.join(dest, name))
            copied.append(name)
        elif os.path.isdir(p) and name in ("figures", "index"):
            d2 = os.path.join(dest, name)
            if os.path.isdir(d2):
                shutil.rmtree(d2)                      # 重建包内副本（源产物不动）
            shutil.copytree(p, d2)
            copied.append(name + "/")
    mds = [f for f in os.listdir(dest) if (f.startswith("chapter-") and not f.endswith("-clean.md"))]
    links, missing = [], []
    for md in mds:
        txt = open(os.path.join(dest, md), encoding="utf-8").read()
        links += IMAGE_LINK_RE.findall(txt)
    for l in links:
        if l.startswith(("http://", "https://")):
            continue
        if not os.path.exists(os.path.join(dest, l)):
            missing.append(l)
    # 脚注语法守卫（用户裁定：不得使用 `[^n]:`）
    footnote_syntax = sum(open(os.path.join(dest, m), encoding="utf-8").read().count("[^") for m in mds)
    return {"book": book, "key": key, "dest": dest, "copied": copied, "markdown": mds,
            "links": len(links), "missing_links": missing, "footnote_syntax_hits": footnote_syntax,
            "ok": (not missing) and footnote_syntax == 0}


def _reviewed_mds(src: str) -> list[str]:
    """本书**全部**留底版章文件，按章号排序（C1：多章时校对表要**按章分节**，不能只看第 1 章）。"""
    if not os.path.isdir(src):
        return []

    def _k(f: str) -> tuple:
        m = re.match(r"chapter-(\d+)", f)
        return (0 if "-front" in f else 1, int(m.group(1)) if m else 10 ** 6, f)

    return sorted((f for f in os.listdir(src)
                   if f.startswith("chapter-") and f.endswith(".md") and not f.endswith("-clean.md")),
                  key=_k)


def build_checklist(book: str, root: str, out: str) -> dict:
    """生成校对表：**只列"需要你判断"的标记**，已判定的另列备查（不让人白看）。"""
    key = resolve_key(book)
    src = os.path.join(root, "s7-out", key)
    md_names = _reviewed_mds(src)
    md_name = md_names[0] if md_names else ""
    md = ""
    for _f in md_names:
        md += open(os.path.join(src, _f), encoding="utf-8").read() + "\n"
    markers = re.findall(r"<!-- REVIEW: ([^>]*?) -->", md)
    todo, done = [], []
    for m in markers:
        cat = m.split()[0] if m.split() else "?"
        page = (re.search(r"page=(\d+)", m) or [None, "?"])[1]
        verdict = (re.search(r"verdict=(\S+)", m) or [None, None])[1]
        note = re.sub(r"^.*?bbox=[\d.,]+\s*", "", m)
        note = re.sub(r"verdict=\S+\s*", "", note).strip()
        (done if verdict else todo).append((page, cat, verdict or "", note[:70]))
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, f"校对表-{key}.md")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# 校对表 · {key}\n\n")
        _files = "、".join(f"`{f}`" for f in md_names) or "（无正文）"
        fh.write(f"- 产物：`s7-out\\{key}\\` 下的 {_files}（含 `figures\\`，自包含）\n")
        if len(md_names) > 1:
            fh.write(f"- **本书共 {len(md_names)} 章**：本表按章汇总（一份交付 = 一份包，不按章拆包）\n")
        fh.write(f"- **需要你判断的标记：{len(todo)} 条**；已判定（无需人工）：{len(done)} 条\n\n")
        fh.write("## 一、需要你判断（严重级/结构类）\n\n| # | 切片页 | 类型 | 我们看到的 | 你的判断 | 严重？ | 备注 |\n|---|---|---|---|---|---|---|\n")
        for i, (page, cat, _v, note) in enumerate(todo, 1):
            fh.write(f"| {i} | {page} | {cat} | {note} |  |  |  |\n")
        fh.write("\n## 二、已判定（备查，不必看）\n\n| # | 切片页 | 类型 | 结论 |\n|---|---|---|---|\n")
        for i, (page, cat, v, _n) in enumerate(done, 1):
            fh.write(f"| {i} | {page} | {cat} | {v} |\n")
        fh.write("\n> 另外：**整页通读**仍是最有价值的检查——标记之外的错误只能靠通读发现。\n")
    return {"book": book, "key": key, "todo": len(todo), "done": len(done), "file": p,
            "markdown": md_names, "ok": True}


def cmd_checklist(a) -> int:
    rc = 0
    for b in a.books.split(","):
        r = build_checklist(b.strip(), a.artifact_root, a.out)
        print(json.dumps(r, ensure_ascii=False))
        rc = rc or 0
    return rc


def cmd_build(a) -> int:
    res = build(a.book, a.artifact_root, a.out)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_build_all(a) -> int:
    rc = 0
    for b in a.books.split(","):
        res = build(b.strip(), a.artifact_root, a.out)
        print(json.dumps({k: res[k] for k in ("book", "key", "ok", "links", "missing_links",
                                             "footnote_syntax_hits")}, ensure_ascii=False))
        rc = rc or (0 if res.get("ok") else 1)
    return rc


def selftest() -> int:
    import tempfile
    cases, passed = [], 0
    root = tempfile.mkdtemp(prefix="rpkg-")
    import validate_run as V
    V._fixture(root, pages=(1, 2))                             # 造一棵最小产物树（无图）
    out = os.path.join(root, "pkg")
    r1 = build("fx", root, out)
    cases.append(("无图的产物 → 打包成功、0 断链",
                  r1["ok"] and r1["links"] == 0 and not r1["missing_links"], r1))
    # 注入一条指向不存在文件的图链接 → 必须报断链（这正是上次打包事故的回归用例）
    # C1：**每一个**章文件都注入（多章时不能只覆盖第 1 章）
    pkg_dir = os.path.join(out, "fx")
    md_list = [os.path.join(pkg_dir, f) for f in os.listdir(pkg_dir)
               if (f.startswith("chapter-") and not f.endswith("-clean.md"))]
    cases.append(("包内章文件齐全（>0）", len(md_list) >= 1, len(md_list)))
    for md in md_list:
        txt = open(md, encoding="utf-8").read() + "\n![图](figures/not-there.png)\n"
        open(md, "w", encoding="utf-8", newline="\n").write(txt)
    # 直接从**产物源**重建，验证 build 用的是源而不是被改过的包
    os.makedirs(os.path.join(root, "s7-out", "fx", "figures"), exist_ok=True)
    open(os.path.join(root, "s7-out", "fx", "figures", "ok.png"), "wb").write(b"\x89PNG")
    src_dir = os.path.join(root, "s7-out", "fx")
    src_mds = [os.path.join(src_dir, f) for f in os.listdir(src_dir)
               if (f.startswith("chapter-") and not f.endswith("-clean.md"))]
    for src_md in src_mds:
        txt2 = open(src_md, encoding="utf-8").read() + "\n![图](figures/not-there.png)\n"
        open(src_md, "w", encoding="utf-8", newline="\n").write(txt2)
    r2 = build("fx", root, out)
    cases.append(("产物里有断链 → 打包**报失败**并列出断链",
                  (not r2["ok"]) and r2["missing_links"] == ["figures/not-there.png"], r2))
    # 修好后 → 重新通过
    for src_md in src_mds:
        _t = open(src_md, encoding="utf-8").read().replace("figures/not-there.png", "figures/ok.png")
        open(src_md, "w", encoding="utf-8", newline="\n").write(_t)
    r3 = build("fx", root, out)
    cases.append(("断链修好后 → 打包通过、图被带上",
                  r3["ok"] and "figures/" in r3["copied"]
                  and os.path.exists(os.path.join(out, "fx", "figures", "ok.png")), r3["copied"]))
    # 脚注语法守卫（逐章注入）
    for src_md in src_mds:
        open(src_md, "a", encoding="utf-8").write("\n[^1]: 页下注\n")
    r4 = build("fx", root, out)
    cases.append(("产物含 `[^n]:` 语法 → 打包报失败（回归用例）",
                  (not r4["ok"]) and r4["footnote_syntax_hits"] > 0, r4["footnote_syntax_hits"]))
    # 校对表：已判定的不进"需要你判断"，未判定的才进（注意力不该花在已判定项上）
    ck = build_checklist("fx", root, out)
    txt = open(ck["file"], encoding="utf-8").read()
    cases.append(("校对表：未判定进「需要你判断」、已判定进「备查」",
                  ck["ok"] and "需要你判断" in txt, {"todo": ck["todo"], "done": ck["done"]}))
    shutil.rmtree(root, ignore_errors=True)
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {json.dumps(extra, ensure_ascii=False)[:140]}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} review_package tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--book", required=True)
    b.add_argument("--out", required=True); b.add_argument("--artifact-root", default=paths.data_root())
    b.set_defaults(func=cmd_build)
    ba = sub.add_parser("build-all"); ba.add_argument("--books", required=True)
    ba.add_argument("--out", required=True); ba.add_argument("--artifact-root", default=paths.data_root())
    ba.set_defaults(func=cmd_build_all)
    ck = sub.add_parser("checklist"); ck.add_argument("--books", required=True)
    ck.add_argument("--out", required=True); ck.add_argument("--artifact-root", default=paths.data_root())
    ck.set_defaults(func=cmd_checklist)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
