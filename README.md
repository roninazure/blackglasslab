
<div align="center">

<h1 style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
           font-size: 42px; letter-spacing: 1px; margin-bottom: 6px;">
⚗️ <span style="color:#39ff14;">BlackGlassLab</span> <span style="color:#a855f7;">v0.7</span>
</h1>

<p style="max-width: 920px; font-size: 16px; line-height: 1.5; margin-top: 0;">
An <b>evolutionary multi-agent forecasting swarm</b> that generates probabilistic YES/NO predictions, scores calibration with <b>Brier</b>,
evolves populations by <b>fitness</b>, produces <b>arbiter consensus</b>, and emits <b>paper-executed trade signals</b> — built to graduate to
<b>Polymarket</b> + <b>Kalshi</b>.
</p>

<p>
  <img src="https://img.shields.io/badge/Status-Paper%20Pilot%20Ready-39ff14?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Engine-Evolutionary%20Swarm-a855f7?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Consensus-Arbiter-00e5ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Scoring-Brier-ffd166?style=for-the-badge&labelColor=0b0f0b" />
</p>

<p>
  <img src="https://img.shields.io/badge/Venue-Polymarket%20%7C%20Kalshi-ff4d6d?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/DB-SQLite-9bf6ff?style=for-the-badge&labelColor=0b0f0b" />
  <img src="https://img.shields.io/badge/Signals-trade__candidates.json-7c3aed?style=for-the-badge&labelColor=0b0f0b" />
</p>

<hr style="border:none;height:2px;background:linear-gradient(90deg,#39ff14,#00e5ff,#a855f7,#ff4d6d); margin: 14px auto; max-width: 980px;" />

</div>

## 🧠 What this is (mission)

**BlackGlassLab is not a toy script.**  
It’s an **adaptive forecasting swarm with capital allocation logic**:

- Operators generate probabilistic forecasts (`p_yes`, rationale)
- Skeptics challenge and stress-test the forecast
- Auditor scores calibration with **Brier**
- Evolver mutates populations based on **fitness**
- Arbiter produces **consensus probability + disagreement**
- Production wrapper emits **trade candidates** + **paper trades**
- Target: **Polymarket/Kalshi** integration (paper-first → live)

---

## 🧬 Architecture (clean mental model)

<table>
<tr>
<td width="50%">

### `orchestrator.py` — the swarm loop
- Samples agents from `agent_population`
- Runs operator + skeptic swarms
- Scores with Brier via auditor
- Updates fitness
- Evolves population periodically
- Writes to SQLite:
  - `runs`
  - `agent_runs`
  - `arbiter_runs`

</td>
<td width="50%">

### `live_runner.py` — production wrapper
- Reads latest `runs` + `arbiter_runs`
- Applies risk controls (.env)
- Writes signals:
  - `signals/trade_candidates.json`
- Inserts paper executions:
  - `paper_trades` (schema-correct, NOT NULL safe)

</td>
</tr>
</table>

---

## ⚡ Quickstart (paper pilot)

### 1) Run a swarm cycle
```bash
python3 orchestrator.py

