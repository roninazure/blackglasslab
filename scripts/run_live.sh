#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."

# Load the shared runtime environment.  .env remains the legacy fallback.
RUNTIME_ENV_FILE="${BGL_RUNTIME_ENV_FILE:-.env.runtime}"
if [[ -f "$RUNTIME_ENV_FILE" ]]; then
  set -o allexport
  source "$RUNTIME_ENV_FILE"
  set +o allexport
elif [[ -f .env ]]; then
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
MAX_CONSECUTIVE_FAILURES="${MAX_CONSECUTIVE_FAILURES:-5}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

COUNT=0
CONSECUTIVE_FAILURES=0

while true; do
  if [[ -f KILL ]]; then
    echo "KILL switch present. Exiting."
    exit 0
  fi

  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : infer loop == (cycle $((COUNT + 1)))"

  # --- INFER ---
  if BGL_REQUIRE_APPROVAL="${BGL_REQUIRE_APPROVAL:-1}" \
  BGL_INFER_USE_LLM="${BGL_INFER_USE_LLM:-1}" \
  BGL_ACTIVE_UNIVERSE_SIZE="${BGL_ACTIVE_UNIVERSE_SIZE:-75}" \
  BGL_EVALUATIONS_PER_CYCLE="${BGL_EVALUATIONS_PER_CYCLE:-15}" \
  BGL_INFER_BATCH="${BGL_INFER_BATCH:-30}" \
  BGL_INFER_COOLDOWN="${BGL_INFER_COOLDOWN:-6}" \
  BGL_MAX_LLM_CALLS_PER_CYCLE="${BGL_MAX_LLM_CALLS_PER_CYCLE:-3}" \
  BGL_MAX_SKEPTIC_CALLS_PER_CYCLE="${BGL_MAX_SKEPTIC_CALLS_PER_CYCLE:-1}" \
  BGL_MAX_DAILY_LLM_CALLS="${BGL_MAX_DAILY_LLM_CALLS:-24}" \
  BGL_SHADOW_THRESHOLD_BUCKETS="${BGL_SHADOW_THRESHOLD_BUCKETS:-0.02,0.03,0.04,0.05}" \
  BGL_TIME_TO_RESOLUTION_WEIGHT="${BGL_TIME_TO_RESOLUTION_WEIGHT:-1.0}" \
  BGL_SHADOW_LEDGER_ENABLED="${BGL_SHADOW_LEDGER_ENABLED:-1}" \
  BGL_CANDIDATE_THRESHOLD_FOR_SKEPTIC="${BGL_CANDIDATE_THRESHOLD_FOR_SKEPTIC:-0.040}" \
  BGL_MIN_OPPORTUNITY_SCORE_FOR_LLM="${BGL_MIN_OPPORTUNITY_SCORE_FOR_LLM:-55}" \
  BGL_MARKET_UNIVERSE_POLICY_MODE="${BGL_MARKET_UNIVERSE_POLICY_MODE:-institutional_v2}" \
  BGL_MAX_PER_CATEGORY="${BGL_MAX_PER_CATEGORY:-3}" \
  BGL_MIN_EDGE_ABS="${BGL_MIN_EDGE_ABS:-0.040}" \
  BGL_MIN_EDGE_VS_MARKET="${BGL_MIN_EDGE_VS_MARKET:-0.040}" \
  BGL_MAX_DISAGREEMENT="${BGL_MAX_DISAGREEMENT:-0.45}" \
  BGL_MAX_DISAGREE="${BGL_MAX_DISAGREE:-0.45}" \
  "$PYTHON_BIN" live_runner.py --mode infer --source polymarket --paper --loops 1; then
    CONSECUTIVE_FAILURES=0
  else
    CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
    echo "== [WARN] live_runner.py exited non-zero (consecutive failures: $CONSECUTIVE_FAILURES / $MAX_CONSECUTIVE_FAILURES) =="
    if [[ "$CONSECUTIVE_FAILURES" -ge "$MAX_CONSECUTIVE_FAILURES" ]]; then
      echo "== [FATAL] $(date -u +%Y-%m-%dT%H:%M:%SZ) : $MAX_CONSECUTIVE_FAILURES consecutive failures — stopping loop. Check logs. =="
      exit 1
    fi
  fi

  # --- INDEPENDENT REVENUE POC PAPER LANE ---
  # Existing deployments remain unchanged unless explicitly enabled.
  if [[ "${BGL_REVENUE_POC_ENABLED:-0}" == "1" ]]; then
    "$PYTHON_BIN" scripts/revenue_poc.py --db "${BGL_DB_PATH:-memory/runs.sqlite}" \
      --ingest-shadow --dashboard --analysis \
      || echo "== [WARN] Revenue POC paper lane exited non-zero =="
  fi

  COUNT=$((COUNT + 1))

  # --- AUTO-RESOLVE every RESOLVE_EVERY cycles ---
  if (( COUNT % RESOLVE_EVERY == 0 )); then
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : auto-resolve (cycle $COUNT) =="
    "$PYTHON_BIN" scripts/resolve_paper_trades.py \
    || echo "== [WARN] resolve_paper_trades.py exited non-zero =="
  fi

  # --- AUTO-EXPORT every EXPORT_EVERY cycles ---
  if (( COUNT % EXPORT_EVERY == 0 )); then
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : auto-export data (cycle $COUNT) =="
    "$PYTHON_BIN" scripts/export_data.py \
    || echo "== [WARN] export_data.py exited non-zero =="
  fi

  # --- AUTO-DISCOVER every DISCOVER_EVERY cycles ---
  if (( COUNT % DISCOVER_EVERY == 0 )); then
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) : auto-discover watchlist (cycle $COUNT) =="
    if [[ "${SWARM_EDGE_WATCHLIST_APPLY:-0}" == "1" ]]; then
      "$PYTHON_BIN" scripts/manage_watchlist.py --apply \
      || echo "== [WARN] manage_watchlist.py exited non-zero =="
    else
      echo "== [INFO] watchlist apply disabled (SWARM_EDGE_WATCHLIST_APPLY=0) =="
    fi
  fi

  if [[ "$LOOPS" -gt 0 && "$COUNT" -ge "$LOOPS" ]]; then
    echo "Completed $COUNT loop(s). Exiting."
    exit 0
  fi

  sleep "$SLEEP_SECS"
done
