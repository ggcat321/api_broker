import os
import sys
from datetime import date
from dotenv import load_dotenv

load_dotenv('API.env')
finlab_token = os.getenv("FINLAB_TOKEN")
if not finlab_token:
    print("FINLAB_TOKEN not found in API.env")
    sys.exit(1)

try:
    import finlab
    from finlab import data
    finlab.login(finlab_token)
    df = data.get('disposal_information')
    if df is not None and not df.empty:
        print("FinLab Disposal Data found!")
        print("Columns:", df.columns)
        
        today_str = date.today().strftime("%Y-%m-%d")
        if '處置結束日期' in df.columns:
            active = df[df['處置結束日期'] >= today_str]
            print(f"Active disposals matching {today_str}: {len(active)}")
            if len(active) > 0:
                print(active[['stock_id', '處置開始日期', '處置結束日期']].head())
        else:
            print("No '處置結束日期' column!")
            print(df.tail())
    else:
         print("No data returned or empty.")
except Exception as e:
    print("Error:", e)
