@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "LINTLOOM_QUIET=1"
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] .venv not found, running install.cmd first ...
    call install.cmd
)
echo Running 100 tests (some of them execute the real sqlfluff CLI) ...
echo.
".venv\Scripts\python.exe" -m unittest discover -s tests -v
echo.
echo Exit code: %errorlevel%
pause
