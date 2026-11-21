# install.ps1 —— 一键把运行环境装好（B2；契约见 SPEC §7.10）
#
# 用法（在仓库根跑）:
#   pwsh -ExecutionPolicy Bypass -File install.ps1
#   pwsh -ExecutionPolicy Bypass -File install.ps1 -WithPaddle          # 另装本地见证引擎（约 2 GB + 需 GPU）
#   pwsh -ExecutionPolicy Bypass -File install.ps1 -DataRoot D:\chemdoc-data
#   pwsh -ExecutionPolicy Bypass -File install.ps1 -Uv                  # 用 uv（更快；本机没有 3.12 时也靠它取一个）
#
# 做什么：定位 Python(**>=3.12**) → 建 venv → 装 requirements.txt →（可选）装 Paddle 栈 → 自检
#         （paths / preflight --selftest / validate_run selftest）
# 不做：**不碰密钥**（只提示怎么填）、**不写工作区之外的东西**（venv/缓存都建在仓库里）、
#       失败即**非零退出**并给可操作的补法（不静默"看起来成功"）。
#
# 为什么要求 >=3.12：实测的依赖钉版里 `numpy==2.5.3` **自带 `Requires-Python >=3.12`**；
# 本机系统 Python 只有 3.10.9（实测：pip/uv 都会拒绝，装不上）。而"哪套版本是验证过的"
# 不能拍脑袋改（numpy 影响墨迹统计 → 门禁数字），所以要求解释器升到 3.12，而不是降依赖版本。
#
# 为什么要求 PowerShell 7+：`[CmdletBinding()]` + `$PSScriptRoot` 作参数默认值在 Windows 自带的
# PowerShell 5.1 上求值为空 → 脚本首条即崩（2026-10-14 实测）。**不为 5.1 适配**，用 `#Requires` 明确报错。
#Requires -Version 7.0
[CmdletBinding()]
param(
  [string]$ToolsRoot = $PSScriptRoot,
  [string]$DataRoot = "",
  [string]$VenvName = "dual-mcp",
  [string]$PaddleVenvName = "paddle",
  [string]$PythonVersion = "3.12",
  [switch]$WithPaddle,
  [switch]$Uv,
  [switch]$SkipVerify,
  [switch]$SkipSample
)

$ErrorActionPreference = 'Stop'
function Say($m) { Write-Host $m }
function Die($m) { Write-Host "`n[失败] $m" -ForegroundColor Red; exit 1 }

Say "=== chemdoc 环境安装（B2） ==="
Say "工具根：$ToolsRoot"
if (-not (Test-Path -LiteralPath (Join-Path $ToolsRoot '_mcp-test\paths.py'))) {
  Die "这里看起来不是仓库根（找不到 _mcp-test\paths.py）：$ToolsRoot`n      → 用 -ToolsRoot 指定仓库根"
}
$req = Join-Path $ToolsRoot 'requirements.txt'
if (-not (Test-Path -LiteralPath $req)) { Die "找不到依赖清单：$req" }

# **机器本地物**统一进 `<工具根>/.local/`（venv / 模型缓存 / uv 缓存 / 临时目录）：
#   · 产品树顶层只多这一项，排除/保护清单也只需一条
#   · uv 默认缓存/解释器在 %LOCALAPPDATA%（受限环境会被拒，实测 os error 5）→ 指进本地根
#   · 想放到别的盘（比如 SSD）：设环境变量 CHEMDOC_LOCAL（paths.py 认它）
$LocalRoot = if ($env:CHEMDOC_LOCAL) { $env:CHEMDOC_LOCAL } else { Join-Path $ToolsRoot '.local' }
$VenvsDir  = Join-Path $LocalRoot 'venvs'
$TmpDir    = Join-Path $LocalRoot 'tmp'
New-Item -ItemType Directory -Force -Path $VenvsDir, $TmpDir | Out-Null
$LocalNote = Join-Path $LocalRoot 'README.txt'
if (-not (Test-Path -LiteralPath $LocalNote)) {
  @(
    '机器本地物都在这个目录里（不是产品内容，删掉会由 install.ps1 重建）：',
    '  venvs\dual-mcp   主环境（确定性 CLI 用）',
    '  venvs\paddle      可选：本地见证引擎（PaddleOCR-VL）',
    '  venvs\mineru      可选：本地 MinerU（未装）',
    '  paddle-home\      模型缓存（约 1.93 GB）',
    '  uv-cache\         uv 的包缓存与托管解释器',
    '  tmp\              临时目录（跑自检/回归时用）',
    '',
    '想搬到别的盘：设 CHEMDOC_LOCAL=<目标目录> 再跑 install.ps1。'
  ) | Set-Content -LiteralPath $LocalNote -Encoding UTF8
}
Say "本地物目录：$LocalRoot"
$uvExe = (Get-Command uv -ErrorAction SilentlyContinue).Source
if ($uvExe) {
  $env:UV_CACHE_DIR = Join-Path $LocalRoot 'uv-cache'
  $env:UV_PYTHON_INSTALL_DIR = Join-Path $LocalRoot 'uv-cache\python'
}

# ---- 1) 找 Python（>=PythonVersion；找不到就让 uv 取一个）----------------------------------------
$minMajor, $minMinor = 3, [int]($PythonVersion.Split('.')[1])
function Test-PyExe([string]$exe, [string[]]$pre) {
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $null }
  $probe = @($pre) + @('-c', 'import sys;print(sys.executable);print(sys.version_info[0],sys.version_info[1])')
  $out = & $exe @probe 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
  $lines = @($out | Where-Object { $_ -and $_.ToString().Trim() -ne '' })
  if ($lines.Count -lt 2) { return $null }
  $v = $lines[1].ToString().Trim().Split(' ')
  $maj, $mino = [int]$v[0], [int]$v[1]
  if ($maj -lt $minMajor -or ($maj -eq $minMajor -and $mino -lt $minMinor)) { return $null }
  return @{ exe = $exe; args = @($pre); py = $lines[0].ToString().Trim(); ver = "$maj.$mino" }
}
$py = $null
foreach ($cand in @(@{ e = 'py'; a = @('-3.13') }, @{ e = 'py'; a = @('-3.12') },
                    @{ e = 'py'; a = @('-3') }, @{ e = 'python'; a = @() }, @{ e = 'python3'; a = @() })) {
  if ($py) { break }
  $py = Test-PyExe $cand.e $cand.a
}
$useUvVenv = $false
if (-not $py) {
  if ($uvExe) {
    Say "本机没有 >=$PythonVersion 的 Python → 用 uv 取一个（缓存在仓库内，不污染系统）"
    $useUvVenv = $true
  } else {
    Die "找不到 Python >=$PythonVersion。两种补法：`n      ① winget install Python.Python.3.12（或 python.org 装 3.12+）`n      ② 装 uv 后重跑并加 -Uv（uv 会自动取一个 3.12）"
  }
} else {
  Say "Python：$($py.py)（$($py.ver)）"
}

# ---- 2) 建 venv（幂等；已存在的若解释器版本不符 → **挪到一边**再建，不删除）------------------------
function Resolve-Venv([string]$Name) {
  if ($Name -match '[\\/]') { return (Join-Path $ToolsRoot $Name) }   # 显式路径（兼容老用法）
  return (Join-Path $VenvsDir $Name)                                   # 叶子名 → <本地根>/venvs/<名>
}
$venv = Resolve-Venv $VenvName
$venvPy = Join-Path $venv 'Scripts\python.exe'
function Get-VenvVer([string]$Target) {
  $e = Join-Path $Target 'Scripts\python.exe'
  if (-not (Test-Path -LiteralPath $e)) { return "" }
  $out = & $e -c 'import sys;print(str(sys.version_info[0])+"."+str(sys.version_info[1]))' 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $out) { return "" }
  return $out.ToString().Trim()
}
function Test-VerOk([string]$ver) {
  if (-not $ver) { return $false }
  $p = $ver.Split('.'); if ($p.Count -lt 2) { return $false }
  return ([int]$p[0] -gt $minMajor) -or ([int]$p[0] -eq $minMajor -and [int]$p[1] -ge $minMinor)
}
function New-Venv([string]$Target) {
  if (Test-Path -LiteralPath (Join-Path $Target 'Scripts\python.exe')) {
    $ver = Get-VenvVer $Target
    if (Test-VerOk $ver) { Say "venv 已存在：$Target（Python $ver，幂等，不重建）"; return }
    $aside = "$Target.bak-$(Get-Date -Format yyyyMMddHHmmss)"
    Say "已存在的 venv 是 Python $ver（<$PythonVersion，装不了钉版依赖）→ **挪到一边**（不删除）：$aside"
    Move-Item -LiteralPath $Target -Destination $aside
  }
  Say "建 venv：$Target（Python $PythonVersion）"
  if ($useUvVenv -or ($Uv -and $uvExe)) {
    & uv venv --python $PythonVersion $Target
  } else {
    & $py.exe @($py.args) -m venv $Target
  }
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $Target 'Scripts\python.exe'))) {
    Die "建 venv 失败：$Target`n      → 若 python 是 Microsoft Store 版，请改用 python.org 版；或加 -Uv 用 uv 建"
  }
}
New-Venv $venv

# ---- 3) 装依赖（默认 pip；-Uv 且有 uv 时用 uv 加速——**uv 失败自动退回 pip**）----------------------
function Install-Req([string]$ReqFile, [string]$TargetPy) {
  # 有本地 wheel 房就优先用它（离线也能装；没网/网慢都不怕）
  $whl = Join-Path $ToolsRoot "_wheels"
  $FL = @()
  if (Test-Path $whl) {
    $n = (Get-ChildItem $whl -Filter *.whl -ErrorAction SilentlyContinue | Measure-Object).Count
    if ($n -gt 0) { $FL = @("--find-links", $whl); Say "本地 wheel 房：$whl（$n 个 wheel）→ 优先从它装" }
  }
  $tryUv = [bool]($Uv -and $uvExe)
  if ($tryUv) {
    Say "uv pip install -r $([IO.Path]::GetFileName($ReqFile))  (UV_CACHE_DIR=$env:UV_CACHE_DIR)"
    & uv pip install --python $TargetPy @FL -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
      Say "  ↳ uv 没成功（缓存/网络/权限/版本）→ **退回 pip**（不影响结果，只是慢一点）"
      $tryUv = $false
    }
  }
  if (-not $tryUv) {
    & $TargetPy -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) { Say "venv 里没有 pip → ensurepip"; & $TargetPy -m ensurepip --upgrade | Out-Null }
    Say "pip install -r $([IO.Path]::GetFileName($ReqFile))"
    & $TargetPy -m pip install --disable-pip-version-check -q @FL -r $ReqFile
  }
  if ($LASTEXITCODE -ne 0) {
    Die "装依赖失败：$ReqFile`n      → 检查网络/代理；或先手动：$TargetPy -m pip install -r $ReqFile"
  }
}
Install-Req $req $venvPy

if ($WithPaddle) {
  $paddleReq = Join-Path $ToolsRoot 'requirements-optional-paddle.txt'
  if (-not (Test-Path -LiteralPath $paddleReq)) { Die "找不到可选清单：$paddleReq" }
  $pv = Resolve-Venv $PaddleVenvName
  New-Venv $pv
  Install-Req $paddleReq (Join-Path $pv 'Scripts\python.exe')
  Say "提示：Paddle 首次运行会自动下模型（约 1.93 GB）到 $LocalRoot\paddle-home\.paddlex\official_models"
}

# ---- 4) 自检（数据无关的四项；失败即非零退出）---------------------------------------------------
if (-not $SkipVerify) {
  Say "`n=== 自检 ==="
  if ($DataRoot) {
    $env:CHEMDOC_ROOT = $DataRoot
    Say "本次自检的数据根（仅本进程）：$DataRoot"
  }
  # 自检会建临时目录：系统 %TEMP% 在受限环境里可能拒写（实测撞到）→ 指到本地根下
  $env:TEMP = $TmpDir
  $env:TMP = $TmpDir
  $env:PYTHONUTF8 = "1"
  $checks = @(
    @{ name = '工作根'; script = '_mcp-test\paths.py'; args = @('show') },
    @{ name = '工作根自测'; script = '_mcp-test\paths.py'; args = @('selftest') },
    @{ name = '预检自测'; script = '_mcp-test\preflight.py'; args = @('--selftest') },
    @{ name = '安装/根验收'; script = '_mcp-test\b2_install_check.py'; args = @('selftest', '--quick') },
    @{ name = '校验器自测'; script = '_mcp-test\validate_run.py'; args = @('selftest') },
    @{ name = '引擎槽位（含自动 env）'; script = '_mcp-test\engines.py'; args = @('check') }
  )
  foreach ($c in $checks) {
    $p = Join-Path $ToolsRoot $c.script
    $tail = & $venvPy -X utf8 $p @($c.args) 2>&1 | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { Die "$($c.name) 没通过：$tail" }
    Say ("  ✓ {0}：{1}" -f $c.name, $tail)
  }
}

# ---- 4.5) 自制样书（装完就完整：C1/C2 回归与离线 demo 的唯一载体，SPEC §3.14）-------------
if (-not $SkipSample) {
  Say "`n=== 造自制样书（9 页，全部自造内容）+ 跑一遍管线（只写暂存区）==="
  $fx = Join-Path $ToolsRoot "_mcp-test\fx_sample.py"
  & $venvPy -X utf8 $fx make --root $ToolsRoot 2>&1 | Select-Object -Last 10 | ForEach-Object { Say "  $_" }
  if ($LASTEXITCODE -ne 0) { Say "[警告] 样书生成返回 $LASTEXITCODE（可手动：& '$venvPy' -X utf8 '$fx' make --force）" }
  else {
    Say "样书就绪（C1 跨章 / C2 跨卷回归现在可以真跑）"
    # 2026-10-26：**所有书都要声明版式**——样书 9 页全是单栏，装完直接替用户声明，
    # 否则 preflight 会对它报「版式未声明」（demo 也会带着这条警告跑）。
    & $venvPy -X utf8 (Join-Path $ToolsRoot "_mcp-test\intake.py") layout --book fx-sample --layout "单栏" --root $ToolsRoot 2>&1 | Out-Null
  }
} else { Say "（-SkipSample：跳过样书生成 → C1/C2 回归会因缺数据而明示跳过）" }

# ---- 5) 下一步 ----------------------------------------------------------------------------------
Say "`n=== 装好了 ==="
Say "解释器：$venvPy"
Say "跑一次预检（会报'能不能跑/缺什么/规模投影/隐私行'）："
Say "  & '$venvPy' -X utf8 `"$ToolsRoot\_mcp-test\preflight.py`" --book <书key>"
Say "密钥：**双击仓库根的 输入密钥.cmd**（掩码小窗；值只写进 %USERPROFILE%\.chemdoc\secrets.json，agent 不读）"
if ($DataRoot) {
  Say "数据根（本次只对自检生效；要长期生效请自己设用户环境变量）："
  Say "  setx CHEMDOC_ROOT `"$DataRoot`""
}
Say "全量自测（改完代码跑这个；期望结尾'失败套件 = 无'）："
Say "  & '$venvPy' -X utf8 `"$ToolsRoot\_mcp-test\c3_full_regression.py`""
Say "书注册表是**数据**（权威位置 = 数据根下的 books.json；本机现在没有就说明还没登记过书）："
Say "  看现有书：& '$venvPy' -X utf8 `"$ToolsRoot\_mcp-test\intake.py`" list"
Say "  登记第一本：& '$venvPy' -X utf8 `"$ToolsRoot\_mcp-test\intake.py`" add --pdf <切片PDF> --ranges 1-50 --key <目录键> --label <书名>"
Say "  （格式参考 $ToolsRoot\_mcp-test\books.sample.json；做完的书可 retire / restore / remove --force）"
exit 0
