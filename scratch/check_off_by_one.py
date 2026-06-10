import os
import pandas as pd
import numpy as np
import finlab

token = os.environ.get('FINLAB_TOKEN')
finlab.login(api_token=token)

from finlab import data as finlab_data

print("Loading close prices...")
close = finlab_data.get('price:收盤價')
print("Close DataFrame shape:", close.shape)

# Let's pick a few highly active stocks (e.g. 2330 TSMC, 2317 Foxconn)
stocks = ['2330', '2317']

print("\n--- 6-Day Rule comparison ---")
for stock in stocks:
    if stock in close.columns:
        series = close[stock].dropna()
        # Today is index -1
        p_today = series.iloc[-1]
        
        # 5-day return calculation (what current cond_1 does using close.iloc[-6:])
        p_start_5 = series.iloc[-6]
        ret_5 = (p_today / p_start_5 - 1) * 100
        
        # 6-day return calculation (correct)
        p_start_6 = series.iloc[-7]
        ret_6 = (p_today / p_start_6 - 1) * 100
        
        print(f"Stock {stock} (today's price: {p_today}):")
        print(f"  Start price (index -6, 5 days ago): {p_start_5}, calculated 5d return: {ret_5:.2f}%")
        print(f"  Start price (index -7, 6 days ago): {p_start_6}, calculated 6d return: {ret_6:.2f}%")

print("\n--- 30-Day Rule comparison ---")
for stock in stocks:
    if stock in close.columns:
        series = close[stock].dropna()
        p_today = series.iloc[-1]
        
        # 29-day return calculation (what current cond_2 does using close.iloc[-30:])
        p_start_29 = series.iloc[-30]
        ret_29 = (p_today / p_start_29 - 1) * 100
        
        # 30-day return calculation (correct)
        p_start_30 = series.iloc[-31]
        ret_30 = (p_today / p_start_30 - 1) * 100
        
        print(f"Stock {stock} (today's price: {p_today}):")
        print(f"  Start price (index -30, 29 days ago): {p_start_29}, calculated 29d return: {ret_29:.2f}%")
        print(f"  Start price (index -31, 30 days ago): {p_start_30}, calculated 30d return: {ret_30:.2f}%")
