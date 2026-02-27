from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
import random


@dataclass
class SkepticOutput:
    side: str
    confidence: float
    rationale: str


def _seed_from_state(state: Optional[Dict[str, Any]]) -> int:
    if not state:
        return 1337
    try:
        return int(state.get("seed", 1337))
    except Exception:
        return 1337


def _mode_from_state(state: Optional[Dict[str, Any]]) -> str:
    if not state:
        return "always_opposite"
    return str(state.get("mode", "always_opposite"))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def run(
    market: Dict[str, Any],
    operator_side: str,
    operator_conf: float,
    operator_rationale: str,
    skeptic_state: Optional[Dict[str, Any]] = None,
) -> SkepticOutput:
    """
    Skeptic agent.
    v0.6-compatible: accepts skeptic_state (mode/seed) so orchestrator can run a swarm.
    Modes supported:
      - always_opposite (default)
      - always_no
      - mirror_confidence
    """
    seed = _seed_from_state(skeptic_state)
    mode = _mode_from_state(skeptic_state)

    rng = random.Random(seed)

    op_side = str(operator_side).upper()
    op_conf = _clamp01(operator_conf)

    if mode == "always_no":
        side = "NO"
        conf = max(0.50, min(0.75, 0.55 + 0.10 * rng.random()))
        rationale = "Mode=always_no. Defaulting to NO with moderate confidence."

    elif mode == "mirror_confidence":
        # Mirror confidence but flip direction relative to operator
        side = "NO" if op_side == "YES" else "YES"
        conf = _clamp01(op_conf)
        rationale = (
            f"Mode=mirror_confidence. Flipping operator side ({op_side}) while mirroring confidence ({op_conf:.2f})."
        )

    else:
        # always_opposite (default)
        side = "NO" if op_side == "YES" else "YES"
        # Slightly damp confidence so skeptic isn't unrealistically certain
        conf = _clamp01(0.50 + (op_conf - 0.50) * 0.85)
        rationale = (
            f"Mode=always_opposite. Operator said {op_side} ({op_conf:.2f}). "
            f"Skeptic takes the other side with damped confidence ({conf:.2f})."
        )

    # Lightly reference operator rationale for traceability
    if operator_rationale:
        rationale += f" Operator rationale (ref): {operator_rationale[:180]}"

    return SkepticOutput(side=side, confidence=float(conf), rationale=rationale)
