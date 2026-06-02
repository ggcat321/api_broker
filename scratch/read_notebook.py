import os
import json

notebook_path = "/Users/jeffrey/Downloads/集保股權集中TDCC(Buy).ipynb"

if os.path.exists(notebook_path):
    with open(notebook_path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    print("Number of cells:", len(nb['cells']))
    code_cells = [c for c in nb['cells'] if c['cell_type'] == 'code']
    print("Number of code cells:", len(code_cells))
    
    # Let's search for cells that contain plotting or backtest metrics
    for idx, cell in enumerate(code_cells):
        source = "".join(cell['source'])
        if any(kw in source.lower() for kw in ["plot", "cumprod", "cumulative", "drawdown", "backtest", "equity"]):
            print(f"\n--- Code Cell {idx} ---")
            print("Source:")
            print("\n".join(cell['source'][:15])) # print first 15 lines of matching cells
            print("...")
            
            # Print output if available
            outputs = cell.get('outputs', [])
            if outputs:
                print(f"Has {len(outputs)} outputs")
