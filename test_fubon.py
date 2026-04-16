import asyncio
from fubon_neo.sdk import FubonSDK
import os
from dotenv import load_dotenv

load_dotenv('API.env')
sdk = FubonSDK()
res = sdk.login(os.environ.get("ID"), os.environ.get("PW"), os.environ.get("c_pw"))
if res.is_success:
    q = sdk.marketdata.rest_client.stock.intraday.quote(symbol="6147")
    print(q)
