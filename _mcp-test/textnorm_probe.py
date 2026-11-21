# -*- coding: utf-8 -*-
"""归一化探针：给成对的写法，打印 norm_alias / norm_strong / norm_weak，看是否被判为"同一内容"。

用法: python -X utf8 textnorm_probe.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # 脚本自己所在目录（可整目录搬迁）
import textnorm as tn  # noqa: E402

PAIRS = [
    ("下标花括号", "V_{0}", "V_0"),
    ("三级下标", "V_{100}", "V_{100}"),
    ("撇号（LaTeX）", "V^{\\prime}", "V'"),
    ("撇号（花括号内引号）", "V^{'}", "V'"),
    ("撇号（纯引号）", "V^'", "V'"),
    ("度数记号", "t=^{\\circ}C", "t=\u00b0C"),
    ("度数记号2", "t=\\mathrm{~\u00b0C}", "t=\u00b0C"),
    ("text 包装", "$pV=\\text{恒量}$", "$pV=恒量$"),
    ("公式编号", "$pV=nRT\\tag{2.1}$", "$pV=nRT$"),
    ("全半角括号", "$(p,V,T)$", "（p、V、T）"),
    ("空白", "表 2.1 数据", "表2.1数据"),
    ("数字空格", "1 0 0", "100"),
    ("真差异-数字", "1.0 K", "4.2 K"),
]

if __name__ == "__main__":
    print(f"{'用例':<22}{'alias同':<9}{'strong同':<10}{'weak同':<8}  strong(A) | strong(B)")
    for name, a, b in PAIRS:
        row = (name, tn.norm_alias(a) == tn.norm_alias(b),
               tn.norm_strong(a) == tn.norm_strong(b), tn.norm_weak(a) == tn.norm_weak(b))
        print(f"{row[0]:<22}{str(row[1]):<9}{str(row[2]):<10}{str(row[3]):<8}  "
              f"{tn.norm_strong(a)!r} | {tn.norm_strong(b)!r}")
