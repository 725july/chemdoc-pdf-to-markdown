# -*- coding: utf-8 -*-
"""B2 · 工作根（**工具 / 数据 / 密钥** 三分离）—— SPEC §7.10。

两个锚点，**唯一出口**（其它模块不许再自己 `dirname(dirname(__file__))`）：

  * `TOOLS_ROOT`  —— 本文件所在目录（`_mcp-test`）的上一级：**代码 + 文档 + 环境**
                     （venv / 模型 / 缓存 / 依赖清单）。由 `__file__` 推出，**永不可覆盖**。
  * `data_root()` —— **数据**（输入切片 PDF + 各阶段产物与台账：`_test-inputs`、`s0-profiles`…
                     `s7-out`、`ocr-raw`、`golden`、`errata`）。
                     优先级 **显式参数 → 环境变量 `CHEMDOC_ROOT` → 默认 = `TOOLS_ROOT`**。

**为什么默认等于 TOOLS_ROOT**：那是 B2 之前的行为 —— 默认不变 → 现有产物、四本归档基线与
全套回归**逐字节不受影响**；要让"工具"与"数据"分家，才显式指定（参数或环境变量）。

**为什么必须集中**：B2 之前 23 处各自推导根，其中一个 `ROOT` 同时当"代码根"和"数据根"用
（`books.json` 的路径解析根基 = `_mcp-test` 的上一级）→ 换机器/换目录就会写错地方。
**硬校验（不静默兜底）**：显式或环境给的根**必须存在且是目录**，否则 `DataRootError`，
消息里**说明这个值从哪来**（参数还是环境变量）。

用法:
  python -X utf8 paths.py show        # 打印两个锚点与来源（`preflight` / B2 验收也用它）
  python -X utf8 paths.py selftest
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))          # = `_mcp-test`
TOOLS_ROOT = os.path.dirname(MODULE_DIR)                         # = 工具/仓库根（含 docs、dual-mcp-bridge、venv）
ENV_VAR = "CHEMDOC_ROOT"
DEFAULT = TOOLS_ROOT


class DataRootError(RuntimeError):
    """数据根不合法（**必须报错**：宁可停下，也不要把产物写到别处）。"""


def normalize(p: str) -> str:
    """绝对化 + 规范化（相对路径按**当前工作目录**解析）。"""
    return os.path.normpath(os.path.abspath(str(p)))


def env_value() -> str:
    return (os.environ.get(ENV_VAR) or "").strip()


def source(explicit: str | None = None) -> str:
    """这次的数据根**从哪来**（给人看的短句）。"""
    if explicit:
        return "参数（--artifact-root / --out-root / --root）"
    if env_value():
        return "环境变量 %s=%s" % (ENV_VAR, env_value())
    return "默认（工具根；没有设 %s）" % ENV_VAR


def data_root(explicit: str | None = None) -> str:
    """解析**数据根**：显式参数 → 环境变量 → 默认（工具根）。给的根必须存在且是目录。"""
    if explicit:
        p, why = normalize(explicit), "参数"
    elif env_value():
        p, why = normalize(env_value()), "环境变量 %s" % ENV_VAR
    else:
        return DEFAULT
    if not os.path.exists(p):
        raise DataRootError("数据根不存在：%s（来自%s）。改掉它，或把 %s 指向已有的数据目录。"
                            % (p, why, ENV_VAR if why.startswith("环境") else "参数"))
    if not os.path.isdir(p):
        raise DataRootError("数据根必须是目录，不是文件：%s（来自%s）。" % (p, why))
    return p


DATA_MARKERS = ("s7-out", "s6-out", "s5-out", "s2-ir", "ocr-raw")


def has_data(explicit: str | None = None) -> bool:
    """**这份拷贝里有没有"数据树"**（产物 / 引擎原生产物）。

    给自测、预检与回归用，口径一句话：**数据不在 ⇒ 与数据有关的断言明示跳过（不假装跑过）；
    数据在而缺 ⇒ 仍然 FAIL**。判据刻意宽松 —— 只要出现任意一个产物目录就算有数据；
    源切片 PDF（`_test-inputs`）**单独存在不算**（它只是输入，不能证明引擎跑过）。
    """
    try:
        root = data_root(explicit)
    except DataRootError:
        return False
    return any(os.path.isdir(os.path.join(root, m)) for m in DATA_MARKERS)


LOCAL_DIR = ".local"            # **机器本地物**的唯一去处（venv / 模型缓存 / uv 缓存 / 临时目录）
ENV_LOCAL = "CHEMDOC_LOCAL"      # 想把它放到别的盘（比如 SSD）就设这个


def local_root(explicit: str | None = None) -> str:
    """**机器本地物**的根目录（默认 `<工具根>/.local`）。

    为什么单独一个目录：venv、模型缓存、uv 缓存、临时目录都**不是产品内容**，也不该散落在工具树顶层——
    否则"装一次"就把安装目录弄脏，排除清单/保护清单还得一条条列。收进一个目录后：
    产品树顶层只多 `.local/` 一项；想备份/搬家/清空，也只对这一个目录动手。
    优先级：explicit → `CHEMDOC_LOCAL` → `<工具根>/.local`。
    """
    env = (os.environ.get(ENV_LOCAL) or "").strip()
    return normalize(explicit or env or os.path.join(TOOLS_ROOT, LOCAL_DIR))


def local_sub(*parts) -> str:
    """`<本地根>/<parts…>`（**不创建**）。"""
    return os.path.join(local_root(), *[str(p) for p in parts if str(p)])


def ensure_local(*parts) -> str:
    """`<本地根>/<parts…>`，顺手创建（**默认权限**）。"""
    p = local_sub(*parts)
    os.makedirs(p, exist_ok=True)
    return p


def tmp_dir(prefix: str = "paths-tmp-") -> str:
    """给**自测/临时物**用的临时目录（优先 `<本地根>/tmp`，造不出来才退回系统临时目录）。

    为什么不用裸 `tempfile.mkdtemp()`：受限环境（沙箱 / CI / 只读系统盘）里系统 `%TEMP%` 常常**拒写**，
    自测会当场 `PermissionError`（实测：干净 venv 里跑 `paths.py selftest` 就撞上）。
    为什么放在本地根下：临时目录也是**机器本地物**，不该散落到安装目录顶层（SPEC §7.10）。
    （**辅助手段不得杀死主流程**，SPEC §7-9：兜底仍退回系统临时目录。）
    """
    base = local_sub("tmp")
    try:
        os.makedirs(base, exist_ok=True)
        return _mkdtemp_default_mode(base, prefix)
    except OSError:
        return tempfile.mkdtemp(prefix=prefix)


def _mkdtemp_default_mode(base: str, prefix: str) -> str:
    """在 `base` 下建临时目录，**用默认权限**（不要 `tempfile` 的 0o700）。

    为什么不用现成的 `tempfile.mkdtemp`：它用 `os.mkdir(..., 0o700)` 建目录，而**受限沙箱**
    （本机 DSH Windows 沙箱实测）会**拒绝写入以 0o700 创建的目录** → 自测当场 `PermissionError`。
    同一父目录下用**默认权限**建的目录可以正常写入。这里自己起名 + `os.mkdir`（不带 mode），
    并保留"建不出来就退回系统临时目录"的降级（SPEC §7-9：辅助手段不得杀死主流程）。
    """
    import random
    import string
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(20):
        p = os.path.join(base, prefix + "".join(random.choices(alphabet, k=8)))
        try:
            os.mkdir(p)                                              # ← 不带 mode
            return p
        except FileExistsError:
            continue
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def explain(explicit: str | None = None) -> dict:
    """给 `preflight` / 验收用：两个锚点 + 来源 + （若坏）错因。**不抛异常。**"""
    out = {"tools_root": TOOLS_ROOT, "module_dir": MODULE_DIR, "env_var": ENV_VAR,
           "data_root": "", "data_root_source": source(explicit), "error": ""}
    try:
        out["data_root"] = data_root(explicit)
    except DataRootError as exc:                                     # 把"坏根"如实报出来，不崩
        out["error"] = str(exc)
    return out


# ---------------------------------------------------------------- CLI
def cmd_show(_a) -> int:
    e = explain()
    print("工具根（代码/文档/环境，不可覆盖）：", e["tools_root"])
    print("数据根（输入与产物）            ：", e["data_root"] or "（**无效**）")
    print("数据根来源                      ：", e["data_root_source"])
    if e["error"]:
        print("错误                            ：", e["error"])
    print("（要分家：设 %s=D:\\chemdoc-data，或用各命令的 --artifact-root/--out-root）" % ENV_VAR)
    return 1 if e["error"] else 0


def selftest() -> int:
    cases: list[tuple] = []
    saved = os.environ.get(ENV_VAR)

    def _env(v):
        if v is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = v

    try:
        _env(None)
        old_style = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cases.append(("默认 = 工具根，**且等于 B2 之前的老写法**（零行为变化）",
                      data_root() == TOOLS_ROOT == old_style, {"data_root": data_root()}))
        tmp = tmp_dir("paths-selftest-")
        _env(tmp)
        cases.append(("%s 生效（解析为绝对路径）" % ENV_VAR, data_root() == normalize(tmp), data_root()))
        _env(tmp)
        _a1 = data_root()
        _env(tmp + os.sep + ".")
        _a2 = data_root()
        cases.append(("**不缓存**：改环境变量立刻生效（`.`/`..` 被规范化）",
                      _a1 == _a2 == normalize(tmp), (_a1, _a2)))
        cases.append(("显式参数**优先**于环境变量",
                      data_root(TOOLS_ROOT) == TOOLS_ROOT and source(TOOLS_ROOT).startswith("参数"), None))
        _env(os.path.join(tmp, "does-not-exist"))
        try:
            data_root()
            cases.append(("负例：环境变量指向不存在的目录 → 必须报错", False, "没报错"))
        except DataRootError as exc:
            cases.append(("负例：环境变量指向不存在的目录 → `DataRootError` 且**说明值从哪来**",
                          ENV_VAR in str(exc) and "不存在" in str(exc), str(exc)[:80]))
        cases.append(("显式参数给了好根时**不看**坏环境变量（显式最高优先）",
                      data_root(tmp) == normalize(tmp), None))
        cases.append(("坏环境变量下 `explain()` **不抛异常**、把错因写在 error 里",
                      bool(explain()["error"]) and explain()["data_root"] == "", explain()))
        _env(None)
        f = os.path.join(tmp, "a-file.txt")
        open(f, "w", encoding="utf-8").write("x")
        try:
            data_root(f)
            cases.append(("负例：根指向**文件** → 必须报错", False, "没报错"))
        except DataRootError as exc:
            cases.append(("负例：根指向**文件**（不是目录）→ `DataRootError`", "必须是目录" in str(exc), str(exc)[:80]))
        _rel = os.path.join(tmp, "reldir")
        os.makedirs(_rel, exist_ok=True)
        _cwd = os.getcwd()
        try:
            os.chdir(tmp)
            _env("reldir")
            cases.append(("相对路径按 CWD 解析成绝对路径",
                          os.path.isabs(data_root()) and data_root() == normalize("reldir"), data_root()))
        finally:
            os.chdir(_cwd)
            _env(None)
        e = explain()
        cases.append(("`explain()` 键齐（tools_root/module_dir/data_root/source/env_var/error）",
                      all(k in e for k in ("tools_root", "module_dir", "data_root", "data_root_source",
                                           "env_var", "error")) and e["data_root"] == TOOLS_ROOT, sorted(e)))
        # 注意：**不能**断言"本树一定有数据"——新鲜下载的副本里就是没有（B2 实测：这条自己会把副本判红）。
        # 只断言"判据与 DATA_MARKERS 一致、坏根不抛异常、只认产物目录（不把 _test-inputs 当数据）"。
        _expect = any(os.path.isdir(os.path.join(TOOLS_ROOT, m)) for m in DATA_MARKERS)
        cases.append(("`local_root()`：默认 = `<工具根>/.local`；`CHEMDOC_LOCAL` 可覆盖；`tmp_dir()` 落在它下面",
                      local_root() == os.path.join(TOOLS_ROOT, LOCAL_DIR)
                      and os.path.dirname(os.path.dirname(tmp_dir())) == local_root(),
                      {"local_root": local_root(), "tmp": tmp_dir()}))
        cases.append(("`has_data()`：判据 == 「工具根下有没有 DATA_MARKERS 目录」；坏根 → False 不抛异常；"
                      "**只认产物目录**（`_test-inputs` 之类的纯输入不算）",
                      has_data() == _expect and has_data(os.path.join(tmp, "nope")) is False
                      and "s7-out" in DATA_MARKERS and "_test-inputs" not in DATA_MARKERS, has_data()))
        cases.append(("自洽：`_mcp-test` 就在工具根下、`paths.py` 就在模块目录里",
                      os.path.isdir(os.path.join(TOOLS_ROOT, "_mcp-test"))
                      and os.path.isfile(os.path.join(MODULE_DIR, "paths.py")), None))
        cases.append(("默认来源那句话里写明「没有设环境变量」（免得误以为被覆盖）",
                      "没有设" in source(), source()))
    finally:
        if saved is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = saved
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print("\n%d/%d paths tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 工作根（工具 / 数据 分离）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
