# -*- coding: utf-8 -*-
"""S5 视觉判定闭环的最小实现（3 页试点 / U-09 默认口径：适配器 + 离线回放桩）。

用法:
  python -X utf8 s5_pilot.py prepare --queue <visual-queue.jsonl> --render-root <dir> --run-root <dir> [--margin 24]
  python -X utf8 s5_pilot.py crop --render <png> --box x0 y0 x1 y1 --out <png>
  python -X utf8 s5_pilot.py ingest --run <run dir> [--verdicts <verdicts.jsonl>] [--tag batch|per_item]
  python -X utf8 s5_pilot.py compare --a <verdicts A> --b <verdicts B>

设计约束（docs/new/SPEC-化学教材PDF转Markdown-v2.md §4.2 / §4.4 / U-07 / U-09）:
  * 判定者 = 运行本工作流的 agent。本脚本**只准备证据包、只摄入判定**，不自行判定。
  * 视觉后端可替换：判定记录必须自带 judge.model / judge.mode（换豆包 = 改配置）。
  * **U-07 未裁决前自动改写关闭**：decision_intended 与 action_applied 分开记录，
    action_applied 一般为 review；**例外**：转写（U-10）→ insert，图区 `exists`（U-15）→
    `insert_figure` / insert（由 S7 确定性物化）。
  * 判定词表：exists / not_exists / **no_content_diff**（按 D2：空格·标点·记号写法差异不计内容）/ unclear。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from contracts import VERDICTS_WITH_TRANSCRIPTION
from tuning import TUNE

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

CONF_THRESHOLD = TUNE["conf_threshold"]  # SPEC §4.4（值见 tuning.json）
# U-10 用户裁定：转写类（transcribe_region）结果**直接写入正文**；S3 的存在性/删除类仍只标记
BAND_NAMES = ((0.0, 0.25, "上"), (0.25, 0.55, "中上"), (0.55, 0.85, "中下"), (0.85, 1.01, "下"))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: str) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def band_of(bbox, height) -> str:
    if not bbox:
        return "?"
    frac = ((bbox[1] + bbox[3]) / 2.0) / float(height)
    for lo, hi, name in BAND_NAMES:
        if lo <= frac < hi:
            return name
    return "?"


def clip_bbox(bbox, width, height, margin):
    if not bbox:
        return None
    x0, y0, x1, y1 = [int(round(v)) for v in bbox]
    return [max(0, x0 - margin), max(0, y0 - margin),
            min(width, x1 + margin), min(height, y1 + margin)]


# --------------------------------------------------------------------------- prepare
def cmd_prepare(args) -> int:
    items = load_jsonl(args.queue)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-3p-s5pilot"
    run = os.path.join(args.run_root, "runs", run_id)
    crops_dir = os.path.join(run, "crops")
    packets_dir = os.path.join(run, "packets")
    os.makedirs(crops_dir, exist_ok=True)
    os.makedirs(packets_dir, exist_ok=True)

    questions, by_page = [], {}
    for it in items:
        render = it["render"]
        page = int(it["pdf_page"])
        w, h = int(render["width"]), int(render["height"])
        bbox = it.get("bbox")
        origin = os.path.join(args.render_root, os.path.basename(render["path"]))
        if not os.path.exists(origin):
            print(f"!! render missing for {it['item_id']}: {origin}", file=sys.stderr)
            return 2

        rec = {
            "item_id": it["item_id"], "queue_id": it.get("queue_id"), "pdf_page": page,
            "review_type": it["review_type"], "required": bool(it.get("required")),
            "bbox": bbox, "bbox_norm": it.get("bbox_norm"),   # 归一化框：S7 用它把插入落到正确位置
            "bbox_status": (it.get("bbox_provenance") or {}).get("status", "unknown"),
            "bbox_space": (it.get("bbox_provenance") or {}).get("coordinate_space"),
            "band": band_of(bbox, h), "span": it.get("span") or {},
            "context": it.get("context") or {}, "render_path": render["path"],
            "render_sha256": render["sha256"], "render_size": [w, h], "dpi": render.get("dpi"),
            "crop": None, "state_before": it.get("verification_state") or {},
        }
        if bbox and Image is not None:
            box = clip_bbox(bbox, w, h, args.margin)
            crop_rel = os.path.join("crops", f"{it['item_id']}.png")
            with Image.open(origin) as img:
                img.crop(tuple(box)).save(os.path.join(run, crop_rel))
            rec["crop"], rec["crop_box"] = crop_rel, box
        questions.append(rec)
        by_page.setdefault(page, []).append(rec)

    write_jsonl(os.path.join(run, "questions.jsonl"), questions)

    for page, rows in sorted(by_page.items()):
        rows_sorted = sorted(rows, key=lambda r: (r["bbox"] or [0, 0, 0, 0])[1])
        with open(os.path.join(packets_dir, f"page_{page:04d}.packet.json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"pdf_page": page, "item_count": len(rows_sorted),
                       "render": rows_sorted[0]["render_path"],
                       "render_sha256": rows_sorted[0]["render_sha256"],
                       "items": rows_sorted}, fh, ensure_ascii=False, indent=2)

        lines = [f"# 视觉判定证据包 — 第 {page} 页（{len(rows_sorted)} 项，≤32 项/包）", "",
                 f"- 全页渲染：`{rows_sorted[0]['render_path']}`  sha256 `{rows_sorted[0]['render_sha256'][:16]}…`",
                 f"- 页面像素：{rows_sorted[0]['render_size'][0]} × {rows_sorted[0]['render_size'][1]} @ {rows_sorted[0]['dpi']} dpi",
                 "- 待问清单（每项必须回答：**这在页面上存在吗？**）", ""]
        for i, r in enumerate(rows_sorted, 1):
            lines.append(f"## {i}. `{r['item_id']}` — {r['review_type']}（位置带：{r['band']}）")
            lines.append(f"- bbox（渲染像素，左上原点）：`{r['bbox']}`（status={r['bbox_status']}）")
            lines.append(f"- 单项裁图：`{r['crop']}`  box=`{r.get('crop_box')}`" if r["crop"]
                         else "- **无 bbox**：只能整页判定")
            sp = r["span"]
            lines.append(f"- 操作：`{sp.get('operation')}`")
            lines.append(f"- 主引擎(MinerU) span：`{(sp.get('mineru_span') or '')[:200]}`")
            lines.append(f"- 见证(Paddle) span：`{(sp.get('paddle_span') or '')[:200]}`")
            if r.get("review_type") == "figure_region":
                fctx = r.get("context") or {}
                lines.append(f"- 图区：未覆盖 **{fctx.get('uncovered_frac')}**；"
                             f"题注线索 `{fctx.get('caption_hint') or '—'}`；"
                             f"旁证 `{fctx.get('covered_by') or []}`")
            ctx = r["context"]
            if ctx:
                lines.append(f"- 上下文(主)：`{(ctx.get('mineru') or '')[:160]}`")
            lines.append("")
        with open(os.path.join(packets_dir, f"page_{page:04d}.packet.md"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))

    manifest = {
        "run_id": run_id, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "queue": os.path.abspath(args.queue), "queue_sha256": sha256_file(args.queue),
        "render_root": os.path.abspath(args.render_root), "margin_px": args.margin,
        "item_count": len(questions), "pages": sorted(by_page),
        "per_page_counts": {str(p): len(v) for p, v in sorted(by_page.items())},
        "crop_count": sum(1 for q in questions if q["crop"]),
        "auto_rewrite_enabled": True,
        "policy": ("U-10 裁定：`transcribe_region`（转写类）结果**直接写入正文**；"
                   "其余（存在性/删除类）按 U-07 默认口径 action_applied=review，只标记不改写"),
    }
    with open(os.path.join(run, "run-manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"run": run, **manifest}, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- crop
def cmd_crop(args) -> int:
    box = [int(v) for v in args.box]
    with Image.open(args.render) as img:
        box = [max(0, box[0]), max(0, box[1]), min(img.width, box[2]), min(img.height, box[3])]
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        img.crop(tuple(box)).save(args.out)
    print(json.dumps({"out": args.out, "box": box, "sha256": sha256_file(args.out)},
                     ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- ingest
def _binding_for_run(run: str, page=None) -> dict:
    """从 run 目录（`<root>\\s5-out\\<key>\\runs\\<tag>`）反推 key 与根，取**该页所属卷**的指纹（C0）。"""
    try:
        import binding
        run = os.path.abspath(run)
        key = os.path.basename(os.path.dirname(os.path.dirname(run)))
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(run))))
        return binding.current(key, root, page)
    except Exception:                                             # noqa: BLE001
        return {}


def cmd_ingest(args) -> int:
    run = args.run
    tag = args.tag or "batch"
    verdicts_path = args.verdicts or os.path.join(run, f"verdicts.{tag}.jsonl")
    questions = {q["item_id"]: q for q in load_jsonl(os.path.join(run, "questions.jsonl"))}
    raw = load_jsonl(verdicts_path)

    ingested, problems = [], []
    for v in raw:
        iid = v.get("item_id")
        if iid not in questions:
            problems.append({"item_id": iid, "problem": "unknown item_id"})
            continue
        q, verdict = questions[iid], v.get("verdict")
        if verdict not in VERDICTS_WITH_TRANSCRIPTION:
            problems.append({"item_id": iid, "problem": f"bad verdict {verdict!r}"})
            continue
        conf = float(v.get("confidence", 0.0))
        if not (v.get("evidence") or {}).get("what_i_see"):
            problems.append({"item_id": iid, "problem": "missing evidence.what_i_see"})

        # 判定生效（SPEC §4.1：视觉层的答案决定 补/删/保留）。显式 intended_action 优先，
        # 否则按 operation × verdict 的机械兜底映射。
        op = (q.get("span") or {}).get("operation")
        rt = q.get("review_type")
        transcription = v.get("transcription")
        if rt == "figure_region":
            # U-15：图区题确认有图 → S7 补图（insert）；没有 → no-op；看不清 → review。
            if verdict == "exists":
                intended, applied = "insert_figure", "insert"
            elif verdict == "not_exists":
                intended, applied = "no_op", "no_op"
            else:
                intended, applied = "review", "review"
        elif verdict == "transcribed":
            if not (transcription or "").strip():
                problems.append({"item_id": iid, "problem": "transcribed 但缺 transcription"})
            intended, applied = "insert_transcription", "insert"      # U-10 裁定：直接改正文
        elif verdict == "no_text":
            intended, applied = "no_op", "no_op"
        elif verdict == "no_content_diff":
            intended, applied = "no_op", "no_op"
        elif verdict == "unclear":
            intended, applied = "review", "review"
        elif verdict == "exists":
            intended = {"delete": "restore_to_main", "insert": "keep_main",
                        "replace": "keep_main", "structure": "keep_main"}.get(op, "keep_main")
            applied = "review"
        else:  # not_exists
            intended = {"insert": "reject_main_claim", "delete": "confirm_omit",
                        "replace": "keep_main_flagged", "structure": "review"}.get(op, "review")
            applied = "review"
        intended = v.get("intended_action") or intended

        ingested.append({
            **{k: q[k] for k in ("item_id", "pdf_page", "review_type", "bbox", "band",
                                 "render_sha256", "crop")},
            "bbox_norm": q.get("bbox_norm"),      # 归一化框：S7 用它与 IR 块位置比较（像素框不可直接比）
            "caption_hint": q.get("caption_hint") or (q.get("context") or {}).get("caption_hint"),
            "verdict": verdict, "confidence": conf,
            "decision_intended": intended,
            "action_applied": applied,
            "transcription": (transcription if verdict == "transcribed" else None),
            "needs_escalation": verdict == "unclear" or conf < CONF_THRESHOLD,
            "judge": v.get("judge"), "evidence": v.get("evidence"),
            "arm": tag,
            # **判定 ↔ 产物版本绑定**：本次判定所依据的源 PDF/引擎产物/IR 指纹（校验时比对；
            # 不一致则该判定作废，见 _mcp-test/binding.py）
            "binding": _binding_for_run(run, q.get("pdf_page")),
        })

    missing = sorted(set(questions) - {r["item_id"] for r in ingested})
    out_path = os.path.join(run, f"verdicts.ingested.{tag}.jsonl")
    write_jsonl(out_path, ingested)

    by_page, by_verdict, esc = {}, {}, []
    for r in ingested:
        by_page.setdefault(str(r["pdf_page"]), {}).setdefault(r["verdict"], 0)
        by_page[str(r["pdf_page"])][r["verdict"]] += 1
        by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
        if r["needs_escalation"]:
            esc.append({"item_id": r["item_id"], "pdf_page": r["pdf_page"],
                        "review_type": r["review_type"], "verdict": r["verdict"],
                        "confidence": r["confidence"]})
    report = {
        "run": os.path.abspath(run), "arm": tag,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "questions_total": len(questions), "judged": len(ingested), "missing": missing,
        "problems": problems, "by_verdict": by_verdict, "by_page": by_page,
        "escalations": {"count": len(esc),
                        "rate_per_100_pages": round(len(esc) / (3 / 100.0), 1), "items": esc},
        "transcriptions": [{"item_id": r["item_id"], "pdf_page": r["pdf_page"], "bbox": r["bbox"],
                            "transcription": r["transcription"], "confidence": r["confidence"],
                            "model": (r.get("judge") or {}).get("model"),
                            "mode": (r.get("judge") or {}).get("mode")}
                           for r in ingested if r["verdict"] == "transcribed"],
        # 注意：**这里不判门禁**（产出者不得自判；门禁由 validate_run.py 独立判定）。
        # 原先这里写过一个 "G2_no_silent_loss": pass/fail，被源码扫描抓出来 → 改名并降为内部自检项。
        "checks": {"all_questions_judged": not missing},
        "auto_rewrite_enabled": True, "auto_rewrite_scope": "transcribe_region only (U-10 裁定)",
        "confidence_threshold": CONF_THRESHOLD,
        "verdicts_path": os.path.abspath(verdicts_path),
        "verdicts_sha256": sha256_file(verdicts_path),
    }
    with open(os.path.join(run, f"report.{tag}.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ("arm", "questions_total", "judged", "missing",
                                             "problems", "by_verdict", "by_page", "escalations")},
                     ensure_ascii=False, indent=2)[:4000])
    return 0 if not missing and not problems else 1


# --------------------------------------------------------------------------- compare
def cmd_compare(args) -> int:
    a = {r["item_id"]: r for r in load_jsonl(args.a)}
    b = {r["item_id"]: r for r in load_jsonl(args.b)}
    common = sorted(set(a) & set(b))
    agree = [i for i in common if a[i]["verdict"] == b[i]["verdict"]]
    disagree = [{"item_id": i, "a_verdict": a[i]["verdict"], "b_verdict": b[i]["verdict"],
                 "a_conf": a[i]["confidence"], "b_conf": b[i]["confidence"],
                 "a_intended": a[i]["decision_intended"], "b_intended": b[i]["decision_intended"]}
                for i in common if a[i]["verdict"] != b[i]["verdict"]]
    out = {"a": args.a, "b": args.b, "common": len(common), "agree": len(agree),
           "disagree": len(disagree),
           "agreement_rate": round(len(agree) / len(common), 4) if common else None,
           "a_only": sorted(set(a) - set(b)), "b_only": sorted(set(b) - set(a)),
           "mean_conf_a": round(sum(a[i]["confidence"] for i in common) / len(common), 3) if common else None,
           "mean_conf_b": round(sum(b[i]["confidence"] for i in common) / len(common), 3) if common else None,
           "disagreements": disagree}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--queue", required=True)
    p.add_argument("--render-root", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--margin", type=int, default=24)
    p.set_defaults(func=cmd_prepare)

    c0 = sub.add_parser("crop")
    c0.add_argument("--render", required=True)
    c0.add_argument("--box", nargs=4, required=True)
    c0.add_argument("--out", required=True)
    c0.set_defaults(func=cmd_crop)

    g = sub.add_parser("ingest")
    g.add_argument("--run", required=True)
    g.add_argument("--verdicts")
    g.add_argument("--tag", default="batch")
    g.set_defaults(func=cmd_ingest)

    c = sub.add_parser("compare")
    c.add_argument("--a", required=True)
    c.add_argument("--b", required=True)
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
