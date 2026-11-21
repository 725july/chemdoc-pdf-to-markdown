# -*- coding: utf-8 -*-
"""S5 待问清单准入门（SPEC v2 §4.7 的 N-1…N-8）+ 负例自测。

用法:
  python -X utf8 s5_queue_gate.py run      --queue <visual-queue.jsonl> --out <dir>
  python -X utf8 s5_queue_gate.py selftest
  python -X utf8 s5_queue_gate.py measure  --admitted <admitted.jsonl> --verdicts <verdicts.batch.jsonl>

设计要点:
  * 门只做**可机器判定**的事：问题形态、归一化后是否等价、锚点合理性。
  * 绝不用门去判"页面上有没有"（那是 §4.1 视觉层的职责）。
  * 归一化落点遵循 U-01 默认口径：**只作用于比较视图**，不改产物字节。
  * **按问题类型分流（C3 修）**：N-0/N-2/N-3/N-6/N-7 是为 **span 差异类**问题写的；
    **`transcribe_region`（§3.7 的 R1/R4 转写任务）不得被它们拒收**——那类任务的判据是
    "该区域内是否有文字"，与 span 等价性无关。实测：不加分流时 5 条转写任务被误杀 4 条，
    而它们正是 **G11 硬门禁的入口**。
  * **U-15 图区题**：`figure_region` 同样没有 `mineru_span`，走空 span 豁免；
    额外要求 ① bbox 在位（否则 `figure_anchor_missing` → 重裁）；② `figure_ink_px ≥ 100`
    （300dpi 暗像素绝对数；否则 `figure_blank_region` → 登记空白）。**R1 的密度判据 0.02 不动。**
  * **拒收必须有去处（C3 修）**：每个拒收理由都带 `destination`（转结构检查器 / 更窄裁图重问 /
    登记为 no_content_diff …）。**无去处的拒收 = 静默丢失 = 禁止**。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from tuning import TUNE

CJK = r"\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
ALIGN_ARTIFACT_CHARS = TUNE["align_artifact_chars"]  # L2（原与 textnorm.py 各有一份）
MIN_PREFIX_FOR_TRUNCATION = TUNE["min_prefix_for_truncation"]  # L2

# --- C3：按问题类型分流 ------------------------------------------------------
# 这些判据只对 span 差异类问题成立；转写任务一律豁免（见本文档头 "按问题类型分流"）。
SPAN_ONLY_REASONS = ("N-0_empty_span", "N-2_no_content_diff", "N-3_notation_alias_only",
                     "N-6_span_truncation_only", "N-7_alignment_artifact")
TRANSCRIBE_TYPE = "transcribe_region"
FIGURE_TYPE = "figure_region"
# **拒收 ≠ 不判**：每个拒收理由都必须有去处，否则就是静默丢失。
DESTINATIONS = {
    "N-0_empty_span": "no_question_registered",          # 没有可问的问题 → 登记，不再占用判定配额
    "N-1_structure_as_existence": "structure_checker",   # SPEC §4.7 N-1 的原始裁定
    "N-2_no_content_diff": "no_content_diff_registered",  # 按 D2 不算内容差异 → 折叠登记
    "N-3_notation_alias_only": "notation_registry",       # 归 U-02 记号别名
    "N-6_span_truncation_only": "span_boundary_registered",
    "N-7_alignment_artifact": "recrop_and_requery",       # 对齐失败 → 更窄裁图**重问**（不是不看见）
    # U-15：图区题
    "figure_blank_region": "blank_region",                # 暗像素太少 → 纯空白，登记去处
    "figure_anchor_missing": "recrop_and_requery",         # 无 bbox → 重新裁图再问
}


def destination_for(reasons: list[str]) -> str:
    """拒收项的去处：取第一个有映射的理由；未映射 → 空串（由 selftest 逼着补全）。"""
    for r in reasons or []:
        if r in DESTINATIONS:
            return DESTINATIONS[r]
    return ""

# --- 记号别名（N-3 / U-02 的别名索引雏形，只做比较用） -----------------------
ALIASES = [
    (r"\mathrm{^{\circ}C}", "\u00b0C"),
    (r"^{\circ}\mathrm{C}", "\u00b0C"),
    (r"^{\circ}C", "\u00b0C"),
    (r"^{\circ} C", "\u00b0C"),
    ("\u2103", "\u00b0C"),          # ℃
    (r"^{\prime}", "'"),
    (r"\prime", "'"),
    (r"\Delta", "\u0394"),          # Δ
    (r"\left", ""), (r"\right", ""),
    (r"\qquad", " "), (r"\quad", " "),
    (r"\dfrac", r"\frac"),
    (r"\,", ""), (r"\;", ""), (r"\!", ""),
]
PUNCT_FOLD = {"\uff0c": ",", "\uff1a": ":", "\uff1b": ";", "\u3001": ",",
              "\uff08": "(", "\uff09": ")", "\uff1d": "=", "\uff0e": "."}


def apply_aliases(t: str) -> str:
    for src, dst in ALIASES:
        t = t.replace(src, dst)
    return t


def collapse_command_spaces(t: str) -> str:
    t = re.sub(r"\\([A-Za-z]+)\s*\{", r"\\\1{", t)   # \cmd {x} -> \cmd{x}
    t = re.sub(r"([_^])\s*\{", r"\1{", t)            # _ { -> _{ ; ^ { -> ^{
    t = re.sub(r"\{\s+", "{", t)
    t = re.sub(r"\s+\}", "}", t)
    return t


def strip_math_spaces(t: str) -> str:
    """N1/N3：数学区内空白折叠（含 1 0 0 -> 100）。"""
    def _strip(m):
        return re.sub(r"\s+", "", m.group(0))
    t = re.sub(r"\$\$.+?\$\$", _strip, t, flags=re.S)
    t = re.sub(r"\$.+?\$", _strip, t, flags=re.S)
    return t


def fold_cjk_ascii_spaces(t: str) -> str:
    t = re.sub(r"(?<=[" + CJK + r"])[ \t]+(?=[0-9A-Za-z])", "", t)
    t = re.sub(r"(?<=[0-9A-Za-z])[ \t]+(?=[" + CJK + r"])", "", t)
    return t


def fold_punct(t: str) -> str:
    for a, b in PUNCT_FOLD.items():
        t = t.replace(a, b)
    return t


def norm_alias(t: str) -> str:
    """只做记号别名与命令空格（用于区分 N-3 与 N-2）。"""
    return collapse_command_spaces(apply_aliases(t or ""))


def norm_strong(t: str) -> str:
    """数学区空白无关，但保留数学区外的空白。"""
    return fold_cjk_ascii_spaces(collapse_command_spaces(strip_math_spaces(apply_aliases(t or ""))))


def norm_weak(t: str) -> str:
    """按 D2：数学区外空白与全/半角标点也不计（仅比较用）。"""
    return re.sub(r"\s+", "", fold_punct(norm_strong(t)))


def diff_regions(a: str, b: str, ctx: int = 30):
    """归一后用序列对齐裁出真实差异区间；"只在一侧延伸到串尾"的块判为尾部截断。"""
    a, b = norm_strong(a), norm_strong(b)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = sm.get_opcodes()
    # 截断判定需要"有实质公共内容"作前提：否则一侧为空 / 两侧毫无共同内容时，
    # 任何 insert/delete 都会被误判成尾部截断（这正是 insert 类存在性问题的形态）。
    equal_run = max((i2 - i1 for tag, i1, i2, _, _ in ops if tag == "equal"), default=0)
    truncation_allowed = equal_run >= MIN_PREFIX_FOR_TRUNCATION
    regions, tail_truncation, tail_substantive, tail_text = [], False, False, ""
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        is_last = (idx == len(ops) - 1)
        tail_ext_a = (i2 == len(a) and j1 == j2 == len(b))
        tail_ext_b = (j2 == len(b) and i1 == i2 == len(a))
        if is_last and truncation_allowed and (tail_ext_a or tail_ext_b):
            tail_truncation = True
            tail_text = a[i1:i2] if tail_ext_a else b[j1:j2]
            if len(tail_text) >= 20:
                tail_substantive = True
            continue
        regions.append({
            "kind": tag,
            "a": a[max(0, i1 - ctx):min(len(a), i2 + ctx)],
            "b": b[max(0, j1 - ctx):min(len(b), j2 + ctx)],
            "a_raw": a[i1:i2], "b_raw": b[j1:j2],
            "a_len": i2 - i1, "b_len": j2 - j1,
        })
    a_len = sum(r["a_len"] for r in regions)
    b_len = sum(r["b_len"] for r in regions)
    return {"regions": regions, "a_len": a_len, "b_len": b_len,
            "a_mid": " | ".join(r["a"] for r in regions),
            "b_mid": " | ".join(r["b"] for r in regions),
            "tail_truncation": tail_truncation, "tail_substantive": tail_substantive,
            "tail_text": tail_text[:120]}


def bbox_area(bb):
    if not bb:
        return 0
    return max(0, bb[2] - bb[0]) * max(0, bb[3] - bb[1])


# --------------------------------------------------------------------- gate
def gate_item(it: dict, claims: dict | None = None) -> dict:
    span = it.get("span") or {}
    a = span.get("mineru_span") or ""
    b = span.get("paddle_span") or ""
    op = span.get("operation")
    bb = it.get("bbox")
    render = it.get("render") or {}
    page_area = float(render.get("width", 0)) * float(render.get("height", 0)) or 1.0
    area = bbox_area(bb)
    reasons, flags, exempted = [], [], []

    # C3：转写任务走**独立通道**——它的问题是"该区域内有没有文字"，
    # span 等价性判据（N-0/N-2/N-3/N-6/N-7）在定义上不适用。
    # U-15：图区题同理——它问"这里有没有图"，天然没有 mineru_span。
    is_transcribe = it.get("review_type") == TRANSCRIBE_TYPE
    is_figure = it.get("review_type") == FIGURE_TYPE
    if is_transcribe or is_figure:
        exempted = list(SPAN_ONLY_REASONS)

    # N-8 锚点归属（构造期要求；缺失只告警，不拒收）
    if not it.get("anchor_provider"):
        flags.append("anchor_unattributed")

    # N-4 锚点合理性（不拒收，只降级问题形态）
    if bb is None or (it.get("bbox_provenance") or {}).get("status") == "missing":
        flags.append("anchor_missing")
    elif area > 0.5 * page_area:
        flags.append("anchor_coarse")

    # U-15：图区题的两个专用准入条件
    if is_figure:
        if bb is None or (it.get("bbox_provenance") or {}).get("status") == "missing":
            reasons.append("figure_anchor_missing")
        ink = it.get("figure_ink_px")
        if ink is None:
            flags.append("figure_ink_unverified")
        elif int(ink) < TUNE["figcov_ink_min_px"]:
            reasons.append("figure_blank_region")

    # science_candidate 类：span 恒为空，问题来自 candidate 记录的"主张"，
    # 因此 N-0/N-1 与 span 归一化规则一律不适用（首轮实现曾在此误杀全部 12 条黄金样本）。
    is_candidate = it.get("review_type") == "science_candidate"
    claim = (claims or {}).get(it.get("item_id"))
    claim_text = ""
    if claim:
        claim_text = (claim.get("body_after") or claim.get("original_page_transcription")
                      or claim.get("body_before") or "")
    claim_rec = None
    if is_candidate:
        if not (claim_text or "").strip():
            reasons.append("N-0_no_claim")
        else:
            claim_rec = {"class": claim.get("class"), "claim": claim_text[:400],
                         "page_transcription": (claim.get("original_page_transcription") or "")[:400]}

    # N-1 结构差异不得以存在性问题下发（仅 ocr_difference 类）
    if not reasons and not is_candidate and not is_transcribe and not is_figure and op == "structure" and (
            (not a.strip() and not b.strip()) or area > 0.5 * page_area):
        reasons.append("N-1_structure_as_existence")

    # N-0 无问题可问（仅 span 差异类；转写/图区任务豁免）
    if not reasons and not is_candidate and not is_transcribe and not is_figure and not a and not b:
        reasons.append("N-0_empty_span")

    dr = None
    if not reasons and not is_candidate and not is_transcribe and not is_figure:
        if norm_alias(a) == norm_alias(b):
            reasons.append("N-3_notation_alias_only")
        elif norm_weak(a) == norm_weak(b):
            reasons.append("N-2_no_content_diff")
        else:
            dr = diff_regions(a, b)
            if not dr["regions"]:
                reasons.append("N-6_span_truncation_only")
            elif max(dr["a_len"], dr["b_len"]) > ALIGN_ARTIFACT_CHARS:
                reasons.append("N-7_alignment_artifact")
            elif dr["tail_truncation"]:
                flags.append("tail_truncation_ignored")
                if dr["tail_substantive"]:
                    flags.append("tail_truncation_substantive")

    admitted = not reasons
    return {
        "item_id": it.get("item_id"), "pdf_page": it.get("pdf_page"),
        "review_type": it.get("review_type"), "operation": op,
        "admitted": admitted, "reject_reasons": reasons, "flags": flags,
        # C3：拒收必须有去处；无理由（=准入）时为 None。
        "destination": None if admitted else destination_for(reasons),
        "exempted_from": sorted(set(exempted) - set(reasons)),
        "claim": claim_rec,
        "bbox": bb, "bbox_area_px": area, "page_area_px": page_area,
        "question_kind": ("whole_page" if "anchor_coarse" in flags or "anchor_missing" in flags
                          else "located"),
        "span_diff": ({"a": dr["a_mid"], "b": dr["b_mid"], "a_len": dr["a_len"],
                       "b_len": dr["b_len"], "regions": len(dr["regions"]),
                       "tail_truncation": dr["tail_truncation"],
                       "tail_text": dr["tail_text"]} if dr else None),
        "span_len": {"mineru": len(a), "paddle": len(b)},
    }


def cmd_run(args) -> int:
    items = [json.loads(l) for l in open(args.queue, encoding="utf-8") if l.strip()]
    claims = {}
    if getattr(args, "candidates", None) and os.path.exists(args.candidates):
        for line in open(args.candidates, encoding="utf-8"):
            if line.strip():
                c = json.loads(line)
                claims[c.get("candidate_id")] = c
    results = [gate_item(it, claims) for it in items]
    admitted = [r for r in results if r["admitted"]]
    rejected = [r for r in results if not r["admitted"]]

    os.makedirs(args.out, exist_ok=True)
    for name, rows in (("gate-admitted.jsonl", admitted), ("gate-rejected.jsonl", rejected),
                       ("gate-all.jsonl", results)):
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    def tally(rows, key):
        out = {}
        for r in rows:
            for v in (r[key] if isinstance(r[key], list) else [r[key]]):
                out[v] = out.get(v, 0) + 1
        return out

    report = {
        "queue": os.path.abspath(args.queue), "items": len(items),
        "admitted": len(admitted), "rejected": len(rejected),
        "rejected_by_reason": tally(rejected, "reject_reasons"),
        # C3：拒收必须**有去处**——无去处的拒收就是静默丢失，必须能在报告里一眼看见。
        "rejected_by_destination": tally(rejected, "destination"),
        "rejected_without_destination": [r["item_id"] for r in rejected if not r["destination"]],
        # C3：转写任务是 G11 的入口，**必须**全部准入（数量对不上就是分流坏了）。
        "transcribe_tasks": sum(1 for r in results if r.get("review_type") == TRANSCRIBE_TYPE),
        "transcribe_admitted": sum(1 for r in results
                                   if r.get("review_type") == TRANSCRIBE_TYPE and r["admitted"]),
        "figure_tasks": sum(1 for r in results if r.get("review_type") == FIGURE_TYPE),
        "figure_admitted": sum(1 for r in results
                               if r.get("review_type") == FIGURE_TYPE and r["admitted"]),
        "flags": tally(results, "flags"),
        "admitted_by_page": tally(admitted, "pdf_page"),
        "question_kinds": tally(admitted, "question_kind"),
        "admitted_items_with_span_diff": sum(1 for r in admitted if r["span_diff"]),
        "candidate_claims_resolved": sum(1 for r in admitted if r["claim"]),
        "candidates_file": getattr(args, "candidates", None),
    }
    with open(os.path.join(args.out, "gate-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- measure
def cmd_measure(args) -> int:
    admitted = {json.loads(l)["item_id"] for l in open(args.admitted, encoding="utf-8") if l.strip()}
    verd = {json.loads(l)["item_id"]: json.loads(l)
            for l in open(args.verdicts, encoding="utf-8") if l.strip()}
    per = {}
    if getattr(args, "per_item", None) and os.path.exists(args.per_item):
        per = {json.loads(l)["item_id"]: json.loads(l)
               for l in open(args.per_item, encoding="utf-8") if l.strip()}
    kept = {}
    for i, v in verd.items():
        if i not in admitted:
            continue
        if i in per and per[i]["confidence"] > v["confidence"]:
            v = dict(per[i])  # 细粒度项以裁图臂为准
        kept[i] = v
    SEVERE = ("equation", "formula", "digit", "unit")

    def layered(v):
        if v["verdict"] == "unclear":
            return True
        if v["verdict"] == "no_content_diff":
            return False
        return v["confidence"] < 0.9

    out = {
        "verdicts_total": len(verd), "admitted": len(admitted), "admitted_with_verdict": len(kept),
        "escalations_old_rule": sum(1 for v in kept.values() if v["needs_escalation"]),
        "escalations_layered_rule": sum(1 for v in kept.values() if layered(v)),
        "per_100_pages": {
            "old_rule": round(sum(1 for v in kept.values() if v["needs_escalation"]) / (3 / 100.0), 1),
            "layered_rule": round(sum(1 for v in kept.values() if layered(v)) / (3 / 100.0), 1),
            "budget_D6": 20,
        },
        "still_escalating": sorted(v["item_id"] for v in kept.values() if layered(v)),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- selftest
def _item(iid, op, a, b, bbox, w=2119, h=3046, status="trusted", provider=None, page=1,
          rt="ocr_difference", rule=None):
    it = {"item_id": iid, "pdf_page": page, "review_type": rt,
          "bbox": bbox, "bbox_provenance": {"status": status},
          "anchor_provider": provider, "render": {"width": w, "height": h},
          "span": {"operation": op, "mineru_span": a, "paddle_span": b}}
    if rule:
        it["bbox_provenance"]["rule"] = rule
    return it


def selftest() -> int:
    full_page = [165, 177, 1943, 2848]
    cases = []

    # T1 / N-1：实测形态（OCR-P0001-003）
    r = gate_item(_item("T1", "structure", "", "", full_page))
    cases.append(("N-1 结构差异不得下发", r["admitted"] is False and "N-1_structure_as_existence" in r["reject_reasons"], r))

    # T2 / N-2：空白差异（OCR-P0002-005 形态）
    r = gate_item(_item("T2", "insert", " ", "", [170, 2294, 1939, 2397]))
    cases.append(("N-2 空白不算内容", r["admitted"] is False and "N-2_no_content_diff" in r["reject_reasons"], r))

    # T3 / N-3：℃ 记号别名
    r = gate_item(_item("T3", "replace", r"$$\frac{T}{K}=\frac{t}{^{\circ}C}+273.15$$",
                        "$$\\frac{T}{K}=\\frac{t}{\u2103}+273.15$$", [170, 554, 1939, 780]))
    cases.append(("N-3 摄氏度记号别名", r["admitted"] is False and r["reject_reasons"][0].startswith("N-3"), r))

    # T4 / N-3：撇号别名（OCR-P0003-004 形态）
    r = gate_item(_item("T4", "replace", r"^{\prime}", "'", [174, 2211, 521, 2260]))
    cases.append(("N-3 撇号别名", r["admitted"] is False and r["reject_reasons"][0].startswith("N-3"), r))

    # T5 / N-4 反例：单字锚点（OCR-P0002-006，55x46 px）不得被标 anchor_missing
    r = gate_item(_item("T5", "insert", "\u6216\n", "", [170, 2013, 225, 2059]))
    cases.append(("N-4 单字锚点不得误伤", r["admitted"] is True and "anchor_missing" not in r["flags"], r))

    # T6 / N-4：整页锚点 → anchor_coarse
    r = gate_item(_item("T6", "insert", "\u7b2c2\u7ae0 \u6c14\u4f53\n12", "", full_page))
    cases.append(("N-4 整页锚点标 coarse", "anchor_coarse" in r["flags"], r))

    # T7 / N-5：replace 必须算出差异区间，且只含 \\tag{2.1}（OCR-P0001-002 形态）
    a = "$$pV=nRT\\tag{2.1}$$\n\u5f0f\u4e2d:n\u4e3a\u6c14\u4f53\u7269\u8d28\u7684\u91cf,\u4f46\u5728\u4eba\u7c7b\u8ba4\u8bc6\u81ea\u7136\u89c4\u5f8b\u7684"
    b = "$$pV=nRT$$\n\u5f0f\u4e2d:n\u4e3a\u6c14\u4f53\u7269\u8d28\u7684\u91cf,\u4f46\u5728\u4eba\u7c7b"
    r = gate_item(_item("T7", "replace", a, b, [945, 2467, 1151, 2519]))
    ok = r["admitted"] and r["span_diff"] and "tag{2.1}" in r["span_diff"]["a"] and r["span_diff"]["a_len"] < 40
    cases.append(("N-5 差异区间只含 tag", bool(ok), r))

    # T8 / N-6：纯尾部截断
    r = gate_item(_item("T8", "replace", "\u6c14\u4f53\u7684\u4f53\u79ef\u4e0e\u538b\u529b\u6210\u53cd\u6bd4,\u82e5\u7ba1\u5f84\u5747\u5300\u5219\u7a7a\u6c14\u7684\u4f53\u79ef\u4e0e\u6c14\u4f53\u6240\u53d7\u538b\u529b",
                        "\u6c14\u4f53\u7684\u4f53\u79ef\u4e0e\u538b\u529b\u6210\u53cd\u6bd4,\u82e5\u7ba1\u5f84\u5747\u5300\u5219\u7a7a\u6c14", [1, 1, 50, 50]))
    cases.append(("N-6 尾部截断不算差异", r["admitted"] is False and r["reject_reasons"][0].startswith("N-6"), r))

    # T9 / N-7：对齐失败（长且乱序）不得丢给视觉层
    r = gate_item(_item("T9", "replace", "\u7532" * 400, "\u4e59" * 400, [1, 1, 50, 50]))
    cases.append(("N-7 对齐失败被拒", r["admitted"] is False and r["reject_reasons"][0].startswith("N-7"), r))

    # T10 / N-8：锚点归属缺失 → 告警
    r = gate_item(_item("T10", "insert", "\u7b2c2\u7ae0", "", [1, 1, 60, 60], provider=None))
    cases.append(("N-8 锚点未归属告警", "anchor_unattributed" in r["flags"], r))

    # ---- C3 新增：按问题类型分流（转写任务 = G11 的入口，不得被 span 判据误杀）------
    # T11 = 真跑里被误杀的那两条 TR-R1-* 的形态：transcribe_region + 两个 span 皆空。
    r = gate_item(_item("T11", None, "", "", [170, 554, 1939, 780],
                        rt=TRANSCRIBE_TYPE, rule="R1"), None)
    cases.append(("C3 转写任务不得被 N-0 误杀（TR-R1 形态）",
                  r["admitted"] is True and not r["reject_reasons"]
                  and "N-0_empty_span" in r["exempted_from"], r))

    # T12 = N-7 长乱序文本：对 span 差异类**仍须拒收**，但转写任务**不得**被拒。
    _a, _b = "\u7532" * 400, "\u4e59" * 400
    r_span = gate_item(_item("T12a", "replace", _a, _b, [1, 1, 50, 50]))
    r_tr = gate_item(_item("T12b", None, _a, _b, [1, 1, 50, 50], rt=TRANSCRIBE_TYPE, rule="R4a"))
    cases.append(("C3 分流不越界：span 差异类仍被 N-7 拒收",
                  r_span["admitted"] is False
                  and r_span["reject_reasons"] == ["N-7_alignment_artifact"], r_span))
    cases.append(("C3 转写任务不被 N-7 拒收", r_tr["admitted"] is True, r_tr))

    # T13：**拒收必有去处**——对每一种拒收理由都断言 destination 非空且在映射表里。
    _probe = [
        gate_item(_item("T13a", "structure", "", "", full_page)),                 # N-1
        gate_item(_item("T13b", "insert", " ", "", [170, 2294, 1939, 2397])),     # N-2
        gate_item(_item("T13c", "replace", "$$x$$", "$$y$$", [1, 1, 50, 50])),    # N-0/N-3 覆盖不到则跳过
        gate_item(_item("T13d", "replace", _a, _b, [1, 1, 50, 50])),              # N-7
        gate_item(_item("T13e", "insert", None or "", "", [170, 2013, 225, 2059])),  # N-0
    ]
    _rej = [x for x in _probe if not x["admitted"]]
    cases.append(("C3 拒收必带去处（rejected>0 且 destination 全部在映射表内）",
                  bool(_rej) and all(x["destination"] in DESTINATIONS.values() for x in _rej),
                  [(x["item_id"], x["reject_reasons"], x["destination"]) for x in _rej]))
    cases.append(("C3 准入项不带 destination",
                  all(x["destination"] is None for x in _probe if x["admitted"]), None))

    cases.append(("C3 未映射的理由会暴露出来（不会被悄悄放行）",
                  destination_for(["N-9_unknown_thing"]) == ""
                  and destination_for(["N-1_structure_as_existence"]) == "structure_checker", None))

    # ---- U-15：图区题（空 span 放行；暗像素准入；无 bbox 重裁） ----
    def _fig(iid, ink=657, bbox=(170, 554, 1939, 780), **kw):
        it = _item(iid, "insert", "", "叁键", bbox, rt=FIGURE_TYPE, provider="paddle", **kw)
        it["caption_hint"] = "叁键"
        it["context"] = {"caption_hint": "叁键"}
        if ink is not None:
            it["figure_ink_px"] = ink
        return it

    r = gate_item(_fig("F1"))
    cases.append(("U-15 图区题空 span 放行 + 暗像素达标",
                  r["admitted"] and "N-0_empty_span" in r["exempted_from"]
                  and not r["reject_reasons"], r))
    r = gate_item(_fig("F2", ink=0))
    cases.append(("U-15 图区题暗像素不足 → figure_blank_region + 去处",
                  not r["admitted"] and "figure_blank_region" in r["reject_reasons"]
                  and r["destination"] == "blank_region", r))
    r = gate_item(_fig("F3", bbox=None))
    cases.append(("U-15 图区题无 bbox → figure_anchor_missing + 重裁",
                  not r["admitted"] and "figure_anchor_missing" in r["reject_reasons"]
                  and r["destination"] == "recrop_and_requery", r))
    r = gate_item(_fig("F4", ink=None))
    cases.append(("U-15 图区题缺 ink → 放行但标 figure_ink_unverified",
                  r["admitted"] and "figure_ink_unverified" in r["flags"], r))

    passed = 0
    for name, ok, r in cases:
        _extra = "" if ok else f"   -> {json.dumps(r, ensure_ascii=False)[:160] if r else r}"
        print(("PASS  " if ok else "FAIL  ") + name + _extra)
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} negative tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--queue", required=True); r.add_argument("--out", required=True); r.add_argument("--candidates"); r.set_defaults(func=cmd_run)
    m = sub.add_parser("measure"); m.add_argument("--admitted", required=True); m.add_argument("--verdicts", required=True); m.add_argument("--per-item"); m.set_defaults(func=cmd_measure)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
