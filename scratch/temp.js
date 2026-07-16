    const statusText = document.getElementById('statusText');
    const liveIndicator = document.getElementById('liveIndicator');
    const heatmapContainer = document.getElementById('heatmapContainer');
    
    let ws = null;
    let wsRetryCount = 0;
    const WS_MAX_RETRIES = 10;
    const WS_BASE_DELAY = 1000;
    const WS_MAX_DELAY = 30000;
    
    let sectorsData = {};
    let sharesData = {};
    let allTickers = [];
    let viewMode = 'daily';
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
    
    function setMode(mode) {
        viewMode = mode;
        document.getElementById('btn-daily').style.background = mode === 'daily' ? '#818cf8' : 'transparent';
        document.getElementById('btn-daily').style.color = mode === 'daily' ? '#fff' : '#94a3b8';
        
        document.getElementById('btn-5m').style.background = mode === '5m' ? '#ec4899' : 'transparent';
        document.getElementById('btn-5m').style.color = mode === '5m' ? '#fff' : '#94a3b8';
        
        // Immediately fetch and update
        updateSectors();
    }
    let quotes = {}; // sym -> { price, prev, name }

    // Color mapping helper (Taiwan: Red is UP, Green is DOWN)
    function getColor(pctChange) {
        if (pctChange === null || pctChange === undefined || isNaN(pctChange)) {
            return 'rgba(148, 163, 184, 0.2)'; // Gray for N/A
        }
        
        // Clamp between -10 and 10 for color intensity calculation
        const clamped = Math.max(-10, Math.min(10, pctChange));
        const intensity = Math.max(0.3, Math.abs(clamped) / 10); // Base 0.3 opacity to max 1.0
        
        if (pctChange > 0) {
            // UP: Red (#ef4444 -> 239, 68, 68)
            return `rgba(239, 68, 68, ${intensity})`;
        } else if (pctChange < 0) {
            // DOWN: Green (#22c55e -> 34, 197, 94)
            return `rgba(34, 197, 94, ${intensity})`;
        } else {
            // ZERO
            return 'rgba(148, 163, 184, 0.4)';
        }
    }

    async function init() {
        try {
            // 1. Fetch sectors JSON and shares JSON
            const res = await fetch('/static/sectors.json');
            if (!res.ok) throw new Error('Failed to load sectors.json');
            sectorsData = await res.json();
            
            try {
                const sRes = await fetch('/static/shares.json');
                if (sRes.ok) sharesData = await sRes.json();
            } catch (e) {
                console.warn('Could not load shares.json', e);
            }
            
            // Extract all unique tickers
            const tickerSet = new Set();
            for (const [sector, tickers] of Object.entries(sectorsData)) {
                tickers.forEach(t => tickerSet.add(t));
            }
            allTickers = Array.from(tickerSet);
            
            document.getElementById('sectorCount').innerText = Object.keys(sectorsData).length;
            document.getElementById('tickerCount').innerText = allTickers.length;

            if (allTickers.length === 0) {
                heatmapContainer.innerHTML = '<div id="loading">No tickers found.</div>';
                return;
            }

            // 2. Fetch initial quotes for prev close and current price
            heatmapContainer.innerHTML = '<div id="loading">Fetching quotes...</div>';
            
            // Chunk requests if too many
            const chunkSize = 100;
            for (let i = 0; i < allTickers.length; i += chunkSize) {
                const chunk = allTickers.slice(i, i + chunkSize);
                try {
                    const qRes = await fetch(`/api/stock-quotes?symbols=${chunk.join(',')}`);
                    const qData = await qRes.json();
                    for (const [sym, data] of Object.entries(qData)) {
                        quotes[sym] = { price: data.price, prev: data.prev };
                    }
                } catch (e) {
                    console.error('Failed to fetch quotes chunk', e);
                }
            }

            // Fetch metadata for names asynchronously (optional, fire and forget)
            allTickers.forEach(sym => {
                if(!quotes[sym]) quotes[sym] = { price: 0, prev: 0 };
                fetch(`/api/meta/${sym}`).then(r => r.json()).then(data => {
                    if (data && data.name) {
                        quotes[sym].name = data.name;
                        const el = document.getElementById(`name-${sym}`);
                        if(el) el.innerText = data.name;
                    }
                }).catch(()=>{});
            });

            // 3. Render Heatmap
            renderHeatmap();

            // 4. Connect WebSocket
            connectWS();
            
            // 5. Setup loop to recalculate averages and re-render every 3 seconds to avoid flashing too much
            setInterval(updateSectors, 3000);

        } catch (e) {
            heatmapContainer.innerHTML = `<div id="loading" style="color:#ef4444">Error initializing: ${e.message}</div>`;
            console.error(e);
        }
    }

    function renderHeatmap() {
        heatmapContainer.innerHTML = '';
        
        for (const [sector, tickers] of Object.entries(sectorsData)) {
            if (tickers.length === 0) continue;
            
            const sectorCard = document.createElement('div');
            sectorCard.className = 'sector-card';
            sectorCard.id = `sector-card-${sector}`;
            
            const header = document.createElement('div');
            header.className = 'sector-header';
            
            const title = document.createElement('div');
            title.className = 'sector-title';
            title.innerText = sector;
            
            const avgBadge = document.createElement('div');
            avgBadge.className = 'sector-avg';
            avgBadge.id = `sector-avg-${sector}`;
            avgBadge.innerText = '0.00%';
            avgBadge.style.background = getColor(0);
            
            header.appendChild(title);
            
            const volBadge = document.createElement('div');
            volBadge.className = 'sector-vol';
            volBadge.id = `sector-vol-${sector}`;
            volBadge.style.fontSize = '0.75rem';
            volBadge.style.color = '#cbd5e1';
            volBadge.style.marginLeft = '12px';
            volBadge.style.marginRight = 'auto';
            volBadge.innerText = '';
            header.appendChild(volBadge);
            
            header.appendChild(avgBadge);
            sectorCard.appendChild(header);
            
            const grid = document.createElement('div');
            grid.className = 'tickers-grid';
            
            tickers.forEach(sym => {
                const tCard = document.createElement('div');
                tCard.className = 'ticker-card';
                tCard.id = `tcard-${sym}`;
                
                const q = quotes[sym] || { price: 0, prev: 0 };
                const pct = (q.price && q.prev && q.prev > 0) ? ((q.price - q.prev) / q.prev * 100) : 0;
                
                tCard.style.background = getColor(pct);
                
                tCard.innerHTML = `
                    <div class="ticker-symbol" id="name-${sym}" title="${sym}">${q.name || sym}</div>
                    <div class="ticker-price" id="tprice-${sym}">${q.price > 0 ? q.price.toFixed(2) : '-'}</div>
                    <div class="ticker-pct" id="tpct-${sym}">${pct !== 0 ? pct.toFixed(2) + '%' : '-'}</div>
                `;
                grid.appendChild(tCard);
            });
            
            sectorCard.appendChild(grid);
            heatmapContainer.appendChild(sectorCard);
        }
        updateSectors();
    }

    function updateTickerUI(sym, price) {
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
        }
        
        const card = document.getElementById(`tcard-${sym}`);
        const pEl = document.getElementById(`tprice-${sym}`);
        const pctEl = document.getElementById(`tpct-${sym}`);
        
        if (card && pEl && pctEl) {
            card.style.background = getColor(pct);
            pEl.innerText = price.toFixed(2);
            pctEl.innerText = pct > 0 ? '+' + pct.toFixed(2) + '%' : pct.toFixed(2) + '%';
        }
    }

    async function updateSectors() {
        if (viewMode === '5m') {
            try {
                const res = await fetch('/api/momentum-5m');
                if (res.ok) momentumData = await res.json();
            } catch(e) {}
        }
        let sectorAverages = [];

        for (const [sector, tickers] of Object.entries(sectorsData)) {
            if (tickers.length === 0) continue;
            
            let sumWeightedPct = 0;
            let sumWeight = 0;
            let tickerData = [];
            
            let sectorVol = 0;
            let sectorLargeVol = 0;

            tickers.forEach(sym => {
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
            });
            
            const avgPct = sumWeight > 0 ? (sumWeightedPct / sumWeight) : 0;
            sectorAverages.push({ sector, avgPct, totalVol: sectorVol, totalLargeVol: sectorLargeVol });

            const avgBadge = document.getElementById(`sector-avg-${sector}`);
            const sCard = document.getElementById(`sector-card-${sector}`);
            const volBadge = document.getElementById(`sector-vol-${sector}`);
            
            if (avgBadge && sCard) {
                if (viewMode === '5m' && volBadge) {
                    volBadge.innerHTML = `總量 <strong style="color:#38bdf8">${sectorVol}</strong> | 大單 <strong style="color:#fbbf24">${sectorLargeVol}</strong>`;
                } else if (volBadge) {
                    volBadge.innerHTML = '';
                }

                avgBadge.innerText = avgPct > 0 ? '+' + avgPct.toFixed(2) + '%' : avgPct.toFixed(2) + '%';
                avgBadge.style.background = getColor(avgPct);
                
                // Subtle tint to the sector card border based on average
                if (avgPct > 0) {
                    sCard.style.borderColor = `rgba(239, 68, 68, ${Math.min(avgPct/5, 0.8)})`;
                } else if (avgPct < 0) {
                    sCard.style.borderColor = `rgba(34, 197, 94, ${Math.min(Math.abs(avgPct)/5, 0.8)})`;
                } else {
                    sCard.style.borderColor = 'rgba(255,255,255,0.1)';
                }

                // Sort tickers within this sector
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
                }
            }
        }

        // Sort sectors
        sectorAverages.sort((a, b) => {
            if (sortMode === 'pct') return b.avgPct - a.avgPct;
            if (sortMode === 'vol') return b.totalVol - a.totalVol;
            if (sortMode === 'large_vol') return b.totalLargeVol - a.totalLargeVol;
            return b.avgPct - a.avgPct;
        });
        sectorAverages.forEach(item => {
            const sCard = document.getElementById(`sector-card-${item.sector}`);
            if (sCard) heatmapContainer.appendChild(sCard);
        });
    }

    function connectWS() {
        if (ws) {
            ws.onclose = null;
            ws.close();
        }

        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        // Connect to the WS with all tickers. Note: URLs can be long, but usually standard limits allow ~2000 chars.
        // If we have hundreds of tickers, it might be better to split or POST, but /ws/{symbols} is a GET.
        // Assuming the list fits in the URL.
        const wsUrl = `${proto}//${window.location.host}/ws/${allTickers.join(',')}`;
        console.log(`Connecting WS...`);
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            wsRetryCount = 0;
            statusText.innerText = 'Live';
            statusText.style.color = '#4ade80';
            liveIndicator.className = 'live-dot connected';
        };

        ws.onclose = () => {
            statusText.innerText = 'Disconnected';
            statusText.style.color = '#64748b';
            liveIndicator.className = 'live-dot';

            if (wsRetryCount < WS_MAX_RETRIES) {
                wsRetryCount++;
                const delay = Math.min(WS_BASE_DELAY * Math.pow(2, wsRetryCount - 1), WS_MAX_DELAY);
                setTimeout(connectWS, delay);
            }
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.event === 'data' && msg.data) {
                    const channel = msg.channel;
                    const d = msg.data;
                    const sym = d.symbol;
                    if (!sym || !quotes[sym]) return;

                    if (channel === 'trades') {
                        // Only update if there is a new valid price
                        if (d.price && d.price > 0) {
                            updateTickerUI(sym, d.price);
                        }
                    }
                }
            } catch (e) {
                // ignore parsing errors
            }
        };
    }

    init();
</script>
<script>
    document.addEventListener('click', e => {
        const a = e.target.closest('a[href^="/"]');
        if (a && window.parent !== window) {
            e.preventDefault();
            window.parent.postMessage({type:'NAVIGATE', path: a.getAttribute('href')}, '*');
        }
    });
