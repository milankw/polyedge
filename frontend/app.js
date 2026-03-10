/* ── API Configuration ──────────────────────────────────────── */
// Auto-detect: use same hostname as the page, port 8000 for API
var API = window.location.protocol + "//" + window.location.hostname + ":8000";

/* ── State ─────────────────────────────────────────────────── */
var state = {
  currentView: "dashboard",
  markets: [],
  opportunities: [],
  positions: [],
  analytics: null,
  pnlChart: null,
  lastScan: null,
};

/* ── Theme Toggle ──────────────────────────────────────────── */
(function () {
  var t = document.querySelector("[data-theme-toggle]");
  var r = document.documentElement;
  var d = "dark"; // Default to dark for trading terminal
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

  // Hide all views
  document.querySelectorAll(".view-section").forEach(function (s) {
    s.classList.remove("active");
  });

  // Show target
  var target = document.getElementById("view-" + view);
  if (target) target.classList.add("active");

  // Update sidebar active
  document.querySelectorAll(".sidebar-nav button").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-view") === view);
  });

  // Update header
  var titles = {
    dashboard: "Dashboard",
    scanner: "Market Scanner",
    opportunities: "Opportunities",
    positions: "Positions",
    strategies: "Strategy Analysis",
    settings: "Settings",
  };
  document.getElementById("pageTitle").textContent = titles[view] || view;

  // Load data for view
  if (view === "scanner") loadMarkets();
  if (view === "positions") loadPositions("open");
  if (view === "strategies") loadStrategies();
}

/* ── API Calls ─────────────────────────────────────────────── */
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
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return await resp.json();
  } catch (e) {
    console.error("API error:", path, e);
    return null;
  }
}

async function apiPatch(path, body) {
  try {
    var resp = await fetch(API + path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await resp.json();
  } catch (e) {
    console.error("API error:", path, e);
    return null;
  }
}

/* ── Dashboard ─────────────────────────────────────────────── */
async function loadDashboard() {
  var data = await apiGet("/api/analytics/summary");
  if (!data) {
    document.getElementById("engineStatus").textContent = "Offline";
    document.getElementById("statusDot").style.background = "var(--color-error)";
    return;
  }

  document.getElementById("engineStatus").textContent = "Engine Online";
  document.getElementById("statusDot").style.background = "var(--color-success)";
  state.analytics = data;

  // KPIs
  var pnlEl = document.getElementById("kpi-total-pnl");
  var pnlVal = data.total_pnl;
  pnlEl.textContent = formatUSD(pnlVal);
  pnlEl.className = "kpi-value " + (pnlVal > 0 ? "positive" : pnlVal < 0 ? "negative" : "neutral");

  var deltaEl = document.getElementById("kpi-total-pnl-delta");
  deltaEl.textContent = "Realized: " + formatUSD(data.realized_pnl) + " | Unrealized: " + formatUSD(data.unrealized_pnl);
  deltaEl.className = "kpi-delta " + (pnlVal > 0 ? "up" : pnlVal < 0 ? "down" : "flat");

  document.getElementById("kpi-open-positions").textContent = data.open_positions;
  document.getElementById("kpi-exposure").textContent = formatUSD(data.total_exposure) + " exposure";

  var wrEl = document.getElementById("kpi-win-rate");
  wrEl.textContent = data.win_rate + "%";
  wrEl.className = "kpi-value " + (data.win_rate >= 55 ? "positive" : data.win_rate > 0 ? "neutral" : "neutral");
  document.getElementById("kpi-record").textContent = data.wins + "W / " + data.losses + "L";

  // P&L Chart
  updatePnlChart(data.daily_pnl);

  // Load recent positions
  var posData = await apiGet("/api/positions?status=open");
  if (posData && posData.positions.length > 0) {
    renderPositionsTable("recentPositions", posData.positions.slice(0, 5), true);
  }
}

function updatePnlChart(dailyPnl) {
  var ctx = document.getElementById("pnlChart");
  if (!ctx) return;

  if (state.pnlChart) {
    state.pnlChart.destroy();
  }

  var labels = [];
  var values = [];
  var cumulative = 0;

  if (dailyPnl && dailyPnl.length > 0) {
    dailyPnl.forEach(function (d) {
      labels.push(d.day);
      cumulative += d.daily_pnl;
      values.push(cumulative);
    });
  } else {
    // Demo data
    var now = new Date();
    for (var i = 29; i >= 0; i--) {
      var date = new Date(now);
      date.setDate(date.getDate() - i);
      labels.push(date.toISOString().slice(5, 10));
      values.push(0);
    }
  }

  var chartColor = getComputedStyle(document.documentElement).getPropertyValue("--color-primary").trim();
  var surfaceColor = getComputedStyle(document.documentElement).getPropertyValue("--color-surface").trim();
  var textMuted = getComputedStyle(document.documentElement).getPropertyValue("--color-text-faint").trim();

  state.pnlChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels,
      datasets: [{
        label: "Cumulative P&L",
        data: values,
        borderColor: chartColor,
        backgroundColor: chartColor + "20",
        fill: true,
        tension: 0.4,
        pointRadius: 0,
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
          callbacks: {
            label: function (context) {
              return formatUSD(context.parsed.y);
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { color: textMuted, font: { size: 11 }, maxTicksLimit: 8 },
          border: { display: false },
        },
        y: {
          grid: { color: textMuted + "15" },
          ticks: {
            color: textMuted,
            font: { size: 11 },
            callback: function (v) { return "$" + v.toFixed(0); },
          },
          border: { display: false },
        },
      },
      interaction: {
        intersect: false,
        mode: "index",
      },
    },
  });
}

/* ── Market Scanner ────────────────────────────────────────── */
async function loadMarkets(category) {
  var params = "?limit=50";
  if (category && category !== "all") {
    params += "&category=" + category;
  }

  document.getElementById("marketsBody").innerHTML =
    '<tr><td colspan="7" style="text-align:center;padding:var(--space-8);color:var(--color-text-muted);">Scanning markets...</td></tr>';

  var data = await apiGet("/api/markets" + params);
  if (!data) {
    document.getElementById("marketsBody").innerHTML =
      '<tr><td colspan="7" style="text-align:center;padding:var(--space-8);color:var(--color-error);">Failed to load markets</td></tr>';
    return;
  }

  state.markets = data.markets;
  document.getElementById("marketCount").textContent = data.total + " markets loaded";
  renderMarketsTable(data.markets);
}

function renderMarketsTable(markets) {
  var tbody = document.getElementById("marketsBody");
  if (!markets || markets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:var(--space-8);color:var(--color-text-muted);">No markets found</td></tr>';
    return;
  }

  tbody.innerHTML = markets.map(function (m) {
    var edgePct = Math.min(m.edge * 100, 100);
    var signalHtml = (m.signals || []).map(function (s) {
      return '<span class="signal-pill" data-type="' + s.type + '">' + s.type.replace(/_/g, " ") + "</span>";
    }).join("");

    return '<tr>' +
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

/* ── Opportunities ─────────────────────────────────────────── */
async function runScan() {
  showToast("Scanning markets for opportunities...", "info");

  var data = await apiGet("/api/markets/scan?min_edge=0.03&min_volume=500&limit=100");
  if (!data) {
    showToast("Scan failed — check connection", "error");
    return;
  }

  state.opportunities = data.opportunities;
  state.lastScan = new Date();

  // Update KPI
  document.getElementById("kpi-signals").textContent = data.opportunities.length;
  document.getElementById("kpi-signals-detail").textContent =
    "Scanned " + data.scanned + " markets at " + new Date().toLocaleTimeString();

  showToast("Found " + data.opportunities.length + " opportunities from " + data.scanned + " markets", "success");

  renderOpportunities(data.opportunities);

  // Update dashboard top opportunities
  renderTopOpportunities(data.opportunities.slice(0, 5));
}

function renderOpportunities(opps) {
  var container = document.getElementById("opportunitiesList");
  document.getElementById("oppCount").textContent = opps.length + " found";

  if (!opps || opps.length === 0) {
    container.innerHTML = '<div class="empty-state"><p>No opportunities above threshold</p></div>';
    return;
  }

  container.innerHTML = '<div style="overflow-x: auto;"><table class="data-table"><thead><tr>' +
    '<th>Market</th><th>Edge</th><th>Confidence</th><th>Side</th><th>Size</th><th>Volume</th><th>Signals</th>' +
    '</tr></thead><tbody>' +
    opps.map(function (o) {
      var signalHtml = (o.signals || []).map(function (s) {
        return '<span class="signal-pill" data-type="' + s.type + '">' + s.type.replace(/_/g, " ") + "</span>";
      }).join("");

      return '<tr>' +
        '<td><div class="market-question" title="' + escapeHtml(o.question) + '">' + escapeHtml(o.question) + '</div></td>' +
        '<td class="mono" style="color:var(--color-primary);font-weight:600">' + (o.edge * 100).toFixed(1) + '%</td>' +
        '<td><span class="badge badge-' + o.confidence + '">' + o.confidence + '</span></td>' +
        '<td><span class="badge badge-' + o.recommended_side.toLowerCase() + '">' + o.recommended_side + '</span></td>' +
        '<td class="mono">' + formatUSD(o.recommended_size) + '</td>' +
        '<td class="mono">' + formatCompact(o.volume) + '</td>' +
        '<td><div class="signal-pills">' + signalHtml + '</div></td>' +
        '</tr>';
    }).join("") +
    '</tbody></table></div>';
}

function renderTopOpportunities(opps) {
  var container = document.getElementById("topOpportunities");
  if (!opps || opps.length === 0) return;

  container.innerHTML = opps.map(function (o) {
    return '<div style="display:flex;justify-content:space-between;align-items:center;padding:var(--space-3) 0;border-bottom:1px solid var(--color-divider)">' +
      '<div style="flex:1;min-width:0;margin-right:var(--space-3)"><div class="market-question" style="font-size:var(--text-sm)">' + escapeHtml(o.question) + '</div>' +
      '<div style="font-size:var(--text-xs);color:var(--color-text-muted);margin-top:2px">' +
      (o.signals || []).map(function (s) { return s.type.replace(/_/g, " "); }).join(" · ") + '</div></div>' +
      '<div style="text-align:right;flex-shrink:0"><div class="mono" style="color:var(--color-primary);font-weight:600;font-size:var(--text-sm)">' + (o.edge * 100).toFixed(1) + '% edge</div>' +
      '<span class="badge badge-' + o.confidence + '" style="margin-top:2px">' + o.confidence + '</span></div></div>';
  }).join("");
}

/* ── Positions ─────────────────────────────────────────────── */
async function loadPositions(status) {
  // Update tab
  var tabs = document.querySelectorAll("#view-positions .tab-filter");
  tabs.forEach(function (t) {
    t.classList.toggle("active", t.textContent.toLowerCase() === status);
  });

  var data = await apiGet("/api/positions?status=" + status);
  if (!data) return;

  state.positions = data.positions;
  renderPositionsTable("positionsBody", data.positions);
}

function renderPositionsTable(containerId, positions, compact) {
  var tbody = document.getElementById(containerId);
  if (!positions || positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:var(--space-8);color:var(--color-text-muted);">No positions</td></tr>';
    return;
  }

  tbody.innerHTML = positions.map(function (p) {
    var pnlClass = p.pnl > 0 ? "positive" : p.pnl < 0 ? "negative" : "neutral";
    return '<tr>' +
      '<td><div class="market-question">' + escapeHtml(p.question || "Market #" + p.market_id) + '</div></td>' +
      '<td><span class="badge badge-' + p.side.toLowerCase() + '">' + p.side + '</span></td>' +
      '<td class="mono">' + (p.entry_price * 100).toFixed(1) + '&cent;</td>' +
      '<td class="mono">' + (p.current_price * 100).toFixed(1) + '&cent;</td>' +
      '<td class="mono">' + formatUSD(p.size) + '</td>' +
      '<td class="mono" style="color:var(--color-' + (p.pnl > 0 ? "success" : p.pnl < 0 ? "error" : "text-muted") + ');font-weight:600">' +
      (p.pnl >= 0 ? "+" : "") + formatUSD(p.pnl) + '</td>' +
      '<td><span style="font-size:var(--text-xs);color:var(--color-text-muted)">' + escapeHtml(p.strategy || "") + '</span></td>' +
      '<td>' + (p.status === "open" ?
        '<button class="btn btn-ghost btn-sm" onclick="closePosition(' + p.id + ', \'won\')">Won</button>' +
        '<button class="btn btn-ghost btn-sm" onclick="closePosition(' + p.id + ', \'lost\')">Lost</button>'
        : '<span style="font-size:var(--text-xs);color:var(--color-text-faint)">' + p.status + '</span>') +
      '</td></tr>';
  }).join("");
}

async function closePosition(id, status) {
  await apiPatch("/api/positions/" + id, { status: status });
  showToast("Position marked as " + status, status === "won" ? "success" : "error");
  loadPositions("open");
  loadDashboard();
}

/* ── Strategies ────────────────────────────────────────────── */
async function loadStrategies() {
  var data = await apiGet("/api/analytics/strategies");
  if (!data || !data.strategies || data.strategies.length === 0) return;

  var container = document.getElementById("strategiesList");
  container.innerHTML = '<div style="overflow-x: auto;"><table class="data-table"><thead><tr>' +
    '<th>Strategy</th><th>Bets</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Total P&L</th><th>Status</th>' +
    '</tr></thead><tbody>' +
    data.strategies.map(function (s) {
      return '<tr>' +
        '<td style="font-weight:600">' + escapeHtml(s.name) + '</td>' +
        '<td class="mono">' + s.total_bets + '</td>' +
        '<td class="mono" style="color:var(--color-success)">' + s.wins + '</td>' +
        '<td class="mono" style="color:var(--color-error)">' + s.losses + '</td>' +
        '<td class="mono">' + s.win_rate + '%</td>' +
        '<td class="mono" style="color:var(--color-' + (s.total_pnl >= 0 ? "success" : "error") + ')">' + formatUSD(s.total_pnl) + '</td>' +
        '<td><span class="badge badge-' + (s.status === "scaling" ? "high" : s.status === "promising" ? "medium" : "low") + '">' + s.status + '</span></td>' +
        '</tr>';
    }).join("") +
    '</tbody></table></div>';
}

/* ── Settings ──────────────────────────────────────────────── */
async function saveSettings() {
  var fields = ["max_bet_size", "daily_budget", "min_edge", "min_volume"];
  for (var i = 0; i < fields.length; i++) {
    var el = document.getElementById("cfg-" + fields[i]);
    if (el) {
      await apiPost("/api/config", { key: fields[i], value: el.value });
    }
  }
  showToast("Settings saved", "success");
}

async function saveApiConfig() {
  var fields = ["wallet_address", "polymarket_key", "polymarket_secret", "polymarket_passphrase"];
  for (var i = 0; i < fields.length; i++) {
    var el = document.getElementById("cfg-" + fields[i]);
    if (el && el.value) {
      await apiPost("/api/config", { key: fields[i], value: el.value });
    }
  }
  showToast("API configuration saved", "success");
}

async function loadSettings() {
  var data = await apiGet("/api/config");
  if (!data) return;
  var cfg = data.config || {};
  Object.keys(cfg).forEach(function (key) {
    var el = document.getElementById("cfg-" + key);
    if (el && cfg[key] && !cfg[key].endsWith("...")) {
      el.value = cfg[key];
    }
  });
}

/* ── Utilities ─────────────────────────────────────────────── */
function formatUSD(v) {
  if (v === null || v === undefined) return "$0.00";
  var n = parseFloat(v);
  return "$" + Math.abs(n).toFixed(2);
}

function formatCompact(v) {
  var n = parseFloat(v) || 0;
  if (n >= 1000000) return "$" + (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "K";
  return "$" + n.toFixed(0);
}

function escapeHtml(s) {
  if (!s) return "";
  var div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
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
  if (state.currentView === "scanner") await loadMarkets();
  btn.disabled = false;
  btn.style.opacity = "1";
  showToast("Data refreshed", "success");
}

/* ── Initialization ────────────────────────────────────────── */
async function init() {
  await loadDashboard();
  await loadSettings();
  // Auto-refresh every 60s
  setInterval(function () {
    if (state.currentView === "dashboard") loadDashboard();
  }, 60000);
}

document.addEventListener("DOMContentLoaded", init);
