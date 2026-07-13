import pandas as pd
import json
import re

xl = pd.ExcelFile('/Users/jeffrey/Downloads/W_4_My_TW_market_monitor_SEND.xlsx')
df = xl.parse(xl.sheet_names[0])
col = df.columns[0]
sectors = {}
current_sector = None

for val in df[col]:
    if pd.isna(val):
        current_sector = None
        continue
    
    val_str = str(val).strip()
    if not val_str:
        continue
        
    if '(' in val_str and ')' in val_str or not any(x in val_str for x in ['TT', 'US', 'KS', 'JT']):
        clean_name = val_str.split('(')[0].strip()
        current_sector = clean_name
        if current_sector not in sectors:
            sectors[current_sector] = []
    else:
        if current_sector:
            if val_str.endswith(' TT'):
                # Extract only the ticker digits or letters before ' TT'
                ticker = val_str.replace(' TT', '').strip()
                # filter out non-TW formats if necessary, but ' TT' is a good indicator
                sectors[current_sector].append(ticker)

# Remove empty sectors
sectors = {k: v for k, v in sectors.items() if v}

with open('static/sectors.json', 'w', encoding='utf-8') as f:
    json.dump(sectors, f, ensure_ascii=False, indent=2)

print("Saved static/sectors.json")
