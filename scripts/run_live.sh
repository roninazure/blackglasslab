#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Safety kill switch
if [[ -f KILL ]]; then
  echo "KILL switch present. Exiting."
  exit 0
fi

LOOPS="${LOOPS:-0}"          # 0 = forever
SLEEP_SECS="${SLEEP_SECS:-300}"

COUNT=0

while true; do
  if [[ -f KILL ]]; then
    echo "KILL switch present. Exiting."
    exit 0
  fi

  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : infer loop =="
  BGL_INFER_USE_LLM="${BGL_INFER_USE_LLM:-1}" \
  BGL_INFER_BATCH="${BGL_INFER_BATCH:-10}" \
  BGL_INFER_COOLDOWN="${BGL_INFER_COOLDOWN:-43}" \
  BGL_MIN_EDGE_ABS="${BGL_MIN_EDGE_ABS:-0.060}" \
  BGL_MIN_EDGE_VS_MARKET="${BGL_MIN_EDGE_VS_MARKET:-0.060}" \
  BGL_MAX_DISAGREE="${BGL_MAX_DISAGREE:-0.40}" \
  python3 live_runner.py --mode infer --source polymarket --paper --loops 1

  COUNT=$((COUNT + 1))
  if [[ "$LOOPS" -gt 0 && "$COUNT" -ge "$LOOPS" ]]; then
    echo "Completed $COUNT loop(s). Exiting."
    exit 0
  fi

  sleep "$SLEEP_SECS"
done
