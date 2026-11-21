# -*- coding: utf-8 -*-
"""B2 · **工作根与安装层验收**（SPEC §7.10）。纳入全量回归（`c3_full_regression.SUITES`）。

三条检查，**每条都带负例**（没有负例的检查等于没有检查）：

  ① **依赖清单 ↔ 实测 import 集合 双向一致**
     口径：**发布集**里的脚本（`release-exclude.json` 过滤后）真正 `import` 的第三方模块。
     **漏列**（脚本 import 了清单里没有的）与**多列**（清单里写了没人用的）都要报错。
     → 纯函数 `compare_deps()` + 三种人造不一致必须被抓。
  ② **根解析**（`paths.py`）
     默认 == 老写法（**零行为变化**）、环境变量生效、显式参数优先、坏根报错且**说明来源**、
     `explain()` 不抛异常；外加**防退化扫描**：发布集脚本**不许再自己推根**
     （`ROOT = dirname(dirname(__file__))` / `= dirname(HERE)` / `= HERE.parents[2]`）——必须走 `paths`。
  ③ **工具 / 数据真分家可跑**（这条是 B2 的验收本体）
     把**工具树**拷到临时目录、`CHEMDOC_ROOT` 指向**原来的数据根**，在那个副本里跑
     `validate_run.py run --book <真书>` → 必须 exit 0 且判决 `可交付`。
     **数据不在**（副本/新机）→ **明示跳过**，不假装跑过（SPEC §5.3 的诚实口径）。

用法:
  python -X utf8 b2_install_check.py selftest            # 全套（含③；需数据在位）
  python -X utf8 b2_install_check.py selftest --quick    # 只跑①②（数据无关，快）
  python -X utf8 b2_install_check.py show                # 打印锚点/依赖清单/扫描结果（只读）
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # B2：工作根唯一出口（SPEC §7.10）
import release_check as RC

TOOLS = paths.TOOLS_ROOT
DROP = ("pristine-check", "__pycache__", "*.pyc")

# 模块名 → 发行包名（不一致的才列；其余同名）
DIST_ALIAS = {"PIL": "pillow", "markdown_it": "markdown-it-py", "cv2": "opencv-python",
              "paddle": "paddlepaddle-gpu", "yaml": "pyyaml"}
# 只给**可选栈**（本地见证引擎）用的包：允许出现在可选清单里，不算"多列"
OPTIONAL_PKGS = {"paddleocr", "paddlepaddle", "paddlepaddle-gpu", "paddlex", "opencv-contrib-python"}
# **按设计可选**的依赖：脚本自己兜住了「没装」的情况（因此不进 requirements）。
# 硬条件（下面有检查）：它的 import **不得出现在模块顶层**（必须在函数里 / try 里，失败才降级）。
OPTIONAL_BY_DESIGN = {
    "fastmcp": "runners/mineru_cloud.py 只在 `from fastmcp import FastMCP` 失败时装 in-process 桩（不装 fastmcp）",
}


def _norm(name: str) -> str:
    return str(name).strip().lower().replace("_", "-")


def release_scripts() -> list:
    """发布集里的脚本（相对工具根的 POSIX 路径）。"""
    rels = [f"_mcp-test/{f}" for f in sorted(os.listdir(os.path.join(TOOLS, "_mcp-test")))
            if f.endswith(".py")]
    rels += [f"_mcp-test/runners/{f}" for f in sorted(os.listdir(os.path.join(TOOLS, "_mcp-test", "runners")))
             if f.endswith(".py")]
    man = json.load(io.open(os.path.join(TOOLS, "_mcp-test", "release-exclude.json"), encoding="utf-8"))
    keep, _drop = RC.apply_excludes(rels, man)
    return sorted(keep)


def imported_pkgs(rels: list) -> dict:
    """解析这些脚本的顶层 import → {发行包名: [文件, …]}（排除标准库与仓库内部模块）。"""
    std = set(sys.stdlib_module_names)
    local = {os.path.splitext(os.path.basename(p))[0] for p in rels} | {"runners"}
    out: dict = {}
    for rel in rels:
        p = os.path.join(TOOLS, rel.replace("/", os.sep))
        try:
            tree = ast.parse(io.open(p, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if n in std or n in local:
                    continue
                out.setdefault(_norm(DIST_ALIAS.get(n, n)), []).append(os.path.basename(rel))
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


def parse_requirements(path: str) -> list:
    """读 requirements：返回 [(规范名, 原文行)]（忽略注释/空行/`-r` 之类）。"""
    if not os.path.exists(path):
        return []
    out = []
    for line in io.open(path, encoding="utf-8"):
        s = line.split("#")[0].strip()
        if not s or s.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;]", s)[0].strip()
        out.append((_norm(name), s))
    return out


def compare_deps(required: dict, listed_main: list, listed_opt: list) -> dict:
    """**双向**比对（纯函数，便于负例）：漏列 / 多列 / 该进可选却进了主清单。

    * 主清单要求**恰好等于**实测需要的集合（多一个少一个都报）；
    * **按设计可选**的依赖（`OPTIONAL_BY_DESIGN`）不算「漏」；
    * 可选清单**允许显式多钉传递依赖**（如 `paddlepaddle-gpu` 是 `paddleocr` 的运行时依赖、
      必须显式钉 GPU 版）——只有**不在 `OPTIONAL_PKGS` 里**的多余项才报（`extra_opt_undeclared`）。
    """
    req = set(required) - set(OPTIONAL_BY_DESIGN)
    main = {n for n, _ in listed_main}
    opt = {n for n, _ in listed_opt}
    return {"missing_main": sorted(p for p in req - main if p not in opt),
            "missing_opt": sorted(p for p in (req & OPTIONAL_PKGS) if p not in opt),
            "extra_main": sorted(p for p in main - req if p not in OPTIONAL_PKGS),
            "extra_opt": sorted(p for p in opt - req),
            "extra_opt_undeclared": sorted(p for p in opt - req if p not in OPTIONAL_PKGS),
            "opt_in_main": sorted(p for p in (main & OPTIONAL_PKGS))}


def scan_requirements_ascii(paths_list: list) -> list:
    """依赖清单必须是**纯 ASCII**。

    实测坑（B2 当场踩到）：`pip install -r` 用**本机编码**读清单（本机 GBK）→ 我写的 UTF-8
    中文注释让 pip 直接 `UnicodeDecodeError: 'gbk' codec can't decode`，**装不上**。
    中文说明请放文档/install.ps1，清单里只留 ASCII。
    """
    out = []
    for p in paths_list:
        if not os.path.exists(p):
            continue
        raw = open(p, "rb").read()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            out.append((os.path.basename(p), exc.start, str(exc)[:60]))
    return out


def scan_optional_toplevel(rels: list) -> list:
    """**按设计可选**的依赖不许出现在**模块顶层** import（必须在函数 / try 里，失败才降级）。

    为什么：`fastmcp` 不装也能跑，靠的是 runner 自己装桩；一旦有人把它挪到顶层，
    「没装也能跑」就悄悄失效（而 requirements 里没有它）—— 这条检查就是防这个。
    """
    out = []
    pats = [re.compile(r"^(?:import|from)[ \t]+%s\b" % re.escape(m)) for m in OPTIONAL_BY_DESIGN]
    for rel in rels:
        p = os.path.join(TOOLS, rel.replace("/", os.sep))
        for i, line in enumerate(io.open(p, encoding="utf-8").read().splitlines(), 1):
            if any(rx.match(line) for rx in pats):
                out.append((rel, i, line.strip()[:80]))
    return out


ANCHOR_PAT = re.compile(r"os\.path\.dirname\(os\.path\.dirname\(os\.path\.abspath\(__file__\)\)\)"
                        r"|ROOT\w*\s*=\s*os\.path\.dirname\(HERE\)"
                        r"|\bROOT\w*\s*=\s*HERE\.parents\[2\]")


def scan_root_anchors(rels: list) -> list:
    """防退化扫描：发布集脚本里**不许再自己推根**（必须用 `paths`）。返回 [(文件, 行号, 片段)]。"""
    out = []
    for rel in rels:
        p = os.path.join(TOOLS, rel.replace("/", os.sep))
        for i, line in enumerate(io.open(p, encoding="utf-8").read().splitlines(), 1):
            if ANCHOR_PAT.search(line) and "paths.py" not in rel:
                out.append((rel, i, line.strip()[:90]))
    return out


def _run(cmd: list, cwd: str, env: dict, out_path: str) -> int:
    """跑子进程，输出**重定向到文件**（不用管道：沙箱下管道可能被拒）。"""
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        return subprocess.call(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)


def pick_live_book() -> str:
    """挑一本**在数据树里真有交付物**的书（用于「工具/数据分家真跑」）——从注册表 + 磁盘推，**不写死**。"""
    import books as _b
    try:
        bks = _b.load()
    except Exception:                                            # noqa: BLE001
        return ""
    live = _b.delivered(bks)
    return live[0] if live else ""


def check_separation(book: str = "", tmp: str = "") -> dict:
    """③ 工具/数据分家：工具拷到临时目录 + `CHEMDOC_ROOT` 指向原数据根 → 真书校验必须过。"""
    data = paths.data_root()
    key = book
    try:
        import books as _b
        cfg = (_b.load() or {}).get(book) or {}
        key = cfg.get("key") or book
    except Exception:                                                # noqa: BLE001
        pass
    book = book or pick_live_book()
    if not book:
        return {"ok": True, "skipped": True, "why": "注册表里没有「已交付」的书（数据树为空）→ 明示跳过"}
    need = [os.path.join(data, "s7-out", key, "report.json"),
            os.path.join(data, "s7-out", key, "validation-report.json"),
            os.path.join(data, "s2-ir", f"ir.mineru.{key}.jsonl")]
    if not all(os.path.exists(p) for p in need):
        return {"ok": True, "skipped": True,
                "why": "数据根 %s 下没有《%s》的产物（副本/新机）→ 明示跳过，不假装跑过" % (data, key)}
    root = tmp or paths.tmp_dir("b2-sep-")
    tools_copy = os.path.join(root, "tools")
    # 只拷**工具树**（代码 + 文档级配置），**不**拷数据/环境/垃圾：`_mcp-test` + `dual-mcp-bridge`
    #   + 依赖清单与安装器。拷整个工作区会把 trash/.venv/_wheels 也拖进来（实测直接 RecursionError）。
    for name in ("_mcp-test", "dual-mcp-bridge", "requirements.txt",
                 "requirements-optional-paddle.txt", "install.ps1"):
        src, dst = os.path.join(TOOLS, name), os.path.join(tools_copy, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*DROP))
        else:
            os.makedirs(tools_copy, exist_ok=True)
            shutil.copy2(src, dst)
    env = dict(os.environ)
    env["CHEMDOC_ROOT"] = data
    env["PYTHONUTF8"] = "1"
    out_path = os.path.join(root, "validate.log")
    script = os.path.join(tools_copy, "_mcp-test", "validate_run.py")
    rc = _run([sys.executable, "-X", "utf8", script, "run", "--book", book], tools_copy, env, out_path)
    log = io.open(out_path, encoding="utf-8").read() if os.path.exists(out_path) else ""
    verdict = ""
    m = re.search(r'"verdict"\s*:\s*"([^"]+)"', log)
    if m:
        verdict = m.group(1)
    return {"ok": rc == 0 and verdict.startswith("可交付"), "skipped": False, "rc": rc, "verdict": verdict,
            "tools_copy": tools_copy, "data_root": data, "log_tail": log.strip().splitlines()[-3:],
            "why": "工具在临时目录、数据在原位（CHEMDOC_ROOT 指向数据根）→ 校验通过" if rc == 0 else "分家后跑不动"}


def selftest(quick: bool = False, book: str = "") -> int:
    cases: list = []
    tmp = paths.tmp_dir("b2-install-")
    try:
        rels = release_scripts()
        req = imported_pkgs(rels)
        main_list = parse_requirements(os.path.join(TOOLS, "requirements.txt"))
        opt_list = parse_requirements(os.path.join(TOOLS, "requirements-optional-paddle.txt"))
        cmp_res = compare_deps(req, main_list, opt_list)
        cases.append(("① 发布集脚本数 = %d；实测需要的发行包 = %d（%s）"
                      % (len(rels), len(req), "、".join(req)),
                      len(rels) >= 40 and len(req) >= 5, sorted(req)))
        _bad_keys = ("missing_main", "missing_opt", "extra_main", "extra_opt_undeclared", "opt_in_main")
        cases.append(("① `requirements.txt` 与实测 import **双向一致**（漏列/多列都为 0）",
                      not any(cmp_res[k] for k in _bad_keys), {k: cmp_res[k] for k in _bad_keys}))
        cases.append(("① `fastmcp` 属**按设计可选**：不在清单里，但**必须**是函数内 import（不是顶层）",
                      scan_optional_toplevel(rels) == [], scan_optional_toplevel(rels)[:2]))
        _ascii = scan_requirements_ascii([os.path.join(TOOLS, "requirements.txt"),
                                          os.path.join(TOOLS, "requirements-optional-paddle.txt")])
        cases.append(("① 依赖清单**纯 ASCII**（pip 按本机编码读：中文注释会让安装直接崩）",
                      not _ascii, _ascii))
        _fake_req = os.path.join(tmp, "req-utf8.txt")
        io.open(_fake_req, "w", encoding="utf-8").write("# 中文注释\nnumpy==2.5.3\n")
        cases.append(("① 负例：清单里放中文 → 必须被抓",
                      bool(scan_requirements_ascii([_fake_req])), None))
        cases.append(("① 可选栈（Paddle）单独成文件、且**不在主清单**里",
                      "paddleocr" in {n for n, _ in opt_list}
                      and not ({n for n, _ in main_list} & OPTIONAL_PKGS), cmp_res["opt_in_main"]))
        # ① 三种人造不一致必须被抓（负例）
        cases.append(("① 负例：清单**漏掉** numpy → 报 `missing_main`",
                      "numpy" in compare_deps(req, [x for x in main_list if _norm(x[0]) != "numpy"],
                                             opt_list)["missing_main"], None))
        cases.append(("① 负例：清单**多写**一个没人用的包 → 报 `extra_main`",
                      compare_deps(req, main_list + [("nobody-uses-this", "nobody-uses-this==1.0")], opt_list)["extra_main"]
                      == ["nobody-uses-this"], None))
        cases.append(("① 负例：脚本 import 了新包（清单没跟上）→ 报 `missing_main`",
                      "requests" in compare_deps(dict(req, requests=["s3_compare.py"]), main_list,
                                                opt_list)["missing_main"], None))
        _probe = os.path.join(TOOLS, "_mcp-test", "__b2_probe.py")
        io.open(_probe, "w", encoding="utf-8").write("import " + "fastmcp" + "\n")
        _hit = bool(scan_optional_toplevel(["_mcp-test/__b2_probe.py"]))
        os.remove(_probe)
        cases.append(("① 负例：把 `fastmcp` 挪到**模块顶层** → 必须被抓"
                      "（否则「没装也能跑」会悄悄失效）", _hit, None))

        # ② 根解析
        _saved = os.environ.get(paths.ENV_VAR)
        try:
            os.environ.pop(paths.ENV_VAR, None)
            old = os.path.dirname(os.path.dirname(os.path.abspath(os.path.join(TOOLS, "_mcp-test", "x.py"))))
            cases.append(("② 默认数据根 == 老写法（**零行为变化**）", paths.data_root() == old == TOOLS, paths.data_root()))
            os.environ[paths.ENV_VAR] = tmp
            cases.append(("② `%s` 生效" % paths.ENV_VAR, paths.data_root() == paths.normalize(tmp), paths.data_root()))
            cases.append(("② 显式参数优先于环境变量", paths.data_root(TOOLS) == TOOLS, None))
            os.environ[paths.ENV_VAR] = os.path.join(tmp, "nope")
            try:
                paths.data_root()
                cases.append(("② 负例：坏数据根必须报错", False, "没报错"))
            except paths.DataRootError as exc:
                cases.append(("② 负例：坏数据根 → `DataRootError` 且**说明来源**",
                              paths.ENV_VAR in str(exc) and "不存在" in str(exc), str(exc)[:70]))
            cases.append(("② 坏根下 `explain()` 不抛异常、如实报 error",
                          bool(paths.explain()["error"]), None))
        finally:
            if _saved is None:
                os.environ.pop(paths.ENV_VAR, None)
            else:
                os.environ[paths.ENV_VAR] = _saved
        bad = scan_root_anchors(rels)
        cases.append(("② 防退化：发布集里**没有**脚本自己推根（都走 `paths`）", not bad, bad[:4]))
        _fake = os.path.join(tmp, "fake_anchor.py")
        _fake_src = "import os\nROOT = os.path." + "dirname(os.path." + "dirname(os.path.abspath(__file__)))\n"
        io.open(_fake, "w", encoding="utf-8").write(_fake_src)
        _hits = [1 for l in io.open(_fake, encoding="utf-8").read().splitlines() if ANCHOR_PAT.search(l)]
        cases.append(("② 负例：人造一行老写法 → 扫描器**必须**命中", bool(_hits), _hits))

        # ③ 分家（唯一依赖真书的检查）
        if quick:
            cases.append(("③ 工具/数据分家可跑（`--quick` 跳过）", True, "skipped"))
        else:
            r = check_separation(book, os.path.join(tmp, "sep"))
            cases.append(("③ 工具在临时目录 + 数据在原位 → 真书校验 exit 0 且判决 `可交付`"
                          + ("（数据不在 → 明示跳过）" if r.get("skipped") else ""),
                          r["ok"], r.get("why") if r["ok"] else {"rc": r.get("rc"), "verdict": r.get("verdict"),
                                                                 "tail": r.get("log_tail")}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> %s" % str(extra)[:200]))
        passed += bool(ok)
    print("\n%d/%d B2 验收检查通过" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def cmd_show(_a) -> int:
    e = paths.explain()
    print("工具根：", e["tools_root"])
    print("数据根：", e["data_root"] or "（无效）", "｜来源：", e["data_root_source"])
    if e["error"]:
        print("错误  ：", e["error"])
    rels = release_scripts()
    req = imported_pkgs(rels)
    print("\n发布集脚本 %d 个 → 实测需要 %d 个发行包：" % (len(rels), len(req)))
    for k, v in req.items():
        tag = "（可选栈）" if k in OPTIONAL_PKGS else ""
        print("  %-22s %s  %s" % (k, tag, "、".join(v[:4])))
    print("\n主清单：", [n for n, _ in parse_requirements(os.path.join(TOOLS, "requirements.txt"))])
    print("可选清单：", [n for n, _ in parse_requirements(os.path.join(TOOLS, "requirements-optional-paddle.txt"))])
    bad = scan_root_anchors(rels)
    print("\n自己推根的脚本：", bad or "无（都走 paths）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 工作根与安装层验收")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("selftest")
    st.add_argument("--quick", action="store_true", help="只跑①②（数据无关）")
    st.add_argument("--book", default="", help="哪本书做分家实测（默认：自动挑一本已交付的）")
    sub.add_parser("show").set_defaults(func=cmd_show)
    a = ap.parse_args()
    if a.cmd == "show":
        return cmd_show(a)
    return selftest(a.quick, a.book)


if __name__ == "__main__":
    sys.exit(main())
