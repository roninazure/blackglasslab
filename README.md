<div align="center">

<p>
  <img src="https://img.shields.io/badge/STATUS-LIVE_24%2F7-39ff14?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/VENUE-POLYMARKET-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/MODE-PAPER_TRADING-00e5ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/PHASE-PILOT_BASELINE-ffbe0b?style=for-the-badge&labelColor=0b0f0b" />
</p>

<h1 style="margin: 14px 0 6px 0; font-size: 66px; font-weight: 900; letter-spacing: 3px; line-height: 1;">
  SWARM <span style="color:#00e5ff;">EDGE</span>
</h1>

<p style="margin: 0 auto 16px auto; max-width: 920px; font-size: 13px; letter-spacing: 0.42em; text-transform: uppercase; color: #9bf6ff;">
  Market intelligence system • built in the lab • executed against live probability markets
</p>

<p style="margin: 0 auto 16px auto; max-width: 900px; font-size: 20px; line-height: 1.55;">
  <strong>168 agents</strong> forecast, challenge, arbitrate, and paper trade live prediction markets around the clock.
  <br />
  Not a demo surface. Not a toy automation.
  <strong>A continuously running edge engine built to convert market disagreement into disciplined trading decisions.</strong>
</p>

<table>
  <tr>
    <td align="center"><strong>168</strong><br />agents</td>
    <td align="center"><strong>24/7</strong><br />runtime</td>
    <td align="center"><strong>5 min</strong><br />infer cadence</td>
    <td align="center"><strong>30 min</strong><br />arbiter cadence</td>
    <td align="center"><strong>$100</strong><br />paper size</td>
  </tr>
</table>

<p style="margin: 18px auto 14px auto; max-width: 780px; font-size: 15px; line-height: 1.6;">
  <strong>Polymarket</strong> is the first arena. <strong>Kalshi</strong> is next.
  The mandate is simple: discover repeatable edge, prove it under pressure, then scale capital with discipline.
</p>

<p>
  <img src="https://img.shields.io/badge/TRADES-LIVE_PIPELINE-9bf6ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/FILTERS-EDGE_%E2%89%A5_0.05-39ff14?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/DISAGREEMENT-%E2%89%A4_0.25-a855f7?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/BASKET-4_MARKETS-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />
</p>

<p style="margin-top: 14px;">
  <code>forecast → challenge → arbitrate → filter → paper trade → resolve → learn</code>
</p>

<hr style="border:none;height:3px;background:linear-gradient(90deg,#39ff14,#00e5ff,#ff4d6d,#ffbe0b,#39ff14); margin: 24px auto; max-width: 980px;" />

</div>

## Thesis

> *”If you can model the future better than the crowd, you can trade the difference.”*

Prediction markets are **probability exchanges**. Every market is a binary question with a price — *Will X happen?* The price IS the crowd’s probability estimate.

Swarm Edge treats every market as a contest between internal judgment and external pricing. Operators make the case. Skeptics attack it. The arbiter resolves the dispute into a final probability. When that internal view **meaningfully diverges from the live market**, and disagreement stays contained, the system emits a paper trade.

---

## Flow

```
                 SWARM EDGE — CURRENT PILOT FLOW
  ───────────────────────────────────────────────────────────────

  markets/polymarket_watchlist.json  ──▶  4 exact tradable slugs
                    │
                    ▼
          sample operators + skeptics
                    │
                    ▼
         arbiter consensus_p_yes + disagreement
                    │
                    ▼
   compare against live Polymarket market probability
                    │
                    ▼
   edge >= 0.05 and disagreement <= 0.25 ?
             │
      YES ───┴───▶ paper trade ($100)
             │
       NO ───────▶ skip cleanly
```

---

## Pilot Basket

These are the active Polymarket pilot markets. The basket is intentionally narrow, liquid enough to matter, and strict enough to keep the system honest:

- `iran-leadership-change-or-us-x-iran-ceasefire-first`
- `blue-wave-in-2026`
- `fed-emergency-rate-cut-before-2027`
- `us-recession-by-end-of-2026`

---

## Decision Model

Swarm Edge is not a single-model predictor and not a blind price-follower. It is a **committee process** designed to force conflict before conviction:

```
EACH RUN:
  ┌─ sample 3 Operators  ──▶  independent p_yes forecasts
  ├─ sample 3 Skeptics   ──▶  stress-test every assumption
  └─ Arbiter             ──▶  consensus_p_yes + disagreement_score

  LOW disagreement  ──▶  strong consensus  ──▶  trade fires
  HIGH disagreement ──▶  swarm uncertain   ──▶  position filtered out
```

---

## Operate

> ⚠️ **WSL / Ubuntu only.** This project lives at `~/blackglasslab`. Never run from Windows.

```bash
cd ~/blackglasslab

# strict batch validation for the active pilot basket
BGL_MIN_EDGE_ABS=0.05 \
BGL_MAX_DISAGREEMENT=0.25 \
python scripts/validate_arbiter_watchlist.py --reset-db

# one-market arbiter run
export BGL_MARKET_ID='us-recession-by-end-of-2026'
unset BGL_MARKET_QUESTION
python orchestrator.py
python live_runner.py --mode arbiter --source polymarket --paper
```

---

## Policy

Current paper-trading pilot policy:

- Watchlist: exact tradable slugs only from `markets/polymarket_watchlist.json`
- Paper size: `$100` per candidate
- Minimum edge: `BGL_MIN_EDGE_ABS=0.05`
- Maximum disagreement: `BGL_MAX_DISAGREEMENT=0.25`
- Ambiguous event slugs must not trade
- Unpriced markets must not trade
- Standard validation path: `python scripts/validate_arbiter_watchlist.py --reset-db`

Frozen basket:

- `iran-leadership-change-or-us-x-iran-ceasefire-first`
- `blue-wave-in-2026`
- `fed-emergency-rate-cut-before-2027`
- `us-recession-by-end-of-2026`

Operator-grade validation command:

```bash
BGL_MIN_EDGE_ABS=0.05 \
BGL_MAX_DISAGREEMENT=0.25 \
python scripts/validate_arbiter_watchlist.py --reset-db
```

---

## Core Files

| FILE | ROLE |
|------|------|
| `orchestrator.py` | Writes a forecast run for the current market |
| `live_runner.py` | Arbiter paper-trade execution against live Polymarket pricing |
| `markets/polymarket_watchlist.json` | Frozen 4-market Polymarket pilot basket |
| `scripts/validate_arbiter_watchlist.py` | Standard batch validation path for the pilot basket |
| `scripts/discover_polymarket_markets.py` | Discovery funnel for review-only Polymarket market intake |
| `memory/runs.sqlite` | Runs, arbiter outputs, and paper trades |
