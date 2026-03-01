#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "Cleanup runtime artifacts (safe)..."

# Signals are runtime artifacts
rm -f signals/trade_candidates.json 2>/dev/null || true

# Optional log pruning: set BGL_PRUNE_LOGS_DAYS=7 (or similar) if desired
if [ "${BGL_PRUNE_LOGS_DAYS:-}" != "" ]; then
  DAYS="${BGL_PRUNE_LOGS_DAYS}"
  if [[ "$DAYS" =~ ^[0-9]+$ ]]; then
    echo "Pruning logs older than ${DAYS} days..."
    find logs -type f -name 'run_*.json' -mtime "+$DAYS" -print -delete 2>/dev/null || true
  else
    echo "BGL_PRUNE_LOGS_DAYS is not an integer; skipping log pruning."
  fi
fi

echo "Cleanup done."
