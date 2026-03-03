```{=html}
<h1 style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, &#39;Liberation Mono&#39;, &#39;Courier New&#39;, monospace;
           font-size: 42px; letter-spacing: 1px; margin-bottom: 6px;">
```
⚗️ [Black Glass Swarm]{style="color:#39ff14;"}
[v0.7.1]{style="color:#a855f7;"}
```{=html}
</h1>
```
```{=html}
<p style="max-width: 920px; font-size: 16px; line-height: 1.55; margin-top: 0;">
```
A digital prediction lab that runs a small team of AIs, produces a
probability on a YES/NO question, and (paper) places a trade when odds
look mispriced. Built for `<b>`{=html}Polymarket`</b>`{=html} first,
then `<b>`{=html}Kalshi`</b>`{=html}.
```{=html}
</p>
```
```{=html}
<p style="max-width: 920px; font-size: 14px; opacity: 0.9; margin-top: 0;">
```
`<b>`{=html}BlackGlassLab`</b>`{=html} is the umbrella.
`<b>`{=html}Black Glass Swarm`</b>`{=html} is the flagship forecasting +
trading engine.
```{=html}
</p>
```
```{=html}
<p>
```
`<img src="https://img.shields.io/badge/Status-Certified%20Baseline-39ff14?style=for-the-badge&labelColor=0b0f0b" />`{=html}
`<img src="https://img.shields.io/badge/Venue-Polymarket%20First-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />`{=html}
`<img src="https://img.shields.io/badge/Mode-Paper%20Trading-00e5ff?style=for-the-badge&labelColor=0b0f0b" />`{=html}
`<img src="https://img.shields.io/badge/DB-SQLite-9bf6ff?style=for-the-badge&labelColor=0b0f0b" />`{=html}
```{=html}
</p>
```
```{=html}
<hr style="border:none;height:2px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d); margin: 14px auto; max-width: 980px;" />
```
:::

------------------------------------------------------------------------

## 🧠 Mission

Black Glass Swarm is an adaptive forecasting swarm with capital
allocation logic.

-   Operators generate probabilistic forecasts (`p_yes`)
-   Skeptics challenge those forecasts
-   Arbiter produces `consensus_p_yes` and `disagreement`
-   Production wrapper emits trade candidates + paper trades
-   Publish step writes model forecast stream

Goal: trade only when meaningful.

------------------------------------------------------------------------

## 🎯 Real-World Purpose

Prediction markets are markets for probabilities.

If our probability estimate is better than the market's implied
probability, we can trade the difference (edge), manage risk, and scale
capital over time.

Paper-first. Deterministic. Auditable.

------------------------------------------------------------------------

## ✅ What Works (v0.7.1 Certified)

-   Swarm runs deterministically
-   Consensus probability generated
-   Disagreement score produced
-   Trade candidate JSON emitted
-   Paper trade inserted (schema-safe)
-   Publish step updates model_forecasts
-   One-command ship check certifies entire pipeline

Certified via:

    ./scripts/ship_check.sh

If it returns:

    SHIP_CHECK PASS

The contract is intact.

------------------------------------------------------------------------

## 🧪 Swarm Structure

Population: - 152 total agents - 76 Operators - 76 Skeptics

Active pool: - 31 agents eligible

Per run: - 3 Operators - 3 Skeptics

Arbiter combines into: - `consensus_p_yes` - `disagreement`

------------------------------------------------------------------------

## 📈 Trading Logic

### edge_abs

abs(consensus_p\_yes - 0.5)

### edge_vs_market

abs(consensus_p\_yes - p_yes_market)

Trade generation uses edge_vs_market. Confidence filters may use
edge_abs.

------------------------------------------------------------------------

## 📡 Publish Layer

-   Updates `model_forecasts`
-   UPSERT semantics
-   UNIQUE(venue, market_id)
-   Idempotent

Model stream venue = `swarm` Trade venue = `polymarket`

------------------------------------------------------------------------

# 🔒 Contract Lock --- v0.7.1

If ship_check passes:

✔ run_id exists\
✔ signals JSON matches run_id\
✔ paper trade inserted\
✔ publish updated model_forecasts\
✔ schema constraints respected

------------------------------------------------------------------------


