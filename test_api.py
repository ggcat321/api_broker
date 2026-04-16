import urllib.request
import json

try:
    url = "https://www.twse.com.tw/rwd/zh/announcement/disposal?response=json"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode('utf-8'))
        print("TWSE sample:")
        for row in data.get('data', [])[:3]:
            # print column 6 (which is period)
            print(row[2], row[3], row[6] if len(row)>6 else "N/A")
except Exception as e:
    print("TWSE error:", e)

try:
    url2 = "https://www.tpex.org.tw/web/stock/attention/disposal/disposal_result.php?l=zh-tw&o=json"
    req2 = urllib.request.Request(url2, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req2) as response:
        data2 = json.loads(response.read().decode('utf-8'))
        rows = data2.get("aaData") or data2.get("data") or []
        print("\nTPEx sample:")
        for row in rows[:3]:
            # print column 6 (which is period)
            print(row[2], row[3], row[6] if len(row)>6 else "N/A")
except Exception as e:
    print("TPEx error:", e)
