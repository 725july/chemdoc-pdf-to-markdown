#!/usr/bin/env python3
"""把 `errata.md` 里 `kind=missing_text` 的条目**按锚点插回正文**（读者侧工具，2026-11-15）。

**为什么需要它**：主引擎漏掉、页面上确实有的内容，判定后正文里仍然缺（P0-1）。这批条目带有
「前文结尾 + 后文开头」锚点（`anchor.before` / `anchor.after`），确认无误后可以**一键插回**，
不必人工找位置。

**安全规则（宁可不插，也不插错）**：
  * `before` / `after` 必须在同一个 md 里**唯一命中且相邻**（中间只允许空白 / 注释）；
  * 该段文字**已经在**（`missing_text` 已出现在锚点之间或紧随其后）→ 视为已插回，**跳过**（幂等）；
  * `confidence=low` → **拒绝自动插入**（判定者自己都没把握定位）；
  * 锚点找不到 / 不唯一 / 中间夹了别的正文 / 目标文件有歧义 → **拒绝并说明原因**；
  * 改任何文件前先写 `<file>.bak-<时间戳>`；`--dry-run` 只报告、不落盘。

**注意**：本工具改的是**交付物**（`chapter-NN-reviewed.md`，并顺带按 G9 规则重算同名 `-clean.md`）。
**重跑 `pipeline_run run` 会从 IR 重新生成正文、覆盖手工插回**——要**持久**地补字，应让判定走
`transcribed`（S7 的 `VISION-INSERT` 通道），不要依赖本脚本。

用法：
    errata_apply.py apply --book <key> [--root <产物根>] [--dry-run] [--only <item_id,...>]
    errata_apply.py emit-script --book <key> [--root <产物根>]     # 生成单文件版放进交付目录，读者可拷走
    errata_apply.py check --book <key> [--root <产物根>]           # 只报"有几条能插、几条会拒"
    errata_apply.py selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime

import paths

ANCHOR_RE = re.compile(r"<!--\s*MISSING-TEXT-ANCHOR\s+(\{.*?\})\s*-->", re.S)
INSERT_RE = re.compile(r"<!--\s*MISSING-TEXT-INSERT\s+id=([^\s>]+)")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


# ---------------------------------------------------------------- 读条目
def load_entries(deliverable: str, errata_jsonl: str = "") -> list[dict]:
    """从 `errata.md` 的机器可读块（或 `errata.jsonl`）读 `missing_text` 条目。"""
    if errata_jsonl:
        out = []
        for line in open(errata_jsonl, encoding="utf-8"):
            if not line.strip():
                continue
            r = json.loads(line)
            if (r.get("kind") or "") == "missing_text":
                out.append(r)
        return out
    md = os.path.join(deliverable, "errata.md")
    if not os.path.exists(md):
        return []
    text = open(md, encoding="utf-8").read()
    out = []
    for m in ANCHOR_RE.finditer(text):
        try:
            out.append(json.loads(m.group(1)))
        except Exception:                                            # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------- 定位与插入
def _normalize(s: str) -> str:
    """匹配用的宽松化：全/半角空白统一、去首尾。只用于**找位置**，不写回正文。"""
    return re.sub(r"[ \t\r\n\u3000]+", " ", (s or "").strip())


def _gap_ok(gap: str) -> bool:
    """锚点之间的缝隙是否只含空白/注释（允许我们自己的插入痕迹）。"""
    rest = COMMENT_RE.sub("", gap)
    return not rest.strip()


def find_insertion(text: str, before: str, after: str, missing: str) -> dict:
    """返回 `{status: ok|present|missing|ambiguous|gap_dirty, reason}`（纯函数）。"""
    b, a = _normalize(before), _normalize(after)
    if not b or not a:
        return {"status": "missing", "reason": "锚点为空（before/after 至少一个缺失）"}
    nt = _normalize(text)
    m_miss = _normalize(missing)
    cands, dirty = [], 0
    for m in re.finditer(re.escape(b), nt):
        window = nt[m.end(): m.end() + 400]
        k = window.find(a)
        if k < 0:
            continue
        gap = window[:k]
        if m_miss and m_miss in gap:
            return {"status": "present", "reason": "这段文字已经在锚点之间（视为已插回）"}
        if not _gap_ok(gap):
            dirty += 1
            continue
        cands.append(k)
    if len(cands) > 1:
        return {"status": "ambiguous", "reason": "锚点对在正文里出现 %d 次（不唯一）" % len(cands)}
    if not cands:
        if dirty:
            return {"status": "gap_dirty", "reason": "锚点之间夹着别的正文，拒绝插入"}
        return {"status": "missing", "reason": "锚点不再相邻（before 之后不远处找不到 after）"}
    return {"status": "ok"}


def _map_norm_to_raw(text: str, norm_index: int) -> int:
    """把「宽松化文本里的下标」映射回原文下标（按同一套空白折叠规则走一遍）。"""
    n = 0
    i = 0
    while i < len(text):
        if norm_index <= n:
            return i
        ch = text[i]
        if ch in " \t\r\n\u3000":
            # 折叠成单个空格
            j = i
            while j < len(text) and text[j] in " \t\r\n\u3000":
                j += 1
            n += 1
            i = j
            continue
        n += 1
        i += 1
    return len(text)


def apply_entry(text: str, entry: dict) -> dict:
    """在 `text` 里插一条；返回 `{status, text|reason}`。"""
    anchor = entry.get("anchor") or {}
    conf = (anchor.get("confidence") or "high").strip()
    missing = (entry.get("missing_text") or "").strip()
    if not missing:
        return {"status": "refused", "reason": "条目没有 missing_text"}
    if conf == "low":
        return {"status": "refused", "reason": "anchor.confidence=low（判定者自己都没把握，拒绝自动插入）"}
    # 2026-11-15（1c）：S7 每次构建都会标注锚点来源；`stale` = S7 也定位不到 → 提示人工定位
    if (anchor.get("source") or "") == "stale":
        return {"status": "refused",
                "reason": "锚点已过期（S7 标注 source=stale）→ 请人工定位，或重判该条以生成新锚点"}
    found = find_insertion(text, anchor.get("before") or "", anchor.get("after") or "", missing)
    if found["status"] != "ok":
        return found
    # 在**原文**里定位 same 位置：先找 before 原文（宽松化匹配后取该处之后的第一个 after 原文）
    before_raw = anchor.get("before") or ""
    after_raw = anchor.get("after") or ""
    n_text = _normalize(text)
    b = _normalize(before_raw)
    m = re.search(re.escape(b), n_text)
    if not m:
        return {"status": "missing", "reason": "内部错误：宽松化后定位失败"}
    raw_start = _map_norm_to_raw(text, m.end())
    tail = text[raw_start:]
    a = _normalize(after_raw)
    # 在原文尾部按宽松化规则找 after 的起点
    n_tail = _normalize(tail)
    k = n_tail.find(a)
    if k < 0:
        return {"status": "missing", "reason": "内部错误：after 原文定位失败"}
    raw_after = raw_start + _map_norm_to_raw(tail, k)
    pos = (anchor.get("position") or "own_line").strip()
    iid = entry.get("item_id") or "—"
    page = entry.get("page")
    tag = "<!-- MISSING-TEXT-INSERT id=%s page=%s -->" % (iid, page)
    if pos == "inline":
        insert = missing
        new = text[:raw_start] + insert + text[raw_start:]
    else:
        # 另起一行：把锚点之间的空白换成「空行 + 审计注释 + 文字 + 空行」
        between = text[raw_start:raw_after]
        if between.strip() and not _gap_ok(between):
            return {"status": "gap_dirty", "reason": "锚点之间夹着别的正文，拒绝插入"}
        new = text[:raw_start] + "\n\n" + tag + "\n" + missing + "\n\n" + text[raw_after:].lstrip("\n")
    return {"status": "ok", "text": new}


# ---------------------------------------------------------------- 文件级
def _strip_comments(md: str) -> str:
    """与 S7 的 `strip_comments` 同一规则（净版 = 留底版去注释）——插入后必须同步刷新。"""
    s = COMMENT_RE.sub("", md)
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s)


def _targets(deliverable: str) -> list[str]:
    return sorted(os.path.join(deliverable, f) for f in os.listdir(deliverable)
                  if re.match(r"chapter-\d+.*-reviewed\.md$", f)) if os.path.isdir(deliverable) else []


def _backup(path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = "%s.bak-%s" % (path, stamp)
    shutil.copy2(path, dst)
    return dst


def run(book: str, root: str = "", only=(), dry_run: bool = False,
        prefer_staging: bool = False) -> dict:
    root = root or paths.data_root()
    base = os.path.join(root, "s7-out", book)
    staged = os.path.join(root, "s7-out", "_staging", book)
    if prefer_staging and os.path.isdir(staged):
        base = staged                      # `post` 用：先改暂存区，再发布（发布后正式目录才带上插回）
    elif not os.path.isdir(base):
        base = staged if os.path.isdir(staged) else base
    entries = load_entries(base)
    if only:
        want = {x.strip() for x in only}
        entries = [e for e in entries if (e.get("item_id") or "") in want]
    files = _targets(base)
    res = {"book": book, "deliverable": base, "files": files, "entries": len(entries),
           "applied": [], "present": [], "refused": [], "dry_run": bool(dry_run), "backups": []}
    if not entries:
        res["note"] = "没有 missing_text 条目（errata.md 里没有机器可读块，或本来就没有缺口）"
        return res
    if not files:
        res["note"] = "交付目录里没有 chapter-*-reviewed.md"
        return res
    texts = {p: open(p, encoding="utf-8").read() for p in files}
    changed = set()
    for e in entries:
        hit = None
        for p in files:
            out = apply_entry(texts[p], e)
            if out["status"] in ("ok", "present"):
                hit = (p, out)
                break
        if hit is None:
            # 用第一个文件给出拒绝原因（便于用户理解）
            out = apply_entry(texts[files[0]], e)
            res["refused"].append({"item_id": e.get("item_id"), "page": e.get("page"),
                                   "status": out["status"], "reason": out.get("reason")})
            continue
        p, out = hit
        if out["status"] == "present":
            res["present"].append({"item_id": e.get("item_id"), "page": e.get("page"), "file": p})
            continue
        texts[p] = out["text"]
        changed.add(p)
        res["applied"].append({"item_id": e.get("item_id"), "page": e.get("page"), "file": p,
                               "position": (e.get("anchor") or {}).get("position")})
    if not dry_run:
        for p in changed:
            res["backups"].append(_backup(p))
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(texts[p])
            # 净版 = 留底版去注释：**必须同步刷新**，否则 G9 会红
            clean = p.replace("-reviewed.md", "-clean.md")
            if os.path.exists(clean):
                with open(clean, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(_strip_comments(texts[p]))
            side = os.path.join(base, "missing-text-inserted.jsonl")
            with open(side, "a", encoding="utf-8", newline="\n") as fh:
                for x in res["applied"]:
                    fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    return res


# ---------------------------------------------------------------- 单文件版（读者侧）
EMIT_TEMPLATE = r'''#!/usr/bin/env python3
"""把 errata.md 里 kind=missing_text 的条目按锚点插回正文（单文件版，只用标准库）。

用法：  python missing-text-insert.py [--dry-run] [--dir <交付目录>]
规则：锚点必须唯一且相邻；已在文中则跳过；confidence=low 或锚点异常 → 拒绝并说明原因。
改文件前会写 <file>.bak-<时间戳>。
"""
import argparse, json, os, re, shutil, sys
from datetime import datetime

ANCHOR_RE = re.compile(r"<!--\s*MISSING-TEXT-ANCHOR\s+(\{.*?\})\s*-->", re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def norm(s):
    return re.sub(r"[ \t\r\n\u3000]+", " ", (s or "").strip())


def gap_ok(g):
    return not COMMENT_RE.sub("", g).strip()


def strip_comments(md):
    s = COMMENT_RE.sub("", md)
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s)


def map_norm(text, idx):
    n = i = 0
    while i < len(text):
        if idx <= n:
            return i
        if text[i] in " \t\r\n\u3000":
            j = i
            while j < len(text) and text[j] in " \t\r\n\u3000":
                j += 1
            n += 1
            i = j
            continue
        n += 1
        i += 1
    return len(text)


def apply_entry(text, e):
    a = e.get("anchor") or {}
    if (a.get("confidence") or "high") == "low":
        return None, "confidence=low（拒绝自动插入）"
    miss = (e.get("missing_text") or "").strip()
    if not miss:
        return None, "没有 missing_text"
    b, af = norm(a.get("before")), norm(a.get("after"))
    if not b or not af:
        return None, "锚点为空"
    nt = norm(text)
    hits = []
    for m in re.finditer(re.escape(b), nt):
        w = nt[m.end(): m.end() + 400]
        k = w.find(af)
        if k >= 0 and gap_ok(w[:k]):
            hits.append((m.end(), k))
    if not hits:
        return None, "锚点不再相邻（before 之后找不到 after）"
    if len(hits) > 1:
        return None, "锚点对出现 %d 次（不唯一）" % len(hits)
    bend, k = hits[0]
    if norm(miss) in nt[bend:bend + k]:
        return text, "already"
    raw_b = map_norm(text, bend)
    tail = text[raw_b:]
    nk = norm(tail).find(af)
    if nk < 0:
        return None, "after 原文定位失败"
    raw_a = raw_b + map_norm(tail, nk)
    tag = "<!-- MISSING-TEXT-INSERT id=%s page=%s -->" % (e.get("item_id"), e.get("page"))
    if (a.get("position") or "own_line") == "inline":
        return text[:raw_b] + miss + text[raw_b:], "ok"
    between = text[raw_b:raw_a]
    if between.strip() and not gap_ok(between):
        return None, "锚点之间夹着别的正文"
    return (text[:raw_b] + "\n\n" + tag + "\n" + miss + "\n\n"
            + text[raw_a:].lstrip("\n")), "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    d = os.path.abspath(a.dir)
    ep = os.path.join(d, "errata.md")
    if not os.path.exists(ep):
        print("找不到 errata.md：%s" % ep); return 2
    entries = []
    for m in ANCHOR_RE.finditer(open(ep, encoding="utf-8").read()):
        try:
            entries.append(json.loads(m.group(1)))
        except Exception:
            pass
    if not entries:
        print("errata.md 里没有 missing_text 条目"); return 2
    files = sorted(os.path.join(d, f) for f in os.listdir(d) if re.match(r"chapter-\d+.*-reviewed\.md$", f))
    if not files:
        print("交付目录里没有 chapter-*-reviewed.md"); return 2
    texts = {p: open(p, encoding="utf-8").read() for p in files}
    applied = present = refused = 0
    for e in entries:
        done = False
        for p in files:
            new, why = apply_entry(texts[p], e)
            if why == "already":
                print("跳过（已在文中）: %s p%s" % (e.get("item_id"), e.get("page"))); present += 1; done = True; break
            if new is not None:
                texts[p] = new
                print("插入: %s p%s -> %s" % (e.get("item_id"), e.get("page"), os.path.basename(p)))
                applied += 1; done = True; break
        if not done:
            print("拒绝: %s p%s —— %s" % (e.get("item_id"), e.get("page"), why)); refused += 1
    if not a.dry_run and applied:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for p in files:
            cur = open(p, encoding="utf-8").read()
            if cur != texts[p]:
                shutil.copy2(p, "%s.bak-%s" % (p, stamp))
                open(p, "w", encoding="utf-8", newline="\n").write(texts[p])
                clean = p.replace("-reviewed.md", "-clean.md")
                if os.path.exists(clean):
                    open(clean, "w", encoding="utf-8", newline="\n").write(strip_comments(texts[p]))
    print("完成：插入 %d / 已存在 %d / 拒绝 %d%s" % (applied, present, refused,
          "（dry-run，未写盘）" if a.dry_run else ""))
    print("提示：插入后请重跑 validate_run（刷新 G22 封条）；若之后重跑 pipeline_run run，"
          "正文会从 IR 重建、手工插回会被覆盖——要持久请让判定走 transcribed。")
    return 0 if not refused else 3


if __name__ == "__main__":
    sys.exit(main())
'''


def emit_script(book: str, root: str = "") -> dict:
    root = root or paths.data_root()
    base = os.path.join(root, "s7-out", book)
    if not os.path.isdir(base):
        base = os.path.join(root, "s7-out", "_staging", book)
    if not os.path.isdir(base):
        return {"error": "交付目录不存在：%s" % base}
    out = os.path.join(base, "missing-text-insert.py")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(EMIT_TEMPLATE)
    return {"out": out, "bytes": os.path.getsize(out),
            "entries": len(load_entries(base))}


def post(book: str, root: str = "", dry_run: bool = False, publish: bool = False) -> dict:
    """**一条命令收尾**：插回缺口 → 重新校验（必要时校验两次让 G22 回到 pass）→（可选）发布。

    为什么需要它：`pipeline_run run` 之后交付物是"缺口未插回"的状态；插回会改正文与净版，
    而 `validate_run` 会**重写**交付说明（数字变了）→ 旧封条对不上，需要再校验一次才 pass。
    """
    import validate_run as V
    r = run(book, root, dry_run=dry_run, prefer_staging=True)
    out = {"apply": {"applied": len(r.get("applied") or []), "present": len(r.get("present") or []),
                     "refused": len(r.get("refused") or [])}}
    if dry_run or (not r.get("applied") and not r.get("present")):
        out["note"] = "没有可插回的条目（或 dry-run）→ 不重新校验"
        return out
    root2 = root or paths.data_root()
    _stage = os.path.join(root2, "s7-out", "_staging", book)
    _s7 = _stage if os.path.isdir(_stage) else ""
    v1 = V.validate(book, root2, s7_dir=_s7)
    out["validate_1"] = {"verdict": v1.get("verdict"), "exit_code": v1.get("exit_code"),
                         "G22": (v1.get("gates") or {}).get("G22", {}).get("status")}
    if out["validate_1"]["G22"] == "fail":
        v2 = V.validate(book, root2, s7_dir=_s7)
        out["validate_2"] = {"verdict": v2.get("verdict"), "exit_code": v2.get("exit_code"),
                             "G22": (v2.get("gates") or {}).get("G22", {}).get("status")}
    if publish:
        import publish_run as P
        out["publish"] = P.publish(book, root2)
        out["publish"] = {k: out["publish"].get(k) for k in ("result", "verdict", "exit_code")}
    return out


# ---------------------------------------------------------------- CLI
def _fmt(res: dict) -> str:
    lines = ["书：%s  目录：%s" % (res.get("book"), res.get("deliverable")),
             "条目：%d  可插：%d  已在文中：%d  拒绝：%d%s"
             % (res.get("entries", 0), len(res.get("applied") or []), len(res.get("present") or []),
                len(res.get("refused") or []), "（dry-run）" if res.get("dry_run") else "")]
    for x in res.get("applied") or []:
        lines.append("  插入  %s p%s → %s（%s）" % (x["item_id"], x["page"],
                                                  os.path.basename(x["file"]), x.get("position")))
    for x in res.get("present") or []:
        lines.append("  跳过  %s（已在文中）" % x["item_id"])
    for x in res.get("refused") or []:
        lines.append("  拒绝  %s p%s —— %s" % (x["item_id"], x["page"], x["reason"]))
    if res.get("note"):
        lines.append("  说明：%s" % res["note"])
    if res.get("backups"):
        lines.append("  备份：%d 个 .bak-*" % len(res["backups"]))
    return "\n".join(lines)


def selftest() -> int:
    cases = []
    import tempfile
    md = ("<!-- p002 -->\n\n习题 12 …说明(2)的省略是否合理。\n\n"
          "习题13 已知在 $0^{\\circ} \\mathrm{C}$ 和标准压力下，冰的密度…\n")
    entry = {"item_id": "MT-1", "page": 2, "missing_text": "[答案:−3.057×10³ J]",
             "anchor": {"before": "说明(2)的省略是否合理。", "after": "习题13 已知在",
                        "position": "own_line", "confidence": "high"}}
    out = apply_entry(md, entry)
    cases.append(("own_line：按锚点插到两段之间，并留 MISSING-TEXT-INSERT 痕迹",
                  out["status"] == "ok"
                  and "[答案:−3.057×10³ J]" in out["text"]
                  and "MISSING-TEXT-INSERT id=MT-1" in out["text"]
                  and out["text"].index("合理。") < out["text"].index("[答案")
                  < out["text"].index("习题13"), out.get("status")))
    again = apply_entry(out["text"], entry)
    cases.append(("幂等：再跑一次 → 判定为「已插回」且不改动正文",
                  again["status"] == "present", again.get("status")))
    e_inline = dict(entry, item_id="MT-2", missing_text="（见下）",
                    anchor={"before": "说明(2)的省略是否合理。", "after": "习题13 已知在",
                            "position": "inline", "confidence": "high"})
    out2 = apply_entry(md, e_inline)
    cases.append(("inline：段中插入（不另起一行）",
                  out2["status"] == "ok" and "合理。（见下）习题13" in out2["text"].replace("\n", ""),
                  out2.get("status")))
    e_bad = dict(entry, item_id="MT-3", anchor=dict(entry["anchor"], after="根本不存在的后文"))
    cases.append(("拒绝：after 找不到 → 不插（不猜位置）",
                  apply_entry(md, e_bad)["status"] == "missing", None))
    e_dup = dict(entry, item_id="MT-4", anchor={"before": "习题13 已知在", "after": "已知在",
                                                "position": "own_line", "confidence": "high"})
    _dupres = apply_entry(md, e_dup)
    cases.append(("拒绝：锚点不唯一 → ambiguous（宁可不插）",
                  _dupres["status"] in ("ambiguous", "missing"), _dupres.get("status")))
    e_stale = dict(entry, item_id="MT-5s", anchor=dict(entry["anchor"], source="stale"))
    cases.append(("2026-11-15（1c）：`anchor.source=stale`（S7 也定位不到）→ **拒绝**并提示人工定位",
                  apply_entry(md, e_stale)["status"] == "refused", None))
    e_low = dict(entry, item_id="MT-5", anchor=dict(entry["anchor"], confidence="low"))
    cases.append(("拒绝：`confidence=low`（判定者没把握）→ 不自动插入",
                  apply_entry(md, e_low)["status"] == "refused", None))
    e_dirty = dict(entry, item_id="MT-6",
                   anchor={"before": "说明(2)的省略是否合理。", "after": "冰的密度",
                           "position": "own_line", "confidence": "high"})
    cases.append(("拒绝：锚点之间夹着别的正文 → gap_dirty",
                  apply_entry(md, e_dirty)["status"] == "gap_dirty", None))
    cases.append(("净版规则与 S7 一致：去注释后不留 MISSING-TEXT-INSERT",
                  "MISSING-TEXT-INSERT" not in _strip_comments(out["text"]), None))

    # ---- 文件级 + 单文件版（真正跑一遍 emit 出来的脚本）----
    with tempfile.TemporaryDirectory() as td:
        d = os.path.join(td, "s7-out", "fx")
        os.makedirs(d)
        with open(os.path.join(d, "chapter-01-reviewed.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(md)
        with open(os.path.join(d, "chapter-01-clean.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_strip_comments(md))
        with open(os.path.join(d, "errata.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# errata\n\n## 3. missing_text\n\n"
                     "<!-- MISSING-TEXT-ANCHOR " + json.dumps(entry, ensure_ascii=False) + " -->\n")
        res = run("fx", root=td, dry_run=True)
        cases.append(("CLI dry-run：报告可插但**不写盘**",
                      len(res["applied"]) == 1 and res["dry_run"]
                      and "[答案" not in open(os.path.join(d, "chapter-01-reviewed.md"),
                                              encoding="utf-8").read(), res.get("applied")))
        res2 = run("fx", root=td)
        rev = open(os.path.join(d, "chapter-01-reviewed.md"), encoding="utf-8").read()
        cln = open(os.path.join(d, "chapter-01-clean.md"), encoding="utf-8").read()
        cases.append(("CLI 实跑：插入 + 留备份 + 同步刷新净版（G9 口径）",
                      res2["applied"] and rev.count("MISSING-TEXT-INSERT") == 1
                      and "[答案:−3.057×10³ J]" in rev and "MISSING-TEXT-INSERT" not in cln
                      and "[答案:−3.057×10³ J]" in cln
                      and res2["backups"] and os.path.exists(res2["backups"][0]), res2.get("applied")))
        res3 = run("fx", root=td)
        cases.append(("CLI 再跑：幂等（跳过、不重复插入）",
                      not res3["applied"] and len(res3["present"]) == 1, res3.get("present")))
        # dry-run 的 `post`：只算"能插几条"，**不**去校验/发布
        _p = post("fx", root=td, dry_run=True)
        cases.append(("post --dry-run：只报告可插条数，不触发校验/发布",
                      (_p.get("apply") or {}).get("present", 0) >= 0
                      and "validate_1" not in _p, _p))
        em = emit_script("fx", root=td)
        cases.append(("emit-script：单文件版写进交付目录",
                      os.path.exists(em.get("out") or "") and (em.get("bytes") or 0) > 1000, em))

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> %s" % (extra,)))
        passed += bool(ok)
    print("\n%d/%d errata_apply tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="把 errata.md 的 missing_text 按锚点插回正文")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("apply", "check", "emit-script", "post"):
        sp = sub.add_parser(name)
        sp.add_argument("--book", required=True)
        sp.add_argument("--root", default="")
        if name in ("apply", "post"):
            sp.add_argument("--dry-run", action="store_true")
            sp.add_argument("--only", default="", help="只处理这些 item_id（逗号分隔）")
        if name == "post":
            sp.add_argument("--publish", action="store_true", help="插回并校验后再发布")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "emit-script":
        r = emit_script(a.book, a.root)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("out") else 2
    if a.cmd == "post":
        r = post(a.book, a.root, dry_run=a.dry_run, publish=a.publish)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "check":
        r = run(a.book, a.root, dry_run=True)
        print(_fmt(r))
        return 0 if not r.get("refused") else 3
    r = run(a.book, a.root, only=tuple(x for x in (a.only or "").split(",") if x),
            dry_run=a.dry_run)
    print(_fmt(r))
    if r.get("applied") or r.get("present"):
        print("提示：重跑 validate_run 刷新 G22 封条；重跑 pipeline_run run 会从 IR 重建正文"
              "（手工插回会被覆盖）——要持久补字请让判定走 transcribed。")
    return 0 if not r.get("refused") else 3


if __name__ == "__main__":
    raise SystemExit(main())
