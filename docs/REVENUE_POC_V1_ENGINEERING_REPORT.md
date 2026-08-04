# Swarm Edge Revenue POC v1 — Production Hardening Report

Validation date: 2026-08-04 UTC
PR: #29 (draft; not merged or deployed)

## Outcome

PR #29 now supports the full paper-position lifecycle while remaining disabled by default for the existing runtime (`BGL_REVENUE_POC_ENABLED=0`). The Revenue POC ledger is independent of legacy `paper_trades`; its CLI and domain package have no authenticated order path and expose no live mode.

A controlled run against a fresh byte-for-byte copy of production state created 10 automatic $25 Revenue positions. Current read-only Polymarket CLOB marks put the ledger at $996.4533 equity, with $749.75 cash, $250 deployed, $0 realized P&L, -$3.2967 unrealized P&L, and $34.9081 of entry-time modeled EV. This is lifecycle and accounting evidence, not profitability evidence: none of the admitted Revenue contracts has resolved.

## Evidence labels

- **Observed**: measured from the fresh copied production state, an attended controlled run, or current public venue/API responses.
- **Derived**: deterministic calculation from observed fields plus explicitly labeled assumptions.
- **Hypothesis**: a recommendation that still needs resolved out-of-sample evidence.

## Fresh-state reconciliation

The source database was copied from `~/Library/Application Support/SwarmEdge/state/runs.sqlite` before any schema work. At validation start, the source and pristine copy had the same SHA-256 fingerprint:

`19a3b9e77236717058a8e4a75c9e0eed23df135ef26a992527cbabe8cb68795a`

| Measure | Validation-start source | Final current source | Controlled copy after one inference |
|---|---:|---:|---:|
| Shadow snapshots | 1,821 | 1,831 | 1,826 |
| Resolved shadow snapshots | 30 | 30 | 30 |
| Unique contracts | 15 | 15 | 15 |
| Strict-lane pending candidates | 2 | 2 | 2 |
| Legacy paper CLOSED | 2 | 2 | 2 |
| Legacy paper OPEN | 4 | 4 | 4 |
| Legacy paper PENDING | 2 | 2 | 2 |
| Legacy paper VOID | 20 | 20 | 20 |

The five controlled-copy rows came from one attended inference cycle: one real Anthropic evaluation and four deterministic budget skips. It generated no strict-lane candidate and made no legacy paper-trade write. Separately, the running production process added 10 source snapshots during validation (three LLM-used, seven deterministic/budget rows). Final source fingerprint `a7886ab332a539cdda5f806b420ab2642602a4e8eb5c258cb214a189068babed` reconciles that expected append-only drift; resolved, unique-contract, strict-candidate, and legacy-paper counts did not change.

The final controlled-copy fingerprint was `d37feb002b8fa0bc48a17db3c1b3d82c5549b7f3282a7cc8aef77437b32223ff`; it differs by design because it contains the Revenue schema, evaluations, positions, marks, telemetry, and five controlled shadow rows.

## Lifecycle and schema hardening

Migration 006 adds or extends only `revenue_poc_*` state:

- append-only executable evaluations with bid/ask/depth/fee/timestamp source labels;
- independent positions with gross realized P&L, net realized P&L, realized fees, and realized slippage;
- append-only executable marks and estimated exit costs;
- append-only equity points and maximum-drawdown tracking;
- event-level Anthropic usage plus daily rollups with nullable cost;
- immutable entry fields and no-update/no-delete triggers for evaluations, decisions, marks, and equity history.

Opening a position reduces cash by stake plus entry costs. Resolution releases the stake, realizes the binary payout, and records entry plus exit fees/slippage separately. Mark and resolution keys make repeated processing idempotent.

An online SQLite backup was created automatically before the controlled first upgrade. Upgrade and downgrade both passed `PRAGMA quick_check`; downgrade removed all Revenue tables and preserved all 1,826 controlled shadow rows and all 28 legacy paper rows.

## Executable economics: actual versus assumed

Entry-history coverage across 1,826 controlled evaluations:

| Field | Actual | Assumed/fallback |
|---|---:|---:|
| Top bid | 5 current snapshots | 1,821 midpoint/spread reconstructions |
| Top ask | 5 current snapshots | 1,821 midpoint/spread reconstructions |
| Quote timestamp | 5 venue timestamps | 1,821 forecast timestamps |
| Available depth | 0 true books | 1,826 liquidity proxies |
| Fee basis | 2 explicit venue no-fee flags | 3 published category schedules; 1,821 configured fallback assumptions |

Current mark coverage across all 10 open positions:

| Field | Result |
|---|---|
| Top bid / top ask | Actual public Polymarket CLOB for 10/10 |
| Available depth | Actual USD value at the executable top level for 10/10 |
| Quote timestamp | Actual CLOB timestamp for 10/10 |
| Fee basis | 4 explicit venue no-fee flags; 6 published category-schedule assumptions |
| Slippage | Assumed at configured 10 bps for 10/10 |

Marks use the executable exit side: YES positions mark to the top bid; NO positions mark to `1 - top ask`. Midpoints are not used for current P&L. Venue reads are public and unauthenticated. The fee formula follows the venue's probability-dependent taker-fee formula when a rate applies.

## Controlled portfolio dashboard

| Metric | Observed result |
|---|---:|
| Starting balance | $1,000.0000 |
| Cash | $749.7500 |
| Deployed capital | $250.0000 |
| Current equity | $996.4533 |
| Open / resolved positions | 10 / 0 |
| Realized P&L | $0.0000 |
| Unrealized P&L | -$3.2967 |
| Entry-time modeled EV | $34.9081 |
| Open entry fees | $0.0000 |
| Open entry slippage | $0.2500 |
| Recorded spread cost | $2.7287 |
| Maximum observed drawdown | $4.2069 |
| Capital utilization | 25.0% |
| Candidate conversion | 9.434% |
| Opportunity conversion | 0.548% |

Position mix is four politics, three crypto, two geopolitics, and one macro/Fed contract. One position per contract, the five-position category cap, the 20-position limit, and the $500 deployment cap all held.

The maximum drawdown includes the sequential first-mark refresh, during which some positions had current marks while later positions were still held at entry. It is a conservative streaming-path measure, not a simultaneous exchange snapshot.

## Resolution, cash release, and idempotency fixture

A dedicated copy of the controlled ledger received one fixture mark and one fixture YES resolution. The first pass wrote one mark and one resolution; the identical second pass wrote zero of either.

The resolved $25 position recorded:

- gross realized P&L: $17.3729;
- realized fees: $0.1000;
- realized slippage: $0.2250, including its $0.025 entry assumption;
- net realized P&L: $17.0479;
- deployed capital reduction: $250 to $225;
- open category exposure reduction: four politics positions to three;
- exactly one resolution equity event.

The focused recycling test then admitted a new contract after resolution, proving released cash and exposure can be reused without averaging down or pyramiding.

## API telemetry and budget behavior

The controlled inference made one real Anthropic call:

| Field | Observed value |
|---|---|
| Model | `claude-haiku-4-5-20251001` |
| Input tokens | 470 |
| Output tokens | 112 |
| Cache-creation tokens | 0 |
| Cache-read tokens | 0 |
| Estimated cost | $0.001030 |
| Result | rejected below production minimum edge |

Cost is a token-based estimate using the public Anthropic price schedule captured with the usage event. It is not a provider invoice.

The 600 historical LLM-used shadow rows lack token/cost telemetry. They are stored as `historical_unknown`, their costs remain NULL, and the Revenue daily budget fails closed: remaining budget and cost-per-market/candidate/trade are reported as unknown rather than treating historical usage as free.

After ingesting the five new rows, an unchanged-state replay produced 0 evaluations, 1,826 cache hits, 0 admissions, and 601 additional LLM calls avoided cumulatively for that pass. Across the two validation replays the dashboard records 1,201 avoided historical/new LLM evaluations. Cache fingerprints now include market, model, executable quote, depth, fee, slippage, and holding-horizon state.

## Opportunity funnel

Latest controlled inference:

| Stage | Count | Conversion / loss |
|---|---:|---|
| Watchlist | 22 | starting universe |
| Fetch attempted | 20 | 2 blocked by existing positions |
| Fetch succeeded | 19 | 1 failed/stale slug |
| Opportunity scored | 12 | 7 rejected for weak market quality |
| Selected for bounded evaluation | 5 | 7 excluded by evaluation limit |
| Anthropic forecast | 1 | 4 deterministic daily-budget skips |
| Edge pass | 0 | 1 below minimum edge |
| Strict production candidates | 0 | no approval write |

Historical controlled Revenue conversion:

| Stage | Count | Conversion |
|---|---:|---:|
| Shadow snapshots evaluated | 1,826 | 100% |
| Fixed >=2% executable-edge observations | 222 | 12.16% |
| Safety/depth/EV-valid Revenue candidates | 106 | 5.81% |
| Unique Revenue positions | 10 | 9.43% of candidates; 0.55% of evaluations |

The current discovery architecture still constrains economic throughput before probability estimation. Early policy and quality rejections have no model probability, so their lost P&L cannot be estimated honestly.

## Lost-opportunity analysis

Expected loss is de-duplicated by contract and uses the maximum modeled EV per rejected contract, not rejection counts:

| Rejection reason | Unique contracts | Modeled EV not deployed |
|---|---:|---:|
| Executable edge below 2% | 13 | $32.9078 |
| One-position-per-contract | 6 | $10.0618 |
| Skeptic rejection | 7 | Not treated as deployable EV |
| Temporal inconsistency | 6 | Not treated as deployable EV |
| Expired market | 2 | $0.0000 |

The one-position loss is intentional opportunity cost from the no-averaging/no-pyramiding rule. The $32.91 is a model-derived counterfactual, not realized P&L. Discovery, institutional, and weak-quality losses remain unquantifiable until rejected contracts receive shadow forecasts and resolution tracking.

## Adaptive-threshold recommendation

The fixed 2% threshold qualified 222 of 1,826 observations. The current liquidity/spread/horizon counterfactual qualified 171. Among safety/depth/EV-valid candidates, fixed admitted 106 observations with $151.3296 summed modeled EV; adaptive would retain 56 with $87.9723 summed modeled EV.

**Observed:** adaptive filtering is more selective and would reduce exposure and modeled EV. **Not observed:** whether it improves realized P&L, calibration, drawdown, or return per API dollar. No admitted Revenue contract is resolved, so activating adaptive admission would be premature.

Recommendation: keep fixed 2% admission for the attended POC, record both thresholds, and compare out of sample after at least 30 resolved positions per segment. Optimize on net P&L, drawdown, and profit/API-dollar, with shrinkage toward the global result for sparse categories and horizons.

## Strategy assessment

| Strategy | Readiness | Minimal additions required; not implemented |
|---|---|---|
| Directional | Supported for paper research | longer resolved history and calibration by segment |
| Relative value | Partial | contract-family constraints, joint pricing, synchronized marks |
| Cross-market pricing | Partial | normalized event/outcome identity and synchronized venue snapshots |
| Arbitrage | Not supported | atomic multi-leg depth/fill simulation |
| Market making | Not supported | order lifecycle, queue/fill simulator, inventory/adverse-selection controls |
| Liquidity provision | Not supported | maker schedule, rebate, inventory, and adverse-selection models |
| Basket | Partial | correlation, multi-leg accounting, and portfolio risk constraints |
| Volatility | Not supported | order-book time series and path-dependent strategy definitions |

No non-directional strategy or live execution capability was added.

## Highest-ROI next engineering tasks

| Rank | Task | Expected P&L impact | Effort | Risk | API impact | Capital efficiency |
|---:|---|---|---|---|---|---|
| 1 | Accumulate automatic marks and resolved outcomes for Revenue positions | High measurement value; dollar impact unknown | Medium | Low | Low deterministic quote reads | Enables evidence-based recycling and risk controls |
| 2 | Persist true entry-time top-level depth and venue fee metadata before admission | High phantom-edge reduction; amount unknown | Medium | Low | Low | Avoids capital in unfillable/negative-net-edge trades |
| 3 | Move fingerprint/cache gating ahead of every LLM reservation in the main loop | Up to 601 calls avoided on this state replay | Medium | Medium | Strong reduction | More research per $2 budget |
| 4 | Rank all 96 historically eligible discovery contracts deterministically before bounded LLM review | Hypothesis: better candidate selection | Medium | Medium | Neutral with fixed call cap | Wider opportunity set per API dollar |
| 5 | Shadow relative-value constraints across related contracts | Hypothesis: more repeatable, less directional edge | High | Medium | Low until finalists | Potentially hedged capital use |

## Validation results

- Fresh production-state copy and SHA-256 reconciliation: passed.
- Automatic pre-migration SQLite backup: passed.
- Migration upgrade and downgrade: passed; legacy state preserved.
- Fresh copied-state inference: passed; one LLM call, no candidate.
- Automatic Revenue paper entry: passed; 10 unique positions.
- Current mark-to-market: passed; 10/10 actual public CLOB books.
- Shadow resolution scan: passed; 0 matching admitted resolutions.
- Fixture mark/resolution and repeated idempotency: passed.
- Cash and category-exposure recycling: passed.
- Unchanged-state cache: passed; 0 new evaluations on 1,826 rows.
- API-budget behavior: passed; unknown history fails closed.
- Portfolio, exposure, duplicate, and dashboard validation: passed.
- Full test suite: 94 tests passed.
- Python compile check, shell syntax check, and `git diff --check`: passed.

## Exact remaining limitations

- No admitted Revenue position is resolved, so realized profitability, win rate, profit factor, calibration, and repeatability are unknown.
- Historical bid/ask, depth, quote timestamps, fee metadata, tokens, and API dollars cannot be reconstructed; they remain labeled assumptions or unknowns.
- Six of ten current fee rates come from the published category schedule because the market response exposed only `feesEnabled=true`, not a numeric rate.
- Slippage is still a 10 bps assumption; there is no queue/fill or depth-walking simulation.
- Marks are sequential public snapshots, not an atomic portfolio-wide exchange snapshot.
- Early funnel rejections lack forecasts and outcomes, so expected lost P&L cannot be measured.
- API cost is estimated from observed tokens and current public pricing, not reconciled to invoices.
- The Revenue $2 budget currently fails closed in its accounting/query interface, but it does not replace or gate the unchanged upstream inference loop's existing call-count caps.
- Revenue orchestration runs only when explicitly enabled and attended; no scheduler or deployment change is included.
- The lane is paper-only. No authenticated order construction, signing, submission, merge, or deployment was performed.
