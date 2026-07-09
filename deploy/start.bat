@echo off
REM Start the Discord bot on Windows
cd /d "%~dp0.."

if not exist .venv\Scripts\python.exe (
    echo Virtualenv missing. Run:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

if not exist .env (
    echo Missing .env — copy .env.example to .env and set DISCORD_BOT_TOKEN.
    exit /b 1
)

if not exist logs mkdir logs
.venv\Scripts\python.exe bot.py
