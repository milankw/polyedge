"""
PolyEdge — FastAPI server for the copy trading speed engine.
Auto-starts the polling loop on boot. Minimal endpoints.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.engine import CopyEngine, DB_PATH
from backend.scorer import score_trade_full, wallet_tier as compute_wallet_tier
from backend.scout import WalletScout
from backend.seed import seed

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("polyedge.app")

engine = CopyEngine(db_path=DB_PATH)
scout = WalletScout(db_path=DB_PATH)

FILTER_META = [
    {"key": "min_liquidity_usd", "label": "Minimum Liquidity Pool", "description": "Only copy trades in markets where total liquidity is at or above this value.", "unit": "USD", "input_type": "number", "group": "market"},
    {"key": "entry_timing_minutes", "label": "Entry Timing Window", "description": "Reject trades older than this many minutes since the position was opened.", "unit": "minutes", "input_type": "number", "group": "timing"},
    {"key": "max_wallet_pool_pct", "label": "Max Wallet Concentration", "description": "Reject if the wallet controls more than this % of the market liquidity pool.", "unit": "%", "input_type": "number", "group": "risk"},
    {"key": "min_volume_24h_usd", "label": "Minimum 24H Volume", "description": "Only copy trades in markets with at least this much 24-hour trading volume.", "unit": "USD", "input_type": "number", "group": "market"},
    {"key": "min_unique_traders", "label": "Minimum Unique Traders", "description": "Reject markets with fewer unique traders than this threshold.", "unit": "traders", "input_type": "number", "group": "market"},
    {"key": "min_wallet_win_rate", "label": "Min Wallet Win Rate", "description": "Only copy wallets with a historical win rate at or above this percentage.", "unit": "%", "input_type": "number", "group": "wallet"},
    {"key": "min_resolved_markets", "label": "Min Resolved Markets", "description": "Only copy wallets that have traded in at least this many resolved markets.", "unit": "markets", "input_type": "number", "group": "wallet"},
    {"key": "max_bankroll_exposure_pct", "label": "Max Bankroll Exposure", "description": "Hard cap: reject if a single trade would exceed this % of your bankroll.", "unit": "%", "input_type": "number", "group": "risk"},
]


async def _price_refresh_loop():
    """Background task: refresh open position prices every 60s."""
    while True:
        try:
            await asyncio.sleep(60)
            await engine.refresh_prices()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Price refresh error: {e}")


async def _wallet_score_loop():
    """Background task: recompute wallet scores every 5 minutes."""
    while True:
        try:
            await asyncio.sleep(300)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row

                # Fetch all active wallets
                async with db.execute(
                    "SELECT id, address, csv_win_rate, csv_unique_markets FROM wallets WHERE is_active = 1"
                ) as cursor:
                    wallets = [dict(r) for r in await cursor.fetchall()]

                for w in wallets:
                    wid = w["id"]
                    win_rate = float(w["csv_win_rate"] or 0)
                    resolved = int(w["csv_unique_markets"] or 0)

                    # Get averages from recent passed trades for market-specific sub-scores
                    async with db.execute(
                        """SELECT
                            AVG(CAST(fr_liq.actual_value AS REAL)) as avg_liq,
                            AVG(CAST(fr_vol.actual_value AS REAL)) as avg_vol,
                            AVG(CAST(fr_uniq.actual_value AS REAL)) as avg_uniq
                        FROM copy_feed cf
                        LEFT JOIN filter_results fr_liq ON fr_liq.tx_hash = cf.tx_hash AND fr_liq.filter_name = 'min_liquidity'
                        LEFT JOIN filter_results fr_vol ON fr_vol.tx_hash = cf.tx_hash AND fr_vol.filter_name = 'min_volume_24h'
                        LEFT JOIN filter_results fr_uniq ON fr_uniq.tx_hash = cf.tx_hash AND fr_uniq.filter_name = 'min_unique_traders'
                        WHERE cf.wallet_id = ? AND cf.filter_verdict = 'passed'
                        ORDER BY cf.id DESC LIMIT 50""",
                        (wid,),
                    ) as cursor:
                        row = await cursor.fetchone()
                        avg_liq = float(row["avg_liq"]) if row and row["avg_liq"] else 100000.0
                        avg_vol = float(row["avg_vol"]) if row and row["avg_vol"] else 50000.0
                        avg_uniq = int(float(row["avg_uniq"])) if row and row["avg_uniq"] else 100

                    # Compute wallet score using score_trade_full with mid-range defaults for non-wallet metrics
                    result = score_trade_full(
                        liquidity_usd=avg_liq,
                        minutes_since_open=0,
                        wallet_share_pct=0,
                        volume_24h_usd=avg_vol,
                        unique_traders=avg_uniq,
                        win_rate_pct=win_rate,
                        resolved_markets=resolved,
                        exposure_pct=0,
                    )
                    ws = result["score_wallet"]
                    wt = result["wallet_tier"]

                    await db.execute(
                        "UPDATE wallets SET wallet_score = ?, wallet_tier = ? WHERE id = ?",
                        (ws, wt, wid),
                    )

                await db.commit()
                logger.info(f"Wallet scores updated for {len(wallets)} wallets")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Wallet score loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run seed to ensure tables exist
    seed()
    # Auto-start the copy engine loop
    engine.loop_task = asyncio.create_task(engine.start_loop())
    # Start price refresh background task
    price_task = asyncio.create_task(_price_refresh_loop())
    # Start wallet score background task
    wallet_score_task = asyncio.create_task(_wallet_score_loop())
    # Auto-start the wallet scout
    scout._task = asyncio.create_task(scout.start_loop())
    logger.info("PolyEdge copy engine + scout started")
    yield
    # Shutdown
    engine.stop_loop()
    scout.stop_loop()
    price_task.cancel()
    wallet_score_task.cancel()
    await engine.close()
    await scout.close()
    logger.info("PolyEdge copy engine + scout stopped")


app = FastAPI(title="PolyEdge", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helper ────────────────────────────────────────────────

async def _query(sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def _execute(sql: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


# ── Health ────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    status = engine.get_status()
    return {
        "status": "ok",
        "loop_running": status["running"],
        "cycle_count": status["cycle_count"],
        "last_cycle_ms": status["last_cycle_ms"],
        "worst_cycle_ms": status["worst_cycle_ms"],
    }


# ── Feed ──────────────────────────────────────────────────

@app.get("/api/feed")
async def get_feed(limit: int = Query(100, ge=1, le=500), mode: str = Query(None)):
    if mode:
        rows = await _query(
            "SELECT * FROM copy_feed WHERE mode_id = ? ORDER BY id DESC LIMIT ?",
            (mode, limit),
        )
    else:
        rows = await _query(
            "SELECT * FROM copy_feed ORDER BY id DESC LIMIT ?", (limit,)
        )
    # Attach filter_results to each feed item
    if rows:
        tx_hashes = [r["tx_hash"] for r in rows if r.get("tx_hash")]
        if tx_hashes:
            placeholders = ",".join("?" for _ in tx_hashes)
            if mode:
                filters = await _query(
                    f"SELECT * FROM filter_results WHERE tx_hash IN ({placeholders}) AND mode_id = ?",
                    tuple(tx_hashes) + (mode,),
                )
            else:
                filters = await _query(
                    f"SELECT * FROM filter_results WHERE tx_hash IN ({placeholders})",
                    tuple(tx_hashes),
                )
            # Group by tx_hash
            filter_map: dict[str, list] = {}
            for f in filters:
                filter_map.setdefault(f["tx_hash"], []).append(f)
            for row in rows:
                row["filter_results"] = filter_map.get(row["tx_hash"], [])
    return rows


# ── Loop Control ──────────────────────────────────────────

@app.post("/api/loop/start")
async def loop_start():
    if engine._loop_running:
        return {"message": "Loop already running"}
    engine.loop_task = asyncio.create_task(engine.start_loop())
    return {"message": "Loop started"}


@app.post("/api/loop/stop")
async def loop_stop():
    engine.stop_loop()
    return {"message": "Loop stopped"}


@app.get("/api/loop/status")
async def loop_status():
    return engine.get_status()


# ── P&L Summary ──────────────────────────────────────────

@app.get("/api/pnl")
async def pnl_summary(mode: str = Query(None)):
    mode_filter = " AND mode_id = ?" if mode else ""
    mode_params = (mode,) if mode else ()

    # Closed positions stats
    closed = await _query(
        f"""SELECT
            COUNT(*) as closed_count,
            COALESCE(SUM(gross_pnl), 0) as total_gross,
            COALESCE(SUM(poly_fee), 0) as total_fees,
            COALESCE(SUM(slippage), 0) as total_slippage,
            COALESCE(SUM(net_pnl), 0) as total_net,
            COALESCE(AVG(CASE WHEN exit_delay_ms IS NOT NULL THEN exit_delay_ms END), 0) as avg_delay_ms,
            COALESCE(MAX(CASE WHEN exit_delay_ms IS NOT NULL THEN exit_delay_ms END), 0) as worst_delay_ms
        FROM shadow_positions WHERE status = 'closed'{mode_filter}""",
        mode_params,
    )
    c = closed[0] if closed else {}

    # Open positions stats
    open_pos = await _query(
        f"""SELECT
            COUNT(*) as open_count,
            COALESCE(SUM(net_pnl), 0) as open_pnl,
            COALESCE(AVG(CASE WHEN entry_delay_ms IS NOT NULL THEN entry_delay_ms END), 0) as avg_entry_delay
        FROM shadow_positions WHERE status = 'open'{mode_filter}""",
        mode_params,
    )
    o = open_pos[0] if open_pos else {}

    # Win rate
    wins = await _query(
        f"SELECT COUNT(*) as cnt FROM shadow_positions WHERE status = 'closed' AND net_pnl > 0{mode_filter}",
        mode_params,
    )
    win_count = wins[0]["cnt"] if wins else 0
    closed_count = c.get("closed_count", 0)
    win_rate = (win_count / closed_count * 100) if closed_count > 0 else 0

    # Feed-based delay stats (more granular)
    feed_mode_filter = " AND mode_id = ?" if mode else ""
    feed_delay = await _query(
        f"""SELECT
            COALESCE(AVG(delay_ms), 0) as avg_delay_ms,
            COALESCE(MAX(delay_ms), 0) as worst_delay_ms
        FROM copy_feed WHERE delay_ms IS NOT NULL{feed_mode_filter}""",
        mode_params,
    )
    fd = feed_delay[0] if feed_delay else {}

    total_net = c.get("total_net", 0) + o.get("open_pnl", 0)

    # ── Budget breakdown ──
    if mode:
        bankroll_rows = await _query(
            "SELECT bankroll_usd FROM strategy_modes WHERE mode_id = ?", (mode,)
        )
        bankroll = bankroll_rows[0]["bankroll_usd"] if bankroll_rows else 1000.0
    else:
        settings = await _query(
            "SELECT key, value FROM copy_trade_settings WHERE key = 'bankroll_usd'"
        )
        bankroll = float(settings[0]["value"]) if settings else 1000.0

    settings = await _query(
        "SELECT key, value FROM copy_trade_settings WHERE key IN ('max_bankroll_pct', 'min_trade_usd', 'max_trade_usd')"
    )
    max_pct = 10.0
    min_trade = 5.0
    max_trade = 100.0
    for row in settings:
        if row["key"] == "max_bankroll_pct":
            max_pct = float(row["value"])
        elif row["key"] == "min_trade_usd":
            min_trade = float(row["value"])
        elif row["key"] == "max_trade_usd":
            max_trade = float(row["value"])

    # Sum of USDC currently locked in open positions
    locked = await _query(
        f"SELECT COALESCE(SUM(our_size_usdc), 0) as val FROM shadow_positions WHERE status = 'open'{mode_filter}",
        mode_params,
    )
    in_positions = locked[0]["val"] if locked else 0

    realized = c.get("total_net", 0)
    cash_available = bankroll + realized - in_positions
    open_pnl = o.get("open_pnl", 0)
    account_value = bankroll + realized + open_pnl

    return {
        "total_pnl": round(total_net, 2),
        "total_gross": round(c.get("total_gross", 0), 2),
        "total_fees": round(c.get("total_fees", 0), 2),
        "total_slippage": round(c.get("total_slippage", 0), 2),
        "total_net": round(c.get("total_net", 0), 2),
        "trades_today": engine._trades_today,
        "win_rate": round(win_rate, 1),
        "open_positions": o.get("open_count", 0),
        "closed_positions": closed_count,
        "worst_delay_ms": round(fd.get("worst_delay_ms", 0), 0),
        "avg_delay_ms": round(fd.get("avg_delay_ms", 0), 0),
        "bankroll": round(bankroll, 2),
        "in_positions": round(in_positions, 2),
        "cash_available": round(cash_available, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(open_pnl, 2),
        "account_value": round(account_value, 2),
        "sizing_mode": "proportional",
        "min_trade": round(min_trade, 2),
        "max_trade": round(max_trade, 2),
        "max_pct": round(max_pct, 1),
    }


# ── Positions ─────────────────────────────────────────────

@app.get("/api/positions")
async def get_positions(status: str = Query("open"), mode: str = Query(None)):
    if mode:
        rows = await _query(
            "SELECT * FROM shadow_positions WHERE status = ? AND mode_id = ? ORDER BY id DESC",
            (status, mode),
        )
    else:
        rows = await _query(
            "SELECT * FROM shadow_positions WHERE status = ? ORDER BY id DESC",
            (status,),
        )
    return rows


# ── Price Refresh ─────────────────────────────────────────

@app.post("/api/refresh-prices")
async def refresh_prices():
    updated = await engine.refresh_prices()
    return {"updated": updated}


# ── Settings ──────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    rows = await _query("SELECT key, value, description FROM copy_trade_settings")
    return {row["key"]: row["value"] for row in rows}


@app.post("/api/settings")
async def update_settings(data: dict):
    for key, value in data.items():
        await _execute(
            "INSERT OR REPLACE INTO copy_trade_settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    # Auto-reload engine settings
    try:
        await engine.reload_settings()
    except Exception as e:
        logger.error(f"Failed to reload engine settings: {e}")
    return {"message": "Settings updated"}


# ── Filters Config ────────────────────────────────────────

@app.get("/api/filters/config")
async def filters_config():
    """Return filter metadata with current values from DB."""
    rows = await _query("SELECT key, value FROM copy_trade_settings")
    values = {r["key"]: r["value"] for r in rows}
    result = []
    for meta in FILTER_META:
        item = dict(meta)
        item["value"] = values.get(meta["key"], "")
        result.append(item)
    return result


@app.post("/api/engine/reload-settings")
async def reload_engine_settings():
    """Tell the engine to re-read settings from DB."""
    try:
        await engine.reload_settings()
        return {"message": "Engine settings reloaded"}
    except Exception as e:
        logger.error(f"Reload failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})



# -- Wallet Management -------------------------------------------

@app.get("/api/wallets/full")
async def wallets_full():
    """All wallets with full stats for the wallet management tab."""
    wallets = await _query("""
        SELECT id, address, username, csv_win_rate, csv_pnl, csv_volume,
               csv_unique_markets, wallet_tier, wallet_score, is_active,
               added_at, last_trade_at, profile_url
        FROM wallets
        ORDER BY wallet_score DESC NULLS LAST
    """)

    # Get per-wallet position stats across all modes
    pos_stats = await _query("""
        SELECT wallet_id,
            COUNT(CASE WHEN status='open' THEN 1 END) as open_positions,
            COUNT(CASE WHEN status='closed' THEN 1 END) as closed_trades,
            COALESCE(SUM(CASE WHEN status='closed' AND net_pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
            COALESCE(SUM(CASE WHEN status='closed' THEN net_pnl ELSE 0 END), 0) as realized_pnl,
            COALESCE(SUM(CASE WHEN status='open' THEN net_pnl ELSE 0 END), 0) as unrealized_pnl,
            COALESCE(SUM(our_size_usdc), 0) as total_invested,
            COALESCE(AVG(CASE WHEN entry_delay_ms IS NOT NULL THEN entry_delay_ms END), 0) as avg_delay_ms
        FROM shadow_positions
        GROUP BY wallet_id
    """)
    pos_map = {r["wallet_id"]: r for r in pos_stats}

    # Get per-wallet feed stats (total detected trades, passed, failed)
    feed_stats = await _query("""
        SELECT wallet_id,
            COUNT(*) as total_feed,
            COUNT(CASE WHEN filter_verdict='passed' THEN 1 END) as passed,
            COUNT(CASE WHEN filter_verdict='failed' THEN 1 END) as failed
        FROM copy_feed
        WHERE mode_id = 'strict'
        GROUP BY wallet_id
    """)
    feed_map = {r["wallet_id"]: r for r in feed_stats}

    result = []
    for w in wallets:
        wid = w["id"]
        ps = pos_map.get(wid, {})
        fs = feed_map.get(wid, {})

        closed = ps.get("closed_trades", 0)
        wins = ps.get("wins", 0)
        realized = ps.get("realized_pnl", 0)
        unrealized = ps.get("unrealized_pnl", 0)
        total_pnl = realized + unrealized

        result.append({
            "id": wid,
            "address": w["address"],
            "username": w["username"] or "",
            "is_active": bool(w["is_active"]),
            "added_at": w["added_at"],
            "last_trade_at": w["last_trade_at"],
            "profile_url": w["profile_url"] or "",
            # Historical stats (from CSV import)
            "csv_win_rate": round(w["csv_win_rate"] or 0, 1),
            "csv_pnl": round(w["csv_pnl"] or 0, 2),
            "csv_volume": round(w["csv_volume"] or 0, 2),
            "csv_markets": w["csv_unique_markets"] or 0,
            # Scoring
            "wallet_score": round(w["wallet_score"], 1) if w["wallet_score"] else None,
            "wallet_tier": w["wallet_tier"] or "",
            # Live position stats
            "open_positions": ps.get("open_positions", 0),
            "closed_trades": closed,
            "wins": wins,
            "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0,
            "realized_pnl": round(realized, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_pnl": round(total_pnl, 4),
            "total_invested": round(ps.get("total_invested", 0), 2),
            "avg_delay_ms": round(ps.get("avg_delay_ms", 0), 0),
            # Feed stats
            "trades_detected": fs.get("total_feed", 0),
            "trades_passed": fs.get("passed", 0),
            "trades_failed": fs.get("failed", 0),
        })
    return result


@app.delete("/api/wallets/{wallet_id}")
async def remove_wallet(wallet_id: int):
    """Deactivate a wallet — stops tracking, keeps historical data."""
    # Check wallet exists
    rows = await _query("SELECT id, username, address FROM wallets WHERE id = ?", (wallet_id,))
    if not rows:
        return JSONResponse(status_code=404, content={"error": "Wallet not found"})

    wallet = rows[0]

    # Deactivate the wallet
    await _execute("UPDATE wallets SET is_active = 0 WHERE id = ?", (wallet_id,))

    # Close any open shadow positions for this wallet
    await _execute(
        "UPDATE shadow_positions SET status = 'closed', closed_at = datetime('now') WHERE wallet_id = ? AND status = 'open'",
        (wallet_id,),
    )

    # Reload engine state so it stops polling this wallet
    try:
        await engine.reload_settings()
    except Exception as e:
        logger.error(f"Failed to reload engine after wallet removal: {e}")

    return {
        "message": "Wallet removed from tracking",
        "wallet_id": wallet_id,
        "username": wallet.get("username", ""),
    }


# ── Analytics: Per-wallet breakdown ──────────────────────

@app.get("/api/analytics/wallets")
async def analytics_wallets(mode: str = Query(None)):
    mode_filter = " AND sp.mode_id = ?" if mode else ""
    mode_params = (mode,) if mode else ()
    rows = await _query(f"""
        SELECT
            w.username,
            w.address,
            w.csv_win_rate,
            w.csv_pnl,
            w.csv_volume,
            COUNT(CASE WHEN sp.status='closed' THEN 1 END) as closed_trades,
            COUNT(CASE WHEN sp.status='open' THEN 1 END) as open_trades,
            COALESCE(SUM(sp.our_size_usdc), 0) as total_invested,
            COALESCE(SUM(CASE WHEN sp.status='closed' THEN sp.net_pnl ELSE 0 END), 0) as realized_pnl,
            COALESCE(SUM(CASE WHEN sp.status='open' THEN sp.net_pnl ELSE 0 END), 0) as unrealized_pnl,
            COUNT(CASE WHEN sp.status='closed' AND sp.net_pnl > 0 THEN 1 END) as wins,
            COALESCE(AVG(sp.our_size_usdc), 0) as avg_trade_size,
            COALESCE(AVG(CASE WHEN sp.entry_delay_ms IS NOT NULL THEN sp.entry_delay_ms END), 0) as avg_delay_ms
        FROM wallets w
        LEFT JOIN shadow_positions sp ON sp.wallet_id = w.id{mode_filter}
        WHERE w.is_active = 1
        GROUP BY w.id
        ORDER BY (realized_pnl + unrealized_pnl) DESC
    """, mode_params)
    return rows


# ── Analytics: Daily P&L ─────────────────────────────────

@app.get("/api/analytics/daily")
async def analytics_daily():
    rows = await _query("""
        SELECT
            DATE(closed_at) as day,
            SUM(net_pnl) as daily_pnl,
            COUNT(*) as trades
        FROM shadow_positions
        WHERE status = 'closed' AND closed_at IS NOT NULL
        GROUP BY DATE(closed_at)
        ORDER BY day
    """)
    return rows


# ── Analytics: Summary stats ─────────────────────────────

@app.get("/api/analytics/summary")
async def analytics_summary(mode: str = Query(None)):
    mode_filter = " AND mode_id = ?" if mode else ""
    mode_params = (mode,) if mode else ()

    # Core closed-trade stats
    base = await _query(f"""
        SELECT
            COUNT(*) as total_trades,
            COUNT(CASE WHEN net_pnl > 0 THEN 1 END) as total_wins,
            COUNT(CASE WHEN net_pnl <= 0 THEN 1 END) as total_losses,
            COALESCE(AVG(CASE WHEN net_pnl > 0 THEN net_pnl END), 0) as avg_win,
            COALESCE(AVG(CASE WHEN net_pnl <= 0 THEN net_pnl END), 0) as avg_loss,
            COALESCE(MAX(net_pnl), 0) as biggest_win,
            COALESCE(MIN(net_pnl), 0) as biggest_loss,
            COALESCE(SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END), 0) as sum_wins,
            COALESCE(SUM(CASE WHEN net_pnl <= 0 THEN net_pnl ELSE 0 END), 0) as sum_losses,
            COALESCE(SUM(poly_fee + slippage), 0) as total_fees_paid,
            COALESCE(SUM(our_size_usdc), 0) as total_invested,
            COALESCE(SUM(net_pnl), 0) as total_net,
            COALESCE(AVG(
                (julianday(closed_at) - julianday(opened_at)) * 24
            ), 0) as avg_hold_time_hours
        FROM shadow_positions WHERE status = 'closed'{mode_filter}
    """, mode_params)
    b = base[0] if base else {}

    total_trades = b.get("total_trades", 0)
    total_wins = b.get("total_wins", 0)
    win_rate = round((total_wins / total_trades * 100), 1) if total_trades > 0 else 0
    sum_losses = abs(b.get("sum_losses", 0))
    profit_factor = round(b.get("sum_wins", 0) / sum_losses, 2) if sum_losses > 0 else 0
    total_invested = b.get("total_invested", 0)
    roi_pct = round(b.get("total_net", 0) / total_invested * 100, 2) if total_invested > 0 else 0

    # Open positions unrealized
    unrealized = await _query(
        f"SELECT COALESCE(SUM(net_pnl), 0) as val FROM shadow_positions WHERE status = 'open'{mode_filter}",
        mode_params,
    )
    unrealized_pnl = unrealized[0]["val"] if unrealized else 0

    # Avg delay from copy_feed
    feed_mode_filter = " AND mode_id = ?" if mode else ""
    delay = await _query(
        f"SELECT COALESCE(AVG(delay_ms), 0) as avg_delay_ms FROM copy_feed WHERE delay_ms IS NOT NULL{feed_mode_filter}",
        mode_params,
    )
    avg_delay_ms = delay[0]["avg_delay_ms"] if delay else 0

    # Speed vs profit
    speed = await _query(f"""
        SELECT
            COALESCE(SUM(CASE WHEN entry_delay_ms < 5000 THEN net_pnl ELSE 0 END), 0) as fast_trade_pnl,
            COALESCE(SUM(CASE WHEN entry_delay_ms >= 5000 THEN net_pnl ELSE 0 END), 0) as slow_trade_pnl,
            COUNT(CASE WHEN entry_delay_ms < 5000 THEN 1 END) as fast_count,
            COUNT(CASE WHEN entry_delay_ms >= 5000 THEN 1 END) as slow_count
        FROM shadow_positions WHERE status = 'closed' AND entry_delay_ms IS NOT NULL{mode_filter}
    """, mode_params)
    s = speed[0] if speed else {}

    # Tracking since
    tracking = await _query("""
        SELECT MIN(ts) as since FROM (
            SELECT MIN(opened_at) as ts FROM shadow_positions WHERE opened_at IS NOT NULL
            UNION ALL
            SELECT MIN(detected_at) as ts FROM copy_feed WHERE detected_at IS NOT NULL
        )
    """)
    tracking_since = tracking[0]["since"] if tracking and tracking[0]["since"] else None

    # Avg trade score from recent passed trades
    avg_score = await _query(
        f"SELECT COALESCE(AVG(score_trade), 0) as avg_trade_score FROM copy_feed WHERE filter_verdict = 'passed' AND score_trade IS NOT NULL{feed_mode_filter}",
        mode_params,
    )
    avg_trade_score = avg_score[0]["avg_trade_score"] if avg_score else 0

    # Bankroll info
    if mode:
        bankroll_rows = await _query(
            "SELECT bankroll_usd FROM strategy_modes WHERE mode_id = ?", (mode,)
        )
        bankroll = bankroll_rows[0]["bankroll_usd"] if bankroll_rows else 1000.0
    else:
        bk_settings = await _query(
            "SELECT value FROM copy_trade_settings WHERE key = 'bankroll_usd'"
        )
        bankroll = float(bk_settings[0]["value"]) if bk_settings else 1000.0

    settings = await _query(
        "SELECT key, value FROM copy_trade_settings WHERE key IN ('min_trade_usd', 'max_trade_usd')"
    )
    min_trade = 5.0
    max_trade = 100.0
    for row in settings:
        if row["key"] == "min_trade_usd":
            min_trade = float(row["value"])
        elif row["key"] == "max_trade_usd":
            max_trade = float(row["value"])

    return {
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": b.get("total_losses", 0),
        "win_rate": win_rate,
        "avg_win": round(b.get("avg_win", 0), 4),
        "avg_loss": round(b.get("avg_loss", 0), 4),
        "biggest_win": round(b.get("biggest_win", 0), 4),
        "biggest_loss": round(b.get("biggest_loss", 0), 4),
        "profit_factor": profit_factor,
        "total_fees_paid": round(b.get("total_fees_paid", 0), 4),
        "total_invested": round(total_invested, 2),
        "total_net": round(b.get("total_net", 0), 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "roi_pct": roi_pct,
        "avg_hold_time_hours": round(b.get("avg_hold_time_hours", 0), 2),
        "avg_delay_ms": round(avg_delay_ms, 0),
        "fast_trade_pnl": round(s.get("fast_trade_pnl", 0), 4),
        "slow_trade_pnl": round(s.get("slow_trade_pnl", 0), 4),
        "fast_count": s.get("fast_count", 0),
        "slow_count": s.get("slow_count", 0),
        "tracking_since": tracking_since,
        "bankroll_usd": bankroll,
        "sizing_mode": "proportional",
        "min_trade_usd": min_trade,
        "max_trade_usd": max_trade,
        "avg_trade_score": round(avg_trade_score, 1),
    }


# ── Modes ────────────────────────────────────────────────

@app.get("/api/modes")
async def get_modes():
    """Return all strategy modes with settings and P&L summary."""
    modes = await _query("SELECT * FROM strategy_modes ORDER BY mode_id")
    result = []
    for m in modes:
        mid = m["mode_id"]
        # Get filter settings for this mode
        filters_rows = await _query(
            "SELECT key, value FROM mode_filter_settings WHERE mode_id = ?", (mid,)
        )
        filters = {r["key"]: r["value"] for r in filters_rows}

        # P&L stats
        closed = await _query(
            """SELECT COUNT(*) as cnt,
                COALESCE(SUM(net_pnl), 0) as realized
            FROM shadow_positions WHERE status = 'closed' AND mode_id = ?""",
            (mid,),
        )
        c = closed[0] if closed else {}
        open_pos = await _query(
            """SELECT COUNT(*) as cnt,
                COALESCE(SUM(net_pnl), 0) as unrealized
            FROM shadow_positions WHERE status = 'open' AND mode_id = ?""",
            (mid,),
        )
        o = open_pos[0] if open_pos else {}
        wins = await _query(
            "SELECT COUNT(*) as cnt FROM shadow_positions WHERE status = 'closed' AND net_pnl > 0 AND mode_id = ?",
            (mid,),
        )
        win_count = wins[0]["cnt"] if wins else 0
        closed_count = c.get("cnt", 0)
        win_rate = round(win_count / closed_count * 100, 1) if closed_count > 0 else 0

        result.append({
            "mode_id": mid,
            "label": m["label"],
            "description": m.get("description", ""),
            "bankroll_usd": m["bankroll_usd"],
            "is_active": bool(m["is_active"]),
            "total_trades": closed_count,
            "open_positions": o.get("cnt", 0),
            "realized_pnl": round(c.get("realized", 0), 2),
            "unrealized_pnl": round(o.get("unrealized", 0), 2),
            "win_rate": win_rate,
            "filters": filters,
        })
    return result


@app.get("/api/modes/{mode_id}/positions")
async def mode_positions(mode_id: str, status: str = Query("open")):
    rows = await _query(
        "SELECT * FROM shadow_positions WHERE mode_id = ? AND status = ? ORDER BY id DESC",
        (mode_id, status),
    )
    return rows


@app.get("/api/modes/{mode_id}/feed")
async def mode_feed(mode_id: str, limit: int = Query(100, ge=1, le=500)):
    rows = await _query(
        "SELECT * FROM copy_feed WHERE mode_id = ? ORDER BY id DESC LIMIT ?",
        (mode_id, limit),
    )
    return rows


# ── Wallet Scores ────────────────────────────────────────

@app.get("/api/wallets/scores")
async def wallet_scores():
    """Return all active wallets with their dynamic scores and tiers."""
    rows = await _query("""
        SELECT id, address, username, csv_win_rate, csv_pnl, csv_volume,
               csv_unique_markets, wallet_tier, wallet_score,
               sim_total_pnl, sim_win_rate, sim_total_trades
        FROM wallets WHERE is_active = 1
        ORDER BY wallet_score DESC NULLS LAST
    """)
    return rows


# ── Scout: Queue ──────────────────────────────────────────

@app.get("/api/scout/queue")
async def scout_queue(status: str = Query("pending"), limit: int = Query(50, ge=1, le=200)):
    rows = await _query(
        "SELECT * FROM scout_candidates WHERE status = ? ORDER BY score DESC LIMIT ?",
        (status, limit),
    )
    return rows


@app.post("/api/scout/approve")
async def scout_approve(data: dict):
    wallet = (data.get("proxy_wallet") or "").strip().lower()
    if not wallet:
        return JSONResponse(status_code=400, content={"error": "proxy_wallet required"})

    # Fetch candidate info
    rows = await _query(
        "SELECT * FROM scout_candidates WHERE proxy_wallet = ?", (wallet,)
    )
    if not rows:
        return JSONResponse(status_code=404, content={"error": "Candidate not found"})

    candidate = rows[0]

    # Insert into wallets table for tracking
    try:
        await _execute(
            """INSERT OR IGNORE INTO wallets
                (address, username, score, csv_win_rate, csv_pnl, csv_volume, csv_unique_markets, profile_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                wallet,
                candidate.get("username") or "",
                candidate.get("score") or 0,
                candidate.get("win_rate") or 0,
                candidate.get("total_pnl") or 0,
                candidate.get("total_invested") or 0,
                candidate.get("markets_traded") or 0,
                "",
            ),
        )
    except Exception as e:
        logger.error(f"Error inserting approved wallet: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Mark candidate as approved
    await _execute(
        "UPDATE scout_candidates SET status = 'approved', reviewed_at = datetime('now') WHERE proxy_wallet = ?",
        (wallet,),
    )
    return {"message": "Wallet approved and added to tracking", "wallet": wallet}


@app.post("/api/scout/reject")
async def scout_reject(data: dict):
    wallet = (data.get("proxy_wallet") or "").strip().lower()
    if not wallet:
        return JSONResponse(status_code=400, content={"error": "proxy_wallet required"})

    await _execute(
        "UPDATE scout_candidates SET status = 'rejected', reviewed_at = datetime('now') WHERE proxy_wallet = ?",
        (wallet,),
    )
    return {"message": "Candidate rejected", "wallet": wallet}


@app.get("/api/scout/stats")
async def scout_stats():
    counts = await _query("""
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
            COUNT(CASE WHEN status = 'approved' THEN 1 END) as approved,
            COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected
        FROM scout_candidates
    """)
    c = counts[0] if counts else {}
    status = scout.get_status()
    return {
        "total_scanned": status["total_scanned"],
        "cycle_count": status["cycle_count"],
        "running": status["running"],
        "queued": c.get("pending", 0),
        "approved": c.get("approved", 0),
        "rejected": c.get("rejected", 0),
        "total_candidates": c.get("total", 0),
    }


@app.post("/api/scout/start")
async def scout_start():
    if scout._running:
        return {"message": "Scout already running"}
    scout._task = asyncio.create_task(scout.start_loop())
    return {"message": "Scout started"}


@app.post("/api/scout/stop")
async def scout_stop():
    scout.stop_loop()
    return {"message": "Scout stopped"}


# ── Scout: Settings ──────────────────────────────────────

@app.get("/api/scout/settings")
async def get_scout_settings():
    rows = await _query("SELECT key, value FROM scout_settings")
    return {row["key"]: row["value"] for row in rows}


@app.post("/api/scout/settings")
async def update_scout_settings(data: dict):
    for key, value in data.items():
        await _execute(
            "INSERT OR REPLACE INTO scout_settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
    # Reload scout settings
    try:
        await scout._load_settings()
    except Exception as e:
        logger.error(f"Failed to reload scout settings: {e}")
    return {"message": "Scout settings updated"}


# ── Static Files ──────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"), media_type="text/html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
