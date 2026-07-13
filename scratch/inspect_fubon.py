import asyncio
import json
import os
from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK

load_dotenv("API.env")
cert = [f for f in os.listdir('.') if f.endswith('.pfx')][0]
print(f"Using cert: {cert}")

sdk = FubonSDK()
ID = os.getenv("ID")
PW = os.getenv("PW")
CERT_PW = os.getenv("c_pw")
sdk.login(ID, PW, cert, CERT_PW)
sdk.init_realtime()

def handle_msg(msg):
    parsed = json.loads(msg)
    if parsed.get('event') == 'data':
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        os._exit(0)

sdk.marketdata.websocket_client.stock.on("message", handle_msg)
sdk.marketdata.websocket_client.stock.connect()
sdk.marketdata.websocket_client.stock.subscribe({'channel': 'trades', 'symbol': '2330'})

asyncio.get_event_loop().run_forever()
