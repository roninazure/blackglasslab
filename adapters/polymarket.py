# adapters/polymarket.py
from __future__ import annotations

from typing import Any, Dict, List


def load_polymarket_markets(limit: int = 25) -> List[Dict[str, Any]]:
    """
    Stub for weekend ship: keep ingestion separate from trading.
    Next step will implement real fetch + normalization.

    Required normalized fields per market:
      market_id, question, outcome (if resolved else "UNKNOWN")
      plus optional fields: price_yes, volume, end_ts, etc.
    """
    raise RuntimeError(
        "Polymarket adapter not implemented yet. For now run with --source fake. "
        "Weekend ship path: validate pipeline end-to-end on fake, then wire live fetch."
    )
