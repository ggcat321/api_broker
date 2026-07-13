import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data
df = data.get('price:收盤價')
print("price columns:", df.columns[:5])
