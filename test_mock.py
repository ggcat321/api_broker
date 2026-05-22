import pandas as pd
from datetime import date as date_type

disp_df = pd.DataFrame({
    "股票代號": [2330, 2317],
    "股票名稱": ["台積電", "鴻海"],
    "處置開始日期": ["2026-04-10", "2026-04-15"],
    "處置結束日期": ["2026-04-20", "2026-04-16"]
})

disposed = {}

# 定義可能的欄位名稱
end_cols = ['處置結束日期', 'end_date', 'disposal_end_date']
start_cols = ['處置開始日期', 'start_date', 'disposal_start_date']
id_cols = ['股票代號', 'stock_id', 'symbol', 'code']
name_cols = ['股票名稱', 'name', 'stock_name']
measure_cols = ['處置措施', 'measure', 'status', 'condition']

# 找到實際存在的欄位
end_col = next((c for c in end_cols if c in disp_df.columns), None)
start_col = next((c for c in start_cols if c in disp_df.columns), None)
id_col = next((c for c in id_cols if c in disp_df.columns), None)
name_col = next((c for c in name_cols if c in disp_df.columns), None)
m_col = next((c for c in measure_cols if c in disp_df.columns), None)

if id_col and end_col:
    disp_df['_end_dt'] = pd.to_datetime(disp_df[end_col], errors='coerce')
    today_dt = pd.to_datetime(date_type.today())
    active = disp_df[disp_df['_end_dt'] >= today_dt]
    
    for _, row in active.iterrows():
        sid = str(row[id_col]).strip()
        if sid and sid != "nan":
            if sid not in disposed:
                start = str(row[start_col]).strip() if start_col else ''
                end   = str(row[end_col]).strip()
                
                measures = ""
                if m_col:
                    m_val = str(row[m_col]).strip()
                    if m_val and m_val != "nan":
                        measures = m_val[:120]
                        
                name_val = str(row[name_col]).strip() if name_col else sid
                if name_val == "nan": name_val = sid
                
                disposed[sid] = {
                    "name": name_val,
                    "period": f"{start} ~ {end}",
                    "measures": measures,
                    "source": "FinLab"
                }

import json
print(json.dumps(disposed, ensure_ascii=False))
