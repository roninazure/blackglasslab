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
- Default discovery now enforces the `institutional_v1` market-universe policy and refuses junk backfill.
- GTA VI/product comparisons, entertainment/album markets, memes, thin local primaries, malformed questions, and weak-resolution markets are rejected before LLM use.

## Open

- The strict policy selected only 3 of 1,992 scanned active markets. This is safer than junk fill but limits opportunity breadth and category coverage.
- Category and malformed-question detection remain keyword/grammar heuristics and can produce false positives or miss novel wording.
- Existing OPEN/PENDING historical positions can belong to classes now banned; they are preserved and excluded from new selection rather than rewritten.
- Universe discovery depends on current Gamma API metadata and does not yet retain a replayable candidate snapshot beyond the rebuild report.
- Opportunity-score weights are heuristic until enough resolved paper forecasts exist for calibration.
- Daily LLM usage is a local JSON counter designed for one runner process; it is not a transactional multi-process ledger.
- Per-call cost is unavailable unless `BGL_ESTIMATED_COST_PER_CALL_USD` is configured.
- The brain report is latest-cycle state only; historical dashboard storage and streaming are not implemented.
- Legacy reporting CLIs still overlap with the current dashboard and should be retired or reconciled deliberately.
- Calibration evidence is still too small to support an edge claim.
- Paper P&L remains theoretical gross P&L; fees, slippage, spread crossing, latency, and fills are not modeled.
- The runtime still performs sequential external calls, which is acceptable at current cadence but not yet optimized for scale.
- Temporal validation is intentionally conservative and may reject future markets that present ambiguous chronology in the model rationale.
