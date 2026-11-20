@echo off
REM ---------------------------------------------------------------------------
REM FireGuard Backend launcher (Windows)
REM
REM Locates a Python interpreter and starts the FastAPI server.
REM Resolution order:
REM   1. A virtual environment inside this directory (.venv, venv, env)
REM   2. The interpreter on PATH
REM ---------------------------------------------------------------------------
setlocal

cd /d %~dp0

set "PYTHON_EXE="

REM --- 1. Local virtual environment -----------------------------------------
for %%V in (.venv venv env) do (
    if exist "%%V\Scripts\python.exe" (
        set "PYTHON_EXE=%%V\Scripts\python.exe"
        goto :found
    )
)

REM --- 2. Interpreter on PATH -----------------------------------------------
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