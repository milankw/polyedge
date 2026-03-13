import sqlite3, json

db = sqlite3.connect('backend/data/polyedge.db')
db.row_factory = sqlite3.Row

settings = dict(db.execute('SELECT key, value FROM settings').fetchall())
print('=== ENGINE STATUS ===')
print(f'Mode: {settings.get("engine_mode", "?")}')
print(f'Bankroll: {settings.get("bankroll", "?")}')

total = db.execute('SELECT COUNT(*) FROM feed').fetchone()[0]
today = db.execute("SELECT COUNT(*) FROM feed WHERE timestamp > datetime('now', '-12 hours')").fetchone()[0]
print(f'Total feed entries: {total}')
print(f'Last 12h feed entries: {today}')

trades = db.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
print(f'Total trades placed: {trades}')

print('\n=== RECENT TRADES ===')
for r in db.execute('SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5').fetchall():
    print(dict(r))

print('\n=== FILTER FAILURES (last 12h) ===')
rows = db.execute("SELECT filter_results FROM feed WHERE timestamp > datetime('now', '-12 hours') AND filter_results IS NOT NULL").fetchall()
filter_counts = {}
passed_all = 0
for row in rows:
    try:
        fr = json.loads(row[0])
        all_pass = True
        for k, v in fr.items():
            if v == False:
                filter_counts[k] = filter_counts.get(k, 0) + 1
                all_pass = False
            elif v == 'PEND':
                pass
        if all_pass:
            passed_all += 1
    except:
        pass
print(f'Entries analyzed: {len(rows)}')
print(f'Passed ALL filters: {passed_all}')
for k, v in sorted(filter_counts.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v} failures')

wallets = db.execute('SELECT COUNT(*) FROM wallets WHERE active=1').fetchone()[0]
print(f'\nActive tracked wallets: {wallets}')

print('\n=== SCOUT STATUS ===')
queued = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status='queued'").fetchone()[0]
approved = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status='approved'").fetchone()[0]
rejected = db.execute("SELECT COUNT(*) FROM scout_candidates WHERE status='rejected'").fetchone()[0]
print(f'Queued: {queued}, Approved: {approved}, Rejected: {rejected}')

print('\n=== APPROVED SCOUT WALLETS ===')
for r in db.execute("SELECT address, alias, score, metrics FROM scout_candidates WHERE status='approved' ORDER BY score DESC").fetchall():
    m = r[3][:120] if r[3] else ''
    print(f'{r[1]} | score={r[2]} | {m}...')

mwc = settings.get('min_confirming_wallets', '?')
print(f'\nmin_confirming_wallets setting: {mwc}')

# Check if any entries came close to passing all
print('\n=== NEAR MISSES (failed only 1 filter, last 12h) ===')
rows2 = db.execute("SELECT wallet_address, market_slug, filter_results, timestamp FROM feed WHERE timestamp > datetime('now', '-12 hours') AND filter_results IS NOT NULL ORDER BY timestamp DESC").fetchall()
for row in rows2:
    try:
        fr = json.loads(row[2])
        failed = [k for k,v in fr.items() if v == False]
        if len(failed) == 1:
            print(f'  Wallet: {row[0][:12]}... | Market: {row[1][:50]} | Failed only: {failed[0]} | Time: {row[3]}')
    except:
        pass
