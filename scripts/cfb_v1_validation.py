"""Run the one-shot chronological CFB V1 validation; no market I/O."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parallax.cfb import fetch_games, validate

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, help="Optional private JSON cache of CFBD game rows")
    args = parser.parse_args()
    if args.input:
        games = __import__("parallax.cfb", fromlist=["parse_games"]).parse_games(args.input.read_text())
    else:
        games = fetch_games(tuple(range(2010, 2026)))
    print(json.dumps(validate(games), indent=2, sort_keys=True, allow_nan=False))

if __name__ == "__main__":
    main()
