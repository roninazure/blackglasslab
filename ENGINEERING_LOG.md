# Swarm Edge Engineering Log

## 2026-07-24 - Phase 3.1 institutional market universe reset

**Root cause:** The prior watchlist manager retained active markets indefinitely and filled slots from a volume/recency/topic heuristic with narrow sports exclusions. It had no formal resolution-quality gate, institutional category allowlist, spread/liquidity minimums, or hard default ban for novelty, entertainment, product-release comparisons, and thin local primaries.

**Safety:** The launchd service was booted out and the wrapper was stopped before inspection. No `run_live.sh` or `live_runner.py` process remained. `memory/runs.sqlite` and the 25-market watchlist were copied to `archive/phase3_1_market_universe_reset/`; the apply operation created a second timestamped watchlist backup.

**Audit and policy:** All 25 entries were fetched and classified: 3 `INSTITUTIONAL_CORE`, 2 `ACCEPTABLE_RESEARCH`, 8 `SPECULATIVE`, and 12 `BANNED_JUNK`. The new `institutional_v1` policy requires a recognized institutional category, clean binary outcomes, known deadline, sufficient liquidity and volume, executable spread, acceptable probability/horizon, no duplicate exposure, and a minimum quality score. Novelty/other, entertainment, celebrity/album, product-release comparisons, meme markets, sports props, and thin local primaries are rejected by default.

**Rebuild:** The dry run scanned 1,992 active Polymarket contracts and selected only 3 clean markets rather than filling the target of 20 with weak entries. After the report confirmed a materially cleaner universe, apply replaced the watchlist with one major-election, one rates, and one commodities/energy market. The selected set contains 2 institutional-core and 1 acceptable-research market, with no speculative or banned entry.

**Loop integration and observability:** Institutional policy evaluation now precedes temporal analysis, opportunity scoring, and LLM reservation. Disallowed markets receive structured `banned_market_class`, `malformed_market`, `weak_resolution_quality`, or `low_institutional_quality` reasons; hard-banned records score 0/F. Brain activity includes policy allow/reason, institutional quality score, policy classification, and banned class. Morning status shows policy mode and compact universe-quality counts.

**Validation:** Focused policy/pipeline/temporal tests pass, and full discovery passes 36 tests. Python compilation, shell syntax, and `git diff --check` pass. An explicit LLM-enabled non-paper wrapper inference fetched and ranked all 3 markets, reserved 3 primary calls, safely fell back to baseline for one malformed provider response, used no skeptic call, generated no candidate, and inserted no trade. Non-paper CLI connections are now enforced read-only. The SQLite file remains byte-identical to its archive at SHA-256 `a4c50594d485b061997a1df8616c2d911c1aab6475f3b5ce960d9e8e09f05da`; counts remain `CLOSED=2`, `OPEN=4`, `PENDING=1`, `VOID=19`, and cursor 22.

**Next action:** Keep the daemon stopped. Review the three-market universe and the existing pending approval, then explicitly start `swarm-edge` for one attended paper-only cycle if the operator accepts the policy.

## 2026-07-23 - Phase 3 Loop Engine v1

**Objective:** Convert the rotating hourly infer batch into an economical opportunity loop that ranks market quality before inference, routes forecasts by market family, applies a bounded critic pass, and publishes dashboard-ready brain activity.

**Architecture:** Added a deterministic 0-100 opportunity ranker using liquidity, volume, spread, probability band, resolution horizon, category, temporal metadata, novelty risk, and existing category exposure. The infer loop now fetches the sampled batch, applies existing hard quality and concentration gates, sorts eligible markets by opportunity score, and spends LLM calls in rank order. Non-paper inference no longer persists `kv.infer_cursor`.

**Cost controls:** Central defaults cap primary calls at 3 per cycle, skeptic calls at 1 per cycle, and combined calls at 24 per UTC day. Daily usage is stored in an ignored JSON runtime artifact rather than SQLite. Low-scoring, duplicate, cooldown, weak-quality, and category-capped markets do not reach Claude. Provider cost remains `null` unless an operator configures an estimated per-call rate.

**Prompt and critic behavior:** Forecasts route through `macro/fed`, `macro/econ`, `politics`, `crypto`, `legal`, `geopolitics`, `sports`, or `novelty/other` instructions. Every family includes current UTC, the market deadline, time remaining, failure modes, and a temporal self-check. A compact critic runs only for qualifying edge, near-threshold edge, high-confidence risky categories, or ambiguous novelty timing. It can allow, shrink halfway toward the market, or reject.

**Observability:** Each infer cycle writes `signals/swarm_brain_report.json` with ranked opportunities, call usage, budget state, temporal state, final reasons, forecast values, and concise rationale fields. Morning status shows call counts, candidate/reject counts, score grades, and the top five latest opportunities.

**Safety and compatibility:** No SQLite schema changes were made. Historical outcomes were not altered. Real-money execution and wallet code remain absent. Paper insertion still defaults to `PENDING` behind `BGL_REQUIRE_APPROVAL=1`; launchd and the `swarm-edge` wrapper retain their existing entrypoints.

**Validation:** Focused Phase 3, pipeline, and temporal tests pass. Full discovery passes 25 tests. A network-enabled non-paper wrapper run fetched 8 markets, ranked 7, used the 3-call primary cap, made no skeptic call because no forecast met a critic trigger, rejected 2 low-opportunity novelty markets, budget-skipped 2 markets, and generated no candidate. Status showed the running launchd job and the latest top five. Python compilation, diff checks, and database preservation checks are recorded in `reports/phase3_loop_engine_v1.md`.

**Next action:** Accumulate brain reports and resolved paper outcomes, then calibrate opportunity-score bands and critic decisions before expanding batch cadence or model count.

## 2026-07-13 - Phase 2.6 temporal grounding safeguards

**Symptoms:** Four pending trades were rejected after Claude relied on stale release-date assumptions, including "before GTA VI" markets that lacked reliable temporal grounding.

**Investigation scope:** Traced the forecast prompt, market-context builder, live inference loop, candidate creation boundary, and the new temporal validator. Verified that the resolver and database schema remained untouched.

**Findings:** Every LLM forecast prompt now includes current UTC, current date, market end/resolution metadata when available, time remaining, and an explicit event status. The model prompt now requires chronology to match supplied time context. A post-response validator blocks stale date claims, impossible relative-time claims, and contradictory chronology before candidate creation. GTA VI-linked markets are temporarily excluded when verified temporal metadata is missing.

**Files changed:** `context/temporal.py`, `context/market_context.py`, `llm/claude_client.py`, `live_runner.py`, and focused temporal-grounding tests.

**Tests:** Targeted temporal-grounding, pipeline, and resolver unit tests pass. Full discovery, compilation, and runtime validation are still in progress for this pass.

**Next action:** Finish the controlled inference run, refresh operational documentation, and commit the temporal safeguard pass once the repo is clean.

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

## 2026-07-11 - Runtime publication and reporting hardening

**Symptoms:** The unattended wrapper still committed and pushed runtime artifacts by default, while several operator summaries could diverge from SQLite by reading stale `data/` exports or by parsing resolver notes as a single JSON blob. The live loop was also selecting `python3` implicitly rather than the local `.venv`.

**Investigation scope:** Traced `scripts/run_live.sh`, `scripts/export_data.py`, resolver output consumers, morning/integrity checks, the paper dashboard, and the legacy report scripts. Verified the production SQLite fingerprint before and after controlled validation. Re-ran the live reports from SQLite and refreshed the `data/` exports locally only.

**Findings:** Runtime publication is now opt-in behind `SWARM_EDGE_PUBLISH_ENABLED=1`, and watchlist apply is opt-in behind `SWARM_EDGE_WATCHLIST_APPLY=1`. Exports now carry ISO `generated_at_utc` metadata plus `source_db_path`. Resolver notes with appended resolution metadata are parsed correctly by dashboard and reporting consumers. The paper dashboard now counts only `status='CLOSED'` rows as closed. `integrity_check.py` and `morning_status.py` now read positions from SQLite and use `pgrep` for loop checks.

**Files changed:** `scripts/run_live.sh`, `scripts/export_data.py`, `scripts/resolve_paper_trades.py`, `scripts/morning_status.py`, `scripts/integrity_check.py`, `scripts/watch_resolutions.py`, `dashboard/app.py`, `reporting/paper_dashboard.py`, `scripts/make_flyer.py`, `scripts/make_thumbnail.py`, `.gitignore`, `.env.example`, `README.md`, `KNOWN_ISSUES.md`, `ROADMAP.md`, `docs/LEGACY_REVIEW_QUEUE.md`, `tests/test_resolve_paper_trades.py`, `tests/test_runtime_publication_and_reporting.py`, and `swarm_edge_io.py`.

**Tests:** Targeted resolver and publication/reporting unit tests pass. Full test discovery under `tests/` passes. Python compilation passes for the touched modules. `git diff --check` still reports a pre-existing trailing-whitespace issue in the tracked flyer PDF outside this pass.

**Controlled validation:** One controlled `live_runner.py --infer` cycle ran against a temp copy of `memory/runs.sqlite`; it produced no candidate and left the production database fingerprint unchanged. A resolver dry-run against production SQLite reported only the four OPEN trades and left ids 6 and 13 untouched because they are already CLOSED. SQLite-backed operational reports now agree on `OPEN=4`, `CLOSED=2`, `VOID=15`, `PENDING=0`.

**Next action:** Restart the daemon only with local-only publication defaults enabled and manual monitoring on the first runtime window. If the external fetch failure seen in the sandbox recurs in the real runtime, stop and investigate network/DNS before extending the unattended run.
