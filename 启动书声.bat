@echo off
chcp 65001 >nul 2>&1
title 书声 (ShuSheng) v2.0
cd /d "%~dp0"

echo ====================================================================
echo   书声 (ShuSheng) v2.0 - 自动化有声视频与音频生产系统
echo ====================================================================
echo.
echo 正在启动桌面图形客户端，请稍候...
echo.

if not exist "%~dp0envs\main\Scripts\python.exe" (
    echo [错误] 未在当前目录下找到 Python 虚拟运行环境:
    echo        %~dp0envs\main\Scripts\python.exe
    echo.
    echo 请确认程序完整性，或先运行 setup.bat 完成部署！
    echo.
    pause
    exit /b 1
)

"%~dp0envs\main\Scripts\python.exe" "%~dp0app.py"

if %errorlevel% neq 0 (
    echo.
    echo [提示] 应用程序已退出 (退出码: %errorlevel%)。
    pause
)
