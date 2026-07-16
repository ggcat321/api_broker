import json

with open('static/shares.json') as f:
    data = json.load(f)
    
mcaps = []
for sym, info in data['stocks'].items():
    if len(sym) == 4 and info['market'] == 'sii':
        mcap = info['shares'] * info.get('price', 0)
        mcaps.append((sym, mcap, info['shares'], info.get('price', 0)))
        
mcaps.sort(key=lambda x: x[1], reverse=True)
for i in range(10):
    print(mcaps[i])
