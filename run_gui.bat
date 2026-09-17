@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [书声 ShuSheng] 正在启动桌面图形客户端...
"%~dp0envs\main\Scripts\python.exe" "%~dp0app.py"
pause
