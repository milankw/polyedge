#!/usr/bin/env python3
"""
Seed script — creates DB tables and loads wallets from CSV.
Safe to re-run (CREATE IF NOT EXISTS + INSERT OR IGNORE).

IMPORTANT: Never drops or recreates the wallets table.
"""
import csv
import os
import sqlite3

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "polyedge.db")

CSV_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "polymarket_100_wallets.csv"),
    "/app/polymarket_100_wallets.csv",
    os.path.join(os.path.dirname(__file__), "polymarket_100_wallets.csv"),
]

SCHEMA = """
-- Existing wallets table — preserved, never dropped
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

-- Live feed: every copy trade pair goes here
CREATE TABLE IF NOT EXISTS copy_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER REFERENCES wallets(id),
    wallet_address TEXT NOT NULL,
    wallet_username TEXT,
    tx_hash TEXT NOT NULL,
    event_type TEXT NOT NULL,
    side TEXT NOT NULL,
    direction TEXT,
    condition_id TEXT,
    market_slug TEXT,
    market_title TEXT,
    their_price REAL,
    their_size REAL,
    their_timestamp_ms INTEGER,
    our_price REAL,
    our_size REAL,
    our_shares REAL,
    poly_fee REAL DEFAULT 0.0,
    slippage REAL DEFAULT 0.0,
    delay_ms INTEGER,
    realized_pnl REAL,
    net_pnl REAL,
    detected_at TEXT DEFAULT (datetime('now')),
    executed_at_ms INTEGER
);

-- Shadow positions: simulated open/closed positions
CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER REFERENCES wallets(id),
    wallet_address TEXT NOT NULL,
    wallet_username TEXT,
    condition_id TEXT NOT NULL,
    outcome_index INTEGER NOT NULL,
    market_slug TEXT,
    market_title TEXT,
    direction TEXT,
    their_entry_price REAL,
    their_current_size REAL,
    our_entry_price REAL,
    our_size_usdc REAL,
    our_shares REAL,
    current_price REAL,
    gross_pnl REAL DEFAULT 0.0,
    poly_fee REAL DEFAULT 0.0,
    slippage REAL DEFAULT 0.0,
    net_pnl REAL DEFAULT 0.0,
    entry_delay_ms INTEGER,
    exit_delay_ms INTEGER,
    status TEXT DEFAULT 'open',
    opened_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT,
    exit_price REAL,
    UNIQUE(wallet_id, condition_id, outcome_index, status)
);

-- Loop health log
CREATE TABLE IF NOT EXISTS loop_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number INTEGER,
    cycle_ms REAL,
    wallets_polled INTEGER,
    wallets_failed INTEGER,
    new_trades_detected INTEGER,
    copies_executed INTEGER,
    timestamp TEXT DEFAULT (datetime('now'))
);

-- Settings (preserved)
CREATE TABLE IF NOT EXISTS copy_trade_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT
);

-- Legacy tables kept for backward compat
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

-- Filter results: per-trade per-filter evaluation results
CREATE TABLE IF NOT EXISTS filter_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_feed_id INTEGER REFERENCES copy_feed(id),
    tx_hash TEXT NOT NULL,
    filter_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    threshold_value TEXT,
    actual_value TEXT,
    fail_message TEXT,
    evaluated_at TEXT DEFAULT (datetime('now'))
);

-- Scout: discovered wallet candidates
CREATE TABLE IF NOT EXISTS scout_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_wallet TEXT NOT NULL UNIQUE,
    username TEXT,
    score REAL NOT NULL DEFAULT 0,
    total_positions INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    total_invested REAL DEFAULT 0,
    roi_pct REAL DEFAULT 0,
    avg_trade_size REAL DEFAULT 0,
    markets_traded INTEGER DEFAULT 0,
    lifetime_trades INTEGER DEFAULT 0,
    discovered_via TEXT,
    discovered_market TEXT,
    status TEXT DEFAULT 'pending',
    score_breakdown TEXT,
    discovered_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT,
    last_scored_at TEXT DEFAULT (datetime('now'))
);

-- Scout settings
CREATE TABLE IF NOT EXISTS scout_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Strategy modes: parallel trading strategies
CREATE TABLE IF NOT EXISTS strategy_modes (
    mode_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT,
    bankroll_usd REAL DEFAULT 1000.0,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Per-mode filter settings
CREATE TABLE IF NOT EXISTS mode_filter_settings (
    mode_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (mode_id, key),
    FOREIGN KEY (mode_id) REFERENCES strategy_modes(mode_id)
);
"""

COPY_TRADE_DEFAULTS = [
    ("min_liquidity_usd", "75000", "Minimum liquidity pool size in USD"),
    ("entry_timing_minutes", "120", "Max minutes since position opened"),
    ("max_wallet_pool_pct", "10.0", "Max wallet position as % of pool"),
    ("min_volume_24h_usd", "25000", "Minimum 24h volume in USD"),
    ("min_unique_traders", "50", "Minimum unique traders in market"),
    ("resolution_min_days", "3", "Minimum days to resolution"),
    ("resolution_max_days", "45", "Maximum days to resolution"),
    ("min_wallet_win_rate", "55.0", "Minimum wallet win rate %"),
    ("min_resolved_markets", "20", "Minimum resolved markets for win rate"),
    ("max_price_move_6h_pct", "12.0", "Max price movement in 6h %"),
    ("min_confirming_wallets", "2", "Minimum confirming wallets"),
    ("max_bankroll_pct", "10.0", "Max % of bankroll per single trade"),
    ("bankroll_usd", "1000", "Total simulated bankroll in USD"),
    ("min_trade_usd", "5", "Minimum trade size in USD"),
    ("max_trade_usd", "100", "Maximum trade size in USD"),
    ("max_bankroll_exposure_pct", "5.0", "Max % of bankroll exposed per trade (hard cap)"),
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

    cursor.executescript(SCHEMA)
    conn.commit()
    print(f"[seed] Tables created in {DB_PATH}")

    # Add columns if missing (safe migrations)
    for col, typ in [("last_trade_at", "TEXT")]:
        try:
            cursor.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
            conn.commit()
            print(f"[seed] Added {col} column to wallets")
        except sqlite3.OperationalError:
            pass

    # Add filter_verdict column to copy_feed
    try:
        cursor.execute("ALTER TABLE copy_feed ADD COLUMN filter_verdict TEXT DEFAULT 'pending'")
        conn.commit()
        print("[seed] Added filter_verdict column to copy_feed")
    except sqlite3.OperationalError:
        pass

    # Scoring columns on copy_feed
    for col, typ in [
        ("score_trade", "REAL DEFAULT NULL"),
        ("score_wallet", "REAL DEFAULT NULL"),
        ("trade_tier", "TEXT DEFAULT NULL"),
        ("score_breakdown", "TEXT DEFAULT NULL"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE copy_feed ADD COLUMN {col} {typ}")
            conn.commit()
            print(f"[seed] Added {col} column to copy_feed")
        except sqlite3.OperationalError:
            pass

    # Scoring columns on wallets
    for col, typ in [
        ("wallet_tier", "TEXT DEFAULT NULL"),
        ("wallet_score", "REAL DEFAULT NULL"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
            conn.commit()
            print(f"[seed] Added {col} column to wallets")
        except sqlite3.OperationalError:
            pass

    # Scoring columns on shadow_positions
    for col, typ in [
        ("score_trade", "REAL DEFAULT NULL"),
        ("trade_tier", "TEXT DEFAULT NULL"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE shadow_positions ADD COLUMN {col} {typ}")
            conn.commit()
            print(f"[seed] Added {col} column to shadow_positions")
        except sqlite3.OperationalError:
            pass

    # --- Multi-mode migrations ---
    # Add mode_id to shadow_positions, copy_feed, filter_results
    for table, col, typ in [
        ("shadow_positions", "mode_id", "TEXT DEFAULT 'strict'"),
        ("copy_feed", "mode_id", "TEXT DEFAULT 'strict'"),
        ("filter_results", "mode_id", "TEXT DEFAULT 'strict'"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            conn.commit()
            print(f"[seed] Added {col} column to {table}")
        except sqlite3.OperationalError:
            pass

    # Fix UNIQUE constraints for multi-mode:
    # copy_feed: tx_hash was UNIQUE but now same tx can appear for multiple modes
    # We drop the old unique index and create a new one including mode_id.
    # SQLite can't DROP constraint, so we create a new unique index instead.
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_copy_feed_tx_mode "
            "ON copy_feed(tx_hash, mode_id)"
        )
        conn.commit()
        print("[seed] Created unique index uq_copy_feed_tx_mode")
    except sqlite3.OperationalError:
        pass

    # Drop the old tx_hash unique constraint by recreating the index
    # (the original UNIQUE on tx_hash is a table constraint, can't be dropped in SQLite,
    #  but the new uq_copy_feed_tx_mode index will handle multi-mode inserts.
    #  We need to handle the INSERT conflict by using INSERT OR REPLACE or checking.)

    # filter_results: UNIQUE(tx_hash, filter_name) -> needs mode_id
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_filter_results_tx_filter_mode "
            "ON filter_results(tx_hash, filter_name, mode_id)"
        )
        conn.commit()
        print("[seed] Created unique index uq_filter_results_tx_filter_mode")
    except sqlite3.OperationalError:
        pass

    # shadow_positions: UNIQUE(wallet_id, condition_id, outcome_index, status) -> needs mode_id
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_pos_mode "
            "ON shadow_positions(wallet_id, condition_id, outcome_index, status, mode_id)"
        )
        conn.commit()
        print("[seed] Created unique index uq_shadow_pos_mode")
    except sqlite3.OperationalError:
        pass

    # Seed strategy modes
    mode_defs = [
        ("strict", "Strict", "All 7 active filters at tight thresholds", 1000.0, 1),
        ("moderate", "Moderate", "6 filters with relaxed thresholds", 1000.0, 1),
        ("aggressive", "Aggressive", "3 filters only — maximum trade volume", 1000.0, 1),
    ]
    for mode_id, label, desc, bankroll, active in mode_defs:
        cursor.execute(
            "INSERT OR IGNORE INTO strategy_modes (mode_id, label, description, bankroll_usd, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (mode_id, label, desc, bankroll, active),
        )
    conn.commit()
    print(f"[seed] Strategy modes seeded ({len(mode_defs)} modes)")

    # Seed mode filter settings
    mode_filters = {
        "strict": {
            "min_liquidity_usd": "75000",
            "entry_timing_minutes": "120",
            "max_wallet_pool_pct": "10.0",
            "min_volume_24h_usd": "15000",
            "min_unique_traders": "50",
            "min_wallet_win_rate": "55.0",
            "min_resolved_markets": "20",
            "max_bankroll_exposure_pct": "5.0",
        },
        "moderate": {
            "min_liquidity_usd": "25000",
            "entry_timing_minutes": "120",
            "min_volume_24h_usd": "10000",
            "min_unique_traders": "25",
            "min_wallet_win_rate": "50.0",
            "min_resolved_markets": "10",
            "max_bankroll_exposure_pct": "5.0",
            "max_wallet_pool_pct": "100.0",
        },
        "aggressive": {
            "min_liquidity_usd": "10000",
            "min_wallet_win_rate": "50.0",
            "max_bankroll_exposure_pct": "5.0",
            "entry_timing_minutes": "99999",
            "max_wallet_pool_pct": "100.0",
            "min_volume_24h_usd": "0",
            "min_unique_traders": "0",
            "min_resolved_markets": "0",
        },
    }
    filter_count = 0
    for mode_id, filters in mode_filters.items():
        for key, value in filters.items():
            cursor.execute(
                "INSERT OR IGNORE INTO mode_filter_settings (mode_id, key, value) VALUES (?, ?, ?)",
                (mode_id, key, value),
            )
            filter_count += 1
    conn.commit()
    print(f"[seed] Mode filter settings seeded ({filter_count} entries)")

    # Seed default settings
    for key, value, desc in COPY_TRADE_DEFAULTS:
        cursor.execute(
            "INSERT OR IGNORE INTO copy_trade_settings (key, value, description) VALUES (?, ?, ?)",
            (key, value, desc),
        )
    conn.commit()
    print(f"[seed] Copy trade settings seeded ({len(COPY_TRADE_DEFAULTS)} defaults)")

    # Seed scout settings
    scout_defaults = [
        ("min_score_threshold", "45"),
        ("max_wallets_per_cycle", "10"),
        ("cycle_delay_seconds", "60"),
        ("eval_delay_seconds", "5"),
        ("discovery_market_trades", "1"),
        ("discovery_global_trades", "1"),
    ]
    for key, value in scout_defaults:
        cursor.execute(
            "INSERT OR IGNORE INTO scout_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    print(f"[seed] Scout settings seeded ({len(scout_defaults)} defaults)")

    # Load wallets from CSV
    csv_path = find_csv()
    if csv_path is None:
        print("[seed] WARNING: CSV not found — skipping wallet seeding")
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
                print(f"[seed] Error inserting row: {e}")

    conn.commit()
    conn.close()
    print(f"[seed] Done — {inserted} new wallets inserted")


if __name__ == "__main__":
    seed()
