#!/usr/bin/env python3
"""
Seed script — creates DB tables and loads 100 wallets from CSV.
Run once on first boot; safe to re-run (INSERT OR IGNORE).
"""
import csv
import os
import sqlite3
import sys

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "polyedge.db")

# Look for CSV in multiple locations (Docker vs local dev)
CSV_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "polymarket_100_wallets.csv"),
    "/app/polymarket_100_wallets.csv",
    os.path.join(os.path.dirname(__file__), "polymarket_100_wallets.csv"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT UNIQUE NOT NULL,
    username TEXT,
    score REAL,
    csv_win_rate REAL,
    csv_pnl REAL,
    csv_volume REAL,
    csv_unique_markets INTEGER,
    alloc_usd REAL DEFAULT 1000.0,
    is_active INTEGER DEFAULT 1,
    added_at TEXT DEFAULT (datetime('now')),
    last_scanned TEXT,
    sim_total_pnl REAL DEFAULT 0.0,
    sim_realized_pnl REAL DEFAULT 0.0,
    sim_unrealized_pnl REAL DEFAULT 0.0,
    sim_total_trades INTEGER DEFAULT 0,
    sim_wins INTEGER DEFAULT 0,
    sim_losses INTEGER DEFAULT 0,
    sim_win_rate REAL DEFAULT 0.0,
    sim_open_positions INTEGER DEFAULT 0,
    profile_url TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER REFERENCES wallets(id),
    wallet_address TEXT NOT NULL,
    tx_hash TEXT UNIQUE,
    original_timestamp INTEGER,
    condition_id TEXT,
    asset TEXT,
    side TEXT,
    original_size REAL,
    original_price REAL,
    outcome TEXT,
    outcome_index INTEGER,
    market_title TEXT,
    market_slug TEXT,
    event_slug TEXT,
    sim_size REAL,
    sim_entry_price REAL,
    sim_current_price REAL,
    sim_pnl REAL DEFAULT 0.0,
    sim_status TEXT DEFAULT 'open',
    sim_exit_price REAL,
    sim_exit_time TEXT,
    detected_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    total_wallets INTEGER,
    active_wallets INTEGER,
    total_sim_pnl REAL,
    total_realized REAL,
    total_unrealized REAL,
    open_positions INTEGER,
    total_trades INTEGER,
    overall_win_rate REAL,
    best_wallet_id INTEGER,
    worst_wallet_id INTEGER
);

CREATE TABLE IF NOT EXISTS wallet_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER REFERENCES wallets(id),
    timestamp TEXT DEFAULT (datetime('now')),
    sim_pnl REAL,
    open_positions INTEGER,
    win_rate REAL
);

CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    scan_type TEXT,
    wallets_scanned INTEGER,
    new_trades INTEGER,
    prices_updated INTEGER,
    positions_resolved INTEGER,
    duration_seconds REAL,
    errors TEXT
);

CREATE TABLE IF NOT EXISTS copy_trade_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS copy_trade_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT DEFAULT (datetime('now')),
    wallet_address TEXT NOT NULL,
    wallet_username TEXT,
    market_id TEXT,
    market_slug TEXT,
    market_title TEXT,
    direction TEXT,
    wallet_entry_price REAL,
    current_market_price REAL,
    liquidity_pool_usdc REAL,
    volume_24h_usdc REAL,
    unique_trader_count INTEGER,
    resolution_date TEXT,
    days_to_resolution INTEGER,
    confirming_wallet_count INTEGER,
    price_movement_6h_pct REAL,
    wallet_position_pct_of_pool REAL,
    all_filters_passed INTEGER DEFAULT 0,
    filters_failed TEXT,
    filter_results TEXT,
    action_taken TEXT DEFAULT 'LOGGED',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS wallet_position_cache (
    wallet_address TEXT,
    condition_id TEXT,
    outcome_index INTEGER,
    first_seen_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (wallet_address, condition_id, outcome_index)
);
"""

COPY_TRADE_DEFAULTS = [
    ("min_liquidity_usd", "75000", "Minimum liquidity pool size in USD"),
    ("entry_timing_minutes", "5", "Max minutes since position opened"),
    ("max_wallet_pool_pct", "5.0", "Max wallet position as % of pool"),
    ("min_volume_24h_usd", "25000", "Minimum 24h volume in USD"),
    ("min_unique_traders", "50", "Minimum unique traders in market"),
    ("resolution_min_days", "3", "Minimum days to resolution"),
    ("resolution_max_days", "45", "Maximum days to resolution"),
    ("min_wallet_win_rate", "55.0", "Minimum wallet win rate %"),
    ("min_resolved_markets", "20", "Minimum resolved markets for win rate"),
    ("max_price_move_6h_pct", "12.0", "Max price movement in 6h %"),
    ("min_confirming_wallets", "2", "Minimum confirming wallets"),
    ("max_bankroll_pct", "5.0", "Max % of bankroll per trade"),
    ("bankroll_usd", "10000", "Total simulated bankroll in USD"),
    ("execution_mode", "MANUAL", "MANUAL or AUTO"),
    ("poll_interval_minutes", "5", "How often to poll wallets"),
    ("telegram_bot_token", "", "Telegram bot token for alerts"),
    ("telegram_chat_id", "", "Telegram chat ID for alerts"),
]


def find_csv():
    for path in CSV_CANDIDATES:
        resolved = os.path.abspath(path)
        if os.path.isfile(resolved):
            return resolved
    return None


def seed():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create tables
    cursor.executescript(SCHEMA)
    conn.commit()
    print(f"[seed] Tables created in {DB_PATH}")

    # Seed copy trade default settings
    for key, value, desc in COPY_TRADE_DEFAULTS:
        cursor.execute(
            "INSERT OR IGNORE INTO copy_trade_settings (key, value, description) VALUES (?, ?, ?)",
            (key, value, desc),
        )
    conn.commit()
    print(f"[seed] Copy trade settings seeded ({len(COPY_TRADE_DEFAULTS)} defaults)")

    # Find and load CSV
    csv_path = find_csv()
    if csv_path is None:
        print("[seed] WARNING: CSV file not found — skipping wallet seeding")
        print(f"[seed] Searched: {CSV_CANDIDATES}")
        conn.close()
        return

    print(f"[seed] Loading wallets from {csv_path}")
    inserted = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                cursor.execute(
                    """INSERT OR IGNORE INTO wallets
                    (address, username, score, csv_win_rate, csv_pnl, csv_volume,
                     csv_unique_markets, profile_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["wallet_address"].strip().lower(),
                        row["username"].strip(),
                        float(row["score"]),
                        float(row["win_rate"]),
                        float(row["pnl_all"]),
                        float(row["volume_all"]),
                        int(row["unique_markets"]),
                        row.get("polymarket_profile_url", ""),
                    ),
                )
                if cursor.rowcount > 0:
                    inserted += 1
            except Exception as e:
                print(f"[seed] Error inserting row: {e} — {row}")

    conn.commit()
    conn.close()
    total = cursor.lastrowid or 0
    print(f"[seed] Done — {inserted} new wallets inserted (total in DB: checked)")


if __name__ == "__main__":
    seed()
