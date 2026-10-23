@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo ==========================================
echo   LintLoom installer
echo ==========================================
echo.

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3.9+ not found in PATH.
    echo         Install from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [1/3] interpreter: %PY%

if not exist ".venv\Scripts\python.exe" (
    echo [2/3] creating .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto fail
) else (
    echo [2/3] .venv already exists, skip
)

echo [3/3] installing requirements.txt ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if errorlevel 1 goto fail

echo.
echo Running self test ...
".venv\Scripts\python.exe" run.py --selftest >nul 2>&1
if errorlevel 1 (
    echo [WARN] self test failed. Run manually:
    echo        .venv\Scripts\python.exe run.py --selftest
) else (
    echo [OK] environment ready.
)
echo.
echo Next step: double click start.cmd
pause
exit /b 0

:fail
echo.
echo [ERROR] installation failed, see messages above.
pause
exit /b 1
