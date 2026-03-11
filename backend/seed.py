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
"""


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
