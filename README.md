<div align="center">

<h1 style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
           font-size: 42px; letter-spacing: 1px; margin-bottom: 6px;">
⚗️ <span style="color:#39ff14;">BlackGlassLab</span> <span style="color:#a855f7;">v0.7</span>
</h1>

<p style="max-width: 920px; font-size: 16px; line-height: 1.55; margin-top: 0;">
A “digital prediction lab” that runs a small team of AIs, makes a probability call on a YES/NO question,
then (paper) places a trade when the odds look mispriced. Built for <b>Polymarket</b> first, then <b>Kalshi</b>.
</p>

<p>
  <img src="https://img.shields.io/badge/Status-Paper%20Pilot%20Ready-39ff14?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Venue-Polymarket%20First-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Mode-Paper%20Trading-00e5ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/DB-SQLite-9bf6ff?style=for-the-badge&labelColor=0b0f0b" />
</p>

<hr style="border:none;height:2px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d); margin: 14px auto; max-width: 980px;" />

</div>

## 🧠 What BlackGlassLab does (in plain English)

Think of BlackGlassLab as a **mini trading desk**:

- One AI (“**Operator**”) makes a prediction.
- Another AI (“**Skeptic**”) challenges it.
- A referee (“**Arbiter**”) produces the final **consensus probability**.
- The system checks: **Are the market odds different enough from our probability to justify a trade?**
- If yes, it creates a **trade signal** and logs a **paper trade** (simulated trade) so everything is auditable.

This phase is **paper trading only** — no real money execution yet.

---

## 🎯 Why this matters (real-world implication)

Prediction markets (Polymarket/Kalshi) are basically “**markets for probabilities**.”

If our system can reliably estimate the probability of an outcome **better than the market price**, then:

- we can trade the difference (edge),
- manage risk,
- and potentially build a repeatable income stream.

---

## 🔥 The big idea

**BlackGlassLab is an autonomous forecasting swarm** that can improve over time.

It does this by keeping score:

- When it’s right, it gets rewarded.
- When it’s wrong (or overconfident), it gets penalized.
- Better strategies survive and get copied.
- Weaker strategies get replaced (“mutations” are spawned).

Over many runs, the goal is to become **more accurate and more profitable**.

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

BlackGlassLab doesn’t run “one model.” It runs a **small committee** sampled from a larger evolving population.

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

> In other words: **big population for diversity**, **small swarm per run for speed**, and **active set evolves over time**.



