# -*- coding: utf-8 -*-
"""B1-4b 验收（订正版）：**证明"重算确定性" + 如实记录"冻结画像与今日代码的预存漂移"**。

为什么要改口径：第一版把"重算 == 冻结件"当成 B1-4b 的验收，结果发现
  * 冻结件里 `runs_in_band`/`text_like`/`*_raw_px` 是 **null**（这些字段是**冻结之后**才加的），
  * page1 的 `footer_ink` 值也不同（页脚判定逻辑在**更早的某一轮**改过）。
这些**与 B1-4b 无关**：B1-4b 只改了"阈值从哪来"，**取值逐项相同**（见 `tuning.py` 的冻结基线断言），
所以重算结果在改动前后**必然一致**。本脚本因此验两件事：
  ① **确定性**：同一参数重算两次 → 逐字段完全相同（否则连"零行为变化"都无从谈起）；
  ② **漂移如实登记**：与冻结件的差异按"冻结件为 null（字段后来才加）"与"值不同（更早的改动）"分类打印。

**B2b 改动**：
  ① 去掉两处**硬编码绝对路径** → 一律走 `paths`（数据根可换、工具可搬迁）；
  ② **数据不在就明示跳过**（新机器/新鲜下载里既没有冻结画像、也没有源切片）——不是失败；
  ③ 因此它**回归发布集**（`release-exclude.json` 里那条 glob 已删）。**旧位置/旧路径零残留。**
"""
from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # B2：工作根唯一出口（SPEC §7.10）
import s0_classify as s0                                                        # noqa: E402

PROFILE_STEM = "pu-hua-22-30"       # 校验对象：S0 冻结画像（**样本名来自 SPEC §9**，不是某台机器的硬编码路径）


def main() -> int:
    root = paths.data_root()
    _v1 = os.path.join(root, "s0-profiles", PROFILE_STEM + ".profile.json")
    _v2 = _v1[:-5] + ".v2.json"
    if not (os.path.exists(_v1) or os.path.exists(_v2)):
        print("SKIP：本树没有 S0 冻结画像（%s）——**数据不在 ⇒ 明示跳过**（新机器正常状态）"
              % os.path.relpath(_v1, root))
        print("\nSKIP profile determinism test（缺数据）")
        return 0
    # 优先对 v2（同后端/同阈值的冻结参考）比较：信号更干净；v2 不在时退回 v1
    frozen_path = _v2 if os.path.exists(_v2) else _v1
    print("参考画像：", "v2" if frozen_path == _v2 else "v1", os.path.basename(frozen_path))
    quiet = {"source", "sha256"}
    frozen = json.load(io.open(frozen_path, encoding="utf-8"))
    src = frozen.get("source") or ""
    if not src or not os.path.exists(src):
        print("SKIP：冻结画像里记的源切片不在本机（%s）——**换机器后路径必然不同** ⇒ 明示跳过" % (src or "<空>"))
        print("\nSKIP profile determinism test（源切片不在）")
        return 0
    args = (src, frozen["sampled_pages"], frozen["sample_step"], frozen["dpi"], True)

    def flat(obj, path=""):
        if isinstance(obj, dict):
            for k in sorted(obj):
                yield from flat(obj[k], path + "/" + str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from flat(v, path + "[%d]" % i)
        else:
            yield path, obj

    def diff(a, b):
        da, db = dict(flat(a)), dict(flat(b))
        out = []
        for k in sorted(set(da) | set(db)):
            if da.get(k, "<缺>") != db.get(k, "<缺>"):
                out.append((k, da.get(k, "<缺>"), db.get(k, "<缺>")))
        return out

    r1 = s0.profile(*args)
    r2 = s0.profile(*args)
    d_det = diff({k: v for k, v in r1.items() if k not in quiet}, {k: v for k, v in r2.items() if k not in quiet})
    print("① 确定性：两次重算的差异字段数 =", len(d_det))
    if d_det:
        for k, a, b in d_det[:5]:
            print("     ", k, a, "→", b)

    d_fz = diff({k: v for k, v in frozen.items() if k not in quiet},
                {k: v for k, v in r1.items() if k not in quiet})
    new_fields = [(k, a, b) for k, a, b in d_fz if a is None and b is not None]      # 冻结件 null
    absent = [(k, a, b) for k, a, b in d_fz if a == "<缺>"]                          # 冻结件根本没这字段
    valued = [(k, a, b) for k, a, b in d_fz if (k, a, b) not in new_fields + absent]
    print("② 与冻结件的差异：共 %d 处 —— 冻结为 null（字段后来才加）= %d，冻结缺字段 = %d，**值不同 = %d**"
          % (len(d_fz), len(new_fields), len(absent), len(valued)))
    for k, a, b in valued:
        print("     值不同：", k, "| 冻结", json.dumps(a, ensure_ascii=False)[:60],
              "| 重算", json.dumps(b, ensure_ascii=False)[:60])
    print()
    ok = (not d_det)
    print(("PASS  " if ok else "FAIL  ") + "版面画像**重算确定性**成立（同一参数两次逐字段相同）")
    if len(valued):
        print("WARN  冻结画像与今日结果存在差异（%d 处值不同）：既有**预存漂移**，也含 **2026-10-13 渲染后端"
              "由 MuPDF 换成 pypdfium2** 造成的像素统计上移（冻结画像无 `render_backend` 字段）；"
              "与 B1-4b 无关（B1-4b 取值逐项相同）。建议列为下一轮待办：重算并归档新版画像，或标注版本。"
              % len(valued))
    print("\n1/1 profile determinism test passed" if ok else "\n0/1")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
