import sqlite3, json

db = sqlite3.connect('backend/data/polyedge.db')
db.row_factory = sqlite3.Row

# Copy trade settings (filter thresholds)
print('=== COPY TRADE SETTINGS ===')
cts = dict(db.execute('SELECT key, value FROM copy_trade_settings').fetchall())
for k,v in sorted(cts.items()):
    print(f'  {k}: {v}')

# Engine mode from settings
mode = cts.get('engine_mode', '?')
print(f'\nEngine mode: {mode}')

# Total feed
total = db.execute('SELECT COUNT(*) FROM copy_feed').fetchone()[0]
recent = db.execute("SELECT COUNT(*) FROM copy_feed WHERE detected_at > datetime('now', '-12 hours')").fetchone()[0]
print(f'\nTotal feed entries: {total}')
print(f'Last 12h entries: {recent}')

# Trades placed
trades_total = db.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
trades_recent = db.execute("SELECT COUNT(*) FROM trades WHERE timestamp > datetime('now', '-12 hours')").fetchone()[0]
print(f'Total trades placed: {trades_total}')
print(f'Trades last 12h: {trades_recent}')

# Recent trades
print('\n=== RECENT TRADES (last 5) ===')
for r in db.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5').fetchall():
    print(dict(r))

# Filter analysis on recent feed entries
print('\n=== FILTER ANALYSIS (last 12h feed entries) ===')
rows = db.execute("SELECT id, wallet_address, market_slug, detected_at FROM copy_feed WHERE detected_at > datetime('now', '-12 hours') ORDER BY detected_at DESC").fetchall()
print(f'Feed entries to analyze: {len(rows)}')

filter_fail_counts = {}
passed_all_count = 0
near_misses = []

for feed_row in rows:
    feed_id = feed_row[0]
    filters = db.execute('SELECT filter_name, status FROM filter_results WHERE copy_feed_id = ?', (feed_id,)).fetchall()
    if not filters:
        continue
    
    failed_filters = [f[0] for f in filters if f[1] == 'failed']
    passed_filters = [f[0] for f in filters if f[1] == 'passed']
    pending_filters = [f[0] for f in filters if f[1] == 'pending']
    
    for ff in failed_filters:
        filter_fail_counts[ff] = filter_fail_counts.get(ff, 0) + 1
    
    if len(failed_filters) == 0:
        passed_all_count += 1
    elif len(failed_filters) == 1:
        near_misses.append({
            'wallet': feed_row[1][:16],
            'market': feed_row[2][:60] if feed_row[2] else '?',
            'failed': failed_filters[0],
            'time': feed_row[3]
        })

print(f'\nPassed ALL filters: {passed_all_count}')
print(f'Near misses (failed only 1 filter): {len(near_misses)}')
print('\nFilter failure counts:')
for k, v in sorted(filter_fail_counts.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v}')

if near_misses:
    print('\n=== NEAR MISSES (failed only 1 filter) ===')
    for nm in near_misses[:15]:
        print(f'  {nm["wallet"]}... | {nm["market"]} | Failed: {nm["failed"]} | {nm["time"]}')

# Active wallets
aw = db.execute('SELECT COUNT(*) FROM wallets WHERE active=1').fetchone()[0]
print(f'\nActive tracked wallets: {aw}')

# Scout
print('\n=== SCOUT STATUS ===')
for status in ['queued','approved','rejected']:
    c = db.execute(f"SELECT COUNT(*) FROM scout_candidates WHERE status=?", (status,)).fetchone()[0]
    print(f'  {status}: {c}')

print('\n=== APPROVED SCOUT WALLETS ===')
for r in db.execute("SELECT address, alias, score, metrics FROM scout_candidates WHERE status='approved' ORDER BY score DESC").fetchall():
    m = json.loads(r[3]) if r[3] else {}
    print(f'  {r[1]} (score={r[2]}): PnL=${m.get("pnl","?")}, ROI={m.get("roi","?")}%, WR={m.get("win_rate","?")}%')

# Loop log
print('\n=== RECENT LOOP LOG ===')
for r in db.execute('SELECT * FROM loop_log ORDER BY timestamp DESC LIMIT 5').fetchall():
    print(dict(r))
