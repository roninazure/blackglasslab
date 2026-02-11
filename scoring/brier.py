from __future__ import annotations

def brier_score(prob_yes: float, outcome_yes: bool) -> float:
    """
    Brier score for binary events.
    Lower is better. Range: [0, 1].
    """
    p = max(0.0, min(1.0, float(prob_yes)))
    o = 1.0 if outcome_yes else 0.0
    return (p - o) ** 2

def brier_to_reward(brier: float) -> float:
    """
    Convert Brier score to a reward-like number where higher is better.
    Simple mapping: reward = 1 - brier
    Range: [0, 1], higher is better.
    """
    b = max(0.0, min(1.0, float(brier)))
    return 1.0 - b
