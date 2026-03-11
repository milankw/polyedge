# PolyEdge v2 — Copy-Trade Research Platform

Research tool to determine if copy-trading top Polymarket wallets can be consistently profitable. Tracks 100 hand-picked wallets, mirrors every trade with simulated money, and generates deep analytics.

## Quick Start

### Docker (recommended)
```bash
docker compose up --build
# Open http://localhost:8892
```

### Local Development
```bash
pip install -r backend/requirements.txt
python backend/seed.py
uvicorn backend.app:app --host 0.0.0.0 --port 8892
```

## Architecture

- **Backend**: FastAPI + SQLite (aiosqlite) + APScheduler
- **Frontend**: Vanilla JS SPA with Chart.js
- **Data**: Polymarket Data API (trades) + Gamma API (prices/resolution)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/health | Health check |
| GET | /api/stats | Quick stats |
| GET | /api/dashboard | Full dashboard data |
| GET | /api/wallets | All wallets |
| GET | /api/wallets/{id} | Wallet detail |
| GET | /api/wallets/{id}/trades | Wallet trades |
| GET | /api/trades | All trades (paginated) |
| GET | /api/trades/recent | Last 50 trades |
| GET | /api/snapshots | Equity curve data |
| GET | /api/scan-log | Scan history |
| POST | /api/scan | Trigger wallet scan |
| POST | /api/update-prices | Trigger price update |
| POST | /api/full-cycle | Full scan + prices |

## Scheduler

- Every 30 min: Scan wallets for new trades
- Every 15 min: Update prices + resolve markets
- Every 1 hour: Save snapshots for equity curves

## How It Works

1. **Scan**: Fetches trades from 100 tracked wallets via Polymarket Data API
2. **Mirror**: Creates simulated positions proportional to wallet allocation ($1000 default)
3. **Price**: Updates live prices via Gamma API, resolves completed markets
4. **Analyze**: Tracks P&L, win rates, equity curves across all wallets
