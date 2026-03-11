/* ── PolyEdge v2 — Frontend App ─────────────────────────────── */

var API = window.location.origin + '/api';

/* ── State ─────────────────────────────────────────────────── */
var state = {
  tab: 'overview',
  data: null,
  walletDetail: null,
  charts: {},
  refreshTimer: null,
  sortCol: null,
  sortAsc: true,
};

/* ── Helpers ───────────────────────────────────────────────── */
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function fmt(n, decimals) {
  if (n == null) return '—';
  decimals = decimals != null ? decimals : 2;
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtUsd(n) {
  if (n == null) return '—';
  var prefix = n >= 0 ? '+$' : '-$';
  return prefix + fmt(Math.abs(n));
}

function fmtPct(n) {
  if (n == null) return '—';
  return fmt(n, 1) + '%';
}

function pnlClass(n) {
  if (n == null || n === 0) return 'neutral';
  return n > 0 ? 'positive' : 'negative';
}

function truncAddr(addr) {
  if (!addr) return '';
  return addr.slice(0, 6) + '...' + addr.slice(-4);
}

function timeAgo(ts) {
  if (!ts) return 'never';
  var d = new Date(ts + (ts.indexOf('Z') === -1 && ts.indexOf('+') === -1 ? 'Z' : ''));
  var secs = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  return Math.floor(secs / 86400) + 'd ago';
}

function statusBadge(status) {
  return '<span class="status-badge ' + (status || 'open') + '">' + (status || 'open') + '</span>';
}

/* ── Data fetching ─────────────────────────────────────────── */
async function fetchDashboard() {
  try {
    var resp = await fetch(API + '/dashboard');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    state.data = await resp.json();
    renderCurrentTab();
    updateSidebarStatus();
  } catch (e) {
    console.error('Failed to fetch dashboard:', e);
    $('#main-content').innerHTML = '<div class="empty-state">Failed to load data. Is the server running?</div>';
  }
}

async function fetchWalletDetail(id) {
  try {
    var resp = await fetch(API + '/wallets/' + id);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    state.walletDetail = await resp.json();
    renderWalletOverlay();
    $('#wallet-overlay').classList.add('open');
  } catch (e) {
    console.error('Failed to fetch wallet detail:', e);
  }
}

async function triggerAction(endpoint) {
  try {
    var resp = await fetch(API + '/' + endpoint, { method: 'POST' });
    var result = await resp.json();
    alert(JSON.stringify(result, null, 2));
    fetchDashboard();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

/* ── Sidebar status ────────────────────────────────────────── */
function updateSidebarStatus() {
  if (!state.data) return;
  var log = state.data.scan_log;
  var dot = $('#status-dot');
  var text = $('#last-scan-text');
  if (log && log.length > 0) {
    dot.className = 'status-dot online';
    text.textContent = 'Last: ' + timeAgo(log[0].timestamp);
  } else {
    dot.className = 'status-dot offline';
    text.textContent = 'No scans yet';
  }
}

/* ── Navigation ────────────────────────────────────────────── */
function switchTab(tab) {
  state.tab = tab;
  $$('.nav-item').forEach(function(el) {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  renderCurrentTab();
}

function renderCurrentTab() {
  if (!state.data) return;
  destroyCharts();
  switch (state.tab) {
    case 'overview': renderOverview(); break;
    case 'wallets': renderWallets(); break;
    case 'trades': renderTrades(); break;
    case 'analytics': renderAnalytics(); break;
    case 'settings': renderSettings(); break;
  }
}

function destroyCharts() {
  Object.keys(state.charts).forEach(function(k) {
    if (state.charts[k]) { state.charts[k].destroy(); delete state.charts[k]; }
  });
}

/* ── Overview ──────────────────────────────────────────────── */
function renderOverview() {
  var d = state.data;
  var g = d.global_stats;
  var html = '';

  // KPI row
  html += '<div class="kpi-row">';
  html += kpiCard('Total Sim P&L', fmtUsd(g.total_pnl), pnlClass(g.total_pnl), 'Realized: ' + fmtUsd(g.realized) + ' | Unrealized: ' + fmtUsd(g.unrealized));
  html += kpiCard('Win Rate', fmtPct(g.win_rate), g.win_rate >= 50 ? 'positive' : 'negative', g.wins + 'W / ' + g.losses + 'L');
  html += kpiCard('Open Positions', fmt(g.open_positions, 0), 'neutral', '');
  html += kpiCard('Total Trades', fmt(g.total_trades, 0), 'neutral', '');
  html += kpiCard('Active Wallets', g.active_wallets + ' / ' + g.total_wallets, 'neutral', '');
  html += '</div>';

  // Two columns: equity curve + activity
  html += '<div class="two-col">';
  html += '<div class="panel"><div class="panel-header"><h2>Equity Curve (7d)</h2></div><div class="panel-body"><div class="chart-container"><canvas id="equity-chart"></canvas></div></div></div>';
  html += '<div class="panel"><div class="panel-header"><h2>Recent Activity</h2></div><div class="panel-body">' + renderActivityFeed(d.recent_trades.slice(0, 10)) + '</div></div>';
  html += '</div>';

  // Wallet leaderboard
  html += '<div class="panel"><div class="panel-header"><h2>Wallet Leaderboard</h2></div><div class="panel-body" style="padding:0;overflow-x:auto;">';
  html += renderWalletTable(d.wallets);
  html += '</div></div>';

  $('#main-content').innerHTML = html;

  // Draw equity chart
  if (d.snapshots && d.snapshots.length > 0) {
    drawEquityChart('equity-chart', d.snapshots);
  }
}

function kpiCard(label, value, colorClass, sub) {
  return '<div class="kpi-card"><div class="kpi-label">' + label + '</div><div class="kpi-value ' + colorClass + '">' + value + '</div>' + (sub ? '<div class="kpi-sub">' + sub + '</div>' : '') + '</div>';
}

function renderActivityFeed(trades) {
  if (!trades || trades.length === 0) return '<div class="empty-state">No recent trades</div>';
  var html = '<ul class="activity-feed">';
  trades.forEach(function(t) {
    html += '<li class="activity-item">';
    html += '<span class="activity-side ' + t.side + '">' + t.side + '</span>';
    html += '<span class="activity-market">' + (t.wallet_username || truncAddr(t.wallet_address)) + ' — ' + (t.market_title || 'Unknown') + '</span>';
    html += '<span class="activity-pnl ' + pnlClass(t.sim_pnl) + '">' + fmtUsd(t.sim_pnl) + '</span>';
    html += '</li>';
  });
  html += '</ul>';
  return html;
}

function renderWalletTable(wallets) {
  var html = '<table class="data-table"><thead><tr>';
  html += '<th>#</th><th>Wallet</th><th>Score</th><th>Sim P&L</th><th>Win Rate</th><th>Open</th><th>Trades</th><th>Last Active</th>';
  html += '</tr></thead><tbody>';
  wallets.forEach(function(w, i) {
    var pnl = w.sim_total_pnl || 0;
    html += '<tr class="clickable" onclick="openWallet(' + w.id + ')">';
    html += '<td>' + (i + 1) + '</td>';
    html += '<td><strong>' + (w.username || truncAddr(w.address)) + '</strong><br><span style="color:var(--text-muted);font-size:var(--fs-xs)">' + truncAddr(w.address) + '</span></td>';
    html += '<td>' + fmt(w.score, 1) + '</td>';
    html += '<td class="' + pnlClass(pnl) + '">' + fmtUsd(pnl) + '</td>';
    html += '<td>' + fmtPct(w.sim_win_rate) + '</td>';
    html += '<td>' + (w.sim_open_positions || 0) + '</td>';
    html += '<td>' + (w.sim_total_trades || 0) + '</td>';
    html += '<td>' + timeAgo(w.last_scanned) + '</td>';
    html += '</tr>';
  });
  html += '</tbody></table>';
  return html;
}

/* ── Wallets Grid ──────────────────────────────────────────── */
function renderWallets() {
  var d = state.data;
  var html = '<h2 style="margin-bottom:var(--gap-lg)">All Wallets (' + d.wallets.length + ')</h2>';
  html += '<div class="wallet-grid">';
  d.wallets.forEach(function(w) {
    var pnl = w.sim_total_pnl || 0;
    html += '<div class="wallet-card" onclick="openWallet(' + w.id + ')">';
    html += '<div class="wc-header"><div><div class="wc-name">' + (w.username || 'Anonymous') + '</div><div class="wc-address">' + truncAddr(w.address) + '</div></div><span class="wc-score">' + fmt(w.score, 1) + '</span></div>';
    html += '<div class="wc-pnl ' + pnlClass(pnl) + '">' + fmtUsd(pnl) + '</div>';
    html += '<div class="wc-stats">';
    html += '<div><div class="stat-label">Win Rate</div><div>' + fmtPct(w.sim_win_rate) + '</div></div>';
    html += '<div><div class="stat-label">Open</div><div>' + (w.sim_open_positions || 0) + '</div></div>';
    html += '<div><div class="stat-label">Trades</div><div>' + (w.sim_total_trades || 0) + '</div></div>';
    html += '</div></div>';
  });
  html += '</div>';
  $('#main-content').innerHTML = html;
}

/* ── Wallet Detail Overlay ─────────────────────────────────── */
function openWallet(id) {
  fetchWalletDetail(id);
}

function closeWallet() {
  $('#wallet-overlay').classList.remove('open');
  state.walletDetail = null;
}

function renderWalletOverlay() {
  var d = state.walletDetail;
  if (!d || d.error) return;
  var w = d.wallet;

  $('#overlay-title').textContent = (w.username || truncAddr(w.address)) + ' — Detail';

  var html = '';

  // Header info
  html += '<div style="margin-bottom:var(--gap-xl)">';
  html += '<div style="display:flex;gap:var(--gap-xl);flex-wrap:wrap;margin-bottom:var(--gap-md)">';
  html += '<div><span style="color:var(--text-muted)">Address:</span> <span class="mono">' + w.address + '</span></div>';
  html += '<div><span style="color:var(--text-muted)">Score:</span> ' + fmt(w.score, 1) + '</div>';
  html += '<div><span style="color:var(--text-muted)">CSV Win Rate:</span> ' + fmtPct(w.csv_win_rate) + '</div>';
  html += '<div><span style="color:var(--text-muted)">CSV PnL:</span> ' + fmtUsd(w.csv_pnl) + '</div>';
  html += '<div><span style="color:var(--text-muted)">CSV Volume:</span> $' + fmt(w.csv_volume) + '</div>';
  html += '</div></div>';

  // KPIs
  html += '<div class="kpi-row" style="grid-template-columns:repeat(6,1fr)">';
  html += kpiCard('Sim P&L', fmtUsd(w.sim_total_pnl), pnlClass(w.sim_total_pnl), '');
  html += kpiCard('Win Rate', fmtPct(w.sim_win_rate), w.sim_win_rate >= 50 ? 'positive' : 'negative', w.sim_wins + 'W / ' + w.sim_losses + 'L');
  html += kpiCard('Open Positions', String(w.sim_open_positions || 0), 'neutral', '');
  html += kpiCard('Avg Trade Size', '$' + fmt(d.avg_trade_size), 'neutral', '');
  html += kpiCard('Best Trade', d.best_trade ? fmtUsd(d.best_trade.sim_pnl) : '—', 'positive', d.best_trade ? (d.best_trade.market_title || '').slice(0, 30) : '');
  html += kpiCard('Worst Trade', d.worst_trade ? fmtUsd(d.worst_trade.sim_pnl) : '—', 'negative', d.worst_trade ? (d.worst_trade.market_title || '').slice(0, 30) : '');
  html += '</div>';

  // Equity curve
  html += '<div class="panel"><div class="panel-header"><h2>Wallet Equity Curve</h2></div><div class="panel-body"><div class="chart-container"><canvas id="wallet-equity-chart"></canvas></div></div></div>';

  // Trade table with filters
  html += '<div class="panel"><div class="panel-header"><h2>Trades (' + d.trades.length + ')</h2>';
  html += '<div class="filter-bar" style="margin-bottom:0"><select id="wd-status-filter" onchange="filterWalletTrades()"><option value="">All</option><option value="open">Open</option><option value="won">Won</option><option value="lost">Lost</option><option value="sold">Sold</option></select></div>';
  html += '</div><div class="panel-body" style="padding:0;overflow-x:auto;">';
  html += '<table class="data-table" id="wd-trades-table"><thead><tr>';
  html += '<th>Time</th><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Current</th><th>P&L</th><th>Status</th>';
  html += '</tr></thead><tbody id="wd-trades-body">';
  html += renderWalletTradeRows(d.trades);
  html += '</tbody></table></div></div>';

  // Streaks
  html += '<div style="display:flex;gap:var(--gap-xl);margin-top:var(--gap-lg)">';
  html += '<div class="panel" style="flex:1"><div class="panel-body"><span style="color:var(--text-muted)">Max Win Streak:</span> <strong class="positive">' + d.max_win_streak + '</strong></div></div>';
  html += '<div class="panel" style="flex:1"><div class="panel-body"><span style="color:var(--text-muted)">Max Loss Streak:</span> <strong class="negative">' + d.max_loss_streak + '</strong></div></div>';
  html += '</div>';

  $('#overlay-body').innerHTML = html;

  // Draw wallet equity chart
  if (d.snapshots && d.snapshots.length > 0) {
    drawWalletEquityChart('wallet-equity-chart', d.snapshots);
  }
}

function renderWalletTradeRows(trades, statusFilter) {
  var html = '';
  trades.forEach(function(t) {
    if (statusFilter && t.sim_status !== statusFilter) return;
    html += '<tr>';
    html += '<td>' + timeAgo(t.detected_at) + '</td>';
    html += '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">' + (t.market_title || '—') + '</td>';
    html += '<td><span class="activity-side ' + t.side + '">' + t.side + '</span></td>';
    html += '<td>$' + fmt(t.sim_size) + '</td>';
    html += '<td>' + fmt(t.sim_entry_price, 4) + '</td>';
    html += '<td>' + fmt(t.sim_current_price, 4) + '</td>';
    html += '<td class="' + pnlClass(t.sim_pnl) + '">' + fmtUsd(t.sim_pnl) + '</td>';
    html += '<td>' + statusBadge(t.sim_status) + '</td>';
    html += '</tr>';
  });
  return html || '<tr><td colspan="8" class="empty-state">No trades</td></tr>';
}

function filterWalletTrades() {
  if (!state.walletDetail) return;
  var filter = document.getElementById('wd-status-filter').value;
  document.getElementById('wd-trades-body').innerHTML = renderWalletTradeRows(state.walletDetail.trades, filter);
}

/* ── Trades Tab ────────────────────────────────────────────── */
function renderTrades() {
  var d = state.data;
  var trades = d.recent_trades || [];

  var html = '<div class="panel"><div class="panel-header"><h2>All Trades</h2></div>';
  html += '<div class="panel-body" style="padding:0;overflow-x:auto;">';
  html += '<table class="data-table"><thead><tr>';
  html += '<th>Time</th><th>Wallet</th><th>Market</th><th>Side</th><th>Size</th><th>Entry</th><th>Current</th><th>P&L</th><th>Status</th>';
  html += '</tr></thead><tbody>';

  if (trades.length === 0) {
    html += '<tr><td colspan="9" class="empty-state">No trades yet. Run a scan to detect trades from tracked wallets.</td></tr>';
  } else {
    trades.forEach(function(t) {
      html += '<tr>';
      html += '<td>' + timeAgo(t.detected_at) + '</td>';
      html += '<td class="clickable" onclick="openWallet(' + t.wallet_id + ')" style="color:var(--blue)">' + (t.wallet_username || truncAddr(t.wallet_address)) + '</td>';
      html += '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">' + (t.market_title || '—') + '</td>';
      html += '<td><span class="activity-side ' + t.side + '">' + t.side + '</span></td>';
      html += '<td>$' + fmt(t.sim_size) + '</td>';
      html += '<td>' + fmt(t.sim_entry_price, 4) + '</td>';
      html += '<td>' + fmt(t.sim_current_price, 4) + '</td>';
      html += '<td class="' + pnlClass(t.sim_pnl) + '">' + fmtUsd(t.sim_pnl) + '</td>';
      html += '<td>' + statusBadge(t.sim_status) + '</td>';
      html += '</tr>';
    });
  }

  html += '</tbody></table></div></div>';
  $('#main-content').innerHTML = html;
}

/* ── Analytics Tab ─────────────────────────────────────────── */
function renderAnalytics() {
  var d = state.data;
  var html = '<h2 style="margin-bottom:var(--gap-lg)">Analytics</h2>';
  html += '<div class="analytics-grid">';

  // Win rate by wallet tier
  html += '<div class="panel"><div class="panel-header"><h2>Win Rate by Tier</h2></div><div class="panel-body"><div class="chart-container"><canvas id="tier-chart"></canvas></div></div></div>';

  // P&L distribution
  html += '<div class="panel"><div class="panel-header"><h2>P&L Distribution</h2></div><div class="panel-body"><div class="chart-container"><canvas id="pnl-dist-chart"></canvas></div></div></div>';

  // Cumulative P&L
  html += '<div class="panel"><div class="panel-header"><h2>Cumulative P&L</h2></div><div class="panel-body"><div class="chart-container"><canvas id="cum-pnl-chart"></canvas></div></div></div>';

  // Top performers table
  html += '<div class="panel"><div class="panel-header"><h2>Top 10 Performers</h2></div><div class="panel-body" style="padding:0">';
  html += '<table class="data-table"><thead><tr><th>#</th><th>Wallet</th><th>Score</th><th>Sim P&L</th><th>Win Rate</th><th>Trades</th></tr></thead><tbody>';
  var sorted = (d.wallets || []).slice().sort(function(a, b) { return (b.sim_total_pnl || 0) - (a.sim_total_pnl || 0); });
  sorted.slice(0, 10).forEach(function(w, i) {
    html += '<tr class="clickable" onclick="openWallet(' + w.id + ')">';
    html += '<td>' + (i + 1) + '</td>';
    html += '<td>' + (w.username || truncAddr(w.address)) + '</td>';
    html += '<td>' + fmt(w.score, 1) + '</td>';
    html += '<td class="' + pnlClass(w.sim_total_pnl) + '">' + fmtUsd(w.sim_total_pnl) + '</td>';
    html += '<td>' + fmtPct(w.sim_win_rate) + '</td>';
    html += '<td>' + (w.sim_total_trades || 0) + '</td>';
    html += '</tr>';
  });
  html += '</tbody></table></div></div>';

  html += '</div>';
  $('#main-content').innerHTML = html;

  // Draw analytics charts
  drawTierChart(d.wallets);
  drawPnlDistChart(d.wallets);
  drawCumPnlChart(d.snapshots);
}

/* ── Settings Tab ──────────────────────────────────────────── */
function renderSettings() {
  var d = state.data;
  var html = '<h2 style="margin-bottom:var(--gap-lg)">Settings & Scan Log</h2>';

  // Action buttons
  html += '<div class="settings-actions">';
  html += '<button class="btn btn-primary" onclick="triggerAction(\'scan\')">Scan Now</button>';
  html += '<button class="btn" onclick="triggerAction(\'update-prices\')">Update Prices</button>';
  html += '<button class="btn" onclick="triggerAction(\'full-cycle\')">Full Cycle</button>';
  html += '</div>';

  // System status
  html += '<div class="panel"><div class="panel-header"><h2>System Status</h2></div><div class="panel-body">';
  var lastScan = d.scan_log && d.scan_log.length > 0 ? d.scan_log[0] : null;
  html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:var(--gap-lg)">';
  html += '<div><div style="color:var(--text-muted);font-size:var(--fs-xs)">Last Scan</div><div>' + (lastScan ? timeAgo(lastScan.timestamp) : 'Never') + '</div></div>';
  html += '<div><div style="color:var(--text-muted);font-size:var(--fs-xs)">Scheduler</div><div>Active (30m scan / 15m prices)</div></div>';
  html += '<div><div style="color:var(--text-muted);font-size:var(--fs-xs)">Tracked Wallets</div><div>' + (d.global_stats.total_wallets || 0) + '</div></div>';
  html += '</div></div></div>';

  // Scan log table
  html += '<div class="panel"><div class="panel-header"><h2>Scan Log</h2></div><div class="panel-body" style="padding:0;overflow-x:auto;">';
  html += '<table class="data-table"><thead><tr>';
  html += '<th>Time</th><th>Type</th><th>Wallets</th><th>New Trades</th><th>Prices</th><th>Resolved</th><th>Duration</th><th>Errors</th>';
  html += '</tr></thead><tbody>';

  if (!d.scan_log || d.scan_log.length === 0) {
    html += '<tr><td colspan="8" class="empty-state">No scans recorded yet</td></tr>';
  } else {
    d.scan_log.forEach(function(s) {
      var errCount = 0;
      if (s.errors) {
        try { errCount = JSON.parse(s.errors).length; } catch(e) { errCount = s.errors ? 1 : 0; }
      }
      html += '<tr>';
      html += '<td>' + timeAgo(s.timestamp) + '</td>';
      html += '<td>' + statusBadge(s.scan_type) + '</td>';
      html += '<td>' + (s.wallets_scanned || 0) + '</td>';
      html += '<td>' + (s.new_trades || 0) + '</td>';
      html += '<td>' + (s.prices_updated || 0) + '</td>';
      html += '<td>' + (s.positions_resolved || 0) + '</td>';
      html += '<td>' + fmt(s.duration_seconds, 1) + 's</td>';
      html += '<td class="' + (errCount > 0 ? 'negative' : '') + '">' + errCount + '</td>';
      html += '</tr>';
    });
  }

  html += '</tbody></table></div></div>';
  $('#main-content').innerHTML = html;
}

/* ── Charts ────────────────────────────────────────────────── */
var chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: { mode: 'index', intersect: false },
  },
  scales: {
    x: {
      grid: { color: 'rgba(255,255,255,0.05)' },
      ticks: { color: '#6b7280', font: { size: 10 } },
    },
    y: {
      grid: { color: 'rgba(255,255,255,0.05)' },
      ticks: { color: '#6b7280', font: { size: 10 } },
    },
  },
};

function drawEquityChart(canvasId, snapshots) {
  var ctx = document.getElementById(canvasId);
  if (!ctx) return;
  var labels = snapshots.map(function(s) {
    var d = new Date(s.timestamp + (s.timestamp.indexOf('Z') === -1 ? 'Z' : ''));
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  });
  var data = snapshots.map(function(s) { return s.total_sim_pnl || 0; });

  state.charts.equity = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        data: data,
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59,130,246,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: Object.assign({}, chartDefaults),
  });
}

function drawWalletEquityChart(canvasId, snapshots) {
  var ctx = document.getElementById(canvasId);
  if (!ctx) return;
  var labels = snapshots.map(function(s) {
    var d = new Date(s.timestamp + (s.timestamp.indexOf('Z') === -1 ? 'Z' : ''));
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });
  var data = snapshots.map(function(s) { return s.sim_pnl || 0; });

  state.charts.walletEquity = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        data: data,
        borderColor: '#8b5cf6',
        backgroundColor: 'rgba(139,92,246,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: Object.assign({}, chartDefaults),
  });
}

function drawTierChart(wallets) {
  var ctx = document.getElementById('tier-chart');
  if (!ctx || !wallets) return;

  var sorted = wallets.slice().sort(function(a, b) { return (b.score || 0) - (a.score || 0); });
  var tiers = [
    { label: 'Top 10', data: sorted.slice(0, 10) },
    { label: '11-25', data: sorted.slice(10, 25) },
    { label: '26-50', data: sorted.slice(25, 50) },
    { label: '51-100', data: sorted.slice(50, 100) },
  ];

  var labels = tiers.map(function(t) { return t.label; });
  var values = tiers.map(function(t) {
    if (t.data.length === 0) return 0;
    var decided = t.data.reduce(function(s, w) { return s + (w.sim_wins || 0) + (w.sim_losses || 0); }, 0);
    var wins = t.data.reduce(function(s, w) { return s + (w.sim_wins || 0); }, 0);
    return decided > 0 ? (wins / decided * 100) : 0;
  });

  state.charts.tier = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: ['#3b82f6', '#8b5cf6', '#f59e0b', '#6b7280'],
        borderRadius: 4,
      }],
    },
    options: Object.assign({}, chartDefaults, {
      scales: Object.assign({}, chartDefaults.scales, {
        y: Object.assign({}, chartDefaults.scales.y, { beginAtZero: true, max: 100 }),
      }),
    }),
  });
}

function drawPnlDistChart(wallets) {
  var ctx = document.getElementById('pnl-dist-chart');
  if (!ctx || !wallets) return;

  var pnls = wallets.map(function(w) { return w.sim_total_pnl || 0; });
  var min = Math.min.apply(null, pnls.concat([0]));
  var max = Math.max.apply(null, pnls.concat([0]));
  var bucketSize = Math.max(1, (max - min) / 10);
  var buckets = [];
  var labels = [];
  for (var i = 0; i < 10; i++) {
    var low = min + i * bucketSize;
    var high = low + bucketSize;
    labels.push('$' + Math.round(low));
    var count = pnls.filter(function(p) { return p >= low && (i === 9 ? p <= high : p < high); }).length;
    buckets.push(count);
  }

  state.charts.pnlDist = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [{
        data: buckets,
        backgroundColor: buckets.map(function(_, i) {
          var mid = min + (i + 0.5) * bucketSize;
          return mid >= 0 ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)';
        }),
        borderRadius: 4,
      }],
    },
    options: Object.assign({}, chartDefaults, {
      scales: Object.assign({}, chartDefaults.scales, {
        y: Object.assign({}, chartDefaults.scales.y, { beginAtZero: true }),
      }),
    }),
  });
}

function drawCumPnlChart(snapshots) {
  var ctx = document.getElementById('cum-pnl-chart');
  if (!ctx || !snapshots || snapshots.length === 0) return;

  var labels = snapshots.map(function(s) {
    var d = new Date(s.timestamp + (s.timestamp.indexOf('Z') === -1 ? 'Z' : ''));
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  });
  var realized = snapshots.map(function(s) { return s.total_realized || 0; });
  var total = snapshots.map(function(s) { return s.total_sim_pnl || 0; });

  state.charts.cumPnl = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Total P&L',
          data: total,
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59,130,246,0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: 'Realized',
          data: realized,
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34,197,94,0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
    options: Object.assign({}, chartDefaults, {
      plugins: Object.assign({}, chartDefaults.plugins, { legend: { display: true, labels: { color: '#9aa0ac' } } }),
    }),
  });
}

/* ── Init ──────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', function() {
  // Nav clicks
  $$('.nav-item').forEach(function(el) {
    el.addEventListener('click', function() { switchTab(el.dataset.tab); });
  });

  // Overlay close
  $('#overlay-close').addEventListener('click', closeWallet);

  // ESC to close overlay
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeWallet();
  });

  // Initial fetch
  fetchDashboard();

  // Auto-refresh every 60 seconds
  state.refreshTimer = setInterval(fetchDashboard, 60000);
});

/* ── Global functions for onclick handlers ─────────────────── */
window.openWallet = openWallet;
window.triggerAction = triggerAction;
window.filterWalletTrades = filterWalletTrades;
