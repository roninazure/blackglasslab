# PARALLAX commercial v1

PARALLAX is a retail intelligence application for Polymarket US and Kalshi. Trading remains outside this product path. The public reader supplies current market candidates; reviewed model evidence is required before publishing a live BUY. No production fair-value model has been connected, so current live candidates correctly remain WATCH/PASS and have no invented PARALLAX value.

## Run the demo

From the repository root, using its existing environment:

```sh
PARALLAX_MODE=demo .venv/bin/python -m streamlit run dashboard/pages/1_PARALLAX.py
```

The same page is available under PARALLAX when running the existing `dashboard/app.py`. `PARALLAX_MODE=demo` must be explicit. Every synthetic card carries a demo label. Demo models, fees and results are never presented as real evidence. The track record stays empty.

For public live data, omit `PARALLAX_MODE`. The live UI defaults to Explorer. A public-data refresh runs every 30 seconds after the preceding refresh completes; rendering reevaluates quote age. Book and receipt timestamps older than 60 seconds (or in the future) cannot qualify for BUY. A refresh button also requests public reads. The scan samples six markets per venue by default, not the entire catalog.

```sh
.venv/bin/python -m parallax scan --limit 3 --summary
.venv/bin/python -m parallax serve --port 8765
.venv/bin/python -m parallax serve --demo --port 8765
```

The HTTP service binds only to `127.0.0.1`. Demo mode previews PRO; live mode defaults to Explorer. This is a local demo server, not a hardened internet deployment. No POST/order/account route exists. No credentials or `.env` files are loaded. Stop with Ctrl-C.

Publication storage defaults to the separately ignored `data/parallax-commercial/publications.sqlite`. Override using CLI `--db`, or UI `PARALLAX_PRODUCT_DB`. Do not point it at a trading DB. Runtime files must never be staged.

## Implementation map

| File | Responsibility |
| --- | --- |
| `parallax/models.py` | `NormalizedMarket`, `Mechanics`, `Evidence`, `ParallaxPlay`, `RetailExample`, `Verdict`, enums |
| `parallax/normalization.py` | Venue-specific normalization and evidence-to-rules binding |
| `parallax/sources.py` | Reused PMUS public client; Kalshi GET-only discovery, book and activity reader |
| `parallax/economics.py` | Decimal stake, quantity, fee, payout and slippage calculations |
| `parallax/engine.py` | Deterministic qualification, verdicts and four product play types |
| `parallax/service.py` | Shared feed/details, filtering, publication boundary and product metrics |
| `parallax/track_record.py` | Append-only publication/settlement SQLite store and transparent statistics |
| `parallax/entitlements.py` | Plan/Feature/Entitlement boundaries, commercial labels, alert hook |
| `parallax/api.py` | Six required GET interfaces and trusted principal resolver seam |
| `parallax/demo.py`, `parallax/__main__.py` | Clearly synthetic fixtures and read-only CLI |
| `dashboard/pages/1_PARALLAX.py` | Existing Streamlit application's retail feed and expandable details |
| `tests/test_parallax_commercial.py` | Domain, arithmetic, trust, API, source-shape and UI tests |

Only `.gitignore` and the setuptools package list in `pyproject.toml` were extended outside these new files. No dependency or lock file changed. Existing maker, FLASH, paper experiments and execution files were not edited.

## API and access

| Route | Behavior |
| --- | --- |
| `GET /plays` | Accessible feed; filters `venue`, `confidence`, `action`, `play_type`, `resolution_horizon` (hours), `minimum_edge` (percentage points) |
| `GET /plays/{id}` | Full play detail, PRO/API only |
| `GET /markets` | Normalized markets; Explorer receives basic price/outcome metadata |
| `GET /venues` | Visible coverage and execution-disabled status for both venues |
| `GET /track-record` | Explicit publications, settlements and labeled hypothetical statistics |
| `GET /health` | Coverage, generation/rejection counters, collection failures and freshness status |

Explorer: free, up to five cards, both venues, limited detail, venue filter. PRO: $49/month label, full detail, advanced filters and `alert_candidates(plan)` hook. ENTERPRISE / API: Contact Sales label, higher feed limit and API feature placeholder. Internal `edge` and `institutional` identifiers are reserved and grant no access.

Billing seam: supply `resolve_plan(headers)` to `api.server()` after a trusted backend verifies a session/API principal and its billing state. Return the corresponding `Plan`. Do not trust a requested plan query/header: these cannot elevate access. Apply the same trusted session result in Streamlit in place of its live Explorer default. No authentication provider, checkout, payment processing, alert delivery, rate limiting, or enterprise administration is implemented. Demo access selection is available only in explicit demo mode.

## Evidence and qualification contract

The live reader never converts midpoint, another venue's quote, or a research trial into fair value. Connect reviewed evidence through `PlayService.replace_inputs(markets, evidence_by_venue_market)`. Each `Evidence` is a trusted internal model output with YES fair probability, source, model version, rationale, observation/expiry times, review reference and exact `rules_digest(market)`. The digest binds venue, market ID, outcome labels, rules, payout and resolution date. Contradictions and invalidation are explicit. Do not expose untrusted customer submissions to this seam.

The six deterministic evidence checks are: correctly bound sourced output; unexpired evidence; reviewed exact rules; validation reference; two distinct corroborating sources; and no contradiction/invalidation. Score is the percentage of checks passed, explicitly named **evidence qualification**, never win probability. ELITE clears all six; HIGH clears all except optional corroboration; MEDIUM has current sourced evidence but cannot BUY. PASS never buys. A validation reference must identify actual model validation; a string is a provenance pointer, not proof of calibration.

BUY also requires: open market; finite uncrossed executable bid/ask; fresh book and receipt; future resolution date; reviewed rules; at least a five-point estimated edge at the ask; verified $25 scenario capacity; spread at most five points; current verified fees; at least 5% modeled return after estimated costs; no contradictions/invalidation. Missing rules/invalid prices/closed markets/contradictions yield PASS; remediable price-age, liquidity, fee or confidence gaps yield WATCH. Negative edge yields PASS. These are conservative product policy thresholds, not empirically calibrated promises.

`Mechanics` carries payout, minimum/quantity step, per-market tick ranges, and an optional reviewed symmetric fee coefficient, rounding policy, source and expiry. Live fee schedules currently remain unknown; no zero-fee assumption or maker rebate is substituted. Expired schedules are removed from retail estimates and block BUY. Live readers use conservative whole-contract scenarios; no fractional-order support is claimed. An operator must verify applicable fees/mechanics before attaching live actionable evidence.

Venue schema references checked during implementation: [Kalshi bid-book semantics](https://docs.kalshi.com/api-reference/market/get-market-orderbook), [Kalshi tick ranges and fractional representation](https://docs.kalshi.com/getting_started/fixed_point_migration), [Kalshi discovery filters](https://docs.kalshi.com/api-reference/market/get-markets), and [PMUS market metadata](https://docs.polymarket.us/api-reference/markets/get-market-by-id). The [PMUS published fee schedule](https://docs.polymarket.us/fees) was inspected, but its coefficient is not automatically asserted as verified live market-specific fee evidence.

## Retail and track-record semantics

The $10/$25/$50/$100 selections are contract budgets, not personalized sizing. Quantity is rounded down; actual spend and unspent balance are explicit. Payout equals contracts times the verified $1 binary payout. Gross profit is payout minus actual spend; maximum loss before fees equals spend. Fees and optional slippage are separate and included in total-cost/net-profit/total-loss fields. An example exceeding top-ask capacity is unavailable. This v1 does not sweep deeper prices or predict future slippage. Stale prices are visibly labeled historical quotes.

PMUS uses its native long/short mapping and includes the selection title with the market question. It preserves the original market and normalized public book. Top-book sizes, available 24-hour volume, last trade time, rules, status and resolution fields are retained; public trade counts are unknown when unavailable. It is **Polymarket US**, not global Polymarket CLOB coverage.

Kalshi retains original market, bid book and returned trades. NO bids imply YES asks at `1 - bid` and vice versa; fixed-point dollar formats and legacy integer cents are parsed separately. Discovery excludes multivariate combo markets. Scalar/non-$1 payouts cannot qualify. Volume is identified as a 24-hour field; trade count refers only to the returned sample of up to 100. REST receipt is explicitly labeled when no source book timestamp exists. Missing depth stays missing. Exact source market references remain available even where no canonical website URL is supplied. Category may be unknown in market responses. Similar titles across venues are not treated as equivalent settlement contracts.

Every live BUY returned by the shared service first passes `TrackRecord.publish()`, which independently requalifies the inputs and atomically inserts a full immutable snapshot. Persistence failures suppress BUY output. Stable IDs prevent repeated refreshes from creating duplicate publications; v1 publishes at most once per venue/market/side. SQLite triggers reject UPDATE/DELETE. Runtime refreshing never rewrites snapshots. Demo publications are rejected.

Settlement is an internal trusted resolver seam: `TrackRecord.settle()` requires the publication ID, exact venue/market, explicit RESOLVED or VOID, winning side, settlement timestamp and authoritative source reference. It appends once; duplicates and mismatches fail. No automatic resolver is connected. There is no public settlement-write endpoint.

Win rate excludes pending and void plays. Price-only ROI is explicitly before costs. Net hypothetical P/L uses each frozen $100 contract-budget example with actual quantity rounding and estimated fees; it is withheld if any settled nonvoid play lacks that executable scenario. Void costs are unknown and excluded. Results are hypothetical, not claimed executed returns. Calibration metrics remain null until a calibrated model and defensible sample exist. No research trial/history is imported.

## Baseline and validation

Starting branch: `feature/parallax-sports-census`. Starting HEAD: `62ad1cd8ec5e0ea410b32565730f7a842e7f9b8c`. Implementation branch: `feature/parallax-commercial-v1`, created without discarding working changes.

Existing modified files preserved: `data/infer_diagnostics.json`, `data/paper_trades.json`, `maker_spread_economics/live.py`, `maker_spread_economics/live_engine.py`, `maker_spread_economics/polymarket_us.py`, `scripts/parallax_live_maker.py`, `tests/test_maker_spread_economics.py`, `tests/test_parallax_live_maker.py`, `tests/test_polymarket_us.py`.

Existing untracked work preserved: `maker_spread_economics/parallax_paper.py`, `tests/test_parallax_paper.py`, `data/parallax_live_maker.lock`, and SQLite files `parallax_maker_dry_run`, `parallax_paper_6h`, `parallax_paper_diagnostic_15m`, `parallax_paper_smoke`, `parallax_pmus_live`, `parallax_us_maker_dry_run` under `data/` (including ignored SQLite sidecars). None is part of the commercial commit.

Commands:

```sh
.venv/bin/python -m pytest -q tests/test_parallax_commercial.py tests/test_polymarket_us.py tests/test_maker_spread_economics.py tests/test_parallax_live_maker.py tests/test_parallax_paper.py
.venv/bin/python -m pytest -q tests
uvx ruff check parallax tests/test_parallax_commercial.py dashboard/pages/1_PARALLAX.py
uvx mypy --follow-imports=silent --ignore-missing-imports parallax
.venv/bin/python -m parallax scan --limit 3 --summary --db /private/tmp/parallax-commercial-smoke.sqlite
```

Initial full-suite result: **480 passed, 5 failed**. All five failures reproduce in isolation in unchanged sources/tests: FLASH `OperationalReadinessTests.test_mandatory_invariant_loss_disarms`; operator `OperatorDataTests.test_positive_negative_and_resolved_positions`, `test_stale_data_retains_values_and_marks_quote_stale`; `OperatorAppTests.test_search_filters_positions_without_writes`, `test_small_terminal_layout_keyboard_navigation_and_detail`. They are outside this milestone. Later added milestone regressions are included in the final targeted validation, without repeating this unrelated suite.

Public read-only smoke on 2026-09-06: three markets normalized from each venue; 12 YES/NO candidates; 0 BUY; 0 generation failures; executable prices obtained from both venues; some stale/one-sided quotes correctly rejected. No orders placed or live capital used.

Streamlit application tests render the synthetic feed, confidence, scenarios and empty track record. Browser visual QA was attempted through the Browser skill; the runtime reported no available browser and an empty browser list. Screenshot/layout verification remains a limitation.

Final post-Forge validation: **142 targeted/relevant tests passed**, including **58 commercial product tests**. Ruff check and formatting passed; mypy passed for all 12 new package modules. Final live smoke at 2026-09-06 17:00 UTC: health OK, 3 PMUS + 3 Kalshi markets, 12 candidates, 12 PASS, 0 BUY, no collection/generation failures, all observed timestamps fresh. Zero live orders throughout.

## Milestone Forge review

No local SWARM FORGE v2.0 procedure was available in the scoped files or skill catalog. The supplied baseline → implement → test → adversarial milestone → revalidate checklist governs this work.

High-value findings implemented:

- Complement float residue falsely rejected PMUS NO ticks: normalize quote precision; regression added.
- Separate PMUS selection titles were missing from generic questions: retain them in card titles.
- Kalshi default discovery surfaced combos: use documented `mve_filter=exclude`; no topical lanes added.
- Expired fee coefficients could leak into net examples: remove expired estimates and reject qualification.
- Cached BUYs could outlive quote evidence: requalify on every service access; expiry regression added.
- Publication write failure could create an untracked recommendation: fail closed; regression added.
- Different outcome labels with identical rule text could reuse evidence: bind the full outcome/rule/date/payout contract.
- SQLite context management alone did not close connections: explicitly close per operation.
- Stale card prices needed visible context: label past quotes above the calculator.

Additional attacks cover fee/slippage arithmetic, quantity/tick/depth limits, invalid prices/stakes, confidence gating, contradictions, duplicate publications, immutability, unmatched/duplicate settlements, live/demo isolation, plan spoofing and empty/unsupported performance metrics. No execution strategy or live order path is introduced. No speculative architecture or enterprise system added.

Deferred: full Kalshi execution; five-tier billing; new trading strategies; connected validated fair-value evidence; market-specific verified live fees; automatic settlement resolver; alert delivery; production auth/billing/hosting. Next product action: review the clearly labeled demo with a retail user before connecting an audited evidence source.
