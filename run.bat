@echo off
chcp 65001 >nul 2>&1
echo BookAgent v0.1
echo.

if not exist envs\main\Scripts\python.exe (
    echo [错误] 未检测到主环境 (envs\main)。
    echo 请先运行 setup.bat 完成环境安装与部署！
    exit /b 1
)

envs\main\Scripts\python.exe app.py %*
