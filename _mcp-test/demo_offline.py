# -*- coding: utf-8 -*-
"""**离线 demo**（B3）：不填密钥、不装 OCR、不用 GPU，跑完一本书的 22 道门禁 + 交付说明。

它跑的是**自制样书** `fx-sample`（`fx_sample.py` 造：9 页，内容全自造）——
所以任何机器上都能复现；产出的交付物与真书完全同构（净版/留底版/校对包/交付说明/校验报告）。

用法：
  python -X utf8 demo_offline.py run       [--root <数据根>]   # 跑一遍（缺样书会自动造）
  python -X utf8 demo_offline.py selftest                       # 自测（纯函数 + 负例）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import books as books_mod  # noqa: E402
import paths  # noqa: E402

BOOK = "fx-sample"          # 自制样书（唯一载体；SPEC §3.14）


def summarize(rep: dict, book: str, root: str) -> dict:
    """把校验报告变成**人话摘要**（纯函数，便于自测）。

    只读报告的公开字段：`verdict` / `counts` / `hard_gates_failed` / `invariants` / `delivery_note`，
    再加上"交付目录里到底有哪些文件"。
    """
    out_dir = os.path.join(root, "s7-out", book)
    files = []
    if os.path.isdir(out_dir):
        for f in sorted(os.listdir(out_dir)):
            p = os.path.join(out_dir, f)
            if os.path.isfile(p):
                files.append({"name": f, "bytes": os.path.getsize(p)})
    note = (rep.get("delivery_note") or {}).get("path") or ""
    verdict = (rep.get("verdict") or "").strip()
    counts = rep.get("counts") or {}
    hard = rep.get("hard_gates_failed") or []
    inv = rep.get("invariants") or {}
    checklist = [
        f"判决：{verdict or '（缺）'}",
        "门禁：pass %s / warn %s / fail %s / unverified %s / n/a %s"
        % (counts.get("pass"), counts.get("warn"), counts.get("fail"),
           counts.get("unverified"), counts.get("n/a")),
        "硬失败：%s" % ("无" if not hard else "、".join(str(h) for h in hard)),
        "不变量：%s" % ("齐" if inv and all(inv.values()) else "有未成立：%s"
                        % [k for k, v in inv.items() if not v]),
        "交付说明：%s" % (note or "（缺）"),
        "交付目录：%s（%d 个文件）" % (out_dir, len(files)),
    ]
    return {"book": book, "verdict": verdict, "ok": verdict.startswith("可交付"),
            "counts": counts, "hard_gates_failed": hard, "invariants": inv,
            "note": note, "files": files, "checklist": checklist}


def _ensure_sample(root: str) -> dict:
    """缺样书就造（含跑一次管线到暂存区）。"""
    import fx_sample
    chk = fx_sample.check(root=root)
    if chk["ok"]:
        return {"built": False, "check": chk}
    res = fx_sample.make(root=root, with_pipeline=True)
    return {"built": True, "check": fx_sample.check(root=root), "make": {
        k: res.get(k) for k in ("pages", "items", "entry") if k in res}}


def run(root: str = "", publish: bool = True) -> dict:
    root = root or paths.data_root()
    pre = _ensure_sample(root)
    import pipeline_run as pr
    reg = books_mod.load(root=root)
    pr.BOOKS[BOOK] = dict(reg[BOOK])          # 只在本进程里补一次（不改产品配置）
    res = pr.run_pipeline(BOOK, out_root=root, do_publish=publish)
    val = (res.get("stages") or {}).get("validation") or {}
    rep_path = os.path.join(root, "s7-out", BOOK, "validation-report.json")
    rep = json.load(io.open(rep_path, encoding="utf-8")) if os.path.exists(rep_path) else {}
    s = summarize(rep or val, BOOK, root)
    s["stages"] = sorted((res.get("stages") or {}).keys())
    s["sample_built_now"] = pre["built"]
    return s


def selftest() -> int:
    cases = []
    ok_rep = {"verdict": "可交付", "counts": {"pass": 16, "warn": 2, "fail": 0, "unverified": 2, "n/a": 2},
              "hard_gates_failed": [], "invariants": {"I1": True, "I5": True},
              "delivery_note": {"path": "交付说明.md", "bytes": 1234}}
    s = summarize(ok_rep, "x", r"D:\nonexistent")
    cases.append(("正例：`可交付` + 无硬失败 + 不变量齐 → `ok=True`，清单里带交付说明",
                  s["ok"] and "交付说明.md" in " ".join(s["checklist"]), s["checklist"]))
    bad = dict(ok_rep, verdict="不得交付（硬门禁失败）", hard_gates_failed=["G3"], counts=dict(ok_rep["counts"], fail=1))
    s2 = summarize(bad, "x", r"D:\nonexistent")
    cases.append(("负例：硬门禁失败 → `ok=False`，且清单点名 G3（不许说成可交付）",
                  (not s2["ok"]) and "G3" in " ".join(s2["checklist"]), s2["checklist"]))
    bad2 = dict(ok_rep, delivery_note={})
    s3 = summarize(bad2, "x", r"D:\nonexistent")
    cases.append(("负例：缺交付说明 → 清单必须写「（缺）」（不假装有）",
                  "（缺）" in " ".join(s3["checklist"]), s3["checklist"]))
    bad3 = dict(ok_rep, invariants={"I1": True, "I5": False})
    s4 = summarize(bad3, "x", r"D:\nonexistent")
    cases.append(("负例：不变量未成立 → 清单必须点名 I5",
                  "I5" in " ".join(s4["checklist"]), s4["checklist"]))
    cases.append(("缺报告（空 dict）→ 判决写成「（缺）」，不崩",
                  summarize({}, "x", r"D:\nonexistent")["verdict"] == "", None))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:150]))
        passed += bool(ok)
    print("\n%d/%d demo_offline tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="离线 demo：跑一本自制样书，看 22 道门禁 + 交付说明")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--root", default="")
    r.add_argument("--no-publish", action="store_true", help="只写暂存区（不生成正式交付目录）")
    r.set_defaults(func=lambda a: _cmd_run(a))
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    a = ap.parse_args()
    return a.func(a)


def _cmd_run(a) -> int:
    s = run(a.root, publish=not a.no_publish)
    print("=" * 78)
    print("离线 demo：一本书从 PDF 到「可交付」，全程不填密钥、不装 OCR、不用 GPU")
    print("=" * 78)
    for line in s["checklist"]:
        print("  · " + line)
    if s.get("sample_built_now"):
        print("  · （样书是本次现造的）")
    print("\n交付目录里的文件：")
    for f in s["files"]:
        print("    %-28s %8.1f KB" % (f["name"], f["bytes"] / 1024))
    print("\n先看这三个：")
    print("    %s\\交付说明.md          ← 给人看的：判决、目录、数字、已知边界" % os.path.join("s7-out", s["book"]))
    print("    %s\\chapter-01-clean.md  ← 净版正文（可直接用）" % os.path.join("s7-out", s["book"]))
    print("    %s\\validation-report.json ← 机器报告（22 道门禁逐条 + 证据）" % os.path.join("s7-out", s["book"]))
    print("\n结果 =", "可交付" if s["ok"] else "不可交付：%s" % s["verdict"])
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
