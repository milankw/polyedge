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
| GET | /api/copy-trade/signals | Copy trade signals (last 7 days) |
| GET | /api/copy-trade/signals/{id} | Signal detail with filter results |
| GET | /api/copy-trade/settings | Copy trade filter settings |
| POST | /api/copy-trade/settings | Update filter settings |
| POST | /api/copy-trade/scan | Trigger copy trade wallet poll |
| GET | /api/copy-trade/status | Copy trade monitor status |

## Scheduler

- Every 30 min: Scan wallets for new trades
- Every 15 min: Update prices + resolve markets
- Every 1 hour: Save snapshots for equity curves
- Every 5 min: Copy trade wallet poll (detect new positions)

## How It Works

1. **Scan**: Fetches trades from 100 tracked wallets via Polymarket Data API
2. **Mirror**: Creates simulated positions proportional to wallet allocation ($1000 default)
3. **Price**: Updates live prices via Gamma API, resolves completed markets
4. **Analyze**: Tracks P&L, win rates, equity curves across all wallets

## Copy Trading Module

The copy trading module monitors tracked wallets for new positions and runs each through a 10-filter safety chain before generating signals. Every signal is logged — only those passing all filters trigger alerts.

### Safety Filters

1. **Liquidity** — Market must have at least $75K in its liquidity pool. Small pools mean you can't enter/exit without moving the price against you.
2. **Entry Timing** — Position must be less than 5 minutes old. Stale signals mean the price has already moved.
3. **Wallet Position vs Pool** — The wallet's position can't exceed 5% of the pool. If one wallet is too big relative to the market, that's a sign of thin liquidity or market manipulation risk.
4. **24H Volume** — Market must have at least $25K in trading volume over the last 24 hours. Low volume = hard to exit.
5. **Unique Traders** — At least 50 unique traders in the market. More participants = more reliable price discovery.
6. **Resolution Date** — Market must resolve between 3 and 45 days from now. Too soon means not enough edge; too far means too much capital locked up.
7. **Wallet Win Rate** — The wallet must have a 55%+ historical win rate across 20+ resolved markets. Filters out wallets without a proven track record.
8. **Price Movement 6H** — Price can't have moved more than 12% in the last 6 hours. Big recent moves suggest the opportunity is already priced in.
9. **Multi-Wallet Confirmation** — At least 2 tracked wallets must hold the same position. Independent confirmation from multiple smart wallets increases confidence.
10. **Bankroll Exposure** — No single trade can exceed 5% of the total bankroll ($10K default). Basic risk management to prevent blowing up on one bet.

### Configuration

All filter thresholds are stored in the database and editable through the UI (Copy Trading > Filter Settings). The module supports MANUAL mode (log + alert only) and AUTO mode (future: execute trades automatically).

### Telegram Alerts

When a signal passes all 10 filters, the module sends a Telegram alert with market details, filter results, and entry price. Configure `telegram_bot_token` and `telegram_chat_id` in settings to enable.
