import finlab
from finlab import data
import json
import math
import pandas as pd

try:
    # 登入，假設已經有設定或者不需要 token
    # finlab.login(api_token="...") 
    
    # 拿取市值 (通常是 price * 股本，或者 finlab 有現成的)
    # 這裡可以用 data.get('company_basic_info:實收資本額') / 10 * data.get('price:收盤價') 或者是...
    # 看有沒有現成的 'financial_statement:市值' 或 'fundamental_features:市值'
    
    close = data.get('price:收盤價')
    mcap = data.get('price:收盤價') * data.get('company_basic_info:已發行普通股數').fillna(method='ffill') 
    
    print("Latest close date:", close.index[-1])
    print("Mcap shape:", mcap.shape if mcap is not None else "None")
    
except Exception as e:
    print("Error:", e)
