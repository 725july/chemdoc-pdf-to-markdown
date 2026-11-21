# -*- coding: utf-8 -*-
"""S8 · 校对包（review-kit）—— **交付物的一部分**（用户裁定）。

放在书目录内、**不复制正文与图**（避免两份漂移）：
    s7-out/<书>/review-kit/
      ├─ README-校对说明.md   ← 怎么校、约定、严重级定义
      ├─ 校对表.md            ← **按章分节 + 三类分区**（需要你看的 / 已分流无需人工 / 已判定备查）
      ├─ 校对表-坐标.jsonl    ← sidecar：逐条 (页/类型/坐标/item_id/状态/去处)，**坐标不丢**
      └─ pages/               ← 页面图（默认只渲染**有待看项**的页；--all-pages 全渲染）

**A7-10a（2026-10-13）——只改"给人看的界面"**：真书实测 686 条给人看的标记里 **665 条**对应的台账项
是"准入门已拒收且已有去处"的（`N-3_notation_alias_only`）→ 曾把 686 条都摆到核对表上，而真正需人工的只有 ~20 条。
故：① **已分流项单独成节并表头标注**；② 该节**按"同文重复"合并成现象级一行**（处数 + 出现页码）；
③ **类型代号给中文** + 术语小抄；④ **备注栏给人话位置**（左/右栏 × 上/中/下）而不是坐标；
⑤ **机器数字照旧**：`- **待看条数**：N` 仍 = 全部标记数（**G19 硬校验 == report.counts.human_facing_markers**），
另加独立一行「其中需人工」（措辞不命中 G19 的正则）。**不动产物字节、不动门禁口径、不碰 S3/台账。**

约定：整本书 = **一份包**（清单按章分节），不按章拆包——因为判决/errata/报告都是书级的。
用法:
  python -X utf8 s8_review_kit.py run --book puhua-b2 [--s7-dir <书目录>] [--all-pages] [--dpi 300] [--hide-verdicts]
  python -X utf8 s8_review_kit.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from contracts import REVIEW_PAGE_RE
import render
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = paths.data_root()  # 可迁移：_mcp-test 的上一级 = 产物根
FOLDED_RE = re.compile(r"<!--\s*已判定[^：]*：(\d+)\s*条\s*→\s*([^>]*?)-->")
TODO_LINE = "- **待看条数**：{n}（**全部标记**；含已分流与已判定，G19 用这一行）"
HUMAN_LINE = ("- **其中需人工**：{n}（**＝ 实际要你看的条数**；其余 {routed} 条**已由准入门分流、有去处**，"
              "见各章第 2 节）")
# B2：有"已判定/已豁免"的标记被收进备查时，把多出来的那部分如实写出来（否则"其余"对不上账）
HUMAN_LINE_J = ("- **其中需人工**：{n}（**＝ 实际要你看的条数**；其余 {routed} 条**已由准入门分流、有去处**"
                "（第 2 节）；{judged} 条**已判定/已转写**（第 3 节））")

# **类型代号 → 人话**（A7-10a）：代号是给机器看的，人看的必须说清"这是什么问题、你要做什么"。
CAT_HUMAN = {
    "S3_ownership": ("归属不一致", "这块内容（常是侧栏/分栏）该算哪一页、哪一栏，两个引擎说法不同",
                     "对着图确认它在哪一栏、哪一页"),
    "S3_content_diff": ("文字不一致", "两个引擎给出的文字不同", "对着图确认哪个对（或都不对）"),
    "S3_structure_diff": ("结构不一致", "分段/表格/层级这类结构两边不同", "对着图确认该按哪种排"),
    "S3_existence_mineru_only": ("只有主引擎认到", "主引擎说有这块，见证引擎没认到", "对着图确认这块**到底有没有**"),
    "S3_existence_paddle_only": ("只有见证引擎认到", "见证引擎说有这块，主引擎没认到", "对着图确认这块**到底有没有**"),
    "page_end_truncation": ("页末疑似半句", "这一页最后一块不是完整句，可能被页边界截断或丢了后半句",
                            "看它是否与下一页首句连着"),
    "empty_block": ("有墨迹但没文字", "引擎给了位置框、里面却空着（R1 探针命中）", "对着图**把字读出来**"),
    "table_structure_flag": ("表格合并单元格", "表里有跨行/跨列，Markdown 管道表表达不了",
                             "看着图核对拆开后的单元格对不对"),
    "vision_insertion": ("我们按图补的正文", "这是**我们**看图补进去的字（审计痕迹）", "核对补得对不对"),
    # 2026-11-15（实跑 P0-1/P0-2）：主引擎漏掉、页面上确实有的内容（判定已确认缺）→ 让人确认后插回
    "missing_text": ("产物缺这段（已判定）",
                     "判定者确认**页面上有**这段、但主引擎没读出来 → 正文里没有它",
                     "确认这段要不要插回正文（要插就按锚点跑 `errata_apply.py`）"),
    "figure_missing_crop": ("图没裁出来", "这块该有图，但裁图没生成（位置框在、文件不在）",
                            "对着图看这块到底是什么图"),
    "suspect_list_item_heading": ("疑似列表被当成标题",
                                  "这一行像「1. …」的列表项，却被识别成了标题（层级/分段可能错）",
                                  "对着图确认该按标题还是列表排"),
    "review_render_pixels": ("（位置口径）", "位置基于复核渲染像素", "—"),
}
CAT_FALLBACK = ("（未登记的类型）", "这个代号还没有中文说明——请把它当作「两引擎在这处不一致」",
                "对着图判断哪边对")

# **拒收理由代号 → 人话**（A7-10a；"已分流"那一节要说清"为什么不用你看"）
REASON_HUMAN = {
    "N-3_notation_alias_only": "记号写法不同（内容逐字相同）",
    "N-2_no_content_diff": "归一化后没有实质差异（标点/空白）",
    "N-1_structure_as_existence": "把结构差异当成了存在性问题（已转结构检查器）",
    "N-0_empty_span": "空区间",
    "N-7_alignment_artifact": "对齐算法造成的假差异（需更窄裁图重问）",
    "N-4_anchor_missing": "锚点缺失/过粗（降级为整页问题）",
}


def reason_human(reasons) -> str:
    rs = [r for r in (reasons or []) if r]
    if not rs:
        return "—"
    return "；".join(REASON_HUMAN.get(r, r) for r in rs)


def cat_human(cat: str) -> tuple:
    return CAT_HUMAN.get(cat, CAT_FALLBACK)


def _norm_bbox(b) -> tuple:
    try:
        return tuple(round(float(x), 3) for x in (b or [])[:4])
    except Exception:                                                       # noqa: BLE001
        return ()


def human_pos(bbox) -> str:
    """坐标 → 人话位置：**左栏/右栏/通栏 × 上/中/下**（人不需要读 0.083,0.523…）。"""
    b = _norm_bbox(bbox)
    if len(b) != 4:
        return "—"
    x0, y0, x1, y1 = b
    col = "左栏" if x1 <= 0.5 else ("右栏" if x0 >= 0.5 else "通栏")
    yc = (y0 + y1) / 2
    band = "上部" if yc < 0.34 else ("中部" if yc < 0.67 else "下部")
    return f"{col} · {band}"


def _ledger_evidence(root: str, key: str) -> dict:
    """台账里每条判定的**人话依据**（`evidence.what_i_see`）→ `{item_id: 文本}`。

    读不到 / 条目没写依据 → 空表（**不崩、不编**）；校对表第 3 节有就附、没有就不附。
    """
    p = os.path.join(root, "s5-out", key, "judge-worklist.jsonl")
    out: dict = {}
    if not os.path.exists(p):
        return out
    try:
        for line in open(p, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            _e = (r.get("evidence") or {}).get("what_i_see")
            if r.get("item_id") and _e:
                out[r["item_id"]] = str(_e)
    except Exception:                                                       # noqa: BLE001
        return {}
    return out


def _load_authority(root: str, key: str) -> dict:
    """读"权威来源"做分区：`(页, 归一化 bbox)` → {item_id, state, destination, reasons, text}。

    **读不到就返回空表** → 调用方回退到旧行为（逐条列、不分区）。**绝不**因为读不到而崩或假装分过区。
    """
    q = os.path.join(root, "s5-out", f"visual-queue.s3.{key}.jsonl")
    led = os.path.join(root, "s5-out", key, "judge-worklist.jsonl")
    gate = os.path.join(root, "s5-out", key, "gate", "gate-all.jsonl")
    if not (os.path.exists(q) and os.path.exists(led)):
        return {}
    def _jsonl(p):
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    by_id = {r.get("item_id"): r for r in _jsonl(led)}
    gid = {g.get("item_id"): g for g in _jsonl(gate)}
    out: dict = {}
    for it in _jsonl(q):
        row = by_id.get(it.get("item_id")) or {}
        g = gid.get(it.get("item_id")) or {}
        ctx = it.get("context") or {}
        # ⚠️ **一对多**：同一 (页, 坐标) 可能有**多条**队列项（不同类型重叠）。实测：用"后写覆盖前写"的
        #    字典会让 21 条 `S3_ownership` 标记**误认领**到同坐标的"已判"邻居上（于是本该"已分流"的
        #    掉回"需人工"）。所以这里存**列表**，并由 `_claim` **按类型优先**认领。
        out.setdefault((int(it.get("pdf_page") or 0),
                        _norm_bbox(it.get("bbox_norm") or it.get("bbox"))), []).append({
            "item_id": it.get("item_id"), "review_type": it.get("review_type"),
            "state": row.get("state"), "verdict": row.get("verdict"),
            "destination": row.get("destination"), "reasons": g.get("reject_reasons") or [],
            "text": (ctx.get("mineru") or ctx.get("paddle") or "").strip(),
            # 2026-11-15（P0-2）：`errata(kind=missing_text)` 的条目**已判但正文缺内容** →
            # 它们要进第 1 节（需要你看的），不能因为 `state=decided` 被折进"备查"。
            "errata_kind": ((row.get("errata") or {}).get("kind")
                            if isinstance(row.get("errata"), dict) else None),
            "missing_text": ((row.get("errata") or {}).get("missing_text")
                             if isinstance(row.get("errata"), dict) else None),
            "missing_basis": ((row.get("errata") or {}).get("basis")
                              if isinstance(row.get("errata"), dict) else None)})
    return out


def _cat_review_type(cat: str) -> str:
    """`S3_ownership` → `ownership`（S3 标记的类别就是它的 review_type；其余标记没有对应项）。"""
    return cat[3:] if (cat or "").startswith("S3_") else ""


def _pick(cands: list, cat: str) -> dict:
    """多条候选里挑**类型一致**的那条；没有就退回第一条（保守）。"""
    if not cands:
        return {}
    rt = _cat_review_type(cat)
    for c in cands:
        if rt and c.get("review_type") == rt:
            return c
    return cands[0]


def _claim(info_map: dict, page: int, bbox, cat: str = "") -> dict:
    """把一条标记**认领**到权威来源里的队列项：先精确 → 再 2 位小数 → 再同页最近邻（L1 < 0.02）。

    为什么要三级：标记里的 `bbox=` 与队列的 `bbox_norm` **写法/精度可能不同**（实测有 ~20 条差在末位小数，
    只按精确匹配会让它们掉回"需人工"，把 645 说成 665）。**每级都按类型优先**（见 `_pick`）。
    """
    if not info_map:
        return {}
    key = _norm_bbox(bbox)
    if not key:
        return {}
    hit = info_map.get((page, key))
    if hit:
        return _pick(hit, cat)
    key2 = tuple(round(v, 2) for v in key)
    for (pg, bb), v in info_map.items():
        if pg == page and tuple(round(x, 2) for x in bb) == key2:
            return _pick(v, cat)
    best, bd = [], None
    for (pg, bb), v in info_map.items():
        if pg != page or len(bb) != 4:
            continue
        d = sum(abs(a - b) for a, b in zip(bb, key))
        if bd is None or d < bd:
            best, bd = v, d
    return _pick(best, cat) if (bd is not None and bd < 0.02) else {}


def clean_note(note: str) -> str:
    """备注栏**只留人话**：坐标不上桌（坐标写在备注里没人看得懂）。"""
    t = re.sub(r"bbox=[0-9.,\s\-]+", "", note or "").strip(" ;｜|")
    return t or "—"


def parse_markers(md: str) -> tuple[list[dict], list[str]]:
    """→ (未判定标记列表, 已判定折叠块里的条目)。

    未判定项带 `bbox`（从标记的 `bbox=…` 解析）：**分区要靠 (页, bbox) 去权威来源（队列 + 台账）认领**。
    """
    todo = []
    for m in REVIEW_PAGE_RE.finditer(md or ""):
        rest = m.group(3) or ""
        v = (re.search(r"verdict=(\S+)", rest) or [None, None])[1]
        # B3：**必须恰好四段数字**。旧写法 `[0-9.,\s\-]+` 的字符类含空格与数字，备注以
        #     「1. 碳原子是四价的」开头时会把 "1." 一起吞进来 → float 失败 → 坐标丢失、
        #     无法与队列认领（实测 4/8 行中招）。所以这里只认「四个数、逗号分隔」。
        bm = re.search(r"bbox=(-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)", rest)
        bbox = _norm_bbox(bm.group(1).split(",")) if bm else ()
        todo.append({"cat": m.group(1), "cut_page": int(m.group(2)), "verdict": v,
                     "bbox": bbox, "note": rest.strip()[:70]})
    done = []
    for m in FOLDED_RE.finditer(md or ""):
        done += [x.strip() for x in m.group(2).split("；") if x.strip()]
    return todo, done


def _book_page(labels: list, cut_page: int) -> int:
    return labels[cut_page - 1] if 0 <= cut_page - 1 < len(labels) else cut_page


def build(book: str, root: str = ROOT, s7_dir: str = "", dpi: int = 300,
          all_pages: bool = False, hide_verdicts: bool = False) -> dict:
    import binding
    cfg = binding.resolve_cfg(book)
    key = cfg.get("key") or book
    book_dir = s7_dir or os.path.join(root, "s7-out", key)
    if not os.path.isdir(book_dir):
        return {"ok": False, "error": "书目录不存在", "book_dir": book_dir}
    mds = sorted(f for f in os.listdir(book_dir)
                 if f.startswith("chapter-") and f.endswith("-reviewed.md"))
    if not mds:
        return {"ok": False, "error": "找不到 chapter-*-reviewed.md", "book_dir": book_dir}
    kit = os.path.join(book_dir, "review-kit")
    if os.path.isdir(kit):                     # 旧包先归档（**移动，不删除**）→ 不留下过期副本
        sup = os.path.join(book_dir, "_superseded")
        os.makedirs(sup, exist_ok=True)
        shutil.move(kit, os.path.join(sup, "review-kit-" + time.strftime("%Y%m%dT%H%M%SZ")))
    os.makedirs(os.path.join(kit, "pages"), exist_ok=True)
    labels = cfg.get("page_labels") or []
    sections, todo_all, done_all = [], [], []
    for md_name in mds:
        md = open(os.path.join(book_dir, md_name), encoding="utf-8").read()
        t, d = parse_markers(md)
        sections.append((md_name, t, d))
        todo_all += t
        done_all += d
    need = sorted({x["cut_page"] for x in todo_all}) if not all_pages else list(range(1, len(labels) + 1))
    rendered = []
    if need and cfg.get("pdf") and os.path.exists(cfg["pdf"]):
        doc = render.open_doc(cfg["pdf"])
        for cp in need:
            if cp - 1 >= render.page_count(doc):
                continue
            bp = _book_page(labels, cp)
            rel = f"pages/p{bp:04d}.png"
            render.save_png(doc, cp - 1, os.path.join(kit, rel), dpi)
            rendered.append({"cut_page": cp, "book_page": bp, "file": rel})
        render.close(doc)
    # ⚠️ 曾经这里写的是 `x["book_page"]`（那个键**不存在**）——只在"页面图没渲染"时才求值，
    #    于是被 happy path 一直遮住（A7-10a 的端到端自测第一次把它炸出来）。
    missing = [_book_page(labels, x["cut_page"]) for x in todo_all
               if not os.path.exists(os.path.join(kit, f"pages/p{_book_page(labels, x['cut_page']):04d}.png"))]
    # README
    with open(os.path.join(kit, "README-校对说明.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# 校对包使用说明\n\n"
                 "> 本包是**交付物的一部分**，由 S8 从本目录的产物（`chapter-*-reviewed.md`）**确定性派生**。\n"
                 "> 正文与图**不复制**在这里——它们就在上一级目录，包内只放「给人看」的对照材料与清单。\n\n"
                 "## 包里有什么\n\n"
                 "| 文件 | 用途 |\n|---|---|\n"
                 "| `校对表.md` | **你需要看的**（第 1 节）+ **已分流、不用看**（第 2 节）+ 已判定备查（第 3 节）；按章分节 |\n"
                 "| `校对表-坐标.jsonl` | 同样的条目 + **精确坐标/item_id/状态/去处**（机器用；人不用看） |\n"
                 "| `pages/` | 页面图（默认**只渲染有待看项的页**；文件名 `p<书内页码>.png`） |\n"
                 "| `../chapter-*-reviewed.md` | 留底版正文（带标记与审计痕迹，**真身**） |\n"
                 "| `../chapter-*-clean.md` | 干净版正文（可直接使用） |\n"
                 "| `../figures/` | 裁出的图（干净版/留底版的图片链接都指向它） |\n\n"
                 "## 严重级怎么定\n\n"
                 "- **严重**：数字/小数/单位/正负号、上下标、公式、否定词、化学记号——改变科学含义\n"
                 "- **一般**：错字、漏字、多字、标点体例、段落切分与归属\n"
                 "- **不算错**（D2/N5 裁定）：全/半角标点、空格、`〔美〕`/`[美]` 之类**表示法**差异\n\n"
                 "## 怎么记\n\n"
                 "在 `校对表.md` 里补一列，或按这个格式回话：\n`页 23 ｜ 原文「…」 ｜ 应为「…」 ｜ 严重：否`\n\n"
                 "> **提醒**：不要「顺手纠正」拼写或术语——**页面说什么就是什么**（源文本自身的错误照抄，\n"
                 "> 那属于 `errata.md` 的 U 类）。本包在测量用途下可用 `--hide-verdicts` 隐藏我们的结论。\n")
    # 校对表（按章分节 + **三类分区**：需要你看的 / 已分流无需人工 / 已判定备查）
    auth = _load_authority(root, key)
    ev_by_id = _ledger_evidence(root, key)
    sidecar = []
    routed_n = judged_n = 0
    for _md_name, t, _d in sections:
        for x in t:
            info = _claim(auth, x["cut_page"], x.get("bbox"), x.get("cat") or "")
            x["_info"] = info
            x["_routed"] = bool(info.get("state") == "rejected_with_destination"
                                and (info.get("destination") or "").strip())
            # B2：**已判定/已豁免**的标记不该再进"需要你看的"（S7 没折住时的兜底）。
            # 注意：`待看条数`仍是**全部标记数**（G19 口径），只把"其中需人工"减下去。
            x["_judged"] = bool(not x["_routed"] and info.get("state") in ("decided", "waived"))
            if x["_routed"]:
                routed_n += 1
            elif x["_judged"]:
                judged_n += 1
    # 2026-11-15（P0-2）：`missing_text` 缺口**也是给人看的**（要不要插回是人的决定），
    # 所以「需人工」要把它们加回来；否则 §1 写着 6 条、数字却说 5 条。
    # 只补「已判定 → 被 todo_n 减掉」的那部分；未被认领的缺口本来就在 todo_n 里（不能再加一次）
    gap_n = sum(1 for _nm, _t, _d in sections for x in _t
                if x["_judged"] and (x["_info"].get("errata_kind") or "") == "missing_text")
    todo_n = len(todo_all) - routed_n - judged_n + gap_n
    with open(os.path.join(kit, "校对表.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# 校对表 · {key}\n\n")
        # **机器数字**（G19 硬校验 == report.counts.human_facing_markers）**不许因为分区而变化**
        fh.write(TODO_LINE.format(n=len(todo_all)) + "\n")
        fh.write((HUMAN_LINE_J if judged_n else HUMAN_LINE).format(n=todo_n, routed=routed_n, judged=judged_n)
                 + "\n")
        fh.write(f"- 已判定（备查）= **{len(done_all) + judged_n}**\n\n")
        if not auth and any((x.get("cat") or "").startswith("S3_") for x in todo_all):
            # 只有"**有 S3 题要判**但对不上账"时才是真警告：S4 探针标记本来就不在准入队列里，
            # 空队列/零出题的书（例如自制样书）不该被这条吓到。
            fh.write("> ⚠ **这次没读到准入门/台账**（多半是首跑还没跑 `queue`）→ 上面「需人工」里的 "
                     "**S3 题**把未准入的也算进去了，**数字偏高**。跑一次 "
                     "`pipeline_run.py queue --book %s` 后重算即准。\n\n" % key)
        fh.write("## 怎么用这张表（先读这一段）\n\n"
                 "1. **只看第 1 节「需要你看的」**（每章一张表）。每行都给了**页码**和**整页图**链接，"
                 "对着图看即可；看不出问题就留空。\n"
                 "2. 要记意见，写在最后一列，或直接回话："
                 "`页 23 ｜ 原文「…」 ｜ 应为「…」 ｜ 严重：否`。\n"
                 "3. **第 2 节「已分流」不用看**：那些差异是记号写法/空白/别名一类，"
                 "准入门已判定**不值得人工看**，并登记了**去处**；列在这里只为「没丢」。想抽查按页码点图。\n"
                 "4. **第 3 节「已判定」**是我们已经判过的结论（备查）。\n\n"
                 "> 为什么会有第 2 节：两个引擎常把**同一段文字在不同页上**记成「写法不同」的候选，"
                 "这类候选会**重复出现很多次**（本书最多的一页有 54 条相同标记）。逐条列会淹掉真待办，"
                 "所以第 2 节**按「同文重复」合并成一行**（给处数与出现页码）。\n\n")
        fh.write("### 类型代号 → 人话\n\n| 代号 | 人话 | 你要做什么 |\n|---|---|---|\n")
        for _c, (_nm, _desc, _act) in CAT_HUMAN.items():
            fh.write(f"| `{_c}` | **{_nm}**：{_desc} | {_act} |\n")
        fh.write("\n> 位置的说法：**左栏/右栏/通栏 × 上/中/下**（页面被页中线分栏；"
                 "跨中线的叫通栏）。精确坐标在 `校对表-坐标.jsonl`（机器用）。\n")
        for md_name, t, d in sections:
            fh.write(f"\n## {md_name}\n")
            todo_rows = [x for x in t if not x["_routed"] and not x["_judged"]]
            routed_rows = [x for x in t if x["_routed"]]
            judged_rows = [x for x in t if x["_judged"]]
            # 2026-11-15（P0-2）：**已判定、但产物里缺这段**（`missing_text`）→ 必须回到第 1 节。
            # 只按台账状态分流会把它们当"没事了"，用户按"需人工 N"看就看不见缺口。
            gap_rows = [x for x in judged_rows
                        if (x["_info"].get("errata_kind") or "") == "missing_text"]
            judged_rows = [x for x in judged_rows if x not in gap_rows]
            # ── 第 1 节：真待办
            fh.write(f"\n### 1. 需要你看的（{len(todo_rows) + len(gap_rows)} 条）\n\n")
            if not todo_rows:
                fh.write("（本章无）\n")
            else:
                fh.write("| # | 页 | 类型 | 位置 | 整页图 | 备注 | 你的判断 | 严重？ |\n"
                         "|---|---|---|---|---|---|---|---|\n")
                for i, x in enumerate(todo_rows, 1):
                    bp = _book_page(labels, x["cut_page"])
                    img = f"pages/p{bp:04d}.png"
                    ok = os.path.exists(os.path.join(kit, img))
                    nm, desc, act = cat_human(x["cat"])
                    fh.write(f"| {i} | **{bp}** | {nm}（`{x['cat']}`） | {human_pos(x.get('bbox'))} "
                             f"| {'[第%d页](%s)' % (bp, img) if ok else '（未渲染）'} "
                             f"| {clean_note(x['note'])} ｜ 看图要点：{act} |  |  |\n")
            # ── 第 1 节·缺口：**已判定、但产物里缺这段**（2026-11-15 / P0-2）
            if gap_rows:
                fh.write(f"\n#### 1b. 已判定、但**产物里缺这段**（{len(gap_rows)} 条）\n\n"
                         "> 这些不是「没判」，而是**判过、并确认页面上确实有这段**，只是主引擎没读出来 → "
                         "正文里没有。**确认无误后**可以按条目里的锚点跑 "
                         "`errata_apply.py` 把它们插回（会留 `MISSING-TEXT-INSERT` 痕迹）。\n\n"
                         "| # | 页 | 缺的内容（页面上有的） | 凭什么说缺 | 条目 |\n|---|---|---|---|---|\n")
                for i, x in enumerate(gap_rows, 1):
                    bp = _book_page(labels, x["cut_page"])
                    _mt = (x["_info"].get("missing_text") or "").replace("|", "／")
                    _mt = _mt[:80] + ("…" if len(_mt) > 80 else "")
                    fh.write(f"| {i} | **{bp}** | 「{_mt}」 | {clean_note(x['_info'].get('missing_basis') or '—')} "
                             f"| `{x['_info'].get('item_id') or '—'}` |\n")
                fh.write("\n")
            # ── 第 2 节：已分流（现象级）
            fh.write(f"\n### 2. 已分流 · 无需人工（{len(routed_rows)} 条）\n\n")
            if not routed_rows:
                fh.write("（本章无）\n")
            else:
                groups = {}
                for x in routed_rows:
                    txt = re.sub(r"\s+", "", (x["_info"].get("text") or ""))[:40] or "（无文本）"
                    groups.setdefault((x["cat"], txt), []).append(x)
                fh.write("> 准入门已拒收、**且已有去处** → 不用你看（列在这里只为「没丢」）。"
                         "同一段文字在多页重复出现的，合并成一行。\n\n")
                fh.write("| # | 类型 | 同文重复的原文 | 处数 | 出现页 | 去向 |\n|---|---|---|---|---|---|\n")
                for i, ((cat, txt), xs) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1):
                    pages = sorted({_book_page(labels, x["cut_page"]) for x in xs})
                    dests = sorted({(x["_info"].get("destination") or "—") for x in xs})
                    plist = "、".join(str(p) for p in pages[:12]) + ("…" if len(pages) > 12 else "")
                    nm, _d, _a = cat_human(cat)
                    fh.write(f"| {i} | {nm}（`{cat}`） | 「{txt}」 | {len(xs)} | {plist} "
                             f"| {reason_human(xs[0]['_info'].get('reasons'))} → `{'、'.join(dests)}` |\n")
                fh.write(f"\n<details><summary>逐条明细（{len(routed_rows)} 条，机器可读；点开看）</summary>\n\n"
                         "| # | 页 | 类型 | 坐标 | item_id | 去向 |\n|---|---|---|---|---|---|\n")
                for i, x in enumerate(routed_rows, 1):
                    bp = _book_page(labels, x["cut_page"])
                    fh.write(f"| {i} | {bp} | `{x['cat']}` | {','.join(str(v) for v in (x.get('bbox') or ())) or '—'} "
                             f"| `{x['_info'].get('item_id') or '—'}` | `{x['_info'].get('destination') or '—'}` |\n")
                fh.write("\n</details>\n")
            # ── 第 3 节：已判定备查（md 里折叠的 + S8 认领到的 decided/waived）
            _done_items = list(d) + [
                f"{x['_info'].get('item_id')}="
                f"{x['_info'].get('verdict') or x['_info'].get('state')}@c3-ledger"
                for x in judged_rows]
            if _done_items and not hide_verdicts:
                fh.write(f"\n### 3. 已判定（备查 · 无需人工）：{len(_done_items)} 条\n\n")
                fh.write("<details><summary>点开看结论</summary>\n\n")
                for x in _done_items:
                    # 2026-10-26（3.1）：每条附**人话依据**（判定者当时看到了什么），
                    # 否则只拿交付根的人只能看到 `id=verdict@arm`，不知道"凭什么"。
                    _iid = (x.split("=", 1)[0] or "").strip()
                    _ev = (ev_by_id.get(_iid) or "").strip().replace("\n", " ")
                    # 2026-11-15（实跑 P0-3）：原来硬切 80 字且**不加省略号** → "看不到"和"没写"分不清。
                    # 放宽到 200 字，并在截断处补 `…`（完整证据在台账，交付说明里有指针）。
                    if len(_ev) > 200:
                        _ev = _ev[:200] + "…"
                    fh.write(f"- {x}" + (f" — **我认为**：{_ev}" if _ev else "") + "\n")
                fh.write("\n</details>\n")
        fh.write("\n> 整页**通读**仍是最有价值的检查——标记之外的错误只能靠通读发现。\n")
    # sidecar：**坐标不丢**（人看的表里换成"位置"，机器要的坐标在这里）
    for md_name, t, _d in sections:
        for i, x in enumerate(t, 1):
            bp = _book_page(labels, x["cut_page"])
            sidecar.append({"chapter": md_name, "index": i, "book_page": bp, "cut_page": x["cut_page"],
                            "cat": x["cat"], "cat_human": cat_human(x["cat"])[0],
                            "position": human_pos(x.get("bbox")), "bbox": list(x.get("bbox") or []),
                            "note": x["note"], "verdict": x["verdict"],
                            "item_id": (x.get("_info") or {}).get("item_id"),
                            "state": (x.get("_info") or {}).get("state"),
                            "destination": (x.get("_info") or {}).get("destination"),
                            "routed": bool(x.get("_routed"))})
    with open(os.path.join(kit, "校对表-坐标.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in sidecar:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "book": key, "kit": kit, "chapters": len(mds),
            "todo": len(todo_all), "human_todo": todo_n, "routed": routed_n,
            "judged": judged_n, "done": len(done_all),
            "authority": bool(auth), "pages_rendered": len(rendered), "dpi": dpi,
            "pages_without_image": missing}


def cmd_run(a) -> int:
    r = build(a.book, ROOT, a.s7_dir, a.dpi, a.all_pages, a.hide_verdicts)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


def selftest() -> int:
    import tempfile
    cases, passed = [], 0
    d = tempfile.mkdtemp(prefix="s8-")
    md = ("<!-- p001 -->\n\n正文。\n\n"
          "<!-- REVIEW: S3_content_diff page=1 bbox=0.1,0.2,0.3,0.4 -->\n"
          "<!-- 已判定（备查·无需人工）：2 条 → S3-P0002-CONTENT-001=exists@b2bind；TR-R1-0002-01=transcribed@b2bind -->\n")
    t, done = parse_markers(md)
    cases.append(("解析：未判定 1 条、已判定折叠 2 条", len(t) == 1 and len(done) == 2, {"t": t, "d": done}))
    cases.append(("解析：标记里的 `bbox=` 被解成坐标（**分区靠它认领**）",
                  _norm_bbox(t[0]["bbox"]) == (0.1, 0.2, 0.3, 0.4), t[0]))
    t2, d2 = parse_markers("<!-- REVIEW: page_end_truncation page=3 bbox=0.1 -->")
    cases.append(("解析：无 verdict 的标记也算待看", len(t2) == 1 and t2[0]["verdict"] is None, t2))
    # B3：bbox 解析**不许吞备注里的数字**（备注常以 "1. …" 开头）
    _t3, _ = parse_markers("<!-- REVIEW: suspect_list_item_heading page=3 "
                           "bbox=0.336,0.503,0.490,0.519 1. 碳原子是四价的 -->")
    cases.append(("B3：备注以「1. 」开头时 `bbox` 仍解出四元组（旧正则会吞成 *0.519 1.*）",
                  _t3 and tuple(_t3[0]["bbox"]) == (0.336, 0.503, 0.49, 0.519), _t3))
    _t4, _ = parse_markers("<!-- REVIEW: x page=1 bbox=0.1,0.2 -->")
    cases.append(("B3：坐标段数不足 → 空坐标且不抛（不认领，退回需人工）",
                  _t4 and tuple(_t4[0]["bbox"]) == (), _t4))
    _t5, _ = parse_markers("<!-- REVIEW: x page=1 -->")
    cases.append(("B3：没有 `bbox=` → 空坐标且不抛", _t5 and tuple(_t5[0]["bbox"]) == (), _t5))
    _t6, _ = parse_markers("<!-- REVIEW: x page=1 bbox=-0.01,0.5,0.9,1 -->")
    cases.append(("B3：负数/整数写法照样解（四元组）",
                  _t6 and tuple(_t6[0]["bbox"]) == (-0.01, 0.5, 0.9, 1.0), _t6))
    _r = re.search(r"待看条数\*\*：(\d+)", TODO_LINE.format(n=7))
    cases.append(("待看条数行机器可读（G19 依赖它）", bool(_r) and _r.group(1) == "7", TODO_LINE.format(n=7)))
    # 2026-10-26（2.1）：措辞改成"＝实际要你看的条数"，但**机器可读性不变**（G19 / 交付说明都靠正则）
    _t = TODO_LINE.format(n=10)
    _h = HUMAN_LINE.format(n=6, routed=4)
    _hj = HUMAN_LINE_J.format(n=5, routed=3, judged=2)
    cases.append(("2026-10-26（2.1）：两行仍被 G19 / 交付说明的正则解析（改措辞不改机器可读性）",
                  bool(re.search(r"待看条数\*\*：\s*(\d+)", _t))
                  and re.search(r"其中需人工\*\*：\s*(\d+)", _h).group(1) == "6"
                  and re.search(r"其中需人工\*\*：\s*(\d+)", _hj).group(1) == "5", [_t, _h, _hj]))
    cases.append(("A7-10a：坐标 → 人话位置（左/右/通栏 × 上/中/下）",
                  human_pos([0.05, 0.05, 0.45, 0.2]) == "左栏 · 上部"
                  and human_pos([0.55, 0.8, 0.9, 0.9]) == "右栏 · 下部"
                  and human_pos([0.1, 0.5, 0.9, 0.6]) == "通栏 · 中部"
                  and human_pos([]) == "—",
                  [human_pos([0.05, 0.05, 0.45, 0.2]), human_pos([0.55, 0.8, 0.9, 0.9]),
                   human_pos([0.1, 0.5, 0.9, 0.6])]))
    cases.append(("A7-10a：**未登记的类型代号**有兜底中文（不许留代号裸奔）",
                  cat_human("S3_ownership")[0] == "归属不一致"
                  and cat_human("S3_who_knows")[0] == CAT_FALLBACK[0],
                  cat_human("S3_who_knows")))
    # B4：**S7 里出现过的 marker 代号都必须有中文**——否则校对表会打印"（未登记的类型）"
    _s7src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "s7_output.py"),
                  encoding="utf-8").read()
    _cats = set(re.findall(r'marker\(\s*"([A-Za-z_0-9]+)"', _s7src))
    cases.append(("B4：`s7_output` 里所有 `marker(...)` 字面代号都有中文说明（不再出现未登记类型）",
                  bool(_cats) and _cats <= set(CAT_HUMAN), sorted(_cats - set(CAT_HUMAN))))
    cases.append(("A7-10a：拒收理由 → 人话",
                  reason_human(["N-3_notation_alias_only"]).startswith("记号写法不同")
                  and reason_human([]) == "—", reason_human(["N-3_notation_alias_only"])))
    # ---- 端到端：分区 + 现象级合并 + 机器数字不变 + sidecar + 回退
    e2e = os.path.join(d, "e2e")
    book_dir = os.path.join(e2e, "s7-out", "fxbook")
    os.makedirs(os.path.join(e2e, "s5-out", "fxbook", "gate"), exist_ok=True)
    os.makedirs(book_dir, exist_ok=True)
    with open(os.path.join(book_dir, "chapter-01-reviewed.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("<!-- p001 -->\n\n正文一。\n\n"
                 "<!-- REVIEW: S3_ownership page=1 bbox=0.1,0.1,0.4,0.2 -->\n"
                 "<!-- REVIEW: S3_ownership page=2 bbox=0.1,0.1,0.4,0.2 -->\n"
                 "<!-- REVIEW: page_end_truncation page=2 bbox=0.6,0.1,0.9,0.2 -->\n"
                 "<!-- 已判定（备查·无需人工）：1 条 → S3-P0001-CONTENT-001=exists@c3 -->\n")
    with open(os.path.join(e2e, "s5-out", f"visual-queue.s3.fxbook.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        for iid, pg in (("S3-P0001-OWN-001", 1), ("S3-P0002-OWN-002", 2)):
            fh.write(json.dumps({"item_id": iid, "pdf_page": pg, "review_type": "ownership",
                                 "bbox_norm": [0.1, 0.1, 0.4, 0.2],
                                 "context": {"mineru": "调整遭遇 Adjusting this Encounter"}},
                                ensure_ascii=False) + "\n")
        # **一对多**：同页同坐标再放一条**别的类型**（已判）——实测它会"覆盖"同键的归属项，
        # 让归属标记误认领到它身上（21 条真实案例）。这条守住"按类型优先认领"。
        fh.write(json.dumps({"item_id": "S3-P0001-CONTENT-009", "pdf_page": 1,
                             "review_type": "content_diff", "bbox_norm": [0.1, 0.1, 0.4, 0.2],
                             "context": {"mineru": "另一类型的同坐标项"}}, ensure_ascii=False) + "\n")
    with open(os.path.join(e2e, "s5-out", "fxbook", "judge-worklist.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        for iid, pg in (("S3-P0001-OWN-001", 1), ("S3-P0002-OWN-002", 2)):
            fh.write(json.dumps({"item_id": iid, "pdf_page": pg, "state": "rejected_with_destination",
                                 "destination": "notation_registry"}, ensure_ascii=False) + "\n")
        fh.write(json.dumps({"item_id": "S3-P0001-CONTENT-009", "pdf_page": 1, "state": "decided",
                             "destination": None}, ensure_ascii=False) + "\n")
    with open(os.path.join(e2e, "s5-out", "fxbook", "gate", "gate-all.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"item_id": "S3-P0001-OWN-001", "admitted": False,
                             "reject_reasons": ["N-3_notation_alias_only"]}, ensure_ascii=False) + "\n")
    r = build("fxbook", e2e, book_dir)
    cl = open(os.path.join(r["kit"], "校对表.md"), encoding="utf-8").read()
    sc = [json.loads(l) for l in open(os.path.join(r["kit"], "校对表-坐标.jsonl"), encoding="utf-8") if l.strip()]
    cases.append(("A7-10a：**已分流项单独成节**（3 条标记 → 需人工 1 / 已分流 2）",
                  r.get("human_todo") == 1 and r.get("routed") == 2 and r.get("todo") == 3,
                  {"human": r.get("human_todo"), "routed": r.get("routed"), "todo": r.get("todo")}))
    cases.append(("A7-10a：**机器数字不变**（待看条数 == 全部标记数，G19 口径）且另起一行报「其中需人工」",
                  f"- **待看条数**：{r['todo']}" in cl and "- **其中需人工**：1" in cl,
                  [l for l in cl.splitlines() if "待看条数" in l or "其中需人工" in l]))
    _rows = [l for l in cl.splitlines() if "调整遭遇" in l]
    cases.append(("A7-10a：同文重复**合并成现象级一行**（处数 2、出现页 1、2）",
                  len(_rows) == 1 and "| 2 |" in _rows[0] and "1、2" in _rows[0], _rows))
    cases.append(("A7-10a：sidecar 写出且**坐标在**（人看的表里不塞坐标）",
                  len(sc) == 3 and any(x["bbox"] == [0.1, 0.1, 0.4, 0.2] for x in sc)
                  and all("position" in x for x in sc), sc[:1]))
    cases.append(("A7-10a：**读不到权威来源时回退**（不分区、不崩）",
                  build("fxbook", os.path.join(d, "nowhere"), book_dir).get("ok") is not None, None))
    # 2026-10-26（2.1）：对不上账时的"数字偏高"警告 —— **只在有 S3 题时**才响
    _na = build("fxbook", os.path.join(d, "nowhere"), book_dir)
    _cl_na = open(os.path.join(_na["kit"], "校对表.md"), encoding="utf-8").read()
    cases.append(("2026-10-26（2.1）：读不到准入门 + **有 S3 题** → 表头警告「数字偏高」",
                  "没读到准入门" in _cl_na, None))
    _e3 = os.path.join(d, "e3")
    _b3 = os.path.join(_e3, "s7-out", "fx3")
    os.makedirs(_b3, exist_ok=True)
    with open(os.path.join(_b3, "chapter-01-reviewed.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("<!-- p001 -->\n\n正文。\n\n"
                 "<!-- REVIEW: page_end_truncation page=1 bbox=0.2,0.8,0.9,0.9 -->\n")
    _r3 = build("fx3", _e3, _b3)
    _cl3 = open(os.path.join(_r3["kit"], "校对表.md"), encoding="utf-8").read()
    cases.append(("2026-10-26（2.1）负例：只有 S4 探针标记（没有 S3 题）→ **不**报「数字偏高」",
                  "没读到准入门" not in _cl3 and "- **其中需人工**：1" in _cl3, _cl3[:160]))

    # ---- B2：S7 没折住的"已判定"标记，S8 也要收进第 3 节（不许再让人看第二遍）
    e2b = os.path.join(d, "e2b")
    bdir = os.path.join(e2b, "s7-out", "fxb")
    os.makedirs(os.path.join(e2b, "s5-out", "fxb", "gate"), exist_ok=True)
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "chapter-01-reviewed.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("<!-- p001 -->\n\n正文。\n\n"
                 "<!-- REVIEW: empty_block page=1 bbox=0.2,0.2,0.5,0.3 -->\n"
                 "<!-- REVIEW: page_end_truncation page=1 bbox=0.2,0.8,0.9,0.9 -->\n")
    with open(os.path.join(e2b, "s5-out", "visual-queue.s3.fxb.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"item_id": "TR-R1-0001-99", "pdf_page": 1,
                             "review_type": "transcribe_region",
                             "bbox_norm": [0.2, 0.2, 0.5, 0.3],
                             "context": {"mineru": ""}}, ensure_ascii=False) + "\n")
    with open(os.path.join(e2b, "s5-out", "fxb", "judge-worklist.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"item_id": "TR-R1-0001-99", "pdf_page": 1, "state": "decided",
                             "verdict": "transcribed", "destination": None,
                             "evidence": {"what_i_see": "红笔手写答案，非印刷正文"}},
                            ensure_ascii=False) + "\n")
    r2 = build("fxb", e2b, bdir)
    cl2 = open(os.path.join(r2["kit"], "校对表.md"), encoding="utf-8").read()
    _sec1 = cl2.split("### 1.")[1].split("### 2.")[0] if "### 1." in cl2 else ""
    cases.append(("B2：坐标命中 `decided` → 进第 3 节备查，**不进**第 1 节（需人工减 1）",
                  r2.get("judged") == 1 and r2.get("human_todo") == 1
                  and "TR-R1-0001-99=transcribed@c3-ledger" in cl2
                  and "empty_block" not in _sec1,
                  {"judged": r2.get("judged"), "human": r2.get("human_todo"), "sec1": _sec1[:120]}))
    cases.append(("B2：`待看条数` 仍是**全部标记数**（G19 口径不变），只是「其中需人工」变小",
                  "- **待看条数**：2" in cl2 and "- **其中需人工**：1" in cl2,
                  [l for l in cl2.splitlines() if "待看条数" in l or "其中需人工" in l]))
    # 2026-10-26（3.1）：备查条目要带**人话依据**（否则只拿交付根的人看不到"凭什么"）
    cases.append(("2026-10-26（3.1）：第 3 节每条附「我认为：…」（取自台账 evidence.what_i_see）",
                  "我认为" in cl2 and "红笔手写答案" in cl2, None))

    # ---- 2026-11-15（P0-1/P0-2/P0-3）：缺口进第 1 节；证据放宽到 200 字且带省略号 ----
    e3 = os.path.join(d, "e3gap")
    b3 = os.path.join(e3, "s7-out", "fxg")
    os.makedirs(os.path.join(e3, "s5-out", "fxg", "gate"), exist_ok=True)
    os.makedirs(b3, exist_ok=True)
    with open(os.path.join(b3, "chapter-01-reviewed.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("<!-- p002 -->\n\n正文。\n\n"
                 "<!-- REVIEW: missing_text page=2 bbox=0.1,0.3,0.9,0.4 item=MT-P0002-01 -->\n"
                 "<!-- REVIEW: empty_block page=2 bbox=0.1,0.6,0.9,0.7 item=TR-R1-0002-01 -->\n")
    _long_ev = "页面右上角红笔写着" + "很长的说明" * 50       # 260 字，用来验 200 字截断
    with open(os.path.join(e3, "s5-out", "visual-queue.s3.fxg.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"item_id": "MT-P0002-01", "pdf_page": 2, "review_type": "content_diff",
                             "bbox_norm": [0.1, 0.3, 0.9, 0.4], "context": {"mineru": ""}},
                            ensure_ascii=False) + "\n")
        fh.write(json.dumps({"item_id": "TR-R1-0002-01", "pdf_page": 2, "review_type": "transcribe_region",
                             "bbox_norm": [0.1, 0.6, 0.9, 0.7], "context": {"mineru": ""}},
                            ensure_ascii=False) + "\n")
    with open(os.path.join(e3, "s5-out", "fxg", "judge-worklist.jsonl"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"item_id": "MT-P0002-01", "pdf_page": 2, "state": "decided",
                             "verdict": "exists",
                             "errata": {"kind": "missing_text",
                                        "missing_text": "[答案: −3.057×10³ J]",
                                        "anchor": {"before": "题 12。", "after": "题 13。",
                                                   "position": "own_line", "confidence": "high"},
                                        "basis": "主引擎没有这段；见证有；该处有墨迹"},
                             "evidence": {"what_i_see": _long_ev}}, ensure_ascii=False) + "\n")
        fh.write(json.dumps({"item_id": "TR-R1-0002-01", "pdf_page": 2, "state": "decided",
                             "verdict": "transcribed",
                             "evidence": {"what_i_see": _long_ev}}, ensure_ascii=False) + "\n")
    r3b = build("fxg", e3, b3)
    cl3b = open(os.path.join(r3b["kit"], "校对表.md"), encoding="utf-8").read()
    _s1 = cl3b.split("### 1.")[1].split("### 2.")[0] if "### 1." in cl3b else ""
    _s3 = cl3b.split("### 3.")[1] if "### 3." in cl3b else ""
    cases.append(("2026-11-15（P0-2）：`missing_text` 已判 → 进第 1 节「1b 产物缺这段」，**不进**第 3 节",
                  "1b. 已判定、但**产物里缺这段**" in _s1
                  and "[答案: −3.057×10³ J]" in _s1
                  and "MT-P0002-01" in _s1
                  and "MT-P0002-01" not in _s3, {"sec1": _s1[:200]}))
    cases.append(("2026-11-15（P0-3）：第 3 节证据放宽到 200 字，截断处有 `…`（原来 80 字无省略号）",
                  "我认为" in _s3 and "…" in _s3
                  and ("很长的说明" * 38) in _s3
                  and ("很长的说明" * 45) not in _s3, None))
    shutil.rmtree(d, ignore_errors=True)
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {json.dumps(extra, ensure_ascii=False)[:120]}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} S8 tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--book", required=True)
    r.add_argument("--s7-dir", default="")
    r.add_argument("--dpi", type=int, default=300)
    r.add_argument("--all-pages", action="store_true")
    r.add_argument("--hide-verdicts", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args()
    return selftest() if a.cmd == "selftest" else cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
