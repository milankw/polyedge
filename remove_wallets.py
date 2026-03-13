#!/usr/bin/env python3
"""Remove bad wallets from the database."""
import sqlite3
import json

with open("remove_wallet_ids.json") as f:
    data = json.load(f)

remove_ids = data["remove"]
keep_ids = data["keep"]
placeholders = ",".join(str(x) for x in remove_ids)

db = sqlite3.connect("backend/data/polyedge.db")
db.row_factory = sqlite3.Row

print("=== BEFORE ===")
total = db.execute("SELECT COUNT(*) as c FROM wallets WHERE is_active = 1").fetchone()["c"]
print(f"Active wallets: {total}")
open_pos = db.execute("SELECT COUNT(*) as c FROM shadow_positions WHERE status = 'open'").fetchone()["c"]
print(f"Open positions: {open_pos}")

# Check positions on wallets being removed
q = f"SELECT COUNT(*) as c, COALESCE(SUM(our_size_usdc), 0) as invested, COALESCE(SUM(net_pnl), 0) as pnl FROM shadow_positions WHERE status = 'open' AND wallet_id IN ({placeholders})"
rp = db.execute(q).fetchone()
print(f"Positions on removed wallets: {rp['c']} (invested: ${rp['invested']:.2f}, pnl: ${rp['pnl']:.2f})")

# 1. Close any open positions for removed wallets
db.execute(f"UPDATE shadow_positions SET status = 'closed', closed_at = datetime('now') WHERE status = 'open' AND wallet_id IN ({placeholders})")
print(f"\nClosed {rp['c']} positions on removed wallets")

# 2. Deactivate the wallets
db.execute(f"UPDATE wallets SET is_active = 0 WHERE id IN ({placeholders})")
print(f"Deactivated {len(remove_ids)} wallets")

db.commit()

print("\n=== AFTER ===")
active = db.execute("SELECT COUNT(*) as c FROM wallets WHERE is_active = 1").fetchone()["c"]
print(f"Active wallets: {active}")
open_after = db.execute("SELECT COUNT(*) as c FROM shadow_positions WHERE status = 'open'").fetchone()["c"]
print(f"Open positions: {open_after}")

# Show remaining wallets
rows = db.execute("SELECT id, username, address, wallet_score, csv_win_rate, csv_pnl FROM wallets WHERE is_active = 1 ORDER BY wallet_score DESC").fetchall()
print(f"\n=== REMAINING WALLETS ===")
for r in rows:
    name = r['username'] or r['address'][:12]
    print(f"  ID:{r['id']:<6} {name:<25} Score:{r['wallet_score'] or 0:>5.1f}  WR:{r['csv_win_rate']:>4.0f}%  P&L:${r['csv_pnl']:>12,.0f}")

db.close()
print("\nDone.")
