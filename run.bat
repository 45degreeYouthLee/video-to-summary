@echo off
chcp 65001 >nul
:: Windows 一键运行脚本
:: 用法: 将 B站 URL 复制到剪贴板，双击 run.bat 即可

cd /d "%~dp0"

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+ 并加入 PATH
    pause
    exit /b 1
)

:: 检查依赖
if not exist "venv\Scripts\python.exe" (
    echo [首次运行] 创建虚拟环境并安装依赖...
    python -m venv venv
    call venv\Scripts\pip install -r requirements.txt
)

:: 尝试读取剪贴板 URL（需安装 pyperclip，已在 requirements.txt）
for /f "delims=" %%i in ('venv\Scripts\python -c "import pyperclip; print(pyperclip.paste().strip())" 2^>nul') do set CLIP_URL=%%i

if not "%CLIP_URL%"=="" (
    echo [剪贴板] 检测到内容: %CLIP_URL%
    echo 是否使用该 URL? (Y/N)
    choice /c YN /n /m ""
    if %errorlevel%==1 (
        venv\Scripts\python main.py "%CLIP_URL%"
        pause
        exit /b 0
    )
)

:: 手动输入
set /p VIDEO_URL="请输入 B站视频链接: "
venv\Scripts\python main.py "%VIDEO_URL%"
pause
