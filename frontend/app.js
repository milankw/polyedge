/* ── PolyEdge Copy Trading Dashboard ─────────────────────── */

var API = window.location.origin + '/api';
var loopRunning = false;
var currentMode = '';

/* ── Helpers ──────────────────────────────────────────────── */

function esc(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function usd(v) {
  var n = parseFloat(v) || 0;
  var sign = n >= 0 ? '+' : '';
  return sign + '$' + Math.abs(n).toFixed(2);
}

function pnlClass(v) {
  var n = parseFloat(v) || 0;
  if (n > 0) return 'green';
  if (n < 0) return 'red';
  return '';
}

function delayBadge(ms) {
  if (ms === null || ms === undefined) return '<span class="delay-badge grey">—</span>';
  var n = parseInt(ms);
  var cls = n < 500 ? 'green' : n < 2000 ? 'amber' : 'red';
  return '<span class="delay-badge ' + cls + '">' + n + 'ms</span>';
}

function truncate(s, max) {
  if (!s) return '';
  return s.length > max ? s.substring(0, max) + '…' : s;
}

function formatTime(detected_at) {
  if (!detected_at) return '';
  try {
    var d = new Date(detected_at + 'Z');
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (e) {
    return '';
  }
}

/* ── Mode Selection ──────────────────────────────────────── */

function selectMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.mode-btn').forEach(function (btn) {
    var btnMode = btn.getAttribute('data-mode');
    btn.classList.toggle('active', btnMode === mode);
  });
  // Show/hide mode comparison cards
  var comparison = document.getElementById('mode-comparison');
  if (comparison) {
    comparison.style.display = mode ? 'none' : 'grid';
  }
  // Re-poll everything with new mode filter
  pollFeed();
  pollPnl();
  pollPositions();
  pollModes();
  if (currentTab === 'analysis') pollAnalysis();
  if (currentTab === 'wallets') pollWallets();
}

function modeParam() {
  return currentMode ? '&mode=' + currentMode : '';
}

function modeParamFirst() {
  return currentMode ? '?mode=' + currentMode : '';
}

function modeBadge(mode_id) {
  if (!mode_id) return '';
  var cls = mode_id === 'strict' ? 'badge-strict' : mode_id === 'moderate' ? 'badge-moderate' : mode_id === 'aggressive' ? 'badge-aggressive' : '';
  return '<span class="mode-badge ' + cls + '">' + mode_id.toUpperCase() + '</span> ';
}

/* ── API Calls ────────────────────────────────────────────── */

async function apiGet(path) {
  try {
    var resp = await fetch(API + path);
    return await resp.json();
  } catch (e) {
    console.error('API error:', path, e);
    return null;
  }
}

async function apiPost(path, body) {
  try {
    var opts = { method: 'POST' };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    var resp = await fetch(API + path, opts);
    return await resp.json();
  } catch (e) {
    console.error('API error:', path, e);
    return null;
  }
}

/* ── Feed Rendering ───────────────────────────────────────── */

function renderFeed(feed) {
  var container = document.getElementById('feed-container');
  if (!feed || feed.length === 0) {
    container.innerHTML = '<div class="feed-empty">Waiting for trades...</div>';
    return;
  }

  var html = '';
  for (var i = 0; i < feed.length; i++) {
    var t = feed[i];
    var time = formatTime(t.detected_at);
    var badge = delayBadge(t.delay_ms);
    var isFiltered = t.event_type && t.event_type.indexOf('FILTERED_') === 0;
    var displayType = isFiltered ? t.event_type.replace('FILTERED_', '') : t.event_type;
    var typeClass = displayType === 'CLOSE' ? 'close' : 'open';
    var pnlStr = '';

    if (displayType === 'CLOSE' && t.net_pnl !== null) {
      pnlStr = ' <span class="' + pnlClass(t.net_pnl) + '">' + usd(t.net_pnl) + '</span>';
    }

    // Determine filter verdict class
    var verdict = t.filter_verdict || 'skipped';
    var verdictClass = verdict === 'passed' ? 'verdict-passed' : verdict === 'failed' ? 'verdict-failed' : verdict === 'pending' ? 'verdict-pending' : '';
    var verdictIcon = verdict === 'passed' ? '<span class="verdict-icon green" title="All filters passed">PASS</span>' : verdict === 'failed' ? '<span class="verdict-icon red" title="Filter failed">FAIL</span>' : verdict === 'pending' ? '<span class="verdict-icon amber" title="Pending">PEND</span>' : '';

    // Build filter detail rows
    var filterDetailHtml = '';
    if (t.filter_results && t.filter_results.length > 0) {
      filterDetailHtml = '<div class="filter-details">';
      for (var fi = 0; fi < t.filter_results.length; fi++) {
        var fr = t.filter_results[fi];
        var fIcon = fr.status === 'passed' ? '<span class="green">PASS</span>' : fr.status === 'failed' ? '<span class="red">FAIL</span>' : '<span class="amber">PEND</span>';
        var fMsg = fr.fail_message ? ' - ' + esc(fr.fail_message) : '';
        filterDetailHtml += '<div class="filter-detail-row">';
        filterDetailHtml += '<span class="filter-name">' + esc(fr.filter_name) + '</span> ';
        filterDetailHtml += fIcon;
        filterDetailHtml += '<span class="filter-vals">[' + esc(fr.actual_value || '?') + ' / ' + esc(fr.threshold_value || '?') + ']</span>';
        filterDetailHtml += '<span class="filter-msg">' + fMsg + '</span>';
        filterDetailHtml += '</div>';
      }
      filterDetailHtml += '</div>';
    }

    // Their trade line
    html += '<div class="feed-pair ' + verdictClass + '" onclick="this.classList.toggle(\'expanded\')">';
    html += '<div class="feed-line their">';
    html += '<span class="feed-time">' + esc(time) + '</span> ';
    if (!currentMode) html += modeBadge(t.mode_id);
    html += '<span class="feed-wallet">WALLET ' + esc(t.wallet_username || (t.wallet_address || '').substring(0, 10)) + '</span> ';
    html += '<span class="feed-type ' + typeClass + (isFiltered ? ' filtered' : '') + '">' + esc(displayType) + '</span> ';
    if (isFiltered) { html += '<span class="feed-filtered-tag">FILTERED</span> '; }
    html += '<span class="feed-market">"' + esc(truncate(t.market_title, 40)) + '"</span> ';
    html += '<span class="feed-dir">' + esc(t.direction) + '</span> ';
    html += '<span class="feed-size">$' + (parseFloat(t.their_size) || 0).toFixed(0) + '</span> ';
    html += '<span class="feed-price">@' + (parseFloat(t.their_price) || 0).toFixed(3) + '</span>';
    html += '</div>';

    // Our copy line
    html += '<div class="feed-line ours">';
    html += '<span class="feed-arrow">►</span> ';
    html += '<span class="feed-label">US ' + (t.event_type === 'CLOSE' ? 'CLOSED' : 'COPIED') + '</span> ';
    html += '<span class="feed-dir">' + esc(t.direction) + '</span> ';
    html += '<span class="feed-size">$' + (parseFloat(t.our_size) || 0).toFixed(2) + '</span> ';
    html += '<span class="feed-price">@' + (parseFloat(t.our_price) || 0).toFixed(3) + '</span> ';
    html += '<span class="feed-fees">[fee $' + (parseFloat(t.poly_fee) || 0).toFixed(2) + ' + slip $' + (parseFloat(t.slippage) || 0).toFixed(2) + ']</span> ';
    html += pnlStr + ' ';
    html += badge + ' ';
    html += verdictIcon;
    // Score badge
    if (t.score_trade !== null && t.score_trade !== undefined) {
      var st = parseFloat(t.score_trade);
      var sCls = st >= 80 ? 'score-full' : st >= 65 ? 'score-reduced' : st >= 50 ? 'score-consensus' : 'score-skip';
      var sLbl = st >= 80 ? 'Full' : st >= 65 ? 'Reduced' : st >= 50 ? 'Consensus' : 'Skip';
      html += ' <span class="score-badge ' + sCls + '" title="Trade score: ' + st.toFixed(1) + '">' + st.toFixed(0) + ' ' + sLbl + '</span>';
    }
    html += '</div>';
    html += filterDetailHtml;
    html += '</div>';
  }

  container.innerHTML = html;
}

/* ── Top Bar Rendering ────────────────────────────────────── */

function fmtUsd(v) {
  var n = parseFloat(v) || 0;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function renderTopBar(pnl, loop) {
  if (pnl) {
    document.getElementById('stat-today').textContent = pnl.trades_today || 0;
    document.getElementById('stat-win').textContent = (pnl.win_rate || 0).toFixed(0) + '%';

    // Budget bar
    renderBudgetBar(pnl);
  }

  if (loop) {
    loopRunning = loop.running;
    var dot = document.getElementById('loop-indicator');
    dot.className = 'loop-dot ' + (loop.running ? 'on' : 'off');

    var cycleText = loop.running ? (loop.last_cycle_ms || 0).toFixed(0) + 'ms' : 'OFF';
    document.getElementById('stat-cycle').textContent = cycleText;

    var btn = document.getElementById('btn-loop');
    btn.textContent = loop.running ? '■' : '▶';
    btn.className = 'btn-loop ' + (loop.running ? 'running' : 'stopped');
  }
}



/* ── Budget Bar Rendering ─────────────────────────────────── */

function renderBudgetBar(pnl) {
  var bankroll = parseFloat(pnl.bankroll) || 1000;
  var inPos = parseFloat(pnl.in_positions) || 0;
  var cash = parseFloat(pnl.cash_available) || bankroll;
  var totalPnl = parseFloat(pnl.total_pnl) || 0;
  var realizedPnl = parseFloat(pnl.realized_pnl) || 0;
  var unrealizedPnl = parseFloat(pnl.unrealized_pnl) || 0;
  var accountValue = parseFloat(pnl.account_value) || bankroll;
  var minTrade = parseFloat(pnl.min_trade) || 5;
  var maxTrade = parseFloat(pnl.max_trade) || 100;

  // Account value
  var accEl = document.getElementById('budget-account');
  accEl.textContent = fmtUsd(accountValue);
  accEl.className = 'budget-value' + (accountValue > bankroll ? ' positive' : accountValue < bankroll ? ' negative' : '');

  // Cash available
  document.getElementById('budget-cash').textContent = fmtUsd(cash);

  // In positions
  var posEl = document.getElementById('budget-positions');
  posEl.textContent = fmtUsd(inPos);
  posEl.className = 'budget-value' + (inPos > 0 ? ' highlight' : '');
  document.getElementById('budget-pos-count').textContent = (pnl.open_positions || 0) + ' open';

  // P&L
  var pnlEl = document.getElementById('budget-pnl');
  pnlEl.textContent = usd(totalPnl);
  pnlEl.className = 'budget-value ' + pnlClass(totalPnl);
  document.getElementById('budget-pnl-detail').textContent =
    'R: ' + usd(realizedPnl) + ' / U: ' + usd(unrealizedPnl);

  // Per trade (proportional range)
  document.getElementById('budget-trade-size').textContent = '$' + minTrade.toFixed(0) + '\u2013$' + maxTrade.toFixed(0);
  document.getElementById('budget-sizing-mode').textContent = 'proportional';

  // Progress bar — show how much of bankroll is deployed
  var posPct = bankroll > 0 ? Math.min((inPos / bankroll) * 100, 100) : 0;
  document.getElementById('budget-bar-positions').style.width = posPct + '%';

  // P&L portion of bar
  var pnlBarEl = document.getElementById('budget-bar-pnl');
  if (totalPnl > 0) {
    var pnlPct = Math.min((totalPnl / bankroll) * 100, 20);
    pnlBarEl.style.width = pnlPct + '%';
    pnlBarEl.className = 'budget-progress-pnl positive';
  } else if (totalPnl < 0) {
    var lossPct = Math.min((Math.abs(totalPnl) / bankroll) * 100, 20);
    pnlBarEl.style.width = lossPct + '%';
    pnlBarEl.className = 'budget-progress-pnl negative';
  } else {
    pnlBarEl.style.width = '0%';
    pnlBarEl.className = 'budget-progress-pnl';
  }

  document.getElementById('budget-bankroll-label').textContent = fmtUsd(bankroll);
}

/* ── Positions Rendering ──────────────────────────────────── */

function renderPositions(openPos, closedPos) {
  renderPositionTable('open-body', openPos, false);
  renderPositionTable('closed-body', closedPos, true);
}

function renderPositionTable(tbodyId, positions, isClosed) {
  var tbody = document.getElementById(tbodyId);
  if (!positions || positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-row">No positions</td></tr>';
    return;
  }

  var html = '';
  for (var i = 0; i < positions.length; i++) {
    var p = positions[i];
    var totalFees = (parseFloat(p.poly_fee) || 0) + (parseFloat(p.slippage) || 0);
    var delayVal = isClosed ? p.exit_delay_ms : p.entry_delay_ms;

    html += '<tr>';
    html += '<td class="market-cell" title="' + esc(p.market_title) + '">' + (!currentMode ? modeBadge(p.mode_id) : '') + esc(truncate(p.market_title, 30)) + '</td>';
    html += '<td>' + esc(p.direction) + '</td>';
    html += '<td class="num">' + (parseFloat(p.our_entry_price) || 0).toFixed(3) + '</td>';
    html += '<td class="num">' + (parseFloat(isClosed ? p.exit_price : p.current_price) || 0).toFixed(3) + '</td>';
    html += '<td class="num">$' + (parseFloat(p.our_size_usdc) || 0).toFixed(2) + '</td>';
    html += '<td class="num ' + pnlClass(p.gross_pnl) + '">' + usd(p.gross_pnl) + '</td>';
    html += '<td class="num">$' + totalFees.toFixed(2) + '</td>';
    html += '<td class="num ' + pnlClass(p.net_pnl) + '">' + usd(p.net_pnl) + '</td>';
    html += '<td>' + delayBadge(delayVal) + '</td>';
    html += '</tr>';
  }

  tbody.innerHTML = html;
}

/* ── Loop Toggle ──────────────────────────────────────────── */

async function toggleLoop() {
  if (loopRunning) {
    await apiPost('/loop/stop');
  } else {
    await apiPost('/loop/start');
  }
  // Refresh status immediately
  var loop = await apiGet('/loop/status');
  renderTopBar(null, loop);
}

/* ── Polling ──────────────────────────────────────────────── */

async function pollFeed() {
  var feed = await apiGet('/feed?limit=100' + modeParam());
  if (feed) renderFeed(feed);
}

async function pollPnl() {
  var pnl = await apiGet('/pnl' + modeParamFirst());
  var loop = await apiGet('/loop/status');
  renderTopBar(pnl, loop);
}

async function pollPositions() {
  var openPos = await apiGet('/positions?status=open' + modeParam());
  var closedPos = await apiGet('/positions?status=closed' + modeParam());
  renderPositions(openPos, closedPos);
}

/* ── Tab Switching ────────────────────────────────────────── */

var currentTab = 'live';

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
  });
  document.querySelectorAll('.tab-content').forEach(function (el) {
    el.classList.toggle('active', el.id === 'tab-' + tab);
  });
  if (tab === 'analysis') pollAnalysis();
  if (tab === 'wallets') pollWallets();
  if (tab === 'scout') pollScout();
  if (tab === 'filters') pollFilters();
}

/* ── Analysis: State ─────────────────────────────────────── */

var dailyPnlChart = null;

/* ── Analysis: Poll all three endpoints ──────────────────── */

async function pollAnalysis() {
  var mp = modeParamFirst();
  var summary = apiGet('/analytics/summary' + mp);
  var daily = apiGet('/analytics/daily' + mp);
  var wallets = apiGet('/analytics/wallets' + mp);

  var s = await summary;
  var d = await daily;
  var w = await wallets;

  if (s) renderAnalysisSummary(s);
  if (d) renderDailyChart(d);
  if (w) renderWalletLeaderboard(w);
}

/* ── Analysis: KPI cards ─────────────────────────────────── */

function renderAnalysisSummary(s) {
  var totalPnl = (parseFloat(s.total_net) || 0) + (parseFloat(s.unrealized_pnl) || 0);
  var pnlEl = document.getElementById('a-total-pnl');
  pnlEl.textContent = usd(totalPnl);
  pnlEl.className = 'kpi-value ' + (totalPnl > 0 ? 'positive' : totalPnl < 0 ? 'negative' : '');

  document.getElementById('a-pnl-detail').textContent =
    'R: ' + usd(s.total_net) + '  U: ' + usd(s.unrealized_pnl);

  var wrEl = document.getElementById('a-win-rate');
  wrEl.textContent = (s.win_rate || 0) + '%';
  wrEl.className = 'kpi-value ' + (s.win_rate >= 55 ? 'positive' : s.win_rate > 0 ? '' : '');
  document.getElementById('a-record').textContent = s.total_wins + 'W / ' + s.total_losses + 'L';

  document.getElementById('a-profit-factor').textContent =
    s.profit_factor > 0 ? s.profit_factor.toFixed(2) + 'x' : '—';

  var roiEl = document.getElementById('a-roi');
  roiEl.textContent = s.roi_pct !== 0 ? s.roi_pct.toFixed(1) + '%' : '—';
  roiEl.className = 'kpi-value ' + (s.roi_pct > 0 ? 'positive' : s.roi_pct < 0 ? 'negative' : '');
  document.getElementById('a-invested').textContent = '$' + (parseFloat(s.total_invested) || 0).toFixed(2) + ' invested';

  document.getElementById('a-avg-wl').textContent =
    (s.avg_win !== 0 || s.avg_loss !== 0)
      ? usd(s.avg_win) + ' / ' + usd(s.avg_loss)
      : '—';
  document.getElementById('a-best-worst').textContent =
    'best ' + usd(s.biggest_win) + ' / worst ' + usd(s.biggest_loss);

  // Tracking since
  if (s.tracking_since) {
    var since = new Date(s.tracking_since + 'Z');
    var now = new Date();
    var diffH = Math.floor((now - since) / 3600000);
    var days = Math.floor(diffH / 24);
    var hours = diffH % 24;
    document.getElementById('a-tracking').textContent =
      days > 0 ? days + 'd ' + hours + 'h' : hours + 'h';
  } else {
    document.getElementById('a-tracking').textContent = '—';
  }
  document.getElementById('a-hold-time').textContent =
    s.avg_hold_time_hours > 0 ? 'avg hold: ' + s.avg_hold_time_hours.toFixed(1) + 'h' : '';

  // Avg trade score KPI
  var avgScoreEl = document.getElementById('a-avg-score');
  if (avgScoreEl && s.avg_trade_score !== undefined && s.avg_trade_score !== null) {
    var avgS = parseFloat(s.avg_trade_score) || 0;
    avgScoreEl.textContent = avgS.toFixed(1);
    avgScoreEl.className = 'kpi-value ' + (avgS >= 80 ? 'positive' : avgS >= 50 ? 'highlight' : 'negative');
  }

  // Speed insight
  renderSpeedInsight(s);
  // Fee insight
  renderFeeInsight(s);
}

/* ── Analysis: Daily P&L Chart ───────────────────────────── */

function renderDailyChart(data) {
  var emptyEl = document.getElementById('daily-chart-empty');
  var canvas = document.getElementById('dailyPnlChart');

  if (!data || data.length === 0) {
    emptyEl.classList.remove('hidden');
    canvas.style.display = 'none';
    if (dailyPnlChart) { dailyPnlChart.destroy(); dailyPnlChart = null; }
    return;
  }

  emptyEl.classList.add('hidden');
  canvas.style.display = 'block';

  var labels = [];
  var barData = [];
  var barColors = [];
  var cumData = [];
  var cumulative = 0;

  for (var i = 0; i < data.length; i++) {
    labels.push(data[i].day);
    var pnl = parseFloat(data[i].daily_pnl) || 0;
    barData.push(pnl);
    barColors.push(pnl >= 0 ? '#22c55e' : '#ef4444');
    cumulative += pnl;
    cumData.push(cumulative);
  }

  if (dailyPnlChart) dailyPnlChart.destroy();

  dailyPnlChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'Daily P&L',
          data: barData,
          backgroundColor: barColors,
          borderRadius: 2,
          order: 2,
          yAxisID: 'y',
        },
        {
          label: 'Cumulative',
          data: cumData,
          type: 'line',
          borderColor: '#3b82f6',
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 0,
          pointHitRadius: 8,
          tension: 0.3,
          order: 1,
          yAxisID: 'y',
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1f2e',
          titleColor: '#9aa0ac',
          bodyColor: '#e8eaed',
          borderColor: '#2a3040',
          borderWidth: 1,
          padding: 10,
          callbacks: {
            label: function (ctx) {
              return ctx.dataset.label + ': ' + usd(ctx.parsed.y);
            }
          }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: '#6b7280', font: { size: 10, family: "'JetBrains Mono', monospace" }, maxTicksLimit: 10 },
          border: { display: false },
        },
        y: {
          grid: { color: 'rgba(42, 48, 64, 0.5)' },
          ticks: {
            color: '#6b7280',
            font: { size: 10, family: "'JetBrains Mono', monospace" },
            callback: function (v) { return '$' + v.toFixed(2); }
          },
          border: { display: false },
        }
      },
      interaction: { intersect: false, mode: 'index' },
    }
  });
}

/* ── Analysis: Wallet Leaderboard ────────────────────────── */

function renderWalletLeaderboard(wallets) {
  var tbody = document.getElementById('wallet-body');
  if (!wallets || wallets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">Not enough data yet — check back after the engine has been running for a while.</td></tr>';
    return;
  }

  // Filter to wallets with any trade activity
  var active = wallets.filter(function (w) {
    return w.closed_trades > 0 || w.open_trades > 0;
  });

  if (active.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">Not enough data yet — check back after the engine has been running for a while.</td></tr>';
    return;
  }

  var html = '';
  for (var i = 0; i < active.length; i++) {
    var w = active[i];
    var totalPnl = (parseFloat(w.realized_pnl) || 0) + (parseFloat(w.unrealized_pnl) || 0);
    var winRate = w.closed_trades > 0 ? ((w.wins / w.closed_trades) * 100).toFixed(0) : '—';
    var avgDelay = w.avg_delay_ms > 0 ? Math.round(w.avg_delay_ms) + 'ms' : '—';

    html += '<tr>';
    html += '<td class="num">' + (i + 1) + '</td>';
    html += '<td>' + esc(w.username || w.address.substring(0, 10) + '…') + '</td>';
    html += '<td class="num">' + w.closed_trades + '</td>';
    html += '<td class="num">' + winRate + (winRate !== '—' ? '%' : '') + '</td>';
    html += '<td class="num ' + pnlClass(w.realized_pnl) + '">' + usd(w.realized_pnl) + '</td>';
    html += '<td class="num ' + pnlClass(w.unrealized_pnl) + '">' + usd(w.unrealized_pnl) + '</td>';
    html += '<td class="num ' + pnlClass(totalPnl) + '">' + usd(totalPnl) + '</td>';
    html += '<td>' + delayBadge(w.avg_delay_ms > 0 ? w.avg_delay_ms : null) + '</td>';
    html += '</tr>';
  }

  tbody.innerHTML = html;
}

/* ── Analysis: Speed Insight ─────────────────────────────── */

function renderSpeedInsight(s) {
  var body = document.getElementById('insight-speed-body');
  if (s.fast_count === 0 && s.slow_count === 0) {
    body.textContent = 'Not enough data yet.';
    return;
  }

  var fastAvg = s.fast_count > 0 ? (s.fast_trade_pnl / s.fast_count) : 0;
  var slowAvg = s.slow_count > 0 ? (s.slow_trade_pnl / s.slow_count) : 0;

  body.innerHTML =
    '<div class="insight-row"><span class="insight-label">Fast copies (&lt;5s)</span><span class="insight-val ' + pnlClass(s.fast_trade_pnl) + '">' + usd(s.fast_trade_pnl) + ' (' + s.fast_count + ' trades)</span></div>' +
    '<div class="insight-row"><span class="insight-label">Slow copies (&ge;5s)</span><span class="insight-val ' + pnlClass(s.slow_trade_pnl) + '">' + usd(s.slow_trade_pnl) + ' (' + s.slow_count + ' trades)</span></div>' +
    '<div class="insight-row"><span class="insight-label">Avg P&L fast</span><span class="insight-val ' + pnlClass(fastAvg) + '">' + usd(fastAvg) + '/trade</span></div>' +
    '<div class="insight-row"><span class="insight-label">Avg P&L slow</span><span class="insight-val ' + pnlClass(slowAvg) + '">' + usd(slowAvg) + '/trade</span></div>';
}

/* ── Analysis: Fee Impact Insight ────────────────────────── */

function renderFeeInsight(s) {
  var body = document.getElementById('insight-fees-body');
  if (s.total_fees_paid === 0 && s.total_net === 0) {
    body.textContent = 'Not enough data yet.';
    return;
  }

  var grossPnl = (parseFloat(s.total_net) || 0) + (parseFloat(s.total_fees_paid) || 0);
  var feePct = grossPnl !== 0 ? Math.abs(s.total_fees_paid / grossPnl * 100).toFixed(1) : '0';

  body.innerHTML =
    '<div class="insight-row"><span class="insight-label">Total fees + slippage</span><span class="insight-val red">-$' + Math.abs(s.total_fees_paid).toFixed(4) + '</span></div>' +
    '<div class="insight-row"><span class="insight-label">Gross P&L (before fees)</span><span class="insight-val ' + pnlClass(grossPnl) + '">' + usd(grossPnl) + '</span></div>' +
    '<div class="insight-row"><span class="insight-label">Net P&L (after fees)</span><span class="insight-val ' + pnlClass(s.total_net) + '">' + usd(s.total_net) + '</span></div>' +
    '<div class="insight-row"><span class="insight-label">Fee % of gross</span><span class="insight-val">' + feePct + '%</span></div>';
}

/* ── Init ─────────────────────────────────────────────────── */

/* ── Wallets Tab: Poll & Render ───────────────────────────── */

/* -- Wallet Management Tab ------------------------------------- */

var wmAllWallets = [];

async function pollWallets() {
  var data = await apiGet('/wallets/full');
  if (data) {
    wmAllWallets = data;
    applyWalletFilters();
    renderWalletKpis(data);
  }
}

function renderWalletKpis(wallets) {
  var active = wallets.filter(function(w) { return w.is_active; });
  var withTrades = wallets.filter(function(w) { return w.closed_trades > 0 || w.open_positions > 0; });
  var totalOpen = wallets.reduce(function(s, w) { return s + w.open_positions; }, 0);
  var totalPnl = wallets.reduce(function(s, w) { return s + w.total_pnl; }, 0);
  var totalRealized = wallets.reduce(function(s, w) { return s + w.realized_pnl; }, 0);
  var totalUnrealized = wallets.reduce(function(s, w) { return s + w.unrealized_pnl; }, 0);
  var avgWr = active.length > 0
    ? active.reduce(function(s, w) { return s + w.csv_win_rate; }, 0) / active.length
    : 0;
  var scores = active.filter(function(w) { return w.wallet_score !== null; });
  var avgScore = scores.length > 0
    ? scores.reduce(function(s, w) { return s + w.wallet_score; }, 0) / scores.length
    : 0;

  setText('wm-active', active.length);
  setText('wm-total-sub', 'of ' + wallets.length + ' total');
  setText('wm-trading', withTrades.length);
  setText('wm-open-sub', totalOpen + ' open positions');
  setText('wm-avg-wr', avgWr.toFixed(1) + '%');

  var pnlEl = document.getElementById('wm-total-pnl');
  if (pnlEl) {
    pnlEl.textContent = usd(totalPnl);
    pnlEl.className = 'kpi-value ' + pnlClass(totalPnl);
  }
  setText('wm-pnl-detail', 'R: ' + usd(totalRealized) + ' / U: ' + usd(totalUnrealized));
  setText('wm-avg-score', avgScore > 0 ? avgScore.toFixed(1) : '\u2014');
}

function applyWalletFilters() {
  var tier = (document.getElementById('wm-f-tier') || {}).value || '';
  var status = (document.getElementById('wm-f-status') || {}).value || '';
  var pnlFilter = (document.getElementById('wm-f-pnl') || {}).value || '';
  var wrFilter = (document.getElementById('wm-f-wr') || {}).value || '';
  var sortBy = (document.getElementById('wm-f-sort') || {}).value || 'score-desc';
  var search = ((document.getElementById('wm-f-search') || {}).value || '').toLowerCase().trim();

  var filtered = wmAllWallets.filter(function(w) {
    // Tier filter
    if (tier) {
      if (tier === '(No Tier)' || tier === '') {
        // The last option with value "" actually means "no tier"
        // handled separately since both "All Tiers" and "(No Tier)" have value=""
      } else if (w.wallet_tier !== tier) {
        return false;
      }
    }
    // Status filter
    if (status === 'active' && !w.is_active) return false;
    if (status === 'inactive' && w.is_active) return false;
    // P&L filter
    if (pnlFilter === 'profit' && w.total_pnl <= 0) return false;
    if (pnlFilter === 'loss' && w.total_pnl >= 0) return false;
    if (pnlFilter === 'no-trades' && (w.closed_trades > 0 || w.open_positions > 0)) return false;
    // Win Rate filter
    if (wrFilter === '60+' && w.csv_win_rate < 60) return false;
    if (wrFilter === '50+' && w.csv_win_rate < 50) return false;
    if (wrFilter === 'lt50' && w.csv_win_rate >= 50) return false;
    // Search filter
    if (search) {
      var name = (w.username || '').toLowerCase();
      var addr = (w.address || '').toLowerCase();
      if (name.indexOf(search) === -1 && addr.indexOf(search) === -1) return false;
    }
    return true;
  });

  // Sort
  filtered.sort(function(a, b) {
    switch (sortBy) {
      case 'score-desc': return (b.wallet_score || 0) - (a.wallet_score || 0);
      case 'pnl-desc': return b.total_pnl - a.total_pnl;
      case 'pnl-asc': return a.total_pnl - b.total_pnl;
      case 'wr-desc': return b.csv_win_rate - a.csv_win_rate;
      case 'trades-desc': return b.trades_detected - a.trades_detected;
      case 'volume-desc': return (b.csv_volume || 0) - (a.csv_volume || 0);
      case 'added-desc': return (b.added_at || '').localeCompare(a.added_at || '');
      case 'added-asc': return (a.added_at || '').localeCompare(b.added_at || '');
      default: return 0;
    }
  });

  setText('wm-result-count', filtered.length + ' wallet' + (filtered.length !== 1 ? 's' : ''));
  renderWalletTable(filtered);
}

function renderWalletTable(wallets) {
  var tbody = document.getElementById('wm-body');
  if (!tbody) return;

  if (wallets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="15" class="empty-row">No wallets match filters</td></tr>';
    return;
  }

  var html = '';
  for (var i = 0; i < wallets.length; i++) {
    var w = wallets[i];
    var rowClass = w.is_active ? '' : ' class="wm-row-inactive"';

    // Tier badge
    var tierBadge = '';
    if (w.wallet_tier) {
      var tc = w.wallet_tier === 'Prime' ? 'tier-prime'
             : w.wallet_tier === 'Core' ? 'tier-core'
             : w.wallet_tier === 'Opportunistic' ? 'tier-opportunistic'
             : w.wallet_tier === 'Watchlist' ? 'tier-watchlist'
             : w.wallet_tier === 'Skip' ? 'tier-skip' : '';
      tierBadge = '<span class="tier-badge ' + tc + '">' + esc(w.wallet_tier) + '</span>';
    } else {
      tierBadge = '<span class="tier-badge tier-none">\u2014</span>';
    }

    // Pass rate
    var passRate = w.trades_detected > 0
      ? ((w.trades_passed / w.trades_detected) * 100).toFixed(0) + '%'
      : '\u2014';

    // Added date (short format)
    var addedStr = w.added_at ? w.added_at.substring(0, 10) : '\u2014';

    // Score display
    var scoreStr = w.wallet_score !== null ? w.wallet_score.toFixed(1) : '\u2014';
    var scoreCls = w.wallet_score !== null
      ? (w.wallet_score >= 70 ? 'wm-score-high' : w.wallet_score >= 40 ? 'wm-score-mid' : 'wm-score-low')
      : '';

    // Format large numbers
    var csvPnlStr = w.csv_pnl >= 1000000 ? '$' + (w.csv_pnl / 1000000).toFixed(1) + 'M'
                  : w.csv_pnl >= 1000 ? '$' + (w.csv_pnl / 1000).toFixed(1) + 'K'
                  : usd(w.csv_pnl);
    var volStr = w.csv_volume >= 1000000 ? '$' + (w.csv_volume / 1000000).toFixed(1) + 'M'
               : w.csv_volume >= 1000 ? '$' + (w.csv_volume / 1000).toFixed(0) + 'K'
               : '$' + (w.csv_volume || 0).toFixed(0);

    // Action button
    var actionHtml = w.is_active
      ? '<button class="wm-btn-remove" onclick="removeWallet(' + w.id + ', \'' + esc(w.username || w.address.substring(0,10)) + '\')" title="Remove from tracking">Remove</button>'
      : '<span class="wm-removed-badge">Removed</span>';

    // Polymarket profile link
    var nameHtml = w.username
      ? '<a href="https://polymarket.com/profile/' + esc(w.address) + '" target="_blank" rel="noopener" class="wm-wallet-link">' + esc(w.username) + '</a>'
      : '<a href="https://polymarket.com/profile/' + esc(w.address) + '" target="_blank" rel="noopener" class="wm-wallet-link mono">' + esc(w.address.substring(0, 10)) + '\u2026</a>';

    html += '<tr' + rowClass + '>';
    html += '<td class="num">' + (i + 1) + '</td>';
    html += '<td class="wm-wallet-cell">' + nameHtml + '</td>';
    html += '<td class="num ' + scoreCls + '">' + scoreStr + '</td>';
    html += '<td>' + tierBadge + '</td>';
    html += '<td class="num">' + w.csv_win_rate.toFixed(1) + '%</td>';
    html += '<td class="num">' + csvPnlStr + '</td>';
    html += '<td class="num">' + volStr + '</td>';
    html += '<td class="num">' + w.csv_markets + '</td>';
    html += '<td class="num">' + w.open_positions + '</td>';
    html += '<td class="num">' + w.closed_trades + '</td>';
    html += '<td class="num ' + pnlClass(w.total_pnl) + '">' + usd(w.total_pnl) + '</td>';
    html += '<td class="num">' + w.trades_detected + '</td>';
    html += '<td class="num">' + passRate + '</td>';
    html += '<td class="num wm-date">' + addedStr + '</td>';
    html += '<td class="wm-action-cell">' + actionHtml + '</td>';
    html += '</tr>';
  }
  tbody.innerHTML = html;
}

async function removeWallet(id, name) {
  if (!confirm('Remove "' + name + '" from tracking? Open positions will be closed. Historical data is kept.')) return;

  try {
    var resp = await fetch(API + '/wallets/' + id, { method: 'DELETE' });
    var result = await resp.json();
    if (resp.ok) {
      // Refresh wallet list
      pollWallets();
    } else {
      alert('Error: ' + (result.error || 'Failed to remove wallet'));
    }
  } catch (e) {
    alert('Network error removing wallet');
  }
}

function setText(id, val) {
  var el = document.getElementById(id);
  if (el) el.textContent = val;
}

/* ── Filters Tab: Poll, Render, Save ─────────────────────── */

var filtersData = [];

async function pollFilters() {
  var data = await apiGet('/filters/config');
  if (data) {
    filtersData = data;
    renderFiltersTab(data);
  }
}

function renderFiltersTab(filters) {
  var container = document.getElementById('filters-container');
  if (!filters || filters.length === 0) {
    container.innerHTML = '<div class="feed-empty">No filters configured.</div>';
    return;
  }

  // Group by group field
  var groups = {};
  var groupOrder = [];
  for (var i = 0; i < filters.length; i++) {
    var f = filters[i];
    var g = f.group || 'other';
    if (!groups[g]) { groups[g] = []; groupOrder.push(g); }
    groups[g].push(f);
  }

  var html = '';
  for (var gi = 0; gi < groupOrder.length; gi++) {
    var grp = groupOrder[gi];
    html += '<div class="filter-group">';
    html += '<h3 class="filter-group-title">' + esc(grp.toUpperCase()) + '</h3>';
    var items = groups[grp];
    for (var j = 0; j < items.length; j++) {
      var f = items[j];
      var val = f.value !== undefined && f.value !== '' ? f.value : '';
      html += '<div class="filter-card">';
      html += '<div class="filter-card-header">';
      html += '<label class="filter-label" for="filter-' + esc(f.key) + '">' + esc(f.label) + '</label>';
      html += '<span class="filter-unit">' + esc(f.unit || '') + '</span>';
      html += '</div>';
      html += '<p class="filter-description">' + esc(f.description) + '</p>';
      html += '<input type="number" class="filter-input" id="filter-' + esc(f.key) + '" data-key="' + esc(f.key) + '" value="' + esc(val) + '" step="any" />';
      html += '</div>';
    }
    html += '</div>';
  }

  container.innerHTML = html;
}

async function saveFilters() {
  var inputs = document.querySelectorAll('.filter-input');
  var data = {};
  for (var i = 0; i < inputs.length; i++) {
    var inp = inputs[i];
    var key = inp.getAttribute('data-key');
    if (key && inp.value !== '') {
      data[key] = inp.value;
    }
  }

  var statusEl = document.getElementById('filters-status');
  var btn = document.getElementById('btn-save-filters');
  btn.disabled = true;
  statusEl.textContent = 'Saving...';
  statusEl.className = 'filters-status';

  try {
    var resp = await fetch(API + '/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (resp.ok) {
      statusEl.textContent = 'Saved & applied';
      statusEl.className = 'filters-status success';
    } else {
      statusEl.textContent = 'Error saving';
      statusEl.className = 'filters-status error';
    }
  } catch (e) {
    statusEl.textContent = 'Network error';
    statusEl.className = 'filters-status error';
  }

  btn.disabled = false;
  setTimeout(function () { statusEl.textContent = ''; }, 3000);
}

/* ── Scout Tab ────────────────────────────────────────────── */

var scoutRunning = false;

async function pollScout() {
  var stats = apiGet('/scout/stats');
  var queue = apiGet('/scout/queue?status=pending&limit=50');
  var settings = apiGet('/scout/settings');
  var s = await stats;
  var q = await queue;
  var st = await settings;
  if (s) renderScoutStats(s);
  if (q) renderScoutQueue(q);
  if (st) renderScoutSettings(st);
}

function renderScoutStats(s) {
  scoutRunning = s.running;
  var statusEl = document.getElementById('scout-status');
  statusEl.textContent = s.running ? 'RUNNING' : 'STOPPED';
  statusEl.className = 'kpi-value ' + (s.running ? 'positive' : '');

  var btn = document.getElementById('btn-scout-toggle');
  btn.textContent = s.running ? 'Stop' : 'Start';
  btn.className = 'btn-scout-ctrl ' + (s.running ? 'running' : '');

  document.getElementById('scout-scanned').textContent = s.total_scanned || 0;
  document.getElementById('scout-cycles').textContent = (s.cycle_count || 0) + ' cycles';
  document.getElementById('scout-queued').textContent = s.queued || 0;
  document.getElementById('scout-approved').textContent = s.approved || 0;
  document.getElementById('scout-rejected').textContent = s.rejected || 0;
}

function scoreBadgeClass(score) {
  if (score >= 70) return 'score-high';
  if (score >= 45) return 'score-mid';
  return 'score-low';
}

function renderScoutQueue(candidates) {
  var container = document.getElementById('scout-queue');
  if (!candidates || candidates.length === 0) {
    container.innerHTML = '<div class="feed-empty">No pending candidates. Scout will discover new wallets automatically.</div>';
    return;
  }

  var html = '';
  for (var i = 0; i < candidates.length; i++) {
    var c = candidates[i];
    var score = c.score || 0;
    var name = c.username || (c.proxy_wallet || '').substring(0, 12) + '...';
    var winRate = c.win_rate || 0;
    var roi = c.roi_pct || 0;
    var pnl = c.total_pnl || 0;
    var trades = c.lifetime_trades || 0;
    var markets = c.markets_traded || 0;
    var invested = c.total_invested || 0;
    var via = c.discovered_via || '';
    var wallet = c.proxy_wallet || '';

    // Score breakdown
    var breakdownHtml = '';
    if (c.score_breakdown) {
      try {
        var bd = typeof c.score_breakdown === 'string' ? JSON.parse(c.score_breakdown) : c.score_breakdown;
        breakdownHtml = '<div class="scout-breakdown">';
        var keys = ['win_rate', 'consistency', 'roi', 'lifetime_trades', 'portfolio_size', 'underrated'];
        for (var k = 0; k < keys.length; k++) {
          var key = keys[k];
          var item = bd[key];
          if (!item) continue;
          var pct = item.max > 0 ? Math.round(item.points / item.max * 100) : 0;
          breakdownHtml += '<div class="breakdown-row">';
          breakdownHtml += '<span class="breakdown-label">' + esc(key.replace(/_/g, ' ')) + '</span>';
          breakdownHtml += '<span class="breakdown-bar-wrap"><span class="breakdown-bar" style="width:' + pct + '%"></span></span>';
          breakdownHtml += '<span class="breakdown-pts">' + item.points + '/' + item.max + '</span>';
          breakdownHtml += '</div>';
        }
        breakdownHtml += '</div>';
      } catch (e) {
        breakdownHtml = '';
      }
    }

    html += '<div class="scout-card" data-wallet="' + esc(wallet) + '">';
    html += '<div class="scout-card-top">';
    html += '<div class="scout-score-badge ' + scoreBadgeClass(score) + '">' + score + '</div>';
    html += '<div class="scout-card-info">';
    html += '<div class="scout-card-name">' + esc(name) + '</div>';
    html += '<div class="scout-card-addr">' + esc(wallet) + '</div>';
    html += '</div>';
    html += '<div class="scout-card-actions">';
    html += '<button class="btn-approve" onclick="approveCandidate(\'' + esc(wallet) + '\')">Approve</button>';
    html += '<button class="btn-reject" onclick="rejectCandidate(\'' + esc(wallet) + '\')">Reject</button>';
    html += '</div>';
    html += '</div>';
    html += '<div class="scout-card-metrics">';
    html += '<div class="scout-metric"><span class="scout-metric-label">Win Rate</span><span class="scout-metric-val ' + (winRate >= 55 ? 'green' : '') + '">' + winRate.toFixed(1) + '%</span></div>';
    html += '<div class="scout-metric"><span class="scout-metric-label">ROI</span><span class="scout-metric-val ' + (roi > 0 ? 'green' : roi < 0 ? 'red' : '') + '">' + roi.toFixed(1) + '%</span></div>';
    html += '<div class="scout-metric"><span class="scout-metric-label">P&L</span><span class="scout-metric-val ' + pnlClass(pnl) + '">' + usd(pnl) + '</span></div>';
    html += '<div class="scout-metric"><span class="scout-metric-label">Trades</span><span class="scout-metric-val">' + trades + '</span></div>';
    html += '<div class="scout-metric"><span class="scout-metric-label">Markets</span><span class="scout-metric-val">' + markets + '</span></div>';
    html += '<div class="scout-metric"><span class="scout-metric-label">Invested</span><span class="scout-metric-val">$' + invested.toLocaleString('en-US', { maximumFractionDigits: 0 }) + '</span></div>';
    html += '</div>';
    if (via) {
      html += '<div class="scout-card-via">via ' + esc(via.replace(/_/g, ' ')) + '</div>';
    }
    html += breakdownHtml;
    html += '</div>';
  }

  container.innerHTML = html;
}

async function toggleScout() {
  if (scoutRunning) {
    await apiPost('/scout/stop');
  } else {
    await apiPost('/scout/start');
  }
  var s = await apiGet('/scout/stats');
  if (s) renderScoutStats(s);
}

async function approveCandidate(wallet) {
  var result = await apiPost('/scout/approve', { proxy_wallet: wallet });
  if (result && !result.error) {
    var card = document.querySelector('.scout-card[data-wallet="' + wallet + '"]');
    if (card) card.remove();
    var s = await apiGet('/scout/stats');
    if (s) renderScoutStats(s);
  }
}

async function rejectCandidate(wallet) {
  var result = await apiPost('/scout/reject', { proxy_wallet: wallet });
  if (result && !result.error) {
    var card = document.querySelector('.scout-card[data-wallet="' + wallet + '"]');
    if (card) card.remove();
    var s = await apiGet('/scout/stats');
    if (s) renderScoutStats(s);
  }
}

var scoutSettingsData = {};

function renderScoutSettings(settings) {
  scoutSettingsData = settings;
  var container = document.getElementById('scout-settings-container');

  var labels = {
    min_score_threshold: { label: 'Minimum Score', desc: 'Wallets below this score are auto-rejected' },
    max_wallets_per_cycle: { label: 'Max Wallets per Cycle', desc: 'How many wallets to evaluate each cycle' },
    cycle_delay_seconds: { label: 'Cycle Delay (seconds)', desc: 'Pause between discovery cycles' },
    eval_delay_seconds: { label: 'Eval Delay (seconds)', desc: 'Pause between wallet evaluations' },
    discovery_market_trades: { label: 'Discover from Market Trades', desc: '1 = enabled, 0 = disabled' },
    discovery_global_trades: { label: 'Discover from Global Trades', desc: '1 = enabled, 0 = disabled' },
  };

  var html = '';
  var keys = Object.keys(settings);
  for (var i = 0; i < keys.length; i++) {
    var key = keys[i];
    var info = labels[key] || { label: key, desc: '' };
    html += '<div class="filter-card">';
    html += '<div class="filter-card-header">';
    html += '<label class="filter-label" for="scout-' + esc(key) + '">' + esc(info.label) + '</label>';
    html += '</div>';
    html += '<p class="filter-description">' + esc(info.desc) + '</p>';
    html += '<input type="number" class="filter-input scout-setting-input" id="scout-' + esc(key) + '" data-key="' + esc(key) + '" value="' + esc(settings[key]) + '" step="any" />';
    html += '</div>';
  }

  container.innerHTML = html;
}

async function saveScoutSettings() {
  var inputs = document.querySelectorAll('.scout-setting-input');
  var data = {};
  for (var i = 0; i < inputs.length; i++) {
    var inp = inputs[i];
    var key = inp.getAttribute('data-key');
    if (key && inp.value !== '') {
      data[key] = inp.value;
    }
  }

  var statusEl = document.getElementById('scout-settings-status');
  var btn = document.getElementById('btn-save-scout-settings');
  btn.disabled = true;
  statusEl.textContent = 'Saving...';
  statusEl.className = 'filters-status';

  try {
    var resp = await fetch(API + '/scout/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (resp.ok) {
      statusEl.textContent = 'Settings saved';
      statusEl.className = 'filters-status success';
    } else {
      statusEl.textContent = 'Error saving';
      statusEl.className = 'filters-status error';
    }
  } catch (e) {
    statusEl.textContent = 'Network error';
    statusEl.className = 'filters-status error';
  }

  btn.disabled = false;
  setTimeout(function () { statusEl.textContent = ''; }, 3000);
}

/* ── Mode Comparison Cards ───────────────────────────────── */

async function pollModes() {
  var modes = await apiGet('/modes');
  if (!modes) return;
  for (var i = 0; i < modes.length; i++) {
    var m = modes[i];
    var mid = m.mode_id;
    var tradesEl = document.getElementById('mc-' + mid + '-trades');
    var wrEl = document.getElementById('mc-' + mid + '-wr');
    var rpnlEl = document.getElementById('mc-' + mid + '-rpnl');
    var upnlEl = document.getElementById('mc-' + mid + '-upnl');
    var openEl = document.getElementById('mc-' + mid + '-open');
    if (tradesEl) tradesEl.textContent = m.total_trades || 0;
    if (wrEl) wrEl.textContent = (m.win_rate || 0).toFixed(0) + '%';
    if (rpnlEl) {
      rpnlEl.textContent = usd(m.realized_pnl);
      rpnlEl.className = 'mc-val ' + pnlClass(m.realized_pnl);
    }
    if (upnlEl) {
      upnlEl.textContent = usd(m.unrealized_pnl);
      upnlEl.className = 'mc-val ' + pnlClass(m.unrealized_pnl);
    }
    if (openEl) openEl.textContent = m.open_positions || 0;
  }
}

async function init() {
  await pollFeed();
  await pollPnl();
  await pollPositions();
  await pollModes();

  setInterval(pollFeed, 2000);
  setInterval(pollPnl, 5000);
  setInterval(pollPositions, 10000);
  setInterval(pollModes, 15000);
  setInterval(function () {
    if (currentTab === 'analysis') pollAnalysis();
    if (currentTab === 'wallets') pollWallets();
    if (currentTab === 'scout') pollScout();
    if (currentTab === 'filters') pollFilters();
  }, 30000);
}

document.addEventListener('DOMContentLoaded', init);
