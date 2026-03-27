@echo off
title Order Book Live
cd /d "%~dp0"

echo.
echo   ========================================
echo      Order Book Live  -  Starting...
echo   ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Please install Python first.
    echo         Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo [OK] %%i

:: ---- First-time Setup Check ----
set NEED_SETUP=0

:: Check API.env
if not exist "API.env" (
    set NEED_SETUP=1
    echo.
    echo ============================================
    echo   First-time Setup
    echo ============================================
    echo.
    echo [ERROR] API.env not found!
    echo.
    if exist "API.env.example" (
        copy "API.env.example" "API.env" >nul
        echo [OK] Created API.env from API.env.example
        echo.
        echo   Please open API.env with Notepad and fill in your Fubon credentials:
        echo     ID   = Your ID number
        echo     PW   = Your password
        echo     c_pw = Your certificate password
    ) else (
        echo   Please create API.env in this folder with the following content:
        echo.
        echo     ID=your_id
        echo     PW=your_password
        echo     c_pw=your_cert_password
    )
    echo.
)

:: Check .pfx certificate
set PFX_FOUND=0
for %%f in (*.pfx) do set PFX_FOUND=1
if %PFX_FOUND%==0 (
    set NEED_SETUP=1
    echo [ERROR] No .pfx certificate file found!
    echo   Please place your Fubon certificate (.pfx) in this folder.
    echo.
)

:: If setup needed, open folder and pause
if %NEED_SETUP%==1 (
    explorer .
    echo [INFO] Folder opened. Please complete the setup above, then run this again.
    echo.
    pause
    exit /b 0
)

echo [OK] API.env ready
echo [OK] Certificate (.pfx) ready

:: Install dependencies
echo.
echo [INFO] Checking dependencies...
python -m pip install -r requirements.txt --quiet 2>nul
echo [OK] All packages ready

echo.
echo ============================================
echo   Server starting...
echo   URL: http://127.0.0.1:8000
echo.
echo   Browser will open automatically.
echo   DO NOT close this window!
echo   Press Ctrl+C to stop the server.
echo ============================================
echo.

:: Open browser after 2 seconds
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8000"

:: Start server
python server.py

echo.
echo Server stopped.
echo.
pause
