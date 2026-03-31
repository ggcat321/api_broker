import os
import time
import json
import asyncio
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
async def websocket_endpoint(websocket: WebSocket, symbols: str, night: bool = None):
    await websocket.accept()
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    for symbol in symbol_list:
        await manager.connect(websocket, symbol)
    
    # Subscribe to books and trades
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
                    target_client.subscribe({
                        "channel": "books",
                        "symbol": symbol,
                        "afterHours": after_hours
                    })
                    target_client.subscribe({
                        "channel": "trades",
                        "symbol": symbol,
                        "afterHours": after_hours
                    })
                else:
                    target_client.subscribe({
                        "channel": "books",
                        "symbol": symbol
                    })
                    target_client.subscribe({
                        "channel": "trades",
                        "symbol": symbol
                    })
            print(f"Subscribed SDK to {len(symbol_list)} symbols")
    except Exception as e:
        print("Subscription error:", e)
        
    try:
        while True:
            # wait for messages (like keepalives or unsubscriptions)
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
                            target_client.unsubscribe({
                                "channel": "books",
                                "symbol": symbol,
                                "afterHours": after_hours
                            })
                            target_client.unsubscribe({
                                "channel": "trades",
                                "symbol": symbol,
                                "afterHours": after_hours
                            })
                        else:
                            target_client.unsubscribe({
                                "channel": "books",
                                "symbol": symbol
                            })
                            target_client.unsubscribe({
                                "channel": "trades",
                                "symbol": symbol
                            })
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
        
        ev_loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_task = ev_loop.run_in_executor(pool, lambda: fut_client.intraday.quote(symbol=futures_symbol, **fut_kwargs))
            if night:
                # Stock market does not have afterHours quote, but we can fetch it. It just returns last close.
                spot_task = ev_loop.run_in_executor(pool, lambda: stock_client.intraday.quote(symbol='IX0001'))
            else:
                spot_task = ev_loop.run_in_executor(pool, lambda: stock_client.intraday.quote(symbol='IX0001'))
                
            futures_quote, spot_quote = await asyncio.gather(fut_task, spot_task, return_exceptions=True)
        
        if isinstance(futures_quote, Exception):
            return {"error": f"Cannot fetch futures: {futures_quote}"}
        
        futures_price = futures_quote.get("lastPrice") or futures_quote.get("closePrice", 0)
        futures_change = futures_quote.get("change", 0)
        futures_change_pct = futures_quote.get("changePercent", 0)
        futures_name = futures_quote.get("name", futures_symbol)
        
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
                if now.hour >= 14:
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
        ev_loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = [
                ev_loop.run_in_executor(pool, fetch_one, ot, st, sy)
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

if __name__ == "__main__":
    import uvicorn
    print("Starting Web Server at http://127.0.0.1:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
