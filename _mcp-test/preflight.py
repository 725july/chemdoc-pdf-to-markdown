# -*- coding: utf-8 -*-
"""**`preflight.py`（A2）**：只读、只校验，回答「**现在能不能跑、缺什么、补法是什么、会付出什么代价**」。

聚合（不重复造轮子）：
  1. **引擎槽位**（`engines.py`）：runner 存在性 / 凭证**掩码** / 本地模式的显存·包·模型目录 / 磁盘；
  2. **书**（`books.py`）：条目存在、`pdf`/`profile` 是否就位、切片页数、`page_labels`；
  3. **阈值**（`tuning.py`）：未验证参数计数（观测）；
  4. **密钥不落地**（`secret_scan`）：把现值全工作区搜一遍（**只报路径**）；
  5. **规模预测**：页数 → 卷数（真实测量切片 PDF 的页字节）→ **预计判定批数**（**投影，非实测**）；
  6. **隐私确认行**：本配置是否会把整页图上传云端（取决于 primary 的 mode）。

**绝不打印任何密钥值**（只报存在性/长度/掩码）。有阻塞项 → `exit 1`，否则 `exit 0`。

用法: python -X utf8 preflight.py --book <key> [--engine mineru|paddle]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

import books as books_mod
import engines as engines_mod
import render
import volumes as volumes_mod
from tuning import TUNE, meta, unverified
import paths  # B2：工作根唯一出口（SPEC §7.10）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.data_root()

# 出题/探针的**经验速率**（来自既有实测文档，非本机实测 → 只作投影）
RATE_S3 = (1.0, 2.0)          # S3 差异出题：每页 1.0–2.0 项
RATE_S4 = 1.25               # S4 探针（R1/R4）：每页 ≈1.25 项


def project(pages: int, items_per_batch: int = None, pages_per_batch: int = None) -> dict:
    """**投影**（不是实测）：预计出题量与判定批数。批数按"每批 ≤N 项 或 ≤M 页 先到者"估。"""
    ipb = int(items_per_batch or TUNE["items_per_batch"])
    ppb = int(pages_per_batch or TUNE["pages_per_batch"])
    s3_lo, s3_hi = int(pages * RATE_S3[0]), int(pages * RATE_S3[1])
    s4 = int(pages * RATE_S4)
    by_items = (s3_hi + s4 + ipb - 1) // ipb
    by_pages = (pages + ppb - 1) // ppb
    return {"pages": pages, "est_s3_items": [s3_lo, s3_hi], "est_s4_items": s4,
            "items_per_batch": ipb, "pages_per_batch": ppb,
            "est_batches": max(by_items, by_pages), "batches_by_items": by_items, "batches_by_pages": by_pages,
            "basis": "**投影**：S3 ≈1.0–2.0 项/页、S4 ≈1.25 项/页（来自既有实测文档）；配额 `calibrated=%s`"
                     % TUNE["judge_calibrated"]}


def privacy_line(slots: dict) -> dict:
    up = [f"{k}={v['engine']}:{v['mode']}" for k, v in slots.items()
          if v and not v.get("null") and v.get("mode") == "cloud"]
    return {"uploads_pages": bool(up), "cloud_slots": up,
            "note": ("**会把整页图上传云端**（%s）——个人使用允许；**对外发布必须声明**"
                     % "、".join(up)) if up else "全部本地：**不上传**"}


def nonascii_paths(*ps: str) -> list:
    """路径里含非 ASCII 字符的那些（A4：本地 Paddle 在中文路径下可能打不开模型）。"""
    return [p for p in ps if p and any(ord(c) > 127 for c in p)]


def layout_missing(cfg: dict) -> bool:
    """书有没有声明版式（2026-10-26：**所有书都要声明**；单栏也要写）。"""
    return not bool((cfg or {}).get("layout"))


def engine_rate(path_dir: str) -> dict:
    """从 `<引擎产出>/engine-record.json` 量**实测速率**（秒/页）。

    没有记录 / 没有成功页 / 没有耗时 → 返回 `{}`（**不猜**：宁可不投影，也不编一个速率）。
    """
    if not path_dir:
        return {}
    p = os.path.join(path_dir, "engine-record.json")
    if not os.path.exists(p):
        return {}
    try:
        rec = json.load(open(p, encoding="utf-8"))
    except Exception:                                                       # noqa: BLE001
        return {}
    if not isinstance(rec, dict):
        return {}
    pages_map = rec.get("pages")
    if not isinstance(pages_map, dict):
        pages_map = {}
    done = rec.get("pages_done")
    if not isinstance(done, list):
        done = [k for k, v in pages_map.items() if isinstance(v, dict) and v.get("ok")]
    try:
        n = len(done)
        el = float(rec.get("elapsed_sec"))
    except (TypeError, ValueError):
        return {}
    if n <= 0 or el <= 0:
        return {}
    return {"sec_per_page": round(el / n, 1), "pages_measured": n, "source": p}


def project_time(pages: int, rate: dict) -> dict:
    """页数 × 实测速率 → 分钟/小时（**投影**，标注来自哪份 engine-record）。"""
    if not rate or not pages:
        return {}
    minutes = pages * rate["sec_per_page"] / 60.0
    return {"minutes": round(minutes, 1), "hours": round(minutes / 60.0, 2), "rate": rate}


def gather(book: str = "", engine: str = "mineru") -> dict:
    out = {"book": book, "blockers": [], "warnings": [], "sections": {}}
    # B2：**工作根体检**（工具根 / 数据根 + 来源；坏根直接算阻塞项——产物会写错地方，必须先修）
    out["sections"]["roots"] = paths.explain()
    if out["sections"]["roots"]["error"]:
        out["blockers"].append({"where": "paths",
                                "what": out["sections"]["roots"]["error"],
                                "fix": "把 %s 指向一个存在的目录（或用 --artifact-root/--out-root 显式指定），"
                                       "当前来源：%s" % (paths.ENV_VAR, out["sections"]["roots"]["data_root_source"])})
    # ① 引擎槽位
    d = engines_mod.load()
    slots = {n: engines_mod.resolve_slot(d, n) for n in ("primary", "witness")}
    rd = {n: engines_mod.readiness(s) for n, s in slots.items()}
    out["sections"]["engines"] = {"slots": {n: {"engine": s.get("engine"), "mode": s.get("mode"),
                                                "runner": s.get("runner_rel"), "status": s.get("status"),
                                                "cred_env": s.get("cred_env")} for n, s in slots.items()},
                                  "readiness": rd, "vram": engines_mod.vram_free_mb(),
                                  "disk_free_gb": engines_mod.disk_free_gb()}
    for n, r in rd.items():
        for b in r["blockers"]:
            out["blockers"].append({"where": "engines.%s" % n, **b})
    out["sections"]["privacy"] = privacy_line(slots)
    # ② 书
    if book:
        try:
            b = books_mod.load()
        except Exception as exc:                                                 # noqa: BLE001
            out["blockers"].append({"where": "books", "what": "书注册表读不了：%s" % exc, "fix": "修 `_mcp-test/books.json`"})
            b = {}
        if book not in b:
            out["book"] = None
            out["blockers"].append({"where": "books", "what": "注册表里没有这本书：%s" % book,
                                    "fix": "用 `intake.py add --pdf <源书> --ranges <页段> --key %s --label <名>` 登记" % book})
        else:
            c = b[book]
            pdf_ok = os.path.exists(c["pdf"])
            prof_ok = os.path.exists(c["profile"])
            pages = render.page_count(render.open_doc(c["pdf"])) if pdf_ok else None
            out["book"] = {"key": c["key"], "label": c["label"], "pdf": c["pdf"], "pdf_exists": pdf_ok,
                           "profile": c["profile"], "profile_exists": prof_ok, "pages": pages,
                           "page_labels": (c.get("page_labels") or [])[:6],
                           "mineru_dir": c["mineru_dir"], "paddle_dir": c["paddle_dir"],
                           "engine_data": {k: os.path.isdir(c[k]) for k in ("mineru_dir", "paddle_dir")}}
            if not pdf_ok:
                out["blockers"].append({"where": "books", "what": "切片 PDF 不存在：%s" % c["pdf"],
                                        "fix": "重跑 `intake.py add … --force`"})
            if not prof_ok:
                out["warnings"].append({"where": "books", "what": "S0 画像还没出（S6 需要它的家具带）",
                                        "fix": "`s0_classify.py profile --pdf \"%s\" --out \"%s\"`" % (c["pdf"], c["profile"])})
            if layout_missing(c):
                out["warnings"].append({
                    "where": "books",
                    "what": "版式未声明（`books.json.layout` 为空）——按几何排序；主+边栏会被判成单栏、左右交错",
                    "fix": "`intake.py layout --book %s --layout \"单栏\"`（**所有书都要声明**，单栏也写；"
                           "有 IR 后先跑 `layout_declare.py suggest --book %s` 看候选再确认）" % (book, book)})
            for k in ("mineru_dir", "paddle_dir"):
                if not os.path.isdir(c[k]):
                    out["warnings"].append({"where": "books", "what": "引擎产出目录还不存在：%s" % c[k],
                                            "fix": "跑对应引擎（A3）；本目录缺失时 S2 无法装配 IR"})
            if pages:
                out["sections"]["scale"] = project(pages)
                # A6：引擎耗时投影——用**实测** engine-record.json 的速率（没有就明说不投影）
                et = {}
                for _slot, _k in (("primary", "mineru_dir"), ("witness", "paddle_dir")):
                    _r8 = engine_rate(c.get(_k))
                    if _r8:
                        et[_slot] = project_time(pages, _r8)
                out["sections"]["engine_time"] = et
                for _slot, _t in et.items():
                    if _t.get("minutes", 0) > 120:
                        out["warnings"].append({
                            "where": "engines.%s" % _slot,
                            "what": "引擎耗时预计 **%.1f 小时**（%s：%.1f 秒/页实测 × %d 页）"
                                    % (_t["hours"], _slot, _t["rate"]["sec_per_page"], pages),
                            "fix": "过夜级任务：可先跑页段抽样，或把该槽位换成云端"})
    # ③ 阈值
    out["sections"]["tuning"] = {"params": len(TUNE), "unverified": len(unverified()),
                                 "unverified_names": unverified()[:6]}
    out["warnings"].append({"where": "tuning", "what": "%d 个参数仍是 `default-unverified`" % len(unverified()),
                            "fix": "见 `tuning.json` 的 basis/note；只观测、不阻断"})
    # ④ 路径编码（A4）：本地 Paddle 在非 ASCII 路径下可能打不开模型
    _bad = nonascii_paths(paths.data_root(), paths.local_root())
    if _bad:
        out["warnings"].append({
            "where": "paths",
            "what": "路径含非 ASCII：%s（本地 Paddle 可能报 `Cannot open file …inference.json`）" % _bad[0],
            "fix": "换纯英文工作区；或 `subst X:` 映射后把 `CHEMDOC_LOCAL` 指到 `X:\\.local`"})
    return out


def render_report(g: dict) -> str:
    L = []
    e = g["sections"]["engines"]
    _r = g["sections"].get("roots") or {}
    L.append("【工作根】工具（代码/环境）=%s" % _r.get("tools_root", "?"))
    L.append("          数据（输入/产物）=%s  来源：%s"
             % (_r.get("data_root") or "**无效**", _r.get("data_root_source", "?")))
    L.append("【引擎槽位】")
    for n, s in e["slots"].items():
        L.append("  %-8s %-22s runner=%-46s cred=%s" % (n, ("%s:%s" % (s["engine"], s["mode"])) if s["engine"] else "null（合法降级）",
                                                        s["runner"] or "—", s["cred_env"] or "（本地不凭证）"))
    for n, r in e["readiness"].items():
        L.append("  %-8s 就绪度：%s" % (n, "就绪 ✓" if r["ready"] else "**不可用**"))
        for b in r["blockers"]:
            L.append("        **阻塞** %s → %s" % (b["what"], b["fix"]))
        ck = r["checks"].get("credential") or {}
        if ck.get("env"):
            L.append("        凭证 %s：set=%s 来源=%s %s" % (ck["env"], ck.get("set"), ck.get("source"), ck.get("mask")))
    L.append("  本机：显存 %s MiB 总量 / %s MiB 空闲 · 磁盘余量 %s GB"
             % ((e["vram"] or {}).get("total"), (e["vram"] or {}).get("free"), e["disk_free_gb"]))
    L.append("【隐私】%s" % g["sections"]["privacy"]["note"])
    if g.get("book"):
        b = g["book"]
        L.append("【书】%s（key=%s）" % (b["label"], b["key"]))
        L.append("  切片 %s（%s 页；标签前 6 项 %s）" % (os.path.relpath(b["pdf"], ROOT), b["pages"], b["page_labels"]))
        L.append("  画像 %s · 引擎产出 %s" % ("已就位 ✓" if b["profile_exists"] else "**未出**",
                                             json.dumps(b["engine_data"], ensure_ascii=False)))
        sc = g["sections"].get("scale")
        if sc:
            L.append("【规模（**投影**）】预计 S3 出题 %s 项、S4 探针 %s 项 → **约 %d 个判定批**"
                     "（按 ≤%d 项/批 或 ≤%d 页/批，%s）"
                     % (sc["est_s3_items"], sc["est_s4_items"], sc["est_batches"],
                        sc["items_per_batch"], sc["pages_per_batch"], sc["basis"]))
        et = g["sections"].get("engine_time") or {}
        if et:
            _parts = []
            for _slot, _t in et.items():
                _parts.append("%s ≈ **%.0f 分钟**（%.1f 秒/页 × %d 页，实测 %d 页样本）"
                              % (_slot, _t["minutes"], _t["rate"]["sec_per_page"], b["pages"],
                                 _t["rate"]["pages_measured"]))
            L.append("【引擎耗时（**投影**）】" + "；".join(_parts))
        else:
            L.append("【引擎耗时】**无实测速率**（没有 engine-record.json）→ 不投影；"
                     "先跑 1–2 页取样即可得到速率")
    t = g["sections"]["tuning"]
    L.append("【阈值】共 %d 个参数；仍 `default-unverified` %d 个（%s）"
             % (t["params"], t["unverified"], "、".join(t["unverified_names"])))
    for w in g["warnings"]:
        L.append("  警告 [%s] %s → %s" % (w["where"], w["what"], w.get("fix", "")))
    L.append("【结论】%s" % ("**可以开始**（无阻塞项）" if not g["blockers"] else
                          "**当前不可跑**：%d 个阻塞项（见上）" % len(g["blockers"])))
    L.append("（本命令只判「这本书能不能跑」；「我的环境与文档是否一致」看 `doctor.py check`）")
    return "\n".join(L)


def cmd_run(a) -> int:
    g = gather(book=a.book or "", engine=a.engine)
    print(render_report(g))
    if a.json:
        print(json.dumps(g, ensure_ascii=False, indent=2))
    return 0 if not g["blockers"] else 1


def selftest() -> int:
    cases = []
    # 投影（纯函数）
    p = project(50)
    cases.append(("投影：50 页 → 批数 = max(按项, 按页) 且两者都给",
                  p["est_batches"] == max(p["batches_by_items"], p["batches_by_pages"]) and p["pages"] == 50, p))
    p2 = project(400)
    cases.append(("投影：400 页的批数明显大于 50 页（单调）", p2["est_batches"] > p["est_batches"], p2["est_batches"]))
    cases.append(("投影：明确标注是投影（带 basis 与 calibrated 状态）",
                  "投影" in p["basis"] and "calibrated" in p["basis"], p["basis"]))
    # A6：引擎耗时只能来自**实测记录**；没有记录就不投影（不猜）
    import tempfile as _tf
    import shutil as _sh
    _td = _tf.mkdtemp(prefix="rate-")
    try:
        with open(os.path.join(_td, "engine-record.json"), "w", encoding="utf-8") as _fh:
            json.dump({"pages_done": [1, 2, 3], "elapsed_sec": 240.0}, _fh)
        _r = engine_rate(_td)
        cases.append(("A6：速率 = elapsed_sec / 成功页数（240 秒 / 3 页 = 80 秒/页）",
                      _r.get("sec_per_page") == 80.0, _r))
        cases.append(("A6：500 页 × 80 秒/页 ≈ 11.1 小时（过夜级，触发 warning 阈值 120 分钟）",
                      project_time(500, _r).get("hours") == 11.11
                      and project_time(500, _r).get("minutes", 0) > 120, project_time(500, _r)))
        cases.append(("A6：没有 engine-record → **不投影**（返回空，不编一个速率）",
                      engine_rate(os.path.join(_td, "nope")) == {}, None))
        cases.append(("A6：记录损坏 / 页数 0 → 不投影且不崩",
                      engine_rate(_td) != {} and engine_rate("") == {}, None))
    finally:
        _sh.rmtree(_td, ignore_errors=True)
    # A4：非 ASCII 路径提示（不阻断）
    cases.append(("A4：非 ASCII 路径能被指出；纯 ASCII 不误报",
                  nonascii_paths("D:/a/b") == [] and nonascii_paths("D:/中文/x") != [], None))
    # 2026-10-26：所有书都要声明版式（缺声明 → warning，不阻塞）
    cases.append(("版式：缺 `layout` → 判为「未声明」；有声明 → 不告警",
                  layout_missing({}) and layout_missing({"layout": None})
                  and not layout_missing({"layout": [{"pages": [[1, 9]], "kind": "单栏", "regions": 1, "cuts": []}]}),
                  None))
    # 隐私行
    cloud = {"primary": {"engine": "mineru", "mode": "cloud"}, "witness": None}
    local = {"primary": {"engine": "mineru", "mode": "local"}, "witness": {"engine": "paddle", "mode": "local"}}
    cases.append(("隐私：云端槽位 → 明确说**会上传整页图**",
                  privacy_line(cloud)["uploads_pages"] and "上传" in privacy_line(cloud)["note"], None))
    cases.append(("隐私：全本地 → 明确说**不上传**",
                  (not privacy_line(local)["uploads_pages"]) and "不上传" in privacy_line(local)["note"], None))
    # 真实聚合（当前工作区 + 已登记的书）。**书 key 从注册表挑**（不写死某本书；挑不到就跳过）
    _all = books_mod.load()
    _pick = next((c["key"] for c in _all.values() if c.get("state", "active") == "active"), "")
    # 即使 `_pick` 为空（新机器、注册表里还没有书）也要走真 `gather()`：
    # **不许手搓 stub** —— 那会少掉 `sections` 里的键，`render_report()` 当场 KeyError（实测崩过一次）。
    g = gather(book=_pick)
    # 注意：**显存是共享资源**——本地 Paddle 正在跑时（GPU 被占），"见证引擎就绪"必然报阻塞。
    # 所以这里断言的**不是"没有阻塞"**，而是"引擎段的每条阻塞都必须是**可操作的真阻塞**"
    # （缺凭证 / 实测显存不够 / runner 契约不符），且都给了补法；接线错误（找不到 runner 等）仍然会红。
    _eng_blk = [b for b in g["blockers"] if b["where"].startswith("engines")]
    _known = ("凭证", "显存", "runner", "模型目录", "解释器")
    cases.append(("聚合：引擎段的阻塞**只可能是可操作的真阻塞**（缺凭证/显存不够/runner 契约），且都带补法",
                  all(any(k in b["what"] for k in _known) and b.get("fix") for b in _eng_blk), _eng_blk))
    _has_data = paths.has_data()
    # **不要写死 50**：页数是《濒危之城》在 `books.json` 里登记的事实（`page_labels` 长度）。
    # 断言应该是"预检报的页数 == 注册表登记的页数"（自洽、与机器无关），而不是"== 某次流程的常量"。
    _cfg = next((c for c in _all.values() if c["key"] == _pick), {})
    _exp_pages = len(_cfg.get("page_labels") or []) or None
    if not _pick:
        # **一本都没登记**（新机器）：这条与数据/注册表都有关 → **明示跳过**，不判红
        cases.append(("聚合：**注册表里没有任何书**（新机器正常状态）→ 书段断言明示跳过",
                      True, {"books": len(_all)}))
    else:
        cases.append((("聚合：书段给出页数与引擎产出状态" if _has_data else
                       "聚合：**这份拷贝里没有数据树** → 书段只要求「登记项在 + 缺切片如实报阻塞」"
                       "（页数断言明示跳过）"),
                      (g["book"] and (_exp_pages is None or g["book"]["pages"] == _exp_pages)
                       and "engine_data" in g["book"]) if _has_data
                      else (bool(g["book"]) and any(b["where"] == "books" for b in g["blockers"])),
                      g["book"]))
    cases.append(("聚合：**输出里不出现任何 20+ 位的密钥样式串**（只应有掩码）",
                  not __import__("re").search(r"[A-Za-z0-9_\-]{28,}", render_report(g)), None))
    # 负例：不存在的书 → 阻塞且给补法
    g2 = gather(book="no-such-book")
    cases.append(("负例：书未登记 → 阻塞 + 给 intake 补法",
                  any(b["where"] == "books" and "intake.py" in b.get("fix", "") for b in g2["blockers"]),
                  [b["what"] for b in g2["blockers"]]))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:130]))
        passed += bool(ok)
    print("\n%d/%d preflight tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="")
    ap.add_argument("--engine", default="mineru", choices=sorted(volumes_mod.LIMITS))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    return selftest() if a.selftest else cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
