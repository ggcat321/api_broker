import os
import pandas as pd
import numpy as np
import finlab
from dotenv import load_dotenv

# Load env variables
load_dotenv('API.env')
token = os.environ.get('FINLAB_TOKEN')
finlab.login(api_token=token)

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import disposal_checker

# Load close and company data
close = disposal_checker.load(disposal_checker.F_CLOSE)
company = disposal_checker.load(disposal_checker.F_COMPANY)

stocks = ['2330', '2317']

print("\n--- Verifying disposal_checker._calc_ret ---")
for stock in stocks:
    if stock in close.columns:
        series = close[stock].dropna()
        p_today = series.iloc[-1]
        
        # 6d return
        ret_6d_calc = disposal_checker._calc_ret(close, stock, 6)
        # 6d return manually: (P_today / P_index_-7 - 1) * 100
        ret_6d_manual = round(float((p_today / series.iloc[-7] - 1) * 100), 2)
        
        # 30d return
        ret_30d_calc = disposal_checker._calc_ret(close, stock, 30)
        # 30d return manually: (P_today / P_index_-31 - 1) * 100
        ret_30d_manual = round(float((p_today / series.iloc[-31] - 1) * 100), 2)
        
        print(f"Stock {stock}:")
        print(f"  6d calc: {ret_6d_calc}%, manual: {ret_6d_manual}% (Match: {ret_6d_calc == ret_6d_manual})")
        print(f"  30d calc: {ret_30d_calc}%, manual: {ret_30d_manual}% (Match: {ret_30d_calc == ret_30d_manual})")

print("\n--- Verifying tomorrow trigger calculations ---")
for stock in stocks:
    if stock in close.columns:
        trigger_info = disposal_checker._calc_tomorrow_trigger_info(stock, close, company)
        if trigger_info:
            p_today = trigger_info["p_today"]
            p_start_6 = trigger_info["p_start_6"]
            p_start_30 = trigger_info["p_start_30"]
            
            # Manual reference start price checks
            series = close[stock].dropna()
            manual_start_6 = float(series.iloc[-6])
            manual_start_30 = float(series.iloc[-30])
            
            print(f"Stock {stock} (today close {p_today}):")
            print(f"  p_start_6 (start of tomorrow's 6d period): {p_start_6}, manual: {manual_start_6} (Match: {p_start_6 == manual_start_6})")
            print(f"  p_start_30 (start of tomorrow's 30d period): {p_start_30}, manual: {manual_start_30} (Match: {p_start_30 == manual_start_30})")
            print(f"  Tomorrow trigger up: {trigger_info['trigger_up']} (up {trigger_info['up_pct']}% from today)")
