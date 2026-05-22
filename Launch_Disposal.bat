@echo off
title Disposal Stock Monitor
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python first.
    pause
    exit /b 1
)

python -m pip install -r requirements_disposal.txt --quiet
python disposal_app.py
pause
