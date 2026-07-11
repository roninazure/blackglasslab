# Restart Readiness

Classification: `READY_WITH_MANUAL_MONITORING`

## Remaining Blockers

- Controlled validation in this sandbox hit `fetch_failed` on the live infer path, so live network fetch behavior was not fully re-proven here.
- `git diff --check` still reports an existing trailing-whitespace issue in `swarm_edge_flyer.pdf`, which is outside this cleanup pass.
- The daemon is still stopped by design.

## Safe Defaults

- `SWARM_EDGE_PUBLISH_ENABLED=0`
- `SWARM_EDGE_WATCHLIST_APPLY=0`
- `PYTHON_BIN=.venv/bin/python`

## Safe Startup

```bash
SWARM_EDGE_PUBLISH_ENABLED=0 SWARM_EDGE_WATCHLIST_APPLY=0 nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &
```

## Monitoring

```bash
tail -f logs/infer_loop.log
python3 scripts/morning_status.py
```

## Shutdown

```bash
pkill -f run_live.sh && pkill -f live_runner.py
```

## Expected Per-Cycle Outputs

- `signals/infer_pipeline_report.json`
- `signals/infer_diagnostics.json`
- `signals/trade_candidates_infer.json`
- `logs/infer_loop.log`
- `memory/runs.sqlite` cursor updates and, only when paper trades are actually inserted or resolved, legitimate trade-row changes
- `data/paper_trades.json` and `data/infer_diagnostics.json` on export cycles

## Files That Must Not Change

- `markets/polymarket_watchlist.json` unless watchlist apply is explicitly enabled
- `.env`
- historical SQLite rows beyond the legitimate cursor and paper-trade workflow updates
- archived validation evidence
- control documents and tests

## First Runtime Window

- Run for 2 hours, or until one full cycle is observed cleanly and one follow-up summary confirms matching SQLite-backed counts.

## Rollback

1. Stop the loop with `pkill -f run_live.sh && pkill -f live_runner.py`.
2. Inspect `logs/infer_loop.log` and the latest `signals/` artifacts.
3. Verify `memory/runs.sqlite` fingerprint and row counts.
4. Leave historical rows intact; only discard generated snapshots if they are inconsistent.
5. Re-run the validation commands against a temp database copy if the fetch failure repeats.
