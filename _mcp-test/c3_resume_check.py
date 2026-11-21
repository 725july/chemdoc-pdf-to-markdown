# -*- coding: utf-8 -*-
"""**C3 续跑演练（小样本，30 页）**：走**磁盘台账 + CLI**，验证"分批 → 中断 → 续跑 → 判定 → G21"。

为什么用 30 页而不是 428 页：本脚本要验的是**记账机械性**（分批/落盘/续跑/幂等/作废/门禁），
与页数无关；规模上限已由 `judge_worklist.py selftest` 的内存循环覆盖（合成行，无 PDF、无模型调用）。

用法: python -X utf8 c3_resume_check.py
退出码 0 = 全过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace


def _quiet(fn, *a, **kw):
    """跑 CLI 但**不把它那串 JSON 打到屏幕上**（演练只要断言，不要噪声输出）。"""
    with redirect_stdout(StringIO()):
        return fn(*a, **kw)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import judge_worklist as JW  # noqa: E402

PAGES, PPB, IPB = 30, 8, 20


def _synth_queue():
    items, gate = [], {}
    for p in range(1, PAGES + 1):
        items.append({"item_id": f"S3-P{p:04d}-C-1", "pdf_page": p, "review_type": "content_diff"})
        if p % 3 == 0:
            items.append({"item_id": f"S3-P{p:04d}-S-2", "pdf_page": p, "review_type": "structure_diff"})
        if p % 6 == 0:
            items.append({"item_id": f"S3-P{p:04d}-C-3", "pdf_page": p, "review_type": "content_diff",
                          "risk": {"signals": ["digit"]}})
        if p % 15 == 0:
            items.append({"item_id": f"TR-R1-{p:04d}-01", "pdf_page": p,
                          "review_type": "transcribe_region",
                          "bbox_provenance": {"rule": "R1"}})
    # 两条被准入拒收的项（**必须**带去处）
    for i, p in enumerate((7, 21)):
        iid = f"S3-P{p:04d}-REJ-{i}"
        items.append({"item_id": iid, "pdf_page": p, "review_type": "content_diff"})
        gate[iid] = {"item_id": iid, "admitted": False,
                     "reject_reasons": ["N-7_alignment_artifact"],
                     "destination": "recrop_and_requery"}
    return items, gate


def main() -> int:
    cases, root = [], tempfile.mkdtemp(prefix="c3-resume-")
    try:
        q = os.path.join(root, "visual-queue.s3.fx.jsonl")
        items, gate = _synth_queue()
        with open(q, "w", encoding="utf-8", newline="\n") as fh:
            for it in items:
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")
        gd = os.path.join(root, "gate")
        os.makedirs(gd, exist_ok=True)
        with open(os.path.join(gd, "gate-all.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
            for g in gate.values():
                fh.write(json.dumps(g, ensure_ascii=False) + "\n")

        # ① CLI plan（落盘）
        _quiet(JW.cmd_plan, SimpleNamespace(queue=q, gate_dir=gd, out=root, book="fx",
                                    pages_per_batch=PPB, items_per_batch=IPB,
                                    config=None, dry_run=False))
        rows, prog = JW.load_ledger(root)
        recs = prog["batches"]
        cases.append(("台账行数 == 队列条数（含拒收项也算账）", len(rows) == len(items),
                      {"ledger": len(rows), "queue": len(items)}))
        pages_by_batch = {r["batch_id"]: r["_pages"] for r in recs}
        union, dup, owner = set(), [], {}
        for bid, ps in pages_by_batch.items():
            if ps != list(range(min(ps), max(ps) + 1)):
                dup.append(("not_contiguous", bid))
            for p in ps:
                if p in owner:
                    dup.append(("overlap", p, owner[p], bid))
                owner[p] = bid
                union.add(p)
        cases.append(("批内页连续 + 批间不重叠", not dup, dup[:3]))
        cases.append(("每批 ≤%d 页" % PPB, all(len(ps) <= PPB for ps in pages_by_batch.values()),
                      [len(ps) for ps in pages_by_batch.values()]))
        split = []
        seen = {}
        for r in rows:
            if r.get("batch_id"):
                if r["pdf_page"] in seen and seen[r["pdf_page"]] != r["batch_id"]:
                    split.append(r["pdf_page"])
                seen[r["pdf_page"]] = r["batch_id"]
        cases.append(("同一页不拆批", not split, split[:3]))
        st = JW.status(rows, recs)
        first_p0 = next((r["batch_id"] for r in recs if r["by_priority"]["P0"]), None)
        cases.append(("含 P0（R1 转写）的批排在建议顺序首位",
                      first_p0 is None or st["batch_order"][0] == first_p0,
                      {"first_p0": first_p0, "order": st["batch_order"][:3]}))
        cases.append(("拒收项进台账但不占判定配额",
                      all(r["state"] == JW.STATE_REJECTED
                          for r in rows if r["item_id"] in gate)
                      and all(g not in seen for g in gate), None))

        # ② 判前两批（模拟一次会话），再重新**从磁盘**读 → 证明状态可续跑
        order, done = st["batch_order"], []
        for bid in order[:2]:
            ids = [r["item_id"] for r in rows if r["batch_id"] == bid and r["state"] == JW.STATE_PENDING]
            vp = os.path.join(root, f"verdicts.{bid}.jsonl")
            with open(vp, "w", encoding="utf-8", newline="\n") as fh:
                for i in ids:
                    fh.write(json.dumps({"item_id": i, "verdict": "exists",
                                         "judge": {"model": "in-session-agent",
                                                   "mode": "agent"}}, ensure_ascii=False) + "\n")
            _quiet(JW.cmd_apply, SimpleNamespace(ledger=root, verdicts=vp, batch=bid, expired_ids=""))
            done += ids
        rows2, prog2 = JW.load_ledger(root)
        recs2 = prog2["batches"]
        st2 = JW.status(rows2, recs2)
        cases.append(("中断后续跑：状态从磁盘恢复、pending 正确递减",
                      st2["pending"] == st["pending"] - len(done)
                      and all(next(r for r in rows2 if r["item_id"] == i)["state"] == JW.STATE_DECIDED
                              for i in done),
                      {"before": st["pending"], "after": st2["pending"], "judged": len(done)}))
        cases.append(("next_batch 前进到未判批", st2["next_batch"] not in done or st2["next_batch"] is None,
                      {"next": st2["next_batch"], "done_batches": order[:2]}))

        # ③ 幂等：对同一批**再判一次**（同一 verdicts 文件重放）
        vp0 = os.path.join(root, f"verdicts.{order[0]}.jsonl")
        _quiet(JW.cmd_apply, SimpleNamespace(ledger=root, verdicts=vp0, batch=order[0], expired_ids=""))
        rows3, _ = JW.load_ledger(root)
        rev = [r for r in rows3 if r["batch_id"] == order[0]]
        cases.append(("幂等：重复 apply 不翻状态、只记 revisions",
                      all(r["state"] == JW.STATE_DECIDED and int(r["revisions"]) >= 1 for r in rev),
                      {"revisions": sorted({int(r["revisions"]) for r in rev})}))

        # ④ 源变更 → 判定作废回 pending（I2：未判定永不当 pass）
        victim = done[0]
        vp1 = os.path.join(root, "verdicts.expire.jsonl")
        with open(vp1, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"item_id": victim, "verdict": "exists",
                                 "judge": {"model": "m", "mode": "agent"}}, ensure_ascii=False) + "\n")
        _quiet(JW.cmd_apply, SimpleNamespace(ledger=root, verdicts=vp1, batch=order[0],
                                     expired_ids=victim))
        rows4, prog4 = JW.load_ledger(root)
        cases.append(("源变更 → 该判定作废回 pending",
                      next(r for r in rows4 if r["item_id"] == victim)["state"] == JW.STATE_PENDING,
                      None))

        # ⑤ 判完全部 → G21 干净；再注入三种错 → 各自被抓
        rows5 = [dict(r) for r in rows4]
        for r in rows5:
            if r["state"] == JW.STATE_PENDING:
                r.update({"state": JW.STATE_DECIDED, "verdict_ref": r["item_id"],
                          "judge": {"model": "m", "mode": "agent"}})
        JW.write_ledger(root, rows5, recs, {"book": "fx", "queue": q,
                                            "config": prog4.get("config")})
        rows5, prog5 = JW.load_ledger(root)
        recs5 = prog5["batches"]
        clean = JW.check_ledger(rows5, recs5, queue_ids=[i["item_id"] for i in items],
                                product_marks={}, expired_ids=[],
                                report_pending=[])
        cases.append(("判完 + 拒收有去处 → G21 0 违规", clean["ok"], clean["violations_by_kind"]))
        bad = JW.check_ledger([r for r in rows5 if r["item_id"] != items[0]["item_id"]], recs5,
                              queue_ids=[i["item_id"] for i in items], product_marks={},
                              report_pending=[])
        cases.append(("丢项 → G21 抓到 ledger_item_missing",
                      "ledger_item_missing" in bad["violations_by_kind"], bad["violations_by_kind"]))
        rows_d = [dict(r) for r in rows5]
        next(r for r in rows_d if r["state"] == JW.STATE_REJECTED)["destination"] = ""
        bad2 = JW.check_ledger(rows_d, recs5, queue_ids=[i["item_id"] for i in items],
                               product_marks={}, report_pending=[])
        cases.append(("拒收无去处 → G21 抓到 reject_without_destination",
                      "reject_without_destination" in bad2["violations_by_kind"],
                      bad2["violations_by_kind"]))

        # ⑥ CLI status 能跑通（输出吞掉，只看断言）
        _quiet(JW.cmd_status, SimpleNamespace(ledger=root, ids=False))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = 0
    print("--- 断言 ---")
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name
              + ("" if ok else f"   -> {json.dumps(extra, ensure_ascii=False)[:160]}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} c3 resume-scale checks passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
