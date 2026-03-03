<div align="center">

<h1 style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
           font-size: 42px; letter-spacing: 1px; margin-bottom: 6px;">
⚗️ <span style="color:#39ff14;">Black Glass Swarm</span> <span style="color:#a855f7;">v0.7.1</span>
</h1>

<p style="max-width: 920px; font-size: 16px; line-height: 1.55; margin-top: 0;">
A “digital prediction lab” that runs a small team of AIs, makes a probability call on a YES/NO question,
then (paper) places a trade when the odds look mispriced. Built for <b>Polymarket</b> first, then <b>Kalshi</b>.
</p>

<p style="max-width: 920px; font-size: 14px; opacity: 0.9; margin-top: 0;">
<b>BlackGlassLab</b> is the umbrella. <b>Black Glass Swarm</b> is the flagship forecasting + trading engine.
</p>

<p>
  <img src="https://img.shields.io/badge/Status-Certified%20Baseline-39ff14?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/BlackGlassLab-a855f7?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Venue-Polymarket%20First-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Mode-Paper%20Trading-00e5ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/DB-SQLite-9bf6ff?style=for-the-badge&labelColor=0b0f0b" />
</p>

<hr style="border:none;height:2px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d); margin: 14px auto; max-width: 980px;" />

</div>

## 🧠 What this is (mission)

**Black Glass Swarm is an adaptive forecasting swarm with capital allocation logic:**

- Operators generate probabilistic forecasts (`p_yes`, rationale)
- Skeptics challenge and stress-test the forecast
- Arbiter produces **consensus probability + disagreement**
- Production wrapper emits **trade candidates** + **paper trades**
- Publish step maintains a latest snapshot in `model_forecasts` (venue=`swarm`)
- Target: **Polymarket/Kalshi** integration (paper-first → live)

---

## 🎯 Why this matters (real-world implication)

Prediction markets (Polymarket/Kalshi) are basically “markets for probabilities”.

If our system can reliably estimate the probability of an outcome **better than the market price**, then:

- we can trade the difference (edge),
- manage risk,
- build a repeatable income stream.

---

## ✅ What’s working right now (v0.7.1 pilot)

- Runs the AI “Operator vs Skeptic” loop
- Produces a consensus probability (Arbiter)
- Writes results to SQLite (proof + history)
- Generates a trade candidate (`signals/trade_candidates.json`)
- Inserts a valid paper trade record (no schema errors)
- Publishes the latest swarm forecast to `model_forecasts` (UPSERT; UNIQUE(venue, market_id))
- Includes a one-command “ship check” that proves it works end-to-end

---

## 🧪 Swarm scale (real numbers)

Black Glass Swarm doesn’t run “one model.” It runs a **small committee** sampled from a larger evolving population.

**Current population (from SQLite):**
- **Total agents:** **152**
  - **76 Operators**
  - **76 Skeptics**
- **Active pool:** **31**
  - **15 active Operators**
  - **16 active Skeptics**
- **Used per run:** **6**
  - **3 Operators + 3 Skeptics**

### Why disagreement matters (risk)
- Lower disagreement → stronger consensus
- Higher disagreement → more uncertainty

---

## 📈 Trading logic (edge semantics)

Black Glass Swarm uses two “edge” concepts:

- **edge_abs (confidence):** `abs(consensus_p_yes - 0.5)`
- **edge_vs_market (true trade edge):** `abs(consensus_p_yes - p_yes_market)`

`live_runner` filters may use **edge_abs** as a minimum-confidence gate.  
Infer-mode (Phase 1.7) will emphasize **edge_vs_market** with real market odds.

### Polymarket-first adapter (current state)
- `--source polymarket` routes through the Polymarket adapter lane
- In the certified baseline, market probability may be stubbed (`p_yes_market = 0.50`) until live odds ingestion is enabled
- Determinism and auditability take priority over live calls in v0.7.1

---

## 📡 Publish (what it does)

`scripts/publish_latest_swarm_forecasts.py` writes the **latest swarm forecast snapshot** into:

`model_forecasts(venue, market_id, ts_utc, p_yes_model, disagreement, run_id, notes)`

- `venue` is **always** `swarm` for this published stream
- `market_id` is the Polymarket slug stored in `runs.market_id`
- Uses UPSERT to satisfy UNIQUE(venue, market_id)
- Safe to re-run (idempotent)

This is what your dashboards / downstream consumers should read to get the current “official” swarm probability per market.

---

# 🔒 Contract Lock — v0.7.1 Certified Baseline

If `./scripts/ship_check.sh` prints `SHIP_CHECK PASS`, the following are guaranteed:

- `runs` has a latest `run_id`
- `arbiter_runs` has a row for that `run_id`
- `signals/trade_candidates.json` exists and candidate[0].run_id == latest `runs.run_id`
- `paper_trades` has a row for that `run_id` when `--paper` is used
- Publish step succeeded and `model_forecasts.run_id == latest runs.run_id` for `(venue='swarm', market_id=<slug>)`

Not guaranteed:
- Profitability
- Candidates on every run
- Live odds ingestion (until enabled)
- Market resolutions (until markets close)

Version discipline:
- Any change to schema, edge meanings, publish semantics, or ship_check requires a version bump + this Contract Lock update.
