# -*- coding: utf-8 -*-
"""阶段 2：**暂存区 → 校验 → 原子发布**（SPEC §5.1 / U-05 裁定）。

流程（并借鉴原 skill 的 "staged → publish → post-publish validation"）：

    S7 写【暂存区】s7-out\\_staging\\<书>\\
         │
    validate_run（判决绑定这批字节）
         │
   ┌─────┴─────┐
 通过          不通过
   │             │
 发布           【拒绝】：暂存区保留、正式目录**一动不动**、退出码透传
   ├─ 旧版移到 s7-out\\_published\\<书>\\<时间戳>\\   （**移动，绝不删除**）
   ├─ 暂存区 → 正式目录（同卷移动）
   ├─ **发布后再校验一次**（把判决绑定到正式快照）
   └─ 事务日志 s7-out\\_publish\\<书>\\journal.json

用法:
  python -X utf8 publish_run.py publish --book <key> [--artifact-root ...] [--dry-run]
  python -X utf8 publish_run.py recover --book <key> [--artifact-root ...]
  python -X utf8 publish_run.py selftest

退出码：0 已发布且复验通过 / 1,2,3 校验拒绝（透传）/ 4 发布后复验失败 / 5 没有可发布的暂存区
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sys
import paths as _paths  # B2：工作根唯一出口（SPEC §7.10）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 可迁移：脚本自己所在目录
import validate_run as V  # noqa: E402

STAGING, PUBLISHED, JOURNAL = "_staging", "_published", "_publish"
EXIT_OK, EXIT_POST_FAIL, EXIT_NOTHING = 0, 4, 5


def _now_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_hashes(d: str, skip: tuple = ()) -> dict:
    """目录内所有文件的 sha256。

    `skip` 用于排除**校验器自己生成/重写**的 JSON（`validation-report.json` / `report.json`）：
    它们在发布后复验时会重新绑定到正式快照（路径与时间戳必然不同），
    因此"逐字节一致"要比的是**交付载荷**（正文/插图/勘误/索引）。
    """
    out = {}
    for dirpath, _dirs, files in os.walk(d):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), d).replace("\\", "/")
            if rel in skip:
                continue
            out[rel] = _sha256(os.path.join(dirpath, f))
    return dict(sorted(out.items()))


PAYLOAD_SKIP = ("validation-report.json", "report.json", "交付说明.md")
# ↑ **校验器自己的产物**（A6 起把交付说明也算进来）：它们在每次校验时被**整体重写并重新绑定**到当前快照，
#   所以"逐字节一致"要比的是**交付载荷**（正文/插图/勘误/索引/校对包）。
#   实测教训（A6）：交付说明虽然确定性（不含时间戳），**但不能**放进载荷比对 —— 它的"门禁分布"里有
#   G22 **自己的状态**，而 G22 在**首次校验**（还没有封条 → unverified）与**后续校验**（pass）之间必然变一次
#   → 载荷比对会出现假红（staging 版 unverified / 正式版 pass）。故它归入 SKIP，正确性由
#   **不变量 I5（写入即复核）+ 软门禁 G22（封条）** 负责。


def resolve_key(book: str) -> str:
    """把"书名字典键"（如 `puhua`）解析成**目录键**（如 `puhua-26-28`）。

    实测踩过：直接用字典键拼路径会拼出不存在的 `_staging\\puhua`，于是发布器误报"没有可发布的暂存区"。
    """
    try:
        import pipeline_run as pr
        cfg = pr.BOOKS.get(book)
        return cfg["key"] if cfg else book
    except Exception:                                          # noqa: BLE001
        return book


def _first_reviewed(d: str) -> str:
    """目录里**第一个留底版章文件**（按章号序）。

    C1 起一本书可能多章：发布/复验的载荷哈希是**整目录**（`dir_hashes`），不受影响；
    但"挑一个正文做破坏性测试/自证"的地方必须按章号取，不能靠 `os.listdir` 的次序。
    """
    if not os.path.isdir(d):
        return ""

    def _k(f: str) -> tuple:
        m = re.match(r"chapter-(\d+)", f)
        return (0 if "-front" in f else 1, int(m.group(1)) if m else 10 ** 6, f)

    cands = [f for f in os.listdir(d)
             if f.startswith("chapter-") and f.endswith(".md") and not f.endswith("-clean.md")]
    return sorted(cands, key=_k)[0] if cands else ""


def paths(key: str, root: str) -> dict:
    base = os.path.join(root, "s7-out")
    return {"formal": os.path.join(base, key),
            "staging": os.path.join(base, STAGING, key),
            "published_dir": os.path.join(base, PUBLISHED, key),
            "journal": os.path.join(base, JOURNAL, key, "journal.json")}


def _write_journal(p: dict, **fields) -> None:
    os.makedirs(os.path.dirname(p["journal"]), exist_ok=True)
    data = {}
    if os.path.exists(p["journal"]):
        try:
            data = json.load(open(p["journal"], encoding="utf-8"))
        except Exception:                                          # noqa: BLE001
            data = {}
    data.update(fields)
    data["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with open(p["journal"], "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def publish(book_key: str, root: str, stage_dir: str = "", dry_run: bool = False) -> dict:
    p = paths(resolve_key(book_key), root)
    stage = stage_dir or p["staging"]
    if not os.path.isdir(stage):
        return {"book": book_key, "result": "没有可发布的暂存区", "exit_code": EXIT_NOTHING,
                "staging": stage}
    # ① 校验绑定暂存区字节
    vr = V.validate(book_key, root, s7_dir=stage)
    if vr["exit_code"] != 0:
        return {"book": book_key, "result": f"拒绝发布：{vr['verdict']}", "exit_code": vr["exit_code"],
                "staging": stage, "formal_untouched": os.path.isdir(p["formal"]),
                "blocking": vr["blocking"], "verdict": vr["verdict"]}
    # ② 事务日志 + 旧版移开（移动，不删除）
    stamp = _now_stamp()
    prev = os.path.join(p["published_dir"], stamp) if os.path.isdir(p["formal"]) else None
    staging_hashes = dir_hashes(stage)
    payload_staging = dir_hashes(stage, PAYLOAD_SKIP)
    if dry_run:
        return {"book": book_key, "result": "dry-run：校验通过，未发布", "exit_code": EXIT_OK,
                "staging": stage, "would_publish_to": p["formal"], "would_archive_previous_to": prev,
                "verdict": vr["verdict"]}
    _write_journal(p, state="in_progress", started_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                   staging=stage, target=p["formal"], previous=prev,
                   staging_hashes=staging_hashes, verdict=vr["verdict"])
    if prev:
        os.makedirs(p["published_dir"], exist_ok=True)
        shutil.move(p["formal"], prev)                    # ← 旧版移到存档区
    shutil.move(stage, p["formal"])                       # ← 暂存区升为正式
    # ③ 发布后再校验（判决绑定正式快照）
    vr2 = V.validate(book_key, root, s7_dir=p["formal"])
    formal_hashes = dir_hashes(p["formal"])
    payload_formal = dir_hashes(p["formal"], PAYLOAD_SKIP)
    state = "done" if vr2["exit_code"] == 0 else "post_verify_failed"
    _write_journal(p, state=state, finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
                   post_publish_verdict=vr2["verdict"], published_hashes=formal_hashes,
                   payload_hashes_staging=payload_staging, payload_hashes_published=payload_formal,
                   payload_hashes_match=(payload_staging == payload_formal))
    return {"book": book_key,
            "result": "已发布" if state == "done" else "发布后复验失败",
            "exit_code": EXIT_OK if state == "done" else EXIT_POST_FAIL,
            "formal": p["formal"], "archived_previous": prev, "journal": p["journal"],
            "verdict": vr["verdict"], "post_publish_verdict": vr2["verdict"],
            "files": len(formal_hashes), "payload_files": len(payload_formal),
            "payload_hashes_match": payload_staging == payload_formal}


def recover(book_key: str, root: str) -> dict:
    """中断恢复：**只移动，不删除**。"""
    p = paths(resolve_key(book_key), root)
    if not os.path.exists(p["journal"]):
        return {"book": book_key, "result": "没有事务日志，无需恢复", "state": "none"}
    j = json.load(open(p["journal"], encoding="utf-8"))
    if j.get("state") == "done":
        return {"book": book_key, "result": "上一次发布已正常完成", "state": "done"}
    prev, target = j.get("previous"), j.get("target") or p["formal"]
    if not os.path.isdir(target) and prev and os.path.isdir(prev):
        shutil.move(prev, target)
        _write_journal(p, state="recovered_previous")
        return {"book": book_key, "result": "已把存档版移回正式目录", "state": "recovered_previous",
                "restored_from": prev}
    if os.path.isdir(target):
        _write_journal(p, state="recovered_target_present")
        return {"book": book_key, "result": "正式目录已存在，无需恢复", "state": "recovered_target_present"}
    _write_journal(p, state="recover_failed")
    return {"book": book_key, "result": "无法恢复（正式目录与存档版都不存在）", "state": "recover_failed"}


def cmd_publish(a) -> int:
    res = publish(a.book, a.artifact_root, a.stage or "", a.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res["exit_code"]


def cmd_recover(a) -> int:
    res = recover(a.book, a.artifact_root)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if not res["state"].endswith("failed") else EXIT_POST_FAIL


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    import tempfile
    cases, passed = [], 0
    root = tempfile.mkdtemp(prefix="pub-")

    def seed_root() -> None:
        """首次：造一棵有效产物树（含上游阶段），并把 s7-out/fx 当作'暂存区'（模拟 S7 只写暂存区）。"""
        V._fixture(root, pages=(1, 2))
        p = paths("fx", root)
        os.makedirs(os.path.dirname(p["staging"]), exist_ok=True)
        if os.path.isdir(p["staging"]):
            shutil.rmtree(p["staging"])
        shutil.move(p["formal"], p["staging"])

    def stage_fresh() -> None:
        """后续：用**独立副本**刷新暂存区——**不碰正式目录**（这正是阶段 2 的关键性质）。"""
        scratch = tempfile.mkdtemp(prefix="pub-src-")
        V._fixture(scratch, pages=(1, 2))
        p = paths("fx", root)
        if os.path.isdir(p["staging"]):
            shutil.rmtree(p["staging"])
        os.makedirs(os.path.dirname(p["staging"]), exist_ok=True)
        shutil.copytree(os.path.join(scratch, "s7-out", "fx"), p["staging"])
        shutil.rmtree(scratch, ignore_errors=True)

    seed_root()
    p = paths("fx", root)
    r1 = publish("fx", root)
    cases.append(("① 首次发布：校验通过 → 正式目录出现、日志 done",
                  r1["exit_code"] == 0 and os.path.isdir(p["formal"])
                  and json.load(open(p["journal"], encoding="utf-8"))["state"] == "done",
                  {"res": r1.get("result"), "exit": r1["exit_code"]}))
    cases.append(("① 首次发布：没有旧版本 → 存档区为空",
                  r1["archived_previous"] is None and not os.path.isdir(p["published_dir"]), None))

    # ② 二次发布：旧版必须**被移动**到存档区（不删除）
    old_files = set(dir_hashes(p["formal"]).keys())
    stage_fresh()
    with open(os.path.join(p["staging"], "NEW_FILE.txt"), "w", encoding="utf-8") as fh:
        fh.write("second publish\n")
    r2 = publish("fx", root)
    archived = r2["archived_previous"]
    cases.append(("② 二次发布：旧版被移动到存档区，且旧文件仍在（绝不删除）",
                  r2["exit_code"] == 0 and archived and os.path.isdir(archived)
                  and old_files <= set(dir_hashes(archived).keys()),
                  {"archived": archived}))
    cases.append(("② 二次发布：正式目录含新版文件、不含暂存区",
                  os.path.exists(os.path.join(p["formal"], "NEW_FILE.txt"))
                  and not os.path.isdir(p["staging"]), None))
    cases.append(("② 发布后复验：交付载荷逐字节一致 + 判决绑定正式快照",
                  r2["payload_hashes_match"] and r2["post_publish_verdict"] == r2["verdict"]
                  and r2["post_publish_verdict"].startswith("可交付"),
                  {"payload_match": r2["payload_hashes_match"],
                   "post": r2["post_publish_verdict"], "staging": r2["verdict"]}))

    # ③ 拒绝发布：暂存区不合格 → 正式目录**一动不动**
    before = dir_hashes(p["formal"])
    stage_fresh()
    md = _first_reviewed(p["staging"])
    src = open(os.path.join(p["staging"], md), encoding="utf-8").read()
    open(os.path.join(p["staging"], md), "w", encoding="utf-8").write(
        src.replace("<!-- p002 -->", ""))                 # 抹掉一页的页边界 → G1/G2 应报
    r3 = publish("fx", root)
    cases.append(("③ 校验不过 → 拒绝发布、退出码透传",
                  r3["exit_code"] in (1, 2) and "拒绝发布" in r3["result"],
                  {"exit": r3["exit_code"], "result": r3["result"]}))
    cases.append(("③ 拒绝发布后：正式目录与发布前逐文件一致（无半成品）",
                  dir_hashes(p["formal"]) == before, None))
    cases.append(("③ 拒绝发布后：暂存区原样保留（未被消耗、也未被改成正式目录）",
                  os.path.isdir(p["staging"]) and "<!-- p002 -->" not in open(
                      os.path.join(p["staging"], _first_reviewed(p["staging"])),
                      encoding="utf-8").read(), None))

    # ④ 中断恢复：日志 in_progress、正式目录缺失、存档版在 → 移回
    prev = os.path.join(p["published_dir"], "TESTSTAMP")
    os.makedirs(p["published_dir"], exist_ok=True)
    shutil.move(p["formal"], prev)
    _write_journal(p, state="in_progress", target=p["formal"], previous=prev)
    r4 = recover("fx", root)
    cases.append(("④ 中断恢复：把存档版移回正式目录",
                  r4["state"] == "recovered_previous" and os.path.isdir(p["formal"])
                  and not os.path.isdir(prev), r4))

    # ⑤ 没有暂存区 → 退出码 5
    r5 = publish("nothing", root)
    cases.append(("⑤ 没有暂存区 → 退出码 5（不是静默成功）", r5["exit_code"] == EXIT_NOTHING, r5))
    shutil.rmtree(root, ignore_errors=True)
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {extra}"))
        passed += bool(ok)
    print(f"\n{passed}/{len(cases)} publish tests passed")
    return 0 if passed == len(cases) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("publish", "recover"):
        s = sub.add_parser(name)
        s.add_argument("--book", required=True)
        s.add_argument("--artifact-root", default=_paths.data_root())
        if name == "publish":
            s.add_argument("--stage", default="")
            s.add_argument("--dry-run", action="store_true")
            s.set_defaults(func=cmd_publish)
        else:
            s.set_defaults(func=cmd_recover)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
