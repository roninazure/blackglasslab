# Swarm Edge Revenue POC v1 Engineering Report

## Outcome

Revenue POC v1 adds an independent, automatic, paper-only portfolio lane without changing the production `paper_trades` approval path. The controlled copied-state run created 6 unique $25 positions, deployed $150 of the $1,000 ledger, preserved all 26 legacy trades, and recorded $11.05 of model-derived open expected value. Expired, temporally invalid, and skeptic-rejected observations cannot deploy capital. This is operational evidence, not profitability evidence: all 768 shadow forecasts remain unresolved.

## Evidence labels

- **Observed:** measured from `memory/runs.sqlite`, the current reports, or controlled copied-state execution.
- **Derived:** deterministic calculation from observed model/market values and explicit execution assumptions.
- **Hypothesis:** a proposal that requires resolved outcomes or richer market data before adoption.

## Architecture and economic review

**Observed:** The current system is a directional forecasting funnel with one production candidate per cycle, a manual approval gate, fixed edge filters, and a 22-contract watchlist. Midpoint price was historically persisted with spread but not full order-book depth, fees, token counts, or provider cost. The production path uses a 4% edge threshold; the new Revenue POC uses 2% *executable* edge after crossing the bid/ask and applying configured costs.

**Implemented:** Revenue POC reads immutable shadow observations, reconstructs executable quotes, ranks positive-EV opportunities by expected EV per capital-day, and automatically admits positions subject to one position per contract, no averaging/pyramiding, 20 open positions, $500 deployed capital, and five positions per category. It contains no adapter, LLM, agent, connector, or order dependency.

**Challenge:** The one-candidate production cap and 22-market watchlist optimize operational selectivity, not portfolio return. The institutional filters remove large parts of the market before an edge estimate exists, so their P&L effect is unknown. The baseline also rejects spread above 3% and liquidity below $1,000, while discovery applies stricter institutional thresholds; neither cutoff has resolved return evidence.

## Complete conversion funnel

| Stage | Observed count | Conversion | Evidence |
|---|---:|---:|---|
| Discovery raw candidates | 10,419 | — | Phase 3.2 expansion report |
| Unique markets scanned | 5,000 | 48.0% of raw | Phase 3.2 expansion report |
| Eligible core/research | 96 | 1.92% of unique | 26 core + 70 research |
| Selected watchlist | 22 | 22.9% of eligible; 0.44% of unique | category/event/horizon balancing |
| Latest-run fetch attempted | 21 of 22 | 95.5% | 1 stale/failed slug |
| Opportunity scored | 13 | 61.9% of fetched | 5 low institutional + 2 weak resolution excluded |
| Shadow evaluated | 11 | 84.6% of scored | 6 budget skips, 4 edge rejects, 1 skeptic reject |
| LLM attempted | 5 | 45.5% of shadow evaluated | current cycle budget |
| Production candidates | 0 | 0% | no current opportunity passed |
| Historical shadow observations | 768 | — | 265 marked LLM-used |
| Distinct controlled executable evaluations | 764 | 99.5% | 4 unchanged-state cache hits |
| Revenue candidates | 39 | 5.1% | positive EV and >=2% executable edge after safety gates |
| Revenue positions | 6 | 15.4% of candidates; 0.79% of evaluations | one per contract and portfolio limits |

Discovery rejection counts overlap because a market can fail multiple rules. The largest counts are banned market class (4,047), unknown category (2,703), sports prop (1,312), low institutional quality (751), score below minimum (561), low volume (489), low liquidity (467), wide spread (443), probability out of band (274), and horizon outside range (258). Counts alone do not demonstrate lost P&L because rejected markets were not forecast or resolved.

## Lost-opportunity analysis

**Derived:** On the controlled ledger, rejection opportunity is de-duplicated by contract using the maximum modeled EV observed for that contract:

1. Below 2% executable edge: 12 contracts, $28.56 modeled EV not deployed.
2. One-position-per-contract: 4 contracts, $5.70 modeled EV from later observations not added. This is intentionally forgone because averaging and pyramiding are prohibited.

Raw observation totals are not investable totals because they count the same contracts repeatedly. Early discovery, policy, temporal, and quality stages have no model probabilities, so assigning them P&L would be fabricated. The 38 historical skeptic rejections carry a $64.53 raw-edge-dollar proxy at $25 per observation, but none are resolved and the proxy ignores execution costs; they are safety rejections with zero deployable lost EV.

## Adaptive threshold investigation

**Observed:** No shadow threshold has a resolved outcome, so fixed-versus-adaptive profitability cannot be compared. Politics has the highest historical raw edge (2.04% average; 19.9% >=2%), while crypto has the highest >=2% frequency (32.2%; 1.59% average). The 31–90 day horizon has the highest average raw edge (1.80%), followed by 91–180 days (1.24%). These are edge distributions, not returns.

**Implemented counterfactual:** Each evaluation records a recommended threshold starting at 2%, then adds half-spread (capped at 2 percentage points), 0.5–1 point for thin liquidity, and 0.5 point for missing or >180-day horizons. Admission remains fixed at 2% executable edge until resolved samples exist.

**Hypothesis:** Adaptive thresholds should be activated only after each segment has at least 30 resolved positions and improves net P&L, drawdown, and profit/API-dollar out of sample. Category and calibration adjustments should use shrinkage toward the global rate to avoid overfitting.

## Strategy readiness

| Strategy | Readiness | Minimal additions required |
|---|---|---|
| Directional | Supported for paper research | current quote/depth refresh, fee capture, revenue resolution |
| Relative value | Partial | contract-family graph, mutually exclusive/exhaustive constraints, joint pricing |
| Cross-market pricing | Partial | normalized event/outcome identities and synchronized venue snapshots |
| Arbitrage | Not supported | atomic multi-leg quote/depth validation and fill simulation |
| Market making | Not supported | order lifecycle, inventory model, queue/fill simulator; incompatible with current no-order POC |
| Liquidity provision | Not supported | maker-fee/rebate model, adverse-selection and inventory controls |
| Basket | Partial | correlation/exposure model and multi-leg portfolio accounting |
| Volatility | Not supported | time-series order books and path-dependent instruments/signals |

No non-directional strategy was implemented.

## API economics

**Observed:** 265 of 768 historical shadow observations are marked LLM-used across four days (23, 91, 86, and 65 calls/day). Historical token counts and actual provider spend were not captured, and configured estimated cost per call is zero, so a dollar-cost claim would be unsupported.

**Implemented:** deterministic state fingerprints, cache hits/calls avoided, deterministic EV-per-capital-day ranking, configurable $2/day reporting, and cost-per-market/candidate/admission metrics. Replaying unchanged copied state caused zero new evaluations or admissions and recorded 265 LLM calls avoided. The first import cannot retroactively avoid calls already spent.

## Controlled dashboard

| Metric | Result |
|---|---:|
| Starting balance | $1,000.00 |
| Cash | $849.85 |
| Deployed capital | $150.00 |
| Equity at entry marks | $999.85 |
| Open positions | 6 |
| Realized / unrealized P&L | $0.00 / $0.00 |
| Model-derived open EV | $11.05 |
| Fees / slippage / spread cost | $0.00 / $0.15 / $1.38 |
| Capital utilization | 15.0% |
| Candidate / opportunity conversion | 15.4% / 0.79% |
| API calls observed historically | 265 |
| Second-pass calls avoided | 265 |
| API spend / tokens | unavailable historically |

Win rate, profit factor, average winner/loser, maximum realized drawdown, calibration by segment, and repeatability are unavailable until positions resolve. Equity is marked at entry because current quotes are not persisted for Revenue POC positions.

## Highest-ROI next engineering tasks

| Rank | Task | Expected P&L impact | Effort | Risk | API impact | Capital efficiency |
|---:|---|---|---|---|---|---|
| 1 | Resolve and mark Revenue POC positions; persist realized fees and outcomes | High measurement value; direct P&L unknown | Medium | Low | Low | Enables evidence-based recycling and drawdown controls |
| 2 | Persist top-of-book depth, executable fees, and current marks | High downside protection by removing phantom edge; amount unknown | Medium | Low | Low/moderate deterministic quote calls | Avoids capital in unfillable/negative-net-EV trades |
| 3 | Move state-cache check before LLM invocation and persist provider usage | Up to 265 calls avoided on unchanged replay; P&L neutral unless saved budget evaluates better markets | Medium | Medium | Strong reduction | More edge evaluations per $2 budget |
| 4 | Expand deterministic ranking from 22 to the 96 eligible contracts before LLM selection | Hypothesis: larger executable opportunity set; effect unknown | Medium | Medium | Neutral if LLM cap fixed | Better selection without more capital |
| 5 | Add relative-value constraint detection across event families | Hypothesis: less model-risk-dependent edge and more repeatability | High | Medium | Low until finalists need review | Supports hedged/multi-leg use of capital |

## Validation and limitations

- Production database backed up before schema work: `backups/runs.pre_revenue_poc_v1.20260804T000000Z.sqlite` (operational artifact, not committed).
- Schema upgrade/downgrade is scoped to `revenue_poc_*` tables and preserves `paper_trades`.
- Controlled experiments ran on `/tmp` database copies only.
- The Revenue CLI automatically takes an online SQLite backup before first-time schema creation.
- Controlled read-only inference fetched one live snapshot with LLM disabled; it was rejected for weak market quality and created no production or Revenue position.
- Production approval, resolver, position sizing, and safeguards are unchanged.
- Revenue positions cannot submit live orders: the package has no connector/order imports and the CLI exposes no live mode.
- Legacy calibration contains only two resolved P&L samples, total -$52.83; it is too small for threshold or category tuning.
- Historical fee, token, API-dollar, true depth, and mark-to-market data are unavailable. Configuration makes assumptions explicit rather than presenting them as observations.
