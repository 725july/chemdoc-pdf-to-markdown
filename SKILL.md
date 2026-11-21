---
name: chemdoc-pdf-to-markdown
description: 把教材/书籍 PDF 转成可追溯、可交付 Markdown 的双引擎审计工作流（两套 OCR 互证 + 视觉判定 + 22 道门禁 + 交付说明 + 校对包）。当用户要把 PDF 书/教材/扫描件转成交付级 Markdown、要交叉核验 OCR 产物、或要审计既有产物时使用。
---

# chemdoc-pdf-to-markdown

## 什么时候用

- 用户给出 PDF（教材 / 书籍 / 扫描件 / 电子版），要**转成 Markdown**、要**校对 OCR**、要**双引擎交叉核验**，
  或要**可追溯、可交付**的产物（门禁 + 台账 + 交付说明）。
- **不适合**：只想要"一次 OCR 出个大概文本"的场合（本工作流的价值在**互证与可追溯**，流程与成本都更高）。

## 第一步做什么

1. `pwsh -ExecutionPolicy Bypass -File .\install.ps1`（装环境；**需 PowerShell 7+**；幂等；离线也能装）
2. 想先看成果：双击 `离线演示.cmd`（9 页自制样书，不填密钥、不装 OCR、不用 GPU）
3. 要跑真书：`docs\01-快速开始.md` 的六步（登记 → 引擎 → 画像 → 管线 → 判定 → 校验/发布）
4. 跑引擎**不用设任何环境变量**：`& $py -X utf8 _mcp-test\engines.py run --slot witness --book <key>`
5. **版式：所有书都要声明（单栏也写）**。**先有 S2 的 IR**（`s2_ir.py build …` 或让管线跑到 S2）→ **每本书**都跑一次 `layout_declare.py suggest --book <key>`（只读，约 10 秒）→ 确认后 `intake.py layout --book <key> --layout "…"` 落盘（**只改声明、不重切**）；`主+边栏 / 三栏` 还要用户**确认切分线**。`suggest` 读的是 IR，不是引擎原生输出——IR 没建出来它无从比较。**没声明**：`preflight.py` 给警告（不阻塞），读序按几何（主+边栏会左右交错）。

## 别破什么

- **产出者不得自判**：判决只由 `_mcp-test\validate_run.py` 写。
- **判定必须留证**：`judge.model` / `judge.mode` 必填；宁缺勿猜，不确定写 `unclear`。
- **判词写错不会被"假装判过"**：`judge_worklist.py apply` 遇到词表外的判词**只拒那一条**（其余照写、退出码 3，该条留 `pending` 等补判）；提交前可加 `--check-only` 预检。判词 = `exists` / `not_exists` / `no_content_diff` / `unclear` / **`non_book_ink`**（+ 转写类 `transcribed` / `no_text`）。
- **这三类必须停下来问用户**（**不得代答**）：① **版式**（任何书都要声明，单栏也写）；② **非印刷内容**（读者手写 / 背面透印 / 印章 / 贴纸 / 装饰 —— 算不算书的内容，判 `non_book_ink`）；③ **原书疑似印错且影响语义**（登记 `errata(kind=source_typo)` 前先说）。**每次询问都带一个默认选项**——「后续同类问题都按这个处理，不再询问」；用户同意后 `intake.py ask-default --book <key> --kind non_book_ink`（或 `--kind layout`）记进注册表，后续**不再问**；改口加 `--reset` 后重跑（已落盘判定不回溯）。
- **密钥不进产物**；填密钥用 `输入密钥.cmd`。
- **数据和工具可以分家**：`CHEMDOC_ROOT` 指到别处即可（`paths.py show` 看当前值）。

## 参考

- `docs\01-快速开始.md` · `docs\02-使用说明.md` · `docs\03-能力与边界.md`
- 命令自检：任意模块 `selftest`（例：`python -X utf8 _mcp-test\validate_run.py selftest`）
