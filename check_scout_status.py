import sqlite3, json

db = sqlite3.connect('backend/data/polyedge.db')
db.row_factory = sqlite3.Row

# Scout candidates stats
total = db.execute('SELECT COUNT(*) FROM scout_candidates').fetchone()[0]
approved = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status='approved'").fetchone()[0]
rejected = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status='rejected'").fetchone()[0]
queued = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status='queued'").fetchone()[0]
print(f'Scout candidates in DB: {total} total | {approved} approved | {rejected} rejected | {queued} queued')

# Show approved wallets
print('\n=== APPROVED SCOUT WALLETS ===')
for r in db.execute("SELECT id, proxy_wallet, username, score, win_rate, total_pnl, roi_pct, status FROM scout_candidates WHERE status='approved'").fetchall():
    print(f'  #{r[0]} | {r[1][:20]}... | {r[2]} | score={r[3]} | WR={r[4]}% | PnL=${r[5]:,.0f} | ROI={r[6]}%')

# Check if they're in the wallets table (being tracked by engine)
print('\n=== ARE THEY IN TRACKED WALLETS? ===')
for r in db.execute("SELECT proxy_wallet FROM scout_candidates WHERE status='approved'").fetchall():
    addr = r[0]
    tracked = db.execute('SELECT id, active FROM wallets WHERE address = ?', (addr,)).fetchone()
    if tracked:
        print(f'  {addr[:20]}... -> tracked (wallet #{tracked[0]}, active={tracked[1]})')
    else:
        print(f'  {addr[:20]}... -> NOT in wallets table!')

# Total active wallets
aw = db.execute('SELECT COUNT(*) FROM wallets WHERE active=1').fetchone()[0]
print(f'\nTotal active tracked wallets: {aw}')
