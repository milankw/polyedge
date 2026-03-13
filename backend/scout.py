"""
WalletScout -- 24/7 background process that discovers new Polymarket wallets,
scores them on a 100-point system, and queues the best for manual approval.

Runs alongside CopyEngine as an async background task.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import aiosqlite
import httpx

logger = logging.getLogger("polyedge.scout")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "polyedge.db")

# Default settings — overridden by scout_settings table
DEFAULT_SETTINGS = {
    "min_score_threshold": "45",
    "max_wallets_per_cycle": "10",
    "cycle_delay_seconds": "60",
    "eval_delay_seconds": "5",
    "discovery_market_trades": "1",
    "discovery_global_trades": "1",
}


class WalletScout:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

        # Loop state
        self._running: bool = False
        self._cycle_count: int = 0
        self._total_scanned: int = 0
        self._task: asyncio.Task | None = None

        # Settings loaded from DB
        self._min_score: float = 45.0
        self._max_per_cycle: int = 10
        self._cycle_delay: int = 60
        self._eval_delay: int = 5
        self._source_market_trades: bool = True
        self._source_global_trades: bool = True

        # Rotate discovery sources
        self._source_index: int = 0

        # HTTP client
        self._client: httpx.AsyncClient | None = None
        self._db: aiosqlite.Connection | None = None

    # ---- Lifecycle ---------------------------------------------------

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                timeout=httpx.Timeout(12.0, connect=5.0),
                http2=False,
            )

    async def _ensure_db(self):
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")

    async def _load_settings(self):
        await self._ensure_db()
        async with self._db.execute("SELECT key, value FROM scout_settings") as cur:
            rows = await cur.fetchall()
        settings = {r["key"]: r["value"] for r in rows}
        self._min_score = float(settings.get("min_score_threshold", "45"))
        self._max_per_cycle = int(settings.get("max_wallets_per_cycle", "10"))
        self._cycle_delay = int(settings.get("cycle_delay_seconds", "60"))
        self._eval_delay = int(settings.get("eval_delay_seconds", "5"))
        self._source_market_trades = settings.get("discovery_market_trades", "1") == "1"
        self._source_global_trades = settings.get("discovery_global_trades", "1") == "1"

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        if self._db:
            await self._db.close()
            self._db = None

    # ---- HTTP helpers ------------------------------------------------

    async def _api_get(self, url: str, params: dict | None = None) -> dict | list | None:
        await self._ensure_client()
        for attempt in range(3):
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code == 429:
                    backoff = 2.0 * (2 ** attempt)
                    logger.warning(f"429 rate limited, backing off {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                logger.warning(f"Timeout on {url} (attempt {attempt + 1})")
                await asyncio.sleep(1.0)
            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP {e.response.status_code} on {url}")
                return None
            except Exception as e:
                logger.warning(f"Request error: {e}")
                return None
        return None

    # ---- Discovery Sources -------------------------------------------

    async def _discover_from_market_trades(self) -> list[str]:
        """Fetch trades from top active markets to discover wallets."""
        wallets = []
        # Get active markets sorted by volume
        markets = await self._api_get(
            f"{GAMMA_API}/markets",
            params={"limit": "20", "active": "true", "order": "volume24hr", "ascending": "false"},
        )
        if not markets or not isinstance(markets, list):
            logger.warning("No markets returned from Gamma API")
            return wallets

        # Pick top 3 markets per cycle to limit API calls
        for market in markets[:10]:
            condition_id = market.get("conditionId") or market.get("condition_id")
            if not condition_id:
                continue
            trades = await self._api_get(
                f"{DATA_API}/trades",
                params={"market": condition_id, "limit": "500"},
            )
            if trades and isinstance(trades, list):
                for t in trades:
                    pw = (t.get("proxyWallet") or t.get("proxy_wallet") or "").lower().strip()
                    if pw and pw not in wallets:
                        wallets.append(pw)
            await asyncio.sleep(1.0)  # pace between market fetches

        logger.info(f"Discovered {len(wallets)} wallets from market trades")
        return wallets

    async def _discover_from_global_trades(self) -> list[str]:
        """Fetch recent global trades to discover wallets."""
        wallets = []
        trades = await self._api_get(
            f"{DATA_API}/trades",
            params={"limit": "500"},
        )
        if trades and isinstance(trades, list):
            for t in trades:
                pw = (t.get("proxyWallet") or t.get("proxy_wallet") or "").lower().strip()
                if pw and pw not in wallets:
                    wallets.append(pw)
        logger.info(f"Discovered {len(wallets)} wallets from global trades")
        return wallets

    async def _discover_wallets(self) -> list[str]:
        """Rotate between discovery sources."""
        sources = []
        if self._source_market_trades:
            sources.append(self._discover_from_market_trades)
        if self._source_global_trades:
            sources.append(self._discover_from_global_trades)
        if not sources:
            return []

        source_fn = sources[self._source_index % len(sources)]
        self._source_index += 1
        return await source_fn()

    # ---- Filtering known wallets ------------------------------------

    async def _filter_known(self, wallets: list[str]) -> list[str]:
        """Remove wallets already tracked or already evaluated."""
        await self._ensure_db()
        new = []
        for addr in wallets:
            # Check if already tracked
            async with self._db.execute(
                "SELECT 1 FROM wallets WHERE address = ?", (addr,)
            ) as cur:
                if await cur.fetchone():
                    continue
            # Check if already evaluated (within 24h)
            async with self._db.execute(
                "SELECT 1 FROM scout_candidates WHERE proxy_wallet = ? AND last_scored_at > datetime('now', '-24 hours')",
                (addr,),
            ) as cur:
                if await cur.fetchone():
                    continue
            new.append(addr)
        return new

    # ---- Scoring System (100 points) ---------------------------------

    def _score_win_rate(self, win_rate: float) -> int:
        """Win Rate: max 25 pts."""
        if win_rate >= 75:
            return 25
        if win_rate >= 70:
            return 20
        if win_rate >= 65:
            return 15
        if win_rate >= 60:
            return 10
        if win_rate >= 55:
            return 5
        return 0

    def _score_consistency(self, markets_traded: int) -> int:
        """Consistency (resolved markets): max 20 pts."""
        if markets_traded >= 200:
            return 20
        if markets_traded >= 100:
            return 15
        if markets_traded >= 50:
            return 10
        if markets_traded >= 20:
            return 5
        if markets_traded >= 10:
            return 2
        return 0

    def _score_roi(self, roi_pct: float) -> int:
        """ROI %: max 15 pts."""
        if roi_pct >= 50:
            return 15
        if roi_pct >= 25:
            return 10
        if roi_pct >= 10:
            return 5
        return 0

    def _score_lifetime_trades(self, trades: int) -> int:
        """Lifetime trades: max 15 pts."""
        if trades >= 200:
            return 15
        if trades >= 100:
            return 10
        if trades >= 50:
            return 5
        if trades >= 10:
            return 2
        return 0

    def _score_portfolio_size(self, total_value: float) -> int:
        """Portfolio size (mid-size sweet spot): max 15 pts."""
        if total_value > 200_000:
            return 5
        if total_value > 50_000:
            return 10
        if total_value > 5_000:
            return 15
        if total_value > 500:
            return 5
        return 0

    def _score_underrated(self, total_invested: float, win_rate: float) -> int:
        """Underrated bonus: max 10 pts. High win rate but smaller volume."""
        if win_rate < 55:
            return 0
        # Smaller volume relative to win rate = more underrated
        if total_invested < 5_000 and win_rate >= 65:
            return 10
        if total_invested < 20_000 and win_rate >= 60:
            return 7
        if total_invested < 50_000 and win_rate >= 55:
            return 4
        return 0

    async def _evaluate_wallet(self, address: str) -> dict | None:
        """Fetch positions + trades for a wallet and compute scores."""
        # Fetch positions
        positions = await self._api_get(
            f"{DATA_API}/positions",
            params={"user": address},
        )
        if positions is None:
            return None

        if not isinstance(positions, list):
            positions = []

        # Fetch trades (multiple pages for deeper history)
        trades = []
        for offset in [0, 200, 400]:
            page = await self._api_get(
                f"{DATA_API}/trades",
                params={"user": address, "limit": "200", "offset": str(offset)},
            )
            if page and isinstance(page, list):
                trades.extend(page)
                if len(page) < 200:
                    break  # No more pages
            else:
                break
            await asyncio.sleep(0.5)

        # Extract username from trades
        username = ""
        for t in trades:
            name = t.get("pseudonym") or t.get("name") or ""
            if name:
                username = name
                break

        # Calculate metrics from positions
        total_pnl = 0.0
        total_invested = 0.0
        wins = 0
        losses = 0
        markets_set = set()
        portfolio_value = 0.0

        for pos in positions:
            cash_pnl = float(pos.get("cashPnl") or pos.get("cash_pnl") or 0)
            initial_val = float(pos.get("initialValue") or pos.get("initial_value") or 0)
            current_val = float(pos.get("currentValue") or pos.get("current_value") or 0)
            cond_id = pos.get("conditionId") or pos.get("condition_id") or ""

            total_pnl += cash_pnl
            total_invested += initial_val
            portfolio_value += current_val

            if cond_id:
                markets_set.add(cond_id)

            # Determine win/loss from resolved positions
            pct_pnl = float(pos.get("percentPnl") or pos.get("percent_pnl") or 0)
            if pct_pnl > 0:
                wins += 1
            elif pct_pnl < 0:
                losses += 1

        total_positions = len(positions)
        markets_traded = len(markets_set)
        lifetime_trades = len(trades)

        resolved = wins + losses
        win_rate = (wins / resolved * 100) if resolved > 0 else 0
        roi_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        avg_trade_size = (total_invested / lifetime_trades) if lifetime_trades > 0 else 0

        # Compute scores
        s_win_rate = self._score_win_rate(win_rate)
        s_consistency = self._score_consistency(markets_traded)
        s_roi = self._score_roi(roi_pct)
        s_trades = self._score_lifetime_trades(lifetime_trades)
        s_portfolio = self._score_portfolio_size(portfolio_value)
        s_underrated = self._score_underrated(total_invested, win_rate)

        total_score = s_win_rate + s_consistency + s_roi + s_trades + s_portfolio + s_underrated

        # Hard gates: reject wallets that are losing money or too small profit
        if roi_pct < 0:
            total_score = 0  # Negative ROI = not profitable, instant reject
        if total_pnl < 5000:
            total_score = min(total_score, 30)  # Below k profit = cap score below threshold

        breakdown = {
            "win_rate": {"points": s_win_rate, "max": 25, "value": round(win_rate, 1)},
            "consistency": {"points": s_consistency, "max": 20, "value": markets_traded},
            "roi": {"points": s_roi, "max": 15, "value": round(roi_pct, 1)},
            "lifetime_trades": {"points": s_trades, "max": 15, "value": lifetime_trades},
            "portfolio_size": {"points": s_portfolio, "max": 15, "value": round(portfolio_value, 2)},
            "underrated": {"points": s_underrated, "max": 10, "value": round(total_invested, 2)},
        }

        return {
            "proxy_wallet": address,
            "username": username,
            "score": total_score,
            "total_positions": total_positions,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "total_invested": round(total_invested, 2),
            "roi_pct": round(roi_pct, 2),
            "avg_trade_size": round(avg_trade_size, 2),
            "markets_traded": markets_traded,
            "lifetime_trades": lifetime_trades,
            "score_breakdown": json.dumps(breakdown),
        }

    # ---- DB persistence ----------------------------------------------

    async def _save_candidate(self, data: dict, discovered_via: str, discovered_market: str = ""):
        await self._ensure_db()
        await self._db.execute(
            """INSERT INTO scout_candidates
                (proxy_wallet, username, score, total_positions, wins, losses,
                 win_rate, total_pnl, total_invested, roi_pct, avg_trade_size,
                 markets_traded, lifetime_trades, discovered_via, discovered_market,
                 status, score_breakdown, last_scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, datetime('now'))
            ON CONFLICT(proxy_wallet) DO UPDATE SET
                score=excluded.score, username=excluded.username,
                total_positions=excluded.total_positions, wins=excluded.wins,
                losses=excluded.losses, win_rate=excluded.win_rate,
                total_pnl=excluded.total_pnl, total_invested=excluded.total_invested,
                roi_pct=excluded.roi_pct, avg_trade_size=excluded.avg_trade_size,
                markets_traded=excluded.markets_traded, lifetime_trades=excluded.lifetime_trades,
                score_breakdown=excluded.score_breakdown, last_scored_at=datetime('now')
            """,
            (
                data["proxy_wallet"], data["username"], data["score"],
                data["total_positions"], data["wins"], data["losses"],
                data["win_rate"], data["total_pnl"], data["total_invested"],
                data["roi_pct"], data["avg_trade_size"], data["markets_traded"],
                data["lifetime_trades"], discovered_via, discovered_market,
                data["score_breakdown"],
            ),
        )
        await self._db.commit()

    # ---- Main Loop ---------------------------------------------------

    async def start_loop(self):
        self._running = True
        logger.info("WalletScout started")

        await self._ensure_client()
        await self._ensure_db()
        await self._load_settings()

        while self._running:
            try:
                await self._run_cycle()
                self._cycle_count += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Scout cycle error: {e}")

            # Reload settings each cycle in case they changed
            try:
                await self._load_settings()
            except Exception:
                pass

            await asyncio.sleep(self._cycle_delay)

        logger.info("WalletScout stopped")

    async def _run_cycle(self):
        """One discovery + scoring cycle."""
        cycle_start = time.monotonic()

        # Determine discovery source name for this cycle
        sources = []
        if self._source_market_trades:
            sources.append("market_trades")
        if self._source_global_trades:
            sources.append("global_trades")
        if not sources:
            logger.info("No discovery sources enabled, skipping cycle")
            return

        source_name = sources[(self._source_index) % len(sources)]

        # Discover wallets
        raw_wallets = await self._discover_wallets()
        if not raw_wallets:
            logger.info("No wallets discovered this cycle")
            return

        # Filter out known wallets
        new_wallets = await self._filter_known(raw_wallets)
        logger.info(f"Cycle {self._cycle_count}: {len(raw_wallets)} raw -> {len(new_wallets)} new wallets")

        # Evaluate up to max_per_cycle
        evaluated = 0
        for addr in new_wallets[:self._max_per_cycle]:
            if not self._running:
                break

            self._total_scanned += 1
            result = await self._evaluate_wallet(addr)

            if result is None:
                logger.debug(f"Could not evaluate {addr[:10]}...")
                await asyncio.sleep(self._eval_delay)
                continue

            evaluated += 1
            score = result["score"]

            if score >= self._min_score:
                await self._save_candidate(result, discovered_via=source_name)
                logger.info(f"Queued {result['username'] or addr[:10]}... score={score}")
            else:
                # Save below-threshold so we don't re-scan
                result_copy = dict(result)
                await self._save_candidate(result_copy, discovered_via=source_name)
                # Mark as auto-rejected (below threshold)
                await self._db.execute(
                    "UPDATE scout_candidates SET status = 'rejected' WHERE proxy_wallet = ? AND score < ?",
                    (addr, self._min_score),
                )
                await self._db.commit()

            await asyncio.sleep(self._eval_delay)

        elapsed = time.monotonic() - cycle_start
        logger.info(f"Cycle {self._cycle_count} done: evaluated {evaluated} wallets in {elapsed:.1f}s")

    def stop_loop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "total_scanned": self._total_scanned,
        }
