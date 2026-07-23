# Phase 3 Loop Engine v1

## Design summary

Phase 3 changes inference from batch-order scanning to a ranked opportunity loop. The runtime still rotates through the watchlist, but it now fetches the full sampled batch, rejects unsafe or structurally weak markets, computes a deterministic opportunity score, and sorts eligible markets before any LLM call. It evaluates ranked markets until one candidate survives or the configured budget is exhausted.

The SQLite schema is unchanged. Only explicit `--paper` runs advance the infer cursor or insert a paper trade. Paper inserts continue through the existing approval gate and default to `PENDING`. Non-paper inference writes diagnostics and JSON brain activity only.

## Opportunity scoring

The score is 0-100 and produces grades `A` through `F`.

| Component | Maximum | Purpose |
| --- | ---: | --- |
| Liquidity quality | 18 | Log-scaled depth above the existing minimum |
| Volume quality | 14 | Log-scaled evidence of market participation |
| Spread quality | 14 | Rewards tighter executable pricing |
| Probability band | 12 | Penalizes extreme-tail markets |
| Resolution horizon | 10 | Favors nearer, still-live resolution windows |
| Category quality | 10 | Prioritizes researchable domains |
| Temporal metadata | 8 | Rewards verified deadlines |
| Novelty quality | 6 to -14 | Applies a 20-point differential against novelty or malformed-market risk |
| Exposure quality | 8 | Penalizes existing category exposure |

Existing inactive, closed, liquidity, volume, spread, extreme-tail, temporal, duplicate, cooldown, and category-cap controls remain hard gates. The primary new pre-inference reasons are `low_opportunity_score` and `weak_market_quality`.

## Cost-control strategy

Defaults are centralized in `LoopEngineConfig` and exposed through environment variables:

| Variable | Default |
| --- | ---: |
| `BGL_MAX_LLM_CALLS_PER_CYCLE` | 3 |
| `BGL_MAX_SKEPTIC_CALLS_PER_CYCLE` | 1 |
| `BGL_MAX_DAILY_LLM_CALLS` | 24 |
| `BGL_CANDIDATE_THRESHOLD_FOR_SKEPTIC` | 0.040 |
| `BGL_MIN_OPPORTUNITY_SCORE_FOR_LLM` | 55 |

The daily cap counts primary and skeptic calls together and uses `signals/llm_usage_daily.json`. A reserved call counts even if the provider fails, preventing repeated error loops from bypassing the cap. Estimated cost is emitted only when `BGL_ESTIMATED_COST_PER_CALL_USD` is configured.

## Prompt families

Forecasts route to:

- `macro/fed`
- `macro/econ`
- `politics`
- `crypto`
- `legal`
- `geopolitics`
- `sports`
- `novelty/other`

Each compact prompt contains current UTC/date, deadline and time remaining, a temporal self-check, family-specific reasoning instructions, and common failure modes. Prompts explicitly forbid invented dates and unsupported current facts. The existing post-response temporal validator remains authoritative.

## Skeptic loop

The critic is requested only for:

- edge at or above the configured candidate threshold;
- edge within the configured near-threshold ratio;
- high-confidence forecasts in legal, geopolitics, sports, or novelty markets; or
- novelty/other markets with ambiguous temporal context.

The critic checks chronology, stale facts, malformed or novelty-driven questions, and explanation-driven edge. `ALLOW` preserves the forecast. `DOWNGRADE` shrinks the forecast halfway toward the market, lowers confidence, and reruns the candidate filters. `REJECT` blocks the candidate. If a required critic cannot run because its budget is exhausted, the market is conservatively skipped.

New structured reasons:

- `skeptic_reject`
- `skeptic_downgrade`
- `budget_skipped`
- `low_opportunity_score`
- `weak_market_quality`

## Brain report foundation

The latest infer cycle writes `signals/swarm_brain_report.json`. It contains run identity, mode, watchlist/sample totals, rankings, budget use, optional estimated cost, and one normalized record per watchlist market. Records include score, grade, final decision/reason, market/model probability, edge, call flags, temporal/budget states, scoring components, and a short rationale.

This artifact is runtime state and remains ignored by Git. Morning status reads it to show score grades, LLM and skeptic calls, candidates, temporal rejects, budget skips, and the top five opportunities.

## Validation

- Focused Phase 3, pipeline, and temporal tests: pass.
- Full `unittest` discovery: 25 tests pass.
- Python compilation: pass.
- `git diff --check`: pass.
- Controlled `BGL_INFER_USE_LLM=1 swarm-edge infer`: fetched 8, ranked 7, used 3 primary calls, used 0 skeptic calls, rejected 2 low-opportunity markets, budget-skipped 2, and generated 0 candidates. It ran without `--paper`.
- `swarm-edge status`: showed `com.swarmedge.runner` running, 3 latest LLM calls, 0 skeptic calls, 0 candidates, 0 temporal rejects, 2 budget skips, and five ranked opportunities.
- LaunchAgent `com.swarmedge.runner`: existing label, wrapper, working directory, and log paths preserved.
- SQLite schema: unchanged.
- Approval gate: covered by a temporary-database test; paper candidates remain `PENDING`.
- Real-money trading and wallet integration: absent.
- `.env.runtime`: ignored and untracked.

## Remaining risks

- Score weights and grades are policy priors, not empirically calibrated edge predictors.
- The keyword router can misclassify unusual market wording.
- Daily usage JSON assumes a single runner process.
- Latest-cycle brain reporting has no historical event store or streaming transport.
- Critic quality depends on the same provider family as the primary forecast.
- Calibration sample size remains too small to claim live trading edge.
