# ⚡ Order Book Live — 即時五檔報價觀測

即時訂閱台股 / 期貨的買賣五檔掛單與成交價，透過瀏覽器即可操作。

![Preview](https://img.shields.io/badge/Status-Live-brightgreen)

---

## 📦 資料夾內容

| 檔案 | 說明 |
|---|---|
| `Launch_OrderBook.command` | macOS 啟動器（雙擊即可） |
| `Launch_OrderBook.bat` | Windows 啟動器（雙擊即可） |
| `server.py` | 後端伺服器 |
| `static/index.html` | 前端介面 |
| `API.env.example` | 設定檔範本 |
| `requirements.txt` | Python 套件依賴 |

---

## 🚀 首次使用（三步驟）

### Step 1：放入憑證

將您的**富邦數位憑證檔（`.pfx`）** 放入此資料夾中。

> 程式會自動偵測資料夾中的 `.pfx` 檔案，**不需要改檔名**。

### Step 2：設定帳號

1. 將 `API.env.example` **複製一份**，重新命名為 **`API.env`**
2. 用文字編輯器打開 `API.env`，填入您的資訊：

```env
ID=你的身分證字號
PW=你的富邦密碼
c_pw=你的憑證密碼
```

> ⚠️ `API.env` 包含機敏資訊，**請勿分享給他人或上傳至 GitHub**。

### Step 3：啟動

| 系統 | 操作 |
|---|---|
| **macOS** | 雙擊 `Launch_OrderBook.command` |
| **Windows** | 雙擊 `Launch_OrderBook.bat` |

瀏覽器會自動開啟 `http://127.0.0.1:8000`，輸入商品代號（例如 `2330`、`TXFD6`）即可開始觀測。

---

## 💡 使用方式

- **現股**：輸入股票代號，例如 `2330`、`2317`、`0050`
- **期貨**：輸入期貨代號，例如 `TXFD6`（台指期近月）
- 按 `訂閱` 或 Enter 開始接收即時報價
- 要停止伺服器，在終端視窗按 `Ctrl + C`

---

## ⚙️ 環境需求

- **Python 3.8+**（已安裝）
- **pip** 套件管理器
- 首次啟動會自動安裝所需套件

---

## ❓ 常見問題

**Q: 為什麼打開後顯示「Symbol Not Found」？**  
A: 確認商品代號正確，且在開盤時間內（股票 09:00-13:30，期貨含夜盤）。

**Q: 為什麼價格不會跳動？**  
A: 非開盤時間不會有新報價推送，請在交易時段測試。

**Q: 可以多人同時使用嗎？**  
A: 可以，多個瀏覽器分頁都能連到同一個伺服器。
