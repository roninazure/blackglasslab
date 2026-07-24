# Phase 3.1 Market Universe Reset

## Executive Finding

The former watchlist was not suitable for institutional paper research. Its sticky retention and lightweight topic multiplier allowed novelty, entertainment, product-release comparison, thin local primary, malformed, and weak-resolution markets to remain in the default universe. The reset replaces that behavior with a fail-closed, configurable policy and an audited dry-run/apply workflow.

## Safety Baseline

- Branch: `claude/review-blackglass-lab-XCVFO`
- Starting commit: `c79a7a4`
- Runtime: stopped through launchd bootout and `swarm-edge stop`
- Remaining runner processes after stop: none
- Original watchlist: 25 markets
- SQLite paper trades: 26 total (`CLOSED=2`, `OPEN=4`, `PENDING=1`, `VOID=19`)
- SQLite SHA-256: `a4c50594d485b061997a1df8616c2d911c1aab6475f3b5ce960d9e8e09f05da`
- Backups: `archive/phase3_1_market_universe_reset/`

No SQLite schema or historical trade row was changed.

## Old Watchlist Quality

The complete audit is in `reports/current_watchlist_audit.md` and `.json`.

| Classification | Count |
| --- | ---: |
| INSTITUTIONAL_CORE | 3 |
| ACCEPTABLE_RESEARCH | 2 |
| SPECULATIVE | 8 |
| BANNED_JUNK | 12 |
| UNKNOWN_REQUIRES_REVIEW | 0 |

Only 5 of 25 entries passed the new policy. Twelve were hard junk and eight failed institutional quality gates.

## Institutional Policy

Default allowed categories are macro/Fed, macro/economic data, inflation/CPI, rates, recession, major elections, geopolitics, crypto majors, commodities/energy, and legal/regulatory.

Every allowed market must have:

- Clean binary YES/NO outcomes
- Known deadline by default
- Minimum liquidity of $5,000
- Minimum volume of $100,000
- Maximum executable spread of 3 percentage points
- Market probability between 5% and 95%
- Resolution horizon from 2 through 365 days
- Minimum institutional quality score of 60
- No OPEN/PENDING duplicate exposure
- Category and horizon capacity

The 121-365 day horizon is restricted to high-quality macro, election, geopolitical, and legal categories. All values are centralized in `InstitutionalUniverseConfig` and can be overridden with `BGL_UNIVERSE_*` variables.

## Default Bans

Hard-banned classes include:

- GTA VI and other product-release comparison markets
- Album, celebrity, and entertainment markets
- Meme and novelty markets
- GPT/product-release gossip
- Thin district primaries
- Sports props unless explicitly enabled
- Malformed questions
- Unclassified low-signal other markets
- Unclean binary or missing executable resolution metadata

The audit removed five product-release comparisons, four thin local primaries, one low-signal other market, one novelty/meme market, and one weak-resolution market. All 25 old watchlist IDs were removed from the rebuilt universe; historical positions and outcomes were preserved.

## Dry Run And Apply

The dry run scanned 1,992 active Polymarket markets:

- Eligible before balance: 3
- Selected: 3
- Target: 20
- Materially cleaner: yes
- Junk backfill: none

The selected universe:

| Category | Market | Quality |
| --- | --- | ---: |
| Major elections | Renan Santos to win the 2026 Brazilian presidential election | 88.0 |
| Rates | No Fed rate change after the September 2026 meeting | 80.9 |
| Commodities/energy | WTI to reach $100 in July | 61.9 |

This produced 2 `INSTITUTIONAL_CORE`, 1 `ACCEPTABLE_RESEARCH`, 0 `SPECULATIVE`, and 0 `BANNED_JUNK` entries. Apply ran only after the audit and rebuild reports existed and the material-cleanliness gate passed. The timestamped pre-apply backup matches the original watchlist hash.

## Loop Engine Integration

Institutional policy now runs immediately after a market fetch and before temporal reasoning, opportunity scoring, or any LLM call. Rejected markets cannot consume an LLM budget.

New terminal reason codes:

- `banned_market_class`
- `malformed_market`
- `weak_resolution_quality`
- `low_institutional_quality`
- `insufficient_clean_markets`

Hard-banned markets receive `REJECT`, score 0, and grade F. `signals/swarm_brain_report.json` exposes `policy_allowed`, `policy_reason`, `policy_classification`, `institutional_quality_score`, and `banned_class` per market, plus the active policy mode. Morning status reports compact universe counts and average quality.

## Validation

- Focused institutional policy, pipeline, and temporal tests: pass
- Full `unittest` discovery: 36 tests pass
- Python compilation: pass
- `bash -n scripts/run_live.sh`: pass
- `git diff --check`: pass
- Dry-run rebuild: pass, watchlist unchanged
- Applied rebuild: pass after material-cleanliness gate
- Explicit LLM-enabled non-paper inference: 3 fetched, 3 ranked, 3 calls reserved, 2 responses used, 1 malformed response safely fell back to baseline, 0 skeptic calls, 0 candidates, 0 database writes
- Non-paper SQLite connection: read-only at the CLI boundary; regression tested
- Status: policy `institutional_v1`, watchlist 3, average policy quality 76.9, daemon stopped
- Launchd plist: valid and still installed
- Wrapper: executable and still linked at `~/bin/swarm-edge`
- Approval gate: defaults to `BGL_REQUIRE_APPROVAL=1`; tests confirm candidates remain `PENDING`
- Real-money/wallet execution: absent
- `.env.runtime`: ignored and untracked

After validation, SQLite remains byte-identical to the safety backup with the same trade counts and cursor 22.

## Remaining Risks

- Three markets provide limited breadth. The next iteration should improve recall using labeled discovery audits, not lower the hard quality floor.
- Keyword classification and malformed-grammar detection can misclassify unusual but legitimate wording.
- The Gamma API is a live dependency; rebuild reproducibility is limited to the emitted report.
- Existing historical OPEN/PENDING trades may now belong to banned classes. They remain preserved and require ordinary operator handling.
- Policy scores and thresholds are engineering priors, not statistically calibrated alpha.
- The brain artifact retains only the latest cycle.

## Recommended Next Step

Keep unattended operation stopped. Review the three selected contracts and the existing pending paper approval. If accepted, run one explicitly attended `swarm-edge start` paper-only cycle, verify the first `signals/swarm_brain_report.json`, then stop and review before restoring unattended cadence.
