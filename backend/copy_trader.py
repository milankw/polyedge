"""
CopyTradeMonitor — Independent copy trading module that monitors wallets and applies safety filters.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import aiosqlite
import httpx

logger = logging.getLogger("polyedge.copytrade")

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "polyedge.db")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
RATE_LIMIT_DELAY = 0.15  # 150ms between API calls


class CopyTradeMonitor:
    """Independent copy trading module that monitors wallets and applies filters."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._client: httpx.AsyncClient | None = None
        self._last_poll: str | None = None
        self._is_running: bool = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _db(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        return db

    # ── Settings ──────────────────────────────────────────────

    async def get_settings(self) -> dict:
        """Load all settings from copy_trade_settings table."""
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                "SELECT key, value, description FROM copy_trade_settings"
            )
            return {row["key"]: {"value": row["value"], "description": row["description"]} for row in rows}
        finally:
            await db.close()

    async def update_setting(self, key: str, value: str):
        """Update a single setting."""
        db = await self._db()
        try:
            await db.execute(
                "UPDATE copy_trade_settings SET value = ? WHERE key = ?",
                (value, key),
            )
            await db.commit()
        finally:
            await db.close()

    async def update_settings(self, updates: dict):
        """Update multiple settings at once."""
        db = await self._db()
        try:
            for key, value in updates.items():
                await db.execute(
                    "UPDATE copy_trade_settings SET value = ? WHERE key = ?",
                    (str(value), key),
                )
            await db.commit()
        finally:
            await db.close()

    async def _get_setting_value(self, db: aiosqlite.Connection, key: str) -> str | None:
        rows = await db.execute_fetchall(
            "SELECT value FROM copy_trade_settings WHERE key = ?", (key,)
        )
        if rows:
            return rows[0]["value"]
        return None

    # ── Signals ───────────────────────────────────────────────

    async def get_signals(self, days: int = 7) -> list:
        """Get signals from the last N days."""
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                """SELECT * FROM copy_trade_signals
                WHERE detected_at >= datetime('now', ?)
                ORDER BY detected_at DESC""",
                (f"-{days} days",),
            )
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_signal_detail(self, signal_id: int) -> dict | None:
        """Get full detail for one signal including all filter results."""
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                "SELECT * FROM copy_trade_signals WHERE id = ?", (signal_id,)
            )
            if not rows:
                return None
            signal = dict(rows[0])
            if signal.get("filter_results"):
                try:
                    signal["filter_results"] = json.loads(signal["filter_results"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return signal
        finally:
            await db.close()

    # ── Status ────────────────────────────────────────────────

    async def get_status(self) -> dict:
        """Return current module status."""
        db = await self._db()
        try:
            active_row = await db.execute_fetchall(
                "SELECT COUNT(*) as cnt FROM wallets WHERE is_active = 1"
            )
            active_wallets = active_row[0]["cnt"] if active_row else 0

            signal_row = await db.execute_fetchall(
                "SELECT COUNT(*) as cnt FROM copy_trade_signals WHERE detected_at >= datetime('now', '-24 hours')"
            )
            signals_24h = signal_row[0]["cnt"] if signal_row else 0

            passed_row = await db.execute_fetchall(
                "SELECT COUNT(*) as cnt FROM copy_trade_signals WHERE all_filters_passed = 1 AND detected_at >= datetime('now', '-24 hours')"
            )
            passed_24h = passed_row[0]["cnt"] if passed_row else 0

            cache_row = await db.execute_fetchall(
                "SELECT COUNT(*) as cnt FROM wallet_position_cache"
            )
            cached_positions = cache_row[0]["cnt"] if cache_row else 0

            settings = await self.get_settings()
            execution_mode = settings.get("execution_mode", {}).get("value", "MANUAL")
            poll_interval = settings.get("poll_interval_minutes", {}).get("value", "5")

            return {
                "is_running": self._is_running,
                "last_poll": self._last_poll,
                "active_wallets": active_wallets,
                "signals_24h": signals_24h,
                "passed_24h": passed_24h,
                "cached_positions": cached_positions,
                "execution_mode": execution_mode,
                "poll_interval_minutes": int(poll_interval),
            }
        finally:
            await db.close()

    # ── API Helpers ───────────────────────────────────────────

    async def _fetch_positions(self, address: str) -> list[dict]:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{DATA_API}/positions",
                params={"user": address},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch positions for {address}: {e}")
            return []

    async def _fetch_activity(self, address: str, limit: int = 50) -> list[dict]:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{DATA_API}/activity",
                params={"user": address, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch activity for {address}: {e}")
            return []

    async def _fetch_market_by_slug(self, slug: str) -> dict | None:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug, "limit": 1},
            )
            resp.raise_for_status()
            markets = resp.json()
            if markets and len(markets) > 0:
                return markets[0]
            return None
        except Exception as e:
            logger.error(f"Failed to fetch market for slug={slug}: {e}")
            return None

    async def _fetch_midpoint(self, token_id: str) -> float | None:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "mid" in data:
                return float(data["mid"])
            return None
        except Exception as e:
            logger.error(f"Failed to fetch midpoint for token_id={token_id}: {e}")
            return None

    async def _fetch_trades(self, address: str, limit: int = 100) -> list[dict]:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{DATA_API}/trades",
                params={"user": address, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch trades for {address}: {e}")
            return []

    # ── Filter Chain ──────────────────────────────────────────

    async def run_filter_chain(self, wallet: dict, position: dict, market_data: dict | None, db: aiosqlite.Connection) -> dict:
        """
        Run all 10 filters in order.
        If an API call fails during a filter, mark as PENDING not FAILED.
        """
        settings_rows = await db.execute_fetchall("SELECT key, value FROM copy_trade_settings")
        settings = {r["key"]: r["value"] for r in settings_rows}

        filters = {}
        failed_list = []

        # Extract position data
        position_size = float(position.get("currentValue", 0) or position.get("size", 0) or 0)
        condition_id = position.get("conditionId", "")
        outcome_index = int(position.get("outcomeIndex", 0))
        asset_id = position.get("asset", "") or position.get("assetId", "")
        market_slug = position.get("slug", "") or position.get("marketSlug", "")
        entry_price = float(position.get("avgPrice", 0) or position.get("price", 0) or 0)

        # Market data fields
        liquidity = None
        volume_24h = None
        end_date_str = None
        current_price = None

        if market_data:
            liquidity = _safe_float(market_data.get("liquidity"))
            volume_24h = _safe_float(market_data.get("volume24hr") or market_data.get("volume"))
            end_date_str = market_data.get("endDate") or market_data.get("resolutionDate")
            # Try to get current price from outcomePrices
            outcome_prices_raw = market_data.get("outcomePrices", "")
            if isinstance(outcome_prices_raw, str):
                try:
                    outcome_prices = json.loads(outcome_prices_raw)
                    if outcome_prices and outcome_index < len(outcome_prices):
                        current_price = float(outcome_prices[outcome_index])
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass
            elif isinstance(outcome_prices_raw, list) and outcome_index < len(outcome_prices_raw):
                current_price = _safe_float(outcome_prices_raw[outcome_index])

        # ── Filter 1: Liquidity ──────────────────────────────
        min_liquidity = float(settings.get("min_liquidity_usd", "75000"))
        if liquidity is not None:
            passed = liquidity >= min_liquidity
            filters["liquidity"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": liquidity,
                "threshold": min_liquidity,
                "message": f"Liquidity ${liquidity:,.0f} {'≥' if passed else '<'} ${min_liquidity:,.0f}",
            }
            if not passed:
                failed_list.append("liquidity")
        else:
            filters["liquidity"] = {
                "passed": False,
                "status": "PENDING",
                "value": None,
                "threshold": min_liquidity,
                "message": "Could not retrieve liquidity data",
            }

        # ── Filter 2: Entry Timing ───────────────────────────
        entry_timing_minutes = float(settings.get("entry_timing_minutes", "5"))
        activity_time = position.get("timestamp") or position.get("createdAt")
        if activity_time:
            try:
                if isinstance(activity_time, (int, float)):
                    pos_time = datetime.fromtimestamp(activity_time, tz=timezone.utc)
                else:
                    pos_time = datetime.fromisoformat(str(activity_time).replace("Z", "+00:00"))
                minutes_ago = (datetime.now(timezone.utc) - pos_time).total_seconds() / 60
                passed = minutes_ago <= entry_timing_minutes
                filters["entry_timing"] = {
                    "passed": passed,
                    "status": "PASSED" if passed else "FAILED",
                    "value": round(minutes_ago, 1),
                    "threshold": entry_timing_minutes,
                    "message": f"Position opened {minutes_ago:.1f}m ago {'≤' if passed else '>'} {entry_timing_minutes}m",
                }
                if not passed:
                    failed_list.append("entry_timing")
            except Exception:
                filters["entry_timing"] = {
                    "passed": False, "status": "PENDING",
                    "value": None, "threshold": entry_timing_minutes,
                    "message": "Could not parse position timestamp",
                }
        else:
            filters["entry_timing"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": entry_timing_minutes,
                "message": "No timestamp available for position",
            }

        # ── Filter 3: Wallet Position vs Pool ────────────────
        max_wallet_pool_pct = float(settings.get("max_wallet_pool_pct", "5.0"))
        if liquidity and liquidity > 0 and position_size > 0:
            pct = (position_size / liquidity) * 100
            passed = pct < max_wallet_pool_pct
            filters["wallet_pool_pct"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": round(pct, 2),
                "threshold": max_wallet_pool_pct,
                "message": f"Position is {pct:.2f}% of pool {'<' if passed else '≥'} {max_wallet_pool_pct}%",
            }
            if not passed:
                failed_list.append("wallet_pool_pct")
        else:
            filters["wallet_pool_pct"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": max_wallet_pool_pct,
                "message": "Cannot calculate position/pool ratio",
            }

        # ── Filter 4: 24H Volume ─────────────────────────────
        min_volume = float(settings.get("min_volume_24h_usd", "25000"))
        if volume_24h is not None:
            passed = volume_24h >= min_volume
            filters["volume_24h"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": volume_24h,
                "threshold": min_volume,
                "message": f"24h volume ${volume_24h:,.0f} {'≥' if passed else '<'} ${min_volume:,.0f}",
            }
            if not passed:
                failed_list.append("volume_24h")
        else:
            filters["volume_24h"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": min_volume,
                "message": "Could not retrieve 24h volume",
            }

        # ── Filter 5: Unique Traders ─────────────────────────
        min_traders = int(settings.get("min_unique_traders", "50"))
        unique_traders = None
        if market_data:
            unique_traders = _safe_int(market_data.get("uniqueTraders") or market_data.get("competitive"))
        if unique_traders is not None:
            passed = unique_traders >= min_traders
            filters["unique_traders"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": unique_traders,
                "threshold": min_traders,
                "message": f"{unique_traders} unique traders {'≥' if passed else '<'} {min_traders}",
            }
            if not passed:
                failed_list.append("unique_traders")
        else:
            filters["unique_traders"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": min_traders,
                "message": "Unique trader count not available",
            }

        # ── Filter 6: Resolution Date ────────────────────────
        res_min_days = int(settings.get("resolution_min_days", "3"))
        res_max_days = int(settings.get("resolution_max_days", "45"))
        days_to_resolution = None
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
                days_to_resolution = (end_date - datetime.now(timezone.utc)).days
                passed = res_min_days <= days_to_resolution <= res_max_days
                filters["resolution_date"] = {
                    "passed": passed,
                    "status": "PASSED" if passed else "FAILED",
                    "value": days_to_resolution,
                    "threshold": f"{res_min_days}-{res_max_days}",
                    "message": f"{days_to_resolution} days to resolution ({'within' if passed else 'outside'} {res_min_days}-{res_max_days} range)",
                }
                if not passed:
                    failed_list.append("resolution_date")
            except Exception:
                filters["resolution_date"] = {
                    "passed": False, "status": "PENDING",
                    "value": None, "threshold": f"{res_min_days}-{res_max_days}",
                    "message": "Could not parse resolution date",
                }
        else:
            filters["resolution_date"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": f"{res_min_days}-{res_max_days}",
                "message": "No resolution date available",
            }

        # ── Filter 7: Wallet Win Rate ────────────────────────
        min_win_rate = float(settings.get("min_wallet_win_rate", "55.0"))
        min_resolved = int(settings.get("min_resolved_markets", "20"))
        wallet_win_rate = _safe_float(wallet.get("csv_win_rate"))
        wallet_unique_markets = _safe_int(wallet.get("csv_unique_markets"))
        if wallet_win_rate is not None and wallet_unique_markets is not None:
            has_enough_markets = wallet_unique_markets >= min_resolved
            rate_ok = wallet_win_rate >= min_win_rate
            passed = has_enough_markets and rate_ok
            filters["wallet_win_rate"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": wallet_win_rate,
                "threshold": min_win_rate,
                "message": f"Win rate {wallet_win_rate:.1f}% {'≥' if rate_ok else '<'} {min_win_rate}%, {wallet_unique_markets} markets {'≥' if has_enough_markets else '<'} {min_resolved}",
            }
            if not passed:
                failed_list.append("wallet_win_rate")
        else:
            filters["wallet_win_rate"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": min_win_rate,
                "message": "Wallet win rate data not available",
            }

        # ── Filter 8: Price Movement 6h ──────────────────────
        max_move = float(settings.get("max_price_move_6h_pct", "12.0"))
        # We try to compare current price against a reference. Mark PENDING if unavailable.
        if current_price is not None and entry_price and entry_price > 0:
            move_pct = abs(current_price - entry_price) / entry_price * 100
            passed = move_pct <= max_move
            filters["price_movement_6h"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": round(move_pct, 2),
                "threshold": max_move,
                "message": f"Price moved {move_pct:.2f}% {'≤' if passed else '>'} {max_move}%",
            }
            if not passed:
                failed_list.append("price_movement_6h")
        else:
            filters["price_movement_6h"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": max_move,
                "message": "Cannot calculate price movement — insufficient data",
            }

        # ── Filter 9: Multi-Wallet Confirmation ──────────────
        min_confirming = int(settings.get("min_confirming_wallets", "2"))
        if condition_id:
            confirm_rows = await db.execute_fetchall(
                """SELECT COUNT(DISTINCT wallet_address) as cnt
                FROM wallet_position_cache
                WHERE condition_id = ? AND outcome_index = ?""",
                (condition_id, outcome_index),
            )
            confirming_count = confirm_rows[0]["cnt"] if confirm_rows else 0
            passed = confirming_count >= min_confirming
            filters["multi_wallet"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": confirming_count,
                "threshold": min_confirming,
                "message": f"{confirming_count} confirming wallets {'≥' if passed else '<'} {min_confirming}",
            }
            if not passed:
                failed_list.append("multi_wallet")
        else:
            filters["multi_wallet"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": min_confirming,
                "message": "No condition ID for confirmation check",
            }

        # ── Filter 10: Bankroll Exposure ─────────────────────
        max_bankroll_pct = float(settings.get("max_bankroll_pct", "5.0"))
        bankroll = float(settings.get("bankroll_usd", "10000"))
        if bankroll > 0 and current_price is not None and current_price > 0:
            proposed_size = bankroll * (max_bankroll_pct / 100)
            passed = True  # Always passes if we can calculate; this is a sizing guard
            filters["bankroll_exposure"] = {
                "passed": passed,
                "status": "PASSED" if passed else "FAILED",
                "value": round(proposed_size, 2),
                "threshold": max_bankroll_pct,
                "message": f"Max trade ${proposed_size:,.2f} ({max_bankroll_pct}% of ${bankroll:,.0f})",
            }
        else:
            filters["bankroll_exposure"] = {
                "passed": False, "status": "PENDING",
                "value": None, "threshold": max_bankroll_pct,
                "message": "Cannot calculate bankroll exposure",
            }

        # Determine overall pass
        all_passed = all(
            f.get("status") == "PASSED" for f in filters.values()
        )

        return {
            "all_passed": all_passed,
            "filters": filters,
            "failed_list": failed_list,
        }

    # ── Telegram ──────────────────────────────────────────────

    async def send_telegram_alert(self, signal: dict):
        """Send formatted Telegram message for passed signals."""
        db = await self._db()
        try:
            bot_token = await self._get_setting_value(db, "telegram_bot_token")
            chat_id = await self._get_setting_value(db, "telegram_chat_id")
        finally:
            await db.close()

        if not bot_token or not chat_id:
            logger.info("Telegram not configured — skipping alert")
            return

        execution_mode = signal.get("action_taken", "MANUAL")
        text = (
            f"\U0001f7e2 COPY TRADE SIGNAL\n"
            f"Market: {signal.get('market_title', 'Unknown')}\n"
            f"Direction: {signal.get('direction', '?')}\n"
            f"Entry Price: ${signal.get('wallet_entry_price', 0):.4f}\n"
            f"Current Price: ${signal.get('current_market_price', 0):.4f}\n"
            f"Liquidity: ${signal.get('liquidity_pool_usdc', 0):,.0f}\n"
            f"24h Volume: ${signal.get('volume_24h_usdc', 0):,.0f}\n"
            f"Confirming Wallets: {signal.get('confirming_wallet_count', 0)}\n"
            f"Wallet: {signal.get('wallet_username', '?')} ({signal.get('wallet_win_rate', 0)}% win rate)\n"
            f"Resolution: {signal.get('resolution_date', '?')} ({signal.get('days_to_resolution', '?')} days)\n"
            f"\u2705 ALL 10 FILTERS PASSED\n"
            f"Mode: {execution_mode}"
        )

        client = await self._get_client()
        try:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")

    # ── Main Polling Loop ─────────────────────────────────────

    async def poll_wallets(self) -> dict:
        """
        Main polling loop:
        1. Load all active wallets
        2. For each wallet, fetch current positions
        3. Compare against cache to find NEW positions
        4. For each new position, run the filter chain
        5. Log signal to copy_trade_signals
        6. Send Telegram alert if all filters pass
        7. Update wallet_position_cache
        """
        if self._is_running:
            return {"status": "already_running"}

        self._is_running = True
        start = time.time()
        wallets_scanned = 0
        new_positions_found = 0
        signals_passed = 0
        signals_failed = 0
        errors = []

        db = await self._db()
        try:
            # Load active wallets
            wallet_rows = await db.execute_fetchall(
                "SELECT id, address, username, csv_win_rate, csv_unique_markets FROM wallets WHERE is_active = 1"
            )

            for wallet_row in wallet_rows:
                wallet = dict(wallet_row)
                address = wallet["address"]

                try:
                    # Fetch current positions
                    positions = await self._fetch_positions(address)
                    await asyncio.sleep(RATE_LIMIT_DELAY)

                    for pos in positions:
                        condition_id = pos.get("conditionId", "")
                        outcome_index = int(pos.get("outcomeIndex", 0))

                        if not condition_id:
                            continue

                        # Check cache — is this a NEW position?
                        cached = await db.execute_fetchall(
                            """SELECT 1 FROM wallet_position_cache
                            WHERE wallet_address = ? AND condition_id = ? AND outcome_index = ?""",
                            (address, condition_id, outcome_index),
                        )

                        if cached:
                            continue  # Already seen

                        new_positions_found += 1

                        # Cache this position immediately
                        await db.execute(
                            """INSERT OR IGNORE INTO wallet_position_cache
                            (wallet_address, condition_id, outcome_index)
                            VALUES (?, ?, ?)""",
                            (address, condition_id, outcome_index),
                        )

                        # Fetch market data
                        market_slug = pos.get("slug", "") or pos.get("marketSlug", "")
                        market_data = None
                        if market_slug:
                            market_data = await self._fetch_market_by_slug(market_slug)
                            await asyncio.sleep(RATE_LIMIT_DELAY)

                        # Run filter chain
                        filter_result = await self.run_filter_chain(wallet, pos, market_data, db)

                        # Extract values for signal record
                        direction = "YES" if outcome_index == 0 else "NO"
                        entry_price = _safe_float(pos.get("avgPrice") or pos.get("price")) or 0
                        current_price = None
                        liquidity_val = None
                        volume_val = None
                        unique_traders_val = None
                        end_date_val = None
                        days_to_res = None
                        price_move_val = None
                        wallet_pool_pct_val = None

                        if market_data:
                            liquidity_val = _safe_float(market_data.get("liquidity"))
                            volume_val = _safe_float(market_data.get("volume24hr") or market_data.get("volume"))
                            unique_traders_val = _safe_int(market_data.get("uniqueTraders") or market_data.get("competitive"))
                            end_date_val = market_data.get("endDate") or market_data.get("resolutionDate")
                            outcome_prices_raw = market_data.get("outcomePrices", "")
                            if isinstance(outcome_prices_raw, str):
                                try:
                                    op = json.loads(outcome_prices_raw)
                                    if op and outcome_index < len(op):
                                        current_price = float(op[outcome_index])
                                except (json.JSONDecodeError, ValueError, IndexError):
                                    pass
                            elif isinstance(outcome_prices_raw, list) and outcome_index < len(outcome_prices_raw):
                                current_price = _safe_float(outcome_prices_raw[outcome_index])

                        if end_date_val:
                            try:
                                end_dt = datetime.fromisoformat(str(end_date_val).replace("Z", "+00:00"))
                                days_to_res = (end_dt - datetime.now(timezone.utc)).days
                            except Exception:
                                pass

                        # Extract filter-specific values
                        confirming_count = filter_result["filters"].get("multi_wallet", {}).get("value", 0)
                        price_move_val = filter_result["filters"].get("price_movement_6h", {}).get("value")
                        wallet_pool_pct_val = filter_result["filters"].get("wallet_pool_pct", {}).get("value")

                        all_passed = 1 if filter_result["all_passed"] else 0
                        failed_csv = ",".join(filter_result["failed_list"]) if filter_result["failed_list"] else None

                        settings_rows = await db.execute_fetchall("SELECT key, value FROM copy_trade_settings")
                        settings_map = {r["key"]: r["value"] for r in settings_rows}
                        execution_mode = settings_map.get("execution_mode", "MANUAL")
                        action_taken = "LOGGED"
                        if all_passed:
                            signals_passed += 1
                            action_taken = "LOGGED" if execution_mode == "MANUAL" else "EXECUTED"
                        else:
                            signals_failed += 1

                        market_title = pos.get("title", "") or (market_data.get("question", "") if market_data else "")
                        market_id_val = market_data.get("id", "") if market_data else ""

                        # Log signal
                        await db.execute(
                            """INSERT INTO copy_trade_signals
                            (wallet_address, wallet_username, market_id, market_slug, market_title,
                             direction, wallet_entry_price, current_market_price, liquidity_pool_usdc,
                             volume_24h_usdc, unique_trader_count, resolution_date, days_to_resolution,
                             confirming_wallet_count, price_movement_6h_pct, wallet_position_pct_of_pool,
                             all_filters_passed, filters_failed, filter_results, action_taken, notes)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                address,
                                wallet.get("username", ""),
                                market_id_val,
                                market_slug,
                                market_title,
                                direction,
                                entry_price,
                                current_price,
                                liquidity_val,
                                volume_val,
                                unique_traders_val,
                                end_date_val,
                                days_to_res,
                                confirming_count,
                                price_move_val,
                                wallet_pool_pct_val,
                                all_passed,
                                failed_csv,
                                json.dumps(filter_result["filters"]),
                                action_taken,
                                None,
                            ),
                        )

                        # Send Telegram if passed
                        if filter_result["all_passed"]:
                            try:
                                await self.send_telegram_alert({
                                    "market_title": market_title,
                                    "direction": direction,
                                    "wallet_entry_price": entry_price,
                                    "current_market_price": current_price or 0,
                                    "liquidity_pool_usdc": liquidity_val or 0,
                                    "volume_24h_usdc": volume_val or 0,
                                    "confirming_wallet_count": confirming_count or 0,
                                    "wallet_username": wallet.get("username", ""),
                                    "wallet_win_rate": wallet.get("csv_win_rate", 0),
                                    "resolution_date": end_date_val or "?",
                                    "days_to_resolution": days_to_res or "?",
                                    "action_taken": execution_mode,
                                })
                            except Exception as e:
                                logger.error(f"Telegram alert error: {e}")

                    wallets_scanned += 1

                except Exception as e:
                    errors.append(f"{address[:10]}: {str(e)[:100]}")
                    logger.error(f"Error polling wallet {address}: {e}")

            await db.commit()

        finally:
            await db.close()
            self._is_running = False

        self._last_poll = datetime.now(timezone.utc).isoformat()
        duration = time.time() - start

        return {
            "status": "completed",
            "wallets_scanned": wallets_scanned,
            "new_positions_found": new_positions_found,
            "signals_passed": signals_passed,
            "signals_failed": signals_failed,
            "duration_seconds": round(duration, 2),
            "errors": errors,
        }


# ── Utility functions ─────────────────────────────────────────

def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None
