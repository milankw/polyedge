"""
PolyEdge v2 — FastAPI server with all API endpoints + APScheduler.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.copy_trader import CopyTradeMonitor
from backend.engine import CopyTradeEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("polyedge.app")

engine = CopyTradeEngine()
copy_monitor = CopyTradeMonitor()
scheduler = AsyncIOScheduler()

# Track running scan to prevent overlaps
_scan_lock = asyncio.Lock()


async def scheduled_scan():
    if _scan_lock.locked():
        logger.info("Scan already running, skipping scheduled scan")
        return
    async with _scan_lock:
        logger.info("Scheduled scan starting...")
        result = await engine.scan_all_wallets()
        logger.info(f"Scheduled scan complete: {result}")


async def scheduled_price_update():
    if _scan_lock.locked():
        logger.info("Scan running, skipping scheduled price update")
        return
    async with _scan_lock:
        logger.info("Scheduled price update starting...")
        result = await engine.update_prices()
        logger.info(f"Scheduled price update complete: {result}")


async def scheduled_snapshot():
    logger.info("Saving scheduled snapshot...")
    await engine._save_snapshot()
    logger.info("Scheduled snapshot saved")


async def scheduled_copy_trade_poll():
    if copy_monitor._is_running:
        logger.info("Copy trade poll already running, skipping")
        return
    logger.info("Scheduled copy trade poll starting...")
    result = await copy_monitor.poll_wallets()
    logger.info(f"Scheduled copy trade poll complete: {result}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("PolyEdge v2 starting up...")
    scheduler.add_job(scheduled_scan, "interval", minutes=30, id="scan_wallets")
    scheduler.add_job(scheduled_price_update, "interval", minutes=15, id="update_prices")
    scheduler.add_job(scheduled_snapshot, "interval", hours=1, id="save_snapshot")
    scheduler.add_job(scheduled_copy_trade_poll, "interval", minutes=5, id="copy_trade_poll")
    scheduler.start()
    logger.info("Scheduler started — scan every 30m, prices every 15m, snapshots every 1h, copy trade every 5m")
    yield
    # Shutdown
    scheduler.shutdown()
    await engine.close()
    await copy_monitor.close()
    logger.info("PolyEdge v2 shut down")


app = FastAPI(title="PolyEdge v2", lifespan=lifespan)

# ── API Endpoints ──────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "polyedge-v2"}


@app.get("/api/stats")
async def stats():
    return await engine.get_stats()


@app.get("/api/dashboard")
async def dashboard():
    return await engine.get_dashboard_data()


@app.get("/api/wallets")
async def wallets():
    return await engine.get_all_wallets()


@app.get("/api/wallets/{wallet_id}")
async def wallet_detail(wallet_id: int):
    data = await engine.get_wallet_detail(wallet_id)
    if "error" in data:
        return JSONResponse(status_code=404, content=data)
    return data


@app.get("/api/wallets/{wallet_id}/trades")
async def wallet_trades(wallet_id: int):
    return await engine.get_all_trades(wallet_id=wallet_id, limit=500)


@app.get("/api/trades")
async def trades(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    wallet_id: int | None = None,
    status: str | None = None,
):
    return await engine.get_all_trades(limit=limit, offset=offset, wallet_id=wallet_id, status=status)


@app.get("/api/trades/recent")
async def recent_trades():
    return await engine.get_recent_trades()


@app.get("/api/snapshots")
async def snapshots():
    return await engine.get_snapshots()


@app.get("/api/scan-log")
async def scan_log():
    return await engine.get_scan_log()


@app.post("/api/scan")
async def trigger_scan():
    if _scan_lock.locked():
        return {"status": "already_running", "message": "A scan is already in progress"}
    async with _scan_lock:
        result = await engine.scan_all_wallets()
    return {"status": "completed", "result": result}


@app.post("/api/update-prices")
async def trigger_price_update():
    if _scan_lock.locked():
        return {"status": "already_running", "message": "A scan is already in progress"}
    async with _scan_lock:
        result = await engine.update_prices()
    return {"status": "completed", "result": result}


@app.post("/api/full-cycle")
async def trigger_full_cycle():
    if _scan_lock.locked():
        return {"status": "already_running", "message": "A scan is already in progress"}
    async with _scan_lock:
        result = await engine.full_cycle()
    return {"status": "completed", "result": result}


# ── Copy Trade Endpoints ──────────────────────────────────────────


@app.get("/api/copy-trade/signals")
async def copy_trade_signals(days: int = Query(7, ge=1, le=90)):
    return await copy_monitor.get_signals(days=days)


@app.get("/api/copy-trade/signals/{signal_id}")
async def copy_trade_signal_detail(signal_id: int):
    data = await copy_monitor.get_signal_detail(signal_id)
    if "error" in data:
        return JSONResponse(status_code=404, content=data)
    return data


@app.get("/api/copy-trade/settings")
async def copy_trade_settings():
    return await copy_monitor.get_settings()


@app.post("/api/copy-trade/settings")
async def update_copy_trade_settings(updates: dict):
    return await copy_monitor.update_settings(updates)


@app.post("/api/copy-trade/scan")
async def trigger_copy_trade_scan():
    if copy_monitor._is_running:
        return {"status": "already_running", "message": "A copy trade scan is already in progress"}
    result = await copy_monitor.poll_wallets()
    return {"status": "completed", "result": result}


@app.get("/api/copy-trade/status")
async def copy_trade_status():
    return await copy_monitor.get_status()


# ── Static file serving ─────────────────────────────────────────

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
async def serve_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    return FileResponse(index_path, media_type="text/html")


# Mount static files AFTER API routes so /api/ takes priority
app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
