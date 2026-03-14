<div align=”center”>

<h1 style=”font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, ‘Liberation Mono’, ‘Courier New’, monospace;
           font-size: 42px; letter-spacing: 1px; margin-bottom: 6px;”>
⚗️ <span style=”color:#39ff14;”>Black Glass</span> <span style=”color:#a855f7;”>Swarm</span>
</h1>

<p style=”max-width: 920px; font-size: 16px; line-height: 1.55; margin-top: 0;”>
A live AI prediction swarm that runs 24/7, scores YES/NO prediction markets, and paper trades when it finds mispriced odds.
Built for <b>Polymarket</b> first, then <b>Kalshi</b>. Phase 2 data collection is live now.
</p>

<p style=”max-width: 920px; font-size: 14px; opacity: 0.9; margin-top: 0;”>
<b>BlackGlassLab</b> is the umbrella. <b>Swarm</b> is the flagship forecasting + trading engine.
</p>

<p>
  <img src=”https://img.shields.io/badge/Status-Live%20%7C%20Phase%202.2-39ff14?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/BlackGlassLab-a855f7?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/Venue-Polymarket-ff4d6d?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/Mode-Paper%20Trading-00e5ff?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/Trades-291%20placed-9bf6ff?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/Markets-43%20tracked-ffbe0b?style=for-the-badge&labelColor=0b0f0b” />
</p>

<hr style=”border:none;height:2px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d); margin: 14px auto; max-width: 980px;” />

</div>

## 🧠 What this is (mission)

**Black Glass Swarm is an adaptive AI forecasting engine with capital allocation logic:**

- **Operators** generate probabilistic forecasts (`p_yes`, rationale) for each market
- **Skeptics** challenge and stress-test every forecast
- **Arbiter** produces a **consensus probability + disagreement score**
- **Infer loop** runs every 5 minutes — lightweight, continuous scoring
- **Arbiter loop** runs every 30 minutes — full swarm consensus, stricter edge filters
- **Paper trades** fire when the swarm finds edge vs. the live Polymarket price
- **Brier scoring** measures calibration on every resolved trade
- **Auto-resolver** checks Polymarket API every 6 hours and closes settled trades

---

## 🎯 Why this matters

Prediction markets (Polymarket/Kalshi) are markets for probabilities.

If the swarm can reliably estimate the probability of an outcome **better than the market price**, then:

- we trade the difference (edge),
- manage risk with disagreement gates,
- build a repeatable, measurable edge over time,
- and eventually graduate from paper to **real money**.

---

## 📊 Live stats (as of March 2026)

| Metric | Value |
|--------|-------|
| Total paper trades placed | **291** |
| Open positions | **117** |
| Markets tracked | **43** |
| Agent population | **168** (32 active) |
| Total swarm runs | **736** |
| Live since | **Feb 28, 2026** |
| Infer loop cadence | Every **5 min** |
| Arbiter loop cadence | Every **30 min** |

---

## 🗺️ Watchlist (43 markets)

Markets span 5 categories, with near-term resolution targets prioritized for faster Brier feedback:

| Category | Markets | Resolution window |
|----------|---------|-------------------|
| 🏈 Sports | NBA Playoffs, Masters Golf, Arsenal Carabao Cup | 11–64 days |
| 💰 Crypto | BTC $150k (Mar 31), BTC $1M, BTC $100k (Jun) | 17–108 days |
| 🌍 Geopolitical | Ukraine ceasefire, NATO/Russia, Netanyahu | 17–291 days |
| 🏛️ US Politics | Trump/GTA VI, 2028 primary candidates | Indefinite |
| 📉 Macro/Fed | Rate cut scenarios (0–5+ cuts), Recession 2025 | ~90 days |

---

## ⚙️ Architecture

```
polymarket_watchlist.json (43 markets)
           |
           v
  [Infer Loop]   every 5 min  -->  live_runner.py --mode infer
  [Arbiter Loop] every 30 min -->  live_runner.py --mode arbiter
                                          |
                                          v
                                   Operators x3  +  Skeptics x3
                                   Arbiter consensus p_yes
                                          |
                                   edge_vs_market > threshold?
                                          |
                                   YES -> paper_trades (SQLite)
                                          |
  [Auto-Resolver] every 6 hrs  -->  Polymarket Gamma API
                                   p_yes >= 0.999 -> resolved YES
                                   p_no  >= 0.999 -> resolved NO
                                          |
                                          v
                                   Brier score + P&L recorded
```

---

## 🔁 How to run (WSL / Ubuntu)

> **Important:** This project runs in WSL only. Never use the Windows-side copy.

```bash
# Start infer loop (every 5 min, runs forever)
cd ~/blackglasslab
nohup bash scripts/run_live.sh > logs/infer_loop.log 2>&1 &

# Start arbiter loop (every 30 min, runs forever)
nohup bash scripts/run_arbiter.sh > logs/arbiter_loop.log 2>&1 &

# Check loops are alive
ps aux | grep -E ‘run_live|run_arbiter’ | grep -v grep

# View live dashboard
python3 reporting/paper_dashboard.py --venue polymarket --limit 20

# Run ship check (end-to-end health cert)
bash scripts/ship_check.sh
```

---

## 📈 Trading logic

Two edge concepts drive trade decisions:

| Concept | Formula | Purpose |
|---------|---------|---------|
| `edge_abs` | `abs(consensus_p_yes - 0.5)` | Confidence gate |
| `edge_vs_market` | `abs(consensus_p_yes - market_p_yes)` | True trade edge |

### Signal tiers

| Tier | Min edge_abs | Meaning |
|------|-------------|---------|
| **A** | >= 0.070 | Highest conviction — act immediately |
| **B** | >= 0.040 | Strong signal — monitor closely |
| **C** | >= 0.020 | Moderate signal — data collection |
| **D** | < 0.020 | Weak signal — noise reduction needed |

### Arbiter filters (stricter gate)

```
BGL_MIN_EDGE_ABS       = 0.02
BGL_MIN_EDGE_VS_MARKET = 0.01
BGL_MAX_DISAGREE       = 0.60
```

---

## 🧪 Swarm population

Swarm runs a **sampled committee** from an evolving agent population — not a single model.

| Pool | Count |
|------|-------|
| Total agents | **168** |
| Active agents | **32** |
| Used per run | **6** (3 Operators + 3 Skeptics) |

**Why disagreement matters:**
- Low disagreement → strong consensus → higher conviction trade
- High disagreement → uncertainty → position filtered out

---

## 🚀 Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| **1 — Baseline** | ✅ Complete | Agent swarm, paper trading, Brier scoring, ship_check |
| **2 — Live loops** | ✅ Active | Infer (5min) + Arbiter (30min) running 24/7 in WSL |
| **3 — LLM layer** | 🔜 Planned | Enable LLM reasoning per agent — news, geopolitics, sports form |
| **4 — Kalshi** | 🔜 Planned | Second venue, expand market universe |
| **5 — Real money** | 🔜 Pending | Graduate from paper once Brier + P&L prove consistent edge |
| **6 — Website** | 🔜 Vision | Live 3D holographic trade dashboard, real-time swarm visualization |

---

## ✅ Ship check contract

If `./scripts/ship_check.sh` prints `SHIP_CHECK PASS`, the following are guaranteed:

- `runs` has a latest `run_id`
- `arbiter_runs` has a row for that `run_id`
- `signals/trade_candidates.json` exists and `candidate[0].run_id == latest runs.run_id`
- `paper_trades` has a row for that `run_id` when `--paper` is used
- Publish step succeeded and `model_forecasts.run_id == latest runs.run_id`

**Not guaranteed:** Profitability · Candidates on every run · Market resolutions (until markets close)

> Version discipline: any change to schema, edge meanings, publish semantics, or ship_check requires a version bump + contract lock update.

---

## 📁 Key files

| File | Purpose |
|------|---------|
| `live_runner.py` | Core runner — infer and arbiter modes |
| `scripts/run_live.sh` | Infer loop (5 min cadence) |
| `scripts/run_arbiter.sh` | Arbiter loop (30 min cadence) |
| `scripts/resolve_paper_trades.py` | Auto-resolver via Polymarket API |
| `scripts/ship_check.sh` | End-to-end health certification |
| `markets/polymarket_watchlist.json` | 43-market watchlist |
| `reporting/paper_dashboard.py` | Live P&L + signal dashboard |
| `memory/runs.sqlite` | All trades, runs, forecasts (SQLite) |

---

<div align=”center”>
<p style=”font-size: 12px; opacity: 0.6;”>
BlackGlassLab · Swarm Phase 2.2 · Paper trading live since Feb 28, 2026
</p>
</div>
