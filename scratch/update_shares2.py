import finlab
finlab.login(api_token='cbAVe9AHixA2Cn+k5u/GalfSQDGm2wC2E4TosM4p+1Vqt+bTMBfN9zekW5yXW3zl#vip_m')
from finlab import data
import json
import pandas as pd
import yfinance as yf

try:
    print("Fetching basic info from finlab...")
    basic = data.get('company_basic_info')
    
    print("Fetching price from finlab...")
    price = data.get('price:收盤價')
    latest_price = price.iloc[-1].to_dict()
    
    tse_mcap = 0
    otc_mcap = 0
    
    res = {}
    
    for idx, row in basic.iterrows():
        sym = str(row['stock_id']).strip()
        market = row.get('市場別', '')
        shares = row.get('已發行普通股數或TDR原發行股數', 0)
        
        if pd.isna(shares):
            shares = 0
            
        p = latest_price.get(sym, 0)
        if pd.isna(p):
            p = 0
            
        res[sym] = {
            "shares": float(shares),
            "market": market,
            "price": float(p)
        }
        
    tse_mcap = 0
    otc_mcap = 0
    for sym, info in res.items():
        if len(sym) == 4:
            mcap = info['shares'] * info['price']
            if info['market'] == 'sii':
                tse_mcap += mcap
            elif info['market'] == 'otc':
                otc_mcap += mcap
                
    print("Fetching indices from yfinance...")
    twii = yf.download("^TWII", period="5d")
    two = yf.download("^TWO", period="5d")
    
    taiex_points = float(twii['Close'].iloc[-1].iloc[0] if isinstance(twii['Close'], pd.DataFrame) else twii['Close'].iloc[-1]) if not twii.empty else 24000
    tpex_points = float(two['Close'].iloc[-1].iloc[0] if isinstance(two['Close'], pd.DataFrame) else two['Close'].iloc[-1]) if not two.empty else 280
    
    tse_divisor = tse_mcap / taiex_points if tse_mcap > 0 else 3.28e9
    otc_divisor = otc_mcap / tpex_points if otc_mcap > 0 else 2.3e9
    
    print(f"TAIEX Points (Est): {taiex_points:.2f}, TSE Mcap: {tse_mcap:,.0f}, TSE Divisor: {tse_divisor:,.0f}")
    print(f"TPEX Points (Est): {tpex_points:.2f}, OTC Mcap: {otc_mcap:,.0f}, OTC Divisor: {otc_divisor:,.0f}")
    
    output = {
        "tse_divisor": tse_divisor,
        "otc_divisor": otc_divisor,
        "stocks": res
    }
    
    with open('static/shares.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False)
        
    print("Saved to static/shares.json")
except Exception as e:
    import traceback
    traceback.print_exc()
