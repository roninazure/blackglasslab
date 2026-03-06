#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

BGL_INFER_USE_LLM="${BGL_INFER_USE_LLM:-0}" \
BGL_INFER_BATCH="${BGL_INFER_BATCH:-50}" \
BGL_INFER_COOLDOWN="${BGL_INFER_COOLDOWN:-0}" \
BGL_MIN_EDGE_ABS="${BGL_MIN_EDGE_ABS:-0.0}" \
BGL_MIN_EDGE_VS_MARKET="${BGL_MIN_EDGE_VS_MARKET:-0.003}" \
BGL_MAX_DISAGREE="${BGL_MAX_DISAGREE:-1.0}" \
python3 live_runner.py --source polymarket --infer --paper --loops 1 --sleep 0

echo
echo "== trade_candidates_infer.json =="
cat signals/trade_candidates_infer.json

echo
echo "== infer_diagnostics.json =="
cat signals/infer_diagnostics.json
