#!/usr/bin/env python3
"""
PolyEdge Copy Trade Engine — Monitors real Polymarket wallets,
detects trades, and mirrors them with simulated (fake) money.
100% paper trading — no real money ever.
"""

import asyncio
import json
import sqlite3
import os
import math
from datetime import datetime, timezone, timedelta

import httpx

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

REQUEST_DELAY = 0.15  # seconds between API calls for rate limiting


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


class CopyTradeEngine:
    """Core copy-trade engine: discover wallets, scan trades, mirror positions."""

    def __init__(self, db_path=None):
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = DB_PATH

    def _get_db(self):
        db = sqlite3.connect(self.db_path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # ── Wallet Discovery ──────────────────────────────────────────

    async def discover_wallets(self, time_period="MONTH", limit=50, min_pnl=1000) -> list:
        """Fetch leaderboard and return wallet candidates."""
        params = {
            "timePeriod": time_period,
            "orderBy": "PNL",
            "category": "OVERALL",
            "limit": limit,
            "offset": 0,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{DATA_API}/v1/leaderboard", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()

        results = []
        for entry in data:
            pnl = float(entry.get("pnl", 0) or 0)
            if pnl < min_pnl:
                continue
            results.append({
                "rank": entry.get("rank", 0),
                "address": entry.get("proxyWallet", ""),
                "username": entry.get("userName", ""),
                "x_username": entry.get("xUsername", ""),
                "pnl": pnl,
                "volume": float(entry.get("vol", 0) or 0),
                "verified": entry.get("verifiedBadge", False),
                "profile_image": entry.get("profileImage", ""),
            })
        return results

    # ── Wallet Management ─────────────────────────────────────────

    async def add_wallet(self, address: str, label: str = None, alloc_usd: float = 1000.0, source: str = "manual") -> dict:
        """Add a wallet to track."""
        db = self._get_db()
        try:
            existing = db.execute(
                "SELECT id FROM ct_wallets WHERE address = ?", (address.lower(),)
            ).fetchone()
            if existing:
                db.close()
                return {"error": "Wallet already tracked", "id": existing["id"]}

            cur = db.execute("""
                INSERT INTO ct_wallets (address, label, source, alloc_usd, is_active, added_at)
                VALUES (?, ?, ?, ?, 1, datetime('now'))
            """, (address.lower(), label or address[:10], source, alloc_usd))
            db.commit()
            wallet_id = cur.lastrowid
            return {"id": wallet_id, "status": "added", "address": address.lower()}
        finally:
            db.close()

    async def remove_wallet(self, wallet_id: int) -> dict:
        """Remove a wallet from tracking."""
        db = self._get_db()
        try:
            db.execute("DELETE FROM ct_trades WHERE wallet_id = ?", (wallet_id,))
            db.execute("DELETE FROM ct_snapshots WHERE wallet_id = ?", (wallet_id,))
            db.execute("DELETE FROM ct_wallets WHERE id = ?", (wallet_id,))
            db.commit()
            return {"status": "removed", "id": wallet_id}
        finally:
            db.close()

    async def get_wallets(self) -> list:
        """Get all tracked wallets with summary stats."""
        db = self._get_db()
        try:
            wallets = db.execute("""
                SELECT w.*,
                    COALESCE((SELECT COUNT(*) FROM ct_trades t WHERE t.wallet_id = w.id AND t.sim_status = 'open'), 0) as open_positions,
                    COALESCE((SELECT SUM(sim_pnl) FROM ct_trades t WHERE t.wallet_id = w.id), 0) as current_pnl,
                    COALESCE((SELECT COUNT(*) FROM ct_trades t WHERE t.wallet_id = w.id), 0) as trade_count,
                    COALESCE((SELECT MAX(detected_at) FROM ct_trades t WHERE t.wallet_id = w.id), '') as last_trade_time
                FROM ct_wallets w
                ORDER BY w.added_at DESC
            """).fetchall()
            return [dict(w) for w in wallets]
        finally:
            db.close()

    # ── Trade Scanning ────────────────────────────────────────────

    async def scan_wallet_trades(self, wallet_address: str, since_timestamp: int = None) -> list:
        """Fetch recent trades for a wallet from /trades endpoint."""
        params = {
            "user": wallet_address,
            "limit": 100,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{DATA_API}/trades", params=params)
            if resp.status_code != 200:
                return []
            trades = resp.json()

        if since_timestamp:
            trades = [t for t in trades if int(t.get("timestamp", 0) or 0) > since_timestamp]

        return trades

    async def scan_all_wallets(self) -> dict:
        """Scan all active wallets for new trades, mirror them."""
        db = self._get_db()
        summary = {
            "wallets_scanned": 0,
            "new_trades_found": 0,
            "trades_mirrored": 0,
            "errors": [],
        }

        try:
            wallets = db.execute(
                "SELECT * FROM ct_wallets WHERE is_active = 1"
            ).fetchall()

            for wallet in wallets:
                summary["wallets_scanned"] += 1
                wallet_id = wallet["id"]
                address = wallet["address"]
                alloc = wallet["alloc_usd"] or 1000.0

                try:
                    # Get last checked timestamp
                    last_ts = None
                    last_trade = db.execute(
                        "SELECT MAX(original_timestamp) as ts FROM ct_trades WHERE wallet_id = ?",
                        (wallet_id,)
                    ).fetchone()
                    if last_trade and last_trade["ts"]:
                        last_ts = last_trade["ts"]

                    trades = await self.scan_wallet_trades(address, last_ts)
                    await asyncio.sleep(REQUEST_DELAY)

                    for trade in trades:
                        tx_hash = trade.get("transactionHash", "")
                        if not tx_hash:
                            continue

                        # Check if we already have this trade
                        existing = db.execute(
                            "SELECT id FROM ct_trades WHERE original_tx = ?", (tx_hash,)
                        ).fetchone()
                        if existing:
                            continue

                        summary["new_trades_found"] += 1

                        side = trade.get("side", "BUY")
                        original_size = float(trade.get("size", 0) or 0)
                        original_price = float(trade.get("price", 0) or 0)
                        timestamp = int(trade.get("timestamp", 0) or 0)

                        if side == "SELL":
                            # Close matching open position
                            open_pos = db.execute("""
                                SELECT id, sim_size, sim_entry_price FROM ct_trades
                                WHERE wallet_id = ? AND condition_id = ? AND sim_status = 'open'
                                  AND outcome_index = ?
                                ORDER BY detected_at ASC LIMIT 1
                            """, (wallet_id, trade.get("conditionId", ""),
                                  trade.get("outcomeIndex", 0))).fetchone()

                            if open_pos:
                                exit_pnl = (original_price - open_pos["sim_entry_price"]) * open_pos["sim_size"]
                                status = "closed_profit" if exit_pnl >= 0 else "closed_loss"
                                db.execute("""
                                    UPDATE ct_trades SET
                                        sim_current_price = ?,
                                        sim_pnl = ?,
                                        sim_status = ?,
                                        sim_exit_price = ?,
                                        sim_exit_time = datetime('now'),
                                        updated_at = datetime('now')
                                    WHERE id = ?
                                """, (original_price, exit_pnl, status, original_price, open_pos["id"]))
                                summary["trades_mirrored"] += 1

                            # Also record the SELL trade itself
                            db.execute("""
                                INSERT INTO ct_trades
                                (wallet_id, wallet_address, original_tx, original_timestamp,
                                 condition_id, asset, side, original_size, original_price,
                                 outcome, outcome_index, market_title, market_slug, event_slug,
                                 sim_size, sim_entry_price, sim_current_price, sim_pnl,
                                 sim_status, detected_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'closed_profit', datetime('now'), datetime('now'))
                            """, (
                                wallet_id, address, tx_hash, timestamp,
                                trade.get("conditionId", ""), trade.get("asset", ""),
                                side, original_size, original_price,
                                trade.get("outcome", ""), trade.get("outcomeIndex", 0),
                                trade.get("title", ""), trade.get("slug", ""),
                                trade.get("eventSlug", ""),
                                0, original_price, original_price,
                            ))
                        else:
                            # BUY — create a new sim mirror position
                            # Proportional sizing: cap at 10% of alloc per trade
                            wallet_volume = wallet["leaderboard_volume"] or 100000
                            ratio = alloc / max(wallet_volume, 1)
                            sim_size = ratio * original_size
                            max_per_trade = alloc * 0.10
                            sim_size = min(sim_size, max_per_trade)
                            sim_size = max(sim_size, 0.01)  # minimum

                            db.execute("""
                                INSERT INTO ct_trades
                                (wallet_id, wallet_address, original_tx, original_timestamp,
                                 condition_id, asset, side, original_size, original_price,
                                 outcome, outcome_index, market_title, market_slug, event_slug,
                                 sim_size, sim_entry_price, sim_current_price, sim_pnl,
                                 sim_status, detected_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'open', datetime('now'), datetime('now'))
                            """, (
                                wallet_id, address, tx_hash, timestamp,
                                trade.get("conditionId", ""), trade.get("asset", ""),
                                side, original_size, original_price,
                                trade.get("outcome", ""), trade.get("outcomeIndex", 0),
                                trade.get("title", ""), trade.get("slug", ""),
                                trade.get("eventSlug", ""),
                                round(sim_size, 4), original_price, original_price,
                            ))
                            summary["trades_mirrored"] += 1

                    # Update wallet last_checked
                    db.execute(
                        "UPDATE ct_wallets SET last_checked = datetime('now') WHERE id = ?",
                        (wallet_id,)
                    )

                except Exception as e:
                    summary["errors"].append(f"{address[:10]}...: {str(e)}")

            # Update wallet aggregate stats
            for wallet in wallets:
                wid = wallet["id"]
                stats = db.execute("""
                    SELECT
                        COALESCE(SUM(sim_pnl), 0) as total_pnl,
                        COUNT(*) as total_trades,
                        COALESCE(SUM(CASE WHEN sim_status LIKE 'closed_profit%' OR sim_status = 'resolved_win' THEN 1 ELSE 0 END), 0) as wins,
                        COALESCE(SUM(CASE WHEN sim_status LIKE 'closed_loss%' OR sim_status = 'resolved_loss' THEN 1 ELSE 0 END), 0) as losses
                    FROM ct_trades WHERE wallet_id = ? AND side = 'BUY'
                """, (wid,)).fetchone()

                total_closed = (stats["wins"] or 0) + (stats["losses"] or 0)
                win_rate = (stats["wins"] / total_closed * 100) if total_closed > 0 else 0

                db.execute("""
                    UPDATE ct_wallets SET
                        total_sim_pnl = ?,
                        total_sim_trades = ?,
                        win_rate = ?
                    WHERE id = ?
                """, (stats["total_pnl"], stats["total_trades"], round(win_rate, 1), wid))

            # Log the scan
            db.execute("""
                INSERT INTO ct_scan_log (timestamp, wallets_scanned, new_trades_found, trades_mirrored, errors)
                VALUES (datetime('now'), ?, ?, ?, ?)
            """, (
                summary["wallets_scanned"],
                summary["new_trades_found"],
                summary["trades_mirrored"],
                json.dumps(summary["errors"]) if summary["errors"] else None,
            ))

            db.commit()
            return summary

        except Exception as e:
            summary["errors"].append(str(e))
            return summary
        finally:
            db.close()

    # ── Price Updates ─────────────────────────────────────────────

    async def update_prices(self) -> dict:
        """Update current prices for all open sim positions using Gamma API slug lookup."""
        db = self._get_db()
        summary = {"updated": 0, "errors": 0}

        try:
            open_trades = db.execute(
                "SELECT * FROM ct_trades WHERE sim_status = 'open'"
            ).fetchall()

            # Group by market_slug to avoid duplicate API calls
            slug_prices = {}  # slug -> {outcome_index: price}
            for trade in open_trades:
                slug = trade["market_slug"]
                if slug and slug not in slug_prices:
                    try:
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.get(
                                f"{GAMMA_API}/markets",
                                params={"slug": slug, "limit": 1}
                            )
                            if resp.status_code == 200:
                                markets = resp.json()
                                if markets:
                                    market = markets[0]
                                    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
                                    is_closed = market.get("closed", False)
                                    slug_prices[slug] = {
                                        "prices": [float(p) for p in outcome_prices] if outcome_prices else [],
                                        "closed": is_closed
                                    }
                        await asyncio.sleep(REQUEST_DELAY)
                    except Exception:
                        summary["errors"] += 1

            for trade in open_trades:
                slug = trade["market_slug"]
                if slug not in slug_prices:
                    continue
                market_data = slug_prices[slug]
                prices = market_data["prices"]
                outcome_idx = trade["outcome_index"]
                if not prices or outcome_idx >= len(prices):
                    continue

                current_price = prices[outcome_idx]
                entry_price = trade["sim_entry_price"]
                sim_size = trade["sim_size"]
                shares = sim_size / entry_price if entry_price > 0 else 0

                # Check if market resolved
                if market_data["closed"]:
                    if current_price >= 0.95:
                        pnl = shares * 1.0 - sim_size
                        status = "resolved_win"
                    elif current_price <= 0.05:
                        pnl = -sim_size
                        status = "resolved_loss"
                    else:
                        pnl = (current_price - entry_price) * shares
                        status = "closed_profit" if pnl >= 0 else "closed_loss"

                    db.execute("""
                        UPDATE ct_trades SET
                            sim_current_price = ?,
                            sim_pnl = ?,
                            sim_status = ?,
                            sim_exit_price = ?,
                            sim_exit_time = datetime('now'),
                            updated_at = datetime('now')
                        WHERE id = ?
                    """, (current_price, round(pnl, 4), status, current_price, trade["id"]))
                else:
                    # Still open — unrealized P&L
                    unrealized_pnl = (current_price - entry_price) * shares
                    db.execute("""
                        UPDATE ct_trades SET
                            sim_current_price = ?,
                            sim_pnl = ?,
                            updated_at = datetime('now')
                        WHERE id = ?
                    """, (current_price, round(unrealized_pnl, 4), trade["id"]))

                summary["updated"] += 1

            # Update wallet total P&L and win rate
            wallets = db.execute("SELECT id FROM ct_wallets").fetchall()
            for w in wallets:
                stats = db.execute("""
                    SELECT 
                        COALESCE(SUM(sim_pnl), 0) as total_pnl,
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN sim_status IN ('resolved_win', 'closed_profit') THEN 1 ELSE 0 END) as wins
                    FROM ct_trades WHERE wallet_id = ? AND sim_status != 'open'
                """, (w["id"],)).fetchone()
                total_pnl = stats["total_pnl"]
                total_resolved = stats["total_trades"]
                wins = stats["wins"]
                win_rate = round((wins / total_resolved * 100), 1) if total_resolved > 0 else 0.0
                db.execute(
                    "UPDATE ct_wallets SET total_sim_pnl = ?, win_rate = ? WHERE id = ?",
                    (round(total_pnl, 4), win_rate, w["id"])
                )

            db.commit()
            return summary
        finally:
            db.close()

    # ── Performance ───────────────────────────────────────────────

    async def get_wallet_performance(self, wallet_id: int) -> dict:
        """Get detailed P&L for a specific wallet."""
        db = self._get_db()
        try:
            wallet = db.execute(
                "SELECT * FROM ct_wallets WHERE id = ?", (wallet_id,)
            ).fetchone()
            if not wallet:
                return {"error": "Wallet not found"}

            stats = db.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    COALESCE(SUM(sim_pnl), 0) as total_pnl,
                    COALESCE(SUM(CASE WHEN sim_status = 'open' THEN 1 ELSE 0 END), 0) as open_positions,
                    COALESCE(SUM(CASE WHEN sim_status LIKE 'closed_profit%' OR sim_status = 'resolved_win' THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(CASE WHEN sim_status LIKE 'closed_loss%' OR sim_status = 'resolved_loss' THEN 1 ELSE 0 END), 0) as losses,
                    COALESCE(SUM(CASE WHEN sim_status = 'open' THEN sim_pnl ELSE 0 END), 0) as unrealized_pnl,
                    COALESCE(SUM(CASE WHEN sim_status != 'open' THEN sim_pnl ELSE 0 END), 0) as realized_pnl,
                    COALESCE(MAX(sim_pnl), 0) as best_trade_pnl,
                    COALESCE(MIN(sim_pnl), 0) as worst_trade_pnl
                FROM ct_trades WHERE wallet_id = ? AND side = 'BUY'
            """, (wallet_id,)).fetchone()

            total_closed = (stats["wins"] or 0) + (stats["losses"] or 0)
            win_rate = (stats["wins"] / total_closed * 100) if total_closed > 0 else 0

            return {
                "wallet": dict(wallet),
                "total_trades": stats["total_trades"],
                "total_pnl": round(stats["total_pnl"], 2),
                "unrealized_pnl": round(stats["unrealized_pnl"], 2),
                "realized_pnl": round(stats["realized_pnl"], 2),
                "open_positions": stats["open_positions"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round(win_rate, 1),
                "best_trade_pnl": round(stats["best_trade_pnl"], 2),
                "worst_trade_pnl": round(stats["worst_trade_pnl"], 2),
            }
        finally:
            db.close()

    async def get_overall_performance(self) -> dict:
        """Get aggregated performance across all wallets."""
        db = self._get_db()
        try:
            stats = db.execute("""
                SELECT
                    COUNT(DISTINCT wallet_id) as active_wallets,
                    COUNT(*) as total_trades,
                    COALESCE(SUM(sim_pnl), 0) as total_pnl,
                    COALESCE(SUM(CASE WHEN sim_status = 'open' THEN 1 ELSE 0 END), 0) as open_positions,
                    COALESCE(SUM(CASE WHEN sim_status = 'open' THEN sim_pnl ELSE 0 END), 0) as unrealized_pnl,
                    COALESCE(SUM(CASE WHEN sim_status != 'open' THEN sim_pnl ELSE 0 END), 0) as realized_pnl,
                    COALESCE(SUM(CASE WHEN sim_status LIKE 'closed_profit%' OR sim_status = 'resolved_win' THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(CASE WHEN sim_status LIKE 'closed_loss%' OR sim_status = 'resolved_loss' THEN 1 ELSE 0 END), 0) as losses,
                    COALESCE(MAX(sim_pnl), 0) as best_trade_pnl,
                    COALESCE(MIN(sim_pnl), 0) as worst_trade_pnl
                FROM ct_trades WHERE side = 'BUY'
            """).fetchone()

            total_closed = (stats["wins"] or 0) + (stats["losses"] or 0)
            win_rate = (stats["wins"] / total_closed * 100) if total_closed > 0 else 0

            # Best and worst wallets
            best_wallet = db.execute("""
                SELECT w.label, w.address, COALESCE(SUM(t.sim_pnl), 0) as pnl
                FROM ct_wallets w LEFT JOIN ct_trades t ON t.wallet_id = w.id AND t.side = 'BUY'
                WHERE w.is_active = 1
                GROUP BY w.id ORDER BY pnl DESC LIMIT 1
            """).fetchone()

            worst_wallet = db.execute("""
                SELECT w.label, w.address, COALESCE(SUM(t.sim_pnl), 0) as pnl
                FROM ct_wallets w LEFT JOIN ct_trades t ON t.wallet_id = w.id AND t.side = 'BUY'
                WHERE w.is_active = 1
                GROUP BY w.id ORDER BY pnl ASC LIMIT 1
            """).fetchone()

            active_count = db.execute(
                "SELECT COUNT(*) FROM ct_wallets WHERE is_active = 1"
            ).fetchone()[0]

            return {
                "active_wallets": active_count,
                "total_trades": stats["total_trades"],
                "total_pnl": round(stats["total_pnl"], 2),
                "unrealized_pnl": round(stats["unrealized_pnl"], 2),
                "realized_pnl": round(stats["realized_pnl"], 2),
                "open_positions": stats["open_positions"],
                "wins": stats["wins"] or 0,
                "losses": stats["losses"] or 0,
                "win_rate": round(win_rate, 1),
                "best_trade_pnl": round(stats["best_trade_pnl"], 2),
                "worst_trade_pnl": round(stats["worst_trade_pnl"], 2),
                "best_wallet": {
                    "label": best_wallet["label"] if best_wallet else "—",
                    "pnl": round(best_wallet["pnl"], 2) if best_wallet else 0,
                } if best_wallet else None,
                "worst_wallet": {
                    "label": worst_wallet["label"] if worst_wallet else "—",
                    "pnl": round(worst_wallet["pnl"], 2) if worst_wallet else 0,
                } if worst_wallet else None,
            }
        finally:
            db.close()

    async def get_trades(self, wallet_id: int = None, limit: int = 50, offset: int = 0) -> dict:
        """Get trades, optionally filtered by wallet."""
        db = self._get_db()
        try:
            if wallet_id:
                rows = db.execute("""
                    SELECT t.*, w.label as wallet_label
                    FROM ct_trades t LEFT JOIN ct_wallets w ON t.wallet_id = w.id
                    WHERE t.wallet_id = ?
                    ORDER BY t.detected_at DESC LIMIT ? OFFSET ?
                """, (wallet_id, limit, offset)).fetchall()
                total = db.execute(
                    "SELECT COUNT(*) FROM ct_trades WHERE wallet_id = ?", (wallet_id,)
                ).fetchone()[0]
            else:
                rows = db.execute("""
                    SELECT t.*, w.label as wallet_label
                    FROM ct_trades t LEFT JOIN ct_wallets w ON t.wallet_id = w.id
                    ORDER BY t.detected_at DESC LIMIT ? OFFSET ?
                """, (limit, offset)).fetchall()
                total = db.execute("SELECT COUNT(*) FROM ct_trades").fetchone()[0]

            return {"trades": [dict(r) for r in rows], "total": total}
        finally:
            db.close()

    async def get_scan_log(self, limit: int = 50) -> list:
        """Get scan operation history."""
        db = self._get_db()
        try:
            rows = db.execute(
                "SELECT * FROM ct_scan_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    async def check_resolutions(self) -> dict:
        """Check if any markets have resolved and update positions."""
        db = self._get_db()
        summary = {"resolved": 0, "errors": 0}

        try:
            open_trades = db.execute(
                "SELECT DISTINCT condition_id FROM ct_trades WHERE sim_status = 'open' AND condition_id != ''"
            ).fetchall()

            for row in open_trades:
                condition_id = row["condition_id"]
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        resp = await client.get(
                            f"{GAMMA_API}/markets",
                            params={"condition_id": condition_id, "limit": 1}
                        )
                        if resp.status_code != 200:
                            continue
                        markets = resp.json()
                        if not markets:
                            continue
                        market = markets[0]

                    await asyncio.sleep(REQUEST_DELAY)

                    is_closed = market.get("closed", False) or not market.get("active", True)
                    if not is_closed:
                        continue

                    # Market resolved — determine outcome
                    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
                    if len(outcome_prices) < 2:
                        continue

                    yes_final = float(outcome_prices[0])

                    # Update all open trades for this condition
                    trades = db.execute(
                        "SELECT * FROM ct_trades WHERE condition_id = ? AND sim_status = 'open'",
                        (condition_id,)
                    ).fetchall()

                    for trade in trades:
                        outcome_idx = trade["outcome_index"]
                        final_price = yes_final if outcome_idx == 0 else (1.0 - yes_final)
                        entry_price = trade["sim_entry_price"]
                        sim_size = trade["sim_size"]
                        shares = sim_size / entry_price if entry_price > 0 else 0

                        if final_price >= 0.95:
                            # Won
                            pnl = shares * 1.0 - sim_size
                            status = "resolved_win"
                        elif final_price <= 0.05:
                            # Lost
                            pnl = -sim_size
                            status = "resolved_loss"
                        else:
                            # Partial
                            pnl = (final_price - entry_price) * shares
                            status = "closed_profit" if pnl >= 0 else "closed_loss"

                        db.execute("""
                            UPDATE ct_trades SET
                                sim_current_price = ?,
                                sim_pnl = ?,
                                sim_status = ?,
                                sim_exit_price = ?,
                                sim_exit_time = datetime('now'),
                                updated_at = datetime('now')
                            WHERE id = ?
                        """, (final_price, round(pnl, 4), status, final_price, trade["id"]))
                        summary["resolved"] += 1

                except Exception:
                    summary["errors"] += 1

            db.commit()
            return summary
        finally:
            db.close()
