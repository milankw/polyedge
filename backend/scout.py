"""
WalletScout v2 -- High-throughput wallet discovery engine.

Discovery sources (rotated each cycle):
  1. market_trades    - trades from top active markets (broad sweep)
  2. global_trades    - recent global trade feed with pagination
  3. large_trades     - filtered for $500+ trades (quality filter)
  4. holders          - top holders of active markets (different pool)
  5. deep_pagination  - paginate deep into trade history (offset 1000-9000)

Speed optimisations:
  - In-memory seen-set avoids DB round-trips for already-scanned wallets
  - Parallel evaluation via asyncio.gather (batch of 5 at a time)
  - Adaptive rate limiting (backs off on 429, speeds up when clear)
"""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone

import aiosqlite
import httpx

logger = logging.getLogger("polyedge.scout")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "polyedge.db")

# All discovery source names
SOURCE_NAMES = [
    "market_trades",
    "global_trades",
    "large_trades",
    "holders",
    "deep_pagination",
]


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
        self._max_per_cycle: int = 50
        self._cycle_delay: int = 30
        self._eval_delay: float = 1.0
        self._parallel_evals: int = 5

        # Rotate discovery sources
        self._source_index: int = 0

        # In-memory seen-set to skip DB lookups for already-scanned wallets
        self._seen_wallets: set[str] = set()
        self._tracked_wallets: set[str] = set()

        # Market cache (refreshed periodically)
        self._market_cache: list[dict] = []
        self._market_cache_time: float = 0

        # Stats
        self._last_cycle_found: int = 0
        self._last_cycle_new: int = 0
        self._last_cycle_queued: int = 0

        # HTTP client
        self._client: httpx.AsyncClient | None = None
        self._db: aiosqlite.Connection | None = None

    # ---- Lifecycle ---------------------------------------------------

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                timeout=httpx.Timeout(15.0, connect=5.0),
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
        self._max_per_cycle = int(settings.get("max_wallets_per_cycle", "50"))
        self._cycle_delay = int(settings.get("cycle_delay_seconds", "30"))
        self._eval_delay = float(settings.get("eval_delay_seconds", "1"))
        self._parallel_evals = int(settings.get("parallel_evals", "5"))

    async def _load_seen_wallets(self):
        """Load all previously scanned wallets into memory on startup."""
        await self._ensure_db()
        # Load tracked wallets (already in our wallets table)
        async with self._db.execute("SELECT address FROM wallets") as cur:
            rows = await cur.fetchall()
        self._tracked_wallets = {r["address"].lower() for r in rows}

        # Load all previously evaluated candidates
        async with self._db.execute("SELECT proxy_wallet FROM scout_candidates") as cur:
            rows = await cur.fetchall()
        self._seen_wallets = {r["proxy_wallet"].lower() for r in rows}
        logger.info(f"Loaded {len(self._seen_wallets)} seen + {len(self._tracked_wallets)} tracked wallets into memory")

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
                    backoff = 3.0 * (2 ** attempt)
                    logger.warning(f"429 rate limited, backing off {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                logger.warning(f"Timeout on {url} (attempt {attempt + 1})")
                await asyncio.sleep(1.5)
            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP {e.response.status_code} on {url}")
                return None
            except Exception as e:
                logger.warning(f"Request error: {e}")
                return None
        return None

    # ---- Market cache ------------------------------------------------

    async def _refresh_markets(self):
        """Fetch active markets sorted by volume. Cache for 10 minutes."""
        now = time.monotonic()
        if self._market_cache and (now - self._market_cache_time) < 600:
            return
        markets = await self._api_get(
            f"{GAMMA_API}/markets",
            params={"limit": "100", "active": "true", "order": "volume24hr", "ascending": "false"},
        )
        if markets and isinstance(markets, list):
            self._market_cache = markets
            self._market_cache_time = now
            logger.info(f"Refreshed market cache: {len(markets)} active markets")

    # ---- Discovery Sources -------------------------------------------

    def _extract_wallets(self, trades: list) -> list[str]:
        """Extract unique proxy wallet addresses from trade list."""
        seen = set()
        result = []
        for t in trades:
            pw = (t.get("proxyWallet") or t.get("proxy_wallet") or "").lower().strip()
            if pw and pw not in seen:
                seen.add(pw)
                result.append(pw)
        return result

    async def _discover_market_trades(self) -> list[str]:
        """Sweep trades from top 30 active markets."""
        await self._refresh_markets()
        wallets = []
        seen = set()

        # Shuffle to avoid always scanning the same markets first
        markets = list(self._market_cache[:50])
        random.shuffle(markets)

        for market in markets[:30]:
            cid = market.get("conditionId") or market.get("condition_id")
            if not cid:
                continue
            trades = await self._api_get(
                f"{DATA_API}/trades",
                params={"market": cid, "limit": "500"},
            )
            if trades and isinstance(trades, list):
                for pw in self._extract_wallets(trades):
                    if pw not in seen:
                        seen.add(pw)
                        wallets.append(pw)
            await asyncio.sleep(0.5)

        logger.info(f"[market_trades] {len(wallets)} wallets from 30 markets")
        return wallets

    async def _discover_global_trades(self) -> list[str]:
        """Fetch recent global trades across multiple pages."""
        wallets = []
        seen = set()

        for offset in range(0, 4000, 1000):
            trades = await self._api_get(
                f"{DATA_API}/trades",
                params={"limit": "1000", "offset": str(offset)},
            )
            if not trades or not isinstance(trades, list):
                break
            for pw in self._extract_wallets(trades):
                if pw not in seen:
                    seen.add(pw)
                    wallets.append(pw)
            if len(trades) < 1000:
                break
            await asyncio.sleep(0.5)

        logger.info(f"[global_trades] {len(wallets)} wallets from global feed")
        return wallets

    async def _discover_large_trades(self) -> list[str]:
        """Find wallets making $500+ trades -- quality filter."""
        wallets = []
        seen = set()

        for offset in range(0, 4000, 1000):
            trades = await self._api_get(
                f"{DATA_API}/trades",
                params={
                    "limit": "1000",
                    "offset": str(offset),
                    "filterType": "CASH",
                    "filterAmount": "500",
                },
            )
            if not trades or not isinstance(trades, list):
                break
            for pw in self._extract_wallets(trades):
                if pw not in seen:
                    seen.add(pw)
                    wallets.append(pw)
            if len(trades) < 1000:
                break
            await asyncio.sleep(0.5)

        logger.info(f"[large_trades] {len(wallets)} wallets making $500+ trades")
        return wallets

    async def _discover_holders(self) -> list[str]:
        """Fetch top holders from active markets -- different pool than traders."""
        await self._refresh_markets()
        wallets = []
        seen = set()

        markets = list(self._market_cache[:50])
        random.shuffle(markets)

        # Process in batches of 5 markets (API supports comma-separated)
        for i in range(0, min(len(markets), 40), 5):
            batch = markets[i:i+5]
            cids = ",".join(
                m.get("conditionId") or m.get("condition_id") or ""
                for m in batch if m.get("conditionId") or m.get("condition_id")
            )
            if not cids:
                continue
            data = await self._api_get(
                f"{DATA_API}/holders",
                params={"market": cids, "limit": "20"},
            )
            if data and isinstance(data, list):
                for token_data in data:
                    holders = token_data.get("holders", [])
                    for h in holders:
                        pw = (h.get("proxyWallet") or "").lower().strip()
                        if pw and pw not in seen:
                            seen.add(pw)
                            wallets.append(pw)
            await asyncio.sleep(0.5)

        logger.info(f"[holders] {len(wallets)} wallets from market holders")
        return wallets

    async def _discover_deep_pagination(self) -> list[str]:
        """Paginate deeper into trade history to find older active wallets."""
        wallets = []
        seen = set()

        # Random deep offsets (API caps at ~5000)
        offsets = random.sample(range(1000, 5000, 500), min(6, 8))

        for offset in offsets:
            trades = await self._api_get(
                f"{DATA_API}/trades",
                params={"limit": "1000", "offset": str(offset)},
            )
            if not trades or not isinstance(trades, list):
                continue
            for pw in self._extract_wallets(trades):
                if pw not in seen:
                    seen.add(pw)
                    wallets.append(pw)
            await asyncio.sleep(0.5)

        logger.info(f"[deep_pagination] {len(wallets)} wallets from deep offsets")
        return wallets

    async def _discover_wallets(self) -> tuple[list[str], str]:
        """Rotate between discovery sources."""
        source_name = SOURCE_NAMES[self._source_index % len(SOURCE_NAMES)]
        self._source_index += 1

        dispatch = {
            "market_trades": self._discover_market_trades,
            "global_trades": self._discover_global_trades,
            "large_trades": self._discover_large_trades,
            "holders": self._discover_holders,
            "deep_pagination": self._discover_deep_pagination,
        }

        fn = dispatch[source_name]
        wallets = await fn()
        return wallets, source_name

    # ---- Fast in-memory filtering ------------------------------------

    def _filter_known_fast(self, wallets: list[str]) -> list[str]:
        """Filter using in-memory sets -- no DB calls needed."""
        new = []
        for addr in wallets:
            a = addr.lower()
            if a in self._seen_wallets or a in self._tracked_wallets:
                continue
            new.append(a)
        return new

    # ---- Scoring System (100 points) ---------------------------------

    def _score_win_rate(self, win_rate: float) -> int:
        if win_rate >= 75: return 25
        if win_rate >= 70: return 20
        if win_rate >= 65: return 15
        if win_rate >= 60: return 10
        if win_rate >= 55: return 5
        return 0

    def _score_consistency(self, markets_traded: int) -> int:
        if markets_traded >= 200: return 20
        if markets_traded >= 100: return 15
        if markets_traded >= 50: return 10
        if markets_traded >= 20: return 5
        if markets_traded >= 10: return 2
        return 0

    def _score_roi(self, roi_pct: float) -> int:
        if roi_pct >= 50: return 15
        if roi_pct >= 25: return 10
        if roi_pct >= 10: return 5
        return 0

    def _score_lifetime_trades(self, trades: int) -> int:
        if trades >= 200: return 15
        if trades >= 100: return 10
        if trades >= 50: return 5
        if trades >= 10: return 2
        return 0

    def _score_portfolio_size(self, total_value: float) -> int:
        if total_value > 200_000: return 5
        if total_value > 50_000: return 10
        if total_value > 5_000: return 15
        if total_value > 500: return 5
        return 0

    def _score_underrated(self, total_invested: float, win_rate: float) -> int:
        if win_rate < 55: return 0
        if total_invested < 5_000 and win_rate >= 65: return 10
        if total_invested < 20_000 and win_rate >= 60: return 7
        if total_invested < 50_000 and win_rate >= 55: return 4
        return 0

    async def _evaluate_wallet(self, address: str) -> dict | None:
        """Fetch positions + trades for a wallet and compute scores."""
        positions = await self._api_get(
            f"{DATA_API}/positions",
            params={"user": address},
        )
        if positions is None:
            return None
        if not isinstance(positions, list):
            positions = []

        # Fetch trades (multiple pages)
        trades = []
        for offset in [0, 200, 400]:
            page = await self._api_get(
                f"{DATA_API}/trades",
                params={"user": address, "limit": "200", "offset": str(offset)},
            )
            if page and isinstance(page, list):
                trades.extend(page)
                if len(page) < 200:
                    break
            else:
                break
            await asyncio.sleep(0.3)

        # Extract username
        username = ""
        for t in trades:
            name = t.get("pseudonym") or t.get("name") or ""
            if name:
                username = name
                break

        # Calculate metrics
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

            pct_pnl = float(pos.get("percentPnl") or pos.get("percent_pnl") or 0)
            if pct_pnl > 0:
                wins += 1
            elif pct_pnl < 0:
                losses += 1

        markets_traded = len(markets_set)
        lifetime_trades = len(trades)

        resolved = wins + losses
        win_rate = (wins / resolved * 100) if resolved > 0 else 0
        roi_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        avg_trade_size = (total_invested / lifetime_trades) if lifetime_trades > 0 else 0

        # Compute scores
        s_wr = self._score_win_rate(win_rate)
        s_con = self._score_consistency(markets_traded)
        s_roi = self._score_roi(roi_pct)
        s_trades = self._score_lifetime_trades(lifetime_trades)
        s_port = self._score_portfolio_size(portfolio_value)
        s_under = self._score_underrated(total_invested, win_rate)

        total_score = s_wr + s_con + s_roi + s_trades + s_port + s_under

        # Hard gates
        if roi_pct < 0:
            total_score = 0
        if total_pnl < 5000:
            total_score = min(total_score, 30)

        breakdown = {
            "win_rate": {"points": s_wr, "max": 25, "value": round(win_rate, 1)},
            "consistency": {"points": s_con, "max": 20, "value": markets_traded},
            "roi": {"points": s_roi, "max": 15, "value": round(roi_pct, 1)},
            "lifetime_trades": {"points": s_trades, "max": 15, "value": lifetime_trades},
            "portfolio_size": {"points": s_port, "max": 15, "value": round(portfolio_value, 2)},
            "underrated": {"points": s_under, "max": 10, "value": round(total_invested, 2)},
        }

        return {
            "proxy_wallet": address,
            "username": username,
            "score": total_score,
            "total_positions": len(positions),
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

    # ---- Parallel evaluation -----------------------------------------

    async def _evaluate_batch(self, addresses: list[str]) -> list[dict]:
        """Evaluate multiple wallets in parallel."""
        tasks = [self._evaluate_wallet(addr) for addr in addresses]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid = []
        for r in results:
            if isinstance(r, dict) and r is not None:
                valid.append(r)
            elif isinstance(r, Exception):
                logger.debug(f"Eval error: {r}")
        return valid

    # ---- DB persistence ----------------------------------------------

    async def _save_candidate(self, data: dict, discovered_via: str):
        await self._ensure_db()
        await self._db.execute(
            """INSERT INTO scout_candidates
                (proxy_wallet, username, score, total_positions, wins, losses,
                 win_rate, total_pnl, total_invested, roi_pct, avg_trade_size,
                 markets_traded, lifetime_trades, discovered_via, discovered_market,
                 status, score_breakdown, last_scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, datetime('now'))
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
                data["lifetime_trades"], discovered_via,
                "pending" if data["score"] >= self._min_score else "rejected",
                data["score_breakdown"],
            ),
        )
        await self._db.commit()

    # ---- Main Loop ---------------------------------------------------

    async def start_loop(self):
        self._running = True
        logger.info("WalletScout v2 starting...")

        await self._ensure_client()
        await self._ensure_db()
        await self._load_settings()
        await self._load_seen_wallets()

        logger.info(f"Scout ready: {len(self._seen_wallets)} previously seen wallets in memory")

        while self._running:
            try:
                await self._run_cycle()
                self._cycle_count += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Scout cycle error: {e}")

            try:
                await self._load_settings()
            except Exception:
                pass

            await asyncio.sleep(self._cycle_delay)

        logger.info("WalletScout v2 stopped")

    async def _run_cycle(self):
        """One discovery + scoring cycle."""
        cycle_start = time.monotonic()

        # Discover wallets from rotating source
        raw_wallets, source_name = await self._discover_wallets()
        if not raw_wallets:
            logger.info(f"[{source_name}] No wallets discovered, skipping")
            return

        # Fast in-memory filter
        new_wallets = self._filter_known_fast(raw_wallets)
        self._last_cycle_found = len(raw_wallets)
        self._last_cycle_new = len(new_wallets)

        logger.info(f"Cycle {self._cycle_count} [{source_name}]: {len(raw_wallets)} found -> {len(new_wallets)} new")

        if not new_wallets:
            return

        # Evaluate in parallel batches
        to_eval = new_wallets[:self._max_per_cycle]
        queued = 0
        batch_size = self._parallel_evals

        for i in range(0, len(to_eval), batch_size):
            if not self._running:
                break

            batch = to_eval[i:i + batch_size]
            results = await self._evaluate_batch(batch)

            for result in results:
                self._total_scanned += 1
                addr = result["proxy_wallet"].lower()
                self._seen_wallets.add(addr)

                score = result["score"]
                await self._save_candidate(result, discovered_via=source_name)

                if score >= self._min_score:
                    queued += 1
                    logger.info(f"QUEUED: {result['username'] or addr[:12]}... score={score} wr={result['win_rate']}% pnl=${result['total_pnl']:,.0f}")

            # Mark unscorable wallets as seen too
            for addr in batch:
                self._seen_wallets.add(addr.lower())

            await asyncio.sleep(self._eval_delay)

        self._last_cycle_queued = queued
        elapsed = time.monotonic() - cycle_start
        logger.info(f"Cycle {self._cycle_count} done: {len(to_eval)} evaluated, {queued} queued in {elapsed:.1f}s (seen total: {len(self._seen_wallets)})")

    def stop_loop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "total_scanned": self._total_scanned,
            "seen_in_memory": len(self._seen_wallets),
            "tracked_wallets": len(self._tracked_wallets),
            "last_cycle_found": self._last_cycle_found,
            "last_cycle_new": self._last_cycle_new,
            "last_cycle_queued": self._last_cycle_queued,
        }
