@echo off
REM 本机定时运行脚本：每天 20:00 跑一次 daily-opportunity-bot（本仓库）
REM 走本机 SSRDOG 代理（127.0.0.1:9567），国际源才能抓到内容
REM 2026-08-14 修复：%~dp0 指向本仓库（此前硬编码 WorkBuddy 副本，双副本双跑+状态分裂）
REM run_bot.py 会在推送失败时往飞书群发告警——不会再「静默失败」
set HTTP_PROXY=http://127.0.0.1:9567
set HTTPS_PROXY=http://127.0.0.1:9567
cd /d "%~dp0"
REM 首次运行/依赖更新时补齐依赖（已装则秒过）
python -m pip install -q -r requirements.txt
python run_bot.py >> storage\bot_cron.log 2>&1
