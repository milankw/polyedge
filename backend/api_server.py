#!/usr/bin/env python3
"""
Polymarket Prediction Engine — Backend API Server
Scans markets, analyzes opportunities, tracks positions & P&L.
"""

import asyncio
import json
import sqlite3
import time
import math
import hashlib
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ── Database Setup ──────────────────────────────────────────────
import os
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            category TEXT DEFAULT '',
            end_date TEXT,
            outcome_yes_price REAL,
            outcome_no_price REAL,
            volume REAL DEFAULT 0,
            liquidity REAL DEFAULT 0,
            image_url TEXT DEFAULT '',
            slug TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            signal_type TEXT,
            our_probability REAL,
            market_probability REAL,
            edge REAL,
            confidence TEXT,
            reasoning TEXT,
            category TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'active',
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            side TEXT,
            entry_price REAL,
            size REAL,
            current_price REAL,
            pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            strategy TEXT DEFAULT '',
            opened_at TEXT DEFAULT (datetime('now')),
            closed_at TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        CREATE TABLE IF NOT EXISTS pnl_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            total_pnl REAL,
            open_positions INTEGER,
            win_rate REAL,
            total_bets INTEGER
        );
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            category TEXT,
            total_bets INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            avg_edge REAL DEFAULT 0,
            status TEXT DEFAULT 'exploring',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- Simulation tables
        CREATE TABLE IF NOT EXISTS sim_bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            balance REAL DEFAULT 10000,
            available REAL DEFAULT 10000,
            at_risk REAL DEFAULT 0,
            total_deposited REAL DEFAULT 10000,
            total_withdrawn REAL DEFAULT 0,
            total_won REAL DEFAULT 0,
            total_lost REAL DEFAULT 0,
            total_fees REAL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sim_bankroll_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            balance REAL,
            at_risk REAL DEFAULT 0,
            recorded_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sim_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            question TEXT,
            category TEXT DEFAULT '',
            strategy TEXT DEFAULT '',
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            size REAL NOT NULL,
            shares REAL NOT NULL,
            our_probability REAL,
            market_probability REAL,
            edge REAL,
            confidence TEXT DEFAULT 'low',
            signals TEXT DEFAULT '[]',
            reasoning TEXT DEFAULT '',
            current_price REAL,
            unrealized_pnl REAL DEFAULT 0,
            realized_pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            outcome_price REAL,
            placed_at TEXT DEFAULT (datetime('now')),
            resolved_at TEXT,
            resolution_source TEXT DEFAULT '',
            cycle_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS sim_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            markets_scanned INTEGER DEFAULT 0,
            opportunities_found INTEGER DEFAULT 0,
            bets_placed INTEGER DEFAULT 0,
            bets_resolved INTEGER DEFAULT 0,
            cycle_pnl REAL DEFAULT 0,
            status TEXT DEFAULT 'running'
        );

        CREATE TABLE IF NOT EXISTS sim_learning (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            strategy TEXT,
            metric_name TEXT,
            metric_value REAL,
            sample_size INTEGER,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(category, strategy, metric_name)
        );

        CREATE TABLE IF NOT EXISTS sim_confidence (
            category TEXT PRIMARY KEY,
            confidence_score REAL DEFAULT 0.5,
            total_predictions INTEGER DEFAULT 0,
            correct_predictions INTEGER DEFAULT 0,
            avg_edge_predicted REAL DEFAULT 0,
            avg_edge_realized REAL DEFAULT 0,
            calibration_error REAL DEFAULT 0,
            roi REAL DEFAULT 0,
            status TEXT DEFAULT 'learning',
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sim_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            category TEXT DEFAULT '',
            total_bets INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            avg_edge REAL DEFAULT 0,
            status TEXT DEFAULT 'exploring',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sim_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            market_id TEXT,
            question TEXT,
            category TEXT DEFAULT '',
            decision TEXT,
            reasoning TEXT,
            details TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # Seed default config
    defaults = {
        "max_bet_size": "5.00",
        "daily_budget": "50.00",
        "min_edge": "0.05",
        "min_volume": "1000",
        "min_liquidity": "500",
        "risk_level": "conservative",
        "auto_trade": "false",
        "polymarket_key": "",
        "polymarket_secret": "",
        "polymarket_passphrase": "",
        "wallet_address": "",
    }
    sim_defaults = {
        "sim_bankroll": "10000",
        "sim_max_bet": "200",
        "sim_min_bet": "5",
        "sim_daily_budget": "500",
        "sim_min_edge": "0.03",
        "sim_min_volume": "500",
        "sim_max_open_positions": "50",
        "sim_kelly_fraction": "0.25",
        "sim_auto_cycle": "false",
        "sim_cycle_interval": "1800",
    }
    defaults.update(sim_defaults)
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
    db.commit()
    db.close()

init_db()

# ── Polymarket API Client ────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

async def fetch_markets(limit=100, offset=0, active=True, category=None):
    """Fetch markets from Gamma API."""
    params = {
        "limit": limit,
        "offset": offset,
        "active": str(active).lower(),
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    if category:
        params["tag_slug"] = category
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GAMMA_API}/markets", params=params)
        if resp.status_code == 200:
            return resp.json()
    return []

async def fetch_events(limit=50, active=True):
    """Fetch events from Gamma API."""
    params = {
        "limit": limit,
        "active": str(active).lower(),
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GAMMA_API}/events", params=params)
        if resp.status_code == 200:
            return resp.json()
    return []

async def fetch_tags():
    """Fetch available tags/categories."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GAMMA_API}/tags")
        if resp.status_code == 200:
            return resp.json()
    return []

async def fetch_orderbook(token_id: str):
    """Fetch orderbook from CLOB API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        if resp.status_code == 200:
            return resp.json()
    return {}

async def fetch_price_history(token_id: str, fidelity: int = 60):
    """Fetch price history from CLOB API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{CLOB_API}/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": fidelity}
        )
        if resp.status_code == 200:
            return resp.json()
    return {}

async def fetch_spread(token_id: str):
    """Fetch spread from CLOB API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CLOB_API}/spread", params={"token_id": token_id})
        if resp.status_code == 200:
            return resp.json()
    return {}

# ── Analysis Engine ──────────────────────────────────────────────

def analyze_market_edge(market: dict) -> dict:
    """
    Core analysis: detect mispricing using multiple signals.
    Returns edge estimate and confidence level.
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
        return {"edge": 0, "confidence": "none", "signals": []}

    # Signal 1: Arbitrage check (YES + NO should = 1.00)
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
            "detail": f"Market at {yes_price:.0%} with ${volume:,.0f} volume — near-certain outcome may still have small edge"
        })

    # Signal 3: Low-liquidity mispricing
    liquidity = float(market.get("liquidityClob", 0) or 0)
    if liquidity < 2000 and volume_24h > 500 and 0.15 < yes_price < 0.85:
        signals.append({
            "type": "low_liquidity",
            "edge": 0.08,
            "detail": f"Thin book (${liquidity:,.0f}) with activity — likely mispriced"
        })

    # Signal 4: Volume surge detection
    if volume_24h > 0 and volume > 0:
        volume_ratio = volume_24h / max(volume / 30, 1)  # crude daily average
        if volume_ratio > 3:
            signals.append({
                "type": "volume_surge",
                "edge": 0.05,
                "detail": f"24h volume {volume_ratio:.1f}x daily average — information event"
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
                    "detail": f"Resolves in {days_to_end:.1f} days — convergence trade opportunity"
                })
        except (ValueError, TypeError):
            pass

    # Aggregate edge
    if not signals:
        return {"edge": 0, "confidence": "none", "signals": []}

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


# ── FastAPI App ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Market Endpoints ─────────────────────────────────────────────

@app.get("/api/markets")
async def get_markets(
    limit: int = Query(50, le=100),
    offset: int = 0,
    category: Optional[str] = None,
):
    """Fetch and analyze live Polymarket markets."""
    markets = await fetch_markets(limit=limit, offset=offset, category=category)
    db = get_db()
    results = []

    for m in markets:
        analysis = analyze_market_edge(m)
        market_data = {
            "id": m.get("id", ""),
            "question": m.get("question", ""),
            "category": m.get("groupItemTitle", "") or "",
            "slug": m.get("slug", ""),
            "image": m.get("image", ""),
            "yes_price": analysis.get("yes_price", 0),
            "no_price": analysis.get("no_price", 0),
            "volume": float(m.get("volume", 0) or 0),
            "volume_24h": float(m.get("volume24hr", 0) or 0),
            "liquidity": float(m.get("liquidityClob", 0) or 0),
            "end_date": m.get("endDate", ""),
            "edge": analysis["edge"],
            "confidence": analysis["confidence"],
            "signals": analysis["signals"],
            "token_ids": [m.get("clobTokenIds", ["", ""])]
        }
        results.append(market_data)

        # Cache to DB
        try:
            db.execute("""
                INSERT OR REPLACE INTO markets
                (id, question, category, end_date, outcome_yes_price, outcome_no_price,
                 volume, liquidity, image_url, slug, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
            """, (
                market_data["id"], market_data["question"], market_data["category"],
                market_data["end_date"], market_data["yes_price"], market_data["no_price"],
                market_data["volume"], market_data["liquidity"], market_data["image"],
                market_data["slug"]
            ))
        except Exception:
            pass

    db.commit()
    db.close()

    # Sort by edge
    results.sort(key=lambda x: x["edge"], reverse=True)
    return {"markets": results, "total": len(results)}


@app.get("/api/markets/scan")
async def scan_opportunities(
    min_edge: float = Query(0.03),
    min_volume: float = Query(500),
    limit: int = Query(100),
):
    """Scan for the best opportunities across all markets."""
    markets = await fetch_markets(limit=limit)
    opportunities = []

    for m in markets:
        analysis = analyze_market_edge(m)
        volume = float(m.get("volume", 0) or 0)

        if analysis["edge"] >= min_edge and volume >= min_volume:
            opp = {
                "market_id": m.get("id", ""),
                "question": m.get("question", ""),
                "category": m.get("groupItemTitle", "") or "",
                "edge": analysis["edge"],
                "confidence": analysis["confidence"],
                "signals": analysis["signals"],
                "yes_price": analysis.get("yes_price", 0),
                "no_price": analysis.get("no_price", 0),
                "volume": volume,
                "volume_24h": float(m.get("volume24hr", 0) or 0),
                "liquidity": float(m.get("liquidityClob", 0) or 0),
                "recommended_side": "YES" if analysis.get("yes_price", 0) < 0.5 else "NO",
                "recommended_size": min(5.0, max(1.0, analysis["edge"] * 50)),
            }
            opportunities.append(opp)

    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    return {"opportunities": opportunities, "total": len(opportunities), "scanned": len(markets)}


@app.get("/api/tags")
async def get_tags():
    """Get available market categories."""
    tags = await fetch_tags()
    return {"tags": tags}


@app.get("/api/market/{market_id}/details")
async def get_market_details(market_id: str):
    """Get detailed market data including orderbook and price history."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{GAMMA_API}/markets/{market_id}")
        if resp.status_code != 200:
            return {"error": "Market not found"}
        market = resp.json()

    analysis = analyze_market_edge(market)

    # Try to get token IDs for orderbook
    clob_ids = market.get("clobTokenIds", [])
    orderbook = {}
    price_history = {}
    spread = {}

    if clob_ids and len(clob_ids) > 0:
        token_id = clob_ids[0]
        try:
            orderbook = await fetch_orderbook(token_id)
            spread = await fetch_spread(token_id)
            price_history = await fetch_price_history(token_id)
        except Exception:
            pass

    return {
        "market": market,
        "analysis": analysis,
        "orderbook": orderbook,
        "spread": spread,
        "price_history": price_history,
    }


# ── Position Tracking ────────────────────────────────────────────

class PositionCreate(BaseModel):
    market_id: str
    side: str
    entry_price: float
    size: float
    strategy: str = "manual"

class PositionUpdate(BaseModel):
    current_price: Optional[float] = None
    status: Optional[str] = None

@app.get("/api/positions")
def get_positions(status: str = "open"):
    db = get_db()
    rows = db.execute(
        "SELECT p.*, m.question, m.category FROM positions p LEFT JOIN markets m ON p.market_id = m.id WHERE p.status = ? ORDER BY p.opened_at DESC",
        (status,)
    ).fetchall()
    db.close()
    return {"positions": [dict(r) for r in rows]}

@app.post("/api/positions")
def create_position(pos: PositionCreate):
    db = get_db()
    cur = db.execute(
        "INSERT INTO positions (market_id, side, entry_price, size, current_price, strategy) VALUES (?, ?, ?, ?, ?, ?)",
        (pos.market_id, pos.side, pos.entry_price, pos.size, pos.entry_price, pos.strategy)
    )
    # Update strategy stats
    db.execute("""
        INSERT INTO strategies (name, category, total_bets) VALUES (?, '', 1)
        ON CONFLICT(name) DO UPDATE SET total_bets = total_bets + 1
    """, (pos.strategy,))
    db.commit()
    position_id = cur.lastrowid
    db.close()
    return {"id": position_id, "status": "created"}

@app.patch("/api/positions/{position_id}")
def update_position(position_id: int, update: PositionUpdate):
    db = get_db()
    pos = db.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    if not pos:
        db.close()
        return {"error": "Not found"}

    if update.current_price is not None:
        pnl = (update.current_price - pos["entry_price"]) * pos["size"]
        if pos["side"] == "NO":
            pnl = -pnl
        db.execute(
            "UPDATE positions SET current_price = ?, pnl = ? WHERE id = ?",
            (update.current_price, pnl, position_id)
        )

    if update.status:
        db.execute(
            "UPDATE positions SET status = ?, closed_at = datetime('now') WHERE id = ?",
            (update.status, position_id)
        )
        # Update strategy stats on close
        if update.status in ("won", "lost"):
            strategy = pos["strategy"]
            if update.status == "won":
                db.execute(
                    "UPDATE strategies SET wins = wins + 1 WHERE name = ?", (strategy,)
                )
            else:
                db.execute(
                    "UPDATE strategies SET losses = losses + 1 WHERE name = ?", (strategy,)
                )

    db.commit()
    db.close()
    return {"status": "updated"}

# ── P&L and Analytics ────────────────────────────────────────────

@app.get("/api/analytics/summary")
def get_analytics_summary():
    db = get_db()
    open_pos = db.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(size), 0) FROM positions WHERE status = 'open'").fetchone()
    closed = db.execute("SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM positions WHERE status IN ('won', 'lost')").fetchone()
    wins = db.execute("SELECT COUNT(*) FROM positions WHERE status = 'won'").fetchone()[0]
    losses = db.execute("SELECT COUNT(*) FROM positions WHERE status = 'lost'").fetchone()[0]
    total_closed = wins + losses

    # Strategy performance
    strategies = db.execute("SELECT * FROM strategies ORDER BY total_pnl DESC").fetchall()

    # Daily P&L (last 30 days)
    daily_pnl = db.execute("""
        SELECT date(opened_at) as day, SUM(pnl) as daily_pnl, COUNT(*) as bets
        FROM positions
        WHERE opened_at > datetime('now', '-30 days')
        GROUP BY date(opened_at)
        ORDER BY day
    """).fetchall()

    db.close()

    return {
        "open_positions": open_pos[0],
        "unrealized_pnl": round(open_pos[1], 2),
        "total_exposure": round(open_pos[2], 2),
        "closed_positions": closed[0],
        "realized_pnl": round(closed[1], 2),
        "total_pnl": round(open_pos[1] + closed[1], 2),
        "win_rate": round(wins / total_closed * 100, 1) if total_closed > 0 else 0,
        "wins": wins,
        "losses": losses,
        "strategies": [dict(s) for s in strategies],
        "daily_pnl": [dict(d) for d in daily_pnl],
    }

@app.get("/api/analytics/strategies")
def get_strategies():
    db = get_db()
    strategies = db.execute("""
        SELECT s.*,
            CASE WHEN s.total_bets > 0 THEN ROUND(s.wins * 100.0 / s.total_bets, 1) ELSE 0 END as win_rate
        FROM strategies s
        ORDER BY s.total_pnl DESC
    """).fetchall()
    db.close()
    return {"strategies": [dict(s) for s in strategies]}


# ── Config ────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    db = get_db()
    rows = db.execute("SELECT key, value FROM config").fetchall()
    db.close()
    # Mask sensitive values
    config = {}
    for r in rows:
        key = r["key"]
        val = r["value"]
        if key in ("polymarket_key", "polymarket_secret", "polymarket_passphrase", "wallet_address"):
            config[key] = val[:8] + "..." if len(val) > 8 else val
        else:
            config[key] = val
    return {"config": config}

class ConfigUpdate(BaseModel):
    key: str
    value: str

@app.post("/api/config")
def update_config(update: ConfigUpdate):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (update.key, update.value))
    db.commit()
    db.close()
    return {"status": "updated"}


# ── Health ────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "version": "2.0.0"
    }


# ── Simulation Endpoints ──────────────────────────────────────────

from simulator import SimulationEngine

sim_engine = SimulationEngine()


@app.get("/api/sim/status")
def sim_status():
    """Current simulation state: bankroll, active bets, last cycle."""
    db = get_db()
    sim_engine._ensure_bankroll(db)
    bankroll = sim_engine.get_bankroll(db)
    active_bets = db.execute("SELECT COUNT(*) FROM sim_bets WHERE status = 'open'").fetchone()[0]
    total_bets = db.execute("SELECT COUNT(*) FROM sim_bets").fetchone()[0]
    last_cycle = db.execute(
        "SELECT * FROM sim_cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # Calculate total P&L
    pnl_row = db.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN realized_pnl ELSE 0 END), 0) as realized,
            COALESCE(SUM(CASE WHEN status = 'open' THEN unrealized_pnl ELSE 0 END), 0) as unrealized
        FROM sim_bets
    """).fetchone()

    wins = db.execute("SELECT COUNT(*) FROM sim_bets WHERE status = 'won'").fetchone()[0]
    losses = db.execute("SELECT COUNT(*) FROM sim_bets WHERE status = 'lost'").fetchone()[0]
    total_closed = wins + losses

    db.close()
    return {
        "bankroll": bankroll,
        "active_bets": active_bets,
        "total_bets": total_bets,
        "realized_pnl": round(pnl_row["realized"], 2),
        "unrealized_pnl": round(pnl_row["unrealized"], 2),
        "total_pnl": round(pnl_row["realized"] + pnl_row["unrealized"], 2),
        "win_rate": round(wins / total_closed * 100, 1) if total_closed > 0 else 0,
        "wins": wins,
        "losses": losses,
        "last_cycle": dict(last_cycle) if last_cycle else None,
        "engine_running": sim_engine._running,
    }


@app.get("/api/sim/bankroll")
def sim_bankroll():
    """Detailed bankroll with history."""
    db = get_db()
    sim_engine._ensure_bankroll(db)
    bankroll = sim_engine.get_bankroll(db)
    history = db.execute(
        "SELECT balance, at_risk, recorded_at FROM sim_bankroll_history ORDER BY recorded_at ASC"
    ).fetchall()
    db.close()
    return {
        "bankroll": bankroll,
        "history": [dict(h) for h in history],
    }


@app.get("/api/sim/bets")
def sim_bets(
    status: str = Query("all"),
    category: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """List sim bets with filtering."""
    db = get_db()
    where_clauses = []
    params = []

    if status != "all":
        where_clauses.append("status = ?")
        params.append(status)
    if category:
        where_clauses.append("category = ?")
        params.append(category)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    rows = db.execute(
        f"SELECT * FROM sim_bets WHERE {where_sql} ORDER BY placed_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM sim_bets WHERE {where_sql}", params).fetchone()[0]
    db.close()
    result = []
    for r in rows:
        d = dict(r)
        # Parse signals JSON
        try:
            d["signals"] = json.loads(d.get("signals", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["signals"] = []
        result.append(d)
    return {"bets": result, "total": total}


@app.get("/api/sim/bets/{bet_id}")
def sim_bet_detail(bet_id: int):
    """Single bet detail with full reasoning."""
    db = get_db()
    row = db.execute("SELECT * FROM sim_bets WHERE id = ?", (bet_id,)).fetchone()
    db.close()
    if not row:
        return {"error": "Bet not found"}
    d = dict(row)
    try:
        d["signals"] = json.loads(d.get("signals", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["signals"] = []
    return {"bet": d}


@app.post("/api/sim/run-cycle")
async def sim_run_cycle():
    """Manually trigger a simulation cycle."""
    result = await sim_engine.run_cycle()
    return result


@app.get("/api/sim/cycles")
def sim_cycles(limit: int = Query(50, le=200)):
    """List past cycles."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sim_cycles ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    db.close()
    return {"cycles": [dict(r) for r in rows]}


@app.get("/api/sim/performance")
def sim_performance():
    """Overall performance: ROI, Sharpe, drawdown, win rate."""
    db = get_db()
    sim_engine._ensure_bankroll(db)
    bankroll = sim_engine.get_bankroll(db)
    initial_balance = bankroll.get("total_deposited", 10000)
    current_balance = bankroll.get("balance", 10000)

    # Basic stats
    stats = db.execute("""
        SELECT
            COUNT(*) as total_bets,
            SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(CASE WHEN status IN ('won','lost') THEN realized_pnl ELSE 0 END), 0) as realized_pnl,
            COALESCE(SUM(CASE WHEN status = 'open' THEN unrealized_pnl ELSE 0 END), 0) as unrealized_pnl,
            COALESCE(SUM(size), 0) as total_invested,
            COALESCE(AVG(CASE WHEN status IN ('won','lost') THEN realized_pnl ELSE NULL END), 0) as avg_pnl,
            COALESCE(MAX(CASE WHEN status = 'won' THEN realized_pnl ELSE 0 END), 0) as best_trade,
            COALESCE(MIN(CASE WHEN status = 'lost' THEN realized_pnl ELSE 0 END), 0) as worst_trade
        FROM sim_bets
    """).fetchone()

    total_closed = (stats["wins"] or 0) + (stats["losses"] or 0)
    total_pnl = (stats["realized_pnl"] or 0) + (stats["unrealized_pnl"] or 0)
    roi = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0
    win_rate = ((stats["wins"] or 0) / total_closed * 100) if total_closed > 0 else 0

    # Sharpe ratio (simplified from daily returns)
    daily_returns = db.execute("""
        SELECT date(placed_at) as day,
            SUM(CASE WHEN status IN ('won','lost') THEN realized_pnl ELSE unrealized_pnl END) as daily_pnl
        FROM sim_bets
        WHERE placed_at IS NOT NULL
        GROUP BY date(placed_at)
        ORDER BY day
    """).fetchall()

    sharpe = 0
    if len(daily_returns) >= 2:
        returns = [r["daily_pnl"] or 0 for r in daily_returns]
        avg_ret = sum(returns) / len(returns)
        std_ret = (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        if std_ret > 0:
            sharpe = round((avg_ret / std_ret) * (252 ** 0.5), 2)  # Annualized

    # Max drawdown from bankroll history
    history = db.execute(
        "SELECT balance FROM sim_bankroll_history ORDER BY recorded_at ASC"
    ).fetchall()

    max_drawdown = 0
    peak = initial_balance
    for h in history:
        bal = h["balance"]
        if bal > peak:
            peak = bal
        dd = (peak - bal) / peak if peak > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

    # Profit factor
    gross_profit = db.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM sim_bets WHERE status = 'won'"
    ).fetchone()[0]
    gross_loss = abs(db.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM sim_bets WHERE status = 'lost'"
    ).fetchone()[0])
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0

    db.close()
    return {
        "total_bets": stats["total_bets"],
        "total_closed": total_closed,
        "wins": stats["wins"] or 0,
        "losses": stats["losses"] or 0,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "realized_pnl": round(stats["realized_pnl"] or 0, 2),
        "unrealized_pnl": round(stats["unrealized_pnl"] or 0, 2),
        "roi": round(roi, 2),
        "sharpe_ratio": sharpe,
        "max_drawdown": round(max_drawdown * 100, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_pnl_per_trade": round(stats["avg_pnl"] or 0, 2),
        "best_trade": round(stats["best_trade"] or 0, 2),
        "worst_trade": round(stats["worst_trade"] or 0, 2),
        "total_invested": round(stats["total_invested"] or 0, 2),
        "current_balance": round(current_balance, 2),
        "initial_balance": round(initial_balance, 2),
    }


@app.get("/api/sim/learning")
def sim_learning():
    """Learning metrics: accuracy per category, calibration."""
    db = get_db()
    metrics = db.execute(
        "SELECT * FROM sim_learning ORDER BY category, metric_name"
    ).fetchall()
    confidence = db.execute(
        "SELECT * FROM sim_confidence ORDER BY confidence_score DESC"
    ).fetchall()
    db.close()
    return {
        "metrics": [dict(m) for m in metrics],
        "confidence": [dict(c) for c in confidence],
    }


@app.get("/api/sim/confidence")
def sim_confidence():
    """Confidence scores per category."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM sim_confidence ORDER BY confidence_score DESC"
    ).fetchall()
    db.close()
    return {"categories": [dict(r) for r in rows]}


@app.post("/api/sim/reset")
def sim_reset():
    """Reset simulation."""
    db = get_db()
    # Get starting bankroll from config
    cfg_row = db.execute("SELECT value FROM config WHERE key = 'sim_bankroll'").fetchone()
    starting = float(cfg_row["value"]) if cfg_row else 10000.0
    db.close()
    result = sim_engine.reset(starting)
    return result


@app.get("/api/sim/decisions")
def sim_decisions(
    limit: int = Query(100, le=500),
    cycle_id: Optional[int] = None,
):
    """Decision log with reasoning."""
    db = get_db()
    if cycle_id:
        rows = db.execute(
            "SELECT * FROM sim_decisions WHERE cycle_id = ? ORDER BY id DESC LIMIT ?",
            (cycle_id, limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM sim_decisions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    db.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d.get("details", "{}"))
        except (json.JSONDecodeError, TypeError):
            d["details"] = {}
        result.append(d)
    return {"decisions": result}


@app.get("/api/sim/strategies")
def sim_strategies_list():
    """Sim strategy performance."""
    db = get_db()
    rows = db.execute("""
        SELECT s.*,
            CASE WHEN s.total_bets > 0 THEN ROUND(s.wins * 100.0 / s.total_bets, 1) ELSE 0 END as win_rate
        FROM sim_strategies s
        ORDER BY s.total_pnl DESC
    """).fetchall()
    db.close()
    return {"strategies": [dict(r) for r in rows]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
