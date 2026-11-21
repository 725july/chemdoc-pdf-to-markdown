@echo off
rem ============================================================
rem  Offline demo: run the 9-page built-in sample book.
rem  Shows the full 22 gates + delivery note.
rem  No API key, no OCR install, no GPU. Double-click this file.
rem
rem  NOTE: this file is intentionally ASCII-only. cmd.exe reads
rem  batch files in the system OEM codepage, so UTF-8 Chinese
rem  here would be garbled (see docs/02 for details).
rem ============================================================
setlocal
chcp 65001 >nul
set "HERE=%~dp0"
set "PY=%HERE%.local\venvs\dual-mcp\Scripts\python.exe"
if not exist "%PY%" set "PY=%HERE%.venv-dual-mcp\Scripts\python.exe"
if not exist "%PY%" set "PY=%HERE%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Python venv not found. Run install.ps1 first:
  echo   pwsh -ExecutionPolicy Bypass -File "%HERE%install.ps1"
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
"%PY%" -X utf8 "%HERE%_mcp-test\demo_offline.py" run
echo.
echo [demo finished] Press any key to close.
pause >nul
endlocal
