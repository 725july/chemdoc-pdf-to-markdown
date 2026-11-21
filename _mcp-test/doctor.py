# -*- coding: utf-8 -*-
"""**`doctor.py`（A1）**：只读的**环境漂移**检查——"文档说的环境" vs "这台机器实际有的"。

与 `preflight.py` 的分工（**重要**）：
  * `preflight.py` 回答"**现在能不能跑这本书**"（引擎就绪度 / 书 / 规模 / 隐私 / 阻塞项）；
  * `doctor.py` 回答"**我的环境和文档是否一致**"（解释器 / 依赖 / 夹具 / 演示证据 / 环境变量）。
  **doctor 不参与门禁、不判"能不能跑"**——否则会出现"doctor 说可以、preflight 说不行"的两套判据。

用法:
  python -X utf8 doctor.py check [--json] [--strict]
  python -X utf8 doctor.py selftest

退出码：`check` 只报漂移（`--strict` 时有漂移 → 1）；`selftest` 失败 → 1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  （工作根唯一出口，SPEC §7.10）

# 发布集真正需要的 5 个包 → 在 site-packages 里的**文件名**（不是 import 名）
DEPS = {
    "pypdfium2": "pypdfium2",
    "pypdf": "pypdf",
    "pillow": "PIL",
    "numpy": "numpy",
    "markdown-it-py": "markdown_it",
}
VENV_NAMES = ("dual-mcp", "paddle")


def _venv_dir(name: str) -> str:
    return os.path.join(paths.local_root(), "venvs", name)


def _py_in(venv: str) -> str:
    return os.path.join(venv, "Scripts", "python.exe")


def _site_packages(venv: str) -> str:
    return os.path.join(venv, "Lib", "site-packages")


def _has_pkg(venv: str, mod: str) -> bool:
    sp = _site_packages(venv)
    if not os.path.isdir(sp):
        return False
    return any(os.path.exists(os.path.join(sp, mod + ext))
               for ext in ("", ".py", ".pyd", ".so"))


def _has_any_pkg(venv: str, dist: str, mod: str) -> bool:
    sp = _site_packages(venv)
    if not os.path.isdir(sp):
        return False
    if os.path.exists(os.path.join(sp, mod)) or os.path.exists(os.path.join(sp, mod + ".py")):
        return True
    # dist-info / egg-info 也算装上了（纯 Python 包常常只有目录）
    low = dist.lower().replace("-", "_")
    return any(n.lower().replace("-", "_").startswith(low) for n in os.listdir(sp))


def check(root: str = "") -> dict:
    """返回 `{consistent: bool, drift: [...], ok: [...], env: {...}}`（**只读，不修**）。"""
    data_root = root or paths.data_root()
    out = {"data_root": data_root, "local_root": paths.local_root(),
           "drift": [], "ok": [], "env": {}}

    def drift(what: str, fix: str, detail: str = "") -> None:
        out["drift"].append({"what": what, "fix": fix, "detail": detail})

    # ① 解释器：文档默认 `dual-mcp`；只有 `paddle` 是**合法但不一致**
    have = [n for n in VENV_NAMES if os.path.isfile(_py_in(_venv_dir(n)))]
    out["env"]["venvs"] = have
    if "dual-mcp" not in have:
        drift("文档默认的解释器 `.local/venvs/dual-mcp` 不存在（当前只有：%s）"
              % ("、".join(have) or "无"),
              "跑 `install.ps1`（幂等）建出双环境；或**明确记录**你改用哪个解释器跑",
              os.path.join(paths.local_root(), "venvs"))
    # ② 依赖：对**存在的每个** venv 查 5 个包
    for n in have:
        miss = [d for d, m in DEPS.items() if not _has_any_pkg(_venv_dir(n), d, m)]
        out["env"].setdefault("deps", {})[n] = {"missing": miss}
        if miss:
            drift("venv `%s` 缺依赖：%s" % (n, "、".join(miss)),
                  "跑 `install.ps1`；或 `pip install -r requirements.txt`（该环境若没有 pip，用 `install.ps1 -Uv`）",
                  _site_packages(_venv_dir(n)))
        else:
            out["ok"].append("venv `%s` 的 5 个钉版依赖齐" % n)
    # ③ 夹具：样书模板 + fx-sample 产物
    sample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books.sample.json")
    if not os.path.exists(sample):
        drift("缺少书注册表模板 `_mcp-test/books.sample.json`",
              "重新解压发布包（该文件随包走）", sample)
    else:
        out["ok"].append("书注册表模板在")
    fx = os.path.join(data_root, "s7-out", "fx-sample", "report.json")
    if not os.path.exists(fx):
        drift("没跑过离线演示（找不到 `s7-out/fx-sample/report.json`）",
              "双击 `离线演示.cmd`（几秒，无需密钥/GPU）——这是**这台机器能起跑**的最小自检",
              fx)
    else:
        out["ok"].append("离线演示跑过（fx-sample 产物在）")
    # ④ 环境变量：设了就必须指到存在的目录
    for var in (paths.ENV_VAR, "CHEMDOC_LOCAL"):
        v = os.environ.get(var)
        out["env"][var] = v or ""
        if v and not os.path.isdir(v):
            drift("环境变量 `%s` 指向的目录不存在：%s" % (var, v),
                  "改成存在的目录，或清掉这个变量", v)
        elif v:
            out["ok"].append("`%s` = %s（存在）" % (var, v))
    # ⑤ 路径编码：本地 Paddle 在中文路径下可能打不开模型（A4 同源；只提示）
    bad = [p for p in (data_root, paths.local_root())
           if any(ord(ch) > 127 for ch in p)]
    if bad:
        drift("路径含非 ASCII 字符（本地 Paddle 可能报 `Cannot open file …inference.json`）",
              "换纯英文工作区，或用 `subst X:` 映射后把 `CHEMDOC_LOCAL` 指到 `X:\\.local`",
              bad[0])
    out["consistent"] = not out["drift"]
    return out


def render(res: dict) -> str:
    L = ["【环境】工具根=%s" % paths.TOOLS_ROOT,
         "        数据根=%s" % res["data_root"],
         "        本地根=%s" % res["local_root"]]
    for k in res["ok"]:
        L.append("  ok   %s" % k)
    for d in res["drift"]:
        L.append("  漂移 %s" % d["what"])
        L.append("       补法：%s" % d["fix"])
    L.append("【结论】%s" % ("**一致**（与文档的环境假设没有偏差）" if res["consistent"]
                          else "**漂移 %d 处**（照上面每条补；不影响已能跑的环境）" % len(res["drift"])))
    L.append("       注：能不能跑某本书仍以 `preflight.py --book <key>` 为准（doctor 不判这个）。")
    return "\n".join(L)


def cmd_check(a) -> int:
    res = check()
    print(render(res))
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 1 if (a.strict and not res["consistent"]) else 0


def selftest() -> int:
    cases = []
    real = check()
    # 机制断言（不依赖本机装没装）：字段齐、结论自洽
    cases.append(("结论与漂移列表自洽（有漂移 ⇔ consistent=False）",
                  (not real["drift"]) == bool(real["consistent"]), real["consistent"]))
    cases.append(("报告里给每条漂移**都带补法**",
                  all(d.get("fix") for d in real["drift"]), real["drift"][:1]))
    # 反例：造一个假工作根，`CHEMDOC_LOCAL` 指向不存在的目录 → 必须报漂移
    import tempfile
    td = tempfile.mkdtemp(prefix="doctor-")
    old = os.environ.get("CHEMDOC_LOCAL")
    os.environ["CHEMDOC_LOCAL"] = os.path.join(td, "nope")
    try:
        r2 = check(root=td)
    finally:
        if old is None:
            os.environ.pop("CHEMDOC_LOCAL", None)
        else:
            os.environ["CHEMDOC_LOCAL"] = old
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    cases.append(("负例：`CHEMDOC_LOCAL` 指向不存在的目录 → 报漂移并给补法",
                  any("CHEMDOC_LOCAL" in d["what"] and d.get("fix") for d in r2["drift"]),
                  [d["what"] for d in r2["drift"]]))
    cases.append(("负例：没跑过离线演示 → 如实报（不假装一致）",
                  any("离线演示" in d["what"] for d in r2["drift"]),
                  [d["what"] for d in r2["drift"]]))
    cases.append(("渲染里有结论行，且**明确说不判能不能跑**（避免第二套判据）",
                  "【结论】" in render(real) and "preflight" in render(real), None))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:140]))
        passed += bool(ok)
    print("\n%d/%d doctor tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--json", action="store_true")
    c.add_argument("--strict", action="store_true", help="有漂移就 exit 1（默认只报）")
    sub.add_parser("selftest")
    a = ap.parse_args()
    return selftest() if a.cmd == "selftest" else cmd_check(a)


if __name__ == "__main__":
    sys.exit(main())
