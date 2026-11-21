# -*- coding: utf-8 -*-
"""判定台账与配额（**C3**；SPEC §3.10）。**只记账、只算配额，不产生判定。**

设计约束:
  * 判定者 = **会话内 agent**（D16，用户裁定严格保持）；本模块**不调用任何模型**，
    只把"该判什么、按什么顺序、判到哪了"变成可续跑的机械记账。
  * **分批单位 = 连续页区间**（不是条数）：一批 = 一个页区间 + 该区间内全部待判项；
    **同一页不拆批**。
  * **拒收 ≠ 不判**：准入被拒的项也进台账（`state=rejected_with_destination` + `destination`），
    否则"拒收"就变成了静默丢失。
  * **幂等**：重复 apply 同一批不会把已判项翻回 `pending`；后判覆盖前判并留 `revisions`。
  * **过期**：源指纹变更（`expired_verdicts`）→ 该项回 `pending`（I2：未判定永不当 pass）。

用法:
  python -X utf8 judge_worklist.py plan   --queue <visual-queue.s3.<书>.jsonl> [--gate-dir <dir>] ^
                                          --out <s5-out\\<书>> [--pages-per-batch 40] [--items-per-batch 120] ^
                                          [--config <json>] [--dry-run]
  python -X utf8 judge_worklist.py apply  --ledger <dir> --verdicts <verdicts.ingested.<批>.jsonl> [--expired-ids <a,b>]
  python -X utf8 judge_worklist.py status --ledger <dir>
  python -X utf8 judge_worklist.py check  --ledger <dir> [--queue <visual-queue.s3.<书>.jsonl>]
  python -X utf8 judge_worklist.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from contracts import REVIEW_RE
from tuning import TUNE
import paths  # B2：工作根唯一出口（SPEC §7.10）

# 产物里"未判定项"的痕迹是**类别级**的（与 G2 同一约定）：
#   `<!-- REVIEW: S3_content_diff page=2 bbox=… -->`（**不含 item_id**）、
#   转写任务则是 `<!-- REVIEW: transcribe_unresolved page=2 … -->`。
# ⚠️ 踩过：G21 一开始按 item_id 字符串去产物里找 → **13/13 全假红**（真书实测）。
PAGE_RE = re.compile(r"page=(\d+)")


def mark_categories_by_page(md: str) -> dict:
    """从产物里抽出 `{页号: {痕迹类别}}`——G21 判据③的输入（**与 G2 同一约定**，别再另立一套）。"""
    out: dict[int, set] = {}
    for cat, rest in REVIEW_RE.findall(md or ""):
        m = PAGE_RE.search(rest)
        if m:
            out.setdefault(int(m.group(1)), set()).add(cat)
    return out


def expected_marks(review_type: str, item_id: str = "") -> tuple:
    """该类型的未判定项在产物里**可能**留下的痕迹类别（**只用于计数，不用于判 fail**）。

    转写任务有两套既有约定（都写在 G2 里）：`transcribe_unresolved`（有 verdict 但未解决）
    与 `s4_unresolved_<rule>`（S4 命中未被任务覆盖）。
    **产物可见性的权威判据是 G2**——G21 再判一遍会造成"一个根因两条例外"（真书实测踩到）。
    """
    if review_type == "transcribe_region":
        out = ["transcribe_unresolved"]
        parts = (item_id or "").split("-")
        if len(parts) > 1 and parts[0] == "TR":
            out.append(f"s4_unresolved_{parts[1]}")
        return tuple(out)
    return (f"S3_{review_type}",)

# **默认值从哪来**（B0，2026-10-12）：唯一实测事实 = 历史一次会话判过 **39 条 / 3 页**（试点批量臂）。
# → `items_per_batch = 39`：**已知下界**，先保证"不向会话要超过它已证明做过的量"。
# → `pages_per_batch = 40`：**安全阀，未标定**（按实测 ≈1.4 条/页，通常先触发条数上限）。
# → `calibrated = False`：如实声明"这不是标定值"，落进 `judge-progress.json`，**只观测、不进门禁**。
DEFAULT_CONFIG = {"pages_per_batch": TUNE["pages_per_batch"],
                  "items_per_batch": TUNE["items_per_batch"],
                  "calibrated": TUNE["judge_calibrated"]}  # L2：见 tuning.json
PRIORITY_ORDER = ("P0", "P1", "P2", "P3")
STATE_PENDING = "pending"
STATE_DECIDED = "decided"
STATE_WAIVED = "waived"
STATE_REJECTED = "rejected_with_destination"
# 需要判定的状态（`rejected_with_destination` 与 `waived` 不需要）
NEEDS_JUDGMENT = (STATE_PENDING, STATE_DECIDED)
WORKLIST_NAME = "judge-worklist.jsonl"
PROGRESS_NAME = "judge-progress.json"
SEVERE_SIGNALS = ("digit", "unit", "equation", "formula", "minus", "sign", "sub", "sup")


# ---------------------------------------------------------------- 优先级 / 分批
def priority_of(item: dict) -> str:
    """P0 硬门禁相关（R1 转写）> P1 严重级（数字/单位/公式）> P2 结构/归属 > P3 其余。"""
    rt = item.get("review_type")
    iid = item.get("item_id") or ""
    if rt == "transcribe_region":
        rule = ((item.get("bbox_provenance") or {}).get("rule")) or ""
        if rule == "R1" or iid.startswith("TR-R1-"):
            return "P0"                       # 有墨迹无内容 → G11 硬门禁的入口
        return "P1"                            # R4a/R4b：数字/单位/几何探针 → 严重级
    if rt in ("structure_diff", "ownership", "figure_region"):
        return "P2"
    sig = ((item.get("risk") or {}).get("signals")) or []
    if any(str(s).lower() in SEVERE_SIGNALS for s in sig):
        return "P1"
    return "P3"


def build_rows(items: list[dict], gate_by_id: dict | None = None) -> list[dict]:
    """队列行 → 台账行。**全量队列都进台账**（含被拒项）。"""
    gate_by_id = gate_by_id or {}
    rows = []
    for it in items:
        g = gate_by_id.get(it.get("item_id"))
        admitted = True if g is None else bool(g.get("admitted"))
        row = {
            "item_id": it.get("item_id"),
            "pdf_page": int(it.get("pdf_page") or 0),
            "review_type": it.get("review_type"),
            "priority": priority_of(it),
            "batch_id": None,
            "state": STATE_PENDING if admitted else STATE_REJECTED,
            "destination": None if admitted else (g.get("destination") or ""),
            "verdict_ref": None,
            "revisions": 0,
        }
        rows.append(row)
    return rows


def plan_batches(rows: list[dict], pages_per_batch: int = 40,
                 items_per_batch: int = 39) -> list[dict]:
    """按**连续页区间**切批：逐页并入当前批，超上限就封批；**同一页不拆批**。

    返回批清单（按页序，编号 `B001`…），每批 `pages` 是**连续升序**页号。
    """
    by_page: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("state") == STATE_PENDING:
            by_page.setdefault(r["pdf_page"], []).append(r)
    pages = sorted(by_page)
    batches: list[dict] = []
    cur: list[int] = []

    def _items(ps: list[int]) -> list[dict]:
        return [r for p in ps for r in
                sorted(by_page[p], key=lambda r: (PRIORITY_ORDER.index(r["priority"]), r["item_id"]))]

    for p in pages:
        if cur and (len(cur) + 1 > pages_per_batch or len(_items(cur)) + len(by_page[p]) > items_per_batch):
            batches.append(_finish(cur, by_page))
            cur = []
        cur.append(p)
    if cur:
        batches.append(_finish(cur, by_page))
    return batches


def _finish(ps: list[int], by_page: dict[int, list[dict]]) -> dict:
    items = [r for p in ps for r in
             sorted(by_page[p], key=lambda r: (PRIORITY_ORDER.index(r["priority"]), r["item_id"]))]
    return {"pages": list(ps), "items": items,
            "by_priority": {p: sum(1 for r in items if r["priority"] == p) for p in PRIORITY_ORDER}}


def assign_batches(rows: list[dict], batches: list[dict]) -> list[dict]:
    """给台账行写回 `batch_id`（批编号按页序确定 → 可复现）。"""
    for i, b in enumerate(batches, 1):
        bid = f"B{i:03d}"
        for r in b["items"]:
            r["batch_id"] = bid
    return rows


def batch_records(batches: list[dict], rows: list[dict]) -> list[dict]:
    """批清单的可落盘形式（不含 items 明细，含页区间与统计）。"""
    recs = []
    for i, b in enumerate(batches, 1):
        bid = f"B{i:03d}"
        bid_rows = [r for r in rows if r.get("batch_id") == bid]
        recs.append({
            "batch_id": bid, "page_from": min(b["pages"]), "page_to": max(b["pages"]),
            "pages": len(b["pages"]), "pages_list": list(b["pages"]),
            "item_count": len(bid_rows),
            "by_priority": {p: sum(1 for r in bid_rows if r["priority"] == p) for p in PRIORITY_ORDER},
        })
    return recs


def _batch_pending(rec: dict, rows: list[dict] | None):
    if rows is None:
        return rec.get("item_count", 0), rec["by_priority"].get("P0", 0)
    rs = [x for x in rows if x.get("batch_id") == rec["batch_id"] and x["state"] == STATE_PENDING]
    return len(rs), sum(1 for x in rs if x["priority"] == "P0")


def order_batches(recs: list[dict], rows: list[dict] | None = None) -> list[str]:
    """推荐的判定顺序：**还有 P0 未判的批优先**，其次**还有待判项的批**，同级按页序。

    批编号本身仍按页序（机械可复现）；这里只给"下一步判哪批"的建议顺序。
    传入 `rows` 时按**实时待判状态**排序（否则按计划时的静态统计）。
    """
    def key(r):
        pend, p0 = _batch_pending(r, rows)
        return (0 if p0 else 1, 0 if pend else 1, r["page_from"], r["batch_id"])
    return [r["batch_id"] for r in sorted(recs, key=key)]


# ---------------------------------------------------------------- 幂等记账
def apply_verdicts(rows: list[dict], verdicts: list[dict], expired_ids=(),
                   batch_id: str | None = None, check_only: bool = False) -> tuple[list[dict], dict]:
    """把一批判定写进台账（纯函数，幂等）。返回 (rows, report)。

    **判词必须合法**（2026-10-26）：`verdict` 不在 `VERDICTS_WITH_TRANSCRIPTION` 里 → **只拒这一条**
    （不写 decided、保持 pending），记进 `rep["unknown_verdict"]`。为什么不整批拒绝：一个手滑不该让
    一整批好判定白写。为什么不自动纠错：工具猜不到你想写哪个词（`exist` 可能是 `exists` 也可能是
    `not_exists`），猜错 = 悄悄改结论；这里只**退回待办**，由判定者补。
    `check_only=True`：只校验、**不写**（给 agent 提交前预检用）。
    """
    from contracts import VERDICTS_WITH_TRANSCRIPTION as _ALLOWED
    _allowed = set(_ALLOWED)
    exp = set(expired_ids or ())
    by_id = {r["item_id"]: r for r in rows}
    rep = {"applied": [], "reapplied": [], "expired": [], "unknown": [], "not_in_batch": [],
           "unknown_verdict": []}
    for v in verdicts or []:
        iid = v.get("item_id")
        r = by_id.get(iid)
        if r is None:
            rep["unknown"].append(iid)
            continue
        _vd = v.get("verdict")
        if _vd is not None and str(_vd) not in _allowed:
            # **只拒这一条**：不碰台账（保持 pending），并如实报出"允许哪些词"
            rep["unknown_verdict"].append({"item_id": iid, "verdict": _vd,
                                           "allowed": list(_ALLOWED)})
            continue
        if check_only:
            continue
        if iid in exp or v.get("expired"):
            r["state"] = STATE_PENDING          # I2：过期判定视同未判定
            r["expired"] = True
            r["verdict_ref"] = None
            rep["expired"].append(iid)
            continue
        if r.get("state") == STATE_DECIDED:
            r["revisions"] = int(r.get("revisions") or 0) + 1
            rep["reapplied"].append(iid)
        else:
            rep["applied"].append(iid)
        if batch_id and r.get("batch_id") != batch_id:
            rep["not_in_batch"].append(iid)
        r["state"] = STATE_DECIDED
        r["verdict_ref"] = iid
        r["verdict"] = _vd
        # **A3 补线**：台账不仅要记"判过了"，还要能**独立重建判定**（下游/事后审计都要 conf 与证据）。
        # 原先只存 verdict/verdict_ref → `verdicts.ingested.*` 无从生成，门禁于是不认这些判定（实测踩到）。
        for k in ("confidence", "evidence", "transcription", "intended_action", "errata"):
            if v.get(k) is not None:
                r[k] = v[k]
        if v.get("judge"):
            r["judge"] = v["judge"]
        if batch_id:
            r["applied_in_batch"] = batch_id
    return rows, rep


# ------------------------------------------------- 台账 → 下游（A3 补线）
def action_for(review_type: str, verdict: str, operation, confidence: float,
               conf_threshold: float) -> dict:
    """判定 → `decision_intended` / `action_applied` / `needs_escalation`（口径与 `s5_pilot.py` 同构）。

    U-10 裁定：**转写类直接改正文**（`insert`）；**其余一切判定只标记**（`review`），不改正文。
    """
    conf = float(confidence or 0.0)
    if review_type == "figure_region":
        # U-15：确认有图 → S7 确定性补图（交付版）；没有 → no-op；看不清 → review。
        if verdict == "exists":
            intended, applied = "insert_figure", "insert"
        elif verdict == "not_exists":
            intended, applied = "no_op", "no_op"
        else:
            intended, applied = "review", "review"
    elif verdict == "non_book_ink":
        # 2026-10-26：**非本书内容**（透印/手写/印章/装饰）→ S7 把它降级成注释（净版不含），
        # 原文保留在留底版 + errata(kind=non_book_ink)。**不改写、不删除。**
        intended, applied = "quarantine", "review"
    elif verdict == "transcribed":
        intended, applied = "insert_transcription", "insert"
    elif verdict == "no_text":
        intended, applied = "no_op", "no_op"
    elif verdict == "no_content_diff":
        intended, applied = "no_op", "no_op"
    elif verdict == "unclear":
        intended, applied = "review", "review"
    elif verdict == "exists":
        intended = {"delete": "restore_to_main", "insert": "keep_main",
                    "replace": "keep_main", "structure": "keep_main"}.get(operation, "keep_main")
        applied = "review"
    else:                                          # not_exists
        intended = {"insert": "reject_main_claim", "delete": "confirm_omit",
                    "replace": "keep_main_flagged", "structure": "review"}.get(operation, "review")
        applied = "review"
    return {"decision_intended": intended, "action_applied": applied,
            "needs_escalation": (verdict == "unclear") or conf < float(conf_threshold)}


INGEST_FIELDS = ("item_id", "pdf_page", "review_type", "bbox", "bbox_norm", "band",
                 "render_sha256", "crop", "queue_id", "anchor_provider", "caption_hint")

# 产物根 = 本脚本的上一级（与 `s2_ir` / `pipeline_run` / `volumes` 同一约定）。
# **不能**拿台账目录当根：绑定指纹是按**产物根的相对路径**读源 PDF/引擎产物/IR 的，
# 用错根会让每条判定都算成 `expired` → 校验器把已判的项又当"未判定"（实测踩到）。
ROOT = paths.data_root()


def build_ingested(rows: list[dict], queue_by_id: dict, key: str, conf_threshold: float,
                   root: str = None, binding_fn=None) -> list[dict]:
    """把台账里 `decided` 的行**重建**成 `verdicts.ingested.*.jsonl` 的行（纯函数）。

    为什么必须有这一步：`validate_run.merge_verdicts()` **只读 ingest 文件、不读台账**
    （见 SPEC §3.10-3）——台账记了状态但没接到判据的读数路径上，等于**判了白判**（实测：61 条判完，
    G11/G8 仍报"未判定"）。
    """
    out = []
    for r in rows:
        if r.get("state") != STATE_DECIDED or not r.get("verdict"):
            continue
        q = dict(queue_by_id.get(r["item_id"]) or {})
        act = action_for(r.get("review_type") or q.get("review_type") or "", r["verdict"],
                         (q.get("span") or {}).get("operation"), r.get("confidence"), conf_threshold)
        row = {k: q.get(k) for k in INGEST_FIELDS if q.get(k) is not None}
        row.update({
            "item_id": r["item_id"],
            "pdf_page": q.get("pdf_page", r.get("pdf_page")),
            "review_type": q.get("review_type", r.get("review_type")),
            "verdict": r["verdict"],
            "confidence": r.get("confidence"),
            "transcription": r.get("transcription") if r["verdict"] == "transcribed" else None,
            "judge": r.get("judge"),
            "evidence": r.get("evidence"),
            "arm": "c3-ledger",
            **act,
        })
        if r.get("intended_action"):
            row["decision_intended"] = r["intended_action"]
        # 2026-11-15：`errata` 必须**穿透到 ingest**，否则 S7 看不到 `missing_text`
        # （`merge_verdicts` 只读 ingest，不读台账）→ 页末不会留 `REVIEW: missing_text`。
        if r.get("errata"):
            row["errata"] = r["errata"]
        if binding_fn is not None:
            try:
                row["binding"] = binding_fn(row.get("pdf_page"))
            except Exception:                                                   # noqa: BLE001
                row["binding"] = None
        out.append(row)
    return sorted(out, key=lambda x: str(x.get("item_id")))


def write_ingested(ledger_dir: str, key: str, conf_threshold: float = None, force: bool = False) -> dict:
    """把台账判定写成 `runs/c3-ledger/verdicts.ingested.c3.jsonl`（幂等、可重复调用）。

    落在 **runs 下**是硬要求：`merge_verdicts` 只扫 `runs/*/verdicts.ingested.*.jsonl`。

    **A7-14 保护**：当台账里**一条判定都没有**、而目标文件**已存在且有内容**时，**不覆盖**——
    实测踩过：台账被清空后误跑 `backfill`，把 61 条 ingest **重写成 0 条**，把"台账丢了"升级成
    "下游也一起丢了"。要强行覆盖请显式 `force=True`（CLI `--force`）。
    """
    if conf_threshold is None:
        conf_threshold = TUNE.get("conf_threshold", 0.9)
    rows, prog = load_ledger(ledger_dir)
    queue_path = prog.get("queue") or ""
    queue = _load_jsonl(queue_path) if queue_path and os.path.exists(queue_path) else []
    by_id = {q.get("item_id"): q for q in queue}
    binding_fn = None
    try:
        import binding as _binding
        binding_fn = lambda page: _binding.current(key, ROOT, page)  # noqa: E731
    except Exception:                                                           # noqa: BLE001
        binding_fn = None
    ingested = build_ingested(rows, by_id, key, conf_threshold,
                              root=ROOT, binding_fn=binding_fn)
    run_dir = os.path.join(ledger_dir, "runs", "c3-ledger")
    os.makedirs(run_dir, exist_ok=True)
    out_path = os.path.join(run_dir, "verdicts.ingested.c3.jsonl")
    # **A7-14 保护**：台账无判定 + 目标文件已有内容 → 拒绝覆盖（见函数 docstring）
    if not ingested and not force and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return {"out": out_path, "items": 0, "transcribed": 0, "escalations": 0,
                "queue": queue_path, "missing_from_queue": [],
                "refused": ("台账内 0 条判定，而 %s 已有内容（%d B）—— 已拒绝覆盖；"
                            "确需清空请显式 --force" % (out_path, os.path.getsize(out_path)))}
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for row in ingested:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    n_transcribe = sum(1 for r in ingested if r.get("verdict") == "transcribed")
    return {"out": out_path, "items": len(ingested), "transcribed": n_transcribe,
            "escalations": sum(1 for r in ingested if r.get("needs_escalation")),
            "queue": queue_path, "missing_from_queue": sorted(
                set(r["item_id"] for r in rows if r.get("state") == STATE_DECIDED) - set(by_id))}


ERRATA_KINDS = ("source_typo", "non_book_ink", "missing_text")
# 2026-11-15（实跑 P0-1/P0-2）：第三类 `missing_text` —— 主引擎漏掉、页面上确实有的内容。
# 它**不改正文**，但必须给出"前文结尾 + 后文开头"锚点，读者确认后可用脚本插回去（SPEC §2.4.2）。
ANCHOR_POSITIONS = ("inline", "own_line")
ANCHOR_CONFIDENCE = ("high", "low")


def errata_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """从台账里抽出**两类** errata 条目（SPEC §2.4.2；A7-13 + 2026-10-26）。

    - `source_typo`：**原书印错/笔误**（原页如此）→ 正文**一个字都不改**；
    - `non_book_ink`：**非本书内容**（背面透印 / 读者手写 / 印章 / 贴纸 / 装饰）→ S7 把它移出净版
      （留底版降级为注释）。

    两类都必须带 **`block_id`**（绑到源块）+ **`original`**（页上到底是什么字）；
    `non_book_ink` 还要在 `basis` 里说清"为什么它不是书的内容"（看不懂 → 应该判 `unclear`，不是登记）。
    返回 `(errata 行, 校验错误列表)`；**错误不静默丢弃**（交给 `check_ledger` 报）。
    """
    out, errs = [], []
    for r in rows:
        e = r.get("errata")
        if not e:
            continue
        iid = r.get("item_id")
        if not isinstance(e, dict):
            errs.append("errata:not-a-dict:%s" % iid)
            continue
        kind = (e.get("kind") or "source_typo").strip()
        if kind not in ERRATA_KINDS:
            errs.append("errata:bad-kind:%s" % iid)
            kind = "source_typo"
        bid = (e.get("block_id") or "").strip()
        orig = (e.get("original") or "").strip()
        # ---- `missing_text`：不需要 block_id（那段字根本不在主引擎的块里），但**必须**有锚点 ----
        if kind == "missing_text":
            miss = (e.get("missing_text") or "").strip()
            an = e.get("anchor") if isinstance(e.get("anchor"), dict) else {}
            before = (an.get("before") or "").strip()
            after = (an.get("after") or "").strip()
            pos = (an.get("position") or "own_line").strip()
            conf = (an.get("confidence") or "high").strip()
            if not miss:
                errs.append("errata:missing-missing_text:%s" % iid)      # 没有"缺的内容"就没有条目
            if not before or not after:
                errs.append("errata:missing-anchor:%s" % iid)            # 锚点缺一半 → 插不回去
            if pos not in ANCHOR_POSITIONS:
                errs.append("errata:bad-anchor-position:%s" % iid)
                pos = "own_line"
            if conf not in ANCHOR_CONFIDENCE:
                errs.append("errata:bad-anchor-confidence:%s" % iid)
                conf = "low"
            if (e.get("basis") or "").strip() in ("", "—"):
                errs.append("errata:missing-basis:%s" % iid)             # 必须说清"凭什么说页面有、产物没有"
            out.append({
                "item_id": iid, "kind": kind, "page": r.get("pdf_page"),
                "block_id": bid or None,
                "position": e.get("position") or "—",
                "original": orig or miss,
                "suggestion": e.get("suggestion") or "正文不改；确认后用 errata_apply 插回",
                "basis": e.get("basis") or "—",
                "confidence": (r.get("confidence") if r.get("confidence") is not None
                               else e.get("confidence", "—")),
                "judge": r.get("judge"),
                # 只有这一类有这三个字段（其余保持原样，不新增噪音）
                "missing_text": miss,
                # 2026-11-15（1c）：把**位置**也带上，S7 才能按当前读序重算锚点
                "bbox": e.get("bbox") or r.get("bbox_norm") or r.get("bbox"),
                "anchor": {"before": before, "after": after,
                           "position": pos, "confidence": conf},
            })
            continue
        if not bid:
            errs.append("errata:missing-block_id:%s" % iid)          # A7-13 硬要求：绑不到块就无法对账
        if not orig:
            errs.append("errata:missing-original:%s" % iid)          # 不许"感觉原书错了"却写不出原页字面
        if kind == "non_book_ink" and (e.get("basis") or "").strip() in ("", "—"):
            errs.append("errata:missing-basis:%s" % iid)             # 必须说清"为什么不是书的内容"
        out.append({
            "item_id": iid,
            "kind": kind,
            "page": r.get("pdf_page"),
            "block_id": bid or None,
            "position": e.get("position") or "—",
            # 注意：`original` 必须是**原页字面**（供 S7 与产物对账），不是我们的推论
            "original": orig,
            "suggestion": e.get("suggestion") or ("移出净版（非本书内容）" if kind == "non_book_ink"
                                                  else "保持原样"),
            "basis": e.get("basis") or "—",
            "confidence": r.get("confidence") if r.get("confidence") is not None else e.get("confidence", "—"),
            "judge": r.get("judge"),
        })
    return out, errs


def write_errata(ledger_dir: str) -> dict:
    """把台账里的 U 类条目写成 `errata.jsonl`（幂等）。

    **为什么写在台账目录**：与 `waivers.jsonl` 同理 —— 台账目录是"书级权威状态"的位置，
    且不会随 `publish` 的**整体搬移**而丢失（SPEC「豁免记录契约」踩过这个坑）。
    """
    rows, _prog = load_ledger(ledger_dir)
    out, errs = errata_rows(rows)
    path = os.path.join(ledger_dir, "errata.jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return {"out": path, "entries": len(out), "errors": errs}


def waive_rows(rows: list[dict], item_ids, reason: str = "", approved_by: str = "") -> tuple[list[dict], dict]:
    """用户显式豁免（I3 的落点）：`state=waived`，**必须**带 `approved_by`。"""
    ids = set(item_ids or ())
    hit, miss = [], []
    for r in rows:
        if r["item_id"] in ids:
            if not approved_by:
                miss.append(r["item_id"])       # 缺批准人 → 不接受（由调用方报错）
                continue
            r["state"] = STATE_WAIVED
            r["waiver"] = {"reason": reason, "approved_by": approved_by}
            hit.append(r["item_id"])
    return rows, {"waived": hit, "rejected_missing_approved_by": miss}


# ---------------------------------------------------------------- 状态 / G21
def status(rows: list[dict], recs: list[dict]) -> dict:
    by_state: dict[str, int] = {}
    by_pri: dict[str, dict] = {p: {"total": 0, "pending": 0, "decided": 0, "waived": 0,
                                   "rejected": 0} for p in PRIORITY_ORDER}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        k = {"pending": "pending", "decided": "decided", "waived": "waived",
             STATE_REJECTED: "rejected"}.get(r["state"], r["state"])
        cell = by_pri.setdefault(r["priority"], {"total": 0, "pending": 0, "decided": 0,
                                                 "waived": 0, "rejected": 0})
        cell["total"] += 1
        cell[k] = cell.get(k, 0) + 1
    order = order_batches(recs, rows)
    pending_ids = sorted(r["item_id"] for r in rows if r["state"] == STATE_PENDING)
    # 下一批 = 推荐顺序里**还有待判项**的第一批（已判完的批不进 `next_batch`）
    next_batch = next((bid for bid in order
                       if any(r["state"] == STATE_PENDING and r.get("batch_id") == bid
                              for r in rows)), None)
    return {
        "total": len(rows), "by_state": by_state,
        "batch_count": len(recs), "by_priority": by_pri,
        "pending": len(pending_ids), "p0_pending": sum(
            1 for r in rows if r["state"] == STATE_PENDING and r["priority"] == "P0"),
        "pending_ids": pending_ids,
        "next_batch": next_batch, "batch_order": order,
        "batches": recs,
    }


def rec_pages(rec: dict) -> list:
    """批的**真实**页清单：优先 `pages_list`（本轮起落盘）→ `_pages`（内存）→ 旧的连续回退。

    ⚠️ 踩过（第二次同款）：用 `page_from..page_to` **假定连续**，会让**稀疏页**（没差异的页不进队列）
    一律报 `batch_page_union_mismatch` → **G21 误拦**。稀疏输入是合法输入。
    """
    ps = rec.get("pages_list") or rec.get("_pages")
    if ps:
        return list(ps)
    return list(range(rec["page_from"], rec["page_to"] + 1))


def check_ledger(rows: list[dict], recs: list[dict], queue_ids=None, product_marks=None,
                 expired_ids=(), report_pending=None, key: str = "", ingest=None,
                 root: str = "") -> dict:
    """**G21 的判据集**（纯函数）。门禁自己从台账独立复算，不信产物里的自检结论。

    `key`（**A7-14 加固**）：给了它就**逐条独立复算指纹**、抓"台账里已过期的判定"。
    `ingest`（**A7-14 加固**）：给了它就核对"**台账（唯一真相源）与 ingest（下游读数）一致**"。
    `root`（**A7-15**）：复算指纹用的**产物根**。**必须**由调用方传"本次校验的那棵树"——
    不传则退回全局默认根，于是**副本树 / 临时夹具会被整片误判过期**（A7-15 对抗测试实证：
    合成夹具的基线因此全红）。门禁 `gates.g21` 传 `ctx.root`；老调用不传 = 老行为（向后兼容）。
    三者不给（旧调用 / 自测夹具）则跳过对应条目 —— 避免在没有产物上下文的场景假红。
    `product_marks` = `mark_categories_by_page(产物正文)`；判据③按**类别级痕迹**核对
    （与 G2 同一约定——按 item_id 找会全假红，真书实测过）。
    """
    v: list[dict] = []
    exp = set(expired_ids or ())
    key = key or ""

    # ① 批次不重叠 + 批内页连续 + 行与批的页一致
    seen: dict[int, str] = {}
    for rec in recs:
        bid = rec["batch_id"]
        ps = rec_pages(rec)
        # **不要求"页号连续"**（输入可以稀疏）；只要求升序去重（批的定义是"一段页区间"，不是数学区间）
        if ps != sorted(set(ps)):
            v.append({"kind": "batch_pages_not_sorted_unique", "batch": bid, "pages": ps})
        for p in set(ps):
            if p in seen:
                v.append({"kind": "batch_page_overlap", "page": p, "batches": [seen[p], bid]})
            else:
                seen[p] = bid
    for r in rows:
        if r.get("batch_id") and r["batch_id"] in {x["batch_id"] for x in recs}:
            rec = next(x for x in recs if x["batch_id"] == r["batch_id"])
            ps = rec_pages(rec)
            if r["pdf_page"] not in ps:
                v.append({"kind": "row_batch_page_mismatch", "item": r["item_id"],
                          "batch": r["batch_id"], "page": r["pdf_page"]})

    # ② 不丢批：台账 item 集合 == 队列 item 集合；批页并集 == 需判定项的页集合
    if queue_ids is not None:
        q, l = set(queue_ids), {r["item_id"] for r in rows}
        for iid in sorted(q - l):
            v.append({"kind": "ledger_item_missing", "item": iid})
        for iid in sorted(l - q):
            v.append({"kind": "ledger_item_extra", "item": iid})
        if q != l:
            v.append({"kind": "ledger_queue_mismatch", "queue": len(q), "ledger": len(l)})
    if recs:
        batch_pages = set()
        for rec in recs:
            batch_pages |= set(rec_pages(rec))
        need_pages = {r["pdf_page"] for r in rows
                      if r["state"] in (STATE_PENDING, STATE_DECIDED)}
        if batch_pages != need_pages:
            v.append({"kind": "batch_page_union_mismatch",
                      "missing": sorted(need_pages - batch_pages),
                      "extra": sorted(batch_pages - need_pages)})

    # ③ 未判定集合必须**显式登记**：台账 `pending` 与产物报告里的快照 `report.judgment.pending`
    #    必须逐项一致（漂移核对）。**"产物里看不看得见痕迹"归 G2**（它有两条精细约定），
    #    这里只**计数**、不判违规——否则同一个根因会被登记成两条例外（真书实测踩到）。
    pend = sorted(r["item_id"] for r in rows if r["state"] == STATE_PENDING)
    if report_pending is not None and sorted(report_pending) != pend:
        v.append({"kind": "pending_not_registered",
                  "ledger": len(pend), "report": len(report_pending),
                  "only_ledger": sorted(set(pend) - set(report_pending))[:10],
                  "only_report": sorted(set(report_pending) - set(pend))[:10]})
    no_mark = []
    if product_marks is not None:
        for r in rows:
            if r["state"] != STATE_PENDING:
                continue
            cats = (product_marks or {}).get(r["pdf_page"], set())
            if not (set(expected_marks(r.get("review_type") or "", r["item_id"])) & cats):
                no_mark.append({"item": r["item_id"], "page": r["pdf_page"],
                                "want_any": list(expected_marks(r.get("review_type") or "",
                                                                r["item_id"])),
                                "got": sorted(cats)})

    # ④ 判定者与指纹在位；过期判定不得算已判
    no_judge = []
    for r in rows:
        if r["state"] != STATE_DECIDED:
            continue
        if r["item_id"] in exp:
            # **硬**：过期判定（源变了）不得被算作已判（I2 的直接推论）
            v.append({"kind": "expired_counted_as_decided", "item": r["item_id"]})
        j = r.get("judge") or {}
        if not (j.get("model") and j.get("mode")):
            # **观测**（不拦）：可追溯性要求；手写 verdicts 常缺这字段，拦它会把小书/正常书全挡住。
            # 真需要"判定者必须在位"的场合，看报告里的 `decided_without_judge` 计数即可。
            no_judge.append(r["item_id"])

    # ⑤ 拒收必有去处
    for r in rows:
        if r["state"] == STATE_REJECTED and not (r.get("destination") or "").strip():
            v.append({"kind": "reject_without_destination", "item": r["item_id"]})

    # ⑥ **A7-13：U 类（原文如此）条目必须可对账** —— 缺 `block_id` 或写不出原页字面 = 违规。
    #    为什么是硬判据：U 类的语义是"**我们没动这段正文**"，而它的唯一证据就是"绑到哪个块 + 原页怎么写"；
    #    两样缺一样，"正文没被改"就无从验证（SPEC §2.4.2）。
    _errata, _errata_errs = errata_rows(rows)
    for e in _errata_errs:
        # 形如 `errata:missing-block_id:<item_id>` → kind 取"errata:xxx"两段，item 取第三段
        _parts = e.split(":", 2)
        v.append({"kind": ":".join(_parts[:2]), "item": _parts[2] if len(_parts) > 2 else ""})

    # ⑥b **2026-10-26：台账里不得出现词表之外的 verdict**
    #    为什么是硬判据：`action_for` 的 else 分支会把未知词当 `not_exists` → 产物什么都不改，
    #    但这条已被算作"已判定"（从待办里消失）。这正是"假装判过了"，必须拦。
    from contracts import VERDICTS_WITH_TRANSCRIPTION as _ALLOWED_V
    for r in rows:
        _vd = r.get("verdict")
        if r.get("state") == STATE_DECIDED and _vd is not None and str(_vd) not in set(_ALLOWED_V):
            v.append({"kind": "verdict_unknown", "item": r["item_id"], "verdict": _vd})

    # ⑦ **A7-14 加固：孤儿批次引用**（对抗测试抓到的漏洞）
    #    原先只对"**存在的**批次"交叉核对行页区间；若行指向一个**不存在的批次**（如手改成 B999），
    #    没有任何判据会报 → 静默放过。这里补上"行引用的批次必须存在（或为空）"。
    _bids = {rec.get("batch_id") for rec in (recs or [])}
    for r in rows:
        bid = r.get("batch_id")
        if bid and bid not in _bids:
            v.append({"kind": "row_batch_not_in_plan", "item": r["item_id"], "batch": bid})

    # ⑧ **A7-14 加固：判定必须"独立于产物自述"**（对抗测试抓到的漏洞，也是最关键的一条）
    #    原先"过期判定"是把 `report.json.expired_verdicts`（**产物自述**）当输入 —— 而那是由 ingest 算出来的。
    #    于是当**台账**与 ingest 不一致（例如台账里的 `binding` 被改坏/过期）时，门禁看不见（实测 ①④ 漏）。
    #    这里改为**门禁自己从台账逐条复算指纹**：台账里的 binding 一旦过期，判定就不得算"已判"。
    _key = key or ""
    if _key:
        try:
            import binding as _binding
            for r in rows:
                if r.get("state") != STATE_DECIDED:
                    continue
                # 传了 `root` 就用"本次校验的那棵树"复算；没传 = 老行为（binding 的默认根）
                _st = (_binding.status_for_verdict(r, _key, root) if root
                       else _binding.status_for_verdict(r, _key))
                if _st == "expired":
                    v.append({"kind": "ledger_binding_expired", "item": r["item_id"],
                              "page": r.get("pdf_page")})
        except Exception:                                                    # noqa: BLE001
            pass                     # 无法独立复算时不误报（宁可少报，不可假红）

    # ⑨ **A7-14 加固：台账（唯一真相源）与 ingest（下游读数）必须一致**
    #    对抗测试实证：只把台账里某条的 `verdict` 抹掉，别的判据全绿 —— 因为下游读的是 ingest。
    #    两边一旦漂移，"台账说没判 / ingest 说判了"就没人管。这里做集合与取值两层核对。
    if ingest is not None:
        _ing = {r.get("item_id"): r for r in (ingest or [])}
        for r in rows:
            if r.get("state") != STATE_DECIDED:
                continue
            _i = _ing.get(r["item_id"])
            if _i is None:
                v.append({"kind": "decided_missing_from_ingest", "item": r["item_id"]})
            elif (_i.get("verdict") or None) != (r.get("verdict") or None):
                v.append({"kind": "ledger_ingest_verdict_mismatch", "item": r["item_id"],
                          "ledger": r.get("verdict"), "ingest": _i.get("verdict")})

    # ⑩ **A7-15 加固：豁免必须有批准人**（对抗测试抓到的第 4 个盲点；SPEC §5.1 G21 判据⑥）
    #    写入侧（`waive_rows`）**拒收**缺 `approved_by` 的豁免，但**读取侧（本函数）原先从不核对行里的 `waiver`**
    #    → 当同一页还有别的已判项时，"把一条已判项悄悄改成 `waived`、又拿不出谁批准"会**静默通过**
    #    （实测：判决仍是 `可交付`、违规 0 条）。"**没有批准人的豁免**"比误报更严重（A7-14 追加教训点名的形态），
    #    故与 I3「豁免只能由**用户**批准」同一口径，判**违规**（不是观测计数）。
    for r in rows:
        if r.get("state") == STATE_WAIVED and \
                not ((r.get("waiver") or {}).get("approved_by") or "").strip():
            v.append({"kind": "waived_without_approval", "item": r["item_id"]})

    kinds: dict[str, int] = {}
    for x in v:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    return {
        "violations": v, "violations_by_kind": kinds, "ok": not v,
        "measured": {"rows": len(rows), "batches": len(recs), "pending": len(pend),
                     "errata_entries": len(_errata),
                     "decided": sum(1 for r in rows if r["state"] == STATE_DECIDED),
                     "rejected": sum(1 for r in rows if r["state"] == STATE_REJECTED),
                     "waived": sum(1 for r in rows if r["state"] == STATE_WAIVED),
                     # 只观测、不判违规（产物可见性归 G2）
                     "pending_without_product_mark": len(no_mark), "examples": no_mark[:5],
                     # 只观测、不判违规（可追溯性；拦它会把"手写 verdicts"的正常书全挡住）
                     "decided_without_judge": len(no_judge), "no_judge_examples": no_judge[:5]},
    }


# ---------------------------------------------------------------- 落盘 / 读取
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def merge_ledger_state(new_rows: list[dict], old_rows: list[dict]) -> tuple[list[dict], dict]:
    """**A7-14**：台账重建时**保留既有判定状态**（纯函数）。

    **为什么必须有它**：`pipeline_run queue` 会重新生成台账，而**判定不可再生**（要人逐张看图）。
    原先它是**直接覆盖** —— 实测把 61 条已判状态清成 `pending`（劳动白费 + 下游门禁从"已判"变"未判定"）。

    规则（**保守**）：
    - 旧行 `state` ∈ {decided, waived, rejected_with_destination} → **迁移状态与全部判定载荷**，
      并 `revisions += 1`（审计上能看出"台账被重建过"）；
    - 旧行是 pending / expired / 不存在 → **用新行**（pending 重新出题本来就是重建的目的）；
    - **与旧行是否同一页/同一队列无关**：`item_id` 相同就迁移（item_id 由 页+类型+序号 构成）。
    返回 `(合并后的 rows, 报告)`；报告如实给出"保住了多少条判定"。
    """
    old_by_id = {r.get("item_id"): r for r in (old_rows or [])}
    keep_states = {STATE_DECIDED, STATE_WAIVED, STATE_REJECTED}
    payload = ("verdict", "verdict_ref", "confidence", "evidence", "transcription",
               "intended_action", "errata", "judge", "destination", "applied_in_batch", "expired",
               # **`binding` 必须一起迁移**：漏了它 → 保住的判定变成"无指纹的判定" →
               #   G21 判 `expired_counted_as_decided`、下游 G11/G8 跟着红（实测踩到过一次）。
               "binding",
               # **`waiver` 也必须迁移**：它是"谁批准、凭什么"的**审批证据**（`waive_rows` 写在这里），
               #   只保 `state=waived` 而丢掉它 → 豁免成了"无批准人的豁免"（比误报更严重）。
               "waiver")
    preserved, vanish = [], []
    new_ids = {r.get("item_id") for r in new_rows}
    for r in new_rows:
        o = old_by_id.get(r.get("item_id"))
        if not o or o.get("state") not in keep_states:
            continue
        r["state"] = o["state"]
        for k in payload:
            if o.get(k) is not None:
                r[k] = o[k]
        r["revisions"] = int(o.get("revisions") or 0) + 1
        # **不迁移旧 `batch_id`**：批次是"导航信息"，`plan_batches` 每次重建都可能给出不同切分，
        #   硬留旧批次会让"行页 vs 批页区间"不一致 → G21 报 `row_batch_page_mismatch`
        #   （实测踩到：4 条判定被留成旧 B002，而新 B002 的页区间已不同）。判定状态才是要保的东西。
        preserved.append(r.get("item_id"))
    # 防御性统计：旧台账里"已判"但新队列里已不存在的项（不该发生；发生了要报出来，不能静默丢）
    for o in (old_rows or []):
        if o.get("state") == STATE_DECIDED and o.get("item_id") not in new_ids:
            vanish.append(o.get("item_id"))
    rep = {"preserved": len(preserved), "preserved_ids": preserved[:5],
           "vanished_decided": len(vanish), "vanished_ids": vanish[:5],
           "old_rows": len(old_rows or []), "new_rows": len(new_rows)}
    return new_rows, rep


def write_ledger(dirpath: str, rows: list[dict], recs: list[dict], meta: dict) -> dict:
    os.makedirs(dirpath, exist_ok=True)
    wl = os.path.join(dirpath, WORKLIST_NAME)
    with open(wl, "w", encoding="utf-8", newline="\n") as fh:
        for r in sorted(rows, key=lambda r: (r["pdf_page"], r["item_id"])):
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    st = status(rows, recs)
    prog = {"book": meta.get("book"), "created_at_utc": meta.get("created_at_utc")
            or datetime.now(timezone.utc).isoformat(),
            "queue": meta.get("queue"), "queue_sha256": meta.get("queue_sha256"),
            "config": meta.get("config") or DEFAULT_CONFIG,
            "batches": recs, "order": order_batches(recs, rows),
            "counts": {"total": st["total"], "pending": st["pending"],
                       "decided": st["by_state"].get(STATE_DECIDED, 0),
                       "waived": st["by_state"].get(STATE_WAIVED, 0),
                       "rejected": st["by_state"].get(STATE_REJECTED, 0)},
            "worklist_sha256": _sha256(wl),
            "judge_models": sorted({(r.get("judge") or {}).get("model")
                                    for r in rows if r.get("judge")} - {None})}
    with open(os.path.join(dirpath, PROGRESS_NAME), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(prog, fh, ensure_ascii=False, indent=2)
    return prog


def load_ledger(dirpath: str) -> tuple[list[dict], dict]:
    wl, pg = os.path.join(dirpath, WORKLIST_NAME), os.path.join(dirpath, PROGRESS_NAME)
    rows = [json.loads(l) for l in open(wl, encoding="utf-8") if l.strip()] if os.path.exists(wl) else []
    prog = json.load(open(pg, encoding="utf-8")) if os.path.exists(pg) else {}
    recs = prog.get("batches") or []
    for rec in recs:                       # 优先用落盘的**真实**页清单（稀疏输入靠它）
        rec["_pages"] = list(rec.get("pages_list") or range(rec["page_from"], rec["page_to"] + 1))
    return rows, prog


def _load_jsonl(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()] if os.path.exists(path) else []


# ---------------------------------------------------------------- CLI
def _selftest_ingest_bridge() -> list:
    """A3 补线：台账 → `verdicts.ingested.*` 的重建 + 历史负载补齐（含负例）。"""
    cases = []
    # ---- backfill：只补缺、不改状态、不增 revisions、冲突就跳过
    bf_rows = [{"item_id": "A", "state": STATE_DECIDED, "verdict": "exists", "revisions": 0},
               {"item_id": "B", "state": STATE_DECIDED, "verdict": "exists", "revisions": 0,
                "confidence": 0.9, "evidence": {"what_i_see": "已有"}},
               {"item_id": "C", "state": STATE_DECIDED, "verdict": "exists", "revisions": 0},
               {"item_id": "D", "state": STATE_PENDING, "revisions": 0}]
    bf_v = [{"item_id": "A", "verdict": "exists", "confidence": 0.8, "evidence": {"what_i_see": "x"}},
            {"item_id": "B", "verdict": "exists", "confidence": 0.8, "evidence": {"what_i_see": "y"}},
            {"item_id": "C", "verdict": "no_content_diff", "confidence": 0.9,
             "evidence": {"what_i_see": "z"}},
            {"item_id": "D", "verdict": "exists", "confidence": 0.8, "evidence": {"what_i_see": "w"}}]
    out, rep = backfill_payload(bf_rows, bf_v)
    got = {r["item_id"]: r for r in out}
    cases.append(("补齐：缺负载的行补上 conf/evidence（A）",
                  rep["backfilled"] == ["A"] and got["A"].get("confidence") == 0.8, rep))
    cases.append(("补齐：已有负载的行**不覆盖**（B 仍是 0.9/已有）",
                  rep["already"] == ["B"] and got["B"]["evidence"]["what_i_see"] == "已有", rep))
    cases.append(("补齐：与台账 verdict 冲突 → **跳过**（C 不变；非 decided 的项也进 skipped）",
                  ("C" in rep["skipped"]) and got["C"].get("confidence") is None
                  and "A" not in rep["skipped"] and "D" in rep["skipped"], rep))
    cases.append(("补齐：非 decided 的行不动（D 仍 pending、无负载）",
                  got["D"]["state"] == STATE_PENDING and got["D"].get("confidence") is None, rep))
    cases.append(("补齐：**不增 revisions**（revisions 全为 0）",
                  all(r.get("revisions", 0) == 0 for r in out), [r.get("revisions") for r in out]))

    q = [{"item_id": "TR-R1-0008-01", "pdf_page": 8, "review_type": "transcribe_region",
          "bbox": [1, 2, 3, 4], "bbox_norm": [0.1, 0.2, 0.3, 0.4], "crop": "crops\\x.png",
          "span": {"operation": None}},
         {"item_id": "S3-P0002-M-ONLY-003", "pdf_page": 2, "review_type": "existence_mineru_only",
          "bbox": [1, 1, 2, 2], "bbox_norm": [0.1, 0.1, 0.2, 0.2], "crop": "crops\\y.png",
          "span": {"operation": "insert"}},
         {"item_id": "S3-P0009-M-ONLY-011", "pdf_page": 9, "review_type": "existence_mineru_only",
          "span": {"operation": "insert"}}]
    by_id = {x["item_id"]: x for x in q}
    rows = [{"item_id": "TR-R1-0008-01", "pdf_page": 8, "review_type": "transcribe_region",
             "state": STATE_DECIDED, "verdict": "transcribed", "confidence": 0.9,
             "transcription": "看起来比其他地方要深一些。",
             "evidence": {"what_i_see": "整行文字"}, "judge": {"model": "m", "mode": "session"}},
            {"item_id": "S3-P0002-M-ONLY-003", "pdf_page": 2, "state": STATE_DECIDED,
             "verdict": "exists", "confidence": 0.95, "evidence": {"what_i_see": "页脚"},
             "judge": {"model": "m", "mode": "session"}},
            {"item_id": "S3-P0009-M-ONLY-011", "pdf_page": 9, "state": STATE_DECIDED,
             "verdict": "unclear", "confidence": 0.3, "evidence": {"what_i_see": "看不清"}},
            {"item_id": "S3-P9999-XX", "pdf_page": 99, "state": STATE_PENDING, "verdict": None}]
    ing = build_ingested(rows, by_id, "k", 0.9)
    cases.append(("重建 ingest：只收 `decided` 且带 verdict 的行（pending 不进）",
                  [x["item_id"] for x in ing] == ["S3-P0002-M-ONLY-003", "S3-P0009-M-ONLY-011",
                                                  "TR-R1-0008-01"], [x["item_id"] for x in ing]))
    tr = next(x for x in ing if x["item_id"] == "TR-R1-0008-01")
    cases.append(("转写类：`action_applied=insert`（U-10 直接改正文）+ 带上 transcription 与 crop/bbox_norm",
                  tr["action_applied"] == "insert" and tr["transcription"].startswith("看起来")
                  and tr["bbox_norm"] == [0.1, 0.2, 0.3, 0.4] and tr["crop"].endswith("x.png"), tr))
    ex = next(x for x in ing if x["item_id"] == "S3-P0002-M-ONLY-003")
    cases.append(("存在性类：`decision_intended=keep_main`、`action_applied=review`（只标记）",
                  ex["decision_intended"] == "keep_main" and ex["action_applied"] == "review", ex))
    un = next(x for x in ing if x["item_id"] == "S3-P0009-M-ONLY-011")
    cases.append(("unclear：`needs_escalation=True`（低置信也进升级）",
                  un["needs_escalation"] is True and un["action_applied"] == "review", un))
    cases.append(("字段齐备：merge_verdicts 要读的键都在（item_id/verdict/bbox_norm/crop）",
                  all(k in ex for k in ("item_id", "verdict", "bbox_norm", "crop", "confidence")), sorted(ex)))
    cases.append(("负例：队列里没有的台账项 → 不崩且不产行",
                  len([x for x in ing if x["item_id"] == "S3-P9999-XX"]) == 0, None))
    cases.append(("口径表：转写=insert / 无差异=no_op / unclear=review / not_exists 无操作数=review",
                  action_for("transcribe_region", "transcribed", None, 0.9, 0.9)["action_applied"] == "insert"
                  and action_for("content_diff", "no_content_diff", "replace", 0.9, 0.9)["action_applied"] == "no_op"
                  and action_for("content_diff", "unclear", "replace", 0.9, 0.9)["action_applied"] == "review"
                  and action_for("content_diff", "not_exists", None, 0.9, 0.9)["action_applied"] == "review", None))
    cases.append(("低置信（conf < 阈值）→ needs_escalation（即使 verdict=exists）",
                  action_for("content_diff", "exists", "replace", 0.5, 0.9)["needs_escalation"] is True, None))
    cases.append(("U-15 图区：exists→insert_figure/insert；not_exists→no_op；unclear→review",
                  action_for("figure_region", "exists", "insert", 0.95, 0.9)["decision_intended"] == "insert_figure"
                  and action_for("figure_region", "exists", "insert", 0.95, 0.9)["action_applied"] == "insert"
                  and action_for("figure_region", "not_exists", "insert", 0.95, 0.9)["action_applied"] == "no_op"
                  and action_for("figure_region", "unclear", "insert", 0.95, 0.9)["action_applied"] == "review", None))
    cases.append(("U-15 图区 priority=P2",
                  priority_of({"review_type": "figure_region", "item_id": "S3-P0004-FIG-001"}) == "P2", None))
    return cases


def backfill_payload(rows: list[dict], verdicts: list[dict], force: bool = False) -> tuple[list, dict]:
    """**一次性迁移**：给 A3-4 之前就已 `decided` 的行补上判定负载（verdict/confidence/evidence/judge）。

    与 `apply_verdicts` 的区别：**这是把历史判定补齐**，不是"重判"，所以**不动 `state`、不增 `revisions`**；
    状态不冲突的行才补（`state != decided` 或 verdict 不一致 → 记进 `skipped`，让人看见）。
    `force=True`：允许覆盖**同 verdict** 的行（用于重算绑定指纹等"口径修正"，仍不动 state/revisions）。
    """
    by_id = {r["item_id"]: r for r in rows}
    rep = {"backfilled": [], "already": [], "skipped": []}
    for v in verdicts or []:
        r = by_id.get(v.get("item_id"))
        if r is None or r.get("state") != STATE_DECIDED or not v.get("verdict"):
            rep["skipped"].append(v.get("item_id"))
            continue
        if r.get("verdict") and r.get("verdict") != v["verdict"]:
            rep["skipped"].append(v.get("item_id"))          # 与台账不一致 → 不擅自改，留给人看
            continue
        if not force and (r.get("evidence") or r.get("confidence") is not None):
            rep["already"].append(r["item_id"])
            continue
        r["verdict"] = v["verdict"]
        for k in ("confidence", "evidence", "transcription", "judge"):
            if v.get(k) is not None:
                r[k] = v[k]
        rep["backfilled"].append(r["item_id"])
    return rows, rep


BLOCK_FLAG_KINDS = ("non_book_ink",)
BLOCK_FLAG_MODES = ("quarantine", "mark")
BLOCK_FLAGS = "block-flags.jsonl"


def block_flag_rows(flags: list[dict]) -> tuple[list[dict], list[str]]:
    """校验**块级标记**（2026-11-15 / P1-1）：判定者看图时对"某个块"下的直接结论。

    为什么需要它：`non_book_ink` 原本只能落在**队列里已有的题**上；而手写/印章/装饰常常
    根本没出题（S3 的 N-7 把长差异直接丢掉）→ 判词用不上。这条通道让判定者**看到什么标什么**，
    不需要出题、不动队列/台账/G21。

    硬要求：`block_id` + `kind=non_book_ink` + `basis`（说清"为什么不是书的内容"）。
    """
    out, errs = [], []
    for i, r in enumerate(flags or []):
        if not isinstance(r, dict):
            errs.append("flag:not-a-dict:%d" % i)
            continue
        bid = str(r.get("block_id") or "").strip()
        kind = str(r.get("kind") or "non_book_ink").strip()
        basis = str(r.get("basis") or "").strip()
        if not bid:
            errs.append("flag:missing-block_id:%d" % i)
        if kind not in BLOCK_FLAG_KINDS:
            errs.append("flag:bad-kind:%s" % bid)
        if basis in ("", "—"):
            errs.append("flag:missing-basis:%s" % bid)      # 必须说清"为什么不是书的内容"
        mode = str(r.get("mode") or "quarantine").strip()
        if mode not in BLOCK_FLAG_MODES:
            errs.append("flag:bad-mode:%s" % bid)
            mode = "quarantine"
        out.append({"block_id": bid, "kind": kind, "basis": basis, "mode": mode,
                    "page": r.get("page"), "judge": r.get("judge"),
                    "note": str(r.get("note") or "")})
    return out, errs


def load_block_flags(ledger_dir: str) -> list[dict]:
    path = os.path.join(ledger_dir, BLOCK_FLAGS)
    if not os.path.exists(path):
        return []
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    return block_flag_rows(rows)[0]


def write_block_flags(ledger_dir: str, flags: list[dict]) -> dict:
    os.makedirs(ledger_dir, exist_ok=True)
    path = os.path.join(ledger_dir, BLOCK_FLAGS)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in flags:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return {"out": path, "flags": len(flags)}


def set_block_flag(ledger_dir: str, block_id: str, kind: str = "non_book_ink",
                   basis: str = "", page: int | None = None, judge: dict | None = None,
                   note: str = "", reset: bool = False, mode: str = "quarantine") -> dict:
    """加/删一条块级标记（幂等：同 block_id 同 kind 覆盖）。"""
    rows = load_block_flags(ledger_dir)
    keep = [r for r in rows if not (r.get("block_id") == block_id and r.get("kind") == kind)]
    if not reset:
        keep.append({"block_id": block_id, "kind": kind, "basis": basis,
                     "mode": mode, "page": page, "judge": judge, "note": note})
    clean, errs = block_flag_rows(keep)
    if errs:
        raise ValueError("块级标记校验失败：\n  - " + "\n  - ".join(errs))
    return {**write_block_flags(ledger_dir, clean), "errors": errs}


def cmd_backfill(a) -> int:
    """把历史判定负载补进台账（**迁移用**；详见 SPEC §3.10-3 与变更记录 102）。"""
    rows, prog = load_ledger(a.ledger)
    if not rows:
        print(json.dumps({"error": "台账为空或不存在"}, ensure_ascii=False))
        return 2
    verd = []
    for p in (a.verdicts or []):
        verd.extend(_load_jsonl(p))
    rows, rep = backfill_payload(rows, verd, force=bool(getattr(a, "force", False)))
    recs = prog.get("batches") or []
    for rec in recs:
        rec.setdefault("_pages", list(range(rec["page_from"], rec["page_to"] + 1)))
    write_ledger(a.ledger, rows, recs, {"book": prog.get("book"), "queue": prog.get("queue"),
                                        "queue_sha256": prog.get("queue_sha256"),
                                        "config": prog.get("config")})
    try:
        # A7-14：把 --force 透传下去，避免"台账为空 + 目标已有内容"时被静默清空（实测踩过）
        ing = write_ingested(a.ledger, prog.get("book") or os.path.basename(os.path.abspath(a.ledger)),
                             force=bool(getattr(a, "force", False)))
    except Exception as exc:                                                    # noqa: BLE001
        ing = {"error": "%s: %s" % (type(exc).__name__, exc)}
    st = status(rows, recs)
    print(json.dumps({"ledger": os.path.abspath(a.ledger), **rep, "pending": st["pending"],
                      "decided": st["by_state"].get(STATE_DECIDED, 0),
                      "ingested": ing}, ensure_ascii=False, indent=2))
    return 0


def refresh_bindings(rows: list[dict], key: str, root: str = None) -> tuple[list, dict]:
    """按**当前**产物指纹重算每条判定的 `binding`（口径变更/补写卷规范后的**必要迁移**）。

    为什么需要：`binding.current()` 一旦从"全书指纹"切到"逐卷指纹"（例如补写了
    `ocr-raw/volumes-<书>.json`），历史判定里的旧指纹立刻全部 `expired`——**判定本身没变，
    变的只是它绑定的产物版本口径**。G21 会如实报 `expired_counted_as_decided`（实测踩到）。
    本函数只重算指纹，**不动 verdict/state/revisions**。
    """
    import binding as _binding
    root = root or ROOT
    rep = {"refreshed": [], "unchanged": []}
    for r in rows:
        if r.get("state") != STATE_DECIDED:
            continue
        cur = _binding.current(key, root, r.get("pdf_page"))
        if r.get("binding") == cur:
            rep["unchanged"].append(r["item_id"])
            continue
        r["binding"] = cur
        rep["refreshed"].append(r["item_id"])
    return rows, rep


def cmd_flag_block(a) -> int:
    try:
        r = _flag_block_do(a)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    rows = load_block_flags(a.ledger)
    out = {"ledger": os.path.abspath(a.ledger), **r, "all": rows}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("（块级标记只影响**净版/留底版该块的输出**与 errata 登记；不动队列、台账与 G21）")
    return 0


def _flag_block_do(a) -> dict:
    if a.reset:
        r = set_block_flag(a.ledger, a.block_id, a.kind, reset=True)
    else:
        r = set_block_flag(a.ledger, a.block_id, a.kind, basis=a.basis, mode=a.mode,
                           page=(int(a.page) if a.page else None),
                           judge={"model": a.model or "session", "mode": "agent"},
                           note=a.note or "")
    return r


def cmd_refresh_bindings(a) -> int:
    """把台账里已判定的绑定指纹**重算**到当前产物（迁移用；见 SPEC §3.10-3）。"""
    rows, prog = load_ledger(a.ledger)
    if not rows:
        print(json.dumps({"error": "台账为空或不存在"}, ensure_ascii=False))
        return 2
    key = a.book or prog.get("book") or os.path.basename(os.path.abspath(a.ledger))
    rows, rep = refresh_bindings(rows, key)
    recs = prog.get("batches") or []
    for rec in recs:
        rec.setdefault("_pages", list(range(rec["page_from"], rec["page_to"] + 1)))
    write_ledger(a.ledger, rows, recs, {"book": prog.get("book"), "queue": prog.get("queue"),
                                        "queue_sha256": prog.get("queue_sha256"),
                                        "config": prog.get("config")})
    try:
        ing = write_ingested(a.ledger, key)
    except Exception as exc:                                                    # noqa: BLE001
        ing = {"error": "%s: %s" % (type(exc).__name__, exc)}
    print(json.dumps({"ledger": os.path.abspath(a.ledger), "key": key,
                      "refreshed": len(rep["refreshed"]), "unchanged": len(rep["unchanged"]),
                      "ingested": {"items": ing.get("items"), "out": ing.get("out")}},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_plan(a) -> int:
    cfg = dict(DEFAULT_CONFIG)
    if a.config and os.path.exists(a.config):
        cfg.update(json.load(open(a.config, encoding="utf-8")))
    if a.pages_per_batch:
        cfg["pages_per_batch"] = a.pages_per_batch
    if a.items_per_batch:
        cfg["items_per_batch"] = a.items_per_batch
    items = _load_jsonl(a.queue)
    gate_by_id = {}
    gate_dir = a.gate_dir or (os.path.join(a.out, "gate") if a.out else "")
    if gate_dir and os.path.isdir(gate_dir):
        for g in _load_jsonl(os.path.join(gate_dir, "gate-all.jsonl")):
            gate_by_id[g.get("item_id")] = g
    rows = build_rows(items, gate_by_id)
    batches = plan_batches(rows, cfg["pages_per_batch"], cfg["items_per_batch"])
    rows = assign_batches(rows, batches)
    recs = batch_records(batches, rows)
    for rec, b in zip(recs, batches):
        rec["_pages"] = list(b["pages"])
    st = status(rows, recs)
    out = {"queue": os.path.abspath(a.queue), "items": len(items),
           "admitted_pending": st["pending"],
           "rejected": st["by_state"].get(STATE_REJECTED, 0),
           "batches": len(recs), "config": cfg, "batch_order": st["batch_order"],
           "note": "`--dry-run` 只算不落盘"}
    if not a.dry_run and a.out:
        prog = write_ledger(a.out, rows, recs, {
            "book": a.book or os.path.basename(os.path.abspath(a.out)),
            "queue": os.path.abspath(a.queue),
            "queue_sha256": _sha256(a.queue) if os.path.exists(a.queue) else None,
            "config": cfg})
        out["ledger"] = {"worklist": os.path.join(a.out, WORKLIST_NAME),
                         "progress": os.path.join(a.out, PROGRESS_NAME),
                         "worklist_sha256": prog["worklist_sha256"]}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(a) -> int:
    rows, prog = load_ledger(a.ledger)
    if not rows:
        print(json.dumps({"error": "台账为空或不存在", "ledger": a.ledger}, ensure_ascii=False))
        return 2
    verd = _load_jsonl(a.verdicts)
    exp = [x for x in (a.expired_ids or "").split(",") if x]
    rows, rep = apply_verdicts(rows, verd, expired_ids=exp, batch_id=a.batch,
                               check_only=bool(getattr(a, "check_only", False)))
    if getattr(a, "check_only", False):
        # **只校验、不写**：给 agent 提交前预检（把错误挡在落盘之前）
        out = {"check_only": True, "ledger": os.path.abspath(a.ledger),
               "verdicts": os.path.abspath(a.verdicts), **rep}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if rep["unknown_verdict"] or rep["unknown"]:
            _print_rejects(rep)
            return 3
        print("预检通过：%d 条判词与条目都合法（还没写台账）" % len(verd or []))
        return 0
    recs = prog.get("batches") or []
    for rec in recs:
        rec.setdefault("_pages", list(range(rec["page_from"], rec["page_to"] + 1)))
    write_ledger(a.ledger, rows, recs, {"book": prog.get("book"), "queue": prog.get("queue"),
                                        "queue_sha256": prog.get("queue_sha256"),
                                        "config": prog.get("config")})
    # **A3 补线**：写台账之后**重建**下游要读的 ingest 文件（否则门禁看不到这些判定）
    try:
        ing = write_ingested(a.ledger, prog.get("book") or os.path.basename(os.path.abspath(a.ledger)))
    except Exception as exc:                                                    # noqa: BLE001
        ing = {"error": "%s: %s" % (type(exc).__name__, exc)}
    # **A7-13**：同时重建 `errata.jsonl`（U 类：原文如此、正文不改）。判定里没登记就是空文件——
    # 空文件是**正确结果**，不是缺陷（原先连"产出方"都没有，所以必然是空的）。
    try:
        erb = write_errata(a.ledger)
    except Exception as exc:                                                    # noqa: BLE001
        erb = {"error": "%s: %s" % (type(exc).__name__, exc)}
    st = status(rows, recs)
    out = {"ledger": os.path.abspath(a.ledger), "verdicts": os.path.abspath(a.verdicts),
           **rep, "pending_after": st["pending"], "p0_pending_after": st["p0_pending"],
           "next_batch": st["next_batch"], "ingested": ing, "errata": erb}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if rep["unknown_verdict"] or rep["unknown"]:
        _print_rejects(rep)
        return 3                     # 部分被拒：**其余已写入**，但要让人/脚本知道还差哪条
    return 0


def _print_rejects(rep: dict) -> None:
    """被拒条目的人话提示（**不猜用户想写哪个词**，只列条目与合法词表）。"""
    uv = rep.get("unknown_verdict") or []
    un = rep.get("unknown") or []
    if uv:
        print("\n被拒 %d 条（判定词不在词表里，已退回待办）：" % len(uv))
        for x in uv:
            print("  - %s  写的是 %r" % (x["item_id"], x["verdict"]))
        print("  允许的判定词：%s" % " / ".join(uv[0].get("allowed") or []))
        print("  补判后重新 apply 即可（不用重跑引擎/管线；改的是正文内容时才需重跑 pipeline_run run）。")
    if un:
        print("\n有 %d 条 item_id 不在台账里（跳过）：%s" % (len(un), ", ".join(str(x) for x in un)))


def cmd_status(a) -> int:
    rows, prog = load_ledger(a.ledger)
    recs = prog.get("batches") or []
    for rec in recs:
        rec.setdefault("_pages", list(range(rec["page_from"], rec["page_to"] + 1)))
    st = status(rows, recs)
    st.pop("pending_ids", None)                       # 太长，用 --ids 显式要
    if a.ids:
        st["pending_ids"] = sorted(r["item_id"] for r in rows if r["state"] == STATE_PENDING)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_check(a) -> int:
    rows, prog = load_ledger(a.ledger)
    recs = prog.get("batches") or []
    for rec in recs:
        rec.setdefault("_pages", list(range(rec["page_from"], rec["page_to"] + 1)))
    qids = None
    if a.queue and os.path.exists(a.queue):
        qids = [x.get("item_id") for x in _load_jsonl(a.queue)]
    res = check_ledger(rows, recs, queue_ids=qids,
                       product_marks=(mark_categories_by_page(open(a.md, encoding="utf-8").read())
                                      if a.md and os.path.exists(a.md) else None),
                       expired_ids=[x for x in (a.expired_ids or "").split(",") if x],
                       report_pending=None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- selftest
def _row(iid, page, rt="content_diff", rule=None, risk=None, priority=None):
    it = {"item_id": iid, "pdf_page": page, "review_type": rt}
    if rule:
        it["bbox_provenance"] = {"rule": rule}
    if risk:
        it["risk"] = {"signals": risk}
    return it


def _selftest_errata() -> list:
    """**A7-13**：U 类（原文如此、正文不改）条目的登记与校验（正例 + 负例）。"""
    cases = []
    base = {"item_id": "S3-P0009-CONTENT-001", "pdf_page": 9, "state": STATE_DECIDED,
            "verdict": "exists", "confidence": 0.9, "judge": {"model": "m", "mode": "session"}}

    # 正例：带 block_id + original → 抽出成 errata 行，且无违规
    r1 = dict(base, errata={"block_id": "mineru-p0009-b003-text",
                            "original": "Horstmam", "suggestion": "疑为 Horstmann",
                            "basis": "原页确印如此（300dpi 裁图）"})
    rows, errs = errata_rows([r1])
    cases.append(("A7-13 正例：U 类条目被抽出（含 block_id/original/页码/置信度），无违规",
                  len(rows) == 1 and not errs
                  and rows[0]["block_id"] == "mineru-p0009-b003-text"
                  and rows[0]["original"] == "Horstmam" and rows[0]["page"] == 9
                  and rows[0]["confidence"] == 0.9, {"rows": rows, "errs": errs}))

    # 负例①：没写原页字面（只写"感觉原书错了"）→ 违规
    r2 = dict(base, errata={"block_id": "mineru-p0009-b003-text"})
    _rows2, errs2 = errata_rows([r2])
    cases.append(("A7-13 负例①：U 类缺 `original`（写不出原页字面）→ 登记为违规",
                  any(e.startswith("errata:missing-original") for e in errs2), errs2))

    # 负例②：没绑 block_id → 违规（绑不到块就无法对账"正文有没有被改"）
    r3 = dict(base, errata={"original": "Horstmam"})
    _rows3, errs3 = errata_rows([r3])
    cases.append(("A7-13 负例②：U 类缺 `block_id` → 登记为违规",
                  any(e.startswith("errata:missing-block_id") for e in errs3), errs3))

    # 负例③：违规必须真的进 `check_ledger` 的 violations（不能只在自己函数里报）
    rec = {"batch_id": "B001", "page_from": 9, "page_to": 9}
    chk = check_ledger([r3], [rec])
    cases.append(("A7-13 负例③：U 类缺 block_id 会进 `check_ledger` 的 violations（G21 拦得住）",
                  any(x["kind"] == "errata:missing-block_id" for x in chk["violations"]),
                  chk["violations_by_kind"]))

    # 正例②：没有 errata 字段的普通判定 → 不产生任何 errata 行（不得凭空造条目）
    _rows4, errs4 = errata_rows([dict(base), dict(base, item_id="S3-P0010-CONTENT-002")])
    cases.append(("A7-13 正例②：普通判定（无 errata 字段）→ 0 条 errata、0 违规",
                  _rows4 == [] and errs4 == [], {"rows": _rows4, "errs": errs4}))

    # ---- 2026-10-26：两类 kind（source_typo / non_book_ink） ----
    cases.append(("2026-10-26：老条目没有 `kind` → 默认 `source_typo`（向后兼容）",
                  rows[0]["kind"] == "source_typo", rows[0]))
    r5 = dict(base, item_id="S3-P0012-CONTENT-009", pdf_page=12,
              verdict="non_book_ink",
              errata={"kind": "non_book_ink", "block_id": "mineru-p0012-b118-text",
                      "original": "(1) 以电炉丝为系统；W>0, Q<0, OU=0",
                      "basis": "红笔手写答案（页面右下角红色笔迹，非印刷体）"})
    rows5, errs5 = errata_rows([r5])
    cases.append(("2026-10-26 正例：`non_book_ink` 条目被抽出、kind 保留、无违规",
                  len(rows5) == 1 and not errs5 and rows5[0]["kind"] == "non_book_ink"
                  and rows5[0]["page"] == 12, {"rows": rows5, "errs": errs5}))
    r6 = dict(base, errata={"kind": "non_book_ink", "block_id": "b1", "original": "x"})
    _rows6, errs6 = errata_rows([r6])
    cases.append(("2026-10-26 负例①：`non_book_ink` 没写 `basis`（说清为什么不是书的内容）→ 违规",
                  any(e.startswith("errata:missing-basis") for e in errs6), errs6))
    r7 = dict(base, errata={"kind": "typo", "block_id": "b1", "original": "x"})
    _rows7, errs7 = errata_rows([r7])
    cases.append(("2026-10-26 负例②：`kind` 不在枚举里 → 违规且回退 source_typo（不静默吞）",
                  any(e.startswith("errata:bad-kind") for e in errs7)
                  and _rows7 and _rows7[0]["kind"] == "source_typo", {"rows": _rows7, "errs": errs7}))
    _a5 = action_for("content_diff", "non_book_ink", None, 0.9, 0.9)
    cases.append(("2026-10-26：`non_book_ink` → quarantine/review（**不改写、不删除**）",
                  _a5["decision_intended"] == "quarantine" and _a5["action_applied"] == "review", _a5))

    # ---- 2026-11-15：第三类 `missing_text`（主引擎漏、页面有；锚点必须齐） ----
    _mt = {"kind": "missing_text", "missing_text": "[答案: −3.057×10³ J; −3.101×10³ J]",
           "anchor": {"before": "12. 试计算下列过程的功。", "after": "13. 某理想气体 1 mol",
                      "position": "own_line", "confidence": "high"},
           "basis": "MinerU 的 content_list 里没有这段；见证引擎有；该处有墨迹"}
    r8 = dict(base, item_id="MT-P0002-01", pdf_page=2, errata=_mt)
    rows8, errs8 = errata_rows([r8])
    cases.append(("2026-11-15 正例：`missing_text` 带齐 `missing_text`+前文/后文锚点 → 抽出且无违规",
                  len(rows8) == 1 and not errs8 and rows8[0]["kind"] == "missing_text"
                  and rows8[0]["anchor"]["position"] == "own_line"
                  and rows8[0]["anchor"]["confidence"] == "high"
                  and rows8[0]["missing_text"].startswith("[答案"),
                  {"rows": rows8, "errs": errs8}))
    _mt2 = dict(_mt, anchor={"before": "", "after": "13. 某理想气体", "position": "inline"})
    _r9, errs9 = errata_rows([dict(base, errata=_mt2)])
    cases.append(("2026-11-15 负例①：锚点缺一半（插不回去）→ `errata:missing-anchor`",
                  any(e.startswith("errata:missing-anchor") for e in errs9), errs9))
    _mt3 = dict(_mt, anchor={"before": "A。", "after": "B。", "position": "middle",
                             "confidence": "maybe"})
    _r10, errs10 = errata_rows([dict(base, errata=_mt3)])
    cases.append(("2026-11-15 负例②：`position`/`confidence` 非法 → 违规并回退 own_line/low（不静默）",
                  any(e.startswith("errata:bad-anchor-position") for e in errs10)
                  and any(e.startswith("errata:bad-anchor-confidence") for e in errs10)
                  and _r10 and _r10[0]["anchor"]["position"] == "own_line"
                  and _r10[0]["anchor"]["confidence"] == "low",
                  {"rows": _r10, "errs": errs10}))
    _mt4 = dict(_mt, basis="")
    _r11, errs11 = errata_rows([dict(base, errata=_mt4)])
    cases.append(("2026-11-15 负例③：`missing_text` 不写 `basis`（凭什么说页面有、产物没有）→ 违规",
                  any(e.startswith("errata:missing-basis") for e in errs11), errs11))
    # **穿透**：`merge_verdicts` 只读 ingest，所以 `errata` 必须过 `build_ingested` 这一关
    _ing = build_ingested([dict(base, item_id="MT-P0002-01", verdict="exists", errata=_mt)],
                          {}, "fx", 0.9)
    cases.append(("2026-11-15：`errata` 必须**穿透到 ingest**（否则 S7 看不到 missing_text、页末不留痕）",
                  bool(_ing) and (_ing[0].get("errata") or {}).get("kind") == "missing_text",
                  _ing[0] if _ing else None))
    return cases


def _selftest_block_flags() -> list:
    """**2026-11-15（P1-1）**：块级标记（判定者看 graphic 时直接对手写/印章下结论）。"""
    cases = []
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        r = set_block_flag(td, "mineru-p0005-b074-text", "non_book_ink",
                           basis="红笔手写批注（非印刷体）", page=5,
                           judge={"model": "m", "mode": "agent"})
        rows = load_block_flags(td)
        cases.append(("2026-11-15（P1-1）正例：块级 `non_book_ink` 落盘并可读回（带 basis/page/judge）",
                      r["flags"] == 1 and rows and rows[0]["block_id"] == "mineru-p0005-b074-text"
                      and rows[0]["page"] == 5 and rows[0]["judge"]["model"] == "m", rows))
        try:
            set_block_flag(td, "b-x", "non_book_ink", basis="")
            cases.append(("2026-11-15（P1-1）负例：不写 basis → 报错", False, None))
        except Exception as exc:                                    # noqa: BLE001
            cases.append(("2026-11-15（P1-1）负例：不写 basis（凭什么不是书的内容）→ 报错",
                          "basis" in str(exc), str(exc)[:60]))
        try:
            set_block_flag(td, "b-y", "handwriting", basis="x")
            cases.append(("2026-11-15（P1-1）负例：未知 kind → 报错", False, None))
        except Exception as exc:                                    # noqa: BLE001
            cases.append(("2026-11-15（P1-1）负例：未知 kind → 报错", "kind" in str(exc), None))
        r2 = set_block_flag(td, "mineru-p0005-b074-text", "non_book_ink", reset=True)
        cases.append(("2026-11-15（P1-1）：`--reset` 删除该条（幂等）",
                      r2["flags"] == 0 and load_block_flags(td) == [], r2))
        cases.append(("2026-11-15（P1-1）：没有文件时读回空表（降级，不崩）",
                      load_block_flags(os.path.join(td, "nope")) == [], None))
    return cases


def _selftest_verdict_vocab() -> list:
    """**2026-10-26**：判词合法性 —— 一个手滑不该让整批白写，也不该"假装判过了"。"""
    cases = []
    base = [{"item_id": "S3-P0001-CONTENT-001", "pdf_page": 1, "state": STATE_PENDING,
             "review_type": "content_diff", "batch_id": "B001"},
            {"item_id": "S3-P0002-CONTENT-002", "pdf_page": 2, "state": STATE_PENDING,
             "review_type": "content_diff", "batch_id": "B001"}]
    _j = {"model": "m", "mode": "agent"}
    rows, rep = apply_verdicts([dict(r) for r in base],
                               [{"item_id": "S3-P0001-CONTENT-001", "verdict": "exists", "judge": _j},
                                {"item_id": "S3-P0002-CONTENT-002", "verdict": "exist", "judge": _j}],
                               batch_id="B001")
    _by = {r["item_id"]: r for r in rows}
    cases.append(("2026-10-26：一条判词写错 → **只拒那一条**（它仍 pending），其余照常 decided",
                  _by["S3-P0001-CONTENT-001"]["state"] == STATE_DECIDED
                  and _by["S3-P0002-CONTENT-002"]["state"] == STATE_PENDING
                  and len(rep["unknown_verdict"]) == 1
                  and rep["unknown_verdict"][0]["item_id"] == "S3-P0002-CONTENT-002",
                  rep["unknown_verdict"]))
    cases.append(("2026-10-26：被拒条目**不被写进 verdict**（不许把错词落盘）",
                  _by["S3-P0002-CONTENT-002"].get("verdict") is None, _by["S3-P0002-CONTENT-002"]))
    cases.append(("2026-10-26：`check_only=True` → **一条都不改**（提交前预检）",
                  all(r["state"] == STATE_PENDING
                      for r in apply_verdicts([dict(r) for r in base],
                                              [{"item_id": "S3-P0001-CONTENT-001",
                                                "verdict": "exists", "judge": _j}],
                                              batch_id="B001", check_only=True)[0]), None))
    # 台账里混进未知词（手改）→ check_ledger 必须报（G21 兜底）
    _bad = [dict(base[0], state=STATE_DECIDED, verdict="exist", judge=_j)]
    _chk = check_ledger(_bad, [{"batch_id": "B001", "page_from": 1, "page_to": 1}])
    cases.append(("2026-10-26：台账里已 decided 但 verdict 不在词表 → `verdict_unknown` 违规",
                  any(x["kind"] == "verdict_unknown" for x in _chk["violations"]),
                  _chk["violations_by_kind"]))
    return cases


def _selftest_merge_state() -> list:
    """**A7-14**：台账重建必须保住判定（正例 + 负例）。

    这是**数据丢失级**缺陷的回归锁：原先 `queue` 直接覆盖台账，实测把 61 条 decided 清成 pending。
    """
    cases = []

    def _new(iid, page=1):
        return {"item_id": iid, "pdf_page": page, "review_type": "content_diff",
                "state": STATE_PENDING, "batch_id": "B999", "priority": "P3", "revisions": 0}

    old = [
        {"item_id": "A", "state": STATE_DECIDED, "pdf_page": 1, "review_type": "content_diff",
         "batch_id": "B001", "revisions": 0, "verdict": "exists", "confidence": 0.91,
         "evidence": {"what_i_see": "看到内容"}, "judge": {"model": "m", "mode": "session"},
         "transcription": "某段字", "binding": {"ir": "deadbeef", "volume": "v01"}},
        {"item_id": "B", "state": STATE_REJECTED, "pdf_page": 2, "review_type": "ownership",
         "destination": "notation_registry", "revisions": 0},
        {"item_id": "C", "state": STATE_PENDING, "pdf_page": 3, "review_type": "content_diff",
         "revisions": 0},
    ]
    new = [_new("A", 1), _new("B", 2), _new("C", 3)]
    rows, rep = merge_ledger_state(new, old)
    got = {r["item_id"]: r for r in rows}
    cases.append(("A7-14 正例①：重建后台账**保住** decided 状态与全部判定载荷（verdict/confidence/证据/判定者/转写）",
                  got["A"]["state"] == STATE_DECIDED and got["A"]["verdict"] == "exists"
                  and got["A"]["confidence"] == 0.91
                  and (got["A"]["evidence"] or {}).get("what_i_see") == "看到内容"
                  and (got["A"]["judge"] or {}).get("model") == "m"
                  and got["A"]["transcription"] == "某段字", rep))
    cases.append(("A7-14 正例②：拒收项（有去处）也保住，且 pending 项按新队列重出",
                  got["B"]["state"] == STATE_REJECTED
                  and got["B"]["destination"] == "notation_registry"
                  and got["C"]["state"] == STATE_PENDING, rep))
    cases.append(("A7-14 正例③：保住的判定 `revisions+1`（审计上能看出'台账被重建过'）",
                  got["A"]["revisions"] == 1, rep))
    cases.append(("A7-14 正例⑦：**批次按新计划走**（不硬留旧 `batch_id` —— 留了会让"
                  "`row_batch_page_mismatch` 报红，实测踩到）",
                  got["A"]["batch_id"] == "B999", got["A"].get("batch_id")))
    cases.append(("A7-14 正例④：报告如实给出'保住了几条'",
                  rep["preserved"] == 2 and rep["vanished_decided"] == 0, rep))
    cases.append(("A7-14 正例⑥：**`binding` 指纹必须一起迁移**（漏了它 → 保住的判定会被判过期，"
                  "G21/G11/G8 连锁红 —— 实测踩到过一次）",
                  got["A"].get("binding") == {"ir": "deadbeef", "volume": "v01"}, got["A"].get("binding")))

    # 负例①：**不得**把 pending 也当成"保住"（那会让重建失去意义）
    rows2, rep2 = merge_ledger_state([_new("C", 3)], old)
    cases.append(("A7-14 负例①：旧行是 pending → **不迁移**（重建就是为了重新出题）",
                  rows2[0]["state"] == STATE_PENDING and rep2["preserved"] == 0, rep2))

    # 正例⑧：**用户豁免（waived）必须连"审批证据"一起迁移** —— 豁免是"经用户批准的决定"，同样不可再生，
    #   只保 state 而丢 `waiver{approved_by, reason}` = 造出"无批准人的豁免"（本轮自查抓到自己漏了这一项）
    old_w = [dict(old[0], item_id="W", state=STATE_WAIVED, verdict=None,
                  waiver={"reason": "按证据批准", "approved_by": "用户"}, binding={"ir": "x"})]
    rows_w, rep_w = merge_ledger_state([_new("W", 1)], old_w)
    cases.append(("A7-14 正例⑧：`waived` 连 `waiver{approved_by, reason}` 一起迁移"
                  "（不得变成「无批准人的豁免」）",
                  rows_w[0]["state"] == STATE_WAIVED
                  and (rows_w[0].get("waiver") or {}).get("approved_by") == "用户"
                  and (rows_w[0].get("waiver") or {}).get("reason") == "按证据批准"
                  and rep_w["preserved"] == 1, {"state": rows_w[0].get("state"),
                                                "waiver": rows_w[0].get("waiver")}))

    # 负例②：台账里"已判(decided)"的项在新队列里消失 → 必须报出来（不能静默丢）
    #   注意：只统计 **decided**（rejected 可被重新出题/重新拒收，不算"判定劳动丢失"）
    _rows3, rep3 = merge_ledger_state([_new("C", 3)], old)
    cases.append(("A7-14 负例②：旧台账里**已判**但新队列没有的项 → 计入 `vanished_decided`（不静默丢）",
                  rep3["vanished_decided"] == 1 and rep3["vanished_ids"] == ["A"], rep3))

    # 负例③：ingest 保护 —— 台账 0 条判定时**不得覆盖**已有内容的 ingest 文件
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="a714-")
    _run = os.path.join(_d, "runs", "c3-ledger")
    os.makedirs(_run, exist_ok=True)
    _p = os.path.join(_run, "verdicts.ingested.c3.jsonl")
    with open(_p, "w", encoding="utf-8", newline="\n") as _fh:
        _fh.write(json.dumps({"item_id": "A", "verdict": "exists"}, ensure_ascii=False) + "\n")
    _before = open(_p, encoding="utf-8").read()
    _r = write_ingested(_d, "fxbook")
    _after = open(_p, encoding="utf-8").read()
    cases.append(("A7-14 负例③：台账 0 条判定时 `write_ingested` **拒绝覆盖**已有 ingest（实测踩过的二次损害）",
                  bool(_r.get("refused")) and _after == _before, {"refused": _r.get("refused")}))
    # 正例③：显式 force → 允许清空（给"确需重置"留口子）
    _r2 = write_ingested(_d, "fxbook", force=True)
    cases.append(("A7-14 正例⑤：显式 `force=True` 才允许把它清成 0 条",
                  not _r2.get("refused") and os.path.getsize(_p) == 0, _r2))
    return cases


def selftest() -> int:
    cases = []

    # ---- 优先级映射
    _pri = {"P0_R1_transcribe": priority_of(_row("TR-R1-0002-01", 2, "transcribe_region", "R1")),
            "P1_R4a_transcribe": priority_of(_row("TR-R4a-0003-02", 3, "transcribe_region", "R4a")),
            "P1_digit_risk": priority_of(_row("S3-P1-C-1", 1, "content_diff", risk=["digit"])),
            "P2_structure": priority_of(_row("S3-P1-S-2", 1, "structure_diff")),
            "P2_ownership": priority_of(_row("S3-OWN-3", 1, "ownership")),
            "P3_plain": priority_of(_row("S3-P1-C-4", 1, "content_diff"))}
    cases.append(("优先级映射：R1→P0 / R4a·数字→P1 / 结构·归属→P2 / 其余→P3",
                  _pri == {"P0_R1_transcribe": "P0", "P1_R4a_transcribe": "P1",
                           "P1_digit_risk": "P1", "P2_structure": "P2",
                           "P2_ownership": "P2", "P3_plain": "P3"}, _pri))

    # ---- 合成 428 页队列（≈1.4 条/页）+ 队列外的一个拒收项
    items = []
    for p in range(1, 429):
        items.append(_row(f"S3-P{p:04d}-C-1", p, "content_diff"))
        if p % 2 == 0:
            items.append(_row(f"S3-P{p:04d}-C-2", p, "content_diff", risk=["unit"]))
        if p % 5 == 0:
            items.append(_row(f"S3-P{p:04d}-T-3", p, "structure_diff"))
        if p % 90 == 0:
            items.append(_row(f"TR-R1-{p:04d}-01", p, "transcribe_region", "R1"))
    rows = build_rows(items)
    cfg = DEFAULT_CONFIG
    batches = plan_batches(rows, cfg["pages_per_batch"], cfg["items_per_batch"])
    rows = assign_batches(rows, batches)
    recs = batch_records(batches, rows)
    for rec, b in zip(recs, batches):
        rec["_pages"] = list(b["pages"])
    st = status(rows, recs)

    cases.append((f"428 页全部进台账（{len(items)} 项）", len(rows) == len(items), len(rows)))
    cases.append(("分批生效（批数 >1，且每批 ≤40 页）",
                  len(recs) > 1 and all(r["pages"] <= cfg["pages_per_batch"] for r in recs),
                  [r["pages"] for r in recs[:5]]))
    cases.append(("每批 ≤39 项（或单页本身就超限）",
                  all(r["item_count"] <= cfg["items_per_batch"]
                      or r["pages"] == 1 for r in recs), max(r["item_count"] for r in recs)))
    # 同一页不拆批
    page_of_batch = {}
    bad_split = []
    for b in batches:
        for it in b["items"]:
            if it["pdf_page"] in page_of_batch and page_of_batch[it["pdf_page"]] != b["pages"]:
                bad_split.append(it["pdf_page"])
            page_of_batch[it["pdf_page"]] = b["pages"]
    cases.append(("**同一页不拆批**", not bad_split, bad_split[:5]))
    # 批页连续、批间不重叠、并集 == 全部页
    union = set()
    overlap = []
    for b in batches:
        ps = b["pages"]
        if ps != list(range(min(ps), max(ps) + 1)):
            overlap.append(("not_contiguous", ps[:5]))
        for p in ps:
            if p in union:
                overlap.append(("overlap", p))
            union.add(p)
    cases.append(("批内页连续 + 批间不重叠", not overlap, overlap[:5]))
    cases.append(("批页并集 == 全部页", union == set(range(1, 429)), len(union)))
    cases.append(("批编号按页序且唯一",
                  [r["batch_id"] for r in recs] == [f"B{i:03d}" for i in range(1, len(recs) + 1)],
                  [r["batch_id"] for r in recs[:3]]))
    cases.append(("推荐顺序：含 P0 的批优先",
                  st["batch_order"][0] == next(r["batch_id"] for r in recs
                                               if r["by_priority"]["P0"]), st["batch_order"][:4]))
    cases.append(("P0 项数 = 4（428/90 向下）",
                  st["by_priority"]["P0"]["total"] == 4, st["by_priority"]["P0"]))

    # ---- 幂等 / 续跑 / 过期 / 未知
    rows0 = [dict(r) for r in rows]
    recs0 = [dict(r) for r in recs]
    b1 = st["batch_order"][0]
    ids1 = [r["item_id"] for r in rows0 if r["batch_id"] == b1]
    verd = [{"item_id": i, "verdict": "exists", "judge": {"model": "m", "mode": "agent"}} for i in ids1]
    rows1, rep1 = apply_verdicts(rows0, verd, batch_id=b1)
    cases.append(("apply 首批：全部计入 applied", len(rep1["applied"]) == len(ids1), rep1))
    rows2, rep2 = apply_verdicts(rows1, verd, batch_id=b1)
    cases.append(("幂等：重复 apply 不翻状态、只记 revisions",
                  len(rep2["applied"]) == 0 and len(rep2["reapplied"]) == len(ids1)
                  and all(r["state"] == STATE_DECIDED for r in rows2 if r["item_id"] in ids1), rep2))
    st2 = status(rows2, recs0)
    cases.append(("续跑：pending 单调减、next_batch 前进",
                  st2["pending"] == st["pending"] - len(ids1) and st2["next_batch"] != b1,
                  {"before": st["pending"], "after": st2["pending"], "next": st2["next_batch"]}))
    rows3, rep3 = apply_verdicts(rows2, verd, expired_ids=ids1[:1], batch_id=b1)
    cases.append(("源变更 → 判定作废回 pending",
                  rep3["expired"] == ids1[:1]
                  and next(r for r in rows3 if r["item_id"] == ids1[0])["state"] == STATE_PENDING, rep3))
    rows4, rep4 = apply_verdicts(rows3, [{"item_id": "NOPE-1", "verdict": "exists"}], batch_id=b1)
    cases.append(("未知 item 被记下来（不静默）", rep4["unknown"] == ["NOPE-1"], rep4))
    rows5, rep5 = apply_verdicts(rows4, [{"item_id": rows4[0]["item_id"], "verdict": "exists"}],
                                 batch_id="B999")
    cases.append(("判了别的批的项 → 如实记 not_in_batch", rep5["not_in_batch"] == [rows4[0]["item_id"]], rep5))

    # ---- 拒收项进台账 + 去处
    items2 = items + [_row("S3-REJ-1", 7, "content_diff")]
    gate = {"S3-REJ-1": {"item_id": "S3-REJ-1", "admitted": False,
                         "reject_reasons": ["N-7_alignment_artifact"],
                         "destination": "recrop_and_requery"}}
    rows_r = build_rows(items2, gate)
    cases.append(("拒收项也进台账且带去处",
                  next(r for r in rows_r if r["item_id"] == "S3-REJ-1")["state"] == STATE_REJECTED
                  and next(r for r in rows_r if r["item_id"] == "S3-REJ-1")["destination"] == "recrop_and_requery",
                  None))
    cases.append(("拒收项不进批（不占用判定配额）",
                  all(r["item_id"] != "S3-REJ-1" for b in plan_batches(rows_r) for r in b["items"]), None))

    # ---- G21 五条判据：干净用例 0 违规；每种错各自被抓住
    rows_c = [dict(r) for r in rows_r]
    batches_c = plan_batches(rows_c)
    rows_c = assign_batches(rows_c, batches_c)
    recs_c = batch_records(batches_c, rows_c)
    for rec, b in zip(recs_c, batches_c):
        rec["_pages"] = list(b["pages"])
    vids = [r["item_id"] for r in rows_c if r["batch_id"] == "B001"]
    rows_c, _ = apply_verdicts(rows_c, [{"item_id": i, "verdict": "exists",
                                         "judge": {"model": "m", "mode": "agent"}} for i in vids],
                               batch_id="B001")
    qids = [r["item_id"] for r in rows_c]
    pend_ids = [r["item_id"] for r in rows_c if r["state"] == STATE_PENDING]
    # 产物痕迹按**类别级**造（与 G2 同一约定；按 item_id 造是错的——真书实测过）
    def _marks(rs):
        out = {}
        for r in rs:
            if r["state"] == STATE_PENDING:
                out.setdefault(r["pdf_page"], set()).add(expected_marks(r["review_type"], r["item_id"])[0])
        return out
    marks = _marks(rows_c)
    pend_ids = [r["item_id"] for r in rows_c if r["state"] == STATE_PENDING]
    clean = check_ledger(rows_c, recs_c, queue_ids=qids, product_marks=marks,
                         expired_ids=[], report_pending=pend_ids)
    cases.append(("G21 干净用例 0 违规", clean["ok"] and not clean["violations"], clean["violations_by_kind"]))

    # ② 丢项
    bad1 = check_ledger(rows_c, recs_c, queue_ids=qids + ["GHOST-1"], product_marks=marks,
                        report_pending=pend_ids)
    cases.append(("G21 抓住「队列里有、台账里没有」",
                  "ledger_item_missing" in bad1["violations_by_kind"], bad1["violations_by_kind"]))
    cases.append(("G21 台账/队列**集合不一致**要有汇总判据（不能只有逐项）",
                  "ledger_queue_mismatch" in bad1["violations_by_kind"], bad1["violations_by_kind"]))
    # ① 批重叠
    recs_ov = [dict(x) for x in recs_c]
    # 人为制造重叠：**两个字段都要改**（`pages_list` 是落盘真清单，`_pages` 只是内存兼容）
    recs_ov[1]["_pages"] = list(recs_ov[0]["_pages"])
    recs_ov[1]["pages_list"] = list(recs_ov[0]["pages_list"])
    bad2 = check_ledger(rows_c, recs_ov, queue_ids=qids, product_marks=marks, report_pending=pend_ids)
    cases.append(("G21 抓住批页重叠", "batch_page_overlap" in bad2["violations_by_kind"],
                  bad2["violations_by_kind"]))
    # ③ 未判定没登记
    bad3 = check_ledger(rows_c, recs_c, queue_ids=qids, product_marks=marks,
                        report_pending=pend_ids[:-1])
    cases.append(("G21 抓住「未判定集合与报告不一致」",
                  "pending_not_registered" in bad3["violations_by_kind"], bad3["violations_by_kind"]))
    bad3b = check_ledger(rows_c, recs_c, queue_ids=qids, product_marks={},
                         report_pending=pend_ids)
    cases.append(("G21 **不重复管**「产物可见性」（那是 G2 的活）：缺痕迹只计数、不判违规",
                  bad3b["ok"] and bad3b["measured"]["pending_without_product_mark"] == len(pend_ids),
                  {"ok": bad3b["ok"],
                   "no_mark": bad3b["measured"]["pending_without_product_mark"]}))
    # ④ 判定者缺失 / 过期算已判
    rows_j = [dict(r) for r in rows_c]
    next(r for r in rows_j if r["item_id"] in vids)["judge"] = None
    bad4 = check_ledger(rows_j, recs_c, queue_ids=qids, product_marks=marks, report_pending=pend_ids)
    cases.append(("G21 **不拦**「已判但无判定者」（可追溯性 = 观测项）；但必须**数得出来**",
                  bad4["ok"] and bad4["measured"]["decided_without_judge"] == 1,
                  {"ok": bad4["ok"], "no_judge": bad4["measured"]["decided_without_judge"]}))
    # 用户提问驱动：**稀疏页**（没差异的页不进队列，这是正常书的常态）**不得**被 G21 拦
    _sp_pages = (1, 3, 5, 7, 9)
    _sp = build_rows([{"item_id": f"S3-P{p:04d}-C-1", "pdf_page": p, "review_type": "content_diff"}
                      for p in _sp_pages])
    _spb = plan_batches(_sp)
    _sp = assign_batches(_sp, _spb)
    _spr = batch_records(_spb, _sp)
    _sp, _ = apply_verdicts(_sp, [{"item_id": r["item_id"], "verdict": "exists",
                                   "judge": {"model": "m", "mode": "agent"}} for r in _sp],
                            batch_id="B001")
    _sres = check_ledger(_sp, _spr, queue_ids=[r["item_id"] for r in _sp],
                         product_marks={p: {"S3_content_diff"} for p in _sp_pages},
                         report_pending=[])
    cases.append(("G21 稀疏页（1/3/5/7/9）不得误拦（批页清单要落盘，不许假定连续）",
                  _sres["ok"], _sres["violations_by_kind"]))
    bad4b = check_ledger(rows_c, recs_c, queue_ids=qids, product_marks=marks,
                         expired_ids=[vids[0]], report_pending=pend_ids)
    cases.append(("G21 抓住「过期判定被算作已判」",
                  "expired_counted_as_decided" in bad4b["violations_by_kind"],
                  bad4b["violations_by_kind"]))
    # ⑤ 拒收没去处
    rows_d = [dict(r) for r in rows_c]
    next(r for r in rows_d if r["state"] == STATE_REJECTED)["destination"] = ""
    bad5 = check_ledger(rows_d, recs_c, queue_ids=qids, product_marks=marks, report_pending=pend_ids)
    cases.append(("G21 抓住「拒收没有去处」",
                  "reject_without_destination" in bad5["violations_by_kind"],
                  bad5["violations_by_kind"]))

    # ---- 豁免必须带批准人（I3）
    rows_w, rep_w = waive_rows([dict(r) for r in rows_c], [pend_ids[0]], reason="r", approved_by="")
    cases.append(("豁免缺 approved_by → 不接受", rep_w["waived"] == []
                  and rep_w["rejected_missing_approved_by"] == [pend_ids[0]], rep_w))
    rows_w2, rep_w2 = waive_rows([dict(r) for r in rows_c], [pend_ids[0]], reason="r", approved_by="user")
    cases.append(("豁免带批准人 → 记账为 waived",
                  rep_w2["waived"] == [pend_ids[0]]
                  and next(r for r in rows_w2 if r["item_id"] == pend_ids[0])["state"] == STATE_WAIVED,
                  None))

    # ---- A7-15：**豁免必须有批准人**（门禁侧；对抗测试抓到的第 4 个盲点）
    #   背景：写入侧有约束（上面两条），**读取侧原先无校验**；当同一页还有别的已判项时，
    #   "把已判项悄悄改成 waived"曾**判决仍可交付**（实测）。所以判据必须落在 `check_ledger`。
    _w_ok = [dict(r) for r in rows_c]
    _victim = next(r for r in _w_ok if r["state"] == STATE_DECIDED)
    _victim.update({"state": STATE_WAIVED,
                    "waiver": {"reason": "用户批准", "approved_by": "user"}})
    _res_ok = check_ledger(_w_ok, recs_c, queue_ids=qids, product_marks=marks, report_pending=pend_ids)
    cases.append(("A7-15：`waived` 带批准人 → 无 `waived_without_approval`",
                  "waived_without_approval" not in _res_ok["violations_by_kind"],
                  _res_ok["violations_by_kind"]))
    _w_bad = [dict(r) for r in rows_c]
    _victim2 = next(r for r in _w_bad if r["state"] == STATE_DECIDED)
    _victim2.update({"state": STATE_WAIVED})
    _victim2.pop("waiver", None)
    _res_bad = check_ledger(_w_bad, recs_c, queue_ids=qids, product_marks=marks, report_pending=pend_ids)
    cases.append(("A7-15 负例：`waived` **拿不出批准人** → `waived_without_approval`"
                  "（原先这条能静默变成「可交付」——对抗测试实证）",
                  _res_bad["violations_by_kind"].get("waived_without_approval") == 1,
                  _res_bad["violations_by_kind"]))
    _w_bad2 = [dict(r) for r in rows_c]
    _victim3 = next(r for r in _w_bad2 if r["state"] == STATE_DECIDED)
    _victim3.update({"state": STATE_WAIVED, "waiver": {"reason": "口头说过", "approved_by": "   "}})
    _res_bad2 = check_ledger(_w_bad2, recs_c, queue_ids=qids, product_marks=marks, report_pending=pend_ids)
    cases.append(("A7-15 负例：批准人是**空白串** → 同样算「没有批准人」",
                  _res_bad2["violations_by_kind"].get("waived_without_approval") == 1,
                  _res_bad2["violations_by_kind"]))

    # ---- A7-15：复算指纹必须用**本次校验的那棵树**（否则副本树 / 临时夹具整片误判过期）
    #   实测：不传 `root` 时，合成夹具的**基线全红**（判定全被判过期）—— 对抗测试当场的发现。
    import tempfile as _tf
    _r_root = _tf.mkdtemp(prefix="jwroot-")
    os.makedirs(os.path.join(_r_root, "s2-ir"), exist_ok=True)
    with open(os.path.join(_r_root, "s2-ir", "ir.mineru.fxroot.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"page": 1, "block_id": "b1"}, ensure_ascii=False) + "\n")
    import binding as _B
    _rows_b = [{"item_id": "A", "state": STATE_DECIDED, "pdf_page": 1, "review_type": "content_diff",
                "verdict": "exists", "batch_id": "B001", "revisions": 0,
                "binding": _B.current("fxroot", _r_root, 1)}]
    _recs_b = [{"batch_id": "B001", "page_from": 1, "page_to": 1, "pages_list": [1], "_pages": [1]}]
    _ok_root = check_ledger(_rows_b, _recs_b, key="fxroot", root=_r_root)
    cases.append(("A7-15：复算指纹**用传入的产物根** → 指纹匹配、不误报过期",
                  "ledger_binding_expired" not in _ok_root["violations_by_kind"],
                  _ok_root["violations_by_kind"]))
    _other_root = _tf.mkdtemp(prefix="jwroot2-")
    _bad_root = check_ledger(_rows_b, _recs_b, key="fxroot", root=_other_root)
    cases.append(("A7-15 负例：产物根**传错** → 如实报 `ledger_binding_expired`（证明 root 真参与计算）",
                  _bad_root["violations_by_kind"].get("ledger_binding_expired") == 1,
                  _bad_root["violations_by_kind"]))
    import shutil as _sh
    _sh.rmtree(_r_root, ignore_errors=True)
    _sh.rmtree(_other_root, ignore_errors=True)

    # ---- A3 补线：台账 → ingest 重建（含负例）
    cases.extend(_selftest_ingest_bridge())

    # ---- A7-13：U 类（原文如此）条目 —— 正例 + 两条负例
    cases.extend(_selftest_errata())

    # ---- A7-14：台账重建必须保住判定（数据丢失级缺陷的回归锁）
    cases.extend(_selftest_merge_state())

    # ---- 2026-10-26：判词合法性（只拒那一条 + 预检不落盘 + 台账校验兜底）
    cases.extend(_selftest_verdict_vocab())
    cases.extend(_selftest_block_flags())

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name
              + ("" if ok else f"   -> {json.dumps(extra, ensure_ascii=False)[:200]}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} judge_worklist tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--queue", required=True)
    p.add_argument("--gate-dir")
    p.add_argument("--out")
    p.add_argument("--book")
    p.add_argument("--pages-per-batch", type=int, default=0)
    p.add_argument("--items-per-batch", type=int, default=0)
    p.add_argument("--config")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_plan)
    fb = sub.add_parser("flag-block",
                        help="对**某个块**直接挂标记（判定者看图时发现手写/印章/装饰；P1-1）")
    fb.add_argument("--ledger", required=True)
    fb.add_argument("--block-id", required=True)
    fb.add_argument("--kind", default="non_book_ink", choices=list(BLOCK_FLAG_KINDS))
    fb.add_argument("--basis", default="", help="必须写清「为什么它不是书的内容」")
    fb.add_argument("--page", type=int, default=0)
    fb.add_argument("--model", default="")
    fb.add_argument("--note", default="")
    fb.add_argument("--mode", default="quarantine", choices=list(BLOCK_FLAG_MODES),
                    help="quarantine=整块移出净版（块里**全是**非书内容时用）；"
                         "mark=只标不移（块里**混有印刷正文**时用，正文保留）")
    fb.add_argument("--reset", action="store_true", help="删除这条块级标记")
    fb.set_defaults(func=cmd_flag_block)
    a = sub.add_parser("apply", help="把判定写进台账（判词非法的一条会被拒，其余照写）")
    a.add_argument("--ledger", required=True)
    a.add_argument("--verdicts", required=True)
    a.add_argument("--batch")
    a.add_argument("--expired-ids", default="")
    a.add_argument("--check-only", action="store_true",
                   help="只校验判词与条目，**不写台账**（提交前预检；有被拒条目 → 退出码 3）")
    a.set_defaults(func=cmd_apply)
    bf = sub.add_parser("backfill", help="迁移：给 A3-4 之前已 decided 的行补判定负载")
    bf.add_argument("--ledger", required=True)
    bf.add_argument("--verdicts", action="append", default=[])
    bf.add_argument("--force", action="store_true", help="同 verdict 也覆盖（用于重算绑定指纹/口径修正）")
    bf.set_defaults(func=cmd_backfill)
    rb = sub.add_parser("refresh-bindings", help="迁移：按当前产物指纹重算已判定的 binding")
    rb.add_argument("--ledger", required=True)
    rb.add_argument("--book")
    rb.set_defaults(func=cmd_refresh_bindings)
    s = sub.add_parser("status")
    s.add_argument("--ledger", required=True)
    s.add_argument("--ids", action="store_true")
    s.set_defaults(func=cmd_status)
    c = sub.add_parser("check")
    c.add_argument("--ledger", required=True)
    c.add_argument("--queue")
    c.add_argument("--md")
    c.add_argument("--expired-ids", default="")
    c.set_defaults(func=cmd_check)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
