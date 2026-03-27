#!/bin/bash
# ============================================
#   ⚡ Order Book Live — 一鍵啟動器
#   雙擊即可啟動，自動開啟瀏覽器
# ============================================

# 切換到腳本所在目錄（不管從哪裡雙擊都能正確找到檔案）
cd "$(dirname "$0")"

# 顏色定義
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color
BOLD='\033[1m'

clear
echo ""
echo -e "${CYAN}${BOLD}  ╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}  ║     ⚡ Order Book Live  啟動中...        ║${NC}"
echo -e "${CYAN}${BOLD}  ╚══════════════════════════════════════════╝${NC}"
echo ""

# 檢查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ 找不到 Python3，請先安裝 Python！${NC}"
    echo ""
    echo "按任意鍵關閉..."
    read -n 1
    exit 1
fi

PYTHON=$(command -v python3)
echo -e "${GREEN}✓${NC} Python: $($PYTHON --version)"

# ── 首次設定檢查 ──────────────────────────────
NEED_SETUP=false

# 檢查 API.env
if [ ! -f "API.env" ]; then
    NEED_SETUP=true
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  📋 首次使用設定${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${RED}❌ 找不到 API.env 設定檔${NC}"
    echo ""
    if [ -f "API.env.example" ]; then
        cp API.env.example API.env
        echo -e "${GREEN}✓${NC} 已自動從 API.env.example 建立 API.env"
        echo -e "  ${CYAN}請用文字編輯器開啟 API.env，填入您的富邦帳號資訊：${NC}"
    else
        echo -e "  ${CYAN}請在此資料夾中建立 API.env 檔案，內容如下：${NC}"
        echo ""
        echo "    ID=你的身分證字號"
        echo "    PW=你的密碼"
        echo "    c_pw=你的憑證密碼"
    fi
    echo ""
fi

# 檢查 .pfx 憑證
PFX_COUNT=$(find . -maxdepth 1 -name "*.pfx" | wc -l | tr -d ' ')
if [ "$PFX_COUNT" -eq "0" ]; then
    NEED_SETUP=true
    echo -e "${RED}❌ 找不到 .pfx 憑證檔${NC}"
    echo -e "  ${CYAN}請將您的富邦憑證檔（.pfx）放入此資料夾中${NC}"
    echo ""
fi

# 如果缺少設定，開啟資料夾並暫停
if [ "$NEED_SETUP" = true ]; then
    open .
    echo -e "${YELLOW}📂 已開啟資料夾，請完成上述設定後重新啟動此程式${NC}"
    echo ""
    echo "按任意鍵關閉..."
    read -n 1
    exit 0
fi

echo -e "${GREEN}✓${NC} API.env 設定檔就緒"
echo -e "${GREEN}✓${NC} .pfx 憑證檔就緒"

# 檢查並安裝依賴
echo -e "${YELLOW}📦 檢查套件依賴...${NC}"
$PYTHON -m pip install -r requirements.txt --quiet 2>/dev/null
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} 所有套件就緒"
else
    echo -e "${YELLOW}⚠️  部分套件安裝可能有問題，嘗試繼續啟動...${NC}"
fi

echo ""
echo -e "${CYAN}🚀 啟動伺服器中...${NC}"
echo -e "${CYAN}   網址: ${BOLD}http://127.0.0.1:8000${NC}"
echo ""
echo -e "${YELLOW}────────────────────────────────────────────${NC}"
echo -e "${YELLOW}  瀏覽器將自動開啟，請勿關閉此終端視窗！${NC}"
echo -e "${YELLOW}  要停止伺服器，按 Control + C${NC}"
echo -e "${YELLOW}────────────────────────────────────────────${NC}"
echo ""

# 1.5 秒後自動開啟瀏覽器
(sleep 1.5 && open "http://127.0.0.1:8000") &

# 直接用 python 執行 server.py（前景執行，Ctrl+C 可停）
$PYTHON server.py

# server 結束後
echo ""
echo -e "${RED}伺服器已停止。${NC}"
echo ""
echo "按任意鍵關閉..."
read -n 1
