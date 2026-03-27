import os
import time
import json
import asyncio
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
        await websocket.accept()
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

@app.websocket("/ws/{symbol}")
async def websocket_endpoint(websocket: WebSocket, symbol: str):
    await manager.connect(websocket, symbol)
    
    # Subscribe to books
    try:
        if sdk:
            is_futopt = symbol[0].isalpha()
            target_client = sdk.marketdata.websocket_client.futopt if is_futopt else sdk.marketdata.websocket_client.stock
            sub_req = {
                "channel": "books",
                "symbol": symbol
            }
            if is_futopt:
                from datetime import datetime
                h = datetime.now().hour
                after_hours = (h >= 14 or h < 8)
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
            print(f"Subscribed SDK to books: {symbol}")
    except Exception as e:
        print("Subscription error:", e)
        
    try:
        while True:
            # wait for messages (like keepalives or unsubscriptions)
            await websocket.receive_text()
    except WebSocketDisconnect:
        should_unsubscribe = manager.disconnect(websocket, symbol)
        if should_unsubscribe and sdk:
            try:
                is_futopt = symbol[0].isalpha()
                target_client = sdk.marketdata.websocket_client.futopt if is_futopt else sdk.marketdata.websocket_client.stock
                unsub_req = {
                    "channel": "books",
                    "symbol": symbol
                }
                if is_futopt:
                    from datetime import datetime
                    h = datetime.now().hour
                    after_hours = (h >= 14 or h < 8)
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
                print(f"Unsubscribed SDK from books: {symbol}")
            except Exception:
                pass


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

@app.get("/")
async def get_root():
    with open(os.path.join(BASE_DIR, "static", "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

if __name__ == "__main__":
    import uvicorn
    print("Starting Web Server at http://127.0.0.1:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
