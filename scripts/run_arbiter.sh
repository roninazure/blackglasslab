#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Safety kill switch
if [[ -f KILL ]]; then
  echo "KILL switch present. Exiting."
  exit 0
fi

LOOPS="${LOOPS:-0}"
SLEEP_SECS="${SLEEP_SECS:-1800}"   # 30 min default

COUNT=0

while true; do
  if [[ -f KILL ]]; then
    echo "KILL switch present. Exiting."
    exit 0
  fi

  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : arbiter loop =="
  BGL_MIN_EDGE_ABS="${BGL_MIN_EDGE_ABS:-0.030}" \
  BGL_MIN_EDGE_VS_MARKET="${BGL_MIN_EDGE_VS_MARKET:-0.030}" \
  BGL_MAX_DISAGREEMENT="${BGL_MAX_DISAGREEMENT:-0.45}" \
  BGL_MAX_DISAGREE="${BGL_MAX_DISAGREE:-0.45}" \
  python3 live_runner.py --mode arbiter --source polymarket --paper --loops 1

  COUNT=$((COUNT + 1))
  if [[ "$LOOPS" -gt 0 && "$COUNT" -ge "$LOOPS" ]]; then
    echo "Completed $COUNT loop(s). Exiting."
    exit 0
  fi

  sleep "$SLEEP_SECS"
done
