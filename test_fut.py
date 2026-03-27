import os
import time
import asyncio
from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK

load_dotenv('API.env')
sdk = FubonSDK()
sdk.login(os.getenv('ID'), os.getenv('PW'), 'F129483123.pfx', os.getenv('c_pw'))
sdk.init_realtime()

def handle_msg(message):
    print(">>> MESSAGE:", message)

futopt = sdk.marketdata.websocket_client.futopt
futopt.on("message", handle_msg)
futopt.connect()

time.sleep(1)
print("Subscribing to TXF with afterhour: True")
futopt.subscribe({
    "channel": "books",
    "symbol": "TXF",
    "afterhour": True
})
time.sleep(5)
futopt.disconnect()

