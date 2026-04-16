import urllib.request
import json
try:
    req = urllib.request.Request("https://openapi.twse.com.tw/v1/exchangeReport/TWT44U", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        print("TWSE OpenAI data length:", len(data))
        if data:
            for row in data:
                if '6147' in str(row) or '2330' in str(row):
                    print("Found in TWSE:", row)
except Exception as e:
    print("TWSE error:", e)

# Let's try Fugle or Yahoo or TPEx
try:
    req = urllib.request.Request("https://www.tpex.org.tw/openapi/v1/mktdata/tpex_mainboard_quotes", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        print("TPEx Quotes data length:", len(data))
except Exception as e:
    print("TPEx Quotes error:", e)

