@echo off
REM FireGuard backend launcher (Windows).
REM Finds a local venv (.venv, venv, env) or PATH python and starts uvicorn.
setlocal

cd /d %~dp0

set "PYTHON_EXE="

REM Local virtual environment
for %%V in (.venv venv env) do (
    if exist "%%V\Scripts\python.exe" (
        set "PYTHON_EXE=%%V\Scripts\python.exe"
        goto :found
    )
)

REM Interpreter on PATH
where python >nul 2>&1 && set "PYTHON_EXE=python"

:found
if not defined PYTHON_EXE (
    echo [ERROR] No Python interpreter found.
    echo         Create a virtual environment first:
    echo             python -m venv .venv
    pause
    exit /b 1
)

echo [INFO] Starting FireGuard backend with: %PYTHON_EXE%
echo [INFO] API docs: http://localhost:8000/api/docs
echo [INFO] Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

pause