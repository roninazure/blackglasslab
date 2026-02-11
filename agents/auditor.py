from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, Optional

from scoring.brier import brier_score, brier_to_reward

@dataclass
class ScoreOutput:
    accuracy_score: int          # +1 / -1
    brier: float                 # lower is better
    reward: float                # higher is better (1 - brier)
    overconfidence_penalty: float
    total_score: float
    notes: str
    meta: Dict[str, Any]

def _side_conf_to_prob_yes(side: str, conf: float) -> float:
    """
    Convert (side, confidence) to a probability of YES.
    If side=YES: P(YES)=conf
    If side=NO:  P(YES)=1-conf
    """
    c = max(0.5, min(0.99, float(conf)))
    if side.upper() == "YES":
        return c
    return 1.0 - c

def score_prediction(pred_side: str, pred_conf: float, outcome_side: str, rolling_accuracy: Optional[float]) -> ScoreOutput:
    outcome_yes = (outcome_side.upper() == "YES")
    pred_yes = _side_conf_to_prob_yes(pred_side, pred_conf)

    # Accuracy for human-friendly reporting
    correct = (pred_side.upper() == outcome_side.upper())
    accuracy_score = 1 if correct else -1

    # Proper calibration score
    brier = float(brier_score(pred_yes, outcome_yes))
    reward = float(brier_to_reward(brier))  # 1 - brier

    # Overconfidence penalty remains as a guardrail against inflated confidence
    ra = rolling_accuracy if rolling_accuracy is not None else 0.55
    margin = float(pred_conf) - float(ra)
    penalty = 0.0
    if margin > 0.10:
        penalty = round((margin - 0.10) * 2.0, 4)

    # Total score: combine reward (0..1) with a small accuracy term (+/-0.5) and penalty
    # This keeps "being right" visible but rewards calibration more than bravado.
    total = float(reward + (0.5 if correct else -0.5) - penalty)

    notes = (
        f"Correct={correct}. P(YES)={pred_yes:.2f}. "
        f"Brier={brier:.3f} Reward={reward:.3f}. "
        f"RollingAcc={ra:.2f} Conf={pred_conf:.2f} Penalty={penalty:.4f}."
    )

    return ScoreOutput(
        accuracy_score=accuracy_score,
        brier=brier,
        reward=reward,
        overconfidence_penalty=penalty,
        total_score=total,
        notes=notes,
        meta={"agent": "auditor_v0.2", "scoring": "brier+accuracy+penalty"},
    )
