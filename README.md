<div align="center">

<br/>

```
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║    ███████╗██╗    ██╗ █████╗ ██████╗ ███╗   ███╗                     ║
║    ██╔════╝██║    ██║██╔══██╗██╔══██╗████╗ ████║                     ║
║    ███████╗██║ █╗ ██║███████║██████╔╝██╔████╔██║                     ║
║    ╚════██║██║███╗██║██╔══██║██╔══██╗██║╚██╔╝██║                     ║
║    ███████║╚███╔███╔╝██║  ██║██║  ██║██║ ╚═╝ ██║                     ║
║    ╚══════╝ ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝  EDGE              ║
║                                                                       ║
║    AI PREDICTION MARKET ENGINE          STATUS: LIVE ●               ║
║    VENUE: POLYMARKET                    MODE:   PAPER TRADING        ║
║    COMPANY: SWARM AXIS                  CYCLE:  60 MIN               ║
║                                                                       ║
║    ┌─────────────┬─────────────┬─────────────┬─────────────┐         ║
║    │  MARKETS    │   TRADES    │    EDGE      │  BET SIZE   │         ║
║    │     15      │      6      │   ≥ 5.0%     │    $100     │         ║
║    └─────────────┴─────────────┴─────────────┴─────────────┘         ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
```

[![](https://img.shields.io/badge/●_LIVE-00ff88?style=flat-square&labelColor=0d1117)](.)
[![](https://img.shields.io/badge/POLYMARKET-0d1117?style=flat-square&labelColor=0d1117&color=334155)](.)
[![](https://img.shields.io/badge/PAPER_TRADING-0d1117?style=flat-square&labelColor=0d1117&color=334155)](.)
[![](https://img.shields.io/badge/PHASE_3-0d1117?style=flat-square&labelColor=0d1117&color=6366f1)](.)

<br/>

> *"If you can model the future better than the crowd, you can trade the difference."*

<br/>

</div>

---

## SYSTEM

Swarm Edge deploys an adversarial AI committee against live prediction markets. Operators forecast. Skeptics attack. An arbiter resolves. When the swarm's consensus diverges from the live market price by sufficient edge — and agents agree — it fires a paper trade.

Every evaluation is logged. Every trade is scored. The endgame is real capital.

---

## SIGNAL FLOW

```
  polymarket_watchlist.json  [20 verified slugs]
             │
             ▼
      INFER LOOP  ──  every 60 min
             │
             ├─  live price fetch  ──  Gamma API
             ├─  OPERATORS  ──  independent p_yes
             ├─  SKEPTICS   ──  challenge assumptions
             └─  ARBITER    ──  consensus + disagreement
                    │
                    ├──  edge < 0.05          →  SKIP
                    ├──  disagreement > 0.45  →  SKIP
                    └──  edge ≥ 0.05          →  TRADE [$100]
                                │
                                ▼
                         RESOLVER  [on demand]
                    outcome  →  brier score  →  P&L
```

---

## FILTERS

```
  min_edge_abs              ≥  0.05    model must beat market by 5%+
  max_disagreement          ≤  0.45    agents must align
  min_hours_to_resolution   ≥  24h     no expiring markets
  paper_size                =  $100    fixed for clean scoring
```

---

## MARKETS

```
  US POLITICS ──  blue wave 2026  ·  tariff refund ruling
  MACRO / FED ──  recession 2026  ·  0 / 1 / 2 / 3 / 4 / 5 rate cuts
  CRYPTO      ──  eth flips btc   ·  btc $150k by june 2026
  SPORTS      ──  masters 2026    ·  nba finals 2026
```

---

## ROADMAP

```
  [✓]  ENGINE CORE         complete
  [✓]  POLYMARKET LIVE     complete
  [✓]  LLM REASONING       active  ●
  [~]  BRIER SCORING       in progress
  [ ]  KALSHI              planned
  [ ]  REAL CAPITAL        pending proof of edge
```

---

## OPERATE

```bash
cd ~/blackglasslab

# start
nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &

# monitor
tail -f logs/infer_loop.log

# last evaluation
python3 -c "
import json
d = json.load(open('signals/infer_diagnostics.json'))
print('run:', d.get('ts_utc'))
for r in d.get('rows', []):
    print(f'  {r[\"slug\"][:42]:<42}  {r[\"decision\"]}  edge={r[\"edge_abs\"]:.3f}  {r[\"reason\"]}')
"

# view trades
python3 -c "
import sqlite3
conn = sqlite3.connect('memory/runs.sqlite')
rows = conn.execute('SELECT ts_utc, market_id, side, edge, size_usd FROM paper_trades ORDER BY ts_utc DESC').fetchall()
print(len(rows), 'trades'); [print(r) for r in rows]
"

# resolve + score
python3 scripts/resolve_paper_trades.py
python3 scripts/watch_resolutions.py
```

---

## FILES

```
  live_runner.py                     core engine
  scripts/run_live.sh                infer loop daemon
  scripts/resolve_paper_trades.py    resolve closed trades · brier score
  scripts/watch_resolutions.py       P&L summary
  markets/polymarket_watchlist.json  20 verified active slugs
  memory/runs.sqlite                 trades · runs · forecasts
```

---

<div align="center">

```
  SWARM EDGE  ·  SWARM AXIS  ·  PAPER TRADING LIVE  ·  APRIL 2026
  REAL CAPITAL IS THE ENDGAME
```

</div>
