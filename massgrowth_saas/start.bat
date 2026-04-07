@echo off
title MassGrowth SaaS

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found. Download from python.org/downloads
    echo Check "Add Python to PATH" during install!
    pause
    exit /b 1
)

if exist ".venv" rmdir /s /q ".venv"

python -m venv .venv
call .venv\Scripts\activate.bat

pip install -r requirements.txt

echo.
echo Server started: http://localhost:8000
echo.

start "" "http://localhost:8000"
python main.py

pause
