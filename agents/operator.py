from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any
import random

@dataclass
class AgentOutput:
    side: str          # "YES" or "NO"
    confidence: float  # 0.50 - 0.99
    rationale: str
    meta: Dict[str, Any]

def run(market: Dict[str, Any], state: Dict[str, Any] | None = None) -> AgentOutput:
    """
    v0.3 operator: heuristic + seeded variability.
    State example: {"mode": "...", "seed": 123}
    """
    state = state or {}
    seed = int(state.get("seed", 1337))
    mode = str(state.get("mode", "heuristic_yes_bias"))

    # Seeded RNG ensures reproducibility per market_id + seed
    rng = random.Random(seed + (hash(market.get("market_id", "")) % 10_000))

    q = (market.get("question") or "").lower()

    # Base heuristic
    if "not" in q or "no " in q:
        side = "NO"
        conf = 0.58
        rationale = "Heuristic: negation detected; leaning NO."
    elif "will" in q or "expected" in q:
        side = "YES"
        conf = 0.62
        rationale = "Heuristic: forward-looking phrasing; leaning YES."
    else:
        side = rng.choice(["YES", "NO"])
        conf = 0.55
        rationale = "Heuristic: unclear; seeded pick."

    # Mutations: occasional flip + confidence jitter
    if mode.startswith("mutant_"):
        if rng.random() < 0.25:
            side = "NO" if side == "YES" else "YES"
            rationale += " (mutant flip)"
        conf = max(0.50, min(0.80, conf + rng.uniform(-0.06, 0.06)))

    return AgentOutput(
        side=side,
        confidence=float(max(0.50, min(0.99, conf))),
        rationale=f"{rationale} mode={mode} seed={seed}",
        meta={"agent": "operator_v0.3", "method": "heuristic+seed", "mode": mode, "seed": seed},
    )
