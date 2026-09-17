/**
 * WhaleBot.SOL — Modern Frontend Application
 * Handles authentication, real-time polling, tab switching,
 * settings hot-reloading, whale intelligence, and CSV export.
 */

document.addEventListener('DOMContentLoaded', () => {

    // ── 1. Authentication ──────────────────────────────────────────────────
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    if (urlToken) {
        sessionStorage.setItem('wtb_token', urlToken);
        urlParams.delete('token');
        const clean = window.location.pathname + (urlParams.toString() ? '?' + urlParams : '');
        window.history.replaceState({}, '', clean);
    }
    const AUTH_TOKEN = sessionStorage.getItem('wtb_token') || '';

    async function apiFetch(url, options = {}) {
        const headers = options.headers || {};
        if (AUTH_TOKEN) {
            headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
        }
        options.headers = headers;

        const res = await fetch(url, options);
        if (res.status === 401 || res.status === 503) {
            const badge = document.getElementById('mode-badge');
            if (badge) {
                badge.textContent = 'UNAUTHORIZED';
                badge.className = 'mode-pill mode-sl';
            }
            showToast('Authentication required. Append ?token=<WEB_AUTH_TOKEN>', 'error');
            throw new Error(`Unauthorized (${res.status})`);
        }
        return res;
    }

    // Update log download link with token
    const dlLink = document.getElementById('download-log-btn');
    if (dlLink && AUTH_TOKEN) {
        dlLink.href = `/api/download/bot_debug.log?token=${encodeURIComponent(AUTH_TOKEN)}`;
    }

    // ── 2. Toast Notifications ─────────────────────────────────────────────
    const toastContainer = document.getElementById('toast-container');
    function showToast(message, type = 'info') {
        if (!toastContainer) return;
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        toastContainer.appendChild(toast);
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(12px)';
            toast.style.transition = 'all 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, 3500);
    }

    // ── 3. Theme Management ────────────────────────────────────────────────
    const themeBtn = document.getElementById('theme-toggle-btn');
    const themeIcon = document.getElementById('theme-toggle-icon');
    const themeText = document.getElementById('theme-toggle-text');

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('wtb_theme', theme);
        const isDark = theme === 'dark';
        if (themeIcon) themeIcon.textContent = isDark ? '🌙' : '☀️';
        if (themeText) themeText.textContent = isDark ? 'Dark' : 'Light';
    }

    const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
    applyTheme(currentTheme);

    if (themeBtn) {
        themeBtn.addEventListener('click', () => {
            const active = document.documentElement.getAttribute('data-theme') || 'light';
            applyTheme(active === 'light' ? 'dark' : 'light');
        });
    }

    // ── 4. Navigation & Tab Switching ──────────────────────────────────────
    let activeTabId = 'live';
    const desktopTabs = document.querySelectorAll('.nav-tab');
    const drawerTabs = document.querySelectorAll('.drawer-item');
    const tabPanes = document.querySelectorAll('.tab-pane');

    const mobileToggle = document.getElementById('mobile-toggle');
    const drawerClose = document.getElementById('drawer-close');
    const mobileDrawer = document.getElementById('mobile-drawer');
    const drawerBackdrop = document.getElementById('drawer-backdrop');

    function switchTab(targetId) {
        activeTabId = targetId;
        desktopTabs.forEach(t => t.classList.toggle('active', t.getAttribute('data-tab') === targetId));
        drawerTabs.forEach(t => t.classList.toggle('active', t.getAttribute('data-tab') === targetId));
        tabPanes.forEach(p => p.classList.toggle('active', p.id === `tab-${targetId}`));

        // Close mobile drawer if open
        if (mobileDrawer) mobileDrawer.classList.remove('open');
        if (drawerBackdrop) drawerBackdrop.classList.remove('active');

        // Immediate fetch for newly activated tab
        if (targetId === 'live') fetchDashboard();
        else if (targetId === 'history') fetchHistory();
        else if (targetId === 'whales') fetchWhales();
        else if (targetId === 'settings') {
            fetchSettings();
            fetchTelemetry();
            fetchLogInfo();
        }
    }

    desktopTabs.forEach(btn => btn.addEventListener('click', () => switchTab(btn.getAttribute('data-tab'))));
    drawerTabs.forEach(btn => btn.addEventListener('click', () => switchTab(btn.getAttribute('data-tab'))));

    if (mobileToggle && mobileDrawer && drawerBackdrop) {
        mobileToggle.addEventListener('click', () => {
            mobileDrawer.classList.add('open');
            drawerBackdrop.classList.add('active');
        });
    }

    if (drawerClose && mobileDrawer && drawerBackdrop) {
        drawerClose.addEventListener('click', () => {
            mobileDrawer.classList.remove('open');
            drawerBackdrop.classList.remove('active');
        });
    }

    if (drawerBackdrop && mobileDrawer) {
        drawerBackdrop.addEventListener('click', () => {
            mobileDrawer.classList.remove('open');
            drawerBackdrop.classList.remove('active');
        });
    }

    // ── 5. Helper Formatters ───────────────────────────────────────────────
    const formatUSD = (val) => {
        const num = parseFloat(val) || 0;
        return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };

    const formatShortAddr = (addr) => {
        if (!addr || addr.length <= 10) return addr || '--';
        return `${addr.slice(0, 4)}...${addr.slice(-4)}`;
    };

    const copyToClipboard = (text) => {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(() => showToast('Address copied to clipboard!'));
        } else {
            const el = document.createElement('textarea');
            el.value = text;
            document.body.appendChild(el);
            el.select();
            document.execCommand('copy');
            document.body.removeChild(el);
            showToast('Address copied to clipboard!');
        }
    };

    // ── 6. Tab 1: Live Trading Desk ────────────────────────────────────────
    let dashboardTimer = null;

    async function fetchDashboard() {
        try {
            const res = await apiFetch('/api/dashboard');
            const data = await res.json();

            // Mode badge
            const modeBadge = document.getElementById('mode-badge');
            if (modeBadge) {
                const mode = (data.mode || 'PAPER').toUpperCase();
                modeBadge.textContent = mode;
                modeBadge.className = `mode-pill ${mode === 'LIVE' ? 'mode-live' : 'mode-paper'}`;
            }

            // Headline metrics
            const totalValEl = document.getElementById('dash-total-val');
            if (totalValEl) totalValEl.textContent = formatUSD(data.total_value);

            const cashEl = document.getElementById('dash-cash');
            if (cashEl) cashEl.textContent = formatUSD(data.available_cash);

            const netPnlEl = document.getElementById('dash-net-pnl');
            if (netPnlEl) {
                const pnl = data.net_profit || 0;
                netPnlEl.textContent = (pnl >= 0 ? '+' : '') + formatUSD(pnl);
                netPnlEl.className = `kpi-value ${pnl >= 0 ? 'text-emerald' : 'text-rose'}`;
            }

            const activeCountEl = document.getElementById('dash-active-trades');
            if (activeCountEl) {
                activeCountEl.textContent = `${data.active_trades_count || 0} / ${data.max_trades || 10}`;
            }

            // Badges in nav
            const liveCount = data.active_trades_count || 0;
            const b1 = document.getElementById('badge-live-count');
            if (b1) b1.textContent = liveCount;
            const b2 = document.getElementById('m-badge-live');
            if (b2) b2.textContent = liveCount;
            const b3 = document.getElementById('active-positions-badge');
            if (b3) b3.textContent = `${liveCount} Open`;

            // Render active positions
            renderActivePositions(data.trades || []);
        } catch (e) {
            console.error('Error fetching dashboard:', e);
        }
    }

    function renderActivePositions(trades) {
        const container = document.getElementById('live-trades-container');
        if (!container) return;

        if (!trades || trades.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">🐋</div>
                    <h3 class="empty-state-title">No Active Copy-Trade Positions</h3>
                    <p class="empty-state-desc">The bot is scanning 229 Solana alpha whales in real-time. When a verified whale swaps, new positions will appear here instantly.</p>
                </div>
            `;
            return;
        }

        let html = '<div class="positions-grid">';
        trades.forEach(t => {
            const symbol = t.symbol || t.token_symbol || 'TOKEN';
            const mint = t.mint || t.token_mint || '';
            const whale = t.whale_wallet || t.whale || '';
            const entryPrice = parseFloat(t.entry_price || 0);
            const currentPrice = parseFloat(t.current_price || entryPrice);
            const sizeUSD = parseFloat(t.trade_size || t.trade_size_usd || 0);
            
            let pnlUSD = parseFloat(t.profit_usd || 0);
            let pnlPct = parseFloat(t.profit_pct || 0);
            if (pnlUSD === 0 && entryPrice > 0 && currentPrice > 0) {
                pnlPct = ((currentPrice - entryPrice) / entryPrice) * 100;
                pnlUSD = (pnlPct / 100) * sizeUSD;
            }

            const isProfitable = pnlUSD >= 0;
            const pnlClass = isProfitable ? 'positive' : 'negative';
            const sign = isProfitable ? '+' : '';

            html += `
                <div class="position-card">
                    <div class="position-header">
                        <div class="position-token-group">
                            <div class="token-icon-box">⚡</div>
                            <div>
                                <div class="position-token-name">${symbol}</div>
                                <span class="copy-address" onclick="copyAddress('${mint}')" title="Click to copy mint address">
                                    ${formatShortAddr(mint)} <span class="copy-btn-mini">📋</span>
                                </span>
                            </div>
                        </div>
                        <span class="badge-pnl ${pnlClass}">${sign}${pnlPct.toFixed(2)}%</span>
                    </div>

                    <div class="position-meta-row">
                        <span class="meta-name">Copied Whale</span>
                        <span class="copy-address" onclick="copyAddress('${whale}')" title="Click to copy whale address">
                            ${formatShortAddr(whale)} <span class="copy-btn-mini">📋</span>
                        </span>
                    </div>

                    <div class="position-meta-row">
                        <span class="meta-name">Entry Price</span>
                        <span class="meta-val">$${entryPrice > 1 ? entryPrice.toFixed(4) : entryPrice.toFixed(6)}</span>
                    </div>

                    <div class="position-meta-row">
                        <span class="meta-name">Current Price</span>
                        <span class="meta-val">$${currentPrice > 1 ? currentPrice.toFixed(4) : currentPrice.toFixed(6)}</span>
                    </div>

                    <div class="position-meta-row">
                        <span class="meta-name">Position Size</span>
                        <span class="meta-val">${formatUSD(sizeUSD)}</span>
                    </div>

                    <div class="position-pnl-row">
                        <span class="meta-name">Net Return</span>
                        <span class="badge-pnl ${pnlClass}" style="font-size: 0.9rem;">
                            ${sign}${formatUSD(pnlUSD)} (${sign}${pnlPct.toFixed(2)}%)
                        </span>
                    </div>

                    <div style="display: flex; gap: 8px; margin-top: 8px;">
                        <a href="https://solscan.io/token/${mint}" target="_blank" rel="noopener" class="btn btn-secondary" style="flex: 1; justify-content: center; font-size: 0.75rem;">
                            Solscan ↗
                        </a>
                        <a href="https://dexscreener.com/solana/${mint}" target="_blank" rel="noopener" class="btn btn-secondary" style="flex: 1; justify-content: center; font-size: 0.75rem;">
                            DexScreener ↗
                        </a>
                    </div>
                </div>
            `;
        });
        html += '</div>';
        container.innerHTML = html;
    }

    // Expose copyAddress helper globally for inline onclick
    window.copyAddress = copyToClipboard;

    const refreshLiveBtn = document.getElementById('refresh-live-btn');
    if (refreshLiveBtn) refreshLiveBtn.addEventListener('click', fetchDashboard);

    // ── 7. Tab 2: Trade History & its P&L ──────────────────────────────────
    let allHistoryTrades = [];
    const historyFilter = document.getElementById('history-filter');
    const historySearch = document.getElementById('history-search');

    async function fetchHistory() {
        const mode = historyFilter ? historyFilter.value : 'ALL';
        try {
            const res = await apiFetch(`/api/history?mode=${encodeURIComponent(mode)}`);
            const data = await res.json();

            // Metrics
            const totalEl = document.getElementById('hist-total-trades');
            if (totalEl) totalEl.textContent = data.total_trades || 0;

            const wrEl = document.getElementById('hist-win-rate');
            if (wrEl) wrEl.textContent = (data.win_rate || 0).toFixed(1) + '%';

            const pnlEl = document.getElementById('hist-realized-pnl');
            if (pnlEl) {
                const pnl = data.realized_profit || 0;
                pnlEl.textContent = (pnl >= 0 ? '+' : '') + formatUSD(pnl);
                pnlEl.className = `kpi-value ${pnl >= 0 ? 'text-emerald' : 'text-rose'}`;
            }

            const avgEl = document.getElementById('hist-avg-pnl');
            if (avgEl) {
                const avg = data.avg_profit || 0;
                avgEl.textContent = (avg >= 0 ? '+' : '') + formatUSD(avg);
                avgEl.className = `kpi-value ${avg >= 0 ? 'text-emerald' : 'text-rose'}`;
            }

            allHistoryTrades = data.trades || [];
            filterAndRenderHistory();
        } catch (e) {
            console.error('Error fetching trade history:', e);
        }
    }

    function filterAndRenderHistory() {
        const tbody = document.getElementById('history-tbody');
        if (!tbody) return;

        const query = (historySearch ? historySearch.value : '').trim().toLowerCase();

        let filtered = allHistoryTrades;
        if (query) {
            filtered = filtered.filter(t => {
                const symbol = (t.symbol || t.token_symbol || '').toLowerCase();
                const mint = (t.mint || t.token_mint || '').toLowerCase();
                const whale = (t.whale_wallet || t.whale || '').toLowerCase();
                return symbol.includes(query) || mint.includes(query) || whale.includes(query);
            });
        }

        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="9" class="table-empty">No historical trades found matching criteria.</td></tr>`;
            return;
        }

        let html = '';
        filtered.forEach(t => {
            const timeStr = t.timestamp ? new Date(t.timestamp * 1000).toISOString().replace('T', ' ').slice(0, 19) : '--';
            const mode = (t.trade_mode || 'PAPER').toUpperCase();
            const symbol = t.symbol || t.token_symbol || 'TOKEN';
            const mint = t.mint || t.token_mint || '';
            const whale = t.whale_wallet || t.whale || '';
            const size = parseFloat(t.trade_size || t.trade_size_usd || 0);
            const entryPrice = parseFloat(t.entry_price || 0);
            const exitPrice = parseFloat(t.exit_price || entryPrice);
            const duration = t.hold_duration_sec ? `${Math.round(t.hold_duration_sec / 60)}m` : (t.duration || '--');
            const exitReason = (t.exit_reason || 'MANUAL').toUpperCase();

            // P&L calculation
            const pnl = parseFloat(t.real_net_profit_usd !== undefined ? t.real_net_profit_usd : (t.net_profit_usd || t.profit_usd || 0));
            const pnlPct = entryPrice > 0 ? ((exitPrice - entryPrice) / entryPrice) * 100 : 0;
            const isWin = pnl >= 0;
            const pnlBadgeClass = isWin ? 'positive' : 'negative';
            const sign = isWin ? '+' : '';

            // Exit reason styling
            let exitBadgeClass = 'exit-to';
            if (exitReason.includes('PROFIT') || exitReason === 'TP') exitBadgeClass = 'exit-tp';
            else if (exitReason.includes('LOSS') || exitReason === 'SL') exitBadgeClass = 'exit-sl';
            else if (exitReason.includes('TRAILING') || exitReason === 'TS') exitBadgeClass = 'exit-ts';

            html += `
                <tr>
                    <td class="mono-font">${timeStr}</td>
                    <td><span class="mode-pill ${mode === 'LIVE' ? 'mode-live' : 'mode-paper'}">${mode}</span></td>
                    <td>
                        <strong>${symbol}</strong>
                        ${mint ? `<span class="copy-address" onclick="copyAddress('${mint}')" title="Copy mint address">${formatShortAddr(mint)} <span class="copy-btn-mini">📋</span></span>` : ''}
                    </td>
                    <td>
                        <span class="copy-address" onclick="copyAddress('${whale}')" title="Copy whale address">
                            ${formatShortAddr(whale)} <span class="copy-btn-mini">📋</span>
                        </span>
                    </td>
                    <td class="mono-font">${formatUSD(size)}</td>
                    <td class="mono-font">$${entryPrice.toFixed(4)} → $${exitPrice.toFixed(4)}</td>
                    <td>${duration}</td>
                    <td><span class="badge-exit ${exitBadgeClass}">${exitReason}</span></td>
                    <td><span class="badge-pnl ${pnlBadgeClass}">${sign}${formatUSD(pnl)} (${sign}${pnlPct.toFixed(1)}%)</span></td>
                </tr>
            `;
        });
        tbody.innerHTML = html;
    }

    if (historyFilter) historyFilter.addEventListener('change', fetchHistory);
    if (historySearch) historySearch.addEventListener('input', filterAndRenderHistory);

    // CSV Export
    const exportCsvBtn = document.getElementById('export-history-csv');
    if (exportCsvBtn) {
        exportCsvBtn.addEventListener('click', () => {
            if (!allHistoryTrades || allHistoryTrades.length === 0) {
                showToast('No trade history available to export', 'error');
                return;
            }
            const headers = ['Timestamp', 'Mode', 'Token', 'Mint', 'Whale', 'Trade_Size_USD', 'Entry_Price', 'Exit_Price', 'Exit_Reason', 'Net_Profit_USD'];
            const rows = allHistoryTrades.map(t => [
                t.timestamp ? new Date(t.timestamp * 1000).toISOString() : '',
                t.trade_mode || 'PAPER',
                t.symbol || '',
                t.mint || '',
                t.whale_wallet || '',
                t.trade_size || 0,
                t.entry_price || 0,
                t.exit_price || 0,
                t.exit_reason || '',
                t.real_net_profit_usd !== undefined ? t.real_net_profit_usd : (t.net_profit_usd || 0)
            ]);

            const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
            const encodedUri = encodeURI(csvContent);
            const link = document.createElement('a');
            link.setAttribute('href', encodedUri);
            link.setAttribute('download', `wtb_trade_history_${new Date().toISOString().slice(0,10)}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            showToast('Trade history exported to CSV!');
        });
    }

    // ── 8. Tab 3: Whale Analyze ────────────────────────────────────────────
    let allWhales = [];
    const whaleSearch = document.getElementById('whale-search');

    async function fetchWhales() {
        try {
            const res = await apiFetch('/api/whales?mode=ALL');
            const data = await res.json();

            const totalEl = document.getElementById('whale-total-db');
            if (totalEl) totalEl.textContent = data.total_db || 0;

            const wlEl = document.getElementById('whale-whitelists');
            if (wlEl) wlEl.textContent = data.active_whitelists || 0;

            // Update tab badge
            const count = data.active_whitelists || data.total_db || 0;
            const b1 = document.getElementById('badge-whale-count');
            if (b1) b1.textContent = count;
            const b2 = document.getElementById('m-badge-whales');
            if (b2) b2.textContent = count;

            allWhales = data.whales || [];

            // Compute top WR and total signals
            if (allWhales.length > 0) {
                const maxWr = Math.max(...allWhales.map(w => w.win_rate || 0));
                const topWrEl = document.getElementById('whale-top-wr');
                if (topWrEl) topWrEl.textContent = maxWr.toFixed(1) + '%';

                const totalSignals = allWhales.reduce((acc, w) => acc + (w.total || 0), 0);
                const sigEl = document.getElementById('whale-signals-count');
                if (sigEl) sigEl.textContent = totalSignals;
            }

            filterAndRenderWhales();
        } catch (e) {
            console.error('Error fetching whale intelligence:', e);
        }
    }

    function filterAndRenderWhales() {
        const tbody = document.getElementById('whale-tbody');
        if (!tbody) return;

        const query = (whaleSearch ? whaleSearch.value : '').trim().toLowerCase();

        let filtered = allWhales;
        if (query) {
            filtered = filtered.filter(w => (w.wallet || '').toLowerCase().includes(query));
        }

        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" class="table-empty">No tracked whales match the search query.</td></tr>`;
            return;
        }

        let html = '';
        filtered.forEach(w => {
            const wallet = w.wallet || '';
            const status = w.status || 'WHITELIST';
            const wr = parseFloat(w.win_rate || 0);
            const total = w.total || 0;
            const profit = parseFloat(w.profit || 0);
            const modes = w.modes || 'PAPER';
            const isProfit = profit >= 0;
            const sign = isProfit ? '+' : '';

            html += `
                <tr>
                    <td>
                        <span class="copy-address" onclick="copyAddress('${wallet}')" title="Copy wallet address">
                            ${formatShortAddr(wallet)} <span class="copy-btn-mini">📋</span>
                        </span>
                    </td>
                    <td><span class="mode-pill mode-paper">${status}</span></td>
                    <td><span class="badge-pnl ${wr >= 60 ? 'positive' : 'negative'}">${wr.toFixed(1)}%</span></td>
                    <td class="mono-font">${total}</td>
                    <td><span class="badge-pnl ${isProfit ? 'positive' : 'negative'}">${sign}${formatUSD(profit)}</span></td>
                    <td><span class="pill-info">${modes}</span></td>
                    <td>
                        <div style="display: flex; gap: 6px;">
                            <a href="https://solscan.io/account/${wallet}" target="_blank" rel="noopener" class="btn btn-secondary" style="padding: 4px 8px; font-size: 0.72rem;">
                                Solscan ↗
                            </a>
                            <a href="https://gmgn.ai/sol/address/${wallet}" target="_blank" rel="noopener" class="btn btn-secondary" style="padding: 4px 8px; font-size: 0.72rem;">
                                GMGN ↗
                            </a>
                        </div>
                    </td>
                </tr>
            `;
        });
        tbody.innerHTML = html;
    }

    if (whaleSearch) whaleSearch.addEventListener('input', filterAndRenderWhales);

    const refreshWhalesBtn = document.getElementById('refresh-whales-btn');
    if (refreshWhalesBtn) refreshWhalesBtn.addEventListener('click', fetchWhales);

    // ── 9. Tab 4: Settings & Diagnostics ───────────────────────────────────
    let currentSettings = {};
    let settingsSpec = [];

    async function fetchSettings() {
        const container = document.getElementById('settings-container');
        if (!container) return;

        try {
            const res = await apiFetch('/api/settings');
            const data = await res.json();
            currentSettings = data.settings || {};
            settingsSpec = data.spec || [];

            renderSettingsForm(settingsSpec, currentSettings);
        } catch (e) {
            console.error('Error loading settings:', e);
            if (container) container.innerHTML = `<div class="table-empty text-rose">Failed to load configuration schema.</div>`;
        }
    }

    function renderSettingsForm(spec, values) {
        const container = document.getElementById('settings-container');
        if (!container) return;

        // Group settings by their spec category
        const groups = {};
        spec.forEach(item => {
            const g = item.group || 'General';
            if (!groups[g]) groups[g] = [];
            groups[g].push(item);
        });

        let html = '';
        for (const [groupName, items] of Object.entries(groups)) {
            html += `
                <div class="settings-group">
                    <div class="group-title">
                        <span>🛡️</span> ${groupName} Controls
                    </div>
            `;

            items.forEach(item => {
                const key = item.key;
                const val = values[key] !== undefined ? values[key] : item.default;
                const help = item.help || '';
                const type = item.type;

                let inputHtml = '';
                if (type === 'bool') {
                    const isChecked = val === true || val === 'true' || val === 1 || val === '1';
                    inputHtml = `
                        <select class="setting-input setting-field" data-key="${key}" data-type="bool">
                            <option value="true" ${isChecked ? 'selected' : ''}>ENABLED</option>
                            <option value="false" ${!isChecked ? 'selected' : ''}>DISABLED</option>
                        </select>
                    `;
                } else if (type === 'choice') {
                    const options = item.options || ['PAPER', 'LIVE'];
                    inputHtml = `
                        <select class="setting-input setting-field" data-key="${key}" data-type="choice">
                            ${options.map(opt => `<option value="${opt}" ${String(val).toUpperCase() === opt ? 'selected' : ''}>${opt}</option>`).join('')}
                        </select>
                    `;
                } else if (type === 'int') {
                    inputHtml = `
                        <input type="number" step="1" min="${item.min || 0}" max="${item.max || 99999}" 
                               class="setting-input setting-field" data-key="${key}" data-type="int" value="${val}">
                    `;
                } else {
                    inputHtml = `
                        <input type="number" step="0.1" min="${item.min || -100}" max="${item.max || 99999}" 
                               class="setting-input setting-field" data-key="${key}" data-type="float" value="${val}">
                    `;
                }

                html += `
                    <div class="settings-row">
                        <div class="setting-info">
                            <label class="setting-label">${key}</label>
                            <span class="setting-help">${help}</span>
                        </div>
                        <div class="setting-input-wrapper">
                            ${inputHtml}
                        </div>
                    </div>
                `;
            });

            html += `</div>`;
        }

        container.innerHTML = html;
    }

    // Save Settings
    const saveSettingsBtn = document.getElementById('save-settings-btn');
    if (saveSettingsBtn) {
        saveSettingsBtn.addEventListener('click', async () => {
            const fields = document.querySelectorAll('.setting-field');
            let updateCount = 0;
            let errorCount = 0;

            saveSettingsBtn.disabled = true;
            saveSettingsBtn.innerHTML = 'Saving...';

            for (const field of fields) {
                const key = field.getAttribute('data-key');
                const type = field.getAttribute('data-type');
                let value = field.value;

                if (type === 'bool') value = (value === 'true');
                else if (type === 'int') value = parseInt(value, 10);
                else if (type === 'float') value = parseFloat(value);

                // Only send if changed from cached value
                if (currentSettings[key] !== value) {
                    try {
                        const res = await apiFetch('/api/settings', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ key, value })
                        });
                        const data = await res.json();
                        if (data.success) {
                            currentSettings[key] = value;
                            updateCount++;
                        } else {
                            errorCount++;
                            showToast(`Failed to update ${key}: ${data.message}`, 'error');
                        }
                    } catch (e) {
                        errorCount++;
                    }
                }
            }

            saveSettingsBtn.disabled = false;
            saveSettingsBtn.innerHTML = '<span class="btn-icon">💾</span> Save & Apply Settings';

            if (updateCount > 0 && errorCount === 0) {
                showToast(`Successfully updated and hot-reloaded ${updateCount} settings!`);
            } else if (updateCount === 0 && errorCount === 0) {
                showToast('No configuration changes detected.', 'info');
            }
        });
    }

    // Log Metadata
    async function fetchLogInfo() {
        try {
            const res = await apiFetch('/api/log-info');
            const data = await res.json();

            const sizeEl = document.getElementById('log-size-val');
            if (sizeEl) sizeEl.textContent = `${data.size_mb || 0} MB`;

            const modEl = document.getElementById('log-updated-val');
            if (modEl) modEl.textContent = data.modified || '--';
        } catch (e) {
            console.error('Error loading log info:', e);
        }
    }

    // Telemetry & Rate Budgets
    async function fetchTelemetry() {
        try {
            const res = await apiFetch('/api/telemetry');
            const data = await res.json();

            const budgets = data.budgets || {};
            const metersContainer = document.getElementById('budget-meters-container');
            if (metersContainer) {
                let html = '';
                for (const [name, stats] of Object.entries(budgets)) {
                    const count = stats.count_current_window || 0;
                    const total = stats.total_requests || 0;
                    const max = name === 'Jupiter' ? 600 : (name === 'DexScreener' ? 300 : 100);
                    const pct = Math.min(100, Math.round((count / max) * 100));

                    let fillClass = '';
                    if (pct > 80) fillClass = 'danger';
                    else if (pct > 60) fillClass = 'warning';

                    html += `
                        <div class="meter-item">
                            <div class="meter-header">
                                <span class="meter-name">${name}</span>
                                <span class="meter-stat">${count} / ${max} req/min (${pct}%)</span>
                            </div>
                            <div class="meter-bar-track">
                                <div class="meter-bar-fill ${fillClass}" style="width: ${pct}%"></div>
                            </div>
                        </div>
                    `;
                }
                metersContainer.innerHTML = html || '<div class="table-empty">No active connection budgets.</div>';
            }

            // Decoder Stats
            const dec = data.decoder || {};
            const dTot = document.getElementById('dec-total');
            if (dTot) dTot.textContent = dec.total_attempts || 0;

            const dSuc = document.getElementById('dec-success');
            if (dSuc) dSuc.textContent = dec.success || 0;

            const dFb = document.getElementById('dec-fallback');
            if (dFb) dFb.textContent = dec.fallback_success || 0;

            const dFail = document.getElementById('dec-failed');
            if (dFail) dFail.textContent = dec.failed_after_retries || 0;

        } catch (e) {
            console.error('Error fetching telemetry:', e);
        }
    }

    // ── 10. Polling Lifecycles ─────────────────────────────────────────────
    // Initial fetch
    fetchDashboard();

    // High frequency poll for Live Desk (every 4 seconds)
    setInterval(() => {
        if (activeTabId === 'live') {
            fetchDashboard();
        }
    }, 4000);

    // Medium frequency background telemetry poll (every 10 seconds)
    setInterval(() => {
        if (activeTabId === 'settings') {
            fetchTelemetry();
            fetchLogInfo();
        }
    }, 10000);
});
