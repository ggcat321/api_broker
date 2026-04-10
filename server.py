import os
import time
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import requests
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

app = FastAPI()

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}
        self.message_queue = asyncio.Queue()

    async def connect(self, websocket: WebSocket, symbol: str):
        if symbol not in self.active_connections:
            self.active_connections[symbol] = []
        self.active_connections[symbol].append(websocket)

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

def handle_fubon_message(message):
    try:
        msg = json.loads(message)
        event = msg.get("event")
        data = msg.get("data")
        channel = msg.get("channel")
        
        # Debug: log channel and top-level keys for every data message
        if event == "data" and data:
            data_keys = list(data.keys()) if isinstance(data, dict) else f"(type={type(data).__name__})"
            print(f"[DEBUG] channel={channel}, event={event}, data_keys={data_keys}")
            if channel == "trades":
                print(f"[DEBUG-TRADES] Full data: {json.dumps(data, ensure_ascii=False)[:300]}")
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

@app.on_event("startup")
async def startup_event():
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
        
    asyncio.create_task(message_processor())
    asyncio.create_task(vix_scraper())

async def vix_scraper():
    print("Started VIX scraper background task")
    while True:
        try:
            # Use run_in_executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, lambda: requests.post(
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

@app.on_event("shutdown")
def shutdown_event():
    if sdk:
        try:
            sdk.marketdata.websocket_client.stock.disconnect()
            sdk.marketdata.websocket_client.futopt.disconnect()
        except Exception:
            pass

@app.websocket("/ws/{symbols}")
async def websocket_endpoint(websocket: WebSocket, symbols: str, night: bool = None, trades_only: bool = False):
    await websocket.accept()
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    for symbol in symbol_list:
        await manager.connect(websocket, symbol)
    
    # Subscribe to books and/or trades
    try:
        if sdk:
            from datetime import datetime
            if night is not None:
                after_hours = night
            else:
                h = datetime.now().hour
                after_hours = (h >= 14 or h < 8)
            
            for symbol in symbol_list:
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
            print(f"Subscribed SDK to {len(symbol_list)} symbols [{mode}]")
    except Exception as e:
        print("Subscription error:", e)
        
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
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
        is_futopt = symbol[0].isalpha()
        client = sdk.marketdata.rest_client.futopt if is_futopt else sdk.marketdata.rest_client.stock
        res = client.intraday.quote(symbol=symbol)
        return res
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/options-chain/{futures_symbol}")
async def get_options_chain(futures_symbol: str, strikes: int = 15, interval: int = 100, weekly: bool = True, night: bool = False):
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
            year = 2020 + int(year_code)
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

@app.get("/")
async def get_root():
    with open(os.path.join(BASE_DIR, "static", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/options")
async def get_options():
    with open(os.path.join(BASE_DIR, "static", "options.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/etf0050")
async def get_etf0050():
    with open(os.path.join(BASE_DIR, "static", "etf0050.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/etf-pcf/{ticker}")
async def get_etf_pcf(ticker: str):
    """Proxy endpoint to fetch PCF data for different ETFs."""
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
                            # Use total constituent shares as qty
                            qty = float(cols[2].text.strip().replace(",", ""))
                            comp.append({"stkcd": stkcd, "name": name, "qty": qty})
                        except:
                            pass
            return {
                "PCF": {"estdvalue": 0, "baseunit": 1, "is_total_fund": True},
                "InKind": {"FundComposition": comp}
            }
        except Exception as e:
            # Fallback to local json if scraper fails
            pass

    elif ticker == "00922":
        # Scrape Cathay API
        try:
            # Construct yesterday's date or today's date
            from datetime import datetime
            today_str = datetime.now().strftime("%Y-%m-%d")
            url = f"https://cwapi.cathaysite.com.tw/api/ETF/GetETFDetailStockList?FundCode=DO&SearchDate={today_str}"
            h = {
                "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1laWQiOiI0MzI1ODU2IiwidW5pcXVlX25hbWUiOiIiLCJyb2xlIjoiMCIsIkVDSUQiOiIwIiwiU2Vzc2lvbklkIjoiIiwibmJmIjoxNzc1NzIzMzcxLCJleHAiOjE4MzU3MjMzMTEsImlhdCI6MTc3NTcyMzM3MX0.ZpnvUeyN5mmjWZ8lrSRaOEirbr0pu4N3YEmI3wbCZlg",
                "Origin": "https://www.cathaysite.com.tw",
                "Referer": "https://www.cathaysite.com.tw/",
                "User-Agent": "Mozilla/5.0"
            }
            ev_loop = asyncio.get_running_loop()
            res = await ev_loop.run_in_executor(None, lambda: requests.get(url, headers=h, timeout=10))
            data = res.json()
            comp = []
            for item in data.get("result", []):
                stkcd = item.get("stockCode")
                name = item.get("stockName")
                try:
                    qty = float(item.get("volumn", "0").replace(",", ""))
                    if stkcd and name:
                        comp.append({"stkcd": stkcd, "name": name, "qty": qty})
                except:
                    pass
            if comp:
                return {
                    "PCF": {"estdvalue": 0, "baseunit": 1, "is_total_fund": True},
                    "InKind": {"FundComposition": comp}
                }
        except Exception as e:
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

        def fetch_twse_all():
            r = requests.get(
                "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                headers={"Accept": "application/json"},
                timeout=10
            )
            r.raise_for_status()
            data = {}
            for item in r.json():
                try:
                    code = item["Code"]
                    close_str = item.get("ClosingPrice", "").replace(",", "")
                    change_str = item.get("Change", "").replace(",", "")
                    if close_str and close_str not in ("", "--"):
                        price = float(close_str)
                        # PrevClose = Close - Change
                        change = float(change_str) if change_str and change_str not in ("", "--") else 0
                        data[code] = {"price": price, "prev": (price - change)}
                except (ValueError, KeyError):
                    continue
            return data

        price_map = await ev_loop.run_in_executor(None, fetch_twse_all)

        quotes = {}
        missing = []
        for sym in symbol_list:
            if sym in price_map:
                quotes[sym] = price_map[sym]
            else:
                missing.append(sym)

        # Fallback to Fubon SDK
        if missing and sdk:
            def fetch_one(sym):
                try:
                    q = sdk.marketdata.rest_client.stock.intraday.quote(symbol=sym)
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
                q = sdk.marketdata.rest_client.stock.intraday.quote(symbol=sym)
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

if __name__ == "__main__":
    import uvicorn
    print("Starting Web Server at http://127.0.0.1:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
