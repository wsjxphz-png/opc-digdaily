@echo off
REM 本机定时运行脚本（替代 GitHub Actions）：每天 22:00 跑一次 daily-opportunity-bot
REM 走本机 SSRDOG 代理（127.0.0.1:9567），国际源才能抓到内容
set HTTP_PROXY=http://127.0.0.1:9567
set HTTPS_PROXY=http://127.0.0.1:9567
cd /d "C:\Users\Administrator\WorkBuddy\2026-08-07-20-50-04\daily-opportunity-bot"
call .venv\Scripts\python.exe main.py --once >> storage\bot_cron.log 2>&1
