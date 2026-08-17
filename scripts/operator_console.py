#!/usr/bin/env python3
"""Swarm Edge read-only operator-console entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Direct execution places scripts/ first on sys.path.  That directory contains
# executable wrappers whose names may collide with real packages.  Make the
# repository/release root authoritative before importing application modules.
root_string = str(ROOT)
sys.path[:] = [entry for entry in sys.path if entry != root_string]
sys.path.insert(0, root_string)

from operator_console.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
