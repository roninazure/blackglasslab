# Known Issues

- Inference pipeline lacked complete stage-level observability. Resolved in Phase 1 and validated across one complete 25-market rotation.
- Resolver lacked a numeric market-ID fallback. Resolved and live-validated through Gamma `snapshot_id` lookup.
- Historical trades 6 and 13 were orphaned by slug lookup. Resolved through `notes.snapshot.id` and closed with venue-confirmed outcomes in Phase 1.
- Watchlist/category concentration is weak; category labeling is heuristic and differs slightly between discovery and inference.
- Configuration is scattered across environment variables, shell defaults, and Python defaults.
- Structured reporting is incomplete beyond the latest-run pipeline and diagnostics artifacts.
- Inference intentionally evaluates a rotating batch and generates at most one candidate per run; this limits coverage latency.
- LLM failure falls back to the baseline, so a candidate can be generated without LLM output; the pipeline report records this condition.
- Time to resolution affects baseline confidence but is not currently a hard rejection despite legacy documentation implying a minimum-hours filter.
- The integrity checker reports OPEN exposure using logic that includes CLOSED trades; its displayed `6 open` conflicts with the database's verified `4 OPEN` rows.
