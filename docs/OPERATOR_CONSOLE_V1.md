# Swarm Edge Operator Console v1

The Operator Console is a read-only terminal interface for the live Revenue POC. It reads the shared runtime-path configuration, opens SQLite with `mode=ro` and `PRAGMA query_only=ON`, and never calls venue or model APIs.

## Commands

```sh
swarm-edge watch
swarm-edge portfolio
swarm-edge positions
swarm-edge revenue-status
```

`watch` opens the full-screen Textual console. Database state refreshes every two seconds and runtime logs refresh every five seconds. `watch --snapshot` renders a non-interactive status snapshot for validation or capture.

The one-shot commands print a portfolio summary, open/resolved position table, or combined system/portfolio/API/pipeline health report.

## Keyboard controls

| Key | View or action |
|---|---|
| `q` | Quit |
| `r` | Refresh displayed read-only sources |
| `p` | Portfolio |
| `t` | Positions |
| `m` | Market and pipeline |
| `a` | API economics |
| `l` | Event and runtime log feed |
| `e` | Evaluations and rejections |
| `s` | System health |
| `h` | Help |
| `/` | Search positions and markets |
| `↑` / `↓` | Navigate table rows |
| `Enter` | Open position detail |

No control can submit a trade, approve a candidate, restart the runner, apply a migration, or write portfolio state.

## Data and failure behavior

The console reads the production database, launchd job status, active deployment manifest/release pointer, pipeline report, and runtime logs through the existing runtime-path resolver. It does not hardcode checkout-relative state paths.

If a refresh cannot read SQLite, the last valid portfolio values remain visible and the database status changes to `UNAVAILABLE / LAST VALUE RETAINED`. Cycle and quote timestamps are shown with explicit age and `FRESH`, `STALE`, `NO MARK`, or `RESOLVED` labels. Unknown historical API cost remains `UNKNOWN`, never zero.
