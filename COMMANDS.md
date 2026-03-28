# SWARM Quick Reference — Handy Commands

## Start / Stop

```bash
# Start the infer loop (runs every 5 min, survives terminal close)
nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &

# Check if loop is running
pgrep -af live_runner.py

# Stop all loops
pkill -f live_runner.py
```

---

## Monitor Live Output

```bash
# Watch the loop log in real time
tail -f logs/infer_loop.log

# Last 20 lines of log
tail -20 logs/infer_loop.log
```

---

## What Did Claude Just Evaluate?

```bash
python3 -c "
import json
d = json.load(open('signals/infer_diagnostics.json'))
print('Last run:', d.get('ts_utc',''))
print('Settings:', d.get('settings'))
print()
print(f'{\"Market\":<45} {\"Decision\":<8} {\"Edge\":>6} {\"Disagree\":>9} Reason')
print('-'*80)
for r in d.get('rows', []):
    print(f'{r.get(\"slug\",\"\")[:45]:<45} {r.get(\"decision\",\"\"):<8} {r.get(\"edge_abs\",0):>6.3f} {r.get(\"disagreement\",0):>9.3f} {r.get(\"reason\",\"\")}')
"
```

---

## View Paper Trades

```bash
# Recent trades (quick summary)
python3 -c "
import sqlite3
conn = sqlite3.connect('memory/runs.sqlite')
rows = conn.execute('SELECT market_id, side, p_yes, edge, ts_utc FROM paper_trades ORDER BY ts_utc DESC LIMIT 10').fetchall()
for r in rows: print(r)
"

# Recent trades with Claude rationale (post-fix trades only)
python3 -c "
import sqlite3, json
conn = sqlite3.connect('memory/runs.sqlite')
rows = conn.execute(\"SELECT market_id, side, p_yes, edge, ts_utc, notes FROM paper_trades WHERE ts_utc > '2026-03-28T21:00' ORDER BY ts_utc DESC\").fetchall()
for market_id, side, p_yes, edge, ts_utc, notes in rows:
    print('='*60)
    print(f'TRADE PLACED: {ts_utc}')
    print(f'Market : {market_id}')
    print(f'Side   : {side}')
    print(f'p_yes  : {p_yes:.3f}  (Claude model price)')
    if notes:
        n = json.loads(notes) if isinstance(notes, str) else notes
        mkt = n.get('p_yes_market', '?')
        if isinstance(mkt, float): print(f'Market : {mkt:.3f}  (crowd price)')
        print(f'Edge   : {edge:.3f}  ({edge*100:.1f}%)')
        llm = n.get('llm', {})
        print(f'Claude confidence: {llm.get(\"confidence\",\"?\")}')
        print(f'Rationale: {llm.get(\"rationale\",\"none\")}')
    print()
"
```

---

## Sync Latest Code from GitHub

```bash
git pull origin claude/review-blackglass-lab-XCVFO
```

---

## Restart Loop Clean (after code updates)

```bash
pkill -f live_runner.py
sleep 2
git pull origin claude/review-blackglass-lab-XCVFO
nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &
echo "Loop restarted. PID: $!"
```

---

## View Current Watchlist

```bash
cat markets/polymarket_watchlist.json
```

---

## Check Settings (confirm env vars are correct)

```bash
python3 -c "
import json
d = json.load(open('signals/infer_diagnostics.json'))
print(d.get('settings'))
"
```

Expected output:
```
{'batch': 10, 'cooldown': 15, 'max_disagree': 0.45, 'min_edge_abs': 0.04, 'min_edge_vs_market': 0.04, 'paper_size': 100.0}
```

---

## Key Settings Explained

| Setting | Value | Meaning |
|---|---|---|
| `max_disagree` | 0.45 | Claude must be >55% confident to trade |
| `min_edge_abs` | 0.04 | Claude must disagree with market by >4% |
| `batch` | 10 | Markets evaluated per 5-min cycle |
| `cooldown` | 15 | Skip recently traded markets for 15 cycles |
| `paper_size` | $100 | Size of each paper trade |
