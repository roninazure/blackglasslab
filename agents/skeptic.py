from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any

@dataclass
class AgentOutput:
    side: str
    confidence: float
    rationale: str
    meta: Dict[str, Any]

def run(market: Dict[str, Any], operator_side: str, operator_conf: float, operator_rationale: str) -> AgentOutput:
    """
    v0 stub: always takes the opposite side and attacks overconfidence.
    """
    side = "NO" if operator_side == "YES" else "YES"
    # Skeptic confidence inversely related to operator confidence (but capped)
    confidence = max(0.50, min(0.80, 1.0 - operator_conf + 0.10))

    rationale = (
        f"Counter-position: {side}. "
        f"Attack: Operator confidence ({operator_conf:.2f}) exceeds evidence quality in v0. "
        f"Critique: '{operator_rationale}' is heuristic-only; missing data."
    )

    return AgentOutput(
        side=side,
        confidence=float(confidence),
        rationale=rationale,
        meta={"agent": "skeptic_v0", "method": "opposition"},
    )
