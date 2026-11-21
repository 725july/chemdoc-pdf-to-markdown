# -*- coding: utf-8 -*-
"""**L2 调优参数**（B1-4a）：把"能改变交付结论的数字"集中到 `tuning.json`，并给每个数字标**依据**。

**为什么要它**：SPEC 里"**不得拍数**"这条纪律以前只是**正文里的口号**；这里把它变成**机器可查的状态**：
每个参数必须带 `basis`，`default-unverified` 的一律**可见地计数**（在报告里、在自测里），
但**只观测、不阻断**（沿用 C3 的教训：不要把"应该让人看见"的东西做成硬拦）。

**边界**（谁该待在哪一层）：
  * `contracts.py` = **L0 契约**（词表/正则/格式、页空间规则）——改了就是改规格；
  * `books.json`   = **L1 部署配置**（书 ↔ 数据映射）；
  * `tuning.json`  = **L2 调优参数**（阈值/配额/上限）——本文件；
  * `measurements/` = **实测数据**（如 error-rate.json，是"量出来的数"，不是"判据的阈值"）。

**校验是硬的**（宁可加载失败，也不静默用错值）：缺 `basis` / basis 词不在枚举 / 未知键 /
类型不符 / **越界**（概率类必须 0–1 等）→ 一律报错。生效值与改造前的字面量**逐项一致**（零行为变化）。

用法:
  python -X utf8 tuning.py list      # 列出参数（值 + 依据 + 是否未验证）
  python -X utf8 tuning.py check     # 校验（+ 打印未验证计数）
  python -X utf8 tuning.py selftest
环境变量 `BOOKWF_TUNING` 可指向另一份参数文件（部署级覆盖，可选）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SCHEMA_VERSION = 1
BASIS_WORDS = ("measured", "ruling", "derived", "default-unverified")
TOP_KEYS = ("schema", "note", "params")
ENTRY_KEYS = ("value", "basis", "source", "note")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "tuning.json")

# 每个参数期望的类型 + 允许区间（越界即报错；`None` = 不限）
KINDS = {
    "ink_threshold_gray": ("int", (1, 255)),
    "figure_max_px": ("int", (0, 10 ** 5)),      # 图片裁剪后的最大宽度（0 = 不缩；P1-7 / 2026-11-15）
    "s6_column_min_blocks": ("int", (1, 50)),
    "r1_ink_min": ("number", (0.0, 1.0)),
    "align_artifact_chars": ("int", (1, 10 ** 9)),
    "min_prefix_for_truncation": ("int", (0, 10 ** 6)),
    "conf_threshold": ("number", (0.0, 1.0)),
    "pages_per_batch": ("int", (1, 10 ** 4)),
    "items_per_batch": ("int", (1, 10 ** 5)),
    "judge_calibrated": ("bool", None),
    "g10_human_facing_max_per_100": ("number", (0.0, 10 ** 6)),
    "g10_min_pages": ("int", (1, 10 ** 6)),
    "g14_upper95_max": ("number", (0.0, 10 ** 3)),
    "g14_missing_upper_default": ("number", (0.0, 10 ** 3)),
    "g3_dup_min_chars_ir": ("int", (1, 10 ** 4)),
    "g3_dup_min_chars_md": ("int", (1, 10 ** 4)),
    "vol_pages_paddle": ("int", (1, 10 ** 5)),
    "vol_bytes_paddle": ("int", (1, 10 ** 12)),
    "vol_pages_mineru": ("int", (1, 10 ** 5)),
    "vol_bytes_mineru": ("int", (1, 10 ** 12)),
    "s0_row_ink_min_px": ("number", (0, 10 ** 6)),
    "s0_min_block_ink_px": ("number", (0, 10 ** 6)),
    "s0_min_block_ink_px_margin": ("number", (0, 10 ** 6)),
    "s0_margin_frac": ("number", (0, 0.5)),
    "s0_furniture_min_ink_px": ("number", (0, 10 ** 6)),
    "s0_furniture_min_height_px": ("number", (0, 10 ** 4)),
    "s0_block_gap_frac": ("number", (0, 1)),
    "s0_line_gap_frac": ("number", (0, 1)),
    "s0_max_furniture_frac": ("number", (0, 1)),
    "s0_gutter_ink_frac": ("number", (0, 1)),
    "s0_gutter_min_width_frac": ("number", (0, 1)),
    "s0_skew_range": ("list3num", None),
    "s3_y_gap_frac": ("number", (0, 1)),
    "s3_region_len_ratio_max": ("number", (1, 10 ** 3)),
    "figcov_grid": ("int", (10, 2000)),
    "figcov_hole_min_frac": ("number", (0.0, 1.0)),
    "figcov_hole_min_page_area": ("number", (0.0, 1.0)),
    "figcov_hole_min_side": ("number", (0.0, 1.0)),
    "figcov_ink_min_px": ("int", (0, 10 ** 9)),
    "s6_band_header_frac": ("number", (0.0, 1.0)),
    "s6_band_footer_frac": ("number", (0.0, 1.0)),
    # B1（2026-10-25）：转写覆盖对账——空块/页末截断已被视觉转写补过时，不再要求人第二次确认
    "vision_cover_min_overlap": ("number", (0.0, 1.0)),
}


class TuningError(ValueError):
    """参数文件校验失败（**必须报错**，不许静默兜底）。"""


def _type_ok(kind: str, v) -> bool:
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "list3num":
        return (isinstance(v, list) and len(v) == 3
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v))
    return False


def load(path: str = "") -> dict:
    """读参数文件 → `{参数名: 值}`（已校验）。校验不过抛 `TuningError`。"""
    p = path or os.environ.get("BOOKWF_TUNING") or DEFAULT_PATH
    if not os.path.exists(p):
        raise TuningError("调优参数文件不存在：" + p + "（B1-4 起它不再是代码里的字面量）")
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise TuningError("顶层必须是对象")
    unk = sorted(set(data) - set(TOP_KEYS))
    if unk:
        raise TuningError("顶层出现未知字段：" + ", ".join(unk))
    if data.get("schema") != SCHEMA_VERSION:
        raise TuningError("schema 必须是 " + str(SCHEMA_VERSION) + "，实际 " + repr(data.get("schema")))
    params = data.get("params")
    if not isinstance(params, dict) or not params:
        raise TuningError("`params` 必须是非空对象")
    errs, out, meta = [], {}, {}
    for name, ent in params.items():
        if name not in KINDS:
            errs.append(name + "：**未知参数**（拼错名？新参数请先加进 tuning.py 的 KINDS）")
            continue
        if not isinstance(ent, dict):
            errs.append(name + "：必须是对象（value/basis/source/note）")
            continue
        unk2 = sorted(set(ent) - set(ENTRY_KEYS))
        if unk2:
            errs.append(name + "：未知字段 " + ", ".join(unk2))
        if "value" not in ent:
            errs.append(name + "：缺 value")
            continue
        b = ent.get("basis")
        if not b:
            errs.append(name + "：**缺 basis**（每个参数都必须说清依据）")
        elif b not in BASIS_WORDS:
            errs.append(name + "：basis 词非法 " + repr(b) + "（允许 " + "/".join(BASIS_WORDS) + "）")
        kind, rng = KINDS[name]
        v = ent["value"]
        if not _type_ok(kind, v):
            errs.append(name + "：类型不符，期望 " + kind + "，实际 " + type(v).__name__)
        elif rng and not (rng[0] <= float(v) <= rng[1]):
            errs.append(name + "：越界（允许 " + str(rng) + "，实际 " + str(v) + "）")
        if name == "judge_calibrated" and ent.get("basis") != "default-unverified" and v is False:
            errs.append("judge_calibrated：false 时 basis 应为 default-unverified")
        out[name] = v
        meta[name] = {"basis": b, "source": ent.get("source", ""), "note": ent.get("note", "")}
    miss = sorted(set(KINDS) - set(out))
    if miss:
        errs.append("缺少参数定义：" + ", ".join(miss))
    if errs:
        raise TuningError("调优参数校验失败：\n  - " + "\n  - ".join(errs))
    _META.clear()
    _META.update(meta)
    return out


_META: dict = {}


def meta() -> dict:
    return dict(_META)


def unverified(names=None) -> list:
    m = _META
    return sorted(n for n in (names or m) if (m.get(n) or {}).get("basis") == "default-unverified")


def snapshot() -> dict:
    """写进 `report.json` 的快照：生效值 + 依据 + **未验证计数**（观测，不阻断）。"""
    return {"schema": SCHEMA_VERSION, "values": dict(TUNE), "basis": meta(),
            "unverified": unverified(), "unverified_count": len(unverified())}


TUNE = load()

# 回归基线：**冻结**改造前的字面量（证明"搬到 JSON"没有改变任何取值）
EXPECTED = {
    "ink_threshold_gray": 120,  # 2026-10-13 由 128 标定（换 PDFium 后；见 tuning.json 的 source）
    "figure_max_px": 600,       # 2026-11-15（P1-7）宽图等比缩小上限（用户第二轮：900 仍嫌大 → 600）
    "r1_ink_min": 0.02, "align_artifact_chars": 300,
    "min_prefix_for_truncation": 20, "conf_threshold": 0.9, "pages_per_batch": 40,
    "items_per_batch": 39,
    # 2026-10-13 首次整本书交付（A3《濒危之城》）**由实测标定**：B001 恰好 39 条/20 页打满 → 置真；
    # 基线同步（同 `ink_threshold_gray` 的做法：标定即更新基线，并把依据写进 tuning.json 的 source）
    "judge_calibrated": True,
    "g10_human_facing_max_per_100": 20, "g10_min_pages": 30, "g14_upper95_max": 0.5,
    "g14_missing_upper_default": 9.0, "g3_dup_min_chars_ir": 25, "g3_dup_min_chars_md": 30,
    "vol_pages_paddle": 100, "vol_bytes_paddle": 45000000,
    "vol_pages_mineru": 180, "vol_bytes_mineru": 190000000,
    # ---- B1-4b：版面（s0）与出题（s3）阈值 ----
    "s0_row_ink_min_px": 2.0, "s0_min_block_ink_px": 8.0, "s0_min_block_ink_px_margin": 4.0,
    "s0_margin_frac": 0.15, "s0_furniture_min_ink_px": 12, "s0_furniture_min_height_px": 4,
    "s0_block_gap_frac": 0.012, "s0_line_gap_frac": 0.004, "s0_max_furniture_frac": 0.15,
    "s0_gutter_ink_frac": 0.02, "s0_gutter_min_width_frac": 0.03, "s0_skew_range": [-2.0, 2.0, 0.25],
    "s3_y_gap_frac": 0.010, "s3_region_len_ratio_max": 3.0,
    "figcov_grid": 200, "figcov_hole_min_frac": 0.30,
    "figcov_hole_min_page_area": 0.01, "figcov_hole_min_side": 0.02, "figcov_ink_min_px": 100,
    "s6_column_min_blocks": 3,
    "s6_band_header_frac": 0.15, "s6_band_footer_frac": 0.85,
    # 2026-10-25（B1 转写覆盖对账）：新参数进基线；依据写在 tuning.json 的 source
    "vision_cover_min_overlap": 0.5,
}


def selftest() -> int:
    cases = []
    cases.append(("生效值与**冻结基线**逐项一致（说明：`ink_threshold_gray` 于 2026-10-13 由 128 标定为 120，基线已同步）", TUNE == EXPECTED,
                  {k: (TUNE.get(k), v) for k, v in EXPECTED.items() if TUNE.get(k) != v}))
    cases.append(("每个参数都有 basis，且 basis 词合法",
                  all(meta()[k]["basis"] in BASIS_WORDS for k in TUNE), None))
    cases.append(("`items_per_batch` 的依据是 measured（历史下界）",
                  meta()["items_per_batch"]["basis"] == "measured", None))
    cases.append(("未验证参数**可见地计数**（只观测、不阻断）",
                  isinstance(unverified(), list) and len(unverified()) >= 6, unverified()))
    cases.append(("snapshot() 带值/依据/未验证计数（供 report.json 用）",
                  {"values", "basis", "unverified", "unverified_count"} <= set(snapshot())
                  and snapshot()["unverified_count"] == len(unverified()), None))

    # ---- 反证：**配置真被读**（不是摆设）----
    import tempfile
    with open(DEFAULT_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    import copy
    tmp = copy.deepcopy(raw)
    tmp["params"]["r1_ink_min"]["value"] = 0.99
    fd, p = tempfile.mkstemp(prefix="tuning-", suffix=".json")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(tmp, fh, ensure_ascii=False)
        alt = load(p)
        cases.append(("反证 A：换一份参数文件 → 读出的值随之变（0.02 → 0.99）",
                      alt["r1_ink_min"] == 0.99 and EXPECTED["r1_ink_min"] == 0.02, alt["r1_ink_min"]))
        import s5_queue_gate as QG
        cases.append(("反证 B：N-7 判据绑定的阈值 == 配置值（模块真的在用 TUNE）",
                      QG.ALIGN_ARTIFACT_CHARS == TUNE["align_artifact_chars"]
                      and QG.MIN_PREFIX_FOR_TRUNCATION == TUNE["min_prefix_for_truncation"], None))
        item = QG._item("T-ALIGN", "replace", "甲" * 400, "乙" * 400, [1, 1, 50, 50])
        old = QG.ALIGN_ARTIFACT_CHARS
        try:
            QG.ALIGN_ARTIFACT_CHARS = 10 ** 6                     # 阈值抬高 → 不再是"对齐失败"
            loose = QG.gate_item(item)
            QG.ALIGN_ARTIFACT_CHARS = 300
            strict = QG.gate_item(item)
        finally:
            QG.ALIGN_ARTIFACT_CHARS = old
        cases.append(("反证 C：改这个阈值 → 门禁结论真的翻转（300 拒 / 10^6 放行）",
                      strict["admitted"] is False and loose["admitted"] is True,
                      {"strict": strict["reject_reasons"], "loose": loose["reject_reasons"]}))
    finally:
        os.remove(p)

    # ---- 反证 D/E：版面与出题阈值**真的被读**（改它 → 结论翻转）----
    import numpy as _np
    import s0_classify as _s0
    _ink = _np.zeros((40, 10), dtype=_np.uint8)
    _ink[5:8, :] = 255
    _ink[20:23, :] = 255
    _old_row = _s0.ROW_INK_MIN_PX
    try:
        _s0.ROW_INK_MIN_PX = 2.0
        _n_low = len(_s0.segment_blocks(_ink))
        _s0.ROW_INK_MIN_PX = 10 ** 6
        _n_high = len(_s0.segment_blocks(_ink))
    finally:
        _s0.ROW_INK_MIN_PX = _old_row
    cases.append(("反证 D：改「行有墨」阈值 → 分段结果翻转（s0 真的在读配置）",
                  _n_low > 0 and _n_high == 0, {"低阈值段数": _n_low, "高阈值段数": _n_high}))
    cases.append(("反证 D2：s0 的 12 个阈值都绑定到 TUNE",
                  all(getattr(_s0, _n) == (tuple(TUNE[_k]) if _k == "s0_skew_range" else TUNE[_k])
                      for _n, _k in (("ROW_INK_MIN_PX", "s0_row_ink_min_px"),
                                     ("MIN_BLOCK_INK_PX", "s0_min_block_ink_px"),
                                     ("MIN_BLOCK_INK_PX_MARGIN", "s0_min_block_ink_px_margin"),
                                     ("MARGIN_FRAC", "s0_margin_frac"),
                                     ("FURNITURE_MIN_INK_PX", "s0_furniture_min_ink_px"),
                                     ("FURNITURE_MIN_HEIGHT_PX", "s0_furniture_min_height_px"),
                                     ("BLOCK_GAP_FRAC", "s0_block_gap_frac"),
                                     ("LINE_GAP_FRAC", "s0_line_gap_frac"),
                                     ("MAX_FURNITURE_FRAC", "s0_max_furniture_frac"),
                                     ("GUTTER_INK_FRAC", "s0_gutter_ink_frac"),
                                     ("GUTTER_MIN_WIDTH_FRAC", "s0_gutter_min_width_frac"),
                                     ("SKEW_RANGE", "s0_skew_range"))), None))
    import s3_compare as _s3
    _a = {"bbox_norm": [0.1, 0.10, 0.9, 0.16], "kind": "text", "page": 1, "role": "primary"}
    _b = {"bbox_norm": [0.1, 0.20, 0.9, 0.26], "kind": "text", "page": 1, "role": "primary"}  # 纵向间隙 0.04
    _old_gap = _s3.Y_GAP_FRAC
    try:
        _s3.Y_GAP_FRAC = 0.010
        _r_split = len(_s3.build_regions([_a, _b], []))
        _s3.Y_GAP_FRAC = 0.05
        _r_merged = len(_s3.build_regions([_a, _b], []))
    finally:
        _s3.Y_GAP_FRAC = _old_gap
    cases.append(("反证 E：改「区域合并间隙」→ 区域数翻转（s3 真的在读配置）",
                  _r_split == 2 and _r_merged == 1, {"小间隙": _r_split, "大间隙": _r_merged}))
    cases.append(("反证 E2：s3 的两个出题阈值都绑定到 TUNE",
                  _s3.Y_GAP_FRAC == TUNE["s3_y_gap_frac"]
                  and _s3.REGION_LEN_RATIO_MAX == TUNE["s3_region_len_ratio_max"], None))

    # ---- 负例：校验必须真的会拦 ----
    import copy as _copy

    def _bad(mut, tag):
        cfg = _copy.deepcopy(raw)
        mut(cfg)
        fd2, p2 = tempfile.mkstemp(prefix="tuning-bad-", suffix=".json")
        os.close(fd2)
        try:
            with open(p2, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(cfg, fh, ensure_ascii=False)
            try:
                load(p2)
                return False
            except TuningError:
                return True
        finally:
            os.remove(p2)
    cases.append(("负例：缺 basis → 报错",
                  _bad(lambda c: c["params"]["r1_ink_min"].pop("basis"), "缺 basis"), None))
    cases.append(("负例：basis 词非法 → 报错",
                  _bad(lambda c: c["params"]["r1_ink_min"].update({"basis": "随便"}), "basis 非法"), None))
    cases.append(("负例：未知参数名（拼错）→ 报错",
                  _bad(lambda c: c["params"].update({"r1_ink_mim": {"value": 0.02, "basis": "ruling"}}),
                       "未知参数"), None))
    cases.append(("负例：类型不符（0.02 写成字符串）→ 报错",
                  _bad(lambda c: c["params"]["r1_ink_min"].update({"value": "0.02"}), "类型"), None))
    cases.append(("负例：越界（概率 > 1）→ 报错",
                  _bad(lambda c: c["params"]["r1_ink_min"].update({"value": 1.5}), "越界"), None))
    cases.append(("负例：漏掉某个参数定义 → 报错",
                  _bad(lambda c: c["params"].pop("conf_threshold"), "缺参数"), None))
    cases.append(("负例：文件不存在 → 报错（不许静默兜底）", _missing_raises(), None))

    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:150]))
        passed += bool(ok)
    print("\n%d/%d tuning tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


def _missing_raises() -> bool:
    try:
        load(os.path.join(HERE, "no-such-tuning.json"))
        return False
    except TuningError:
        return True


def cmd_list(_a) -> int:
    m = meta()
    print("参数文件：", os.environ.get("BOOKWF_TUNING") or DEFAULT_PATH)
    print("未验证（basis=default-unverified）：%d / %d" % (len(unverified()), len(TUNE)))
    print()
    for k in sorted(TUNE):
        b = (m.get(k) or {}).get("basis", "?")
        mark = "  ⚠未验证" if b == "default-unverified" else ""
        print("  %-30s %-22s %s%s" % (k, TUNE[k], b, mark))
        if (m.get(k) or {}).get("source"):
            print("        └ %s" % m[k]["source"])
    return 0


def cmd_check(_a) -> int:
    print(json.dumps({"params": len(TUNE), "unverified": unverified(),
                      "unverified_count": len(unverified())}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("selftest").set_defaults(func=lambda a: selftest())
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
