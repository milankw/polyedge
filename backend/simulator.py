#!/usr/bin/env python3
"""
PolyEdge Simulation Engine — Core Simulation Logic
Places fictional bets with fake money, tracks against real market prices,
auto-resolves, builds P&L, and learns from every decision.
"""

import json
import sqlite3
import math
import os
import asyncio
import threading
from datetime import datetime, timezone, timedelta

import httpx

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

GAMMA_API = "https://gamma-api.polymarket.com"

# ── Defaults ────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "sim_bankroll": 10000.0,
    "sim_max_bet": 200.0,
    "sim_min_bet": 5.0,
    "sim_daily_budget": 500.0,
    "sim_min_edge": 0.03,
    "sim_min_volume": 500.0,
    "sim_max_open_positions": 50,
    "sim_kelly_fraction": 0.25,
    "sim_auto_cycle": False,
    "sim_cycle_interval": 1800,  # 30 min
}


def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _get_sim_config(db):
    """Load sim config from config table, falling back to defaults."""
    cfg = dict(DEFAULT_CONFIG)
    rows = db.execute("SELECT key, value FROM config WHERE key LIKE 'sim_%'").fetchall()
    for r in rows:
        key = r["key"]
        val = r["value"]
        if key in cfg:
            if isinstance(cfg[key], bool):
                cfg[key] = val.lower() in ("true", "1", "yes")
            elif isinstance(cfg[key], float):
                try:
                    cfg[key] = float(val)
                except ValueError:
                    pass
            elif isinstance(cfg[key], int):
                try:
                    cfg[key] = int(float(val))
                except ValueError:
                    pass
    return cfg


# ── Market Fetching (reuse pattern from api_server) ─────────────

async def _fetch_markets_async(limit=100):
    """Fetch active markets from Gamma API."""
    params = {
        "limit": limit,
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GAMMA_API}/markets", params=params)
        if resp.status_code == 200:
            return resp.json()
    return []


async def _fetch_market_by_id(market_id: str):
    """Fetch a single market by ID."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GAMMA_API}/markets/{market_id}")
        if resp.status_code == 200:
            return resp.json()
    return None


def _analyze_market_edge(market: dict) -> dict:
    """
    Core analysis: detect mispricing using multiple signals.
    Identical logic to api_server.analyze_market_edge for consistency.
    """
    signals = []
    yes_price = 0
    no_price = 0

    try:
        outcomes = json.loads(market.get("outcomePrices", "[]"))
        if len(outcomes) >= 2:
            yes_price = float(outcomes[0])
            no_price = float(outcomes[1])
    except (json.JSONDecodeError, ValueError, IndexError):
        return {"edge": 0, "confidence": "none", "signals": [], "yes_price": 0, "no_price": 0}

    # Signal 1: Arbitrage check
    total = yes_price + no_price
    if total < 0.98:
        arb_edge = 1.0 - total
        signals.append({
            "type": "arbitrage",
            "edge": arb_edge,
            "detail": f"YES+NO = {total:.3f}, gap of {arb_edge:.3f}"
        })

    # Signal 2: Extreme odds with volume imbalance
    volume = float(market.get("volume", 0) or 0)
    volume_24h = float(market.get("volume24hr", 0) or 0)

    if yes_price > 0.90 and volume > 10000:
        signals.append({
            "type": "high_confidence_market",
            "edge": 0.02,
            "detail": f"Market at {yes_price:.0%} with ${volume:,.0f} volume"
        })

    # Signal 3: Low-liquidity mispricing
    liquidity = float(market.get("liquidityClob", 0) or 0)
    if liquidity < 2000 and volume_24h > 500 and 0.15 < yes_price < 0.85:
        signals.append({
            "type": "low_liquidity",
            "edge": 0.08,
            "detail": f"Thin book (${liquidity:,.0f}) with activity"
        })

    # Signal 4: Volume surge detection
    if volume_24h > 0 and volume > 0:
        volume_ratio = volume_24h / max(volume / 30, 1)
        if volume_ratio > 3:
            signals.append({
                "type": "volume_surge",
                "edge": 0.05,
                "detail": f"24h volume {volume_ratio:.1f}x daily average"
            })

    # Signal 5: Price near resolution boundaries
    end_date_str = market.get("endDate", "")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_to_end = (end_date - now).total_seconds() / 86400
            if days_to_end < 3 and 0.05 < yes_price < 0.95:
                signals.append({
                    "type": "near_expiry",
                    "edge": 0.04,
                    "detail": f"Resolves in {days_to_end:.1f} days"
                })
        except (ValueError, TypeError):
            pass

    if not signals:
        return {"edge": 0, "confidence": "none", "signals": [], "yes_price": yes_price, "no_price": no_price}

    total_edge = max(s["edge"] for s in signals)
    confidence = "low"
    if total_edge >= 0.08:
        confidence = "high"
    elif total_edge >= 0.04:
        confidence = "medium"

    return {
        "edge": total_edge,
        "confidence": confidence,
        "signals": signals,
        "yes_price": yes_price,
        "no_price": no_price,
    }


def _kelly_size(edge: float, probability: float, bankroll: float, fraction: float = 0.25) -> float:
    """Calculate Kelly Criterion position size (fractional Kelly)."""
    if edge <= 0 or probability <= 0 or probability >= 1:
        return 0
    # Kelly: f* = (bp - q) / b where b = odds, p = probability of winning, q = 1-p
    # Simplified for binary: f* = edge / odds
    odds = (1 / probability) - 1 if probability < 1 else 0
    if odds <= 0:
        return 0
    kelly_f = (edge * (1 + odds) - (1 - edge)) / odds if odds > 0 else 0
    kelly_f = max(0, kelly_f)
    # Apply fraction and bankroll
    size = bankroll * kelly_f * fraction
    return round(size, 2)


def _classify_strategy(wins: int, losses: int, total_bets: int, roi: float) -> str:
    """Classify strategy lifecycle status."""
    if total_bets < 5:
        return "exploring"
    win_rate = wins / total_bets if total_bets > 0 else 0
    if roi < -0.15 or (total_bets >= 10 and win_rate < 0.35):
        return "killing"
    if win_rate >= 0.55 and roi > 0.05 and total_bets >= 10:
        return "scaling"
    if win_rate >= 0.50 or roi > 0:
        return "promising"
    return "exploring"


# ── Decision Log ────────────────────────────────────────────────

def _log_decision(db, cycle_id, market_id, question, category, decision, reasoning, details=None):
    """Log every decision (bet placed or skipped) for audit trail."""
    db.execute("""
        INSERT INTO sim_decisions
        (cycle_id, market_id, question, category, decision, reasoning, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        cycle_id,
        market_id,
        question,
        category,
        decision,
        reasoning,
        json.dumps(details) if details else "{}",
    ))


# ── SimulationEngine ───────────────────────────────────────────

class SimulationEngine:
    """Core simulation engine that scans, analyzes, bets, monitors, and learns."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False

    def _ensure_bankroll(self, db):
        """Ensure bankroll row exists."""
        row = db.execute("SELECT id FROM sim_bankroll LIMIT 1").fetchone()
        if not row:
            cfg = _get_sim_config(db)
            initial = cfg["sim_bankroll"]
            db.execute("""
                INSERT INTO sim_bankroll
                (balance, available, at_risk, total_deposited, total_withdrawn,
                 total_won, total_lost, total_fees, updated_at)
                VALUES (?, ?, 0, ?, 0, 0, 0, 0, datetime('now'))
            """, (initial, initial, initial))
            db.commit()

    def get_bankroll(self, db=None):
        """Return current bankroll state."""
        should_close = db is None
        if db is None:
            db = get_db()
        self._ensure_bankroll(db)
        row = db.execute("SELECT * FROM sim_bankroll ORDER BY id DESC LIMIT 1").fetchone()
        if should_close:
            db.close()
        return dict(row) if row else {}

    async def run_cycle(self) -> dict:
        """Run a full scan → analyze → decide → bet → monitor → learn cycle."""
        if self._running:
            return {"error": "Cycle already running"}

        self._running = True
        db = get_db()
        result = {
            "markets_scanned": 0,
            "opportunities_found": 0,
            "bets_placed": 0,
            "bets_skipped": 0,
            "bets_resolved": 0,
            "cycle_pnl": 0,
            "decisions": [],
        }

        try:
            self._ensure_bankroll(db)
            cfg = _get_sim_config(db)

            # Create cycle record
            cur = db.execute("""
                INSERT INTO sim_cycles (started_at, status) VALUES (datetime('now'), 'running')
            """)
            cycle_id = cur.lastrowid
            db.commit()

            # ── STEP 1: SCAN ──
            markets = await _fetch_markets_async(limit=100)
            result["markets_scanned"] = len(markets)

            # ── STEP 2: ANALYZE + DECIDE ──
            bankroll = self.get_bankroll(db)
            available = bankroll.get("available", 0)

            # Check daily budget
            today_spent = db.execute("""
                SELECT COALESCE(SUM(size), 0) FROM sim_bets
                WHERE date(placed_at) = date('now') AND status != 'cancelled'
            """).fetchone()[0]
            daily_remaining = cfg["sim_daily_budget"] - today_spent

            # Count open positions
            open_count = db.execute(
                "SELECT COUNT(*) FROM sim_bets WHERE status = 'open'"
            ).fetchone()[0]

            for m in markets:
                analysis = _analyze_market_edge(m)
                market_id = m.get("id", "")
                question = m.get("question", "")
                category = m.get("groupItemTitle", "") or m.get("category", "") or ""
                volume = float(m.get("volume", 0) or 0)
                volume_24h = float(m.get("volume24hr", 0) or 0)

                # Skip if no edge
                if analysis["edge"] < cfg["sim_min_edge"]:
                    continue

                result["opportunities_found"] += 1

                # ── DECIDE: should we bet? ──
                skip_reason = None

                if volume < cfg["sim_min_volume"]:
                    skip_reason = f"Volume ${volume:,.0f} below minimum ${cfg['sim_min_volume']:,.0f}"
                elif available < cfg["sim_min_bet"]:
                    skip_reason = "Insufficient available bankroll"
                elif daily_remaining <= 0:
                    skip_reason = "Daily budget exhausted"
                elif open_count >= cfg["sim_max_open_positions"]:
                    skip_reason = f"Max open positions ({cfg['sim_max_open_positions']}) reached"
                else:
                    # Check if strategy is in 'killing' state
                    strategy_name = f"{category.lower().replace(' ', '_')}_{analysis['signals'][0]['type']}" if analysis['signals'] else category.lower().replace(' ', '_')
                    strat_row = db.execute(
                        "SELECT status FROM sim_strategies WHERE name = ?", (strategy_name,)
                    ).fetchone()
                    if strat_row and strat_row["status"] == "killing":
                        skip_reason = f"Strategy '{strategy_name}' in 'killing' state — skipped"

                if skip_reason:
                    _log_decision(db, cycle_id, market_id, question, category,
                                  "skipped", skip_reason, {
                                      "edge": analysis["edge"],
                                      "confidence": analysis["confidence"],
                                      "volume": volume,
                                  })
                    result["bets_skipped"] += 1
                    result["decisions"].append({
                        "market": question[:80],
                        "decision": "skipped",
                        "reason": skip_reason,
                    })
                    continue

                # ── Calculate position ──
                yes_price = analysis["yes_price"]
                no_price = analysis["no_price"]

                # Choose side: bet on the side we think is underpriced
                if yes_price < 0.5:
                    side = "YES"
                    entry_price = yes_price
                    our_prob = yes_price + analysis["edge"]
                else:
                    side = "NO"
                    entry_price = no_price
                    our_prob = no_price + analysis["edge"]

                our_prob = min(0.99, max(0.01, our_prob))

                # Kelly sizing
                kelly = _kelly_size(analysis["edge"], our_prob, available, cfg["sim_kelly_fraction"])
                size = max(cfg["sim_min_bet"], min(kelly, cfg["sim_max_bet"], daily_remaining, available))

                if size < cfg["sim_min_bet"]:
                    skip_reason = f"Position size ${size:.2f} below minimum ${cfg['sim_min_bet']:.2f}"
                    _log_decision(db, cycle_id, market_id, question, category,
                                  "skipped", skip_reason, {"edge": analysis["edge"]})
                    result["bets_skipped"] += 1
                    continue

                if entry_price <= 0:
                    continue

                shares = size / entry_price
                strategy_name = f"{category.lower().replace(' ', '_')}_{analysis['signals'][0]['type']}" if analysis['signals'] else "general"
                signal_types = json.dumps([s["type"] for s in analysis["signals"]])

                reasoning_text = f"Edge {analysis['edge']*100:.1f}% detected via {', '.join(s['type'] for s in analysis['signals'])}. " \
                                 f"Market probability {entry_price*100:.1f}%, our estimate {our_prob*100:.1f}%. " \
                                 f"Kelly suggests ${kelly:.2f}, sizing to ${size:.2f} ({size/available*100:.1f}% of available)."

                # ── Place sim bet ──
                db.execute("""
                    INSERT INTO sim_bets
                    (market_id, question, category, strategy, side, entry_price, size, shares,
                     our_probability, market_probability, edge, confidence, signals, reasoning,
                     current_price, unrealized_pnl, status, cycle_id, placed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'open', ?, datetime('now'))
                """, (
                    market_id, question, category, strategy_name, side,
                    entry_price, size, shares, our_prob, entry_price,
                    analysis["edge"], analysis["confidence"], signal_types,
                    reasoning_text, entry_price, cycle_id,
                ))

                # Update bankroll
                db.execute("""
                    UPDATE sim_bankroll SET
                        available = available - ?,
                        at_risk = at_risk + ?,
                        updated_at = datetime('now')
                """, (size, size))

                # Update strategy
                db.execute("""
                    INSERT INTO sim_strategies (name, category, total_bets)
                    VALUES (?, ?, 1)
                    ON CONFLICT(name) DO UPDATE SET total_bets = total_bets + 1
                """, (strategy_name, category))

                available -= size
                daily_remaining -= size
                open_count += 1
                result["bets_placed"] += 1

                _log_decision(db, cycle_id, market_id, question, category,
                              "bet_placed", reasoning_text, {
                                  "side": side,
                                  "entry_price": entry_price,
                                  "size": size,
                                  "shares": shares,
                                  "edge": analysis["edge"],
                                  "confidence": analysis["confidence"],
                                  "strategy": strategy_name,
                              })
                result["decisions"].append({
                    "market": question[:80],
                    "decision": "bet_placed",
                    "side": side,
                    "size": size,
                    "edge": analysis["edge"],
                })

            db.commit()

            # ── STEP 3: MONITOR open positions ──
            resolved_pnl = await self._monitor_positions(db, cycle_id)
            result["bets_resolved"] = resolved_pnl["resolved_count"]
            result["cycle_pnl"] = resolved_pnl["total_pnl"]

            # ── STEP 4: LEARN ──
            if resolved_pnl["resolved_count"] > 0:
                self._update_learning(db)

            # ── Update cycle record ──
            db.execute("""
                UPDATE sim_cycles SET
                    completed_at = datetime('now'),
                    markets_scanned = ?,
                    opportunities_found = ?,
                    bets_placed = ?,
                    bets_resolved = ?,
                    cycle_pnl = ?,
                    status = 'completed'
                WHERE id = ?
            """, (
                result["markets_scanned"],
                result["opportunities_found"],
                result["bets_placed"],
                result["bets_resolved"],
                result["cycle_pnl"],
                cycle_id,
            ))

            # Record bankroll snapshot
            bankroll = self.get_bankroll(db)
            db.execute("""
                INSERT INTO sim_bankroll_history (balance, at_risk, recorded_at)
                VALUES (?, ?, datetime('now'))
            """, (bankroll.get("balance", 0), bankroll.get("at_risk", 0)))

            db.commit()
            return result

        except Exception as e:
            # Mark cycle as failed
            try:
                db.execute("""
                    UPDATE sim_cycles SET status = 'failed', completed_at = datetime('now')
                    WHERE id = ? AND status = 'running'
                """, (cycle_id,))
                db.commit()
            except Exception:
                pass
            return {"error": str(e)}
        finally:
            self._running = False
            db.close()

    async def _monitor_positions(self, db, cycle_id: int) -> dict:
        """Check all open positions, update prices, auto-resolve completed markets."""
        open_bets = db.execute(
            "SELECT * FROM sim_bets WHERE status = 'open'"
        ).fetchall()

        resolved_count = 0
        total_pnl = 0

        for bet in open_bets:
            market_id = bet["market_id"]
            market = await _fetch_market_by_id(market_id)
            if not market:
                continue

            analysis = _analyze_market_edge(market)
            current_yes = analysis["yes_price"]
            current_no = analysis["no_price"]

            side = bet["side"]
            entry_price = bet["entry_price"]
            shares = bet["shares"]
            size = bet["size"]

            if side == "YES":
                current_price = current_yes
            else:
                current_price = current_no

            # Calculate unrealized P&L
            unrealized = (current_price - entry_price) * shares

            # Check resolution conditions
            resolved = False
            outcome_price = None
            resolution_source = ""
            status = "open"

            # Condition 1: Market explicitly resolved (price very near 0 or 1)
            if current_yes >= 0.98 or current_yes <= 0.02:
                resolved = True
                outcome_price = current_price
                resolution_source = "market_resolved"
                if side == "YES":
                    status = "won" if current_yes >= 0.98 else "lost"
                else:
                    status = "won" if current_yes <= 0.02 else "lost"

            # Condition 2: End date has passed
            end_date_str = market.get("endDate", "")
            if not resolved and end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) > end_date:
                        resolved = True
                        outcome_price = current_price
                        resolution_source = "expiry"
                        # Best guess: whichever side is winning
                        if side == "YES":
                            status = "won" if current_yes > 0.5 else "lost"
                        else:
                            status = "won" if current_yes < 0.5 else "lost"
                except (ValueError, TypeError):
                    pass

            # Condition 3: Market closed/inactive
            if not resolved and (not market.get("active", True) or market.get("closed", False)):
                resolved = True
                outcome_price = current_price
                resolution_source = "market_closed"
                if side == "YES":
                    status = "won" if current_yes > 0.5 else "lost"
                else:
                    status = "won" if current_yes < 0.5 else "lost"

            if resolved:
                # Calculate realized P&L
                if status == "won":
                    # Full payout: shares * 1.0 - cost
                    realized = shares * 1.0 - size
                else:
                    # Total loss
                    realized = -size

                db.execute("""
                    UPDATE sim_bets SET
                        current_price = ?,
                        unrealized_pnl = 0,
                        realized_pnl = ?,
                        status = ?,
                        outcome_price = ?,
                        resolved_at = datetime('now'),
                        resolution_source = ?
                    WHERE id = ?
                """, (current_price, realized, status, outcome_price,
                      resolution_source, bet["id"]))

                # Update bankroll
                if status == "won":
                    payout = shares * 1.0  # Full $1 per share on win
                    db.execute("""
                        UPDATE sim_bankroll SET
                            balance = balance + ?,
                            available = available + ?,
                            at_risk = at_risk - ?,
                            total_won = total_won + ?,
                            updated_at = datetime('now')
                    """, (realized, payout, size, realized))
                else:
                    db.execute("""
                        UPDATE sim_bankroll SET
                            balance = balance - ?,
                            at_risk = at_risk - ?,
                            total_lost = total_lost + ?,
                            updated_at = datetime('now')
                    """, (size, size, size))

                # Update strategy stats
                strategy = bet["strategy"]
                if status == "won":
                    db.execute("""
                        UPDATE sim_strategies SET
                            wins = wins + 1,
                            total_pnl = total_pnl + ?
                        WHERE name = ?
                    """, (realized, strategy))
                else:
                    db.execute("""
                        UPDATE sim_strategies SET
                            losses = losses + 1,
                            total_pnl = total_pnl - ?
                        WHERE name = ?
                    """, (size, strategy))

                resolved_count += 1
                total_pnl += realized if status == "won" else -size

            else:
                # Just update current price and unrealized P&L
                db.execute("""
                    UPDATE sim_bets SET
                        current_price = ?,
                        unrealized_pnl = ?
                    WHERE id = ?
                """, (current_price, unrealized, bet["id"]))

        db.commit()
        return {"resolved_count": resolved_count, "total_pnl": total_pnl}

    def _update_learning(self, db):
        """Update learning metrics and confidence scores after resolutions."""
        # Get all resolved bets grouped by category
        categories = db.execute("""
            SELECT DISTINCT category FROM sim_bets WHERE status IN ('won', 'lost')
        """).fetchall()

        for cat_row in categories:
            category = cat_row["category"]

            stats = db.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as losses,
                    COALESCE(AVG(edge), 0) as avg_edge_predicted,
                    COALESCE(AVG(realized_pnl / NULLIF(size, 0)), 0) as avg_edge_realized,
                    COALESCE(SUM(realized_pnl), 0) as total_pnl,
                    COALESCE(SUM(size), 0) as total_invested
                FROM sim_bets
                WHERE category = ? AND status IN ('won', 'lost')
            """, (category,)).fetchone()

            total = stats["total"]
            wins = stats["wins"]
            accuracy = wins / total if total > 0 else 0
            calibration_error = abs(stats["avg_edge_predicted"] - stats["avg_edge_realized"])
            roi = stats["total_pnl"] / stats["total_invested"] if stats["total_invested"] > 0 else 0

            # Confidence = weighted accuracy, penalized by calibration error
            confidence = max(0, min(1, accuracy - calibration_error * 0.5))

            # Determine status
            if total < 5:
                status = "learning"
            elif confidence >= 0.6 and accuracy >= 0.55:
                status = "confident"
            elif accuracy < 0.40 or roi < -0.20:
                status = "unreliable"
            else:
                status = "learning"

            db.execute("""
                INSERT INTO sim_confidence
                (category, confidence_score, total_predictions, correct_predictions,
                 avg_edge_predicted, avg_edge_realized, calibration_error, roi, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(category) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    total_predictions = excluded.total_predictions,
                    correct_predictions = excluded.correct_predictions,
                    avg_edge_predicted = excluded.avg_edge_predicted,
                    avg_edge_realized = excluded.avg_edge_realized,
                    calibration_error = excluded.calibration_error,
                    roi = excluded.roi,
                    status = excluded.status,
                    updated_at = excluded.updated_at
            """, (
                category, confidence, total, wins,
                stats["avg_edge_predicted"], stats["avg_edge_realized"],
                calibration_error, roi, status,
            ))

            # Update learning metrics
            for metric_name, metric_value in [
                ("accuracy", accuracy),
                ("calibration", calibration_error),
                ("roi", roi),
            ]:
                db.execute("""
                    INSERT INTO sim_learning (category, strategy, metric_name, metric_value, sample_size, updated_at)
                    VALUES (?, '', ?, ?, ?, datetime('now'))
                    ON CONFLICT(category, strategy, metric_name) DO UPDATE SET
                        metric_value = excluded.metric_value,
                        sample_size = excluded.sample_size,
                        updated_at = excluded.updated_at
                """, (category, metric_name, metric_value, total))

        # Update strategy classifications
        strategies = db.execute("SELECT * FROM sim_strategies").fetchall()
        for s in strategies:
            total_pnl = s["total_pnl"]
            total_bets = s["total_bets"]
            invested = db.execute(
                "SELECT COALESCE(SUM(size), 1) FROM sim_bets WHERE strategy = ?",
                (s["name"],)
            ).fetchone()[0]
            roi = total_pnl / invested if invested > 0 else 0
            new_status = _classify_strategy(s["wins"], s["losses"], total_bets, roi)
            db.execute(
                "UPDATE sim_strategies SET status = ?, avg_edge = ? WHERE name = ?",
                (new_status, roi, s["name"])
            )

        db.commit()

    def reset(self, starting_bankroll: float = 10000.0):
        """Reset entire simulation — clear all bets, reset bankroll."""
        db = get_db()
        db.executescript("""
            DELETE FROM sim_bets;
            DELETE FROM sim_cycles;
            DELETE FROM sim_bankroll;
            DELETE FROM sim_bankroll_history;
            DELETE FROM sim_learning;
            DELETE FROM sim_confidence;
            DELETE FROM sim_strategies;
            DELETE FROM sim_decisions;
        """)
        db.execute("""
            INSERT INTO sim_bankroll
            (balance, available, at_risk, total_deposited, total_withdrawn,
             total_won, total_lost, total_fees, updated_at)
            VALUES (?, ?, 0, ?, 0, 0, 0, 0, datetime('now'))
        """, (starting_bankroll, starting_bankroll, starting_bankroll))
        db.commit()
        db.close()
        return {"status": "reset", "bankroll": starting_bankroll}
