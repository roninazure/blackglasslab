<div align="center">

<h1 style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
           font-size: 42px; letter-spacing: 1px; margin-bottom: 6px;">
⚗️ <span style="color:#39ff14;">Black Glass Swarm</span> <span style="color:#a855f7;">v0.7</span>
</h1>

<p style="max-width: 920px; font-size: 16px; line-height: 1.55; margin-top: 0;">
A “digital prediction lab” that runs a small team of AIs, makes a probability call on a YES/NO question,
then (paper) places a trade when the odds look mispriced. Built for <b>Polymarket</b> first, then <b>Kalshi</b>.
</p>

<p style="max-width: 920px; font-size: 14px; opacity: 0.9; margin-top: 0;">
<b>BlackGlassLab</b> is the umbrella. <b>Black Glass Swarm</b> is the flagship forecasting + trading engine.
</p>

<p>
  <img src="https://img.shields.io/badge/Status-Paper%20Pilot%20Ready-39ff14?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/BlackGlassLab-a855f7?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Venue-Polymarket%20First-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Mode-Paper%20Trading-00e5ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/DB-SQLite-9bf6ff?style=for-the-badge&labelColor=0b0f0b" />
</p>

<hr style="border:none;height:2px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d); margin: 14px auto; max-width: 980px;" />

</div>

## 🧠 What this is (mission)

**Black Glass Swarm is an **adaptive forecasting swarm with capital allocation logic**:

- Operators generate probabilistic forecasts (`p_yes`, rationale)
- Skeptics challenge and stress-test the forecast
- Auditor scores calibration with **Brier**
- Evolver mutates populations based on **fitness**
- Arbiter produces **consensus probability + disagreement**
- Production wrapper emits **trade candidates** + **paper trades**
- Target: **Polymarket/Kalshi** integration (paper-first → live)

---

## 🎯 Why this matters (real-world implication)

Prediction markets (Polymarket/Kalshi) are basically “**markets for probabilities**.”

If our system can reliably estimate the probability of an outcome **better than the market price**, then:

- we can trade the difference (edge),
- manage risk,
- build a repeatable income stream.

---

## ✅ What’s working right now (v0.7 pilot)

- Runs the AI “Operator vs Skeptic” loop
- Produces a consensus probability (Arbiter)
- Writes results to a database (for proof + history)
- Generates a trade candidate
- Writes a signal file you can show in a demo
- Inserts a valid paper trade record (no schema errors)
- Includes a one-command “ship check” that proves it works end-to-end

---

## 🧪 Swarm scale (real numbers)

Black Glass Swarm doesn’t run “one model.” It runs a **small committee** sampled from a larger evolving population.

**Current population (from SQLite):**
- **Total agents:** **152**
  - **76 Operators** (they make a probability call)
  - **76 Skeptics** (they challenge the call)
- **Active pool (eligible to be sampled today):** **31**
  - **15 active Operators**
  - **16 active Skeptics**
- **Used per run (the swarm size):** **6**
  - **3 Operators + 3 Skeptics** per forecasting run

### How these numbers are used
- The system maintains a **large pool** (152) as a “gene bank” of strategies.
- A smaller **active set** (31) is the “starting lineup” — the agents allowed to participate right now.
- Each run uses only **6 agents** to keep the loop fast and repeatable:
  - 3 Operators generate predictions
  - 3 Skeptics challenge them
  - The **Arbiter** combines those 6 opinions into:
    - a final probability (`consensus_p_yes`)
    - and an “internal disagreement” score (`disagreement`)
- Over time, the **Evolver** updates which agents are active:
  - top performers stay active
  - weaker ones get replaced by new mutated variants

### Why this matters (for trading)
- The system is designed to **improve over time** instead of staying fixed.
- The “disagreement” score helps risk control:
  - lower disagreement → stronger consensus
  - higher disagreement → more uncertainty

## 📈 Trading logic update (Phase 1.2)

Black Glass Swarm now measures “edge” the way a trader would:

- **Model probability**: what the swarm believes (`consensus_p_yes`)
- **Market probability**: what the venue implies (`p_yes_market`)
- **Edge vs market**: the gap between them  
  `edge = |consensus_p_yes − p_yes_market|`

When edge is large enough (and disagreement is acceptable), the system generates a trade candidate.

### Polymarket-first adapter (current state)
We introduced an adapter layer so the engine can plug into real venues cleanly:

- `--source polymarket` now routes through a **Polymarket adapter**
- In **Phase 1.2**, that adapter is a **safe stub** (no live API calls yet)
- Market probability is currently a neutral baseline (**p_yes_market = 0.50**) until Phase 1.3 adds real odds ingestion

To preserve auditability, each paper trade records the market probability and edge in `notes` (JSON).

### Paper trade dedupe (safety)
Re-running `live_runner.py --paper` on the **same run** will not create duplicate OPEN trades:

- First run: inserts the paper trade (`paper=inserted`)
- Repeat run for the same run_id: skips insert (`paper=skipped_duplicate`)
- Signals JSON is still written when a candidate exists

> In other words: **big population for diversity**, **small swarm per run for speed**, and **active set evolves over time**.

---
