#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."

# Load .env if present
if [[ -f .env ]]; then
  set -o allexport
  source .env
  set +o allexport
fi

# Safety kill switch
if [[ -f KILL ]]; then
  echo "KILL switch present. Exiting."
  exit 0
fi

LOOPS="${LOOPS:-0}"          # 0 = forever
SLEEP_SECS="${SLEEP_SECS:-3600}"

COUNT=0

while true; do
  if [[ -f KILL ]]; then
    echo "KILL switch present. Exiting."
    exit 0
  fi

  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : infer loop =="
  BGL_INFER_USE_LLM="${BGL_INFER_USE_LLM:-1}" \
  BGL_INFER_BATCH="${BGL_INFER_BATCH:-5}" \
  BGL_INFER_COOLDOWN="${BGL_INFER_COOLDOWN:-15}" \
  BGL_MIN_EDGE_ABS="${BGL_MIN_EDGE_ABS:-0.040}" \
  BGL_MIN_EDGE_VS_MARKET="${BGL_MIN_EDGE_VS_MARKET:-0.040}" \
  BGL_MAX_DISAGREEMENT="${BGL_MAX_DISAGREEMENT:-0.45}" \
  BGL_MAX_DISAGREE="${BGL_MAX_DISAGREE:-0.45}" \
  python3 live_runner.py --mode infer --source polymarket --paper --loops 1 \
  || echo "== [WARN] live_runner.py exited non-zero — continuing loop =="

  COUNT=$((COUNT + 1))
  if [[ "$LOOPS" -gt 0 && "$COUNT" -ge "$LOOPS" ]]; then
    echo "Completed $COUNT loop(s). Exiting."
    exit 0
  fi

  sleep "$SLEEP_SECS"
done
