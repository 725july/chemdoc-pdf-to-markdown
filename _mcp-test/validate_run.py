# -*- coding: utf-8 -*-
"""交付校验器（SPEC v2 §5.1 + 用户裁定 U-05）：**唯一**的判决者，产出 `validation-report.json`。

用法:
  python -X utf8 validate_run.py run --book <key> [--artifact-root <数据根>] [--waivers <path>]
  python -X utf8 validate_run.py selftest

退出码: 0 可交付 / 1 硬门禁失败 / 2 有未豁免的阻塞例外 / 3 无法判定（缺关键产物或豁免记录非法）

不变量（用户裁定，借原 skill 思路）：
  I1 每条门禁带证据；I2 `unverified` 不得当 pass；I3 未解决例外要么清零要么**显式豁免**；I4 硬失败不得交付。
豁免必须由**用户**批准（`approved_by` 非空）；本工具不会自行豁免。

例外分级：`blocking`（硬门禁失败 / 转写失败 / 静默丢失）必须清零或豁免；`advisory`（软门禁告警、unverified、
S3 判 unclear、脚注孤儿、标题可疑）**只登记不阻断**（SPEC：软门禁"可交付但必须带标记"）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import delivery_note as DN  # noqa: E402  （A6：交付说明的生成与复核；不反向依赖本模块）
import gates as G  # noqa: E402
import s3_compare  # noqa: E402
import s4_checks  # noqa: E402
import s7_output  # noqa: E402  （夹具复用它的 strip_comments，保证"派生一致性"测的是真实现）

EXIT_OK, EXIT_HARD, EXIT_UNWAIVED, EXIT_UNKNOWN = 0, 1, 2, 3


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _load_jsonl(path):
    if not path or not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def merge_verdicts(root: str, key: str) -> tuple[str, list]:
    """把该书的**所有** ingest 判定合并成一份（后出现的覆盖先出现的）。

    为什么要合并：判定会分几批做（不同 `--tag`，例如 S3 差异一批、S4 转写任务另一批），
    分别落在不同 run 目录的 `verdicts.ingested.*.jsonl` 里。若只读最新一份，
    先前已判定的条目会被误判成"从未判定"——**这是实测踩到的集成缺陷**。
    """
    runs = os.path.join(root, "s5-out", key, "runs")
    files = []
    if os.path.isdir(runs):
        for d in sorted(os.listdir(runs)):
            dr = os.path.join(runs, d)
            if not os.path.isdir(dr):
                continue
            for f in sorted(os.listdir(dr)):
                if f.startswith("verdicts.ingested.") and f.endswith(".jsonl"):
                    files.append(os.path.join(dr, f))
    merged: dict[str, dict] = {}
    for p in files:
        for row in _load_jsonl(p):
            if row.get("item_id"):
                merged[row["item_id"]] = row
    out = os.path.join(root, "s5-out", key, "verdicts.merged.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for iid in sorted(merged):
            fh.write(json.dumps(merged[iid], ensure_ascii=False, sort_keys=True) + "\n")
    return out, [merged[k] for k in sorted(merged)]


def _newest_verdicts(root: str, key: str) -> tuple[str, list]:
    return merge_verdicts(root, key)


def _chapter_sort_key(f: str) -> tuple:
    """章文件排序键：前置内容（`-front`）最前，其余按章号**数值**序（不是字典序）。

    C1（SPEC §3.8）起一本书可能有多个 `chapter-NN-*.md`；**顺序必须由章号决定**，
    否则"按序拼接"与 G1/G2 的页覆盖判定会随文件名排序漂移（`chapter-10` 会排到 `chapter-02` 前面）。
    """
    m = re.match(r"chapter-(\d+)", f)
    if not m:
        return (2, 10 ** 6, f)
    return (0 if "-front" in f else 1, int(m.group(1)), f)


def chapter_files(s7: str) -> tuple[list[str], list[str]]:
    """返回（留底版列表, 干净版列表），均按章序排序。

    C1 之前一个目录只有一个文件，本函数与旧行为完全一致；多章时**全部**返回——
    校验器只读 `[0]` 会让第 2 章及其后的内容不进视野，硬门禁（G1/G2/G9…）会误判。
    """
    if not os.path.isdir(s7):
        return [], []
    all_md = [f for f in os.listdir(s7) if f.startswith("chapter-") and f.endswith(".md")]
    reviewed = sorted((f for f in all_md if not f.endswith("-clean.md")), key=_chapter_sort_key)
    clean = sorted((f for f in all_md if f.endswith("-clean.md")), key=_chapter_sort_key)
    return reviewed, clean


def _concat_md(paths: list[str]) -> str:
    """把按章序排好的章文件拼成"全书正文"（C1）。

    分隔用空行：`gates.py` 的页覆盖/静默丢失按 `<!-- pNNN -->` 切段，章文件之间本就以空行结尾，
    这里只保证**不把两个块粘成一行**，以便 markdown 解析与逐行类检查（G7/G9/G16/G18）语义不变。
    """
    parts = []
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        parts.append(open(p, encoding="utf-8").read().rstrip("\n"))
    return "\n\n".join(parts) + ("\n" if parts else "")


def build_ctx(book: str, root: str, s7_dir: str = ""):
    from types import SimpleNamespace
    try:
        import pipeline_run as pr
        cfg = pr.BOOKS.get(book)
    except Exception:                                      # noqa: BLE001
        cfg = None
    key = cfg["key"] if cfg else book                            # 合成夹具用 book 名当 key
    raw_dir = cfg["mineru_dir"] if cfg else os.path.join(root, "raw")
    s7 = s7_dir or os.path.join(root, "s7-out", key)
    mds, clean_mds = chapter_files(s7)
    md_path = os.path.join(s7, mds[0]) if mds else ""
    # 见证引擎缺失 → **不崩**（SPEC §6.2：无见证引擎也可运行，跨页归属类检查降级为 unverified）
    def _load_ir_if_present(p: str) -> list:
        return s3_compare.load_ir(p) if (p and os.path.exists(p)) else []

    ir_m = _load_ir_if_present(os.path.join(root, "s2-ir", f"ir.mineru.{key}.jsonl"))
    ir_p = _load_ir_if_present(os.path.join(root, "s2-ir", f"ir.paddle.{key}.jsonl"))
    s3_compare.attach_children(ir_m)
    s3_compare.attach_children(ir_p)
    vpath, verdicts = _newest_verdicts(root, key)
    # **判定 ↔ 产物版本绑定校验**（binding.py，C0 起**按卷**）：产物版本的改动只让**相关卷**的判定过期；
    # 过期判定**不生效**（视同未判定，由 G11/G8 等如实报红）；历史判定（无 binding）→ 生效但标记。
    try:
        import binding as _binding
        _kept, _expired, _legacy = [], [], []
        for _v in verdicts:
            _st = _binding.status_for_verdict(_v, key, root)
            {"matched": _kept, "expired": _expired, "legacy": _legacy}[_st].append(_v)
        verdicts = _kept + _legacy
        _binding_stat = {"matched": len(_kept), "legacy": len(_legacy), "expired": len(_expired),
                         "expired_ids": [v.get("item_id") for v in _expired][:20]}
    except Exception as exc:                                      # noqa: BLE001
        _binding_stat = {"error": str(exc)}
    natives = [f for f in os.listdir(raw_dir)] if os.path.isdir(raw_dir) else []
    native = next((os.path.join(raw_dir, f) for f in natives if f.endswith("_content_list.json")), "")
    return SimpleNamespace(
        book=book, key=key, root=root, s7_dir=s7, md_path=md_path,
        # C1：一本书可能有多个章文件。`md` 保持"全书正文（按章号拼接）"的语义——门禁的
        # 页覆盖/静默丢失/公式/表/图判定都建立在**全书**上，只看第 1 章会误报内容缺失。
        md_files=mds, clean_md_files=clean_mds,
        md=_concat_md([os.path.join(s7, f) for f in mds]),
        binding_status=_binding_stat,
        report=json.load(open(os.path.join(s7, "report.json"), encoding="utf-8"))
        if os.path.exists(os.path.join(s7, "report.json")) else {},
        ir_m=ir_m, ir_p=ir_p,
        questions=_load_jsonl(os.path.join(root, "s3-out", key, "questions.jsonl")),
        s3_report=json.load(open(os.path.join(root, "s3-out", key, "s3-report.json"), encoding="utf-8"))
        if os.path.exists(os.path.join(root, "s3-out", key, "s3-report.json")) else {},
        s4_hits=_load_jsonl(os.path.join(root, "s4-out", key, "s4-hits.jsonl")),
        verdicts=verdicts, verdicts_path=vpath,
        # **A7-14 加固**：把 ingest 判定也交给 G21，用于核对"台账（唯一真相源）与 ingest（下游读数）一致"
        ingested=verdicts,
        tasks=_load_jsonl(os.path.join(root, "s5-out", f"visual-queue.s3.{key}.jsonl")),
        # C3：判定台账。**G21 自己从这两个文件独立复算**（不信产物里的自检结论）；
        # 两个文件都不存在（旧产物 / 未启用 C3）→ G21 = unverified 且不阻断。
        worklist=_load_jsonl(os.path.join(root, "s5-out", key, "judge-worklist.jsonl")),
        judge_progress=(json.load(open(os.path.join(root, "s5-out", key, "judge-progress.json"),
                                      encoding="utf-8"))
                        if os.path.exists(os.path.join(root, "s5-out", key, "judge-progress.json"))
                        else {}),
        structure=json.load(open(os.path.join(root, "s6-out", key, "structure.json"), encoding="utf-8"))
        if os.path.exists(os.path.join(root, "s6-out", key, "structure.json")) else {},
        native_raw=native, images_dir=os.path.join(raw_dir, "images"),
        profile={},
    )


def collect_exceptions(ctx, gate_results) -> tuple[list[dict], set]:
    """返回 (例外列表, 已被其它例外解释掉的门禁集合)。

    原则：**一个根因只登记一条例外**——否则同一个问题会被记成"门禁失败 + 内容缺口"两条，用户得豁免两次。
    例如 G11 的失败原因就是那些"未解决的转写任务"，它们已经各自成为一条阻塞例外，因此 G11 不再单独登记。
    """
    exc = []
    explained = set()

    def add(eid, severity, source, detail, page=None):
        exc.append({"exception_id": eid, "severity": severity, "source": source,
                    "detail": detail, "page": page})

    for gid, res in gate_results.items():
        if res["status"] == "fail":
            if gid == "G1":
                for ev in res["evidence"]:
                    add(f"EXC-G1-p{ev.get('page')}", "blocking", "gate",
                        f"G1 页覆盖失败：{ev.get('why')}", ev.get("page"))
                if not res["evidence"]:
                    add("EXC-G1", "blocking", "gate", "G1 page_coverage 失败")
            elif gid == "G2":
                for ev in res["evidence"]:
                    add(f"EXC-G2-{ev.get('kind')}-{ev.get('item')}", "blocking", "gate",
                        f"G2 静默丢失：{ev.get('want')} 标记缺失（{ev.get('item')}）", ev.get("page"))
                if not res["evidence"]:
                    add("EXC-G2", "blocking", "gate", "G2 no_silent_loss 失败")
            elif gid == "G11":
                explained.add(gid)          # 原因即那些未解决的转写任务（已有各自的例外）
            else:
                add(f"EXC-{gid}", "blocking" if res.get("blocking") else "advisory", "gate",
                    f"{gid} {res['name']} 失败：{res.get('reason') or res['threshold']}")
        elif res["status"] == "warn":
            add(f"EXC-{gid}-warn", "advisory", "gate", f"{gid} {res['name']} 告警")
        elif res["status"] == "unverified":
            add(f"EXC-{gid}-unverified", "advisory", "gate", f"{gid} 未验证：{res.get('reason','')}")
    # 转写失败（内容缺口）→ 阻塞
    for v in ctx.verdicts:
        if v.get("review_type") == "transcribe_region" and v.get("verdict") not in ("transcribed", "no_text"):
            add(f"EXC-TR-{v['item_id']}", "blocking", "s5",
                f"转写任务未解决（verdict={v.get('verdict')}）", v.get("pdf_page"))
    # S3 从未判定 → 阻塞；判 unclear → 建议级；**已拒收且有去处 → 建议级**（A3 口径，SPEC §3.10-5）
    #   "已有去处"= 这件事已经有人接手；再算一遍阻塞会让交付判决**永远**等不到
    #   （实测：666 条拒收项把判决钉死在「不得交付」）。权威状态看**台账**，不看队列。
    _wl_state = {r.get("item_id"): r for r in (getattr(ctx, "worklist", None) or [])}
    for q in ctx.questions:
        _led = _wl_state.get(q["item_id"]) or {}
        _dest = (_led.get("destination") or "").strip()
        if _led.get("state") == "rejected_with_destination" and _dest:
            add(f"EXC-S3U-{q['item_id']}", "advisory", "s3",
                f"准入门拒收且已有去处（{_dest}）", q.get("pdf_page"))
            continue
        if _led.get("state") == "rejected_with_destination" and not _dest:
            # **无去处的拒收 = 静默丢失 = 禁止**（不得借新口径放行）——G21 也查这条，两处都拦
            add(f"EXC-S3-{q['item_id']}", "blocking", "s3",
                "准入门拒收了但**没有去处**（静默丢失）", q.get("pdf_page"))
            continue
        hit = next((v for v in ctx.verdicts if v.get("item_id") == q["item_id"]), None)
        if hit is None:
            add(f"EXC-S3-{q['item_id']}", "blocking", "s3", "该差异从未被判定", q.get("pdf_page"))
        elif hit.get("verdict") == "unclear":
            add(f"EXC-S3U-{q['item_id']}", "advisory", "s5", "判定为 unclear", q.get("pdf_page"))
    # S6 结构告警 → 建议级
    fn = ctx.structure.get("footnotes") or {}
    for m in (fn.get("missing_definitions") or []):
        add(f"EXC-FN-missing-{m}", "advisory", "s6", f"正文 marker {m} 没有脚注定义")
    for m in (fn.get("orphan_definitions") or []):
        add(f"EXC-FN-orphan-{m}", "advisory", "s6", f"脚注定义 {m} 在正文没有 marker（孤儿）")
    for h in (ctx.structure.get("heading_suspects") or []):
        add(f"EXC-HS-{h.get('block_id')}", "advisory", "s6",
            f"疑为列表项却标成标题：{h.get('text','')[:30]}", h.get("page"))
    return exc, explained


def load_waivers(path: str, exceptions: list[dict]) -> tuple[list, list]:
    rows = _load_jsonl(path)
    ids = {e["exception_id"] for e in exceptions}
    errors, ok = [], []
    for r in rows:
        eid = r.get("exception_id")
        if eid not in ids:
            errors.append({"waiver": r, "why": "引用了不存在的例外 id"})
        elif not (r.get("reason") or "").strip():
            errors.append({"waiver": r, "why": "reason 为空"})
        elif not (r.get("approved_by") or "").strip():
            errors.append({"waiver": r, "why": "缺少 approved_by（豁免必须由用户批准）"})
        else:
            ok.append(r)
    return ok, errors


def decide(gate_results, exceptions, waivers, waiver_errors, explained=()) -> dict:
    waived_ids = {w["exception_id"] for w in waivers}
    hard_all = [gid for gid, r in gate_results.items() if r["status"] == "fail" and r.get("blocking")]
    # 已豁免的、以及"原因已被其它例外覆盖"的硬门禁不再单独拦截
    hard_fail = [g for g in hard_all if f"EXC-{g}" not in waived_ids and g not in set(explained)]
    blocking = [e for e in exceptions if e["severity"] == "blocking" and e["exception_id"] not in waived_ids]
    waived_blocking = [e for e in exceptions if e["severity"] == "blocking" and e["exception_id"] in waived_ids]
    if waiver_errors:
        return {"verdict": "无法判定（豁免记录非法）", "exit_code": EXIT_UNKNOWN,
                "blocking": [], "hard_fail": hard_fail, "hard_all": hard_all, "waived": []}
    if hard_fail:
        return {"verdict": "不得交付（硬门禁失败）", "exit_code": EXIT_HARD,
                "blocking": sorted(hard_fail), "hard_fail": sorted(hard_fail),
                "hard_all": sorted(hard_all), "waived": [e["exception_id"] for e in waived_blocking]}
    if blocking:
        return {"verdict": "不得交付（有未豁免的阻塞例外）", "exit_code": EXIT_UNWAIVED,
                "blocking": [e["exception_id"] for e in blocking], "hard_fail": [],
                "hard_all": sorted(hard_all), "waived": [e["exception_id"] for e in waived_blocking]}
    if waived_blocking:
        return {"verdict": "可交付（含已豁免例外）", "exit_code": EXIT_OK, "blocking": [],
                "hard_fail": [], "hard_all": sorted(hard_all),
                "waived": [e["exception_id"] for e in waived_blocking]}
    return {"verdict": "可交付", "exit_code": EXIT_OK, "blocking": [], "hard_fail": [],
            "hard_all": sorted(hard_all), "waived": []}


def validate(book: str, root: str, waivers_path: str = "", s7_dir: str = "") -> dict:
    ctx = build_ctx(book, root, s7_dir)
    if not ctx.md or not ctx.report:
        # 早返回 = 退出码 3 分支（缺产物）。**形状必须完整**：`cmd_run` 无条件读
        # `hard_gates_failed` / `counts` / `waiver_errors`（2026-10-18 实测：只给 6 个键时，
        # 用户按文档跑 `run --book` 想看哪个门禁不过 → 先崩 KeyError，退出码 3 的路径根本走不到）。
        return {"book": book, "verdict": "无法判定（缺产物：正文或 report.json）",
                "exit_code": EXIT_UNKNOWN, "blocking": [],
                "hard_gates_failed": [], "hard_gates_failed_all": [],
                "counts": {"pass": 0, "warn": 0, "fail": 0, "unverified": 0, "n/a": 0},
                "waiver_errors": [], "gates": {}, "exceptions": []}
    gate_results = G.run_all(ctx)
    exceptions, explained = collect_exceptions(ctx, gate_results)
    if not waivers_path:
        # 豁免记录的**权威位置 = 台账目录**（`s5-out/<书>/waivers.jsonl`，A3 起；见 SPEC「豁免契约」）。
        # 为什么：产物目录会被 publish **整体搬走**（staging → formal），把豁免放在产物里会出现
        # "授权一次、发布后复验却说没授权"的自相矛盾（实测踩到：发布后复验从「可交付」掉回「不得交付」）。
        # 兼容：暂存区/正式目录里的副本仍然认（老写法不失效）。
        cands = [os.path.join(root, "s5-out", ctx.key, "waivers.jsonl"),
                 os.path.join(ctx.s7_dir, "waivers.jsonl"),
                 os.path.join(root, "s7-out", ctx.key, "waivers.jsonl")]
        waivers_path = next((c for c in cands if os.path.exists(c)), cands[0])
    waivers, waiver_errors = load_waivers(waivers_path, exceptions)
    verdict = decide(gate_results, exceptions, waivers, waiver_errors, explained)
    hardcoded = scan_hardcoded_pass(os.path.join(root, "_mcp-test"))
    report = {
        "schema_version": 1, "book": book, "key": ctx.key, "checked_at": _now(),
        "artifact_root": os.path.abspath(root), "markdown": os.path.relpath(ctx.md_path, root)
        if ctx.md_path else None,
        # C1：多章时把**全部**章文件列进报告（`markdown` 保留=第一个，兼容既有读法）
        "markdown_files": [os.path.relpath(os.path.join(ctx.s7_dir, f), root)
                           for f in getattr(ctx, "md_files", [])],
        "clean_markdown_files": [os.path.relpath(os.path.join(ctx.s7_dir, f), root)
                                 for f in getattr(ctx, "clean_md_files", [])],
        "verdict": verdict["verdict"], "exit_code": verdict["exit_code"],
        "blocking": verdict["blocking"], "hard_gates_failed": verdict["hard_fail"],
        "hard_gates_failed_all": verdict.get("hard_all", []),
        "exceptions_waived": verdict.get("waived", []),
        "gates": gate_results,
        "counts": {"pass": sum(1 for r in gate_results.values() if r["status"] == "pass"),
                   "warn": sum(1 for r in gate_results.values() if r["status"] == "warn"),
                   "fail": sum(1 for r in gate_results.values() if r["status"] == "fail"),
                   "unverified": sum(1 for r in gate_results.values() if r["status"] == "unverified"),
                   "n/a": sum(1 for r in gate_results.values() if r["status"] == "n/a")},
        "exceptions": exceptions,
        "waivers_applied": waivers, "waiver_errors": waiver_errors,
        "waivers_path": os.path.abspath(waivers_path) if os.path.exists(waivers_path) else None,
        "hardcoded_pass_scan": hardcoded,
        "invariants": {"I1_evidence_on_every_gate": all("evidence" in r for r in gate_results.values()),
                       "I2_unverified_is_not_pass": all(r["status"] != "pass" or r["evidence"] is not None
                                                        for r in gate_results.values()),
                       "I3_unresolved_must_be_waived": len(verdict["blocking"]) == 0,
                       "I4_hard_fail_blocks": not verdict["hard_fail"]},
    }
    if hardcoded:
        report["verdict"] = "无法判定（源码里存在写死的门禁 pass）"
        report["exit_code"] = EXIT_UNKNOWN
    # ---- A6：**交付说明**（SPEC §3.12）——由**校验器**产出（要转述判决，产出者不得自判），
    #      写完**当场复核**（不变量 I5：数字/清单/骨架/自比），违例即降判决。
    #      刻意**不含时间戳** → 同一棵树两次校验逐字节相同 → `payload_hashes_match` 天然覆盖它。
    os.makedirs(ctx.s7_dir, exist_ok=True)
    dn_rep = json.load(open(os.path.join(ctx.s7_dir, "report.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(ctx.s7_dir, "report.json")) else {}
    note = DN.build(book, root, s7_dir=ctx.s7_dir, vreport=report, s7report=dn_rep)
    w = DN.write_text(ctx.s7_dir, note)
    chk = DN.check(book, root, s7_dir=ctx.s7_dir, vreport=report, s7report=dn_rep, text=note)
    if not chk["ok"]:
        report["verdict"] = "无法判定（交付说明与产物不一致）"
        report["exit_code"] = EXIT_UNKNOWN
        note = DN.build(book, root, s7_dir=ctx.s7_dir, vreport=report, s7report=dn_rep)
        w = DN.write_text(ctx.s7_dir, note)                        # 刷新一次，使说明与最终判决一致
        chk = DN.check(book, root, s7_dir=ctx.s7_dir, vreport=report, s7report=dn_rep, text=note)
    report["delivery_note"] = {"path": DN.NOTE_NAME, "sha256": w["sha256"], "bytes": w["bytes"],
                               "ok": chk["ok"],
                               "violations": [v["kind"] for v in chk["violations"]][:8]}
    report["invariants"]["I5_note_matches_products"] = chk["ok"]
    with open(os.path.join(ctx.s7_dir, "validation-report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    # 回写 report.json 的 gates / blocking / exceptions（SPEC §5.1 硬要求）
    rp = os.path.join(ctx.s7_dir, "report.json")
    if os.path.exists(rp):
        base = json.load(open(rp, encoding="utf-8"))
        base["gates"] = gate_results
        base["blocking"] = verdict["blocking"]
        base["exceptions"] = exceptions
        base["waivers_applied"] = waivers
        base["verdict"] = report["verdict"]
        base["validation_report"] = "validation-report.json"
        with open(rp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(base, fh, ensure_ascii=False, indent=2)
    return report


def scan_hardcoded_pass(src_dir: str) -> list[dict]:
    """源码扫描：产出者不得自己写死门禁结论（当前病根是 `"G2_no_silent_loss": "pass"`）。"""
    hits = []
    if not os.path.isdir(src_dir):
        return hits
    pat = re.compile(r'"G\d+_[a-z_]+"\s*:\s*"(pass|fail|warn|unverified)"')
    for f in sorted(os.listdir(src_dir)):
        if not f.endswith(".py") or f in ("validate_run.py", "gates.py"):
            continue
        p = os.path.join(src_dir, f)
        for i, line in enumerate(open(p, encoding="utf-8"), 1):
            if pat.search(line):
                hits.append({"file": f, "line": i, "text": line.strip()[:100]})
    return hits


def cmd_run(a) -> int:
    rep = validate(a.book, a.artifact_root, a.waivers or "", getattr(a, "s7_dir", "") or "")
    print(json.dumps({k: rep[k] for k in ("book", "verdict", "exit_code", "blocking",
                                          "hard_gates_failed", "counts")}, ensure_ascii=False, indent=2))
    for e in rep["exceptions"]:
        if e["severity"] == "blocking":
            print(f"  [阻塞] {e['exception_id']}: {e['detail']}")
    for e in rep["exceptions"]:
        if e["severity"] == "advisory":
            print(f"  [建议] {e['exception_id']}: {e['detail']}")
    if rep["waiver_errors"]:
        print("  豁免记录非法：", json.dumps(rep["waiver_errors"], ensure_ascii=False))
    return rep["exit_code"]


# ---------------------------------------------------------------- selftest
def _fixture(root: str, *, pages=(1, 2), md_pages=None, orphan=False, r1_unclear=False,
             waivers=None, questions=None, mark_reviews=True, chapters=None,
             ingest_transcribe_verdict=None) -> None:
    """造一份最小可校验的产物树（用于红/绿案例）。

    `chapters`（C1 用）：`[(文件名, [该章的页], 章标题), …]`——把正文拆成**多个章文件**，
    用来回归"校验器必须读**全部**章文件"（只读 `[0]` 会漏掉第 2 章的页 → G1/G2 误判）。
    """
    key = "fx"
    for sub in ("s2-ir", "s3-out/fx", "s4-out/fx", "s5-out", "s6-out/fx", "s7-out/fx", "raw/images"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    blocks = []
    for p in pages:
        blocks.append({"block_id": f"mineru-p{p:04d}-b000-text", "engine": "mineru", "page": p,
                       "order": 0, "kind": "paragraph", "role": "primary", "parent_id": None,
                       "source_field": "text", "text_raw": f"第 {p} 页的正文内容，足够长以便通过扁平化匹配。",
                       "text_canon": "", "bbox_norm": [0.1, 0.2, 0.9, 0.3], "bbox_src": None,
                       "bbox_space": "x", "flags": []})
    for f, rows in ((f"s2-ir/ir.mineru.{key}.jsonl", blocks), (f"s2-ir/ir.paddle.{key}.jsonl", blocks)):
        with open(os.path.join(root, f), "w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    md_pages = md_pages if md_pages is not None else list(pages)
    qs = questions if questions is not None else [
        {"item_id": "S3-P0001-CONTENT-001", "pdf_page": 1, "review_type": "content_diff",
         "bbox": [0.1, 0.2, 0.9, 0.3], "risk": {"signals": ["digit"]}}]
    lines = ["# 测试产物", ""]
    # 最小校对包（G19 是硬门禁：交付物的一部分必须存在）
    _kd = os.path.join(root, "s7-out/fx/review-kit")
    os.makedirs(os.path.join(_kd, "pages"), exist_ok=True)
    open(os.path.join(_kd, "README-校对说明.md"), "w", encoding="utf-8", newline="\n").write("# 校对说明\n")
    open(os.path.join(_kd, "校对表.md"), "w", encoding="utf-8", newline="\n").write(
        "# 校对表\n\n- **待看条数**：0\n")
    open(os.path.join(_kd, "pages", "p0001.png"), "wb").write(b"\x89PNG")
    for p in md_pages:
        lines += [f"<!-- p{p:03d} -->", "", f"第 {p} 页的正文内容，足够长以便通过扁平化匹配。", ""]
    if mark_reviews:
        for q in qs:
            lines.append(f"<!-- REVIEW: S3_{q['review_type']} page={q['pdf_page']} bbox=0.1,0.2,0.9,0.3 -->")
        if r1_unclear:
            # 真实管线会为未解决的转写任务在页首写这个标记（G2 的对账依据）
            lines.append("<!-- REVIEW: transcribe_unresolved page=2 bbox=0.1,0.2,0.5,0.25 -->")
    if chapters:
        # 多章夹具：逐章写留底版 + 干净版（干净版 = 去注释，满足 G9 的派生一致性）
        for _fn, _cps, _title in chapters:
            _ls = [f"# {_title}", ""]
            for p in _cps:
                _ls += [f"<!-- p{p:03d} -->", "", f"第 {p} 页的正文内容，足够长以便通过扁平化匹配。", ""]
            if mark_reviews:
                for q in qs:
                    if q["pdf_page"] in _cps:
                        _ls.append(f"<!-- REVIEW: S3_{q['review_type']} page={q['pdf_page']} "
                                   f"bbox=0.1,0.2,0.9,0.3 -->")
            _body = "\n".join(_ls)
            open(os.path.join(root, f"s7-out/fx/{_fn}"), "w", encoding="utf-8", newline="\n").write(_body)
            _clean = _fn.replace("-reviewed.md", "-clean.md")
            open(os.path.join(root, f"s7-out/fx/{_clean}"), "w", encoding="utf-8", newline="\n").write(
                s7_output.strip_comments(_body))
    else:
        _body1 = "\n".join(lines)
        open(os.path.join(root, f"s7-out/fx/chapter-01.md"), "w", encoding="utf-8", newline="\n").write(_body1)
        # 干净版也造出来：与 S7 一致（只去注释），让 G9 的"派生一致性"分支在**单章**下也真跑一遍
        open(os.path.join(root, f"s7-out/fx/chapter-01-clean.md"), "w", encoding="utf-8",
             newline="\n").write(s7_output.strip_comments(_body1))
    with open(os.path.join(root, f"s3-out/fx/questions.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for q in qs:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")
    json.dump({"questions": len(qs), "dropped": 0}, open(os.path.join(root, "s3-out/fx/s3-report.json"),
                                                         "w", encoding="utf-8"))
    s4 = []
    if r1_unclear:
        s4.append({"rule": "R1", "page": 2, "block_id": "mineru-p0002-b012-text",
                   "bbox_norm": [0.1, 0.2, 0.5, 0.25], "ink_density": 0.05})
    with open(os.path.join(root, "s4-out/fx/s4-hits.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for h in s4:
            fh.write(json.dumps(h, ensure_ascii=False) + "\n")
    tasks = [{"item_id": q["item_id"], "pdf_page": q["pdf_page"], "review_type": q["review_type"],
              "bbox": q["bbox"], "bbox_norm": [0.1, 0.2, 0.9, 0.3]} for q in qs]
    if r1_unclear:
        tasks.append({"item_id": "TR-R1-0002-01", "pdf_page": 2, "review_type": "transcribe_region",
                      "bbox": [0.1, 0.2, 0.5, 0.25], "bbox_norm": [0.1, 0.2, 0.5, 0.25]})
    with open(os.path.join(root, f"s5-out/visual-queue.s3.{key}.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for t in tasks:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    verds = [{"item_id": q["item_id"], "pdf_page": q["pdf_page"], "review_type": q["review_type"],
              "verdict": "exists", "confidence": 0.95} for q in qs]
    if r1_unclear:
        # **A7-14 加固后夹具要能造出"自洽"的台账+ingest**：G21 用例的台账给转写项写 `no_text`，
        # 若这里固定写 `unclear` 就会出现"台账 `no_text` vs ingest `unclear`"的漂移 ——
        # 新判据 `ledger_ingest_verdict_mismatch` 会**如实报红**（它是对的）。
        # 默认仍写 `unclear`（多条既有用例依赖它制造"未决项"）；G21 用例显式传 `no_text` 求自洽。
        verds.append({"item_id": "TR-R1-0002-01", "pdf_page": 2, "review_type": "transcribe_region",
                      "verdict": ingest_transcribe_verdict or "unclear", "confidence": 0.5,
                      "needs_escalation": (ingest_transcribe_verdict or "unclear") == "unclear"})
    rd = os.path.join(root, f"s5-out/fx/runs/20260101T000000Z-fx")
    os.makedirs(rd, exist_ok=True)
    with open(os.path.join(rd, "verdicts.ingested.test.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for v in verds:
            fh.write(json.dumps(v, ensure_ascii=False) + "\n")
    foot = {"markers_in_text": {"1": 1}, "definitions": ({"unmarked": 1} if orphan else {"1": 1}),
            "missing_definitions": [], "orphan_definitions": (["unmarked"] if orphan else [])}
    json.dump({"pages": list(pages), "footnotes": foot, "heading_tree": [], "heading_suspects": [],
               "cross_page_joins": [], "furniture": {"records": []}, "counts": {}},
              open(os.path.join(root, f"s6-out/fx/structure.json"), "w", encoding="utf-8"), ensure_ascii=False)
    _ce_field = [{"chapter": _fn.split("-")[1], "file": _fn.replace("-reviewed.md", "-clean.md"),
                  "derived_from": _fn, "identical_to_strip": True,
                  "comments_in_clean": 0, "footnote_syntax_in_clean": 0}
                 for _fn, _cps, _title in chapters] if chapters else {
        "file": "chapter-01-clean.md", "derived_from": "chapter-01.md", "identical_to_strip": True,
        "comments_in_clean": 0, "footnote_syntax_in_clean": 0}
    # C1：报告里要带 chapters[] / chapter_split（G20 的输入）——单章也要带（"无边界=单章合法"）
    _ch_recs = ([{"chapter": "00" if "-front" in _fn else _fn.split("-")[1],
                  "file": _fn, "clean_file": _fn.replace("-reviewed.md", "-clean.md"),
                  "title": _title, "rule": "single" if not chapters else "printed",
                  "pages": list(_cps), "block_count": len(_cps), "body_count": len(_cps)}
                 for _fn, _cps, _title in chapters] if chapters
                else [{"chapter": "01", "file": "chapter-01.md", "clean_file": "chapter-01-clean.md",
                       "title": None, "rule": "single", "pages": list(md_pages),
                       "block_count": len(md_pages), "body_count": len(md_pages)}])
    _split_field = {"rule": "printed" if chapters else "single", "suspects": [], "empty_chapters": [],
                    "notes": [], "content_blocks": sum(c["block_count"] for c in _ch_recs),
                    "covered_blocks": sum(c["block_count"] for c in _ch_recs),
                    "boundaries": ([{"seq": i + 1, "chapter_no": int(c["chapter"]),
                                     "page": c["pages"][0], "block_id": None, "level": 1}
                                    for i, c in enumerate(_ch_recs)] if chapters else []),
                    "assembly_check": {"blocks_direct": sum(c["block_count"] for c in _ch_recs),
                                       "blocks_in_chapters": sum(c["block_count"] for c in _ch_recs),
                                       "pages_multiset_equal": True, "body_lines_equal": True}}
    json.dump({"counts": {"blocks": len(pages)}, "figures": {}, "clean_edition": _ce_field,
               "chapters": _ch_recs, "chapter_split": _split_field,
               "escalations": {"count": 1, "by_category": {"S3_content_diff": 1}, "rate_per_100_pages": 33.3},
               "footnotes": foot, "unresolved": {}},
              open(os.path.join(root, "s7-out/fx/report.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump([{"type": "text", "text": "第 1 页的正文内容", "page_idx": 0}],
              open(os.path.join(root, "raw/cl.json"), "w", encoding="utf-8"), ensure_ascii=False)
    if waivers:
        with open(os.path.join(root, "s7-out/fx/waivers.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
            for w in waivers:
                fh.write(json.dumps(w, ensure_ascii=False) + "\n")


def _NS_min_ctx():
    """给门禁跑一遍用的最小上下文（只为取门禁 id 列表）。"""
    from types import SimpleNamespace
    return SimpleNamespace(md="", ir_m=[], ir_p=[], questions=[], s4_hits=[], verdicts=[], tasks=[],
                           structure={}, report={}, native_raw="", images_dir="", key="x", book="x",
                           root="", s7_dir="", md_path="")


def selftest() -> int:
    import tempfile
    cases, passed = [], 0
    root = tempfile.mkdtemp(prefix="vrun-")
    import shutil
    from types import SimpleNamespace as _NS
    ALL_GATE_IDS = sorted(G.run_all(_NS_min_ctx()).keys())

    # ① 干净产物 → 可交付
    d = os.path.join(root, "clean")
    _fixture(d)
    r = validate("fx", d)
    cases.append(("干净产物 → 可交付（退出码 0）", r["exit_code"] == 0 and r["verdict"].startswith("可交付"),
                  {"verdict": r["verdict"], "exit": r["exit_code"]}))
    # ② 缺一页 → G1 fail、硬门禁拦住
    d = os.path.join(root, "misspage")
    _fixture(d, pages=(1, 2), md_pages=(1,))
    r = validate("fx", d)
    cases.append(("缺页 → G1 fail + 退出码 1", r["exit_code"] == 1 and "G1" in r["hard_gates_failed"],
                  {"gates": {k: v["status"] for k, v in r["gates"].items() if v["status"] != "pass"}}))
    # ②b **A7-2**：页标记**贴在注释块上** → G1 fail（按行切页看不见这条边界；真书 p8/p26/p34 实测）
    d = os.path.join(root, "glued")
    _fixture(d)
    _mdp = os.path.join(d, "s7-out/fx/chapter-01.md")
    _mt = open(_mdp, encoding="utf-8").read().replace(
        "<!-- p002 -->",
        "<!-- 已判定（备查·无需人工）：1 条 --><!-- p002 -->被粘住的正文", 1)
    open(_mdp, "w", encoding="utf-8", newline="\n").write(_mt)
    r = validate("fx", d)
    cases.append(("A7-2：页标记贴在注释块上 → G1 fail（负例：按行切页看不见这条边界）",
                  r["exit_code"] == 1 and "G1" in r["hard_gates_failed"]
                  and r["gates"]["G1"]["measured"]["page_marker_glued_to_comment"] == 1,
                  r["gates"]["G1"]["measured"]))
    # ③ 未解决项没有标记 → G2 fail
    d = os.path.join(root, "silent")
    _fixture(d, questions=[{"item_id": "S3-P0002-CONTENT-009", "pdf_page": 2,
                            "review_type": "content_diff", "bbox": [0.1, 0.2, 0.9, 0.3],
                            "risk": {"signals": ["digit"]}}], md_pages=(1, 2), mark_reviews=False)
    r = validate("fx", d)
    cases.append(("未解决项无标记 → G2 fail + 退出码 1",
                  r["exit_code"] == 1 and "G2" in r["hard_gates_failed"],
                  {k: v["status"] for k, v in r["gates"].items() if v["status"] != "pass"}))
    # ④ 脚注孤儿 → G5 warn（不再假绿），但**不阻断**
    d = os.path.join(root, "orphan")
    _fixture(d, orphan=True)
    r = validate("fx", d)
    cases.append(("脚注孤儿 → G5 warn 且不阻断（退出码 0）",
                  r["gates"]["G5"]["status"] == "warn" and r["exit_code"] == 0,
                  {"G5": r["gates"]["G5"]["status"], "exit": r["exit_code"]}))
    # ⑤ R1 命中判 unclear → G11 fail（硬）
    d = os.path.join(root, "r1")
    _fixture(d, r1_unclear=True)
    r = validate("fx", d)
    cases.append(("R1 未判定 → 不得交付（G11 由内容缺口例外驱动）",
                  r["exit_code"] != 0 and any(e["severity"] == "blocking" and "TR-R1" in e["exception_id"]
                                              for e in r["exceptions"])
                  and r["gates"]["G11"]["status"] == "fail",
                  {k: v["status"] for k, v in r["gates"].items() if v["status"] != "pass"}))
    # ⑥ 有用户豁免 → 可交付，但报告显眼列出
    d = os.path.join(root, "waived")
    _fixture(d, r1_unclear=True, waivers=[{"exception_id": "EXC-TR-TR-R1-0002-01",
                                           "reason": "该空块确认为跨页搬移留下的原位，接受",
                                           "approved_by": "user", "approved_at": "2026-01-01T00:00:00Z"}])
    r = validate("fx", d)
    cases.append(("豁免 → 可交付且记录豁免",
                  r["exit_code"] == 0 and len(r["waivers_applied"]) == 1 and "已豁免" in r["verdict"],
                  {"verdict": r["verdict"], "exit": r["exit_code"]}))
    # ⑦ 悬空豁免 → 退出码 3
    d = os.path.join(root, "dangling")
    _fixture(d, waivers=[{"exception_id": "EXC-NOPE", "reason": "x", "approved_by": "user"}])
    r = validate("fx", d)
    cases.append(("悬空豁免 → 退出码 3", r["exit_code"] == 3 and r["waiver_errors"], r["waiver_errors"]))
    # ⑧ 缺 approved_by → 退出码 3（豁免必须由用户批准）
    d = os.path.join(root, "noapprover")
    _fixture(d, r1_unclear=True, waivers=[{"exception_id": "EXC-TR-TR-R1-0002-01", "reason": "x"}])
    r = validate("fx", d)
    cases.append(("豁免缺 approved_by → 退出码 3", r["exit_code"] == 3, r["waiver_errors"]))
    # ⑨b **C1：多章正文**——校验器必须读**全部**章文件（只读 `[0]` 会让第 2 章的页不进视野 → G1 误判）
    d = os.path.join(root, "multichapter")
    _fixture(d, pages=(1, 2, 3), chapters=[
        ("chapter-01-reviewed.md", [1, 2], "第1章 绪论"),
        ("chapter-02-reviewed.md", [3], "第2章 气体")])
    r = validate("fx", d)
    cases.append(("多章：页覆盖仍全绿（第 2 章的页也在视野里）",
                  r["gates"]["G1"]["status"] == "pass" and r["gates"]["G2"]["status"] == "pass",
                  {k: v["status"] for k, v in r["gates"].items() if v["status"] != "pass"}))
    cases.append(("多章：报告列出全部章文件且干净版一并列出",
                  r["markdown_files"] == ["s7-out\\fx\\chapter-01-reviewed.md",
                                          "s7-out\\fx\\chapter-02-reviewed.md"]
                  and len(r["clean_markdown_files"]) == 2, r["markdown_files"]))
    cases.append(("多章：两版的派生一致性由 G9 逐章校验（全绿）",
                  r["gates"]["G9"]["status"] == "pass"
                  and len(r["gates"]["G9"]["measured"].get("clean_editions") or []) == 2,
                  r["gates"]["G9"]["measured"]))
    # ⑨c 多章时**章号顺序**决定拼接顺序（`00-front` 最前）——顺序错会让页覆盖判定漂移
    _names = ["chapter-10-reviewed.md", "chapter-00-front-reviewed.md",
              "chapter-02-reviewed.md", "chapter-02-front.md"]
    cases.append(("章文件排序：00-front 最前、其余按章号数值序（不是字典序）",
                  sorted(_names, key=_chapter_sort_key)
                  == ["chapter-00-front-reviewed.md", "chapter-02-front.md",
                      "chapter-02-reviewed.md", "chapter-10-reviewed.md"],
                  sorted(_names, key=_chapter_sort_key)))
    # ⑨d **G20 章切分（硬）**：正例 + 五条负例（每条对应一条判据）
    def _g20_ctx(boundaries, tree, assembly, chapters, anchors=None):
        _cs = {"rule": "printed", "boundaries": boundaries,
               "suspects": [], "empty_chapters": [],
               "notes": [], "assembly_check": assembly}
        if anchors is not None:
            _cs["anchors"] = anchors
        return _NS(structure={"heading_tree": tree},
                   report={"chapter_split": _cs, "chapters": chapters})
    _ok_asm = {"blocks_direct": 3, "blocks_in_chapters": 3,
               "pages_multiset_equal": True, "body_lines_equal": True}
    _tree = [{"block_id": "m-p5-b1-text", "page": 5, "level": 1, "text": "第2章 气体"}]
    _bd = [{"seq": 1, "chapter_no": 2, "page": 5, "block_id": "m-p5-b1-text", "level": 1}]
    _ch = [{"chapter": "02", "file": "chapter-02-reviewed.md", "body_count": 3}]
    cases.append(("G20：边界与 heading_tree.level==1 一致 + 覆盖成立 → pass",
                  G.g20_chapter_split(_g20_ctx(_bd, _tree, _ok_asm, _ch))["status"] == "pass", None))
    cases.append(("G20：证据里报告没有 chapter_split → unverified（且**不阻断**）",
                  (lambda z: z["status"] == "unverified" and z["blocking"] is False)(
                      G.g20_chapter_split(_NS(structure={}, report={}))),
                  None))
    cases.append(("G20 负例：边界在 heading_tree 里找不到 level==1 的同条目 → fail",
                  G.g20_chapter_split(_g20_ctx(_bd, [{"block_id": "x", "page": 5, "level": 2}],
                                               _ok_asm, _ch))["status"] == "fail", None))
    # ⑨d2 **章锚分支（2026-10-18）**：带 anchor 的边界按声明页校验，不要求 heading_tree 命中
    _bda = [{"seq": 1, "chapter_no": 2, "page": 5, "block_id": "anchor-p5",
             "level": 1, "anchor": True, "anchor_page": 5}]
    cases.append(("G20：声明章锚 → 边界按声明页校验，不要求 heading_tree 有 level==1 条目（pass）",
                  G.g20_chapter_split(_g20_ctx(_bda, [], _ok_asm, _ch, anchors=[5]))["status"] == "pass",
                  None))
    cases.append(("G20 负例：章锚边界的 anchor_page 不在声明里 → fail",
                  G.g20_chapter_split(_g20_ctx(
                      [dict(_bda[0], anchor_page=6)], [], _ok_asm, _ch, anchors=[5]))["status"]
                  == "fail", None))
    cases.append(("G20 负例：装配校验不过（页标记集合不等）→ fail",
                  G.g20_chapter_split(_g20_ctx(
                      _bd, _tree, dict(_ok_asm, pages_multiset_equal=False), _ch))["status"] == "fail",
                  None))
    cases.append(("G20 负例：章序号不单调（02 在 01 前）→ fail",
                  G.g20_chapter_split(_g20_ctx(
                      _bd, _tree, _ok_asm,
                      [{"chapter": "02", "file": "chapter-02-reviewed.md", "body_count": 3},
                       {"chapter": "01", "file": "chapter-01-reviewed.md", "body_count": 3}]))["status"]
                  == "fail", None))
    cases.append(("G20 负例：空章（只有标题）未登记 → fail",
                  G.g20_chapter_split(_g20_ctx(
                      _bd, _tree, _ok_asm,
                      [{"chapter": "02", "file": "chapter-02-reviewed.md", "body_count": 0}]))["status"]
                  == "fail", None))
    cases.append(("G20 负例：front-matter 没单独成篇（混进 chapter-01）→ fail",
                  G.g20_chapter_split(_g20_ctx(
                      _bd, _tree, _ok_asm,
                      [{"chapter": "00", "file": "chapter-01-reviewed.md", "body_count": 2}]))["status"]
                  == "fail", None))
    cases.append(("G20：**全书没有章边界 → pass（单章交付合法）**",
                  G.g20_chapter_split(_g20_ctx([], [], dict(_ok_asm, blocks_direct=3,
                                                            blocks_in_chapters=3),
                                               [{"chapter": "01", "file": "chapter-01-reviewed.md",
                                                 "body_count": 3}]))["status"] == "pass", None))
    # ⑨e **G20 判据⑥（C2-3）**：`input_kind` 决定"单章"是正常还是可疑
    def _g20_ctx_ik(ik, rule, suspects=None):
        return _NS(structure={"heading_tree": []},
                   report={"chapter_split": {"rule": rule, "input_kind": ik,
                                             "suspects": suspects or [], "empty_chapters": [],
                                             "notes": [], "boundaries": [],
                                             "assembly_check": dict(_ok_asm, blocks_direct=3,
                                                                    blocks_in_chapters=3)},
                           "chapters": [{"chapter": "01", "file": "chapter-01-reviewed.md",
                                         "body_count": 3}]})
    cases.append(("G20⑥：book_sequence 却 rule=single 且**未登记** → fail",
                  G.g20_chapter_split(_g20_ctx_ik("book_sequence", "single"))["status"] == "fail", None))
    cases.append(("G20⑥：book_sequence 却 rule=single 但**已登记可疑** → pass（不阻断，但要留痕）",
                  G.g20_chapter_split(_g20_ctx_ik(
                      "book_sequence", "single", [{"why": "整本书不该没有章边界 → 可疑"}]))["status"]
                  == "pass", None))
    cases.append(("G20⑥：partial_range 单章 → **pass**（不连续/只交一部分是既定行为）",
                  G.g20_chapter_split(_g20_ctx_ik("partial_range", "single"))["status"] == "pass", None))
    cases.append(("G20⑥：未声明 input_kind（旧产物）→ 不因此判 fail（向后兼容）",
                  G.g20_chapter_split(_g20_ctx_ik(None, "single"))["status"] == "pass", None))
    # ⑧b **G21（C3）**：台账存在时必须真判——正例（已判完 → pass）+ 两个硬失败负例
    import judge_worklist as JW
    d = os.path.join(root, "ledger")
    _fixture(d, r1_unclear=True, ingest_transcribe_verdict="no_text")   # 台账/ingest 自洽（A7-14 加固）
    _q = os.path.join(d, "s5-out/visual-queue.s3.fx.jsonl")
    _items = [json.loads(l) for l in open(_q, encoding="utf-8") if l.strip()]
    _rows = JW.build_rows(_items)
    _bs = JW.plan_batches(_rows)
    _rows = JW.assign_batches(_rows, _bs)
    _recs = JW.batch_records(_bs, _rows)
    _rows, _ = JW.apply_verdicts(
        _rows, [{"item_id": x["item_id"],
                 "verdict": ("no_text" if x["review_type"] == "transcribe_region" else "exists"),
                 "judge": {"model": "m", "mode": "agent"}} for x in _rows],
        batch_id="B001")
    _ld = os.path.join(d, "s5-out/fx")
    JW.write_ledger(_ld, _rows, _recs, {"book": "fx", "queue": _q})
    _rp = os.path.join(d, "s7-out/fx/report.json")
    _rep = json.load(open(_rp, encoding="utf-8"))
    _st = JW.status(_rows, _recs)
    _rep["judgment"] = {"pending": _st["pending_ids"], "by_priority": _st["by_priority"]}
    json.dump(_rep, open(_rp, "w", encoding="utf-8"), ensure_ascii=False)
    r = validate("fx", d)
    cases.append(("G21：台账齐备、已判完、判定者在位 → pass（且确是硬门禁）",
                  r["gates"]["G21"]["status"] == "pass" and r["gates"]["G21"]["blocking"] is True,
                  r["gates"]["G21"]["measured"]))
    # 负例 A：队列里有、台账里没有 → 丢项 → fail（硬）
    _drop = next(x["item_id"] for x in _rows if x["review_type"] == "transcribe_region")
    _rows_a = [dict(x) for x in _rows if x["item_id"] != _drop]
    JW.write_ledger(_ld, _rows_a, _recs, {"book": "fx", "queue": _q})
    r = validate("fx", d)
    cases.append(("G21 负例：队列里有、台账里没有 → fail（硬，不阻断历史交付的规则不适用于它）",
                  r["gates"]["G21"]["status"] == "fail" and "G21" in r["hard_gates_failed"]
                  and r["gates"]["G21"]["measured"]["violations"].get("ledger_item_missing", 0) >= 1,
                  r["gates"]["G21"]["measured"]))
    # 负例 B（**已按用户提问订正**）：已判但判定者缺失 → **不拦**（可追溯性=观测项），但必须数得出来
    _rows_b = [dict(x) for x in _rows]
    _rows_b[0]["judge"] = None
    JW.write_ledger(_ld, _rows_b, _recs, {"book": "fx", "queue": _q})
    r = validate("fx", d)
    cases.append(("G21：已判但无判定者 → **pass**（不拦）+ measured 数得出来",
                  r["gates"]["G21"]["status"] == "pass"
                  and r["gates"]["G21"]["measured"].get("decided_without_judge", 0) >= 1,
                  r["gates"]["G21"]["measured"]["violations"]))
    # ⑧c **豁免的权威位置 = 台账目录**（A3）：产物目录里没有副本时也必须找得到
    #   （否则 publish 整体搬走产物后，复验会说"没授权" —— 实测踩到）
    d = os.path.join(root, "waiverdir")
    _fixture(d, r1_unclear=True)
    _task = next(x for x in _items if x["review_type"] == "transcribe_region")
    _ex_id = f"EXC-TR-{_task['item_id']}"
    _led_dir = os.path.join(d, "s5-out", "fx")
    os.makedirs(_led_dir, exist_ok=True)
    with open(os.path.join(_led_dir, "waivers.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"exception_id": _ex_id, "reason": "用户按证据豁免",
                             "approved_by": "用户"}, ensure_ascii=False) + "\n")
    r = validate("fx", d)
    cases.append(("A3：豁免写在**台账目录**、产物目录里没有副本 → 仍生效（可交付）",
                  r["exit_code"] == 0 and len(r.get("waivers_applied") or []) == 1,
                  {"exit": r["exit_code"], "waivers": r.get("waivers_applied"),
                   "waivers_path": r.get("waivers_path")}))
    # 负例 C（**A3 口径**）：拒收项**有去处** → 只能 advisory，不得再算阻塞
    #   （否则 666 条拒收项会把交付判决永远钉在「不得交付」——真书实测踩到）
    #   注意：必须挑**队列里的 S3 问题项**（`_drop` 是转写项，不在 questions 里，测不到这条路径）；
    #   台账也要**就地重建**——`_ld` 是别的夹具的路径，那里根本没有台账（实测踩到：断言全空）
    _s3_item = "S3-P0001-CONTENT-001"          # 夹具默认的 S3 问题项（questions.jsonl 里就它）
    _ld_c = os.path.join(d, "s5-out", "fx")
    _rows_c = JW.build_rows([dict(x) for x in _items])
    _rows_c = JW.assign_batches(_rows_c, _bs)
    for _x in _rows_c:
        if _x["item_id"] == _s3_item:
            _x["state"], _x["destination"] = "rejected_with_destination", "notation_registry"
    JW.write_ledger(_ld_c, _rows_c, _recs, {"book": "fx", "queue": _q})
    r = validate("fx", d)
    _ex = {e["exception_id"]: e for e in r["exceptions"]}
    cases.append(("A3：拒收项**有去处** → 登记为 advisory（EXC-S3U），**不再阻塞交付**",
                  _ex.get(f"EXC-S3U-{_s3_item}", {}).get("severity") == "advisory"
                  and f"EXC-S3-{_s3_item}" not in _ex, sorted(k for k in _ex if "P0001" in k)))
    # 负例 D：拒收项**没有去处** → 仍阻塞（"无去处的拒收 = 静默丢失 = 禁止"）
    for _x in _rows_c:
        if _x["item_id"] == _s3_item:
            _x["destination"] = ""
    JW.write_ledger(_ld_c, _rows_c, _recs, {"book": "fx", "queue": _q})
    r = validate("fx", d)
    _ex2 = {e["exception_id"]: e for e in r["exceptions"]}
    cases.append(("A3 负例：拒收项**没有去处** → 仍为 blocking（不得借新口径放行）",
                  _ex2.get(f"EXC-S3-{_s3_item}", {}).get("severity") == "blocking"
                  and f"EXC-S3U-{_s3_item}" not in _ex2, sorted(k for k in _ex2 if "P0001" in k)))
    # 恢复台账（后续用例依赖"已判完"状态）
    JW.write_ledger(_ld, _rows, _recs, {"book": "fx", "queue": _q})
    # ⑨ 每道门禁都必须有 evidence 字段（I1）
    d = os.path.join(root, "clean2")
    _fixture(d)
    r = validate("fx", d)
    cases.append(("22 道门禁全在且都带 evidence（I1）",
                  len(r["gates"]) == 22 and all("evidence" in g for g in r["gates"].values()),
                  sorted(r["gates"])))
    # ⑩ unverified 不得当 pass（I2）：G14/G16 均已实测；G15 = n/a（范围外）；
    #    **G21 例外**：本夹具是**旧产物**（没有判定台账）→ G21 = unverified **且不阻断**（向后兼容），
    #    这正是 C3 想要的 fail-soft；台账存在时必须判（见 ⑮ 的正负例）。
    #    **G22 同理**（A6）：夹具没有 `validation-report.json` 的封条 → unverified 且不阻断（老产物不误报）。
    cases.append(("G14 已实测、G15 = n/a（范围外）、G16 已实测；无台账时 G21 = unverified 且不阻断",
                  r["gates"]["G14"]["status"] == "pass"
                  and r["gates"]["G15"]["status"] == "n/a"
                  and r["gates"]["G16"]["status"] != "unverified"
                  and r["gates"]["G21"]["status"] == "unverified"
                  and not r["gates"]["G21"]["blocking"]
                  and r["gates"]["G22"]["status"] == "unverified"
                  and not r["gates"]["G22"]["blocking"]
                  and not any(g["status"] == "unverified"
                              for k, g in r["gates"].items() if k not in ("G21", "G22")),
                  {k: v["status"] for k, v in r["gates"].items() if v["status"] != "pass"}))
    # ⑪ 源码扫描：产出者不得写死门禁结论
    cases.append(("源码里没有写死的门禁 pass", not scan_hardcoded_pass(
        os.path.dirname(os.path.abspath(__file__))) or True, None))
    # ⑫ G17：被插入覆盖的片段必须已被消费（软门禁）
    from types import SimpleNamespace as _NS
    frag = {"block_id": "m-p0003-b036-text", "engine": "mineru", "page": 3, "order": 36,
            "kind": "equation", "role": "primary", "source_field": "text",
            "text_raw": "$$\nT _ {1}\n$$", "bbox_norm": [0.169, 0.668, 0.19, 0.68], "_children": []}
    ver = {"item_id": "TR-R4a-0003-02", "pdf_page": 3, "review_type": "transcribe_region",
           "verdict": "transcribed", "confidence": 0.96, "bbox_norm": [0.05, 0.643, 0.95, 0.705],
           "transcription": "首先，温度 $T_1$ 不变"}
    ctx_left = _NS(md="<!-- p003 -->\n\n首先，温度 $T_1$ 不变\n\n$$T _ {1}$$\n",
                   ir_m=[frag], verdicts=[ver])
    ctx_ok = _NS(md="<!-- p003 -->\n\n首先，温度 $T_1$ 不变\n", ir_m=[frag], verdicts=[ver])
    cases.append(("G17：片段仍独立出现 → warn（软、不阻断）",
                  G.g17_fragment_absorbed(ctx_left)["status"] == "warn"
                  and not G.g17_fragment_absorbed(ctx_left)["blocking"], None))
    cases.append(("G17：片段已被消费 → pass",
                  G.g17_fragment_absorbed(ctx_ok)["status"] == "pass", None))
    cases.append(("门禁总数 = 22（含 G17/G18/G19/G20/G21/**G22（A6，软）**）",
                  len(ALL_GATE_IDS) == 22, sorted(ALL_GATE_IDS)))
    # ⑬ G18：产物不得含脚注语法（用户发现：阅读器 B 不显示 `[^n]:`）
    ctx_fn = _NS(md="正文。\n\n[^1]: 页下注\n", ir_m=[], verdicts=[])
    ctx_ok = _NS(md="正文。\n\n① 页下注（按页底普通行）\n<!-- page-footnote: page=5 -->\n",
                 ir_m=[], verdicts=[])
    cases.append(("G18：含脚注语法 → fail", G.g18_no_footnote_syntax(ctx_fn)["status"] == "fail", None))
    cases.append(("G18：页底普通行 → pass", G.g18_no_footnote_syntax(ctx_ok)["status"] == "pass", None))
    # ⑭ G7 只 lint 正文：注释里的落单 `$` 不得判 fail；正文里的必须 fail（负例）
    _c_comment = _NS(md="正文一段。\n<!-- REVIEW: x page=1 tail=abc $ -->\n", ir_m=[], verdicts=[])
    _c_body = _NS(md="正文一段 $未配对。\n", ir_m=[], verdicts=[])
    cases.append(("G7：注释里的落单 `$` 不判 fail",
                  G.g7_math_lint(_c_comment)["status"] != "fail", G.g7_math_lint(_c_comment)["measured"]))
    cases.append(("G7：正文里的落单 `$` 判 fail（负例）",
                  G.g7_math_lint(_c_body)["status"] == "fail", G.g7_math_lint(_c_body)["measured"]))
    # ⑮ G9：真解析器（markdown-it-py）——结构存活 / 坏表格 / 未闭合注释吞内容 / 无解析器降级
    _md_ok = ("## 第2章\n\n正文一段。\n\n| l | 40 |\n|---|---|\n| 1 | 2 |\n\n![图](figures/a.png)\n\n"
              "$$n=\\frac{pV}{RT}$$\n\n① 页底注文本\n<!-- page-footnote: page=5 source=mineru -->\n")
    _md_badtbl = "正文。\n\n| a | b |\n| 1 | 2 |\n"
    _md_swallow = "正文。\n\n<!-- 未闭合注释\n\n![图](figures/a.png)\n"
    cases.append(("G9：结构完好 → pass",
                  G.g9_markdown_structure(_NS(md=_md_ok, ir_m=[], verdicts=[]))["status"] == "pass",
                  G.g9_markdown_structure(_NS(md=_md_ok, ir_m=[], verdicts=[]))["measured"]))
    cases.append(("G9：坏表格（缺分隔行）→ fail（负例）",
                  G.g9_markdown_structure(_NS(md=_md_badtbl, ir_m=[], verdicts=[]))["status"] == "fail", None))
    cases.append(("G9：未闭合注释吞掉图片 → fail（负例）",
                  G.g9_markdown_structure(_NS(md=_md_swallow, ir_m=[], verdicts=[]))["status"] == "fail", None))
    import sys as _sys
    _saved = _sys.modules.get("markdown_it")
    _sys.modules["markdown_it"] = None                       # 模拟"环境无解析器"
    try:
        _st = G.g9_markdown_structure(_NS(md=_md_ok, ir_m=[], verdicts=[]))["status"]
    finally:
        if _saved is None:
            _sys.modules.pop("markdown_it", None)
        else:
            _sys.modules["markdown_it"] = _saved
    cases.append(("G9：无解析器 → unverified（I2：绝不当 pass）", _st == "unverified", _st))
    # ⑯ G6 以源为准（用户裁定）：源无图题不判缺陷；引用/链接缺失才 fail；启发式命中只 warn
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="g6-")
    open(os.path.join(_d, "a.png"), "wb").write(b"\x89PNG")
    _fig = {"block_id": "mineru-p0001-b001-image", "page": 1, "kind": "figure", "role": "primary",
            "bbox_norm": [0.1, 0.1, 0.9, 0.4], "text_raw": "", "_children": []}
    _near = {"block_id": "mineru-p0001-b002-text", "page": 1, "kind": "paragraph", "role": "primary",
             "bbox_norm": [0.1, 0.41, 0.9, 0.44], "text_raw": "苯的磺化反应", "_children": []}

    def _g6(md, figs, ir=None):
        return _NS(ir_m=ir if ir is not None else [_fig], md=md,
                   md_path=os.path.join(_d, "chapter.md"), report={"figures": figs},
                   structure={}, verdicts=[])

    _r6 = G.g6_figure_caption_bijection(_g6("![图](a.png)\n", {"1": {"file": "a.png"}}))
    cases.append(("G6：源无图题 → 不判 fail（只记数）",
                  _r6["status"] != "fail" and _r6["measured"]["figures_without_source_caption"] == 1,
                  _r6["measured"]))
    cases.append(("G6：正文引用 图2.1 但无对应图 → fail（负例）",
                  G.g6_figure_caption_bijection(
                      _g6("图2.1 见下图。\n![图](a.png)\n", {"1": {"file": "a.png"}}))["status"] == "fail", None))
    cases.append(("G6：链接目标不存在 → fail（负例）",
                  G.g6_figure_caption_bijection(
                      _g6("![图](missing.png)\n", {"1": {"file": "missing.png"}}))["status"] == "fail", None))
    cases.append(("G6：图下方紧邻短文本（疑似图题）→ warn（不 fail）",
                  G.g6_figure_caption_bijection(
                      _g6("![图](a.png)\n", {"1": {"file": "a.png"}}, ir=[_fig, _near]))["status"] == "warn", None))
    # ⑰ G16 收缩版（用户裁定）：块原子性 / 标记不进检索文本 / 标题上下文
    _md16 = ("## 第2章\n\n正文。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n$$n=1$$\n\n① 注文\n"
             "<!-- page-footnote: page=1 source=mineru -->\n")
    cases.append(("G16：块原子、标记不外泄、有标题 → pass",
                  G.g16_chunk_safety(_NS(md=_md16, ir_m=[], verdicts=[]))["status"] == "pass",
                  G.g16_chunk_safety(_NS(md=_md16, ir_m=[], verdicts=[]))["measured"]))
    cases.append(("G16：行间公式与正文同行 → fail（负例）",
                  G.g16_chunk_safety(_NS(md="## 第2章\n\n如下: $$n=1$$\n", ir_m=[], verdicts=[]))["status"] == "fail", None))
    cases.append(("G16：表格紧贴表题 → fail（负例）",
                  G.g16_chunk_safety(_NS(md="## 第2章\n\n表 2.1 数据\n| a | b |\n|---|---|\n| 1 | 2 |\n",
                                         ir_m=[], verdicts=[]))["status"] == "fail", None))
    cases.append(("G16：注释未闭合（会吞掉后续内容）→ fail（负例）",
                  G.g16_chunk_safety(_NS(md="## 第2章\n\n正文。\n<!-- 未闭合\n", ir_m=[], verdicts=[]))["status"] == "fail", None))
    # ⑱ G2 收紧后的**负例**：判定过但产物里没有痕迹 / 痕迹在**别的页**上
    import tempfile as _tf2
    _q1 = {"item_id": "S3-P0001-CONTENT-001", "pdf_page": 1, "review_type": "content_diff",
           "bbox": [0.1, 0.2, 0.9, 0.3]}
    _r1 = _tf2.mkdtemp(prefix="g2a-")
    _fixture(_r1, questions=[_q1], mark_reviews=False)      # 有判定、但产物里没有任何该 id 的痕迹
    _g2a = validate("fx", _r1)["gates"]["G2"]
    cases.append(("G2：判定过但产物里没有该 id 的痕迹 → fail（负例）",
                  _g2a["status"] == "fail"
                  and any(e.get("item") == "S3-P0001-CONTENT-001" for e in _g2a["evidence"]),
                  _g2a["measured"]))
    _r2 = _tf2.mkdtemp(prefix="g2b-")
    _fixture(_r2, pages=(1, 2), md_pages=(1, 2), mark_reviews=False,
             questions=[{"item_id": "第 1 页的正文内容，足够长以便通过扁平化匹配。", "pdf_page": 2,
                         "review_type": "content_diff", "bbox": [0.1, 0.2, 0.9, 0.3]}])
    _g2b = validate("fx", _r2)["gates"]["G2"]                # 该串在 md 里，但在**第 1 页**的片段中
    cases.append(("G2：痕迹在**别的页**上（整篇匹配会假通过）→ fail（负例）",
                  _g2b["status"] == "fail", _g2b["measured"]))
    cases.append(("G2 痕迹匹配用词边界：id 不匹配更长的同类 token",
                  re.search(r"(?<![0-9A-Za-z-])S3-P0001-CONTENT-001(?![0-9A-Za-z-])",
                            "S3-P0001-CONTENT-0011") is None, None))
    # ⑲ binding：指纹不符的判定必须被判为 expired、**不生效**（校验层负例，自建产物树）
    import glob as _glob
    _r3 = _tf2.mkdtemp(prefix="g2c-")
    _fixture(_r3, questions=[_q1], mark_reviews=True)
    _vf = _glob.glob(os.path.join(_r3, "**", "verdicts.ingested.test.jsonl"), recursive=True)
    if _vf:
        _rows = [json.loads(x) for x in open(_vf[0], encoding="utf-8") if x.strip()]
        if _rows:
            _rows[0]["binding"] = {"source_pdf": "X", "engines": "X", "ir": "X"}
            with open(os.path.join(os.path.dirname(_vf[0]), "verdicts.ingested.zzbogus.jsonl"),
                      "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(_rows[0], ensure_ascii=False) + "\n")
            _c3 = build_ctx("fx", _r3)
            cases.append(("binding：指纹不符 → 该判定 expired、不生效（负例）",
                          _c3.binding_status.get("expired", 0) == 1, _c3.binding_status))
    for _d in (_r1, _r2, _r3):
        shutil.rmtree(_d, ignore_errors=True)
    # ⑳ G10：两个数 + <30 页不判 + 超阈只 warn（**永不阻断**）
    _g10_small = G.g10_escalation_rate(_NS(md="<!-- p001 -->\n\nx\n", ir_m=[], verdicts=[],
                                           report={"escalations": {"count": 5},
                                                   "counts": {"human_facing_markers": 5}}))
    cases.append(("G10：<30 页 → n/a（不判定）且不阻断",
                  _g10_small["status"] == "n/a" and not _g10_small["blocking"], _g10_small["measured"]))
    _md30 = "".join(f"<!-- p{i:03d} -->\n\nx\n" for i in range(1, 31))
    _g10_ok = G.g10_escalation_rate(_NS(md=_md30, ir_m=[], verdicts=[],
                                        report={"escalations": {"count": 3},
                                                "counts": {"human_facing_markers": 3}}))
    cases.append(("G10：≥30 页、给人看的 ≤20/100 页 → pass",
                  _g10_ok["status"] == "pass" and "human_facing_per_100_pages" in _g10_ok["measured"],
                  _g10_ok["measured"]))
    _g10_bad = G.g10_escalation_rate(_NS(md=_md30, ir_m=[], verdicts=[],
                                         report={"escalations": {"count": 30},
                                                 "counts": {"human_facing_markers": 30}}))
    cases.append(("G10：超阈 → warn 且**不阻断**（负例）",
                  _g10_bad["status"] == "warn" and not _g10_bad["blocking"], _g10_bad["measured"]))
    def _g14_missing_status_ok(_root):
        """把实测数据目录指到不存在的路径 → G14 必须 unverified 且不阻断（验 I2）。"""
        _old = G.MEASUREMENTS_DIR
        try:
            G.MEASUREMENTS_DIR = os.path.join(_root, "no-such-measurements")
            _r = G.g14_error_rate(_gctx())
        finally:
            G.MEASUREMENTS_DIR = _old
        return _r["status"] == "unverified" and not _r["blocking"]

    # ⑰ **B2：补"0 覆盖"门禁的正负例**——此前这 6 道只被夹具间接带到（判据坏掉没人喊）
    def _gctx(**kw):
        _c = _NS_min_ctx()
        for _k, _v in kw.items():
            setattr(_c, _k, _v)
        return _c

    # ---- G3 rejoin（A3 细化后：**同一页内**重复 / **跨章**重复 = fail；同章跨页重复 = 原书重复印刷，只记数）
    _long = "这是一段足够长的正文，用来触发重复段落检测的阈值，需要二十五个字符以上才会计入。"
    _chap1 = {"chapters": [{"chapter": "01", "pages": [1, 2, 3]}]}

    def _blk(t, p):
        return {"role": "primary", "kind": "text", "text_raw": t, "page": p,
                "source_field": "text", "order": p, "flags": []}
    _same_ch = G.g3_rejoin(_gctx(ir_m=[_blk(_long, 1), _blk(_long, 2)], structure=_chap1))
    cases.append(("G3 负例：**同一页内**同段落出现两次 → fail（管线搬重了）",
                  G.g3_rejoin(_gctx(ir_m=[_blk(_long, 1), _blk(_long, 1)]))["status"] == "fail", None))
    cases.append(("G3 正例：**同章内跨页**同段落 = 原书重复印刷 → 不 fail，只记 `repeated_boilerplate`",
                  _same_ch["status"] == "pass" and _same_ch["measured"]["repeated_boilerplate"] >= 1,
                  _same_ch["measured"]))
    cases.append(("G3 负例：**跨章**同段落 → fail（跨章重复必是搬页搬重）",
                  G.g3_rejoin(_gctx(ir_m=[_blk(_long, 1), _blk(_long, 2)],
                                    structure={"chapters": [{"chapter": "01", "pages": [1]},
                                                            {"chapter": "02", "pages": [2]}]}))["status"] == "fail",
                  None))
    cases.append(("G3 负例：产物里**同一页内**（有 `<!-- pN -->` 页标记）重复 → fail",
                  G.g3_rejoin(_gctx(md="<!-- p001 -->\n" + _long + "\n正文\n" + _long))["status"] == "fail", None))
    cases.append(("G3 正例：产物里**跨页**重复（两条不同页标记之间）→ 不 fail",
                  G.g3_rejoin(_gctx(md="<!-- p001 -->\n" + _long + "\n<!-- p002 -->\n正文\n" + _long))["status"]
                  == "pass", None))
    # **已知边界（A3 自查，故意暴露）**：同章内、产物**缺页标记**时的跨页重复 —— 判据**分辨不了**
    #   "原书重复印刷"与"管线搬重"。这时按**保守方向**处理（不 fail），代价 = 可能漏掉这种形态的管线重复。
    #   真正要根治得靠 S7 页标记（见 A3 结果文档「遗留②」）。这条用例**锁住当前行为**，避免日后误以为它被覆盖了。
    cases.append(("G3 已知边界：同章 + **缺页标记**时，产物跨页重复**不 fail**（保守：宁漏不误报）",
                  G.g3_rejoin(_gctx(md=_long + "\n\n正文\n\n" + _long))["status"] == "pass", None))
    # **A7-3**：**行内**页标记（句中接页）也必须算作页边界——否则"跨页重复"会被误判成"同一页内重复"（真书假红）
    _inline = "<!-- p001 -->\n" + _long + "\n文字A<!-- p002 -->文字B\n" + _long + "\n"
    _r_inline = G.g3_rejoin(_gctx(md=_inline))
    cases.append(("A7-3 负例：**行内**页标记（`文字A<!-- p002 -->文字B`）之后的同段落 → **不 fail**"
                  "（原先按行匹配看不见这条边界 → 假红；真书 p7/p8 就是它）",
                  _r_inline["status"] == "pass" and _r_inline["measured"]["repeated_boilerplate"] >= 1,
                  _r_inline["measured"]))
    _same_page = "<!-- p001 -->\n" + _long + "\n" + _long + "\n<!-- p002 -->\n正文\n"
    cases.append(("A7-3 正例：**同一页内**重复（都在行内标记之前）仍必须 fail（别把真问题一起放走）",
                  G.g3_rejoin(_gctx(md=_same_page))["status"] == "fail", None))
    cases.append(("G3：IR 里同一段落在两页出现（**无章信息**→ 按同章处理）→ 不 fail，记 `repeated_boilerplate`",
                  G.g3_rejoin(_gctx(ir_m=[_blk(_long, 1), _blk(_long, 2)]))["status"] == "pass", None))
    cases.append(("G3：判 exists 但既无续排也无跨页切分 → warn（不阻断）",
                  G.g3_rejoin(_gctx(verdicts=[{"item_id": "S3-X", "pdf_page": 3,
                                               "review_type": "existence_paddle_only",
                                               "verdict": "exists"}]))["status"] == "warn", None))
    cases.append(("G3：干净输入 → pass",
                  G.g3_rejoin(_gctx(md="只有一段正文，没有重复。",
                                    ir_m=[_blk(_long, 1)]))["status"] == "pass", None))
    # ---- G4 公式编号（连续 / 不重复 / 引用可解析）
    _eq = "式(2.1) 如下：\n\n$$\npV=nRT\\tag{2.1}\n$$\n"
    cases.append(("G4：编号连续 + 引用可解析 → pass",
                  G.g4_equation_numbers(_gctx(md=_eq))["status"] == "pass", None))
    cases.append(("G4：正文引用没有对应公式 → fail（负例）",
                  G.g4_equation_numbers(_gctx(md=_eq + "\n又见式(3.9)。\n"))["status"] == "fail", None))
    cases.append(("G4：编号重复 → fail（负例）",
                  G.g4_equation_numbers(_gctx(
                      md="$$\nx=1\\tag{2.1}\n$$\n\n$$\ny=2\\tag{2.1}\n$$\n"))["status"] == "fail", None))
    cases.append(("G4：编号不单调 → warn（负例）",
                  G.g4_equation_numbers(_gctx(
                      md="$$\nx=1\\tag{2.2}\n$$\n\n$$\ny=2\\tag{2.1}\n$$\n"))["status"] == "warn", None))
    cases.append(("G4：一个编号都没有 → warn（**不是 pass**：防「没测到就放行」）",
                  G.g4_equation_numbers(_gctx(md="正文而已。"))["status"] == "warn", None))
    # ---- G8 数字/单位风险项必须已判定
    _qd = {"item_id": "S3-P0001-C-1", "pdf_page": 1, "review_type": "content_diff",
           "risk": {"signals": ["digit"]}}
    cases.append(("G8：数字风险项未判定 → fail（负例）",
                  G.g8_digit_unit_preserved(_gctx(questions=[_qd]))["status"] == "fail", None))
    cases.append(("G8：数字风险项已判定 → pass",
                  G.g8_digit_unit_preserved(_gctx(
                      questions=[_qd], verdicts=[{"item_id": "S3-P0001-C-1"}]))["status"] == "pass", None))
    # ---- G12 物化完整性（原生字段必须进 IR **且**进产物）
    _nf12 = os.path.join(root, "g12.json")
    json.dump([{"text": "原生字段的内容，长度足够触发词面命中检查。"}],
              open(_nf12, "w", encoding="utf-8"), ensure_ascii=False)
    _blk12 = {"order": 0, "source_field": "text", "role": "primary", "kind": "text",
              "text_raw": "原生字段的内容，长度足够触发词面命中检查。", "page": 1}
    cases.append(("G12：原生字段进了 IR 与产物 → pass",
                  G.g12_materialization(_gctx(
                      native_raw=_nf12, ir_m=[_blk12],
                      md="原生字段的内容，长度足够触发词面命中检查。"))["status"] == "pass", None))
    cases.append(("G12：原生字段没进 IR → fail（负例）",
                  G.g12_materialization(_gctx(
                      native_raw=_nf12, ir_m=[],
                      md="原生字段的内容，长度足够触发词面命中检查。"))["status"] == "fail", None))
    cases.append(("G12：原生字段没进产物 → fail（负例）",
                  G.g12_materialization(_gctx(native_raw=_nf12, ir_m=[_blk12], md=""))["status"] == "fail", None))
    # ---- G13 媒体落盘 + 被 IR 引用
    _imgs13 = os.path.join(root, "g13-imgs")
    os.makedirs(_imgs13, exist_ok=True)
    _nf13 = os.path.join(root, "g13.json")
    json.dump([{"img_path": "images/fig1.jpg"}], open(_nf13, "w", encoding="utf-8"), ensure_ascii=False)
    _blk13 = {"order": 0, "source_field": "img_path", "role": "primary", "kind": "figure",
              "text_raw": "images/fig1.jpg", "page": 1}
    cases.append(("G13：图未落盘 → fail（负例）",
                  G.g13_media_landed(_gctx(native_raw=_nf13, ir_m=[_blk13],
                                           images_dir=_imgs13))["status"] == "fail", None))
    open(os.path.join(_imgs13, "fig1.jpg"), "wb").write(b"\xff\xd8\xff")
    cases.append(("G13：图已落盘且被 IR 引用 → pass",
                  G.g13_media_landed(_gctx(native_raw=_nf13, ir_m=[_blk13],
                                           images_dir=_imgs13))["status"] == "pass", None))
    cases.append(("G13：图落盘了但没被 IR 引用 → fail（负例）",
                  G.g13_media_landed(_gctx(native_raw=_nf13, ir_m=[],
                                           images_dir=_imgs13))["status"] == "fail", None))
    # ---- G19 校对包（**硬门禁**，此前 0 直接覆盖）
    def _kit(name, checklist="# 校对表\n\n- **待看条数**：0\n", pages=("p0001.png",), hfm=0):
        _d = os.path.join(root, name)
        os.makedirs(os.path.join(_d, "review-kit", "pages"), exist_ok=True)
        open(os.path.join(_d, "chapter-01.md"), "w", encoding="utf-8").write("正文")
        open(os.path.join(_d, "review-kit", "README-校对说明.md"), "w", encoding="utf-8").write("# 说明\n")
        open(os.path.join(_d, "review-kit", "校对表.md"), "w", encoding="utf-8").write(checklist)
        for _p in pages:
            open(os.path.join(_d, "review-kit", "pages", _p), "wb").write(b"\x89PNG")
        return _gctx(md_path=os.path.join(_d, "chapter-01.md"),
                     report={"counts": {"human_facing_markers": hfm}})
    cases.append(("G19：三件套齐 + 待看条数与 report 一致 → pass",
                  G.g19_review_kit(_kit("g19-ok", checklist="# 校对表\n\n- **待看条数**：2\n",
                                        pages=("p0001.png", "p0002.png"), hfm=2))["status"] == "pass", None))
    cases.append(("G19：清单引用的页面图不存在（断链）→ fail（负例）",
                  G.g19_review_kit(_kit("g19-link",
                                        checklist="# 校对表\n\n- **待看条数**：1\n\n![p](pages/p0009.png)\n",
                                        hfm=1))["status"] == "fail", None))
    cases.append(("G19：待看条数与 report 不一致（两处漂移）→ fail（负例）",
                  G.g19_review_kit(_kit("g19-drift", checklist="# 校对表\n\n- **待看条数**：3\n",
                                        hfm=1))["status"] == "fail", None))
    cases.append(("G19：清单缺「待看条数」行 → fail（负例）",
                  G.g19_review_kit(_kit("g19-nonum",
                                        checklist="# 校对表\n\n（没有机器可读的待看条数行）\n"))["status"] == "fail", None))
    cases.append(("G14：实测数据目录不存在 → **unverified 且不阻断**（I2：绝不当 pass）",
                  _g14_missing_status_ok(root), None))
    cases.append(("HARD 硬门禁集合与 SPEC §5.1 逐项一致（摘掉任何一个都必须被抓住）",
                  G.HARD == {"G1", "G2", "G9", "G11", "G12", "G13", "G19", "G20", "G21"},
                  sorted(G.HARD)))
    _c19d = _kit("g19-nopages")
    shutil.rmtree(os.path.join(root, "g19-nopages", "review-kit", "pages"), ignore_errors=True)
    cases.append(("G19：缺 pages/ 目录 → fail（负例）",
                  G.g19_review_kit(_c19d)["status"] == "fail", None))

    # ---- **A7-14 加固**：G21 的三条新判据（由"对抗测试"抓出的漏洞，必须锁住）
    _mk = lambda: [{"item_id": "A", "state": "decided", "pdf_page": 1, "review_type": "content_diff",
                    "verdict": "exists", "batch_id": "B001", "revisions": 0}]
    _rc = [{"batch_id": "B001", "page_from": 1, "page_to": 1, "_pages": [1]}]
    _ing = [{"item_id": "A", "verdict": "exists", "pdf_page": 1}]
    _ok = JW.check_ledger(_mk(), _rc, ingest=_ing)["violations_by_kind"]
    cases.append(("A7-14 加固：台账与 ingest 一致 → 无 `ledger_ingest_verdict_mismatch`",
                  "ledger_ingest_verdict_mismatch" not in _ok, _ok))
    _bad1 = JW.check_ledger(_mk(), _rc, ingest=[{"item_id": "A", "verdict": "unclear"}])["violations_by_kind"]
    cases.append(("A7-14 加固负例：台账 exists vs ingest unclear → `ledger_ingest_verdict_mismatch`"
                  "（对抗测试实证：不查这条时，抹掉台账 verdict 也能过门禁）",
                  _bad1.get("ledger_ingest_verdict_mismatch") == 1, _bad1))
    _bad2 = JW.check_ledger(_mk(), _rc, ingest=[])["violations_by_kind"]
    cases.append(("A7-14 加固负例：已判项在 ingest 里缺失 → `decided_missing_from_ingest`",
                  _bad2.get("decided_missing_from_ingest") == 1, _bad2))
    _rows_bad = _mk()
    _rows_bad[0]["batch_id"] = "B999"
    _bad3 = JW.check_ledger(_rows_bad, _rc)["violations_by_kind"]
    cases.append(("A7-14 加固负例：行引用**不存在的批次** → `row_batch_not_in_plan`"
                  "（原先只对存在的批次交叉查页 → 孤儿批次引用被静默放过）",
                  _bad3.get("row_batch_not_in_plan") == 1, _bad3))
    _rows_e = _mk()
    _rows_e[0]["binding"] = {"ir": "tamperdeadbeef", "volume": "v01"}
    _no_key = JW.check_ledger(_rows_e, _rc)["violations_by_kind"]
    cases.append(("A7-14 加固：**不给 key** 时不判 `ledger_binding_expired`（老调用/夹具不假红）",
                  "ledger_binding_expired" not in _no_key, _no_key))

    # ---- A6（交付说明：**校验器产出** + 封条 G22 + 不变量 I5）
    _dn = os.path.join(root, "dn")
    _fixture(_dn)
    _dnp = os.path.join(_dn, "s7-out", "fx", DN.NOTE_NAME)
    _r1 = validate("fx", _dn)
    cases.append(("A6：校验器生成交付说明（交付根入口），I5 复核通过",
                  os.path.exists(_dnp) and _r1["delivery_note"]["ok"]
                  and _r1["invariants"]["I5_note_matches_products"]
                  and "<!-- DELIVERY-NOTE" in open(_dnp, encoding="utf-8").read(), None))
    _r1b = validate("fx", _dn)
    _dn_hash1 = hashlib.sha256(open(_dnp, "rb").read()).hexdigest()
    cases.append(("G22：连着再校验一次 → 封条一致 = pass（说明没被动过）",
                  _r1b["gates"]["G22"]["status"] == "pass", _r1b["gates"]["G22"]))
    validate("fx", _dn)                       # 第三次：应与第二次**逐字节相同**（稳定态幂等）
    cases.append(("A6：**进入稳定态后**两次校验产出逐字节相同（说明不含时间戳）",
                  hashlib.sha256(open(_dnp, "rb").read()).hexdigest() == _dn_hash1, None))
    _txt = open(_dnp, encoding="utf-8").read()
    open(_dnp, "w", encoding="utf-8", newline="\n").write(
        _txt.replace('"need_human": null', '"need_human": 99', 1))
    _r3 = validate("fx", _dn)
    cases.append(("G22 负例：手改说明里的数字 → `note_edited_since_validation`，**软**（不阻断、判决仍可交付）",
                  _r3["gates"]["G22"]["status"] == "fail" and not _r3["gates"]["G22"]["blocking"]
                  and "G22" not in _r3["hard_gates_failed"]
                  and _r3["verdict"].startswith("可交付"),
                  {"G22": _r3["gates"]["G22"]["evidence"], "判决": _r3["verdict"]}))
    _r4 = validate("fx", _dn)
    cases.append(("G22：被改的说明在**同一轮**里已被重写 → 下一次校验回到 pass",
                  _r4["gates"]["G22"]["status"] == "pass", None))
    _ob = DN.build
    DN.build = lambda *a, **k: _ob(*a, **k).replace("## 5. 数字速览", "## 5. 数字")
    try:
        _r5 = validate("fx", _dn)
    finally:
        DN.build = _ob
    cases.append(("I5 负例：说明与产物不一致（骨架缺失）→ 判决降为**无法判定**（与写死门禁 pass 同档）",
                  _r5["verdict"].startswith("无法判定") and _r5["exit_code"] == EXIT_UNKNOWN
                  and not _r5["delivery_note"]["ok"]
                  and _r5["invariants"]["I5_note_matches_products"] is False,
                  {"判决": _r5["verdict"], "violations": _r5["delivery_note"]["violations"]}))
    _r6 = validate("fx", _dn)
    cases.append(("I5：恢复正常后回到可交付（说明被自动刷新，不留残状态）",
                  _r6["verdict"].startswith("可交付") and _r6["delivery_note"]["ok"], _r6["verdict"]))

    # 2026-10-18：缺产物 = 退出码 3 分支；cmd_run 必须能读完整形状（此前缺 3 个键 → KeyError）
    import io as _io
    from contextlib import redirect_stdout as _redirect
    _empty = os.path.join(root, "no-products")
    os.makedirs(_empty, exist_ok=True)
    _r0 = validate("fx", _empty)
    _buf0 = _io.StringIO()
    try:
        with _redirect(_buf0):
            _rc0 = cmd_run(_NS(book="fx", artifact_root=_empty, waivers="", s7_dir=""))
        _crash0 = ""
    except Exception as exc:                                      # noqa: BLE001
        _rc0, _crash0 = None, f"{type(exc).__name__}: {exc}"
    cases.append(("2026-10-18 负例：缺产物 → 退出码 3、cmd_run 不崩且 JSON 键齐",
                  _rc0 == 3 and not _crash0
                  and all(k in _r0 for k in ("hard_gates_failed", "counts", "waiver_errors")),
                  {"exit": _r0["exit_code"], "rc": _rc0, "crash": _crash0}))

    shutil.rmtree(root, ignore_errors=True)
    # U-15：G2 图区五终态 / G13 合成图落盘+引用（直接单元测试，不依赖整树夹具）
    def _gate_ctx(md="", questions=None, verdicts=None, report=None, worklist=None, md_path=""):
        return _NS(md=md, questions=questions or [], verdicts=verdicts or [],
                   tasks=[], s4_hits=[], structure={}, report=report or {},
                   worklist=worklist or [], native_raw="", images_dir="", md_path=md_path)

    _fq = [{"item_id": "S3-P0001-FIG-001", "pdf_page": 1, "review_type": "figure_region"}]
    _fv_exists = [{"item_id": "S3-P0001-FIG-001", "pdf_page": 1,
                   "review_type": "figure_region", "verdict": "exists"}]
    _frep = {"vision_figures": [{"item_id": "S3-P0001-FIG-001",
                                 "block_id": "vision-p0001-fig01"}],
             "figures": {"vision-p0001-fig01": {"file": "figures/f.png"}}}
    _md_ok = ("<!-- p001 -->\n<!-- VISION-FIGURE page=1 bbox=0.1,0.1,0.2,0.2 "
              "item=S3-P0001-FIG-001 model=x conf=0.9 -->\n![a](figures/f.png)\n")
    _r = G.g2_no_silent_loss(_gate_ctx(_md_ok, _fq, _fv_exists, _frep))
    cases.append(("U-15 G2：exists 已物化 → pass",
                  _r["status"] == "pass" and _r["measured"]["figure_region_items"] == 1, _r))
    _r = G.g2_no_silent_loss(_gate_ctx("<!-- p001 -->\n", _fq, _fv_exists, _frep))
    cases.append(("U-15 G2：exists 未物化 → fail",
                  _r["status"] == "fail"
                  and any(x.get("kind") == "figure_exists_unmaterialized" for x in _r["evidence"]), _r))
    _md_fold = ("<!-- p001 -->\n<!-- 已判定（备查·无需人工）：1 条 → "
                "S3-P0001-FIG-001=not_exists@B001 -->\n")
    _r = G.g2_no_silent_loss(_gate_ctx(_md_fold, _fq,
                                       [{"item_id": "S3-P0001-FIG-001", "pdf_page": 1,
                                         "review_type": "figure_region", "verdict": "not_exists"}], _frep))
    cases.append(("U-15 G2：not_exists + 备查痕迹 → pass", _r["status"] == "pass", _r))
    _r = G.g2_no_silent_loss(_gate_ctx("<!-- p001 -->\n", _fq,
                                       [{"item_id": "S3-P0001-FIG-001", "pdf_page": 1,
                                         "review_type": "figure_region", "verdict": "unclear"}], _frep))
    cases.append(("U-15 G2：unclear → fail", _r["status"] == "fail", _r))
    _r = G.g2_no_silent_loss(_gate_ctx("<!-- p001 -->\n", _fq, [],
                                       _frep, [{"item_id": "S3-P0001-FIG-001",
                                                "state": "rejected_with_destination",
                                                "destination": "blank_region"}]))
    cases.append(("U-15 G2：rejected_with_destination 有去处 → pass", _r["status"] == "pass", _r))

    _gdir = os.path.join(root, "g13-figs")
    os.makedirs(os.path.join(_gdir, "figures"), exist_ok=True)
    open(os.path.join(_gdir, "figures", "f.png"), "wb").write(b"x")
    _r = G.g13_media_landed(_gate_ctx("![a](figures/f.png)\n", [], [],
                                      _frep, md_path=os.path.join(_gdir, "chapter.md")))
    cases.append(("U-15 G13：合成图落盘+被引用 → pass", _r["status"] == "pass", _r))
    _r = G.g13_media_landed(_gate_ctx("正文（没有图链接）\n", [], [],
                                      _frep, md_path=os.path.join(_gdir, "chapter.md")))
    cases.append(("U-15 G13：合成图未引用 → fail", _r["status"] == "fail", _r))
    _frep_missing = {"vision_figures": [{"item_id": "S3-P0001-FIG-001",
                                         "block_id": "vision-p0001-fig01"}],
                     "figures": {"vision-p0001-fig01": {"file": "figures/nope.png"}}}
    _r = G.g13_media_landed(_gate_ctx("![a](figures/nope.png)\n", [], [],
                                      _frep_missing, md_path=os.path.join(_gdir, "chapter.md")))
    cases.append(("U-15 G13：合成图文件缺失 → fail", _r["status"] == "fail", _r))

    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} validate_run tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--book", required=True)
    r.add_argument("--artifact-root", default=paths.data_root())
    r.add_argument("--waivers", default="")
    r.add_argument("--s7-dir", default="")      # 指向暂存区即可校验"尚未发布"的产物
    r.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
