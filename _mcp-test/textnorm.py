# -*- coding: utf-8 -*-
"""正文归一化的**唯一实现**（SPEC v2 §2.1 的 N1–N6 + D2）。

⚠️ 目前 `s5_queue_gate.py` 里仍有一份等价副本（首轮写就）；下一轮应改为从本模块导入
（见 `docs/new/S2-IR适配器-结果.md` 的"已知重复"）。本模块只做**比较视图**用的归一化——
U-01 默认口径：**产物字节保真，归一只作用于比较/检索视图**。
"""
from __future__ import annotations

import difflib
import re
from tuning import TUNE

# "CJK 侧"字符类：汉字 + CJK 标点 + 全角形式 + **带圈字母数字（①②…，脚注标记）** + 省略号
CJK = r"\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\u2460-\u24ff\u2026"
ALIGN_ARTIFACT_CHARS = TUNE["align_artifact_chars"]          # L2（单点：tuning.json）
MIN_PREFIX_FOR_TRUNCATION = TUNE["min_prefix_for_truncation"]  # L2

ALIASES = [
    (r"\mathrm{^{\circ}C}", "\u00b0C"),
    (r"^{\circ}\mathrm{C}", "\u00b0C"),
    (r"^{\circ}C", "\u00b0C"),
    (r"^{\circ} C", "\u00b0C"),
    (r"^{\circ}", "\u00b0"),        # 单独的角度号（实测：有机化学里 ° 与 ^{\circ} 混用）
    (r"\sim", "~"),                 # 实测：\sim 与全角 ～
    (r"\approx", "\u2248"),
    (r"\mathrm{~\u00b0C}", "\u00b0C"),
    ("\u2103", "\u00b0C"),          # ℃
    (r"^{\prime}", "'"),
    (r"^{' }", "'"),
    (r"^ {'}", "'"),
    (r"^{'}", "'"),
    (r"\prime", "'"),
    (r"\Delta", "\u0394"),          # Δ
    (r"\left", ""), (r"\right", ""),
    (r"\qquad", " "), (r"\quad", " "),
    (r"\dfrac", r"\frac"),
    (r"\,", ""), (r"\;", ""), (r"\!", ""),
]
# 单字符上下标的花括号是记法差异：p_{2} 与 p_2 同义
SINGLE_CHAR_BRACE = re.compile(r"([_^])\{([^{}])\}")
PUNCT_FOLD = {
    "\uff0c": ",", "\uff1a": ":", "\uff1b": ";", "\u3001": ",",
    "\uff08": "(", "\uff09": ")", "\uff1d": "=", "\uff0e": ".",
    "\uff1f": "?", "\uff01": "!", "\u201c": "\"", "\u201d": "\"",
    "\u2018": "'", "\u2019": "'", "\u300a": "<", "\u300b": ">",
    "\u2014": "-", "\uff0d": "-", "\uff5e": "~", "\uff05": "%",
    "\uff0b": "+", "\uff0f": "/", "\uff03": "#", "\uff06": "&", "\uff0a": "*",
    # 注意：`。` 永不折叠（与小数点不可区分，SPEC N5）
}


def apply_aliases(t: str) -> str:
    for src, dst in ALIASES:
        t = t.replace(src, dst)
    t = SINGLE_CHAR_BRACE.sub(r"\1\2", t)
    return t


def collapse_command_spaces(t: str) -> str:
    t = re.sub(r"\\([A-Za-z]+)\s*\{", r"\\\1{", t)
    t = re.sub(r"([_^])\s*\{", r"\1{", t)
    t = re.sub(r"\{\s+", "{", t)
    t = re.sub(r"\s+\}", "}", t)
    return t


def strip_math_spaces(t: str) -> str:
    def _strip(m):
        return re.sub(r"\s+", "", m.group(0))
    t = re.sub(r"\$\$.+?\$\$", _strip, t, flags=re.S)
    t = re.sub(r"\$.+?\$", _strip, t, flags=re.S)
    return t


def fold_cjk_ascii_spaces(t: str) -> str:
    t = re.sub(r"(?<=[" + CJK + r"])[ \t]+(?=[0-9A-Za-z])", "", t)
    t = re.sub(r"(?<=[0-9A-Za-z])[ \t]+(?=[" + CJK + r"])", "", t)
    return t


def fold_punct(t: str) -> str:
    for a, b in PUNCT_FOLD.items():
        t = t.replace(a, b)
    return t


def norm_alias(t: str) -> str:
    """只做记号别名与命令空格（用于区分 N-3 与 N-2）。"""
    return collapse_command_spaces(apply_aliases(t or ""))


def norm_strong(t: str) -> str:
    """数学区空白无关、且剥掉非内容包装（`\\text{}`/`$`/`~`），但保留数学区外的空白。

    顺序有讲究：`strip_math_spaces` 依赖 `$` 才能识别数学区，所以剥包装必须放在它**之后**。
    """
    t = apply_aliases(t or "")
    t = strip_math_spaces(t)
    t = unwrap_commands(t)
    t = collapse_command_spaces(t)
    return fold_cjk_ascii_spaces(t)


def unwrap_commands(t: str) -> str:
    """剥掉**不是内容**的包装：`\\text{X}`/`\\mathrm{X}` 外壳、`$` 数学定界符、`~`（LaTeX 空距）。"""
    for cmd in ("text", "mathrm", "operatorname", "textbf", "textit", "textnormal"):
        t = re.sub(r"\\" + cmd + r"\{([^{}]*)\}", r"\1", t)
    t = t.replace("$", "").replace("~", "")
    return t


def norm_weak(t: str) -> str:
    """按 D2：**全/半角标点**与数学区空白不计；**但 ASCII↔ASCII 空白与数字间的空白照旧算**（SPEC N3/N4 的禁止归一化清单）。"""
    return fold_punct(norm_strong(t))


def norm_nospace(t: str) -> str:
    """**仅用于给差异区间分类**（判断"这一处差异是不是纯粹空白"），不参与内容判定。"""
    return re.sub(r"\s+", "", norm_weak(t))


def diff_regions(a: str, b: str, ctx: int = 30) -> dict:
    """归一后用序列对齐裁出真实差异区间；"只在一侧延伸到串尾"的块判为尾部截断。"""
    a, b = norm_strong(a), norm_strong(b)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = sm.get_opcodes()
    equal_run = max((i2 - i1 for tag, i1, i2, _, _ in ops if tag == "equal"), default=0)
    truncation_allowed = equal_run >= MIN_PREFIX_FOR_TRUNCATION
    regions, tail_truncation, tail_substantive, tail_text = [], False, False, ""
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        is_last = (idx == len(ops) - 1)
        tail_ext_a = (i2 == len(a) and j1 == j2 == len(b))
        tail_ext_b = (j2 == len(b) and i1 == i2 == len(a))
        if is_last and truncation_allowed and (tail_ext_a or tail_ext_b):
            tail_truncation = True
            tail_text = a[i1:i2] if tail_ext_a else b[j1:j2]
            if len(tail_text) >= 20:
                tail_substantive = True
            continue
        regions.append({"kind": tag,
                        "a": a[max(0, i1 - ctx):min(len(a), i2 + ctx)],
                        "b": b[max(0, j1 - ctx):min(len(b), j2 + ctx)],
                        "a_raw": a[i1:i2], "b_raw": b[j1:j2],
                        "a_len": i2 - i1, "b_len": j2 - j1})
    return {"regions": regions,
            "a_len": sum(r["a_len"] for r in regions),
            "b_len": sum(r["b_len"] for r in regions),
            "a_mid": " | ".join(r["a"] for r in regions),
            "b_mid": " | ".join(r["b"] for r in regions),
            "tail_truncation": tail_truncation, "tail_substantive": tail_substantive,
            "tail_text": tail_text[:120]}
