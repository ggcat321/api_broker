import os
from dotenv import load_dotenv
load_dotenv('API.env')
token = os.environ.get('FINLAB_TOKEN')

import sys
sys.path.append(os.getcwd())

from finlab import data
data.login(token)
try:
    df = data.get('disposal_information')
    print("DataFrame shape:", df.shape)
    print("Columns:", df.columns.tolist())
    df = df.reset_index()
    print("Columns after reset:", df.columns.tolist())
    
    end_cols = ['處置結束日期', 'end_date', 'disposal_end_date']
    end_col = next((c for c in end_cols if c in df.columns), None)
    
    print("End col:", end_col)
    
    if end_col:
        print("Sample raw dates:", df[end_col].dropna().tail().tolist())
except Exception as e:
    print("Error:", e)
