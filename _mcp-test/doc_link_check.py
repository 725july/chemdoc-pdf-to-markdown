# -*- coding: utf-8 -*-
"""文档内链检查：markdown 文件里的相对链接是否都指向真实存在的文件。

用法: python -X utf8 doc_link_check.py <目录> [--quiet]

要点：**先剥掉代码块与行内代码再检查**——否则"示例路径"（如产物内的 `figures/x.png`）
会被误报成失效链接（实测踩过）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

FENCE_RE = re.compile(r"```.*?```", re.S)
TICK_RE = re.compile(r"`[^`]*`")
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def check_dir(base: str) -> dict:
    bad, total = [], 0
    for doc in sorted(os.listdir(base)):
        if not doc.endswith(".md"):
            continue
        raw = open(os.path.join(base, doc), encoding="utf-8").read()
        txt = TICK_RE.sub("", FENCE_RE.sub("", raw))
        for link in sorted(set(LINK_RE.findall(txt))):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            total += 1
            target = os.path.normpath(os.path.join(base, link.split("#")[0]))
            if not os.path.exists(target):
                bad.append({"doc": doc, "link": link})
    return {"base": os.path.abspath(base), "links_checked": total,
            "broken": bad, "broken_count": len(bad)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    res = check_dir(a.directory)
    if not a.quiet:
        print(f"检查 {res['base']}：{res['links_checked']} 条相对链接，失效 {res['broken_count']} 条")
        for b in res["broken"]:
            print(f"  ✗ {b['doc']}  ->  {b['link']}")
    return 1 if res["broken_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
