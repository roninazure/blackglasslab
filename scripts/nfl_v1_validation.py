"""Single bounded NFL V1 historical validation attempt; no market I/O."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from datetime import datetime, UTC

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parallax.nfl import fetch_games, validate


def main() -> None:
    games = fetch_games()
    # Bound the attempt to the latest 20 completed seasons available; this is
    # ample for a leakage audit and avoids uncontrolled historical work.
    completed_through = datetime.now(UTC).year - 1
    seasons = sorted({game.season for game in games if game.season <= completed_through})
    selected = set(seasons[-20:])
    result = validate([game for game in games if game.season in selected])
    raw, selected = result.metrics["elo_raw"], result.metrics["elo_selected"]
    beats_baseline = selected["brier"] < result.metrics["naive_50"]["brier"] and selected["log_loss"] < result.metrics["naive_50"]["log_loss"]
    calibration_reasonable = selected["ece"] <= raw["ece"]
    decision = "GO" if beats_baseline and calibration_reasonable else "IMPROVE"
    print(json.dumps({"source": result.source, "license_status": result.license_status, "fields_used": result.fields_used, "seasons": result.seasons, "train_seasons": result.train_seasons, "calibration_seasons": result.calibration_seasons, "holdout_seasons": result.holdout_seasons, "sample_size": result.sample_size, "metrics": result.metrics, "leakage_check": result.leakage_check, "reproducible": result.reproducible, "decision": decision, "decision_reason": "selected policy failed holdout calibration comparison" if not calibration_reasonable else "holdout improvement and calibration acceptable"}, allow_nan=False))


if __name__ == "__main__":
    main()
