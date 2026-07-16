import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data

try:
    info = data.search("指數")
    print("Search '指數':", info)
except Exception as e:
    print(e)
