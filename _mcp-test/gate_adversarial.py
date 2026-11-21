# -*- coding: utf-8 -*-
"""**A7-15 对抗测试**：门禁会不会「还在、但看不到」（**数据方向**的检查）。

与**变异探针** `mutation_probe.py`（**代码方向**）正交，缺一不可：

  * 变异探针：「**判据被改坏**时，自测必须变红」—— 武器被卸了，测试要喊；
  * 对抗测试：「门禁**读错了权威来源** / **静默跳过比较对象** / **两个权威来源各说各话**」—— 枪还在，但**瞄错了**。

口径见 `SPEC-…v2.md` §5.3 与 `docs/new/门禁与测试的分工-为什么没报警.md`。

**设计要点（每条都是踩出来的，别改回去）**：

  1. **基线必绿**：未篡改的夹具必须 0 违规 / 可交付 —— 否则一切「抓住」都是**假阳性**
     （变异探针踩过：不校准基线时 8 条「被抓」全是假的）。
  2. **无害篡改不得被抓**：同值重写 / 加一个无人读取的字段 / 只动时间戳 → 必须**不**报抓。
  3. **只认指名判据**：断言必须点名**期望的 violation kind**；**不接受**「反正它红了就算抓住」
     （那会把「弄坏了别的东西」记成功劳）。
  4. **不碰真实交付物**：默认在**临时目录**（`tempfile`；全量回归会把它指到工作区临时区）的合成夹具上
     动手（无 PDF、无 OCR、无子进程）；
     `--real <书>` 只做「**只读复刻**」—— 把台账/IR/产物**复制**到临时根，篡改只落副本，
     并在首尾核对源文件 `sha256`（"我只在副本上动手"要有证据，不靠自觉）。
  5. **不改代码**：把判据改坏是变异探针的活；此处只改**数据**。

用法:
  python -X utf8 gate_adversarial.py run                          # 合成夹具（默认；已纳入全量回归）
  python -X utf8 gate_adversarial.py selftest                      # 同上
  python -X utf8 gate_adversarial.py run --real <书key>            # 真书只读复刻（可选；较慢）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import paths  # B2：工作根唯一出口（SPEC §7.10）

REPO = paths.MODULE_DIR          # 可迁移：脚本自己所在目录
ROOT = paths.data_root()                                # 产物根 = 上一级
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import delivery_note as DN                                   # noqa: E402
import judge_worklist as JW                                  # noqa: E402
import validate_run as VR                                    # noqa: E402

PAGES_PER_BATCH_FIXTURE = 1        # 夹具刻意切两批：`row_batch_page_mismatch` / 批级判据要有对象可比
KEY_FIXTURE = "fx"


# ---------------------------------------------------------------- 小工具
def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


class _Ctx:
    """一份（合成或复刻的）现场：**读走门禁的权威来源**、把篡改写回磁盘。

    刻意**只**用 `validate_run.build_ctx()` 与 `judge_worklist` 的公开入口读数据 ——
    这样"对抗测试读的东西"与"门禁读的东西"是同一份，篡改才真的打在判据的读数路径上。
    """

    def __init__(self, root: str, key: str):
        self.root, self.key = root, key
        self.ledger_dir = os.path.join(root, "s5-out", key)
        self.wl = os.path.join(self.ledger_dir, JW.WORKLIST_NAME)
        self.pg = os.path.join(self.ledger_dir, JW.PROGRESS_NAME)
        self.s7 = os.path.join(root, "s7-out", key)
        self.report_path = os.path.join(self.s7, "report.json")

    # ---- 读
    def rows(self) -> list[dict]:
        return [json.loads(l) for l in open(self.wl, encoding="utf-8") if l.strip()]

    def prog(self) -> dict:
        return json.load(open(self.pg, encoding="utf-8"))

    def recs(self) -> list[dict]:
        return list(self.prog().get("batches") or [])

    def report(self) -> dict:
        return json.load(open(self.report_path, encoding="utf-8"))

    def ingest_paths(self) -> list[str]:
        out = []
        base = os.path.join(self.ledger_dir, "runs")
        for b, _dirs, files in os.walk(base):
            for f in sorted(files):
                if f.startswith("verdicts.ingested.") and f.endswith(".jsonl"):
                    out.append(os.path.join(b, f))
        return out

    # ---- 写（**这是唯一的篡改入口**）
    def set_rows(self, rows: list[dict]) -> None:
        p = self.prog()
        JW.write_ledger(self.ledger_dir, rows, self.recs(),
                        {"book": p.get("book"), "created_at_utc": p.get("created_at_utc"),
                         "queue": p.get("queue"), "config": p.get("config")})

    def set_recs(self, recs: list[dict]) -> None:
        p = self.prog()
        JW.write_ledger(self.ledger_dir, self.rows(), recs,
                        {"book": p.get("book"), "created_at_utc": p.get("created_at_utc"),
                         "queue": p.get("queue"), "config": p.get("config")})

    def set_report(self, rep: dict) -> None:
        with open(self.report_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)

    def set_ingest(self, rows: list[dict]) -> None:
        paths = self.ingest_paths()
        assert paths, "该现场没有 ingest 文件（无法做 ingest 层篡改）"
        _write_jsonl(paths[0], rows)

    def ingest(self) -> list[dict]:
        paths = self.ingest_paths()
        return [json.loads(l) for l in open(paths[0], encoding="utf-8") if l.strip()] if paths else []

    def md_paths(self) -> list[str]:
        """产物正文（`-clean.md` 除外）：页标记类篡改就打在它上面。"""
        if not os.path.isdir(self.s7):
            return []
        return [os.path.join(self.s7, f) for f in sorted(os.listdir(self.s7))
                if f.endswith(".md") and not f.endswith("-clean.md")
                and os.path.isfile(os.path.join(self.s7, f))]

    def set_md(self, transform) -> None:
        """把每条正文按 `transform(text) -> text` 重写（页标记篡改用）。"""
        paths = self.md_paths()
        assert paths, "该现场没有产物正文（无法做页标记层篡改）"
        for p in paths:
            txt = open(p, encoding="utf-8").read()
            with open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(transform(txt))

    # ---- 语义化查询（给 precondition 用）
    def by_state(self, state: str) -> list[dict]:
        return [r for r in self.rows() if r.get("state") == state]

    # ---- 交付说明（A6：封条层篡改；说明由校验器写，路径固定）
    def note_path(self) -> str:
        return os.path.join(self.root, "s7-out", self.key, DN.NOTE_NAME)

    def edit_note(self, transform) -> None:
        """按 `transform(text) -> text` 重写交付说明（封条层篡改用）。"""
        p = self.note_path()
        assert os.path.isfile(p), "该现场没有交付说明（无法做封条层篡改）"
        txt = open(p, encoding="utf-8").read()
        new = transform(txt)
        assert new != txt, "这条篡改没有改动任何字节（用例失效）"
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new)

    def remove_note(self) -> None:
        os.remove(self.note_path())

    def decided(self) -> list[dict]:
        return self.by_state(JW.STATE_DECIDED)

    def has_ingest(self) -> bool:
        return bool(self.ingest_paths())

    def has_2_batches(self) -> bool:
        return len(self.recs()) >= 2


# ---------------------------------------------------------------- 合成夹具
def _build_synthetic(root: str) -> None:
    """造一份**基线应当全绿**的最小现场（S7 产物 + 队列 + 台账 + ingest + 报告）。

    刻意留**一条 pending**（普通 content_diff，既不是 R1 也不是数字风险）：
    这样"未判定必须显式登记"这条判据才有对象可篡改，而基线仍可交付。
    """
    qs = [
        {"item_id": "S3-P0001-C-1", "pdf_page": 1, "review_type": "content_diff",
         "bbox": [0.1, 0.2, 0.9, 0.3], "risk": {"signals": ["digit"]}},      # 数字风险 → G8 要看它已判
        {"item_id": "S3-P0001-C-2", "pdf_page": 1, "review_type": "content_diff",
         "bbox": [0.4, 0.2, 0.9, 0.3], "risk": {"signals": []}},
        {"item_id": "S3-P0002-S-1", "pdf_page": 2, "review_type": "structure_diff",
         "bbox": [0.1, 0.5, 0.9, 0.6]},
        {"item_id": "S3-P0002-C-3", "pdf_page": 2, "review_type": "content_diff",
         "bbox": [0.1, 0.7, 0.9, 0.8], "risk": {"signals": []}},             # ← 留在 pending
    ]
    VR._fixture(root, pages=(1, 2), questions=qs, r1_unclear=True,
                ingest_transcribe_verdict="no_text")
    queue = os.path.join(root, "s5-out", f"visual-queue.s3.{KEY_FIXTURE}.jsonl")
    items = [json.loads(l) for l in open(queue, encoding="utf-8") if l.strip()]
    rows = JW.build_rows(items)
    bs = JW.plan_batches(rows, pages_per_batch=PAGES_PER_BATCH_FIXTURE)
    rows = JW.assign_batches(rows, bs)
    recs = JW.batch_records(bs, rows)
    # 判完除 `S3-P0002-C-3` 以外的全部（转写项按 ingest 的口径写 `no_text`，保持台账/ingest 自洽）
    verdicts = [{"item_id": r["item_id"],
                 "verdict": ("no_text" if r["review_type"] == "transcribe_region" else "exists"),
                 "judge": {"model": "adversarial-fixture", "mode": "agent"}}
                for r in rows if r["item_id"] != "S3-P0002-C-3"]
    rows, _ = JW.apply_verdicts(rows, verdicts, batch_id=None)
    # 给行盖**真指纹**（否则 `ledger_binding_expired` 无从测起：无指纹 = legacy，不是过期）
    rows, _ = JW.refresh_bindings(rows, KEY_FIXTURE, root)
    JW.write_ledger(os.path.join(root, "s5-out", KEY_FIXTURE), rows, recs,
                    {"book": KEY_FIXTURE, "queue": queue})
    rep = json.load(open(os.path.join(root, "s7-out", KEY_FIXTURE, "report.json"), encoding="utf-8"))
    st = JW.status(rows, recs)
    rep["judgment"] = {"pending": st["pending_ids"], "by_priority": st["by_priority"]}
    with open(os.path.join(root, "s7-out", KEY_FIXTURE, "report.json"), "w",
              encoding="utf-8", newline="\n") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2)
    # **A6**：先跑一次校验 —— 让它把**交付说明 + 封条**写进现场。
    #   为什么必须做：G22 判的是「说明自上一轮校验后被改过」→ **没有封条就没有对象**
    #   （那样 N1/N2 两条用例会静默退化成"unverified"、看着像漏抓）。顺带让基线也覆盖 G22=pass。
    VR.validate(KEY_FIXTURE, root)


# ---------------------------------------------------------------- 判据读数（与 gates.g21 同口径）
def _read_g21(ctx: _Ctx) -> dict:
    """**按门禁的读数路径**取一遍数据（与 `gates.g21_judge_ledger` 逐项同源）。"""
    c = VR.build_ctx(ctx.key, ctx.root)
    return {
        "rows": c.worklist, "recs": c.judge_progress.get("batches") or [],
        "queue_ids": [t.get("item_id") for t in (c.tasks or [])] or None,
        "product_marks": JW.mark_categories_by_page(c.md or ""),
        "expired_ids": (c.report or {}).get("expired_verdicts") or [],
        "report_pending": ((c.report or {}).get("judgment") or {}).get("pending"),
        "key": c.key, "ingest": c.ingested, "root": c.root,
    }


def _tier1(ctx: _Ctx) -> dict:
    return JW.check_ledger(**_read_g21(ctx))


# ---------------------------------------------------------------- 篡改清单
def _batch_ids(ctx: _Ctx) -> list[str]:
    return [r["batch_id"] for r in ctx.recs()]


def _victim_same_page_twin(ctx: _Ctx) -> str:
    """挑一条**与另一条已判项同页**的已判行（按语义现场挑，**不许硬编码 item_id**）。

    ⚠️ 这个细节是踩出来的：若挑到的行**独占了它那一页**，把它的状态一改，
    「批页并集」那条判据会**顺带**抓住，于是"豁免必须有批准人"这条判据**根本没被测到**
    —— 实测：真书上第一版（硬编码合成夹具的 item_id）就是这么**静默变成空操作**的。
    """
    dec = ctx.decided()
    for r in dec:
        if sum(1 for x in dec if x["pdf_page"] == r["pdf_page"]) > 1:
            return r["item_id"]
    return dec[0]["item_id"] if dec else ""


def _cases() -> list[dict]:
    """**每条硬判据配一条篡改用例**（SPEC §5.3 契约第 2 条）。

    `kind = None` → **对照用例**：必须"没被抓"（基线绿 / 无害篡改）。
    `needs` → 该形态存在才跑（真书复刻模式下缺形态就**明示跳过**，不假装跑过）。
    """
    C: list[dict] = []

    def add(cid, layer, label, kind, tamper, needs=(), gate="", blocking=True):
        C.append({"id": cid, "layer": layer, "label": label, "kind": kind, "gate": gate,
                  "blocking": blocking, "tamper": tamper, "needs": tuple(needs)})

    # ---- 对照（探针自己也要被验）
    add("C1-baseline", "对照", "**原样不动**（基线必须绿）", None, lambda c: None)
    add("C2-noop", "对照", "台账**同值重写**（无害）", None,
        lambda c: c.set_rows(c.rows()))
    add("C3-harmless", "对照", "加**无人读取**的字段 + 只动时间戳（无害）", None,
        lambda c: (c.set_rows([dict(r, _note="无害字段") for r in c.rows()]),
                   c.set_report(dict(c.report(), checked_at="2026-01-01T00:00:00Z"))))

    # ---- 台账层
    add("L1-binding", "台账", "篡改某条判定的 `binding` 指纹", "ledger_binding_expired",
        lambda c: c.set_rows([dict(r, binding={"ir": "tampereddeadbeef", "volume": "v01"})
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]),
        needs=("binding",))
    add("L2-batch-orphan", "台账", "把一条判定挂到**不存在的批次**（B999）", "row_batch_not_in_plan",
        lambda c: c.set_rows([dict(r, batch_id="B999")
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))
    add("L3-batch-pagemiss", "台账", "把判定挂到**别的批**（存在，但页区间不含它）",
        "row_batch_page_mismatch",
        lambda c: c.set_rows([dict(r, batch_id=_batch_ids(c)[0])
                              if r.get("batch_id") == _batch_ids(c)[1] else r
                              for r in c.rows()]),
        needs=("2batches",))
    add("L4-decided-to-pending", "台账", "把一条**已判**改回 `pending`（报告未同步）",
        "pending_not_registered",
        lambda c: c.set_rows([dict(r, state=JW.STATE_PENDING)
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))
    add("L5-verdict-wiped", "台账", "**抹掉**一条已判的 `verdict`（下游 ingest 还算它已判）",
        "ledger_ingest_verdict_mismatch",
        lambda c: c.set_rows([dict(r, verdict=None)
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))
    add("L6-row-dropped", "台账", "**删掉**一条已判行（丢项）", "ledger_item_missing",
        lambda c: c.set_rows([r for r in c.rows()
                              if r["item_id"] != c.decided()[0]["item_id"]]))
    add("L7-row-ghost", "台账", "**多加**一条队列里没有的行", "ledger_item_extra",
        lambda c: c.set_rows(c.rows() + [dict(c.decided()[0], item_id="GHOST-1")]))
    add("L8-reject-no-dest", "台账", "让一条**拒收项没有去处**", "reject_without_destination",
        lambda c: c.set_rows([dict(r, state=JW.STATE_REJECTED, destination="")
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))
    add("L9-waived-no-approver", "台账", "把一条已判改成 `waived`，**拿不出谁批准**"
        "（受害行按语义挑：与另一条已判项**同页** → 批页并集不会顺带抓住它）",
        "waived_without_approval",
        lambda c: c.set_rows([dict(r, state=JW.STATE_WAIVED, waiver=None)
                              if r["item_id"] == _victim_same_page_twin(c) else r
                              for r in c.rows()]),
        needs=("decided",))
    add("L10-errata-no-block", "台账", "U 类（原文如此）条目**缺 `block_id`**",
        "errata:missing-block_id",
        lambda c: c.set_rows([dict(r, errata={"original": "原页字面", "suggestion": "保持"})
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))
    add("L11-errata-no-original", "台账", "U 类条目**写不出原页字面**", "errata:missing-original",
        lambda c: c.set_rows([dict(r, errata={"block_id": "mineru-p0001-b000-text"})
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))
    add("L12-errata-not-dict", "台账", "U 类条目**不是字典**（形态坏）", "errata:not-a-dict",
        lambda c: c.set_rows([dict(r, errata="感觉原书错了")
                              if r["item_id"] == c.decided()[0]["item_id"] else r
                              for r in c.rows()]))

    # ---- 批计划层
    add("B1-pages-dup", "批计划", "批页清单**未升序去重**", "batch_pages_not_sorted_unique",
        lambda c: c.set_recs([dict(r, pages_list=list(r["pages_list"]) * 2) if i == 0 else r
                              for i, r in enumerate(c.recs())]))
    add("B2-overlap", "批计划", "**两个批的页区间重叠**", "batch_page_overlap",
        lambda c: c.set_recs([dict(r, pages_list=[1]) if r["batch_id"] == "B002" else r
                              for r in c.recs()]),
        needs=("2batches",))
    add("B3-union-miss", "批计划", "批计划里**整批消失**（该页还有已判项 → 批页并集漏页）",
        "batch_page_union_mismatch",
        lambda c: c.set_recs([r for r in c.recs() if r["batch_id"] != "B002"]),
        needs=("2batches",))

    # ---- ingest 层（下游读数）
    add("I1-ingest-row-missing", "ingest", "ingest **少了**一条已判项", "decided_missing_from_ingest",
        lambda c: c.set_ingest([r for r in c.ingest()
                                if r["item_id"] != c.decided()[0]["item_id"]]),
        needs=("ingest",))
    add("I2-ingest-verdict-flip", "ingest", "ingest 里那条判定**取值被改**（exists→unclear）",
        "ledger_ingest_verdict_mismatch",
        lambda c: c.set_ingest([dict(r, verdict="unclear")
                                if r["item_id"] == c.decided()[0]["item_id"] else r
                                for r in c.ingest()]),
        needs=("ingest",))
    add("I3-ingest-wiped", "ingest", "ingest **整份清空**（模拟「下游读数丢了」）",
        "decided_missing_from_ingest",
        lambda c: c.set_ingest([]), needs=("ingest",))

    # ---- 产物自述层（"产物说没事"不等于没事）
    add("P1-expired-selfreport", "产物自述", "把一条判定塞进 `report.expired_verdicts`"
        "（**产物自述**说过期）", "expired_counted_as_decided",
        lambda c: c.set_report(dict(c.report(),
                                    expired_verdicts=[c.decided()[0]["item_id"]])),
        needs=("decided",))
    add("P2-pending-cleared", "产物自述", "**把报告的未判定清单清空**（台账里还有 pending）",
        "pending_not_registered",
        lambda c: c.set_report(dict(c.report(),
                                    judgment=dict((c.report().get("judgment") or {}), pending=[]))),
        needs=("pending",))

    # ---- 产物正文层（页标记；A7-2 把这条判据补进 G1 后，按 SPEC §5.3 维护规则在此配一条篡改）
    add("M1-page-marker-glued", "产物正文", "把某页的页标记**贴到注释块上**（按行切页看不见这条边界）",
        None, lambda c: c.set_md(_glue_first_marker), needs=("md",), gate="G1")
    add("M2-page-marker-dropped", "产物正文", "**删掉**某页的页标记（该页有正文块）",
        None, lambda c: c.set_md(_drop_first_marker), needs=("md",), gate="G1")
    # **软门禁**也要有篡改用例（SPEC §5.3 维护规则）：把一段正文**在同一页内**复制一遍
    #   → G3 必须 fail，**但不得阻断交付**（软门禁的语义要一起锁住）
    add("M3-samepage-duplicate", "产物正文", "在同一页内插入**两行相同的长正文**（模拟管线搬重）",
        None, lambda c: c.set_md(_inject_samepage_dup), needs=("md",), gate="G3", blocking=False)

    # ---- 交付说明层（A6：封条软门禁 G22；SPEC §3.12）
    #   说明由**校验器**每次校验整体重写 → "写没写"不用测；测的是**自上一轮校验后被改过 / 被删**。
    add("N1-note-number-edited", "交付说明", "**改掉交付说明里的一个数字**（封条对不上）",
        None, lambda c: c.edit_note(_bump_note_number), needs=("note",), gate="G22", blocking=False)
    add("N2-note-deleted", "交付说明", "**删掉**交付说明（封条还在，文件没了）",
        None, lambda c: c.remove_note(), needs=("note",), gate="G22", blocking=False)

    return C


def _bump_note_number(text: str) -> str:
    """把交付说明**机器块**里的第一个整数 +1（**语义化**：任何树都能改到，不许硬编码数值）。

    为什么不能硬编码：真书与合成夹具的数字不同（同一份用例要两边都跑）—— 硬编码会**静默变成空操作**，
    看起来像"门禁漏抓"（SPEC §5.3 第 6/7 条，实测踩过）。
    """
    m = DN.BLOCK_RE.search(text)
    assert m, "交付说明里没有机器可读块（无法做封条层篡改）"
    blk = json.loads(m.group(1))
    for k in ("human_facing_markers", "figures", "tables", "pages", "judged", "chapters"):
        if isinstance(blk.get(k), int):
            blk[k] = blk[k] + 1
            break
    else:
        raise AssertionError("机器块里没有一个可改的整数（用例失效）")
    new = json.dumps(blk, ensure_ascii=False, sort_keys=True, separators=(", ", ": "))
    return text.replace(m.group(0), "<!-- DELIVERY-NOTE " + new + " -->", 1)


def _first_standalone_marker(text: str):
    """定位**第一条"独占一行"的页标记**（合成夹具与真书都适用；按语义挑，不许硬编码页号）。

    注意正则**不要**用 `\\s*$`：那会把标记行后面的空行一起吞掉，`m.end()` 就不是"标记结尾"了
    （实测踩到：篡改后插入的正文被吞掉，用例静默变成另一种形态）。
    """
    return re.search(r"^<!--\s*p(\d{3,4})\s*-->[ \t]*$", text or "", flags=re.M)


def _glue_first_marker(text: str) -> str:
    """把第一条独占标记改成"贴在注释块后面"（复现真书 p8/p26/p34 的坏形态）。"""
    m = _first_standalone_marker(text)
    if not m:
        return text
    return (text[:m.start()] + "<!-- 已判定（备查·无需人工）：1 条 -->" + m.group(0)
            + "被粘住的正文" + text[m.end():])


def _drop_first_marker(text: str) -> str:
    """删掉第一条独占标记（该页有正文块 → G1 的 `hard_missing`）。"""
    m = _first_standalone_marker(text)
    if not m:
        return text
    return text[:m.start()] + text[m.end():]


def _inject_samepage_dup(text: str) -> str:
    """在**同一页内**插入两行相同的长正文（模拟"管线把同一段搬两遍"）→ G3 的"同一页内重复"。

    为什么是"插入"而不是"复制某一行"：合成夹具的正文行只有 22 字，**低于 G3 的最小长度阈值（30）**，
    复制它不会触发判据（实测踩到）。插入两行长文既能跨过阈值，又**不动任何既有行**（影响面最小）。
    """
    m = _first_standalone_marker(text)
    if not m:
        return text
    para = ("这是一段足够长的、被管线在同一页里写了两遍的正文，用于验证 G3 的同页重复判据。")
    ins = "\n" + para + "\n" + para
    return text[:m.end()] + ins + text[m.end():]


# ---------------------------------------------------------------- 跑一条用例
def _verb(ok: bool) -> str:
    return "抓住" if ok else "!! 漏 !!"


def _mutable_files(ctx: _Ctx) -> list[str]:
    """**"篡改面"的文件清单**（唯一口径，签名与还原都用它）。

    刻意**可枚举**：`s5-out/<key>/**`（台账 + 批清单 + runs/ingest）、`s7-out/<key>/*.md`（正文/页标记）、
    `s7-out/<key>/report.json`（产物自述）、`s7-out/<key>/交付说明.md` + `validation-report.json`（A6：校验器产物，
    封条就在后者里）。既用于"篡改真的改了字节吗"的核对，也用于真书复刻的还原。
    """
    out = []
    for base, _dirs, names in os.walk(os.path.join(ctx.root, "s5-out", ctx.key)):
        out += [os.path.join(base, n) for n in names]
    out += ctx.md_paths()
    for f in ("report.json", DN.NOTE_NAME, "validation-report.json"):
        p = os.path.join(ctx.root, "s7-out", ctx.key, f)
        if os.path.isfile(p):
            out.append(p)
    return sorted(set(out))


def _mutable_signature(ctx: _Ctx) -> str:
    """"篡改面"的字节指纹（台账/批清单/ingest/报告自述）。

    **为什么要有它**：若某条篡改**一个字节都没改**（例如挑的受害行在这本书里不存在），
    它看起来就像"门禁漏抓"—— 那是**用例失效**，不是判据缺口。实测踩过：真书上第一版
    L9 硬编码了合成夹具的 `item_id` → 空操作 → 假"漏"。
    """
    h = hashlib.sha256()
    for p in _mutable_files(ctx):
        h.update(os.path.relpath(p, ctx.root).replace("\\", "/").encode("utf-8"))
        h.update(_sha256_file(p).encode("utf-8") if os.path.isfile(p) else b"missing")
    return h.hexdigest()


def _run_case(case: dict, pristine: str, work_root: str, key: str, *, copy_tree: bool) -> dict:
    """在**一份干净的工作树**上做一件坏事 → 两层各查一遍 → 还原。

    `copy_tree=True`（合成夹具）：整树复制，隔离最强；
    `copy_tree=False`（真书复刻）：只**同步会被改的那几个文件**（见 `_sync_mutables` 的说明——
    真书整树 90 MB × 23 次会撞磁盘上限，实测 `WinError 112`）。
    """
    if copy_tree:
        if os.path.exists(work_root):
            shutil.rmtree(work_root, ignore_errors=True)
        shutil.copytree(pristine, work_root)
    else:
        # 真书复刻：工作树在 `run()` 里已经整树复制过一次，这里只把"会被改的文件"还原回 pristine
        _sync_mutables(work_root, pristine, key)
    ctx = _Ctx(work_root, key)
    sig_before = _mutable_signature(ctx)
    case["tamper"](ctx)
    noop = bool(case["kind"] or case.get("gate")) and _mutable_signature(ctx) == sig_before

    t1 = _tier1(ctx)
    rep = VR.validate(key, work_root)
    g21 = rep["gates"]["G21"]
    kinds = g21["measured"]["violations"]
    hard = rep["hard_gates_failed"] or []
    verdict = rep["verdict"]

    if case["kind"] is None and not case.get("gate"):      # 对照：必须**没**被抓
        ok = (t1["ok"] and not t1["violations"]
              and g21["status"] == "pass" and not hard and verdict.startswith("可交付"))
        detail = {"tier1": t1["violations_by_kind"], "G21": g21["status"],
                  "硬失败": hard, "判决": verdict}
    else:                                        # 篡改：必须**指名**抓住（判据层 + 接线层）
        if case.get("gate"):                     # 期望"某道门禁 fail"（不限于 G21 的 violation kind）
            g = case["gate"]
            got = (rep["gates"].get(g) or {}).get("status")
            if case.get("blocking", True):       # **硬**门禁：必须真的拦住交付
                blocked = (g in hard) and not verdict.startswith("可交付")
                ok = (got == "fail") and blocked and not noop
            else:                                # **软**门禁：必须红，但**不得**阻断交付（分级也要锁住）
                blocked = g not in hard
                ok = (got == "fail") and blocked and verdict.startswith("可交付") and not noop
            detail = {"门禁": g, "状态": got, "软门禁不阻断": blocked, "判决": verdict}
        else:                                    # 期望"某类 G21 违规"（判据层 + 门禁层都要命中）
            in_t1 = case["kind"] in t1["violations_by_kind"]
            in_g21 = case["kind"] in kinds
            blocked = ("G21" in hard) and not verdict.startswith("可交付")
            ok = in_t1 and in_g21 and blocked and not noop
            detail = {"判据层": in_t1, "门禁层": in_g21, "已拦住": blocked,
                      "违规": kinds, "判决": verdict}
        if noop:
            detail = {"无效篡改": "这条篡改**一个字节都没改**（用例失效，不是门禁漏抓）", **detail}
    return {"id": case["id"], "layer": case["layer"], "label": case["label"],
            "kind": case["kind"], "gate": case.get("gate", ""),
            "blocking": case.get("blocking", True),
            "ok": bool(ok), "detail": detail, "skipped": False}


def _skip(case: dict, why: str) -> dict:
    return {"id": case["id"], "layer": case["layer"], "label": case["label"],
            "kind": case["kind"], "gate": case.get("gate", ""),
            "blocking": case.get("blocking", True),
            "ok": True, "detail": {"跳过": why}, "skipped": True}


def _needs_ok(case: dict, ctx: _Ctx) -> tuple[bool, str]:
    for need in case["needs"]:
        if need == "binding":
            if not any(r.get("binding") for r in ctx.rows()):
                return False, "该书的台账行没有 `binding` 指纹（无过期可测）"
        elif need == "2batches":
            if not ctx.has_2_batches():
                return False, "该书只有 1 个判定批（批间判据无对象）"
        elif need == "ingest":
            if not ctx.has_ingest():
                return False, "该书没有 ingest 文件"
        elif need == "decided":
            if not ctx.decided():
                return False, "该书没有已判项"
        elif need == "pending":
            if not ctx.by_state(JW.STATE_PENDING):
                return False, "该书没有未判定项"
        elif need == "md":
            if not ctx.md_paths():
                return False, "该书没有产物正文（无法做页标记层篡改）"
        elif need == "note":
            if not os.path.isfile(ctx.note_path()):
                return False, "该现场没有交付说明（A6 未落地 / 未重新发布 → 封条层无对象）"
    return True, ""


# ---------------------------------------------------------------- 真书：只读复刻
#   只读复刻的**两个坑**（都是实测踩到的，别改回去）：
#   ① 交付目录里可能有 `_superseded/`（历次 review-kit 快照，实测 495 MB）→ 必须跳过；
#   ② **不要每个用例都整树拷贝**（90 MB × 23 次会撞磁盘上限，实测 `WinError 112`）→
#      复制**一次**，用例之间只"还原会被改的那几个文件"（sync）。
_SKIP_DIRS_S5 = {"crops", "renders", "packets"}
_SKIP_DIRS_ANY = {"_superseded"}
_SKIP_FILES_S5 = (".png", ".jpg", ".jpeg")


def _replicate_book(src_root: str, dst_root: str, key: str) -> list[str]:
    """把真书**门禁真正要读的小文件**复制到临时根（跳过裁图/渲染/历次快照大件）。

    返回**源文件清单**（供首尾 `sha256` 核对：证明"只读"不是自觉，是有证据的）。
    """
    touched: list[str] = []
    rels = ["s2-ir", f"s3-out/{key}", f"s4-out/{key}", f"s5-out/{key}",
            f"s6-out/{key}", f"s7-out/{key}", "ocr-raw"]
    for rel in rels:
        src = os.path.join(src_root, rel)
        if not os.path.isdir(src):
            continue
        in_s5 = rel.startswith("s5-out")
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS_ANY
                       and not (in_s5 and d in _SKIP_DIRS_S5)]
            for f in files:
                if in_s5 and f.lower().endswith(_SKIP_FILES_S5):
                    continue
                s = os.path.join(base, f)
                d = os.path.join(dst_root, os.path.relpath(s, src_root))
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
                touched.append(s)
    s5 = os.path.join(src_root, "s5-out")
    if os.path.isdir(s5):
        for f in sorted(os.listdir(s5)):
            if f.startswith("visual-queue") and key in f and f.endswith(".jsonl"):
                s = os.path.join(s5, f)
                d = os.path.join(dst_root, "s5-out", f)
                shutil.copy2(s, d)
                touched.append(s)
    return touched


def _sync_mutables(work: str, pristine: str, key: str) -> None:
    """把一份工作树**还原**成 pristine（只还原"篡改可能碰过"的那几个文件）。

    篡改面 = `_mutable_files()` 的口径（`s5-out/<key>/**` + `s7-out/<key>/*.md` + `report.json`），
    因此同步面**可枚举**；多出来的文件一并删掉（防泄漏到下一个用例）。
    """
    pctx = _Ctx(pristine, key)
    keep = {os.path.relpath(p, pristine) for p in _mutable_files(pctx)}
    dst_root = os.path.join(work, "s5-out", key)
    for base, _dirs, files in os.walk(dst_root):                 # 删掉多出来的（篡改没造过新文件，防御性）
        for f in files:
            p = os.path.join(base, f)
            if os.path.relpath(p, work) not in keep:
                os.remove(p)
    for rel in sorted(keep):
        s = os.path.join(pristine, rel)
        d = os.path.join(work, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)


# ---------------------------------------------------------------- 主流程
def _print_case(r: dict, idx: int, total: int) -> None:
    if r["skipped"]:
        print("SKIP  %02d/%02d [%s] %s   —— %s"
              % (idx, total, r["layer"], r["label"], r["detail"]["跳过"]))
        return
    if r["kind"] is None and not r.get("gate"):
        tail = "必须**没**被抓" if r["ok"] else "!! 探针假报（无害篡改被当成抓）!!"
        print(("PASS  " if r["ok"] else "FAIL  ") + "%02d/%02d [%s] %s   → %s（判决=%s）"
              % (idx, total, r["layer"], r["label"], tail, r["detail"].get("判决", "—")))
    else:
        want = ("门禁 `%s` fail%s" % (r["gate"], "" if r.get("blocking", True) else "（软·不得阻断）")
                ) if r.get("gate") else ("期望 `%s`" % r["kind"])
        print(("PASS  " if r["ok"] else "FAIL  ") + "%02d/%02d [%s] %s   → %s：%s"
              % (idx, total, r["layer"], r["label"], want, _verb(r["ok"])))
    if not r["ok"]:
        print("       详单：" + json.dumps(r["detail"], ensure_ascii=False)[:400])


def run(real_key: str = "") -> int:
    tmp = tempfile.mkdtemp(prefix="gate-adv-")
    pristine = os.path.join(tmp, "pristine")
    results: list[dict] = []
    try:
        if real_key:
            key = real_key
            src_root = ROOT
            touched = _replicate_book(src_root, pristine, key)
            before = {p: _sha256_file(p) for p in touched}
            work = os.path.join(tmp, "work")
            shutil.copytree(pristine, work)          # 工作树只整树复制**一次**（见 _sync_mutables 说明）
            copy_tree = False
        else:
            key = KEY_FIXTURE
            _build_synthetic(pristine)
            before = {}
            work = ""
            copy_tree = True
        pctx = _Ctx(pristine, key)
        cases = _cases()
        title = ("真书**只读复刻**：" + key) if real_key else "合成夹具（fx）"
        print("=== A7-15 对抗测试（数据方向）：%s ===" % title)
        print("    %d 条用例：**基线/无害对照** + 逐条**指名判据**的篡改；"
              "每条的断言 = 判据层 + 门禁接线层都要命中\n" % len(cases))
        for i, case in enumerate(cases, 1):
            ok, why = _needs_ok(case, pctx)
            if not ok:
                r = _skip(case, why)
            else:
                work_root = os.path.join(tmp, "case-%s" % case["id"]) if copy_tree else work
                r = _run_case(case, pristine, work_root, key, copy_tree=copy_tree)
            results.append(r)
            _print_case(r, i, len(cases))

        if real_key:                       # **首尾核对**：源文件一个字节都不许变
            after = {p: _sha256_file(p) for p in touched}
            changed = [os.path.relpath(p, ROOT) for p in touched if before.get(p) != after.get(p)]
            r = {"id": "R1-source-untouched", "layer": "真书", "kind": None,
                 "label": "复刻模式下**源文件零改动**（sha256 首尾一致）", "ok": not changed,
                 "detail": {"changed": changed}, "skipped": False}
            results.append(r)
            _print_case(r, len(results), len(results))

        ran = [r for r in results if not r["skipped"]]
        missed = [r for r in ran if not r["ok"]]
        skipped = [r for r in results if r["skipped"]]
        print("\n未抓住清单：" + ("无" if not missed else
                              "、".join("%s(%s)" % (r["id"], r["label"]) for r in missed)))
        if skipped:
            print("明示跳过（该现场没有该形态）：" + "、".join(r["id"] for r in skipped))
        print("%d/%d 对抗测试用例达标（篡改必须被**指名判据**抓住；基线全绿；无害篡改不得被抓）"
              % (len(ran) - len(missed), len(ran)))
        return 0 if not missed else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def selftest() -> int:
    return run()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--real", default="", help="真书 key（只读复刻；默认空 = 合成夹具）")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    return run(a.real)


if __name__ == "__main__":
    sys.exit(main())
