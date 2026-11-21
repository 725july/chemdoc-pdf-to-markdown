# -*- coding: utf-8 -*-
"""C3 改完后的一次性总回归：17 套件 + 3 条端到端回归 + 四本已发布产物逐字节比对。

用法: python -X utf8 c3_full_regression.py
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
import paths  # B2：工作根唯一出口（SPEC §7.10）

TOOLS = paths.TOOLS_ROOT   # B2：工具/环境（_mcp-test、.local/）
DATA = paths.data_root()   # B2：数据（s7-out、归档基线）  # 可迁移：_mcp-test 的上一级 = 产物根

# 进程内把临时目录指到工作区临时区（纵深防御）：若某次运行的 TEMP 被指到工作区根，
# 沙箱下 `mkdtemp` 建的目录清不掉 → 会**漏成工作区杂物**（2026-10-13 清理时发现 40+ 个）。
_TMP_ROOT = os.path.join(paths.local_root(), "tmp", "pytmp")   # 机器本地物统一在 .local/ 下
import tempfile as _tempfile  # noqa: E402
try:
    os.makedirs(_TMP_ROOT, exist_ok=True)
    _tempfile.tempdir = _TMP_ROOT
    for _v in ("TMPDIR", "TEMP", "TMP"):
        os.environ.setdefault(_v, _TMP_ROOT)
except OSError:
    # 造不出来就退回系统临时目录：**辅助手段不得把整轮回归带走**（副本树曾因目录权限整轮失败）
    pass
sys.path.insert(0, os.path.join(TOOLS, "_mcp-test"))

SUITES = ["paths", "s0_classify", "s2_ir", "s3_compare", "s4_checks", "s5_queue_gate", "s5_judge_adapter",
          "s6_assemble", "s7_output", "s8_review_kit", "delivery_note", "errata_apply", "validate_run", "publish_run", "binding",
          "pipeline_run", "review_package", "golden_diff", "volumes", "chapters", "judge_worklist", "mutation_probe", "gate_adversarial", "contracts", "books", "tuning", "render", "release_check", "intake", "layout_declare", "secret_scan", "engines", "preflight", "doctor", "secrets", "runners.mineru_cloud", "runners.paddle_local", "b2_install_check", "fx_sample", "demo_offline"]
REGRESSIONS = ["c1_cross_chapter_check", "c2_volume_merge_check", "c2_book_run_check",
               "c3_resume_check", "b2_go_no_go_check", "b1_4b_profile_recompute"]
import books as _books_mod  # noqa: E402  （B2b：归档书单**从磁盘枚举**，不写死）


def published_keys() -> list:
    """有归档快照的书（目录键）—— 从 `<数据根>/s7-out/_published/` **枚举**。

    为什么不写死四本：那是"某台机器的历史"，新机器上不存在。
    """
    return _books_mod.published(DATA)


def _tail(mod, fn="selftest", main=None):
    """跑一个套件的 selftest/main，只取最后一行汇总。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn() if callable(fn) else 1
    lines = [l for l in buf.getvalue().splitlines() if l.strip()]
    # 只按**行首**计 FAIL：有些套件把自己的用例名写成含 "FAIL" 的字样（如变异探针），
    # 用子串计数会把正常输出误判成失败（本脚本自己踩过）。
    nfail = sum(1 for l in lines if l.startswith("FAIL"))
    return rc, (lines[-1] if lines else ""), nfail


def _norm(fp: str) -> bytes:
    """比对前归一：**忽略校验器写入的时间戳 `checked_at`**——归档是"发布后重跑校验"写的，
    与现场副本的时间戳天然不同；把时间戳当内容差异会假报。**其余一律按字节比**（含 gates/counts/verdict）。"""
    raw = open(fp, "rb").read()
    if fp.endswith(".json"):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:                                              # noqa: BLE001
            return raw
        if isinstance(obj, dict) and "checked_at" in obj:
            obj.pop("checked_at", None)
            return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return raw


def _hash_tree(d: str) -> str:
    out = []
    for base, _dirs, files in os.walk(d):
        for f in sorted(files):
            fp = os.path.join(base, f)
            out.append(os.path.relpath(fp, d).replace("\\", "/") + ":" +
                       hashlib.sha256(_norm(fp)).hexdigest())
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


def _last_hash(d: str) -> str:
    """归档目录里**最新一次**发布快照的指纹。"""
    subs = sorted(os.listdir(d)) if os.path.isdir(d) else []
    return _hash_tree(os.path.join(d, subs[-1])) if subs else ""


def _try_import(name: str):
    """导入套件；**模块不在本拷贝里**（新鲜下载/发布集过滤掉的一次性脚本）→ 返回 (None, 原因)。

    为什么必须显式处理：`b1_4b_profile_recompute.py` 按 B4 的发布边界**不进发布集**，
    于是"刚下载下来的那份拷贝"跑总回归会在这里 `ModuleNotFoundError` **直接崩**（实测）。
    口径：**明示跳过**（打印 SKIP + 汇总里点名），不假装跑过、也不算失败。
    """
    try:
        return importlib.import_module(name), ""
    except ModuleNotFoundError as exc:
        return None, "模块不在本拷贝里（%s）" % exc


def _run_suite(mod):
    """有 `selftest()` 就直接调；没有的（如 golden_diff）走 CLI 的 `selftest` 子命令。"""
    if hasattr(mod, "selftest"):
        return _tail(mod, getattr(mod, "selftest"))
    old = sys.argv
    sys.argv = [getattr(mod, "__name__", "x"), "selftest"]
    try:
        return _tail(mod, getattr(mod, "main"))
    finally:
        sys.argv = old


def main() -> int:
    bad = []
    total_assert = 0

    def _published_compare() -> list:
        """**归档自洽性**（B2b 口径：书单**从磁盘枚举**，B2b 前写死四本）。

        为什么改口径（B2b 实测）：原先写死"四本旧样本"，那四本的工作副本已按清理批次移入 `trash\\`；
        改成枚举之后，**活跃书**（例如刚重新发布过的 A 线真书）也会进来 —— 而"活跃书的归档 = **上一版**"
        与当前产物**必然不同**，拿它当失败是**判据错了**。所以现在分三种情形：
          ① 归档快照缺失/为空 → **FAIL**（这才是真问题）；
          ② 书在注册表里 **state=retired**（不再维护）且工作副本还在 → 做"副本 vs 归档"**逐字节**比对；
          ③ 其它（活跃书）→ 只查"归档在位且非空"，并**说清为什么没做逐字节比对**。
        """
        out = []
        if not paths.has_data(DATA):                 # 新鲜下载/空机器：没有数据树，就没有"已发布样本"可查
            _ks = published_keys()
            if not _ks:
                return [("<无归档>", True, "**这份拷贝里没有数据树 / 没有任何归档快照** → 归档基线检查明示跳过")]
            return [(k, True, "**这份拷贝里没有数据树** → 归档基线检查明示跳过（不假装比过）") for k in _ks]
        try:
            _retired = {c.get("key") for c in _books_mod.load(DATA).values()
                        if c.get("state") == "retired"}
        except Exception:                            # noqa: BLE001  注册表不在/坏了 → 只做"归档在位"检查
            _retired = set()
        for key in published_keys():
            cur = os.path.join(DATA, "s7-out", key)
            arc = os.path.join(DATA, "s7-out", "_published", key)
            if not _last_hash(arc):
                out.append((key, False, "**归档快照缺失或为空**"))
                bad.append("archive-empty:" + key)
                continue
            if os.path.isdir(cur) and key in _retired:
                same = _hash_tree(cur) == _last_hash(arc)
                out.append((key, same, "**退休书**：产物与归档逐字节一致" if same
                            else "**退休书**：产物与归档不一致"))
                if not same:
                    bad.append("archive:" + key)
            elif os.path.isdir(cur):
                out.append((key, True, "活跃书：归档 = **上一版**（与当前产物必然不同）→ 只查归档在位且非空"))
            else:
                out.append((key, True, "工作副本已移入 trash（归档基线在位，未做逐字节比对）"))
        return out

    if "--archive-only" in sys.argv:          # 只跑归档比对（省时间/token）
        print("--- 四本旧样本：工作副本 vs 归档（逐字节；仅忽略校验器时间戳）---")
        for key, same, note in _published_compare():
            print(("PASS  " if same else "FAIL  ") + f"{key:18s} {note}")
        print("\n结果 =", "全绿" if not bad else f"有问题：{bad}")
        return 0 if not bad else 1

    skipped, data_skips = [], []
    for name in SUITES:
        mod, why = _try_import(name)
        if mod is None:
            skipped.append(name); print(f"SKIP  {name:20s} {why}"); continue
        rc, tail, nfail = _run_suite(mod)
        ok = rc == 0 and nfail == 0
        if "SKIP" in tail:
            data_skips.append(name)
        if "/" in tail:
            try:
                total_assert += int(tail.split("/")[0].split()[-1])
            except Exception:                                          # noqa: BLE001
                pass
        print(("PASS  " if ok else "FAIL  ") + f"{name:20s} {tail}")
        if not ok:
            bad.append(name)
    for name in REGRESSIONS:
        mod, why = _try_import(name)
        if mod is None:
            skipped.append(name); print(f"SKIP  {name:20s} {why}"); continue
        fn = getattr(mod, "main", None) or getattr(mod, "selftest", None)
        rc, tail, nfail = _tail(mod, fn)
        ok = rc == 0 and nfail == 0
        if "SKIP" in tail:
            data_skips.append(name)
        if "/" in tail:
            try:
                total_assert += int(tail.split("/")[0].split()[-1])
            except Exception:                                          # noqa: BLE001
                pass
        print(("PASS  " if ok else "FAIL  ") + f"{name:20s} {tail}")
        if not ok:
            bad.append(name)
    print(f"\n套件断言合计 ≈ {total_assert} 条；失败套件 = {bad or '无'}")
    if skipped:
        print("明示跳过（**模块不在本拷贝里**；发布边界见 release-exclude.json）：" + "、".join(skipped))
    if data_skips:
        print("明示跳过（**该套件/回归需要数据，本树没有** —— 不假装跑过）：" + "、".join(data_skips))

    print("\n--- 四本旧样本：工作副本 vs 归档（逐字节；仅忽略校验器时间戳）---")
    for key, same, note in _published_compare():
        print(("PASS  " if same else "FAIL  ") + f"{key:18s} {note}")
    print("\n结果 =", "全绿" if not bad else f"有问题：{bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
