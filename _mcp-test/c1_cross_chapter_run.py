# -*- coding: utf-8 -*-
r"""C1 真跨章回归的**驱动脚本**（一次性，不属管线；SPEC §3.8 验证方案②）。

样本：`普通化学原理 第4版…-22-30.pdf`（9 页 = 物理页 22–30 = 印刷页 6–14），
       印刷页 10（切片第 5 页）= 第 2 章首页，且与 `2.1`/`2.1.1` **同页** → 必须支持页内块级切分。

做法：**复用冻结的引擎原生产物**（不重跑 MinerU 云端）
  - MinerU：旧 run 的 `content_list.json` + `layout.json`（已复制到 `ocr-raw/mineru-puhua-22-30/`）
  - Paddle：本轮本地重跑（`ocr-raw\paddle-puhua-22-30\`，确定性、可复现）
然后走 `pipeline_run.run_pipeline(..., do_publish=False)`：S2→S3→S4→S6→S7→S8→校验，
**只写暂存区**（正式目录不动；本样本不发布）。

用法: python -X utf8 c1_cross_chapter_run.py [--root <仓库根>]
"""
from __future__ import annotations

import argparse
import os
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录

DEFAULT_BOOK = "fx-sample"          # 自制样书（SPEC §3.14）——发布集里**只有**它；真样本只在本机显式传参


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=paths.data_root())
    ap.add_argument("--book", default=DEFAULT_BOOK,
                    help="要跑的批次（默认 = 自制样书；真样本请显式传 --book <key>，它不随发布集分发）")
    a = ap.parse_args()
    import books as bk
    import pipeline_run as pr

    BOOK = a.book
    reg = bk.load(root=a.root)
    if BOOK not in reg:
        print("SKIP：注册表里没有这本书（%s）→ 先跑 fx_sample.py make 造样书" % BOOK)
        return 0
    # 路径**从注册表解析**（注册表是数据的唯一地图；代码里不写死任何一本书的路径）
    pr.BOOKS[BOOK] = dict(reg[BOOK])
    for k in ("pdf", "mineru_dir", "paddle_dir", "profile"):
        p = pr.BOOKS[BOOK][k]
        print(f"[precheck] {k}: {p} exists={os.path.exists(p)}")
        if not os.path.exists(p):
            print("SKIP：缺输入 %s（引擎原生产物 / 画像不在本机）" % p)
            return 0
    res = pr.run_pipeline(BOOK, out_root=a.root, do_publish=False)
    import json
    print(json.dumps({k: v for k, v in res["stages"].items() if k != "S3"},
                     ensure_ascii=False, indent=1)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
