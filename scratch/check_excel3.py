import pandas as pd

try:
    df = pd.read_excel("/Users/jeffrey/Downloads/Insti_Margin (1).xlsx", sheet_name="Summary", header=None, skiprows=4, nrows=5)
    
    for col in range(23, min(60, len(df.columns))):
        print(f"Col {col}: {df.iloc[:, col].values}")
except Exception as e:
    print(e)
