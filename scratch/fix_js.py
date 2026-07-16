import re

with open('static/sector_heatmap.html', 'r', encoding='utf-8') as f:
    content = f.read()

bad_block = """            tickers.forEach(sym => {
                const q = quotes[sym];
                let pct = 0;
                if (viewMode === 'daily') {
                    if (q && q.price > 0 && q.prev > 0) {
                        pct = ((q.price - q.prev) / q.prev * 100);
                        const shares = sharesData[sym] || 1; 
                        const mcap = q.price * shares;
                        sumWeightedPct += (pct * mcap);
                        sumWeight += mcap;
                    }
                } else {
                    const m = momentumData[sym];
                    if (m) {
                        if (q) q.price = m.price;
                        const prev = m.oldest_price || (q && q.price) || 0;
                        if (prev > 0 && m.price > 0) {
                            pct = ((m.price - prev) / prev * 100);
                            const shares = sharesData[sym] || 1; 
                            const mcap = m.price * shares;
                            sumWeightedPct += (pct * mcap);
                            sumWeight += mcap;
                        }
                        let tVol = (m.vol || 0);
                        let tLarge = (m.large_vol || 0);
                        sectorVol += tVol;
                        sectorLargeVol += tLarge;
                        tickerData.push({ sym, pct, vol: tVol, large_vol: tLarge });
                    } else {
                        tickerData.push({ sym, pct, vol: 0, large_vol: 0 });
                    }
                } else {
                    tickerData.push({ sym, pct, vol: 0, large_vol: 0 });
                }
            });"""

good_block = """            tickers.forEach(sym => {
                const q = quotes[sym];
                let pct = 0;
                let tVol = 0;
                let tLarge = 0;
                
                if (viewMode === 'daily') {
                    if (q && q.price > 0 && q.prev > 0) {
                        pct = ((q.price - q.prev) / q.prev * 100);
                        const shares = sharesData[sym] || 1; 
                        const mcap = q.price * shares;
                        sumWeightedPct += (pct * mcap);
                        sumWeight += mcap;
                    }
                } else {
                    const m = momentumData[sym];
                    if (m) {
                        if (q) q.price = m.price;
                        const prev = m.oldest_price || (q && q.price) || 0;
                        if (prev > 0 && m.price > 0) {
                            pct = ((m.price - prev) / prev * 100);
                            const shares = sharesData[sym] || 1; 
                            const mcap = m.price * shares;
                            sumWeightedPct += (pct * mcap);
                            sumWeight += mcap;
                        }
                        tVol = (m.vol || 0);
                        tLarge = (m.large_vol || 0);
                        sectorVol += tVol;
                        sectorLargeVol += tLarge;
                    }
                }
                
                tickerData.push({ sym, pct, vol: tVol, large_vol: tLarge });
            });"""

if bad_block in content:
    content = content.replace(bad_block, good_block)
    with open('static/sector_heatmap.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Fixed!")
else:
    print("Could not find bad block!")

