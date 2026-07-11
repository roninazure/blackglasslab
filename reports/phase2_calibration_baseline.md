# Phase 2 Calibration Baseline

- Evidence strength: **anecdotal_only**
- Resolved forecasts: `2`
- Included trade IDs: `[6, 13]`
- Excluded trades: `19`
- Model Brier: `0.168400`
- Market Brier: `0.176360`
- Brier skill vs market: `0.045136`
- Model log loss: `0.443852`
- Market log loss: `0.539773`
- Total theoretical gross P&L: `$-52.8330`
- Average P&L/trade: `$-26.4165`
- Hit rate: `0.500000`
- Average signed edge: `-0.190250`
- Average absolute edge: `0.190250`

> Two resolved trades are anecdotal only and are insufficient to establish forecasting or trading edge.

## Inventory

- Tables: `['agent_population', 'agent_runs', 'arbiter_runs', 'kv', 'paper_trades', 'runs']`
- Row counts: `{'agent_population': 24, 'agent_runs': 24, 'arbiter_runs': 4, 'kv': 1, 'paper_trades': 21, 'runs': 4}`
- Paper trade statuses: `{'CLOSED': 2, 'OPEN': 4, 'VOID': 15}`
- Forecasts table present: `false`

## Included Trades

| ID | Model | Category | Side | Outcome | Model p | Market p | Model Brier | Market Brier | P&L |
|---:|---|---|---|---|---:|---:|---:|---:|---:|
| 6 | claude-haiku-4-5-20251001 | unknown | NO | YES | 0.4200 | 0.5000 | 0.3364 | 0.2500 | -100.0000 |
| 13 | claude-haiku-4-5-20251001 | other | NO | NO | 0.0200 | 0.3205 | 0.0004 | 0.1027 | 47.1670 |

## Exclusions

| ID | Status | Exclusion |
|---:|---|---|
| 1 | VOID | status_void |
| 2 | OPEN | status_open_unresolved |
| 3 | OPEN | status_open_unresolved |
| 4 | OPEN | status_open_unresolved |
| 5 | VOID | status_void |
| 7 | VOID | status_void |
| 8 | VOID | status_void |
| 9 | VOID | status_void |
| 10 | VOID | status_void |
| 11 | VOID | status_void |
| 12 | VOID | status_void |
| 14 | OPEN | status_open_unresolved |
| 15 | VOID | status_void |
| 16 | VOID | status_void |
| 17 | VOID | status_void |
| 18 | VOID | status_void |
| 19 | VOID | status_void |
| 20 | VOID | status_void |
| 21 | VOID | status_void |

## Interpretation

P&L is theoretical gross paper P&L. Fees, slippage, spread crossing, latency, and fill risk are not modeled. Calibration buckets and confidence intervals are withheld because the sample has fewer than 10 resolved forecasts.
