import re

with open('static/sector_heatmap.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add Toggle Buttons
toggle_html = """        <div style="color:#94a3b8">Tickers: <span id="tickerCount" style="color:#e2e8f0;font-weight:700">0</span></div>
        <div style="display:flex; background:rgba(255,255,255,0.05); border-radius:20px; padding:2px; margin-left:15px; border:1px solid rgba(255,255,255,0.1)">
            <button id="btn-daily" onclick="setMode('daily')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:#818cf8; color:#fff; transition:0.3s">今日總計</button>
            <button id="btn-5m" onclick="setMode('5m')" style="border:none; border-radius:20px; padding:4px 14px; font-size:12px; cursor:pointer; font-weight:bold; background:transparent; color:#94a3b8; transition:0.3s">近5分鐘</button>
        </div>"""
content = content.replace('        <div style="color:#94a3b8">Tickers: <span id="tickerCount" style="color:#e2e8f0;font-weight:700">0</span></div>', toggle_html)

# 2. Add Variables and setMode
js_vars = """    let viewMode = 'daily';
    let momentumData = {};
    
    function setMode(mode) {
        viewMode = mode;
        document.getElementById('btn-daily').style.background = mode === 'daily' ? '#818cf8' : 'transparent';
        document.getElementById('btn-daily').style.color = mode === 'daily' ? '#fff' : '#94a3b8';
        
        document.getElementById('btn-5m').style.background = mode === '5m' ? '#ec4899' : 'transparent';
        document.getElementById('btn-5m').style.color = mode === '5m' ? '#fff' : '#94a3b8';
        
        // Immediately fetch and update
        updateSectors();
    }"""
content = content.replace("    let allTickers = [];", "    let allTickers = [];\n" + js_vars)

# 3. Add VolBadge to RenderHeatmap
render_header = """            header.appendChild(title);
            
            const volBadge = document.createElement('div');
            volBadge.className = 'sector-vol';
            volBadge.id = `sector-vol-${sector}`;
            volBadge.style.fontSize = '0.75rem';
            volBadge.style.color = '#cbd5e1';
            volBadge.style.marginLeft = '12px';
            volBadge.style.marginRight = 'auto';
            volBadge.innerText = '';
            header.appendChild(volBadge);
            
            header.appendChild(avgBadge);"""
content = content.replace("            header.appendChild(title);\n            header.appendChild(avgBadge);", render_header)

# 4. Modify updateSectors
update_sectors_old = """    function updateSectors() {"""
update_sectors_new = """    async function updateSectors() {
        if (viewMode === '5m') {
            try {
                const res = await fetch('/api/momentum-5m');
                if (res.ok) momentumData = await res.json();
            } catch(e) {}
        }"""
content = content.replace(update_sectors_old, update_sectors_new)

# 5. Modify Ticker loop in updateSectors
ticker_loop_old = """            tickers.forEach(sym => {
                const q = quotes[sym];
                let pct = 0;
                if (q && q.price > 0 && q.prev > 0) {
                    pct = ((q.price - q.prev) / q.prev * 100);
                    const shares = sharesData[sym] || 1; 
                    const mcap = q.price * shares;
                    sumWeightedPct += (pct * mcap);
                    sumWeight += mcap;
                }
                tickerData.push({ sym, pct });
            });"""

ticker_loop_new = """            let sectorVol = 0;
            let sectorLargeVol = 0;

            tickers.forEach(sym => {
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
                        sectorVol += (m.vol || 0);
                        sectorLargeVol += (m.large_vol || 0);
                    }
                }
                tickerData.push({ sym, pct });
            });"""
content = content.replace(ticker_loop_old, ticker_loop_new)

# 6. Update avgBadge logic and sectorVol in updateSectors
avg_badge_logic_old = """            const avgBadge = document.getElementById(`sector-avg-${sector}`);
            const sCard = document.getElementById(`sector-card-${sector}`);
            
            if (avgBadge && sCard) {"""

avg_badge_logic_new = """            const avgBadge = document.getElementById(`sector-avg-${sector}`);
            const sCard = document.getElementById(`sector-card-${sector}`);
            const volBadge = document.getElementById(`sector-vol-${sector}`);
            
            if (avgBadge && sCard) {
                if (viewMode === '5m' && volBadge) {
                    volBadge.innerHTML = `總量 <strong style="color:#38bdf8">${sectorVol}</strong> | 大單 <strong style="color:#fbbf24">${sectorLargeVol}</strong>`;
                } else if (volBadge) {
                    volBadge.innerHTML = '';
                }
"""
content = content.replace(avg_badge_logic_old, avg_badge_logic_new)

# 7. Modify updateTickerUI logic
ticker_ui_old = """    function updateTickerUI(sym, price) {
        if (!quotes[sym]) return;
        quotes[sym].price = price;
        const q = quotes[sym];
        const pct = (q.price && q.prev && q.prev > 0) ? ((q.price - q.prev) / q.prev * 100) : 0;"""

ticker_ui_new = """    function updateTickerUI(sym, price) {
        if (!quotes[sym]) return;
        quotes[sym].price = price;
        const q = quotes[sym];
        let pct = 0;
        if (viewMode === 'daily') {
            pct = (q.price && q.prev && q.prev > 0) ? ((q.price - q.prev) / q.prev * 100) : 0;
        } else {
            const m = momentumData[sym];
            const prev = (m && m.oldest_price) ? m.oldest_price : q.price;
            pct = prev > 0 ? ((q.price - prev) / prev * 100) : 0;
        }"""
content = content.replace(ticker_ui_old, ticker_ui_new)

with open('static/sector_heatmap.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Updated sector_heatmap.html")
