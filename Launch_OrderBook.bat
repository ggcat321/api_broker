@echo off
chcp 65001 >nul
title ⚡ Order Book Live

:: 切換到腳本所在目錄
cd /d "%~dp0"

echo.
echo   ╔══════════════════════════════════════════╗
echo   ║     ⚡ Order Book Live  啟動中...        ║
echo   ╚══════════════════════════════════════════╝
echo.

:: 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 找不到 Python，請先安裝 Python！
    echo    下載：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo ✓ %%i

:: ── 首次設定檢查 ──────────────────────────────
set NEED_SETUP=0

:: 檢查 API.env
if not exist "API.env" (
    set NEED_SETUP=1
    echo.
    echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    echo   📋 首次使用設定
    echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    echo.
    echo ❌ 找不到 API.env 設定檔
    echo.
    if exist "API.env.example" (
        copy "API.env.example" "API.env" >nul
        echo ✓ 已自動從 API.env.example 建立 API.env
        echo   請用記事本開啟 API.env，填入您的富邦帳號資訊
    ) else (
        echo   請在此資料夾中建立 API.env 檔案，內容如下：
        echo.
        echo     ID=你的身分證字號
        echo     PW=你的密碼
        echo     c_pw=你的憑證密碼
    )
    echo.
)

:: 檢查 .pfx 憑證
set PFX_FOUND=0
for %%f in (*.pfx) do set PFX_FOUND=1
if %PFX_FOUND%==0 (
    set NEED_SETUP=1
    echo ❌ 找不到 .pfx 憑證檔
    echo   請將您的富邦憑證檔（.pfx）放入此資料夾中
    echo.
)

:: 如果缺少設定，開啟資料夾並暫停
if %NEED_SETUP%==1 (
    explorer .
    echo 📂 已開啟資料夾，請完成上述設定後重新啟動此程式
    echo.
    pause
    exit /b 0
)

echo ✓ API.env 設定檔就緒
echo ✓ .pfx 憑證檔就緒

:: 安裝依賴
echo.
echo 📦 檢查套件依賴...
python -m pip install -r requirements.txt --quiet 2>nul
echo ✓ 套件就緒

echo.
echo 🚀 啟動伺服器中...
echo    網址: http://127.0.0.1:8000
echo.
echo ────────────────────────────────────────────
echo   瀏覽器將自動開啟，請勿關閉此視窗！
echo   要停止伺服器，按 Ctrl + C
echo ────────────────────────────────────────────
echo.

:: 1.5 秒後開啟瀏覽器
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8000"

:: 啟動伺服器
python server.py

echo.
echo 伺服器已停止。
echo.
pause
