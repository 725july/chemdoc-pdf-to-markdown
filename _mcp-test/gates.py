# -*- coding: utf-8 -*-
"""22 道门禁的实现（SPEC v2 §5）。**判决权归独立校验器，产出者不得自判。**

设计约束（用户裁定 U-05 借鉴原 skill）：
  * 每道门禁返回 **status + measured + evidence**；没有证据只能算 `unverified`；
  * `unverified` **永不**当作 pass；`blocking=True` 只表示"**失败**即不得交付"；
  * 代码里**不允许写死的 "pass"**（自测有源码扫描用例）。

硬门禁（失败即不得交付）：G1 / G2 / G9 / G11 / G12 / G13 / G19 / G20 / G21。其余为软门禁，G10 不阻断。
G22（`delivery_note`，软，A6 引入）= **交付说明的封条**（SPEC §3.12）：防"说明自上一轮校验后被改过/被删"。
"""
from __future__ import annotations

import os
import re
from contracts import EQ_TAG_RE, FURNITURE, PAGE_MARK_RE, REVIEW_RE
from tuning import TUNE
import delivery_note as DN

IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
FIGREF_RE = re.compile(r"图\s*(\d+)\s*[.\-–]\s*(\d+)")

# 硬门禁 = 失败即不得交付（SPEC §5.1 是唯一权威）：G19 校对包、G20 章切分、G21 判定台账在本轮补入。
# G20 的 `unverified`（产物来自未启用 c1 的旧版报告）由 G20 自己传 blocking=False，避免"未启用即阻断"。
# G21 同理（产物来自未启用 C3 的旧版报告 → unverified 且 blocking=False）。
HARD = {"G1", "G2", "G9", "G11", "G12", "G13", "G19", "G20", "G21"}

# 实测数据目录：**模块级常量**，好让自测把它指向不存在的目录，
# 验证"无实测记录 → unverified（I2，绝不当 pass）"这条路径真的成立（B2 变异探针发现的缺口）。
MEASUREMENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "measurements")


def _flat(s: str) -> str:
    """极度宽松的扁平化：去掉空白与常见 LaTeX 包装字符，只留可见字符。"""
    return re.sub(r"[\s$\\{}]+", "", s or "")


def _g(gid, name, status, measured, evidence=None, threshold="", reason="", blocking=None):
    return {"id": gid, "name": name, "status": status,
            "blocking": (gid in HARD) if blocking is None else blocking,
            "threshold": threshold, "measured": measured,
            "evidence": evidence or [], "reason": reason}


def page_markers(md: str) -> list[int]:
    return sorted({int(m) for m in PAGE_MARK_RE.findall(md or "")})


HTML_COMMENT_RE = re.compile(r"<!--.*?-->")     # 本地用：判断"页标记前面是不是只有注释"（A7-2）


def marker_placement_violations(md: str) -> list[dict]:
    """**页标记落点检查（A7-2）**：页标记**不得与注释同行**。

    为什么要有它（真书实测）：p8/p26/p34 的页标记被粘在"上一页 `已判定（备查）` 折叠注释行"上
    → **按行切页的读者与工具看不见这条边界**（318→516 行被当成同一页；这正是 A3-5 记的
    "305–510 行整段无页标记"）；根因是"跨页接句"的锚点误取到注释行，于是**后半句被粘进注释行**、
    句中硬断（本机制本来要修的就是这个）。**允许**的形态：① 该行只有页标记；② 页标记与**正文**同行
    （`…进入并<!-- p016 -->登记参赛。`——页边界落在句中时按设计就是行内）。
    """
    out = []
    for lineno, line in enumerate((md or "").splitlines(), 1):
        if not PAGE_MARK_RE.search(line):
            continue
        others = [c for c in HTML_COMMENT_RE.finditer(line)
                  if not PAGE_MARK_RE.fullmatch(c.group(0).strip())]
        if not others:                       # 该行没有别的注释 → 允许（独占一行，或与正文同行）
            continue
        for m in PAGE_MARK_RE.finditer(line):
            out.append({"page": int(m.group(1)), "line": lineno,
                        "kind": "page_marker_glued_to_comment",
                        "why": "页标记与注释同行（贴在注释块上）→ 按行切页看不见这条边界，"
                               "上一页的注释会被算进本页",
                        "text": line.strip()[:120]})
    return out


def review_markers(md: str) -> list[tuple[str, str]]:
    return [(c, rest.strip()) for c, rest in REVIEW_RE.findall(md or "")]


# ------------------------------------------------------------------ G1
def g1_page_coverage(ctx) -> dict:
    exp = sorted({b["page"] for b in ctx.ir_m} | {b["page"] for b in ctx.ir_p})
    if not exp:
        return _g("G1", "page_coverage", "unverified", {"expected_pages": []},
                  reason="两引擎 IR 都没有页信息，无法核对覆盖", threshold="每页至少 1 块或显式声明空白页；100%")
    covered = page_markers(ctx.md)
    declared = set(ctx.structure.get("blank_pages") or [])
    has_content = {p for p in exp
                   if any(b.get("role") == "primary" and b["kind"] not in FURNITURE
                          for b in ctx.ir_m + ctx.ir_p if b["page"] == p)}
    missing = [p for p in exp if p not in covered]
    hard_missing = [p for p in missing if p in has_content]
    soft_missing = [p for p in missing if p not in has_content and p not in declared]
    glued = marker_placement_violations(ctx.md)          # A7-2：标记不得贴在注释块上
    ev = [{"page": p, "why": "两引擎该页有正文块，但产物里没有页边界标记"} for p in hard_missing]
    ev += [{"page": p, "why": "该页无正文块，但也未在 structure.blank_pages 里显式声明"} for p in soft_missing]
    ev += glued[:8]
    status = "fail" if (hard_missing or glued) else ("warn" if soft_missing else "pass")
    return _g("G1", "page_coverage", status,
              {"expected_pages": exp, "covered": covered, "declared_blank": sorted(declared),
               "missing": missing, "hard_missing": hard_missing, "soft_missing": soft_missing,
               "page_marker_glued_to_comment": len(glued), "glued_examples": glued[:5]},
              ev, "每页至少 1 块或显式声明空白页；页标记独占一行或行内且前面是正文；100%")


# ------------------------------------------------------------------ G2
def g2_no_silent_loss(ctx) -> dict:
    """未解决项必须**在产物里可见**（页首 REVIEW 标记）或属于报告级登记项。缺标记 = 静默丢失。"""
    marks = review_markers(ctx.md)
    mark_cats = {c for c, _ in marks}
    need, missing = [], []
    # ① S3 未决问题：必须有 S3_<type> 标记（同页）
    by_page_marks = {}
    judged_ids = {v.get("item_id") for v in (ctx.verdicts or [])}
    # 产物**按页切分**（页标记可能是行内的）→ 用于"该条目**所属页**的痕迹"校验。
    # 只做"整篇子串匹配"是不够的：① 页面归属算错时察觉不到；② 子串命中会假通过。
    _pg = {}
    _parts = re.split(r"<!--\s*p(\d{3,4})\s*-->", ctx.md or "")
    if len(_parts) >= 3:
        _pg[1] = _parts[0]
        for _i in range(1, len(_parts), 2):
            _pg[int(_parts[_i])] = _parts[_i + 1] if _i + 1 < len(_parts) else ""

    def _trace_on_page(item_id, page) -> bool:
        seg = _pg.get(int(page), "")
        return bool(re.search(r"(?<![0-9A-Za-z-])" + re.escape(item_id) + r"(?![0-9A-Za-z-])", seg))

    def _vision_trace_on_page(item_id, page) -> bool:
        """只认 `<!-- VISION-FIGURE … item=… -->`；折叠“备查”块不算物化痕迹。"""
        seg = _pg.get(int(page), "")
        return bool(re.search(r"<!--\s*VISION-FIGURE\b[^>]*\bitem="
                              + re.escape(item_id) + r"(?:\s|-->)", seg))

    _wl = {r.get("item_id"): r for r in (getattr(ctx, "worklist", None) or [])}
    _vmap = {v.get("item_id"): v for v in (ctx.verdicts or [])}
    _fig_qs = [q for q in ctx.questions if q.get("review_type") == "figure_region"]
    _fig_fail = []
    for q in _fig_qs:
        iid, page = q.get("item_id"), q.get("pdf_page")
        led = _wl.get(iid) or {}
        # U-15：S5 拒收且**有去处** = 已处置（G21 再查去处非空）
        if led.get("state") == "rejected_with_destination" and (led.get("destination") or "").strip():
            continue
        v = _vmap.get(iid)
        if v is None:
            _fig_fail.append({"kind": "figure_unjudged", "item": iid, "page": page,
                              "why": "图区候选从未判定"})
            continue
        verdict = v.get("verdict")
        if verdict == "exists":
            materialized = any(x.get("item_id") == iid
                               for x in (ctx.report.get("vision_figures") or []))
            if materialized and _vision_trace_on_page(iid, page):
                continue
            _fig_fail.append({"kind": "figure_exists_unmaterialized", "item": iid, "page": page,
                              "why": "判定 exists 但没有物化（VISION-FIGURE / vision_figures 缺失）"})
        elif verdict == "not_exists":
            if _trace_on_page(iid, page):
                continue
            _fig_fail.append({"kind": "figure_not_exists_no_trace", "item": iid, "page": page,
                              "why": "判定 not_exists 但产物里没有备查痕迹"})
        else:
            _fig_fail.append({"kind": f"figure_{verdict or 'unjudged'}", "item": iid, "page": page,
                              "why": f"图区判定未解决（verdict={verdict}）"})
    for c, rest in marks:
        m = re.search(r"page=(\d+)", rest)
        if m:
            by_page_marks.setdefault(int(m.group(1)), set()).add(c)
    for q in ctx.questions:
        if q.get("review_type") == "figure_region":
            continue                                   # U-15：图区题走上面的专用五终态
        cat = f"S3_{q['review_type']}"
        if cat not in by_page_marks.get(q["pdf_page"], set()):
            # **已判定的问题被折叠到页尾"备查"块**（用户裁定：读者在正文旁只看到真待办）→
            # 只要它的 item_id 仍出现在产物里（折叠块内），就算痕迹在位，不算静默丢失。
            if q["item_id"] in judged_ids and _trace_on_page(q["item_id"], q["pdf_page"]):
                continue
            need.append({"kind": "s3_question", "item": q["item_id"], "page": q["pdf_page"], "want": cat})
    # ② 转写任务未解决：必须有 transcribe_unresolved 标记
    for v in ctx.verdicts:
        if v.get("review_type") == "transcribe_region" and v.get("verdict") not in ("transcribed", "no_text"):
            if "transcribe_unresolved" not in by_page_marks.get(v["pdf_page"], set()):
                need.append({"kind": "transcribe_unresolved", "item": v["item_id"],
                             "page": v["pdf_page"], "want": "transcribe_unresolved"})
    # ③ S4 命中未被任务覆盖：必须有 s4_unresolved_<rule> 标记
    covered = set()
    for t in ctx.tasks:
        iid = t.get("item_id") or ""
        if iid.startswith("TR-"):
            covered.add((iid.split("-")[1], t.get("pdf_page")))
    for h in ctx.s4_hits:
        if h.get("rule") in ("R1", "R4a", "R4b") and (h["rule"], h.get("page")) not in covered:
            cat = f"s4_unresolved_{h['rule']}"
            if cat not in by_page_marks.get(h.get("page"), set()):
                need.append({"kind": "s4_hit_uncovered", "item": f"{h['rule']}@p{h.get('page')}",
                             "page": h.get("page"), "want": cat})
    need.extend(_fig_fail)
    missing = [n for n in need
               if (str(n.get("kind", "")).startswith("figure_")
                   or (n.get("want") not in mark_cats and n.get("want") not in by_page_marks.get(n.get("page"), set())))]
    status = "fail" if missing else "pass"
    return _g("G2", "no_silent_loss", status,
              {"unresolved_items": len(need), "visible": len(need) - len(missing),
               "figure_region_items": len(_fig_qs), "figure_region_missing": len(_fig_fail),
               "marker_categories": sorted(mark_cats)},
              missing, "未解决项 100% 在产物里可见")


# ------------------------------------------------------------------ G3
def _page_chapter(ctx) -> dict:
    """`{页号: 章号}`（C1 起一本书可能多章；单章时全部同章）。切分信息缺失 → 空表（不判跨章）。"""
    out = {}
    for c in (ctx.structure.get("chapters") or []):
        pgs = c.get("pages") or []
        for p in pgs:
            out[int(p)] = c.get("chapter")
    return out


def g3_rejoin(ctx) -> dict:
    """**重接续排**：查的是"**管线有没有把同一段搬两遍**"，不是"原书能不能重复印同一句"。

    A3 细化（SPEC §5.1 G3 / 变更记录 103）：跨页同文本分两类——
      * **同章内**跨页重复（该文本在同一页内不重复，却在同章多页各出现一次）= 原书**重复印刷的固定栏目/范例句**
        （实测：D&D 模组的「调整遭遇」侧栏、XP/宝藏结算说明、怪物数据页）→ **不 fail**，记 `repeated_boilerplate`；
      * **同一页内**重复、或**跨章**重复 = 管线搬页搬重了 → **fail**。
    """
    chap_of = _page_chapter(ctx)
    seen, dup, boiler = {}, [], []
    for b in ctx.ir_m:
        if b.get("role") != "primary" or b["kind"] in FURNITURE or b["kind"] in ("figure", "equation"):
            continue
        key = _flat(b.get("text_raw"))[:200]
        if len(key) < TUNE["g3_dup_min_chars_ir"]:
            continue
        page, prev = b["page"], seen.get(key)
        if prev is not None:
            # **章信息缺失时按"同章"处理**（保守：不拿"没有证据"当"跨章"来判 fail）
            same_chapter = (not chap_of) or (chap_of.get(prev) is not None
                                             and chap_of.get(prev) == chap_of.get(page))
            if prev != page and same_chapter:
                boiler.append({"text_head": key[:40], "pages": [prev, page]})
            else:
                dup.append({"text_head": key[:40], "pages": [prev, page],
                            "why": "同一页内重复" if prev == page else "跨章重复（疑似搬页搬重）"})
        seen.setdefault(key, page)
    # **产物级重复段落**：IR 内不重复、但产物里重复（实测：跨页切分 + 视觉插入会各写一次同一段）
    #   ⚠️ 口径与上面的 IR 检查**逐字一致**：同章内跨页重复 = 原书重复印刷（记数）；**同页内**或**跨章** = fail。
    #   **A7-3：页归属按「页标记的字符位置」切分**（`PAGE_MARK_RE.finditer` 的偏移量），**行内标记同等对待**。
    #   原先按"整行匹配"（`^\s*<!--\s*pNNN\s*-->\s*$`）→ **句中接页的行内标记看不见**：真书实测把 p7/p8 的
    #   「调整队伍强度」栏（原书两页各印一次）误判成"同一页内重复" → **假红**（G3 fail）。
    _md_txt = ctx.md or ""
    _mk = [(m.start(), int(m.group(1))) for m in PAGE_MARK_RE.finditer(_md_txt)]
    _mk_off = [o for o, _p in _mk]

    def _page_at(_off: int):
        """该偏移之前**最后一条**页标记的页号（没有则 None）。"""
        _pg = None
        for _o, _p in _mk:
            if _o < _off:
                _pg = _p
            else:
                break
        return _pg

    _line_off = [m.start() for m in re.finditer(r"(?m)^", _md_txt)]   # 每行起始偏移
    md_seen, md_dup, md_boiler, cur_page = {}, [], [], None
    for i, line in enumerate(_md_txt.splitlines()):
        if line.lstrip().startswith("<!--"):
            continue          # 页标记/其它注释都不是内容（`joined from previous page` 等本就会重复出现）
        key = _flat(line)
        if len(key) < TUNE["g3_dup_min_chars_md"]:
            continue
        off = _line_off[i] if i < len(_line_off) else len(_md_txt)
        cur_page = _page_at(off)
        prev = md_seen.get(key)
        if prev is not None:
            _p_prev, _p_now = prev[0], cur_page
            _ln = prev[2]                     # **行号**（`prev[1]` 是偏移量，别混）
            # **两次出现之间是否跨过页标记？**（按**偏移量**判，行内标记也算）跨过 = 原书另一页也在印同一句
            # （重复印刷，记数）；没跨过 = 落在同一页里写了两遍（管线重复物化，fail）。
            _crossed = any(prev[1] < _o < off for _o in _mk_off)
            if _crossed:
                md_boiler.append({"text_head": key[:40], "lines": [_ln, i + 1]})
            elif _p_prev is not None and _p_now is not None and _p_prev != _p_now:
                md_dup.append({"text_head": key[:40], "lines": [_ln, i + 1], "why": "跨章重复"})
            elif _p_prev is not None and _p_prev == _p_now:
                md_dup.append({"text_head": key[:40], "lines": [_ln, i + 1], "why": "同一页内重复"})
            else:
                # **页码未知**（该段没有任何 `<!-- pN -->` 页标记）→ **不判"同一页"**：判"同页"会制造假红。
                # （"缺页标记"另有 G1 硬判据守着；这里只对无标记的旧产物保守处理。）
                md_boiler.append({"text_head": key[:40], "lines": [_ln, i + 1]})
        else:
            md_seen[key] = (cur_page, off, i + 1)
    # 归属错：S3 判 exists 且应为 restore_to_main，但没有对应续排/切分
    own = []
    joined_pages = {j["page_from"] for j in ctx.structure.get("cross_page_joins", [])}
    split_pages = {s["to_page"] for s in ctx.structure.get("cross_page_splits", [])}
    for v in ctx.verdicts:
        if v.get("review_type") == "existence_paddle_only" and v.get("verdict") == "exists":
            if v["pdf_page"] not in joined_pages and v["pdf_page"] not in split_pages:
                own.append({"item": v["item_id"], "page": v["pdf_page"],
                            "why": "判为'页面上存在'但该页既没有续排也没有跨页切分（内容仍可能留在别页）"})
    status = "fail" if (dup or md_dup) else ("warn" if own else "pass")
    return _g("G3", "rejoin", status,
              {"duplicate_paragraphs_ir": len(dup), "duplicate_paragraphs_markdown": len(md_dup),
               "repeated_boilerplate": len(boiler) + len(md_boiler), "ownership_flagged": len(own)},
              dup + md_dup + own + (boiler + md_boiler)[:6],
              "管线不得把同一段搬两遍（同页内重复 / 跨章重复 = 0）；原书重复印刷的固定栏目只记数")


# ------------------------------------------------------------------ G4
def g4_equation_numbers(ctx) -> dict:
    tags = EQ_TAG_RE.findall(ctx.md)
    nums, bad = [], []
    for t in tags:
        parts = re.findall(r"\d+", t)
        if not parts:
            bad.append({"tag": t, "why": "编号里没有数字"})
            continue
        nums.append(tuple(int(x) for x in parts))
    dups = sorted({t for t in tags if tags.count(t) > 1})
    order_ok = nums == sorted(nums)
    refs = re.findall(r"式\s*[（(]?\s*(\d+\.\d+)", ctx.md)
    unresolved_refs = [r for r in refs if r not in tags]
    ev = [{"tag": t, "why": "重复编号"} for t in dups] + bad
    ev += [{"ref": r, "why": "正文引用但没有对应公式"} for r in unresolved_refs]
    if not tags:
        return _g("G4", "equation_numbers", "warn", {"tags": 0},
                  [{"why": "产物中未发现 \\tag{} 编号"}], "0 缺失（编号连续 + 引用可解析）")
    status = "fail" if (dups or bad or unresolved_refs) else ("warn" if not order_ok else "pass")
    note = [] if refs else [{"why": f"未检查'正文引用↔公式'：样本中未发现引用语法（已扫 式N.M 形式）"}]
    return _g("G4", "equation_numbers", status,
              {"tags": len(tags), "duplicates": dups, "monotonic": order_ok, "refs": len(refs)},
              ev + note, "0 缺失（编号连续 + 引用可解析）")


# ------------------------------------------------------------------ G5
def g5_footnote_bijection(ctx) -> dict:
    fn = ctx.structure.get("footnotes") or {}
    miss = fn.get("missing_definitions") or []
    orph = fn.get("orphan_definitions") or []
    ev = [{"marker": m, "why": "正文有 marker 但没有脚注定义"} for m in miss]
    ev += [{"marker": m, "why": "有脚注定义但正文没有 marker（孤儿）"} for m in orph]
    status = "pass" if not (miss or orph) else "warn"
    return _g("G5", "footnote_bijection", status,
              {"markers_in_text": fn.get("markers_in_text"), "definitions": fn.get("definitions"),
               "missing": miss, "orphans": orph}, ev, "marker 集合 == definition 集合；0 差异")


# ------------------------------------------------------------------ G6
def g6_figure_caption_bijection(ctx) -> dict:
    figs = ctx.report.get("figures") or {}
    linked = set(IMAGE_LINK_RE.findall(ctx.md))
    files = {v["file"]: v for v in figs.values()} if isinstance(figs, dict) else {}
    ev = []
    # **以源为准**（用户裁定）：源里本来就没有图题时**绝不算缺陷**，只记数。
    # 实测：有机 120–124 原生 28/28 张图的 image_caption 都是空的、页面上也没印图题 —— 旧判据
    # "每张图必须有图题"是在要求源里不存在的东西，属**构造性误报**（两批书都被它假红过）。
    src_no_caption, possible_caption = [], []
    for b in ctx.ir_m:
        if b["kind"] != "figure" or b.get("role") != "primary":
            continue
        has_cap = bool((b.get("text_raw") or "").strip()) or any(
            k.get("kind") == "caption" or k.get("source_field", "").endswith("caption")
            for k in b.get("_children", []))
        if has_cap:
            continue
        src_no_caption.append({"block_id": b["block_id"], "page": b["page"]})
        # 启发式：图块正下方**紧邻的短文本块**可能是图题（引擎把它当普通文本块了）→ 只判 warn
        for o in ctx.ir_m:
            if o is b or o.get("page") != b["page"] or o.get("role") != "primary":
                continue
            y0 = o["bbox_norm"][1]
            if b["bbox_norm"][3] <= y0 and (y0 - b["bbox_norm"][3]) < 0.03:
                t = (o.get("text_raw") or "").strip()
                if 2 <= len(t) <= 40 and not t.endswith(("。", ".", "．", "！", "？")):
                    possible_caption.append({"figure": b["block_id"], "page": b["page"],
                                             "near_text": t[:40]})
                    break
    ev += [dict(x, why="源无图题（按源为准：只记数，不判缺陷）") for x in src_no_caption[:4]]
    ev += [dict(x, why="图下方紧邻短文本块（可能是图题被引擎当普通文本）") for x in possible_caption[:4]]
    unreferenced = [f for f in files if f not in linked]
    ev += [{"file": f, "why": "裁出的图没有被产物引用"} for f in unreferenced]
    refs = {f"{a}.{b}" for a, b in FIGREF_RE.findall(ctx.md)}
    have = {v.get("figure_number") for v in files.values()}
    dangling = sorted(r for r in refs if r not in have)
    ev += [{"ref": r, "why": "正文引用了 图N.M，但没有对应图文件"} for r in dangling]
    # **链接目标必须真的存在**（实测教训：校对包里没带 figures/ 目录，图全成断链，而当时的 G6 查不出来）
    _base = os.path.dirname(ctx.md_path) if ctx.md_path else ""
    missing_files = [l for l in sorted(linked)
                     if _base and not os.path.exists(os.path.join(_base, l))]
    ev += [{"file": l, "why": "正文链接指向的图文件不存在（断链）"} for l in missing_files]
    status = "fail" if (missing_files or dangling) else ("warn" if (unreferenced or possible_caption) else "pass")
    return _g("G6", "figure_caption_bijection", status,
              {"figures": len(files), "linked": len(linked), "unreferenced": len(unreferenced),
               "missing_link_targets": len(missing_files),
               "figures_without_source_caption": len(src_no_caption),
               "possible_captions_as_text": len(possible_caption),
               "text_refs": sorted(refs), "refs_without_file": dangling},
              ev, "源有图题→产物必须有；`图N.M` 引用与链接目标必须成立；**源无图题不判缺陷**（只记数）")


# ------------------------------------------------------------------ G7
def g7_math_lint(ctx) -> dict:
    # **只 lint 正文**：`<!-- … -->` 是元数据（标记/审计），其中的文本可能被截断
    # （实测：`page_end_truncation` 的尾文截断后剩一个落单 `$`，把整道门禁判成 fail）。
    md = re.sub(r"<!--.*?-->", " ", ctx.md or "", flags=re.S)
    issues = []
    if md.count("$$") % 2:
        issues.append({"why": "行间公式 $$ 未配对"})
    inline = md.replace("$$", "")
    if inline.count("$") % 2:
        issues.append({"why": "行内公式 $ 未配对"})
    for pat, why in ((r"_\s+\{", "下标写成 `_ {`"), (r"\^\s+\{", "上标写成 `^ {`"),
                     (r"\\frac\s+\{", "分式写成 `\\frac {`")):
        for m in re.finditer(pat, md):
            issues.append({"why": why, "at": md[max(0, m.start() - 20):m.start() + 24]})
    for m in re.finditer(r"\$\$([^$]{4,})\$\$", md):
        body = m.group(1)
        if re.search(r"\d(?:\s+\d){2,}", body):
            issues.append({"why": "数学区内数字被空格分隔（会渲染成多个数字）",
                           "at": re.search(r"\d(?:\s+\d){2,}", body).group(0)})
        if re.search(r"\S\s{2,}\S", body):
            issues.append({"why": "数学区内连续空格", "at": body[:40]})
    status = "fail" if any("未配对" in i["why"] for i in issues) else ("warn" if issues else "pass")
    return _g("G7", "math_lint", status, {"issues": len(issues)}, issues[:12],
              "无未配对定界符；无 `_ {`/`^ {`/`\\frac {`；数学区无连续空格/数字被空格分隔")


# ------------------------------------------------------------------ G8
def g8_digit_unit_preserved(ctx) -> dict:
    flagged = []
    for q in ctx.questions:
        sig = ((q.get("risk") or {}).get("signals")) or []
        if any(s in ("digit", "unit") for s in sig):
            flagged.append(q)
    undecided = [q for q in flagged if not any(v.get("item_id") == q["item_id"] for v in ctx.verdicts)]
    ev = [{"item": q["item_id"], "page": q["pdf_page"], "why": "数字/单位风险项尚未判定"}
          for q in undecided]
    status = "fail" if undecided else "pass"
    return _g("G8", "digit_unit_preserved", status,
              {"risk_flagged": len(flagged), "decided": len(flagged) - len(undecided)}, ev,
              "数字/小数/单位/正负号 与见证引擎不一致的条目全部登记")


# ------------------------------------------------------------------ G9
def g9_markdown_structure(ctx) -> dict:
    """**标准解析器真解析**，并验证"结构存活"（SPEC §5 G9）。

    判据（每条都能给数）：
      ① 解析不报错；
      ② **表格**：源里每个 GFM 表（表头行 + 分隔行）都被解析成 `<table>`；
      ③ **图片**：源里每个 `![](...)` 都渲染出 `<img>`（能抓"未闭合注释把后面全吞掉"这类灾难）；
      ④ **标题**：源里每个 `^#{1,6} ` 都渲染出 `<hN>`；
      ⑤ **行间公式**：`$$…$$` 成对，且**独占段落**（不被正文包住）；
      ⑥ **页底注**：`<!-- page-footnote … -->` 标记必须**紧跟在一段注文之后**（防"注变成注释"）。
    无解析器 → `unverified`（I2：unverified 永不当 pass）。
    注意：markdown-it-py 的默认预设是 **commonmark（不含表格）**，必须显式 `enable("table")`。
    """
    md = ctx.md or ""
    try:
        from markdown_it import MarkdownIt
    except Exception:                                            # noqa: BLE001
        return _unverified("G9", "markdown_structure",
                           "环境无 Markdown 解析器（markdown-it-py 未安装）→ 无法真解析",
                           "标准解析器可解析；表格/公式/脚注结构存活")
    mdit = MarkdownIt("commonmark").enable("table")
    exp_tbl = len(re.findall(r"(?m)^\|[^\n]*\|\s*$\n^\|[\s:|-]+\|\s*$", md))
    # 表格样式的**行块**：若有行块缺分隔行 → 该表会被静默渲染成普通文本（结构丢失），必须判 fail
    _runs, _cur = [], []
    for _l in md.splitlines():
        if _l.strip().startswith("|") and _l.strip().endswith("|"):
            _cur.append(_l)
        elif _cur:
            _runs.append(_cur); _cur = []
    if _cur:
        _runs.append(_cur)
    broken_tbl = [r for r in _runs
                  if not any(re.match(r"^\|[\s:|-]+\|$", x.strip()) for x in r)]
    exp_img = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", md))
    exp_h = len(re.findall(r"(?m)^#{1,6}\s+\S", md))
    dollars = md.count("$$")
    md_lines = md.splitlines()
    glue_math = [l.strip()[:70] for l in md_lines
                 if "$$" in l and not l.strip().startswith("$$")]
    orphan_notes = []
    for i, l in enumerate(md_lines):
        if "page-footnote" in l and l.lstrip().startswith("<!--"):
            prev = next((x for x in reversed(md_lines[:i])
                         if x.strip() and not x.lstrip().startswith("<!--")), "")
            if not prev:
                orphan_notes.append({"line": i + 1, "why": "页底注标记前没有注文段落"})
    try:
        tokens = mdit.parse(md)
        html = mdit.render(md)
    except Exception as exc:                                     # noqa: BLE001
        return _g("G9", "markdown_roundtrip", "fail", {"parse_error": str(exc)},
                  [{"why": f"标准解析器解析失败：{exc}"}], "标准解析器可解析；表格/公式/脚注结构存活")
    got_tbl = html.count("<table")
    got_img = html.count("<img")
    got_h = len(re.findall(r"<h[1-6][ >]", html))
    ev = []
    if got_tbl != exp_tbl:
        ev.append({"why": "表格未被解析成 <table>（结构不成立）", "expected": exp_tbl, "got": got_tbl})
    if broken_tbl:
        ev.append({"why": "有表格样式的行块但缺分隔行（该表会退化成普通文本）",
                   "blocks": len(broken_tbl), "sample": broken_tbl[0][0].strip()[:60]})
    if got_img != exp_img:
        ev.append({"why": "图片未被渲染成 <img>（可能被注释/块吞掉）", "expected": exp_img, "got": got_img})
    if got_h != exp_h:
        ev.append({"why": "标题未被解析成 <hN>", "expected": exp_h, "got": got_h})
    if dollars % 2:
        ev.append({"why": "行间公式 $$ 不成对", "count": dollars})
    if orphan_notes:
        ev += orphan_notes
    measured = {"parser": "markdown-it-py", "tokens": len(tokens),
                "tables_expected": exp_tbl, "tables_rendered": got_tbl,
                "images_expected": exp_img, "images_rendered": got_img,
                "headings_expected": exp_h, "headings_rendered": got_h,
                "display_math_pairs": dollars // 2, "display_math_not_standalone": len(glue_math),
                "table_row_blocks": len(_runs), "table_blocks_without_delimiter": len(broken_tbl)}
    # **双版本**：干净版必须确由留底版派生（逐字节等于"去注释"结果）、无注释、可解析
    # C1 起一本书可能多章 → `clean_edition` 可以是**列表**（每章一条），旧字典形式继续兼容
    _ce_all = (getattr(ctx, "report", None) or {}).get("clean_edition") or {}
    _ce_list = _ce_all if isinstance(_ce_all, list) else [_ce_all]
    _ce_list = [c for c in _ce_list if c]
    _base_dir = os.path.dirname(getattr(ctx, "md_path", "") or "")
    for _ce in _ce_list:
        _tag = _ce.get("chapter") or _ce.get("file") or "?"
        if not _ce.get("identical_to_strip"):
            ev.append({"why": "干净版 ≠ 留底版去注释（派生不一致，可能被手改）",
                       "file": _ce.get("file"), "chapter": _tag})
        if _ce.get("comments_in_clean"):
            ev.append({"why": "干净版里仍有 HTML 注释", "count": _ce["comments_in_clean"], "chapter": _tag})
        if _ce.get("footnote_syntax_in_clean"):
            ev.append({"why": "干净版里仍有脚注语法", "count": _ce["footnote_syntax_in_clean"],
                       "chapter": _tag})
        _cf = _ce.get("file") or ""
        _cp = os.path.join(_base_dir, _cf)
        if _cf and not os.path.exists(_cp):
            ev.append({"why": "干净版文件不存在", "file": _cf, "chapter": _tag})
        elif _cf:
            _ctxt = open(_cp, encoding="utf-8").read()
            try:
                _chtml = mdit.render(_ctxt)
            except Exception as exc:                                # noqa: BLE001
                ev.append({"why": f"干净版解析失败：{exc}", "chapter": _tag})
                _chtml = ""
            if _chtml and _chtml.count("<img") != len(re.findall(r"!\[[^\]]*\]\([^)]+\)", _ctxt)):
                ev.append({"why": "干净版图片未被渲染成 <img>（结构不成立）", "chapter": _tag})
    if _ce_list:
        measured["clean_editions"] = [c.get("file") for c in _ce_list]
        measured["clean_identical_to_reviewed"] = all(bool(c.get("identical_to_strip")) for c in _ce_list)
        measured["clean_edition"] = _ce_list[0].get("file") if len(_ce_list) == 1 else None
    return _g("G9", "markdown_roundtrip", "fail" if ev else "pass", measured, ev,
              "标准解析器可解析；表格/公式/脚注结构存活；**干净版确由留底版派生**")


# ------------------------------------------------------------------ G10
def g10_escalation_rate(ctx) -> dict:
    """**升级率**（用户裁定：拆两个数 + 小样本不判 + **永不阻断**）。

    ① `escalations_per_100_pages`：管线制造的升级总量（观测"管线噪声水平"）；
    ② `human_facing_per_100_pages`：**真正需要人看**的条数（D6 口径，`counts.human_facing_markers`）。
    **靶子只对 ②**：≤20/100 页；**样本 <30 页不判定（`n/a`）**——5 页样本里一条问题就等于 20/100 页，
    比率没有统计意义（否则会永远 warn → 告警疲劳）。**超阈只 warn，绝不阻断交付。**
    """
    rep = getattr(ctx, "report", None) or {}
    esc = rep.get("escalations") or {}
    counts = rep.get("counts") or {}
    pages = len(page_markers(getattr(ctx, "md", "") or "")) or 1
    esc_n = esc.get("count") or 0
    hf_n = counts.get("human_facing_markers")
    if hf_n is None:
        hf_n = esc_n
    per = round(esc_n / (pages / 100.0), 1)
    per_hf = round(hf_n / (pages / 100.0), 1)
    measured = {"pages": pages, "escalations": esc_n, "human_facing": hf_n,
                "escalations_per_100_pages": per, "human_facing_per_100_pages": per_hf}
    crit = "给人看的条数 ≤20/100 页（**≥30 页才判**；超阈只 warn，**永不阻断**）"
    if pages < TUNE["g10_min_pages"]:
        return _g("G10", "escalation_rate", "n/a", measured,
                  [{"why": f"样本仅 {pages} 页（<30）→ 比率无统计意义，不判定；两个数仅作观测"}],
                  reason="小样本不判（用户裁定：阈值 30 页）", threshold=crit, blocking=False)
    status = "pass" if per_hf <= TUNE["g10_human_facing_max_per_100"] else "warn"
    ev = [] if status == "pass" else [{"why": "给人看的升级率超目标（**观测信号，不阻断**）",
                                       "human_facing_per_100_pages": per_hf}]
    return _g("G10", "escalation_rate", status, measured, ev, crit, blocking=False)


# ------------------------------------------------------------------ G11
def g11_geometry_without_content(ctx) -> dict:
    hits = [h for h in ctx.s4_hits if h.get("rule") == "R1"]
    split_pages = {s["to_page"] for s in ctx.structure.get("cross_page_splits", [])}
    judged, undecided = [], []
    for h in hits:
        page = h.get("page")
        vs = [v for v in ctx.verdicts if v.get("pdf_page") == page
              and v.get("review_type") == "transcribe_region"
              and f"R1-{page:04d}" in (v.get("item_id") or "")]
        ok = [v for v in vs if v.get("verdict") in ("transcribed", "no_text")]
        if not ok and page in split_pages:
            ok = [{"verdict": "cross_page_split"}]      # 已被跨页切分解释掉
        rec = {"page": page, "ink": h.get("ink_density"), "verdicts": [v.get("verdict") for v in vs]}
        (judged if ok else undecided).append(rec)
    status = "fail" if undecided else "pass"
    return _g("G11", "geometry_without_content", status,
              {"r1_hits": len(hits), "judged": len(judged), "undecided": len(undecided)},
              undecided, "0 未判定")


# ------------------------------------------------------------------ G12
def g12_materialization(ctx) -> dict:
    miss_ir, miss_md = [], []
    if ctx.native_raw:
        import json as _json
        raw = _json.load(open(ctx.native_raw, encoding="utf-8"))
        fields = ("text", "content", "table_body", "table_caption", "table_footnote",
                  "image_caption", "image_footnote", "chart_caption", "chart_footnote")
        have = {(b.get("order"), b.get("source_field")) for b in ctx.ir_m}
        # 家具（页眉/页脚/页码）是**正当剔除**的（S0/S6 的职责），不算物化丢失
        furniture_keys = {(b.get("order"), b.get("source_field")) for b in ctx.ir_m
                          if b.get("kind") in FURNITURE}
        # 跨页切分是**正当归属修正**：原生的整串会被切成两半分别落在两页，
        # 因此"整串能不能词面命中"不再成立——只要两半都在产物里，就算已物化。
        fm = _flat(ctx.md)
        split_pairs = [(_flat(s.get("keep") or ""), _flat(s.get("moved_text") or ""))
                       for s in (ctx.structure.get("cross_page_splits") or [])]

        def _explained_by_split(flat_val: str) -> bool:
            for k, m in split_pairs:
                if k and m and k in fm and m in fm and flat_val.endswith(m):
                    return True
            return False

        # 被视觉插入**吸收**的片段同样不算法化丢失（它的内容就在插入的那句话里）
        insert_flats = [_flat(v.get("transcription")) for v in (ctx.verdicts or [])
                        if v.get("verdict") == "transcribed" and (v.get("transcription") or "").strip()]

        def _explained_by_insertion(flat_val: str) -> bool:
            return any(flat_val and flat_val in ft for ft in insert_flats)
        for order, item in enumerate(raw):
            for f in fields:
                vals = item.get(f)
                vals = vals if isinstance(vals, list) else [vals]
                for v in vals:
                    if not (v and str(v).strip()):
                        continue
                    if (order, f) not in have:
                        miss_ir.append({"order": order, "field": f, "value": str(v)[:60]})
                    if (order, f) in furniture_keys:
                        continue
                    flat = _flat(str(v))
                    if f == "table_body":
                        for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", str(v), re.S | re.I):
                            cell = re.sub(r"<[^>]+>", "", cell)
                            if cell.strip() and _flat(cell) not in fm:
                                miss_md.append({"field": f, "cell": cell[:40]})
                    elif (len(flat) >= 4 and flat not in fm
                          and not _explained_by_split(flat) and not _explained_by_insertion(flat)):
                        miss_md.append({"field": f, "value": str(v)[:60]})
    ev = [dict(x, why="原生字段没有进 IR") for x in miss_ir] + \
         [dict(x, why="原生内容在产物里词面命中失败") for x in miss_md]
    status = "fail" if (miss_ir or miss_md) else "pass"
    return _g("G12", "materialization_completeness", status,
              {"missing_in_ir": len(miss_ir), "missing_in_markdown": len(miss_md)}, ev[:12],
              "0 缺失")


# ------------------------------------------------------------------ G13
def g13_media_landed(ctx) -> dict:
    import json as _json
    import os
    ev = []
    total = 0
    if ctx.native_raw:
        raw = _json.load(open(ctx.native_raw, encoding="utf-8"))
        referenced = {b["text_raw"].strip() for b in ctx.ir_m if b.get("source_field") == "img_path"}
        for order, item in enumerate(raw):
            p = item.get("img_path")
            if not p:
                continue
            total += 1
            base = os.path.basename(str(p))
            on_disk = bool(ctx.images_dir) and os.path.exists(os.path.join(ctx.images_dir, base))
            if not on_disk or str(p) not in referenced:
                ev.append({"img_path": str(p), "on_disk": on_disk,
                           "referenced_in_ir": str(p) in referenced, "why": "媒体未落盘或未被引用"})
    # U-15：**合成图**（VISION-FIGURE）也必须落盘 + 被正文引用；原生 img_path 分支不变。
    _vfigs = (ctx.report or {}).get("vision_figures") or []
    _figs = (ctx.report or {}).get("figures") or {}
    _linked = set(IMAGE_LINK_RE.findall(ctx.md or ""))
    _base = os.path.dirname(ctx.md_path) if getattr(ctx, "md_path", "") else ""
    for item in _vfigs:
        bid = item.get("block_id")
        fig = _figs.get(bid) if bid else None
        if not fig:
            ev.append({"item_id": item.get("item_id"), "block_id": bid,
                       "why": "vision_figures 声明了合成图，但 report.figures 里没有它"})
            continue
        f = fig.get("file") or ""
        on_disk = bool(_base and f and os.path.exists(os.path.join(_base, f)))
        if not on_disk:
            ev.append({"item_id": item.get("item_id"), "file": f,
                       "why": "合成图文件没有落盘"})
        if f not in _linked:
            ev.append({"item_id": item.get("item_id"), "file": f,
                       "why": "合成图没有被正文引用（断链）"})
    status = "fail" if ev else "pass"
    return _g("G13", "media_landed", status,
              {"img_paths": total, "vision_figures": len(_vfigs), "problems": len(ev)},
              ev, "0 缺失")


# ------------------------------------------------------------------ G14..G16
def _unverified(gid, name, reason, threshold):
    return _g(gid, name, "unverified", {}, [{"why": reason}], reason=reason, threshold=threshold)


def _na(gid, name, reason, threshold="不适用"):
    """**范围外**（用户裁定 N/A）：不算 pass/fail、不阻断、**也不进 unverified** —— 但必须在报告里列出理由。

    与 `unverified` 的区别：`unverified` 是"本该能测、但当前测不了"（一旦能测就该给数）；
    `n/a` 是"**判据归属别处**（例如属下游检索层），本工作流不承诺"。
    """
    return _g(gid, name, "n/a", {}, [{"why": reason}], reason=reason, threshold=threshold,
              blocking=False)


def g14_error_rate(ctx) -> dict:
    """**内容级错误率**（SPEC §4.6）——判据改成**可验形式**：

    在**已完成的 golden 抽样**上：① 严重级错误数 = **0（观测）**；② 错误率 **95% 上界 ≤ 0.5 项/页**。
    **全量/整本书的错误率不在本工作流的承诺内**（见 `LIMITATIONS-产物能力与不支持清单.md`）——
    抽样下"严重级 = 0"永远只能给**上界**，写成"必须 = 0"是永远无法被验证为真的判据。

    实测数字来自 `measurements/error-rate.json`（**数据文件，不是代码常量**）；文件缺失 → `unverified`（I2）。
    """
    import json as _json
    p = os.path.join(MEASUREMENTS_DIR, "error-rate.json")
    if not os.path.exists(p):
        return _unverified("G14", "error_rate",
                           "无实测记录（`measurements/error-rate.json` 缺失）→ 无法测",
                           "已抽样部分：严重级 = 0（观测）且错误率 95% 上界 ≤0.5 项/页")
    m = _json.load(open(p, encoding="utf-8"))
    sev = int(m.get("severe_errors", 1))
    up = float(m.get("rate_upper95_per_page", TUNE["g14_missing_upper_default"]))
    ev = [{"why": "实测记录（数据文件）", "file": "measurements/error-rate.json",
           "pages": m.get("pages"), "severe_errors": sev, "general_errors": m.get("general_errors"),
           "rate_upper95_per_page": up, "scope": m.get("scope"),
           "whole_book": "**未验证**（不在承诺内）"}]
    if sev:
        ev.append({"why": "观测到严重级错误 → 违反判据"})
    if up > TUNE["g14_upper95_max"]:
        ev.append({"why": "错误率 95% 上界超过 0.5 项/页"})
    measured = {"pages": m.get("pages"), "severe_errors": sev,
                "general_errors": m.get("general_errors"), "rate_per_page": m.get("rate_per_page"),
                "rate_upper95_per_page": up, "scope": m.get("scope"),
                "whole_book_error_rate": "n/a（未验证，不在承诺内）"}
    return _g("G14", "error_rate", "pass" if (sev == 0 and up <= 0.5) else "fail", measured, ev,
              "已抽样部分：严重级 0（观测）且错误率 95% 上界 ≤0.5 项/页；**全量不在承诺内**")


def g15_lexical_searchability(ctx) -> dict:
    return _na("G15", "lexical_searchability",
               "**范围外**（用户裁定）：'同一事实的各种写法都能搜到'是**检索层**指标"
               "（要维护同义词/单位归一，属下游检索层），与产物保真正交、且抓不到转换错误；"
               "产物不承诺此项（见 LIMITATIONS-产物能力与不支持清单.md）")


def g16_chunk_safety(ctx) -> dict:
    """**收缩后的产物侧检查**（用户裁定：依赖"还没定的分块器"那半 → N/A）。

    只查三条与"分块器是什么"无关、且产物自身必须成立的：
      ① **块原子性**：行间公式 `$$…$$`、GFM 表格、页底注各自**独占 Markdown 块**（不被正文包住）；
      ② **标记不进检索文本**：去掉 `<!-- … -->` 后的检索视图里 0 注释残留；
      ③ **标题层级存在**：能为块提供上下文。
    """
    md = ctx.md or ""
    lines = md.splitlines()
    issues = []
    glue_math = [l.strip()[:70] for l in lines if "$$" in l and not l.strip().startswith("$$")]
    if glue_math:
        issues.append({"why": "行间公式未独占块（同一行还有正文）", "at": glue_math[0]})
    for i, l in enumerate(lines):
        s = l.strip()
        if s.startswith("|") and s.endswith("|") and i:
            p = lines[i - 1].strip()
            if p and not p.startswith("|") and not p.startswith("<!--"):
                issues.append({"why": "表格块紧贴正文（不是独立块）", "line": i + 1, "at": s[:50]})
                break
    for i, l in enumerate(lines):
        if "page-footnote" in l and l.lstrip().startswith("<!--"):
            p = lines[i - 1] if i else ""
            if not p.strip() or p.lstrip().startswith("<!--"):
                issues.append({"why": "页底注与标记不相邻（注可能被切成独立块）", "line": i + 1})
            break
    retrieval = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    leaked = len(re.findall(r"<!--|-->", retrieval))
    if leaked:
        issues.append({"why": "检索视图里仍有注释残留", "count": leaked})
    headings = len(re.findall(r"(?m)^#{1,6}\s+\S", md))
    if headings == 0:
        issues.append({"why": "产物没有任何标题层级（块缺少上下文）"})
    return _g("G16", "chunk_safety", "fail" if issues else "pass",
              {"headings": headings, "comments_in_retrieval_view": leaked,
               "glued_display_math": len(glue_math)},
              issues, "行间公式/表格/页底注在产物内原子；标记不进检索文本；标题层级存在")


def g17_fragment_absorbed(ctx) -> dict:
    """**插入之后必须消费被覆盖的片段**（软门禁）。

    实测：第 3 页原生把「首先，温度 T₁ 不变」切成一个孤立的 `$$T_1$$`；视觉层补回整句后，
    那个残块若照常输出，就与插入内容并排出现（用户发现"多一个 T₁"）。
    本门禁检查：凡"落在转写区域内 + 文字已被转写内容包含"的片段，**不得再以独立行出现**。
    """
    md_lines = {_flat(l) for l in (ctx.md or "").splitlines() if _flat(l)}
    frags, left = [], []
    for v in (ctx.verdicts or []):
        if v.get("review_type") != "transcribe_region" or v.get("verdict") != "transcribed":
            continue
        reg = v.get("bbox_norm")
        ft = _flat(v.get("transcription"))
        if not reg or len(reg) != 4 or len(ft) < 8:
            continue
        for b in ctx.ir_m:
            if b.get("role") != "primary" or b.get("kind") in FURNITURE:
                continue
            bb = b.get("bbox_norm") or []
            if len(bb) != 4:
                continue
            if not (reg[0] <= bb[0] and reg[1] <= bb[1] and bb[2] <= reg[2] and bb[3] <= reg[3]):
                continue
            fb = _flat(b.get("text_raw"))
            if fb and fb in ft:
                rec = {"block_id": b["block_id"], "page": b["page"], "text": fb,
                       "item_id": v.get("item_id")}
                frags.append(rec)
                if fb in md_lines:
                    left.append(dict(rec, why="该片段已被插入内容覆盖，却仍以独立块出现在产物里"))
    status = "warn" if left else "pass"
    return _g("G17", "fragment_absorbed", status,
              {"fragments_covered_by_insert": len(frags), "still_standalone": len(left)},
              left, "被插入内容覆盖的片段必须已被消费（不残留独立块）", blocking=False)


def g18_no_footnote_syntax(ctx) -> dict:
    """**产物不得出现 Markdown 脚注语法**（用户裁定，软门禁）。

    实测：`[^1]:` 定义在阅读器 A 显示、阅读器 B **不显示**。因此页下注一律按页底位置输出为**普通行**，
    由 `<!-- page-footnote: … -->` 注释留审计。本门禁防止有人（或某次改动）把脚注语法加回来。
    """
    md = ctx.md or ""
    defs = re.findall(r"(?m)^\s*\[\^[^\]]*\]:", md)
    refs = [m for m in re.findall(r"\[\^[^\]]+\]", md) if not re.match(r"\[\^[^\]]+\]:", m)]
    ev = [{"why": "产物含脚注**定义**语法 `[^n]:`（阅读器兼容性差）", "sample": d.strip()[:40]} for d in defs[:4]]
    ev += [{"why": "产物含脚注**引用**语法 `[^n]`", "sample": r[:40]} for r in refs[:4]]
    status = "fail" if (defs or refs) else "pass"
    return _g("G18", "no_footnote_syntax", status,
              {"definition_lines": len(defs), "inline_refs": len(refs)}, ev,
              "0 处脚注语法（页下注按页底普通行显示）", blocking=False)


def g19_review_kit(ctx) -> dict:
    """**校对包是交付物的一部分**（用户裁定）——所以它缺失就是交付不完整。

    检查：① 三件套齐（`README-校对说明.md`、`校对表.md`、`pages/`）；② 清单里引用的页面图都存在（无断链）；
    ③ 清单的"待看条数"与 `report.counts.human_facing_markers` **一致**（防两处漂移；报告没给该字段时跳过）。
    """
    base = os.path.dirname(getattr(ctx, "md_path", "") or "")
    kit = os.path.join(base, "review-kit")
    ev, missing = [], []
    for f in ("README-校对说明.md", "校对表.md"):
        if not os.path.exists(os.path.join(kit, f)):
            missing.append(f)
    pages_dir = os.path.join(kit, "pages")
    if not os.path.isdir(pages_dir):
        missing.append("pages/")
    todo, refs = 0, []
    cl = os.path.join(kit, "校对表.md")
    if os.path.exists(cl):
        txt = open(cl, encoding="utf-8").read()
        m = re.search(r"待看条数\*\*：\s*(\d+)", txt)
        todo = int(m.group(1)) if m else -1
        refs = sorted(set(re.findall(r"pages/(p\d{4}\.png)", txt)))
        for r in refs:
            if not os.path.exists(os.path.join(pages_dir, r)):
                ev.append({"why": "清单引用的页面图不存在（断链）", "file": r})
        if todo < 0:
            ev.append({"why": "清单缺少机器可读的「待看条数」行"})
    hfm = ((getattr(ctx, "report", None) or {}).get("counts") or {}).get("human_facing_markers")
    if hfm is not None and todo >= 0 and todo != hfm:
        ev.append({"why": "清单待看条数与 report 的 human_facing_markers 不一致",
                   "checklist": todo, "report": hfm})
    ev += [{"why": "校对包缺少文件/目录", "missing": f} for f in missing]
    imgs = len(os.listdir(pages_dir)) if os.path.isdir(pages_dir) else 0
    return _g("G19", "review_kit", "fail" if (missing or ev) else "pass",
              {"todo_items": todo, "human_facing_markers": hfm, "page_images": imgs,
               "images_referenced": len(refs)},
              ev, "三件套齐；清单页号都有页面图；待看条数 == human_facing_markers")


def g20_chapter_split(ctx) -> dict:
    """**章切分（C1，SPEC §3.8 裁定④，硬门禁）**。

    判据（每条都机器可判）：
      ① **边界与原生层级一致**：`chapter_split.boundaries` 里的每条边界都要能在 `heading_tree` 里
         找到 `level==1` 的同条目（`block_id` 相同即算一致）；冲突必须已登记（`suspects` 非空即为已登记，
         且**登记过的冲突不产生边界**）。**章锚分支（2026-10-18）**：带 `anchor=true` 的边界改按
         `chapter_split.anchors` 的声明页校验（`anchor_page` 必须在声明里），不要求 heading_tree 命中。
      ② **章节精确覆盖全部内容块**：`blocks_in_chapters == blocks_direct`，且 S7 的装配验证
         `pages_multiset_equal` / `body_lines_equal` 成立（逐章拼接 == 一次装配）。
      ③ **章序号与文件序号单调**（`00-front` 在前）。
      ④ **无空章**：`body_count==0` 的章必须出现在 `empty_chapters` 里（登记，不静默）。
      ⑤ **front-matter 单独成篇**：有章前内容时文件名必须是 `chapter-00-front*`。
      **全书没有任何章边界 → `pass（单章交付合法）`**。
    """
    rep = getattr(ctx, "report", None) or {}
    cs = rep.get("chapter_split") or {}
    chs = rep.get("chapters") or []
    tree = (getattr(ctx, "structure", None) or {}).get("heading_tree") or []
    if not cs:
        return _g("G20", "chapter_split", "unverified", {},
                  [{"why": "report.json 里没有 chapter_split（C1 未启用或报告来自旧版产物）"}],
                  "边界与层级一致；章节精确覆盖；序号单调；无空章（登记）；front-matter 单独成篇",
                  blocking=False)
    ev = []
    bds = cs.get("boundaries") or []
    lvl1 = {(h.get("block_id"), h.get("page")) for h in tree if h.get("level") == 1}
    lvl1_ids = {h.get("block_id") for h in tree if h.get("level") == 1}
    # **章锚分支（SPEC §3.8，2026-10-18）**：声明了 anchors 时，带 `anchor=true` 的边界按**声明页**校验，
    # 不再要求 heading_tree 有 level==1 条目（锚点就是权威；对不上只登记、不阻断）。
    _declared = set(cs.get("anchors") or [])
    _bad_anchor = [b for b in bds if b.get("anchor") and b.get("anchor_page") not in _declared]
    if _bad_anchor:
        ev.append({"why": "章锚边界的 anchor_page 不在声明的 anchors 里", "items": _bad_anchor[:5]})
    _unmatched = [b for b in bds if not b.get("anchor")
                  and (b.get("block_id"), b.get("page")) not in lvl1 and b.get("block_id") not in lvl1_ids]
    if _unmatched:
        ev.append({"why": "有章边界在 heading_tree 里找不到 level==1 的同条目", "items": _unmatched[:5]})
    ac = cs.get("assembly_check") or {}
    if ac and not (ac.get("pages_multiset_equal") and ac.get("body_lines_equal")):
        ev.append({"why": "章节拼接与一次装配不一致（漏页/重复页/内容漏重）", "assembly_check": ac})
    if ac and ac.get("blocks_in_chapters") != ac.get("blocks_direct"):
        ev.append({"why": "章节覆盖的块数与正文总块数不等",
                   "in_chapters": ac.get("blocks_in_chapters"), "direct": ac.get("blocks_direct")})
    if not ac:
        ev.append({"why": "缺少 assembly_check（无法判定「精确覆盖」）"})
    _no = [c.get("chapter") for c in chs]
    _seq = [int(str(x)) if str(x).isdigit() else -1 for x in _no]
    if _seq != sorted(_seq):
        ev.append({"why": "章序号不是单调递增（含 00-front 在最前）", "order": _no})
    _files = [c.get("file") for c in chs]
    if sorted(_files) != _files:
        ev.append({"why": "文件序号与章序号不同序", "files": _files})
    _empty_reg = {(e.get("file_stem") or e.get("chapter_no")) for e in (cs.get("empty_chapters") or [])}
    for c in chs:
        if (c.get("body_count") or 0) == 0 and (c.get("file_stem") or c.get("file")
                                                or c.get("chapter")) not in _empty_reg:
            ev.append({"why": "有空章未登记（只有标题、没有正文）", "file": c.get("file")})
    # 判据⑤（补充）：**章号标签必须与文件名前缀一致**——"00" 只配 `chapter-00-front*`；
    # 单章产物（`chapter-01-*`）的标签也必须是 `01`，否则报告与产物自相矛盾。
    for c in chs:
        _f = c.get("file") or ""
        _tag = str(c.get("chapter") or "")
        if _f.startswith("chapter-") and _tag and _tag not in ("None", "") and "-" in _f:
            _pfx = _f.split("chapter-", 1)[1]
            if not _pfx.startswith(_tag):
                ev.append({"why": "章号标签与文件名前缀不一致（报告与产物自相矛盾）",
                           "chapter": _tag, "file": _f})
    _front = [c for c in chs if str(c.get("chapter")) == "00"]
    if _front and not all((c.get("file") or "").startswith("chapter-00-front") for c in _front):
        ev.append({"why": "章前内容没有单独成篇（应为 chapter-00-front）",
                   "files": [c.get("file") for c in _front]})
    # 判据⑥（C2-3）：输入是**完整书级序列**却 `rule=single` → **可疑**（整本书不该一个章标题都没有）。
    # `partial_range`（不连续/只交一部分）的单章 = **正常**（§3.8 输入性质，保持现状、无提示）。
    _ik = cs.get("input_kind")
    _rule = cs.get("rule")
    if _ik == "book_sequence" and _rule == "single":
        _registered = any("可疑" in str(s.get("why") or "") for s in (cs.get("suspects") or []))
        if not _registered:
            ev.append({"why": "输入是完整书级序列（book_sequence）但没有任何章边界（rule=single）",
                       "input_kind": _ik, "rule": _rule})
    measured = {"rule": cs.get("rule"), "chapters": len(chs),
                "boundaries": len(bds), "suspects": len(cs.get("suspects") or []),
                "empty_chapters": len(cs.get("empty_chapters") or []),
                "notes": len(cs.get("notes") or []),
                "files": _files, "assembly_check": ac, "input_kind": _ik}
    if _declared:                                  # 只增字段：未声明锚点的旧报告不变
        measured["anchors"] = len(_declared)
    return _g("G20", "chapter_split", "fail" if ev else "pass", measured, ev,
              "边界与 heading_tree.level 一致；章节精确覆盖全部块；序号单调；无空章（登记）；front-matter 单独成篇；无边界=单章合法")


# ------------------------------------------------------------------ G21
def g21_judge_ledger(ctx) -> dict:
    """**G21 判定台账完整性**（C3；SPEC §3.10）。

    门禁**自己从台账文件独立复算**（不信产物里的自检结论）；台账不存在（旧产物/未启用 C3）
    → `unverified` 且 `blocking=False`（I2：不当 pass，也不阻断历史交付）。
    """
    import judge_worklist as JW                     # 延迟导入，避免模块级循环
    rows = getattr(ctx, "worklist", None)
    prog = getattr(ctx, "judge_progress", None) or {}
    thr = "0 违规（批不重叠 / 不丢批 / 未判定登记 / 判定者在位 / 拒收有去处）"
    if not rows or not prog:
        return _g("G21", "judge_ledger", "unverified",
                  {"worklist_rows": len(rows or []), "has_progress": bool(prog)},
                  reason="无判定台账（旧产物或未启用 C3）", threshold=thr, blocking=False)
    recs = prog.get("batches") or []
    for r in recs:
        r.setdefault("_pages", list(range(r["page_from"], r["page_to"] + 1)))
    tasks = getattr(ctx, "tasks", None) or []
    qids = [t.get("item_id") for t in tasks] if tasks else None
    rep = getattr(ctx, "report", None) or {}
    res = JW.check_ledger(
        rows, recs, queue_ids=qids,
        # 判据③用**类别级痕迹**（与 G2 同一约定）：按 item_id 找会全假红（真书实测 13/13）
        product_marks=JW.mark_categories_by_page(getattr(ctx, "md", "") or ""),
        expired_ids=(rep.get("expired_verdicts") or []),
        report_pending=(rep.get("judgment") or {}).get("pending"),
        # **A7-14 加固**：把书 key 传进去，让门禁**自己从台账复算指纹**。
        # 对抗测试实证：只看 `report.json` 的"产物自述"时，台账里被改坏/过期的 `binding` 会被静默放过。
        key=getattr(ctx, "key", "") or "",
        # **A7-15**：复算指纹必须用**本次校验的那棵树**（`ctx.root`）—— 否则副本树/临时夹具
        # 会被整片误判过期（对抗测试实证：不传 root 时合成夹具基线全红）。
        root=getattr(ctx, "root", "") or "",
        # 同时核对"台账（唯一真相源）与 ingest（下游读数）一致"
        ingest=getattr(ctx, "ingested", None))
    measured = {**res["measured"], "violations": res["violations_by_kind"],
                "next_batch": (prog.get("order") or [None])[0]}
    status = "pass" if res["ok"] else "fail"
    return _g("G21", "judge_ledger", status, measured, res["violations"][:12], thr)


def g22_delivery_note(ctx) -> dict:
    """**交付说明的封条**（A6，软门禁）—— SPEC §3.12 / §5 G22。

    **判的不是"写没写"**：`交付说明.md` 由校验器**每次校验整体重写**，所以"写没写"它自己保证。
    G22 判的是**自上一轮校验以来有没有人动过它**：上一轮 `validation-report.json.delivery_note.sha256`
    == 盘上 `交付说明.md` 的 sha256。
      * 不一致 → **fail**（`note_edited_since_validation`，软：本轮校验会重写它，所以不阻断）；
      * 文件没了（但上一轮有封条）→ **fail**（`note_missing`）；
      * 没有 `validation-report.json` 或它没有 `delivery_note` 块（老产物 / 首次校验）→ **unverified 且不阻断**（I2）。
    """
    base = os.path.dirname(getattr(ctx, "md_path", "") or "")
    thr = "封条一致（上一轮记录的 sha256 == 盘上说明的 sha256）"
    if not base or not os.path.isdir(base):
        return _unverified("G22", "delivery_note", "没有交付目录（无产物可判）", thr)
    res = DN.seal_check(s7_dir=base)
    if res["seal"] == "unverified":
        return _unverified("G22", "delivery_note", res.get("seal_reason", ""), thr)
    if res["seal"] == "fail":
        return _g("G22", "delivery_note", "fail", {"seal": "mismatch"},
                  res["seal_violations"][:4], thr,
                  reason="交付说明自上一轮校验后被改过 / 被删（本轮会重写成正确的）", blocking=False)
    return _g("G22", "delivery_note", "pass", res.get("seal_measured") or {}, [], thr, blocking=False)


ALL_GATES = [g1_page_coverage, g2_no_silent_loss, g3_rejoin, g4_equation_numbers,
             g5_footnote_bijection, g6_figure_caption_bijection, g7_math_lint,
             g8_digit_unit_preserved, g9_markdown_structure, g10_escalation_rate,
             g11_geometry_without_content, g12_materialization, g13_media_landed,
             g14_error_rate, g15_lexical_searchability, g16_chunk_safety, g17_fragment_absorbed,
             g18_no_footnote_syntax, g19_review_kit, g20_chapter_split, g21_judge_ledger,
             g22_delivery_note]


def run_all(ctx) -> dict:
    gates = {}
    for fn in ALL_GATES:
        try:
            res = fn(ctx)
        except Exception as exc:                                   # noqa: BLE001
            res = _g("G?", getattr(fn, "__name__", "?"), "unverified", {},
                     [{"why": f"门禁自身异常：{type(exc).__name__}: {exc}"}],
                     reason="门禁执行失败")
        gates[res["id"]] = res
    return gates
