# -*- coding: utf-8 -*-
"""A6 · **交付说明**（交付根自解释）—— SPEC §3.12 / 软门禁 G22 / 不变量 I5。

**它解决什么问题**：交付根原先只有 `report.json`（机器台账）与 `validation-report.json`（门禁全表），
**没有给自然人的入口** —— 「这本书能不能用、先看哪个文件、哪里没底」只能靠工作区文档代偿。

    s7-out/<书>/
      交付说明.md            ← 本模块产出：判决转述 + 按角色的阅读指引 + 目录清单 + 数字速览 + 已知边界

**谁产出**：本模块的 `build`/`write` 由 **`validate_run.py`（独立校验器）** 调用（**不是** S7/S8）——
说明必须**转述判决**，而判决只由校验器给出（SPEC §5.1「产出者不得自判」）。

**三条硬口径**（SPEC §3.12）：
  ① **每次校验整体重写**、**不含时间戳**（时间戳看 `validation-report.json.checked_at`）→ 同一棵树两次校验
     产出的说明**逐字节相同**，于是 `publish_run` 的 `payload_hashes_match` 天然**覆盖**它（不会因两个
     时间戳把发布自证打成假红）；② **数字一律取自 `report.json` / `validation-report.json`**，缺字段写
     「未记录」，**不推算不编造**；③ **目录清单按磁盘实际扫描**（盘上有而登记表里没有的也如实列出）。

**两层保护**：
  - **不变量 I5**（`check`，写完当场复核）：机器块数字 == `report.json`、清单文件都在盘上、骨架标题齐、
    盘上文本 == 当场重建的文本 → 违例由校验器把判决降为「无法判定」。
  - **软门禁 G22**（`seal_check`）：**封条** —— 上一轮 `validation-report.json.delivery_note.sha256`
    必须等于盘上说明的 sha256 → 否则「说明自上次校验后被改过」；无封条/无 `validation-report.json` → unverified。

用法:
  python -X utf8 delivery_note.py build --book <key> [--artifact-root ...] [--s7-dir ...]
  python -X utf8 delivery_note.py check --book <key> [--artifact-root ...] [--s7-dir ...]
  python -X utf8 delivery_note.py selftest
退出码：build 0 成功 / check 0 = 一致，1 = 有不一致
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = paths.data_root()  # 可迁移：_mcp-test 的上一级 = 产物根

NOTE_NAME = "交付说明.md"
VR_NAME = "validation-report.json"
BLOCK_RE = re.compile(r"<!--\s*DELIVERY-NOTE\s+(\{.*?\})\s*-->")
NEED_HUMAN_RE = re.compile(r"其中需人工\*\*：\s*(\d+)")
ROUTED_RE = re.compile(r"其余\s*(\d+)\s*条")

# 骨架标题（**生成与复核共用同一份字面量**：写漏一节，`check` 立刻报 `note_section_missing`）
SECTIONS = [
    "## 1. 判决",
    "## 2. 先看什么（按你要做的事）",
    "## 3. 两版正文的区别",
    "## 4. 这个目录里有什么",
    "## 5. 数字速览",
    "## 6. 我们改过什么",
    "## 7. 已知边界（诚实交代）",
    "## 8. 出处与分发",
]

# 机器可读块的键（SPEC §3.12 第 5 条：**键固定**）
BLOCK_KEYS = ("schema", "book", "key", "verdict", "exit_code", "gates", "hard_gates_failed",
              "waived", "blocking", "pages", "chapters", "figures", "tables", "equations",
              "human_facing_markers", "need_human", "judged", "files")

# 目录清单的「用途」登记表（**盘上有而这里没有的照样列出**，只是用途写「未登记」）
PURPOSE_EXACT = {
    NOTE_NAME: "本文件：交付根入口（**自动生成，勿手改**）",
    "report.json": "全过程台账（机器）：逐页状态、全部发现与处置、证据哈希、未决项",
    VR_NAME: "门禁判决（机器，**权威**）：全表 + 每条的证据 + 例外 + 退出码",
    "errata.md": "勘误与「非本书内容」：§1 原书印错（**正文不改**）/ §2 非书内容（**已移出净版**）",
    "index/": "检索 sidecar（每块一条，供下游检索层用）",
    "figures/": "裁出的插图（两版正文的图片链接都指向它）",
    "review-kit/": "校对包（**给人看**）：校对表 + 页面图 + 使用说明",
    "_superseded/": "历次校对包旧快照（留底用，正常不用打开）",
}

SEO = {"pass": "通过", "verdict_pass": "可交付", "warn": "提醒", "fail": "失败",
       "unverified": "未验证", "n/a": "不适用"}


def resolve_key(book: str) -> str:
    """`books.json` 的字典键 → 目录键（不是每本书都同名；沿用 publish_run 的口径）。"""
    try:
        import books as _b
        cfg = (_b.load() or {}).get(book)
        if cfg and cfg.get("key"):
            return cfg["key"]
    except Exception:                                              # noqa: BLE001
        pass
    return book


def _book_meta(key: str) -> dict:
    """书注册项里的展示信息（label / 源文件名 / 分发提示）。读不到就空 —— **不许编造**。"""
    try:
        import books as _b
        for _k, cfg in (_b.load() or {}).items():
            if cfg.get("key") == key:
                return {"label": cfg.get("label") or "", "pdf": os.path.basename(cfg.get("pdf") or ""),
                        "notice": cfg.get("notice") or "", "source_ranges": cfg.get("source_ranges") or ""}
    except Exception:                                              # noqa: BLE001
        pass
    return {}


def _dir(root: str, key: str, s7_dir: str = "") -> str:
    return s7_dir or os.path.join(root, "s7-out", key)


def _load_json(p: str) -> dict:
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                              # noqa: BLE001
        return {}


def _sha256_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _purpose(name: str) -> str:
    if name in PURPOSE_EXACT:
        return PURPOSE_EXACT[name]
    if name.startswith("chapter-") and name.endswith("-clean.md"):
        return "**干净版正文**（可以直接用；只去 HTML 注释，正文逐字未改）"
    if name.startswith("chapter-") and name.endswith("-reviewed.md"):
        return "**留底版正文**（真身；带页标记 / REVIEW 标记 / 审计痕迹）"
    return "（未登记用途 —— 请对照 `report.json` 或问维护者）"


def inventory(d: str) -> list[dict]:
    """交付根条目清单（**按磁盘实际扫描**；目录带 `/` 后缀）。

    **本文件自己一律算在内**（哪怕此刻还没写盘）—— 否则"生成时没有、写盘后有了"会让
    清单自我矛盾（实测踩到：`note_inventory_incomplete` + `note_not_as_generated` 同时炸）。
    """
    if not os.path.isdir(d):
        return []
    names = sorted(os.listdir(d))
    if NOTE_NAME not in names:
        names = sorted(names + [NOTE_NAME])
    return [{"name": name + ("/" if os.path.isdir(os.path.join(d, name)) else ""),
             "dir": os.path.isdir(os.path.join(d, name))} for name in names]


def _todo_lines(p: str) -> dict:
    """从 `review-kit/校对表.md` 取两个**机器可读**行（G19 用的也是同一份口径）。

    `需人工` = 真正要自然人看的条数；`其余` = 已由准入门分流、有去处的条数。
    （两者缺失就给 None → 渲染成「未记录」，**不推算**。）
    """
    try:
        txt = open(p, encoding="utf-8").read()
    except Exception:                                              # noqa: BLE001
        return {"need_human": None, "routed": None}
    m1, m2 = NEED_HUMAN_RE.search(txt), ROUTED_RE.search(txt)
    return {"need_human": int(m1.group(1)) if m1 else None,
            "routed": int(m2.group(1)) if m2 else None}


def _table_rows(block: str) -> int:
    """数一个 Markdown 表里的**真实数据行**（跳过表头/分隔行/占位行）。"""
    n, prev_data = 0, False
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            prev_data = False
            continue
        if re.match(r"^\|[\s:\-|]+\|?$", s):                        # 分隔行 → 前一行是表头
            n -= 1 if prev_data else 0
            prev_data = False
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and cells[0] not in ("", "—", "-", "–"):
            n += 1
            prev_data = True
        else:
            prev_data = False
    return max(0, n)


def _errata_rows(p: str) -> int | None:
    """`errata.md` 的**真实**数据行数（两类合计）；文件不在 → None。

    两个实测坑（都会把对外数字报错，必须挡住）：
      ① 空表里的**占位行** `| — | — | — | — | 本样本未发现 U 类条目 | — |` 不是数据（真书实测被报成 1 条）；
      ② **表头**不能算数据 —— 而它的措辞可能变（`页码` / `页`），所以按 Markdown 结构判：**分隔行
         `|---|---|` 前面那一行是表头**。
    2026-10-26 起 `errata.md` 有**两张表**（source_typo / non_book_ink），本函数数合计。
    """
    if not os.path.exists(p):
        return None
    return _table_rows(open(p, encoding="utf-8").read())


def _errata_non_book(p: str) -> int | None:
    """`errata.md` 里 **`non_book_ink`（非本书内容）** 的条数；文件不在 → None。

    按 `## 2.` 小节切分（S7 的写法固定：§1 = 原书印错，§2 = 非本书内容）。
    老产物没有该小节 → 0（不误报）。
    """
    if not os.path.exists(p):
        return None
    t = open(p, encoding="utf-8").read()
    if "## 2." not in t:
        return 0
    seg = t.split("## 2.")[1]
    # 2026-11-15：§3（missing_text）不能被算进 §2 —— 否则"非本书内容"的条数会虚高
    if "## 3." in seg:
        seg = seg.split("## 3.")[0]
    return _table_rows(seg)


def _errata_missing(p: str) -> int | None:
    """`errata.md` 里 **`missing_text`（主引擎漏、页面有）** 的条数；文件不在 → None。

    按 `## 3.` 小节切分（S7 固定写法）；老产物没有 §3 → 0（不误报）。
    """
    if not os.path.exists(p):
        return None
    t = open(p, encoding="utf-8").read()
    return _table_rows(t.split("## 3.")[1]) if "## 3." in t else 0


def _missing_inserted(d: str) -> int | None:
    """交付目录里**已插回**的 `missing_text` 条数（按正文的 `MISSING-TEXT-INSERT` 痕迹数）。

    2026-11-15：读者确认后跑 `errata_apply.py` 插回，交付说明必须跟着改口
    （不能一边说"正文里暂时没有"、一边正文里已经有了）。
    """
    n, seen = 0, False
    try:
        for f in os.listdir(d):
            if not f.endswith("-reviewed.md"):
                continue
            seen = True
            t = open(os.path.join(d, f), encoding="utf-8").read()
            n += len(re.findall(r"<!--\s*MISSING-TEXT-INSERT\s+id=", t))
    except OSError:
        return None
    return n if seen else None


def numbers(root: str, key: str, s7_dir: str = "", vreport: dict | None = None,
            s7report: dict | None = None) -> dict:
    """全部对外数字的**唯一来源**（生成与复核都走这里；缺就 None → 渲染成「未记录」）。"""
    v = vreport if vreport is not None else _load_json(os.path.join(_dir(root, key, s7_dir), VR_NAME))
    s7 = s7report if s7report is not None else _load_json(os.path.join(_dir(root, key, s7_dir), "report.json"))
    d = _dir(root, key, s7_dir)
    c = s7.get("counts") or {}
    cs = s7.get("chapter_split") or {}
    un = s7.get("unresolved") or {}
    q = _todo_lines(os.path.join(d, "review-kit", "校对表.md"))
    chapters = s7.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        chapters = [f for f in (os.listdir(d) if os.path.isdir(d) else [])
                    if f.startswith("chapter-") and f.endswith(".md") and not f.endswith("-clean.md")]
    pages = cs.get("input_pages")
    return {
        "pages": (len(pages) if isinstance(pages, list) and pages else None),
        "chapters": (len(chapters) if chapters else None),
        "figures": c.get("figures"), "tables": c.get("tables"), "equations": c.get("equations"),
        "human_facing_markers": c.get("human_facing_markers"),
        "need_human": q["need_human"], "routed": q["routed"],
        "judged": un.get("s5_verdicts"), "vision_insertions": c.get("vision_insertions"),
        # 2026-10-26（2.2）：被跨页切分消费掉的转写也要算进"我们改过什么"，否则说明低估改动量
        "vision_consumed_by_split": len(s7.get("split_consumed_transcriptions") or []),
        "cross_page_splits": c.get("cross_page_splits"),
        "cross_page_split_sources": c.get("cross_page_split_sources"),
        "errata_rows": _errata_rows(os.path.join(d, "errata.md")),
        "errata_non_book_ink": _errata_non_book(os.path.join(d, "errata.md")),
        # 2026-11-15（P0-1/P2-2）：主引擎漏、页面上确实有的内容（正文暂缺，读者确认后可插回）
        "errata_missing_text": _errata_missing(os.path.join(d, "errata.md")),
        "missing_inserted": _missing_inserted(d),
        "pending": len(((s7.get("judgment") or {}).get("pending")) or []),
        "escalation_rate": (s7.get("escalations") or {}).get("rate_per_100_pages"),
        "unverified_tuning": (s7.get("tuning") or {}).get("unverified_count"),
        "verdict": v.get("verdict"), "exit_code": v.get("exit_code"),
        "gates": v.get("counts") or None,
        "hard_gates_failed": v.get("hard_gates_failed"),
        "waived": len(v.get("exceptions_waived") or []),
        "blocking": len(v.get("blocking") or []),
        "gate_rows": v.get("gates") or {},
        "vision_insertions_detail": s7.get("vision_insertions") or [],
    }


def _ranges_human(sr) -> str:
    """`[[1,50]]` → 「1–50」（给人看的页码区间；**不许**把原始 JSON 直接甩给人）。"""
    if not isinstance(sr, list) or not sr:
        return ""
    out = []
    for x in sr:
        if isinstance(x, list) and len(x) == 2:
            out.append(f"{x[0]}–{x[1]}" if x[0] != x[1] else str(x[0]))
    return "、".join(out)


def _notice_human(s: str) -> str:
    return (s or "").rstrip("。.；; ") + "。" if s else ""


def _s(v, dash: str = "未记录") -> str:
    return dash if v is None else str(v)


def _measured(m: dict, limit: int = 4) -> str:
    """把 `measured` 压成一行人话（只取标量与"一层字典里的标量"，深结构跳过）。"""
    out = []
    for k, v in (m or {}).items():
        if isinstance(v, bool) or isinstance(v, (int, float, str)):
            out.append(f"{k}={v}")
        elif isinstance(v, dict):
            inner = [f"{a}={b}" for a, b in v.items() if isinstance(b, (int, float, str)) and not isinstance(b, bool)]
            if inner:
                out.append(f"{k}: " + "、".join(inner))
        if len(out) >= limit:
            break
    return "；".join(out) if out else "—"


def _disclaimer(n: dict) -> str:
    """「可交付」的**限定语**：判的是**形态**，不是内容正确性（2026-10-25 用户要求）。

    只在判决是「可交付」时出现——别的判决本来就没在说"可交付"。
    """
    if not str((n or {}).get("verdict") or "").startswith("可交付"):
        return ""
    return ("> **「可交付」说的是「形态」**：本判决由 **22 道形态门禁**产生，"
            "**不构成对内容正确性、科学事实或完整性的保证**；"
            "内容核对见 `review-kit/校对表.md`（**需人工 %s 条**）。" % _s(n.get("need_human")))


DISCLAIMER_FOOTER = "门禁查的是**可验证的形态**；「门禁通过」不等于「内容已核」。"


def build(book: str, root: str = ROOT, s7_dir: str = "", vreport: dict | None = None,
          s7report: dict | None = None) -> str:
    """生成交付说明全文（**纯函数**：同输入 → 同输出；不含时间戳）。"""
    key = resolve_key(book)
    d = _dir(root, key, s7_dir)
    n = numbers(root, key, s7_dir, vreport, s7report)
    meta = _book_meta(key)
    inv = inventory(d)
    g = n["gates"] or {}
    gate_txt = "／".join(f"{g.get(k, 0)} {SEO[k]}" for k in ("pass", "warn", "fail", "unverified", "n/a") if k in g)
    hard = n["hard_gates_failed"]
    L: list[str] = []
    A = L.append

    A(f"# 交付说明 — {meta.get('label') or key}")
    A("")
    A("> **本文件由独立校验器（`validate_run.py`）自动生成，每次校验整体重写——请不要手改。**")
    A("> 改了下一次校验会如实报出来（软门禁 `G22`），并把它覆盖回去；要复核一份交付物有没有被动过，跑：")
    A("> `python _mcp-test\\delivery_note.py check --book %s`。" % key)
    A("> 校验时间与完整证据见同目录的 `%s`（**判决的权威来源**）。" % VR_NAME)
    A("")
    A("## 1. 判决")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 判决 | **{_s(n['verdict'], '尚未校验')}** |")
    A(f"| 硬门禁失败 | {'无' if not hard else '、'.join(hard)} |")
    A(f"| 门禁分布 | {gate_txt or '未记录'} |")
    A(f"| 阻塞例外（未豁免） | {n['blocking']} |")
    A(f"| 已豁免例外 | {n['waived']} |")
    A(f"| 未判定项 | {n['pending']} |")
    A("")
    A(f"**这是什么**：把 PDF 转成**可追溯 Markdown** 的产物 —— 两版正文 + 插图 + 勘误 + 检索索引 + 校对包 + 全过程台账，"
      f"由**独立校验器**逐项体检（门禁全表见 `{VR_NAME}`）。")
    _d = _disclaimer(n)
    if _d:
        A("")
        A(_d)
    A("")
    A("## 2. 先看什么（按你要做的事）")
    A("")
    A("| 你想做的事 | 打开这个 |")
    A("|---|---|")
    A("| 我只要**能直接用的正文** | `chapter-*-clean.md`（干净版） |")
    A(f"| 我要**核对有没有漏字/错字** | `review-kit/校对表.md`（**其中需人工 {_s(n['need_human'])} 条**）+ `review-kit/pages/`（页面图） |")
    A(f"| 我要看机器**凭什么**这么判 | `{VR_NAME}`（门禁全表 + 每条的证据） |")
    A("| 我要看**每一页发生了什么** | `report.json`（逐页状态、全部发现与处置） |")
    A("| 我要拿这段文字去**检索** | `index/entities.jsonl` |")
    A("| 我要**引用插图** | `figures/` |")
    A("| 我要看**原书是不是印错了** / **页上哪些不是书的内容** | `errata.md`（**§1 原书印错** → 正文不改；**§2 非书内容** → 已移出净版） |")
    A("")
    A("## 3. 两版正文的区别")
    A("")
    A("- `chapter-*-reviewed.md`（**留底 / 真身**）：带审计痕迹 —— 页标记、`<!-- REVIEW -->` 标记、")
    A("  视觉插入痕迹、跨页切分说明、已判定（备查）折叠块。**复核与追责看它。**")
    A("- `chapter-*-clean.md`（**直接用**）：只去掉 HTML 注释，**正文逐字未改**（由硬门禁 `G9` 保证：")
    A("  干净版 == 留底版去注释）。")
    A("")
    A("> 两者是**同一份内容**的两个视图。**不要手工改 clean 版**——要改请改留底版并留痕。")
    A("")
    A("## 4. 这个目录里有什么")
    A("")
    A("| 条目 | 用途 |")
    A("|---|---|")
    for it in inv:
        A(f"| `{it['name']}` | {_purpose(it['name'])} |")
    if not inv:
        A("| — | （目录不存在或为空） |")
    A("")
    A("## 5. 数字速览")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A(f"| 源书页数 | {_s(n['pages'])} |")
    A(f"| 章 | {_s(n['chapters'])} |")
    A(f"| 图 / 表 / 公式 | {_s(n['figures'])} / {_s(n['tables'])} / {_s(n['equations'])} |")
    A(f"| 给人看的标记 | {_s(n['human_facing_markers'])}（其中**需人工 {_s(n['need_human'])}**"
      + (f"，其余 {n['routed']} 条已由准入门分流、有去处" if n["routed"] is not None else "") + "） |")
    A(f"| 判定 | 已判 {_s(n['judged'])} 条，未判 {n['pending']} 条 |")
    A(f"| 视觉转写（我们看图补的字） | 插入正文 **{_s(n['vision_insertions'])}** 处"
      f" + 被跨页切分消费 **{_s(n['vision_consumed_by_split'])}** 处 |")
    A(f"| 跨页切分 / 接句 | {_s(n['cross_page_splits'])} / {_s(n['cross_page_split_sources'])} |")
    A(f"| 勘误登记（原书如此） | {_s(n['errata_rows'])} 条 |")
    A("")
    A("## 6. 我们改过什么")
    A("")
    vi = n["vision_insertions_detail"]
    _vsplit = n.get("vision_consumed_by_split") or 0
    _vtot = len(vi) + _vsplit
    if _vtot == 0:
        A("- **正文没有因为「看图」被改过**：视觉转写 **0 处**（插入 0 + 被跨页切分消费 0）——"
          "正文完全来自两个引擎的转录。")
    else:
        A(f"- **正文只在两类情况下被我们改过**：**视觉转写共 {_vtot} 处** ——"
          f"**{len(vi)} 处插入正文**（两个引擎都没读出来、由判定者**看图读出**）+"
          f" **{_vsplit} 处被跨页切分消费**（那段字由跨页续排搬回后一页，不再重复插入）。"
          "每处都留 `VISION-INSERT` 痕迹与判定记录（模型 / 模式 / 置信度）：")
    if vi:
        A("")
        A("  | 页 | 内容（前 40 字） | 判定项 | 模型 | 置信度 |")
        A("  |---|---|---|---|---|")
        for x in vi[:12]:
            t = (x.get("text") or "").replace("\n", " ").replace("|", "／")[:40]
            A(f"  | {_s(x.get('page'))} | {t} | `{x.get('item_id') or '—'}` | {x.get('model') or '—'} | {_s(x.get('confidence'))} |")
    if _vsplit:
        A("")
        A(f"  > 另有 **{_vsplit} 处**转写被**跨页切分**消费（同一句已由续排搬到位，不再重复插入）；"
          "清单见 `report.json` 的 `split_consumed_transcriptions`。")
    A(f"- **结构上**：跨页切分 {_s(n['cross_page_splits'])} 处、接句 {_s(n['cross_page_split_sources'])} 处"
      "（页标记落在正文的页边界上，按行切页也看得见）。")
    A(f"- **原书自身的印刷错误 / 过时表述**：**一个字都没改**，只登记在 `errata.md`（本次 {_s(n['errata_rows'])} 条）。")
    if (n.get("errata_non_book_ink") or 0) > 0:
        A(f"- **非本书内容（透印/手写/印章/装饰）**：{n['errata_non_book_ink']} 处**已从净版移出**，"
          "留底版仍有 `<!-- NON-BOOK-INK … -->` 注释，明细见 `errata.md` §2（**不是**原书印错）。")
    # 2026-11-15（P0-1 / P2-2）：**内容缺口**必须在交付说明里点名，而不是只留在台账
    _mts = n.get("errata_missing_text")
    _mins = n.get("missing_inserted") or 0
    if _mts:
        if _mins >= _mts:
            A(f"- **内容缺口（判定记录）**：**{_mts} 处**，**已全部插回正文**"
              f"（正文里每处都有 `MISSING-TEXT-INSERT` 审计痕迹；`errata.md` §3 保留登记）。")
        elif _mins > 0:
            A(f"- **内容缺口（判定记录）**：**{_mts} 处**，其中 **{_mins} 处已插回正文**、"
              f"**{_mts - _mins} 处仍缺**。逐条见 `errata.md` **§3**；要插回剩下的，跑 "
              "`errata_apply.py apply --book <key>`（按锚点插入，拒绝猜位置）。")
        else:
            A(f"- **内容缺口（判定记录）**：**{_mts} 处**——判定者已看图确认**页面上有这段**，"
              "但主引擎没读出来，**正文里暂时没有**。逐条（含「插在哪」与依据）见 `errata.md` **§3**；"
              "确认无误后可用 `errata_apply.py` 按锚点把它们插回正文（插入处留 `MISSING-TEXT-INSERT` 痕迹）。"
              "**这不是「已解决」，是「已登记、待你决定要不要插回」。**")
    elif _mts == 0:
        A("- **内容缺口（判定记录）**：0 处（本次判定没有发现「页面有、正文缺」的内容）。")
    A("- **科学 / 事实正确性**：本工作流**不判、不改、也不登记**（口径见 SPEC §2.4）。")
    A("")
    A("## 7. 已知边界（诚实交代）")
    A("")
    rows = [(gid, r) for gid, r in sorted((n["gate_rows"] or {}).items())
            if isinstance(r, dict) and r.get("status") not in ("pass", "n/a")]
    if rows:
        A("下面是本次**没有拿到 pass** 的门禁 —— 不是要你去修，而是让你知道**哪里没底**：")
        A("")
        A("| 门禁 | 状态 | 机器读数 | 它管什么 |")
        A("|---|---|---|---|")
        for gid, r in rows:
            why = (r.get("evidence") or [{}])[0].get("why") or r.get("reason") or ""
            A(f"| `{gid}` {r.get('name') or ''} | {SEO.get(r.get('status'), r.get('status'))} | "
              f"{_measured(r.get('measured') or {})} | {(r.get('threshold') or '')[:60]} |")
            if why:
                A(f"| | | {str(why)[:90]} | |")
    else:
        A("本次**所有门禁都是 pass 或 n/a**（没有提醒、没有未验证）。")
    A("")
    A(f"- **升级率（G10 观测，不阻断）**：{_s(n['escalation_rate'])} 条/100 页 —— 绝大多数是"
      "「准入门已拒收且有去处」的登记，**不是**待办。")
    A(f"- **调优参数里 {_s(n['unverified_tuning'])} 项** basis = `default-unverified`（**只观测、不阻断**）。")
    A("- **能力边界（不许诺的东西）**：整本书的误差率不在承诺内、检索层同义词/单位归一不做、"
      "科学正确性不判 —— 详见工作区 `docs/new/LIMITATIONS-产物能力与不支持清单.md`。")
    # 2026-10-26（3.1）：只拿交付根的人也要能看见"凭什么这么判"
    A("- **每条判定凭什么**：`review-kit/校对表.md` 第 3 节「已判定（备查）」里，每条结论后面都跟着"
      "判定者当时看到的一句话（`我认为：…`）；完整证据仍在台账（`s5-out/<书>/judge-worklist.jsonl`）。")
    A("")
    A("## 8. 出处与分发")
    A("")
    A(f"- 源文件：`{meta.get('pdf') or '未记录'}`；书注册项：{meta.get('label') or key}"
      + (f"；页码范围：{_ranges_human(meta.get('source_ranges'))}" if _ranges_human(meta.get("source_ranges")) else "") + "。")
    A(f"- **分发提示**：{_notice_human(meta.get('notice')) or '未登记 —— 对外分发前请先确认原书许可。'}")
    A("- **隐私提醒**：本项目个人使用下默认允许把**整页图**上传云端 OCR（本项目默认口径，SPEC §0.2-①）；"
      "**对外发布时必须在声明里写明**。")
    A("- 对外发布前必读：工作区 `docs/new/来源与许可证核查.md`（渲染栈许可 / 发布集排除 / 对外声明模板）。")
    A("")
    A("---")
    A("")
    A(DISCLAIMER_FOOTER)
    A("")
    A(_block(book, key, n, inv))
    A("")
    return "\n".join(L)


def _block(book: str, key: str, n: dict, inv: list[dict]) -> str:
    """机器可读块（单行 JSON；键固定，见 SPEC §3.12 第 5 条）。"""
    payload = {
        "schema": 1, "book": book, "key": key,
        "verdict": n["verdict"], "exit_code": n["exit_code"],
        "gates": n["gates"], "hard_gates_failed": n["hard_gates_failed"],
        "waived": n["waived"], "blocking": n["blocking"],
        "pages": n["pages"], "chapters": n["chapters"], "figures": n["figures"],
        "tables": n["tables"], "equations": n["equations"],
        "human_facing_markers": n["human_facing_markers"], "need_human": n["need_human"],
        "judged": n["judged"], "files": [it["name"] for it in inv],
    }
    return "<!-- DELIVERY-NOTE " + json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                              separators=(", ", ": ")) + " -->"


def write_text(d: str, text: str) -> dict:
    """落盘（LF、UTF-8；**不含时间戳** → 幂等）。返回 sha256/字节数。"""
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, NOTE_NAME)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return {"path": p, "sha256": _sha256_text(text), "bytes": len(text.encode("utf-8"))}


def write(book: str, root: str = ROOT, s7_dir: str = "", vreport: dict | None = None,
          s7report: dict | None = None) -> dict:
    d = _dir(root, resolve_key(book), s7_dir)
    text = build(book, root, s7_dir, vreport, s7report)
    res = write_text(d, text)
    res["text"] = text
    return res


def check(book: str, root: str = ROOT, s7_dir: str = "", vreport: dict | None = None,
          s7report: dict | None = None, text: str | None = None) -> dict:
    """复核（**不变量 I5** 的判据；也供 CLI `check` 用）。

    `text` 给定时按"将要写盘的文本"复核（校验器写完那一瞬间的用法）；不给则读盘上的文件。
    """
    key = resolve_key(book)
    d = _dir(root, key, s7_dir)
    viol: list[dict] = []
    p = os.path.join(d, NOTE_NAME)
    from_disk = text is None
    if from_disk:
        if not os.path.exists(p):
            return {"ok": False, "violations": [{"kind": "note_missing", "why": f"交付根没有 `{NOTE_NAME}`"}],
                    "sha256": "", "path": p}
        text = open(p, encoding="utf-8").read()
    sha = _sha256_text(text)
    n = numbers(root, key, s7_dir, vreport, s7report)
    m = BLOCK_RE.search(text)
    block: dict = {}
    if not m:
        viol.append({"kind": "machine_block_missing",
                     "why": "说明里没有机器可读块 `<!-- DELIVERY-NOTE {…} -->`（G19/复核都靠它）"})
    else:
        try:
            block = json.loads(m.group(1))
        except Exception as exc:                                   # noqa: BLE001
            viol.append({"kind": "machine_block_unparsable", "why": f"机器块不是合法 JSON：{exc}"})
            block = {}
    if block:
        want = {"verdict": n["verdict"], "exit_code": n["exit_code"], "gates": n["gates"],
                "hard_gates_failed": n["hard_gates_failed"], "waived": n["waived"],
                "blocking": n["blocking"], "pages": n["pages"], "chapters": n["chapters"],
                "figures": n["figures"], "tables": n["tables"], "equations": n["equations"],
                "human_facing_markers": n["human_facing_markers"], "need_human": n["need_human"],
                "judged": n["judged"]}
        for k, exp in want.items():
            got = block.get(k)
            if got != exp:
                viol.append({"kind": "note_number_mismatch", "field": k, "note": got, "source": exp,
                             "why": f"说明里的 `{k}` 与产物/判决不一致"})
        miss = [k for k in BLOCK_KEYS if k not in block]
        if miss:
            viol.append({"kind": "machine_block_key_missing", "keys": miss,
                         "why": "机器块缺固定键（SPEC §3.12 第 5 条）"})
    # 目录清单：盘上有的必须**都列出**；列出的必须**都在盘上**
    disk = [it["name"] for it in inventory(d)]
    listed = block.get("files") if isinstance(block.get("files"), list) else None
    if listed is not None:
        for name in listed:
            if name not in disk:
                viol.append({"kind": "note_file_missing", "file": name,
                             "why": f"说明列了 `{name}`，但交付根里没有它"})
        for name in disk:
            if name not in listed:
                viol.append({"kind": "note_inventory_incomplete", "file": name,
                             "why": f"交付根里有 `{name}`，说明的清单里没有它（不许静默隐藏）"})
    for sec in SECTIONS:
        if sec not in text:
            viol.append({"kind": "note_section_missing", "section": sec,
                         "why": f"说明缺骨架标题 `{sec}`"})
    exp_text = build(book, root, s7_dir, vreport, s7report)
    if text != exp_text:
        viol.append({"kind": "note_not_as_generated",
                     "why": "盘上文本 != 按当前产物当场重建的文本（被手改过，或产物变了没重写）"})
    ts = (vreport or _load_json(os.path.join(d, VR_NAME))).get("checked_at")
    if ts and ts in text:
        viol.append({"kind": "note_has_timestamp",
                     "why": "说明里抄了本轮校验时间戳 —— 会破坏「两次校验逐字节相同」（SPEC §3.12 第 2 条）"})
    res = {"ok": not viol, "violations": viol, "sha256": sha, "path": p}
    if from_disk:
        res.update(seal_violations(d))
    return res


def seal_violations(d: str) -> dict:
    """**封条**核对（软门禁 G22 的判据）：上一轮记录的 sha256 == 盘上说明的 sha256。"""
    vp = os.path.join(d, VR_NAME)
    np_ = os.path.join(d, NOTE_NAME)
    if not os.path.exists(vp):
        return {"seal": "unverified", "seal_reason": f"没有 `{VR_NAME}`（老产物 / 首次校验，不误报）",
                "seal_violations": []}
    v = _load_json(vp)
    seal = ((v.get("delivery_note") or {}).get("sha256")) or ""
    if not seal:
        return {"seal": "unverified", "seal_reason": "上一轮校验没有留封条（老产物，不误报）",
                "seal_violations": []}
    if not os.path.exists(np_):
        return {"seal": "fail", "seal_violations": [{"kind": "note_missing",
                "why": f"上一轮有封条，但 `{NOTE_NAME}` 不见了"}]}
    actual = hashlib.sha256(open(np_, "rb").read()).hexdigest()
    if actual != seal:
        return {"seal": "fail", "seal_violations": [{"kind": "note_edited_since_validation",
                "sealed": seal[:12], "actual": actual[:12],
                "why": "交付说明自上一轮校验之后被改过（封条对不上）"}]}
    return {"seal": "pass", "seal_measured": {"sealed_sha256": seal[:12]}, "seal_violations": []}


def seal_check(root: str = "", key: str = "", s7_dir: str = "") -> dict:
    d = s7_dir or _dir(root, resolve_key(key))
    return seal_violations(d)


# ---------------------------------------------------------------- CLI
def cmd_build(a) -> int:
    root, key = a.artifact_root, resolve_key(a.book)
    d = _dir(root, key, a.s7_dir)
    res = write(a.book, root, a.s7_dir)
    n = numbers(a.book, root, a.s7_dir)
    print(json.dumps({"book": a.book, "key": key, "dir": d, "path": res["path"],
                      "bytes": res["bytes"], "sha256": res["sha256"][:12],
                      "verdict": n["verdict"], "gates": n["gates"]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_check(a) -> int:
    root, key = a.artifact_root, resolve_key(a.book)
    res = check(a.book, root, a.s7_dir)
    print(json.dumps({"book": a.book, "key": key, "ok": res["ok"], "path": res["path"],
                      "sha256": res.get("sha256", "")[:12], "seal": res.get("seal"),
                      "violations": [{"kind": v["kind"], "why": v.get("why", "")} for v in res["violations"]]},
                     ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- selftest
def _fixture(root: str) -> str:
    """最小交付根（**够触发每一条判据**）：正文两版 + report.json + validation-report.json + 校对表 + 图。"""
    d = os.path.join(root, "s7-out", "fx")
    for sub in ("figures", "index", "review-kit/pages"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    open(os.path.join(d, "chapter-01-reviewed.md"), "w", encoding="utf-8").write("# 章\n<!-- p001 -->正文\n")
    open(os.path.join(d, "chapter-01-clean.md"), "w", encoding="utf-8").write("# 章\n正文\n")
    open(os.path.join(d, "figures", "fig-p0001-x1.png"), "wb").write(b"\x89PNG")
    open(os.path.join(d, "index", "entities.jsonl"), "w", encoding="utf-8").write("{}\n")
    open(os.path.join(d, "errata.md"), "w", encoding="utf-8").write(
        "| 页 | 原文 | 建议 | 依据 |\n|---|---|---|---|\n")
    open(os.path.join(d, "review-kit", "README-校对说明.md"), "w", encoding="utf-8").write("# 说明\n")
    open(os.path.join(d, "review-kit", "校对表.md"), "w", encoding="utf-8").write(
        "# 校对表\n\n- **待看条数**：11\n- **其中需人工**：7（其余 4 条已由准入门分流）\n")
    open(os.path.join(d, "review-kit", "pages", "p0001.png"), "wb").write(b"\x89PNG")
    rep = {"book": "fx", "counts": {"figures": 2, "tables": 3, "equations": 0, "human_facing_markers": 11,
                                    "vision_insertions": 1, "cross_page_splits": 2,
                                    "cross_page_split_sources": 1},
           "chapter_split": {"input_pages": [1, 2, 3]}, "chapters": [{"no": "01"}],
           "unresolved": {"s5_verdicts": 5}, "judgment": {"pending": []},
           "escalations": {"rate_per_100_pages": 12.5}, "tuning": {"unverified_count": 21},
           "vision_insertions": [{"page": 2, "text": "看图补的一句", "item_id": "TR-1", "model": "m",
                                  "mode": "session", "confidence": 0.9}],
           # 2026-10-26（2.2）：被"跨页切分"消费掉的转写（不再插入正文，但也是我们看图读出来的）
           "split_consumed_transcriptions": ["TR-2", "TR-3"]}
    json.dump(rep, open(os.path.join(d, "report.json"), "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)
    vr = {"book": "fx", "verdict": "可交付", "exit_code": 0, "hard_gates_failed": [], "blocking": [],
          "checked_at": "2026-10-13T12:00:00Z",
          "exceptions_waived": [], "counts": {"pass": 15, "warn": 4, "fail": 1, "unverified": 0, "n/a": 2},
          "gates": {"G3": {"id": "G3", "name": "rejoin", "status": "fail", "blocking": False,
                           "threshold": "同一页内重复 = 0", "measured": {"duplicate_paragraphs_markdown": 1},
                           "evidence": [{"why": "同一页内重复（原书两栏逐字相同）"}]}}}
    json.dump(vr, open(os.path.join(d, VR_NAME), "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)
    return d


def _tamper(path: str, old: str, new: str) -> bool:
    t = open(path, encoding="utf-8").read()
    assert old in t, f"夹具里没有 {old!r}"
    open(path, "w", encoding="utf-8", newline="\n").write(t.replace(old, new, 1))
    return True


def selftest() -> int:
    import shutil
    tmp = tempfile.mkdtemp(prefix="dn-selftest-")
    cases: list[tuple] = []

    def _seal(res: dict) -> None:
        """模拟校验器写封条：把 sha256 记进 `validation-report.json`（**封条字段归校验器所有**）。"""
        p = os.path.join(os.path.dirname(res["path"]), VR_NAME)
        v = _load_json(p)
        v["delivery_note"] = {"sha256": res["sha256"], "ok": True}
        json.dump(v, open(p, "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=2)

    def _wr() -> dict:
        res = write("fx", tmp)
        _seal(res)
        return res

    try:
        d = _fixture(tmp)
        note_p = os.path.join(d, NOTE_NAME)
        res = _wr()
        txt = open(note_p, encoding="utf-8").read()
        cases.append(("生成：8 节骨架齐全 + 机器块可解析", all(s in txt for s in SECTIONS) and bool(BLOCK_RE.search(txt)), None))
        cases.append(("确定性：同输入两次生成**逐字节相同**、且**不抄**校验时间戳",
                      build("fx", tmp) == build("fx", tmp)
                      and "2026-10-13T12:00:00Z" not in txt, None))
        cases.append(("落盘 sha256 == 文本 sha256（封条口径一致）", res["sha256"] == _sha256_text(txt), None))
        blk = json.loads(BLOCK_RE.search(txt).group(1))
        cases.append(("数字取自产物：页/图/表/需人工/已判 都对",
                      (blk["pages"], blk["figures"], blk["tables"], blk["need_human"], blk["judged"])
                      == (3, 2, 3, 7, 5), blk))
        cases.append(("判决与门禁分布转述自 validation-report.json",
                      blk["verdict"] == "可交付" and blk["gates"]["fail"] == 1 and blk["exit_code"] == 0, None))
        cases.append(("目录清单**按磁盘扫描**（含未登记的 sidecar 目录）",
                      "review-kit/" in blk["files"] and "index/" in blk["files"] and NOTE_NAME in blk["files"],
                      blk["files"]))
        cases.append(("已知边界节列出了非 pass 的门禁（G3）", "`G3`" in txt and "rejoin" in txt, None))
        # 2026-10-25：免责声明（判「可交付」时限定为**形态**，并给页脚）
        cases.append(("免责：「可交付」时说明里有「形态」限定 + 指向校对表（需人工 N）",
                      "说的是「形态」" in txt and "不构成对内容正确性" in txt
                      and "需人工 7 条" in txt, None))
        # 2026-10-26（2.2）：视觉贡献拆两栏（插入 + 被切分消费），总数进摘要
        cases.append(("2026-10-26（2.2）：视觉转写拆「插入 1 处 + 被跨页切分消费 2 处」，总数 3 处",
                      "插入正文 **1** 处" in txt and "被跨页切分消费 **2** 处" in txt
                      and "视觉转写共 3 处" in txt, None))
        cases.append(("2026-10-26（2.2）：「本次没有视觉转写」只在**两栏都为 0** 时才出现",
                      "（本次没有视觉转写）" not in txt, None))
        cases.append(("免责页脚：始终有「门禁通过 ≠ 内容已核」",
                      "不等于" in txt and "内容已核" in txt, None))
        cases.append(("免责：**非「可交付」判决** → 不加那段限定（页脚仍在）",
                      _disclaimer({"verdict": "不可交付", "need_human": 3}) == ""
                      and _disclaimer({"verdict": "可交付", "need_human": 3}) != "",
                      _disclaimer({"verdict": "可交付", "need_human": 3})))
        cases.append(("「需人工 / 其余分流」按校对表的两个机器行填（不是推算）",
                      "需人工 7" in txt and "其余 4 条已由准入门分流" in txt, None))
        # 2026-11-15（P0-1/P2-2）：`missing_text` 只数 §3 的真实数据行（§1/§2/占位行都不算）
        _mtf = os.path.join(tmp, "mt-errata.md")
        open(_mtf, "w", encoding="utf-8").write(
            "# 勘误与「非本书内容」（errata）\n\n## 1. 原书印错（source_typo，正文不改）\n\n"
            "| 页码 | 位置 | 原文 | 建议 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n"
            "| 3 | 上 | Horstmam | Horstmann | 原页如此 | 0.9 |\n\n"
            "## 2. 非本书内容（non_book_ink，已移出净版）\n\n"
            "| 页码 | 位置 | 页上原文 | 是什么 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n"
            "| — | — | — | — | 本次未登记 | — |\n\n"
            "## 3. 主引擎漏掉、页面上确实有（missing_text，正文暂缺）\n\n"
            "| 页码 | 缺的内容（页上原文） | 插在哪 | 依据 | 置信度 |\n|---|---|---|---|---|\n"
            "| 2 | [答案: −3.057×10³ J] | 题12与题13之间，另起一行 | 主引擎没有这段 | high |\n")
        cases.append(("2026-11-15（P0-1）：`missing_text` 只数 `§3` 的真实数据行（§1/§2/占位行都不算）",
                      _errata_missing(_mtf) == 1 and _errata_non_book(_mtf) == 0, _errata_missing(_mtf)))
        # 插回后：交付说明必须改口（不能一边说"暂时没有"、一边正文里已经有）
        _md2 = os.path.join(tmp, "ch2")
        os.makedirs(_md2, exist_ok=True)
        open(os.path.join(_md2, "chapter-01-reviewed.md"), "w", encoding="utf-8").write(
            "正文。\n\n<!-- MISSING-TEXT-INSERT id=MT-1 page=2 -->\n[答案:1 J]\n\n后续。\n")
        cases.append(("2026-11-15（1b）：正文里有 `MISSING-TEXT-INSERT` → 数出「已插回」条数",
                      _missing_inserted(_md2) == 1 and _missing_inserted(os.path.join(tmp, "nope")) is None,
                      _missing_inserted(_md2)))
        _ph = os.path.join(tmp, "ph-errata.md")
        open(_ph, "w", encoding="utf-8").write(
            "| 页码 | 位置 | 原文 | 建议 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n"
            "| — | — | — | — | 本样本未发现 U 类条目 | — |\n")
        _dt = os.path.join(tmp, "data-errata.md")
        open(_dt, "w", encoding="utf-8").write(
            "| 页码 | 位置 | 原文 | 建议 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n"
            "| 12 | 第 3 段 | Horstmam | Horstmann | 原页确印如此 | 0.9 |\n"
            "| 33 | 表下注 | 液氨 | 液氦 | 原页确印如此 | 0.8 |\n")
        cases.append(("errata **占位行不算条数**（真书空表的占位行实测会被错报成 1 条）",
                      _errata_rows(os.path.join(d, "errata.md")) == 0
                      and _errata_rows(_ph) == 0 and _errata_rows(_dt) == 2, None))
        # 2026-10-26：两类 kind 分开计数（§1 原书印错 / §2 非本书内容）
        _nb_p = os.path.join(tmp, "nb-errata.md")
        open(_nb_p, "w", encoding="utf-8").write(
            "# 勘误与「非本书内容」（errata）\n\n## 1. 原书印错（source_typo，正文不改）\n\n"
            "| 页码 | 位置 | 原文 | 建议 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n"
            "| — | — | — | — | 本次未登记 | — |\n\n"
            "## 2. 非本书内容（non_book_ink，已移出净版）\n\n"
            "| 页码 | 位置 | 页上原文 | 是什么 | 依据 | 置信度 |\n|---|---|---|---|---|---|\n"
            "| 12 | 右栏 · 下部 | (1) 以电炉丝为系统；W>0, Q<0, OU=0 | 移出净版（非本书内容） | 红笔手写 | 0.95 |\n")
        cases.append(("2026-10-26：errata 两类**分开计数**（合计 1、其中非书内容 1；占位行不算）",
                      _errata_rows(_nb_p) == 1 and _errata_non_book(_nb_p) == 1
                      and _errata_non_book(_dt) == 0, {"nb": _errata_non_book(_nb_p)}))
        cases.append(("页码范围给人话（`[[1,50]]` → 「1–50」），不甩原始 JSON",
                      _ranges_human([[1, 50]]) == "1–50" and _ranges_human([[1, 2], [5, 5]]) == "1–2、5",
                      None))
        cases.append(("分发提示不出现双句号（notice 自带句号时要去重）",
                      _notice_human("原书如此。") == "原书如此。", None))
        c = check("fx", tmp)
        cases.append(("check：刚写完 → ok（无违例）", c["ok"] and not c["violations"], c["violations"]))
        cases.append(("封条：写完后 seal_check = pass", c.get("seal") == "pass", c.get("seal")))

        _tamper(note_p, '"human_facing_markers": 11', '"human_facing_markers": 99')
        c = check("fx", tmp)
        cases.append(("负例：改机器块里的数字 → `note_number_mismatch`（且封条对不上）",
                      any(v["kind"] == "note_number_mismatch" for v in c["violations"])
                      and c.get("seal") == "fail"
                      and any(v["kind"] == "note_edited_since_validation" for v in c["seal_violations"]),
                      [v["kind"] for v in c["violations"]]))
        _wr()                                                       # 复原

        _tamper(note_p, "## 5. 数字速览", "## 5. 数字")
        cases.append(("负例：删掉一个骨架标题 → `note_section_missing`",
                      any(v["kind"] == "note_section_missing" for v in check("fx", tmp)["violations"]), None))
        _wr()

        _tamper(note_p, "本文件由独立校验器", "本文件由我手改")
        cases.append(("负例：手改人读文字 → `note_not_as_generated`",
                      any(v["kind"] == "note_not_as_generated" for v in check("fx", tmp)["violations"]), None))
        _wr()

        shutil.rmtree(os.path.join(d, "figures"))
        cases.append(("负例：清单里的目录不见了 → `note_file_missing`",
                      any(v["kind"] == "note_file_missing" for v in check("fx", tmp)["violations"]), None))
        os.makedirs(os.path.join(d, "figures"), exist_ok=True)
        open(os.path.join(d, "figures", "x.png"), "wb").write(b"\x89PNG")

        open(os.path.join(d, "新出现的东西.json"), "w", encoding="utf-8").write("{}\n")
        cases.append(("负例：盘上多了未列出的条目 → `note_inventory_incomplete`（不许静默隐藏）",
                      any(v["kind"] == "note_inventory_incomplete" for v in check("fx", tmp)["violations"]), None))
        os.remove(os.path.join(d, "新出现的东西.json"))

        _tamper(note_p, "<!-- DELIVERY-NOTE", "<!-- DELIVERY-NOTE-X")
        cases.append(("负例：机器块被破坏 → `machine_block_missing`",
                      any(v["kind"] == "machine_block_missing" for v in check("fx", tmp)["violations"]), None))
        _wr()

        _tamper(note_p, '{"blocking"', '{blocking"')
        cases.append(("负例：机器块 JSON 坏掉 → `machine_block_unparsable`",
                      any(v["kind"] == "machine_block_unparsable" for v in check("fx", tmp)["violations"]), None))
        _wr()

        os.remove(note_p)
        cases.append(("负例：说明文件被删 → `note_missing`（check）且封条 = fail",
                      check("fx", tmp)["violations"][0]["kind"] == "note_missing"
                      and seal_check(tmp, "fx")["seal"] == "fail", None))
        _wr()

        os.remove(os.path.join(d, VR_NAME))
        cases.append(("无 validation-report.json（老产物）→ 封条 **unverified**，不误报",
                      seal_check(tmp, "fx")["seal"] == "unverified", None))
        cases.append(("字段缺失（没有判决）→ 渲染成「未记录」，不崩",
                      "未记录" in build("fx", tmp), None))
        json.dump({"book": "fx"}, open(os.path.join(d, VR_NAME), "w", encoding="utf-8"), ensure_ascii=False)

        cases.append(("`--book` 用书名字典键也能解析到目录键（`fx`）", resolve_key("fx") == "fx", None))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} 条自测通过")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="A6 交付说明（交付根自解释）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--book", required=True)
        sp.add_argument("--artifact-root", default=ROOT)
        sp.add_argument("--s7-dir", default="")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    return cmd_build(a) if a.cmd == "build" else cmd_check(a)


if __name__ == "__main__":
    sys.exit(main())
