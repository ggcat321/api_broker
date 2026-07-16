import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data

try:
    df = data.get('price:收盤價')
    print("taiex in price:", 'IX0001' in df.columns, 'Y9999' in df.columns, 'TWA00' in df.columns)
    
    # 找尋大盤指數
    for col in df.columns:
        if '加權' in col or 'TAIEX' in col or col == 'IX0001':
            print("Found:", col)
except Exception as e:
    print(e)
