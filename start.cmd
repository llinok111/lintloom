@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] .venv not found, running install.cmd first ...
    call install.cmd
)
echo Starting LintLoom on http://127.0.0.1:8731/ ...
echo Press Ctrl+C in this window to stop.
echo.
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
