@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo BookAgent v0.1 安装与部署向导
echo ========================================

echo [1/10] 检查 Python 运行环境...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [错误] 未检测到 Python，请确保已安装 Python 并添加到系统 PATH 中。
    exit /b 1
)

echo [2/10] 检查 Python 版本 (>= 3.10)...
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if %ERRORLEVEL% NEQ 0 (
    echo [错误] Python 版本需要大于等于 3.10。请升级您的 Python 版本。
    exit /b 1
)

echo [3/10] 检查 FFmpeg 运行环境...
ffmpeg -version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [错误] 未检测到 FFmpeg。请访问 https://ffmpeg.org/ 下载并配置系统 PATH。
    exit /b 1
)

echo [4/10] 创建主环境 (envs\main)...
if not exist envs\main (
    python -m venv envs\main
)

echo [5/10] 安装主环境依赖...
envs\main\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
envs\main\Scripts\pip.exe install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo [错误] 主环境依赖安装失败，请检查网络或 pip 配置！
    exit /b 1
)

set KOKORO_FAILED=0
echo [6/10] 创建 Kokoro 独立环境 (envs\kokoro)...
if not exist envs\kokoro (
    python -m venv envs\kokoro
)
echo [7/10] 安装 Kokoro 环境依赖...
envs\kokoro\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
envs\kokoro\Scripts\pip.exe install -r requirements-kokoro.txt
if %ERRORLEVEL% NEQ 0 (
    echo KOKORO_SETUP_FAILED
    set KOKORO_FAILED=1
)

set F5_FAILED=0
echo [8/10] 创建 F5-TTS 独立环境 (envs\f5)...
if not exist envs\f5 (
    python -m venv envs\f5
)
echo [9/10] 安装 F5-TTS 环境依赖...
envs\f5\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
envs\f5\Scripts\pip.exe install -r requirements-f5.txt
if %ERRORLEVEL% NEQ 0 (
    echo F5_SETUP_FAILED
    set F5_FAILED=1
)

echo [10/10] 创建必要目录与配置...
if not exist books mkdir books
if not exist logs mkdir logs
if not exist tests\fixtures mkdir tests\fixtures

if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
    ) else (
        echo. > .env
    )
    echo [提示] 自动创建 .env 文件，请补充必要的 API Keys。
)

echo.
echo 执行 Smoke Test (主环境快速校验)...
envs\main\Scripts\python.exe -c "import yaml, ebooklib; print('主环境基础包加载正常')"
if %ERRORLEVEL% NEQ 0 (
    echo [错误] 主环境 Smoke Test 失败！
    exit /b 1
)

echo ========================================
echo 安装结果摘要：
if %KOKORO_FAILED% NEQ 0 (
    echo - [警告] Kokoro 独立环境安装失败，部分 TTS 功能可能受限。
)
if %F5_FAILED% NEQ 0 (
    echo - [警告] F5-TTS 独立环境安装失败，部分 TTS 功能可能受限。
)

if %KOKORO_FAILED% EQU 0 if %F5_FAILED% EQU 0 (
    echo Setup completed successfully
)
echo ========================================
