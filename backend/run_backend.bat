@echo off
set VENV_PATH=E:\antigravity\venvs\fireguard_env
set PYTHON_EXE=%VENV_PATH%\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Virtual environment not found at %VENV_PATH%
    pause
    exit /b 1
)

echo [INFO] Starting FireGuard Backend using venv: %VENV_PATH%
cd /d %~dp0
"%PYTHON_EXE%" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
pause
