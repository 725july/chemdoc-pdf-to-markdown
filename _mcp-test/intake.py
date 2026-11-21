# -*- coding: utf-8 -*-
"""**A1 `intake.py`：把"这本书、这些页"变成一条可跑的书条目**（SPEC §3.11）。

设计要点（与其它配置同一套纪律）：
  * `--ranges` / `--anchors` / `--layout` 的页号 = **源 PDF 物理页号**（PDF 阅读器里的页序，1 起；
    **不是页面上印的页码**）；`,` 分段、`a-b` 或单页 `a`；**片内页恒为 1..N 连续**（既有口径）；
  * **硬校验，不许静默兜底**：段内 `a≤b` / 段间不重叠 / 展开无重复 / 不越界 / `key` 冲突需 `--force` /
    `--anchors` 严格递增且必须落在 `--ranges` 内 / `page_labels` 长度 == 切片页数；
  * **`--dry-run` 一个字都不落盘**；
  * 落盘是**事务式**的：先写临时文件并用 `books.load()` 校验通过，才原子替换 `books.json`；
  * 写完给出**下一步清单**（跑引擎 → S0 画像 → 管线），这就是"引导"的第一层。

用法:
    python -X utf8 intake.py interview --pdf <源书> --out questions.json   # 只读问卷（页数/哈希）
    python -X utf8 intake.py add --answers answers.json [--dry-run] [--force]
    python -X utf8 intake.py add --pdf <源书> --ranges 1-100,300-499 --key mybook --label "我的书" \
        [--anchors 1,15] [--layout "1-20:单栏, 21-100:两栏"]
                              [--engine mineru] [--out-dir _test-inputs] [--write-volumes] [--dry-run] [--force]
    python -X utf8 intake.py list | check | selftest
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile

import books as books_mod
import render
import volumes as volumes_mod
import paths  # B2：工作根唯一出口（SPEC §7.10）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.data_root()
BOOKS_JSON = os.path.join(HERE, "books.json")   # **旧位置**（B2b 之前）；真正位置见 books.registry_path()
REMOVED_LOG = "books-removed.jsonl"            # 注销台账（数据根下，只追加，供回滚）


class IntakeError(ValueError):
    """输入不合法（**必须报错**，不许静默兜底）。"""


# ---------------------------------------------------------------- 解析与校验
def parse_ranges(spec: str) -> list:
    """`1-100,300-499,7` → [(1,100),(300,499),(7,7)]；语法错即报错。"""
    if not spec or not spec.strip():
        raise IntakeError("--ranges 不能为空")
    out = []
    for chunk in str(spec).split(","):
        c = chunk.strip()
        if not c:
            raise IntakeError("--ranges 里有空段：%r" % spec)
        if "-" in c:
            parts = c.split("-")
            if len(parts) != 2:
                raise IntakeError("段格式错（应为 a-b 或 a）：%r" % c)
            try:
                a, b = int(parts[0]), int(parts[1])
            except ValueError:
                raise IntakeError("段不是整数：%r" % c)
        else:
            try:
                a = b = int(c)
            except ValueError:
                raise IntakeError("段不是整数：%r" % c)
        if a <= 0 or b <= 0:
            raise IntakeError("页号必须 ≥1：%r" % c)
        if a > b:
            raise IntakeError("段内 a > b：%r" % c)
        out.append((a, b))
    return out


def expand(ranges: list) -> list:
    ps = []
    for a, b in ranges:
        ps.extend(range(a, b + 1))
    return ps


def validate(ranges: list, total: int, anchors=None) -> list:
    """全部硬校验；返回展开后的页号列表（顺序 = 段顺序）。"""
    ps = expand(ranges)
    if len(set(ps)) != len(ps):
        dup = sorted({p for p in ps if ps.count(p) > 1})
        raise IntakeError("段间重叠/重复页：%s" % dup[:12])
    out_of = [p for p in ps if p > total]
    if out_of:
        raise IntakeError("页号越界（源书共 %d 页）：%s" % (total, out_of[:12]))
    for a, b in ranges:
        if b > total:
            raise IntakeError("段 %d-%d 超出源书页数 %d" % (a, b, total))
    if anchors:
        # **页号口径（2026-10-18）**：`--anchors` 和 `--ranges` 一样，用**源 PDF 物理页号**
        # （PDF 阅读器里的页序），不是页面上印的页码，也不是切片后的片内页号。
        if any(a not in set(ps) for a in anchors):
            raise IntakeError("--anchors 必须是 --ranges 内的**源 PDF 物理页号**：%s" % anchors)
        if list(anchors) != sorted(set(anchors)):
            raise IntakeError("--anchors 必须严格递增且不重复：%s" % anchors)
    return ps


def parse_anchors(spec: str):
    if not spec:
        return None
    try:
        out = [int(x) for x in str(spec).replace(" ", "").split(",") if x != ""]
    except ValueError:
        raise IntakeError("--anchors 不是整数列表：%r" % spec)
    return out


# ---------------------------------------------------------------- 规划
LAYOUT_KINDS = {"单栏": 1, "两栏": 2, "主副栏": 2, "三栏": 3}


def _compress_pages(pages: list) -> list:
    out = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        out.append([start, prev])
        start = prev = p
    out.append([start, prev])
    return out


def parse_layout(spec: str, source_pages: list):
    """解析 `--layout`（人话词表，页号=源 PDF 物理页号），并做覆盖校验。

    返回 `[{"pages": [[a,b],…], "kind": "两栏", "regions": 2, "cuts": [...]}]`。
    页段必须**恰好覆盖** `--ranges` 的全部源页、不重叠；`单栏` 不接受切分线。
    """
    if not spec or not str(spec).strip():
        return None
    src = set(source_pages)
    covered = set()
    segs = []
    for raw in str(spec).replace("；", ";").split(","):
        part = raw.strip()
        if not part:
            raise IntakeError("--layout 里有空段：%r" % spec)
        if ":" in part:
            pages_s, decl = part.split(":", 1)
        else:
            pages_s, decl = "", part
        decl = decl.strip()
        cuts = []
        if "@" in decl:
            decl, cut_s = decl.split("@", 1)
            try:
                cuts = [float(x) for x in cut_s.replace(" ", "").split("|") if x]
            except ValueError:
                raise IntakeError("--layout 切分线不是数字：%r" % cut_s)
        kind = decl.strip()
        if kind not in LAYOUT_KINDS:
            raise IntakeError("--layout 未知版式：%r（只支持 %s）"
                              % (kind, " / ".join(LAYOUT_KINDS)))
        regions = LAYOUT_KINDS[kind]
        if regions == 1 and cuts:
            raise IntakeError("--layout 单栏不接受切分线：%r" % part)
        if cuts and len(cuts) != regions - 1:
            raise IntakeError("--layout %s 需要 %d 条切分线（实际 %d）"
                              % (kind, regions - 1, len(cuts)))
        if any(not (0.0 < c < 1.0) for c in cuts) or cuts != sorted(set(cuts)):
            raise IntakeError("--layout 切分线必须在 (0,1) 内严格递增：%r" % cuts)
        if pages_s.strip():
            rs = parse_ranges(pages_s)
            if len(rs) != 1:
                raise IntakeError("--layout 每个页段只写一个范围：%r" % pages_s)
            pages = list(range(rs[0][0], rs[0][1] + 1))
        else:
            pages = list(source_pages)
        bad = [p for p in pages if p not in src]
        if bad:
            raise IntakeError("--layout 页号不在 --ranges 内（源 PDF 物理页号）：%s" % bad[:12])
        overlap = covered & set(pages)
        if overlap:
            raise IntakeError("--layout 页段重叠：%s" % sorted(overlap)[:12])
        covered.update(pages)
        segs.append({"pages": _compress_pages(pages), "kind": kind,
                     "regions": regions, "cuts": cuts})
    missing = sorted(src - covered)
    if missing:
        raise IntakeError("--layout 必须覆盖 --ranges 的全部源页；缺：%s" % missing[:12])
    segs.sort(key=lambda s: s["pages"][0][0])
    return segs


def pdf_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def interview(pdf: str, out: str = "") -> dict:
    """**只读**生成配置问卷（不逐页扫描，只读页数/哈希）。

    产出的 `questions.json` 是 agent 与用户之间的**契约**：agent 用 ask 或直接让用户填
    `answers.json`；`intake.py add --answers` 负责硬校验与落盘。
    """
    if not os.path.exists(pdf):
        raise IntakeError("源书不存在：%s" % pdf)
    doc = render.open_doc(pdf)
    try:
        total = render.page_count(doc)
    finally:
        render.close(doc)
    q = {
        "schema": 1,
        "pdf": os.path.abspath(pdf),
        "pdf_sha256": pdf_sha256(pdf),
        "page_count": total,
        "questions": [
            {"id": "key", "kind": "string", "required": True, "default": "",
             "prompt": "这本书的 key（英文/数字/短横线，作目录名）", "example": "my-book"},
            {"id": "label", "kind": "string", "required": True, "default": "",
             "prompt": "给人看的书名", "example": "我的化学教材"},
            {"id": "ranges", "kind": "ranges", "required": True,
             "default": "1-%d" % total,
             "prompt": "要处理哪些页？**源 PDF 物理页号**（不是页面上印的页码），如 1-300 或 1-20,30-50",
             "example": "1-%d" % total},
            {"id": "layout", "kind": "layout", "required": True, "default": "单栏",
             "prompt": "这本书的版面是哪种？**所有书都要声明**（单栏也要写「单栏」）——"
                       "「单栏」= 从上到下一行行读；「两栏」= 左右并排、左→右；"
                       "「主副栏」= 正文 + 旁边一条窄的补充说明（主栏先读）；「三栏」= 左→中→右。"
                       "若只有部分页不同，给页码范围（如 11-21:主副栏@0.275）；"
                       "不确定就先写「单栏」，**等 OCR 出 IR 后我拿 `layout_declare.py suggest` "
                       "的具体页面再问你一次**。",
             "example": "单栏"},
            {"id": "anchors", "kind": "int_list", "required": False, "default": "",
             "prompt": "每一章的首页（可选）：**源 PDF 物理页号**、逗号分隔、严格递增。"
                       "这些页之前的封面/前言/目录会单独成一篇（chapter-00）；"
                       "不想保留前置页，就把它们从 ranges 里排除",
             "example": "1,21,75"},
            {"id": "toc_pages", "kind": "ranges", "required": False, "default": "",
             "prompt": "目录页（可选）：**源 PDF 物理页号**，如 1-9。"
                       "这些页上出现的章名不会被当成新一章的开头（避免把目录当成很多章）；"
                       "目录内容仍归开头的 chapter-00",
             "example": "1-9"},
            {"id": "privacy_ack", "kind": "bool", "required": False, "default": True,
             "prompt": "是否允许把整页图上传云端（个人使用默认允许；对外发布须声明）"},
        ],
    }
    if out:
        with io.open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(q, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return q


def load_answers(path: str) -> dict:
    if not os.path.exists(path):
        raise IntakeError("answers 文件不存在：%s" % path)
    try:
        data = json.load(io.open(path, encoding="utf-8"))
    except Exception as exc:                                      # noqa: BLE001
        raise IntakeError("answers 不是合法 JSON：%s" % exc)
    if data.get("schema") != 1:
        raise IntakeError("answers schema 必须是 1（实际 %r）" % (data.get("schema"),))
    return data


def add_from_answers(answers_path: str, root: str = ROOT, out_dir: str = "_test-inputs",
                     engine: str = "mineru", dry_run: bool = False, force: bool = False,
                     write_volumes: bool = False) -> dict:
    """从 `answers.json` 登记一本书。

    **先验 PDF 指纹**（interview 时记的 sha256）：PDF 变了就报错、要求重答，不静默套旧答案。
    """
    data = load_answers(answers_path)
    pdf = data.get("pdf") or ""
    if not pdf or not os.path.exists(pdf):
        raise IntakeError("answers 里的 pdf 不存在：%r" % pdf)
    want = data.get("pdf_sha256")
    if want and pdf_sha256(pdf) != want:
        raise IntakeError("PDF 与 interview 时的指纹不一致（文件变了）→ 请重新 interview/重答")
    ans = data.get("answers") or {}
    key = str(ans.get("key") or "").strip()
    label = str(ans.get("label") or "").strip()
    if not key or not label:
        raise IntakeError("answers 缺 key 或 label（必填）")
    ranges = str(ans.get("ranges") or "").strip() or "1-%d" % (data.get("page_count") or 1)
    privacy = ans.get("privacy_ack", True)
    if not isinstance(privacy, bool):
        raise IntakeError("answers.privacy_ack 必须是布尔值")
    return add(pdf, ranges, key, label, root=root, out_dir=out_dir, engine=engine,
               anchors_spec=str(ans.get("anchors") or ""),
               layout_spec=str(ans.get("layout") or ""),
               toc_spec=str(ans.get("toc_pages") or ""),
               dry_run=dry_run, force=force, write_volumes=write_volumes,
               privacy_ack=privacy)


def plan(src_pdf: str, ranges: list, key: str, root: str = ROOT, out_dir: str = "_test-inputs",
         anchors=None, engine: str = "mineru", layout_spec: str = "",
         privacy_ack=None, toc_spec: str = "") -> dict:
    """算出"将要发生什么"（**不落盘**）：片内页数、page_labels、是否跳页、卷规划、书条目。"""
    if not os.path.exists(src_pdf):
        raise IntakeError("源书不存在：%s" % src_pdf)
    doc = render.open_doc(src_pdf)
    try:
        total = render.page_count(doc)
    finally:
        render.close(doc)
    pages = validate(ranges, total, anchors)
    layout_segs = parse_layout(layout_spec, pages) if layout_spec else None
    toc = sorted(set(expand(parse_ranges(toc_spec)))) if toc_spec else []
    if toc:
        _bad_toc = [p for p in toc if p not in set(pages)]
        if _bad_toc:
            raise IntakeError("--toc 页号不在 --ranges 内（源 PDF 物理页号）：%s" % _bad_toc[:12])
    labels = list(pages)                                   # page_labels = 源 PDF 物理页号序列
    contiguous = all((r[1] - r[0] + 1) == len(range(r[0], r[1] + 1)) for r in ranges) and len(ranges) == 1
    out_pdf = os.path.join(root, out_dir, "%s.pdf" % key)
    pb = None
    vols = None
    try:
        pb = render.measure_page_bytes(src_pdf, pages)
        vols = volumes_mod.plan_volumes(len(pages), volumes_mod.LIMITS[engine],
                                        {i + 1: pb[p] for i, p in enumerate(pages)})
    except Exception:                                      # noqa: BLE001  （卷规划只是预览，失败不阻断）
        vols = None
    entry = {"label": None, "key": key, "pdf": os.path.relpath(out_pdf, root).replace("\\", "/"),
             "mineru_dir": "ocr-raw/mineru-%s" % key, "paddle_dir": "ocr-raw/paddle-%s" % key,
             "profile": "s0-profiles/%s.profile.json" % key,
             "page_labels": labels,
             "source_pdf": os.path.relpath(src_pdf, root).replace("\\", "/"),
             "source_ranges": [list(r) for r in ranges]}
    if anchors:
        entry["chapter_anchors"] = list(anchors)
    if layout_segs:
        entry["layout"] = layout_segs
    if privacy_ack is not None:
        entry["privacy_ack"] = bool(privacy_ack)
    if toc:
        entry["toc_pages"] = list(toc)
    return {"key": key, "source_pdf": os.path.abspath(src_pdf), "source_pages": total,
            "ranges": [list(r) for r in ranges], "cut_pages": pages, "count": len(pages),
            "contiguous": contiguous, "page_labels": labels, "out_pdf": out_pdf,
            "volumes": ([{"id": v["id"], "cut_pages": v["cut_pages"]} for v in vols] if vols else None),
            "layout": layout_segs, "toc_pages": toc, "entry": entry}


def _rel_ranges(ranges: list) -> str:
    return ",".join(("%d-%d" % (a, b)) if a != b else str(a) for a, b in ranges)


def fmt_preview(p: dict, root: str = ROOT) -> str:
    labels = p["page_labels"]
    shown = ",".join(str(x) for x in labels[:12]) + ("…（共 %d）" % len(labels) if len(labels) > 12 else "")
    lines = [
        "【预览】key=%s" % p["key"],
        "  源书：%s（共 %d 页）" % (os.path.basename(p["source_pdf"]), p["source_pages"]),
        "  页段（**源 PDF 物理页号**）：%s → 片内页 1..%d（%s）"
        % (_rel_ranges(p["ranges"]), p["count"], "连续" if p["contiguous"] else "**跳页**（将登记 input_continuity.contiguous=false）"),
        "  page_labels：%s" % shown,
        ("  layout：%s" % json.dumps(p["layout"], ensure_ascii=False)) if p.get("layout") else "",
        ("  toc_pages：%s" % json.dumps(p["toc_pages"], ensure_ascii=False)) if p.get("toc_pages") else "",
        "  切片将写到：%s" % os.path.relpath(p["out_pdf"], root),
        "  卷规划（按 %s 上限）：%s" % ("mineru", ("%d 卷 %s" % (len(p["volumes"]), [v["cut_pages"] for v in p["volumes"]])
                                                    if p["volumes"] else "（预览失败，跳过）")),
        "  将写入 books.json 的条目：",
    ]
    for k, v in p["entry"].items():
        lines.append("      %-14s %s" % (k, json.dumps(v, ensure_ascii=False)))
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------- 落盘
def _write_books_atomic(root: str, key: str, entry: dict, force: bool) -> dict:
    path = books_mod.registry_path(root)
    if os.path.exists(path):
        data = json.load(io.open(path, encoding="utf-8"))
    else:                                   # **新机器第一次登记**：就地建一个空注册表（SPEC §3.13）
        data = {"schema": books_mod.SCHEMA_VERSION,
                "note": "书注册表（数据侧；权威位置 = 数据根下 books.json）。格式见 _mcp-test/books.sample.json。",
                "books": {}}
        print("[intake] 还没有注册表 → 新建：%s" % path)
    if key in data.get("books", {}) and not force:
        raise IntakeError("key 已存在：%s（要覆盖请加 --force）" % key)
    data.setdefault("books", {})[key] = entry
    tmp = path + ".tmp-intake"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    books_mod.load(path=tmp, root=root)                    # 事务式：校验不过就不替换
    os.replace(tmp, path)
    return data


def add(src_pdf: str, ranges_spec: str, key: str, label: str, root: str = ROOT,
        out_dir: str = "_test-inputs", anchors_spec: str = "", engine: str = "mineru",
        dry_run: bool = False, force: bool = False, write_volumes: bool = False,
        layout_spec: str = "", privacy_ack=None, toc_spec: str = "") -> dict:
    ranges = parse_ranges(ranges_spec)
    anchors = parse_anchors(anchors_spec)
    if not key or any(c in key for c in "/\\"):
        raise IntakeError("--key 非法（不能为空、不能含路径分隔符）：%r" % key)
    if not label:
        raise IntakeError("--label 不能为空")
    p = plan(src_pdf, ranges, key, root=root, out_dir=out_dir, anchors=anchors,
             engine=engine, layout_spec=layout_spec, privacy_ack=privacy_ack,
             toc_spec=toc_spec)
    p["entry"]["label"] = label
    print(fmt_preview(p, root))
    if not (dry_run or True):
        pass
    if dry_run:
        print("\n[dry-run] **未写任何文件**。确认无误后去掉 --dry-run 重跑。")
        return {"dry_run": True, **p}
    # ① 切片
    info = render.slice_ranges(src_pdf, p["cut_pages"], p["out_pdf"])
    assert info["count"] == p["count"], "切片页数与计划不一致"
    # ② 写 books.json（事务式）
    data = _write_books_atomic(root, key, p["entry"], force)
    # ③ 卷规范（可选）
    if write_volumes:
        vp = os.path.join(root, "ocr-raw", "volumes-%s.json" % key)
        os.makedirs(os.path.dirname(vp), exist_ok=True)
        with io.open(vp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"book": key, "source_pdf": p["out_pdf"], "engine": engine,
                       "limits": volumes_mod.LIMITS[engine], "page_count": p["count"],
                       "volumes": p["volumes"] or []}, fh, ensure_ascii=False, indent=2)
        print("  卷规范已写：%s" % os.path.relpath(vp, root))
    print("\n[完成] 切片 %d 页 → %s；books.json 已更新（共 %d 本）"
          % (p["count"], os.path.relpath(p["out_pdf"], root), len(data["books"])))
    print("\n【下一步】")
    print("  $py='%s'" % sys.executable)
    print("  1) 跑引擎：把两引擎的原生输出放进 %s / %s" % (p["entry"]["mineru_dir"], p["entry"]["paddle_dir"]))
    print("     （本地 Paddle 需 GPU；本机 RTX 4060 约 6.9 GB 空闲，可跑；见 engines.json/preflight）")
    print("  2) 出 S0 画像：& $py -X utf8 _mcp-test\\s0_classify.py profile --pdf \"%s\" --out \"s0-profiles\\%s.profile.json\""
          % (p["out_pdf"], key))
    print("  3) 跑管线：& $py -X utf8 _mcp-test\\pipeline_run.py book --book %s --no-publish" % key)
    return {"dry_run": False, **p}


# ---------------------------------------------------------------- 生命周期（注册 / 退休 / 恢复 / 注销）
def _load_registry(root: str = ROOT) -> tuple:
    path = books_mod.registry_path(root)
    if not os.path.exists(path):
        raise IntakeError("还没有书注册表：%s（先 add 一本，或确认 CHEMDOC_ROOT/BOOKWF_BOOKS）" % path)
    return path, json.load(io.open(path, encoding="utf-8"))


def _save_registry(path: str, data: dict, root: str = ROOT) -> None:
    """事务式落盘：先写临时文件并过 `books.load()` 校验，再原子替换。"""
    tmp = path + ".tmp-intake"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    books_mod.load(path=tmp, root=root)
    os.replace(tmp, path)


def set_state(name: str, state: str, why: str = "", root: str = ROOT) -> dict:
    """把某条书**退休 / 恢复**（SPEC §3.13）。**不删数据、不删条目** —— 只是可声明的状态。"""
    path, data = _load_registry(root)
    bks = data.get("books", {})
    if name not in bks:
        raise IntakeError("注册表里没有这条：%s（现有：%s）" % (name, ", ".join(sorted(bks))))
    cfg = bks[name]
    if state == "retired":
        if not str(why or "").strip():
            raise IntakeError("退休必须写清为什么：--why \"…\"（state=retired 不带 why 会被 books.load() 拒绝）")
        cfg["state"] = "retired"
        cfg["why"] = why
        cfg["retired_at"] = _now()
    else:
        cfg.pop("state", None)
        cfg.pop("why", None)
        cfg.pop("retired_at", None)
    _save_registry(path, data, root)
    return {"book": name, "key": cfg.get("key"), "state": cfg.get("state", "active"),
            "registry": path, "total": len(bks)}


def remove(name: str, force: bool = False, why: str = "", root: str = ROOT) -> dict:
    """**注销**（真删条目）。默认拒绝；必须 `--force`；被删条目**追加**进 `<数据根>/books-removed.jsonl`。

    为什么这么保守：注册表是"这本书的输入/产物在哪"的**唯一地图**；条目一删，
    `validate_run`/`publish`/`release` 都找不到它了。所以：① 先要求 `retire`（可恢复）；
    ② 真删要显式 `--force`；③ 删掉的内容**留一份台账**（只追加），随时能 `restore` 回来。
    """
    path, data = _load_registry(root)
    bks = data.get("books", {})
    if name not in bks:
        raise IntakeError("注册表里没有这条：%s" % name)
    cfg = bks[name]
    if not force:
        raise IntakeError("拒绝删条目：%s。**先退休**（intake.py retire %s --why \"…\"）；"
                          "确要真删请显式加 --force（会写一份台账到 %s）" % (name, name, REMOVED_LOG))
    log = os.path.join(root, REMOVED_LOG)
    with io.open(log, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"removed_at": _now(), "why": why, "name": name, "entry": cfg},
                            ensure_ascii=False) + "\n")
    del bks[name]
    _save_registry(path, data, root)
    return {"removed": name, "key": cfg.get("key"), "log": log, "remaining": len(bks)}


def set_layout(book_key: str, layout_spec: str, root: str = ROOT, dry_run: bool = False,
               clear: bool = False) -> dict:
    """**两段式的第二段落盘**：声明/更新一本书的版式（只改这一本；事务式写）。

    为什么单独一条命令：`add` 会重新切片，而第二段发生在**引擎跑完之后**——
    那时只需要改 `books.json` 里的 `layout`，不该再动 PDF/切片。
    """
    path, data = _load_registry(root)
    bks = data.get("books", {})
    if book_key not in bks:
        raise IntakeError("注册表里没有这本书：%s（先 add 登记）" % book_key)
    entry = dict(bks[book_key])
    src_pages = []
    for a, b in (entry.get("source_ranges") or []):
        src_pages.extend(range(int(a), int(b) + 1))
    if not src_pages:
        src_pages = [int(p) for p in (entry.get("page_labels") or [])]
    if not src_pages:                     # 老条目（C2 夹具等）没有映射 → 用卷规范的 book_pages
        vp = os.path.join(root, "ocr-raw", "volumes-%s.json" % book_key)
        if os.path.exists(vp):
            try:
                vd = json.load(io.open(vp, encoding="utf-8"))
                for v in (vd.get("volumes") or []):
                    src_pages.extend(int(p) for p in (v.get("book_pages") or []))
            except Exception:                                      # noqa: BLE001
                src_pages = []
    if not src_pages:                     # 最后兜底：按切片 PDF 的 1..N（与书级序列同空间）
        pdf = os.path.join(root, entry.get("pdf") or "")
        if entry.get("pdf") and os.path.exists(pdf):
            doc = render.open_doc(pdf)
            try:
                src_pages = list(range(1, render.page_count(doc) + 1))
            finally:
                render.close(doc)
            print("⚠ 该书没有 source_ranges/page_labels → 按切片页 1..%d 校验声明"
                  "（与 volumes 的 book_pages 同空间）" % len(src_pages))
    src_pages = sorted(set(src_pages))
    if not src_pages:
        raise IntakeError("该书没有 source_ranges/page_labels，也没有可读的切片 PDF，无法校验声明页号")
    if clear:
        prev = entry.pop("layout", None)
        segs = None
        print("【版式声明】%s：清除（原 %s）" % (book_key, json.dumps(prev, ensure_ascii=False)))
    else:
        segs = parse_layout(layout_spec, src_pages)
        if not segs:
            raise IntakeError("--layout 为空（要清除已有声明请用 --clear）")
        entry["layout"] = segs
        print("【版式声明】%s：%s" % (book_key, json.dumps(segs, ensure_ascii=False)))
    if dry_run:
        print("[dry-run] 未写 books.json。确认无误后去掉 --dry-run 重跑。")
        return {"dry_run": True, "book": book_key, "layout": segs}
    bks[book_key] = entry
    _save_registry(path, data, root)
    print("已写入：%s" % path)
    return {"book": book_key, "layout": segs, "registry": path}


def cmd_layout(a) -> int:
    r = set_layout(a.book, a.layout, root=a.root, dry_run=a.dry_run, clear=a.clear)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def set_ask_default(book_key: str = "", kind: str = "non_book_ink", value: str = "",
                    root: str = ROOT, reset: bool = False, global_: bool = False) -> dict:
    """记/清**按问题类型的默认答复**（`books.json.ask_defaults`；SPEC §3.16）。

    只在用户明确说过"**后续同类问题都按这个办，不再问**"之后才写；它是**用户裁定**的落盘处，
    不是 agent 的偏好。判定/报告里引用时必须写「默认沿用（来源：books.json ask_defaults.<kind>）」。

    **2026-11-15（P2-1）**：`global_=True` 写**顶层**（对所有书生效）；书级条目**优先于**全局。
    生效值与来源在 `books.load()` 里合并（`ask_defaults_source`）。
    """
    path, data = _load_registry(root)
    if kind not in books_mod.ASK_DEFAULT_KINDS:
        raise IntakeError("未知问题类型：%s（允许：%s）"
                          % (kind, " / ".join(books_mod.ASK_DEFAULT_KINDS)))
    val = (value or "").strip() or books_mod.ASK_DEFAULT_VALUES[kind]
    if not reset and val != books_mod.ASK_DEFAULT_VALUES[kind]:
        raise IntakeError("%s 目前只接受 %r（实际 %r）"
                          % (kind, books_mod.ASK_DEFAULT_VALUES[kind], val))
    if global_:
        ad = dict(data.get("ask_defaults") or {})
        if reset:
            ad.pop(kind, None)
        else:
            ad[kind] = val
        if ad:
            data["ask_defaults"] = ad
        else:
            data.pop("ask_defaults", None)
        _save_registry(path, data, root)
        return {"scope": "global", "ask_defaults": ad, "registry": path,
                "note": "全局默认影响所有书（书级同类型答复优先）；已落盘的判定不回溯"}
    bks = data.get("books", {})
    if book_key not in bks:
        raise IntakeError("注册表里没有这本书：%s（先 add 登记）" % book_key)
    entry = dict(bks[book_key])
    ad = dict(entry.get("ask_defaults") or {})
    if reset:
        ad.pop(kind, None)
    else:
        ad[kind] = val
    if ad:
        entry["ask_defaults"] = ad
    else:
        entry.pop("ask_defaults", None)
    bks[book_key] = entry
    data["books"] = bks
    _save_registry(path, data, root)
    return {"book": book_key, "ask_defaults": ad, "registry": path,
            "note": "默认只影响**后续同类询问**；已落盘的判定不回溯（要改就重判）"}


def cmd_ask_default(a) -> int:
    r = set_ask_default(getattr(a, "book", "") or "", a.kind, a.value, root=a.root,
                        reset=a.reset, global_=getattr(a, "global_", False))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_retire(a) -> int:
    r = set_state(a.name, "retired", a.why, root=a.root)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("（数据**一个字节都没动**；只是这条不再要求数据在位。恢复：intake.py restore %s）" % a.name)
    return 0


def cmd_restore(a) -> int:
    r = set_state(a.name, "active", "", root=a.root)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_remove(a) -> int:
    r = remove(a.name, force=a.force, why=a.why, root=a.root)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_removed(a) -> int:
    log = os.path.join(a.root, REMOVED_LOG)
    if not os.path.exists(log):
        print("（没有注销台账）"); return 0
    for line in io.open(log, encoding="utf-8"):
        if line.strip():
            d = json.loads(line)
            print("%s  %-16s key=%-18s why=%s" % (d.get("removed_at"), d.get("name"),
                                                  (d.get("entry") or {}).get("key"), d.get("why") or ""))
    return 0


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 子命令
def cmd_list(_a) -> int:
    path = books_mod.registry_path()
    if not os.path.exists(path):
        print("还没有书注册表：%s（新机器正常状态；用 add 建第一本）" % path)
        return 0
    data = json.load(io.open(path, encoding="utf-8"))
    print("书注册表：%s（%d 本）" % (path, len(data.get("books", {}))))
    for k, v in sorted(data.get("books", {}).items()):
        extra = []
        if v.get("source_ranges"):
            extra.append("源段=%s" % _rel_ranges([tuple(x) for x in v["source_ranges"]]))
        if v.get("page_labels"):
            extra.append("标签 %d 项" % len(v["page_labels"]))
        if v.get("chapter_anchors"):
            extra.append("章锚 %s" % v["chapter_anchors"])
        st = v.get("state", "active")
        print("  %-16s key=%-18s [%-7s] %s  %s" % (k, v.get("key"), st, v.get("label", "")[:26], " · ".join(extra)))
    return 0


def cmd_check(_a) -> int:
    b = books_mod.load()
    miss = books_mod.validate_existence(b)
    retired = sorted(k for k, v in b.items() if v.get("state") == "retired")
    print(json.dumps({"books": len(b), "retired": retired, "missing": miss, "ok": not miss},
                     ensure_ascii=False, indent=2))
    return 0 if not miss else 1


def selftest() -> int:
    cases = []
    # 解析与校验（负例为主）
    cases.append(("区间解析：1-3,7,9-10", parse_ranges("1-3,7,9-10") == [(1, 3), (7, 7), (9, 10)], None))
    for bad, why in (("", "空"), ("1-", "段格式"), ("a-b", "非整数"), ("0-3", "页号≥1"), ("5-2", "a>b"),
                     ("1,,3", "空段")):
        try:
            parse_ranges(bad)
            cases.append(("负例：--ranges %s → 报错" % why, False, bad))
        except IntakeError:
            cases.append(("负例：--ranges %s → 报错" % why, True, None))
    try:
        validate([(1, 5), (4, 8)], 100)
        cases.append(("负例：段间重叠 → 报错", False, None))
    except IntakeError:
        cases.append(("负例：段间重叠 → 报错", True, None))
    try:
        validate([(1, 5)], 3)
        cases.append(("负例：越界 → 报错", False, None))
    except IntakeError:
        cases.append(("负例：越界 → 报错", True, None))
    try:
        validate([(1, 5)], 10, anchors=[3, 2])
        cases.append(("负例：--anchors 非递增 → 报错", False, None))
    except IntakeError:
        cases.append(("负例：--anchors 非递增 → 报错", True, None))
    try:
        validate([(1, 5)], 10, anchors=[9])
        cases.append(("负例：--anchors 越界 → 报错", False, None))
    except IntakeError:
        cases.append(("负例：--anchors 越界 → 报错", True, None))
    _ps = validate([(22, 26)], 30, anchors=[22, 26])
    cases.append(("--anchors 用**源 PDF 物理页号**（22,26 ∈ 22-26）",
                  _ps == [22, 23, 24, 25, 26], _ps))
    try:
        validate([(22, 26)], 30, anchors=[1, 5])
        cases.append(("负例：--anchors 不在 --ranges 内（1,5 不是源页号）→ 报错", False, None))
    except IntakeError:
        cases.append(("负例：--anchors 不在 --ranges 内（1,5 不是源页号）→ 报错", True, None))
    # 版式声明（SPEC §3.16）：人话词表 + 源页面覆盖 + 切分线
    _lay = parse_layout("1-2:单栏, 3-5:两栏", [1, 2, 3, 4, 5])
    cases.append(("--layout 人话词表（单栏/两栏）+ 源页覆盖",
                  [s["kind"] for s in _lay] == ["单栏", "两栏"]
                  and [s["pages"] for s in _lay] == [[[1, 2]], [[3, 5]]], _lay))
    _lay2 = parse_layout("3-5:主副栏@0.27", [3, 4, 5])
    cases.append(("--layout 主副栏@0.27 → 2 区 + 1 条切分线",
                  _lay2[0]["regions"] == 2 and _lay2[0]["cuts"] == [0.27], _lay2))
    try:
        parse_layout("1-2:两栏", [1, 2, 3, 4, 5])
        cases.append(("负例：--layout 未覆盖全部源页 → 报错", False, None))
    except IntakeError:
        cases.append(("负例：--layout 未覆盖全部源页 → 报错", True, None))
    try:
        parse_layout("1-5:三栏@0.3", [1, 2, 3, 4, 5])
        cases.append(("负例：--layout 三栏只给 1 条切分线 → 报错", False, None))
    except IntakeError:
        cases.append(("负例：--layout 三栏只给 1 条切分线 → 报错", True, None))

    # 端到端（在临时 root 里，不碰真实工作区）
    from pypdf import PdfWriter
    tmp = tempfile.mkdtemp(prefix="intake-")
    try:
        os.makedirs(os.path.join(tmp, "_mcp-test"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "_test-inputs"), exist_ok=True)
        with io.open(os.path.join(tmp, "books.json"), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"schema": 1, "note": "测试用", "books": {}}, fh, ensure_ascii=False, indent=2)
        w = PdfWriter()
        for _ in range(10):
            w.add_blank_page(width=595, height=842)
        src = os.path.join(tmp, "src.pdf")
        with open(src, "wb") as fh:
            w.write(fh)

        # ① dry-run **不落盘**
        r = add(src, "1-3,7", "tbook", "测试书", root=tmp, dry_run=True)
        cases.append(("dry-run：不产出切片、不改 books.json",
                      (not os.path.exists(r["out_pdf"]))
                      and json.load(io.open(os.path.join(tmp, "books.json"), encoding="utf-8"))["books"] == {},
                      None))
        cases.append(("dry-run：预览内容正确（4 页、标签 [1,2,3,7]、跳页）",
                      r["count"] == 4 and r["page_labels"] == [1, 2, 3, 7] and r["contiguous"] is False, r["page_labels"]))
        # ② 真落盘
        r2 = add(src, "1-3,7", "tbook", "测试书", root=tmp, anchors_spec="1,3")
        bk = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("落盘：切片 4 页且文件存在", r2["count"] == 4 and os.path.exists(r2["out_pdf"]), None))
        cases.append(("落盘：books.json 通过 books.load() 校验（含新可选字段）",
                      "tbook" in bk and bk["tbook"]["page_labels"] == [1, 2, 3, 7]
                      and bk["tbook"]["chapter_anchors"] == [1, 3]
                      and bk["tbook"]["source_ranges"] == [[1, 3], [7, 7]], bk.get("tbook")))
        cases.append(("切片文件本身可读（4 页）",
                      render.page_count(render.open_doc(r2["out_pdf"])) == 4, None))
        # 版式声明落盘（SPEC §3.16）：源页号 + 人话词表
        r3 = add(src, "1-3,7", "tlay", "版式书", root=tmp,
                 layout_spec="1-3:单栏, 7:两栏")
        bk3 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("--layout 落盘到 books.json（段/区数/切分线）",
                      bk3["tlay"]["layout"][0]["kind"] == "单栏"
                      and bk3["tlay"]["layout"][1]["kind"] == "两栏"
                      and bk3["tlay"]["layout"][1]["regions"] == 2, bk3["tlay"].get("layout")))
        # 两段式第二段（SPEC §3.16/§3.17）：引擎跑完后就地更新声明，不重新切片
        r7 = set_layout("tlay", "1-3:主副栏@0.27, 7:单栏", root=tmp)
        bk7 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("layout 子命令：就地更新声明（切片/page_labels 不动）",
                      r7["layout"][0]["kind"] == "主副栏"
                      and bk7["tlay"]["layout"][0]["kind"] == "主副栏"
                      and bk7["tlay"]["page_labels"] == [1, 2, 3, 7], bk7["tlay"].get("layout")))
        set_layout("tlay", "1-3:单栏, 7:单栏", root=tmp, dry_run=True)
        bk8 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("layout --dry-run：不落盘（仍是原声明）",
                      bk8["tlay"]["layout"][0]["kind"] == "主副栏", bk8["tlay"].get("layout")))
        set_layout("tlay", "", root=tmp, clear=True)
        bk9 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("layout --clear：声明被移除", "layout" not in bk9["tlay"],
                      bk9["tlay"].get("layout")))
        try:
            set_layout("tlay", "1-3:两栏", root=tmp)
            cases.append(("负例：layout 未覆盖全部源页 → 报错", False, None))
        except IntakeError:
            cases.append(("负例：layout 未覆盖全部源页 → 报错", True, None))
        # ⑥b 2026-10-26：ask_defaults（按问题类型的默认答复；只在用户同意"以后别问"后才写）
        _ad = set_ask_default("tlay", "non_book_ink", root=tmp)
        bk_ad = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("ask-default：写上 non_book_ink=quarantine，books.load() 能读回",
                      _ad["ask_defaults"] == {"non_book_ink": "quarantine"}
                      and bk_ad["tlay"].get("ask_defaults", {}).get("non_book_ink") == "quarantine",
                      bk_ad["tlay"].get("ask_defaults")))
        _ad2 = set_ask_default("tlay", "non_book_ink", root=tmp, reset=True)
        bk_ad2 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("ask-default --reset：字段整个消失（恢复「每次都问」）",
                      _ad2["ask_defaults"] == {} and "ask_defaults" not in bk_ad2["tlay"],
                      bk_ad2["tlay"].get("ask_defaults")))
        # 2026-11-15（P2-1）：全局层（`--global`）——对所有书生效，书级优先
        _g = set_ask_default("", "non_book_ink", root=tmp, global_=True)
        _bk_g = books_mod.load(root=tmp)["tlay"]
        cases.append(("2026-11-15（P2-1）：`ask-default --global` 写顶层 → 书读到（来源 global）",
                      _g.get("scope") == "global"
                      and _bk_g.get("ask_defaults", {}).get("non_book_ink") == "quarantine"
                      and (_bk_g.get("ask_defaults_source") or {}).get("non_book_ink") == "global",
                      _bk_g.get("ask_defaults_source")))
        _g2 = set_ask_default("", "non_book_ink", root=tmp, global_=True, reset=True)
        _bk_g2 = books_mod.load(root=tmp)["tlay"]
        cases.append(("2026-11-15（P2-1）：`--global --reset` → 顶层字段消失、书里也读不到",
                      _g2.get("ask_defaults") == {}
                      and "non_book_ink" not in (_bk_g2.get("ask_defaults") or {}),
                      _bk_g2.get("ask_defaults")))
        try:
            set_ask_default("tlay", "handwriting", root=tmp)
            cases.append(("负例：ask-default 未知问题类型 → 报错", False, None))
        except IntakeError:
            cases.append(("负例：ask-default 未知问题类型 → 报错", True, None))
        try:
            set_ask_default("tlay", "non_book_ink", value="保持原样", root=tmp)
            cases.append(("负例：ask-default 值不在枚举里 → 报错（默认不是自由文本）", False, None))
        except IntakeError:
            cases.append(("负例：ask-default 值不在枚举里 → 报错（默认不是自由文本）", True, None))
        # ⑦ 问卷 → answers → add（SPEC §3.17）：只读页数/哈希，不逐页扫描
        qpath = os.path.join(tmp, "questions.json")
        q = interview(src, qpath)
        cases.append(("interview：questions.json 带页数/哈希/默认页段/7 问",
                      q["page_count"] == 10 and os.path.exists(qpath)
                      and len(q["questions"]) == 7
                      and any(x["id"] == "ranges" and x["default"] == "1-10" for x in q["questions"])
                      and any(x["id"] == "toc_pages" for x in q["questions"]),
                      {"page_count": q["page_count"], "questions": len(q["questions"])}))
        apath = os.path.join(tmp, "answers.json")
        ans = {"schema": 1, "pdf": src, "pdf_sha256": q["pdf_sha256"], "page_count": 10,
               "answers": {"key": "tans", "label": "问答书", "ranges": "1-5",
                           "layout": "1-2:单栏, 3-5:两栏", "anchors": "1,3",
                           "toc_pages": "1-2", "privacy_ack": False}}
        with io.open(apath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(ans, fh, ensure_ascii=False)
        r6 = add_from_answers(apath, root=tmp)
        bk6 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("add --answers：按 answers 落盘（含 layout/anchors/privacy_ack）",
                      r6["count"] == 5 and bk6["tans"]["chapter_anchors"] == [1, 3]
                      and bk6["tans"]["toc_pages"] == [1, 2]
                      and bk6["tans"]["privacy_ack"] is False
                      and bk6["tans"]["layout"][1]["kind"] == "两栏", bk6["tans"]))
        bad = dict(ans, pdf_sha256="0" * 64)
        bpath = os.path.join(tmp, "answers-bad.json")
        with io.open(bpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(bad, fh, ensure_ascii=False)
        try:
            add_from_answers(bpath, root=tmp)
            cases.append(("负例：answers 的 PDF 指纹不符 → 报错", False, None))
        except IntakeError:
            cases.append(("负例：answers 的 PDF 指纹不符 → 报错", True, None))
        bad2 = {"schema": 1, "pdf": src, "pdf_sha256": q["pdf_sha256"], "page_count": 10,
                "answers": {"key": "", "label": "无 key", "ranges": "1-2"}}
        cpath = os.path.join(tmp, "answers-nokey.json")
        with io.open(cpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(bad2, fh, ensure_ascii=False)
        try:
            add_from_answers(cpath, root=tmp)
            cases.append(("负例：answers 缺 key → 报错", False, None))
        except IntakeError:
            cases.append(("负例：answers 缺 key → 报错", True, None))
        # ③ key 冲突
        try:
            add(src, "1-2", "tbook", "重复", root=tmp)
            cases.append(("负例：key 已存在且无 --force → 报错", False, None))
        except IntakeError:
            cases.append(("负例：key 已存在且无 --force → 报错", True, None))
        # ④ --force 覆盖
        r4 = add(src, "2-4", "tbook", "覆盖后", root=tmp, force=True)
        bk4 = books_mod.load(path=os.path.join(tmp, "books.json"), root=tmp)
        cases.append(("--force 覆盖：标签更新为 [2,3,4]",
                      r4["page_labels"] == [2, 3, 4] and bk4["tbook"]["page_labels"] == [2, 3, 4], None))
        # ⑤ 连续段（单段）判为连续
        r5 = plan(src, [(1, 5)], "t2", root=tmp)
        cases.append(("单段连续 → contiguous=True", r5["contiguous"] is True, None))
        # ⑥ 源书不存在
        try:
            plan(os.path.join(tmp, "nope.pdf"), [(1, 2)], "t3", root=tmp)
            cases.append(("负例：源书不存在 → 报错", False, None))
        except IntakeError:
            cases.append(("负例：源书不存在 → 报错", True, None))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:120]))
        passed += bool(ok)
    print("\n%d/%d intake tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def _dispatch_add(x) -> int:
    if x.answers:
        add_from_answers(x.answers, root=ROOT, out_dir=x.out_dir, engine=x.engine,
                         dry_run=x.dry_run, force=x.force, write_volumes=x.write_volumes)
        return 0
    if not (x.pdf and x.ranges and x.key and x.label):
        raise IntakeError("要么给 --pdf/--ranges/--key/--label，要么给 --answers")
    add(x.pdf, x.ranges, x.key, x.label, root=ROOT, out_dir=x.out_dir, engine=x.engine,
        anchors_spec=x.anchors, layout_spec=x.layout, toc_spec=x.toc,
        dry_run=x.dry_run, force=x.force, write_volumes=x.write_volumes)
    return 0


def _dispatch_interview(x) -> int:
    q = interview(x.pdf, x.out)
    print(json.dumps({"pdf": q["pdf"], "page_count": q["page_count"],
                      "pdf_sha256": q["pdf_sha256"][:16] + "…",
                      "questions": len(q["questions"]), "out": x.out or None},
                     ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="登记一本书（可直接给参数，或 --answers 走问卷）")
    a.add_argument("--pdf", default="")
    a.add_argument("--ranges", default="",
                   help="源 PDF 物理页号（1 起，PDF 阅读器里的页序；不是页面上印的页码），如 1-100,300-499")
    a.add_argument("--key", default="")
    a.add_argument("--label", default="")
    a.add_argument("--answers", default="", help="interview 产出的 answers.json（含 key/label/ranges/layout/anchors）")
    a.add_argument("--anchors", default="",
                   help="章锚：源 PDF 物理页号、逗号分隔、严格递增、必须落在 --ranges 内，如 1,15,40")
    a.add_argument("--layout", default="",
                   help="版式声明（人话）：单栏/两栏/主副栏/三栏；页号=源 PDF 物理页号；"
                        "如 \"两栏\" 或 \"1-20:单栏, 21-300:主副栏@0.27\"")
    a.add_argument("--toc", default="",
                   help="目录页：源 PDF 物理页号、逗号分隔（如 1-9）；这些页的章标题不进切章器")
    a.add_argument("--engine", default="mineru", choices=sorted(volumes_mod.LIMITS))
    a.add_argument("--out-dir", default="_test-inputs")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true")
    a.add_argument("--write-volumes", action="store_true")
    a.set_defaults(func=_dispatch_add)
    iv = sub.add_parser("interview", help="只读生成 questions.json（页数/哈希；不逐页扫描）")
    iv.add_argument("--pdf", required=True)
    iv.add_argument("--out", default="", help="写到该路径（不写则只打印摘要）")
    ly = sub.add_parser("layout", help="声明/更新一本书的版式（两段式的第二段落盘）")
    ly.add_argument("--book", required=True)
    ly.add_argument("--layout", default="", help="人话词表，如 \"1-10:单栏, 11-21:主副栏@0.275\"")
    ly.add_argument("--clear", action="store_true", help="清除已有声明")
    ly.add_argument("--dry-run", action="store_true")
    ly.add_argument("--root", default=ROOT)
    ly.set_defaults(func=cmd_layout)
    ad = sub.add_parser("ask-default",
                        help="记/清「后续同类问题都按这个办」的默认答复（books.json.ask_defaults）")
    ad.add_argument("--global", dest="global_", action="store_true",
                    help="写到顶层（**对所有书生效**；书级同类型答复优先）")
    ad.add_argument("--book", default="")
    ad.add_argument("--kind", default="non_book_ink",
                    choices=list(books_mod.ASK_DEFAULT_KINDS), help="问题类型")
    ad.add_argument("--value", default="", help="默认答复（留空 = 该类型的标准答复）")
    ad.add_argument("--reset", action="store_true", help="清除该类型的默认（恢复为「每次都问」）")
    ad.add_argument("--root", default=ROOT)
    ad.set_defaults(func=cmd_ask_default)
    iv.set_defaults(func=_dispatch_interview)
    _l = sub.add_parser("list"); _l.add_argument("--root", default=ROOT); _l.set_defaults(func=cmd_list)
    _c = sub.add_parser("check"); _c.add_argument("--root", default=ROOT); _c.set_defaults(func=cmd_check)
    rt = sub.add_parser("retire", help="标记某本书退休（保留条目；不再要求数据在位）")
    rt.add_argument("name"); rt.add_argument("--why", default=""); rt.add_argument("--root", default=ROOT)
    rt.set_defaults(func=cmd_retire)
    rs = sub.add_parser("restore", help="把退休的书恢复为 active")
    rs.add_argument("name"); rs.add_argument("--root", default=ROOT)
    rs.set_defaults(func=cmd_restore)
    rm = sub.add_parser("remove", help="**注销**（真删条目；默认拒绝，须 --force，会写台账）")
    rm.add_argument("name"); rm.add_argument("--force", action="store_true")
    rm.add_argument("--why", default=""); rm.add_argument("--root", default=ROOT)
    rm.set_defaults(func=cmd_remove)
    rv = sub.add_parser("removed", help="列出注销台账（只追加，可回滚）")
    rv.add_argument("--root", default=ROOT); rv.set_defaults(func=cmd_removed)
    sub.add_parser("selftest").set_defaults(func=lambda x: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IntakeError as exc:                 # 命令行的错误走人话，不甩 traceback
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(2)
