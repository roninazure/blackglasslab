# Swarm Edge Architecture

## Runtime Data Flow

1. **Discovery and watchlist:** `scripts/manage_watchlist.py` validates current slugs and discovers active Polymarket markets. The runtime watchlist is `markets/polymarket_watchlist.json`.
2. **Adapter:** `adapters/registry.py` selects `adapters/polymarket_adapter.py`, which resolves an exact market or event slug through Gamma. Numeric market-ID lookup supports resolver recovery.
3. **Inference runner:** `live_runner.py` loads and deduplicates the watchlist, applies the rotating batch cursor and cooldown, blocks existing OPEN/PENDING positions, fetches snapshots, and runs market-quality filters from `models/baseline.py`.
4. **Filters:** `models/baseline.py` checks active/closed state, liquidity, volume, and spread. It calculates time to resolution as a scoring feature; it does not currently enforce a minimum-time rejection gate. `live_runner.py` applies price, tail, edge, disagreement, and category-cap checks.
5. **LLM inference:** `llm/openai_client.py` is a compatibility shim to `llm/claude_client.py`. Claude returns model probability, confidence, and rationale; failures fall back to the deterministic baseline.
6. **Candidates and approval:** Passing inference produces at most one candidate in `signals/trade_candidates_infer.json`. With `--paper`, `live_runner.py` inserts it as PENDING by default. `scripts/approve_trades.py` is the explicit operator approval/rejection interface.
7. **Persistence:** SQLite `memory/runs.sqlite` stores runs, agent results, key/value cursor state, and `paper_trades`. No real-money order path exists.
8. **Resolution:** `scripts/resolve_paper_trades.py` checks OPEN Polymarket trades by slug, then `notes.snapshot.id`, and applies conservative binary-price resolution and Brier/P&L scoring. `--dry-run` performs no trade updates.
9. **Reporting:** `signals/infer_diagnostics.json` preserves evaluated-market diagnostics. `signals/infer_pipeline_report.json` records the complete per-run watchlist funnel. `reporting/`, `scripts/morning_status.py`, and `scripts/export_data.py` provide terminal and export views.
10. **Dashboard:** `dashboard/app.py` is a Streamlit view over SQLite/exported data and inference diagnostics.

`scripts/run_live.sh` orchestrates periodic inference, resolution, export, and discovery. It is an operator-started process and is not started by application code.
