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

LOOPS="${LOOPS:-0}"           # 0 = forever
SLEEP_SECS="${SLEEP_SECS:-3600}"
RESOLVE_EVERY="${RESOLVE_EVERY:-6}"    # resolve closed trades every N cycles
EXPORT_EVERY="${EXPORT_EVERY:-6}"      # export data to JSON + push every N cycles
DISCOVER_EVERY="${DISCOVER_EVERY:-24}" # refresh watchlist every N cycles

COUNT=0

while true; do
  if [[ -f KILL ]]; then
    echo "KILL switch present. Exiting."
    exit 0
  fi

  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : infer loop == (cycle $((COUNT + 1)))"

  # --- INFER ---
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

  # --- AUTO-RESOLVE every RESOLVE_EVERY cycles ---
  if (( COUNT % RESOLVE_EVERY == 0 )); then
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : auto-resolve (cycle $COUNT) =="
    python3 scripts/resolve_paper_trades.py \
    || echo "== [WARN] resolve_paper_trades.py exited non-zero =="
  fi

  # --- AUTO-EXPORT every EXPORT_EVERY cycles ---
  if (( COUNT % EXPORT_EVERY == 0 )); then
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : auto-export data (cycle $COUNT) =="
    python3 scripts/export_data.py \
    || echo "== [WARN] export_data.py exited non-zero =="
  fi

  # --- AUTO-DISCOVER every DISCOVER_EVERY cycles ---
  if (( COUNT % DISCOVER_EVERY == 0 )); then
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : auto-discover watchlist (cycle $COUNT) =="
    python3 scripts/manage_watchlist.py --apply \
    || echo "== [WARN] manage_watchlist.py exited non-zero =="
  fi

  if [[ "$LOOPS" -gt 0 && "$COUNT" -ge "$LOOPS" ]]; then
    echo "Completed $COUNT loop(s). Exiting."
    exit 0
  fi

  sleep "$SLEEP_SECS"
done
