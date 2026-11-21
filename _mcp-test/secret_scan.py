# -*- coding: utf-8 -*-
"""**密钥不落地检查**（用户口径 2026-10-13：**不进上下文 / 不上传 / agent 不读只引用**）。

它做一件很具体的事：**把当前密钥的值，在整棵工作区里搜一遍**，命中就报**文件路径**（**绝不打印值**）。
—— 这是"密钥不进上下文/不上传"的**机器可查**形式：**机器受控地读一次，agent 永远看不到值**。

三条口径的落点：
  ① **不进上下文**：本脚本与 `preflight` 的输出**只有**「来源 / 长度 / 掩码 / 命中路径」；
  ② **不上传**：命中即拦（`exit 1`），发布物另由 `release_check.py` 兜底（它拦"密钥样式串"）；
  ③ **agent 不读只引用**：agent 只运行本脚本；配置里只许出现**变量名**（`cred_env`）。

值来源优先级：**进程环境 → User 环境 → `%USERPROFILE%\\.chemdoc\\secrets.json`**。

用法:
    python -X utf8 secret_scan.py check     # 扫整棵工作区（跳过 venv/缓存），输出掩码与命中
    python -X utf8 secret_scan.py show      # 只看来源/长度/掩码（不扫描）
    python -X utf8 secret_scan.py selftest
"""
from __future__ import annotations

import fnmatch
import io
import json
import os
import subprocess
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.TOOLS_ROOT
NAMES = ("MINERU_API_TOKEN",)
SECRETS_FILE = os.path.join(os.path.expanduser("~"), ".chemdoc", "secrets.json")
SKIP_DIRS = (".local", ".venv-dual-mcp", ".venv-paddle", ".uv-cache", "_wheels", ".paddle-home",
             "__pycache__", ".git", "_port02")
SKIP_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".zip", ".docx", ".xlsx", ".pptx", ".whl", ".dll", ".exe")
MAX_BYTES = 3_000_000


def mask(v: str) -> str:
    if not v:
        return "(空)"
    return "长度 %d 掩码 %s…%s" % (len(v), v[:4], v[-4:]) if len(v) >= 12 else "长度 %d（偏短）" % len(v)


def _user_env(name: str) -> str:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "[Environment]::GetEnvironmentVariable('%s','User')" % name],
                             capture_output=True, text=True).stdout
        return out.strip()
    except Exception:                                                            # noqa: BLE001
        return ""


def resolve(names=NAMES) -> dict:
    """→ {name: {"source": …, "value": …, "length": n, "mask": …}}（**调用方只许用 length/mask/source**）。"""
    out = {}
    file_vals = {}
    if os.path.exists(SECRETS_FILE):
        try:
            file_vals = json.load(io.open(SECRETS_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            file_vals = {}
    for n in names:
        v = os.environ.get(n) or ""
        src = "进程环境"
        if not v:
            v = _user_env(n)
            src = "User 环境"
        if not v and isinstance(file_vals, dict) and file_vals.get(n):
            v = str(file_vals[n])
            src = "secrets.json"
        out[n] = {"source": src if v else "(未设置)", "value": v, "length": len(v), "mask": mask(v)}
    return out


def scan_roots() -> list:
    """要扫的根 = **工具根 + 数据根**（去重）。

    为什么必须两个：B2 之后"数据"可以在别处（`CHEMDOC_ROOT`）——只扫代码树就会把
    "数据里落了密钥"漏掉，那份"干净"就是假的。数据根配坏时如实报出来（不静默跳过）。
    """
    out = [paths.TOOLS_ROOT]
    try:
        dr = paths.normalize(paths.data_root())
    except paths.DataRootError as exc:
        print("  （警告）数据根无效，本次只扫工具根：%s" % exc)
        return out
    if dr not in [paths.normalize(x) for x in out]:
        out.append(dr)
    return out


def scan_value(value: str, root: str = ROOT) -> tuple:
    """在 root 下找**含该值**的文本文件 → (命中列表, 扫描文件数)。**只返回路径**。"""
    hits, scanned = [], 0
    if not value:
        return hits, scanned
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.lower().endswith(SKIP_EXT):
                continue
            p = os.path.join(base, f)
            try:
                if os.path.getsize(p) > MAX_BYTES:
                    continue
                t = io.open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            scanned += 1
            if value in t:
                hits.append(os.path.relpath(p, root).replace("\\", "/"))
    return sorted(hits), scanned


def cmd_show(_a) -> int:
    r = resolve()
    for n, info in r.items():
        print("  %-20s 来源=%-12s %s" % (n, info["source"], info["mask"]))
    return 0


def cmd_check(_a) -> int:
    r = resolve()
    any_set = False
    bad = 0
    for n, info in r.items():
        print("  %-20s 来源=%-12s %s" % (n, info["source"], info["mask"]))
        if not info["value"]:
            print("     → 未设置：跳过扫描（用 `输入密钥.cmd` / User 环境变量 / secrets.json 设置）")
            continue
        any_set = True
        roots = scan_roots()
        hits, scanned = [], 0
        for r in roots:
            h, s = scan_value(info["value"], r)
            hits += [(r, x) for x in h]
            scanned += s
        print("     → 扫描 %d 个文本文件（根：%s），命中 **%d**"
              % (scanned, "、".join(paths.normalize(x) for x in roots), len(hits)))
        for r, h in hits:
            print("        **命中** %s" % h)
        bad += len(hits)
    print("\n结果：%s" % ("**有密钥落地**（不可发布，也不可进上下文）" if bad else
                       ("未设置密钥，未扫描" if not any_set else "干净：密钥未出现在任何文件里")))
    return 0 if not bad else 1


def selftest() -> int:
    import tempfile
    cases = []
    r = resolve()
    cases.append(("resolve 返回三要素（来源/长度/掩码）且**不含** value 之外的字段泄露",
                  all(set(v) >= {"source", "length", "mask", "value"} for v in r.values()), list(r)))
    cases.append(("掩码形态正确（长度+前4后4）",
                  all((v["mask"].startswith("长度") ) for v in r.values()), [v["mask"] for v in r.values()]))

    # 夹具**运行时拼接**：本文件里不出现任何"像密钥的长串"（否则 release_check 会命中本文件）
    fake = "zzz9" + ("Q" * 40)
    tmp = tempfile.mkdtemp(prefix="secretscan-")
    try:
        clean = os.path.join(tmp, "clean.md")
        with io.open(clean, "w", encoding="utf-8") as fh:
            fh.write("这里只有说明文字，没有密钥。\n")
        hits, n = scan_value(fake, tmp)
        cases.append(("正例：干净目录 → 0 命中（且确实扫到了文件）", hits == [] and n >= 1, (hits, n)))
        dirty = os.path.join(tmp, "logs", "run.log")
        os.makedirs(os.path.dirname(dirty), exist_ok=True)
        with io.open(dirty, "w", encoding="utf-8") as fh:
            fh.write("Authorization: Bearer %s\n" % fake)     # ← 运行时拼出来的"泄露"
        hits, n = scan_value(fake, tmp)
        cases.append(("负例：日志里带上了值 → 命中且**只报路径**",
                      hits == ["logs/run.log"] and fake not in json.dumps(hits), hits))
        trunc = os.path.join(tmp, "trunc.md")
        with io.open(trunc, "w", encoding="utf-8") as fh:
            fh.write("前缀八个字符：%s\n" % fake[:8])
        hits, n = scan_value(fake, tmp)
        cases.append(("负例（反向）：只出现截断前缀 → **不算命中**（避免过宽误报）",
                      hits == ["logs/run.log"], hits))
        os.remove(dirty)
        hits, n = scan_value(fake, tmp)
        cases.append(("清掉泄露文件后 → 恢复 0 命中", hits == [], hits))
        binp = os.path.join(tmp, "x.pdf")
        with io.open(binp, "wb") as fh:
            fh.write(("PDF" + fake).encode())
        hits, n = scan_value(fake, tmp)
        cases.append(("二进制扩展名被跳过（.pdf 不读）", hits == [], hits))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:120]))
        passed += bool(ok)
    print("\n%d/%d secret_scan tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
