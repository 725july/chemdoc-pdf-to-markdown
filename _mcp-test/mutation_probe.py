# -*- coding: utf-8 -*-
"""**B2 变异探针**：故意把判据改坏，看自测抓不抓得住（"测试的测试"）。

为什么要它：342 条断言只证明"代码在做我让它做的事"，不证明"**判据被改错时会有人喊**"。

**设计要点（三条都是踩出来的）**：
  1. **整套目录拷到临时区**再变异。"只把被变异的那一个文件放进临时目录"会让 `__file__` 换位置，
     导致按 `__file__` 找数据文件的门禁（如 G14 找 `measurements/error-rate.json`）集体变
     `unverified` → **每条变异都凭空多一个 FAIL** → 假阳性（实测 8 条"被抓"全是假的）。
  2. **与同环境基线做差分**：先在同一临时区跑一次**未变异**的同套件，取基线失败集合；
     "被抓" = **退出码变了** 或 **出现了基线里没有的 FAIL**。
  3. 导入套件前**预热被变异的模块**：有些套件顶部会 `sys.path.insert(0, 仓库)`，
     不预热就会 import 到未变异的仓库版（实测 8 条假漏）。
  4. **绝不修改仓库文件**：全程只在临时区动刀，收尾核对仓库 sha256。

用法:
  python -X utf8 mutation_probe.py run        # 全量变异扫描（含基线差分）
  python -X utf8 mutation_probe.py selftest   # 探针自身
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
import paths  # B2：工作根唯一出口（SPEC §7.10）

REPO = paths.MODULE_DIR

MUTATIONS = [
    dict(id="baseline_noop", file="gates.py",
         old='HARD = {"G1", "G2", "G9", "G11", "G12", "G13", "G19", "G20", "G21"}',
         new='HARD = {"G1", "G2", "G9", "G11", "G12", "G13", "G19", "G20", "G21"}',
         suite="validate_run", expect="not_caught", why="无害变异：探针**不得**把它当被抓"),
    dict(id="G3_always_pass", file="gates.py",
         old='status = "fail" if (dup or md_dup) else ("warn" if own else "pass")',
         new='status = "pass"', suite="validate_run", expect="caught", why="G3 重复段落判据废掉"),
    dict(id="G4_always_pass", file="gates.py",
         old='status = "fail" if (dups or bad or unresolved_refs) else ("warn" if not order_ok else "pass")',
         new='status = "pass"', suite="validate_run", expect="caught", why="G4 公式编号判据废掉"),
    dict(id="G8_always_pass", file="gates.py",
         old='''    status = "fail" if undecided else "pass"
    return _g("G8", "digit_unit_preserved", status,''',
         new='''    status = "pass"
    return _g("G8", "digit_unit_preserved", status,''',
         suite="validate_run", expect="caught", why="G8 数字/单位风险项不再要求已判"),
    dict(id="G11_always_pass", file="gates.py",
         old='''    status = "fail" if undecided else "pass"
    return _g("G11", "geometry_without_content", status,''',
         new='''    status = "pass"
    return _g("G11", "geometry_without_content", status,''',
         suite="validate_run", expect="caught", why="G11（硬）不再要求 R1 已判"),
    dict(id="G12_always_pass", file="gates.py",
         old='status = "fail" if (miss_ir or miss_md) else "pass"',
         new='status = "pass"', suite="validate_run", expect="caught", why="G12 物化完整性废掉"),
    dict(id="G13_always_pass", file="gates.py",
         old='''    status = "fail" if ev else "pass"
    return _g("G13", "media_landed", status,''',
         new='''    status = "pass"
    return _g("G13", "media_landed", status,''',
         suite="validate_run", expect="caught", why="G13 媒体落盘废掉"),
    dict(id="G19_always_pass", file="gates.py",
         old='return _g("G19", "review_kit", "fail" if (missing or ev) else "pass",',
         new='return _g("G19", "review_kit", "pass",',
         suite="validate_run", expect="caught", why="G19（硬）校对包废掉"),
    dict(id="G21_always_pass", file="gates.py",
         old='status = "pass" if res["ok"] else "fail"',
         new='status = "pass"', suite="validate_run", expect="caught", why="G21（硬）判定台账废掉"),
    dict(id="HARD_drop_G19", file="gates.py",
         old='HARD = {"G1", "G2", "G9", "G11", "G12", "G13", "G19", "G20", "G21"}',
         new='HARD = {"G1", "G2", "G9", "G11", "G12", "G13", "G20", "G21"}',
         suite="validate_run", expect="caught", why="硬门禁集合里把 G19 摘掉（失败将不再阻断）"),
    dict(id="G14_missing_becomes_pass", file="gates.py",
         old='        return _unverified("G14", "error_rate",',
         new='        return _g("G14", "error_rate", "pass",',
         suite="validate_run", expect="caught", why="G14 把「无实测记录」当 pass（违反 I2）"),
    dict(id="ledger_summary_removed", file="judge_worklist.py",
         old="    if q != l:",
         new="    if False:",
         suite="judge_worklist", expect="caught", why="台账/队列不一致的**汇总**判据被摘掉"),
    dict(id="transcribe_exemption_removed", file="s5_queue_gate.py",
         old='''    if is_transcribe or is_figure:
        exempted = list(SPAN_ONLY_REASONS)''',
         new='''    if is_figure:
        exempted = list(SPAN_ONLY_REASONS)''',
         suite="s5_queue_gate", expect="caught", why="转写任务的**豁免**被摘掉（C3 修的坑复活；U-15 的图区豁免保留）"),
]


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _repo_hashes() -> dict:
    return {f: _sha(os.path.join(REPO, f)) for f in sorted(os.listdir(REPO)) if f.endswith(".py")}


def _purge(*bases) -> None:
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if any(f.startswith(b) for b in bases):
            sys.modules.pop(name, None)


def _stage() -> str:
    """把整个 `_mcp-test` 拷到临时区（数据文件跟着走，`__file__` 语义不变）。返回"仓库根"路径。"""
    tmp = tempfile.mkdtemp(prefix="mutprobe-")
    dst = os.path.join(tmp, "repo")
    shutil.copytree(REPO, dst,
                    ignore=shutil.ignore_patterns("pristine-check", "__pycache__", "*.pyc"))
    return dst


def _cleanup(stage: str) -> None:
    shutil.rmtree(os.path.dirname(stage), ignore_errors=True)


def _run_in(stage: str, suite: str, mut: dict | None = None) -> dict:
    spec_ok, applied = True, 0
    if mut is not None:
        p = os.path.join(stage, mut["file"])
        src = io.open(p, encoding="utf-8").read()
        applied = src.count(mut["old"])
        spec_ok = applied == 1
        if spec_ok:
            io.open(p, "w", encoding="utf-8", newline="\n").write(src.replace(mut["old"], mut["new"]))
    sys.path.insert(0, stage)
    importlib.invalidate_caches()
    _purge(stage, REPO)
    # B2：把**数据根钉到暂存区**（= 与基线同环境、且不受外部 `CHEMDOC_ROOT` 影响）。
    # 不钉的话：装了 B2 之后，外部环境变量会让"变异体"和"基线"读到不同的数据 → 差分失效。
    _prev_env = os.environ.get("CHEMDOC_ROOT")
    os.environ["CHEMDOC_ROOT"] = os.path.dirname(stage)
    rc, out = 99, ""
    try:
        if mut is not None:
            # 预热被变异的模块（否则被套件自己的 `sys.path.insert(0, 仓库)` 抢到未变异版）
            importlib.import_module(os.path.splitext(mut["file"])[0])
        mod = importlib.import_module(suite)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mod.selftest()
        out = buf.getvalue()
    except Exception as exc:                                    # noqa: BLE001
        rc, out = 99, "EXC " + type(exc).__name__ + ": " + str(exc)[:160]
    finally:
        sys.path.remove(stage)
        _purge(stage)
        if _prev_env is None:
            os.environ.pop("CHEMDOC_ROOT", None)
        else:
            os.environ["CHEMDOC_ROOT"] = _prev_env
    return {"spec_ok": spec_ok, "applied": applied, "rc": rc,
            "fails": {l.strip() for l in out.splitlines() if l.startswith("FAIL")},
            "last": (out.strip().splitlines() or [""])[-1][:90]}


def _baseline(suite: str) -> dict:
    st = _stage()
    try:
        return _run_in(st, suite)
    finally:
        _cleanup(st)


def cmd_run(_a) -> int:
    before = _repo_hashes()
    base_by_suite, rows, gaps, over = {}, [], [], []
    for mut in MUTATIONS:
        if mut["suite"] not in base_by_suite:
            base_by_suite[mut["suite"]] = _baseline(mut["suite"])
        base = base_by_suite[mut["suite"]]
        st = _stage()
        try:
            r = _run_in(st, mut["suite"], mut)
        finally:
            _cleanup(st)
        new_fails = r["fails"] - base["fails"]
        caught = (r["rc"] != base["rc"]) or bool(new_fails)
        rows.append(dict(mut, **r, caught=caught, new_fails=sorted(new_fails)[:2]))
        if not r["spec_ok"]:
            gaps.append(mut["id"] + "（**SPEC 无效**：old 匹配 " + str(r["applied"]) + " 次）")
        elif mut["expect"] == "caught" and not caught:
            gaps.append(mut["id"] + "（判据坏了但**没人喊**）")
        elif mut["expect"] == "not_caught" and caught:
            over.append(mut["id"])
    after = _repo_hashes()
    print("基线（同环境、未变异）：")
    for k, v in base_by_suite.items():
        print("   %-16s rc=%s  %s" % (k, v["rc"], v["last"]))
    print("\n变异扫描（%d 条）" % len(rows))
    print("%-32s %-18s %-11s %-8s %s" % ("变异", "文件", "期望", "结果", "说明"))
    print("-" * 106)
    for r in rows:
        got = "SPEC无效" if not r["spec_ok"] else ("被抓" if r["caught"] else "**漏掉**")
        if r["expect"] == "not_caught" and r["caught"]:
            got = "**误抓**"
        print("%-32s %-18s %-11s %-8s %s" % (r["id"], r["file"], r["expect"], got, r["why"]))
    # 得分只统计"**期望被抓**"的变异（无害变异不计入分母，否则分数被稀释得看不懂）
    exp = [r for r in rows if r["spec_ok"] and r["expect"] == "caught"]
    caught = sum(1 for r in exp if r["caught"])
    print("\n变异得分（命中率，只统计期望被抓的变异）= %d/%d = %.2f"
          % (caught, len(exp), caught / max(1, len(exp))))
    print("仓库文件未被改动 =", before == after)
    if gaps:
        print("\n**真缺口（必须补断言）**：")
        for g in gaps:
            print("  -", g)
    if over:
        print("\n**探针过敏感**：", over)
    return 0 if (not gaps and not over and before == after) else 1


def selftest() -> int:
    cases, before = [], _repo_hashes()
    for mut in MUTATIONS:
        src = io.open(os.path.join(REPO, mut["file"]), encoding="utf-8").read()
        cases.append(("规格有效（old 唯一）：" + mut["id"], src.count(mut["old"]) == 1,
                      src.count(mut["old"])))
    base = _baseline("validate_run")
    cases.append(("同环境基线必须全绿（防「环境造成的假阳性」）",
                  base["rc"] == 0 and not base["fails"],
                  {"rc": base["rc"], "fails": sorted(base["fails"])[:2], "last": base["last"]}))
    noop = next(m for m in MUTATIONS if m["expect"] == "not_caught")
    st = _stage()
    try:
        r1 = _run_in(st, "validate_run", noop)
    finally:
        _cleanup(st)
    cases.append(("无害变异**不得**被判被抓（差分后 rc 与 FAIL 集合都不变）",
                  r1["rc"] == base["rc"] and not (r1["fails"] - base["fails"]), r1["last"]))
    known = next(m for m in MUTATIONS if m["id"] == "G3_always_pass")
    st = _stage()
    try:
        r2 = _run_in(st, "validate_run", known)
    finally:
        _cleanup(st)
    cases.append(("已知必抓的变异必须被抓（G3 判据废掉）",
                  (r2["rc"] != base["rc"]) or bool(r2["fails"] - base["fails"]),
                  {"rc": r2["rc"], "new_fails": sorted(r2["fails"] - base["fails"])[:1]}))
    cases.append(("探针不修改仓库文件", before == _repo_hashes(), None))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:130]))
        passed += bool(ok)
    print("\n%d/%d mutation_probe tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
