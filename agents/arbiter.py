from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Dict, List


@dataclass
class ArbiterResult:
    consensus_side: str
    consensus_p_yes: float
    disagreement: float
    winner_agent: str
    winner_fitness: float
    notes: str


def p_yes(side: str, conf: float) -> float:
    c = float(conf)
    if side.upper() == "YES":
        return c
    return 1.0 - c


def stdev(xs: List[float]) -> float:
    if not xs:
        return 0.0
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / len(xs)
    return sqrt(var)


def arbitrate(agent_rows: List[Dict[str, Any]]) -> ArbiterResult:
    """
    agent_rows: list of dicts with keys:
      agent_name, side, conf, fitness, reward, brier
    """
    if not agent_rows:
        return ArbiterResult(
            consensus_side="NO",
            consensus_p_yes=0.0,
            disagreement=0.0,
            winner_agent="none",
            winner_fitness=0.0,
            notes="No agents provided.",
        )

    pys = [p_yes(r["side"], r["conf"]) for r in agent_rows]
    disagreement = stdev(pys)

    # Weight consensus by (fitness clipped) so high-quality agents influence more, but nobody dominates infinitely.
    weights = []
    for r in agent_rows:
        f = float(r.get("fitness", 0.0))
        w = max(0.05, min(2.0, 0.5 + f))  # 0.05..2.0
        weights.append(w)

    wsum = sum(weights) or 1.0
    consensus_p_yes = sum(w * py for w, py in zip(weights, pys)) / wsum
    consensus_side = "YES" if consensus_p_yes >= 0.5 else "NO"

    winner = max(agent_rows, key=lambda r: float(r.get("fitness", 0.0)))
    notes = (
        f"ConsensusPYES={consensus_p_yes:.3f} Disagreement(stdevPYES)={disagreement:.3f}. "
        f"Winner={winner['agent_name']} fitness={float(winner.get('fitness',0.0)):.3f}."
    )

    return ArbiterResult(
        consensus_side=consensus_side,
        consensus_p_yes=float(consensus_p_yes),
        disagreement=float(disagreement),
        winner_agent=str(winner["agent_name"]),
        winner_fitness=float(winner.get("fitness", 0.0)),
        notes=notes,
    )
