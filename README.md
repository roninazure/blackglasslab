<div align="center">

<br/>

<sub>AUTONOMOUS PREDICTION MARKET ENGINE</sub>

# Swarm Edge

<p>An AI committee that debates the future, finds mispriced odds, and trades the difference.</p>

<br/>

<img src="https://img.shields.io/badge/live-polymarket-000?style=flat-square&labelColor=000&color=00ff88&logoColor=white" />
&nbsp;
<img src="https://img.shields.io/badge/phase_3-llm_reasoning-000?style=flat-square&labelColor=000&color=6366f1" />
&nbsp;
<img src="https://img.shields.io/badge/mode-paper_trading-000?style=flat-square&labelColor=000&color=334155" />
&nbsp;
<img src="https://img.shields.io/badge/edge_threshold-≥_5%25-000?style=flat-square&labelColor=000&color=334155" />

<br/><br/>

<table>
<tr>
<td align="center" width="140"><strong>20</strong><br/><sub>markets tracked</sub></td>
<td align="center" width="140"><strong>4</strong><br/><sub>open positions</sub></td>
<td align="center" width="140"><strong>60 min</strong><br/><sub>cycle time</sub></td>
<td align="center" width="140"><strong>$100</strong><br/><sub>per trade</sub></td>
</tr>
</table>

<br/>

</div>

---

Prediction markets are probability exchanges. Every market is a binary question with a price — *Will X happen?* That price is the crowd's estimate.

Swarm Edge runs an adversarial AI committee against those prices. Operators make the case. Skeptics attack it. An arbiter resolves the dispute into a consensus probability. When that view diverges from the live market by enough edge — and agents agree — a trade fires.

The endgame is real capital. Paper trading earns it.

---

## Signal Pipeline

```
  WATCHLIST (20 verified Polymarket slugs)
       │
       ▼
  INFER LOOP  ·  every 60 min
       │
       ├─  fetch live prices  ·  Polymarket Gamma API
       ├─  Operators   →  independent p_yes per market
       ├─  Skeptics    →  challenge every assumption
       └─  Arbiter     →  consensus_p_yes  +  disagreement
                │
                ├─  edge < 0.05          →  skip
                ├─  disagreement > 0.45  →  skip
                └─  edge ≥ 0.05          →  paper trade  [$100]
                         │
                         ▼
                RESOLVER  ·  on demand
                fetch outcome  →  Brier score  +  P&L
```

---

## Filters

| | THRESHOLD | PURPOSE |
|-|-----------|---------|
| `min_edge_abs` | ≥ 0.05 | Model must diverge from market by 5%+ |
| `max_disagreement` | ≤ 0.45 | Agents must agree — no split decisions |
| `min_hours_to_resolution` | ≥ 24h | Skip markets closing imminently |
| `paper_size` | $100 | Fixed sizing for clean scoring |

---

## Markets

```
  US POLITICS    blue wave 2026  ·  tariff refund ruling
  MACRO / FED    recession 2026  ·  0–5 rate cut scenarios
  CRYPTO         eth flips btc   ·  btc $150k by june
  SPORTS         masters 2026    ·  nba finals 2026
```

---

## Roadmap

| | PHASE | STATUS |
|-|-------|--------|
| ① | Engine core | ✅ complete |
| ② | Polymarket live pricing | ✅ complete |
| ③ | LLM reasoning layer | ✅ active |
| ④ | Brier scoring + resolution | 🔄 in progress |
| ⑤ | Kalshi expansion | planned |
| ⑥ | Real capital allocation | pending proof of edge |

---

## Run It

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
python3 scripts/resolve_paper_trades.py && python3 scripts/watch_resolutions.py
```

---

## Files

| FILE | PURPOSE |
|------|---------|
| `live_runner.py` | Core engine |
| `scripts/run_live.sh` | Infer loop daemon |
| `scripts/resolve_paper_trades.py` | Resolve closed trades · Brier score |
| `scripts/watch_resolutions.py` | P&L summary |
| `markets/polymarket_watchlist.json` | 20 verified active slugs |
| `memory/runs.sqlite` | Trades · runs · forecasts |

---

<div align="center">
<sub>Swarm Edge · Phase 3 · Paper Trading · April 2026</sub>
</div>
