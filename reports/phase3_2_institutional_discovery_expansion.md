# Phase 3.2 Institutional Discovery Expansion

## Executive Finding

Phase 3.1's hard bans were correct, but its discovery and classification coverage were too narrow. Phase 3.2 expands serious-market recognition and candidate acquisition without weakening those bans. The applied default watchlist grew from 3 to 22 markets, all CORE or RESEARCH tier, with no WATCH or BANNED filler.

The runtime was stopped before changes and remains stopped.

## Root Cause

- Question-only classification put most markets into novelty/other even when event metadata identified a serious topic.
- One volume-sorted market query provided narrow coverage and discarded event-level category and title context.
- One quality threshold set conflated serious research-grade markets with weak contracts.
- The malformed-question rule treated missing terminal punctuation as a structural defect.

The final scan evaluated 5,000 unique markets from 10,419 raw candidates. It classified 26 CORE, 70 RESEARCH, 751 WATCH, and 4,153 BANNED. Rejection counts overlap because a market can fail more than one quality check:

| Rejection cause | Count |
| --- | ---: |
| Category ban | 1,344 |
| Unknown category | 2,703 |
| Missing or unclear deadline | 4 |
| Liquidity below threshold | 467 |
| Volume below threshold | 489 |
| Spread too wide | 443 |
| Probability out of band | 274 |
| Weak resolution quality | 106 |
| Malformed question | 0 |
| Duplicate exposure | 1 |
| Horizon outside range | 258 |
| Quality score below threshold | 561 |
| API metadata missing | 76 |

Full evidence is in `reports/phase3_2_rejection_analysis.json` and its Markdown rendering.

## Policy Changes

`institutional_v2` recognizes Fed and rates, inflation/CPI, employment, GDP and recession, central banks, major elections, geopolitics, BTC/ETH, commodities and oil/gas, major indices, legal/regulatory, and high-liquidity corporate/regulatory events. It combines the question with event and market metadata before classification.

Hard bans remain in force for GTA VI and product comparisons, entertainment, albums, celebrity markets, memes, novelty without serious evidence, malformed questions, thin local primaries, and sports props unless explicitly enabled.

The policy tiers are:

- CORE: liquidity at least $25,000, volume at least $250,000, spread at most 0.02, probability from 0.05 to 0.95, and quality at least 75.
- RESEARCH: serious category and clean resolution with liquidity at least $5,000, volume at least $50,000, spread at most 0.04, probability from 0.025 to 0.975, and quality at least 55.
- WATCH: serious contracts that do not clear the trading-research requirements. Reported, but excluded from the default watchlist.
- BANNED: hard-banned, unknown-category, malformed, or weak-resolution markets. Never selected by default.

All selected markets require a known deadline, a clean binary outcome, a 2-365 day horizon, and no open or pending duplicate exposure. Defaults cap each category at four markets and each event at two.

## Discovery Breadth

Discovery now starts from active Gamma events and expands their contained markets. It queries `volume24hr`, cumulative `volume`, `liquidity`, and ascending `endDate`; balances the candidate budget across queries; paginates; deduplicates; and preserves event context. Defaults are eight pages, 5,000 unique candidates, and a target of 24. Optional category hints and all limits are environment-configurable.

The final apply stopped at 22 because category and event caps prevented a clean 24-market selection. It did not add WATCH or BANNED contracts.

## Applied Watchlist

- Size: 22, up from 3.
- Tiers: 15 CORE and 7 RESEARCH.
- Categories: macro/Fed 4, major elections 4, geopolitics 4, crypto majors 4, corporate/regulatory events 4, oil/gas 2.
- Horizons: 2-14 days 7, 15-45 days 2, 46-120 days 4, 121-365 days 9.
- Average institutional quality: 80.64.
- Distinct events: 14; maximum markets per event: 2.
- Banned classes selected: 0.

The complete market list, scores, categories, metrics, comparison, and timestamped backup path are in `reports/phase3_2_universe_expansion.json` and its Markdown rendering.

## Loop And Reporting

`signals/swarm_brain_report.json` and `signals/infer_pipeline_report.json` now include policy tier, institutional category, institutional quality, watchlist and sampled tier counts, and rejection distribution. Status reports the v2 policy, watchlist quality, selected tier counts, and scan tier counts.

The approval gate remains enabled by default. Non-paper inference opens SQLite read-only, and no wallet, order submission, or real-money execution path exists.

## Validation

- Focused policy and loop tests: 36 tests passed after the final policy correction.
- Full unit discovery: 47 tests passed.
- Wrapper test isolation: replaced Linux-only `/bin/true` with macOS `/usr/bin/true`; repeat full discovery leaves production SQLite byte-identical.
- Python compilation: passed.
- `scripts/run_live.sh` and `bin/swarm-edge` syntax: passed.
- `git diff --check`: passed.
- Dry run: 22 clean markets; watchlist unchanged.
- Apply: 22 clean markets; original watchlist backed up.
- Controlled inference without `--paper`: 8 fetched and ranked, 5 LLM calls, 1 skeptic call, 3 budget skips, 5 rejects, 0 candidates.
- Status: `institutional_v2`, watchlist 22, average quality 80.6, runtime not running.
- SQLite: byte-identical before and after at SHA-256 `676823731e30a18dcb584e28cec58732be5b062035a7083689afcc24d5338fdc`.
- Paper trades: `CLOSED=2`, `OPEN=4`, `VOID=20`, unchanged.
- `.env.runtime`: ignored and untracked.
- launchd plist: present and valid; job intentionally unloaded.

## Remaining Risks

- Category and malformed-text detection are deterministic heuristics, not a labeled classifier.
- The selected universe still lacks currently eligible CPI, jobs, GDP, central-bank, index, and legal markets.
- Event caps reduce concentration but do not model correlation or mutually exclusive contracts.
- Discovery reports retain evaluated evidence, but there is no durable historical snapshot series for replay.
- Opportunity and quality weights remain uncalibrated against a statistically meaningful set of resolved paper forecasts.

## Recommended Next Step

Keep unattended operation stopped. The operator should manually review the 22 contracts and their resolution rules, then explicitly run one attended approval-gated paper cycle. Use that cycle to verify category routing, correlated-event exposure, and candidate behavior before considering a daemon restart.
