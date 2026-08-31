import os
import re
import time
import json
import asyncio
import threading
import requests
import urllib3
from contextlib import asynccontextmanager
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 批次抓報價時的並發上限。太高會被富邦 API 限流，漏掉的個股會讓 iNAV 少算。
SDK_QUOTE_CONCURRENCY = int(os.getenv("SDK_QUOTE_CONCURRENCY", "8"))

# 同時掛在券商 WS 上的商品數上限。前端送來的順序即優先順序，
# 超過的從尾巴砍掉並回報給前端。富邦實測上限約 200，若主控台出現
# 「[SDK WARN] 訂閱被拒」代表還是太高，用環境變數往下調。
MAX_WS_SYMBOLS = int(os.getenv("MAX_WS_SYMBOLS", "200"))

# 主動型 ETF 的 PCF 快取秒數，見 get_etf_pcf() 的說明
PCF_TTL_TODAY = int(os.getenv("PCF_TTL_TODAY", "1800"))     # 已拿到今日公告版
PCF_TTL_WAITING = int(os.getenv("PCF_TTL_WAITING", "180"))  # 還在等今日公告
PCF_TTL_FAILED = int(os.getenv("PCF_TTL_FAILED", "60"))     # 抓取失敗、暫時吃備份

# 報價優先順序：越前面越優先拿到 API 額度。
# 0050 排第一（它同時是 00631L iNAV 的代理標的），接著主動型，再來 00631L。
QUOTE_PRIORITY_TICKERS = ["0050", "00981A", "00403A", "00631L"]
os.chdir(BASE_DIR)

# Load env variables
env_path = os.path.join(BASE_DIR, "API.env")
load_dotenv(dotenv_path=env_path)
# 自動偵測資料夾中的 .pfx 憑證檔（使用者只需放入自己的 .pfx 即可）
pfx_files = [f for f in os.listdir(BASE_DIR) if f.endswith('.pfx')]
if pfx_files:
    pfx_path = os.path.join(BASE_DIR, pfx_files[0])
    print(f"偵測到憑證檔: {pfx_files[0]}")
else:
    pfx_path = None
    print("⚠️ 未偵測到 .pfx 憑證檔，請將憑證放入此資料夾！")


ID = os.getenv("ID")
PW = os.getenv("PW")
CERT_PW = os.getenv("c_pw")

# ─── Lifespan (replaces deprecated on_event) ───────────────────────
# ─── Momentum Snapshot Task ──────────────
async def momentum_snapshot_task():
    global CURRENT_BUCKET
    print("Started Momentum Snapshot task (10s intervals)")
    while True:
        await asyncio.sleep(10)
        # Snapshot the current bucket
        MOMENTUM_BUCKETS.append(dict(CURRENT_BUCKET))
        # Reset current bucket
        CURRENT_BUCKET = defaultdict(lambda: {'price': None, 'vol': 0, 'large_vol': 0})


@asynccontextmanager
async def lifespan(app):
    global sdk, loop
    loop = asyncio.get_running_loop()
    
    print("Initializing Fubon SDK Connection...")
    sdk = FubonSDK(300, 3) 
    try:
        accounts = sdk.login(ID, PW, pfx_path, CERT_PW)
        print("Login Success:", accounts)
        sdk.init_realtime()
        stock = sdk.marketdata.websocket_client.stock
        stock.on("message", handle_fubon_message)
        stock.connect()
        
        # Initialize Futures & Options client as well
        futopt = sdk.marketdata.websocket_client.futopt
        futopt.on("message", handle_fubon_message)
        futopt.connect()
        
        print("Connected to Fubon Market Data (Stock & FutOpt) Websockets")
    except Exception as e:
        print(f"Failed to login or connect to python sdk: {e}")
        
    # Start background tasks
    asyncio.create_task(message_processor())
    asyncio.create_task(vix_scraper())
    asyncio.create_task(fubon_sdk_watchdog())
    asyncio.create_task(momentum_snapshot_task())
    asyncio.create_task(active_pcf_refresher())

    yield  # ← Server is running
    
    # Shutdown
    if sdk:
        try:
            sdk.marketdata.websocket_client.stock.disconnect()
            sdk.marketdata.websocket_client.futopt.disconnect()
        except Exception:
            pass
    print("Server shutdown complete.")

app = FastAPI(lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware

# Enable CORS for local standalone dashboard requests (including file:// null origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        # 記下每個商品「實際送出去的訂閱長什麼樣」。退訂時原樣照抄，
        # 不要靠當下的時間重新推算 —— 13:50 用日盤訂、14:05 用夜盤退，
        # 參數對不上，券商那邊的訂閱就永遠留著，白白佔用額度。
        self.symbol_subs = {}       # symbol -> {"is_futopt": bool, "channels": set, "after_hours": bool}
        self.message_queue = asyncio.Queue()

    async def connect(self, websocket: WebSocket, symbol: str):
        is_first = False
        if symbol not in self.active_connections:
            self.active_connections[symbol] = []
            is_first = True
        self.active_connections[symbol].append(websocket)
        return is_first

    def record_sub(self, symbol, is_futopt, channel, after_hours):
        rec = self.symbol_subs.setdefault(
            symbol, {"is_futopt": is_futopt, "channels": set(), "after_hours": after_hours})
        rec["channels"].add(channel)
        return rec

    def has_channel(self, symbol, channel):
        return channel in (self.symbol_subs.get(symbol) or {}).get("channels", set())

    def disconnect(self, websocket: WebSocket, symbol: str):
        if symbol in self.active_connections:
            if websocket in self.active_connections[symbol]:
                self.active_connections[symbol].remove(websocket)
            if len(self.active_connections[symbol]) == 0:
                del self.active_connections[symbol]
                return True
        return False

    async def broadcast(self, symbol: str, data: dict):
        if symbol in self.active_connections:
            for connection in list(self.active_connections[symbol]):
                try:
                    await connection.send_json(data)
                except Exception:
                    pass

manager = ConnectionManager()
sdk = None
loop = None

from collections import deque, defaultdict
import copy

# Momentum tracking
LATEST_QUOTES = {}
# BUCKETS stores the last 30 buckets (5 minutes if 10s per bucket)
# Each bucket is a dict: symbol -> {'price': float, 'vol': int, 'large_vol': int}
MOMENTUM_BUCKETS = deque(maxlen=30)
CURRENT_BUCKET = defaultdict(lambda: {'price': None, 'vol': 0, 'large_vol': 0})


sdk_last_msg_time = time.time()
sdk_retry_count = 0

SUBSCRIBE_FAILURES = deque(maxlen=200)


def handle_fubon_message(message):
    global sdk_last_msg_time, sdk_retry_count
    try:
        sdk_last_msg_time = time.time()
        sdk_retry_count = 0
        msg = json.loads(message)
        event = msg.get("event")
        data = msg.get("data")
        channel = msg.get("channel")
        
        # Debug: log channel and top-level keys for every data message
        if event == "data" and data:
            data_keys = list(data.keys()) if isinstance(data, dict) else f"(type={type(data).__name__})"
            if channel == "trades":
                sym = data.get("symbol")
                if sym:
                    price = data.get("price")
                    if price is None and "trades" in data and len(data["trades"]) > 0:
                        price = data["trades"][-1].get("price")
                    
                    vol = data.get("size", data.get("volume", 0))
                    if vol == 0 and "trades" in data and len(data["trades"]) > 0:
                        vol = data["trades"][-1].get("size", data["trades"][-1].get("volume", 0))
                        
                    if price is not None:
                        price = float(price)
                        LATEST_QUOTES[sym] = {'price': price}
                        if CURRENT_BUCKET[sym]['price'] is None:
                            CURRENT_BUCKET[sym]['price'] = price
                        CURRENT_BUCKET[sym]['vol'] += vol
                        if vol >= 50:  # define large order as >= 50
                            CURRENT_BUCKET[sym]['large_vol'] += vol
                            
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(manager.message_queue.put(msg), loop)
        # Handle 'subscribed' confirmation events
        elif event == "subscribed" and data:
            print(f"[DEBUG] Subscribed confirmation: {msg}")
            if "symbol" in data:
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(manager.message_queue.put(msg), loop)
        # Handle API error events
        elif event == "error":
            err_text = str(data.get("message") or data.get("msg") or msg).lower() if isinstance(data, dict) else str(msg).lower()
            if "limit" in err_text or "subscribe" in err_text:
                # 訂閱失敗 = 該檔沒有即時價 = iNAV 少算它的漲跌。不要當成無害事件吞掉，
                # 一併轉發給前端，讓覆蓋率面板能反映出來。
                SUBSCRIBE_FAILURES.append({"ts": time.time(), "msg": str(msg)[:300]})
                print(f"[SDK WARN] 訂閱被拒 (累計 {len(SUBSCRIBE_FAILURES)} 次): {msg}")
            else:
                print(f"[DEBUG] Error event: {msg}")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(manager.message_queue.put(msg), loop)
    except Exception as e:
        print("Error parsing msg:", e)

# ─── Fubon SDK Watchdog (auto-reconnect with backoff) ──────────────
ACTIVE_PCF_TICKERS = ["00981A", "00403A"]


async def active_pcf_refresher():
    """在背景把主動型 ETF 的 PCF 抓到最新，不要等使用者開頁面才抓。

    以前是「開頁 → 現場啟動 Chromium → 等 10 秒」，只要那一次失敗，
    畫面就直接吃本地備份，而且要等下一次開頁才會再試。
    改成背景輪詢：拿到今日公告版就放慢，還沒拿到就持續重試。
    """
    await asyncio.sleep(5)      # 讓 SDK 先連上，不要開機就一起搶資源
    while True:
        now_tpe = datetime_now_taipei()
        # 只有在「今天的申贖清單有可能已經公告」的時段才密集重試。
        # 午夜過後 is_today_release 就會變 False，若不設這道閘，
        # 整個凌晨會每 3 分鐘啟動一次 Chromium（一晚上百次），純粹浪費。
        publish_window = now_tpe.weekday() < 5 and 7 <= now_tpe.hour < 15

        wait = PCF_TTL_TODAY
        for ticker in ACTIVE_PCF_TICKERS:
            try:
                data = await get_etf_pcf(ticker)
                pcf = (data or {}).get("PCF") or {}
                got_today = pcf.get("source") == "ezmoney" and pcf.get("is_today_release")
                if not got_today and publish_window:
                    wait = min(wait, PCF_TTL_WAITING)
            except Exception as e:
                print(f"[PCF] 背景更新 {ticker} 失敗: {e}")
                if publish_window:
                    wait = min(wait, PCF_TTL_FAILED)
        await asyncio.sleep(max(wait, 60))


def resubscribe_all():
    """把 manager 記錄過的訂閱全部重送一次（SDK 重連後使用）。

    以 manager.symbol_subs 為準 —— 那裡存的是「當初實際送出去的參數」，
    包含頻道與盤別，重送才會跟原本一致。
    """
    if not sdk:
        return 0
    count = 0
    for symbol, rec in list(manager.symbol_subs.items()):
        try:
            target = (sdk.marketdata.websocket_client.futopt if rec["is_futopt"]
                      else sdk.marketdata.websocket_client.stock)
            for channel in rec["channels"]:
                req = {"channel": channel, "symbol": symbol}
                if rec["is_futopt"]:
                    req["afterHours"] = rec["after_hours"]
                target.subscribe(req)
            count += 1
        except Exception as e:
            print(f"[SDK] 回補訂閱 {symbol} 失敗: {e}")
    return count


async def fubon_sdk_watchdog():
    global sdk_last_msg_time, sdk_retry_count
    """Periodically check Fubon SDK WS connectivity and reconnect if needed."""
    MAX_RETRIES = 10
    BASE_BACKOFF = 30  # seconds
    MAX_BACKOFF = 300  # 5 minutes cap
    
    print(f"Started Fubon SDK watchdog (max {MAX_RETRIES} consecutive retries)")
    
    while True:
        await asyncio.sleep(30)
        try:
            elapsed = time.time() - sdk_last_msg_time
            # If no message received in 90 seconds during trading hours, reconnect.
            # 盤別用台北時間判斷。用機器本地時間的話，UTC 伺服器會把台北 13:00~17:00
            # 當成非交易時段而不重連，卻把半夜當成盤中。
            h = datetime_now_taipei().hour
            is_trading = (9 <= h < 14) or (15 <= h < 24) or (h < 5)  # Day + Night sessions
            
            if is_trading and elapsed > 90:
                if sdk_retry_count >= MAX_RETRIES:
                    print(f"🛑 Fubon SDK: reached max retries ({MAX_RETRIES}), stopping watchdog. Manual restart required.")
                    return  # Exit the watchdog entirely
                
                sdk_retry_count += 1
                backoff = min(BASE_BACKOFF * (2 ** (sdk_retry_count - 1)), MAX_BACKOFF)
                print(f"⚠️ No Fubon SDK message for {elapsed:.0f}s, reconnect attempt {sdk_retry_count}/{MAX_RETRIES} (next backoff: {backoff}s)...")
                
                try:
                    sdk.marketdata.websocket_client.stock.disconnect()
                    sdk.marketdata.websocket_client.futopt.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(2)
                try:
                    sdk.marketdata.websocket_client.stock.connect()
                    sdk.marketdata.websocket_client.futopt.connect()
                    sdk_last_msg_time = time.time()
                    # 重連之後券商那邊的訂閱清單是空的，一定要把原本訂過的全部重送。
                    # 少了這一步，重連會「成功」但一筆報價都不會再進來，
                    # 畫面上的價格就停在斷線那一刻，而且沒有任何錯誤訊息。
                    restored = resubscribe_all()
                    print(f"✅ Fubon SDK reconnected successfully，已回補訂閱 {restored} 檔")
                except Exception as e:
                    print(f"❌ Fubon SDK reconnect failed: {e}")
                
                # Wait the backoff period before checking again
                await asyncio.sleep(backoff)
        except Exception as e:
            print(f"SDK watchdog error: {e}")

async def vix_scraper():
    print("Started VIX scraper background task")
    while True:
        try:
            # Use run_in_executor to avoid blocking the event loop
            ev_loop = asyncio.get_running_loop()
            res = await ev_loop.run_in_executor(None, lambda: requests.post(
                'https://mis.taifex.com.tw/futures/api/getQuoteListVIX', 
                json={'SortColumn':'','AscDesc':'A'}, 
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=5
            ))
            data = res.json()
            if data and data.get("RtCode") == "0" and "QuoteList" in data.get("RtData", {}):
                vixes = data["RtData"]["QuoteList"]
                if vixes:
                    msg = {"event": "vix_update", "data": vixes[0]}
                    # broadcast to all unique websockets
                    unique_websockets = set()
                    for websockets in manager.active_connections.values():
                        unique_websockets.update(websockets)
                    
                    for ws in unique_websockets:
                        try:
                            await ws.send_json(msg)
                        except Exception:
                            pass
        except Exception as e:
            print("VIX scraper error:", e)
        
        await asyncio.sleep(15)

async def message_processor():
    while True:
        try:
            msg = await manager.message_queue.get()
            event = msg.get("event")
            data = msg.get("data", {})
            symbol = data.get("symbol")
            if symbol:
                await manager.broadcast(symbol, msg)
            elif event == "error":
                # Fubon error responses might not contain the symbol precisely mapped
                # We broadcast the error to all clients so they at least see it
                for sym in list(manager.active_connections.keys()):
                    await manager.broadcast(sym, msg)
        except Exception as e:
            print("Message processor error:", e)


@app.websocket("/ws/{symbols}")
async def websocket_endpoint(websocket: WebSocket, symbols: str, night: bool = None,
                             trades_only: bool = False, books: str = ""):
    await websocket.accept()
    # 前端傳來的順序 = 優先順序（越前面越重要）。券商的訂閱數有上限，
    # 超額的部分一定要從「最不重要的尾巴」砍，而不是隨機掉幾檔，
    # 否則掉到權重大的成分股，iNAV 會偏得很難看。
    symbol_list = []
    for s in symbols.split(","):
        s = s.strip()
        if s and s not in symbol_list:      # 去重但保留順序
            symbol_list.append(s)

    # 大量訂閱時預設只要成交（trades），省頻寬。但有些頁面確實需要五檔（books），
    # 例如 0050 頁的「目標套利價設算」要用台積電的買賣價來設算。
    # 由前端用 ?books=2330,0050 明講它要哪些商品的五檔。
    books_wanted = {s.strip() for s in (books or "").split(",") if s.strip()}
    DEFAULT_BOOKS = {"0050", "006208", "00922", "00981A", "00403A", "00631L"}
    is_bulk = len(symbol_list) > 10

    def wants_books(symbol):
        if trades_only:
            return False
        if not is_bulk:
            return True
        return symbol in books_wanted or symbol in DEFAULT_BOOKS

    # 盤別要用台北時間判斷。用機器本地時間的話，伺服器跑在 UTC 時
    # 台北 10:00 會被當成 02:00 → after_hours=True → 整個日盤訂到夜盤頻道，
    # 期貨報價一整天都收不到，而且不會有任何錯誤訊息。
    if night is not None:
        after_hours = night
    else:
        h = datetime_now_taipei().hour
        after_hours = (h >= 14 or h < 8)

    symbols_to_subscribe = []       # 這次要新訂的
    upgrade_books = []              # 別人訂過但只有 trades，我需要 books
    already_subscribed = []
    skipped = []
    for symbol in symbol_list:
        if symbol in manager.active_connections:
            # 別人已經訂了，不佔新的額度。
            # 但如果我要五檔而現有訂閱只有成交，還是得補訂 books ——
            # 少了這一步，「已訂閱」這條路會把 ?books= 整個吃掉。
            await manager.connect(websocket, symbol)
            already_subscribed.append(symbol)
            if wants_books(symbol) and not manager.has_channel(symbol, "books"):
                upgrade_books.append(symbol)
            continue
        # 注意：manager.connect() 會立刻把 symbol 放進 active_connections，
        # 所以額度只能看 len(active_connections)，再加上 symbols_to_subscribe
        # 會把同一檔算兩次，實際上限直接砍半。
        if len(manager.active_connections) >= MAX_WS_SYMBOLS:
            skipped.append(symbol)
            continue
        await manager.connect(websocket, symbol)
        symbols_to_subscribe.append(symbol)

    # 這條連線真正持有的商品（skipped 的沒有 connect，不可以放進來）
    my_symbols = already_subscribed + symbols_to_subscribe

    # Notify client immediately for symbols already subscribed by another session
    for symbol in already_subscribed:
        try:
            await websocket.send_json({"event": "subscribed", "data": {"symbol": symbol}})
        except Exception:
            pass

    if skipped:
        # 明講被砍掉哪些，前端才能把它們算進「未報價權重」而不是假裝沒漲跌
        print(f"[WS] 訂閱額度用盡 ({MAX_WS_SYMBOLS})，跳過 {len(skipped)} 檔: "
              f"{','.join(skipped[:20])}{' …' if len(skipped) > 20 else ''}")
        try:
            await websocket.send_json({
                "event": "quota_skipped",
                "data": {"symbols": skipped, "limit": MAX_WS_SYMBOLS},
            })
        except Exception:
            pass

    # Subscribe to books and/or trades
    try:
        if sdk and (symbols_to_subscribe or upgrade_books):
            BATCH_SIZE = 20     # Batch to respect Fubon SDK rate limits

            def do_subscribe(symbol, channel):
                is_futopt = symbol[0].isalpha() and symbol != "IX0001"
                target = (sdk.marketdata.websocket_client.futopt if is_futopt
                          else sdk.marketdata.websocket_client.stock)
                req = {"channel": channel, "symbol": symbol}
                if is_futopt:
                    req["afterHours"] = after_hours
                target.subscribe(req)
                manager.record_sub(symbol, is_futopt, channel, after_hours)

            for i in range(0, len(symbols_to_subscribe), BATCH_SIZE):
                for symbol in symbols_to_subscribe[i:i + BATCH_SIZE]:
                    if wants_books(symbol):
                        do_subscribe(symbol, "books")
                    do_subscribe(symbol, "trades")
                await asyncio.sleep(0.02)

            for symbol in upgrade_books:
                try:
                    do_subscribe(symbol, "books")
                except Exception as e:
                    print(f"[WS] 補訂 {symbol} 五檔失敗: {e}")

            mode = "trades-only (bulk optimized)" if (trades_only or is_bulk) else "books+trades"
            book_syms = sorted(s for s in my_symbols if manager.has_channel(s, "books"))
            print(f"Subscribed SDK to {len(symbols_to_subscribe)} new symbols "
                  f"[{mode}, batch={BATCH_SIZE}]"
                  + (f" 含五檔: {','.join(book_syms)}" if book_syms else "")
                  + (f" (補訂五檔 {len(upgrade_books)} 檔)" if upgrade_books else ""))
    except Exception as e:
        print("Subscription error:", e)

    try:
        while True:
            # Use wait_for with timeout to create keepalive ping opportunities.
            # If no client message in 25s, we send a ping. If ping fails → connection is dead.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=25)
            except asyncio.TimeoutError:
                # No client message for 25s — send a ping to verify connection is alive
                try:
                    await websocket.send_json({"event": "ping"})
                except Exception:
                    break  # Ping failed, connection is dead
    except Exception:
        pass
    finally:
        # 一定要用 finally。原本清理寫在 except 裡，但 ping 失敗是用 break 跳出的，
        # try 正常結束 → except 不會執行 → 這條連線的訂閱永遠留在 manager 裡，
        # 佔著額度不放、broadcast 還會一直對死掉的 socket 送資料。
        # 筆電闔上、Wi-Fi 斷線這種沒有 TCP FIN 的情況正是走這條路。
        for symbol in my_symbols:
            should_unsubscribe = manager.disconnect(websocket, symbol)
            if not should_unsubscribe:
                continue
            rec = manager.symbol_subs.pop(symbol, None)
            if not sdk or not rec:
                continue
            try:
                target = (sdk.marketdata.websocket_client.futopt if rec["is_futopt"]
                          else sdk.marketdata.websocket_client.stock)
                for channel in rec["channels"]:
                    # 原樣照抄當初送出去的參數，不重新推算
                    req = {"channel": channel, "symbol": symbol}
                    if rec["is_futopt"]:
                        req["afterHours"] = rec["after_hours"]
                    target.unsubscribe(req)
                print(f"Unsubscribed SDK from: {symbol} ({','.join(sorted(rec['channels']))})")
            except Exception as e:
                print(f"Unsubscribe error for {symbol}:", e)


# Ensure static dir exists
if not os.path.exists(os.path.join(BASE_DIR, "static")):
    os.makedirs(os.path.join(BASE_DIR, "static"))

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/api/meta/{symbol}")
async def get_meta(symbol: str):
    if not sdk:
        return {"error": "SDK not initialized"}
    
    try:
        is_futopt = symbol[0].isalpha() and symbol != "IX0001"
        client = sdk.marketdata.rest_client.futopt if is_futopt else sdk.marketdata.rest_client.stock
        res = client.intraday.quote(symbol=symbol)
        return res
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/options-chain/{futures_symbol}")
async def get_options_chain(futures_symbol: str, strikes: int = 17, interval: int = 100, weekly: bool = True, night: bool = False):
    """Fetch options chain centered around the current futures price.
    weekly=True (default): use nearest weekly options (TX1/TX2/TX4/TX5)
    weekly=False: use monthly options (TXO)
    """
    if not sdk:
        return {"error": "SDK not initialized"}
    
    try:
        from datetime import datetime, timedelta
        
        # 1. Get current futures price + TAIEX spot index in parallel
        fut_client = sdk.marketdata.rest_client.futopt
        stock_client = sdk.marketdata.rest_client.stock
        
        fut_kwargs = {"type": "afterHours"} if night else {}
        
        ev_loop = asyncio.get_running_loop()
        fut_task = ev_loop.run_in_executor(None, lambda: fut_client.intraday.quote(symbol=futures_symbol, **fut_kwargs))
        spot_task = ev_loop.run_in_executor(None, lambda: stock_client.intraday.quote(symbol='IX0001'))
            
        futures_quote, spot_quote = await asyncio.gather(fut_task, spot_task, return_exceptions=True)
        
        if isinstance(futures_quote, Exception):
            return {"error": f"Cannot fetch futures: {futures_quote}"}
        
        futures_price = futures_quote.get("lastPrice") or futures_quote.get("closePrice", 0)
        futures_change = futures_quote.get("change", 0)
        futures_change_pct = futures_quote.get("changePercent", 0)
        futures_name = futures_quote.get("name", futures_symbol)
        futures_close_price = futures_quote.get("closePrice")
        futures_prev_close = futures_quote.get("previousClose")
        
        # TAIEX spot index uses closePrice (no lastPrice for indices)
        spot_data = None
        if not isinstance(spot_quote, Exception) and spot_quote:
            spot_price = spot_quote.get("closePrice") or spot_quote.get("lastPrice")
            spot_data = {
                "symbol": "IX0001",
                "name": spot_quote.get("name", "加權指數"),
                "price": spot_price,
                "change": spot_quote.get("change", 0),
                "changePct": spot_quote.get("changePercent", 0),
                "previousClose": spot_quote.get("previousClose"),
                "closePrice": spot_quote.get("closePrice"),
            }
        
        if not futures_price:
            return {"error": f"Cannot get price for {futures_symbol}"}
        
        # 2. Determine option product code and month/year
        if weekly:
            # Calculate nearest Wednesday expiry
            now = datetime.now()
            today = now.date()
            weekday = today.weekday()  # Monday=0 ... Sunday=6
            
            # Find next Wednesday
            days_to_wed = (2 - weekday) % 7
            if days_to_wed == 0:
                # Today is Wednesday — if past 13:30 settlement, use next week
                if now.hour > 13 or (now.hour == 13 and now.minute >= 30):
                    days_to_wed = 7
            
            expiry_date = today + timedelta(days=days_to_wed)
            days_to_expiry = (expiry_date - today).days
            
            # Determine which week of the month (1-5)
            day = expiry_date.day
            if day <= 7:
                week_num = 1
            elif day <= 14:
                week_num = 2
            elif day <= 21:
                week_num = 3
            elif day <= 28:
                week_num = 4
            else:
                week_num = 5
            
            # Product code: week 3 = monthly (TXO), others = TX{n}
            if week_num == 3:
                product = "TXO"
            else:
                product = f"TX{week_num}"
            
            # Month/year from the expiry date (NOT the futures symbol)
            call_month = chr(ord('A') + expiry_date.month - 1)
            put_month = chr(ord('M') + expiry_date.month - 1)
            year_code = str(expiry_date.year % 10)
            
            expiry_str = expiry_date.strftime("%Y-%m-%d")
            expiry_weekday = ["一", "二", "三", "四", "五", "六", "日"][expiry_date.weekday()]
            
            print(f"[OPTIONS] Weekly mode: product={product}, expiry={expiry_str}(週{expiry_weekday}), "
                  f"days_to_expiry={days_to_expiry}, call={call_month}{year_code}, put={put_month}{year_code}")
        else:
            # Monthly mode: extract from futures symbol (e.g. TXFD6 → month=D, year=6)
            product = "TXO"
            call_month = futures_symbol[-2]
            year_code = futures_symbol[-1]
            put_month = chr(ord('M') + (ord(call_month.upper()) - ord('A')))
            
            # Estimate expiry: 3rd Wednesday of the month
            month_idx = ord(call_month.upper()) - ord('A')  # 0-based
            year = (datetime.now().year // 10) * 10 + int(year_code)
            # Find 3rd Wednesday
            import calendar
            cal = calendar.monthcalendar(year, month_idx + 1)
            third_wed = [week[2] for week in cal if week[2] != 0][2]
            from datetime import date
            expiry_date = date(year, month_idx + 1, third_wed)
            days_to_expiry = (expiry_date - datetime.now().date()).days
            expiry_str = expiry_date.strftime("%Y-%m-%d")
            expiry_weekday = ["一", "二", "三", "四", "五", "六", "日"][expiry_date.weekday()]
            week_num = 3
            
            print(f"[OPTIONS] Monthly mode: product=TXO, expiry={expiry_str}, "
                  f"days_to_expiry={days_to_expiry}, call={call_month}{year_code}, put={put_month}{year_code}")
        
        # 3. Calculate center strike (round to nearest interval)
        center_strike = round(futures_price / interval) * interval
        
        # 4. Generate strike list
        strike_list = [center_strike + (i - strikes) * interval for i in range(2 * strikes + 1)]
        
        # 5. Build all option symbols to fetch
        symbols_to_fetch = []
        for strike in strike_list:
            s = int(strike)
            call_sym = f"{product}{s}{call_month}{year_code}"
            put_sym = f"{product}{s}{put_month}{year_code}"
            symbols_to_fetch.append(("call", s, call_sym))
            symbols_to_fetch.append(("put", s, put_sym))
        
        # 6. Fetch all quotes in parallel using thread pool
        def fetch_one(opt_type, strike, symbol):
            try:
                q = fut_client.intraday.quote(symbol=symbol, **fut_kwargs)
                return (opt_type, strike, symbol, q)
            except Exception:
                return (opt_type, strike, symbol, {})
        
        results = {}
        ev_loop = asyncio.get_running_loop()
        tasks = [
            ev_loop.run_in_executor(None, fetch_one, ot, st, sy)
            for ot, st, sy in symbols_to_fetch
        ]
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        
        for item in completed:
            if isinstance(item, Exception):
                continue
            opt_type, strike, symbol, quote = item
            if strike not in results:
                results[strike] = {"strike": strike}
            total = quote.get("total", {})
            last_trade = quote.get("lastTrade", {})
            results[strike][opt_type] = {
                "symbol": symbol,
                "name": quote.get("name", ""),
                "lastPrice": quote.get("lastPrice"),
                "closePrice": quote.get("closePrice"),
                "previousClose": quote.get("previousClose"),
                "change": quote.get("change"),
                "changePercent": quote.get("changePercent"),
                "volume": total.get("tradeVolume") if isinstance(total, dict) else None,
                "bidPrice": last_trade.get("bid") if isinstance(last_trade, dict) else None,
                "askPrice": last_trade.get("ask") if isinstance(last_trade, dict) else None,
            }
        
        # Sort by strike ascending
        chain = sorted(results.values(), key=lambda x: x["strike"])
        
        return {
            "futuresSymbol": futures_symbol,
            "futuresName": futures_name,
            "futuresPrice": futures_price,
            "futuresClosePrice": futures_close_price,
            "futuresPreviousClose": futures_prev_close,
            "futuresChange": futures_change,
            "futuresChangePct": futures_change_pct,
            "spot": spot_data,
            "centerStrike": center_strike,
            "product": product,
            "callMonth": call_month,
            "putMonth": put_month,
            "yearCode": year_code,
            "interval": interval,
            "weekly": weekly,
            "expiryDate": expiry_str,
            "daysToExpiry": days_to_expiry,
            "chain": chain
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

@app.get("/tdcc")
async def get_tdcc_dashboard():
    with open(os.path.join(BASE_DIR, "static", "tdcc_dashboard.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

from pydantic import BaseModel

class TaiexRequest(BaseModel):
    token: str
    start_date: str = None
    end_date: str = None

@app.post("/api/taiex")
def get_taiex_benchmark(req: TaiexRequest):
    try:
        import finlab
        from finlab import data
        import pandas as pd
        
        # Login
        finlab.login(api_token=req.token)
        
        # Fetch the benchmark total return index
        df = data.get('benchmark_return:發行量加權股價報酬指數')
        
        # Reset index to get the date column
        df = df.reset_index()
        df.columns = ['date', 'value']
        
        # Format date column
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
        # Filter by start_date / end_date
        if req.start_date:
            df = df[df['date'] >= req.start_date]
        if req.end_date:
            df = df[df['date'] <= req.end_date]
            
        # Convert to list of dicts
        records = df.to_dict(orient='records')
        return {"success": True, "data": records}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

@app.get("/")
@app.get("/options")
@app.get("/etf0050")
@app.get("/disposal")
@app.get("/queue")
@app.get("/sector_heatmap")
@app.get("/active_etf")
@app.get("/active-etf")
async def get_app_wrapper():
    with open(os.path.join(BASE_DIR, "static", "app.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/_content/{page}")
async def get_content(page: str):
    valid = {"index", "options", "etf0050", "disposal", "queue", "sector_heatmap", "active_etf"}
    if page not in valid: 
        return HTMLResponse("Not Found", status_code=404)
    with open(os.path.join(BASE_DIR, "static", f"{page}.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

pcf_cache = {}


TAIPEI_TZ = None  # 延遲初始化，見 _taipei_tz()


def _taipei_tz():
    global TAIPEI_TZ
    if TAIPEI_TZ is None:
        from datetime import timezone, timedelta
        TAIPEI_TZ = timezone(timedelta(hours=8))
    return TAIPEI_TZ


def datetime_now_taipei():
    """所有「現在幾點/今天幾號」的判斷都要用台北時間，伺服器時區不該影響盤別。"""
    from datetime import datetime
    return datetime.now(_taipei_tz())


def parse_pcf_trandate(v):
    """PCF 來源可能是 ISO 字串或 .NET 的 '/Date(1785859200000)/'，統一轉成 date。

    同一個 GetPCF 端點兩種格式都會出現（實測 00981A 回 ISO、00403A 回 .NET），
    所以兩種都得吃。.NET 那個 epoch 是「台北時間的午夜」，一定要用固定的 +08:00
    去換算 —— 用機器本地時區的話，伺服器如果跑在 UTC 就會整整少一天。
    """
    if not v:
        return None
    from datetime import datetime

    def _from_ms(ms):
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=_taipei_tz()).date()

    if isinstance(v, (int, float)):
        try:
            return _from_ms(v)
        except Exception:
            return None
    s = str(v).strip()
    m = re.search(r"/Date\((-?\d+)", s)
    if m:
        try:
            return _from_ms(int(m.group(1)))
        except Exception:
            return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt).date()
        except Exception:
            continue
    return None


def _business_days_between(d_from, d_to):
    """d_from(不含) 到 d_to(含) 之間有幾個工作日。不含國定假日，只用於提示。

    以前用 `weeks*5 + min(rem,5)` 近似，星期五的資料到了星期一會被算成落後 3 天，
    於是每個星期一都誤判成過期 —— 一定要真的數。
    """
    from datetime import timedelta
    if not d_from or not d_to or d_to <= d_from:
        return 0
    days = 0
    cur = d_from
    while cur < d_to:
        cur += timedelta(days=1)
        if cur.weekday() < 5:       # 0=一 … 4=五
            days += 1
    return days


def taipei_today():
    from datetime import datetime
    return datetime.now(_taipei_tz()).date()


def last_trading_day(d=None):
    """最近一個交易日（只避開週末，不含國定假日）。

    週末時最新的公告本來就是週五那一份，不加這層的話整個週末都會
    以為「還沒等到今天的公告」，每 3 分鐘白開一次 Chromium。
    """
    from datetime import timedelta
    d = d or taipei_today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def annotate_pcf(res_data: dict, source: str):
    """在 PCF 上補齊 trandate_iso / postdate_iso / source / stale_days。"""
    if not isinstance(res_data, dict) or "error" in res_data:
        return res_data
    pcf = res_data.setdefault("PCF", {})
    d = parse_pcf_trandate(pcf.get("trandate_iso") or pcf.get("trandate"))
    pcf["trandate_iso"] = d.isoformat() if d else None

    # PostDate = 這份申贖清單的「公告日」。它等於今天，就代表我們拿到的
    # 確實是今天這一版（基金一天只公告一次），不必再去猜日期差。
    pd = parse_pcf_trandate(pcf.get("postdate_iso") or pcf.get("postdate"))
    pcf["postdate_iso"] = pd.isoformat() if pd else None

    pcf["source"] = source
    today_tpe = taipei_today()
    pcf["is_today_release"] = bool(pd and pd >= last_trading_day(today_tpe))
    pcf["stale_days"] = _business_days_between(d, today_tpe) if d else None
    return res_data


# PCF 本地備份放在專用目錄，不要放在專案根目錄。
# 放根目錄很容易被 commit 進版控（實際發生過）：某台機器抓到的淨值被推上 git，
# 另一台 clone 下來就算爬蟲整個壞掉，也永遠有一份「看起來正常」的化石資料墊底，
# 故障因此跨機器偽裝成正常。這個目錄天生就在版控之外。
PCF_CACHE_DIR = os.path.join(BASE_DIR, ".pcf_cache")


def pcf_backup_paths(ticker):
    """回傳 (寫入路徑, [讀取候選路徑...])。舊版寫在專案根目錄，仍可讀但不再寫入。"""
    primary = os.path.join(PCF_CACHE_DIR, f"{ticker}.json")
    legacy = os.path.join(BASE_DIR, f"{ticker}.json")
    return primary, [primary, legacy]


EZMONEY_PCF_URL = 'https://www.ezmoney.com.tw/ETF/Transaction/PCF'
# 已知的 fundCode。若網站改碼，會自動從頁面上的基金下拉選單重新探索並覆寫這裡。
EZMONEY_FUND_CODES = {'00981A': '49YTW', '00403A': '63YTW'}


async def _ezmoney_list_fund_options(page):
    """把 PCF 頁面上所有可選的基金（下拉選單 option 或連結）抓出來。"""
    return await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('select option').forEach(o => out.push({
            kind: 'option',
            value: (o.value || '').trim(),
            text: (o.textContent || '').trim(),
            select: o.parentElement ? (o.parentElement.id || o.parentElement.name || '') : ''
        }));
        document.querySelectorAll('a[href*="fundCode"], [data-fundcode], [data-fund-code]').forEach(a => {
            const href = a.getAttribute('href') || '';
            const m = href.match(/fundCode=([^&#]+)/i);
            out.push({
                kind: 'link',
                value: (a.getAttribute('data-fundcode') || a.getAttribute('data-fund-code') || (m ? m[1] : '')).trim(),
                text: (a.textContent || '').trim(),
                select: href
            });
        });
        return out;
    }""")


def _ezmoney_match_codes(options):
    """從選單文字裡把台股 ETF 代號（如 00981A）對到它的 fundCode。"""
    found = {}
    for o in options or []:
        val = (o.get('value') or '').strip()
        if not val:
            continue
        m = re.search(r'(\d{4,6}[A-Z]?)', o.get('text') or '')
        if m:
            found.setdefault(m.group(1), val)
    return found


async def _scrape_ezmoney_pcf(ticker: str):
    """Scrape PCF and Asset holdings for Active ETFs from ezmoney using Playwright.

    實測（2026-08-14）結論：
    - `?fundCode=XXXXX` 這條路徑正常，開頁後站方會自己打 POST /ETF/Transaction/GetPCF。
    - **不要**用下拉選單的 select_option()：那些 <option> 是隱藏的（外面包了自訂樣式的
      下拉），選了也不會觸發任何請求，只會拿到頁面預設帶出來的那一檔。
      選單只拿來「讀取 代號 -> fundCode 對照」，不拿來當觸發手段。
    - **不要**攔截/中止任何請求。原本用 route.abort() 擋 google/analytics，
      是這支爬蟲最可能的失效點，而且拿掉之後頁面照樣秒開。
    """
    diag = {'urls_seen': [], 'options': None, 'used_code': None, 'title': None}

    # 例外一律往外拋，由 fetch_ezmoney_pcf() 分類並決定要不要退回本地備份
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        raw_data = {}

        async def handle_response(response):
            url = response.url
            low = url.lower()
            if any(k in low for k in ('pcf', 'fund', 'asset', 'nav')):
                if url not in diag['urls_seen']:
                    diag['urls_seen'].append(url)
            if 'getpcf' in low:
                try:
                    payload = await response.json()
                except Exception:
                    return
                # 只接受「確實是這一檔、而且淨值不是 0」的回應。
                # 頁面預設會先帶出另一檔（且欄位全為 0），不擋掉就會覆蓋掉正確結果。
                if not isinstance(payload, dict) or not payload:
                    return
                finfo = payload.get('fund') or {}
                got = str(finfo.get('sStockNo') or '').strip().upper()
                if got and got != ticker.upper():
                    return
                items = {i.get('PCFCode'): i.get('Amount')
                         for i in (payload.get('pcf') or []) if isinstance(i, dict)}
                try:
                    if float(items.get('NAV') or 0) <= 0:
                        return
                except (TypeError, ValueError):
                    return
                raw_data.clear()
                raw_data.update(payload)
        page.on('response', handle_response)

        async def wait_for_data(seconds=12):
            for _ in range(int(seconds * 2)):
                if raw_data:
                    return True
                await asyncio.sleep(0.5)
            return bool(raw_data)

        async def load_with_code(code):
            diag['used_code'] = code
            await page.goto(f'{EZMONEY_PCF_URL}?fundCode={code}',
                            wait_until='domcontentloaded', timeout=30000)
            return await wait_for_data()

        # 1) 快路徑：用已知的 fundCode 直接開
        fund_code = EZMONEY_FUND_CODES.get(ticker)
        if fund_code:
            await load_with_code(fund_code)

        # 2) 沒拿到 → 回清單頁，從選單「讀出」新的 fundCode 對照表再試一次。
        #    （只讀取，不點選 —— option 是隱藏的，選了不會觸發載入）
        if not raw_data:
            print(f"[PCF] {ticker} 以 fundCode={fund_code} 未取得資料，改從基金選單探索…")
            await page.goto(EZMONEY_PCF_URL, wait_until='domcontentloaded', timeout=30000)
            try:
                await page.wait_for_selector('select option, a[href*="fundCode"]',
                                             state='attached', timeout=10000)
            except Exception:
                pass
            try:
                diag['title'] = await page.title()
            except Exception:
                pass

            options = await _ezmoney_list_fund_options(page)
            diag['options'] = options
            discovered = _ezmoney_match_codes(options)
            if discovered:
                EZMONEY_FUND_CODES.update(discovered)
                print(f"[PCF] 從選單探索到 {len(discovered)} 檔基金，{ticker} -> {discovered.get(ticker)}")

            new_code = discovered.get(ticker)
            if new_code and new_code != fund_code:
                await load_with_code(new_code)

        await browser.close()

        if not raw_data:
            print(f"[PCF] {ticker} 抓取失敗。標題={diag['title']!r} "
                  f"使用代碼={diag['used_code']!r} "
                  f"攔到的相關請求={diag['urls_seen'][:8]}")
            if diag['options'] is not None:
                preview = [f"{o.get('value')}={o.get('text')[:24]}" for o in diag['options'][:25]]
                print(f"[PCF] 頁面上的選項({len(diag['options'])}): {preview}")

        if raw_data:
            pcf_items = {item.get('PCFCode'): item.get('Amount') for item in (raw_data.get('pcf') or []) if isinstance(item, dict)}
            out_unit = float(pcf_items.get('OUT_UNIT') or 1)
            baseunit = float(pcf_items.get('FUND_BASEUNIT') or 500000)
            nav_total = float(pcf_items.get('NAV') or 0)
            p_unit = float(pcf_items.get('P_UNIT') or 0)

            # High-precision NAV: NAV / OUT_UNIT (e.g. 28.58232345...)
            nav = (nav_total / out_unit) if (nav_total > 0 and out_unit > 0) else (p_unit or 0)
            cash_diff = float(pcf_items.get('DIFF_ACT_AMT') or 0)

            comp = []
            futures = []
            for asset_grp in (raw_data.get('asset') or []):
                if not isinstance(asset_grp, dict): continue
                asset_code = asset_grp.get('AssetCode')
                for d in (asset_grp.get('Details') or []):
                    if not isinstance(d, dict): continue
                    code = str(d.get('DetailCode') or '').strip()
                    name = str(d.get('DetailName') or '').strip()
                    share = float(d.get('Share') or 0)
                    weight = float(d.get('NavRate') or 0)
                    if asset_code == 'ST' or d.get('Type') == '1' or not d.get('MTH'):
                        qty_per_basket = (share / out_unit) * baseunit if out_unit > 0 else share
                        comp.append({
                            'stkcd': code,
                            'name': name,
                            'qty': round(qty_per_basket, 2),
                            'total_shares': share,
                            'weight': weight
                        })
                    elif asset_code == 'GD' or d.get('Type') == '2' or d.get('MTH'):
                        futures.append({
                            'code': code,
                            'name': name,
                            'qty': share,
                            'weight': weight,
                            'mth': str(d.get('MTH') or '')
                        })

            fund_info = raw_data.get('fund') or {}
            pcf_list = raw_data.get('pcf') or []
            tran_date = ""
            post_date = ""
            if pcf_list and isinstance(pcf_list[0], dict):
                tran_date = str(pcf_list[0].get('TranDate') or pcf_list[0].get('PostDate') or '')
                post_date = str(pcf_list[0].get('PostDate') or '')
            if not tran_date:
                tran_date = str(fund_info.get('FundDate') or '')

            # 身分檢查：確認回來的真的是我們要的那一檔。
            # ezmoney 的 PCF 頁有下拉選單，如果 fundCode 沒生效就會回預設基金，
            # 那會安靜地把別檔的淨值畫到這一檔上——寧可退回舊資料也不能顯示錯的。
            got_no = str(fund_info.get('sStockNo') or '').strip().upper()
            if got_no and got_no != ticker.upper():
                print(f"[PCF] !! 要 {ticker} 卻收到 {got_no} "
                      f"({fund_info.get('sFundName')})，fundCode 未生效，放棄本次結果")
                raw_data = {}

        if raw_data:
            res_data = {
                'PCF': {
                    'nav': nav,
                    'p_unit': p_unit,
                    'official_inav': p_unit if p_unit > 0 else nav,
                    'nav_total': nav_total,
                    'out_unit': out_unit,
                    'baseunit': baseunit,
                    'estdvalue': cash_diff,
                    'trandate': tran_date,
                    'postdate': post_date,
                    'is_total_fund': False
                },
                'InKind': {
                    'FundComposition': comp
                },
                'Futures': futures,
                'FundName': str(fund_info.get('sFundName') or ticker),
                'StockNo': str(fund_info.get('sStockNo') or ticker)
            }

            annotate_pcf(res_data, 'ezmoney')

            # Save local JSON backup（寫到 .pcf_cache/，不會進版控）
            try:
                os.makedirs(PCF_CACHE_DIR, exist_ok=True)
                with open(pcf_backup_paths(ticker)[0], 'w', encoding='utf-8') as f:
                    json.dump(res_data, f, ensure_ascii=False, indent=2)
            except Exception as save_err:
                print(f"Warning: Could not save local backup for {ticker}: {save_err}")

            return res_data


    return None


# 最近一次抓取的診斷資訊，給 /api/etf-pcf-debug 用
PCF_LAST_ATTEMPT = {}


def _run_coro_in_thread(coro_factory, timeout=120):
    """在獨立執行緒的獨立 event loop 裡跑一個 coroutine。

    Playwright 不能跟行情主迴圈搶同一個 event loop：盤中每秒有上百則報價要處理，
    迴圈被佔滿時 page.goto / wait_for_selector 的計時器會延遲觸發而 timeout，
    於是「半夜測試都好、盤中一定失敗」。丟到自己的執行緒就不受影響。
    """
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as e:      # noqa: BLE001 - 帶回主執行緒再分類
            box["error"] = e

    t = threading.Thread(target=target, name="pcf-scraper", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"PCF 抓取超過 {timeout}s 未完成")
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def fetch_ezmoney_pcf(ticker: str):
    """對外介面：在背景執行緒抓 PCF，失敗才退回本地備份。"""
    attempt = {"ts": time.time(), "ok": False, "error": None, "source": None}
    PCF_LAST_ATTEMPT[ticker] = attempt

    try:
        ev_loop = asyncio.get_running_loop()
        res_data = await ev_loop.run_in_executor(
            None, lambda: _run_coro_in_thread(lambda: _scrape_ezmoney_pcf(ticker)))
        if res_data:
            attempt.update(ok=True, source="ezmoney",
                           trandate=(res_data.get("PCF") or {}).get("trandate_iso"),
                           postdate=(res_data.get("PCF") or {}).get("postdate_iso"))
            return res_data
        attempt["error"] = "GetPCF 沒有回應（詳見主控台的 [PCF] 訊息）"
    except ImportError:
        attempt["error"] = "playwright 未安裝"
        print(f"[PCF] !! 找不到 playwright，{ticker} 無法抓取即時 PCF，只能用本地快取。\n"
              f"       修復方式：\n"
              f"         python -m pip install playwright\n"
              f"         python -m playwright install chromium")
    except Exception as e:
        msg = str(e)
        if "Executable doesn" in msg or "playwright install" in msg or "BrowserType.launch" in msg:
            attempt["error"] = "playwright 瀏覽器本體未安裝"
            print(f"[PCF] !! Playwright 瀏覽器本體未安裝，{ticker} 只能用本地快取。\n"
                  f"       修復方式： python -m playwright install chromium")
        else:
            attempt["error"] = f"{type(e).__name__}: {msg[:200]}"
            print(f"Ezmoney scraper error for {ticker}: {e}")

    # Fallback: Read local JSON backup if available.
    # 重要：這份備份可能是好幾天前的。用舊淨值搭配今天的昨收會讓 iNAV 整段偏掉，
    # 所以一定要把基準日與過期天數標出來給前端。
    for candidate in pcf_backup_paths(ticker)[1]:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            annotate_pcf(cached, 'local-backup')
            pcf = cached.get('PCF') or {}
            attempt.update(source="local-backup", trandate=pcf.get("trandate_iso"),
                           backup_path=candidate)
            print(f"Loading local fallback PCF for {ticker} from {candidate} "
                  f"(基準日 {pcf.get('trandate_iso')}, 落後約 {pcf.get('stale_days')} 個交易日)")
            return cached
        except Exception as f_err:
            print(f"Failed to read local fallback {candidate} for {ticker}: {f_err}")

    return {"error": f"Failed to receive GetPCF response for {ticker} from ezmoney"}

@app.get("/api/etf-pcf-debug/{ticker}")
async def etf_pcf_debug(ticker: str):
    """一鍵診斷主動型 ETF 的 PCF 抓取狀況。

    直接用瀏覽器開 http://127.0.0.1:8000/api/etf-pcf-debug/00981A 就看得到，
    不必去終端機翻 log。
    """
    info = {"ticker": ticker, "now_taipei": datetime_now_taipei().isoformat()}

    # 1. 環境：playwright 套件與瀏覽器本體是不是都在
    try:
        import playwright
        info["playwright_version"] = getattr(playwright, "__version__", "unknown")
        try:
            from playwright.async_api import async_playwright
            info["playwright_import"] = "ok"
        except Exception as e:
            info["playwright_import"] = f"FAILED: {e}"
    except ImportError:
        info["playwright_version"] = None
        info["playwright_import"] = "NOT INSTALLED — pip install playwright"

    # 2. 本地備份檔的狀態
    import datetime as _dt
    info["local_backup"] = []
    for path in pcf_backup_paths(ticker)[1]:
        if os.path.exists(path):
            st = os.stat(path)
            info["local_backup"].append({
                "path": path,
                "mtime": _dt.datetime.fromtimestamp(st.st_mtime, _taipei_tz()).isoformat(),
                "bytes": st.st_size,
            })

    # 3. 記憶體快取狀態
    entry = pcf_cache.get(ticker)
    info["cache"] = None if not entry else {
        "age_seconds": round(time.time() - entry.get("ts", 0), 1),
        "live": entry.get("live"),
        "is_today_release": entry.get("is_today_release"),
    }

    # 4. 真的抓一次（強制，不吃快取）
    started = time.time()
    data = await fetch_ezmoney_pcf(ticker)
    info["fetch_seconds"] = round(time.time() - started, 1)
    info["last_attempt"] = PCF_LAST_ATTEMPT.get(ticker)

    pcf = (data or {}).get("PCF") or {}
    info["result"] = {
        "error": (data or {}).get("error"),
        "source": pcf.get("source"),
        "fund_name": (data or {}).get("FundName"),
        "stock_no": (data or {}).get("StockNo"),
        "trandate": pcf.get("trandate_iso"),
        "postdate": pcf.get("postdate_iso"),
        "is_today_release": pcf.get("is_today_release"),
        "stale_days": pcf.get("stale_days"),
        "nav_per_unit": pcf.get("nav"),
        "n_holdings": len(((data or {}).get("InKind") or {}).get("FundComposition") or []),
    }

    src = pcf.get("source")
    if src == "ezmoney":
        info["verdict"] = "OK — 這是剛從 ezmoney 抓回來的即時資料"
    elif src == "local-backup":
        info["verdict"] = ("爬蟲失敗，畫面上看到的是本地備份。原因見 last_attempt.error，"
                           "以及主控台的 [PCF] 訊息")
    else:
        info["verdict"] = "抓取失敗且沒有可用的本地備份"
    return info


@app.get("/api/etf-pcf/{ticker}")
async def get_etf_pcf(ticker: str, force: bool = False):
    """Proxy endpoint to fetch PCF data for different ETFs.

    快取策略（主動型 ETF）：判斷「是不是最新」不靠日期推算，而是看 PostDate ——
    那是這份申贖清單的公告日，基金一天只公告一次，PostDate == 今天就代表
    我們手上的確實是今天這一版，沒有更新的可以拿了。

    - 拿到今天公告的那一版  → 快取 30 分鐘（它就是最新，重抓也是同一份）
    - 還沒等到今天的公告    → 只快取 3 分鐘，持續回頭確認（早盤開站常遇到）
    - 抓取失敗吃本地備份    → 只擋 60 秒，避免每個 request 都重開 Chromium
    - `?force=1`            → 完全跳過快取（前端「重新載入 PCF」按鈕用）
    """
    now = time.time()

    if ticker in ["00981A", "00403A"]:
        entry = pcf_cache.get(ticker)
        if entry and not force:
            age = now - entry.get("ts", 0)
            ttl = (PCF_TTL_TODAY if entry.get("is_today_release")
                   else PCF_TTL_WAITING if entry.get("live")
                   else PCF_TTL_FAILED)
            if age < ttl:
                return entry["data"]

        res_data = await fetch_ezmoney_pcf(ticker)
        if res_data and "error" not in res_data:
            pcf = res_data.get("PCF") or {}
            live = pcf.get("source") == "ezmoney"
            is_today = bool(pcf.get("is_today_release"))
            pcf_cache[ticker] = {
                "ts": now,
                "live": live,
                "is_today_release": is_today,
                "data": res_data,
            }
            if live and is_today:
                print(f"[PCF] {ticker} 已是今日公告版本 "
                      f"(公告日 {pcf.get('postdate_iso')}, 基準日 {pcf.get('trandate_iso')})")
            elif live:
                print(f"[PCF] {ticker} 尚未取得今日公告 "
                      f"(公告日 {pcf.get('postdate_iso')}, 基準日 {pcf.get('trandate_iso')})，"
                      f"{PCF_TTL_WAITING} 秒後會再確認")
            else:
                print(f"[PCF] {ticker} 使用本地備份 (基準日 {pcf.get('trandate_iso')}, "
                      f"落後 {pcf.get('stale_days')} 個交易日)")
        return res_data

    today_str = taipei_today().isoformat()
    entry = pcf_cache.get(ticker)
    if entry and entry.get("date") == today_str and entry.get("fresh") and not force:
        ttl = entry.get("ttl")
        if ttl is None or (now - entry.get("ts", 0)) < ttl:
            return entry["data"]

    if ticker == "00631L":
        import uuid
        device_id = str(uuid.uuid4())
        url = (
            f"https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
            f"?APIType=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2F"
            f"&DeviceId={device_id}&FuncId=PCF%2FDaily&AppName=ETF"
            f"&Device=3&Platform=ETF&ticker=00631L&ndate="
        )
        try:
            ev_loop = asyncio.get_running_loop()
            res = await ev_loop.run_in_executor(None, lambda: requests.get(
                url,
                headers={
                    "Referer": "https://www.yuantaetfs.com/",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "application/json",
                },
                timeout=10
            ))
            raw_data = res.json()
            pcf = raw_data.get('PCF', {})
            fw = raw_data.get('FundWeights', {})

            comp = []
            # 不可以寫 `float(x or 1)` —— 那會讓下面的 `if out_unit > 0` 變成死碼，
            # 欄位缺漏時每檔持股數量會放大 baseunit 倍（50 萬倍），
            # 前端再除以 baseunit，就把整檔基金的資產總值當成每單位淨值印出來。
            out_unit = _num(pcf.get('osunit'), 0)
            baseunit = _num(pcf.get('baseunit'), 500000)
            if out_unit <= 0:
                raise ValueError(f"00631L osunit 無效: {pcf.get('osunit')!r}")
            for s in fw.get('StockWeights', []):
                total_shares = float(s.get('qty') or 0)
                qty_per_basket = (total_shares / out_unit) * baseunit if out_unit > 0 else 0
                comp.append({
                    'stkcd': str(s.get('code') or '').strip(),
                    'name': str(s.get('name') or '').strip(),
                    'qty': round(qty_per_basket, 2),
                    'total_shares': total_shares,
                    'weight': float(s.get('weights') or 0)
                })

            futures = []
            for f in fw.get('FutureWeights', []):
                futures.append({
                    'code': str(f.get('code') or '').strip(),
                    'name': str(f.get('name') or '').strip(),
                    'qty': float(f.get('qty') or 0),
                    'weight': float(f.get('weights') or 0),
                    'mth': str(f.get('ym') or '')
                })

            official_inav = None
            try:
                url_inav = (
                    f"https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
                    f"?APIType=ETFBackstage&CompanyName=YUANTAFUNDS&PageName=%2F"
                    f"&DeviceId={device_id}&FuncId=ETFNAV%2FGetINAV_Data&AppName=ETF"
                    f"&Device=3&Platform=ETF&ticker=00631L&ndate="
                )
                res_inav = await ev_loop.run_in_executor(None, lambda: requests.get(
                    url_inav,
                    headers={"Referer": "https://www.yuantaetfs.com/", "User-Agent": "Mozilla/5.0"},
                    timeout=5
                ))
                for item in res_inav.json().get("Data", []):
                    if item.get("ETF_ID") == "00631L":
                        official_inav = float(item.get("NOW_NAV") or 0)
                        break
            except Exception:
                pass

            res_data = {
                'PCF': {
                    'nav': float(pcf.get('nav') or 0),
                    'p_unit': float(pcf.get('nav') or 0),
                    'official_inav': official_inav,
                    'nav_total': float(pcf.get('totalav') or 0),
                    'out_unit': out_unit,
                    'baseunit': baseunit,
                    'estdvalue': float(pcf.get('estdvalue') or 0),
                    'trandate': str(pcf.get('trandate') or ''),
                    'is_total_fund': False
                },
                'InKind': {
                    'FundComposition': comp
                },
                'Futures': futures,
                'FundName': str(pcf.get('fundname') or '元大台灣50單日正向2倍基金'),
                'StockNo': '00631L'
            }
            if res_data:
                annotate_pcf(res_data, 'yuanta')
                # 這份資料裡的 official_inav 是元大的「即時」淨值 (NOW_NAV)。
                # 跟其他 PCF 不一樣，它盤中一直在動 —— 鎖一整天的話，
                # 09:05 抓到的淨值會被拿去跟 13:00 的市價比，
                # 憑空生出一個幾個百分點的假折價。給它短 TTL。
                pcf_cache[ticker] = {"date": today_str, "fresh": True,
                                     "ts": now, "ttl": 60, "data": res_data}
            return res_data
        except Exception as e:
            return {"error": str(e)}

    if ticker == "0050":
        import uuid
        device_id = str(uuid.uuid4())
        url = (
            f"https://etfapi.yuantaetfs.com/ectranslation/api/bridge"
            f"?APIType=ETFAPI&CompanyName=YUANTAFUNDS&PageName=%2F"
            f"&DeviceId={device_id}&FuncId=PCF%2FDaily&AppName=ETF"
            f"&Device=3&Platform=ETF&ticker=0050&ndate="
        )
        try:
            ev_loop = asyncio.get_running_loop()
            res = await ev_loop.run_in_executor(None, lambda: requests.get(
                url,
                headers={
                    "Referer": "https://www.yuantaetfs.com/",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "application/json",
                },
                timeout=10
            ))
            return res.json()
        except Exception as e:
            return {"error": str(e)}

    elif ticker == "006208":
        # Scrape Fubon Assets
        try:
            from bs4 import BeautifulSoup
            url = "https://websys.fsit.com.tw/FubonETF/Fund/Assets.aspx?stkId=006208"
            ev_loop = asyncio.get_running_loop()
            res = await ev_loop.run_in_executor(None, lambda: requests.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            ))
            soup = BeautifulSoup(res.text, "html.parser")
            tables = soup.find_all("table")
            table = tables[1] if len(tables) > 1 else None
            comp = []
            dropped_rows = []
            if table:
                for tr in table.find_all("tr")[1:]:
                    cols = tr.find_all("td")
                    if len(cols) >= 5:
                        stkcd = cols[0].text.strip()
                        name = cols[1].text.strip()
                        if not stkcd: continue
                        qty = _num(cols[2].text, -1)
                        if qty < 0:
                            dropped_rows.append(f"{stkcd}({cols[2].text.strip()!r})")
                            continue
                        comp.append({"stkcd": stkcd, "name": name, "qty": qty})
            if dropped_rows:
                # 部分成分股解析失敗比全部失敗更危險：畫面看起來正常，
                # iNAV 卻少算了那幾檔。整批放棄，改用本地 JSON。
                raise ValueError(f"006208 有 {len(dropped_rows)} 列數量解析失敗: {dropped_rows[:5]}")
            
            # Scrape futures for 006208.
            # 以前所有含「期貨」的列都被加總成同一個代號、統一乘 200。
            # 小台是 50、電子期 4000、金融期 1000，混在一起會直接算錯；
            # 不同到期月也會被壓成最後解析到的那一個月。改成依
            # (商品, 月份) 分開累計，並用各自的乘數。
            FUT_ROOTS = [("小型臺指", "MXF", 50), ("小型台指", "MXF", 50),
                         ("電子", "TEF", 4000), ("金融", "TFF", 1000),
                         ("臺股期貨", "TXF", 200), ("台股期貨", "TXF", 200),
                         ("臺指", "TXF", 200), ("台指", "TXF", 200)]
            if tables:
                fut_acc = {}
                for tr in tables[0].find_all("tr"):
                    cols = [c.text.strip() for c in tr.find_all("td")]
                    if len(cols) < 5 or "期貨" not in cols[1]:
                        continue
                    label = cols[1]
                    root, mult = None, None
                    for kw, r, mv in FUT_ROOTS:
                        if kw in label:
                            root, mult = r, mv
                            break
                    if not root:
                        print(f"[006208] 未知的期貨商品，未計入: {label!r}")
                        continue
                    m = re.search(r"(\d{4})/(\d{2})", label)
                    if not m:
                        print(f"[006208] 期貨列找不到契約月份，未計入: {label!r}")
                        continue
                    sym = f"{root}{chr(ord('A') + int(m.group(2)) - 1)}{m.group(1)[-1]}"
                    qty = _num(cols[2], 0)
                    if qty <= 0:
                        continue
                    acc = fut_acc.setdefault(sym, {"qty": 0.0, "mult": mult,
                                                   "name": f"{m.group(1)}/{m.group(2)} {label[:8]}"})
                    acc["qty"] += qty
                for sym, acc in fut_acc.items():
                    comp.append({"stkcd": sym, "name": acc["name"],
                                 "qty": acc["qty"] * acc["mult"]})

            # Extract units, nav, cash.
            # 這裡最危險：解析失敗時 units 會停在初始值 1，`units or 1` 又讓它看起來
            # 合法，於是前端算出 Σ(price×qty) / 1 —— 淨值會變成幾百億而不是一百出頭，
            # 而且被當成成功結果快取一整天。一定要驗證後才放行。
            units = 0
            nav_val = 0
            page_text = soup.get_text(" ", strip=True)
            m = re.search(r"基金在外流通單位數\(單位\)\s*([\d,\.]+)", page_text)
            if m:
                units = _num(m.group(1), 0)
            m = re.search(r"基金每單位淨值\(新臺?台?幣\)\s*([\d,\.]+)", page_text)
            if m:
                nav_val = _num(m.group(1), 0)

            if units <= 0 or nav_val <= 0:
                raise ValueError(
                    f"006208 流通單位數/淨值解析失敗 (units={units}, nav={nav_val})，"
                    f"頁面結構可能已改版")
            if not comp:
                raise ValueError("006208 成分股清單是空的，頁面結構可能已改版")

            res_data = {
                "PCF": {"estdvalue": 0, "baseunit": units, "is_total_fund": False, "nav": nav_val},
                "InKind": {"FundComposition": comp}
            }
            pcf_cache[ticker] = {"date": today_str, "fresh": True, "data": res_data}
            return res_data
        except Exception as e:
            print("Fubon scraper error:", e)
            # Fallback to local json if scraper fails
            pass

    elif ticker == "00922":
        # Scrape Cathay API
        try:
            from datetime import datetime
            today_str = datetime.now().strftime("%Y-%m-%d")
            today_str_slash = datetime.now().strftime("%Y/%m/%d")
            url_list = f"https://cwapi.cathaysite.com.tw/api/BuySale/GetStocksList?FundCode=DO&SearchDate={today_str_slash}&IsTest=false"
            url_meta = f"https://cwapi.cathaysite.com.tw/api/BuySale/GetBuySale?FundCode=DO&SearchDate={today_str_slash}&IsTest=false"
            h = {
                "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1laWQiOiI0MzI1ODU2IiwidW5pcXVlX25hbWUiOiIiLCJyb2xlIjoiMCIsIkVDSUQiOiIwIiwiU2Vzc2lvbklkIjoiIiwibmJmIjoxNzc1NzIzMzcxLCJleHAiOjE4MzU3MjMzMTEsImlhdCI6MTc3NTcyMzM3MX0.ZpnvUeyN5mmjWZ8lrSRaOEirbr0pu4N3YEmI3wbCZlg",
                "Origin": "https://www.cathaysite.com.tw",
                "Referer": "https://www.cathaysite.com.tw/",
                "User-Agent": "Mozilla/5.0"
            }
            ev_loop = asyncio.get_running_loop()
            res_list, res_meta = await asyncio.gather(
                ev_loop.run_in_executor(None, lambda: requests.get(url_list, headers=h, timeout=10, verify=False)),
                ev_loop.run_in_executor(None, lambda: requests.get(url_meta, headers=h, timeout=10, verify=False))
            )
            data = res_list.json()
            meta = res_meta.json().get("result", {})
            # 兩個欄位各自解析。以前共用一個 try，早盤 bm(現金差異額) 還沒公告時
            # 會連帶把 API 給的 basketUnit 一起丟掉、退回寫死的 500000，
            # 申購基數若實際是 1,000,000 就會讓 iNAV 整整差兩倍。
            estdvalue = _num(meta.get("bm"), 0)
            basket_unit = _num(meta.get("basketUnit"), 500000)
            if basket_unit <= 0:
                basket_unit = 500000

            comp = []
            dropped = []
            for item in data.get("result", []):
                stkcd = item.get("prod")
                name = item.get("prodName")
                raw = item.get("basketShares")
                qty = _num(raw, -1)
                if not stkcd or not name or qty < 0:
                    dropped.append(f"{stkcd or '?'}({raw!r})")
                    continue
                comp.append({"stkcd": stkcd, "name": name, "qty": qty})
            if dropped:
                # 靜默少一檔成分股 = iNAV 少算那一檔的權重，而且畫面上看不出來。
                # 寧可整批放棄改用本地 JSON，也不要送出一份缺角的清單。
                raise ValueError(f"00922 有 {len(dropped)} 檔成分股解析失敗: {dropped[:5]}")
            if comp:
                res_data = {
                    "PCF": {"estdvalue": estdvalue, "baseunit": basket_unit, "is_total_fund": False},
                    "InKind": {"FundComposition": comp}
                }
                pcf_cache[ticker] = {"date": today_str, "fresh": True, "data": res_data}
                return res_data
        except Exception as e:
            print(f"00922 Cathay API Scraper Error: {e}")
            pass

    # For any failures, try loading static JSON
    path = os.path.join(BASE_DIR, f"{ticker}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return annotate_pcf(json.load(f), 'local-backup')
        except Exception as e:
            return {"error": f"Failed to read {ticker}.json: {str(e)}"}
    
    return {"error": f"Local PCF file {ticker}.json not found and no scraper available. Please create {ticker}.json manually."}


def _num(v, default=0.0):
    """把各家 API 的數值欄位轉成 float。

    它們可能是 '1,234,567'、'-'、''、None，甚至已經是數字（此時 .replace 會爆）。
    以前各處直接 `float(x.replace(",", ""))` 包在一個大 try 裡，
    一個欄位壞掉會連帶把同一個 try 內的其他欄位一起打回預設值。
    """
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "")
    if t in ("", "-", "--", "N/A"):
        return default
    try:
        return float(t)
    except ValueError:
        return default


def _mis_num(v):
    """MIS 的數值欄位可能是 ''、'-' 或 '12.34'。"""
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_twse_mis_quotes(symbols):
    """用證交所 MIS 補富邦 SDK 抓不到的個股報價（同步函式，請丟 executor 跑）。

    不知道個股是上市還是上櫃，所以 tse_ 與 otc_ 兩個前綴都問，
    存在的那個才會出現在 msgArray 裡。
    回傳 { symbol: {"price": float, "prev": float} }。
    """
    stock_syms = [s for s in symbols if re.fullmatch(r"\d{4,6}[A-Z]?", s or "")]
    if not stock_syms:
        return {}

    out = {}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
    }
    BATCH = 25          # 一次問太多會被打回票（tse+otc 等於兩倍通道數）
    for i in range(0, len(stock_syms), BATCH):
        chunk = stock_syms[i:i + BATCH]
        ch = "|".join([f"tse_{s}.tw" for s in chunk] + [f"otc_{s}.tw" for s in chunk])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ch}&json=1&delay=0"
        try:
            res = requests.get(url, headers=headers, timeout=8)
            payload = res.json()
        except Exception:
            continue
        for item in (payload.get("msgArray") or []):
            code = str(item.get("c") or "").strip()
            if not code or code in out:
                continue
            prev = _mis_num(item.get("y"))          # y = 昨收
            price = _mis_num(item.get("z"))         # z = 最新成交價（盤前/無成交會是 '-'）
            if price is None:
                # 沒有成交價就退而求其次：開盤價 → 最佳買價 → 昨收
                price = _mis_num(item.get("o"))
            if price is None:
                bid = str(item.get("b") or "").split("_")[0]
                price = _mis_num(bid)
            if price is None:
                price = prev
            if prev and prev > 0 and price and price > 0:
                out[code] = {"price": price, "prev": prev}
    return out


@app.get("/api/stock-quotes")
async def get_stock_quotes(symbols: str):
    """
    Fetch latest prices and previous closes for a comma-separated list of stock symbols.
    Returns: { "symbol": {"price": float, "prev": float}, ... }

    呼叫端傳來的順序就是優先順序 —— API 有額度限制，被限流時先掉的一定是最後面那些。
    另外會把 QUOTE_PRIORITY_TICKERS（0050 → 主動型 → 00631L）穩定地提到最前面，
    這樣就算呼叫端沒排序，關鍵標的也不會排在一百檔成分股後面。
    """
    seen = set()
    symbol_list = []
    for s in symbols.split(","):
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            symbol_list.append(s)
    if not symbol_list:
        return {}

    def _rank(sym):
        try:
            return QUOTE_PRIORITY_TICKERS.index(sym)
        except ValueError:
            return len(QUOTE_PRIORITY_TICKERS)

    symbol_list.sort(key=_rank)     # 穩定排序，同級維持呼叫端給的順序

    try:
        ev_loop = asyncio.get_running_loop()

        def get_current_txf_symbol():
            import calendar
            # 結算日的 13:30 換月要用台北時間判斷；UTC 主機上台北 13:30 是 05:30，
            # 會導致結算後仍然回報已到期的合約。
            now = datetime_now_taipei()
            month_letters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L']
            m_idx = now.month - 1
            cal = calendar.monthcalendar(now.year, now.month)
            third_wed = [w[2] for w in cal if w[2] != 0][2]
            if now.day > third_wed or (now.day == third_wed and (now.hour > 13 or (now.hour == 13 and now.minute >= 30))):
                m_idx = (m_idx + 1) % 12
                year = now.year if m_idx > 0 else now.year + 1
            else:
                year = now.year
            return f"TXF{month_letters[m_idx]}{str(year)[-1]}"

        quotes = {}

        if sdk:
            CONT_FUT_ALIAS = {"TXFR1": "TXF", "TXF": "TXF", "TX": "TXF",
                              "MXFR1": "MXF", "TEFR1": "TEF", "TFFR1": "TFF"}

            def fetch_via_sdk(sym):
                try:
                    root = CONT_FUT_ALIAS.get(sym)
                    if root:
                        # 連續月代號 → 換算成當下的近月合約
                        tx_sym = root + get_current_txf_symbol()[3:]
                        q = sdk.marketdata.rest_client.futopt.intraday.quote(symbol=tx_sym)
                        p_val = float(q.get("lastPrice") or q.get("closePrice") or q.get("previousClose") or 0)
                        pr_val = float(q.get("previousClose") or p_val)
                        return (sym, {"price": p_val, "prev": pr_val} if p_val else None)

                    # 具體的期貨合約代號（例如 006208 成分裡的 TXFI6）也要走期貨 client。
                    # 以前只認上面那份別名清單，其他一律送去現貨 client，
                    # 於是期貨腿永遠拿不到價，iNAV 就少掉那一整塊曝險。
                    is_futopt = sym[0].isalpha() and sym != "IX0001"
                    client = (sdk.marketdata.rest_client.futopt if is_futopt
                              else sdk.marketdata.rest_client.stock)
                    q = client.intraday.quote(symbol=sym)
                    price = q.get("lastPrice") or q.get("closePrice") or q.get("previousClose")
                    prev = q.get("previousClose") or price
                    return (sym, {"price": float(price), "prev": float(prev)} if price else None)
                except Exception:
                    return (sym, None)

            # 一次噴 100+ 個 REST 請求會被限流，失敗的個股就沒有昨收，
            # 前端會把它們當成沒漲跌 —— iNAV 因此系統性低估波動。
            # 這裡限制並發並對漏掉的個股補抓一次。
            sem = asyncio.Semaphore(SDK_QUOTE_CONCURRENCY)

            async def bounded(sym):
                async with sem:
                    return await ev_loop.run_in_executor(None, fetch_via_sdk, sym)

            async def sweep(syms):
                res = await asyncio.gather(*[bounded(s) for s in syms], return_exceptions=True)
                for item in res:
                    if not isinstance(item, Exception) and item and item[1]:
                        quotes[item[0]] = item[1]

            await sweep(symbol_list)

            missing = [s for s in symbol_list if s not in quotes]
            if missing:
                await asyncio.sleep(0.3)
                await sweep(missing)

        # 第二來源：富邦 SDK 拿不到的個股，改問證交所 MIS。
        # 少一檔報價 = iNAV 少算它的漲跌，所以值得多繞一圈。
        still_missing = [s for s in symbol_list if s not in quotes]
        if still_missing:
            try:
                ev_loop = asyncio.get_running_loop()
                twse = await ev_loop.run_in_executor(None, fetch_twse_mis_quotes, still_missing)
                if twse:
                    quotes.update(twse)
                    print(f"[QUOTES] 證交所 MIS 補回 {len(twse)} 檔: "
                          f"{','.join(list(twse)[:15])}")
            except Exception as e:
                print(f"[QUOTES] MIS 備援失敗: {e}")

        still_missing = [s for s in symbol_list if s not in quotes]
        if still_missing:
            print(f"[QUOTES] {len(still_missing)}/{len(symbol_list)} 檔仍取不到報價: "
                  f"{','.join(still_missing[:20])}{' …' if len(still_missing) > 20 else ''}")

        return quotes

    except Exception as e:
        print(f"TWSE stock-quotes error: {e}, falling back to Fubon SDK full")
        if not sdk: return {"error": "SDK not initialized and TWSE unavailable"}

        def fetch_one_sdk(sym):
            try:
                is_futopt = sym[0].isalpha() and sym != "IX0001"
                client = sdk.marketdata.rest_client.futopt if is_futopt else sdk.marketdata.rest_client.stock
                q = client.intraday.quote(symbol=sym)
                price = q.get("lastPrice") or q.get("closePrice") or q.get("previousClose")
                prev  = q.get("previousClose") or price
                return (sym, {"price": float(price), "prev": float(prev)} if price else None)
            except Exception:
                return (sym, None)

        ev_loop = asyncio.get_running_loop()
        tasks = [ev_loop.run_in_executor(None, fetch_one_sdk, sym) for sym in symbol_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        quotes = {}
        for item in results:
            if isinstance(item, Exception): continue
            sym, data = item
            if data: quotes[sym] = data
        return quotes

# ─── Disposal / Warning Detection API ──────────────────────────────

@app.get("/api/disposal/disposed")
async def api_disposal_disposed():
    """取得目前處置中的股票清單（TWSE + TPEx）。"""
    try:
        from disposal_checker import fetch_disposed_stocks
        ev_loop = asyncio.get_running_loop()
        result = await ev_loop.run_in_executor(None, fetch_disposed_stocks)
        return result
    except Exception as e:
        print(f"Disposal disposed API error: {e}")
        return {"error": str(e), "disposed": {}}


@app.get("/api/disposal/attention")
async def api_disposal_attention():
    """取得今日注意股清單（TWSE + TPEx），含累計次數與處置預警等級。"""
    try:
        from disposal_checker import fetch_attention_stocks
        ev_loop = asyncio.get_running_loop()
        result = await ev_loop.run_in_executor(None, fetch_attention_stocks)
        return result
    except Exception as e:
        print(f"Disposal attention API error: {e}")
        return {"error": str(e), "attention": {}}


@app.get("/api/disposal/debug_raw")
async def api_disposal_debug_raw():
    """【Debug 用】直接回傳 TWSE/TPEx 的原始 JSON，方便排查欄位結構。"""
    import requests as _req
    result = {}
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.twse.com.tw/zh/announcement/notetrans.html',
        'Accept': 'application/json, text/plain, */*',
    }
    try:
        r = _req.get("https://www.twse.com.tw/rwd/zh/announcement/disposal?response=json", headers=hdrs, timeout=10)
        d = r.json()
        result["twse"] = {
            "stat": d.get("stat"),
            "fields": d.get("fields"),
            "row_count": len(d.get("data") or []),
            "first_row": (d.get("data") or [None])[0],
        }
    except Exception as e:
        result["twse"] = {"error": str(e)}
    try:
        tpex_hdrs = {**hdrs, 'Referer': 'https://www.tpex.org.tw/web/stock/attention/disposal/disposal_query.php?l=zh-tw'}
        r = _req.get("https://www.tpex.org.tw/web/stock/attention/disposal/disposal_result.php?l=zh-tw&o=json", headers=tpex_hdrs, timeout=10)
        d = r.json()
        rows = d.get("aaData") or d.get("data") or []
        result["tpex"] = {
            "keys": list(d.keys()),
            "row_count": len(rows),
            "first_row": rows[0] if rows else None,
        }
    except Exception as e:
        result["tpex"] = {"error": str(e)}
    return result


@app.get("/api/disposal/scan")
async def api_disposal_scan(refresh: bool = False):
    """執行全條款掃描，回傳高風險股票清單。首次呼叫約需 15-30 秒。"""
    try:
        from disposal_checker import check_all_conditions, clear_cache
        if refresh:
            ev_loop = asyncio.get_running_loop()
            await ev_loop.run_in_executor(None, clear_cache)
        ev_loop = asyncio.get_running_loop()
        result = await ev_loop.run_in_executor(None, check_all_conditions)
        return result
    except Exception as e:
        print(f"Disposal scan API error: {e}")
        return {"error": str(e)}


@app.get("/api/disposal/stock/{stock_id}")
async def api_disposal_stock(stock_id: str):
    """查詢個股警示狀態（首次呼叫會觸發全市場掃描）。"""
    try:
        from disposal_checker import check_single_stock
        ev_loop = asyncio.get_running_loop()
        result = await ev_loop.run_in_executor(None, check_single_stock, stock_id)
        if result is None:
            return {"error": f"查無股票 {stock_id}"}
        return result
    except Exception as e:
        print(f"Disposal stock API error: {e}")
        return {"error": str(e)}


@app.get("/api/momentum-5m")
def get_momentum_5m():
    """
    Calculate the 5-minute momentum for all tracked symbols.
    Combines the historic BUCKETS + CURRENT_BUCKET.
    """
    result = {}
    # Find the oldest price and accumulate volumes
    # Iterate from oldest bucket to newest to find the first valid price
    
    # Combine all historical buckets and current.
    # 這個 endpoint 是同步函式，FastAPI 會丟到 worker thread 跑，跟 SDK 的回呼
    # 執行緒真的同時在動。直接把 CURRENT_BUCKET 這個「活的」dict 掛上去迭代，
    # 開盤爆量時只要有新商品第一次成交插入 key，就會炸
    # RuntimeError: dictionary changed size during iteration（HTTP 500，動能面板空白）。
    # 先做淺拷貝快照再算。
    all_buckets = list(MOMENTUM_BUCKETS) + [dict(CURRENT_BUCKET)]

    # We want to iterate through all symbols that we have data for
    all_symbols = set()
    for b in all_buckets:
        all_symbols.update(list(b.keys()))
        
    for sym in all_symbols:
        oldest_price = None
        total_vol = 0
        total_large = 0
        
        for b in all_buckets:
            if sym in b:
                if oldest_price is None and b[sym]['price'] is not None:
                    oldest_price = b[sym]['price']
                total_vol += b[sym]['vol']
                total_large += b[sym]['large_vol']
                
        current_price = LATEST_QUOTES.get(sym, {}).get("price")
        if current_price is None:
            current_price = oldest_price
            
        if oldest_price and oldest_price > 0:
            pct_change = (current_price - oldest_price) / oldest_price * 100
        else:
            pct_change = 0
            
        result[sym] = {
            "price": current_price,
            "oldest_price": oldest_price,
            "pct_change": pct_change,
            "vol": total_vol,
            "large_vol": total_large
        }
        
    return result


if __name__ == "__main__":
    import uvicorn
    import webbrowser
    from threading import Timer
    
    print("Starting Web Server at http://127.0.0.1:8000")
    Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000/etf0050")).start()
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_excludes=["*.log", "*.pyc", "__pycache__", ".DS_Store", "scratch", "log"],
        ws_ping_interval=20,     # uvicorn sends WS ping every 20s
        ws_ping_timeout=20,      # close if no pong within 20s
    )
