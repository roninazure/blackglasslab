<div align="center">

<br/>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://readme-typing-svg.demolab.com?font=Share+Tech+Mono&size=13&duration=3000&pause=1000&color=00FF88&center=true&vCenter=true&width=600&lines=INITIALIZING+SWARM+ENGINE...;FETCHING+POLYMARKET+PRICES...;RUNNING+LLM+CONSENSUS...;EDGE+DETECTED+%E2%80%94+PAPER+TRADE+FILED." />
  <img src="https://readme-typing-svg.demolab.com?font=Share+Tech+Mono&size=13&duration=3000&pause=1000&color=00FF88&center=true&vCenter=true&width=600&lines=INITIALIZING+SWARM+ENGINE...;FETCHING+POLYMARKET+PRICES...;RUNNING+LLM+CONSENSUS...;EDGE+DETECTED+%E2%80%94+PAPER+TRADE+FILED." alt="Typing SVG" />
</picture>

<br/>

<h1>
  <span>SWARM</span><span style="color:#00e5ff;">_</span><span>EDGE</span>
</h1>

<p><code>AI · PREDICTION MARKETS · AUTONOMOUS PAPER TRADING</code></p>

<br/>

<img src="https://img.shields.io/badge/◉_LIVE-POLYMARKET-00ff88?style=for-the-badge&labelColor=0d1117&color=00ff88" />
&nbsp;
<img src="https://img.shields.io/badge/PHASE-3__LLM__ACTIVE-00e5ff?style=for-the-badge&labelColor=0d1117&color=00e5ff" />
&nbsp;
<img src="https://img.shields.io/badge/MODE-PAPER__TRADING-6366f1?style=for-the-badge&labelColor=0d1117" />
&nbsp;
<img src="https://img.shields.io/badge/EDGE__THRESHOLD-≥_5%25-f59e0b?style=for-the-badge&labelColor=0d1117" />

<br/><br/>

```
╔══════════════════════════════════════════════════════════════════════╗
║  MARKETS TRACKED  │  ACTIVE TRADES  │  CYCLE TIME  │  PAPER SIZE   ║
║       20          │       4         │   60  min    │    $100 / bet ║
╚══════════════════════════════════════════════════════════════════════╝
```

<br/>

> **`"If you can model the future better than the crowd, you can trade the difference."`**

<br/>

</div>

---

## `// SYSTEM OVERVIEW`

Swarm Edge is an autonomous prediction market engine. It deploys an adversarial AI committee against live binary markets on Polymarket — pitting independent forecasters against skeptics — and fires a paper trade only when conviction is high and edge is real.

This is not a demo. The loop runs every 60 minutes. Every evaluation is logged. Every trade is scored.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                    SWARM EDGE — SIGNAL FLOW                     │
  ├─────────────────────────────────────────────────────────────────┤
  │                                                                 │
  │   WATCHLIST  ──▶  LIVE PRICES (Gamma API)  ──▶  LLM COMMITTEE  │
  │                                                        │        │
  │                              ┌─────────────────────────┘        │
  │                              ▼                                  │
  │              ┌─── OPERATORS  →  independent p_yes               │
  │              ├─── SKEPTICS   →  challenge assumptions            │
  │              └─── ARBITER    →  consensus + disagreement score  │
  │                                        │                        │
  │                         edge ≥ 0.05 AND disagree ≤ 0.45?       │
  │                                        │                        │
  │                              YES ──────┴──▶  PAPER TRADE        │
  │                              NO  ──────────▶  SKIP              │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

## `// SIGNAL FILTERS`

| PARAMETER | THRESHOLD | FUNCTION |
|-----------|-----------|----------|
| `min_edge_abs` | `≥ 0.05` | Model probability must diverge from market by 5%+ |
| `max_disagreement` | `≤ 0.45` | Committee must be aligned — no split decisions |
| `min_hours_to_resolution` | `≥ 24h` | Avoid expiring markets |
| `paper_size` | `$100` | Fixed sizing for clean Brier score comparison |

---

## `// ACTIVE MARKET UNIVERSE`

```
  US POLITICS  ──  Blue wave 2026  ·  Tariff refund ruling
  MACRO / FED  ──  Recession 2026  ·  0 / 1 / 2 / 3 / 4 / 5 rate cuts
  CRYPTO       ──  ETH flips BTC   ·  BTC $150k by June 2026
  SPORTS       ──  Masters 2026    ·  NBA Finals 2026
```

---

## `// MISSION PHASES`

```
  ① ENGINE CORE       ████████████████████  COMPLETE
  ② POLYMARKET LIVE   ████████████████████  COMPLETE
  ③ LLM REASONING     ████████████████████  ACTIVE ◉
  ④ SCORING / BRIER   ████████░░░░░░░░░░░░  IN PROGRESS
  ⑤ KALSHI EXPANSION  ░░░░░░░░░░░░░░░░░░░░  PLANNED
  ⑥ REAL CAPITAL      ░░░░░░░░░░░░░░░░░░░░  PENDING PROOF
```

---

## `// DEPLOY`

```bash
cd ~/blackglasslab

# ── START ENGINE ─────────────────────────────────────────
nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &

# ── MONITOR ──────────────────────────────────────────────
tail -f logs/infer_loop.log

# ── LAST EVALUATION ──────────────────────────────────────
python3 -c "
import json
d = json.load(open('signals/infer_diagnostics.json'))
print('run:', d.get('ts_utc'))
for r in d.get('rows', []):
    print(f'  {r[\"slug\"][:40]:<40}  {r[\"decision\"]}  edge={r[\"edge_abs\"]:.3f}  {r[\"reason\"]}')
"

# ── VIEW TRADES ───────────────────────────────────────────
python3 -c "
import sqlite3
conn = sqlite3.connect('memory/runs.sqlite')
rows = conn.execute('SELECT ts_utc, market_id, side, edge, size_usd FROM paper_trades ORDER BY ts_utc DESC').fetchall()
print(len(rows), 'trades')
for r in rows: print(r)
"

# ── RESOLVE + SCORE ───────────────────────────────────────
python3 scripts/resolve_paper_trades.py
python3 scripts/watch_resolutions.py
```

---

## `// SYSTEM FILES`

| FILE | ROLE |
|------|------|
| `live_runner.py` | Core engine |
| `scripts/run_live.sh` | Infer loop daemon |
| `scripts/resolve_paper_trades.py` | Resolve closed trades + Brier score |
| `scripts/watch_resolutions.py` | P&L summary |
| `markets/polymarket_watchlist.json` | 20 verified active market slugs |
| `memory/runs.sqlite` | All trades · runs · forecasts |

---

<div align="center">

<br/>

```
▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
▓  SWARM EDGE  ·  PHASE 3  ·  PAPER TRADING LIVE  ·  APRIL 2026   ▓
▓  REAL CAPITAL IS THE ENDGAME                                     ▓
▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
```

</div>
