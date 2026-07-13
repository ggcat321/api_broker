import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data
df = data.get('company_basic_info')
print("company_basic_info columns:", df.columns if df is not None else "Not Found")
# Also try 'security_categories'
try:
    df2 = data.get('security_categories')
    print("security_categories:", df2.columns)
except:
    pass

try:
    mcap = data.get('price:總市值')
    if mcap is not None:
        print("mcap index:", mcap.index[-1])
        print("mcap 2330:", mcap['2330'].iloc[-1])
except Exception as e:
    print(e)
