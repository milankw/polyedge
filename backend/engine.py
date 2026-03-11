"""
CopyTradeEngine — scans Polymarket wallets, mirrors trades, tracks P&L.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import aiosqlite
import httpx

logger = logging.getLogger("polyedge.engine")

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "polyedge.db")

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RATE_LIMIT_DELAY = 0.15  # 150ms between API calls


class CopyTradeEngine:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._client: httpx.AsyncClient | None = None

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

    # ── API helpers ──────────────────────────────────────────────

    async def _fetch_trades(self, address: str, limit: int = 100) -> list[dict]:
        client = await self._get_client()
        try:
            resp = await client.get(
                f"{DATA_API}/trades",
                params={"user": address, "limit": limit, "offset": 0},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch trades for {address}: {e}")
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

    # ── Core scan ────────────────────────────────────────────────

    async def scan_all_wallets(self) -> dict:
        """Scan all active wallets for new trades and mirror them."""
        start = time.time()
        db = await self._db()
        errors = []
        wallets_scanned = 0
        total_new_trades = 0

        try:
            rows = await db.execute_fetchall(
                "SELECT id, address, alloc_usd, csv_volume FROM wallets WHERE is_active = 1"
            )
            for row in rows:
                wallet_id = row["id"]
                address = row["address"]
                alloc = row["alloc_usd"] or 1000.0
                csv_volume = row["csv_volume"] or 1.0

                try:
                    trades = await self._fetch_trades(address)
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    new_count = 0

                    for t in trades:
                        tx_hash = t.get("transactionHash")
                        if not tx_hash:
                            continue

                        # Check dedup
                        existing = await db.execute_fetchall(
                            "SELECT id FROM trades WHERE tx_hash = ?", (tx_hash,)
                        )
                        if existing:
                            continue

                        side = t.get("side", "").upper()
                        original_size = float(t.get("size", 0))
                        original_price = float(t.get("price", 0))
                        outcome_index = int(t.get("outcomeIndex", 0))
                        condition_id = t.get("conditionId", "")
                        timestamp_raw = t.get("timestamp")

                        # Parse timestamp
                        original_ts = None
                        if timestamp_raw:
                            try:
                                original_ts = int(timestamp_raw)
                            except (ValueError, TypeError):
                                try:
                                    dt = datetime.fromisoformat(str(timestamp_raw).replace("Z", "+00:00"))
                                    original_ts = int(dt.timestamp())
                                except Exception:
                                    original_ts = int(time.time())

                        if side == "BUY":
                            # Proportional sizing: min(10% of alloc, proportional to wallet volume)
                            proportional = alloc * (original_size / max(csv_volume, 1))
                            sim_size = min(alloc * 0.10, proportional)
                            sim_size = max(sim_size, 0.01)  # floor

                            await db.execute(
                                """INSERT INTO trades
                                (wallet_id, wallet_address, tx_hash, original_timestamp,
                                 condition_id, asset, side, original_size, original_price,
                                 outcome, outcome_index, market_title, market_slug, event_slug,
                                 sim_size, sim_entry_price, sim_current_price, sim_pnl, sim_status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 'open')""",
                                (
                                    wallet_id,
                                    address,
                                    tx_hash,
                                    original_ts,
                                    condition_id,
                                    t.get("asset", ""),
                                    "BUY",
                                    original_size,
                                    original_price,
                                    t.get("outcome", ""),
                                    outcome_index,
                                    t.get("title", ""),
                                    t.get("slug", ""),
                                    t.get("eventSlug", ""),
                                    sim_size,
                                    original_price,
                                    original_price,
                                ),
                            )
                            new_count += 1

                        elif side == "SELL":
                            # Record the sell trade
                            await db.execute(
                                """INSERT INTO trades
                                (wallet_id, wallet_address, tx_hash, original_timestamp,
                                 condition_id, asset, side, original_size, original_price,
                                 outcome, outcome_index, market_title, market_slug, event_slug,
                                 sim_size, sim_entry_price, sim_current_price, sim_pnl, sim_status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0.0, 'sold')""",
                                (
                                    wallet_id,
                                    address,
                                    tx_hash,
                                    original_ts,
                                    condition_id,
                                    t.get("asset", ""),
                                    "SELL",
                                    original_size,
                                    original_price,
                                    t.get("outcome", ""),
                                    outcome_index,
                                    t.get("title", ""),
                                    t.get("slug", ""),
                                    t.get("eventSlug", ""),
                                    original_price,
                                    original_price,
                                ),
                            )
                            new_count += 1

                            # Close matching open BUY position
                            open_pos = await db.execute_fetchall(
                                """SELECT id, sim_size, sim_entry_price FROM trades
                                WHERE wallet_id = ? AND condition_id = ? AND outcome_index = ?
                                AND sim_status = 'open' AND side = 'BUY'
                                ORDER BY detected_at ASC LIMIT 1""",
                                (wallet_id, condition_id, outcome_index),
                            )
                            if open_pos:
                                pos = open_pos[0]
                                shares = pos["sim_size"] / pos["sim_entry_price"] if pos["sim_entry_price"] > 0 else 0
                                realized_pnl = (original_price - pos["sim_entry_price"]) * shares
                                await db.execute(
                                    """UPDATE trades SET sim_status = 'sold',
                                    sim_exit_price = ?, sim_exit_time = datetime('now'),
                                    sim_pnl = ?, sim_current_price = ?,
                                    updated_at = datetime('now')
                                    WHERE id = ?""",
                                    (original_price, realized_pnl, original_price, pos["id"]),
                                )

                    total_new_trades += new_count
                    wallets_scanned += 1

                    # Update wallet last_scanned
                    await db.execute(
                        "UPDATE wallets SET last_scanned = datetime('now') WHERE id = ?",
                        (wallet_id,),
                    )

                except Exception as e:
                    errors.append(f"{address[:10]}: {str(e)[:100]}")
                    logger.error(f"Error scanning wallet {address}: {e}")

            await db.commit()

            # Update wallet stats
            await self._update_wallet_stats(db)
            await db.commit()

        finally:
            await db.close()

        duration = time.time() - start

        # Log the scan
        await self._log_scan("trades", wallets_scanned, total_new_trades, 0, 0, duration, errors)

        return {
            "wallets_scanned": wallets_scanned,
            "new_trades": total_new_trades,
            "duration_seconds": round(duration, 2),
            "errors": errors,
        }

    # ── Price update + resolution ─────────────────────────────────

    async def update_prices(self) -> dict:
        """Update prices for all open positions, resolve completed markets."""
        start = time.time()
        db = await self._db()
        errors = []
        prices_updated = 0
        positions_resolved = 0

        try:
            # Get all open trades grouped by slug
            open_trades = await db.execute_fetchall(
                """SELECT id, market_slug, outcome_index, sim_size, sim_entry_price
                FROM trades WHERE sim_status = 'open' AND side = 'BUY'"""
            )

            # Group by slug
            slug_map: dict[str, list] = {}
            for t in open_trades:
                slug = t["market_slug"]
                if slug:
                    slug_map.setdefault(slug, []).append(t)

            # Fetch price for each unique slug
            for slug, trade_list in slug_map.items():
                try:
                    market = await self._fetch_market_by_slug(slug)
                    await asyncio.sleep(RATE_LIMIT_DELAY)

                    if not market:
                        continue

                    outcome_prices_raw = market.get("outcomePrices", "")
                    if isinstance(outcome_prices_raw, str):
                        try:
                            outcome_prices = json.loads(outcome_prices_raw)
                        except json.JSONDecodeError:
                            continue
                    else:
                        outcome_prices = outcome_prices_raw

                    if not outcome_prices or len(outcome_prices) < 2:
                        continue

                    is_closed = market.get("closed", False)

                    for t in trade_list:
                        idx = t["outcome_index"]
                        if idx >= len(outcome_prices):
                            continue

                        current_price = float(outcome_prices[idx])
                        entry_price = t["sim_entry_price"] or 0
                        sim_size = t["sim_size"] or 0
                        shares = sim_size / entry_price if entry_price > 0 else 0

                        if is_closed:
                            # Market resolved
                            if current_price >= 0.95:
                                # This outcome won
                                pnl = (1.0 * shares) - sim_size
                                await db.execute(
                                    """UPDATE trades SET sim_status = 'won',
                                    sim_current_price = ?, sim_exit_price = 1.0,
                                    sim_pnl = ?, sim_exit_time = datetime('now'),
                                    updated_at = datetime('now')
                                    WHERE id = ?""",
                                    (current_price, pnl, t["id"]),
                                )
                                positions_resolved += 1
                            elif current_price <= 0.05:
                                # This outcome lost
                                pnl = -sim_size
                                await db.execute(
                                    """UPDATE trades SET sim_status = 'lost',
                                    sim_current_price = ?, sim_exit_price = 0.0,
                                    sim_pnl = ?, sim_exit_time = datetime('now'),
                                    updated_at = datetime('now')
                                    WHERE id = ?""",
                                    (current_price, pnl, t["id"]),
                                )
                                positions_resolved += 1
                            else:
                                # Closed but ambiguous price — mark as price
                                unrealized = (current_price - entry_price) * shares
                                await db.execute(
                                    """UPDATE trades SET sim_current_price = ?,
                                    sim_pnl = ?, updated_at = datetime('now')
                                    WHERE id = ?""",
                                    (current_price, unrealized, t["id"]),
                                )
                        else:
                            # Still open — update unrealized P&L
                            unrealized = (current_price - entry_price) * shares
                            await db.execute(
                                """UPDATE trades SET sim_current_price = ?,
                                sim_pnl = ?, updated_at = datetime('now')
                                WHERE id = ?""",
                                (current_price, unrealized, t["id"]),
                            )
                        prices_updated += 1

                except Exception as e:
                    errors.append(f"slug={slug}: {str(e)[:100]}")
                    logger.error(f"Error updating prices for slug={slug}: {e}")

            await db.commit()

            # Update wallet stats
            await self._update_wallet_stats(db)
            await db.commit()

        finally:
            await db.close()

        duration = time.time() - start
        await self._log_scan("prices", 0, 0, prices_updated, positions_resolved, duration, errors)

        return {
            "prices_updated": prices_updated,
            "positions_resolved": positions_resolved,
            "duration_seconds": round(duration, 2),
            "errors": errors,
        }

    # ── Full cycle ───────────────────────────────────────────────

    async def full_cycle(self) -> dict:
        """Run scan_all_wallets + update_prices + save_snapshot."""
        scan_result = await self.scan_all_wallets()
        price_result = await self.update_prices()
        await self._save_snapshot()

        return {
            "scan": scan_result,
            "prices": price_result,
        }

    # ── Snapshot ─────────────────────────────────────────────────

    async def _save_snapshot(self):
        db = await self._db()
        try:
            # Global snapshot
            row = await db.execute_fetchall("""
                SELECT
                    COUNT(*) as total_wallets,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_wallets,
                    SUM(sim_total_pnl) as total_sim_pnl,
                    SUM(sim_realized_pnl) as total_realized,
                    SUM(sim_unrealized_pnl) as total_unrealized,
                    SUM(sim_open_positions) as open_positions,
                    SUM(sim_total_trades) as total_trades
                FROM wallets
            """)
            if row:
                r = row[0]
                total_trades = r["total_trades"] or 0
                wins = 0
                wr_row = await db.execute_fetchall(
                    "SELECT SUM(sim_wins) as w FROM wallets"
                )
                if wr_row:
                    wins = wr_row[0]["w"] or 0
                overall_wr = (wins / total_trades * 100) if total_trades > 0 else 0

                # Best/worst wallet
                best = await db.execute_fetchall(
                    "SELECT id FROM wallets ORDER BY sim_total_pnl DESC LIMIT 1"
                )
                worst = await db.execute_fetchall(
                    "SELECT id FROM wallets ORDER BY sim_total_pnl ASC LIMIT 1"
                )

                await db.execute(
                    """INSERT INTO snapshots
                    (total_wallets, active_wallets, total_sim_pnl, total_realized,
                     total_unrealized, open_positions, total_trades, overall_win_rate,
                     best_wallet_id, worst_wallet_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["total_wallets"],
                        r["active_wallets"],
                        r["total_sim_pnl"] or 0,
                        r["total_realized"] or 0,
                        r["total_unrealized"] or 0,
                        r["open_positions"] or 0,
                        total_trades,
                        overall_wr,
                        best[0]["id"] if best else None,
                        worst[0]["id"] if worst else None,
                    ),
                )

            # Per-wallet snapshots
            wallets = await db.execute_fetchall(
                "SELECT id, sim_total_pnl, sim_open_positions, sim_win_rate FROM wallets WHERE is_active = 1"
            )
            for w in wallets:
                await db.execute(
                    """INSERT INTO wallet_snapshots (wallet_id, sim_pnl, open_positions, win_rate)
                    VALUES (?, ?, ?, ?)""",
                    (w["id"], w["sim_total_pnl"] or 0, w["sim_open_positions"] or 0, w["sim_win_rate"] or 0),
                )

            await db.commit()
        finally:
            await db.close()

    # ── Wallet stats update ──────────────────────────────────────

    async def _update_wallet_stats(self, db: aiosqlite.Connection):
        """Recompute sim stats for all wallets from trade data."""
        wallets = await db.execute_fetchall("SELECT id FROM wallets")
        for w in wallets:
            wid = w["id"]
            stats = await db.execute_fetchall(
                """SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN sim_status = 'won' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN sim_status = 'lost' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN sim_status IN ('won','lost','sold') THEN sim_pnl ELSE 0 END) as realized,
                    SUM(CASE WHEN sim_status = 'open' THEN sim_pnl ELSE 0 END) as unrealized,
                    SUM(CASE WHEN sim_status = 'open' THEN 1 ELSE 0 END) as open_pos
                FROM trades WHERE wallet_id = ? AND side = 'BUY'""",
                (wid,),
            )
            if stats:
                s = stats[0]
                total = s["total_trades"] or 0
                wins = s["wins"] or 0
                losses = s["losses"] or 0
                realized = s["realized"] or 0
                unrealized = s["unrealized"] or 0
                open_pos = s["open_pos"] or 0
                decided = wins + losses
                wr = (wins / decided * 100) if decided > 0 else 0

                await db.execute(
                    """UPDATE wallets SET
                    sim_total_pnl = ?, sim_realized_pnl = ?, sim_unrealized_pnl = ?,
                    sim_total_trades = ?, sim_wins = ?, sim_losses = ?,
                    sim_win_rate = ?, sim_open_positions = ?
                    WHERE id = ?""",
                    (realized + unrealized, realized, unrealized, total, wins, losses, wr, open_pos, wid),
                )

    # ── Scan log ─────────────────────────────────────────────────

    async def _log_scan(
        self, scan_type: str, wallets_scanned: int, new_trades: int,
        prices_updated: int, positions_resolved: int, duration: float, errors: list
    ):
        db = await self._db()
        try:
            await db.execute(
                """INSERT INTO scan_log
                (scan_type, wallets_scanned, new_trades, prices_updated,
                 positions_resolved, duration_seconds, errors)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_type,
                    wallets_scanned,
                    new_trades,
                    prices_updated,
                    positions_resolved,
                    round(duration, 2),
                    json.dumps(errors) if errors else None,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    # ── Dashboard data ───────────────────────────────────────────

    async def get_dashboard_data(self) -> dict:
        """Return everything the frontend needs in one call."""
        db = await self._db()
        try:
            # Global stats
            global_row = await db.execute_fetchall("""
                SELECT
                    SUM(sim_total_pnl) as total_pnl,
                    SUM(sim_realized_pnl) as realized,
                    SUM(sim_unrealized_pnl) as unrealized,
                    SUM(sim_total_trades) as total_trades,
                    SUM(sim_wins) as wins,
                    SUM(sim_losses) as losses,
                    SUM(sim_open_positions) as open_positions,
                    COUNT(*) as total_wallets,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_wallets
                FROM wallets
            """)
            g = global_row[0] if global_row else {}
            total_trades = g["total_trades"] or 0
            wins = g["wins"] or 0
            losses = g["losses"] or 0
            decided = wins + losses
            win_rate = (wins / decided * 100) if decided > 0 else 0

            # Best/worst wallet
            best = await db.execute_fetchall(
                "SELECT id, username, address, sim_total_pnl FROM wallets ORDER BY sim_total_pnl DESC LIMIT 1"
            )
            worst = await db.execute_fetchall(
                "SELECT id, username, address, sim_total_pnl FROM wallets ORDER BY sim_total_pnl ASC LIMIT 1"
            )

            global_stats = {
                "total_pnl": g["total_pnl"] or 0,
                "realized": g["realized"] or 0,
                "unrealized": g["unrealized"] or 0,
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 2),
                "open_positions": g["open_positions"] or 0,
                "total_wallets": g["total_wallets"] or 0,
                "active_wallets": g["active_wallets"] or 0,
                "best_wallet": dict(best[0]) if best else None,
                "worst_wallet": dict(worst[0]) if worst else None,
            }

            # Wallets sorted by sim_pnl
            wallets_rows = await db.execute_fetchall(
                """SELECT id, address, username, score, csv_win_rate, csv_pnl, csv_volume,
                   alloc_usd, is_active, sim_total_pnl, sim_realized_pnl, sim_unrealized_pnl,
                   sim_total_trades, sim_wins, sim_losses, sim_win_rate, sim_open_positions,
                   last_scanned, profile_url
                FROM wallets ORDER BY sim_total_pnl DESC"""
            )
            wallets = [dict(w) for w in wallets_rows]

            # Recent trades
            recent_rows = await db.execute_fetchall(
                """SELECT t.*, w.username as wallet_username
                FROM trades t JOIN wallets w ON t.wallet_id = w.id
                ORDER BY t.detected_at DESC LIMIT 50"""
            )
            recent_trades = [dict(r) for r in recent_rows]

            # Snapshots (last 168 hours = 7 days)
            snap_rows = await db.execute_fetchall(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 168"
            )
            snapshots = [dict(s) for s in snap_rows]
            snapshots.reverse()

            # Scan log
            log_rows = await db.execute_fetchall(
                "SELECT * FROM scan_log ORDER BY timestamp DESC LIMIT 20"
            )
            scan_log = [dict(l) for l in log_rows]

            return {
                "global_stats": global_stats,
                "wallets": wallets,
                "recent_trades": recent_trades,
                "snapshots": snapshots,
                "scan_log": scan_log,
            }
        finally:
            await db.close()

    # ── Wallet detail ────────────────────────────────────────────

    async def get_wallet_detail(self, wallet_id: int) -> dict:
        """Deep dive into one wallet."""
        db = await self._db()
        try:
            # Wallet info
            w_rows = await db.execute_fetchall(
                "SELECT * FROM wallets WHERE id = ?", (wallet_id,)
            )
            if not w_rows:
                return {"error": "Wallet not found"}
            wallet = dict(w_rows[0])

            # All trades
            trade_rows = await db.execute_fetchall(
                """SELECT * FROM trades WHERE wallet_id = ?
                ORDER BY original_timestamp DESC""",
                (wallet_id,),
            )
            trades = [dict(t) for t in trade_rows]

            # Wallet snapshots
            snap_rows = await db.execute_fetchall(
                """SELECT * FROM wallet_snapshots WHERE wallet_id = ?
                ORDER BY timestamp DESC LIMIT 168""",
                (wallet_id,),
            )
            snapshots = [dict(s) for s in snap_rows]
            snapshots.reverse()

            # P&L breakdown
            best_trade = None
            worst_trade = None
            for t in trades:
                if t["side"] != "BUY":
                    continue
                pnl = t["sim_pnl"] or 0
                if best_trade is None or pnl > (best_trade["sim_pnl"] or 0):
                    best_trade = t
                if worst_trade is None or pnl < (worst_trade["sim_pnl"] or 0):
                    worst_trade = t

            # Avg trade size
            buy_trades = [t for t in trades if t["side"] == "BUY"]
            avg_size = sum(t["sim_size"] or 0 for t in buy_trades) / len(buy_trades) if buy_trades else 0

            # Win/loss streaks
            results = []
            for t in sorted(buy_trades, key=lambda x: x["original_timestamp"] or 0):
                if t["sim_status"] == "won":
                    results.append("W")
                elif t["sim_status"] == "lost":
                    results.append("L")
            max_win_streak = max_loss_streak = current = 0
            current_type = None
            for r in results:
                if r == current_type:
                    current += 1
                else:
                    current_type = r
                    current = 1
                if r == "W":
                    max_win_streak = max(max_win_streak, current)
                else:
                    max_loss_streak = max(max_loss_streak, current)

            return {
                "wallet": wallet,
                "trades": trades,
                "snapshots": snapshots,
                "best_trade": best_trade,
                "worst_trade": worst_trade,
                "avg_trade_size": round(avg_size, 2),
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
            }
        finally:
            await db.close()

    # ── Query helpers for API endpoints ──────────────────────────

    async def get_all_wallets(self) -> list[dict]:
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                """SELECT id, address, username, score, csv_win_rate, csv_pnl, csv_volume,
                   alloc_usd, is_active, sim_total_pnl, sim_realized_pnl, sim_unrealized_pnl,
                   sim_total_trades, sim_wins, sim_losses, sim_win_rate, sim_open_positions,
                   last_scanned, profile_url
                FROM wallets ORDER BY sim_total_pnl DESC"""
            )
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_all_trades(self, limit: int = 200, offset: int = 0,
                              wallet_id: int | None = None, status: str | None = None) -> list[dict]:
        db = await self._db()
        try:
            query = """SELECT t.*, w.username as wallet_username
                FROM trades t JOIN wallets w ON t.wallet_id = w.id
                WHERE 1=1"""
            params: list = []
            if wallet_id:
                query += " AND t.wallet_id = ?"
                params.append(wallet_id)
            if status:
                query += " AND t.sim_status = ?"
                params.append(status)
            query += " ORDER BY t.detected_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = await db.execute_fetchall(query, params)
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_recent_trades(self, limit: int = 50) -> list[dict]:
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                """SELECT t.*, w.username as wallet_username
                FROM trades t JOIN wallets w ON t.wallet_id = w.id
                ORDER BY t.detected_at DESC LIMIT ?""",
                (limit,),
            )
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_snapshots(self, limit: int = 168) -> list[dict]:
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            result = [dict(r) for r in rows]
            result.reverse()
            return result
        finally:
            await db.close()

    async def get_scan_log(self, limit: int = 20) -> list[dict]:
        db = await self._db()
        try:
            rows = await db.execute_fetchall(
                "SELECT * FROM scan_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_stats(self) -> dict:
        db = await self._db()
        try:
            row = await db.execute_fetchall("""
                SELECT
                    SUM(sim_total_pnl) as total_pnl,
                    SUM(sim_total_trades) as total_trades,
                    SUM(sim_wins) as wins,
                    SUM(sim_losses) as losses,
                    SUM(sim_open_positions) as open_positions,
                    COUNT(*) as total_wallets,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) as active_wallets
                FROM wallets
            """)
            g = row[0] if row else {}
            decided = (g["wins"] or 0) + (g["losses"] or 0)
            win_rate = ((g["wins"] or 0) / decided * 100) if decided > 0 else 0

            # Last scan time
            last_scan = await db.execute_fetchall(
                "SELECT timestamp FROM scan_log ORDER BY timestamp DESC LIMIT 1"
            )
            return {
                "total_pnl": g["total_pnl"] or 0,
                "total_trades": g["total_trades"] or 0,
                "win_rate": round(win_rate, 2),
                "open_positions": g["open_positions"] or 0,
                "active_wallets": g["active_wallets"] or 0,
                "total_wallets": g["total_wallets"] or 0,
                "last_scan": last_scan[0]["timestamp"] if last_scan else None,
            }
        finally:
            await db.close()
