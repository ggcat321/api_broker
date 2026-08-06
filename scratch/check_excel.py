import pandas as pd

try:
    df = pd.read_excel("/Users/jeffrey/Downloads/Insti_Margin (1).xlsx", sheet_name="Summary", header=None, skiprows=4, nrows=5)
    
    # We want to check columns X(23) to AM(38)
    # Print out those columns
    cols = df.columns
    if len(cols) > 38:
        subset = df.iloc[:, 23:39]
        print(subset)
    else:
        print(f"File only has {len(cols)} columns")
except Exception as e:
    print(e)
