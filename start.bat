@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo =============================================
echo   一人公司赚钱机会挖掘 每日推送系统
echo =============================================
echo.

if "%1"=="daemon" (
    echo [模式] 定时服务 (每天 9:00 自动执行^)
    echo.
    call .venv\Scripts\python.exe main.py --daemon
) else if "%1"=="once" (
    echo [模式] 立即执行一次
    echo.
    call .venv\Scripts\python.exe main.py --once
) else (
    echo 用法:
    echo   start.bat once         立即执行一次
    echo   start.bat daemon       启动定时服务
    echo.
)
