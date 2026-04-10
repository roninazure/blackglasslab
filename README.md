<div align="center">

<br/>

<h1>SWARM EDGE</h1>

<p><strong>AI Prediction Market Trading Engine</strong></p>

[![STATUS](https://img.shields.io/badge/STATUS-LIVE-00ff88?style=flat-square&labelColor=0d1117)](.)
[![MODE](https://img.shields.io/badge/MODE-PAPER_TRADING-6366f1?style=flat-square&labelColor=0d1117)](.)
[![VENUE](https://img.shields.io/badge/VENUE-POLYMARKET-e879f9?style=flat-square&labelColor=0d1117)](.)
[![PHASE](https://img.shields.io/badge/PHASE-3_LLM_ACTIVE-f59e0b?style=flat-square&labelColor=0d1117)](.)

<br/>

*An AI committee that debates the future, finds mispriced odds, and trades the difference.*

<br/>

</div>

---

## What It Does

Swarm Edge is a paper-trading engine for prediction markets. It runs adversarial AI reasoning on binary markets — independent forecasters vs skeptics — and places a trade when the consensus diverges from the crowd by enough edge.

**The thesis:** If an AI committee can estimate probabilities better than a prediction market, the gap is exploitable.

**The endgame:** Proven Brier score edge → real capital allocation.

---

## Signal Pipeline

```
  WATCHLIST (20 verified Polymarket slugs)
       │
       ▼
  INFER LOOP  [every 60 min]
       │
       ├─ Fetch live prices from Polymarket Gamma API
       ├─ Claude Haiku reasons on each market
       ├─ Operators → independent p_yes estimates
       ├─ Skeptics  → challenge every assumption
       └─ Arbiter   → consensus_p_yes + disagreement score
              │
              ├─ edge < 0.05          →  SKIP
              ├─ disagreement > 0.45  →  SKIP (agents too split)
              └─ edge ≥ 0.05          →  PAPER TRADE  [$100]
                       │
                       ▼
              RESOLVER  [on demand]
              Fetch outcome → Brier score + P&L
```

---

## Trade Filters

| FILTER | VALUE | REASON |
|--------|-------|--------|
| `min_edge_abs` | ≥ 0.05 | Minimum model vs market gap |
| `max_disagreement` | ≤ 0.45 | Skip if agents are split |
| `min_hours_to_resolution` | ≥ 24h | No last-minute noise |
| `paper_size` | $100 | Fixed for fair Brier comparison |

---

## Active Markets

| CATEGORY | MARKETS |
|----------|---------|
| US Politics | Blue wave 2026 · Tariff refund ruling |
| Macro / Fed | Recession 2026 · 0–5 rate cut scenarios |
| Crypto | ETH flips BTC · BTC $150k by June |
| Sports | Masters 2026 (live) · NBA Finals 2026 |

---

## Roadmap

| PHASE | STATUS | OBJECTIVE |
|-------|--------|-----------|
| **① Engine** | ✅ Done | Core loop · SQLite · Brier scoring |
| **② Polymarket** | ✅ Done | Live prices · real slugs · paper trades |
| **③ LLM Reasoning** | ✅ Active | Claude Haiku on every market |
| **④ Scoring** | 🔄 In Progress | Resolve trades · measure calibration |
| **⑤ Kalshi** | 📋 Planned | Second venue · broader universe |
| **⑥ Real Capital** | 💰 Pending | Proven edge → live allocation |

---

## Commands

```bash
cd ~/blackglasslab

# Start the loop
nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &

# Watch it run
tail -f logs/infer_loop.log

# Check last evaluation
python3 -c "
import json
d = json.load(open('signals/infer_diagnostics.json'))
print('Last run:', d.get('ts_utc'))
for r in d.get('rows', []):
    print(r.get('slug','')[:45], r.get('decision'), 'edge='+str(round(r.get('edge_abs',0),3)), r.get('reason',''))
"

# View trades
python3 -c "
import sqlite3
conn = sqlite3.connect('memory/runs.sqlite')
rows = conn.execute('SELECT ts_utc, market_id, side, edge, size_usd FROM paper_trades ORDER BY ts_utc DESC').fetchall()
print(len(rows), 'trades')
for r in rows: print(r)
"

# Resolve closed trades + compute P&L
python3 scripts/resolve_paper_trades.py

# P&L summary
python3 scripts/watch_resolutions.py
```

---

## Key Files

| FILE | PURPOSE |
|------|---------|
| `live_runner.py` | Core engine |
| `scripts/run_live.sh` | Infer loop daemon |
| `scripts/resolve_paper_trades.py` | Resolve + score closed trades |
| `scripts/watch_resolutions.py` | P&L summary |
| `markets/polymarket_watchlist.json` | 20 verified active markets |
| `memory/runs.sqlite` | All trades, runs, forecasts |

---

<div align="center">

<br/>

`SWARM EDGE  ·  PHASE 3  ·  PAPER TRADING  ·  APRIL 2026`

</div>
