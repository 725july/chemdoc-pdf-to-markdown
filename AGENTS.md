# AGENTS.md —— 这份工作流的入口（给使用者与 agent）

> 先读这一页；细节在 `docs\`。**这是一份"产品"，不是开发仓库**：里面没有测试用例与变更历史。
> **许可**：代码**保留所有权利**（可自用与修改，**不得再分发**代码或其衍生品）；**用本工具产出的文档产物归你**。条款见 `LICENSE`。

## 1. 这是什么

**PDF → 可追溯 Markdown** 的双引擎审计工作流：两套 OCR 互证 → 视觉判定 → **22 道门禁** →
交付说明 + 两版正文 + 校对包 + 报告。引擎之后的每一步都是**确定性 CLI**，**从不调用模型**。

## 2. 三分钟上手

| 步骤 | 命令 / 动作 |
|---|---|
| ① 装环境 | `pwsh -ExecutionPolicy Bypass -File .\install.ps1`（**需 PowerShell 7+**；要本地 OCR 加 `-WithPaddle`；离线可装，自带 `_wheels/`） |
| ② 看成果 | 双击 `离线演示.cmd` |
| ③ 跑自己的书 | `docs\01-快速开始.md`（登记 → 引擎 → 画像 → 管线 → 判定 → 校验 → 发布） |
| ④ 只用云端引擎 | 双击 `输入密钥.cmd`（掩码小窗；值只进你自己的用户目录） |
| ⑤ 能不能跑 / 缺什么 | `python -X utf8 _mcp-test\preflight.py --book <key>` |
| ⑥ 跑引擎（**自动带环境**） | `& $py -X utf8 _mcp-test\engines.py run --slot witness --book <key>`（本地 Paddle；云端把 `--slot` 换成 `primary`） |

## 3. 日常五条命令

```powershell
$py = ".local\venvs\dual-mcp\Scripts\python.exe"
& $py -X utf8 _mcp-test\intake.py list                    # 有哪些书（含退休状态）
& $py -X utf8 _mcp-test\pipeline_run.py run --book <key>  # 跑管线（只写暂存区 + 自校验）
& $py -X utf8 _mcp-test\judge_worklist.py status --ledger s5-out\<key>   # 还差哪些判定
& $py -X utf8 _mcp-test\validate_run.py run --book <key>  # 22 道门禁（唯一判决者）
& $py -X utf8 _mcp-test\publish_run.py publish --book <key>               # 发布到 s7-out\<key>（归档旧版）
```

## 4. 三条铁律

1. **产出者不得自判**：门禁只由 `validate_run.py` 写；任何界面/脚本都不得绕过它。
2. **判定要留证**：每条判定必须带 `judge.model` 与 `judge.mode`；过期判定不算已判（宁缺勿猜，不确定写 `unclear`）。
3. **密钥不进产物**：不进配置/日志/产物/对话；填密钥用 `输入密钥.cmd`，自检用 `secret_scan.py check`（只报路径、不打印值）。

## 5. 必须停下来问用户的三类（**不得代答**）

| 类型 | 什么时候问 | 用户的答复落到哪 |
|---|---|---|
| **版式** | 登记完书、**任何书都要问**（单栏也要声明）；`主+边栏 / 三栏` 还要确认切分线 | `intake.py layout --book <key> --layout "单栏"` |
| **非印刷内容** | 看到**读者手写 / 背面透印 / 印章 / 贴纸 / 装饰**，要判断"算不算书的内容" | 判定词 `non_book_ink`（该块**移出净版**、登记 `errata.md`，正文一字不改）；**若那一块根本没成题**，用块级标记（见下） |
| **原书疑似印错且影响语义** | 登记 `errata(kind=source_typo)` **之前**先说给用户听 | `errata.md`（正文照原样，不改） |

### 5.1 手写/印章/装饰落在哪一块：**按"整块是什么"决定删不删**（不得一律删）

| 这一块的内容 | 做法 | 结果 |
|---|---|---|
| **整块都是**非书内容（整块手写、印章、贴纸；或一张图里**只有**手写圈注） | `judge_worklist.py flag-block --ledger s5-out\<key> --block-id <块> --basis "…"`（默认 `--mode quarantine`） | 该块**移出净版**，留 `<!-- NON-BOOK-INK: block=… 原文: … -->` |
| **混有印刷正文 / 印刷图**（正文里夹了红笔圈注；结构式图上有手写标记） | 同上，加 `--mode mark` | **正文/图保留**，只加 `<!-- NON-BOOK-INK-MARK: …（含印刷内容，未移除） -->` |
| 拿不准 | **不标** | 留给人看（宁缺勿猜） |

> 一句话：**"整块都是非书内容"才移出；"块里还有印刷内容"只标不删。**
> 块级标记写 `s5-out\<key>\block-flags.jsonl`，**不动队列/台账**；`errata.md` §2b 会登记。

**每次询问都要带一个默认选项**：「**本次任务后续同类问题都按这个处理，不再询问**」。

用户同意后记进注册表（`books.json.ask_defaults`），后续同类**不再问**：

```powershell
& $py -X utf8 _mcp-test\intake.py ask-default --book <key> --kind non_book_ink   # 或 --kind layout
```

判定/报告里写「**默认沿用（来源：books.json ask_defaults.<类型>）**」以便追溯；
改口用 `--reset`，然后重跑（**已落盘的判定不回溯**，要改就重判）。

## 6. 目录地图

| 路径 | 说明 |
|---|---|
| `docs\01-快速开始.md` | 从零跑通自己的第一本书 |
| `docs\02-使用说明.md` | 日常操作全表 + 产物清单 + 22 道门禁一览 |
| `docs\03-能力与边界.md` | 能做什么 / 明确不做 / 判决词表 / 隐私 / 出处 |
| `_mcp-test\` | 全部 CLI（`paths.py show` 看当前工具根与数据根） |
| `.local\` | **机器本地物**都在这里：`venvs\`（双环境）、`paddle-home\`（模型缓存）、`uv-cache\`、`tmp\`。不是产品内容，删掉由 `install.ps1` 重建 |
| `_wheels\` | 离线依赖（装环境时自动用） |
| `books.json` | **书注册表**（数据侧；新机器为空是正常状态） |

## 7. 出问题先看哪里

- 「能不能跑 / 缺什么 / 大概多少批」→ `preflight.py --book <key>`
- 「**我的环境和文档是不是一致**」→ `doctor.py check`（解释器 / 依赖 / 夹具 / 演示证据；只报漂移 + 补法，**不判**能不能跑）
- 「本地还是云端、要不要密钥」→ `engines.py matrix`
- 「用的是哪个根（工具 / 数据）」→ `paths.py show`
- 「这本书我不再维护了」→ `intake.py retire <条目> --why "…"`（可 `restore`；真删要 `remove --force`，会留台账）
