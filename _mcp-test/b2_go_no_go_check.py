# -*- coding: utf-8 -*-
"""**B2 前进夹具（go / no-go）**：证明"**失败会挡住、修好能前进**"，且拒绝发布**不伤已发布版本**。

为什么需要它：现有断言几乎都在问"**错的东西会报错吗**"，很少问"**正常的东西走得通吗**"。
（实测教训：C3 的"稀疏页被误拦""缺 judge 被误拦"两次都是**人工复核查出来的**，不是测试抓出来的。）

做法：全程在**一次性临时工作区**里跑，不碰真实产物：
  ① 坏产物（缺页）→ `validate` 非 0 且 `publish` **拒绝**（正式目录不出现，存档区不出现，事务日志不产生）
  ② **修好** → 可交付 + 发布成功（正式目录出现、`state=done`、负载哈希一致）        ← **前进**
  ③ **再坏一次**（已发布版本在场）→ 仍拒绝，且**已发布版本逐字节不变**、存档区不新增  ← 拒绝不伤人
  ④ **再修好** → 再次发布成功，旧版**被移动**到存档区（绝不删除）                    ← 前进可重复

用法: python -X utf8 b2_go_no_go_check.py
退出码 0 = 全过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import publish_run as P            # noqa: E402
import validate_run as V           # noqa: E402


def _seed(root: str, **fx) -> None:
    """造一棵产物树，并把"正式目录"挪成"暂存区"（模拟 S7 只写暂存区）。"""
    V._fixture(root, **fx)
    p = P.paths("fx", root)
    os.makedirs(os.path.dirname(p["staging"]), exist_ok=True)
    if os.path.isdir(p["staging"]):
        shutil.rmtree(p["staging"])
    shutil.move(p["formal"], p["staging"])


def _refresh_staging(root: str, **fx) -> None:
    """用**独立副本**刷新暂存区（正式目录一动不动）。"""
    scratch = tempfile.mkdtemp(prefix="gonogo-src-")
    try:
        V._fixture(scratch, **fx)
        p = P.paths("fx", root)
        if os.path.isdir(p["staging"]):
            shutil.rmtree(p["staging"])
        os.makedirs(os.path.dirname(p["staging"]), exist_ok=True)
        shutil.copytree(os.path.join(scratch, "s7-out", "fx"), p["staging"])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    cases, root = [], tempfile.mkdtemp(prefix="gonogo-")
    try:
        # ---------- ① 坏产物：必须**挡住** ----------
        _seed(root, pages=(1, 2), md_pages=(1,))          # 缺一页 → G1 fail（硬）
        p = P.paths("fx", root)
        v1 = V.validate("fx", root, s7_dir=p["staging"])
        r1 = P.publish("fx", root)
        cases.append(("① 坏产物：判不得交付且退出码非 0",
                      v1["exit_code"] != 0 and "G1" in v1["hard_gates_failed"],
                      {"verdict": v1["verdict"], "hard": v1["hard_gates_failed"]}))
        cases.append(("① 坏产物：**发布被拒绝**且理由写明",
                      r1["exit_code"] != 0 and str(r1.get("result", "")).startswith("拒绝发布"),
                      {"exit": r1["exit_code"], "result": r1.get("result")}))
        cases.append(("① 坏产物：正式目录**没出现**（不产半成品）",
                      not os.path.isdir(p["formal"]), p["formal"]))
        cases.append(("① 坏产物：存档区**没出现**、事务日志**没产生**",
                      not os.path.isdir(p["published_dir"]) and not os.path.exists(p["journal"]), None))

        # ---------- ② 修好：必须**前进** ----------
        _refresh_staging(root, pages=(1, 2))
        r2 = P.publish("fx", root)
        cases.append(("② 修好 → 发布成功：正式目录出现 + 日志 done + 负载哈希一致",
                      r2["exit_code"] == 0 and os.path.isdir(p["formal"])
                      and json.load(open(p["journal"], encoding="utf-8"))["state"] == "done"
                      and r2.get("payload_hashes_match") is True,
                      {"exit": r2["exit_code"], "result": r2.get("result")}))

        # ---------- ③ 再坏一次：拒绝，且**不伤已发布版本** ----------
        published_before = P.dir_hashes(p["formal"])
        arc_before = sorted(os.listdir(p["published_dir"])) if os.path.isdir(p["published_dir"]) else []
        _refresh_staging(root, pages=(1, 2), md_pages=(1,))
        r3 = P.publish("fx", root)
        arc_after = sorted(os.listdir(p["published_dir"])) if os.path.isdir(p["published_dir"]) else []
        cases.append(("③ 已发布版本在场时再坏 → 仍拒绝（退出码非 0）",
                      r3["exit_code"] != 0, {"exit": r3["exit_code"], "result": r3.get("result")}))
        cases.append(("③ 拒绝发布**不伤已发布版本**（逐字节不变、存档区不新增）",
                      P.dir_hashes(p["formal"]) == published_before and arc_before == arc_after,
                      {"formal_changed": P.dir_hashes(p["formal"]) != published_before,
                       "archive_new": sorted(set(arc_after) - set(arc_before))}))

        # ---------- ④ 再修好：前进可重复，旧版被**移动**归档（不删除） ----------
        _refresh_staging(root, pages=(1, 2))
        with open(os.path.join(p["staging"], "NEW_FILE.txt"), "w", encoding="utf-8") as fh:
            fh.write("第三次发布\n")
        r4 = P.publish("fx", root)
        archived = r4.get("archived_previous")
        cases.append(("④ 再修好 → 再次发布成功，且旧版被**移动**到存档区（旧文件仍在）",
                      r4["exit_code"] == 0 and archived and os.path.isdir(archived)
                      and set(published_before) <= set(P.dir_hashes(archived).keys()),
                      {"archived": archived}))

        # ---------- ⑤ 判据不能是"永远红灯"：交付路径本身必须通 ----------
        cases.append(("⑤ 前进夹具自身有效：至少 2 次发布成功（不是「永远拒绝」）",
                      sum(1 for r in (r1, r2, r3, r4) if r["exit_code"] == 0) >= 2,
                      [r["exit_code"] for r in (r1, r2, r3, r4)]))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name
              + ("" if ok else "   -> " + json.dumps(extra, ensure_ascii=False)[:160]))
        passed += bool(ok)
    print("\n%d/%d go/no-go checks passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
