# Known Issues

## Resolved

- Inference pipeline now records a terminal reason for every watchlist market.
- Resolver fallback by `notes.snapshot.id` is implemented and validated.
- `run_live.sh` no longer auto-publishes runtime snapshots by default.
- Legacy summaries now parse resolver notes that contain a base JSON blob plus a resolver JSON line.
- `scripts/integrity_check.py` now reports positions from SQLite rather than stale exported JSON.
- Forecast prompts now carry explicit temporal context, and a post-response validator blocks stale or contradictory chronology before candidate creation.
- "Before GTA VI" markets are held out unless the runtime can establish verified temporal metadata; guessed release dates no longer reach the candidate queue.
- Infer batches are ranked before LLM use, with per-cycle and daily call caps plus structured budget-skip reasons.
- Prompt families and the bounded skeptic pass now cover the major market categories.
- Latest-cycle brain activity is available as structured JSON and in the morning status summary.

## Open

- Watchlist/category concentration is still heuristic and can cluster by topic.
- Opportunity-score weights are heuristic until enough resolved paper forecasts exist for calibration.
- Daily LLM usage is a local JSON counter designed for one runner process; it is not a transactional multi-process ledger.
- Per-call cost is unavailable unless `BGL_ESTIMATED_COST_PER_CALL_USD` is configured.
- The category classifier is keyword-based and can misroute novel wording.
- The brain report is latest-cycle state only; historical dashboard storage and streaming are not implemented.
- Legacy reporting CLIs still overlap with the current dashboard and should be retired or reconciled deliberately.
- Calibration evidence is still too small to support an edge claim.
- Paper P&L remains theoretical gross P&L; fees, slippage, spread crossing, latency, and fills are not modeled.
- The runtime still performs sequential external calls, which is acceptable at current cadence but not yet optimized for scale.
- Temporal validation is intentionally conservative and may reject future markets that present ambiguous chronology in the model rationale.
