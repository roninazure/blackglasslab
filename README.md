<div align=”center”>

<h1 style=”font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, ‘Liberation Mono’, ‘Courier New’, monospace;
           font-size: 64px; font-weight: 900; letter-spacing: 4px; margin-bottom: 4px; margin-top: 4px;”>
⚗️ <span style=”color:#39ff14;”>S</span><span style=”color:#00e5ff;”>W</span><span style=”color:#a855f7;”>A</span><span style=”color:#ff4d6d;”>R</span><span style=”color:#ffbe0b;”>M</span>
</h1>

<p style=”font-family: ui-monospace, monospace; font-size: 12px; letter-spacing: 3px; color: #ff4d6d; margin-top: 0; margin-bottom: 14px;”>
◈ &nbsp; A N &nbsp; A I &nbsp; P R E D I C T I O N &nbsp; S W A R M &nbsp; · &nbsp; B U I L T &nbsp; I N &nbsp; T H E &nbsp; L A B &nbsp; · &nbsp; R U N N I N G &nbsp; I N &nbsp; T H E &nbsp; W I L D &nbsp; ◈
</p>

<p style=”max-width: 820px; font-size: 16px; line-height: 1.6; margin: 0 auto 10px;”>
<b>168 AI agents</b> run 24/7, debate the future, and paper trade prediction markets when they find mispriced odds.<br/>
Not a demo. Not a prototype. <b>A live autonomous trading intelligence — evolving every 5 minutes.</b>
</p>

<p style=”max-width: 720px; font-size: 13px; opacity: 0.8; margin: 0 auto 16px;”>
<b>Polymarket</b> is the first arena. &nbsp;·&nbsp; <b>Kalshi</b> is next. &nbsp;·&nbsp; <b>Real capital</b> is the endgame.
</p>

<p>
  <img src=”https://img.shields.io/badge/STATUS-LIVE_24%2F7-39ff14?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/PHASE-2.2_ACTIVE-a855f7?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/VENUE-POLYMARKET-ff4d6d?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/MODE-PAPER_TRADING-00e5ff?style=for-the-badge&labelColor=0b0f0b” />
</p>
<p>
  <img src=”https://img.shields.io/badge/TRADES-291_PLACED-9bf6ff?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/MARKETS-43_TRACKED-ffbe0b?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/AGENTS-168_IN_POPULATION-39ff14?style=for-the-badge&labelColor=0b0f0b” />
  <img src=”https://img.shields.io/badge/RUNS-736_TOTAL-a855f7?style=for-the-badge&labelColor=0b0f0b” />
</p>

<hr style=”border:none;height:3px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d,#39ff14); margin: 18px auto; max-width: 980px;” />

</div>

## ⚡ THE CONCEPT

> *”If you can model the future better than the crowd, you can trade the difference.”*

Prediction markets are **probability exchanges**. Every market is a binary question with a price — *Will X happen?* The price IS the crowd’s probability estimate.

Swarm runs a committee of AI agents — Operators and Skeptics — that independently forecast each market, debate the outcome, and reach a consensus probability. When the swarm’s consensus **diverges from the market price** by a sufficient edge, it places a trade.

When enough edge is proven, it trades **real capital**.

---

## 🧬 SIGNAL PIPELINE

```
                     SWARM — SIGNAL PIPELINE
  ──────────────────────────────────────────────────────────────────

  polymarket_watchlist.json  ──▶  43 markets · 5 categories
               │
        ┌──────┴──────┐
        │             │
   [INFER LOOP]  [ARBITER LOOP]
    every 5 min   every 30 min
    lightweight   full consensus · tighter filters
        │             │
        └──────┬──────┘
               │
               ▼
  ┌── OPERATORS  (3 sampled)  ──▶  independent p_yes + rationale
  ├── SKEPTICS   (3 sampled)  ──▶  challenge every assumption
  └── ARBITER                 ──▶  consensus_p_yes + disagreement
               │
               ▼
  edge_vs_market > threshold?  ──  YES  ──▶  paper_trade (SQLite)
                                                     │
  [AUTO-RESOLVER]  every 6h  ──▶  Polymarket API     │
      p_yes ≥ 0.999  ──▶  resolved YES               ▼
      p_no  ≥ 0.999  ──▶  resolved NO        Brier score + P&L
```

---

## 📡 LIVE TELEMETRY — March 2026

<div align=”center”>

| 🔴 METRIC | VALUE |
|-----------|-------|
| Paper trades placed | **291** |
| Open positions | **117** |
| Markets tracked | **43** |
| Agent population | **168** (32 active per run) |
| Total swarm runs | **736** |
| Operational since | **Feb 28, 2026** |
| Infer loop cadence | **Every 5 min** |
| Arbiter loop cadence | **Every 30 min** |
| Auto-resolution | **Every 6 hrs via cron** |

</div>

---

## 🎯 TARGET MARKETS — 43 ACTIVE

Weighted toward near-term resolution for maximum Brier score feedback velocity:

| CATEGORY | MARKETS | RESOLVES |
|----------|---------|----------|
| 🏈 **Sports** | NBA Playoffs · Masters Golf · Arsenal Carabao Cup | 11–64 days |
| 💰 **Crypto** | BTC $150k (Mar 31) · BTC $1M · BTC $100k (Jun) | 17–108 days |
| 🌍 **Geopolitical** | Ukraine ceasefire · NATO/Russia clash · Netanyahu | 17–291 days |
| 🏛️ **US Politics** | Trump removal · 2028 Democratic primary | Indefinite |
| 📉 **Macro / Fed** | 0–5+ rate cuts 2026 · US Recession 2026 | ~90–300 days |

---

## 🔬 HOW THE SWARM THINKS

Not one model. Not a single LLM call. A **living committee of 168 agents** — sampled, debated, and arbitered on every run.

```
EACH RUN:
  ┌─ sample 3 Operators  ──▶  independent p_yes forecasts
  ├─ sample 3 Skeptics   ──▶  stress-test every assumption
  └─ Arbiter             ──▶  consensus_p_yes + disagreement_score

  LOW disagreement  ──▶  strong consensus  ──▶  trade fires
  HIGH disagreement ──▶  swarm uncertain   ──▶  position filtered out
```

### Signal Tiers — Edge Classification

| TIER | THRESHOLD | CLASSIFICATION |
|------|-----------|----------------|
| 🔴 **A** | edge_abs ≥ 0.070 | Maximum conviction — act immediately |
| 🟠 **B** | edge_abs ≥ 0.040 | Strong signal — monitor closely |
| 🟡 **C** | edge_abs ≥ 0.020 | Moderate signal — data collection mode |
| ⚪ **D** | edge_abs < 0.020 | Noise floor — observation only |

---

## 💻 DEPLOY THE SWARM

> ⚠️ **WSL / Ubuntu only.** This project lives at `~/blackglasslab`. Never run from the Windows copy.

```bash
cd ~/blackglasslab

# ── LAUNCH INFER LOOP  (every 5 min · runs forever) ───────────────
nohup bash scripts/run_live.sh > logs/infer_loop.log 2>&1 &

# ── LAUNCH ARBITER LOOP  (every 30 min · runs forever) ────────────
nohup bash scripts/run_arbiter.sh > logs/arbiter_loop.log 2>&1 &

# ── VERIFY BOTH LOOPS ARE ALIVE ───────────────────────────────────
ps aux | grep -E ‘run_live|run_arbiter’ | grep -v grep

# ── LIVE P&L + SIGNAL DASHBOARD ──────────────────────────────────
python3 reporting/paper_dashboard.py --venue polymarket --limit 20

# ── END-TO-END HEALTH CERTIFICATION ──────────────────────────────
bash scripts/ship_check.sh
```

---

## 🚀 MISSION ROADMAP

<div align=”center”>
<p style=”font-family: ui-monospace, monospace; font-size: 11px; letter-spacing: 3px; color: #a855f7; opacity: 0.8;”>
◈ PHASE PROGRESSION · THE ENDGAME IS REAL CAPITAL ◈
</p>
</div>

| PHASE | STATUS | MISSION OBJECTIVE |
|-------|--------|-------------------|
| **① Baseline** | ✅ **COMPLETE** | Swarm built · paper trading live · Brier scoring · ship_check certified |
| **② Live Loops** | ✅ **ACTIVE NOW** | Infer + Arbiter running 24/7 · auto-resolver on cron · 736 runs |
| **③ LLM Layer** | 🔬 **IN THE LAB** | Wire LLM reasoning into every agent — news, geopolitics, sports form |
| **④ Kalshi** | 📡 **PLANNED** | Second venue · larger market universe · cross-venue signals |
| **⑤ Real Money** | 💰 **PENDING PROOF** | Proven Brier edge → live capital allocation → real P&L |
| **⑥ Command Center** | 🖥️ **THE VISION** | 3D holographic trade dashboard · live swarm visualization · real-time everything |

---

## 🔒 SHIP CHECK CONTRACT

```bash
bash scripts/ship_check.sh   # output must contain: SHIP_CHECK PASS
```

**On PASS, the following are guaranteed:**
- `runs` → latest `run_id` written
- `arbiter_runs` → row exists for that `run_id`
- `signals/trade_candidates.json` → `candidate[0].run_id` matches latest run
- `paper_trades` → valid row inserted when `--paper` is passed
- `model_forecasts` → UPSERT succeeded for `(venue=’swarm’, market_id=<slug>)`

**Not guaranteed:** Profitability · A candidate on every run · Market closure before deadline

> *Version discipline: any change to schema, edge semantics, or publish logic requires a version bump and contract update.*

---

## 📁 SYSTEM FILES

| FILE | ROLE |
|------|------|
| `live_runner.py` | Core engine — infer and arbiter execution |
| `scripts/run_live.sh` | Infer loop daemon (5 min) |
| `scripts/run_arbiter.sh` | Arbiter loop daemon (30 min) |
| `scripts/resolve_paper_trades.py` | Auto-resolver via Polymarket Gamma API |
| `scripts/ship_check.sh` | End-to-end health certification |
| `markets/polymarket_watchlist.json` | 43-market target watchlist |
| `reporting/paper_dashboard.py` | Live P&L + signal tier dashboard |
| `memory/runs.sqlite` | All data — trades, runs, agents, forecasts |

---

<div align=”center”>

<hr style=”border:none;height:3px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d,#39ff14); margin: 16px auto; max-width: 980px;” />

<p style=”font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, ‘Courier New’, monospace;
          font-size: 11px; letter-spacing: 3px; color: #39ff14; opacity: 0.6; margin: 0;”>
⚗️ &nbsp; SWARM &nbsp;·&nbsp; PHASE 2.2 &nbsp;·&nbsp; AUTOMATED TRADING LIVE SINCE FEB 2026<br/>
REAL CAPITAL IS THE ENDGAME
</p>

</div>
