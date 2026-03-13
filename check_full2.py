import sqlite3, json

db = sqlite3.connect('backend/data/polyedge.db')
db.row_factory = sqlite3.Row

# Trades table schema
print('=== TRADES TABLE SCHEMA ===')
for r in db.execute("PRAGMA table_info(trades)").fetchall():
    print(f'  {r[1]} ({r[2]})')

# All trades
print('\n=== ALL TRADES ===')
for r in db.execute('SELECT * FROM trades ORDER BY rowid DESC LIMIT 10').fetchall():
    print(dict(r))

# Feed analysis last 12h
rows = db.execute("SELECT id, wallet_address, market_slug, detected_at FROM copy_feed WHERE detected_at > datetime('now', '-12 hours') ORDER BY detected_at DESC").fetchall()
print(f'\n=== FEED ANALYSIS (last 12h: {len(rows)} entries) ===')

filter_fail_counts = {}
passed_all_count = 0
near_misses = []
entries_with_filters = 0

for feed_row in rows:
    feed_id = feed_row[0]
    filters = db.execute('SELECT filter_name, status, actual_value, fail_message FROM filter_results WHERE copy_feed_id = ?', (feed_id,)).fetchall()
    if not filters:
        continue
    entries_with_filters += 1
    
    failed_filters = [(f[0], f[2], f[3]) for f in filters if f[1] == 'failed']
    
    for ff in failed_filters:
        filter_fail_counts[ff[0]] = filter_fail_counts.get(ff[0], 0) + 1
    
    if len(failed_filters) == 0:
        passed_all_count += 1
    elif len(failed_filters) == 1:
        near_misses.append({
            'wallet': feed_row[1][:16],
            'market': feed_row[2][:60] if feed_row[2] else '?',
            'failed': failed_filters[0][0],
            'actual': failed_filters[0][1],
            'msg': failed_filters[0][2],
            'time': feed_row[3]
        })

print(f'Entries with filter results: {entries_with_filters}')
print(f'Passed ALL filters: {passed_all_count}')
print(f'Near misses (failed only 1): {len(near_misses)}')

print('\nFilter failure counts:')
for k, v in sorted(filter_fail_counts.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v}')

if near_misses:
    print('\n=== NEAR MISSES ===')
    for nm in near_misses[:20]:
        print(f'  {nm["wallet"]}... | {nm["failed"]}: {nm["actual"]} ({nm["msg"]}) | {nm["time"]}')

# Scout
print('\n=== SCOUT ===')
for status in ['queued','approved','rejected']:
    c = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status=?", (status,)).fetchone()[0]
    print(f'  {status}: {c}')

print('\n=== APPROVED WALLETS ===')
for r in db.execute("SELECT address, alias, score, metrics FROM scout_candidates WHERE status='approved' ORDER BY score DESC").fetchall():
    try:
        m = json.loads(r[3]) if r[3] else {}
    except:
        m = {}
    print(f'  {r[1]} (score={r[2]})')

# Total wallets active
aw = db.execute('SELECT COUNT(*) FROM wallets WHERE active=1').fetchone()[0]
print(f'\nActive tracked wallets: {aw}')

# Check copy_feed table schema
print('\n=== COPY_FEED SCHEMA ===')
for r in db.execute("PRAGMA table_info(copy_feed)").fetchall():
    print(f'  {r[1]} ({r[2]})')

# Check most recent feed entries
print('\n=== MOST RECENT FEED ENTRIES ===')
for r in db.execute('SELECT id, wallet_address, market_slug, side, amount_usd, detected_at FROM copy_feed ORDER BY id DESC LIMIT 5').fetchall():
    print(f'  #{r[0]} | {r[1][:16]}... | {r[2][:40]} | {r[3]} | ${r[4]} | {r[5]}')
