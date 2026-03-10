# PolyEdge — Polymarket Prediction Engine

Data-driven prediction market trading engine with live market scanning, opportunity detection, position tracking, and P&L analytics.

Connects to [Polymarket's public APIs](https://docs.polymarket.com/) to scan markets in real-time and detect mispriced opportunities using multiple signal types.

---

## Features

- **Market Scanner** — Live feed of 50+ Polymarket markets with prices, volume, and signal detection
- **Opportunity Detection** — 5 signal types: arbitrage gaps, volume surges, low-liquidity mispricing, near-expiry convergence, high-confidence analysis
- **Position Tracking** — Log trades, track entry/current prices, calculate P&L
- **Strategy Analysis** — Performance tracking by strategy type to identify profitable niches
- **Dashboard** — KPI cards, P&L charts, top opportunities, all auto-refreshing

---

## Quick Start (Docker)

**This is the easiest way.** It runs on any free port without touching your existing services.

```bash
git clone https://github.com/YOUR_USERNAME/polyedge.git
cd polyedge
docker compose up -d
```

Dashboard opens at **http://your-server-ip:3000**
API available at **http://your-server-ip:8000**

To change the dashboard port, edit `docker-compose.yml` and change `3000:80` to `YOUR_PORT:80`.

---

## Manual Setup (No Docker)

### 1. Backend (Python API)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python api_server.py
```

API runs on port **8000**.

### 2. Frontend

```bash
npm install -g serve
cd frontend
serve . -l 3000 --single
```

Dashboard runs on port **3000**. Open **http://localhost:3000** in your browser.

---

## Deploy Behind Existing Nginx

If you already have nginx on port 80, see `nginx.conf.example` for how to proxy PolyEdge through a subdomain or path prefix without conflicting.

---

## APIs Used

### Already integrated (no keys needed)

| API | Base URL | Purpose |
|-----|----------|---------|
| Polymarket Gamma API | `gamma-api.polymarket.com` | Market discovery, events, tags, sports |
| Polymarket CLOB API (public) | `clob.polymarket.com` | Orderbooks, prices, spreads, history |
| Polymarket Data API | `data-api.polymarket.com` | Positions, trades, leaderboards |

### Required for live trading

To place actual bets, you need Polymarket CLOB API credentials:

1. Create a Polymarket account at [polymarket.com](https://polymarket.com)
2. Fund your account with USDC.e on Polygon
3. Export your API credentials using the [Polymarket SDK](https://docs.polymarket.com/trading/quickstart):

```python
pip install py-clob-client

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

client = ClobClient(
    "https://clob.polymarket.com",
    chain_id=137,
    key="YOUR_PRIVATE_KEY"
)

creds = client.create_or_derive_api_creds()
print(f"API Key: {creds.api_key}")
print(f"Secret: {creds.api_secret}")
print(f"Passphrase: {creds.api_passphrase}")
```

4. Enter these in the dashboard under **Settings > API Configuration**

---

## Signal Types

| Signal | What it detects | Edge |
|--------|----------------|------|
| **Arbitrage** | YES + NO prices don't sum to $1.00 | Variable |
| **Volume Surge** | 24h volume 3x+ above daily average | ~5% |
| **Low Liquidity** | Thin orderbook with active trading | ~8% |
| **Near Expiry** | Market resolving in < 3 days | ~4% |
| **High Confidence** | Market at 90%+ with high volume | ~2% |

---

## Project Structure

```
polyedge/
├── backend/
│   ├── api_server.py      # FastAPI backend — market scanner, analytics, DB
│   └── requirements.txt   # Python dependencies
├── frontend/
│   ├── index.html          # Dashboard UI
│   ├── app.js              # Frontend logic
│   ├── base.css            # Reset/base styles
│   └── style.css           # Design tokens + components
├── Dockerfile
├── docker-compose.yml
├── nginx.conf.example      # Proxy config for existing nginx
├── start.sh                # Container startup script
└── README.md
```

---

## Strategy

**Phase 1: Exploration** — Many small bets ($1–5) across categories. Track which signal types and markets produce consistent returns.

**Phase 2: Identification** — After 20+ bets, strategies with >55% win rate get flagged for scaling.

**Phase 3: Scaling** — Increase bet sizes on proven strategies. Continue small exploratory bets to discover new edges.

---

## License

MIT
