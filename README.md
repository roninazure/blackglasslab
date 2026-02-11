# BlackGlassLab
A semi-random evolutionary lab for probabilistic agents.

## What it does
- Runs adversarial agents (Operator vs Skeptic)
- Scores forecasts with Brier (calibration-aware)
- Tracks performance over time in SQLite
- Applies Reaper resets when last-N reward degrades
- Logs runs + events for forensics

## Run

    python3 orchestrator.py

