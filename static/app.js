document.addEventListener('DOMContentLoaded', () => {

    // ── Dashboard auth ──────────────────────────────────────────────────────
    // The API now requires the WEB_AUTH_TOKEN shared secret. Open the dashboard as
    //   https://<host>/?token=<WEB_AUTH_TOKEN>
    // The token is kept in sessionStorage so it survives in-page navigation, and stripped
    // from the visible URL so it isn't left sitting in the address bar or copied into
    // a screenshot.
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    if (urlToken) {
        sessionStorage.setItem('wtb_token', urlToken);
        urlParams.delete('token');
        const clean = window.location.pathname + (urlParams.toString() ? '?' + urlParams : '');
        window.history.replaceState({}, '', clean);
    }
    const AUTH_TOKEN = sessionStorage.getItem('wtb_token') || '';

    /** fetch() with the bearer token attached. Surfaces 401s instead of failing silently. */
    async function apiFetch(url) {
        const res = await fetch(url, {
            headers: AUTH_TOKEN ? { 'Authorization': `Bearer ${AUTH_TOKEN}` } : {}
        });
        if (res.status === 401 || res.status === 503) {
            const banner = document.getElementById('mode-badge');
            if (banner) banner.textContent = 'UNAUTHORIZED — append ?token=<WEB_AUTH_TOKEN>';
            throw new Error(`Auth failed (${res.status})`);
        }
        return res;
    }

    // Authenticate the log download link, which is a plain anchor and can't send headers.
    const dlLink = document.querySelector('a[href^="/api/download/"]');
    if (dlLink && AUTH_TOKEN) {
        dlLink.href = `${dlLink.getAttribute('href')}?token=${encodeURIComponent(AUTH_TOKEN)}`;
    }

    // Sidebar Toggle
    const sidebar = document.getElementById('sidebar');
    const mobileToggle = document.getElementById('mobile-toggle');
    const desktopToggle = document.getElementById('desktop-toggle');
    const sidebarClose = document.getElementById('sidebar-close');

    if (mobileToggle) mobileToggle.addEventListener('click', () => sidebar.classList.toggle('open'));
    if (desktopToggle) desktopToggle.addEventListener('click', () => sidebar.classList.remove('collapsed'));
    if (sidebarClose) sidebarClose.addEventListener('click', () => sidebar.classList.add('collapsed'));
    
    // Tab Switching Logic
    const navLinks = document.querySelectorAll('.nav-links li');
    const tabContents = document.querySelectorAll('.tab-content');

    navLinks.forEach(link => {
        link.addEventListener('click', () => {
            navLinks.forEach(l => l.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            link.classList.add('active');
            const target = document.getElementById(link.getAttribute('data-target'));
            if(target) target.classList.add('active');
            
            // Force immediate fetch when switching tabs for snappiness
            fetchAllData();
        });
    });

    // Formatters
    const formatMoney = (val) => {
        const num = parseFloat(val) || 0;
        return '$' + num.toFixed(2);
    };

    const formatPct = (val) => {
        const num = parseFloat(val) || 0;
        return num.toFixed(1) + '%';
    };

    const formatPnl = (val) => {
        const num = parseFloat(val) || 0;
        const colorClass = num > 0 ? 'profit-positive' : (num < 0 ? 'profit-negative' : '');
        const sign = num > 0 ? '+' : (num < 0 ? '-' : '');
        return `<span class="${colorClass}">${sign}${formatMoney(Math.abs(num))}</span>`;
    };

    const shortenAddress = (addr) => {
        if (!addr) return 'Unknown';
        return addr.substring(0, 6) + '...' + addr.substring(addr.length - 4);
    };

    const formatDuration = (secs) => {
        if (secs == null) return "N/A";
        const m = Math.floor(secs / 60);
        const s = Math.floor(secs % 60);
        return `${m}m ${s}s`;
    };

    // Fetch and Render Functions
    async function fetchDashboard() {
        try {
            const res = await apiFetch('/api/dashboard');
            const data = await res.json();
            
            document.getElementById('mode-badge').textContent = `MODE: ${data.mode}`;
            document.getElementById('dash-total-val').textContent = formatMoney(data.total_value);
            document.getElementById('dash-cash').textContent = formatMoney(data.available_cash);
            
            const netPnlEl = document.getElementById('dash-net-pnl');
            netPnlEl.innerHTML = formatPnl(data.net_profit);
            const netPnlCard = netPnlEl.closest('.metric-card');
            if (netPnlCard) {
                netPnlCard.style.borderLeftColor = data.net_profit > 0 ? '#00FF94' : (data.net_profit < 0 ? '#FF3366' : '#94A3B8');
            }
            
            document.getElementById('dash-active-trades').textContent = `${data.active_trades_count} / ${data.max_trades}`;


            // Render live trades (for now just a placeholder if empty, else table)
            const container = document.getElementById('live-trades-container');
            if(data.active_trades_count === 0) {
                container.innerHTML = `<div style="background:var(--bg-secondary); padding:20px; border-radius:8px; border:1px solid var(--border); color:var(--text-muted);">🐋 There are currently no active trades. The bot is waiting for a whale to make a move.</div>`;
            } else {
                let html = `<div class="table-container"><table class="data-table"><thead><tr><th>Token</th><th>Whale</th><th>Entry Time</th><th>Entry Price</th><th>Current Price</th><th>Size</th><th>P&L (%)</th><th>P&L ($)</th></tr></thead><tbody>`;
                data.trades.forEach(t => {
                    const size = parseFloat(t.trade_size_usd || t.trade_size) || 0;
                    const entry = parseFloat(t.entry_price) || 1;
                    const current = parseFloat(t.current_price) || entry;
                    
                    const pct = ((current - entry) / entry) * 100;
                    const pnlUsd = t.profit_usd !== undefined ? t.profit_usd : (((current - entry) / entry) * size);
                    
                    const pctColorClass = pct > 0 ? 'profit-positive' : (pct < 0 ? 'profit-negative' : '');
                    const pctSign = pct > 0 ? '+' : '';
                    const pctHtml = `<span class="${pctColorClass}">${pctSign}${pct.toFixed(2)}%</span>`;
                    
                    let entryTimeStr = 'N/A';
                    if (t.timestamp_entry) {
                        const d = new Date(t.timestamp_entry);
                        if (!isNaN(d.getTime())) {
                            entryTimeStr = d.toLocaleTimeString();
                        }
                    }
                    
                    html += `<tr>
                        <td><span style="font-family:monospace">${shortenAddress(t.token_address || t.token)}</span></td>
                        <td><span style="font-family:monospace">${shortenAddress(t.whale_wallet || t.wallet)}</span></td>
                        <td>${entryTimeStr}</td>
                        <td>${entry.toFixed(6)}</td>
                        <td>${current.toFixed(6)}</td>
                        <td>${formatMoney(size)}</td>
                        <td>${pctHtml}</td>
                        <td>${formatPnl(pnlUsd)}</td>
                    </tr>`;
                });
                html += `</tbody></table></div>`;
                container.innerHTML = html;
            }

        } catch (e) {
            console.error(e);
        }
    }

    async function fetchHistory() {
        try {
            const mode = document.getElementById('history-filter').value;
            const res = await apiFetch(`/api/history?mode=${mode}`);
            const data = await res.json();

            document.getElementById('hist-total-trades').textContent = data.total_trades;
            document.getElementById('hist-win-rate').textContent = formatPct(data.win_rate);
            
            const histPnlEl = document.getElementById('hist-realized-pnl');
            histPnlEl.innerHTML = formatPnl(data.realized_profit);
            const histPnlCard = histPnlEl.closest('.metric-card');
            if (histPnlCard) {
                histPnlCard.style.borderLeftColor = data.realized_profit > 0 ? '#00FF94' : (data.realized_profit < 0 ? '#FF3366' : '#94A3B8');
            }

            const histAvgEl = document.getElementById('hist-avg-pnl');
            histAvgEl.innerHTML = formatPnl(data.avg_profit);
            const histAvgCard = histAvgEl.closest('.metric-card');
            if (histAvgCard) {
                histAvgCard.style.borderLeftColor = data.avg_profit > 0 ? '#00FF94' : (data.avg_profit < 0 ? '#FF3366' : '#94A3B8');
            }


            const tbody = document.getElementById('history-tbody');
            let html = '';
            data.trades.forEach(t => {
                const pnl = t.real_net_profit_usd || t.net_profit_usd || 0;
                let modeColor = t.trade_mode === 'LIVE' ? 'color: var(--emerald);' : 'color: var(--amber);';
                html += `<tr>
                    <td>${new Date(t.timestamp_entry).toLocaleString()}</td>
                    <td><b style="${modeColor}">${t.trade_mode || 'UNKNOWN'}</b></td>
                    <td><span style="font-family:monospace">${shortenAddress(t.token_address || t.token)}</span></td>
                    <td><span style="font-family:monospace">${shortenAddress(t.whale_wallet || t.wallet)}</span></td>
                    <td>${formatMoney(t.trade_size_usd)}</td>
                    <td>${(t.entry_usd_price||0).toFixed(6)} / ${(t.exit_usd_price||0).toFixed(6)}</td>
                    <td>${formatDuration(t.hold_duration_seconds || t.duration_seconds)}</td>
                    <td><b>${t.exit_reason || 'CLOSED'}</b></td>
                    <td>${formatPnl(pnl)}</td>
                </tr>`;
            });
            tbody.innerHTML = html || `<tr><td colspan="8" style="text-align:center;">No trades found.</td></tr>`;

        } catch (e) {
            console.error(e);
        }
    }

    async function fetchWhales() {
        try {
            const mode = document.getElementById('analytics-filter').value;
            const res = await apiFetch(`/api/whales?mode=${mode}`);
            const data = await res.json();

            document.getElementById('whale-total-db').textContent = data.total_db;
            document.getElementById('whale-whitelists').textContent = data.active_whitelists;

            const tbody = document.getElementById('whale-tbody');
            const modesCol = document.querySelector('.modes-col');
            
            if (mode === 'ALL') {
                modesCol.style.display = 'table-cell';
            } else {
                modesCol.style.display = 'none';
            }

            let html = '';
            data.whales.forEach(w => {
                let statusColor = '';
                if(w.status === 'WHITELIST') statusColor = 'color: var(--emerald)';
                else if(w.status === 'BLACKLIST') statusColor = 'color: var(--red)';

                html += `<tr>
                    <td><span style="font-family:monospace" title="${w.wallet}">${shortenAddress(w.wallet)}</span></td>
                    <td><b style="${statusColor}">${w.status}</b></td>
                    <td>${formatPct(w.win_rate)}</td>
                    <td>${w.total}</td>
                    <td>${formatPnl(w.profit)}</td>
                    ${mode === 'ALL' ? `<td>${w.modes}</td>` : ''}
                </tr>`;
            });
            tbody.innerHTML = html || `<tr><td colspan="6" style="text-align:center;">No whales tracked yet.</td></tr>`;

        } catch (e) {
            console.error(e);
        }
    }

    async function fetchLogs() {
        try {
            const res = await apiFetch('/api/logs');
            const data = await res.json();

            const envViewer = document.getElementById('env-viewer');
            let envHtml = '<table>';
            data.env.forEach(item => {
                envHtml += `<tr><td>${item.key}</td><td>${item.val}</td></tr>`;
            });
            envHtml += '</table>';
            envViewer.innerHTML = envHtml;

            const logViewer = document.getElementById('log-viewer');
            logViewer.textContent = data.logs || "No logs available.";
            // scroll to bottom smoothly
            logViewer.scrollTop = logViewer.scrollHeight;

        } catch (e) {
            console.error(e);
        }
    }

    // Refresh controls
    document.getElementById('history-filter').addEventListener('change', fetchHistory);
    document.getElementById('analytics-filter').addEventListener('change', fetchWhales);
    document.getElementById('refresh-logs-btn').addEventListener('click', fetchLogs);

    // Global fetch
    const fetchAllData = () => {
        fetchDashboard();
        fetchHistory();
        fetchWhales();
        fetchLogs();
    };

    // Init
    fetchAllData();

    // Auto-refresh every 5 seconds (only the active tab to save bandwidth)
    setInterval(() => {
        const activeTab = document.querySelector('.tab-content.active').id;
        if(activeTab === 'dashboard') fetchDashboard();
        else if(activeTab === 'history') fetchHistory();
        else if(activeTab === 'analytics') fetchWhales();
        else if(activeTab === 'system') fetchLogs(); // auto refresh logs too
    }, 5000);

});


