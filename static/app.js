document.addEventListener('DOMContentLoaded', () => {

    // ── Dashboard Auth ──────────────────────────────────────────────────────
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
            if (banner) {
                banner.textContent = 'UNAUTHORIZED — append ?token=<WEB_AUTH_TOKEN>';
                banner.className = 'badge badge-mode';
                banner.style.color = 'var(--loss-red)';
                banner.style.background = 'var(--loss-bg)';
            }
            throw new Error(`Auth failed (${res.status})`);
        }
        return res;
    }

    // Authenticate the log download link
    const dlLink = document.querySelector('a[href^="/api/download/"]');
    if (dlLink && AUTH_TOKEN) {
        dlLink.href = `${dlLink.getAttribute('href')}?token=${encodeURIComponent(AUTH_TOKEN)}`;
    }

    // ── Theme Switcher ──────────────────────────────────────────────────────
    const themeToggleBtn = document.getElementById('theme-toggle-btn');
    const themeToggleLabel = document.getElementById('theme-toggle-label');
    const themeTogglePill = document.getElementById('theme-toggle-pill');

    function syncThemeUI(theme) {
        if (!themeToggleLabel || !themeTogglePill) return;
        if (theme === 'dark') {
            themeToggleLabel.textContent = '🌙 Dark Theme';
            themeTogglePill.textContent = 'Switch to Light';
        } else {
            themeToggleLabel.textContent = '☀️ Light Theme';
            themeTogglePill.textContent = 'Switch to Dark';
        }
    }

    const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
    syncThemeUI(currentTheme);

    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const active = document.documentElement.getAttribute('data-theme') || 'light';
            const next = active === 'light' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', next);
            localStorage.setItem('wtb_theme', next);
            syncThemeUI(next);
        });
    }

    // ── Sidebar Controls ────────────────────────────────────────────────────
    const sidebar = document.getElementById('sidebar');
    const mobileToggle = document.getElementById('mobile-toggle');
    const desktopToggle = document.getElementById('desktop-toggle');
    const sidebarClose = document.getElementById('sidebar-close');

    if (mobileToggle) mobileToggle.addEventListener('click', () => sidebar.classList.toggle('open'));
    if (desktopToggle) desktopToggle.addEventListener('click', () => sidebar.classList.remove('collapsed'));
    if (sidebarClose) sidebarClose.addEventListener('click', () => sidebar.classList.add('collapsed'));
    
    // ── Tab Switching ───────────────────────────────────────────────────────
    const navLinks = document.querySelectorAll('.nav-links li');
    const tabContents = document.querySelectorAll('.tab-content');

    navLinks.forEach(link => {
        link.addEventListener('click', () => {
            navLinks.forEach(l => l.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            link.classList.add('active');
            const target = document.getElementById(link.getAttribute('data-target'));
            if(target) target.classList.add('active');
            
            // Trigger immediate fetch for snappy UI
            fetchAllData();
        });
    });

    // ── Formatters ──────────────────────────────────────────────────────────
    const formatMoney = (val) => {
        const num = parseFloat(val) || 0;
        return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
        if (addr.length <= 10) return addr;
        return addr.substring(0, 6) + '...' + addr.substring(addr.length - 4);
    };

    const formatDuration = (secs) => {
        if (secs == null) return "N/A";
        const m = Math.floor(secs / 60);
        const s = Math.floor(secs % 60);
        return `${m}m ${s}s`;
    };

    // ── Data Fetching ───────────────────────────────────────────────────────

    // 1. Live Dashboard
    async function fetchDashboard() {
        try {
            const res = await apiFetch('/api/dashboard');
            const data = await res.json();
            
            const badge = document.getElementById('mode-badge');
            badge.textContent = `MODE: ${data.mode}`;
            badge.className = `badge badge-mode ${data.mode === 'LIVE' ? 'mode-live' : 'mode-paper'}`;

            document.getElementById('dash-total-val').textContent = formatMoney(data.total_value);
            document.getElementById('dash-cash').textContent = formatMoney(data.available_cash);
            
            const netPnlEl = document.getElementById('dash-net-pnl');
            netPnlEl.innerHTML = formatPnl(data.net_profit);
            const netPnlCard = document.getElementById('dash-net-pnl-card') || netPnlEl.closest('.metric-card');
            if (netPnlCard) {
                const accent = data.net_profit > 0 ? 'var(--profit-green)' : (data.net_profit < 0 ? 'var(--loss-red)' : 'var(--text-muted)');
                netPnlCard.style.setProperty('--metric-accent', accent);
            }
            
            document.getElementById('dash-active-trades').textContent = `${data.active_trades_count} / ${data.max_trades}`;

            // Render live trades
            const container = document.getElementById('live-trades-container');
            if (data.active_trades_count === 0) {
                container.innerHTML = `
                    <div class="empty-state-box">
                        <span class="empty-state-icon">🐋</span>
                        <div><strong>No Active Trades In Flight</strong></div>
                        <div style="font-size: 0.8rem;">The bot is currently scanning for verified high-conviction whale moves.</div>
                    </div>
                `;
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
                        <td style="font-family:monospace">$${entry.toFixed(6)}</td>
                        <td style="font-family:monospace">$${current.toFixed(6)}</td>
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

    // 2. Trade History
    async function fetchHistory() {
        try {
            const mode = document.getElementById('history-filter').value;
            const res = await apiFetch(`/api/history?mode=${mode}`);
            const data = await res.json();

            document.getElementById('hist-total-trades').textContent = data.total_trades;
            document.getElementById('hist-win-rate').textContent = formatPct(data.win_rate);
            
            const histPnlEl = document.getElementById('hist-realized-pnl');
            histPnlEl.innerHTML = formatPnl(data.realized_profit);
            const histPnlCard = document.getElementById('hist-realized-pnl-card') || histPnlEl.closest('.metric-card');
            if (histPnlCard) {
                const accent = data.realized_profit > 0 ? 'var(--profit-green)' : (data.realized_profit < 0 ? 'var(--loss-red)' : 'var(--text-muted)');
                histPnlCard.style.setProperty('--metric-accent', accent);
            }

            const histAvgEl = document.getElementById('hist-avg-pnl');
            histAvgEl.innerHTML = formatPnl(data.avg_profit);
            const histAvgCard = document.getElementById('hist-avg-pnl-card') || histAvgEl.closest('.metric-card');
            if (histAvgCard) {
                const accent = data.avg_profit > 0 ? 'var(--profit-green)' : (data.avg_profit < 0 ? 'var(--loss-red)' : 'var(--text-muted)');
                histAvgCard.style.setProperty('--metric-accent', accent);
            }

            const tbody = document.getElementById('history-tbody');
            let html = '';
            data.trades.forEach(t => {
                const pnl = t.real_net_profit_usd || t.net_profit_usd || 0;
                const isLive = String(t.trade_mode).toUpperCase() === 'LIVE';
                const modeClass = isLive ? 'status-live' : 'status-paper';
                
                const reasonUpper = String(t.exit_reason || '').toUpperCase();
                let reasonClass = 'status-neutral';
                if (reasonUpper.includes('PROFIT') || reasonUpper.includes('TP')) reasonClass = 'status-whitelist';
                else if (reasonUpper.includes('STOP') || reasonUpper.includes('SL')) reasonClass = 'status-blacklist';

                html += `<tr>
                    <td>${new Date(t.timestamp_entry).toLocaleString()}</td>
                    <td><span class="pill-status ${modeClass}">${t.trade_mode || 'UNKNOWN'}</span></td>
                    <td><span style="font-family:monospace" title="${t.token_address || t.token}">${shortenAddress(t.token_address || t.token)}</span></td>
                    <td><span style="font-family:monospace" title="${t.whale_wallet || t.wallet}">${shortenAddress(t.whale_wallet || t.wallet)}</span></td>
                    <td>${formatMoney(t.trade_size_usd)}</td>
                    <td style="font-family:monospace">${(t.entry_usd_price||0).toFixed(6)} / ${(t.exit_usd_price||0).toFixed(6)}</td>
                    <td>${formatDuration(t.hold_duration_seconds || t.duration_seconds)}</td>
                    <td><span class="pill-status ${reasonClass}">${t.exit_reason || 'CLOSED'}</span></td>
                    <td>${formatPnl(pnl)}</td>
                </tr>`;
            });
            tbody.innerHTML = html || `<tr><td colspan="9" style="text-align:center; padding: 24px; color: var(--text-muted);">No historical trades found for this filter.</td></tr>`;

        } catch (e) {
            console.error(e);
        }
    }

    // 3. Whale Analytics
    async function fetchWhales() {
        try {
            const mode = document.getElementById('analytics-filter').value;
            const res = await apiFetch(`/api/whales?mode=${mode}`);
            const data = await res.json();

            document.getElementById('whale-total-db').textContent = data.total_db;
            document.getElementById('whale-whitelists').textContent = data.active_whitelists;

            const tbody = document.getElementById('whale-tbody');
            const modesCol = document.querySelector('.modes-col');
            
            if (modesCol) {
                modesCol.style.display = mode === 'ALL' ? 'table-cell' : 'none';
            }

            let html = '';
            data.whales.forEach(w => {
                let pillClass = 'status-neutral';
                if (w.status === 'WHITELIST') pillClass = 'status-whitelist';
                else if (w.status === 'BLACKLIST') pillClass = 'status-blacklist';

                html += `<tr>
                    <td><span style="font-family:monospace" title="${w.wallet}">${shortenAddress(w.wallet)}</span></td>
                    <td><span class="pill-status ${pillClass}">${w.status}</span></td>
                    <td><strong>${formatPct(w.win_rate)}</strong></td>
                    <td>${w.total}</td>
                    <td>${formatPnl(w.profit)}</td>
                    ${mode === 'ALL' ? `<td>${w.modes}</td>` : ''}
                </tr>`;
            });
            tbody.innerHTML = html || `<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">No whales found in this category.</td></tr>`;

        } catch (e) {
            console.error(e);
        }
    }

    // 4. System & Logs
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
            logViewer.scrollTop = logViewer.scrollHeight;

        } catch (e) {
            console.error(e);
        }
    }

    // ── Event Handlers ──────────────────────────────────────────────────────
    const histFilter = document.getElementById('history-filter');
    const analyticsFilter = document.getElementById('analytics-filter');
    const refreshLogsBtn = document.getElementById('refresh-logs-btn');

    if (histFilter) histFilter.addEventListener('change', fetchHistory);
    if (analyticsFilter) analyticsFilter.addEventListener('change', fetchWhales);
    if (refreshLogsBtn) refreshLogsBtn.addEventListener('click', fetchLogs);

    // Initial Load
    const fetchAllData = () => {
        fetchDashboard();
        fetchHistory();
        fetchWhales();
        fetchLogs();
    };

    fetchAllData();

    // Auto-refresh active tab every 5s
    setInterval(() => {
        const activeTabEl = document.querySelector('.tab-content.active');
        if (!activeTabEl) return;
        const activeTab = activeTabEl.id;
        if (activeTab === 'dashboard') fetchDashboard();
        else if (activeTab === 'history') fetchHistory();
        else if (activeTab === 'analytics') fetchWhales();
        else if (activeTab === 'system') fetchLogs();
    }, 5000);

});
