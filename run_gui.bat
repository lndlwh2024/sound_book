@echo off
setlocal
cd /d "%~dp0"

if not exist "envs\main\Scripts\python.exe" (
    echo [ERROR] Python environment not found: envs\main\Scripts\python.exe
    pause
    exit /b 1
)

"envs\main\Scripts\python.exe" "app.py" %*
