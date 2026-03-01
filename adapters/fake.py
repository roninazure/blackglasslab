# adapters/fake.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def load_fake_markets(path: str = "markets/fake_markets.json") -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Missing {p}.")
    return json.loads(p.read_text(encoding="utf-8"))

