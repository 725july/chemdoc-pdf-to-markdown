# -*- coding: utf-8 -*-
"""**发布排除校验器（B4）**：把 `release-exclude.json` 的边界变成可执行、可回归的检查。

设计（与其它配置同一套纪律）：
  * **清单是数据**（`release-exclude.json`），**校验是代码**（本文件）：改边界必须同时让两边都绿；
  * 内核是**纯函数**（`apply_excludes` / `scan_entries`），所以自测**不需要拷贝整棵树**——喂合成条目即可；
  * **fail-closed**：发布集里出现 `import fitz`、疑似密钥、受版权书 → **报错退出 1**；
  * 个人路径只 **warn**（不阻断），因为它常出现在文档里且危害低。

用法:
    python -X utf8 release_check.py check     # 走真实目录树，输出发布集与违规
    python -X utf8 release_check.py list      # 只看发布集构成与排除统计
    python -X utf8 release_check.py selftest  # 纯函数负例（不需要真实树）
"""
from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.TOOLS_ROOT
MANIFEST = os.path.join(HERE, "release-exclude.json")
TOP_KEYS = ("schema", "note", "exclude_paths", "exclude_globs", "glob_exclusions_why",
            "content_rules", "known_copyrighted_sources", "copyright_note")
RULE_KEYS = ("id", "severity", "include", "pattern", "why")


class ManifestError(ValueError):
    """清单本身不合法（**必须报错**，不许静默少排除）。"""


def load_manifest(path: str = "") -> dict:
    p = path or MANIFEST
    with io.open(p, encoding="utf-8") as fh:
        m = json.load(fh)
    errs = []
    unk = [k for k in m if k not in TOP_KEYS]
    if unk:
        errs.append("顶层未知字段：%s" % unk)
    if m.get("schema") != 1:
        errs.append("schema 必须是 1")
    for key in ("exclude_paths", "exclude_globs", "content_rules"):
        if not isinstance(m.get(key), list) or not m[key]:
            errs.append("%s 必须是非空列表" % key)
    for e in m.get("exclude_paths", []):
        if not isinstance(e, dict) or not e.get("path") or not e.get("why"):
            errs.append("exclude_paths 每项必须有 path 与 why：%r" % (e,))
    for r in m.get("content_rules", []):
        miss = [k for k in RULE_KEYS if k not in r]
        if miss:
            errs.append("规则缺字段 %s：%r" % (miss, r.get("id")))
            continue
        if r["severity"] not in ("violation", "warning"):
            errs.append("规则 %s 的 severity 必须是 violation/warning" % r["id"])
        try:
            re.compile(r["pattern"])
        except re.error as exc:
            errs.append("规则 %s 的正则不合法：%s" % (r["id"], exc))
    ids = [r.get("id") for r in m.get("content_rules", []) if isinstance(r, dict)]
    if len(ids) != len(set(ids)):
        errs.append("规则 id 重复：%s" % ids)
    if errs:
        raise ManifestError("发布排除清单不合法：\n  - " + "\n  - ".join(errs))
    return m


# ---------------------------------------------------------------- 纯函数（可单测）
def _norm(rel: str) -> str:
    return rel.replace("\\", "/").lstrip("./")


def _glob_hit(rel: str, pattern: str) -> bool:
    """glob 匹配：`**/` 开头的模式**也要能匹配根级文件**（早先只试 `*/*.pdf` → 根目录 PDF 漏网）。"""
    cands = [pattern]
    if pattern.startswith("**/"):
        cands.append(pattern[3:])
    for c in cands:
        if fnmatch.fnmatch(rel, c) or fnmatch.fnmatch(os.path.basename(rel), c):
            return True
    return False


def apply_excludes(rels: list, manifest: dict) -> tuple:
    """把相对路径列表按清单过滤 → (保留, 被排除的 [(path, why)])。`*` 不跨目录、`**` 跨目录。"""
    paths = [_norm(p) for p in rels]
    drop, keep = [], []
    dirs = [(_norm(e["path"]).rstrip("/"), e["why"]) for e in manifest["exclude_paths"]]
    globs = [(g, manifest.get("glob_exclusions_why", "按 exclude_globs 排除"))
             for g in manifest["exclude_globs"]]
    for p in paths:
        why = None
        for d, w in dirs:
            if p == d or p.startswith(d + "/"):
                why = w
                break
        if why is None:
            for g, w in globs:
                if _glob_hit(p, g):
                    why = w
                    break
        (drop if why else keep).append(p if not why else (p, why))
    return keep, (drop if not keep else [x for x in drop if isinstance(x, tuple)])


def scan_entries(entries: list, manifest: dict) -> tuple:
    """entries: [{"path": str, "text": str}]（只喂**发布集内**的文件）→ (violations, warnings)。"""
    viol, warn = [], []
    rules = []
    for r in manifest["content_rules"]:
        rules.append((r["id"], r["severity"], r["include"], re.compile(r["pattern"], re.MULTILINE), r["why"]))
    for e in entries:
        base = os.path.basename(e["path"])
        for rid, sev, inc, rx, why in rules:
            if not any(fnmatch.fnmatch(base, pat) for pat in inc):
                continue
            m = rx.search(e["text"] or "")
            if not m:
                continue
            rec = {"rule": rid, "path": e["path"], "line": (e["text"][:m.start()].count("\n") + 1),
                   "snippet": (e["text"][max(0, m.start() - 0):m.end()]).strip()[:90], "why": why}
            (viol if sev == "violation" else warn).append(rec)
    return viol, warn


# ---------------------------------------------------------------- 真实目录树
TEXT_EXT = (".py", ".json", ".md", ".ps1", ".cmd", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini")


def walk_release(root: str = "") -> tuple:
    r = root or ROOT
    rels = []
    for base, dirs, files in os.walk(r):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            rels.append(_norm(os.path.relpath(os.path.join(base, f), r)))
    return rels


def cmd_check(_a) -> int:
    m = load_manifest()
    rels = walk_release()
    keep, dropped = apply_excludes(rels, m)
    entries = []
    for p in keep:
        if p.endswith(TEXT_EXT):
            try:
                with io.open(os.path.join(ROOT, p), encoding="utf-8", errors="ignore") as fh:
                    entries.append({"path": p, "text": fh.read()})
            except OSError:
                pass
    viol, warn = scan_entries(entries, m)
    tops = {}
    for p in keep:
        tops[p.split("/")[0] if "/" in p else "(根)"] = tops.get(p.split("/")[0] if "/" in p else "(根)", 0) + 1
    print("发布集：%d 个文件（排除 %d 个）" % (len(keep), len(dropped)))
    print("  顶层构成：", json.dumps(dict(sorted(tops.items(), key=lambda kv: -kv[1])), ensure_ascii=False))
    print("  规则检查的文本文件：%d" % len(entries))
    for r in viol:
        print("  **违规** [%s] %s:%s  %s\n            → %s" % (r["rule"], r["path"], r["line"], r["snippet"], r["why"]))
    for r in warn:
        print("  警告   [%s] %s:%s  %s" % (r["rule"], r["path"], r["line"], r["snippet"]))
    print("\n结果：违规 %d、警告 %d → %s" % (len(viol), len(warn), "可发布" if not viol else "**不可发布**"))
    return 0 if not viol else 1


def cmd_list(_a) -> int:
    m = load_manifest()
    rels = walk_release()
    keep, dropped = apply_excludes(rels, m)
    print("发布集（%d）：" % len(keep))
    for p in sorted(keep):
        print("   ", p)
    seen = {}
    for p, why in dropped:
        seen.setdefault(why, 0)
        seen[why] += 1
    print("\n排除（%d），按理由分组：" % len(dropped))
    for why, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        print("   %4d  %s" % (n, why[:80]))
    return 0


# ---------------------------------------------------------------- 自测（纯函数，负例为主）
def selftest() -> int:
    cases = []
    m = load_manifest()
    cases.append(("清单可加载且规则齐备", len(m["content_rules"]) >= 3 and len(m["exclude_paths"]) >= 20,
                  len(m["exclude_paths"])))

    # 排除语义
    rels = ["_mcp-test/gates.py", "build-dual-mcp-book-skill/SKILL.md", "_test-inputs/x.pdf",
            "docs/new/SPEC-化学教材PDF转Markdown-v2.md", "_mcp-test/probe_old.py",
            "s7-out/puhua/chapter-01-clean.md", ".secrets/env.ps1", "trash/2026/x.txt",
            "_mcp-test/render.py", "_mcp-test/books.sample.json", "requirements.txt",
            "install.ps1", "AGENTS.md", "SKILL.md", "docs/01-快速开始.md"]
    keep, dropped = apply_excludes(rels, m)
    cases.append(("第三方 skill 目录被排除", "build-dual-mcp-book-skill/SKILL.md" not in keep, keep))
    cases.append(("受版权书目 _test-inputs 被排除", "_test-inputs/x.pdf" not in keep, keep))
    cases.append(("交付物目录 s7-out 被排除", not any(p.startswith("s7-out/") for p in keep), keep))
    cases.append(("密钥文件被排除", ".secrets/env.ps1" not in keep, keep))
    cases.append(("冻结诊断 probe_*.py 被排除", "_mcp-test/probe_old.py" not in keep, keep))
    cases.append(("**根目录的 PDF 也必须被排除**（曾因 glob 匹配器 fail-open 漏网）",
                  "x.pdf" not in apply_excludes(["x.pdf", "_mcp-test/render.py"], m)[0], None))
    cases.append(("`_mcp-test` 旧 run/诊断数据目录被排除",
                  not any(p.startswith("_mcp-test/retrieval-pilot/")
                          for p in apply_excludes(["_mcp-test/retrieval-pilot/a.json",
                                                   "_mcp-test/render.py"], m)[0]), None))
    cases.append(("在册代码与配置**保留**（代码 = 产品本体；配置 = 示例注册表/引擎/调优）",
                  "_mcp-test/gates.py" in keep and "_mcp-test/render.py" in keep
                  and "_mcp-test/books.sample.json" in keep
                  and "requirements.txt" in keep and "install.ps1" in keep, keep))
    # 发布边界（2026-10-13 起）：**开发文档 / agent 入口 / skill 入口**属开发侧，不进"产品"发布集；
    # 分发副本自带面向使用者的 README/AGENTS/SKILL/docs（见 copy_release.py 的 COPY_OWNED）。
    cases.append(("开发文档与 agent/skill 入口**不进发布集**（产品副本自带面向使用者的版本）",
                  not any(p == "docs" or p.startswith("docs/") for p in keep)
                  and "AGENTS.md" not in keep and "SKILL.md" not in keep, keep))

    # 内容规则：负例必须真的被抓
    v, w = scan_entries([{"path": "a/b.py", "text": "import fitz\nx=1\n"}], m)
    cases.append(("负例：`import fitz` 被抓为违规", any(r["rule"] == "no-pymupdf" for r in v), v))
    _dev = "D:" + chr(92) + "AIwork" + chr(92) + "02"      # **故意拼出来**：规则不该在源码里带真路径
    v2, _w2 = scan_entries([{"path": "输入密钥.cmd", "text": "set PHP=" + _dev + chr(92) + ".venv" + chr(92) + "Scripts" + chr(92) + "python.exe\n"}], m)
    cases.append(("负例：**开发机绝对路径**被抓为违规（旧规则只查用户目录，漏过）",
                  any(r["rule"] == "no-dev-abs-path" for r in v2), v2))
    v3, _w3 = scan_entries([{"path": "输入密钥.cmd", "text": "set PHP=%~dp0.local\\venvs\\dual-mcp\\Scripts\\python.exe\n"}], m)
    cases.append(("正例：`%~dp0` 相对出口**不该**被误抓",
                  not any(r["rule"] == "no-dev-abs-path" for r in v3), v3))
    v, w = scan_entries([{"path": "a/c.py", "text": 'import pymupdf as fitz\n'}], m)
    cases.append(("负例：`import pymupdf` 也被抓", any(r["rule"] == "no-pymupdf" for r in v), v))
    # 夹具运行时拼接：本文件里不出现「密钥样式串」字面量（否则规则会命中校验器自己）
    fake_key = "abcdef0123456789" + "abcdef01"
    fake_json = '{"mineru_api' + '_key": "' + fake_key + '"}'
    v, w = scan_entries([{"path": "conf.json", "text": fake_json}], m)
    cases.append(("负例：密钥样式串被抓", any(r["rule"] == "no-secret-literal" for r in v), v))
    fake_path = "路径 C:" + chr(92) + "Users" + chr(92) + "someone"
    v, w = scan_entries([{"path": "d.md", "text": fake_path}], m)
    cases.append(("个人路径只算警告、不算违规",
                  not v and any(r["rule"] == "no-personal-path" for r in w), (v, w)))
    v, w = scan_entries([{"path": "ok.py", "text": "import render\n# 只是提到 fitz 不算 import\n"}], m)
    cases.append(("正例：干净文件不误报", not v and not w, (v, w)))
    v, w = scan_entries([{"path": "ok.md", "text": "我们**不用** PyMuPDF 了，改用 pypdfium2。"}], m)
    cases.append(("正例：文档里提到 PyMuPDF 不误报", not v, v))

    # 清单自身的负例：未知字段必须报错
    import tempfile
    bad = dict(m)
    bad = json.loads(json.dumps(m))
    bad["exclude_pathsx"] = []
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(bad, fh, ensure_ascii=False)
        try:
            load_manifest(tmp)
            cases.append(("负例：清单未知字段 → 报错", False, None))
        except ManifestError:
            cases.append(("负例：清单未知字段 → 报错", True, None))
        bad2 = json.loads(json.dumps(m))
        bad2["content_rules"][0]["pattern"] = "([unclosed"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(bad2, fh, ensure_ascii=False)
        try:
            load_manifest(tmp)
            cases.append(("负例：规则正则不合法 → 报错", False, None))
        except ManifestError:
            cases.append(("负例：规则正则不合法 → 报错", True, None))
    finally:
        os.remove(tmp)

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:130]))
        passed += bool(ok)
    print("\n%d/%d release_check tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
