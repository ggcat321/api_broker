import os
import pandas as pd

excel_path = "/Users/jeffrey/Downloads/集保股權集中TDCC(Buy).xlsx"

if os.path.exists(excel_path):
    df = pd.read_excel(excel_path, sheet_name="Sheet1")
    active = df[df['exit_date'].isnull()]
    print(active[['trade_index', 'symbol', 'entry_date', 'exit_date', 'position', 'return', 'industry', 'trade_price@entry_date', '類別@entry_sig_date']])
