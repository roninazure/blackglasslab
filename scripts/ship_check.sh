#!/usr/bin/env bash
set -euo pipefail

echo "== Phase 1.1 Ship Check =="

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

db="memory/runs.sqlite"

SOURCE="${BGL_SOURCE:-fake}"
PAPER_SIZE="${BGL_PAPER_SIZE:-100}"

MIN_EDGE="${BGL_MIN_EDGE:-0.0}"
MAX_DISAGREE="${BGL_MAX_DISAGREE:-1.0}"

echo "root=$ROOT"
echo "db=$db"
echo "source=$SOURCE paper_size=$PAPER_SIZE"
echo "ship_check_overrides: min_edge=$MIN_EDGE max_disagree=$MAX_DISAGREE"
echo

echo "1) Compile gate..."
python3 -m py_compile orchestrator.py live_runner.py

echo "2) Run orchestrator..."
python3 orchestrator.py

echo "3) Latest run_id from runs..."
RID="$(sqlite3 "$db" "select run_id from runs order by id desc limit 1;")"
echo "latest run_id=$RID"
echo

echo "4) Run live_runner in ARBITER mode..."
rm -f signals/trade_candidates.json
mkdir -p signals

BGL_MIN_EDGE="$MIN_EDGE" \
BGL_MIN_EDGE_ABS="$MIN_EDGE" \
BGL_MIN_EDGE_VS_MARKET="$MIN_EDGE" \
BGL_MAX_DISAGREE="$MAX_DISAGREE" \
BGL_MAX_DISAGREEMENT="$MAX_DISAGREE" \
BGL_PAPER_SIZE="$PAPER_SIZE" \
python3 live_runner.py --source polymarket --paper --mode arbiter

echo "5) Verify signals JSON exists + run_id matches..."
test -f signals/trade_candidates.json || { echo "SHIP_CHECK FAIL: Missing signals/trade_candidates.json"; exit 1; }

SIG_RID="$(python3 - <<'PY'
import json
with open("signals/trade_candidates.json","r",encoding="utf-8") as f:
    arr=json.load(f)
print(arr[0].get("run_id","") if arr else "")
PY
)"

if [[ -z "$SIG_RID" ]]; then
  echo "SHIP_CHECK FAIL: signals JSON is empty (no candidate emitted)"
  exit 1
fi

if [[ "$SIG_RID" != "$RID" ]]; then
  echo "SHIP_CHECK FAIL: signals run_id mismatch (signals=$SIG_RID db=$RID)"
  exit 1
fi

echo "signals OK"
echo

echo "6) DB proofs..."
echo "LATEST_RUN_ID|$RID"
sqlite3 "$db" "select 'ARBITER_COUNT|' || count(*) from arbiter_runs where run_id='$RID';"
sqlite3 "$db" "select 'PAPER_TRADE_FOR_RUN_COUNT|' || count(*) from paper_trades where run_id='$RID';"
sqlite3 "$db" "select 'PAPER_TRADE_FOR_RUN_ROW|' || id || '|' || run_id || '|' || venue || '|' || reason || '|' || status || '|' || market_id || '|' || side || '|' || consensus_p_yes || '|' || disagreement || '|' || size_usd from paper_trades where run_id='$RID' order by id desc limit 1;"

echo
echo "SHIP_CHECK PASS: run_id=$RID"
