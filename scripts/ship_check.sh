#!/usr/bin/env bash
set -euo pipefail

echo "== Phase 1.7 Ship Check (Arbiter + Publish) =="

# Always anchor to repo root (no cwd surprises)
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

DB="memory/runs.sqlite"
SOURCE="${SOURCE:-fake}"
PAPER_SIZE="${PAPER_SIZE:-100}"
MIN_EDGE="${MIN_EDGE:-0.0}"
MAX_DISAGREE="${MAX_DISAGREE:-1.0}"

echo "root=$root"
echo "db=$DB"
echo "source=$SOURCE paper_size=$PAPER_SIZE"
echo "ship_check_overrides: min_edge=$MIN_EDGE max_disagree=$MAX_DISAGREE"
echo

mkdir -p memory signals logs

# Always start clean so we never validate a stale file
rm -f signals/trade_candidates_arbiter.json

echo "1) Compile gate..."
python3 -m py_compile orchestrator.py live_runner.py scripts/publish_latest_swarm_forecasts.py

echo "2) Run orchestrator..."
python3 orchestrator.py

echo "3) Latest run_id from runs..."
latest_run_id="$(sqlite3 "$DB" "select run_id from runs order by id desc limit 1;")"
if [[ -z "$latest_run_id" ]]; then
  echo "SHIP_CHECK FAIL: latest_run_id empty"
  exit 1
fi
echo "latest run_id=$latest_run_id"
echo

echo "4) Run live_runner in ARBITER mode..."
BGL_MIN_EDGE_ABS="$MIN_EDGE" \
BGL_MIN_EDGE_VS_MARKET="$MIN_EDGE" \
BGL_MAX_DISAGREE="$MAX_DISAGREE" \
BGL_MAX_DISAGREEMENT="$MAX_DISAGREE" \
BGL_PAPER_SIZE="$PAPER_SIZE" \
python3 live_runner.py --source polymarket --paper --mode arbiter --loops 1

echo "5) Verify arbiter signals JSON exists + run_id matches..."
test -f signals/trade_candidates_arbiter.json || { echo "SHIP_CHECK FAIL: Missing signals/trade_candidates_arbiter.json"; exit 1; }

sig_rid="$(python3 - <<'PY'
import json
p="signals/trade_candidates_arbiter.json"
with open(p,"r",encoding="utf-8") as f:
    data=json.load(f)
print("" if not data else data[0].get("run_id",""))
PY
)"
if [[ -z "$sig_rid" ]]; then
  echo "SHIP_CHECK FAIL: trade_candidates_arbiter.json had no candidate run_id"
  exit 1
fi
if [[ "$sig_rid" != "$latest_run_id" ]]; then
  echo "SHIP_CHECK FAIL: signals run_id=$sig_rid does not match latest run_id=$latest_run_id"
  exit 1
fi
echo "signals OK"
echo

echo "6) DB proofs..."
echo "LATEST_RUN_ID|$latest_run_id"
arb_count="$(sqlite3 "$DB" "select count(*) from arbiter_runs where run_id='$latest_run_id';")"
echo "ARBITER_COUNT|$arb_count"
paper_cnt="$(sqlite3 "$DB" "select count(*) from paper_trades where run_id='$latest_run_id';")"
echo "PAPER_TRADE_FOR_RUN_COUNT|$paper_cnt"
paper_row="$(sqlite3 "$DB" "select id,run_id,venue,reason,status,market_id,side,consensus_p_yes,disagreement,size_usd from paper_trades where run_id='$latest_run_id' order by id desc limit 1;")"
echo "PAPER_TRADE_FOR_RUN_ROW|$paper_row"
echo

echo "7) Publish latest swarm forecast + verify model_forecasts.run_id matches..."
python3 scripts/publish_latest_swarm_forecasts.py

pub_rid="$(sqlite3 "$DB" "select run_id from model_forecasts order by ts_utc desc limit 1;")"
if [[ -z "$pub_rid" ]]; then
  echo "SHIP_CHECK FAIL: model_forecasts has no rows"
  exit 1
fi
if [[ "$pub_rid" != "$latest_run_id" ]]; then
  echo "SHIP_CHECK FAIL: model_forecasts.run_id=$pub_rid does not match latest run_id=$latest_run_id"
  exit 1
fi
echo "publish OK"
echo

echo "SHIP_CHECK PASS: run_id=$latest_run_id"
