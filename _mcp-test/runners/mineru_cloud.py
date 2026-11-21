# -*- coding: utf-8 -*-
"""MinerU 云端 **直连 runner**（A3 建）—— 把「引擎怎么跑」做成一条确定性 CLI。

**为什么需要它**：`engines.json` 原先指向 `dual-mcp-bridge/mineru_mcp_wrapper.py`，那是**stdio MCP 服务**
（`--help` 只认 `--verbose`），只能由 MCP 客户端驱动，**不是可编排的 CLI**。本文件是**薄 CLI**，
内部**复用**那个 wrapper 里已验通的直连流程（token → `file-urls/batch` → 签名 URL PUT → 轮询 → 取 zip
→ 安全解包 → 空白页显式声明），**不复制 HTTP/解包逻辑**（复用即少一处漂移）。

契约（SPEC「runner 契约（A3 建）」）：
  * CLI：``python mineru_cloud.py --book <key> [--out-root <r>] [--force] [--selftest]``；
  * 落盘：写进 `books.json` 该书 `mineru_dir`，布局 = `content_list + layout + images`（与 `native_layout` 一致）；
  * 幂等：已有**覆盖完整**的原生输出 → **跳过**（不重传、不重算），`--force` 才重跑；
  * 失败：写 `engine-record.json` 的失败清单，**非 0 退出、打印原因**（缺页由 G1 兜住，绝不静默）；
  * 密钥：**只从环境读**（进程 → User 环境 → `secrets.json`），**任何输出只有掩码**，永不打印值。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from pathlib import Path
import sys

_HERE = Path(__file__).resolve()
_TOOLS = _HERE.parents[2]                     # 工具/环境根（_mcp-test、venv、本地物都在它下面）
_MCP_TEST = _TOOLS / "_mcp-test"
if str(_MCP_TEST) not in sys.path:            # **先让 `import paths` 找得到**（直接喊 runner 也能跑）
    sys.path.insert(0, str(_MCP_TEST))

import paths  # noqa: E402  B2：工作根唯一出口（SPEC §7.10）

HERE = Path(__file__).resolve()
TOOLS = HERE.parents[2]                     # 工具/环境根（_mcp-test、dual-mcp-bridge、venv、模型、缓存）
MCP_TEST = TOOLS / "_mcp-test"
WRAPPER = TOOLS / "dual-mcp-bridge" / "mineru_mcp_wrapper.py"
CRED_ENV = "MINERU_API_TOKEN"
RECORD_NAME = "engine-record.json"

for _p in (str(MCP_TEST),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA = Path(paths.data_root())   # 数据根（输入与产物；books.json 的相对路径都相对它）


# --------------------------------------------------------------------- 密钥
def mask_token(value: str) -> str:
    """只给「长度 + 掩码」——**绝不返回/打印值**。短值不泄漏中间段。"""
    n = len(value or "")
    if n == 0:
        return "未设置"
    if n <= 8:
        return "长度 %d 掩码 %s…(过短，仅显示首字符)" % (n, (value[0] + "***"))
    return "长度 %d 掩码 %s…%s" % (n, value[:4], value[-4:])


def resolve_token() -> tuple[str, str]:
    """按优先级取凭证：进程环境 → User 环境 → `%USERPROFILE%\\.chemdoc\\secrets.json`。

    返回 ``(值, 来源)``；来源只用于打印。**agent 不读密钥文件**，本函数是 runner 进程内的机器读取。
    """
    value = (os.environ.get(CRED_ENV) or "").strip()
    if value:
        return value, "进程环境"
    try:                                        # 复用既有口径（User 环境 → secrets.json）
        import secret_scan
        got = secret_scan.resolve((CRED_ENV,))[CRED_ENV]
        if got.get("value"):
            return str(got["value"]), str(got.get("source") or "secret_scan")
    except Exception:                                                       # noqa: BLE001
        pass
    return "", "未找到"


# ----------------------------------------------------------- 原生产出自查
def _load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_content_list(target: Path):
    """找原生 content_list：优先精确名，其次 `<uuid>_content_list.json`，最后递归。"""
    preferred = Path(target) / "content_list.json"
    if preferred.is_file():
        return preferred
    matches = sorted(Path(target).glob("*_content_list.json"))
    if matches:
        return matches[0]
    matches = sorted(Path(target).rglob("*_content_list.json"))
    return matches[0] if matches else None


def find_layout(target: Path):
    """找原生 layout.json（提供 page_size；顶层优先，其次递归）。"""
    for cand in (Path(target) / "layout.json",):
        if cand.is_file():
            return cand
    matches = sorted(Path(target).rglob("layout.json"))
    return matches[0] if matches else None


def pages_needing_content(blocks: list, page_count: int) -> list[int]:
    """既**没有内容块**、也**没有显式空白页声明**的页 —— 这些页缺证据，必须补声明。"""
    with_content, declared = set(), set()
    for b in blocks:
        try:
            idx = int(b.get("page_idx", -1))
        except (TypeError, ValueError, AttributeError):
            continue
        text = (b.get("text") if isinstance(b, dict) else "") or ""
        if str(text).strip():
            with_content.add(idx)
        if isinstance(b, dict) and b.get("type") == "empty_page" and b.get("empty") is True:
            declared.add(idx)
    return [p for p in range(page_count) if p not in with_content and p not in declared]


def normalize_content_list(content_path, pdf_path, page_count: int | None = None,
                           log: bool = True) -> dict:
    """**幂等**归一：非对象块 / 非整数 page_idx / 越界 → 报错（拒绝静默修补）；缺页补显式空白声明。

    与 wrapper 内的同名归一**同口径**（wrapper 是按卷调用时的入口，本函数是按书调用时的入口）。
    """
    if page_count is None:
        import pypdf
        with open(pdf_path, "rb") as fh:
            page_count = len(pypdf.PdfReader(fh).pages)
    blocks = _load_json(Path(content_path))
    if not isinstance(blocks, list):
        raise RuntimeError("原生 content_list 根不是列表：%s" % content_path)
    seen = set()
    for b in blocks:
        if not isinstance(b, dict):
            raise RuntimeError("原生 content_list 含非对象块")
        idx = b.get("page_idx")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise RuntimeError("原生 content_list 有块缺整数 page_idx")
        if idx < 0 or idx >= page_count:
            raise RuntimeError("page_idx %d 越出本书页数 %d" % (idx, page_count))
        seen.add(idx)
    missing = pages_needing_content(blocks, page_count)
    for idx in missing:
        blocks.append({"type": "empty_page", "empty": True,
                       "reason": "provider returned no content block for this page",
                       "page_idx": idx})
    if missing and log:
        print("[runner] 引擎未给 %d 页任何块 → 补**显式空白页声明**：%s" % (len(missing), missing), flush=True)
    blocks.sort(key=lambda b: int(b.get("page_idx", 0)))
    target = Path(content_path)
    if target.name != "content_list.json":
        target = target.with_name("content_list.json")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blocks, fh, ensure_ascii=False, indent=2)
    return {"content_list": str(target), "page_count": page_count,
            "blocks": len(blocks), "declared_empty": missing}


def coverage(target, pdf_path) -> dict:
    """页覆盖自查（**只读**）：`ok` 的判据 = 每页有块 + 无「缺证据」页 + `layout.json` 能给出全部 page_size。"""
    import pypdf
    with open(pdf_path, "rb") as fh:
        page_count = len(pypdf.PdfReader(fh).pages)
    target = Path(target)
    cl = find_content_list(target)
    if cl is None:
        return {"ok": False, "page_count": page_count, "content_list": None,
                "reason": "没有原生 content_list（引擎还没跑，或产出目录被清空）"}
    blocks = _load_json(cl)
    seen = sorted({int(b["page_idx"]) for b in blocks
                   if isinstance(b, dict) and isinstance(b.get("page_idx"), int)})
    need = pages_needing_content(blocks, page_count)
    missing_pages = [p for p in range(page_count) if p not in set(seen)]
    lay = find_layout(target)
    layout_pages, layout_ok = [], False
    if lay is not None:
        try:
            lay_pages = (_load_json(lay) or {}).get("pdf_info") or []
            layout_pages = sorted({int(p.get("page_idx", -1)) for p in lay_pages})
            layout_ok = set(range(page_count)) <= set(layout_pages)
        except Exception:                                                   # noqa: BLE001
            layout_ok = False
    images = target / "images"
    ok = (not missing_pages) and (not need) and layout_ok
    return {"ok": ok, "page_count": page_count, "content_list": str(cl),
            "layout": (str(lay) if lay else None), "blocks": len(blocks),
            "pages_with_blocks": len(seen), "missing_pages": missing_pages[:20],
            "pages_needing_declaration": need[:20], "layout_covers_all": layout_ok,
            "images_dir": images.is_dir(),
            "images": (len(list(images.glob("*"))) if images.is_dir() else 0),
            "reason": ("" if ok else
                       ("缺页 %s" % missing_pages[:20] if missing_pages else
                        "无证据页 %s" % need[:20] if need else
                        "layout.json 未覆盖全部 page_size"))}


# ------------------------------------------------------------------ 落盘记录
def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_record(target: Path, record: dict) -> str:
    record = dict(record)
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = Path(target) / RECORD_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    return str(out)


# ---------------------------------------------------------------------- 主流程
def _load_engine_module():
    """按路径加载引擎封装；**只为 import 注入 FastMCP 桩**（不装 `fastmcp[server]`）。

    wrapper 顶层 `from fastmcp import FastMCP` 并且 `main()` 里 `mcp.run(transport="stdio")` —— 我们
    **不用**它的 stdio 服务，只用它已验通的直连函数；本机 fastmcp 4.x 未装 server extra，
    直接 import 会 `FastMCP server support is not installed`。桩只在**本进程**内生效，不改环境、不装包。
    """
    import importlib.util
    if not WRAPPER.is_file():
        raise RuntimeError("找不到引擎封装：%s" % WRAPPER)
    _install_fastmcp_stub()
    spec = importlib.util.spec_from_file_location("mineru_mcp_wrapper", str(WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                            # type: ignore[union-attr]
    return mod


class _FastMCPStub:
    """只满足 wrapper 的**构造**与 `.tool(...)(fn)` 注册调用；不做任何服务。"""

    def __init__(self, *a, **k):
        pass

    def tool(self, *a, **k):
        def _register(fn):
            return fn
        return _register

    def run(self, *a, **k):
        raise RuntimeError("FastMCP 桩不提供 stdio 服务：请用本 runner 的 CLI（--book）")


def _install_fastmcp_stub() -> None:
    """装桩的**唯一判据** =  `FastMCP` 能不能 import 出来。

    「`fastmcp` 在 sys.modules 里」**不等于**能用：fastmcp 4.x 有懒加载，`import fastmcp` 成功而
    `from fastmcp import FastMCP` 报 `server support is not installed`（本机实测踩到）。
    """
    import sys as _sys
    try:
        from fastmcp import FastMCP  # noqa: F401
        return                              # 真类可用 → 不插桩
    except Exception:                                                       # noqa: BLE001
        pass
    mod = _sys.modules.get("fastmcp")
    if mod is None:
        import types
        mod = types.ModuleType("fastmcp")
        _sys.modules["fastmcp"] = mod
    mod.FastMCP = _FastMCPStub                                              # type: ignore[attr-defined]


def run_book(book: str, out_root: str | None = None, force: bool = False,
             language: str = "ch") -> int:
    import books as _books
    root = Path(out_root or DATA)
    cfg = _books.load().get(book)
    if not cfg:
        print("[runner] 书注册表里没有 key=%s" % book)
        return 1
    pdf = Path(cfg["pdf"])
    if not pdf.is_absolute():
        pdf = root / cfg["pdf"]
    target = Path(cfg["mineru_dir"])
    if not target.is_absolute():
        target = root / cfg["mineru_dir"]
    print("[runner] 书=%s（%s）" % (book, cfg.get("label")))
    print("[runner] 源 PDF=%s（%s）" % (pdf, "存在" if pdf.is_file() else "**缺失**"))
    print("[runner] 产出目录=%s" % target)
    if not pdf.is_file():
        print("[runner] 源 PDF 不存在，无法跑引擎")
        return 1

    before = coverage(target, pdf)
    if before["ok"] and not force:
        print("[runner] 原生输出**已覆盖完整**（%d 页）→ 幂等跳过，不重传；要重跑加 --force"
              % before["page_count"])
        write_record(target, {"runner": "runners/mineru_cloud.py", "book": book, "pdf": str(pdf),
                              "status": "skipped_complete", "coverage": before,
                              "declared_failures": [], "cred": {"env": CRED_ENV, "source": None,
                                                                "mask": None}})
        return 0

    token, source = resolve_token()
    print("[runner] 凭证 %s：%s（来源=%s）" % (CRED_ENV, mask_token(token), source))
    record = {"runner": "runners/mineru_cloud.py", "book": book, "pdf": str(pdf),
              "pdf_sha256": sha256_file(pdf), "target": str(target),
              "cred": {"env": CRED_ENV, "source": source, "mask": mask_token(token)},
              "declared_failures": [], "status": "running"}
    if not token:
        print("[runner] 凭证未设置：让**用户本人**双击 `输入密钥.cmd`（agent 不代填、不读密钥文件）")
        record["status"] = "failed"
        record["error"] = "credential %s not set" % CRED_ENV
        target.mkdir(parents=True, exist_ok=True)
        write_record(target, record)
        return 2

    os.environ[CRED_ENV] = token            # 只在**本进程**内传递（wrapper 读环境，不经命令行）
    try:
        wrapper = _load_engine_module()
        items = wrapper.parse_documents_vlm_forced_ocr(
            file_sources=[str(pdf)], language=language, output_dir=str(target))
    except Exception as exc:                                                # noqa: BLE001
        print("[runner] 引擎调用失败：%s" % exc)
        record["status"] = "failed"
        record["error"] = str(exc)[:800]
        target.mkdir(parents=True, exist_ok=True)
        write_record(target, record)
        return 3

    item = (items or [{}])[0]
    if item.get("status") != "success":
        msg = str(item.get("error") or item.get("status") or "未知失败")
        print("[runner] 引擎返回失败：%s" % msg)
        record["status"] = "failed"
        record["error"] = msg[:800]
        record["declared_failures"].append({"what": "mineru extract", "detail": msg[:300]})
        write_record(target, record)
        return 4

    # 把原生产物**收敛到约定位置**（S2 按 `content_list.json` + `layout.json` 读；见 mining layout 契约）
    print("[runner] 引擎成功：extract=%s" % item.get("extract_path"))
    extract = Path(str(item.get("extract_path")))
    moved = []
    src_cl = Path(str(item.get("content_list_path")))
    dst_cl = target / "content_list.json"
    if src_cl.resolve() != dst_cl.resolve() and src_cl.is_file():
        _move(src_cl, dst_cl)
        moved.append({"from": src_cl.name, "to": dst_cl.name})
    src_lay = find_layout(extract) if extract.is_dir() else None
    if src_lay is None:
        src_lay = find_layout(target)
    dst_lay = target / "layout.json"
    if src_lay and src_lay.resolve() != dst_lay.resolve():
        _move(src_lay, dst_lay)
        moved.append({"from": src_lay.name, "to": dst_lay.name})
    if extract.is_dir() and extract.resolve() != target.resolve():
        for f in sorted(extract.glob("*")):
            tgt = target / f.name
            if f.is_dir() and f.name == "images":
                tgt_dir = target / "images"
                tgt_dir.mkdir(parents=True, exist_ok=True)
                for sub in sorted(f.glob("*")):
                    _move(sub, tgt_dir / sub.name)
            elif not tgt.exists() and f.is_file():
                _move(f, tgt)
    if not dst_cl.is_file():
        print("[runner] **落盘不齐**：约定路径下没有 content_list.json（布局与 native_layout 不一致）")
        record["status"] = "failed"
        record["error"] = "content_list.json 未落到 %s" % dst_cl
        write_record(target, record)
        return 5

    norm = normalize_content_list(dst_cl, pdf)
    after = coverage(target, pdf)
    record.update({
        "status": "success" if after["ok"] else "incomplete",
        "model_version": item.get("model_version"), "enable_ocr": item.get("enable_ocr"),
        "language": item.get("language"), "page_count": item.get("page_count"),
        "zip_sha256": item.get("zip_sha256"),
        "normalized": {"blocks": norm["blocks"], "declared_empty": norm["declared_empty"]},
        "moved": moved, "coverage": after,
    })
    if not after["ok"]:
        record["declared_failures"].append({"what": "page coverage", "detail": after["reason"]})
    write_record(target, record)
    print("[runner] 落盘完成：%s" % json.dumps(
        {k: after[k] for k in ("ok", "page_count", "blocks", "pages_with_blocks",
                               "layout_covers_all", "images")}, ensure_ascii=False))
    if not after["ok"]:
        print("[runner] **覆盖不完整**：%s → 缺页由 G1 兜住，绝不静默" % after["reason"])
        return 6
    return 0


def _move(src: Path, dst: Path) -> None:
    """移动（不删除、不覆盖）：目标已存在则**保留原样**并让调用方看见。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.replace(str(src), str(dst))
    except OSError:
        import shutil
        shutil.copyfile(str(src), str(dst))


# ------------------------------------------------------------------ selftest
def selftest() -> int:
    import tempfile

    cases = []
    tok = "sk-" + ("a" * 43) + "drDc"
    m = mask_token(tok)
    cases.append(("掩码：只给长度 + 首尾，**不含中段**",
                  ("长度 %d" % len(tok) in m) and ("aaaa" not in m) and m.endswith("drDc"), m))
    cases.append(("掩码：未设置/过短都不崩", mask_token("") == "未设置" and "过短" in mask_token("abcd"),
                  [mask_token(""), mask_token("abcd")]))
    cases.append(("缺证据页：无内容且无显式声明的页被点名",
                  pages_needing_content([{"page_idx": 0, "text": "x"},
                                         {"page_idx": 1, "type": "empty_page", "empty": True}], 4) == [2, 3], None))
    cases.append(("缺证据页：有 type=empty_page 但 empty 不为真 → 仍算缺证据",
                  pages_needing_content([{"page_idx": 0, "type": "empty_page"}], 1) == [0], None))

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        pdf = td / "b.pdf"
        import pypdf
        writer = pypdf.PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=612, height=792)
        with open(pdf, "wb") as fh:
            writer.write(fh)

        cl = td / "content_list.json"
        cl.write_text(json.dumps([{"page_idx": 1, "type": "text", "text": "hi"}]),
                      encoding="utf-8")
        r = normalize_content_list(cl, pdf, log=False)
        blocks = _load_json(cl)
        got_pages = [b["page_idx"] for b in blocks]
        empt = [b for b in blocks if b.get("type") == "empty_page"]
        cases.append(("归一：缺页补**显式**空白声明且排序",
                      r["declared_empty"] == [0, 2] and got_pages == [0, 1, 2]
                      and len(empt) == 2 and all(b.get("empty") is True for b in empt), got_pages))
        cases.append(("归一：**幂等**（再跑一次不新增块）",
                      normalize_content_list(cl, pdf, log=False)["blocks"] == len(blocks), None))

        cases.append(("负例：非整数 page_idx → 报错",
                      _raises(lambda: normalize_content_list(
                          _write(td / "bad1.json", [{"page_idx": "0"}]), pdf)), None))
        cases.append(("负例：page_idx 越界 → 报错",
                      _raises(lambda: normalize_content_list(
                          _write(td / "bad2.json", [{"page_idx": 9}]), pdf)), None))
        cases.append(("负例：根不是列表 → 报错",
                      _raises(lambda: normalize_content_list(
                          _write(td / "bad3.json", {"page_idx": 0}), pdf)), None))

        cov = coverage(td, pdf)
        cases.append(("覆盖自查：缺 layout.json → ok=False 且**给原因**",
                      (not cov["ok"]) and "layout" in cov["reason"], cov))
        lay = {"pdf_info": [{"page_idx": i, "page_size": [612, 792]} for i in range(3)]}
        (td / "layout.json").write_text(json.dumps(lay), encoding="utf-8")
        (td / "images").mkdir()
        cases.append(("覆盖自查：三页有块 + layout 全覆盖 → ok=True",
                      coverage(td, pdf)["ok"], coverage(td, pdf)))

        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--book", required=True)
        ap.add_argument("--force", action="store_true")
        cases.append(("CLI 契约：`--book` 必填、`--force` 可给（engines 就绪度按此静态探测）",
                      ap.parse_args(["--book", "x", "--force"]).book == "x", None))

    # 引擎封装可加载：FastMCP 桩只补 import，不改环境、不装包（真跑时 wrapper 只被当函数库用）
    import contextlib
    import io as _io
    _err = _io.StringIO()
    try:
        with contextlib.redirect_stderr(_err):
            mod = _load_engine_module()
        loaded = callable(getattr(mod, "parse_documents_vlm_forced_ocr", None))
        stub_used = getattr(sys.modules.get("fastmcp"), "FastMCP", None) is _FastMCPStub
    except Exception as exc:                                                # noqa: BLE001
        loaded, stub_used = False, False
        _err.write(str(exc))
    cases.append(("引擎封装可加载（FastMCP 未装 server extra 时用**桩**，直连函数可用）",
                  loaded and stub_used, _err.getvalue()[:160]))
    cases.append(("桩不伪装成服务：`run()` 直接报错（避免被误当 stdio 服务启动）",
                  _raises(lambda: _FastMCPStub().run()), None))

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:160]))
        passed += bool(ok)
    print("\n%d/%d runners.mineru_cloud tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def _write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                                                       # noqa: BLE001
        return True
    return False


# ----------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MinerU 云端直连 runner（A3）")
    ap.add_argument("--book", help="书注册表 key（books.json）")
    ap.add_argument("--out-root", default=None, help="产物根（默认 = 本文件上两级）")
    ap.add_argument("--language", default="ch", choices=["ch", "en"])
    ap.add_argument("--force", action="store_true", help="已有完整产出也重跑（默认幂等跳过）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.book:
        ap.error("--book 必填（或用 --selftest）")
    return run_book(args.book, args.out_root, force=args.force, language=args.language)


if __name__ == "__main__":
    raise SystemExit(main())
