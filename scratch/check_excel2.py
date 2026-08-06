import pandas as pd

try:
    df = pd.read_excel("/Users/jeffrey/Downloads/Insti_Margin (1).xlsx", sheet_name="Summary", header=None, skiprows=4, nrows=5)
    
    subset = df.iloc[:, 23:39]
    for col in subset.columns:
        print(f"Col {col}: {subset[col].values}")
except Exception as e:
    print(e)
