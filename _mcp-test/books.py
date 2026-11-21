# -*- coding: utf-8 -*-
"""**L1 部署配置：书注册表**（B1-2）。把"哪本书对应哪些数据"从代码里挪进 `books.json`。

**为什么**：换机器/换书/交给别人时，**改 JSON 而不是改 Python**；路径写成**相对产物根**，
所以整棵树搬走也不用动它。权威口径仍见 `SPEC-化学教材PDF转Markdown-v2.md` §10。

**校验是硬的**（宁可加载失败，也不静默用错书）：
  * 顶层：`schema` 必须是 1；`books` 必须是非空对象；
  * 每本：`label`/`key`/`pdf`/`mineru_dir`/`paddle_dir`/`profile` 齐备且为非空字符串；
  * `key` 全局唯一、且不含路径分隔符（它会被当目录名用）；
  * `page_labels`（可选）必须是整数列表；
  * **出现未知字段 → 直接报错**（防止拼错字段名后"看起来生效其实没生效"）。

**路径解析**：JSON 里写**相对产物根**的 POSIX 风格路径（`ocr-raw/paddle`），
`load()` 返回与旧代码**完全同形**的 dict（绝对路径、`key`、可选 `page_labels`），
因此 `pipeline_run.BOOKS` 及其 6 处消费方**零改动**；返回的是普通可变 dict（有自测会临时改写它）。

用法:
  python -X utf8 books.py list                     # 列出注册表（相对路径）
  python -X utf8 books.py check                    # 校验 + 检查路径是否存在
  python -X utf8 books.py selftest
环境变量 `BOOKWF_BOOKS` 可指向另一份注册表（部署级覆盖，可选）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

SCHEMA_VERSION = 1
REQUIRED = ("label", "key", "pdf", "mineru_dir", "paddle_dir", "profile")
OPTIONAL = ("page_labels", "source_pdf", "source_ranges", "chapter_anchors", "toc_pages", "layout",
            "privacy_ack", "notice",
            # 2026-10-26：按问题类型的"默认答复"（用户说过"后续同类按此处理，不再询问"才写）
            "ask_defaults",
            "state", "retired_at", "why")
# `ask_defaults` 允许的键（问题类型）与取值说明：**只记"用户明确同意过"的默认**，不许预置。
ASK_DEFAULT_KINDS = ("non_book_ink", "layout")
# 每种问题类型目前允许的默认答复（收窄是有意的：默认不是自由文本，避免写成一句没人执行的承诺）
ASK_DEFAULT_VALUES = {"non_book_ink": "quarantine", "layout": "declared"}
TOP_KEYS = ("schema", "note", "books", "ask_defaults")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.data_root()                                  # 数据根（默认 = 工具根；见 paths.py / SPEC §7.10）
REGISTRY_NAME = "books.json"
SAMPLE_NAME = "books.sample.json"
STATES = ("active", "retired")
LEGACY_PATH = os.path.join(HERE, REGISTRY_NAME)           # **旧位置**（B2b 之前注册表在工具目录里）
DEFAULT_PATH = LEGACY_PATH                                # 兼容别名（老代码/老文档引用它）


def registry_path(root: str = "") -> str:
    """注册表的**权威位置 = 数据根下的 `books.json`**（SPEC §3.13）。

    为什么搬：注册表是**数据**（每台机器各不相同），原先它躺在**工具目录** `_mcp-test/books.json` 里、
    还**进了发布集** —— 等于把某台机器的书单打包送人；新机器上那些书根本不存在。
    优先级：**`BOOKWF_BOOKS` → `<数据根>/books.json` → 兼容旧位置 `<工具根>/_mcp-test/books.json`**。
    返回的是"应当使用/创建"的路径（存在与否由调用方决定）。
    """
    env = (os.environ.get("BOOKWF_BOOKS") or "").strip()
    if env:
        return paths.normalize(env)
    r = root or ROOT
    p = os.path.join(r, REGISTRY_NAME)
    if os.path.exists(p):
        return p
    if os.path.exists(LEGACY_PATH):                       # 旧树：继续用，但提示一次
        return LEGACY_PATH
    return p                                              # 新机器：返回"应有位置"（首次 `intake add` 会建它）


def sample_path() -> str:
    """随发布集走的**示例注册表**（模板 + 示例条目；下载后看到的就是它）。"""
    return os.path.join(HERE, SAMPLE_NAME)


class BookRegistryError(ValueError):
    """注册表校验失败（**必须报错**，不许静默兜底）。"""


def _resolve(root: str, rel: str) -> str:
    """JSON 里的 POSIX 相对路径 → 本机绝对路径（根由调用方给，默认是产物根）。"""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
    return os.path.join(root, *parts) if parts else root


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def _check_ask_defaults(ad, where: str, errs: list) -> None:
    """校验 `ask_defaults`（**每本**与**顶层全局**共用同一套规则）。"""
    if ad is None:
        return
    if not isinstance(ad, dict):
        errs.append(where + ".ask_defaults：必须是对象（{问题类型: 默认答复}）")
        return
    for _k, _v in ad.items():
        if _k not in ASK_DEFAULT_KINDS:
            errs.append(where + ".ask_defaults：未知问题类型 %r（允许 %s）"
                        % (_k, "/".join(ASK_DEFAULT_KINDS)))
        elif _v != ASK_DEFAULT_VALUES[_k]:
            errs.append(where + ".ask_defaults.%s：只接受 %r（实际 %r）"
                        % (_k, ASK_DEFAULT_VALUES[_k], _v))
        if not isinstance(_v, str) or not _v.strip():
            errs.append(where + ".ask_defaults.%s：默认答复必须是非空字符串" % _k)


def load(path: str = "", root: str = "") -> dict:
    """读注册表 → **与旧代码同形**的 dict（绝对路径）。校验不过就抛 `BookRegistryError`。"""
    explicit = bool(path or (os.environ.get("BOOKWF_BOOKS") or "").strip())
    p = path or registry_path(root)
    r = root or ROOT
    if not os.path.exists(p):
        if explicit:
            raise BookRegistryError("书注册表不存在：" + p + "（**显式指定**的路径/环境变量不给兜底）")
        # 新机器：**空注册表是合法状态**（SPEC §3.13）——不静默、也不崩，给一句可操作提示
        print("[books] 还没有书注册表（%s）。先按 %s 的形状建第一本："
              % (p, os.path.basename(sample_path())))
        print("        python -X utf8 _mcp-test/intake.py add <切片PDF> --ranges 1-50 --key <目录键>")
        return {}
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise BookRegistryError("顶层必须是对象")
    _errs_top: list = []
    _check_ask_defaults(data.get("ask_defaults"), "顶层", _errs_top)
    if _errs_top:
        raise BookRegistryError("注册表校验失败：\n  - " + "\n  - ".join(_errs_top))
    unknown_top = sorted(set(data) - set(TOP_KEYS))
    if unknown_top:
        raise BookRegistryError("顶层出现未知字段：" + ", ".join(unknown_top))
    if data.get("schema") != SCHEMA_VERSION:
        raise BookRegistryError("schema 必须是 " + str(SCHEMA_VERSION)
                                + "，实际 " + repr(data.get("schema")))
    bks = data.get("books")
    if not isinstance(bks, dict):
        raise BookRegistryError("`books` 必须是对象（**空对象是合法的**：新机器还没登记任何书）")
    out, seen_keys, errs = {}, {}, []
    for name, cfg in bks.items():
        if not isinstance(cfg, dict):
            errs.append(name + "：必须是对象")
            continue
        miss = [f for f in REQUIRED if not str(cfg.get(f) or "").strip()]
        if miss:
            errs.append(name + "：缺字段 " + ", ".join(miss))
            continue
        unknown = sorted(set(cfg) - set(REQUIRED) - set(OPTIONAL))
        if unknown:
            errs.append(name + "：未知字段 " + ", ".join(unknown))
            continue
        for f in REQUIRED:
            if not isinstance(cfg[f], str):
                errs.append(name + "." + f + "：必须是字符串")
        key = cfg["key"]
        if any(s in key for s in ("/", "\\")):
            errs.append(name + ".key：不能含路径分隔符（会被当目录名）")
        if key in seen_keys:
            errs.append(name + ".key：与 " + seen_keys[key] + " 重复（" + key + "）")
        seen_keys[key] = name
        if "page_labels" in cfg:
            pl = cfg["page_labels"]
            if not (isinstance(pl, list) and pl and all(isinstance(x, int) for x in pl)):
                errs.append(name + ".page_labels：必须是**非空整数列表**")
        if "source_pdf" in cfg and not isinstance(cfg["source_pdf"], str):
            errs.append(name + ".source_pdf：必须是字符串")
        if "source_ranges" in cfg:
            sr = cfg["source_ranges"]
            if not (isinstance(sr, list) and sr and all(
                    isinstance(x, list) and len(x) == 2 and all(isinstance(v, int) for v in x) for x in sr)):
                errs.append(name + ".source_ranges：必须是 [[a,b],…] 整数页段")
        st = cfg.get("state", "active")
        if st not in STATES:
            errs.append(name + ".state：必须是 " + " / ".join(STATES) + "（实际 " + repr(st) + "）")
        for f in ("retired_at", "why"):
            if f in cfg and not isinstance(cfg[f], str):
                errs.append(name + "." + f + "：必须是字符串")
        if st == "retired" and not str(cfg.get("why") or "").strip():
            errs.append(name + "：state=retired 必须带 `why`（**为什么退休**要写清，不许静默）")
        if "privacy_ack" in cfg and not isinstance(cfg["privacy_ack"], bool):
            errs.append(name + ".privacy_ack：必须是布尔值（是否允许整页图上传云端）")
        if "notice" in cfg and not isinstance(cfg["notice"], str):
            errs.append(name + ".notice：必须是字符串（A6 交付说明的「出处与分发」提示，自由文本）")
        if "chapter_anchors" in cfg:
            ca = cfg["chapter_anchors"]
            if not (isinstance(ca, list) and ca and all(isinstance(x, int) for x in ca)):
                errs.append(name + ".chapter_anchors：必须是**非空整数列表**（源 PDF 物理页号，1 起）")
            elif ca != sorted(set(ca)):
                errs.append(name + ".chapter_anchors：必须**严格递增且不重复**")
            else:
                _sr = cfg.get("source_ranges")
                if isinstance(_sr, list):
                    _pages = set()
                    for _r in _sr:
                        if (isinstance(_r, list) and len(_r) == 2
                                and all(isinstance(_v, int) for _v in _r)):
                            _pages.update(range(_r[0], _r[1] + 1))
                    _bad = [a for a in ca if _pages and a not in _pages]
                    if _bad:
                        errs.append(name + ".chapter_anchors：必须落在 source_ranges 内"
                                    "（源 PDF 物理页号）：" + repr(_bad))
        if "toc_pages" in cfg:
            tp = cfg["toc_pages"]
            if not (isinstance(tp, list) and tp and all(isinstance(x, int) for x in tp)):
                errs.append(name + ".toc_pages：必须是**非空整数列表**（源 PDF 物理页号，1 起）")
            elif tp != sorted(set(tp)):
                errs.append(name + ".toc_pages：必须**严格递增且不重复**")
            else:
                _sr = cfg.get("source_ranges")
                if isinstance(_sr, list):
                    _pages = set()
                    for _r in _sr:
                        if (isinstance(_r, list) and len(_r) == 2
                                and all(isinstance(_v, int) for _v in _r)):
                            _pages.update(range(_r[0], _r[1] + 1))
                    _bad = [p for p in tp if _pages and p not in _pages]
                    if _bad:
                        errs.append(name + ".toc_pages：必须落在 source_ranges 内"
                                    "（源 PDF 物理页号）：" + repr(_bad))
        if "layout" in cfg:
            lay = cfg["layout"]
            _kinds = {"单栏": 1, "两栏": 2, "主副栏": 2, "三栏": 3}
            if not (isinstance(lay, list) and lay):
                errs.append(name + ".layout：必须是**非空段列表**")
            else:
                _lp, _lay_ok = set(), True
                for _seg in lay:
                    if not isinstance(_seg, dict):
                        errs.append(name + ".layout：每段必须是对象")
                        _lay_ok = False
                        continue
                    _kind, _reg = _seg.get("kind"), _seg.get("regions")
                    _cuts = _seg.get("cuts") or []
                    if _kind not in _kinds:
                        errs.append(name + ".layout：未知版式 %r" % (_kind,))
                        _lay_ok = False
                    elif _reg != _kinds[_kind]:
                        errs.append(name + ".layout：%s 的 regions 必须是 %d"
                                    % (_kind, _kinds[_kind]))
                        _lay_ok = False
                    if not (isinstance(_cuts, list) and all(
                            isinstance(c, (int, float)) and not isinstance(c, bool) for c in _cuts)):
                        errs.append(name + ".layout：cuts 必须是数字列表")
                        _lay_ok = False
                        _cuts = []
                    if _cuts and (len(_cuts) != _kinds.get(_kind, 1) - 1
                                  or _cuts != sorted(set(_cuts))
                                  or any(not (0 < c < 1) for c in _cuts)):
                        errs.append(name + ".layout：cuts 必须是 (0,1) 内严格递增的 k-1 条")
                        _lay_ok = False
                    _pgs = _seg.get("pages")
                    if not (isinstance(_pgs, list) and _pgs and all(
                            isinstance(x, list) and len(x) == 2 and all(isinstance(v, int) for v in x)
                            for x in _pgs)):
                        errs.append(name + ".layout：pages 必须是 [[a,b],…]")
                        _lay_ok = False
                        continue
                    for _a, _b in _pgs:
                        _r = set(range(_a, _b + 1))
                        if _lp & _r:
                            errs.append(name + ".layout：页段重叠")
                            _lay_ok = False
                        _lp |= _r
                _sr = cfg.get("source_ranges")
                if _lay_ok and isinstance(_sr, list):
                    _src = set()
                    for _a, _b in _sr:
                        _src |= set(range(_a, _b + 1))
                    if _lp != _src:
                        errs.append(name + ".layout：页段必须恰好覆盖 source_ranges（源 PDF 物理页号）")
        # 2026-10-26：`ask_defaults`（按问题类型的默认答复）——**只记用户明确同意过的**，不许预置
        _check_ask_defaults(cfg.get("ask_defaults"), name, errs)
        row = {"label": cfg["label"], "key": key,
               "pdf": _resolve(r, cfg["pdf"]),
               "mineru_dir": _resolve(r, cfg["mineru_dir"]),
               "paddle_dir": _resolve(r, cfg["paddle_dir"]),
               "profile": _resolve(r, cfg["profile"])}
        if "page_labels" in cfg:
            row["page_labels"] = list(cfg["page_labels"])
        if "source_pdf" in cfg:
            row["source_pdf"] = _resolve(r, cfg["source_pdf"])
        row["state"] = cfg.get("state", "active")
        for f in ("notice", "retired_at", "why"):
            if f in cfg:
                row[f] = cfg[f]
        for k in ("source_ranges", "chapter_anchors", "toc_pages", "layout", "privacy_ack"):
            if k in cfg:
                row[k] = json.loads(json.dumps(cfg[k]))          # 深拷贝，避免外部篡改
        # 2026-11-15（P2-1）：**全局 `ask_defaults` + 书级覆盖** → 合并成生效值，并记来源
        _gad = data.get("ask_defaults") or {}
        _bad = cfg.get("ask_defaults") or {}
        _eff, _src = {}, {}
        for _k in ASK_DEFAULT_KINDS:
            if _k in _bad:
                _eff[_k], _src[_k] = _bad[_k], "book"
            elif _k in _gad:
                _eff[_k], _src[_k] = _gad[_k], "global"
        if _eff:
            row["ask_defaults"] = _eff
            row["ask_defaults_source"] = _src
        out[name] = row
    if errs:
        raise BookRegistryError("注册表校验失败：\n  - " + "\n  - ".join(errs))
    return out


def validate_existence(books: dict) -> list:
    """检查注册表里引用的**路径是否存在**（加载时**不查**：注册表允许先写、数据后到）。"""
    miss = []
    for name, cfg in sorted(books.items()):
        if cfg.get("state") == "retired":
            continue            # **退休的书不要求数据在位**（SPEC §3.13）——"我不再维护它了"是可声明的状态
        for f in ("pdf", "mineru_dir", "paddle_dir", "profile"):
            if not os.path.exists(cfg[f]):
                # 同时给**目录键**：自测/预检要按目录键引用（`name` 是注册表条目名，两者不同名！
                # 实测踩过：混用会让"已交付书不得缺失"那条断言恒为真 = 假绿）
                miss.append({"book": name, "key": cfg.get("key") or name, "field": f, "path": cfg[f]})
    return miss


def _write_tmp(cfg: dict) -> str:
    import tempfile
    fd, p = tempfile.mkstemp(prefix="books-", suffix=".json")
    os.close(fd)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return p


def delivered(books: dict, root: str = "") -> list:
    """**已交付的书**（目录键）—— 从**磁盘**推导：`<数据根>/s7-out/<key>/report.json` 存在即算。

    为什么不写死书单：书单是"某台机器的历史"，换机器就不成立。判据唯一出口就是这里。
    """
    r = root or ROOT
    out = []
    for cfg in (books or {}).values():
        k = (cfg or {}).get("key")
        if k and os.path.exists(os.path.join(r, "s7-out", k, "report.json")):
            out.append(k)
    return sorted(out)


def published(root: str = "") -> list:
    """**有归档快照的书**（目录键）—— 从 `<数据根>/s7-out/_published/` **枚举**（同样不写死）。"""
    r = root or ROOT
    d = os.path.join(r, "s7-out", "_published")
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)))


def delivered_missing(miss: list, delivered: tuple, hard_fields=("pdf", "profile")) -> dict:
    """**已交付的书**里，哪些**硬字段**（默认：源切片与画像）缺失了。

    为什么单独抽成一个纯函数：原来的自测**比错了标识符** —— 它把 `validate_existence` 返回的
    `book`（注册表**条目名**，如 `puhua`）拿去和**目录键**（如 `puhua-26-28`）比，交集恒为空
    ⇒ 那条"已交付书不得有任何缺失"**从来没真正检查过任何东西**（假绿，B2 解释时当场查出）。
    本函数按**目录键**归类，并且把"缺的是引擎原生产物（可归档）"与"缺的是源切片/画像（不可丢）"
    分开 —— 后者缺失才算硬伤。
    """
    hard = tuple(hard_fields)
    by_key: dict = {}
    for m in miss:
        k = m.get("key") or m.get("book") or ""
        by_key.setdefault(k, set()).add(m.get("field", ""))
    return {k: sorted(v & set(hard)) for k, v in by_key.items() if k in set(delivered) and (v & set(hard))}


def selftest() -> int:
    cases = []
    b = load()
    smp = load(path=sample_path(), root=ROOT)        # 示例注册表（随发布集走，**机器无关**）
    cases.append(("示例注册表 `books.sample.json` 可加载、形状合规（下载后能看到的模板；≥1 条、字段齐）",
                  bool(smp) and all(c.get("key") and c.get("label") and c.get("pdf") for c in smp.values()),
                  sorted(smp)))
    cases.append(("真实注册表可加载 —— **空注册表也是合法状态**（新机器就是这样；不再拿某台机器的书单当基线）",
                  isinstance(b, dict), {"条目数": len(b), "注册表": os.path.basename(registry_path())}))
    cases.append(("注册表**权威位置 = 数据根下**（`BOOKWF_BOOKS` > 数据根 > 兼容旧位置 `_mcp-test/books.json`）",
                  registry_path().endswith(REGISTRY_NAME)
                  and paths.normalize(registry_path()) in (paths.normalize(os.path.join(ROOT, REGISTRY_NAME)),
                                                           paths.normalize(LEGACY_PATH)),
                  registry_path()))
    cases.append(("返回的是**普通可变 dict**（自测会临时加书，见 c1/c2 回归）",
                  isinstance(b, dict) and (b.__setitem__("__tmp__", {}) or True)
                  and b.pop("__tmp__", None) is not None, None))
    cases.append((("路径已解析为绝对路径且都在数据根下" if b else
                   "路径解析：**注册表为空**（新机器）→ 该断言明示跳过"),
                  (all(str(c["pdf"]).startswith(ROOT) for c in b.values()) if b else True), None))
    _miss = validate_existence(b)
    # **数据不在 ⇒ 明示跳过**（新鲜下载/空机器上"输入与引擎产物都不在"是正常的，不是失败）；
    # 数据在而缺 ⇒ 仍然 FAIL（见 SPEC §7.10 的口径）。
    _has_data = paths.has_data()
    cases.append((("`validate_existence`：缺失只可能是**引擎类目录**，且逐书如实列出（新登记的书在跑引擎前允许缺失）"
                   if _has_data else
                   "`validate_existence`：**这份拷贝里没有数据树** → 与数据有关的断言明示跳过（缺失清单照常打印）"),
                  (all(m["field"] in ("mineru_dir", "paddle_dir") for m in _miss) if _has_data else True),
                  _miss[:2]))
    _ran = tuple(delivered(b))              # **从磁盘推导**（`s7-out/<key>/report.json` 存在即算）——不写死书单
    _hard = delivered_missing(_miss, _ran)
    _gone = sorted({(m.get("key"), m["field"]) for m in _miss if (m.get("key") in _ran)})
    cases.append((("`validate_existence`：**已交付书**的**源切片与画像**不得缺失（引擎原生产物若已按"
                   "清理批次移出，必须在这里**如实登记**）" if _has_data else
                   "`validate_existence`：**这份拷贝里没有数据树** → 「已交付书不得缺失」明示跳过"),
                  (not _hard) if _has_data else True, {"硬缺失": _hard, "如实登记": _gone[:4]}))
    # retired：**不要求数据在位**（SPEC §3.13）——"不再维护"必须可声明，不是靠删条目
    _fake = {"a": {"label": "l", "key": "ak", "pdf": "/nope/a", "mineru_dir": "/nope/b",
                   "paddle_dir": "/nope/c", "profile": "/nope/d", "state": "active"},
             "r": {"label": "l", "key": "rk", "pdf": "/nope/a", "mineru_dir": "/nope/b",
                   "paddle_dir": "/nope/c", "profile": "/nope/d", "state": "retired", "why": "已归档"}}
    cases.append(("`validate_existence`：**retired 的书不要求数据在位**（只登记 active 的缺失）",
                  sorted({m["book"] for m in validate_existence(_fake)}) == ["a"], validate_existence(_fake)))
    # delivered()/published()：**从磁盘推导**（不写死书单）
    _t = paths.tmp_dir("books-derived-")
    os.makedirs(os.path.join(_t, "s7-out", "k1"), exist_ok=True)
    os.makedirs(os.path.join(_t, "s7-out", "_published", "k1", "20260101T000000Z"), exist_ok=True)
    open(os.path.join(_t, "s7-out", "k1", "report.json"), "w", encoding="utf-8").write("{}")
    _bk = {"x": {"key": "k1"}, "y": {"key": "k2"}}
    cases.append(("`delivered()`：**从磁盘推导**（有 `s7-out/<key>/report.json` 才算已交付；不是写死书单）",
                  delivered(_bk, _t) == ["k1"] and delivered(_bk, _t + "-none") == [], delivered(_bk, _t)))
    cases.append(("`published()`：**从 `s7-out/_published/*` 枚举**（同样不写死书单）",
                  published(_t) == ["k1"] and published(_t + "-none") == [], published(_t)))

    # 负例：**按目录键**比对才抓得住（用错的标识符正是原来的假绿根因）
    cases.append(("`delivered()`：本注册表的已交付书 = %s（从磁盘推导）" % (list(_ran) or "（无）"),
                  isinstance(_ran, tuple), list(_ran)))
    _syn = ("k-a", "k-b")            # 夹具用**合成键**（不引用任何真实书 key —— 换机器也成立）
    cases.append(("负例：已交付书的**源切片**缺失 → `delivered_missing` 必须报出来"
                  "（原写法拿注册表条目名比目录键 → 恒为真 = 假绿）",
                  delivered_missing([{"book": "x", "key": "k-a", "field": "pdf"},
                                     {"book": "x", "key": "k-a", "field": "mineru_dir"}], _syn)
                  == {"k-a": ["pdf"]}
                  and delivered_missing([{"book": "x", "key": "k-a", "field": "mineru_dir"}], _syn) == {},
                  None))

    # ---- 负例：校验必须真的会拦（B2 的教训：没有负例的检查等于没有检查）----
    base = {"schema": 1, "books": {"x": {"label": "l", "key": "k", "pdf": "a", "mineru_dir": "b",
                                         "paddle_dir": "c", "profile": "d"}}}
    def _bad(mut, tag):
        import copy
        cfg = copy.deepcopy(base)
        mut(cfg)
        p = _write_tmp(cfg)
        try:
            load(p)
            return False, tag
        except BookRegistryError:
            return True, tag
        finally:
            os.remove(p)
    cases.append(("负例：缺必填字段 → 报错", _bad(lambda c: c["books"]["x"].pop("profile"), "缺字段")[0], None))
    cases.append(("负例：未知字段（拼错名） → 报错",
                  _bad(lambda c: c["books"]["x"].update({"pdff": "x"}), "未知字段")[0], None))
    cases.append(("负例：key 重复 → 报错",
                  _bad(lambda c: c["books"].update({"y": dict(c["books"]["x"])}), "key 重复")[0], None))
    cases.append(("负例：page_labels 不是整数列表 → 报错",
                  _bad(lambda c: c["books"]["x"].update({"page_labels": ["1"]}), "page_labels")[0], None))
    cases.append(("负例：layout 未知版式 → 报错",
                  _bad(lambda c: c["books"]["x"].update(
                      {"layout": [{"pages": [[1, 2]], "kind": "双栏", "regions": 2, "cuts": []}]}),
                      "layout")[0], None))
    cases.append(("负例：layout 三栏只给 1 条切分线 → 报错",
                  _bad(lambda c: c["books"]["x"].update(
                      {"layout": [{"pages": [[1, 2]], "kind": "三栏", "regions": 3,
                                   "cuts": [0.3]}]}), "layout")[0], None))
    cases.append(("负例：privacy_ack 不是布尔 → 报错",
                  _bad(lambda c: c["books"]["x"].update({"privacy_ack": "yes"}), "privacy_ack")[0], None))
    # 2026-10-26：ask_defaults（按问题类型的默认答复）
    def _ok(mut):
        import copy
        cfg = copy.deepcopy(base)
        mut(cfg)
        p = _write_tmp(cfg)
        try:
            return load(p)
        finally:
            os.remove(p)
    # 2026-11-15（P2-1）：**全局层** + 书级覆盖
    def _reg(top_ad=None, book_ad=None):
        c = {"schema": SCHEMA_VERSION, "note": "t",
             "books": {"x": {"label": "X", "key": "x", "pdf": "a.pdf", "mineru_dir": "m",
                             "paddle_dir": "p", "profile": "s.json"}}}
        if top_ad is not None:
            c["ask_defaults"] = top_ad
        if book_ad is not None:
            c["books"]["x"]["ask_defaults"] = book_ad
        return c

    import tempfile as _tf, os as _os
    def _load(c):
        d = _tf.mkdtemp()
        fp = _os.path.join(d, "books.json")
        with open(fp, "w", encoding="utf-8") as _fh:
            _fh.write(json.dumps(c, ensure_ascii=False))
        try:
            return load(fp, root=d)["x"], None
        except BookRegistryError as e:
            return None, str(e)

    _r, _e = _load(_reg(top_ad={"non_book_ink": "quarantine"}))
    cases.append(("2026-11-15（P2-1）：顶层 `ask_defaults` 对**所有书**生效（来源标 global）",
                  _r is not None and _r["ask_defaults"] == {"non_book_ink": "quarantine"}
                  and _r["ask_defaults_source"] == {"non_book_ink": "global"}, _e))
    _r2, _e2 = _load(_reg(top_ad={"non_book_ink": "quarantine", "layout": "declared"},
                         book_ad={"non_book_ink": "quarantine"}))
    cases.append(("2026-11-15（P2-1）：书级与全局同类型 → **书级优先**，另一类型仍来自全局",
                  _r2 is not None
                  and _r2["ask_defaults_source"] == {"non_book_ink": "book", "layout": "global"},
                  _e2))
    _r3, _e3 = _load(_reg(top_ad={"handwriting": "ignore"}))
    cases.append(("2026-11-15（P2-1）负例：顶层 `ask_defaults` 非法类型 → 报错（不静默）",
                  _r3 is None and "ask_defaults" in (_e3 or ""), _e3))
    cases.append(("ask_defaults：合法值（non_book_ink=quarantine / layout=declared）被接受且透传",
                  _ok(lambda c: c["books"]["x"].update(
                      {"ask_defaults": {"non_book_ink": "quarantine", "layout": "declared"}}))
                  ["x"]["ask_defaults"] == {"non_book_ink": "quarantine", "layout": "declared"}, None))
    cases.append(("负例：ask_defaults 未知问题类型 → 报错",
                  _bad(lambda c: c["books"]["x"].update({"ask_defaults": {"handwriting": "ignore"}}),
                       "ask_defaults")[0], None))
    cases.append(("负例：ask_defaults 值不在枚举里 → 报错（不许写自由文本承诺）",
                  _bad(lambda c: c["books"]["x"].update(
                      {"ask_defaults": {"non_book_ink": "保持原样"}}), "ask_defaults")[0], None))
    cases.append(("负例：toc_pages 不是整数列表 → 报错",
                  _bad(lambda c: c["books"]["x"].update({"toc_pages": ["1"]}), "toc_pages")[0], None))
    cases.append(("负例：schema 版本不对 → 报错",
                  _bad(lambda c: c.update({"schema": 99}), "schema")[0], None))
    cases.append(("负例：注册表文件不存在 → 报错（不许静默兜底）", _missing_raises(), None))
    cases.append(("负例：`state` 取值非法 → 报错",
                  _bad(lambda c: c["books"]["x"].update({"state": "zombie"}), "state")[0], None))
    cases.append(("负例：`state=retired` 却不写 `why`（为什么退休）→ 报错",
                  _bad(lambda c: c["books"]["x"].update({"state": "retired"}), "why")[0], None))
    # A6：`notice`（可选字符串）—— 交付说明的「出处与分发」提示；**只打印、不参与任何判据**
    cases.append((("可选字段 `notice` 透传到行里（A6：交付说明打印分发提示）" if b else
                   "可选字段 `notice`：**注册表为空** → 明示跳过"),
                  (all(isinstance(c.get("notice"), str) for c in b.values()) if b else True),
                  [k for k, c in b.items() if "notice" in c]))
    cases.append(("可选字段 `state` 透传到行里（生命周期；默认 active）",
                  all(c.get("state") in STATES for c in (list(b.values()) + list(smp.values()))),
                  sorted({c.get("state") for c in b.values()})))
    cases.append(("负例：`notice` 不是字符串 → 报错",
                  _bad(lambda c: c["books"]["x"].update({"notice": 3}), "notice")[0], None))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:140]))
        passed += bool(ok)
    print("\n%d/%d books tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def _missing_raises() -> bool:
    try:
        load(os.path.join(HERE, "no-such-books.json"))
        return False
    except BookRegistryError:
        return True


def cmd_list(_a) -> int:
    p = registry_path()
    if not os.path.exists(p):
        print("还没有书注册表：", p)
        print("（新机器正常状态；按", os.path.basename(sample_path()), "的形状用 intake.py add 建第一本）")
        return 0
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    print("注册表：", p)
    print("数据根：", ROOT)
    for name, cfg in sorted(data["books"].items()):
        print("  %-14s key=%-18s [%s] %s" % (name, cfg["key"], cfg.get("state", "active"), cfg["label"]))
        for f in ("pdf", "mineru_dir", "paddle_dir", "profile"):
            print("        %-11s %s" % (f, cfg[f]))
        if "page_labels" in cfg:
            print("        %-11s %s" % ("page_labels", cfg["page_labels"]))
    return 0


def cmd_check(_a) -> int:
    b = load()
    miss = validate_existence(b)
    print(json.dumps({"books": len(b), "missing": miss, "ok": not miss}, ensure_ascii=False, indent=2))
    return 0 if not miss else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
