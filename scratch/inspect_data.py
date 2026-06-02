import os
import pandas as pd

excel_path = "/Users/jeffrey/Downloads/集保股權集中TDCC(Buy).xlsx"

if os.path.exists(excel_path):
    df = pd.read_excel(excel_path, sheet_name="Sheet1")
    print("Total rows:", len(df))
    print("Columns:", list(df.columns))
    
    # Check nulls in critical columns
    print("\nNull counts:")
    print(df[['entry_date', 'exit_date', 'return', 'position']].isnull().sum())
    
    # Unique values in position
    print("\nUnique position values:", df['position'].unique())
    
    # Latest entry dates
    print("\nLatest entry dates:")
    print(df['entry_date'].value_counts().sort_index(ascending=False).head(10))
    
    # Latest exit dates
    print("\nLatest exit dates:")
    print(df['exit_date'].value_counts().sort_index(ascending=False).head(10))
    
    # Print some active trades (where exit_date is null)
    active = df[df['exit_date'].isnull()]
    print(f"\nActive trades (null exit_date) - Count: {len(active)}")
    if len(active) > 0:
        print(active[['trade_index', 'symbol', 'entry_date', 'position', 'return', 'trade_price@entry_date']].head(10))
        
    # Print some closed trades
    closed = df[df['exit_date'].notnull()]
    print(f"\nClosed trades - Count: {len(closed)}")
    if len(closed) > 0:
        print(closed[['trade_index', 'symbol', 'entry_date', 'exit_date', 'return']].head(5))
        
    # Return statistics
    print("\nReturn stats:")
    print(df['return'].describe())
