#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="memory/runs.sqlite"
SIGNALS_PATH="signals/trade_candidates.json"
ASSERT_SQL="scripts/assert_db.sql"

SOURCE="${BGL_SOURCE:-polymarket}"
PAPER_SIZE="${BGL_PAPER_SIZE_USD:-100}"

# Deterministic ship-check overrides (do NOT depend on market randomness)
SHIP_MIN_EDGE="${BGL_SHIP_MIN_EDGE:-0.0}"
SHIP_MAX_DISAGREE="${BGL_SHIP_MAX_DISAGREE:-1.0}"

fail() { echo "SHIP_CHECK FAIL: $*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"; }

need_cmd python3
need_cmd sqlite3

[ -f "$DB_PATH" ] || fail "Missing DB at $DB_PATH"
[ -f "orchestrator.py" ] || fail "Missing orchestrator.py"
[ -f "live_runner.py" ] || fail "Missing live_runner.py"
[ -f "$ASSERT_SQL" ] || fail "Missing $ASSERT_SQL"

echo "== Phase 1.1 Ship Check =="
echo "root=$ROOT_DIR"
echo "db=$DB_PATH"
echo "source=$SOURCE paper_size=$PAPER_SIZE"
echo "ship_check_overrides: min_edge=$SHIP_MIN_EDGE max_disagree=$SHIP_MAX_DISAGREE"
echo

echo "1) Compile gate..."
python3 -m py_compile orchestrator.py live_runner.py

echo "2) Run orchestrator (writes runs/agent_runs/arbiter_runs)..."
python3 orchestrator.py

echo "3) Fetch latest run_id..."
RID="$(sqlite3 "$DB_PATH" "select run_id from runs order by id desc limit 1;")"
[ -n "$RID" ] || fail "Latest run_id is empty"
echo "latest run_id=$RID"
echo

echo "4) Run live_runner (deterministic: force candidate to pass filters)..."
mkdir -p signals
rm -f "$SIGNALS_PATH"

python3 live_runner.py \
  --source "$SOURCE" \
  --paper \
  --min-edge "$SHIP_MIN_EDGE" \
  --max-disagree "$SHIP_MAX_DISAGREE" \
  --paper-size "$PAPER_SIZE"

echo "5) Verify signals JSON exists and matches run_id..."
[ -f "$SIGNALS_PATH" ] || fail "Missing signals file: $SIGNALS_PATH"

python3 - <<PY
import json, sys
p = "$SIGNALS_PATH"
rid = "$RID"
with open(p, "r", encoding="utf-8") as f:
    data = json.load(f)
if not isinstance(data, list) or not data:
    print("signals JSON is not a non-empty list")
    sys.exit(2)
c = data[0]
missing = [k for k in ("run_id","market_id","question","venue","side","consensus_p_yes","disagreement","size_usd","reason","status","ts_utc") if k not in c]
if missing:
    print("candidate missing keys:", missing)
    sys.exit(3)
if c["run_id"] != rid:
    print("candidate run_id mismatch:", c["run_id"], "!=", rid)
    sys.exit(4)
print("signals OK")
PY
echo

echo "6) DB proofs + assertions..."
SQL_TMP="$(mktemp)"
trap 'rm -f "$SQL_TMP"' EXIT
sed "s/__RUN_ID__/$RID/g" "$ASSERT_SQL" > "$SQL_TMP"
sqlite3 "$DB_PATH" < "$SQL_TMP"

ARB_COUNT="$(sqlite3 "$DB_PATH" "select count(*) from arbiter_runs where run_id='$RID';")"
[ "$ARB_COUNT" = "1" ] || fail "arbiter_runs count for run_id=$RID expected 1, got $ARB_COUNT"

PT_COUNT="$(sqlite3 "$DB_PATH" "select count(*) from paper_trades where run_id='$RID';")"
[ "$PT_COUNT" -ge 1 ] || fail "paper_trades count for run_id=$RID expected >=1, got $PT_COUNT"

PT_REASON="$(sqlite3 "$DB_PATH" "select reason from paper_trades where run_id='$RID' order by id desc limit 1;")"
[ "$PT_REASON" = "arbiter" ] || fail "latest paper_trade reason for run_id=$RID expected 'arbiter', got '$PT_REASON'"

echo
echo "SHIP_CHECK PASS: run_id=$RID (arbiter + signals + paper trade OK)"
