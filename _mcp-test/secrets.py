# -*- coding: utf-8 -*-
"""**`secrets.py`（A2）**：密钥的**用户自助入口**（agent 不读、不代填）。

口径（用户 2026-10-13）：**不进上下文 / 不上传 / agent 不读只引用**。
- 值只写进 **`%USERPROFILE%\\.chemdoc\\secrets.json`**（工作区**之外**，ACL 只留本人）；
- 任何输出**只有存在性/长度/掩码**；
- `list` 的"需要哪些变量名"来自 `engines.json` 里各引擎 **cloud 模式**的 `cred_env`（本地模式不凭证）；
- `gui` 是给用户双击用的**掩码小窗**（`输入密钥.cmd` → 本命令）；
- **不代填、不自动测试联网**：真实验证发生在第一次真跑（A3），这里只做**格式检查**（长度/前缀/空白）。

用法: python -X utf8 secrets.py list | set <NAME> | gui | selftest
"""
from __future__ import annotations

import getpass
import io
import json
import os
import subprocess
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.TOOLS_ROOT
DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".chemdoc", "secrets.json")
NAME_RE = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")


class SecretError(ValueError):
    """密钥操作不合法（**必须报错**）。"""


def path() -> str:
    return os.environ.get("CHEMDOC_SECRETS") or DEFAULT_PATH


def load_file(p: str = "") -> dict:
    fp = p or path()
    if not os.path.exists(fp):
        return {}
    try:
        d = json.load(io.open(fp, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _lock_acl(fp: str) -> bool:
    """尽力把 ACL 收紧到"只当前用户"（失败不算致命——文件在工作区之外，且我们会如实报告）。"""
    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return False
        subprocess.run(["icacls", fp, "/inheritance:r", "/grant:r", "%s:F" % user],
                       capture_output=True, text=True, timeout=60)
        return True
    except Exception:                                                            # noqa: BLE001
        return False


def save_file(data: dict, p: str = "") -> dict:
    fp = p or path()
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, fp)
    return {"path": fp, "acl_locked": _lock_acl(fp), "keys": sorted(data)}


def mask(v: str) -> str:
    if not v:
        return "(空)"
    return "长度 %d 掩码 %s…%s" % (len(v), v[:4], v[-4:]) if len(v) >= 12 else "长度 %d（偏短）" % len(v)


def needed_names() -> list:
    """从 `engines.json` 收集"需要凭证的变量名"（只 cloud 模式有 cred_env）。"""
    try:
        import engines as engines_mod
        d = engines_mod.load()
        names = set()
        for e in d["engines"].values():
            for m in (e.get("modes") or {}).values():
                if m.get("cred_env"):
                    names.add(m["cred_env"])
        return sorted(names)
    except Exception:                                                            # noqa: BLE001
        return []


def status(name: str) -> dict:
    try:
        import secret_scan
        r = secret_scan.resolve((name,))[name]
        return {"name": name, "set": bool(r["value"]), "source": r["source"], "mask": r["mask"]}
    except Exception:                                                            # noqa: BLE001
        v = os.environ.get(name) or ""
        return {"name": name, "set": bool(v), "source": "进程环境", "mask": mask(v)}


def fmt_check(v: str) -> dict:
    """**格式检查**（不做联网验证——真实验证在第一次真跑）。"""
    return {"length": len(v), "has_whitespace": any(c.isspace() for c in v),
            "prefix_sk": v.startswith("sk-"),
            "note": "只做格式检查；**联网验证发生在第一次真跑（A3）**"}


def cmd_list(_a) -> int:
    names = needed_names()
    print("需要凭证的变量名（来自 engines.json 的 cloud 模式）：", "、".join(names) or "（无）")
    print("密钥文件：%s（%s）" % (path(), "存在" if os.path.exists(path()) else "不存在"))
    for n in names:
        s = status(n)
        print("  %-22s %s（来源：%s）" % (n, ("已设置 %s" % s["mask"]) if s["set"] else "**未设置**", s["source"]))
    return 0


def cmd_set(a) -> int:
    n = a.name
    if not NAME_RE.match(n):
        raise SecretError("变量名不合法（大写字母/数字/下划线）：%r" % n)
    v = getpass.getpass("粘贴 %s（输入不回显）：" % n).strip()
    if not v:
        raise SecretError("输入为空，未写入")
    data = load_file()
    data[n] = v
    info = save_file(data)
    print("已保存：%s（%s）" % (info["path"], "ACL 已收紧" if info["acl_locked"] else "ACL 收紧失败（不影响使用）"))
    print("  %s → %s | 格式检查 %s" % (n, mask(v), json.dumps(fmt_check(v), ensure_ascii=False)))
    print("  （值未回显、未进日志；agent 不读该文件）")
    return 0


def cmd_gui(_a) -> int:
    """掩码小窗：给用户双击 `输入密钥.cmd` 用。"""
    import tkinter as tk
    from tkinter import messagebox
    names = needed_names() or ["MINERU_API_TOKEN"]
    root = tk.Tk()
    root.title("输入密钥（值不会进对话/日志）")
    tk.Label(root, text="密钥只写入：\n%s\n（工作区之外；只留本人 ACL）" % path(),
             justify="left", fg="#333").pack(padx=12, pady=(10, 4), anchor="w")
    vars_ = {}
    for n in names:
        row = tk.Frame(root)
        row.pack(fill="x", padx=12, pady=3)
        tk.Label(row, text=n, width=22, anchor="w").pack(side="left")
        e = tk.Entry(row, width=44, show="•")
        e.pack(side="left")
        st = status(n)
        tk.Label(row, text=("已设置 %s" % st["mask"]) if st["set"] else "未设置", fg="#666").pack(side="left", padx=6)
        vars_[n] = e

    def on_save():
        data = load_file()
        changed = []
        for n, e in vars_.items():
            v = e.get().strip()
            if v:
                data[n] = v
                changed.append((n, v))
        if not changed:
            messagebox.showinfo("提示", "没有输入新值（已有的不会被清掉）")
            return
        info = save_file(data)
        msg = ["已保存：%s" % info["path"], "ACL：%s" % ("已收紧" if info["acl_locked"] else "收紧失败（不影响使用）"), ""]
        for n, v in changed:
            msg.append("%s → %s" % (n, mask(v)))
            msg.append("  格式检查：%s" % json.dumps(fmt_check(v), ensure_ascii=False))
        msg.append("")
        msg.append("联网验证发生在第一次真跑（A3）。")
        for n, _ in changed:
            vars_[n].delete(0, "end")
        messagebox.showinfo("完成", "\n".join(msg))

    tk.Button(root, text="保存", width=14, command=on_save).pack(pady=10)
    tk.Label(root, text="提示：值不会回显、不会进日志、不会被 agent 读取；保存后可直接关窗。",
             fg="#666").pack(padx=12, pady=(0, 10))
    root.mainloop()
    return 0


def selftest() -> int:
    import tempfile
    cases = []
    tmp = tempfile.mkdtemp(prefix="secrets-")
    fp = os.path.join(tmp, "sub", "secrets.json")
    try:
        cases.append(("初始：文件不存在 → 空 dict", load_file(fp) == {}, None))
        info = save_file({"MINERU_API_TOKEN": "x" * 30}, fp)
        cases.append(("保存：建目录并写入、返回 keys 与 ACL 状态",
                      os.path.exists(fp) and info["keys"] == ["MINERU_API_TOKEN"], info))
        cases.append(("读回：round-trip 一致", load_file(fp)["MINERU_API_TOKEN"] == "x" * 30, None))
        cases.append(("掩码：只给长度与前4后4，**不含**完整值",
                      (lambda m: m.startswith("长度 30") and "x" * 30 not in m)(mask("x" * 30)), mask("x" * 30)))
        cases.append(("格式检查：空白与 sk- 前缀能被识别",
                      fmt_check("sk-abc").get("prefix_sk") is True and fmt_check("a b").get("has_whitespace") is True, None))
        cases.append(("需要哪些变量名：来自 engines.json 的 cloud 模式（当前含 MINERU_API_TOKEN）",
                      "MINERU_API_TOKEN" in needed_names(), needed_names()))
        # 负例：变量名不合法必须报错（命令行路径）
        class A:
            name = "bad-name"
        try:
            cmd_set(A())
            cases.append(("负例：变量名非法 → 报错", False, None))
        except SecretError:
            cases.append(("负例：变量名非法 → 报错", True, None))
        # 负例：损坏的 JSON → 当作空（不崩、不覆盖）
        with io.open(fp, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 JSON")
        cases.append(("负例：文件损坏 → 读成空 dict（不崩）", load_file(fp) == {}, None))
        cases.append(("状态查询：只返回存在性/来源/掩码三样",
                      set(status("MINERU_API_TOKEN")) == {"name", "set", "source", "mask"}, status("MINERU_API_TOKEN")))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:120]))
        passed += bool(ok)
    print("\n%d/%d secrets tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    s = sub.add_parser("set")
    s.add_argument("name")
    s.set_defaults(func=cmd_set)
    sub.add_parser("gui").set_defaults(func=cmd_gui)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
