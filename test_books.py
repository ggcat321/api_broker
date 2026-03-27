import os
import time
import json
from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# Load env
env_path = os.path.join(BASE_DIR, "API.env")
load_dotenv(dotenv_path=env_path)
pfx_path = os.path.join(BASE_DIR, "F129483123.pfx") 

ID = os.getenv("ID")
PW = os.getenv("PW")
CERT_PW = os.getenv("c_pw")

sdk = FubonSDK(300, 3)
accounts = sdk.login(ID, PW, pfx_path, CERT_PW)
print("Login:", accounts)
sdk.init_realtime()

def handle_message(message):
    print("Received msg:", message)

stock = sdk.marketdata.websocket_client.stock
stock.on("message", handle_message)

stock.connect()
time.sleep(1)
stock.subscribe({
    "channel": "books",
    "symbol": "2330"
})
time.sleep(5)
stock.disconnect()
