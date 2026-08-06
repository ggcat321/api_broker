import os
import time
import json
import asyncio
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
        self.message_queue = asyncio.Queue()

    async def connect(self, websocket: WebSocket, symbol: str):
        is_first = False
        if symbol not in self.active_connections:
            self.active_connections[symbol] = []
            is_first = True
        self.active_connections[symbol].append(websocket)
        return is_first

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
            print(f"[DEBUG] Error event: {msg}")
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(manager.message_queue.put(msg), loop)
    except Exception as e:
        print("Error parsing msg:", e)

# ─── Fubon SDK Watchdog (auto-reconnect with backoff) ──────────────
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
            # If no message received in 90 seconds during trading hours, reconnect
            from datetime import datetime
            h = datetime.now().hour
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
                    print("✅ Fubon SDK reconnected successfully")
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
async def websocket_endpoint(websocket: WebSocket, symbols: str, night: bool = None, trades_only: bool = False):
    await websocket.accept()
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    
    symbols_to_subscribe = []
    already_subscribed = []
    for symbol in symbol_list:
        is_first = await manager.connect(websocket, symbol)
        if is_first:
            symbols_to_subscribe.append(symbol)
        else:
            already_subscribed.append(symbol)
    
    # Notify client immediately for symbols already subscribed by another session
    for symbol in already_subscribed:
        try:
            await websocket.send_json({"event": "subscribed", "data": {"symbol": symbol}})
        except Exception:
            pass
    
    # Subscribe to books and/or trades
    try:
        if sdk and symbols_to_subscribe:
            from datetime import datetime
            if night is not None:
                after_hours = night
            else:
                h = datetime.now().hour
                after_hours = (h >= 14 or h < 8)
            
            for symbol in symbols_to_subscribe:
                is_futopt = symbol[0].isalpha() and symbol != "IX0001"
                target_client = sdk.marketdata.websocket_client.futopt if is_futopt else sdk.marketdata.websocket_client.stock
                if is_futopt:
                    if not trades_only:
                        target_client.subscribe({"channel": "books", "symbol": symbol, "afterHours": after_hours})
                    target_client.subscribe({"channel": "trades", "symbol": symbol, "afterHours": after_hours})
                else:
                    if not trades_only:
                        target_client.subscribe({"channel": "books", "symbol": symbol})
                    target_client.subscribe({"channel": "trades", "symbol": symbol})
            mode = "trades-only" if trades_only else "books+trades"
            print(f"Subscribed SDK to {len(symbols_to_subscribe)} new symbols [{mode}]")
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
    except (WebSocketDisconnect, Exception):
        if sdk:
            from datetime import datetime
            if night is not None:
                after_hours = night
            else:
                h = datetime.now().hour
                after_hours = (h >= 14 or h < 8)
            
            for symbol in symbol_list:
                should_unsubscribe = manager.disconnect(websocket, symbol)
                if should_unsubscribe:
                    try:
                        is_futopt = symbol[0].isalpha() and symbol != "IX0001"
                        target_client = sdk.marketdata.websocket_client.futopt if is_futopt else sdk.marketdata.websocket_client.stock
                        if is_futopt:
                            if not trades_only:
                                target_client.unsubscribe({"channel": "books", "symbol": symbol, "afterHours": after_hours})
                            target_client.unsubscribe({"channel": "trades", "symbol": symbol, "afterHours": after_hours})
                        else:
                            if not trades_only:
                                target_client.unsubscribe({"channel": "books", "symbol": symbol})
                            target_client.unsubscribe({"channel": "trades", "symbol": symbol})
                        print(f"Unsubscribed SDK from: {symbol}")
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

async def fetch_ezmoney_pcf(ticker: str):
    """Scrape PCF and Asset holdings for Active ETFs 00981A & 00403A from ezmoney using Playwright."""
    code_map = {'00981A': '49YTW', '00403A': '63YTW'}
    fund_code = code_map.get(ticker, ticker)
    local_json_path = os.path.join(BASE_DIR, f"{ticker}.json")
    
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # Block tracking/analytics domains to prevent timeout
            await page.route('**/*', lambda route: route.abort() if any(d in route.request.url for d in ['google', 'doubleclick', 'gtag', 'analytics']) else route.continue_())
            
            raw_data = {}
            async def handle_response(response):
                if 'GetPCF' in response.url or 'getpcf' in response.url.lower():
                    nonlocal raw_data
                    try: raw_data = await response.json()
                    except: pass
            page.on('response', handle_response)
            
            await page.goto(f'https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode={fund_code}', wait_until='domcontentloaded', timeout=15000)
            
            # Poll for raw_data up to 8 seconds
            for _ in range(16):
                if raw_data: break
                await asyncio.sleep(0.5)
                
            await browser.close()
            
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
                if pcf_list and isinstance(pcf_list[0], dict):
                    tran_date = str(pcf_list[0].get('TranDate') or pcf_list[0].get('PostDate') or '')
                if not tran_date:
                    tran_date = str(fund_info.get('FundDate') or '')

                res_data = {
                    'PCF': {
                        'nav': nav,
                        'p_unit': p_unit,
                        'nav_total': nav_total,
                        'out_unit': out_unit,
                        'baseunit': baseunit,
                        'estdvalue': cash_diff,
                        'trandate': tran_date,
                        'is_total_fund': False
                    },
                    'InKind': {
                        'FundComposition': comp
                    },
                    'Futures': futures,
                    'FundName': str(fund_info.get('sFundName') or ticker),
                    'StockNo': str(fund_info.get('sStockNo') or ticker)
                }
                
                # Save local JSON backup
                try:
                    with open(local_json_path, 'w', encoding='utf-8') as f:
                        json.dump(res_data, f, ensure_ascii=False, indent=2)
                except Exception as save_err:
                    print(f"Warning: Could not save local backup for {ticker}: {save_err}")

                return res_data

    except Exception as e:
        print(f"Ezmoney scraper error for {ticker}: {e}")

    # Fallback: Read local JSON backup if available
    if os.path.exists(local_json_path):
        try:
            print(f"Loading local fallback PCF for {ticker} from {local_json_path}")
            with open(local_json_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as f_err:
            print(f"Failed to read local fallback for {ticker}: {f_err}")

    return {"error": f"Failed to receive GetPCF response for {ticker} from ezmoney"}

@app.get("/api/etf-pcf/{ticker}")
async def get_etf_pcf(ticker: str):
    """Proxy endpoint to fetch PCF data for different ETFs."""
    from datetime import datetime
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    if ticker in pcf_cache and pcf_cache[ticker].get("date") == today_str:
        return pcf_cache[ticker]["data"]

    if ticker in ["00981A", "00403A"]:
        res_data = await fetch_ezmoney_pcf(ticker)
        if res_data and "error" not in res_data:
            pcf_cache[ticker] = {"date": today_str, "data": res_data}
        return res_data
        
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
            out_unit = float(pcf.get('osunit') or 1)
            baseunit = float(pcf.get('baseunit') or 500000)
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
                pcf_cache[ticker] = {"date": today_str, "data": res_data}
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
            if table:
                for tr in table.find_all("tr")[1:]:
                    cols = tr.find_all("td")
                    if len(cols) >= 5:
                        stkcd = cols[0].text.strip()
                        name = cols[1].text.strip()
                        if not stkcd: continue
                        try:
                            qty = float(cols[2].text.strip().replace(",", ""))
                            comp.append({"stkcd": stkcd, "name": name, "qty": qty})
                        except:
                            pass
            
            # Scrape futures for 006208
            if tables:
                tx_qty = 0
                futures_symbol = "TXFR1"
                futures_name = "台指期"
                for tr in tables[0].find_all("tr"):
                    cols = [c.text.strip() for c in tr.find_all("td")]
                    if len(cols) >= 5 and "期貨" in cols[1]:
                        import re
                        m = re.search(r"(\d{4})/(\d{2})", cols[1])
                        if m:
                            y = m.group(1)[-1]
                            mo = int(m.group(2))
                            month_letter = chr(ord("A") + mo - 1)
                            futures_symbol = f"TXF{month_letter}{y}"
                            futures_name = f"{m.group(1)}/{m.group(2)} 台指期"
                        try:
                            tx_qty += float(cols[2].replace(",", ""))
                        except:
                            pass
                if tx_qty > 0:
                    comp.append({"stkcd": futures_symbol, "name": futures_name, "qty": tx_qty * 200})

            # Extract units, nav, cash
            units = 1
            nav_val = 0
            for d in soup.find_all(["div", "span", "td"]):
                if "基金在外流通單位數(單位)" in d.text:
                    try:
                        parts = d.text.split("基金在外流通單位數(單位)")
                        units = float(parts[1].split()[0].replace(",", ""))
                        nav_val = float(parts[1].split("基金每單位淨值(新台幣)")[1].strip().split()[0].replace(",", ""))
                    except: pass

            res_data = {
                "PCF": {"estdvalue": 0, "baseunit": units or 1, "is_total_fund": False, "nav": nav_val},
                "InKind": {"FundComposition": comp}
            }
            pcf_cache[ticker] = {"date": today_str, "data": res_data}
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
            try:
                estdvalue = float(meta.get("bm", "0").replace(",", ""))
                basket_unit = float(meta.get("basketUnit", "500000").replace(",", ""))
            except:
                estdvalue = 0
                basket_unit = 500000

            comp = []
            for item in data.get("result", []):
                stkcd = item.get("prod")
                name = item.get("prodName")
                try:
                    qty = float(item.get("basketShares", "0").replace(",", ""))
                    if stkcd and name:
                        comp.append({"stkcd": stkcd, "name": name, "qty": qty})
                except:
                    pass
            if comp:
                res_data = {
                    "PCF": {"estdvalue": estdvalue, "baseunit": basket_unit, "is_total_fund": False},
                    "InKind": {"FundComposition": comp}
                }
                pcf_cache[ticker] = {"date": today_str, "data": res_data}
                return res_data
        except Exception as e:
            print(f"00922 Cathay API Scraper Error: {e}")
            pass

    # For any failures, try loading static JSON
    path = os.path.join(BASE_DIR, f"{ticker}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"error": f"Failed to read {ticker}.json: {str(e)}"}
    
    return {"error": f"Local PCF file {ticker}.json not found and no scraper available. Please create {ticker}.json manually."}


@app.get("/api/stock-quotes")
async def get_stock_quotes(symbols: str):
    """
    Fetch latest prices and previous closes for a comma-separated list of stock symbols.
    Returns: { "symbol": {"price": float, "prev": float}, ... }
    """
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        return {}

    try:
        ev_loop = asyncio.get_running_loop()
        
        # 1. Fetch the main ETFs instantly via Fubon SDK for live accuracy
        etfs_to_fetch = [s for s in symbol_list if s in ["0050", "006208", "00922", "00981A", "00403A", "00631L"]]
        remaining_symbols = [s for s in symbol_list if s not in etfs_to_fetch]
        
        quotes = {}
        if etfs_to_fetch and sdk:
            def fetch_etf(sym):
                try:
                    q = sdk.marketdata.rest_client.stock.intraday.quote(symbol=sym)
                    price = q.get("lastPrice") or q.get("closePrice") or q.get("previousClose")
                    prev  = q.get("previousClose") or price
                    return (sym, {"price": float(price), "prev": float(prev)} if price else None)
                except Exception:
                    return (sym, None)
            
            tasks = [ev_loop.run_in_executor(None, fetch_etf, sym) for sym in etfs_to_fetch]
            res = await asyncio.gather(*tasks, return_exceptions=True)
            for item in res:
                if not isinstance(item, Exception) and item[1]:
                    quotes[item[0]] = item[1]

        def fetch_twse_all():
            from datetime import datetime
            r = requests.get(
                "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                headers={"Accept": "application/json"},
                timeout=10,
                verify=False
            )
            r.raise_for_status()
            
            now_dt = datetime.now()
            today_minguo = f"{now_dt.year - 1911}{now_dt.strftime('%m%d')}"
            
            data = {}
            for item in r.json():
                try:
                    code = item["Code"]
                    close_str = item.get("ClosingPrice", "").replace(",", "")
                    change_str = item.get("Change", "").replace(",", "")
                    item_date = item.get("Date", "")
                    
                    if close_str and close_str not in ("", "--"):
                        price = float(close_str)
                        change = float(change_str) if change_str and change_str not in ("", "--") else 0
                        
                        if item_date == today_minguo:
                            # EOD snapshot for today -> prev is price - change
                            data[code] = {"price": price, "prev": (price - change)}
                        else:
                            # Snapshot from a previous session -> prev is exactly price
                            data[code] = {"price": price, "prev": price}
                except (ValueError, KeyError):
                    continue
            return data

        price_map = await ev_loop.run_in_executor(None, fetch_twse_all)

        missing = []
        for sym in remaining_symbols:
            if sym in price_map:
                quotes[sym] = price_map[sym]
            else:
                missing.append(sym)

        # Fallback to Fubon SDK
        if missing and sdk:
            def get_current_txf_symbol():
                from datetime import datetime
                import calendar
                now = datetime.now()
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

            def fetch_one(sym):
                try:
                    is_futopt = (sym[0].isalpha() and sym != "IX0001") or sym.startswith("TX")
                    client = sdk.marketdata.rest_client.futopt if is_futopt else sdk.marketdata.rest_client.stock
                    target_sym = get_current_txf_symbol() if sym in ["TXFR1", "TXF", "TX"] else sym
                    q = client.intraday.quote(symbol=target_sym)
                    price = q.get("lastPrice") or q.get("closePrice") or q.get("previousClose")
                    prev  = q.get("previousClose") or price
                    return (sym, {"price": float(price), "prev": float(prev)} if price else None)
                except Exception:
                    return (sym, None)

            tasks = [ev_loop.run_in_executor(None, fetch_one, sym) for sym in missing]
            fallback_results = await asyncio.gather(*tasks, return_exceptions=True)

            for item in fallback_results:
                if isinstance(item, Exception): continue
                sym, data = item
                if data: quotes[sym] = data

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
    
    # Combine all historical buckets and current
    all_buckets = list(MOMENTUM_BUCKETS) + [CURRENT_BUCKET]
    
    # We want to iterate through all symbols that we have data for
    all_symbols = set()
    for b in all_buckets:
        all_symbols.update(b.keys())
        
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
