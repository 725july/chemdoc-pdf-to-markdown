# -*- coding: utf-8 -*-
"""判定 ↔ 产物版本**绑定校验**。

**为什么需要**：判定（S5）是对"当时那一版引擎产物 / IR 上的那个问题"作出的结论。
若之后引擎重跑、IR 重建，而 `item_id`（页号+序号+类别）碰巧对上，**旧判定会被当成新判定静默生效**。
本模块给每次判定盖一个"指纹"，校验时重新计算并比对：

  * `matched`  → 判定仍适用于当前产物版本，正常生效；
  * `expired`  → 产物版本已变 → **不生效**（视同"未判定"，由既有门禁如 G11/G8 如实报红）；
  * `legacy`   → 历史判定（早于本机制，记录里没有 `binding` 字段）→ 生效但**标记出来**（不静默）。

指纹内容：源 PDF 哈希 + MinerU 原生（`content_list.json`/`layout.json`）+ Paddle 原生（`result.json`）+ S2 的 IR 文件。
用法:
  python -X utf8 binding.py show --book puhua-b2
  python -X utf8 binding.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

ROOT_DEFAULT = paths.data_root()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest(pairs: list[tuple[str, str]]) -> str:
    """把若干 (标签, 文件) 合成一个短指纹；文件不存在记为 missing。"""
    h = hashlib.sha256()
    for label, path in sorted(pairs):
        mark = sha256_file(path)[:16] if path and os.path.isfile(path) else "missing"
        h.update(f"{label}={mark};".encode("utf-8"))
    return h.hexdigest()[:16]


def _files_named(d: str, names: tuple[str, ...]) -> list[tuple[str, str]]:
    out = []
    if not d or not os.path.isdir(d):
        return out
    for base, _dirs, files in os.walk(d):
        for f in files:
            if f in names:
                p = os.path.join(base, f)
                out.append((os.path.relpath(p, d).replace("\\", "/"), p))
    return out


def resolve_cfg(book_or_key: str) -> dict:
    import pipeline_run as pr
    cfg = pr.BOOKS.get(book_or_key)
    if cfg:
        return cfg
    for c in pr.BOOKS.values():                     # 也接受目录键（如 puhua-22-25-29）
        if c.get("key") == book_or_key:
            return c
    return {}


_IR_CACHE: dict = {}


def load_volumes(book_or_key: str, root: str = ROOT_DEFAULT):
    """读 `ocr-raw/volumes-<key>.json`（C0 的卷规范产物）；没有则返回 None（= 单卷）。"""
    cfg = resolve_cfg(book_or_key)
    key = cfg.get("key") or book_or_key
    p = os.path.join(root, "ocr-raw", f"volumes-{key}.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:                                             # noqa: BLE001
        return None


def volume_for_page(book_or_key: str, root: str, page):
    vv = load_volumes(book_or_key, root)
    if not vv or page is None:
        return None
    for v in vv.get("volumes", []):
        if page in (v.get("cut_pages") or []):
            return v
    return None


def _ir_lines_hash(root: str, key: str, cut_pages) -> str:
    """该卷页范围内的 IR 行哈希（只被这一卷的改动影响）。"""
    ck = (root, key, tuple(cut_pages))
    if ck in _IR_CACHE:
        return _IR_CACHE[ck]
    h = hashlib.sha256()
    p = os.path.join(root, "s2-ir", f"ir.mineru.{key}.jsonl")
    if os.path.isfile(p):
        pages = set(cut_pages)
        for line in open(p, encoding="utf-8"):
            if not line.strip():
                continue
            try:
                pg = json.loads(line).get("page")
            except Exception:                                     # noqa: BLE001
                continue
            if pg in pages:
                h.update(line.encode("utf-8"))
    _IR_CACHE[ck] = h.hexdigest()[:16]
    return _IR_CACHE[ck]


def current(book_or_key: str, root: str = ROOT_DEFAULT, page=None) -> dict:
    """判定必须与之绑定的指纹。

    * **有 `volumes-<key>.json`（多卷）**：按**该页所属卷**算指纹（源 PDF + 卷 PDF 哈希 + 该卷页范围的 IR 行哈希）
      —— 这样"只重跑第 2 卷"只会让第 2 卷的判定过期，不会把全书判定作废。
    * **没有卷信息（单卷）**：退回**全书**指纹（与历史判定一致，保证既有判定不失效）。
    """
    cfg = resolve_cfg(book_or_key)
    key = cfg.get("key") or book_or_key
    v = volume_for_page(book_or_key, root, page)
    if v:                                       # 逐卷指纹
        vv = load_volumes(book_or_key, root) or {}
        return {"volume": v.get("id"), "source_pdf": vv.get("source_pdf_sha256", ""),
                "volume_pdf": v.get("sha256", ""),
                "ir": _ir_lines_hash(root, key, v.get("cut_pages") or [])}
    pairs: list[tuple[str, str]] = []
    pairs += _files_named(cfg.get("mineru_dir", ""), ("content_list.json", "layout.json"))
    pairs += _files_named(cfg.get("paddle_dir", ""), ("result.json",))
    for eng in ("mineru", "paddle"):
        pairs.append((f"ir/{eng}", os.path.join(root, "s2-ir", f"ir.{eng}.{key}.jsonl")))
    return {"source_pdf": _digest([("pdf", cfg.get("pdf", ""))]),
            "engines": _digest([p for p in pairs if not p[0].startswith("ir/")]),
            "ir": _digest([p for p in pairs if p[0].startswith("ir/")])}


def status(verdict: dict, cur: dict) -> str:
    b = (verdict or {}).get("binding")
    if not b:
        return "legacy"
    return "matched" if b == cur else "expired"


def status_for_verdict(v: dict, book_or_key: str, root: str = ROOT_DEFAULT) -> str:
    """按**该判定所属页的卷**判断状态（多卷时只让相关卷的改动使其过期）。"""
    b = (v or {}).get("binding")
    if not b:
        return "legacy"
    return "matched" if b == current(book_or_key, root, (v or {}).get("pdf_page")) else "expired"


def split(verdicts: list[dict], cur: dict) -> dict:
    out = {"matched": [], "expired": [], "legacy": []}
    for v in verdicts or []:
        out[status(v, cur)].append(v)
    return out


def selftest() -> int:
    import tempfile
    cases, passed = [], 0
    d = tempfile.mkdtemp(prefix="bind-")
    a = os.path.join(d, "a.json"); b = os.path.join(d, "b.json")
    open(a, "w", encoding="utf-8").write('{"x":1}')
    open(b, "w", encoding="utf-8").write('{"x":1}')
    cases.append(("同内容文件 → 同指纹", sha256_file(a) == sha256_file(b), None))
    cases.append(("_digest 稳定（与顺序无关）",
                  _digest([("a", a), ("b", b)]) == _digest([("b", b), ("a", a)]), None))
    open(b, "w", encoding="utf-8").write('{"x":2}')
    cases.append(("内容变了 → 指纹变", sha256_file(a) != sha256_file(b), None))
    cases.append(("_digest 反映变化", _digest([("a", a)]) != _digest([("a", b)]), None))
    cases.append(("文件缺失 → 记为 missing（不抛异常）",
                  isinstance(_digest([("z", os.path.join(d, "nope"))]), str), None))
    cur = {"source_pdf": "S", "engines": "E", "ir": "I"}
    cases.append(("无 binding 字段 → legacy",
                  status({"item_id": "x"}, cur) == "legacy", None))
    cases.append(("binding 不同 → expired",
                  status({"binding": {"source_pdf": "S", "engines": "X", "ir": "I"}}, cur) == "expired", None))
    cases.append(("binding 相同 → matched", status({"binding": dict(cur)}, cur) == "matched", None))
    cases.append(("split 三分类齐全",
                  split([{"binding": dict(cur)}, {"binding": {"engines": "X"}}, {}], cur)
                  == {"matched": [{"binding": dict(cur)}], "expired": [{"binding": {"engines": "X"}}],
                      "legacy": [{}]}, None))
    # ⑵ **按卷绑定**：只重跑一卷，不能让全书的判定作废（C0 的关键回归）
    _r = tempfile.mkdtemp(prefix="bindvol-")
    os.makedirs(os.path.join(_r, "s2-ir"), exist_ok=True)
    os.makedirs(os.path.join(_r, "ocr-raw"), exist_ok=True)
    with open(os.path.join(_r, "s2-ir", "ir.mineru.fxvol.jsonl"), "w", encoding="utf-8") as fh:
        for p in range(1, 5):
            fh.write(json.dumps({"page": p, "block_id": f"b{p}", "kind": "paragraph"}, ensure_ascii=False) + "\n")
    vols = {"book": "fxvol", "source_pdf_sha256": "SRC", "volumes": [
        {"id": "v01", "cut_pages": [1, 2], "sha256": "V1"},
        {"id": "v02", "cut_pages": [3, 4], "sha256": "V2"}]}
    _vpath = os.path.join(_r, "ocr-raw", "volumes-fxvol.json")

    def _write_vols(sha2):
        vols["volumes"][1]["sha256"] = sha2
        json.dump(vols, open(_vpath, "w", encoding="utf-8"), ensure_ascii=False)
    _write_vols("V2")
    b1 = current("fxvol", _r, 2)
    b2 = current("fxvol", _r, 3)
    cases.append(("有卷信息 → 不同卷指纹不同", b1 != b2 and b1.get("volume") == "v01" and b2.get("volume") == "v02",
                  {"b1": b1, "b2": b2}))
    _vd1 = {"item_id": "x1", "pdf_page": 2, "binding": dict(b1)}
    _vd2 = {"item_id": "x2", "pdf_page": 3, "binding": dict(b2)}
    _write_vols("V2-CHANGED")                     # 模拟"只重跑了第 2 卷"
    cases.append(("重跑 v02 → v01 的判定仍 matched（**洞已堵**）",
                  status_for_verdict(_vd1, "fxvol", _r) == "matched", status_for_verdict(_vd1, "fxvol", _r)))
    cases.append(("重跑 v02 → v02 的判定 expired（负例）",
                  status_for_verdict(_vd2, "fxvol", _r) == "expired", status_for_verdict(_vd2, "fxvol", _r)))
    os.remove(_vpath)                             # 无卷信息 → 退回全书指纹（历史判定不失效）
    cases.append(("无卷信息 → page 参数不影响指纹（退回全书，兼容历史判定）",
                  current("fxvol", _r, 2) == current("fxvol", _r), None))
    import shutil as _sh
    _sh.rmtree(_r, ignore_errors=True)
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name)
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} binding tests passed")
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("--book", required=True)
    s.add_argument("--root", default=ROOT_DEFAULT)
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    print(json.dumps({"book": a.book, "binding": current(a.book, a.root)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
