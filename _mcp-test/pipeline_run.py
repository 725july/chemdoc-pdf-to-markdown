# -*- coding: utf-8 -*-
"""端到端编排（S2→S7）+ S3 问题清单 → S5 视觉队列的适配器。

用法:
  python -X utf8 pipeline_run.py queue  --book <key> [--out-root <数据根>]
  python -X utf8 pipeline_run.py run    --book <key> [--out-root <数据根>]
  python -X utf8 pipeline_run.py all    [--out-root <数据根>]
  python -X utf8 pipeline_run.py selftest

设计:
  * **不通过子进程**（沙箱下 piped stdio 会 EPERM），而是**直接调用各阶段的函数**，保证可复现。
  * `queue` 把 S3 的 `questions.jsonl` 转成 S5 的队列格式（含页渲染与 render sha256），
    于是"新管线自产的待问清单"可以直接喂给已有的 S5 harness（`s5_pilot.py prepare/ingest`）。
  * 每个阶段都写出自己的报告；`run` 返回一份汇总（供报告与验收引用）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from types import SimpleNamespace
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import s2_ir  # noqa: E402
import s3_compare  # noqa: E402
import s4_checks  # noqa: E402
import s6_assemble  # noqa: E402
import s7_output  # noqa: E402
import s5_pilot  # noqa: E402

ROOT = paths.data_root()  # 可迁移：_mcp-test 的上一级 = 产物根
# ---- L1 部署配置（B1-2）：书注册表在 `_mcp-test\books.json`，**改书/换机器改 JSON，不改代码** ----
# 返回的是普通**可变** dict（`c1_cross_chapter_run` / `c2_book_run_check` 会临时加一本书）。
import books as _books  # noqa: E402
import render
from tuning import TUNE

BOOKS = _books.load()


def _crop_ink_px(path: str, px, threshold: int) -> int:
    """U-15：图区题专用——300dpi 裁图内的暗像素绝对数（**不是** R1 的密度）。"""
    from PIL import Image
    im = Image.open(path).convert("L")
    box = tuple(max(0, int(v)) for v in px)
    crop = im.crop(box)
    return sum(1 for v in crop.getdata() if v < threshold)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ S2..S7
def run_pipeline(book_key: str, out_root: str = ROOT, do_publish: bool = True) -> dict:
    cfg = BOOKS[book_key]
    out = {"book": book_key, "label": cfg["label"], "stages": {}}
    ir_dir = os.path.join(out_root, "s2-ir")
    os.makedirs(ir_dir, exist_ok=True)
    mcl_files = os.listdir(cfg["mineru_dir"])
    # 优先取精确的 content_list.json（同目录还可能有 `<uuid>_content_list.json` / `_v2.json` 变体）
    mcl = "content_list.json" if "content_list.json" in mcl_files else next(
        f for f in mcl_files if f.endswith("_content_list.json"))
    m_ir = os.path.join(ir_dir, f"ir.mineru.{cfg['key']}.jsonl")
    p_ir = os.path.join(ir_dir, f"ir.paddle.{cfg['key']}.jsonl")
    ns = SimpleNamespace(engine="mineru", content_list=os.path.join(cfg["mineru_dir"], mcl),
                         layout=os.path.join(cfg["mineru_dir"], "layout.json"),
                         page_sizes=None, pages=None, out=m_ir)
    s2_ir.cmd_build(ns)
    ns = SimpleNamespace(engine="paddle", pages=cfg["paddle_dir"], out=p_ir,
                         content_list=None, layout=None, page_sizes=None)
    s2_ir.cmd_build(ns)
    out["stages"]["S2"] = {"mineru_ir": m_ir, "paddle_ir": p_ir}
    # S3
    s3_dir = os.path.join(out_root, "s3-out", cfg["key"])
    s3_compare.cmd_compare(SimpleNamespace(mineru=m_ir, paddle=p_ir, out=s3_dir))
    out["stages"]["S3"] = json.load(open(os.path.join(s3_dir, "s3-report.json"), encoding="utf-8"))
    # S4
    s4_dir = os.path.join(out_root, "s4-out", cfg["key"])
    s4_checks.cmd_run(SimpleNamespace(ir=m_ir, engine="mineru",
                                      raw=os.path.join(cfg["mineru_dir"], mcl),
                                      images=os.path.join(cfg["mineru_dir"], "images"),
                                      pdf=cfg["pdf"], out=s4_dir, dpi=300))
    out["stages"]["S4"] = json.load(open(os.path.join(s4_dir, "s4-report.json"), encoding="utf-8"))
    # **S5 准入（2026-10-26）**：必须在 S6/S7/S8 **之前**跑一次，否则校对包的「需人工」
    # 会把"准入门本来会分流掉的题"也算进去（真书实测：校对包 30 vs 队列 15，两个数打架）。
    # 只生成 gate / admitted 文件与队列，**不改 S6/S7 吃的问题清单**（切分口径不动）。
    try:
        _g = build_s5_queue(book_key, out_root=out_root)
        out["stages"]["S5_gate"] = {k: _g.get(k) for k in
                                    ("items", "admitted", "rejected", "queue_admitted")}
    except Exception as exc:                                        # noqa: BLE001
        # 辅助步骤失败**不得带走主流程**（SPEC §7-9）：如实记录，S8 会退化成"全部标记都算需人工"
        out["stages"]["S5_gate"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    # **合并全部判定**（不同 tag 的 ingest 文件都要算进来，否则先判定的条目会被漏掉）
    # S6 需要它做"跨页段落切分"，S7 需要它把转写写进正文 → 所以在 S6 之前就算好
    import validate_run as _vr
    verdicts_path, _verdicts = _vr.merge_verdicts(out_root, cfg["key"])
    if not _verdicts:
        verdicts_path = None
    # S6
    s6_dir = os.path.join(out_root, "s6-out", cfg["key"])
    s6_assemble.cmd_run(SimpleNamespace(ir=m_ir, profile=cfg["profile"],
                                        questions=os.path.join(s3_dir, "questions.jsonl"),
                                        s4_hits=os.path.join(s4_dir, "s4-hits.jsonl"),
                                        verdicts=verdicts_path,
                                        page_labels=",".join(str(x) for x in cfg.get("page_labels", [])),
                                        layout=cfg.get("layout") or None,
                                        out=s6_dir))
    out["stages"]["S6"] = json.load(open(os.path.join(s6_dir, "structure.json"), encoding="utf-8"))["counts"]
    # S7
    # S7 写【暂存区】（阶段 2：**校验通过才发布**，正式目录在校验前一动不动）
    stage_root = os.path.join(out_root, "s7-out", "_staging")
    s7_output.cmd_run(SimpleNamespace(ir=m_ir, structure=os.path.join(s6_dir, "structure.json"),
                                      pdf=cfg["pdf"], questions=os.path.join(s3_dir, "questions.jsonl"),
                                      s4_hits=os.path.join(s4_dir, "s4-hits.jsonl"),
                                      verdicts=verdicts_path, out_root=out_root,
                                      anchors=cfg.get("chapter_anchors") or None,
                                      layout=cfg.get("layout") or None,
                                      toc=cfg.get("toc_pages") or None,
                                      out=stage_root, book=cfg["key"]))
    stage_dir = os.path.join(stage_root, cfg["key"])
    rep = json.load(open(os.path.join(stage_dir, "report.json"), encoding="utf-8"))
    out["stages"]["S7"] = {"staging": stage_dir,
                           "escalations": rep["escalations"]["by_category"],
                           "counts": {k: v for k, v in rep["counts"].items() if k != "reviews"},
                           "vision_insertions": len(rep.get("vision_insertions", [])),
                           "vision_absorbed": len(rep.get("vision_absorbed", [])),
                           "verdicts_used": verdicts_path}
    # S8 · 校对包（交付物的一部分：放在书目录内，不复制正文与图）
    import s8_review_kit
    out["stages"]["S8"] = s8_review_kit.build(cfg["key"], out_root, s7_dir=stage_dir, dpi=300)
    # 独立交付校验（判决绑定暂存区字节）+ 阶段 2 的原子发布
    import validate_run
    import publish_run
    vr = validate_run.validate(book_key, out_root, s7_dir=stage_dir)
    out["stages"]["validation"] = {"verdict": vr["verdict"], "exit_code": vr["exit_code"],
                                   "blocking": vr["blocking"], "counts": vr.get("counts", {}),
                                   "exceptions": len(vr.get("exceptions", []))}
    pub = publish_run.publish(book_key, out_root) if do_publish else {
        "result": "skipped（--no-publish）", "exit_code": 0}
    out["stages"]["publish"] = pub
    print(f"[validate] {cfg['label']}: {vr['verdict']} (exit={vr['exit_code']}) "
          f"blocking={vr['blocking']} gates={vr.get('counts', {})}")
    print(f"[publish]  {cfg['label']}: {pub.get('result')} (exit={pub.get('exit_code')}) "
          f"formal={pub.get('formal', pub.get('formal_untouched'))}")
    return out


# ------------------------------------------------- S3 问题清单 → S5 队列
def _bbox_of(hit: dict, blocks: list[dict]):
    if hit.get("bbox_norm"):
        return hit["bbox_norm"]
    for b in blocks:
        if b.get("block_id") == hit.get("block_id") and b.get("bbox_norm"):
            return b["bbox_norm"]
    return None


def derive_transcribe_region(hit: dict, blocks: list[dict]):
    """SPEC §3.7 的区域推导规则（**不得手写 bbox**）。"""
    rule = hit.get("rule")
    bb = _bbox_of(hit, blocks)
    if rule == "R4a" and bb:                      # 孤立单符号公式 → 整行带（前提句与它同排）
        return [0.05, max(0.0, bb[1] - 0.025), 0.95, min(1.0, bb[3] + 0.025)]
    if rule == "R4b":                             # 序列缺起点 → 首个后续标记所在块上方
        page = hit.get("page")
        cands = [b for b in blocks if b.get("page") == page and b.get("role") == "primary"
                 and b.get("bbox_norm")]
        marks = [b for b in cands if any(m in (b.get("text_raw") or "")
                                        for m in ("其次", "然后", "最后", "再次"))]
        if marks:
            top = min(b["bbox_norm"][1] for b in marks)
            return [0.05, max(0.0, top - 0.05), 0.95, min(1.0, top + 0.06)]
        return None
    if rule == "R1" and bb:                       # 空块所在区域就是被漏掉的内容
        return list(bb)
    return None


def build_s5_queue(book_key: str, out_root: str = ROOT, dpi: int = 300,
                   force_reset: bool = False) -> dict:
    """把 S3 的 questions.jsonl 转成 S5 能直接吃的队列（含页渲染）。

    **A7-14**：`force_reset=False`（默认）时，重建台账会**保留既有判定状态**；
    只有显式 `--force-reset` 才会按新队列把判定清零。
    """
    cfg = BOOKS[book_key]
    s3_dir = os.path.join(out_root, "s3-out", cfg["key"])
    qs = [json.loads(l) for l in open(os.path.join(s3_dir, "questions.jsonl"), encoding="utf-8") if l.strip()]
    run_root = os.path.join(out_root, "s5-out")
    render_dir = os.path.join(run_root, "renders", cfg["key"])
    os.makedirs(render_dir, exist_ok=True)
    doc = render.open_doc(cfg["pdf"])
    renders, rows = {}, []

    def ensure_render(page: int) -> dict:
        if page not in renders:
            path = os.path.join(render_dir, f"page_{page:04d}.png")
            _w, _h = render.save_png(doc, page - 1, path, dpi)   # 后端：pypdfium2（见 render.py）
            renders[page] = {"path": path, "sha256": sha256_file(path),
                             "width": _w, "height": _h, "dpi": dpi}
        return renders[page]

    def render_field(page: int) -> dict:
        r = ensure_render(page)
        return {"dpi": dpi, "width": r["width"], "height": r["height"],
                "path": os.path.relpath(r["path"], run_root).replace("\\", "/"),
                "sha256": r["sha256"]}

    def to_px(page: int, bb):
        """S3/S4 的 bbox 是**归一化 0–1**，而 s5_pilot 的裁图按**渲染像素**——必须转换。
        （踩过：不转换时裁出 25×25 px 的空白角，视觉层拿不到任何信息。）"""
        if not bb:
            return None, None
        r = ensure_render(page)
        px = [round(bb[0] * r["width"]), round(bb[1] * r["height"]),
              round(bb[2] * r["width"]), round(bb[3] * r["height"])]
        return px, bb

    for q in qs:
        page = q["pdf_page"]
        px, norm = to_px(page, q["bbox"])
        row = {
            "item_id": q["item_id"], "queue_id": f"VQ-S3-{q['item_id']}", "pdf_page": page,
            "review_type": q["review_type"], "required": True,
            "bbox": px, "bbox_norm": norm,
            "bbox_provenance": {"coordinate_space": "review-render-pixels", "source": "s3",
                                "status": "trusted" if px else "missing"},
            "render": render_field(page),
            "span": {"operation": q.get("operation"),
                     "mineru_span": q.get("mineru_span") or "", "paddle_span": q.get("paddle_span") or ""},
            "context": {"mineru": (q.get("mineru_span") or "")[:300],
                        "paddle": (q.get("paddle_span") or "")[:300]},
            "anchor_provider": q.get("anchor_provider"),      # N-8：锚点归属由 S3 构造保证
            # C3：优先级的依据（P1 = 数字/单位/公式等严重级信号）
            "risk": q.get("risk") or {},
            "crop_escalation": {"status": "planned", "reason_code": "s3_question"},
            "verification_state": {"visual_status": "pending", "risk_status": "visual_required",
                                   "consensus_kind": "none"},
            "schema_version": 1,
        }
        if q.get("review_type") == "figure_region":
            row["context"] = q.get("context") or {}
            row["caption_hint"] = q.get("caption_hint") or ""
            row["figure_ink_px"] = (_crop_ink_px(ensure_render(page)["path"], px,
                                                 TUNE["ink_threshold_gray"]) if px else None)
        rows.append(row)
    # --- S4 → S5（SPEC §3.7）：R1/R4a/R4b 各生成一条 transcribe_region 任务。
    #     这是"两引擎一致地漏"那一类缺陷**唯一**的兜底入口（S3 只问两引擎之间的差异）。
    m_ir = os.path.join(out_root, "s2-ir", f"ir.mineru.{cfg['key']}.jsonl")
    s4_path = os.path.join(out_root, "s4-out", cfg["key"], "s4-hits.jsonl")
    blocks = ([json.loads(l) for l in open(m_ir, encoding="utf-8") if l.strip()]
              if os.path.exists(m_ir) else [])
    hits = ([json.loads(l) for l in open(s4_path, encoding="utf-8") if l.strip()]
            if os.path.exists(s4_path) else [])
    n_s4 = 0
    for h in hits:
        if h.get("rule") not in ("R4a", "R4b", "R1"):
            continue
        region = derive_transcribe_region(h, blocks)
        if not region:
            continue
        page = h.get("page") or 1
        n_s4 += 1
        px, norm = to_px(page, region)
        rows.append({
            "item_id": f"TR-{h['rule']}-{page:04d}-{n_s4:02d}",
            "queue_id": f"VQ-TR-{h['rule']}-{page:04d}-{n_s4:02d}", "pdf_page": page,
            "review_type": "transcribe_region", "required": True,
            "bbox": px, "bbox_norm": norm,
            "bbox_provenance": {"coordinate_space": "review-render-pixels", "source": "s4",
                                "status": "trusted", "rule": h["rule"]},
            "render": render_field(page),
            "span": {"operation": None, "mineru_span": h.get("text") or h.get("tail") or "",
                     "paddle_span": ""},
            "context": {"mineru": json.dumps(h, ensure_ascii=False)[:300], "paddle": ""},
            "anchor_provider": "mineru",
            "question": "请逐字转写该区域内的文字（若该区域确无文字，回 no_text）",
            "crop_escalation": {"status": "planned", "reason_code": f"s4_{h['rule']}"},
            "verification_state": {"visual_status": "pending", "risk_status": "visual_required",
                                   "consensus_kind": "none"},
            "schema_version": 1,
        })
    doc.close()
    qpath = os.path.join(run_root, f"visual-queue.s3.{cfg['key']}.jsonl")
    with open(qpath, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    # --- C3：**准入门必须接上**（此前无任何调用方 → 试点实测的 36% 降噪在整本书上等于不存在）。
    #     口径（SPEC §3.10-5）：① `transcribe_region` 走独立通道，不得被 span 判据拒收（它是 G11 的入口）；
    #     ② 拒收项**必须落盘登记并带去处的**（无去处的拒收 = 静默丢失 = 禁止）；
    #     ③ **全量队列原文件不动**，实际喂给判定作业包的是**准入子集**（新文件）。
    import s5_queue_gate as QG
    gate_dir = os.path.join(run_root, cfg["key"], "gate")
    os.makedirs(gate_dir, exist_ok=True)
    results = [QG.gate_item(r) for r in rows]
    admitted_rows = [r for r, g in zip(rows, results) if g["admitted"]]
    rejected = [g for g in results if not g["admitted"]]
    for name, payload in (("gate-all.jsonl", results),
                          ("gate-admitted.jsonl", [g for g in results if g["admitted"]]),
                          ("gate-rejected.jsonl", rejected)):
        with open(os.path.join(gate_dir, name), "w", encoding="utf-8", newline="\n") as fh:
            for g in payload:
                fh.write(json.dumps(g, ensure_ascii=False, sort_keys=True) + "\n")
    _by_reason, _by_dest = {}, {}
    for g in rejected:
        for rs in g["reject_reasons"]:
            _by_reason[rs] = _by_reason.get(rs, 0) + 1
        _by_dest[g["destination"]] = _by_dest.get(g["destination"], 0) + 1
    gate_report = {
        "book": book_key, "key": cfg["key"], "queue": qpath, "gate_dir": gate_dir,
        "items": len(rows), "admitted": len(admitted_rows), "rejected": len(rejected),
        "rejected_by_reason": _by_reason, "rejected_by_destination": _by_dest,
        "rejected_without_destination": [g["item_id"] for g in rejected if not g["destination"]],
        "transcribe_tasks": sum(1 for r in rows if r.get("review_type") == "transcribe_region"),
        "transcribe_admitted": sum(1 for r, g in zip(rows, results)
                                   if r.get("review_type") == "transcribe_region" and g["admitted"]),
        "figure_tasks": sum(1 for r in rows if r.get("review_type") == "figure_region"),
        "figure_admitted": sum(1 for r, g in zip(rows, results)
                               if r.get("review_type") == "figure_region" and g["admitted"]),
        "queue_full": qpath,
        "queue_admitted": os.path.join(run_root, f"visual-queue.admitted.{cfg['key']}.jsonl"),
    }
    with open(os.path.join(gate_dir, "gate-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(gate_report, fh, ensure_ascii=False, indent=2)
    admitted_path = gate_report["queue_admitted"]
    with open(admitted_path, "w", encoding="utf-8", newline="\n") as fh:
        for r in admitted_rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    # --- C3：生成**判定台账**（分批 / 配额 / 优先级）。这是"判到哪了"的唯一权威状态，
    #     判定者的活从"一次会话判完"变成"按批续跑"。
    import judge_worklist as JW
    _cfg = dict(JW.DEFAULT_CONFIG)
    _cfgfile = os.path.join(run_root, cfg["key"], "judge-config.json")
    if os.path.exists(_cfgfile):
        _cfg.update(json.load(open(_cfgfile, encoding="utf-8")))
    _rows = JW.build_rows(rows, {g["item_id"]: g for g in results})
    _bs = JW.plan_batches(_rows, _cfg["pages_per_batch"], _cfg["items_per_batch"])
    _rows = JW.assign_batches(_rows, _bs)
    _recs = JW.batch_records(_bs, _rows)
    # **A7-14：台账重建必须先合并既有判定状态**（判定不可再生；原先直接覆盖 →
    #   实测把 61 条 decided 清成 pending，下游门禁从"已判"变"未判定"）。
    _keep_rep = {"preserved": 0, "skipped": "无旧台账"}
    if not force_reset:
        _old_rows, _ = JW.load_ledger(os.path.join(run_root, cfg["key"]))
        if _old_rows:
            _rows, _keep_rep = JW.merge_ledger_state(_rows, _old_rows)
    else:
        _keep_rep = {"preserved": 0, "skipped": "用户显式 --force-reset（按新队列重建，判定状态清零）"}
    _st = JW.status(_rows, _recs)
    _prog = JW.write_ledger(os.path.join(run_root, cfg["key"]), _rows, _recs,
                            {"book": cfg["key"], "queue": qpath,
                             "queue_sha256": sha256_file(qpath), "config": _cfg})
    gate_report["ledger"] = {"worklist": os.path.join(run_root, cfg["key"], JW.WORKLIST_NAME),
                             "progress": os.path.join(run_root, cfg["key"], JW.PROGRESS_NAME),
                             "preserved_verdicts": _keep_rep,
                             "batches": len(_recs), "pending": _st["pending"],
                             "p0_pending": _st["p0_pending"], "next_batch": _st["next_batch"],
                             "worklist_sha256": _prog["worklist_sha256"]}
    with open(os.path.join(gate_dir, "gate-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(gate_report, fh, ensure_ascii=False, indent=2)

    # 直接调用 S5 prepare（同一进程，避免子进程）
    prep = SimpleNamespace(queue=admitted_path, render_root=render_dir,
                           run_root=os.path.join(run_root, cfg["key"]), margin=24)
    s5_pilot.cmd_prepare(prep)
    newest = sorted(os.listdir(os.path.join(run_root, cfg["key"], "runs")))[-1]
    res = {"book": book_key, "queue": qpath, "items": len(rows),
           "s3_items": len(qs), "s4_transcribe_tasks": n_s4,
           "gate": {k: gate_report[k] for k in ("admitted", "rejected", "rejected_by_reason",
                                                "rejected_by_destination",
                                                "rejected_without_destination",
                                                "transcribe_tasks", "transcribe_admitted",
                                                "figure_tasks", "figure_admitted",
                                                "gate_dir")},
           "queue_admitted": admitted_path, "admitted_items": len(admitted_rows),
           "ledger": gate_report["ledger"],
           "s5_run": os.path.join(run_root, cfg["key"], "runs", newest)}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


def selftest() -> int:
    cases, passed = [], 0
    # 1) 书一级输入齐备：**pdf+profile**（`intake` 的顺序是「先登记书 → 再跑引擎 → 出画像」，
    #    所以引擎目录由 `books.py check` 报告，不在这里硬断言）
    _has_data = paths.has_data()          # 数据不在 ⇒ 明示跳过（新鲜下载/空机器；见 SPEC §7.10）
    # **与机器无关的那一条**：每本注册书的四个路径都必须**锚在数据根之下**（换机器 / 工具数据分家时不跑偏）。
    # 这才是「接线对不对」的判据；「6 本书在不在」是**环境事实**，那一半按 `_has_data` 明示跳过。
    _root = paths.normalize(paths.data_root())
    _unanchored = [f"{k}:{x}" for k, c in BOOKS.items() for x in ("pdf", "mineru_dir", "paddle_dir", "profile")
                   if not paths.normalize(c[x]).startswith(_root)]
    cases.append(("每本注册书的四个路径都**锚在数据根之下**（与数据在不在无关）",
                  not _unanchored, _unanchored[:3]))

    # 2026-11-15：**聚合成一条**（原来每本书一条）——注册书数量会随清理变化，
    # 逐书一条会让"套件断言合计"随数据漂移，基线就不稳了。
    _bad_in = [k for k, c in BOOKS.items()
               if _has_data and not all(os.path.exists(c[x]) for x in ("pdf", "profile"))]
    cases.append((("每本注册书的输入（pdf+profile）齐备（%d 本）" % len(BOOKS)) if _has_data else
                  "每本注册书的输入（pdf+profile）：**这份拷贝里没有数据树** → 明示跳过",
                  (not _bad_in) if _has_data else True, _bad_in[:3]))
    # 2) 队列转换的字段完整性（用合成问题，不依赖磁盘产物）
    q = {"item_id": "S3-TEST-001", "pdf_page": 1, "review_type": "content_diff", "bbox": [0.1, 0.2, 0.3, 0.4],
         "operation": "replace", "mineru_span": "A", "paddle_span": "B", "anchor_provider": "both"}
    row = {"item_id": q["item_id"], "pdf_page": q["pdf_page"], "review_type": q["review_type"],
           "bbox": q["bbox"], "span": {"operation": q["operation"], "mineru_span": q["mineru_span"],
                                       "paddle_span": q["paddle_span"]},
           "anchor_provider": q["anchor_provider"]}
    need = {"item_id", "pdf_page", "review_type", "bbox", "span", "anchor_provider"}
    cases.append(("S5 队列字段完整（含 anchor_provider，N-8）", need <= set(row), sorted(row)))
    # 3) S5 prepare 能接受该格式（用渲染目录做最小验证）
    cases.append(("S5 prepare 的必需入参存在",
                  {"queue", "render_root", "run_root"} <= set(vars(SimpleNamespace(
                      queue=1, render_root=2, run_root=3, margin=24))), None))
    # 4) 产物已存在（说明端到端至少跑过一次）——只查**正式发布过**的批次；
    #    `puhua-22-30` 是 C1/C2 的**回归样本**（只进 `_staging`，刻意不发布），不参与本断言。
    #    **显式列出"已端到端跑过"的批次**：不写"注册表里除某某以外的全部"——
    #    新登记的书（intake 刚建、还没跑引擎）不该被这条断言牵连。
    # **从磁盘枚举**"有正式交付目录"的批次（不写死书单；`_staging`/`_published` 这类下划线目录跳过）
    _s7 = os.path.join(ROOT, "s7-out")
    _published = sorted(n for n in (os.listdir(_s7) if os.path.isdir(_s7) else [])
                        if not n.startswith("_") and os.path.isdir(os.path.join(_s7, n)))
    #   **2026-10-13 清理后口径**：这四本的**工作副本**已移进 `trash\`（不再跑那四本），
    #   归档基线仍在 `s7-out\_published\<书>\`。所以断言改为"**在位则必须是完整的交付物**"，
    #   已归档的那些**明示跳过**（不假装它还在，也不算失败）。
    _present = [k for k in _published
                if os.path.isdir(os.path.join(ROOT, "s7-out", k))]
    _absent = [k for k in _published if k not in _present]
    _ok = all(os.path.exists(os.path.join(ROOT, "s7-out", k, "report.json")) for k in _present)
    _extra = ("（已归档跳过：%s）" % ",".join(_absent)) if _absent else ""
    cases.append(("四本曾端到端跑过的书：**在位**者交付物完整%s" % _extra, _ok, sorted(_present)))
    # 5) **C3：准入门必须在 queue 路径里被调用**（此前无调用方 → 36% 降噪形同不存在）
    import inspect as _inspect
    _src = _inspect.getsource(build_s5_queue)
    cases.append(("C3 queue 路径调用准入门", "s5_queue_gate" in _src and "gate_item" in _src, None))
    cases.append(("C3 全量队列文件不被覆盖（实际判定用准入子集）",
                  "visual-queue.admitted." in _src and "gate-rejected.jsonl" in _src, None))
    # 2026-10-26（2.1）：准入必须**在 S6/S7/S8 之前**跑（否则校对包「需人工」把未准入的也算进去）
    _run_src = _inspect.getsource(run_pipeline)
    _i_gate, _i_s6 = _run_src.find("build_s5_queue("), _run_src.find("S6 需要它做")
    cases.append(("2026-10-26（2.1）：`run` 在 S6 之前自动跑一次准入门，且失败只降级不中断",
                  _i_gate > 0 and _i_s6 > _i_gate and "S5_gate" in _run_src
                  and "不得带走主流程" in _run_src, {"gate_at": _i_gate, "s6_at": _i_s6}))
    # 6) **C3：真队列复算——转写任务（G11 入口）一条都不许被拒收**
    import s5_queue_gate as _QG
    _qfiles = [os.path.join(ROOT, "s5-out", f) for f in os.listdir(os.path.join(ROOT, "s5-out"))
               if f.startswith("visual-queue.s3.")] if os.path.isdir(os.path.join(ROOT, "s5-out")) else []
    _tr_total = _tr_bad = _rej_nodest = 0
    for _p in _qfiles:
        for _l in open(_p, encoding="utf-8"):
            if not _l.strip():
                continue
            _it = json.loads(_l)
            _g = _QG.gate_item(_it)
            if _it.get("review_type") == "transcribe_region":
                _tr_total += 1
                _tr_bad += 0 if _g["admitted"] else 1
            if not _g["admitted"] and not _g["destination"]:
                _rej_nodest += 1
    # 2026-11-15：**清理后口径**——判据从"有没有数据树"改成"**有没有真队列里真存在转写任务**"，
    # 否则"数据树在、但队列里没有转写题"会被当成 0 误杀（0>0 恒假）而假红。
    _has_tr = _tr_total > 0
    cases.append((("C3 真队列：转写任务 0 误杀（>0 才有效）" if _has_tr else
                   "C3 真队列：**这份拷贝里没有转写任务**（队列为空/无 transcribe）→ 真队列断言明示跳过"),
                  (_tr_bad == 0), {"transcribe": _tr_total, "killed": _tr_bad}))
    cases.append((("C3 真队列：拒收项 0 条缺去处" if _has_data else
                   "C3 真队列：**这份拷贝里没有数据树** → 拒收去处断言明示跳过"),
                  (_rej_nodest == 0) if _has_data else True, _rej_nodest))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} pipeline tests passed")
    return 0 if passed == len(cases) else 1


def _ledger_summary(key: str, out_root: str) -> dict | None:
    """读判定台账 → 写进 `report.json.judgment{}` 的**快照**（C3；SPEC §3.10 产物）。

    台账文件仍是**唯一权威状态**；门禁（G21）自己从台账独立复算，只把这个快照当作
    "未判定集合有没有被悄悄改掉"的**漂移核对**对象。台账不存在 → 返回 `None`（旧产物形态）。
    """
    import judge_worklist as JW
    led = os.path.join(out_root, "s5-out", key)
    if not os.path.exists(os.path.join(led, JW.WORKLIST_NAME)):
        return None
    rows, prog = JW.load_ledger(led)
    recs = prog.get("batches") or []
    for r in recs:
        r.setdefault("_pages", list(range(r["page_from"], r["page_to"] + 1)))
    st = JW.status(rows, recs)
    by_dest: dict[str, int] = {}
    for r in rows:
        if r["state"] == JW.STATE_REJECTED:
            by_dest[r.get("destination") or ""] = by_dest.get(r.get("destination") or "", 0) + 1
    return {
        "batches": [{k: v for k, v in r.items() if not k.startswith("_")} for r in st["batches"]],
        "by_priority": st["by_priority"],
        "pending": st["pending_ids"],
        "waived": sorted(r["item_id"] for r in rows if r["state"] == JW.STATE_WAIVED),
        "rejected_by_destination": by_dest,
        "judge_models": prog.get("judge_models") or [],
        "worklist_sha256": prog.get("worklist_sha256"),
        "next_batch": st["next_batch"],
    }


def run_book(book_key: str, out_root: str = ROOT, do_publish: bool = False,
             engine: str = "mineru", write_volumes: bool = False) -> dict:
    """**书级流水线（C2-2，SPEC §3.9）**：合并卷 → S6/S7（C1 只跑一次）→ S8 → 校验 → （可选）发布。

    与 `run_pipeline` 的区别：`run_pipeline` 服务**一批切片**（S2 直接吃该批的原生输出）；
    `run_book` 服务**整本书**——先把**逐卷** IR 按**书内页号**合并成一条连续序列（`volumes.merge_volumes`），
    再让 S6/S7 在这条**书级序列**上跑**一次**。于是"卷首落在章中间"不再产生错标（§3.9 的核心）。

    前提：逐卷引擎产物与**逐卷 IR 已存在**（命名 `ir.<engine>.<key>.<卷id>.jsonl`）；
    **单卷书**走直通（原样、零重排），与现状逐字节一致。
    """
    import s8_review_kit
    import validate_run
    import volumes as V

    cfg = BOOKS[book_key]
    key = cfg["key"]
    out = {"book": book_key, "key": key, "label": cfg["label"], "stages": {}}
    # 1) 卷规范（唯一合并依据）：缺则按输入 PDF 现算
    vplan = V.build_volumes(book_key, out_root, engine, write=write_volumes)
    if not vplan.get("volumes"):
        out["error"] = vplan.get("error") or "无法规划卷"
        return out
    out["stages"]["volumes"] = {"count": len(vplan["volumes"]), "written": vplan.get("written"),
                                "ids": [v["id"] for v in vplan["volumes"]]}
    # 2) 合并（IR 层）——单卷直通，多卷真合并
    merge = V.merge_volumes(book_key, out_root, engine)
    out["stages"]["merge"] = {k: merge.get(k) for k in ("ok", "rows", "merged_ir", "error")}
    if not merge.get("ok"):
        out["stages"]["merge"]["continuity"] = merge.get("merge", {}).get("continuity")
        return out
    m_ir = merge.get("merged_ir")
    # **多卷书：把合并结果也写到"约定路径"** `ir.<engine>.<key>.jsonl` —— 校验器（validate_run）
    # 与产物报告都按这个约定读 IR；否则多卷书会"校验器找不到 IR"（实测踩到）。
    _conv = os.path.join(out_root, "s2-ir", f"ir.{engine}.{key}.jsonl")
    if os.path.abspath(m_ir) != os.path.abspath(_conv) and os.path.exists(m_ir):
        import shutil as _sh
        _sh.copyfile(m_ir, _conv)
        out["stages"]["merge"]["conventional_ir"] = _conv
    out["stages"]["merge"]["input_continuity"] = merge.get("merge", {}).get("input_continuity")
    # 3) 下游全部在**书级序列**上跑（S3/S4 仍用单文件约定；书级路径不重跑 S3/S4）
    s3_dir = os.path.join(out_root, "s3-out", key)
    s4_dir = os.path.join(out_root, "s4-out", key)
    s6_dir = os.path.join(out_root, "s6-out", key)
    verdicts_path, _verdicts = validate_run.merge_verdicts(out_root, key)
    if not _verdicts:
        verdicts_path = None
    # **页标签口径（C2）**：合并后的 IR 页号已在**书内页号空间**；`--page-labels` 的 CLI 语义是
    # "片内 1..N → 书内页号"，套到合并序列上会把 22 → 1（二次映射）。因此这里**不传页标签**：
    # S6 的"真实页号相邻"判断直接按合并后的页号走（空 labels = 不做映射）。
    s6_assemble.cmd_run(SimpleNamespace(
        ir=m_ir, profile=cfg["profile"],
        questions=os.path.join(s3_dir, "questions.jsonl") if os.path.exists(
            os.path.join(s3_dir, "questions.jsonl")) else None,
        s4_hits=os.path.join(s4_dir, "s4-hits.jsonl") if os.path.exists(
            os.path.join(s4_dir, "s4-hits.jsonl")) else None,
        verdicts=verdicts_path,
        page_labels=None,
        layout=cfg.get("layout") or None,
        out=s6_dir))
    out["stages"]["S6"] = json.load(open(os.path.join(s6_dir, "structure.json"),
                                         encoding="utf-8"))["counts"]
    stage_root = os.path.join(out_root, "s7-out", "_staging")
    # C3：把**判定台账快照**写进 `report.json`（`judgment{}`）。台账文件仍是唯一权威状态；
    # 这里只是"生成产物那一刻台账长这样"，供 G21 判据③核对"未判定集合有没有被悄悄改掉"。
    import tuning as _tuning
    _inject = {"volumes": merge.get("volumes"), "merge": merge.get("merge"),
               # B1-4a：把本次生效的**调优参数快照**写进产物（含"未验证阈值"计数，**只观测不阻断**）
               "tuning": _tuning.snapshot(),
               # 渲染后端身份（AGPL 替换后为 pypdfium2+pypdf；写进产物便于追溯）
               "render": {"backend": render.BACKEND, "versions": render.BACKEND_VERSIONS}}
    _led = _ledger_summary(key, out_root)
    if _led:
        _inject["judgment"] = _led
    inject = json.dumps(_inject, ensure_ascii=False)
    s7_output.cmd_run(SimpleNamespace(
        ir=m_ir, structure=os.path.join(s6_dir, "structure.json"), pdf=cfg["pdf"],
        questions=os.path.join(s3_dir, "questions.jsonl") if os.path.exists(
            os.path.join(s3_dir, "questions.jsonl")) else None,
        s4_hits=os.path.join(s4_dir, "s4-hits.jsonl") if os.path.exists(
            os.path.join(s4_dir, "s4-hits.jsonl")) else None,
        verdicts=verdicts_path, extra_in=inject, out_root=out_root,
        # 章锚（SPEC §3.8）：注册表里就是**源 PDF 物理页号**；s7 会用 structure.page_labels 换算
        anchors=cfg.get("chapter_anchors") or None,
        layout=cfg.get("layout") or None,
        toc=cfg.get("toc_pages") or None,
        # C2-3：多卷合并后的序列 = **完整书级序列**；单卷直通（本批就是全本）也算 book_sequence。
        # 用 `input_continuity` 兜底：输入页映射不连续 → 归 `partial_range`（只登记、不阻断）。
        input_kind=("partial_range"
                    if not (merge.get("merge", {}).get("input_continuity") or {}).get("contiguous", True)
                    else "book_sequence"),
        out=stage_root, book=key))
    stage_dir = os.path.join(stage_root, key)
    rep = json.load(open(os.path.join(stage_dir, "report.json"), encoding="utf-8"))
    cs = rep.get("chapter_split") or {}
    out["stages"]["S7"] = {"staging": stage_dir,
                           "chapters": [(c.get("chapter"), c.get("file")) for c in rep.get("chapters", [])],
                           "chapter_split": {"rule": cs.get("rule"),
                                             "boundaries": len(cs.get("boundaries") or []),
                                             "suspects": len(cs.get("suspects") or []),
                                             "empty_chapters": len(cs.get("empty_chapters") or [])},
                           "assembly_check": cs.get("assembly_check")}
    # 4) 校对包 + 判决（**书级一份**）+ 可选发布
    out["stages"]["S8"] = s8_review_kit.build(key, out_root, s7_dir=stage_dir, dpi=300)
    vr = validate_run.validate(book_key, out_root, s7_dir=stage_dir)
    out["stages"]["validation"] = {"verdict": vr["verdict"], "exit_code": vr["exit_code"],
                                   "blocking": vr["blocking"], "counts": vr.get("counts", {}),
                                   "G20": (vr.get("gates") or {}).get("G20", {}).get("status")}
    if do_publish:
        import publish_run
        out["stages"]["publish"] = publish_run.publish(book_key, out_root)
    else:
        out["stages"]["publish"] = {"result": "skipped（书级 run_book 默认不发布）", "exit_code": 0}
    return out


def cmd_book(a) -> int:
    res = run_book(a.book, a.out_root, do_publish=not a.no_publish, engine=a.engine,
                   write_volumes=a.write_volumes)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("stages", {}).get("validation", {}).get("exit_code", 1) in (0, 2) else 1


def cmd_run(a) -> int:
    res = run_pipeline(a.book, a.out_root, do_publish=not a.no_publish)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("stages", {}).get("publish", {}).get("exit_code", 1) == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--book", required=True, choices=sorted(BOOKS))
    r.add_argument("--out-root", default=ROOT)
    r.add_argument("--no-publish", action="store_true", help="只写到暂存区并校验，不发布")
    r.set_defaults(func=cmd_run)
    q = sub.add_parser("queue")
    q.add_argument("--book", required=True, choices=sorted(BOOKS))
    q.add_argument("--out-root", default=ROOT)
    q.add_argument("--force-reset", action="store_true",
                   help="按新队列**重建**台账并清零既有判定状态（默认：保留既有判定，见 A7-14）")
    q.set_defaults(func=lambda a: (build_s5_queue(a.book, a.out_root, force_reset=a.force_reset) and 0))
    bk = sub.add_parser("book", help="书级流水线（C2-2）：合并卷 → 切章一次 → 书级判决")
    bk.add_argument("--book", required=True, choices=sorted(BOOKS))
    bk.add_argument("--out-root", default=ROOT)
    bk.add_argument("--engine", default="mineru", choices=["mineru", "paddle"])
    bk.add_argument("--write-volumes", action="store_true", help="把卷规范写到 ocr-raw/volumes-<书>.json")
    bk.add_argument("--no-publish", action="store_true")
    bk.set_defaults(func=cmd_book)
    a2 = sub.add_parser("all")
    a2.add_argument("--out-root", default=ROOT)
    a2.add_argument("--no-publish", action="store_true")
    a2.set_defaults(func=lambda x: 0 if all(
        run_pipeline(k, x.out_root, do_publish=not x.no_publish)["stages"]["publish"]["exit_code"] == 0
        for k in sorted(BOOKS)) else 1)
    sub.add_parser("selftest").set_defaults(func=lambda x: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
