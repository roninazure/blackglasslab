# Known Issues

## Resolved

- Inference pipeline now records a terminal reason for every watchlist market.
- Resolver fallback by `notes.snapshot.id` is implemented and validated.
- `run_live.sh` no longer auto-publishes runtime snapshots by default.
- Legacy summaries now parse resolver notes that contain a base JSON blob plus a resolver JSON line.
- `scripts/integrity_check.py` now reports positions from SQLite rather than stale exported JSON.
- Forecast prompts now carry explicit temporal context, and a post-response validator blocks stale or contradictory chronology before candidate creation.
- "Before GTA VI" markets are held out unless the runtime can establish verified temporal metadata; guessed release dates no longer reach the candidate queue.

## Open

- Watchlist/category concentration is still heuristic and can cluster by topic.
- Configuration remains scattered across shell defaults, `.env`, Python defaults, and helper scripts.
- Structured reporting is incomplete beyond the main runtime pipeline and calibration artifacts.
- Legacy reporting CLIs still overlap with the current dashboard and should be retired or reconciled deliberately.
- Calibration evidence is still too small to support an edge claim.
- Paper P&L remains theoretical gross P&L; fees, slippage, spread crossing, latency, and fills are not modeled.
- The runtime still performs sequential external calls, which is acceptable at current cadence but not yet optimized for scale.
- Temporal validation is intentionally conservative and may reject future markets that present ambiguous chronology in the model rationale.
