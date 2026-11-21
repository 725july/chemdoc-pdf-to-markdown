# -*- coding: utf-8 -*-
"""**引擎槽位（A2）**：`engines.json` 的加载、硬校验、**实测就绪度**与四组合矩阵。

设计要点：
  * **两个槽位对称**：`slots.primary` / `slots.witness` 各自选 `engine × mode`，`mode ∈ {local, cloud}`
    —— **MinerU 也能本地跑**，所以"专业 GPU 双本地"只是换 `mode` 的事；
  * **S2 之后不关心引擎跑在哪**（只读目录）→ 换模式**不改管线**；
  * **硬校验**：cloud 必须给 `cred_env`、**local 必须 `cred_env=null`**、`cred_env` 必须是**变量名**
    （配置里永不出现密钥值）、未知字段/引擎/模式/适配器 → 报错；
  * **就绪度是实测的**：runner 是否存在 / 本地模式的**空闲显存**是否够 / 包是否装 / 模型目录 / 磁盘余量；
    凭证**只报存在性/长度/掩码**（复用 `secret_scan.resolve`）。

用法: python -X utf8 engines.py list | check | matrix | selftest
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import paths  # B2：工作根唯一出口（SPEC §7.10）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = paths.TOOLS_ROOT
DEFAULT_PATH = os.path.join(HERE, "engines.json")
TOP_KEYS = ("schema", "note", "slots", "engines", "combos")
SLOT_KEYS = ("engine", "mode", "adapter", "why")
MODE_KEYS = ("runner", "cred_env", "status", "requires", "python", "env", "note")
CRED_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
STATUSES = ("ready", "not-installed", "planned")


class EngineConfigError(ValueError):
    """引擎配置不合法（**必须报错**，不许静默兜底）。"""


def load(path: str = "") -> dict:
    p = path or DEFAULT_PATH
    with io.open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    errs = []
    unk = [k for k in d if k not in TOP_KEYS]
    if unk:
        errs.append("顶层未知字段：%s" % unk)
    if d.get("schema") != 1:
        errs.append("schema 必须是 1")
    eng, slots = d.get("engines"), d.get("slots")
    if not isinstance(eng, dict) or not eng:
        errs.append("engines 必须是非空对象")
    if not isinstance(slots, dict) or "primary" not in slots:
        errs.append("slots 必须含 primary")
    for name, e in (eng or {}).items():
        for k in ("native_layout", "adapters", "modes"):
            if k not in e:
                errs.append("engines.%s 缺 %s" % (name, k))
        for mname, m in (e.get("modes") or {}).items():
            if mname not in ("local", "cloud"):
                errs.append("engines.%s.modes.%s：未知模式（只许 local/cloud）" % (name, mname))
            muk = [k for k in m if k not in MODE_KEYS]
            if muk:
                errs.append("engines.%s.modes.%s 未知字段：%s" % (name, mname, muk))
            ce = m.get("cred_env")
            if mname == "cloud" and not ce:
                errs.append("engines.%s.modes.cloud 必须给 cred_env（变量名）" % name)
            if mname == "local" and ce is not None:
                errs.append("engines.%s.modes.local 的 cred_env 必须是 null（本地引擎不凭证）" % name)
            if ce and not CRED_NAME_RE.match(str(ce)):
                errs.append("engines.%s.modes.%s 的 cred_env 必须是**变量名**（大写字母/数字/下划线），"
                            "疑似把密钥值写进了配置：%r" % (name, mname, str(ce)[:12]))
            if m.get("status") and m["status"] not in STATUSES:
                errs.append("engines.%s.modes.%s 的 status 必须是 %s" % (name, mname, STATUSES))
    for sname in ("primary", "witness"):
        s = (slots or {}).get(sname)
        if s is None:
            if sname == "primary":
                errs.append("slots.primary 不能为 null")
            continue
        suk = [k for k in s if k not in SLOT_KEYS]
        if suk:
            errs.append("slots.%s 未知字段：%s" % (sname, suk))
        e = (eng or {}).get(s.get("engine"))
        if not e:
            errs.append("slots.%s.engine 未知：%r" % (sname, s.get("engine")))
            continue
        if s.get("adapter") not in (e.get("adapters") or []):
            errs.append("slots.%s.adapter 必须是 %s 之一：%r" % (sname, e.get("adapters"), s.get("adapter")))
        if s.get("mode") not in (e.get("modes") or {}):
            errs.append("slots.%s.mode 未知：%r（该引擎有 %s）"
                        % (sname, s.get("mode"), sorted((e.get("modes") or {}).keys())))
    if errs:
        raise EngineConfigError("引擎配置不合法：\n  - " + "\n  - ".join(errs))
    return d


def resolve_slot(d: dict, name: str) -> dict:
    """把槽位展开成"可直接执行"的描述（runner 绝对路径、python 绝对路径、env、requires）。"""
    s = d["slots"].get(name)
    if s is None:
        return {"slot": name, "engine": None, "mode": None, "null": True,
                "note": "witness=null：**合法降级**（跨页归属类检查转 unverified）"}
    e = d["engines"][s["engine"]]
    m = e["modes"][s["mode"]]
    def _abs(p):
        """相对路径一律相对**工具根**解析；但 `.local/...` 这种**机器本地物**要相对 `paths.local_root()`，
        这样用户用 `CHEMDOC_LOCAL` 把本地物挪到别的盘时，槽位的 venv / 模型缓存 / TEMP 也跟着走。"""
        if not p:
            return None
        s = str(p).replace("\\", "/")
        if s == paths.LOCAL_DIR or s.startswith(paths.LOCAL_DIR + "/"):
            rest = s[len(paths.LOCAL_DIR):].lstrip("/")
            return os.path.join(paths.local_root(), *[x for x in rest.split("/") if x])
        return os.path.join(ROOT, *s.split("/"))
    py = m.get("python")
    return {"slot": name, "engine": s["engine"], "mode": s["mode"], "adapter": s["adapter"],
            "runner": _abs(m.get("runner")), "runner_rel": m.get("runner"),
            "status": m.get("status"), "cred_env": m.get("cred_env"),
            "requires": m.get("requires") or {}, "python": _abs(py) if py else None,
            "env": {k: _abs(v) if isinstance(v, str) and ("/" in v or "\\" in v or v.startswith(".")) and not v.isdigit()
                    else v for k, v in (m.get("env") or {}).items()},
            "note": m.get("note"), "native_layout": e.get("native_layout")}


def slot_env(slot: str) -> dict:
    """槽位声明的**运行环境**（已解析成绝对路径）。给"代码自动设环境"用（不靠人手设、不靠改 json）。"""
    return dict(resolve_slot(load(), slot).get("env") or {})


def find_slot(spec: str) -> str:
    """把 `primary` / `witness` / `paddle:local` / `mineru:cloud` 都解析成**槽位名**。"""
    d = load()
    if spec in d["slots"]:
        return spec
    if ":" in spec:
        eng, mode = spec.split(":", 1)
        for name, s in d["slots"].items():
            if s.get("engine") == eng and s.get("mode") == mode:
                return name
    raise KeyError("找不到槽位：%s（可用：%s；或 engine:mode 如 paddle:local）"
                   % (spec, ", ".join(sorted(d["slots"]))))


def cmd_run(a) -> int:
    """**跑引擎**：自动带上槽位的 python 与 env（`engines.json` 里声明的那些）。

    为什么要有这条命令：`env`（如 `USERPROFILE` / `TEMP` / `PYTHONUTF8`）声明在配置里，
    但以前**没有任何代码消费它** —— 结果要么人来设（易漏、易错），要么 agent 设（更不该）。
    现在由这里统一注入子进程环境；`runners/paddle_local.py` 里还有一层自保（直接喊 runner 也能跑）。
    """
    try:
        slot = find_slot(a.slot)
    except KeyError as e:
        print(str(e)); return 2
    sl = resolve_slot(load(), slot)
    if sl.get("null"):
        print("槽位 %s 是 null（合法降级）→ 没有可跑的 runner" % slot); return 2
    runner, py = sl.get("runner"), sl.get("python")
    if not runner or not os.path.exists(runner):
        print("runner 不存在：%s" % runner); return 2
    # 槽位里的 `python` 是**venv 目录**（runner 自己会拼 `Scripts/python.exe`）→ 这里补全成可执行文件
    exe = py if (py and str(py).lower().endswith(".exe")) else (os.path.join(str(py), "Scripts", "python.exe") if py else "")
    if not exe or not os.path.isfile(exe):
        print("解释器不存在：%s（先跑 install.ps1；venv 都在 <工具根>/%s/venvs/）" % (exe or py, paths.LOCAL_DIR))
        return 2
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (sl.get("env") or {}).items()})
    # **绝不往 PYTHONPATH 里塞 `_mcp-test`**：实测它会顶掉标准库 `secrets`（numpy 起不来）。
    # runner 自己解决导入（脚本目录在 sys.path[0]；见 paddle_local.py 的说明）。
    cmd = [exe, "-X", "utf8", runner, "--book", a.book] + list(a.rest or [])
    print("[engines] %s:%s（runner=%s）" % (sl["engine"], sl["mode"], os.path.relpath(runner, ROOT)))
    print("[engines] 自动设的环境：%s" % (sl.get("env") or "（该槽位不需要）"))
    print("[engines] 解释器：%s" % os.path.relpath(exe, ROOT))
    r = subprocess.run(cmd, cwd=ROOT, env=env)
    return int(r.returncode)


# ---------------------------------------------------------------- 实测指标
def vram_free_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        tot, free = [int(x) for x in out.split("\n")[0].split(",")[:2]]
        return {"total": tot, "free": free}
    except Exception:                                                            # noqa: BLE001
        return None


def disk_free_gb(path: str = ""):  # 默认查**数据根**（产物写在那里）
    try:
        import shutil
        return round(shutil.disk_usage(path or paths.data_root()).free / (1024 ** 3), 1)
    except Exception:                                                            # noqa: BLE001
        return None


def _cred_status(name: str) -> dict:
    try:
        import secret_scan
        r = secret_scan.resolve((name,))[name]
        return {"set": bool(r["value"]), "length": r["length"], "mask": r["mask"], "source": r["source"]}
    except Exception:                                                            # noqa: BLE001
        v = os.environ.get(name) or ""
        return {"set": bool(v), "length": len(v), "mask": "(掩码不可用)", "source": "进程环境"}


def _pkg_installed(py: str, pkg: str):
    if not py or not os.path.exists(py):
        return None
    try:
        r = subprocess.run([py, "-c", "import %s" % pkg], capture_output=True, text=True, timeout=90)
        return r.returncode == 0
    except Exception:                                                            # noqa: BLE001
        return None


def runner_cli_contract(path: str) -> dict:
    """**静态**探测 runner 的 CLI 契约（A3）：模块级 `main()` + argparse + **`--book` 入口**。

    **只读文件、不执行 runner**（执行会真跑引擎/要凭证）。非 .py（如 .cmd/.ps1）→ `n/a`（不判）。
    口径见 SPEC「runner 契约（A3 建）」：CLI 形如 `--book <key>`，这样「换引擎/换部署」只需改配置。
    **注意**：`has_main + argparse` 只是必要条件——MCP wrapper 也满足这两条，但它没有 `--book`，
    所以**不能**被管线编排（这正是 A3 当初的坑，判据必须能把它挑出来）。
    """
    out = {"path": path, "checked": False, "has_main": False, "argparse": False,
           "book_arg": False, "ok": None}
    if not path or not str(path).lower().endswith(".py") or not os.path.exists(path):
        out["note"] = "非 .py 或不存在的 runner → 不做静态契约探测"
        return out
    out["checked"] = True
    try:
        import ast
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except Exception as exc:                                                     # noqa: BLE001
        out["error"] = "无法解析：%s" % str(exc)[:120]
        out["ok"] = False
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            out["has_main"] = True
    out["argparse"] = ("import argparse" in src) or ("argparse." in src)
    out["book_arg"] = ('"--book"' in src) or ("'--book'" in src)
    out["ok"] = bool(out["has_main"] and out["argparse"] and out["book_arg"])
    return out


def readiness(sl: dict) -> dict:
    """实测就绪度：返回 {ready, blockers:[{what, fix}], checks:{…}}。**只报掩码，不打印值。**"""
    if sl.get("null"):
        return {"ready": True, "blockers": [], "checks": {"slot": "null（合法降级）"}}
    blockers, checks = [], {}
    rp = sl.get("runner")
    checks["runner"] = {"path": sl.get("runner_rel"), "exists": bool(rp and os.path.exists(rp))}
    if not checks["runner"]["exists"]:
        blockers.append({"what": "runner 不存在：%s" % sl.get("runner_rel"),
                         "fix": "建该 runner（本地引擎需实现「读引擎→落原生输出」），或把 slots 的 mode 改回可用的那条"})
    else:
        # A3：runner 存在还不够，还要**是条能编排的 CLI**（stdio MCP 服务不能被管线调用）
        cli = runner_cli_contract(rp)
        checks["runner_cli"] = cli
        if cli.get("ok") is False:
            blockers.append({"what": "runner 不是可编排 CLI（缺 main() / argparse / `--book` 入口）：%s" % sl.get("runner_rel"),
                             "fix": "按 SPEC「runner 契约」加薄 CLI（`--book <key>`）：复用引擎封装、自己落原生输出"})
    if sl["mode"] == "cloud":
        cs = _cred_status(sl["cred_env"])
        checks["credential"] = {"env": sl["cred_env"], "set": cs["set"], "source": cs["source"], "mask": cs["mask"]}
        if not cs["set"]:
            blockers.append({"what": "云端引擎缺少凭证 %s" % sl["cred_env"],
                             "fix": "用「输入密钥.cmd」/ `secrets.py set %s` / 设 User 环境变量；"
                                    "（agent 不代填、也不读该文件）" % sl["cred_env"]})
    else:
        checks["credential"] = {"env": None, "set": None, "note": "本地模式不凭证 ✓"}
        req = sl.get("requires") or {}
        v = vram_free_mb()
        checks["vram"] = {"free_mb": (v or {}).get("free"), "total_mb": (v or {}).get("total"),
                          "need_mb": req.get("vram_mb")}
        if req.get("vram_mb") and v and v["free"] < int(req["vram_mb"]):
            blockers.append({"what": "空闲显存不足：需要 %d MiB，实测空闲 %d MiB" % (req["vram_mb"], v["free"]),
                             "fix": "关掉占显存的程序，或改用云端模式"})
        if req.get("models_dir"):
            mp = os.path.join(ROOT, *req["models_dir"].replace("\\", "/").split("/"))
            checks["models_dir"] = {"path": req["models_dir"], "exists": os.path.isdir(mp)}
            if not os.path.isdir(mp):
                blockers.append({"what": "模型目录不存在：%s" % req["models_dir"], "fix": "先下载模型（见 SPEC §10 的 Paddle 命令）"})
        if req.get("disk_gb"):
            dg = disk_free_gb()
            checks["disk_free_gb"] = dg
            if dg is not None and dg < float(req["disk_gb"]):
                blockers.append({"what": "磁盘余量不足：需要 %s GB，实测 %s GB" % (req["disk_gb"], dg),
                                 "fix": "清理磁盘，或改用云端模式"})
        py = sl.get("python")
        checks["python"] = {"path": os.path.relpath(py, ROOT) if py else None, "exists": bool(py and os.path.exists(py))}
        if py and not os.path.exists(py):
            blockers.append({"what": "解释器不存在：%s" % os.path.relpath(py, ROOT), "fix": "先建该 venv"})
        if req.get("python_pkg"):
            ok = _pkg_installed(py, req["python_pkg"])
            checks["python_pkg"] = {"pkg": req["python_pkg"], "installed": ok}
            if ok is not True:
                blockers.append({"what": "本地引擎所需包未安装：%s" % req["python_pkg"],
                                 "fix": "在该 venv 里 `uv pip install %s`（本机实测未装）" % req["python_pkg"]})
    if sl.get("status") and sl["status"] != "ready":
        checks["declared_status"] = sl["status"]
    return {"ready": not blockers, "blockers": blockers, "checks": checks}


def matrix(d: dict = None) -> list:
    d = d or load()
    return [{"combo": k, "status": v} for k, v in (d.get("combos") or {}).items()]


def _tmp_py(source: str) -> str:
    """给自测造一个临时 .py（**只读探测**用；临时目录按进程 TEMP，不落在产物根）。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".py")
    with io.open(fd, "w", encoding="utf-8") as fh:
        fh.write(source)
    return path


# ---------------------------------------------------------------- CLI
def cmd_list(_a) -> int:
    d = load()
    for name in ("primary", "witness"):
        sl = resolve_slot(d, name)
        if sl.get("null"):
            print("  %-8s = null（合法降级）" % name)
            continue
        c = (sl["cred_env"] or "（本地，不凭证）")
        print("  %-8s %s:%s  adapter=%s  runner=%s  cred=%s  status=%s"
              % (name, sl["engine"], sl["mode"], sl["adapter"], sl["runner_rel"], c, sl["status"]))
    return 0


def cmd_check(_a) -> int:
    d = load()
    bad = 0
    for name in ("primary", "witness"):
        sl = resolve_slot(d, name)
        r = readiness(sl)
        head = "null（合法降级）" if sl.get("null") else "%s:%s" % (sl["engine"], sl["mode"])
        print("  %-8s %-22s %s" % (name, head, "就绪 ✓" if r["ready"] else "**不可用**（%d 个阻塞项）" % len(r["blockers"])))
        for k, v in r["checks"].items():
            print("        %-14s %s" % (k, json.dumps(v, ensure_ascii=False)))
        for b in r["blockers"]:
            print("        **阻塞** %s\n                 → %s" % (b["what"], b["fix"]))
        bad += len(r["blockers"])
    print("\n结果：阻塞项 = %d → %s" % (bad, "可跑" if not bad else "**当前配置不可跑**"))
    return 0 if not bad else 1


def cmd_matrix(_a) -> int:
    v = vram_free_mb()
    print("本机实测：显存 %s MiB 总量 / %s MiB 空闲 | 磁盘余量 %s GB"
          % ((v or {}).get("total"), (v or {}).get("free"), disk_free_gb()))
    print("\n四组合：")
    for row in matrix():
        print("  %-28s %s" % (row["combo"], row["status"]))
    return 0


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:                                                            # noqa: BLE001
        return True


def _local_moves_with_env() -> bool:
    """把 `CHEMDOC_LOCAL` 指到临时目录，槽位 env 与 `local_root()` 必须**同向**。"""
    import tempfile
    old = os.environ.get(paths.ENV_LOCAL)
    try:
        td = tempfile.mkdtemp(prefix="localmove-")
        os.environ[paths.ENV_LOCAL] = td
        env = resolve_slot(load(), "witness").get("env") or {}
        return (str(env.get("USERPROFILE", "")).startswith(paths.normalize(td))
                and paths.local_root() == paths.normalize(td))
    finally:
        if old is None:
            os.environ.pop(paths.ENV_LOCAL, None)
        else:
            os.environ[paths.ENV_LOCAL] = old


def selftest() -> int:
    import tempfile
    cases = []
    d = load()
    cases.append(("真实配置可加载（A3 起两个槽位都有引擎；witness=null 仍是合法降级）",
                  bool(d["slots"]["primary"]) and isinstance(d["engines"], dict), None))
    m = matrix(d)
    cases.append(("四组合矩阵齐备", len(m) == 4, [x["combo"] for x in m]))
    sl = resolve_slot(d, "primary")
    cases.append(("槽位展开：runner 解析为**绝对路径**、cred_env 是变量名",
                  os.path.isabs(sl["runner"]) and sl["cred_env"] == "MINERU_API_TOKEN", sl))
    sn = resolve_slot(d, "witness")
    cases.append(("A3：**见证引擎已挂上**（paddle:local，不是 null 降级）",
                  (not sn.get("null")) and sn["engine"] == "paddle" and sn["mode"] == "local",
                  {k: sn.get(k) for k in ("engine", "mode", "runner_rel")}))
    cases.append(("A3：见证引擎的**接线**都对（runner 绝对路径 + 本地模式不凭证 + runner 是真 CLI）",
                  (not sn.get("null")) and os.path.isabs(sn["runner"]) and sn["cred_env"] is None
                  and readiness(sn)["checks"].get("runner_cli", {}).get("ok") is True,
                  {k: readiness(sn)["checks"].get(k) for k in ("runner", "runner_cli")}))
    # **机器人可移植**：模型目录/解释器是否存在是**该机器的事实**，不是接线对错。所以只断言：
    # ①查过这个项目 ②若存在则为 True ③若缺失**必须**成为阻塞项并给补法（裸机上也如实报）。
    _rc = readiness(sn)["checks"]
    cases.append(("A3：模型目录/解释器**查过**，且缺失时成为带补法的阻塞项（裸机也如实报，不静默）",
                  all(("exists" in _rc.get(k, {})) for k in ("models_dir", "python"))
                  and all(any(k in b["what"] for b in readiness(sn)["blockers"])
                          for k in ("模型目录", "解释器") if _rc.get(k, {}).get("exists") is False),
                  {k: _rc.get(k) for k in ("models_dir", "python", "vram")}))
    # **显存是共享资源**：主会话/别的进程在跑 GPU 时这里就会不够——所以断言**机制**，不断言"此刻够用"
    # （曾经写成"两个槽位都必须 ready"，本地 Paddle 跑起来后必然假红，实测踩到）。
    _r = readiness(sn)
    _blk = [b for b in _r["blockers"] if "显存" in b["what"]]
    cases.append(("A3：显存**机制**——够用时不报显存阻塞；不够时报阻塞（带实测数字 + 补法），允许被占",
                  (len(_blk) == 0 and (_r["checks"].get("vram") or {}).get("free_mb", 0)
                   >= ((_r["checks"].get("vram") or {}).get("need_mb") or 0))
                  or (len(_blk) == 1 and _blk[0].get("fix") and "实测" in _blk[0]["what"]),
                  {"ready": _r["ready"], "vram": _r["checks"].get("vram"),
                   "blockers": [b["what"] for b in _r["blockers"]]}))
    cases.append(("witness=null **仍**是合法降级（单引擎不会因此变红）",
                  readiness({"slot": "witness", "null": True})["ready"], None))
    # 显存判据的**确定性**负例（不依赖 GPU 此刻忙不忙）：人造需求离谱 → 必须报阻塞且带实测数字
    _low = dict(sn, requires=dict(sn.get("requires") or {}, vram_mb=10 ** 9))
    _lb = readiness(_low)["blockers"]
    cases.append(("负例：显存需求离谱（人造） → 报阻塞并报**实测空闲 MiB**",
                  any("显存" in b["what"] and "实测" in b["what"] for b in _lb), _lb))

    base = json.loads(json.dumps(d))
    def bad(mut, tag):
        cfg = json.loads(json.dumps(base))
        mut(cfg)
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with io.open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False)
            try:
                load(tmp)
                return False
            except EngineConfigError:
                return True
        finally:
            os.remove(tmp)
    cases.append(("负例：顶层未知字段 → 报错", bad(lambda c: c.update({"slotsx": {}}), "未知顶层"), None))
    cases.append(("负例：未知引擎 → 报错",
                  bad(lambda c: c["slots"]["primary"].update({"engine": "tesseract"}), "未知引擎"), None))
    cases.append(("负例：未知模式（如 remote） → 报错",
                  bad(lambda c: c["slots"]["primary"].update({"mode": "remote"}), "未知模式"), None))
    cases.append(("负例：cloud 缺 cred_env → 报错",
                  bad(lambda c: c["engines"]["mineru"]["modes"]["cloud"].update({"cred_env": None}), "缺凭证"), None))
    cases.append(("负例：local 写了 cred_env → 报错（本地不凭证）",
                  bad(lambda c: c["engines"]["paddle"]["modes"]["local"].update({"cred_env": "X_TOKEN"}), "local 凭证"), None))
    cases.append(("负例：cred_env 像密钥值（不是变量名） → 报错",
                  bad(lambda c: c["engines"]["mineru"]["modes"]["cloud"].update({"cred_env": "sk-abcdef0123456789"}), "值入配置"), None))
    cases.append(("负例：adapter 不在该引擎的 adapters 里 → 报错",
                  bad(lambda c: c["slots"]["primary"].update({"adapter": "paddle"}), "适配器不符"), None))
    cases.append(("负例：status 非法 → 报错",
                  bad(lambda c: c["engines"]["mineru"]["modes"]["cloud"].update({"status": "maybe"}), "status"), None))

    # 就绪度：人造槽位（runner 不存在 / 显存不够 / 缺凭证）
    fake = {"slot": "x", "engine": "mineru", "mode": "cloud", "adapter": "mineru",
            "runner": os.path.join(ROOT, "no-such-runner.py"), "runner_rel": "no-such-runner.py",
            "status": "ready", "cred_env": "MINERU_API_TOKEN_NO_SUCH_VAR", "requires": {}, "python": None, "env": {}}
    r = readiness(fake)
    cases.append(("就绪度：runner 不存在 + 凭证未设 → 两个阻塞项且给补法",
                  (not r["ready"]) and len(r["blockers"]) == 2 and all(b.get("fix") for b in r["blockers"]),
                  [b["what"] for b in r["blockers"]]))
    fake2 = dict(fake, cred_env=None, mode="local",
                 requires={"vram_mb": 10 ** 9, "models_dir": "no-such-models"}, status="ready",
                 runner=os.path.join(ROOT, "_mcp-test", "dump_paddle_raw.py"), runner_rel="_mcp-test/dump_paddle_raw.py")
    r2 = readiness(fake2)
    cases.append(("就绪度：显存需求离谱 + 模型目录缺失 → 报阻塞且**报实测数字**",
                  (not r2["ready"]) and any("显存" in b["what"] for b in r2["blockers"])
                  and any("模型目录" in b["what"] for b in r2["blockers"]), [b["what"] for b in r2["blockers"]]))

    # 槽位 env：**必须能落成绝对路径、且跟本地根一致**（这是"代码自动设环境"的地基）
    _w = resolve_slot(load(), "witness")
    _env = _w.get("env") or {}
    cases.append(("槽位 `witness` 的 env 已解析成**绝对路径**，且都在本地根之下"
                  "（`USERPROFILE`/`TEMP` 指到 `<本地根>/…`）",
                  bool(_env) and all(os.path.isabs(v) for v in _env.values() if os.sep in str(v) or "/" in str(v))
                  and str(_env.get("USERPROFILE", "")).startswith(paths.local_root())
                  and str(_env.get("TEMP", "")).startswith(paths.local_root()), _env))
    cases.append(("负例：`CHEMDOC_LOCAL` 指到别处 → 槽位 env 与 `local_root()` **一起**搬过去（不会一半新一半旧）",
                  _local_moves_with_env(), None))
    cases.append(("`find_slot`：`primary`/`witness`/`paddle:local` 都能解析；瞎写的必须报错",
                  find_slot("witness") == "witness" and find_slot("paddle:local") == "witness"
                  and _raises(lambda: find_slot("nope:nope")), None))

    # A3：runner CLI 契约（**静态**探测，不执行 runner）
    cases.append(("CLI 契约：现有 primary runner 是**可编排 CLI**（main() + argparse + `--book`）",
                  runner_cli_contract(sl["runner"]).get("ok") is True, runner_cli_contract(sl["runner"])))
    cases.append(("CLI 契约负例：MCP wrapper 有 main()+argparse 但**没有 `--book`** → 不算可编排 CLI",
                  runner_cli_contract(os.path.join(ROOT, "dual-mcp-bridge",
                                                   "mineru_mcp_wrapper.py")).get("ok") is False,
                  runner_cli_contract(os.path.join(ROOT, "dual-mcp-bridge", "mineru_mcp_wrapper.py"))))
    cases.append(("CLI 契约负例：只有 main() + argparse、无 `--book` → 报不通过",
                  runner_cli_contract(_tmp_py("import argparse\ndef main():\n    pass\n")).get("ok") is False, None))
    cases.append(("CLI 契约：非 .py runner → 不判（ok=None，不误报阻塞）",
                  runner_cli_contract(os.path.join(ROOT, "输入密钥.cmd"))["ok"] is None, None))
    cases.append(("就绪度负例：runner 存在但**不是 CLI** → 报阻塞并指向契约",
                  (not readiness(dict(fake, runner=os.path.join(ROOT, "dual-mcp-bridge",
                                                                "mineru_mcp_wrapper.py"),
                                      runner_rel="dual-mcp-bridge/mineru_mcp_wrapper.py"))["ready"]),
                  None))

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:130]))
        passed += bool(ok)
    print("\n%d/%d engines tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("matrix").set_defaults(func=cmd_matrix)
    rr = sub.add_parser("run", help="跑引擎（自动带上槽位声明的 python 与 env）")
    rr.add_argument("--slot", default="witness", help="槽位名或 engine:mode（如 primary / witness / paddle:local）")
    rr.add_argument("--book", required=True)
    rr.add_argument("rest", nargs="*", help="透传给 runner 的额外参数（可省）")
    rr.set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
