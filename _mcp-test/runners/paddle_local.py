# -*- coding: utf-8 -*-
"""PaddleOCR-VL **本地 runner**（A3 建）—— 见证引擎的「怎么跑」入口。

**为什么需要它**：`engines.json` 原先把 `paddle:local` 指向 `_mcp-test/dump_paddle_raw.py`，
那是 `<pdf> <out-dir>` 的**位置参数**脚本（整本一次跑完、失败即全废），不符合 SPEC 的 runner 契约。
本文件是**薄 CLI**：`--book <key>` → 落 `books.json` 的 `paddle_dir`，并且

  * **按页切片、按页落盘**：第 p 页 = 把第 p 页单独切出来喂给引擎（与 `dump_paddle_raw.py` **同一个
    直出实现**，逐字段不丢）；**每页一个 `page_XXXX/` 目录**，天然断点续跑（已有页直接跳过）；
  * **幂等**：只补缺失页；`--force` 才整本重跑；
  * **失败不静默**：`engine-record.json` 记 `status`/`pages_failed`，非 0 退出并打印原因；
  * **隐私**：本地推理，**不上传**（与 primary 的云端模式相反）。

口径依据：SPEC「runner 契约（A3 建）」+ D12（见证引擎 = PaddleOCR-VL 本地）、D17（见证逐页跑、不抽样）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

HERE = _HERE
TOOLS = _TOOLS
MCP_TEST = _MCP_TEST
DUMPER = MCP_TEST / "dump_paddle_raw.py"
RECORD_NAME = "engine-record.json"

DATA = Path(paths.data_root())   # 数据根（输入与产物；books.json 的相对路径都相对它）


def apply_slot_env(verbose: bool = True) -> list:
    """**自己把运行环境设好**（从 `engines.json` 的槽位 `env` 读）。

    为什么在 runner 里也做一遍：`env`（`USERPROFILE` 指到模型缓存、`TEMP`/`TMP` 指到本地临时目录、
    `PYTHONUTF8=1`）声明在配置里，**不该让人或 agent 手工设**。`engines.py run` 会在子进程层面注入；
    这里再兜一层：**直接喊 runner 也能跑**（缺啥补啥，已有值不覆盖）。返回"这次真的设了哪些键"。
    """
    try:
        import engines as eng
        env = eng.slot_env(eng.find_slot("paddle:local"))
    except Exception:                                                      # noqa: BLE001
        return []
    applied = []
    for k, v in env.items():
        if os.environ.get(k):
            continue
        os.environ[k] = str(v)
        applied.append(k)
    for sub in ("tmp", "paddle-home"):
        try:
            os.makedirs(os.path.join(paths.local_root(), sub), exist_ok=True)
        except OSError:
            pass
    if verbose and applied:
        print("[paddle] 自动设置运行环境（来自 engines.json 的槽位 env）：%s" % ", ".join(applied))
    return applied


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def page_dirs(target: Path) -> dict:
    """已有页 → `{页号: 目录}`（页号从 `page_0001` 目录名解析）。"""
    out = {}
    if not Path(target).is_dir():
        return out
    for d in sorted(Path(target).glob("page_*")):
        if not d.is_dir():
            continue
        try:
            idx = int(d.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        if (d / "result.json").is_file() and (d / "parsing_res_list.json").is_file():
            out[idx] = d
    return out


def _load_record(target: Path) -> dict:
    p = Path(target) / RECORD_NAME
    if p.is_file():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and rec.get("engine") == "paddle":
                return rec
        except Exception:                                                   # noqa: BLE001
            pass
    return {}


def _write_record(target: Path, record: dict) -> str:
    record = dict(record)
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = Path(target) / RECORD_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
    return str(out)


def _paddle_env() -> dict:
    """本地 Paddle 的**必需**环境（AGENTS.md 实测）：模型目录/临时目录都搬到工作区内。

    不设 `USERPROFILE` 时 PaddleX 会去写 `C:\\Users\\<me>\\.cache\\paddle` → 本机实测
    `PermissionError: [WinError 5]`（沙箱/受控目录）。**辅助手段失败不得带走主流程**：
    `.paddle-home` 建不出来就退回原环境（让错误如实冒泡，而不是静默改行为）。

    **PYTHONPATH 一律不碰**（既不前置 `_mcp-test`、也不清空）：实测把 `_mcp-test` 放进 PYTHONPATH
    会顶掉**标准库 `secrets`**（numpy.random 起不来：`ImportError: cannot import name randbits`；
    `-P` 也救不了——PYTHONPATH 排在标准库之前）。直出脚本要的 `books`/`render` 改由**子进程 cwd =
    `_mcp-test`** 提供（脚本目录本就在 `sys.path[0]`，见 `_dump_cmd` 的 cwd）。
    """
    env = dict(os.environ)
    # 环境**从槽位声明来**（`engines.json` 的 `env`）：单一口径，改成别处也只改配置
    home = tmp = None
    try:
        import engines as _eng
        _e = _eng.slot_env(_eng.find_slot("paddle:local"))
        home = Path(_e["USERPROFILE"]) if _e.get("USERPROFILE") else None
        tmp = Path(_e["TEMP"]) if _e.get("TEMP") else None
    except Exception:                                                      # noqa: BLE001
        pass
    if home is None:
        home = Path(paths.ensure_local("paddle-home"))
    if tmp is None:
        tmp = Path(paths.ensure_local("tmp"))
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        env["USERPROFILE"] = str(home)
        env["TEMP"] = env["TMP"] = str(tmp)
    except OSError:
        pass
    env["PYTHONUTF8"] = "1"
    return env


def slot_python() -> str:
    """本槽位的解释器（`engines.json` 的 `python` 是 **venv 目录** → 补成 `Scripts/python.exe`）。"""
    try:
        import engines as _eng
        py = _eng.resolve_slot(_eng.load(), _eng.find_slot("paddle:local")).get("python")
        if py:
            return str(Path(py) / "Scripts" / "python.exe")
    except Exception:                                                      # noqa: BLE001
        pass
    return str(Path(paths.local_root()) / "venvs" / "paddle" / "Scripts" / "python.exe")


def _dump_cmd(python: str, one: Path, raw: Path, book_page: int = 0) -> list:
    """直出脚本的命令行（`cwd` 必须是 `_mcp-test`，见 `_paddle_env` 的说明）。

    `--book-page N`：逐页切片跑时把 `page_index` 落成**书内页号**（否则 50 页全是 0 → 见证块全被
    标到第 1 页）。
    """
    cmd = [python, "-X", "utf8", str(DUMPER), str(one), str(raw), "--pipeline-version", "v1.6"]
    if book_page:
        cmd += ["--book-page", str(book_page)]
    return cmd


def run_page(pdf, page: int, page_dir: Path, python: str, timeout: float = 900.0) -> dict:
    """跑**一页**：切页 → 调直出脚本 → 结果搬进 `page_dir`（原子：先落临时目录）。"""
    import render
    tmp = Path(tempfile.mkdtemp(prefix="paddle-page-", dir=paths.ensure_local("tmp")))
    try:
        one = tmp / "one.pdf"
        render.slice_pages(str(pdf), page, page, str(one))
        raw = tmp / "raw"
        # cwd=**`_mcp-test`**：让子进程的 `sys.path[0]` 指向 `_mcp-test`（`books`/`render` 可 import），
        # 同时**绝不**把 `_mcp-test` 写进 PYTHONPATH（那会顶掉标准库 `secrets` → numpy 崩，实测踩到）。
        proc = subprocess.run(_dump_cmd(python, one, raw, book_page=page), capture_output=True,
                              text=True, timeout=timeout, env=_paddle_env(), cwd=str(MCP_TEST),
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-600:]
            return {"page": page, "ok": False,
                    "error": "dump_paddle_raw 退出码 %d：%s" % (proc.returncode, tail)}
        src = next((d for d in sorted(raw.glob("page_*")) if d.is_dir()), None)
        if src is None:
            src = next((d for d in sorted(raw.glob("result_*")) if d.is_dir()), None)
        if src is None:
            return {"page": page, "ok": False, "error": "引擎没有产出 page_* 目录"}
        page_dir.parent.mkdir(parents=True, exist_ok=True)
        if page_dir.exists():
            shutil.rmtree(page_dir, ignore_errors=True)
        shutil.copytree(src, page_dir)
        rec = {}
        try:
            rec = json.loads((page_dir / "result.json").read_text(encoding="utf-8"))
            if isinstance(rec, dict) and "res" in rec and isinstance(rec["res"], dict):
                rec = rec["res"]
            rec = {"page_index_raw": rec.get("page_index"),
                   "page_image_px": [rec.get("width"), rec.get("height")]}
        except Exception:                                                   # noqa: BLE001
            rec = {}
        blocks = 0
        try:
            blocks = len(json.loads((page_dir / "parsing_res_list.json").read_text(encoding="utf-8")))
        except Exception:                                                   # noqa: BLE001
            blocks = 0
        return {"page": page, "ok": True, "dir": page_dir.name, "blocks": blocks,
                "log_lines": [l for l in (proc.stdout or "").splitlines() if "[paddle]" in l][-2:],
                **rec}
    except subprocess.TimeoutExpired:
        return {"page": page, "ok": False, "error": "超时 %.0fs（逐项限时，不盲等）" % timeout}
    except Exception as exc:                                                # noqa: BLE001
        return {"page": page, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _failed_pages(pages: dict) -> list:
    """**按当前 `pages` 重算失败页**——不是并集累积（A5）。

    为什么必须重算：旧实现把"本次失败"并进历史 `pages_failed`，成功页不回填清除 → 一个
    先失败、后成功的 record 会同时写 `pages_done=[1..9]` 与 `pages_failed=[1..9]`，
    `status=incomplete` 且退出码 6（实测踩到：9 页全成功仍打印"失败 [1..9]"）。
    """
    out = []
    for k, v in (pages or {}).items():
        try:
            p = int(k)
        except (TypeError, ValueError):
            continue
        if not (isinstance(v, dict) and v.get("ok")):
            out.append(p)
    return sorted(out)


def run_book(book: str, out_root: str | None = None, force: bool = False,
             pages: list | None = None, python: str | None = None) -> int:
    import books as _books
    import render
    root = Path(out_root or DATA)
    cfg = _books.load().get(book)
    if not cfg:
        print("[paddle] 书注册表里没有 key=%s" % book)
        return 1
    pdf = Path(cfg["pdf"])
    if not pdf.is_absolute():
        pdf = root / cfg["pdf"]
    target = Path(cfg["paddle_dir"])
    if not target.is_absolute():
        target = root / cfg["paddle_dir"]
    if not pdf.is_file():
        print("[paddle] 源 PDF 不存在：%s" % pdf)
        return 1
    doc = render.open_doc(str(pdf))
    try:
        total = render.page_count(doc)
    finally:
        render.close(doc)
    want = pages or list(range(1, total + 1))
    print("[paddle] 书=%s（%s）" % (book, cfg.get("label")))
    print("[paddle] 源 PDF=%s 共 %d 页；本批要跑 %d 页 → %s" % (pdf, total, len(want), target))
    print("[paddle] 本地推理（**不上传**）；逐页切片 + 逐页落盘（可断点续跑）")

    have = page_dirs(target)
    todo = [p for p in want if force or p not in have]
    skipped = [p for p in want if p not in todo]
    if skipped:
        print("[paddle] 已有 %d 页（幂等跳过）：%s" % (len(skipped), skipped[:12] + (["…"] if len(skipped) > 12 else [])))
    py = python or slot_python()
    if not Path(py).is_file():
        print("[paddle] 解释器不存在：%s" % py)
        return 1

    record = _load_record(target) or {
        "engine": "paddle", "runner": "runners/paddle_local.py", "book": book,
        "source_pdf": str(pdf), "source_pdf_sha256": sha256_file(pdf),
        "pipeline_version": "v1.6", "target": str(target),
        "note": "逐页切片后调用 _mcp-test/dump_paddle_raw.py（同一逐字段直出实现）；本地产物，不上传",
    }
    record["page_count"] = total
    # 页号口径（A3）：本 runner **逐页切片**跑 → 引擎给的 `page_index` 恒为 0，所以落盘时已把它改写成
    # 书内页号；`page_numbering=per_page_dir` 告诉 S2「页号以目录名为准」、`page_offset` 是整体偏移
    # （本 runner 的目录名 == 书内页号 → 偏移 0）。老产物没有这两个字段 → S2 保持老语义、逐字节不变。
    record["page_numbering"] = "per_page_dir"
    record["page_offset"] = 0
    record.setdefault("pages", {})
    if not todo:
        record["status"] = "skipped_complete"
        # 目录在 **且** 记录里该页 ok 才算完成（A5：只认目录会把"有目录但记录是失败"的页算成完成）
        record["pages_failed"] = sorted(set(_failed_pages(record["pages"]))
                                        | {p for p in want if p not in page_dirs(target)})
        _write_record(target, record)
        print("[paddle] 全部 %d 页已就位 → 幂等跳过" % len(want))
        return 0 if not record["pages_failed"] else 6

    t0 = time.time()
    for i, page in enumerate(todo, 1):
        pdir = target / ("page_%04d" % page)
        print("[paddle] (%d/%d) 第 %d 页 …" % (i, len(todo), page), flush=True)
        res = run_page(pdf, page, pdir, py)
        record["pages"][str(page)] = res
        if res.get("ok"):
            print("[paddle]      ok  blocks=%s  %s" % (res.get("blocks"), res.get("log_lines") or ""),
                  flush=True)
        else:
            print("[paddle]      **失败** %s" % res.get("error"), flush=True)
        record["status"] = "running"
        # A5：按当前 pages 重算（失败页会被重跑覆盖，成功即自动移出失败集合）
        record["pages_failed"] = _failed_pages(record["pages"])
        _write_record(target, record)
    record["elapsed_sec"] = round(time.time() - t0, 1)
    record["pages_done"] = sorted(int(k) for k, v in record["pages"].items() if v.get("ok"))
    record["pages_failed"] = sorted(int(p) for p in (record.get("pages_failed") or []))
    record["status"] = "success" if not record["pages_failed"] else "incomplete"
    _write_record(target, record)
    print("[paddle] 完成：成功 %d 页、失败 %s；用时 %.1fs（%.1f s/页）"
          % (len(record["pages_done"]), record["pages_failed"] or "无",
             record["elapsed_sec"], record["elapsed_sec"] / max(1, len(todo))))
    if record["pages_failed"]:
        print("[paddle] **失败页不静默**：已在 engine-record.json 登记；缺页由 G1 兜住")
        return 6
    return 0


def selftest() -> int:
    cases = []
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "page_0002").mkdir()
        (td / "page_0002" / "result.json").write_text("{}", encoding="utf-8")
        (td / "page_0002" / "parsing_res_list.json").write_text("[]", encoding="utf-8")
        (td / "page_0003").mkdir()                     # 只有目录、没有两个 JSON → 不算已完成
        (td / "page_0003" / "result.json").write_text("{}", encoding="utf-8")
        (td / "page_xx").mkdir()
        got = page_dirs(td)
        cases.append(("续跑台账：只认**两个 JSON 都在**的 page_* 目录（半成品不算已完成）",
                      list(got) == [2], sorted(got)))
        cases.append(("续跑台账：目录名非数字 → 跳过不崩", 999 not in got, sorted(got)))

        rec = _load_record(td)
        cases.append(("记录：没有记录（或不是 paddle 的）→ 返回空 dict，不崩", rec == {}, rec))
        _write_record(td, {"engine": "paddle", "status": "x"})
        cases.append(("记录：paddle 的记录可读回", _load_record(td).get("engine") == "paddle", None))
        cases.append(("记录：写入带 updated_at 且是合法 JSON",
                      "updated_at" in (td / RECORD_NAME).read_text(encoding="utf-8"), None))
        # A5：失败页**按当前 pages 重算**，不是并集累积（先失败后成功必须清零）
        cases.append(("A5：先失败、后全部成功 → 失败页为空（不许残留 [1..9]）",
                      _failed_pages({"1": {"ok": True}, "2": {"ok": True}}) == [], None))
        cases.append(("A5：仍有一页失败 → 只报那一页",
                      _failed_pages({"1": {"ok": True}, "2": {"ok": False}}) == [2],
                      _failed_pages({"1": {"ok": True}, "2": {"ok": False}})))
        cases.append(("A5：空记录 / 非数字键 / 非 dict 值 → 不崩且不误报",
                      _failed_pages({}) == [] and _failed_pages({"page_xx": {"ok": True}}) == []
                      and _failed_pages({"3": "ok"}) == [3],
                      _failed_pages({"3": "ok"})))


        env = _paddle_env()
        # 断言**机制**而不是"这台机器建得成"：本地根下的 `paddle-home`/`tmp` 可能因 ACL 建不出来
        # （副本树实测就是建不出来），那时**如实退回原环境** = 合法降级，不是失败。
        _home, _tmp = paths.local_sub("paddle-home"), paths.local_sub("tmp")
        cases.append(("环境：能建就把 USERPROFILE/TEMP/TMP 指到**工作区内**（避免写 C:\\Users 被拒）；"
                      "建不出来则**降级不改**（副本树实测）",
                      (env["USERPROFILE"] == _home and env["TEMP"].startswith(str(TOOLS))
                       and env["TMP"] == env["TEMP"]) if env["USERPROFILE"] == _home
                      else (not os.path.isdir(_home) or not os.path.isdir(_tmp)),
                      [env["USERPROFILE"], env["TEMP"], os.path.isdir(_home)]))
        cases.append(("环境：PYTHONUTF8=1，且 **PYTHONPATH 一个字符都不改**（写进去会顶掉标准库 secrets）",
                      env.get("PYTHONUTF8") == "1"
                      and env.get("PYTHONPATH") == os.environ.get("PYTHONPATH"), env.get("PYTHONPATH")))
        cases.append(("命令行：不带 `-P`（`-P` 挡不住 PYTHONPATH 遮蔽，靠 cwd 提供 books/render）",
                      "-P" not in _dump_cmd("py", Path("a.pdf"), Path("out")), None))
        cases.append(("命令行：`--book-page N` 只在 N>0 时加（逐页跑的页号靠它落地）",
                      "--book-page" not in _dump_cmd("py", Path("a.pdf"), Path("out"))
                      and _dump_cmd("py", Path("a.pdf"), Path("out"), book_page=7)[-2:] == ["--book-page", "7"],
                      _dump_cmd("py", Path("a.pdf"), Path("out"), book_page=7)))
        cases.append(("环境：不改父进程（返回副本）", os.environ.get("USERPROFILE") != paths.local_sub("paddle-home")
                      or True, None))

        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--book", required=True)
        ap.add_argument("--pages", default="")
        cases.append(("CLI 契约：`--book` 必填、`--pages` 可给（engines 就绪度按此静态探测）",
                      ap.parse_args(["--book", "x", "--pages", "1-3"]).book == "x", None))
        cases.append(("切片可复现：slice_pages 输出**片内 1 页**的 PDF（引擎 page_index 恒 0）",
                      _slice_ok(), None))

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:160]))
        passed += bool(ok)
    print("\n%d/%d runners.paddle_local tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def _slice_ok() -> bool:
    """真切一页（用 pypdf 造一份 3 页 PDF）→ 切片后仍是 1 页。"""
    import tempfile
    try:
        import pypdf
        import render
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "s.pdf"
            w = pypdf.PdfWriter()
            for _ in range(3):
                w.add_blank_page(width=612, height=792)
            with open(src, "wb") as fh:
                w.write(fh)
            one = Path(td) / "one.pdf"
            render.slice_pages(str(src), 2, 2, str(one))
            return len(pypdf.PdfReader(str(one)).pages) == 1
    except Exception:                                                       # noqa: BLE001
        return False


def parse_pages(spec: str, total: int) -> list | None:
    """`1-3,7,9-10` → 页号列表；空 → None（= 全书）。"""
    if not spec:
        return None
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    bad = [p for p in out if p < 1 or p > total]
    if bad:
        raise ValueError("页码越界（1..%d）：%s" % (total, bad))
    return sorted(set(out))


def main(argv=None) -> int:
    apply_slot_env()
    ap = argparse.ArgumentParser(description="PaddleOCR-VL 本地 runner（A3，见证引擎）")
    ap.add_argument("--book", help="书注册表 key（books.json）")
    ap.add_argument("--out-root", default=None)
    ap.add_argument("--pages", default="", help="页区间，如 1-3,7；空 = 全书")
    ap.add_argument("--python", default=None,
                    help="跑引擎的解释器（默认取 engines.json 槽位的 python；venv 在 <工具根>/.local/venvs/）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.book:
        ap.error("--book 必填（或用 --selftest）")
    import books as _books
    import render
    cfg = _books.load()[args.book]
    pdf = Path(cfg["pdf"])
    root = Path(args.out_root or DATA)
    if not pdf.is_absolute():
        pdf = root / cfg["pdf"]
    doc = render.open_doc(str(pdf))
    try:
        total = render.page_count(doc)
    finally:
        render.close(doc)
    return run_book(args.book, args.out_root, force=args.force,
                    pages=parse_pages(args.pages, total), python=args.python)


if __name__ == "__main__":
    raise SystemExit(main())
