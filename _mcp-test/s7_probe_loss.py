# -*- coding: utf-8 -*-
"""G12 复核探针：原生输出的每个 caption/footnote 字段，是否真的出现在产物里。

用法: python -X utf8 s7_probe_loss.py <native_content_list.json> <chapter.md>
用途：当 G12（物化完整性）报"missing_in_markdown"时，区分**真丢失**与**判据过严**（例如家具被正当剔除）。
"""
from __future__ import annotations

import json
import re
import sys

FIELDS = ("image_caption", "image_footnote", "chart_caption", "chart_footnote",
          "table_caption", "table_footnote")


def flat(s: str) -> str:
    return re.sub(r"[\s$\\{}]+", "", s or "")


def main() -> int:
    raw = json.load(open(sys.argv[1], encoding="utf-8"))
    md = open(sys.argv[2], encoding="utf-8").read()
    fm = flat(md)
    miss = 0
    for i, it in enumerate(raw):
        for f in FIELDS:
            vals = it.get(f)
            if not vals:
                continue
            for x in (vals if isinstance(vals, list) else [vals]):
                hit = flat(str(x)) in fm
                miss += 0 if hit else 1
                print(f"order={i:<3} type={str(it.get('type')):<12} {f:<16} "
                      f"{'OK ' if hit else 'MISS'}  {str(x)[:58]!r}")
    print(f"\n缺失 {miss} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
