import requests
url = 'https://www.tpex.org.tw/www/zh-tw/announce/market/disposal.json'
hdrs = {'User-Agent': 'Mozilla/5.0 (compatible; Python/requests)'}
r = requests.get(url, headers=hdrs, timeout=10, verify=False)
print(r.status_code)
print(r.text[:200])
