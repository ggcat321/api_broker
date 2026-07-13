import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data
import json

df = data.get('company_basic_info')
res = {}
for idx, row in df.iterrows():
    sym = str(row['stock_id'])
    shares = row.get('已發行普通股數或TDR原發行股數', 0)
    if not isinstance(shares, (int, float)) or str(shares).lower() == 'nan':
        shares = 0
    res[sym] = float(shares)

print(f"Got {len(res)} companies.")
print("2330 shares:", res.get('2330'))

with open('static/shares.json', 'w', encoding='utf-8') as f:
    json.dump(res, f)

print("Saved static/shares.json")
