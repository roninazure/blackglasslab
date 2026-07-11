# Swarm Edge Engineering Log

## 2026-07-11 - Phase 1 stabilization and observability

**Symptoms:** A 25-market watchlist could produce only one or two diagnostics rows. Historical OPEN trades 6 and 13 could not be resolved by their stored slugs despite numeric IDs in `notes.snapshot.id`.

**Investigation scope:** Traced watchlist loading, cursor batching, cooldown, duplicate positions, adapter lookup, market-quality and strategy filters, LLM fallback, category caps, candidate creation, approval-gated persistence, diagnostics, and resolver lookup. Historical SQLite contents were baselined before modification.

**Findings:** Runtime defaults select five markets per cycle, cooldown entries were silently skipped, inference returned after the first candidate, category-cap skips lacked a structured reason, and duplicate positions were detected only during insertion. The resolver used only a slug query. Time to resolution is a baseline feature, not a hard gate.

**Files changed:** `live_runner.py`, `adapters/polymarket_adapter.py`, `scripts/resolve_paper_trades.py`, targeted tests, and Phase 1 control documents.

**Tests:** Three targeted `unittest` cases pass using temporary databases and mocked network calls. Full Python compilation passes. The watchlist manager dry run retained all 25 markets and made no changes.

**Controlled validation:** One attended inference ran without `--paper`: `watchlist=25`, `blocked_existing_position=4`, `fetch_attempted=3`, `fetch_failed=0`, `extreme_tail=1`, `llm_attempted=2`, `edge_rejected=1`, `candidates_generated=1`, `paper_not_requested=1`, and `finalized_markets=25`. Sixteen markets were outside the rotating batch and two selected markets were in cooldown. Resolver dry-run checked six OPEN trades; normal slugs found four unresolved markets, while trades 6 and 13 used `lookup_source=snapshot_id` and were recognized as resolved. Dry-run left both OPEN.

**Preservation:** `paper_trades` remained at 21 rows (`OPEN=6`, `VOID=15`, `PENDING=0`). Its ordered-row SHA-256 fingerprint remained `cc165675ad0fc13d9b0f6db7989486a4c7685e618f86e3c4eb605fb7643828a6` before and after validation. The controlled inference advanced only the existing `kv.infer_cursor` from 20 to 0. No daemon process was started.

**Next action:** Run Phase 1 observability in attended paper mode long enough to verify funnel stability before considering any strategy change.

## 2026-07-11 - Phase 1 operational acceptance

**Safety snapshot:** Validation began on `claude/review-blackglass-lab-XCVFO` with cursor 0, 21 paper trades (`OPEN=6`, `VOID=15`, `PENDING=0`), schema fingerprint `590243a3ac0dcc565a5f4fbe826ada85846c2412793f20b29dba53d4cf37de2c`, and paper-trade fingerprint `cc165675ad0fc13d9b0f6db7989486a4c7685e618f86e3c4eb605fb7643828a6`. No daemon or runner process was active.

**Rotation:** Five attended `python3 live_runner.py --infer` cycles advanced cursor `0 -> 5 -> 10 -> 15 -> 20 -> 0`. All 25 markets were sampled exactly once, every run finalized 25 structured records, and there were no reconciliation mismatches. Aggregate sampled terminal reasons were 4 existing positions, 7 cooldown skips, 6 extreme tails, and 8 minimum-edge rejections. The runs attempted 14 fetches and 8 LLM calls, generated no candidates, and inserted no paper trades.

**Resolution:** A consistent SQLite backup was created before writing. A repeat dry-run reconfirmed trade 6 as YES and trade 13 as NO through `lookup_source=snapshot_id`. The existing resolver then changed only those rows: trade 6 closed with Brier 0.3364 and P&L -$100.00; trade 13 closed with Brier 0.0004 and P&L +$47.167. IDs 2, 3, 4, and 14 remain OPEN. The post-resolution paper-trade fingerprint is `55d5b4d19a3d1f733b71cfe6670b2d5f25108b7ce852277f1ece90f4271334a6`; the schema is unchanged.

**Acceptance:** Targeted tests, full discovery, Python compilation, and the scoped diff check pass. The project integrity check reports 7 passes and 4 existing operational warnings. Phase 1 acceptance evidence is stored in `archive/phase1_validation/`. Phase 1 is complete; unattended operation remains stopped.

**Next action:** Begin Phase 2 with a read-only calibration baseline over CLOSED trades, defining sample-size requirements and reporting Brier score by model/reason/category before any threshold changes.

## 2026-07-11 - Phase 2 calibration baseline

**Scope:** Inventoried the production SQLite schema and built deterministic, read-only calibration analytics over `paper_trades`. No thresholds, models, watchlist entries, trade rows, or schema objects changed.

**Inventory:** SQLite contains 21 paper trades, 4 runs, 24 agent runs, 4 arbiter runs, and 24 population rows. There is no `forecasts`, scoring, resolution, or diagnostics table. All four legacy `runs` outcomes are `UNRESOLVED` and are excluded despite populated legacy Brier fields. Paper trades contain 2 CLOSED, 4 OPEN, 15 VOID, and 0 PENDING rows. Only CLOSED IDs 6 and 13 are binary resolved forecasts; both have entry market probabilities, model probabilities, Brier scores, resolver P&L, and resolution timestamps.

**Baseline:** Model Brier is 0.168400 versus market Brier 0.176360 on two matched forecasts, yielding Brier skill 0.045136. Model log loss is 0.443852 versus market log loss 0.539773. Theoretical gross paper P&L is -$52.8330, hit rate is 50%, average signed edge is -0.190250, and average absolute edge is 0.190250. These figures are classified `anecdotal_only`; no edge claim is permitted.

**P&L assessment:** Resolver YES, NO, and losing-trade formulas are mathematically correct for binary shares at the recorded entry probability. The formula omits fees, slippage, spread crossing, latency, and fills, so the result is gross theoretical rather than realistic net P&L. No formula or historical value was changed.

**Verification:** Targeted tests cover Brier, market Brier, skill score, payout formulas, exclusions, missing metadata, and source immutability. Production paper-trade fingerprint remained `55d5b4d19a3d1f733b71cfe6670b2d5f25108b7ce852277f1ece90f4271334a6` before and after analysis.

**Next action:** Accumulate resolved paper forecasts without strategy changes and rerun the same baseline at the 10-, 30-, and 100-forecast evidence gates.
