# -*- coding: utf-8 -*-
"""**L0 契约层**（B1-3）：跨模块契约的**唯一**声明处。权威仍是 `SPEC-化学教材PDF转Markdown-v2.md`。

**为什么要有这个文件**：实测发现有 **同名不同义**（比"重复定义"更危险）——
`TAG_RE` 在 `gates.py` 里是公式编号 `\tag{}`，在 `s7_output.py` 里却是 **HTML 标签剥离**；
`CHAPTER_RE` 在三个文件里是三套不同正则；`VERDICTS` 在两处是两套词表。
→ 本模块**按用途命名**，各模块 import 对应契约；`contracts.selftest` 会扫描并禁止再自行定义。

**取值口径**：全部照抄原来各文件的取值，**零行为变化**（改的是"在哪声明"和"叫什么名"）。
"""
from __future__ import annotations

import io
import os
import re

# ---- 家具（页眉/页脚/页码）：**取值在 6 个文件里完全一致**，可真合并 --------------
FURNITURE = {"running_head", "running_foot", "page_number"}

# ---- 页标记与 REVIEW 标记 ---------------------------------------------------
PAGE_MARK_RE = re.compile(r"<!--\s*p(\d{3,4})\s*-->")

# **宽松版**：只取"类别 + 其余"（`gates.py` 判痕迹用、`judge_worklist.py` 统计用）
REVIEW_RE = re.compile(r"<!--\s*REVIEW:\s*([A-Za-z0-9_]+)\s*(.*?)-->")
# **严格版**：要求 `page=` 紧随类别（`s8_review_kit.py` 生成校对包用）
# 说明：两套**语义确实不同**，强行合一会改产物 —— 所以保留两个**具名**模式，而不是让它们散在各文件里。
REVIEW_PAGE_RE = re.compile(r"<!--\s*REVIEW:\s*(\S+)\s+page=(\d+)([^>]*?)-->")

# ---- 章标题的三个**不同**用途（旧名都叫 CHAPTER_RE，是陷阱） --------------------
# `chapters.py`：抓"第N章"并**取号**（支持中文数字，且防止把"第1章节"误判）
CHAPTER_NO_RE = re.compile(r"^第\s*([0-9]+|[一二三四五六七八九十百零〇两]+)\s*章(?![0-9一二三四五六七八九十])")
# `s6_assemble.py`：只判"**是不是**章标题"（宽松，不取号）
CHAPTER_HEADING_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十]+\s*章")
# `s7_output.py`：只要**阿拉伯数字**章的引用
CHAPTER_ARABIC_RE = re.compile(r"第\s*([0-9]+)\s*章")

# ---- 两个**同名不同义**的正则（旧名都叫 TAG_RE） --------------------------------
# `gates.py`：公式编号
EQ_TAG_RE = re.compile(r"\\tag\{([^}]+)\}")
# `s7_output.py`：HTML 标签剥离
HTML_TAG_RE = re.compile(r"<[^>]+>")

# 灰度阈值（原 `INK_THRESHOLD = 128`）**已迁到 L2 调优参数**：见 `tuning.json` 的
# `ink_threshold_gray`（它属「能改变交付结论的阈值」，不是格式契约）。

# ---- 判定词表：两套**不同**词表 -------------------------------------------------
# 2026-10-26：加 `non_book_ink`（非本书内容：背面透印 / 读者手写 / 印章 / 贴纸 / 装饰）。
# 它**不改写、不删除**正文，只把该块降级成 `<!-- NON-BOOK-INK … -->` 注释（净版靠 G9 去注释自然不含）。
VERDICTS_S3 = ("exists", "not_exists", "no_content_diff", "unclear", "non_book_ink")
VERDICTS_WITH_TRANSCRIPTION = ("exists", "not_exists", "no_content_diff", "unclear", "non_book_ink",
                               "transcribed", "no_text")

# **阈值类**名字：B1-4a 起这些值必须在 `tuning.json` 里，**不允许**在代码里写数值字面量。
# 允许的写法是"从 TUNE 绑定"（`R1_INK_MIN = TUNE["r1_ink_min"]`）——它不以数字字面量开头。
THRESHOLD_NAMES = ("R1_INK_MIN", "CONF_THRESHOLD", "ALIGN_ARTIFACT_CHARS",
                   "MIN_PREFIX_FOR_TRUNCATION", "INK_THRESHOLD")


def scan_hardcoded_thresholds(mcp_dir: str | None = None) -> list:
    """扫在册文件里"把阈值写死成数值"的地方（B1-4a 的防退化断言）。"""
    d = mcp_dir or os.path.dirname(os.path.abspath(__file__))
    pat = re.compile(r"^(" + "|".join(THRESHOLD_NAMES) + r")\s*=\s*[0-9(]")
    hits = []
    for f in in_scope_scripts(d):
        for i, l in enumerate(io.open(os.path.join(d, f), encoding="utf-8").read().split("\n"), 1):
            if pat.match(l):
                hits.append({"file": f, "line": i, "text": l.strip()[:80]})
    return hits


# 供 `selftest` 扫描用：这些名字**只允许**在本文件里定义
CONTRACT_NAMES = ("FURNITURE", "PAGE_MARK_RE", "REVIEW_RE", "REVIEW_PAGE_RE", "CHAPTER_NO_RE",
                  "CHAPTER_HEADING_RE", "CHAPTER_ARABIC_RE", "EQ_TAG_RE", "HTML_TAG_RE",
                  "VERDICTS_S3", "VERDICTS_WITH_TRANSCRIPTION")
# 旧的歧义名：在册文件里**不允许再作为定义出现**（用了就 import 具名契约）
AMBIGUOUS_NAMES = ("TAG_RE", "CHAPTER_RE", "VERDICTS")
# 冻结的一次性诊断脚本（用户裁定：原样保留，不纳入契约扫描）
FROZEN = ("probe_", "verify_", "check_", "s0_probe", "s2_probe", "s3_probe", "textnorm_probe",
          "gate_sim", "handshake", "add_candidate", "author_phase4", "build_retrieval",
          "compare_engines", "dump_mineru_blocks", "geometry_without_content", "ink_coverage",
          "inspect_ocr_raw", "lexical_searchability", "paddle_run_stability",
          "serialization_fold", "write_pilot_tasks")


def in_scope_scripts(mcp_dir: str | None = None) -> list:
    d = mcp_dir or os.path.dirname(os.path.abspath(__file__))
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".py") or f == "contracts.py":
            continue
        if any(f.startswith(x) for x in FROZEN):
            continue
        out.append(f)
    return out


def scan_redefinitions(mcp_dir: str | None = None) -> list:
    """扫在册文件里"自行定义契约名/歧义名"的地方。返回 [{file, line, name}]。"""
    d = mcp_dir or os.path.dirname(os.path.abspath(__file__))
    hits = []
    pat = re.compile(r"^(" + "|".join(CONTRACT_NAMES + AMBIGUOUS_NAMES) + r")\s*=")
    for f in in_scope_scripts(d):
        for i, l in enumerate(io.open(os.path.join(d, f), encoding="utf-8").read().split("\n"), 1):
            m = pat.match(l)
            if m:
                hits.append({"file": f, "line": i, "name": m.group(1)})
    return hits


def selftest() -> int:
    cases = []
    hits = scan_redefinitions()
    cases.append(("在册文件里**不得**再自行定义契约名（含旧歧义名 TAG_RE/CHAPTER_RE/VERDICTS）",
                  not hits, hits[:6]))
    cases.append(("FURNITURE 取值与六处旧定义一致",
                  FURNITURE == {"running_head", "running_foot", "page_number"}, sorted(FURNITURE)))
    cases.append(("两套 REVIEW 模式确实不同（宽松取类别 / 严格要 page=）",
                  REVIEW_RE.pattern != REVIEW_PAGE_RE.pattern
                  and REVIEW_RE.search("<!-- REVIEW: S3_content_diff page=2 -->") is not None
                  and REVIEW_PAGE_RE.search("<!-- REVIEW: S3_content_diff page=2 -->") is not None,
                  None))
    cases.append(("两个 TAG 类正则语义不同（公式编号 vs HTML 标签）",
                  EQ_TAG_RE.pattern != HTML_TAG_RE.pattern
                  and EQ_TAG_RE.search(r"$$x\tag{2.1}$$") is not None
                  and HTML_TAG_RE.search("<td>x</td>") is not None, None))
    cases.append(("章标题三个正则各司其职（取号 / 判断 / 阿拉伯）",
                  CHAPTER_NO_RE.match("第二章 气体") is not None
                  and CHAPTER_HEADING_RE.match("第二章 气体") is not None
                  and CHAPTER_ARABIC_RE.search("第二章 气体") is None
                  and CHAPTER_ARABIC_RE.search("第2章 气体") is not None, None))
    th = scan_hardcoded_thresholds()
    cases.append(("在册文件里**不得把阈值写死**（必须从 tuning.json 绑定）", not th, th[:4]))
    cases.append(("判定词表两套（S3 四词 / 带转写六词）",
                  len(VERDICTS_S3) == 5 and len(VERDICTS_WITH_TRANSCRIPTION) == 7
                  and "non_book_ink" in VERDICTS_S3 and "non_book_ink" in VERDICTS_WITH_TRANSCRIPTION, None))
    passed = 0
    for name, ok, extra in cases:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   -> " + str(extra)[:140]))
        passed += bool(ok)
    print("\n%d/%d contract tests passed" % (passed, len(cases)))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
