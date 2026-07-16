import re

with open('static/sector_heatmap.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. UI Buttons
toggle_html_old = """        <div style="display:flex; background:rgba(255,255,255,0.05); border-radius:20px; padding:2px; margin-left:15px; border:1px solid rgba(255,255,255,0.1)">
            <button id="btn-daily" onclick="setMode('daily')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:#818cf8; color:#fff; transition:0.3s">今日總計</button>
            <button id="btn-5m" onclick="setMode('5m')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:transparent; color:#94a3b8; transition:0.3s">近5分鐘</button>
        </div>"""

toggle_html_new = """        <div style="display:flex; gap: 15px; margin-left:15px;">
            <div style="display:flex; background:rgba(255,255,255,0.05); border-radius:20px; padding:2px; border:1px solid rgba(255,255,255,0.1)">
                <button id="btn-daily" onclick="setMode('daily')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:#818cf8; color:#fff; transition:0.3s">今日總計</button>
                <button id="btn-5m" onclick="setMode('5m')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:transparent; color:#94a3b8; transition:0.3s">近5分鐘</button>
            </div>
            <div style="display:flex; background:rgba(255,255,255,0.05); border-radius:20px; padding:2px; border:1px solid rgba(255,255,255,0.1)">
                <button id="btn-sort-pct" onclick="setSort('pct')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:#f59e0b; color:#fff; transition:0.3s">排序: 漲跌</button>
                <button id="btn-sort-vol" onclick="setSort('vol')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:transparent; color:#94a3b8; transition:0.3s">排序: 總量</button>
                <button id="btn-sort-large" onclick="setSort('large_vol')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:transparent; color:#94a3b8; transition:0.3s">排序: 大單</button>
            </div>
        </div>"""
content = content.replace(toggle_html_old, toggle_html_new)

# 2. JS State
js_state_old = """    let viewMode = 'daily';
    let momentumData = {};
    
    function setMode(mode) {"""

js_state_new = """    let viewMode = 'daily';
    let sortMode = 'pct';
    let momentumData = {};
    
    function setSort(mode) {
        sortMode = mode;
        ['pct', 'vol', 'large_vol'].forEach(m => {
            const btn = document.getElementById(`btn-sort-${m === 'large_vol' ? 'large' : m}`);
            if (btn) {
                btn.style.background = m === mode ? '#f59e0b' : 'transparent';
                btn.style.color = m === mode ? '#fff' : '#94a3b8';
            }
        });
        updateSectors();
    }
    
    function setMode(mode) {"""
content = content.replace(js_state_old, js_state_new)

# 3. Add Vol to TickerData Loop
ticker_loop_old = """                }
                tickerData.push({ sym, pct });
            });"""

ticker_loop_new = """                }
                tickerData.push({ sym, pct, vol: tVol, large_vol: tLarge });
            });"""

content = content.replace("""                    if (m) {
                        if (q) q.price = m.price;
                        const prev = m.oldest_price || (q && q.price) || 0;
                        if (prev > 0 && m.price > 0) {
                            pct = ((m.price - prev) / prev * 100);
                            const shares = sharesData[sym] || 1; 
                            const mcap = m.price * shares;
                            sumWeightedPct += (pct * mcap);
                            sumWeight += mcap;
                        }
                        sectorVol += (m.vol || 0);
                        sectorLargeVol += (m.large_vol || 0);
                    }
                }
                tickerData.push({ sym, pct });""",
"""                    if (m) {
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
                }""")

# 4. SectorAverages push
content = content.replace(
    "sectorAverages.push({ sector, avgPct });",
    "sectorAverages.push({ sector, avgPct, totalVol: sectorVol, totalLargeVol: sectorLargeVol });"
)

# 5. Ticker Sorting and Extra Div
ticker_sort_old = """                // Sort tickers within this sector
                tickerData.sort((a, b) => b.pct - a.pct);
                const grid = sCard.querySelector('.tickers-grid');
                if (grid) {
                    tickerData.forEach(item => {
                        const tCard = document.getElementById(`tcard-${item.sym}`);
                        if (tCard) grid.appendChild(tCard);
                    });
                }"""

ticker_sort_new = """                // Sort tickers within this sector
                tickerData.sort((a, b) => {
                    if (sortMode === 'pct') return b.pct - a.pct;
                    if (sortMode === 'vol') return b.vol - a.vol;
                    if (sortMode === 'large_vol') return b.large_vol - a.large_vol;
                    return b.pct - a.pct;
                });
                const grid = sCard.querySelector('.tickers-grid');
                if (grid) {
                    tickerData.forEach(item => {
                        const tCard = document.getElementById(`tcard-${item.sym}`);
                        if (tCard) {
                            grid.appendChild(tCard);
                            
                            const extraId = `textra-${item.sym}`;
                            let extraDiv = document.getElementById(extraId);
                            if (!extraDiv) {
                                extraDiv = document.createElement('div');
                                extraDiv.id = extraId;
                                extraDiv.style.fontSize = '0.75rem';
                                extraDiv.style.marginTop = '2px';
                                extraDiv.style.fontWeight = 'bold';
                                tCard.appendChild(extraDiv);
                            }
                            if (sortMode === 'vol' && viewMode === '5m') {
                                extraDiv.innerHTML = `<span style="color:#bae6fd">量:${item.vol}</span>`;
                            } else if (sortMode === 'large_vol' && viewMode === '5m') {
                                extraDiv.innerHTML = `<span style="color:#fde047">大:${item.large_vol}</span>`;
                            } else {
                                extraDiv.innerHTML = '';
                            }
                        }
                    });
                }"""
content = content.replace(ticker_sort_old, ticker_sort_new)

# 6. Sector Sorting
sector_sort_old = """        // Sort sectors
        sectorAverages.sort((a, b) => b.avgPct - a.avgPct);
        sectorAverages.forEach(item => {"""

sector_sort_new = """        // Sort sectors
        sectorAverages.sort((a, b) => {
            if (sortMode === 'pct') return b.avgPct - a.avgPct;
            if (sortMode === 'vol') return b.totalVol - a.totalVol;
            if (sortMode === 'large_vol') return b.totalLargeVol - a.totalLargeVol;
            return b.avgPct - a.avgPct;
        });
        sectorAverages.forEach(item => {"""
content = content.replace(sector_sort_old, sector_sort_new)

with open('static/sector_heatmap.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Updated sector_heatmap.html")
