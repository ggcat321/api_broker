import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data

try:
    # 取得上市指數
    taiex = data.get('benchmark_return:發行量加權股價報酬指數')
    print("taiex columns:", taiex.columns)
except Exception as e:
    print(e)
    
try:
    df = data.get('price:收盤價')
    print("price index columns:", df.columns[:5])
except Exception as e:
    print(e)
