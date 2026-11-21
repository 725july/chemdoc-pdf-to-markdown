@echo off
rem ============================================================
rem  Enter API keys via a masked dialog (double-click).
rem  Values go to %USERPROFILE%\.chemdoc\secrets.json only
rem  (outside the workspace, ACL restricted to you).
rem  Values are never echoed, logged, or written into products.
rem
rem  NOTE: this file is intentionally ASCII-only (cmd.exe reads
rem  batch files in the system OEM codepage).
rem ============================================================
setlocal
set "HERE=%~dp0"
set "PYW=%HERE%.local\venvs\dual-mcp\Scripts\pythonw.exe"
set "PY=%HERE%.local\venvs\dual-mcp\Scripts\python.exe"
if not exist "%PY%" set "PY=%HERE%.venv-dual-mcp\Scripts\python.exe"
if not exist "%PYW%" set "PYW=%PY%"
set "SCRIPT=%HERE%_mcp-test\secrets.py"

if exist "%PYW%" (
  start "" "%PYW%" -X utf8 "%SCRIPT%" gui
  goto :eof
)
echo.
echo [ERROR] Python interpreter not found:
echo   %PYW%
echo Run install.ps1 first.
echo.
pause
