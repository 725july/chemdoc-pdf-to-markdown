# -*- coding: utf-8 -*-
"""为 C2 真跨卷回归准备：把 `-22-30` 的逐卷 IR 拆出来，并写卷规范（v01=片1–4, v02=片5–9）。

只写"新增文件"，不改任何既有产物；卷规范写到 `ocr-raw/volumes-puhua-22-30.json`（可审计）。
"""
import json
import os
import paths  # B2：工作根唯一出口（SPEC §7.10）

ROOT = paths.data_root()  # 可迁移：数据根（SPEC §7.10）
DEFAULT_BOOK = "fx-sample"  # 自制样书（SPEC §3.14）——发布集里只有它
# 自制样书的卷规范由 `fx_sample.py make` 直接生成；本脚本是给**别的书**（含本机真样本）补卷用的
KEY = os.environ.get("FX_BOOK") or DEFAULT_BOOK
SRC = os.path.join(ROOT, "s2-ir", f"ir.mineru.{KEY}.jsonl")
V1, V2 = [1, 2, 3, 4], [5, 6, 7, 8, 9]

rows = [json.loads(l) for l in open(SRC, encoding="utf-8") if l.strip()]
for vol, pages in (("v01", V1), ("v02", V2)):
    p = os.path.join(ROOT, "s2-ir", f"ir.mineru.{KEY}.{vol}.jsonl")
    if os.path.exists(p):
        print("已存在，跳过：", p)
        continue
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for pg in pages:
            for r in rows:
                if r["page"] == pg:
                    fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    print("写出逐卷 IR：", p)

vp = os.path.join(ROOT, "ocr-raw", f"volumes-{KEY}.json")
spec = {"book": KEY, "engine": "mineru", "page_count": 9,
        "source_pdf": os.path.join(ROOT, "普通化学原理 第4版 (华彤文,王颖霞,卞江,陈景祖)-22-30.pdf"),
        "limits": {"pages": 180, "bytes": 190000000},
        "volumes": [
            {"id": "v01", "cut_pages": V1, "book_pages": V1, "bytes": 0, "sha256": "regression-fixture"},
            {"id": "v02", "cut_pages": V2, "book_pages": V2, "bytes": 0, "sha256": "regression-fixture"}],
        "note": "C2 回归用：把同一连续范围（片内 1–9）切成两卷，章边界正好落在卷界（片内第 5 页 = 第2章首页）"}
with open(vp, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(spec, fh, ensure_ascii=False, indent=2)
print("写出卷规范：", vp)
