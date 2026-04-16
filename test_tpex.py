import requests
import urllib3
urllib3.disable_warnings()

s = requests.Session()
s.verify = False
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest'
})
# 1. Get cookies
s.get("https://www.tpex.org.tw/web/stock/attention/disposal/disposal_query.php?l=zh-tw")

# 2. Fetch data
url = "https://www.tpex.org.tw/web/stock/attention/disposal/disposal_result.php?l=zh-tw&o=json"
r = s.get(url, headers={'Referer': 'https://www.tpex.org.tw/web/stock/attention/disposal/disposal_query.php?l=zh-tw'})
print(r.status_code)
if r.status_code == 200:
    for row in r.json().get('aaData', []):
        print(row[2], row[3], row[6])
