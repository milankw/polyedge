/* ── API Configuration ──────────────────────────────────────── */
var API = window.location.protocol + "//" + window.location.hostname + ":8000";

/* ── State ─────────────────────────────────────────────────── */
var state = {
  currentView: "dashboard",
  markets: [],
  equityChart: null,
  accuracyChart: null,
};

/* ── Theme Toggle ──────────────────────────────────────────── */
(function () {
  var t = document.querySelector("[data-theme-toggle]");
  var r = document.documentElement;
  var d = "dark";
  r.setAttribute("data-theme", d);
  if (t) {
    t.addEventListener("click", function () {
      d = d === "dark" ? "light" : "dark";
      r.setAttribute("data-theme", d);
      t.setAttribute("aria-label", "Switch to " + (d === "dark" ? "light" : "dark") + " mode");
      t.innerHTML = d === "dark"
        ? '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
        : '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
    });
  }
})();

/* ── View Switching ────────────────────────────────────────── */
function switchView(view) {
  state.currentView = view;
  document.querySelectorAll(".view-section").forEach(function (s) { s.classList.remove("active"); });
  var target = document.getElementById("view-" + view);
  if (target) target.classList.add("active");
  document.querySelectorAll(".sidebar-nav button").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-view") === view);
  });
  var titles = {
    "dashboard": "Simulation Dashboard",
    "active-bets": "Active Bets",
    "history": "Bet History",
    "scanner": "Market Scanner",
    "learning": "Learning & Confidence",
    "strategies": "Strategy Performance",
    "cycles": "Cycle Log",
    "settings": "Settings",
  };
  document.getElementById("pageTitle").textContent = titles[view] || view;

  if (view === "dashboard") loadDashboard();
  if (view === "active-bets") loadActiveBets();
  if (view === "history") loadHistory("all");
  if (view === "scanner") loadMarkets();
  if (view === "learning") loadLearning();
  if (view === "strategies") loadSimStrategies();
  if (view === "cycles") { loadCycles(); loadDecisions(); }
  if (view === "settings") loadSimSettings();
}

/* ── API Helpers ───────────────────────────────────────────── */
async function apiGet(path) {
  try {
    var resp = await fetch(API + path);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return await resp.json();
  } catch (e) {
    console.error("API error:", path, e);
    return null;
  }
}

async function apiPost(path, body) {
  try {
    var resp = await fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return await resp.json();
  } catch (e) {
    console.error("API error:", path, e);
    return null;
  }
}

/* ── Dashboard ─────────────────────────────────────────────── */
async function loadDashboard() {
  var [status, perf, bankrollData] = await Promise.all([
    apiGet("/api/sim/status"),
    apiGet("/api/sim/performance"),
    apiGet("/api/sim/bankroll"),
  ]);

  if (!status) {
    document.getElementById("engineStatus").textContent = "Offline";
    document.getElementById("statusDot").style.background = "var(--color-error)";
    return;
  }

  document.getElementById("engineStatus").textContent = "Engine Online";
  document.getElementById("statusDot").style.background = "var(--color-success)";

  // Bankroll hero
  var bal = status.bankroll.balance || 10000;
  var initial = status.bankroll.total_deposited || 10000;
  var pnlVal = bal - initial;
  var pnlPct = (pnlVal / initial * 100);
  document.getElementById("bankrollValue").textContent = formatMoney(bal);
  var heroEl = document.getElementById("bankrollHero");
  heroEl.className = "bankroll-hero " + (pnlVal > 0 ? "positive" : pnlVal < 0 ? "negative" : "");
  var deltaText = (pnlVal >= 0 ? "+" : "") + formatMoney(pnlVal) + " (" + pnlPct.toFixed(2) + "%)";
  document.getElementById("bankrollDelta").textContent = pnlVal !== 0 ? deltaText : "Starting balance";

  // KPIs
  setKPI("kpi-pnl", formatMoney(status.total_pnl), status.total_pnl,
    "Realized: " + formatMoney(status.realized_pnl) + " | Unrealized: " + formatMoney(status.unrealized_pnl));
  setKPI("kpi-winrate", status.win_rate + "%", status.win_rate >= 55 ? 1 : status.win_rate > 0 ? 0 : 0,
    status.wins + "W / " + status.losses + "L");

  if (perf) {
    setKPI("kpi-roi", perf.roi + "%", perf.roi, "Sharpe: " + (perf.sharpe_ratio || "—"));
  }
  setKPI("kpi-open", status.active_bets, 0,
    formatMoney(status.bankroll.at_risk || 0) + " at risk");

  // Equity chart
  if (bankrollData && bankrollData.history) {
    updateEquityChart(bankrollData.history, initial);
  }

  // Recent activity
  var bets = await apiGet("/api/sim/bets?limit=10");
  if (bets && bets.bets && bets.bets.length > 0) {
    renderRecentActivity(bets.bets);
  }

  // Engine status
  if (status.last_cycle) {
    var lc = status.last_cycle;
    document.getElementById("lastCycleTime").textContent = formatTime(lc.completed_at || lc.started_at);
    document.getElementById("lastScanned").textContent = lc.markets_scanned || 0;
    document.getElementById("lastBetsPlaced").textContent = lc.bets_placed || 0;
    document.getElementById("lastResolved").textContent = lc.bets_resolved || 0;
  }
}

function setKPI(id, value, direction, detail) {
  var valEl = document.getElementById(id);
  valEl.textContent = value;
  valEl.className = "kpi-value " + (direction > 0 ? "positive" : direction < 0 ? "negative" : "neutral");
  var detailId = id + "-detail";
  if (!document.getElementById(detailId)) detailId = id.replace("kpi-", "kpi-") + "-detail";
  // find sibling kpi-delta
  var detailEl = valEl.nextElementSibling;
  if (detailEl && detail) {
    detailEl.textContent = detail;
    detailEl.className = "kpi-delta " + (direction > 0 ? "up" : direction < 0 ? "down" : "flat");
  }
}

function updateEquityChart(history, initial) {
  var ctx = document.getElementById("equityChart");
  if (!ctx) return;
  if (state.equityChart) state.equityChart.destroy();

  var labels = [];
  var values = [];

  if (history.length > 0) {
    history.forEach(function (h) {
      labels.push(formatTime(h.recorded_at));
      values.push(h.balance);
    });
  } else {
    labels.push("Start");
    values.push(initial || 10000);
  }

  var chartColor = getCSS("--color-primary");
  var surfaceColor = getCSS("--color-surface");
  var textMuted = getCSS("--color-text-faint");

  state.equityChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [{
        label: "Bankroll",
        data: values,
        borderColor: chartColor,
        backgroundColor: chartColor + "20",
        fill: true,
        tension: 0.4,
        pointRadius: values.length > 20 ? 0 : 3,
        pointHitRadius: 10,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: surfaceColor,
          titleColor: textMuted,
          bodyColor: chartColor,
          borderColor: textMuted + "40",
          borderWidth: 1,
          padding: 12,
          displayColors: false,
          callbacks: { label: function (c) { return formatMoney(c.parsed.y); } },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: textMuted, font: { size: 10 }, maxTicksLimit: 8 }, border: { display: false } },
        y: { grid: { color: textMuted + "15" }, ticks: { color: textMuted, font: { size: 10 }, callback: function (v) { return "$" + v.toLocaleString(); } }, border: { display: false } },
      },
      interaction: { intersect: false, mode: "index" },
    },
  });
}

function renderRecentActivity(bets) {
  var container = document.getElementById("recentActivity");
  container.innerHTML = bets.map(function (b) {
    var icon, color;
    if (b.status === "open") { icon = "&#9679;"; color = "var(--color-primary)"; }
    else if (b.status === "won") { icon = "&#10003;"; color = "var(--color-success)"; }
    else { icon = "&#10007;"; color = "var(--color-error)"; }

    var pnl = b.status === "open" ? b.unrealized_pnl : b.realized_pnl;
    var pnlStr = (pnl >= 0 ? "+" : "") + formatMoney(pnl);
    var pnlColor = pnl > 0 ? "var(--color-success)" : pnl < 0 ? "var(--color-error)" : "var(--color-text-muted)";

    return '<div class="activity-item" onclick="showBetDetail(' + b.id + ')">' +
      '<div class="activity-icon" style="color:' + color + '">' + icon + '</div>' +
      '<div class="activity-body">' +
        '<div class="activity-text">' + escapeHtml(truncate(b.question, 60)) + '</div>' +
        '<div class="activity-meta">' + b.side + ' @ ' + centsStr(b.entry_price) + ' &middot; ' + formatMoney(b.size) + ' &middot; ' + formatTime(b.placed_at) + '</div>' +
      '</div>' +
      '<div class="activity-pnl mono" style="color:' + pnlColor + '">' + pnlStr + '</div>' +
    '</div>';
  }).join("");
}

/* ── Active Bets ───────────────────────────────────────────── */
async function loadActiveBets() {
  var data = await apiGet("/api/sim/bets?status=open&limit=100");
  if (!data) return;
  document.getElementById("activeBetsCount").textContent = data.total + " open";
  var tbody = document.getElementById("activeBetsBody");

  if (!data.bets || data.bets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="table-empty">No active bets</td></tr>';
    return;
  }

  tbody.innerHTML = data.bets.map(function (b) {
    var upnl = b.unrealized_pnl || 0;
    var pnlColor = upnl > 0 ? "var(--color-success)" : upnl < 0 ? "var(--color-error)" : "var(--color-text-muted)";
    var timeOpen = timeSince(b.placed_at);
    return '<tr onclick="showBetDetail(' + b.id + ')" style="cursor:pointer">' +
      '<td><div class="market-question">' + escapeHtml(truncate(b.question, 50)) + '</div></td>' +
      '<td><span class="badge badge-' + b.side.toLowerCase() + '">' + b.side + '</span></td>' +
      '<td class="mono">' + centsStr(b.entry_price) + '</td>' +
      '<td class="mono">' + centsStr(b.current_price) + '</td>' +
      '<td class="mono">' + formatMoney(b.size) + '</td>' +
      '<td class="mono" style="color:' + pnlColor + ';font-weight:600">' + (upnl >= 0 ? "+" : "") + formatMoney(upnl) + '</td>' +
      '<td class="mono">' + ((b.edge || 0) * 100).toFixed(1) + '%</td>' +
      '<td><span class="badge badge-' + b.confidence + '">' + b.confidence + '</span></td>' +
      '<td class="text-xs text-muted">' + escapeHtml(b.strategy || "") + '</td>' +
      '<td class="text-xs text-muted">' + timeOpen + '</td>' +
    '</tr>';
  }).join("");
}

/* ── Bet History ───────────────────────────────────────────── */
async function loadHistory(status) {
  // Update tabs
  document.querySelectorAll("#historyTabs .tab-filter").forEach(function (btn) {
    btn.classList.toggle("active", btn.textContent.toLowerCase() === status);
  });

  var url = "/api/sim/bets?limit=100";
  if (status !== "all") url += "&status=" + status;
  var data = await apiGet(url);
  if (!data) return;

  // Filter to only resolved for history (unless 'all' which includes open)
  var bets = data.bets;
  if (status === "all") {
    bets = bets.filter(function (b) { return b.status !== "open"; });
  }

  document.getElementById("historyCount").textContent = bets.length + " bets";
  var tbody = document.getElementById("historyBody");

  if (bets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="table-empty">No resolved bets</td></tr>';
    return;
  }

  tbody.innerHTML = bets.map(function (b) {
    var pnl = b.realized_pnl || 0;
    var pnlColor = pnl > 0 ? "var(--color-success)" : pnl < 0 ? "var(--color-error)" : "var(--color-text-muted)";
    var statusClass = b.status === "won" ? "badge-high" : b.status === "lost" ? "badge-danger" : "badge-none";
    return '<tr onclick="showBetDetail(' + b.id + ')" style="cursor:pointer">' +
      '<td><div class="market-question">' + escapeHtml(truncate(b.question, 50)) + '</div></td>' +
      '<td><span class="badge badge-' + b.side.toLowerCase() + '">' + b.side + '</span></td>' +
      '<td class="mono">' + centsStr(b.entry_price) + '</td>' +
      '<td class="mono">' + centsStr(b.outcome_price) + '</td>' +
      '<td class="mono">' + formatMoney(b.size) + '</td>' +
      '<td class="mono" style="color:' + pnlColor + ';font-weight:600">' + (pnl >= 0 ? "+" : "") + formatMoney(pnl) + '</td>' +
      '<td class="mono">' + ((b.edge || 0) * 100).toFixed(1) + '%</td>' +
      '<td class="text-xs text-muted">' + escapeHtml(b.strategy || "") + '</td>' +
      '<td><span class="badge ' + statusClass + '">' + b.status + '</span></td>' +
      '<td class="text-xs text-muted">' + formatDate(b.resolved_at || b.placed_at) + '</td>' +
    '</tr>';
  }).join("");
}

/* ── Bet Detail Modal ──────────────────────────────────────── */
async function showBetDetail(id) {
  var data = await apiGet("/api/sim/bets/" + id);
  if (!data || !data.bet) return;
  var b = data.bet;

  document.getElementById("modalTitle").textContent = "Bet #" + b.id;
  var pnl = b.status === "open" ? b.unrealized_pnl : b.realized_pnl;
  var pnlColor = pnl > 0 ? "var(--color-success)" : pnl < 0 ? "var(--color-error)" : "var(--color-text-muted)";

  var signals = b.signals || [];
  if (typeof signals === "string") { try { signals = JSON.parse(signals); } catch (e) { signals = []; } }

  document.getElementById("modalBody").innerHTML =
    '<div class="modal-section">' +
      '<div class="modal-question">' + escapeHtml(b.question) + '</div>' +
    '</div>' +
    '<div class="modal-grid">' +
      modalStat("Side", '<span class="badge badge-' + b.side.toLowerCase() + '">' + b.side + '</span>') +
      modalStat("Status", '<span class="badge badge-' + (b.status === "won" ? "high" : b.status === "lost" ? "danger" : "low") + '">' + b.status + '</span>') +
      modalStat("Entry Price", centsStr(b.entry_price)) +
      modalStat("Current Price", centsStr(b.current_price)) +
      modalStat("Size", formatMoney(b.size)) +
      modalStat("Shares", (b.shares || 0).toFixed(2)) +
      modalStat("P&L", '<span style="color:' + pnlColor + ';font-weight:700">' + (pnl >= 0 ? "+" : "") + formatMoney(pnl) + '</span>') +
      modalStat("Edge", ((b.edge || 0) * 100).toFixed(1) + '%') +
      modalStat("Our Probability", ((b.our_probability || 0) * 100).toFixed(1) + '%') +
      modalStat("Market Probability", ((b.market_probability || 0) * 100).toFixed(1) + '%') +
      modalStat("Confidence", b.confidence || "—") +
      modalStat("Strategy", b.strategy || "—") +
      modalStat("Category", b.category || "—") +
      modalStat("Placed", formatTime(b.placed_at)) +
      (b.resolved_at ? modalStat("Resolved", formatTime(b.resolved_at)) : "") +
      (b.resolution_source ? modalStat("Resolution", b.resolution_source) : "") +
    '</div>' +
    '<div class="modal-section">' +
      '<div class="modal-label">Reasoning</div>' +
      '<div class="modal-reasoning">' + escapeHtml(b.reasoning || "No reasoning recorded") + '</div>' +
    '</div>' +
    (signals.length > 0 ? '<div class="modal-section"><div class="modal-label">Signals</div><div class="signal-pills">' +
      signals.map(function (s) {
        var type = typeof s === "string" ? s : s;
        return '<span class="signal-pill" data-type="' + type + '">' + type.replace(/_/g, " ") + '</span>';
      }).join("") + '</div></div>' : '');

  document.getElementById("betModal").classList.add("active");
}

function modalStat(label, value) {
  return '<div class="modal-stat"><span class="modal-stat-label">' + label + '</span><span class="modal-stat-value">' + value + '</span></div>';
}

function closeBetModal(event) {
  if (event && event.target !== event.currentTarget) return;
  document.getElementById("betModal").classList.remove("active");
}

/* ── Market Scanner ────────────────────────────────────────── */
async function loadMarkets(category) {
  var params = "?limit=50";
  if (category && category !== "all") params += "&category=" + category;

  document.getElementById("marketsBody").innerHTML = '<tr><td colspan="7" class="table-empty">Scanning markets...</td></tr>';
  var data = await apiGet("/api/markets" + params);
  if (!data) {
    document.getElementById("marketsBody").innerHTML = '<tr><td colspan="7" class="table-empty" style="color:var(--color-error)">Failed to load</td></tr>';
    return;
  }

  state.markets = data.markets;
  document.getElementById("marketCount").textContent = data.total + " markets";
  renderMarketsTable(data.markets);
}

function renderMarketsTable(markets) {
  var tbody = document.getElementById("marketsBody");
  if (!markets || markets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No markets found</td></tr>';
    return;
  }

  tbody.innerHTML = markets.map(function (m) {
    var edgePct = Math.min(m.edge * 100, 100);
    var signalHtml = (m.signals || []).map(function (s) {
      return '<span class="signal-pill" data-type="' + s.type + '">' + s.type.replace(/_/g, " ") + '</span>';
    }).join("");
    var isOpportunity = m.edge >= 0.03;

    return '<tr class="' + (isOpportunity ? "row-opportunity" : "") + '">' +
      '<td><div class="market-question" title="' + escapeHtml(m.question) + '">' + escapeHtml(m.question) + '</div></td>' +
      '<td class="mono"><span style="color:var(--color-success)">' + (m.yes_price * 100).toFixed(1) + '&cent;</span></td>' +
      '<td class="mono"><span style="color:var(--color-error)">' + (m.no_price * 100).toFixed(1) + '&cent;</span></td>' +
      '<td class="mono">' + formatCompact(m.volume) + '</td>' +
      '<td><div class="edge-bar"><div class="edge-bar-track"><div class="edge-bar-fill" style="width:' + edgePct + '%"></div></div><span class="mono" style="font-size:var(--text-xs)">' + (m.edge * 100).toFixed(1) + '%</span></div></td>' +
      '<td><span class="badge badge-' + m.confidence + '">' + m.confidence + '</span></td>' +
      '<td><div class="signal-pills">' + signalHtml + '</div></td>' +
    '</tr>';
  }).join("");
}

function filterMarkets(category) {
  document.querySelectorAll("#categoryTabs .tab-filter").forEach(function (btn) {
    btn.classList.toggle("active", btn.textContent.toLowerCase().includes(category) || (category === "all" && btn.textContent === "All Markets"));
  });
  loadMarkets(category);
}

/* ── Learning & Confidence ─────────────────────────────────── */
async function loadLearning() {
  var [confData, learnData] = await Promise.all([
    apiGet("/api/sim/confidence"),
    apiGet("/api/sim/learning"),
  ]);

  // Confidence bars
  var container = document.getElementById("confidenceList");
  if (confData && confData.categories && confData.categories.length > 0) {
    container.innerHTML = confData.categories.map(function (c) {
      var pct = Math.round((c.confidence_score || 0) * 100);
      var barColor = c.status === "confident" ? "var(--color-success)" :
                     c.status === "unreliable" ? "var(--color-error)" : "var(--color-primary)";
      return '<div class="confidence-item">' +
        '<div class="confidence-header">' +
          '<span class="confidence-name">' + escapeHtml(c.category || "Unknown") + '</span>' +
          '<span class="badge badge-' + (c.status === "confident" ? "high" : c.status === "unreliable" ? "danger" : "low") + '">' + c.status + '</span>' +
        '</div>' +
        '<div class="progress-bar"><div class="progress-bar-fill" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
        '<div class="confidence-stats">' +
          '<span>' + pct + '% confidence</span>' +
          '<span>' + c.total_predictions + ' predictions</span>' +
          '<span>' + c.correct_predictions + ' correct</span>' +
          '<span>ROI: ' + ((c.roi || 0) * 100).toFixed(1) + '%</span>' +
        '</div>' +
      '</div>';
    }).join("");
  }

  // Accuracy chart
  if (learnData && learnData.confidence && learnData.confidence.length > 0) {
    updateAccuracyChart(learnData.confidence);
  }

  // Learning summary
  var summary = document.getElementById("learningSummary");
  if (confData && confData.categories && confData.categories.length > 0) {
    var good = confData.categories.filter(function (c) { return c.status === "confident"; });
    var bad = confData.categories.filter(function (c) { return c.status === "unreliable"; });
    var learning = confData.categories.filter(function (c) { return c.status === "learning"; });

    var html = '<div class="learning-grid">';
    if (good.length > 0) {
      html += '<div class="learning-card learning-good"><div class="learning-card-title">Strong Categories</div>' +
        good.map(function (c) { return '<div class="learning-card-item">' + escapeHtml(c.category) + ' — ' + Math.round(c.confidence_score * 100) + '% confidence</div>'; }).join("") + '</div>';
    }
    if (bad.length > 0) {
      html += '<div class="learning-card learning-bad"><div class="learning-card-title">Weak Categories</div>' +
        bad.map(function (c) { return '<div class="learning-card-item">' + escapeHtml(c.category) + ' — ' + Math.round(c.confidence_score * 100) + '% confidence</div>'; }).join("") + '</div>';
    }
    if (learning.length > 0) {
      html += '<div class="learning-card learning-neutral"><div class="learning-card-title">Still Learning</div>' +
        learning.map(function (c) { return '<div class="learning-card-item">' + escapeHtml(c.category) + ' — ' + c.total_predictions + ' predictions</div>'; }).join("") + '</div>';
    }
    html += '</div>';
    summary.innerHTML = html;
  }
}

function updateAccuracyChart(categories) {
  var ctx = document.getElementById("accuracyChart");
  if (!ctx) return;
  if (state.accuracyChart) state.accuracyChart.destroy();

  var labels = categories.map(function (c) { return truncate(c.category || "?", 15); });
  var predicted = categories.map(function (c) { return ((c.avg_edge_predicted || 0) * 100).toFixed(1); });
  var realized = categories.map(function (c) { return ((c.avg_edge_realized || 0) * 100).toFixed(1); });

  var textMuted = getCSS("--color-text-faint");

  state.accuracyChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [
        { label: "Predicted Edge", data: predicted, backgroundColor: getCSS("--color-primary") + "80", borderColor: getCSS("--color-primary"), borderWidth: 1 },
        { label: "Realized Edge", data: realized, backgroundColor: getCSS("--color-success") + "80", borderColor: getCSS("--color-success"), borderWidth: 1 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: textMuted, font: { size: 11 } } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: textMuted, font: { size: 10 } }, border: { display: false } },
        y: { grid: { color: textMuted + "15" }, ticks: { color: textMuted, font: { size: 10 }, callback: function (v) { return v + "%"; } }, border: { display: false } },
      },
    },
  });
}

/* ── Strategy Performance ──────────────────────────────────── */
async function loadSimStrategies() {
  var data = await apiGet("/api/sim/strategies");
  if (!data || !data.strategies || data.strategies.length === 0) return;

  var container = document.getElementById("simStrategiesList");
  container.innerHTML = '<div style="overflow-x:auto"><table class="data-table"><thead><tr>' +
    '<th>Strategy</th><th>Bets</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Total P&L</th><th>Avg Edge</th><th>Status</th>' +
    '</tr></thead><tbody>' +
    data.strategies.map(function (s) {
      var pnlColor = s.total_pnl > 0 ? "var(--color-success)" : s.total_pnl < 0 ? "var(--color-error)" : "var(--color-text-muted)";
      var statusClass = s.status === "scaling" ? "scaling" : s.status === "promising" ? "promising" : s.status === "killing" ? "killing" : "exploring";
      return '<tr>' +
        '<td style="font-weight:600">' + escapeHtml(s.name) + '</td>' +
        '<td class="mono">' + s.total_bets + '</td>' +
        '<td class="mono" style="color:var(--color-success)">' + s.wins + '</td>' +
        '<td class="mono" style="color:var(--color-error)">' + s.losses + '</td>' +
        '<td class="mono">' + (s.win_rate || 0) + '%</td>' +
        '<td class="mono" style="color:' + pnlColor + ';font-weight:600">' + formatMoney(s.total_pnl) + '</td>' +
        '<td class="mono">' + ((s.avg_edge || 0) * 100).toFixed(1) + '%</td>' +
        '<td><span class="badge badge-' + statusClass + '">' + s.status + '</span></td>' +
      '</tr>';
    }).join("") +
    '</tbody></table></div>';
}

/* ── Cycle Log ─────────────────────────────────────────────── */
async function loadCycles() {
  var data = await apiGet("/api/sim/cycles?limit=50");
  if (!data) return;
  var tbody = document.getElementById("cyclesBody");

  if (!data.cycles || data.cycles.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="table-empty">No cycles run yet</td></tr>';
    return;
  }

  tbody.innerHTML = data.cycles.map(function (c) {
    var duration = "—";
    if (c.started_at && c.completed_at) {
      var ms = new Date(c.completed_at) - new Date(c.started_at);
      duration = (ms / 1000).toFixed(1) + "s";
    }
    var pnlColor = (c.cycle_pnl || 0) > 0 ? "var(--color-success)" : (c.cycle_pnl || 0) < 0 ? "var(--color-error)" : "var(--color-text-muted)";
    var statusClass = c.status === "completed" ? "badge-high" : c.status === "failed" ? "badge-danger" : "badge-low";

    return '<tr>' +
      '<td class="mono">#' + c.id + '</td>' +
      '<td class="text-xs">' + formatTime(c.started_at) + '</td>' +
      '<td class="mono">' + duration + '</td>' +
      '<td class="mono">' + (c.markets_scanned || 0) + '</td>' +
      '<td class="mono">' + (c.opportunities_found || 0) + '</td>' +
      '<td class="mono">' + (c.bets_placed || 0) + '</td>' +
      '<td class="mono">' + (c.bets_resolved || 0) + '</td>' +
      '<td class="mono" style="color:' + pnlColor + '">' + formatMoney(c.cycle_pnl || 0) + '</td>' +
      '<td><span class="badge ' + statusClass + '">' + c.status + '</span></td>' +
    '</tr>';
  }).join("");
}

async function loadDecisions() {
  var data = await apiGet("/api/sim/decisions?limit=50");
  if (!data) return;
  var container = document.getElementById("decisionLog");

  if (!data.decisions || data.decisions.length === 0) {
    container.innerHTML = '<div class="empty-state"><p>Decisions logged after each cycle</p></div>';
    return;
  }

  container.innerHTML = data.decisions.map(function (d) {
    var icon = d.decision === "bet_placed" ? "&#9679;" : "&#8212;";
    var color = d.decision === "bet_placed" ? "var(--color-success)" : "var(--color-text-faint)";
    var details = d.details || {};

    return '<div class="decision-item">' +
      '<div class="decision-icon" style="color:' + color + '">' + icon + '</div>' +
      '<div class="decision-body">' +
        '<div class="decision-action">' +
          '<span class="badge badge-' + (d.decision === "bet_placed" ? "high" : "none") + '">' + d.decision.replace(/_/g, " ") + '</span>' +
          '<span class="text-xs text-muted">' + escapeHtml(truncate(d.question || "", 50)) + '</span>' +
        '</div>' +
        '<div class="decision-reason text-xs">' + escapeHtml(d.reasoning || "") + '</div>' +
        (details.side ? '<div class="decision-meta text-xs mono">' + details.side + ' @ ' + ((details.entry_price || 0) * 100).toFixed(1) + '&cent; &middot; $' + (details.size || 0).toFixed(2) + ' &middot; Edge ' + ((details.edge || 0) * 100).toFixed(1) + '%</div>' : '') +
      '</div>' +
      '<div class="decision-time text-xs text-muted">' + formatTime(d.created_at) + '</div>' +
    '</div>';
  }).join("");
}

/* ── Run Simulation Cycle ──────────────────────────────────── */
async function runSimCycle() {
  var btn = document.getElementById("runCycleBtn");
  btn.disabled = true;
  btn.innerHTML = '<svg class="spin" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg> Running...';
  showToast("Running simulation cycle...", "info");

  var result = await apiPost("/api/sim/run-cycle");
  btn.disabled = false;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Run Cycle';

  if (result && !result.error) {
    showToast(
      "Cycle complete: " + result.markets_scanned + " scanned, " +
      result.bets_placed + " bets placed, " + result.bets_resolved + " resolved",
      "success"
    );
    loadDashboard();
  } else {
    showToast("Cycle failed: " + (result ? result.error : "connection error"), "error");
  }
}

/* ── Settings ──────────────────────────────────────────────── */
async function loadSimSettings() {
  var data = await apiGet("/api/config");
  if (!data) return;
  var cfg = data.config || {};
  Object.keys(cfg).forEach(function (key) {
    var el = document.getElementById("cfg-" + key);
    if (el && cfg[key]) el.value = cfg[key];
  });
}

async function saveSimSettings() {
  var fields = [
    "sim_bankroll", "sim_max_bet", "sim_min_bet", "sim_daily_budget",
    "sim_min_edge", "sim_min_volume", "sim_max_open_positions", "sim_kelly_fraction"
  ];
  for (var i = 0; i < fields.length; i++) {
    var el = document.getElementById("cfg-" + fields[i]);
    if (el) await apiPost("/api/config", { key: fields[i], value: el.value });
  }
  showToast("Settings saved", "success");
}

async function resetSimulation() {
  if (!confirm("Reset all simulation data? This will clear all bets, cycles, and learning metrics, and reset the bankroll.")) return;
  var result = await apiPost("/api/sim/reset");
  if (result) {
    showToast("Simulation reset — bankroll: " + formatMoney(result.bankroll), "success");
    loadDashboard();
  }
}

/* ── Utilities ─────────────────────────────────────────────── */
function formatMoney(v) {
  if (v === null || v === undefined) return "$0.00";
  var n = parseFloat(v);
  var sign = n < 0 ? "-" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatCompact(v) {
  var n = parseFloat(v) || 0;
  if (n >= 1000000) return "$" + (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "K";
  return "$" + n.toFixed(0);
}

function centsStr(v) {
  if (!v && v !== 0) return "—";
  return (parseFloat(v) * 100).toFixed(1) + "\u00A2";
}

function escapeHtml(s) {
  if (!s) return "";
  var div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

function truncate(s, len) {
  if (!s) return "";
  return s.length > len ? s.substring(0, len) + "..." : s;
}

function formatTime(ts) {
  if (!ts) return "—";
  try {
    var d = new Date(ts + (ts.includes("Z") || ts.includes("+") ? "" : "Z"));
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ts; }
}

function formatDate(ts) {
  if (!ts) return "—";
  try {
    var d = new Date(ts + (ts.includes("Z") || ts.includes("+") ? "" : "Z"));
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch (e) { return ts; }
}

function timeSince(ts) {
  if (!ts) return "—";
  try {
    var d = new Date(ts + (ts.includes("Z") || ts.includes("+") ? "" : "Z"));
    var now = new Date();
    var sec = Math.floor((now - d) / 1000);
    if (sec < 60) return sec + "s";
    var min = Math.floor(sec / 60);
    if (min < 60) return min + "m";
    var hr = Math.floor(min / 60);
    if (hr < 24) return hr + "h";
    var days = Math.floor(hr / 24);
    return days + "d";
  } catch (e) { return "—"; }
}

function getCSS(prop) {
  return getComputedStyle(document.documentElement).getPropertyValue(prop).trim();
}

function showToast(message, type) {
  var container = document.getElementById("toastContainer");
  var toast = document.createElement("div");
  toast.className = "toast";
  var color = type === "success" ? "var(--color-success)" : type === "error" ? "var(--color-error)" : "var(--color-primary)";
  toast.innerHTML = '<div style="display:flex;align-items:center;gap:var(--space-2)">' +
    '<div style="width:8px;height:8px;border-radius:var(--radius-full);background:' + color + ';flex-shrink:0"></div>' +
    '<span>' + escapeHtml(message) + '</span></div>';
  container.appendChild(toast);
  setTimeout(function () {
    toast.style.opacity = "0";
    toast.style.transition = "opacity 300ms";
    setTimeout(function () { toast.remove(); }, 300);
  }, 4000);
}

async function refreshData() {
  var btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  btn.style.opacity = "0.5";
  await loadDashboard();
  btn.disabled = false;
  btn.style.opacity = "1";
  showToast("Data refreshed", "success");
}

/* ── Initialization ────────────────────────────────────────── */
async function init() {
  await loadDashboard();
  setInterval(function () {
    if (state.currentView === "dashboard") loadDashboard();
  }, 60000);
}

document.addEventListener("DOMContentLoaded", init);
