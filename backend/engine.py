"""
CopyEngine — continuous parallel polling loop for copy trading.

All 100 wallets polled in parallel via asyncio.gather.
Persistent httpx.AsyncClient with connection pooling.
In-memory tx_hash set for O(1) dedup.
Ms-level delay tracking on every copy.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import aiosqlite
import httpx

from backend.scorer import score_trade_full

logger = logging.getLogger("polyedge.engine")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "polyedge.db")

FILTER_SETTING_KEYS = [
    "min_liquidity_usd", "entry_timing_minutes", "max_wallet_pool_pct",
    "min_volume_24h_usd", "min_unique_traders", "resolution_min_days",
    "resolution_max_days", "min_wallet_win_rate", "min_resolved_markets",
    "max_price_move_6h_pct", "min_confirming_wallets", "max_bankroll_exposure_pct",
]

POLY_FEE_RATE = 0.02
SLIPPAGE_RATE = 0.005


class CopyEngine:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

        # In-memory state
        self._wallet_trades: dict[str, set[str]] = {}  # address -> known tx_hashes
        self._wallet_positions: dict[tuple, dict] = {}  # (address, mode_id) -> {(cid,idx): pos}
        self._wallet_addresses: list[str] = []
        self._wallet_info: dict[str, dict] = {}  # address -> wallet row

        # Loop state
        self._loop_running: bool = False
        self._cycle_count: int = 0
        self._last_cycle_ms: float = 0.0
        self._worst_cycle_ms: float = 0.0
        self._trades_today: int = 0
        self._start_time: float = 0.0
        self.loop_task: asyncio.Task | None = None

        # Bankroll settings (loaded from DB)
        self._bankroll_usd: float = 1000.0
        self._max_bankroll_pct: float = 10.0  # max % of bankroll per single trade
        self._min_trade_usd: float = 5.0      # floor per trade
        self._max_trade_usd: float = 100.0     # ceiling per trade

        # Filter settings (loaded from DB)
        self._filter_settings: dict[str, float] = {}

        # Multi-mode strategy settings
        self._mode_settings: dict[str, dict] = {}      # mode_id -> {filter_key: value}
        self._mode_bankrolls: dict[str, float] = {}     # mode_id -> bankroll_usd
        self._mode_active: dict[str, bool] = {}         # mode_id -> is_active

        # HTTP client — persistent, with connection pooling
        self._client: httpx.AsyncClient | None = None
        self._db: aiosqlite.Connection | None = None

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=15,
                    max_keepalive_connections=10,
                ),
                timeout=httpx.Timeout(8.0, connect=5.0),
                http2=False,
            )

    async def _ensure_db(self):
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")

    async def _load_state(self):
        """Load wallets, known tx_hashes, and open positions into memory."""
        await self._ensure_db()

        # Load active wallets
        async with self._db.execute(
            "SELECT * FROM wallets WHERE is_active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            self._wallet_addresses = []
            self._wallet_info = {}
            for row in rows:
                addr = row["address"]
                self._wallet_addresses.append(addr)
                self._wallet_info[addr] = dict(row)
        logger.info(f"Loaded {len(self._wallet_addresses)} active wallets")

        # Load known tx_hashes from copy_feed into memory for O(1) dedup
        async with self._db.execute(
            "SELECT wallet_address, tx_hash FROM copy_feed"
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                addr = row["wallet_address"]
                if addr not in self._wallet_trades:
                    self._wallet_trades[addr] = set()
                self._wallet_trades[addr].add(row["tx_hash"])
        total_known = sum(len(v) for v in self._wallet_trades.values())
        logger.info(f"Loaded {total_known} known tx_hashes into memory")

        # Load open shadow positions (mode-aware)
        self._wallet_positions = {}
        async with self._db.execute(
            "SELECT * FROM shadow_positions WHERE status = 'open'"
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                addr = row["wallet_address"]
                mode_id = row["mode_id"] if "mode_id" in row.keys() else "strict"
                pos_key = (addr, mode_id)
                key = (row["condition_id"], row["outcome_index"])
                if pos_key not in self._wallet_positions:
                    self._wallet_positions[pos_key] = {}
                self._wallet_positions[pos_key][key] = dict(row)
        total_pos = sum(len(v) for v in self._wallet_positions.values())
        logger.info(f"Loaded {total_pos} open shadow positions")

        # Load bankroll settings from DB
        async with self._db.execute(
            "SELECT key, value FROM copy_trade_settings WHERE key IN ('bankroll_usd', 'max_bankroll_pct', 'min_trade_usd', 'max_trade_usd')"
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                if row["key"] == "bankroll_usd":
                    self._bankroll_usd = float(row["value"])
                elif row["key"] == "max_bankroll_pct":
                    self._max_bankroll_pct = float(row["value"])
                elif row["key"] == "min_trade_usd":
                    self._min_trade_usd = float(row["value"])
                elif row["key"] == "max_trade_usd":
                    self._max_trade_usd = float(row["value"])
        logger.info(
            f"Bankroll: ${self._bankroll_usd:.0f}, proportional sizing "
            f"(min ${self._min_trade_usd:.0f}, max ${self._max_trade_usd:.0f}, "
            f"cap {self._max_bankroll_pct}% of bankroll)"
        )

        # Load filter thresholds
        await self._load_filter_settings()

        # Load multi-mode settings
        await self._load_mode_settings()

    # ── Filter Settings ──────────────────────────────────────

    async def _load_filter_settings(self):
        """Load filter threshold settings from DB."""
        await self._ensure_db()
        placeholders = ",".join("?" for _ in FILTER_SETTING_KEYS)
        async with self._db.execute(
            f"SELECT key, value FROM copy_trade_settings WHERE key IN ({placeholders})",
            tuple(FILTER_SETTING_KEYS),
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                try:
                    self._filter_settings[row["key"]] = float(row["value"])
                except (ValueError, TypeError):
                    pass
        logger.info(f"Loaded {len(self._filter_settings)} filter settings")

    async def _load_mode_settings(self):
        """Load multi-mode strategy settings from DB."""
        await self._ensure_db()
        self._mode_settings = {}
        self._mode_bankrolls = {}
        self._mode_active = {}

        # Load mode definitions
        try:
            async with self._db.execute(
                "SELECT mode_id, bankroll_usd, is_active FROM strategy_modes"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    mid = row["mode_id"]
                    self._mode_bankrolls[mid] = float(row["bankroll_usd"])
                    self._mode_active[mid] = bool(row["is_active"])
                    self._mode_settings[mid] = {}
        except Exception as e:
            logger.warning(f"Failed to load strategy_modes (table may not exist yet): {e}")
            # Fallback: single strict mode using global settings
            self._mode_settings = {"strict": dict(self._filter_settings)}
            self._mode_bankrolls = {"strict": self._bankroll_usd}
            self._mode_active = {"strict": True}
            return

        # Load per-mode filter settings
        try:
            async with self._db.execute(
                "SELECT mode_id, key, value FROM mode_filter_settings"
            ) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    mid = row["mode_id"]
                    if mid in self._mode_settings:
                        try:
                            self._mode_settings[mid][row["key"]] = float(row["value"])
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            logger.warning(f"Failed to load mode_filter_settings: {e}")

        logger.info(
            f"Loaded {len(self._mode_settings)} strategy modes: "
            + ", ".join(f"{mid}({'on' if self._mode_active.get(mid) else 'off'})" for mid in self._mode_settings)
        )

    async def reload_settings(self):
        """Reload all settings from DB (called after settings update)."""
        await self._load_state()
        await self._load_filter_settings()
        await self._load_mode_settings()
        logger.info("Engine settings reloaded from DB")

    # ── Filter API Helpers ────────────────────────────────────

    async def _fetch_market_data(self, condition_id: str) -> dict | None:
        """Fetch market data from Gamma API by condition_id."""
        await self._ensure_client()
        try:
            resp = await self._client.get(
                f"{GAMMA_API}/markets",
                params={"condition_ids": condition_id, "limit": 1},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0]
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"_fetch_market_data failed for {condition_id[:12]}...: {e}")
            return None

    async def _fetch_market_holders(self, condition_id: str) -> list[dict]:
        """Fetch holder/trader data from Data API."""
        await self._ensure_client()
        try:
            # Try /trades endpoint to count unique traders
            resp = await self._client.get(
                f"{DATA_API}/trades",
                params={"market": condition_id, "limit": 500},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"_fetch_market_holders failed: {e}")
            return []

    async def _fetch_price_history(self, token_id: str) -> list[dict]:
        """Fetch 6h price history from CLOB API."""
        await self._ensure_client()
        try:
            resp = await self._client.get(
                f"{CLOB_API}/prices-history",
                params={"market": token_id, "interval": "6h", "fidelity": 60},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if isinstance(data, dict) and "history" in data:
                return data["history"]
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"_fetch_price_history failed: {e}")
            return []

    async def _fetch_wallet_position_in_market(self, wallet_address: str, condition_id: str) -> float:
        """Fetch wallet's position size in a specific market from Data API."""
        await self._ensure_client()
        try:
            resp = await self._client.get(
                f"{DATA_API}/positions",
                params={"user": wallet_address},
            )
            if resp.status_code != 200:
                return 0.0
            positions = resp.json()
            if not isinstance(positions, list):
                return 0.0
            for pos in positions:
                if pos.get("conditionId") == condition_id or pos.get("condition_id") == condition_id:
                    return float(pos.get("size", 0) or pos.get("currentValue", 0) or 0)
            return 0.0
        except Exception as e:
            logger.debug(f"_fetch_wallet_position_in_market failed: {e}")
            return 0.0

    # ── Filter Evaluation ────────────────────────────────────

    async def _evaluate_filters(self, address: str, trade: dict, market_data: dict | None,
                                filter_settings: dict | None = None, bankroll: float | None = None) -> list[dict]:
        """Run all 10 filters. Returns list of filter result dicts."""
        results = []
        fs = filter_settings if filter_settings is not None else self._filter_settings
        bankroll_for_eval = bankroll if bankroll is not None else self._bankroll_usd
        condition_id = trade.get("conditionId", "")

        # F1 — Min Liquidity
        f1 = {"filter_name": "min_liquidity", "status": "pending", "threshold_value": str(fs.get("min_liquidity_usd", 75000))}
        if market_data:
            liquidity = float(market_data.get("liquidityNum", 0) or market_data.get("liquidity", 0) or 0)
            threshold = fs.get("min_liquidity_usd", 75000)
            f1["actual_value"] = str(round(liquidity, 2))
            if liquidity >= threshold:
                f1["status"] = "passed"
            else:
                f1["status"] = "failed"
                f1["fail_message"] = f"Liquidity ${liquidity:,.0f} below ${threshold:,.0f} minimum"
        results.append(f1)

        # F2 — Entry Timing Window
        f2 = {"filter_name": "entry_timing", "status": "pending", "threshold_value": str(fs.get("entry_timing_minutes", 120))}
        their_ts_ms = self._parse_timestamp_ms(trade)
        if their_ts_ms:
            now_ms = int(time.time() * 1000)
            minutes_since = (now_ms - their_ts_ms) / 60000.0
            threshold = fs.get("entry_timing_minutes", 120)
            f2["actual_value"] = str(round(minutes_since, 1))
            if minutes_since <= threshold:
                f2["status"] = "passed"
            else:
                f2["status"] = "failed"
                f2["fail_message"] = f"Trade is {minutes_since:.0f}min old, max {threshold:.0f}min"
        results.append(f2)

        # F3 — Wallet Concentration
        f3 = {"filter_name": "wallet_concentration", "status": "pending", "threshold_value": str(fs.get("max_wallet_pool_pct", 10.0))}
        if market_data and condition_id:
            try:
                position_value = await self._fetch_wallet_position_in_market(address, condition_id)
                liquidity = float(market_data.get("liquidityNum", 0) or market_data.get("liquidity", 0) or 0)
                if liquidity > 0:
                    concentration_pct = (position_value / liquidity) * 100
                    threshold = fs.get("max_wallet_pool_pct", 10.0)
                    f3["actual_value"] = str(round(concentration_pct, 2))
                    if concentration_pct <= threshold:
                        f3["status"] = "passed"
                    else:
                        f3["status"] = "failed"
                        f3["fail_message"] = f"Wallet controls {concentration_pct:.1f}% of pool, max {threshold:.1f}%"
                else:
                    f3["actual_value"] = "0"
                    f3["status"] = "passed"
            except Exception:
                pass
        results.append(f3)

        # F4 — Min 24H Volume
        f4 = {"filter_name": "min_volume_24h", "status": "pending", "threshold_value": str(fs.get("min_volume_24h_usd", 25000))}
        if market_data:
            volume = float(market_data.get("volume24hr", 0) or market_data.get("volume24hrClob", 0) or 0)
            threshold = fs.get("min_volume_24h_usd", 25000)
            f4["actual_value"] = str(round(volume, 2))
            if volume >= threshold:
                f4["status"] = "passed"
            else:
                f4["status"] = "failed"
                f4["fail_message"] = f"24h volume ${volume:,.0f} below ${threshold:,.0f} minimum"
        results.append(f4)

        # F5 — Min Unique Traders
        f5 = {"filter_name": "min_unique_traders", "status": "pending", "threshold_value": str(fs.get("min_unique_traders", 50))}
        if condition_id:
            try:
                trades_data = await self._fetch_market_holders(condition_id)
                if trades_data:
                    unique_addrs = set()
                    for t in trades_data:
                        user = t.get("proxyWallet") or t.get("user") or t.get("taker") or t.get("maker")
                        if user:
                            unique_addrs.add(user.lower())
                    trader_count = len(unique_addrs)
                    threshold = fs.get("min_unique_traders", 50)
                    f5["actual_value"] = str(trader_count)
                    if trader_count >= threshold:
                        f5["status"] = "passed"
                    else:
                        f5["status"] = "failed"
                        f5["fail_message"] = f"Only {trader_count} unique traders, need {threshold:.0f}"
            except Exception:
                pass
        results.append(f5)

        # F6 — Resolution Date Window [DISABLED]
        f6 = {"filter_name": "resolution_window", "status": "passed",
              "threshold_value": "disabled", "actual_value": "disabled", "fail_message": None}
        results.append(f6)

        # F7 — Wallet Win Rate & Track Record (local data only)
        f7 = {"filter_name": "wallet_win_rate", "status": "pending",
              "threshold_value": f"{fs.get('min_wallet_win_rate', 55.0)}%/{fs.get('min_resolved_markets', 20)}m"}
        wallet_info = self._wallet_info.get(address, {})
        csv_win_rate = float(wallet_info.get("csv_win_rate", 0) or 0)
        csv_unique_markets = int(wallet_info.get("csv_unique_markets", 0) or 0)
        min_wr = fs.get("min_wallet_win_rate", 55.0)
        min_markets = fs.get("min_resolved_markets", 20)
        f7["actual_value"] = f"{csv_win_rate:.1f}%/{csv_unique_markets}m"
        if csv_win_rate >= min_wr and csv_unique_markets >= min_markets:
            f7["status"] = "passed"
        else:
            f7["status"] = "failed"
            parts = []
            if csv_win_rate < min_wr:
                parts.append(f"Win rate {csv_win_rate:.1f}% < {min_wr:.1f}%")
            if csv_unique_markets < min_markets:
                parts.append(f"Markets {csv_unique_markets} < {min_markets:.0f}")
            f7["fail_message"] = "; ".join(parts)
        results.append(f7)

        # F8 — Recent Price Movement Cap [DISABLED]
        f8 = {"filter_name": "price_movement_6h", "status": "passed",
              "threshold_value": "disabled", "actual_value": "disabled", "fail_message": None}
        results.append(f8)

        # F9 — Multi-Wallet Confirmation [DISABLED]
        f9 = {"filter_name": "multi_wallet_confirm", "status": "passed",
              "threshold_value": "disabled", "actual_value": "disabled", "fail_message": None}
        results.append(f9)

        # F10 — Max Bankroll Exposure (local check)
        f10 = {"filter_name": "bankroll_exposure", "status": "pending",
               "threshold_value": str(fs.get("max_bankroll_exposure_pct", 5.0))}
        their_size = float(trade.get("size", 0) or 0)
        their_volume = float(self._wallet_info.get(address, {}).get("csv_volume", 0) or 0)
        bankroll = bankroll_for_eval
        if their_volume > 0 and their_size > 0:
            proposed = bankroll * (their_size / their_volume)
        else:
            proposed = bankroll * 0.01
        max_allowed_sizing = bankroll * (self._max_bankroll_pct / 100.0)
        proposed = max(self._min_trade_usd, min(proposed, self._max_trade_usd, max_allowed_sizing))
        exposure_pct = (proposed / bankroll) * 100 if bankroll > 0 else 0
        threshold = fs.get("max_bankroll_exposure_pct", 5.0)
        f10["actual_value"] = str(round(exposure_pct, 2))
        if exposure_pct <= threshold:
            f10["status"] = "passed"
        else:
            f10["status"] = "failed"
            f10["fail_message"] = f"Exposure {exposure_pct:.1f}% exceeds {threshold:.1f}% cap"
        results.append(f10)

        return results

    async def _store_filter_results(self, tx_hash: str, copy_feed_id: int | None, results: list[dict],
                                     mode_id: str = "strict"):
        """Store filter results in DB and update copy_feed verdict."""
        await self._ensure_db()

        for r in results:
            try:
                await self._db.execute(
                    """INSERT OR REPLACE INTO filter_results
                    (copy_feed_id, tx_hash, filter_name, status, threshold_value, actual_value, fail_message, mode_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (copy_feed_id, tx_hash, r["filter_name"], r["status"],
                     r.get("threshold_value"), r.get("actual_value"), r.get("fail_message"), mode_id),
                )
            except Exception as e:
                logger.error(f"Failed to store filter result: {e}")

        # Compute aggregate verdict
        statuses = [r["status"] for r in results]
        if any(s == "failed" for s in statuses):
            verdict = "failed"
        elif any(s == "pending" for s in statuses):
            verdict = "pending"
        else:
            verdict = "passed"

        if copy_feed_id:
            try:
                await self._db.execute(
                    "UPDATE copy_feed SET filter_verdict = ? WHERE id = ?",
                    (verdict, copy_feed_id),
                )
            except Exception as e:
                logger.error(f"Failed to update filter_verdict: {e}")

        await self._db.commit()
        return verdict

    # ── Priming ───────────────────────────────────────────────

    async def _prime_known_trades(self):
        """Fetch current trades for all wallets and mark as known.
        This prevents old/historical trades from being treated as new.
        Only trades that appear AFTER this priming pass will be copied."""
        logger.info("PRIMING: fetching existing trades to build dedup set...")
        BATCH_SIZE = 10
        BATCH_DELAY = 0.5
        total_primed = 0

        for i in range(0, len(self._wallet_addresses), BATCH_SIZE):
            batch = self._wallet_addresses[i : i + BATCH_SIZE]
            tasks = [self._fetch_recent_trades(addr, limit=20) for addr in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for j, result in enumerate(results):
                addr = batch[j]
                if isinstance(result, list):
                    if addr not in self._wallet_trades:
                        self._wallet_trades[addr] = set()
                    for trade in result:
                        tx_hash = trade.get("transactionHash")
                        if tx_hash:
                            self._wallet_trades[addr].add(tx_hash)
                            total_primed += 1

            if i + BATCH_SIZE < len(self._wallet_addresses):
                await asyncio.sleep(BATCH_DELAY)

        logger.info(f"PRIMING DONE: {total_primed} existing trades marked as known")

    # ── Main Loop ─────────────────────────────────────────────

    async def start_loop(self):
        """Start the continuous polling loop. Runs forever."""
        logger.info("Starting copy engine loop...")
        await self._ensure_client()
        await self._ensure_db()
        await self._load_state()

        # If copy_feed is empty (fresh start), prime the dedup set
        # so historical trades are ignored
        async with self._db.execute("SELECT COUNT(*) FROM copy_feed") as cursor:
            row = await cursor.fetchone()
            feed_count = row[0] if row else 0
        if feed_count == 0 and not any(self._wallet_trades.values()):
            await self._prime_known_trades()

        self._loop_running = True
        self._start_time = time.monotonic()
        self._cycle_count = 0
        self._worst_cycle_ms = 0.0

        while self._loop_running:
            try:
                cycle_start = time.monotonic()
                new_trades, failed = await self._run_cycle()
                cycle_ms = (time.monotonic() - cycle_start) * 1000
                self._last_cycle_ms = cycle_ms
                self._worst_cycle_ms = max(self._worst_cycle_ms, cycle_ms)
                self._cycle_count += 1

                if cycle_ms > 3000:
                    logger.warning(f"SLOW CYCLE #{self._cycle_count}: {cycle_ms:.0f}ms")

                # Log cycle health every 100 cycles
                if self._cycle_count % 100 == 0:
                    try:
                        await self._db.execute(
                            """INSERT INTO loop_log
                            (cycle_number, cycle_ms, wallets_polled, wallets_failed,
                             new_trades_detected, copies_executed)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                self._cycle_count,
                                cycle_ms,
                                len(self._wallet_addresses),
                                failed,
                                new_trades,
                                new_trades,
                            ),
                        )
                        await self._db.commit()
                    except Exception as e:
                        logger.error(f"Failed to log cycle: {e}")

            except Exception as e:
                logger.exception(f"Cycle error (continuing): {e}")

            # Pause between cycles — 2s keeps us well under rate limits
            await asyncio.sleep(2.0)

    def stop_loop(self):
        self._loop_running = False
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()

    async def close(self):
        self._loop_running = False
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._db:
            await self._db.close()
            self._db = None

    # ── Cycle ─────────────────────────────────────────────────

    async def _run_cycle(self) -> tuple[int, int]:
        """Fetch wallets in batches to avoid 429 rate limits."""
        BATCH_SIZE = 10
        BATCH_DELAY = 0.5  # 500ms between batches
        new_trades = 0
        failed = 0

        for i in range(0, len(self._wallet_addresses), BATCH_SIZE):
            batch = self._wallet_addresses[i : i + BATCH_SIZE]
            tasks = [self._poll_wallet(addr) for addr in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for j, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Wallet {batch[j][:10]}...: {result}")
                    failed += 1
                elif isinstance(result, int):
                    new_trades += result

            # Brief pause between batches to stay under rate limits
            if i + BATCH_SIZE < len(self._wallet_addresses):
                await asyncio.sleep(BATCH_DELAY)

        return new_trades, failed

    async def _poll_wallet(self, address: str) -> int:
        """Fetch latest trades for one wallet, diff against known state, run filters for each mode."""
        trades = await self._fetch_recent_trades(address, limit=20)
        known = self._wallet_trades.get(address, set())
        new_count = 0

        for trade in trades:
            tx_hash = trade.get("transactionHash")
            if not tx_hash or tx_hash in known:
                continue

            detection_time_ms = int(time.time() * 1000)
            their_timestamp_ms = self._parse_timestamp_ms(trade)
            delay_ms = (
                detection_time_ms - their_timestamp_ms
                if their_timestamp_ms
                else None
            )

            # Fetch market data ONCE (shared across all modes)
            condition_id = trade.get("conditionId", "")
            market_data = None
            if condition_id:
                market_data = await self._fetch_market_data(condition_id)

            # Evaluate and execute for EACH active mode
            for mode_id, mode_settings in self._mode_settings.items():
                if not self._mode_active.get(mode_id, False):
                    continue

                mode_bankroll = self._mode_bankrolls.get(mode_id, self._bankroll_usd)

                # Run filters with this mode's settings
                mode_filter_results = await self._evaluate_filters(
                    address, trade, market_data, mode_settings, mode_bankroll
                )

                # Execute copy with mode_id
                copy_feed_id = await self._execute_copy(
                    address, trade, delay_ms, mode_filter_results, mode_id=mode_id
                )

                # Store filter results with mode_id
                if copy_feed_id:
                    verdict = await self._store_filter_results(
                        tx_hash, copy_feed_id, mode_filter_results, mode_id=mode_id
                    )
                    logger.info(f"Filters [{mode_id}]: {verdict} for {tx_hash[:12]}...")

            known.add(tx_hash)
            new_count += 1

        self._wallet_trades[address] = known
        return new_count

    # ── API Fetching ──────────────────────────────────────────

    async def _fetch_recent_trades(self, address: str, limit: int = 20) -> list[dict]:
        await self._ensure_client()
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.get(
                    f"{DATA_API}/trades",
                    params={"user": address, "limit": limit},
                )
                if resp.status_code == 429:
                    # Rate limited — back off and retry
                    backoff = 1.0 * (2 ** attempt)
                    logger.warning(f"429 for {address[:10]}... backing off {backoff}s")
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else []
            except httpx.TimeoutException:
                return []
            except Exception as e:
                if attempt < max_retries:
                    await asyncio.sleep(0.5)
                    continue
                logger.debug(f"Fetch failed for {address[:10]}...: {e}")
                return []
        return []

    def _parse_timestamp_ms(self, trade: dict) -> int | None:
        ts = trade.get("timestamp")
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return int(ts * 1000) if ts < 1e12 else int(ts)
        if isinstance(ts, str):
            try:
                if ts.isdigit():
                    val = int(ts)
                    return val * 1000 if val < 1e12 else val
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except (ValueError, OSError):
                pass
        return None

    # ── Copy Execution ────────────────────────────────────────

    def _has_open_position(self, wallet_id: int, condition_id: str, outcome_index: int,
                            mode_id: str = "strict") -> bool:
        for addr, info in self._wallet_info.items():
            if info["id"] == wallet_id:
                positions = self._wallet_positions.get((addr, mode_id), {})
                return (condition_id, outcome_index) in positions
        return False

    async def _execute_copy(self, their_address: str, trade: dict, delay_ms: int | None,
                            filter_results: list[dict] | None = None,
                            mode_id: str = "strict") -> int | None:
        """Mirror a detected trade. Returns copy_feed_id or None."""
        side = trade.get("side", "").upper()
        tx_hash = trade["transactionHash"]
        condition_id = trade.get("conditionId", "")
        outcome_index = int(trade.get("outcomeIndex", 0))
        their_price = float(trade.get("price", 0) or 0)
        their_size = float(trade.get("size", 0) or 0)
        market_slug = trade.get("slug", "")
        market_title = trade.get("title", "")
        direction = "YES" if outcome_index == 0 else "NO"

        wallet_info = self._wallet_info.get(their_address, {})
        wallet_id = wallet_info.get("id", 0)
        wallet_username = wallet_info.get("username", "")

        # Compute filter verdict
        filter_verdict = "skipped"
        if filter_results:
            statuses = [r["status"] for r in filter_results]
            if any(s == "failed" for s in statuses):
                filter_verdict = "failed"
            elif any(s == "pending" for s in statuses):
                filter_verdict = "pending"
            else:
                filter_verdict = "passed"

        # Extract metrics from filter results for scoring
        score_data = None
        if filter_results and filter_verdict == "passed":
            metrics = {}
            for fr in filter_results:
                name = fr.get("filter_name", "")
                val = fr.get("actual_value")
                if val and val != "disabled":
                    try:
                        if name == "min_liquidity":
                            metrics["liquidity_usd"] = float(val)
                        elif name == "entry_timing":
                            metrics["minutes_since_open"] = float(val)
                        elif name == "wallet_concentration":
                            metrics["wallet_share_pct"] = float(val)
                        elif name == "min_volume_24h":
                            metrics["volume_24h_usd"] = float(val)
                        elif name == "min_unique_traders":
                            metrics["unique_traders"] = int(float(val))
                        elif name == "wallet_win_rate":
                            parts = val.split("/")
                            metrics["win_rate_pct"] = float(parts[0].replace("%", ""))
                            metrics["resolved_markets"] = int(parts[1].replace("m", ""))
                        elif name == "bankroll_exposure":
                            metrics["exposure_pct"] = float(val)
                    except (ValueError, IndexError):
                        pass

            # Compute scores if we have enough data
            if len(metrics) >= 5:
                score_data = score_trade_full(
                    liquidity_usd=metrics.get("liquidity_usd", 0),
                    minutes_since_open=metrics.get("minutes_since_open", 0),
                    wallet_share_pct=metrics.get("wallet_share_pct", 0),
                    volume_24h_usd=metrics.get("volume_24h_usd", 0),
                    unique_traders=metrics.get("unique_traders", 0),
                    win_rate_pct=metrics.get("win_rate_pct", 0),
                    resolved_markets=metrics.get("resolved_markets", 0),
                    exposure_pct=metrics.get("exposure_pct", 0),
                )

        # Proportional sizing: mirror their trade size relative to their portfolio
        bankroll = self._mode_bankrolls.get(mode_id, self._bankroll_usd)
        their_volume = float(wallet_info.get("csv_volume", 0) or 0)

        if their_volume > 0 and their_size > 0:
            their_pct = their_size / their_volume
            our_size = bankroll * their_pct
        else:
            our_size = bankroll * 0.01

        # Clamp to min/max bounds
        max_allowed = bankroll * (self._max_bankroll_pct / 100.0)
        # Also apply bankroll exposure hard cap (Filter 10)
        mode_fs = self._mode_settings.get(mode_id, self._filter_settings)
        max_exposure = bankroll * (mode_fs.get("max_bankroll_exposure_pct", 5.0) / 100.0)
        our_size = max(self._min_trade_usd, min(our_size, self._max_trade_usd, max_allowed, max_exposure))
        our_size = round(our_size, 2)

        our_price = their_price
        slippage_cost = our_size * SLIPPAGE_RATE
        poly_fee = our_size * POLY_FEE_RATE
        our_shares = our_size / our_price if our_price > 0 else 0
        execution_time_ms = int(time.time() * 1000)

        realized_pnl = None
        net_pnl = None
        pos_key = (condition_id, outcome_index)

        # Only execute shadow position changes if filters passed
        filters_ok = (filter_verdict == "passed")

        if side == "BUY":
            has_pos = self._has_open_position(wallet_id, condition_id, outcome_index, mode_id)
            event_type = "ADD" if has_pos else "OPEN"

            if not has_pos and filters_ok:
                try:
                    await self._db.execute(
                        """INSERT OR REPLACE INTO shadow_positions
                        (wallet_id, wallet_address, wallet_username, condition_id,
                         outcome_index, market_slug, market_title, direction,
                         their_entry_price, their_current_size, our_entry_price,
                         our_size_usdc, our_shares, current_price, poly_fee,
                         slippage, entry_delay_ms, status, score_trade, trade_tier, mode_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                        (
                            wallet_id, their_address, wallet_username, condition_id,
                            outcome_index, market_slug, market_title, direction,
                            their_price, their_size, our_price,
                            our_size, our_shares, our_price, poly_fee,
                            slippage_cost, delay_ms,
                            score_data["score_trade"] if score_data else None,
                            score_data["trade_tier"] if score_data else None,
                            mode_id,
                        ),
                    )
                    await self._db.commit()
                except Exception as e:
                    logger.error(f"Failed to insert shadow position: {e}")

                addr_positions = self._wallet_positions.setdefault((their_address, mode_id), {})
                addr_positions[pos_key] = {
                    "wallet_id": wallet_id,
                    "condition_id": condition_id,
                    "outcome_index": outcome_index,
                    "our_entry_price": our_price,
                    "our_size_usdc": our_size,
                    "our_shares": our_shares,
                }
            elif has_pos and filters_ok:
                try:
                    await self._db.execute(
                        """UPDATE shadow_positions
                        SET our_size_usdc = our_size_usdc + ?,
                            our_shares = our_shares + ?,
                            their_current_size = their_current_size + ?,
                            poly_fee = poly_fee + ?,
                            slippage = slippage + ?
                        WHERE wallet_id = ? AND condition_id = ? AND outcome_index = ?
                              AND status = 'open' AND mode_id = ?""",
                        (our_size, our_shares, their_size, poly_fee, slippage_cost,
                         wallet_id, condition_id, outcome_index, mode_id),
                    )
                    await self._db.commit()
                except Exception as e:
                    logger.error(f"Failed to update shadow position: {e}")

            if not filters_ok:
                event_type = f"FILTERED_{event_type}"

        elif side == "SELL":
            event_type = "CLOSE"
            pos_data = self._wallet_positions.get((their_address, mode_id), {}).get(pos_key)
            if pos_data:
                entry_price = pos_data.get("our_entry_price", 0)
                shares = pos_data.get("our_shares", 0)
                realized_pnl = (our_price - entry_price) * shares if shares else 0
                net_pnl = realized_pnl - poly_fee - slippage_cost

                try:
                    await self._db.execute(
                        """UPDATE shadow_positions
                        SET status = 'closed', exit_price = ?, closed_at = datetime('now'),
                            gross_pnl = ?, net_pnl = ?, exit_delay_ms = ?
                        WHERE wallet_id = ? AND condition_id = ? AND outcome_index = ?
                              AND status = 'open' AND mode_id = ?""",
                        (our_price, realized_pnl, net_pnl, delay_ms,
                         wallet_id, condition_id, outcome_index, mode_id),
                    )
                    await self._db.commit()
                except Exception as e:
                    logger.error(f"Failed to close shadow position: {e}")

                self._wallet_positions.get((their_address, mode_id), {}).pop(pos_key, None)
        else:
            event_type = "UNKNOWN"

        # Log paired trade to copy_feed
        copy_feed_id = None
        try:
            cursor = await self._db.execute(
                """INSERT OR IGNORE INTO copy_feed
                (wallet_id, wallet_address, wallet_username, tx_hash, event_type,
                 side, direction, condition_id, market_slug, market_title,
                 their_price, their_size, their_timestamp_ms,
                 our_price, our_size, our_shares, poly_fee, slippage,
                 delay_ms, realized_pnl, net_pnl, executed_at_ms, filter_verdict,
                 score_trade, score_wallet, trade_tier, score_breakdown, mode_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    wallet_id, their_address, wallet_username, tx_hash, event_type,
                    side, direction, condition_id, market_slug, market_title,
                    their_price, their_size, self._parse_timestamp_ms(trade),
                    our_price, our_size, our_shares, poly_fee, slippage_cost,
                    delay_ms, realized_pnl, net_pnl, execution_time_ms, filter_verdict,
                    score_data["score_trade"] if score_data else None,
                    score_data["score_wallet"] if score_data else None,
                    score_data["trade_tier"] if score_data else None,
                    json.dumps(score_data) if score_data else None,
                    mode_id,
                ),
            )
            await self._db.commit()
            copy_feed_id = cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to log copy_feed: {e}")

        self._trades_today += 1
        logger.info(
            f"COPY [{mode_id}] {event_type} | {wallet_username or their_address[:10]} | "
            f"{side} {direction} | them ${their_size:.0f} -> us ${our_size:.2f} @{our_price:.3f} | "
            f"delay={delay_ms}ms | filters={filter_verdict} | "
            f"score_t={score_data['score_trade'] if score_data else '?'} tier={score_data['trade_tier'] if score_data else '?'}"
        )
        return copy_feed_id

    # ── Price Refresh ─────────────────────────────────────────

    async def refresh_prices(self) -> int:
        """Refresh current_price on all open shadow positions via Gamma API."""
        await self._ensure_client()
        await self._ensure_db()

        async with self._db.execute(
            "SELECT DISTINCT market_slug FROM shadow_positions WHERE status = 'open' AND market_slug IS NOT NULL"
        ) as cursor:
            slugs = [row["market_slug"] for row in await cursor.fetchall()]

        if not slugs:
            return 0

        updated = 0
        for slug in slugs:
            try:
                resp = await self._client.get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug, "limit": 1},
                )
                resp.raise_for_status()
                markets = resp.json()
                if not markets:
                    continue

                market = markets[0] if isinstance(markets, list) else markets
                outcome_prices = market.get("outcomePrices")
                if not outcome_prices:
                    continue

                if isinstance(outcome_prices, str):
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices

                for idx, price in enumerate(prices):
                    price_f = float(price)
                    await self._db.execute(
                        """UPDATE shadow_positions
                        SET current_price = ?,
                            gross_pnl = (? - our_entry_price) * our_shares,
                            net_pnl = (? - our_entry_price) * our_shares - poly_fee - slippage
                        WHERE market_slug = ? AND outcome_index = ? AND status = 'open'""",
                        (price_f, price_f, price_f, slug, idx),
                    )
                    updated += 1

                await self._db.commit()
            except Exception as e:
                logger.debug(f"Price refresh failed for {slug}: {e}")

        logger.info(f"Price refresh: updated {updated} position groups")
        return updated

    # ── Status ────────────────────────────────────────────────

    def get_status(self) -> dict:
        uptime = time.monotonic() - self._start_time if self._start_time else 0
        return {
            "running": self._loop_running,
            "cycle_count": self._cycle_count,
            "last_cycle_ms": round(self._last_cycle_ms, 1),
            "worst_cycle_ms": round(self._worst_cycle_ms, 1),
            "trades_today": self._trades_today,
            "uptime_seconds": round(uptime, 0),
            "wallets_loaded": len(self._wallet_addresses),
        }
