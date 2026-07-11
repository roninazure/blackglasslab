# Phase 2 Calibration Baseline Method

## Eligible Evidence

The baseline includes only `paper_trades` rows with `status='CLOSED'`, a binary `resolved_outcome` of `YES` or `NO`, and a valid model probability in `p_yes` (falling back to `consensus_p_yes` only when needed). `VOID`, `OPEN`, `PENDING`, non-binary, and unresolved records are excluded. Legacy `runs` rows marked `UNRESOLVED` are not calibration evidence even when legacy Brier columns are populated.

Market comparison requires a valid entry `p_yes_market` in `notes`. Model comparison requires an explicit model identifier; otherwise the record is grouped as `unknown`. Category analysis reports missing categories as `unknown`. P&L uses resolver metadata when present and otherwise applies the documented resolver formula.

## Metrics

For outcome `y` (YES=1, NO=0) and probability `p`:

- **Brier score:** `(p - y)^2`; lower is better. Model Brier uses `p_yes`. Market Brier uses entry `notes.p_yes_market` on the matched subset.
- **Brier skill score versus market:** `1 - model_brier_matched / market_brier`. Positive values favor the model; zero is parity; negative values favor the market. It is unavailable when the market Brier is zero or entry prices are missing.
- **Absolute calibration error:** mean `abs(p - y)`. With small samples this is descriptive, not a calibration curve.
- **Log loss:** mean `-[y*ln(p) + (1-y)*ln(1-p)]`, with probabilities clipped to `[1e-15, 1-1e-15]` only for numerical safety.
- **Realized P&L:** resolver-recorded or formula-derived theoretical gross paper profit in USD.
- **Hit rate:** fraction of trades whose selected side matches the resolved outcome.
- **Signed edge:** `model_p_yes - entry_market_p_yes`; absolute edge is its magnitude.

## P&L Interpretation

For stake `S` and entry YES probability `p`:

- Winning YES: `S * (1/p - 1)`.
- Winning NO: `S * (p/(1-p))`, equivalent to buying NO at price `1-p`.
- Losing trade: `-S`.
- Missing entry probability: the resolver's current fallback is even-money `+S` for a winner and `-S` for a loser.

Probabilities are bounded to `[0.001, 0.999]` in the resolver. No exchange fees, spread crossing, slippage, partial fills, latency, or capital constraints are deducted. Reported P&L is therefore **theoretical gross paper P&L**, not realistic net executable P&L.

## Evidence Gates

- Fewer than 10 resolved forecasts: **anecdotal only**.
- 10-29: **preliminary**.
- 30-99: **directional**.
- 100-299: **moderate evidence**.
- 300 or more: **stronger evidence**.

No claim of forecasting or trading edge is permitted below 30 resolved forecasts; 100 matched forecasts is the preferred minimum before treating a result as moderate evidence. Calibration buckets and deterministic bootstrap 95% intervals are reported only at 10 or more observations. Multiple slices reduce effective sample size and must not be treated as independent confirmation. The current two CLOSED trades are insufficient to establish edge.
